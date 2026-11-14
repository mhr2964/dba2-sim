"""Tier 1, §12 step 3's verify bullets for the screen beat: monotonicity
at extreme fixtures, and scheme-dependent outcome shares (a `switch_all`
team produces switches, a `drop` team produces unders) on the *same*
underlying contest probability."""
from __future__ import annotations

from random import Random

from dba2.domain.team import DefensiveScheme
from dba2.sim.beats.screen import ScreenOutcome, apply_switch, resolve_screen_beat
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_sim_player

_MID_DEFENDER_RATINGS = {
    "screen_navigation": 50,
    "lateral_quickness": 45,
    "switchability": 40,
    "scheme_discipline": 60,
}


def _screener(rating_overrides):
    return make_sim_player(1, tuning=DEFAULT_TUNING, ratings_overrides=rating_overrides)


def _defender(rating_overrides):
    return make_sim_player(2, tuning=DEFAULT_TUNING, ratings_overrides=rating_overrides)


def _resolve(screener, defender, scheme=DefensiveScheme.DROP):
    return resolve_screen_beat(
        screener,
        defender,
        defensive_scheme=scheme,
        tuning=DEFAULT_TUNING,
        rng=Random(0),
        clock_seconds=18.0,
        home_score=10,
        away_score=8,
    )


def test_blown_probability_is_monotonic_in_the_screener_defender_gap():
    elite_screener = _screener({"screen_setting": 95, "screen_timing": 92, "strength": 90})
    poor_defender = _defender(
        {
            "screen_navigation": 10,
            "lateral_quickness": 12,
            "switchability": 8,
            "scheme_discipline": 15,
        }
    )
    poor_screener = _screener({"screen_setting": 10, "screen_timing": 12, "strength": 8})
    elite_defender = _defender(
        {
            "screen_navigation": 95,
            "lateral_quickness": 90,
            "switchability": 92,
            "scheme_discipline": 96,
        }
    )

    high = _resolve(elite_screener, poor_defender)
    low = _resolve(poor_screener, elite_defender)
    high_blown = high.probabilities[ScreenOutcome.BLOWN.value]
    low_blown = low.probabilities[ScreenOutcome.BLOWN.value]
    assert high_blown > low_blown


def test_probabilities_always_sum_to_one_and_stay_in_bounds():
    screener = _screener({"screen_setting": 60, "screen_timing": 55, "strength": 70})
    defender = _defender(_MID_DEFENDER_RATINGS)
    for scheme in DefensiveScheme:
        result = _resolve(screener, defender, scheme)
        assert abs(sum(result.probabilities.values()) - 1.0) < 1e-9
        for p in result.probabilities.values():
            assert 0.0 <= p <= 1.0


def test_switch_all_scheme_produces_more_switches_than_drop_scheme():
    screener = _screener({"screen_setting": 60, "screen_timing": 55, "strength": 70})
    defender = _defender(_MID_DEFENDER_RATINGS)
    switch_all = _resolve(screener, defender, DefensiveScheme.SWITCH_ALL)
    drop = _resolve(screener, defender, DefensiveScheme.DROP)
    switch_all_p = switch_all.probabilities[ScreenOutcome.SWITCH.value]
    drop_p = drop.probabilities[ScreenOutcome.SWITCH.value]
    assert switch_all_p > drop_p


def test_drop_scheme_produces_more_unders_than_switch_all_scheme():
    screener = _screener({"screen_setting": 60, "screen_timing": 55, "strength": 70})
    defender = _defender(_MID_DEFENDER_RATINGS)
    drop = _resolve(screener, defender, DefensiveScheme.DROP)
    switch_all = _resolve(screener, defender, DefensiveScheme.SWITCH_ALL)
    drop_p = drop.probabilities[ScreenOutcome.UNDER.value]
    switch_all_p = switch_all.probabilities[ScreenOutcome.UNDER.value]
    assert drop_p > switch_all_p


def test_apply_switch_swaps_the_two_involved_defenders_and_leaves_others_untouched():
    matchups = {1: 11, 2: 12, 3: 13}
    updated = apply_switch(
        matchups, ball_handler_id=1, on_ball_defender_id=11, screener_id=2, screen_defender_id=12
    )
    assert updated == {1: 12, 2: 11, 3: 13}
    # Original mapping must not be mutated in place.
    assert matchups == {1: 11, 2: 12, 3: 13}
