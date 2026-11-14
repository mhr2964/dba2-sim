"""NEXT_PHASES adjacency map — phase-0-foundation.md §6.2.

Strictly linear except the two documented branch points (`SETUP` and
`REGULAR_SEASON`). v1's third and fourth branch points (skipping straight
to `PROGRESSION_PENDING` from either `AWARDS_CLOSED` or `DRAFT_LOTTERY_DONE`)
existed only because a commissioner ran an ad hoc rollover command from
wherever they happened to be standing — that capability is `force_advance`
(`phase/service.py`), an explicit, audited, reason-requiring escape hatch,
not an extra edge silently baked into this graph. Keeping it in the graph
would make an operator convenience indistinguishable from a legal
transition, which is exactly what the doc says a graph must not do.
"""
from __future__ import annotations

from collections import deque

from dba2.domain.phase import Phase

NEXT_PHASES: dict[Phase, frozenset[Phase]] = {
    Phase.SETUP: frozenset({Phase.PRESEASON, Phase.REGULAR_SEASON}),
    Phase.PRESEASON: frozenset({Phase.REGULAR_SEASON}),
    Phase.REGULAR_SEASON: frozenset(
        {Phase.TRADE_DEADLINE, Phase.REGULAR_SEASON_COMPLETE}
    ),
    Phase.TRADE_DEADLINE: frozenset({Phase.POST_DEADLINE}),
    Phase.POST_DEADLINE: frozenset({Phase.REGULAR_SEASON_COMPLETE}),
    Phase.REGULAR_SEASON_COMPLETE: frozenset({Phase.PLAYIN}),
    Phase.PLAYIN: frozenset({Phase.PLAYOFFS_R1}),
    Phase.PLAYOFFS_R1: frozenset({Phase.PLAYOFFS_R2}),
    Phase.PLAYOFFS_R2: frozenset({Phase.CONFERENCE_FINALS}),
    Phase.CONFERENCE_FINALS: frozenset({Phase.FINALS}),
    Phase.FINALS: frozenset({Phase.AWARDS}),
    Phase.AWARDS: frozenset({Phase.DRAFT_LOTTERY}),
    Phase.DRAFT_LOTTERY: frozenset({Phase.DRAFT}),
    Phase.DRAFT: frozenset({Phase.POST_DRAFT_TRADES}),
    Phase.POST_DRAFT_TRADES: frozenset({Phase.PROGRESSION}),
    Phase.PROGRESSION: frozenset({Phase.OPTION_DECISIONS}),
    Phase.OPTION_DECISIONS: frozenset({Phase.RESIGN_WINDOW}),
    Phase.RESIGN_WINDOW: frozenset({Phase.FREE_AGENCY}),
    Phase.FREE_AGENCY: frozenset({Phase.WAIVERS}),
    Phase.WAIVERS: frozenset({Phase.PRESEASON}),  # closes the loop into next season
}

# Every Phase member must be a key (even ones with no outgoing edge would
# need an explicit empty frozenset — none exist in this graph, but the
# assertion below is what actually guarantees NEXT_PHASES never silently
# drifts out of sync with the Phase enum as phases get added later).
assert set(NEXT_PHASES) == set(Phase), "NEXT_PHASES must cover every Phase member"


def legal_next_phases(current: Phase) -> frozenset[Phase]:
    """Which phases `current` may legally advance *to* next."""
    return NEXT_PHASES[current]


def is_legal_transition(from_: Phase, to: Phase) -> bool:
    return to in NEXT_PHASES[from_]


# Stage E (phase-5-stage-e-advance.md P5-99.b, P5-99.f) -- the branch rule and
# the no-op set live here, not in `season_service`, because `route_to` below
# reads the first and `graph.py` owns adjacency (P5-39): a pure module cannot
# import a service, and a service that owned them would leave `route_to` with
# nothing to resolve a branch against.

# The branch rule, explicit and never alphabetical (F12). Exactly the two
# phases whose `NEXT_PHASES` entry has more than one member; the other edge
# out of each (`SETUP -> REGULAR_SEASON`, `REGULAR_SEASON ->
# REGULAR_SEASON_COMPLETE`) stays a legal graph edge reachable only through
# `force_advance`. `REGULAR_SEASON -> TRADE_DEADLINE` is the cadence's own
# second cycle (P5-71): the deadline is crossed when 45% of the schedule is
# played. Alphabetical order would pick `REGULAR_SEASON_COMPLETE` there.
_DEFAULT_SUCCESSOR: dict[Phase, Phase] = {
    Phase.SETUP: Phase.PRESEASON,
    Phase.REGULAR_SEASON: Phase.TRADE_DEADLINE,
}

# The parent's eight phases where an Entry press does nothing (F14). It is
# eight and not ten because the other two non-cycle-bearing phases do real
# work: `REGULAR_SEASON_COMPLETE` (the stats rebuild) and `PROGRESSION` (agency
# checkpoint #2). `/advance-to` steps only through these, so a phase added to
# this set that does work would let it walk past that work.
_NO_OP_PHASES: frozenset[Phase] = frozenset(
    {
        Phase.PLAYIN,
        Phase.PLAYOFFS_R1,
        Phase.PLAYOFFS_R2,
        Phase.CONFERENCE_FINALS,
        Phase.FINALS,
        Phase.AWARDS,
        Phase.POST_DRAFT_TRADES,
        Phase.OPTION_DECISIONS,
    }
)


def _route_successors(phase: Phase) -> frozenset[Phase]:
    """The edges `route_to` may walk out of `phase`: a branch point yields its
    one default successor, and the season-boundary edge `WAIVERS -> PRESEASON`
    yields nothing (F13)."""
    if phase is Phase.WAIVERS:
        return frozenset()
    if phase in _DEFAULT_SUCCESSOR:
        return frozenset({_DEFAULT_SUCCESSOR[phase]})
    return NEXT_PHASES[phase]


def route_to(from_phase: Phase, to_phase: Phase) -> tuple[Phase, ...] | None:
    """The phases entered, in order and ending at `to_phase`, walking from
    `from_phase` (the start itself is not listed) -- or `None` when no route
    exists within one season.

    BFS over `NEXT_PHASES` with branch points resolved through
    `_DEFAULT_SUCCESSOR`, never crossing `WAIVERS -> PRESEASON` (F13: nothing
    writes `current_season`, so crossing it loops the same season forever)
    and never targeting `SETUP` (only ever a source). The `SETUP` refusal is
    checked before `from == to`, so `SETUP -> SETUP` is `None`, not `()`.
    `from == to` is `()`.
    """
    if to_phase is Phase.SETUP:
        return None
    if from_phase == to_phase:
        return ()
    came_from: dict[Phase, Phase] = {}
    frontier = deque([from_phase])
    while frontier:
        current = frontier.popleft()
        for successor in sorted(_route_successors(current)):
            if successor == from_phase or successor in came_from:
                continue
            came_from[successor] = current
            if successor == to_phase:
                route = [successor]
                while came_from[route[-1]] != from_phase:
                    route.append(came_from[route[-1]])
                route.reverse()
                return tuple(route)
            frontier.append(successor)
    return None
