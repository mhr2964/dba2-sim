"""Tier 1: `ReasonTerm`/`DecisionRecord` -- Decision P2-18's reconstruction
identity, per §12 step 1's own verify list: "`DecisionRecord`
reconstruction holds on a hand-built record."
(docs/design/phase-2-manager-minds.md)"""
from __future__ import annotations

from dba2.domain.team import GmPhilosophy
from dba2.managers.explain import DecisionRecord, ReasonTerm
from dba2.managers.tuning import CompetitiveWindow


def _hand_built_record(*, score: float) -> DecisionRecord:
    terms = (
        ReasonTerm(label="talent", input_value=84.0, weight=1.0, contribution=646.7),
        ReasonTerm(label="surplus", input_value=-60.0, weight=1.0, contribution=-60.0),
        ReasonTerm(label="need_bonus", input_value=0.20, weight=100.0, contribution=20.0),
    )
    return DecisionRecord(
        team_id=7,
        philosophy=GmPhilosophy.STAR_MAXER,
        window=CompetitiveWindow.CONTEND,
        subject="player:123",
        verdict="accept",
        score=score,
        threshold=40.0,
        terms=terms,
    )


def test_reconstructs_when_terms_sum_to_score():
    record = _hand_built_record(score=646.7 - 60.0 + 20.0)
    assert record.reconstruction_error() <= 1e-6
    assert record.reconstructs()


def test_does_not_reconstruct_when_terms_disagree_with_score():
    record = _hand_built_record(score=999.0)
    assert record.reconstruction_error() > 1e-6
    assert not record.reconstructs()


def test_reconstructs_respects_a_custom_tolerance():
    record = _hand_built_record(score=646.7 - 60.0 + 20.0 + 0.0005)
    assert not record.reconstructs(tolerance=1e-6)
    assert record.reconstructs(tolerance=1e-3)


def test_terms_are_reason_term_instances():
    record = _hand_built_record(score=606.7)
    assert all(isinstance(term, ReasonTerm) for term in record.terms)
