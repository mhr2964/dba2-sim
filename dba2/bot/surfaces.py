"""The wire channel poster and the auto-updating standings message
(§11 step 11: "The wire channel (P5-15) and the auto-updating standings
message").

Both functions assume `bot/channels.py::reconcile_league_channels` has
already run for this league (so `league_channels` has a row for both
`ChannelPurpose.WIRE` and `ChannelPurpose.STANDINGS`) -- neither creates
a channel itself, per this module's own narrower job: posting content
into channels `channels.py` already reconciled, never channel lifecycle.

**What this module deliberately does NOT do: decide WHEN to run.**
Decision P5-15 says the wire channel posts "at the end of each media
pulse cycle" and the standings message stays "auto-updating" -- both are
real triggers the season orchestrator (§32, Stage E, unbuilt) owns, not
this Stage C module. `sync_standings_message` is reachable from the
`/setup` command today (`dba2/bot/cogs/commissioner.py`), matching
P5-13's own three named call sites for the channel reconcile itself
(startup, manager assignment, `/setup`) -- `/setup` is a reasonable
fourth trigger to expose now precisely because Stage E does not exist
yet to provide a better one.

**`post_wire_panels`/`post_new_wire_stories` have ZERO callers anywhere
in this codebase** (Stage C critic round nit -- a prior version of this
docstring claimed both were "reachable from `/setup`," which was never
true: `/setup` only ever wired up `sync_standings_message`). Disclosed
here rather than silently left to look load-bearing: both are real,
correct, unreachable scaffolding for whichever future commit builds the
orchestrator's own end-of-media-pulse-cycle trigger (§32, Stage E) --
`post_wire_panels` has its own unit test (`tests/unit/bot/test_
surfaces.py`) so it is at least proven correct in isolation, but neither
function is wired into anything a real Discord interaction can reach
today.

**Publication is per story (phase-5-stage-e-advance.md P5-99.s.3, F17).**
`post_new_wire_stories` posts the stories that have no `posted_at`, one
message each, and marks each story the moment its own send returns -- so an
interrupted publication resumes at the story it stopped at, and history
cannot re-post because history is marked. The mark is written through
`media_service.mark_wire_story_posted`: `dba2/bot/` writes NO `leagues`
column (P5-99.m.1); the record that a Discord message went out lives on the
story.
"""

from __future__ import annotations

import logging

import discord

from dba2.data.db import Executor
from dba2.data.repositories import channel_repo
from dba2.domain.channel import ChannelPurpose
from dba2.presentation.panel import Panel
from dba2.services import media_service, view_service

from .embeds import panel_to_embed

log = logging.getLogger(__name__)


async def post_wire_panels(channel: discord.TextChannel, panels: tuple[Panel, ...]) -> int:
    """Decision P5-15: "the wire channel posts `MediaStory.rendered_text`
    verbatim, ONE MESSAGE PER STORY" -- never a single batched message,
    so a long story never gets silently truncated to fit alongside
    others. Returns the number of sends that COMPLETED, counted as each
    returns, for a caller (a command handler) to report back. A send that
    raises propagates -- the panels before it are already out, the ones after
    it are not attempted -- so the count never includes a send that failed."""
    sent = 0
    for panel in panels:
        await channel.send(embed=panel_to_embed(panel))
        sent += 1
    return sent


async def post_new_wire_stories(
    guild: discord.Guild,
    ex: Executor,
    *,
    league_id: int,
    season: int,
    cycle_index: int | None = None,
) -> int:
    """Posts the wire stories nobody has posted yet (§6.3's "at the end of each
    media pulse cycle"), one message per story, and returns how many were sent.

    Reads `view_service.get_unposted_wire_panels` -- the stories with no
    `posted_at`, optionally narrowed to `cycle_index` -- so the promise this
    docstring always made ("never re-post history every time this is called")
    now holds for a stronger reason than the old cycle scope: history is marked.
    `cycle_index=None` is the startup sweep's shape and reads the whole season.

    **Send, then mark, per story, in that order** (P5-99.s.2). A send that raises
    propagates -- that story and every later one stay unposted and post next time,
    so nothing is lost. A MARK that raises is logged and does NOT stop the rest:
    that one story has been posted and is still unmarked, so it posts once more on
    a later call (the disclosed R12 bound, one duplicate per failed mark); the
    stories after it are posted and marked normally. Marking before sending would
    turn the same failure into a story that never posts at all."""
    row = await channel_repo.get_league_channel(
        ex, league_id=league_id, purpose=ChannelPurpose.WIRE
    )
    if row is None:
        raise RuntimeError(
            f"no wire channel registered for league {league_id} -- "
            "run bot.channels.reconcile_league_channels first"
        )
    channel = guild.get_channel(row.channel_id)
    if channel is None:
        raise RuntimeError(
            f"wire channel {row.channel_id} no longer resolves in guild {guild.id} -- "
            "re-run bot.channels.reconcile_league_channels"
        )
    unposted = await view_service.get_unposted_wire_panels(
        ex, league_id=league_id, season=season, cycle_index=cycle_index
    )
    posted = 0
    for story_id, panel in unposted:
        # One message per story through the same sender `post_wire_panels` proves
        # (P5-15), called with a one-element tuple so the mark can follow it. The sender either
        # sends (and counts) or raises out of this function before the mark, so `posted` is what
        # actually went out and a story is marked only after its own send returned.
        posted += await post_wire_panels(channel, (panel,))
        try:
            await media_service.mark_wire_story_posted(ex, story_id)
        except Exception:
            log.exception(
                "surfaces.post_new_wire_stories: story %s was posted to the wire but its "
                "posted_at mark failed; it will post once more on a later call",
                story_id,
            )
    return posted


async def sync_standings_message(
    guild: discord.Guild,
    ex: Executor,
    *,
    league_id: int,
    season: int,
    cycle_index: int,
) -> discord.Message:
    """P5-12's "pinned/auto-updating standings embed": sends and pins the
    first message, then edits that same message on every later call --
    the message id is read from and written back to `league_channels`
    (P5-12's own "a message-id column on the standings row specifically
    ... its id must survive a bot restart"), never held only in memory.
    """
    row = await channel_repo.get_league_channel(
        ex, league_id=league_id, purpose=ChannelPurpose.STANDINGS
    )
    if row is None:
        raise RuntimeError(
            f"no standings channel registered for league {league_id} -- "
            "run bot.channels.reconcile_league_channels first"
        )
    channel = guild.get_channel(row.channel_id)
    if channel is None:
        raise RuntimeError(
            f"standings channel {row.channel_id} no longer resolves in guild {guild.id} -- "
            "re-run bot.channels.reconcile_league_channels"
        )

    panel = await view_service.get_standings_panel(
        ex, league_id=league_id, season=season, cycle_index=cycle_index
    )
    embed = panel_to_embed(panel)

    if row.message_id is not None:
        try:
            message = await channel.fetch_message(row.message_id)
        except discord.NotFound:
            message = None
        if message is not None:
            return await message.edit(embed=embed)

    message = await channel.send(embed=embed)
    try:
        await message.pin()
    except discord.HTTPException:
        pass  # missing "Manage Messages" or a full pin list -- the message
        # itself still posted and is still the one we track; a pin
        # failure must not stop the registry from recording it.
    await channel_repo.set_league_channel_message_id(
        ex, league_id=league_id, purpose=ChannelPurpose.STANDINGS, message_id=message.id
    )
    return message
