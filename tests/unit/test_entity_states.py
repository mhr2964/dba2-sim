"""phase-0-foundation.md §6.4 — the three entity machines' transition
tables, and the generic validator they share.
"""
from __future__ import annotations

from dba2.domain.game import GameStatus
from dba2.domain.phase import AwardBallotStatus, TradeStatus
from dba2.phase.entity_states import (
    AWARD_BALLOT_TRANSITIONS,
    GAME_TRANSITIONS,
    TRADE_TRANSITIONS,
    is_legal_transition,
)


def test_generic_validator_accepts_declared_edges_and_rejects_others():
    table = {GameStatus.SCHEDULED: frozenset({GameStatus.IN_PROGRESS})}
    assert is_legal_transition(table, GameStatus.SCHEDULED, GameStatus.IN_PROGRESS)
    assert not is_legal_transition(table, GameStatus.SCHEDULED, GameStatus.FINAL)
    # A `from_` state with no entry at all (not just an empty set) must not
    # raise — the generic validator treats "no row" as "no legal edges".
    assert not is_legal_transition(table, GameStatus.FINAL, GameStatus.SCHEDULED)


def test_game_transitions_cover_every_status():
    assert set(GAME_TRANSITIONS) == set(GameStatus)


def test_game_normal_path_is_legal():
    assert is_legal_transition(GAME_TRANSITIONS, GameStatus.SCHEDULED, GameStatus.IN_PROGRESS)
    assert is_legal_transition(GAME_TRANSITIONS, GameStatus.IN_PROGRESS, GameStatus.FINAL)


def test_game_final_allows_only_the_resim_edge_back_to_in_progress():
    """Decision P1-26 (phase-1-simulation-engine.md): `FINAL -> IN_PROGRESS`
    is the one legal, checked re-sim edge — every other `FINAL`-sourced
    transition is still illegal."""
    assert is_legal_transition(GAME_TRANSITIONS, GameStatus.FINAL, GameStatus.IN_PROGRESS)
    for status in GameStatus:
        if status is GameStatus.IN_PROGRESS:
            continue
        assert not is_legal_transition(GAME_TRANSITIONS, GameStatus.FINAL, status)


def test_game_postponed_always_resumes_to_scheduled_only():
    assert GAME_TRANSITIONS[GameStatus.POSTPONED] == frozenset({GameStatus.SCHEDULED})


def test_game_cannot_jump_scheduled_straight_to_final():
    assert not is_legal_transition(GAME_TRANSITIONS, GameStatus.SCHEDULED, GameStatus.FINAL)


def test_trade_transitions_cover_every_status():
    assert set(TRADE_TRANSITIONS) == set(TradeStatus)


def test_trade_proposed_transitions_match_the_doc_literally():
    """§6.4: proposed -> {accepted, rejected, countered, expired, vetoed}."""
    assert TRADE_TRANSITIONS[TradeStatus.PROPOSED] == frozenset(
        {
            TradeStatus.ACCEPTED,
            TradeStatus.REJECTED,
            TradeStatus.COUNTERED,
            TradeStatus.EXPIRED,
            TradeStatus.VETOED,
        }
    )


def test_trade_terminal_statuses_have_no_outgoing_edges():
    for status in (
        TradeStatus.ACCEPTED,
        TradeStatus.REJECTED,
        TradeStatus.COUNTERED,
        TradeStatus.EXPIRED,
        TradeStatus.VETOED,
    ):
        assert TRADE_TRANSITIONS[status] == frozenset()


def test_award_ballot_transitions_cover_every_status():
    assert set(AWARD_BALLOT_TRANSITIONS) == set(AwardBallotStatus)


def test_award_ballot_linear_path_open_closed_certified():
    assert is_legal_transition(
        AWARD_BALLOT_TRANSITIONS, AwardBallotStatus.OPEN, AwardBallotStatus.CLOSED
    )
    assert is_legal_transition(
        AWARD_BALLOT_TRANSITIONS, AwardBallotStatus.CLOSED, AwardBallotStatus.CERTIFIED
    )
    assert not is_legal_transition(
        AWARD_BALLOT_TRANSITIONS, AwardBallotStatus.OPEN, AwardBallotStatus.CERTIFIED
    )


def test_award_ballot_certified_is_terminal():
    assert AWARD_BALLOT_TRANSITIONS[AwardBallotStatus.CERTIFIED] == frozenset()
