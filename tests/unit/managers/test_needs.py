"""Tier 1: the positional need matrix -- §5's derivation and §12 step 6's
own verify list, followed bullet-for-bullet (docs/design/phase-2-manager-
minds.md).

**The saturation-test fixture (`_BALANCED_ROSTER_SPECS`) is not a hand-
computed decimal check -- it's a hand-*searched* one, and that's stated
here rather than left implicit.** §12 step 6's own bullet says "on a
hand-built balanced 14-man roster ... every DEPTH-tier glut and every
ROTATION-tier need is 0," and a short derivation shows why that's a tight
constraint, not a loose one: the ROTATION-cumulative group (the 1 STAR + 4
STARTER + 3 ROTATION players) always contributes exactly 8.0 of total
coverage mass (`Sum_pos coverage(p, pos) == 1.0` per player, 8 players),
while `Sum_pos target(pos, ROTATION)` is exactly `4*1.60 + 1.40 = 7.80` --
only 0.20 of total slack across all 5 positions combined, so *every*
position's ROTATION-cumulative supply has to land within a hair of its
own target simultaneously. A naive PG/SG/SF/PF/C round-robin over 14
primary-only assignments provably cannot hit this (verified by hand: it
leaves PF and C roughly 0.4 short each) -- reaching zero requires primary
*and* secondary positions chosen so the 0.20 of slack lands in the right
places. `_BALANCED_ROSTER_SPECS` below is one concrete, hand-built
solution meeting §12 step 6's own literal recipe ("one 84, four 76, three
70, six 63") that also actually clears every bar; it does not claim to be
the *only* one.
"""
from __future__ import annotations

import inspect
import random

import pytest

from dba2.domain.player import Position
from dba2.domain.team import GmPhilosophy
from dba2.managers import needs
from dba2.managers.needs import (
    NEED_BONUS_CLAMP_HI,
    NEED_BONUS_CLAMP_LO,
    NeedTier,
    build_need_matrix,
    clamp_need_bonus,
    need_bonus,
    position_coverage,
    tier_for_overall,
)
from dba2.managers.philosophy import PROFILES
from dba2.managers.snapshot import build_team_snapshot
from dba2.sim import derivation
from tests.fixtures.managers import CAP_SETTINGS, make_roster_player
from tests.fixtures.sim import make_team

_SEASON = 2026
_POSITIONS = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)


# ---------------------------------------------------------------------------
# Bullet 1 -- coverage sums to 1.0 for every player, with and without a
# secondary position
# ---------------------------------------------------------------------------


def test_position_coverage_sums_to_one_with_no_secondary():
    rp = make_roster_player(
        1, age=25, season=_SEASON, overall_cached=70, primary_position=Position.SG,
        peak_age_start=27, peak_age_end=31,
    )
    coverage = position_coverage(rp)
    assert set(coverage) == set(Position)
    assert sum(coverage.values()) == pytest.approx(1.0)
    # §5's own worked example, verbatim: a SG with no secondary normalizes
    # to PG 0.167, SG 0.667, SF 0.167 (PF/C get nothing -- distance 2/3).
    assert coverage[Position.PG] == pytest.approx(1 / 6)
    assert coverage[Position.SG] == pytest.approx(2 / 3)
    assert coverage[Position.SF] == pytest.approx(1 / 6)
    assert coverage[Position.PF] == pytest.approx(0.0)
    assert coverage[Position.C] == pytest.approx(0.0)


def test_position_coverage_sums_to_one_with_a_secondary_position():
    rp = make_roster_player(
        2, age=25, season=_SEASON, overall_cached=70,
        primary_position=Position.SG, secondary_position=Position.PF,
        peak_age_start=27, peak_age_end=31,
    )
    coverage = position_coverage(rp)
    assert set(coverage) == set(Position)
    assert sum(coverage.values()) == pytest.approx(1.0)
    # raw: SG 1.00 (primary), PF 0.50 (secondary), PG/SF 0.25 each (adjacent
    # to primary SG), C 0.25 (adjacent to secondary PF, not already
    # counted; SF is already counted via primary-adjacency so it does not
    # collect a second 0.25) -- total raw 2.25.
    assert coverage[Position.SG] == pytest.approx(1.00 / 2.25)
    assert coverage[Position.PF] == pytest.approx(0.50 / 2.25)
    assert coverage[Position.PG] == pytest.approx(0.25 / 2.25)
    assert coverage[Position.SF] == pytest.approx(0.25 / 2.25)
    assert coverage[Position.C] == pytest.approx(0.25 / 2.25)


# ---------------------------------------------------------------------------
# Bullet 2 -- a roster with no center shows maximum need at (C, STARTER)
# ---------------------------------------------------------------------------


def test_roster_with_no_center_shows_maximum_need_at_starter_center():
    # PG/SG/SF only -- C's sole neighbor is PF, so with no PF and no C
    # anywhere (primary or secondary), C's supply is exactly 0 at every
    # tier, by construction.
    roster = tuple(
        make_roster_player(
            100 + i, age=26, season=_SEASON, overall_cached=76,
            primary_position=[Position.PG, Position.SG, Position.SF][i % 3],
            peak_age_start=27, peak_age_end=31,
        )
        for i in range(9)
    )
    snapshot = build_team_snapshot(make_team(1), roster, (), CAP_SETTINGS)
    matrix = build_need_matrix(snapshot)
    assert matrix.supply[Position.C][NeedTier.STARTER] == pytest.approx(0.0)
    assert matrix.need[Position.C][NeedTier.STARTER] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Bullet 3 -- three 82-overall point guards show a glut penalty at (PG, STAR)
# ---------------------------------------------------------------------------


def test_three_star_point_guards_show_a_glut_penalty_at_pg_star():
    roster = tuple(
        make_roster_player(
            200 + i, age=26, season=_SEASON, overall_cached=82,
            primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        )
        for i in range(3)
    )
    snapshot = build_team_snapshot(make_team(1), roster, (), CAP_SETTINGS)
    matrix = build_need_matrix(snapshot)
    assert matrix.supply[Position.PG][NeedTier.STAR] == pytest.approx(2.4)
    assert matrix.glut[Position.PG][NeedTier.STAR] > 0.0
    # target 0.40, GLUT_SLACK 0.75 -> (2.4 - 0.40 - 0.75) / 0.75 > 1, so the
    # clamp itself saturates at 1.0 here.
    assert matrix.glut[Position.PG][NeedTier.STAR] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Bullet 4 -- every need/glut score is in [0, 1], over several differently
# shaped rosters (empty, single-position)
# ---------------------------------------------------------------------------


def _assert_matrix_in_unit_interval(matrix):
    for pos in Position:
        for tier in NeedTier:
            need_v = matrix.need[pos][tier]
            glut_v = matrix.glut[pos][tier]
            assert 0.0 <= need_v <= 1.0, (pos, tier, "need", need_v)
            assert 0.0 <= glut_v <= 1.0, (pos, tier, "glut", glut_v)


def test_all_scores_are_in_the_unit_interval_across_several_roster_shapes():
    empty_snapshot = build_team_snapshot(make_team(1), (), (), CAP_SETTINGS)
    _assert_matrix_in_unit_interval(build_need_matrix(empty_snapshot))

    single_position_roster = tuple(
        make_roster_player(
            300 + i, age=25, season=_SEASON, overall_cached=60 + i,
            primary_position=Position.C, peak_age_start=27, peak_age_end=31,
        )
        for i in range(6)
    )
    single_position_snapshot = build_team_snapshot(
        make_team(2), single_position_roster, (), CAP_SETTINGS
    )
    _assert_matrix_in_unit_interval(build_need_matrix(single_position_snapshot))


# ---------------------------------------------------------------------------
# Bullet 5 -- the matrix is exactly 5x4, and the target table is the only
# place its eight numbers appear
# ---------------------------------------------------------------------------


def test_matrix_is_exactly_five_positions_by_four_tiers():
    roster = tuple(
        make_roster_player(
            400 + i, age=25, season=_SEASON, overall_cached=70,
            primary_position=_POSITIONS[i % 5], peak_age_start=27, peak_age_end=31,
        )
        for i in range(5)
    )
    snapshot = build_team_snapshot(make_team(1), roster, (), CAP_SETTINGS)
    matrix = build_need_matrix(snapshot)
    for grid in (matrix.supply, matrix.need, matrix.glut):
        assert set(grid) == set(Position), f"expected exactly 5 position rows, got {set(grid)}"
        for pos, row in grid.items():
            assert set(row) == set(NeedTier), (
                f"expected exactly 4 tier columns at {pos}, got {set(row)}"
            )


def test_target_table_numbers_appear_exactly_once_in_the_module_source():
    source = inspect.getsource(needs)
    assert source.count("(0.40, 1.00, 1.60, 2.60)") == 1, (
        "the non-center target row must be declared exactly once"
    )
    assert source.count("(0.30, 1.00, 1.40, 2.20)") == 1, (
        "the center target row must be declared exactly once"
    )


# ---------------------------------------------------------------------------
# Bullet 6 -- the saturation regression test: a hand-built balanced 14-man
# roster has zero DEPTH-tier glut and zero ROTATION-tier need everywhere
# (see module docstring for how this exact fixture was derived).
# ---------------------------------------------------------------------------

_BALANCED_ROSTER_SPECS: tuple[tuple[int, Position, Position | None], ...] = (
    (84, Position.SG, Position.C),
    (76, Position.C, Position.SF),
    (76, Position.PG, Position.SF),
    (76, Position.SG, Position.PF),
    (76, Position.PF, Position.SF),
    (70, Position.PG, Position.PF),
    (70, Position.SF, Position.C),
    (70, Position.PG, Position.PF),
    (63, Position.PG, Position.C),
    (63, Position.C, Position.SF),
    (63, Position.SF, Position.PG),
    (63, Position.SG, Position.PG),
    (63, Position.SF, Position.PG),
    (63, Position.C, Position.PF),
)
assert len(_BALANCED_ROSTER_SPECS) == 14
assert sum(1 for ovr, _, _ in _BALANCED_ROSTER_SPECS if ovr == 84) == 1
assert sum(1 for ovr, _, _ in _BALANCED_ROSTER_SPECS if ovr == 76) == 4
assert sum(1 for ovr, _, _ in _BALANCED_ROSTER_SPECS if ovr == 70) == 3
assert sum(1 for ovr, _, _ in _BALANCED_ROSTER_SPECS if ovr == 63) == 6


def test_balanced_fourteen_man_roster_has_zero_depth_glut_and_zero_rotation_need():
    roster = tuple(
        make_roster_player(
            500 + i, age=27, season=_SEASON, overall_cached=ovr,
            primary_position=primary, secondary_position=secondary,
            peak_age_start=27, peak_age_end=31,
        )
        for i, (ovr, primary, secondary) in enumerate(_BALANCED_ROSTER_SPECS)
    )
    snapshot = build_team_snapshot(make_team(1), roster, (), CAP_SETTINGS)
    matrix = build_need_matrix(snapshot)

    for pos in Position:
        assert matrix.glut[pos][NeedTier.DEPTH] == pytest.approx(0.0), (
            pos, "DEPTH glut", matrix.glut[pos][NeedTier.DEPTH],
            "supply", matrix.supply[pos][NeedTier.DEPTH],
        )
        assert matrix.need[pos][NeedTier.ROTATION] == pytest.approx(0.0), (
            pos, "ROTATION need", matrix.need[pos][NeedTier.ROTATION],
            "supply", matrix.supply[pos][NeedTier.ROTATION],
        )


# ---------------------------------------------------------------------------
# tier_for_overall -- the cutoff table itself, since every test above
# depends on it being right
# ---------------------------------------------------------------------------


def test_tier_for_overall_matches_the_stated_cutoffs():
    assert tier_for_overall(99) is NeedTier.STAR
    assert tier_for_overall(80) is NeedTier.STAR
    assert tier_for_overall(79) is NeedTier.STARTER
    assert tier_for_overall(73) is NeedTier.STARTER
    assert tier_for_overall(72) is NeedTier.ROTATION
    assert tier_for_overall(66) is NeedTier.ROTATION
    assert tier_for_overall(65) is NeedTier.DEPTH
    assert tier_for_overall(40) is NeedTier.DEPTH


def test_tier_cutoffs_are_derived_from_derivations_tier_talent_means():
    # §5 line 550: the cutoffs "come straight from `derivation._TIER_TALENT`'s
    # means (84/76/70/63) cut at midpoints." `needs.py` hardcodes 80/73/66
    # rather than importing the private `_TIER_TALENT` at runtime (it answers
    # a different question -- a generative talent draw's center, not a
    # scored-roster tier boundary -- and re-deriving from it every call would
    # buy nothing). That means nothing here *fails loudly* if `_TIER_TALENT`
    # is ever retuned; this test is that guard. If it goes red, `needs.py`'s
    # hardcoded cutoffs need to move with it, by hand.
    star = derivation._TIER_TALENT[derivation.RosterTier.STAR].mean
    starter = derivation._TIER_TALENT[derivation.RosterTier.STARTER].mean
    rotation = derivation._TIER_TALENT[derivation.RosterTier.ROTATION].mean
    bench = derivation._TIER_TALENT[derivation.RosterTier.BENCH].mean
    assert (star, starter, rotation, bench) == (84.0, 76.0, 70.0, 63.0)

    assert needs._STAR_MIN_OVERALL == round((star + starter) / 2)
    assert needs._STARTER_MIN_OVERALL == round((starter + rotation) / 2)
    assert needs._ROTATION_MIN_OVERALL == round((rotation + bench) / 2)


# ---------------------------------------------------------------------------
# Bullet 7 -- the need_bonus clamp binds on < 2% of (player, team) pairs
# over a synthetic 30-team league (the one sampled, distributional
# assertion in this step's verify list -- see test_valuation.py's own
# `test_market_value_divergence_across_teams_for_players_with_real_growth_
# room` for this project's established "seeded random.Random, not a bare
# random.Random()" convention for exactly this kind of check).
#
# **Why this is expected to read near 0%, not just under 2%:** `need_
# bonus`'s two terms are each `fit_sens * WEIGHT * (a coverage-weighted
# average of a [0, 1] matrix cell)`, and a coverage-weighted average of
# values in [0, 1] is itself always in [0, 1] (`Sum_pos coverage(p, pos) ==
# 1.0` makes it a convex combination). So `need_bonus` is bounded in
# `[-fit_sens * GLUT_LOSS, +fit_sens * NEED_GAIN]` algebraically, for
# *every* roster shape -- and the highest `fit_sens` any of the seven
# `PhilosophyProfile`s carries is `egalitarian`'s 1.30, giving a provable
# range of `[-0.325, +0.39]`, entirely inside the clamp's `[-0.35, +0.50]`
# already. The clamp cannot bind at all under today's `PROFILES`/`GmTuning`
# values -- this test still exercises the real formula over a large,
# varied sample (not asserting the algebra directly) so it would catch a
# future `fit_sens`, `NEED_GAIN`, or `GLUT_LOSS` retune that pushes the
# bound past the clamp's own.
# ---------------------------------------------------------------------------


def _synthetic_team(team_id: int, *, season: int):
    roster = []
    for j in range(14):
        ovr = 55 + ((team_id * 7 + j * 5) % 45)
        primary = _POSITIONS[(team_id + j) % 5]
        secondary = _POSITIONS[(team_id + j + 2) % 5] if j % 3 == 0 else None
        roster.append(
            make_roster_player(
                team_id * 1000 + j, age=25, season=season, overall_cached=ovr,
                primary_position=primary, secondary_position=secondary,
                peak_age_start=27, peak_age_end=31,
            )
        )
    return build_team_snapshot(make_team(team_id), tuple(roster), (), CAP_SETTINGS)


def _synthetic_candidates(count: int, *, season: int):
    players = []
    for i in range(count):
        ovr = 55 + ((i * 11) % 45)
        primary = _POSITIONS[i % 5]
        secondary = _POSITIONS[(i + 3) % 5] if i % 4 == 0 else None
        players.append(
            make_roster_player(
                90_000 + i, age=25, season=season, overall_cached=ovr,
                primary_position=primary, secondary_position=secondary,
                peak_age_start=27, peak_age_end=31,
            )
        )
    return players


def test_need_bonus_clamp_binds_on_less_than_two_percent_of_pairs_over_a_synthetic_league():
    season = _SEASON
    teams = [_synthetic_team(tid, season=season) for tid in range(1, 31)]
    matrices = {t.id: build_need_matrix(t) for t in teams}
    candidates = _synthetic_candidates(60, season=season)
    philosophies = list(GmPhilosophy)

    rng = random.Random(20260828)
    total = 0
    bound = 0
    for team in teams:
        matrix = matrices[team.id]
        for player in rng.sample(candidates, 20):
            philosophy = rng.choice(philosophies)
            profile = PROFILES[philosophy]
            ovr = player.player.overall_cached
            raw = need_bonus(player, ovr, matrix, profile)
            clamped = clamp_need_bonus(raw)
            total += 1
            if clamped != pytest.approx(raw, abs=1e-9):
                bound += 1

    assert total >= 500, "sample too small to make a distributional claim"
    fraction_bound = bound / total
    assert fraction_bound < 0.02, (
        f"clamp bound on {fraction_bound:.1%} of {total} (player, team) pairs, "
        f">= the 2% ceiling §12 step 6's verify list states"
    )


def test_clamp_need_bonus_bounds_match_the_fit_value_formula():
    assert NEED_BONUS_CLAMP_LO == pytest.approx(-0.35)
    assert NEED_BONUS_CLAMP_HI == pytest.approx(0.50)
    assert clamp_need_bonus(-10.0) == pytest.approx(NEED_BONUS_CLAMP_LO)
    assert clamp_need_bonus(10.0) == pytest.approx(NEED_BONUS_CLAMP_HI)
    assert clamp_need_bonus(0.0) == pytest.approx(0.0)
