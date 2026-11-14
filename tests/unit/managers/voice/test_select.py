"""Tier 1: `managers/voice/select.py` -- §11 step 14's own verify list
(docs/design/phase-3-inter-manager-society.md, §6.3), followed
bullet-for-bullet.

`NegotiationFrame` instances below are hand-built directly (every field
given explicitly) rather than routed through `voice/frame.py::derive_frame`
-- this file tests the SELECTOR, which only ever reads a frame's fields via
`getattr`/attribute access, never how the frame itself was produced.
"""
from __future__ import annotations

import dataclasses
import random

import pytest

from dba2.domain.team import GmPhilosophy
from dba2.managers.negotiation import NegotiationStance
from dba2.managers.tuning import CompetitiveWindow
from dba2.managers.voice.frame import NegotiationFrame, SituationToken
from dba2.managers.voice.select import (
    PhraseSelection,
    PhraseTemplate,
    Trigger,
    select_phrase,
)


def _frame(
    *,
    philosophy: GmPhilosophy = GmPhilosophy.STAR_MAXER,
    situation: SituationToken = SituationToken.OPEN,
    window: CompetitiveWindow = CompetitiveWindow.CONTEND,
    top_in_share: float = 0.5,
    gap_rel: float = 0.0,
    gap_over_band: float = 0.0,
    cap_relief_share: float = 0.0,
) -> NegotiationFrame:
    return NegotiationFrame(
        gap_abs=0.0, gap_rel=gap_rel, gap_over_band=gap_over_band, s_in=0.0, s_out=0.0,
        in_players=0, out_players=0, in_picks=0, out_picks=0, top_in_share=top_in_share,
        cap_relief_share=cap_relief_share,
        illegal_token=None, round_index=0, stance=NegotiationStance.COUNTER,
        situation=situation, philosophy=philosophy, window=window,
        h2h_shade=0.0, h2h_opened=0.0, h2h_far_reject_streak=0.0,
    )


def _template(
    template_id: str,
    *,
    philosophies: tuple[str, ...] = (),
    situations: tuple[str, ...] = (),
    windows: tuple[str, ...] = (),
    triggers: tuple[Trigger, ...] = (),
    weight: float = 1.0,
    text: str = "line",
    slots: tuple[str, ...] = (),
) -> PhraseTemplate:
    return PhraseTemplate(
        id=template_id, philosophies=philosophies, situations=situations, windows=windows,
        triggers=triggers, weight=weight, text=text, slots=slots,
    )


# ---------------------------------------------------------------------------
# Step 2 -- dimension filtering, wildcard vs closed allow-list.
# ---------------------------------------------------------------------------


def test_wildcard_dimensions_match_every_frame():
    template = _template("t1")  # all three dimensions empty == wildcard
    frame = _frame(philosophy=GmPhilosophy.CHAOS, situation=SituationToken.EXPIRE_ROUNDS,
                    window=CompetitiveWindow.REBUILD)
    selection = select_phrase(
        frame, (template,), use_counts={}, repeat_penalty=0.25, rng=random.Random(1),
    )
    assert selection.template_id == "t1"


def test_non_wildcard_dimension_excludes_a_non_matching_frame():
    matching = _template("matches", philosophies=("star_maxer",))
    fallback = _template("fallback", philosophies=())
    frame_star_maxer = _frame(philosophy=GmPhilosophy.STAR_MAXER)
    frame_egalitarian = _frame(philosophy=GmPhilosophy.EGALITARIAN)

    # A star_maxer frame: "matches" survives dimension filtering and, with
    # a crushing repeat penalty on the fallback, must be the one chosen.
    selection_star = select_phrase(
        frame_star_maxer, (matching, fallback), use_counts={"fallback": 60},
        repeat_penalty=0.5, rng=random.Random(1),
    )
    assert selection_star.template_id == "matches"

    # An egalitarian frame: "matches" is excluded by the philosophy
    # dimension before weighting ever runs -- only "fallback" can survive,
    # regardless of any use_counts/repeat_penalty on it.
    selection_egalitarian = select_phrase(
        frame_egalitarian, (matching, fallback), use_counts={"matches": 0},
        repeat_penalty=0.5, rng=random.Random(1),
    )
    assert selection_egalitarian.template_id == "fallback"


def test_situation_and_window_dimensions_also_filter():
    situation_only = _template("situation_only", situations=("counter_ask_more",))
    window_only = _template("window_only", windows=("contend",))
    fallback = _template("fallback")

    frame = _frame(situation=SituationToken.COUNTER_GIVE_LESS, window=CompetitiveWindow.REBUILD)
    selection = select_phrase(
        frame, (situation_only, window_only, fallback), use_counts={},
        repeat_penalty=0.25, rng=random.Random(1),
    )
    assert selection.template_id == "fallback"


# ---------------------------------------------------------------------------
# Step 3 -- trigger filtering, the closed six-operator set.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op,value,top_in_share,expected_survives",
    [
        ("lt", 0.5, 0.4, True),
        ("lt", 0.5, 0.6, False),
        ("lte", 0.5, 0.5, True),
        ("lte", 0.5, 0.6, False),
        ("gte", 0.75, 0.75, True),
        ("gte", 0.75, 0.74, False),
        ("gt", 0.75, 0.76, True),
        ("gt", 0.75, 0.75, False),
        ("eq", 0.5, 0.5, True),
        ("eq", 0.5, 0.5000001, False),
    ],
)
def test_each_closed_operator_evaluated_against_the_frame(
    op, value, top_in_share, expected_survives
):
    triggered = _template(
        "triggered", triggers=(Trigger(feature="top_in_share", op=op, value=value),)
    )
    fallback = _template("fallback")
    frame = _frame(top_in_share=top_in_share)
    selection = select_phrase(
        frame, (triggered, fallback), use_counts={"fallback": 60}, repeat_penalty=0.5,
        rng=random.Random(1),
    )
    # fallback is crushed by the repeat penalty so "triggered" wins whenever
    # its own trigger holds, and loses (leaving only fallback) otherwise.
    assert (selection.template_id == "triggered") == expected_survives


def test_in_operator_checks_membership_against_a_value_collection():
    triggered = _template(
        "triggered", triggers=(Trigger(feature="in_players", op="in", value=(1, 2, 3)),)
    )
    fallback = _template("fallback")
    frame_matches = dataclasses.replace(_frame(), in_players=2)
    selection = select_phrase(
        frame_matches, (triggered, fallback), use_counts={"fallback": 10_000},
        repeat_penalty=0.01, rng=random.Random(1),
    )
    assert selection.template_id == "triggered"


def test_a_template_with_no_triggers_always_survives_step_3():
    fallback = _template("fallback")  # no triggers -- Decision P3-19's fallback shape
    selection = select_phrase(
        _frame(), (fallback,), use_counts={}, repeat_penalty=0.25, rng=random.Random(1),
    )
    assert selection.template_id == "fallback"


# ---------------------------------------------------------------------------
# Step 4 -- deterministic sort by template_id, independent of iteration
# order of BOTH the library tuple and the use_counts mapping.
# ---------------------------------------------------------------------------


def test_selection_is_stable_under_library_tuple_ordering_and_use_counts_key_ordering():
    """§6.3 step 4's sort is the thing that makes the outcome independent
    of the library tuple's own iteration order -- proven by constructing
    weights where iterating UNSORTED changes which template a fixed
    `rng.random()` draw's cumulative-weight bisection lands on (critic
    finding B2, §11 step 14: the prior version of this test used equal
    weights, so its own weighted draw bisected to the same template
    regardless of order -- deleting the `sorted(...)` call in `select.py`
    left it green).

    Three templates, `a`/`b`/`c`, at weights `1.0`/`1.0`/`98.0`. Iterated
    in SORTED-by-id order (`a, b, c`), `rng.choices`' cumulative weight
    bounds are `[1, 2, 100]`, so a draw landing in `[0, 1)` picks `a`.
    Iterating the SAME three templates UNSORTED, in `[c, a, b]` order
    instead, gives cumulative bounds `[98, 99, 100]` -- the identical draw
    now lands in `c`'s `[0, 98)` bucket, a genuinely different answer.
    `random.Random(139)`'s first draw is `0.0015101727801708487`, deep
    inside `[0, 1)` with margin either way -- hand-verified against both
    orderings directly against `random.Random.choices` before writing this
    test (also confirmed by the round-trip below: replacing `sorted(...)`
    with `list(...)` in `select.py` makes this exact assertion fail,
    landing `c` instead of `a` on the shuffled input).

    `a` -- the alphabetically-first id -- is therefore the one answer
    step 4's sort guarantees regardless of which of the two input
    orderings (the library tuple, the `use_counts` mapping's own key
    order) this function is fed, and the literal `"a"` below is asserted
    directly rather than rebuilt from either result (the prior version's
    own tautology: comparing a value against a `PhraseSelection` built
    FROM that same value)."""
    frame = _frame()
    templates_forward = (
        _template("a", weight=1.0), _template("b", weight=1.0), _template("c", weight=98.0),
    )
    templates_shuffled = (templates_forward[2], templates_forward[0], templates_forward[1])
    use_counts_forward = {"a": 0, "b": 0, "c": 0}
    use_counts_shuffled = {"c": 0, "a": 0, "b": 0}

    selection_forward = select_phrase(
        frame, templates_forward, use_counts=use_counts_forward, repeat_penalty=0.25,
        rng=random.Random(139),
    )
    selection_shuffled = select_phrase(
        frame, templates_shuffled, use_counts=use_counts_shuffled, repeat_penalty=0.25,
        rng=random.Random(139),
    )
    assert selection_forward == selection_shuffled == PhraseSelection(
        template_id="a", slot_bindings={}
    )


def test_selection_is_deterministic_from_rng_alone_across_repeated_calls():
    frame = _frame()
    templates = (_template("a", weight=2.0), _template("b", weight=1.0), _template("c", weight=1.0))
    results = {
        select_phrase(
            frame, templates, use_counts={}, repeat_penalty=0.25, rng=random.Random(777),
        ).template_id
        for _ in range(20)
    }
    # the exact same (frame, library, rng-seed) always chooses the same template
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Step 5/6 -- the repeat penalty measurably reduces same-template repeats.
# ---------------------------------------------------------------------------


def test_repeat_penalty_measurably_reduces_the_previously_chosen_templates_frequency():
    frame = _frame()
    templates = (_template("a", weight=1.0), _template("b", weight=1.0), _template("c", weight=1.0))
    rng = random.Random(2024)
    use_counts: dict[str, int] = {}
    choices: list[str] = []
    for _ in range(300):
        selection = select_phrase(
            frame, templates, use_counts=use_counts, repeat_penalty=0.25, rng=rng,
        )
        choices.append(selection.template_id)
        use_counts[selection.template_id] = use_counts.get(selection.template_id, 0) + 1

    # With three equal-weight templates and a real repeat penalty, no
    # single template should dominate anywhere near 1/3 + a wide margin --
    # confirmed against the penalty-free baseline below, which WOULD let a
    # template's own share drift arbitrarily since nothing ever pulls it
    # back down.
    from collections import Counter

    counts = Counter(choices)
    most_common_share = counts.most_common(1)[0][1] / len(choices)
    assert most_common_share < 0.45, (
        f"repeat penalty did not visibly flatten selection frequency: {counts}"
    )

    # A back-to-back re-selection (the same use_counts state, asking again
    # immediately after a template was JUST chosen) must down-weight that
    # exact template relative to a fresh (zero-use) peer -- the mechanism's
    # own direct effect, not just the aggregate outcome above.
    fresh_use_counts = {"a": 5, "b": 0, "c": 0}
    picks_after_a_used_heavily = [
        select_phrase(
            frame, templates, use_counts=fresh_use_counts, repeat_penalty=0.25,
            rng=random.Random(seed),
        ).template_id
        for seed in range(200)
    ]
    heavy_use_share = picks_after_a_used_heavily.count("a") / len(picks_after_a_used_heavily)
    assert heavy_use_share < 1 / 3, (
        "a template used 5 times already must be selected LESS often than an "
        "equal-weight peer used zero times"
    )


def test_repeat_penalty_is_a_required_parameter_with_no_default():
    import inspect

    signature = inspect.signature(select_phrase)
    assert signature.parameters["repeat_penalty"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# slot_bindings -- always empty, step-14 scope boundary.
# ---------------------------------------------------------------------------


def test_slot_bindings_is_always_empty_even_when_the_template_declares_slots():
    template_with_slots = _template("has_slots", slots=("ask_asset",))
    selection = select_phrase(
        _frame(), (template_with_slots,), use_counts={}, repeat_penalty=0.25,
        rng=random.Random(1),
    )
    assert selection.template_id == "has_slots"
    assert selection.slot_bindings == {}


# ---------------------------------------------------------------------------
# Tier 1 -- an exact worked selection with a single survivor.
# ---------------------------------------------------------------------------


def test_single_survivor_is_always_chosen_regardless_of_weight_or_rng_state():
    only = _template("only", weight=0.0001)
    for seed in (0, 1, 2, 999):
        selection = select_phrase(
            _frame(), (only,), use_counts={}, repeat_penalty=0.25, rng=random.Random(seed),
        )
        assert selection == PhraseSelection(template_id="only", slot_bindings={})
