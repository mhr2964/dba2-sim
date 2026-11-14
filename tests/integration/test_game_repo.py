"""Live I/O proof for `data/repositories/game_repo.py` — smoke coverage for
create/get plus the two schedule-style list queries (§4.7, §5).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from datetime import date

import pytest

from dba2.core.errors import ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, league_repo, team_repo
from dba2.domain.game import GameStatus, GameType


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Game Repo Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    return league.id


async def _make_team(pool, league_id: int, code: str) -> int:
    team = await team_repo.create_team(
        pool,
        league_id=league_id,
        code=code,
        name=f"{code} Team",
        city=f"{code} City",
        conference="West",
        division="Pacific",
        home_market_size=2,
    )
    return team.id


@pytest.mark.asyncio
async def test_create_get_game_and_list_for_league_season():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "GMA")
            team_b = await _make_team(pool, league_id, "GMB")

            # Inserted out of game_index order to prove the list query sorts.
            game_2 = await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=2,
                game_date=date(2026, 11, 2),
                home_team_id=team_a,
                away_team_id=team_b,
                game_type=GameType.REGULAR,
                rng_seed=222,
            )
            game_1 = await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=1,
                game_date=date(2026, 11, 1),
                home_team_id=team_b,
                away_team_id=team_a,
                game_type=GameType.REGULAR,
                rng_seed=111,
            )
            assert game_1.status == GameStatus.SCHEDULED
            assert game_1.home_score is None
            assert game_1.simmed_at is None

            fetched = await game_repo.get_game(pool, game_2.id)
            assert fetched is not None
            assert fetched.game_index == 2

            for_season = await game_repo.list_games_for_league_season(pool, league_id, 2026)
            assert [g.id for g in for_season] == [game_1.id, game_2.id]

            assert await game_repo.list_games_for_league_season(pool, league_id, 2099) == []
            assert await game_repo.get_game(pool, -1) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_map_game_round_trips_every_field_distinctly():
    """Every `Game` field gets a distinct value so a mapper that swapped two
    same-typed fields (e.g. `home_score`/`away_score`, or `home_team_id`/
    `away_team_id`, all `int`) would fail this test."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "DGA")
            team_b = await _make_team(pool, league_id, "DGB")

            created = await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2027,
                game_index=17,
                game_date=date(2027, 3, 15),
                home_team_id=team_a,
                away_team_id=team_b,
                home_score=101,
                away_score=97,
                status=GameStatus.FINAL,
                game_type=GameType.PLAYOFF,
                rng_seed=987654321,
            )
            fetched = await game_repo.get_game(pool, created.id)
            assert fetched.league_id == league_id
            assert fetched.season == 2027
            assert fetched.game_index == 17
            assert fetched.game_date == date(2027, 3, 15)
            assert fetched.home_team_id == team_a
            assert fetched.away_team_id == team_b
            assert fetched.home_score == 101
            assert fetched.away_score == 97
            assert fetched.status == GameStatus.FINAL
            assert fetched.game_type == GameType.PLAYOFF
            assert fetched.rng_seed == 987654321
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_games_for_team_season_includes_home_and_away():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "TSA")
            team_b = await _make_team(pool, league_id, "TSB")
            team_c = await _make_team(pool, league_id, "TSC")

            # Inserted out of date order to prove the list query sorts.
            game_later = await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=5,
                game_date=date(2026, 12, 25),
                home_team_id=team_b,
                away_team_id=team_a,
                game_type=GameType.REGULAR,
                rng_seed=5,
            )
            game_earlier = await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=4,
                game_date=date(2026, 11, 20),
                home_team_id=team_a,
                away_team_id=team_c,
                game_type=GameType.REGULAR,
                rng_seed=4,
            )
            # A game not involving team_a should not appear in its schedule.
            await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=6,
                game_date=date(2026, 12, 1),
                home_team_id=team_b,
                away_team_id=team_c,
                game_type=GameType.REGULAR,
                rng_seed=6,
            )

            schedule = await game_repo.list_games_for_team_season(
                pool, team_a, 2026, league_id=league_id
            )
            assert [g.id for g in schedule] == [game_earlier.id, game_later.id]

            assert (
                await game_repo.list_games_for_team_season(
                    pool, team_a, 2099, league_id=league_id
                )
                == []
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_games_for_team_season_scopes_by_league_id():
    """A wrong `league_id` returns nothing even though `team_id`/`season`
    match — proves the new required predicate is actually applied, not just
    accepted and ignored."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        other_league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "LSA")
            team_b = await _make_team(pool, league_id, "LSB")
            await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=9,
                game_date=date(2026, 11, 9),
                home_team_id=team_a,
                away_team_id=team_b,
                game_type=GameType.REGULAR,
                rng_seed=9,
            )

            assert (
                await game_repo.list_games_for_team_season(
                    pool, team_a, 2026, league_id=other_league_id
                )
                == []
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", other_league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_set_status_guards_on_stale_from_status():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "SSA")
            team_b = await _make_team(pool, league_id, "SSB")
            game = await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=11,
                game_date=date(2026, 11, 11),
                home_team_id=team_a,
                away_team_id=team_b,
                game_type=GameType.REGULAR,
                rng_seed=11,
            )
            assert game.status == GameStatus.SCHEDULED

            await game_repo.set_status(
                pool,
                game.id,
                from_status=GameStatus.SCHEDULED,
                to_status=GameStatus.IN_PROGRESS,
            )
            after_first = await game_repo.get_game(pool, game.id)
            assert after_first.status == GameStatus.IN_PROGRESS

            # Stale from_status (row already moved past SCHEDULED) -> no-op,
            # not an error, matching the terminate_contract/complete_lottery
            # guard pattern.
            await game_repo.set_status(
                pool,
                game.id,
                from_status=GameStatus.SCHEDULED,
                to_status=GameStatus.POSTPONED,
            )
            after_stale = await game_repo.get_game(pool, game.id)
            assert after_stale.status == GameStatus.IN_PROGRESS

            # The re-sim edge (Decision P1-26): FINAL -> IN_PROGRESS is
            # legal at this layer too (this function trusts the caller's
            # legality check, it doesn't re-derive it).
            await game_repo.set_status(
                pool, game.id, from_status=GameStatus.IN_PROGRESS, to_status=GameStatus.FINAL
            )
            await game_repo.set_status(
                pool, game.id, from_status=GameStatus.FINAL, to_status=GameStatus.IN_PROGRESS
            )
            after_resim = await game_repo.get_game(pool, game.id)
            assert after_resim.status == GameStatus.IN_PROGRESS
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_finalize_game_writes_result_and_guards_on_already_final():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "FGA")
            team_b = await _make_team(pool, league_id, "FGB")
            game = await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=13,
                game_date=date(2026, 11, 13),
                home_team_id=team_a,
                away_team_id=team_b,
                game_type=GameType.REGULAR,
                rng_seed=13,
            )
            await game_repo.set_status(
                pool,
                game.id,
                from_status=GameStatus.SCHEDULED,
                to_status=GameStatus.IN_PROGRESS,
            )

            await game_repo.finalize_game(
                pool,
                game.id,
                home_score=101,
                away_score=97,
                overtime_periods=1,
                engine_version=3,
                roster_digest="digest-abc",
            )
            finalized = await game_repo.get_game(pool, game.id)
            assert finalized.status == GameStatus.FINAL
            assert finalized.home_score == 101
            assert finalized.away_score == 97
            assert finalized.overtime_periods == 1
            assert finalized.engine_version == 3
            assert finalized.roster_digest == "digest-abc"
            assert finalized.simmed_at is not None

            # Already-final -> a second call is a no-op, not a silent
            # overwrite, matching the terminate_contract guard pattern.
            await game_repo.finalize_game(
                pool,
                game.id,
                home_score=999,
                away_score=999,
                overtime_periods=2,
                engine_version=99,
                roster_digest="should-not-apply",
            )
            unchanged = await game_repo.get_game(pool, game.id)
            assert unchanged.home_score == 101
            assert unchanged.roster_digest == "digest-abc"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_finalize_game_rejects_illegal_transition_and_allows_legal_one():
    """Critic round 11 B1: `finalize_game` was a second, unchecked writer of
    `games.status` — calling it directly on a freshly created (`scheduled`)
    game silently jumped straight to `final`, a transition
    `entity_states.GAME_TRANSITIONS`/`test_entity_states.py:50` both declare
    illegal. It must now raise instead, and the normal `IN_PROGRESS ->
    FINAL` path (the only legal source status per `GAME_TRANSITIONS`) must
    still work."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "FIA")
            team_b = await _make_team(pool, league_id, "FIB")
            scheduled_game = await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=31,
                game_date=date(2026, 11, 30),
                home_team_id=team_a,
                away_team_id=team_b,
                game_type=GameType.REGULAR,
                rng_seed=31,
            )
            assert scheduled_game.status == GameStatus.SCHEDULED

            with pytest.raises(ValidationError):
                await game_repo.finalize_game(
                    pool,
                    scheduled_game.id,
                    home_score=100,
                    away_score=90,
                    overtime_periods=0,
                    engine_version=1,
                    roster_digest="x",
                )
            still_scheduled = await game_repo.get_game(pool, scheduled_game.id)
            assert still_scheduled.status == GameStatus.SCHEDULED
            assert still_scheduled.home_score is None

            # The normal path: scheduled -> in_progress -> final still works.
            await game_repo.set_status(
                pool,
                scheduled_game.id,
                from_status=GameStatus.SCHEDULED,
                to_status=GameStatus.IN_PROGRESS,
            )
            await game_repo.finalize_game(
                pool,
                scheduled_game.id,
                home_score=100,
                away_score=90,
                overtime_periods=0,
                engine_version=1,
                roster_digest="x",
            )
            finalized = await game_repo.get_game(pool, scheduled_game.id)
            assert finalized.status == GameStatus.FINAL
            assert finalized.home_score == 100
            assert finalized.away_score == 90
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_scheduled_games_filters_status_and_game_type():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "LGA")
            team_b = await _make_team(pool, league_id, "LGB")
            scheduled = await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=21,
                game_date=date(2026, 11, 21),
                home_team_id=team_a,
                away_team_id=team_b,
                game_type=GameType.REGULAR,
                rng_seed=21,
            )
            already_final = await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=22,
                game_date=date(2026, 11, 22),
                home_team_id=team_a,
                away_team_id=team_b,
                status=GameStatus.FINAL,
                game_type=GameType.REGULAR,
                rng_seed=22,
            )
            playoff_scheduled = await game_repo.create_game(
                pool,
                league_id=league_id,
                season=2026,
                game_index=23,
                game_date=date(2026, 11, 23),
                home_team_id=team_a,
                away_team_id=team_b,
                game_type=GameType.PLAYOFF,
                rng_seed=23,
            )

            regular_scheduled = await game_repo.list_scheduled_games(
                pool, league_id, 2026, GameType.REGULAR
            )
            assert [g.id for g in regular_scheduled] == [scheduled.id]
            assert already_final.id not in [g.id for g in regular_scheduled]

            playoff_result = await game_repo.list_scheduled_games(
                pool, league_id, 2026, GameType.PLAYOFF
            )
            assert [g.id for g in playoff_result] == [playoff_scheduled.id]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_games_many_bulk_inserts_and_validates_field_set():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "BGA")
            team_b = await _make_team(pool, league_id, "BGB")

            games_in = [
                {
                    "league_id": league_id,
                    "season": 2026,
                    "game_index": 101,
                    "game_date": date(2026, 12, 1),
                    "home_team_id": team_a,
                    "away_team_id": team_b,
                    "game_type": GameType.REGULAR,
                    "rng_seed": 101,
                },
                {
                    "league_id": league_id,
                    "season": 2026,
                    "game_index": 102,
                    "game_date": date(2026, 12, 2),
                    "home_team_id": team_b,
                    "away_team_id": team_a,
                    "game_type": GameType.REGULAR,
                    "rng_seed": 102,
                },
            ]
            created = await game_repo.create_games_many(pool, games_in)
            assert {g.game_index for g in created} == {101, 102}
            for g in created:
                assert g.status == GameStatus.SCHEDULED
                assert g.home_score is None
                assert g.away_score is None

            assert await game_repo.create_games_many(pool, []) == []

            with pytest.raises(ValidationError):
                incomplete = dict(games_in[0])
                del incomplete["rng_seed"]
                await game_repo.create_games_many(pool, [incomplete])
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_games_many_round_trips_every_field_distinctly():
    """Critic round 11 B3: `create_games_many`'s field round-trip was
    untested — a `home_team_id`/`away_team_id` swap in its `INSERT ...
    SELECT` column list survived the whole suite (critic mutation-verified
    this directly). Every field gets a distinct per-row value, keyed by
    `game_index` since `RETURNING`'s row order is unspecified (nit 3), so a
    column-order mistake in any field is detectable."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "RTA")
            team_b = await _make_team(pool, league_id, "RTB")

            games_in = [
                {
                    "league_id": league_id,
                    "season": 2026,
                    "game_index": 201,
                    "game_date": date(2026, 12, 10),
                    "home_team_id": team_a,
                    "away_team_id": team_b,
                    "game_type": GameType.REGULAR,
                    "rng_seed": 5001,
                },
                {
                    "league_id": league_id,
                    "season": 2026,
                    "game_index": 202,
                    "game_date": date(2026, 12, 11),
                    "home_team_id": team_b,
                    "away_team_id": team_a,
                    "game_type": GameType.PLAYOFF,
                    "rng_seed": 5002,
                },
            ]
            created = await game_repo.create_games_many(pool, games_in)
            by_index = {g.game_index: g for g in created}
            assert set(by_index) == {201, 202}

            for expected in games_in:
                got = by_index[expected["game_index"]]
                assert got.league_id == expected["league_id"]
                assert got.season == expected["season"]
                assert got.game_date == expected["game_date"]
                assert got.home_team_id == expected["home_team_id"], (
                    f"game_index={expected['game_index']}"
                )
                assert got.away_team_id == expected["away_team_id"], (
                    f"game_index={expected['game_index']}"
                )
                assert got.game_type == expected["game_type"]
                assert got.rng_seed == expected["rng_seed"]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
