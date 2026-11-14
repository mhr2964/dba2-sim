"""Rebound contest (phase-1-simulation-engine.md §4.6's rebound bullet,
§4.4's Rebound row) -- "a resolution sub-beat, big enough for its own
file" per §3's module layout. Fires after any `MISSED_FG` and after a
missed final free throw (§4.6): "the exact same rebound sub-resolution as
a missed field goal."

Zero I/O, per Decision P1-1. Restricted per Decision P1-16: the only
`PlayerTendencies` field read here is `crash_oreb_freq`, one of the
allowlisted names -- enforced by `tests/unit/sim/test_tuning.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from random import Random

from dba2.sim.context import SimPlayer
from dba2.sim.events import BeatRecord
from dba2.sim.probability import ability_score, clamp, contest_probability, weighted_choice
from dba2.sim.tuning import TuningProfile


@dataclass(frozen=True)
class ReboundBeatResult:
    is_offensive: bool
    rebounder_id: int
    probabilities: dict[str, float]
    record: BeatRecord


def _crash_scale(player: SimPlayer, tuning: TuningProfile) -> float:
    """§4.4's Rebound row Modifiers: `crash_oreb_freq`† scales the
    offensive side. A multiplicative nudge around 1.0 (never negative),
    same shape `_help_aggression_scale` in `composites.py` uses for an
    analogous tendency-driven scale."""
    mod = tuning.rebound_modifiers
    nudge = (player.tendencies.crash_oreb_freq - 50.0) * mod.crash_oreb_freq_share_scale
    return max(0.0, 1.0 + nudge)


def _crash_bias_scale(crash_oreb_bias: int, tuning: TuningProfile) -> float:
    """`team_strategy.crash_oreb_bias` (tendency-domain, 0-100) linearly
    mapped onto `[lo, hi]`, mirroring `composites._help_aggression_scale`
    exactly (same modifier shape, different tuning source)."""
    mod = tuning.rebound_modifiers
    t = clamp(crash_oreb_bias / 100.0, 0.0, 1.0)
    return mod.crash_oreb_bias_lo + t * (mod.crash_oreb_bias_hi - mod.crash_oreb_bias_lo)


def resolve_rebound_beat(
    offense_lineup: tuple[SimPlayer, ...],
    defense_lineup: tuple[SimPlayer, ...],
    *,
    crash_oreb_bias: int,
    tuning: TuningProfile,
    rng: Random,
    clock_seconds: float,
    home_score: int,
    away_score: int,
    out_of_position_defender_id: int | None = None,
) -> ReboundBeatResult:
    """§4.4's Rebound row: "a weighted draw over all ten on-court
    players." Two draws, both from the same `rng`: first a team-level
    offense-vs-defense contest (the same `contest_probability` shape
    every other beat uses, over the two sides' mean ability scores, the
    offensive side additionally scaled by `crash_oreb_bias`), then a
    within-side weighted choice attributing the board to one specific
    player -- unavoidable because Decision P1-12's "two scores, one
    binary draw" shape has no way to name an individual out of 5
    candidates on its own.

    `out_of_position_defender_id`, when given, is the beat-3 helper who
    committed away from their assignment (§4.4: "a committed low-man is
    out of position") -- their individual defensive-rebound weight is
    scaled down by `tuning.rebound_modifiers.out_of_position_defender_penalty`."""
    cfg = tuning.rebound
    mod = tuning.rebound_modifiers
    bias_scale = _crash_bias_scale(crash_oreb_bias, tuning)

    offense_team_score = (
        sum(ability_score(p, cfg.offense_weights) for p in offense_lineup) / len(offense_lineup)
    ) * bias_scale
    defense_team_score = sum(ability_score(p, cfg.defense_weights) for p in defense_lineup) / len(
        defense_lineup
    )

    p_offensive = contest_probability(
        offense_team_score,
        defense_team_score,
        base_rate=cfg.base_rate,
        scale=cfg.scale,
        band=cfg.band,
    )
    side_probabilities = {"offense": p_offensive, "defense": 1.0 - p_offensive}
    side = weighted_choice(rng, side_probabilities)
    is_offensive = side == "offense"

    # docs/design/per-36-spread-compression.md SC-3.1/SC-7 Commit 2: raise
    # `ability_score` to `attribution_concentration_exponent` BEFORE either
    # modifier multiplies it. Ordering is load-bearing, not incidental --
    # `_crash_scale`/`out_of_position_defender_penalty` are tendency/
    # positioning scales calibrated at their current relative strength
    # against `oreb_pct` and out-of-position behaviour; exponentiating the
    # modifier too would silently turn a 0.7x penalty into 0.7^2.4 ~= 0.41x,
    # an uncalibrated, unreviewed constant. See `ReboundModifierTuning`'s
    # own docstring for the full derivation.
    exponent = mod.attribution_concentration_exponent
    if is_offensive:
        individual_weights = {
            p.id: (ability_score(p, cfg.offense_weights) ** exponent) * _crash_scale(p, tuning)
            for p in offense_lineup
        }
    else:
        individual_weights = {
            p.id: (ability_score(p, cfg.defense_weights) ** exponent)
            * (mod.out_of_position_defender_penalty if p.id == out_of_position_defender_id else 1.0)
            for p in defense_lineup
        }
    rebounder_id = weighted_choice(rng, individual_weights)

    probabilities = dict(side_probabilities)
    record = BeatRecord(
        beat="rebound",
        actor_ids={"rebounder": rebounder_id},
        outcome=side,
        probabilities=probabilities,
        clock_seconds=clock_seconds,
        home_score=home_score,
        away_score=away_score,
    )
    return ReboundBeatResult(
        is_offensive=is_offensive,
        rebounder_id=rebounder_id,
        probabilities=probabilities,
        record=record,
    )
