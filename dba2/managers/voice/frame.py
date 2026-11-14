"""`DecisionRecord -> NegotiationFrame`, the FULL frame -- Decision P3-15,
§6.1 (docs/design/phase-3-inter-manager-society.md), §11 step 14.

`managers/negotiation.py::NegotiationFrame` already declares the
purely-arithmetic fields (§6.1's table, `gap_abs` through
`cap_relief_share` -- originally ten, now eleven with `gap_over_band`,
added at step 15 to give templates a field that actually agrees with
`respond()`'s own far-gap cutoff; see that field's own docstring) --
that module's own docstring calls this "the numeric half only" and
names this file as the place the full frame and its derivation function
are built. `NegotiationFrame` below is that base class, extended with
§6.1's other nine fields (`illegal_token`, `round_index`, `stance`,
`situation`, `philosophy`, `window`, `h2h_shade`, `h2h_opened`,
`h2h_far_reject_streak`) -- inheritance rather than a re-declaration of
the numeric fields, since every field on both classes is required (no
defaults anywhere), so ordering cannot conflict.

**`SituationToken` is declared here, not reused from `gm_service.py`'s
`NegotiationSituation`.** That module's own comment calls its nine-member
enum "a lightweight, SERVICES-scoped stand-in for the ten-token
`SituationToken` enum `managers/voice/frame.py` builds at step 14" and
explicitly defers reconciling the two to whenever dialogue is wired into
the service (steps 15/16) -- not this step's job, and not attempted here.

**Scope boundary, stated once so it does not have to be re-derived by a
reader:** `stance` and `situation` are the turn's OWN tokens, supplied by
the caller (§6.1's table: "the turn's own stance/situation token"), exactly
like `philosophy`/`window` ("off the record") -- `derive_frame` below
packages caller-supplied context into a `NegotiationFrame`, it does not
compute `situation` from the record's own shape. The one exception is the
illegality short-circuit (Decision P3-15), which OVERRIDES both tokens to
`REJECT_ILLEGAL`/`reject_illegal` regardless of what the caller passed,
because an illegal record's `stance`/`situation` can only ever be that one
value (§4.2's own legality-first ordering, mirrored here).

Zero I/O, per Decision P2-2(b)'s purity extension carried into `managers/`
(`tests/unit/test_layering.py`): no `dba2.data`, no `datetime.now()`, no
bare `random.Random()`, no `open()`, no `os.environ`. Imports only
`dba2.domain.team` (`GmPhilosophy`), `dba2.domain.head_to_head`
(`HeadToHead`), `dba2.managers.explain` (`DecisionRecord`),
`dba2.managers.memory` (`shade`), `dba2.managers.negotiation` (the
numeric-half base class, `NegotiationStance`, and `_clamp` -- reused
rather than re-declared, see `_clamp`'s own import comment below),
`dba2.managers.tuning` (`CompetitiveWindow`), and stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from dba2.domain.head_to_head import HeadToHead
from dba2.domain.team import GmPhilosophy
from dba2.managers.explain import DecisionRecord
from dba2.managers.memory import shade
from dba2.managers.negotiation import NegotiationFrame as _NumericFrame
from dba2.managers.negotiation import (
    NegotiationStance,
    _clamp,  # reused below, not re-declared (nit N7)
    counter_band,  # reused for gap_over_band, §11 step 15's B2 fix
)
from dba2.managers.tuning import CompetitiveWindow

# ---------------------------------------------------------------------------
# SituationToken -- §6.2's ten-token closed set, listed rather than counted
# so the count cannot drift (the design doc's own instruction, quoted
# verbatim in each member's docstring below is unnecessary repetition --
# the ten tokens are named once, here, in the doc's own order).
# ---------------------------------------------------------------------------


class SituationToken(StrEnum):
    """§6.2's ten situation tokens, verbatim and in the design doc's own
    listed order: `open, accept_round0, accept_after_counter,
    counter_ask_more, counter_give_less, reject_too_far, reject_deadlock,
    reject_illegal, expire_stalled, expire_rounds`. This is the finer-
    grained phrase-selection trigger the voice layer reads -- distinct from
    `negotiation.NegotiationStance`, the state machine's own six-member
    transition label (e.g. `ACCEPT` splits into `accept_round0`/
    `accept_after_counter` here; `COUNTER` splits into
    `counter_ask_more`/`counter_give_less`; `EXPIRE` splits into
    `expire_stalled`/`expire_rounds`)."""

    OPEN = "open"
    ACCEPT_ROUND0 = "accept_round0"
    ACCEPT_AFTER_COUNTER = "accept_after_counter"
    COUNTER_ASK_MORE = "counter_ask_more"
    COUNTER_GIVE_LESS = "counter_give_less"
    REJECT_TOO_FAR = "reject_too_far"
    REJECT_DEADLOCK = "reject_deadlock"
    REJECT_ILLEGAL = "reject_illegal"
    EXPIRE_STALLED = "expire_stalled"
    EXPIRE_ROUNDS = "expire_rounds"


# ---------------------------------------------------------------------------
# NegotiationFrame -- the FULL frame. Extends negotiation.py's numeric-only
# base with §6.1's other nine fields.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NegotiationFrame(_NumericFrame):
    """§6.1's full feature vocabulary -- the eleven numeric fields inherited
    from `negotiation.NegotiationFrame` (see that class's own docstrings
    for their derivations/ranges), plus these nine. Every field's value on
    an `illegal:*` record and its real range is quoted from §6.1's table."""

    illegal_token: str | None
    """The verdict's `illegal:*` suffix (e.g. `"roster_max"`), or `None` on
    a legal record. One of exactly five values on the illegal path (§4.2's
    five-token legality gate); real range: `enum | None`."""

    round_index: int
    """The turn's own round index, as given by the caller (this class
    applies no bound of its own). Real range: `[0, MAX_NEGOTIATION_ROUNDS]`
    -- `MAX_NEGOTIATION_ROUNDS` is a `services/`-scoped constant
    (Decision P3-8) this module never imports."""

    stance: NegotiationStance
    """The turn's own stance token, as given -- overridden to
    `NegotiationStance.REJECT_ILLEGAL` by Decision P3-15's short-circuit
    regardless of what the caller passed, since an illegal record's stance
    can only ever be that one value."""

    situation: SituationToken
    """The turn's own situation token, as given -- overridden to
    `SituationToken.REJECT_ILLEGAL` by the same short-circuit, for the
    same reason."""

    philosophy: GmPhilosophy
    """`record.philosophy`, read straight off the record -- NOT zeroed or
    overridden on the illegal path (§6.1's table: "off the record, as
    given" applies on both the legal and illegal paths)."""

    window: CompetitiveWindow
    """`record.window`, read straight off the record -- same "as given on
    both paths" rule as `philosophy`."""

    h2h_shade: float
    """`memory.shade(head_to_head, p_bar)` -- `0.0` when `head_to_head is
    None` (no memory subsystem, or this pair's first-ever negotiation) or
    `p_bar is None` (§6.1(b)/§8.1). **Not zeroed by the illegality
    short-circuit** -- the short-circuit template (§6.1) lists specific
    fields as `0.0` and omits every `h2h_*` field, so these three are
    computed identically on the legal and illegal paths. Real range:
    `[-1, +1]`."""

    h2h_opened: float
    """`head_to_head.opened` (decayed), or `0.0` when `head_to_head is
    None` -- the same "unknown opponent == no memory subsystem" identity
    `shade()` itself relies on (§6.1(b)). Real range: `[0, inf)`."""

    h2h_far_reject_streak: float
    """`head_to_head.far_reject_streak` (decayed), or `0.0` when
    `head_to_head is None`. **Read off the real `HeadToHead` row directly**
    (`domain/head_to_head.py`) -- `memory.HeadToHeadLike`, the protocol
    `shade()` itself is typed against, only names `opened`/
    `accepts_by_opponent` (the two fields `shade()` reads), not this one;
    this field is why `derive_frame` below is typed against the real
    `HeadToHead` dataclass rather than `HeadToHeadLike` (critic finding
    B3, §11 step 14 -- `HeadToHeadLike` structurally does not declare
    `far_reject_streak`, so a caller could construct an object satisfying
    the protocol at the type level while raising `AttributeError` here at
    runtime), exactly as `memory.py`'s own `target_count_for_pair` is
    already typed against `HeadToHead | None`, not `HeadToHeadLike`, one
    function over, for the identical reason. Real range: `[0, inf)`."""


# ---------------------------------------------------------------------------
# derive_frame -- Decision P3-15's derivation, total over every
# DecisionRecord verdict shape.
# ---------------------------------------------------------------------------


def derive_frame(
    record: DecisionRecord,
    *,
    stance: NegotiationStance,
    situation: SituationToken,
    round_index: int,
    head_to_head: HeadToHead | None,
    p_bar: float | None,
) -> NegotiationFrame:
    """`DecisionRecord -> NegotiationFrame`, Decision P3-15 -- **short-
    circuits on illegality before any division, `max`, or sum over `terms`,
    or even the `h2h_*` reads, is evaluated: the illegality check is this
    function's literal first statement** (critic finding N2, §11 step 14
    -- computing `h2h_*` ahead of the branch, even though the values
    themselves are correctly identical on both paths, weakened §6.1's own
    stated property that "a reader can check the branch by eye" as the
    derivation's first statement; the values are now computed twice, once
    per path, rather than once before the branch). An `illegal:*` record
    has `score = threshold = 0.0` and `terms = ()` (`trade.py:648-652`),
    which would otherwise divide `0.0 / 0.0`. This mirrors §4.2's own
    ordering (Decision P3-6 checks legality before reading a gap) rather
    than guarding each denominator individually.

    `head_to_head` is typed against the real `dba2.domain.head_to_head.
    HeadToHead` dataclass, not `memory.HeadToHeadLike` (critic finding B3,
    §11 step 14 -- `HeadToHeadLike` only declares `opened`/`accepts_by_
    opponent`, the two fields `shade()` itself reads, and does not declare
    `far_reject_streak`, which this function also reads; a `HeadToHeadLike`
    parameter type let a caller construct an object that satisfies the
    annotation while raising `AttributeError` at runtime). This matches
    `memory.py::target_count_for_pair`'s own precedent exactly -- that
    function reads `head_to_head.far_reject_streak` off a `HeadToHead |
    None` parameter for the identical reason.

    `stance`/`situation`/`round_index`/`philosophy`/`window` are the
    caller's own context for this turn -- this function packages them into
    the frame, it does not derive `situation` from the record's own shape
    (that derivation lives in `negotiation.respond()`/the negotiation loop,
    not here). The one override: on the illegal path, `stance`/`situation`
    are replaced with `NegotiationStance.REJECT_ILLEGAL`/
    `SituationToken.REJECT_ILLEGAL` regardless of what was passed in,
    exactly as Decision P3-15's short-circuit template states.

    `head_to_head`/`p_bar` feed `h2h_shade`/`h2h_opened`/`h2h_far_reject_
    streak` identically on both the legal and illegal paths (§6.1's short-
    circuit template lists specific fields as `0.0` and omits these three
    -- they are not part of the illegality short-circuit at all).

    **Every legal-path denominator is strictly positive (§6.1(a)):** on a
    non-`illegal:*` record, `record.threshold = max(tuning.min_abs_gain,
    tuning.min_rel_gain * S_out)` (`trade.py:709`) is itself computed by
    `evaluate_trade` under whatever tuning profile produced this record, so
    `max(s_out, record.threshold, 1.0) >= 1.0 > 0` unconditionally --
    reading `record.threshold` rather than recomputing it is what makes
    this hold without this module needing a `GmTuning` parameter at all.

    **The stated `gap_rel >= -COUNTER_BAND_MULT` bound is narrower than it
    reads, and this is a corrected claim (critic finding B1, §11 step 14
    -- the original claim was universal and false; see `negotiation.
    NegotiationFrame.gap_rel`'s own corrected docstring and the design
    doc's dated correction to §6.1 for the full derivation).** It holds
    only on the SUBSET of legal records where `respond()` would NOT
    terminate via the far-gap `REJECT_TOO_FAR` cutoff -- a genuinely
    far-reject record has `gap_rel < -COUNTER_BAND_MULT` by the identical
    arithmetic, not `>=`, which is exactly what §6.2's situation-token
    split on `reject_too_far` (in-band-vs-genuinely-far) is SUPPOSED to
    depend on. On that subset, the bound additionally requires `tuning.
    min_abs_gain >= 1.0` on the `GmTuning` profile that produced `record`
    -- a precondition on the CALLER's tuning profile, not something this
    function can check (it never sees a `GmTuning`), so it is asserted by
    the caller's own test suite rather than enforced here.

    **Second correction, appended §11 step 15 (critic + architect rounds):
    the far-gap cutoff quoted in the paragraph above (`gap_abs <
    -COUNTER_BAND_MULT * max(s_out, record.threshold, 1.0)`) is itself
    wrong -- it was never `respond()`'s real cutoff.** `respond()`'s actual
    far-gap branch (`managers/negotiation.py::respond`) is `gap <
    -counter_band(actor_record)`, i.e. `gap_abs < -COUNTER_BAND_MULT *
    record.threshold` -- no `s_out`, no outer `max()`. `gap_rel`'s
    denominator (`max(s_out, threshold, 1.0)`) is a different quantity from
    `counter_band`'s (`threshold` alone) whenever `s_out > threshold`, the
    common case on a real package -- a real 2,214-selection `reject_too_far`
    corpus measured `gap_rel` entirely inside `[-1.1784, -0.0008]`, zero
    below `-3.0`, proving `gap_rel` carries no structural relationship to
    the real cutoff. `gap_over_band` (declared next to `gap_rel` on the
    base numeric class) is derived against the REAL cutoff instead and is
    exact under this profile's own precondition -- see its own docstring.
    §6.2's `reject_too_far` split now triggers on `gap_over_band`, not
    `gap_rel`.

    `gap_over_band = gap_abs / max(counter_band(record), 1.0)` on the legal
    path, `0.0` on the illegal short-circuit (same template as every other
    numeric field).

    `top_in_share` divides by the POSITIVE incoming mass (`Σ max(c, 0)`
    over `in:`-labelled contributions), not `s_in` itself, which is what
    keeps it in `[0, 1]` on a mixed-sign incoming bundle -- e.g. incoming
    contributions of `{+150, -140}` give `s_in = 10` but `top_in_share =
    150 / max(150, 1) = 1.0`, not `150 / 10 = 15.0` (§6.1(c)).

    `cap_relief_share` is SIGNED, not `abs()`'d: positive means most of
    what this deal does for the team is cap relief, negative means most of
    what it does is take on salary (§6.1(d)) -- dividing the (possibly
    negative) `cap_delta_bonus` contribution by the same positive mass
    denominator (over ALL of `record.terms`, not just `in:`), then clamping
    to `[-1, +1]` since the numerator can exceed the positive mass in
    magnitude when it is the only negative term."""
    if record.verdict.startswith("illegal:"):
        return NegotiationFrame(
            gap_abs=0.0, gap_rel=0.0, gap_over_band=0.0, s_in=0.0, s_out=0.0,
            in_players=0, out_players=0, in_picks=0, out_picks=0,
            top_in_share=0.0, cap_relief_share=0.0,
            illegal_token=record.verdict.removeprefix("illegal:"),
            round_index=round_index,
            stance=NegotiationStance.REJECT_ILLEGAL,
            situation=SituationToken.REJECT_ILLEGAL,
            philosophy=record.philosophy,
            window=record.window,
            h2h_shade=shade(head_to_head, p_bar),
            h2h_opened=0.0 if head_to_head is None else head_to_head.opened,
            h2h_far_reject_streak=(
                0.0 if head_to_head is None else head_to_head.far_reject_streak
            ),
        )

    h2h_shade_value = shade(head_to_head, p_bar)
    h2h_opened_value = 0.0 if head_to_head is None else head_to_head.opened
    h2h_far_reject_streak_value = (
        0.0 if head_to_head is None else head_to_head.far_reject_streak
    )

    in_terms = [t for t in record.terms if t.label.startswith("in:")]
    out_terms = [t for t in record.terms if t.label.startswith("out:")]
    s_in = sum(t.input_value for t in in_terms)
    s_out = sum(t.input_value for t in out_terms)
    in_players = sum(1 for t in record.terms if t.label.startswith("in:player:"))
    out_players = sum(1 for t in record.terms if t.label.startswith("out:player:"))
    in_picks = sum(1 for t in record.terms if t.label.startswith("in:pick:"))
    out_picks = sum(1 for t in record.terms if t.label.startswith("out:pick:"))

    gap_abs = record.score - record.threshold
    gap_rel = gap_abs / max(s_out, record.threshold, 1.0)
    gap_over_band = gap_abs / max(counter_band(record), 1.0)

    in_contributions = [t.contribution for t in in_terms]
    top_in_contribution = max(in_contributions, default=0.0)
    positive_in_mass = max(sum(max(c, 0.0) for c in in_contributions), 1.0)
    top_in_share = _clamp(top_in_contribution / positive_in_mass, 0.0, 1.0)

    cap_delta_contribution = next(
        (t.contribution for t in record.terms if t.label == "cap_delta_bonus"), 0.0
    )
    positive_total_mass = max(sum(max(t.contribution, 0.0) for t in record.terms), 1.0)
    cap_relief_share = _clamp(cap_delta_contribution / positive_total_mass, -1.0, 1.0)

    return NegotiationFrame(
        gap_abs=gap_abs, gap_rel=gap_rel, gap_over_band=gap_over_band, s_in=s_in, s_out=s_out,
        in_players=in_players, out_players=out_players,
        in_picks=in_picks, out_picks=out_picks,
        top_in_share=top_in_share, cap_relief_share=cap_relief_share,
        illegal_token=None,
        round_index=round_index,
        stance=stance,
        situation=situation,
        philosophy=record.philosophy,
        window=record.window,
        h2h_shade=h2h_shade_value,
        h2h_opened=h2h_opened_value,
        h2h_far_reject_streak=h2h_far_reject_streak_value,
    )
