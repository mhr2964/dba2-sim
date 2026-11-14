"""The draft's two posts: a ping in the on-the-clock team's GM channel, and
the public record in `#games` (docs/design/phase-5-stage-d-prime-draft.md,
Decision P5-98.i).

**Order matters, and it is enforced here rather than asked of callers: the
ping first, the public line second.** The ping is the only way a person
learns it is their turn; a `#games` line they are not watching is not a
notification. So `notify_on_the_clock` does both halves, in that order, and
a caller cannot get it backwards by calling them in the wrong sequence.

**Each half is wrapped independently, and neither can raise.** A league can
lose either channel at any time (`games_channel` raises
`GamesChannelUnavailable` for one whose channels were never reconciled; a
GM channel can be deleted), and by the time anything here runs the pick is
already committed -- a failure to announce must never turn a real pick into
an error a person reads as "it didn't work". Every failure is a logged
warning carrying `game_feed.describe_discord_failure`'s reason, and the
`AnnounceReport` this returns is what lets `/draft` add `post_commit.
honest_suffix`'s "it went through, but..." to its reply.

**Each pick is announced exactly once, by the call that made it.** The
lines come from the `DraftCycleOutcome`'s own picks plus the human's, never
from a re-read of `draft_selections` -- a re-read is what would re-announce
the whole draft on the second `/draft`. The announcement is therefore
at-most-once: a crash between the commit and the post loses those lines and
nothing re-posts them. That is the opposite trade from `offer_delivery`'s
at-least-once queue, and the right one here, because a duplicated "TOR are
on the clock" sends a second person to make a pick that is already gone
while a missed line is repaired by the next cycle's message.

**This module is Stage E's producer seam** (`game_feed.open_block`'s role
for the broadcast, P5-97.n): when `/advance` runs the DRAFT hook it will
call this same function with the same `DraftCycleOutcome`. **In D'-25 the
hook path posts nothing** -- `run_draft_cycle` is a service and has no
Discord dependency, and `/draft` is the only caller -- so a draft that
starts from a press is silent until that seam is wired.

**It posts NEW messages only.** Nothing here edits a `#games` line, opens
or closes a block, or reveals a game, so P5-97.m's reveal rule is untouched
and no score can leak through the draft (a unit test pins that by name).
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg
import discord

from dba2.core.logging import get_logger
from dba2.data.db import Executor
from dba2.data.repositories import channel_repo, draft_repo, player_repo, team_repo
from dba2.presentation.draft import (
    build_cycle_post,
    build_on_the_clock_line,
    build_on_the_clock_ping,
    build_paused_line,
    build_pick_line,
)
from dba2.services.gm_service import DraftCycleOutcome, DraftCycleState

from . import game_feed

log = get_logger(__name__)


@dataclass(frozen=True)
class AnnouncedPick:
    """One selection this call is responsible for announcing. The human's own
    pick is passed in as one of these because it is NOT in the cycle's
    `DraftResult` -- it was applied by `apply_human_draft_pick`, in the
    transaction before the cycle ran."""

    team_id: int
    player_id: int
    pick_number: int
    round: int | None = None
    by_manager: bool = False


@dataclass(frozen=True)
class AnnounceReport:
    """What actually reached Discord. `/draft` reads this to decide whether
    its own reply needs `honest_suffix`'s disclosure -- the pick committed
    either way, and only the announcement of it is in question."""

    ping_owed: bool = False
    """A human-managed team is on the clock, so a ping was this call's job."""

    pinged: bool = False
    posted: bool = False

    @property
    def complete(self) -> bool:
        return self.posted and (self.pinged or not self.ping_owed)


def _require_no_open_transaction(ex: Executor) -> None:
    """P5-14 as a guard, `game_feed`'s own (`:320-323`) carried here rather
    than imported: every Discord call below follows a database read, so a
    caller that handed in a connection with a transaction open would put
    them inside it -- and `/draft`'s caller has two transactions of its own
    that must both be closed before any of this runs."""
    if isinstance(ex, asyncpg.Connection) and ex.is_in_transaction():
        raise ValueError(
            "draft_feed must not be called from inside a database transaction: "
            "Discord is never written to inside one (P5-14)"
        )


async def notify_on_the_clock(
    bot,
    ex: Executor,
    *,
    league_id: int,
    season: int,
    outcome: DraftCycleOutcome,
    human_pick: AnnouncedPick | None = None,
) -> AnnounceReport:
    """Announce one draft cycle: the GM-channel ping first, then `#games`.

    Never raises for anything Discord or a missing channel does; see the
    module docstring. `human_pick` is the pick made by the person whose
    command produced `outcome`, or `None` on a path (Stage E's press) where
    no person picked."""
    _require_no_open_transaction(ex)
    ping_owed = outcome.state is DraftCycleState.WAITING_ON_HUMAN
    pinged = await _ping_the_team_on_the_clock(bot, ex, outcome) if ping_owed else False
    posted = await announce_cycle(
        bot, ex, league_id=league_id, season=season, outcome=outcome, human_pick=human_pick
    )
    return AnnounceReport(ping_owed=ping_owed, pinged=pinged, posted=posted)


async def announce_cycle(
    bot,
    ex: Executor,
    *,
    league_id: int,
    season: int,
    outcome: DraftCycleOutcome,
    human_pick: AnnouncedPick | None = None,
) -> bool:
    """The public half, in `#games`: every pick this cycle made, one line
    each, then who is on the clock -- or, when the cycle STALLED or was
    SKIPPED and nobody is, one line saying the draft is paused and why
    (round-1 critic's M2). `AllowedMentions.none()` comes from
    `game_feed.send_line` (P5-97.s) -- everything in these lines is data.

    Public because Stage E's seam may want the record without the ping (a
    press whose cycle stops at nobody), but `notify_on_the_clock` is what
    a caller should use: it is the one that guarantees the order."""
    _require_no_open_transaction(ex)
    try:
        content = await _build_post(
            ex, league_id=league_id, season=season, outcome=outcome, human_pick=human_pick
        )
        if content is None:
            return True  # nothing to say is not a failure to say it
        channel = await game_feed.games_channel(bot, ex, league_id)
        await game_feed.send_line(channel, content)
        return True
    except game_feed.GamesChannelUnavailable as exc:
        log.warning("draft: league %s has no usable #games channel: %s", league_id, exc)
        return False
    except Exception as exc:
        log.warning(
            "draft: could not post league %s's draft lines to #games: %s",
            league_id,
            game_feed.describe_discord_failure(exc),
        )
        return False


async def _ping_the_team_on_the_clock(bot, ex: Executor, outcome: DraftCycleOutcome) -> bool:
    """The GM-channel push, with `AllowedMentions` restricted to the one
    manager (`offer_delivery.py:251-267`'s exact shape): the mention is the
    whole point of the message, and nothing else in it may ping.

    A team with no active GM channel is a logged warning and not an error --
    `Delivery.NO_GM_CHANNEL`'s own precedent."""
    if outcome.team_id is None or outcome.manager_user_id is None:
        log.warning("draft: a human-managed team is on the clock with no manager to ping")
        return False
    try:
        row = await channel_repo.get_active_gm_channel(ex, outcome.team_id)
        if row is None:
            log.warning(
                "draft: team %s has no active GM channel, so its manager was not pinged",
                outcome.team_id,
            )
            return False
        channel = bot.get_channel(row.channel_id)
        if channel is None:
            channel = await game_feed.bounded(bot.fetch_channel(row.channel_id))
        if not callable(getattr(channel, "send", None)):
            # A category, forum root or voice channel cannot take a message;
            # a GM channel that became one is unreachable, not a bug.
            log.warning("draft: team %s's GM channel cannot take messages", outcome.team_id)
            return False
        await game_feed.bounded(
            channel.send(
                content=build_on_the_clock_ping(
                    manager_user_id=outcome.manager_user_id,
                    team_code=await _team_code(ex, outcome.team_id),
                    round=outcome.round,
                    pick_number=outcome.pick_number,
                ),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=False,
                    replied_user=False,
                    users=[discord.Object(id=outcome.manager_user_id)],
                ),
            )
        )
        return True
    except Exception as exc:
        log.warning(
            "draft: could not ping team %s's manager: %s",
            outcome.team_id,
            game_feed.describe_discord_failure(exc),
        )
        return False


async def _build_post(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    outcome: DraftCycleOutcome,
    human_pick: AnnouncedPick | None,
) -> str | None:
    """The `#games` message, or `None` when this cycle has nothing to say.

    **The SET of picks comes from the outcome and the human's own pick, and
    from nowhere else** -- that is what makes the announcement at-most-once
    instead of "the whole draft, again". The one board read below supplies
    ROUND LABELS only (`DraftPickResult` carries a `pick_number` and no
    round), and a pick number it cannot explain degrades to a line without
    a round rather than dropping the pick."""
    picks: list[AnnouncedPick] = []
    if human_pick is not None:
        picks.append(human_pick)
    if outcome.draft is not None:
        picks.extend(
            AnnouncedPick(
                team_id=result.team_id,
                player_id=result.prospect_id,
                pick_number=result.pick_number,
            )
            for result in outcome.draft.picks
        )
    clock_line = None
    if outcome.state is DraftCycleState.WAITING_ON_HUMAN and outcome.team_id is not None:
        clock_line = build_on_the_clock_line(
            team_code=await _team_code(ex, outcome.team_id),
            round=outcome.round,
            pick_number=outcome.pick_number,
        )
    # Round-1 critic's M2. A STALLED or SKIPPED cycle has nobody on the
    # clock, so without this the public record is every pick line and then
    # silence -- indistinguishable from a healthy draft between picks, while
    # the only person who knows it has stopped is the one whose ephemeral
    # reply said so. COMPLETE is deliberately not here: a finished draft is
    # not a paused one.
    paused_line = None
    if outcome.state in (DraftCycleState.STALLED, DraftCycleState.SKIPPED):
        paused_line = build_paused_line(outcome.reason.value if outcome.reason else None)
    if not picks and clock_line is None and paused_line is None:
        return None

    rounds = await _rounds_by_pick_number(ex, league_id, season)
    lines = []
    for pick in picks:
        lines.append(
            build_pick_line(
                team_code=await _team_code(ex, pick.team_id),
                player_name=await _player_name(ex, pick.player_id),
                round=pick.round if pick.round is not None else rounds.get(pick.pick_number),
                pick_number=pick.pick_number,
                by_manager=pick.by_manager,
            )
        )
    return build_cycle_post(lines, clock_line, paused_line=paused_line)


async def _rounds_by_pick_number(ex: Executor, league_id: int, season: int) -> dict[int, int]:
    """Pick number -> round, for the ROUND LABELS `_build_post` needs and
    `DraftPickResult` does not carry.

    **Keyed by pick number because there is nothing else to key it by**
    (round-1 critic's minor 6): the announcement's picks come from
    `DraftPickResult`, which carries a `pick_number` and no pick id, so a
    map keyed by `draft_picks.id` could not be looked up. And `draft_picks`
    has NO unique constraint on `(league_id, season, pick_number)` --
    migration 004 puts one on `drafts (league_id, season)` and on
    `draft_selections (draft_id, pick_number)`, neither of which stops two
    `draft_picks` rows claiming the same number. So a number two rows
    disagree about is dropped rather than resolved by whichever row came
    last: `build_pick_line` already degrades a `round=None` to `Pick 31:`,
    which is honest, and a confidently wrong round is not."""
    rows = await draft_repo.list_draft_picks_for_season(ex, league_id, season)
    rounds: dict[int, int] = {}
    ambiguous: set[int] = set()
    for row in rows:
        if row.pick_number is None:
            continue
        if row.pick_number in rounds and rounds[row.pick_number] != row.round:
            ambiguous.add(row.pick_number)
        rounds[row.pick_number] = row.round
    for pick_number in ambiguous:
        log.warning(
            "draft: league %s season %s has two picks numbered %s in different rounds, "
            "so that pick's line is announced without a round",
            league_id,
            season,
            pick_number,
        )
        rounds.pop(pick_number, None)
    return rounds


async def _team_code(ex: Executor, team_id: int) -> str:
    """A code, or an honest id. Every string here is decoration on a pick
    that already happened, so a missing row degrades the wording and never
    stops the post."""
    team = await team_repo.get_team(ex, team_id)
    return f"team {team_id}" if team is None else team.code


async def _player_name(ex: Executor, player_id: int) -> str:
    player = await player_repo.get_player(ex, player_id)
    return f"player {player_id}" if player is None else f"{player.first_name} {player.last_name}"
