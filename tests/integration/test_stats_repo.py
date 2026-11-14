"""Live I/O proof for `data/repositories/stats_repo.py` — smoke coverage for
player_game_stats, team_game_stats (per-game inserts), and
player_season_stats (the wholesale-upsert materialized aggregate) (§4.7).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from dba2.core.errors import ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, league_repo, player_repo, stats_repo, team_repo
from dba2.data.repositories.stats_repo import _BOX_SCORE_COLUMN_NAMES
from dba2.domain.game import GameStatus, GameType
from dba2.domain.player import Position, RosterStatus
from dba2.domain.stats import LeaderMode, LeaderStat


def _flat_box(value: int) -> dict[str, int]:
    return {name: value for name in _BOX_SCORE_COLUMN_NAMES}


def _distinct_box(offset: int = 0) -> dict[str, int]:
    """Every box column gets a different value so a mapper that swapped two
    fields — invisible under `_flat_box`, where every field holds the same
    value — would produce a detectable mismatch. `offset` shifts the whole
    set so two calls (e.g. insert vs. overwrite) don't coincidentally reuse
    the same per-field values (mirrors `test_ratings_repo._distinct_ratings`
    and the Phase 0 step-7 critic round B2 lesson it documents)."""
    return {name: i + offset for i, name in enumerate(_BOX_SCORE_COLUMN_NAMES)}


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Stats Repo Test League",
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


async def _make_player(pool, league_id: int, team_id: int | None = None) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Stat",
        last_name="Line",
        primary_position=Position.SG,
        height_in=77,
        weight_lb=200,
        wingspan_in=80,
        birth_date=date(1999, 5, 5),
        roster_status=RosterStatus.ACTIVE,
        overall_cached=75,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    return player.id


async def _make_game(
    pool, league_id: int, home_team_id: int, away_team_id: int,
    *, season: int = 2026, game_type: GameType = GameType.REGULAR,
    status: GameStatus = GameStatus.SCHEDULED,
) -> int:
    game = await game_repo.create_game(
        pool,
        league_id=league_id,
        season=season,
        game_index=random.randint(1, 1_000_000),
        game_date=date(2026, 11, 10),
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        game_type=game_type,
        rng_seed=42,
        status=status,
    )
    return game.id


@pytest.mark.asyncio
async def test_create_player_game_stats_rejects_incomplete_box_dict():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "PGA")
            player_id = await _make_player(pool, league_id, team_id)
            other_team_id = await _make_team(pool, league_id, "PGB")
            game_id = await _make_game(pool, league_id, team_id, other_team_id)

            incomplete = _flat_box(5)
            del incomplete["points"]
            with pytest.raises(ValidationError):
                await stats_repo.create_player_game_stats(
                    pool,
                    game_id=game_id,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_id,
                    minutes_played=Decimal("30.5"),
                    plus_minus=4,
                    box=incomplete,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_get_list_player_game_stats_round_trips_every_field_distinctly():
    """Every box column plus `minutes_played`/`plus_minus` gets a distinct
    value so a mapper that swapped two same-typed fields (e.g. `oreb`/
    `dreb`) would fail this test (Phase 0 step-7 critic round, B2 lesson).
    `league_id` is asserted too — a critic round found 3 mutations swapping
    it into a same-typed ID field (`game_id`/`player_id`/`team_id`) survived
    this suite because none of the ID-block fields were asserted, only the
    box columns and a couple of distinguishing scalars (Phase 0 step-7
    critic round, batch 3, B1)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            home_team_id = await _make_team(pool, league_id, "PHA")
            away_team_id = await _make_team(pool, league_id, "PHB")
            player_id = await _make_player(pool, league_id, home_team_id)
            game_id = await _make_game(pool, league_id, home_team_id, away_team_id)

            box = _distinct_box(offset=0)
            created = await stats_repo.create_player_game_stats(
                pool,
                game_id=game_id,
                league_id=league_id,
                player_id=player_id,
                team_id=home_team_id,
                minutes_played=Decimal("34.5"),
                plus_minus=-7,
                box=box,
            )
            assert created.game_id == game_id
            assert created.league_id == league_id
            assert created.player_id == player_id
            assert created.team_id == home_team_id
            assert created.minutes_played == Decimal("34.5")
            assert created.plus_minus == -7
            for name, value in box.items():
                assert getattr(created, name) == value, name

            fetched = await stats_repo.get_player_game_stats(pool, game_id, player_id)
            assert fetched is not None
            assert fetched.league_id == league_id
            assert fetched.team_id == home_team_id
            for name, value in box.items():
                assert getattr(fetched, name) == value, name

            for_game = await stats_repo.list_player_game_stats_for_game(pool, game_id)
            assert [s.player_id for s in for_game] == [player_id]

            for_player = await stats_repo.list_player_game_stats_for_player(
                pool, player_id, league_id=league_id
            )
            assert [s.game_id for s in for_player] == [game_id]

            assert await stats_repo.get_player_game_stats(pool, game_id, -1) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_get_list_team_game_stats_round_trips_every_field_distinctly():
    """`league_id` is asserted alongside the box columns — see the player
    game-stats test's docstring for why (Phase 0 step-7 critic round,
    batch 3, B1)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            home_team_id = await _make_team(pool, league_id, "TGA")
            away_team_id = await _make_team(pool, league_id, "TGB")
            game_id = await _make_game(pool, league_id, home_team_id, away_team_id)

            box = _distinct_box(offset=10)
            created = await stats_repo.create_team_game_stats(
                pool,
                game_id=game_id,
                league_id=league_id,
                team_id=home_team_id,
                possessions=98,
                box=box,
            )
            assert created.game_id == game_id
            assert created.league_id == league_id
            assert created.team_id == home_team_id
            assert created.possessions == 98
            for name, value in box.items():
                assert getattr(created, name) == value, name

            fetched = await stats_repo.get_team_game_stats(pool, game_id, home_team_id)
            assert fetched is not None
            assert fetched.league_id == league_id
            for name, value in box.items():
                assert getattr(fetched, name) == value, name

            for_game = await stats_repo.list_team_game_stats_for_game(pool, game_id)
            assert [s.team_id for s in for_game] == [home_team_id]

            for_team = await stats_repo.list_team_game_stats_for_team(pool, home_team_id)
            assert [s.game_id for s in for_team] == [game_id]

            assert await stats_repo.get_team_game_stats(pool, game_id, -1) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_upsert_player_season_stats_round_trips_and_overwrites():
    """`league_id`/`player_id` are asserted alongside the box columns — see
    the player game-stats test's docstring for why (Phase 0 step-7 critic
    round, batch 3, B1)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)

            initial_box = _distinct_box(offset=0)
            created = await stats_repo.upsert_player_season_stats(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2026,
                game_type=GameType.REGULAR,
                games_played=10,
                minutes_played=Decimal("320.5"),
                box=initial_box,
            )
            assert created.league_id == league_id
            assert created.player_id == player_id
            assert created.games_played == 10
            assert created.minutes_played == Decimal("320.5")
            assert created.game_type == GameType.REGULAR
            for name, value in initial_box.items():
                assert getattr(created, name) == value, name

            fetched = await stats_repo.get_player_season_stats(
                pool, league_id, player_id, 2026, GameType.REGULAR
            )
            assert fetched is not None
            assert fetched.league_id == league_id
            assert fetched.games_played == 10
            for name, value in initial_box.items():
                assert getattr(fetched, name) == value, name

            overwrite_box = _distinct_box(offset=50)
            updated = await stats_repo.upsert_player_season_stats(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2026,
                game_type=GameType.REGULAR,
                games_played=20,
                minutes_played=Decimal("640.0"),
                box=overwrite_box,
            )
            assert updated.games_played == 20
            assert updated.minutes_played == Decimal("640.0")
            for name, value in overwrite_box.items():
                assert getattr(updated, name) == value, name

            # A distinct game_type is a distinct row, not an overwrite of
            # the regular-season one.
            playoff_box = _distinct_box(offset=80)
            await stats_repo.upsert_player_season_stats(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2026,
                game_type=GameType.PLAYOFF,
                games_played=4,
                minutes_played=Decimal("140.0"),
                box=playoff_box,
            )

            all_seasons = await stats_repo.list_player_season_stats_for_player(
                pool, league_id, player_id
            )
            assert [(s.season, s.game_type) for s in all_seasons] == [
                (2026, GameType.PLAYOFF),
                (2026, GameType.REGULAR),
            ]

            assert (
                await stats_repo.get_player_season_stats(
                    pool, league_id, player_id, 2099, GameType.REGULAR
                )
                is None
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_player_game_stats_many_bulk_inserts_and_validates():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            home_team_id = await _make_team(pool, league_id, "BPA")
            away_team_id = await _make_team(pool, league_id, "BPB")
            player_1 = await _make_player(pool, league_id, home_team_id)
            player_2 = await _make_player(pool, league_id, home_team_id)
            game_id = await _make_game(pool, league_id, home_team_id, away_team_id)

            rows_in = [
                {
                    "game_id": game_id,
                    "league_id": league_id,
                    "player_id": player_1,
                    "team_id": home_team_id,
                    "minutes_played": Decimal("28.5"),
                    "plus_minus": 5,
                    "box": _distinct_box(offset=0),
                },
                {
                    "game_id": game_id,
                    "league_id": league_id,
                    "player_id": player_2,
                    "team_id": home_team_id,
                    "minutes_played": Decimal("22.0"),
                    "plus_minus": -3,
                    "box": _distinct_box(offset=20),
                },
            ]
            created = await stats_repo.create_player_game_stats_many(pool, rows_in)
            assert {s.player_id for s in created} == {player_1, player_2}
            by_player = {s.player_id: s for s in created}
            assert by_player[player_1].minutes_played == Decimal("28.5")
            assert by_player[player_1].plus_minus == 5
            assert by_player[player_2].minutes_played == Decimal("22.0")
            assert by_player[player_2].plus_minus == -3
            for name, value in rows_in[0]["box"].items():
                assert getattr(by_player[player_1], name) == value, name
            for name, value in rows_in[1]["box"].items():
                assert getattr(by_player[player_2], name) == value, name

            assert await stats_repo.create_player_game_stats_many(pool, []) == []

            incomplete = dict(rows_in[0])
            incomplete["box"] = dict(incomplete["box"])
            del incomplete["box"]["points"]
            with pytest.raises(ValidationError):
                await stats_repo.create_player_game_stats_many(pool, [incomplete])
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_team_game_stats_many_bulk_inserts():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            home_team_id = await _make_team(pool, league_id, "BTA")
            away_team_id = await _make_team(pool, league_id, "BTB")
            game_id = await _make_game(pool, league_id, home_team_id, away_team_id)

            rows_in = [
                {
                    "game_id": game_id,
                    "league_id": league_id,
                    "team_id": home_team_id,
                    "possessions": 97,
                    "box": _distinct_box(offset=0),
                },
                {
                    "game_id": game_id,
                    "league_id": league_id,
                    "team_id": away_team_id,
                    "possessions": 96,
                    "box": _distinct_box(offset=20),
                },
            ]
            created = await stats_repo.create_team_game_stats_many(pool, rows_in)
            assert {s.team_id for s in created} == {home_team_id, away_team_id}
            by_team = {s.team_id: s for s in created}
            assert by_team[home_team_id].possessions == 97
            assert by_team[away_team_id].possessions == 96
            for name, value in rows_in[0]["box"].items():
                assert getattr(by_team[home_team_id], name) == value, name
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_delete_stats_for_game_removes_player_and_team_rows_only():
    """Deletes `player_game_stats`/`team_game_stats` for the game but leaves
    `player_season_stats` untouched — the materialized aggregate is rebuilt
    wholesale elsewhere, not accumulated per game (Decision P1-25)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            home_team_id = await _make_team(pool, league_id, "DSA")
            away_team_id = await _make_team(pool, league_id, "DSB")
            player_id = await _make_player(pool, league_id, home_team_id)
            game_id = await _make_game(pool, league_id, home_team_id, away_team_id)

            await stats_repo.create_player_game_stats(
                pool,
                game_id=game_id,
                league_id=league_id,
                player_id=player_id,
                team_id=home_team_id,
                minutes_played=Decimal("30.0"),
                plus_minus=2,
                box=_distinct_box(offset=0),
            )
            await stats_repo.create_team_game_stats(
                pool,
                game_id=game_id,
                league_id=league_id,
                team_id=home_team_id,
                possessions=95,
                box=_distinct_box(offset=0),
            )
            await stats_repo.upsert_player_season_stats(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2026,
                game_type=GameType.REGULAR,
                games_played=1,
                minutes_played=Decimal("30.0"),
                box=_distinct_box(offset=0),
            )

            await stats_repo.delete_stats_for_game(pool, game_id)

            assert await stats_repo.get_player_game_stats(pool, game_id, player_id) is None
            assert await stats_repo.get_team_game_stats(pool, game_id, home_team_id) is None
            # player_season_stats survives — a different table with a
            # different write pattern (wholesale upsert, not per-game).
            season_stats = await stats_repo.get_player_season_stats(
                pool, league_id, player_id, 2026, GameType.REGULAR
            )
            assert season_stats is not None

            # Re-insert after delete must not raise a UniqueViolationError
            # (Decision P1-25's whole point).
            reinserted = await stats_repo.create_player_game_stats(
                pool,
                game_id=game_id,
                league_id=league_id,
                player_id=player_id,
                team_id=home_team_id,
                minutes_played=Decimal("31.0"),
                plus_minus=9,
                box=_distinct_box(offset=5),
            )
            assert reinserted.minutes_played == Decimal("31.0")
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_delete_stats_for_game_clears_stale_period_scores_on_resim():
    """Critic round 11 B2: a re-sim into a shorter game left stale
    `game_period_scores` rows behind, because `upsert_game_period_scores`'s
    `ON CONFLICT DO UPDATE` only overwrites the periods the new sim
    actually produces. Sim 1 goes to one OT (5 periods, home 110 total);
    the re-sim ends in regulation (4 periods, home 80 total), with
    `delete_stats_for_game` called in between exactly as Decision P1-25
    prescribes. After the re-sim, `list_game_period_scores` must return
    only the 4 new periods, summing to the new final score of 80 — not the
    old OT period lingering on top of it."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            home_team_id = await _make_team(pool, league_id, "RSA")
            away_team_id = await _make_team(pool, league_id, "RSB")
            game_id = await _make_game(pool, league_id, home_team_id, away_team_id)

            sim_1_periods = [
                {
                    "game_id": game_id,
                    "league_id": league_id,
                    "team_id": home_team_id,
                    "period": period,
                    "points": points,
                }
                for period, points in enumerate([20, 20, 20, 20, 30], start=1)
            ]
            await stats_repo.upsert_game_period_scores(pool, sim_1_periods)
            after_sim_1 = await stats_repo.list_game_period_scores(pool, game_id)
            assert sum(p.points for p in after_sim_1) == 110

            await stats_repo.delete_stats_for_game(pool, game_id)

            resim_periods = [
                {
                    "game_id": game_id,
                    "league_id": league_id,
                    "team_id": home_team_id,
                    "period": period,
                    "points": points,
                }
                for period, points in enumerate([20, 20, 20, 20], start=1)
            ]
            await stats_repo.upsert_game_period_scores(pool, resim_periods)

            after_resim = await stats_repo.list_game_period_scores(pool, game_id)
            assert [(p.period, p.points) for p in after_resim] == [
                (1, 20),
                (2, 20),
                (3, 20),
                (4, 20),
            ]
            assert sum(p.points for p in after_resim) == 80
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_upsert_and_list_game_period_scores():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            home_team_id = await _make_team(pool, league_id, "PSA")
            away_team_id = await _make_team(pool, league_id, "PSB")
            game_id = await _make_game(pool, league_id, home_team_id, away_team_id)

            initial = [
                {
                    "game_id": game_id,
                    "league_id": league_id,
                    "team_id": home_team_id,
                    "period": 1,
                    "points": 28,
                },
                {
                    "game_id": game_id,
                    "league_id": league_id,
                    "team_id": away_team_id,
                    "period": 1,
                    "points": 24,
                },
            ]
            created = await stats_repo.upsert_game_period_scores(pool, initial)
            assert {(p.team_id, p.period): p.points for p in created} == {
                (home_team_id, 1): 28,
                (away_team_id, 1): 24,
            }

            assert await stats_repo.upsert_game_period_scores(pool, []) == []

            # A re-sim overwrites the same (game_id, team_id, period) slot
            # in place rather than duplicating or erroring.
            overwrite = [
                {
                    "game_id": game_id,
                    "league_id": league_id,
                    "team_id": home_team_id,
                    "period": 1,
                    "points": 30,
                },
            ]
            await stats_repo.upsert_game_period_scores(pool, overwrite)

            listed = await stats_repo.list_game_period_scores(pool, game_id)
            assert {(p.team_id, p.period): p.points for p in listed} == {
                (home_team_id, 1): 30,
                (away_team_id, 1): 24,
            }
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_get_career_scoring_to_date_for_league_partitions_and_scopes_correctly():
    """P4-122 obligation 3 -- the first bulk `player_season_stats`-for-a-
    league reader proven against a real database, real rows across at
    least three seasons and both `GameType`s: `game_type` filtering, the
    `season < N` / `season == N` partition, league scoping, the `int` cast
    at the `SUM` boundary, and absence (not a zero row) for a player with
    no in-scope rows at all."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        other_league_id = await _make_league(pool)
        try:
            # P1: a real career (2024) plus this season's own REGULAR and
            # PLAYOFF rows -- PLAYOFF must never leak into the REGULAR read.
            p1 = await _make_player(pool, league_id)
            await stats_repo.upsert_player_season_stats(
                pool,
                league_id=league_id,
                player_id=p1,
                season=2024,
                game_type=GameType.REGULAR,
                games_played=40,
                minutes_played=Decimal("1200.0"),
                box=_flat_box(1000),
            )
            await stats_repo.upsert_player_season_stats(
                pool,
                league_id=league_id,
                player_id=p1,
                season=2025,
                game_type=GameType.REGULAR,
                games_played=50,
                minutes_played=Decimal("1500.0"),
                box=_flat_box(1200),
            )
            await stats_repo.upsert_player_season_stats(
                pool,
                league_id=league_id,
                player_id=p1,
                season=2025,
                game_type=GameType.PLAYOFF,
                games_played=10,
                minutes_played=Decimal("300.0"),
                box=_flat_box(300),
            )

            # P2: a genuine new entrant -- no row before 2025 at all, so
            # `career_points_before_season` must read `0` via COALESCE,
            # never a missing-key error.
            p2 = await _make_player(pool, league_id)
            await stats_repo.upsert_player_season_stats(
                pool,
                league_id=league_id,
                player_id=p2,
                season=2025,
                game_type=GameType.REGULAR,
                games_played=45,
                minutes_played=Decimal("1350.0"),
                box=_flat_box(800),
            )

            # P3: retired before 2025 -- a real career total, no row in the
            # target season at all, so the in-season half reads `0`/`0`
            # via COALESCE rather than the player being dropped.
            p3 = await _make_player(pool, league_id)
            await stats_repo.upsert_player_season_stats(
                pool,
                league_id=league_id,
                player_id=p3,
                season=2023,
                game_type=GameType.REGULAR,
                games_played=30,
                minutes_played=Decimal("900.0"),
                box=_flat_box(500),
            )

            # P4: same shape as P1, but in a DIFFERENT league -- must never
            # appear in league_id's own results (league scoping).
            p4 = await _make_player(pool, other_league_id)
            await stats_repo.upsert_player_season_stats(
                pool,
                league_id=other_league_id,
                player_id=p4,
                season=2025,
                game_type=GameType.REGULAR,
                games_played=50,
                minutes_played=Decimal("1500.0"),
                box=_flat_box(2000),
            )

            # P5: only a row in a season AFTER the target season -- must be
            # absent from the returned dict entirely, not present with
            # zeros (this repository's own "absent, not zero" contract).
            p5 = await _make_player(pool, league_id)
            await stats_repo.upsert_player_season_stats(
                pool,
                league_id=league_id,
                player_id=p5,
                season=2026,
                game_type=GameType.REGULAR,
                games_played=20,
                minutes_played=Decimal("600.0"),
                box=_flat_box(400),
            )

            result = await stats_repo.get_career_scoring_to_date_for_league(
                pool, league_id, season=2025, game_type=GameType.REGULAR
            )

            assert result[p1].career_points_before_season == 1000
            assert result[p1].points_in_season == 1200  # PLAYOFF's 300 excluded
            assert result[p1].games_played_in_season == 50

            assert result[p2].career_points_before_season == 0
            assert result[p2].points_in_season == 800
            assert result[p2].games_played_in_season == 45

            assert result[p3].career_points_before_season == 500
            assert result[p3].points_in_season == 0
            assert result[p3].games_played_in_season == 0

            assert p4 not in result
            assert p5 not in result

            for row in result.values():
                assert isinstance(row.career_points_before_season, int)
                assert isinstance(row.points_in_season, int)
                assert isinstance(row.games_played_in_season, int)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", other_league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_player_season_stat_leaders_orders_by_the_requested_stat_and_scopes_correctly():
    """Phase 5 Stage B step 8's `/leaders` read (docs/design/phase-5-
    discord-presentation.md §7.1) -- proves the real SQL expression map
    (`rebounds` sums `oreb + dreb`, matching `_BoxScoreColumns`'s own "no
    separate total" convention) and the `league_id`/`season`/`game_type`
    scoping, not just that SOME ordering happens."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        other_league_id = await _make_league(pool)
        try:
            p_high = await _make_player(pool, league_id)
            p_mid = await _make_player(pool, league_id)
            p_low = await _make_player(pool, league_id)
            p_wrong_season = await _make_player(pool, league_id)
            p_playoff = await _make_player(pool, league_id)
            p_other_league = await _make_player(pool, other_league_id)

            async def _upsert(player_id, *, league, season, game_type, points, oreb, dreb, games):
                box = dict(_flat_box(0), points=points, oreb=oreb, dreb=dreb)
                await stats_repo.upsert_player_season_stats(
                    pool, league_id=league, player_id=player_id, season=season,
                    game_type=game_type, games_played=games, minutes_played=Decimal("500"),
                    box=box,
                )

            await _upsert(p_high, league=league_id, season=2026, game_type=GameType.REGULAR,
                           points=30, oreb=2, dreb=3, games=10)
            await _upsert(p_mid, league=league_id, season=2026, game_type=GameType.REGULAR,
                           points=20, oreb=5, dreb=10, games=10)
            await _upsert(p_low, league=league_id, season=2026, game_type=GameType.REGULAR,
                           points=10, oreb=1, dreb=1, games=10)
            await _upsert(p_wrong_season, league=league_id, season=2025, game_type=GameType.REGULAR,
                           points=99, oreb=0, dreb=0, games=10)
            await _upsert(p_playoff, league=league_id, season=2026, game_type=GameType.PLAYOFF,
                           points=99, oreb=0, dreb=0, games=10)
            await _upsert(p_other_league, league=other_league_id, season=2026,
                           game_type=GameType.REGULAR, points=99, oreb=0, dreb=0, games=10)

            points_leaders = await stats_repo.list_player_season_stat_leaders(
                pool, league_id=league_id, season=2026, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, limit=10,
            )
            assert [row.player_id for row in points_leaders] == [p_high, p_mid, p_low]
            assert [row.stat_total for row in points_leaders] == [30, 20, 10]

            rebound_leaders = await stats_repo.list_player_season_stat_leaders(
                pool, league_id=league_id, season=2026, game_type=GameType.REGULAR,
                stat=LeaderStat.REBOUNDS, limit=10,
            )
            assert rebound_leaders[0].player_id == p_mid  # 5 + 10 = 15, highest total
            assert rebound_leaders[0].stat_total == 15

            limited = await stats_repo.list_player_season_stat_leaders(
                pool, league_id=league_id, season=2026, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, limit=1,
            )
            assert [row.player_id for row in limited] == [p_high]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", other_league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_player_season_stat_leaders_rejects_unknown_stat():
    pool = await get_pool()
    try:
        with pytest.raises(ValidationError, match="Unknown LeaderStat"):
            await stats_repo.list_player_season_stat_leaders(
                pool, league_id=1, season=2026, game_type=GameType.REGULAR,
                stat="not_a_real_stat", limit=10,
            )
    finally:
        await close_pool()


async def _seed_final_game_stat(
    pool, *, game_id: int, league_id: int, player_id: int, team_id: int, box: dict[str, int],
) -> None:
    await stats_repo.create_player_game_stats(
        pool, game_id=game_id, league_id=league_id, player_id=player_id, team_id=team_id,
        minutes_played=Decimal("30.0"), plus_minus=0, box=box,
    )


@pytest.mark.asyncio
async def test_list_live_stat_leaders_orders_by_the_requested_stat_and_scopes_correctly():
    """docs/design/leaders-live.md LD-1: the new live read's own proof,
    mirroring `test_list_player_season_stat_leaders_orders_by_the_
    requested_stat_and_scopes_correctly` above, but seeded through `games`
    + `player_game_stats` -- `list_live_stat_leaders`'s real source tables
    -- with an extra in-progress-game case that sibling test has no
    equivalent for, since `player_season_stats` carries no per-game
    `status` to filter on. Every helper call below spells out its own
    `league_id`/`game_id`/`team_id` explicitly rather than deriving one
    from another, so each seeded row's scope is visible at its own call
    site."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        other_league_id = await _make_league(pool)
        try:
            home_team_id = await _make_team(pool, league_id, "LVA")
            away_team_id = await _make_team(pool, league_id, "LVB")
            other_team_id = await _make_team(pool, other_league_id, "LVC")
            other_team_id_2 = await _make_team(pool, other_league_id, "LVD")

            p_high = await _make_player(pool, league_id, home_team_id)
            p_mid = await _make_player(pool, league_id, away_team_id)
            p_low = await _make_player(pool, league_id, home_team_id)
            p_wrong_season = await _make_player(pool, league_id, away_team_id)
            p_playoff = await _make_player(pool, league_id, home_team_id)
            p_in_progress = await _make_player(pool, league_id, away_team_id)
            p_other_league = await _make_player(pool, other_league_id, other_team_id)

            game_regular = await _make_game(
                pool, league_id, home_team_id, away_team_id, status=GameStatus.FINAL,
            )
            game_wrong_season = await _make_game(
                pool, league_id, home_team_id, away_team_id,
                season=2025, status=GameStatus.FINAL,
            )
            game_playoff = await _make_game(
                pool, league_id, home_team_id, away_team_id,
                game_type=GameType.PLAYOFF, status=GameStatus.FINAL,
            )
            game_in_progress = await _make_game(
                pool, league_id, home_team_id, away_team_id, status=GameStatus.IN_PROGRESS,
            )
            game_other_league = await _make_game(
                pool, other_league_id, other_team_id, other_team_id_2, status=GameStatus.FINAL,
            )

            await _seed_final_game_stat(
                pool, game_id=game_regular, league_id=league_id, player_id=p_high,
                team_id=home_team_id, box=dict(_flat_box(0), points=30, oreb=2, dreb=3),
            )
            await _seed_final_game_stat(
                pool, game_id=game_regular, league_id=league_id, player_id=p_mid,
                team_id=away_team_id, box=dict(_flat_box(0), points=20, oreb=5, dreb=10),
            )
            await _seed_final_game_stat(
                pool, game_id=game_regular, league_id=league_id, player_id=p_low,
                team_id=home_team_id, box=dict(_flat_box(0), points=10, oreb=1, dreb=1),
            )
            await _seed_final_game_stat(
                pool, game_id=game_wrong_season, league_id=league_id, player_id=p_wrong_season,
                team_id=away_team_id, box=dict(_flat_box(0), points=99),
            )
            await _seed_final_game_stat(
                pool, game_id=game_playoff, league_id=league_id, player_id=p_playoff,
                team_id=home_team_id, box=dict(_flat_box(0), points=99),
            )
            await _seed_final_game_stat(
                pool, game_id=game_in_progress, league_id=league_id, player_id=p_in_progress,
                team_id=away_team_id, box=dict(_flat_box(0), points=99),
            )
            await _seed_final_game_stat(
                pool, game_id=game_other_league, league_id=other_league_id,
                player_id=p_other_league, team_id=other_team_id, box=dict(_flat_box(0), points=99),
            )

            points_leaders = (await stats_repo.list_live_stat_leaders(
                pool, league_id=league_id, season=2026, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.TOTAL, apply_bar=False,
                page=1, page_size=10,
            )).rows
            assert [row.player_id for row in points_leaders] == [p_high, p_mid, p_low]
            assert [row.stat_total for row in points_leaders] == [30, 20, 10]
            assert all(row.games_played == 1 for row in points_leaders)

            rebound_leaders = (await stats_repo.list_live_stat_leaders(
                pool, league_id=league_id, season=2026, game_type=GameType.REGULAR,
                stat=LeaderStat.REBOUNDS, mode=LeaderMode.TOTAL, apply_bar=False,
                page=1, page_size=10,
            )).rows
            assert rebound_leaders[0].player_id == p_mid  # 5 + 10 = 15, highest total
            assert rebound_leaders[0].stat_total == 15

            limited = (await stats_repo.list_live_stat_leaders(
                pool, league_id=league_id, season=2026, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.TOTAL, apply_bar=False,
                page=1, page_size=1,
            )).rows
            assert [row.player_id for row in limited] == [p_high]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", other_league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_live_stat_leaders_breaks_ties_on_player_id_ascending():
    """Same tie-break guarantee as `list_player_season_stat_leaders` --
    two equal totals order deterministically rather than depending on
    Postgres's own unspecified tie order."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            home_team_id = await _make_team(pool, league_id, "TIA")
            away_team_id = await _make_team(pool, league_id, "TIB")
            p_lower_id = await _make_player(pool, league_id, home_team_id)
            p_higher_id = await _make_player(pool, league_id, away_team_id)
            game_id = await _make_game(
                pool, league_id, home_team_id, away_team_id, status=GameStatus.FINAL,
            )
            first, second = sorted([p_lower_id, p_higher_id])
            await _seed_final_game_stat(
                pool, game_id=game_id, league_id=league_id, player_id=first,
                team_id=home_team_id, box=dict(_flat_box(0), points=15),
            )
            await _seed_final_game_stat(
                pool, game_id=game_id, league_id=league_id, player_id=second,
                team_id=away_team_id, box=dict(_flat_box(0), points=15),
            )

            leaders = (await stats_repo.list_live_stat_leaders(
                pool, league_id=league_id, season=2026, game_type=GameType.REGULAR,
                stat=LeaderStat.POINTS, mode=LeaderMode.TOTAL, apply_bar=False,
                page=1, page_size=10,
            )).rows
            assert [row.player_id for row in leaders] == [first, second]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_live_stat_leaders_rejects_unknown_stat():
    pool = await get_pool()
    try:
        with pytest.raises(ValidationError, match="Unknown LeaderStat"):
            await stats_repo.list_live_stat_leaders(
                pool, league_id=1, season=2026, game_type=GameType.REGULAR,
                stat="not_a_real_stat", mode=LeaderMode.TOTAL, apply_bar=False,
                page=1, page_size=10,
            )
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# docs/design/media-v2-newsroom.md MV-2c/MV-2g -- the media scanner's own
# live-window and style reads (step 2).
# ---------------------------------------------------------------------------


async def _make_dated_game(
    pool, league_id: int, home_team_id: int, away_team_id: int, *, game_date, season: int = 2026,
) -> int:
    game = await game_repo.create_game(
        pool,
        league_id=league_id,
        season=season,
        game_index=random.randint(1, 1_000_000),
        game_date=game_date,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        game_type=GameType.REGULAR,
        rng_seed=42,
        status=GameStatus.FINAL,
    )
    return game.id


@pytest.mark.asyncio
async def test_list_recent_form_windows_splits_the_most_recent_n_games_from_the_rest():
    """`window` = 2 over 5 total games: the two most recent (by `game_date`
    DESC) land in the window sums, all five land in the season sums --
    MV-2c's own "window is a strict subset of the season aggregate"
    property, checked directly against real rows rather than assumed."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            home = await _make_team(pool, league_id, "FWA")
            away = await _make_team(pool, league_id, "FWB")
            player_id = await _make_player(pool, league_id, home)

            for day, points in zip(range(1, 6), (10, 10, 10, 40, 50), strict=True):
                game_id = await _make_dated_game(
                    pool, league_id, home, away, game_date=date(2026, 11, day)
                )
                await stats_repo.create_player_game_stats(
                    pool, game_id=game_id, league_id=league_id, player_id=player_id,
                    team_id=home, minutes_played=Decimal("30.0"), plus_minus=0,
                    box=dict(_flat_box(0), points=points),
                )

            windows = await stats_repo.list_recent_form_windows(
                pool, league_id=league_id, season=2026, game_type=GameType.REGULAR, window=2
            )
            assert len(windows) == 1
            window = windows[0]
            assert window.player_id == player_id
            assert window.team_id == home
            assert window.window_games == 2
            assert window.season_games == 5
            # The two most recent games (day 4 and day 5): 40 + 50.
            assert window.window_points == 90.0
            assert isinstance(window.window_points, float)
            # All five games: 10+10+10+40+50.
            assert window.season_points == 120.0
            assert window.season_minutes == 150.0

            # A player with zero final games this league-season is absent,
            # never a fabricated zero row.
            no_games_player = await _make_player(pool, league_id, home)
            still_windows = await stats_repo.list_recent_form_windows(
                pool, league_id=league_id, season=2026, game_type=GameType.REGULAR, window=2
            )
            assert no_games_player not in {w.player_id for w in still_windows}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_recent_form_windows_attributes_team_id_to_the_most_recent_game():
    """A player traded mid-window still aggregates across both team ids
    (MV-2c's own disclosed rule) -- `team_id` on the returned row is the
    team of his MOST RECENT game, not his first."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            old_team = await _make_team(pool, league_id, "FWC")
            new_team = await _make_team(pool, league_id, "FWD")
            opponent = await _make_team(pool, league_id, "FWE")
            player_id = await _make_player(pool, league_id, new_team)

            older_game = await _make_dated_game(
                pool, league_id, old_team, opponent, game_date=date(2026, 11, 1)
            )
            await stats_repo.create_player_game_stats(
                pool, game_id=older_game, league_id=league_id, player_id=player_id,
                team_id=old_team, minutes_played=Decimal("20.0"), plus_minus=0,
                box=dict(_flat_box(0), points=5),
            )
            newer_game = await _make_dated_game(
                pool, league_id, new_team, opponent, game_date=date(2026, 11, 10)
            )
            await stats_repo.create_player_game_stats(
                pool, game_id=newer_game, league_id=league_id, player_id=player_id,
                team_id=new_team, minutes_played=Decimal("25.0"), plus_minus=0,
                box=dict(_flat_box(0), points=15),
            )

            windows = await stats_repo.list_recent_form_windows(
                pool, league_id=league_id, season=2026, game_type=GameType.REGULAR, window=1
            )
            assert len(windows) == 1
            assert windows[0].team_id == new_team
            assert windows[0].season_games == 2
            assert windows[0].season_points == 20.0
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_team_style_aggregates_computes_ratios_over_final_games_only():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "TSA")
            opponent = await _make_team(pool, league_id, "TSB")

            final_game = await _make_dated_game(
                pool, league_id, team_id, opponent, game_date=date(2026, 11, 1)
            )
            await stats_repo.create_team_game_stats(
                pool, game_id=final_game, league_id=league_id, team_id=team_id,
                possessions=100,
                box=dict(
                    _flat_box(0), fga_rim=20, fga_mid=10, fga_three=20, fgm_three=8,
                    fta=15, oreb=10, dreb=30, tov=15,
                ),
            )
            # A SCHEDULED (not final) game's rows must not be counted.
            scheduled_game = await game_repo.create_game(
                pool, league_id=league_id, season=2026,
                game_index=random.randint(1, 1_000_000), game_date=date(2026, 11, 5),
                home_team_id=team_id, away_team_id=opponent, game_type=GameType.REGULAR,
                rng_seed=1, status=GameStatus.SCHEDULED,
            )
            await stats_repo.create_team_game_stats(
                pool, game_id=scheduled_game.id, league_id=league_id, team_id=team_id,
                possessions=999, box=dict(_flat_box(0), points=999),
            )

            aggregates = await stats_repo.list_team_style_aggregates(
                pool, league_id=league_id, season=2026, game_type=GameType.REGULAR
            )
            assert len(aggregates) == 1
            row = aggregates[0]
            assert row.team_id == team_id
            assert row.games == 1
            assert row.avg_possessions == 100.0
            # three_share = 20 / (20 + 10 + 20) = 0.4
            assert row.three_share == pytest.approx(0.4)
            # oreb_rate = 10 / (10 + 30) = 0.25
            assert row.oreb_rate == pytest.approx(0.25)
            # fta_rate = 15 / 50 = 0.3
            assert row.fta_rate == pytest.approx(0.3)
            # tov_per_100 = 15 / 100 * 100 = 15.0
            assert row.tov_per_100 == pytest.approx(15.0)
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_team_style_aggregates_returns_empty_for_a_team_with_no_final_games():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            aggregates = await stats_repo.list_team_style_aggregates(
                pool, league_id=league_id, season=2026, game_type=GameType.REGULAR
            )
            assert aggregates == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
