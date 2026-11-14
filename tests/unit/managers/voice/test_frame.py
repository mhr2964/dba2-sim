"""Tier 1: `managers/voice/frame.py` -- §11 step 14's own verify list
(docs/design/phase-3-inter-manager-society.md), followed bullet-for-bullet.

Every hand-built `DecisionRecord` mirrors `test_negotiation.py`'s own
`_record` convention: an `illegal:*` verdict forces `score=threshold=0.0`,
`terms=()` (`trade.py:648-652`'s real shape) unless the test explicitly
needs a POISONED record (see `_poisoned_illegal_record` below) to assert
the short-circuit structurally rather than by absence of an exception.
"""
from __future__ import annotations

import dataclasses

import pytest

from dba2.domain.team import GmPhilosophy
from dba2.managers.explain import DecisionRecord, ReasonTerm
from dba2.managers.negotiation import COUNTER_BAND_MULT, NegotiationStance, counter_band, respond
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow
from dba2.managers.voice.frame import NegotiationFrame, SituationToken, derive_frame

_FAR_REJECT_GAP_MULTIPLE = 10.0
"""How far below the `REJECT_TOO_FAR` cutoff the worked far-reject test
record below sits, in units of `threshold` -- an arbitrary, comfortably
large multiple (the critic's own reproduction), not a value with any
independent meaning."""

_ILLEGAL_TOKENS = (
    "illegal:roster_max",
    "illegal:roster_min",
    "illegal:apron_2",
    "illegal:apron_1",
    "illegal:salary_matching",
)


@dataclasses.dataclass
class _H2HDouble:
    """A hand-built double carrying the same three fields the real
    `HeadToHead` dataclass does that this module's code path touches
    (`opened`, `accepts_by_opponent`, `far_reject_streak`) -- `derive_
    frame` is typed against `HeadToHead | None` (critic finding B3, §11
    step 14), not the narrower `HeadToHeadLike` protocol `shade()` itself
    uses, since `shade()`'s own contract only needs `opened`/`accepts_by_
    opponent` while `derive_frame` additionally reads `far_reject_streak`.
    This double satisfies the real dataclass's shape structurally, exactly
    as `memory.py::target_count_for_pair`'s own tests already do against
    the same real type one function over."""

    opened: float
    accepts_by_opponent: float
    far_reject_streak: float


class _PoisonNumber:
    """A number-shaped object that raises on every arithmetic dunder the
    legal path's `gap_abs`/`gap_rel` computation could reach -- used to
    prove STRUCTURALLY that the illegality short-circuit never touches
    `record.score`/`record.threshold`, rather than merely observing "no
    exception was raised" on an accidentally-safe input (§11 step 14's own
    verify: "the short-circuit is asserted structurally... not merely by
    absence of an exception")."""

    def __sub__(self, other):
        raise AssertionError("arithmetic ran on the illegal short-circuit path")

    def __rsub__(self, other):
        raise AssertionError("arithmetic ran on the illegal short-circuit path")

    def __truediv__(self, other):
        raise AssertionError("arithmetic ran on the illegal short-circuit path")

    def __rtruediv__(self, other):
        raise AssertionError("arithmetic ran on the illegal short-circuit path")


class _PoisonTerms:
    """An iterable-shaped object that raises the moment anything tries to
    iterate it -- proves `record.terms` is never walked (no `sum`, no list
    comprehension) on the illegal path."""

    def __iter__(self):
        raise AssertionError("record.terms was iterated on the illegal short-circuit path")


def _poisoned_illegal_record(illegal_token: str) -> DecisionRecord:
    """An `illegal:*` `DecisionRecord` whose `score`/`threshold`/`terms`
    are POISONED (see above) rather than the real `0.0`/`0.0`/`()` shape --
    if `derive_frame` ever performed a division, a `max`, or a sum over
    `terms` on this record, the poison raises immediately. `philosophy`/
    `window`/`subject`/`verdict` are left real, since the short-circuit
    DOES read `verdict` (to detect illegality) and `philosophy`/`window`
    (§6.1's table: "off the record, as given," on both paths)."""
    return DecisionRecord(
        team_id=1,
        philosophy=GmPhilosophy.TENDENCY_RESPECTER,
        window=CompetitiveWindow.RETOOL,
        subject="t",
        verdict=illegal_token,
        score=_PoisonNumber(),  # type: ignore[arg-type]
        threshold=_PoisonNumber(),  # type: ignore[arg-type]
        terms=_PoisonTerms(),  # type: ignore[arg-type]
    )


def _legal_record(
    *,
    score: float,
    threshold: float,
    terms: tuple[ReasonTerm, ...] = (),
    team_id: int = 1,
) -> DecisionRecord:
    verdict = "accept" if score >= threshold else "reject"
    return DecisionRecord(
        team_id=team_id,
        philosophy=GmPhilosophy.STAR_MAXER,
        window=CompetitiveWindow.CONTEND,
        subject="t",
        verdict=verdict,
        score=score,
        threshold=threshold,
        terms=terms,
    )


# ---------------------------------------------------------------------------
# SituationToken -- the closed ten-token enum, verbatim.
# ---------------------------------------------------------------------------


def test_situation_token_has_exactly_the_ten_tokens_from_section_6_2():
    assert {member.value for member in SituationToken} == {
        "open", "accept_round0", "accept_after_counter", "counter_ask_more",
        "counter_give_less", "reject_too_far", "reject_deadlock", "reject_illegal",
        "expire_stalled", "expire_rounds",
    }
    assert len(SituationToken) == 10


# ---------------------------------------------------------------------------
# NegotiationFrame -- the full shape, frozen, extends the numeric-only base.
# ---------------------------------------------------------------------------


def test_negotiation_frame_has_all_twenty_fields_and_is_frozen():
    """Twenty, not nineteen -- §11 step 15's B2 fix adds `gap_over_band`
    to the eleven-field numeric base (see `test_negotiation.py`'s own
    field-count test for why `gap_rel` alone cannot serve as the
    `reject_too_far`-split trigger)."""
    field_names = {f.name for f in dataclasses.fields(NegotiationFrame)}
    assert field_names == {
        # the eleven numeric fields, inherited from negotiation.NegotiationFrame
        "gap_abs", "gap_rel", "gap_over_band", "s_in", "s_out", "in_players",
        "out_players", "in_picks", "out_picks", "top_in_share", "cap_relief_share",
        # the nine full-frame fields, added here
        "illegal_token", "round_index", "stance", "situation", "philosophy",
        "window", "h2h_shade", "h2h_opened", "h2h_far_reject_streak",
    }
    frame = derive_frame(
        _legal_record(score=100.0, threshold=40.0),
        stance=NegotiationStance.ACCEPT, situation=SituationToken.ACCEPT_ROUND0,
        round_index=0, head_to_head=None, p_bar=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.gap_abs = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The illegality short-circuit -- Decision P3-15, all five tokens, asserted
# STRUCTURALLY via the poisoned record above.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("illegal_token", _ILLEGAL_TOKENS)
def test_illegal_short_circuit_is_structural_not_merely_exception_free(illegal_token):
    """No division, no `max`, no sum over `terms` is reached on the
    illegal path -- proven by poisoning `score`/`threshold`/`terms` so any
    of those operations would raise, then asserting the call still
    succeeds and produces exactly §6.1's short-circuit values."""
    record = _poisoned_illegal_record(illegal_token)
    frame = derive_frame(
        record,
        stance=NegotiationStance.ACCEPT,  # deliberately wrong -- must be overridden
        situation=SituationToken.OPEN,  # deliberately wrong -- must be overridden
        round_index=2,
        head_to_head=_H2HDouble(opened=5.0, accepts_by_opponent=3.0, far_reject_streak=1.0),
        p_bar=0.75,
    )
    assert frame.gap_abs == 0.0
    assert frame.gap_rel == 0.0
    assert frame.s_in == 0.0
    assert frame.s_out == 0.0
    assert frame.in_players == 0
    assert frame.out_players == 0
    assert frame.in_picks == 0
    assert frame.out_picks == 0
    assert frame.top_in_share == 0.0
    assert frame.cap_relief_share == 0.0
    assert frame.illegal_token == illegal_token.removeprefix("illegal:")
    assert frame.round_index == 2
    assert frame.stance == NegotiationStance.REJECT_ILLEGAL
    assert frame.situation == SituationToken.REJECT_ILLEGAL
    assert frame.philosophy == GmPhilosophy.TENDENCY_RESPECTER
    assert frame.window == CompetitiveWindow.RETOOL


def test_illegal_short_circuit_h2h_fields_are_not_zeroed_by_the_short_circuit_itself():
    """§6.1's short-circuit template lists specific fields as `0.0` and
    omits every `h2h_*` field -- they are computed identically on the
    legal and illegal paths, off the caller's own `head_to_head`/`p_bar`."""
    h2h = _H2HDouble(opened=10.0, accepts_by_opponent=8.0, far_reject_streak=2.0)
    frame = derive_frame(
        _poisoned_illegal_record("illegal:apron_1"),
        stance=NegotiationStance.COUNTER, situation=SituationToken.COUNTER_ASK_MORE,
        round_index=1, head_to_head=h2h, p_bar=0.7709,
    )
    assert frame.h2h_opened == 10.0
    assert frame.h2h_far_reject_streak == 2.0
    assert frame.h2h_shade != 0.0  # a real opponent-history reading, not the no-memory zero


# ---------------------------------------------------------------------------
# The no-memory case -- §6.1(b).
# ---------------------------------------------------------------------------


def test_no_memory_context_produces_the_three_zero_memory_fields():
    frame = derive_frame(
        _legal_record(score=100.0, threshold=40.0),
        stance=NegotiationStance.ACCEPT, situation=SituationToken.ACCEPT_ROUND0,
        round_index=0, head_to_head=None, p_bar=0.7709,
    )
    assert frame.h2h_shade == 0.0
    assert frame.h2h_opened == 0.0
    assert frame.h2h_far_reject_streak == 0.0


def test_no_memory_case_also_holds_when_p_bar_is_none_with_a_real_head_to_head_row():
    """`shade()` zeroes on `p_bar is None` even with real memory -- but
    `h2h_opened`/`h2h_far_reject_streak` are read off the row directly and
    are NOT gated by `p_bar` at all."""
    h2h = _H2HDouble(opened=14.5, accepts_by_opponent=10.0, far_reject_streak=3.0)
    frame = derive_frame(
        _legal_record(score=100.0, threshold=40.0),
        stance=NegotiationStance.ACCEPT, situation=SituationToken.ACCEPT_ROUND0,
        round_index=0, head_to_head=h2h, p_bar=None,
    )
    assert frame.h2h_shade == 0.0
    assert frame.h2h_opened == 14.5
    assert frame.h2h_far_reject_streak == 3.0


# ---------------------------------------------------------------------------
# top_in_share -- §6.1(c)'s {+150, -140} counterexample.
# ---------------------------------------------------------------------------


def test_top_in_share_stays_in_0_1_on_the_150_minus_140_counterexample():
    """Round 2's `top_in_share = max(in:, 0.0) / max(s_in, 1.0)` gave
    `150 / max(10, 1) = 15.0` on this exact counterexample -- fifteen times
    its claimed ceiling. Dividing by the POSITIVE incoming mass instead
    gives `150 / 150 = 1.0`."""
    terms = (
        ReasonTerm(label="in:player:1", input_value=150.0, weight=1.0, contribution=150.0),
        ReasonTerm(label="in:player:2", input_value=-140.0, weight=1.0, contribution=-140.0),
    )
    record = _legal_record(score=10.0, threshold=1.0, terms=terms)
    frame = derive_frame(
        record, stance=NegotiationStance.ACCEPT, situation=SituationToken.ACCEPT_ROUND0,
        round_index=0, head_to_head=None, p_bar=None,
    )
    assert frame.s_in == pytest.approx(10.0)
    assert frame.top_in_share == pytest.approx(1.0)
    assert 0.0 <= frame.top_in_share <= 1.0


def test_top_in_share_reads_zero_on_an_all_negative_incoming_bundle():
    """The outer clamp makes the all-negative case read `0.0` rather than
    a large negative number (§6.1(c)'s own closing line)."""
    terms = (
        ReasonTerm(label="in:player:1", input_value=-50.0, weight=1.0, contribution=-50.0),
        ReasonTerm(label="in:player:2", input_value=-25.0, weight=1.0, contribution=-25.0),
    )
    record = _legal_record(score=-75.0, threshold=1.0, terms=terms)
    frame = derive_frame(
        record, stance=NegotiationStance.REJECT_DEADLOCK, situation=SituationToken.REJECT_DEADLOCK,
        round_index=0, head_to_head=None, p_bar=None,
    )
    assert frame.top_in_share == 0.0


# ---------------------------------------------------------------------------
# cap_relief_share -- §6.1(d)'s signed-not-absolute fix.
# ---------------------------------------------------------------------------


def test_cap_relief_share_flips_sign_at_equal_magnitude_when_it_is_the_only_term():
    """§6.1(d)'s own worked case: "the clamp is now two-sided because the
    numerator can exceed the positive mass in magnitude when it is the
    ONLY negative term." With `cap_delta_bonus` as the record's only term,
    the positive-sign record's denominator is its own `+30` (share `1.0`
    exactly); the negative-sign record's positive mass is `0.0`, floored
    to `1.0` (`max(..., 1.0)`), so its share is `-30 / 1.0`, clamped to
    `-1.0` exactly -- two hand-built records, identical except for
    `cap_delta_bonus`'s sign, landing at `+1.0`/`-1.0`: opposite sign, and
    (via the clamp) exactly equal magnitude."""
    positive_term = (
        ReasonTerm(label="cap_delta_bonus", input_value=30.0, weight=1.0, contribution=30.0),
    )
    negative_term = (
        ReasonTerm(label="cap_delta_bonus", input_value=30.0, weight=1.0, contribution=-30.0),
    )
    record_positive = _legal_record(score=30.0, threshold=1.0, terms=positive_term)
    record_negative = _legal_record(score=-30.0, threshold=1.0, terms=negative_term)
    frame_positive = derive_frame(
        record_positive, stance=NegotiationStance.ACCEPT, situation=SituationToken.ACCEPT_ROUND0,
        round_index=0, head_to_head=None, p_bar=None,
    )
    frame_negative = derive_frame(
        record_negative, stance=NegotiationStance.REJECT_DEADLOCK,
        situation=SituationToken.REJECT_DEADLOCK, round_index=0, head_to_head=None, p_bar=None,
    )
    assert frame_positive.cap_relief_share == pytest.approx(1.0)
    assert frame_negative.cap_relief_share == pytest.approx(-1.0)
    assert frame_positive.cap_relief_share == pytest.approx(-frame_negative.cap_relief_share)


def test_cap_relief_share_stays_in_bounds_with_other_positive_mass_present():
    """A less saturated case: `cap_delta_bonus` shares the positive mass
    with another `in:` term, so the two signs' denominators legitimately
    differ (the positive case's denominator includes the cap term's own
    `+30`; the negative case's does not) -- still opposite-signed and
    still within `[-1, +1]`, without asserting exact magnitude equality
    (only the saturated single-term case above guarantees that exactly)."""
    base_terms = (
        ReasonTerm(label="in:player:1", input_value=100.0, weight=1.0, contribution=100.0),
        ReasonTerm(label="out:player:1", input_value=50.0, weight=-1.0, contribution=-50.0),
    )
    positive_cap_term = ReasonTerm(
        label="cap_delta_bonus", input_value=30.0, weight=1.0, contribution=30.0
    )
    negative_cap_term = ReasonTerm(
        label="cap_delta_bonus", input_value=30.0, weight=1.0, contribution=-30.0
    )
    record_positive = _legal_record(
        score=80.0, threshold=1.0, terms=(*base_terms, positive_cap_term)
    )
    record_negative = _legal_record(
        score=20.0, threshold=1.0, terms=(*base_terms, negative_cap_term)
    )
    frame_positive = derive_frame(
        record_positive, stance=NegotiationStance.ACCEPT, situation=SituationToken.ACCEPT_ROUND0,
        round_index=0, head_to_head=None, p_bar=None,
    )
    frame_negative = derive_frame(
        record_negative, stance=NegotiationStance.ACCEPT, situation=SituationToken.ACCEPT_ROUND0,
        round_index=0, head_to_head=None, p_bar=None,
    )
    assert frame_positive.cap_relief_share > 0.0
    assert frame_negative.cap_relief_share < 0.0
    assert -1.0 <= frame_negative.cap_relief_share <= 1.0
    assert -1.0 <= frame_positive.cap_relief_share <= 1.0


def test_cap_relief_share_is_zero_when_no_cap_delta_bonus_term_is_present():
    terms = (ReasonTerm(label="in:player:1", input_value=50.0, weight=1.0, contribution=50.0),)
    record = _legal_record(score=50.0, threshold=1.0, terms=terms)
    frame = derive_frame(
        record, stance=NegotiationStance.ACCEPT, situation=SituationToken.ACCEPT_ROUND0,
        round_index=0, head_to_head=None, p_bar=None,
    )
    assert frame.cap_relief_share == 0.0


# ---------------------------------------------------------------------------
# gap_rel -- §6.1(a)'s positivity argument and stated bound.
# ---------------------------------------------------------------------------


def test_gap_rel_reproduces_negative_counter_band_mult_at_the_reject_too_far_boundary():
    """§6.1(a): the `gap_rel >= -COUNTER_BAND_MULT` bound holds on the
    SUBSET of records where `respond()` would NOT terminate via the
    far-gap `REJECT_TOO_FAR` cutoff (the corrected, narrower claim -- see
    `negotiation.NegotiationFrame.gap_rel`'s own docstring, critic finding
    B1, §11 step 14), whenever `tuning.min_abs_gain >= 1.0` -- asserted
    here as the test's own stated precondition against the real `DEFAULT_
    TUNING` profile, not silently assumed. On a near-zero-`s_out` package,
    `threshold = tuning.min_abs_gain` (`trade.py:709`'s own
    `max(min_abs_gain, min_rel_gain * s_out)` with `s_out ~= 0`), so
    `gap_rel = gap_abs / threshold`; at EXACTLY `negotiation.respond()`'s
    own `REJECT_TOO_FAR` cutoff (`gap_abs == -COUNTER_BAND_MULT *
    threshold`, which `respond()`'s own `gap < -counter_band(...)` check
    does NOT terminate on, being a strict `<`, not `<=`), `gap_rel`
    reproduces `-COUNTER_BAND_MULT` exactly -- this is the boundary of the
    subset the narrowed claim actually covers, not a universal case."""
    assert DEFAULT_TUNING.min_abs_gain >= 1.0
    threshold = DEFAULT_TUNING.min_abs_gain
    boundary_score = threshold - COUNTER_BAND_MULT * threshold
    record = _legal_record(score=boundary_score, threshold=threshold, terms=())
    frame = derive_frame(
        record, stance=NegotiationStance.REJECT_TOO_FAR, situation=SituationToken.REJECT_TOO_FAR,
        round_index=2, head_to_head=None, p_bar=None,
    )
    assert frame.gap_rel == pytest.approx(-COUNTER_BAND_MULT)
    assert frame.gap_rel >= -COUNTER_BAND_MULT - 1e-9


def test_gap_rel_goes_below_the_floor_on_a_genuinely_far_reject_record():
    """The other half of the corrected claim (critic finding B1, §11 step
    14): `gap_rel` is UNBOUNDED BELOW in general, and a record that is
    genuinely far past the `REJECT_TOO_FAR` cutoff -- not merely sitting
    at its boundary, as the test above does -- must produce a `gap_rel`
    STRICTLY BELOW `-COUNTER_BAND_MULT`, proving this frame does not clamp
    or otherwise lie about the value. The critic's own reproduction:
    `score = threshold - 10 * threshold` with a near-zero `s_out`, so
    `threshold = max(s_out, threshold, 1.0)` and `gap_rel = gap_abs /
    threshold = (score - threshold) / threshold = -10.0` exactly --
    `negotiation.respond()` would terminate this exact record
    `REJECT_TOO_FAR` via the far-gap cutoff, since `gap_abs = -10 *
    threshold < -COUNTER_BAND_MULT * threshold` at the shipped `MULT =
    3.0`."""
    assert DEFAULT_TUNING.min_abs_gain >= 1.0
    threshold = DEFAULT_TUNING.min_abs_gain
    far_score = threshold - _FAR_REJECT_GAP_MULTIPLE * threshold
    record = _legal_record(score=far_score, threshold=threshold, terms=())
    frame = derive_frame(
        record, stance=NegotiationStance.REJECT_TOO_FAR, situation=SituationToken.REJECT_TOO_FAR,
        round_index=2, head_to_head=None, p_bar=None,
    )
    assert frame.gap_rel == pytest.approx(-_FAR_REJECT_GAP_MULTIPLE)
    assert frame.gap_rel < -COUNTER_BAND_MULT


def test_gap_rel_and_gap_abs_exact_values_on_a_worked_legal_package():
    out_terms = (
        ReasonTerm(label="out:player:1", input_value=200.0, weight=-1.0, contribution=-200.0),
        ReasonTerm(label="out:pick:9", input_value=255.0, weight=-1.0, contribution=-255.0),
    )
    record = _legal_record(score=160.0, threshold=40.0, terms=out_terms)
    frame = derive_frame(
        record, stance=NegotiationStance.ACCEPT, situation=SituationToken.ACCEPT_ROUND0,
        round_index=0, head_to_head=None, p_bar=None,
    )
    assert frame.s_out == pytest.approx(455.0)
    assert frame.gap_abs == pytest.approx(120.0)
    assert frame.gap_rel == pytest.approx(120.0 / 455.0)
    assert frame.out_players == 1
    assert frame.out_picks == 1
    assert frame.in_players == 0
    assert frame.in_picks == 0


# ---------------------------------------------------------------------------
# gap_over_band -- §11 step 15's B2 fix. Exact against respond()'s REAL
# far-gap cutoff (`gap < -counter_band(actor_record)`), unlike `gap_rel`
# above (which the design doc's own second correction retracts as a
# `reject_too_far`-split trigger -- see `negotiation.NegotiationFrame.
# gap_rel`'s own corrected docstring).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("illegal_token", _ILLEGAL_TOKENS)
def test_gap_over_band_is_zero_on_all_five_illegal_tokens(illegal_token):
    record = _poisoned_illegal_record(illegal_token)
    frame = derive_frame(
        record, stance=NegotiationStance.ACCEPT, situation=SituationToken.OPEN,
        round_index=0, head_to_head=None, p_bar=None,
    )
    assert frame.gap_over_band == 0.0


def test_counter_band_mult_times_min_abs_gain_precondition_holds_against_default_tuning():
    """The exactness identity's own stated precondition
    (`negotiation.NegotiationFrame.gap_over_band`'s docstring):
    `COUNTER_BAND_MULT * tuning.min_abs_gain >= 1.0`, satisfied 120x over
    (`3.0 * 40.0 = 120.0`) on the shipped `DEFAULT_TUNING` profile -- this
    is what keeps `max(counter_band(record), 1.0)`'s floor inert on every
    legal record."""
    assert COUNTER_BAND_MULT * DEFAULT_TUNING.min_abs_gain >= 1.0
    assert counter_band(_legal_record(score=1.0, threshold=DEFAULT_TUNING.min_abs_gain)) > 1.0


def _give_less_actor_record(*, score: float, threshold: float) -> DecisionRecord:
    """A `reject`-verdict record carrying two `out:player:*` terms, which
    makes `respond()`'s give-less axis AVAILABLE (`_out_player_count(record)
    >= 2`) -- with `rounds_remaining > 0` too, this isolates the far-gap
    cutoff as the ONLY way `respond()` can still terminate `REJECT_TOO_FAR`
    on this record, which is exactly what the exactness test below needs to
    isolate."""
    terms = (
        ReasonTerm(label="out:player:1", input_value=50.0, weight=-1.0, contribution=-50.0),
        ReasonTerm(label="out:player:2", input_value=50.0, weight=-1.0, contribution=-50.0),
    )
    return DecisionRecord(
        team_id=1, philosophy=GmPhilosophy.STAR_MAXER, window=CompetitiveWindow.CONTEND,
        subject="t", verdict="reject", score=score, threshold=threshold, terms=terms,
    )


def _accepting_partner_record() -> DecisionRecord:
    return DecisionRecord(
        team_id=2, philosophy=GmPhilosophy.EGALITARIAN, window=CompetitiveWindow.CONTEND,
        subject="t", verdict="accept", score=100.0, threshold=1.0, terms=(),
    )


@pytest.mark.parametrize("gap_over_counter_band", [-10.0, -3.0001, -1.0001, -1.0, -0.9999, 0.0])
def test_gap_over_band_lt_neg_one_agrees_with_responds_own_verdict_at_a_spread_of_records(
    gap_over_counter_band,
):
    """The architect's own exactness test: for a spread of hand-built
    records including one AT exactly the `-counter_band` boundary
    (`gap_over_counter_band == -1.0`), `gap_over_band < -1.0` (strict,
    matching `respond()`'s own strict `<`, so equality is NOT far) agrees
    with `respond()`'s own verdict on the SAME record."""
    threshold = DEFAULT_TUNING.min_abs_gain
    band = COUNTER_BAND_MULT * threshold
    gap = gap_over_counter_band * band
    score = threshold + gap

    record_a = _give_less_actor_record(score=score, threshold=threshold)
    record_b = _accepting_partner_record()

    verdict = respond(record_a, record_b, rounds_remaining=1, max_outgoing_players=3)
    respond_says_far = verdict == NegotiationStance.REJECT_TOO_FAR

    frame = derive_frame(
        record_a, stance=NegotiationStance.COUNTER, situation=SituationToken.COUNTER_GIVE_LESS,
        round_index=0, head_to_head=None, p_bar=None,
    )
    assert (frame.gap_over_band < -1.0) == respond_says_far


# ---------------------------------------------------------------------------
# Passthrough fields on the legal path -- stance/situation/round_index/
# philosophy/window are the CALLER's own tokens, not derived here.
# ---------------------------------------------------------------------------


def test_legal_path_passes_through_caller_supplied_stance_situation_and_round_index():
    record = _legal_record(score=500.0, threshold=100.0)
    frame = derive_frame(
        record, stance=NegotiationStance.COUNTER, situation=SituationToken.COUNTER_GIVE_LESS,
        round_index=1, head_to_head=None, p_bar=None,
    )
    assert frame.stance == NegotiationStance.COUNTER
    assert frame.situation == SituationToken.COUNTER_GIVE_LESS
    assert frame.round_index == 1
    assert frame.philosophy == GmPhilosophy.STAR_MAXER
    assert frame.window == CompetitiveWindow.CONTEND
    assert frame.illegal_token is None
