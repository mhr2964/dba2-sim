"""Live I/O proof for `tools/seed_dev_league.py`'s per-player atomicity and
(§12 step 7) the calibrated-generator rewrite (Decision S7-14).

Phase 0 step-9 critic round, B3: `_create_random_player` (this module's
predecessor, before the §11.4 rewrite replaced it with
`_create_synthetic_player`) used to perform its writes as independent
statements on the raw pool, with no transaction. A failure between the
ratings write and the recompute call left a committed player permanently
stuck at its `overall_cached=1` placeholder -- reproduced live by critic
via an injected mid-loop failure. The fix wraps every per-player write in
one `transaction(pool)` block; `test_create_synthetic_player_rolls_back_
entirely_on_a_mid_write_failure` proves the rollback survived the rewrite
by injecting the same kind of failure and asserting nothing was left
behind.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as every other tests/integration/ module.
"""
from __future__ import annotations

import random
from unittest.mock import AsyncMock, patch

import pytest

from dba2.core.rng import rng_for
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, player_repo, ratings_repo, team_repo
from dba2.domain.player import Position
from dba2.services import strategy_service
from dba2.sim import derivation as d
from tools import seed_dev_league


@pytest.mark.asyncio
async def test_create_synthetic_player_rolls_back_entirely_on_a_mid_write_failure():
    pool = await get_pool()
    try:
        league = await league_repo.create_league(
            pool,
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="Seed Atomicity Test League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=1,
        )
        try:
            team = await team_repo.create_team(
                pool,
                league_id=league.id,
                code="ATM",
                name="Atomic Testers",
                city="Testburg",
                conference="East",
                division="Atlantic",
                home_market_size=1,
            )
            records: list = []

            with patch.object(
                seed_dev_league.ratings_service,
                "recompute_overall",
                new=AsyncMock(side_effect=RuntimeError("critic B3 injected failure")),
            ):
                with pytest.raises(RuntimeError, match="critic B3 injected failure"):
                    await seed_dev_league._create_synthetic_player(
                        pool,
                        league_id=league.id,
                        team_id=team.id,
                        league_seed=1,
                        team_index=0,
                        slot_index=0,
                        position=Position.PG,
                        tier=d.RosterTier.STAR,
                        team_offset=0.0,
                        bio_rng=rng_for(1, "test", "bio"),
                        current_season=2026,
                        records=records,
                    )

            # Nothing from the failed player must have survived -- not a
            # player row stuck at the overall_cached=1 placeholder, no
            # orphaned ratings row, and nothing appended to `records`.
            players = await player_repo.list_players_for_team(pool, team.id)
            assert players == []
            assert records == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_seed_dev_league_persists_a_calibrated_thirty_by_fourteen_league():
    """§12 step 7's first verify bullet: a live seeded league of 30x14
    actually persists correctly -- every team has 14 players, every player
    has a ratings row (55 fields, `overall_cached` in range) and a
    tendencies row (17 fields, shot frequencies summing to 100 per the DB
    CHECK), and every team has a `team_strategy` row for the season."""
    pool = await get_pool()
    league_id = None
    try:
        league_id = await seed_dev_league.seed_dev_league(
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="Calibrated Dev League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=42,
        )

        teams = await team_repo.list_teams_for_league(pool, league_id)
        assert len(teams) == 30

        for team in teams:
            players = await player_repo.list_players_for_team(pool, team.id)
            assert len(players) == 14, f"team {team.id} has {len(players)} players, expected 14"

            strategy = await team_repo.get_team_strategy(pool, team.id, 2026)
            assert strategy is not None

            for player in players:
                ratings = await ratings_repo.get_player_ratings(pool, player.id)
                assert ratings is not None
                assert 0 <= player.overall_cached <= 99

                tendencies = await ratings_repo.get_player_tendencies(pool, player.id)
                assert tendencies is not None
                assert (
                    tendencies.shot_freq_rim
                    + tendencies.shot_freq_mid
                    + tendencies.shot_freq_three
                    + tendencies.shot_freq_post
                    == 100
                )
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_seed_dev_league_same_seed_reproduces_identical_overall_cached():
    """§12 step 7's second verify bullet: re-running the tool at the same
    seed reproduces identical `overall_cached` values for every player --
    the direct check on Decision S7-14's `core.rng.rng_for`-based
    reproducibility claim. Compares by (team code, roster slot) rather
    than by player id, since ids are assigned by the database and differ
    across the two runs."""
    pool = await get_pool()
    league_ids: list[int] = []
    try:

        async def _overalls_by_team_code(league_id: int) -> dict[str, list[int]]:
            teams = await team_repo.list_teams_for_league(pool, league_id)
            result: dict[str, list[int]] = {}
            for team in teams:
                players = await player_repo.list_players_for_team(pool, team.id)
                # `list_players_for_team` orders by `overall_cached DESC` --
                # a stable, seed-independent ordering to compare slot-by-slot.
                result[team.code] = [p.overall_cached for p in players]
            return result

        for _ in range(2):
            league_id = await seed_dev_league.seed_dev_league(
                discord_guild_id=random.randint(10_000_000, 2_000_000_000),
                name="Reproducibility Test League",
                start_season_year=2026,
                current_season=2026,
                commissioner_user_id=1,
                rng_seed=777,
            )
            league_ids.append(league_id)

        first = await _overalls_by_team_code(league_ids[0])
        second = await _overalls_by_team_code(league_ids[1])
        assert first == second
    finally:
        for league_id in league_ids:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_seed_dev_league_same_seed_reproduces_identical_tendencies_and_team_strategy():
    """Critic round 17, B17-2: the prior tendency-generation stream was
    salted by the database-assigned serial `player.id`
    (`rng_for(league_seed, "tendencies", player.id)`), which is not stable
    across two separate runs of this tool at the same `--rng-seed` (a fresh
    insert never gets the same id twice) -- so `overall_cached` reproduced
    at a fixed seed but `player_tendencies` (419/420 rows) and every
    `team_strategy` numeric that reads a tendency field (30/30 rows) did
    not, live-measured. The fix salts by this tool's own stable per-slot
    identity, `(team_index, slot_index)`, the same one every other
    generation stream already uses. Compares by (team code, roster slot)
    rather than by player id, same reasoning as the `overall_cached`
    determinism test above."""
    pool = await get_pool()
    league_ids: list[int] = []
    try:

        async def _snapshot(league_id: int):
            teams = await team_repo.list_teams_for_league(pool, league_id)
            tendencies_by_team: dict[str, list[tuple]] = {}
            strategy_by_team: dict[str, tuple] = {}
            for team in teams:
                # `list_players_for_team` orders by `overall_cached DESC` --
                # a stable, seed-independent ordering to compare slot-by-slot.
                players = await player_repo.list_players_for_team(pool, team.id)
                rows = []
                for player in players:
                    t = await ratings_repo.get_player_tendencies(pool, player.id)
                    rows.append(
                        tuple(
                            sorted(
                                (k, v)
                                for k, v in vars(t).items()
                                if k not in {"player_id", "league_id", "updated_at"}
                            )
                        )
                    )
                tendencies_by_team[team.code] = rows

                strategy = await team_repo.get_team_strategy(pool, team.id, 2026)
                strategy_by_team[team.code] = tuple(
                    sorted(
                        (k, v)
                        for k, v in vars(strategy).items()
                        if k not in {"team_id", "league_id", "season", "set_by", "updated_at"}
                    )
                )
            return tendencies_by_team, strategy_by_team

        for _ in range(2):
            league_id = await seed_dev_league.seed_dev_league(
                discord_guild_id=random.randint(10_000_000, 2_000_000_000),
                name="Tendency Reproducibility Test League",
                start_season_year=2026,
                current_season=2026,
                commissioner_user_id=1,
                rng_seed=4242,
            )
            league_ids.append(league_id)

        first_tendencies, first_strategy = await _snapshot(league_ids[0])
        second_tendencies, second_strategy = await _snapshot(league_ids[1])
        assert first_tendencies == second_tendencies
        assert first_strategy == second_strategy
    finally:
        for league_id in league_ids:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_emit_targets_writes_the_section_7_artifact(tmp_path):
    """§12 step 7's third verify bullet: `--emit-targets` produces the
    file, shaped per §7 (11 groups, 17 tendency fields, `by_position`
    keyed by all 5 positions)."""
    import json

    pool = await get_pool()
    league_id = None
    try:
        target_path = tmp_path / "synthetic_calibration_targets.json"
        league_id = await seed_dev_league.seed_dev_league(
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="Emit Targets Test League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=99,
            emit_targets_path=str(target_path),
        )

        payload = json.loads(target_path.read_text(encoding="utf-8"))
        assert payload["league_seed"] == 99
        assert payload["teams"] == 30
        assert payload["players_per_team"] == 14
        assert set(payload["groups"]) == set(d.RATING_GROUPS)
        assert len(payload["tendencies"]) == 17
        assert set(payload["by_position"]) == {p.value for p in Position}
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_ensure_defaults_for_season_is_idempotent_after_seeding():
    """Bridges §12 step 5 and step 7: the seed tool already calls
    `strategy_service.ensure_defaults_for_season` once; calling it again on
    the same league writes 0 additional rows."""
    pool = await get_pool()
    league_id = None
    try:
        league_id = await seed_dev_league.seed_dev_league(
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="Idempotence Test League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=7,
        )

        rows_written = await strategy_service.ensure_defaults_for_season(pool, league_id, 2026)
        assert rows_written == 0
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("season", [2025, 2024, 2023])
async def test_real_team_rosters_put_each_real_player_on_the_team_he_played_for(season):
    """`--real-team-rosters`: with 14 real players per team, every seeded
    player carries a real `external_ref` and sits on the franchise the
    season file names for that ref, so no rating profile lands on a team it
    never played for. Runs on the RPD-8 version-2 seeds (2023-2025)."""
    import json

    pool = await get_pool()
    league_id = None
    try:
        league_id = await seed_dev_league.seed_dev_league(
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="Real Rosters Dev League",
            start_season_year=2026,
            current_season=2026,
            commissioner_user_id=1,
            rng_seed=42,
            real_players_season=season,
            real_players_per_team=14,
            real_team_rosters=True,
        )
        real_team_of = {
            row["external_ref"]: row["team"]["abbreviation"]
            for row in json.loads(
                (seed_dev_league._REAL_PLAYERS_DIR / f"{season}.json").read_text(encoding="utf-8")
            )["players"]
        }
        teams = await team_repo.list_teams_for_league(pool, league_id)
        for team in teams:
            players = await player_repo.list_players_for_team(pool, team.id)
            assert len(players) == 14
            for player in players:
                real_code = real_team_of[player.external_ref]
                seeded_code = seed_dev_league._REAL_DATA_TEAM_CODE.get(team.code, team.code)
                assert seeded_code == real_code, (
                    f"{player.external_ref} is a {real_code} "
                    f"player seeded onto {team.code}"
                )
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()
