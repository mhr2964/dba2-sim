"""Beat 2a: screener vs. screen-defender (phase-1-simulation-engine.md
§4.3/§4.4, Decision P1-10).

Zero I/O, per Decision P1-1.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from random import Random

from dba2.domain.team import DefensiveScheme
from dba2.sim.context import SimPlayer
from dba2.sim.events import BeatRecord
from dba2.sim.probability import ability_score, contest_probability, weighted_choice
from dba2.sim.tuning import TuningProfile


class ScreenOutcome(StrEnum):
    SWITCH = "switch"
    OVER = "over"
    UNDER = "under"
    BLOWN = "blown"
    HEDGE_RECOVER = "hedge_recover"


@dataclass(frozen=True)
class ScreenBeatResult:
    outcome: ScreenOutcome
    probabilities: dict[str, float]
    record: BeatRecord


def resolve_screen_beat(
    screener: SimPlayer,
    screen_defender: SimPlayer,
    *,
    defensive_scheme: DefensiveScheme,
    tuning: TuningProfile,
    rng: Random,
    clock_seconds: float,
    home_score: int,
    away_score: int,
) -> ScreenBeatResult:
    """§4.4's Screen row. `BLOWN` is the sole offense-favoring outcome (the
    screen defender loses the matchup outright); `OVER`/`UNDER`/`SWITCH`/
    `HEDGE_RECOVER` are all "the defense contained this" outcomes and
    split the defense-favorable remainder by `defensive_scheme` (§4.4's
    named Modifier) via `tuning.screen.defense_outcome_shares` -- the
    mechanism that makes a `switch_all` team produce switches and a
    `drop` team produce unders on the *same* underlying contest
    probability (§12 step 3's verify bullet)."""
    cfg = tuning.screen
    offense_score = ability_score(screener, cfg.offense_weights)
    defense_score = ability_score(screen_defender, cfg.defense_weights)
    p_blown = contest_probability(
        offense_score, defense_score, base_rate=cfg.base_rate_blown, scale=cfg.scale, band=cfg.band
    )
    shares = cfg.defense_outcome_shares[defensive_scheme]
    remainder = 1.0 - p_blown

    probabilities: dict[str, float] = {ScreenOutcome.BLOWN.value: p_blown}
    for outcome_name, share in shares.items():
        probabilities[outcome_name] = remainder * share

    outcome = ScreenOutcome(weighted_choice(rng, probabilities))
    record = BeatRecord(
        beat="screen",
        actor_ids={"screener": screener.id, "screen_defender": screen_defender.id},
        outcome=outcome.value,
        probabilities=probabilities,
        clock_seconds=clock_seconds,
        home_score=home_score,
        away_score=away_score,
    )
    return ScreenBeatResult(outcome=outcome, probabilities=probabilities, record=record)


def apply_switch(
    matchups: Mapping[int, int],
    *,
    ball_handler_id: int,
    on_ball_defender_id: int,
    screener_id: int,
    screen_defender_id: int,
) -> dict[int, int]:
    """Decision P1-10: on `SWITCH`, the on-ball defender and the screen
    defender trade assignments for the remainder of the possession -- the
    screen defender now guards the ball-handler, the on-ball defender now
    guards the screener. Returns a new matchup mapping (caller merges it
    into the possession's live matchups, per Decision P1-9); does not
    mutate `matchups` in place."""
    updated = dict(matchups)
    updated[ball_handler_id] = screen_defender_id
    updated[screener_id] = on_ball_defender_id
    return updated
