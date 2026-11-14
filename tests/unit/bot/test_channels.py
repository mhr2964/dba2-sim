"""Pure-function proof for `dba2/bot/channels.py`'s Blocking Issue 2/2b
fixes (Stage C critic round) -- `_is_role_target`/`_resolve_permission_
target` need no Postgres pool and no fake guild double, so they get a
real unit test here rather than only being exercised indirectly through
`tests/integration/test_bot_channels.py`'s full reconcile flow.
"""
from __future__ import annotations

import discord

from dba2.bot.channels import _is_role_target, _resolve_permission_target


def _fake_role(role_id: int) -> discord.Role:
    role = discord.Role.__new__(discord.Role)
    role.id = role_id
    role.name = "some-role"
    return role


def _fake_member(user_id: int) -> discord.Member:
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


class _FakeGuild:
    def __init__(self):
        self._members: dict[int, discord.Member] = {}
        self._roles: dict[int, discord.Role] = {}
        self._unfetchable: set[int] = set()

    def get_member(self, user_id):
        return self._members.get(user_id)

    async def fetch_member(self, user_id):
        if user_id in self._unfetchable:
            raise discord.NotFound.__new__(discord.NotFound)
        return self._members.setdefault(user_id, _fake_member(user_id))

    def get_role(self, role_id):
        return self._roles.get(role_id)


def test_is_role_target_true_for_a_real_role():
    assert _is_role_target(_fake_role(1)) is True


def test_is_role_target_true_for_a_typed_object_stand_in():
    """Blocking Issue 2b's exact reproduction: `GuildChannel.overwrites`
    (real `discord.py` 2.7.1 source) substitutes `discord.Object(id=..,
    type=Role)` for a role that isn't cache-resident -- the bare
    `isinstance(target, discord.Role)` check this function replaces does
    NOT recognize that stand-in as role-shaped."""
    stand_in = discord.Object(id=42, type=discord.Role)
    assert _is_role_target(stand_in) is True


def test_is_role_target_false_for_an_untyped_object():
    """The pre-fix production bug, restated: a bare `discord.Object`
    (Blocking Issue 2's own target type, `type` defaulting to `Object`
    itself) is not role-shaped."""
    assert _is_role_target(discord.Object(id=42)) is False


def test_is_role_target_false_for_a_member():
    assert _is_role_target(_fake_member(7)) is False


async def test_resolve_permission_target_passes_through_a_real_member_or_role():
    guild = _FakeGuild()
    member = _fake_member(1)
    role = _fake_role(2)
    assert await _resolve_permission_target(guild, member) is member
    assert await _resolve_permission_target(guild, role) is role


async def test_resolve_permission_target_resolves_a_role_object_stand_in_via_get_role():
    guild = _FakeGuild()
    real_role = _fake_role(99)
    guild._roles[99] = real_role
    stand_in = discord.Object(id=99, type=discord.Role)
    resolved = await _resolve_permission_target(guild, stand_in)
    assert resolved is real_role


async def test_resolve_permission_target_resolves_a_cached_member_object_stand_in():
    guild = _FakeGuild()
    real_member = _fake_member(55)
    guild._members[55] = real_member
    stand_in = discord.Object(id=55, type=discord.User)
    resolved = await _resolve_permission_target(guild, stand_in)
    assert resolved is real_member


async def test_resolve_permission_target_falls_back_to_fetch_member_for_an_uncached_member():
    guild = _FakeGuild()
    stand_in = discord.Object(id=66, type=discord.User)
    resolved = await _resolve_permission_target(guild, stand_in)
    assert resolved is not None
    assert resolved.id == 66


async def test_resolve_permission_target_returns_none_when_the_member_left_the_guild():
    """Real `Guild.fetch_member` raises `discord.NotFound` for a user no
    longer in the guild -- `_resolve_permission_target` swallows that
    (any `discord.HTTPException`) and returns `None` so its caller can
    skip the stale overwrite instead of crashing the whole reconcile."""
    guild = _FakeGuild()
    guild._unfetchable.add(77)
    stand_in = discord.Object(id=77, type=discord.User)
    resolved = await _resolve_permission_target(guild, stand_in)
    assert resolved is None


async def test_resolve_permission_target_returns_none_for_a_non_object_non_member_non_role():
    guild = _FakeGuild()
    assert await _resolve_permission_target(guild, object()) is None
