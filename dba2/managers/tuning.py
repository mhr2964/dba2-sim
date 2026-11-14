"""Every tunable constant Manager Minds reads, as one frozen profile
(docs/design/phase-2-manager-minds.md §3, §12 step 1).

Mirrors `sim/tuning.py::TuningProfile`'s shape exactly (Decision P1-12's
discipline, transplanted per §6's opening line: "one shape plus a weight
table means every tuning question reduces to 'which scalar'"). Every
constant named anywhere in the design doc that is *shared* across two or
more of `managers/`'s modules lives here; a constant that is genuinely
local to one module's own data table (the age-curve anchors, the
market-salary anchors, the pick slot table, the need-target table) stays
declared in the module that owns it, at the step that builds that module
-- `tuning.py` is not a dumping ground for every number in the document,
it is the *shared* tunable surface (§12 step 1's own scope: "every
constant in this document" read against which constants step 1 actually
lists, all of which are cross-module).

`GM_TUNING_VERSION` is Decision P1-22's versioning discipline transplanted:
a module-level constant (not a `GmTuning` field, same reasoning
`sim/tuning.py::TUNING_VERSION` gives) bumped on any change to a constant
or weight table anywhere under `managers/`, so a stored `DecisionRecord`
or golden-table regeneration can be tied to the tuning surface that
produced it.

Zero I/O, per Decision P2-2 (the same purity extension `sim/` carries):
no `dba2.data` import, no `datetime.now()`, no bare `random.Random()`, no
`open()`, no `os.environ`. This module imports only stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

GM_TUNING_VERSION: int = 3
"""Bumped 1 -> 2 by Slice 22-B (architect round 23 §4.3, Decision P2-42):
`decisions/signing.py`'s `_YEARS_BASE_LO`/`_YEARS_BASE_HI` constants moved.
Known, accepted cost of this bump (Decision P2-42, not discharged by this
slice): item 14's `se_precertified` citation in `seeds/gm_validation_
bands.json` was measured at `gm_tuning_version: 1` and is now stale, which
re-arms `compute_m14`'s live `se_measured <= se_threshold` check -- that
check reads FAIL at 3 seeds until either a fresh ~45-seed re-precertification
or a ruling extends Decision P2-35's decoupling to this constant set.

Bumped 2 -> 3 by Phase 3 §11 step 4 (docs/design/phase-3-inter-manager-
society.md, Decision P3-30): `managers/negotiation.py` and `managers/
memory.py` declare Phase 3's entire `managers/`-scoped constant set in
this one commit -- `COUNTER_BAND_MULT`, `MIN_CONCESSION_FRAC`,
`H2H_BAND_SHADE`, `H2H_MARGIN_SHADE`, `H2H_SHADE_PRIOR`, `H2H_DECAY` --
and per this module's own rule ("bumped on any change to a constant or
weight table anywhere under `managers/`"), DECLARING a new constant under
`managers/` is itself the triggering change; obeying the rule literally
here is what makes every later Phase 3 step provably bump-free (Decision
P3-30: "nothing after step 4 adds or moves one"). **This is the phase's
one and only `GM_TUNING_VERSION` bump** -- every subsequent Phase 3 step
introduces its own constants only in `services/` (`gm_service.py`) or
`seeds/` (`gm_phrases.json`, `gm_validation_bands.json`), both outside
this rule's blast radius by design (§3's module layout, Decision P3-17).
Same accepted cost as the 1->2 bump, restated: M14 reads FAIL here until
Phase 3's own step 8 re-precertifies at 45 seeds, and every RS3
`precision` block's `sd_across_seeds` citation reports `sd_source:
"this_run"` instead of `"precertified"` until then (Decision P2-35 item
2 -- "by design, not as a regression")."""


class CompetitiveWindow(StrEnum):
    """§7's four-band engine value -- computed, never persisted (Decision
    P2-13). `teams.timeline` stays the three-valued persisted column
    Phase 0 already ships; `managers/window.py` (step 4) maps onto it
    many-to-one (`REBUILD -> rebuild`, `RETOOL -> retool`, `WIN_NOW` and
    `CONTEND -> win_now`). Declared here, not in `window.py`, because
    §7's parameter table below needs a key type and the classifier that
    *produces* a `CompetitiveWindow` value is step 4's job, not step 1's
    -- the enum and its data table are step 1's, per §12 step 1's own
    text: "the enum and its constants are data and belong here; only the
    *classifier* waits for step 4."
    """

    REBUILD = "rebuild"
    RETOOL = "retool"
    WIN_NOW = "win_now"
    CONTEND = "contend"


@dataclass(frozen=True)
class WindowParams:
    """§7's per-window multiplier row -- four scalars applied inside §4's
    valuation formulas plus one (`window_urgency`) applied inside §9.3's
    signing formula. Every field here is read window-explicit by its
    consuming module; none has a "default" value the way a
    `GmPhilosophy`-keyed scalar might, because §4.5 states plainly that
    `market_value` "takes a window argument... it is not a function of
    the player alone."
    """

    gamma: float
    """Future-season discount rate applied as `gamma ** t` (§4.5)."""

    window_upside: float
    """Multiplies `upside_weight` inside `upside_weight_eff` (§4.3.1)."""

    age_exponent: float
    """Applied as `age_mult_eff ** age_exponent` (§7) -- amplifies both
    the young and the old ends of the age curve for a rebuilder,
    compresses both for a contender, from one exponent."""

    desired_room: float
    """Share of cap this window wants held in reserve, read by §9.2's
    `cap_delta_bonus` (`room_hunger`)."""

    window_urgency: float
    """How far over market this window's teams will bid in free agency,
    read by §9.3's `willingness` (REBUILD 0.00 -> CONTEND 0.18)."""


# §7's table, verbatim. `CompetitiveWindow.REBUILD` compounds the most
# heavily on the future (`gamma = 0.93`) and gives young players the most
# credit (`age_exponent = 1.50` amplifies the age-curve's above-1.0 side
# hardest); `CONTEND` discounts the future steeply (`gamma = 0.74`) and
# wants zero cap room held back (`desired_room = 0.00`) because winning
# now is worth spending now.
WINDOW_PARAMS: dict[CompetitiveWindow, WindowParams] = {
    CompetitiveWindow.REBUILD: WindowParams(
        gamma=0.93, window_upside=1.40, age_exponent=1.50, desired_room=0.20,
        window_urgency=0.00,
    ),
    CompetitiveWindow.RETOOL: WindowParams(
        gamma=0.88, window_upside=1.10, age_exponent=1.15, desired_room=0.10,
        window_urgency=0.05,
    ),
    CompetitiveWindow.WIN_NOW: WindowParams(
        gamma=0.80, window_upside=0.80, age_exponent=0.85, desired_room=0.03,
        window_urgency=0.12,
    ),
    CompetitiveWindow.CONTEND: WindowParams(
        gamma=0.74, window_upside=0.60, age_exponent=0.70, desired_room=0.00,
        window_urgency=0.18,
    ),
}
assert set(WINDOW_PARAMS) == set(CompetitiveWindow), (
    "WINDOW_PARAMS must cover exactly the four CompetitiveWindow values"
)


@dataclass(frozen=True)
class GmTuning:
    """The shared tunable surface every `managers/` module not yet built
    reads from -- valuation (§4), needs (§5), window (§7), cap-sheet (§8),
    trade/signing/draft (§9). Every field has a default so `GmTuning()`
    alone is a complete, usable profile, mirroring `sim/tuning.py::
    TuningProfile`'s own "a calibration sweep constructs variants via
    `dataclasses.replace`, never by editing defaults in place" contract.
    """

    # -- §4.1: the VP unit ---------------------------------------------
    vp_per_cap_season: float = 1000.0
    """One full salary-cap season of spend == this many VP (§4.1)."""

    # -- §4.5 / §8: horizons ---------------------------------------------
    horizon: int = 5
    """`market_value`'s season cap for a player under contract (§4.5) and
    the cap-sheet projection's own horizon (§8, Decision P2-15) -- the
    same constant, both places, deliberately: Decision P2-17 requires the
    two to stay in lockstep."""
    rookie_horizon: int = 4
    """Seasons a slot-1-through-30 rookie-scale deal is priced over
    (§4.5, §4.6, §9.4)."""
    second_round_horizon: int = 2
    """Seasons a slot-31-through-60 pick is priced over -- a short
    minimum deal, not a four-year scale contract (Decision P2-27, §4.6's
    fix for the pick-30-vs-31 inversion)."""

    # -- §4.3.1: the projected-peak / upside model -----------------------
    ramp_origin_age: float = 19.0
    """`age_ramp`'s denominator floor age -- guards `peak_age_start`
    values at or below this from a divide-by-zero or sign flip (§4.3.1)."""
    ramp_min_span: float = 1.0
    """`age_ramp`'s minimum denominator span, alongside `ramp_origin_age`
    (§4.3.1)."""
    growth_max: float = 12.0
    """Rating points of growth room at a full-ramp age-19 prospect with a
    neutral residual (§4.3.1)."""
    residual_gain: float = 0.50
    """How much a maxed-out `potential` residual scales `room_raw` by,
    +/-50% (§4.3.1)."""
    residual_cap: float = 11.0
    """Clamp bound (rating points) on the raw `potential - ovr` residual
    before it's normalized to [-1, +1] -- roughly 1.5 sigma of the
    generator's own `potential` noise (§4.3.1). Also `scouting.py`'s
    (step 2) `potential_variance` volatility-dial bound (§4.7), the same
    constant reused rather than a second one declared."""
    eps: float = 1e-9
    """Division guard in `room_t`'s `age_ramp(...)` denominator -- reached
    only where the numerator is already exactly 0, so it never actually
    scales anything (§4.3.1)."""

    # -- §4.4: the option-exercise model ----------------------------------
    opt_floor: float = 0.15
    """Player-option exercise probability floor (`q_t`, §4.4)."""
    opt_ceil: float = 0.95
    """Player-option exercise probability ceiling (`q_t`, §4.4)."""
    option_sens: float = 60.0
    """VP scale at which an option's exercise decision "stops being
    close" -- shared width parameter for both `softplus` (team option)
    and the `logistic` (player option), §4.4."""

    # -- §4.6: picks -------------------------------------------------------
    unresolved_discount: tuple[float, float, float, float, float] = (
        1.00, 0.85, 0.72, 0.60, 0.50,
    )
    """`UNRESOLVED_DISCOUNT` by years-out, index `min(years_out, 4)` --
    the last entry covers "4 or more" (§4.6). Slot uncertainty on an
    unresolved future pick, layered on top of (not instead of) the
    `gamma ** years_out` time discount."""
    peak_age_ref: float = 26.0
    """`PEAK_AGE_REF` -- the seeder's mean `peak_age_start` draw (uniform
    24-28), used for a pick's `E_ovr_y` progression since a pick has no
    real player yet to read a `peak_age_start` column from (§4.6)."""
    slot_sigma: float = 4.0
    """Spread of the `logistic` that turns `expected_slot` and
    `protection_through` into a conveyance probability `P` (§4.6)."""
    rollover_max: int = 2
    """A protection rolls at most this many times before the obligation
    converts to `SECOND_ROUND_FALLBACK` (§4.6)."""
    second_round_fallback_picks: int = 2
    """How many second-round picks a fully-rolled protected pick converts
    into (§4.6's `SECOND_ROUND_FALLBACK = 2 * pick_value(second_round_
    fallback_slot, ...)`)."""
    second_round_fallback_slot: int = 45
    """The slot `SECOND_ROUND_FALLBACK` prices its two converted picks at
    (§4.6) -- the standard real-world conversion."""

    # -- §4.7: the scouting draw (built at step 2) -------------------------
    scout_sd_base: float = 3.0
    """`SCOUT_SD_BASE` -- rostered-player scouting-noise SD anchor, in
    rating points, before the `potential_variance` volatility dial
    (§4.7)."""
    scout_sd_prospect: float = 5.0
    """`SCOUT_SD_PROSPECT` -- the wider anchor for a `draft_eligible`
    player, who has no track record to narrow the scouting spread
    against (§4.7)."""
    scout_sd_gain: float = 0.50
    """How far the volatility dial swings `scout_sd` off its anchor,
    +/-50% (§4.7)."""

    # -- §5: the positional-need matrix (built at step 6) ------------------
    need_gain: float = 0.30
    """`NEED_GAIN` -- scales the positive half of `need_bonus` (§5)."""
    glut_loss: float = 0.25
    """`GLUT_LOSS` -- scales the negative half of `need_bonus` (§5)."""
    glut_slack: float = 0.75
    """`GLUT_SLACK` -- how far past target a position/tier must sit
    before `glut` starts penalizing it (§5, Decision P2-8's step 4)."""

    # -- §7: the window classifier's own inputs (built at step 4) ---------
    replacement_ovr: int = 60
    """`REPLACEMENT_OVR` -- the floor `future_strength` pads an empty
    season-`t+2` roster slot at, not a reward (§7). §4.4's lowest
    non-minimum market-salary anchor (2.5% of cap): "a body you can
    always buy.\""""
    window_input_upside: float = 0.50
    """`WINDOW_INPUT_UPSIDE` -- the league-default `upside_weight` used
    by `projected_ovr_2`, with no `window_upside` factor, no `confidence`
    damping, no `risk` adjustment, and no scouting draw -- the fixed
    constant that breaks `future_strength`'s circularity at the actual
    cycle point (§7)."""

    # -- §8: cap-sheet projection (built at step 7) -------------------------
    cap_growth: float = 1.05
    """`CAP_GROWTH` -- per-season growth rate a future season with no
    real `league_cap_settings` row is projected at, from the latest real
    row (Decision P2-16)."""

    # -- §9.2: trade evaluation (built at step 8) --------------------------
    min_abs_gain: float = 40.0
    """`MIN_ABS_GAIN` -- the trade-acceptance threshold's absolute floor,
    in VP (§9.2)."""
    min_rel_gain: float = 0.06
    """`MIN_REL_GAIN` -- the trade-acceptance threshold's relative floor,
    as a share of what's given up (§9.2). The primary anti-churn
    mechanism (§14), ahead of `loyalty`."""
    room_hunger_floor: float = 0.05
    """`ROOM_HUNGER_FLOOR` -- guards `cap_delta_bonus`'s `room_hunger`
    division for a CONTEND team's `desired_room = 0.00` (§9.2)."""
    cap_relief_weight: float = 0.60
    """`CAP_RELIEF_WEIGHT` -- scales `cap_delta_bonus`'s VP output
    (§9.2)."""

    # -- §9.4: draft (built at step 9) --------------------------------------
    board_reference_slot: int = 1
    """The single slot every prospect on a `build_draft_board` result is
    priced at for *ordering* purposes (`DraftBoardEntry.rank_value`) --
    `pick_pref` and every other per-prospect term factor out of a common
    reference slot, so the board's order is invariant to which slot is
    chosen here **as long as that slot shares a horizon with every other
    candidate reference slot** (§9.4, `managers/decisions/draft.py`'s own
    module docstring). **The invariance does NOT hold across the 30/31
    horizon split** (step 9 critic round 18's own finding): `pick_horizon`
    returns `ROOKIE_HORIZON=4` for `slot <= 30`
    and `SECOND_ROUND_HORIZON=2` for `slot >= 31`, and the horizon change
    moves a prospect's own talent-side value `A(p)`, prospect-dependently
    -- measured, `board_reference_slot=31` vs `=1` reorders 51 of 60
    prospects on the real stub class. `1` because it makes the top of the
    board directly comparable to `pick_value(1)`, which is exactly what
    §12 step 9's own 25%-consistency check does -- and because it is
    inside the round-1 horizon where the invariance this field exists to
    provide is actually true. `__post_init__` below enforces this bound
    rather than leaving it a docstring-only promise."""

    def __post_init__(self) -> None:
        if not (1 <= self.board_reference_slot <= 30):
            raise ValueError(
                f"board_reference_slot must be in [1, 30] (the ROOKIE_HORIZON "
                f"round), got {self.board_reference_slot} -- a reference slot "
                f"past the 30/31 horizon split breaks build_draft_board's own "
                f"reference-slot invariance, since pick_horizon changes there"
            )

    def unresolved_discount_for(self, years_out: int) -> float:
        """`UNRESOLVED_DISCOUNT[years_out]`, clamped at the "4 or more"
        entry -- the one place `unresolved_discount`'s fixed-length tuple
        is indexed, so `picks.py` (step 5) never has to re-derive the
        clamp itself."""
        index = min(max(years_out, 0), len(self.unresolved_discount) - 1)
        return self.unresolved_discount[index]


DEFAULT_TUNING: GmTuning = GmTuning()
