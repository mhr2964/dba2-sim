"""Live I/O proof for `dba2/bot/cogs/commissioner.py`'s `/assign-manager`/
`/remove-manager` commands -- Blocking Issue 6, Stage C critic round: both
commands did the DB write, then unguarded Discord I/O with no `defer()`
and no exception handling, unlike `/setup` and unlike `client.py`'s own
identical reconcile call. This proves, against a real Postgres pool and a
fake Discord guild (reusing `test_bot_channels.py`'s own fakes, the same
disclosed "decision logic, not a live gateway" scope that module's
docstring names), that:

1. Both commands `defer()` before the DB write.
2. When the post-commit Discord reconcile fails, the user still gets a
   real followup message (never "the application did not respond"), and
   it honestly names the partial failure rather than claiming full
   success.
3. The DB write is never rolled back by a Discord-side failure (Decision
   P5-14: the two are separable by design).
"""
from __future__ import annotations

import random
from unittest.mock import patch

import discord
import pytest

from dba2.bot import channels as bot_channels
from dba2.bot.cogs.commissioner import CommissionerCommands
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import channel_repo, team_repo
from dba2.services import league_service

from .test_bot_channels import _fake_member, _FakeGuild

_COMMISSIONER_ID = 100
_MANAGER_ID = 200


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self._done = False
        self.sent_messages: list[str] = []

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred = True
        self._done = True

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, message: str, *, ephemeral: bool = False) -> None:
        self._done = True
        self.sent_messages.append(message)


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []

    async def send(self, message: str, *, ephemeral: bool = False) -> None:
        self.sent_messages.append(message)


class _FakeInteraction:
    def __init__(self, *, guild, guild_id: int, user_id: int) -> None:
        self.guild = guild
        self.guild_id = guild_id
        self.user = _fake_member(user_id)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


class _ChannelCreateFailsGuild(_FakeGuild):
    """A guild whose `create_text_channel` always raises `discord.
    HTTPException` -- simulates a real Discord-side failure (a
    permissions error, a rate limit) on the fresh-create path `reconcile_
    gm_channel` takes for a team assigned a manager for the first time."""

    async def create_text_channel(self, name: str, *, overwrites=None, **_kwargs):
        raise discord.HTTPException.__new__(discord.HTTPException)


class _SecondChannelCreateFailsGuild(_FakeGuild):
    """A guild whose `create_text_channel` succeeds on its first call and
    raises `discord.HTTPException` on every call after that -- simulates
    a PARTIAL Discord-side failure mid-`reconcile_league_channels`
    (`/setup` reconciles `ChannelPurpose.WIRE`, `STANDINGS`, then `GAMES`;
    this lets `WIRE` succeed and both later purposes fail)."""

    def __init__(self) -> None:
        super().__init__()
        self._create_calls = 0

    async def create_text_channel(self, name: str, *, overwrites=None, **kwargs):
        self._create_calls += 1
        if self._create_calls >= 2:
            raise discord.HTTPException.__new__(discord.HTTPException)
        return await super().create_text_channel(name, overwrites=overwrites, **kwargs)


class _ChannelCreateRaisesConnectionResetGuild(_FakeGuild):
    """§42.1's Blocking Issue 2 (critic re-verification): a guild whose
    `create_text_channel` raises a bare `ConnectionResetError` -- the
    shape of failure `discord.py` itself can surface after exhausting its
    own retry budget on a real network fault, which is NOT a `discord.
    HTTPException` subclass. Before the fix, this escaped `reconcile_
    league_channels`'s per-purpose guard (scoped to `except discord.
    HTTPException` only) and then the round-2 fix's own `/setup` wrapper
    (the identical scope), leaving the interaction deferred forever with
    zero followups -- the literal same bug Blocking Issue A fixed, for a
    different exception type."""

    async def create_text_channel(self, name: str, *, overwrites=None, **_kwargs):
        raise ConnectionResetError("connection reset by peer")


async def _make_league_and_team(pool):
    league = await league_service.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Commissioner Cog Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=_COMMISSIONER_ID,
        rng_seed=1,
    )
    teams = await team_repo.list_teams_for_league(pool, league.id)
    return league, teams[0]


@pytest.mark.asyncio
async def test_assign_manager_defers_before_the_db_write():
    pool = await get_pool()
    try:
        league, team = await _make_league_and_team(pool)
        try:
            guild = _FakeGuild()
            interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            cog = CommissionerCommands(bot=None)
            manager = _fake_member(_MANAGER_ID)

            await cog.assign_manager.callback(cog, interaction, team.code, manager)

            assert interaction.response.deferred is True
            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id == _MANAGER_ID
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_assign_manager_reports_an_honest_partial_failure_when_reconcile_fails():
    """The DB write succeeds (the team really is now managed), the
    Discord reconcile fails, and the user's followup says both things --
    not silence, not an unqualified success claim."""
    pool = await get_pool()
    try:
        league, team = await _make_league_and_team(pool)
        try:
            guild = _ChannelCreateFailsGuild()
            interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            cog = CommissionerCommands(bot=None)
            manager = _fake_member(_MANAGER_ID)

            await cog.assign_manager.callback(cog, interaction, team.code, manager)

            # The DB write is NOT rolled back by the Discord-side failure.
            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id == _MANAGER_ID

            # The user got a real response, naming the partial failure.
            assert len(interaction.followup.sent_messages) == 1
            message = interaction.followup.sent_messages[0]
            assert "now manages" in message
            assert "could not be set up" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_assign_manager_reports_an_honest_partial_failure_on_a_non_discord_reconcile_error():
    """Blocking Issue B1, critic re-verification round 4: `reconcile_gm_
    channel` writes to Postgres (`channel_repo.create_gm_channel`) inside
    the exact scope `_reconcile_gm_channel_after_commit` only guarded with
    `except discord.HTTPException` -- a DB-layer exception (or a bare
    `ConnectionResetError`/`OSError`/`asyncio.TimeoutError` from `guild.
    fetch_member`) is NOT a `discord.HTTPException` and escaped uncaught,
    leaving the interaction deferred forever with zero followups sent.
    Reproduced live before this fix: patching `channel_repo.create_gm_
    channel` to raise `RuntimeError` and driving this callback produced
    `deferred=True, followups=0` with the exception propagating out of
    the command entirely -- the exact failure mode Blocking Issue A and
    §42.1's Blocking Issue 2 already fixed for sibling call sites."""
    pool = await get_pool()
    try:
        league, team = await _make_league_and_team(pool)
        try:
            guild = _FakeGuild()
            interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            cog = CommissionerCommands(bot=None)
            manager = _fake_member(_MANAGER_ID)

            with patch.object(
                channel_repo, "create_gm_channel", side_effect=RuntimeError("db exploded")
            ):
                await cog.assign_manager.callback(cog, interaction, team.code, manager)

            # The DB write (assigning the manager) is NOT rolled back by
            # the Discord-side reconcile's own DB-layer failure.
            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id == _MANAGER_ID

            assert len(interaction.followup.sent_messages) == 1
            message = interaction.followup.sent_messages[0]
            assert "now manages" in message
            assert "could not be set up" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_remove_manager_reports_an_honest_partial_failure_on_a_non_discord_reconcile_error():
    """`remove_manager`'s twin of the test directly above -- the archive-
    on-removal path calls `channel_repo.archive_gm_channel` inside the
    same guard."""
    pool = await get_pool()
    try:
        league, team = await _make_league_and_team(pool)
        try:
            guild = _FakeGuild()
            cog = CommissionerCommands(bot=None)
            manager = _fake_member(_MANAGER_ID)
            assign_interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            await cog.assign_manager.callback(cog, assign_interaction, team.code, manager)

            remove_interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            with patch.object(
                channel_repo, "archive_gm_channel", side_effect=RuntimeError("db exploded")
            ):
                await cog.remove_manager.callback(cog, remove_interaction, team.code)

            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id is None

            assert len(remove_interaction.followup.sent_messages) == 1
            message = remove_interaction.followup.sent_messages[0]
            assert "no longer has a manager" in message
            assert "could not be set up" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_remove_manager_defers_before_the_db_write():
    pool = await get_pool()
    try:
        league, team = await _make_league_and_team(pool)
        try:
            guild = _FakeGuild()
            cog = CommissionerCommands(bot=None)
            manager = _fake_member(_MANAGER_ID)
            assign_interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            await cog.assign_manager.callback(cog, assign_interaction, team.code, manager)

            remove_interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            await cog.remove_manager.callback(cog, remove_interaction, team.code)

            assert remove_interaction.response.deferred is True
            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id is None

            assert len(remove_interaction.followup.sent_messages) == 1
            assert "no longer has a manager" in remove_interaction.followup.sent_messages[0]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_remove_manager_reports_an_honest_partial_failure_when_reconcile_fails():
    """The DB write (clearing `manager_user_id`) succeeds; the archive-
    on-removal Discord I/O (stripping the channel's member overwrites)
    fails -- the user's followup says both things."""
    pool = await get_pool()
    try:
        league, team = await _make_league_and_team(pool)
        try:
            guild = _FakeGuild()
            cog = CommissionerCommands(bot=None)
            manager = _fake_member(_MANAGER_ID)
            assign_interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            await cog.assign_manager.callback(cog, assign_interaction, team.code, manager)

            # The manager's GM channel exists and resolves -- force the
            # archive-on-removal step's own Discord I/O (stripping the
            # channel's overwrites) to fail, matching a real permissions
            # error mid-reconcile.
            created_channel = next(iter(guild._channels.values()))

            async def _failing_set_permissions(target, *, overwrite=None, reason=None):
                raise discord.HTTPException.__new__(discord.HTTPException)

            created_channel.set_permissions = _failing_set_permissions

            remove_interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            await cog.remove_manager.callback(cog, remove_interaction, team.code)

            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id is None

            assert len(remove_interaction.followup.sent_messages) == 1
            message = remove_interaction.followup.sent_messages[0]
            assert "no longer has a manager" in message
            assert "could not be set up" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_setup_reports_an_honest_failure_when_channel_reconcile_fails():
    """Blocking Issue A, critic re-verification of the Stage C fix round:
    `/setup` called `reconcile_league_channels` completely unguarded --
    only the standings-sync half later in the same function was wrapped.
    A `discord.HTTPException` (e.g. `discord.Forbidden` on a fresh guild
    where the bot doesn't yet have Manage Channels -- very plausible on a
    user's very first command) left the interaction deferred forever with
    zero followups sent. This proves the fix: a real followup is always
    sent, naming the failure rather than leaving Discord's "Bot is
    thinking..." spinner forever."""
    pool = await get_pool()
    try:
        league, _team = await _make_league_and_team(pool)
        try:
            guild = _ChannelCreateFailsGuild()
            interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            cog = CommissionerCommands(bot=None)

            await cog.setup.callback(cog, interaction)

            assert interaction.response.deferred is True
            # The critic's own repro: zero followups sent is the bug.
            assert len(interaction.followup.sent_messages) == 1
            message = interaction.followup.sent_messages[0].lower()
            assert "failed" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_setup_reports_a_partial_success_when_one_purpose_fails():
    """A single failing purpose (here, `STANDINGS`, the second one
    reconciled) does not lose the other purposes' real result -- the
    followup distinguishes partial success ("1/3") from a total failure,
    per `reconcile_league_channels`'s own per-purpose `discord.
    HTTPException` handling."""
    pool = await get_pool()
    try:
        league, _team = await _make_league_and_team(pool)
        try:
            guild = _SecondChannelCreateFailsGuild()
            interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            cog = CommissionerCommands(bot=None)

            await cog.setup.callback(cog, interaction)

            assert len(interaction.followup.sent_messages) == 1
            message = interaction.followup.sent_messages[0]
            assert "1/3 league channel(s)" in message
            assert "Could not set up" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_setup_reports_an_honest_partial_success_when_the_standings_sync_fails():
    """Round-3 critic nit: `/setup`'s own standings-sync follow-up was
    still a THIRD hand-rolled copy of the honest-partial-success pattern
    (§43.2's own "zero duplicate copies remain" claim was inaccurate as a
    result) -- migrated here to the shared `run_after_commit`/`honest_
    suffix` module, matching `_reconcile_gm_channel_after_commit`'s own
    shape. Both league channels reconcile fine (a healthy `_FakeGuild`),
    but `sync_standings_message` itself raises -- proves the channel
    reconcile's own real success ("3/3 league channel(s)") is not lost,
    and the honest suffix (not the old hand-rolled wording) is appended."""
    pool = await get_pool()
    try:
        league, _team = await _make_league_and_team(pool)
        try:
            guild = _FakeGuild()
            interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            cog = CommissionerCommands(bot=None)

            with patch(
                "dba2.bot.cogs.commissioner.sync_standings_message",
                side_effect=RuntimeError("standings sync exploded"),
            ):
                await cog.setup.callback(cog, interaction)

            assert len(interaction.followup.sent_messages) == 1
            message = interaction.followup.sent_messages[0]
            assert "3/3 league channel(s)" in message
            assert "Could not set up" not in message
            assert "standings message could not be synced" in message
            assert "error logged" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_assign_manager_leaks_no_team_existence_to_a_non_commissioner():
    """Blocking Issue C, critic re-verification of the Stage C fix round:
    `/assign-manager` never called `ensure_commissioner` (the interaction-
    layer half P5-38 requires) before resolving the team by code -- a
    non-commissioner got `PermissionDenied` for a real team code but
    `"No team with code ... "` for a bogus one, a live team-id
    enumeration oracle even though the service layer itself was already
    safe. After the fix, both cases fail identically, before any team
    lookup runs."""
    pool = await get_pool()
    try:
        league, team = await _make_league_and_team(pool)
        try:
            guild = _FakeGuild()
            non_commissioner_id = _COMMISSIONER_ID + 999
            manager = _fake_member(_MANAGER_ID)
            cog = CommissionerCommands(bot=None)

            real_code_interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=non_commissioner_id
            )
            await cog.assign_manager.callback(cog, real_code_interaction, team.code, manager)

            bogus_code_interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=non_commissioner_id
            )
            await cog.assign_manager.callback(
                cog, bogus_code_interaction, "ZZZ", manager
            )

            real_message = real_code_interaction.response.sent_messages[0]
            bogus_message = bogus_code_interaction.response.sent_messages[0]
            assert real_message == bogus_message
            assert "is not the league's commissioner" in real_message
            assert "No team with code" not in real_message
            assert "No team with code" not in bogus_message

            # Neither call touched the database -- team is still unmanaged.
            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_remove_manager_leaks_no_team_existence_to_a_non_commissioner():
    """Same proof as `test_assign_manager_leaks_no_team_existence_to_a_
    non_commissioner`, for `/remove-manager`."""
    pool = await get_pool()
    try:
        league, team = await _make_league_and_team(pool)
        try:
            guild = _FakeGuild()
            non_commissioner_id = _COMMISSIONER_ID + 999
            cog = CommissionerCommands(bot=None)

            real_code_interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=non_commissioner_id
            )
            await cog.remove_manager.callback(cog, real_code_interaction, team.code)

            bogus_code_interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=non_commissioner_id
            )
            await cog.remove_manager.callback(cog, bogus_code_interaction, "ZZZ")

            real_message = real_code_interaction.response.sent_messages[0]
            bogus_message = bogus_code_interaction.response.sent_messages[0]
            assert real_message == bogus_message
            assert "is not the league's commissioner" in real_message
            assert "No team with code" not in real_message
            assert "No team with code" not in bogus_message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_assign_manager_reports_an_honest_failure_on_a_non_domain_db_exception():
    """Critic round 6: `/assign-manager`'s DB-write `try` (`get_team_by_
    code`'s read plus `league_service.assign_manager`'s own transaction)
    only caught `NotFoundError`/`PermissionDenied` -- both domain-only.
    An asyncpg connection reset, pool-acquisition timeout, or deadlock is
    neither, and escaped uncaught, leaving the interaction deferred
    forever with zero response -- the same "deferred forever" failure
    mode Blocking Issue A, §42.1's Blocking Issue 2, and B1 already fixed
    at five other call sites, recurring here for the DB-write half of
    this command (the Discord-reconcile half already had its own `except
    Exception` since round 4). Reproduced live before this fix: patching
    `league_service.assign_manager` to raise a non-domain exception and
    driving this callback produced `deferred=True`, zero `response`/
    `followup` calls, exception escaping uncaught entirely."""
    pool = await get_pool()
    try:
        league, team = await _make_league_and_team(pool)
        try:
            guild = _FakeGuild()
            interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            cog = CommissionerCommands(bot=None)
            manager = _fake_member(_MANAGER_ID)

            with patch.object(
                league_service,
                "assign_manager",
                side_effect=ConnectionResetError("connection reset by peer"),
            ):
                await cog.assign_manager.callback(cog, interaction, team.code, manager)

            # No manager was actually assigned -- the DB write itself
            # raised, so nothing committed.
            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id is None

            assert interaction.response.deferred is True
            assert len(interaction.followup.sent_messages) == 1
            message = interaction.followup.sent_messages[0].lower()
            assert "went wrong" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_remove_manager_reports_an_honest_failure_on_a_non_domain_db_exception():
    """`/remove-manager`'s twin of the test directly above -- see that
    test's docstring for the shared repro/rationale."""
    pool = await get_pool()
    try:
        league, team = await _make_league_and_team(pool)
        try:
            guild = _FakeGuild()
            cog = CommissionerCommands(bot=None)
            manager = _fake_member(_MANAGER_ID)
            assign_interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            await cog.assign_manager.callback(cog, assign_interaction, team.code, manager)

            remove_interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            with patch.object(
                league_service,
                "remove_manager",
                side_effect=ConnectionResetError("connection reset by peer"),
            ):
                await cog.remove_manager.callback(cog, remove_interaction, team.code)

            # The manager assignment from before this call is untouched --
            # the removal's own DB write raised before it could commit.
            refetched = await team_repo.get_team(pool, team.id)
            assert refetched is not None
            assert refetched.manager_user_id == _MANAGER_ID

            assert remove_interaction.response.deferred is True
            assert len(remove_interaction.followup.sent_messages) == 1
            message = remove_interaction.followup.sent_messages[0].lower()
            assert "went wrong" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_setup_reports_an_honest_failure_on_a_non_http_exception():
    """§42.1's Blocking Issue 2: `reconcile_league_channels`'s per-purpose
    guard (and `/setup`'s own former wrapper) only caught `discord.
    HTTPException` -- a bare `ConnectionResetError` (the shape `discord.
    py` itself raises after exhausting its own retry budget on a real
    network fault, not an `HTTPException` subclass) escaped uncaught,
    leaving `/setup` deferred forever with zero followups. This is the
    same repro as `test_setup_reports_an_honest_failure_when_channel_
    reconcile_fails` above, but for the exception class the round-2 fix's
    guard did NOT cover."""
    pool = await get_pool()
    try:
        league, _team = await _make_league_and_team(pool)
        try:
            guild = _ChannelCreateRaisesConnectionResetGuild()
            interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            cog = CommissionerCommands(bot=None)

            await cog.setup.callback(cog, interaction)

            assert interaction.response.deferred is True
            assert len(interaction.followup.sent_messages) == 1
            message = interaction.followup.sent_messages[0].lower()
            assert "failed" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_setup_reports_an_honest_failure_on_a_db_write_error_inside_the_reconcile():
    """§42.1's Blocking Issue 2, the other named failure mode: `_reconcile_
    one_league_channel` writes to Postgres (`channel_repo.create_league_
    channel`) inside the same try block `reconcile_league_channels`
    guards -- an asyncpg error there is not a `discord.HTTPException`
    either, and is the LIKELIEST real failure mode named by the critic
    (a DB write, not a Discord API call, is what's actually inside this
    loop alongside the Discord call). Patches the real repo function
    `dba2.bot.channels` calls to raise a plain DB-shaped exception and
    proves `/setup` still sends exactly one honest followup rather than
    hanging."""
    pool = await get_pool()
    try:
        league, _team = await _make_league_and_team(pool)
        try:
            guild = _FakeGuild()
            interaction = _FakeInteraction(
                guild=guild, guild_id=league.discord_guild_id, user_id=_COMMISSIONER_ID
            )
            cog = CommissionerCommands(bot=None)

            with patch.object(
                bot_channels.channel_repo,
                "create_league_channel",
                side_effect=RuntimeError("connection pool exhausted"),
            ):
                await cog.setup.callback(cog, interaction)

            assert interaction.response.deferred is True
            assert len(interaction.followup.sent_messages) == 1
            message = interaction.followup.sent_messages[0].lower()
            assert "failed" in message
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
    finally:
        await close_pool()
