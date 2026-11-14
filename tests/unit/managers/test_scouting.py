"""Tier 1: the disagreement mechanism -- §4.7's scouting draw and §4.3.1's
projected-peak model (docs/design/phase-2-manager-minds.md, §12 step 2).

Per §12 step 2's own verify list: determinism from the seed alone, two
teams' estimates of the same player differ, the pre-clamp draw is
unbiased (mean signed error < 0.5 over 10,000 draws), `scout_sd` stays in
its declared bounds, the Gap E regression test (a 34-year-old's
`projected_peak` equals his `ovr` exactly, a 21-year-old's exceeds it for
every residual value including the most negative), and no estimate
escapes `[0, 99]`.

**What this file does NOT verify, deliberately (§4.7, §12 step 2's own
text):** that two teams' *valuations* of the same player differ by
enough to clear a trade threshold. Two teams' scouted *estimates*
differing is necessary but not sufficient for that -- `managers/
valuation.py` (step 3) doesn't exist yet, so the market_value divergence
verify (>= 50% of players with `age_ramp > 0.25` differing by more than
`MIN_REL_GAIN`) belongs to step 3's test suite, not this one."""
from __future__ import annotations

import dataclasses
import statistics

import pytest

from dba2.managers.scouting import (
    ScoutedPeak,
    age_ramp,
    growth_residual,
    player_age,
    projected_peak,
    room_raw,
    scout_player,
    scout_roster_player,
    scout_sd,
    volatility,
)
from dba2.managers.snapshot import build_roster_player
from dba2.managers.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_player, make_ratings

_SEED = 424242


def _scout(
    *,
    team_id: int = 1,
    player_id: int = 100,
    season: int = 2026,
    ovr: int = 70,
    age: float = 25,
    peak_age_start: float = 27,
    potential: int = 70,
    potential_variance: int = 70,
    draft_eligible: bool = False,
    risk: float = 1.0,
    league_rng_seed: int = _SEED,
) -> ScoutedPeak:
    return scout_player(
        league_rng_seed=league_rng_seed,
        team_id=team_id,
        player_id=player_id,
        season=season,
        ovr=ovr,
        age=age,
        peak_age_start=peak_age_start,
        potential=potential,
        potential_variance=potential_variance,
        draft_eligible=draft_eligible,
        risk=risk,
    )


# ---------------------------------------------------------------------------
# Determinism + disagreement
# ---------------------------------------------------------------------------


def test_scout_player_is_deterministic_from_the_seed_alone():
    first = _scout(team_id=7, player_id=42, season=2026)
    second = _scout(team_id=7, player_id=42, season=2026)
    assert first == second


def test_two_teams_scouted_estimates_of_the_same_player_differ():
    team_a = _scout(team_id=1, player_id=42, age=22, peak_age_start=28)
    team_b = _scout(team_id=2, player_id=42, age=22, peak_age_start=28)
    assert team_a.pre_clamp_draw != team_b.pre_clamp_draw
    assert team_a.scouted_peak != team_b.scouted_peak


def test_different_seasons_and_players_also_draw_independent_streams():
    base = _scout(team_id=1, player_id=1, season=2026)
    other_player = _scout(team_id=1, player_id=2, season=2026)
    other_season = _scout(team_id=1, player_id=1, season=2027)
    assert base.pre_clamp_draw != other_player.pre_clamp_draw
    assert base.pre_clamp_draw != other_season.pre_clamp_draw


# ---------------------------------------------------------------------------
# Pre-clamp unbiasedness (§4.7's own stated bar)
# ---------------------------------------------------------------------------


def test_pre_clamp_draw_is_unbiased_over_10000_draws():
    # Fixed peak/sd, 10,000 independent (team, player) salts -- the floor
    # at `ovr` never binds here because we read `pre_clamp_draw` directly,
    # which is exactly why the verify is stated on this field.
    ovr = 70
    age = 22
    peak_age_start = 28
    potential = 74
    errors = []
    for player_id in range(10_000):
        result = _scout(
            team_id=1,
            player_id=player_id,
            ovr=ovr,
            age=age,
            peak_age_start=peak_age_start,
            potential=potential,
        )
        errors.append(result.pre_clamp_draw - result.projected_peak)
    mean_signed_error = statistics.mean(errors)
    assert abs(mean_signed_error) < 0.5, (
        f"pre-clamp draw is biased: mean signed error {mean_signed_error!r}"
    )


# ---------------------------------------------------------------------------
# scout_sd bounds
# ---------------------------------------------------------------------------


def test_scout_sd_in_bounds_for_every_rostered_player_in_a_synthetic_league():
    for ovr in range(0, 100, 5):
        for potential_variance in range(0, 100, 5):
            sd = scout_sd(potential_variance, ovr, draft_eligible=False)
            assert 1.5 <= sd <= 4.5, (ovr, potential_variance, sd)


def test_scout_sd_wider_range_for_draft_eligible_prospects():
    for ovr in range(0, 100, 5):
        for potential_variance in range(0, 100, 5):
            sd = scout_sd(potential_variance, ovr, draft_eligible=True)
            assert 2.5 <= sd <= 7.5, (ovr, potential_variance, sd)


def test_scout_sd_full_league_range_is_1_5_to_7_5():
    # The union of the rostered and prospect ranges -- the number the
    # design doc states as the headline range for "every player."
    regular = [
        scout_sd(pv, ovr, draft_eligible=False)
        for ovr in range(0, 100, 5)
        for pv in range(0, 100, 5)
    ]
    prospects = [
        scout_sd(pv, ovr, draft_eligible=True)
        for ovr in range(0, 100, 5)
        for pv in range(0, 100, 5)
    ]
    all_sds = regular + prospects
    assert min(all_sds) == pytest.approx(1.5)
    assert max(all_sds) == pytest.approx(7.5)


# ---------------------------------------------------------------------------
# Gap E regression test -- the reason §4.3.1 exists
# ---------------------------------------------------------------------------


def test_gap_e_regression_34_year_old_projected_peak_equals_ovr_exactly():
    ovr = 76
    age = 34
    for peak_age_start in (24, 25, 26, 27, 28):
        # Every residual value, including the most negative, per §12
        # step 2's own text -- residual only *scales* room_raw, so at
        # age_ramp == 0 the residual's sign and magnitude cannot matter.
        for potential in range(ovr - 40, ovr + 40, 4):
            peak = projected_peak(ovr, age, peak_age_start, potential)
            assert peak == pytest.approx(ovr), (
                f"34yo (peak_age_start={peak_age_start}, potential={potential}) "
                f"projected_peak={peak} != ovr={ovr}"
            )


def test_gap_e_regression_21_year_old_projected_peak_exceeds_ovr_always():
    ovr = 68
    age = 21
    for peak_age_start in (24, 25, 26, 27, 28):
        for potential in range(ovr - 40, ovr + 40, 4):
            peak = projected_peak(ovr, age, peak_age_start, potential)
            assert peak > ovr, (
                f"21yo (peak_age_start={peak_age_start}, potential={potential}) "
                f"projected_peak={peak} did not exceed ovr={ovr}"
            )


def test_gap_e_regression_most_negative_residual_still_exceeds_ovr_for_a_21yo():
    # The specific edge case named in the design doc: a maxed-out
    # negative residual (potential far below ovr) must still leave some
    # growth room for a young player, because RESIDUAL_GAIN=0.50 bounds
    # the residual's effect to +/-50% rather than letting it zero out
    # room_raw entirely.
    ovr = 70
    age = 21
    peak_age_start = 27
    peak = projected_peak(ovr, age, peak_age_start, potential=ovr - 100)
    assert peak > ovr


# ---------------------------------------------------------------------------
# No estimate escapes [0, 99]
# ---------------------------------------------------------------------------


def test_no_scouted_peak_escapes_0_99():
    for ovr in (0, 1, 40, 70, 99):
        for potential_variance in (0, 50, 99):
            for player_id in range(200):
                result = _scout(
                    team_id=1,
                    player_id=player_id,
                    ovr=ovr,
                    age=19,
                    peak_age_start=28,
                    potential=99,
                    potential_variance=potential_variance,
                    draft_eligible=True,
                )
                assert 0.0 <= result.scouted_peak <= 99.0, (ovr, player_id, result)


# ---------------------------------------------------------------------------
# age_ramp / growth_residual / room_raw building blocks
# ---------------------------------------------------------------------------


def test_age_ramp_is_zero_at_and_above_peak_age_start():
    assert age_ramp(28, 28) == 0.0
    assert age_ramp(30, 28) == 0.0


def test_age_ramp_denominator_guard_against_low_peak_age_start():
    # peak_age_start at or below RAMP_ORIGIN_AGE (19) must not divide by
    # zero or flip sign -- RAMP_MIN_SPAN floors the denominator at 1.0.
    ramp = age_ramp(18, 19, tuning=DEFAULT_TUNING)
    assert 0.0 <= ramp <= 1.0


def test_growth_residual_is_clamped_to_unit_range():
    assert growth_residual(200, 0) == pytest.approx(1.0)
    assert growth_residual(-200, 0) == pytest.approx(-1.0)


def test_volatility_is_clamped_to_unit_range_and_shares_residual_cap():
    assert volatility(200, 0) == pytest.approx(1.0)
    assert volatility(-200, 0) == pytest.approx(-1.0)
    assert volatility(70, 70) == pytest.approx(0.0)


def test_room_raw_bounded_below_by_half_growth_max_at_full_ramp():
    # age_ramp == 1.0, residual == -1.0 (worst case): room_raw should be
    # exactly GROWTH_MAX * 1.0 * 0.5.
    room = room_raw(age=19, peak_age_start=27, potential=-1000, ovr=0)
    assert room == pytest.approx(DEFAULT_TUNING.growth_max * 0.5)


# ---------------------------------------------------------------------------
# The risk-weighted tail adjustment (§6)
# ---------------------------------------------------------------------------


def test_effective_peak_equals_scouted_peak_at_or_past_peak_age_start():
    result = _scout(age=30, peak_age_start=28, risk=1.60)
    assert result.effective_peak == pytest.approx(result.scouted_peak)


def test_effective_peak_applies_risk_tail_below_peak_age_start():
    result = _scout(age=21, peak_age_start=28, risk=1.60)
    expected = result.scouted_peak + (1.60 - 1.0) * result.scout_sd
    assert result.effective_peak == pytest.approx(expected)


def test_effective_peak_at_neutral_risk_equals_scouted_peak():
    result = _scout(age=21, peak_age_start=28, risk=1.0)
    assert result.effective_peak == pytest.approx(result.scouted_peak)


def test_risk_averse_gm_prices_below_risk_loving_gm_for_a_young_prospect():
    averse = _scout(team_id=1, age=21, peak_age_start=28, risk=0.85)
    loving = _scout(team_id=1, age=21, peak_age_start=28, risk=1.60)
    assert loving.effective_peak > averse.effective_peak
    # Both draw the same scouted_peak (same seed/team/player/season) --
    # only the tail term differs.
    assert loving.scouted_peak == pytest.approx(averse.scouted_peak)


# ---------------------------------------------------------------------------
# player_age + the RosterPlayer wrapper
# ---------------------------------------------------------------------------


def test_player_age_matches_seed_dev_league_convention():
    import datetime

    assert player_age(datetime.date(1998, 1, 1), 2026) == 28


def test_scout_roster_player_matches_scout_player_on_the_same_inputs():
    player = make_player(1, overall_cached=72)
    ratings = dataclasses.replace(
        make_ratings(1, potential=80, potential_variance=75), peak_age_start=27
    )
    roster_player = build_roster_player(player, ratings)

    via_wrapper = scout_roster_player(
        roster_player,
        league_rng_seed=_SEED,
        team_id=3,
        season=2026,
        risk=1.0,
    )
    via_primitives = scout_player(
        league_rng_seed=_SEED,
        team_id=3,
        player_id=1,
        season=2026,
        ovr=72,
        age=player_age(player.birth_date, 2026),
        peak_age_start=27,
        potential=80,
        potential_variance=75,
        draft_eligible=False,
        risk=1.0,
    )
    assert via_wrapper == via_primitives


def test_scout_roster_player_reads_draft_eligible_status():
    from dba2.domain.player import RosterStatus

    player = dataclasses.replace(
        make_player(2, overall_cached=60, team_id=None),
        roster_status=RosterStatus.DRAFT_ELIGIBLE,
    )
    ratings = make_ratings(2)
    roster_player = build_roster_player(player, ratings)

    result = scout_roster_player(
        roster_player, league_rng_seed=_SEED, team_id=5, season=2026, risk=1.0
    )
    expected_sd = scout_sd(
        ratings.potential_variance, player.overall_cached, draft_eligible=True
    )
    assert result.scout_sd == pytest.approx(expected_sd)
