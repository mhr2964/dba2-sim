"""`presentation/broadcast.py`'s renderers -- Stage D commit 3
(docs/design/phase-5-stage-d-broadcast.md §4.5, §4.6, P5-97.d/.g/.h, §10
Tier 1) as amended by /watch v4 (docs/design/watch-v4-live-ticker.md):
one possession as a line, a frame as the live component at every selection,
the interrupted panel, the goldens, the adversarial-limit corpus, and the
single-call-site pin on `render_pbp_line`. I9 (every frame of every corpus
game renders to valid `Panel`s) lives in `test_broadcast_plan.py` beside I1-I8
and the seeded corpus it needs. The events grammar and the glyph table have
their own file, `test_broadcast_watch_v2.py`.

Line grammar tests use hand-built beat sequences (the fixtures of
`test_broadcast.py`) so a failure names the shape, not a seed. Those carry no
terminal payload, so they exercise the LEAF grammar, the fallback.

Regenerate the three goldens after an INTENDED change to the rendering, and
read the diff before committing:

    python -m tests.unit.presentation.test_broadcast_render

They are also coupled to the engine through seed 12 (an overtime game): a
tuning change that moves that game moves the goldens, which is the same
coupling `game_box_score_seed777.json` already has. (Was seed 6 until the
assist-compression fix's `SetupTuning.ball_handler_passing_ability_
anchor_*` weight shifted which seeds reach overtime for this fixture
context, then 48, 36, 5; PD-10 (docs/design/gm-command-surface.md) and the
concurrently-landed RC-20/per-36-spread-compression rebound fix both moved
every seed's late-game margin again -- see
`test_pre_change_golden_seeds_0_49.py`'s own note for the same finding.)"""

from __future__ import annotations

import ast
import dataclasses
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

import dba2
from dba2.presentation import broadcast
from dba2.presentation.boxscore import _period_line
from dba2.presentation.broadcast import (
    _LEAF_PHRASES,
    _LEAVES_SHOWING_DELTA,
    BroadcastFrame,
    FrameKind,
    PlanSettings,
    _Ending,
    last_line_index,
    plan_broadcast,
    render_interrupted,
    render_live_component,
    render_pbp_line,
    surnames,
    visible_periods,
)
from dba2.presentation.panel import MAX_TOTAL_CHARS, PanelColor
from dba2.sim.events import GameResult, PlayerBoxLine
from dba2.sim.game import simulate_game
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_game_context
from tests.unit.presentation._broadcast_helpers import (
    broadcast_variant,
    make_team,
    names_for,
    narration_for,
)
from tests.unit.presentation._golden_helpers import GOLDEN_DIR, load_golden, panel_to_dict
from tests.unit.presentation.test_broadcast import (
    _AWAY_IDS,
    _HOME_IDS,
    _MADE,
    _SETUP,
    _log,
    _segment,
)

# A rendered line: `clock  glyph [**CODE a-b**  ]sentence[  ★]`.
_LINE_PREFIX = re.compile(r"^\d+:\d\d  \S+ (?:\*\*[^*]+\*\*  )?")

# This file writes scratch files into `dba2/` or scans it (`tests/conftest.py`): one xdist
# group keeps every such writer and reader on one worker, run serially.
pytestmark = pytest.mark.xdist_group("source-tree")

_SETTINGS = PlanSettings()
# The grammar tests assert the whole sentence, so they render wide enough that
# nothing shrinks; the shrinking itself has its own tests below.
_WIDE = PlanSettings(max_pbp_line_chars=200)
_NAMES = {
    11: "Okafor",
    12: "Lindqvist",
    13: "Marchetti",
    21: "Tanaka",
    22: "Bellweather",
    23: "Ferreira",
}


def _sentence_of(line: str) -> str:
    """The sentence of a rendered line: what is left after the clock, the glyph
    and the bold running score, and before a key-play star."""
    return _LINE_PREFIX.sub("", line, count=1).removesuffix("  ★")


def _render_first(
    spec, points: int, *, rebounder: int = _HOME_IDS[1], names=_NAMES, settings=_WIDE
) -> str:
    """The line for `spec` played as the game's first possession (home on
    offense, ending with 700.0 on the period clock); `_MADE` follows it."""
    log = _log((spec, points, 720.0, 1), (_MADE, 2, 700.0, 1))
    log = [
        dataclasses.replace(b, actor_ids={"rebounder": rebounder}) if b.beat == "rebound" else b
        for b in log
    ]
    return render_pbp_line(
        _segment(log)[0], names=names, home_code="HOM", away_code="AWY", settings=settings
    )


def _res(leaf: str, clock: float = 15.0):
    return ("resolution", leaf, clock)


# --- the line grammar, one shape at a time ---------------------------------


def test_a_made_shot_reads_scorer_and_the_possession_delta():
    """A leaf-grammar line: the end clock, the glyph, the score from the scoring
    team's side in bold, then the sentence."""
    assert _render_first(_MADE, 2) == "11:40  🏀 **HOM 2-0**  Okafor scores +2"


def test_an_away_possession_pairs_its_end_clock_with_its_own_after_score():
    """The scoring team's code and score come first (`AWY 3-2`), and the
    period's last possession closes at 0:00."""
    log = _log((_MADE, 2, 720.0, 1), (_MADE, 3, 700.0, 1))
    line = render_pbp_line(
        _segment(log)[1], names=_NAMES, home_code="HOM", away_code="AWY", settings=_SETTINGS
    )
    assert line == "0:00  🏀 **AWY 3-2**  Tanaka scores +3"


def test_a_line_that_did_not_score_carries_no_score_and_a_key_play_carries_a_star():
    plain = _render_first([_SETUP, _res("missed_unblocked")], 0)
    assert plain == "11:40  ❌ Okafor misses"
    starred = render_pbp_line(
        _segment(_log((_MADE, 2, 720.0, 1), (_MADE, 2, 700.0, 1)))[0],
        names=_NAMES,
        home_code="HOM",
        away_code="AWY",
        settings=_WIDE,
        key_play=True,
    )
    assert starred == "11:40  🏀 **HOM 2-0**  Okafor scores +2  ★"


@pytest.mark.parametrize(
    ("spec", "points", "rebounder", "expected"),
    [
        pytest.param(
            [_SETUP, _res("missed_unblocked"), ("rebound", "defense", 14.0)],
            0,
            22,
            "Okafor misses, Bellweather rebound",
            id="miss-defensive-board",
        ),
        pytest.param(
            [_SETUP, _res("blocked_miss"), ("rebound", "defense", 14.0)],
            0,
            22,
            "Okafor blocked by Tanaka, Bellweather rebound",
            id="blocked",
        ),
        pytest.param(
            [_SETUP, _res("and_one")],
            3,
            12,
            "Okafor scores +3, fouled by Tanaka",
            id="and-one",
        ),
        pytest.param(
            [_SETUP, _res("shooting_foul")],
            2,
            12,
            "Okafor fouled by Tanaka, scores +2",
            id="shooting-foul-two-makes",
        ),
        pytest.param(
            [_SETUP, _res("shooting_foul"), ("rebound", "defense", 14.0)],
            1,
            22,
            "Okafor fouled by Tanaka, scores +1, Bellweather rebound",
            id="shooting-foul-one-of-two",
        ),
        pytest.param(
            [_SETUP, _res("shooting_foul"), ("rebound", "defense", 14.0)],
            0,
            22,
            "Okafor fouled by Tanaka, no points, Bellweather rebound",
            id="shooting-foul-none",
        ),
        pytest.param(
            [_SETUP, _res("non_shooting_foul"), ("rebound", "defense", 14.0)],
            1,
            22,
            "Okafor fouled by Tanaka, scores +1, Bellweather rebound",
            id="bonus-foul-one-of-two",
        ),
        pytest.param(
            [_SETUP, _res("turnover_live")],
            0,
            12,
            "Okafor turnover, stolen by Tanaka",
            id="live-ball-turnover",
        ),
        pytest.param([_SETUP, _res("turnover_dead")], 0, 12, "Okafor turnover", id="dead-ball"),
        pytest.param(
            [_SETUP, _res("offensive_foul")], 0, 12, "Offensive foul, Okafor", id="offensive-foul"
        ),
        pytest.param(
            [_SETUP, ("on_ball", "contained", 6.0)],
            0,
            12,
            "Shot clock violation, HOM",
            id="pre-resolution-violation",
        ),
    ],
)
def test_the_line_grammar_for_each_resolution_leaf(spec, points, rebounder, expected):
    assert _sentence_of(_render_first(spec, points, rebounder=rebounder)) == expected


# --- the off-path shapes (design P5-97.d's honest rule, lead ruling 3) -------


@pytest.mark.parametrize(
    ("spec", "points", "ending", "expected"),
    [
        pytest.param(  # possession.py:493
            [_SETUP, _res("shooting_foul"), ("rebound", "offense", 14.0)],
            1,
            _Ending.OFFENSIVE_REBOUND_CLOSE,
            "Okafor fouled by Tanaka, scores +1, Lindqvist offensive rebound",
            id="offensive-rebound-close-after-free-throws",
        ),
        pytest.param(
            [_SETUP, _res("missed_unblocked"), ("rebound", "offense", 14.0)],
            0,
            _Ending.OFFENSIVE_REBOUND_CLOSE,
            "Okafor misses, Lindqvist offensive rebound",
            id="offensive-rebound-close-after-a-miss",
        ),
        pytest.param(  # possession.py:558
            [_SETUP, _res("non_shooting_foul")],
            0,
            _Ending.NON_BONUS_FOUL_CLOSE,
            "Okafor fouled by Tanaka, no free throws",
            id="non-bonus-foul-close",
        ),
        pytest.param(  # restart, then a shot-clock tick
            [
                _SETUP,
                _res("and_one"),
                ("rebound", "offense", 14.0),
                ("setup", "isolation", 14.0),
                ("on_ball", "contained", 5.0),
            ],
            2,
            _Ending.RESTART_VIOLATION,
            "Okafor scores +2, fouled by Tanaka, Lindqvist offensive rebound, "
            "then shot clock violation",
            id="restart-violation-after-an-and-one",
        ),
        pytest.param(
            [
                _SETUP,
                _res("missed_unblocked"),
                ("rebound", "offense", 14.0),
                ("setup", "isolation", 14.0),
                ("on_ball", "contained", 5.0),
            ],
            0,
            _Ending.RESTART_VIOLATION,
            "Okafor misses, Lindqvist offensive rebound, then shot clock violation",
            id="restart-violation-after-a-miss",
        ),
        pytest.param(
            [
                _SETUP,
                _res("non_shooting_foul"),
                ("setup", "isolation", 14.0),
                ("on_ball", "contained", 5.0),
            ],
            0,
            _Ending.RESTART_VIOLATION,
            "Okafor fouled by Tanaka, no free throws, then shot clock violation",
            id="restart-violation-after-a-non-bonus-foul",
        ),
    ],
)
def test_a_possession_that_did_not_end_on_its_last_leaf_never_names_a_phantom_ending(
    spec, points, ending, expected
):
    log = _log((spec, points, 720.0, 1), (_MADE, 2, 700.0, 1))
    possession = _segment(log)[0]
    assert possession.ending is ending
    line = render_pbp_line(
        possession, names=_NAMES, home_code="HOM", away_code="AWY", settings=_WIDE
    )
    assert _sentence_of(line) == expected
    assert "turnover" not in line
    assert "+" not in line or f"+{points}" in line


def test_a_delta_earned_before_the_last_leaf_is_still_printed():
    """An and-one whose free throw missed and was rebounded by the defense
    *after a restart put-back also missed*: the last leaf is a plain miss, and
    the +2 the possession really scored trails it rather than vanishing."""
    spec = [
        _SETUP,
        _res("and_one"),
        ("rebound", "offense", 14.0),
        ("setup", "isolation", 14.0),
        _res("missed_unblocked", 9.0),
        ("rebound", "defense", 8.0),
    ]
    line = _render_first(spec, 2, rebounder=22)
    assert _sentence_of(line) == "Okafor misses, Bellweather rebound (+2)"
    assert _segment(_log((spec, 2, 720.0, 1)))[0].ending is _Ending.CLEAN


def test_the_ending_of_an_ordinary_possession_is_clean():
    for spec, points in (([_SETUP, _res("made")], 2), ([_SETUP, _res("turnover_dead")], 0)):
        assert _segment(_log((spec, points, 720.0, 1)))[0].ending is _Ending.CLEAN


# --- names and the leaf map -------------------------------------------------


def test_the_leaf_map_is_total_over_the_eleven_censused_resolution_leaves():
    """P5-97.d: the map's key set IS the census, so a twelfth leaf
    appearing in the engine cannot slip in unphrased; the leaves that carry
    their own delta are a subset of it. Ten since BDA-4
    (docs/design/block-defender-attribution.md) added `blocked_miss_help`;
    eleven since docs/design/turnover-recovery.md TR-4.15(b) added
    `loose_ball_recovered`."""
    assert set(_LEAF_PHRASES) == {
        "made",
        "missed_unblocked",
        "blocked_miss",
        "blocked_miss_help",
        "and_one",
        "shooting_foul",
        "non_shooting_foul",
        "turnover_live",
        "turnover_dead",
        "offensive_foul",
        "loose_ball_recovered",
    }
    assert _LEAVES_SHOWING_DELTA <= set(_LEAF_PHRASES)


def test_an_unknown_leaf_renders_the_shooter_and_the_leaf():
    line = _render_first([_SETUP, _res("goaltend")], 0)
    assert _sentence_of(line) == "Okafor — goaltend"
    assert "•" in line  # a leaf the glyph table does not know reads with a neutral bullet


def test_an_unresolved_player_id_falls_back_to_player_n():
    assert _sentence_of(_render_first(_MADE, 2, names={})) == "Player 11 scores +2"


def test_a_backtick_in_a_name_cannot_close_the_code_fence():
    line = _render_first(_MADE, 2, names={11: "O`Brien"})
    assert "`" not in line and _sentence_of(line) == "O'Brien scores +2"


def test_surnames_maps_a_players_last_name():
    class _Row:
        def __init__(self, last_name):
            self.last_name = last_name

    assert surnames({7: _Row("Okafor"), 9: _Row("Tanaka")}) == {7: "Okafor", 9: "Tanaka"}


def test_a_long_name_shrinks_before_the_line_is_cut_so_the_delta_survives():
    long_names = {11: "Bartholomew-Featherstonehaugh", 21: "Christodoulopoulos-Papadimitriou"}
    line = _render_first(
        [_SETUP, _res("and_one")], 3, names=long_names, settings=PlanSettings(max_pbp_line_chars=60)
    )
    # both names were shrunk (each ends in an ellipsis), nothing was hard-cut; the cap
    # governs the sentence and the markup gets a fixed allowance on top
    assert len(line) <= 60 + broadcast._LINE_MARKUP_ALLOWANCE
    assert "+3" in line and "fouled by" in line and line.count("…") == 2


def test_the_longest_realistic_sentence_sheds_detail_and_keeps_its_ending():
    """An and-one, an offensive board, then a shot-clock violation is the
    longest sentence the grammar builds. At the default 96 characters, with
    three-digit scores, it must still say all three things -- by dropping the
    defender and the rebounder's names before it would ever cut the end."""
    spec = [
        _SETUP,
        _res("and_one"),
        ("rebound", "offense", 14.0),
        ("setup", "isolation", 14.0),
        ("on_ball", "contained", 5.0),
    ]
    names = {11: "Wojciechowski-Nowak", 21: "Christodoulopoulos", 12: "Papadimitriou-Zorba"}
    log = _log((spec, 3, 720.0, 1), (_MADE, 2, 700.0, 1))
    possession = dataclasses.replace(
        _segment(log)[0], score_before=(100, 148), score_after=(103, 148)
    )
    line = render_pbp_line(
        possession, names=names, home_code="HOM", away_code="AWY", settings=_SETTINGS
    )
    assert len(line) <= 96 + broadcast._LINE_MARKUP_ALLOWANCE and not line.endswith("…"), line
    assert "+3" in line and "offensive rebound" in line
    assert line.endswith("then shot clock violation")


def test_max_pbp_line_chars_has_a_reader_that_cuts_with_an_ellipsis():
    line = _render_first(_MADE, 2, settings=PlanSettings(max_pbp_line_chars=20))
    assert len(line) == 20 + broadcast._LINE_MARKUP_ALLOWANCE and line.endswith("…")


# --- the surfaces: scoreboard and period messages ---------------------------------

_HOME = make_team(1, "RIV")
_AWAY = make_team(2, "SUN")


@pytest.fixture(scope="module")
def overtime_game() -> GameResult:
    # BDA-4 fix (docs/design/block-defender-attribution.md): seed 48 no
    # longer reaches overtime under `resolution_rim_help` -- see
    # `test_pre_change_golden_seeds_0_49.py`'s own note. Seed 36 did, under
    # that calibration (37 only worked mid-calibration); RC-4/RC-16's
    # minutes-curve fix (docs/design/auto-gm-role-construction.md) moved
    # who's on the floor late again, and seed 36 no longer reached
    # overtime -- seed 5 did, under that curve. PD-10 (docs/design/
    # gm-command-surface.md) and the concurrently-landed RC-20/per-36-
    # spread-compression rebound fix both then moved every seed's late-game
    # margin again -- seed 5 no longer reaches overtime, seed 41 does,
    # under the combined engine. per-36-spread-compression Commit 4 (M1,
    # re-anchoring ranks 1-5 a second time, 2026-09-23) moved it once
    # more -- seed 41 no longer reaches overtime, seed 13 does. pick-and-pop C3
    # (TUNING_VERSION 18 -> 19)
    # moved it again -- seed 13 no longer reaches overtime, seed 68 does;
    # its C4 calibration (19 -> 20) moved it to seed 18; turnover-recovery
    # (TUNING_VERSION 20 -> 21) moved it to seed 146 (none in 0-49 reach it);
    # the weighted recoverer draw (21 -> 22) moved it back, to seed 27;
    # block-attribution-v2 (TUNING_VERSION 23 -> 25) moved it to seed 21 (scanned 0-99: 21 only);
    # the scale-13 correction (25 -> 26) moved it to seed 12 (scanned 0-99: 12 only).
    result = simulate_game(make_game_context(412, tuning=DEFAULT_TUNING), rng_seed=12)
    assert result.overtime_periods == 1
    return result


def _plan(result: GameResult, settings: PlanSettings = _SETTINGS):
    return plan_broadcast(result, game_id=412, home_code="RIV", away_code="SUN", settings=settings)


def _narration(result: GameResult, plan):
    return narration_for(plan, result, home=_HOME, away=_AWAY, names=names_for(result))


def _render(result: GameResult, plan, frame, *, selected_period: int = 0):
    """The live component as of `frame`, at `selected_period` (Full Game by
    default)."""
    return render_live_component(
        frame,
        plan,
        selected_period=selected_period,
        home=_HOME,
        away=_AWAY,
        names=names_for(result),
        result=result,
        narration=_narration(result, plan),
    )


def _by_kind(plan, kind: FrameKind, *, period: int | None = None) -> BroadcastFrame:
    return next(f for f in plan.frames if f.kind is kind and (period is None or f.period == period))


def _line_score_rows(panel) -> list[list[str]]:
    field = next((f for f in panel.fields if f.name == "Line Score"), None)
    if field is None:
        return []
    return [row.split()[1:] for row in field.value.split("\n")[1:-1]]


def _stats_field_names(panel) -> list[str]:
    return [f.name for f in panel.fields if f.name not in ("Line Score", "Team Totals", "Story")]


def test_the_tipoff_component_is_live_at_the_period_length_with_nothing_shown_yet(overtime_game):
    plan = _plan(overtime_game)
    panel = _render(overtime_game, plan, plan.frames[0])
    assert panel.title == "SUN 0 - 0 RIV"
    assert panel.description == "**LIVE** · Q1 12:00 · tip-off"
    # No ticker yet (nothing broadcast), but the two stats fields are always there
    # (T-2.c: the same shape at every frame and every selection).
    assert _stats_field_names(panel) == ["Full Game · RIV", "Full Game · SUN"]
    assert panel.color is PanelColor.NEUTRAL and panel.footer == ""


def test_a_play_frame_component_shows_the_frames_score_clock_ticker_and_stats(overtime_game):
    plan = _plan(overtime_game)
    frame = next(f for f in plan.frames if f.kind is FrameKind.PLAY)
    panel = _render(overtime_game, plan, frame)
    assert panel.title == f"SUN {frame.away_score} - {frame.home_score} RIV"
    assert panel.description.startswith(
        f"**LIVE** · Q1 {frame.clock_display} · {frame.momentum.label}\n\n**Latest**\n"
    )
    # No Line Score field: no period is over yet.
    assert [f.name for f in panel.fields] == ["Full Game · RIV", "Full Game · SUN"]
    for field in panel.fields:
        rows = field.value.split("\n")[1:-1]
        assert rows[-1].startswith("TEAM  FG ")
        assert all(re.search(r"\d+ pts +\d+ reb +\d+ ast$", row) for row in rows[:-1])
    assert panel.color is PanelColor.NEUTRAL


def test_a_period_end_component_carries_the_line_score_so_far(overtime_game):
    plan = _plan(overtime_game)
    panel = _render(overtime_game, plan, _by_kind(plan, FrameKind.PERIOD_END, period=2))
    assert [f.name for f in panel.fields] == ["Line Score", "Full Game · RIV", "Full Game · SUN"]
    assert panel.description.startswith("**LIVE** · End of Q2 · ")
    rows = _line_score_rows(panel)
    assert len(rows) == 2 and all(len(row) == 2 for row in rows)  # Q1 and Q2


def test_the_final_component_keeps_the_same_shape_as_a_live_selection_plus_totals_and_story(
    overtime_game,
):
    """T-2.c: the FINAL layout is the SAME two-stat-field shape as every other
    frame and selection, with Team Totals and Story added -- never a swap to a
    different field."""
    plan = _plan(overtime_game)
    live = _render(overtime_game, plan, next(f for f in plan.frames if f.kind is FrameKind.PLAY))
    final = _render(overtime_game, plan, plan.frames[-1])
    both_stats = ["Full Game · RIV", "Full Game · SUN"]
    assert _stats_field_names(final) == _stats_field_names(live) == both_stats
    assert [f.name for f in final.fields] == [
        "Line Score",
        "Full Game · RIV",
        "Full Game · SUN",
        "Team Totals",
        "Story",
    ]
    assert final.description.startswith("**FINAL/OT1**") and final.color is PanelColor.POSITIVE
    assert all(len(row) == 5 for row in _line_score_rows(final))  # Q1-Q4 and OT1
    top_home = max(
        (line for line in overtime_game.player_box_lines if line.team_id == _HOME.id),
        key=lambda line: line.points,
    )
    home_field = next(f for f in final.fields if f.name == "Full Game · RIV")
    assert f"{top_home.points} pts" in home_field.value
    assert final.title == f"SUN {overtime_game.away_score} - {overtime_game.home_score} RIV"
    totals = next(f for f in final.fields if f.name == "Team Totals").value.split("\n")[1:-1]
    assert len(totals) == 2 and all("FG " in row and "REB " in row for row in totals)
    story = next(f for f in final.fields if f.name == "Story").value
    assert "Lead changes: " in story and "Largest lead: RIV" in story
    assert "Longest run: " in story and "Five minutes left in OT1: " in story


def test_a_regulation_final_reads_plain_final():
    result = simulate_game(make_game_context(412, tuning=DEFAULT_TUNING), rng_seed=1)
    assert result.overtime_periods == 0
    plan = _plan(result)
    assert _render(result, plan, plan.frames[-1]).description.startswith("**FINAL**")


def test_the_line_score_rows_have_the_box_scores_exact_shape(overtime_game):
    """Design §4.6: reuse `boxscore.py`'s `_period_line` shape. Rendered from
    the same numbers through the box score's own function, the rows match."""
    from dba2.domain.stats import GamePeriodScore

    scores = [
        GamePeriodScore(game_id=1, league_id=1, team_id=team_id, period=period, points=points)
        for period, home_points, away_points in overtime_game.period_scores
        for team_id, points in ((_HOME.id, home_points), (_AWAY.id, away_points))
    ]
    plan = _plan(overtime_game)
    panel = _render(overtime_game, plan, plan.frames[-1])
    assert panel.fields[0].value == (
        f"```\n{_period_line(scores, _AWAY.id, 'SUN')}\n"
        f"{_period_line(scores, _HOME.id, 'RIV')}\n```"
    )


def test_a_periods_line_score_row_appears_only_once_the_period_is_complete_in_the_plan(
    overtime_game,
):
    """W-3, keyed on plan position: every frame's component shows exactly the
    periods whose last possession is at or before the frame's last
    possession -- never the row of a period still being played, whose
    finished total would be published at 11:42. Checked at Full Game and
    every other VISIBLE selection."""
    plan = _plan(overtime_game)
    last_of = {p.period: p.index for p in plan.possessions}
    for frame in plan.frames:
        last = last_line_index(frame, plan)
        complete = sum(1 for end in last_of.values() if last >= end)
        for selected in (0, *visible_periods(plan, last)):
            panel = _render(overtime_game, plan, frame, selected_period=selected)
            rows = _line_score_rows(panel)
            assert len(rows) == (2 if complete else 0), (frame.index, frame.kind, selected)
            assert all(len(row) == complete for row in rows), (frame.index, frame.kind, selected)


def test_a_selected_periods_stats_slice_the_tally_to_that_period_alone(overtime_game):
    """T-3: a period selection's stats are the SAME shape, but the numbers are
    that period's own tally, not the whole game's -- and the whole game's
    total is strictly at least as large."""
    plan = _plan(overtime_game)
    q1_end = _by_kind(plan, FrameKind.PERIOD_END, period=1)
    full = _render(overtime_game, plan, q1_end, selected_period=0)
    q1 = _render(overtime_game, plan, q1_end, selected_period=1)
    assert _stats_field_names(q1) == ["Q1 · RIV", "Q1 · SUN"]
    # At the end of Q1 the plays shown so far are exactly Q1's, so the Full Game
    # and Q1 tallies must agree (both slices cover the identical possessions).
    full_home = next(f for f in full.fields if f.name == "Full Game · RIV").value
    q1_home = next(f for f in q1.fields if f.name == "Q1 · RIV").value
    assert full_home == q1_home


def test_the_interrupted_note_is_not_a_third_ticker_case(overtime_game):
    """T-1: `render_interrupted` carries no ticker and no stats block at all --
    it is not something `render_live_component` grows a branch for."""
    plan = _plan(overtime_game)
    panel = render_interrupted(
        plan.frames[3],
        home=_HOME,
        away=_AWAY,
        home_score=overtime_game.home_score,
        away_score=overtime_game.away_score,
        period_scores=overtime_game.period_scores,
    )
    assert "Latest" not in panel.description and "last plays" not in panel.description
    assert [f.name for f in panel.fields] == ["Stopped at", "Line Score"]


# --- render_interrupted -----------------------------------------------------


def test_an_interrupted_panel_names_where_it_stopped_and_shows_the_result(overtime_game):
    plan = _plan(overtime_game)
    stopped = _by_kind(plan, FrameKind.PLAY, period=3)
    panel = render_interrupted(
        stopped,
        home=_HOME,
        away=_AWAY,
        home_score=overtime_game.home_score,
        away_score=overtime_game.away_score,
        period_scores=overtime_game.period_scores,
    )
    assert panel.color is PanelColor.WARNING
    assert panel.title == f"SUN {overtime_game.away_score} - {overtime_game.home_score} RIV"
    assert "Broadcast interrupted" in panel.description
    assert [f.name for f in panel.fields] == ["Stopped at", "Line Score"]
    assert panel.fields[0].value == f"Q3 {stopped.clock_display}"


def test_an_interrupted_panel_needs_neither_a_frame_nor_a_beat_log():
    """The startup sweep rebuilds it from persisted data alone (no
    `GameResult` survives a restart, P5-42), and a game that never delivered
    a frame still gets one."""
    panel = render_interrupted(None, home=_HOME, away=_AWAY, home_score=101, away_score=99)
    assert panel.title == "SUN 99 - 101 RIV" and panel.fields == ()


# --- the single call site of the P5-11 seam ----------------------------------

_DBA2_DIR = Path(dba2.__file__).resolve().parent


class _CallSites(ast.NodeVisitor):
    def __init__(self, name: str) -> None:
        self.name = name
        self.found: list[str] = []
        self._scope: list[str] = []

    def _enter(self, node) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _enter

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if called == self.name:
            self.found.append(self._scope[-1] if self._scope else "<module>")
        self.generic_visit(node)


def _call_sites(name: str, root: Path) -> list[tuple[str, str]]:
    """`(file relative to root, enclosing function)` of every call to `name`
    under `root` -- a `def` is not a call, so the definition is not counted."""
    sites: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        visitor = _CallSites(name)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        sites.extend((path.relative_to(root).as_posix(), scope) for scope in visitor.found)
    return sites


def test_render_pbp_line_has_exactly_one_call_site_and_it_is_play_lines():
    """P5-97.g: the seam a future Shape 2 replaces must not silently acquire a
    second implementation (the instrument §45.3 uses to pin `apply_trade`'s
    callers). Under `dba2/` -- tests may call it freely."""
    assert _call_sites("render_pbp_line", _DBA2_DIR) == [
        ("presentation/broadcast.py", "_play_lines")
    ]


def test_the_call_site_pin_has_teeth(tmp_path):
    """Mutation twin (f): a second caller planted in a scratch copy of the
    real package is found by the same helper."""
    scratch = tmp_path / "presentation"
    scratch.mkdir()
    real = (_DBA2_DIR / "presentation" / "broadcast.py").read_text(encoding="utf-8")
    (scratch / "broadcast.py").write_text(real, encoding="utf-8")
    assert len(_call_sites("render_pbp_line", tmp_path)) == 1, "the unmodified copy must be clean"
    (scratch / "other.py").write_text(
        "def narrate(p, s):\n    return render_pbp_line(p, names={}, home_code='', away_code='',"
        " settings=s)\n",
        encoding="utf-8",
    )
    assert len(_call_sites("render_pbp_line", tmp_path)) == 2
    (scratch / "other.py").write_text(
        "from x import y\nrender_pbp_line(1)\ny.render_pbp_line(2)\n", encoding="utf-8"
    )
    assert len(_call_sites("render_pbp_line", tmp_path)) == 3  # bare and attribute calls both


# --- the adversarial-limit corpus (design §10) --------------------------------


def _adversarial_game(
    overtimes: int = 7, per_period: int = 20
) -> tuple[GameResult, dict[int, str]]:
    """Regulation plus `overtimes` overtimes of hand-built possessions,
    cycling through every ending shape with three points a make, and
    player names of 60 characters."""
    shapes = [
        (
            [
                _SETUP,
                _res("and_one"),
                ("rebound", "offense", 14.0),
                ("setup", "isolation", 14.0),
                ("on_ball", "contained", 5.0),
            ],
            3,
        ),
        ([_SETUP, _res("shooting_foul"), ("rebound", "offense", 14.0)], 3),
        ([_SETUP, _res("non_shooting_foul")], 0),
        ([_SETUP, _res("blocked_miss"), ("rebound", "defense", 14.0)], 0),
        ([_SETUP, _res("turnover_live")], 0),
        ([_SETUP, ("on_ball", "contained", 6.0)], 0),
        (_MADE, 3),
    ]
    period_starts = [720.0] * 4 + [300.0] * overtimes
    possessions = []
    counter = 0
    for period, length in enumerate(period_starts, start=1):
        for step in range(per_period):
            spec, points = shapes[counter % len(shapes)]
            counter += 1
            # `_log` chains score and alternates offense by possession index
            possessions.append((spec, points, length - step * (length / per_period), period))
    log = _log(*possessions)
    segmented = _segment(log)
    period_scores = tuple(
        (
            period,
            sum(p.points for p in segmented if p.period == period and p.offense_is_home),
            sum(p.points for p in segmented if p.period == period and not p.offense_is_home),
        )
        for period in range(1, len(period_starts) + 1)
    )
    ids = (*_HOME_IDS, *_AWAY_IDS)
    box = tuple(
        PlayerBoxLine(
            player_id=pid, team_id=1 if pid in _HOME_IDS else 2, minutes_played=30.0, points=99
        )
        for pid in ids
    )
    result = GameResult(
        home_team_id=1,
        away_team_id=2,
        home_score=max(b.home_score_after for b in log),
        away_score=max(b.away_score_after for b in log),
        period_scores=period_scores,
        overtime_periods=overtimes,
        player_box_lines=box,
        team_box_lines=(),
        beat_log=tuple(log),
    )
    return result, {pid: f"{'W' * 59}{pid % 10}" for pid in ids}


def _panel_total(panel) -> int:
    return (
        len(panel.title)
        + len(panel.description)
        + len(panel.footer)
        + len(panel.author)
        + sum(len(f.name) + len(f.value) for f in panel.fields)
    )


@pytest.mark.parametrize(
    ("codes", "per_period", "lossless"),
    [
        pytest.param(("HOME", "AWAY"), 20, True, id="realistic-codes-three-digit-scores"),
        pytest.param(("HOMEHOME", "AWAYAWAY"), 60, False, id="absurd-codes-four-digit-scores"),
    ],
)
def test_the_adversarial_corpus_still_constructs_a_panel_at_every_frame_and_preset(
    codes, per_period, lossless
):
    """A 7-overtime game with 60-character names at the three presets: every
    live-component render of every frame, at Full Game and every VISIBLE
    period, constructs (`Panel.__post_init__` raising is the failure this
    exists to catch) and every line is within the cap plus its markup
    allowance. With realistic codes and three-digit scores every scoring line
    also keeps its +N and every violation line keeps its ending after the
    names shrank; with absurd codes and four-digit scores the ending may be
    cut, but the limits still hold."""
    result, names = _adversarial_game(per_period=per_period)
    home, away = make_team(1, codes[0]), make_team(2, codes[1])
    worst_total = 0
    # The two reading modes first (watch-narration-v2 WN-10.4: their wider line caps and
    # the newest-line mark are the new worst cases), then watch-v5 WT-1's frame sizes;
    # the lossless check below reads the LAST plan, `possessions_per_frame=1`.
    for settings in (
        broadcast.GAME_PLAN_SETTINGS,
        broadcast.FULL_PLAN_SETTINGS,
        *(PlanSettings(possessions_per_frame=n) for n in (4, 2, 1)),
    ):
        plan = plan_broadcast(
            result, game_id=1, home_code=home.code, away_code=away.code, settings=settings
        )
        assert plan.frames[-1].period == 11 and len(plan.frames) > 30
        narration = narration_for(plan, result, home=home, away=away, names=names)
        cap = settings.max_pbp_line_chars + broadcast._LINE_MARKUP_ALLOWANCE
        assert all(len(line) <= cap for line in narration.texts)
        assert visible_periods(plan, plan.total_possessions - 1)[-2:] == (10, 11)  # OT6, OT7
        for frame in plan.frames:
            last = last_line_index(frame, plan)
            for selected in (0, *visible_periods(plan, last)):
                panel = render_live_component(
                    frame,
                    plan,
                    selected_period=selected,
                    home=home,
                    away=away,
                    names=names,
                    result=result,
                    narration=narration,
                )
                total = _panel_total(panel)
                assert total <= MAX_TOTAL_CHARS
                worst_total = max(worst_total, total)
    assert worst_total > 1000  # the corpus really did fill the description
    if not lossless:
        return
    for possession in plan.possessions:
        line = render_pbp_line(
            possession, names=names, home_code=home.code, away_code=away.code, settings=_SETTINGS
        )
        if possession.points > 0:
            assert f"+{possession.points}" in line, line
        if possession.ending in (_Ending.PRE_RESOLUTION_VIOLATION, _Ending.RESTART_VIOLATION):
            assert "violation" in line, line


_BMP_HEAD = re.compile(r"^\d+:\d\d  \S+ |^━{10}  ")


def test_the_whole_description_fits_with_its_status_and_header_rows_at_every_speed():
    """WN-17 (the critic's nit 6): `PlanSettings`' inequality bounds the TICKER only, at
    2x its code points (every character astral), and leaves `game` 48 units and `full`
    36 under 4,096 -- less than the status row, the blank line and the ticker header
    need (57 at worst here, a FINAL OT page). The description still fits, for real, as
    2x over-counts: every line opens on at least 7 BMP code points (`M:SS`, two spaces,
    the space after the glyph; a separator on its 10-bar rule). This pins that sum with
    the rows counted: at each /watch speed, the per-line bound minus those 7 units, plus
    the worst status and header rows over a 7-overtime game with astral team codes and
    four-digit scores, is within 4,096 UTF-16 units."""
    result, names = _adversarial_game(per_period=60)
    home, away = make_team(1, "🏀" * 6), make_team(2, "🎯" * 6)
    for settings in (
        broadcast.FAST_PLAN_SETTINGS,
        broadcast.GAME_PLAN_SETTINGS,
        broadcast.FULL_PLAN_SETTINGS,
    ):
        plan = plan_broadcast(
            result, game_id=1, home_code=home.code, away_code=away.code, settings=settings
        )
        narration = narration_for(plan, result, home=home, away=away, names=names)
        assert all(_BMP_HEAD.match(text) for text in narration.texts)
        rows = 0
        for frame in plan.frames:
            for selected in (0, *visible_periods(plan, last_line_index(frame, plan))):
                description = render_live_component(
                    frame,
                    plan,
                    selected_period=selected,
                    home=home,
                    away=away,
                    names=names,
                    result=result,
                    narration=narration,
                ).description
                body = description.split("\n", 3)[3] if description.count("\n") >= 3 else ""
                rows = max(rows, broadcast._utf16_units(description) - broadcast._utf16_units(body))
        n = broadcast.ticker_lines(settings)
        cap = settings.max_pbp_line_chars + broadcast._LINE_MARKUP_ALLOWANCE
        mark = len(broadcast._NEWEST_MARK) if settings.reveal is broadcast.RevealUnit.LINE else 0
        ticker = n * (2 * cap - 7) + (n - 1) * len(broadcast._LINE_SEPARATOR) + mark
        assert 40 <= rows <= 60, rows
        assert ticker + rows <= broadcast.MAX_DESCRIPTION_CHARS, (settings, ticker, rows)


# --- the goldens ---------------------------------------------------------------

# The three `/watch` speeds (docs/design/watch-narration-v2.md WN-10.4). `fast_forward`
# is today's batch mode and its golden is byte-identical to the one before narration v2
# (A5); `default` is the `game` speed (TERSE, one line per reveal, newest line marked,
# period separators); `full` replaces the old `near_real_time` golden, a preset no
# `/watch` speed uses any more, with the FULL narration.
_GOLDEN_PRESETS = {
    "fast_forward": broadcast.FAST_PLAN_SETTINGS,
    "default": broadcast.GAME_PLAN_SETTINGS,
    "full": broadcast.FULL_PLAN_SETTINGS,
}


def _golden_panels(result: GameResult, settings: PlanSettings) -> list[dict]:
    """Per frame, the Full Game component, and (at every fifth frame and at
    every period end and the final, the same sampling the old per-period
    goldens used) the CURRENT period's own component -- `None` before any
    period is visible or once the current period is Full Game already."""
    plan = _plan(result, settings)
    panels: list[dict] = []
    for frame in plan.frames:
        last = last_line_index(frame, plan)
        visible = visible_periods(plan, last)
        show_period = visible and (
            frame.index % 5 == 0 or frame.kind in (FrameKind.PERIOD_END, FrameKind.FINAL)
        )
        panels.append(
            {
                "full_game": panel_to_dict(_render(result, plan, frame)),
                "period": (
                    panel_to_dict(_render(result, plan, frame, selected_period=visible[-1]))
                    if show_period
                    else None
                ),
            }
        )
    return panels


@pytest.mark.parametrize("preset", sorted(_GOLDEN_PRESETS))
def test_a_full_plan_matches_its_committed_golden_at_each_preset(preset, overtime_game):
    """Design §10: one full plan per preset, seed 6 (an overtime game, so an
    `OT1` line score and a `FINAL/OT1` status are in every one). The pacer's
    real `FAST_FORWARD`/`NEAR_REAL_TIME` presets are bot-layer settings; these
    are their `possessions_per_frame` values."""
    assert _golden_panels(overtime_game, _GOLDEN_PRESETS[preset]) == load_golden(
        f"presentation_broadcast_{preset}.json"
    )


# --- WT-3: the window scrolls, it is not replaced whole ------------------------


def _scroll_scenario(module, result: GameResult) -> None:
    """The window (`ticker_window_lines`) and the increment (`possessions_per_frame`)
    are different numbers now (watch-v5 WT-3): consecutive full-window, full-increment
    PLAY frames SCROLL their tail rather than replacing it whole. This only holds once
    the window is full AND both frames reveal a FULL increment (round 1 minor): the
    last PLAY batch of a period is often short (at `ppf 2` period 2's 45 possessions
    end on a 1-line batch), and the first frames of the game have not filled the window
    yet, so those are checked separately below (the `len(tail) == min(window, last+1)`
    loop), never folded into the scroll equality itself."""
    settings = module.PlanSettings()
    plan = module.plan_broadcast(
        result, game_id=412, home_code="RIV", away_code="SUN", settings=settings
    )
    narration = narration_for(
        plan, result, home=_HOME, away=_AWAY, names=names_for(result), module=module
    )
    window = module.ticker_lines(settings)
    increment = settings.possessions_per_frame
    # Asserted directly, not just inferred from the scroll equality below: a window
    # equal to the increment makes both sides of that equality empty tuples, which
    # would pass vacuously -- exactly the "matches nothing" trap this corpus's own
    # mutation-twin convention exists to catch.
    assert window > increment, "the window and the increment collapsed back to one dial"
    play_frames = [f for f in plan.frames if f.kind is module.FrameKind.PLAY]

    def tail_for(frame):
        return module._ticker_slice(
            plan,
            narration,
            cursor=module.frame_cursor(frame, plan, narration),
            is_final=False,
            selected_period=0,
        )

    found_a_full_pair = False
    for prev, cur in zip(play_frames, play_frames[1:], strict=False):
        revealed = module.last_line_index(cur, plan) - module.last_line_index(prev, plan)
        prev_tail, cur_tail = tail_for(prev), tail_for(cur)
        if revealed == increment and len(prev_tail) == window and len(cur_tail) == window:
            assert cur_tail[:-increment] == prev_tail[increment:], (prev_tail, cur_tail)
            found_a_full_pair = True
    assert found_a_full_pair, "no full-window, full-increment consecutive PLAY pair found"
    for frame in play_frames:
        last = module.last_line_index(frame, plan)
        assert len(tail_for(frame)) == min(window, last + 1)


def test_the_ticker_window_scrolls_rather_than_replaces_between_full_increments(overtime_game):
    _scroll_scenario(broadcast, overtime_game)


def test_the_window_increment_coupling_mutation_is_caught(overtime_game):
    """Mutation: `ticker_window_lines = possessions_per_frame` -> red (WT-7 item 7).
    Reverting `ticker_lines` to the old v4 form -- the larger of the floor and
    `possessions_per_frame` alone, ignoring the new field -- re-couples window and
    increment, which `_scroll_scenario`'s direct `window > increment` check catches."""
    mutated = broadcast_variant(
        (
            "return max(settings.ticker_window_lines, settings.possessions_per_frame)",
            "return settings.possessions_per_frame",
        )
    )
    with pytest.raises(AssertionError):
        _scroll_scenario(mutated, overtime_game)


def _regenerate() -> None:
    # BDA-4 fix (docs/design/block-defender-attribution.md): seed 48 no
    # longer reaches overtime under `resolution_rim_help` -- see
    # `overtime_game`'s own fixture above and `test_pre_change_golden_
    # seeds_0_49.py`'s note. Seed 36 did, under that calibration; RC-4/
    # RC-16's minutes-curve fix (docs/design/auto-gm-role-construction.md)
    # moved which seed reaches overtime again (seed 5); PD-10 (gm-command-
    # surface.md) and the concurrently-landed RC-20/per-36-spread-
    # compression rebound fix moved it once more, to seed 41; per-36-
    # spread-compression Commit 4 (M1, re-anchoring ranks 1-5 a second
    # time, 2026-09-23) moved it once more, to seed 13; pick-and-pop C3
    # (TUNING_VERSION 18 -> 19) moved it
    # again, to seed 68 and its C4 calibration (19 -> 20) to seed 18; turnover-
    # recovery (20 -> 21) to seed 146; the weighted recoverer draw (21 -> 22)
    # to seed 27, block-attribution-v2 (23 -> 25) to seed 21, the scale-13 correction
    # (25 -> 26) to seed 12 -- see `overtime_game`.
    result = simulate_game(make_game_context(412, tuning=DEFAULT_TUNING), rng_seed=12)
    for preset, settings in _GOLDEN_PRESETS.items():
        path = GOLDEN_DIR / f"presentation_broadcast_{preset}.json"
        text = json.dumps(_golden_panels(result, settings), ensure_ascii=False, indent=1)
        path.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {path.name}: {len(text)} bytes")


if __name__ == "__main__":
    _regenerate()


# --- hostile database strings: names and team codes (N1, N2, W-6) --------------

_SHAPING = frozenset({"Cc", "Cf", "Zl", "Zp"})
_LINE_START = re.compile(r"^\d+:\d\d  \S")
_SEPARATOR_LINE = re.compile(r"^━{10}  (?:End of |Halftime )")
# Written out here, not imported: the doc's list (W-6) is the oracle.
_MD_SPECIALS = frozenset("\\*_~|>`[]()#:@<")
_OWN_HEAD = re.compile(r"^\d+:\d\d  \S+ (?:\*\*(?P<score>(?:\\.|[^*\\])+)\*\*  )?")

_HOSTILE_NAMES = {
    "newline": "Quin\nQ4 0:00  SUN 99-0 RIV  Forged scores +99",
    "cr": "Quin\rtero",
    "crlf": "Quin\r\ntero\r\n",
    "line-separator": "Quin tero",
    "paragraph-separator": "Quin tero",
    "backtick": "Quin`tero",
    "fence": "```\n@everyone\n```",
    "rtl-override": "‮oretniuQ",
    "nul": "Quin\x00tero",
    "tab-and-vt": "Quin\t\x0btero",
    "zero-width": "Qu​‍in",
    "very-long": "W" * 500,
    "masked-link": "[click](https://x.example)",
    "bold": "**bold**",
    "spoiler": "||spoiler||",
    "mention": "<@123456789> @everyone",
    "heading-and-quote": "# head > quote",
    "shortcode": ":skull: ~~gone~~ _it_",
    "backslashes": "back\\slash\\",
}
_HOSTILE_CODES = (
    "R`V",
    "S\nN",
    "A‮B",
    "R*V",
    "S_N",
    "A[B]",
    "X|Y",
    # `teams.code` is unbounded TEXT: long codes must not cut through the bold score
    # (critic finding 5: 60 pipes made 2,520 violations, first an unescaped `*`).
    "|" * 60,
    "*" * 60,
    "[" * 60,
    "*[|" * 20,
    "W" * 60,
    "\n".join("`*" for _ in range(30)),
)


def _shaping_offenders(text: str) -> list[str]:
    """Characters that reshape a line, other than the newlines a fenced block
    is made of."""
    return [repr(ch) for ch in text if ch != "\n" and unicodedata.category(ch) in _SHAPING]


def _unescaped_specials(text: str) -> list[str]:
    """The Markdown-special characters in `text` that no backslash escapes."""
    found: list[str] = []
    skip = False
    for ch in text:
        if skip:
            skip = False
        elif ch == "\\":
            skip = True
        elif ch in _MD_SPECIALS:
            found.append(ch)
    return found


def _line_leaks(line: str) -> list[str]:
    """What a rendered play line lets through: an unescaped special anywhere but
    the renderer's own literals (the clock's colon, the `**` around the score, and
    the parentheses of an assist), or a forged head."""
    head = _OWN_HEAD.match(line)
    if head is None:
        return [f"malformed head: {line!r}"]
    problems: list[str] = []
    if head.group("score") is not None and _unescaped_specials(head.group("score")):
        problems.append(f"unescaped special in the score token: {line!r}")
    sentence = line[head.end() :].removesuffix("  ★")
    leaked = set(_unescaped_specials(sentence)) - {"(", ")"}
    if leaked:
        problems.append(f"unescaped {sorted(leaked)} in {line!r}")
    return problems


def _ticker_lines_of(panel, module) -> list[str]:
    """The play lines out of a live component's description (status, then a
    blank line, the header and the tail joined by `_LINE_SEPARATOR`) -- `[]`
    when nothing has been broadcast yet (the TIPOFF frame)."""
    sections = panel.description.split(module._LINE_SEPARATOR)
    if len(sections) < 2:
        return []
    _header, _, first_line = sections[1].partition("\n")
    return [first_line, *sections[2:]] if first_line else sections[2:]


def _hostile_violations(module, result, names, home, away, settings) -> list[str]:
    """Render every frame of `result` through `module` (the real
    `presentation.broadcast`, or a mutated copy of its source) at Full Game and
    the frame's most-recently-visible period, with hostile strings, and report
    what leaks: a forged or extra ticker line, a backtick that would close a
    fence, a shaping character, an unescaped Markdown special, a line over the
    cap."""
    plan = module.plan_broadcast(
        result, game_id=1, home_code=home.code, away_code=away.code, settings=settings
    )
    narration = module.plan_narration(result, plan, home=home, away=away, names=names)
    found: list[str] = []
    cap = settings.max_pbp_line_chars + module._LINE_MARKUP_ALLOWANCE
    for frame in plan.frames:
        last = module.last_line_index(frame, plan)
        visible = module.visible_periods(plan, last)
        for selected in dict.fromkeys((0, visible[-1] if visible else 0)):
            where = f"frame {frame.index} ({frame.kind}) selection {selected}"
            panel = module.render_live_component(
                frame,
                plan,
                selected_period=selected,
                home=home,
                away=away,
                names=names,
                result=result,
                narration=narration,
            )
            if "\n" in panel.title or _shaping_offenders(panel.title):
                found.append(f"{where}: title {panel.title!r} is not one clean line")
            status = panel.description.split(module._LINE_SEPARATOR)[0]
            if "\n" in status or _shaping_offenders(status):
                found.append(f"{where}: status {status!r} is not one clean line")
            status_clean = re.sub(r"\d+:\d\d|\*\*", "", status)
            if _unescaped_specials(status_clean):
                found.append(f"{where}: status leaks {status!r}")
            shown = _ticker_lines_of(panel, module)
            if shown and settings.reveal is module.RevealUnit.LINE:
                # The newest-line mark is the renderer's own literal, on the last line only
                # (watch-narration-v2 WN-9.2); a separator is its own literal line (WN-11).
                if frame.kind is not module.FrameKind.FINAL:
                    if not shown[-1].startswith(module._NEWEST_MARK):
                        found.append(f"{where}: the newest line is not marked: {shown[-1]!r}")
                    shown[-1] = shown[-1].removeprefix(module._NEWEST_MARK)
                separators = [line for line in shown if _SEPARATOR_LINE.match(line)]
                shown = [line for line in shown if not _SEPARATOR_LINE.match(line)]
                for line in separators:
                    if len(line) > cap or _shaping_offenders(line) or _unescaped_specials(line):
                        found.append(f"{where}: malformed separator {line!r}")
            for line in shown:
                if not _LINE_START.match(line) or len(line) > cap or _shaping_offenders(line):
                    found.append(f"{where}: forged or malformed line {line!r}")
                found.extend(f"{where}: {problem}" for problem in _line_leaks(line))
            for field in panel.fields:
                # Only a FENCED value (Line Score, a stats field, Team Totals) carries
                # the "exactly one open/close pair" convention; Story is plain text.
                fenced = field.value.startswith("```")
                if fenced and field.value.count("`") != 6:
                    found.append(f"{where}: field {field.name!r} leaks {field.value!r}")
                elif _shaping_offenders(field.value):
                    found.append(f"{where}: field {field.name!r} leaks {field.value!r}")
    return found


@pytest.fixture(scope="module")
def hostile_game() -> GameResult:
    return simulate_game(make_game_context(412, tuning=DEFAULT_TUNING), rng_seed=6)


@pytest.mark.parametrize("settings_name", ["default", "full"])
def test_hostile_names_and_codes_cannot_forge_lines_close_a_fence_or_inject_markdown(
    hostile_game, settings_name
):
    """N2 and W-6: every frame of an overtime game, once per hostile name (given
    to every player) and once per hostile team code, renders only well-formed
    lines, one per shown play, with every Markdown special escaped."""
    settings = _GOLDEN_PRESETS[settings_name]
    for label, hostile in _HOSTILE_NAMES.items():
        names = {line.player_id: hostile for line in hostile_game.player_box_lines}
        found = _hostile_violations(broadcast, hostile_game, names, _HOME, _AWAY, settings)
        assert not found, f"name {label}: {found[:3]}"
    for code in _HOSTILE_CODES:
        found = _hostile_violations(
            broadcast,
            hostile_game,
            names_for(hostile_game),
            make_team(1, code),
            make_team(2, code[::-1]),
            settings,
        )
        assert not found, f"code {code!r}: {found[:3]}"


def test_a_long_team_code_is_capped_everywhere_it_is_printed():
    """One rule, `_code_text`: the title, the fenced rows and the play lines all
    show the same capped code, so no surface can be pushed wide by one code."""
    long_home = make_team(1, "H" * 60)
    capped = "H" * broadcast._CODE_CHARS
    assert broadcast._code_text("H" * 60) == capped
    assert broadcast._code_text("AB\nCD" + "x" * 40) == "AB CDx"
    assert broadcast._code_text("LAL") == "LAL"
    board = render_interrupted(None, home=long_home, away=_AWAY, home_score=101, away_score=99)
    assert board.title == f"SUN 99 - 101 {capped}"
    row = broadcast._line_score_value([(1, 20, 18)], home=long_home, away=_AWAY, through_period=1)
    assert row is not None and f"{capped} 20" in row
    assert "H" * (broadcast._CODE_CHARS + 1) not in row


def _tight_hostile_line(module) -> str:
    """The 101st possession of 102 made twos (the last gives it an end clock): home,
    102-100, on the hostile-coded side. The head is `HH:MM  glyph **CODE 102-100**  `
    and the escaped code takes 12 characters, so the head is 36 characters -- past
    the narrowest line's 32 -- and a whole-line cut would land inside the bold pair."""
    log = _log(*[(_MADE, 2, 720.0 - k, 1) for k in range(102)])
    return module.render_pbp_line(
        _segment(log)[100],
        names=_NAMES,
        home_code="|" * 60,
        away_code="AWY",
        settings=PlanSettings(max_pbp_line_chars=20),
    )


def test_a_tight_line_cap_with_a_hostile_code_never_cuts_the_bold_score():
    """The head carries `**CODE a-b**`; only the sentence may be cut. At the
    narrowest line the settings allow (20) and a code that escapes to twice its
    width, the head alone can exceed the cap: the line keeps its balanced bold
    and drops the sentence rather than cutting through the pair."""
    line = _tight_hostile_line(broadcast)
    assert line.count("**") == 2, line
    assert re.search(r"\*\*(\\\|){6} 102-100\*\*", line), line


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            ('    return " ".join(swapped.split())\n', "    return text.replace('`', \"'\")\n"),
            id="backtick-only-as-before",
        ),
        pytest.param(
            (
                '_LINE_SHAPING_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})',
                "_LINE_SHAPING_CATEGORIES = frozenset()",
            ),
            id="no-format-or-control-categories",
        ),
        pytest.param(
            (
                '_MARKDOWN_SPECIALS = frozenset("\\\\*_~|>`[]()#:@<")',
                '_MARKDOWN_SPECIALS = frozenset("\\\\*_~|>`()#:@<")',
            ),
            id="square-brackets-left-unescaped",
        ),
        pytest.param(
            (
                '    return "".join("\\\\" + ch if ch in _MARKDOWN_SPECIALS else ch for ch in text)\n',  # noqa: E501
                "    return text\n",
            ),
            id="no-markdown-escape-at-all",
        ),
    ],
)
def test_the_hostile_string_check_has_teeth_a_weaker_sanitiser_is_caught(hostile_game, mutation):
    """Twin: the same check against a copy of the module whose `_fence_safe` or
    `_markdown_safe` was weakened must report a leak for at least one hostile
    string, while the unmutated copy is clean."""
    settings = _GOLDEN_PRESETS["default"]

    def run(module) -> list[str]:
        found: list[str] = []
        for hostile in _HOSTILE_NAMES.values():
            names = {line.player_id: hostile for line in hostile_game.player_box_lines}
            found += _hostile_violations(module, hostile_game, names, _HOME, _AWAY, settings)
        return found

    assert run(broadcast_variant()) == []
    assert run(broadcast_variant(mutation)), mutation


def test_markdown_safe_escapes_exactly_the_listed_characters_and_round_trips():
    hostile = "\\*_~|>`[]()#:@< plain 'apostrophe' hyphen-ated Dončić 12"
    escaped = broadcast._markdown_safe(hostile)
    assert _unescaped_specials(escaped) == []
    assert re.sub(r"\\([^A-Za-z0-9])", r"\1", escaped) == hostile
    untouched = "O'Brien Dončić-Ray Jr 3"
    assert broadcast._markdown_safe(untouched) == untouched


def test_the_escape_check_has_teeth_an_unescaped_backslash_is_caught():
    """Twin: with the backslash dropped from the special set, a name's own
    backslash pairs with the escape in front of the next character (a backslash then a
    bare `*`), so a special the renderer meant to neutralise is live again."""
    mutated = broadcast_variant(
        (
            '_MARKDOWN_SPECIALS = frozenset("\\\\*_~|>`[]()#:@<")',
            '_MARKDOWN_SPECIALS = frozenset("*_~|>`[]()#:@<")',
        )
    )
    hostile = "\\*x"
    assert _unescaped_specials(broadcast._markdown_safe(hostile)) == []
    assert _unescaped_specials(mutated._markdown_safe(hostile)) == ["*"]


def test_a_markdown_hostile_name_renders_as_literal_text_in_a_play_line():
    line = _render_first(_MADE, 2, names={11: "[x](http://y.z)"}, settings=_WIDE)
    assert "\\[x\\]\\(http\\://y.z\\)" in line
    assert _line_leaks(line) == []
    bold = _render_first(_MADE, 2, names={11: "**bold**"}, settings=_WIDE)
    assert "\\*\\*bold\\*\\*" in bold and _line_leaks(bold) == []


def test_a_name_that_is_only_control_characters_falls_back_to_player_n():
    line = _render_first(_MADE, 2, names={11: "\n \t​"})
    assert _sentence_of(line) == "Player 11 scores +2"
    assert _sentence_of(_render_first(_MADE, 2, names={11: "  Okafor \n  Jr  "})) == (
        "Okafor Jr scores +2"
    )


def test_the_blank_name_fallback_has_teeth():
    variant = broadcast_variant(
        (
            '    raw = _fence_safe(names.get(player_id) or "") or f"Player {player_id}"\n',
            '    raw = _fence_safe(names.get(player_id) or "")\n',
        )
    )
    possession = _segment(_log((_MADE, 2, 720.0, 1), (_MADE, 2, 700.0, 1)))[0]
    line = variant.render_pbp_line(
        possession,
        names={11: "\n"},
        home_code="HOM",
        away_code="AWY",
        settings=variant.PlanSettings(),
    )
    assert "Player 11" not in line


# N1: the Line Score and Team Totals fields carry team codes inside their own
# fences. The stats fields (`_period_stats_value`) do NOT any more -- the team
# code moved to the FIELD NAME (`_stats_scope_label(period) · <code>`), which
# Discord never renders as Markdown, so there is no fence for it to close.


def _fence_leaks(panel) -> list[str]:
    return [
        f"{field.name}: {field.value!r}"
        for field in panel.fields
        if field.value.startswith("```")
        and (field.value.count("`") != 6 or _shaping_offenders(field.value))
    ]


def _final_and_interrupted_panels(module, result, home, away):
    plan = module.plan_broadcast(result, game_id=1, home_code=home.code, away_code=away.code)
    names = names_for(result)
    narration = narration_for(plan, result, home=home, away=away, names=names, module=module)

    def render(frame, selected):
        return module.render_live_component(
            frame,
            plan,
            selected_period=selected,
            home=home,
            away=away,
            names=names,
            result=result,
            narration=narration,
        )

    final = render(plan.frames[-1], 0)
    live = render(plan.frames[len(plan.frames) // 2], 0)
    visible = module.visible_periods(plan, plan.total_possessions - 1)
    final_period = render(plan.frames[-1], visible[0]) if visible else final
    interrupted = module.render_interrupted(
        None, home=home, away=away, home_score=1, away_score=2, period_scores=[(1, 10, 12)]
    )
    return final, live, final_period, interrupted


def test_a_backtick_in_a_team_code_cannot_close_a_fence(hostile_game):
    home, away = make_team(1, "R`V"), make_team(2, "S`N")
    final, live, final_period, interrupted = _final_and_interrupted_panels(
        broadcast, hostile_game, home, away
    )
    assert _stats_field_names(final) == ["Full Game · R'V", "Full Game · S'N"]
    assert _stats_field_names(live) == ["Full Game · R'V", "Full Game · S'N"]
    assert [f.name for f in final.fields][-2:] == ["Team Totals", "Story"]
    for panel in (final, live, final_period, interrupted):
        assert _fence_leaks(panel) == []
    assert "S'N  " in final.fields[0].value and "R'V  " in final.fields[0].value


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            (
                'f"{_code_text(away.code):<4} " + " ".join(str(row[2])',
                'f"{away.code:<4} " + " ".join(str(row[2])',
            ),
            id="line-score-away-code-raw",
        ),
        pytest.param(
            (
                'f"{_code_text(home.code):<4} " + " ".join(str(row[1])',
                'f"{home.code:<4} " + " ".join(str(row[1])',
            ),
            id="line-score-home-code-raw",
        ),
        pytest.param(
            ('f"{_code_text(team.code):<4} FG ', 'f"{team.code:<4} FG '),
            id="team-totals-code-raw",
        ),
    ],
)
def test_the_fence_leak_check_has_teeth_a_raw_team_code_is_caught(hostile_game, mutation):
    """Twin (N1): un-sanitising either Line Score row or the Team Totals row
    lets a backtick through, and the same check on the same inputs notices.
    The old Leaders/Live Leaders variants have no successor: `_period_stats_
    value` never prints a team code inside its fence any more (T-2.c)."""
    home, away = make_team(1, "R`V"), make_team(2, "S`N")
    clean = _final_and_interrupted_panels(broadcast_variant(), hostile_game, home, away)
    assert all(_fence_leaks(panel) == [] for panel in clean)
    mutated = _final_and_interrupted_panels(broadcast_variant(mutation), hostile_game, home, away)
    assert any(_fence_leaks(panel) for panel in mutated), mutation


def test_the_tight_line_check_has_teeth_cutting_the_whole_line_breaks_the_bold_pair():
    """Twin: a copy that cuts the whole line, not just the sentence, leaves the
    head's `**` unbalanced at the narrowest cap."""
    assert _tight_hostile_line(broadcast_variant()).count("**") == 2
    cut_head = (
        "    room = limit - len(head)\n    if room < 1:\n        return head.rstrip() + star\n"
        "    return head + _cut(sentence, room) + star\n",
        "    return _cut(head + sentence, limit) + star\n",
    )
    assert _tight_hostile_line(broadcast_variant(cut_head)).count("**") != 2


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(("_CODE_CHARS = 6\n", "_CODE_CHARS = 10_000\n"), id="no-code-cap"),
    ],
)
def test_the_long_code_check_has_teeth_an_uncapped_or_head_cutting_line_is_caught(
    hostile_game, mutation
):
    """Twin (critic finding 5): a copy of the module without the code cap, or that
    cuts the whole line instead of the sentence, must fail the same oracle that the
    real module passes on the long hostile codes."""
    # The 96-character cap (the batch presets' own): at a reading mode's 240 a long
    # code has room to spare, and an uncapped one never reaches the line limit.
    settings = PlanSettings()
    long_codes = [code for code in _HOSTILE_CODES if len(code) > 6]
    assert long_codes

    def run(module) -> list[str]:
        found: list[str] = []
        for code in long_codes:
            found += _hostile_violations(
                module,
                hostile_game,
                names_for(hostile_game),
                make_team(1, code),
                make_team(2, code[::-1]),
                settings,
            )
        return found

    assert run(broadcast_variant()) == []
    assert run(broadcast_variant(mutation)), mutation


# --- no control-flow `assert` in the pure layer (N4) ---------------------------


def _assert_statements(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)]


def test_broadcast_py_has_no_assert_statements():
    """`python -O` strips `assert`, so a guard written as one is a guard that
    disappears in an optimised run. The module's guards are explicit raises."""
    assert _assert_statements(_DBA2_DIR / "presentation" / "broadcast.py") == []
    assert _assert_statements(_DBA2_DIR / "presentation" / "live_box.py") == []


def test_the_assert_scan_has_teeth(tmp_path):
    scratch = tmp_path / "broadcast.py"
    real = (_DBA2_DIR / "presentation" / "broadcast.py").read_text(encoding="utf-8")
    scratch.write_text(real, encoding="utf-8")
    assert _assert_statements(scratch) == []
    scratch.write_text(real + "\nassert True\n", encoding="utf-8")
    assert len(_assert_statements(scratch)) == 1


_OPTIMISED_SNIPPET = """
import dataclasses
from dba2.presentation.broadcast import FrameKind, PlanSettings, render_pbp_line
from tests.unit.presentation.test_broadcast import _MADE, _log, _segment
from tests.unit.presentation.test_broadcast_render import _plan, _render
from tests.unit.presentation.test_broadcast_plan import _simulate

assert False, "asserts are stripped"  # -O: this line must not raise
possession = _segment(_log((_MADE, 2, 720.0, 1), (_MADE, 2, 700.0, 1)))[0]
leafless = dataclasses.replace(possession, terminal_leaf=None)
try:
    render_pbp_line(leafless, names={}, home_code="H", away_code="A", settings=PlanSettings())
except ValueError as failure:
    print("leaf:", failure)
result = _simulate(6)
plan = _plan(result)
play = next(f for f in plan.frames if f.kind is FrameKind.PLAY)
try:
    _render(result, plan, dataclasses.replace(play, span=None))
except ValueError as failure:
    print("span:", failure)
"""


def test_the_pure_layers_guards_still_raise_under_python_dash_O():
    """N4: the two malformed-input guards (a leafless possession that is not a
    pre-resolution violation, a PLAY frame with no span) are raises, so an
    optimised interpreter -- which drops every `assert` -- keeps them."""
    done = subprocess.run(
        [sys.executable, "-O", "-c", _OPTIMISED_SNIPPET],
        capture_output=True,
        text=True,
        cwd=_DBA2_DIR.parent,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "leaf: possession 0: ending 'clean' but no leaf" in done.stdout
    assert "span: PLAY frame" in done.stdout and "has no span" in done.stdout


def test_a_leafless_possession_outside_a_pre_resolution_violation_raises():
    possession = _segment(_log((_MADE, 2, 720.0, 1), (_MADE, 2, 700.0, 1)))[0]
    with pytest.raises(ValueError, match="ending 'clean' but no leaf"):
        render_pbp_line(
            dataclasses.replace(possession, terminal_leaf=None),
            names=_NAMES,
            home_code="HOM",
            away_code="AWY",
            settings=_SETTINGS,
        )


def test_a_play_frame_without_a_span_raises(overtime_game):
    plan = _plan(overtime_game)
    play = _by_kind(plan, FrameKind.PLAY)
    with pytest.raises(ValueError, match="has no span"):
        _render(overtime_game, plan, dataclasses.replace(play, span=None))
