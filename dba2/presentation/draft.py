"""The draft's own plain-text lines and its confirmation panel
(docs/design/phase-5-stage-d-prime-draft.md, Decision P5-98.i).

Pure, like every other module here (Decision P5-3): no `discord`, no
`dba2.services`, no database. Everything a line needs -- a team's code, a
player's name, a round and a pick number -- is passed in by `bot/draft_
feed.py`, which is the half that may read rows and talk to Discord.

**Three surfaces, three shapes, and the difference matters.**

* `build_pick_line` is one line of the public `#games` record of the
  draft. It carries no mention, because everything in it is data.
* `build_on_the_clock_line` is the same channel's "whose turn is it now",
  and it also carries no mention: a `#games` full of pings is how a league
  learns to mute `#games`.
* `build_on_the_clock_ping` is the GM channel's, and it is the ONLY one
  with a mention in it -- the one message whose whole job is to reach a
  person who is not watching. Its caller restricts `allowed_mentions` to
  exactly that user (`offer_delivery`'s own rule, §6.5 there), which is
  what stops anything else in the text becoming a real ping.

`build_pick_panel` is the command's own public confirmation. It repeats
the pick rather than linking to `#games`, because the person who ran
`/draft` should not have to go and look for confirmation that their pick
landed.
"""

from __future__ import annotations

from collections.abc import Sequence

from dba2.presentation.panel import (
    MAX_DESCRIPTION_CHARS,
    MAX_FIELD_VALUE_CHARS,
    MAX_TITLE_CHARS,
    Panel,
    PanelColor,
    PanelField,
)

_MAX_MESSAGE_CHARS = 2000
"""Discord's own message `content` limit -- the same clip `presentation/
offers.py` applies to its notification text."""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clip_to_message_limit(text: str) -> str:
    """`_clip` at Discord's message limit, for the one caller that builds
    its own text: `cogs/draft.py`'s reply sentence carries a player's name
    and its own disclosures, and the alternative is a second copy of the
    number 2000 in `bot/` (round-1 critic's blocking issue B3)."""
    return _clip(text, _MAX_MESSAGE_CHARS)


def build_pick_line(
    *,
    team_code: str,
    player_name: str,
    round: int | None,
    pick_number: int,
    by_manager: bool = False,
) -> str:
    """One selection, as `#games` records it. `by_manager` distinguishes a
    person's pick from the AI's -- the same fact `league_transactions`'
    `actor_user_id` carries durably (P5-98.e), said out loud here because a
    league watching the draft can see who is actually turning up for it.

    `round=None` is the honest degradation for a pick whose round its caller
    could not resolve: the selection still gets announced, by the number that
    is unique across the whole draft anyway (`draft_selections`' own
    `UNIQUE (draft_id, pick_number)`)."""
    who = " (manager's pick)" if by_manager else ""
    where = f"Round {round}, pick {pick_number}" if round is not None else f"Pick {pick_number}"
    return _clip(f"**{where}:** {team_code} select {player_name}{who}.", _MAX_MESSAGE_CHARS)


def build_on_the_clock_line(*, team_code: str, round: int, pick_number: int) -> str:
    """`#games`' "whose turn is it", with no mention in it."""
    return _clip(
        f"**{team_code} are on the clock** -- round {round}, pick {pick_number}.",
        _MAX_MESSAGE_CHARS,
    )


def build_on_the_clock_ping(
    *, manager_user_id: int, team_code: str, round: int, pick_number: int
) -> str:
    """The GM channel's push. The mention is the whole point of this
    message: a `#games` line somebody is not watching is not a
    notification, which is why this one is posted FIRST."""
    return _clip(
        f"<@{manager_user_id}> **{team_code} are on the clock** -- round {round}, "
        f"pick {pick_number}. Make the pick with "
        f"`/draft code:{team_code} player_id:<prospect>`.",
        _MAX_MESSAGE_CHARS,
    )


_PAUSE_DETAIL_BY_REASON: dict[str, str] = {
    "pool_exhausted": (
        "no draft-eligible prospects remain, so the commissioner needs to add a draft class"
    ),
    "unresolved_picks": (
        "some picks have no slot yet, so the lottery has to finish resolving this draft"
    ),
    "phase_moved": "the league has left the draft",
    "resume_contended": (
        "another draft action held the board, so ask the commissioner to continue the draft"
    ),
    "resume_failed": (
        "the AI could not continue (the error is logged), so ask the commissioner to "
        "continue the draft"
    ),
}
"""Keyed by `DraftCycleReason`'s VALUES rather than its members -- this
module is pure (Decision P5-3) and may not import a service enum, which is
the same reason `build_pick_panel` takes a finished `next_up` sentence. An
unrecognised key degrades to the generic line below rather than raising.

**None of these tells a league to run a command** (round-2 critic's minor
3). `#games` is read by everybody and the next pick after a stopped resume
usually belongs to an AI team, so "run `/draft` again" is unactionable for
almost every reader of it -- the commissioner is the one who can restart a
draft that has stopped, so the commissioner is who these name."""

_PAUSE_DETAIL_UNKNOWN = (
    "nobody is on the clock and the AI is not continuing on its own, so ask the "
    "commissioner to continue the draft"
)

_ADVANCING_LINE = "**The commissioner is advancing the league** -- the draft continues from there."
"""`claim_held`'s own line, and the one state below that is NOT a pause
(round-2 critic's minor 5): the commissioner is running the advance right
now, the draft continues from their press, and the one thing a league would
do about a paused draft -- go and find the commissioner -- is already
happening."""


def build_paused_line(reason: str | None) -> str:
    """`#games`' "this draft is not coming back on its own" (round-1
    critic's M2) -- or, for `claim_held`, "it is coming back, from the
    commissioner's press" (round-2 critic's minor 5).

    **The failure this exists for is a silent one.** A `STALLED` or
    `SKIPPED` cycle posts every pick it made and then stops -- no clock
    line, because nobody is on the clock -- so the public record reads
    exactly like a healthy draft between picks, and the only person told
    otherwise is the one whose (ephemeral) `/draft` reply said so. One line,
    ASCII, no mention in it, posted inside the same per-cycle message."""
    if reason == "claim_held":
        return _clip(_ADVANCING_LINE, _MAX_MESSAGE_CHARS)
    detail = _PAUSE_DETAIL_BY_REASON.get(reason or "", _PAUSE_DETAIL_UNKNOWN)
    return _clip(f"**The draft is paused** -- {detail}.", _MAX_MESSAGE_CHARS)


_OMITTED_MARKER = "_(earlier picks in this run are not shown)_"


def build_cycle_post(
    lines: Sequence[str], clock_line: str | None, paused_line: str | None = None
) -> str:
    """One `#games` message for one cycle: every pick line, then who is on
    the clock -- or, when nobody is, why the draft has stopped
    (`paused_line`; the two are mutually exclusive in practice, since a
    cycle either left somebody on the clock or did not).

    **One message rather than one per pick**, because a cycle can make
    dozens of picks in a row (every AI team between two people) and a
    channel full of one-line messages is unreadable -- and unlike a game's
    line, nothing here is ever edited by id afterwards, which is the reason
    `#games` posts games separately.

    **The tail is what survives truncation.** Discord rejects a message over
    2000 characters outright, so a 59-pick cycle would post NOTHING at all
    without this: the oldest pick lines are dropped, with a marker saying
    so, and the sentence telling the next person to go -- or telling the
    league the draft has stopped -- is never the part that gets cut."""
    tail = [line for line in (clock_line, paused_line) if line is not None]
    kept = list(lines)
    body = "\n".join([*kept, *tail])
    if len(body) <= _MAX_MESSAGE_CHARS:
        return body
    while kept:
        kept.pop(0)
        body = "\n".join([_OMITTED_MARKER, *kept, *tail])
        if len(body) <= _MAX_MESSAGE_CHARS:
            return body
    return _clip("\n".join([_OMITTED_MARKER, *tail]), _MAX_MESSAGE_CHARS)


def build_pick_panel(
    *,
    team_code: str,
    player_name: str,
    round: int,
    pick_number: int,
    next_up: str,
) -> Panel:
    """`/draft`'s own confirmation. `next_up` is whatever the caller has to
    say about the board now -- who is on the clock, that the draft is
    finished, or why nothing followed this pick -- passed in as a finished
    sentence rather than reconstructed here, because the states it can
    describe belong to `DraftCycleOutcome` and a pure renderer may not
    import it.

    **Every field is clipped to Discord's own limit for that surface**
    (round-1 critic's blocking issue B3). `Panel.__post_init__` RAISES on a
    title over 256 characters, and nothing between `players.last_name` (a
    `TEXT` column) and this call bounds a name: a 300-character last name
    reproduced it live, after the pick had committed, so the pick landed and
    the person who made it got no reply at all. The limits are Discord's,
    this module is the layer that knows them, and a clipped title is a
    strictly better answer than an exception on a write that already
    happened. `cogs/draft.py` wraps the call as well, because the NEXT
    overflow will be in a surface nobody has thought about yet."""
    return Panel(
        title=_clip(f"{team_code} select {player_name}", MAX_TITLE_CHARS),
        description=_clip(f"Round {round}, pick {pick_number}.", MAX_DESCRIPTION_CHARS),
        fields=(PanelField(name="Next", value=_clip(next_up, MAX_FIELD_VALUE_CHARS)),),
        color=PanelColor.POSITIVE,
    )
