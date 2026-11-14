"""Tier 1: monotonicity for the rebound beat -- §12 step 4's own wording,
"an elite offensive_rebounding player beats a replacement-level one at
the rebound draw more often, not 'yields exactly 0.63'.\"

The four tests at the bottom of this file (`test_*_attribution_exponent*`,
`test_*_ordering*`) are docs/design/per-36-spread-compression.md SC-8.4's
own required set for Commit 2's `attribution_concentration_exponent`:
the ordering invariant, the all-equal-scores case, the strictly-
positive-weights-at-the-rating-floor case, and the exponent-vs-modifier
ordering."""
from __future__ import annotations

from random import Random

from dba2.sim.beats.rebound import resolve_rebound_beat
from dba2.sim.probability import ability_score
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_sim_player


def _lineup(base_id, *, height_in=78, **ratings_overrides):
    return tuple(
        make_sim_player(
            base_id + i,
            tuning=DEFAULT_TUNING,
            height_in=height_in,
            ratings_overrides=ratings_overrides or None,
        )
        for i in range(5)
    )


def test_probabilities_sum_to_one_and_stay_in_bounds():
    offense = _lineup(1)
    defense = _lineup(101)
    result = resolve_rebound_beat(
        offense,
        defense,
        crash_oreb_bias=50,
        tuning=DEFAULT_TUNING,
        rng=Random(0),
        clock_seconds=2.0,
        home_score=10,
        away_score=8,
    )
    assert abs(sum(result.probabilities.values()) - 1.0) < 1e-9
    for p in result.probabilities.values():
        assert 0.0 <= p <= 1.0
    assert result.rebounder_id in {p.id for p in (offense + defense)}


def test_elite_offensive_rebounding_team_wins_offensive_boards_more_often():
    elite_offense = _lineup(1, height_in=90, offensive_rebounding=95, motor=92)
    poor_offense = _lineup(1, height_in=68, offensive_rebounding=8, motor=10)
    defense = _lineup(101)

    def _p_offensive(offense):
        result = resolve_rebound_beat(
            offense,
            defense,
            crash_oreb_bias=50,
            tuning=DEFAULT_TUNING,
            rng=Random(0),
            clock_seconds=2.0,
            home_score=0,
            away_score=0,
        )
        return result.probabilities["offense"]

    assert _p_offensive(elite_offense) > _p_offensive(poor_offense)


def test_higher_crash_oreb_bias_raises_offensive_rebound_probability():
    offense = _lineup(1)
    defense = _lineup(101)

    def _p_offensive(bias):
        result = resolve_rebound_beat(
            offense,
            defense,
            crash_oreb_bias=bias,
            tuning=DEFAULT_TUNING,
            rng=Random(0),
            clock_seconds=2.0,
            home_score=0,
            away_score=0,
        )
        return result.probabilities["offense"]

    assert _p_offensive(95) > _p_offensive(5)


def test_out_of_position_defender_lowers_defensive_rebound_attribution_weight():
    offense = _lineup(1)
    defense = _lineup(101, defensive_rebounding=60)
    target = defense[0]

    def _defense_attribution_share(out_of_position_id):
        wins = 0
        total = 0
        for seed in range(400):
            result = resolve_rebound_beat(
                offense,
                defense,
                crash_oreb_bias=50,
                tuning=DEFAULT_TUNING,
                rng=Random(seed),
                clock_seconds=2.0,
                home_score=0,
                away_score=0,
                out_of_position_defender_id=out_of_position_id,
            )
            if not result.is_offensive:
                total += 1
                if result.rebounder_id == target.id:
                    wins += 1
        return wins / total if total else 0.0

    share_penalized = _defense_attribution_share(target.id)
    share_neutral = _defense_attribution_share(None)
    assert share_penalized < share_neutral


# ---------------------------------------------------------------------------
# docs/design/per-36-spread-compression.md SC-8.4's required set for
# Commit 2's `attribution_concentration_exponent`.
# ---------------------------------------------------------------------------


def test_higher_score_never_produces_a_smaller_attribution_weight():
    """The ordering invariant: raising a positive score to
    `attribution_concentration_exponent` preserves order -- an elite
    defensive rebounder still outweighs a poor one after exponentiation,
    same as before it (a concentration exponent sharpens the draw, it
    does not reorder the candidates)."""
    tuning = DEFAULT_TUNING
    mod = tuning.rebound_modifiers
    cfg = tuning.rebound
    poor = make_sim_player(
        1,
        tuning=tuning,
        height_in=70,
        ratings_overrides={"defensive_rebounding": 30, "strength": 30},
    )
    elite = make_sim_player(
        2,
        tuning=tuning,
        height_in=86,
        ratings_overrides={"defensive_rebounding": 95, "strength": 90},
    )

    poor_score = ability_score(poor, cfg.defense_weights)
    elite_score = ability_score(elite, cfg.defense_weights)
    assert elite_score > poor_score

    poor_weight = poor_score**mod.attribution_concentration_exponent
    elite_weight = elite_score**mod.attribution_concentration_exponent
    assert elite_weight > poor_weight


def test_equal_scores_stay_uniform_after_the_exponent_and_resolve_does_not_crash():
    """The all-equal-scores case: five identically-rated candidates still
    produce five equal weights after exponentiation (no artificial
    tie-break skew), and `resolve_rebound_beat` still terminates with a
    valid winner rather than crashing or hitting `weighted_choice`'s
    degenerate fallback."""
    tuning = DEFAULT_TUNING
    mod = tuning.rebound_modifiers
    cfg = tuning.rebound
    offense = _lineup(1)
    defense = _lineup(101, defensive_rebounding=55, strength=55)

    weights = {
        p.id: ability_score(p, cfg.defense_weights) ** mod.attribution_concentration_exponent
        for p in defense
    }
    assert len({round(w, 9) for w in weights.values()}) == 1

    result = resolve_rebound_beat(
        offense,
        defense,
        crash_oreb_bias=50,
        tuning=tuning,
        rng=Random(0),
        clock_seconds=2.0,
        home_score=0,
        away_score=0,
    )
    assert result.rebounder_id in {p.id for p in (offense + defense)}


def test_scores_stay_strictly_positive_at_the_rating_floor():
    """Every `ability_score` at this call site is a weighted mean of
    `[25, 99]`-clamped rating fields and a `normalize_physical`-derived
    `height_in` term (both strictly positive, per `derivation.py`'s clamp
    and the physical anchor table's own >=1.0 floor), so `score ** gamma`
    is strictly positive even at the rating floor and `weighted_choice`'s
    `rng.choice` fallback -- named elsewhere in this file's tuning
    comments as an RNG-desync hazard -- is never reached here."""
    tuning = DEFAULT_TUNING
    mod = tuning.rebound_modifiers
    cfg = tuning.rebound
    floor_lineup = _lineup(
        1,
        height_in=60,
        defensive_rebounding=25,
        strength=25,
        offensive_rebounding=25,
        motor=25,
    )
    for p in floor_lineup:
        offense_score = ability_score(p, cfg.offense_weights)
        defense_score = ability_score(p, cfg.defense_weights)
        assert offense_score > 0
        assert defense_score > 0
        assert offense_score**mod.attribution_concentration_exponent > 0
        assert defense_score**mod.attribution_concentration_exponent > 0

    result = resolve_rebound_beat(
        floor_lineup,
        floor_lineup,
        crash_oreb_bias=50,
        tuning=tuning,
        rng=Random(0),
        clock_seconds=2.0,
        home_score=0,
        away_score=0,
    )
    assert result.rebounder_id in {p.id for p in floor_lineup}


def test_out_of_position_penalty_multiplies_the_exponentiated_weight_not_the_raw_score():
    """The exponent-vs-modifier ordering invariant (SC-7 Commit 2 step 2):
    `out_of_position_defender_penalty` must multiply the ALREADY-
    exponentiated weight (`(score ** gamma) * penalty`), not fold inside
    the exponent (`(score * penalty) ** gamma`) -- the two formulas
    predict measurably different realized shares for this fixture
    (~0.149 vs ~0.094), distinguishable well within noise at n=6000
    draws. A regression to the folded (wrong) ordering would show up here
    as a measured share close to the wrong prediction instead of the
    right one."""
    tuning = DEFAULT_TUNING
    mod = tuning.rebound_modifiers
    gamma = mod.attribution_concentration_exponent
    penalty = mod.out_of_position_defender_penalty

    offense = _lineup(1)
    defense = _lineup(101, defensive_rebounding=60, strength=60)  # identical across all 5
    target = defense[0]

    defensive_draws = 0
    target_wins = 0
    for seed in range(6000):
        result = resolve_rebound_beat(
            offense,
            defense,
            crash_oreb_bias=50,
            tuning=tuning,
            rng=Random(seed),
            clock_seconds=2.0,
            home_score=0,
            away_score=0,
            out_of_position_defender_id=target.id,
        )
        if not result.is_offensive:
            defensive_draws += 1
            if result.rebounder_id == target.id:
                target_wins += 1

    measured_share = target_wins / defensive_draws
    correct_share = penalty / (penalty + 4)
    folded_share = (penalty**gamma) / (penalty**gamma + 4)

    assert abs(measured_share - correct_share) < 0.03, (
        f"measured {measured_share:.4f} vs correct-ordering prediction {correct_share:.4f}"
    )
    assert abs(measured_share - folded_share) > 0.03, (
        f"measured {measured_share:.4f} matches the folded (wrong-ordering) prediction "
        f"{folded_share:.4f} -- the exponent is being applied after multiplying in the "
        "penalty, not before"
    )
