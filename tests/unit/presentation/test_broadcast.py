"""`presentation/broadcast.py`'s possession segmentation -- Stage D commit 1
(docs/design/phase-5-stage-d-broadcast.md §4.3, §10 Tier 1). Hand-built
beat sequences only: no engine, no DB, no Discord. The real-game
conservation properties (design §9, over a seeded corpus) land with the
planner in Stage D commit 2, in `test_broadcast_plan.py`; this file pins
the shapes by construction so a failure names the shape, not a seed.

A hand-built beat carries exactly the fields `possession.py::_finish`
stamps: `period`/`*_after`/`game_clock_seconds` are identical on every
beat of one possession, `home_score`/`away_score` are the possession's
ENTRY score, and `clock_seconds` is the SHOT clock at that beat (24.0 at a
possession's opening setup beat, 14.0 at an offensive-rebound restart)."""
from __future__ import annotations

import dataclasses
from dataclasses import replace

import pytest

from dba2.presentation.broadcast import (
    _classify_ending,
    _Ending,
    _foul_points_attributable,
    _group_by_possession,
    segment_possessions,
)
from dba2.sim.events import BeatRecord
from dba2.sim.state import new_possession_state
from dba2.sim.tuning import DEFAULT_TUNING

_HOME, _AWAY = 1, 2
_HOME_IDS = (11, 12, 13, 14, 15)
_AWAY_IDS = (21, 22, 23, 24, 25)
_PLAYER_TEAM = {**{p: _HOME for p in _HOME_IDS}, **{p: _AWAY for p in _AWAY_IDS}}
_ROLES = ("ball_handler", "screener", "spacer_a", "spacer_b", "spacer_c")
_SHOT = 24.0

# (beat, outcome, shot_clock) triples -- one possession's beats, unstamped.
_SETUP = ("setup", "pick_and_roll", _SHOT)


def _actors(beat: str, offense_home: bool) -> dict[str, int]:
    offense = _HOME_IDS if offense_home else _AWAY_IDS
    defense = _AWAY_IDS if offense_home else _HOME_IDS
    if beat == "setup":
        return {
            **{f"offense:{role}": pid for role, pid in zip(_ROLES, offense, strict=True)},
            **{f"defense:{role}": pid for role, pid in zip(_ROLES, defense, strict=True)},
        }
    if beat == "resolution":
        return {"shooter": offense[0], "defender": defense[0]}
    if beat == "rebound":
        return {"rebounder": offense[1]}
    return {"ball_handler": offense[0], "on_ball_defender": defense[0]}


def _possession(
    spec: list[tuple[str, str, float]],
    *,
    offense_home: bool,
    before: tuple[int, int],
    points: int = 0,
    period: int = 1,
    clock: float = 720.0,
) -> list[BeatRecord]:
    after = (before[0] + points, before[1]) if offense_home else (before[0], before[1] + points)
    return [
        BeatRecord(
            beat=beat,
            actor_ids=_actors(beat, offense_home),
            outcome=outcome,
            probabilities={},
            clock_seconds=shot_clock,
            home_score=before[0],
            away_score=before[1],
            period=period,
            home_score_after=after[0],
            away_score_after=after[1],
            game_clock_seconds=clock,
        )
        for beat, outcome, shot_clock in spec
    ]


def _log(*possessions: tuple[list[tuple[str, str, float]], int, float, int]) -> list[BeatRecord]:
    """Chains hand-built possessions the way `game.py` does: strict
    alternation from home, each possession's entry score the previous one's
    after-score. Each item is `(spec, points, start_clock, period)`."""
    log: list[BeatRecord] = []
    score = (0, 0)
    for index, (spec, points, clock, period) in enumerate(possessions):
        beats = _possession(
            spec,
            offense_home=index % 2 == 0,
            before=score,
            points=points,
            period=period,
            clock=clock,
        )
        log.extend(beats)
        score = (beats[0].home_score_after, beats[0].away_score_after)
    return log


def _segment(log):
    return segment_possessions(log, player_team=_PLAYER_TEAM, home_team_id=_HOME)


_MADE = [_SETUP, ("screen", "hedge", 20.0), ("on_ball", "beat_defender", 17.0),
         ("resolution", "made", 13.0)]


# --- the clean shapes ------------------------------------------------------


def test_empty_log_is_an_empty_tuple():
    assert _segment([]) == ()


def test_a_clean_possession_reads_back_every_field():
    log = _log((_MADE, 2, 720.0, 1), (_MADE, 3, 700.0, 1))
    first, second = _segment(log)

    assert first.index == 0 and first.period == 1
    assert first.offense_is_home is True
    assert first.score_before == (0, 0) and first.score_after == (2, 0)
    assert first.points == 2
    assert first.terminal_leaf == "made"
    assert (first.shooter_id, first.defender_id) == (_HOME_IDS[0], _AWAY_IDS[0])
    assert first.rebounder_id is None and first.rebound_is_offensive is None
    assert first.clock_start_seconds == 720.0
    assert [tuple(g) for g in _group_by_possession(log)] == [tuple(log[:4]), tuple(log[4:])]

    assert second.index == 1 and second.offense_is_home is False
    assert second.score_before == first.score_after == (2, 0)
    assert second.score_after == (2, 3) and second.points == 3
    assert (second.shooter_id, second.defender_id) == (_AWAY_IDS[0], _HOME_IDS[0])


def test_end_clock_is_the_next_possessions_start_within_a_period():
    first, second, third = _segment(
        _log((_MADE, 2, 720.0, 1), (_MADE, 3, 700.5, 1), (_MADE, 0, 685.0, 1))
    )
    assert first.clock_end_seconds == second.clock_start_seconds == 700.5
    assert second.clock_end_seconds == third.clock_start_seconds == 685.0


def test_end_clock_is_zero_on_the_last_possession_of_a_period_and_of_the_game():
    """§2.1: 0.0 when the next possession is in another period, or when
    there is no next possession -- regulation's last period and overtime
    alike."""
    log = _log(
        (_MADE, 2, 720.0, 1),
        (_MADE, 2, 3.5, 1),  # period 1's last possession
        (_MADE, 2, 720.0, 2),
    )
    first, last_of_period, opener = _segment(log)
    assert first.clock_end_seconds == 3.5
    assert last_of_period.clock_end_seconds == 0.0
    assert opener.clock_end_seconds == 0.0  # last possession of the whole log


def test_overtime_possessions_segment_across_the_period_4_to_5_boundary():
    log = _log(
        (_MADE, 2, 40.0, 4),
        (_MADE, 2, 22.0, 4),  # period 4's last possession
        (_MADE, 2, 300.0, 5),  # overtime opens at 300.0
        (_MADE, 2, 281.0, 5),
    )
    possessions = _segment(log)
    assert [p.period for p in possessions] == [4, 4, 5, 5]
    assert [p.clock_end_seconds for p in possessions] == [22.0, 0.0, 281.0, 0.0]
    assert [p.index for p in possessions] == [0, 1, 2, 3]


# --- the offensive-rebound continuation ------------------------------------


def test_an_offensive_rebound_restart_is_not_a_possession_boundary():
    """The second setup beat carries the reset shot clock (14), so it is not
    the maximum -- a `beat == "setup"` test alone would have split this
    possession in two. It also pins the rebound fields: the LAST rebound
    beat and its offense/defense vocabulary."""
    spec = [
        _SETUP,
        ("resolution", "missed_unblocked", 17.0),
        ("rebound", "offense", 15.0),
        ("setup", "isolation", 14.0),
        ("resolution", "made", 9.0),
    ]
    (possession,) = _segment(_log((spec, 2, 720.0, 1)))
    assert len(_group_by_possession(_log((spec, 2, 720.0, 1)))[0]) == 5
    assert possession.terminal_leaf == "made"
    assert possession.points == 2
    assert possession.rebound_is_offensive is True
    assert possession.rebounder_id == _HOME_IDS[1]


def test_a_defensive_rebound_ends_the_possession_on_a_miss():
    spec = [_SETUP, ("resolution", "blocked_miss", 17.0), ("rebound", "defense", 16.0)]
    (possession,) = _segment(_log((spec, 0, 720.0, 1)))
    assert possession.terminal_leaf == "blocked_miss"
    assert possession.rebound_is_offensive is False
    assert possession.points == 0


def test_the_last_rebound_wins_when_a_possession_has_several():
    spec = [
        _SETUP,
        ("resolution", "missed_unblocked", 17.0),
        ("rebound", "offense", 15.0),
        ("setup", "isolation", 14.0),
        ("resolution", "missed_unblocked", 9.0),
        ("rebound", "defense", 8.0),
    ]
    (possession,) = _segment(_log((spec, 0, 720.0, 1)))
    assert possession.terminal_leaf == "missed_unblocked"
    assert possession.rebound_is_offensive is False


# --- the None-terminal-leaf shapes (possession.py:642/:695/:732/:816/:877) -


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param([("setup", "isolation", _SHOT)], id="setup-tick-642"),
        pytest.param([_SETUP, ("screen", "hedge", 20.0)], id="screen-tick-695"),
        pytest.param(
            [_SETUP, ("screen", "hedge", 20.0), ("on_ball", "contained", 6.0)],
            id="on_ball-tick-732",
        ),
        pytest.param(
            [_SETUP, ("on_ball", "beat_defender", 12.0), ("help", "commit", 2.0)],
            id="help-tick-816",
        ),
        pytest.param(
            [
                _SETUP,
                ("on_ball", "beat_defender", 12.0),
                ("help", "commit", 8.0),
                ("rotation", "kickout_open", 1.0),
            ],
            id="rotation-tick-877",
        ),
    ],
)
def test_a_pre_resolution_shot_clock_violation_has_no_terminal_leaf(shape):
    """The five sites where a possession ends with no `resolution` beat --
    setup is only ONE of them (design §4.3's minor fix). Each is followed by
    a clean possession so a mis-split would shift its index or score."""
    first, second = _segment(_log((shape, 0, 720.0, 1), (_MADE, 2, 705.0, 1)))
    assert first.terminal_leaf is None
    assert first.shooter_id is None and first.defender_id is None
    assert first.points == 0
    assert first.score_after == (0, 0)
    grouped = _group_by_possession(_log((shape, 0, 720.0, 1), (_MADE, 2, 705.0, 1)))
    assert len(grouped[0]) == len(shape)
    assert second.index == 1 and second.terminal_leaf == "made"
    assert first.clock_end_seconds == 705.0


# --- scoring shapes ---------------------------------------------------------


def test_an_and_one_is_one_possession_with_the_full_delta():
    spec = [_SETUP, ("resolution", "and_one", 15.0)]
    (possession,) = _segment(_log((spec, 3, 720.0, 1)))
    assert possession.terminal_leaf == "and_one"
    assert possession.points == 3
    assert possession.score_after == (3, 0)


def test_free_throw_possessions_take_their_delta_from_the_stamped_score():
    """The record carries no free-throw count (Finding 2); the point total
    is the stamped delta, so a 2-shot foul that goes 1-for-2 reads 1."""
    one_of_two = [_SETUP, ("resolution", "shooting_foul", 15.0), ("rebound", "defense", 14.0)]
    two_of_two = [_SETUP, ("resolution", "shooting_foul", 15.0)]
    first, second = _segment(_log((one_of_two, 1, 720.0, 1), (two_of_two, 2, 700.0, 1)))
    assert (first.terminal_leaf, first.points) == ("shooting_foul", 1)
    assert first.rebound_is_offensive is False  # a missed last FT draws a rebound
    assert (second.terminal_leaf, second.points) == ("shooting_foul", 2)


def test_a_budget_exhausted_possession_keeps_its_credit_event_leaf():
    """`possession.py:493`: an offensive rebound with the continuation
    budget spent closes through `_turnover_dead()` after a credit event
    (here a 1-for-2 trip to the line) was recorded. The log has no phantom
    turnover beat -- the last resolution leaf IS the credit event, and the
    points shown are the possession's real delta (design §4.5)."""
    spec = [_SETUP, ("resolution", "shooting_foul", 15.0), ("rebound", "offense", 14.0)]
    (possession,) = _segment(_log((spec, 1, 720.0, 1)))
    assert possession.terminal_leaf == "shooting_foul"
    assert possession.rebound_is_offensive is True
    assert possession.points == 1


def test_a_non_bonus_foul_with_the_budget_spent_keeps_its_credit_event_leaf():
    """`possession.py:558`: same shape, no rebound beat."""
    spec = [_SETUP, ("resolution", "non_shooting_foul", 15.0)]
    (possession,) = _segment(_log((spec, 0, 720.0, 1)))
    assert possession.terminal_leaf == "non_shooting_foul"
    assert possession.points == 0
    assert possession.rebound_is_offensive is None


# --- every ambiguity raises -------------------------------------------------


def test_a_log_not_opening_on_a_possession_raises():
    log = _log((_MADE, 2, 720.0, 1))
    with pytest.raises(ValueError, match="does not open on a possession-opening setup beat"):
        _segment(log[1:])


def test_a_log_opening_on_a_reset_clock_setup_beat_raises():
    """A setup beat at 14.0 is a continuation restart, not an opener --
    the maximum shot clock in the log (24.0, later) says so."""
    log = _log((_MADE, 2, 720.0, 1), (_MADE, 2, 700.0, 1))
    restart = dataclasses.replace(log[0], clock_seconds=14.0)
    with pytest.raises(ValueError, match="does not open on a possession-opening setup beat"):
        _segment([restart, *log[1:]])


def test_beats_disagreeing_on_a_stamped_fact_raise():
    log = _log((_MADE, 2, 720.0, 1))
    for field, value in (
        ("game_clock_seconds", 719.0),
        ("period", 2),
        ("home_score_after", 5),
        ("home_score", 1),
    ):
        broken = [log[0], dataclasses.replace(log[1], **{field: value}), *log[2:]]
        with pytest.raises(ValueError, match="disagree on their stamped"):
            _segment(broken)


@pytest.mark.parametrize(
    "field,value",
    [
        ("period", 0),
        ("home_score_after", -1),
        ("away_score_after", -1),
        ("game_clock_seconds", -1.0),
    ],
)
def test_an_unstamped_possession_raises(field, value):
    """A sentinel is constant across a possession, so the equality check
    alone would pass it -- the sentinel check is what catches it."""
    log = _log((_MADE, 0, 720.0, 1))
    unstamped = [dataclasses.replace(b, **{field: value}) for b in log]
    with pytest.raises(ValueError, match="unstamped"):
        _segment(unstamped)


def test_disagreeing_offense_derivations_raise():
    """Actors say the AWAY team is on offense at index 0, where parity says
    home (game.py:269)."""
    home_first = _possession(_MADE, offense_home=False, before=(0, 0), points=2)
    with pytest.raises(ValueError, match="offense_is_home is False from the setup actors"):
        _segment(home_first)


def test_swapped_team_assignment_raises_rather_than_misattributing():
    """The same log against the wrong `home_team_id` flips derivation (a)
    on every possession, so the cross-check fires on possession 0."""
    log = _log((_MADE, 2, 720.0, 1), (_MADE, 2, 700.0, 1))
    with pytest.raises(ValueError, match="offense_is_home"):
        segment_possessions(log, player_team=_PLAYER_TEAM, home_team_id=_AWAY)


def test_an_offense_actor_missing_from_player_team_raises():
    log = _log((_MADE, 2, 720.0, 1))
    partial = {pid: team for pid, team in _PLAYER_TEAM.items() if pid != _HOME_IDS[0]}
    with pytest.raises(ValueError, match="in no team's player_team map"):
        segment_possessions(log, player_team=partial, home_team_id=_HOME)


def test_a_setup_beat_with_no_offense_actor_raises():
    log = _log((_MADE, 2, 720.0, 1))
    stripped = dataclasses.replace(
        log[0], actor_ids={k: v for k, v in log[0].actor_ids.items() if k.startswith("defense:")}
    )
    with pytest.raises(ValueError, match="no offense:<role> actor"):
        _segment([stripped, *log[1:]])


def test_the_defenses_score_moving_raises():
    log = _possession(_MADE, offense_home=True, before=(0, 0), points=2)
    log = [dataclasses.replace(b, away_score_after=1) for b in log]
    with pytest.raises(ValueError, match="not a non-negative change to the offense's side"):
        _segment(log)


def test_a_score_going_backwards_raises():
    log = _possession(_MADE, offense_home=True, before=(10, 10), points=0)
    log = [dataclasses.replace(b, home_score_after=8) for b in log]
    with pytest.raises(ValueError, match="not a non-negative change to the offense's side"):
        _segment(log)


def test_an_unrecognized_rebound_outcome_raises():
    spec = [_SETUP, ("resolution", "missed_unblocked", 17.0), ("rebound", "offensive", 15.0)]
    with pytest.raises(ValueError, match="unrecognized rebound outcome"):
        _segment(_log((spec, 0, 720.0, 1)))


# --- what segmentation reads off the beats, because a possession keeps none ---


def _res(leaf: str, clock: float = 15.0):
    return ("resolution", leaf, clock)


def _only(spec, points: int = 0):
    (possession,) = _segment(_log((spec, points, 720.0, 1)))
    return possession


def test_a_possession_keeps_no_beats_only_what_the_renderer_reads_off_them():
    possession = _only(_MADE, 2)
    assert not hasattr(possession, "beats")
    assert possession.ending is _Ending.CLEAN
    assert possession.tail_rebounder_id is None and possession.tail_rebound_is_offensive is None


def test_the_tail_rebound_is_the_one_after_the_last_resolution_not_the_last_of_the_possession():
    """After a foul (no rebound of its own) the last board of the possession
    may be an EARLIER one; the tail rebound must not borrow it."""
    earlier_board = [
        _SETUP,
        _res("missed_unblocked"),
        ("rebound", "offense", 14.0),
        ("setup", "isolation", 14.0),
        _res("non_shooting_foul", 9.0),
    ]
    possession = _only(earlier_board, 0)
    assert possession.rebound_is_offensive is True  # the possession's last board
    assert possession.tail_rebound_is_offensive is None and possession.tail_rebounder_id is None
    after = _only([_SETUP, _res("missed_unblocked"), ("rebound", "defense", 14.0)])
    assert after.tail_rebound_is_offensive is False and after.tail_rebounder_id == _HOME_IDS[1]


@pytest.mark.parametrize(
    ("spec", "points", "attributable", "ending"),
    [
        pytest.param([_SETUP, _res("shooting_foul")], 2, True, _Ending.CLEAN, id="one-resolution"),
        pytest.param(
            [_SETUP, _res("missed_unblocked"), ("rebound", "offense", 14.0),
             ("setup", "isolation", 14.0), _res("non_shooting_foul", 9.0)],
            2, True, _Ending.CLEAN, id="earlier-miss-cannot-have-scored",
        ),
        pytest.param(
            [_SETUP, _res("shooting_foul"), ("rebound", "offense", 14.0),
             ("setup", "isolation", 14.0), _res("non_shooting_foul", 9.0)],
            2, False, _Ending.UNDETERMINED_FOUL_CLOSE, id="earlier-foul-may-have-scored",
        ),
        pytest.param(
            [_SETUP, _res("and_one"), ("rebound", "offense", 14.0),
             ("setup", "isolation", 14.0), _res("shooting_foul", 9.0)],
            3, False, _Ending.CLEAN, id="earlier-and-one-may-have-scored",
        ),
        pytest.param(
            [_SETUP, _res("shooting_foul"), ("rebound", "offense", 14.0),
             ("setup", "isolation", 14.0), _res("non_shooting_foul", 9.0)],
            0, False, _Ending.NON_BONUS_FOUL_CLOSE, id="scoreless-close-is-provable",
        ),
    ],
)
def test_foul_point_attribution_and_ending_are_computed_at_segmentation(
    spec, points, attributable, ending
):
    possession = _only(spec, points)
    assert possession.foul_points_attributable is attributable
    assert possession.ending is ending


def _beats(spec, points: int = 0):
    return _possession(spec, offense_home=True, before=(0, 0), points=points)


def test_an_earlier_loose_ball_recovery_leaves_a_later_fouls_points_attributable():
    """docs/design/turnover-recovery.md TR-5.A.14 / mutation 10. A recovery
    can never score (its payload carries no `ftm`, TR-4.11), so a possession
    that recovers a loose ball and then draws a scoring foul must still
    credit the foul with the points: `CLEAN`, not `UNDETERMINED_FOUL_CLOSE`.
    `test_broadcast_ground_truth._ending_mismatches` permits EITHER verdict on
    exactly this shape, so this is the only test that pins it. The negative
    twin (an earlier made shot, which can score) keeps the test from passing
    by always answering True."""
    recovered = _beats(
        [_SETUP, _res("loose_ball_recovered"), ("setup", "isolation", 12.0),
         _res("non_shooting_foul", 8.0)],
        points=2,
    )
    assert _foul_points_attributable(recovered) is True
    assert (
        _classify_ending(recovered, points=2, foul_points_attributable=True) is _Ending.CLEAN
    )

    made_first = _beats(
        [_SETUP, _res("made"), ("setup", "isolation", 12.0), _res("non_shooting_foul", 8.0)],
        points=2,
    )
    assert _foul_points_attributable(made_first) is False
    assert (
        _classify_ending(made_first, points=2, foul_points_attributable=False)
        is _Ending.UNDETERMINED_FOUL_CLOSE
    )


def test_a_recovery_that_is_the_last_beat_is_the_scramble_violation_close():
    """TR-4.14 / mutation 17 at the classifier itself: a recovery always
    continues the possession, so a `loose_ball_recovered` resolution with no
    beat after it means the scramble ran the shot clock out --
    `RESTART_VIOLATION`, never `CLEAN`. A recovery followed by a restart that
    ALSO expires is the ordinary restart shape and stays one."""
    scramble = _beats([_SETUP, _res("loose_ball_recovered")])
    assert (
        _classify_ending(scramble, points=0, foul_points_attributable=True)
        is _Ending.RESTART_VIOLATION
    )
    restart = _beats([_SETUP, _res("loose_ball_recovered"), ("setup", "isolation", 1.0)])
    assert (
        _classify_ending(restart, points=0, foul_points_attributable=True)
        is _Ending.RESTART_VIOLATION
    )
    finished = _beats([_SETUP, _res("loose_ball_recovered"), ("setup", "isolation", 12.0),
                       _res("made", 8.0)], points=2)
    assert (
        _classify_ending(finished, points=2, foul_points_attributable=True) is _Ending.CLEAN
    )


# --- the assumption segmentation borrows from the engine's tuning --------------


def test_an_offensive_rebound_never_resets_the_shot_clock_back_to_its_start():
    """Segmentation opens a possession on a `setup` beat whose shot clock is
    the log's MAXIMUM (design §4.3, Finding 3) and treats a restart's `setup`
    beat as a continuation only because it carries a lower clock.
    `state.py::PossessionState.reset_on_offensive_rebound` sets the clock to
    `max(current, offensive_rebound_shot_clock_reset)`, so the restart clock
    is below the start only while the reset is below the start. A tuning that
    raised the reset to 24.0 would make every restart look like a new
    possession; nothing else in the suite pins the value."""
    possession = DEFAULT_TUNING.possession
    assert possession.offensive_rebound_shot_clock_reset < possession.shot_clock_seconds


def test_a_reset_that_returned_the_clock_to_the_start_would_break_segmentation():
    """Twin: with the reset raised to the start value the engine's own state
    machine puts a restart's clock at 24.0, and a log carrying that restart
    is not segmented as one possession."""
    tuning = replace(
        DEFAULT_TUNING,
        possession=replace(
            DEFAULT_TUNING.possession,
            offensive_rebound_shot_clock_reset=DEFAULT_TUNING.possession.shot_clock_seconds,
        ),
    )
    state = new_possession_state(tuning)
    state.tick(6.0)
    state.reset_on_offensive_rebound(tuning)
    assert state.shot_clock_seconds == _SHOT  # back at the start: the assumption is gone
    spec = [
        _SETUP,
        ("resolution", "missed_unblocked", 17.0),
        ("rebound", "offense", 15.0),
        ("setup", "isolation", state.shot_clock_seconds),  # the restart, at the possession maximum
        ("resolution", "made", 9.0),
    ]
    with pytest.raises(ValueError, match="offense_is_home"):
        _segment(_log((spec, 2, 720.0, 1)))
