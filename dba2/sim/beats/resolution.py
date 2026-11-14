"""Beat 5: zone selection, contest, make/miss, foul, turnover, free throws
(phase-1-simulation-engine.md §4.3/§4.4/§4.5/§4.6).

Zero I/O, per Decision P1-1. Restricted per Decision P1-16 like every
other resolution beat: the only `PlayerTendencies` fields this file may
read are `pullup_vs_catch`, `foul_freq`, `steal_gamble_freq`, and (B3 fix,
critic-proven -- see `tuning.ResolutionShotTuning`'s docstring for the
full history) `shot_freq_rim`/`shot_freq_mid`/`shot_freq_three` -- 6 of
the 8 allowlisted names (Decision P1-16 amendment) -- enforced by
`tests/unit/sim/test_tuning.py`. Zone selection (`select_shot_zone`,
below) lives here, matching §12 step 4's own outline text ("resolution
(zone selection, contest, make/miss...)"), not in `playbook.py`.

**Interpretive call, flagged in this dispatch's build report: and-ones,
shooting fouls, non-shooting fouls, and offensive fouls all draw from one
shared `resolution_foul` contest** ("a foul happens on this beat"), which
this beat then splits by flavor via `tuning.foul_split`'s fixed shares --
§4.4's table gives exactly one Resolution(foul) row, not four, so a
single contest with a documented flavor split is this dispatch's reading
of "one ability vector governs whether a foul is drawn at all," with the
*kind* of foul left to a data-driven split the same shape `ScreenTuning.
defense_outcome_shares` already uses for an analogous problem. All 9
leaf outcomes below are computed once and drawn with a single
`weighted_choice` call, matching every other beat's "flatten then one
draw" shape.

BDA-4 (docs/design/block-defender-attribution.md) adds a 10th leaf,
`blocked_miss_help`, to the same single `weighted_choice` draw -- a
weak-side rim-help contest, zone-gated to `Zone.RIM` only, riding the
existing draw. docs/design/block-attribution-v2.md BA-2.1 makes it a
contest over every eligible helper rather than the floor's single best rim
protector: one probability (the share-weighted mean of the helpers' own
contests) enters the leaf draw, and the blocker is drawn from the helpers
only when that leaf is drawn -- TR-4.7's recoverer shape below (see
`resolve_resolution_beat`'s own docstring for the gates and
`TuningProfile.resolution_rim_help` for the tuning).

docs/design/turnover-recovery.md TR-4.1 adds an 11th leaf,
`loose_ball_recovered`, carved out of `turnover_live`'s own mass by the
caller-supplied recovery probability -- the offense won its own loose ball,
which is not a turnover (TR-2). It too rides the one existing draw (TR-4.8),
and at the default probability of `0.0` its key is omitted entirely, so the
leaf dict, its order, its sum and its cumulative walk are exactly the
pre-TR shape. When that leaf is drawn -- and only then -- one further
`weighted_choice` names the recoverer (`beats/loose_ball.py::draw_recoverer`,
TR-4.7 as revised 2026-09-24)."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from random import Random

from dba2.domain.role import ShotDiet
from dba2.sim.beats.loose_ball import draw_recoverer
from dba2.sim.composites import (
    COMMITTED_HELPER_PENALTY,
    aggression_linear_scale,
    contest_quality,
)
from dba2.sim.context import SimPlayer
from dba2.sim.events import BeatRecord, TerminalKind, TerminalOutcome, Zone
from dba2.sim.playbook import ActionType
from dba2.sim.probability import (
    ability_score,
    clamp,
    contest_probability,
    rating_value,
    weighted_choice,
)
from dba2.sim.tuning import DEFAULT_TUNING, TuningProfile

# §4.4's Resolution(shot)/Resolution(block) rows both read "contest
# quality," but the table names concrete fields only for the block row
# (rim_protection/vertical/wingspan_in/lateral_quickness). Reusing
# `resolution_block.defense_weights` for the rim/mid shot-make contest --
# rather than declaring a near-duplicate weight vector nowhere the doc
# calls for one -- is this dispatch's documented interpretive call:
# contesting a shot near the rim and contesting/blocking it draw on the
# same physical-defense skill set.
#
# B25-2 fix (critic-round-25-proven): a three-point shot is NOT the same
# contest -- rim_protection/vertical/wingspan_in are a *rim*-defense skill
# set, and reusing them for a jump shot 23+ feet away sign-inverted the
# beat (a drop-coverage rim anchor with no closeout speed measured as the
# *best* three-point defender in the engine, 28.7 points better than a
# true perimeter stopper). `select_contest_weights` below branches on zone:
# `tuning.resolution_shot.three_contest_weights` (closeout_speed/
# closeout_control, the same pair `beats/rotation.py`'s kick-out contest
# already uses) for a three, `resolution_block.defense_weights` unchanged
# for rim/mid/post (both measured inside their own reference bands at the
# old wiring, so this is a three-only fix, not a both-sides-of-every-zone
# rewire).
_CONTEST_QUALITY_SOURCE = (
    "resolution_block.defense_weights (rim/mid); resolution_shot.three_contest_weights (three)"
)


def _select_contest_weights(zone: Zone, tuning: TuningProfile) -> Mapping[str, float]:
    if zone is Zone.THREE:
        return tuning.resolution_shot.three_contest_weights
    return tuning.resolution_block.defense_weights


@dataclass(frozen=True)
class ResolutionBeatResult:
    outcome: TerminalOutcome
    probabilities: dict[str, float]
    record: BeatRecord


def _three_point_bias_scale(three_point_bias: int, tuning: TuningProfile) -> tuple[float, float]:
    """docs/design/gm-command-surface.md PD-2: `team_strategy.three_point_
    bias` (0-100, neutral 50) linearly mapped onto `tuning.resolution_shot
    .three_point_bias_lo/_hi`, mirroring `beats/rebound.py::_crash_bias_
    scale` exactly (same modifier shape, different tuning source). Returns
    `(three_scale, mid_scale)`: applied multiplicatively to `Zone.THREE`
    and, **inversely**, to `Zone.MID` -- `mid_scale` is `three_scale`'s
    exact reciprocal (an interpretive call, not spelled out further than
    "applied ... inversely to MID" in the design doc), chosen specifically
    because it makes acceptance criterion 3a's "neutral is exactly 1.0 for
    BOTH directions" hold from a single pinned pair: the reciprocal of an
    exact IEEE754 `1.0` is exactly `1.0` too, so `three_point_bias_lo/_hi`
    only has to be verified exact once (`_require_neutral_three_point_
    bias_scale_is_exactly_one` below), not twice against two independently
    -tuned pairs. `Zone.RIM` gets no factor from this knob at all -- the
    design names only THREE/MID as the pair this bias moves."""
    cfg = tuning.resolution_shot
    t = clamp(three_point_bias / 100.0, 0.0, 1.0)
    three_scale = cfg.three_point_bias_lo + t * (cfg.three_point_bias_hi - cfg.three_point_bias_lo)
    return three_scale, 1.0 / three_scale


def select_shot_zone(
    shooter: SimPlayer, rng: Random, tuning: TuningProfile, three_point_bias: int
) -> Zone:
    """§4.4's Resolution(shot) row: "the shooting column for the drawn
    zone." B3 fix (critic-proven): this used to live in `playbook.py`,
    specifically to dodge this file's Decision P1-16 mechanical guard
    (`shot_freq_rim`/`_mid`/`_three` weren't on the allowlist) -- critic
    proved that was an evasion of the guard's file-scoped reach, not a
    legitimate selection-beat distinction, since this draw happens once
    per shot attempt inside the same beat that immediately contests it,
    not once per possession the way `beats/setup.py::select_action_type`'s
    role/action-type selection does. The real fix was amending the
    allowlist (Decision P1-16 amendment, `tuning.ResolutionShotTuning`'s
    docstring) to add these 3 names, and moving this function back here,
    where §12 step 4's own outline text says it belongs.

    `shot_freq_post` is deliberately excluded from the weights here --
    `post_up` actions use `tuning.resolution_shot.post_shot` as a
    zone-independent special case (per §4.4: "post_scoring for post
    shapes"), so this function is never called for a post-up shot
    attempt; `resolve_resolution_beat` branches around it entirely.

    **`tuning`/`three_point_bias` are new parameters (PD-2, build step 7)**:
    `three_point_bias` is `team_strategy.three_point_bias` for the
    offense's own team (§2.4/PD-2: previously a shipped, validated,
    persisted, re-derived, rendered knob no engine file read at all).

    **`shooter.directive.shot_diet` is read here too (PD-3, build step
    8)** -- the same seam, a later, separate commit per the design's own
    build order, added on top of PD-2's already-shipped team knob rather
    than replacing it (2.4: "wire three_point_bias and shot_diet at the
    same seam, in the same change" -- read as "same seam", not "same
    commit"; build order step 7/8 is the literal sequencing). A player
    with no directive row (`shooter.directive is None`) or a directive row
    whose `shot_diet` field was never set (`None`) both read as
    `ShotDiet.BALANCED`, this enum's own neutral member -- multiplying
    every zone weight by 1.0, i.e. today's behaviour, unchanged."""
    three_scale, mid_scale = _three_point_bias_scale(three_point_bias, tuning)
    shot_diet = (
        shooter.directive.shot_diet
        if shooter.directive is not None and shooter.directive.shot_diet is not None
        else ShotDiet.BALANCED
    )
    diet_mult = tuning.resolution_shot.shot_diet_zone_multipliers[shot_diet]
    weights = {
        Zone.RIM: max(shooter.tendencies.shot_freq_rim, 0) * diet_mult["rim"],
        Zone.MID: max(shooter.tendencies.shot_freq_mid, 0) * diet_mult["mid"] * mid_scale,
        Zone.THREE: max(shooter.tendencies.shot_freq_three, 0) * diet_mult["three"] * three_scale,
    }
    return weighted_choice(rng, weights)


def _require_shot_diet_zone_multipliers_are_total_and_strictly_positive(
    tuning: TuningProfile,
) -> None:
    """PD-2 disclosure 3 + mutation 14/17: every `ShotDiet` member must
    have an entry, every entry must cover all three zone names, and every
    cell must be strictly positive -- a zero would let `weighted_choice`'s
    total collapse to zero for a shooter whose other two zone tendencies
    are also zero, diverting it into its `rng.choice` fallback branch
    (`probability.py:114-116`) and desyncing every subsequent draw in that
    seeded game. Checked with a real `raise`, the `advance.py::_require_a_
    reply_for_every_press_kind` shape -- an `assert` is stripped by
    `python -O`."""
    table = tuning.resolution_shot.shot_diet_zone_multipliers
    missing_diets = sorted(v.value for v in set(ShotDiet) - set(table))
    if missing_diets:
        raise RuntimeError(
            f"shot_diet_zone_multipliers is missing entries for: {missing_diets}"
        )
    non_positive: list[str] = []
    for diet, zones in table.items():
        missing_zones = sorted({"rim", "mid", "three"} - set(zones))
        if missing_zones:
            raise RuntimeError(
                f"shot_diet_zone_multipliers[{diet.value!r}] is missing zone(s): {missing_zones}"
            )
        non_positive.extend(
            f"{diet.value}.{zone}={value!r}" for zone, value in zones.items() if value <= 0.0
        )
    if non_positive:
        raise RuntimeError(
            "shot_diet_zone_multipliers must be strictly positive everywhere -- "
            f"got: {non_positive}"
        )


def _require_neutral_three_point_bias_scale_is_exactly_one(tuning: TuningProfile) -> None:
    """PD-2 acceptance criterion 3a, checked with a real `raise` -- an
    `assert` is stripped by `python -O`, and the silent shot-chart drift
    it would have prevented has no other alarm. Round 2's critic proved
    exactness does NOT hold for symmetric `lo`/`hi` pairs in general (124
    of 999 swept pairs missed exactly `1.0`), so this is a real numeric
    check against the actual shipped constants, not a restated assumption."""
    three_scale, mid_scale = _three_point_bias_scale(50, tuning)
    if three_scale != 1.0 or mid_scale != 1.0:
        raise RuntimeError(
            "PD-2's neutral (three_point_bias=50) shot-zone scale must be exactly 1.0 in "
            f"both directions -- got three={three_scale!r}, mid={mid_scale!r}. "
            "tuning.resolution_shot.three_point_bias_lo/_hi no longer pins to an exact "
            "midpoint of 1.0; pick a pair that does (0.7/1.3 is verified to)."
        )


def _require_three_point_bias_lo_hi_are_strictly_positive(tuning: TuningProfile) -> None:
    """PD-2 disclosure 3's other half: `three_point_bias_lo`/`_hi` feed
    `_three_point_bias_scale` multiplicatively (both directly and via
    `mid_scale`'s reciprocal), so either one reaching zero or negative
    would either zero out a zone weight outright or flip `mid_scale`'s
    sign -- both real desync risks, checked at import rather than assumed."""
    cfg = tuning.resolution_shot
    if cfg.three_point_bias_lo <= 0.0 or cfg.three_point_bias_hi <= 0.0:
        raise RuntimeError(
            "tuning.resolution_shot.three_point_bias_lo/_hi must both be strictly positive -- "
            f"got lo={cfg.three_point_bias_lo!r}, hi={cfg.three_point_bias_hi!r}"
        )


_require_neutral_three_point_bias_scale_is_exactly_one(DEFAULT_TUNING)
_require_three_point_bias_lo_hi_are_strictly_positive(DEFAULT_TUNING)
_require_shot_diet_zone_multipliers_are_total_and_strictly_positive(DEFAULT_TUNING)


def _pop_zone_and_shot_inputs(
    shooter: SimPlayer, tuning: TuningProfile, rng: Random, three_point_bias: int
) -> tuple[Zone, str, str]:
    """docs/design/pick-and-pop.md PP-10: a pop's zone is drawn over
    `{MID, THREE}` only -- **never `RIM`** (a popped-beyond-the-arc shot
    classified as a rim look would make the pop invisible in the box score
    as anything other than a roll) -- from the popper's own `shot_freq_mid`/
    `shot_freq_three`, with the same `three_point_bias`/`shot_diet`
    multipliers `select_shot_zone` already applies (a `five_out` team's
    `more_threes` directive should move a pop's zone mix for the same
    reason it moves every other shot's).

    **Zero-total guard (PP-10.1), not optional:** if both zone weights are
    `<= 0`, `weighted_choice` would divert into its `rng.choice` fallback
    (`probability.py`), desyncing every subsequent draw in that seeded
    game -- the same hazard PD-2 disclosure 3 raised for
    `shot_diet_zone_multipliers`. Resolved *before* the draw with an
    explicit `Zone.MID` return (the lower-value, lower-variance outcome --
    a big with no declared shooting tendency at all is taking a short pop,
    not a three), never with a floor that would make the draw meaningless.

    **Field is `shot_field_by_zone[zone]["catch"]`, unconditionally** -- the
    `pullup_vs_catch` `rng.random()` call `_zone_and_shot_inputs` makes for
    a normal shot is NOT made here: a pop is a catch by definition, and a
    dead draw kept "for stream alignment" would be worse than the one
    fewer draw (determinism here is per-`(rng_seed, possession_index)`; a
    branch that didn't exist before has no stream to stay aligned with).

    Returns `(zone, offense_rating_field, block_zone_key)`, same shape as
    `_zone_and_shot_inputs` -- `block_zone_key` stays `zone.value`, so
    `block_zone_multiplier` and `_select_contest_weights`'s `THREE` branch
    both apply unchanged. BDA-4's weak-side rim-help contest cannot fire on
    a pop (its own gate 1 is `zone is Zone.RIM`) -- no interaction to
    design."""
    three_scale, mid_scale = _three_point_bias_scale(three_point_bias, tuning)
    shot_diet = (
        shooter.directive.shot_diet
        if shooter.directive is not None and shooter.directive.shot_diet is not None
        else ShotDiet.BALANCED
    )
    diet_mult = tuning.resolution_shot.shot_diet_zone_multipliers[shot_diet]
    mid_weight = max(shooter.tendencies.shot_freq_mid, 0) * diet_mult["mid"] * mid_scale
    three_weight = max(shooter.tendencies.shot_freq_three, 0) * diet_mult["three"] * three_scale
    if mid_weight <= 0.0 and three_weight <= 0.0:
        zone = Zone.MID
    else:
        zone = weighted_choice(rng, {Zone.MID: mid_weight, Zone.THREE: three_weight})
    field_name = tuning.resolution_shot.shot_field_by_zone[zone.value]["catch"]
    return zone, field_name, zone.value


def _zone_and_shot_inputs(
    shooter: SimPlayer,
    action_type: ActionType,
    tuning: TuningProfile,
    rng: Random,
    three_point_bias: int,
    *,
    is_pop_shot: bool = False,
) -> tuple[Zone, str, str]:
    """Returns `(zone, offense_rating_field, block_zone_key)`.

    **`is_pop_shot` (PP-10) branches to `_pop_zone_and_shot_inputs` before
    the post-up check below** -- a pop is never a post-up (`possession.py`
    never sets both), but the pop branch is deliberately checked first so
    a future third-shape's ordering never has to reason about which of two
    special cases wins.

    Post-up shapes bypass the normal zone/catch-vs-dribble machinery
    entirely -- §4.4: "post_scoring for post shapes" -- and are box-
    classified as `Zone.MID` (documented simplification, flagged in the
    build report: real post shots span rim-adjacent and short-mid looks,
    and the engine has no finer-grained zone to place them in). Per PD-2's
    own disclosure 1, `three_point_bias` (and PD-3's later `shot_diet`)
    therefore never affects a post-up possession -- this branch returns
    before `select_shot_zone` is ever called."""
    if is_pop_shot:
        return _pop_zone_and_shot_inputs(shooter, tuning, rng, three_point_bias)
    if action_type is ActionType.POST_UP:
        return Zone.MID, "post_scoring", "post"
    zone = select_shot_zone(shooter, rng, tuning, three_point_bias)
    is_dribble = rng.random() < (shooter.tendencies.pullup_vs_catch / 100.0)
    field_name = tuning.resolution_shot.shot_field_by_zone[zone.value][
        "dribble" if is_dribble else "catch"
    ]
    return zone, field_name, zone.value


def _rim_help_block_weights(
    pool: Sequence[SimPlayer],
    *,
    primary_defender_id: int,
    committed_id: int | None,
    shooter_score: float,
    help_aggression: int,
    tuning: TuningProfile,
) -> tuple[dict[int, float], dict[int, SimPlayer]]:
    """docs/design/block-attribution-v2.md BA-2.1/BA-2.2: each eligible
    helper's `share_i * p_i`, keyed by his index in `pool` (like
    `draw_recoverer`, so the draw walks pool order and a fixture reusing an
    id cannot merge two helpers), plus the key -> player map. The values sum
    to `p_help_block` and are the blocker draw's weights.

    `c_i` is the helper's cached `rim_protection_contribution`, times
    `COMMITTED_HELPER_PENALTY` if he is `committed_id` (in both uses below).
    `share_i = max(c_i - shift, 1) ** exponent / sum_j (...)`, computed with
    every base divided by the pool's largest first -- the same ratios,
    without overflowing a float at a large exponent (the limit-case test
    runs 200). `p_i` is his own contest against the shooter, times the
    defense's help-aggression scale. Because the shares sum to 1,
    `p_help_block` is at most the best helper's `p_i`: the floor's capacity
    is set by its best blocker, and a second big takes a share of it rather
    than adding to it (BA-2.1 option D, rejected)."""
    cfg = tuning.resolution_rim_help
    helpers = [(key, p) for key, p in enumerate(pool) if p.id != primary_defender_id]
    if not helpers:
        return {}, {}
    scalars = [
        p.rim_protection_contribution * (COMMITTED_HELPER_PENALTY if p.id == committed_id else 1.0)
        for _, p in helpers
    ]
    bases = [max(c - cfg.concentration_shift, 1.0) for c in scalars]
    top = max(bases)
    raw_shares = [(b / top) ** cfg.attribution_concentration_exponent for b in bases]
    share_total = sum(raw_shares)
    aggression_scale = aggression_linear_scale(
        help_aggression, cfg.aggression_lo, cfg.aggression_hi
    )
    weights: dict[int, float] = {}
    for (key, _player), c, raw_share in zip(helpers, scalars, raw_shares, strict=True):
        p_contest = contest_probability(
            c, shooter_score, base_rate=cfg.base_rate, scale=cfg.scale, band=cfg.band
        )
        weights[key] = raw_share / share_total * p_contest * aggression_scale
    return weights, dict(helpers)


def resolve_resolution_beat(
    shooter: SimPlayer,
    defender: SimPlayer,
    *,
    action_type: ActionType,
    tuning: TuningProfile,
    rng: Random,
    clock_seconds: float,
    home_score: int,
    away_score: int,
    is_committed_helper_contest: bool = False,
    assisted: bool = False,
    passer_id: int | None = None,
    rim_help_pool: Sequence[SimPlayer] = (),
    rim_help_committed_id: int | None = None,
    help_aggression: int = 50,
    three_point_bias: int = 50,
    is_pop_shot: bool = False,
    loose_ball_recovery_probability: float = 0.0,
    loose_ball_candidates: Sequence[SimPlayer] = (),
) -> ResolutionBeatResult:
    """Beat 5: the terminal (or continuation-flavored, per §4.5 -- this
    beat does not itself decide terminal-vs-continuation; `possession.py`
    does, using `state.GameState`'s bonus/continuation-budget state) shot
    look. `shooter` is whichever player currently has the ball when the
    chain reaches this beat (the caller resolves that from the role
    chain); `defender` is whoever is contesting (the matchup defender, or
    a committed helper when `is_committed_helper_contest=True`, per
    `composites.contest_quality`'s own penalty). `assisted`/`passer_id`
    are supplied by the caller, never computed here -- Decision P1-15:
    "assists are structural, never probabilistic," and only `possession.py`
    knows the beat-chain history a structural assist depends on.

    docs/design/block-attribution-v2.md BA-2.1/BA-2.6/BA-2.8 (superseding
    BDA-4's single `rim_help_defender`): `rim_help_pool` is the defenders
    who could rotate to the rim on this shot -- the caller passes the
    defense lineup minus the on-ball defender (BDA-4.4 gate 4, which only
    the caller can see); the shot's own primary `defender` is dropped here
    (gate 3 -- his block is the `blocked_miss` leaf, no double count). An
    empty pool (the default) means no weak-side contest and collapses the
    leaf arithmetic to the pre-BDA-4 shape exactly. `rim_help_committed_id`
    is the possession's out-of-position committed helper (BDA-4.4 gate 5,
    `possession.py`'s `out_of_position_defender_id`) -- a different
    question from `is_committed_helper_contest` (whether *this shot's*
    contest is late): if he is in the pool his scalar is penalized in both
    his share and his contest. When `blocked_miss_help` is drawn, one more
    `weighted_choice` names the blocker (`TerminalOutcome.
    blocking_defender_id`, `actor_ids["blocked_by"]`); no other leaf
    consumes that draw.

    `three_point_bias` (PD-2, build step 7): the offense team's own
    `team_strategy.three_point_bias`, threaded here from `game.py` via
    `possession.py::simulate_possession`, the same threading shape
    `crash_oreb_bias` already uses for `resolve_rebound_beat`. Defaults to
    the tendency-domain neutral midpoint (50, i.e. no scaling) so every
    existing caller that doesn't pass it keeps today's shot chart exactly
    -- same convention `offense_pace_target`'s own default already uses.

    `is_pop_shot` (docs/design/pick-and-pop.md PP-10): `possession.py`
    passes `True` on `POP_OPEN`/`POP_CONTESTED` only. Defaults to `False`
    so every existing caller keeps today's zone/catch-vs-dribble machinery
    exactly -- routes to `_pop_zone_and_shot_inputs` (MID/THREE only, catch
    column only, no `pullup_vs_catch` draw) instead of the normal path.

    `loose_ball_recovery_probability`/`loose_ball_candidates`
    (docs/design/turnover-recovery.md TR-4.1/TR-4.7/TR-4.9): the share of
    this beat's live-ball turnover mass the offense recovers, and the
    offensive players who can recover it. `possession.py` computes the
    probability once per possession (`beats/loose_ball.py`) and passes it
    only while the continuation budget has room -- `0.0` otherwise, so a
    recovery is never drawn when it could not continue the possession
    (TR-4.12). The candidates are the offense's five on the floor; the
    recoverer is drawn from them (`draw_recoverer`, one `rng` draw) only
    when the `loose_ball_recovered` leaf is the one drawn. At `0.0` (the
    default) the `loose_ball_recovered` key is omitted, not inserted at
    zero weight, and no recoverer draw can happen, so every caller that
    does not opt in is byte-identical to before (TR-4.8, TR-7.1). A
    positive probability with no candidates raises: with no type checker in
    this project that guard is the only thing between a threading mistake
    and a nameless player on a broadcast line."""
    if not 0.0 <= loose_ball_recovery_probability <= 1.0:
        raise ValueError(
            "loose_ball_recovery_probability must be in [0, 1], got "
            f"{loose_ball_recovery_probability!r}"
        )
    if loose_ball_recovery_probability > 0.0 and not loose_ball_candidates:
        raise ValueError(
            "loose_ball_recovery_probability is positive but no loose_ball_candidates were given"
        )
    zone, offense_field, block_zone_key = _zone_and_shot_inputs(
        shooter, action_type, tuning, rng, three_point_bias, is_pop_shot=is_pop_shot
    )
    zone_cfg = (
        tuning.resolution_shot.post_shot
        if action_type is ActionType.POST_UP
        else tuning.resolution_shot.zone_tuning[zone.value]
    )

    contest_weights = _select_contest_weights(zone, tuning)  # see module-level comment
    offense_shot_score = rating_value(shooter, offense_field)
    defense_contest_score = contest_quality(
        defender, weights=contest_weights, is_committed_helper=is_committed_helper_contest
    )
    p_make = contest_probability(
        offense_shot_score,
        defense_contest_score,
        base_rate=zone_cfg.base_rate,
        scale=zone_cfg.scale,
        band=zone_cfg.band,
    )

    block_cfg = tuning.resolution_block
    # `contest_probability`'s convention is "the returned probability
    # rises with the first (offense) score, falls with the second
    # (defense) score" -- but a block is a *defense*-favoring outcome
    # (§4.4's Resolution(block) row: the block-defense column names the
    # blocker's own skills). The blocker's score is passed as the first
    # argument and the shooter's as the second so `p_block` rises with
    # the blocker's ability and falls with the shooter's, matching every
    # other beat's "first argument favors the outcome" convention rather
    # than inverting it silently.
    block_shooter_score = ability_score(shooter, block_cfg.offense_weights)
    block_defender_score = contest_quality(
        defender, weights=block_cfg.defense_weights, is_committed_helper=is_committed_helper_contest
    )
    p_block_raw = contest_probability(
        block_defender_score,
        block_shooter_score,
        base_rate=block_cfg.base_rate,
        scale=block_cfg.scale,
        band=block_cfg.band,
    )
    p_block = p_block_raw * tuning.resolution_shot.block_zone_multiplier[block_zone_key]

    # BDA-4.4's gates: a weak-side rim-help contest only applies to a
    # rim-zone shot (gate 1, never mid/three/post -- post is classified
    # MID at `_zone_and_shot_inputs` and never reaches this branch), only
    # over helpers the caller offered (gates 2 and 4) other than the shot's
    # primary contesting defender (gate 3, inside `_rim_help_block_weights`).
    # Offense side reuses `block_shooter_score` verbatim (BDA-4.5):
    # finishing through a help defender and through the primary shot-blocker
    # draw on the same offensive skill set.
    help_block_weights: dict[int, float] = {}
    helpers_by_key: dict[int, SimPlayer] = {}
    if zone is Zone.RIM:
        help_block_weights, helpers_by_key = _rim_help_block_weights(
            rim_help_pool,
            primary_defender_id=defender.id,
            committed_id=rim_help_committed_id,
            shooter_score=block_shooter_score,
            help_aggression=help_aggression,
            tuning=tuning,
        )
    p_help_block = sum(help_block_weights.values())

    foul_cfg = tuning.resolution_foul
    foul_offense_score = ability_score(shooter, foul_cfg.offense_weights)
    foul_defense_score = ability_score(defender, foul_cfg.defense_weights)
    foul_diff_bonus = (
        defender.tendencies.foul_freq - 50.0
    ) * tuning.foul_split.foul_freq_diff_weight
    p_foul = contest_probability(
        foul_offense_score + foul_diff_bonus,
        foul_defense_score,
        base_rate=foul_cfg.base_rate,
        scale=foul_cfg.scale,
        band=foul_cfg.band,
    )

    # Same convention fix as the block contest above: a turnover is a
    # defense-favoring outcome (§4.4's Resolution(turnover) row's
    # "offense" column -- ball_security/pass_under_pressure -- is what
    # the ball-handler uses to *avoid* one), so the defender's score goes
    # first (raises `p_turnover`) and the shooter's ball-security score
    # goes second (lowers it), matching `beats/help.py`'s existing
    # `p_committed_given_attempt` swap for the same reason.
    tov_cfg = tuning.resolution_turnover
    tov_shooter_score = ability_score(shooter, tov_cfg.offense_weights)
    tov_defender_score = ability_score(defender, tov_cfg.defense_weights)
    p_turnover = contest_probability(
        tov_defender_score,
        tov_shooter_score,
        base_rate=tov_cfg.base_rate,
        scale=tov_cfg.scale,
        band=tov_cfg.band,
    )

    p_shot = max(0.0, 1.0 - p_foul - p_turnover)

    # --- turnover leaves ---
    ts_cfg = tuning.turnover_split
    steal_score = ability_score(defender, tuning.steal_attribution)
    live_steal_share = clamp(
        ts_cfg.live_steal_base_share
        + (steal_score - 50.0) * ts_cfg.steal_ability_share_scale
        + (defender.tendencies.steal_gamble_freq - 50.0) * ts_cfg.steal_gamble_share_scale,
        0.0,
        1.0 - ts_cfg.offensive_foul_share,
    )
    turnover_dead_share = max(0.0, 1.0 - live_steal_share - ts_cfg.offensive_foul_share)

    # --- foul leaves ---
    fs_cfg = tuning.foul_split
    non_shooting_share = max(0.0, 1.0 - fs_cfg.shooting_share - fs_cfg.offensive_share)
    p_foul_shot_related = p_foul * fs_cfg.shooting_share

    # TR-4.1: the recovery is carved out of the live-ball flavor only -- the
    # one turnover where the ball is still in play and either team can get
    # it. At probability 0.0 the multiplier is the literal 1.0 (`x * 1.0 ==
    # x` exactly) and the key below is never inserted (TR-4.8).
    live_turnover_mass = p_turnover * live_steal_share
    leaves: dict[str, float] = {
        "turnover_live": live_turnover_mass * (1.0 - loose_ball_recovery_probability),
    }
    if loose_ball_recovery_probability > 0.0:
        leaves["loose_ball_recovered"] = live_turnover_mass * loose_ball_recovery_probability
    leaves |= {
        "turnover_dead": p_turnover * turnover_dead_share,
        "offensive_foul": (
            p_turnover * ts_cfg.offensive_foul_share + p_foul * fs_cfg.offensive_share
        ),
        "and_one": p_foul_shot_related * p_make,
        "shooting_foul": p_foul_shot_related * (1.0 - p_make),
        "non_shooting_foul": p_foul * non_shooting_share,
        "blocked_miss": p_shot * p_block,
        # BDA-4.5's leaf arithmetic: `p_help_block` is 0.0 whenever the
        # gates above leave no helper, which collapses this leaf to 0 and
        # the two below back to exactly their pre-BDA-4 values -- no
        # behavior change for any call site that passes no pool.
        "blocked_miss_help": p_shot * (1.0 - p_block) * p_help_block,
        "made": p_shot * (1.0 - p_block) * (1.0 - p_help_block) * p_make,
        "missed_unblocked": p_shot * (1.0 - p_block) * (1.0 - p_help_block) * (1.0 - p_make),
    }

    leaf = weighted_choice(rng, leaves)
    # TR-4.7 (revised 2026-09-24): the recoverer is drawn here, after the
    # leaf draw and only on the recovery leaf, so every other outcome --
    # and the whole default path -- consumes exactly the draws it did
    # before. One draw per recovery, from this beat's own `rng`.
    loose_ball_recoverer = (
        draw_recoverer(loose_ball_candidates, rng, tuning)
        if leaf == "loose_ball_recovered"
        else None
    )
    # BA-2.1: the same leaf-only shape for the help blocker -- the posterior
    # "who blocked, given a help block" is proportional to each helper's
    # `share * p`, the very terms `p_help_block` summed. The leaf's weight is
    # 0.0 unless `help_block_weights` has a positive entry, so the draw below
    # always has something to pick, and it is one `rng.random()`.
    help_blocker = (
        helpers_by_key[weighted_choice(rng, help_block_weights)]
        if leaf == "blocked_miss_help"
        else None
    )
    outcome = _build_outcome(
        leaf,
        shooter=shooter,
        defender=defender,
        zone=zone,
        assisted=assisted,
        passer_id=passer_id,
        help_blocker=help_blocker,
        loose_ball_recoverer=loose_ball_recoverer,
    )

    actor_ids = {"shooter": shooter.id, "defender": defender.id}
    if leaf == "loose_ball_recovered":
        # TR-4.7: the recoverer is a real participant in this beat, so he
        # is named here too -- which also gives him the (tiny, real)
        # `FatigueTuning.beat_participation_depletion` hit for it. The
        # record stores the drawn id, so a replayed beat log names him
        # without re-drawing.
        assert loose_ball_recoverer is not None  # drawn just above
        actor_ids["recovered_by"] = loose_ball_recoverer.id
    if leaf == "blocked_miss_help":
        # BA-2.8, mirroring "recovered_by": the drawn blocker is named on the
        # record (a replayed beat log names him without re-drawing) and so
        # takes the small per-beat participation depletion for the play.
        assert help_blocker is not None  # drawn just above
        actor_ids["blocked_by"] = help_blocker.id
    record = BeatRecord(
        beat="resolution",
        actor_ids=actor_ids,
        outcome=leaf,
        probabilities=leaves,
        clock_seconds=clock_seconds,
        home_score=home_score,
        away_score=away_score,
    )
    return ResolutionBeatResult(outcome=outcome, probabilities=leaves, record=record)


def _build_outcome(
    leaf: str,
    *,
    shooter: SimPlayer,
    defender: SimPlayer,
    zone: Zone,
    assisted: bool,
    passer_id: int | None,
    help_blocker: SimPlayer | None = None,
    loose_ball_recoverer: SimPlayer | None = None,
) -> TerminalOutcome:
    if leaf == "loose_ball_recovered":
        # TR-4.11: two ids and nothing else. Every scoring-adjacent field
        # (`ftm`, `ft_count`, `fouled_player_id`, `rebounder_id`,
        # `rebound_is_offensive`, ...) stays at its default, and that is the
        # whole mechanism by which this event produces no box-score line.
        # The loser is NOT carried in `turnover_player_id`: this is not a
        # turnover, and a kind-ungated read of that field would ship one.
        assert loose_ball_recoverer is not None  # drawn in resolve_resolution_beat
        return TerminalOutcome(
            kind=TerminalKind.LOOSE_BALL_RECOVERED,
            loose_ball_player_id=shooter.id,
            recovering_player_id=loose_ball_recoverer.id,
        )
    if leaf == "turnover_live":
        return TerminalOutcome(
            kind=TerminalKind.TURNOVER_LIVE,
            steal=True,
            stealing_defender_id=defender.id,
            turnover_player_id=shooter.id,
        )
    if leaf == "turnover_dead":
        return TerminalOutcome(kind=TerminalKind.TURNOVER_DEAD, turnover_player_id=shooter.id)
    if leaf == "offensive_foul":
        return TerminalOutcome(kind=TerminalKind.OFFENSIVE_FOUL, offensive_fouler_id=shooter.id)
    if leaf == "and_one":
        return TerminalOutcome(
            kind=TerminalKind.MADE_FG,
            zone=zone,
            and_one=True,
            assisted=assisted,
            scorer_id=shooter.id,
            passer_id=passer_id,
            ft_count=1,
            fouler_id=defender.id,
            fouled_player_id=shooter.id,
            foul_type="shooting",
        )
    if leaf == "shooting_foul":
        return TerminalOutcome(
            kind=TerminalKind.SHOOTING_FOUL,
            zone=zone,
            ft_count=3 if zone is Zone.THREE else 2,
            fouler_id=defender.id,
            fouled_player_id=shooter.id,
            foul_type="shooting",
        )
    if leaf == "non_shooting_foul":
        return TerminalOutcome(
            kind=TerminalKind.NON_SHOOTING_FOUL,
            ft_count=2,
            fouler_id=defender.id,
            fouled_player_id=shooter.id,
            foul_type="non_shooting",
        )
    if leaf == "blocked_miss":
        return TerminalOutcome(
            kind=TerminalKind.MISSED_FG,
            zone=zone,
            scorer_id=shooter.id,
            blocked=True,
            blocking_defender_id=defender.id,
        )
    if leaf == "blocked_miss_help":
        # The leaf's weight is 0.0 (so `weighted_choice` cannot draw it)
        # unless some helper had a positive `share * p`, and the blocker is
        # drawn from exactly those helpers.
        assert help_blocker is not None, "blocked_miss_help leaf drawn with no help blocker"
        return TerminalOutcome(
            kind=TerminalKind.MISSED_FG,
            zone=zone,
            scorer_id=shooter.id,
            blocked=True,
            blocking_defender_id=help_blocker.id,
        )
    if leaf == "made":
        return TerminalOutcome(
            kind=TerminalKind.MADE_FG,
            zone=zone,
            and_one=False,
            assisted=assisted,
            scorer_id=shooter.id,
            passer_id=passer_id,
        )
    if leaf == "missed_unblocked":
        return TerminalOutcome(
            kind=TerminalKind.MISSED_FG, zone=zone, scorer_id=shooter.id, blocked=False
        )
    raise ValueError(f"unrecognized resolution beat leaf: {leaf}")  # pragma: no cover
