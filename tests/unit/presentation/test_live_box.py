"""`presentation/live_box.py` -- the running tally behind the live scoreboard's
leaders (watch-v2 W-2).

The tally is only worth showing if it is EXACT, so the load-bearing test is the
conservation test: after the last possession of every game of the 34-seed
planner corpus, every player's tally equals the engine's own
`result.player_box_lines` on every counting stat the tally reproduces. The
seed set is `test_broadcast_plan`'s (`range(30)` plus four custom-id seeds), so
the corpus includes overtime games, blowouts and ids other than the fixture's.

Minutes and plus-minus are the two box-line stats a plan cannot reconstruct (they
need lineups) and are not shown live.
"""

from __future__ import annotations

import dataclasses
import functools

import pytest

from dba2.presentation.broadcast import _PlayEvent, segment_possessions
from dba2.presentation.live_box import COUNTING_STATS, LiveLine, leaders, tally
from dba2.sim.events import PlayerBoxLine, TerminalKind, Zone
from tests.unit.presentation.test_broadcast import _log, _segment
from tests.unit.presentation.test_broadcast_plan import (
    _CUSTOM_ID_SEEDS,
    _DEFAULT_SEEDS,
    _simulate,
)

_SEEDS = (*_DEFAULT_SEEDS, *_CUSTOM_ID_SEEDS)


@functools.cache
def _game(seed: int):
    result = _simulate(seed)
    possessions = segment_possessions(
        result.beat_log,
        player_team={line.player_id: line.team_id for line in result.player_box_lines},
        home_team_id=result.home_team_id,
    )
    return result, possessions


def _mismatches(result, possessions) -> list[str]:
    lines = tally(possessions)
    problems: list[str] = []
    box_ids = {line.player_id for line in result.player_box_lines}
    for stray in sorted(set(lines) - box_ids):
        problems.append(f"player {stray} is in the tally but not in the box score")
    for box in result.player_box_lines:
        live = lines.get(box.player_id)
        for stat in COUNTING_STATS:
            got = getattr(live, stat) if live is not None else 0
            if got != getattr(box, stat):
                want = getattr(box, stat)
                problems.append(f"player {box.player_id} {stat}: live {got}, box {want}")
        if live is not None and live.is_home != (box.team_id == result.home_team_id):
            problems.append(f"player {box.player_id} is credited to the wrong side")
    return problems


def test_the_counting_stats_are_exactly_the_sixteen_the_box_line_carries_minus_the_lineup_ones():
    box_fields = {f.name for f in dataclasses.fields(PlayerBoxLine)}
    lineup_only = {"player_id", "team_id", "minutes_played", "plus_minus"}
    assert set(COUNTING_STATS) == box_fields - lineup_only
    assert len(COUNTING_STATS) == 16


@pytest.mark.parametrize("seed", _SEEDS)
def test_the_live_tally_equals_the_box_score_for_every_player_and_every_counting_stat(seed):
    result, possessions = _game(seed)
    assert _mismatches(result, possessions) == []


def test_the_conservation_check_has_teeth_it_sees_a_dropped_credit_event():
    """Twin: throw away every credit event (keep only the terminals) and the same
    comparison must go red -- a check that sums only terminals is the bug class
    `boxscore.py`'s own docstring names."""
    result, possessions = _game(0)
    assert _mismatches(result, possessions) == []
    terminals_only = tuple(
        dataclasses.replace(p, events=p.events[:1]) for p in possessions
    )
    assert _mismatches(result, terminals_only), "dropping credit events went unnoticed"


def test_the_conservation_check_has_teeth_it_sees_a_dropped_free_throw():
    result, possessions = _game(0)
    without_ft = tuple(
        dataclasses.replace(
            p, events=tuple(dataclasses.replace(e, ft_count=0, ftm=0) for e in p.events)
        )
        for p in possessions
    )
    problems = _mismatches(result, without_ft)
    assert any("points" in problem or "fta" in problem for problem in problems)


def test_the_corpus_really_exercises_credit_events_and_every_stat():
    """Non-vacuity: a conservation test over stats that are all zero proves nothing."""
    totals = dict.fromkeys(COUNTING_STATS, 0)
    credited = 0
    for seed in _SEEDS:
        result, possessions = _game(seed)
        credited += sum(len(p.events) - 1 for p in possessions if p.events)
        for box in result.player_box_lines:
            for stat in COUNTING_STATS:
                totals[stat] += getattr(box, stat)
    assert credited > 200 and all(value > 0 for value in totals.values()), (credited, totals)


# --- the rules, one event at a time -----------------------------------------------


def _event(kind: TerminalKind, **fields) -> _PlayEvent:
    base = dict.fromkeys(
        (
            "zone", "scorer_id", "passer_id", "blocking_defender_id", "stealing_defender_id",
            "turnover_player_id", "fouler_id", "fouled_player_id", "offensive_fouler_id",
            "rebounder_id", "rebound_is_offensive",
            # docs/design/turnover-recovery.md TR-4.16: the two recovery ids.
            "loose_ball_player_id", "recovering_player_id",
        )
    )
    flags = {"and_one": False, "assisted": False, "blocked": False, "steal": False}
    return _PlayEvent(kind=kind, ft_count=0, ftm=0, **{**base, **flags, **fields})


def _possession(*events: _PlayEvent, offense_home: bool = True):
    """One possession whose events are `events` (box-score order: terminal first)."""
    log = _log(([("setup", "pick_and_roll", 24.0), ("resolution", "made", 13.0)], 2, 720.0, 1))
    base = _segment(log)[0]
    return dataclasses.replace(base, events=events, offense_is_home=offense_home)


def test_a_made_assisted_three_credits_points_the_attempt_and_the_assist():
    made = _event(TerminalKind.MADE_FG, zone=Zone.THREE, scorer_id=1, passer_id=2, assisted=True)
    lines = tally([_possession(made)])
    assert (lines[1].points, lines[1].fga_three, lines[1].fgm_three) == (3, 1, 1)
    assert lines[2].ast == 1 and lines[1].is_home and lines[2].is_home


def test_a_block_credits_the_defense_and_the_shooters_miss():
    miss = _event(
        TerminalKind.MISSED_FG, zone=Zone.MID, scorer_id=1, blocked=True, blocking_defender_id=9
    )
    lines = tally([_possession(miss)])
    assert (lines[1].fga_mid, lines[1].fgm_mid, lines[1].points) == (1, 0, 0)
    assert lines[9].blk == 1 and lines[9].is_home is False


def test_a_rebound_credits_the_side_the_flag_names():
    defensive = _event(
        TerminalKind.MISSED_FG, zone=Zone.RIM, scorer_id=1, rebounder_id=9,
        rebound_is_offensive=False,
    )
    offensive = _event(
        TerminalKind.MISSED_FG, zone=Zone.RIM, scorer_id=1, rebounder_id=2,
        rebound_is_offensive=True,
    )
    lines = tally([_possession(defensive, offense_home=True), _possession(offensive)])
    assert lines[9].dreb == 1 and lines[9].is_home is False and lines[9].oreb == 0
    assert lines[2].oreb == 1 and lines[2].is_home is True


def test_free_throws_credit_the_fouled_player_and_a_shooting_foul_the_fouler():
    foul = dataclasses.replace(
        _event(TerminalKind.SHOOTING_FOUL, fouler_id=9, fouled_player_id=1), ft_count=3, ftm=2
    )
    lines = tally([_possession(foul)])
    assert (lines[1].fta, lines[1].ftm, lines[1].points) == (3, 2, 2)
    assert lines[9].pf == 1 and lines[9].points == 0


def test_a_steal_is_a_turnover_and_a_steal_and_a_fabricated_dead_ball_is_nothing():
    steal = _event(
        TerminalKind.TURNOVER_LIVE, turnover_player_id=1, steal=True, stealing_defender_id=9
    )
    fabricated = _event(TerminalKind.TURNOVER_DEAD)  # no ball handler: the forced close
    lines = tally([_possession(steal), _possession(fabricated)])
    assert lines[1].tov == 1 and lines[9].stl == 1 and len(lines) == 2


def test_an_offensive_foul_is_a_turnover_and_a_personal_foul_on_the_offense():
    lines = tally([_possession(_event(TerminalKind.OFFENSIVE_FOUL, offensive_fouler_id=1))])
    assert (lines[1].tov, lines[1].pf) == (1, 1) and lines[1].is_home is True


def test_personal_fouls_stop_counting_at_six():
    foul = _event(TerminalKind.NON_SHOOTING_FOUL, fouler_id=9, fouled_player_id=1)
    lines = tally([_possession(foul)] * 8)
    assert lines[9].pf == 6


def test_events_apply_in_box_score_order_and_a_possession_is_counted_once():
    """`tally` reads `possession.events` as given (terminal first, then the
    credits): two possessions' worth of the same list count twice, never once."""
    made = _event(TerminalKind.MADE_FG, zone=Zone.RIM, scorer_id=1)
    assert tally([_possession(made), _possession(made)])[1].points == 4
    assert tally([]) == {}


# --- leaders ----------------------------------------------------------------------


def _line(player_id: int, is_home: bool, **stats) -> LiveLine:
    return LiveLine(player_id=player_id, is_home=is_home, **stats)


def test_leaders_are_the_top_scorers_of_one_side_by_points_rebounds_assists_then_id():
    rows = [
        _line(1, True, points=10, oreb=1, dreb=1, ast=5),
        _line(2, True, points=10, oreb=2, dreb=1, ast=0),  # same points, more boards
        _line(3, True, points=10, oreb=1, dreb=1, ast=2),  # ties 1 on boards, fewer assists
        _line(4, True, points=10, oreb=1, dreb=1, ast=2),  # ties 3 on everything: id decides
        _line(5, True, points=0, dreb=15),  # has not scored: not a leader
        _line(6, False, points=30),
    ]
    assert [line.player_id for line in leaders(rows, is_home=True, count=3)] == [2, 1, 3]
    assert [line.player_id for line in leaders(rows, is_home=True, count=10)] == [2, 1, 3, 4]
    assert [line.player_id for line in leaders(rows, is_home=False, count=2)] == [6]
    assert leaders([], is_home=True, count=2) == []
