"""`/advance-force`'s target rule as a pure table (phase-5-stage-e-advance.md P5-99.g).

Force overrides blockers, not geometry: it refuses any target that is not strictly FORWARD
of the phase the league is in along the season route -- the rule `graph.route_to` states
(a route exists within one season, never into `SETUP`, never across `WAIVERS ->
PRESEASON`). Trimming the command's choice list is not the rule: a raw payload can name any
phase, so the service refuses, and each refusal has its own sentence.

The expected answer is written out from a hand-made season order, not read from
`route_to`, and then cross-checked against it.
"""

from __future__ import annotations

import pytest

from dba2.domain.phase import Phase
from dba2.phase.graph import route_to
from dba2.services import season_service

# The season route in order, written by hand. `REGULAR_SEASON -> REGULAR_SEASON_COMPLETE` is
# a legal graph edge force alone may take, but the route walks `TRADE_DEADLINE` and
# `POST_DEADLINE` first, so "forward" is simply "later in this list".
_SEASON_ORDER = [
    Phase.SETUP,
    Phase.PRESEASON,
    Phase.REGULAR_SEASON,
    Phase.TRADE_DEADLINE,
    Phase.POST_DEADLINE,
    Phase.REGULAR_SEASON_COMPLETE,
    Phase.PLAYIN,
    Phase.PLAYOFFS_R1,
    Phase.PLAYOFFS_R2,
    Phase.CONFERENCE_FINALS,
    Phase.FINALS,
    Phase.AWARDS,
    Phase.DRAFT_LOTTERY,
    Phase.DRAFT,
    Phase.POST_DRAFT_TRADES,
    Phase.PROGRESSION,
    Phase.OPTION_DECISIONS,
    Phase.RESIGN_WINDOW,
    Phase.FREE_AGENCY,
    Phase.WAIVERS,
]


def _expected(from_phase: Phase, to_phase: Phase) -> str | None:
    if to_phase is Phase.PRESEASON:
        return season_service.FORCE_PRESEASON_MESSAGE
    if to_phase is Phase.SETUP:
        return season_service.FORCE_SETUP_MESSAGE
    if to_phase is from_phase:
        return season_service.force_same_phase_message(from_phase)
    if _SEASON_ORDER.index(to_phase) < _SEASON_ORDER.index(from_phase):
        return season_service.force_not_forward_message(from_phase, to_phase)
    return None


def test_the_hand_made_season_order_covers_every_phase_once():
    assert sorted(_SEASON_ORDER) == sorted(Phase)


@pytest.mark.parametrize("from_phase", list(Phase))
def test_every_pair_is_refused_or_allowed_as_the_table_says(from_phase):
    for to_phase in Phase:
        assert season_service.force_target_refusal(from_phase, to_phase) == _expected(
            from_phase, to_phase
        ), (from_phase, to_phase)


@pytest.mark.parametrize("from_phase", list(Phase))
def test_what_is_allowed_is_exactly_what_route_to_can_route(from_phase):
    """The rule is `route_to`'s: an allowed target always has a route, and a target with no
    route is always refused (the two refusals `route_to` does not name -- the phase itself and
    `PRESEASON` -- are the service's own)."""
    for to_phase in Phase:
        refusal = season_service.force_target_refusal(from_phase, to_phase)
        route = route_to(from_phase, to_phase)
        if refusal is None:
            assert route, (from_phase, to_phase)
        elif route is None:
            assert refusal is not None
        else:
            # A route exists but force still refuses: only the phase it is in (route `()`)
            # or `PRESEASON` from `SETUP`/earlier, which the service names on its own.
            assert to_phase is from_phase or to_phase is Phase.PRESEASON, (from_phase, to_phase)


def test_each_kind_of_refusal_has_its_own_sentence():
    preseason = season_service.force_target_refusal(Phase.PLAYIN, Phase.PRESEASON)
    setup = season_service.force_target_refusal(Phase.PLAYIN, Phase.SETUP)
    same = season_service.force_target_refusal(Phase.PLAYIN, Phase.PLAYIN)
    backwards = season_service.force_target_refusal(Phase.AWARDS, Phase.PLAYIN)
    messages = {preseason, setup, same, backwards}
    assert None not in messages
    assert len(messages) == 4


def test_the_not_forward_refusal_names_both_phases():
    message = season_service.force_not_forward_message(Phase.DRAFT, Phase.REGULAR_SEASON)
    assert "DRAFT" in message and "REGULAR_SEASON" in message


# --- the missing-schedule refusal's predicate (P5-99.g, FIXES-3 m-1) -----------------------
# Written out by hand: the phases a schedule-less season is stranded from (or into).
_SCHEDULE_PHASES = {
    Phase.REGULAR_SEASON,
    Phase.TRADE_DEADLINE,
    Phase.POST_DEADLINE,
    Phase.REGULAR_SEASON_COMPLETE,
}


@pytest.mark.parametrize("from_phase", list(Phase))
def test_force_needs_a_schedule_out_of_setup_preseason_or_a_schedule_phase_or_into_one(
    from_phase,
):
    for to_phase in Phase:
        expected = (
            from_phase in {Phase.SETUP, Phase.PRESEASON}
            or from_phase in _SCHEDULE_PHASES
            or to_phase in _SCHEDULE_PHASES
        )
        assert season_service.force_needs_a_schedule(from_phase, to_phase) is expected, (
            from_phase,
            to_phase,
        )


@pytest.mark.parametrize("to_phase", [Phase.DRAFT, Phase.AWARDS, Phase.FREE_AGENCY, Phase.WAIVERS])
def test_out_of_setup_to_a_non_schedule_phase_has_its_own_sentence(to_phase):
    text = season_service.force_schedule_missing_message(Phase.SETUP, to_phase, 2026)
    assert "out of SETUP" in text and to_phase.value in text
    assert "stranded" in text and "Run /advance" in text
    assert "plays the regular-season schedule" not in text  # the target is not a schedule phase
