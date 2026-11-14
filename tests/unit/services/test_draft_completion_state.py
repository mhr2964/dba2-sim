"""Stage E's exit gate and `run_draft_cycle` must agree on what a finished draft
is (phase-5-stage-e-advance.md P5-99.e, build-plan commit 2), so the predicate
is one pure function both call. This file drives it directly over hand-built
boards -- five boards, one per way a draft can stand -- because the integration
suite reaches only the shapes a seeded league happens to produce, and the two
mutations the design names (an "eligible list is empty" completion test, and an
ignored human set) are each invisible to every board but one.
"""
from __future__ import annotations

import pytest

from dba2.domain.draft import DraftPick, ProtectionType
from dba2.services.gm_service import (
    DraftCycleReason,
    DraftCycleState,
    draft_completion_state,
)

_HUMAN_TEAM = 7
_AI_TEAM = 3


def _pick(
    pick_id: int,
    *,
    team: int,
    pick_number: int | None,
    used: int | None = None,
    round_: int = 1,
) -> DraftPick:
    return DraftPick(
        id=pick_id,
        league_id=1,
        season=2026,
        round=round_,
        original_team_id=team,
        current_team_id=team,
        pick_number=pick_number,
        used_for_player_id=used,
        protection_type=ProtectionType.NONE,
        protection_through=None,
        swap_with_pick_id=None,
        conveyed=False,
        rolls_over_to_season=None,
    )


def _board(*specs: tuple[int, int, int | None, int | None]) -> list[DraftPick]:
    """`(pick id, team, pick_number, used_for_player_id)` per pick."""
    return [
        _pick(pick_id, team=team, pick_number=number, used=used)
        for pick_id, team, number, used in specs
    ]


_HUMAN = frozenset({_HUMAN_TEAM})

# (picks, human team ids, expected state, expected reason, expected on-the-clock pick id)
_BOARDS = [
    pytest.param(
        _board((1, _AI_TEAM, 1, 100), (2, _HUMAN_TEAM, 2, None)),
        _HUMAN,
        DraftCycleState.WAITING_ON_HUMAN,
        None,
        2,
        id="human-on-the-clock",
    ),
    pytest.param(
        _board((1, _AI_TEAM, 1, 100), (2, _AI_TEAM, 2, None)),
        _HUMAN,
        DraftCycleState.STALLED,
        DraftCycleReason.POOL_EXHAUSTED,
        2,
        id="ai-on-the-clock-nobody-left-to-draft",
    ),
    pytest.param(
        _board((1, _AI_TEAM, 1, 100), (2, _AI_TEAM, None, None)),
        frozenset(),
        DraftCycleState.STALLED,
        DraftCycleReason.UNRESOLVED_PICKS,
        None,
        id="lottery-unresolved-eligible-list-empty",
    ),
    pytest.param(
        _board((1, _AI_TEAM, 1, 100), (2, _HUMAN_TEAM, 2, 101)),
        _HUMAN,
        DraftCycleState.COMPLETE,
        None,
        None,
        id="every-pick-used",
    ),
    pytest.param(
        _board((1, _AI_TEAM, None, None), (2, _HUMAN_TEAM, 2, None), (3, _AI_TEAM, 3, None)),
        _HUMAN,
        DraftCycleState.WAITING_ON_HUMAN,
        None,
        2,
        id="unresolved-pick-does-not-hide-the-clock",
    ),
]


@pytest.mark.parametrize(
    ("picks", "human_team_ids", "state", "reason", "on_the_clock_pick_id"), _BOARDS
)
def test_draft_completion_state_over_five_boards(
    picks, human_team_ids, state, reason, on_the_clock_pick_id
):
    completion = draft_completion_state(picks, human_team_ids)
    assert completion.state is state
    assert completion.reason is reason
    assert (completion.pick.id if completion.pick is not None else None) == on_the_clock_pick_id


def test_the_on_the_clock_pick_is_the_lowest_pick_number_not_the_first_listed():
    """The eligible list is sorted by `(round, pick_number, id)`; the board is
    handed over in whatever order the query returned."""
    picks = [
        _pick(9, team=_AI_TEAM, pick_number=30, round_=1),
        _pick(4, team=_HUMAN_TEAM, pick_number=5, round_=1),
    ]
    completion = draft_completion_state(picks, frozenset({_HUMAN_TEAM}))
    assert completion.pick is not None
    assert completion.pick.id == 4
    assert completion.state is DraftCycleState.WAITING_ON_HUMAN


def test_an_empty_board_is_complete_because_nothing_is_owed():
    """Vacuous, and deliberate: the exit gate must not read an empty `draft_picks`
    table as anything but COMPLETE here. Refusing to *enter* a phase with no board
    is the pre-entry check's job (P5-99.h), not this predicate's."""
    completion = draft_completion_state([], frozenset())
    assert completion.state is DraftCycleState.COMPLETE
    assert completion.reason is None
    assert completion.pick is None


def test_the_human_set_is_read_not_defaulted():
    """Same board, two human sets, two answers -- the twin of the empty-set
    mutation, on the one input that distinguishes them."""
    picks = [_pick(1, team=_HUMAN_TEAM, pick_number=1)]
    assert (
        draft_completion_state(picks, frozenset({_HUMAN_TEAM})).state
        is DraftCycleState.WAITING_ON_HUMAN
    )
    assert draft_completion_state(picks, frozenset()).state is DraftCycleState.STALLED
