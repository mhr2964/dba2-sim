"""Tier 1 (phase-1-simulation-engine.md §9): the probability helpers are
tested for monotonicity, boundedness, and clamping -- "never exact
probabilities," per §9's own decision, since an exact-value test would
have to be rewritten on every tuning sweep."""
from __future__ import annotations

from random import Random

import pytest

from dba2.sim.probability import (
    ability_score,
    clamp,
    contest_probability,
    logit,
    rating_value,
    sigmoid,
    weighted_choice,
)
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_sim_player


def test_sigmoid_and_logit_are_inverses_over_the_open_interval():
    for p in (0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99):
        assert sigmoid(logit(p)) == pytest.approx(p, abs=1e-9)


def test_sigmoid_never_escapes_zero_one_even_for_extreme_inputs():
    # -10_000 underflows to exactly 0.0 in float arithmetic, which is the
    # mathematically correct limit, not a bug -- the property under test
    # is "never raises OverflowError and never exceeds the [0, 1] bound,"
    # not "never reaches the bound."
    assert 0.0 <= sigmoid(-10_000.0) < 1e-9
    assert 1.0 - 1e-9 < sigmoid(10_000.0) <= 1.0


def test_clamp_rejects_an_inverted_band():
    with pytest.raises(ValueError):
        clamp(0.5, hi=0.1, lo=0.9)


def test_contest_probability_is_monotonic_increasing_in_offense_score():
    low = contest_probability(10.0, 50.0, base_rate=0.5, scale=40.0, band=(0.0, 1.0))
    mid = contest_probability(50.0, 50.0, base_rate=0.5, scale=40.0, band=(0.0, 1.0))
    high = contest_probability(90.0, 50.0, base_rate=0.5, scale=40.0, band=(0.0, 1.0))
    assert low < mid < high


def test_contest_probability_is_monotonic_decreasing_in_defense_score():
    low = contest_probability(50.0, 90.0, base_rate=0.5, scale=40.0, band=(0.0, 1.0))
    mid = contest_probability(50.0, 50.0, base_rate=0.5, scale=40.0, band=(0.0, 1.0))
    high = contest_probability(50.0, 10.0, base_rate=0.5, scale=40.0, band=(0.0, 1.0))
    assert low < mid < high


def test_contest_probability_is_clamped_to_its_declared_band_even_at_extremes():
    p_low_end = contest_probability(-1000.0, 1000.0, base_rate=0.5, scale=10.0, band=(0.1, 0.9))
    p_high_end = contest_probability(1000.0, -1000.0, base_rate=0.5, scale=10.0, band=(0.1, 0.9))
    assert p_low_end == pytest.approx(0.1)
    assert p_high_end == pytest.approx(0.9)


def test_contest_probability_equals_base_rate_at_zero_diff():
    p = contest_probability(50.0, 50.0, base_rate=0.37, scale=40.0, band=(0.0, 1.0))
    assert p == pytest.approx(0.37, abs=1e-9)


def test_contest_probability_rejects_nonpositive_scale():
    with pytest.raises(ValueError):
        contest_probability(50.0, 50.0, base_rate=0.5, scale=0.0, band=(0.0, 1.0))


def test_rating_value_reads_a_plain_ratings_field():
    player = make_sim_player(1, tuning=DEFAULT_TUNING, ratings_overrides={"handle": 77})
    assert rating_value(player, "handle") == 77.0


def test_rating_value_dispatches_wingspan_and_height_to_the_normalized_cache():
    player = make_sim_player(2, tuning=DEFAULT_TUNING, wingspan_in=84, height_in=81)
    assert rating_value(player, "wingspan_in") == player.wingspan_normalized
    assert rating_value(player, "height_in") == player.height_normalized
    # Sanity: these are genuinely different values, not both falling back
    # to some shared default -- a distinct-fixture check for this
    # dispatch's own mechanism, in the spirit of this project's fixture
    # discipline.
    assert player.wingspan_normalized != player.height_normalized


def test_ability_score_is_a_normalized_weighted_mean():
    player = make_sim_player(
        3, tuning=DEFAULT_TUNING, ratings_overrides={"handle": 80, "first_step": 40}
    )
    score = ability_score(player, {"handle": 3.0, "first_step": 1.0})
    assert score == pytest.approx((80 * 3 + 40 * 1) / 4)


def test_ability_score_rejects_a_nonpositive_weight_sum():
    player = make_sim_player(4, tuning=DEFAULT_TUNING)
    with pytest.raises(ValueError):
        ability_score(player, {"handle": 0.0, "first_step": 0.0})


def test_weighted_choice_is_deterministic_given_the_same_rng_state():
    weights = {"a": 1.0, "b": 2.0, "c": 3.0}
    first = weighted_choice(Random(42), weights)
    second = weighted_choice(Random(42), weights)
    assert first == second


def test_weighted_choice_never_picks_a_zero_weight_key_over_many_draws():
    weights = {"never": 0.0, "always": 1.0}
    rng = Random(7)
    results = {weighted_choice(rng, weights) for _ in range(200)}
    assert results == {"always"}


def test_weighted_choice_falls_back_to_uniform_when_every_weight_is_nonpositive():
    weights = {"a": 0.0, "b": 0.0}
    rng = Random(1)
    results = {weighted_choice(rng, weights) for _ in range(200)}
    assert results == {"a", "b"}
