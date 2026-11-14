"""The negotiation state machine's pure half -- `NegotiationStance`,
`counter_band()`, `counter_band_eff()` (channel 3's shaded band, step 12),
`respond()`, `is_stalled()`, and `NegotiationFrame`'s numeric-only subset,
plus FOUR of Decision P3-30's six `managers/`-scoped Phase 3 constants
(docs/design/phase-3-inter-manager-society.md §3, §4, §8.2, §11 steps 4
and 12).

**This module, plus `memory.py`'s pure half, is the one and only
`GM_TUNING_VERSION` bump this phase makes (Decision P3-30).** Every
constant below is declared once, here, at its FINAL value -- no later
step adds or moves a `managers/`-scoped constant, which is exactly what
`tests/unit/managers/test_constant_inventory.py` makes mechanical.

**What this module deliberately does NOT build, and why -- both are
later steps' jobs, not a gap in this one.**

- **The full `NegotiationFrame`** (`philosophy`, `window`, `stance`,
  `situation`, `illegal_token`, `round_index`, `h2h_shade`, `h2h_opened`,
  `h2h_far_reject_streak`) and the `DecisionRecord -> NegotiationFrame`
  derivation itself are `managers/voice/frame.py`'s job, built at step 14
  (§3's module layout: "frame.py # DecisionRecord -> NegotiationFrame
  (the named, TOTAL feature vocabulary)"). Step 4's own Verify clause
  never asks this module to convert a `DecisionRecord`; only the
  dataclass *shape* for the ten purely-arithmetic fields belongs here,
  per §3's own comment ("NegotiationFrame(numeric half)"). Building the
  illegality short-circuit / denominator-positivity derivation now would
  duplicate, rather than anticipate, step 14's own work -- and step 14
  needs `philosophy`/`window`/`h2h_*` inputs this module has no way to
  supply.
- **Stall detection's own turn-history bookkeeping.** Decision P3-9's
  stall rule needs "the acting side's own gap against its own band,
  compared only between same-actor turns, with a cell flip resetting the
  baseline" -- that requires a real sequence of turns, which does not
  exist until step 7 builds the negotiation loop inside
  `run_trade_window`. `respond()` below implements exactly §4.2's own
  total per-turn rule, whose only EXPIRE branch is round exhaustion
  (`EXPIRED:rounds`); `EXPIRED:stalled` is a LOOP-level override of what
  `respond()` would otherwise return as `COUNTER` (Decision P3-9 states
  it as a second, independent expiry condition, not a branch of §4.2's
  cross-product rule). What step 4 *can* build, and does, is
  `is_stalled()` -- the pure, enumerable arithmetic predicate Decision
  P3-9 states once the loop has already picked the right two same-actor
  gaps to compare. The loop (step 7) owns tracking which actor moved on
  the previous turn and supplies `previous_actor=None` on a cell flip.

Zero I/O, per Decision P2-2(b)'s purity extension carried into `managers/`
(`tests/unit/test_layering.py`): no `dba2.data`, no `datetime.now()`, no
bare `random.Random()`, no `open()`, no `os.environ`. Imports only
`dba2.managers.explain` (for `DecisionRecord`) and stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from dba2.managers.explain import DecisionRecord

# ---------------------------------------------------------------------------
# Decision P3-30's table -- four of Phase 3's six managers/-scoped
# constants. The other two (H2H_SHADE_PRIOR, H2H_DECAY) live in memory.py,
# next to shade(), the one function that reads them.
# ---------------------------------------------------------------------------

COUNTER_BAND_MULT: float = 3.0
"""**FINAL, Decision P3-43** -- `counter_band()`'s one multiplier. Rounds
1-8 of the design carried `2.0` as a step-1-census placeholder; the census
ran and refuted it, and every larger value the metric (M23) appeared to
want. 3.0 is set on §4.3's physical-reach argument (a team's whole
outgoing package is worth at most 16.667 acceptance thresholds, so
`MULT=3.0` asks a give-less repair to claw back at least 18% of A's book
-- an ordinary lopsided pair, not `MULT=8.0`'s 48%, past what a
single-asset drop can plausibly recover), not on the metric's own
reading -- no band clears M23-as-written honestly, so M23 stops being a
counted gate (Decision P3-43 part 3) rather than the constant chasing a
metric that supplies no stopping rule."""

MIN_CONCESSION_FRAC: float = 0.10
"""Decision P3-9's stall-rule bar, as a share of the acting side's own
`counter_band()`: a counter that fails to move `|gap|` toward zero by at
least `MIN_CONCESSION_FRAC * counter_band(actor_record)` is a stall
candidate (see `is_stalled()`; the loop, step 7, owns the actor-and-
baseline bookkeeping this predicate depends on). An oscillation
invariant, not a tuning surface: at the shipped `COUNTER_BAND_MULT=3.0`
the bar is 0.30 thresholds, 19-28x below the smallest real concession
the give-less axis can ever make (a whole asset, 5.6-8.3 thresholds per
§4.3's identity) -- it exists to catch a package alternating between two
near-identical states, not to shape ordinary counters."""

H2H_BAND_SHADE: float = 0.25
"""Channel 3's own shade coefficient (§8.2, built at step 12):
`COUNTER_BAND_MULT_eff = COUNTER_BAND_MULT * (1 + H2H_BAND_SHADE * shade)`.
Declared here, at its final value, per Decision P3-30's rule that all six
`managers/`-scoped Phase 3 constants land in the one commit that bumps
`GM_TUNING_VERSION` -- step 12 reads it, nothing before step 12 does."""

H2H_MARGIN_SHADE: float = 0.25
"""Channel 2's own shade coefficient (§8.3, built at step 13) -- shades
`build_counter`'s self-margin stop condition, a threshold-relative margin
rather than the counter band itself. Declared here for the same reason
`H2H_BAND_SHADE` is: Decision P3-30 closes the six-constant set at step 4
so no later step can re-open `GM_TUNING_VERSION`."""


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# NegotiationStance -- §4.2's six-member total-function codomain.
# ---------------------------------------------------------------------------


class NegotiationStance(StrEnum):
    """The six terminal/non-terminal tokens `respond()` returns -- §4.2's
    rule, read verbatim: legality first (Decision P3-6), then the
    both-legal cell, then (for the two "exactly one side is short" cells)
    a far-reject/round-exhaustion/no-axis ladder, with `COUNTER` as the
    one non-terminal outcome. Distinct from `SituationToken` (§6.2, built
    at step 14) -- a `NegotiationStance` is the state-machine's own
    transition label; a situation token is the finer-grained phrase-
    selection trigger the voice layer reads (e.g. `ACCEPT` splits into
    `accept_round0`/`accept_after_counter`)."""

    ACCEPT = "accept"
    REJECT_ILLEGAL = "reject_illegal"
    REJECT_DEADLOCK = "reject_deadlock"
    REJECT_TOO_FAR = "reject_too_far"
    COUNTER = "counter"
    EXPIRE = "expire"


# ---------------------------------------------------------------------------
# NegotiationFrame -- numeric half only (§6.1's originally-ten,
# now-eleven pure-arithmetic fields -- `gap_over_band` added at step 15,
# see its own field docstring below). The full frame, plus the
# DecisionRecord -> NegotiationFrame
# derivation, is voice/frame.py's job at step 14 -- see this module's own
# docstring for why building that derivation now would be premature.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NegotiationFrame:
    """§6.1's feature vocabulary, numeric subset only -- Decision P3-15's
    field table, minus every field that needs context this module never
    receives (`philosophy`, `window`, `stance`, `situation`,
    `illegal_token`, `round_index`, `h2h_*`). Every field's real range and
    illegal-record value below are §6.1's own table, quoted so a reader of
    this file does not have to cross-reference the design doc to know
    what a field means."""

    gap_abs: float
    """`score - threshold`. Real range: all of ℝ. `0.0` on an illegal
    record (Decision P3-15's short-circuit)."""

    gap_rel: float
    """`gap_abs / max(s_out, threshold, 1.0)`. Real range: ℝ, UNBOUNDED
    BELOW in general (critic finding B1, §11 step 14 -- the original claim
    below was a universal `>= -COUNTER_BAND_MULT` bound, which is false).
    A genuinely far-reject record -- one `respond()` would terminate
    `REJECT_TOO_FAR` via the far-gap cutoff, `gap_abs < -COUNTER_BAND_MULT
    * max(s_out, threshold, 1.0)` -- has `gap_rel < -COUNTER_BAND_MULT` by
    that identical arithmetic, not `>=`; §6.2's own situation-token split
    on `reject_too_far` (in-band-vs-genuinely-far) depends on exactly this.
    The narrower, TRUE claim: `gap_rel >= -COUNTER_BAND_MULT` holds only on
    the SUBSET of records where `respond()` would NOT terminate via that
    branch (a near-tautology restating `respond()`'s own cutoff, not an
    independent guarantee), and additionally requires the tuning surface's
    own `min_abs_gain >= 1.0` (§6.1(a) -- the shipped `40.0` satisfies this
    by 40x). See `docs/design/phase-3-inter-manager-society.md` §6.1's
    dated correction for the full derivation. `0.0` on an illegal
    record.

    **Second correction, appended §11 step 15 (critic + architect rounds):
    the far-reject cutoff quoted above (`-COUNTER_BAND_MULT * max(s_out,
    threshold, 1.0)`) is itself wrong.** `respond()`'s REAL far-gap cutoff
    (below, in this same module) is `gap < -counter_band(actor_record)` --
    `counter_band()` is `COUNTER_BAND_MULT * record.threshold`, with no
    `s_out` and no outer `max()` at all. `gap_rel`'s own denominator
    (`max(s_out, threshold, 1.0)`) is a DIFFERENT quantity from
    `counter_band`'s (`threshold` alone) whenever `s_out > threshold`, which
    is the common case on a real package (a real 2,214-selection
    `reject_too_far` corpus measured `gap_rel` ranging `-1.1784` to
    `-0.0008`, zero instances below `-3.0` -- proving `gap_rel` carries no
    structural relationship to `respond()`'s actual cutoff and cannot
    separate the two `reject_too_far` causes no matter what threshold a
    template picks). Use `gap_over_band` below instead -- it is derived
    against the REAL cutoff and is exact under this profile's own stated
    precondition (see `gap_over_band`'s own docstring)."""

    gap_over_band: float
    """`gap_abs / max(counter_band(record), 1.0)` -- exact against
    `respond()`'s REAL far-gap cutoff, unlike `gap_rel` above (second
    correction, §11 step 15). `counter_band(record) == COUNTER_BAND_MULT *
    record.threshold` (this module, below) -- no `s_out`, no outer `max()`,
    the SAME quantity `respond()` itself compares `gap` against.

    **Exactness identity.** Under the precondition `COUNTER_BAND_MULT *
    tuning.min_abs_gain >= 1.0` (satisfied 120x over on the shipped profile:
    `3.0 * 40.0 = 120.0 > 1.0`), `counter_band(record) >= 1.0` on every
    legal record, so the `max(..., 1.0)` floor is inert and:

        gap_over_band < -1.0  <=>  gap_abs < -counter_band(record)

    exactly -- i.e. `gap_over_band < -1.0` agrees with `respond()`'s own
    far-gap branch on every record satisfying the precondition, not
    approximately. `0.0` on an illegal record (same short-circuit template
    as every other numeric field).

    **Known, disclosed imprecision -- channel 3's shading (not fixed, out
    of scope this dispatch).** On a turn where `h2h_shade != 0`, `respond()`
    actually decided against the SHADED band (`counter_band_eff`, below),
    not `counter_band()` -- but `gap_over_band` is always computed against
    the UNSHADED `counter_band()`, since dialogue deliberately reads the
    real, unshaded record (the same "dialogue never reads the channel-3
    synthetic record" rule `gm_service.py`'s dialogue block already
    follows). This makes the `< -1.0` cutoff imprecise on shaded turns only
    -- the true boundary shifts to roughly `[-1.25, -0.75]` at
    `H2H_BAND_SHADE`'s realised range (§8.2). Threading shade into the
    frame to close this gap is explicitly out of scope: the imprecision is
    bounded and disclosed, and a bigger job than it warrants."""

    s_in: float
    """Σ `input_value` over every `in:`-labelled term. Real range: all of
    ℝ -- may be <= 0 on a salary-dump-shaped incoming bundle (§6.1(c)).
    `0.0` on an illegal record."""

    s_out: float
    """Σ `input_value` over every `out:`-labelled term (`trade.py:708`'s
    own `outgoing_fit_sum`, reused). Real range: all of ℝ, same caveat as
    `s_in`. `0.0` on an illegal record."""

    in_players: int
    """Count of `in:player:*`-labelled terms. `0` on an illegal record."""

    out_players: int
    """Count of `out:player:*`-labelled terms. `0` on an illegal record."""

    in_picks: int
    """Count of `in:pick:*`-labelled terms. `0` on an illegal record."""

    out_picks: int
    """Count of `out:pick:*`-labelled terms. `0` on an illegal record."""

    top_in_share: float
    """`clamp(max(in: contributions, default=0.0) / max(sum(max(c, 0) for
    c in in: contributions), 1.0), 0.0, 1.0)` -- share of the POSITIVE
    incoming mass one player represents (dividing by the positive mass,
    not `s_in` itself, is what keeps this in `[0, 1]` on a mixed-sign
    incoming bundle -- §6.1(c)). `0.0` on an illegal record."""

    cap_relief_share: float
    """`clamp(cap_delta_bonus contribution (default 0.0) / max(sum(max(c,
    0) for c in every contribution), 1.0), -1.0, 1.0)` -- SIGNED: positive
    means most of what this deal does for the team is cap relief,
    negative means most of what it does is take on salary (§6.1(d) --
    dropping the `abs()` round 3 shipped is a real fix, not cosmetic:
    absolute value made a team shedding salary and one absorbing it read
    identically). `0.0` on an illegal record."""


# ---------------------------------------------------------------------------
# Decision P3-7 -- counter_band. One constant, not two.
# ---------------------------------------------------------------------------


def counter_band(record: DecisionRecord) -> float:
    """`COUNTER_BAND_MULT * threshold`, Decision P3-7 -- **reads
    `record.threshold` directly rather than recomputing `S_out`/
    `threshold` from `record.terms` against some `GmTuning`.** This is
    the "reusing S_out/threshold exactly as evaluate_trade computes them,
    don't re-derive independently" instruction taken as literally as
    possible: `record.threshold` already IS
    `max(tuning.min_abs_gain, tuning.min_rel_gain * S_out)`
    (`trade.py:708-709`), computed by `evaluate_trade` at the moment the
    record was produced, under whatever `GmTuning` that call actually
    used. Recomputing it here from `record.terms` against a *second*,
    independently-supplied `GmTuning` would risk a silent mismatch the
    moment a caller runs a sweep with a non-default tuning profile --
    reading the field this function's own docstring says is "reused"
    carries zero such risk, by construction, for every record this phase
    ever calls it on.

    The two worked rows in §4.2/Decision P3-7 -- `120.000` VP at
    `threshold=40.000` (`S_out=455`) and `389.106` VP at
    `threshold=129.702` (`S_out=2161.7`) -- both reproduce exactly under
    this reading, since a real `evaluate_trade`-produced record's
    `threshold` field already carries those exact figures."""
    return COUNTER_BAND_MULT * record.threshold


def counter_band_eff(record: DecisionRecord, shade: float) -> float:
    """Channel 3's shaded band (Decision P3-26/§8.2, built at step 12):
    `COUNTER_BAND_MULT_eff * threshold`, where `COUNTER_BAND_MULT_eff =
    COUNTER_BAND_MULT * (1 + H2H_BAND_SHADE * shade)`. `shade` is the
    ACTING side's own memory of its negotiation partner (`managers.
    memory.shade`, called by the caller against that side's own
    `HeadToHead` row -- this module never reads `head_to_head` itself,
    per Decision 4's layering rule).

    **Exactly `counter_band(record)` at `shade = 0.0`** -- the "no
    memory -> identical to today's unshaded behavior" invariant every
    Phase 3 step has maintained (`shade()`'s own contract: `0.0` exactly
    when there is no memory subsystem, no row for this pair, or an
    average opponent). This is a separate function rather than an
    optional-shade parameter grafted onto `counter_band` itself, so
    `counter_band(record)`'s own call sites (`is_stalled`'s callers that
    have no shade context, this module's own docstrings, every pre-step-12
    test) keep calling the exact same one-argument, always-unshaded
    function they always have.

    **Reachable range, at the shipped constants:** `[2.25, 3.75] *
    threshold` in the abstract (`H2H_BAND_SHADE=0.25`'s own `[-1, +1]`
    envelope), narrower in practice because `shade`'s own positive
    ceiling is `< 1` at any real `p_bar != 0.5` (§8.1) -- at the measured
    `p_bar = 0.7709`, `shade <= 0.2972`-ish (before the `n/(n+H2H_SHADE_
    PRIOR)` shrinkage further pulls it toward 0), so the realised
    widening arm stays inside `[2.25, ~3.223] * threshold`, per §8.2's
    own worked bound."""
    return COUNTER_BAND_MULT * (1.0 + H2H_BAND_SHADE * shade) * record.threshold


def _out_player_count(record: DecisionRecord) -> int:
    """Count of `out:player:*`-labelled terms on `record` -- the same
    quantity §4.3's availability column calls `|O|`: A's own outgoing
    player count, read off either side's record (A's `out:player:` terms
    and B's `in:player:` terms name the identical physical set, since a
    negotiation's `package` is always expressed from the initiator's
    perspective, §4.1). `respond()` below always reads it off
    `record_a`."""
    return sum(1 for term in record.terms if term.label.startswith("out:player:"))


def respond(
    record_a: DecisionRecord,
    record_b: DecisionRecord,
    *,
    rounds_remaining: int,
    max_outgoing_players: int = 3,
) -> NegotiationStance:
    """§4.2's per-turn transition rule, total over the verdict cross-
    product, implemented in the exact order the design doc states --
    **legality first (Decision P3-6), before anything reads a gap.** An
    `illegal:*` record has `score = threshold = 0.0`
    (`trade.py:648-652`), so `gap = 0.0`, so a gap-first implementation
    would read every illegal package as `ACCEPT`. This function checks
    `verdict.startswith("illegal:")` on both records before touching
    `.score`/`.threshold` at all, which is what makes that trap
    structurally unreachable rather than merely untested.

    `record_a`/`record_b` are one package's two evaluations -- `record_a`
    the initiator's own acceptance decision, `record_b` the responder's,
    matching §4.1's `NegotiationTurn.record_a, record_b` fields exactly.

    `rounds_remaining` is a plain parameter, never a module constant --
    Decision P3-8 keeps `MAX_NEGOTIATION_ROUNDS` in `gm_service.py`
    (services), specifically so the negotiation LOOP can pass
    `rounds_remaining=0` for M29's control run without editing a
    `managers/`-scoped constant (which would trip `GM_TUNING_VERSION`).
    This function only ever asks "is there a round left," never how many
    rounds a negotiation is allowed in total.

    `max_outgoing_players` mirrors `gm_service.py::_MAX_OUTGOING_PLAYERS`
    (`= 3` today) -- a **services-scoped** constant `managers/` cannot
    import (Decision 4's layering rule forbids `managers/` -> `services/`
    entirely, and this constant is private besides). Passed explicitly
    with a default matching the live value, rather than hidden behind a
    second, silently-driftable copy of `3` inside this module.

    **Channel 3's shading (Decision P3-26/§8.2, step 12) deliberately does
    NOT reach this function as a parameter.** A new keyword parameter here
    -- even a `None`-defaulted one -- is itself a new declaration under
    `dba2/managers/`, which `test_constant_inventory.py`'s own unfiltered
    walker tracks (site (c): "numeric/string/tuple defaults in any
    FunctionDef's args.defaults/kw_defaults") -- confirmed by measurement,
    not assumption: adding `band_override: float | None = None` here trips
    the committed-snapshot test with `added=['...respond.band_override']`,
    which is exactly Decision P3-30's "nothing after step 4 adds or moves
    one [managers/-scoped declaration]" guard doing its job. Channel 3's
    real, live effect on this function's own decision is instead achieved
    by the CALLER (`gm_service.py::_run_negotiation`) substituting a
    synthetic copy of `actor_record` -- same `verdict`/`terms`, `threshold`
    rescaled so this function's own unmodified `counter_band(actor_record)`
    naturally evaluates to the shaded `band_eff`, and `score` shifted by
    the identical delta so `gap = score - threshold` is UNCHANGED -- before
    calling this function. This function itself, and `counter_band()`, are
    therefore BYTE-IDENTICAL to their pre-step-12 selves; only the record
    the caller hands in differs, and only for the one call that has a real
    shade reading to apply.

    Per §4.2's own pseudocode, read top to bottom:

    1. Legality (Decision P3-6) -> `REJECT_ILLEGAL`, terminal.
    2. The cell, over the both-legal population only:
       `(accept, accept)` -> `ACCEPT`; `(reject, reject)` ->
       `REJECT_DEADLOCK` (Decision P3-14 -- both terminal).
    3. Exactly one side is short, so exactly one repair axis exists
       (Decision P3-10): `(reject, accept)` -> A is the actor, give-less
       is the axis; `(accept, reject)` -> B is the actor, ask-more is the
       axis. Then, in order: a gap below `-counter_band(actor_record)`
       terminates `REJECT_TOO_FAR` (a genuinely far package); exhausting
       the round budget terminates `EXPIRE`; an axis with no legal move
       available (give-less needs `|O| >= 2`; ask-more needs
       `|O| < max_outgoing_players`, §4.3's availability column)
       terminates `REJECT_TOO_FAR` again (the *other*, in-band-but-
       unavailable cause -- §4.2's own "two causes, not the same event"
       note); anything left over is `COUNTER`.

    **Deliberately not built here: stall detection.** See this module's
    own docstring -- `EXPIRED:stalled` is a loop-level override of a
    would-be `COUNTER`, not one of this total function's own branches,
    and the negotiation loop that could supply the turn-history it needs
    does not exist until step 7."""
    if record_a.verdict.startswith("illegal:") or record_b.verdict.startswith("illegal:"):
        return NegotiationStance.REJECT_ILLEGAL

    a_accepts = record_a.verdict == "accept"
    b_accepts = record_b.verdict == "accept"

    if a_accepts and b_accepts:
        return NegotiationStance.ACCEPT
    if not a_accepts and not b_accepts:
        return NegotiationStance.REJECT_DEADLOCK

    outgoing_count = _out_player_count(record_a)
    if not a_accepts:  # cell == (reject, accept) -- A is short, give-less axis
        actor_record = record_a
        axis_available = outgoing_count >= 2
    else:  # cell == (accept, reject) -- B is short, ask-more axis
        actor_record = record_b
        axis_available = outgoing_count < max_outgoing_players

    gap = actor_record.score - actor_record.threshold
    if gap < -counter_band(actor_record):
        return NegotiationStance.REJECT_TOO_FAR
    if rounds_remaining <= 0:
        return NegotiationStance.EXPIRE
    if not axis_available:
        return NegotiationStance.REJECT_TOO_FAR
    return NegotiationStance.COUNTER


# ---------------------------------------------------------------------------
# Decision P3-9 / architect round-10's C9 -- the stall predicate's pure
# arithmetic half. See this module's own docstring for the scope decision
# (item 9 of this dispatch's own task) -- the loop, not this function,
# owns turn-history bookkeeping; this function owns the numbers once the
# loop has already picked the right two turns to compare.
# ---------------------------------------------------------------------------


def is_stalled(
    *,
    actor: int,
    previous_actor: int | None,
    previous_gap: float,
    gap: float,
    band: float,
) -> bool:
    """`True` iff a counter failed to move `|gap|` toward zero by at
    least `MIN_CONCESSION_FRAC * band` (Decision P3-9), compared **only
    between turns with the same actor** -- a cell flip resets the
    baseline rather than comparing across actors (architect round-10's
    C9, restated in §4.2/§7). Both halves of that rule are asserted here,
    not left to the caller:

    - `previous_actor is None` (this actor's first turn in the
      negotiation) or `previous_actor != actor` (the cell flipped since
      the last turn) -> never stalled, `False` -- the caller should treat
      this turn's own `gap` as the new baseline for this actor going
      forward, exactly as "a negotiation whose cell flips every turn
      therefore never stalls and expires on the round cap instead" (§4.2)
      requires.
    - `previous_actor == actor` -> stalled iff
      `abs(previous_gap) - abs(gap) < MIN_CONCESSION_FRAC * band`, both
      read off the acting side's own record for the package on the table
      (never the other side's, and never the terminal situation token --
      §7's "reads the `gap`, never the terminal token" rule, which this
      function's signature makes structurally true: it has no token
      parameter to misread).

    `band` is the acting side's own `counter_band(actor_record)` at the
    CURRENT turn, matching §4.2's "the acting side's own gap against its
    own band" -- a package's `S_out` can move between turns (a counter
    changes what is on the table), so the band is not assumed stable
    across the comparison."""
    if previous_actor is None or previous_actor != actor:
        return False
    concession = abs(previous_gap) - abs(gap)
    return concession < MIN_CONCESSION_FRAC * band
