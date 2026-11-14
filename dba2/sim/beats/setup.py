"""Beat 1: strategy + personnel -> action type + role assignment
(phase-1-simulation-engine.md §4.3 table, Decision P1-7/P1-8/P1-9).

Zero I/O, per Decision P1-1.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from random import Random

from dba2.domain.role import UsageMode
from dba2.domain.team import OffensiveScheme
from dba2.sim.composites import passer_score
from dba2.sim.context import SimPlayer
from dba2.sim.events import BeatRecord
from dba2.sim.playbook import (
    ROLE_TEMPLATES,
    ActionType,
    RoleAssignment,
    RoleSlot,
    assign_matchups,
    defense_roles_from_matchups,
)
from dba2.sim.probability import weighted_choice
from dba2.sim.tuning import DEFAULT_TUNING, TuningProfile

# Decision P1-8 names 3 of these explicitly (pnr_ball_freq/iso_freq/
# transition_freq for the ball-handler role, matched to the action type).
# `player_tendencies` (domain/ratings.py) has no `dho_freq` or `post_freq`
# column, so `DHO`/`POST_UP` fall back to the nearest available proxy
# (`pnr_ball_freq` down-weighted by `tuning.setup.dho_share_of_pnr`, and
# `shot_freq_post` respectively) -- an interpretive call, flagged in this
# dispatch's build report, not a value the design doc names.
_ACTION_TYPE_TENDENCY_FIELD: dict[ActionType, str] = {
    ActionType.PNR_BALL: "pnr_ball_freq",
    ActionType.DHO: "pnr_ball_freq",
    ActionType.ISO: "iso_freq",
    ActionType.POST_UP: "shot_freq_post",
    ActionType.OFF_BALL_SCREEN: "cut_freq",
    ActionType.TRANSITION: "transition_freq",
    ActionType.SPOT_UP: "spot_up_freq",
}

_BALL_HANDLER_TENDENCY_FIELD: dict[ActionType, str] = {
    ActionType.PNR_BALL: "pnr_ball_freq",
    ActionType.DHO: "pnr_ball_freq",
    ActionType.ISO: "iso_freq",
    ActionType.TRANSITION: "transition_freq",
}

# Roles with no ball-handler/screener special case (below) read a single
# tendency field directly. Another interpretive extension of Decision
# P1-8, which only names the ball-handler/screener/shooter cases
# explicitly -- the remaining role names have no closer-named tendency,
# so each maps to the nearest available field.
_ROLE_TENDENCY_FIELD: dict[RoleSlot, str] = {
    RoleSlot.WEAK_SIDE_SHOOTER: "spot_up_freq",
    RoleSlot.CORNER_SHOOTER: "spot_up_freq",
    RoleSlot.SHOOTER: "spot_up_freq",
    RoleSlot.PASSER: "pass_freq",
    RoleSlot.ENTRY_PASSER: "pass_freq",
    RoleSlot.POST_SCORER: "shot_freq_post",
    RoleSlot.CUTTER: "cut_freq",
    RoleSlot.TRAILER: "transition_freq",
    RoleSlot.WING_RUNNER: "transition_freq",
}

# PD-5 (docs/design/gm-command-surface.md): `player.directive.usage_mode`'s
# role-**family** grouping -- five families, not all twelve `RoleSlot`
# members individually, "the same size as `defense_outcome_shares`"
# (PD-5's own wording). **Must be total over every OFFENSIVE `RoleSlot`
# member `_role_weight` can actually be called with** -- derived below
# from `ROLE_TEMPLATES` itself (every `RoleTemplate.offense` entry, the
# real, structural ground truth for "which roles `_role_weight` is ever
# invoked for", per `assign_offense_roles`'s own per-role loop below)
# rather than hand-copied from `RoleSlot`'s own `# offense` comment, which
# nothing enforces stays in sync with `ROLE_TEMPLATES`. Pinned total at
# import by
# `_require_role_family_map_is_total_over_offensive_roleslots` below --
# a missing slot would otherwise be a `KeyError` inside a possession, not
# a startup failure.
_ROLE_FAMILY: dict[RoleSlot, str] = {
    RoleSlot.BALL_HANDLER: "on_ball",
    RoleSlot.SCREENER: "screening",
    RoleSlot.HANDOFF_BIG: "screening",
    RoleSlot.WEAK_SIDE_SHOOTER: "spacing",
    RoleSlot.CORNER_SHOOTER: "spacing",
    RoleSlot.SHOOTER: "spacing",
    RoleSlot.POST_SCORER: "post",
    RoleSlot.CUTTER: "movement",
    RoleSlot.TRAILER: "movement",
    RoleSlot.WING_RUNNER: "movement",
    RoleSlot.PASSER: "movement",
    RoleSlot.ENTRY_PASSER: "movement",
}

_OFFENSIVE_ROLE_SLOTS = frozenset(
    role for template in ROLE_TEMPLATES.values() for role in template.offense
)


def _action_type_weights(
    offense_lineup: tuple[SimPlayer, ...],
    tuning: TuningProfile,
    *,
    offensive_scheme: OffensiveScheme | None = None,
) -> dict[ActionType, float]:
    """`offensive_scheme` (PD-10, docs/design/gm-command-surface.md):
    blend, not replace (PD-10.2) -- multiplies the existing tendency-driven
    weight by `tuning.setup.offensive_scheme_action_multipliers[scheme]`
    rather than overriding it. `None` (the default, and every caller's
    behaviour before PD-10) means no scheme factor is applied AT ALL --
    not a lookup into a neutral row, since `OffensiveScheme` has no neutral
    member (PD-10.4). Called from TWO places
    (`select_action_type`'s draw, and `resolve_setup_beat`'s `probabilities`
    recompute for the persisted `BeatRecord`) -- **both must be passed the
    same scheme**, or the stored record's probabilities silently lie about
    the distribution the draw actually used (PD-10.1's named trap,
    mutation 19)."""
    weights: dict[ActionType, float] = {}
    scheme_row = (
        tuning.setup.offensive_scheme_action_multipliers[offensive_scheme]
        if offensive_scheme is not None
        else None
    )
    for action_type in ActionType:
        base = tuning.setup.action_type_base_weight[action_type.value]
        field = _ACTION_TYPE_TENDENCY_FIELD[action_type]
        tendency_mean = sum(getattr(p.tendencies, field) for p in offense_lineup) / len(
            offense_lineup
        )
        weight = base * max(tendency_mean, 0.0)
        if action_type is ActionType.DHO:
            weight *= tuning.setup.dho_share_of_pnr
        if scheme_row is not None:
            weight *= scheme_row[action_type.value]
        weights[action_type] = weight
    return weights


def select_action_type(
    offense_lineup: tuple[SimPlayer, ...],
    tuning: TuningProfile,
    rng: Random,
    *,
    offensive_scheme: OffensiveScheme | None = None,
) -> ActionType:
    """Decision P1-8: one weighted draw over the 7 action types, tilted by
    the on-court offensive lineup's mean tendency for each type (see
    `_ACTION_TYPE_TENDENCY_FIELD`) against `tuning.setup`'s base rates, and
    by `offensive_scheme` (PD-10) when one is given -- see
    `_action_type_weights`'s own docstring."""
    weights = _action_type_weights(offense_lineup, tuning, offensive_scheme=offensive_scheme)
    return weighted_choice(rng, weights)


def _overall_to_touch_share_estimate(overall_cached: float, tuning: TuningProfile) -> float:
    """B3 fix: the no-role-row fallback used to be `overall_cached / 99.0`
    (~0.35-1.0), compared directly against a real `player_roles.
    touch_share` (~0.10-0.35, `NUMERIC(5,4)`) in the same weighted-draw --
    critic proved this let roleless bench players systematically out-draw
    a real starter for ball-handling/role selection. This linearly
    interpolates between `tuning.setup`'s two documented anchor points
    (`overall_fallback_anchor_low`/`_high`) instead, landing the fallback
    on `touch_share`'s own numeric range, and clamps outside the anchor
    range the same way `composites.normalize_physical` clamps outside its
    own table -- a 30-overall player floors at the low anchor's share
    rather than extrapolating toward zero, a 99-overall player caps at the
    high anchor's share rather than extrapolating past what a real
    touch_share reaches."""
    (lo_overall, lo_share) = tuning.setup.overall_fallback_anchor_low
    (hi_overall, hi_share) = tuning.setup.overall_fallback_anchor_high
    if overall_cached <= lo_overall:
        return lo_share
    if overall_cached >= hi_overall:
        return hi_share
    t = (overall_cached - lo_overall) / (hi_overall - lo_overall)
    return lo_share + t * (hi_share - lo_share)


def _ball_handler_passing_concentration(player: SimPlayer, tuning: TuningProfile) -> float:
    """Assist-compression fix (season-level analysis, this dispatch): a
    second, independent multiplier on the `BALL_HANDLER` role's weight,
    on top of the existing `overall_cached` scale in `_role_weight` below
    -- see `SetupTuning.ball_handler_passing_ability_anchor_low/_high`'s
    own comment for why this exists and why it's scoped to this one role.
    The raw anchor interpolation is the same linear-interpolation-between-
    two-anchors shape as `_overall_to_touch_share_estimate` above, clamped
    the same way outside the anchor range.

    Usage-dampening fix (critic-proven regression in the anchor widening
    above): the raw multiplier used to apply in full regardless of how
    much the ball this player actually sees, which let a no-role/low-
    overall bench player's merely-good (not elite) passing score pull a
    bigger multiplier than a real high-usage starter's, inverting the
    usage/overall hierarchy `_overall_to_touch_share_estimate`'s own fix
    exists to protect (confirmed on a real generated 420-player league,
    seed 1: a 62-overall bench player outweighed an 82-overall teammate for
    BALL_HANDLER selection). `usage_ratio` reads this player's own
    `overall_cached`-estimated usage (`_overall_to_touch_share_estimate`
    above -- **RC-20**, `docs/design/auto-gm-role-construction.md`: no
    longer a real `touch_share_f` for a role-carrying player either, since
    `player_roles.touch_share` is a raw share of a team's 240 minutes and
    was never a usage/involvement signal, and reading it here duplicated
    that same unit collision `_role_weight`'s own scale had) against
    `overall_fallback_anchor_high`'s own share (0.32 -- reusing that
    anchor's documented "a real high-usage starter's" bar rather than
    declaring a new, possibly-drifting constant), so a player already at or
    above a genuine starter's estimated usage gets the multiplier at full
    strength, while a bench-level one gets a steeply reduced share of it --
    `ball_handler_passing_usage_dampening_power`'s cube is what actually
    restores the star-vs-bench invariant at *this* anchor pair's ceiling
    (linear/quadratic dampening still narrowly inverted it on the same
    fixture -- see `SetupTuning`'s own comment for the measured numbers at
    each power). Confirmed this does not meaningfully cost the fix's
    original goal: the same 420-player league's best passer's measured
    season apg is within noise of the undamped version (a real generated
    starter's estimated usage already sits close to or above the 0.32 bar,
    so dampening barely touches them -- it is the no-role/low-usage players
    the dampening is aimed at).

    `docs/design/per-36-spread-compression.md` SC-7 Commit 3: the raw
    `score` line below is now `dba2.sim.composites.passer_score` (factored
    out, not restated) so `beats/rotation.py`'s own passer-ability tilt
    (SC-3.5) reads the identical formula rather than a second, possibly-
    drifting copy of it."""
    score = passer_score(player.ratings)
    (lo_score, lo_mult) = tuning.setup.ball_handler_passing_ability_anchor_low
    (hi_score, hi_mult) = tuning.setup.ball_handler_passing_ability_anchor_high
    if score <= lo_score:
        raw_mult = lo_mult
    elif score >= hi_score:
        raw_mult = hi_mult
    else:
        t = (score - lo_score) / (hi_score - lo_score)
        raw_mult = lo_mult + t * (hi_mult - lo_mult)

    usage_bar = tuning.setup.overall_fallback_anchor_high[1]
    estimated_usage = _overall_to_touch_share_estimate(player.player.overall_cached, tuning)
    usage_ratio = min(estimated_usage / usage_bar, 1.0)
    usage_weight = usage_ratio**tuning.setup.ball_handler_passing_usage_dampening_power
    return 1.0 + (raw_mult - 1.0) * usage_weight


def _role_weight(
    role: RoleSlot, player: SimPlayer, action_type: ActionType, tuning: TuningProfile
) -> float:
    """**`player.directive.usage_mode` is read here (PD-5, docs/design/
    gm-command-surface.md)** -- a role-family multiplier on `base`,
    chosen over `scale` as a legibility placement, not a behavioural one
    (PD-5's own proof: the two forms are bit-identical up to float-ulp
    reassociation noise, since multiplication commutes). A player with no
    directive row, or a directive row whose `usage_mode` was never set,
    reads as `UsageMode.BALANCED`, whose every family multiplier is `1.0`
    -- today's behaviour, unchanged. **Unguarded by PD-9's read allowlist**
    (R10's own disclosed limit): `setup.py` is `test_tuning.py`'s
    structural `_UNRESTRICTED_BEAT_FILENAMES` carve-out (Decision P1-8),
    which predates this directive and was never scoped to exclude it."""
    if role is RoleSlot.BALL_HANDLER:
        field = _BALL_HANDLER_TENDENCY_FIELD[action_type]
        base = getattr(player.tendencies, field)
        base *= _ball_handler_passing_concentration(player, tuning)
    elif role in (RoleSlot.SCREENER, RoleSlot.HANDOFF_BIG):
        # Decision P1-8: "the screener by pnr_screen_freq and
        # screen_setting" -- tendency times ability, normalized to [0, 1].
        base = player.tendencies.pnr_screen_freq * (player.ratings.screen_setting / 99.0)
    else:
        base = getattr(player.tendencies, _ROLE_TENDENCY_FIELD[role])

    usage_mode = (
        player.directive.usage_mode
        if player.directive is not None and player.directive.usage_mode is not None
        else UsageMode.BALANCED
    )
    family = _ROLE_FAMILY[role]
    base *= tuning.setup.usage_mode_family_multipliers[usage_mode][family]

    # RC-20 (docs/design/auto-gm-role-construction.md): this used to read a
    # role-carrying player's real `touch_share_f` here and fall back to
    # `_overall_to_touch_share_estimate` only for a roleless player --
    # `player_roles.touch_share` and this scale are not the same unit
    # (`touch_share` is a raw share of a team's 240 minutes; this is an
    # involvement/usage estimate `_ball_handler_passing_concentration`
    # above also reads), and `747eb30`'s star-vs-bench cube was tuned
    # against exactly this `overall_cached`-derived scale, not against a
    # written `touch_share`. Reading `touch_share_f` here is now `usage_
    # mode`'s job (PD-5, docs/design/gm-command-surface.md) -- see this
    # function's own docstring above -- so this scale is unconditionally
    # `overall_cached`-derived, for every player, role-carrying or not.
    scale = max(
        _overall_to_touch_share_estimate(player.player.overall_cached, tuning),
        tuning.setup.min_overall_scale,
    )
    return max(base, 0.0) * scale


def _require_role_family_map_is_total_over_offensive_roleslots() -> None:
    """Mutation 14's role-family half: every `RoleSlot` `_role_weight` can
    actually be called with (per `ROLE_TEMPLATES`, not `RoleSlot`'s own
    unenforced `# offense` comment) must have a `_ROLE_FAMILY` entry.
    Checked with a real `raise` -- an `assert` is stripped by `python -O`,
    and the `KeyError` it would have prevented arrives mid-possession."""
    missing = sorted(role.value for role in _OFFENSIVE_ROLE_SLOTS - set(_ROLE_FAMILY))
    if missing:
        raise RuntimeError(f"_ROLE_FAMILY is missing offensive RoleSlot member(s): {missing}")


def _require_usage_mode_family_multipliers_is_total(tuning: TuningProfile) -> None:
    """Every `UsageMode` member must have a full five-family entry --
    checked with a real `raise` for the identical reason the role-family
    map's own pin is."""
    table = tuning.setup.usage_mode_family_multipliers
    families = set(_ROLE_FAMILY.values())
    missing_modes = sorted(v.value for v in set(UsageMode) - set(table))
    if missing_modes:
        raise RuntimeError(f"usage_mode_family_multipliers is missing entries for: {missing_modes}")
    for mode, weights in table.items():
        missing_families = sorted(families - set(weights))
        if missing_families:
            raise RuntimeError(
                f"usage_mode_family_multipliers[{mode.value!r}] is missing "
                f"family(ies): {missing_families}"
            )


def _require_offensive_scheme_action_multipliers_are_total_and_positive(
    tuning: TuningProfile,
) -> None:
    """PD-10.7: every `OffensiveScheme` member must have an entry, every
    entry must cover every `ActionType` value, and every cell must be
    strictly positive -- the identical `shot_diet_zone_multipliers`/
    `usage_mode_family_multipliers` shape, checked with a real `raise` (an
    `assert` is stripped by `python -O`, and the `KeyError` it prevents
    otherwise arrives mid-possession).

    The required action-type key set is derived from `ActionType` ITSELF
    (`{a.value for a in ActionType}`), never a hardcoded list of seven
    strings -- a deliberate strengthening over PD-3's own pin, which
    hardcodes `{"rim", "mid", "three"}`: an eighth action type added later
    fails loudly here, where PD-3's shape would let a new zone through
    silently."""
    table = tuning.setup.offensive_scheme_action_multipliers
    required_actions = {a.value for a in ActionType}
    missing_schemes = sorted(v.value for v in set(OffensiveScheme) - set(table))
    if missing_schemes:
        raise RuntimeError(
            f"offensive_scheme_action_multipliers is missing entries for: {missing_schemes}"
        )
    non_positive: list[str] = []
    for scheme, weights in table.items():
        missing_actions = sorted(required_actions - set(weights))
        if missing_actions:
            raise RuntimeError(
                f"offensive_scheme_action_multipliers[{scheme.value!r}] is missing "
                f"action type(s): {missing_actions}"
            )
        non_positive.extend(
            f"{scheme.value}.{action}={value!r}"
            for action, value in weights.items()
            if value <= 0.0
        )
    if non_positive:
        raise RuntimeError(
            "offensive_scheme_action_multipliers must be strictly positive everywhere -- "
            f"got: {non_positive}"
        )


_require_role_family_map_is_total_over_offensive_roleslots()
_require_usage_mode_family_multipliers_is_total(DEFAULT_TUNING)
_require_offensive_scheme_action_multipliers_are_total_and_positive(DEFAULT_TUNING)


def assign_offense_roles(
    action_type: ActionType,
    offense_lineup: tuple[SimPlayer, ...],
    tuning: TuningProfile,
    rng: Random,
) -> dict[RoleSlot, int]:
    """Decision P1-8: sequential weighted selection, each role's draw
    removing its pick from the pool the next role draws from -- the
    mechanism that guarantees "every declared role is filled by a
    distinct on-court player" (§12 step 2's verify bullet) by
    construction, not by a post-hoc distinctness check."""
    template = ROLE_TEMPLATES[action_type]
    if len(offense_lineup) < len(template.offense):
        raise ValueError(
            f"{action_type.value} needs {len(template.offense)} offense players, "
            f"got {len(offense_lineup)}"
        )
    remaining = list(offense_lineup)
    assigned: dict[RoleSlot, int] = {}
    for role in template.offense:
        weights = {p.id: _role_weight(role, p, action_type, tuning) for p in remaining}
        chosen_id = weighted_choice(rng, weights)
        assigned[role] = chosen_id
        remaining = [p for p in remaining if p.id != chosen_id]
    return assigned


@dataclass(frozen=True)
class SetupBeatResult:
    action_type: ActionType
    role_assignment: RoleAssignment
    matchups: dict[int, int]
    seconds_consumed: float
    record: BeatRecord


def resolve_setup_beat(
    offense_lineup: tuple[SimPlayer, ...],
    defense_lineup: tuple[SimPlayer, ...],
    tuning: TuningProfile,
    rng: Random,
    *,
    clock_seconds: float,
    home_score: int,
    away_score: int,
    seconds_consumed: float = 6.0,
    matchups: Mapping[int, int] | None = None,
    offensive_scheme: OffensiveScheme | None = None,
) -> SetupBeatResult:
    """Beat 1, end to end: matchups (Decision P1-9) -> action type
    (Decision P1-8) -> offense role assignment -> defense role assignment
    derived from the matchups (`playbook.defense_roles_from_matchups`).

    B2 fix (critic-proven): Decision P1-9 assigns matchups **once per
    possession**, and Decision P1-10 says a `SWITCH` persists "for the
    remainder of that possession only." `possession.py`'s chain loop calls
    this function again on every secondary read (a `CONTAINED` on-ball
    result reselects a new action/role assignment "chosen by the same
    weighted selection as beat 1," per §4.3's Decision P1-11 text), and
    the old code called `assign_matchups` fresh on every one of those
    calls, silently reverting any `apply_switch` mutation the moment a
    possession continued. `matchups`, when given, is used as-is instead of
    recomputing it -- `possession.py` calls `assign_matchups` itself
    exactly once, at the true start of beat 1, and threads its own live
    (possibly switch-mutated) matchup dict into every subsequent call in
    the same possession. `matchups=None` is only valid for that first-ever
    call, where there is no prior possession state to preserve.

    `offensive_scheme` (PD-10): forwarded to **both** `_action_type_weights`
    call sites below -- the draw (via `select_action_type`) and the
    `probabilities` recompute for the persisted `BeatRecord`. Passing it to
    only one would ship a beat log whose stated probabilities do not match
    the distribution the draw actually used (PD-10.1's named trap,
    mutation 19)."""
    if len(offense_lineup) != 5 or len(defense_lineup) != 5:
        raise ValueError("setup beat requires a full 5-player lineup on both sides")

    resolved_matchups = (
        dict(matchups) if matchups is not None else assign_matchups(offense_lineup, defense_lineup)
    )
    action_type = select_action_type(
        offense_lineup, tuning, rng, offensive_scheme=offensive_scheme
    )
    offense_roles = assign_offense_roles(action_type, offense_lineup, tuning, rng)
    defense_roles = defense_roles_from_matchups(
        action_type, offense_roles, resolved_matchups, defense_lineup
    )
    role_assignment = RoleAssignment(
        action_type=action_type, offense=offense_roles, defense=defense_roles
    )

    weights = _action_type_weights(offense_lineup, tuning, offensive_scheme=offensive_scheme)
    total = sum(weights.values())
    probabilities = (
        {at.value: w / total for at, w in weights.items()}
        if total > 0
        else {at.value: 1.0 / len(weights) for at in weights}
    )

    actor_ids = {f"offense:{role.value}": pid for role, pid in offense_roles.items()}
    actor_ids.update({f"defense:{role.value}": pid for role, pid in defense_roles.items()})

    record = BeatRecord(
        beat="setup",
        actor_ids=actor_ids,
        outcome=action_type.value,
        probabilities=probabilities,
        clock_seconds=clock_seconds,
        home_score=home_score,
        away_score=away_score,
    )
    return SetupBeatResult(
        action_type=action_type,
        role_assignment=role_assignment,
        matchups=resolved_matchups,
        seconds_consumed=seconds_consumed,
        record=record,
    )
