"""Live I/O proof for `dba2/bot/client.py::_reconcile_guild` -- §42.1's
Blocking Issue 3 (critic re-verification of the round-2 fix, an
undisclosed regression in a caller nobody reviewed).

`_reconcile_guild` used to short-circuit with `except discord.
HTTPException: log; return` around its call to `reconcile_league_
channels` -- a deliberate bail-out if the league-channel reconcile fails
(e.g. the bot lacks Manage Channels on a fresh guild, the exact scenario
commit `90443f4`'s own message cites as motivation), skipping every
per-team GM-channel reconcile for that guild rather than attempting one
per managed team, each independently failing the same way and burning
Discord's rate-limit budget for nothing. Once `reconcile_league_
channels` started swallowing every per-purpose failure internally
(returning a partial/empty dict instead of raising -- a `dba2/bot/
channels.py` change, never disclosed in `client.py` or any commit body
that touched it), that `except` clause went dead: the `return` stopped
firing, and the bot silently started proceeding to N individually-
failing GM-channel reconciles on exactly the guild state the original
short-circuit protected against.

This proves the fix, the same "decision logic against a real Postgres
pool and a fake Discord guild" shape `test_bot_channels.py`'s own module
docstring establishes and discloses (not a real-guild test):

1. A guild where EVERY league-channel purpose fails to reconcile still
   bails out before the per-team loop -- no GM-channel creation is
   attempted for any managed team.
2. A guild where every purpose reconciles fine still runs the per-team
   loop as before -- the bail-out does not fire on the healthy path.
"""
from __future__ import annotations

import random
from unittest.mock import AsyncMock, PropertyMock, patch

import discord
import pytest

from dba2.bot.client import DBABot, build_bot
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import channel_repo
from dba2.services import league_service

from .test_bot_channels import _FakeGuild

_COMMISSIONER_ID = 100
_MANAGER_ID = 200
_OTHER_MANAGER_ID = 201


class _ChannelCreateAlwaysFailsGuild(_FakeGuild):
    """Every `create_text_channel` call raises `discord.HTTPException` --
    simulates the bot lacking Manage Channels on a fresh guild, the exact
    scenario commit `90443f4`'s own message names as motivation. Every
    `ChannelPurpose` fails, so `reconcile_league_channels` comes back
    with an EMPTY result -- the worst case, and the one the original
    short-circuit existed for.

    Counts every call (successful or not): both the league-channel and
    GM-channel reconciles fail identically against this guild, so a
    channel COUNT alone can't distinguish "bailed out before the
    per-team loop" from "attempted every team and each one individually
    failed" -- both end with zero real channels. The call count can:
    3 (all three `ChannelPurpose`s attempted, nothing beyond that) proves
    the bail-out fired; more than 3 proves the per-team loop still ran."""

    def __init__(self) -> None:
        super().__init__()
        self.create_text_channel_calls = 0

    async def create_text_channel(self, name: str, *, overwrites=None, **_kwargs):
        self.create_text_channel_calls += 1
        raise discord.HTTPException.__new__(discord.HTTPException)


class _StandingsChannelCreateFailsGuild(_FakeGuild):
    """Fails `create_text_channel` only for the "standings" league
    channel (§6.1's `ChannelPurpose.STANDINGS`, named via `_LEAGUE_
    CHANNEL_NAMES` in `dba2/bot/channels.py`) -- WIRE and GAMES succeed,
    STANDINGS fails, giving `reconcile_league_channels` a genuine 2-of-3
    PARTIAL result (nit N1) rather than the total-failure case `_ChannelCreate
    AlwaysFailsGuild` above covers. Failing by NAME rather than by call
    COUNT (unlike `test_commissioner_cog.py`'s own `_SecondChannelCreate
    FailsGuild`) matters here specifically because this test also needs
    the per-team GM-channel creates (`gm-<code>`) that come AFTER the
    league-channel reconcile to succeed -- a call-count-based failure
    would poison those too."""

    async def create_text_channel(self, name: str, *, overwrites=None, **kwargs):
        if name == "standings":
            raise discord.HTTPException.__new__(discord.HTTPException)
        return await super().create_text_channel(name, overwrites=overwrites, **kwargs)


async def _make_league_with_two_managed_teams(pool):
    league = await league_service.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Client Reconcile Guild Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=_COMMISSIONER_ID,
        rng_seed=1,
    )
    from dba2.data.repositories import team_repo

    teams = await team_repo.list_teams_for_league(pool, league.id)
    await league_service.assign_manager(
        pool,
        league_id=league.id,
        team_id=teams[0].id,
        user_id=_MANAGER_ID,
        actor_user_id=_COMMISSIONER_ID,
    )
    await league_service.assign_manager(
        pool,
        league_id=league.id,
        team_id=teams[1].id,
        user_id=_OTHER_MANAGER_ID,
        actor_user_id=_COMMISSIONER_ID,
    )
    return league


@pytest.mark.asyncio
async def test_reconcile_guild_bails_out_before_the_per_team_loop_on_total_failure():
    """The regression's own repro, inverted into a proof: when every
    league-channel purpose fails, no GM channel is created for either
    managed team -- the bail-out fires, matching the original short-
    circuit's protective intent."""
    pool = await get_pool()
    try:
        league = await _make_league_with_two_managed_teams(pool)
        try:
            guild = _ChannelCreateAlwaysFailsGuild()
            guild.id = league.discord_guild_id
            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()

            await bot._reconcile_guild(guild, pool)

            # Exactly 3 attempts (one per `ChannelPurpose`) -- if the
            # per-team loop had run too, each of the 2 managed teams'
            # `reconcile_gm_channel` would add its own `create_text_
            # channel` attempt, pushing this past 3.
            assert guild.create_text_channel_calls == 3
            assert guild._channels == {}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_guild_one_teams_gm_channel_failure_does_not_abort_the_others():
    """Blocking Issue B1, critic re-verification round 4: `reconcile_gm_
    channel` writes to Postgres (`channel_repo.create_gm_channel`) inside
    the per-team loop's `try`, which was scoped to `except discord.
    HTTPException` -- a DB-layer exception is NOT a `discord.HTTPException`
    and escaped that `try` entirely, aborting `_reconcile_guild`'s
    per-team loop for every remaining team in the guild, not just the one
    that failed. Reproduced live before this fix: patching `channel_repo.
    create_gm_channel` to raise `RuntimeError` for one team and driving
    `_reconcile_guild` let the exception escape the function outright,
    never reaching the second team. This proves the fix: one team's
    failure is logged and the loop continues to the next team."""
    pool = await get_pool()
    try:
        league = await _make_league_with_two_managed_teams(pool)
        try:
            from dba2.data.repositories import team_repo

            teams = await team_repo.list_teams_for_league(pool, league.id)
            failing_team_id = teams[0].id
            succeeding_team_id = teams[1].id

            real_create_gm_channel = channel_repo.create_gm_channel

            async def _create_gm_channel_fails_for_one_team(ex, *, league_id, team_id, channel_id):
                if team_id == failing_team_id:
                    raise RuntimeError("db exploded for one team")
                return await real_create_gm_channel(
                    ex, league_id=league_id, team_id=team_id, channel_id=channel_id
                )

            guild = _FakeGuild()
            guild.id = league.discord_guild_id
            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()

            with patch.object(
                channel_repo,
                "create_gm_channel",
                side_effect=_create_gm_channel_fails_for_one_team,
            ):
                await bot._reconcile_guild(guild, pool)

            # The failing team's registry write never committed (its
            # Discord-side channel may still exist, orphaned -- a
            # pre-existing, disclosed limitation of "Discord write before
            # DB write," out of this fix's scope) -- but critically, the
            # SECOND team's reconcile still ran and DID commit, proving
            # the loop was not aborted by the first team's failure.
            failing_row = await channel_repo.get_active_gm_channel(pool, failing_team_id)
            succeeding_row = await channel_repo.get_active_gm_channel(pool, succeeding_team_id)
            assert failing_row is None
            assert succeeding_row is not None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_guild_partial_league_channel_success_still_runs_the_per_team_loop():
    """Nit N1, critic round 4: the bail-out threshold used to be `len(
    league_channels) < len(ChannelPurpose)`, treating a partial
    league-channel success identically to total failure and permanently
    suppressing the per-team GM reconcile for that guild. Deliberately
    narrowed to bail only on total failure (`not league_channels`) -- a
    partial success is evidence the bot CAN create channels in this
    guild, so the per-team loop still runs (and each team's own failure
    is independently caught, per B1 above, rather than needing this
    outer bail-out for protection)."""
    pool = await get_pool()
    try:
        league = await _make_league_with_two_managed_teams(pool)
        try:
            guild = _StandingsChannelCreateFailsGuild()
            guild.id = league.discord_guild_id
            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()

            await bot._reconcile_guild(guild, pool)

            # 2 league channels (WIRE and GAMES succeed, STANDINGS fails --
            # the partial case) + 2 GM channels (the per-team loop still ran
            # for both managed teams) = 4 real channels created.
            assert len(guild._channels) == 4
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_guild_still_runs_the_per_team_loop_when_league_channels_succeed():
    """The bail-out must not fire on the healthy path -- both managed
    teams still get a GM channel when the league-channel reconcile fully
    succeeds, exactly as before this fix."""
    pool = await get_pool()
    try:
        league = await _make_league_with_two_managed_teams(pool)
        try:
            guild = _FakeGuild()
            guild.id = league.discord_guild_id
            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()

            await bot._reconcile_guild(guild, pool)

            # 3 league channels (wire, standings, games) + 2 GM channels (one
            # per managed team) = 5 real channels created.
            assert len(guild._channels) == 5
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


async def _make_league_with_one_managed_team(pool):
    league = await league_service.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Client on_ready Guild Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=_COMMISSIONER_ID,
        rng_seed=1,
    )
    from dba2.data.repositories import team_repo

    teams = await team_repo.list_teams_for_league(pool, league.id)
    await league_service.assign_manager(
        pool,
        league_id=league.id,
        team_id=teams[0].id,
        user_id=_MANAGER_ID,
        actor_user_id=_COMMISSIONER_ID,
    )
    return league


@pytest.mark.asyncio
async def test_on_ready_survives_a_guilds_tree_sync_failure_and_reconciles_every_guild():
    """The blocking issue, critic round 5: `_reconcile_guild`'s guard
    around `tree.copy_global_to`/`tree.sync` was scoped to `except
    discord.HTTPException`, the same narrow-exception bug fixed 4 times
    over elsewhere in this project -- `tree.sync` is real Discord network
    I/O, and `discord.py` can raise a bare `ConnectionResetError`/`OSError`/
    `asyncio.TimeoutError` after exhausting its own retry budget, neither
    a `discord.HTTPException`. Reproduced live before this fix: patching
    `bot.tree.sync` to raise `ConnectionResetError` for one of 2 guilds let
    the exception escape `_reconcile_guild` entirely, which in turn escaped
    `on_ready`'s per-guild loop (itself unguarded) -- ZERO channels were
    reconciled in EITHER guild, not just the one whose sync failed. This
    proves the fix: `tree.sync` raising a non-`HTTPException` for one guild
    no longer escapes `_reconcile_guild` (widened to `except Exception`),
    so both guilds' channels get reconciled and `on_ready` itself never
    raises."""
    pool = await get_pool()
    try:
        league1 = await _make_league_with_one_managed_team(pool)
        league2 = await _make_league_with_one_managed_team(pool)
        try:
            guild1 = _FakeGuild()
            guild1.id = league1.discord_guild_id
            guild2 = _FakeGuild()
            guild2.id = league2.discord_guild_id

            bot = build_bot()
            await bot.setup_hook()

            async def _sync_fails_only_for_guild1(*, guild=None, **_kwargs):
                if guild is not None and guild.id == guild1.id:
                    raise ConnectionResetError("simulated network reset")
                return []

            bot.tree.sync = AsyncMock(side_effect=_sync_fails_only_for_guild1)

            with patch.object(
                DBABot, "guilds", new_callable=PropertyMock, return_value=[guild1, guild2]
            ):
                await bot.on_ready()  # must not raise

            # 3 league channels (wire, standings, games) + 1 GM channel (the
            # one managed team) = 4 real channels each -- both guilds fully
            # reconciled despite guild1's sync failure.
            assert len(guild1._channels) == 4
            assert len(guild2._channels) == 4
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league1.id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league2.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_on_ready_one_guilds_reconcile_failure_does_not_abort_the_others():
    """The defense-in-depth half of the same fix: `on_ready`'s per-guild
    loop itself had no guard of its own around calling `_reconcile_guild`
    -- so ANY exception escaping `_reconcile_guild`, regardless of where
    inside it originates (not just the `tree.sync` case above), would
    abort the loop for every OTHER guild too. Proven independently of the
    `tree.sync` widening above by making `_reconcile_guild` itself raise
    for one guild (a stand-in for "some future code path inside it that
    doesn't have its own guard") -- confirming the loop still reaches and
    reconciles the second guild."""
    pool = await get_pool()
    try:
        league1 = await _make_league_with_one_managed_team(pool)
        league2 = await _make_league_with_one_managed_team(pool)
        try:
            guild1 = _FakeGuild()
            guild1.id = league1.discord_guild_id
            guild2 = _FakeGuild()
            guild2.id = league2.discord_guild_id

            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()

            real_reconcile_guild = bot._reconcile_guild

            async def _reconcile_guild_fails_for_guild1(guild, pool):
                if guild.id == guild1.id:
                    raise RuntimeError("simulated unguarded failure inside _reconcile_guild")
                return await real_reconcile_guild(guild, pool)

            with (
                patch.object(
                    DBABot, "guilds", new_callable=PropertyMock, return_value=[guild1, guild2]
                ),
                patch.object(
                    bot, "_reconcile_guild", side_effect=_reconcile_guild_fails_for_guild1
                ),
            ):
                await bot.on_ready()  # must not raise

            # guild2's reconcile still ran and produced real channels
            # (3 league channels + 1 GM channel = 4), proving guild1's
            # failure did not abort the loop before reaching it.
            assert len(guild2._channels) == 4
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league1.id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league2.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_guild_drains_the_offer_delivery_queue_after_the_gm_channels():
    """Design §6.5: the startup drain rides `_reconcile_guild`, AFTER the GM
    channels exist -- an offer written while the bot was down needs the channel
    the reconcile just made."""
    from dba2.bot import offer_delivery

    pool = await get_pool()
    try:
        league = await _make_league_with_two_managed_teams(pool)
        try:
            guild = _FakeGuild()
            guild.id = league.discord_guild_id
            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()
            with patch.object(
                offer_delivery, "drain_undelivered_offers", new_callable=AsyncMock
            ) as drain:
                drain.return_value = offer_delivery.DrainReport(outcomes={})
                await bot._reconcile_guild(guild, pool)

            drain.assert_awaited_once()
            assert drain.await_args.kwargs == {"league_id": league.id}
            # Called after the reconcile: both managed teams already have rows.
            from dba2.data.repositories import team_repo

            for team in await team_repo.list_teams_for_league(pool, league.id):
                if team.manager_user_id is not None:
                    assert await channel_repo.get_active_gm_channel(pool, team.id) is not None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_a_failing_offer_drain_never_fails_the_reconcile_it_rides_on():
    from dba2.bot import offer_delivery

    pool = await get_pool()
    try:
        league = await _make_league_with_two_managed_teams(pool)
        try:
            guild = _FakeGuild()
            guild.id = league.discord_guild_id
            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()
            with patch.object(
                offer_delivery,
                "drain_undelivered_offers",
                new_callable=AsyncMock,
                side_effect=RuntimeError("discord is down"),
            ):
                await bot._reconcile_guild(guild, pool)  # must not raise
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_guild_drains_offers_even_when_every_channel_create_fails():
    """The total-failure bail-out skips the per-team loop but not the drain:
    the drain reads existing `gm_channels` rows, which are postable whether or
    not THIS startup's creates succeeded. Before the fix the bail-out `return`
    preceded the drain, so a guild whose creates all fail never delivered
    queued offers. The healthy guild still drains exactly once (no double
    drain from the two exit paths)."""
    from dba2.bot import offer_delivery

    pool = await get_pool()
    try:
        league = await _make_league_with_two_managed_teams(pool)
        healthy_league = await _make_league_with_two_managed_teams(pool)
        try:
            failing_guild = _ChannelCreateAlwaysFailsGuild()
            failing_guild.id = league.discord_guild_id
            healthy_guild = _FakeGuild()
            healthy_guild.id = healthy_league.discord_guild_id
            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()
            with patch.object(
                offer_delivery, "drain_undelivered_offers", new_callable=AsyncMock
            ) as drain:
                drain.return_value = offer_delivery.DrainReport(outcomes={})
                await bot._reconcile_guild(failing_guild, pool)
                # Bail-out fired (3 attempts, no per-team loop) AND the drain ran.
                assert failing_guild.create_text_channel_calls == 3
                drain.assert_awaited_once()
                assert drain.await_args.kwargs == {"league_id": league.id}

                drain.reset_mock()
                await bot._reconcile_guild(healthy_guild, pool)
                drain.assert_awaited_once()
                assert drain.await_args.kwargs == {"league_id": healthy_league.id}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", healthy_league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_a_raising_offer_drain_does_not_escape_the_total_failure_bail_out():
    from dba2.bot import offer_delivery

    pool = await get_pool()
    try:
        league = await _make_league_with_two_managed_teams(pool)
        try:
            guild = _ChannelCreateAlwaysFailsGuild()
            guild.id = league.discord_guild_id
            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()
            with patch.object(
                offer_delivery,
                "drain_undelivered_offers",
                new_callable=AsyncMock,
                side_effect=RuntimeError("discord is down"),
            ) as drain:
                await bot._reconcile_guild(guild, pool)  # must not raise
            drain.assert_awaited_once()
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


# --- Stage D: the `#games` startup sweep rides the same two exits as the drain -


@pytest.mark.asyncio
async def test_reconcile_guild_sweeps_games_once_after_the_drain_on_the_healthy_path():
    """Design P5-97.q item 6 (M12): `game_feed.sweep_league` is awaited from
    `_reconcile_guild` with the same call contract shape as the offer drain --
    one await, `league_id=` as the only keyword -- after the GM channels exist
    and after the drain."""
    from dba2.bot import game_feed, offer_delivery

    pool = await get_pool()
    try:
        league = await _make_league_with_two_managed_teams(pool)
        try:
            guild = _FakeGuild()
            guild.id = league.discord_guild_id
            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()
            order: list[str] = []

            async def drain(*_args, **_kwargs):
                order.append("drain")
                return offer_delivery.DrainReport(outcomes={})

            async def sweep(*_args, **_kwargs):
                order.append("sweep")
                return game_feed.SweepReport(league_id=league.id, entries=())

            with (
                patch.object(offer_delivery, "drain_undelivered_offers", side_effect=drain),
                patch.object(
                    game_feed, "sweep_league", new_callable=AsyncMock, side_effect=sweep
                ) as swept,
            ):
                await bot._reconcile_guild(guild, pool)

            swept.assert_awaited_once()
            assert swept.await_args.kwargs == {"league_id": league.id}
            assert swept.await_args.args == (bot, pool)
            assert order == ["drain", "sweep"]
            from dba2.data.repositories import team_repo

            for team in await team_repo.list_teams_for_league(pool, league.id):
                if team.manager_user_id is not None:
                    assert await channel_repo.get_active_gm_channel(pool, team.id) is not None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_a_failing_games_sweep_never_fails_the_reconcile_it_rides_on():
    from dba2.bot import game_feed

    pool = await get_pool()
    try:
        league = await _make_league_with_two_managed_teams(pool)
        try:
            guild = _FakeGuild()
            guild.id = league.discord_guild_id
            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()
            with patch.object(
                game_feed,
                "sweep_league",
                new_callable=AsyncMock,
                side_effect=RuntimeError("discord is down"),
            ) as swept:
                await bot._reconcile_guild(guild, pool)  # must not raise
            swept.assert_awaited_once()
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_guild_sweeps_games_even_when_every_channel_create_fails():
    """The total-failure bail-out skips the per-team loop but not the sweep (it
    reads existing `league_channels` rows and never creates a channel), and the
    healthy guild sweeps exactly once -- no double sweep from the two exits."""
    from dba2.bot import game_feed

    pool = await get_pool()
    try:
        league = await _make_league_with_two_managed_teams(pool)
        healthy_league = await _make_league_with_two_managed_teams(pool)
        try:
            failing_guild = _ChannelCreateAlwaysFailsGuild()
            failing_guild.id = league.discord_guild_id
            healthy_guild = _FakeGuild()
            healthy_guild.id = healthy_league.discord_guild_id
            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()
            with patch.object(game_feed, "sweep_league", new_callable=AsyncMock) as swept:
                swept.return_value = game_feed.SweepReport(league_id=league.id, entries=())
                await bot._reconcile_guild(failing_guild, pool)
                assert failing_guild.create_text_channel_calls == 3  # bail-out fired
                swept.assert_awaited_once()
                assert swept.await_args.kwargs == {"league_id": league.id}

                swept.reset_mock()
                await bot._reconcile_guild(healthy_guild, pool)
                swept.assert_awaited_once()
                assert swept.await_args.kwargs == {"league_id": healthy_league.id}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
            await pool.execute("DELETE FROM leagues WHERE id = $1", healthy_league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_a_raising_games_sweep_does_not_escape_the_total_failure_bail_out():
    from dba2.bot import game_feed

    pool = await get_pool()
    try:
        league = await _make_league_with_two_managed_teams(pool)
        try:
            guild = _ChannelCreateAlwaysFailsGuild()
            guild.id = league.discord_guild_id
            bot = build_bot()
            await bot.setup_hook()
            bot.tree.sync = AsyncMock()
            with patch.object(
                game_feed,
                "sweep_league",
                new_callable=AsyncMock,
                side_effect=RuntimeError("discord is down"),
            ) as swept:
                await bot._reconcile_guild(guild, pool)  # must not raise
            swept.assert_awaited_once()
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()
