"""Tier 2: invariants over 10,000 seeded possessions (§12 step 4's own
verify bullets) -- exactly one terminal outcome per possession, the
shared continuation budget never exceeds its configured cap, the shot
clock never goes negative, a shot-clock violation always resolves to
`TURNOVER_DEAD`, and closed-outcome exhaustiveness over the
terminal-eligible members."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace

import dba2.sim.possession as possession_mod
from dba2.core.rng import rng_for
from dba2.domain.player import Position
from dba2.domain.team import DefensiveScheme
from dba2.sim.events import TerminalKind, Zone
from dba2.sim.playbook import ActionType
from dba2.sim.possession import simulate_possession
from dba2.sim.state import GameState, new_possession_state
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_sim_player

_POSITIONS = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)
# docs/design/turnover-recovery.md TR-5.B.2: two sets, not one. The single
# `frozenset(TerminalKind) - {PERIOD_EXPIRED}` this used to be auto-widened
# to admit `LOOSE_BALL_RECOVERED` as a legal terminal the moment the member
# was added, which silently cost the 10,000-possession loop below its
# ability to catch a recovery escaping as a possession's end.
#
# `PERIOD_EXPIRED` is in neither: no production code constructs it at all
# (TR-1.12, `game.py`'s module docstring). `LOOSE_BALL_RECOVERED` is a
# legal credit event but never a terminal (TR-4.12) -- the recovery leaf is
# only offered while the continuation budget has room, so every drawn
# recovery is followed by another beat.
_POSSESSION_TERMINAL_KINDS = frozenset(TerminalKind) - {
    TerminalKind.PERIOD_EXPIRED,
    TerminalKind.LOOSE_BALL_RECOVERED,
}
_CREDIT_EVENT_KINDS = frozenset(TerminalKind) - {TerminalKind.PERIOD_EXPIRED}


def _lineup(base_id: int, team_id: int) -> tuple:
    return tuple(
        make_sim_player(
            base_id + i,
            tuning=DEFAULT_TUNING,
            primary_position=_POSITIONS[i],
            height_in=74 + i * 3,
            team_id=team_id,
        )
        for i in range(5)
    )


def _run_possession(offense, defense, tuning, seed, game_state, *, offense_is_home=True):
    rng = rng_for(20260821, seed)
    return simulate_possession(
        offense,
        defense,
        tuning=tuning,
        rng=rng,
        game_state=game_state,
        home_score=0,
        away_score=0,
        offense_is_home=offense_is_home,
        defense_help_aggression=50,
        defense_defensive_scheme=DefensiveScheme.DROP,
        crash_oreb_bias=50,
    )


def test_ten_thousand_seeded_possessions_satisfy_the_closed_set_invariants():
    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    game_state = GameState()

    saw_shot_clock_violation = False
    saw_continuation = False
    recoveries = 0
    for i in range(10_000):
        if i % 100 == 0:
            # Mimic a period boundary so the bonus path (and therefore a
            # terminal NON_SHOOTING_FOUL, not just a continuation one)
            # gets real coverage across the run, rather than every
            # possession starting from a permanently-fresh foul count.
            game_state.reset_team_fouls_for_new_period()
        sim_result = _run_possession(offense, defense, DEFAULT_TUNING, i, game_state)

        terminal = sim_result.result.terminal
        assert terminal.kind in _POSSESSION_TERMINAL_KINDS
        for event in sim_result.result.credit_events:
            assert event.kind in _CREDIT_EVENT_KINDS
            if event.kind is TerminalKind.LOOSE_BALL_RECOVERED:
                recoveries += 1

        budget_max = DEFAULT_TUNING.possession.continuation_budget_max
        assert 0 <= sim_result.continuation_uses <= budget_max
        if sim_result.continuation_uses > 0:
            saw_continuation = True

        assert sim_result.elapsed_seconds >= 0.0

        if terminal.kind is TerminalKind.TURNOVER_DEAD:
            saw_shot_clock_violation = True

    # Population-level sanity: both paths this test exists to bound are
    # actually exercised, not vacuously true over an all-uniform sample.
    assert saw_shot_clock_violation
    assert saw_continuation
    # TR-4.12's non-vacuity: the never-terminal assertion above is only
    # worth something if recoveries actually happened in this sample.
    assert recoveries > 0


def test_shot_clock_violation_always_resolves_to_turnover_dead():
    """Direct check of the one-directional implication (a violation is
    always `TURNOVER_DEAD`; the converse -- a bad-pass `TURNOVER_DEAD`
    unrelated to the clock -- is not claimed). An artificially tiny shot
    clock forces a violation on effectively every possession.

    B25-1 fix (critic-round-25-proven) regression: a genuine shot-clock
    violation must also carry a real `turnover_player_id` now -- the old
    code left it `None`, which made `boxscore.py::_apply_outcome` silently
    skip crediting the turnover (see `test_shot_clock_violation_credits_a_
    real_turnover_in_the_box_score` below for the box-score-level proof)."""
    tiny_clock_tuning = replace(
        DEFAULT_TUNING, possession=replace(DEFAULT_TUNING.possession, shot_clock_seconds=0.01)
    )
    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    offense_ids = {p.id for p in offense}
    for i in range(200):
        game_state = GameState()
        sim_result = _run_possession(offense, defense, tiny_clock_tuning, i, game_state)
        terminal = sim_result.result.terminal
        assert terminal.kind is TerminalKind.TURNOVER_DEAD
        assert terminal.turnover_player_id is not None, (
            "B25-1 regression: a shot-clock-violation TURNOVER_DEAD must attribute "
            "a real player, or it silently vanishes from the box score"
        )
        assert terminal.turnover_player_id in offense_ids


def test_shot_clock_violation_credits_a_real_turnover_in_the_box_score():
    """B25-1 fix (critic-round-25-proven): before this fix, every shot-
    clock-violation possession produced a `TURNOVER_DEAD` with no
    `turnover_player_id`, and `boxscore.py::_apply_outcome` only credits a
    `TURNOVER_DEAD`'s `tov` when that field is not `None` -- so ~13.85% of
    possessions (measured, at the step-11 pace constants) vanished from the
    box score entirely: no FGA, no FTA, no turnover, nothing, and
    `tov_per_100_poss` silently understated the true rate. This runs a
    forced-violation possession through the real accumulator
    (`boxscore.accumulate_game`, not just `simulate_possession` in
    isolation) and asserts the turnover is now visible end to end -- team
    `tov` increments, exactly one player's `tov` increments, and team
    `possessions` still increments (the possession itself is not lost, only
    the turnover attribution was)."""
    from dba2.sim.boxscore import PossessionRecord, accumulate_game

    tiny_clock_tuning = replace(
        DEFAULT_TUNING, possession=replace(DEFAULT_TUNING.possession, shot_clock_seconds=0.01)
    )
    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    game_state = GameState()
    sim_result = _run_possession(offense, defense, tiny_clock_tuning, 0, game_state)
    assert sim_result.result.terminal.kind is TerminalKind.TURNOVER_DEAD
    assert sim_result.result.terminal.turnover_player_id is not None

    player_team = {p.id: p.player.team_id for p in (*offense, *defense)}
    record = PossessionRecord(
        offense_team_id=1,
        defense_team_id=2,
        offense_lineup=tuple(p.id for p in offense),
        defense_lineup=tuple(p.id for p in defense),
        elapsed_seconds=sim_result.elapsed_seconds,
        result=sim_result.result,
    )
    player_lines, team_lines = accumulate_game(
        [record], player_team=player_team, home_team_id=1, away_team_id=2
    )

    offense_team_line = next(t for t in team_lines if t.team_id == 1)
    assert offense_team_line.possessions == 1
    assert offense_team_line.tov == 1, (
        "shot-clock-violation turnover never reached the team box line -- "
        "the B25-1 fix regressed or boxscore.py's TURNOVER_DEAD handling changed"
    )
    credited_players = [p for p in player_lines if p.tov == 1]
    assert len(credited_players) == 1
    assert credited_players[0].player_id == sim_result.result.terminal.turnover_player_id


def test_possession_state_shot_clock_never_goes_negative():
    ps = new_possession_state(DEFAULT_TUNING)
    ps.tick(1_000.0)
    assert ps.shot_clock_seconds == 0.0
    assert ps.is_shot_clock_violation


def test_possession_state_continuation_budget_caps_at_the_configured_max():
    ps = new_possession_state(DEFAULT_TUNING)
    for _ in range(DEFAULT_TUNING.possession.continuation_budget_max):
        assert ps.can_continue(DEFAULT_TUNING)
        ps.use_continuation()
    assert not ps.can_continue(DEFAULT_TUNING)


def test_offensive_rebound_resets_shot_clock_but_not_continuation_budget():
    ps = new_possession_state(DEFAULT_TUNING)
    ps.tick(20.0)
    ps.use_continuation()
    before_uses = ps.continuation_uses
    ps.reset_on_offensive_rebound(DEFAULT_TUNING)
    assert ps.shot_clock_seconds == DEFAULT_TUNING.possession.offensive_rebound_shot_clock_reset
    assert ps.continuation_uses == before_uses


def test_out_of_position_defender_id_is_wired_from_a_beaten_helper_and_affects_rebounds():
    """B7 fix (critic-round-7-proven): `possession.py:317` used to hardcode
    `out_of_position_defender_id=None` at its only production call site
    even though `helper` (the beat-3 defender who committed to help and,
    on the `KICKOUT_OPEN`/`ROLL_LIVE`/no-vacated-target paths, got beaten)
    was already in scope -- leaving `tuning.rebound_modifiers.
    out_of_position_defender_penalty` and its `rebound.py` branch dead in
    the live engine, with only isolated `test_rebound.py` coverage that
    never went through a real possession.

    Two things, both required by the fix-round brief: (1) the field is
    genuinely populated by a real possession, not just by
    `test_rebound.py`'s isolated call, and with the *correct* player (the
    same one the beat log's "help" `BeatRecord` names); (2) it is
    observable in the resulting rebound outcome, not merely passed and
    ignored -- proven by replaying the identical possession stream twice,
    once under the real penalty and once with it neutralized to 1.0, and
    showing at least one possession's defensive rebounder differs. Because
    `weighted_choice` draws exactly one RNG value regardless of the
    weights it's given, both runs consume `rng_for` identically up to the
    rebound draw itself -- so any difference in the winning rebounder is
    attributable only to the penalty parameter, not seed drift."""
    offense = _lineup(1, 1)
    defense = _lineup(101, 2)

    # Bias the whole defense toward helping (Decision P1-16's
    # `help_gamble_freq`, plus real help-commit ability) and the offense
    # toward `pnr_ball` (Decision P1-8's `pnr_ball_freq`) so the
    # HELP_COMMITTED/HELP_LATE -> beat 4 path this fix targets is actually
    # reached often across a few hundred seeds, without forcing any one
    # possession's outcome by hand.
    offense = tuple(
        replace(
            p,
            tendencies=replace(p.tendencies, pnr_ball_freq=90, shot_freq_rim=70),
        )
        for p in offense
    )
    defense = tuple(
        replace(p, tendencies=replace(p.tendencies, help_gamble_freq=90)) for p in defense
    )

    def _captured_penalty_run(tuning, seeds):
        captures: dict[int, list[int | None]] = {}
        beat_logs: dict[int, tuple] = {}
        orig = possession_mod.resolve_rebound_beat

        def _capturing(*args, **kwargs):
            captures.setdefault(current_seed[0], []).append(
                kwargs.get("out_of_position_defender_id")
            )
            return orig(*args, **kwargs)

        current_seed = [None]
        possession_mod.resolve_rebound_beat = _capturing
        try:
            for seed in seeds:
                current_seed[0] = seed
                game_state = GameState()
                sim_result = _run_possession(offense, defense, tuning, seed, game_state)
                beat_logs[seed] = sim_result.beat_log
        finally:
            possession_mod.resolve_rebound_beat = orig
        return captures, beat_logs

    seeds = range(400)
    real_captures, real_beat_logs = _captured_penalty_run(DEFAULT_TUNING, seeds)

    nonneutral_seeds = [
        s for s, calls in real_captures.items() if any(c is not None for c in calls)
    ]
    assert nonneutral_seeds, (
        "no possession across 400 seeds ever populated out_of_position_defender_id -- "
        "the wiring at possession.py's HELP_COMMITTED/HELP_LATE call sites isn't firing"
    )

    # Correctness: every non-None capture must name the player the *most
    # recently preceding* "help" `BeatRecord` in that same possession's
    # beat log recorded as the helper -- proving this is the real beat-3
    # defender for the specific shot this rebound followed, not an
    # arbitrary id or a stale one left over from an earlier secondary
    # read in the same possession (a possession can loop through beat 3
    # more than once via Decision P1-11's continuation budget, so
    # matching against "the log's last help beat" globally would be
    # wrong whenever more than one rebound-needing shot occurred).
    for seed in nonneutral_seeds:
        beat_log = real_beat_logs[seed]
        most_recent_helper_id: int | None = None
        rebound_index = 0
        for record in beat_log:
            if record.beat == "help":
                most_recent_helper_id = record.actor_ids["helper"]
            elif record.beat == "rebound":
                captured_id = real_captures[seed][rebound_index]
                rebound_index += 1
                if captured_id is not None:
                    assert captured_id == most_recent_helper_id, (
                        f"seed {seed}, rebound #{rebound_index}: captured "
                        f"out_of_position_defender_id={captured_id} does not match the "
                        f"most recently preceding help beat's helper={most_recent_helper_id}"
                    )
        assert rebound_index == len(real_captures[seed]), (
            f"seed {seed}: {len(real_captures[seed])} resolve_rebound_beat calls but only "
            f"{rebound_index} 'rebound' BeatRecords in the log -- capture/log accounting drifted"
        )

    # Observability: replay the exact same seeds with the penalty
    # neutralized and confirm at least one captured rebound's actual
    # winner changes.
    neutral_tuning = replace(
        DEFAULT_TUNING,
        rebound_modifiers=replace(
            DEFAULT_TUNING.rebound_modifiers, out_of_position_defender_penalty=1.0
        ),
    )
    saw_a_difference = False
    for seed in nonneutral_seeds:
        game_state = GameState()
        real_result = _run_possession(offense, defense, DEFAULT_TUNING, seed, game_state)
        game_state_neutral = GameState()
        neutral_result = _run_possession(offense, defense, neutral_tuning, seed, game_state_neutral)
        real_rebounders = [
            e.rebounder_id
            for e in (real_result.result.terminal, *real_result.result.credit_events)
            if e.rebounder_id is not None
        ]
        neutral_rebounders = [
            e.rebounder_id
            for e in (neutral_result.result.terminal, *neutral_result.result.credit_events)
            if e.rebounder_id is not None
        ]
        if real_rebounders != neutral_rebounders:
            saw_a_difference = True
            break
    assert saw_a_difference, (
        "neutralizing out_of_position_defender_penalty never changed a single rebound "
        "outcome across every seed that populated the field -- the parameter isn't "
        "actually load-bearing for the resulting rebound stats"
    )


def test_out_of_position_defender_id_fires_on_no_pass_kickout_contested_and_roll_tagged():
    """B10 fix (critic-round-8-proven): the original B7 wiring populated
    `out_of_position_defender_id` only on the 3 `RotationOutcome` branches
    where the helper's own contest/tag *failed* (`KICKOUT_OPEN`,
    `ROLL_LIVE`, the no-vacated-target fallback), explicitly excluding
    `NO_PASS`/`KICKOUT_CONTESTED`/`ROLL_TAGGED` on the rationale that "the
    helper's contest/tag succeeded there." Critic proved that's the wrong
    qualifier -- §4.4/§4.6 and `rebound.py`'s own docstring both condition
    on *commitment*, not outcome, and `NO_PASS` in particular never even
    has the helper contest anything. This test targets exactly the 3
    branches B10 newly wires and confirms each one, independently, now
    populates `out_of_position_defender_id` with the correct helper at
    least once across a real possession stream -- not just that *some*
    branch does (the pre-existing test above already covered the
    pre-B10 subset). Correlation is by call order within one possession
    (each `resolve_rebound_beat` call is immediately preceded, in the same
    trip, by the `resolve_rotation_beat` call whose outcome decided the
    shot it's rebounding -- no other beat sits between them on this
    branch), the same "adjacent in the same possession's call sequence"
    shape the pre-existing test uses via the beat log."""
    from dba2.sim.beats.rotation import RotationOutcome
    from dba2.sim.beats.rotation import resolve_rotation_beat as orig_rotation
    from dba2.sim.beats.setup import resolve_setup_beat as orig_setup

    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    offense = tuple(
        replace(
            p,
            tendencies=replace(p.tendencies, pnr_ball_freq=90, shot_freq_rim=70),
        )
        for p in offense
    )
    defense = tuple(
        replace(p, tendencies=replace(p.tendencies, help_gamble_freq=90)) for p in defense
    )

    targets = (
        RotationOutcome.NO_PASS,
        RotationOutcome.KICKOUT_CONTESTED,
        RotationOutcome.ROLL_TAGGED,
    )
    seen_wired_for: dict[RotationOutcome, bool] = dict.fromkeys(targets, False)

    orig_rebound = possession_mod.resolve_rebound_beat
    pending_outcome: list[RotationOutcome | None] = [None]

    def _setup_spy(*args, **kwargs):
        # Every trip through the chain loop (a fresh possession attempt or
        # a continuation) starts here -- clearing `pending_outcome` at
        # this boundary means a rebound that follows a trip with no
        # rotation-beat call at all (no-helper-assigned, CONTAINED,
        # DEFENDER_WINS, HELP_STAYED, and the no-vacated-target fallback,
        # none of which call `resolve_rotation_beat`) can never be
        # misattributed to a stale outcome left over from an earlier trip.
        pending_outcome[0] = None
        return orig_setup(*args, **kwargs)

    def _rotation_spy(*args, **kwargs):
        result = orig_rotation(*args, **kwargs)
        pending_outcome[0] = result.outcome
        return result

    def _rebound_spy(*args, **kwargs):
        outcome = pending_outcome[0]
        captured = kwargs.get("out_of_position_defender_id")
        if outcome in seen_wired_for and captured is not None:
            seen_wired_for[outcome] = True
        pending_outcome[0] = None  # this trip's rebound is spent
        return orig_rebound(*args, **kwargs)

    possession_mod.resolve_setup_beat = _setup_spy
    possession_mod.resolve_rotation_beat = _rotation_spy
    possession_mod.resolve_rebound_beat = _rebound_spy
    try:
        for seed in range(1500):
            if all(seen_wired_for.values()):
                break
            pending_outcome[0] = None
            game_state = GameState()
            _run_possession(offense, defense, DEFAULT_TUNING, seed, game_state)
    finally:
        possession_mod.resolve_setup_beat = orig_setup
        possession_mod.resolve_rotation_beat = orig_rotation
        possession_mod.resolve_rebound_beat = orig_rebound

    for outcome, wired in seen_wired_for.items():
        assert wired, (
            f"{outcome} never immediately preceded a populated "
            "out_of_position_defender_id across 1500 seeds -- B10's widened wiring "
            "isn't actually reaching this branch"
        )


def test_possession_replay_determinism_from_seed_index_and_entering_state():
    """B5 fix: §6's second named determinism claim, verbatim: "a
    possession replayed standalone from `(games.rng_seed, possession_index,
    entering game state)` reproduces exactly." Builds a non-trivial
    entering `GameState` (mid-period, with real foul history -- not a
    freshly-constructed default, which would trivially pass) twice
    independently, replays the identical `(seed, possession_index)` pair
    against each, and asserts the two results -- including `beat_log`
    (`BeatRecord` is a frozen dataclass, so this is a structural, not
    identity, comparison) and every mutation `game_state` picked up --
    are exactly equal."""

    def _entering_state() -> GameState:
        gs = GameState(period=2, clock_seconds=430.0)
        gs.record_team_foul(2)
        gs.record_team_foul(2)
        gs.record_player_foul(101, DEFAULT_TUNING)
        return gs

    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    seed = 90210
    possession_index = 37

    outcomes = []
    final_states = []
    for _ in range(2):
        game_state = _entering_state()
        rng = rng_for(seed, possession_index)
        sim_result = simulate_possession(
            offense,
            defense,
            tuning=DEFAULT_TUNING,
            rng=rng,
            game_state=game_state,
            home_score=10,
            away_score=8,
            offense_is_home=True,
            defense_help_aggression=50,
            defense_defensive_scheme=DefensiveScheme.DROP,
            crash_oreb_bias=50,
        )
        outcomes.append(sim_result)
        final_states.append(game_state)

    assert outcomes[0].result == outcomes[1].result
    assert outcomes[0].beat_log == outcomes[1].beat_log
    assert outcomes[0].elapsed_seconds == outcomes[1].elapsed_seconds
    assert outcomes[0].continuation_uses == outcomes[1].continuation_uses
    assert final_states[0].team_fouls_this_period == final_states[1].team_fouls_this_period
    assert final_states[0].players.keys() == final_states[1].players.keys()
    for player_id in final_states[0].players:
        assert final_states[0].players[player_id] == final_states[1].players[player_id]


def test_rim_help_reaches_the_best_rim_protector_not_only_the_positional_matchup():
    """BDA-4.1 (docs/design/block-defender-attribution.md), kept under
    block-attribution-v2's pool: the weak-side rim help comes from the
    floor, not from whoever the offense's shooter happens to be positionally
    matched against -- an elite rim protector fixed at a *guard* position
    (never the primary matchup defender for a big man's shot) must still
    turn up as a `blocking_defender_id` across a seeded sample, which the
    pre-BDA-4 rigid positional-matchup routing (BDA-2) could never produce."""
    offense = tuple(
        make_sim_player(
            base_id,
            tuning=DEFAULT_TUNING,
            primary_position=pos,
            height_in=height,
            team_id=1,
            tendencies_overrides={
                "shot_freq_rim": 100,
                "shot_freq_mid": 0,
                "shot_freq_three": 0,
                "shot_freq_post": 0,
            },
        )
        for base_id, pos, height in zip(range(1, 6), _POSITIONS, (74, 77, 79, 82, 85), strict=True)
    )
    elite = make_sim_player(
        101,
        tuning=DEFAULT_TUNING,
        primary_position=Position.PG,
        height_in=73,
        team_id=2,
        ratings_overrides={"rim_protection": 97, "vertical": 95, "lateral_quickness": 90},
        wingspan_in=90,
    )
    defense_rest = tuple(
        make_sim_player(
            base_id, tuning=DEFAULT_TUNING, primary_position=pos, height_in=height, team_id=2
        )
        for base_id, pos, height in zip(
            range(102, 106), _POSITIONS[1:], (76, 80, 83, 86), strict=True
        )
    )
    defense = (elite, *defense_rest)

    game_state = GameState()
    seen_blocking_ids: set[int] = set()
    for seed in range(1500):
        sim_result = _run_possession(offense, defense, DEFAULT_TUNING, seed, game_state)
        for event in (sim_result.result.terminal, *sim_result.result.credit_events):
            if event.blocking_defender_id is not None:
                seen_blocking_ids.add(event.blocking_defender_id)

    assert elite.id in seen_blocking_ids, (
        "the floor's best rim protector, fixed at a guard position never matched "
        "against a big man's shot, never once registered a block across 1500 "
        "possessions -- the weak-side rim-help mechanism isn't reaching him"
    )


def _spy_rim_help_pools(monkeypatch) -> list[tuple[int, tuple[int, ...]]]:
    """Wraps the resolution beat `possession.py` calls and records, per
    shot, `(primary defender id, rim-help pool ids)` -- consumes no RNG."""
    seen: list[tuple[int, tuple[int, ...]]] = []
    original = possession_mod.resolve_resolution_beat

    def spy(shooter, defender, **kwargs):
        seen.append((defender.id, tuple(p.id for p in kwargs["rim_help_pool"])))
        return original(shooter, defender, **kwargs)

    monkeypatch.setattr(possession_mod, "resolve_resolution_beat", spy)
    return seen


def test_rim_help_pool_excludes_the_on_ball_defender(monkeypatch):
    """BDA-4.4 gate 4 under block-attribution-v2's pool (BA-2.6): when the
    floor's best rim protector is the on-ball defender for this shot
    (attached to the ball-handler, not rotating in from the weak side), he
    is never in the rim-help pool -- not just when he happens to also be the
    shot's own primary contesting `defender` (gate 3, checked in
    `test_resolution.py`), but also on the `HELP_COMMITTED`/`HELP_LATE`
    branches where a *different* defender (`helper`) contests the shot while
    the elite defender stays attached to the ball-handler. The other four
    defenders still can help (pre-v2, gate 4 nulled the whole contest).
    Restricted to `iso`/`transition` (never `pnr_ball`/`dho`) so a screen
    `SWITCH` can never reassign who the on-ball defender is mid-possession."""
    shared_tendencies = {
        "iso_freq": 90,
        "pnr_ball_freq": 5,
        "transition_freq": 90,
        "shot_freq_rim": 100,
        "shot_freq_mid": 0,
        "shot_freq_three": 0,
        "shot_freq_post": 0,
    }
    offense_ball_handler = make_sim_player(
        1,
        tuning=DEFAULT_TUNING,
        primary_position=Position.PG,
        height_in=74,
        team_id=1,
        touch_share="0.97",
        tendencies_overrides=shared_tendencies,
    )
    offense_rest = tuple(
        make_sim_player(
            base_id,
            tuning=DEFAULT_TUNING,
            primary_position=pos,
            height_in=height,
            team_id=1,
            touch_share="0.01",
            tendencies_overrides=shared_tendencies,
        )
        for base_id, pos, height in zip(range(2, 6), _POSITIONS[1:], (77, 79, 82, 85), strict=True)
    )
    offense = (offense_ball_handler, *offense_rest)

    elite = make_sim_player(
        101,
        tuning=DEFAULT_TUNING,
        primary_position=Position.PG,
        height_in=73,
        team_id=2,
        ratings_overrides={"rim_protection": 97, "vertical": 95, "lateral_quickness": 90},
        wingspan_in=90,
    )
    defense_rest = tuple(
        make_sim_player(
            base_id, tuning=DEFAULT_TUNING, primary_position=pos, height_in=height, team_id=2
        )
        for base_id, pos, height in zip(
            range(102, 106), _POSITIONS[1:], (76, 80, 83, 86), strict=True
        )
    )
    defense = (elite, *defense_rest)

    pools = _spy_rim_help_pools(monkeypatch)
    game_state = GameState()
    checked_shots = 0
    help_leaves_without_him = 0
    for seed in range(2000):
        pools.clear()
        sim_result = _run_possession(offense, defense, DEFAULT_TUNING, seed, game_state)
        setup_records = [r for r in sim_result.beat_log if r.beat == "setup"]
        resolution_records = [r for r in sim_result.beat_log if r.beat == "resolution"]
        if len(setup_records) != 1 or not resolution_records:
            # A continuation reselected a fresh action/ball-handler this
            # fixture doesn't control for -- skip rather than assert
            # against a possession shape this test isn't built to cover.
            continue
        setup_record = setup_records[0]
        if setup_record.actor_ids.get("offense:ball_handler") != offense_ball_handler.id:
            continue
        if setup_record.outcome not in {"iso", "transition"}:
            continue
        for (primary_id, pool_ids), record in zip(pools, resolution_records, strict=True):
            checked_shots += 1
            assert elite.id not in pool_ids, (
                "the floor's best rim protector is this shot's on-ball defender and must "
                f"never be offered as a weak-side helper, got pool {pool_ids}"
            )
            assert set(pool_ids) == {p.id for p in defense} - {elite.id}
            assert record.actor_ids.get("blocked_by") != elite.id
            if record.outcome == "blocked_miss_help":
                help_leaves_without_him += 1
                assert record.actor_ids["blocked_by"] not in {elite.id, primary_id}

    assert checked_shots > 0, (
        "fixture never produced a single-attempt iso/transition possession with the "
        "designated ball handler on offense -- test setup is broken, not the gate "
        "it's meant to check"
    )
    assert help_leaves_without_him > 0, "the rest of the floor never helped at the rim"


def test_the_possessions_committed_helper_rides_along_as_rim_help_committed_id(monkeypatch):
    """BDA-4.4 gate 5 per member (block-attribution-v2 BA-2.6): the
    possession's out-of-position committed helper -- the help beat's own
    `helper` -- is handed to the resolution beat as `rim_help_committed_id`
    (penalized there in share and contest). Never an id the possession's help
    beat did not name, and actually threaded on some shots."""
    seen: list[int | None] = []
    original = possession_mod.resolve_resolution_beat

    def spy(shooter, defender, **kwargs):
        seen.append(kwargs["rim_help_committed_id"])
        return original(shooter, defender, **kwargs)

    monkeypatch.setattr(possession_mod, "resolve_resolution_beat", spy)
    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    game_state = GameState()
    threaded = 0
    for seed in range(600):
        seen.clear()
        sim_result = _run_possession(offense, defense, DEFAULT_TUNING, seed, game_state)
        helpers = {r.actor_ids["helper"] for r in sim_result.beat_log if r.beat == "help"}
        for committed_id in seen:
            if committed_id is not None:
                threaded += 1
                assert committed_id in helpers, (seed, committed_id, helpers)
    assert threaded > 0


def _help_blocker_counts(monkeypatch, defense, seeds: int, *, eligible: set[int]) -> Counter:
    """Help blocks by blocker over `seeds` possessions against a rim-only
    offense whose point guard takes nearly every shot, counted only on shots
    where every id in `eligible` was offered as a helper (in the pool and not
    the shot's primary defender) -- the split among helpers who were all
    there to be sent."""
    rim_only = {
        "shot_freq_rim": 100,
        "shot_freq_mid": 0,
        "shot_freq_three": 0,
        "shot_freq_post": 0,
        "iso_freq": 90,
    }
    offense = tuple(
        make_sim_player(
            base_id,
            tuning=DEFAULT_TUNING,
            primary_position=pos,
            height_in=height,
            team_id=1,
            touch_share="0.96" if base_id == 1 else "0.01",
            tendencies_overrides=rim_only,
        )
        for base_id, pos, height in zip(range(1, 6), _POSITIONS, (74, 77, 79, 82, 85), strict=True)
    )
    pools = _spy_rim_help_pools(monkeypatch)
    counts: Counter = Counter()
    game_state = GameState()
    for seed in range(seeds):
        pools.clear()
        sim_result = _run_possession(offense, defense, DEFAULT_TUNING, seed, game_state)
        records = [r for r in sim_result.beat_log if r.beat == "resolution"]
        for (primary_id, pool_ids), record in zip(pools, records, strict=True):
            offered = set(pool_ids) - {primary_id}
            if record.outcome == "blocked_miss_help" and eligible <= offered:
                counts[record.actor_ids["blocked_by"]] += 1
    return counts


def _shot_blocker(pid: int, pos: Position, height: int, level: int):
    return make_sim_player(
        pid,
        tuning=DEFAULT_TUNING,
        primary_position=pos,
        height_in=height,
        team_id=2,
        ratings_overrides={
            "rim_protection": level,
            "vertical": level,
            "rotation_speed": level,
            "help_iq": level,
        },
        wingspan_in=height + 7 if level >= 90 else height + 1,
    )


def test_second_big_on_the_floor_gets_a_share_of_help_blocks(monkeypatch):
    """BA-2.1/BA-4.2: attribution is a concentrated share, not a `max`. Two
    equal elite bigs on one floor split the weak-side blocks (neither takes
    more than 80% -- Kornet beside Wembanyama), while a lone elite big with
    four weak helpers takes at least 90% of them (the concentration is steep
    enough that guards essentially never get a weak-side block)."""
    two_bigs = (
        _shot_blocker(101, Position.PG, 74, 55),
        _shot_blocker(102, Position.SG, 77, 55),
        _shot_blocker(103, Position.SF, 79, 55),
        _shot_blocker(104, Position.PF, 83, 92),
        _shot_blocker(105, Position.C, 85, 92),
    )
    counts = _help_blocker_counts(monkeypatch, two_bigs, 2500, eligible={104, 105})
    total = sum(counts.values())
    assert total >= 50, f"too few help blocks to judge a split: {counts}"
    assert counts[104] / total <= 0.80 and counts[105] / total <= 0.80, counts

    lone_big = (*two_bigs[:3], _shot_blocker(104, Position.PF, 83, 55), two_bigs[4])
    counts = _help_blocker_counts(monkeypatch, lone_big, 2500, eligible={105})
    total = sum(counts.values())
    assert total >= 50, f"too few help blocks to judge a split: {counts}"
    assert counts[105] / total >= 0.90, counts


# --- docs/design/pick-and-pop.md -------------------------------------------


def test_dho_shape_never_produces_roll_and_can_produce_pop():
    """PP-14 mutation 11, at the wired possession level: a real `dho`
    possession must never reach `ROLL_LIVE`/`ROLL_TAGGED` (PP-6.1's
    dropped leg) and must be able to reach `POP_OPEN`/`POP_CONTESTED`.
    `dho`'s action-type weight is always a fixed fraction of `pnr_ball`'s
    own (`dho_share_of_pnr`, both keyed on `pnr_ball_freq` since
    `PlayerTendencies` has no `dho_freq` column) -- correlated by call
    order within one possession attempt, via the "setup" `BeatRecord`'s
    `outcome` (the chosen action type's own value) immediately preceding
    the "rotation" beat in the same trip, the same shape
    `test_out_of_position_defender_id_fires_on_no_pass_kickout_contested_
    and_roll_tagged` above already uses."""
    from dba2.sim.beats.rotation import RotationOutcome
    from dba2.sim.beats.rotation import resolve_rotation_beat as orig_rotation
    from dba2.sim.beats.setup import resolve_setup_beat as orig_setup

    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    offense = tuple(
        replace(
            p,
            ratings=replace(p.ratings, catch_shoot_3=92, catch_shoot_mid=88),
            tendencies=replace(p.tendencies, pnr_ball_freq=95, help_gamble_freq=5),
        )
        for p in offense
    )
    defense = tuple(
        replace(p, tendencies=replace(p.tendencies, help_gamble_freq=95)) for p in defense
    )

    pending_action = [None]
    dho_rotation_outcomes: list = []

    def _setup_spy(*args, **kwargs):
        result = orig_setup(*args, **kwargs)
        pending_action[0] = result.action_type
        return result

    def _rotation_spy(*args, **kwargs):
        result = orig_rotation(*args, **kwargs)
        if pending_action[0] is ActionType.DHO:
            dho_rotation_outcomes.append(result.outcome)
        return result

    possession_mod.resolve_setup_beat = _setup_spy
    possession_mod.resolve_rotation_beat = _rotation_spy
    try:
        for seed in range(2000):
            pending_action[0] = None
            game_state = GameState()
            _run_possession(offense, defense, DEFAULT_TUNING, seed, game_state)
    finally:
        possession_mod.resolve_setup_beat = orig_setup
        possession_mod.resolve_rotation_beat = orig_rotation

    assert dho_rotation_outcomes, "no dho possession ever reached beat 4 across 2000 seeds"
    assert not any(
        o in (RotationOutcome.ROLL_LIVE, RotationOutcome.ROLL_TAGGED)
        for o in dho_rotation_outcomes
    ), "a dho possession produced a ROLL_* outcome -- PP-6.1's dropped leg is reachable"
    assert any(
        o in (RotationOutcome.POP_OPEN, RotationOutcome.POP_CONTESTED)
        for o in dho_rotation_outcomes
    ), "no dho possession ever produced a pop outcome across 2000 seeds"


def test_iso_never_produces_pop_outcomes_scope_boundary():
    """PP-14 mutation 10: `iso` has no screener role at all and must never
    reach `POP_OPEN`/`POP_CONTESTED` -- guards the scope boundary a future
    edit (adding `ActionType.ISO` to `_POP_AVAILABLE_ACTIONS`) is most
    likely to blur. Biased toward `iso` the same way the BDA-4 test above
    does."""
    from dba2.sim.beats.rotation import RotationOutcome
    from dba2.sim.beats.rotation import resolve_rotation_beat as orig_rotation
    from dba2.sim.beats.setup import resolve_setup_beat as orig_setup

    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    offense = tuple(
        replace(
            p,
            ratings=replace(p.ratings, catch_shoot_3=92, catch_shoot_mid=88),
            tendencies=replace(
                p.tendencies, iso_freq=95, pnr_ball_freq=5, help_gamble_freq=5
            ),
        )
        for p in offense
    )
    defense = tuple(
        replace(p, tendencies=replace(p.tendencies, help_gamble_freq=95)) for p in defense
    )

    pending_action = [None]
    iso_rotation_outcomes: list = []

    def _setup_spy(*args, **kwargs):
        result = orig_setup(*args, **kwargs)
        pending_action[0] = result.action_type
        return result

    def _rotation_spy(*args, **kwargs):
        result = orig_rotation(*args, **kwargs)
        if pending_action[0] is ActionType.ISO:
            iso_rotation_outcomes.append(result.outcome)
        return result

    possession_mod.resolve_setup_beat = _setup_spy
    possession_mod.resolve_rotation_beat = _rotation_spy
    try:
        for seed in range(1000):
            pending_action[0] = None
            game_state = GameState()
            _run_possession(offense, defense, DEFAULT_TUNING, seed, game_state)
    finally:
        possession_mod.resolve_setup_beat = orig_setup
        possession_mod.resolve_rotation_beat = orig_rotation

    assert iso_rotation_outcomes, "no iso possession ever reached beat 4 across 1000 seeds"
    assert not any(
        o in (RotationOutcome.POP_OPEN, RotationOutcome.POP_CONTESTED)
        for o in iso_rotation_outcomes
    )


def test_pop_open_and_pop_contested_each_never_resolve_rim_and_open_converts_better():
    """PP-14 mutations 7 and 8, and PP-13 criteria 4(a)/4(d), at the wired
    possession level. Biases a real `pnr_ball` possession toward an
    elite-shooting screener and a help-happy defense, spies on the
    rotation -> resolution call pair inside the same trip (correlated by
    call order, `pending_outcome[0]` cleared the moment it's consumed),
    and checks separately for `POP_OPEN`/`POP_CONTESTED`: (a) the shot's
    zone is never `Zone.RIM` -- proving BOTH call sites still pass
    `is_pop_shot=True` (mutation 8 deletes it from only one, which would
    leave the other outcome's shots free to land at `Zone.RIM`); (b)
    `POP_OPEN` always reaches beat 5 with `is_committed_helper_contest=True`
    and `POP_CONTESTED` with `False` (mutation 7 flips `POP_OPEN`'s flag).
    The make-rate gap that flag produces is asserted in `test_resolution.py`,
    where the sample is large enough to carry it."""
    from dba2.sim.beats.resolution import resolve_resolution_beat as orig_resolution
    from dba2.sim.beats.rotation import RotationOutcome
    from dba2.sim.beats.rotation import resolve_rotation_beat as orig_rotation

    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    offense = tuple(
        replace(
            p,
            ratings=replace(p.ratings, catch_shoot_3=92, catch_shoot_mid=90),
            tendencies=replace(p.tendencies, pnr_ball_freq=95, help_gamble_freq=5),
        )
        for p in offense
    )
    defense = tuple(
        replace(p, tendencies=replace(p.tendencies, help_gamble_freq=95)) for p in defense
    )

    pending_outcome = [None]
    zones_by_outcome: dict = {
        RotationOutcome.POP_OPEN: [],
        RotationOutcome.POP_CONTESTED: [],
    }
    # Deterministic companion to the statistical open-vs-contested rate
    # check below: a flipped `POP_OPEN` flag (mutation 7) removes the 0.85
    # committed-helper penalty from that leaf, which shrinks the rate gap
    # to noise at this sample size -- the rate assertion alone survived it
    # when hand-run. The flag itself is what PP-10.2 specifies.
    committed_flags_by_outcome: dict = {
        RotationOutcome.POP_OPEN: set(),
        RotationOutcome.POP_CONTESTED: set(),
    }

    def _rotation_spy(*args, **kwargs):
        result = orig_rotation(*args, **kwargs)
        pending_outcome[0] = result.outcome
        return result

    def _resolution_spy(*args, **kwargs):
        result = orig_resolution(*args, **kwargs)
        outcome = pending_outcome[0]
        # Keyed on the rotation outcome alone, NOT on `is_pop_shot`: a
        # mutant that drops `is_pop_shot=True` from one call site must
        # still be captured here so it fails the `Zone.RIM` assertion
        # below, not the "never fired" one.
        if outcome in zones_by_outcome:
            zones_by_outcome[outcome].append(result.outcome.zone)
            committed_flags_by_outcome[outcome].add(
                kwargs.get("is_committed_helper_contest", False)
            )
        pending_outcome[0] = None
        return result

    possession_mod.resolve_rotation_beat = _rotation_spy
    possession_mod.resolve_resolution_beat = _resolution_spy
    try:
        for seed in range(1500):
            pending_outcome[0] = None
            game_state = GameState()
            _run_possession(offense, defense, DEFAULT_TUNING, seed, game_state)
    finally:
        possession_mod.resolve_rotation_beat = orig_rotation
        possession_mod.resolve_resolution_beat = orig_resolution

    for outcome in (RotationOutcome.POP_OPEN, RotationOutcome.POP_CONTESTED):
        assert zones_by_outcome[outcome], f"{outcome} never fired across 1500 seeds"
        assert all(z is not Zone.RIM for z in zones_by_outcome[outcome])

    assert committed_flags_by_outcome[RotationOutcome.POP_OPEN] == {True}
    assert committed_flags_by_outcome[RotationOutcome.POP_CONTESTED] == {False}


def test_pop_shot_scorer_and_passer_are_wired_correctly_on_a_make():
    """PP-13 criterion 4(b): every made pop's `scorer_id` is the
    screener's and `passer_id` the ball-handler's, with `assisted=True`.
    Correlated by call order (rotation immediately followed by resolution
    in the same trip), the same spy shape the tests above use, rather than
    inferred from a made shot's player identity after the fact -- role
    assignment is not pinned to a fixed offensive player across seeds, so
    a bare "non-ball-handler scored" heuristic is not unique to a pop."""
    from dba2.sim.beats.resolution import resolve_resolution_beat as orig_resolution
    from dba2.sim.beats.rotation import RotationOutcome
    from dba2.sim.beats.rotation import resolve_rotation_beat as orig_rotation

    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    offense = tuple(
        replace(
            p,
            ratings=replace(p.ratings, catch_shoot_3=95, catch_shoot_mid=92),
            tendencies=replace(p.tendencies, pnr_ball_freq=95, help_gamble_freq=5),
        )
        for p in offense
    )
    defense = tuple(
        replace(
            p,
            ratings=replace(p.ratings, closeout_speed=10, closeout_control=10),
            tendencies=replace(p.tendencies, help_gamble_freq=95),
        )
        for p in defense
    )

    pending = [None]  # (screener_id, passer_id) or None

    def _rotation_spy(*args, **kwargs):
        result = orig_rotation(*args, **kwargs)
        if result.outcome in (RotationOutcome.POP_OPEN, RotationOutcome.POP_CONTESTED):
            actor_ids = result.record.actor_ids
            pending[0] = (actor_ids["screener"], actor_ids["passer"])
        else:
            pending[0] = None
        return result

    made_pops: list = []

    def _resolution_spy(*args, **kwargs):
        result = orig_resolution(*args, **kwargs)
        if pending[0] is not None and kwargs.get("is_pop_shot"):
            screener_id, passer_id = pending[0]
            made_pops.append((result.outcome, screener_id, passer_id))
        pending[0] = None
        return result

    possession_mod.resolve_rotation_beat = _rotation_spy
    possession_mod.resolve_resolution_beat = _resolution_spy
    try:
        for seed in range(1500):
            pending[0] = None
            game_state = GameState()
            _run_possession(offense, defense, DEFAULT_TUNING, seed, game_state)
    finally:
        possession_mod.resolve_rotation_beat = orig_rotation
        possession_mod.resolve_resolution_beat = orig_resolution

    assert made_pops, "no pop shot attempt was captured across 1500 seeds"
    saw_a_made_pop = False
    for outcome, screener_id, passer_id in made_pops:
        if outcome.kind is TerminalKind.MADE_FG:
            saw_a_made_pop = True
            assert outcome.scorer_id == screener_id
            assert outcome.passer_id == passer_id
            assert outcome.assisted is True
    assert saw_a_made_pop, "no captured pop attempt was a make across 1500 seeds"


def test_pop_shot_and_rotation_use_the_poppers_live_post_switch_defender():
    """PP-8.3's defender asymmetry, without relying on goldens. Forces every
    screen to `SWITCH`, so the screener's live defender is the ball-handler's
    pre-switch on-ball defender (`apply_switch`'s own return value, read from
    a spy rather than recomputed) -- distinct from both the pre-switch screen
    defender and the beat-3 `helper`. For every pop, asserts that both the
    rotation beat's `popper_defender` and the pop shot's defender are that
    live defender. Kills: `popper_defender=helper`, resolving the pop shot
    against `helper`, and using the pre-switch `screen_def_id`."""
    from dba2.sim.beats.resolution import resolve_resolution_beat as orig_resolution
    from dba2.sim.beats.rotation import RotationOutcome
    from dba2.sim.beats.rotation import resolve_rotation_beat as orig_rotation
    from dba2.sim.beats.screen import ScreenOutcome
    from dba2.sim.beats.screen import apply_switch as orig_apply_switch
    from dba2.sim.beats.screen import resolve_screen_beat as orig_screen

    offense = tuple(
        replace(
            p,
            ratings=replace(p.ratings, catch_shoot_3=95, catch_shoot_mid=92),
            tendencies=replace(p.tendencies, pnr_ball_freq=95, help_gamble_freq=5),
        )
        for p in _lineup(1, 1)
    )
    defense = tuple(
        replace(p, tendencies=replace(p.tendencies, help_gamble_freq=95))
        for p in _lineup(101, 2)
    )

    latest_switch: dict = {}  # screener_id -> (live_defender_id, pre_switch_screen_def_id)
    pending: list = [None]  # (expected_live_def_id, helper_id, pre_switch_def_id)
    checked: list = []  # (expected, rotation_popper_def, shot_def, helper, pre_switch)

    def _screen_spy(*args, **kwargs):
        result = orig_screen(*args, **kwargs)
        return replace(
            result,
            outcome=ScreenOutcome.SWITCH,
            record=replace(result.record, outcome=ScreenOutcome.SWITCH.value),
        )

    def _switch_spy(matchups, **kwargs):
        updated = orig_apply_switch(matchups, **kwargs)
        latest_switch[kwargs["screener_id"]] = (
            updated[kwargs["screener_id"]],
            kwargs["screen_defender_id"],
        )
        return updated

    def _rotation_spy(*args, **kwargs):
        result = orig_rotation(*args, **kwargs)
        if result.outcome in (RotationOutcome.POP_OPEN, RotationOutcome.POP_CONTESTED):
            live_id, pre_id = latest_switch[kwargs["screener"].id]
            pending[0] = (live_id, kwargs["closing_defender"].id, pre_id)
            checked.append(
                [live_id, kwargs["popper_defender"].id, None,
                 kwargs["closing_defender"].id, pre_id]
            )
        else:
            pending[0] = None
        return result

    def _resolution_spy(*args, **kwargs):
        if pending[0] is not None:
            checked[-1][2] = args[1].id
        pending[0] = None
        return orig_resolution(*args, **kwargs)

    possession_mod.resolve_screen_beat = _screen_spy
    possession_mod.apply_switch = _switch_spy
    possession_mod.resolve_rotation_beat = _rotation_spy
    possession_mod.resolve_resolution_beat = _resolution_spy
    try:
        for seed in range(1500):
            pending[0] = None
            _run_possession(offense, defense, DEFAULT_TUNING, seed, GameState())
    finally:
        possession_mod.resolve_screen_beat = orig_screen
        possession_mod.apply_switch = orig_apply_switch
        possession_mod.resolve_rotation_beat = orig_rotation
        possession_mod.resolve_resolution_beat = orig_resolution

    distinct = [c for c in checked if len({c[0], c[3], c[4]}) == 3]
    assert len(distinct) >= 20, f"only {len(distinct)} pops had 3 distinct defenders"
    for live_id, rotation_def, shot_def, _helper_id, _pre_id in distinct:
        assert rotation_def == live_id, "rotation's popper_defender is not the live defender"
        assert shot_def == live_id, "pop shot was not resolved against the live defender"


# --- docs/design/turnover-recovery.md: the recovery continuation ------------


def _recovery_heavy_tuning(**possession_overrides):
    """Live turnovers on (almost) every resolution beat, and the offense
    winning 90% of them: `resolution_turnover` pinned at its 0.4 band edge,
    the live share at its ceiling, and the loose-ball band collapsed onto
    0.9. Everything else is the default profile."""
    tuning = replace(
        DEFAULT_TUNING,
        resolution_turnover=replace(
            DEFAULT_TUNING.resolution_turnover, base_rate=0.4, band=(0.4, 0.4)
        ),
        turnover_split=replace(DEFAULT_TUNING.turnover_split, live_steal_base_share=0.92),
        loose_ball=replace(DEFAULT_TUNING.loose_ball, band=(0.9, 0.9)),
    )
    if possession_overrides:
        tuning = replace(tuning, possession=replace(tuning.possession, **possession_overrides))
    return tuning


def _recoveries(sim_result) -> list:
    return [
        e
        for e in sim_result.result.credit_events
        if e.kind is TerminalKind.LOOSE_BALL_RECOVERED
    ]


def test_a_spent_budget_never_offers_the_recovery_leaf():
    """TR-4.9 / mutation 3: with no continuation budget the gate is closed
    on every resolution beat, so the `loose_ball_recovered` key is never in
    the leaf dict at all and no recovery is ever drawn -- the defense keeps
    every live loose ball. A gate captured at closure-creation time (the
    wrong place, TR-4.9) or dropped entirely would offer it anyway."""
    tuning = _recovery_heavy_tuning(continuation_budget_max=0)
    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    resolution_beats = 0
    for i in range(400):
        sim_result = _run_possession(offense, defense, tuning, i, GameState())
        assert sim_result.continuation_uses == 0
        assert not _recoveries(sim_result)
        for record in sim_result.beat_log:
            if record.beat == "resolution":
                resolution_beats += 1
                assert "loose_ball_recovered" not in record.probabilities
    assert resolution_beats > 0


def test_every_recovery_spends_a_continuation_and_never_exceeds_the_budget():
    """TR-4.5 / mutation 4: a recovery is the fourth member of Decision
    P1-11's single shared budget. Under a tuning that recovers 90% of a
    near-certain live turnover, a possession must still stop recovering at
    the cap, and its continuation count must cover every recovery."""
    tuning = _recovery_heavy_tuning()
    budget_max = tuning.possession.continuation_budget_max
    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    at_cap = 0
    for i in range(400):
        sim_result = _run_possession(offense, defense, tuning, i, GameState())
        recovered = len(_recoveries(sim_result))
        assert recovered <= budget_max
        assert sim_result.continuation_uses >= recovered
        assert sim_result.continuation_uses <= budget_max
        if recovered == budget_max:
            at_cap += 1
    assert at_cap > 0, "no possession reached the cap -- the bound above was never tested"


def test_a_recovery_ticks_the_scramble_and_never_resets_the_shot_clock():
    """TR-4.6 / mutation 5: no change of possession and no shot hit the rim,
    so the shot clock keeps draining. With the resolution beat's own tick
    pinned to a constant, the Setup beat that follows a recovery must open
    at exactly (clock at the resolution beat) - resolution tick - scramble,
    each scaled by pace -- computed with the same two `max(0, c - s)` steps
    `PossessionState.tick` takes, so the comparison is exact."""
    tuning = _recovery_heavy_tuning(resolution_seconds_range=(1.0, 1.0))
    pace = possession_mod._pace_seconds_scale(50, tuning)
    scramble = tuning.loose_ball.scramble_seconds
    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    checked = below_reset = 0
    for i in range(300):
        log = _run_possession(offense, defense, tuning, i, GameState()).beat_log
        for record, following in zip(log, log[1:], strict=False):
            if record.beat != "resolution" or record.outcome != "loose_ball_recovered":
                continue
            expected = max(0.0, max(0.0, record.clock_seconds - 1.0 * pace) - scramble * pace)
            assert following.beat == "setup"
            assert following.clock_seconds == expected
            checked += 1
            if expected < tuning.possession.offensive_rebound_shot_clock_reset:
                below_reset += 1
    assert checked > 0
    assert below_reset > 0, "no recovery happened under the 14s reset line -- not a real test"


def test_a_scramble_that_runs_out_the_clock_is_a_violation_on_the_recoverer():
    """TR-4.14's engine half: when the scramble tick expires the shot clock
    the possession ends in an attributed `TURNOVER_DEAD` on the recoverer,
    the resolution beat is the possession's last beat, and the recovery is
    still a credit event (never the terminal)."""
    tuning = _recovery_heavy_tuning()
    offense = _lineup(1, 1)
    defense = _lineup(101, 2)
    closes = 0
    for i in range(1500):
        sim_result = _run_possession(offense, defense, tuning, i, GameState())
        last = sim_result.beat_log[-1]
        if last.beat == "resolution" and last.outcome == "loose_ball_recovered":
            closes += 1
            terminal = sim_result.result.terminal
            assert terminal.kind is TerminalKind.TURNOVER_DEAD
            assert terminal.turnover_player_id == _recoveries(sim_result)[-1].recovering_player_id
    assert closes > 0


def test_the_offenses_best_loose_ball_player_recovers_most_but_not_all():
    """TR-4.7 (revised 2026-09-24): a weighted draw over the OFFENSE on the
    cached loose-ball scalar -- the guard with elite hustle is named most
    often (99 against four 40s: an expected 38% share) even though he is
    rarely the ball-handler who lost it, but his four teammates recover
    some too (an argmax would name him on every
    one), and no defender is ever named (mutation 9's `defense_lineup`)."""
    offense = tuple(
        make_sim_player(
            base_id,
            tuning=DEFAULT_TUNING,
            primary_position=pos,
            height_in=74 + i * 3,
            team_id=1,
            ratings_overrides=(
                {"motor": 99, "hands": 99, "basketball_iq": 99}
                if base_id == 4
                else {"motor": 40, "hands": 40, "basketball_iq": 40}
            ),
        )
        for i, (base_id, pos) in enumerate(zip(range(1, 6), _POSITIONS, strict=True))
    )
    defense = _lineup(101, 2)
    tuning = _recovery_heavy_tuning()
    recoverers: Counter[int] = Counter()
    for i in range(200):
        for event in _recoveries(_run_possession(offense, defense, tuning, i, GameState())):
            recoverers[event.recovering_player_id] += 1
    assert set(recoverers) <= {1, 2, 3, 4, 5}
    assert recoverers.most_common(1)[0][0] == 4
    assert len(recoverers) >= 4, recoverers
    assert recoverers[4] < sum(recoverers.values())


def test_zero_recovery_probability_never_reaches_the_recoverer_draw(monkeypatch):
    """The inert default (TR-4.8, TR-4.7 revised): with the loose-ball band
    collapsed to 0.0 the recovery leaf is never offered, so the weighted
    recoverer draw -- the only RNG this feature adds -- must never run.
    Together with the resolution beat's stream-position tests, this is what
    makes a zero-recovery engine byte-identical to the argmax one."""
    import dba2.sim.beats.resolution as resolution_mod

    def _must_not_draw(*_args, **_kwargs):
        raise AssertionError("draw_recoverer ran at recovery probability 0.0")

    monkeypatch.setattr(resolution_mod, "draw_recoverer", _must_not_draw)
    tuning = replace(
        _recovery_heavy_tuning(),
        loose_ball=replace(DEFAULT_TUNING.loose_ball, band=(0.0, 0.0)),
    )
    offense, defense = _lineup(1, 1), _lineup(101, 2)
    for i in range(500):
        sim_result = _run_possession(offense, defense, tuning, i, GameState())
        assert not _recoveries(sim_result)
