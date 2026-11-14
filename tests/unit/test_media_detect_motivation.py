"""`dba2/media/detect/motivation.py` -- MV-2b's ladder-state-at-a-past-instant
reconstruction and the satisfaction-swing helper."""

from __future__ import annotations

from datetime import UTC, datetime

from dba2.domain.motivation import MotivationAxis, PlayerMotivationEvent, RequestState
from dba2.domain.phase import Phase
from dba2.media.detect.motivation import request_state_at, satisfaction_swings

_LEAGUE = 1
_PLAYER = 1
_SEASON = 2026


def _event(
    *, created_at: datetime, from_state=None, to_state=None, axis=MotivationAxis.WINNING, delta=0
) -> PlayerMotivationEvent:
    return PlayerMotivationEvent(
        id=1, league_id=_LEAGUE, player_id=_PLAYER, season=_SEASON, phase=Phase.TRADE_DEADLINE,
        event_type="ladder_transition", axis=axis, delta=delta, note=None,
        created_at=created_at, from_state=from_state, to_state=to_state,
    )


_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_T1 = datetime(2026, 2, 1, tzinfo=UTC)
_T2 = datetime(2026, 3, 1, tzinfo=UTC)
_TRADE_AT = datetime(2026, 2, 15, tzinfo=UTC)


def test_no_transitions_reads_current_request_state():
    assert (
        request_state_at((), as_of=_TRADE_AT, current_request_state=RequestState.CONTENT)
        is RequestState.CONTENT
    )


def test_a_transition_strictly_after_reads_its_from_state():
    after = _event(
        created_at=_T2, from_state=RequestState.CONTENT, to_state=RequestState.UNSETTLED
    )
    result = request_state_at(
        (after,), as_of=_TRADE_AT, current_request_state=RequestState.UNSETTLED
    )
    assert result is RequestState.CONTENT


def test_no_transition_after_reads_the_latest_befores_to_state():
    before = _event(
        created_at=_T0, from_state=RequestState.UNSETTLED, to_state=RequestState.CONTENT
    )
    result = request_state_at(
        (before,), as_of=_TRADE_AT, current_request_state=RequestState.CONTENT
    )
    assert result is RequestState.CONTENT


def test_earliest_after_wins_when_several_transitions_follow_the_trade():
    later = _event(created_at=_T2, from_state=RequestState.TRADE_REQUESTED, to_state=None)
    earlier_after = _event(created_at=_T1, from_state=RequestState.CONTENT, to_state=None)
    result = request_state_at(
        (later, earlier_after), as_of=_T0, current_request_state=RequestState.CONTENT
    )
    assert result is RequestState.CONTENT


def test_latest_before_wins_when_several_transitions_precede_the_trade():
    earlier = _event(created_at=_T0, from_state=None, to_state=RequestState.UNSETTLED)
    later_before = _event(created_at=_T1, from_state=None, to_state=RequestState.CONTENT)
    result = request_state_at(
        (earlier, later_before), as_of=_T2, current_request_state=RequestState.CONTENT
    )
    assert result is RequestState.CONTENT


def test_a_pre_migration_row_on_the_relevant_leg_reads_as_no_reading_not_content():
    """A row written before migration 021 carries `from_state`/`to_state` both
    `NULL` -- landing on that row's own `to_state` (the BEFORE branch) must read
    `None`, never a fabricated `CONTENT`."""
    pre_migration = _event(created_at=_T0, from_state=None, to_state=None)
    result = request_state_at(
        (pre_migration,), as_of=_T2, current_request_state=RequestState.CONTENT
    )
    assert result is None


def test_satisfaction_swings_excludes_a_borderline_total():
    events = (
        _event(created_at=_T0, axis=MotivationAxis.WINNING, delta=14),
        _event(created_at=_T1, axis=MotivationAxis.MONEY, delta=15),
        _event(created_at=_T2, axis=MotivationAxis.LEGACY, delta=-15),
    )
    swings = satisfaction_swings(events)
    assert swings == {MotivationAxis.MONEY: 15, MotivationAxis.LEGACY: -15}
    assert MotivationAxis.WINNING not in swings


def test_satisfaction_swings_sums_multiple_events_on_the_same_axis():
    events = (
        _event(created_at=_T0, axis=MotivationAxis.ROLE_USAGE, delta=8),
        _event(created_at=_T1, axis=MotivationAxis.ROLE_USAGE, delta=8),
    )
    assert satisfaction_swings(events) == {MotivationAxis.ROLE_USAGE: 16}


def test_satisfaction_swings_empty_input_returns_empty():
    assert satisfaction_swings(()) == {}
