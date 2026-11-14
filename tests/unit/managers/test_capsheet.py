"""Tier 1: `managers/capsheet.py` -- Decision P2-15's nine quantities,
Decision P2-16's `projected` flag, and Decision P2-17's season-0 exactness
requirement (docs/design/phase-2-manager-minds.md §8, §12 step 7's own
verify list, followed precisely).

`test_compute_team_season_cap_sheet_matches_hand_computed_dollar_figures`
and `test_season_0_row_is_never_projected_and_uses_the_real_row_verbatim`
are this file's load-bearing pair: the first proves the single-season
formula against numbers computed by hand outside the module under test,
the second is the unit-level half of Decision P2-17's cross-check
(`tests/integration/test_capsheet_service.py::
test_project_league_cap_sheets_season_0_matches_get_cap_summary_exactly`
is the DB-backed half that closes the loop against the real
`cap_service.get_cap_summary`)."""
from __future__ import annotations

import pytest

from dba2.managers.capsheet import (
    CapSheetTeamSeasonInputs,
    compute_team_season_cap_sheet,
    project_cap_settings,
    project_team_cap_sheet,
)
from dba2.managers.tuning import DEFAULT_TUNING, GmTuning
from tests.fixtures.managers import CAP_SETTINGS

TEAM_ID = 7
SEASON = CAP_SETTINGS.season  # 2026


def _inputs(
    *,
    season: int = SEASON,
    committed_salary: int = 0,
    dead_cap: int = 0,
    cap_holds: int = 0,
    team_option_salary: int = 0,
    player_option_salary: int = 0,
    roster_count: int = 0,
) -> CapSheetTeamSeasonInputs:
    return CapSheetTeamSeasonInputs(
        team_id=TEAM_ID,
        season=season,
        committed_salary=committed_salary,
        dead_cap=dead_cap,
        cap_holds=cap_holds,
        team_option_salary=team_option_salary,
        player_option_salary=player_option_salary,
        roster_count=roster_count,
    )


def test_compute_team_season_cap_sheet_matches_hand_computed_dollar_figures():
    """A hand-built team: 12 players under contract, $120M committed,
    $3M dead cap, $2M in cap holds, a $5M team option and a $4M player
    option (both already inside `committed_salary`, per the module's own
    documented reading of item 8), against `CAP_SETTINGS`'s real
    2025-26 figures (salary_cap=154,647,000, luxury_tax=187,895,000,
    apron_1=195,945,000, apron_2=207,824,000, min_salary=1,272,870,
    roster_min=14).

    Hand computation: minimum_filler = max(0, 14 - 12) * 1,272,870 =
    2 * 1,272,870 = 2,545,740. used = 120,000,000 + 3,000,000 +
    2,545,740 = 125,545,740 (cap_holds excluded, include_holds=False).
    room = 154,647,000 - 125,545,740 = 29,101,260. tax_room =
    187,895,000 - 125,545,740 = 62,349,260. apron1_room =
    195,945,000 - 125,545,740 = 70,399,260. apron2_room =
    207,824,000 - 125,545,740 = 82,278,260."""
    inputs = _inputs(
        committed_salary=120_000_000,
        dead_cap=3_000_000,
        cap_holds=2_000_000,
        team_option_salary=5_000_000,
        player_option_salary=4_000_000,
        roster_count=12,
    )
    result = compute_team_season_cap_sheet(
        inputs, CAP_SETTINGS, projected=False, include_holds=False
    )

    assert result.team_id == TEAM_ID
    assert result.season == SEASON
    assert result.committed_salary == 120_000_000
    assert result.dead_cap == 3_000_000
    assert result.cap_holds == 2_000_000
    assert result.team_option_salary == 5_000_000
    assert result.player_option_salary == 4_000_000
    assert result.roster_count == 12
    assert result.minimum_filler == 2_545_740
    assert result.used == 125_545_740
    assert result.room == 29_101_260
    assert result.tax_room == 62_349_260
    assert result.apron1_room == 70_399_260
    assert result.apron2_room == 82_278_260
    assert result.include_holds is False
    assert result.projected is False


def test_minimum_filler_is_nonzero_for_a_12_man_roster():
    """§8 item 7's own stated reason this module exists as its own step --
    a 12-man roster under `CAP_SETTINGS.roster_min == 14` must carry a
    nonzero filler term, not silently report room that doesn't exist."""
    inputs = _inputs(committed_salary=50_000_000, roster_count=12)
    result = compute_team_season_cap_sheet(inputs, CAP_SETTINGS, projected=False)
    assert result.minimum_filler == 2 * CAP_SETTINGS.min_salary
    assert result.minimum_filler > 0


def test_minimum_filler_is_zero_for_a_full_legal_roster():
    inputs = _inputs(committed_salary=50_000_000, roster_count=15)
    result = compute_team_season_cap_sheet(inputs, CAP_SETTINGS, projected=False)
    assert result.minimum_filler == 0


def test_cap_holds_always_computed_but_only_added_to_used_when_asked():
    """Mirrors `cap_service.get_cap_summary`'s `include_holds` contract
    exactly (§8 item 3)."""
    inputs = _inputs(committed_salary=10_000_000, cap_holds=9_000_000, roster_count=15)

    without = compute_team_season_cap_sheet(
        inputs, CAP_SETTINGS, projected=False, include_holds=False
    )
    assert without.cap_holds == 9_000_000
    assert without.used == 10_000_000
    assert without.include_holds is False

    with_holds = compute_team_season_cap_sheet(
        inputs, CAP_SETTINGS, projected=False, include_holds=True
    )
    assert with_holds.cap_holds == 9_000_000
    assert with_holds.used == 19_000_000
    assert with_holds.include_holds is True


def test_option_salaries_are_reported_but_not_double_counted_into_used():
    """§8 items 4/5: `team_option_salary`/`player_option_salary` are a
    breakdown *of* `committed_salary` here (see module docstring's
    judgment call), so `used` must not add them a second time."""
    inputs = _inputs(
        committed_salary=100_000_000,
        team_option_salary=8_000_000,
        player_option_salary=6_000_000,
        roster_count=15,
    )
    result = compute_team_season_cap_sheet(inputs, CAP_SETTINGS, projected=False)
    assert result.team_option_salary == 8_000_000
    assert result.player_option_salary == 6_000_000
    assert result.used == 100_000_000


def test_project_cap_settings_grows_only_dollar_fields():
    grown = project_cap_settings(CAP_SETTINGS, CAP_SETTINGS.season + 2, tuning=DEFAULT_TUNING)
    growth = DEFAULT_TUNING.cap_growth**2

    assert grown.season == CAP_SETTINGS.season + 2
    assert grown.salary_cap == round(CAP_SETTINGS.salary_cap * growth)
    assert grown.luxury_tax == round(CAP_SETTINGS.luxury_tax * growth)
    assert grown.apron_1 == round(CAP_SETTINGS.apron_1 * growth)
    assert grown.apron_2 == round(CAP_SETTINGS.apron_2 * growth)
    assert grown.min_salary == round(CAP_SETTINGS.min_salary * growth)
    assert grown.mle_amount == round(CAP_SETTINGS.mle_amount * growth)
    assert grown.bi_annual_amount == round(CAP_SETTINGS.bi_annual_amount * growth)

    # Percentage/count fields are rule constants, not dollar figures --
    # carried over unchanged.
    assert grown.max_pct_0_6 == CAP_SETTINGS.max_pct_0_6
    assert grown.max_pct_7_9 == CAP_SETTINGS.max_pct_7_9
    assert grown.max_pct_10_plus == CAP_SETTINGS.max_pct_10_plus
    assert grown.supermax_pct == CAP_SETTINGS.supermax_pct
    assert grown.raise_pct_bird == CAP_SETTINGS.raise_pct_bird
    assert grown.raise_pct_non_bird == CAP_SETTINGS.raise_pct_non_bird
    assert grown.roster_min == CAP_SETTINGS.roster_min
    assert grown.roster_max == CAP_SETTINGS.roster_max


def test_a_future_season_with_no_real_cap_settings_row_is_flagged_projected():
    """Decision P2-16: a horizon season with no real `league_cap_settings`
    row must carry `projected=True`, and its cap figures must be the
    latest real row grown at `CAP_GROWTH` per season out."""
    horizon = tuple(_inputs(season=CAP_SETTINGS.season + i, roster_count=15) for i in range(5))
    real_cap_settings = {CAP_SETTINGS.season: CAP_SETTINGS}

    results = project_team_cap_sheet(horizon, real_cap_settings, tuning=DEFAULT_TUNING)

    assert len(results) == 5
    assert results[0].projected is False
    assert results[0].room == CAP_SETTINGS.salary_cap  # season 0: no committed $, full room

    for i in range(1, 5):
        assert results[i].projected is True
        expected_growth = DEFAULT_TUNING.cap_growth**i
        expected_cap = round(CAP_SETTINGS.salary_cap * expected_growth)
        assert results[i].room == expected_cap


def test_season_0_row_is_never_projected_and_uses_the_real_row_verbatim():
    horizon = tuple(_inputs(season=CAP_SETTINGS.season + i, roster_count=15) for i in range(5))
    real_cap_settings = {CAP_SETTINGS.season: CAP_SETTINGS}
    results = project_team_cap_sheet(horizon, real_cap_settings, tuning=DEFAULT_TUNING)
    assert results[0].projected is False
    assert results[0].room == CAP_SETTINGS.salary_cap - results[0].used


def test_project_team_cap_sheet_uses_a_later_real_row_when_present():
    """A real row for season+2 (a league admin pre-seeded next-next
    season's cap settings) must be used verbatim, not grown from
    season+0 -- growth only applies to seasons with no real row at all."""
    later_real = project_cap_settings(CAP_SETTINGS, CAP_SETTINGS.season + 2, tuning=DEFAULT_TUNING)
    # Perturb the "real" row so it's distinguishable from a grown one.
    import dataclasses

    later_real = dataclasses.replace(later_real, salary_cap=later_real.salary_cap + 999)

    horizon = tuple(_inputs(season=CAP_SETTINGS.season + i, roster_count=15) for i in range(5))
    real_cap_settings = {CAP_SETTINGS.season: CAP_SETTINGS, CAP_SETTINGS.season + 2: later_real}
    results = project_team_cap_sheet(horizon, real_cap_settings, tuning=DEFAULT_TUNING)

    assert results[2].projected is False
    assert results[2].room == later_real.salary_cap
    # season+1 still has no real row, so it's grown from season 0, not
    # from the later real row (which is in the future relative to it).
    assert results[1].projected is True
    assert results[1].room == round(CAP_SETTINGS.salary_cap * DEFAULT_TUNING.cap_growth**1)
    # season+3/+4 grow forward from the later real row at season+2, not
    # from season 0's.
    assert results[3].projected is True
    assert results[3].room == round(later_real.salary_cap * DEFAULT_TUNING.cap_growth**1)


def test_project_team_cap_sheet_raises_without_a_real_anchor_at_or_before_first_season():
    horizon = (_inputs(season=CAP_SETTINGS.season, roster_count=15),)
    with pytest.raises(ValueError):
        project_team_cap_sheet(horizon, {}, tuning=DEFAULT_TUNING)


def test_project_team_cap_sheet_raises_on_empty_inputs():
    with pytest.raises(ValueError):
        project_team_cap_sheet((), {CAP_SETTINGS.season: CAP_SETTINGS}, tuning=DEFAULT_TUNING)


def test_horizon_matches_default_tuning():
    """Decision P2-15/tuning.py's own cross-reference: the projection
    horizon and `GmTuning.horizon` are the same constant, deliberately."""
    assert DEFAULT_TUNING.horizon == 5
    assert isinstance(DEFAULT_TUNING, GmTuning)
