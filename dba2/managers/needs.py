"""The positional need matrix -- 5 positions x 4 quality tiers, derived
entirely from a roster, plus `need_bonus` (docs/design/phase-2-manager-
minds.md §5, §12 step 6, Decisions P2-8, P2-28, P2-9, P2-10).

**The need-target table is this module's own local data**, per `tuning.
py`'s own scope note ("a constant that is genuinely local to one module's
own data table ... the need-target table ... stays declared in the module
that owns it"). `NEED_GAIN`/`GLUT_LOSS`/`GLUT_SLACK` are the three §5
constants that *are* shared (they also feed §9.2's trade evaluation, step
8), so those three alone live in `managers/tuning.py::GmTuning`, already
shipped at step 1 -- this module reads them, never redeclares them.

**Tier thresholds.** §5's four quality tiers (STAR/STARTER/ROTATION/DEPTH)
are a *different* four-tier concept from `sim.derivation.RosterTier`
(STAR/STARTER/ROTATION/BENCH) -- the same name for two different things
would be worse than two different names, so this module's tiers get their
own `NeedTier` enum rather than reusing `RosterTier`. The cut points
(80/73/66) are the *values*, computed once by hand from `sim.derivation.
_TIER_TALENT`'s four tier means (84, 76, 70, 63) at their midpoints
(82, 73, 66.5, rounded to the nearest whole overall a real roster can
have) -- not imported at runtime: `_TIER_TALENT` is `derivation.py`'s own
private module constant (leading underscore, never imported by any other
module in this codebase), and `RosterTier`'s tier *sizes* (1/4/3/6 of 14
roster slots) answer a different question (what fraction of a roster sits
in each tier) than this module's tiers do (what overall a player has to
clear to count toward a given quality bar). Re-deriving the cutoffs here
as this module's own literal data, the same way `picks.py` declares its
own slot-anchor table rather than importing `valuation.py`'s age-curve
anchors, keeps the two tier concepts from silently coupling if either
one's table is ever retuned independently.

**Decision P2-9 -- where `fit_value` lives.** This step's own task
description is explicit that `window.py` (step 4) is the precedent to
check before deciding, and `window.py`'s own §12 step 4 scope is narrow
by design: it delivers the *classifier* only, and Decision P2-13's
`Timeline` map is included solely because §7 names it as step 4's own
deliverable -- nothing in step 4 reaches ahead into a later step's
formula. §5 states `fit_value`'s formula in the same breath as `need_
bonus`'s, but Decision P2-9's own text draws the contrast between `market_
value` (team-agnostic) and `fit_value` (team-specific) to explain *why
`needs.py` exists*, not to hand `fit_value` a home. `fit_value` multiplies
a step-3 output (`market_value`) by a clamp of this step's output (`need_
bonus`) and is actually consumed by `decisions/trade.py`, §12 step 8's
deliverable -- building it here would mean either importing `valuation.py`
into `needs.py` for a formula with no caller yet, or leaving it unused
dead code until step 8. This module delivers exactly `NeedMatrix` (the
need/glut/supply grid) and `need_bonus` (the raw, unclamped VP-share
formula) plus `NEED_BONUS_CLAMP_LO`/`_HI` and `clamp_need_bonus` (the one
clamp implementation `fit_value` will wrap `market_value(...) * (1 +
clamp_need_bonus(need_bonus(...)))` around at step 8) -- so step 8 never
re-derives the -0.35/+0.50 bounds a second time, the same "one
implementation, not two" discipline Decision P2-33 exists to enforce
elsewhere in this package.

**`ovr` is an explicit parameter, not derived internally**, mirroring
`valuation.py::market_value`'s own documented reason: whichever `ovr`
`fit_value`'s caller used for `market_value` (raw `overall_cached`, or
`effective_talent_ovr`'s `defense_first` tilt) is the same `ovr` that must
select `need_bonus`'s tier column, and this module has no way to verify
that consistency on its own if it re-derived one internally.

**`build_need_matrix` reads `overall_cached` directly, never a tilted
value**, for the same reason `window.py::team_strength` does (§7, Decision
P2-3): a team's own need matrix is a fact about its roster, not about
which of the seven philosophies happens to be asking, so it takes no
`PhilosophyProfile` argument at all.

Zero I/O, per Decision P2-2's purity extension: no `dba2.data`, no
`datetime.now()`, no bare `random.Random()`, no `open()`, no
`os.environ`. Imports `domain` (`Position`), `managers.philosophy`
(`PhilosophyProfile`, for `fit_sens`), `managers.snapshot` (`RosterPlayer`,
`TeamSnapshot`), and `managers.tuning` only -- deliberately does not
import `managers.valuation`, for the reason stated above.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from dba2.domain.player import Position
from dba2.managers.philosophy import PhilosophyProfile
from dba2.managers.snapshot import RosterPlayer, TeamSnapshot
from dba2.managers.tuning import DEFAULT_TUNING, GmTuning


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# §5 -- the five positions, PG..C, and adjacency along that line
# ---------------------------------------------------------------------------

_POSITIONS: tuple[Position, ...] = (
    Position.PG, Position.SG, Position.SF, Position.PF, Position.C,
)


def _adjacent(position: Position) -> tuple[Position, ...]:
    """The position(s) at index distance exactly 1 from `position` on the
    PG-SG-SF-PF-C line -- one neighbor for the two end positions (PG, C),
    two for the three middle ones."""
    idx = _POSITIONS.index(position)
    neighbors = []
    if idx > 0:
        neighbors.append(_POSITIONS[idx - 1])
    if idx < len(_POSITIONS) - 1:
        neighbors.append(_POSITIONS[idx + 1])
    return tuple(neighbors)


# ---------------------------------------------------------------------------
# Decision P2-28 -- position coverage, normalized to sum 1.0
# ---------------------------------------------------------------------------


def position_coverage(roster_player: RosterPlayer) -> Mapping[Position, float]:
    """Decision P2-28: raw weight 1.00 to `primary_position`, 0.50 to
    `secondary_position`, 0.25 to each position adjacent (index distance
    exactly 1) to either one and **not already counted** -- read here as
    "a position already assigned a weight by the primary/secondary rule
    does not additionally collect an adjacency bump, and a position
    adjacent to both primary and secondary collects the 0.25 only once,"
    the reading that reproduces §5's own worked example exactly (a SG with
    no secondary: raw PG 0.25, SG 1.00, SF 0.25 -> normalized 0.167,
    0.667, 0.167). Then divided by the row total so `sum(coverage.
    values()) == 1.0` always -- `primary_position` alone guarantees a
    nonzero total, so this never divides by zero."""
    player = roster_player.player
    primary = player.primary_position
    secondary = player.secondary_position
    adjacent_to_primary = set(_adjacent(primary))
    adjacent_to_secondary = set(_adjacent(secondary)) if secondary is not None else set()

    raw: dict[Position, float] = {}
    for pos in _POSITIONS:
        if pos == primary:
            raw[pos] = 1.00
        elif secondary is not None and pos == secondary:
            raw[pos] = 0.50
        elif pos in adjacent_to_primary or pos in adjacent_to_secondary:
            raw[pos] = 0.25
        else:
            raw[pos] = 0.0

    total = sum(raw.values())
    return MappingProxyType({pos: raw[pos] / total for pos in _POSITIONS})


# ---------------------------------------------------------------------------
# Decision P2-8 -- the four quality tiers and their overall cutoffs
# ---------------------------------------------------------------------------


class NeedTier(StrEnum):
    """§5's four quality tiers, STAR (best) to DEPTH (worst) -- a distinct
    enum from `sim.derivation.RosterTier` (see module docstring)."""

    STAR = "star"
    STARTER = "starter"
    ROTATION = "rotation"
    DEPTH = "depth"


# Best-to-worst, the order the cumulative-downward supply sum (below)
# walks in.
_TIER_ORDER: tuple[NeedTier, ...] = (
    NeedTier.STAR, NeedTier.STARTER, NeedTier.ROTATION, NeedTier.DEPTH,
)

# §5's table, verbatim: STAR >= 80, STARTER 73-79, ROTATION 66-72, DEPTH <= 65.
_STAR_MIN_OVERALL = 80
_STARTER_MIN_OVERALL = 73
_ROTATION_MIN_OVERALL = 66


def tier_for_overall(overall: int) -> NeedTier:
    """§5's cutoff table, read top-down."""
    if overall >= _STAR_MIN_OVERALL:
        return NeedTier.STAR
    if overall >= _STARTER_MIN_OVERALL:
        return NeedTier.STARTER
    if overall >= _ROTATION_MIN_OVERALL:
        return NeedTier.ROTATION
    return NeedTier.DEPTH


# ---------------------------------------------------------------------------
# §5's target table (in bodies) -- the only place these eight numbers appear
# ---------------------------------------------------------------------------

_TARGET_NON_CENTER: tuple[float, float, float, float] = (0.40, 1.00, 1.60, 2.60)
"""PG/SG/SF/PF row, STAR..DEPTH, indexed by `_TIER_ORDER`."""

_TARGET_CENTER: tuple[float, float, float, float] = (0.30, 1.00, 1.40, 2.20)
"""C row, STAR..DEPTH, indexed by `_TIER_ORDER`."""


def _target(position: Position, tier: NeedTier) -> float:
    table = _TARGET_CENTER if position is Position.C else _TARGET_NON_CENTER
    return table[_TIER_ORDER.index(tier)]


# ---------------------------------------------------------------------------
# The matrix itself -- supply (cumulative downward), need, glut
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NeedMatrix:
    """One team's 5x4 need/glut grid (5 positions x 4 tiers), plus
    the `supply` it was derived from (kept alongside `need`/`glut` rather
    than discarded, the same "expose the intermediate the formula was
    built from" precedent `window.py::RawInputs` sets). Every leaf is a
    plain `float`, every outer/inner mapping is a `Position`/`NeedTier`
    keyed `MappingProxyType` covering exactly the 5/4 keys of its enum."""

    supply: Mapping[Position, Mapping[NeedTier, float]]
    need: Mapping[Position, Mapping[NeedTier, float]]
    glut: Mapping[Position, Mapping[NeedTier, float]]


def build_need_matrix(
    team_snapshot: TeamSnapshot, *, tuning: GmTuning = DEFAULT_TUNING
) -> NeedMatrix:
    """Decision P2-8's four-step derivation, from a `TeamSnapshot`'s
    roster alone (no philosophy, no window -- see module docstring):

    1. `position_coverage` per rostered player.
    2. `supply[pos][tier] = sum(coverage(p, pos))` over every player whose
       own `tier_for_overall(p.overall_cached)` is `tier` **or better**
       (STAR is best) -- cumulative downward, so a STAR player's coverage
       counts toward STARTER/ROTATION/DEPTH supply at that position too.
    3. `_target(pos, tier)`, the table above.
    4. `need = clamp((target - supply) / target, 0, 1)`; `glut =
       clamp((supply - target - GLUT_SLACK) / GLUT_SLACK, 0, 1)`.
    """
    coverages = [position_coverage(rp) for rp in team_snapshot.roster]
    tiers = [tier_for_overall(rp.player.overall_cached) for rp in team_snapshot.roster]
    tier_ranks = [_TIER_ORDER.index(t) for t in tiers]

    supply: dict[Position, dict[NeedTier, float]] = {pos: {} for pos in _POSITIONS}
    need: dict[Position, dict[NeedTier, float]] = {pos: {} for pos in _POSITIONS}
    glut: dict[Position, dict[NeedTier, float]] = {pos: {} for pos in _POSITIONS}

    for pos in _POSITIONS:
        for rank, tier in enumerate(_TIER_ORDER):
            s = sum(
                coverage[pos]
                for coverage, player_rank in zip(coverages, tier_ranks, strict=True)
                if player_rank <= rank
            )
            target = _target(pos, tier)
            supply[pos][tier] = s
            need[pos][tier] = _clamp((target - s) / target, 0.0, 1.0)
            glut[pos][tier] = _clamp((s - target - tuning.glut_slack) / tuning.glut_slack, 0.0, 1.0)

    return NeedMatrix(
        supply=MappingProxyType({pos: MappingProxyType(d) for pos, d in supply.items()}),
        need=MappingProxyType({pos: MappingProxyType(d) for pos, d in need.items()}),
        glut=MappingProxyType({pos: MappingProxyType(d) for pos, d in glut.items()}),
    )


# ---------------------------------------------------------------------------
# §5 -- need_bonus, and the clamp fit_value (step 8) will wrap it in
# ---------------------------------------------------------------------------


def need_bonus(
    roster_player: RosterPlayer,
    ovr: int,
    matrix: NeedMatrix,
    profile: PhilosophyProfile,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """§5's `need_bonus`, verbatim and **unclamped**:

    `fit_sens * NEED_GAIN * Sum_pos coverage(p, pos) * need[pos][tier(p)]
     - fit_sens * GLUT_LOSS * Sum_pos coverage(p, pos) * glut[pos][tier(p)]`

    `tier(p)` is `tier_for_overall(ovr)` -- `ovr` is the explicit talent
    scalar the caller already used for `market_value` (see module
    docstring), not re-derived from `roster_player.player.overall_cached`
    internally. The clamp in §5's `fit_value` formula
    (`clamp(need_bonus, -0.35, +0.50)`) is applied by `clamp_need_bonus`
    below, not here -- this function is exactly the sum-of-products §5
    states, nothing more."""
    coverage = position_coverage(roster_player)
    tier = tier_for_overall(ovr)
    gain = sum(coverage[pos] * matrix.need[pos][tier] for pos in _POSITIONS)
    loss = sum(coverage[pos] * matrix.glut[pos][tier] for pos in _POSITIONS)
    return profile.fit_sens * (tuning.need_gain * gain - tuning.glut_loss * loss)


NEED_BONUS_CLAMP_LO: float = -0.35
NEED_BONUS_CLAMP_HI: float = 0.50
"""§5's `fit_value` formula: `market_value * (1 + clamp(need_bonus, -0.35,
+0.50))`. Declared here (not inside `clamp_need_bonus`, not re-declared at
step 8) so `decisions/trade.py` reads the same two bounds `clamp_need_
bonus` itself uses -- one implementation, one pair of bounds, per
Decision P2-33's "a second implementation of a formula this module
already has one of is exactly how §4.6 acquired its worst bugs.\""""


def clamp_need_bonus(raw_need_bonus: float) -> float:
    """The clamp half of §5's `fit_value` formula, isolated so step 8's
    `fit_value` is exactly `market_value(...) * (1 +
    clamp_need_bonus(need_bonus(...)))` with no second copy of `-0.35`/
    `+0.50` anywhere."""
    return _clamp(raw_need_bonus, NEED_BONUS_CLAMP_LO, NEED_BONUS_CLAMP_HI)
