"""Live I/O proof for `data/repositories/award_repo.py` — smoke coverage
for player_awards, including the migration's UNIQUE
(league_id, player_id, season, award_type) constraint surfacing a plain
`asyncpg.UniqueViolationError` on a duplicate insert (this module does not
catch or upsert around it — see the module docstring).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it —
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime
from decimal import Decimal

import asyncpg
import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import award_repo, league_repo, player_repo
from dba2.domain.award import AwardType
from dba2.domain.player import Position, RosterStatus


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Award Repo Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    return league.id


async def _make_player(pool, league_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Award",
        last_name="Player",
        primary_position=Position.PF,
        height_in=81,
        weight_lb=230,
        wingspan_in=85,
        birth_date=date(1995, 8, 8),
        roster_status=RosterStatus.FREE_AGENT,
        overall_cached=90,
        overall_computed_at=datetime.now(UTC),
    )
    return player.id


@pytest.mark.asyncio
async def test_create_get_award_round_trips_every_field_distinctly():
    """Every field gets a distinct value so a mapper that swapped two
    same-typed fields (e.g. `season`/`place`, both `int`) would fail this
    test (Phase 0 step-7 critic round, B2 lesson)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)

            award = await award_repo.create_award(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2025,
                award_type=AwardType.MVP,
                place=1,
                vote_share=Decimal("0.8734"),
            )
            assert award.league_id == league_id
            assert award.player_id == player_id
            assert award.season == 2025
            assert award.award_type == AwardType.MVP
            assert award.place == 1
            assert award.vote_share == Decimal("0.8734")

            fetched = await award_repo.get_award(pool, award.id)
            assert fetched == award

            assert await award_repo.get_award(pool, -1) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_create_award_with_null_place_and_vote_share():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)
            award = await award_repo.create_award(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2026,
                award_type=AwardType.ALL_STAR,
            )
            assert award.place is None
            assert award.vote_share is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_awards_for_player_filters_by_award_type():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)

            mvp_2023 = await award_repo.create_award(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2023,
                award_type=AwardType.MVP,
            )
            mvp_2025 = await award_repo.create_award(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2025,
                award_type=AwardType.MVP,
            )
            dpoy_2024 = await award_repo.create_award(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2024,
                award_type=AwardType.DPOY,
            )

            all_awards = await award_repo.list_awards_for_player(pool, player_id)
            assert [a.id for a in all_awards] == [mvp_2023.id, dpoy_2024.id, mvp_2025.id]

            mvp_only = await award_repo.list_awards_for_player(
                pool, player_id, award_type=AwardType.MVP
            )
            assert [a.id for a in mvp_only] == [mvp_2023.id, mvp_2025.id]

            # Supermax eligibility's own read shape: MVP in any of the last
            # three seasons is a Python filter over this list.
            recent_mvp_seasons = {a.season for a in mvp_only if a.season >= 2023}
            assert recent_mvp_seasons == {2023, 2025}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_awards_for_league_season_filters_by_award_type():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_1 = await _make_player(pool, league_id)
            player_2 = await _make_player(pool, league_id)

            award_1 = await award_repo.create_award(
                pool, league_id=league_id, player_id=player_1, season=2026, award_type=AwardType.ROY
            )
            award_2 = await award_repo.create_award(
                pool,
                league_id=league_id,
                player_id=player_2,
                season=2026,
                award_type=AwardType.SMOY,
            )

            for_season = await award_repo.list_awards_for_league_season(pool, league_id, 2026)
            assert {a.id for a in for_season} == {award_1.id, award_2.id}

            roy_only = await award_repo.list_awards_for_league_season(
                pool, league_id, 2026, award_type=AwardType.ROY
            )
            assert [a.id for a in roy_only] == [award_1.id]

            assert await award_repo.list_awards_for_league_season(pool, league_id, 2099) == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_duplicate_award_raises_unique_violation():
    """`create_award` is a plain INSERT — the migration's UNIQUE
    (league_id, player_id, season, award_type) constraint is meant to raise
    on a duplicate, not be silently absorbed by an upsert."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            player_id = await _make_player(pool, league_id)
            await award_repo.create_award(
                pool,
                league_id=league_id,
                player_id=player_id,
                season=2026,
                award_type=AwardType.MIP,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await award_repo.create_award(
                    pool,
                    league_id=league_id,
                    player_id=player_id,
                    season=2026,
                    award_type=AwardType.MIP,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
