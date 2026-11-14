"""Phase 1 step 9 (`docs/design/phase-1-simulation-engine.md` §12 item 9):
`compute_overall`'s position-weighted formula. The two pre-existing
pinning tests for the exclusion sets live in
`tests/integration/test_ratings_service.py` (updated in this same round --
see that file's module docstring note) since that file already builds
`PlayerRatings` fixtures for the DB-backed consistency tests; this file
covers the genuinely new behavior: the weight table's own shape (every
position's weights are non-negative and sum to exactly 1, over exactly the
53 public fields) and the monotonicity property that shape is supposed to
guarantee by construction.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction

from dba2.domain.player import Position
from dba2.domain.ratings import PlayerRatings, RatingSource
from dba2.sim.derivation import POSITION_RATING_OFFSETS, RATING_FIELD_ORDER
from dba2.sim.ratings import (
    _HIDDEN_FROM_OVERALL,
    _NON_RATING_FIELDS,
    _OVERALL_WEIGHTS,
    _PUBLIC_RATING_FIELDS,
    compute_overall,
    compute_overall_tilted,
)

_POSITIONS = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)


def _ratings(values: dict[str, int]) -> PlayerRatings:
    return PlayerRatings(
        player_id=1,
        league_id=1,
        source=RatingSource.MANUAL,
        derived_from_season=None,
        confidence=Decimal("1.00"),
        updated_at=datetime.now(UTC),
        **values,
    )


def _distinct_ratings(offset: int = 0) -> dict[str, int]:
    """Every 0-99 rating-domain field a different value (mod 90), including
    the 2 hidden fields (`compute_overall` must ignore them regardless of
    what they're set to) -- `PlayerRatings` fields minus `_NON_RATING_
    FIELDS`, the same 55-field derivation `compute_overall` itself starts
    from before narrowing to the 53 public ones -- plus fixed, valid
    `peak_age_start`/`peak_age_end` values so the resulting dict satisfies
    `PlayerRatings`'s full constructor (those 2 are plain ages, not part of
    the 55 rating fields this fixture is otherwise varying)."""
    names = [
        f.name for f in dataclasses.fields(PlayerRatings) if f.name not in _NON_RATING_FIELDS
    ]
    ratings = {name: (i + offset) % 90 for i, name in enumerate(names)}
    ratings["peak_age_start"] = 26
    ratings["peak_age_end"] = 30
    return ratings


def test_every_positions_weights_are_non_negative_and_sum_to_exactly_one():
    """§12 item 9's own framing: weights non-negative and summing to
    exactly 1.0 is what makes monotonicity true *by construction* --
    pinned here as exact `Fraction` arithmetic (not `pytest.approx`)
    because the whole point is that this sum has no floating-point slop to
    tolerate."""
    for position in _POSITIONS:
        weights = _OVERALL_WEIGHTS[position]
        assert set(weights) == set(_PUBLIC_RATING_FIELDS)
        assert all(w >= 0 for w in weights.values())
        assert sum(weights.values(), Fraction(0)) == Fraction(1, 1)


def test_public_rating_fields_excludes_potential_and_potential_variance():
    """Critic round 14's finding, pinned directly: `compute_overall`'s
    weighted field set must not contain either hidden development field,
    regardless of what value they hold on a given player."""
    assert _HIDDEN_FROM_OVERALL == frozenset({"potential", "potential_variance"})
    assert "potential" not in _PUBLIC_RATING_FIELDS
    assert "potential_variance" not in _PUBLIC_RATING_FIELDS
    assert len(_PUBLIC_RATING_FIELDS) == 53


def test_compute_overall_ignores_the_two_hidden_fields():
    """Raising `potential`/`potential_variance` to 99 while holding every
    public field at a fixed baseline must not move `compute_overall` at
    all -- the direct behavioral proof that the exclusion is real, not
    just a set-membership pin."""
    baseline = _distinct_ratings(offset=0)
    hidden_maxed = dict(baseline, potential=99, potential_variance=99)
    for position in _POSITIONS:
        assert compute_overall(_ratings(baseline), position) == compute_overall(
            _ratings(hidden_maxed), position
        )


def test_compute_overall_is_monotone_in_every_public_field_across_all_positions():
    """The design doc's required verify step: "a monotonicity test (raising
    any weighted rating never lowers overall)." Sampled across all 5
    positions and a handful of distinct baseline fixtures rather than a
    single all-flat one, so a field with a genuinely zero weight for some
    position is exercised too (raising it must merely not *lower* the
    result, which a same-mean flat fixture wouldn't distinguish from a
    field that was silently dropped)."""
    baselines = [_distinct_ratings(offset=0), _distinct_ratings(offset=37)]
    for position in _POSITIONS:
        for baseline in baselines:
            base_overall = compute_overall(_ratings(baseline), position)
            for field_name in _PUBLIC_RATING_FIELDS:
                raised = dict(baseline)
                current = raised[field_name]
                if current >= 99:
                    continue
                raised[field_name] = 99
                raised_overall = compute_overall(_ratings(raised), position)
                assert raised_overall >= base_overall, (
                    f"{position}: raising {field_name} from {current} to 99 lowered "
                    f"overall from {base_overall} to {raised_overall}"
                )


def _position_typical_ratings(position: Position, talent: float) -> PlayerRatings:
    """A talent-`talent`, zero-noise player whose 55 ratings follow
    `position`'s own generation offset shape exactly (`ratings[f] = talent
    + POSITION_RATING_OFFSETS[position][f]`) -- i.e. a player who is
    *exactly* as talented as `talent` and *exactly* as position-typical as
    the generator's own offset table says a player of this position should
    be, no more and no less. This is deliberately not a flat/uniform-value
    fixture: a flat fixture is trivially position-invariant under any
    weight table that sums to 1 (weighted mean of a constant is that
    constant, regardless of the weights), so it cannot distinguish a fixed
    from a broken `compute_overall`. This fixture is critic round 21's own
    reproduction recipe for B21-1 (`POSITION_RATING_OFFSETS` alone, no
    noise) plus a shared talent baseline, which is exactly the shape a real
    `synthetic_ratings` draw's *expected value* takes."""
    offsets = POSITION_RATING_OFFSETS[position]
    values = {
        field_name: max(0, min(99, round(talent + offsets[field_name])))
        for field_name in RATING_FIELD_ORDER
    }
    values["peak_age_start"] = 26
    values["peak_age_end"] = 30
    return PlayerRatings(
        player_id=1,
        league_id=1,
        source=RatingSource.MANUAL,
        derived_from_season=None,
        confidence=Decimal("1.00"),
        updated_at=datetime.now(UTC),
        **values,
    )


def test_compute_overall_gives_equal_talent_players_the_same_overall_across_positions():
    """Critic round 21, B21-1: before re-centering, applying a non-uniform
    weight table to `POSITION_RATING_OFFSETS`'s zero-sum-per-position row
    does not itself land back on 0 (a plain mean would, by
    `derivation._double_center`'s I2/I3 construction, but a weighted one
    generally does not) -- so two players who share the same talent draw
    but differ only in position used to score materially differently
    (critic measured a ~2.36-point spread from this artifact alone). A
    position-neutral-talent player must read as equally good regardless of
    position, so `compute_overall` must subtract each position's own
    artifact before returning. Sampled at three talent levels so the fix
    isn't only checked at one baseline."""
    for talent in (55.0, 70.0, 85.0):
        overalls = {
            position: compute_overall(_position_typical_ratings(position, talent), position)
            for position in _POSITIONS
        }
        spread = max(overalls.values()) - min(overalls.values())
        assert spread <= 1, (
            f"talent={talent}: overall varies by position purely from position identity "
            f"(spread={spread}): {overalls}"
        )


# ---------------------------------------------------------------------------
# Decision P2-9's tilted entry point (phase-2-manager-minds.md §6, §8,
# §12 step 3) -- the change is additive to an already-shipped, already-
# validated Phase 1 system, so the default-tilt case must reproduce
# `compute_overall`'s own output bit-identically. §14's risk row states
# this as the acceptance test in so many words: "a test asserts the
# default-tilt result is bit-identical to `compute_overall`. If it isn't,
# the change is wrong."
# ---------------------------------------------------------------------------

_DEFENSE_FIRST_TILT = {
    "ON_BALL_DEF": 1.45, "TEAM_DEF": 1.45, "SHOOTING": 0.85, "CREATION": 0.85,
}


def test_compute_overall_tilted_reproduces_compute_overall_at_the_default_tilt():
    """An empty `group_tilt` (every non-`defense_first` philosophy's own
    `group_tilt`, per `managers/philosophy.py`'s `_NO_TILT`) must produce
    the exact same `int` `compute_overall` does -- not merely a numerically
    close one -- for every position and across several distinct rating
    fixtures."""
    baselines = [
        _distinct_ratings(offset=0), _distinct_ratings(offset=37), _distinct_ratings(offset=61),
    ]
    for position in _POSITIONS:
        for baseline in baselines:
            ratings = _ratings(baseline)
            default = compute_overall(ratings, position)
            tilted_no_op = compute_overall_tilted(ratings, position, {})
            assert tilted_no_op == default, (
                f"{position}, offset baseline: compute_overall_tilted(..., {{}}) = "
                f"{tilted_no_op} != compute_overall(...) = {default}"
            )


def test_compute_overall_tilted_with_defense_first_tilt_generally_moves_the_score():
    """The tilted path is only required to be a no-op at the *default*
    tilt -- a real tilt (`defense_first`'s own group multipliers) should
    generally move `overall` for a player whose ratings are lopsided
    toward exactly the groups being tilted."""
    offsets = POSITION_RATING_OFFSETS[Position.C]
    lopsided = {
        field_name: max(0, min(99, round(70 + offsets[field_name])))
        for field_name in RATING_FIELD_ORDER
    }
    # Push shooting/creation ratings far above the group's typical level
    # and on-ball/team defense far below it -- the opposite of what a
    # defense_first tilt rewards, so the tilt is guaranteed to move the
    # score in a checkable (downward) direction.
    for field_name in ("catch_shoot_3", "off_dribble_3", "handle", "first_step"):
        lopsided[field_name] = 95
    for field_name in ("on_ball_defense", "help_iq", "rim_protection", "post_defense"):
        lopsided[field_name] = 20
    lopsided["peak_age_start"] = 26
    lopsided["peak_age_end"] = 30
    ratings = _ratings(lopsided)

    default = compute_overall(ratings, Position.C)
    tilted = compute_overall_tilted(ratings, Position.C, _DEFENSE_FIRST_TILT)
    assert tilted != default
    assert tilted < default, (
        "a shooting/creation-heavy, defense-weak center should score LOWER "
        "under defense_first's tilt (which up-weights ON_BALL_DEF/TEAM_DEF "
        "and down-weights SHOOTING/CREATION), not higher"
    )
