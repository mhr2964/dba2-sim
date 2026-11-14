"""Tier 1 for the loose-ball recovery contest (docs/design/turnover-recovery.md
TR-4.4/TR-4.7): the per-player cache, the recovery probability's direction
and band (a pure function, no RNG), and the weighted recoverer draw (TR-4.7
as revised 2026-09-24: one `rng.random()` per call, weights proportional to
`loose_ball_contribution`)."""
from __future__ import annotations

import dataclasses
from collections import Counter
from random import Random

import pytest

from dba2.sim.beats.loose_ball import draw_recoverer, recovery_probability
from dba2.sim.composites import loose_ball_contribution
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_sim_player

_HUSTLE = {"motor": 95, "hands": 95, "basketball_iq": 95}
_PASSIVE = {"motor": 30, "hands": 30, "basketball_iq": 30}
_EVEN = {"motor": 60, "hands": 60, "basketball_iq": 60}


def _lineup(base_id: int, overrides: dict[str, int] | None = None) -> tuple:
    return tuple(
        make_sim_player(base_id + i, tuning=DEFAULT_TUNING, ratings_overrides=overrides)
        for i in range(5)
    )


def test_the_cache_is_the_weighted_mean_of_motor_hands_and_basketball_iq():
    player = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        ratings_overrides={"motor": 80, "hands": 60, "basketball_iq": 40},
    )
    expected = 0.40 * 80 + 0.35 * 60 + 0.25 * 40
    assert player.loose_ball_contribution == pytest.approx(expected)
    assert loose_ball_contribution(player.ratings, DEFAULT_TUNING) == player.loose_ball_contribution


def test_equal_lineups_recover_at_the_base_rate():
    """`base_rate` is directly readable as the league-average offensive
    share because both sides use one symmetric vector (TR-4.4)."""
    p = recovery_probability(_lineup(1, _EVEN), _lineup(101, _EVEN), DEFAULT_TUNING)
    assert p == pytest.approx(DEFAULT_TUNING.loose_ball.base_rate)


def test_a_hustle_offense_recovers_more_and_a_hustle_defense_less_within_the_band():
    lo, hi = DEFAULT_TUNING.loose_ball.band
    neutral = recovery_probability(_lineup(1, _EVEN), _lineup(101, _EVEN), DEFAULT_TUNING)
    hustle_offense = recovery_probability(_lineup(1, _HUSTLE), _lineup(101, _EVEN), DEFAULT_TUNING)
    hustle_defense = recovery_probability(_lineup(1, _EVEN), _lineup(101, _HUSTLE), DEFAULT_TUNING)
    assert hustle_defense < neutral < hustle_offense
    extreme = recovery_probability(_lineup(1, _HUSTLE), _lineup(101, _PASSIVE), DEFAULT_TUNING)
    floor = recovery_probability(_lineup(1, _PASSIVE), _lineup(101, _HUSTLE), DEFAULT_TUNING)
    assert lo <= floor < extreme <= hi


# Five distinct loose-ball scores (uniform ratings, so the cache equals the
# rating): the best is 3x the worst, a wider spread than a real lineup's.
_SPREAD = (95, 80, 60, 45, 30)


def _spread_lineup() -> tuple:
    return tuple(
        make_sim_player(
            i + 1,
            tuning=DEFAULT_TUNING,
            ratings_overrides={"motor": r, "hands": r, "basketball_iq": r},
        )
        for i, r in enumerate(_SPREAD)
    )


def _one_random_later(seed: int) -> Random:
    twin = Random(seed)
    twin.random()
    return twin


def _tuning_with_exponent(exponent: float):
    return dataclasses.replace(
        DEFAULT_TUNING,
        loose_ball=dataclasses.replace(
            DEFAULT_TUNING.loose_ball, recovery_concentration_exponent=exponent
        ),
    )


def _chi_square_and_counts(exponent: float, n: int = 20_000):
    lineup = _spread_lineup()
    tuning = _tuning_with_exponent(exponent)
    rng = Random(20260924)
    counts = Counter(draw_recoverer(lineup, rng, tuning).id for _ in range(n))
    weights = {p.id: p.loose_ball_contribution**exponent for p in lineup}
    total = sum(weights.values())
    chi_square = sum(
        (counts[pid] - n * w / total) ** 2 / (n * w / total) for pid, w in weights.items()
    )
    return chi_square, counts


def test_the_default_exponent_is_2_4():
    assert DEFAULT_TUNING.loose_ball.recovery_concentration_exponent == 2.4


def test_the_recoverer_draw_follows_score_to_the_default_exponent():
    """R12: a chi-square goodness-of-fit over 20,000 draws against weights
    `score ** 2.4`. Mutations caught: the argmax (top share 1.0), `min`, the
    exponent dropped (1.0 weights put the statistic in the thousands), and
    uniform weights. The seed is fixed, so this is deterministic; 18.47 is
    the 0.001 critical value at 4 degrees of freedom."""
    chi_square, counts = _chi_square_and_counts(
        DEFAULT_TUNING.loose_ball.recovery_concentration_exponent
    )
    assert chi_square < 18.47, (chi_square, counts)
    assert [pid for pid, _ in counts.most_common()] == [1, 2, 3, 4, 5]
    assert counts[5] > 0, "the worst loose-ball player never recovers"


def test_exponent_one_reproduces_the_plain_proportional_draw():
    """Exponent 1.0 is the c3efb8b behaviour: weights exactly proportional to
    the cached score."""
    chi_square, counts = _chi_square_and_counts(1.0)
    assert chi_square < 18.47, (chi_square, counts)
    assert 0.25 < counts[1] / sum(counts.values()) < 0.5


def test_a_larger_exponent_concentrates_recoveries_on_the_best_player():
    shares = []
    for exponent in (1.0, 2.4, 4.0, 8.0):
        _, counts = _chi_square_and_counts(exponent, n=5_000)
        shares.append(counts[1] / 5_000)
    assert shares == sorted(shares) and len(set(shares)) == 4, shares


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
def test_a_non_positive_exponent_is_rejected_at_construction(bad):
    with pytest.raises(ValueError, match="recovery_concentration_exponent"):
        dataclasses.replace(DEFAULT_TUNING.loose_ball, recovery_concentration_exponent=bad)


def test_the_recoverer_draw_is_deterministic_and_consumes_exactly_one_random():
    """Same seed, same recoverer; and the stream advances by exactly one
    `rng.random()` -- the property that keeps every later draw in the
    possession aligned (the `weighted_choice` fast path, never `rng.choice`)."""
    lineup = _spread_lineup()
    for seed in range(300):
        first, second = Random(seed), Random(seed)
        a = draw_recoverer(lineup, first, DEFAULT_TUNING)
        b = draw_recoverer(lineup, second, DEFAULT_TUNING)
        assert a.id == b.id
        assert first.getstate() == _one_random_later(seed).getstate()


def test_the_ball_loser_and_every_other_player_are_eligible():
    """All five are candidates (TR-4.7): with equal scores every lineup
    slot is drawn, the first included."""
    rng = Random(7)
    lineup = _lineup(1, _EVEN)
    assert {draw_recoverer(lineup, rng, DEFAULT_TUNING).id for _ in range(500)} == {1, 2, 3, 4, 5}


def test_an_all_zero_lineup_draws_uniformly_down_the_one_random_path():
    """The zero-total guard (PD-2 disclosure 3 / PP-10.1's hazard):
    `weighted_choice` would send an all-zero vector to `rng.choice`, which
    consumes a different number of draws and desyncs the seeded game. The
    guard substitutes equal weights, so the draw is uniform AND still exactly
    one `rng.random()`. A single zero-score player among positive ones is
    simply never drawn."""
    zeroed = tuple(
        dataclasses.replace(p, loose_ball_contribution=0.0) for p in _lineup(1, _EVEN)
    )
    seen = set()
    for seed in range(500):
        rng = Random(seed)
        seen.add(draw_recoverer(zeroed, rng, DEFAULT_TUNING).id)
        assert rng.getstate() == _one_random_later(seed).getstate()
    assert seen == {1, 2, 3, 4, 5}

    one_zero = (dataclasses.replace(_spread_lineup()[0], loose_ball_contribution=0.0),
                *_spread_lineup()[1:])
    rng = Random(3)
    assert 1 not in {draw_recoverer(one_zero, rng, DEFAULT_TUNING).id for _ in range(2000)}

    # A negative score is clamped before the fractional power (a negative
    # base to 2.4 is complex, not a weight).
    negative = (dataclasses.replace(_spread_lineup()[0], loose_ball_contribution=-5.0),
                *_spread_lineup()[1:])
    rng = Random(3)
    assert 1 not in {draw_recoverer(negative, rng, DEFAULT_TUNING).id for _ in range(2000)}


def test_an_empty_candidate_list_raises():
    with pytest.raises(ValueError, match="at least one offensive player"):
        draw_recoverer((), Random(0), DEFAULT_TUNING)
