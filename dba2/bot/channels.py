"""Idempotent channel/permission reconciliation (Decision P5-13, §6).

docs/design/phase-5-discord-presentation.md §6.1 (the league-wide
`wire`/`standings`/`games` channels), §6.2 (per-team private GM channels), §13.4
("Discord state drifts and the bot is not the only actor in the
guild.").

**Decision P5-13, restated because it is this module's entire reason to
exist as a function rather than a one-shot script:** channel setup is an
idempotent reconcile, never a one-shot create -- safe to call on every
startup, after every manager assignment/removal, and from a `/setup`
command. **Decision P5-14, this module's other governing rule:** Discord
is never written to inside a database transaction. Every function here
either does ONLY Discord I/O plus a single non-transactional registry
write, or is called by a `bot/`-side handler strictly AFTER a services-
layer transaction (e.g. `league_service.assign_manager`) has already
committed -- never from inside one.

Every public function here is type-hinted against the real `discord.py`
`Guild`/`TextChannel` types, but calls only the small, documented subset
of their API a unit test's fake double can implement without a live
gateway connection (`create_text_channel`, `get_channel`, `get_member`,
`get_role`, `fetch_member`, `set_permissions`, `edit`, `.overwrites`,
`.default_role`) -- Python does not enforce type hints at runtime, so
`tests/integration/test_bot_channels.py`'s fakes satisfy these functions
by duck typing alone. This is disclosed rather than presented as
equivalent to a real-guild test: it proves the reconcile's own DECISION
LOGIC (create vs. reuse vs. reactivate vs. archive, given a guild's
observable state), not that a real Discord gateway accepts the exact
calls made -- that is what §11 step 12/C-14's real-guild smoke test is
for, and it is not possible in this environment (no `DISCORD_TOKEN`, no
test guild -- see the session report this module's own commit
references). **`set_permissions`'s real argument-TYPE contract (`Member`/
`Role` only, `ValueError` on anything else) is a narrower claim than
duck typing alone can prove** -- `test_bot_channels.py`'s own fake
enforces that contract explicitly (Blocking Issue 2's own test-harness
fix, Stage C critic round) rather than accepting whatever type the
production code happens to pass, so a regression back to a bare `discord.
Object` target would fail the test suite, not just a real guild.
"""
from __future__ import annotations

import discord

from dba2.core.logging import get_logger
from dba2.data.db import Executor
from dba2.data.repositories import channel_repo
from dba2.domain.channel import ChannelPurpose

log = get_logger(__name__)

_LEAGUE_CHANNEL_NAMES: dict[ChannelPurpose, str] = {
    ChannelPurpose.WIRE: "league-wire",
    ChannelPurpose.STANDINGS: "standings",
    ChannelPurpose.GAMES: "games",
}


# ---------------------------------------------------------------------------
# League-wide channels (§6.1)
# ---------------------------------------------------------------------------


async def reconcile_league_channels(
    guild: discord.Guild, ex: Executor, *, league_id: int
) -> dict[ChannelPurpose, discord.TextChannel]:
    """Ensures every `ChannelPurpose` has a real, resolvable channel in
    `guild` and that `league_channels` points at it. Calling this twice
    in a row with nothing changed creates nothing new (`get_channel`
    resolves the already-registered id and returns immediately).

    **A single purpose's failure is caught and logged per purpose, not
    left to abort the whole reconcile** (Blocking Issue A, Stage C critic
    round: `/setup` previously called this function completely unguarded,
    so an uncaught exception left its interaction deferred forever with
    zero response). **Widened from `except discord.HTTPException` to a
    bare `except Exception` (§42.1's Blocking Issue 2, a critic re-
    verification of that same fix round): `_reconcile_one_league_channel`
    also writes to Postgres inside this same try block (`channel_repo.
    create_league_channel`/`set_league_channel_id`), and an asyncpg error
    there is NOT a `discord.HTTPException` -- it propagated straight
    through uncaught, the identical "deferred forever" failure mode
    Blocking Issue A fixed, just for a different exception type. A bare
    `ConnectionResetError`/`OSError`/`asyncio.TimeoutError` `discord.py`
    can raise after exhausting its own retry budget on a real network
    fault is the same shape of gap. This matches the already-accepted
    pattern `commissioner.py::setup`'s own standings-sync half already
    uses for the identical reason (Discord I/O can fail in more ways than
    `HTTPException`), rather than inventing a third, narrower scope.** The
    caller compares `len(result)` against `len(ChannelPurpose)` to tell a
    total failure (nothing came back) from a partial one (one purpose
    succeeded, one didn't) -- the same "always tell the user the truth
    about which half succeeded" contract `commissioner.py`'s `_reconcile_
    gm_channel_after_commit` already keeps for the per-team reconcile."""
    result: dict[ChannelPurpose, discord.TextChannel] = {}
    for purpose in ChannelPurpose:
        try:
            result[purpose] = await _reconcile_one_league_channel(
                guild, ex, league_id=league_id, purpose=purpose
            )
        except Exception:
            log.exception(
                "league channel reconcile failed for purpose %s in league %s",
                purpose,
                league_id,
            )
    return result


async def _reconcile_one_league_channel(
    guild: discord.Guild, ex: Executor, *, league_id: int, purpose: ChannelPurpose
) -> discord.TextChannel:
    row = await channel_repo.get_league_channel(ex, league_id=league_id, purpose=purpose)
    channel = guild.get_channel(row.channel_id) if row is not None else None
    if channel is not None:
        return channel

    # No row, or the row's channel_id no longer resolves (§13.4: someone
    # deleted the channel) -- create fresh either way.
    created = await guild.create_text_channel(_LEAGUE_CHANNEL_NAMES[purpose])
    if row is None:
        await channel_repo.create_league_channel(
            ex, league_id=league_id, purpose=purpose, channel_id=created.id
        )
    else:
        await channel_repo.set_league_channel_id(
            ex, league_id=league_id, purpose=purpose, channel_id=created.id
        )
        if row.message_id is not None:
            # The pinned standings message died with its channel -- the
            # stored id is now a dangling reference to nothing.
            # `sync_standings_message` (dba2/bot/surfaces.py) reads this
            # NULL and sends a fresh message next time it runs.
            await channel_repo.set_league_channel_message_id(
                ex, league_id=league_id, purpose=purpose, message_id=None
            )
    return created


# ---------------------------------------------------------------------------
# Per-team GM channels (§6.2, Decision P5-41)
# ---------------------------------------------------------------------------


def _is_role_target(target: object) -> bool:
    """True for anything real `discord.py` `set_permissions`/`.overwrites`
    would treat as role-shaped. A genuine `discord.Role` is the common
    case; `GuildChannel.overwrites` (verified against `discord.py` 2.7.1
    source) substitutes a `discord.Object(id=.., type=Role)` stand-in
    when the role itself isn't cache-resident, and a bare `isinstance(
    target, discord.Role)` check -- this function's own predecessor --
    does not recognize that stand-in as role-shaped, which let the
    "leave human-admin role overwrites untouched" rule in `_sync_
    overwrites` below be defeated by a cache miss and clear a role
    overwrite it was specifically supposed to preserve (Blocking Issue
    2b, Stage C critic round)."""
    if isinstance(target, discord.Role):
        return True
    return isinstance(target, discord.Object) and target.type is discord.Role


async def _resolve_permission_target(
    guild: discord.Guild, target: discord.Member | discord.Role | discord.Object
) -> discord.Member | discord.Role | None:
    """Real `abc.GuildChannel.set_permissions` (verified against
    `discord.py` 2.7.1 source) requires a genuine `Member` or `Role`
    instance and raises `ValueError` for a bare `discord.Object` -- it
    only appeared to work in this codebase because `Guild._create_
    channel` (the fresh-create path) is the one real API that DOES
    accept `Object` for its initial overwrites; every reuse/reactivate/
    archive path calls `set_permissions` directly and would have raised
    on the very first real guild (Blocking Issue 2). `channel.overwrites`
    hands back a typed `Object` stand-in when a target isn't cache-
    resident; this re-resolves it into a real object before any `set_
    permissions` call. Returns `None` if the target cannot be resolved
    (the member is no longer in the guild -- Discord itself deletes a
    member's overwrites when they leave, so this is a rare transient-
    cache-miss case, not the steady state) -- the caller logs and skips
    rather than crashing the whole reconcile over one stale entry."""
    if isinstance(target, (discord.Member, discord.Role)):
        return target
    if not isinstance(target, discord.Object):
        return None
    if target.type is discord.Role:
        return guild.get_role(target.id)
    member = guild.get_member(target.id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(target.id)
    except discord.HTTPException:
        return None


async def _require_member(guild: discord.Guild, user_id: int) -> discord.Member:
    """A real `discord.Member`, cache-first, falling back to a `fetch_
    member` HTTP round trip -- never a bare `discord.Object`, which real
    `set_permissions` rejects (see `_resolve_permission_target`'s own
    docstring). Raises `discord.NotFound` (a `discord.HTTPException`
    subclass, already caught by every caller of `reconcile_gm_channel`)
    if `user_id` is not actually a member of `guild` -- correct: this
    reconcile cannot grant channel access to someone who isn't in the
    server."""
    member = guild.get_member(user_id)
    if member is not None:
        return member
    return await guild.fetch_member(user_id)


async def _gm_channel_overwrites(
    guild: discord.Guild, *, manager_user_id: int, commissioner_user_id: int
) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
    """§6.2's "private GM channels": the manager and the commissioner see
    it, nobody else. JC-6 (still an open judgment call, §21) is whether a
    non-acting spectator should be able to READ it -- this is the
    conservative reading of Pillar 8's own "private" wording; widening it
    is one overwrite entry, not a redesign.

    Targets are real `discord.Member` instances (`_require_member`), not
    `discord.Object` -- see `_resolve_permission_target`'s docstring for
    why a bare `Object` only worked on the fresh-create path."""
    manager = await _require_member(guild, manager_user_id)
    overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        manager: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if commissioner_user_id != manager_user_id:
        commissioner = await _require_member(guild, commissioner_user_id)
        overwrites[commissioner] = discord.PermissionOverwrite(view_channel=True)
    return overwrites


async def _sync_overwrites(
    guild: discord.Guild,
    channel: discord.TextChannel,
    desired: dict[discord.Role | discord.Member, discord.PermissionOverwrite],
) -> None:
    """Applies `desired` and clears any stale MEMBER-targeted overwrite
    this reconcile itself owns but no longer wants (a former manager's
    leftover `view_channel=True`) -- §13.4's "reconcile gets written as
    a startup-only function and then not called at the moments that
    matter" risk, addressed by making every call re-assert the full
    desired set rather than only adding to it. Role-targeted overwrites
    are left untouched entirely (`_is_role_target`, including `@everyone`
    if it is not already in `desired` -- it always is here, so this only
    matters for a future caller) -- this reconcile only ever creates
    role overwrites for `@everyone`, and clearing a role overwrite it
    does not recognize would silently discard something a human admin
    set up on purpose."""
    desired_ids = {getattr(target, "id", None) for target in desired}
    for target in list(channel.overwrites):
        if _is_role_target(target):
            continue
        if getattr(target, "id", None) in desired_ids:
            continue
        resolved = await _resolve_permission_target(guild, target)
        if resolved is None:
            log.warning(
                "could not resolve stale overwrite target %s on channel %s -- skipping",
                getattr(target, "id", None),
                channel.id,
            )
            continue
        await channel.set_permissions(resolved, overwrite=None)
    for target, overwrite in desired.items():
        await channel.set_permissions(target, overwrite=overwrite)


async def _deny_all_member_overwrites(guild: discord.Guild, channel: discord.TextChannel) -> None:
    """The archive-on-removal step: strip every member-targeted `view_
    channel` grant so nobody keeps access to a channel whose manager was
    just removed, while leaving the channel itself (and its history)
    intact -- P5-41's "archive, never delete." Role-targeted overwrites
    (`_is_role_target`) are left untouched, same reasoning as `_sync_
    overwrites`."""
    for target in list(channel.overwrites):
        if _is_role_target(target):
            continue
        resolved = await _resolve_permission_target(guild, target)
        if resolved is None:
            log.warning(
                "could not resolve stale overwrite target %s on channel %s -- skipping",
                getattr(target, "id", None),
                channel.id,
            )
            continue
        await channel.set_permissions(resolved, overwrite=None)


async def reconcile_gm_channel(
    guild: discord.Guild,
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    team_code: str,
    manager_user_id: int | None,
    commissioner_user_id: int,
) -> discord.TextChannel | None:
    """The GM-channel half of P5-13's reconcile, run by the `bot/`-side
    caller of `league_service.assign_manager`/`remove_manager` strictly
    AFTER that function's own transaction has committed (Decision P5-14
    -- this function is never called from inside one).

    `manager_user_id=None` archives the team's active channel, if any,
    and returns it (or `None` if the team never had one). A real
    `manager_user_id` ensures an active channel exists with the correct
    permission overwrite, reactivating the team's most recent archived
    channel rather than creating a second one when its channel still
    resolves (P5-41: "archived ... recoverable if the same person comes
    back"), and creating fresh only when nothing usable is found --
    covering both idempotence obligations C-13 names: calling this twice
    for the same assignment creates nothing new ("double-assign"), and a
    manually-deleted channel is re-created and the registry re-pointed
    rather than left dangling.
    """
    active = await channel_repo.get_active_gm_channel(ex, team_id)

    if manager_user_id is None:
        return await _archive_gm_channel(guild, ex, active=active, team_code=team_code)

    overwrites = await _gm_channel_overwrites(
        guild, manager_user_id=manager_user_id, commissioner_user_id=commissioner_user_id
    )
    channel_name = f"gm-{team_code.lower()}"

    if active is not None:
        channel = guild.get_channel(active.channel_id)
        if channel is not None:
            await _sync_overwrites(guild, channel, overwrites)
            return channel
        # Row says active but the channel is gone (§13.4). Re-create and
        # re-point the SAME row rather than leaving a dangling one.
        created = await guild.create_text_channel(channel_name, overwrites=overwrites)
        await channel_repo.reactivate_gm_channel(
            ex, channel_row_id=active.id, channel_id=created.id
        )
        return created

    most_recent = await channel_repo.get_most_recent_gm_channel(ex, team_id)
    if most_recent is not None:
        channel = guild.get_channel(most_recent.channel_id)
        if channel is not None:
            await _sync_overwrites(guild, channel, overwrites)
            try:
                await channel.edit(name=channel_name)
            except discord.HTTPException:
                log.warning("could not rename reactivated GM channel %s", channel.id)
            await channel_repo.reactivate_gm_channel(
                ex, channel_row_id=most_recent.id, channel_id=channel.id
            )
            return channel
        created = await guild.create_text_channel(channel_name, overwrites=overwrites)
        await channel_repo.reactivate_gm_channel(
            ex, channel_row_id=most_recent.id, channel_id=created.id
        )
        return created

    created = await guild.create_text_channel(channel_name, overwrites=overwrites)
    await channel_repo.create_gm_channel(
        ex, league_id=league_id, team_id=team_id, channel_id=created.id
    )
    return created


async def _archive_gm_channel(guild, ex: Executor, *, active, team_code: str):
    if active is None:
        return None
    channel = guild.get_channel(active.channel_id)
    if channel is not None:
        await _deny_all_member_overwrites(guild, channel)
        try:
            await channel.edit(name=f"archived-gm-{team_code.lower()}")
        except discord.HTTPException:
            log.warning("could not rename archived GM channel %s", active.channel_id)
    await channel_repo.archive_gm_channel(ex, channel_row_id=active.id)
    return channel
