"""§12 step 1's verify list (`docs/design/phase-1-step-7-calibration.md`):
the 55-field partition, `RATING_FIELD_ORDER`'s shape, the I1/I2/I3
centering invariants, the two spot-check assertions, and the exact
solved §8.4 spreads/`pos_row` table against the doc's printed reference.
"""
from __future__ import annotations

import dataclasses

import pytest

from dba2.domain.player import Position
from dba2.domain.ratings import PlayerRatings
from dba2.sim import derivation as d
from dba2.sim.ratings import _NON_RATING_FIELDS

_POSITIONS = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)


def test_rating_groups_partition_exactly_the_55_rating_fields():
    assert sum(len(fields) for fields in d.RATING_GROUPS.values()) == 55
    assert len(d.RATING_GROUPS) == 11
    all_fields = {f for fields in d.RATING_GROUPS.values() for f in fields}
    expected = {f.name for f in dataclasses.fields(PlayerRatings)} - _NON_RATING_FIELDS
    assert all_fields == expected


def test_rating_field_order_is_55_unique_names_no_age_column():
    assert len(d.RATING_FIELD_ORDER) == 55
    assert len(set(d.RATING_FIELD_ORDER)) == 55
    assert "peak_age_start" not in d.RATING_FIELD_ORDER
    assert "peak_age_end" not in d.RATING_FIELD_ORDER


def test_invariant_i1_every_field_sums_to_zero_across_positions():
    for field_name in d.RATING_FIELD_ORDER:
        total = sum(d.POSITION_RATING_OFFSETS[pos][field_name] for pos in _POSITIONS)
        assert abs(total) < 1e-9, (field_name, total)


def test_invariant_i2_every_position_sums_to_zero_across_fields():
    for pos in _POSITIONS:
        total = sum(d.POSITION_RATING_OFFSETS[pos].values())
        assert abs(total) < 1e-9, (pos, total)


def test_invariant_i3_position_neutral_fields_are_exactly_zero():
    assert d.POSITION_NEUTRAL_FIELDS == {
        "potential",
        "potential_variance",
        "work_ethic",
        "consistency",
        "coachability",
    }
    for field_name in d.POSITION_NEUTRAL_FIELDS:
        for pos in _POSITIONS:
            assert d.POSITION_RATING_OFFSETS[pos][field_name] == 0.0


def test_row_centering_correction_matches_the_doc_r_pos_over_50_row():
    """§6.3's two-row table: assert `r_pos/50`, not its negation (the
    delta applied). §12 step 1 names this row explicitly."""
    expected = {
        Position.PG: 1.076,
        Position.SG: 1.076,
        Position.SF: 0.796,
        Position.PF: -0.444,
        Position.C: -2.504,
    }
    for pos, value in expected.items():
        assert d._ROW_CORRECTION[pos] == pytest.approx(value, abs=0.001)


def test_centered_c_column_spot_check():
    assert d.POSITION_RATING_OFFSETS[Position.C]["rim_protection"] > 18
    assert d.POSITION_RATING_OFFSETS[Position.C]["catch_shoot_3"] < -12


def test_talent_variance_matches_the_doc_60_96():
    assert d.TALENT_VARIANCE == pytest.approx(60.96, abs=0.01)


@pytest.mark.parametrize(
    ("group", "expected_sd"),
    [
        ("PHYSICAL", 9.6927),
        ("CREATION", 11.1755),
        ("FINISHING", 11.1243),
        ("SHOOTING", 11.7624),
        ("PLAYMAKING", 10.6837),
        ("SCREENING", 14.1575),
        ("OFF_BALL", 10.2651),
        ("ON_BALL_DEF", 9.9999),
        ("TEAM_DEF", 9.6032),
        ("MENTAL", 9.8693),
        ("DEVELOPMENT", 10.0846),
    ],
)
def test_expected_group_sd_matches_pinned_model_output(group, expected_sd):
    """Pins `expected_group_sd`'s own computed output (not a re-typed
    band) so a future offset-table edit that silently drifts the model
    is caught by this fast unit test -- §6.6's actual pass band (±1.5 of
    whatever this function returns, measured against a real generated
    league at 20 seeds) is asserted by
    test_derivation_league_distribution.py, which is the test that
    exercises the generator rather than the model in isolation. `PHYSICAL`
    (~9.7) and `SCREENING` (~14.1) additionally match §6.6/§6.7's own
    printed floor/ceiling figures."""
    assert d.expected_group_sd(group) == pytest.approx(expected_sd, abs=1e-3)


def test_solved_tendency_spreads_match_the_doc_reference_values():
    expected = {
        "help_gamble_freq": 30.11,
        "pnr_screen_freq": 10.42,
        "spot_up_freq": 15.94,
        "iso_freq": 15.66,
    }
    for field_name, value in expected.items():
        assert d.TENDENCY_SPREADS[field_name] == pytest.approx(value, abs=0.05)


_EXPECTED_POS_ROWS: dict[str, dict[Position, float]] = {
    "drive_freq": {
        Position.PG: -2.43, Position.SG: -1.36, Position.SF: -0.08,
        Position.PF: 1.26, Position.C: 2.61,
    },
    "pullup_vs_catch": {
        Position.PG: -10.23, Position.SG: -3.17, Position.SF: 2.97,
        Position.PF: 7.51, Position.C: 2.92,
    },
    "iso_freq": {
        Position.PG: -7.30, Position.SG: -3.08, Position.SF: 1.37,
        Position.PF: 2.33, Position.C: 6.67,
    },
    "pnr_ball_freq": {
        Position.PG: -1.05, Position.SG: 0.71, Position.SF: -0.56,
        Position.PF: 0.57, Position.C: 0.32,
    },
    "pnr_screen_freq": {
        Position.PG: -0.32, Position.SG: 0.39, Position.SF: 0.24,
        Position.PF: 0.22, Position.C: -0.52,
    },
    "transition_freq": {
        Position.PG: -1.29, Position.SG: -0.40, Position.SF: 0.58,
        Position.PF: 0.25, Position.C: 0.86,
    },
    "pass_freq": {
        Position.PG: -4.67, Position.SG: -1.18, Position.SF: 0.63,
        Position.PF: 3.27, Position.C: 1.96,
    },
    "spot_up_freq": {
        Position.PG: -4.46, Position.SG: -4.37, Position.SF: -1.03,
        Position.PF: 2.29, Position.C: 7.57,
    },
    "cut_freq": {
        Position.PG: -2.51, Position.SG: -3.46, Position.SF: -0.76,
        Position.PF: 2.95, Position.C: 3.78,
    },
    "crash_oreb_freq": {
        Position.PG: -1.03, Position.SG: -0.10, Position.SF: 1.10,
        Position.PF: 0.82, Position.C: -0.79,
    },
    "help_gamble_freq": {
        Position.PG: -2.50, Position.SG: -4.51, Position.SF: -2.91,
        Position.PF: 2.67, Position.C: 7.26,
    },
    "steal_gamble_freq": {
        Position.PG: -5.73, Position.SG: -2.87, Position.SF: 0.36,
        Position.PF: 3.04, Position.C: 5.19,
    },
    "foul_freq": {
        Position.PG: 4.41, Position.SG: 3.10, Position.SF: 0.41,
        Position.PF: -2.33, Position.C: -5.60,
    },
}


@pytest.mark.parametrize("field_name", list(_EXPECTED_POS_ROWS))
def test_solved_pos_row_matches_the_doc_full_13x5_reference_table(field_name):
    row = d.TENDENCY_POSITION_ROWS[field_name]
    for pos, expected_value in _EXPECTED_POS_ROWS[field_name].items():
        assert row[pos] == pytest.approx(expected_value, abs=0.05), (field_name, pos)


def test_every_desired_row_and_every_solved_pos_row_sums_to_zero():
    for formula in d._TENDENCY_FORMULAS.values():
        assert abs(sum(formula.desired.values())) < 1e-9
    for row in d.TENDENCY_POSITION_ROWS.values():
        assert abs(sum(row.values())) < 1e-9
