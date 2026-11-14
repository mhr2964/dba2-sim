"""`dba2/media/pulse.py` -- `PulseSnapshot`/`build_pulse_snapshot`/
`diff_pulse_snapshots` (Decisions P4-27, P4-28). §11 Stage D step 17.

Adversarial cases this dispatch specifically asked for: an empty snapshot,
a single-team league, a subject present in the new snapshot but not the
old one and vice versa, and the "missed cycle widens the window, never
corrupts it" scenario (Decision P4-28)."""

from __future__ import annotations

from dba2.media.frame import StoryKind
from dba2.media.pulse import (
    PulseSnapshot,
    TeamPulseState,
    build_pulse_snapshot,
    diff_pulse_snapshots,
)

# ---------------------------------------------------------------------------
# build_pulse_snapshot
# ---------------------------------------------------------------------------


def test_build_pulse_snapshot_ranks_by_games_win_rate_ties_broken_by_team_id():
    # Round 11 rename (nit): the old name embedded a bare "win_pct" substring
    # -- stale since `pulse.py`'s helper was itself renamed away from `_win_
    # pct` (Round 10 critic fix nit 6) to stop colliding with `window.py`'s
    # own `win_pct` field under a grep-based P4-30 compliance check. A test
    # NAME containing the same substring would still trip that grep even
    # though the code it tests no longer does.
    snapshot = build_pulse_snapshot(
        cycle_index=1,
        team_game_results={
            1: (True, True, True),  # 3-0
            2: (True, False),  # 1-1
            3: (True, False),  # 1-1, tie with 2, broken by team_id
            4: (),  # 0-0, no games yet
        },
    )
    assert snapshot.teams[1].standings_position == 1
    assert snapshot.teams[2].standings_position == 2
    assert snapshot.teams[3].standings_position == 3
    assert snapshot.teams[4].standings_position == 4
    assert snapshot.teams[4].games_played == 0


def test_build_pulse_snapshot_empty_league_produces_empty_teams():
    snapshot = build_pulse_snapshot(cycle_index=0, team_game_results={})
    assert snapshot.teams == {}


def test_build_pulse_snapshot_single_team_league_ranks_at_position_one():
    snapshot = build_pulse_snapshot(cycle_index=0, team_game_results={7: (True, False, True)})
    assert snapshot.teams[7].standings_position == 1
    assert snapshot.teams[7].wins == 2
    assert snapshot.teams[7].losses == 1


def test_streak_length_sign_convention_and_empty_case():
    losing_streak = build_pulse_snapshot(
        cycle_index=0, team_game_results={1: (True, False, False, False)}
    )
    assert losing_streak.teams[1].streak_length == -3

    win_streak = build_pulse_snapshot(
        cycle_index=0, team_game_results={1: (False, True, True, True, True)}
    )
    assert win_streak.teams[1].streak_length == 4

    no_games = build_pulse_snapshot(cycle_index=0, team_game_results={1: ()})
    assert no_games.teams[1].streak_length == 0


# ---------------------------------------------------------------------------
# diff_pulse_snapshots -- Decision P4-28's own rules.
# ---------------------------------------------------------------------------


def test_diff_against_absent_snapshot_yields_no_candidates_never_zero_baseline():
    """The dispatch's own named rule, verbatim: absent snapshot -> no
    candidates, never a candidate computed against a fabricated zero."""
    current = build_pulse_snapshot(cycle_index=1, team_game_results={1: (True,) * 10})
    assert diff_pulse_snapshots(None, current) == ()


def test_diff_of_two_empty_snapshots_is_empty():
    empty = PulseSnapshot(cycle_index=0, teams={})
    assert diff_pulse_snapshots(empty, empty) == ()


def test_diff_single_team_league_no_movement_produces_no_candidate():
    previous = build_pulse_snapshot(cycle_index=0, team_game_results={1: (True, True)})
    current = build_pulse_snapshot(cycle_index=1, team_game_results={1: (True, True, True)})
    assert diff_pulse_snapshots(previous, current) == ()


def test_diff_skips_a_team_present_in_current_but_absent_from_previous():
    """An expansion team, or a team's first-ever cycle -- present now,
    absent from the prior snapshot. Must be skipped for THAT team, not
    treated as a 0-baseline collapse/surge."""
    previous = build_pulse_snapshot(cycle_index=0, team_game_results={1: (True,) * 5})
    current = build_pulse_snapshot(
        cycle_index=1, team_game_results={1: (True,) * 5, 99: (False,) * 5}
    )
    candidates = diff_pulse_snapshots(previous, current)
    assert all(c.team_id != 99 for c in candidates)


def test_diff_ignores_a_team_present_in_previous_but_absent_from_current():
    """The reverse case -- a team that existed at the prior pulse and does
    not appear in the current one (removed/relocated). Iteration is driven
    by `current.teams`, so this can never raise or fabricate a candidate
    for a team that no longer exists."""
    previous = build_pulse_snapshot(
        cycle_index=0, team_game_results={1: (True,) * 5, 2: (False,) * 5}
    )
    current = build_pulse_snapshot(cycle_index=1, team_game_results={1: (True,) * 5})
    candidates = diff_pulse_snapshots(previous, current)
    assert all(c.team_id != 2 for c in candidates)


def test_diff_emits_standings_surge_and_collapse_with_correct_sign():
    # Team 1 rises from 4th to 1st (surge); team 4 falls from 1st to 4th (collapse).
    previous = PulseSnapshot(
        cycle_index=0,
        teams={
            1: TeamPulseState(
                team_id=1, wins=5, losses=5, standings_position=4, streak_length=1, win_pct=0.5
            ),
            4: TeamPulseState(
                team_id=4, wins=8, losses=2, standings_position=1, streak_length=3, win_pct=0.8
            ),
        },
    )
    current = PulseSnapshot(
        cycle_index=1,
        teams={
            1: TeamPulseState(
                team_id=1,
                wins=12,
                losses=5,
                standings_position=1,
                streak_length=6,
                win_pct=12 / 17,
            ),
            4: TeamPulseState(
                team_id=4,
                wins=8,
                losses=9,
                standings_position=4,
                streak_length=-3,
                win_pct=8 / 17,
            ),
        },
    )
    candidates = diff_pulse_snapshots(previous, current)
    surge = next(c for c in candidates if c.team_id == 1 and c.kind is StoryKind.STANDINGS_SURGE)
    assert surge.standings_position == 1
    assert surge.standings_position_at_last_pulse == 4

    collapse = next(
        c for c in candidates if c.team_id == 4 and c.kind is StoryKind.STANDINGS_COLLAPSE
    )
    assert collapse.standings_position == 4
    assert collapse.standings_position_at_last_pulse == 1


def test_diff_below_threshold_movement_produces_no_standings_candidate():
    previous = PulseSnapshot(
        cycle_index=0,
        teams={
            1: TeamPulseState(
                team_id=1, wins=5, losses=5, standings_position=5, streak_length=0, win_pct=0.5
            )
        },
    )
    current = PulseSnapshot(
        cycle_index=1,
        teams={
            1: TeamPulseState(
                team_id=1,
                wins=6,
                losses=5,
                standings_position=6,
                streak_length=1,
                win_pct=6 / 11,
            )
        },
    )
    assert diff_pulse_snapshots(previous, current) == ()


def test_diff_emits_streak_candidate_only_when_it_grew_past_threshold():
    previous = PulseSnapshot(
        cycle_index=0,
        teams={
            1: TeamPulseState(
                team_id=1,
                wins=5,
                losses=2,
                standings_position=1,
                streak_length=3,
                win_pct=5 / 7,
            )
        },
    )
    current_below = PulseSnapshot(
        cycle_index=1,
        teams={
            1: TeamPulseState(
                team_id=1, wins=6, losses=2, standings_position=1, streak_length=4, win_pct=0.75
            )
        },
    )
    assert diff_pulse_snapshots(previous, current_below) == ()

    current_above = PulseSnapshot(
        cycle_index=1,
        teams={
            1: TeamPulseState(
                team_id=1, wins=8, losses=2, standings_position=1, streak_length=6, win_pct=0.8
            )
        },
    )
    candidates = diff_pulse_snapshots(previous, current_above)
    assert len(candidates) == 1
    assert candidates[0].kind is StoryKind.STREAK
    assert candidates[0].streak_length == 6


def test_diff_emits_streak_candidate_on_sign_reversal_at_equal_magnitude():
    """Round 10 critic fix (B2): a 5-game LOSING streak reversing straight
    into a 5-game WINNING streak (or -9 -> +9) is the single most
    story-shaped thing a team can do in a cycle, and the old `abs(streak)
    > abs(previous_streak)` comparison read `5 == 5` as zero growth and
    emitted nothing. Both the critic's exact reproduction cases."""
    previous_5 = PulseSnapshot(
        cycle_index=0,
        teams={
            1: TeamPulseState(
                team_id=1,
                wins=2,
                losses=7,
                standings_position=1,
                streak_length=-5,
                win_pct=2 / 9,
            )
        },
    )
    current_5 = PulseSnapshot(
        cycle_index=1,
        teams={
            1: TeamPulseState(
                team_id=1, wins=7, losses=7, standings_position=1, streak_length=5, win_pct=0.5
            )
        },
    )
    candidates = diff_pulse_snapshots(previous_5, current_5)
    streak_candidates = [c for c in candidates if c.kind is StoryKind.STREAK]
    assert len(streak_candidates) == 1
    assert streak_candidates[0].streak_length == 5

    previous_9 = PulseSnapshot(
        cycle_index=0,
        teams={
            1: TeamPulseState(
                team_id=1,
                wins=2,
                losses=11,
                standings_position=1,
                streak_length=-9,
                win_pct=2 / 13,
            )
        },
    )
    current_9 = PulseSnapshot(
        cycle_index=1,
        teams={
            1: TeamPulseState(
                team_id=1,
                wins=11,
                losses=11,
                standings_position=1,
                streak_length=9,
                win_pct=0.5,
            )
        },
    )
    candidates_9 = diff_pulse_snapshots(previous_9, current_9)
    streak_candidates_9 = [c for c in candidates_9 if c.kind is StoryKind.STREAK]
    assert len(streak_candidates_9) == 1
    assert streak_candidates_9[0].streak_length == 9


def test_missed_cycle_widens_the_diff_window_rather_than_corrupting_it():
    """Decision P4-28's own named scenario: cycle N's snapshot is missing,
    so cycle N+2 diffs against cycle N-1's snapshot instead. The result
    must be the two-cycle movement -- correct-but-coarser -- not an error
    and not the same result as a normal one-cycle diff would have given
    for a smaller, single-cycle move."""
    cycle_n_minus_1 = PulseSnapshot(
        cycle_index=0,
        teams={
            1: TeamPulseState(
                team_id=1, wins=2, losses=2, standings_position=8, streak_length=1, win_pct=0.5
            )
        },
    )
    # cycle N is skipped entirely -- never diffed.
    cycle_n_plus_2 = PulseSnapshot(
        cycle_index=2,
        teams={
            1: TeamPulseState(
                team_id=1,
                wins=10,
                losses=2,
                standings_position=1,
                streak_length=8,
                win_pct=10 / 12,
            )
        },
    )
    widened = diff_pulse_snapshots(cycle_n_minus_1, cycle_n_plus_2)
    assert len(widened) == 2  # both a standings surge and a streak
    kinds = {c.kind for c in widened}
    assert kinds == {StoryKind.STANDINGS_SURGE, StoryKind.STREAK}
    surge = next(c for c in widened if c.kind is StoryKind.STANDINGS_SURGE)
    assert surge.standings_position_at_last_pulse == 8
    assert surge.standings_position == 1

    # A single, un-skipped cycle covering only the LAST leg of that same
    # movement reads as a smaller, correctly narrower delta -- proving the
    # widened read above is really coarser, not just differently computed.
    cycle_n_plus_1 = PulseSnapshot(
        cycle_index=1,
        teams={
            1: TeamPulseState(
                team_id=1, wins=8, losses=2, standings_position=2, streak_length=6, win_pct=0.8
            )
        },
    )
    narrow = diff_pulse_snapshots(cycle_n_plus_1, cycle_n_plus_2)
    narrow_surge = next((c for c in narrow if c.kind is StoryKind.STANDINGS_SURGE), None)
    assert narrow_surge is None  # 2 -> 1 is below the 3-spot threshold on its own
