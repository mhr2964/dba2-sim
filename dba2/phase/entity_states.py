"""Per-row entity lifecycle machines — phase-0-foundation.md §6.4.

The boundary rule: league-scoped process state goes in the one phase graph
(`phase/graph.py`); per-row entity lifecycle (a game, a trade, an award
ballot) gets its own declared enum plus a validated transition table here.
"Ad hoc status strings compared inline are not permitted anywhere" (§6.4) —
this module is where a new `status` column's legal moves are supposed to
get registered, not string-compared in a service.

The enums (`GameStatus`, `TradeStatus`, `AwardBallotStatus`) are imported
from `domain/`, not redefined here — `domain/` owns every dataclass and
enum; this module owns only the transition tables and the generic
validator that checks them.
"""
from __future__ import annotations

from dba2.domain.game import GameStatus
from dba2.domain.phase import AwardBallotStatus, TradeStatus


def is_legal_transition[E](transitions: dict[E, frozenset[E]], from_: E, to: E) -> bool:
    """Generic edge check shared by all three entity machines below: `to`
    is legal from `from_` iff it appears in `transitions[from_]`. Unlike
    `graph.py`'s own `is_legal_transition` (which closes over the single
    module-level `NEXT_PHASES` map), this one takes the table explicitly
    since it has to serve three independent maps rather than one."""
    return to in transitions.get(from_, frozenset())


# scheduled -> in_progress -> final is the normal path. `postponed` can be
# reached from either `scheduled` or `in_progress` and always resumes back
# to `scheduled` (re-slotted) rather than jumping straight to `in_progress`
# or `final` — Phase 0 has no engine yet that could resume a postponed game
# mid-simulation, so "postponed" always means "re-schedule it."
#
# `FINAL -> IN_PROGRESS` (Decision P1-26, phase-1-simulation-engine.md): the
# tuning loop re-sims the same schedule dozens of times per calibration run,
# so this is Phase 1's *dominant* status-changing path, not a rare escape
# hatch — routing it around this table would make GAME_TRANSITIONS
# decoration for the majority of Phase 1's transitions. The edge is legal
# for any caller that reaches it (`is_legal_transition` is a pure lookup
# with no notion of caller identity); the actual protection against misuse
# is one level down, at the service layer — `game_sim_service.sim_game` is
# the only caller in the codebase permitted to request it, and only when
# its own `resim=True` argument is explicitly passed. `game_sim_service.
# sim_game` calls `is_legal_transition(GAME_TRANSITIONS, ...)` directly for
# both the resim and fresh-sim edges (critic round 19 B19-2 fixed a version
# that reimplemented this table's rule inline instead of reading it).
GAME_TRANSITIONS: dict[GameStatus, frozenset[GameStatus]] = {
    GameStatus.SCHEDULED: frozenset({GameStatus.IN_PROGRESS, GameStatus.POSTPONED}),
    GameStatus.IN_PROGRESS: frozenset({GameStatus.FINAL, GameStatus.POSTPONED}),
    GameStatus.FINAL: frozenset({GameStatus.IN_PROGRESS}),
    GameStatus.POSTPONED: frozenset({GameStatus.SCHEDULED}),
}

# Given literally in §6.4: proposed -> {accepted, rejected, countered,
# expired, vetoed}. None of those five have an outgoing edge — a countered
# offer becomes a new trade proposal (a new row), not this row changing
# state again.
TRADE_TRANSITIONS: dict[TradeStatus, frozenset[TradeStatus]] = {
    TradeStatus.PROPOSED: frozenset(
        {
            TradeStatus.ACCEPTED,
            TradeStatus.REJECTED,
            TradeStatus.COUNTERED,
            TradeStatus.EXPIRED,
            TradeStatus.VETOED,
        }
    ),
    TradeStatus.ACCEPTED: frozenset(),
    TradeStatus.REJECTED: frozenset(),
    TradeStatus.COUNTERED: frozenset(),
    TradeStatus.EXPIRED: frozenset(),
    TradeStatus.VETOED: frozenset(),
}

# Linear per §6.4's own naming: open -> closed -> certified.
AWARD_BALLOT_TRANSITIONS: dict[AwardBallotStatus, frozenset[AwardBallotStatus]] = {
    AwardBallotStatus.OPEN: frozenset({AwardBallotStatus.CLOSED}),
    AwardBallotStatus.CLOSED: frozenset({AwardBallotStatus.CERTIFIED}),
    AwardBallotStatus.CERTIFIED: frozenset(),
}
