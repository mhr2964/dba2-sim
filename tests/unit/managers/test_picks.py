"""Tier 1/3: draft pick and prospect valuation -- §4.6's formulas and
§12 step 5's own verify list (docs/design/phase-2-manager-minds.md).

Per this dispatch's task description and the module docstring of
`dba2.managers.picks`, this file follows §12 step 5's verify list
precisely, including the two named boundary cases (29->30, 30->31) and
the **documented, expected** monotonicity gap at 29->30: `picks.py` reads
`rookie_scale` exactly as the caller supplies it (Decision P2-33's exact
row lookup, no interpolation), and at this step the only table available
is migration 003's real, un-repaired, corrupt-row-30 data -- Decision
P2-33's repair (migration 009) is step 7's deliverable, not this one's.
`test_pick_value_is_monotone_non_increasing_...` below asserts the
29->30 exception explicitly rather than silently excluding it from the
sweep.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dba2.domain.team import GmPhilosophy
from dba2.managers.picks import (
    conveyance_probability,
    e_ovr_y,
    entry_ovr,
    expected_slot,
    implied_residual,
    peak_ovr,
    pick_horizon,
    pick_value,
    projected_slot_from_window_score,
    protected_pick_value,
    rookie_share_y,
    second_round_fallback,
    slot_if_conveys,
    swap_rights_value,
)
from dba2.managers.snapshot import LeagueSnapshot, build_league_snapshot, build_team_snapshot
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow
from dba2.managers.window import window_score
from tests.fixtures.managers import (
    CAP_SETTINGS,
    PICK_ARCHETYPES,
    ROOKIE_SCALE,
    generate_pick_archetype_table,
    make_roster_player,
)
from tests.fixtures.sim import make_team

_GOLDEN_PATH = Path(__file__).resolve().parents[3] / "seeds" / "gm_pick_archetypes.json"

_DEFAULT_PHILOSOPHY = GmPhilosophy.TENDENCY_RESPECTER


def _pv(slot: int, years_out: int, philosophy: GmPhilosophy, window: CompetitiveWindow) -> float:
    return pick_value(
        slot, years_out, philosophy, window, rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS
    )


# ---------------------------------------------------------------------------
# The golden archetype table (§10, §12 step 5's own deliverable)
# ---------------------------------------------------------------------------


def test_archetype_table_matches_the_checked_in_golden_file():
    golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    actual = json.loads(json.dumps(generate_pick_archetype_table(), sort_keys=True))
    assert actual == golden["archetypes"], (
        "computed pick archetype table no longer matches "
        "seeds/gm_pick_archetypes.json -- if this is an intentional tuning "
        "change (or migration 009's row-30 repair landing), regenerate the "
        "golden file in the same commit that bumps GM_TUNING_VERSION"
    )


def test_archetype_table_covers_all_8_rows_and_28_columns():
    table = generate_pick_archetype_table()
    assert len(table) == 8, "§10's pick archetype table is 8 rows (its own itemized count)"
    for label, row in table.items():
        assert len(row) == 28, f"{label}: expected 28 (philosophy, window) columns, got {len(row)}"
        for philosophy in GmPhilosophy:
            for window in CompetitiveWindow:
                assert f"{philosophy.value}:{window.value}" in row


def test_pick_archetype_labels_match_ss10s_eight_named_rows():
    labels = {a.label for a in PICK_ARCHETYPES}
    assert labels == {
        "top3_pick_2y_out",
        "protected_top5_expected_slot_2",
        "protected_top5_expected_slot_15",
        "pick_29",
        "pick_30",
        "pick_31",
        "pick_45",
        "pick_60",
    }


# ---------------------------------------------------------------------------
# The slot table (§4.6's two columns) and its implied-residual cross-check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slot,expected_entry,expected_peak",
    [
        (1, 68, 82), (3, 67, 79), (5, 65, 77), (10, 63, 74),
        (20, 60, 70), (30, 58, 68), (45, 55, 64), (60, 52, 61),
    ],
)
def test_slot_table_anchor_values(slot, expected_entry, expected_peak):
    assert entry_ovr(slot) == pytest.approx(expected_entry)
    assert peak_ovr(slot) == pytest.approx(expected_peak)


def test_implied_residual_stays_in_bounds_and_is_monotone_non_increasing():
    # §4.6's restated cross-check -- has actual power to fail, unlike the
    # "within 1.5 rating points" form it replaced.
    residuals = [implied_residual(slot) for slot in (1, 3, 5, 10, 20, 30, 45, 60)]
    for r in residuals:
        assert -1.0 <= r <= 1.0, r
    for a, b in zip(residuals, residuals[1:], strict=False):
        assert b <= a + 1e-9, "implied_residual must be monotone non-increasing in slot"


def test_implied_residual_matches_the_docs_worked_anchor_values():
    expected = {
        1: 0.722, 3: 0.333, 5: 0.333, 10: 0.139,
        20: -0.056, 30: -0.056, 45: -0.250, 60: -0.250,
    }
    for slot, exp in expected.items():
        assert implied_residual(slot) == pytest.approx(exp, abs=0.005)


# ---------------------------------------------------------------------------
# Decision P2-33 -- rookie_share_y is an exact row lookup, no interpolation
# ---------------------------------------------------------------------------


def test_rookie_share_y_is_an_exact_row_lookup():
    for slot in (1, 15, 29, 30):
        row = ROOKIE_SCALE[slot]
        expected = (
            row.year_1_salary / CAP_SETTINGS.salary_cap,
            row.year_2_salary / CAP_SETTINGS.salary_cap,
            row.year_3_salary / CAP_SETTINGS.salary_cap,
            row.year_4_salary / CAP_SETTINGS.salary_cap,
        )
        for y in range(4):
            assert rookie_share_y(slot, y, ROOKIE_SCALE, CAP_SETTINGS) == pytest.approx(expected[y])


def test_rookie_share_y_at_slot_31_and_beyond_is_min_salary_not_a_row_lookup():
    expected = CAP_SETTINGS.min_salary / CAP_SETTINGS.salary_cap
    for slot in (31, 45, 60):
        for y in range(pick_horizon(slot)):
            assert rookie_share_y(slot, y, ROOKIE_SCALE, CAP_SETTINGS) == pytest.approx(expected)


def test_rookie_share_y_at_slot_31_never_indexes_rookie_scale():
    # "pick 31 returns a value rather than a KeyError" -- rookie_scale has
    # 30 rows; deleting row 31 (which never existed) from the caller's
    # table must not change anything about how slot 31 prices.
    short_table = {k: v for k, v in ROOKIE_SCALE.items() if k != 30}  # still 29 rows, no 30 either
    for y in range(pick_horizon(31)):
        rookie_share_y(31, y, short_table, CAP_SETTINGS)  # must not raise


def test_pick_31_returns_a_value_rather_than_a_key_error():
    value = _pv(31, 0, _DEFAULT_PHILOSOPHY, CompetitiveWindow.RETOOL)
    assert value > 0.0


def test_no_interpolation_appears_anywhere_in_picks_module_source():
    # Grep-verifiable per Decision P2-33's own text: `_piecewise_linear`
    # -- the module's one interpolation helper -- is called exactly twice
    # in the whole module (entry_ovr, peak_ovr, the slot table's own two
    # columns), and never inside rookie_share_y's body, which is the
    # function Decision P2-33 exists to keep an exact-lookup-only path.
    source = Path(__file__).resolve().parents[3].joinpath("dba2", "managers", "picks.py").read_text(
        encoding="utf-8"
    )
    call_count = source.count("return _piecewise_linear(")
    assert call_count == 2, f"expected exactly 2 call sites (entry_ovr, peak_ovr): {call_count}"
    rookie_share_fn = source.split("def rookie_share_y(")[1].split("\ndef ")[0]
    assert "_piecewise_linear" not in rookie_share_fn


# ---------------------------------------------------------------------------
# pick_value: horizon split, the worked pick-1 table, and the documented
# 29->30 / 30->31 boundary cases
# ---------------------------------------------------------------------------


def test_pick_horizon_splits_at_30():
    assert pick_horizon(30) == DEFAULT_TUNING.rookie_horizon == 4
    assert pick_horizon(31) == DEFAULT_TUNING.second_round_horizon == 2


def test_pick_1_retool_default_philosophy_matches_the_docs_worked_total():
    # §4.6's fully worked table: discounted total 676.3 VP.
    assert _pv(1, 0, _DEFAULT_PHILOSOPHY, CompetitiveWindow.RETOOL) == pytest.approx(676.3, abs=0.5)


def test_slot_values_across_the_board_match_the_doc_at_retool_default_philosophy():
    # §4.6's own published slot list, generated on the exact-lookup rule
    # against the real, un-repaired seed data -- note pick 30 here is the
    # doc's own stated *un-repaired* figure (168.4), not the post-repair
    # 145.8, because migration 009 has not landed as of this step.
    expected = {1: 676.3, 5: 401.1, 29: 149.9, 30: 168.4, 31: 77.9, 45: 42.1, 60: 25.3}
    for slot, exp in expected.items():
        actual = _pv(slot, 0, _DEFAULT_PHILOSOPHY, CompetitiveWindow.RETOOL)
        assert actual == pytest.approx(exp, abs=0.5), (slot, actual, exp)


def test_slot_45_prices_inside_the_docs_band_at_default_philosophy_under_all_four_windows():
    expected = {
        CompetitiveWindow.CONTEND: 34.6,
        CompetitiveWindow.WIN_NOW: 37.3,
        CompetitiveWindow.RETOOL: 42.1,
        CompetitiveWindow.REBUILD: 47.1,
    }
    for window, exp in expected.items():
        actual = _pv(45, 0, _DEFAULT_PHILOSOPHY, window)
        assert 30.0 <= actual <= 60.0, (window, actual)
        assert actual == pytest.approx(exp, abs=0.5), (window, actual, exp)


def test_boundary_29_to_30_is_a_known_documented_inversion_pending_migration_009():
    """**This is the documented, expected finding, not a bug.** With the
    real, currently-seeded (un-repaired) `rookie_scale` row 30 -- whose
    **all four year-columns are corrupt, not year 4 alone** (y1/y2/y3 each
    ~-33.99% below row 29's own figure, y4 ~-37.56% below row 29's,
    against columns that otherwise decline 2.4-2.7%/slot [y1-y3] or
    4.3-4.6%/slot [y4] from row 25 through row 29) -- `pick_value(30, ...)`
    prices ABOVE `pick_value(29, ...)` at every one of the 28 (philosophy,
    window) pairs, exactly as §4.6 states: "under the exact lookup ...
    pick 30 prices 12.3% above pick 29 at all 28 (philosophy, window)
    pairs." **The 12.3% figure itself is the doc's one fully-worked
    example (default philosophy, RETOOL) -- measured here, the inversion's
    actual size ranges 6.5%-14.9% across the 28 pairs** (`kappa` moves how
    much weight the surplus term carries relative to talent), so this test
    asserts the doc's real, load-bearing claim (the inversion's *existence
    and sign* at every pair) plus its literal worked figure at the one
    pair it names, rather than pinning "12.3%" everywhere the doc's prose
    reads as uniform.

    **Honest scope note (critic round 10, B2) -- read before trusting this
    test to mean more than it does.** This test's only claim is about the
    CURRENT, un-repaired seed data. It is **not** a regression guard for
    migration 009, and it cannot become one: it prices exclusively against
    this test file's imported `ROOKIE_SCALE` (a hand-transcribed fixture
    copy of migration 003's rows -- verified once, by hand, against
    `alembic/versions/003_contracts.py`, but never re-verified
    automatically against any migration), never against a real migration
    table. If migration 009 is simply never written, or is written but
    does nothing, this test keeps asserting `p30 > p29` and keeps passing
    forever -- it can only ever confirm "the bug still exists in this
    fixture," never "the bug was supposed to be fixed and was." **Step 7
    (`managers/capsheet.py` + migration 009) owns the real regression
    test**: once migration 009 exists, step 7 must add a test that reads
    the ACTUAL post-migration `rookie_scale` table (not this fixture) and
    asserts `pick_value(30, ...) <= pick_value(29, ...)` at all 28 pairs
    (monotonicity restored). That test does not exist yet, deliberately --
    migration 009 doesn't exist yet, and building a test against data that
    doesn't exist would be premature. This docstring and
    `docs/ledger/phase-2.md`'s round-6 entry both record the obligation so
    it isn't lost."""
    for philosophy in GmPhilosophy:
        for window in CompetitiveWindow:
            p29 = _pv(29, 0, philosophy, window)
            p30 = _pv(30, 0, philosophy, window)
            assert p30 > p29, (
                "expected the documented pre-migration-009 inversion at 29->30 to "
                f"hold ({philosophy.value}, {window.value}): p29={p29} p30={p30}"
            )
            margin = (p30 - p29) / p29
            assert 0.05 <= margin <= 0.16, (philosophy.value, window.value, margin)

    default_retool_margin = (
        _pv(30, 0, _DEFAULT_PHILOSOPHY, CompetitiveWindow.RETOOL)
        - _pv(29, 0, _DEFAULT_PHILOSOPHY, CompetitiveWindow.RETOOL)
    ) / _pv(29, 0, _DEFAULT_PHILOSOPHY, CompetitiveWindow.RETOOL)
    assert default_retool_margin == pytest.approx(0.123, abs=0.005)


def test_boundary_30_to_31_is_the_deliberate_horizon_cliff():
    # The horizon split (4 seasons -> 2 seasons) produces a real cliff
    # regardless of row 30's repair status -- this boundary is unaffected
    # by Decision P2-33's data repair and must hold today.
    for philosophy in GmPhilosophy:
        for window in CompetitiveWindow:
            p30 = _pv(30, 0, philosophy, window)
            p31 = _pv(31, 0, philosophy, window)
            assert p31 < p30, (philosophy.value, window.value, p30, p31)
            cliff = (p30 - p31) / p30
            assert cliff > 0.25, "the 30->31 horizon split should be a real cliff, not a soft step"


def test_pick_value_is_monotone_non_increasing_across_60_slots_except_the_documented_29_30_gap():
    for philosophy in GmPhilosophy:
        for window in CompetitiveWindow:
            values = [_pv(slot, 0, philosophy, window) for slot in range(1, 61)]
            for slot in range(1, 60):
                a, b = values[slot - 1], values[slot]
                if slot == 29:  # the 29 (index 28) -> 30 (index 29) boundary
                    continue  # documented, expected exception -- see the dedicated test above
                assert b <= a + 1e-6, (
                    f"pick_value not monotone non-increasing at slot {slot}->{slot + 1} "
                    f"({philosophy.value}, {window.value}): {a} -> {b}"
                )


def test_a_pick_four_years_out_is_worth_less_than_the_same_slot_next_year():
    for slot in (1, 15, 45):
        for philosophy in GmPhilosophy:
            for window in CompetitiveWindow:
                one_year = _pv(slot, 1, philosophy, window)
                four_years = _pv(slot, 4, philosophy, window)
                assert four_years < one_year, (slot, philosophy.value, window.value)


def test_youth_developer_values_every_pick_above_star_maxer():
    for window in CompetitiveWindow:
        for slot in range(1, 61):
            youth = _pv(slot, 0, GmPhilosophy.YOUTH_DEVELOPER, window)
            star = _pv(slot, 0, GmPhilosophy.STAR_MAXER, window)
            assert youth > star, (slot, window.value, youth, star)


# ---------------------------------------------------------------------------
# Expected slot -- the regression-toward-the-mean formula
# ---------------------------------------------------------------------------


def test_expected_slot_worst_team_matches_the_docs_worked_values():
    # projected_slot=1 (the worst team, inverted to lottery order).
    assert expected_slot(1, 3) == pytest.approx(12.4, abs=0.05)
    assert expected_slot(1, 4) == pytest.approx(13.6, abs=0.05)


def test_expected_slot_regresses_toward_the_mean_as_years_out_grows():
    # A far-future pick's expected slot should sit closer to 15.5 than a
    # near-future one, for a team not already projected near the mean.
    for projected_slot in (1, 30):
        near = expected_slot(projected_slot, 1)
        far = expected_slot(projected_slot, 5)
        assert abs(far - 15.5) < abs(near - 15.5)


def test_expected_slot_at_years_out_zero_is_the_projected_slot_itself():
    assert expected_slot(7, 0) == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# projected_slot_from_window_score -- critic round 10, B3: this is the one
# function that carries the `managers.window` dependency (§12's whole
# stated reason `picks.py` follows `window.py`) and it shipped with zero
# test coverage. Each team below carries exactly one `RosterPlayer` with no
# contract and a fixed age, so `future_strength` (no locked-in players ->
# padded fully at REPLACEMENT_OVR) and `mean_age` (identical age everywhere)
# are constant across teams and contribute z=0 -- isolating `team_strength`/
# `win_pct`/`top2_ovr`, all three monotone increasing in `overall_cached`,
# so `window_score` is a clean, strictly monotone function of roster
# strength here, with no need to monkeypatch `_z_scores` the way
# `test_window.py` does for its own degenerate-league cases.
# ---------------------------------------------------------------------------

_PSFWS_SEASON = 2026


def _league_ranked_by_overall(overalls: tuple[int, ...]) -> LeagueSnapshot:
    teams = []
    for team_id, ovr in enumerate(overalls, start=1):
        roster = (
            make_roster_player(
                70_000 + team_id, age=26, season=_PSFWS_SEASON, overall_cached=ovr,
                peak_age_start=27, peak_age_end=31,
            ),
        )
        snapshot = build_team_snapshot(make_team(team_id), roster, (), CAP_SETTINGS)
        teams.append(snapshot)
    return build_league_snapshot(1, _PSFWS_SEASON, tuple(teams))


def test_projected_slot_from_window_score_worst_team_gets_slot_1_best_gets_last():
    # team_id 1 has the weakest roster (overall 60), team_id 5 the
    # strongest (overall 90) -- "the worst window_score gets projected_slot
    # = 1" (§4.6), inverted lottery order.
    league = _league_ranked_by_overall((60, 65, 70, 80, 90))
    assert projected_slot_from_window_score(league.team_by_id(1), league) == 1
    assert projected_slot_from_window_score(league.team_by_id(5), league) == 5
    assert projected_slot_from_window_score(league.team_by_id(3), league) == 3


def test_projected_slot_from_window_score_matches_a_full_manual_ranking():
    overalls = (55, 90, 62, 78, 70, 40, 85)
    league = _league_ranked_by_overall(overalls)
    scores = {t.id: window_score(t, league) for t in league.teams}
    worst_first = sorted(scores, key=lambda tid: scores[tid])
    for expected_rank, team_id in enumerate(worst_first, start=1):
        actual = projected_slot_from_window_score(league.team_by_id(team_id), league)
        assert actual == expected_rank, (team_id, scores)


def test_projected_slot_from_window_score_raises_key_error_for_a_team_not_in_the_league():
    league = _league_ranked_by_overall((60, 70))
    outsider = build_team_snapshot(make_team(999), (), (), CAP_SETTINGS)
    with pytest.raises(KeyError):
        projected_slot_from_window_score(outsider, league)


def test_projected_slot_from_window_score_genuinely_depends_on_window_score():
    # Two leagues, identical except team 1's own roster strength -- if the
    # function silently used a stand-in (a fixed default, a different
    # ranking key) instead of the real window_score, team 1's projected
    # slot would not move between the two leagues.
    league_team1_weak = _league_ranked_by_overall((30, 70, 70, 70, 70))
    league_team1_strong = _league_ranked_by_overall((95, 70, 70, 70, 70))
    slot_weak = projected_slot_from_window_score(
        league_team1_weak.team_by_id(1), league_team1_weak
    )
    slot_strong = projected_slot_from_window_score(
        league_team1_strong.team_by_id(1), league_team1_strong
    )
    assert slot_weak == 1, "team 1 is the weakest of the five -> worst window_score -> slot 1"
    assert slot_strong == 5, "team 1 is now the strongest of the five -> slot 5"
    assert slot_weak != slot_strong


def test_expected_slot_regression_chained_off_a_real_projected_slot_from_window_score():
    # §4.6's own worked check ("today's worst team's pick lands at slot
    # 12.4 three years out, 13.6 four years out") pinned end-to-end,
    # through the real window_score ranking rather than a bare literal
    # projected_slot=1 -- ties expected_slot's regression to the actual
    # `window.py` dependency this function exists to carry.
    league = _league_ranked_by_overall((30, 60, 65, 70, 75, 80, 85, 90, 95, 99))
    worst_team = league.team_by_id(1)
    projected_slot = projected_slot_from_window_score(worst_team, league)
    assert projected_slot == 1

    assert expected_slot(projected_slot, 3) == pytest.approx(12.4, abs=0.05)
    assert expected_slot(projected_slot, 4) == pytest.approx(13.6, abs=0.05)
    # regression toward the mean (15.5): a further-out pick lands at a
    # LATER expected slot than the raw projected_slot=1 ranking suggests,
    # and moves closer to 15.5 as years_out grows.
    assert expected_slot(projected_slot, 3) > projected_slot
    gap_3y = abs(expected_slot(projected_slot, 3) - 15.5)
    gap_4y = abs(expected_slot(projected_slot, 4) - 15.5)
    assert gap_4y < gap_3y


def test_years_out_decay_pinned_to_the_exact_formula_at_0_1_and_3plus():
    league = _league_ranked_by_overall((30, 99))
    projected_slot = projected_slot_from_window_score(league.team_by_id(1), league)
    assert projected_slot == 1

    for years_out in (0, 1, 3, 5):
        expected = 15.5 + (projected_slot - 15.5) * (0.60**years_out)
        assert expected_slot(projected_slot, years_out) == pytest.approx(expected)

    # the |slot - 15.5| gap must shrink monotonically as years_out grows
    # (0.60^years_out is a strictly decreasing multiplier on that gap).
    gaps = [abs(expected_slot(projected_slot, y) - 15.5) for y in (0, 1, 3, 5)]
    assert gaps == sorted(gaps, reverse=True)
    assert gaps[0] == pytest.approx(14.5)  # years_out=0: no decay at all


# ---------------------------------------------------------------------------
# Protection conveyance -- the rollover recursion
# ---------------------------------------------------------------------------


def test_protection_worked_example_top5_protected_expected_slot_2():
    # §4.6's fully worked example: expected_slot=2, protection_through=5,
    # REBUILD, default philosophy.
    rebuild = CompetitiveWindow.REBUILD
    unprotected = _pv(2, 0, _DEFAULT_PHILOSOPHY, rebuild)
    protected = protected_pick_value(
        2.0, 5, _DEFAULT_PHILOSOPHY, rebuild, rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS
    )
    assert unprotected == pytest.approx(717.0, abs=0.5)
    assert protected == pytest.approx(276.1, abs=0.5)
    share = protected / unprotected
    assert 0.30 <= share <= 0.55
    assert share == pytest.approx(0.385, abs=0.01)


def test_protection_sanity_case_top5_protected_expected_slot_15():
    rebuild = CompetitiveWindow.REBUILD
    unprotected = _pv(15, 0, _DEFAULT_PHILOSOPHY, rebuild)
    protected = protected_pick_value(
        15.0, 5, _DEFAULT_PHILOSOPHY, rebuild, rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS
    )
    share = protected / unprotected
    assert share > 0.90
    assert share == pytest.approx(0.983, abs=0.01)


def test_conveyance_probability_is_a_logistic_centered_on_protection_through():
    assert conveyance_probability(5, 5) == pytest.approx(0.5)
    assert conveyance_probability(2, 5) == pytest.approx(0.3208, abs=0.001)


def test_slot_if_conveys_lands_strictly_outside_the_protection():
    # A protected pick that conveys always lands worse than its
    # protection threshold -- never at the unconditional expected_slot
    # when that slot would itself have been protected (round 1's fixed
    # bug, §4.6).
    assert slot_if_conveys(2.0, 5) == 6
    assert slot_if_conveys(15.0, 5) == 15  # already outside protection


def test_second_round_fallback_is_two_slot_45_picks():
    expected = 2 * _pv(45, 3, _DEFAULT_PHILOSOPHY, CompetitiveWindow.REBUILD)
    actual = second_round_fallback(
        3, _DEFAULT_PHILOSOPHY, CompetitiveWindow.REBUILD,
        rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS,
    )
    assert actual == pytest.approx(expected)


def test_protected_value_never_exceeds_the_unprotected_equivalent():
    for philosophy in GmPhilosophy:
        for window in CompetitiveWindow:
            unprotected = _pv(3, 0, philosophy, window)
            protected = protected_pick_value(
                3.0, 5, philosophy, window, rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS
            )
            assert protected <= unprotected + 1e-6, (philosophy.value, window.value)


# ---------------------------------------------------------------------------
# Swap rights
# ---------------------------------------------------------------------------


def test_swap_rights_value_is_half_the_positive_gap():
    assert swap_rights_value(200.0, 120.0) == pytest.approx(40.0)


def test_swap_rights_value_floors_at_zero_when_own_pick_is_worse():
    assert swap_rights_value(100.0, 150.0) == 0.0


# ---------------------------------------------------------------------------
# e_ovr_y sanity -- entry at y=0, mid-progression by y=3, never exceeds peak
# ---------------------------------------------------------------------------


def test_e_ovr_y_starts_at_entry_and_never_exceeds_peak():
    for slot in (1, 15, 45, 60):
        assert e_ovr_y(slot, 0) == pytest.approx(entry_ovr(slot))
        for y in range(pick_horizon(slot)):
            assert e_ovr_y(slot, y) <= peak_ovr(slot) + 1e-9
