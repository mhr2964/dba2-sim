"""Market value, no team context (docs/design/phase-2-manager-minds.md
§4.1-§4.5, §12 step 3).

The talent curve (§4.2), the age curve (§4.3), the projected-peak-based
upside term over step 2's scouted peak (§4.3.1, consuming `managers.
scouting`), contract surplus with Decision P2-26's option-exercise
weighting (§4.4), and the discounted multi-season sum that puts them all
together (§4.5). This is **step 3's own text, verbatim**: "the artifact
every later step consumes ... the wrongness [of which] is most invisible,"
so it is pinned to a golden file (`seeds/gm_value_archetypes.json`) before
`window.py`, `picks.py`, `needs.py`, or any `decisions/` module reads it.

**Two things explicitly out of this step's scope, per §12 step 3's own
text and this dispatch's task description:**

1. **Draft picks and `draft_eligible` prospects.** §4.6's `pick_value` is
   step 5's (`picks.py`); §9.4's implied-rookie-contract pricing for a
   named prospect is step 9's (`decisions/draft.py`). `market_value` below
   handles only a player under a real contract (`RosterPlayer.contract is
   not None`) -- a `draft_eligible` player has zero controlled seasons by
   construction (§9.4: "`T` ... is zero for a `draft_eligible` player"
   before Decision P2-27's second half, which this step does not build,
   changes that), so `market_value` returns `0.0` for one rather than
   raising.
2. **`managers/capsheet.py`'s multi-season cap projection (§8, step 7).**
   `market_value`'s discounted sum needs *a* `LeagueCapSettings` for each
   season `t` in its horizon, but the projected-growth machinery
   (`CAP_GROWTH`, Decision P2-16) that would supply a *different* one per
   future season doesn't exist yet at step 3 and isn't this step's to
   build. Every worked figure in §4.4/§4.6 (and `tools/_verify_gm_design_
   numbers.py`, which reproduces them) holds `CAP`/`MIN_SALARY`/the
   `max_pct_*` anchors **constant across every season of a single sum** --
   there is no `CAP_GROWTH` factor anywhere in §4.4's or §4.5's formulas.
   `market_value` below does the same: one `LeagueCapSettings` snapshot,
   read for every season `t` in the horizon. This is consistent with the
   design doc's own scope, not a shortcut around it.

**One interpretation call, stated because §10's own text depends on it
resolving a particular way.** §10's performance-envelope paragraph
describes `market_value` as "team-agnostic ... computed once per player
per pass ... and reused across all 30 evaluating teams." Taken completely
literally, that would make `market_value` produce the *same* number for
every team, which directly contradicts this same step's own required
verify ("the disagreement channel is asserted on valuations, not on
estimates ... two randomly chosen teams' `market_value` of the same
player differs") and Decision P2-9's own explanation of Gap C (a
deterministic valuation system with no per-team channel is exactly the
trade-deadlock failure mode §4.7 exists to prevent). The two statements
are read here as being about *different* axes of "team-specific": §4.7's
scouted peak (a function of `team_id` via `rng_for`) is the channel Gap C
names as the fix, so it has to reach `market_value`'s output, not stop at
`ScoutedPeak`. "Team-agnostic" is read as *not reading `TeamSnapshot`'s
positional-need matrix* -- Decision P2-9's actual textual contrast is
with `fit_value`, which multiplies `market_value` by a team's own
`need_bonus` (§5, not built until step 6). `market_value` below takes an
explicit `ScoutedPeak` (built by whichever team is asking, via `scout_
player`/`scout_roster_player`) and an explicit talent `ovr` (built by
`effective_talent_ovr`, below) as inputs, rather than deriving either
from a `TeamSnapshot` -- which is what "no team context" means for this
step's own purposes: it is a pure function of a player, a philosophy, a
window, and whichever team's own view of that player the caller supplies,
never of that team's roster or needs. Flagged in the step-3 report for a
critic pass rather than resolved silently.

Zero I/O, per Decision P2-2's purity extension: no `dba2.data`, no
`datetime.now()`, no bare `random.Random()`, no `open()`, no
`os.environ`. Imports `domain`, `core` (transitively via `managers.
scouting`), `sim.ratings` (Decision P2-9's tilted entry point), and its
own `managers` siblings only.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from dba2.domain.contract import ContractYear, OptionType
from dba2.domain.league import LeagueCapSettings
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import PlayerRatings
from dba2.domain.team import GmPhilosophy
from dba2.managers.philosophy import PROFILES, PhilosophyProfile
from dba2.managers.scouting import ScoutedPeak, age_ramp, player_age, scout_player
from dba2.managers.snapshot import RosterPlayer
from dba2.managers.tuning import (
    DEFAULT_TUNING,
    WINDOW_PARAMS,
    CompetitiveWindow,
    GmTuning,
    WindowParams,
)
from dba2.sim.ratings import compute_overall_tilted


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _piecewise_linear(x: float, anchors: Sequence[tuple[float, float]]) -> float:
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:], strict=False):
        if x0 <= x <= x1:
            frac = (x - x0) / (x1 - x0)
            return y0 + (y1 - y0) * frac
    raise AssertionError(f"{x} not covered by anchors {anchors}")  # pragma: no cover


# ---------------------------------------------------------------------------
# §4.2 -- the talent curve
# ---------------------------------------------------------------------------


def base_value(ovr: float, kappa: float) -> float:
    """`100 * exp((ovr - 70) / kappa)` (§4.2). Convex in `ovr` by
    construction -- `kappa` is the philosophy's star curvature (§6), the
    single parameter that makes `star_maxer` and `egalitarian` genuinely
    different agents. Decision P2-6's real identity holds for every
    `kappa`: `base_value(70, kappa) == 100` exactly, since `exp(0) == 1`."""
    return 100.0 * math.exp((ovr - 70.0) / kappa)


# ---------------------------------------------------------------------------
# §4.3 -- the age curve
# ---------------------------------------------------------------------------

# §4.3's anchor table, verbatim -- piecewise-linear through these points,
# clamped flat outside them.
_AGE_MULT_ANCHORS: tuple[tuple[float, float], ...] = (
    (20.0, 1.30), (23.0, 1.25), (25.0, 1.15), (27.0, 1.05), (29.0, 1.00),
    (31.0, 0.85), (33.0, 0.65), (35.0, 0.45), (38.0, 0.25),
)

# The age above and at which `vet_bias`'s reshape applies (§6) -- "anchored
# at 1.00 for age 29," and vet_bias only ever reshapes the below-1.0 side
# of the curve.
_VET_BIAS_AGE_THRESHOLD = 29.0


def age_mult(age: float) -> float:
    """§4.3's piecewise-linear age curve, read off the anchor table
    directly -- never off memory (§4.3's own stated reason for existing:
    round 1 misread the anchors in four places)."""
    return _piecewise_linear(age, _AGE_MULT_ANCHORS)


def age_mult_eff(age: float, *, vet_bias: float, age_exponent: float) -> float:
    """`vet_bias`'s reshape (above 29 only) composed with `age_exponent`'s
    power, in that order (§6: "`vet_bias` composes with §7's `age_
    exponent`"). `age_mult_eff = age_mult(age)` below 29; `1 - (1 -
    age_mult(age)) / vet_bias` at or above it; then raised to `age_
    exponent` either way -- `age_exponent` amplifies both the young and
    the old ends of the curve at once from one exponent, because `age_
    mult` sits above 1.0 for the young and below 1.0 for the old (§7)."""
    base = age_mult(age)
    if age >= _VET_BIAS_AGE_THRESHOLD:
        base = 1.0 - (1.0 - base) / vet_bias
        # Real, pre-existing defect surfaced by tools/gm_harness's own age
        # draw (19-38, §4.2 of the phase-2-architect-round-7 report) --
        # never hit by any hand-built fixture in this codebase before now.
        # `vet_bias < 1.0` (`youth_developer`, 0.70) reshapes `base` below
        # zero once `age_mult(age)` crosses 0.30 -- age >= 37.25 on the
        # piecewise-linear curve between anchors (35.0, 0.45)/(38.0, 0.25),
        # not only once it bottoms out at the 38+ floor anchor (0.25): e.g.
        # at the floor, `1.0 - (1.0 - 0.25) / 0.70 = -0.071`. `age_
        # exponent` (§7's table) is fractional for every window (0.70-
        # 1.50), and Python's `**` returns a `complex` for a negative base
        # raised to a non-integer power -- not a `ValueError`, so this
        # silently poisoned every later float comparison the complex
        # result touched (`propose_packages`' own `sorted(..., key=...)`
        # is where `run_trade_window` first surfaced it, as an unrelated-
        # looking `TypeError`). Clamped to `0.0` here: there is no
        # meaningful real-valued "amplify this player's already-inverted-
        # past-zero age multiplier by a fractional power" reading once
        # `vet_bias`'s reshape has pushed `base` past the floor its own
        # curve was defined against, so `0.0` (the same "no age credit
        # left" reading the floor anchor itself already expresses at 0.25)
        # is the natural, real-valued extension, not an arbitrary choice.
        base = max(0.0, base)
    return base**age_exponent


# ---------------------------------------------------------------------------
# §4.3.1 -- the upside term, over step 2's scouted peak
# ---------------------------------------------------------------------------


def upside_weight_eff(
    upside_weight: float, window_upside: float, confidence: float
) -> float:
    """`clamp(upside_weight * window_upside * (0.4 + 0.6 * confidence), 0,
    1)` (§4.3.1, Decision P2-7). The clamp at 1.0 is load-bearing (§4.3.1:
    "lets a GM value a player above his own scouted ceiling" if unclamped)
    -- it is what makes `effective_ovr <= effective_peak` an algebraic
    property of `room_t`/`effective_ovr_t` below rather than a hoped-for
    numerical coincidence."""
    return _clamp(upside_weight * window_upside * (0.4 + 0.6 * confidence), 0.0, 1.0)


def room_t(
    effective_peak: float,
    ovr: float,
    age: float,
    t: int,
    peak_age_start: float,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """`max(0, effective_peak - ovr) * age_ramp(age + t, peak_age_start) /
    max(age_ramp(age, peak_age_start), EPS)` (§4.3.1) -- growth room burns
    off linearly to zero as the player ages toward `peak_age_start` inside
    the multi-season sum. `age_ramp` (from `managers.scouting`, not
    re-derived here) is non-increasing in age, so this ratio is always
    `<= 1`: a season further in the future never has *more* growth room
    left than the season being scouted right now."""
    ramp_t = age_ramp(age + t, peak_age_start, tuning=tuning)
    ramp_0 = age_ramp(age, peak_age_start, tuning=tuning)
    return max(0.0, effective_peak - ovr) * ramp_t / max(ramp_0, tuning.eps)


def effective_ovr_t(
    ovr: float,
    effective_peak: float,
    age: float,
    t: int,
    peak_age_start: float,
    uw_eff: float,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """`ovr + upside_weight_eff * room_t` (§4.3.1). Algebraically bounded
    above by `effective_peak`: `uw_eff` is clamped to `[0, 1]` and `room_t`'s
    own ratio factor is `<= 1` (see `room_t`'s docstring), so `uw_eff *
    room_t <= max(0, effective_peak - ovr)`, i.e. `effective_ovr_t <=
    ovr + (effective_peak - ovr) == effective_peak` whenever `effective_
    peak > ovr`, and `effective_ovr_t == ovr <= effective_peak` (§4.7's
    floor-at-`ovr` clamp) otherwise. Pinned as a direct test over the
    whole archetype grid rather than only argued here."""
    return ovr + uw_eff * room_t(effective_peak, ovr, age, t, peak_age_start, tuning=tuning)


# ---------------------------------------------------------------------------
# §4.4 -- market salary and the option-exercise model
# ---------------------------------------------------------------------------

# §4.4's talent-side anchors, verbatim -- the money-side values for 55-76
# are flat constants (not read from `league_cap_settings`); 80/85/90 read
# the league's own max-contract ladder (`max_pct_0_6/7_9/10_plus`) so the
# curve's top end is never a second, un-synced copy of that data. 55's
# floor reads `min_salary` the same way.
_MARKET_SALARY_FLAT_SHARES: dict[float, float] = {
    60.0: 0.025, 64.0: 0.045, 68.0: 0.08, 72.0: 0.12, 76.0: 0.18,
}


def market_salary_share(effective_ovr: float, cap_settings: LeagueCapSettings) -> float:
    """§4.4's market-salary curve, as a share of `cap_settings.salary_cap`
    -- piecewise-linear through 9 anchors, continuous and monotone across
    the `min_salary`/2.5%-of-cap join (§4.4: "comfortably below the 60 ->
    2.5% anchor")."""
    anchors: tuple[tuple[float, float], ...] = (
        (55.0, cap_settings.min_salary / cap_settings.salary_cap),
        *((ovr, _MARKET_SALARY_FLAT_SHARES[ovr]) for ovr in (60.0, 64.0, 68.0, 72.0, 76.0)),
        (80.0, cap_settings.max_pct_0_6 / 100.0),
        (85.0, cap_settings.max_pct_7_9 / 100.0),
        (90.0, cap_settings.max_pct_10_plus / 100.0),
    )
    return _piecewise_linear(effective_ovr, anchors)


def softplus(v: float, sens: float) -> float:
    """`OPTION_SENS * ln(1 + exp(V / OPTION_SENS))`, evaluated in §4.4's
    own overflow-safe form: `max(V, 0) + sens * ln(1 + exp(-|V| / sens))`.
    Algebraically `>= max(v, 0) >= v` everywhere -- a team option can
    never be worth less than the guaranteed year it replaces (§4.4's
    first ordering)."""
    return max(v, 0.0) + sens * math.log1p(math.exp(-abs(v) / sens))


def logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def player_option_q(surplus_t: float, *, tuning: GmTuning = DEFAULT_TUNING) -> float:
    """`OPT_FLOOR + (OPT_CEIL - OPT_FLOOR) * logistic(-surplus_t /
    OPTION_SENS)` (§4.4, Decision P2-26) -- keyed off `surplus_t` alone
    (the player's own paycheck), never off `V_t` (the team's opinion of
    his talent). Ranges over `[OPT_FLOOR, OPT_CEIL] = [0.15, 0.95]`."""
    return tuning.opt_floor + (tuning.opt_ceil - tuning.opt_floor) * logistic(
        -surplus_t / tuning.option_sens
    )


def season_contribution(
    v_t: float,
    surplus_t: float,
    option_type: OptionType,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """Decision P2-26's three-way branch on `contract_years.option_type`
    (§4.4): `V_t` for a guaranteed year (`OptionType.NONE`); `softplus(V_t)`
    for a team option; `player_option_q(surplus_t) * V_t` for a player
    option or an early termination -- the same rule (§4.4 never
    distinguishes `EARLY_TERMINATION` from `PLAYER`: both are "the player's
    own exercise decision, keyed off his own paycheck")."""
    if option_type is OptionType.TEAM:
        return softplus(v_t, tuning.option_sens)
    if option_type in (OptionType.PLAYER, OptionType.EARLY_TERMINATION):
        return player_option_q(surplus_t, tuning=tuning) * v_t
    return v_t


# ---------------------------------------------------------------------------
# Decision P2-9 -- the tilted talent scalar (§6, §8's public entry point)
# ---------------------------------------------------------------------------


def tilted_overall(
    overall_cached: int, ratings: PlayerRatings, position: Position, profile: PhilosophyProfile
) -> int:
    """Decision P2-3's one stated exception, extracted (RC-22.3 step 1,
    `docs/design/auto-gm-role-construction.md`, C10): every philosophy
    except one reads `overall_cached` directly (`profile.group_tilt` is
    the empty `_NO_TILT` mapping); `defense_first` re-derives a
    philosophy-specific overall through `sim.ratings.compute_overall_
    tilted`, which "replaces `ovr` throughout §4 for that philosophy only"
    (§6). Branches on `bool(profile.group_tilt)` rather than special-
    casing `GmPhilosophy.DEFENSE_FIRST` by name, so any future philosophy
    that sets a non-empty `group_tilt` gets the same treatment without a
    second call site to update.

    Takes `overall_cached`/`ratings`/`position` directly rather than a
    `RosterPlayer` -- a `RosterPlayer` also carries a contract load
    (`roster_player.contract`), which `role_service.construct_team_roles`
    (auto-gm-role-construction.md's GM-cadence pass) has no use for and
    should not need to fabricate just to rank a rotation."""
    if not profile.group_tilt:
        return overall_cached
    return compute_overall_tilted(ratings, position, profile.group_tilt)


def effective_talent_ovr(roster_player: RosterPlayer, profile: PhilosophyProfile) -> int:
    """One-line wrapper over `tilted_overall` (RC-22.3 step 1, C10) -- the
    contract-bearing `RosterPlayer` is this function's own concern, not
    the tilt computation's."""
    return tilted_overall(
        roster_player.player.overall_cached,
        roster_player.ratings,
        roster_player.player.primary_position,
        profile,
    )


# ---------------------------------------------------------------------------
# §4.5 -- putting it together: market value
# ---------------------------------------------------------------------------


def _controlled_years(roster_player: RosterPlayer, season: int) -> tuple[ContractYear, ...]:
    """The contract-year rows still controlling this player from `season`
    onward, ascending -- "`T = min(controlled seasons remaining, HORIZON)`"
    (§4.5)'s own `T` reads as `len` of this. Empty for a `draft_eligible`
    player (`roster_player.contract is None`), which is this step's
    documented scope boundary (see module docstring, item 1)."""
    if roster_player.contract is None:
        return ()
    return tuple(y for y in roster_player.contract.years if y.season >= season)


def market_value(
    roster_player: RosterPlayer,
    ovr: int,
    scouted_peak: ScoutedPeak,
    profile: PhilosophyProfile,
    window: WindowParams,
    cap_settings: LeagueCapSettings,
    season: int,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """§4.5's full formula: `asset_pref * Sum_t gamma^t * season_
    contribution_t`, `t = 0..T-1`. `asset_pref` is always `1.0` here --
    the `pick_pref` branch is for an asset priced off an implied rookie
    contract (Decision P2-34), which is step 5's/9's scope, not this
    step's (see module docstring, item 1).

    `ovr` and `scouted_peak` are both taken as explicit parameters rather
    than derived from `roster_player` internally: `ovr` must be the same
    (possibly `defense_first`-tilted, via `effective_talent_ovr`) value
    that `scouted_peak` was itself scouted against, and this function has
    no way to verify that consistency on its own -- `market_value_for`
    below is the convenience wrapper that keeps the two in lockstep for
    every caller that doesn't need to construct them separately (e.g. a
    `picks.py`/`decisions/` caller re-pricing the same scouted view
    against several windows without re-drawing it)."""
    controlled = _controlled_years(roster_player, season)
    horizon = min(len(controlled), tuning.horizon)
    if horizon == 0:
        return 0.0

    birth_date = roster_player.player.birth_date
    age = float(player_age(birth_date, season))
    peak_age_start = float(roster_player.ratings.peak_age_start)
    confidence = roster_player.confidence_f
    uw_eff = upside_weight_eff(profile.upside_weight, window.window_upside, confidence)

    total = 0.0
    for t, year in enumerate(controlled[:horizon]):
        eff_ovr = effective_ovr_t(
            ovr, scouted_peak.effective_peak, age, t, peak_age_start, uw_eff, tuning=tuning
        )
        a_mult = age_mult_eff(age + t, vet_bias=profile.vet_bias, age_exponent=window.age_exponent)
        talent = base_value(eff_ovr, profile.kappa) * a_mult

        cap_hit_share = year.salary / cap_settings.salary_cap
        surplus_t = (
            market_salary_share(eff_ovr, cap_settings) - cap_hit_share
        ) * tuning.vp_per_cap_season
        v_t = talent + profile.surplus_weight * surplus_t

        contribution = season_contribution(v_t, surplus_t, year.option_type, tuning=tuning)
        total += (window.gamma**t) * contribution

    return total


def market_value_for(
    roster_player: RosterPlayer,
    *,
    league_rng_seed: int,
    team_id: int,
    season: int,
    philosophy: GmPhilosophy,
    window: CompetitiveWindow,
    cap_settings: LeagueCapSettings,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """Convenience wrapper over `market_value`, `effective_talent_ovr`, and
    `managers.scouting.scout_player` for a caller holding a `RosterPlayer`,
    a `(philosophy, window)` pair, and a scouting identity (`team_id`,
    `league_rng_seed`, `season`) rather than the three primitives `market_
    value` itself needs -- keeps `ovr` and `scouted_peak` consistent (both
    computed from the same tilted-or-not talent scalar) without every
    caller re-deriving that plumbing, the same role `scout_roster_player`
    plays over `scout_player` in step 2. Not itself part of §4.5's formula."""
    profile = PROFILES[philosophy]
    params = WINDOW_PARAMS[window]
    ovr = effective_talent_ovr(roster_player, profile)
    player = roster_player.player
    ratings = roster_player.ratings
    age = float(player_age(player.birth_date, season))

    scouted = scout_player(
        league_rng_seed=league_rng_seed,
        team_id=team_id,
        player_id=player.id,
        season=season,
        ovr=ovr,
        age=age,
        peak_age_start=ratings.peak_age_start,
        potential=ratings.potential,
        potential_variance=ratings.potential_variance,
        draft_eligible=player.roster_status is RosterStatus.DRAFT_ELIGIBLE,
        risk=profile.risk,
        tuning=tuning,
    )
    return market_value(
        roster_player, ovr, scouted, profile, params, cap_settings, season, tuning=tuning
    )
