"""`domain/broadcast.py` -- the status vocabulary, the legal-move table and
the sweep's drain-shape classifier, with no database. The repository's use of
them against real Postgres is `tests/integration/test_broadcast_repo.py`.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dba2.domain.broadcast import (
    BROADCAST_TRANSITIONS,
    BroadcastStatus,
    GameBroadcast,
    SweepShape,
    is_legal_broadcast_transition,
)

_T = datetime(2026, 9, 19, tzinfo=UTC)


def _row(status, *, claimed=False, posted=False) -> GameBroadcast:
    return GameBroadcast(
        id=1, league_id=1, game_id=1, block_id="2026-11-01", root_message_id=None,
        thread_id=None, message_id=None, status=status,
        reveal_claimed_at=_T if claimed or posted else None,
        reveal_posted_at=_T if posted else None, created_at=_T,
    )


def test_status_vocabulary_is_the_designs_five():
    assert {s.value for s in BroadcastStatus} == {
        "pending", "queued", "live", "finished", "interrupted",
    }


def test_transition_table_covers_every_status_and_targets_only_real_ones():
    assert set(BROADCAST_TRANSITIONS) == set(BroadcastStatus)
    for targets in BROADCAST_TRANSITIONS.values():
        assert targets <= set(BroadcastStatus)


@pytest.mark.parametrize(
    "old,new",
    [
        (BroadcastStatus.PENDING, BroadcastStatus.QUEUED),
        (BroadcastStatus.QUEUED, BroadcastStatus.LIVE),
        (BroadcastStatus.QUEUED, BroadcastStatus.INTERRUPTED),
        (BroadcastStatus.LIVE, BroadcastStatus.FINISHED),
        (BroadcastStatus.LIVE, BroadcastStatus.INTERRUPTED),
    ],
)
def test_the_designs_transitions_are_legal(old, new):
    assert is_legal_broadcast_transition(old, new)


@pytest.mark.parametrize(
    "old,new",
    [
        (BroadcastStatus.PENDING, BroadcastStatus.LIVE),
        (BroadcastStatus.PENDING, BroadcastStatus.FINISHED),
        (BroadcastStatus.QUEUED, BroadcastStatus.FINISHED),
        (BroadcastStatus.LIVE, BroadcastStatus.QUEUED),
        (BroadcastStatus.FINISHED, BroadcastStatus.LIVE),
        (BroadcastStatus.FINISHED, BroadcastStatus.INTERRUPTED),
        (BroadcastStatus.INTERRUPTED, BroadcastStatus.FINISHED),
        (BroadcastStatus.INTERRUPTED, BroadcastStatus.PENDING),
        (BroadcastStatus.LIVE, BroadcastStatus.LIVE),
    ],
)
def test_everything_else_is_illegal(old, new):
    assert not is_legal_broadcast_transition(old, new)


@pytest.mark.parametrize(
    "row,shape",
    [
        (_row(BroadcastStatus.LIVE), SweepShape.INTERRUPTED_BROADCAST),
        (_row(BroadcastStatus.QUEUED), SweepShape.INTERRUPTED_BROADCAST),
        # precedence: a live row is an interrupted broadcast even if claimed
        (_row(BroadcastStatus.LIVE, claimed=True), SweepShape.INTERRUPTED_BROADCAST),
        (_row(BroadcastStatus.FINISHED, claimed=True), SweepShape.CRASHED_REVEAL),
        (_row(BroadcastStatus.INTERRUPTED, claimed=True), SweepShape.CRASHED_REVEAL),
        # precedence: a claimed pending row is a crashed reveal, not an orphan
        (_row(BroadcastStatus.PENDING, claimed=True), SweepShape.CRASHED_REVEAL),
        (_row(BroadcastStatus.PENDING), SweepShape.ORPHANED_PENDING),
        (_row(BroadcastStatus.FINISHED), SweepShape.UNREVEALED_ENDED),
        (_row(BroadcastStatus.INTERRUPTED), SweepShape.UNREVEALED_ENDED),
    ],
)
def test_sweep_shape_classification(row, shape):
    assert row.sweep_shape is shape
    assert not row.is_revealed


@pytest.mark.parametrize("status", list(BroadcastStatus))
def test_a_posted_reveal_needs_no_sweep_whatever_the_status(status):
    row = _row(status, posted=True)
    assert row.sweep_shape is None
    assert row.is_revealed
