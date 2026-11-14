"""RPD-8-6: the no-repair generator agrees with `synthetic_ratings`, and the fit solver
lands the displayed overall on the target."""

from __future__ import annotations

from random import Random

import pytest

from dba2.domain.player import Position
from dba2.sim.derivation import (
    POSITION_RATING_OFFSETS,
    RATING_FIELD_ORDER,
    synthetic_ratings,
)
from tools import _real_fields as rf
from tools import _real_synth as rs

POSITIONS = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)


def _own_repair_is_a_noop(talent: float, position: Position, seed: str) -> bool:
    return synthetic_ratings(talent, position, Random(seed)) == rs.synthetic_no_repair(
        talent, position, Random(seed)
    )


def test_agrees_field_for_field_wherever_the_repair_is_a_noop() -> None:
    agreed = 0
    for position in POSITIONS:
        for talent in (52.0, 63.4, 70.0, 76.2, 84.0):
            for i in range(20):
                seed = f"{position.value}:{talent}:{i}"
                if _own_repair_is_a_noop(talent, position, seed):
                    agreed += 1
    # The repair fires whenever the mean of 55 misses round(talent) by more than half a
    # point, which the group noise (SD 6 per group) makes common: only about a fifth of
    # the 500 draws are untouched. Still a real pin, and the next test covers the rest.
    assert agreed >= 50


def test_where_repair_did_move_things_it_only_shifted_in_one_direction() -> None:
    # The two generators may only differ by the repair pass, which adds +1s or -1s but
    # never both; anything else would mean the draw order or arithmetic diverged.
    differing = 0
    for position in POSITIONS:
        for i in range(30):
            seed = f"{position.value}:shift:{i}"
            repaired = synthetic_ratings(73.0, position, Random(seed))
            plain = rs.synthetic_no_repair(73.0, position, Random(seed))
            diffs = [repaired[f] - plain[f] for f in RATING_FIELD_ORDER]
            if any(diffs):
                differing += 1
                assert all(d >= 0 for d in diffs) or all(d <= 0 for d in diffs)
    assert differing > 0


def test_noise_drawn_once_equals_a_fresh_seeded_draw_per_probe() -> None:
    """The fit draws the noise once; that is only the per-probe re-seeding the
    bisection needs if a fresh `Random(seed)` always yields the same noise."""
    noise = rs.draw_noise(Random("k"))
    for talent in (40.0, 63.3, 91.7):
        assert rs.ratings_from_noise(talent, Position.PF, noise) == rs.synthetic_no_repair(
            talent, Position.PF, Random("k")
        )


def test_same_seed_same_ratings_and_talent_is_monotone_per_field() -> None:
    low = rs.synthetic_no_repair(60.0, Position.SF, Random("x"))
    same = rs.synthetic_no_repair(60.0, Position.SF, Random("x"))
    high = rs.synthetic_no_repair(75.0, Position.SF, Random("x"))
    assert low == same
    assert all(high[f] >= low[f] for f in RATING_FIELD_ORDER)


def _real_block(position: Position, talent: float, shift: float = 0.0) -> dict[str, int]:
    offsets = POSITION_RATING_OFFSETS[position]
    return {
        f: int(min(99, max(25, round(talent + offsets[f] + shift + ((i * 7) % 11) - 5))))
        for i, f in enumerate(rf.REAL_FIELDS)
    }


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("target", [45, 58, 66, 71, 79, 88, 94])
def test_fit_lands_the_displayed_overall_on_the_target(position: Position, target: int) -> None:
    real = _real_block(position, float(target))
    fit = rs.fit_synthetic_remainder(
        seed_key=f"p:{target}",
        position=position,
        real_fields=real,
        target_overall=target,
        hidden_talent=float(target),
    )
    assert abs(fit.overall - target) <= 1
    assert fit.overall == rs.overall_of(fit.ratings, position)
    assert set(fit.ratings) == set(RATING_FIELD_ORDER)
    assert all(fit.ratings[f] == v for f, v in real.items())  # real fields untouched
    assert all(25 <= v <= 99 for v in fit.ratings.values())


def test_fit_usually_hits_the_target_exactly() -> None:
    hits = 0
    total = 0
    for position in POSITIONS:
        for target in range(50, 92, 3):
            fit = rs.fit_synthetic_remainder(
                seed_key=f"q:{position.value}:{target}",
                position=position,
                real_fields=_real_block(position, float(target) - 4.0),
                target_overall=target,
                hidden_talent=float(target),
            )
            total += 1
            hits += fit.hit(target)
    assert hits / total >= 0.95


def test_fit_is_deterministic() -> None:
    kwargs = {
        "seed_key": "det",
        "position": Position.PG,
        "real_fields": _real_block(Position.PG, 72.0),
        "target_overall": 75,
        "hidden_talent": 75.0,
    }
    assert rs.fit_synthetic_remainder(**kwargs) == rs.fit_synthetic_remainder(**kwargs)


def test_real_fields_that_average_high_pull_the_synthetic_remainder_down_not_the_target() -> None:
    position = Position.SF
    base = rs.fit_synthetic_remainder(
        seed_key="a",
        position=position,
        real_fields=_real_block(position, 70.0),
        target_overall=70,
        hidden_talent=70.0,
    )
    strong_real = rs.fit_synthetic_remainder(
        seed_key="a", position=position, real_fields=_real_block(position, 70.0, 8.0),
        target_overall=70,
        hidden_talent=70.0,
    )
    assert strong_real.t_prime < base.t_prime
    assert abs(strong_real.overall - 70) <= 1


def test_unreachable_target_returns_closest_and_reports_a_miss() -> None:
    real = dict.fromkeys(rf.REAL_FIELDS, 25)
    fit = rs.fit_synthetic_remainder(
        seed_key="z", position=Position.PG, real_fields=real, target_overall=97, hidden_talent=97.0
    )
    assert not fit.hit(97)
    assert fit.overall < 97


def test_nudge_loop_corrects_an_overshoot_from_a_coarse_step() -> None:
    # 2 movable fields only: bisection alone cannot hit every target, the nudge must.
    position = Position.SG
    movable = ("speed", "handle")
    real = {f: 60 for f in RATING_FIELD_ORDER if f not in movable}
    target = rs.overall_of({**real, "speed": 60, "handle": 60}, position) + 1
    fit = rs.fit_synthetic_remainder(
        seed_key="n", position=position, real_fields=real, target_overall=target, hidden_talent=60.0
    )
    assert fit.overall == target
    assert all(fit.ratings[f] == 60 for f in real)


def test_target_below_reach_of_very_high_real_fields_returns_the_lowest_talent() -> None:
    real = dict.fromkeys(rf.REAL_FIELDS, 99)
    fit = rs.fit_synthetic_remainder(
        seed_key="lo", position=Position.SF, real_fields=real, target_overall=40, hidden_talent=40.0
    )
    assert fit.overall > 40 and not fit.hit(40)
    assert fit.t_prime == -40.0


def test_a_target_needing_a_synthetic_talent_above_the_target_still_fits() -> None:
    # Real fields near 60 pull the displayed overall down, so the remainder must be
    # generated well above the target itself.
    position = Position.SG
    fit = rs.fit_synthetic_remainder(
        seed_key="far",
        position=position,
        real_fields=_real_block(position, 60.0),
        target_overall=74,
        hidden_talent=74.0,
    )
    assert abs(fit.overall - 74) <= 1
    assert fit.t_prime > 74


def test_hidden_fields_follow_the_mapped_talent_not_the_shifted_talent() -> None:
    """Critic B2: `potential`/`potential_variance` are invisible to the overall, so the
    fit's T' shift must not reach them. Real fields 10 points under the target force T'
    far above T; the hidden pair must equal what `synthetic_ratings` gives at T."""
    position = Position.SG
    for i in range(20):
        target = 60 + i
        real = _real_block(position, float(target) - 10.0)
        fit = rs.fit_synthetic_remainder(
            seed_key=f"h:{i}",
            position=position,
            real_fields=real,
            target_overall=target,
            hidden_talent=float(target),
        )
        assert fit.t_prime > target + 3  # the shift is real, so the check bites
        at_t = rs.synthetic_no_repair(float(target), position, Random(f"h:{i}"))
        for name in ("potential", "potential_variance"):
            assert fit.ratings[name] == at_t[name]


def test_hidden_fields_relation_to_overall_matches_the_legacy_synthetic() -> None:
    """mean(potential - overall) over a season-sized sample stays near the legacy
    synthetic's (about 0) even when the real fields drag T' well away from T."""
    gaps, legacy_gaps = [], []
    for i, position in enumerate(POSITIONS * 40):
        target = 58 + (i * 7) % 30
        fit = rs.fit_synthetic_remainder(
            seed_key=f"m:{i}",
            position=position,
            real_fields=_real_block(position, float(target) - 6.0),
            target_overall=target,
            hidden_talent=float(target),
        )
        gaps.append(fit.ratings["potential"] - fit.overall)
        legacy = synthetic_ratings(float(target), position, Random(f"m:{i}"))
        legacy_gaps.append(legacy["potential"] - rs.overall_of(legacy, position))
    assert abs(sum(gaps) / len(gaps) - sum(legacy_gaps) / len(legacy_gaps)) < 1.0
    assert abs(sum(gaps) / len(gaps)) < 1.0
