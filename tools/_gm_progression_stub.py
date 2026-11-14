"""Harness-only "age one season" stub (Decision P2-23,
docs/design/phase-2-manager-minds.md §3, §12 step 10) -- the same
quarantine `tools/_gm_draft_class_stub.py` (Decision P2-32) established;
copied here now that a real sibling exists to copy the pattern from
directly, rather than being established fresh the way that file's own
docstring notes it was ("no existing sibling to copy from directly" --
stale as of this dispatch; see that file's own docstring, updated
alongside this one, for the corrected phrasing).

**What this stub does, stated so its limits are not mistaken for
results** (design doc's own words, quoted in full, Decision P2-23 §3):
"ages every player one year, nudges ratings toward `potential` below
`peak_age_start` and away above `peak_age_end`, expires contracts, and
retires players past 40 -- crude on purpose, and quarantined so that its
crudeness can never leak into the real system or be mistaken for the
progression design." That sentence is the entire design-doc spec; every
mechanic below is this dispatch's own interpretation of it, stated
explicitly wherever the design doc is silent -- same posture the
draft-class stub's own docstring takes.

**Scope -- only rostered/free-agent players are aged (interpretation
call).** `RosterStatus.ACTIVE`/`TWO_WAY`/`INJURED`/`FREE_AGENT` players
are aged; `DRAFT_ELIGIBLE` prospects and already-`RETIRED` players pass
through with every field unchanged. A prospect is not "aged" by this
stub until the harness's own draft mechanics (not built this dispatch)
actually draft them -- this stub's sibling, `_gm_draft_class_stub.py`,
owns prospect generation entirely separately, so double-aging a
not-yet-drafted prospect here would be this stub reaching into a
responsibility that is not its own.

**Rating nudge mechanics (interpretation call -- the design doc names
only the two-sided "nudge toward/away from potential," no field-level
mechanics).** A single scalar `delta` per player per season --
`+_GROWTH_STEP_FRAC * (potential - overall)` (floored at 0.0; see
below) below `peak_age_start`, `-_DECLINE_STEP` above `peak_age_end`,
`0.0` in the prime window -- applied *uniformly* to every one of the 53
public rating fields (`sim.derivation.RATING_FIELD_ORDER`'s 55-field
set, minus `potential`/`potential_variance` -- see "53, not 55" below),
each independently clamped to `[0, 99]`. Applying one scalar to every
field preserves a player's individual strengths/weaknesses shape (a
shooter stays relatively a better shooter than a defender as they age)
rather than resampling a fresh random profile every season through
`synthetic_ratings`, which would erase that shape entirely and be a much
cruder, far less defensible choice for a function whose whole job is
*aging* an existing player, not regenerating one. `potential`,
`potential_variance`, `peak_age_start`, `peak_age_end` are the
target/shape the nudge moves *toward*, not moving state themselves, so
none of the four are touched.

**53, not 55 (a literal-text discrepancy in this dispatch's own task
description, resolved here).** The task text says "apply delta
uniformly to all 55 rating fields" and, two lines later, excludes
`potential`/`potential_variance` from that same nudge -- two of the 55
excluded from "all 55" is not literally all 55. Read together with
`sim.ratings.compute_overall`'s own docstring ("the 53 public rating
fields -- the 55 non-age rating fields minus the 2 ... hidden:
potential/potential_variance"), the only internally-consistent reading
is the 53-field carrier set, matching `compute_overall`'s own
public/hidden split exactly. `peak_age_start`/`peak_age_end` were never
members of `RATING_FIELD_ORDER`'s 55 to begin with (`_RatingColumns`'s
own docstring separates them out as the schema's 2 plain-SMALLINT age
columns, distinct from the 55-field `rating`-domain block) -- their own
"not touched" mention in the task text is confirmatory, not a third
exclusion on top of the 53.

**Growth floored at 0.0 (task text's own stated reason, restated here).**
`potential`'s own generator has no age term (`scouting.py`'s Gap-E
docstring) -- a prospect's drawn `potential` can, rarely, land below
their drawn `overall`, which would otherwise back a negative "growth"
step for a player who is still below `peak_age_start`. Flooring at 0.0
means that rare draw simply produces no movement that season rather than
an incoherent downward nudge inside what is supposed to be the growth
phase; documented as an expected edge case of the upstream generator,
not a bug in this stub.

**RPD-6 (docs/design/real-player-data-integration.md): real-seeded
players get a real-trajectory delta instead of the synthetic mechanic
above, additive only.** `Player.external_ref is not None` is the
lifetime marker for "this is a real player" -- not `PlayerRatings.
source`, which gets overwritten to `RatingSource.PROGRESSION` the very
first time any player is aged (`tools/advance_dev_league_season.py`), so
keying the real-trajectory branch on `source` would silently stop firing
after one season. For each such player whose real career
(`tools/_gm_real_trajectory.py::expected_delta_at_age`) brackets their
current in-sim age, the per-field delta is their own real season-over-
season rating change at that age -- not one scalar applied uniformly
like the synthetic mechanic, since a real player's own strengths/
weaknesses shape is already measured, not something this stub needs to
preserve through a uniform nudge. That expected delta is then scaled by
a fit/success/minutes-derived multiplier (`_REAL_FIT_MULTIPLIER_LO`..
`_REAL_FIT_MULTIPLIER_HI`, caller-supplied via `RealPlayerContext` --
absent for a player with no computed context yet, which multiplies by
exactly 1.0: track the real arc exactly, the correct default when there
is no divergence signal), **then by Decision P4-50's separate morale
trim** (`MORALE_TRIM_SPAN`, its own docstring has the formula -- neutral
or absent morale trims by exactly `1.0` too, so the disclosed `[0.5,
1.5]` multiplier band above widens to `[0.5 * (1 - MORALE_TRIM_SPAN),
1.5 * (1 + MORALE_TRIM_SPAN)]` only once morale is genuinely
non-neutral), and perturbed by RPD-2's variance (a small
per-field Gaussian draw from `rng_for(seed, "progression", "real_
variance", player_id, season)` -- the first real use of this function's
`seed` parameter). A real player whose trajectory data doesn't bracket
their current age (before their first real season on file, or at/past
their last) falls through to the exact same synthetic mechanic every
other player uses -- RPD-3's "not a hard cap" decision, restated in
`_gm_real_trajectory.py`'s own docstring.

**Retirement (age > 40) skips the nudge, not the aging.** A player who
crosses 40 this season is returned `RETIRED`/`retired_in_season =
season` with their ratings untouched (no point nudging a player out of
the league), but `years_pro` still increments -- they were still aged
this season, just aged *out*.

**Contract handling, two branches (interpretation calls -- the design
doc says only "expires contracts," nothing about downstream roster/team
state).**

1. *Natural expiry* (`contract.end_season < season`, non-retiring
   player): the sensible reading of "expires" is that the player becomes
   a free agent -- nothing else in this crude stub would otherwise ever
   move a player off an expired contract, and leaving them `ACTIVE` on a
   phantom still-attached team would be a worse fiction than the one
   this stub already knowingly commits to elsewhere. `contract = None`,
   `team_id = None`, `roster_status = FREE_AGENT`.
2. *Retirement* (age > 40, any contract still `ACTIVE` regardless of its
   own `end_season`): the same "no longer holds an NBA contract" state
   applies unconditionally -- a retiring player's remaining contract
   term is irrelevant once they've left the league. `RETIRED` (from the
   retirement branch) takes precedence over `FREE_AGENT` here; a player
   is never returned in both states.

Neither branch generates a `dead_cap_charges` row (a *named non-goal* --
that ledger is `contract_repo`'s own real mechanic, `domain/contract.py`'s
`DeadCapCharge`, entirely out of scope for an in-memory harness stub with
no repository to write to). A contract still inside its term
(`end_season >= season`, non-retiring player) is left completely
untouched by this function -- no partial per-year contract-year
bookkeeping (`ContractYear` rows, raises, option decisions) either,
another named non-goal; that is real cap-sheet machinery
(`contract_repo`/`capsheet_service`), not this stub's job.

**Deviation from the task's literal text: no intermediate `EXPIRED`
`Contract` object is built.** The task description asks for a new
`Contract` via `dataclasses.replace(..., status=ContractStatus.EXPIRED)`
to be constructed before setting `RosterPlayer.contract = None`. Built
that way, the object would be unreachable dead code: `RosterPlayer` (and
its `PlayerContract` wrapper) have no field to carry a lapsed/historical
contract forward once `contract` itself is `None` -- there is nothing to
assign it to and, being a pure zero-I/O harness function, no repository
this stub could write it to either (`contract_repo` is the real writer
of `contracts.status`, not built this dispatch). Constructing it just to
discard it on the next line would be lint-clean (ruff's dummy-variable
convention swallows an underscore-prefixed unused-name warning) but
functionally pointless -- so only the actually-observable state
transition (`contract = None`, `team_id = None`, `roster_status` per the
branch above) is implemented. Flagged explicitly rather than silently
dropped.

**`tuning` remains unused by this function's body**, for the reason
originally stated here: its own knobs are not what `_GROWTH_STEP_FRAC`/
`_DECLINE_STEP` below read from (they're this stub's own harness-tool
constants). Kept in the signature anyway, per the draft-class stub's own
"call-site stability over dropping a parameter a given code path doesn't
currently need" convention.

**`seed` is no longer unused -- RPD-6 (docs/design/real-player-data-
integration.md) wires it into a small variance draw for real-seeded
players only** (see `_nudge_ratings`'s real-trajectory branch below,
`tools/_gm_real_trajectory.py`). Synthetic players are entirely
unaffected -- their branch still draws nothing, matching every claim this
docstring made about determinism before RPD-6 existed.

**`_GROWTH_STEP_FRAC = 0.25` / `_DECLINE_STEP = 2.0`: harness-tool
constants, no calibration claim.** This dispatch's own stance, not a
borrowed one -- the draft-class stub's sibling constant, `_PEAK_NOISE_SD
= 3.0`, carries no equivalent "no calibration claim" prose in that
file's docstring either (it is a bare, uncommented module constant
there), so citing it as documented precedent would misattribute a stance
to a passage that doesn't make it (critic NIT 3, prior round). Real
progression curves are Phase 7's job (design doc's own words, quoted
above); these two numbers exist only to make players visibly age within
a multi-season harness run, nothing more.

**Quarantine (Decision P2-23, "same quarantine, same import test" as
the sibling draft-class stub).** Lives in `tools/`, underscore-prefixed,
never imported by anything under `dba2/` -- enforced by
`tests/unit/test_layering.py::
test_nothing_under_dba2_imports_the_gm_progression_stub`. `tools/` is
not `managers/` and carries none of Decision P2-2(b)'s zero-I/O purity
rules in the sense that would require `rng_for` -- moot here anyway,
since this stub ends up importing no RNG at all (see the `seed` note
above).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType

from dba2.core.rng import rng_for
from dba2.domain.player import Player, Position, RosterStatus
from dba2.domain.ratings import PlayerRatings
from dba2.managers.scouting import player_age
from dba2.managers.snapshot import RosterPlayer, build_roster_player
from dba2.managers.tuning import DEFAULT_TUNING, GmTuning
from dba2.sim.derivation import RATING_FIELD_ORDER
from dba2.sim.ratings import compute_overall
from tools._gm_real_trajectory import expected_delta_at_age, real_sourced_fields
from tools._real_fields import REAL_BUILDER_VERSION

_GROWTH_STEP_FRAC = 0.25
_DECLINE_STEP = 2.0
_RETIREMENT_AGE = 40

# RPD-6's fit/success/minutes multiplier range -- a context-free real
# player (no RealPlayerContext supplied) gets exactly 1.0, the midpoint,
# tracking their real arc with no divergence. Judgment call, same
# disclosed-not-calibrated posture as _GROWTH_STEP_FRAC/_DECLINE_STEP
# above: a believability *lever*, not a claim about how much a real NBA
# player's development actually varies with role/fit.
_REAL_FIT_MULTIPLIER_LO = 0.5
_REAL_FIT_MULTIPLIER_HI = 1.5
_REAL_VARIANCE_SD = 0.75

MORALE_TRIM_SPAN: float = 0.2
"""Decision P4-50 (AMENDS P4-19; Phase 4 Stage C step 13b) -- the morale
signal enters as a SEPARATE multiplicative trim, not a fourth term folded
into the `(fit + success + minutes) / 3` mean above: `morale_trim = 1 +
MORALE_TRIM_SPAN * (2 * morale_signal - 1)`, `morale_signal` the
caller's own `[0, 1]`-normalized `player_motivation.morale` reading
(`RealPlayerContext.morale`'s own docstring has the exact `(morale +
100) / 200` normalization). **Neutral morale (`morale_signal = 0.5`)
gives `morale_trim` exactly `1.0`** -- every existing player's
progression is byte-identical until morale is genuinely non-neutral --
and the three tuned fit/success/minutes signals keep their untouched
1/3 weights, unlike P4-19's original (rejected) fourth-mean-term shape.
**`MORALE_TRIM_SPAN = 0.0` disables the mechanism completely**, which is
what makes the targeted integration check below a real control run
(Decision P4-34 requires one), not just a claim. Disclosed, not
calibrated -- same status as `_REAL_FIT_MULTIPLIER_LO`/`HI`/`_REAL_
VARIANCE_SD` above; there is no real-world basis to calibrate a fit/
success/minutes/morale multiplier band against, and this phase does not
acquire one. The disclosed multiplier band correspondingly widens from
`[0.5, 1.5]` to `[0.5 * (1 - MORALE_TRIM_SPAN), 1.5 * (1 +
MORALE_TRIM_SPAN)]`."""

# Caps the raw per-field, per-season delta `tools/_gm_real_trajectory.py`
# reports, before the fit multiplier or variance are applied -- now only
# ever applied to `_REAL_SOURCED_FIELDS` (RPD-6 tuning pass, 2026-09-12:
# `_gm_real_trajectory.py` no longer reports a delta at all for the other
# 38 nudge-eligible fields, which have no real BDL source at any tier and
# were found to be re-derived independently *each season* from that
# season's own percentile/z-score -- not smoothly tracked, and were
# clamp-riding 36.1% of the time versus 13.8% for the 15 real-sourced
# fields on the full 30-season backfill; see that constant's own comment
# for the measurement). This cap now guards only the real-sourced fields'
# occasional genuine outlier season (a real breakout, an injury-shortened
# year) -- confirmed directly on `off_ball_movement`, one of the 38
# excluded fields, which swung 65 -> 93 between two adjacent real ages on
# file purely from that independent re-derivation. Scaled roughly to the
# synthetic mechanic's own typical per-season magnitude
# (`_GROWTH_STEP_FRAC * (potential - overall)` is usually single digits;
# `_DECLINE_STEP` is 2.0) -- a believability cap, not a claim that real
# per-season deltas are actually this small.
_MAX_REAL_FIELD_DELTA = 8.0


@dataclass(frozen=True)
class RealPlayerContext:
    """RPD-2/RPD-6's fit/success/minutes signals for one real player in
    one season, each expected in `[0, 1]` (the caller's job to normalize
    -- this stub does no clamping on the way in, only on the final
    nudged rating). `tools/advance_dev_league_season.py` is the intended
    builder, from `managers/needs.py`'s need/glut matrix (fit),
    `managers/window.py`'s `CompetitiveWindow` (success), and
    `player_season_stats.minutes_played` (minutes)."""

    fit: float
    success: float
    minutes: float
    morale: float | None = None
    """Decision P4-50 (Stage C step 13b): `player_motivation.morale`
    (`[-100, 100]`), normalized to `[0, 1]` by the CALLER before being
    passed here -- `(morale + 100) / 200`, so a neutral `morale == 0`
    reads `morale_signal == 0.5`. `tools/advance_dev_league_season.py`
    is the intended builder, exactly like `fit`/`success`/`minutes`
    above, reading `player_motivation.morale` through the same agency
    checkpoint Decision P4-67's Stage C step 13a wires in. `None` (the
    default -- every pre-Stage-C-step-13b caller, and any real player
    whose motivation bootstrap hasn't reached them yet) means "no
    motivation data," treated as neutral (`morale_signal = 0.5`,
    `morale_trim = 1.0`) -- the same "absent is neutral" convention
    Decision P4-9 establishes for Pillar 5's other caller-supplied
    signals, extended here rather than re-invented."""


_AGED_STATUSES: frozenset[RosterStatus] = frozenset(
    {
        RosterStatus.ACTIVE,
        RosterStatus.TWO_WAY,
        RosterStatus.INJURED,
        RosterStatus.FREE_AGENT,
    }
)

# The 53-field carrier set the nudge is applied to -- RATING_FIELD_ORDER's
# 55 minus `potential`/`potential_variance` (see module docstring's "53,
# not 55" section for why this is the correct reading of the task text).
_UNTOUCHED_RATING_FIELDS: frozenset[str] = frozenset({"potential", "potential_variance"})
_NUDGE_FIELDS: tuple[str, ...] = tuple(
    field_name for field_name in RATING_FIELD_ORDER if field_name not in _UNTOUCHED_RATING_FIELDS
)
assert len(_NUDGE_FIELDS) == 53

# RPD-8-7/8-8: which season rows feed the real trajectory. Version 2
# (`REAL_BUILDER_VERSION`) reads only rebuilt seasons (2009-2025), whose fields
# are `T + offset + 8u`, and trajects the 30 `REAL_FIELDS`; a legacy season's
# `70 + 12*z*w` is never mixed in, since the scale change would read as growth
# (a career with fewer than two rebuilt seasons falls back to the synthetic
# mechanic). This one constant drives BOTH the trajectory and the synthetic
# complement below, because the field list is derived from it. `None` is the
# pre-RPD-8 all-files, legacy-15-field behaviour, kept only for the legacy tests.
_REAL_TRAJECTORY_BUILDER_VERSION: int | None = REAL_BUILDER_VERSION

# RPD-6 tuning pass (2026-09-12): the real-trajectory branch only ever
# reports a delta for the real-sourced fields (see `_gm_real_trajectory.py`);
# a real player's remaining nudge-eligible fields get the same synthetic
# scalar every non-real player's do.
_SYNTHETIC_FIELDS_FOR_REAL_PLAYERS: tuple[str, ...] = tuple(
    field_name
    for field_name in _NUDGE_FIELDS
    if field_name not in real_sourced_fields(_REAL_TRAJECTORY_BUILDER_VERSION)
)
assert len(_SYNTHETIC_FIELDS_FOR_REAL_PLAYERS) == 53 - len(
    real_sourced_fields(_REAL_TRAJECTORY_BUILDER_VERSION)
)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _synthetic_scalar_delta(
    potential: int, overall: int, age: int, peak_age_start: int, peak_age_end: int
) -> float:
    """The single-scalar "nudge toward/away from potential" delta -- see
    module docstring's "Rating nudge mechanics". Factored out of `_nudge_
    ratings` (RPD-6 tuning pass, 2026-09-12) so `_real_trajectory_nudge`
    can apply the exact same formula to a real player's own non-real-
    sourced fields (`_REAL_SOURCED_FIELDS`'s complement) instead of the
    real-trajectory mechanic, which has nothing real to report there."""
    if age < peak_age_start:
        return max(0.0, _GROWTH_STEP_FRAC * (potential - overall))
    if age > peak_age_end:
        return -_DECLINE_STEP
    return 0.0


def progress_one_season(
    players: tuple[RosterPlayer, ...],
    *,
    season: int,
    seed: int,
    tuning: GmTuning = DEFAULT_TUNING,
    real_player_context: Mapping[int, RealPlayerContext] = MappingProxyType({}),
) -> tuple[RosterPlayer, ...]:
    """Ages every `ACTIVE`/`TWO_WAY`/`INJURED`/`FREE_AGENT` player in
    `players` one season into `season`; `DRAFT_ELIGIBLE` prospects and
    already-`RETIRED` players pass through unchanged. One output player
    per input player, same order. See the module docstring for every
    interpretation call and named non-goal.

    `real_player_context` (RPD-6, default empty -- fully additive) maps a
    real player's `Player.id` to their `RealPlayerContext` for this
    season; a real player missing from this mapping still gets the
    real-trajectory delta (if their career data brackets this age) with a
    neutral 1.0 multiplier. `tuning` remains unused (see module
    docstring); `seed` is used only inside the real-trajectory branch."""
    return tuple(
        _progress_player(rp, season, seed, real_player_context.get(rp.player.id))
        if rp.player.roster_status in _AGED_STATUSES
        else rp
        for rp in players
    )


def _progress_player(
    rp: RosterPlayer, season: int, seed: int, context: RealPlayerContext | None
) -> RosterPlayer:
    aged_at = datetime(season, 6, 15, tzinfo=UTC)
    age = player_age(rp.player.birth_date, season)
    is_retiring = age > _RETIREMENT_AGE

    if is_retiring:
        new_ratings = rp.ratings
        new_overall = rp.player.overall_cached
    else:
        new_ratings, new_overall = _nudge_ratings(
            rp.ratings,
            rp.player.primary_position,
            rp.player.overall_cached,
            age,
            aged_at,
            external_ref=rp.player.external_ref,
            player_id=rp.player.id,
            season=season,
            seed=seed,
            context=context,
        )

    contract = rp.contract
    team_id = rp.player.team_id
    roster_status = RosterStatus.RETIRED if is_retiring else rp.player.roster_status

    # Retirement clears roster/team state unconditionally (module
    # docstring: "unconditionally"), independent of whether this player
    # ever held a contract object -- `tools/seed_dev_league.py` seeds
    # every player `ACTIVE` + `team_id` set + `contract=None` (it does not
    # generate contracts), so gating `team_id = None` on `contract_lapsed`
    # (which itself requires `contract is not None`) left a contract-less
    # retiree with a dangling `team_id` (critic-round B1). `contract_
    # lapsed` below still only gates whether there is an actual `Contract`
    # object to expire -- nothing to expire if `contract is None` -- but it
    # no longer gates `team_id`/`roster_status`/`retired_in_season`, which
    # depend only on `is_retiring`.
    if is_retiring:
        team_id = None
    else:
        # Step 4's expiry check for a non-retiring player is gated on
        # end_season < season -- see module docstring's "Contract
        # handling" section, branch 1.
        contract_lapsed = contract is not None and contract.contract.end_season < season
        if contract_lapsed:
            contract = None
            team_id = None
            roster_status = RosterStatus.FREE_AGENT

    if is_retiring and contract is not None:
        # Branch 2 of "Contract handling": a retiring player's remaining
        # contract term is irrelevant once they've left the league --
        # expire it if (and only if) one actually existed.
        contract = None

    new_player = _aged_player(
        rp.player,
        new_overall=new_overall,
        team_id=team_id,
        roster_status=roster_status,
        retiring=is_retiring,
        season=season,
        aged_at=aged_at,
    )
    return build_roster_player(new_player, new_ratings, contract=contract)


def _aged_player(
    player: Player,
    *,
    new_overall: int,
    team_id: int | None,
    roster_status: RosterStatus,
    retiring: bool,
    season: int,
    aged_at: datetime,
) -> Player:
    return replace(
        player,
        years_pro=player.years_pro + 1,
        team_id=team_id,
        roster_status=roster_status,
        overall_cached=new_overall,
        overall_computed_at=aged_at,
        retired_in_season=season if retiring else player.retired_in_season,
        updated_at=aged_at,
    )


def _real_trajectory_nudge(
    ratings: PlayerRatings,
    position: Position,
    overall: int,
    age: int,
    aged_at: datetime,
    *,
    external_ref: str,
    player_id: int,
    season: int,
    seed: int,
    context: RealPlayerContext | None,
) -> tuple[PlayerRatings, int] | None:
    """RPD-6's real-trajectory branch -- see module docstring. Returns
    `None` (not a delta of zero) when this player's real career data
    doesn't bracket `age`, the caller's signal to fall back to `_nudge_
    ratings`'s existing synthetic mechanic for every field. When real
    trajectory data *does* bracket `age`, only `real_sourced_fields(
    _REAL_TRAJECTORY_BUILDER_VERSION)` (the 30 `REAL_FIELDS` at version 2) are
    moved by it (RPD-6 tuning pass, 2026-09-12 -- see `_gm_real_trajectory.
    py`'s `_REAL_SOURCED_FIELDS` comment); this player's remaining
    nudge-eligible fields get the same synthetic scalar `_nudge_ratings`
    would apply to a non-real player at this age, using this player's own
    (real, peak-age-computation-derived) `peak_age_start`/`peak_age_end`."""
    expected_delta = expected_delta_at_age(
        external_ref, age, builder_version=_REAL_TRAJECTORY_BUILDER_VERSION
    )
    if expected_delta is None:
        return None

    fit = context.fit if context is not None else 0.5
    success = context.success if context is not None else 0.5
    minutes = context.minutes if context is not None else 0.5
    combined = _clamp((fit + success + minutes) / 3.0, 0.0, 1.0)
    base_multiplier = _REAL_FIT_MULTIPLIER_LO + (
        _REAL_FIT_MULTIPLIER_HI - _REAL_FIT_MULTIPLIER_LO
    ) * combined

    # Decision P4-50 (AMENDS P4-19; Stage C step 13b): morale is a
    # SEPARATE multiplicative trim on top of `base_multiplier`, never a
    # fourth term in `combined` above -- the three tuned signals keep
    # their untouched 1/3 weights, and a neutral/absent morale trims by
    # exactly `1.0`. `MORALE_TRIM_SPAN`'s own docstring has the formula
    # and the disclosed-band consequence.
    morale_signal = context.morale if context is not None and context.morale is not None else 0.5
    morale_trim = 1.0 + MORALE_TRIM_SPAN * (2.0 * morale_signal - 1.0)
    multiplier = base_multiplier * morale_trim

    variance_rng = rng_for(seed, "progression", "real_variance", player_id, season)
    nudged_fields = {
        field_name: round(
            _clamp(
                getattr(ratings, field_name)
                + _clamp(field_delta, -_MAX_REAL_FIELD_DELTA, _MAX_REAL_FIELD_DELTA) * multiplier
                + variance_rng.gauss(0.0, _REAL_VARIANCE_SD),
                0,
                99,
            )
        )
        for field_name, field_delta in expected_delta.items()
    }

    synthetic_delta = _synthetic_scalar_delta(
        ratings.potential, overall, age, ratings.peak_age_start, ratings.peak_age_end
    )
    if synthetic_delta != 0.0:
        nudged_fields.update(
            {
                field_name: round(_clamp(getattr(ratings, field_name) + synthetic_delta, 0, 99))
                for field_name in _SYNTHETIC_FIELDS_FOR_REAL_PLAYERS
            }
        )

    new_ratings = replace(ratings, updated_at=aged_at, **nudged_fields)
    return new_ratings, compute_overall(new_ratings, position)


def _nudge_ratings(
    ratings: PlayerRatings,
    position: Position,
    overall: int,
    age: int,
    aged_at: datetime,
    *,
    external_ref: str | None,
    player_id: int,
    season: int,
    seed: int,
    context: RealPlayerContext | None,
) -> tuple[PlayerRatings, int]:
    """Steps 3's "delta" -- see module docstring's "Rating nudge
    mechanics", "53, not 55", and RPD-6 sections. Prime-window players
    (`peak_age_start <= age <= peak_age_end`) get `delta = 0.0` and are
    returned with their `ratings` object completely untouched, not even
    `updated_at` -- a no-op season should not manufacture the appearance
    that anything was recomputed. `overall_cached` is still recomputed
    via `compute_overall` in every branch (including delta == 0.0),
    since it is a pure, deterministic function of `ratings`/`position`
    and this keeps the Player-level `overall_computed_at` timestamp
    honest about "this player was assessed this season" even when the
    assessment reconfirms the same number."""
    if external_ref is not None:
        real_result = _real_trajectory_nudge(
            ratings,
            position,
            overall,
            age,
            aged_at,
            external_ref=external_ref,
            player_id=player_id,
            season=season,
            seed=seed,
            context=context,
        )
        if real_result is not None:
            return real_result

    peak_age_start = ratings.peak_age_start
    peak_age_end = ratings.peak_age_end
    potential = ratings.potential

    delta = _synthetic_scalar_delta(potential, overall, age, peak_age_start, peak_age_end)

    if delta == 0.0:
        return ratings, compute_overall(ratings, position)

    # `round(...)`, not `int(...)` (critic NIT 1): `int()` truncates toward
    # zero, so any `delta < 1.0` moved zero fields every season -- and since
    # `overall_cached` then never changed, the gap driving `delta` never
    # changed either, permanently stalling a small growth/decline gap
    # across any number of simulated seasons. `round()` matches `sim.
    # ratings.compute_overall`'s own `round(recentered)` convention, so a
    # small delta still accumulates visible movement over enough seasons
    # instead of truncating to nothing forever.
    nudged_fields = {
        field_name: round(_clamp(getattr(ratings, field_name) + delta, 0, 99))
        for field_name in _NUDGE_FIELDS
    }
    new_ratings = replace(ratings, updated_at=aged_at, **nudged_fields)
    return new_ratings, compute_overall(new_ratings, position)
