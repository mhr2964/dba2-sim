"""Beat 3: named low-man/helper vs. the driver (phase-1-simulation-engine.md
§4.3/§4.4).

Zero I/O, per Decision P1-1.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from random import Random

from dba2.domain.role import DefenseMode
from dba2.sim.context import SimPlayer
from dba2.sim.events import BeatRecord
from dba2.sim.probability import ability_score, contest_probability, weighted_choice
from dba2.sim.tuning import DEFAULT_TUNING, TuningProfile


class HelpOutcome(StrEnum):
    HELP_STAYED = "help_stayed"
    HELP_COMMITTED = "help_committed"
    HELP_LATE = "help_late"


@dataclass(frozen=True)
class HelpBeatResult:
    outcome: HelpOutcome
    probabilities: dict[str, float]
    record: BeatRecord


def _gate_probability(helper: SimPlayer, *, help_aggression: int, tuning: TuningProfile) -> float:
    """§4.4's Help-row Modifiers: "scheme_discipline + help_aggression
    gate the *decision* to help." `scheme_discipline` (a `PlayerRatings`
    field) and `help_aggression` (a `TeamStrategy` field) are both
    legitimate Modifiers-column inputs per §4.4's own header, not ability-
    vector members -- neither appears in `tuning.help.offense_weights`/
    `defense_weights`. `help_gamble_freq`† (the one allowlisted tendency
    on this row) also raises the gate: a gambler is, definitionally, more
    likely to leave their assignment to help at all.

    **`helper.directive.defense_mode` is read here too (docs/design/
    gm-command-surface.md PD-4)**: a fixed additive offset on `gate_diff`,
    applied AFTER the weighted mean above -- so a `stay_home` player on a
    `help_aggression = 90` team still helps more than a `stay_home` player
    on a `help_aggression = 10` team (the team knob and the player
    directive compose; neither overrides the other). A helper with no
    directive row, or a directive row whose `defense_mode` was never set,
    reads as `DefenseMode.BALANCED`, whose offset is `0.0` -- today's
    behaviour, unchanged."""
    cfg = tuning.help
    weighted_sum = (
        helper.ratings.scheme_discipline * cfg.gate_scheme_discipline_weight
        + help_aggression * cfg.gate_help_aggression_weight
        + helper.tendencies.help_gamble_freq * cfg.gate_help_gamble_weight
    )
    weight_total = (
        cfg.gate_scheme_discipline_weight
        + cfg.gate_help_aggression_weight
        + cfg.gate_help_gamble_weight
    )
    # Center on the ratings/tendencies domains' shared 0-99ish midpoint so
    # `gate_diff == 0` (and therefore `p_attempt == gate_base_rate`) means
    # "a perfectly average helper on a perfectly average scheme."
    gate_diff = weighted_sum / weight_total - 50.0
    defense_mode = (
        helper.directive.defense_mode
        if helper.directive is not None and helper.directive.defense_mode is not None
        else DefenseMode.BALANCED
    )
    gate_diff += cfg.defense_mode_gate_offset[defense_mode]
    return contest_probability(
        gate_diff, 0.0, base_rate=cfg.gate_base_rate, scale=cfg.gate_scale, band=cfg.gate_band
    )


def _require_defense_mode_gate_offset_is_total(tuning: TuningProfile) -> None:
    """Mutation 14: every `DefenseMode` member must have an offset entry.
    Checked with a real `raise` -- an `assert` is stripped by `python -O`,
    and the `KeyError` it would have prevented arrives mid-possession,
    not at import."""
    table = tuning.help.defense_mode_gate_offset
    missing = sorted(v.value for v in set(DefenseMode) - set(table))
    if missing:
        raise RuntimeError(f"defense_mode_gate_offset is missing entries for: {missing}")


_require_defense_mode_gate_offset_is_total(DEFAULT_TUNING)


def resolve_help_beat(
    driver: SimPlayer,
    helper: SimPlayer,
    *,
    help_aggression: int,
    tuning: TuningProfile,
    rng: Random,
    clock_seconds: float,
    home_score: int,
    away_score: int,
    helper_in_foul_trouble: bool = False,
) -> HelpBeatResult:
    """§4.4's Help row, gated per the module docstring on `_gate_probability`.

    Given an attempt, quality (`HELP_COMMITTED` vs `HELP_LATE`) is decided
    by the plain ability-vector contest (`finishing_rim`/`contact_finishing`/
    `floater`/`draw_foul` on `driver` vs `rotation_speed`/`help_iq`/
    `rim_protection`/`vertical`/`wingspan_in` on `helper`), with
    `help_gamble_freq`† applying a fixed penalty to the helper's side of
    that contest above a neutral midpoint -- a gambler who does help is
    modeled as more likely to be out of position when they get there, not
    just more likely to help in the first place. `scheme_discipline` only
    ever enters the gate, never this quality contest, per §4.4's own
    wording ("gate the *decision* to help").

    B4 fix (critic-proven): `helper_in_foul_trouble` (§4.6: "foul trouble
    ... reduces the fouler's help-commit probability," `state.
    PlayerGameState.is_in_foul_trouble`, read by the caller and passed in
    here -- this beat itself never reaches into `state.py`) applies a
    second fixed penalty to the same side of the same quality contest,
    same mechanism and shape as the `help_gamble_freq` penalty above,
    shifting mass from `HELP_COMMITTED` toward `HELP_LATE` when true. It
    does not touch `_gate_probability` -- foul trouble makes a helper more
    tentative when they do engage, not less willing to engage at all,
    which stays governed by `scheme_discipline`/`help_aggression`/
    `help_gamble_freq` alone, per §4.4's own gate/quality split.

    Reports a single 3-way probability distribution
    (`help_stayed`/`help_committed`/`help_late`) and draws the outcome
    with one `weighted_choice` call, rather than two sequential Bernoulli
    draws, keeping this beat's RNG usage the same "one draw" shape as
    every other beat in the chain."""
    cfg = tuning.help
    p_attempt = _gate_probability(helper, help_aggression=help_aggression, tuning=tuning)

    offense_score = ability_score(driver, cfg.offense_weights)
    defense_score = ability_score(helper, cfg.defense_weights)
    gamble_penalty = (
        max(helper.tendencies.help_gamble_freq - 50.0, 0.0)
        * cfg.quality_help_gamble_penalty_per_point
    )
    foul_trouble_penalty = cfg.foul_trouble_help_commit_penalty if helper_in_foul_trouble else 0.0
    p_committed_given_attempt = contest_probability(
        defense_score - gamble_penalty - foul_trouble_penalty,
        offense_score,
        base_rate=cfg.quality_base_rate,
        scale=cfg.quality_scale,
        band=cfg.quality_band,
    )

    p_committed = p_attempt * p_committed_given_attempt
    p_late = p_attempt * (1.0 - p_committed_given_attempt)
    p_stayed = 1.0 - p_attempt

    probabilities = {
        HelpOutcome.HELP_STAYED.value: p_stayed,
        HelpOutcome.HELP_COMMITTED.value: p_committed,
        HelpOutcome.HELP_LATE.value: p_late,
    }
    outcome = HelpOutcome(weighted_choice(rng, probabilities))
    record = BeatRecord(
        beat="help",
        actor_ids={"driver": driver.id, "helper": helper.id},
        outcome=outcome.value,
        probabilities=probabilities,
        clock_seconds=clock_seconds,
        home_score=home_score,
        away_score=away_score,
    )
    return HelpBeatResult(outcome=outcome, probabilities=probabilities, record=record)
