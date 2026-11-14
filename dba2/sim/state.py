"""In-game runtime state: clock, score, fatigue, fouls, heat, lineups
(phase-1-simulation-engine.md §3, §5, §8.1, Decision P1-20).

Decision P1-20: "fatigue, heat/streak, foul trouble, and on-court lineups
live in sim/state.py and are never persisted." Everything here is a plain
mutable dataclass -- unlike `context.py`'s frozen `SimPlayer`/`SimTeam`
(constant for the whole game), state mutates possession by possession, and
`possession.py`/`game.py` are the only callers that ever mutate it.

Built incrementally across this dispatch's two steps, per the build
brief's own resolved ambiguity: step 4 needs only `GameState`'s
per-team-per-period foul counter (what `beats/resolution.py`'s
`NON_SHOOTING_FOUL` draw reads to decide terminal-bonus-foul vs
continuation) plus the shot clock and continuation-budget counters
(`PossessionState`, Decision P1-11); step 5 extends the same `GameState`
object with the fuller fatigue/heat/rotation/lineup state `game.py` needs,
rather than two separate files or a step-4 stub replaced wholesale in
step 5.

§8.1 names `GameState`, `PlayerGameState`, `LineupState` as this module's
types; `PossessionState` is this dispatch's own addition (not named in
§8.1's list, which is not framed as exhaustive -- "everything else is a
sim/ type") because Decision P1-11's shot-clock/continuation-budget pair
is possession-scoped, not game-scoped, and folding it into `GameState`
would make "reset at the start of every possession" an easy-to-miss
convention instead of a type boundary.

Zero I/O, per Decision P1-1.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dba2.sim.tuning import TuningProfile


@dataclass
class PossessionState:
    """Possession-scoped, reset by `possession.py` at the start of every
    possession -- never carried across possessions. Decision P1-11's
    shared continuation-budget cap (secondary reads, offensive rebounds,
    and non-bonus fouls all draw from the same counter) and the 24-second
    shot clock, which resets to `tuning.possession.
    offensive_rebound_shot_clock_reset` on an offensive rebound but does
    NOT reset the continuation counter (§4.6's rebound bullet, restated
    explicitly because the design doc calls out this exact
    non-symmetry)."""

    shot_clock_seconds: float
    continuation_uses: int = 0
    # Cumulative game-clock seconds this possession has consumed --
    # separate from `shot_clock_seconds` because an offensive-rebound
    # reset (below) moves the *countdown* back up to 14 without undoing
    # any of the real game time already spent, and `game.py` (step 5)
    # needs the real total to decrement the period clock correctly.
    elapsed_seconds: float = 0.0

    def can_continue(self, tuning: TuningProfile) -> bool:
        return self.continuation_uses < tuning.possession.continuation_budget_max

    def use_continuation(self) -> None:
        """Caller must have already checked `can_continue` -- this method
        does not itself enforce the cap, matching every other mutator in
        this module (dumb counters; the cap/branch logic lives in
        `possession.py`, which is what actually decides what to do when
        the budget is exhausted)."""
        self.continuation_uses += 1

    def tick(self, seconds: float) -> None:
        self.shot_clock_seconds = max(0.0, self.shot_clock_seconds - seconds)
        self.elapsed_seconds += seconds

    def reset_on_offensive_rebound(self, tuning: TuningProfile) -> None:
        """Real NBA rule (not an interpretive calibration): the clock
        resets *up* to 14 only if fewer than 14 seconds remained -- a
        possession with, say, 18 seconds still on the clock at the
        offensive rebound keeps its 18, it is never shortened. `max`, not
        `min` (an earlier version of this method had this backwards)."""
        self.shot_clock_seconds = max(
            self.shot_clock_seconds, tuning.possession.offensive_rebound_shot_clock_reset
        )
        # Deliberately does NOT touch continuation_uses -- §4.6's rebound
        # bullet: the shot-clock reset and the continuation-budget
        # permanence are two different things that must not blur.

    @property
    def is_shot_clock_violation(self) -> bool:
        return self.shot_clock_seconds <= 0.0


def new_possession_state(tuning: TuningProfile) -> PossessionState:
    return PossessionState(shot_clock_seconds=tuning.possession.shot_clock_seconds)


@dataclass
class PlayerGameState:
    """One player's whole-game mutable state. `fatigue` is a bounded pool
    in [0.0, 1.0] (0 = fully fresh, 1 = fully gassed) per Decision P1-20
    ("a per-player pool depleted by seconds played ... recovering on the
    bench"); `heat` is a bounded, decaying shot-result modifier in
    [-1.0, 1.0] gated by `consistency`/`composure` (also Decision P1-20).
    Both are plain floats mutated in place by `game.py`/`possession.py`,
    not recomputed from history each time -- the pool *is* the state."""

    player_id: int
    fatigue: float = 0.0
    seconds_played: float = 0.0
    heat: float = 0.0
    fouls: int = 0

    def is_disqualified(self, tuning: TuningProfile) -> bool:
        return self.fouls >= tuning.possession.fouls_to_disqualify

    def is_in_foul_trouble(self, *, period: int) -> bool:
        """§4.6: "Foul trouble (4+ before the fourth period) feeds the
        rotation model." Only meaningful before period 4 (regulation) --
        by the fourth period or overtime a player is either already
        disqualified (6 fouls) or playing through it, so the rotation
        model has nothing left to protect against."""
        return period < 4 and self.fouls >= 4


@dataclass
class LineupState:
    """The 5 players each team currently has on the floor. A plain pair
    of id sets -- `game.py`'s substitution model is what decides
    membership; this type just holds the current answer so `possession.py`
    (matchup pairing, Decision P1-9) and `boxscore.py` (plus-minus,
    Decision P1-20) can both read it without recomputing from minutes
    targets on every possession."""

    home_on_court: frozenset[int]
    away_on_court: frozenset[int]


@dataclass
class GameState:
    """Decision P1-20, in full: "fatigue, heat/streak, foul trouble, and
    on-court lineups live in sim/state.py and are never persisted." Plus
    the per-team-per-period foul counter Amendment 1's second pass added
    explicitly ("the same shape as personal foul counts, just scoped to
    the team and the period instead of the player and the game") --
    that's `team_fouls_this_period` below, and it's the one piece step 4's
    `beats/resolution.py` actually needs to resolve a `NON_SHOOTING_FOUL`
    draw at all (bonus vs continuation, §4.5)."""

    period: int = 1
    clock_seconds: float = 0.0
    home_score: int = 0
    away_score: int = 0
    overtime_periods: int = 0

    # Decision P1-20's per-team-per-period foul counter, keyed by team_id.
    # Reset by `reset_team_fouls_for_new_period` at the start of every
    # period (including overtime) -- never mid-period.
    team_fouls_this_period: dict[int, int] = field(default_factory=dict)

    # Step 5: per-player fatigue/heat/personal-foul state, keyed by
    # player_id, and the currently on-court lineup per team.
    players: dict[int, PlayerGameState] = field(default_factory=dict)
    lineup: LineupState | None = None

    def player(self, player_id: int) -> PlayerGameState:
        """Lazily materializes a fresh `PlayerGameState` on first touch --
        every player starts the game fresh (0 fatigue, 0 fouls), so there
        is nothing to look up ahead of time for a bench player who hasn't
        entered yet."""
        state = self.players.get(player_id)
        if state is None:
            state = PlayerGameState(player_id=player_id)
            self.players[player_id] = state
        return state

    def record_team_foul(self, team_id: int) -> int:
        """Increments and returns the team's foul count for the current
        period -- the return value is what `possession.py` compares
        against `tuning.possession.bonus_team_foul_threshold` to decide
        whether the *next* non-shooting foul this period is a bonus
        terminal outcome (§4.5)."""
        n = self.team_fouls_this_period.get(team_id, 0) + 1
        self.team_fouls_this_period[team_id] = n
        return n

    def is_in_bonus(self, team_id: int, tuning: TuningProfile) -> bool:
        threshold = tuning.possession.bonus_team_foul_threshold
        return self.team_fouls_this_period.get(team_id, 0) >= threshold

    def reset_team_fouls_for_new_period(self) -> None:
        self.team_fouls_this_period = {}

    def record_player_foul(self, player_id: int, tuning: TuningProfile) -> int:
        """Capped at `tuning.possession.fouls_to_disqualify` (6) --
        `game.py`'s rotation model only removes a disqualified player at
        the *next* possession boundary (real dead-ball substitution
        timing, Decision P1-19), so the same already-6-fouled player can
        still be on the floor and draw another foul call within the
        possession that disqualified them. A real box score's `PF`
        column never exceeds 6 (a player is physically removed before a
        7th personal foul is possible), so this is a box-score-accuracy
        cap, not a gameplay rule change -- it doesn't affect team-foul/
        bonus bookkeeping (`record_team_foul`, separate) or
        `is_disqualified`'s own threshold check, which is already true
        the moment the count reaches 6 regardless of this cap."""
        state = self.player(player_id)
        state.fouls = min(state.fouls + 1, tuning.possession.fouls_to_disqualify)
        return state.fouls
