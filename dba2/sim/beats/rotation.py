"""Beat 4: kick-out read + tag/no-tag on the roll + pop
(phase-1-simulation-engine.md §4.3 table, §4.4's three Rotation rows;
docs/design/pick-and-pop.md adds the third row, `rotation_pop`).

Zero I/O, per Decision P1-1. Restricted the same way `on_ball.py`/
`help.py` are (Decision P1-16): the only `PlayerTendencies` field this
file may read is one of the allowlisted names, and neither ability
vector may contain a tendency -- enforced by `tests/unit/sim/test_tuning.py`.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from random import Random
from types import MappingProxyType

from dba2.sim.composites import passer_score
from dba2.sim.context import SimPlayer
from dba2.sim.events import BeatRecord
from dba2.sim.probability import ability_score, contest_probability, rating_value, weighted_choice
from dba2.sim.tuning import DEFAULT_TUNING, RotationBranchTuning, TuningProfile


class RotationOutcome(StrEnum):
    KICKOUT_OPEN = "kickout_open"
    KICKOUT_CONTESTED = "kickout_contested"
    ROLL_LIVE = "roll_live"
    ROLL_TAGGED = "roll_tagged"
    # docs/design/pick-and-pop.md PP-4: the screener's second live threat --
    # only reachable inside the screener branch, alongside ROLL_*, both
    # handed to beat 5 with the screener as shooter (PP-2).
    POP_OPEN = "pop_open"
    POP_CONTESTED = "pop_contested"
    NO_PASS = "no_pass"


# docs/design/pick-and-pop.md PP-1b/PP-9.2: replaces the chained conditional
# that used to derive `RotationBeatResult.branch` -- that conditional's final
# `else` was `"no_pass"`, so adding POP_OPEN/POP_CONTESTED as one more `elif`
# would have silently mislabeled every pop as branch "no_pass" in stored beat
# data, with no exception and no existing test catching it. A total mapping
# derived from the enum itself (checked below, a real `raise` at import) means
# an eighth outcome added later cannot repeat that failure mode silently.
_ROTATION_OUTCOME_TO_BRANCH: Mapping[RotationOutcome, str] = MappingProxyType(
    {
        RotationOutcome.KICKOUT_OPEN: "kickout",
        RotationOutcome.KICKOUT_CONTESTED: "kickout",
        RotationOutcome.ROLL_LIVE: "roll",
        RotationOutcome.ROLL_TAGGED: "roll",
        RotationOutcome.POP_OPEN: "pop",
        RotationOutcome.POP_CONTESTED: "pop",
        RotationOutcome.NO_PASS: "no_pass",
    }
)


def _require_rotation_outcome_branch_map_is_total(mapping: Mapping[RotationOutcome, str]) -> None:
    """PP-9.2's pin. Real `raise`, not `assert` (stripped by `python -O`) --
    an outcome missing from the map is exactly PP-1b's foot-gun: a builder
    who adds an enum member and forgets this map ships a silent mislabel,
    not a crash, unless this check exists."""
    missing = [member.value for member in RotationOutcome if member not in mapping]
    if missing:
        raise RuntimeError(
            f"_ROTATION_OUTCOME_TO_BRANCH is missing RotationOutcome member(s): {missing} -- "
            "every RotationOutcome must map to a branch label or RotationBeatResult.branch "
            "silently lies about that outcome's branch"
        )


_require_rotation_outcome_branch_map_is_total(_ROTATION_OUTCOME_TO_BRANCH)


def _require_pop_share_anchors_are_ordered_and_in_unit_range(tuning: TuningProfile) -> None:
    """PP-9.3's pin. `pop_share_anchor_low[0] < pop_share_anchor_high[0]`
    (else PP-7's interpolation divides by zero or inverts), and both shares
    in `[0.0, 1.0]` -- a share above 1.0 would make the roll leg's weight
    negative, which `weighted_choice` clamps to 0.0 via `max(w, 0.0)`
    (`probability.py`) rather than reporting it. The selection gate's own
    fields (`pop_shooting_weights`) are coupled to `shot_field_by_zone`'s
    catch column (PP-5) -- an inverted or out-of-range anchor pair breaks
    that coupling silently instead of raising, which is why this is a real
    `raise` at import, not a comment asserting it."""
    cfg = tuning.rotation_branch
    lo_score, lo_share = cfg.pop_share_anchor_low
    hi_score, hi_share = cfg.pop_share_anchor_high
    if lo_score >= hi_score:
        raise RuntimeError(
            "rotation_branch.pop_share_anchor_low's score "
            f"({lo_score!r}) must be strictly less than pop_share_anchor_high's score "
            f"({hi_score!r})"
        )
    for name, share in (("pop_share_anchor_low", lo_share), ("pop_share_anchor_high", hi_share)):
        if not (0.0 <= share <= 1.0):
            raise RuntimeError(
                f"rotation_branch.{name}'s share ({share!r}) must be in [0.0, 1.0]"
            )


_require_pop_share_anchors_are_ordered_and_in_unit_range(DEFAULT_TUNING)


# §4.4's Rotation(kick-out) offense ability vector spans two different
# actors ("off_ball_movement, relocation, cutting, basketball_iq (vacated
# man); passing_vision, passing_accuracy, pass_under_pressure (passer)")
# -- the one row in the whole table that isn't a single-player vector.
# `probability.ability_score` only ever reads one player, so this file
# splits the vector's field names by which named actor each belongs to
# and computes the weighted mean across both players itself, rather than
# inventing a second ability-vector-reading primitive in `probability.py`
# for a shape used exactly once in the entire beat table.
_VACATED_MAN_FIELDS = frozenset({"off_ball_movement", "relocation", "cutting", "basketball_iq"})
_PASSER_FIELDS = frozenset({"passing_vision", "passing_accuracy", "pass_under_pressure"})


def _kickout_offense_score(
    vacated_shooter: SimPlayer, passer: SimPlayer, weights
) -> float:
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ValueError("kickout offense weights must sum to a positive total")
    total = 0.0
    for field_name, w in weights.items():
        if field_name in _VACATED_MAN_FIELDS:
            total += rating_value(vacated_shooter, field_name) * w
        elif field_name in _PASSER_FIELDS:
            total += rating_value(passer, field_name) * w
        else:  # pragma: no cover -- guarded by test_tuning.py's field-name check
            raise ValueError(f"unrecognized kickout ability-vector field: {field_name}")
    return total / total_weight


def _passer_tilt_multiplier(passer: SimPlayer, branch_cfg: RotationBranchTuning) -> float:
    """docs/design/per-36-spread-compression.md SC-3.5/SC-7 Commit 3: the
    kickout/roll-vs-no_pass split was ability-blind -- `tuning.py`'s own
    former `SetupTuning` comment (still true of `_role_weight`'s ball-
    handler-selection draw) flagged it as a residual: "how often a
    `HELP_COMMITTED`/`HELP_LATE` read even attempts a pass is unaffected...
    since `rotation_branch`'s kickout/roll-vs-no_pass split is a fixed
    rate, not scaled by the passer's own ability." This tilts it, over
    `dba2.sim.composites.passer_score`, imported rather than restated
    (SC-7 Commit 3 step 2), so this tilt and `beats/setup.py`'s
    concentration multiplier can never silently drift on what "passer
    ability" means.

    **Log-linear (geometric), not linear, interpolation -- corrected by a
    forward-fix, see `RotationBranchTuning`'s own docstring for the full
    account of what the original commit got wrong and why.** The mechanism
    this multiplier feeds is multiplicative (`resolve_rotation_beat` below:
    `kickout`/`roll` weights `*= tilt`, `no_pass` weight `/= tilt`), so
    `passer_tilt_anchor_low`/`_high`'s multipliers are exact reciprocals
    (`lo_mult * hi_mult == 1.0`, enforced by `RotationBranchTuning.
    __post_init__`) rather than additively symmetric around 1.0. Linearly
    interpolating raw multipliers between a reciprocal pair does **not**
    generally cross 1.0 at the score midpoint (e.g. the line from 0.87 to
    1.15 crosses 1.0 off-center); interpolating `log(multiplier)` linearly
    does, by construction, for any reciprocal pair -- so a passer scoring
    exactly `RotationBranchTuning.neutral_passer_score` still returns
    exactly 1.0, reproducing today's exact 0.45/0.35/0.20 split with no
    tilt applied, the same exactness guarantee the original (additively-
    symmetric, linear) construction had, now correct for a genuinely
    mean-preserving-at-the-anchors multiplier pair too."""
    score = passer_score(passer.ratings)
    lo_score, lo_mult = branch_cfg.passer_tilt_anchor_low
    hi_score, hi_mult = branch_cfg.passer_tilt_anchor_high
    if score <= lo_score:
        return lo_mult
    if score >= hi_score:
        return hi_mult
    t = (score - lo_score) / (hi_score - lo_score)
    log_lo, log_hi = math.log(lo_mult), math.log(hi_mult)
    return math.exp(log_lo + t * (log_hi - log_lo))


def _pop_share(screener: SimPlayer, branch_cfg: RotationBranchTuning) -> float:
    """docs/design/pick-and-pop.md PP-7: the roll-vs-pop share `s` is not a
    contest -- it's "how much of this screener's threat is shooting," a
    clamped two-anchor linear interpolation of the screener's own
    catch-and-shoot score, the same idiom `beats/setup.py`'s
    `_overall_to_touch_share_estimate`/`_ball_handler_passing_concentration`
    already use twice. Clamped outside the anchor range in both directions:
    a below-floor shooter floors at the low anchor's share rather than
    extrapolating toward a negative share; an above-ceiling shooter caps at
    the high anchor's share rather than extrapolating past 1.0."""
    score = ability_score(screener, branch_cfg.pop_shooting_weights)
    lo_score, lo_share = branch_cfg.pop_share_anchor_low
    hi_score, hi_share = branch_cfg.pop_share_anchor_high
    if score <= lo_score:
        return lo_share
    if score >= hi_score:
        return hi_share
    t = (score - lo_score) / (hi_score - lo_score)
    return lo_share + t * (hi_share - lo_share)


@dataclass(frozen=True)
class RotationBeatResult:
    outcome: RotationOutcome
    branch: str  # "kickout" | "roll" | "pop" | "no_pass" -- which sub-contest fired
    probabilities: dict[str, float]
    record: BeatRecord


def resolve_rotation_beat(
    *,
    vacated_shooter: SimPlayer,
    passer: SimPlayer,
    closing_defender: SimPlayer,
    screener: SimPlayer | None,
    tagger: SimPlayer | None,
    popper_defender: SimPlayer | None = None,
    tuning: TuningProfile,
    rng: Random,
    clock_seconds: float,
    home_score: int,
    away_score: int,
) -> RotationBeatResult:
    """Beat 4, only ever called on `HELP_COMMITTED`/`HELP_LATE` (§4.3's
    table). One of up to 4 mutually exclusive reads happens: a kick-out to
    the vacated shooter (the offensive player whose primary defender is the
    committed helper -- Decision P1-9's matchup pairing, resolved by the
    caller), a feed to the rolling screener, a feed to the popping screener
    (docs/design/pick-and-pop.md), or no pass at all (`NO_PASS`, the driver
    keeps the ball and attacks straight into beat 5's resolution). Roll and
    pop are only offered when the action type's role template actually
    names a screener -- `screener` is `None` otherwise, in which case
    neither `tagger` nor `popper_defender` matter.

    **`popper_defender` (PP-9): availability is expressed entirely through
    parameter presence, matching this function's existing convention.**
    `roll_available = screener is not None and tagger is not None`;
    `pop_available = screener is not None and popper_defender is not None`.
    **PP-9.1: `popper_defender=None` is exactly inert** -- no pop key is
    ever inserted and the `probabilities` dict is key-for-key,
    order-for-order, value-for-value identical to before this parameter
    existed. The pop keys are inserted in their own block *after* the
    existing roll block (never interleaved into the base dict literal),
    because `weighted_choice` walks `weights.items()` in insertion order
    and accumulates -- two dicts with the same keys in a different order
    can resolve the same `rng.random()` draw to a different outcome.

    **Composition with the passer tilt (docs/design/pick-and-pop.md PP-3/
    PP-17.2): the pop split operates strictly DOWNSTREAM of the tilt.** The
    tilt (per-36 SC-7 Commit 3) multiplies the three base weights first;
    the pop share `s` then splits the already-tilted screener-branch weight
    (`roll_base_weight * tilt`) between roll and pop. The two axes are
    different actors (ball-handler's passing vs. screener's shooting) and
    the tilt's log-linear/reciprocal construction is untouched: total
    screener-branch mass, and therefore `branch_total`, is identical to
    what it was without this feature.

    **PP-3 Option A / PP-6.1: the screener branch's mass (`roll_base_
    weight`) is split ability-weighted between the roll and pop legs when
    both are available (`pnr_ball`), and is not split at all when only one
    leg is available** -- when only roll is available (today's shape, and
    every existing caller of this function), the screener branch's mass
    goes to roll in full, unsplit, byte-identical to before this feature.
    When only pop is available (`dho`, whose `handoff_big` never rolls),
    the *pop's share* of that mass is offered and the remaining
    `(1 - s) * roll_base_weight` is simply dropped, not reallocated --
    `branch_total` renormalizes over whichever branches actually have
    mass, matching `weighted_choice`'s existing "renormalizes over
    reachable branches" shape.

    Reports one flattened probability distribution (3 to 7 leaves,
    depending on which of roll/pop are available) and draws with a single
    `weighted_choice` call, matching `help.py`'s "one draw" shape rather
    than a branch draw followed by a second conditional draw."""
    branch_cfg = tuning.rotation_branch
    # docs/design/per-36-spread-compression.md SC-7 Commit 3 (Nit 6): tilt
    # the three base weights by the passer's own ability BEFORE
    # branch_total is computed, so the effect is equal strength whether or
    # not a roll leg is reachable (`pnr_ball`'s 0.800 base pass rate vs
    # `dho`/`iso`/`transition`'s 0.692) -- applying it after renormalization
    # would not, since the two action shapes start from different branch
    # totals.
    tilt = _passer_tilt_multiplier(passer, branch_cfg)
    if tilt <= 0.0:
        # `RotationBranchTuning.__post_init__` already rejects a config
        # whose anchor multipliers could produce this through the normal
        # dataclass constructor (including `dataclasses.replace`, which
        # re-runs `__post_init__`), but the divide below
        # (`no_pass_base_weight / tilt`) doesn't know that -- guard here
        # too, defense-in-depth, rather than let any future bypass of that
        # validation (e.g. a mock/test double standing in for
        # `RotationBranchTuning`) surface as a bare ZeroDivisionError with
        # no context.
        raise ValueError(
            f"passer tilt multiplier must be positive, got {tilt!r} for "
            f"passer {passer.id}"
        )
    tilted_kickout_weight = branch_cfg.kickout_base_weight * tilt
    tilted_roll_weight = branch_cfg.roll_base_weight * tilt
    tilted_no_pass_weight = branch_cfg.no_pass_base_weight / tilt
    branch_weights: dict[str, float] = {
        "kickout": tilted_kickout_weight,
        "no_pass": tilted_no_pass_weight,
    }
    roll_available = screener is not None and tagger is not None
    pop_available = screener is not None and popper_defender is not None
    if roll_available and pop_available:
        pop_share = _pop_share(screener, branch_cfg)
        branch_weights["roll"] = tilted_roll_weight * (1.0 - pop_share)
        branch_weights["pop"] = tilted_roll_weight * pop_share
    elif roll_available:
        branch_weights["roll"] = tilted_roll_weight
    elif pop_available:
        pop_share = _pop_share(screener, branch_cfg)
        branch_weights["pop"] = tilted_roll_weight * pop_share
    branch_total = sum(branch_weights.values())

    kt = tuning.rotation_kickout
    kickout_offense = _kickout_offense_score(vacated_shooter, passer, kt.offense_weights)
    kickout_defense = ability_score(closing_defender, kt.defense_weights)
    p_open = contest_probability(
        kickout_offense, kickout_defense, base_rate=kt.base_rate, scale=kt.scale, band=kt.band
    )

    probabilities: dict[str, float] = {
        RotationOutcome.KICKOUT_OPEN.value: (branch_weights["kickout"] / branch_total) * p_open,
        RotationOutcome.KICKOUT_CONTESTED.value: (branch_weights["kickout"] / branch_total)
        * (1.0 - p_open),
        RotationOutcome.NO_PASS.value: branch_weights["no_pass"] / branch_total,
    }

    if roll_available:
        rt = tuning.rotation_roll_tag
        roll_offense = ability_score(screener, rt.offense_weights)
        roll_defense = ability_score(tagger, rt.defense_weights)
        p_live = contest_probability(
            roll_offense, roll_defense, base_rate=rt.base_rate, scale=rt.scale, band=rt.band
        )
        probabilities[RotationOutcome.ROLL_LIVE.value] = (
            branch_weights["roll"] / branch_total
        ) * p_live
        probabilities[RotationOutcome.ROLL_TAGGED.value] = (
            branch_weights["roll"] / branch_total
        ) * (1.0 - p_live)

    # PP-9.1: this block must stay after the roll block above -- see this
    # function's own docstring for why insertion order is load-bearing.
    if pop_available:
        pt = tuning.rotation_pop
        # PP-8.3: the popper is contested by his own live matchup defender
        # (pre-switch the screen defender, post-switch whoever the switch
        # put on him), never the helper -- the one place this beat is
        # deliberately not parallel to the roll branch. `popper_defender`
        # is resolved by the caller from the possession's live matchup
        # dict; this function only ever reads whichever player it's given.
        pop_offense = ability_score(screener, pt.offense_weights)
        pop_defense = ability_score(popper_defender, pt.defense_weights)
        p_pop_open = contest_probability(
            pop_offense, pop_defense, base_rate=pt.base_rate, scale=pt.scale, band=pt.band
        )
        probabilities[RotationOutcome.POP_OPEN.value] = (
            branch_weights["pop"] / branch_total
        ) * p_pop_open
        probabilities[RotationOutcome.POP_CONTESTED.value] = (
            branch_weights["pop"] / branch_total
        ) * (1.0 - p_pop_open)

    outcome = RotationOutcome(weighted_choice(rng, probabilities))
    branch = _ROTATION_OUTCOME_TO_BRANCH[outcome]

    actor_ids = {
        "vacated_shooter": vacated_shooter.id,
        "passer": passer.id,
        "closing_defender": closing_defender.id,
    }
    if roll_available:
        actor_ids["screener"] = screener.id
        actor_ids["tagger"] = tagger.id
    if pop_available:
        actor_ids["screener"] = screener.id
        actor_ids["popper_defender"] = popper_defender.id

    record = BeatRecord(
        beat="rotation",
        actor_ids=actor_ids,
        outcome=outcome.value,
        probabilities=probabilities,
        clock_seconds=clock_seconds,
        home_score=home_score,
        away_score=away_score,
    )
    return RotationBeatResult(
        outcome=outcome, branch=branch, probabilities=probabilities, record=record
    )
