"""Tier 1: `managers/decisions/signing.py` -- §9.3's offer-construction
formula and §12 step 9's own signing verify list (docs/design/phase-2-
manager-minds.md).
"""
from __future__ import annotations

import pytest

from dba2.domain.team import GmPhilosophy
from dba2.managers.decisions.signing import (
    _OLD_AGE_SHORT_YEARS,
    _YEARS_BASE_HI,
    _YEARS_BASE_LO,
    SigningOffer,
    _base_years_from_window,
    construct_offer,
    legal_capacity,
    offer_salary,
    offer_years,
    willingness,
)
from dba2.managers.needs import build_need_matrix
from dba2.managers.snapshot import build_team_snapshot
from dba2.managers.tuning import GM_TUNING_VERSION, WINDOW_PARAMS, CompetitiveWindow
from tests.fixtures.managers import CAP_SETTINGS, make_roster_player
from tests.fixtures.sim import make_team

_SEASON = 2026


def _empty_matrix():
    snapshot = build_team_snapshot(make_team(1), (), (), CAP_SETTINGS)
    return build_need_matrix(snapshot)


# ---------------------------------------------------------------------------
# offer_years -- interpretation call 1
# ---------------------------------------------------------------------------


def test_offer_years_old_player_is_always_one_year_regardless_of_window():
    for window in CompetitiveWindow:
        assert offer_years(33.0, window) == 1
        assert offer_years(38.0, window) == 1


def test_offer_years_young_player_gets_a_bonus_year_only_on_rebuild_or_retool():
    rebuild_years = offer_years(24.0, CompetitiveWindow.REBUILD)
    retool_years = offer_years(24.0, CompetitiveWindow.RETOOL)
    win_now_years = offer_years(24.0, CompetitiveWindow.WIN_NOW)
    contend_years = offer_years(24.0, CompetitiveWindow.CONTEND)
    assert rebuild_years > win_now_years
    assert retool_years > contend_years
    # A CONTEND team never offers a young player its own extra year --
    # only REBUILD/RETOOL do.
    assert offer_years(24.0, CompetitiveWindow.CONTEND) == offer_years(
        29.0, CompetitiveWindow.CONTEND
    )


def test_offer_years_stays_within_the_1_to_5_clamp():
    for age in (18.0, 24.0, 29.0, 33.0, 40.0):
        for window in CompetitiveWindow:
            years = offer_years(age, window)
            assert 1 <= years <= 5


def test_offer_years_mutation_test_base_years_actually_varies_by_window():
    """Confirms `_base_years_from_window` has real teeth: REBUILD's base
    must exceed CONTEND's for a middle-aged player (no old/young override
    branch triggered), or the "contenders offer short, rebuilders offer
    long" claim `offer_years` implements is not actually implemented."""
    mid_age = 29.0
    assert offer_years(mid_age, CompetitiveWindow.REBUILD) > offer_years(
        mid_age, CompetitiveWindow.CONTEND
    )


# ---------------------------------------------------------------------------
# Slice 22-B (architect round 23 §4.2-§4.3-§4.7, Decision P2-42) --
# `_YEARS_BASE_LO`/`_YEARS_BASE_HI` recalibrated 1/4 -> 3/6, `_YEARS_
# CLAMP_HI` unchanged at 5 (now load-bearing). 8 test obligations, §4.7.
# ---------------------------------------------------------------------------


def test_slice_22b_raw_base_map_is_6_5_4_3_pre_clamp_and_5_5_4_3_post_clamp():
    """§4.7 obligation 1. A middle-aged player (strictly between 25 and 33,
    so neither the old-age override nor the young-REBUILD/RETOOL bonus
    branch fires) exercises `_base_years_from_window` and `offer_years`
    cleanly. **Mutation to confirm this test bites** (verified by hand
    during this build, then reverted): leaving `_YEARS_BASE_HI` at 4 while
    moving only `_YEARS_BASE_LO` to 3 collapses the raw map to 5/4/4/3 --
    this assertion fails against that mutation."""
    mid_age = 29.0
    raw = {
        window: _base_years_from_window(window)
        for window in CompetitiveWindow
    }
    assert raw[CompetitiveWindow.REBUILD] == 6
    assert raw[CompetitiveWindow.RETOOL] == 5
    assert raw[CompetitiveWindow.WIN_NOW] == 4
    assert raw[CompetitiveWindow.CONTEND] == 3

    post_clamp = {
        window: offer_years(mid_age, window) for window in CompetitiveWindow
    }
    assert post_clamp[CompetitiveWindow.REBUILD] == 5
    assert post_clamp[CompetitiveWindow.RETOOL] == 5
    assert post_clamp[CompetitiveWindow.WIN_NOW] == 4
    assert post_clamp[CompetitiveWindow.CONTEND] == 3


def test_slice_22b_clamp_is_binding_not_incidental_for_rebuild_and_retool():
    """§4.7 obligation 2 (new). Pins the exact confound round 23 found in
    round 22's original sketch: at this rung, REBUILD and RETOOL's young
    bonus is provably INERT, because `_YEARS_CLAMP_HI = 5` already caps
    their un-bonused base (6 and 5.2105 -> 5) before the bonus is even
    applied. `offer_years` must therefore return exactly 5 for BOTH a
    young (<=25) and a middle-young (26-32) player in both windows -- if
    the bonus were live, the young case would exceed the older case."""
    for window in (CompetitiveWindow.REBUILD, CompetitiveWindow.RETOOL):
        young = offer_years(25.0, window)
        mid = offer_years(30.0, window)
        assert young == 5, f"{window}: expected the clamp to bind at 5 for a young player"
        assert mid == 5, f"{window}: expected the clamp to bind at 5 for a mid-age player"
        assert young == mid, (
            f"{window}: the young bonus must be inert at this rung -- if this "
            f"ever differs, the clamp has stopped binding and REBUILD/RETOOL "
            f"duration is distinguishable again, which changes this slice's "
            f"disclosed design cost"
        )


def test_slice_22b_no_rounding_tie_in_the_gamma_interpolants():
    """§4.7 obligation 3 (new). `_base_years_from_window`'s
    `round(_YEARS_BASE_LO + (HI - LO) * frac)` must not land on a `.5` for
    any of the four `gamma` values, because Python's banker's rounding
    would then make the map depend on parity of a future `_YEARS_BASE_HI`
    change rather than on `frac` itself. Recomputes the raw (pre-round)
    interpolant independently from `WINDOW_PARAMS`' own gammas and the
    module's real `_YEARS_BASE_LO`/`_YEARS_BASE_HI` constants, so this
    test does not silently go stale if either moves again."""
    contend_gamma = WINDOW_PARAMS[CompetitiveWindow.CONTEND].gamma
    rebuild_gamma = WINDOW_PARAMS[CompetitiveWindow.REBUILD].gamma
    min_distance_from_a_half = 0.05
    for window in CompetitiveWindow:
        frac = (WINDOW_PARAMS[window].gamma - contend_gamma) / (rebuild_gamma - contend_gamma)
        raw = _YEARS_BASE_LO + (_YEARS_BASE_HI - _YEARS_BASE_LO) * frac
        nearest_half = round(raw - 0.5) + 0.5
        distance = abs(raw - nearest_half)
        assert distance > min_distance_from_a_half, (
            f"{window}: raw interpolant {raw} sits within {min_distance_from_a_half} "
            f"of a .5 rounding boundary -- banker's rounding could silently flip "
            f"this window on a future _YEARS_BASE_HI change"
        )


def test_slice_22b_old_age_short_years_extraction_is_behaviour_neutral():
    """§4.7 obligation 4. `_OLD_AGE_SHORT_YEARS` is a pure name for the
    bare literal `1` that previously lived inline at `offer_years`' age>=33
    branch -- confirms the extraction changed no behaviour: the constant's
    own value is 1, and `offer_years` at age>=33 returns exactly that
    value, byte-identically to pre-slice, for all four windows."""
    assert _OLD_AGE_SHORT_YEARS == 1
    for window in CompetitiveWindow:
        assert offer_years(33.0, window) == _OLD_AGE_SHORT_YEARS
        assert offer_years(38.0, window) == _OLD_AGE_SHORT_YEARS


def test_slice_22b_gm_tuning_version_is_3():
    """§4.7 obligation 5 (half -- the other half, that `seeds/gm_
    validation_bands.json`'s own top-level `gm_tuning_version` moves with
    it and that `load_bands` raises if they ever drift, is asserted in
    `tests/unit/managers/test_tuning.py` and `tools/gm_harness/bands.py`'s
    own runtime cross-check, not duplicated here). Slice 22-B bumped this
    1 -> 2; Phase 3 §11 step 4 (Decision P3-30) has since bumped it
    2 -> 3 -- see `test_tuning.py::test_gm_tuning_version_is_3_after_
    phase_3_step_4` for that bump's own citation. This test's name keeps
    the historical "slice_22b" label since the value it asserts on is the
    live one, not a slice-22b-specific snapshot."""
    assert GM_TUNING_VERSION == 3


# ---------------------------------------------------------------------------
# legal_capacity -- interpretation call 2
# ---------------------------------------------------------------------------


def test_legal_capacity_prefers_positive_room_over_any_exception():
    assert legal_capacity(10_000_000, CAP_SETTINGS) == 10_000_000


def test_legal_capacity_falls_back_to_the_largest_available_exception():
    expected = max(CAP_SETTINGS.mle_amount, CAP_SETTINGS.bi_annual_amount, CAP_SETTINGS.min_salary)
    assert legal_capacity(0, CAP_SETTINGS) == expected
    assert legal_capacity(-5_000_000, CAP_SETTINGS) == expected
    assert expected == CAP_SETTINGS.mle_amount, "MLE is the largest exception in this fixture"


def test_legal_capacity_floors_a_tiny_positive_room_at_the_minimum_exception():
    """Critic's finding: `room=$1` used to return `$1` unchanged, below
    even the minimum exception every team always has. `room=$1` must now
    floor at `min_salary`; a `room` that already clears `min_salary` (the
    10M case above) must still pass through unchanged, not get bumped up
    to the (larger) MLE/bi-annual exceptions -- only the minimum exception
    is an unconditional floor here."""
    assert legal_capacity(1, CAP_SETTINGS) == CAP_SETTINGS.min_salary
    assert legal_capacity(1, CAP_SETTINGS) < CAP_SETTINGS.mle_amount


# ---------------------------------------------------------------------------
# legal_capacity -- slice 12-A, the incumbent (Bird-rights) bucket
# (Decision P2-20, architect round 11 §5.3, round 12 §5.4).
# Test numbering below matches round 11 §5.5's own list (items 1-3 fall
# here; items 4, 5, 6, 7, 8, 10 are gm_service.py-level, `test_gm_service_
# pure.py`; item 9, the harness-level headline test, is in `tests/unit/
# tools/gm_harness/test_season.py` -- round 12's own precedent for its
# analogous headline test).
# ---------------------------------------------------------------------------


def test_legal_capacity_is_unchanged_for_a_non_incumbent():
    """Round 11 §5.5 item 1. The two-positional-argument call must return
    exactly today's value regardless of the new keyword-only parameters'
    mere existence -- room > 0, room = 0, room < 0. **Kills:** any refactor
    that makes the incumbent branch reachable without `incumbent_prior_
    salary` actually being supplied."""
    for room in (10_000_000, 0, -5_000_000):
        assert legal_capacity(room, CAP_SETTINGS) == legal_capacity(room, CAP_SETTINGS)
    assert legal_capacity(10_000_000, CAP_SETTINGS) == 10_000_000
    expected_fallback = max(
        CAP_SETTINGS.mle_amount, CAP_SETTINGS.bi_annual_amount, CAP_SETTINGS.min_salary
    )
    assert legal_capacity(0, CAP_SETTINGS) == expected_fallback
    assert legal_capacity(-5_000_000, CAP_SETTINGS) == expected_fallback


def test_legal_capacity_lets_an_over_cap_team_re_sign_its_own_player_above_room():
    """Round 11 §5.5 item 2. Room strongly negative, a large incumbent
    prior salary (the gate, not read numerically by the formula -- see
    module docstring's interpretation call 3), ample apron room -> the
    returned capacity is the player's own years-of-service max-percentage
    tier of the cap, not the (much smaller) MLE. **Kills:** the incumbent
    branch being ordered after the `max(mle, bi_annual, min_salary)`
    return (it would never be reached); the `max(ladder, bird_ceiling)`
    being a `min` instead (it would pick the SMALLER of the two, discarding
    the whole point of the bucket)."""
    tier_ceiling = round(CAP_SETTINGS.max_pct_10_plus / 100.0 * CAP_SETTINGS.salary_cap)
    capacity = legal_capacity(
        -50_000_000,
        CAP_SETTINGS,
        incumbent_prior_salary=30_000_000,
        incumbent_years_pro=12,
        apron_room=tier_ceiling + 10_000_000,  # ample -- never the binding constraint here
    )
    assert capacity == tier_ceiling
    assert capacity > CAP_SETTINGS.mle_amount


def test_legal_capacity_never_exceeds_apron_room_for_an_incumbent():
    """Round 11 §5.5 item 3, rewritten per architect round 14 §5.5 (the
    original `apron_room=1` fixture is now invalid: the *hard* `apron_2`-
    basis clamp at `1 + GAP = 11,879,001` sits below `mle_amount`, so it
    would bind first and the test would no longer demonstrate its own
    claim). `apron_room=3_000_000` puts the hard ceiling at `3,000,000 +
    11,879,000 = 14,879,000`, still above `mle_amount` (14,104,000) -- so
    the MLE still wins, demonstrating specifically that the *soft*, `apron_
    1`-basis Bird ceiling clamps only the Bird bucket, never the rest of
    the ladder (the hard ceiling is a different test, immediately below).
    **Kills:** applying the soft `apron_room` clamp as a global clamp on
    the whole ladder instead of a per-bucket one on the Bird term alone --
    the mutation that would silently REDUCE every over-cap incumbent's
    capacity below what a fresh free agent could already draw, making
    metrics 8b/10 worse while looking like a fix."""
    capacity = legal_capacity(
        -50_000_000,
        CAP_SETTINGS,
        incumbent_prior_salary=30_000_000,
        incumbent_years_pro=12,
        apron_room=3_000_000,  # below the tier ceiling and the MLE, but hard ceiling > MLE
    )
    assert capacity == CAP_SETTINGS.mle_amount
    assert capacity > 3_000_000


def test_legal_capacity_clamps_every_bucket_at_the_apron_2_headroom():
    """New sibling to item 3 (architect round 14 §5.5). Same fixture as
    item 3's original `apron_room=1`, which item 3 itself can no longer
    use: the *hard*, all-methods `apron_2`-basis ceiling now binds and
    clamps the return value to `apron_room + GAP`, below `mle_amount`.
    `GAP = apron_2 - apron_1 = 207,824,000 - 195,945,000 = 11,879,000`
    (written out as a literal, not derived from `CAP_SETTINGS`, so this
    test fails loudly if the fixture's cap settings ever drift). **Kills:**
    dropping the hard clamp entirely; computing the headroom off the wrong
    basis (`apron_2 - used`, or `apron_room` alone with no `GAP` added);
    using `luxury_tax` in place of `apron_1`."""
    gap = 11_879_000
    assert CAP_SETTINGS.apron_2 - CAP_SETTINGS.apron_1 == gap
    capacity = legal_capacity(
        -50_000_000,
        CAP_SETTINGS,
        incumbent_prior_salary=30_000_000,
        incumbent_years_pro=12,
        apron_room=1,
    )
    assert capacity == 1 + gap == 11_879_001
    assert capacity < CAP_SETTINGS.mle_amount


def test_legal_capacity_bird_ceiling_uses_the_players_own_years_pro_tier():
    """The tiering rule is per-player, not a single constant -- a 5-years-
    pro incumbent's ceiling is `max_pct_0_6`, not `max_pct_10_plus`, given
    identical room/apron inputs. Confirms `_bird_max_pct_tier` is actually
    read, not hardcoded to the most permissive tier."""
    kwargs = dict(
        room=-50_000_000, cap_settings=CAP_SETTINGS, incumbent_prior_salary=30_000_000,
        apron_room=10_000_000_000,  # never the binding constraint
    )
    junior_capacity = legal_capacity(incumbent_years_pro=5, **kwargs)
    senior_capacity = legal_capacity(incumbent_years_pro=12, **kwargs)
    assert junior_capacity == round(CAP_SETTINGS.max_pct_0_6 / 100.0 * CAP_SETTINGS.salary_cap)
    assert senior_capacity == round(CAP_SETTINGS.max_pct_10_plus / 100.0 * CAP_SETTINGS.salary_cap)
    assert junior_capacity < senior_capacity


# ---------------------------------------------------------------------------
# willingness -- the dormant [0.70, 1.60] guard
# ---------------------------------------------------------------------------


def test_willingness_never_exceeds_its_outer_clamp():
    for window in CompetitiveWindow:
        for need_bonus_raw in (-1.0, -0.35, 0.0, 0.50, 5.0):
            w = willingness(need_bonus_raw, window)
            assert 0.70 <= w <= 1.60


def test_willingness_upper_guard_never_binds_at_current_constants():
    """§9.3's own claim: against `need_bonus`'s actual *reachable* range at
    any real philosophy (+0.39 at the top -- tighter than the raw
    `clamp_need_bonus` bound of +0.50, per §5's own stated per-philosophy
    maximum) and `window_urgency` in [0.00, 0.18], willingness's reachable
    max is 1.57 -- strictly inside [0.70, 1.60]'s upper bound. The upper
    clamp is a dormant, forward-looking guard, not dead code (see module
    docstring) -- this test pins that it does not currently bind, and that
    the raw `clamp_need_bonus` ceiling (+0.50) *would* make it bind
    (`willingness(0.50, CONTEND) == 1.60` exactly), which is why the clamp
    is a real, load-bearing guard rather than provably-dead code."""
    reachable_max = willingness(0.39, CompetitiveWindow.CONTEND)
    assert reachable_max < 1.60
    assert reachable_max == pytest.approx(1.57, abs=1e-9)

    raw_clamp_bound = willingness(0.50, CompetitiveWindow.CONTEND)
    assert raw_clamp_bound == pytest.approx(1.60, abs=1e-9)


# ---------------------------------------------------------------------------
# §12 step 9 -- the signing verify bullets
# ---------------------------------------------------------------------------


def test_no_ai_offer_ever_exceeds_its_teams_legal_room_or_exception():
    roster_player = make_roster_player(5001, age=26, season=_SEASON, overall_cached=82)
    matrix = _empty_matrix()
    rooms = (-10_000_000, 0, 5_000_000, 40_000_000)

    for philosophy in GmPhilosophy:
        for window in CompetitiveWindow:
            for room in rooms:
                offer, record = construct_offer(
                    roster_player, matrix, team_id=1, philosophy=philosophy, window=window,
                    cap_settings=CAP_SETTINGS, room=room, season=_SEASON,
                )
                cap = legal_capacity(room, CAP_SETTINGS)
                assert offer.annual_salary <= cap, (
                    f"offer {offer.annual_salary} exceeded legal capacity {cap} at "
                    f"{philosophy}, {window}, room={room}"
                )
                assert record.reconstructs(), (record.score, record.terms)


def test_offer_salary_mutation_test_is_actually_capped_by_legal_capacity():
    """Mutation check: a tiny `legal_capacity_dollars` must bind even for
    a max-worthy talent -- if `offer_salary` ever silently ignored its
    `legal_capacity_dollars` argument, this would fail."""
    salary = offer_salary(95, CAP_SETTINGS, 1, 0.0, CompetitiveWindow.CONTEND)
    assert salary == 1


def test_offer_salary_floors_at_min_salary_for_a_low_ovr_low_willingness_free_agent():
    """Critic's finding, reproduced exactly: a 55-ovr FA at `willingness =
    0.70` used to be offered $891,009 against a real `min_salary` of
    $1,272,870 -- no real contract signs below `min_salary`. `legal_
    capacity_dollars` is set to the full cap here so the floor, not the
    cap, is what's under test."""
    pre_floor_market_times_willingness = 891_009
    salary = offer_salary(
        55, CAP_SETTINGS, CAP_SETTINGS.salary_cap, -1.0, CompetitiveWindow.REBUILD
    )
    assert salary == CAP_SETTINGS.min_salary
    assert salary > pre_floor_market_times_willingness


def test_max_worthy_free_agent_draws_a_max_offer_from_at_least_one_team():
    """§12 step 9: "a max-worthy free agent draws a max offer from at
    least one team." `market_salary_share`'s own top anchor (ovr >= 90)
    already IS `max_pct_10_plus / 100` of the cap (`valuation.py`'s own
    established curve, not re-derived here) -- a team bidding at
    `willingness >= 1.0` therefore offers at least that share whenever its
    legal capacity allows it."""
    roster_player = make_roster_player(5002, age=25, season=_SEASON, overall_cached=95)
    matrix = _empty_matrix()
    max_dollars = round(CAP_SETTINGS.max_pct_10_plus / 100.0 * CAP_SETTINGS.salary_cap)
    room = CAP_SETTINGS.salary_cap  # plenty of room, never the binding constraint here

    results = [
        construct_offer(
            roster_player, matrix, team_id=1, philosophy=philosophy, window=window,
            cap_settings=CAP_SETTINGS, room=room, season=_SEASON,
        )
        for philosophy in GmPhilosophy
        for window in CompetitiveWindow
    ]
    offers = [offer for offer, _ in results]
    assert any(offer.annual_salary >= max_dollars for offer in offers), (
        f"no offer reached the max-worthy threshold of {max_dollars}; "
        f"offers were {[o.annual_salary for o in offers]}"
    )


def test_signing_offer_guaranteed_total_is_flat_annual_times_years():
    offer = SigningOffer(
        team_id=1, player_id=1, years=3, annual_salary=10_000_000, window=CompetitiveWindow.RETOOL,
    )
    assert offer.guaranteed_total == 30_000_000


# ---------------------------------------------------------------------------
# construct_offer's DecisionRecord (Decision P2-18)
# ---------------------------------------------------------------------------


def test_construct_offer_decision_record_reconstructs_across_several_scenarios():
    roster_player = make_roster_player(5003, age=27, season=_SEASON, overall_cached=88)
    matrix = _empty_matrix()
    rooms = (-5_000_000, 0, 1, 3_000_000, 60_000_000)

    for philosophy in GmPhilosophy:
        for window in CompetitiveWindow:
            for room in rooms:
                _, record = construct_offer(
                    roster_player, matrix, team_id=1, philosophy=philosophy, window=window,
                    cap_settings=CAP_SETTINGS, room=room, season=_SEASON,
                )
                assert record.reconstructs(), (
                    philosophy, window, room, record.score, record.terms
                )
                assert record.verdict == "offer"


def test_construct_offer_decision_record_mutation_test_terms_actually_sum_to_score():
    """Breaks the record's own term list (drops the `legal_capacity_cap`
    term) and confirms `reconstructs()` then fails whenever that term was
    actually nonzero -- i.e. the reconstruction check has real teeth, not
    just a tautology against however the terms happened to be built."""
    roster_player = make_roster_player(5004, age=24, season=_SEASON, overall_cached=99)
    matrix = _empty_matrix()
    # A tiny room forces the legal_capacity_cap term to bind hard for a
    # 99-ovr player, so dropping that term changes the sum materially.
    _, record = construct_offer(
        roster_player, matrix, team_id=1, philosophy=GmPhilosophy.STAR_MAXER,
        window=CompetitiveWindow.CONTEND, cap_settings=CAP_SETTINGS, room=1, season=_SEASON,
    )
    cap_term = next(t for t in record.terms if t.label == "legal_capacity_cap")
    assert cap_term.contribution != 0.0, "expected the legal_capacity_cap term to actually bind"

    broken_terms = tuple(t for t in record.terms if t.label != "legal_capacity_cap")
    import dataclasses

    broken_record = dataclasses.replace(record, terms=broken_terms)
    assert not broken_record.reconstructs(), (
        "dropping a nonzero term should have broken the reconstruction identity"
    )
