"""Tier 1/3: market value, no team context -- §4.1-§4.5's formulas and
§12 step 3's own verify list (docs/design/phase-2-manager-minds.md).

Per §12 step 3's own text, this step is "the artifact every later step
consumes and the one whose wrongness is most invisible," so its verify
list is followed precisely rather than sampled from: the golden archetype
table, the two required monotonicity properties, the star-to-bench
ratio ordering, the negative-value case, Decision P2-6's real identity,
the `effective_ovr <= effective_peak` clamp, Decision P2-26's option
orderings over a grid (not four corners), and -- named in the design doc
as the harder bar step 2 deliberately deferred here -- the disagreement
channel asserted on *valuations*, not on scouted estimates.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from dba2.domain.contract import OptionType
from dba2.domain.team import GmPhilosophy
from dba2.managers.philosophy import PROFILES
from dba2.managers.scouting import scout_player
from dba2.managers.tuning import DEFAULT_TUNING, WINDOW_PARAMS, CompetitiveWindow
from dba2.managers.valuation import (
    age_mult_eff,
    base_value,
    effective_ovr_t,
    effective_talent_ovr,
    market_salary_share,
    market_value,
    market_value_for,
    season_contribution,
    upside_weight_eff,
)
from tests.fixtures.managers import (
    ARCHETYPES,
    CAP_SETTINGS,
    generate_archetype_table,
    make_contract,
    make_roster_player,
)

_GOLDEN_PATH = (
    Path(__file__).resolve().parents[3] / "seeds" / "gm_value_archetypes.json"
)


# ---------------------------------------------------------------------------
# The golden archetype table (§10, §12 step 3's own deliverable)
# ---------------------------------------------------------------------------


def test_archetype_table_matches_the_checked_in_golden_file():
    golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    actual = json.loads(json.dumps(generate_archetype_table(), sort_keys=True))
    assert actual == golden["archetypes"], (
        "computed archetype table no longer matches seeds/gm_value_archetypes.json "
        "-- if this is an intentional tuning change, regenerate the golden file in "
        "the same commit that bumps GM_TUNING_VERSION, per §10/§12 step 3"
    )


def test_archetype_table_covers_all_12_rows_and_28_columns():
    table = generate_archetype_table()
    assert len(table) == 12
    for label, row in table.items():
        assert len(row) == 28, f"{label}: expected 28 (philosophy, window) columns, got {len(row)}"
        for philosophy in GmPhilosophy:
            for window in CompetitiveWindow:
                assert f"{philosophy.value}:{window.value}" in row


# ---------------------------------------------------------------------------
# Decision P2-6's real identity
# ---------------------------------------------------------------------------


def test_base_value_at_70_is_100_for_every_kappa():
    for kappa in sorted({p.kappa for p in PROFILES.values()}):
        assert base_value(70.0, kappa) == pytest.approx(100.0)


def test_market_salary_at_70_is_exactly_10_percent_of_cap():
    assert market_salary_share(70.0, CAP_SETTINGS) == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# Monotonicity -- in overall, and in added positive-surplus years
# ---------------------------------------------------------------------------


def test_market_value_is_monotone_in_overall():
    season = 2026
    contract = make_contract(900, start_season=season, salaries=(15_000_000,) * 4)
    profile = PROFILES[GmPhilosophy.TENDENCY_RESPECTER]
    window = WINDOW_PARAMS[CompetitiveWindow.RETOOL]
    prior = None
    for ovr in (60, 65, 70, 75, 80, 85, 90):
        roster_player = make_roster_player(
            900, age=27, season=season, overall_cached=ovr, contract=contract,
            peak_age_start=27, peak_age_end=31,
        )
        scouted = scout_player(
            league_rng_seed=42, team_id=1, player_id=900, season=season, ovr=ovr,
            age=27, peak_age_start=27, potential=ovr, potential_variance=ovr,
            draft_eligible=False, risk=profile.risk,
        )
        value = market_value(roster_player, ovr, scouted, profile, window, CAP_SETTINGS, season)
        if prior is not None:
            assert value > prior, (
                f"overall {ovr}: market_value did not increase ({value} <= {prior})"
            )
        prior = value


def test_market_value_increases_with_an_added_positive_surplus_year():
    """Appending an extra season at a bargain (well-below-market) salary
    to an already-guaranteed contract must raise total market_value --
    the design's own framing for why this property has to hold ("added
    positive-surplus years")."""
    season = 2026
    profile = PROFILES[GmPhilosophy.TENDENCY_RESPECTER]
    window = WINDOW_PARAMS[CompetitiveWindow.RETOOL]
    ovr = 76

    def _value(salaries: tuple[int, ...]) -> float:
        contract = make_contract(901, start_season=season, salaries=salaries)
        roster_player = make_roster_player(
            901, age=27, season=season, overall_cached=ovr, contract=contract,
            peak_age_start=30, peak_age_end=34,
        )
        scouted = scout_player(
            league_rng_seed=42, team_id=1, player_id=901, season=season, ovr=ovr,
            age=27, peak_age_start=30, potential=ovr, potential_variance=ovr,
            draft_eligible=False, risk=profile.risk,
        )
        return market_value(roster_player, ovr, scouted, profile, window, CAP_SETTINGS, season)

    bargain_salary = 2_000_000  # well below market_salary(76) ~= 18% of cap
    three_years = _value((bargain_salary,) * 3)
    four_years = _value((bargain_salary,) * 4)
    assert four_years > three_years


# ---------------------------------------------------------------------------
# star_maxer's star-to-bench ratio strictly exceeds egalitarian's
# ---------------------------------------------------------------------------


def test_star_maxer_star_to_bench_ratio_exceeds_egalitarian():
    star_maxer = PROFILES[GmPhilosophy.STAR_MAXER]
    egalitarian = PROFILES[GmPhilosophy.EGALITARIAN]
    star_maxer_ratio = base_value(84.0, star_maxer.kappa) / base_value(63.0, star_maxer.kappa)
    egalitarian_ratio = base_value(84.0, egalitarian.kappa) / base_value(63.0, egalitarian.kappa)
    assert star_maxer_ratio > egalitarian_ratio


# ---------------------------------------------------------------------------
# A player owed above-market money for three years values negative
# ---------------------------------------------------------------------------


def test_player_owed_above_market_money_for_three_years_values_negative():
    season = 2026
    profile = PROFILES[GmPhilosophy.TENDENCY_RESPECTER]
    window = WINDOW_PARAMS[CompetitiveWindow.RETOOL]
    ovr = 70  # market_salary(70) == 10% of cap
    massively_overpaid_salary = round(CAP_SETTINGS.salary_cap * 0.34)  # >> 10% of cap
    contract = make_contract(902, start_season=season, salaries=(massively_overpaid_salary,) * 3)
    roster_player = make_roster_player(
        902, age=30, season=season, overall_cached=ovr, contract=contract,
        peak_age_start=27, peak_age_end=31,  # past peak -- no upside term muddying the sign
    )
    scouted = scout_player(
        league_rng_seed=42, team_id=1, player_id=902, season=season, ovr=ovr,
        age=30, peak_age_start=27, potential=ovr, potential_variance=ovr,
        draft_eligible=False, risk=profile.risk,
    )
    value = market_value(roster_player, ovr, scouted, profile, window, CAP_SETTINGS, season)
    assert value < 0.0, (
        f"expected a negative market_value for a 3-year massive overpay, got {value}"
    )


# ---------------------------------------------------------------------------
# effective_ovr <= effective_peak, for every archetype under every pair
# ---------------------------------------------------------------------------


def test_effective_ovr_never_exceeds_effective_peak_across_the_whole_archetype_grid():
    for archetype in ARCHETYPES:
        roster_player = archetype.roster_player
        player = roster_player.player
        ratings = roster_player.ratings
        for philosophy in GmPhilosophy:
            profile = PROFILES[philosophy]
            ovr = effective_talent_ovr(roster_player, profile)
            age = float(archetype.season - player.birth_date.year)
            scouted = scout_player(
                league_rng_seed=archetype.league_rng_seed,
                team_id=archetype.team_id,
                player_id=player.id,
                season=archetype.season,
                ovr=ovr,
                age=age,
                peak_age_start=ratings.peak_age_start,
                potential=ratings.potential,
                potential_variance=ratings.potential_variance,
                draft_eligible=False,
                risk=profile.risk,
            )
            confidence = roster_player.confidence_f
            for window_key in CompetitiveWindow:
                window = WINDOW_PARAMS[window_key]
                uw_eff = upside_weight_eff(profile.upside_weight, window.window_upside, confidence)
                for t in range(5):
                    eff_ovr = effective_ovr_t(
                        ovr, scouted.effective_peak, age, t, ratings.peak_age_start, uw_eff,
                    )
                    assert eff_ovr <= scouted.effective_peak + 1e-9, (
                        archetype.label, philosophy, window_key, t, eff_ovr, scouted.effective_peak
                    )


# ---------------------------------------------------------------------------
# Decision P2-9's tilted entry point -- effective_talent_ovr's own contract
# ---------------------------------------------------------------------------


def test_effective_talent_ovr_reads_overall_cached_for_non_tilted_philosophies():
    archetype = ARCHETYPES[0]
    for philosophy in GmPhilosophy:
        if philosophy is GmPhilosophy.DEFENSE_FIRST:
            continue
        profile = PROFILES[philosophy]
        assert effective_talent_ovr(archetype.roster_player, profile) == (
            archetype.roster_player.player.overall_cached
        )


def test_effective_talent_ovr_differs_from_overall_cached_for_defense_first_on_a_lopsided_player():
    # defense_first_wing has deliberately lopsided shooting/creation-vs-
    # defense ratings, so the tilt must move its overall away from the
    # stored default-weighted overall_cached.
    wing = next(a for a in ARCHETYPES if a.label == "defense_first_wing").roster_player
    tilted = effective_talent_ovr(wing, PROFILES[GmPhilosophy.DEFENSE_FIRST])
    assert tilted != wing.player.overall_cached


# ---------------------------------------------------------------------------
# Decision P2-26's orderings, over the doc's own stated grid
# ---------------------------------------------------------------------------


def test_option_orderings_hold_over_the_full_ovr_surplus_kappa_grid():
    kappas = sorted({p.kappa for p in PROFILES.values()})
    max_relative_premium = 0.0
    checked = 0
    for kappa in kappas:
        for ovr in range(50, 96):
            for surplus in range(-2000, 201, 5):
                talent = base_value(float(ovr), kappa)
                v_t = talent + surplus
                team_option = season_contribution(v_t, surplus, OptionType.TEAM)
                player_option = season_contribution(v_t, surplus, OptionType.PLAYER)
                guaranteed = season_contribution(v_t, surplus, OptionType.NONE)
                assert guaranteed == pytest.approx(v_t)

                # 1. team option >= guaranteed, always.
                assert team_option >= guaranteed - 1e-9, (kappa, ovr, surplus)
                # 2. team option > player option, at every row.
                assert team_option > player_option, (kappa, ovr, surplus)
                # 3. player option <= guaranteed whenever the guaranteed year is
                #    worth having (v_t > 0).
                if v_t > 0:
                    assert player_option <= guaranteed + 1e-9, (kappa, ovr, surplus)
                # 4. the relative premium bound, wherever v_t < 0. The player
                #    option prices ABOVE the guaranteed year on a liability
                #    year (adverse selection: he opts in when overpaid), so
                #    the premium is (player_option - guaranteed), not the
                #    reverse -- critic round 8, B1: the swapped subtraction
                #    made this assertion permanently vacuous (always <= 0,
                #    true even with q_t forced to 0, a 100% premium).
                if v_t < 0:
                    relative_premium = (player_option - guaranteed) / abs(guaranteed)
                    max_relative_premium = max(max_relative_premium, relative_premium)
                    assert relative_premium < 0.45, (kappa, ovr, surplus, relative_premium)
                checked += 1
    assert checked > 10_000, "grid too sparse to be a meaningful check"
    assert max_relative_premium < 0.45


def test_team_option_never_haircut_at_market():
    # "a team option at market is not haircut" -- surplus 0, the premium
    # over guaranteed is negligible (§4.4).
    v_t = base_value(84.0, 7.5) + 0.0
    team_option = season_contribution(v_t, 0.0, OptionType.TEAM)
    assert team_option - v_t < 0.01


def test_player_option_worth_more_than_guaranteed_on_a_deep_liability_year():
    # §4.4's own named counterexample: a badly negative V_t prices HIGHER
    # as a player option than as a guaranteed year (adverse selection --
    # the player might walk anyway).
    v_t = base_value(63.0, 7.5) - 200.0
    player_option = season_contribution(v_t, -200.0, OptionType.PLAYER)
    guaranteed = season_contribution(v_t, -200.0, OptionType.NONE)
    assert player_option > guaranteed


# ---------------------------------------------------------------------------
# The disagreement channel, asserted on *valuations* (§4.7's harder bar,
# deliberately deferred by step 2 to this step -- §14's "Trade deadlock"
# risk row names this test as the actual mitigation).
# ---------------------------------------------------------------------------


def _synthetic_league_players(count: int, *, season: int) -> list:
    """`count` distinct rostered players, ages spanning young (real growth
    room, `age_ramp > 0.25`) through old (none), each on a plain 3-year
    guaranteed deal near market. Not itself an archetype -- deliberately
    separate from the checked-in golden table, since this is a
    distributional check over many players, not a pinned few."""
    players = []
    for i in range(count):
        age = 20 + (i % 14)  # 20..33, spans well above and below peak_age_start
        peak_age_start = 27 + (i % 3)  # 27..29
        ovr = 65 + (i % 20)  # 65..84
        potential = ovr + ((i * 7) % 15) - 5  # spread residuals both signs
        salary = round(0.10 * CAP_SETTINGS.salary_cap * (0.8 + 0.02 * (i % 10)))
        contract = make_contract(2000 + i, start_season=season, salaries=(salary,) * 3)
        roster_player = make_roster_player(
            2000 + i, age=age, season=season, overall_cached=ovr, contract=contract,
            peak_age_start=peak_age_start, peak_age_end=peak_age_start + 4,
            ratings_overrides={"potential": max(0, min(99, potential)), "potential_variance": ovr},
        )
        players.append((roster_player, age, peak_age_start))
    return players


def test_market_value_divergence_across_teams_for_players_with_real_growth_room():
    season = 2026
    league_rng_seed = 909090
    window = CompetitiveWindow.RETOOL
    rng = random.Random(1234)

    players = _synthetic_league_players(450, season=season)  # ~ a 30-team league's roster count

    from dba2.managers.scouting import age_ramp

    diverging = 0
    total_eligible = 0
    for roster_player, age, peak_age_start in players:
        if age_ramp(float(age), float(peak_age_start)) <= 0.25:
            continue
        total_eligible += 1
        team_a, team_b = rng.sample(range(1, 31), 2)
        value_a = market_value_for(
            roster_player, league_rng_seed=league_rng_seed, team_id=team_a, season=season,
            philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=window, cap_settings=CAP_SETTINGS,
        )
        value_b = market_value_for(
            roster_player, league_rng_seed=league_rng_seed, team_id=team_b, season=season,
            philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=window, cap_settings=CAP_SETTINGS,
        )
        rel_diff = abs(value_a - value_b) / max(abs(value_a), abs(value_b), 1e-9)
        if rel_diff > DEFAULT_TUNING.min_rel_gain:
            diverging += 1

    assert total_eligible > 50, "too few age_ramp > 0.25 players to measure anything"
    fraction_diverging = diverging / total_eligible
    assert fraction_diverging >= 0.50, (
        f"only {fraction_diverging:.1%} of age_ramp>0.25 players diverge by more than "
        f"MIN_REL_GAIN across two random teams (need >= 50%) -- {diverging}/{total_eligible}"
    )


# ---------------------------------------------------------------------------
# age_mult_eff's negative-base clamp -- regression test for the defect
# tools/gm_harness's T7a build surfaced (critic round 1, finding B1):
# `age_mult(38)` sits at its floor anchor (0.25); a `vet_bias < 1.0`
# (youth_developer, 0.70) reshapes it past zero before the fractional
# `age_exponent` is applied, and Python's `**` on a negative base with a
# non-integer exponent silently returns a `complex`, not a `ValueError`.
# ---------------------------------------------------------------------------


def test_age_mult_eff_clamps_negative_base_to_zero_for_low_vet_bias_at_floor_age():
    vet_bias = PROFILES[GmPhilosophy.YOUTH_DEVELOPER].vet_bias
    assert vet_bias == pytest.approx(0.70)
    # 1.0 - (1.0 - 0.25) / 0.70 ~= -0.0714 without the clamp -- a negative
    # base raised to a fractional age_exponent below would return a
    # `complex`, not a real 0.0.
    result = age_mult_eff(38.0, vet_bias=vet_bias, age_exponent=1.15)
    assert isinstance(result, float)
    assert result == 0.0


def test_age_mult_eff_does_not_clamp_when_vet_bias_keeps_base_non_negative():
    """The clamp only ever engages once vet_bias's reshape pushes base past
    zero -- confirms it's a no-op for the common vet_bias=1.00 case, even
    at the same floor age that triggers it under youth_developer."""
    result = age_mult_eff(38.0, vet_bias=1.00, age_exponent=1.15)
    assert result == pytest.approx(0.25**1.15)


def test_age_mult_eff_clamps_before_age_mult_reaches_its_floor_anchor():
    """The clamp's trigger condition is `age_mult(age) < 0.30` (base
    crosses zero), not "age_mult has bottomed out at its 38+ floor
    anchor" -- those are different ages. On the piecewise curve between
    anchors (35.0, 0.45) and (38.0, 0.25), `age_mult` crosses 0.30 at
    exactly age 37.25, three quarters of a year before the floor anchor
    itself. This covers that gap (37.25 < age < 38, base already negative
    but age_mult not yet at its floor) -- previously untested."""
    vet_bias = PROFILES[GmPhilosophy.YOUTH_DEVELOPER].vet_bias
    result = age_mult_eff(37.3, vet_bias=vet_bias, age_exponent=1.15)
    assert isinstance(result, float)
    assert result == 0.0
