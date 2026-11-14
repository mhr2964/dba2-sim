"""§12 step 3's verify list: the four `shot_freq_*` sum to exactly 100
over 10,000 seeded players and over a hand-built adversarial set; every
value inside its declared band; monotonicity per §9's rule; the returned
key set equals the 17 `PlayerTendencies` field names; `rng=None` is
deterministic; every `DESIRED` row and every solved `pos_row` sums to 0
(also covered directly in test_derivation_ratings_model.py; not repeated
here).
"""
from __future__ import annotations

import dataclasses
from random import Random

from dba2.domain.player import Position
from dba2.domain.ratings import PlayerRatings, PlayerTendencies
from dba2.sim import derivation as d
from tests.fixtures.sim import make_ratings

_POSITIONS = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)
_SHOT_FREQ_FIELDS = ("shot_freq_rim", "shot_freq_mid", "shot_freq_three", "shot_freq_post")


def _all_at(value: int) -> PlayerRatings:
    return make_ratings(1, seed=1, **{f: value for f in d.RATING_FIELD_ORDER})


def test_shot_freqs_sum_to_100_over_10000_seeded_players():
    rng = Random(2026)
    for i in range(10_000):
        overrides = {f: rng.randint(0, 99) for f in d.RATING_FIELD_ORDER}
        ratings = make_ratings(i, seed=i, **overrides)
        position = _POSITIONS[i % 5]
        result = d.derive_tendencies(ratings, position, rng=rng)
        total = sum(result[f] for f in _SHOT_FREQ_FIELDS)
        assert total == 100, (i, result)


def _adversarial_players() -> list[tuple[PlayerRatings, Position]]:
    players: list[tuple[PlayerRatings, Position]] = [
        (_all_at(25), Position.PG),
        (_all_at(99), Position.C),
    ]
    tier_means = {
        d.RosterTier.STAR: 84.0,
        d.RosterTier.STARTER: 76.0,
        d.RosterTier.ROTATION: 70.0,
        d.RosterTier.BENCH: 63.0,
    }
    seed = 0
    for mean in tier_means.values():
        for position in _POSITIONS:
            seed += 1
            rng = Random(seed)
            ratings_dict = d.synthetic_ratings(mean, position, rng)
            ratings = make_ratings(seed, seed=seed, **ratings_dict)
            players.append((ratings, position))
    return players


def test_shot_freqs_sum_to_100_over_adversarial_set():
    for ratings, position in _adversarial_players():
        result = d.derive_tendencies(ratings, position, rng=None)
        total = sum(result[f] for f in _SHOT_FREQ_FIELDS)
        assert total == 100, (ratings.player_id, position, result)


def test_every_value_inside_its_declared_band():
    for ratings, position in _adversarial_players():
        result = d.derive_tendencies(ratings, position, rng=None)
        for field_name, (lo, hi) in (
            (name, formula.band) for name, formula in d._TENDENCY_FORMULAS.items()
        ):
            assert lo <= result[field_name] <= hi, (field_name, result[field_name])
        for field_name in _SHOT_FREQ_FIELDS:
            assert 2 <= result[field_name] <= 94


def test_returned_key_set_equals_the_17_tendency_field_names():
    ratings = _all_at(60)
    result = d.derive_tendencies(ratings, Position.SF, rng=None)
    expected = {
        f.name
        for f in dataclasses.fields(PlayerTendencies)
        if f.name not in {"player_id", "league_id", "updated_at"}
    }
    assert set(result) == expected
    assert len(expected) == 17


def test_rng_none_is_deterministic():
    ratings = make_ratings(1, seed=5)
    a = d.derive_tendencies(ratings, Position.SG, rng=None)
    b = d.derive_tendencies(ratings, Position.SG, rng=None)
    assert a == b


def test_raising_catch_shoot_3_never_lowers_shot_freq_three():
    base = make_ratings(1, seed=1, catch_shoot_3=40)
    boosted = dataclasses.replace(base, catch_shoot_3=95)
    for position in _POSITIONS:
        low = d.derive_tendencies(base, position, rng=None)
        high = d.derive_tendencies(boosted, position, rng=None)
        assert high["shot_freq_three"] >= low["shot_freq_three"], position


def test_raising_foul_discipline_never_raises_foul_freq():
    base = make_ratings(1, seed=2, foul_discipline=20)
    disciplined = dataclasses.replace(base, foul_discipline=95)
    for position in _POSITIONS:
        low = d.derive_tendencies(base, position, rng=None)
        high = d.derive_tendencies(disciplined, position, rng=None)
        assert high["foul_freq"] <= low["foul_freq"], position


def test_rng_supplied_adds_noise_and_still_sums_shot_freqs_to_100():
    ratings = make_ratings(1, seed=3)
    rng = Random(999)
    result = d.derive_tendencies(ratings, Position.PF, rng=rng)
    total = sum(result[f] for f in _SHOT_FREQ_FIELDS)
    assert total == 100
