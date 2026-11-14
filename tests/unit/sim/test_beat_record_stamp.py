"""Decision P5-29's proof obligation for the `BeatRecord` engine change
(P5-26 ruling on Phase 5's §14 question 2, designed at P5-27/P5-28):
three invariant tests that fail if `possession.py::_finish`'s stamp is
wrong, not merely absent. Per the design doc's own §17.4 wording:

1. **No sentinel survives** -- proves P5-27's defaults (`period=0`,
   `home_score_after=away_score_after=-1`) are safe rather than
   dangerous, i.e. every real record really did pass through the stamp.
2. **Conservation against `GameResult`** -- proves `possession.py`'s own
   `_points_for` and `game.py`'s own `_points_for` agree on every
   possession of a real game (the two-call-sites duplication P5-28
   deliberately leaves in place, per Decision P5-30).
3. **Chaining** -- proves a possession's own `*_after` pair equals the
   next possession's pre-possession `home_score`/`away_score`, which is
   the check that would catch a credit-event-scoring possession (an
   and-one, a put-back) being stamped from its terminal outcome alone.

None of these existed before this change -- the golden file, and
`test_determinism.py`/`test_possession.py`/`test_events.py`, are
deliberately left unedited (P5-29 items 1-2); this file is the new
detector P5-29 item 5 requires, added in the same commit as the fields
and the stamp.
"""
from __future__ import annotations

import dataclasses
import functools

import pytest

from dba2.sim.boxscore import PossessionRecord, accumulate_game
from dba2.sim.events import (
    BeatRecord,
    PossessionResult,
    TerminalKind,
    TerminalOutcome,
)
from dba2.sim.game import simulate_game
from dba2.sim.possession import _stamp_possession_records
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_game_context

_GAME_ID = 12345
# A spread of seeds, chosen to include at least one overtime game (seed
# selection below verifies this rather than assuming it) so the period
# boundary and chaining invariants are exercised across a period-4 ->
# period-5 transition, not just within regulation.
#
# Assist-compression fix: none of seeds 1-9/777 reach overtime under the
# `SetupTuning.ball_handler_passing_ability_anchor_*` weight (ball-handler
# role draws shifted, which shifts late-game possession outcomes even in a
# game that never itself hits the ball-handler-passing-ability edge case)
# -- 48 was added because it was the nearest seed (scanned 0-59) that did,
# per the same finding documented in `test_pre_change_golden_seeds_0_49.py`.
#
# BDA-4 fix (docs/design/block-defender-attribution.md): 48 no longer
# reaches overtime under `resolution_rim_help` (any resolution-leaf
# probability shift moves late-game score margins, the same class of drift
# the assist-compression fix note above already describes) -- rescanned
# 0-59, replaced with 36, the nearest seed that does under the final
# calibrated tuning (37 briefly worked mid-calibration but stopped once
# `base_rate`/`scale` were solved against `blk_per_game`/`top_shot_
# blocker_bpg`, the same volatility this class of fixture-seed drift
# always has -- rescanned once more against the shipped values, not
# reused from a mid-flight number).
#
# PD-10 (docs/design/gm-command-surface.md) plus the concurrently-landed
# RC-20/per-36-spread-compression rebound fix moved it again, same
# mechanism, see `test_pre_change_golden_seeds_0_49.py`'s own note -- 36
# no longer reaches overtime, 41 does.
#
# per-36-spread-compression Commit 4 (M1, TUNING_VERSION 17 -> 18) moved it
# once more: 41 no longer reaches overtime, 13 (and 48) do.
#
# pick-and-pop C3 (TUNING_VERSION 18 -> 19) moved it again: 13 and 41 no longer
# reach overtime, 68 does (scanned 0-149: 68, 71, 99); its C4 calibration
# (19 -> 20) moved it to 18 (scanned 0-149: 18, 68, 71).
#
# turnover-recovery (TUNING_VERSION 20 -> 21) moved it again: no seed in 0-49
# reaches overtime (scanned 0-199: 146 and 158 do), so 146 replaces 18.
#
# The weighted recoverer draw (TUNING_VERSION 21 -> 22) moved it back inside
# 0-49 (scanned 0-399: 27, 293, 318, 320, 359), so 27 replaces 146.
#
# block-attribution-v2 (TUNING_VERSION 23 -> 25) moved it to seed 21 (scanned 0-99: 21 only),
# so 21 replaces 27.
#
# The scale-13 correction (TUNING_VERSION 25 -> 26) moved it to seed 12
# (scanned 0-99: 12 only), so 12 replaces 21.
_SEEDS = (777, 1, 2, 3, 4, 5, 6, 7, 8, 9, 13, 41, 12)


def _simulate(seed: int):
    ctx = make_game_context(_GAME_ID, tuning=DEFAULT_TUNING)
    return simulate_game(ctx, rng_seed=seed)


def test_seed_spread_includes_at_least_one_overtime_game():
    """A precondition for the chaining test's period-boundary coverage,
    checked rather than assumed -- the preamble's own "exactly N" rule
    applies to test fixtures too."""
    results = [_simulate(seed) for seed in _SEEDS]
    assert any(r.overtime_periods > 0 for r in results), (
        "none of the fixed seed spread produced overtime -- widen _SEEDS "
        "so the chaining invariant below actually covers a period-4 -> "
        "period-5 boundary"
    )


def test_no_sentinel_survives_a_real_simulated_game():
    """P5-27's defaults are impossible real values by design. If any
    record in a real game's beat log still carries one, some path
    through `_finish` failed to stamp it."""
    for seed in _SEEDS:
        result = _simulate(seed)
        assert result.beat_log, "fixture produced no beats -- test is vacuous"
        for i, record in enumerate(result.beat_log):
            assert record.period >= 1, (
                f"seed {seed} beat {i} ({record.beat}) has unstamped period {record.period}"
            )
            assert record.home_score_after >= 0, (
                f"seed {seed} beat {i} ({record.beat}) has sentinel home_score_after"
            )
            assert record.away_score_after >= 0, (
                f"seed {seed} beat {i} ({record.beat}) has sentinel away_score_after"
            )


def test_beat_log_conservation_against_game_result():
    """The final stamped record's after-score must equal `GameResult`'s
    own final score, and each period's stamped scoring must equal
    `GameResult.period_scores`' entry for that period. This is the check
    that proves `possession.py::_points_for` and `game.py::_points_for`
    agree on every possession of a real game -- if they ever diverge,
    this fails on the first game simulated rather than on a scoreboard
    in a live guild."""
    for seed in _SEEDS:
        result = _simulate(seed)
        last = result.beat_log[-1]
        assert (last.home_score_after, last.away_score_after) == (
            result.home_score,
            result.away_score,
        ), f"seed {seed}: final stamped score does not match GameResult's own final score"

        # last *_after pair carrying each period, keyed by period number
        last_after_by_period: dict[int, tuple[int, int]] = {}
        for record in result.beat_log:
            last_after_by_period[record.period] = (record.home_score_after, record.away_score_after)

        periods_seen = sorted(last_after_by_period)
        assert periods_seen == list(range(1, len(periods_seen) + 1)), (
            f"seed {seed}: beat log periods are not a contiguous 1..N run: {periods_seen}"
        )

        prior_home, prior_away = 0, 0
        for period_number, home_delta, away_delta in result.period_scores:
            assert period_number in last_after_by_period, (
                f"seed {seed}: period_scores names period {period_number} with no beat log entry"
            )
            cum_home, cum_away = last_after_by_period[period_number]
            assert cum_home - prior_home == home_delta, (
                f"seed {seed} period {period_number}: beat log home delta "
                f"{cum_home - prior_home} != GameResult.period_scores' {home_delta}"
            )
            assert cum_away - prior_away == away_delta, (
                f"seed {seed} period {period_number}: beat log away delta "
                f"{cum_away - prior_away} != GameResult.period_scores' {away_delta}"
            )
            prior_home, prior_away = cum_home, cum_away


def test_beat_log_chaining_across_possession_boundaries():
    """For consecutive records that cross a possession boundary, the
    earlier possession's `*_after` pair must equal the later possession's
    pre-possession `home_score`/`away_score`. This is the check that
    catches a credit-event-scoring possession (an and-one, a put-back)
    being stamped from its terminal outcome alone -- the sharp edge
    Decision P5-27/§17.1 names explicitly: `possession.py`'s own
    `running_home`/`running_away` count only the terminal, never credit
    events, so a stamp built on them (rather than on the full
    `(outcome, *credit_events)` set) would pass every check above and
    still be wrong exactly at an and-one."""
    for seed in _SEEDS:
        result = _simulate(seed)
        beats = result.beat_log
        # A single possession can loop back through more than one `setup`
        # beat (an offensive-rebound continuation restarts the action
        # without ending the possession -- `possession.py`'s own
        # `attempt_index`/`is_first_attempt` shape), so `beat == "setup"`
        # is NOT a reliable possession-boundary marker. What IS reliable:
        # every record `_finish` stamps in one call shares the exact same
        # (before, after) score tuple, because they are stamped from the
        # same possession's own `home_score`/`away_score`/`offense_points`
        # -- so a change in that tuple is the possession boundary.
        groups: list[tuple[int, int, int, int]] = []
        for record in beats:
            key = (
                record.home_score,
                record.away_score,
                record.home_score_after,
                record.away_score_after,
            )
            if not groups or groups[-1] != key:
                groups.append(key)
        assert len(groups) > 1, f"seed {seed}: fixture produced only one possession"
        for i in range(1, len(groups)):
            prev_home, prev_away, prev_after_home, prev_after_away = groups[i - 1]
            cur_home, cur_away, _cur_after_home, _cur_after_away = groups[i]
            assert (cur_home, cur_away) == (prev_after_home, prev_after_away), (
                f"seed {seed} possession {i}: boundary score mismatch -- "
                f"prev after=({prev_after_home}, {prev_after_away}) "
                f"cur before=({cur_home}, {cur_away})"
            )


# --- Stage D commit 0 (P5-97.b): the stamped game clock -------------------
#
# `game_clock_seconds` is the period clock at a possession's START, stamped
# in the same `replace(...)` call as the fields above. The two tests below
# are the assertions phase-5-stage-d-broadcast.md §2.2 names for it, plus a
# constancy check and a field-set pin (`test_events.py` pins
# `PossessionResult`'s field set but has never pinned `BeatRecord`'s -- see
# design §22.1 -- and it is deliberately not edited to start now).


def _period_length(period: int) -> float:
    possession = DEFAULT_TUNING.possession
    return possession.period_seconds if period <= 4 else possession.overtime_seconds


def _possessions(beat_log):
    """Split a beat log into possessions per design §4.3: a new possession
    starts at every `setup` beat whose shot clock equals the log's maximum
    shot clock (an offensive-rebound continuation's `setup` beat carries the
    reset shot clock, which is lower). The first beat must be such a beat."""
    shot_clock_start = max(b.clock_seconds for b in beat_log)
    groups: list[list[BeatRecord]] = []
    for record in beat_log:
        if record.beat == "setup" and record.clock_seconds == shot_clock_start:
            groups.append([])
        assert groups, "beat log does not open on a possession-opening setup beat"
        groups[-1].append(record)
    return groups


def test_beat_record_field_set_is_pinned():
    """`BeatRecord`'s field set, pinned so a further addition is a
    deliberate, test-visible act (design §22.1's named weakest link)."""
    assert [f.name for f in dataclasses.fields(BeatRecord)] == [
        "beat",
        "actor_ids",
        "outcome",
        "probabilities",
        "clock_seconds",
        "home_score",
        "away_score",
        "period",
        "home_score_after",
        "away_score_after",
        "game_clock_seconds",
        "terminal",
        "possession_credits",
    ]
    assert BeatRecord.__dataclass_fields__["game_clock_seconds"].default == -1.0
    # watch-v2 E-1: defaulted, appended AFTER game_clock_seconds, so every
    # existing positional/keyword construction site keeps working.
    assert BeatRecord.__dataclass_fields__["terminal"].default is None
    assert BeatRecord.__dataclass_fields__["possession_credits"].default == ()


def test_no_game_clock_sentinel_survives_a_real_simulated_game():
    """`game_clock_seconds`'s default (-1.0) is impossible for a real
    period clock, so any record still carrying it skipped the stamp."""
    for seed in _SEEDS:
        result = _simulate(seed)
        assert result.beat_log, "fixture produced no beats -- test is vacuous"
        for i, record in enumerate(result.beat_log):
            assert record.game_clock_seconds >= 0.0, (
                f"seed {seed} beat {i} ({record.beat}) has sentinel game_clock_seconds "
                f"{record.game_clock_seconds}"
            )


def test_game_clock_is_identical_across_every_beat_of_one_possession():
    for seed in _SEEDS:
        result = _simulate(seed)
        possessions = _possessions(result.beat_log)
        assert len(possessions) > 1, f"seed {seed}: fixture produced only one possession"
        for n, beats in enumerate(possessions):
            clocks = {b.game_clock_seconds for b in beats}
            periods = {b.period for b in beats}
            assert len(clocks) == 1, (
                f"seed {seed} possession {n}: beats disagree on game_clock_seconds {clocks}"
            )
            assert len(periods) == 1, f"seed {seed} possession {n}: beats span periods {periods}"


def test_possession_start_clocks_decrease_within_a_period_and_reset_at_each_boundary():
    """Start clocks are strictly decreasing within a period; the first
    possession of period p starts at the period's full length (720.0
    regulation, 300.0 overtime); and the clock resets upward exactly where
    the period number changes -- never anywhere else."""
    saw_overtime = False
    for seed in _SEEDS:
        result = _simulate(seed)
        possessions = _possessions(result.beat_log)
        prev_period: int | None = None
        prev_clock: float | None = None
        for n, beats in enumerate(possessions):
            period, clock = beats[0].period, beats[0].game_clock_seconds
            assert 0.0 < clock <= _period_length(period), (
                f"seed {seed} possession {n}: start clock {clock} outside "
                f"(0, {_period_length(period)}] for period {period}"
            )
            if period != prev_period:
                if prev_period is not None:
                    assert period == prev_period + 1, (
                        f"seed {seed} possession {n}: period jumped {prev_period} -> {period}"
                    )
                    assert clock > prev_clock, (
                        f"seed {seed} possession {n}: clock did not reset upward at the "
                        f"period {prev_period} -> {period} boundary ({prev_clock} -> {clock})"
                    )
                assert clock == _period_length(period), (
                    f"seed {seed} possession {n}: period {period} opens at {clock}, "
                    f"not {_period_length(period)}"
                )
                saw_overtime = saw_overtime or period > 4
            else:
                assert clock < prev_clock, (
                    f"seed {seed} possession {n}: start clock not strictly decreasing "
                    f"within period {period} ({prev_clock} -> {clock})"
                )
            prev_period, prev_clock = period, clock
    assert saw_overtime, "seed spread never reached an overtime period -- 300.0 reset unchecked"


# --- watch-v2 E-1: the terminal payload on a possession's last record -------
#
# docs/design/watch-v2-scrollable-broadcast.md, "What the data can say, and
# the engine change that makes it so". The corpus is seeds 0-49 (the same
# fixture context every golden uses, widened from 0-29 by the assist-
# compression fix); some seed in that range goes to overtime (36, as of the
# BDA-4 fix -- see `test_pre_change_golden_seeds_0_49.py`'s own note, which
# has shifted more than once), which the first test below checks rather
# than assumes.

# turnover-recovery (TUNING_VERSION 20 -> 21): no seed in 0-49 reaches
# overtime any more; 146 is appended rather than widening the whole range
# (the same stopgap pick-and-pop C3 used, see `_SEEDS` above). The weighted
# recoverer draw (21 -> 22) put an overtime seed (27) back inside 0-49, so the
# append is dropped, as pick-and-pop C4 dropped its own. block-attribution-v2 (TUNING_VERSION 23 ->
# 25) moved it to seed 21 (scanned 0-99: 21 only); the scale-13 correction (25 -> 26) to
# seed 12 (scanned 0-99: 12 only).
_CORPUS_SEEDS = tuple(range(50))
_HOME_TEAM_ID, _AWAY_TEAM_ID = 1, 2  # make_game_context's defaults

# The resolution beat's leaf -> the `TerminalKind` its `_build_outcome` makes.
_LEAF_TO_KIND = {
    "made": TerminalKind.MADE_FG,
    "and_one": TerminalKind.MADE_FG,
    "missed_unblocked": TerminalKind.MISSED_FG,
    "blocked_miss": TerminalKind.MISSED_FG,
    # BDA-4 (docs/design/block-defender-attribution.md): the weak-side
    # rim-help block -- same `TerminalKind` as `blocked_miss`, just a
    # different `blocking_defender_id`.
    "blocked_miss_help": TerminalKind.MISSED_FG,
    "shooting_foul": TerminalKind.SHOOTING_FOUL,
    "non_shooting_foul": TerminalKind.NON_SHOOTING_FOUL,
    "turnover_live": TerminalKind.TURNOVER_LIVE,
    "turnover_dead": TerminalKind.TURNOVER_DEAD,
    "offensive_foul": TerminalKind.OFFENSIVE_FOUL,
    # docs/design/turnover-recovery.md TR-4.2: the one identity mapping. The
    # leaf is never a possession's terminal kind (TR-4.12), so it only ever
    # appears below as a disagreement -- the scramble-violation close.
    "loose_ball_recovered": TerminalKind.LOOSE_BALL_RECOVERED,
}


@functools.cache
def _corpus():
    return tuple((seed, _simulate(seed)) for seed in _CORPUS_SEEDS)


def test_corpus_includes_overtime_games():
    assert any(r.overtime_periods > 0 for _seed, r in _corpus())
    assert any(r.overtime_periods == 0 for _seed, r in _corpus())


def test_exactly_the_last_record_of_every_possession_carries_the_terminal():
    possessions_seen = 0
    for seed, result in _corpus():
        groups = _possessions(result.beat_log)
        possessions_seen += len(groups)
        for n, beats in enumerate(groups):
            where = f"seed {seed} possession {n}"
            assert beats[-1].terminal is not None, f"{where}: last record has no terminal"
            for record in beats[:-1]:
                assert record.terminal is None, f"{where}: a non-last {record.beat} has a terminal"
                assert record.possession_credits == (), (
                    f"{where}: a non-last {record.beat} carries credits"
                )
        # Independent count: the box score credits one `possessions` per
        # possession, so it must equal both the groups and the terminals.
        terminals = [b for b in result.beat_log if b.terminal is not None]
        assert len(terminals) == len(groups) == sum(t.possessions for t in result.team_box_lines), (
            f"seed {seed}: terminals, possession groups and box-score possessions disagree"
        )
    assert possessions_seen > 5000


def test_the_payload_can_sit_on_any_of_seven_beat_kinds():
    """The design's warning, as a pinned fact: nothing may assume the payload
    is on the resolution (or rebound) beat. Measured over seeds 0-29: last
    beat is resolution 3657, rebound 2072, setup 20, on_ball 18, help 14,
    rotation 12, screen 7 -- the last five are violation closes. The exact
    observed set is pinned, so a new kind appearing (or one vanishing) is a
    deliberate, test-visible act, not a surprise to a consumer."""
    last_beats = {
        beats[-1].beat for _seed, r in _corpus() for beats in _possessions(r.beat_log)
    }
    assert last_beats == {
        "resolution",
        "rebound",
        "setup",
        "on_ball",
        "help",
        "rotation",
        "screen",
    }


def test_possession_credits_keep_the_order_the_engine_credited_them():
    """`possession_credits` is an ORDERED sequence -- a consumer that applies
    `[terminal, *possession_credits]` (or narrates it in reverse) depends on
    it. The offensive-rebound credits' order is checkable from the
    possession's own beat group: each credited offensive rebound is one
    `rebound` beat whose `outcome` is "offense", in beat order. A reversed (or
    otherwise reordered) tuple fails here."""
    checked = distinguishable = 0
    for seed, result in _corpus():
        for n, beats in enumerate(_possessions(result.beat_log)):
            credits = beats[-1].possession_credits
            if len(credits) < 2:
                continue
            checked += 1
            from_credits = [c.rebounder_id for c in credits if c.rebound_is_offensive]
            from_beats = [
                b.actor_ids["rebounder"]
                for b in beats
                if b.beat == "rebound" and b.outcome == "offense"
            ]
            assert from_credits == from_beats, (
                f"seed {seed} possession {n}: credits' offensive-rebound order "
                f"{from_credits} != the beats' {from_beats}"
            )
            if from_beats != from_beats[::-1]:
                distinguishable += 1
    # Non-vacuous: such possessions exist, and some have an order a reversal
    # would visibly change.
    assert checked > 0
    assert distinguishable > 0


def test_terminal_kind_agrees_with_the_resolution_leaf_except_for_turnover_dead_closes():
    """The engine's terminal is not always the last resolution leaf's own
    outcome, and every disagreement is a `TURNOVER_DEAD`. Three classes, told
    apart by `turnover_player_id` and by what follows the last resolution:

    * `turnover_player_id is None`: the budget-exhausted close --
      `possession.py`'s `_turnover_dead()` after a credit event, which its own
      docstring says "represents no additional real-world fact". Always
      carries credit events (the real fact was credited first).
    * `turnover_player_id` set: a restart violation -- a continuation restarted
      after a credit event and the shot clock then expired before another
      resolution beat (`_tick` -> `_turnover_dead(ball_handler)`, a real,
      attributed turnover). Beats follow the last resolution's own rebound.
    * the scramble-violation close (docs/design/turnover-recovery.md TR-4.14):
      the last resolution is `loose_ball_recovered` and NOTHING follows it --
      the scramble tick itself ran the shot clock out, and the turnover is
      the recoverer's (`_turnover_dead(outcome.recovering_player_id)`).

    Possessions with no resolution beat at all (a pre-resolution shot-clock
    violation) have no leaf to compare and are pinned separately."""
    fabricated = restart = scramble = no_resolution = 0
    for seed, result in _corpus():
        for n, beats in enumerate(_possessions(result.beat_log)):
            where = f"seed {seed} possession {n}"
            terminal = beats[-1].terminal
            resolutions = [b for b in beats if b.beat == "resolution"]
            if not resolutions:
                no_resolution += 1
                assert terminal.kind is TerminalKind.TURNOVER_DEAD, where
                assert terminal.turnover_player_id is not None, where
                continue
            if _LEAF_TO_KIND[resolutions[-1].outcome] is terminal.kind:
                continue
            assert terminal.kind is TerminalKind.TURNOVER_DEAD, (
                f"{where}: leaf {resolutions[-1].outcome!r} vs terminal {terminal.kind}"
            )
            if terminal.turnover_player_id is None:
                fabricated += 1
                assert beats[-1].possession_credits, f"{where}: fabricated close with no credit"
            elif resolutions[-1].outcome == "loose_ball_recovered" and beats[-1] is resolutions[-1]:
                scramble += 1
                recovery = beats[-1].possession_credits[-1]
                assert recovery.kind is TerminalKind.LOOSE_BALL_RECOVERED, where
                assert terminal.turnover_player_id == recovery.recovering_player_id, where
            else:
                restart += 1
                tail = beats[beats.index(resolutions[-1]) + 1 :]
                own_rebound = 1 if tail and tail[0].beat == "rebound" else 0
                assert len(tail) > own_rebound, (
                    f"{where}: attributed TURNOVER_DEAD disagrees with the leaf but no beat "
                    "follows the last resolution's own rebound"
                )
    # Non-vacuous: every class occurs in the corpus (measured over seeds
    # 0-29: 36 fabricated, 45 restart, 26 with no resolution beat; the
    # scramble close is ~0.1 a game over this fixture).
    assert fabricated > 0
    assert restart > 0
    assert scramble > 0
    assert no_resolution > 0


def test_a_loose_ball_recovery_is_never_a_terminal_and_does_occur():
    """TR-4.12's invariant over the real multi-seed corpus (mutations 14 and
    15): no possession ends on `LOOSE_BALL_RECOVERED`, and at least one
    carries it as a credit event, or the first assertion is vacuous."""
    credited = 0
    for seed, result in _corpus():
        for n, beats in enumerate(_possessions(result.beat_log)):
            last = beats[-1]
            assert last.terminal.kind is not TerminalKind.LOOSE_BALL_RECOVERED, (
                f"seed {seed} possession {n}"
            )
            credited += sum(
                1 for c in last.possession_credits if c.kind is TerminalKind.LOOSE_BALL_RECOVERED
            )
    assert credited > 0


def test_only_a_turnover_ever_carries_turnover_player_id():
    """TR-4.11 / mutation 18: a field called `turnover_player_id` on an event
    that is not a turnover is a phantom turnover waiting for the next reader
    who forgets a kind gate. The recovery carries its loser in
    `loose_ball_player_id` instead; this pins that for every kind, terminal
    and credit alike."""
    turnover_kinds = (TerminalKind.TURNOVER_LIVE, TerminalKind.TURNOVER_DEAD)
    checked_recoveries = 0
    for seed, result in _corpus():
        for beats in _possessions(result.beat_log):
            last = beats[-1]
            for event in (last.terminal, *last.possession_credits):
                if event.kind not in turnover_kinds:
                    assert event.turnover_player_id is None, (seed, event)
                if event.kind is TerminalKind.LOOSE_BALL_RECOVERED:
                    checked_recoveries += 1
                    assert event.loose_ball_player_id is not None, (seed, event)
                    assert event.recovering_player_id is not None, (seed, event)
    assert checked_recoveries > 0


def test_empty_beat_log_is_rejected():
    """`_finish` cannot reach this branch with a real possession
    (`setup.record` is appended before every early return), so the stamp
    helper it delegates to is called directly."""
    with pytest.raises(ValueError, match="empty beat log"):
        _stamp_possession_records(
            [],
            outcome=TerminalOutcome(kind=TerminalKind.TURNOVER_DEAD),
            credit_events=[],
            period=1,
            home_score_after=0,
            away_score_after=0,
            game_clock_seconds=720.0,
        )


def test_terminal_and_credits_replayed_through_the_box_accumulator_reproduce_the_box_score():
    """The payload is complete: feeding each possession's last-record
    `terminal` + `possession_credits` to `boxscore.accumulate_game` (the
    engine's own rules, incl. the personal-foul cap) reproduces every
    counting stat in `player_box_lines` and `team_box_lines`. Minutes and
    plus-minus need lineups the beat log does not carry and are excluded."""
    sample = _corpus()[0][1]
    player_counting = [
        f.name
        for f in dataclasses.fields(type(sample.player_box_lines[0]))
        if f.name not in ("player_id", "team_id", "minutes_played", "plus_minus")
    ]
    team_counting = [
        f.name for f in dataclasses.fields(type(sample.team_box_lines[0])) if f.name != "team_id"
    ]
    assert "possessions" in team_counting
    ctx = make_game_context(_GAME_ID, tuning=DEFAULT_TUNING)
    player_team = {p.id: ctx.home.id for p in ctx.home.players}
    player_team.update({p.id: ctx.away.id for p in ctx.away.players})

    for seed, result in _corpus():
        records = []
        for n, beats in enumerate(_possessions(result.beat_log)):
            last = beats[-1]
            offense_is_home = n % 2 == 0  # game.py: possession_index % 2 == 0
            records.append(
                PossessionRecord(
                    offense_team_id=_HOME_TEAM_ID if offense_is_home else _AWAY_TEAM_ID,
                    defense_team_id=_AWAY_TEAM_ID if offense_is_home else _HOME_TEAM_ID,
                    offense_lineup=(),
                    defense_lineup=(),
                    elapsed_seconds=0.0,
                    result=PossessionResult(
                        terminal=last.terminal, credit_events=list(last.possession_credits)
                    ),
                )
            )
        players, teams = accumulate_game(
            records,
            player_team=player_team,
            home_team_id=_HOME_TEAM_ID,
            away_team_id=_AWAY_TEAM_ID,
        )
        replayed_players = {p.player_id: p for p in players}
        for line in result.player_box_lines:
            replay = replayed_players.get(line.player_id)
            for name in player_counting:
                got = getattr(replay, name) if replay is not None else 0
                assert got == getattr(line, name), (
                    f"seed {seed} player {line.player_id} {name}: replay {got} "
                    f"!= box {getattr(line, name)}"
                )
        # A replayed player with no line in the box score would be a phantom.
        assert set(replayed_players) <= {p.player_id for p in result.player_box_lines}
        replayed_teams = {t.team_id: t for t in teams}
        for line in result.team_box_lines:
            for name in team_counting:
                assert getattr(replayed_teams[line.team_id], name) == getattr(line, name), (
                    f"seed {seed} team {line.team_id} {name}"
                )


def test_a_loose_ball_recovery_adds_nothing_to_any_box_line():
    """docs/design/turnover-recovery.md TR-2/TR-4.3 (mutations 6, 7 and 13):
    a recovered loose ball is not a turnover, not a steal and not a point.
    The replay above cannot see that -- the engine and the replay both run
    the same accumulator, so a recovery that wrongly credited `tov`, `stl` or
    `ftm` would move both sides together. This replays every game of the
    corpus twice, once as recorded and once with every
    `LOOSE_BALL_RECOVERED` credit removed, and the two box scores must be
    identical in every counting stat. Non-vacuous: recoveries must exist."""
    ctx = make_game_context(_GAME_ID, tuning=DEFAULT_TUNING)
    player_team = {p.id: ctx.home.id for p in ctx.home.players}
    player_team.update({p.id: ctx.away.id for p in ctx.away.players})

    def _replay(result, *, strip_recoveries: bool):
        records = []
        for n, beats in enumerate(_possessions(result.beat_log)):
            last = beats[-1]
            credits = [
                c
                for c in last.possession_credits
                if not (strip_recoveries and c.kind is TerminalKind.LOOSE_BALL_RECOVERED)
            ]
            offense_is_home = n % 2 == 0
            records.append(
                PossessionRecord(
                    offense_team_id=_HOME_TEAM_ID if offense_is_home else _AWAY_TEAM_ID,
                    defense_team_id=_AWAY_TEAM_ID if offense_is_home else _HOME_TEAM_ID,
                    offense_lineup=(),
                    defense_lineup=(),
                    elapsed_seconds=0.0,
                    result=PossessionResult(terminal=last.terminal, credit_events=credits),
                )
            )
        return accumulate_game(
            records,
            player_team=player_team,
            home_team_id=_HOME_TEAM_ID,
            away_team_id=_AWAY_TEAM_ID,
        )

    recoveries = 0
    for seed, result in _corpus():
        recoveries += sum(
            1
            for b in result.beat_log
            for c in b.possession_credits
            if c.kind is TerminalKind.LOOSE_BALL_RECOVERED
        )
        assert _replay(result, strip_recoveries=False) == _replay(
            result, strip_recoveries=True
        ), f"seed {seed}: a LOOSE_BALL_RECOVERED credit changed the box score"
    assert recoveries > 0
