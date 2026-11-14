"""Tier 1: monotonicity for beat 4 (kick-out read, roll tag, pop --
docs/design/pick-and-pop.md)."""
from __future__ import annotations

from dataclasses import replace
from random import Random
from unittest.mock import patch

import pytest

from dba2.sim.beats import rotation as rotation_module
from dba2.sim.beats.rotation import (
    RotationOutcome,
    _passer_tilt_multiplier,
    _require_pop_share_anchors_are_ordered_and_in_unit_range,
    _require_rotation_outcome_branch_map_is_total,
    resolve_rotation_beat,
)
from dba2.sim.tuning import DEFAULT_TUNING, RotationBranchTuning
from tests.fixtures.sim import make_sim_player


def _resolve(**kwargs):
    return resolve_rotation_beat(
        tuning=DEFAULT_TUNING,
        rng=Random(0),
        clock_seconds=18.0,
        home_score=10,
        away_score=8,
        **kwargs,
    )


def test_probabilities_always_sum_to_one_and_stay_in_bounds():
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    passer = make_sim_player(2, tuning=DEFAULT_TUNING)
    closer = make_sim_player(3, tuning=DEFAULT_TUNING)
    screener = make_sim_player(4, tuning=DEFAULT_TUNING)
    tagger = make_sim_player(5, tuning=DEFAULT_TUNING)
    result = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=closer,
        screener=screener,
        tagger=tagger,
    )
    assert abs(sum(result.probabilities.values()) - 1.0) < 1e-9
    for p in result.probabilities.values():
        assert 0.0 <= p <= 1.0


def test_kickout_open_probability_rises_with_elite_offense_vs_poor_closeout():
    elite_vacated = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "off_ball_movement": 95,
            "relocation": 92,
            "cutting": 90,
            "basketball_iq": 88,
        },
    )
    elite_passer = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "passing_vision": 95,
            "passing_accuracy": 92,
            "pass_under_pressure": 90,
        },
    )
    poor_closer = make_sim_player(
        3, tuning=DEFAULT_TUNING, ratings_overrides={"closeout_speed": 10, "closeout_control": 12}
    )
    poor_vacated = make_sim_player(
        4,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "off_ball_movement": 10,
            "relocation": 12,
            "cutting": 8,
            "basketball_iq": 15,
        },
    )
    poor_passer = make_sim_player(
        5,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "passing_vision": 10,
            "passing_accuracy": 12,
            "pass_under_pressure": 8,
        },
    )
    elite_closer = make_sim_player(
        6, tuning=DEFAULT_TUNING, ratings_overrides={"closeout_speed": 95, "closeout_control": 92}
    )

    high = _resolve(
        vacated_shooter=elite_vacated,
        passer=elite_passer,
        closing_defender=poor_closer,
        screener=None,
        tagger=None,
    )
    low = _resolve(
        vacated_shooter=poor_vacated,
        passer=poor_passer,
        closing_defender=elite_closer,
        screener=None,
        tagger=None,
    )
    assert (
        high.probabilities[RotationOutcome.KICKOUT_OPEN.value]
        > low.probabilities[RotationOutcome.KICKOUT_OPEN.value]
    )


def test_roll_live_probability_rises_with_elite_screener_vs_poor_tagger():
    elite_screener = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "roll_ability": 95,
            "catch_hands": 92,
            "short_roll_passing": 90,
            "vertical": 88,
        },
    )
    poor_tagger = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"scheme_discipline": 10, "help_iq": 12, "rotation_speed": 8},
    )
    poor_screener = make_sim_player(
        3,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "roll_ability": 10,
            "catch_hands": 12,
            "short_roll_passing": 8,
            "vertical": 15,
        },
    )
    elite_tagger = make_sim_player(
        4,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"scheme_discipline": 95, "help_iq": 92, "rotation_speed": 90},
    )
    vacated = make_sim_player(5, tuning=DEFAULT_TUNING)
    passer = make_sim_player(6, tuning=DEFAULT_TUNING)

    high = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=poor_tagger,
        screener=elite_screener,
        tagger=poor_tagger,
    )
    low = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=elite_tagger,
        screener=poor_screener,
        tagger=elite_tagger,
    )
    assert (
        high.probabilities[RotationOutcome.ROLL_LIVE.value]
        > low.probabilities[RotationOutcome.ROLL_LIVE.value]
    )


def test_no_roll_available_omits_roll_outcomes():
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    passer = make_sim_player(2, tuning=DEFAULT_TUNING)
    closer = make_sim_player(3, tuning=DEFAULT_TUNING)
    result = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=closer,
        screener=None,
        tagger=None,
    )
    assert RotationOutcome.ROLL_LIVE.value not in result.probabilities
    assert RotationOutcome.ROLL_TAGGED.value not in result.probabilities


# docs/design/per-36-spread-compression.md SC-3.5/SC-7 Commit 3 -- the
# passer-ability tilt on the kickout/roll-vs-no_pass branch draw.


def test_passer_tilt_multiplier_is_exactly_one_at_the_neutral_anchor():
    """SC-8.4's exactness bar (the same one PD-2 set at `tuning.py:943-953`):
    a passer scoring exactly `RotationBranchTuning.neutral_passer_score`
    (70.0, re-centered on the real measured passer_score distribution by
    the per-36 Commit 3 mean-preserving forward-fix) must return a
    multiplier of *exactly* 1.0, not approximately -- `passer_tilt_anchor_
    low`/`_high` are exact-reciprocal-multiplier anchors interpolated in
    log space, which crosses 1.0 at the score midpoint by construction, so
    this is provable, not just measurable."""
    branch_cfg = DEFAULT_TUNING.rotation_branch
    assert branch_cfg.neutral_passer_score == 70.0
    neutral_passer = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"passing_vision": 70, "passing_accuracy": 70},
    )
    assert _passer_tilt_multiplier(neutral_passer, branch_cfg) == 1.0


def test_branch_shares_reproduce_the_pre_tilt_split_exactly_at_neutral():
    """The full-beat version of the exactness bar above: with a neutral
    passer, `resolve_rotation_beat`'s reported branch shares must equal
    today's exact 0.45/0.35/0.20 kickout/roll/no_pass split, to float
    equality -- not just the multiplier in isolation."""
    branch_cfg = DEFAULT_TUNING.rotation_branch
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    passer = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"passing_vision": 70, "passing_accuracy": 70},
    )
    closer = make_sim_player(3, tuning=DEFAULT_TUNING)
    screener = make_sim_player(4, tuning=DEFAULT_TUNING)
    tagger = make_sim_player(5, tuning=DEFAULT_TUNING)
    result = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=closer,
        screener=screener,
        tagger=tagger,
    )
    kickout_share = (
        result.probabilities[RotationOutcome.KICKOUT_OPEN.value]
        + result.probabilities[RotationOutcome.KICKOUT_CONTESTED.value]
    )
    roll_share = (
        result.probabilities[RotationOutcome.ROLL_LIVE.value]
        + result.probabilities[RotationOutcome.ROLL_TAGGED.value]
    )
    no_pass_share = result.probabilities[RotationOutcome.NO_PASS.value]
    assert kickout_share == branch_cfg.kickout_base_weight
    assert roll_share == branch_cfg.roll_base_weight
    assert no_pass_share == branch_cfg.no_pass_base_weight


def test_passer_tilt_multiplier_is_monotonic_in_passer_score():
    """An elite passer must draw a strictly higher multiplier than a
    neutral one, who must draw a strictly higher multiplier than a poor
    one -- SC-3.5's whole point: ability now moves this draw."""
    branch_cfg = DEFAULT_TUNING.rotation_branch
    poor = make_sim_player(
        1, tuning=DEFAULT_TUNING, ratings_overrides={"passing_vision": 25, "passing_accuracy": 25}
    )
    neutral = make_sim_player(
        2, tuning=DEFAULT_TUNING, ratings_overrides={"passing_vision": 70, "passing_accuracy": 70}
    )
    elite = make_sim_player(
        3, tuning=DEFAULT_TUNING, ratings_overrides={"passing_vision": 99, "passing_accuracy": 99}
    )
    poor_mult = _passer_tilt_multiplier(poor, branch_cfg)
    neutral_mult = _passer_tilt_multiplier(neutral, branch_cfg)
    elite_mult = _passer_tilt_multiplier(elite, branch_cfg)
    assert poor_mult < neutral_mult < elite_mult
    assert neutral_mult == 1.0


def test_passer_tilt_multiplier_clamps_outside_the_anchor_range():
    """Below `passer_tilt_anchor_low`'s score or above `passer_tilt_anchor_
    high`'s, the multiplier must clamp flat at that anchor's own value --
    not extrapolate past it -- the same outside-range behaviour
    `SetupTuning`'s own two anchor pairs already use."""
    branch_cfg = DEFAULT_TUNING.rotation_branch
    lo_score, lo_mult = branch_cfg.passer_tilt_anchor_low
    hi_score, hi_mult = branch_cfg.passer_tilt_anchor_high
    below_floor = make_sim_player(
        1, tuning=DEFAULT_TUNING, ratings_overrides={"passing_vision": 25, "passing_accuracy": 25}
    )
    above_ceiling = make_sim_player(
        2, tuning=DEFAULT_TUNING, ratings_overrides={"passing_vision": 99, "passing_accuracy": 99}
    )
    assert 25.0 <= lo_score  # sanity: the fixture score really is <= lo_score
    assert _passer_tilt_multiplier(below_floor, branch_cfg) == lo_mult
    assert hi_score <= 99.0  # sanity: the fixture score really is >= hi_score
    assert _passer_tilt_multiplier(above_ceiling, branch_cfg) == hi_mult


def test_kickout_vs_no_pass_odds_are_unaffected_by_roll_availability():
    """Nit 6: applying the tilt to the base weights before `branch_total`
    is computed means the *odds* of kickout vs. no_pass reduce to
    `(kickout_base_weight / no_pass_base_weight) * tilt**2` regardless of
    whether a roll leg is reachable -- `branch_total` cancels out of a
    ratio between any two of the three tilted base weights REGARDLESS of
    what else is in it (roll present or not). Tilting *after*
    renormalization instead would not have this property, since
    `pnr_ball`'s `branch_total` and `dho`'s differ -- exactly the
    "systematically different strength" failure mode Nit 6 names."""
    branch_cfg = DEFAULT_TUNING.rotation_branch
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    passer = make_sim_player(
        2, tuning=DEFAULT_TUNING, ratings_overrides={"passing_vision": 99, "passing_accuracy": 99}
    )
    closer = make_sim_player(3, tuning=DEFAULT_TUNING)
    screener = make_sim_player(4, tuning=DEFAULT_TUNING)
    tagger = make_sim_player(5, tuning=DEFAULT_TUNING)

    def _kickout_over_no_pass(*, with_roll: bool) -> float:
        result = _resolve(
            vacated_shooter=vacated,
            passer=passer,
            closing_defender=closer,
            screener=screener if with_roll else None,
            tagger=tagger if with_roll else None,
        )
        kickout_share = (
            result.probabilities[RotationOutcome.KICKOUT_OPEN.value]
            + result.probabilities[RotationOutcome.KICKOUT_CONTESTED.value]
        )
        return kickout_share / result.probabilities[RotationOutcome.NO_PASS.value]

    tilt = _passer_tilt_multiplier(passer, branch_cfg)
    expected = (branch_cfg.kickout_base_weight / branch_cfg.no_pass_base_weight) * tilt**2
    assert abs(_kickout_over_no_pass(with_roll=True) - expected) < 1e-9
    assert abs(_kickout_over_no_pass(with_roll=False) - expected) < 1e-9


def test_no_pass_share_shrinks_for_a_better_passer_with_and_without_a_roll_leg():
    """Directional check in both action shapes -- SC-9 risk 2's own
    concern (this moves who shoots, not just who passes) starts here: an
    elite passer's `NO_PASS` share must be lower than a neutral passer's,
    whether or not a roll leg is reachable."""
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    closer = make_sim_player(3, tuning=DEFAULT_TUNING)
    screener = make_sim_player(4, tuning=DEFAULT_TUNING)
    tagger = make_sim_player(5, tuning=DEFAULT_TUNING)
    neutral_passer = make_sim_player(
        2, tuning=DEFAULT_TUNING, ratings_overrides={"passing_vision": 70, "passing_accuracy": 70}
    )
    elite_passer = make_sim_player(
        6, tuning=DEFAULT_TUNING, ratings_overrides={"passing_vision": 99, "passing_accuracy": 99}
    )

    def _no_pass_share(passer, *, with_roll: bool) -> float:
        result = _resolve(
            vacated_shooter=vacated,
            passer=passer,
            closing_defender=closer,
            screener=screener if with_roll else None,
            tagger=tagger if with_roll else None,
        )
        return result.probabilities[RotationOutcome.NO_PASS.value]

    assert _no_pass_share(elite_passer, with_roll=True) < _no_pass_share(
        neutral_passer, with_roll=True
    )
    assert _no_pass_share(elite_passer, with_roll=False) < _no_pass_share(
        neutral_passer, with_roll=False
    )


# docs/design/per-36-spread-compression.md SC-7 Commit 3 forward-fix
# (post-critic-review): the original commit's "mean-preserving by
# construction" claim was false (additive, not odds-space, anchor
# symmetry). These tests lock in the corrected, odds-space-symmetric
# construction and its guards.


def test_rotation_branch_tuning_anchors_are_odds_space_reciprocal():
    """The tilt mechanism is multiplicative (kickout/roll *= tilt, no_pass
    /= tilt), so the condition that actually keeps the two anchors
    mean-preserving at the anchors themselves is `lo_mult * hi_mult == 1.0`
    -- not additive symmetry around 1.0, which is what the original,
    now-corrected commit had (0.75 * 1.25 = 0.9375)."""
    branch_cfg = DEFAULT_TUNING.rotation_branch
    _, lo_mult = branch_cfg.passer_tilt_anchor_low
    _, hi_mult = branch_cfg.passer_tilt_anchor_high
    assert lo_mult * hi_mult == pytest.approx(1.0, abs=1e-9)


def test_rotation_branch_tuning_neutral_score_is_the_anchor_midpoint():
    """Required for the log-linear interpolation in `beats/rotation.py` to
    cross exactly 1.0 at `neutral_passer_score` -- enforced at construction
    by `RotationBranchTuning.__post_init__`, not just true by convention."""
    branch_cfg = DEFAULT_TUNING.rotation_branch
    lo_score, _ = branch_cfg.passer_tilt_anchor_low
    hi_score, _ = branch_cfg.passer_tilt_anchor_high
    assert branch_cfg.neutral_passer_score == pytest.approx((lo_score + hi_score) / 2.0)


def test_rotation_branch_tuning_rejects_non_reciprocal_anchor_multipliers():
    """A future edit that reintroduces additively-symmetric (but not
    odds-space-symmetric) anchors -- exactly the original commit's bug --
    must fail construction, not silently ship."""
    with pytest.raises(ValueError, match="reciprocal"):
        RotationBranchTuning(
            passer_tilt_anchor_low=(55.0, 0.75),
            passer_tilt_anchor_high=(85.0, 1.25),
        )


def test_rotation_branch_tuning_rejects_non_positive_anchor_multiplier():
    """A zero or negative multiplier at either anchor would let
    `beats/rotation.py::resolve_rotation_beat` divide `no_pass_base_weight`
    by zero or a negative number -- rejected at construction, before it can
    ever reach that divide."""
    with pytest.raises(ValueError, match="positive"):
        RotationBranchTuning(
            passer_tilt_anchor_low=(55.0, 0.0),
            passer_tilt_anchor_high=(85.0, 1.15),
        )


def test_rotation_branch_tuning_rejects_neutral_score_off_midpoint():
    """`neutral_passer_score` must equal the anchors' own midpoint, or the
    log-linear interpolation no longer crosses exactly 1.0 there."""
    with pytest.raises(ValueError, match="midpoint"):
        RotationBranchTuning(
            neutral_passer_score=60.0,
            passer_tilt_anchor_low=(55.0, 1.0 / 1.15),
            passer_tilt_anchor_high=(85.0, 1.15),
        )


def test_rotation_branch_tuning_rejects_neutral_score_outside_anchor_range():
    """`neutral_passer_score` must lie strictly between the two anchor
    scores -- a value outside the range can never be the interpolation's
    own midpoint."""
    with pytest.raises(ValueError, match="strictly between"):
        RotationBranchTuning(
            neutral_passer_score=100.0,
            passer_tilt_anchor_low=(55.0, 1.0 / 1.15),
            passer_tilt_anchor_high=(85.0, 1.15),
        )


def test_passer_tilt_multiplier_uses_log_linear_interpolation():
    """Linearly interpolating the raw multiplier between a reciprocal pair
    (e.g. 0.8696 -> 1.15) does not cross 1.0 at the score midpoint --
    interpolating `log(multiplier)` does, by construction. This is the
    property that makes odds-space-symmetric anchors actually reproduce
    the untilted 0.45/0.35/0.20 split at `neutral_passer_score`; a
    regression back to linear-in-multiplier interpolation would silently
    break the exactness guarantee without failing any float-equality-at-
    neutral test alone (that test would still pass, since both schemes
    agree at the midpoint by symmetry -- this test checks a point strictly
    between an anchor and the midpoint, where the two schemes diverge)."""
    branch_cfg = DEFAULT_TUNING.rotation_branch
    lo_score, lo_mult = branch_cfg.passer_tilt_anchor_low
    hi_score, hi_mult = branch_cfg.passer_tilt_anchor_high
    off_center_score = 60  # strictly between lo_score (55) and the midpoint (70)
    off_center_player = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"passing_vision": off_center_score, "passing_accuracy": off_center_score},
    )
    actual = _passer_tilt_multiplier(off_center_player, branch_cfg)
    t = (off_center_score - lo_score) / (hi_score - lo_score)
    linear_prediction = lo_mult + t * (hi_mult - lo_mult)
    assert actual != pytest.approx(linear_prediction, abs=1e-6)


def test_resolve_rotation_beat_raises_a_clear_error_on_non_positive_tilt():
    """Defense-in-depth: even though `RotationBranchTuning.__post_init__`
    already rejects any config that could produce a non-positive tilt,
    `resolve_rotation_beat` guards its own divide
    (`no_pass_base_weight / tilt`) directly, so a bypass of that
    validation (e.g. a test double standing in for `_passer_tilt_
    multiplier`) surfaces as a clear `ValueError`, not a bare
    `ZeroDivisionError`."""
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    passer = make_sim_player(2, tuning=DEFAULT_TUNING)
    closer = make_sim_player(3, tuning=DEFAULT_TUNING)
    with patch.object(rotation_module, "_passer_tilt_multiplier", return_value=0.0):
        with pytest.raises(ValueError, match="positive"):
            _resolve(
                vacated_shooter=vacated,
                passer=passer,
                closing_defender=closer,
                screener=None,
                tagger=None,
            )




# --- docs/design/pick-and-pop.md ------------------------------------------


def test_no_popper_defender_omits_pop_outcomes():
    """PP-9.1/PP-12 criterion 1's own claim, stated directly: a `None`
    `popper_defender` (every existing caller's shape, unchanged by this
    dispatch) never inserts a pop key at all."""
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    passer = make_sim_player(2, tuning=DEFAULT_TUNING)
    closer = make_sim_player(3, tuning=DEFAULT_TUNING)
    screener = make_sim_player(4, tuning=DEFAULT_TUNING)
    tagger = make_sim_player(5, tuning=DEFAULT_TUNING)
    result = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=closer,
        screener=screener,
        tagger=tagger,
    )
    assert RotationOutcome.POP_OPEN.value not in result.probabilities
    assert RotationOutcome.POP_CONTESTED.value not in result.probabilities


def test_pop_available_produces_positive_pop_probability_mass():
    """PP-14 mutation 1, the existence proof: with `popper_defender`
    supplied, an elite-shooting screener's pop mass must be strictly
    positive -- deleting the `if pop_available:` block would zero this out
    silently (no key at all, not merely a zero-weight key)."""
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    passer = make_sim_player(2, tuning=DEFAULT_TUNING)
    closer = make_sim_player(3, tuning=DEFAULT_TUNING)
    elite_shooter = make_sim_player(
        4,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"catch_shoot_3": 90, "catch_shoot_mid": 85},
    )
    tagger = make_sim_player(5, tuning=DEFAULT_TUNING)
    popper_defender = make_sim_player(6, tuning=DEFAULT_TUNING)

    result = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=closer,
        screener=elite_shooter,
        tagger=tagger,
        popper_defender=popper_defender,
    )
    assert RotationOutcome.POP_OPEN.value in result.probabilities
    assert RotationOutcome.POP_CONTESTED.value in result.probabilities
    pop_mass = (
        result.probabilities[RotationOutcome.POP_OPEN.value]
        + result.probabilities[RotationOutcome.POP_CONTESTED.value]
    )
    assert pop_mass > 0.0
    assert abs(sum(result.probabilities.values()) - 1.0) < 1e-9


def _pop_share_of_screener_branch(result) -> float:
    pop = (
        result.probabilities[RotationOutcome.POP_OPEN.value]
        + result.probabilities[RotationOutcome.POP_CONTESTED.value]
    )
    roll = (
        result.probabilities[RotationOutcome.ROLL_LIVE.value]
        + result.probabilities[RotationOutcome.ROLL_TAGGED.value]
    )
    return pop / (pop + roll)


def test_pop_share_of_screener_branch_is_at_least_5x_higher_for_elite_shooter():
    """PP-13 criterion 3 / PP-14 mutation 2: **the feature's core claim.**
    A fixed `pnr_ball`-shaped fixture (screener/tagger/popper_defender all
    present) with only the screener's catch-and-shoot ratings varied must
    show the pop share of the screener branch rise at least 5x from a
    35/40 screener to a 90/85 one -- PP-7's defaults predict ~15x (0.75 vs
    0.05, both anchor-clamped), the 5x floor deliberately leaves room for a
    later calibration narrowing without silently gutting the feature. This
    is computed directly off the closed-form `probabilities` dict (the
    share has no `rng` dependency of its own -- only which leaf the single
    draw lands on does), not sampled, so it is exact rather than noisy."""
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    passer = make_sim_player(2, tuning=DEFAULT_TUNING)
    closer = make_sim_player(3, tuning=DEFAULT_TUNING)
    tagger = make_sim_player(5, tuning=DEFAULT_TUNING)
    popper_defender = make_sim_player(6, tuning=DEFAULT_TUNING)

    elite_shooter = make_sim_player(
        4, tuning=DEFAULT_TUNING, ratings_overrides={"catch_shoot_3": 90, "catch_shoot_mid": 85}
    )
    poor_shooter = make_sim_player(
        4, tuning=DEFAULT_TUNING, ratings_overrides={"catch_shoot_3": 35, "catch_shoot_mid": 40}
    )

    high = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=closer,
        screener=elite_shooter,
        tagger=tagger,
        popper_defender=popper_defender,
    )
    low = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=closer,
        screener=poor_shooter,
        tagger=tagger,
        popper_defender=popper_defender,
    )
    high_share = _pop_share_of_screener_branch(high)
    low_share = _pop_share_of_screener_branch(low)
    assert high_share >= 5.0 * low_share, (high_share, low_share)


def test_pop_share_ignores_roll_ability_only_the_shooting_vector_moves_it():
    """PP-14 mutation 3's mirror direction: swapping `pop_shooting_weights`
    for a roll-ability vector would satisfy the 5x test above via any
    rating that happens to correlate -- this proves the negative: an elite
    **roller** with poor catch-and-shoot ratings gets the SAME (floored)
    pop share as a plain poor shooter, not an elevated one."""
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    passer = make_sim_player(2, tuning=DEFAULT_TUNING)
    closer = make_sim_player(3, tuning=DEFAULT_TUNING)
    tagger = make_sim_player(5, tuning=DEFAULT_TUNING)
    popper_defender = make_sim_player(6, tuning=DEFAULT_TUNING)

    elite_roller_poor_shooter = make_sim_player(
        4,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "roll_ability": 95,
            "catch_hands": 92,
            "short_roll_passing": 90,
            "vertical": 88,
            "catch_shoot_3": 35,
            "catch_shoot_mid": 40,
        },
    )
    plain_poor_shooter = make_sim_player(
        4, tuning=DEFAULT_TUNING, ratings_overrides={"catch_shoot_3": 35, "catch_shoot_mid": 40}
    )

    roller_result = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=closer,
        screener=elite_roller_poor_shooter,
        tagger=tagger,
        popper_defender=popper_defender,
    )
    plain_result = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=closer,
        screener=plain_poor_shooter,
        tagger=tagger,
        popper_defender=popper_defender,
    )
    assert _pop_share_of_screener_branch(roller_result) == pytest.approx(
        _pop_share_of_screener_branch(plain_result)
    )


def test_pop_openness_reads_the_popper_defender_not_the_helper_or_tagger():
    """PP-14 mutation 6: a direct unit test proving the pop contest reads
    `popper_defender`, not `closing_defender` (the beat-3 helper) or
    `tagger`. `closing_defender`/`tagger` are held fixed across both calls;
    only `popper_defender`'s closeout ratings change. If the contest were
    wired to read `closing_defender` instead (the mutation), this would go
    green under the mutation because varying `popper_defender` alone would
    have no effect."""
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    passer = make_sim_player(2, tuning=DEFAULT_TUNING)
    screener = make_sim_player(4, tuning=DEFAULT_TUNING)
    tagger = make_sim_player(5, tuning=DEFAULT_TUNING)
    # Fixed for both calls -- an elite closeout defender, deliberately NOT
    # used as popper_defender in either call, so it can never leak in.
    fixed_closing_defender = make_sim_player(
        3, tuning=DEFAULT_TUNING, ratings_overrides={"closeout_speed": 95, "closeout_control": 92}
    )
    poor_popper_defender = make_sim_player(
        6, tuning=DEFAULT_TUNING, ratings_overrides={"closeout_speed": 10, "closeout_control": 12}
    )
    elite_popper_defender = make_sim_player(
        7, tuning=DEFAULT_TUNING, ratings_overrides={"closeout_speed": 95, "closeout_control": 92}
    )

    open_vs_poor = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=fixed_closing_defender,
        screener=screener,
        tagger=tagger,
        popper_defender=poor_popper_defender,
    )
    open_vs_elite = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=fixed_closing_defender,
        screener=screener,
        tagger=tagger,
        popper_defender=elite_popper_defender,
    )
    assert (
        open_vs_poor.probabilities[RotationOutcome.POP_OPEN.value]
        > open_vs_elite.probabilities[RotationOutcome.POP_OPEN.value]
    )


def test_dho_shape_screener_with_no_tagger_offers_pop_but_not_roll():
    """PP-6.1: `dho`'s `handoff_big` has no roll leg at all -- `screener`
    present, `tagger=None`, `popper_defender` present. ROLL_* must be
    entirely absent (not zero-weighted) and POP_* must be present, with
    the flattened distribution still summing to exactly 1."""
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    passer = make_sim_player(2, tuning=DEFAULT_TUNING)
    closer = make_sim_player(3, tuning=DEFAULT_TUNING)
    screener = make_sim_player(
        4, tuning=DEFAULT_TUNING, ratings_overrides={"catch_shoot_3": 90, "catch_shoot_mid": 85}
    )
    popper_defender = make_sim_player(6, tuning=DEFAULT_TUNING)

    result = _resolve(
        vacated_shooter=vacated,
        passer=passer,
        closing_defender=closer,
        screener=screener,
        tagger=None,
        popper_defender=popper_defender,
    )
    assert RotationOutcome.ROLL_LIVE.value not in result.probabilities
    assert RotationOutcome.ROLL_TAGGED.value not in result.probabilities
    assert RotationOutcome.POP_OPEN.value in result.probabilities
    assert RotationOutcome.POP_CONTESTED.value in result.probabilities
    assert abs(sum(result.probabilities.values()) - 1.0) < 1e-9


def test_pop_outcome_is_labeled_branch_pop_not_no_pass():
    """PP-1b/PP-9.2's own named regression: the chained conditional this
    replaced defaulted its final `else` to `"no_pass"`, which is exactly
    what a bare `elif`-extension would have shipped for every pop. Finds a
    seed that actually draws a pop outcome (an elite, wide-open shooter
    makes this the dominant leaf almost every draw) and asserts the
    resulting `branch` field names it correctly."""
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    passer = make_sim_player(2, tuning=DEFAULT_TUNING)
    closer = make_sim_player(
        3, tuning=DEFAULT_TUNING, ratings_overrides={"closeout_speed": 10, "closeout_control": 10}
    )
    screener = make_sim_player(
        4, tuning=DEFAULT_TUNING, ratings_overrides={"catch_shoot_3": 99, "catch_shoot_mid": 99}
    )
    tagger = make_sim_player(
        5, tuning=DEFAULT_TUNING, ratings_overrides={"scheme_discipline": 10, "help_iq": 10}
    )
    popper_defender = make_sim_player(
        6, tuning=DEFAULT_TUNING, ratings_overrides={"closeout_speed": 10, "closeout_control": 10}
    )

    found_pop = False
    for seed in range(50):
        result = resolve_rotation_beat(
            vacated_shooter=vacated,
            passer=passer,
            closing_defender=closer,
            screener=screener,
            tagger=tagger,
            popper_defender=popper_defender,
            tuning=DEFAULT_TUNING,
            rng=Random(seed),
            clock_seconds=18.0,
            home_score=10,
            away_score=8,
        )
        if result.outcome in (RotationOutcome.POP_OPEN, RotationOutcome.POP_CONTESTED):
            found_pop = True
            assert result.branch == "pop"
    assert found_pop, "no seed in range(50) drew a pop outcome on an overwhelmingly pop fixture"


def test_rotation_outcome_branch_map_pin_raises_on_a_missing_member():
    """PP-9.2's pin, exercised directly: a map missing one real
    `RotationOutcome` member must raise, naming that member."""
    incomplete = {
        member: "kickout" for member in RotationOutcome if member is not RotationOutcome.POP_OPEN
    }
    with pytest.raises(RuntimeError, match="pop_open"):
        _require_rotation_outcome_branch_map_is_total(incomplete)


def test_pop_share_anchor_pin_raises_on_inverted_anchors():
    """PP-9.3's pin: `pop_share_anchor_low`'s score must be strictly less
    than `pop_share_anchor_high`'s."""
    bad = replace(
        DEFAULT_TUNING,
        rotation_branch=replace(
            DEFAULT_TUNING.rotation_branch,
            pop_share_anchor_low=(85.0, 0.05),
            pop_share_anchor_high=(45.0, 0.75),
        ),
    )
    with pytest.raises(RuntimeError, match="strictly less than"):
        _require_pop_share_anchors_are_ordered_and_in_unit_range(bad)


def test_pop_share_anchor_pin_raises_on_a_share_outside_unit_range():
    """PP-9.3's pin, other half: a share above 1.0 would make the roll
    leg's weight negative, which `weighted_choice` clamps to 0.0 rather
    than reporting -- caught here instead, naming the offending value."""
    bad = replace(
        DEFAULT_TUNING,
        rotation_branch=replace(
            DEFAULT_TUNING.rotation_branch, pop_share_anchor_high=(85.0, 1.5)
        ),
    )
    with pytest.raises(RuntimeError, match="1.5"):
        _require_pop_share_anchors_are_ordered_and_in_unit_range(bad)


def test_pop_split_composes_downstream_of_the_passer_tilt():
    """PP-17.2: the pop share splits the already-tilted screener-branch
    weight, so for any passer (including a strongly tilted one) the total
    screener-branch mass with a pop leg available equals the roll-only
    branch's mass, and kickout/no_pass are untouched. If the split were
    applied before the tilt, or the tilt reapplied to only one leg, the
    sums would diverge for a non-neutral passer."""
    vacated = make_sim_player(1, tuning=DEFAULT_TUNING)
    closer = make_sim_player(3, tuning=DEFAULT_TUNING)
    screener = make_sim_player(
        4,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"catch_shoot_3": 80, "catch_shoot_mid": 75},
    )
    tagger = make_sim_player(5, tuning=DEFAULT_TUNING)
    popper_defender = make_sim_player(6, tuning=DEFAULT_TUNING)
    for pv in (40, 70, 95):
        passer = make_sim_player(
            2,
            tuning=DEFAULT_TUNING,
            ratings_overrides={"passing_vision": pv, "passing_accuracy": pv},
        )
        kwargs = dict(
            vacated_shooter=vacated,
            passer=passer,
            closing_defender=closer,
            screener=screener,
            tagger=tagger,
        )
        roll_only = _resolve(**kwargs).probabilities
        both = _resolve(popper_defender=popper_defender, **kwargs).probabilities

        def screener_mass(p):
            return sum(
                p.get(o.value, 0.0)
                for o in (
                    RotationOutcome.ROLL_LIVE,
                    RotationOutcome.ROLL_TAGGED,
                    RotationOutcome.POP_OPEN,
                    RotationOutcome.POP_CONTESTED,
                )
            )

        assert screener_mass(both) == pytest.approx(screener_mass(roll_only))
        for o in (
            RotationOutcome.KICKOUT_OPEN,
            RotationOutcome.KICKOUT_CONTESTED,
            RotationOutcome.NO_PASS,
        ):
            assert both[o.value] == pytest.approx(roll_only[o.value])
        assert sum(both.values()) == pytest.approx(1.0)
