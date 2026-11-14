"""game_broadcasts -- migration 019, the persisted half of Stage D's `/watch`
broadcast (docs/design/phase-5-stage-d-broadcast.md §8, P5-97).

Why this lives in `domain/` and not next to the bot: `data/` may not import
`phase/`, `bot/` or `services/`, and both the repository (to refuse an illegal
status write) and the bot layer (to decide what a sweep should do with a row)
need the same two facts -- which status moves are legal, and which of the
sweep's drain shapes a row is in. Putting them here gives both one definition.

The row stores Discord message ids and a status. It never stores a beat log
(P5-42 stands): not one `BeatRecord` reaches Postgres through this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class BroadcastStatus(StrEnum):
    """A closed set enforced HERE, over a plain `TEXT` column with no DB
    `CHECK` (migration 019 / 016's shared choice), so widening it is a Python
    change and not a constraint swap.

    `pending`      the row exists (a block was opened) but no thread does.
    `queued`       the thread and its placeholder message exist; the task has
                   not yet got a broadcast slot.
    `live`         the pacer is editing the message.
    `finished`     the final frame landed.
    `interrupted`  the broadcast ended abnormally (a failed final frame, a
                   guarded task failure, or a process death the sweep found).
    """

    PENDING = "pending"
    QUEUED = "queued"
    LIVE = "live"
    FINISHED = "finished"
    INTERRUPTED = "interrupted"


BROADCAST_TRANSITIONS: dict[BroadcastStatus, frozenset[BroadcastStatus]] = {
    BroadcastStatus.PENDING: frozenset({BroadcastStatus.QUEUED}),
    BroadcastStatus.QUEUED: frozenset({BroadcastStatus.LIVE, BroadcastStatus.INTERRUPTED}),
    BroadcastStatus.LIVE: frozenset({BroadcastStatus.FINISHED, BroadcastStatus.INTERRUPTED}),
    BroadcastStatus.FINISHED: frozenset(),
    BroadcastStatus.INTERRUPTED: frozenset(),
}
"""The legal status moves. Derived from the design's own state machine: a row
is born `pending`; `/watch` persists the thread and moves it to `queued`
(P5-97.w step 6); the task takes a slot and goes `live` (step 8); it ends
`finished`, or `interrupted` (P5-97.q items 1-2). A queued task that dies
before it ever got a slot, and the startup sweep's process-died case
(P5-97.q item 6, `live` or `queued`), both go to `interrupted` directly.
`finished` and `interrupted` are terminal -- `/rewatch` is deferred (§12 item
7), so nothing re-opens a broadcast. `pending` moves only to `queued`: a
`pending` row whose block is gone is revealed by the sweep without changing
its status (P5-97.q item 6, third shape)."""


def is_legal_broadcast_transition(old: BroadcastStatus, new: BroadcastStatus) -> bool:
    return new in BROADCAST_TRANSITIONS[old]


class SweepShape(StrEnum):
    """Which of the startup sweep's drain shapes a still-unrevealed row is in
    (P5-97.q item 6). The first three are the design's; the fourth is what its
    own reveal table requires (see `GameBroadcast.sweep_shape`)."""

    INTERRUPTED_BROADCAST = "interrupted_broadcast"
    """`status` is `live` or `queued`: the process died mid-broadcast. Edit
    the stored `message_id` to the interrupted panel, mark `interrupted`, then
    reveal."""
    CRASHED_REVEAL = "crashed_reveal"
    """`reveal_claimed_at` set and `reveal_posted_at` NULL: a reveal claimed
    the row and died before posting. Re-post and set `reveal_posted_at`. The
    claim CAS cannot be re-taken (it is already set), so the sweep goes
    straight to posting -- safe because the sweep runs at startup, when no
    live owner of that claim can exist."""
    ORPHANED_PENDING = "orphaned_pending"
    """`status = 'pending'` and no claim: its block is gone. Reveal it
    (`root_message_id IS NULL` means post fresh rather than edit)."""
    UNREVEALED_ENDED = "unrevealed_ended"
    """`finished`/`interrupted` with no claim: the broadcast ended while its
    block was still open, so it correctly did not reveal (P5-97.m), and the
    process then restarted before the block closed. Not one of the design's
    three listed shapes, but its reveal table says the startup sweep reveals
    "every unrevealed row", and without this shape such a row would never be
    revealed."""


@dataclass(frozen=True)
class GameBroadcast:
    """One `game_broadcasts` row. `UNIQUE (game_id)`: at most one per game."""

    id: int
    league_id: int
    game_id: int
    block_id: str
    root_message_id: int | None
    """The game's line in `#games`. `None` until the slate post (or `/watch`'s
    repair path) lands; a reveal on a row with none posts a fresh message."""
    thread_id: int | None
    """The game's thread. Set once, together with `message_id` and the move
    to `queued`; `create_thread` is guarded by `thread_id IS NULL`."""
    message_id: int | None
    """The thread message the pacer edits. A real channel message, never an
    interaction followup (P5-97.l)."""
    status: BroadcastStatus
    reveal_claimed_at: datetime | None
    """Phase one of the reveal CAS. Set by whoever won the claim."""
    reveal_posted_at: datetime | None
    """Phase two: the result line reached `#games`. `NULL` with a claim set is
    a crashed reveal."""
    created_at: datetime

    @property
    def is_revealed(self) -> bool:
        return self.reveal_posted_at is not None

    @property
    def sweep_shape(self) -> SweepShape | None:
        """The drain shape this row is in, or `None` when there is nothing
        left to do (result already posted). Precedence matters: a `live`/
        `queued` row is an interrupted broadcast first, whatever its reveal
        columns say; a claimed-but-unposted row is a crashed reveal before it
        is a `pending` orphan."""
        if self.reveal_posted_at is not None:
            return None
        if self.status in (BroadcastStatus.LIVE, BroadcastStatus.QUEUED):
            return SweepShape.INTERRUPTED_BROADCAST
        if self.reveal_claimed_at is not None:
            return SweepShape.CRASHED_REVEAL
        if self.status is BroadcastStatus.PENDING:
            return SweepShape.ORPHANED_PENDING
        return SweepShape.UNREVEALED_ENDED
