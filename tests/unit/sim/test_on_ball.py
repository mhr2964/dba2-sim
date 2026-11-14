"""Tier 1: monotonicity for the on-ball beat, plus the screen-result/
spacing/fatigue Modifiers each shifting the diff in the expected direction."""
from __future__ import annotations

from random import Random

from dba2.sim.beats.on_ball import OnBallOutcome, resolve_on_ball_beat
from dba2.sim.beats.screen import ScreenOutcome
from dba2.sim.composites import spacing_aggregate
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_sim_player


def _resolve(ball_handler, defender, **kwargs):
    return resolve_on_ball_beat(
        ball_handler,
        defender,
        tuning=DEFAULT_TUNING,
        rng=Random(0),
        clock_seconds=18.0,
        home_score=10,
        away_score=8,
        **kwargs,
    )


def test_advantage_probability_is_monotonic_in_the_handler_defender_gap():
    elite_handler = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "handle": 95,
            "first_step": 92,
            "change_of_direction": 90,
            "ball_security": 88,
            "screen_use": 85,
        },
    )
    poor_defender = make_sim_player(
        2,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "on_ball_defense": 10,
            "lateral_quickness": 12,
            "hands": 8,
            "strength": 15,
            "foul_discipline": 20,
        },
    )
    poor_handler = make_sim_player(
        3,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "handle": 10,
            "first_step": 12,
            "change_of_direction": 8,
            "ball_security": 15,
            "screen_use": 20,
        },
    )
    elite_defender = make_sim_player(
        4,
        tuning=DEFAULT_TUNING,
        ratings_overrides={
            "on_ball_defense": 95,
            "lateral_quickness": 90,
            "hands": 92,
            "strength": 88,
            "foul_discipline": 85,
        },
    )

    high = _resolve(elite_handler, poor_defender)
    low = _resolve(poor_handler, elite_defender)
    assert (
        high.probabilities[OnBallOutcome.ADVANTAGE.value]
        > low.probabilities[OnBallOutcome.ADVANTAGE.value]
    )


def test_probabilities_always_sum_to_one_and_stay_in_bounds():
    handler = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    result = _resolve(handler, defender)
    assert abs(sum(result.probabilities.values()) - 1.0) < 1e-9
    for p in result.probabilities.values():
        assert 0.0 <= p <= 1.0


def test_a_blown_screen_raises_advantage_probability_relative_to_no_screen():
    handler = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    no_screen = _resolve(handler, defender)
    blown = _resolve(handler, defender, screen_outcome=ScreenOutcome.BLOWN)
    assert (
        blown.probabilities[OnBallOutcome.ADVANTAGE.value]
        > no_screen.probabilities[OnBallOutcome.ADVANTAGE.value]
    )


def test_a_well_navigated_under_lowers_advantage_probability_relative_to_no_screen():
    handler = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    no_screen = _resolve(handler, defender)
    under = _resolve(handler, defender, screen_outcome=ScreenOutcome.UNDER)
    assert (
        under.probabilities[OnBallOutcome.ADVANTAGE.value]
        < no_screen.probabilities[OnBallOutcome.ADVANTAGE.value]
    )


def test_higher_spacing_aggregate_raises_advantage_probability():
    """B1 regression: the fixture used to compare an unrealistic literal
    `0.0` against `90.0` fed straight into the beat's `spacing_aggregate`
    kwarg -- values `composites.spacing_aggregate()` can never actually
    produce (it's centered onto ~[-1, 1], not the raw 0-99 rating scale),
    which is exactly why the pre-fix scale-mismatch bug went undetected:
    the test never exercised the real composites->beat pipeline. This
    builds two genuinely realistic non-role-offense lineups (bad spacing
    ~30, elite spacing ~75 on the `catch_shoot_3`/`relocation` scale that
    feeds `spacing_contribution`), runs them through the real
    `composites.spacing_aggregate()`, and feeds *that* output into the
    beat -- proving the fix holds through the actual call path, not just
    the beat's own arithmetic."""
    handler = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)

    bad_spacing_lineup = [
        make_sim_player(
            10 + i,
            tuning=DEFAULT_TUNING,
            ratings_overrides={"catch_shoot_3": 30, "relocation": 30},
        )
        for i in range(3)
    ]
    elite_spacing_lineup = [
        make_sim_player(
            20 + i,
            tuning=DEFAULT_TUNING,
            ratings_overrides={"catch_shoot_3": 75, "relocation": 75},
        )
        for i in range(3)
    ]

    tight = _resolve(
        handler, defender, spacing_aggregate=spacing_aggregate(bad_spacing_lineup)
    )
    spaced = _resolve(
        handler, defender, spacing_aggregate=spacing_aggregate(elite_spacing_lineup)
    )
    assert (
        spaced.probabilities[OnBallOutcome.ADVANTAGE.value]
        > tight.probabilities[OnBallOutcome.ADVANTAGE.value]
    )


def test_offense_fatigue_lowers_advantage_probability_relative_to_defense_fatigue():
    handler = make_sim_player(1, tuning=DEFAULT_TUNING)
    defender = make_sim_player(2, tuning=DEFAULT_TUNING)
    fresh = _resolve(handler, defender, offense_fatigue=0.0, defense_fatigue=0.0)
    tired_offense = _resolve(handler, defender, offense_fatigue=1.0, defense_fatigue=0.0)
    tired_defense = _resolve(handler, defender, offense_fatigue=0.0, defense_fatigue=1.0)
    assert (
        tired_offense.probabilities[OnBallOutcome.ADVANTAGE.value]
        < fresh.probabilities[OnBallOutcome.ADVANTAGE.value]
        < tired_defense.probabilities[OnBallOutcome.ADVANTAGE.value]
    )
