"""§12 step 2's verify list: 10,000 seeded players every rating in
[25, 99]; the repair postcondition `abs(mean_of_55 - round(talent)) <=
0.5`; the returned key set being exactly `RATING_FIELD_ORDER`; positions
3/3/3/3/2; height by position within ±1.5 of §5.3's means; and seed
determinism (same seed -> byte-identical, different seed -> different).
"""
from __future__ import annotations

from random import Random

import pytest

from dba2.domain.player import Position
from dba2.sim import derivation as d

_POSITIONS = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)


def _all_talents_and_positions(seed: int, n: int) -> list[tuple[float, Position]]:
    rng = Random(seed)
    tiers = list(d.RosterTier)
    out: list[tuple[float, Position]] = []
    for i in range(n):
        tier = tiers[i % len(tiers)]
        team_offset = rng.gauss(0.0, 2.5)
        talent = d.tier_talent(tier, team_offset, rng)
        position = _POSITIONS[i % 5]
        out.append((talent, position))
    return out


def test_10000_seeded_players_every_rating_in_25_to_99_and_repaired_within_half_point():
    rng = Random(12345)
    for i in range(10_000):
        tier = list(d.RosterTier)[i % 4]
        talent = d.tier_talent(tier, rng.gauss(0.0, 2.5), rng)
        position = _POSITIONS[i % 5]
        ratings = d.synthetic_ratings(talent, position, rng)

        assert set(ratings) == set(d.RATING_FIELD_ORDER)
        assert all(25 <= v <= 99 for v in ratings.values())

        target = round(max(40.0, min(97.0, talent)))
        mean_of_55 = sum(ratings.values()) / 55
        assert abs(mean_of_55 - target) <= 0.5, (talent, target, mean_of_55)


def test_synthetic_ratings_key_set_is_exactly_rating_field_order():
    rng = Random(1)
    ratings = d.synthetic_ratings(70.0, Position.PG, rng)
    assert set(ratings) == set(d.RATING_FIELD_ORDER)
    assert "peak_age_start" not in ratings
    assert "peak_age_end" not in ratings


def test_synthetic_positions_is_3_3_3_3_2():
    rng = Random(7)
    positions = d.synthetic_positions(rng)
    assert len(positions) == 14
    counts = {pos: positions.count(pos) for pos in _POSITIONS}
    values = sorted(counts.values())
    assert values == [2, 3, 3, 3, 3]
    assert set(positions) == set(_POSITIONS)


def test_synthetic_positions_over_1000_draws_keeps_every_position_representable():
    rng = Random(99)
    short_counts = {pos: 0 for pos in _POSITIONS}
    for _ in range(1000):
        positions = d.synthetic_positions(rng)
        counts = {pos: positions.count(pos) for pos in _POSITIONS}
        short = [pos for pos, c in counts.items() if c == 2]
        assert len(short) == 1
        short_counts[short[0]] += 1
    # each position should be the "short" one roughly 1/5 of the time
    for pos, count in short_counts.items():
        assert 100 < count < 300, (pos, count)


@pytest.mark.parametrize(
    ("position", "expected_mean"),
    [
        (Position.PG, 74.5),
        (Position.SG, 77.0),
        (Position.SF, 79.0),
        (Position.PF, 81.0),
        (Position.C, 83.0),
    ],
)
def test_height_by_position_within_1_5_of_doc_means_over_1000_draws(position, expected_mean):
    rng = Random(2026)
    heights = [d.synthetic_physicals(position, rng).height_in for _ in range(1000)]
    mean_height = sum(heights) / len(heights)
    assert abs(mean_height - expected_mean) <= 1.5


def test_synthetic_physicals_bounds_and_ordering():
    rng = Random(3)
    for _ in range(2000):
        for position in _POSITIONS:
            physicals = d.synthetic_physicals(position, rng)
            assert 70 <= physicals.height_in <= 88
            assert physicals.height_in - 1 <= physicals.wingspan_in <= physicals.height_in + 9
            assert 160 <= physicals.weight_lb <= 300


def test_same_seed_produces_byte_identical_roster_and_different_seed_does_not():
    def build(seed: int) -> tuple:
        rng = Random(seed)
        positions = d.synthetic_positions(rng)
        physicals = tuple(d.synthetic_physicals(pos, rng) for pos in positions)
        talents = tuple(
            d.tier_talent(d.TIER_BY_SLOT[i], 1.2, rng) for i in range(14)
        )
        ratings = tuple(
            tuple(sorted(d.synthetic_ratings(talents[i], positions[i], rng).items()))
            for i in range(14)
        )
        return positions, physicals, talents, ratings

    a1 = build(42)
    a2 = build(42)
    b = build(43)

    assert a1 == a2
    assert a1 != b


def test_tier_talent_is_clamped_to_40_97():
    rng = Random(555)
    for _ in range(5000):
        for tier in d.RosterTier:
            value = d.tier_talent(tier, rng.gauss(0.0, 2.5), rng)
            assert 40.0 <= value <= 97.0
