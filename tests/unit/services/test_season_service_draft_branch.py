"""The DRAFT branch's and the unlock's pure halves (phase-5-stage-e-advance.md
P5-99.h, P5-99.b.2 rows 2 and 5, P5-99.c "/advance-unlock", build-plan commit 13):
which preconditions of the `DRAFT_LOTTERY -> DRAFT` edge a board is missing, the age
rule of the unlock, and the new vocabulary.

The board check is three independent facts, and its refusal names exactly the ones that
are missing -- a person told "there is no draft" when only the prospects are absent goes
looking in the wrong place.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from dba2.domain.transaction import TransactionType
from dba2.services import season_service
from dba2.services.season_service import (
    BlockerCode,
    DraftBoardFacts,
    PressKind,
    draft_board_blockers,
)

_READY = DraftBoardFacts(has_draft_row=True, resolved_picks=60, has_available_prospect=True)


def test_a_board_with_all_three_preconditions_has_no_blocker():
    assert draft_board_blockers(_READY) == []


@pytest.mark.parametrize(
    ("facts", "named", "not_named"),
    [
        (
            DraftBoardFacts(has_draft_row=False, resolved_picks=60, has_available_prospect=True),
            ["drafts row"],
            ["resolved pick", "available prospect"],
        ),
        (
            DraftBoardFacts(has_draft_row=True, resolved_picks=0, has_available_prospect=True),
            ["resolved pick"],
            ["drafts row", "available prospect"],
        ),
        (
            DraftBoardFacts(has_draft_row=True, resolved_picks=60, has_available_prospect=False),
            ["available prospect"],
            ["drafts row", "resolved pick"],
        ),
        (
            DraftBoardFacts(has_draft_row=False, resolved_picks=0, has_available_prospect=False),
            ["drafts row", "resolved pick", "available prospect"],
            [],
        ),
    ],
)
def test_the_refusal_names_exactly_the_missing_preconditions(facts, named, not_named):
    (blocker,) = draft_board_blockers(facts)
    assert blocker.code is BlockerCode.DRAFT_BOARD_NOT_READY
    for fragment in named:
        assert fragment in blocker.message
    for fragment in not_named:
        assert fragment not in blocker.message


def test_the_refusal_always_names_the_only_tool_and_the_two_open_questions():
    (blocker,) = draft_board_blockers(
        DraftBoardFacts(has_draft_row=False, resolved_picks=0, has_available_prospect=False)
    )
    assert "tools/dev_league_draft.py" in blocker.message
    assert "U1" in blocker.message and "U2" in blocker.message


def test_a_draft_resume_is_a_press_kind_of_its_own():
    assert PressKind.DRAFT_RESUME.value == "draft_resume"
    assert PressKind.DRAFT_RESUME not in {PressKind.BLOCKED, PressKind.ENTRY, PressKind.BLOCK}


# --- /advance-unlock (P5-99.c) ---------------------------------------------------------


def test_the_advance_unlock_audit_event_exists_in_the_transaction_vocabulary():
    assert TransactionType.ADVANCE_UNLOCK.value == "advance_unlock"


def test_the_young_claim_refusal_names_the_holder_the_age_and_the_wait_left():
    message = season_service.unlock_too_young_message(
        claimed_by=99, age=timedelta(minutes=3, seconds=20)
    )
    assert "user 99" in message
    assert "3 minute(s) old" in message
    assert "10 minutes" in message
    assert "7 minute(s)" in message, "600s - 200s = 400s, which rounds UP to 7 whole minutes"


def test_the_wait_left_never_rounds_down_to_a_claim_that_is_still_too_young():
    message = season_service.unlock_too_young_message(
        claimed_by=1, age=timedelta(minutes=9, seconds=59)
    )
    assert "1 minute(s)" in message


def test_the_young_claim_refusal_uses_the_threshold_it_was_given_and_says_to_wait():
    message = season_service.unlock_too_young_message(
        claimed_by=1, age=timedelta(minutes=5), min_age=timedelta(minutes=30)
    )
    assert "30 minutes" in message
    assert "25 minute(s)" in message
    assert "wait for it" in message and "twice" in message
