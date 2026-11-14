"""`tools/seed_dev_league.py --replace-league` against the real Postgres: the guild's old
league, its `league_channels` and `team_gm_channels` rows go, the Discord deletes happen
first (through a fake -- no network, no token), and the new league is seeded.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it, like every other
module in tests/integration/. Guild ids are random, so no real league is ever touched."""

from __future__ import annotations

import random

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import channel_repo, league_repo, team_repo
from dba2.domain.channel import ChannelPurpose
from dba2.services import league_service
from tools import seed_dev_league as sdl


def _new_guild_id() -> int:
    return random.randint(10_000_000, 2_000_000_000)


async def _make_league_with_channels(pool, guild_id: int, base_channel_id: int):
    """A 30-team league with the three league channels and one team GM channel."""
    league = await league_service.create_league(
        pool,
        discord_guild_id=guild_id,
        name="Replace Me",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    channel_ids = []
    purposes = (ChannelPurpose.GAMES, ChannelPurpose.STANDINGS, ChannelPurpose.WIRE)
    for offset, purpose in enumerate(purposes):
        channel_id = base_channel_id + offset
        await channel_repo.create_league_channel(
            pool, league_id=league.id, purpose=purpose, channel_id=channel_id
        )
        channel_ids.append(channel_id)
    teams = await team_repo.list_teams_for_league(pool, league.id)
    gm_channel_id = base_channel_id + 10
    await channel_repo.create_gm_channel(
        pool, league_id=league.id, team_id=teams[0].id, channel_id=gm_channel_id
    )
    channel_ids.append(gm_channel_id)
    return league, channel_ids


async def _count(pool, table: str, league_id: int) -> int:
    sql = f"SELECT count(*) FROM {table} WHERE league_id = $1"
    return await pool.fetchval(sql, league_id)


def _recording_deleter(
    calls: list[int], gone: frozenset[int] = frozenset(), fail: int | None = None
):
    async def delete(channel_id: int) -> bool:
        calls.append(channel_id)
        if channel_id == fail:
            raise sdl.ReplaceLeagueError(
                f"Discord refused to delete channel {channel_id}: HTTP 500"
            )
        return channel_id not in gone

    return delete


def _seed_kwargs(guild_id: int) -> dict:
    return {
        "discord_guild_id": guild_id,
        "name": "Fresh",
        "start_season_year": 2026,
        "current_season": 2026,
        "commissioner_user_id": 1,
        "rng_seed": 1,
        "players_per_team": 1,
    }


async def _cleanup(pool, *guild_ids: int) -> None:
    for guild_id in guild_ids:
        await pool.execute("DELETE FROM leagues WHERE discord_guild_id = $1", guild_id)


@pytest.mark.asyncio
async def test_replace_league_removes_the_old_league_and_seeds_a_new_one():
    pool = await get_pool()
    guild_id = _new_guild_id()
    bystander_guild_id = _new_guild_id() + 1
    try:
        old, old_channels = await _make_league_with_channels(pool, guild_id, 9_000_000_100)
        bystander, bystander_channels = await _make_league_with_channels(
            pool, bystander_guild_id, 9_000_000_200
        )
        calls: list[int] = []

        new_id = await sdl.seed_dev_league(
            **_seed_kwargs(guild_id),
            replace_league=True,
            # one channel Discord reports already gone: the run must still proceed
            delete_channel=_recording_deleter(calls, gone=frozenset({old_channels[1]})),
        )

        # Discord saw exactly the old league's channels: league channels then the GM channel
        assert calls == old_channels
        # the old league and everything cascading from it are gone
        assert await league_repo.get_league(pool, old.id) is None
        assert await _count(pool, "league_channels", old.id) == 0
        assert await _count(pool, "team_gm_channels", old.id) == 0
        assert await pool.fetchval("SELECT count(*) FROM teams WHERE league_id = $1", old.id) == 0
        # the new league exists for the same guild, seeded as usual
        assert new_id != old.id
        new = await league_repo.get_league_by_guild_id(pool, guild_id)
        assert new is not None and new.id == new_id
        assert await pool.fetchval("SELECT count(*) FROM teams WHERE league_id = $1", new_id) == 30
        assert await _count(pool, "league_channels", new_id) == 0
        # another guild's league and channels were not touched
        assert await league_repo.get_league(pool, bystander.id) is not None
        assert await _count(pool, "league_channels", bystander.id) == 3
        assert set(calls).isdisjoint(bystander_channels)
    finally:
        await _cleanup(pool, guild_id, bystander_guild_id)
        await close_pool()


@pytest.mark.asyncio
async def test_a_discord_failure_leaves_the_old_league_and_seeds_nothing():
    pool = await get_pool()
    guild_id = _new_guild_id()
    try:
        old, old_channels = await _make_league_with_channels(pool, guild_id, 9_000_000_300)
        calls: list[int] = []

        with pytest.raises(sdl.ReplaceLeagueError, match="HTTP 500"):
            await sdl.seed_dev_league(
                **_seed_kwargs(guild_id),
                replace_league=True,
                delete_channel=_recording_deleter(calls, fail=old_channels[1]),
            )

        assert calls == old_channels[:2]  # stopped at the failure
        assert (await league_repo.get_league_by_guild_id(pool, guild_id)).id == old.id
        assert await _count(pool, "league_channels", old.id) == 3
        assert await _count(pool, "team_gm_channels", old.id) == 1
        remaining = await pool.fetchval(
            "SELECT count(*) FROM leagues WHERE discord_guild_id = $1", guild_id
        )
        assert remaining == 1
    finally:
        await _cleanup(pool, guild_id)
        await close_pool()


@pytest.mark.asyncio
async def test_replace_league_on_a_guild_with_no_league_just_seeds():
    pool = await get_pool()
    guild_id = _new_guild_id()
    try:
        calls: list[int] = []
        new_id = await sdl.seed_dev_league(
            **_seed_kwargs(guild_id), replace_league=True, delete_channel=_recording_deleter(calls)
        )
        assert calls == []
        assert (await league_repo.get_league_by_guild_id(pool, guild_id)).id == new_id
    finally:
        await _cleanup(pool, guild_id)
        await close_pool()


@pytest.mark.asyncio
async def test_load_replacement_plan_reads_the_registry_in_delete_order():
    pool = await get_pool()
    guild_id = _new_guild_id()
    try:
        old, old_channels = await _make_league_with_channels(pool, guild_id, 9_000_000_400)
        plan = await sdl.load_replacement_plan(pool, guild_id)
        assert plan.guild_id == guild_id
        assert plan.league_ids == (old.id,)
        assert plan.channel_ids == tuple(old_channels)
    finally:
        await _cleanup(pool, guild_id)
        await close_pool()
