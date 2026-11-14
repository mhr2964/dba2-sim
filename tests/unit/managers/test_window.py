"""Tier 1: `CompetitiveWindow` classification -- §7's score, rank bands,
two absolute guards, Decision P2-14's hysteresis cap, and the map onto
`Timeline` (docs/design/phase-2-manager-minds.md, §12 step 4's own verify
list, followed precisely rather than sampled from).

The single most important test in this file is
`test_classification_is_identical_across_all_30_teams_and_7_philosophies`
-- §12 step 4's own regression test for the circular-dependency bug that
took design rounds 2-5 to actually close: "classifying the same roster
from any of the 30 teams' snapshots, under all seven philosophies,
returns an identical window ... 210 evaluations, one answer."
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
from itertools import cycle

import pytest

from dba2.domain.team import GmPhilosophy, Timeline
from dba2.managers import window
from dba2.managers.explain import ReasonTerm
from dba2.managers.scouting import age_ramp, projected_peak
from dba2.managers.snapshot import build_league_snapshot, build_team_snapshot
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow
from dba2.managers.window import (
    SCORE_WEIGHTS,
    apply_hysteresis,
    classify_window,
    future_strength,
    mean_age,
    projected_ovr_2,
    score_terms,
    team_strength,
    to_timeline,
    top2_ovr,
    window_score,
)
from tests.fixtures.managers import CAP_SETTINGS, make_contract, make_roster_player
from tests.fixtures.sim import make_team

# This file writes scratch files into `dba2/` or scans it (`tests/conftest.py`): one xdist
# group keeps every such writer and reader on one worker, run serially.
pytestmark = pytest.mark.xdist_group("source-tree")

_SEASON = 2026


# ---------------------------------------------------------------------------
# The five raw inputs -- hand-computed expectations over shuffled rosters
# (shuffled so the test also proves each function sorts by overall_cached
# itself rather than trusting construction order).
# ---------------------------------------------------------------------------


def _ten_player_roster():
    # (overall, age), matched by position; overall values chosen distinct
    # and NOT already sorted, ages independently distinct from overall so
    # a sort-by-age bug (instead of sort-by-overall) would be caught too.
    specs = [
        (60, 24), (90, 22), (55, 33), (75, 28), (45, 27),
        (85, 35), (65, 24), (50, 20), (70, 26), (80, 31),
    ]
    return tuple(
        make_roster_player(
            8000 + i, age=age, season=_SEASON, overall_cached=ovr,
            peak_age_start=27, peak_age_end=31,
        )
        for i, (ovr, age) in enumerate(specs)
    )


def test_team_strength_sorts_desc_on_overall_and_weights_correctly():
    roster = _ten_player_roster()
    snapshot = build_team_snapshot(make_team(1), roster, (), CAP_SETTINGS)
    # sorted desc: 90,85,80,75,70,65,60,55,50,45
    expected = (
        0.16 * 90 + 0.15 * 85 + 0.13 * 80 + 0.12 * 75 + 0.11 * 70
        + 0.09 * 65 + 0.08 * 60 + 0.06 * 55 + 0.05 * 50 + 0.05 * 45
    )
    assert team_strength(snapshot) == pytest.approx(expected)


def test_top2_ovr_is_the_mean_of_the_two_highest():
    roster = _ten_player_roster()
    snapshot = build_team_snapshot(make_team(1), roster, (), CAP_SETTINGS)
    assert top2_ovr(snapshot) == pytest.approx((90 + 85) / 2)


def test_top2_ovr_handles_a_roster_with_fewer_than_two_players():
    snapshot = build_team_snapshot(make_team(1), (), (), CAP_SETTINGS)
    assert top2_ovr(snapshot) == 0.0
    one_player = (
        make_roster_player(
            1, age=25, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
        ),
    )
    snapshot_one = build_team_snapshot(make_team(2), one_player, (), CAP_SETTINGS)
    assert top2_ovr(snapshot_one) == pytest.approx(70.0)


def test_mean_age_weights_ages_in_the_same_overall_desc_order_as_team_strength():
    roster = _ten_player_roster()
    snapshot = build_team_snapshot(make_team(1), roster, (), CAP_SETTINGS)
    # sorted desc on overall (90,85,80,75,70,65,60,55,50,45) carries ages
    # (22,35,31,28,26,24,24,33,20,27) in that same order -- the last two
    # weights are both 0.05, so their relative order doesn't affect the sum.
    expected = (
        0.16 * 22 + 0.15 * 35 + 0.13 * 31 + 0.12 * 28 + 0.11 * 26
        + 0.09 * 24 + 0.08 * 24 + 0.06 * 33 + 0.05 * 20 + 0.05 * 27
    )
    assert mean_age(snapshot, _SEASON) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# projected_ovr_2 / future_strength -- the circularity-critical formula
# ---------------------------------------------------------------------------


def test_projected_ovr_2_matches_the_formula_built_from_independent_primitives():
    """Re-derives §7's formula from `managers.scouting`'s own, separately
    tested `age_ramp`/`projected_peak` primitives, rather than pinning a
    hand-computed decimal -- the same discipline `test_valuation.py`
    uses for its grid checks."""
    player = make_roster_player(
        1, age=21, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
        ratings_overrides={"potential": 78, "potential_variance": 70},
    )
    ovr = player.player.overall_cached
    age = 21.0
    peak_age_start = 27.0
    peak = projected_peak(ovr, age, peak_age_start, player.ratings.potential)
    ramp_2 = age_ramp(age + 2.0, peak_age_start)
    ramp_0 = age_ramp(age, peak_age_start)
    expected = ovr + DEFAULT_TUNING.window_input_upside * max(0.0, peak - ovr) * ramp_2 / ramp_0

    assert projected_ovr_2(player, _SEASON) == pytest.approx(expected)


def test_projected_ovr_2_equals_ovr_exactly_for_a_player_at_or_past_peak():
    # age_ramp is 0 at and above peak_age_start for both t=0 and t=2, so
    # room is exactly 0 regardless of the residual -- same shape as
    # scouting.py's own Gap E regression test, one layer up.
    player = make_roster_player(
        1, age=34, season=_SEASON, overall_cached=80, peak_age_start=27, peak_age_end=31,
        ratings_overrides={"potential": 99, "potential_variance": 90},
    )
    assert projected_ovr_2(player, _SEASON) == pytest.approx(80.0)


def test_projected_ovr_2_does_not_depend_on_confidence():
    """§7: no `confidence` damping in `projected_ovr_2` -- two players
    identical except `confidence_f` must project identically."""
    low_conf = make_roster_player(
        1, age=22, season=_SEASON, overall_cached=72, peak_age_start=28, peak_age_end=32,
        ratings_overrides={"potential": 82, "potential_variance": 75},
    )
    high_conf = dataclasses.replace(low_conf, confidence_f=1.0)
    low_conf = dataclasses.replace(low_conf, confidence_f=0.10)
    assert projected_ovr_2(low_conf, _SEASON) == pytest.approx(projected_ovr_2(high_conf, _SEASON))


def test_future_strength_pads_short_rosters_at_replacement_ovr():
    season = _SEASON
    target_season = season + 2

    def _past_peak_player(pid: int, ovr: int) -> object:
        return make_roster_player(
            pid, age=32, season=season, overall_cached=ovr, peak_age_start=27, peak_age_end=31,
            contract=make_contract(pid, start_season=season, salaries=(10_000_000,) * 4),
        )

    roster = (_past_peak_player(1, 90), _past_peak_player(2, 85), _past_peak_player(3, 80))
    assert any(y.season == target_season for y in roster[0].contract.years)

    snapshot = build_team_snapshot(make_team(1), roster, (), CAP_SETTINGS)
    # past-peak players project at exactly their own overall_cached (see
    # test_projected_ovr_2_equals_ovr_exactly_for_a_player_at_or_past_peak),
    # so the expected padded vector is [90, 85, 80, 60, 60, 60, 60, 60, 60, 60].
    expected = (
        0.16 * 90 + 0.15 * 85 + 0.13 * 80
        + (0.12 + 0.11 + 0.09 + 0.08 + 0.06 + 0.05 + 0.05) * DEFAULT_TUNING.replacement_ovr
    )
    assert future_strength(snapshot, season) == pytest.approx(expected)


def test_future_strength_excludes_players_not_under_contract_in_season_plus_2():
    season = _SEASON
    expiring = make_roster_player(
        1, age=32, season=season, overall_cached=95, peak_age_start=27, peak_age_end=31,
        # expires at `season`, so it's not under contract at `season + 2`.
        contract=make_contract(1, start_season=season, salaries=(20_000_000,)),
    )
    snapshot = build_team_snapshot(make_team(1), (expiring,), (), CAP_SETTINGS)
    # the 95-overall expiring player contributes nothing -- ten replacement-level slots.
    expected = DEFAULT_TUNING.replacement_ovr  # weighted sum over ten equal values == that value
    assert future_strength(snapshot, season) == pytest.approx(float(expected))


# ---------------------------------------------------------------------------
# window_score -- the exact weighted z-score formula, all five inputs pinned
# ---------------------------------------------------------------------------


def test_window_score_is_the_exact_weighted_z_sum_of_all_five_declared_inputs(monkeypatch):
    team_ids = [1, 2, 3]
    z_by_field = {
        "team_strength": [1.0, -2.0, 0.5],
        "win_pct": [0.2, 0.3, -0.4],
        "top2_ovr": [-1.0, 1.0, 0.0],
        "future_strength": [2.0, -1.0, -1.0],
        "mean_age": [0.5, 0.5, -1.0],
    }
    # SCORE_WEIGHTS' own iteration order == the order window_score requests
    # each field's z-scores in -- cycling through it lets one fake serve
    # every window_score() call (one call per team below) without needing
    # to reset state between calls.
    calls = cycle(z_by_field)

    def fake_z_scores(values):
        return z_by_field[next(calls)]

    teams = tuple(build_team_snapshot(make_team(tid), (), (), CAP_SETTINGS) for tid in team_ids)
    league = build_league_snapshot(1, _SEASON, teams)
    monkeypatch.setattr(window, "_z_scores", fake_z_scores)

    for idx, tid in enumerate(team_ids):
        expected = sum(SCORE_WEIGHTS[field] * z_by_field[field][idx] for field in SCORE_WEIGHTS)
        actual = window_score(league.team_by_id(tid), league)
        assert actual == pytest.approx(expected), (tid, expected, actual)


def test_score_weights_declare_exactly_the_five_raw_input_fields():
    assert set(SCORE_WEIGHTS) == {
        "team_strength", "win_pct", "top2_ovr", "future_strength", "mean_age",
    }
    assert SCORE_WEIGHTS["team_strength"] == pytest.approx(0.38)
    assert SCORE_WEIGHTS["win_pct"] == pytest.approx(0.22)
    assert SCORE_WEIGHTS["top2_ovr"] == pytest.approx(0.13)
    assert SCORE_WEIGHTS["future_strength"] == pytest.approx(0.17)
    assert SCORE_WEIGHTS["mean_age"] == pytest.approx(-0.10)


# ---------------------------------------------------------------------------
# score_terms -- Decision P2-18's reconstruction identity, over every team
# in a synthetic 30-team league (reusing the same _FILLER_TEAMS/_fixed_roster
# fixtures the circular-dependency regression test below builds).
# ---------------------------------------------------------------------------


def test_score_terms_declares_exactly_the_five_score_weight_fields_in_order():
    team = build_team_snapshot(make_team(1), (), (), CAP_SETTINGS)
    league = build_league_snapshot(1, _SEASON, (team,))
    terms = score_terms(team, league)
    assert [t.label for t in terms] == list(SCORE_WEIGHTS)
    for term in terms:
        assert isinstance(term, ReasonTerm)
        assert term.weight == pytest.approx(SCORE_WEIGHTS[term.label])


def test_score_terms_reconstructs_window_score_over_every_team_in_a_30_team_league():
    """§9.1/Decision P2-18: `sum(t.contribution for t in terms)` must equal
    `window_score(...)` to within `1e-9`, for every one of the 30 teams
    in a synthetic league -- reuses `_FILLER_TEAMS`/`_fixed_roster`, the
    same 30-team fixture `test_classification_is_identical_across_all_
    30_teams_and_7_philosophies` builds below."""
    season = _SEASON
    fixed_roster = _fixed_roster(season)

    checked = 0
    for variable_team_id in range(1, 31):
        team = make_team(variable_team_id)
        variable_snapshot = build_team_snapshot(team, fixed_roster, (), CAP_SETTINGS)
        league = build_league_snapshot(1, season, (variable_snapshot, *_FILLER_TEAMS))

        for team_snapshot in league.teams:
            terms = score_terms(team_snapshot, league)
            expected = window_score(team_snapshot, league)
            total = sum(t.contribution for t in terms)
            assert abs(total - expected) < 1e-9, (team_snapshot.id, total, expected)
            checked += 1

    assert checked == 30 * 30


# ---------------------------------------------------------------------------
# Rank bands populated + the two absolute guards, over a synthetic league
# ---------------------------------------------------------------------------


def _build_league_with_scores(scores_by_team_id, *, star_team_ids=frozenset()):
    """A 1-30 team league whose `window_score` (once `_z_scores` is
    monkeypatched to ignore its `values` argument) is driven entirely by
    `scores_by_team_id`, in insertion order -- `star_team_ids` optionally
    gives a named team a single 85-overall rostered player, for guard-2
    cases."""
    team_ids = list(scores_by_team_id)
    teams = []
    for tid in team_ids:
        if tid in star_team_ids:
            roster = (
                make_roster_player(
                    90_000 + tid, age=27, season=_SEASON, overall_cached=85,
                    peak_age_start=30, peak_age_end=34,
                ),
            )
        else:
            roster = ()
        teams.append(build_team_snapshot(make_team(tid), roster, (), CAP_SETTINGS))
    league = build_league_snapshot(1, _SEASON, tuple(teams))
    z_list = [scores_by_team_id[tid] for tid in team_ids]
    return league, z_list


def test_every_band_is_populated_over_a_synthetic_30_team_league(monkeypatch):
    n = 30
    zs = {i + 1: -3.0 + i * (6.0 / (n - 1)) for i in range(n)}
    top6_ids = frozenset(
        tid for tid, _ in sorted(zs.items(), key=lambda kv: kv[1], reverse=True)[:6]
    )
    league, z_list = _build_league_with_scores(zs, star_team_ids=top6_ids)
    monkeypatch.setattr(window, "_z_scores", lambda values: z_list)

    bands = {classify_window(t, league) for t in league.teams}
    assert bands == set(CompetitiveWindow), f"missing band(s): {set(CompetitiveWindow) - bands}"


def test_absolute_score_guard_caps_a_would_be_win_now_team_at_retool(monkeypatch):
    team_ids = list(range(1, 31))
    zs = {tid: 5.0 for tid in team_ids[:6]}  # ranks 1-6, comfortably CONTEND
    target_id = team_ids[6]
    zs[target_id] = -1.5  # rank 7 by ordering, but score = 0.80 * -1.5 = -1.2 < -0.90
    for tid in team_ids[7:]:
        zs[tid] = -3.0  # ranks 8-30, well below the target
    league, z_list = _build_league_with_scores(zs)
    monkeypatch.setattr(window, "_z_scores", lambda values: z_list)

    target_snapshot = league.team_by_id(target_id)
    assert window_score(target_snapshot, league) == pytest.approx(-1.2)
    assert classify_window(target_snapshot, league) == CompetitiveWindow.RETOOL


def test_absolute_no_star_guard_caps_a_would_be_contend_team_at_win_now(monkeypatch):
    team_ids = list(range(1, 31))
    zs = {team_ids[0]: 5.0}
    for tid in team_ids[1:]:
        zs[tid] = -1.0
    league, z_list = _build_league_with_scores(zs)  # nobody gets a star -> star_team_ids empty
    monkeypatch.setattr(window, "_z_scores", lambda values: z_list)

    target_snapshot = league.team_by_id(team_ids[0])
    assert classify_window(target_snapshot, league) == CompetitiveWindow.WIN_NOW


# ---------------------------------------------------------------------------
# Decision P2-14 -- the hysteresis cap
# ---------------------------------------------------------------------------


def test_apply_hysteresis_caps_movement_at_one_band_in_either_direction():
    respecter = GmPhilosophy.TENDENCY_RESPECTER
    assert apply_hysteresis(
        CompetitiveWindow.REBUILD, CompetitiveWindow.CONTEND, respecter
    ) == CompetitiveWindow.RETOOL
    assert apply_hysteresis(
        CompetitiveWindow.CONTEND, CompetitiveWindow.REBUILD, respecter
    ) == CompetitiveWindow.WIN_NOW
    assert apply_hysteresis(
        CompetitiveWindow.RETOOL, CompetitiveWindow.WIN_NOW, respecter
    ) == CompetitiveWindow.WIN_NOW
    assert apply_hysteresis(
        CompetitiveWindow.WIN_NOW, CompetitiveWindow.WIN_NOW, respecter
    ) == CompetitiveWindow.WIN_NOW


def test_chaos_is_exempt_from_the_hysteresis_cap():
    assert apply_hysteresis(
        CompetitiveWindow.REBUILD, CompetitiveWindow.CONTEND, GmPhilosophy.CHAOS
    ) == CompetitiveWindow.CONTEND
    assert apply_hysteresis(
        CompetitiveWindow.CONTEND, CompetitiveWindow.REBUILD, GmPhilosophy.CHAOS
    ) == CompetitiveWindow.REBUILD


# ---------------------------------------------------------------------------
# Decision P2-13 -- the map onto the persisted Timeline column
# ---------------------------------------------------------------------------


def test_to_timeline_matches_decision_p2_13s_map():
    assert to_timeline(CompetitiveWindow.REBUILD) is Timeline.REBUILD
    assert to_timeline(CompetitiveWindow.RETOOL) is Timeline.RETOOL
    assert to_timeline(CompetitiveWindow.WIN_NOW) is Timeline.WIN_NOW
    assert to_timeline(CompetitiveWindow.CONTEND) is Timeline.WIN_NOW


# ---------------------------------------------------------------------------
# The circular-dependency regression test -- §12 step 4's own words
# ---------------------------------------------------------------------------


def _filler_team(team_id: int, season: int):
    roster = []
    for j in range(8):
        pid = team_id * 100 + j
        ovr = 55 + ((team_id + j) % 30)
        age = 22 + ((team_id + j) % 14)
        salary = round(0.05 * CAP_SETTINGS.salary_cap) + j * 100_000
        contract = make_contract(pid, start_season=season, salaries=(salary,) * 4)
        roster.append(
            make_roster_player(
                pid, age=age, season=season, overall_cached=ovr, contract=contract,
                peak_age_start=26 + (j % 4), peak_age_end=30 + (j % 4),
            )
        )
    return build_team_snapshot(make_team(team_id), tuple(roster), (), CAP_SETTINGS)


# A fixed, never-varying 29-team background -- the same 29 teams appear in
# every one of the 210 evaluations below. Only the 30th (variable) slot's
# team identity and philosophy change.
_FILLER_TEAMS = tuple(_filler_team(tid, _SEASON) for tid in range(201, 230))
assert len(_FILLER_TEAMS) == 29


def _fixed_roster(season: int):
    growth_player = make_roster_player(
        9001, age=24, season=season, overall_cached=88, peak_age_start=29, peak_age_end=33,
        contract=make_contract(9001, start_season=season, salaries=(28_000_000,) * 4),
        ratings_overrides={"potential": 93, "potential_variance": 85},
    )
    past_peak_short_deal = make_roster_player(
        9002, age=34, season=season, overall_cached=80, peak_age_start=27, peak_age_end=31,
        # expires at `season` -- exercises future_strength's padding branch.
        contract=make_contract(9002, start_season=season, salaries=(9_000_000,)),
        ratings_overrides={"potential": 74, "potential_variance": 70},
    )
    negative_residual = make_roster_player(
        9003, age=22, season=season, overall_cached=75, peak_age_start=26, peak_age_end=30,
        contract=make_contract(9003, start_season=season, salaries=(3_500_000,) * 4),
        ratings_overrides={"potential": 68, "potential_variance": 60},
    )
    fillers = tuple(
        make_roster_player(
            9010 + j, age=25 + j, season=season, overall_cached=60 + j,
            contract=make_contract(
                9010 + j, start_season=season, salaries=(2_000_000 + j * 50_000,) * 4,
            ),
            peak_age_start=27, peak_age_end=31,
        )
        for j in range(7)
    )
    return (growth_player, past_peak_short_deal, negative_residual, *fillers)


@pytest.mark.slow
def test_classification_is_identical_across_all_30_teams_and_7_philosophies():
    """§12 step 4's own regression test, verbatim: "classifying the same
    roster from any of the 30 teams' snapshots, under all seven
    philosophies, returns an identical window ... which fails if a
    philosophy constant, a window_upside, or a scouted value ever
    re-enters future_strength." The 29-team background (`_FILLER_TEAMS`)
    never changes across the 210 evaluations; only the 30th slot's
    `team_id` (1-30) and `gm_philosophy` (all seven) vary, wrapping the
    exact same 10 `RosterPlayer` objects every time."""
    season = _SEASON
    fixed_roster = _fixed_roster(season)

    bands: set[CompetitiveWindow] = set()
    scores: set[float] = set()
    evaluations = 0
    for variable_team_id in range(1, 31):
        for philosophy in GmPhilosophy:
            team = dataclasses.replace(make_team(variable_team_id), gm_philosophy=philosophy)
            variable_snapshot = build_team_snapshot(team, fixed_roster, (), CAP_SETTINGS)
            league = build_league_snapshot(1, season, (variable_snapshot, *_FILLER_TEAMS))
            bands.add(classify_window(variable_snapshot, league))
            scores.add(window_score(variable_snapshot, league))
            evaluations += 1

    assert evaluations == 210
    # Critic round 9, B1: asserting only on the 4-way band gives this test no
    # teeth at realistic leak magnitudes -- a team_id-seeded scouting leak up
    # to ~4.5 overall points still collapses to one band (the fixed roster
    # sits mid-band, margin 0.0694) and passes silently, even though a real
    # scouting draw (scout_sd in [1.5, 7.5]) is comfortably inside that range.
    # The real invariant is bit-identical SCORES, not identical bands.
    assert len(scores) == 1, (
        f"expected one identical window_score across all 210 (team_id, "
        f"philosophy) combinations, got {len(scores)} distinct values "
        f"{sorted(scores)} -- a philosophy constant, window_upside, "
        f"confidence, risk, or a scouted value has re-entered future_strength"
    )
    assert len(bands) == 1, (
        f"expected one identical window across all 210 combinations, got "
        f"{bands} -- band-level check kept as a secondary, human-readable "
        f"signal; the score check above is the real assertion"
    )


# ---------------------------------------------------------------------------
# Purity: no cap-sheet quantity, no circular-dependency input, anywhere in
# window.py's actual code (its docstrings talk about both by name -- this
# walks the AST, not the raw text, so those mentions can't false-positive).
# ---------------------------------------------------------------------------


def test_window_module_never_references_cap_sheet_or_circular_dependency_inputs():
    tree = ast.parse(inspect.getsource(window))
    forbidden = {
        # cap-sheet-shaped (§7: cap_flex was removed specifically to avoid
        # importing §8's projected room before capsheet.py exists).
        "cap_settings", "LeagueCapSettings", "salary_cap", "CAP_GROWTH",
        # the actual circular-dependency inputs §7 spent rounds 2-5
        # closing: a philosophy constant, window_upside, confidence, risk,
        # or a scouted value re-entering future_strength.
        "scout_player", "scout_roster_player", "ScoutedPeak", "rng_for",
        "PROFILES", "PhilosophyProfile", "confidence_f", "window_upside",
        "upside_weight", "upside_weight_eff", "risk", "effective_talent_ovr",
    }
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute, ast.arg)):
            name = node.id if isinstance(node, ast.Name) else (
                node.attr if isinstance(node, ast.Attribute) else node.arg
            )
            if name in forbidden:
                referenced.add(name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name in forbidden:
                    referenced.add(alias.name)
    assert not referenced, (
        f"managers/window.py's actual code (not its docstrings) references "
        f"{referenced} -- a closed circular-dependency input or a cap-sheet-"
        f"shaped quantity has crept back in"
    )
