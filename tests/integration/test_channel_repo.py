"""Live I/O proof for `data/repositories/channel_repo.py` -- migration
016's `league_channels`/`team_gm_channels` tables (Decision P5-12,
P5-41). `tests/integration/test_bot_channels.py` proves `bot/channels.
py`'s reconcile logic end to end (against a fake Discord guild + this
real repo); this file proves the repo module itself, in isolation.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it
-- same requirement as every other tests/integration/ module that
touches real Postgres.
"""
from __future__ import annotations

import random

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import channel_repo, league_repo, team_repo
from dba2.domain.channel import ChannelPurpose
from dba2.domain.phase import Phase
from dba2.domain.team import GmPhilosophy, Timeline

_SEASON = 2026


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Channel Repo Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=1,
        rng_seed=1,
        current_phase=Phase.REGULAR_SEASON,
    )
    return league.id


async def _make_team(pool, league_id: int, code: str) -> int:
    team = await team_repo.create_team(
        pool,
        league_id=league_id,
        code=code,
        name=f"{code} Team",
        city=f"{code} City",
        conference="East",
        division="Atlantic",
        home_market_size=3,
        gm_philosophy=GmPhilosophy.EGALITARIAN,
        timeline=Timeline.RETOOL,
    )
    return team.id


@pytest.mark.asyncio
async def test_league_channel_create_get_and_id_update_roundtrip():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            assert await channel_repo.get_league_channel(
                pool, league_id=league_id, purpose=ChannelPurpose.WIRE
            ) is None

            created = await channel_repo.create_league_channel(
                pool, league_id=league_id, purpose=ChannelPurpose.WIRE, channel_id=111
            )
            assert created.channel_id == 111
            assert created.message_id is None

            fetched = await channel_repo.get_league_channel(
                pool, league_id=league_id, purpose=ChannelPurpose.WIRE
            )
            assert fetched is not None
            assert fetched.channel_id == 111

            # A different purpose is a different row entirely.
            assert await channel_repo.get_league_channel(
                pool, league_id=league_id, purpose=ChannelPurpose.STANDINGS
            ) is None

            await channel_repo.set_league_channel_id(
                pool, league_id=league_id, purpose=ChannelPurpose.WIRE, channel_id=222
            )
            refetched = await channel_repo.get_league_channel(
                pool, league_id=league_id, purpose=ChannelPurpose.WIRE
            )
            assert refetched is not None
            assert refetched.channel_id == 222
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_standings_message_id_write_and_clear():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            await channel_repo.create_league_channel(
                pool, league_id=league_id, purpose=ChannelPurpose.STANDINGS, channel_id=333
            )
            await channel_repo.set_league_channel_message_id(
                pool, league_id=league_id, purpose=ChannelPurpose.STANDINGS, message_id=999
            )
            row = await channel_repo.get_league_channel(
                pool, league_id=league_id, purpose=ChannelPurpose.STANDINGS
            )
            assert row is not None
            assert row.message_id == 999

            await channel_repo.set_league_channel_message_id(
                pool, league_id=league_id, purpose=ChannelPurpose.STANDINGS, message_id=None
            )
            row_cleared = await channel_repo.get_league_channel(
                pool, league_id=league_id, purpose=ChannelPurpose.STANDINGS
            )
            assert row_cleared is not None
            assert row_cleared.message_id is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_gm_channel_create_archive_and_reactivate_cycle():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "AAA")

            assert await channel_repo.get_active_gm_channel(pool, team_id) is None
            assert await channel_repo.get_most_recent_gm_channel(pool, team_id) is None

            created = await channel_repo.create_gm_channel(
                pool, league_id=league_id, team_id=team_id, channel_id=444
            )
            assert created.archived_at is None

            active = await channel_repo.get_active_gm_channel(pool, team_id)
            assert active is not None
            assert active.id == created.id

            await channel_repo.archive_gm_channel(pool, channel_row_id=created.id)
            assert await channel_repo.get_active_gm_channel(pool, team_id) is None

            most_recent = await channel_repo.get_most_recent_gm_channel(pool, team_id)
            assert most_recent is not None
            assert most_recent.id == created.id
            assert most_recent.archived_at is not None

            # A no-op re-archive must not overwrite the original timestamp
            # (team_repo.close_manager_assignment's own precedent).
            first_archived_at = most_recent.archived_at
            await channel_repo.archive_gm_channel(pool, channel_row_id=created.id)
            re_fetched = await channel_repo.get_most_recent_gm_channel(pool, team_id)
            assert re_fetched is not None
            assert re_fetched.archived_at == first_archived_at

            await channel_repo.reactivate_gm_channel(
                pool, channel_row_id=created.id, channel_id=555
            )
            reactivated = await channel_repo.get_active_gm_channel(pool, team_id)
            assert reactivated is not None
            assert reactivated.id == created.id
            assert reactivated.channel_id == 555
            assert reactivated.archived_at is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
