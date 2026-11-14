"""B25-3 fix (critic-round-25-proven): discrimination-floor regression
tests, added FIRST per critic's explicit recommendation, before any further
re-tuning of `team_win_total_sd` -- so a future flattening pass cannot
silently crush skill discrimination to noise again the way step 11's
chain-wide flattening did.

Critic's finding: every one of the beats step 11 flattened retained only
about 27% of its pre-flatten discrimination at a *realistic* 30-point
rating gap (all 11 unit-test monotonicity checks use an 85-point gap with a
bare `>` assertion, which holds at any positive `scale` and cannot detect a
73% flattening). This file reproduces critic's own measurement method
exactly: `contest_probability(65, 50, ...) - contest_probability(35, 50,
...)` -- one side of a contest at 65 (a clearly-above-average, not
extreme, rating) vs. 35 (clearly-below-average), the other side held at the
domain's neutral midpoint (50), which is what the critic round 25 report's
own spread table is computed against (independently re-derived and
confirmed byte-identical to critic's cited OLD/NEW numbers before this
file was written).

Floors below are set at roughly 90% of this round's B25-3 retune targets
(see `tuning.py`'s own per-beat B25-3 comments for the exact target and the
targeted-vs-uniform retention rationale) -- enough margin to tolerate a
minor future constant nudge without enough slack to let another
blanket-flattening pass silently sail through. Every floor here was
verified, before the retune landed, to REJECT the round-18 shipped values
(the ones critic measured at ~27% retention) -- so this test suite is a
real regression guard, not a restatement of whatever the constants happen
to be today.
"""
from __future__ import annotations

from dba2.sim.probability import contest_probability
from dba2.sim.tuning import DEFAULT_TUNING

# (name, base_rate, scale, band, floor) -- floor is the minimum
# `contest_probability(65, 50, ...) - contest_probability(35, 50, ...)`
# this beat's live tuning constants must produce.
_DISCRIMINATION_FLOOR_CASES = [
    (
        "screen.blown",
        DEFAULT_TUNING.screen.base_rate_blown,
        DEFAULT_TUNING.screen.scale,
        DEFAULT_TUNING.screen.band,
        0.040,
    ),
    (
        "on_ball.advantage",
        DEFAULT_TUNING.on_ball.base_rate_advantage,
        DEFAULT_TUNING.on_ball.scale,
        DEFAULT_TUNING.on_ball.band,
        0.080,
    ),
    (
        "help.gate",
        DEFAULT_TUNING.help.gate_base_rate,
        DEFAULT_TUNING.help.gate_scale,
        DEFAULT_TUNING.help.gate_band,
        0.055,
    ),
    (
        "help.quality",
        DEFAULT_TUNING.help.quality_base_rate,
        DEFAULT_TUNING.help.quality_scale,
        DEFAULT_TUNING.help.quality_band,
        0.080,
    ),
    (
        "rotation_kickout",
        DEFAULT_TUNING.rotation_kickout.base_rate,
        DEFAULT_TUNING.rotation_kickout.scale,
        DEFAULT_TUNING.rotation_kickout.band,
        0.080,
    ),
    (
        "rotation_roll_tag",
        DEFAULT_TUNING.rotation_roll_tag.base_rate,
        DEFAULT_TUNING.rotation_roll_tag.scale,
        DEFAULT_TUNING.rotation_roll_tag.band,
        0.080,
    ),
    (
        "resolution_shot.rim",
        DEFAULT_TUNING.resolution_shot.zone_tuning["rim"].base_rate,
        DEFAULT_TUNING.resolution_shot.zone_tuning["rim"].scale,
        DEFAULT_TUNING.resolution_shot.zone_tuning["rim"].band,
        0.080,
    ),
    (
        "resolution_shot.mid",
        DEFAULT_TUNING.resolution_shot.zone_tuning["mid"].base_rate,
        DEFAULT_TUNING.resolution_shot.zone_tuning["mid"].scale,
        DEFAULT_TUNING.resolution_shot.zone_tuning["mid"].band,
        0.075,
    ),
    (
        "resolution_turnover",
        DEFAULT_TUNING.resolution_turnover.base_rate,
        DEFAULT_TUNING.resolution_turnover.scale,
        DEFAULT_TUNING.resolution_turnover.band,
        0.030,
    ),
    (
        "resolution_foul",
        DEFAULT_TUNING.resolution_foul.base_rate,
        DEFAULT_TUNING.resolution_foul.scale,
        DEFAULT_TUNING.resolution_foul.band,
        0.045,
    ),
    (
        "rebound",
        DEFAULT_TUNING.rebound.base_rate,
        DEFAULT_TUNING.rebound.scale,
        DEFAULT_TUNING.rebound.band,
        0.075,
    ),
    # BDA-4 (docs/design/block-defender-attribution.md): the weak-side
    # rim-help contest, added new -- not a step-11/round-25 retune, but
    # this file's own convention (module docstring) is that a beat this
    # skill-sensitive gets a floor from the day it lands, not after a
    # future flattening pass is caught in the act a second time. The floor
    # is verified below to reject a deliberately over-flattened value
    # (3x the shipped scale) before the shipped scale itself is trusted.
    (
        "resolution_rim_help",
        DEFAULT_TUNING.resolution_rim_help.base_rate,
        DEFAULT_TUNING.resolution_rim_help.scale,
        DEFAULT_TUNING.resolution_rim_help.band,
        # block-attribution-v2 BA-4.2: re-measured at scale 12 / base 0.055
        # -- spread 0.1524 (was 0.0605 at 45 / 0.10). Floor ~90% of that;
        # the 3x-flattened check below (scale 36: 0.0442) still fails it.
        0.137,
    ),
]


# A beat with no round-18 history (BDA-4's `resolution_rim_help`) is
# checked against this multiple of its own shipped scale instead --
# roughly the same "how far a chain-wide flattening pass moved a scale"
# ratio round 18's real history shows (e.g. rebound's 35 -> 130 is ~3.7x).
_OVER_FLATTENED_SCALE_MULTIPLIER = 3.0


def _spread_at_realistic_gap(base_rate: float, scale: float, band: tuple[float, float]) -> float:
    hi = contest_probability(65.0, 50.0, base_rate=base_rate, scale=scale, band=band)
    lo = contest_probability(35.0, 50.0, base_rate=base_rate, scale=scale, band=band)
    return hi - lo


def test_every_flattened_beat_clears_its_realistic_gap_discrimination_floor():
    failures = []
    for name, base_rate, scale, band, floor in _DISCRIMINATION_FLOOR_CASES:
        spread = _spread_at_realistic_gap(base_rate, scale, band)
        if spread < floor:
            failures.append(f"{name}: spread={spread:.4f} < floor={floor:.4f} (scale={scale})")
    assert not failures, (
        "B25-3 regression -- at least one beat's discrimination at a realistic 30-point "
        "rating gap has fallen back below its floor (blanket-flattening recurrence?): "
        + "; ".join(failures)
    )


def test_discrimination_floor_cases_would_have_rejected_the_round_18_shipped_values():
    """Proves the floors above are a real regression guard, not a
    restatement of today's constants: every floor rejects the exact
    round-18 (pre-B25-3) shipped scale for its beat, at that beat's own
    base_rate/band, reproducing critic round 25's measured ~27% retention
    finding directly rather than trusting the number in prose.

    `resolution_rim_help` (BDA-4) has no round-18 history -- it didn't
    exist yet -- so it's checked against a synthetic 3x-over-flattened
    scale instead (`_OVER_FLATTENED_SCALE_MULTIPLIER`), per this file's
    module docstring's own convention ("verified... to REJECT" a
    deliberately-too-flat value before the shipped one is trusted, not
    trusting the round-18 dict specifically)."""
    round_18_shipped_scales = {
        "screen.blown": 130.0,
        "on_ball.advantage": 149.0,
        "help.gate": 223.0,
        "help.quality": 149.0,
        "rotation_kickout": 149.0,
        "rotation_roll_tag": 149.0,
        "resolution_shot.rim": 106.0,
        "resolution_shot.mid": 113.0,
        "resolution_turnover": 124.0,
        "resolution_foul": 111.0,
        "rebound": 130.0,
    }
    still_passing = []
    for name, base_rate, scale, band, floor in _DISCRIMINATION_FLOOR_CASES:
        old_scale = round_18_shipped_scales.get(name, scale * _OVER_FLATTENED_SCALE_MULTIPLIER)
        spread = _spread_at_realistic_gap(base_rate, old_scale, band)
        if spread >= floor:
            still_passing.append(f"{name}: old spread={spread:.4f} >= floor={floor:.4f}")
    assert not still_passing, (
        "at least one floor is too loose -- the round-18 shipped (or synthetic "
        f"over-flattened) scale still clears it: {still_passing}"
    )
