"""/watch narration v2 (docs/design/watch-narration-v2.md) on hand-built beat logs:
the chain facts a pre-line is built from (WN-1, WN-5, WN-6), so a failure names the
shape, not a seed. The same facts are held to the engine's own record over the seeded
corpus in `test_broadcast_ground_truth.py` (WN-12)."""

from __future__ import annotations

import re

import pytest

from dba2.presentation import broadcast
from dba2.presentation.broadcast import (
    NarrationLevel,
    PlanSettings,
    _chain_segments,
    _core_five,
    _defender_read,
    _elapsed_before,
    _game_clock_at,
    _p_make,
    _restart_reason,
    plan_broadcast,
    segment_possessions,
)
from dba2.presentation.narration_phrases import LIBRARY
from dba2.sim.events import BeatRecord, PlayerBoxLine, TerminalKind, TerminalOutcome, Zone
from tests.unit.presentation._broadcast_helpers import make_team

_HOME, _AWAY = 1, 2
_HOME_IDS = (11, 12, 13, 14, 15)
_AWAY_IDS = (21, 22, 23, 24, 25)
_PLAYER_TEAM = {**{p: _HOME for p in _HOME_IDS}, **{p: _AWAY for p in _AWAY_IDS}}


def _beat(kind: str, outcome: str, shot_clock: float, **actors: int) -> BeatRecord:
    return BeatRecord(
        beat=kind,
        actor_ids=dict(actors),
        outcome=outcome,
        probabilities={},
        clock_seconds=shot_clock,
        home_score=0,
        away_score=0,
        period=1,
        home_score_after=0,
        away_score_after=0,
        game_clock_seconds=700.0,
    )


def _setup(action: str = "iso", shot_clock: float = 24.0, **actors: int) -> BeatRecord:
    base = {"offense:ball_handler": 11, "defense:on_ball_def": 21}
    return _beat("setup", action, shot_clock, **{**base, **actors})


# --- WN-6: the clock inside a possession ---------------------------------------


def test_elapsed_is_the_shot_clocks_fall_between_consecutive_beats():
    beats = [_setup(), _beat("on_ball", "advantage", 20.0), _beat("resolution", "made", 13.0)]
    assert _elapsed_before(beats) == (0.0, 4.0, 11.0)


def test_an_offensive_rebound_reset_rise_consumes_no_time():
    """T3's hand-built half: 9 -> 14 is the reset (`max(current, 14)`), never a
    negative step, and the clock keeps falling from the reset value after it."""
    beats = [
        _setup(),
        _beat("resolution", "missed_unblocked", 15.0),
        _beat("rebound", "offense", 9.0),
        _setup(shot_clock=14.0),
        _beat("resolution", "made", 10.0),
    ]
    elapsed = _elapsed_before(beats)
    assert elapsed == (0.0, 9.0, 15.0, 15.0, 19.0)
    assert all(b >= a for a, b in zip(elapsed, elapsed[1:], strict=False))


def test_the_game_clock_at_a_beat_clamps_at_the_horn():
    log = [
        BeatRecord(
            beat="setup",
            actor_ids={"offense:ball_handler": 11, "defense:on_ball_def": 21},
            outcome="iso",
            probabilities={},
            clock_seconds=24.0,
            home_score=0,
            away_score=0,
            period=1,
            home_score_after=0,
            away_score_after=0,
            game_clock_seconds=6.0,
        )
    ]
    possession = segment_possessions(log, player_team=_PLAYER_TEAM, home_team_id=_HOME)[0]
    assert _game_clock_at(possession, 4.0) == 2.0
    assert _game_clock_at(possession, 9.0) == 0.0


# --- WN-1.1: the make probability, exactly ---------------------------------------


def test_p_make_is_the_ratio_of_the_two_unblocked_shot_leaves():
    record = _beat("resolution", "made", 10.0)
    record = BeatRecord(
        **{
            **record.__dict__,
            "probabilities": {
                "made": 0.2,
                "missed_unblocked": 0.3,
                "turnover_live": 0.1,
                "blocked_miss": 0.05,
            },
        }
    )
    assert _p_make(record) == pytest.approx(0.4)


def test_p_make_is_none_without_both_leaves():
    assert _p_make(_beat("resolution", "made", 10.0)) is None


# --- segments and restarts (WN-4.3) ------------------------------------------------


def test_a_possession_splits_into_one_segment_per_setup():
    beats = [
        _setup(),
        _beat("on_ball", "contained", 18.0),
        _setup(shot_clock=16.0),
        _beat("resolution", "made", 12.0),
    ]
    segments = _chain_segments(beats)
    assert [(s.first, s.last) for s in segments] == [(0, 1), (2, 3)]
    assert [b.beat for b in segments[1].beats(beats)] == ["setup", "resolution"]


def test_a_possession_that_does_not_open_on_a_setup_is_refused():
    with pytest.raises(ValueError):
        _chain_segments([_beat("on_ball", "advantage", 20.0)])


@pytest.mark.parametrize(
    ("previous", "reason"),
    [
        (("on_ball", "contained"), "reset"),
        (("rebound", "offense"), "offensive_board"),
        (("resolution", "non_shooting_foul"), "foul"),
        (("resolution", "loose_ball_recovered"), "scramble"),  # WN-13, after TR
        (("resolution", "made"), None),
        (("rebound", "defense"), None),
    ],
)
def test_the_restart_reason_is_read_off_the_beat_before_the_setup(previous, reason):
    assert _restart_reason(_beat(previous[0], previous[1], 10.0)) == reason


# --- WN-5: a read the defender won ---------------------------------------------------


@pytest.mark.parametrize(
    ("read", "key"),
    [
        (("on_ball", "defender_wins"), "on_ball_defender"),
        (("rotation", "kickout_contested"), "closing_defender"),
        (("rotation", "roll_tagged"), "tagger"),
        (("rotation", "pop_contested"), "popper_defender"),
    ],
)
def test_a_defender_won_read_names_its_winner_when_he_contests_the_shot(read, key):
    segment = [
        _setup(),
        _beat(read[0], read[1], 18.0, **{key: 24}),
        _beat("resolution", "missed_unblocked", 14.0, shooter=11, defender=24),
    ]
    found = _defender_read(segment)
    assert found is not None and (found.beat, found.outcome, found.defender_id) == (*read, 24)


def test_a_read_whose_winner_is_not_the_contesting_defender_credits_nobody():
    segment = [
        _setup(),
        _beat("on_ball", "defender_wins", 18.0, on_ball_defender=21),
        _beat("resolution", "missed_unblocked", 14.0, shooter=11, defender=22),
    ]
    assert _defender_read(segment) is None


@pytest.mark.parametrize(
    "read",
    [
        ("on_ball", "advantage"),
        ("on_ball", "contained"),
        ("rotation", "kickout_open"),
        ("rotation", "roll_live"),
        ("rotation", "pop_open"),
    ],
)
def test_an_open_or_offense_won_read_is_no_defender_fact(read):
    segment = [
        _setup(),
        _beat(read[0], read[1], 18.0, on_ball_defender=21, closing_defender=21),
        _beat("resolution", "missed_unblocked", 14.0, shooter=11, defender=21),
    ]
    assert _defender_read(segment) is None


def test_a_segment_with_no_shot_has_no_defender_fact():
    assert (
        _defender_read([_setup(), _beat("on_ball", "defender_wins", 18.0, on_ball_defender=21)])
        is None
    )


# --- the core five (WN-1.2) -----------------------------------------------------------


def _box(player_id: int, team_id: int, minutes: float) -> PlayerBoxLine:
    fields = {f: 0 for f in PlayerBoxLine.__dataclass_fields__}
    return PlayerBoxLine(
        **{**fields, "player_id": player_id, "team_id": team_id, "minutes_played": minutes}
    )


def test_the_core_five_is_the_top_five_minutes_per_team_ties_to_the_lower_id():
    lines = [_box(pid, _HOME, 30.0 - i) for i, pid in enumerate((11, 12, 13, 14, 15, 16))]
    lines += [_box(pid, _AWAY, 20.0) for pid in (26, 25, 24, 23, 22, 21)]

    class _Result:
        player_box_lines = lines

    assert _core_five(_Result()) == frozenset({11, 12, 13, 14, 15, 21, 22, 23, 24, 25})


def test_the_chain_beat_set_is_the_five_pre_line_beats():
    assert broadcast._CHAIN_BEATS == {"setup", "screen", "on_ball", "help", "rotation"}


# --- the renderer on hand-built possessions (WN-2.2, WN-4, WN-5) -----------------------

_NAMES = {
    11: "Okafor", 12: "Lindqvist", 13: "Marchetti", 14: "Adeyemi", 15: "Kowalczyk",
    21: "Tanaka", 22: "Bellweather", 23: "Ferreira", 24: "Nkemelu", 25: "Petrov",
}  # fmt: skip
_HEAD = re.compile(r"^\d+:\d\d  \S+ ")


def _stamped(beats: list[BeatRecord], *, points: int, terminal, credits=()) -> list[BeatRecord]:
    """One home possession at 700.0 on the period clock, stamped like `_finish` does,
    with the terminal payload (E-1) on its last beat."""
    stamped = [
        BeatRecord(
            **{
                **b.__dict__,
                "home_score": 0,
                "away_score": 0,
                "home_score_after": points,
                "away_score_after": 0,
                "game_clock_seconds": 700.0,
            }
        )
        for b in beats
    ]
    last = stamped[-1]
    stamped[-1] = BeatRecord(
        **{**last.__dict__, "terminal": terminal, "possession_credits": tuple(credits)}
    )
    return stamped


def _box_result(log: list[BeatRecord]):
    lines = [_box(pid, _HOME, 30.0) for pid in _HOME_IDS] + [
        _box(pid, _AWAY, 30.0) for pid in _AWAY_IDS
    ]

    class _Result:
        beat_log = log
        player_box_lines = lines
        home_team_id = _HOME
        away_team_id = _AWAY
        home_score = log[0].home_score_after
        away_score = log[0].away_score_after

    return _Result()


def _narrate(log: list[BeatRecord]) -> list[str]:
    result = _box_result(log)
    plan = plan_broadcast(
        result,
        game_id=412,
        home_code="HOM",
        away_code="AWY",
        settings=PlanSettings(narration=NarrationLevel.FULL, max_pbp_line_chars=200),
    )
    narration = broadcast.plan_narration(
        result, plan, home=make_team(1, "HOM"), away=make_team(2, "AWY"), names=_NAMES
    )
    return [_HEAD.sub("", text, count=1) for text in narration.texts]


def _pnr_setup(shot_clock: float = 24.0) -> BeatRecord:
    return _setup(
        "pnr_ball",
        shot_clock,
        **{"offense:screener": 12, "offense:corner_shooter": 13, "defense:screen_def": 22},
    )


def test_a_switch_names_the_screen_defender_on_the_ball_and_a_forced_miss_credits_him():
    """Appendix A's examples 16 and 13 in one: the S line says the switch (the engine's
    own statement), the O line names the POST-switch defender, and a `defender_wins`
    read followed by an unblocked miss credits that defender with the stop."""
    miss = TerminalOutcome(
        kind=TerminalKind.MISSED_FG, zone=Zone.MID, scorer_id=11,
        rebounder_id=21, rebound_is_offensive=False,
    )  # fmt: skip
    log = _stamped(
        [
            _pnr_setup(),
            _beat("screen", "switch", 20.0, screener=12, screen_defender=22),
            _beat("on_ball", "defender_wins", 17.0, ball_handler=11, on_ball_defender=22),
            _beat("resolution", "missed_unblocked", 14.0, shooter=11, defender=22),
            _beat("rebound", "defense", 11.0, rebounder=21),
        ],
        points=0,
        terminal=miss,
    )
    s_line, o_line, t_line = _narrate(log)
    assert "Okafor" in s_line and "Lindqvist" in s_line and "Bellweather" in s_line
    assert "switch" in s_line
    assert "Bellweather" in o_line and "Tanaka" not in o_line
    assert t_line.startswith("Okafor misses a mid-range shot")
    assert "Bellweather" in t_line.split(",")[0] and t_line.endswith("Tanaka rebounds")


def test_a_reset_names_its_reason_and_opens_on_the_new_holder_without_claiming_a_pass():
    """WN-4.3 as amended by WN-17: after a `contained` read the next setup is prefixed by
    its reason and opens on its new ball holder -- never "{Y} passes to {X}", and no
    "now has it" either: a reset moves the ball by definition, and the line's own
    subject already says who has it."""
    made = TerminalOutcome(kind=TerminalKind.MADE_FG, zone=Zone.RIM, scorer_id=14)
    log = _stamped(
        [
            _setup("iso"),
            _beat("on_ball", "contained", 18.0, ball_handler=11, on_ball_defender=21),
            _setup("iso", 15.0, **{"offense:ball_handler": 14, "defense:on_ball_def": 24}),
            _beat("on_ball", "advantage", 12.0, ball_handler=14, on_ball_defender=24),
            _beat("resolution", "made", 9.0, shooter=14, defender=24),
        ],
        points=2,
        terminal=made,
    )
    lines = _narrate(log)
    assert len(lines) == 5
    restart = lines[2]
    assert restart.startswith(("Reset — Adeyemi ", "They reset — Adeyemi "))
    assert not any(p.template.format(X="Adeyemi") in restart for p in LIBRARY[_NOW])
    assert "pass" not in restart and "Okafor" not in restart
    assert lines[-1].startswith("**HOM 2-0**  Adeyemi scores at the rim")


_NOW = ("R", "now_has_it")


def test_after_an_offensive_board_a_new_holder_is_said_to_have_it():
    """WN-4.3/WN-17: the credit line just told the reader the rebounder had the ball, so
    when someone else starts the next action the restart says so, naming him."""
    miss = TerminalOutcome(
        kind=TerminalKind.MISSED_FG, zone=Zone.MID, scorer_id=11,
        rebounder_id=12, rebound_is_offensive=True,
    )  # fmt: skip
    made = TerminalOutcome(kind=TerminalKind.MADE_FG, zone=Zone.RIM, scorer_id=14)
    log = _stamped(
        [
            _setup("iso"),
            _beat("resolution", "missed_unblocked", 18.0, shooter=11, defender=21),
            _beat("rebound", "offense", 15.0, rebounder=12),
            _setup("iso", 14.0, **{"offense:ball_handler": 14, "defense:on_ball_def": 24}),
            _beat("resolution", "made", 9.0, shooter=14, defender=24),
        ],
        points=2,
        terminal=made,
        credits=(miss,),
    )
    lines = _narrate(log)
    restart = lines[2]
    assert restart.startswith(("Off the offensive board — ", "Second chance — "))
    assert any(p.template.format(X="Adeyemi") in restart for p in LIBRARY[_NOW]), restart


def test_a_kick_out_is_a_pass_line_with_the_help_clause_and_the_role_location():
    made = TerminalOutcome(
        kind=TerminalKind.MADE_FG, zone=Zone.THREE, scorer_id=13, assisted=True, passer_id=11
    )
    log = _stamped(
        [
            _pnr_setup(),
            _beat("screen", "under", 20.0, screener=12, screen_defender=22),
            _beat("on_ball", "advantage", 17.0, ball_handler=11, on_ball_defender=21),
            _beat("help", "help_late", 15.0, driver=11, helper=23),
            _beat(
                "rotation", "kickout_open", 13.0,
                vacated_shooter=13, passer=11, closing_defender=23, screener=12, tagger=23,
            ),
            _beat("resolution", "made", 11.0, shooter=13, defender=23),
        ],
        points=3,
        terminal=made,
    )  # fmt: skip
    s_line, o_line, p_line, t_line = _narrate(log)
    assert "under" in s_line
    assert "Okafor" in o_line and "Tanaka" in o_line and "Ferreira" not in o_line
    assert p_line.startswith("Ferreira") and "late" in p_line
    assert "Marchetti in the corner" in p_line
    assert " over " not in t_line  # an open read never earns "over"


def test_a_two_beat_possession_is_one_setup_line_and_the_terminal():
    """WN-1.1: 43% of possessions are setup + resolution; FULL renders them as S + T,
    and a tough make over a core-five defender says so, before the assist."""
    made = TerminalOutcome(
        kind=TerminalKind.MADE_FG, zone=Zone.MID, scorer_id=13, assisted=True, passer_id=11
    )
    shot = _beat("resolution", "made", 18.0, shooter=13, defender=23)
    shot = BeatRecord(**{**shot.__dict__, "probabilities": {"made": 0.3, "missed_unblocked": 0.7}})
    log = _stamped(
        [
            _beat(
                "setup", "spot_up", 24.0,
                **{"offense:shooter": 13, "offense:passer": 11, "defense:closer": 23},
            ),
            shot,
        ],
        points=2,
        terminal=made,
    )  # fmt: skip
    s_line, t_line = _narrate(log)
    assert s_line.startswith("HOM ball, Okafor") and "Marchetti" in s_line and "Ferreira" in s_line
    assert ", a tough " in t_line and "over Ferreira" in t_line
    assert t_line.index("over Ferreira") < t_line.index("(Okafor assist)")


def test_a_credit_event_gets_its_own_line_in_order_and_a_forced_close_promotes_it():
    """WN-2.1: a non-bonus foul is its own CREDIT line, before the restart it caused;
    when the budget then forces the close (a terminal that owes the reader nothing),
    the last credit becomes the TERMINAL line instead of printing nothing."""
    foul = TerminalOutcome(
        kind=TerminalKind.NON_SHOOTING_FOUL, fouler_id=21, fouled_player_id=11, ft_count=0
    )
    closing = TerminalOutcome(
        kind=TerminalKind.NON_SHOOTING_FOUL, fouler_id=22, fouled_player_id=14, ft_count=0
    )
    forced = TerminalOutcome(kind=TerminalKind.TURNOVER_DEAD)
    log = _stamped(
        [
            _setup("iso"),
            _beat("on_ball", "defender_wins", 18.0, ball_handler=11, on_ball_defender=21),
            _beat("resolution", "non_shooting_foul", 16.0, shooter=11, defender=21),
            _setup("iso", 14.0),
            _beat("on_ball", "defender_wins", 11.0, ball_handler=11, on_ball_defender=21),
            _beat("resolution", "non_shooting_foul", 9.0, shooter=11, defender=22),
        ],
        points=0,
        terminal=forced,
        credits=(foul, closing),
    )
    lines = _narrate(log)
    assert lines[2] == "Tanaka fouls Okafor, no free throws"
    assert lines[3].startswith(("After the foul — ", "Off the foul — "))
    assert lines[-1] == "Bellweather fouls Adeyemi, no free throws"
    assert sum("fouls" in line for line in lines) == 2


def test_a_pre_resolution_shot_clock_violation_keeps_todays_terminal():
    violation = TerminalOutcome(kind=TerminalKind.TURNOVER_DEAD, turnover_player_id=11)
    log = _stamped(
        [_setup("iso"), _beat("on_ball", "advantage", 3.0, ball_handler=11, on_ball_defender=21)],
        points=0,
        terminal=violation,
    )
    lines = _narrate(log)
    assert lines[-1] == "Shot clock violation, HOM"
    assert len(lines) == 3


def test_an_unnamed_action_narrates_no_pre_line_rather_than_a_hole():
    """A hand-built log whose action and roles the grammar does not know (the
    `test_broadcast.py` fixtures) renders only its terminal: a missing role is never a
    line with a blank in it."""
    made = TerminalOutcome(kind=TerminalKind.MADE_FG, zone=Zone.RIM, scorer_id=11)
    log = _stamped(
        [
            _beat("setup", "pick_and_roll", 24.0, **{"offense:spacer_a": 11}),
            _beat("on_ball", "beat_defender", 17.0, ball_handler=11, on_ball_defender=21),
            _beat("resolution", "made", 13.0, shooter=11, defender=21),
        ],
        points=2,
        terminal=made,
    )
    assert _narrate(log) == ["**HOM 2-0**  Okafor scores at the rim"]
