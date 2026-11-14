"""`dba2/media/detect/motivation.py` -- docs/design/media-v2-newsroom.md
MV-2b: the one additive schema change (`player_motivation_events.from_state`/
`to_state`, migration 021, already landed in build steps 1-2) plus the two
pure helpers it exists to feed. Neither helper detects a `StoryKind` on its
own -- both are consumed by `dba2/media/detect/trades.py`'s own
`TRADE_BROKEN_PROMISE` detector (MV-2a), and `satisfaction_swings` is a
general-purpose fact source any future detector may also read.

**No new read, no new read-model (MV-2b's own decision, revision 4).**
`agency_service.load_ladder_transitions_for_league` already exists and
`load_player_unrest_candidates` already calls it every media cycle -- every
`ladder_transition` row for a league-season is already in memory by the
time any detector runs. Reconstructing a player's ladder state *as of* a
past instant is therefore a pure function over rows a caller already has,
never a second query.

Zero I/O, the same purity rules every other module under `dba2/media/`
carries: no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.motivation` and stdlib.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from dba2.domain.motivation import MotivationAxis, PlayerMotivationEvent, RequestState

CHECKPOINT_EVALUATION_EVENT_TYPE = "checkpoint_evaluation"
"""The real event-type string `dba2/agency/motivation.py::evaluate_axes`'s
own default writes (`WrittenDelta.event_type`, `agency_service.py`'s own
per-write insert loop) -- not a named module constant there (a plain
string-literal default), reused here by its real value rather than
re-derived, matching this module's own "read what the checkpoint already
wrote" framing."""

SATISFACTION_SWING_THRESHOLD = 15
"""Disclosed, not calibrated -- MV-2b's own text: the axis scale is
`[-100, 100]` and `SATISFACTION_SWING_THRESHOLD` compares a SUMMED delta
across a season's checkpoints (`agency/motivation.py::SATISFACTION_SCALE =
10.0` per single checkpoint write), so `15` across two checkpoints a season
is a real move, not noise -- the same reasoning MV-2b's own text gives."""


def request_state_at(
    transitions: Sequence[PlayerMotivationEvent],
    *,
    as_of: datetime,
    current_request_state: RequestState,
) -> RequestState | None:
    """MV-2b's reconstruction, verbatim: the `from_state` of the earliest
    transition strictly AFTER `as_of` if one exists; else the `to_state` of
    the latest transition at-or-before `as_of` if one exists; else
    `current_request_state` (no transition happened all season, so the
    state never moved). `transitions` is the caller's own season-scoped
    `agency_service.load_ladder_transitions_for_league` output for ONE
    player (already filtered/loaded, this function's own purity boundary).

    **A `None` on the relevant leg is "no reading," never `CONTENT`.** A
    transition row written before migration 021 carries `from_state`/
    `to_state` both `NULL` -- if that is the specific row this
    reconstruction lands on, the return is `None`, and the caller (MV-2a's
    `TRADE_BROKEN_PROMISE` detector) must not read that as `CONTENT` (this
    project's standing "absent is neutral, never a fabricated value"
    rule)."""
    after = sorted((t for t in transitions if t.created_at > as_of), key=lambda t: t.created_at)
    if after:
        return after[0].from_state
    before = sorted((t for t in transitions if t.created_at <= as_of), key=lambda t: t.created_at)
    if before:
        return before[-1].to_state
    return current_request_state


def satisfaction_swings(events: Sequence[PlayerMotivationEvent]) -> dict[MotivationAxis, int]:
    """Sums `delta` per `axis` across `events` (the caller's own season-
    scoped, player-scoped `checkpoint_evaluation` rows) and returns only
    the axes whose summed delta is `<= -SATISFACTION_SWING_THRESHOLD` or
    `>= +SATISFACTION_SWING_THRESHOLD` -- an axis with no real swing is
    simply absent from the returned mapping, never a `0` entry."""
    totals: dict[MotivationAxis, int] = {}
    for event in events:
        totals[event.axis] = totals.get(event.axis, 0) + event.delta
    return {
        axis: total
        for axis, total in totals.items()
        if total <= -SATISFACTION_SWING_THRESHOLD or total >= SATISFACTION_SWING_THRESHOLD
    }
