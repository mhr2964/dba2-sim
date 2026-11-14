"""`sim_scheduled_games`'s opt-in per-game records and its inter-game yield
(phase-5-stage-e-advance.md P5-99.i, P5-99.j).

The records exist because the Stage E press has to hand each simmed game to the
block feed with the ids of the game it belongs to, and `GameResult` carries no
game id. A results tuple correlated to the input list by position would attach
the wrong id to every game after the first failed one, which is why every case
below that names a game compares against the `games` row, and one of them
deliberately fails a game in the middle of the batch.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo
from dba2.domain.game import GameStatus, GameType
from dba2.services import game_sim_service
from tests.integration.test_game_sim_service import _seed_league_with_schedule

_SEASON = 2026
# Game indices are zero-based, so this is the first seven games of the schedule --
# enough to fail one in the middle and sim several after it.
_THROUGH = 6


async def _first_games(pool, league_id: int, count: int):
    games = await game_repo.list_scheduled_games(pool, league_id, _SEASON, GameType.REGULAR)
    return [g for g in games if g.game_index <= count]


async def _assert_record_matches_its_row(pool, record) -> None:
    row = await game_repo.get_game(pool, record.game_id)
    assert row is not None
    assert row.status is GameStatus.FINAL
    assert (row.game_date, row.home_team_id, row.away_team_id) == (
        record.game_date,
        record.home_team_id,
        record.away_team_id,
    )
    assert (row.home_score, row.away_score) == (
        record.result.home_score,
        record.result.away_score,
    )
    assert (record.result.home_team_id, record.result.away_team_id) == (
        row.home_team_id,
        row.away_team_id,
    )


@pytest.mark.asyncio
async def test_results_are_empty_by_default():
    pool = await get_pool()
    try:
        league_id = await _seed_league_with_schedule(pool, name="Results Default", rng_seed=11)
        try:
            scheduled = await _first_games(pool, league_id, _THROUGH)
            batch = await game_sim_service.sim_scheduled_games(
                pool, league_id, _SEASON, through_game_index=_THROUGH
            )
            assert batch.games_simmed == len(scheduled) > 1
            assert batch.failed_game_ids == ()
            assert batch.results == ()
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_results_when_asked_hold_one_record_per_simmed_game_matching_its_row():
    pool = await get_pool()
    try:
        league_id = await _seed_league_with_schedule(pool, name="Results Asked", rng_seed=12)
        try:
            scheduled = await _first_games(pool, league_id, _THROUGH)
            batch = await game_sim_service.sim_scheduled_games(
                pool, league_id, _SEASON, through_game_index=_THROUGH, return_results=True
            )

            assert batch.games_simmed == len(scheduled) > 1
            assert len(batch.results) == batch.games_simmed
            assert [r.game_id for r in batch.results] == [g.id for g in scheduled]
            for record in batch.results:
                await _assert_record_matches_its_row(pool, record)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_a_failed_game_in_the_middle_of_a_batch_leaves_every_other_record_on_its_own_id():
    """The case a positional implementation fails: game 2 fails, so record k of
    the results is NOT game k of the input list from there on."""
    pool = await get_pool()
    try:
        league_id = await _seed_league_with_schedule(pool, name="Results Failure", rng_seed=13)
        try:
            scheduled = await _first_games(pool, league_id, _THROUGH)
            doomed = scheduled[1]
            # A team with no strategy row fails deterministically: "missing
            # team/strategy snapshot data", recorded, the batch continues.
            await pool.execute(
                "DELETE FROM team_strategy WHERE team_id = $1 AND season = $2",
                doomed.home_team_id,
                _SEASON,
            )
            failing = {
                g.id
                for g in scheduled
                if doomed.home_team_id in (g.home_team_id, g.away_team_id)
            }
            expected_ok = [g.id for g in scheduled if g.id not in failing]
            assert doomed.id in failing
            assert any(gid > doomed.id for gid in expected_ok), "no game after the failure"

            batch = await game_sim_service.sim_scheduled_games(
                pool, league_id, _SEASON, through_game_index=_THROUGH, return_results=True
            )

            assert set(batch.failed_game_ids) == failing
            assert batch.games_simmed == len(expected_ok)
            assert len(batch.results) == batch.games_simmed
            assert [r.game_id for r in batch.results] == expected_ok
            for record in batch.results:
                await _assert_record_matches_its_row(pool, record)
            for failed_id in failing:
                row = await game_repo.get_game(pool, failed_id)
                assert row.status is GameStatus.SCHEDULED
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_the_batch_yields_to_the_event_loop_between_games_and_never_inside_a_transaction():
    """P5-99.j: `await asyncio.sleep(0)` between games, so a block press does not
    hold the loop for its whole run -- and only there. Yielding inside a
    per-game transaction would hold a connection while other coroutines run."""
    pool = await get_pool()
    try:
        league_id = await _seed_league_with_schedule(pool, name="Results Yield", rng_seed=14)
        try:
            game_count = len(await _first_games(pool, league_id, _THROUGH))
            in_transaction = False
            yields: list[bool] = []
            real_transaction = game_sim_service.transaction
            real_sleep = asyncio.sleep

            @asynccontextmanager
            async def tracked_transaction(ex):
                nonlocal in_transaction
                async with real_transaction(ex) as conn:
                    in_transaction = True
                    try:
                        yield conn
                    finally:
                        in_transaction = False

            async def tracked_sleep(delay, *args, **kwargs):
                if delay == 0:
                    yields.append(in_transaction)
                return await real_sleep(delay, *args, **kwargs)

            with (
                patch.object(game_sim_service, "transaction", tracked_transaction),
                patch.object(asyncio, "sleep", tracked_sleep),
            ):
                batch = await game_sim_service.sim_scheduled_games(
                    pool, league_id, _SEASON, through_game_index=_THROUGH
                )

            assert batch.games_simmed == game_count > 1
            assert yields == [False] * (game_count - 1)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
