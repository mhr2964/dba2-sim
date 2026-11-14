"""Live I/O proof for `services/strategy_service.py`
(docs/design/phase-1-step-7-calibration.md §9, §12 step 5).

Builds full 30x14 leagues via `tools.seed_dev_league.seed_dev_league`
(which already calls `ensure_defaults_for_season` once during seeding, per
§11.4) rather than hand-rolling rosters here -- the same calibrated
generator §9's own bands are stated against, so a band failure here is a
real signal rather than an artifact of a hand-built fixture with no talent
curve.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as every other tests/integration/ module that touches
real Postgres.
"""
from __future__ import annotations

import random
import statistics

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import player_repo, ratings_repo, team_repo
from dba2.services import strategy_service
from dba2.services.strategy_service import (
    _NUMERIC_SPECS,
    NUMERIC_UNDERLYING_FEATURES,
    _build_squads,
    raw_features,
)
from tools import seed_dev_league


async def _seed_league(pool, *, name: str, rng_seed: int) -> int:
    return await seed_dev_league.seed_dev_league(
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name=name,
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=rng_seed,
    )


@pytest.mark.asyncio
async def test_ensure_defaults_for_season_writes_exactly_one_row_per_team():
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _seed_league(pool, name="Strategy One-Row Test League", rng_seed=11)
        teams = await team_repo.list_teams_for_league(pool, league_id)
        assert len(teams) == 30
        for team in teams:
            strategy = await team_repo.get_team_strategy(pool, team.id, 2026)
            assert strategy is not None

        # Seeding already called ensure_defaults_for_season once -- calling
        # it again must write 0 rows (every team already has one).
        rows_written = await strategy_service.ensure_defaults_for_season(pool, league_id, 2026)
        assert rows_written == 0
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_a_partial_rerun_reproduces_the_same_schemes_and_numerics_as_the_full_run():
    """The idempotence-vs-standardization trap named in §9.3/§9.4 and
    §12 step 5: standardization must run over *every* team in the league,
    not just the teams a given call is about to write. Proof: wipe
    `team_strategy` for 5 of 30 teams (simulating "these 5 were never
    written") and re-run -- the recomputed values for exactly those 5
    teams must match what the original full run produced, since the
    roster/ratings/tendencies inputs are unchanged and the standardization
    set (all 30 teams) is unchanged too. The four numeric fields are the
    tighter half of this proof (a standardized score cannot survive a
    perturbed standardization set the way a scheme argmax sometimes can)."""
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _seed_league(
            pool, name="Strategy Partial Rerun Test League", rng_seed=222
        )
        teams = await team_repo.list_teams_for_league(pool, league_id)
        assert len(teams) == 30

        original = {}
        for team in teams:
            strategy = await team_repo.get_team_strategy(pool, team.id, 2026)
            assert strategy is not None
            original[team.id] = strategy

        wiped_team_ids = [t.id for t in teams[:5]]
        for team_id in wiped_team_ids:
            await pool.execute(
                "DELETE FROM team_strategy WHERE league_id = $1 AND team_id = $2",
                league_id,
                team_id,
            )

        rows_written = await strategy_service.ensure_defaults_for_season(pool, league_id, 2026)
        assert rows_written == 5

        for team_id in wiped_team_ids:
            recomputed = await team_repo.get_team_strategy(pool, team_id, 2026)
            assert recomputed is not None
            baseline = original[team_id]
            assert recomputed.offensive_scheme == baseline.offensive_scheme
            assert recomputed.defensive_scheme == baseline.defensive_scheme
            assert recomputed.pace_target == baseline.pace_target
            assert recomputed.help_aggression == baseline.help_aggression
            assert recomputed.three_point_bias == baseline.three_point_bias
            assert recomputed.crash_oreb_bias == baseline.crash_oreb_bias
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("rng_seed", [301, 302, 303])
async def test_section_9_3_and_9_4_bands_hold_on_a_seeded_thirty_team_league(rng_seed):
    """§9.3's scheme-diversity band and §9.4's construction bands (mean,
    SD, range-inside-band), asserted directly against persisted
    `team_strategy` rows at three seeds minimum, per §12 step 5."""
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _seed_league(
            pool, name=f"Strategy Band Test League {rng_seed}", rng_seed=rng_seed
        )
        teams = await team_repo.list_teams_for_league(pool, league_id)
        assert len(teams) == 30

        strategies = [await team_repo.get_team_strategy(pool, t.id, 2026) for t in teams]
        assert all(s is not None for s in strategies)

        # §9.3: at least 3 distinct schemes per side, no scheme > 20 of 30.
        offense_counts: dict[str, int] = {}
        defense_counts: dict[str, int] = {}
        for s in strategies:
            offense_counts[s.offensive_scheme] = offense_counts.get(s.offensive_scheme, 0) + 1
            defense_counts[s.defensive_scheme] = defense_counts.get(s.defensive_scheme, 0) + 1
        assert len(offense_counts) >= 3, offense_counts
        assert len(defense_counts) >= 3, defense_counts
        assert max(offense_counts.values()) <= 20, offense_counts
        assert max(defense_counts.values()) <= 20, defense_counts

        # §9.4: each numeric field's league mean in [49, 51], SD in
        # [9.0, 11.0] (construction), and strictly-inside-band on >= 90%
        # of teams (the clamp-rarely-bites check).
        numeric_fields = ("pace_target", "help_aggression", "three_point_bias", "crash_oreb_bias")
        for field_name in numeric_fields:
            values = [getattr(s, field_name) for s in strategies]
            mean = statistics.mean(values)
            sd = statistics.pstdev(values)
            assert 49.0 <= mean <= 51.0, f"{field_name} mean {mean}"
            assert 9.0 <= sd <= 11.0, f"{field_name} sd {sd}"

            lo, hi = _NUMERIC_SPECS[field_name][1]
            inside = sum(1 for v in values if lo < v < hi)
            assert inside / len(values) >= 0.90, f"{field_name} inside-band fraction {inside}/30"
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_section_9_4_anti_degeneracy_assertion_on_a_seeded_thirty_team_league():
    """§9.4's own required guard (§12 step 5, critic round 15's B15-1
    correction): each of the 10 features the four numeric raw scores
    actually read must have an across-team, pre-standardization SD above
    0.05 -- not the four combined `R[k]` scores, which are sums of
    already-unit-SD inputs and can never themselves read as degenerate."""
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _seed_league(
            pool, name="Strategy Anti-Degeneracy League", rng_seed=404
        )
        teams = await team_repo.list_teams_for_league(pool, league_id)
        team_ids = [t.id for t in teams]

        players = await player_repo.list_players_for_league(pool, league_id)
        all_ratings = await ratings_repo.get_for_league(pool, league_id)
        ratings_by_player = {r.player_id: r for r in all_ratings}
        all_tendencies = await ratings_repo.get_tendencies_for_league(pool, league_id)
        tendencies_by_player = {t.player_id: t for t in all_tendencies}

        squads = _build_squads(team_ids, players, ratings_by_player, tendencies_by_player)
        raw = raw_features(squads, team_ids)

        for feature_name in NUMERIC_UNDERLYING_FEATURES:
            sd = statistics.pstdev(raw[feature_name][tid] for tid in team_ids)
            assert sd > 0.05, f"{feature_name} raw across-team SD {sd} <= 0.05"
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()
