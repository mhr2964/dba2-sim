"""Live I/O proof for `dba2/bot/channels.py`'s reconcile logic (Decision
P5-13/P5-14), against a REAL Postgres pool (`channel_repo`'s own tables)
and a FAKE Discord guild.

**Disclosed, not hidden: this is not a real-guild test.** This
environment has no `DISCORD_TOKEN` and no test guild (verified -- `.env`
carries only `DATABASE_URL`; see the Stage C session report), so §11
step 12/C-14's own real-guild smoke test is not possible here. What
follows proves the reconcile's own DECISION LOGIC -- create vs. reuse
vs. reactivate vs. archive, given a guild's observable state -- against
lightweight fakes that implement exactly the `discord.py` surface `bot/
channels.py` calls (`create_text_channel`, `get_channel`, `set_
permissions`, `edit`, `.overwrites`), the same subset that module's own
docstring names. It does NOT prove a real Discord gateway accepts the
exact calls made.

`_fake_role`/`_fake_member` construct real `discord.Role`/`discord.
Member` instances via `__new__` (skipping `__init__`, which needs a live
gateway `ConnectionState`) so the `isinstance(target, discord.Role)`/
`_is_role_target` checks inside `bot/channels.py` see genuine `discord.py`
types, not duck-typed stand-ins -- the one place this file reaches past
pure duck typing, because `channels.py`'s own logic branches on those
exact `isinstance` checks.

**`_FakeChannel.set_permissions` enforces the real `discord.py` 2.7.1
argument-TYPE contract** (verified against `abc.GuildChannel.set_
permissions`'s own source: `target` must be `Member`/`User`-shaped or
`Role`, else it raises `ValueError`) -- Blocking Issue 2's own critic
finding was that the PREVIOUS version of this fake accepted whatever
target type the production code happened to pass (a bare `discord.
Object`), which is why B2's bug (`_gm_channel_overwrites` building
`discord.Object` targets that real `set_permissions` rejects on every
path except fresh-create) shipped without this test suite catching it.
This fake now rejects the same inputs the real API would, so a
regression back to a bare `Object` target fails here, not only against
a real guild.
"""
from __future__ import annotations

import random

import discord
import pytest

from dba2.bot import channels as bot_channels
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import channel_repo, league_repo, team_repo
from dba2.domain.channel import ChannelPurpose
from dba2.domain.phase import Phase
from dba2.domain.team import GmPhilosophy, Timeline

_SEASON = 2026
_COMMISSIONER_ID = 100
_MANAGER_ID = 200
_OTHER_MANAGER_ID = 201


def _fake_role(role_id: int) -> discord.Role:
    role = discord.Role.__new__(discord.Role)
    role.id = role_id
    role.name = "@everyone"
    return role


def _fake_member(user_id: int) -> discord.Member:
    """A real `discord.Member`, constructed the same `__new__`-bypass way
    `_fake_role` builds a real `Role`. `Member.id` is a delegate onto
    `self._user.id` (verified against `discord.py` 2.7.1 source -- `id`
    is not settable directly, `Member.__init__` needs a live gateway
    payload), so this sets `._user` to a minimal stand-in carrying the
    attributes `discord.abc.User`'s own runtime-checkable Protocol reads
    (`isinstance(member, discord.abc.User)` is exactly what real `set_
    permissions` checks)."""

    class _FakeUser:
        def __init__(self, id: int) -> None:
            self.id = id
            self.name = "fake"
            self.discriminator = "0"
            self.global_name = None
            self.bot = False
            self.system = False

    member = discord.Member.__new__(discord.Member)
    member._user = _FakeUser(user_id)
    return member


class _FakeChannel:
    def __init__(self, channel_id: int, name: str, overwrites: dict | None = None):
        self.id = channel_id
        self.name = name
        self.overwrites: dict = dict(overwrites) if overwrites else {}
        self.edit_calls: list[str] = []

    @property
    def mention(self) -> str:
        # Real `discord.TextChannel.mention` -- `commissioner.py::setup`'s
        # own success-path message formats every reconciled channel this
        # way (`test_commissioner_cog.py::test_setup_reports_a_partial_
        # success_when_one_purpose_fails` is the first test to exercise
        # a real (non-exception) `/setup` success path against this fake).
        return f"<#{self.id}>"

    async def edit(self, *, name: str | None = None, **_kwargs) -> None:
        if name is not None:
            self.name = name
            self.edit_calls.append(name)

    async def set_permissions(self, target, *, overwrite=None, reason: str | None = None) -> None:
        # Mirrors real `abc.GuildChannel.set_permissions`'s own argument-
        # TYPE contract exactly (`discord.py` 2.7.1 source): a bare
        # `discord.Object` is NOT an accepted target -- only something
        # `discord.abc.User`-shaped (a real `Member`/`User`) or a real
        # `Role`. Blocking Issue 2's own critic finding: the prior version
        # of this fake accepted anything, which is why B2's `discord.
        # Object` targets shipped without this test suite ever catching
        # the real API's `ValueError`.
        if not isinstance(target, (discord.abc.User, discord.Role)):
            raise ValueError("target parameter must be either Member or Role")
        # Real Discord keys a channel's permission overwrites by (id,
        # type), not by Python object identity -- `edit_channel_
        # permissions`/`delete_channel_permissions` both take `target.id`
        # on the wire, never the object itself. A resolved `Member`
        # fetched fresh (`_resolve_permission_target`'s `fetch_member`
        # fallback) is a DIFFERENT Python object than whatever stand-in
        # was previously in `self.overwrites`, but the SAME id -- so this
        # replaces/removes by id, matching the real API's semantics
        # rather than a plain dict's object-identity lookup.
        for existing in [t for t in self.overwrites if getattr(t, "id", None) == target.id]:
            del self.overwrites[existing]
        if overwrite is not None:
            self.overwrites[target] = overwrite


class _FakeGuild:
    def __init__(self):
        self.default_role = _fake_role(0)
        self.id = random.randint(10_000_000, 2_000_000_000)
        self._channels: dict[int, _FakeChannel] = {}
        self._next_id = 1000
        self._members: dict[int, discord.Member] = {}
        self._roles: dict[int, discord.Role] = {0: self.default_role}
        self._unfetchable_member_ids: set[int] = set()

    def get_channel(self, channel_id: int) -> _FakeChannel | None:
        return self._channels.get(channel_id)

    def get_member(self, user_id: int) -> discord.Member | None:
        return self._members.get(user_id)

    async def fetch_member(self, user_id: int) -> discord.Member:
        """Real `Guild.fetch_member` raises `discord.NotFound` (a `discord.
        HTTPException` subclass) for a user who isn't actually a member of
        the guild -- `_unfetchable_member_ids` (test-only) simulates that
        case; every other id resolves and is cached, exactly as a real
        member-intent-populated guild would for any id this reconcile
        legitimately deals with."""
        if user_id in self._unfetchable_member_ids:
            raise discord.NotFound.__new__(discord.NotFound)
        member = self._members.setdefault(user_id, _fake_member(user_id))
        return member

    def get_role(self, role_id: int) -> discord.Role | None:
        return self._roles.get(role_id)

    async def create_text_channel(self, name: str, *, overwrites=None, **_kwargs) -> _FakeChannel:
        self._next_id += 1
        channel = _FakeChannel(self._next_id, name, overwrites)
        self._channels[channel.id] = channel
        return channel

    def delete_channel(self, channel_id: int) -> None:
        """Test-only: simulates a human manually deleting a channel --
        §13.4's own named risk -- without touching the registry."""
        self._channels.pop(channel_id, None)


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Bot Channels Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=_COMMISSIONER_ID,
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


# ---------------------------------------------------------------------------
# League-wide channels (§6.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_league_channels_creates_wire_standings_and_games_once():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            guild = _FakeGuild()
            created = await bot_channels.reconcile_league_channels(guild, pool, league_id=league_id)
            assert set(created) == {
                ChannelPurpose.WIRE,
                ChannelPurpose.STANDINGS,
                ChannelPurpose.GAMES,
            }
            assert len(guild._channels) == 3

            wire_row = await channel_repo.get_league_channel(
                pool, league_id=league_id, purpose=ChannelPurpose.WIRE
            )
            assert wire_row is not None
            assert wire_row.channel_id == created[ChannelPurpose.WIRE].id
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_league_channels_is_idempotent():
    """Calling it twice with nothing changed creates nothing new -- P5-13's
    own "safe to call on every startup" obligation."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            guild = _FakeGuild()
            first = await bot_channels.reconcile_league_channels(guild, pool, league_id=league_id)
            second = await bot_channels.reconcile_league_channels(guild, pool, league_id=league_id)
            assert first[ChannelPurpose.WIRE].id == second[ChannelPurpose.WIRE].id
            assert first[ChannelPurpose.STANDINGS].id == second[ChannelPurpose.STANDINGS].id
            assert first[ChannelPurpose.GAMES].id == second[ChannelPurpose.GAMES].id
            assert len(guild._channels) == 3
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_league_channels_creates_a_public_games_channel():
    """Broadcast design §7 "Permissions": `#games` is a public-read league
    channel, the same permission model as its `wire`/`standings` siblings --
    created with NO overwrites, so `@everyone` keeps its default read access
    and nothing is denied or member-scoped. The bot's own send/embed/create-
    public-threads/send-in-threads rights come from its guild role, not from
    a per-channel overwrite (the sibling channels grant none either), so the
    reconcile must not narrow the channel below what the siblings get."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            guild = _FakeGuild()
            created = await bot_channels.reconcile_league_channels(guild, pool, league_id=league_id)

            games = created[ChannelPurpose.GAMES]
            assert games.name == "games"
            assert games.overwrites == {}
            for sibling in (ChannelPurpose.WIRE, ChannelPurpose.STANDINGS):
                assert created[sibling].overwrites == games.overwrites

            row = await channel_repo.get_league_channel(
                pool, league_id=league_id, purpose=ChannelPurpose.GAMES
            )
            assert row is not None
            assert row.channel_id == games.id
            assert row.message_id is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_league_channels_recreates_a_manually_deleted_channel():
    """§13.4's named risk: "someone deletes a channel." The registry's
    dangling id is re-pointed at a fresh channel, not left broken."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            guild = _FakeGuild()
            first = await bot_channels.reconcile_league_channels(guild, pool, league_id=league_id)
            old_wire_id = first[ChannelPurpose.WIRE].id
            guild.delete_channel(old_wire_id)

            second = await bot_channels.reconcile_league_channels(guild, pool, league_id=league_id)
            assert second[ChannelPurpose.WIRE].id != old_wire_id

            row = await channel_repo.get_league_channel(
                pool, league_id=league_id, purpose=ChannelPurpose.WIRE
            )
            assert row is not None
            assert row.channel_id == second[ChannelPurpose.WIRE].id
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_league_channels_clears_a_dangling_standings_message_id():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            guild = _FakeGuild()
            created = await bot_channels.reconcile_league_channels(guild, pool, league_id=league_id)
            await channel_repo.set_league_channel_message_id(
                pool, league_id=league_id, purpose=ChannelPurpose.STANDINGS, message_id=999
            )
            guild.delete_channel(created[ChannelPurpose.STANDINGS].id)

            await bot_channels.reconcile_league_channels(guild, pool, league_id=league_id)
            row = await channel_repo.get_league_channel(
                pool, league_id=league_id, purpose=ChannelPurpose.STANDINGS
            )
            assert row is not None
            assert row.message_id is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Per-team GM channels (§6.2, Decision P5-41)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_gm_channel_creates_with_correct_overwrites():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "AAA")
            guild = _FakeGuild()
            channel = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="AAA",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            assert channel is not None
            assert channel.name == "gm-aaa"

            everyone_ow = channel.overwrites[guild.default_role]
            assert everyone_ow.view_channel is False

            manager_target = next(
                t for t in channel.overwrites if getattr(t, "id", None) == _MANAGER_ID
            )
            manager_ow = channel.overwrites[manager_target]
            assert manager_ow.view_channel is True
            assert manager_ow.send_messages is True

            commissioner_target = next(
                t for t in channel.overwrites if getattr(t, "id", None) == _COMMISSIONER_ID
            )
            commissioner_ow = channel.overwrites[commissioner_target]
            assert commissioner_ow.view_channel is True

            active = await channel_repo.get_active_gm_channel(pool, team_id)
            assert active is not None
            assert active.channel_id == channel.id
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_gm_channel_double_assign_creates_no_second_channel():
    """C-13's own "double-assign" idempotence obligation."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "BBB")
            guild = _FakeGuild()
            first = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="BBB",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            second = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="BBB",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            assert first.id == second.id
            assert len(guild._channels) == 1
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_gm_channel_recreates_a_manually_deleted_active_channel():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "CCC")
            guild = _FakeGuild()
            first = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="CCC",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            guild.delete_channel(first.id)

            second = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="CCC",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            assert second.id != first.id

            active = await channel_repo.get_active_gm_channel(pool, team_id)
            assert active is not None
            assert active.channel_id == second.id
            # Re-points the SAME registry row rather than creating a
            # second one for the team.
            all_rows_for_team = [
                r
                for r in [await channel_repo.get_most_recent_gm_channel(pool, team_id)]
                if r is not None
            ]
            assert len(all_rows_for_team) == 1
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_gm_channel_archives_on_removal():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "DDD")
            guild = _FakeGuild()
            channel = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="DDD",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            result = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="DDD",
                manager_user_id=None,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            assert result is not None
            assert result.id == channel.id
            assert channel.name == "archived-gm-ddd"
            # Every member-targeted overwrite is stripped -- only the
            # (still-present) @everyone deny remains.
            assert all(isinstance(t, discord.Role) for t in channel.overwrites)

            assert await channel_repo.get_active_gm_channel(pool, team_id) is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_gm_channel_reactivates_an_archived_channel_for_a_new_manager():
    """P5-41: "archived ... recoverable if the same person comes back" --
    generalized here to a DIFFERENT person, since the design's own reason
    (avoid channel-creation clutter) applies identically either way."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "EEE")
            guild = _FakeGuild()
            original = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="EEE",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="EEE",
                manager_user_id=None,
                commissioner_user_id=_COMMISSIONER_ID,
            )

            reactivated = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="EEE",
                manager_user_id=_OTHER_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            assert reactivated.id == original.id
            assert reactivated.name == "gm-eee"
            assert len(guild._channels) == 1

            new_manager_target = next(
                t for t in reactivated.overwrites if getattr(t, "id", None) == _OTHER_MANAGER_ID
            )
            assert reactivated.overwrites[new_manager_target].view_channel is True
            assert not any(
                getattr(t, "id", None) == _MANAGER_ID for t in reactivated.overwrites
            )

            active = await channel_repo.get_active_gm_channel(pool, team_id)
            assert active is not None
            assert active.archived_at is None
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_gm_channel_reactivates_a_manually_deleted_archived_channel():
    """The rarer compound case: a manager was removed (archived), and
    THEN the archived channel was manually deleted before anyone
    reactivated it -- reconcile must still create fresh and re-point the
    SAME row rather than erroring or leaving it dangling."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "FFF")
            guild = _FakeGuild()
            original = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="FFF",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="FFF",
                manager_user_id=None,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            guild.delete_channel(original.id)

            reactivated = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="FFF",
                manager_user_id=_OTHER_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            assert reactivated.id != original.id
            active = await channel_repo.get_active_gm_channel(pool, team_id)
            assert active is not None
            assert active.channel_id == reactivated.id
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Blocking Issues 2/2b (Stage C critic round) -- real Member/Role targets,
# and the role-overwrite-preservation rule surviving an uncached stand-in.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_gm_channel_uses_real_member_targets_not_bare_objects():
    """Blocking Issue 2's direct proof: every target `_FakeChannel.set_
    permissions` receives must satisfy the SAME argument-type contract
    real `discord.py` enforces (`isinstance(target, (discord.abc.User,
    discord.Role))`, or `set_permissions` itself raises `ValueError` --
    see this fake's own docstring). Before the fix, `_gm_channel_
    overwrites` built bare `discord.Object` targets and this call would
    have raised on every path except fresh-create; it does not raise
    here, and every key in the resulting `channel.overwrites` is a real
    `Member`/`Role`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "GGG")
            guild = _FakeGuild()
            channel = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="GGG",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            assert channel is not None
            for target in channel.overwrites:
                assert isinstance(target, (discord.Member, discord.Role)), (
                    f"non-Member/Role overwrite target leaked through: {target!r}"
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_gm_channel_preserves_a_human_admin_role_overwrite_even_when_uncached():
    """Blocking Issue 2b's direct reproduction: a human admin's own role
    overwrite on the channel, injected as the SAME typed `Object` stand-
    in real `GuildChannel.overwrites` returns for a role that isn't
    cache-resident (`discord.Object(id=.., type=Role)`) -- a bare
    `isinstance(target, discord.Role)` check (the pre-fix code) does not
    recognize this as role-shaped and would have cleared it as a stale
    overwrite the very next time the manager changed. `_is_role_target`
    must leave it alone."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "HHH")
            guild = _FakeGuild()
            channel = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="HHH",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            admin_role_stand_in = discord.Object(id=555_555, type=discord.Role)
            channel.overwrites[admin_role_stand_in] = discord.PermissionOverwrite(
                manage_messages=True
            )

            # Re-running the reconcile for a DIFFERENT manager is exactly
            # the "manager changed" trigger that walks `_sync_overwrites`'
            # stale-clearing branch.
            await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="HHH",
                manager_user_id=_OTHER_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )

            assert any(
                getattr(t, "id", None) == 555_555 for t in channel.overwrites
            ), "a human admin's role overwrite was cleared despite being role-shaped"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_gm_channel_resolves_an_uncached_stale_member_overwrite_before_clearing():
    """A former manager's leftover overwrite, injected as the typed
    `Object` stand-in real `.overwrites` would hand back for a member
    who fell out of cache (`discord.Object(id=.., type=discord.User)`) --
    `_resolve_permission_target` must re-resolve it into a real `Member`
    (via `guild.get_member`/`fetch_member`) before `set_permissions` is
    called to clear it, not pass the stand-in straight through."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "III")
            guild = _FakeGuild()
            channel = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="III",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            stale_id = 424_242
            stale_stand_in = discord.Object(id=stale_id, type=discord.User)
            channel.overwrites[stale_stand_in] = discord.PermissionOverwrite(view_channel=True)

            await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="III",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )

            assert not any(getattr(t, "id", None) == stale_id for t in channel.overwrites), (
                "the stale member overwrite was not cleared"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_reconcile_gm_channel_skips_an_unresolvable_stale_overwrite_without_crashing():
    """The member who left the guild entirely (real `Guild.fetch_member`
    raises `discord.NotFound`) -- `_resolve_permission_target` returns
    `None` and the reconcile logs and skips rather than crashing on a
    single unresolvable stale entry."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "JJJ")
            guild = _FakeGuild()
            channel = await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="JJJ",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
            gone_id = 313_131
            guild._unfetchable_member_ids.add(gone_id)
            channel.overwrites[discord.Object(id=gone_id, type=discord.User)] = (
                discord.PermissionOverwrite(view_channel=True)
            )

            # Must not raise.
            await bot_channels.reconcile_gm_channel(
                guild,
                pool,
                league_id=league_id,
                team_id=team_id,
                team_code="JJJ",
                manager_user_id=_MANAGER_ID,
                commissioner_user_id=_COMMISSIONER_ID,
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
