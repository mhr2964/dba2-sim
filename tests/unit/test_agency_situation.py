"""`dba2/agency/situation.py` -- `PlayerSituation` and its five axis
scorers (Decision P4-9). §10 test 2's totality-style sweep, applied here
to the scorers rather than `StoryFrame` (this phase's media analogue,
Stage D)."""
from __future__ import annotations

from dba2.agency.situation import (
    AXIS_SCORERS,
    PlayerSituation,
    score_legacy,
    score_loyalty,
    score_money,
    score_role_usage,
    score_winning,
    window_success_score,
)
from dba2.domain.motivation import MotivationAxis
from dba2.managers.tuning import CompetitiveWindow


def test_all_fields_none_scores_neutral_zero_on_every_axis():
    """P4-9: "None is neutral, never zero" -- for a scorer, "neutral"
    means a zero CONTRIBUTION (no satisfaction pressure either way), not
    a punitive reading."""
    situation = PlayerSituation()
    for scorer in AXIS_SCORERS.values():
        assert scorer(situation) == 0.0


def test_axis_scorers_dict_covers_every_motivation_axis():
    assert set(AXIS_SCORERS) == set(MotivationAxis)


def test_axis_scorers_iterates_in_motivation_axis_declaration_order():
    """Round-5 critic finding B3: `evaluate_satisfaction`'s docstring
    claims emission order matches `MotivationAxis` declaration order, and
    `largest_magnitude_axis`'s tie-break assumes the same -- set equality
    alone (the test above) does not catch a swap. Order-sensitive on
    purpose, per P4-44's own "emission order is part of the contract, not
    an implementation accident" rule."""
    assert list(AXIS_SCORERS.keys()) == list(MotivationAxis)


def test_score_winning_neutral_at_midpoint_and_bounded():
    assert score_winning(PlayerSituation(team_success=0.5)) == 0.0
    assert score_winning(PlayerSituation(team_success=1.0)) == 1.0
    assert score_winning(PlayerSituation(team_success=0.0)) == -1.0
    # A wildly out-of-range input (caller bug) still clamps, never raises
    # or returns something outside [-1, 1].
    assert score_winning(PlayerSituation(team_success=5.0)) == 1.0


def test_score_winning_monotonic_in_team_success():
    low = score_winning(PlayerSituation(team_success=0.2))
    high = score_winning(PlayerSituation(team_success=0.8))
    assert high > low


def test_score_money_neutral_at_market_value_and_signed_around_it():
    assert score_money(PlayerSituation(pay_vs_market=1.0)) == 0.0
    assert score_money(PlayerSituation(pay_vs_market=1.5)) > 0.0  # overpaid -> satisfied
    assert score_money(PlayerSituation(pay_vs_market=0.5)) < 0.0  # underpaid -> dissatisfied


def test_score_loyalty_zero_at_fresh_acquisition_and_saturates():
    assert score_loyalty(PlayerSituation(tenure_seasons=0.0)) == 0.0
    assert score_loyalty(PlayerSituation(tenure_seasons=1000.0)) == 1.0
    # Never negative -- loyalty is a plain non-negative reading, not a
    # signed contribution around some "neutral tenure" midpoint.
    assert score_loyalty(PlayerSituation(tenure_seasons=0.5)) >= 0.0


def test_score_role_usage_averages_only_present_components():
    """A real `role_fit` reading is not diluted to zero just because
    `minutes_share` hasn't been computed yet this checkpoint."""
    only_role_fit = score_role_usage(PlayerSituation(role_fit=2.0))
    only_minutes = score_role_usage(PlayerSituation(minutes_share=1.0))
    both = score_role_usage(PlayerSituation(role_fit=2.0, minutes_share=1.0))

    assert only_role_fit > 0.0
    assert only_minutes > 0.0
    # Averaging two positive, saturated-at-1.0 components should read the
    # same as either alone reading a saturated 1.0 -- not half of it.
    assert both == only_role_fit == only_minutes == 1.0


def test_score_legacy_averages_only_present_components():
    only_market = score_legacy(PlayerSituation(market_match=1.0))
    only_accolades = score_legacy(PlayerSituation(team_accolades=1.0))
    both = score_legacy(PlayerSituation(market_match=1.0, team_accolades=1.0))

    assert only_market == only_accolades == both == 1.0


def test_score_legacy_neutral_component_does_not_move_the_axis():
    assert score_legacy(PlayerSituation(market_match=0.5, team_accolades=0.5)) == 0.0


# ---------------------------------------------------------------------------
# `window_success_score` -- the shared DB/harness builder helper, pure, so
# both `services/agency_service.py` and `tools/gm_harness/` normalize a
# window identically rather than risking drift. Round-5 critic finding B2:
# `pay_vs_market`'s own shared helper, `expected_market_salary`, was an
# uncalibrated proxy this module invented -- it has been removed. Both call
# sites now read `managers/valuation.py::market_salary_share(effective_
# talent_ovr(...), cap_settings) * cap_settings.salary_cap` directly --
# NOT `market_value_for`, which this round found returns a multi-season
# discounted trade-equity score, not a dollar figure comparable to one
# season's salary (see `services/agency_service.py::build_player_
# situation`'s comment, and the design doc's P4-9/P4-63 correction, for
# the full derivation). `market_salary_share` already lives in, and is
# tested by, `tests/unit/managers/test_valuation.py`; nothing about
# wrapping it here would add coverage, only a second copy of its
# signature to keep in sync.
# ---------------------------------------------------------------------------


def test_window_success_score_none_is_none():
    assert window_success_score(None) is None


def test_window_success_score_is_monotonic_and_bounded():
    assert window_success_score(CompetitiveWindow.REBUILD) == 0.0
    assert window_success_score(CompetitiveWindow.CONTEND) == 1.0
    assert (
        window_success_score(CompetitiveWindow.REBUILD)
        < window_success_score(CompetitiveWindow.RETOOL)
        < window_success_score(CompetitiveWindow.WIN_NOW)
        < window_success_score(CompetitiveWindow.CONTEND)
    )
