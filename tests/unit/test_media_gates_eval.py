"""The gates-v2 offline evaluation set as a test
(docs/design/media-v2-step8-gates-v2.md G2-9, G2-11, G2-12 item 2).

Pins the POOLED gate's outcome on every case, so the "before" number of the
design (19 of 20 negatives accepted) cannot drift silently, and the BOUND
gate's outcome, so the "after" cannot either. The negatives the bound gate
still accepts are asserted as PASSING on purpose: they are the classes the
design declares uncatchable (G2-14), and asserting them is what stops a
reader believing either gate is a fact-checker."""
from __future__ import annotations

import pytest

import tools.media_gate_eval as gate_eval
from dba2.media.write.verify import NumberPolicy

CASES = gate_eval.load_cases()
BY_ID = {c.id: c for c in CASES}

POOLED_REJECTS = frozenset({"N09", "D13"})
"""The original case the pooled gate rejects: an invented decimal no pool
contains (a sanity row). And D13: the pooled gate still reads the digits of
"76ers" as a number (it is not the shipped gate, so it is left as it was)."""

LIVE_1704 = ("3605", "3597", "3599", "3606", "3610", "3611", "3598", "3609",
             "3589", "3587", "3588", "3586")
"""The gates-v2 live run's direction cases, verbatim (text, package, brief)."""

WRONG_DIRECTION_LIVE = ("3605", "3597", "3599", "3606", "3610", "3611")
"""Accepted live, each stating its own standings move in the wrong direction."""

WRONG_DIRECTION_SYNTHETIC = ("D07", "D08", "D09", "D10", "D11", "D12")

POOLED_REJECTS_DIRECTION = frozenset(
    {"3605", "3597", "3610", "3611", "3589", "3587", "3588", "3586", "D02"}
)
"""The pooled gate predates the `derived` list: the live texts quoting a
derived win percentage or games-behind figure, and D02's prior position
(22nd), are not in its pool."""

PRIOR_POSITION_UNBOUND = frozenset({"D01", "D02"})
"""Without the derived list (the move phrase): the position a team moved
FROM binds to nothing."""

BOUND_BLIND_SPOTS = frozenset({"N13", "N14", "N16"})
"""Negatives the bound gate still accepts, each a G2-14 class: a league
number stated about a team, two of the same team's numbers swapped, a true
number under a false qualifier."""

OTHER_ENTITY = ("N01", "N02", "N03", "N06", "N07", "N08", "N15", "N17", "N18", "N19", "N20")
"""G2-11 item 1: the negatives whose number belongs to a different entity
(or to no entity at all in that shape)."""


def test_fixture_composition_matches_the_design():
    labels = [c.label for c in CASES]
    assert len(CASES) == 75
    assert labels.count("negative") == 26
    assert sum(1 for c in CASES if c.source == "synthetic" and c.label != "negative") == 27
    assert sum(1 for c in CASES if c.source != "synthetic") == 22
    assert labels.count("clean") == 30 and labels.count("derived_true") == 11
    assert labels.count("rhetorical") == 1 and labels.count("false_claim") == 7
    assert len(BY_ID) == 75
    assert tuple(c.id for c in CASES if c.source == "g2-live-1704") == LIVE_1704


def test_live_cases_use_their_own_leagues_alias_table():
    """League 1704 reuses the hand-built cases' team names under other ids."""
    live = BY_ID["3605"].team_aliases
    assert all(tid > 12000 for tid in live)
    assert BY_ID["D01"].team_aliases is live  # a mutation inherits its base's table
    assert all(tid < 100 for tid in BY_ID["N01"].team_aliases)


# -- before: the pooled gate ------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_pooled_gate_outcome_is_pinned(case):
    rejected = POOLED_REJECTS | POOLED_REJECTS_DIRECTION
    assert gate_eval.evaluate(case, NumberPolicy.POOLED).passed is (case.id not in rejected)


def test_pooled_gate_accepts_25_of_26_negatives():
    negatives = [c for c in CASES if c.label == "negative"]
    assert sum(gate_eval.evaluate(c, NumberPolicy.POOLED).passed for c in negatives) == 25


# -- after: the bound gate ---------------------------------------------------


def _bound_accepts(case) -> bool:
    if case.label in gate_eval.TRUE_LABELS:
        return True
    return case.id in BOUND_BLIND_SPOTS


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_bound_gate_outcome_is_pinned(case):
    result = gate_eval.evaluate(case, NumberPolicy.BOUND)
    assert result.passed is _bound_accepts(case), result.offending


def test_bound_gate_accepts_no_other_entity_negative():
    accepted = [i for i in OTHER_ENTITY if gate_eval.evaluate(BY_ID[i], NumberPolicy.BOUND).passed]
    assert accepted == []


def test_every_bound_survivor_is_a_documented_blind_spot():
    survivors = [c for c in CASES if c.label == "negative"
                 and gate_eval.evaluate(c, NumberPolicy.BOUND).passed]
    assert {c.id for c in survivors} == BOUND_BLIND_SPOTS
    assert all("documented blind spot" in c.note for c in survivors)


@pytest.mark.parametrize("case_id", [*WRONG_DIRECTION_LIVE, *WRONG_DIRECTION_SYNTHETIC])
def test_every_wrong_direction_move_is_rejected_by_the_claims_gate(case_id):
    """The 6 live stories accepted with their standings move backwards, and
    the 6 synthetic flips: every one is now a C4 direction failure."""
    result = gate_eval.evaluate(BY_ID[case_id], NumberPolicy.BOUND)
    assert result.gate == "claims"
    assert all(line.startswith("move direction") for line in result.offending)


def test_live_story_3605_names_both_backwards_spans():
    result = gate_eval.evaluate(BY_ID["3605"], NumberPolicy.BOUND)
    assert [line.split(" ->")[0] for line in result.offending] == [
        "move direction 'up' '4 spots'", "move direction 'up' '4 places'",
    ]


def test_every_live_move_in_the_true_direction_or_no_direction_is_accepted():
    ids = ("3598", "3609", "3589", "3587", "3588", "3586")
    assert [i for i in ids if gate_eval.evaluate(BY_ID[i], NumberPolicy.BOUND).passed] == list(ids)


def test_the_rhetorical_story_is_the_one_accepted_false_reject():
    rejected_true = [c.id for c in CASES if c.label not in ("negative", "false_claim")
                     and not gate_eval.evaluate(c, NumberPolicy.BOUND).passed]
    assert rejected_true == ["3281"]  # "Not 2. Not 3." is true of nothing


def test_digits_inside_a_team_name_are_not_a_number_d13():
    """Second gates-v2 live pass, story 3858: "76ers" was read as 76 and
    rejected as bound to nothing (its brief did not carry the name)."""
    result = gate_eval.evaluate(BY_ID["D13"], NumberPolicy.BOUND)
    assert result.passed, result.offending


def test_3291_games_left_is_rejected_by_the_claims_gate_with_the_span():
    result = gate_eval.evaluate(BY_ID["3291"], NumberPolicy.BOUND)
    assert result.gate == "claims"
    assert any("'1 game left'" in line for line in result.offending)


def test_a_misattribution_names_both_entities():
    result = gate_eval.evaluate(BY_ID["N01"], NumberPolicy.BOUND)
    assert result.offending == ("record 24-13 -> Los Angeles Lakers; bound to Denver Nuggets",)


def test_without_the_derived_list_two_true_derivations_are_rejected():
    """G2-7's ordering constraint: BOUND must not ship before `derived`."""
    rejected = [c.id for c in CASES if c.label == "derived_true"
                and not gate_eval.evaluate(c, NumberPolicy.BOUND, derived=False).passed]
    assert rejected == ["3290", "P19", "3589", "3587", "3588", "3586",
                        *sorted(PRIOR_POSITION_UNBOUND)]


# -- the tool ------------------------------------------------------------------


def test_tool_prints_the_three_columns_and_makes_no_model_call(capsys, monkeypatch):
    import dba2.services.media_writer_llm as llm

    def _refuse(*args, **kwargs):
        raise AssertionError("the offline eval must never construct a writer")

    monkeypatch.setattr(llm.AnthropicWriter, "__init__", _refuse)
    assert gate_eval.main(["--policy", "pooled"]) == 0
    out = capsys.readouterr().out
    assert "negative      26 |                25                 3                 3" in out
    assert "N01" in out  # accepted under pooled, listed as a disagreement
    assert gate_eval.main(["--policy", "bound"]) == 0
    out = capsys.readouterr().out
    assert "N13" in out and "N01 " not in out
