"""The chain driver: runs beats 1->2a->2b->3->4->5 in sequence per §4.3's
table, enforces Decision P1-11's shared continuation budget and the
24-second shot-clock backstop, and returns one possession's result
(phase-1-simulation-engine.md §4.3, §4.5, §4.6, Decision P1-11/P1-14).

Zero I/O, per Decision P1-1. `simulate_possession`'s `rng` must be drawn
via `core.rng.rng_for(game.rng_seed, possession_index)` by the caller
(`game.py`) -- this module never constructs a `Random` itself, matching
every other module under `sim/`.

**Interpretive calls made in this dispatch, named here because this file
is where they all converge (§4.2's table only gives each action type's
role *names*, not how the 5-beat chain actually threads through them):**

1. **Only the 4 action types with a `ball_handler` role (`pnr_ball`,
   `dho`, `iso`, `transition`) run the full screen/on-ball/help/rotation
   chain.** `post_up`, `off_ball_screen`, and `spot_up` have no live
   dribble read to beat 2b's shape (a post entry, a cut, and a catch-and-
   shoot look are not "ball-handler vs on-ball defender" contests) --
   they resolve directly against their template's primary scorer and
   that scorer's matchup defender, with `assisted=True` and the
   template's own named passer role (`entry_passer`/`passer`) credited
   structurally (Decision P1-15), since all three shapes are definitionally
   fed by a pass with no intervening dribble move.
2. **The screen beat (2a) only fires for `pnr_ball`/`dho`** (the two
   ball-handler actions with a real screener-shaped role --
   `dho`'s `handoff_big` is passed into `screen.py`'s generic
   `screener` parameter). `iso`/`transition` have no screener role and
   skip straight to the on-ball beat.
3. **The rotation beat's "roll" branch is only offered for `pnr_ball`; the
   "pop" branch (docs/design/pick-and-pop.md) is offered for both `pnr_ball`
   and `dho`.** `dho`'s `handoff_big` was previously described in this
   comment as typically popping or re-screening rather than rolling, with
   nothing implementing that sentence -- pick-and-pop.md's build wires the
   pop half of it (re-screening remains unmodelled, PP-18). `dho` now
   reaches `KICKOUT_OPEN`/`KICKOUT_CONTESTED`/`POP_OPEN`/`POP_CONTESTED`/
   `NO_PASS` on a `HELP_COMMITTED`/`HELP_LATE` read (`screener=handoff_big`,
   `tagger=None`, `popper_defender=` the live matchup dict's answer for
   `handoff_big`, passed to `beats/rotation.py`); `pnr_ball` reaches all 7
   members of `RotationOutcome`.
4. **`KICKOUT_OPEN`/`ROLL_LIVE` pass `is_committed_helper_contest=True`
   into `beats/resolution.py`; `KICKOUT_CONTESTED`/`ROLL_TAGGED` pass
   `False`.** This is what makes "open" vs "contested"/"tagged" a real
   probability difference at the shot: `composites.contest_quality`'s
   `committed_helper_penalty` already exists for exactly this ("a
   committed helper contesting out of position ... is scaled down");
   reusing it here means beat 4's own outcome, not a new knob, decides
   which contest strength beat 5 sees.
5. **B1 fix (critic-proven), replacing this dispatch's original interpretive
   call, which silently discarded an already-resolved outcome:** a
   budget-exhausted continuation now differs by which real-world fact, if
   any, already happened this beat. A `CONTAINED` secondary read carries
   no scoreable fact of its own (nothing happened yet -- the on-ball
   defender merely contained the dribble), so once
   `PossessionState.can_continue` is `False` there, the chain is forced
   straight into a resolution attempt with the current ball-handler and
   on-ball defender, matching §4.5's own "resolution proceeds straight to
   a shot or turnover outcome instead" text -- never a bare `TURNOVER_DEAD`
   for this flavor. A non-bonus foul or an offensive rebound, by contrast,
   *has* already resolved a real fact by the time the budget check runs
   (the foul's `TerminalOutcome`, or the shot+rebound's enriched
   `TerminalOutcome`) -- that fact is credited via `credit_events` (per
   §4.5: "an offensive rebound ... and a non-bonus non-shooting foul ...
   are always continuation events ... they can still have real box-score
   consequences ... but they never themselves end the possession"), and
   only *then* is the possession forced to a `TURNOVER_DEAD` terminal --
   there is no further live action this engine models once that credited
   event's own chance to continue is spent, so `TURNOVER_DEAD` remains the
   correct closed-set member for those two flavors specifically (an
   interpretive call, not a doc-mandated derivation -- flagged here since
   §4.5's prose doesn't fully spell out this exact sub-case).
6. **A missed final free throw's rebound draw uses the same 5 v 5
   lineup as the shot that drew the foul** -- no live substitution
   happens mid-possession (Decision P1-19: "substitutions happen at dead
   balls," and a possession is the unit between dead balls), so this is
   exactly the on-court lineup the whole possession already used.
7. **B2 fix (critic-proven): `assign_matchups` (Decision P1-9) now runs
   exactly once per possession, at the true start of beat 1** --
   `simulate_possession` calls it itself, before the chain loop begins,
   and threads the resulting (possibly `apply_switch`-mutated) matchup
   dict into every `resolve_setup_beat` call for the rest of this
   possession's secondary reads via that function's `matchups=` parameter.
   Previously, every loop iteration called `resolve_setup_beat` with
   `matchups=None`, which made it recompute a fresh pairing from scratch
   on every secondary read -- silently reverting any switch the instant a
   possession continued, contradicting Decision P1-9/P1-10's "a switch
   persists for the remainder of that possession."
8. **B4 fix (critic-proven): fatigue, foul trouble, and pace are now
   threaded through, scoped exactly to what §12 step 5 names.** Fatigue:
   `resolve_on_ball_beat`'s `offense_fatigue`/`defense_fatigue` parameters
   (declared since step 4, never fed a real value) now read
   `game_state.player(id).fatigue` for the live ball-handler/on-ball
   defender -- the pool itself is depleted/recovered by `game.py`, once
   per possession, after this function returns (see `game._update_fatigue`).
   Foul trouble: `resolve_help_beat`'s new `helper_in_foul_trouble`
   parameter reads `game_state.player(helper.id).is_in_foul_trouble(
   period=game_state.period)`. Pace: `offense_pace_target` (from
   `team_strategy.pace_target`, passed in by `game.py`) scales every
   seconds-consuming tick in this chain via `_pace_seconds_scale` --
   Decision P1-17's other named half, an early-clock-shot-probability
   effect, is out of this fix round's scope and is not wired.
9. **B7 fix (critic-proven), superseded by B10 below.** The original B7
   fix populated `out_of_position_defender_id` only on the 3 call sites
   where the helper's own contest/tag *failed* (the no-vacated-target
   fallback, `KICKOUT_OPEN`, `ROLL_LIVE`), leaving `KICKOUT_CONTESTED`/
   `ROLL_TAGGED`/`NO_PASS` at `None` on the stated rationale that "the
   helper's contest/tag succeeded there." Critic round 8 proved that
   rationale contradicts both §4.4/§4.6's own wording ("a committed
   low-man is out of position" -- the qualifier is *commitment*, not
   outcome) and `rebound.py`'s own docstring ("the helper who committed
   away from their assignment") -- and is factually wrong for `NO_PASS`
   specifically, since the helper never contests anything at all on that
   path (the ball-handler shoots against `on_ball_defender` while the
   helper stays stranded away from the vacated shooter), arguably the
   *cleanest* committed-low-man-out-of-position case in the whole chain,
   yet it was the one explicitly excluded.
10. **B10 fix (critic-round-8-proven): `out_of_position_defender_id` is
    now populated by *commitment*, not by contest/tag outcome, matching
    both the design doc and `rebound.py`'s docstring verbatim.** Every
    call into `_resolve_shot_and_fts` reached via the `HELP_COMMITTED`/
    `HELP_LATE` branch (the fallback, `NO_PASS`, `KICKOUT_OPEN`,
    `KICKOUT_CONTESTED`, `ROLL_LIVE`, `ROLL_TAGGED` -- all 6) now passes
    `out_of_position_defender_id=helper.id`, because `helper` committed
    away from their original assignment on every one of those 6 branches
    regardless of what happens afterward. This is deliberately decoupled
    from `is_committed_helper` (the *separate*, still-outcome-based
    `is_committed_helper_contest` flag from interpretive call 4 above,
    which correctly stays keyed on whether `helper` is the one contesting
    *this specific shot* and whether that contest arrived in time) --
    commitment answers "is this defender out of position for the
    rebound," contest outcome answers "is this shot's contest weakened,"
    and they are genuinely different facts that happen to be True
    together on `KICKOUT_OPEN`/`ROLL_LIVE` and False together pre-B10 on
    `KICKOUT_CONTESTED`/`ROLL_TAGGED`/`NO_PASS` -- but only the first
    fact is what `rebound.py`'s penalty is supposed to gate on. Every
    other call site (no-ball-handler actions, `CONTAINED`/`DEFENDER_WINS`,
    `HELP_STAYED`, the no-helper-assigned branch) has no beat-3 helper in
    scope at all -- `helper` never committed away from anything on those
    paths -- and passes `out_of_position_defender_id=None` explicitly
    (the parameter is keyword-required now, no default, per this round's
    nit fix, so every call site states its answer rather than falling
    through to one).
11. **B25-1 fix (critic-round-25-proven): every `_tick`-caused shot-clock
    violation now attributes a real `turnover_player_id`, not just a bare
    `TURNOVER_DEAD`.** `boxscore.py::_apply_outcome` only credits a
    `TURNOVER_DEAD`'s `tov` when `turnover_player_id` is not `None` --
    the old code never passed one for a genuine clock violation, which
    made every such possession (13.85% of them, measured, at the step-11
    pace constants) vanish from the box score with no FGA/FTA/TOV
    anywhere. `_turnover_dead` now takes an optional player id;
    `_setup_violation_ball_handler_id` covers the one call site (setup's
    own tick) with no `ball_handler` variable yet in scope, and every
    other tick call site (screen/on-ball/help/rotation) passes the
    already-in-scope `ball_handler.id` -- the ball never leaves their
    hands before any of those beats' own tick. The two budget-exhaustion
    call sites (interpretive call 5) deliberately keep passing no
    argument: that terminal represents no *additional* real-world fact on
    top of the credit event already recorded for the foul/rebound that
    triggered the exhaustion, so crediting a second, fabricated turnover
    there would double-count a single real event.
12. **B25-1 fix (critic-round-25-proven): the chain loop now tracks
    `attempt_index` and reads a distinct, cheaper pair of constants
    (`tuning.possession.continuation_setup_seconds`/`continuation_
    secondary_beat_seconds_range`) on every iteration after the first.**
    Every previous version of this loop paid the exact same setup +
    secondary-beat cost on a continuation (a `CONTAINED` secondary read,
    an offensive rebound, or a non-bonus foul, all per Decision P1-11's
    shared budget) as it did on the possession's opening attempt, as if
    the offense brought the ball back up the floor every time -- critic
    measured this as the dominant driver of both the pace retune's
    inflated average possession length and its inflated shot-clock-
    violation rate (see `tuning.PossessionTuning`'s own B25-1 comment for
    the full measurement). This is a documented interpretive call, not a
    design-doc derivation: §4.5/§4.6 describe *what* a continuation is
    but not how much game-clock time resuming play after one costs.
13. **BDA-4 fix (docs/design/block-defender-attribution.md), revised by
    docs/design/block-attribution-v2.md BA-2.1/BA-2.6: the weak-side
    rim-help pool is threaded into every `_resolve_shot_and_fts` call by
    closure (BDA-4.2) rather than as a new parameter on any of the 11 call
    sites.** Per shot, the pool is `defense_lineup` minus this shot's own
    on-ball defender (BDA-4.4 gate 4) -- tracked via the
    `current_on_ball_defender_id` local, reset to `None` at the top of
    every loop iteration and set only inside the 4 ball-handler action
    types' own branch (the 3 no-ball-handler types have no "on-ball
    defender" concept at all, per interpretive call 1 above, so the whole
    lineup is offered -- gate 3, checked inside `resolve_resolution_beat`
    itself, drops the shot's own primary defender). The possession's
    `out_of_position_defender_id` rides along as `rim_help_committed_id`
    (gate 5). Until v2 this was a single candidate, `composites.best_rim_
    protector(defense_lineup)`, nulled when he was on the ball; that
    winner-take-all rule gave every team's best big the same saturated
    exposure (BA-1 point 1).
14. **docs/design/pick-and-pop.md PP-8.3: the pop's contesting defender is
    the popper's own live matchup defender, not the beat-3 helper --
    deliberately not parallel to the roll branch, where the helper (also
    the tagger) is who contests.** `popper_defender=defense_by_id[
    matchups[screener.id]]`, read from the possession's live matchup dict
    at the point beat 4 runs, i.e. after any `apply_switch` mutation
    earlier this iteration (`apply_switch` already updates
    `matchups[screener_id]` on a `SWITCH`, `beats/screen.py:80-97`) -- so a
    `switch_all` team that puts a guard on a popping five is punished
    automatically, with no new mechanism. The `screener` local bound
    inside interpretive call 2's screen-beat branch (`_SCREENER_ACTIONS`,
    identical to `_POP_AVAILABLE_ACTIONS`'s two members) is reused here
    rather than a second `SCREENER`-else-`HANDOFF_BIG` lookup -- it is
    guaranteed already set whenever `pop_available` is `True` in this same
    loop iteration, since both frozensets name exactly `{PNR_BALL, DHO}`.
15. **docs/design/turnover-recovery.md TR-4.5/TR-4.6/TR-4.9: a recovered
    loose ball is the fourth member of Decision P1-11's shared continuation
    budget.** The recovery probability is a pure function of the two
    lineups, computed once per possession (`beats/loose_ball.py`) and
    threaded by closure like the rim-help pool; the offense lineup rides
    along as the recoverer candidates, and the resolution beat draws the
    recoverer from them only on a drawn recovery (TR-4.7,
    revised 2026-09-24: a weighted draw, not an argmax). The
    `can_continue` gate is evaluated **per resolution beat, inside
    `_resolve_shot_and_fts`** -- `possession_state` mutates across
    the chain loop, so a gate read at closure-creation time would be frozen
    at "budget fresh" and never fire. With the budget spent the leaf is not
    offered at all and its mass stays with `turnover_live`, which is what
    makes `LOOSE_BALL_RECOVERED` never terminal (TR-4.12). A drawn recovery
    is appended to `credit_events` (it charges no turnover and no steal --
    its payload carries no box-score field), uses one continuation, ticks
    `tuning.loose_ball.scramble_seconds` (a scalar, so no RNG) with the
    normal violation check -- a scramble that busts the shot clock is a real
    violation attributed to the recoverer -- and does NOT reset the shot
    clock: no change of possession, no shot hit the rim. The chain then
    restarts at Setup like every other continuation.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from random import Random

from dba2.domain.team import DefensiveScheme, OffensiveScheme
from dba2.sim.beats.help import HelpOutcome, resolve_help_beat
from dba2.sim.beats.loose_ball import recovery_probability
from dba2.sim.beats.on_ball import OnBallOutcome, resolve_on_ball_beat
from dba2.sim.beats.rebound import resolve_rebound_beat
from dba2.sim.beats.resolution import resolve_resolution_beat
from dba2.sim.beats.rotation import RotationOutcome, resolve_rotation_beat
from dba2.sim.beats.screen import ScreenOutcome, apply_switch, resolve_screen_beat
from dba2.sim.beats.setup import resolve_setup_beat
from dba2.sim.composites import spacing_aggregate
from dba2.sim.context import SimPlayer
from dba2.sim.events import BeatRecord, PossessionResult, TerminalKind, TerminalOutcome, Zone
from dba2.sim.playbook import ActionType, RoleSlot, assign_matchups
from dba2.sim.probability import clamp, contest_probability, rating_value, weighted_choice
from dba2.sim.state import GameState, new_possession_state
from dba2.sim.tuning import TuningProfile

_BALL_HANDLER_ACTIONS = frozenset(
    {ActionType.PNR_BALL, ActionType.DHO, ActionType.ISO, ActionType.TRANSITION}
)
_SCREENER_ACTIONS = frozenset({ActionType.PNR_BALL, ActionType.DHO})
_ROLL_AVAILABLE_ACTIONS = frozenset({ActionType.PNR_BALL})
# docs/design/pick-and-pop.md PP-6: `dho`'s `handoff_big` has no roll leg
# (PP-6.1) but does get a pop leg -- identical membership to
# `_SCREENER_ACTIONS` today, but named and checked separately (PP-6.2:
# `off_ball_screen` has a real SCREENER slot too but never reaches this
# beat at all, the scope boundary a future edit is most likely to blur).
_POP_AVAILABLE_ACTIONS = frozenset({ActionType.PNR_BALL, ActionType.DHO})

# See module docstring, interpretive call 1.
_HELPER_ROLE_BY_ACTION: dict[ActionType, RoleSlot] = {
    ActionType.PNR_BALL: RoleSlot.LOW_MAN,
    ActionType.DHO: RoleSlot.WEAK_SIDE_DEF,
    ActionType.ISO: RoleSlot.NEAREST_HELPER,
    ActionType.TRANSITION: RoleSlot.RIM_PROTECTOR,
}
_NO_BALL_HANDLER_SHOOTER_ROLE: dict[ActionType, RoleSlot] = {
    ActionType.POST_UP: RoleSlot.POST_SCORER,
    ActionType.OFF_BALL_SCREEN: RoleSlot.CUTTER,
    ActionType.SPOT_UP: RoleSlot.SHOOTER,
}
_NO_BALL_HANDLER_DEFENDER_ROLE: dict[ActionType, RoleSlot] = {
    ActionType.POST_UP: RoleSlot.POST_DEF,
    ActionType.OFF_BALL_SCREEN: RoleSlot.CUTTER_DEF,
    ActionType.SPOT_UP: RoleSlot.CLOSER,
}
_NO_BALL_HANDLER_PASSER_ROLE: dict[ActionType, RoleSlot] = {
    ActionType.POST_UP: RoleSlot.ENTRY_PASSER,
    ActionType.OFF_BALL_SCREEN: RoleSlot.ENTRY_PASSER,
    ActionType.SPOT_UP: RoleSlot.PASSER,
}

# Decision P1-11's hard depth cap exists so "a bug cannot loop forever" --
# this is that same defensive backstop at the possession-loop level. Never
# reached in practice: the shot clock and the 3-continuation budget both
# force termination well before this.
_MAX_LOOP_ITERATIONS = 50


@dataclass(frozen=True)
class PossessionSimResult:
    """§12 step 4 says `possession.py` "returns one `PossessionResult`" --
    true of the engine-facing outcome, but `game.py` also needs this
    possession's beat log to accumulate `GameResult.beat_log` (§7.1/§8.1),
    and `events.PossessionResult` deliberately carries only `terminal`/
    `credit_events` (§4.5's own restated shape). This thin wrapper is
    this dispatch's own addition, not a change to `events.py`'s
    contract."""

    result: PossessionResult
    beat_log: tuple[BeatRecord, ...]
    elapsed_seconds: float
    continuation_uses: int


def _lineup_by_id(lineup: tuple[SimPlayer, ...]) -> dict[int, SimPlayer]:
    return {p.id: p for p in lineup}


def _points_for(outcome: TerminalOutcome) -> int:
    """Points scored by this terminal/credit event alone, for
    `possession.py`'s own running score display in `BeatRecord`s (see
    module docstring on why this is *display*-only, not authoritative --
    `game.py` recomputes the authoritative score from the full
    `PossessionResult`)."""
    points = 0
    if outcome.kind is TerminalKind.MADE_FG:
        points += 3 if outcome.zone is Zone.THREE else 2
    points += outcome.ftm
    return points


def _stamp_possession_records(
    beat_log: Sequence[BeatRecord],
    *,
    outcome: TerminalOutcome,
    credit_events: Sequence[TerminalOutcome],
    period: int,
    home_score_after: int,
    away_score_after: int,
    game_clock_seconds: float,
) -> tuple[BeatRecord, ...]:
    """The one stamp `simulate_possession._finish` applies to a possession's
    beat records (Decision P5-27/P5-28, P5-97.b, watch-v2 E-1): period,
    after-scores and start clock go on EVERY record; the possession's own
    `terminal` and `possession_credits` go on the LAST record only (a
    snapshot after the final append; the outcomes are frozen, so sharing
    them is safe). The last record is usually a `resolution` or `rebound`
    beat but may be ANY beat kind (a violation close can end on `setup`,
    `on_ball`, `help`, `rotation` or `screen`), so consumers must read
    `terminal`, never assume the beat kind.

    Raises on an empty log rather than silently attaching nothing:
    `setup.record` is appended before every early return, so an empty log
    cannot occur today, and a future path that breaks that must fail loudly
    instead of dropping the terminal payload. Module-level (not a closure)
    only so a test can reach that branch, which no real possession can."""
    if not beat_log:
        raise ValueError(
            "possession finished with an empty beat log: the terminal outcome "
            f"({outcome.kind}) has no beat record to attach to"
        )
    stamped = [
        replace(
            record,
            period=period,
            home_score_after=home_score_after,
            away_score_after=away_score_after,
            game_clock_seconds=game_clock_seconds,
        )
        for record in beat_log
    ]
    stamped[-1] = replace(
        stamped[-1], terminal=outcome, possession_credits=tuple(credit_events)
    )
    return tuple(stamped)


def _simulate_free_throws(
    shooter: SimPlayer, ft_count: int, tuning: TuningProfile, rng: Random
) -> tuple[int, bool]:
    """§4.6: "each is an independent draw against free_throw." Returns
    `(makes, last_attempt_missed)` -- the caller uses the second value to
    decide whether the trip's final attempt is live (§4.6: "The final
    attempt of the trip, if missed, is live")."""
    cfg = tuning.free_throw
    makes = 0
    last_missed = False
    offense_score = rating_value(shooter, "free_throw")
    p_make = contest_probability(
        offense_score, 50.0, base_rate=cfg.base_rate, scale=cfg.scale, band=cfg.band
    )
    for _ in range(ft_count):
        made = weighted_choice(rng, {"make": p_make, "miss": 1.0 - p_make}) == "make"
        last_missed = not made
        if made:
            makes += 1
    return makes, last_missed


def _random_seconds(rng: Random, span: tuple[float, float]) -> float:
    lo, hi = span
    return rng.uniform(lo, hi)


def _pace_seconds_scale(pace_target: int, tuning: TuningProfile) -> float:
    """B4 fix: Decision P1-17's "`team_strategy.pace_target` scales those
    [per-beat] costs" -- a high-pace offense's beats run faster
    (`pace_scale_lo`), a low-pace offense's run slower (`pace_scale_hi`),
    linearly interpolated with 50 (the domain's neutral midpoint) landing
    at the mean of the two, i.e. ~1.0 with the documented defaults."""
    cfg = tuning.possession
    t = clamp(pace_target / 100.0, 0.0, 1.0)
    return cfg.pace_scale_hi - t * (cfg.pace_scale_hi - cfg.pace_scale_lo)


def _setup_violation_ball_handler_id(
    action_type: ActionType, offense_roles: dict[RoleSlot, int]
) -> int:
    """B25-1 fix: who "has the ball" if the shot clock expires during
    setup itself, before any secondary beat has even run. Every other
    `_tick` call site in this chain (screen/on-ball/help/rotation) has a
    real `ball_handler` already in scope, since those beats only exist
    for the 4 ball-handler action types (module docstring interpretive
    call 1) -- setup is the one call site that runs for *every* action
    type, including the 3 with no `RoleSlot.BALL_HANDLER` at all
    (post_up/off_ball_screen/spot_up). This is a documented interpretive
    call, not a design-doc derivation: those 3 shapes are "definitionally
    fed by a pass with no intervening dribble move" (interpretive call 1
    above), so there is no tracked role for whoever brings the ball up
    before that entry pass -- attributing the violation to that action's
    eventual shooter (the closest thing this engine models to "the
    offense's primary actor for this set") is the same kind of
    documented approximation `_resolve_shot_and_fts`'s post-up zone
    classification already makes elsewhere in this file."""
    if action_type in _BALL_HANDLER_ACTIONS:
        return offense_roles[RoleSlot.BALL_HANDLER]
    return offense_roles[_NO_BALL_HANDLER_SHOOTER_ROLE[action_type]]


def simulate_possession(
    offense_lineup: tuple[SimPlayer, ...],
    defense_lineup: tuple[SimPlayer, ...],
    *,
    tuning: TuningProfile,
    rng: Random,
    game_state: GameState,
    home_score: int,
    away_score: int,
    offense_is_home: bool,
    defense_help_aggression: int,
    defense_defensive_scheme: DefensiveScheme,
    crash_oreb_bias: int,
    offense_pace_target: int = 50,
    three_point_bias: int = 50,
    offensive_scheme: OffensiveScheme | None = None,
) -> PossessionSimResult:
    """Runs one possession end to end. `offense_lineup`/`defense_lineup`
    are the fixed 5 v 5 on-court sets for this possession (Decision
    P1-19: substitutions happen only at dead balls, i.e. between
    possessions, so this function never changes who's on the floor).
    `game_state` is mutated in place (team/player foul counts, and read
    for live fatigue/foul-trouble -- see the module docstring's
    interpretive call 8) -- the piece of `state.py` this beat chain
    genuinely needs mid-chain. `offense_pace_target` defaults to the
    tendency-domain's neutral midpoint (50, i.e. no scaling) so every
    existing caller that doesn't pass it keeps today's timing exactly.

    `three_point_bias` (docs/design/gm-command-surface.md PD-2, build step
    7): the offense team's own `team_strategy.three_point_bias`, forwarded
    to `resolve_resolution_beat` the same way `crash_oreb_bias` is already
    forwarded to `resolve_rebound_beat` -- same default-50/no-scaling
    convention as `offense_pace_target` for the identical reason.

    `offensive_scheme` (PD-10, build step 10): the offense team's own
    `team_strategy.offensive_scheme`, forwarded into the **one**
    `resolve_setup_beat` call in the chain loop below. Unlike
    `three_point_bias`, `None` (the default) is not a neutral-row lookup --
    `OffensiveScheme` has no neutral member (PD-10.4) -- it means no scheme
    factor is applied at all, which is what makes every existing caller
    that doesn't pass it byte-identical to pre-PD-10 behaviour (acceptance
    criterion 6a). That one call site runs once per loop iteration, not
    once per possession -- a continuation (a `CONTAINED` secondary read, an
    offensive rebound, a non-bonus foul) reselects a fresh action type, and
    the scheme applies to each of those reselections: a team's scheme does
    not switch off because the first look was contained (PD-10.6).
    """
    if len(offense_lineup) != 5 or len(defense_lineup) != 5:
        raise ValueError("simulate_possession requires a full 5-player lineup on both sides")

    offense_by_id = _lineup_by_id(offense_lineup)
    defense_by_id = _lineup_by_id(defense_lineup)
    defense_team_id = defense_lineup[0].player.team_id
    pace_scale = _pace_seconds_scale(offense_pace_target, tuning)

    possession_state = new_possession_state(tuning)
    beat_log: list[BeatRecord] = []
    credit_events: list[TerminalOutcome] = []
    running_home, running_away = home_score, away_score
    # Decision P1-9: assigned exactly once, at beat 1's true start (B2
    # fix, see module docstring interpretive call 7) -- every subsequent
    # `resolve_setup_beat` call in this possession's chain loop threads
    # this same (possibly `apply_switch`-mutated) dict back in via its
    # `matchups=` parameter instead of letting the beat recompute it.
    matchups: dict[int, int] = assign_matchups(offense_lineup, defense_lineup)
    # TR-4.4 (docs/design/turnover-recovery.md): a pure function of the
    # two lineups, so -- unlike `matchups` -- computed once per possession.
    # The budget gate is NOT applied here; see `_resolve_shot_and_fts` and
    # module docstring interpretive call 15.
    # The recoverer is not chosen here: TR-4.7's weighted draw happens
    # inside the resolution beat, only when a recovery is drawn.
    loose_ball_recovery_p = recovery_probability(offense_lineup, defense_lineup, tuning)
    # Set only inside the ball-handler-action branch below, to whichever
    # defender is attached to the ball for *this* shot (BDA-4.4 gate 4: he
    # is left out of the rim-help pool, module docstring call 13);
    # `None` for the 3 no-ball-handler action types, which have no such
    # concept, and reset every loop iteration since a continuation can
    # reselect a different action type.
    current_on_ball_defender_id: int | None = None

    def _apply_points(outcome: TerminalOutcome) -> None:
        nonlocal running_home, running_away
        pts = _points_for(outcome)
        if pts == 0:
            return
        if offense_is_home:
            running_home += pts
        else:
            running_away += pts

    def _finish(outcome: TerminalOutcome) -> PossessionSimResult:
        _apply_points(outcome)
        # Decision P5-27/P5-28: the two facts a `BeatRecord` cannot know at
        # its own construction site -- the period, and the score *after*
        # this possession -- are both final here and nowhere earlier. The
        # after-score is the possession's own entry score plus every point
        # this possession scored, terminal AND credit events (an and-one's
        # made free throw, a put-back), never the terminal alone -- see
        # this module's own docstring on why `running_home`/`running_away`
        # are display-only and not this arithmetic's source.
        offense_points = sum(_points_for(event) for event in (outcome, *credit_events))
        if offense_is_home:
            home_after, away_after = home_score + offense_points, away_score
        else:
            home_after, away_after = home_score, away_score + offense_points
        stamped_beat_log = _stamp_possession_records(
            beat_log,
            outcome=outcome,
            credit_events=credit_events,
            period=game_state.period,
            home_score_after=home_after,
            away_score_after=away_after,
            # Stage D commit 0 (P5-97.b): `game.py` decrements the
            # period clock only after `simulate_possession` returns,
            # so at this point it is still the possession's START clock.
            game_clock_seconds=game_state.clock_seconds,
        )
        return PossessionSimResult(
            result=PossessionResult(terminal=outcome, credit_events=credit_events),
            beat_log=stamped_beat_log,
            elapsed_seconds=possession_state.elapsed_seconds,
            continuation_uses=possession_state.continuation_uses,
        )

    def _turnover_dead(turnover_player_id: int | None = None) -> PossessionSimResult:
        """Naming nit fix (critic-round-7-proven): this used to be named
        `_shot_clock_violation`, but it is also the terminal every
        budget-exhaustion case in this module forces the possession into
        (see interpretive call 5 above) -- a real name for both callers,
        not just the shot-clock one.

        B25-1 fix (critic-round-25-proven): a genuine shot-clock violation
        (this function invoked right after a `_tick(...)` call reports
        `True`) is a real, attributable turnover -- whoever holds the ball
        when the clock expires loses it, the same way a bad pass or a
        stripped dribble does -- and `boxscore.py::_apply_outcome` already
        only credits a `TURNOVER_DEAD`'s `tov` when `turnover_player_id` is
        not `None`. The old code always called this with no argument,
        which silently made every shot-clock violation (13.85% of
        possessions, measured, once the step-11 pace retune is applied)
        invisible to the box score -- no FGA, no FTA, no turnover, nothing.
        Every real (tick-caused) call site below now passes the player
        currently holding the ball; the budget-exhaustion call sites
        (an already-credited non-bonus foul or offensive rebound forced to
        close out, see interpretive call 5) deliberately keep passing
        `None` -- that terminal represents no *additional* real-world fact
        beyond the credit event already recorded for this possession, so
        crediting a second, fabricated turnover on top of it would double
        an event that only happened once."""
        return _finish(
            TerminalOutcome(kind=TerminalKind.TURNOVER_DEAD, turnover_player_id=turnover_player_id)
        )

    def _tick(seconds: float) -> bool:
        """Ticks the shot clock; returns True if a violation occurred."""
        possession_state.tick(seconds)
        return possession_state.is_shot_clock_violation

    def _apply_rebound(
        outcome: TerminalOutcome,
        *,
        out_of_position_defender_id: int | None,
    ) -> tuple[PossessionSimResult | None, TerminalOutcome]:
        """Handles both rebound-eligible cases in one place: a plain
        `MISSED_FG` (every miss, blocked or not) and a missed *final*
        free throw (§4.6: "the exact same rebound sub-resolution as a
        missed field goal"). Returns `(early_result, outcome)` --
        `early_result` is non-`None` only when the rebound (or a budget
        exhaustion) forces an immediate return; otherwise the caller
        keeps going with the enriched `outcome`.

        `out_of_position_defender_id` (B7 fix, module docstring
        interpretive call 9) is threaded in by the caller -- this
        function has no beat-3 context of its own, it only forwards
        whatever `_resolve_shot_and_fts` was told."""
        rebound_result = resolve_rebound_beat(
            offense_lineup,
            defense_lineup,
            crash_oreb_bias=crash_oreb_bias,
            tuning=tuning,
            rng=rng,
            clock_seconds=possession_state.shot_clock_seconds,
            home_score=running_home,
            away_score=running_away,
            out_of_position_defender_id=out_of_position_defender_id,
        )
        beat_log.append(rebound_result.record)
        outcome = replace(
            outcome,
            rebounder_id=rebound_result.rebounder_id,
            rebound_is_offensive=rebound_result.is_offensive,
        )
        if not rebound_result.is_offensive:
            return None, outcome
        if not possession_state.can_continue(tuning):
            # B1 fix (critic-proven): this offensive rebound -- and the
            # shot/foul outcome it's now attached to -- already happened
            # and already had real box-score consequences (points, an
            # assist, a foul count) before this budget check ever runs;
            # discarding `outcome` here (the old behavior) silently
            # dropped an already-resolved fact. It is a continuation
            # event per §4.5, not a discarded one: credit it, then force
            # the possession's *ending* (never the fact that already
            # happened) to `TURNOVER_DEAD` -- there is no further live
            # action this engine models once this credited rebound's own
            # chance to continue is spent.
            credit_events.append(outcome)
            return _turnover_dead(), outcome
        possession_state.use_continuation()
        possession_state.reset_on_offensive_rebound(tuning)
        credit_events.append(outcome)
        return None, outcome

    def _resolve_shot_and_fts(
        shooter: SimPlayer,
        defender: SimPlayer,
        *,
        action_type: ActionType,
        is_committed_helper: bool,
        assisted: bool,
        passer_id: int | None,
        # NIT fix (critic-round-8-proven): no default -- `_apply_rebound`'s
        # equivalent parameter is correctly keyword-required with no
        # default already; a default here reintroduces the exact "a new
        # call site silently omits it and gets None" risk B7 existed to
        # fix. Every call site below passes it explicitly, `None` where no
        # beat-3 helper is in scope, `helper.id` everywhere one is.
        out_of_position_defender_id: int | None,
        # docs/design/pick-and-pop.md PP-10: defaulted `False` (mirroring
        # `resolve_resolution_beat`'s own default) rather than made
        # keyword-required like the parameter above -- only the 2 new
        # POP_OPEN/POP_CONTESTED call sites below are ever `True`; every
        # other call site's shot is definitionally not a pop.
        is_pop_shot: bool = False,
    ) -> PossessionSimResult | None:
        # BDA-4.4 gate 4 (block-attribution-v2 BA-2.6): the on-ball
        # defender is attached to the ball, not rotating, so he is left out
        # of the rim-help pool -- the one gate input `resolve_resolution_
        # beat` has no scope over. The rest of the floor can still help.
        # Gate 5's committed helper is `out_of_position_defender_id`,
        # already in this closure's scope (a required parameter above).
        rim_help_pool = tuple(
            p for p in defense_lineup if p.id != current_on_ball_defender_id
        )
        # TR-4.9: read the live budget at the moment of THIS beat. With it
        # spent the recovery leaf is not offered at all, so a recovery can
        # never be the possession's last event (TR-4.12).
        recovery_p = loose_ball_recovery_p if possession_state.can_continue(tuning) else 0.0
        result = resolve_resolution_beat(
            shooter,
            defender,
            action_type=action_type,
            tuning=tuning,
            rng=rng,
            clock_seconds=possession_state.shot_clock_seconds,
            home_score=running_home,
            away_score=running_away,
            is_committed_helper_contest=is_committed_helper,
            assisted=assisted,
            passer_id=passer_id,
            rim_help_pool=rim_help_pool,
            rim_help_committed_id=out_of_position_defender_id,
            help_aggression=defense_help_aggression,
            three_point_bias=three_point_bias,
            is_pop_shot=is_pop_shot,
            loose_ball_recovery_probability=recovery_p,
            loose_ball_candidates=offense_lineup,
        )
        beat_log.append(result.record)
        outcome = result.outcome
        # Resolution's own seconds cost, for pace realism (§5/§11) --
        # ticked after the fact rather than checked for a violation: the
        # shot has already happened, so a clock reaching 0 here doesn't
        # retroactively invalidate an outcome already drawn. Scaled by
        # `pace_scale` per Decision P1-17 (B4 fix).
        possession_state.tick(
            _random_seconds(rng, tuning.possession.resolution_seconds_range) * pace_scale
        )

        if outcome.kind is TerminalKind.LOOSE_BALL_RECOVERED:
            # TR-5.A.9: this arm must come first -- falling through to
            # `_finish` would END the possession on a recovery. The gate
            # above guarantees the budget has room, so there is no
            # budget-exhausted branch here (TR-4.9). Credit (no box-score
            # line: the payload carries none), use a continuation, tick the
            # scramble without resetting the shot clock (TR-4.6), and keep
            # looping.
            credit_events.append(outcome)
            possession_state.use_continuation()
            if _tick(tuning.loose_ball.scramble_seconds * pace_scale):
                # TR-4.14's scramble-violation close: the recoverer holds
                # the ball when the clock expires.
                return _turnover_dead(outcome.recovering_player_id)
            return None  # signals "keep looping" to the caller
        if outcome.kind is TerminalKind.NON_SHOOTING_FOUL:
            game_state.record_team_foul(defense_team_id)
            game_state.record_player_foul(outcome.fouler_id, tuning)
            if not game_state.is_in_bonus(defense_team_id, tuning):
                outcome = replace(outcome, ft_count=0)
                if not possession_state.can_continue(tuning):
                    # B1 fix (critic-proven): this non-bonus foul already
                    # happened -- `game_state`'s team/player foul counts
                    # above have already fired -- before this budget check
                    # runs; the old code discarded `outcome` (and its real
                    # box-score fact: the foul call itself) rather than
                    # crediting it. Credit it as a continuation event (per
                    # §4.5: fouls "still count against the fouler" even
                    # when they don't end the possession), then force the
                    # possession's ending to `TURNOVER_DEAD` -- no shot was
                    # taken this beat and no further chain read is
                    # possible with the budget spent, so there is no other
                    # closed-set member this could legitimately be.
                    credit_events.append(outcome)
                    return _turnover_dead()
                possession_state.use_continuation()
                credit_events.append(outcome)
                return None  # signals "keep looping" to the caller
        elif outcome.kind in (TerminalKind.SHOOTING_FOUL, TerminalKind.OFFENSIVE_FOUL) or (
            outcome.kind is TerminalKind.MADE_FG and outcome.and_one
        ):
            if outcome.kind is TerminalKind.OFFENSIVE_FOUL:
                game_state.record_player_foul(outcome.offensive_fouler_id, tuning)
            else:
                game_state.record_team_foul(defense_team_id)
                game_state.record_player_foul(outcome.fouler_id, tuning)

        needs_rebound = False
        if outcome.ft_count > 0:
            fouled = offense_by_id[outcome.fouled_player_id]
            makes, last_missed = _simulate_free_throws(fouled, outcome.ft_count, tuning, rng)
            outcome = replace(outcome, ftm=makes)
            needs_rebound = last_missed
        elif outcome.kind is TerminalKind.MISSED_FG:
            # §4.6: every missed field goal (blocked or not) draws a
            # rebound -- not just a missed final free throw.
            needs_rebound = True

        if needs_rebound:
            early, outcome = _apply_rebound(
                outcome, out_of_position_defender_id=out_of_position_defender_id
            )
            if early is not None:
                return early
            if outcome.rebound_is_offensive:
                return None  # keep looping -- offense retained the ball

        return _finish(outcome)

    for attempt_index in range(_MAX_LOOP_ITERATIONS):
        # B25-1 fix: every iteration after the first is a continuation
        # (Decision P1-11's shared budget), which resumes play -- it does
        # not re-run "the offense brings the ball up the floor" -- so it
        # reads the cheaper `continuation_*` constants instead of the first
        # attempt's own (see `PossessionTuning`'s B25-1 comment). This is
        # what actually fixes the structural defect critic measured: 36%
        # of possessions that continue at least once were paying the SAME
        # full setup+secondary cost on every cycle, which is what made
        # them 2-6x more likely to blow the shot clock than a single-cycle
        # possession.
        is_first_attempt = attempt_index == 0
        # BDA-4.4 gate 4: reset every iteration -- a continuation can
        # reselect a different action type, and the 3 no-ball-handler
        # types never set this at all (see module docstring interpretive
        # call 13).
        current_on_ball_defender_id = None
        setup_seconds_cost = (
            tuning.possession.setup_seconds
            if is_first_attempt
            else tuning.possession.continuation_setup_seconds
        )
        secondary_range = (
            tuning.possession.secondary_beat_seconds_range
            if is_first_attempt
            else tuning.possession.continuation_secondary_beat_seconds_range
        )
        setup = resolve_setup_beat(
            offense_lineup,
            defense_lineup,
            tuning,
            rng,
            clock_seconds=possession_state.shot_clock_seconds,
            home_score=running_home,
            away_score=running_away,
            seconds_consumed=setup_seconds_cost,
            # B2 fix: thread the possession's live matchup pairing in
            # instead of letting this beat recompute it from scratch on
            # every secondary read -- see module docstring interpretive
            # call 7.
            matchups=matchups,
            offensive_scheme=offensive_scheme,
        )
        # NIT2 fix (critic-proven): logged before the violation check, not
        # after -- this beat ran and its BeatRecord belongs in the log
        # regardless of whether the tick that follows ends the possession.
        beat_log.append(setup.record)
        action_type = setup.action_type
        offense_roles = setup.role_assignment.offense
        defense_roles = setup.role_assignment.defense
        matchups = dict(setup.matchups)
        if _tick(setup.seconds_consumed * pace_scale):
            # B25-1 fix: attribute to whoever has the ball at this point in
            # the chain (see `_setup_violation_ball_handler_id`'s own
            # docstring) rather than leaving `turnover_player_id` unset.
            return _turnover_dead(_setup_violation_ball_handler_id(action_type, offense_roles))

        if action_type not in _BALL_HANDLER_ACTIONS:
            shooter_role = _NO_BALL_HANDLER_SHOOTER_ROLE[action_type]
            defender_role = _NO_BALL_HANDLER_DEFENDER_ROLE[action_type]
            passer_role = _NO_BALL_HANDLER_PASSER_ROLE[action_type]
            shooter = offense_by_id[offense_roles[shooter_role]]
            defender = defense_by_id[defense_roles[defender_role]]
            passer_id = offense_roles.get(passer_role)
            outcome = _resolve_shot_and_fts(
                shooter,
                defender,
                action_type=action_type,
                is_committed_helper=False,
                assisted=passer_id is not None,
                passer_id=passer_id,
                # No beat-3 helper exists on this path -- these action
                # types never run the screen/on-ball/help/rotation chain
                # at all (interpretive call 1 above).
                out_of_position_defender_id=None,
            )
            if outcome is not None:
                return outcome
            continue

        ball_handler = offense_by_id[offense_roles[RoleSlot.BALL_HANDLER]]
        on_ball_defender_id = matchups[ball_handler.id]
        screen_outcome: ScreenOutcome | None = None

        if action_type in _SCREENER_ACTIONS:
            screener_role = RoleSlot.SCREENER if RoleSlot.SCREENER in offense_roles else (
                RoleSlot.HANDOFF_BIG
            )
            screener = offense_by_id[offense_roles[screener_role]]
            screen_def_id = defense_roles[RoleSlot.SCREEN_DEF]
            screen_defender = defense_by_id[screen_def_id]

            screen_result = resolve_screen_beat(
                screener,
                screen_defender,
                defensive_scheme=defense_defensive_scheme,
                tuning=tuning,
                rng=rng,
                clock_seconds=possession_state.shot_clock_seconds,
                home_score=running_home,
                away_score=running_away,
            )
            beat_log.append(screen_result.record)
            screen_seconds = _random_seconds(rng, secondary_range)
            if _tick(screen_seconds * pace_scale):
                # B25-1 fix: the ball is still in `ball_handler`'s hands at
                # every secondary-beat tick in this chain -- no pass has
                # happened yet on any of these paths.
                return _turnover_dead(ball_handler.id)
            screen_outcome = screen_result.outcome
            if screen_outcome is ScreenOutcome.SWITCH:
                matchups = apply_switch(
                    matchups,
                    ball_handler_id=ball_handler.id,
                    on_ball_defender_id=on_ball_defender_id,
                    screener_id=screener.id,
                    screen_defender_id=screen_def_id,
                )
                on_ball_defender_id = matchups[ball_handler.id]

        on_ball_defender = defense_by_id[on_ball_defender_id]
        # BDA-4.4 gate 4: set once, after any `apply_switch` mutation above
        # has already been folded into `on_ball_defender_id` -- every
        # `_resolve_shot_and_fts` call for the rest of this iteration
        # (CONTAINED/DEFENDER_WINS/HELP_STAYED/NO_PASS/KICKOUT_*/ROLL_*)
        # reads this same value via closure.
        current_on_ball_defender_id = on_ball_defender_id
        non_role_offense = tuple(
            p for p in offense_lineup if p.id not in offense_roles.values()
        )
        spacing = spacing_aggregate(non_role_offense)

        on_ball_result = resolve_on_ball_beat(
            ball_handler,
            on_ball_defender,
            tuning=tuning,
            rng=rng,
            clock_seconds=possession_state.shot_clock_seconds,
            home_score=running_home,
            away_score=running_away,
            screen_outcome=screen_outcome,
            # B4 fix: `offense_fatigue`/`defense_fatigue` have accepted a
            # value since step 4 but no caller ever fed one -- read the
            # live pool `game.py` depletes/recovers possession by
            # possession (see module docstring interpretive call 8).
            offense_fatigue=game_state.player(ball_handler.id).fatigue,
            defense_fatigue=game_state.player(on_ball_defender.id).fatigue,
            spacing_aggregate=spacing,
        )
        beat_log.append(on_ball_result.record)
        if _tick(_random_seconds(rng, secondary_range) * pace_scale):
            return _turnover_dead(ball_handler.id)  # B25-1 fix, see screen tick above

        if on_ball_result.outcome is OnBallOutcome.CONTAINED:
            if not possession_state.can_continue(tuning):
                # B1 fix (critic-proven): `CONTAINED` carries no scoreable
                # fact of its own -- nothing to credit -- but the old code
                # forced this straight to a bare `TURNOVER_DEAD` regardless
                # of where the chain actually was. §4.5's own text for the
                # analogous non-bonus-foul case ("resolution proceeds
                # straight to a shot or turnover outcome instead") is the
                # correct shape here too: force a resolution attempt with
                # the ball still in the current ball-handler's hands
                # against their matchup defender, exactly like the
                # `DEFENDER_WINS` branch below -- never a bare
                # `TURNOVER_DEAD` for this flavor.
                outcome = _resolve_shot_and_fts(
                    ball_handler,
                    on_ball_defender,
                    action_type=action_type,
                    is_committed_helper=False,
                    assisted=False,
                    passer_id=None,
                    # No beat-3 helper is in scope on this path -- explicit
                    # per the B10 nit fix (module docstring interpretive
                    # call 9): the parameter is required everywhere now,
                    # not defaulted, so a call site can't silently omit it.
                    out_of_position_defender_id=None,
                )
                if outcome is not None:
                    return outcome
                continue
            possession_state.use_continuation()
            continue

        if on_ball_result.outcome is OnBallOutcome.DEFENDER_WINS:
            outcome = _resolve_shot_and_fts(
                ball_handler,
                on_ball_defender,
                action_type=action_type,
                is_committed_helper=False,
                assisted=False,
                passer_id=None,
                out_of_position_defender_id=None,
            )
            if outcome is not None:
                return outcome
            continue

        # ADVANTAGE
        helper_role = _HELPER_ROLE_BY_ACTION[action_type]
        helper_id = defense_roles.get(helper_role)
        if helper_id is None:
            outcome = _resolve_shot_and_fts(
                ball_handler,
                on_ball_defender,
                action_type=action_type,
                is_committed_helper=False,
                assisted=False,
                passer_id=None,
                out_of_position_defender_id=None,
            )
            if outcome is not None:
                return outcome
            continue
        helper = defense_by_id[helper_id]

        help_result = resolve_help_beat(
            ball_handler,
            helper,
            help_aggression=defense_help_aggression,
            tuning=tuning,
            rng=rng,
            clock_seconds=possession_state.shot_clock_seconds,
            home_score=running_home,
            away_score=running_away,
            # B4 fix: §4.6's "foul trouble ... reduces the fouler's
            # help-commit probability" (see module docstring interpretive
            # call 8).
            helper_in_foul_trouble=game_state.player(helper.id).is_in_foul_trouble(
                period=game_state.period
            ),
        )
        beat_log.append(help_result.record)
        if _tick(_random_seconds(rng, secondary_range) * pace_scale):
            return _turnover_dead(ball_handler.id)  # B25-1 fix, see screen tick above

        if help_result.outcome is HelpOutcome.HELP_STAYED:
            # `helper` never committed away from their assignment on this
            # path (that's what HELP_STAYED means) -- not out of position.
            outcome = _resolve_shot_and_fts(
                ball_handler,
                on_ball_defender,
                action_type=action_type,
                is_committed_helper=False,
                assisted=False,
                passer_id=None,
                out_of_position_defender_id=None,
            )
            if outcome is not None:
                return outcome
            continue

        # HELP_COMMITTED / HELP_LATE -> beat 4
        vacated_offense_id = next(
            (off_id for off_id, def_id in matchups.items() if def_id == helper.id), None
        )
        if vacated_offense_id is None:
            outcome = _resolve_shot_and_fts(
                ball_handler,
                on_ball_defender,
                action_type=action_type,
                is_committed_helper=True,
                assisted=False,
                passer_id=None,
                # B7 fix: `helper` committed to help and, since there is
                # no clear vacated assignment to attribute it to, is the
                # out-of-position defender for this shot's rebound.
                out_of_position_defender_id=helper.id,
            )
            if outcome is not None:
                return outcome
            continue
        vacated_shooter = offense_by_id[vacated_offense_id]

        roll_available = (
            action_type in _ROLL_AVAILABLE_ACTIONS and RoleSlot.SCREENER in offense_roles
        )
        # docs/design/pick-and-pop.md PP-6/PP-9/interpretive call 14:
        # `pop_available` reuses the `screener` local bound inside this
        # iteration's screen-beat branch (interpretive call 2) rather than
        # a second SCREENER-else-HANDOFF_BIG lookup -- guaranteed already
        # set whenever `action_type in _POP_AVAILABLE_ACTIONS`, since that
        # frozenset names the same two members as `_SCREENER_ACTIONS`.
        pop_available = action_type in _POP_AVAILABLE_ACTIONS
        screener_for_rotation = screener if (roll_available or pop_available) else None
        popper_defender = (
            defense_by_id[matchups[screener.id]] if pop_available else None
        )

        rotation_result = resolve_rotation_beat(
            vacated_shooter=vacated_shooter,
            passer=ball_handler,
            closing_defender=helper,
            screener=screener_for_rotation,
            tagger=helper if roll_available else None,
            popper_defender=popper_defender,
            tuning=tuning,
            rng=rng,
            clock_seconds=possession_state.shot_clock_seconds,
            home_score=running_home,
            away_score=running_away,
        )
        beat_log.append(rotation_result.record)
        if _tick(_random_seconds(rng, secondary_range) * pace_scale):
            return _turnover_dead(ball_handler.id)  # B25-1 fix, see screen tick above

        if rotation_result.outcome is RotationOutcome.NO_PASS:
            outcome = _resolve_shot_and_fts(
                ball_handler,
                on_ball_defender,
                action_type=action_type,
                is_committed_helper=False,
                assisted=False,
                passer_id=None,
                # B10 fix (critic-round-8-proven): `helper` is committed
                # away from their assignment on this entire HELP_COMMITTED/
                # HELP_LATE branch, and NO_PASS is the case where the
                # helper never even gets a chance to contest -- the
                # ball-handler shoots against `on_ball_defender` while
                # `helper` stays stranded away from the vacated shooter.
                # §4.4/§4.6's own wording ("a committed low-man is out of
                # position") and `rebound.py`'s own docstring ("the helper
                # who committed away from their assignment") both condition
                # on commitment, not on whether a later contest/tag
                # succeeded -- `is_committed_helper` (this shot's contest-
                # quality flag, interpretive call 4) stays `False` because
                # `helper` isn't even the one contesting this shot, but
                # `out_of_position_defender_id` is a separate fact about
                # the rebound, not the contest, and `helper` is exactly the
                # out-of-position defender the rebound beat needs to know
                # about.
                out_of_position_defender_id=helper.id,
            )
        elif rotation_result.outcome is RotationOutcome.KICKOUT_OPEN:
            outcome = _resolve_shot_and_fts(
                vacated_shooter,
                helper,
                action_type=action_type,
                is_committed_helper=True,
                assisted=True,
                passer_id=ball_handler.id,
                # B7 fix: the closeout failed to arrive in time (that's
                # what KICKOUT_OPEN means) -- `helper` is out of position
                # for this shot's rebound even though they end up as the
                # (late) contesting defender.
                out_of_position_defender_id=helper.id,
            )
        elif rotation_result.outcome is RotationOutcome.KICKOUT_CONTESTED:
            outcome = _resolve_shot_and_fts(
                vacated_shooter,
                helper,
                action_type=action_type,
                is_committed_helper=False,
                assisted=True,
                passer_id=ball_handler.id,
                # B10 fix (critic-round-8-proven): `helper` still committed
                # away from their original assignment on this path -- the
                # only thing KICKOUT_CONTESTED changes is that the
                # closeout arrived in time to contest this specific shot
                # (which is why `is_committed_helper_contest` above stays
                # `False`, a deliberate and separate distinction, per
                # interpretive call 4). Commitment, not contest outcome, is
                # what §4.4/§4.6 and `rebound.py`'s docstring both condition
                # the rebound's out-of-position penalty on.
                out_of_position_defender_id=helper.id,
            )
        elif rotation_result.outcome is RotationOutcome.ROLL_LIVE:
            outcome = _resolve_shot_and_fts(
                screener_for_rotation,
                helper,
                action_type=action_type,
                is_committed_helper=True,
                assisted=True,
                passer_id=ball_handler.id,
                # B7 fix: the tag on the roll failed (ROLL_LIVE) -- same
                # reasoning as KICKOUT_OPEN above.
                out_of_position_defender_id=helper.id,
            )
        elif rotation_result.outcome is RotationOutcome.ROLL_TAGGED:
            outcome = _resolve_shot_and_fts(
                screener_for_rotation,
                helper,
                action_type=action_type,
                is_committed_helper=False,
                assisted=True,
                passer_id=ball_handler.id,
                # B10 fix (critic-round-8-proven): same reasoning as
                # KICKOUT_CONTESTED above -- `helper` committed away from
                # their assignment to tag the roll; the tag succeeding
                # (ROLL_TAGGED) only affects this shot's own contest
                # quality, not whether `helper` is out of position for the
                # rebound that follows.
                out_of_position_defender_id=helper.id,
            )
        elif rotation_result.outcome is RotationOutcome.POP_OPEN:
            # docs/design/pick-and-pop.md PP-10/PP-10.1/PP-10.2: the
            # shooter is the popping screener, the defender is his own live
            # matchup defender (PP-8.3, not `helper`) -- `is_committed_
            # helper=True` reuses `contest_quality`'s existing
            # `committed_helper_penalty` for a recovering screen defender
            # closing out late, the same reuse `KICKOUT_OPEN`/`ROLL_LIVE`
            # already make (interpretive call 4). `out_of_position_
            # defender_id=helper.id` is PP-10.1/B10, not a copy-paste:
            # `helper` committed away from their assignment on every branch
            # of this HELP_COMMITTED/HELP_LATE read regardless of what the
            # popper's own defender does, so the rebound penalty still
            # conditions on `helper`'s commitment, not on this shot's
            # contest.
            outcome = _resolve_shot_and_fts(
                screener_for_rotation,
                popper_defender,
                action_type=action_type,
                is_committed_helper=True,
                assisted=True,
                passer_id=ball_handler.id,
                out_of_position_defender_id=helper.id,
                is_pop_shot=True,
            )
        else:  # POP_CONTESTED
            outcome = _resolve_shot_and_fts(
                screener_for_rotation,
                popper_defender,
                action_type=action_type,
                is_committed_helper=False,
                assisted=True,
                passer_id=ball_handler.id,
                out_of_position_defender_id=helper.id,
                is_pop_shot=True,
            )
        if outcome is not None:
            return outcome
        continue

    # Defensive backstop only -- see `_MAX_LOOP_ITERATIONS`'s docstring.
    return _turnover_dead()  # pragma: no cover
