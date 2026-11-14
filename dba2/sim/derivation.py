"""Calibrated synthetic roster/ratings/tendencies generator
(`docs/design/phase-1-step-7-calibration.md`, Decisions S7-2 through
S7-11a) plus the closed-form variance model (§6.7) that turns this
module's own offset table into the league-distribution bands the step-7
verify test asserts against, instead of a document hand-typing them.

Zero I/O, per Decision P1-1 (this file's own scope note, §11.1): may
import `domain` and `core` only. No `sim.probability`, no `sim.ratings`,
no `dba2.data` — `tests/unit/test_layering.py` enforces the `sim` clause
mechanically and this module's own docstring in the design doc names the
narrower "domain and core only" rule on top of it, so a couple of small
helpers below (`_clamp`, `_weighted_choice`) duplicate rather than import
`sim.probability`'s versions.

Everything here is pure: every randomness source is an explicit `Random`
parameter (never a bare `random.Random()` construction), and every
league-shaped constant (`RATING_GROUPS`, the raw offset table,
`TENDENCY_TARGET_SD`, the tier talent table) is declared data, not a
value read from a file or a database. `expected_group_sd`,
`TENDENCY_SPREADS` and `TENDENCY_POSITION_ROWS` are *solved* from that
declared data at import time (§6.7, §8.4) rather than hand-typed, which
is what makes them impossible to drift out of sync with the offset table
the way revision 1's hand-typed bands did (design doc §3).
"""
from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from random import Random
from types import MappingProxyType

from dba2.domain.player import Position
from dba2.domain.ratings import PlayerRatings, PlayerTendencies

# ---------------------------------------------------------------------------
# Small local helpers -- duplicated from sim.probability rather than
# imported, per this module's "domain and core only" import restriction
# (see module docstring).
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


def _weighted_choice(rng: Random, weights: Mapping[Position, float]) -> Position:
    """One draw from `rng` over `weights` (need not sum to 1). Used only for
    §5.2's guard-skewed star-position draw, so the fallback shape
    `sim.probability.weighted_choice` needs (an all-non-positive-weight
    input) is not reproduced here -- every weight in `_STAR_POSITION_WEIGHTS`
    is a positive literal."""
    items = list(weights.items())
    total = sum(w for _, w in items)
    r = rng.random() * total
    upto = 0.0
    for key, w in items:
        upto += w
        if r <= upto:
            return key
    return items[-1][0]  # floating-point safety net, never reached in practice


_POSITIONS: tuple[Position, ...] = (
    Position.PG,
    Position.SG,
    Position.SF,
    Position.PF,
    Position.C,
)

# ---------------------------------------------------------------------------
# §6.1 -- rating groups (Decision S7-4). The 11 groups are exactly
# domain/ratings.py's own section comments, promoted from comments to a
# declared constant. `RATING_FIELD_ORDER`'s flattening of this table, in
# declaration order, is the single canonical iteration order for
# everything in this module (§6.2's noise draws, §6.5's repair loop).
# ---------------------------------------------------------------------------

RATING_GROUPS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "PHYSICAL": (
            "speed",
            "acceleration",
            "lateral_quickness",
            "strength",
            "vertical",
            "stamina",
            "durability",
        ),
        "CREATION": (
            "handle",
            "ball_security",
            "first_step",
            "change_of_direction",
            "screen_use",
            "draw_foul",
        ),
        "FINISHING": ("finishing_rim", "contact_finishing", "floater", "post_scoring"),
        "SHOOTING": (
            "catch_shoot_3",
            "off_dribble_3",
            "catch_shoot_mid",
            "off_dribble_mid",
            "free_throw",
        ),
        "PLAYMAKING": (
            "passing_vision",
            "passing_accuracy",
            "pass_under_pressure",
            "short_roll_passing",
        ),
        "SCREENING": ("screen_setting", "screen_timing", "roll_ability", "catch_hands"),
        "OFF_BALL": ("off_ball_movement", "relocation", "cutting", "offensive_rebounding"),
        "ON_BALL_DEF": ("on_ball_defense", "hands", "post_defense", "foul_discipline"),
        "TEAM_DEF": (
            "screen_navigation",
            "help_iq",
            "rotation_speed",
            "rim_protection",
            "closeout_speed",
            "closeout_control",
            "scheme_discipline",
            "switchability",
            "defensive_rebounding",
        ),
        "MENTAL": ("basketball_iq", "composure", "consistency", "motor", "coachability"),
        "DEVELOPMENT": ("potential", "potential_variance", "work_ethic"),
    }
)

RATING_FIELD_ORDER: tuple[str, ...] = tuple(
    field_name for fields in RATING_GROUPS.values() for field_name in fields
)
assert len(RATING_FIELD_ORDER) == 55
assert len(set(RATING_FIELD_ORDER)) == 55

_FIELD_TO_GROUP: Mapping[str, str] = MappingProxyType(
    {field_name: group for group, fields in RATING_GROUPS.items() for field_name in fields}
)

# §6.3 -- the 5 fields §6.4 authors as identically zero across all 5
# positions, exempt from the row-centering pass (I3).
POSITION_NEUTRAL_FIELDS: frozenset[str] = frozenset(
    {"potential", "potential_variance", "work_ethic", "consistency", "coachability"}
)

_CARRIER_FIELDS: tuple[str, ...] = tuple(
    f for f in RATING_FIELD_ORDER if f not in POSITION_NEUTRAL_FIELDS
)
assert len(_CARRIER_FIELDS) == 50

# ---------------------------------------------------------------------------
# §6.4 -- raw position offsets, pre-centering. Column order matches
# `_POSITIONS` (PG, SG, SF, PF, C) exactly.
# ---------------------------------------------------------------------------

_RAW_POSITION_OFFSETS: Mapping[str, tuple[float, float, float, float, float]] = MappingProxyType(
    {
        # PHYSICAL
        "speed": (8, 6, 2, -4, -10),
        "acceleration": (8, 6, 2, -4, -10),
        "lateral_quickness": (8, 5, 1, -4, -9),
        "strength": (-12, -7, 0, 8, 13),
        "vertical": (0, 3, 3, 1, -3),
        "stamina": (2, 1, 0, -1, -2),
        "durability": (1, 1, 0, -1, -2),
        # CREATION
        "handle": (14, 6, 0, -8, -16),
        "ball_security": (5, 2, 0, -3, -6),
        "first_step": (10, 6, 1, -6, -12),
        "change_of_direction": (11, 6, 1, -7, -13),
        "screen_use": (9, 4, 0, -5, -9),
        "draw_foul": (4, 2, 0, -2, -4),
        # FINISHING
        "finishing_rim": (-4, -2, 1, 4, 7),
        "contact_finishing": (-6, -3, 1, 5, 8),
        "floater": (8, 4, 0, -4, -9),
        "post_scoring": (-14, -10, -2, 9, 17),
        # SHOOTING
        "catch_shoot_3": (6, 11, 6, -1, -15),
        "off_dribble_3": (9, 10, 2, -6, -16),
        "catch_shoot_mid": (4, 7, 3, 0, -8),
        "off_dribble_mid": (7, 9, 2, -4, -12),
        "free_throw": (8, 7, 2, -3, -13),
        # PLAYMAKING
        "passing_vision": (14, 4, 1, -6, -10),
        "passing_accuracy": (13, 4, 1, -6, -10),
        "pass_under_pressure": (12, 4, 0, -6, -10),
        "short_roll_passing": (-4, -4, 0, 5, 8),
        # SCREENING
        "screen_setting": (-13, -9, -2, 9, 15),
        "screen_timing": (-11, -7, -1, 8, 13),
        "roll_ability": (-12, -9, -2, 9, 15),
        "catch_hands": (-6, -4, 0, 5, 8),
        # OFF_BALL
        "off_ball_movement": (0, 7, 6, -3, -8),
        "relocation": (2, 8, 6, -4, -10),
        "cutting": (-2, 4, 6, 2, -6),
        "offensive_rebounding": (-12, -8, -1, 8, 15),
        # ON_BALL_DEF
        "on_ball_defense": (5, 4, 2, -3, -7),
        "hands": (6, 4, 1, -3, -6),
        "post_defense": (-13, -9, -1, 8, 15),
        "foul_discipline": (3, 2, 0, -2, -5),
        # TEAM_DEF
        "screen_navigation": (7, 6, 2, -5, -10),
        "help_iq": (-1, 0, 1, 2, 3),
        "rotation_speed": (4, 4, 2, -3, -7),
        "rim_protection": (-16, -12, -3, 9, 19),
        "closeout_speed": (6, 5, 2, -4, -9),
        "closeout_control": (5, 4, 2, -3, -8),
        "scheme_discipline": (0, 0, 0, 1, 1),
        "switchability": (2, 4, 6, 1, -8),
        "defensive_rebounding": (-13, -8, 0, 9, 16),
        # MENTAL
        "basketball_iq": (4, 0, 0, -1, -2),
        "composure": (2, 0, 0, 0, 0),
        "consistency": (0, 0, 0, 0, 0),
        "motor": (0, 0, 1, 1, 1),
        "coachability": (0, 0, 0, 0, 0),
        # DEVELOPMENT
        "potential": (0, 0, 0, 0, 0),
        "potential_variance": (0, 0, 0, 0, 0),
        "work_ethic": (0, 0, 0, 0, 0),
    }
)
assert set(_RAW_POSITION_OFFSETS) == set(RATING_FIELD_ORDER)


def _double_center(
    raw: Mapping[str, tuple[float, float, float, float, float]],
) -> tuple[Mapping[Position, Mapping[str, float]], Mapping[Position, float]]:
    """§6.3's two-step pass, applied once at import.

    Step 1: subtract each field's own mean-over-5-positions from every
    cell -- makes every field's column sum to 0 (I1), and leaves every
    `POSITION_NEUTRAL_FIELDS` cell at exactly 0 (its raw column is already
    all-zero, so its mean is 0).

    Step 2: for each position, the row sum over all 55 (now column-
    centered) fields, divided by 50, is subtracted from the 50 carrier
    fields only (I2, I3) -- returned alongside the centered table as
    `_ROW_CORRECTION` (the `r_pos/50` row §6.3's two-row table names
    explicitly), so the verify test can assert the exact value a builder
    computes without re-deriving it independently.
    """
    column_centered: dict[str, dict[Position, float]] = {}
    for field_name, values in raw.items():
        mean = sum(values) / len(values)
        column_centered[field_name] = {
            pos: values[i] - mean for i, pos in enumerate(_POSITIONS)
        }

    row_sum: dict[Position, float] = {
        pos: sum(column_centered[f][pos] for f in RATING_FIELD_ORDER) for pos in _POSITIONS
    }
    row_correction: dict[Position, float] = {
        pos: row_sum[pos] / len(_CARRIER_FIELDS) for pos in _POSITIONS
    }

    centered: dict[Position, dict[str, float]] = {pos: {} for pos in _POSITIONS}
    for field_name in RATING_FIELD_ORDER:
        is_carrier = field_name not in POSITION_NEUTRAL_FIELDS
        for pos in _POSITIONS:
            value = column_centered[field_name][pos]
            if is_carrier:
                value -= row_correction[pos]
            centered[pos][field_name] = value

    return (
        MappingProxyType({pos: MappingProxyType(centered[pos]) for pos in _POSITIONS}),
        MappingProxyType(row_correction),
    )


POSITION_RATING_OFFSETS: Mapping[Position, Mapping[str, float]]
_ROW_CORRECTION: Mapping[Position, float]
POSITION_RATING_OFFSETS, _ROW_CORRECTION = _double_center(_RAW_POSITION_OFFSETS)


def _offset_column(field_name: str) -> Mapping[Position, float]:
    return {pos: POSITION_RATING_OFFSETS[pos][field_name] for pos in _POSITIONS}


# ---------------------------------------------------------------------------
# §5.1 -- roster tiers (Decision S7-2) and §6.7's `TALENT_VARIANCE`, the
# between-tier + within-tier + team-offset variance of the mean-of-55
# statistic, computed from the tier table below rather than typed.
# ---------------------------------------------------------------------------


class RosterTier(StrEnum):
    STAR = "star"
    STARTER = "starter"
    ROTATION = "rotation"
    BENCH = "bench"


TIER_BY_SLOT: tuple[RosterTier, ...] = (
    RosterTier.STAR,
    *([RosterTier.STARTER] * 4),
    *([RosterTier.ROTATION] * 3),
    *([RosterTier.BENCH] * 6),
)
assert len(TIER_BY_SLOT) == 14


@dataclass(frozen=True)
class _TierTalent:
    mean: float
    sd: float


_TIER_TALENT: Mapping[RosterTier, _TierTalent] = MappingProxyType(
    {
        RosterTier.STAR: _TierTalent(84.0, 4.0),
        RosterTier.STARTER: _TierTalent(76.0, 3.0),
        RosterTier.ROTATION: _TierTalent(70.0, 3.0),
        RosterTier.BENCH: _TierTalent(63.0, 3.0),
    }
)

_TEAM_OFFSET_SD = 2.5
_TALENT_CLAMP: tuple[float, float] = (40.0, 97.0)

_GROUP_NOISE_SD = 6.0
_FIELD_NOISE_SD = 4.0
GROUP_NOISE_VARIANCE = _GROUP_NOISE_SD**2  # 36.0
FIELD_NOISE_VARIANCE = _FIELD_NOISE_SD**2  # 16.0


def _talent_variance() -> float:
    counts: Mapping[RosterTier, int] = {tier: TIER_BY_SLOT.count(tier) for tier in RosterTier}
    total = sum(counts.values())
    weighted_mean = sum(counts[t] * _TIER_TALENT[t].mean for t in counts) / total
    between = sum(
        counts[t] * (_TIER_TALENT[t].mean - weighted_mean) ** 2 for t in counts
    ) / total
    within = sum(counts[t] * _TIER_TALENT[t].sd**2 for t in counts) / total
    return between + within + _TEAM_OFFSET_SD**2


TALENT_VARIANCE: float = _talent_variance()


# ---------------------------------------------------------------------------
# §6.7 -- the variance model. `Vp(a, b)` is the across-position covariance
# of two centered offset columns; `expected_group_sd` is what §6.6's
# per-group SD band asserts against.
# ---------------------------------------------------------------------------


def _vp(a: Mapping[Position, float], b: Mapping[Position, float]) -> float:
    return sum(a[pos] * b[pos] for pos in _POSITIONS) / len(_POSITIONS)


def _group_mean_offset_column(group: str) -> Mapping[Position, float]:
    fields = RATING_GROUPS[group]
    return {
        pos: sum(POSITION_RATING_OFFSETS[pos][f] for f in fields) / len(fields)
        for pos in _POSITIONS
    }


def expected_group_sd(group: str) -> float:
    """§6.7's closed-form model of a rating group's league-wide per-player
    SD -- what §6.6's per-group SD band (±1.5) asserts realized output
    against. `shrink(n_g)` accounts for §6.5's repair pass subtracting the
    mean-of-55 noise from every field it touches (derivation in §6.7)."""
    fields = RATING_GROUPS[group]
    n_g = len(fields)
    mean_col = _group_mean_offset_column(group)
    vp_group = _vp(mean_col, mean_col)
    shrink = (72 * n_g + 32) / 55 - 3.920
    variance = (
        TALENT_VARIANCE + vp_group + GROUP_NOISE_VARIANCE + FIELD_NOISE_VARIANCE / n_g - shrink
    )
    return math.sqrt(variance)


# ---------------------------------------------------------------------------
# §5.2 -- position assignment (Decision S7-7)
# ---------------------------------------------------------------------------

_STAR_POSITION_WEIGHTS: Mapping[Position, float] = MappingProxyType(
    {
        Position.PG: 0.22,
        Position.SG: 0.20,
        Position.SF: 0.22,
        Position.PF: 0.20,
        Position.C: 0.16,
    }
)


def synthetic_positions(rng: Random) -> tuple[Position, ...]:
    """A roster's 14 positions, slot 1 = the star (§5.1's tier/slot
    mapping), per §5.2. 3/3/3/3/2, with the short position drawn
    uniformly and the star's position drawn from the guard-skewed weights
    above. No renormalization step (revision 1's dead branch, deleted --
    §5.2's own reasoning: the short position always still holds 2
    entries after the star is removed, so all 5 positions remain
    representable)."""
    short = rng.choice(_POSITIONS)
    multiset: list[Position] = []
    for pos in _POSITIONS:
        multiset.extend([pos] * (2 if pos is short else 3))

    star_position = _weighted_choice(rng, _STAR_POSITION_WEIGHTS)
    multiset.remove(star_position)

    rng.shuffle(multiset)
    result = (star_position, *multiset)
    assert len(result) == 14
    return result


# ---------------------------------------------------------------------------
# §5.3 -- physicals (Decision S7-8)
# ---------------------------------------------------------------------------

_HEIGHT_BY_POSITION: Mapping[Position, tuple[float, float]] = MappingProxyType(
    {
        Position.PG: (74.5, 1.6),
        Position.SG: (77.0, 1.5),
        Position.SF: (79.0, 1.5),
        Position.PF: (81.0, 1.5),
        Position.C: (83.0, 1.5),
    }
)


@dataclass(frozen=True)
class Physicals:
    height_in: int
    wingspan_in: int
    weight_lb: int


def synthetic_physicals(position: Position, rng: Random) -> Physicals:
    """Per §5.3, drawn once per player. `wingspan_in` and `weight_lb` are
    both conditioned on the *drawn* `height_in`, not on the position mean,
    so a short-for-position player also gets a short-for-them wingspan and
    a lighter weight rather than the two being independently noisy."""
    mu, sd = _HEIGHT_BY_POSITION[position]
    height = int(_clamp(round(rng.gauss(mu, sd)), 70, 88))
    wingspan = int(_clamp(round(height + rng.gauss(3.5, 1.5)), height - 1, height + 9))
    weight = int(_clamp(round(6.67 * height - 303.6 + rng.gauss(0.0, 12.0)), 160, 300))
    return Physicals(height_in=height, wingspan_in=wingspan, weight_lb=weight)


# ---------------------------------------------------------------------------
# §5.1 / §6.2 / §6.5 -- tier talent, the 55 ratings, and the clamp repair
# ---------------------------------------------------------------------------


def tier_talent(tier: RosterTier, team_offset: float, rng: Random) -> float:
    """One player's latent talent scalar `T` (§5.1): a tier draw plus the
    team's own once-per-team offset, clamped to `[40, 97]`."""
    spec = _TIER_TALENT[tier]
    draw = rng.gauss(spec.mean, spec.sd)
    return _clamp(draw + team_offset, *_TALENT_CLAMP)


def synthetic_ratings(talent: float, position: Position, rng: Random) -> dict[str, int]:
    """The 55 rating fields for one player (§6.2, §6.5) -- exactly
    `RATING_FIELD_ORDER`'s key set, no age columns (§5.4's ownership
    split: `tools/seed_dev_league.py` merges `peak_age_start`/
    `peak_age_end` in separately).

    Draw order (§6.2): 11 group noises in `RATING_GROUPS` declaration
    order, then 55 field noises in `RATING_FIELD_ORDER` -- fixed so "same
    league seed -> same league" survives a future field addition without
    reshuffling every existing player's values.
    """
    offsets = POSITION_RATING_OFFSETS[position]

    group_noise: dict[str, float] = {
        group: rng.gauss(0.0, _GROUP_NOISE_SD) for group in RATING_GROUPS
    }

    raw: dict[str, float] = {}
    for field_name in RATING_FIELD_ORDER:
        field_noise = rng.gauss(0.0, _FIELD_NOISE_SD)
        raw[field_name] = (
            talent + offsets[field_name] + group_noise[_FIELD_TO_GROUP[field_name]] + field_noise
        )

    ratings = {
        field_name: int(_clamp(round(value), 25, 99)) for field_name, value in raw.items()
    }
    _repair_clamp(ratings, talent)

    assert set(ratings) == set(RATING_FIELD_ORDER), (
        "synthetic_ratings must return exactly RATING_FIELD_ORDER's key set -- "
        "no age column reachable by the repair loop"
    )
    return ratings


def _repair_clamp(ratings: dict[str, int], talent: float) -> None:
    """§6.5's deterministic repair pass, in place. Restores
    `mean-of-55 == round(talent)` (within 0.5) after the `[25, 99]` clamp
    has potentially compressed an extreme-profile player's mean below
    (or above) their tier target.

    Iteration starts at index `target % 55` (not always 0) so a clipped
    extreme profile's redistributed mass doesn't systematically land on
    the earliest-ordered fields (§6.5's NIT-4 fix) -- and re-checks
    movability every step, since a field that was movable can stop being
    so mid-pass.
    """
    n = len(RATING_FIELD_ORDER)
    target = int(_clamp(round(talent), 25, 99))

    def mean_of_55() -> float:
        return sum(ratings.values()) / n

    d = target - mean_of_55()
    if abs(d) <= 0.5:
        return

    sign = 1 if d > 0 else -1
    start = target % n
    idx = start
    moved_since_cycle_start = False
    steps = 0
    max_steps = n * 74  # §13's stated worst-case bound

    while abs(target - mean_of_55()) > 0.5 and steps < max_steps:
        field_name = RATING_FIELD_ORDER[idx]
        value = ratings[field_name]
        movable = value < 99 if sign > 0 else value > 25
        if movable:
            ratings[field_name] = value + sign
            moved_since_cycle_start = True

        idx = (idx + 1) % n
        steps += 1
        if idx == start:
            if not moved_since_cycle_start:
                break  # no movable field remains anywhere in the table
            moved_since_cycle_start = False

    assert abs(target - mean_of_55()) <= 0.5, (
        f"clamp repair failed to converge: target={target}, mean={mean_of_55()}"
    )


# ---------------------------------------------------------------------------
# §8 -- derive_tendencies
# ---------------------------------------------------------------------------

_TENDENCY_NON_FIELDS = frozenset({"player_id", "league_id", "updated_at"})
_TENDENCY_FIELD_NAMES: frozenset[str] = frozenset(
    f.name for f in dataclasses.fields(PlayerTendencies) if f.name not in _TENDENCY_NON_FIELDS
)
assert len(_TENDENCY_FIELD_NAMES) == 17


def _z(rating_value: float) -> float:
    """§4's `z(f)`."""
    return _clamp((rating_value - 70.0) / 12.0, -2.5, 2.5)


def _talent_z(mean_of_55: float) -> float:
    """§4's `talent_z`. Computed locally from the plain mean of the 55
    fields, deliberately *not* via `sim.ratings.compute_overall` -- §4's
    own reasoning: step 12 item 9 replaces `compute_overall`'s body with a
    position-weighted formula, and derived tendencies must not silently
    change when that lands."""
    return _clamp((mean_of_55 - 70.0) / 8.0, -3.0, 3.0)


# --- §8.2: shot-frequency affinities -----------------------------------

_SHOT_BASE: Mapping[str, float] = MappingProxyType(
    {"rim": 33.0, "mid": 20.0, "three": 39.0, "post": 8.0}
)
_SHOT_K = 0.55
_SHOT_ZONE_ORDER: tuple[str, ...] = ("rim", "mid", "three", "post")

_SHOT_ZONE_WEIGHTS: Mapping[str, tuple[tuple[float, str], ...]] = MappingProxyType(
    {
        "rim": (
            (0.45, "finishing_rim"),
            (0.25, "contact_finishing"),
            (0.20, "first_step"),
            (0.10, "vertical"),
        ),
        "mid": ((0.50, "catch_shoot_mid"), (0.30, "off_dribble_mid"), (0.20, "floater")),
        "three": ((0.55, "catch_shoot_3"), (0.35, "off_dribble_3"), (0.10, "relocation")),
        "post": ((0.60, "post_scoring"), (0.25, "strength"), (0.15, "screen_setting")),
    }
)


def _shot_affinities(ratings: PlayerRatings, rng: Random | None) -> dict[str, float]:
    affinities: dict[str, float] = {}
    for zone in _SHOT_ZONE_ORDER:
        combo = sum(
            weight * _z(getattr(ratings, field_name))
            for weight, field_name in _SHOT_ZONE_WEIGHTS[zone]
        )
        noise = rng.gauss(0.0, 0.10) if rng is not None else 0.0
        affinities[zone] = _SHOT_BASE[zone] * math.exp(_SHOT_K * combo + noise)
    return affinities


def _apportion_shot_freqs(affinities: Mapping[str, float]) -> dict[str, int]:
    """§8.3's sum-100 algorithm: normalize, largest-remainder apportion the
    integer deficit, then a sum-preserving floor-2 repair. No RNG anywhere
    in the rounding -- deterministic given `affinities`."""
    order = _SHOT_ZONE_ORDER
    total = sum(affinities[z] for z in order)
    x = {z: 100.0 * affinities[z] / total for z in order}
    base = {z: math.floor(x[z]) for z in order}
    remainder = {z: x[z] - base[z] for z in order}
    deficit = 100 - int(sum(base.values()))

    ranked = sorted(order, key=lambda z: (-remainder[z], order.index(z)))
    value = dict(base)
    for zone in ranked[:deficit]:
        value[zone] += 1
    value = {z: int(v) for z, v in value.items()}

    while min(value.values()) < 2:
        z_low = min(order, key=lambda z: value[z])
        need = 2 - value[z_low]
        z_high = max(order, key=lambda z: value[z])
        value[z_low] += need
        value[z_high] -= need

    assert sum(value.values()) == 100
    assert all(2 <= v <= 94 for v in value.values())
    return value


# --- §8.4: the other 13 tendencies --------------------------------------


@dataclass(frozen=True)
class _ZTerm:
    """One weighted z-term of a tendency's `combo`. `other_field` set means
    a *difference* term `z(field) - z(other_field)` (`pullup_vs_catch`'s
    shape); `sign = -1.0` means the term is authored as `-field`
    (`help_gamble_freq`'s `-scheme_discipline` etc)."""

    weight: float
    field_name: str
    sign: float = 1.0
    other_field: str | None = None

    def value(self, ratings: PlayerRatings) -> float:
        v = _z(getattr(ratings, self.field_name))
        if self.other_field is not None:
            v -= _z(getattr(ratings, self.other_field))
        return self.sign * self.weight * v

    def offset_contribution(self, position: Position) -> float:
        off = POSITION_RATING_OFFSETS[position][self.field_name]
        if self.other_field is not None:
            off -= POSITION_RATING_OFFSETS[position][self.other_field]
        return self.sign * self.weight * off / 12.0

    def g(self) -> dict[str, float]:
        """§6.7/§8.4's `g[field]`: this term's signed weight/12
        contribution to each rating field it reads."""
        result = {self.field_name: self.sign * self.weight / 12.0}
        if self.other_field is not None:
            existing = result.get(self.other_field, 0.0)
            result[self.other_field] = existing - self.sign * self.weight / 12.0
        return result


@dataclass(frozen=True)
class _TalentTerm:
    weight: float

    def value(self, talent_z: float) -> float:
        return self.weight * talent_z

    def t(self) -> float:
        return self.weight / 8.0


@dataclass(frozen=True)
class _TendencyFormula:
    base: float
    z_terms: tuple[_ZTerm, ...]
    band: tuple[float, float]
    desired: Mapping[Position, float]
    talent_term: _TalentTerm | None = None


def _desired(pg: float, sg: float, sf: float, pf: float, c: float) -> Mapping[Position, float]:
    values = {Position.PG: pg, Position.SG: sg, Position.SF: sf, Position.PF: pf, Position.C: c}
    assert abs(sum(values.values())) < 1e-9
    return MappingProxyType(values)


# Declaration order is the module's canonical order for these 13 fields
# (§8.1's draw order: shot affinities first, then these 13 in this order).
_TENDENCY_FORMULAS: Mapping[str, _TendencyFormula] = MappingProxyType(
    {
        "drive_freq": _TendencyFormula(
            base=45.0,
            z_terms=(
                _ZTerm(0.35, "first_step"),
                _ZTerm(0.25, "handle"),
                _ZTerm(0.20, "change_of_direction"),
                _ZTerm(0.20, "finishing_rim"),
            ),
            band=(10.0, 90.0),
            desired=_desired(7, 3, 0, -4, -6),
        ),
        "pullup_vs_catch": _TendencyFormula(
            base=40.0,
            z_terms=(
                _ZTerm(0.45, "off_dribble_3", other_field="catch_shoot_3"),
                _ZTerm(0.25, "off_dribble_mid", other_field="catch_shoot_mid"),
                _ZTerm(0.20, "handle"),
                _ZTerm(0.10, "first_step"),
            ),
            band=(5.0, 90.0),
            desired=_desired(8, 4, 0, -4, -8),
        ),
        "iso_freq": _TendencyFormula(
            base=22.0,
            z_terms=(
                _ZTerm(0.30, "handle"),
                _ZTerm(0.25, "off_dribble_mid"),
                _ZTerm(0.20, "off_dribble_3"),
            ),
            talent_term=_TalentTerm(0.25),
            band=(2.0, 70.0),
            desired=_desired(2, 4, 2, -3, -5),
        ),
        "pnr_ball_freq": _TendencyFormula(
            base=35.0,
            z_terms=(
                _ZTerm(0.35, "handle"),
                _ZTerm(0.25, "pass_under_pressure"),
                _ZTerm(0.20, "screen_use"),
                _ZTerm(0.20, "passing_vision"),
            ),
            band=(3.0, 85.0),
            desired=_desired(12, 5, -1, -6, -10),
        ),
        "pnr_screen_freq": _TendencyFormula(
            base=30.0,
            z_terms=(
                _ZTerm(0.40, "screen_setting"),
                _ZTerm(0.30, "roll_ability"),
                _ZTerm(0.20, "screen_timing"),
                _ZTerm(0.10, "strength"),
            ),
            band=(2.0, 90.0),
            desired=_desired(-12, -8, -2, 8, 14),
        ),
        "transition_freq": _TendencyFormula(
            base=30.0,
            z_terms=(
                _ZTerm(0.40, "speed"),
                _ZTerm(0.25, "acceleration"),
                _ZTerm(0.20, "motor"),
                _ZTerm(0.15, "stamina"),
            ),
            band=(5.0, 80.0),
            desired=_desired(4, 3, 1, -3, -5),
        ),
        "pass_freq": _TendencyFormula(
            base=45.0,
            z_terms=(
                _ZTerm(0.40, "passing_vision"),
                _ZTerm(0.30, "passing_accuracy"),
                _ZTerm(0.20, "basketball_iq"),
                _ZTerm(0.10, "pass_under_pressure"),
            ),
            band=(10.0, 90.0),
            desired=_desired(8, 1, 0, -3, -6),
        ),
        "spot_up_freq": _TendencyFormula(
            base=45.0,
            z_terms=(
                _ZTerm(0.45, "catch_shoot_3"),
                _ZTerm(0.30, "relocation"),
                _ZTerm(0.15, "catch_shoot_mid"),
                _ZTerm(0.10, "off_ball_movement"),
            ),
            band=(5.0, 90.0),
            desired=_desired(-2, 5, 4, -1, -6),
        ),
        "cut_freq": _TendencyFormula(
            base=30.0,
            z_terms=(
                _ZTerm(0.45, "cutting"),
                _ZTerm(0.30, "off_ball_movement"),
                _ZTerm(0.15, "vertical"),
                _ZTerm(0.10, "motor"),
            ),
            band=(3.0, 80.0),
            desired=_desired(-6, 0, 4, 3, -1),
        ),
        "crash_oreb_freq": _TendencyFormula(
            base=30.0,
            z_terms=(
                _ZTerm(0.50, "offensive_rebounding"),
                _ZTerm(0.30, "motor"),
                _ZTerm(0.20, "vertical"),
            ),
            band=(2.0, 90.0),
            desired=_desired(-10, -6, 0, 6, 10),
        ),
        "help_gamble_freq": _TendencyFormula(
            base=35.0,
            z_terms=(
                _ZTerm(0.55, "scheme_discipline", sign=-1.0),
                _ZTerm(0.25, "rotation_speed"),
                _ZTerm(0.20, "basketball_iq", sign=-1.0),
            ),
            band=(5.0, 85.0),
            desired=_desired(0, 0, 0, 0, 0),
        ),
        "steal_gamble_freq": _TendencyFormula(
            base=35.0,
            z_terms=(
                _ZTerm(0.45, "hands"),
                _ZTerm(0.30, "scheme_discipline", sign=-1.0),
                _ZTerm(0.25, "lateral_quickness"),
            ),
            band=(5.0, 85.0),
            desired=_desired(4, 3, 1, -3, -5),
        ),
        "foul_freq": _TendencyFormula(
            base=40.0,
            z_terms=(
                _ZTerm(0.60, "foul_discipline", sign=-1.0),
                _ZTerm(0.25, "lateral_quickness", sign=-1.0),
                _ZTerm(0.15, "rim_protection"),
            ),
            band=(5.0, 90.0),
            desired=_desired(-5, -3, 0, 3, 5),
        ),
    }
)

_SCALAR_TENDENCY_ORDER: tuple[str, ...] = tuple(_TENDENCY_FORMULAS)

TENDENCY_TARGET_SD = 13.5
_TENDENCY_NOISE_VARIANCE = 16.0  # Normal(0, 4.0)^2


def _vp_desired(desired: Mapping[Position, float]) -> float:
    return sum(desired[pos] ** 2 for pos in _POSITIONS) / len(_POSITIONS)


def _combined_g(formula: _TendencyFormula) -> dict[str, float]:
    g: dict[str, float] = {}
    for term in formula.z_terms:
        for field_name, coeff in term.g().items():
            g[field_name] = g.get(field_name, 0.0) + coeff
    return g


def _var_resid(formula: _TendencyFormula) -> float:
    """§8.4's `Var_resid[f]` -- the variance the rating-driven `combo`
    term contributes, decomposed into talent/group/field noise exactly
    the way `expected_group_sd` decomposes a rating group's variance."""
    g = _combined_g(formula)
    t = formula.talent_term.t() if formula.talent_term is not None else 0.0
    talent_component = TALENT_VARIANCE * (sum(g.values()) + t) ** 2

    group_sums: dict[str, float] = {}
    for field_name, coeff in g.items():
        group = _FIELD_TO_GROUP[field_name]
        group_sums[group] = group_sums.get(group, 0.0) + coeff
    group_component = GROUP_NOISE_VARIANCE * sum(v**2 for v in group_sums.values())

    field_component = FIELD_NOISE_VARIANCE * sum(v**2 for v in g.values())
    return talent_component + group_component + field_component


def _solve_spread(formula: _TendencyFormula) -> float:
    vp = _vp_desired(formula.desired)
    var_resid = _var_resid(formula)
    numerator = TENDENCY_TARGET_SD**2 - _TENDENCY_NOISE_VARIANCE - vp
    return math.sqrt(numerator) / math.sqrt(var_resid)


def _implied(formula: _TendencyFormula, spread: float, position: Position) -> float:
    return spread * sum(term.offset_contribution(position) for term in formula.z_terms)


def _solve_pos_row(formula: _TendencyFormula, spread: float) -> Mapping[Position, float]:
    raw = {pos: formula.desired[pos] - _implied(formula, spread, pos) for pos in _POSITIONS}
    mean = sum(raw.values()) / len(raw)
    return MappingProxyType({pos: raw[pos] - mean for pos in _POSITIONS})


TENDENCY_SPREADS: Mapping[str, float] = MappingProxyType(
    {name: _solve_spread(formula) for name, formula in _TENDENCY_FORMULAS.items()}
)
TENDENCY_POSITION_ROWS: Mapping[str, Mapping[Position, float]] = MappingProxyType(
    {
        name: _solve_pos_row(formula, TENDENCY_SPREADS[name])
        for name, formula in _TENDENCY_FORMULAS.items()
    }
)


def derive_tendencies(
    ratings: PlayerRatings, position: Position, *, rng: Random | None = None
) -> dict[str, int]:
    """Pure (Decision S7-9): `rng=None` means zero noise and fully
    deterministic output, which is what makes every formula in §8
    assertable directly. `rng` supplied applies Decision P1-19 item 2's
    "plus seeded noise" -- the caller supplies a generation-stream RNG
    (`rng_for(seed, "tendencies", player_id)`), never a bare `Random()`.

    Returns exactly the 17 `PlayerTendencies` field names (minus
    `player_id`/`league_id`/`updated_at`), so
    `ratings_repo.upsert_player_tendencies(**values)` can take the result
    directly.
    """
    mean_of_55 = sum(getattr(ratings, f) for f in RATING_FIELD_ORDER) / len(RATING_FIELD_ORDER)
    talent_z = _talent_z(mean_of_55)

    affinities = _shot_affinities(ratings, rng)
    shot_freqs = _apportion_shot_freqs(affinities)

    result: dict[str, int] = {
        "shot_freq_rim": shot_freqs["rim"],
        "shot_freq_mid": shot_freqs["mid"],
        "shot_freq_three": shot_freqs["three"],
        "shot_freq_post": shot_freqs["post"],
    }

    for name in _SCALAR_TENDENCY_ORDER:
        formula = _TENDENCY_FORMULAS[name]
        combo = sum(term.value(ratings) for term in formula.z_terms)
        if formula.talent_term is not None:
            combo += formula.talent_term.value(talent_z)
        noise = rng.gauss(0.0, 4.0) if rng is not None else 0.0
        raw = (
            formula.base
            + TENDENCY_SPREADS[name] * combo
            + TENDENCY_POSITION_ROWS[name][position]
            + noise
        )
        lo, hi = formula.band
        result[name] = int(round(_clamp(raw, lo, hi)))

    assert set(result) == _TENDENCY_FIELD_NAMES
    return result
