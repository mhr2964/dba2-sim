"""The `Phase` enum plus the two entity-status enums that have no table of
their own to live next to (`TradeStatus`, `AwardBallotStatus` — Phase 0 has
no `trades` or `award_ballots` table yet; `GameStatus` lives in
`domain/game.py` instead, next to `Game`, since that table exists).

phase-0-foundation.md §6.2 (the `Phase` enum, 20 values) and §6.4 (the
three entity-status enums).

This is `domain/`'s canonical home for `Phase`: `dba2/phase/states.py`
imports and re-exports it rather than redeclaring it, because `domain/`
must not import anything from `dba2` except `core` (Decision 4) while
`phase/` legitimately depends on `domain/` for types — declaring it here
and importing it upward into `phase/` is the only direction that doesn't
create a cycle.
"""
from __future__ import annotations

from enum import StrEnum


class Phase(StrEnum):
    """§6.2's ordered season-phase list."""

    SETUP = "SETUP"
    PRESEASON = "PRESEASON"
    REGULAR_SEASON = "REGULAR_SEASON"
    TRADE_DEADLINE = "TRADE_DEADLINE"
    POST_DEADLINE = "POST_DEADLINE"
    REGULAR_SEASON_COMPLETE = "REGULAR_SEASON_COMPLETE"
    PLAYIN = "PLAYIN"
    PLAYOFFS_R1 = "PLAYOFFS_R1"
    PLAYOFFS_R2 = "PLAYOFFS_R2"
    CONFERENCE_FINALS = "CONFERENCE_FINALS"
    FINALS = "FINALS"
    AWARDS = "AWARDS"
    DRAFT_LOTTERY = "DRAFT_LOTTERY"
    DRAFT = "DRAFT"
    POST_DRAFT_TRADES = "POST_DRAFT_TRADES"
    PROGRESSION = "PROGRESSION"
    OPTION_DECISIONS = "OPTION_DECISIONS"
    RESIGN_WINDOW = "RESIGN_WINDOW"
    FREE_AGENCY = "FREE_AGENCY"
    WAIVERS = "WAIVERS"


class TradeStatus(StrEnum):
    """§6.4 — literal transition set given in the doc: proposed can move to
    any of the other five; none of the other five have an outgoing edge (a
    countered offer is a new trade proposal, i.e. a new row, not this row
    changing state again)."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COUNTERED = "countered"
    EXPIRED = "expired"
    VETOED = "vetoed"


class AwardBallotStatus(StrEnum):
    """§6.4 — linear: open -> closed -> certified."""

    OPEN = "open"
    CLOSED = "closed"
    CERTIFIED = "certified"
