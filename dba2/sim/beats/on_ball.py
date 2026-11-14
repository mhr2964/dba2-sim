"""Beat 2b: ball-handler vs. on-ball defender (phase-1-simulation-engine.md
§4.3/§4.4).

Zero I/O, per Decision P1-1.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from random import Random

from dba2.sim.beats.screen import ScreenOutcome
from dba2.sim.context import SimPlayer
from dba2.sim.events import BeatRecord
from dba2.sim.probability import ability_score, contest_probability, weighted_choice
from dba2.sim.tuning import TuningProfile


class OnBallOutcome(StrEnum):
    ADVANTAGE = "advantage"
    CONTAINED = "contained"
    DEFENDER_WINS = "defender_wins"


@dataclass(frozen=True)
class OnBallBeatResult:
    outcome: OnBallOutcome
    probabilities: dict[str, float]
    record: BeatRecord


def resolve_on_ball_beat(
    ball_handler: SimPlayer,
    on_ball_defender: SimPlayer,
    *,
    tuning: TuningProfile,
    rng: Random,
    clock_seconds: float,
    home_score: int,
    away_score: int,
    screen_outcome: ScreenOutcome | None = None,
    offense_fatigue: float = 0.0,
    defense_fatigue: float = 0.0,
    spacing_aggregate: float = 0.0,
) -> OnBallBeatResult:
    """§4.4's On-ball row. `ADVANTAGE` is the sole offense-favoring
    outcome; the defense-favorable remainder splits between `CONTAINED`
    and `DEFENDER_WINS` by `tuning.on_ball.defender_wins_share`. Modifiers
    (screen result, fatigue on both sides, the possession's spacing
    aggregate) shift the offense/defense diff *before* the logistic,
    per §4.4's header ("scheme values, state, aggregates ... never enter
    an ability-score vector") -- none of them are read off either
    player's ability vector.

    `screen_outcome`/`offense_fatigue`/`defense_fatigue`/
    `spacing_aggregate` all default to their no-op value so this beat is
    independently callable without a preceding screen beat or a live
    `state.py`/`composites.py` pipeline (neither exists yet for actions
    without a screener, and `state.py` itself is step 4-5 scope)."""
    cfg = tuning.on_ball
    offense_score = ability_score(ball_handler, cfg.offense_weights)
    defense_score = ability_score(on_ball_defender, cfg.defense_weights)

    diff_bonus = 0.0
    if screen_outcome is not None:
        diff_bonus += cfg.screen_outcome_diff_bonus.get(screen_outcome.value, 0.0)
    diff_bonus += spacing_aggregate * cfg.spacing_diff_weight
    # Higher `offense_fatigue` (a more gassed ball-handler) should lower
    # the offense's diff; higher `defense_fatigue` should raise it -- the
    # bonus is the *defense's* fatigue minus the *offense's*, not the
    # other way around.
    diff_bonus += (defense_fatigue - offense_fatigue) * cfg.fatigue_diff_weight

    p_advantage = contest_probability(
        offense_score + diff_bonus,
        defense_score,
        base_rate=cfg.base_rate_advantage,
        scale=cfg.scale,
        band=cfg.band,
    )
    remainder = 1.0 - p_advantage
    p_defender_wins = remainder * cfg.defender_wins_share
    p_contained = remainder - p_defender_wins

    probabilities = {
        OnBallOutcome.ADVANTAGE.value: p_advantage,
        OnBallOutcome.CONTAINED.value: p_contained,
        OnBallOutcome.DEFENDER_WINS.value: p_defender_wins,
    }
    outcome = OnBallOutcome(weighted_choice(rng, probabilities))
    record = BeatRecord(
        beat="on_ball",
        actor_ids={"ball_handler": ball_handler.id, "on_ball_defender": on_ball_defender.id},
        outcome=outcome.value,
        probabilities=probabilities,
        clock_seconds=clock_seconds,
        home_score=home_score,
        away_score=away_score,
    )
    return OnBallBeatResult(outcome=outcome, probabilities=probabilities, record=record)
