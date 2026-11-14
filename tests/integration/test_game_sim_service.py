"""Live I/O proof for `services/game_sim_service.py`
(phase-1-simulation-engine.md §8.3/§9 Tier 4, §12 step 8).

Uses `tools.seed_dev_league.seed_dev_league` (full calibrated 30x14 league
with ratings/tendencies/team_strategy already written -- the same
ready-to-sim fixture `test_strategy_service.py` uses) plus
`schedule_service.generate_regular_season` for real scheduled games, rather
than hand-building a `GameContext` -- this module is specifically proving
the DB round trip, not the engine's own math (that's `tests/unit/sim/`'s
job).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as every other tests/integration/ module.
"""
from __future__ import annotations

import json
import random
from unittest.mock import AsyncMock, patch

import pytest

from dba2.core.errors import ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, player_repo, stats_repo
from dba2.domain.game import GameStatus
from dba2.phase import entity_states
from dba2.services import game_sim_service, schedule_service
from tests.fixtures.beat_log import beat_record_from_json
from tools import seed_dev_league


async def _seed_league_with_schedule(pool, *, name: str, rng_seed: int) -> int:
    league_id = await seed_dev_league.seed_dev_league(
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name=name,
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=rng_seed,
    )
    await schedule_service.generate_regular_season(pool, league_id, 2026)
    return league_id


@pytest.mark.asyncio
async def test_sim_game_reconciles_box_stats_and_period_scores_and_finalizes():
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _seed_league_with_schedule(
            pool, name="Sim Game Reconcile League", rng_seed=501
        )
        games = await game_repo.list_games_for_league_season(pool, league_id, 2026)
        game = games[0]
        assert game.status == GameStatus.SCHEDULED

        result = await game_sim_service.sim_game(pool, game.id)

        # Period scores sum to the final score (§9 Tier-4 criterion).
        assert sum(h for _, h, _a in result.period_scores) == result.home_score
        assert sum(a for _, _h, a in result.period_scores) == result.away_score

        # Box stats sum to the persisted final score.
        team_stats = {
            t.team_id: t for t in await stats_repo.list_team_game_stats_for_game(pool, game.id)
        }
        assert team_stats[game.home_team_id].points == result.home_score
        assert team_stats[game.away_team_id].points == result.away_score

        player_stats = await stats_repo.list_player_game_stats_for_game(pool, game.id)
        assert len(player_stats) == len(result.player_box_lines)
        points_by_team: dict[int, int] = {game.home_team_id: 0, game.away_team_id: 0}
        for row in player_stats:
            points_by_team[row.team_id] += row.points
        assert points_by_team[game.home_team_id] == result.home_score
        assert points_by_team[game.away_team_id] == result.away_score

        # Period rows in the DB also sum correctly, independent of the
        # in-memory GameResult check above.
        period_rows = await stats_repo.list_game_period_scores(pool, game.id)
        db_home_total = sum(p.points for p in period_rows if p.team_id == game.home_team_id)
        db_away_total = sum(p.points for p in period_rows if p.team_id == game.away_team_id)
        assert db_home_total == result.home_score
        assert db_away_total == result.away_score

        finalized = await game_repo.get_game(pool, game.id)
        assert finalized.status == GameStatus.FINAL
        assert finalized.home_score == result.home_score
        assert finalized.away_score == result.away_score
        assert finalized.overtime_periods == result.overtime_periods
        assert finalized.roster_digest is not None
        assert finalized.engine_version > 0
        assert finalized.simmed_at is not None
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_sim_game_write_beat_log_writes_a_file_that_round_trips_with_the_terminal_payload(
    tmp_path, monkeypatch
):
    """`write_beat_log=True` is opt-in and had no test. Since watch-v2 E-1 the
    file carries the nested terminal payload; the file must rebuild into
    exactly the `GameResult.beat_log` the call returned."""
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _seed_league_with_schedule(
            pool, name="Sim Game Beat Log League", rng_seed=503
        )
        game = (await game_repo.list_games_for_league_season(pool, league_id, 2026))[0]

        # `_write_beat_log` writes under a relative `artifacts/` path; run the
        # sim from a temp directory so nothing lands in the repo.
        monkeypatch.chdir(tmp_path)
        result = await game_sim_service.sim_game(pool, game.id, write_beat_log=True)

        path = tmp_path / "artifacts" / "beatlogs" / str(league_id) / "2026" / f"{game.id}.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == len(result.beat_log) > 0
        rebuilt = [beat_record_from_json(json.loads(line)) for line in lines]
        assert rebuilt == list(result.beat_log)
        assert any(r.terminal is not None for r in rebuilt)
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_sim_game_rejects_a_final_game_without_resim_and_a_scheduled_game_with_resim():
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _seed_league_with_schedule(
            pool, name="Sim Game Status Guard League", rng_seed=502
        )
        games = await game_repo.list_games_for_league_season(pool, league_id, 2026)
        already_final_game, still_scheduled_game = games[0], games[1]

        await game_sim_service.sim_game(pool, already_final_game.id)

        with pytest.raises(ValidationError, match="not scheduled"):
            await game_sim_service.sim_game(pool, already_final_game.id, resim=False)

        with pytest.raises(ValidationError, match="requires game .* to be final"):
            await game_sim_service.sim_game(pool, still_scheduled_game.id, resim=True)
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_resim_status_guard_actually_reads_game_transitions():
    """Critic round 19 B19-2: `sim_game`'s resim-status-transition legality
    check used to be a hardcoded reimplementation of `entity_states.
    GAME_TRANSITIONS` with zero production readers of the table anywhere in
    the codebase -- editing `GAME_TRANSITIONS` changed zero behavior. Proof
    this is now wired for real: temporarily removing the declared `FINAL ->
    IN_PROGRESS` edge (Decision P1-26) must make an otherwise-legal resim
    fail, with `game.status` itself unchanged (still `final`) the whole
    time -- if this test still passed with the edge removed, the service
    would still be reading its own hardcoded copy instead of the table."""
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _seed_league_with_schedule(
            pool, name="Game Transitions Table Wiring League", rng_seed=504
        )
        game = (await game_repo.list_games_for_league_season(pool, league_id, 2026))[0]
        await game_sim_service.sim_game(pool, game.id)
        assert (await game_repo.get_game(pool, game.id)).status == GameStatus.FINAL

        original_final_edges = entity_states.GAME_TRANSITIONS[GameStatus.FINAL]
        entity_states.GAME_TRANSITIONS[GameStatus.FINAL] = frozenset()
        try:
            with pytest.raises(ValidationError, match="requires game .* to be final"):
                await game_sim_service.sim_game(pool, game.id, resim=True)
            # The guard rejected it before ever touching status -- still final.
            assert (await game_repo.get_game(pool, game.id)).status == GameStatus.FINAL
        finally:
            entity_states.GAME_TRANSITIONS[GameStatus.FINAL] = original_final_edges

        # Table restored -- the identical resim call now succeeds again,
        # proving the earlier failure came from the table, not from some
        # other side effect of mutating the dict.
        await game_sim_service.sim_game(pool, game.id, resim=True)
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_resim_replaces_rather_than_duplicates_rows_including_a_shrinking_period_count():
    """Decision P1-25, plus the exact edge case critic caught once in step
    6 at the repo layer (`test_stats_repo.py::
    test_delete_stats_for_game_clears_stale_period_scores_on_resim`):
    a resim that produces *fewer* periods than a previous run must not
    leave the previous run's trailing period rows behind. Proven here by
    manually injecting a stale extra-period row before the resim (standing
    in for "a previous sim went to overtime") and asserting it's gone
    afterward -- `sim_game`'s own determinism means two sims of the same
    game/seed always produce the same period count on their own, so this
    is the only way to exercise the shrinking case without hunting for a
    seed that happens to go to overtime on one run and not the other."""
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _seed_league_with_schedule(
            pool, name="Sim Game Resim Shrink League", rng_seed=503
        )
        games = await game_repo.list_games_for_league_season(pool, league_id, 2026)
        game = games[0]

        first_result = await game_sim_service.sim_game(pool, game.id)
        first_player_ids = {row.player_id for row in first_result.player_box_lines}
        real_period_count = len(first_result.period_scores)

        # Simulate "a previous run went to a 5th (OT) period" by injecting
        # a stale extra period row this deterministic re-run will never
        # itself produce.
        stale_period = real_period_count + 1
        await stats_repo.upsert_game_period_scores(
            pool,
            [
                {
                    "game_id": game.id,
                    "league_id": game.league_id,
                    "team_id": game.home_team_id,
                    "period": stale_period,
                    "points": 11,
                },
                {
                    "game_id": game.id,
                    "league_id": game.league_id,
                    "team_id": game.away_team_id,
                    "period": stale_period,
                    "points": 9,
                },
            ],
        )
        periods_before_resim = await stats_repo.list_game_period_scores(pool, game.id)
        assert any(p.period == stale_period for p in periods_before_resim)

        second_result = await game_sim_service.sim_game(pool, game.id, resim=True)

        # Determinism: same seed, same roster -> byte-identical result.
        assert second_result.home_score == first_result.home_score
        assert second_result.away_score == first_result.away_score
        assert len(second_result.period_scores) == real_period_count

        periods_after_resim = await stats_repo.list_game_period_scores(pool, game.id)
        assert not any(p.period == stale_period for p in periods_after_resim), (
            "delete_stats_for_game must clear a stale trailing period before the resim "
            "re-inserts -- an upsert-only write would have left this period behind"
        )
        assert len(periods_after_resim) == 2 * real_period_count

        # Rows replaced, not duplicated, for the box-score tables too.
        second_player_stats = await stats_repo.list_player_game_stats_for_game(pool, game.id)
        assert {row.player_id for row in second_player_stats} == first_player_ids
        assert len(second_player_stats) == len(first_player_ids)
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_sim_game_rolls_back_entirely_on_an_injected_mid_write_failure():
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _seed_league_with_schedule(
            pool, name="Sim Game Atomicity League", rng_seed=504
        )
        games = await game_repo.list_games_for_league_season(pool, league_id, 2026)
        game = games[0]

        with patch.object(
            game_sim_service.stats_repo,
            "create_team_game_stats_many",
            new=AsyncMock(side_effect=RuntimeError("injected mid-write failure")),
        ):
            with pytest.raises(RuntimeError, match="injected mid-write failure"):
                await game_sim_service.sim_game(pool, game.id)

        # Nothing from the failed sim survived: status still scheduled, no
        # player_game_stats/period rows, no finalized score.
        after = await game_repo.get_game(pool, game.id)
        assert after.status == GameStatus.SCHEDULED
        assert after.home_score is None
        assert after.simmed_at is None

        player_stats = await stats_repo.list_player_game_stats_for_game(pool, game.id)
        assert player_stats == []
        period_rows = await stats_repo.list_game_period_scores(pool, game.id)
        assert period_rows == []

        # And the game is still perfectly simmable afterward -- the failed
        # attempt didn't leave it stuck in a state sim_game's own status
        # guard would reject.
        result = await game_sim_service.sim_game(pool, game.id)
        assert result.home_score is not None
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_build_context_excludes_non_active_players_from_the_available_roster():
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _seed_league_with_schedule(
            pool, name="Build Context Roster Filter League", rng_seed=505
        )
        games = await game_repo.list_games_for_league_season(pool, league_id, 2026)
        game = games[0]

        home_players = await player_repo.list_players_for_team(pool, game.home_team_id)
        benched = home_players[0]
        from dba2.domain.player import RosterStatus

        await pool.execute(
            "UPDATE players SET roster_status = $2 WHERE id = $1",
            benched.id,
            RosterStatus.INJURED.value,
        )

        context = await game_sim_service.build_context(pool, game)
        home_ids = {p.id for p in context.home.players}
        assert benched.id not in home_ids
        assert len(home_ids) == len(home_players) - 1
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()
