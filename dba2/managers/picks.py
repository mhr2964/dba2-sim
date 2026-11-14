"""Draft pick and prospect valuation -- the two-column slot table, the
rookie-contract sum against `rookie_scale`, the unresolved discount and
expected-slot regression, protection conveyance, and swap rights
(docs/design/phase-2-manager-minds.md §4.6, §12 step 5).

**Decision P2-33 -- the one rule this whole module exists to get right,
stated up front:** `rookie_share_y` is an **exact `rookie_scale` row
lookup**, everywhere in this module, with **no interpolation anywhere**.
Round 3 shipped two different implementations of this one function inside
a single design-doc section and they silently disagreed at 24 of 30
slots. There is exactly one implementation here (`rookie_share_y`, below),
and every other function that needs a rookie-scale figure calls it rather
than re-deriving one.

**Known, documented, temporary inconsistency -- read before touching row
30 of anything.** Decision P2-33 also repairs `rookie_scale` row 30 (a
corrupt migration-003 seed row -- **all four year-columns are corrupt, not
year 4 alone**: y1/y2/y3 are each ~-33.99% below row 29's own figure, and
y4 is ~-37.56% below row 29's, against columns that otherwise decline a
steady 2.4-2.7%/slot (y1-y3) or 4.3-4.6%/slot (y4) from row 25 through row
29 -- so migration 009 must rewrite all four values, not `year_4` alone; a
year-4-only repair leaves the 29->30 inversion below fully intact, all
28/28 (philosophy, window) pairs still inverted) -- but that repair is
**migration 009's job, which is step 7's (`managers/capsheet.py`)
deliverable, not this one's**.
This module reads `rookie_scale` exactly as the caller's `RookieScaleTable`
supplies it, and at every step-5-and-earlier call site the only table that
exists is migration 003's own, un-repaired, corrupt-row-30 data. Repairing
row 30 here would be a second, undocumented copy of a fix that belongs in
exactly one migration -- the same reasoning that got
`tools/_verify_gm_design_numbers.py` (which applied Decision P2-33's
repair *inline*, for design-time-only reasons stated in its own now-gone
header) **deleted at this step**, per the design doc's own explicit
instruction: `valuation.py`/`picks.py` plus the checked-in archetype
golden tables (`seeds/gm_value_archetypes.json`,
`seeds/gm_pick_archetypes.json`) now serve the drift-protection role that
script was standing in for, and a second live implementation of §4.6's
math is exactly the failure mode P2-33 exists to close (critic round 10,
B4 -- the script and this module had started to disagree at slot 30,
145.8 vs 168.4, before the script was removed).

**The consequence, stated rather than worked around:** with the real,
currently-seeded (un-repaired) row 30, `pick_value(30, ...)` prices
**above** `pick_value(29, ...)` by roughly 12.3% at every (philosophy,
window) pair -- the exact inversion Decision P2-33 exists to close, and
the design doc's own text says so explicitly (§4.6: "under the exact
lookup ... pick 30 prices 12.3% above pick 29 at all 28 (philosophy,
window) pairs, so §12 step 5's monotonicity assertion fails on the
builder's first run"). `tests/unit/managers/test_picks.py` asserts
monotonicity across slots 1-60 with the 29->30 boundary carved out and
explicitly labeled as this known, migration-009-pending gap -- not
silently skipped, and not silently patched around by repairing the seed
data in this module, which is out of this step's scope. **That boundary
test is a fixture-level tripwire only** (it prices against this test
suite's own hand-transcribed `ROOKIE_SCALE` fixture, never against a real
migration) -- it is not, and cannot be, a regression guard for migration
009 landing correctly; step 7 owns writing that guard once migration 009
exists (critic round 10, B2; see the boundary test's own docstring for
the full honest scope note). Once migration 009 lands (step 7) and a
caller supplies the repaired table, the monotonicity assertion here holds
with no boundary exception.

Zero I/O, per Decision P2-2's purity extension: no `dba2.data`, no
`datetime.now()`, no bare `random.Random()`, no `open()`, no
`os.environ`. `RookieScaleTable` is a plain `Mapping` the caller supplies
(`services/gm_service.py`, not built this dispatch, would build one from
`contract_repo.list_rookie_scale`) -- `managers/` may not import `dba2.data`
(Decision P2-2(b)), so this module never reads the table itself, exactly
the same shape `market_value` takes an explicit `LeagueCapSettings` rather
than reading one from `dba2.data`.

Imports `managers.valuation` (`base_value`, `age_mult_eff`,
`market_salary_share`, `logistic` -- the exact same talent-curve, age-
curve, market-salary-curve, and logistic implementations `market_value`
itself uses, reused rather than re-derived, for the same reason Decision
P2-33 exists: a second implementation of a formula this module already
has one of is exactly how §4.6 acquired its worst bugs) and
`managers.window` (`window_score`, for the expected-slot regression --
"now able to call step 4's `window_score`," §12's stated reason `picks.py`
is built after `window.py`).
"""
from __future__ import annotations

from collections.abc import Mapping

from dba2.domain.contract import RookieScale
from dba2.domain.league import LeagueCapSettings
from dba2.domain.team import GmPhilosophy
from dba2.managers.philosophy import PROFILES
from dba2.managers.scouting import age_ramp
from dba2.managers.snapshot import LeagueSnapshot, TeamSnapshot
from dba2.managers.tuning import DEFAULT_TUNING, WINDOW_PARAMS, CompetitiveWindow, GmTuning
from dba2.managers.valuation import age_mult_eff, base_value, logistic, market_salary_share
from dba2.managers.window import window_score

RookieScaleTable = Mapping[int, RookieScale]
"""`pick_number` (1-30) -> that pick's `RookieScale` row -- the shape a
caller builds from `contract_repo.list_rookie_scale` (keyed by
`RookieScale.pick_number`). Read via `rookie_share_y`'s exact lookup only;
slots 31-60 never index into it (Decision P2-27)."""


# ---------------------------------------------------------------------------
# §4.6 -- the two-column slot table (entry overall / peak overall)
# ---------------------------------------------------------------------------

# The eight anchor picks and their entry/peak overalls, verbatim (§4.6).
_SLOT_ANCHORS: tuple[int, ...] = (1, 3, 5, 10, 20, 30, 45, 60)
_ENTRY_ANCHOR_VALUES: tuple[float, ...] = (68.0, 67.0, 65.0, 63.0, 60.0, 58.0, 55.0, 52.0)
_PEAK_ANCHOR_VALUES: tuple[float, ...] = (82.0, 79.0, 77.0, 74.0, 70.0, 68.0, 64.0, 61.0)

_ENTRY_ANCHORS: tuple[tuple[float, float], ...] = tuple(
    zip(_SLOT_ANCHORS, _ENTRY_ANCHOR_VALUES, strict=True)
)
_PEAK_ANCHORS: tuple[tuple[float, float], ...] = tuple(
    zip(_SLOT_ANCHORS, _PEAK_ANCHOR_VALUES, strict=True)
)


def _piecewise_linear(x: float, anchors: tuple[tuple[float, float], ...]) -> float:
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:], strict=False):
        if x0 <= x <= x1:
            frac = (x - x0) / (x1 - x0)
            return y0 + (y1 - y0) * frac
    raise AssertionError(f"{x} not covered by anchors {anchors}")  # pragma: no cover


def entry_ovr(slot: float) -> float:
    """`E[entry overall]` at age 20, piecewise-linear through §4.6's eight
    anchor picks, clamped flat outside them."""
    return _piecewise_linear(slot, _ENTRY_ANCHORS)


def peak_ovr(slot: float) -> float:
    """`E[peak overall]`, the same eight-anchor piecewise-linear table
    (§4.6)."""
    return _piecewise_linear(slot, _PEAK_ANCHORS)


def implied_residual(slot: float, *, tuning: GmTuning = DEFAULT_TUNING) -> float:
    """§4.6's restated cross-check between the two slot-table columns and
    §4.3.1's `age_ramp`/residual model:

    `((peak(slot) - entry(slot)) / (GROWTH_MAX * age_ramp(20, PEAK_AGE_REF)) - 1) / RESIDUAL_GAIN`

    Not read by `pick_value` at all -- this is the slot table's own
    internal-consistency check (§12 step 5: "the implied residual stays in
    `[-1, +1]` and is monotone non-increasing in slot"), inverting §4.3.1's
    model instead of asserting a loose growth-agreement band, which is the
    form round 2's version had no power to fail on."""
    denom = tuning.growth_max * age_ramp(20.0, tuning.peak_age_ref, tuning=tuning)
    return ((peak_ovr(slot) - entry_ovr(slot)) / denom - 1.0) / tuning.residual_gain


def pick_horizon(slot: int, *, tuning: GmTuning = DEFAULT_TUNING) -> int:
    """`H(slot) = ROOKIE_HORIZON` (4 seasons) for `slot <= 30`,
    `SECOND_ROUND_HORIZON` (2 seasons) for `slot >= 31` (§4.6, Decision
    P2-27 -- "a second-round pick does not sign a four-year scale
    contract; it signs a short minimum deal with no scale protection")."""
    return tuning.rookie_horizon if slot <= 30 else tuning.second_round_horizon


def e_ovr_y(slot: float, y: int, *, tuning: GmTuning = DEFAULT_TUNING) -> float:
    """`entry(slot) + (peak(slot) - entry(slot)) * y / (PEAK_AGE_REF - 20)`
    (§4.6) -- linear progression from the entry overall toward the peak
    overall, reaching `peak(slot)` only at `y = PEAK_AGE_REF - 20 = 6`, so
    a 4-season rookie deal (`y = 0..3`) captures only half the projected
    growth (§4.6's own stated reason "teams re-sign their own
    first-rounders")."""
    entry = entry_ovr(slot)
    peak = peak_ovr(slot)
    return entry + (peak - entry) * y / (tuning.peak_age_ref - 20.0)


def rookie_share_y(
    slot: int,
    y: int,
    rookie_scale: RookieScaleTable,
    cap_settings: LeagueCapSettings,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """Decision P2-33's rule, and the *only* form this module implements
    it in: an **exact `rookie_scale` row lookup**, never an interpolation.

    `slot <= 30` reads `rookie_scale[slot]`'s `year_{y+1}_salary` column
    directly as a share of `cap_settings.salary_cap` -- raises `KeyError`
    if the caller's table is missing that row, deliberately: a silent
    fallback here would be exactly the "smoothing a data error inside a
    formula" failure mode Decision P2-33 exists to close, one layer worse
    than the bug it replaces. `slot >= 31` reads `cap_settings.min_salary`
    (Decision P2-27 -- a second-round pick signs a short minimum deal, not
    a rookie-scale contract, so it was never in `rookie_scale` to begin
    with -- `rookie_scale` has exactly 30 rows and this branch is why
    `pick_value(31, ...)` never indexes into it)."""
    if slot <= 30:
        row = rookie_scale[slot]
        salaries = (row.year_1_salary, row.year_2_salary, row.year_3_salary, row.year_4_salary)
        return salaries[y] / cap_settings.salary_cap
    return cap_settings.min_salary / cap_settings.salary_cap


# ---------------------------------------------------------------------------
# §4.6 -- pick_value itself
# ---------------------------------------------------------------------------


def pick_value(
    slot: int,
    years_out: int,
    philosophy: GmPhilosophy,
    window: CompetitiveWindow,
    *,
    rookie_scale: RookieScaleTable,
    cap_settings: LeagueCapSettings,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """§4.6's full formula, verbatim:

    `gamma^years_out * UNRESOLVED_DISCOUNT[years_out] * pick_pref
     * Sum_{y=0..H(slot)-1} gamma^y * [ base_value(E_ovr_y, kappa) * age_mult_eff(20 + y)
                                       + surplus_weight * (market_salary(E_ovr_y) - rookie_share_y)
                                                         * VP_PER_CAP_SEASON ]`

    `base_value`, `age_mult_eff`, and `market_salary_share` are the exact
    functions `market_value` (`managers/valuation.py`) uses -- reused, not
    re-derived, so a talent- or age-curve tuning change can never
    silently apply to players but not picks (or the reverse). `age_mult_eff`
    is evaluated at `age = 20 + y` with no `vet_bias` reshape ever binding
    (a rookie-scale season never reaches age 29), so `vet_bias`'s presence
    here is only for API symmetry with `market_value`'s own call.

    `rookie_scale`/`cap_settings` carry the module docstring's documented
    known-inconsistency: whatever table the caller supplies is read
    exactly (Decision P2-33), including an un-repaired row 30 if that is
    what the caller has -- this function does not know or care whether
    migration 009 has run."""
    profile = PROFILES[philosophy]
    params = WINDOW_PARAMS[window]
    horizon = pick_horizon(slot, tuning=tuning)

    total = 0.0
    for y in range(horizon):
        ovr_y = e_ovr_y(slot, y, tuning=tuning)
        age = float(20 + y)
        a_mult = age_mult_eff(age, vet_bias=profile.vet_bias, age_exponent=params.age_exponent)
        talent = base_value(ovr_y, profile.kappa) * a_mult

        share = rookie_share_y(slot, y, rookie_scale, cap_settings, tuning=tuning)
        surplus = (
            profile.surplus_weight
            * (market_salary_share(ovr_y, cap_settings) - share)
            * tuning.vp_per_cap_season
        )
        total += (params.gamma**y) * (talent + surplus)

    discount = (
        (params.gamma**years_out) * tuning.unresolved_discount_for(years_out) * profile.pick_pref
    )
    return discount * total


# ---------------------------------------------------------------------------
# §4.6 -- expected slot for an unresolved pick
# ---------------------------------------------------------------------------


def rank_teams_by_projected_slot(
    league_snapshot: LeagueSnapshot, *, tuning: GmTuning = DEFAULT_TUNING
) -> dict[int, int]:
    """The `{team_id: projected_slot}` ranking `projected_slot_from_window_
    score` computes internally, pulled out as its own function (B3 perf
    fix, critic round 17). A caller pricing many unresolved picks across
    one pass (`services/gm_service.py::run_trade_window`, via
    `ValuationCache`) can rank all of `league_snapshot`'s teams by
    `window_score` exactly once and reuse the result, instead of every one
    of thousands of `_price_pick` calls in that pass re-ranking all 30
    teams from scratch (critic measured 102,090 `window_score` calls,
    driven by 3,403 pick-pricing calls x 30 teams) -- the ranking is fixed
    for the lifetime of one `(league_snapshot, tuning)` pair; nothing in
    it varies per pick or per team being priced."""
    scores = {t.id: window_score(t, league_snapshot, tuning=tuning) for t in league_snapshot.teams}
    worst_first = sorted(scores, key=lambda tid: scores[tid])
    return {team_id: rank for rank, team_id in enumerate(worst_first, start=1)}


def projected_slot_from_window_score(
    team_snapshot: TeamSnapshot,
    league_snapshot: LeagueSnapshot,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
    precomputed_ranks: Mapping[int, int] | None = None,
) -> int:
    """Ranks every team in `league_snapshot` by `window_score` (step 4)
    and inverts to lottery order -- the *worst* `window_score` gets
    `projected_slot = 1` (§4.6: "rank all 30 teams by `window_score`,
    invert to lottery order"). Ties are broken by iteration order over
    `league_snapshot.teams` (stable sort), which this module makes no
    claim about resolving fairly -- a real lottery's tiebreak is a Phase 3
    concern, not this formula's.

    `precomputed_ranks` (B3 perf fix, critic round 17) -- an optional
    `rank_teams_by_projected_slot(league_snapshot, tuning)` result a
    caller has already computed once for this exact `(league_snapshot,
    tuning)` pair; when supplied, this call does zero re-ranking, just a
    dict lookup. `None` (the default) reproduces the original from-scratch
    behavior exactly, so every existing caller/test is unaffected."""
    ranks = (
        precomputed_ranks
        if precomputed_ranks is not None
        else rank_teams_by_projected_slot(league_snapshot, tuning=tuning)
    )
    if team_snapshot.id not in ranks:
        raise KeyError(f"team {team_snapshot.id} is not in this league_snapshot")
    return ranks[team_snapshot.id]


def expected_slot(projected_slot: float, years_out: int) -> float:
    """`15.5 + (projected_slot - 15.5) * 0.60^years_out` (§4.6) -- regresses
    a projected finish toward the league's mean lottery position (15.5) as
    the pick gets further out, "the honest answer, and the one that stops
    AI GMs from paying superstar prices for far-future picks." Today's
    worst team's pick (`projected_slot = 1`) lands at slot **12.4** three
    years out and **13.6** four years out, per §4.6's own worked check."""
    return 15.5 + (projected_slot - 15.5) * (0.60**years_out)


# ---------------------------------------------------------------------------
# §4.6 -- protection conveyance, with its rollover recursion
# ---------------------------------------------------------------------------


def conveyance_probability(
    expected_slot_value: float, protection_through: int, *, tuning: GmTuning = DEFAULT_TUNING
) -> float:
    """`P = logistic((expected_slot - protection_through) / SLOT_SIGMA)`
    (§4.6) -- the probability the pick lands *outside* its protection
    (i.e. conveys) this season."""
    return logistic((expected_slot_value - protection_through) / tuning.slot_sigma)


def slot_if_conveys(expected_slot_value: float, protection_through: int) -> int:
    """`max(protection_through + 1, expected_slot)`, rounded to the
    nearest integer slot for `pick_value`'s own lookup (§4.6). Conveying
    means the pick fell *outside* its protection -- never at the
    unconditional `expected_slot` when that slot would itself be
    protected, which is round 1's fixed error (§4.6: "a conveyed pick was
    priced at the *unconditional* expected slot even though conveyance
    means the pick landed outside the protection")."""
    return round(max(protection_through + 1, expected_slot_value))


def second_round_fallback(
    years_out: int,
    philosophy: GmPhilosophy,
    window: CompetitiveWindow,
    *,
    rookie_scale: RookieScaleTable,
    cap_settings: LeagueCapSettings,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """`SECOND_ROUND_FALLBACK = SECOND_ROUND_FALLBACK_PICKS *
    pick_value(SECOND_ROUND_FALLBACK_SLOT, years_out, ...)` (§4.6) -- two
    second-rounders (the standard real conversion), priced at slot 45,
    what a fully-rolled protection converts into after `ROLLOVER_MAX`
    rolls."""
    return tuning.second_round_fallback_picks * pick_value(
        tuning.second_round_fallback_slot,
        years_out,
        philosophy,
        window,
        rookie_scale=rookie_scale,
        cap_settings=cap_settings,
        tuning=tuning,
    )


def protected_pick_value(
    expected_slot_value: float,
    protection_through: int,
    philosophy: GmPhilosophy,
    window: CompetitiveWindow,
    *,
    rookie_scale: RookieScaleTable,
    cap_settings: LeagueCapSettings,
    years_out: int = 0,
    rollover_count: int = 0,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """§4.6's protection recursion, verbatim:

    `protected_value(expected_slot, prot, years_out, k) =
          P * pick_value(slot_if_conveys, years_out, ...)
        + (1 - P) * ( protected_value(expected_slot, prot, years_out + 1, k + 1)
                      if k < ROLLOVER_MAX
                      else   SECOND_ROUND_FALLBACK(years_out + 1) )`

    with `ROLLOVER_MAX = 2` -- a protection rolls at most twice before the
    obligation converts to two slot-45 picks. **One discount, not two**
    (§4.6): the recursion advances `years_out`, which is the *only* place
    time is priced -- there is no extra `gamma` multiplied in at this
    level, since `pick_value` already carries `gamma^years_out *
    UNRESOLVED_DISCOUNT[years_out]` on its own. The recursion terminates
    in at most `ROLLOVER_MAX + 1 = 3` levels, a closed sum, not an
    integral, and Python's default recursion limit is not a concern at
    that depth.

    Unrolled, this is exactly (with `P` fixed across all levels since
    `expected_slot_value`/`protection_through` never change across a
    roll): `P*V(0) + (1-P)*P*V(1) + (1-P)^2*P*V(2) + (1-P)^3*FALLBACK(3)`
    -- the four terms §4.6's worked example names "conveys this year,"
    "rolls once, conveys at slot," "rolls twice, conveys at slot," and
    "converts to two slot-45 seconds."""
    p = conveyance_probability(expected_slot_value, protection_through, tuning=tuning)
    conveyed_slot = slot_if_conveys(expected_slot_value, protection_through)

    conveys_now = p * pick_value(
        conveyed_slot,
        years_out,
        philosophy,
        window,
        rookie_scale=rookie_scale,
        cap_settings=cap_settings,
        tuning=tuning,
    )

    if rollover_count < tuning.rollover_max:
        rolled = (1.0 - p) * protected_pick_value(
            expected_slot_value,
            protection_through,
            philosophy,
            window,
            rookie_scale=rookie_scale,
            cap_settings=cap_settings,
            years_out=years_out + 1,
            rollover_count=rollover_count + 1,
            tuning=tuning,
        )
    else:
        rolled = (1.0 - p) * second_round_fallback(
            years_out + 1,
            philosophy,
            window,
            rookie_scale=rookie_scale,
            cap_settings=cap_settings,
            tuning=tuning,
        )

    return conveys_now + rolled


# ---------------------------------------------------------------------------
# §4.6 -- swap rights
# ---------------------------------------------------------------------------

_SWAP_RIGHTS_FACTOR = 0.5
"""§4.6: "the 0.5 is a stated approximation of the option's expected value
under slot uncertainty, not a computed integral, and it is named as an
approximation because a critic will otherwise (correctly) ask." Local to
this module (not `tuning.py`) since it is used by exactly one formula, per
`tuning.py`'s own "genuinely local to one module's own data table" scope
note."""


def swap_rights_value(own_pick_value: float, other_pick_value: float) -> float:
    """`max(0, pick_value(own) - pick_value(other)) * 0.5`, added to the
    holder of the swap right (§4.6). Both arguments are already-computed
    `pick_value` outputs (or `protected_pick_value` outputs, for a
    protected swap) -- this function does no pricing of its own, only the
    swap-option approximation on top."""
    return max(0.0, own_pick_value - other_pick_value) * _SWAP_RIGHTS_FACTOR
