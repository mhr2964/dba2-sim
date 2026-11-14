"""Live I/O proof for `data/repositories/records_repo.py` — this module owns
no table (§5: "reads stats tables, owns no writes to them"), so every test
here seeds `player_season_stats` via `stats_repo.upsert_player_season_stats`
and exercises `records_repo`'s one read query, the leaderboard
(`list_top_player_season_stat_for_league`). A player's full season-stats
log is `stats_repo.list_player_season_stats_for_player`'s job, not this
module's — see `test_stats_repo.py` for that coverage (Phase 0 step-7
critic round, B2: this file used to duplicate that query under a
`records_repo`-local name; deleted along with the duplicate function).

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
from dba2.data.repositories import league_repo, player_repo, records_repo, stats_repo
from dba2.data.repositories.stats_repo import _BOX_SCORE_COLUMN_NAMES
from dba2.domain.game import GameType
from dba2.domain.player import Position, RosterStatus


def _distinct_box(offset: int = 0) -> dict[str, int]:
    return {name: i + offset for i, name in enumerate(_BOX_SCORE_COLUMN_NAMES)}


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Records Repo Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    return league.id


async def _make_player(pool, league_id: int, *, last_name: str) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Records",
        last_name=last_name,
        primary_position=Position.SF,
        height_in=79,
        weight_lb=215,
        wingspan_in=82,
        birth_date=date(1998, 8, 8),
        roster_status=RosterStatus.ACTIVE,
        overall_cached=80,
        overall_computed_at=datetime.now(UTC),
    )
    return player.id


@pytest.mark.asyncio
async def test_list_top_player_season_stat_for_league_orders_and_limits():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            low_scorer = await _make_player(pool, league_id, last_name="Low")
            mid_scorer = await _make_player(pool, league_id, last_name="Mid")
            high_scorer = await _make_player(pool, league_id, last_name="High")

            for player_id, points in ((low_scorer, 500), (mid_scorer, 900), (high_scorer, 1400)):
                box = _distinct_box(offset=0)
                box["points"] = points
                await stats_repo.upsert_player_season_stats(
                    pool,
                    league_id=league_id,
                    player_id=player_id,
                    season=2026,
                    game_type=GameType.REGULAR,
                    games_played=70,
                    minutes_played=Decimal("2000.0"),
                    box=box,
                )

            top_2 = await records_repo.list_top_player_season_stat_for_league(
                pool,
                league_id=league_id,
                season=2026,
                game_type=GameType.REGULAR,
                stat="points",
                limit=2,
            )
            assert [s.player_id for s in top_2] == [high_scorer, mid_scorer]
            assert [s.points for s in top_2] == [1400, 900]

            # A season/game_type combo with no rows is an empty leaderboard,
            # not an error.
            empty = await records_repo.list_top_player_season_stat_for_league(
                pool,
                league_id=league_id,
                season=2099,
                game_type=GameType.REGULAR,
                stat="points",
                limit=10,
            )
            assert empty == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_top_player_season_stat_for_league_rejects_invalid_stat():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            with pytest.raises(ValidationError):
                await records_repo.list_top_player_season_stat_for_league(
                    pool,
                    league_id=league_id,
                    season=2026,
                    game_type=GameType.REGULAR,
                    # Not a real column — must never reach interpolated SQL.
                    stat="id; DROP TABLE player_season_stats;--",
                    limit=10,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


