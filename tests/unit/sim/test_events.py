"""Tier 1: the record shapes themselves (phase-1-simulation-engine.md
§4.5, §8.1). Full exhaustiveness-over-a-seeded-run (§9's Tier 2) is
`possession.py`'s concern (step 4, not built this dispatch) -- this pins
the closed set's membership and the `CreditEvent`/`TerminalOutcome`
same-shape guarantee so a future edit can't silently narrow either
without a test noticing."""
from __future__ import annotations

from dba2.sim.events import (
    BeatRecord,
    CreditEvent,
    PossessionResult,
    TerminalKind,
    TerminalOutcome,
    Zone,
)


def test_terminal_kind_is_exactly_decision_p1_14s_nine_member_closed_set():
    """Nine since docs/design/turnover-recovery.md TR-4.2 added
    `loose_ball_recovered`, the one member that is never terminal (TR-4.13).
    If this file is not in a diff that adds a member, the member was not
    added."""
    assert {member.value for member in TerminalKind} == {
        "made_fg",
        "missed_fg",
        "turnover_live",
        "turnover_dead",
        "shooting_foul",
        "non_shooting_foul",
        "offensive_foul",
        "loose_ball_recovered",
        "period_expired",
    }


def test_credit_event_is_the_identical_dataclass_as_terminal_outcome():
    """§4.5: "A CreditEvent is the same shape a terminal outcome's payload
    already has for the same real-world fact." -- one dataclass, two
    names, verified by identity rather than by field-list comparison so
    the two can never drift apart."""
    credit: CreditEvent = TerminalOutcome(kind=TerminalKind.NON_SHOOTING_FOUL, fouler_id=7)
    assert isinstance(credit, TerminalOutcome)
    # `CreditEvent` is a PEP 695 `type` alias (a `TypeAliasType` wrapper
    # object, not `TerminalOutcome` itself) -- `__value__` is the actual
    # aliased type, which is what must be identical to `TerminalOutcome`.
    assert CreditEvent.__value__ is TerminalOutcome


def test_possession_result_carries_exactly_terminal_and_credit_events():
    made = TerminalOutcome(
        kind=TerminalKind.MADE_FG, zone=Zone.THREE, assisted=True, scorer_id=1, passer_id=2
    )
    foul_credit = TerminalOutcome(kind=TerminalKind.NON_SHOOTING_FOUL, fouler_id=3)
    result = PossessionResult(terminal=made, credit_events=[foul_credit])
    assert result.terminal is made
    assert result.credit_events == [foul_credit]
    assert {f.name for f in __import__("dataclasses").fields(PossessionResult)} == {
        "terminal",
        "credit_events",
    }


def test_possession_result_defaults_to_no_credit_events():
    result = PossessionResult(terminal=TerminalOutcome(kind=TerminalKind.TURNOVER_DEAD))
    assert result.credit_events == []


def test_beat_record_carries_the_fields_decision_p1_23_names():
    record = BeatRecord(
        beat="screen",
        actor_ids={"screener": 1, "screen_defender": 2},
        outcome="blown",
        probabilities={
            "blown": 0.2,
            "over": 0.3,
            "under": 0.3,
            "switch": 0.1,
            "hedge_recover": 0.1,
        },
        clock_seconds=18.5,
        home_score=10,
        away_score=8,
    )
    assert record.beat == "screen"
    assert record.actor_ids["screener"] == 1
    assert abs(sum(record.probabilities.values()) - 1.0) < 1e-9
