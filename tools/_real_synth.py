"""The no-repair synthetic generator and the fit solver for real-player seeds
(RPD-8-6, docs/design/real-player-ratings-v2.md).

**Why this exists instead of calling `sim.derivation.synthetic_ratings`.** A real
player's ~25 measured fields are fixed; the rest must be synthetic and must land the
player's DISPLAYED overall (`compute_overall`) on the rating the quantile map chose.
Fitting that needs a search over the synthetic talent `T'`, and `synthetic_ratings`
cannot be searched: it runs `_repair_clamp` over all 55 fields internally, whose result
is not monotone in talent (it starts at `target % 55` and only fires past a 0.5 miss),
and it would move the real fields too. So this module re-implements the draw with the
SAME draw order (11 group noises, then 55 field noises) and no repair, reading
`derivation.py`'s tables and noise SDs read-only. `tests/unit/tools/test_real_synth.py`
pins that wherever `synthetic_ratings`' own repair is a no-op the two agree field for
field.

**Why bisection is well defined.** Every probe builds a fresh `Random` from the same
seed, so the noise is identical across probes; each synthetic field is then
`clamp(round(T' + offset + noise))`, a non-decreasing step function of `T'`; real
fields are constants; `compute_overall` is monotone in every field. So the overall is a
non-decreasing step function of `T'` and the smallest `T'` reaching the target exists
(or does not, when even all-99 synthetic fields fall short). The overall can jump past
the target in one step, so a bounded nudge loop then moves single synthetic fields by
one point (movable fields only, re-checked every step) until the overall equals the
target or nothing can move.

**Hidden fields are generated at the mapped talent `T`, not at `T'`.** `potential` and
`potential_variance` are excluded from `compute_overall` (`_HIDDEN_FROM_OVERALL`), so the
fit cannot see them and has no reason to move them. Generating them at `T'` put 100% of
the `T' - T` correction (mean -1.4, SD 3.2, up to +-14 on real rows) on the two fields the
progression stub reads as `potential - overall`, so rebuilt players grew less than
synthetic peers at equal overall (critic B2). The bisection therefore shifts only the
PUBLIC synthetic remainder; the hidden pair keeps the same relation to `T` that
`synthetic_ratings` gives it.

Zero I/O, deterministic, no `dba2/` file is edited.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from random import Random
from types import SimpleNamespace

from dba2.domain.player import Position
from dba2.sim.derivation import (
    _FIELD_NOISE_SD,
    _FIELD_TO_GROUP,
    _GROUP_NOISE_SD,
    POSITION_RATING_OFFSETS,
    RATING_FIELD_ORDER,
    RATING_GROUPS,
)
from dba2.sim.ratings import _HIDDEN_FROM_OVERALL, compute_overall

# Search window for T'. Wide on purpose: with real fields clamped at 99 (or 25) the
# synthetic remainder may need a talent far outside the [40, 97] talent clamp.
_T_PRIME_LOW = -40.0
_T_PRIME_HIGH = 160.0
_BISECTION_STEPS = 24  # window / 2**24 is far finer than any rounding step
_NEAR_WINDOW = 40.0
_NUDGE_STEP_CAP = 74  # per synthetic field, mirrors _repair_clamp's n * 74 bound

# Ordering only matters for determinism of the nudge loop.
_PUBLIC_ORDER: tuple[str, ...] = tuple(
    f for f in RATING_FIELD_ORDER if f not in _HIDDEN_FROM_OVERALL
)


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


@dataclass(frozen=True)
class Noise:
    """The 11 group noises then 55 field noises one `Random` yields, in the order
    `sim.derivation.synthetic_ratings` draws them."""

    group: dict[str, float]
    field: dict[str, float]


def draw_noise(rng: Random) -> Noise:
    group = {group: rng.gauss(0.0, _GROUP_NOISE_SD) for group in RATING_GROUPS}
    field = {name: rng.gauss(0.0, _FIELD_NOISE_SD) for name in RATING_FIELD_ORDER}
    return Noise(group=group, field=field)


def ratings_from_noise(talent: float, position: Position, noise: Noise) -> dict[str, int]:
    offsets = POSITION_RATING_OFFSETS[position]
    return {
        name: int(
            _clamp(
                round(
                    talent + offsets[name] + noise.group[_FIELD_TO_GROUP[name]] + noise.field[name]
                ),
                25,
                99,
            )
        )
        for name in RATING_FIELD_ORDER
    }


def synthetic_no_repair(talent: float, position: Position, rng: Random) -> dict[str, int]:
    """`sim.derivation.synthetic_ratings` minus `_repair_clamp`: identical draw order
    and arithmetic, so it equals that function wherever its repair does nothing."""
    return ratings_from_noise(talent, position, draw_noise(rng))


def overall_of(ratings: Mapping[str, int], position: Position) -> int:
    """The displayed overall, by the sim's own function (never re-implemented here)."""
    return compute_overall(SimpleNamespace(**ratings), position)


@dataclass(frozen=True)
class RemainderFit:
    ratings: dict[str, int]  # all 55 fields, real ones untouched
    t_prime: float  # the talent the PUBLIC synthetic remainder was generated at
    overall: int  # compute_overall of `ratings`
    nudges: int  # single-field one-point corrections after the bisection

    def hit(self, target: int) -> bool:
        return self.overall == target


def fit_synthetic_remainder(
    *,
    seed_key: str,
    position: Position,
    real_fields: Mapping[str, int],
    target_overall: int,
    hidden_talent: float,
) -> RemainderFit:
    """All 55 ratings for one player: `real_fields` fixed, every other PUBLIC field
    synthetic at the smallest `T'` whose displayed overall reaches `target_overall`,
    then nudged one point at a time onto the target. The hidden fields (`potential`,
    `potential_variance`) never enter the overall and are drawn at `hidden_talent` (the
    player's mapped talent `T`), from the same noise, not at `T'`.

    `seed_key` seeds the noise (the builder passes `f"{external_ref}:{season}"`, the
    ETL's own seed); every probe sees that same noise, which is what makes the
    bisection well defined. When the target is unreachable (real fields clamped so
    high or low that even all-99 or all-25 synthetic fields cannot meet it) the closest
    reachable overall is returned and `hit()` is False; the build report counts those."""

    # Every probe of a fresh `Random(seed_key)` yields this same noise, so drawing it
    # once is exactly the per-probe re-seeding the bisection relies on (pinned by a
    # test) at a third of the cost.
    noise = draw_noise(Random(seed_key))
    hidden = {
        name: value
        for name, value in ratings_from_noise(hidden_talent, position, noise).items()
        if name in _HIDDEN_FROM_OVERALL
    }

    def build(t_prime: float) -> dict[str, int]:
        ratings = ratings_from_noise(t_prime, position, noise)
        ratings.update(hidden)
        ratings.update(real_fields)
        return ratings

    def overall_at(t_prime: float) -> int:
        return overall_of(build(t_prime), position)

    # Bracket around the target first (real fields usually sit near it), widening to
    # the full window only when the narrow one does not cross. Invariant for the
    # bisection: overall(lo) < target <= overall(hi).
    lo = max(_T_PRIME_LOW, target_overall - _NEAR_WINDOW)
    hi = min(_T_PRIME_HIGH, target_overall + _NEAR_WINDOW)
    t_prime: float | None = None
    if overall_at(hi) < target_overall:
        lo, hi = hi, _T_PRIME_HIGH
        if overall_at(hi) < target_overall:
            t_prime = _T_PRIME_HIGH  # unreachable: even the highest synthetic talent falls short
    elif overall_at(lo) >= target_overall:
        lo, hi = _T_PRIME_LOW, lo
        if overall_at(lo) >= target_overall:
            t_prime = _T_PRIME_LOW  # already above target at the lowest synthetic talent
    if t_prime is None:
        for _ in range(_BISECTION_STEPS):
            mid = (lo + hi) / 2.0
            if overall_at(mid) >= target_overall:
                hi = mid
            else:
                lo = mid
        t_prime = hi

    ratings = build(t_prime)
    movable = [f for f in _PUBLIC_ORDER if f not in real_fields]
    overall = overall_of(ratings, position)
    nudges = 0
    if movable and overall != target_overall:
        sign = 1 if overall < target_overall else -1
        start = target_overall % len(movable)
        idx = start
        moved_in_cycle = False
        for _ in range(len(movable) * _NUDGE_STEP_CAP):
            field_name = movable[idx]
            value = ratings[field_name]
            if (sign > 0 and value < 99) or (sign < 0 and value > 25):
                ratings[field_name] = value + sign
                moved_in_cycle = True
                nudges += 1
                overall = overall_of(ratings, position)
                if overall == target_overall or (overall - target_overall) * sign > 0:
                    break
            idx = (idx + 1) % len(movable)
            if idx == start:
                if not moved_in_cycle:
                    break  # no movable field remains
                moved_in_cycle = False
    return RemainderFit(ratings=ratings, t_prime=t_prime, overall=overall, nudges=nudges)
