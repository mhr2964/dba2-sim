"""The loose-ball recovery contest (docs/design/turnover-recovery.md
TR-4.4/TR-4.7): when the resolution beat draws a live-ball turnover, the
offense can win its own loose ball back, and when it does, it is not a
turnover at all -- no `tov`, no `stl`, the possession continues.

`recovery_probability` is a pure function, called **once per possession**
by `possession.py::simulate_possession` (following the
BDA-4.1 precedent for "a pure function of the lineups, don't recompute it
per shot"); it consumes no RNG, and the recovery itself rides the
resolution beat's existing single `weighted_choice` draw as its own leaf
(TR-4.8). `draw_recoverer` is the one RNG consumer here (TR-4.7, revised
2026-09-24): `resolve_resolution_beat` calls it exactly once per drawn
recovery and never otherwise, so a possession with no recovery -- and the
whole engine at recovery probability `0.0` -- draws nothing extra. The
per-shot `can_continue` gate is applied by the caller at each resolution
beat, never here (TR-4.9).

Zero I/O, per Decision P1-1. Reads no `PlayerTendencies` field at all --
only the construction-time `SimPlayer.loose_ball_contribution` cache, which
is built from three plain `PlayerRatings` columns (`tuning.loose_ball.
weights`), so Decision P1-16's allowlist is untouched.
"""
from __future__ import annotations

from collections.abc import Sequence
from random import Random
from statistics import fmean

from dba2.sim.context import SimPlayer
from dba2.sim.probability import contest_probability, weighted_choice
from dba2.sim.tuning import TuningProfile


def recovery_probability(
    offense_lineup: Sequence[SimPlayer],
    defense_lineup: Sequence[SimPlayer],
    tuning: TuningProfile,
) -> float:
    """The offense's probability of winning a live loose ball: each side's
    mean cached `loose_ball_contribution` into `contest_probability`, offense
    first (the first score favors the outcome, this codebase's convention).
    A 5-man mean on both sides, so the realized team-to-team spread is
    small by construction -- measured at 4.5-4.8 points of probability
    across the 30 most-used lineups (TR-12, Commit 1)."""
    cfg = tuning.loose_ball
    return contest_probability(
        fmean(p.loose_ball_contribution for p in offense_lineup),
        fmean(p.loose_ball_contribution for p in defense_lineup),
        base_rate=cfg.base_rate,
        scale=cfg.scale,
        band=cfg.band,
    )


def draw_recoverer(
    offense_lineup: Sequence[SimPlayer], rng: Random, tuning: TuningProfile
) -> SimPlayer:
    """TR-4.7 (revised 2026-09-24, user decision): one `weighted_choice`
    draw over the offense, each player weighted by his cached
    `loose_ball_contribution` raised to `tuning.loose_ball.
    recovery_concentration_exponent` (R12; 1.0 is plain proportional
    weights, TR-4.7 records the measured spread). All five offensive players
    are candidates, including the one
    who lost the ball (re-gathering your own bobbled dribble is the
    commonest real version of this event).

    Exactly one `rng.random()` call, always. `weighted_choice` would divert
    an all-non-positive weight vector into `rng.choice`, which consumes a
    different number of draws and desyncs the rest of the seeded game (the
    hazard PD-2 disclosure 3 and PP-10.1 name); the zero-total guard below
    substitutes equal weights instead, so the draw still happens, once,
    down the same `rng.random()` path. Keys are lineup indices, not player
    ids, so the walk order is lineup order and a fixture that reuses an id
    cannot collapse two candidates into one key."""
    if not offense_lineup:
        raise ValueError("draw_recoverer requires at least one offensive player")
    exponent = tuning.loose_ball.recovery_concentration_exponent
    weights = {
        i: max(p.loose_ball_contribution, 0.0) ** exponent
        for i, p in enumerate(offense_lineup)
    }
    if sum(weights.values()) <= 0.0:
        weights = dict.fromkeys(weights, 1.0)
    return offense_lineup[weighted_choice(rng, weights)]
