"""Free-agent offer construction -- §9.3, Decision P2-21
(docs/design/phase-2-manager-minds.md §9.3, §12 step 9).

**Interpretation call 1 -- `years = f(age, window)`, under-specified in
the design doc** (it only says "contenders offer short, rebuilders offer
long to young players," no formula). Read here as: a base years-count
derived from the window's own `gamma` posture (§7's per-window discount
rate -- REBUILD 0.93 down to CONTEND 0.74, already an ordered "how much
this team's own posture discounts the future" scalar with no new table
needed), linearly mapped onto `[_YEARS_BASE_LO, _YEARS_BASE_HI]`; shortened
to a flat `_OLD_AGE_SHORT_YEARS`-year offer for any player at or above
`_OLD_AGE_SHORT_CUTOFF` (33) regardless of window (a declining asset does
not get a multi-year deal just because the offering team is rebuilding);
lengthened by one year for a player at or below `_YOUNG_AGE_LONG_CUTOFF`
(25) on a REBUILD/RETOOL-window team specifically (§9.3's own "long to
young players" is stated as a rebuilder-specific behavior, not a
universal one); clamped to `[1, 5]` (§9.3's own outer bound). Flagged for
critic scrutiny -- this is a real reading, not the only possible one, and
`offer_years` is the one function in this module with no design-doc
formula to check it against.

**Slice 22-B (architect round 23, `docs/ledger/reports/phase-2-architect-
round-23.md` §4.2-§4.3) -- `_YEARS_BASE_LO`/`_YEARS_BASE_HI` recalibrated
1/4 -> 3/6 to raise realised mean `offer_years` toward the generator's own
3.5714-season equilibrium (item 10's duration diagnosis).** See
`_base_years_from_window` and `offer_years`'s own docstrings for the exact
pre-/post-clamp maps this produces and the disclosed REBUILD/RETOOL
duration-collapse cost. `_YEARS_CLAMP_HI` stays at 5, deliberately.

**Interpretation call 2 -- `legal_capacity`, "whichever exception the
team actually has," no selection rule given.** Read here as largest-
available-first: cap room itself when positive; otherwise the largest of
the MLE, the bi-annual, and the minimum-salary exception (`league_cap_
settings.mle_amount`/`bi_annual_amount`/`min_salary`) -- the "no free
ATM, but no unnecessary underbidding either" reading of a design doc that
names three candidate exceptions without picking one. Flagged for critic
scrutiny alongside interpretation call 1.

**Interpretation call 3 -- `legal_capacity`'s incumbent (Bird-rights)
branch (Decision P2-20, architect round 11 §5.3, round 12 §5.4's three
amendments -- `docs/ledger/reports/phase-2-architect-round-11.md`,
`-round-12.md`).** The spec's own pseudocode names the ceiling as
`max_pct_tier_for_this_player · cap` but its `legal_capacity` code
skeleton only lists `incumbent_prior_salary`/`apron_room` as new keyword
parameters -- silent on how the per-player years-of-service tier
(`cap_settings.max_pct_0_6/7_9/10_plus`, the identical rule `tools/
gm_harness/league.py::_max_pct_tier` already applies to every generated
contract) reaches this function without a players-in-the-league lookup
this module cannot make (Decision P2-2 purity). Read here as: a third
keyword parameter, `incumbent_years_pro`, required (non-`None`) whenever
`incumbent_prior_salary` is not `None` -- the caller already has the
player's `years_pro` on hand (`RosterPlayer.player.years_pro`) at every
site that can supply `incumbent_prior_salary` in the first place, so this
adds no new I/O, only a parameter. `incumbent_prior_salary` itself is
**not read numerically by the ceiling formula below** -- it exists as the
non-`None` gate that says "this call is an incumbent re-sign," carried as
a real dollar figure (the player's own current, about-to-lapse salary)
rather than a bare boolean because a future raise-limited Bird formula
(the real NBA's 105%/108%-of-prior-salary cap) would need exactly this
number, and every other value this module threads through a
`DecisionRecord` is a real figure, not a flag. Flagged for critic
scrutiny alongside interpretation calls 1-2.

**`effective_ovr` reads as `effective_talent_ovr` alone, with no scouted-
upside layering.** §9.3's own formula text never mentions `ScoutedPeak`/
`effective_peak`/`upside_weight_eff` at all (contrast §4.3.1's `market_
value` and §9.4's draft pricing, both of which name the scouting machinery
explicitly) -- so `market_salary(effective_ovr)` here reads `effective_
ovr` as the plain philosophy-tilted talent scalar every other formula in
this codebase calls `ovr` before any upside layering, not a new
scouted-peak-driven quantity this section never asks for.

**The outer `willingness` clamp `[0.70, 1.60]`, verbatim from §9.3.** The
design doc's own text: against the *reachable* range (`need_bonus`
clamped to `[-0.35, +0.50]`, `window_urgency` in `[0.00, 0.18]`),
willingness spans `[0.675, 1.57]` and **the upper guard never actually
binds at current constants** -- it is kept anyway because its job is to
survive a future retune of `NEED_GAIN` or `window_urgency`, not today's
constants. State this rather than "simplify" the guard away, the same
"dormant, not dead" note `window.py`'s own hysteresis cap carries.

**`SigningOffer.guaranteed_total` assumes a flat annual salary across
`years`** (no raise schedule) -- a further, smaller simplification: §9.3
names no per-year raise formula for a fresh free-agent offer (`raise_pct_
bird`/`raise_pct_non_bird` govern a *re-signing*'s Bird-rights bookkeeping,
which this module does not model), so a single annual figure repeated for
every year of the offer is the simplest reading consistent with "one
offer" (§9.3's own phrase).

**`legal_capacity`'s minimum-exception floor (fix round, critic's B-list).**
The real NBA's Minimum Salary Exception is available to literally every
team regardless of cap or apron status -- unlike the MLE/bi-annual, which
this module's own interpretation call 2 already treats as unconditionally
available too, but only in the `room <= 0` branch. Round 1 of this fix let
a small *positive* `room` (e.g. $1) return itself unchanged, undercutting
even the minimum exception -- fixed by flooring the `room > 0` branch at
`min_salary` as well, so `legal_capacity` never returns less than `min_
salary` under any input.

**`offer_salary`'s minimum-salary floor (fix round).** No real contract
signs below `min_salary` -- round 1 had no floor at all, so a low-`ovr`,
low-`willingness` free agent could draw an offer under the league minimum
(measured: a 55-ovr FA at `willingness = 0.70` offered $891,009 against a
real `min_salary` of $1,272,870). The floor is applied *before* the
`legal_capacity_dollars` cap, never after (`min(legal_capacity_dollars,
max(market * willingness, min_salary))`), so `legal_capacity_dollars`
stays the one hard outer bound -- with `legal_capacity` now floored at
`min_salary` too, the two floors agree and `legal_capacity_dollars` is
never itself below `min_salary` for a caller going through `legal_
capacity`, though `offer_salary` is still unit-tested directly with
arbitrary raw `legal_capacity_dollars` values and must keep respecting
whatever it is handed (`test_offer_salary_mutation_test_is_actually_
capped_by_legal_capacity`).

**`DecisionRecord`/`ReasonTerm` output (Decision P2-18, added this fix
round -- decisions/__init__.py's own docstring states every module in
`decisions/` emits one, unscoped to trade evaluation alone; `trade.py`'s
step-8 shape is mirrored exactly, nothing richer).** `construct_offer` now
returns `(SigningOffer, DecisionRecord)` -- a contract change from the
single-`SigningOffer` return round 1 shipped, propagated to this
module's one caller (`services/free_agency_service.py::construct_offer_
for_team`). The record decomposes the *unrounded* salary additively so it
reconstructs exactly: `market_salary` (the base `market_salary_share *
cap`), `need_bonus` and `window_urgency` (the two components summed inside
`willingness`, each weighted by `market_salary`), `willingness_clamp` (the
delta the outer `[0.70, 1.60]` clamp applies, zero whenever that guard
does not bind), `min_salary_floor` (zero unless the floor above binds),
and `legal_capacity_cap` (zero unless `legal_capacity_dollars` binds,
negative when it does). `verdict` is always `"offer"` -- this function has
no reject branch, unlike `trade.py::evaluate_trade`'s legality gate;
`threshold` is `legal_capacity_dollars` itself, the ceiling this offer's
score cannot exceed, stated for a reader rather than left at a
placeholder `0.0`. `offer_years` -- the years side of the offer -- has no
term of its own here, since `willingness`/`offer_salary` are the only
formula §9.3 asks a record to explain the *dollar* reasoning behind; the
returned `SigningOffer.years` still carries `offer_years`'s own output
unchanged.

Zero I/O, per Decision P2-2's purity extension: no `dba2.data`, no
`datetime.now()`, no bare `random.Random()`, no `open()`, no
`os.environ`.
"""
from __future__ import annotations

from dataclasses import dataclass

from dba2.domain.league import LeagueCapSettings
from dba2.domain.team import GmPhilosophy
from dba2.managers.explain import DecisionRecord, ReasonTerm
from dba2.managers.needs import NeedMatrix, clamp_need_bonus, need_bonus
from dba2.managers.philosophy import PROFILES
from dba2.managers.scouting import player_age
from dba2.managers.snapshot import RosterPlayer
from dba2.managers.tuning import DEFAULT_TUNING, WINDOW_PARAMS, CompetitiveWindow, GmTuning
from dba2.managers.valuation import effective_talent_ovr, market_salary_share


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# §9.3 -- offer_years (interpretation call 1, see module docstring)
# ---------------------------------------------------------------------------

_YEARS_BASE_LO = 3
_YEARS_BASE_HI = 6
_YEARS_CLAMP_LO = 1
_YEARS_CLAMP_HI = 5
_OLD_AGE_SHORT_CUTOFF = 33.0
_YOUNG_AGE_LONG_CUTOFF = 25.0
_OLD_AGE_SHORT_YEARS = 1
"""Behaviour-neutral extraction (Slice 22-B, architect round 23 §4.3) of
the bare literal `offer_years` previously assigned inline at its `age >=
_OLD_AGE_SHORT_CUTOFF` branch -- named so the full duration constant set
is complete for this slice's `GM_TUNING_VERSION` bump, not because its
value changed."""
_YOUNG_BONUS_WINDOWS: tuple[CompetitiveWindow, ...] = (
    CompetitiveWindow.REBUILD,
    CompetitiveWindow.RETOOL,
)


def _base_years_from_window(window: CompetitiveWindow) -> int:
    """Linear map of `window.gamma` onto `[_YEARS_BASE_LO, _YEARS_BASE_HI]`
    -- CONTEND's own `gamma` (0.74) anchors the low end, REBUILD's (0.93)
    the high end.

    **Slice 22-B (architect round 23 §4.2-§4.3): `_YEARS_BASE_LO`/`_HI`
    moved 1/4 -> 3/6, preserving the identical 3-step gamma spread the
    interpolation above divides by.** The raw interpolated map is now
    **6 / 5.2105 / 3.9474 / 3** for REBUILD / RETOOL / WIN_NOW / CONTEND
    -- but `offer_years`' own `[1, 5]` clamp below is now **binding** on
    the top two windows, so the map `offer_years` actually produces is
    **5 / 5 / 4 / 3**: REBUILD and RETOOL are no longer distinguishable in
    contract duration. This is a disclosed, deliberate design cost (round
    23 §4.2 Problem 2), not a bug -- raising `_YEARS_CLAMP_HI` to preserve
    the distinction was considered and rejected, since it overshoots the
    generator's own 3.5714-season equilibrium by 9% (the exact "solve a
    free parameter for a PASS" mistake `_CAP_HOLD_MULT` already made
    once)."""
    contend_gamma = WINDOW_PARAMS[CompetitiveWindow.CONTEND].gamma
    rebuild_gamma = WINDOW_PARAMS[CompetitiveWindow.REBUILD].gamma
    frac = (WINDOW_PARAMS[window].gamma - contend_gamma) / (rebuild_gamma - contend_gamma)
    return round(_YEARS_BASE_LO + (_YEARS_BASE_HI - _YEARS_BASE_LO) * frac)


def offer_years(age: float, window: CompetitiveWindow) -> int:
    """§9.3's `years = f(age, window)` -- see module docstring's
    interpretation call 1 for the full reasoning. Old age overrides
    window entirely (a `_OLD_AGE_SHORT_YEARS`-year offer); young age on a
    rebuilding-postured window adds one year on top of the window's own
    base; every other case is the window's base years, clamped to
    `[_YEARS_CLAMP_LO, _YEARS_CLAMP_HI]` = `[1, 5]`.

    **Slice 22-B: the `[1, 5]` clamp is no longer inert.** Before this
    slice the raw base map (4/3/2/1) never reached either clamp bound.
    After the `_YEARS_BASE_LO`/`_HI` move (see `_base_years_from_window`),
    the clamp is the **binding term for REBUILD and RETOOL** -- both
    windows' raw base (and, for REBUILD, the young-player bonus on top of
    it) get capped at 5, which is why REBUILD and RETOOL now offer
    identical durations at every age band. WIN_NOW and CONTEND are
    unaffected; the clamp does not reach them at this rung."""
    base = _base_years_from_window(window)
    if age >= _OLD_AGE_SHORT_CUTOFF:
        years = _OLD_AGE_SHORT_YEARS
    elif age <= _YOUNG_AGE_LONG_CUTOFF and window in _YOUNG_BONUS_WINDOWS:
        years = base + 1
    else:
        years = base
    return int(_clamp(years, _YEARS_CLAMP_LO, _YEARS_CLAMP_HI))


# ---------------------------------------------------------------------------
# §9.3 -- legal_capacity (interpretation call 2, see module docstring)
# ---------------------------------------------------------------------------


def bird_max_pct_tier(years_pro: int, cap_settings: LeagueCapSettings) -> int:
    """The years-of-service max-salary tier, as a percentage of the cap --
    the identical rule `tools/gm_harness/league.py::_max_pct_tier` applies
    to every generated contract, duplicated here (not imported) because
    `dba2/managers` cannot depend on `tools/` (Decision P2-2's layering
    direction runs the other way: the harness depends on the manager
    layer, never the reverse). See module docstring's interpretation call
    3 for why this function exists at all rather than a fourth `legal_
    capacity` positional argument.

    **Promoted to public (architect round 14 §5.1).** Was `_bird_max_pct_
    tier`; `gm_service.py::_effective_capacity` and `_finalize_signing`
    both need it directly (via `bird_ceiling` below) now that the hard
    `apron_2` gate applies to the stateful ladder too, and a private name
    reads wrong for a cross-module dependency."""
    if years_pro <= 6:
        return cap_settings.max_pct_0_6
    if years_pro <= 9:
        return cap_settings.max_pct_7_9
    return cap_settings.max_pct_10_plus


def apron2_headroom(cap_settings: LeagueCapSettings, apron_room: int) -> int:
    """`apron_room + (apron_2 - apron_1)` -- the `apron_2`-basis headroom
    implied by an `apron_1`-basis ledger value, architect round 14 §3 H4's
    derived-not-stored identity. `apron_room` is on the `apron_1` basis
    (`SeasonCapProjection.apron1_room`, i.e. `ctx.apron1_room_by_team`/
    `run_free_agency`'s own `apron_room_remaining` ledger) -- both read the
    same `season + 1`, `include_holds=False` capsheet row `compute_m2`
    reads at its clean boundaries, so this is a derivation of the same
    quantity `metrics.py::compute_m2` measures, never a second ledger
    (round 14 §3 H4, Decision P2-33's warning against duplicated state)."""
    return apron_room + (cap_settings.apron_2 - cap_settings.apron_1)


def bird_ceiling(
    cap_settings: LeagueCapSettings, *, incumbent_years_pro: int, apron_room: int | None
) -> int:
    """`min(max_pct_tier(incumbent_years_pro) * cap, apron_room)` -- the
    soft, per-incumbent Bird ceiling (round 11's rule), unbounded above
    when `apron_room is None`. One definition, shared by `legal_capacity`'s
    incumbent branch and `gm_service.py`'s `_effective_capacity`/`_finalize_
    signing` (architect round 14 §5.3 note 1: the tag site and the
    capacity site must evaluate the identical expression, or they can
    disagree -- the T3 bug class three critic rounds already closed for
    the non-incumbent ladder)."""
    tier = round(
        bird_max_pct_tier(incumbent_years_pro, cap_settings) / 100.0 * cap_settings.salary_cap
    )
    return tier if apron_room is None else min(tier, apron_room)


def legal_capacity(
    room: int,
    cap_settings: LeagueCapSettings,
    *,
    incumbent_prior_salary: int | None = None,
    incumbent_years_pro: int | None = None,
    apron_room: int | None = None,
) -> int:
    """Largest-available-first: `room` itself when positive (the team has
    real cap space); otherwise the largest of the MLE, the bi-annual, and
    the minimum-salary exception. `room` is the team's own pre-signing
    projected cap room (`managers.capsheet.SeasonCapProjection.room`) --
    caller-supplied, not recomputed here, mirroring `trade.py::cap_delta_
    bonus`'s own `next_season_room_dollars` convention.

    **Floored at `min_salary` even when `room > 0`** (fix round, module
    docstring) -- every team always has at least the minimum exception, so
    a tiny positive `room` (e.g. $1) may not undercut it.

    **The incumbent (Bird-rights) branch, Decision P2-20 (module
    docstring, interpretation call 3).** The first two positional
    parameters and the default behaviour are completely unchanged --
    `legal_capacity(room, cap_settings)` returns exactly what it always
    has. When `incumbent_prior_salary` is not `None` (an incumbent
    re-signing its own team, never a fresh free agent -- see `interpretation
    call 3` for why this parameter, not a bare flag, is the gate), the
    bucket is **per incumbent, not once per pass** (unlike the MLE/
    bi-annual buckets `_effective_capacity` tracks): `bird_ceiling(...)`
    above, and the return value before the hard clamp is `max(today's
    ladder, bird_ceiling)`. **`apron_room` is a ceiling on the resulting
    salary, never a floor** -- a team already over its own `apron_room`
    gets `bird_ceiling <= 0`, which `max()` with the unchanged ladder
    simply discards; it never *reduces* what a team could otherwise
    offer.

    **The hard `apron_2` clamp (architect round 14 §5.2/§5.3).** When
    `apron_room` is not `None`, the final return is additionally clamped
    to `apron2_headroom(cap_settings, apron_room)` -- a **second**, global
    ceiling applied after the per-bucket `max()` above, `GAP = apron_2 -
    apron_1` above the soft one and on the same `apron_1`-basis ledger.
    This is what makes the `apron_2` legality invariant hold structurally
    (round 14 §5.2's three-line proof) rather than merely bounding the
    Bird bucket alone (round 11 §5.5 item 3's now-superseded claim -- see
    that test's rewrite). `apron_room` stays a ceiling only, never a
    floor: both clamps are `min`, so they can only reduce.

    Three properties this shape guarantees (round 14 §5.3):
    1. `legal_capacity(room, cap_settings)` -- two positional args, no
       `apron_room` -- is **exactly** today's function; every existing
       caller (including `construct_offer`'s own internal call) is
       byte-identical.
    2. `apron_room` is a **per-bucket** ceiling on the Bird term at the
       `apron_1` basis (item 3's rule, preserved) *and additionally* a
       **global** ceiling at the `apron_2` basis -- different numbers,
       `GAP` apart; conflating them is the mutation item 3 was written to
       kill.
    3. `apron_room` is still never a floor."""
    if room > 0:
        ladder = max(room, cap_settings.min_salary)
    else:
        ladder = max(
            cap_settings.mle_amount, cap_settings.bi_annual_amount, cap_settings.min_salary
        )
    if incumbent_prior_salary is None:
        bucket = ladder
    else:
        assert incumbent_years_pro is not None, (
            "legal_capacity's incumbent branch requires incumbent_years_pro whenever "
            "incumbent_prior_salary is provided (module docstring, interpretation call 3)"
        )
        bucket = max(
            ladder,
            bird_ceiling(
                cap_settings, incumbent_years_pro=incumbent_years_pro, apron_room=apron_room
            ),
        )
    if apron_room is None:
        return bucket
    return min(bucket, apron2_headroom(cap_settings, apron_room))


# ---------------------------------------------------------------------------
# §9.3 -- willingness and salary
# ---------------------------------------------------------------------------

_WILLINGNESS_LO = 0.70
_WILLINGNESS_HI = 1.60


def willingness(need_bonus_raw: float, window: CompetitiveWindow) -> float:
    """`clamp(1 + clamp_need_bonus(need_bonus) + window_urgency, 0.70,
    1.60)` (§9.3) -- see module docstring for why the upper bound is a
    dormant, forward-looking guard rather than dead code."""
    params = WINDOW_PARAMS[window]
    bonus = clamp_need_bonus(need_bonus_raw)
    return _clamp(1.0 + bonus + params.window_urgency, _WILLINGNESS_LO, _WILLINGNESS_HI)


def offer_salary(
    effective_ovr: int,
    cap_settings: LeagueCapSettings,
    legal_capacity_dollars: int,
    need_bonus_raw: float,
    window: CompetitiveWindow,
) -> int:
    """`min(legal_capacity, max(market_salary(effective_ovr) * willingness,
    min_salary))` (§9.3, extended by the fix round's minimum-salary floor
    -- see module docstring). The floor is applied before the legal-
    capacity cap, never after, so `legal_capacity_dollars` stays this
    function's one hard outer bound regardless of what floors it. Rounded
    to the nearest dollar."""
    market = market_salary_share(float(effective_ovr), cap_settings) * cap_settings.salary_cap
    w = willingness(need_bonus_raw, window)
    floored = max(market * w, float(cap_settings.min_salary))
    return round(min(float(legal_capacity_dollars), floored))


# ---------------------------------------------------------------------------
# §9.3 -- one team's one offer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SigningOffer:
    team_id: int
    player_id: int
    years: int
    annual_salary: int
    window: CompetitiveWindow

    @property
    def guaranteed_total(self) -> int:
        """`annual_salary * years` -- a flat schedule, no raises modeled
        (see module docstring)."""
        return self.annual_salary * self.years


def construct_offer(
    roster_player: RosterPlayer,
    matrix: NeedMatrix,
    *,
    team_id: int,
    philosophy: GmPhilosophy,
    window: CompetitiveWindow,
    cap_settings: LeagueCapSettings,
    room: int,
    season: int,
    tuning: GmTuning = DEFAULT_TUNING,
) -> tuple[SigningOffer, DecisionRecord]:
    """One team's full §9.3 offer for one free agent, plus its Decision
    P2-18 `DecisionRecord` (module docstring): `offer_years` from the
    player's own age and this team's window, `offer_salary` from the
    player's `effective_talent_ovr`, this team's legal capacity, and this
    team's own `need_bonus` for the player at that talent scalar.

    **The record's `score` is the unrounded salary**, additively
    decomposed exactly (`market_salary`, `need_bonus`, `window_urgency`,
    `willingness_clamp`, `min_salary_floor`, `legal_capacity_cap` -- see
    module docstring for each term's meaning); `SigningOffer.annual_
    salary` is that same figure rounded to the nearest dollar
    (`offer_salary`'s own contract), so the two can differ by at most
    $0.50 -- `record.reconstructs()` checks the record's own internal sum,
    not agreement with the rounded offer."""
    profile = PROFILES[philosophy]
    ovr = effective_talent_ovr(roster_player, profile)
    age = float(player_age(roster_player.player.birth_date, season))
    years = offer_years(age, window)
    capacity = legal_capacity(room, cap_settings)
    bonus_raw = need_bonus(roster_player, ovr, matrix, profile, tuning=tuning)
    salary = offer_salary(ovr, cap_settings, capacity, bonus_raw, window)

    market = market_salary_share(float(ovr), cap_settings) * cap_settings.salary_cap
    bonus = clamp_need_bonus(bonus_raw)
    urgency = WINDOW_PARAMS[window].window_urgency
    raw_willingness = 1.0 + bonus + urgency
    clamped_willingness = willingness(bonus_raw, window)
    pre_floor = market * clamped_willingness
    floored = max(pre_floor, float(cap_settings.min_salary))
    capped = min(float(capacity), floored)

    terms = (
        ReasonTerm(label="market_salary", input_value=market, weight=1.0, contribution=market),
        ReasonTerm(
            label="need_bonus", input_value=bonus, weight=market, contribution=market * bonus,
        ),
        ReasonTerm(
            label="window_urgency", input_value=urgency, weight=market,
            contribution=market * urgency,
        ),
        ReasonTerm(
            label="willingness_clamp", input_value=raw_willingness, weight=1.0,
            contribution=market * (clamped_willingness - raw_willingness),
        ),
        ReasonTerm(
            label="min_salary_floor", input_value=float(cap_settings.min_salary), weight=1.0,
            contribution=floored - pre_floor,
        ),
        ReasonTerm(
            label="legal_capacity_cap", input_value=float(capacity), weight=1.0,
            contribution=capped - floored,
        ),
    )
    record = DecisionRecord(
        team_id=team_id, philosophy=philosophy, window=window,
        subject=f"player:{roster_player.id}", verdict="offer",
        score=sum(term.contribution for term in terms), threshold=float(capacity), terms=terms,
    )

    offer = SigningOffer(
        team_id=team_id,
        player_id=roster_player.id,
        years=years,
        annual_salary=salary,
        window=window,
    )
    return offer, record
