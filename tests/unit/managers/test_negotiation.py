"""Tier 1: `managers/negotiation.py` -- §11 step 4's own verify list
(docs/design/phase-3-inter-manager-society.md), followed bullet-for-bullet.

Every hand-built `DecisionRecord` below sets `verdict` via the EXACT same
rule `evaluate_trade` uses at `trade.py:710`
(`"accept" if score >= threshold else "reject"`), read directly this
dispatch -- `_verdict_for` mirrors that one line verbatim rather than
reimplementing a guess at what it does (this dispatch's own item 5).
Illegal records mirror `trade.py:648-652`'s real shape exactly:
`score=threshold=0.0`, `terms=()`.
"""
from __future__ import annotations

import itertools

import pytest

from dba2.domain.team import GmPhilosophy
from dba2.managers.explain import DecisionRecord, ReasonTerm
from dba2.managers.negotiation import (
    COUNTER_BAND_MULT,
    H2H_BAND_SHADE,
    H2H_MARGIN_SHADE,
    MIN_CONCESSION_FRAC,
    NegotiationFrame,
    NegotiationStance,
    counter_band,
    counter_band_eff,
    is_stalled,
    respond,
)
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow

_ILLEGAL_TOKENS = (
    "illegal:roster_max",
    "illegal:roster_min",
    "illegal:apron_2",
    "illegal:apron_1",
    "illegal:salary_matching",
)


def _verdict_for(score: float, threshold: float) -> str:
    """`trade.py:710`'s own rule, verbatim: `"accept" if score >= threshold
    else "reject"`."""
    return "accept" if score >= threshold else "reject"


def _record(
    *,
    score: float = 0.0,
    threshold: float = 40.0,
    verdict: str | None = None,
    out_player_count: int = 0,
    team_id: int = 1,
) -> DecisionRecord:
    """A hand-built `DecisionRecord`. `verdict=None` (the default)
    computes it via `_verdict_for` -- pass an explicit `illegal:*` token
    to build an illegal record, which also forces `score=threshold=0.0`
    and `terms=()`, matching `trade.py:648-652` exactly (Decision P3-6's
    trap: an illegal record's `gap` is `0.0`, which a gap-first check
    would misread as `ACCEPT`)."""
    if verdict is not None and verdict.startswith("illegal:"):
        return DecisionRecord(
            team_id=team_id, philosophy=GmPhilosophy.TENDENCY_RESPECTER,
            window=CompetitiveWindow.RETOOL, subject="t", verdict=verdict,
            score=0.0, threshold=0.0, terms=(),
        )
    resolved_verdict = verdict if verdict is not None else _verdict_for(score, threshold)
    terms = tuple(
        ReasonTerm(
            label=f"out:player:{i}", input_value=10.0, weight=-1.0, contribution=-10.0
        )
        for i in range(out_player_count)
    )
    return DecisionRecord(
        team_id=team_id, philosophy=GmPhilosophy.TENDENCY_RESPECTER,
        window=CompetitiveWindow.RETOOL, subject="t", verdict=resolved_verdict,
        score=score, threshold=threshold, terms=terms,
    )


# ---------------------------------------------------------------------------
# Decision P3-30's constants -- final values, declared once, at step 4.
# ---------------------------------------------------------------------------


def test_the_six_constants_are_declared_at_their_final_step_4_values():
    """Decision P3-30's table -- the four `negotiation.py`-scoped
    constants (the other two, H2H_SHADE_PRIOR/H2H_DECAY, live in
    memory.py and are asserted in test_memory.py)."""
    assert COUNTER_BAND_MULT == 3.0
    assert MIN_CONCESSION_FRAC == 0.10
    assert H2H_BAND_SHADE == 0.25
    assert H2H_MARGIN_SHADE == 0.25


# ---------------------------------------------------------------------------
# counter_band -- Decision P3-7's two worked rows, plus the constant-ratio
# property at S_out=600.
# ---------------------------------------------------------------------------


def test_counter_band_reproduces_the_two_worked_rows_to_within_half_a_percent():
    """§4.2/Decision P3-7's two worked cases: `120.000` VP at
    `S_out=455.0`, `389.106` VP at `S_out=2161.7`, both at the final
    `COUNTER_BAND_MULT=3.0`. Threshold is computed from the real
    `DEFAULT_TUNING` constants (`min_abs_gain=40.0`, `min_rel_gain=0.06`),
    not transcribed, so a future tuning sweep that moved either would
    move this test's own expectation rather than silently pass a stale
    number."""
    threshold_small = max(DEFAULT_TUNING.min_abs_gain, DEFAULT_TUNING.min_rel_gain * 455.0)
    threshold_star = max(DEFAULT_TUNING.min_abs_gain, DEFAULT_TUNING.min_rel_gain * 2161.7)
    assert threshold_small == pytest.approx(40.000, abs=1e-3)
    assert threshold_star == pytest.approx(129.702, abs=1e-3)

    band_small = counter_band(_record(score=0.0, threshold=threshold_small))
    band_star = counter_band(_record(score=0.0, threshold=threshold_star))
    assert band_small == pytest.approx(120.000, rel=0.005)
    assert band_star == pytest.approx(389.106, rel=0.005)


def test_counter_band_ratio_is_the_constant_3_0_at_every_s_out_including_600():
    """Decision P3-7: "the ratio column is constant by construction: 3.0
    in both rows, and at S_out=600 too" -- `counter_band` is
    `COUNTER_BAND_MULT * threshold` for ANY threshold, so this holds at
    every `S_out`, not only the two worked rows."""
    for s_out in (0.0, 455.0, 600.0, 2161.7, 50_000.0):
        threshold = max(DEFAULT_TUNING.min_abs_gain, DEFAULT_TUNING.min_rel_gain * s_out)
        record = _record(score=0.0, threshold=threshold)
        assert counter_band(record) == pytest.approx(COUNTER_BAND_MULT * threshold)
        if threshold > 0:
            assert counter_band(record) / threshold == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# counter_band_eff -- Decision P3-26/§8.2's channel 3 (step 12). Pure,
# exhaustive, no real corpus needed.
#
# `respond()`/`counter_band()` themselves are BYTE-IDENTICAL to their
# pre-step-12 selves (confirmed: `test_the_six_constants_are_declared_at_
# their_final_step_4_values` and every other pre-existing test in this
# file, unmodified, still passes) -- `gm_service.py::_run_negotiation`
# achieves channel 3's real, live effect on `respond()`'s own decision by
# substituting a SYNTHETIC copy of the acting side's `DecisionRecord`
# (same `verdict`/`terms`, `threshold` rescaled so `counter_band()`'s own
# unmodified arithmetic evaluates to `band_eff`, `score` shifted by the
# identical delta so `gap` is unchanged) rather than by adding a new
# parameter to `respond()`. A new keyword parameter under `managers/` --
# even a `None`-defaulted one -- trips `test_constant_inventory.py`'s
# mechanical Decision P3-30 guard (measured directly: it did, before this
# design was corrected), which forbids any new `managers/`-scoped
# declaration after step 4's one `GM_TUNING_VERSION` bump. The tests below
# exercise `respond()` through the exact same record-substitution shape
# `_run_negotiation` uses, so a rigorous test of the substitution is a
# rigorous test of channel 3's real, live effect on negotiation behavior --
# without needing to engineer real trade assets into a needle-width
# numeric window (measured directly to be infeasible with real assets --
# see test_head_to_head_writer.py's own controlled-probe test for the
# measured 5x miss).
# ---------------------------------------------------------------------------


def test_counter_band_eff_is_counter_band_at_shade_zero():
    """The one invariant every Phase 3 step has maintained: no memory (or
    an average opponent, `shade == 0.0`) reproduces today's unshaded
    behavior exactly, not approximately."""
    for s_out in (0.0, 455.0, 600.0, 2161.7, 50_000.0):
        threshold = max(DEFAULT_TUNING.min_abs_gain, DEFAULT_TUNING.min_rel_gain * s_out)
        record = _record(score=0.0, threshold=threshold)
        assert counter_band_eff(record, 0.0) == pytest.approx(counter_band(record))


def test_counter_band_eff_spans_the_theoretical_2_25_to_3_75_envelope_at_shade_plus_minus_1():
    """§8.2: `[2.25, 3.75]` around the final `COUNTER_BAND_MULT=3.0` base,
    at the two theoretical extremes `shade = +-1.0` (unreachable in
    practice at any real `p_bar != 0.5` -- §8.1 -- but the envelope this
    function's own arithmetic must never exceed)."""
    threshold = 100.0
    record = _record(score=0.0, threshold=threshold)
    assert counter_band_eff(record, 1.0) == pytest.approx(3.75 * threshold)
    assert counter_band_eff(record, -1.0) == pytest.approx(2.25 * threshold)


def _shaded_record(record: DecisionRecord, band_eff: float) -> DecisionRecord:
    """`gm_service.py::_run_negotiation`'s own substitution, reproduced
    here exactly: rescale `threshold` so `counter_band()`'s own unmodified
    arithmetic evaluates to `band_eff`, and shift `score` by the identical
    delta so `gap = score - threshold` is UNCHANGED."""
    import dataclasses as dc

    shaded_threshold = band_eff / COUNTER_BAND_MULT
    return dc.replace(
        record,
        threshold=shaded_threshold,
        score=record.score - (record.threshold - shaded_threshold),
    )


def test_shaded_record_substitution_preserves_gap_and_reproduces_band_eff():
    """The substitution's own two invariants, checked directly before
    trusting it to drive `respond()` below: `gap` unchanged, `counter_
    band()` of the synthetic record equal to the requested `band_eff`."""
    record = _record(score=250.0, threshold=100.0)
    original_gap = record.score - record.threshold
    for band_eff in (225.3, 300.0, 374.7):
        shaded = _shaded_record(record, band_eff)
        assert shaded.score - shaded.threshold == pytest.approx(original_gap)
        assert counter_band(shaded) == pytest.approx(band_eff)
        assert shaded.verdict == record.verdict
        assert shaded.terms == record.terms


def test_shaded_record_substitution_is_a_no_op_at_shade_zero():
    """`band_eff == band_base` (shade `0.0`) must produce a record whose
    `respond()` behavior is identical to the unshaded original -- the
    "no memory -> byte-identical" invariant, at the substitution site."""
    threshold = 100.0
    band_base = COUNTER_BAND_MULT * threshold
    for gap_mult, rounds_remaining in itertools.product(
        (-2.0, -1.0, -0.5, -0.01, 0.0, 1.0), (0, 1, 2),
    ):
        gap = gap_mult * band_base
        record_a = _record(score=threshold + gap, threshold=threshold, out_player_count=0)
        record_b = _record(score=1_000_000.0, threshold=threshold)
        shaded_a = _shaded_record(record_a, band_base)
        without_shading = respond(record_a, record_b, rounds_remaining=rounds_remaining)
        with_noop_shading = respond(shaded_a, record_b, rounds_remaining=rounds_remaining)
        assert without_shading == with_noop_shading


def test_shaded_record_substitution_is_two_sided_flip_and_null_elsewhere():
    """§11 step 12's own verify, at the pure mechanism level: the
    substitution must be able to WIDEN a would-be far reject into
    something else (COUNTER-reachable territory, here read as `EXPIRE` at
    `rounds_remaining=0` so this test needs no `build_counter`/real assets
    at all -- the flip this test cares about is `REJECT_TOO_FAR` vs
    "anything else," which is exactly what gm_service.py's own flip
    arithmetic (`(gap < -band_base) != (gap < -band_eff)`) compares),
    NARROW an in-band package INTO a far reject, and do neither when `gap`
    sits comfortably inside or comfortably outside both bands."""
    threshold = 100.0
    band_base = COUNTER_BAND_MULT * threshold  # 300.0
    record_b_accept = _record(score=1_000_000.0, threshold=threshold)  # B accepts -- A is short

    def _too_far(gap: float, band_eff: float) -> bool:
        record_a = _record(score=threshold + gap, threshold=threshold, out_player_count=0)
        shaded_a = _shaded_record(record_a, band_eff)
        stance = respond(shaded_a, record_b_accept, rounds_remaining=0)
        return stance == NegotiationStance.REJECT_TOO_FAR

    band_widened = 1.2 * band_base   # inside H2H_BAND_SHADE=0.25's own +25% ceiling
    band_narrowed = 0.8 * band_base  # inside its own -25% floor

    # -- Widening flip: gap sits between band_base and the widened band --
    gap_widen_zone = -1.1 * band_base
    assert _too_far(gap_widen_zone, band_base) is True, "must be far under the UNshaded baseline"
    assert _too_far(gap_widen_zone, band_widened) is False, (
        "must NOT be far once the acting side's own band widens past this gap"
    )

    # -- Narrowing flip: gap sits between the narrowed band and band_base --
    gap_narrow_zone = -0.9 * band_base
    assert _too_far(gap_narrow_zone, band_base) is False, "must be IN-band under the baseline"
    assert _too_far(gap_narrow_zone, band_narrowed) is True, (
        "must become far once the acting side's own band narrows past this gap"
    )

    # -- No flip: comfortably inside BOTH the widened and narrowed bands --
    gap_inside_both = -0.5 * band_base
    for band_eff in (band_base, band_widened, band_narrowed):
        assert _too_far(gap_inside_both, band_eff) is False

    # -- No flip: comfortably outside BOTH the widened and narrowed bands --
    gap_outside_both = -2.0 * band_base
    for band_eff in (band_base, band_widened, band_narrowed):
        assert _too_far(gap_outside_both, band_eff) is True


# ---------------------------------------------------------------------------
# respond -- Decision P3-6's ordering trap, all five illegal tokens.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("illegal_token", _ILLEGAL_TOKENS)
def test_every_illegal_token_returns_reject_illegal_never_accept(illegal_token):
    """Decision P3-6: an illegal record has `score=threshold=0.0`, so
    `gap=0.0`, so a gap-first stance function would read every illegal
    package as ACCEPT. Tests all five tokens, on both sides, against
    every other-side verdict shape (accept, reject, and a second illegal
    token) -- illegality must win regardless."""
    illegal = _record(verdict=illegal_token)
    accept = _record(score=100.0, threshold=40.0)
    reject = _record(score=-100.0, threshold=40.0, out_player_count=0)

    assert respond(illegal, accept, rounds_remaining=2) == NegotiationStance.REJECT_ILLEGAL
    assert respond(accept, illegal, rounds_remaining=2) == NegotiationStance.REJECT_ILLEGAL
    assert respond(illegal, reject, rounds_remaining=2) == NegotiationStance.REJECT_ILLEGAL
    assert respond(reject, illegal, rounds_remaining=2) == NegotiationStance.REJECT_ILLEGAL
    for other_token in _ILLEGAL_TOKENS:
        other = _record(verdict=other_token)
        assert respond(illegal, other, rounds_remaining=2) == NegotiationStance.REJECT_ILLEGAL
        assert respond(other, illegal, rounds_remaining=2) == NegotiationStance.REJECT_ILLEGAL

    # And never ACCEPT, explicitly -- the exact trap Decision P3-6 names.
    assert respond(illegal, accept, rounds_remaining=2) != NegotiationStance.ACCEPT
    assert respond(accept, illegal, rounds_remaining=2) != NegotiationStance.ACCEPT


def test_gap_positive_or_zero_agrees_with_evaluate_trades_own_verdict_rule_10000_records():
    """§11 step 4's verify: "gap >= 0 returns ACCEPT and agrees with
    evaluate_trade's own verdict on 10,000 hand-built records with zero
    disagreements." Each record's own `verdict` is assigned via
    `_verdict_for` -- `trade.py:710`'s real rule, read directly -- so
    this is a genuine identity check, not a test of a second, independent
    reimplementation: for every one of 10,000 randomized (score,
    threshold) pairs, `record.verdict == "accept"` iff
    `record.score - record.threshold >= 0`, and `respond` returns
    `ACCEPT` iff BOTH sides satisfy that identity."""
    import random

    rng = random.Random(3141592)
    disagreements = 0
    for _ in range(10_000):
        score_a = rng.uniform(-2000.0, 2000.0)
        threshold_a = rng.uniform(0.0, 500.0)
        score_b = rng.uniform(-2000.0, 2000.0)
        threshold_b = rng.uniform(0.0, 500.0)
        record_a = _record(score=score_a, threshold=threshold_a, out_player_count=3)
        record_b = _record(score=score_b, threshold=threshold_b, out_player_count=3)

        gap_a_nonneg = (record_a.score - record_a.threshold) >= 0
        gap_b_nonneg = (record_b.score - record_b.threshold) >= 0
        verdict_says_accept_a = record_a.verdict == "accept"
        verdict_says_accept_b = record_b.verdict == "accept"
        if gap_a_nonneg != verdict_says_accept_a or gap_b_nonneg != verdict_says_accept_b:
            disagreements += 1
            continue

        stance = respond(record_a, record_b, rounds_remaining=2)
        if verdict_says_accept_a and verdict_says_accept_b:
            if stance != NegotiationStance.ACCEPT:
                disagreements += 1
        elif stance == NegotiationStance.ACCEPT:
            # ACCEPT must never fire unless both sides' real verdict says so.
            disagreements += 1

    assert disagreements == 0


# ---------------------------------------------------------------------------
# §4.2's rule is total -- exhaustive over the verdict cross-product, by
# enumeration.
# ---------------------------------------------------------------------------


def _oracle(
    verdict_a: str, verdict_b: str, *, gap: float, band: float, rounds_remaining: int,
    axis_available: bool,
) -> NegotiationStance:
    """An independent re-statement of §4.2's own pseudocode, written
    without reference to `negotiation.py::respond`'s implementation --
    the enumeration test below cross-checks the real function against
    this oracle over the full representative grid, rather than the real
    function checking itself."""
    if verdict_a.startswith("illegal:") or verdict_b.startswith("illegal:"):
        return NegotiationStance.REJECT_ILLEGAL
    a_accepts = verdict_a == "accept"
    b_accepts = verdict_b == "accept"
    if a_accepts and b_accepts:
        return NegotiationStance.ACCEPT
    if not a_accepts and not b_accepts:
        return NegotiationStance.REJECT_DEADLOCK
    # Exactly one side short.
    if gap < -band:
        return NegotiationStance.REJECT_TOO_FAR
    if rounds_remaining <= 0:
        return NegotiationStance.EXPIRE
    if not axis_available:
        return NegotiationStance.REJECT_TOO_FAR
    return NegotiationStance.COUNTER


_VERDICT_GRID = ("accept", "reject", *_ILLEGAL_TOKENS)


@pytest.mark.parametrize("verdict_a,verdict_b", list(itertools.product(_VERDICT_GRID, repeat=2)))
def test_section_4_2_is_exhaustive_over_the_verdict_cross_product(verdict_a, verdict_b):
    """§11 step 4's verify: "§4.2's rule is exhaustive over the verdict
    cross-product, asserted by enumeration (not by testing a few
    hand-picked cases)." Enumerates all 7x7=49 (verdict_a, verdict_b)
    combinations; for the two "exactly one side short" cells, further
    enumerates a representative grid of gap-vs-band and
    rounds-remaining/axis-availability combinations."""
    is_illegal_a = verdict_a.startswith("illegal:")
    is_illegal_b = verdict_b.startswith("illegal:")
    threshold = 100.0
    band = COUNTER_BAND_MULT * threshold  # 300.0

    if is_illegal_a or is_illegal_b or verdict_a == verdict_b:
        # (accept,accept)/(reject,reject)/either-illegal: no further
        # sub-grid needed, since none of gap/rounds/axis matters.
        record_a = _record(verdict=verdict_a, score=0.0, threshold=threshold)
        record_b = _record(verdict=verdict_b, score=0.0, threshold=threshold)
        expected = _oracle(
            verdict_a, verdict_b, gap=0.0, band=band, rounds_remaining=2, axis_available=True,
        )
        assert respond(record_a, record_b, rounds_remaining=2) == expected
        return

    # Exactly one side short: (reject, accept) -> A short/give-less;
    # (accept, reject) -> B short/ask-more.
    a_short = verdict_a == "reject"
    for gap_mult, rounds_remaining, out_count in itertools.product(
        (-2.0, -1.0, -1.0 + 1e-9, -0.5, -0.01), (0, 1, 2), (0, 1, 2, 3, 4),
    ):
        gap = gap_mult * band  # always negative: the actor's own verdict is "reject"
        actor_score = threshold + gap
        if a_short:
            record_a = _record(score=actor_score, threshold=threshold, out_player_count=out_count)
            record_b = _record(score=1_000_000.0, threshold=threshold)
            axis_available = out_count >= 2  # give-less: needs |O| >= 2
        else:
            record_a = _record(score=1_000_000.0, threshold=threshold, out_player_count=out_count)
            record_b = _record(score=actor_score, threshold=threshold)
            axis_available = out_count < 3  # ask-more: needs |O| < max_outgoing_players (3)

        expected = _oracle(
            verdict_a, verdict_b, gap=gap, band=band, rounds_remaining=rounds_remaining,
            axis_available=axis_available,
        )
        actual = respond(record_a, record_b, rounds_remaining=rounds_remaining)
        assert actual == expected, (
            f"verdict_a={verdict_a} verdict_b={verdict_b} gap={gap} band={band} "
            f"rounds_remaining={rounds_remaining} out_count={out_count} "
            f"axis_available={axis_available}: expected {expected}, got {actual}"
        )


def test_reject_reject_cell_reaches_reject_deadlock():
    """Decision P3-14: `(reject, reject)` is its own terminal token,
    distinct from `reject_too_far` -- "we are both short and it cannot be
    bridged" is a different fact from "you are nowhere near.\""""
    record_a = _record(score=-100.0, threshold=40.0)
    record_b = _record(score=-50.0, threshold=40.0)
    assert respond(record_a, record_b, rounds_remaining=2) == NegotiationStance.REJECT_DEADLOCK


# ---------------------------------------------------------------------------
# is_stalled -- Decision P3-9 / architect round-10's C9, the actor-and-
# baseline semantics, by enumeration.
# ---------------------------------------------------------------------------


def test_is_stalled_actor_and_baseline_semantics_by_enumeration():
    """§11 step 4's verify: "the stall rule's own actor-and-baseline
    semantics are asserted by enumeration -- the acting side's own gap
    against its own band, compared only between same-actor turns, with a
    cell flip resetting the baseline rather than comparing across
    actors." See `negotiation.py::is_stalled`'s own docstring for why
    the turn-history bookkeeping this rule ultimately needs (which turn
    is "the previous same-actor turn") is deferred to step 7's loop --
    this test enumerates the pure predicate step 4 actually builds."""
    band = 300.0
    threshold = MIN_CONCESSION_FRAC * band  # 30.0 -- the exact boundary

    # First turn ever for this actor (no baseline) -> never stalled,
    # regardless of gap/band, enumerated over several gap values.
    for gap in (-1000.0, -1.0, 0.0, 1.0, 1000.0):
        assert is_stalled(
            actor=1, previous_actor=None, previous_gap=-500.0, gap=gap, band=band,
        ) is False

    # A cell flip (previous turn belonged to the OTHER actor) resets the
    # baseline -> never stalled, enumerated over both actor identities and
    # several gap combinations that WOULD stall if compared same-actor.
    for previous_gap, gap in itertools.product((-500.0, -100.0), (-499.0, -50.0)):
        assert is_stalled(
            actor=1, previous_actor=2, previous_gap=previous_gap, gap=gap, band=band,
        ) is False
        assert is_stalled(
            actor=2, previous_actor=1, previous_gap=previous_gap, gap=gap, band=band,
        ) is False

    # Same actor, concession >= MIN_CONCESSION_FRAC * band -> not stalled.
    # concession = |previous_gap| - |gap|
    assert is_stalled(
        actor=1, previous_actor=1, previous_gap=-500.0, gap=-500.0 + threshold, band=band,
    ) is False
    # |gap| grew (moved AWAY from zero) -- concession is negative, very much
    # below the bar, so this is stalled (a strong instance of "failed to
    # move |gap| toward zero," not an edge case).
    assert is_stalled(
        actor=1, previous_actor=1, previous_gap=-500.0, gap=-1000.0, band=band,
    ) is True

    # Same actor, concession strictly < MIN_CONCESSION_FRAC * band -> stalled.
    assert is_stalled(
        actor=1, previous_actor=1, previous_gap=-500.0, gap=-500.0 + threshold + 1.0, band=band,
    ) is False
    assert is_stalled(
        actor=1, previous_actor=1, previous_gap=-500.0, gap=-500.0 + threshold - 1.0, band=band,
    ) is True
    # Exactly at the boundary: concession == threshold -> NOT stalled ("at
    # least" the bar, per Decision P3-9's own wording, `<` not `<=`).
    assert is_stalled(
        actor=1, previous_actor=1, previous_gap=-500.0, gap=-500.0 + threshold, band=band,
    ) is False


def test_is_stalled_reads_nothing_but_its_own_gap_and_band_never_a_situation_token():
    """§7's own rule, restated as a structural property of this
    function's signature: `is_stalled` has no `situation`/token parameter
    at all, so it structurally cannot make the "reads the terminal token
    instead of the gap" mistake §7 documents as the natural but wrong
    implementation."""
    import inspect

    params = inspect.signature(is_stalled).parameters
    assert "situation" not in params
    assert "stance" not in params


# ---------------------------------------------------------------------------
# NegotiationFrame -- the dataclass shape only (no derivation function is
# built at step 4 -- see negotiation.py's own module docstring for why).
# ---------------------------------------------------------------------------


def test_negotiation_frame_numeric_half_constructs_and_is_frozen():
    frame = NegotiationFrame(
        gap_abs=0.0, gap_rel=0.0, gap_over_band=0.0, s_in=0.0, s_out=0.0, in_players=0,
        out_players=0, in_picks=0, out_picks=0, top_in_share=0.0, cap_relief_share=0.0,
    )
    import dataclasses

    assert frame.gap_abs == 0.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.gap_abs = 1.0  # type: ignore[misc]


def test_negotiation_frame_has_exactly_the_eleven_numeric_fields():
    """Pinning the field set so a later step (14) that builds the FULL
    frame in `voice/frame.py` cannot silently widen THIS dataclass
    instead of building its own -- §3's module layout keeps them
    separate on purpose. Eleven, not ten, since §11 step 15's B2 fix adds
    `gap_over_band` next to `gap_rel` (the field `gap_rel` itself cannot
    correctly serve as a `reject_too_far`-split trigger -- see `gap_rel`'s
    own corrected docstring)."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(NegotiationFrame)}
    assert field_names == {
        "gap_abs", "gap_rel", "gap_over_band", "s_in", "s_out", "in_players", "out_players",
        "in_picks", "out_picks", "top_in_share", "cap_relief_share",
    }
