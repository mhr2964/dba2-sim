"""Tier 1: the physical anchor normalization and the spacing/scheme-
pressure aggregates (phase-1-simulation-engine.md §4.1, Decision
P1-6/P1-6a/P1-13)."""
from __future__ import annotations

import pytest

from dba2.sim.composites import (
    contest_quality,
    normalize_physical,
    scheme_contribution,
    scheme_pressure_aggregate,
    spacing_aggregate,
    spacing_contribution,
)
from dba2.sim.tuning import DEFAULT_TUNING, AnchorPoint, PhysicalAnchorTable
from tests.fixtures.sim import make_ratings, make_sim_player

_TABLE = PhysicalAnchorTable(
    points=(
        AnchorPoint(70.0, 10.0),
        AnchorPoint(80.0, 50.0),
        AnchorPoint(90.0, 90.0),
    )
)


def test_normalize_physical_interpolates_between_anchor_points():
    assert normalize_physical(75.0, _TABLE) == pytest.approx(30.0)


def test_normalize_physical_clamps_below_the_lowest_anchor():
    assert normalize_physical(50.0, _TABLE) == 10.0


def test_normalize_physical_clamps_above_the_highest_anchor():
    assert normalize_physical(120.0, _TABLE) == 90.0


def test_normalize_physical_is_exact_at_an_anchor_point():
    assert normalize_physical(80.0, _TABLE) == 50.0


def test_physical_anchor_table_rejects_unsorted_points():
    with pytest.raises(ValueError):
        PhysicalAnchorTable(points=(AnchorPoint(80.0, 50.0), AnchorPoint(70.0, 10.0)))


def test_spacing_contribution_is_monotonic_in_its_named_ratings():
    low = make_ratings(1, catch_shoot_3=20, relocation=20)
    high = make_ratings(2, catch_shoot_3=90, relocation=90)
    assert spacing_contribution(low, DEFAULT_TUNING) < spacing_contribution(high, DEFAULT_TUNING)


def test_scheme_contribution_is_monotonic_in_its_named_ratings():
    low = make_ratings(1, scheme_discipline=15, help_iq=15)
    high = make_ratings(2, scheme_discipline=95, help_iq=95)
    assert scheme_contribution(low, DEFAULT_TUNING) < scheme_contribution(high, DEFAULT_TUNING)


def test_spacing_aggregate_is_the_mean_over_the_non_role_set_only_centered_on_50():
    """B1 fix: `spacing_aggregate` no longer returns the raw 0-99-scale
    mean -- it centers it onto ~[-1, 1] (`(mean - 50.0) / 50.0`, mirroring
    `beats/help._gate_probability`'s own centering) so a call site can
    combine it with genuinely [-1, 1]-scale modifier terms (e.g. fatigue)
    without silently saturating the beat's probability band, which is
    exactly what critic proved the pre-fix raw-scale version did."""
    non_role = [
        make_sim_player(i, tuning=DEFAULT_TUNING, ratings_overrides={"catch_shoot_3": 30 + i})
        for i in range(1, 4)
    ]
    raw_mean = sum(p.spacing_contribution for p in non_role) / len(non_role)
    expected = (raw_mean - 50.0) / 50.0
    assert spacing_aggregate(tuple(non_role)) == pytest.approx(expected)


def test_spacing_aggregate_is_zero_on_an_empty_non_role_set():
    assert spacing_aggregate(()) == 0.0


def test_scheme_pressure_aggregate_scales_with_help_aggression():
    # `scheme_discipline`/`help_iq` both pinned comfortably above the
    # centered composite's 50-point midpoint (B1 fix) -- the aggregate is
    # now centered before the `help_aggression` multiplier is applied, so
    # a mean straddling 50 could flip the direction of "scales with" for
    # a negative centered value; pinning both fields well above 50
    # guarantees the centered value stays positive and the multiplier's
    # own monotonic-increasing shape is what this test is actually
    # exercising.
    defenders = tuple(
        make_sim_player(
            i,
            tuning=DEFAULT_TUNING,
            ratings_overrides={"scheme_discipline": 80, "help_iq": 80},
        )
        for i in range(1, 4)
    )
    low_aggression = scheme_pressure_aggregate(defenders, help_aggression=5, tuning=DEFAULT_TUNING)
    high_aggression = scheme_pressure_aggregate(
        defenders, help_aggression=95, tuning=DEFAULT_TUNING
    )
    assert low_aggression < high_aggression


def test_scheme_pressure_aggregate_is_zero_on_an_empty_non_role_set():
    assert scheme_pressure_aggregate((), help_aggression=50, tuning=DEFAULT_TUNING) == 0.0


def test_contest_quality_penalizes_a_committed_helper_relative_to_the_primary_defender():
    defender = make_sim_player(
        1, tuning=DEFAULT_TUNING, ratings_overrides={"rim_protection": 80, "vertical": 80}
    )
    weights = {"rim_protection": 1.0, "vertical": 1.0}
    primary = contest_quality(defender, weights=weights, is_committed_helper=False)
    helper = contest_quality(defender, weights=weights, is_committed_helper=True)
    assert helper < primary
