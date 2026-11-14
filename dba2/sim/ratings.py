"""Decision 12's `overall_cached` formula (phase-0-foundation.md §4.3.1,
§4.3.2, §7.4).

`compute_overall` is the one pure function Decision 12 requires: "one pure
function in `sim/ratings.py` computes it position-weighted from
`player_ratings` ... exactly one writer (`ratings_service.recompute_overall`)
persists it ... a test recomputes every player's overall and asserts
equality with the stored value." This module owns only the computation
half -- zero I/O, and per Decision 4/§3's layering rule (`sim` may not
import `data`), it imports nothing from `dba2` beyond `domain` and its own
sibling `sim.derivation` (an intra-`sim` import, not a layering violation).

Phase 0 shipped this as a documented placeholder: a plain mean of 55
fields, explicitly promising "a real position-weighted derivation ...
replaces this function's body in Phase 1 without changing its signature or
any caller." That promise turns out to be only half-keepable: computing a
position-weighted mean requires knowing the position, and `PlayerRatings`
itself carries no `primary_position` column (that lives on `players`,
§4.3.1) -- so `compute_overall` now takes an explicit `position: Position`
parameter, matching the precedent `sim.derivation.derive_tendencies`
already set for the same reason. `ratings_service.recompute_overall` is
still the one writer; it now fetches `players.primary_position` the same
way `derive_tendencies_for_player` already fetches it, rather than this
module doing any I/O of its own.

Phase 1 step 9 (docs/design/phase-1-simulation-engine.md §12 item 9) also
excludes `potential`/`potential_variance` from the composite -- §4.3.2
marks both "(hidden)", and critic round 14 flagged the placeholder's plain
mean as leaking them through a sortable, publicly displayed column. They
stay part of the 55-field generation partition `sim.derivation.RATING_GROUPS`
depends on (progression math needs them); they are simply not weighted
into `overall_cached`.
"""
from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from types import MappingProxyType

from dba2.domain.player import Position
from dba2.domain.ratings import PlayerRatings
from dba2.sim.derivation import POSITION_RATING_OFFSETS, RATING_GROUPS

# Every `PlayerRatings` field that is NOT one of the 55 0-99 `rating`-domain
# columns: the six provenance/identity columns (player_id, league_id,
# source, derived_from_season, confidence, updated_at) plus the two plain-
# SMALLINT peak-age columns (an age, not a 0-99 rating -- §4.3.2's own
# peak_age_start/peak_age_end exception). This is the *generation*
# partition -- `sim.derivation.RATING_FIELD_ORDER`'s 55 fields and its
# closed-form variance model (§6.7) both depend on this exact 8-name set,
# so it is NOT where `overall_cached`'s narrower hidden-from-the-public-
# composite exclusion (see `_HIDDEN_FROM_OVERALL` below) belongs -- folding
# `potential`/`potential_variance` in here would silently break the
# 55-field partition `test_derivation_ratings_model.py` and the step-7
# calibration doc's offset table pin against.
#
# Derived from `dataclasses.fields(ratings)` minus this exclusion set,
# rather than hand-listing the 55 rating names a second time (as
# `ratings_repo._RATING_COLUMN_NAMES` does against migration order) -- a
# future rating column added to `domain.ratings.PlayerRatings` is picked up
# here automatically instead of silently being left out by a second,
# un-synced column list, which is exactly the class of drift Decision
# 11/12 exist to prevent.
_NON_RATING_FIELDS: frozenset[str] = frozenset(
    {
        "player_id",
        "league_id",
        "source",
        "derived_from_season",
        "confidence",
        "updated_at",
        "peak_age_start",
        "peak_age_end",
    }
)

# The 2 rating-domain fields §4.3.2 marks "(hidden)": real 0-99 ratings
# (so they stay in the 55-field generation partition above), but excluded
# from the public, sortable `overall_cached` composite -- critic round 14's
# finding that the placeholder's plain mean leaked them through a public
# column. Separate constant from `_NON_RATING_FIELDS` on purpose (see that
# constant's comment): these two are still ratings, just not public ones.
_HIDDEN_FROM_OVERALL: frozenset[str] = frozenset({"potential", "potential_variance"})

# The 53 fields `compute_overall` actually weights: `RATING_FIELD_ORDER`'s
# canonical 55-field group order (§6.1), minus the 2 hidden fields above --
# reusing step 7's already-critic-cleared `RATING_GROUPS` clustering as the
# organizing structure for the weight table below, rather than inventing a
# second, un-synced grouping.
_PUBLIC_RATING_FIELDS: tuple[str, ...] = tuple(
    field_name
    for fields in RATING_GROUPS.values()
    for field_name in fields
    if field_name not in _HIDDEN_FROM_OVERALL
)
assert len(_PUBLIC_RATING_FIELDS) == 53
assert len(set(_PUBLIC_RATING_FIELDS)) == 53

# ---------------------------------------------------------------------------
# Per-position group weights (§12 item 9's basketball-grounding requirement:
# "direction and rough magnitude matter more than precision"). Each
# position's 11 group weights are integer parts-per-100 that sum to exactly
# 100 -- kept as plain ints here (not yet divided) so the sum-to-100 check
# below is itself exact integer arithmetic, no floating point involved.
#
# Basketball reasoning per group, PG -> C:
#   PHYSICAL     rises toward the 5 (strength/vertical matter more for
#                post/rim play than for a guard's overall shot).
#   CREATION     (handle, first step, off-the-dribble tools) falls sharply
#                toward the 5 -- shot creation off the bounce is a
#                perimeter-player skill.
#   FINISHING    (rim finishing, floater, post scoring) rises toward the 5
#                -- post scoring/contact finishing are big-man skills.
#   SHOOTING     peaks at SG (a shooting guard's defining skill), falls
#                hard toward the 5 -- centers are not weighted on jumpers.
#   PLAYMAKING   falls sharply toward the 5 -- ball distribution is a
#                guard's job.
#   SCREENING    rises sharply toward the 5 -- a center's screen-setting/
#                roll game matters far more than a point guard's.
#   OFF_BALL     (movement, cutting, offensive rebounding) rises modestly
#                toward the 5 -- rebounding pulls this group up for bigs.
#   ON_BALL_DEF  (on-ball/perimeter defense) falls modestly toward the 5 --
#                still relevant for a rim-protecting 5, but perimeter
#                on-ball defense is primarily a guard/wing job. `post_defense`
#                is a member of this group in `RATING_GROUPS` (generation
#                purposes) but is weighted as a TEAM_DEF field below (critic
#                round 21 nit 1 -- see `_OVERALL_WEIGHTING_GROUPS`): its own
#                offset shape (PG -13, C +15) marks it as an interior/big
#                skill, not a perimeter one, so weighting it under this
#                group's guard-skewed percentage made a PG's post_defense
#                count double a C's.
#   TEAM_DEF     (help defense, rim protection, defensive rebounding) rises
#                sharply toward the 5 -- the anchor of a big's defensive
#                value. Also carries `post_defense`'s weight share, per the
#                ON_BALL_DEF note above.
#   MENTAL       held constant (10) across positions -- IQ/composure/
#                consistency/motor/coachability are not position-specific
#                traits.
#   DEVELOPMENT  held constant (3) across positions -- after excluding
#                `potential`/`potential_variance`, this bucket is just
#                `work_ethic`, an equally general trait.
_POSITION_GROUP_WEIGHTS_PCT: Mapping[Position, Mapping[str, int]] = MappingProxyType(
    {
        Position.PG: MappingProxyType(
            {
                "PHYSICAL": 8,
                "CREATION": 14,
                "FINISHING": 6,
                "SHOOTING": 12,
                "PLAYMAKING": 16,
                "SCREENING": 2,
                "OFF_BALL": 6,
                "ON_BALL_DEF": 12,
                "TEAM_DEF": 11,
                "MENTAL": 10,
                "DEVELOPMENT": 3,
            }
        ),
        Position.SG: MappingProxyType(
            {
                "PHYSICAL": 9,
                "CREATION": 13,
                "FINISHING": 8,
                "SHOOTING": 16,
                "PLAYMAKING": 8,
                "SCREENING": 3,
                "OFF_BALL": 8,
                "ON_BALL_DEF": 11,
                "TEAM_DEF": 11,
                "MENTAL": 10,
                "DEVELOPMENT": 3,
            }
        ),
        Position.SF: MappingProxyType(
            {
                "PHYSICAL": 10,
                "CREATION": 10,
                "FINISHING": 9,
                "SHOOTING": 11,
                "PLAYMAKING": 8,
                "SCREENING": 5,
                "OFF_BALL": 8,
                "ON_BALL_DEF": 10,
                "TEAM_DEF": 16,
                "MENTAL": 10,
                "DEVELOPMENT": 3,
            }
        ),
        Position.PF: MappingProxyType(
            {
                "PHYSICAL": 12,
                "CREATION": 6,
                "FINISHING": 11,
                "SHOOTING": 7,
                "PLAYMAKING": 6,
                "SCREENING": 9,
                "OFF_BALL": 10,
                "ON_BALL_DEF": 8,
                "TEAM_DEF": 18,
                "MENTAL": 10,
                "DEVELOPMENT": 3,
            }
        ),
        Position.C: MappingProxyType(
            {
                "PHYSICAL": 14,
                "CREATION": 3,
                "FINISHING": 13,
                "SHOOTING": 4,
                "PLAYMAKING": 4,
                "SCREENING": 12,
                "OFF_BALL": 11,
                "ON_BALL_DEF": 6,
                "TEAM_DEF": 20,
                "MENTAL": 10,
                "DEVELOPMENT": 3,
            }
        ),
    }
)

for _position, _group_pcts in _POSITION_GROUP_WEIGHTS_PCT.items():
    assert set(_group_pcts) == set(RATING_GROUPS), (
        f"{_position}: group weight table doesn't cover exactly RATING_GROUPS's groups"
    )
    assert sum(_group_pcts.values()) == 100, (
        f"{_position}: group weight percentages sum to {sum(_group_pcts.values())}, not 100"
    )


# Which group's percentage each field draws from when `_build_field_weights`
# splits a group's share across its fields -- identical to `RATING_GROUPS`
# except `post_defense` moves from ON_BALL_DEF to TEAM_DEF (critic round 21
# nit 1, see the ON_BALL_DEF/TEAM_DEF comments above for the basketball
# reasoning). This changes only how `overall_cached` weights the field; it
# does NOT touch `sim.derivation.RATING_GROUPS` itself, so generation
# (`synthetic_ratings`'s group-noise draw) and calibration
# (`expected_group_sd`, the step-7 calibration doc's per-group SD bands) are
# both untouched -- this is a weighting-only regrouping.
_OVERALL_WEIGHTING_GROUPS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        group: (
            tuple(f for f in fields if f != "post_defense")
            if group == "ON_BALL_DEF"
            else (*fields, "post_defense")
            if group == "TEAM_DEF"
            else fields
        )
        for group, fields in RATING_GROUPS.items()
    }
)
assert "post_defense" not in _OVERALL_WEIGHTING_GROUPS["ON_BALL_DEF"]
assert "post_defense" in _OVERALL_WEIGHTING_GROUPS["TEAM_DEF"]
assert set(_OVERALL_WEIGHTING_GROUPS) == set(RATING_GROUPS)
assert sum(len(fields) for fields in _OVERALL_WEIGHTING_GROUPS.values()) == sum(
    len(fields) for fields in RATING_GROUPS.values()
)
assert {f for fields in _OVERALL_WEIGHTING_GROUPS.values() for f in fields} == {
    f for fields in RATING_GROUPS.values() for f in fields
}


def _build_field_weights(group_pcts: Mapping[str, int]) -> Mapping[str, Fraction]:
    """Splits each group's integer percentage evenly across that group's
    *public* (non-hidden) fields, as an exact `Fraction` rather than a
    float -- `Fraction(group_pct, 100 * n_fields)` summed back up over a
    group's `n_fields` reconstructs `Fraction(group_pct, 100)` exactly, so
    the resulting per-position weight map is guaranteed (not just
    numerically close) to sum to `Fraction(1, 1)`: the monotonicity
    requirement below falls out of "every weight is non-negative and they
    sum to exactly 1" by construction, rather than needing its own
    separate proof.

    Splits over `_OVERALL_WEIGHTING_GROUPS`, not `RATING_GROUPS` directly
    (see that constant's comment) -- the two agree everywhere except
    `post_defense`."""
    weights: dict[str, Fraction] = {}
    for group, field_names in _OVERALL_WEIGHTING_GROUPS.items():
        public_fields = [f for f in field_names if f not in _HIDDEN_FROM_OVERALL]
        share = Fraction(group_pcts[group], 100 * len(public_fields))
        for field_name in public_fields:
            weights[field_name] = share
    return weights


# Per-position field weights, non-negative Fractions summing to exactly
# 1 -- built once at import time from the group table above, the same
# "solved from declared data at import time" pattern
# `sim.derivation`'s own module docstring already uses for
# `expected_group_sd`/`TENDENCY_SPREADS`.
_OVERALL_WEIGHTS: Mapping[Position, Mapping[str, Fraction]] = MappingProxyType(
    {
        position: MappingProxyType(_build_field_weights(group_pcts))
        for position, group_pcts in _POSITION_GROUP_WEIGHTS_PCT.items()
    }
)

for _position, _weights in _OVERALL_WEIGHTS.items():
    assert set(_weights) == set(_PUBLIC_RATING_FIELDS), (
        f"{_position}: weight map's field set doesn't match _PUBLIC_RATING_FIELDS"
    )
    assert all(w >= 0 for w in _weights.values()), f"{_position}: has a negative weight"
    assert sum(_weights.values(), Fraction(0)) == Fraction(1, 1), (
        f"{_position}: weights sum to {sum(_weights.values(), Fraction(0))}, not exactly 1"
    )


def _position_overall_artifact(position: Position) -> float:
    """Critic round 21, B21-1: the talent-independent bump (or drag) this
    position's own weight table bakes into `compute_overall` purely from
    `sim.derivation.POSITION_RATING_OFFSETS`' shape -- the weighted sum
    this function's weights produce for a hypothetical talent-0,
    noise-free player whose 53 public ratings are exactly this position's
    own generation offsets. `derivation._double_center` guarantees this
    quantity is exactly 0 under a *plain* mean (I2/I3 -- every position's
    row of the 55-field offset table sums to 0), but a non-uniform weight
    table does not annihilate a zero-sum row the way a plain mean does,
    so this is generally nonzero (critic measured +1.81/+0.90/+0.06/
    +0.63/+2.42 for PG/SG/SF/PF/C). `compute_overall` subtracts this per
    position so that two players sharing the same talent draw score the
    same regardless of position, restoring cross-position comparability
    without changing how the weights differentiate skill *within* a
    position (subtracting a position-constant is an affine shift, so it
    changes no field's marginal contribution)."""
    weights = _OVERALL_WEIGHTS[position]
    offsets = POSITION_RATING_OFFSETS[position]
    return float(sum(weight * offsets[field_name] for field_name, weight in weights.items()))


# Computed once at import time, same pattern as `_OVERALL_WEIGHTS` above.
_POSITION_OVERALL_ARTIFACT: Mapping[Position, float] = MappingProxyType(
    {position: _position_overall_artifact(position) for position in _OVERALL_WEIGHTS}
)


def _tilted_group_fractions(
    position: Position, group_tilt: Mapping[str, float]
) -> Mapping[str, Fraction]:
    """Decision P2-9 (phase-2-manager-minds.md §6, §8): `_POSITION_GROUP_
    WEIGHTS_PCT[position]`'s per-group integer percentages, each multiplied
    by `group_tilt.get(group, 1.0)` and renormalized to sum to exactly
    `Fraction(1, 1)`. `group_tilt` is applied over `_OVERALL_WEIGHTING_
    GROUPS`'s group names (the ones this dict's keys are), never over
    `sim.derivation.RATING_GROUPS` directly -- see that constant's own
    comment for why the two differ (`post_defense`).

    Every multiplication and division here is exact `Fraction` arithmetic
    (`Fraction(1.0)` converts a Python float via its exact binary value,
    not a decimal approximation) so that an all-1.0 `group_tilt` -- the
    default every non-`defense_first` philosophy uses -- reduces to
    `Fraction(pct, 100)` for every group, identically to
    `_build_field_weights`'s own computation below. `Fraction` always
    stores its canonical (lowest-terms) representation, so two arithmetic
    paths that reach the same rational number are guaranteed to produce
    bit-identical `Fraction` objects regardless of the path taken --
    which is what makes the bit-identical-at-default-tilt regression test
    in `tests/unit/sim/test_ratings.py` a property of the math, not a
    coincidence of one particular code path."""
    base_pcts = _POSITION_GROUP_WEIGHTS_PCT[position]
    tilted = {
        group: Fraction(pct) * Fraction(group_tilt.get(group, 1.0))
        for group, pct in base_pcts.items()
    }
    total = sum(tilted.values(), Fraction(0))
    return {group: value / total for group, value in tilted.items()}


def _tilted_field_weights(
    position: Position, group_tilt: Mapping[str, float]
) -> Mapping[str, Fraction]:
    """The tilted analogue of `_build_field_weights`: splits each tilted,
    renormalized group fraction evenly across that group's public fields.
    Iterates `_OVERALL_WEIGHTING_GROUPS.items()` in the same fixed order
    `_build_field_weights` does, so the two functions insert fields into
    their result dicts in identical order -- required for the default-tilt
    bit-identity guarantee to extend past the weights themselves and into
    `_tilted_overall_artifact`'s float summation below (float addition is
    not associative, so summation order has to match too, not just the
    individual terms)."""
    group_fracs = _tilted_group_fractions(position, group_tilt)
    weights: dict[str, Fraction] = {}
    for group, field_names in _OVERALL_WEIGHTING_GROUPS.items():
        public_fields = [f for f in field_names if f not in _HIDDEN_FROM_OVERALL]
        share = group_fracs[group] / len(public_fields)
        for field_name in public_fields:
            weights[field_name] = share
    return weights


def _tilted_overall_artifact(position: Position, weights: Mapping[str, Fraction]) -> float:
    """The tilted analogue of `_position_overall_artifact` -- recomputed
    for the tilted weights rather than reusing `_POSITION_OVERALL_
    ARTIFACT[position]` (§6: "the tilted entry point must recompute
    `_POSITION_OVERALL_ARTIFACT` for the tilted weights, not reuse the
    default-weight one... that constant is precomputed at import from the
    *default* table, so reusing it lets cross-position comparability
    drift"). Not cached at import time like `_POSITION_OVERALL_ARTIFACT`
    is, because `group_tilt` is runtime data (a `PhilosophyProfile` field),
    not a module-level constant -- there is nothing to precompute against
    until a caller supplies a tilt."""
    offsets = POSITION_RATING_OFFSETS[position]
    return float(sum(weight * offsets[field_name] for field_name, weight in weights.items()))


def compute_overall_tilted(
    ratings: PlayerRatings, position: Position, group_tilt: Mapping[str, float]
) -> int:
    """Decision P2-9's public tilted-overall entry point (phase-2-manager-
    minds.md §6, §8, §12 step 3): re-weights `_POSITION_GROUP_WEIGHTS_PCT`'s
    per-position group percentages by `group_tilt` (a `Mapping[str, float]`
    defaulting -- via `.get(group, 1.0)` -- to no-op for any group not
    named in it; an empty mapping tilts nothing at all), renormalizes to
    sum to 1, and re-runs `compute_overall`'s own weighted-mean machinery
    against the tilted weights, including its own re-centered artifact
    (`_tilted_overall_artifact`, not `_POSITION_OVERALL_ARTIFACT`).

    Only `defense_first` (§6's `_DEFENSE_FIRST_TILT`) sets a non-empty
    `group_tilt` anywhere in this codebase; every other philosophy reads
    `group_tilt = {}` (`managers/philosophy.py`'s `_NO_TILT`), for which
    this function is required to reproduce `compute_overall`'s own output
    bit-identically -- pinned by `tests/unit/sim/test_ratings.py`'s
    `test_compute_overall_tilted_reproduces_compute_overall_at_the_default_
    tilt` (§14's risk row: "the tilt is additive ... a test asserts the
    default-tilt result is bit-identical to `compute_overall`. If it
    isn't, the change is wrong.")."""
    weights = _tilted_field_weights(position, group_tilt)
    artifact = _tilted_overall_artifact(position, weights)
    terms = (weight * getattr(ratings, field_name) for field_name, weight in weights.items())
    weighted_sum = sum(terms, Fraction(0))
    recentered = weighted_sum - artifact
    return max(0, min(99, round(recentered)))


def compute_overall(ratings: PlayerRatings, position: Position) -> int:
    """Position-weighted mean of the 53 public rating fields (the 55
    non-age rating fields minus the 2 §4.3.2 marks "(hidden)":
    `potential`/`potential_variance`), re-centered per position and
    clamped to the `rating` domain's [0, 99] range. Zero I/O --
    `ratings_service.recompute_overall` is the only intended caller,
    wiring this to a database read/write and supplying `position` from
    the player's own `primary_position` row.

    Every position's weights are non-negative and sum to exactly 1
    (enforced above, at import time, via exact `Fraction` arithmetic) --
    which is precisely what makes this monotone in every field by
    construction: raising any single rating while holding the rest fixed
    strictly increases (or, for a zero-weighted field, leaves unchanged)
    the weighted sum, since every term's coefficient is >= 0. There is no
    weight-table-shaped way for this function to be non-monotone; the
    monotonicity test in `tests/unit/sim/test_ratings.py` confirms it
    holds in practice rather than only asserting it algebraically here.
    Subtracting `_POSITION_OVERALL_ARTIFACT[position]` below is a per-
    position *constant* shift, so it cannot affect monotonicity in any
    field.

    Critic round 21, B21-1: subtracts `_POSITION_OVERALL_ARTIFACT[position]`
    (see that function's docstring) so that two players sharing the same
    talent draw score the same `overall_cached` regardless of position --
    a non-uniform weight table applied directly to
    `sim.derivation.POSITION_RATING_OFFSETS`'s zero-sum-per-position row
    (I2/I3) does not itself land on 0, so without this step the weight
    table was silently converting part of the offset table's shape into a
    free, position-only overall bump.
    """
    weights = _OVERALL_WEIGHTS[position]
    terms = (
        weight * getattr(ratings, field_name) for field_name, weight in weights.items()
    )
    weighted_sum = sum(terms, Fraction(0))
    recentered = weighted_sum - _POSITION_OVERALL_ARTIFACT[position]
    return max(0, min(99, round(recentered)))
