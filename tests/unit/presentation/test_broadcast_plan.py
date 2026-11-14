"""`presentation/broadcast.py`'s planner -- Stage D commit 2, and the
conservation-invariant property test design §9 orders written BEFORE any
pacing code exists (§11 step 13; §13.5's mitigation: a silently mis-planned
broadcast is a scoreboard that is wrong in a plausible way).

Real simulated games, no DB, no Discord -- the same unit-tier precedent as
`tests/unit/sim/test_beat_record_stamp.py`, and the same helpers. The seeds
are FIXED and listed below so a failure is reproducible; the corpus
deliberately includes overtime games and blowouts (checked, not assumed).

Each invariant is a module-level checker that raises `AssertionError`, so
the mutation twins can hand it a deliberately broken plan and prove it
notices -- "a rule that silently matches nothing is worse than no rule"
(P4-60 / P5-74). The three presets are the `possessions_per_frame` values of
design §6's table (10 / 6 / 2); the named `FAST_FORWARD`/(retired) `NEAR_REAL_TIME`
presets themselves are bot-layer settings that land in a later commit.

I9 (the scoreboard and every period message build a valid `Panel` for every
frame, and what they render agrees with the frame) joined this corpus with the
renderers.
I10 (the slate renderers cannot spoil a score) joined it with
`presentation/games.py`; its generated-input property test and mutation twins
live in `test_games.py`."""
from __future__ import annotations

import dataclasses
import pickle
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date

import pytest

from dba2.presentation import broadcast
from dba2.presentation.broadcast import (
    _LEAF_PHRASES,
    _LINE_MARKUP_ALLOWANCE,
    _LINE_SEPARATOR,
    _NEVER_SCORING_LEAVES,
    DEFAULT_PLAN_SETTINGS,
    FAST_PLAN_SETTINGS,
    FULL_PLAN_SETTINGS,
    GAME_PLAN_SETTINGS,
    BroadcastPlan,
    FrameKind,
    Line,
    LineKind,
    Narration,
    NarrationLevel,
    PlanSettings,
    RevealUnit,
    _Ending,
    _format_clock,
    _group_by_possession,
    _Possession,
    _utf16_units,
    completed_through,
    frame_cursor,
    header_score,
    last_line_index,
    line_read_seconds,
    plan_broadcast,
    plan_narration,
    plan_period_lines,
    render_live_component,
    render_pbp_line,
    reveal_schedule,
    segment_possessions,
    visible_periods,
)
from dba2.presentation.games import (
    SlateGame,
    build_matchup_line,
    build_result_line,
    build_slate_header,
    build_watching_line,
)
from dba2.sim.events import BeatRecord, GameResult
from dba2.sim.game import simulate_game
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_game_context
from tests.unit.presentation._broadcast_helpers import (
    broadcast_variant,
    make_team,
    names_for,
)

_GAME_ID = 412
_HOME_CODE, _AWAY_CODE = "HOM", "AWY"

# Seeds 0-49 on the default fixture teams: 36 goes to overtime (was 48
# until the BDA-4 fix below shifted it again), and some subset reaches a
# 30+ point blowout -- both are asserted below via `any(...)`, not a fixed
# seed list, so this comment names the overtime seed only where a test
# actually pins one. Seeds 100-103 use different team and player ids, so
# nothing here can quietly depend on the fixture's ids 1/2.
#
# Widened from 0-29 (assist-compression fix): none of 0-29 reached overtime
# under the `SetupTuning.ball_handler_passing_ability_anchor_*` weight --
# same finding as `test_pre_change_golden_seeds_0_49.py`'s own note, and 48
# was the replacement seed used there at the time.
#
# BDA-4 fix (docs/design/block-defender-attribution.md): the weak-side
# rim-help contest shifted which seed reaches overtime yet again (same
# mechanism as the assist-compression note above), and did so a second
# time mid-calibration (37 worked briefly, then stopped once `resolution_
# rim_help.base_rate`/`scale` reached their shipped values) -- 48 fell back
# to regulation, 36 (also inside 0-49) was the fixture's overtime game
# then, verified against the shipped calibration.
#
# RC-4/RC-16 (auto-gm-role-construction.md), then PD-10 (gm-command-
# surface.md) plus the concurrently-landed RC-20/per-36-spread-compression
# rebound fix, each moved the overtime seed again in turn (36 -> 5 -> 41,
# per `test_pre_change_golden_seeds_0_49.py`'s own note); per-36-spread-
# compression Commit 4 (M1, re-anchoring ranks 1-5 a second time,
# 2026-09-23) moved it once more, to seed 13 -- still inside 0-49 every
# time, so the net has never needed widening.
# pick-and-pop C3 (TUNING_VERSION 18 -> 19) briefly left no overtime seed in
# 0-49; its C4 calibration (19 -> 20) put one back (seed 18) -- see
# `test_pre_change_golden_seeds_0_49.py`'s note. turnover-recovery
# (TUNING_VERSION 20 -> 21) left none in 0-49 again (scanned 0-199: 146, 158),
# so 146 was appended rather than the window widened; the weighted recoverer
# draw (TUNING_VERSION 21 -> 22) put one back (seed 27; scanned 0-399: 27, 293,
# 318, 320, 359), so the append is dropped again. block-attribution-v2 (TUNING_VERSION 23 -> 25)
# moved it to seed 21 (scanned 0-99: 21 only); the scale-13 correction (TUNING_VERSION 25 -> 26)
# moved it to seed 12 (scanned 0-99: 12 only).
_DEFAULT_SEEDS = tuple(range(50))
_CUSTOM_ID_SEEDS = (100, 101, 102, 103)
_BLOWOUT_MARGIN = 30

_PRESETS = {
    "fast_forward": PlanSettings(possessions_per_frame=10),
    "default": PlanSettings(),
    "one_per_frame": PlanSettings(possessions_per_frame=1),
}
# watch-v5 WT-1's tick per preset, used ONLY to report a duration (frames x tick);
# the pure layer owns no clock. `"default"` reads `PlanSettings()`'s own field
# default (watch-v5 WT-7): `one_per_frame` must be given a DIFFERENT
# `possessions_per_frame` above, or it would be byte-identical to `"default"`.
_TICK_SECONDS = {"fast_forward": 1.5, "default": 1.5, "one_per_frame": 2.6}


@dataclass(frozen=True)
class _Game:
    seed: int
    result: GameResult
    possessions: tuple[_Possession, ...]
    beats: tuple[tuple[BeatRecord, ...], ...]  # by possession index
    plans: dict[str, BroadcastPlan]


def _simulate(seed: int) -> GameResult:
    if seed in _CUSTOM_ID_SEEDS:
        offset = 10 * (seed - 99)
        ctx = make_game_context(
            _GAME_ID,
            tuning=DEFAULT_TUNING,
            home_team_id=7 + offset,
            away_team_id=3 + offset,
            home_player_ids=tuple(range(301 + offset, 311 + offset)),
            away_player_ids=tuple(range(401 + offset, 411 + offset)),
        )
    else:
        ctx = make_game_context(_GAME_ID, tuning=DEFAULT_TUNING)
    return simulate_game(ctx, rng_seed=seed)


def _plans_for(result: GameResult) -> dict[str, BroadcastPlan]:
    return {
        name: plan_broadcast(
            result,
            game_id=_GAME_ID,
            home_code=_HOME_CODE,
            away_code=_AWAY_CODE,
            settings=settings,
        )
        for name, settings in _PRESETS.items()
    }


def _player_team(result: GameResult) -> dict[int, int]:
    return {line.player_id: line.team_id for line in result.player_box_lines}


@pytest.fixture(scope="module")
def corpus() -> list[_Game]:
    games = []
    for seed in (*_DEFAULT_SEEDS, *_CUSTOM_ID_SEEDS):
        result = _simulate(seed)
        possessions = segment_possessions(
            result.beat_log, player_team=_player_team(result), home_team_id=result.home_team_id
        )
        beats = tuple(tuple(group) for group in _group_by_possession(result.beat_log))
        assert len(beats) == len(possessions)
        games.append(_Game(seed, result, possessions, beats, _plans_for(result)))
    return games


# --- the checkers: I1-I8, plus the plan's own shape ------------------------


def _check_i1(plan: BroadcastPlan) -> None:
    """Over PLAY frames only (B5) the spans tile `range(total_possessions)`
    exactly; every other kind has no span."""
    covered: list[int] = []
    for frame in plan.frames:
        if frame.kind is FrameKind.PLAY:
            assert frame.span is not None, f"PLAY frame {frame.index} has no span"
            first, last = frame.span
            covered.extend(range(first, last + 1))
        else:
            assert frame.span is None, f"{frame.kind} frame {frame.index} has span {frame.span}"
    assert covered == list(range(plan.total_possessions)), (
        "PLAY spans do not reproduce range(total_possessions) exactly"
    )


def _check_i2(result: GameResult, plan: BroadcastPlan) -> None:
    final = plan.frames[-1]
    assert final.kind is FrameKind.FINAL
    assert (final.home_score, final.away_score) == (result.home_score, result.away_score)
    before_final = plan.frames[-2]
    assert (before_final.home_score, before_final.away_score) == (
        result.home_score,
        result.away_score,
    ), "the last non-FINAL frame's score is not the final score"


def _check_i3(result: GameResult, plan: BroadcastPlan) -> None:
    """Per period, the score gained equals `period_scores`. The TIPOFF frame
    (period 1, 0-0) is excluded from "last frame of period p" by taking only
    PLAY/PERIOD_END frames; the subtrahend for period 1 is (0, 0)."""
    scoring = [f for f in plan.frames if f.kind in (FrameKind.PLAY, FrameKind.PERIOD_END)]
    prior = (0, 0)
    for period, home_delta, away_delta in result.period_scores:
        last = [f for f in scoring if f.period == period][-1]
        now = (last.home_score, last.away_score)
        assert (now[0] - prior[0], now[1] - prior[1]) == (home_delta, away_delta), (
            f"period {period}: frames gain {(now[0] - prior[0], now[1] - prior[1])}, "
            f"period_scores says {(home_delta, away_delta)}"
        )
        prior = now


def _check_i4(possessions: tuple[_Possession, ...], plan: BroadcastPlan) -> None:
    """The derived end clock (§2.1) and Finding 4, pinned as a property."""
    for i, p in enumerate(possessions):
        following = possessions[i + 1] if i + 1 < len(possessions) else None
        same_period = following is not None and following.period == p.period
        if same_period:
            assert p.clock_start_seconds > following.clock_start_seconds, (
                f"possession {i}: start clock not strictly decreasing within period {p.period}"
            )
            assert p.clock_end_seconds == following.clock_start_seconds, (
                f"possession {i}: end clock is not the next possession's start"
            )
        else:
            if following is not None:
                assert following.period == p.period + 1
                assert following.clock_start_seconds > p.clock_start_seconds, (
                    f"possession {i}: clock did not reset upward at the period boundary"
                )
            assert p.clock_end_seconds == 0.0, (
                f"possession {i} closes period {p.period} at {p.clock_end_seconds}, not 0.0"
            )
        assert 0.0 <= p.clock_end_seconds < p.clock_start_seconds, (
            f"possession {i}: end clock {p.clock_end_seconds} not in [0, start "
            f"{p.clock_start_seconds})"
        )

    # A frame's clock_display is its last possession's end clock.
    last_played: _Possession | None = None
    for frame in plan.frames:
        if frame.kind is FrameKind.TIPOFF:
            assert frame.clock_display == _format_clock(possessions[0].clock_start_seconds)
            continue
        if frame.kind is FrameKind.PLAY:
            assert frame.span is not None
            last_played = possessions[frame.span[1]]
        assert last_played is not None
        assert frame.clock_display == _format_clock(last_played.clock_end_seconds), (
            f"frame {frame.index} ({frame.kind}) shows {frame.clock_display}"
        )


def _check_i5(plan: BroadcastPlan) -> None:
    for earlier, later in zip(plan.frames, plan.frames[1:], strict=False):
        assert later.home_score >= earlier.home_score and later.away_score >= earlier.away_score, (
            f"score went backwards between frames {earlier.index} and {later.index}"
        )


def _check_i6(possessions: tuple[_Possession, ...]) -> None:
    """A scoring terminal leaf scores. The converse is deliberately NOT
    asserted -- see `test_the_scoring_leaf_rule_is_not_symmetric`."""
    for p in possessions:
        if p.terminal_leaf in ("made", "and_one"):
            assert p.points > 0, f"possession {p.index}: leaf {p.terminal_leaf} scored 0"


def _check_i7(possessions: tuple[_Possession, ...]) -> None:
    for earlier, later in zip(possessions, possessions[1:], strict=False):
        assert earlier.score_after == later.score_before, (
            f"possessions {earlier.index}->{later.index}: {earlier.score_after} then "
            f"{later.score_before}"
        )


def _check_i8(
    result: GameResult,
    possessions: tuple[_Possession, ...],
    beats: tuple[tuple[BeatRecord, ...], ...],
) -> None:
    """Re-derived here from the setup actors, independently of the
    segmenter, against `index % 2 == 0` (`game.py:269`)."""
    team_of = _player_team(result)
    for p in possessions:
        opener = beats[p.index][0]
        first_offense = next(v for k, v in opener.actor_ids.items() if k.startswith("offense:"))
        assert (team_of[first_offense] == result.home_team_id) == (p.index % 2 == 0), (
            f"possession {p.index}: actors and parity disagree on the offense"
        )
        assert p.offense_is_home == (p.index % 2 == 0)


def _check_shape(result: GameResult, possessions: tuple[_Possession, ...], plan: BroadcastPlan):
    """Design §4.4, the parts I1-I5 do not already pin: frame kinds in
    order, a frame never spanning a period, batch sizes, and momentum."""
    settings = plan.settings
    frames = plan.frames
    assert [f.index for f in frames] == list(range(len(frames))), "frame indices not dense"
    assert frames[0].kind is FrameKind.TIPOFF and frames[-1].kind is FrameKind.FINAL
    tipoff = frames[0]
    assert (tipoff.period, tipoff.home_score, tipoff.away_score) == (1, 0, 0)
    assert tipoff.momentum.label == "even"

    periods = [period for period, _, _ in result.period_scores]
    assert [f.period for f in frames if f.kind is FrameKind.PERIOD_END] == periods
    assert frames[-1].period == periods[-1]

    for position, frame in enumerate(frames):
        if frame.kind is FrameKind.PERIOD_END:
            previous = frames[position - 1]
            assert previous.kind is FrameKind.PLAY and previous.period == frame.period, (
                f"PERIOD_END {frame.index} does not close a PLAY frame of its own period"
            )
            next_index = previous.span[1] + 1
            following = possessions[next_index] if next_index < len(possessions) else None
            assert following is None or following.period != frame.period, (
                f"PERIOD_END {frame.index} closes period {frame.period} early"
            )
        if frame.kind is not FrameKind.PLAY:
            continue
        first, last = frame.span
        size = last - first + 1
        assert 1 <= size <= settings.possessions_per_frame
        assert {possessions[i].period for i in range(first, last + 1)} == {frame.period}, (
            f"PLAY frame {frame.index} spans a period boundary"
        )
        assert (frame.home_score, frame.away_score) == possessions[last].score_after
        if size < settings.possessions_per_frame:
            # short only when it is the last frame of its period
            assert frames[position + 1].kind is FrameKind.PERIOD_END, (
                f"short PLAY frame {frame.index} is not the last of period {frame.period}"
            )

    _check_momentum(possessions, plan)


def _check_momentum(possessions: tuple[_Possession, ...], plan: BroadcastPlan) -> None:
    """P5-97.f, re-derived with its own arithmetic: trailing window ending at
    the frame's last possession, home minus away, run label past the
    threshold."""
    settings = plan.settings
    last_index = None
    for frame in plan.frames:
        if frame.kind is FrameKind.TIPOFF:
            continue
        if frame.kind is FrameKind.PLAY:
            last_index = frame.span[1]
        window = possessions[
            max(0, last_index + 1 - settings.momentum_window_possessions) : last_index + 1
        ]
        home = sum(p.points for p in window if p.offense_is_home)
        away = sum(p.points for p in window if not p.offense_is_home)
        momentum = frame.momentum
        assert (momentum.home_points, momentum.away_points) == (home, away)
        assert momentum.margin == home - away
        if abs(home - away) >= settings.momentum_margin_threshold:
            code = _HOME_CODE if home > away else _AWAY_CODE
            assert momentum.label == f"{code} {max(home, away)}-{min(home, away)} run"
        else:
            assert momentum.label == "even"


def _check_i9(result: GameResult, plan: BroadcastPlan, *, names=None) -> None:
    """Every frame renders to a valid live-component `Panel` at Full Game AND
    every VISIBLE period (`Panel.__post_init__` is the limit assertion,
    watch-v4 T-8 #1), and what Full Game renders agrees with the frame: every
    play line is at most `max_pbp_line_chars` plus the markup allowance, and
    the ticker's newest line carries the frame's clock and the last
    possession's score, so a line, the score bug and the clock describe one
    instant."""
    home = make_team(result.home_team_id, _HOME_CODE)
    away = make_team(result.away_team_id, _AWAY_CODE)
    names = names_for(result) if names is None else names
    lines = tuple(
        line
        for pl in plan_period_lines(plan, home=home, away=away, names=names)
        for line in pl.lines
    )
    cap = plan.settings.max_pbp_line_chars + _LINE_MARKUP_ALLOWANCE
    assert all(len(line) <= cap for line in lines), f"a play line over {cap}"
    narration = plan_narration(result, plan, home=home, away=away, names=names)
    title_of = lambda f: f"{_AWAY_CODE} {f.away_score} - {f.home_score} {_HOME_CODE}"  # noqa: E731
    for frame in plan.frames:
        last = last_line_index(frame, plan)
        selectable = (0, *visible_periods(plan, last))
        panels = {}
        try:
            for selected in selectable:
                panels[selected] = render_live_component(
                    frame,
                    plan,
                    selected_period=selected,
                    home=home,
                    away=away,
                    names=names,
                    result=result,
                    narration=narration,
                )
        except ValueError as failure:
            raise AssertionError(
                f"frame {frame.index} ({frame.kind}) selection {selected} has no valid Panel: "
                f"{failure}"
            ) from failure
        # A PLAY frame's title is read at its cursor (watch-narration-v2 WN-9.3); at the
        # frame's own cursor that is the frame's score, so a frame whose stored score
        # disagrees with its lines is caught here first.
        assert all(panel.title == title_of(frame) for panel in panels.values()), (
            f"frame {frame.index}: the title is not at the frame's score"
        )
        board = panels[0]
        if frame.kind is FrameKind.TIPOFF:
            assert board.description.endswith("tip-off")
            continue
        assert "Latest" in board.description or "last plays" in board.description
        # The newest ticker line: split off everything before the LAST `_LINE_SEPARATOR`
        # (the gap between play lines), then off the header's own single "\n" -- needed
        # as its own step now that a tail can be exactly one line (watch-v5's
        # `possessions_per_frame=1`), in which case there is no `_LINE_SEPARATOR` in the
        # description at all and the naive single-split used to reach into the header.
        tail_block = board.description.strip().rsplit(_LINE_SEPARATOR, 1)[-1]
        newest = tail_block.rsplit("\n", 1)[-1]
        possession = plan.possessions[last]
        assert (frame.home_score, frame.away_score) == possession.score_after, (
            f"frame {frame.index}: the frame's score is not at the last possession's"
        )
        if possession.points > 0:
            code = _HOME_CODE if possession.offense_is_home else _AWAY_CODE
            ours, theirs = (
                (frame.home_score, frame.away_score)
                if possession.offense_is_home
                else (frame.away_score, frame.home_score)
            )
            assert f"**{code} {ours}-{theirs}**" in newest, (
                f"frame {frame.index}: newest line {newest!r} is not at the frame's score"
            )
        if frame.kind is FrameKind.PLAY:
            assert newest.startswith(f"{frame.clock_display}  "), (
                f"frame {frame.index}: newest line {newest!r} is not at the frame's clock"
            )


def _check_i10(result: GameResult, *, matchup=build_matchup_line) -> None:
    """I10 over a corpus game: `SlateGame` has no score field, and the slate
    lines built for THIS game contain exactly the numbers their own inputs put
    there -- so neither of the game's real final scores can be in them -- while
    the one score-taking renderer does carry both (the positive control)."""
    game = SlateGame(
        game_id=_GAME_ID, home_code=_HOME_CODE, away_code=_AWAY_CODE, game_date=date(2026, 10, 22)
    )
    assert not [f.name for f in dataclasses.fields(SlateGame) if "score" in f.name]
    lines = {
        "matchup": (matchup(game), [str(_GAME_ID)]),
        "watching": (build_watching_line(game), [str(_GAME_ID)]),
        "header": (build_slate_header("2026-10-22", 13), ["2026", "10", "22", "13"]),
    }
    for name, (line, allowed) in lines.items():
        numbers = re.findall(r"[0-9]+", line)
        assert numbers == allowed, f"{name} line {line!r} carries a stray number"
    revealed = re.findall(
        r"[0-9]+",
        build_result_line(game, home_score=result.home_score, away_score=result.away_score),
    )
    assert revealed == [str(_GAME_ID), str(result.away_score), str(result.home_score)]


def _period_prefix(period: int) -> str:
    return f"Q{period}" if period <= 4 else f"OT{period - 4}"


def _check_all(game: _Game) -> None:
    result, possessions = game.result, game.possessions
    _check_i4(possessions, next(iter(game.plans.values())))  # possession-level, once
    _check_i6(possessions)
    _check_i7(possessions)
    _check_i8(result, possessions, game.beats)
    for plan in game.plans.values():
        assert plan.total_possessions == len(possessions)
        assert (plan.home_team_id, plan.away_team_id) == (result.home_team_id, result.away_team_id)
        _check_i1(plan)
        _check_i2(result, plan)
        _check_i3(result, plan)
        _check_i4(possessions, plan)
        _check_i5(plan)
        _check_i9(result, plan)
        _check_i10(result)
        _check_shape(result, possessions, plan)


# --- the corpus, and the property test --------------------------------------


def test_corpus_includes_an_overtime_game_and_a_blowout(corpus):
    """Preconditions, checked rather than assumed (design §9)."""
    assert len(corpus) >= 20
    assert any(g.result.overtime_periods > 0 for g in corpus), "no overtime game in the corpus"
    assert any(
        abs(g.result.home_score - g.result.away_score) >= _BLOWOUT_MARGIN for g in corpus
    ), "no blowout in the corpus"
    assert {g.seed for g in corpus if g.seed in _CUSTOM_ID_SEEDS} == set(_CUSTOM_ID_SEEDS)
    assert all(g.result.home_team_id != 1 for g in corpus if g.seed in _CUSTOM_ID_SEEDS)


@pytest.mark.slow
def test_conservation_invariants_hold_across_the_seeded_corpus_at_all_three_presets(corpus):
    """Design §9's property test: I1-I10 on every plan of >= 20 real seeded
    games, at all three presets."""
    for game in corpus:
        try:
            _check_all(game)
        except AssertionError as failure:
            raise AssertionError(f"seed {game.seed}: {failure}") from failure


# --- what the corpus must show, not just tolerate ---------------------------


def test_the_scoring_leaf_rule_is_not_symmetric(corpus):
    """I6's converse is deliberately unasserted, and design §9 orders a real
    counterexample pinned rather than assumed: a possession that scores while
    its last resolution leaf is not `made`/`and_one`. Free throws are the
    common case (`shooting_foul`); the striking one is a possession whose
    credit event scored and whose last shot then MISSED."""
    scored_on_other_leaf = Counter(
        p.terminal_leaf
        for g in corpus
        for p in g.possessions
        if p.points > 0 and p.terminal_leaf not in ("made", "and_one")
    )
    assert scored_on_other_leaf["shooting_foul"] > 0, scored_on_other_leaf
    assert scored_on_other_leaf["missed_unblocked"] > 0, (
        f"no possession in the corpus scored and then ended on a missed shot: "
        f"{scored_on_other_leaf}"
    )


def _shapes(corpus):
    """`(game, possession, its beats)` for every possession of the corpus. A
    possession keeps no beats (the plan would carry ~97% dead weight), so the
    independent tail helpers below read them from the raw log, grouped by
    the same `_group_by_possession` the segmenter uses."""
    for g in corpus:
        for p in g.possessions:
            yield g, p, g.beats[p.index]


def _is_offensive_rebound_budget_exhaustion(p: _Possession, beats) -> bool:
    """`possession.py:493`: the log ends on an offensive rebound -- a restart
    would have appended another setup beat."""
    return beats[-1].beat == "rebound" and beats[-1].outcome == "offense"


def _is_non_bonus_foul_budget_exhaustion(p: _Possession, beats) -> bool:
    """`possession.py:558`, the PROVABLE subset: the log ends on a non-shooting
    foul that scored nothing and drew no rebound. A bonus foul goes to the
    line and a missed last free throw draws a rebound beat, so a scoreless one
    with the foul as the LAST beat can only be the non-bonus, budget-spent
    close-out. The same close-out AFTER earlier points is real but not
    provable from the log -- `_is_undetermined_foul_close`, and
    `test_broadcast_ground_truth.py` holds both to the engine's own record."""
    return (
        beats[-1].beat == "resolution"
        and beats[-1].outcome == "non_shooting_foul"
        and p.points == 0
    )


def _is_undetermined_foul_close(p: _Possession, beats) -> bool:
    """The same tail on a possession that scored, where an earlier resolution
    beat could have scored it: a bonus foul that made its free throws and a
    non-bonus close-out after earlier points look identical in the log.

    Block-defender-attribution fix (this dispatch, critic-proven): this used
    to hardcode `("missed_unblocked", "blocked_miss")` as its own private
    copy of production's never-scoring-leaf set, which drifted the moment
    `broadcast.py:101`'s `_NEVER_SCORING_LEAVES` grew a third member
    (`blocked_miss_help`, BDA-4) -- a possession carrying an earlier
    `blocked_miss_help` used to silently mis-bucket here instead of being
    recognized as undetermined. Reading `_NEVER_SCORING_LEAVES` directly
    means this can't drift out of sync with production again."""
    resolutions = [b for b in beats if b.beat == "resolution"]
    return (
        beats[-1].beat == "resolution"
        and beats[-1].outcome == "non_shooting_foul"
        and p.points > 0
        and any(b.outcome not in _NEVER_SCORING_LEAVES for b in resolutions[:-1])
    )


def _is_restart_tick_violation(p: _Possession, beats) -> bool:
    """A continuation restarted (a credit event was recorded) and the shot
    clock then expired before another resolution: the last resolution leaf is
    the credit event's, but the real terminal is a shot-clock turnover. Or
    (docs/design/turnover-recovery.md TR-4.14) the scramble-violation close:
    the possession's LAST beat is a `loose_ball_recovered` resolution -- a
    recovery always continues the possession, so ending on one means the
    scramble itself ran the shot clock out."""
    if p.terminal_leaf is None:
        return False
    last = beats[-1]
    if last.beat == "resolution" and last.outcome == "loose_ball_recovered":
        return True
    return last.beat not in ("resolution", "rebound")


def test_budget_exhausted_possessions_keep_the_credit_event_and_the_real_point_delta(corpus):
    """P5-97.d's honest rule, pinned on real possessions: a budget-exhausted
    close-out has a resolution leaf (it is never the None shape, and no
    phantom turnover beat exists in the log) and its points are the stamped
    delta. Both `:493` and `:558` shapes must actually occur."""
    every = [(p, beats) for _, p, beats in _shapes(corpus)]
    shape_493 = [p for p, beats in every if _is_offensive_rebound_budget_exhaustion(p, beats)]
    shape_558 = [p for p, beats in every if _is_non_bonus_foul_budget_exhaustion(p, beats)]
    assert shape_493 and shape_558, (len(shape_493), len(shape_558))
    for p in (*shape_493, *shape_558):
        assert p.terminal_leaf is not None
        assert p.terminal_leaf != "turnover_dead"
        assert p.points == sum(p.score_after) - sum(p.score_before)
    assert all(p.rebound_is_offensive is True for p in shape_493)


def test_pre_resolution_violations_report_which_beat_ended_them(corpus):
    """Design §4.3: "the corpus test should report the observed split rather
    than assume it". The split is asserted only to be drawn from the five
    engine sites (`:642` setup, `:695` screen, `:732` on_ball, `:816` help,
    `:877` rotation) and to be non-empty."""
    split = Counter(beats[-1].beat for _, p, beats in _shapes(corpus) if p.terminal_leaf is None)
    assert sum(split.values()) > 0
    assert set(split) <= {"setup", "screen", "on_ball", "help", "rotation"}, split


def test_every_possession_renders_a_leaf_grammar_line_that_keeps_its_delta_and_its_real_ending(
    corpus,
):
    """P5-97.d's honest rule over every possession of the corpus at the
    default line settings, on the FALLBACK grammar (the events stripped, as a
    hand-built beat log has none): a non-empty line within the cap plus its
    markup allowance, containing its own `+N` when the possession scored (and
    no `+` when it did not), that says "shot clock violation" exactly when the
    possession really ended in one, and that never invents a turnover the
    resolution leaf did not record. The independent tail checks above and the
    renderer's own classifier must agree possession by possession."""
    settings = PlanSettings()
    shapes = Counter()
    for game in corpus:
        names = names_for(game.result)
        for p in game.possessions:
            beats = game.beats[p.index]
            line = render_pbp_line(
                dataclasses.replace(p, events=()),
                names=names,
                home_code=_HOME_CODE,
                away_code=_AWAY_CODE,
                settings=settings,
            )
            ending = p.ending
            shapes[ending] += 1
            where = f"seed {game.seed} possession {p.index} ({ending}): {line!r}"
            assert line and len(line) <= settings.max_pbp_line_chars + _LINE_MARKUP_ALLOWANCE, where
            if p.points > 0:
                assert re.search(rf"\+{p.points}\b", line), where
            else:
                assert "+" not in line, where
            lowered = line.lower()
            assert ("shot clock violation" in lowered) == (
                ending in (_Ending.PRE_RESOLUTION_VIOLATION, _Ending.RESTART_VIOLATION)
            ), where
            if p.terminal_leaf not in ("turnover_live", "turnover_dead", "offensive_foul"):
                assert "turnover" not in lowered, where
            independent = {
                _Ending.OFFENSIVE_REBOUND_CLOSE: _is_offensive_rebound_budget_exhaustion(p, beats),
                _Ending.NON_BONUS_FOUL_CLOSE: _is_non_bonus_foul_budget_exhaustion(p, beats),
                _Ending.UNDETERMINED_FOUL_CLOSE: _is_undetermined_foul_close(p, beats),
                _Ending.RESTART_VIOLATION: _is_restart_tick_violation(p, beats),
            }
            for shape, expected in independent.items():
                assert (ending is shape) == expected, where
            if ending is _Ending.OFFENSIVE_REBOUND_CLOSE:
                assert "offensive rebound" in lowered, where
            if ending is _Ending.NON_BONUS_FOUL_CLOSE:
                assert "no free throws" in lowered, where
            if ending is _Ending.UNDETERMINED_FOUL_CLOSE:
                assert "scores +" not in lowered and "no free throws" not in lowered, where
            if ending is _Ending.RESTART_VIOLATION:
                assert "then shot clock violation" in lowered, where
    assert set(shapes) == set(_Ending), shapes  # every shape actually occurred


def test_a_help_block_credits_the_drawn_blocker_not_the_floors_rim_protector(corpus):
    """docs/design/block-attribution-v2.md BA-2.8/BA-4.2: a weak-side block's
    blocker is drawn from every eligible helper, so he can be someone other
    than the setup beat's `defense:rim_protector` (the floor's single best,
    kept for narration). The possession's block event -- what the broadcast
    names ("blocked by ...") and the live box credits -- carries the drawn
    blocker the resolution beat recorded as `blocked_by`, and the corpus
    really contains help blocks where the two differ, so this is not passing
    on the old one-candidate shape by coincidence."""
    help_blocks = differs = 0
    for game in corpus:
        for p in game.possessions:
            beats = game.beats[p.index]
            resolutions = [b for b in beats if b.beat == "resolution"]
            if not resolutions or resolutions[-1].outcome != "blocked_miss_help":
                continue
            help_blocks += 1
            blocker = resolutions[-1].actor_ids["blocked_by"]
            where = f"seed {game.seed} possession {p.index}"
            assert blocker in {e.blocking_defender_id for e in p.events if e.blocked}, where
            setups = [b for b in beats if b.beat == "setup"]
            if setups[-1].actor_ids.get("defense:rim_protector") != blocker:
                differs += 1
    assert help_blocks > 0 and differs > 0, (help_blocks, differs)


def test_corpus_report(corpus, capsys):
    """Prints the rates design §6/§4.5 asked the tests to report (run with
    `-s` to read them). Asserts only that frame counts track the design's
    measured table, loosely -- a structural drift signal, not a tuned band."""
    possessions = sum(len(g.possessions) for g in corpus)
    mean_frames = {
        name: sum(len(g.plans[name].frames) for g in corpus) / len(corpus) for name in _PRESETS
    }
    lines = [f"games={len(corpus)} possessions={possessions}"]
    for name, mean in mean_frames.items():
        lines.append(
            f"  {name}: mean frames {mean:.1f} -> ~{mean * _TICK_SECONDS[name]:.0f}s at "
            f"{_TICK_SECONDS[name]}s/tick"
        )
    n493 = sum(_is_offensive_rebound_budget_exhaustion(p, b) for _, p, b in _shapes(corpus))
    n558 = sum(_is_non_bonus_foul_budget_exhaustion(p, b) for _, p, b in _shapes(corpus))
    nrestart = sum(_is_restart_tick_violation(p, b) for _, p, b in _shapes(corpus))
    lines.append(
        f"  budget-exhausted close-outs: :493={n493} :558={n558} "
        f"({(n493 + n558) / possessions:.2%}); plus continuation-restart shot-clock "
        f"violations={nrestart} (together {(n493 + n558 + nrestart) / possessions:.2%})"
    )
    endings = Counter(p.ending for _, p, _ in _shapes(corpus))
    off_path = n493 + n558 + nrestart
    lines.append(
        "  endings: "
        + " ".join(f"{e.value}={endings[e]} ({endings[e] / possessions:.2%})" for e in _Ending)
        + f"; off-path total {off_path} ({off_path / possessions:.2%})"
    )
    with capsys.disabled():
        print("\n" + "\n".join(lines))
    assert 0.005 <= off_path / possessions <= 0.03, "the ~1.4% off-path rate drifted"
    # This dict's own `"fast_forward"` key is unchanged (still `possessions_per_frame =
    # 10`, design §6's original table shape, not the bot-layer FAST_FORWARD preset WT-1
    # changed): its measured mean is still 27.1 frames. `"default"` and `"one_per_frame"`
    # now read `possessions_per_frame = 2` and `= 1` (watch-v5 WT-1/WT-7 item 5), so their
    # measured means moved to 103.9 (was 40.1 at the old ppf=6) and 199.4 (was 104.2 at
    # the old ppf=2) -- a consequence of the `_PRESETS` change, not a separate tuning, but
    # not caught by WT-7's own completeness list, so it is fixed here rather than
    # silently left red.
    assert 22 <= mean_frames["fast_forward"] <= 33
    assert 85 <= mean_frames["default"] <= 125
    assert 165 <= mean_frames["one_per_frame"] <= 235


def test_an_empty_beat_log_plans_no_frames():
    empty = dataclasses.replace(_simulate(0), beat_log=())
    plan = plan_broadcast(empty, game_id=1, home_code=_HOME_CODE, away_code=_AWAY_CODE)
    assert plan.frames == () and plan.total_possessions == 0


# --- hand-checked plan arithmetic -------------------------------------------


def test_clock_format_rounds_up_so_zero_zero_means_the_period_is_over():
    assert _format_clock(720.0) == "12:00"
    assert _format_clock(300.0) == "5:00"
    assert _format_clock(431.7) == "7:12"
    assert _format_clock(59.0) == "0:59"
    assert _format_clock(0.4) == "0:01"
    assert _format_clock(0.0) == "0:00"
    with pytest.raises(ValueError):
        _format_clock(-0.1)


def test_a_plan_reads_back_its_own_frames(corpus):
    game = next(g for g in corpus if g.seed == 0)
    plan = game.plans["default"]
    assert plan.game_id == _GAME_ID
    assert plan.settings == _PRESETS["default"]
    tipoff = plan.frames[0]
    assert tipoff.clock_display == "12:00" and tipoff.span is None
    first_play = plan.frames[1]
    assert first_play.kind is FrameKind.PLAY and first_play.span == (0, 1)
    assert plan.frames[-1].kind is FrameKind.FINAL
    assert plan.frames[-2].kind is FrameKind.PERIOD_END and plan.frames[-2].clock_display == "0:00"


def test_overtime_frames_carry_periods_above_four_and_close_at_zero(corpus):
    # BDA-4 fix (docs/design/block-defender-attribution.md): seed 48 no
    # longer reaches overtime under `resolution_rim_help` -- see
    # `test_pre_change_golden_seeds_0_49.py`'s own note. Seed 36 did, under
    # that calibration; RC-4/RC-16's minutes-curve fix
    # (docs/design/auto-gm-role-construction.md) moved who's on the floor
    # late again, and seed 36 no longer reaches overtime -- seed 5 did,
    # under that curve. PD-10 (docs/design/gm-command-surface.md) and the
    # concurrently-landed RC-20/per-36-spread-compression rebound fix both
    # then moved every seed's late-game margin again -- seed 5 no longer
    # reaches overtime, seed 41 does, under the combined engine. per-36-
    # spread-compression Commit 4 (M1, re-anchoring ranks 1-5 a second
    # time, 2026-09-23) moved it once more -- seed 41 no longer reaches
    # overtime, seed 13 does. pick-and-pop C3 (TUNING_VERSION 18 -> 19) moved it again -- seed 13 no
    # longer reaches overtime, seed 68 does; its C4 calibration (19 -> 20) then
    # moved it to seed 18; turnover-recovery (TUNING_VERSION 20 -> 21) to 146;
    # the weighted recoverer draw (21 -> 22) to 27; block-attribution-v2 (TUNING_VERSION 23 -> 25)
    # moved it to seed 21 (scanned 0-99: 21 only); the scale-13 correction
    # (TUNING_VERSION 25 -> 26) moved it to seed 12 (scanned 0-99: 12 only).
    game = next(g for g in corpus if g.seed == 12)
    assert game.result.overtime_periods == 1
    plan = game.plans["default"]
    period_ends = [f for f in plan.frames if f.kind is FrameKind.PERIOD_END]
    assert [f.period for f in period_ends] == [1, 2, 3, 4, 5]
    assert all(f.clock_display == "0:00" for f in period_ends)
    assert plan.frames[-1].period == 5
    first_overtime = next(f for f in plan.frames if f.period == 5)
    assert first_overtime.kind is FrameKind.PLAY


# --- mutation twins: each checker must go red on a plan that breaks its rule


def _regulation_game(corpus) -> _Game:
    return next(g for g in corpus if g.seed == 1)


def _replace_frame(plan: BroadcastPlan, position: int, **changes) -> BroadcastPlan:
    frames = list(plan.frames)
    frames[position] = dataclasses.replace(frames[position], **changes)
    return dataclasses.replace(plan, frames=tuple(frames))


def _first(plan: BroadcastPlan, kind: FrameKind, *, period: int | None = None) -> int:
    return next(
        f.index for f in plan.frames if f.kind is kind and (period is None or f.period == period)
    )


def _replace_possession(possessions, position: int, **changes):
    mutated = list(possessions)
    mutated[position] = dataclasses.replace(mutated[position], **changes)
    return tuple(mutated)


def test_i1_has_teeth_a_period_end_frame_with_a_span(corpus):
    """Mutation twin (j): give a PERIOD_END frame a non-None span."""
    plan = _regulation_game(corpus).plans["default"]
    _check_i1(plan)
    broken = _replace_frame(plan, _first(plan, FrameKind.PERIOD_END), span=(0, 0))
    with pytest.raises(AssertionError):
        _check_i1(broken)


def test_i1_has_teeth_a_dropped_or_doubled_possession(corpus):
    plan = _regulation_game(corpus).plans["default"]
    play = _first(plan, FrameKind.PLAY)
    first, last = plan.frames[play].span
    with pytest.raises(AssertionError):
        _check_i1(_replace_frame(plan, play, span=(first, last - 1)))  # one dropped
    with pytest.raises(AssertionError):
        _check_i1(_replace_frame(plan, play, span=(first, last + 1)))  # one doubled
    with pytest.raises(AssertionError):
        _check_i1(_replace_frame(plan, play, span=None))


def test_i2_has_teeth_a_final_score_off_by_one(corpus):
    game = _regulation_game(corpus)
    plan = game.plans["default"]
    _check_i2(game.result, plan)
    with pytest.raises(AssertionError):
        _check_i2(game.result, _replace_frame(plan, len(plan.frames) - 1, home_score=1000))
    with pytest.raises(AssertionError):
        _check_i2(game.result, _replace_frame(plan, len(plan.frames) - 2, away_score=1000))


def test_i3_has_teeth_a_pre_possession_score_in_a_period_close(corpus):
    game = _regulation_game(corpus)
    plan = game.plans["default"]
    _check_i3(game.result, plan)
    position = _first(plan, FrameKind.PERIOD_END, period=2)
    home = plan.frames[position].home_score
    with pytest.raises(AssertionError):
        _check_i3(game.result, _replace_frame(plan, position, home_score=home - 2))


def test_i4_has_teeth_a_wrong_end_clock_a_wrong_display_and_a_period_not_ending_at_zero(corpus):
    game = _regulation_game(corpus)
    plan, possessions = game.plans["default"], game.possessions
    _check_i4(possessions, plan)
    # end clock is not the next possession's start
    with pytest.raises(AssertionError):
        _check_i4(_replace_possession(possessions, 3, clock_end_seconds=1.0), plan)
    # a period's last possession does not end at 0.0
    last_of_period_1 = next(p.index for p in possessions if p.period == 2) - 1
    with pytest.raises(AssertionError):
        _check_i4(_replace_possession(possessions, last_of_period_1, clock_end_seconds=2.5), plan)
    # a frame shows the wrong clock
    with pytest.raises(AssertionError):
        wrong_display = _replace_frame(plan, _first(plan, FrameKind.PLAY), clock_display="9:99")
        _check_i4(possessions, wrong_display)
    # start clocks not decreasing
    with pytest.raises(AssertionError):
        _check_i4(_replace_possession(possessions, 3, clock_start_seconds=10_000.0), plan)


def test_i5_has_teeth_a_score_that_goes_backwards(corpus):
    plan = _regulation_game(corpus).plans["default"]
    _check_i5(plan)
    late = _first(plan, FrameKind.PERIOD_END, period=3)
    with pytest.raises(AssertionError):
        _check_i5(_replace_frame(plan, late, away_score=0))


def test_i6_has_teeth_a_scoring_leaf_that_scored_nothing(corpus):
    possessions = _regulation_game(corpus).possessions
    _check_i6(possessions)
    made = next(p.index for p in possessions if p.terminal_leaf == "made")
    with pytest.raises(AssertionError):
        _check_i6(_replace_possession(possessions, made, points=0))


def test_i7_has_teeth_a_broken_chain(corpus):
    possessions = _regulation_game(corpus).possessions
    _check_i7(possessions)
    with pytest.raises(AssertionError):
        _check_i7(_replace_possession(possessions, 5, score_after=(999, 999)))


def test_i8_has_teeth_the_wrong_home_team(corpus):
    game = _regulation_game(corpus)
    _check_i8(game.result, game.possessions, game.beats)
    swapped = dataclasses.replace(game.result, home_team_id=game.result.away_team_id)
    with pytest.raises(AssertionError):
        _check_i8(swapped, game.possessions, game.beats)


def test_the_shape_and_momentum_checks_have_teeth(corpus):
    game = _regulation_game(corpus)
    plan = game.plans["default"]
    _check_shape(game.result, game.possessions, plan)
    play = _first(plan, FrameKind.PLAY)
    wrong_momentum = dataclasses.replace(plan.frames[play].momentum, home_points=999)
    with pytest.raises(AssertionError):
        wrong_plan = _replace_frame(plan, play, momentum=wrong_momentum)
        _check_shape(game.result, game.possessions, wrong_plan)
    # a PLAY frame crossing a period boundary
    boundary = next(p.index for p in game.possessions if p.period == 2)
    last_of_period_1 = next(
        f for f in plan.frames if f.kind is FrameKind.PLAY and f.span[1] == boundary - 1
    )
    with pytest.raises(AssertionError):
        _check_shape(
            game.result,
            game.possessions,
            _replace_frame(plan, last_of_period_1.index, span=(last_of_period_1.span[0], boundary)),
        )


def test_i9_has_teeth_a_frame_off_its_own_lines(corpus):
    """Mutation twin for I9: a frame whose score or clock disagrees with the
    lines it renders must fail the check. The old twin's other half -- a
    settings object that bypassed `chunk_budget_units` to overflow a period
    message -- has no analogue any more: `PlanSettings.__post_init__`'s ticker
    inequality (T-1.a) is a configuration-time bound on the ONLY thing that
    still varies with settings (the ticker's own worst case), so there is no
    lever left in `render_live_component`'s inputs that reaches
    `Panel.__post_init__`'s ceiling -- which is what "bounded by construction"
    means."""
    game = _regulation_game(corpus)
    plan = game.plans["default"]
    _check_i9(game.result, plan)
    play = _first(plan, FrameKind.PLAY)
    with pytest.raises(
        AssertionError, match="not at the last possession's|not at the frame's score"
    ):
        _check_i9(game.result, _replace_frame(plan, play, home_score=999))
    with pytest.raises(AssertionError, match="not at the frame's clock"):
        _check_i9(game.result, _replace_frame(plan, play, clock_display="9:99"))


def test_i10_has_teeth_a_slate_line_that_leaks_the_final_score(corpus):
    """Mutation twin for I10 over the corpus: a matchup renderer that appends
    the game's real score is caught, and so is one that drops a number."""
    game = _regulation_game(corpus)
    _check_i10(game.result)
    leaked = f" {game.result.home_score}-{game.result.away_score}"
    with pytest.raises(AssertionError):
        _check_i10(game.result, matchup=lambda g: build_matchup_line(g) + leaked)
    with pytest.raises(AssertionError):
        _check_i10(game.result, matchup=lambda g: "Game · DAL @ LAL")


# --- the plan keeps no beats (N7) ---------------------------------------------

# 72_000 is the pin watch-v2's design raises it to for the `_PlayEvent`s a possession now
# carries (design table row `presentation/broadcast.py`). `_PlayEvent` is a slotted
# dataclass; without `slots=True` the worst plans (seed 6, the overtime game) measured
# fast_forward 61.8 KB, default 71.2 KB, one-per-frame 76.9 KB, over the pin. With slots the
# builder measured 41.3 / 49.2 / 54.9 KB. See
# `test_a_one_possession_per_frame_plan_fits_the_designs_raised_pin`.
_MAX_PLAN_BYTES = 72_000


def _find_beats(value, path: str = "plan") -> list[str]:
    """Where a `BeatRecord` sits anywhere inside `value`, as attribute paths."""
    if isinstance(value, BeatRecord):
        return [path]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [
            hit
            for f in dataclasses.fields(value)
            for hit in _find_beats(getattr(value, f.name), f"{path}.{f.name}")
        ]
    if isinstance(value, tuple | list):
        return [hit for i, item in enumerate(value) for hit in _find_beats(item, f"{path}[{i}]")]
    if isinstance(value, dict):
        return [hit for k, item in value.items() for hit in _find_beats(item, f"{path}[{k!r}]")]
    return []


def _check_no_beats(plan) -> None:
    hits = _find_beats(plan)
    assert not hits, f"the plan holds beat records: {hits[:3]}"


def _check_plan_size(plan) -> None:
    size = len(pickle.dumps(plan))
    assert size <= _MAX_PLAN_BYTES, f"a plan pickles to {size} bytes (limit {_MAX_PLAN_BYTES})"


def test_a_plan_keeps_no_beat_records_and_stays_small(corpus):
    """N7: a plan lives for a whole broadcast, once per game of a block, so it
    keeps the facts the renderer reads off the beats and not the beats. Every
    plan of the corpus at the fast-forward and default presets stays under the
    pin, and no plan of any preset holds a beat."""
    for game in corpus:
        for name, plan in game.plans.items():
            try:
                _check_no_beats(plan)
                if name != "one_per_frame":
                    _check_plan_size(plan)
            except AssertionError as failure:
                raise AssertionError(f"seed {game.seed} {name}: {failure}") from failure


def test_a_one_possession_per_frame_plan_fits_the_designs_raised_pin(corpus):
    for game in corpus:
        _check_plan_size(game.plans["one_per_frame"])


@dataclass(frozen=True)
class _FatPossession:
    possession: _Possession
    beats: tuple[BeatRecord, ...]


def test_the_slimness_checks_have_teeth(corpus):
    """Twin: a plan re-armed with each possession's beats fails the beat scan,
    and a plan padded past the size limit fails the size check."""
    game = _regulation_game(corpus)
    plan = game.plans["default"]
    _check_no_beats(plan)
    _check_plan_size(plan)
    fat = dataclasses.replace(
        plan,
        possessions=tuple(
            _FatPossession(p, game.beats[p.index]) for p in plan.possessions
        ),
    )
    assert _find_beats(fat)[0].startswith("plan.possessions[0].beats[0]")
    with pytest.raises(AssertionError, match="holds beat records"):
        _check_no_beats(fat)
    with pytest.raises(AssertionError, match="pickles to"):
        _check_plan_size(dataclasses.replace(plan, possessions=(b"x" * _MAX_PLAN_BYTES,)))


# --- the narration beside the plan (docs/design/watch-narration-v2.md WN-8, T9) -----------
#
# The plan pin above does not move: the rendered lines live NEXT to the plan, rendered once
# per `/watch` from the beat log the caller still holds, and get their own pin. WN-8 guessed
# "<= 64 KB worst" from the text alone (~3x today's 16.3 KB of lines); the builder measured
# the FULL render at 84,281 B on the TUNING_VERSION 21 overtime seed (146, 843 lines),
# 78,041 B at 22 (seed 27, 776 lines) and 77,909 B at 23 (worst of seeds 0-49 at
# `FULL_PLAN_SETTINGS`' 190-character cap) -- the text is ~52 KB and each `Line` adds
# ~34 B of pickle framing. TERSE is 23-24 KB. The pin keeps ~14% headroom over the
# largest measurement, the plan pin's own margin.
_MAX_NARRATION_BYTES = 96_000
_NARRATION_SETTINGS = (
    PlanSettings(narration=NarrationLevel.TERSE),
    PlanSettings(narration=NarrationLevel.FULL),
    GAME_PLAN_SETTINGS,
    FULL_PLAN_SETTINGS,
)


def _check_narration_size(narration) -> None:
    size = len(pickle.dumps(narration))
    assert size <= _MAX_NARRATION_BYTES, (
        f"a narration pickles to {size} bytes (limit {_MAX_NARRATION_BYTES})"
    )


def test_the_rendered_narration_fits_its_own_pin_and_holds_no_beats(corpus):
    """T9: every corpus game's narration, at every level, stays under its own pin and
    -- like the plan (N7) -- holds no beat record: `plan_narration` drops the beat log
    it re-walks."""
    home, away = make_team(1, _HOME_CODE), make_team(2, _AWAY_CODE)
    for game in corpus:
        for settings in _NARRATION_SETTINGS:
            plan = plan_broadcast(
                game.result,
                game_id=_GAME_ID,
                home_code=_HOME_CODE,
                away_code=_AWAY_CODE,
                settings=settings,
            )
            narration = plan_narration(
                game.result, plan, home=home, away=away, names=names_for(game.result)
            )
            try:
                _check_narration_size(narration)
                assert not _find_beats(narration), "the narration holds beat records"
            except AssertionError as failure:
                raise AssertionError(f"seed {game.seed} {settings}: {failure}") from failure


def test_the_narration_size_check_has_teeth(corpus):
    game = _regulation_game(corpus)
    plan = game.plans["default"]
    home, away = make_team(1, _HOME_CODE), make_team(2, _AWAY_CODE)
    narration = plan_narration(
        game.result, plan, home=home, away=away, names=names_for(game.result)
    )
    _check_narration_size(narration)
    with pytest.raises(AssertionError, match="pickles to"):
        _check_narration_size(dataclasses.replace(narration, lines=(b"x" * _MAX_NARRATION_BYTES,)))


# --- PlanSettings validates its own preconditions (N8) -------------------------

_GOOD_SETTINGS = [
    {},
    {
        "possessions_per_frame": 1,
        "momentum_window_possessions": 1,
        "momentum_margin_threshold": 1,
        "max_pbp_line_chars": 20,
    },
    {"possessions_per_frame": 18},  # ticker_lines=18: worst 3,922 of 4,096 (T-1.a)
    {"max_pbp_line_chars": 243},  # at the default frame (ticker_lines=8): worst 4,094
    {"ticker_window_lines": 6},  # the floor itself (watch-v5 WT-3)
]
_BAD_SETTINGS = [
    {"possessions_per_frame": 0},
    {"possessions_per_frame": -3},
    {"possessions_per_frame": True},
    {"possessions_per_frame": 6.0},
    {"possessions_per_frame": 19},  # ticker_lines=19: worst 4,140, over 4,096
    {"momentum_window_possessions": 0},
    {"momentum_margin_threshold": 0},
    {"max_pbp_line_chars": 19},
    {"max_pbp_line_chars": 0},
    {"max_pbp_line_chars": 244},  # at the default frame (ticker_lines=8): worst 4,110, over 4,096
    {"ticker_window_lines": 5},  # one below the floor (watch-v5 WT-3)
]


def _unrejected(settings_cls) -> list[dict]:
    """The `_BAD_SETTINGS` a `PlanSettings` class lets through, plus the
    `_GOOD_SETTINGS` it wrongly refuses (both listed, so either kind of
    mutant shows)."""
    wrong: list[dict] = []
    for kwargs in _BAD_SETTINGS:
        try:
            settings_cls(**kwargs)
        except ValueError:
            continue
        wrong.append(kwargs)
    for kwargs in _GOOD_SETTINGS:
        try:
            settings_cls(**kwargs)
        except ValueError:
            wrong.append({"refused": kwargs})
    return wrong


def test_plan_settings_refuse_what_the_planner_would_misuse():
    assert _unrejected(PlanSettings) == []


def test_a_refusal_names_the_field_and_the_value():
    with pytest.raises(ValueError, match=r"possessions_per_frame must be an integer >= 1, got 0"):
        PlanSettings(possessions_per_frame=0)
    with pytest.raises(ValueError, match=r"max_pbp_line_chars must be an integer >= 20, got 19"):
        PlanSettings(max_pbp_line_chars=19)
    with pytest.raises(
        ValueError, match=r"a 19-line ticker of 96 character lines plus markup needs up to 4140"
    ):
        PlanSettings(possessions_per_frame=19)
    with pytest.raises(
        ValueError, match=r"a 8-line ticker of 329 character lines plus markup needs up to 5470"
    ):
        PlanSettings(max_pbp_line_chars=329)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(("_MIN_LINE_CHARS = 20\n", "_MIN_LINE_CHARS = 0\n"), id="no-line-floor"),
        pytest.param(("or value < floor:", "or value < 0:"), id="no-count-floor"),
        pytest.param(
            ("if worst_ticker > MAX_DESCRIPTION_CHARS:", "if False:"),
            id="no-fit-bound",
        ),
        pytest.param(("if isinstance(value, bool) or not", "if not"), id="bool-accepted"),
        pytest.param(("or not isinstance(value, int) or", "or"), id="float-accepted"),
    ],
)
def test_the_settings_check_has_teeth_each_guard_removed_is_noticed(mutation):
    """Twin: the unmutated copy passes the same matrix; each variant with one
    guard removed lets a bad setting through."""
    assert _unrejected(broadcast_variant().PlanSettings) == []
    assert _unrejected(broadcast_variant(mutation).PlanSettings), mutation


def test_ticker_lines_is_the_larger_of_the_stated_window_and_the_frame_size():
    """watch-v5 WT-3: the window (`ticker_window_lines`) and the increment
    (`possessions_per_frame`) are decoupled fields now, and `ticker_lines` is the
    larger of the two -- asserted against the presets themselves (T-8 #3), not
    literals -- so v4 T-1.a's no-play-dropped-between-consecutive-ticks guarantee
    (the `max()`) survives untouched."""
    from dba2.presentation.broadcast import TICKER_LINES_FLOOR, ticker_lines

    for settings in _PRESETS.values():
        assert ticker_lines(settings) >= settings.possessions_per_frame
        assert ticker_lines(settings) >= settings.ticker_window_lines
        assert ticker_lines(settings) >= TICKER_LINES_FLOOR
    assert (
        ticker_lines(PlanSettings(possessions_per_frame=2))
        == DEFAULT_PLAN_SETTINGS.ticker_window_lines
        == 8
    )
    assert ticker_lines(PlanSettings(possessions_per_frame=10)) == 10
    assert ticker_lines(PlanSettings(ticker_window_lines=8, possessions_per_frame=2)) == 8


# --- the leaf map covers what the engine really emits (N5) ---------------------


def _unphrased_leaves(all_beats, phrases) -> set[str]:
    """The `resolution` outcomes in `all_beats` that `phrases` has no entry
    for: each would render as the fallback `"<shooter> — <leaf>"`."""
    return {
        b.outcome for beats in all_beats for b in beats if b.beat == "resolution"
    } - set(phrases)


def test_every_resolution_outcome_the_engine_emits_has_a_phrase(corpus):
    """N5: `test_the_leaf_map_is_total_over_the_eleven_censused_resolution_leaves`
    (test_broadcast_render.py) compares the map to a hand-written literal;
    this one compares it to what the engine actually produced over the
    loaded corpus, so an eleventh leaf in `resolution.py` that the census
    missed fails here instead of rendering as a placeholder on a live
    broadcast."""
    all_beats = [beats for g in corpus for beats in g.beats]
    observed = {b.outcome for beats in all_beats for b in beats if b.beat == "resolution"}
    assert _unphrased_leaves(all_beats, _LEAF_PHRASES) == set(), observed
    # non-vacuity: the corpus met every phrased leaf, so "subset" is not "empty"
    assert observed == set(_LEAF_PHRASES), set(_LEAF_PHRASES) ^ observed


def test_the_leaf_coverage_check_has_teeth(corpus):
    """Twin: a leaf missing from the map, and an engine leaf the map never
    heard of, are both reported."""
    game = _regulation_game(corpus)
    all_beats = list(game.beats)
    without_made = {leaf: text for leaf, text in _LEAF_PHRASES.items() if leaf != "made"}
    assert "made" in _unphrased_leaves(all_beats, without_made)
    goaltend = dataclasses.replace(
        next(b for beats in all_beats for b in beats if b.beat == "resolution"), outcome="goaltend"
    )
    assert _unphrased_leaves([*all_beats, (goaltend,)], _LEAF_PHRASES) == {"goaltend"}


# --- line-granular rendering (docs/design/watch-narration-v2.md WN-9, WN-11; T4, T11) -----

_READING_MODES = {"game": GAME_PLAN_SETTINGS, "full": FULL_PLAN_SETTINGS}


def _mode_render(game: _Game, settings: PlanSettings, module=None):
    module = module or broadcast
    plan = module.plan_broadcast(
        game.result,
        game_id=_GAME_ID,
        home_code=_HOME_CODE,
        away_code=_AWAY_CODE,
        settings=settings,
    )
    home = make_team(game.result.home_team_id, _HOME_CODE)
    away = make_team(game.result.away_team_id, _AWAY_CODE)
    narration = module.plan_narration(
        game.result, plan, home=home, away=away, names=names_for(game.result)
    )
    return plan, narration, home, away


def _header_violations(plan, narration, module=None) -> list[str]:
    """T4: at every cursor the header lies in `[score_before, score_after]` of the
    possession on screen, equals `score_after` once its TERMINAL (or the separator
    after it) is shown, and moves by exactly the newest line's own points -- so a
    scoring line and its score change land in the same edit."""
    module = module or broadcast
    bad = []
    previous = (0, 0)
    for cursor, line in enumerate(narration.lines):
        possession = plan.possessions[line.possession]
        home_score, away_score = module.header_score(plan, narration, cursor)
        low, high = possession.score_before, possession.score_after
        if not (low[0] <= home_score <= high[0] and low[1] <= away_score <= high[1]):
            bad.append(f"line {cursor}: header {(home_score, away_score)} outside {low}..{high}")
        if line.kind in ("terminal", "boundary") and (home_score, away_score) != high:
            bad.append(f"line {cursor}: the possession is over but the header is not at {high}")
        moved = home_score + away_score - sum(previous)
        if moved != line.points:
            bad.append(f"line {cursor}: header moved {moved}, the line scored {line.points}")
        previous = (home_score, away_score)
    return bad


@pytest.mark.parametrize("mode", sorted(_READING_MODES))
def test_the_header_score_moves_exactly_when_a_scoring_line_is_revealed(corpus, mode):
    for game in corpus:
        plan, narration, _, _ = _mode_render(game, _READING_MODES[mode])
        bad = _header_violations(plan, narration)
        assert not bad, f"seed {game.seed}: {bad[:3]}"


def test_the_header_check_has_teeth_when_the_score_lags_its_line(corpus):
    """T10 twin for T4: a header that sums the possession's lines BEFORE the newest one
    shows a made basket with an unchanged score -- the defect a frame-level snapshot
    made impossible and a line-level cursor could reintroduce."""
    mutated = broadcast_variant(("    position = cursor\n", "    position = cursor - 1\n"))
    game = _regulation_game(corpus)
    plan, narration, _, _ = _mode_render(game, FULL_PLAN_SETTINGS, module=mutated)
    assert _header_violations(plan, narration, module=mutated)


def test_a_scoring_line_and_its_score_land_in_one_panel(corpus):
    """T4's rendering half, on one full FULL-mode game: at every scoring line's own
    cursor the panel's title already carries the new score and its newest ticker line
    IS that line (marked as newest)."""
    game = _regulation_game(corpus)
    plan, narration, home, away = _mode_render(game, FULL_PLAN_SETTINGS)
    frame_of = {
        index: frame
        for frame in plan.frames
        if frame.kind is FrameKind.PLAY
        for index in range(frame.span[0], frame.span[1] + 1)
    }
    checked = 0
    for cursor, line in enumerate(narration.lines):
        if line.points <= 0:
            continue
        panel = render_live_component(
            frame_of[line.possession],
            plan,
            selected_period=0,
            home=home,
            away=away,
            names=names_for(game.result),
            result=game.result,
            narration=narration,
            cursor=cursor,
        )
        home_score, away_score = header_score(plan, narration, cursor)
        assert panel.title == f"{_AWAY_CODE} {away_score} - {home_score} {_HOME_CODE}"
        assert panel.description.endswith("\n> " + line.text) or panel.description.endswith(
            _LINE_SEPARATOR + "> " + line.text
        )
        checked += 1
    assert checked > 50


def _separator_violations(plan, narration) -> list[str]:
    """T11 (WN-11.4): in a reading mode, exactly one BOUNDARY line between each two
    periods with possessions, right after the ended period's last TERMINAL, carrying
    that period's PERIOD_END score (which is the next period's opening score), and none
    before the first period or after the last; in `fast`, none at all."""
    boundaries = [(n, line) for n, line in enumerate(narration.lines) if line.kind == "boundary"]
    if plan.settings.reveal is RevealUnit.FRAME:
        return [f"a separator in batch mode: {boundaries[0][1].text}"] if boundaries else []
    bad = []
    periods = sorted({p.period for p in plan.possessions})
    if len(boundaries) != len(periods) - 1:
        bad.append(f"{len(boundaries)} separators for {len(periods)} periods")
    period_end = {f.period: f for f in plan.frames if f.kind is FrameKind.PERIOD_END}
    first_of = {}
    for p in plan.possessions:
        first_of.setdefault(p.period, p)
    for n, line in boundaries:
        ended = plan.possessions[line.possession].period
        if n != narration.terminal_at[line.possession] + 1:
            bad.append(f"separator {n} is not right after its period's last TERMINAL")
        if ended + 1 not in first_of:
            bad.append(f"a separator after the last period ({ended})")
            continue
        frame = period_end[ended]
        score = f"{_AWAY_CODE} {frame.away_score} - {frame.home_score} {_HOME_CODE}"
        if score not in line.text:
            bad.append(f"separator {line.text!r} does not carry {score}")
        if first_of[ended + 1].score_before != (frame.home_score, frame.away_score):
            bad.append("the next period does not open at the separator's score")
        label = {1: "End of Q1", 2: "Halftime", 3: "End of Q3", 4: "End of regulation"}.get(
            ended, f"End of OT{ended - 4}"
        )
        if label not in line.text or not line.text.startswith("━" * 10 + "  "):
            bad.append(f"separator {line.text!r} is not the {label} rule")
    return bad


@pytest.mark.parametrize("mode", ["fast", "game", "full"])
def test_a_separator_marks_every_period_boundary_and_nothing_else(corpus, mode):
    settings = {"fast": FAST_PLAN_SETTINGS, **_READING_MODES}[mode]
    overtime_separators = 0
    for game in corpus:
        plan, narration, _, _ = _mode_render(game, settings)
        bad = _separator_violations(plan, narration)
        assert not bad, f"seed {game.seed}: {bad[:3]}"
        overtime_separators += sum(
            "End of regulation" in line.text for line in narration.lines if line.kind == "boundary"
        )
    if mode != "fast":
        assert overtime_separators > 0, "no overtime game: the regulation row is untested"


def test_the_separator_check_has_teeth_when_the_last_period_gets_one(corpus):
    """T11 twin: drop the 'never after the last period' guard."""
    mutated = broadcast_variant(
        (
            "        if frame.kind is not FrameKind.PERIOD_END or frame.period >= last_period:\n",
            "        if frame.kind is not FrameKind.PERIOD_END:\n",
        )
    )
    game = _regulation_game(corpus)
    plan, narration, _, _ = _mode_render(game, GAME_PLAN_SETTINGS, module=mutated)
    assert _separator_violations(plan, narration)


def test_the_longest_separator_fits_a_line_and_the_mark_is_never_stored(corpus):
    game = next(g for g in corpus if g.result.overtime_periods)
    for settings in _READING_MODES.values():
        plan, narration, _, _ = _mode_render(game, settings)
        longest = max(
            (line.text for line in narration.lines if line.kind == "boundary"), key=len
        )
        assert _utf16_units(longest) <= settings.max_pbp_line_chars + _LINE_MARKUP_ALLOWANCE
        assert not any(text.startswith("> ") for text in narration.texts)


@pytest.mark.parametrize("mode", ["fast", "game", "full"])
def test_only_a_live_reading_mode_marks_its_newest_line(corpus, mode):
    """WN-9.2: while LIVE in a reading mode the ticker's newest line, and only it, is
    blockquoted; never at FINAL, never in `fast` (which is what keeps its golden)."""
    settings = {"fast": FAST_PLAN_SETTINGS, **_READING_MODES}[mode]
    game = _regulation_game(corpus)
    plan, narration, home, away = _mode_render(game, settings)
    for frame in plan.frames:
        panel = render_live_component(
            frame,
            plan,
            selected_period=0,
            home=home,
            away=away,
            names=names_for(game.result),
            result=game.result,
            narration=narration,
        )
        marked = [row for row in panel.description.split("\n") if row.startswith("> ")]
        live_reading = mode != "fast" and frame.kind not in (FrameKind.FINAL, FrameKind.TIPOFF)
        assert len(marked) == (1 if live_reading else 0), (frame.index, marked)
        if marked:
            assert panel.description.endswith(marked[0])


def test_a_frames_cursor_and_completed_possession_agree_with_the_frame(corpus):
    """WN-9.1: at a frame's own cursor the newest completed possession is the frame's
    `last_line_index` (so the stats, line score, cycle row and pager cap read exactly
    what they read before), and a PERIOD_END frame in a reading mode reveals its
    separator."""
    game = _regulation_game(corpus)
    for settings in (FAST_PLAN_SETTINGS, *_READING_MODES.values()):
        plan, narration, _, _ = _mode_render(game, settings)
        for frame in plan.frames:
            cursor = frame_cursor(frame, plan, narration)
            assert completed_through(narration, cursor) == last_line_index(frame, plan)
            is_separator = cursor >= 0 and narration.lines[cursor].kind == "boundary"
            expects_separator = (
                frame.kind is FrameKind.PERIOD_END
                and settings.reveal is RevealUnit.LINE
                and frame.period < plan.frames[-1].period
            )
            assert is_separator == expects_separator, (frame.index, frame.kind)


def test_a_pre_line_does_not_complete_its_possession():
    """`completed_through` counts a possession only once its TERMINAL is revealed."""
    lines = (
        Line(LineKind.SETUP, 0, 700.0, "a", 0, 1),
        Line(LineKind.TERMINAL, 0, 690.0, "b", 2, 1),
        Line(LineKind.BOUNDARY, 0, 0.0, "c", 0, 1),
        Line(LineKind.SETUP, 1, 720.0, "d", 0, 1),
    )
    narration = Narration(NarrationLevel.FULL, lines, (1,))
    assert [completed_through(narration, n) for n in (-1, 0, 1, 2, 3)] == [-1, -1, 0, 0, 0]


# --- pacing for a human reader (docs/design/watch-narration-v2.md WN-10; T8) ----------------


@pytest.mark.parametrize(
    ("kind", "chars", "points", "key_play", "expected"),
    [
        (LineKind.SETUP, 0, 0, False, 2.0),  # 0.5 base, clamped UP to read_min
        (LineKind.SETUP, 50, 0, False, 0.5 + 0.035 * 50),  # inside the clamp: 2.25
        (LineKind.ON_BALL, 200, 0, False, 5.0),  # clamped DOWN to read_max
        (LineKind.TERMINAL, 50, 2, False, 0.5 + 0.035 * 50 + 0.8),  # + score_hold
        (LineKind.TERMINAL, 200, 3, True, 5.0 + 0.8 + 1.2),  # + key_hold, after the clamp
        (LineKind.CREDIT, 40, 1, False, 2.0 + 0.8),  # 1.9 clamps UP to read_min, then + score_hold
        (LineKind.BOUNDARY, 70, 0, False, 6.0),  # period_hold, whatever its length
    ],
)
def test_the_reading_time_model_is_the_designs(kind, chars, points, key_play, expected):
    line = Line(kind, 0, 0.0, "x" * chars, points, chars)
    assert line_read_seconds(line, GAME_PLAN_SETTINGS, key_play=key_play) == pytest.approx(
        expected
    )


def test_a_batch_schedule_is_todays_tick_schedule(corpus):
    game = _regulation_game(corpus)
    plan, narration, _, _ = _mode_render(game, FAST_PLAN_SETTINGS)
    schedule = reveal_schedule(plan, narration, tick_seconds=1.5)
    assert [(r.frame_index, r.due_seconds) for r in schedule] == [
        (f.index, f.index * 1.5) for f in plan.frames
    ]
    assert [r.cursor for r in schedule] == [frame_cursor(f, plan, narration) for f in plan.frames]


@pytest.mark.parametrize("mode", sorted(_READING_MODES))
def test_a_reading_schedule_reveals_every_line_once_on_its_readers_clock(corpus, mode):
    """WN-10.2: TIPOFF first (held `read_min`), then every narration line exactly once and
    in order, each due when the line before it has had its reading time; a PERIOD_END
    after the last period re-shows the last line for `read_min`; FINAL last. The plan's
    frames are untouched -- every frame gets at least one reveal."""
    settings = _READING_MODES[mode]
    for game in corpus[:10]:
        plan, narration, _, _ = _mode_render(game, settings)
        schedule = reveal_schedule(plan, narration, tick_seconds=99.0)  # the tick is unused
        starred = broadcast._key_plays(plan.possessions)
        assert schedule[0] == broadcast.Reveal(0, -1, 0.0)
        assert plan.frames[schedule[-1].frame_index].kind is FrameKind.FINAL
        assert {r.frame_index for r in schedule} == {f.index for f in plan.frames}
        # A reveal is NEW when its cursor passes the one before it (the TIPOFF's -1 first).
        pairs = zip(schedule, schedule[1:], strict=False)
        is_new = [False] + [b.cursor > a.cursor for a, b in pairs]
        assert [r.cursor for r, fresh in zip(schedule, is_new, strict=True) if fresh] == list(
            range(len(narration.lines))
        )
        for n, (prev, nxt) in enumerate(zip(schedule, schedule[1:], strict=False)):
            gap = nxt.due_seconds - prev.due_seconds
            if is_new[n]:
                line = narration.lines[prev.cursor]
                key = line.kind is LineKind.TERMINAL and line.possession in starred
                assert gap == pytest.approx(line_read_seconds(line, settings, key_play=key))
            else:  # TIPOFF, or a PERIOD_END that re-shows the last period's final line
                assert gap == pytest.approx(settings.read_min_seconds)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("read_base_seconds", -1.0),
        ("read_per_char_seconds", float("nan")),
        ("read_max_seconds", float("inf")),
        ("score_hold_seconds", True),
        ("period_hold_seconds", "6"),
        ("read_min_seconds", 0.0),
        ("read_max_seconds", 1.5),  # under read_min's 2.0
    ],
)
def test_a_bad_reading_constant_is_refused_and_named(field, value):
    with pytest.raises(ValueError, match="read_min_seconds|" + field):
        PlanSettings(**{field: value})


def test_a_bad_reveal_or_narration_value_is_refused():
    with pytest.raises(ValueError, match="reveal"):
        PlanSettings(reveal="line")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="narration"):
        PlanSettings(narration="full")  # type: ignore[arg-type]
