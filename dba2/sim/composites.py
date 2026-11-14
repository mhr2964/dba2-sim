"""Decision 13's derived composites (phase-1-simulation-engine.md §4.1,
Decision P1-6/P1-6a/P1-13): the spacing and defensive-scheme-pressure
aggregates, and the wingspan/height physical-anchor normalization.

"Pure, no columns": unlike `probability.ability_score`, nothing here reads
a `player_ratings` column by name directly out of a live possession -- the
per-player pieces (`spacing_contribution`, `scheme_contribution`,
physical normalization) are computed once at `SimPlayer` construction from
raw domain objects, and the per-possession aggregates below only ever sum
those already-computed scalars (Decision P1-6a's whole point: expensive
column-reading happens once per game, not once per possession).

Zero I/O, per Decision P1-1. `SimPlayer` is only ever referenced as a type
hint here (under `TYPE_CHECKING`, with `from __future__ import
annotations` making every annotation a string at runtime) -- a real,
module-level import would be circular, since `context.py` calls this
module's per-ratings helpers while building a `SimPlayer` in the first
place.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from dba2.domain.ratings import PlayerRatings
from dba2.sim.tuning import CompositeTuning, PhysicalAnchorTable, TuningProfile

if TYPE_CHECKING:
    from dba2.sim.context import SimPlayer


def normalize_physical(value_in: float, table: PhysicalAnchorTable) -> float:
    """Decision P1-13: map raw inches onto the 0-99 rating scale via
    `table`'s anchor points, linearly interpolating between the two
    bracketing points and clamping outside the table's own range (a player
    shorter than the shortest anchor gets the shortest anchor's rating,
    not an extrapolated negative one)."""
    points = table.points
    if value_in <= points[0].inches:
        return points[0].rating
    if value_in >= points[-1].inches:
        return points[-1].rating
    for lo, hi in zip(points, points[1:], strict=False):
        if lo.inches <= value_in <= hi.inches:
            span = hi.inches - lo.inches
            t = (value_in - lo.inches) / span if span else 0.0
            return lo.rating + t * (hi.rating - lo.rating)
    return points[-1].rating  # unreachable: points is sorted and bounds-checked above


def _weighted_mean_of_ratings(ratings: PlayerRatings, weights: Mapping[str, float]) -> float:
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ValueError("composite weights must sum to a positive total")
    return sum(getattr(ratings, field) * w for field, w in weights.items()) / total_weight


def passer_score(ratings: PlayerRatings) -> float:
    """`0.6 * passing_vision + 0.4 * passing_accuracy` -- the one-off
    passing-ability score `beats/setup.py::_ball_handler_passing_
    concentration` originally defined inline (`tuning.py:302-304`'s own
    comment: "roughly `derivation.py`'s own `pass_freq` formula weights for
    these two fields, 0.40/0.30, renormalized to sum to 1 -- an
    interpretive call, not a value named anywhere else"). Factored out here
    (not left as two separate inline copies) per
    docs/design/per-36-spread-compression.md SC-7 Commit 3 step 2's own
    instruction: a second restatement of this formula is exactly the drift
    `_usage_scale` was factored out to prevent. `beats/setup.py`'s
    `_ball_handler_passing_concentration` (role-weight concentration) and
    `beats/rotation.py`'s pass/no-pass branch tilt (SC-3.5/SC-7 Commit 3)
    are its two consumers -- neither is a `WeightVector` reader, so this
    plain two-field scalar, not `_weighted_mean_of_ratings`, is the right
    shape (no ability-vector normalization semantics apply to a fixed
    0.6/0.4 split)."""
    return 0.6 * ratings.passing_vision + 0.4 * ratings.passing_accuracy


def spacing_contribution(ratings: PlayerRatings, tuning: TuningProfile) -> float:
    """Decision P1-6's per-player spacing term (`catch_shoot_3` +
    `relocation`), legal to cache at `SimPlayer` construction per Decision
    P1-4 because it depends on nothing but this one player's own ratings."""
    return _weighted_mean_of_ratings(ratings, tuning.composites.spacing_weights)


def scheme_contribution(ratings: PlayerRatings, tuning: TuningProfile) -> float:
    """Decision P1-6's per-player scheme-pressure term (`scheme_discipline`
    + `help_iq`), same construction-time-cache legality as
    `spacing_contribution` above."""
    return _weighted_mean_of_ratings(ratings, tuning.composites.scheme_weights)


def rim_protection_contribution(
    ratings: PlayerRatings, wingspan_normalized: float, tuning: TuningProfile
) -> float:
    """The weighted mean of `resolution_rim_help.defense_weights` on this
    player alone -- the same construction-time-cache legality
    `spacing_contribution`/`scheme_contribution` above already have
    (Decision P1-4/P1-6a: it depends on nothing but this one player's own
    ratings/physical anchors).

    docs/design/block-attribution-v2.md BA-2.4: this is the *one* scalar
    the weak-side rim-help contest reads for each eligible helper, both for
    his proximity share and for his block contest (`beats/resolution.py`),
    so the player the floor sends is the player whose ability is contested.
    Until v2 it read `resolution_block.defense_weights` (BDA-4.1) while the
    contest read the rim-help vector, and the two could rank players
    differently. `best_rim_protector` below still takes its `max` to name
    `RoleSlot.RIM_PROTECTOR`.

    Can't call straight into `_weighted_mean_of_ratings` above: the vector
    includes `wingspan_in`, a `Player`
    field Decision P1-13 normalizes onto rating scale at `SimPlayer`
    construction, not a `PlayerRatings` column -- the same honorary
    exception `probability.rating_value` already carries for every other
    ability-vector reader (`resolution.py`'s block/rim-help contests
    among them). `ratings` has no `wingspan_in` attribute, so this
    substitutes the already-normalized cache the caller (`build_sim_player`)
    passes in instead of reading it off `ratings` directly."""
    weights = tuning.resolution_rim_help.defense_weights
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ValueError("composite weights must sum to a positive total")
    total = 0.0
    for field_name, w in weights.items():
        value = (
            wingspan_normalized if field_name == "wingspan_in" else getattr(ratings, field_name)
        )
        total += value * w
    return total / total_weight


def loose_ball_contribution(ratings: PlayerRatings, tuning: TuningProfile) -> float:
    """docs/design/turnover-recovery.md TR-4.4: the weighted mean of
    `tuning.loose_ball.weights` on this player alone -- the per-player half
    of the loose-ball contest, cached at `SimPlayer` construction with the
    same Decision P1-4/P1-6a legality as `spacing_contribution` (it depends
    on nothing but this one player's own ratings). All three weighted fields
    are plain `PlayerRatings` columns, so unlike `rim_protection_contribution`
    there is no physical-anchor special case."""
    return _weighted_mean_of_ratings(ratings, tuning.loose_ball.weights)


def best_rim_protector(defense_lineup: Sequence[SimPlayer]) -> SimPlayer:
    """BDA-4.7: the floor's best rim protector, by the already-cached
    `rim_protection_contribution` scalar -- one `max` over (at most) 5
    cached floats. Names `RoleSlot.RIM_PROTECTOR` in
    `playbook.defense_roles_from_matchups` (Option C's honest,
    non-matchup-derived role slot) for the beat log, broadcast and newsroom:
    a named slot must be one player. docs/design/block-attribution-v2.md
    BA-2.6: the weak-side rim-help *contest* no longer reads it -- it runs
    over every eligible helper and draws the blocker -- so a help block's
    `blocking_defender_id` can be someone other than this player."""
    return max(defense_lineup, key=lambda p: p.rim_protection_contribution)


def _center_on_rating_midpoint(mean_rating: float) -> float:
    """Map a 0-99-scale mean rating onto a centered ~[-1, 1] value the same
    way `beats/help._gate_probability` centers `scheme_discipline` on
    `- 50.0` before it enters a diff -- fixes the B1 defect critic
    reproduced: `spacing_aggregate`/`scheme_pressure_aggregate` used to
    return the raw 0-99-scale mean untouched, which every call site that
    treats it as a small [-1, 1]-ish modifier term (`on_ball.py`'s
    `spacing_aggregate * cfg.spacing_diff_weight`, sized to compose with a
    genuinely [-1, 1] fatigue diff) silently multiplied by ~50x too much,
    saturating the beat's probability band at any realistic input
    regardless of the actual ability difference being modeled. Centering
    here, once, means every present and future consumer gets a
    correctly-scaled value automatically instead of every call site having
    to remember its own rescale."""
    return (mean_rating - 50.0) / 50.0


def spacing_aggregate(non_role_offense: Sequence[SimPlayer]) -> float:
    """Decision P1-6a: the mean of the already-cached `spacing_contribution`
    terms over the possession's actual non-role offensive players,
    recomputed fresh every possession (never cached across possessions --
    the non-role set changes with the action type, Decision P1-6a's whole
    correction), centered onto ~[-1, 1] per `_center_on_rating_midpoint`
    (B1 fix) so a call site can combine it with other diff-scale modifier
    terms without a scale mismatch. Empty input (no template names fewer
    than 4 offense roles today, but a future 5-role template could)
    returns 0.0 rather than raising -- 0.0 is also `_center_on_rating_
    midpoint`'s own neutral point, so an empty non-role set reads as "no
    spacing signal" either way."""
    if not non_role_offense:
        return 0.0
    mean_contribution = sum(p.spacing_contribution for p in non_role_offense) / len(
        non_role_offense
    )
    return _center_on_rating_midpoint(mean_contribution)


def aggression_linear_scale(help_aggression: int, lo: float, hi: float) -> float:
    """Linear map of `team_strategy.help_aggression` (tendency domain,
    0-100) onto `[lo, hi]` -- factored out of `_help_aggression_scale`
    below so a second consumer (`beats/resolution.py`'s BDA-4 weak-side
    rim-help contest, `tuning.ResolutionRimHelpTuning.aggression_lo/_hi`)
    can reuse the exact same two-point-linear-scale shape without
    re-deriving it, the same way `beats/setup.py`'s anchor-interpolation
    helpers already get reused pattern-for-pattern rather than each
    caller inventing its own."""
    t = clamp01(help_aggression / 100.0)
    return lo + t * (hi - lo)


def _help_aggression_scale(help_aggression: int, tuning: CompositeTuning) -> float:
    """Linear map of `team_strategy.help_aggression` (tendency domain,
    0-100) onto `[lo, hi]` -- the possession-level, state-dependent
    modifier Decision P1-6a describes as the second reason cross-possession
    caching would be wrong (this scales the aggregate itself, not a
    per-player term, so it can never be folded into a construction-time
    cache)."""
    return aggression_linear_scale(
        help_aggression,
        tuning.scheme_pressure_help_aggression_lo,
        tuning.scheme_pressure_help_aggression_hi,
    )


def clamp01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def scheme_pressure_aggregate(
    non_role_defense: Sequence[SimPlayer],
    *,
    help_aggression: int,
    tuning: TuningProfile,
) -> float:
    """Decision P1-6's defensive scheme-pressure composite: the mean of the
    non-role defenders' cached `scheme_contribution` terms, centered onto
    ~[-1, 1] per `_center_on_rating_midpoint` (same B1 fix as
    `spacing_aggregate` -- this composite has the identical raw-0-99-scale
    defect and no consumer yet, so it is fixed at the source here rather
    than left for whichever beat wires it up first to rediscover), then
    scaled by the live `team_strategy.help_aggression` value -- recomputed
    every possession for the same reason `spacing_aggregate` is."""
    if not non_role_defense:
        return 0.0
    mean_contribution = sum(p.scheme_contribution for p in non_role_defense) / len(
        non_role_defense
    )
    centered = _center_on_rating_midpoint(mean_contribution)
    return centered * _help_aggression_scale(help_aggression, tuning.composites)


# A committed helper contesting out of position is this much less effective
# (see `contest_quality`). Shared with `beats/resolution.py`'s rim-help pool,
# which applies it to a helper's cached scalar directly
# (docs/design/block-attribution-v2.md BA-2.6: no new knob).
COMMITTED_HELPER_PENALTY = 0.85


def contest_quality(
    defender: SimPlayer,
    *,
    weights: Mapping[str, float],
    is_committed_helper: bool,
    committed_helper_penalty: float = COMMITTED_HELPER_PENALTY,
) -> float:
    """§4.4's Resolution(shot) defense modifier: a composite contest score
    for whichever defender is actually contesting (the matchup defender,
    or a committed helper per the Help beat's outcome). Not wired into any
    beat this dispatch -- resolution (beat 5) is step 4-5 scope -- but
    declared here per §3's module layout so `composites.py` is complete
    for that step without another visit to this file.

    `weights` is read the same way `probability.ability_score` reads an
    ability vector (including the `wingspan_in`/`height_in` honorary
    exception), duplicated here rather than imported from `probability.py`
    to avoid this module depending on the beat-contract module for a
    single weighted-mean line; both call the same underlying arithmetic.
    A committed helper contesting out of position (not their primary
    matchup) is scaled down by `committed_helper_penalty` -- a real
    basketball fact (help defenders contesting late are less effective
    than the primary defender) with nowhere else in this file to live.
    """
    from dba2.sim.probability import ability_score

    score = ability_score(defender, weights)
    return score * committed_helper_penalty if is_committed_helper else score
