"""The universal contest -> probability helpers (phase-1-simulation-engine.md
§4.4, Decision P1-12): read a named ability vector off a `SimPlayer`, turn
two vectors' weighted means into a single clamped probability, and make one
deterministic weighted draw from the possession's RNG.

Zero I/O, per Decision P1-1. Imports only `context` (for the `SimPlayer`
type) and stdlib -- no `dba2.data`, no bare `random.Random()` construction
(every function here takes an already-seeded `random.Random` as a
parameter), no `datetime`, no file or env access.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from random import Random

from dba2.sim.context import SimPlayer


def sigmoid(x: float) -> float:
    """Numerically stable logistic function -- avoids `OverflowError` on
    the branch `math.exp` would otherwise take for a large-magnitude `x`
    (a genuinely reachable case here: an extreme fixture's score diff
    divided by a small `scale` easily exceeds `math.exp`'s ~709 ceiling)."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def logit(p: float, *, epsilon: float = 1e-6) -> float:
    """Inverse of `sigmoid`, with `p` clamped away from the 0/1 boundary
    first -- `math.log(0)` and division by zero are both real inputs a
    beat's declared `base_rate` could otherwise hit."""
    p = clamp(p, epsilon, 1.0 - epsilon)
    return math.log(p / (1.0 - p))


def clamp(value: float, lo: float, hi: float) -> float:
    if lo > hi:
        raise ValueError(f"clamp band is inverted: lo={lo} > hi={hi}")
    return min(max(value, lo), hi)


def rating_value(player: SimPlayer, field_name: str) -> float:
    """Read one named field off `player` for use in an ability vector.

    Decision P1-16's one named exception: `wingspan_in`/`height_in` are
    `Player` fields, not `PlayerRatings` fields, but Decision P1-13
    normalizes both into 0-99 rating space at `SimPlayer` construction --
    this function is the single place that dispatches to the normalized
    cache instead of the raw domain field, so every ability-vector reader
    (screen/on_ball/help today, resolution/rebound/rotation once step 4-5
    lands) goes through one mechanism rather than each beat re-deriving
    the wingspan/height special case independently."""
    if field_name == "wingspan_in":
        return player.wingspan_normalized
    if field_name == "height_in":
        return player.height_normalized
    return float(getattr(player.ratings, field_name))


def ability_score(player: SimPlayer, weights: Mapping[str, float]) -> float:
    """The weighted mean of an ability vector's named fields on `player`,
    per Decision P1-12 ("each side's score is the weighted mean of those
    ratings on the named actors"). Weights are normalized by their own sum
    so a caller can add/drop a field without re-balancing the rest."""
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ValueError("ability vector weights must sum to a positive total")
    return sum(rating_value(player, field) * w for field, w in weights.items()) / total_weight


def contest_probability(
    offense_score: float,
    defense_score: float,
    *,
    base_rate: float,
    scale: float,
    band: tuple[float, float],
) -> float:
    """Decision P1-12's universal shape: a base rate (at `diff == 0`)
    shifted in logit space by the scaled score difference, then clamped to
    the beat's declared plausible band.

    Monotonic increasing in `offense_score`, monotonic decreasing in
    `defense_score`, and bounded to `band` regardless of how extreme the
    inputs are -- `sigmoid` alone already bounds the raw shift to (0, 1),
    and the explicit `clamp` enforces the tighter, beat-specific plausible
    range on top of that.
    """
    if scale <= 0:
        raise ValueError(f"scale must be positive, got {scale}")
    diff = offense_score - defense_score
    shifted = logit(base_rate) + diff / scale
    p = sigmoid(shifted)
    lo, hi = band
    return clamp(p, lo, hi)


def weighted_choice[K](rng: Random, weights: Mapping[K, float]) -> K:
    """One deterministic draw from `rng` picking a key from `weights`.

    Weights need not sum to 1 (normalized internally). A degenerate
    all-non-positive-weight input falls back to a uniform draw among the
    candidate keys rather than raising -- a beat with an extreme fixture
    (e.g. every branch clamped to a zero floor) must still terminate with
    *some* outcome, per §4.5's closed-set requirement, not blow up.
    """
    items = list(weights.items())
    if not items:
        raise ValueError("weighted_choice requires at least one candidate")
    total = sum(max(w, 0.0) for _, w in items)
    if total <= 0:
        return rng.choice([k for k, _ in items])
    r = rng.random() * total
    upto = 0.0
    for key, w in items:
        upto += max(w, 0.0)
        if r <= upto:
            return key
    return items[-1][0]  # floating-point safety net, never reached in practice
