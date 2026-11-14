"""phase-0-foundation.md §8 step 6's verify line, literally: every legal
edge asserted, a representative illegal edge from each phase rejected, and
the graph confirmed fully connected with `WAIVERS -> PRESEASON` closing the
loop.
"""
from __future__ import annotations

from dba2.domain.phase import Phase
from dba2.phase.graph import (
    _DEFAULT_SUCCESSOR,
    _NO_OP_PHASES,
    NEXT_PHASES,
    is_legal_transition,
    legal_next_phases,
    route_to,
)


def test_every_phase_is_a_key():
    """No phase silently missing an adjacency entry."""
    assert set(NEXT_PHASES) == set(Phase)


def test_every_legal_edge_is_accepted():
    for from_phase, targets in NEXT_PHASES.items():
        for to_phase in targets:
            assert is_legal_transition(from_phase, to_phase), (
                f"{from_phase} -> {to_phase} should be legal"
            )


def test_a_representative_illegal_edge_is_rejected_from_every_phase():
    for from_phase in Phase:
        legal = NEXT_PHASES[from_phase]
        # Any phase not in this phase's legal target set is an illegal edge.
        illegal_candidates = [p for p in Phase if p not in legal]
        assert illegal_candidates, f"{from_phase} has no illegal candidates to test against"
        for illegal_target in illegal_candidates:
            assert not is_legal_transition(from_phase, illegal_target), (
                f"{from_phase} -> {illegal_target} should be illegal"
            )


def test_legal_next_phases_matches_next_phases_dict():
    for phase in Phase:
        assert legal_next_phases(phase) == NEXT_PHASES[phase]


def test_the_two_documented_branch_points():
    """§6.2: exactly two phases have more than one legal next phase."""
    branch_points = {p: targets for p, targets in NEXT_PHASES.items() if len(targets) > 1}
    assert branch_points == {
        Phase.SETUP: frozenset({Phase.PRESEASON, Phase.REGULAR_SEASON}),
        Phase.REGULAR_SEASON: frozenset(
            {Phase.TRADE_DEADLINE, Phase.REGULAR_SEASON_COMPLETE}
        ),
    }


def test_graph_is_fully_connected_and_waivers_closes_the_loop_into_preseason():
    """Starting from SETUP, following NEXT_PHASES reaches every phase, and
    WAIVERS's only edge goes back to PRESEASON — closing the loop into the
    next season rather than dead-ending."""
    assert NEXT_PHASES[Phase.WAIVERS] == frozenset({Phase.PRESEASON})

    reachable: set[Phase] = set()
    frontier = [Phase.SETUP]
    while frontier:
        current = frontier.pop()
        if current in reachable:
            continue
        reachable.add(current)
        frontier.extend(NEXT_PHASES[current])

    assert reachable == set(Phase), (
        f"unreachable from SETUP: {set(Phase) - reachable}"
    )


def test_every_non_setup_phase_can_reach_every_other_non_setup_phase_via_the_loop():
    """WAIVERS -> PRESEASON closes PRESEASON..WAIVERS into a cycle, so every
    phase in that cycle can reach every other phase in it. SETUP is
    deliberately excluded: it's the one-time entry point at league creation
    and has no incoming edge from anywhere (nothing in a normal season
    rolls a league back to SETUP), so it is unreachable from inside the
    cycle — that is correct graph shape, not a defect."""
    non_setup = set(Phase) - {Phase.SETUP}
    for start in non_setup:
        reachable: set[Phase] = set()
        frontier = [start]
        while frontier:
            current = frontier.pop()
            if current in reachable:
                continue
            reachable.add(current)
            frontier.extend(NEXT_PHASES[current])
        assert reachable == non_setup, f"{start} cannot reach: {non_setup - reachable}"


# ---------------------------------------------------------------------------
# Stage E (P5-99.b, P5-99.f): the branch rule, the no-op set and `route_to`.
# ---------------------------------------------------------------------------


def test_default_successor_keys_are_exactly_the_branch_points_and_name_a_legal_edge():
    """`/advance` reads `_DEFAULT_SUCCESSOR` and nothing else at a branch, so
    it must cover every phase with more than one legal next phase, cover no
    other, and never name an edge the graph does not have."""
    branch_points = {p for p, targets in NEXT_PHASES.items() if len(targets) > 1}
    assert set(_DEFAULT_SUCCESSOR) == branch_points
    for phase, successor in _DEFAULT_SUCCESSOR.items():
        assert is_legal_transition(phase, successor)


def test_default_successor_is_the_documented_pair_not_an_alphabetical_pick():
    """`REGULAR_SEASON -> TRADE_DEADLINE` is the cadence's second cycle (P5-71);
    `REGULAR_SEASON_COMPLETE` sorts first alphabetically, so this literal is
    the witness that the rule is stated, never derived from name order."""
    assert _DEFAULT_SUCCESSOR == {
        Phase.SETUP: Phase.PRESEASON,
        Phase.REGULAR_SEASON: Phase.TRADE_DEADLINE,
    }


def test_no_op_phases_are_the_parents_eight():
    assert _NO_OP_PHASES == frozenset(
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


def test_progression_and_regular_season_complete_are_not_no_op_phases():
    """F14: both do real work (an agency checkpoint; the stats rebuild), so
    `/advance-to` must never walk through either."""
    assert Phase.PROGRESSION not in _NO_OP_PHASES
    assert Phase.REGULAR_SEASON_COMPLETE not in _NO_OP_PHASES


def test_route_to_a_straight_run_lists_every_phase_entered_and_not_the_start():
    assert route_to(Phase.PLAYIN, Phase.AWARDS) == (
        Phase.PLAYOFFS_R1,
        Phase.PLAYOFFS_R2,
        Phase.CONFERENCE_FINALS,
        Phase.FINALS,
        Phase.AWARDS,
    )
    assert route_to(Phase.PLAYIN, Phase.PLAYOFFS_R1) == (Phase.PLAYOFFS_R1,)


def test_route_to_resolves_regular_season_through_the_trade_deadline():
    """The witness an alphabetical resolution fails: the alphabetically first
    successor of `REGULAR_SEASON` is `REGULAR_SEASON_COMPLETE`, which would
    leave `TRADE_DEADLINE` unreachable."""
    assert route_to(Phase.REGULAR_SEASON, Phase.TRADE_DEADLINE) == (Phase.TRADE_DEADLINE,)
    assert route_to(Phase.REGULAR_SEASON, Phase.REGULAR_SEASON_COMPLETE) == (
        Phase.TRADE_DEADLINE,
        Phase.POST_DEADLINE,
        Phase.REGULAR_SEASON_COMPLETE,
    )


def test_route_to_resolves_setup_through_preseason():
    assert route_to(Phase.SETUP, Phase.PRESEASON) == (Phase.PRESEASON,)
    assert route_to(Phase.SETUP, Phase.REGULAR_SEASON) == (
        Phase.PRESEASON,
        Phase.REGULAR_SEASON,
    )


def test_route_to_refuses_any_route_crossing_the_season_boundary():
    """F13: `WAIVERS -> PRESEASON` closes the loop into next season, which
    nothing writes (F8) -- a walk across it would loop the same season."""
    assert route_to(Phase.WAIVERS, Phase.PRESEASON) is None
    assert route_to(Phase.WAIVERS, Phase.DRAFT) is None
    assert route_to(Phase.FREE_AGENCY, Phase.PRESEASON) is None
    assert route_to(Phase.DRAFT, Phase.REGULAR_SEASON) is None
    assert route_to(Phase.WAIVERS, Phase.REGULAR_SEASON) is None


def test_route_to_refuses_setup_as_a_target_even_from_setup():
    """`SETUP` is only ever a source (P5-99.b.2 row 6). The refusal is checked
    before the `from == to` case, so `SETUP -> SETUP` is `None`, not `()`."""
    for start in Phase:
        assert route_to(start, Phase.SETUP) is None


def test_route_to_the_same_phase_is_an_empty_route():
    for phase in set(Phase) - {Phase.SETUP}:
        assert route_to(phase, phase) == ()


def test_route_to_is_forward_only_within_one_cycle():
    assert route_to(Phase.AWARDS, Phase.PLAYIN) is None
    assert route_to(Phase.TRADE_DEADLINE, Phase.REGULAR_SEASON) is None


def test_route_to_from_every_phase_to_every_reachable_target_walks_only_legal_edges():
    for start in Phase:
        for target in Phase:
            route = route_to(start, target)
            if route is None:
                continue
            here = start
            for step in route:
                assert is_legal_transition(here, step), f"{here} -> {step} is not an edge"
                assert not (here is Phase.WAIVERS and step is Phase.PRESEASON)
                here = step
            assert here == target
