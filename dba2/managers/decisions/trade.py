"""Trade evaluation -- `fit_value`, package evaluation, the acceptance
threshold, `cap_delta_bonus`, and Decision P2-19/P2-20's legality gates
(docs/design/phase-2-manager-minds.md §9.1, §9.2, §12 step 8).

**`fit_value`'s home.** `needs.py`'s own module docstring (step 6) states
this explicitly: `fit_value = market_value(...) * (1 + clamp_need_bonus(
need_bonus(...)))`, built here rather than in `needs.py` because it is
consumed by exactly this module and nowhere else at step 6's own time.
`fit_value` (the primitive, mirroring `market_value`'s explicit-`ovr`/
explicit-`ScoutedPeak` shape) and `fit_value_for` (the convenience
wrapper over `market_value_for`, mirroring its identity-holding shape)
both live here for the same reason the raw/`_for` split exists in
`valuation.py`.

**Interpretation call 1 -- a pick's `fit_value`-equivalent contribution.**
§9.2 states `net_A`'s two sums generically over "`a in I`"/"`b in O`," and
§5's need matrix is keyed by `Position` -- a `DraftPick` has no position,
so it cannot be run through `need_bonus`/`clamp_need_bonus` at all. Read
here as the plain consequence stated in this step's own task description:
a pick's contribution to `net_A` is `pick_value(...)` (via `picks.py`,
step 5, reused rather than re-derived) directly, with no need-bonus wrap
-- `fit_value` itself is defined only for `RosterPlayer`s. `_price_pick`
below is the one place a `DraftPick` in a bundle is turned into a VP
figure: an already-`pick_number`-assigned pick reads `pick_value` at its
real slot; an unresolved pick needs a `LeagueSnapshot` to project its
`original_team_id`'s finish (`picks.py::projected_slot_from_window_score`/
`expected_slot`), so `evaluate_trade` takes an optional `league_snapshot`
and raises rather than guessing if an unresolved pick shows up in a bundle
with none supplied.

**Interpretation call 2 -- Decision P2-20's CBA depth dial (revised, critic
round 14 B2).** The design doc's own text is explicit that this is "Phase
2's own simplified subset," not the real CBA, so no base-year compensation,
no repeater tax, no exception-specific hard-cap triggers (MLE hard cap etc.)
are modelled anywhere below (per this step's own explicit exclusion) --
stated here rather than silently omitted. Round 14's critic reproduced a
concrete case where an invented "100% matching above apron 1" band let a
team cross `apron_1` by $6.7M with the trade still ruled legal, because
nothing checked `apron_1` as a ceiling in its own right -- only as a switch
between two matching-ratio bands. Replaced with three independent rules,
none of them a band on the other two:

- **Salary matching** -- the one and only matching-ratio rule, active
  whenever the team is over the cap *before* the trade (`used_pre >
  salary_cap`), independent of either apron: `incoming <= outgoing * 1.25 +
  100,000`. There is no separate, tighter band for a team already over
  `apron_1` -- `apron_1` is enforced below, as a ceiling, not as a
  matching-ratio switch.
- **`apron_1` ceiling** -- a hard block: post-trade `used_post` may not
  exceed `apron_1`, checked independently of the matching rule above (a
  trade can satisfy the 125%+$100k matching ratio and still push `used_post`
  over `apron_1`, exactly the case critic round 14 reproduced).
- **`apron_2` ceiling** -- a second, stricter hard block: post-trade
  `used_post` may not exceed `apron_2` either. Checked as its own explicit
  condition rather than assumed subsumed by the `apron_1` check, since a
  team can sit between the two aprons and only one of the two checks fires.
  This is also where P2-19's separately-stated "leaves either team ... over
  apron_2 ... rejected outright" and P2-20's "apron 2 as a hard trade
  block" turn out to be **the same constraint stated twice, not two
  independent ones**, once `apron_1`'s own ceiling is broken out on its
  own: P2-19 names the apron_2 *destination* state, P2-20 names both
  aprons as hard blocks, and the two straightforward ceiling checks here
  (`apron_1`, `apron_2`) satisfy both statements at once. A team already
  over apron 2 that sheds enough salary to land back under apron 2 is
  legal (both P2-19's and this gate's own text read the *post-trade*
  figure only); a team that stays over apron 2 post-trade, whether it was
  over apron 2 already or crossed into it via this trade, is illegal. No
  second, silently-overlapping "no net increase for an over-apron-2 team"
  check is built on top of it: a stricter "no net increase, ever, even
  while falling back under apron 2" reading was considered and rejected,
  because P2-19's own text names the *destination* state, not the
  *direction* of the change, as what makes a trade illegal.

`_check_legality` only ever sees `team_snapshot`'s own side of the trade;
"either team involved" (both B2's fix text and P2-19's own phrasing) is
satisfied structurally by `evaluate_trade`'s own documented contract that a
caller evaluating both sides calls this twice, once per team, with
`incoming`/`outgoing`/`team_snapshot` swapped -- each call's own apron
checks cover that call's own team.

**The "next season" anchor.** `salary_next`, `cap_delta_bonus`, and every
legality/matching gate below all read the same `next_season`/
`next_season_cap_settings`/`next_season_room_dollars` triple -- the single
season a caller (`services/gm_service.py`, step 10, not built this
dispatch) designates as "the season this trade takes effect for cap
purposes" (§9.2's own phrase for `salary_next`, generalized here to the
gates too, since evaluating legality against a *different* season than
the salary figures being compared would be internally inconsistent).
`roster_min`/`roster_max`/`min_salary` are read off `next_season_cap_
settings` for the same reason, even though they are rule constants that
never actually differ from the current season's row (`capsheet.py::
project_cap_settings`'s own documented behavior) -- one fewer parameter,
no behavior change.

**`chaos`'s seeded jitter lives here, not in `philosophy.py`.**
`philosophy.py`'s own module docstring names this module by name: "`chaos`
... is a multiplier (its `risk` is the highest in the table, plus the
seeded jitter §6 describes -- applied by `managers/decisions/trade.py`,
not here)" (§6): `x clamp(1 + N(0, 0.25),
0.5, 1.6)`, drawn from `rng_for(league_rng_seed, "chaos", team_id,
asset_id, season, phase)`. Applied only inside `fit_value_for` (the
identity-holding entry point -- the raw `fit_value` primitive stays a pure
function of its explicit inputs, mirroring `market_value`/`market_value_
for`'s own split) and only to a `RosterPlayer`'s `fit_value`, never to a
pick's `pick_value` -- §6's own text says "its own final `fit_value`,"
and a pick has no `fit_value` at all (interpretation call 1, above), so
there is nothing for the jitter to multiply there. This is itself a third
interpretation call, flagged for critic alongside the two above: the
design doc names the jitter as part of §6's philosophy table, not §9.2's
trade formula, so it was easy to miss entirely -- `rng_for` is a pure,
deterministic derivation (no bare `random.Random()`, no wall-clock read),
so applying it here does not violate this module's zero-I/O requirement.

Zero I/O, per Decision P2-2's purity extension: no `dba2.data`, no
`datetime.now()`, no bare `random.Random()` (only `rng_for`'s
deterministic derivation), no `open()`, no `os.environ`.
"""
from __future__ import annotations

from collections.abc import Mapping

from dba2.core.rng import rng_for
from dba2.domain.draft import DraftPick, ProtectionType
from dba2.domain.league import LeagueCapSettings
from dba2.domain.phase import Phase
from dba2.domain.team import GmPhilosophy
from dba2.managers.explain import DecisionRecord, ReasonTerm
from dba2.managers.needs import NeedMatrix, build_need_matrix, clamp_need_bonus, need_bonus
from dba2.managers.philosophy import PROFILES, PhilosophyProfile
from dba2.managers.picks import (
    RookieScaleTable,
    expected_slot,
    pick_value,
    projected_slot_from_window_score,
    protected_pick_value,
)
from dba2.managers.scouting import ScoutedPeak
from dba2.managers.snapshot import AssetBundle, LeagueSnapshot, RosterPlayer, TeamSnapshot
from dba2.managers.tuning import (
    DEFAULT_TUNING,
    WINDOW_PARAMS,
    CompetitiveWindow,
    GmTuning,
    WindowParams,
)
from dba2.managers.valuation import effective_talent_ovr, market_value, market_value_for

_CHAOS_JITTER_SD = 0.25
_CHAOS_JITTER_LO = 0.5
_CHAOS_JITTER_HI = 1.6
"""§6's chaos-jitter constants, verbatim: `clamp(1 + N(0, 0.25), 0.5,
1.6)`. Local to this module -- the same "genuinely local to one module's
own data" scope `tuning.py` reserves for a formula with exactly one
caller (see `picks.py::_SWAP_RIGHTS_FACTOR`'s identical reasoning)."""

_SLOTS_PER_ROUND = 30
"""The round-1/round-2 boundary `picks.py` itself treats as a bare literal
throughout (`pick_horizon`'s `slot <= 30`, `rookie_share_y`'s same check) --
named here rather than left as a second unnamed `30` in this module's own
round-offset arithmetic (round 15's B1 fix). A 30-team, 30-slot-per-round
league is a whole-project invariant (`schedule_service.py::_TEAMS_PER_
LEAGUE`), not a Phase-2-local guess, so this is a name for that invariant,
not an independent tunable."""

_TRADE_MATCHING_PCT = 1.25
_TRADE_MATCHING_FLAT = 100_000
"""§9.2/Decision P2-20's over-the-cap matching rule -- the *only*
matching-ratio rule this module enforces (critic round 14 B2 removed the
invented, tighter "100% matching above apron_1" band): `incoming <=
outgoing * 1.25 + $100,000`, active whenever the team is over the cap
before the trade, independent of either apron. `apron_1`/`apron_2` are
enforced separately, as ceilings, in `_check_legality`."""


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# §9.2 -- fit_value, the team-specific wrap around step 3's market_value
# ---------------------------------------------------------------------------


def fit_value(
    roster_player: RosterPlayer,
    ovr: int,
    scouted_peak: ScoutedPeak,
    matrix: NeedMatrix,
    profile: PhilosophyProfile,
    window: WindowParams,
    cap_settings: LeagueCapSettings,
    season: int,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """`market_value(...) * (1 + clamp_need_bonus(need_bonus(...)))` (§5's
    formula, §9.2's own restatement), reusing `market_value` (step 3) and
    `need_bonus`/`clamp_need_bonus` (step 6) rather than re-deriving
    either. `ovr` is an explicit parameter for the same reason `market_
    value`'s own docstring gives: the caller's tilted-or-not talent scalar
    has to be the one `scouted_peak` was scouted against *and* the one
    that selects `need_bonus`'s tier column, and this function has no way
    to verify that consistency on its own."""
    mv = market_value(
        roster_player, ovr, scouted_peak, profile, window, cap_settings, season, tuning=tuning
    )
    bonus = clamp_need_bonus(need_bonus(roster_player, ovr, matrix, profile, tuning=tuning))
    return mv * (1.0 + bonus)


def fit_value_for(
    roster_player: RosterPlayer,
    matrix: NeedMatrix,
    *,
    league_rng_seed: int,
    team_id: int,
    season: int,
    phase: Phase,
    philosophy: GmPhilosophy,
    window: CompetitiveWindow,
    cap_settings: LeagueCapSettings,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """Convenience wrapper over `market_value_for` (step 3) for a caller
    holding a `RosterPlayer`, a `(philosophy, window)` pair, and a
    scouting identity -- the same role `market_value_for` plays over
    `market_value` itself. Reuses `market_value_for` wholesale (never
    re-derives `scout_player`'s call site) and separately derives `ovr =
    effective_talent_ovr(roster_player, profile)` -- a pure, deterministic
    function of `roster_player`/`profile` alone with no scouting draw
    involved -- purely so `need_bonus`'s tier lookup reads the same talent
    scalar `market_value_for` used internally, without either function
    exposing its own intermediate `ovr` to the other.

    `phase` is `chaos`'s seeded-jitter salt (see module docstring) --
    required even for the other six philosophies, where it goes unread,
    the same "always required, only load-bearing under one branch" shape
    `profile.group_tilt` already has for `defense_first`."""
    profile = PROFILES[philosophy]
    mv = market_value_for(
        roster_player,
        league_rng_seed=league_rng_seed,
        team_id=team_id,
        season=season,
        philosophy=philosophy,
        window=window,
        cap_settings=cap_settings,
        tuning=tuning,
    )
    ovr = effective_talent_ovr(roster_player, profile)
    bonus = clamp_need_bonus(need_bonus(roster_player, ovr, matrix, profile, tuning=tuning))
    value = mv * (1.0 + bonus)

    if philosophy is GmPhilosophy.CHAOS:
        rng = rng_for(league_rng_seed, "chaos", team_id, roster_player.id, season, phase)
        jitter = _clamp(1.0 + rng.gauss(0.0, _CHAOS_JITTER_SD), _CHAOS_JITTER_LO, _CHAOS_JITTER_HI)
        value *= jitter

    return value


# ---------------------------------------------------------------------------
# §9.2 -- a pick's fit_value-equivalent contribution (interpretation call 1)
# ---------------------------------------------------------------------------


def _price_pick(
    pick: DraftPick,
    philosophy: GmPhilosophy,
    window: CompetitiveWindow,
    *,
    season: int,
    rookie_scale: RookieScaleTable,
    cap_settings: LeagueCapSettings,
    league_snapshot: LeagueSnapshot | None,
    tuning: GmTuning = DEFAULT_TUNING,
    pick_slot_ranks: Mapping[int, int] | None = None,
) -> float:
    """A `DraftPick`'s own §4.6 valuation, reused wholesale from
    `picks.py` (step 5) -- never a second implementation of any of its
    math. An already-slotted pick (`pick_number` set) reads `pick_value`
    at that real slot directly; an unresolved pick needs `picks.py`'s
    projection chain (`projected_slot_from_window_score` off `original_
    team_id`'s own finish, `expected_slot`'s regression, then `pick_value`
    or `protected_pick_value` depending on `protection_type`), which needs
    a `LeagueSnapshot` -- raises `ValueError` rather than guessing a slot
    if the caller supplied none and this bundle needs one.

    `pick_slot_ranks` (B3 perf fix, critic round 17) -- an optional
    `picks.rank_teams_by_projected_slot(league_snapshot, tuning)` result,
    forwarded verbatim to `projected_slot_from_window_score` so a caller
    pricing many unresolved picks in one pass (`services/gm_service.py::
    run_trade_window`/`ValuationCache`) ranks the league once, not once
    per pick. `None` (the default) reproduces the original from-scratch
    per-call ranking.

    **Round offset (critic round 14 B1, order corrected in round 15's
    fix).** `projected_slot_from_window_score` returns a 1..N rank
    *within* `pick.round` (the team's inverse-standings position among
    the league, not an absolute slot) -- a round-2 pick's real slots are
    31-60, not 1-30, so a round-2 pick's projected rank needs a `+30`
    offset before `pick_value`'s slot machinery sees it. **The offset
    must be applied AFTER `expected_slot`'s regression, not before**:
    `expected_slot` regresses any input toward **15.5**, round 1's own
    mean (§4.6). Offsetting first (round 15's B1 finding) hands
    `expected_slot` an already-shifted value in the 31-60 range and
    regresses it toward round 1's mean instead of round 2's own (45.5),
    which drags every future round-2 pick back across the 30/31 horizon
    cliff -- non-monotone in `years_out` and priced 2.1-2.6x too high
    for `years_out >= 1`. Regressing first and shifting after correctly
    lands the regression around each round's own mean (round 1: 15.5;
    round 2, once shifted: 45.5). Round 1 (`pick.round == 1`) still gets
    a zero offset either way. The already-slotted branch above (real
    `pick_number` set) needs no offset -- a real `pick_number` is
    already an absolute slot, whichever round it came from.

    **Known follow-up, not built this round (nit 4).** `pick.swap_with_
    pick_id`, `pick.conveyed`, and `pick.rolls_over_to_season` are never
    read below -- a protected pick (`protection_type != NONE`) is priced
    through `protected_pick_value` correctly, but a *swap-rights* pick is
    priced here as if it were a plain unresolved pick, with no `picks.py::
    swap_rights_value` wiring. Named here as a real, scoped-out gap
    (critic round 14), not silently dropped."""
    years_out = max(0, pick.season - season)
    if pick.pick_number is not None:
        return pick_value(
            pick.pick_number, years_out, philosophy, window,
            rookie_scale=rookie_scale, cap_settings=cap_settings, tuning=tuning,
        )

    if league_snapshot is None:
        raise ValueError(
            f"pick {pick.id} has no assigned pick_number and no league_snapshot was "
            "supplied to project its expected slot"
        )
    original_team = league_snapshot.team_by_id(pick.original_team_id)
    projected_slot = projected_slot_from_window_score(
        original_team, league_snapshot, tuning=tuning, precomputed_ranks=pick_slot_ranks,
    )
    exp_slot = expected_slot(float(projected_slot), years_out) + (pick.round - 1) * _SLOTS_PER_ROUND

    if pick.protection_type is ProtectionType.NONE:
        return pick_value(
            round(exp_slot), years_out, philosophy, window,
            rookie_scale=rookie_scale, cap_settings=cap_settings, tuning=tuning,
        )
    assert pick.protection_through is not None, (
        "a pick with protection_type != NONE must carry protection_through"
    )
    # `exp_slot` here is already round-offset (round 2's absolute 31-60
    # axis), so `protection_through` on a round-2 pick reads on that same
    # absolute axis -- a "top-N" protection with N <= 30 unconditionally
    # conveys for a round-2 pick (critic round 16, nit 4: swept top-{5,
    # 10,30,40} at years_out 0-2, all priced strictly below pick_value(31,
    # years_out), so this doesn't currently produce an inflated price --
    # but the semantics themselves are a real interpretation call, stated
    # here rather than left implicit).
    return protected_pick_value(
        exp_slot, pick.protection_through, philosophy, window,
        rookie_scale=rookie_scale, cap_settings=cap_settings, years_out=years_out, tuning=tuning,
    )


# ---------------------------------------------------------------------------
# §9.2 -- salary_next, cap_delta_bonus
# ---------------------------------------------------------------------------


def salary_next(roster_player: RosterPlayer, next_season: int) -> int:
    """The player's own `contract_years.salary` for `next_season` -- the
    per-asset input `cap_delta_bonus`'s `salary_delta_share` and every
    legality/matching gate below reads. `0` for a `draft_eligible` player
    (`contract is None`) and for a player whose contract does not cover
    `next_season` at all (already expired, or not yet started) -- neither
    case adds a real dollar to `next_season`'s books, so `0` is the
    correct figure, not a missing one. **Picks have no `salary_next`** --
    a `DraftPick` never carries a current-season salary, so `evaluate_
    trade`'s salary sums below iterate `bundle.players` only, never
    `bundle.picks` (stated here since it is a real reading of the spec,
    not an oversight: `cap_delta_bonus`'s worked example is unit-consistent
    only if the salary side is players-only)."""
    if roster_player.contract is None:
        return 0
    for year in roster_player.contract.years:
        if year.season == next_season:
            return year.salary
    return 0


def _contract_covers_season(roster_player: RosterPlayer, season: int) -> bool:
    """Whether `roster_player` has a contract-year row for `season` at all
    -- the exact basis `capsheet_service.py` counts a team's `roster_count`
    on for a projected season (players under contract *for that season*,
    not today's bodies). `_check_legality`'s filler math needs this same
    basis to match `next_season_room_dollars`'s own provenance (round 15's
    B2 fix) -- checked separately from `salary_next` in case a $0 salary
    year is ever legal, so a coverage check never silently piggybacks on
    a nonzero-salary check.

    **Do not confuse this with `gm_service.py::_occupies_next_season`, and
    do not merge the two (architect round 12).** This is a *financial*
    basis: a contract-less rostered player and an already-lapsed contract
    both fail it (cover no season), even though both physically occupy a
    roster slot. `_occupies_next_season` is the *physical* basis
    `_next_season_roster_count`/free agency/the waiver trigger all need
    instead -- the two genuinely disagree on those two cases, by design,
    and round 12 exists because a divergence of exactly this shape went
    unnoticed once already."""
    return roster_player.contract is not None and any(
        year.season == season for year in roster_player.contract.years
    )


def cap_delta_bonus(
    incoming: AssetBundle,
    outgoing: AssetBundle,
    window: CompetitiveWindow,
    next_season: int,
    next_season_room_dollars: int,
    next_season_cap: int,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """§9.2's formula, verbatim:

    `room_share_next = next_season_room_dollars / next_season_cap`
    `room_hunger = clamp((desired_room - room_share_next) / max(desired_
     room, ROOM_HUNGER_FLOOR), 0, 1)`
    `salary_delta_share = (Sum_O salary_next - Sum_I salary_next) /
     next_season_cap`
    `cap_delta_bonus = room_hunger * salary_delta_share * VP_PER_CAP_
     SEASON * CAP_RELIEF_WEIGHT`

    `next_season_room_dollars` is the team's own **pre-trade** projected
    `room` for `next_season` (`managers.capsheet.SeasonCapProjection.room`,
    step 7 -- a caller-supplied fact about the team's existing cap
    posture, not recomputed here); this trade's own effect on that room is
    what `salary_delta_share` alone carries. `ROOM_HUNGER_FLOOR` guards a
    CONTEND team's `desired_room = 0.00` from a divide-by-zero (§9.2's own
    worked reasoning: the numerator is already negative or zero in that
    case, so the clamp floors it at `0` regardless of the guarded
    denominator's exact value)."""
    params = WINDOW_PARAMS[window]
    room_share_next = next_season_room_dollars / next_season_cap
    hunger_denom = max(params.desired_room, tuning.room_hunger_floor)
    room_hunger = _clamp((params.desired_room - room_share_next) / hunger_denom, 0.0, 1.0)

    outgoing_salary = sum(salary_next(rp, next_season) for rp in outgoing.players)
    incoming_salary = sum(salary_next(rp, next_season) for rp in incoming.players)
    salary_delta_share = (outgoing_salary - incoming_salary) / next_season_cap

    return room_hunger * salary_delta_share * tuning.vp_per_cap_season * tuning.cap_relief_weight


# ---------------------------------------------------------------------------
# Decision P2-19/P2-20 -- the legality gates (a hard block, never a term)
# ---------------------------------------------------------------------------


def _check_legality(
    team_snapshot: TeamSnapshot,
    incoming: AssetBundle,
    outgoing: AssetBundle,
    next_season: int,
    next_season_cap_settings: LeagueCapSettings,
    next_season_room_dollars: int,
) -> str | None:
    """`None` if `team_snapshot` accepting `incoming` for `outgoing` is
    legal; otherwise a stable `"illegal:<rule>"` token naming exactly
    which rule failed -- checked in this order, the first failure wins:
    `roster_max`, `roster_min`, `apron_2`, `apron_1`, `salary_matching`.
    See module docstring (interpretation call 2, revised critic round 14
    B2) for why `apron_1`/`apron_2` are two independent ceiling checks
    rather than a matching-ratio band, and for why every dollar/count
    threshold here reads off `next_season_cap_settings` alone.

    **`used_post`, computed once, correctly, off the right roster-count
    basis (nit 1, provenance corrected in round 15's fix).** `used_pre`
    (below) already carries `capsheet.py`'s pre-trade `minimum_filler`
    baked in -- `used_post` strips that component back out and adds the
    post-trade one, rather than adding a *second*, post-trade filler
    charge on top (critic round 14's original finding). **The filler
    math must use the same roster-count basis `capsheet_service.py` used
    to compute `next_season_room_dollars`: players with a contract-year
    row actually covering `next_season`, not `len(team_snapshot.roster)`
    (today's bodies)** -- round 15's B2 finding: those two counts can
    differ (a player on today's roster whose deal doesn't reach
    `next_season` contributes to `len(roster)` but not to capsheet's own
    count), and the earlier fix silently assumed they were the same,
    producing a phantom filler charge on any team carrying an
    about-to-expire contract. `roster_max`/`roster_min` themselves stay
    on `len(team_snapshot.roster)` deliberately -- those gates are about
    the real, physical roster size the moment the trade closes, not a
    hypothetical future season's contract coverage; only the *financial*
    filler math needs to match capsheet's projection basis.

    **`next_season_room_dollars`'s `include_holds` provenance (critic
    round 16, nit 3).** Whether the caller's room figure came from a
    `capsheet_service.project_league_cap_sheets(..., include_holds=True)`
    or `False` (the default) pass is not tracked here and shifts `used_
    pre`/`used_post` by the whole cap-holds total against the apron
    ceilings -- netting itself is unaffected (holds pass through both
    sides identically), only the ceiling *comparison*. Callers (`services/
    gm_service.py`, step 10) should pass a `False`-projected room
    consistently for legality purposes; stated here rather than silently
    assumed."""
    settings = next_season_cap_settings
    pre_roster_count = len(team_snapshot.roster)
    post_roster_count = pre_roster_count - len(outgoing.players) + len(incoming.players)
    if post_roster_count > settings.roster_max:
        return "illegal:roster_max"

    outgoing_salary = sum(salary_next(rp, next_season) for rp in outgoing.players)
    incoming_salary = sum(salary_next(rp, next_season) for rp in incoming.players)
    used_pre = settings.salary_cap - next_season_room_dollars

    pre_contracted = sum(
        1 for rp in team_snapshot.roster if _contract_covers_season(rp, next_season)
    )
    outgoing_contracted = sum(
        1 for rp in outgoing.players if _contract_covers_season(rp, next_season)
    )
    incoming_contracted = sum(
        1 for rp in incoming.players if _contract_covers_season(rp, next_season)
    )
    post_contracted = pre_contracted - outgoing_contracted + incoming_contracted

    pre_filler = max(0, settings.roster_min - pre_contracted) * settings.min_salary
    post_filler = max(0, settings.roster_min - post_contracted) * settings.min_salary
    used_post = used_pre - pre_filler + post_filler - outgoing_salary + incoming_salary

    if post_roster_count < settings.roster_min:
        # "below roster_min with no minimum-salary path" -- legal iff the
        # team could still fill out to roster_min at min_salary without
        # itself breaching apron_2 (the same hard ceiling the next check
        # below applies to the trade's own salaries). `used_post` above
        # already includes that post-trade filler charge, so no second
        # `filler_cost` is added here (nit 1).
        if used_post > settings.apron_2:
            return "illegal:roster_min"

    if used_post > settings.apron_2:
        return "illegal:apron_2"
    if used_post > settings.apron_1:
        return "illegal:apron_1"

    if used_pre > settings.salary_cap:  # over the cap -- the one matching rule applies
        if incoming_salary > outgoing_salary * _TRADE_MATCHING_PCT + _TRADE_MATCHING_FLAT:
            return "illegal:salary_matching"

    return None


# ---------------------------------------------------------------------------
# §9.2 -- package evaluation and the acceptance threshold
# ---------------------------------------------------------------------------


def _bundle_subject(incoming: AssetBundle, outgoing: AssetBundle) -> str:
    """A stable, structured identifier for `DecisionRecord.subject` --
    never a sentence (Decision P2-18, `explain.py`'s own rule)."""
    in_ids = sorted(f"p{rp.id}" for rp in incoming.players) + sorted(
        f"k{pick.id}" for pick in incoming.picks
    )
    out_ids = sorted(f"p{rp.id}" for rp in outgoing.players) + sorted(
        f"k{pick.id}" for pick in outgoing.picks
    )
    return f"in:[{','.join(in_ids)}]|out:[{','.join(out_ids)}]"


def evaluate_trade(
    team_snapshot: TeamSnapshot,
    incoming: AssetBundle,
    outgoing: AssetBundle,
    philosophy: GmPhilosophy,
    window: CompetitiveWindow,
    *,
    league_rng_seed: int,
    season: int,
    phase: Phase,
    cap_settings: LeagueCapSettings,
    next_season: int,
    next_season_cap_settings: LeagueCapSettings,
    next_season_room_dollars: int,
    rookie_scale: RookieScaleTable,
    league_snapshot: LeagueSnapshot | None = None,
    tuning: GmTuning = DEFAULT_TUNING,
    pick_slot_ranks: Mapping[int, int] | None = None,
) -> DecisionRecord:
    """One team's (`team_snapshot`'s) full §9.1/§9.2 evaluation of
    receiving `incoming` in exchange for sending away `outgoing` -- a
    single `DecisionRecord`, never a two-sided "trade" wrapper type (a
    caller evaluating whether *both* sides would accept calls this twice,
    once per team, with `incoming`/`outgoing`/`team_snapshot` swapped;
    §12 step 8's own verify list requires the *return type itself* to
    carry no counter-package field, which reusing `DecisionRecord` as-is,
    rather than inventing a new wrapper, satisfies structurally).

    Order of operations, matching Decision P2-19's own "a hard gate,
    evaluated before value" text: legality first (`_check_legality`) --
    an illegal trade returns immediately with `score=threshold=0.0` and
    `terms=()` (which trivially reconstructs) and a `verdict` naming the
    failed rule, never a value computation. A legal trade then prices
    every asset (`fit_value_for` for a `RosterPlayer`, `_price_pick` for a
    `DraftPick`) plus one `cap_delta_bonus` term, sums to `net_A`, and
    accepts iff `net_A >= threshold_A = max(MIN_ABS_GAIN, MIN_REL_GAIN *
    Sum_O fit_value_A)` (§9.2). **`Sum_O fit_value_A` in the threshold is
    computed as the sum of every `out:` term's `input_value`, which
    includes outgoing picks' `pick_value` alongside outgoing players'
    `fit_value`** (critic round 15 nit 7) -- consistent with interpretation
    call 1's reading that a pick's `fit_value`-equivalent contribution to
    `net_A` itself is `pick_value`, so the same figure belongs in the
    threshold that gates `net_A`, not a narrower player-only sum.

    **`loyalty` applies to outgoing players only, never to outgoing picks
    (critic round 14 nit 3).** §6 (line 621) is explicit: "`loyalty`
    multiplies the `fit_value` of *outgoing* players only" -- the more
    specific, more deliberate statement of `loyalty`'s scope than §9.2's
    generic `Sum_O` notation, and the reading consistent with
    interpretation call 1 above (a pick has no `fit_value` to begin with,
    only `pick_value`, so a player-endowment-effect multiplier has nothing
    of its own kind to apply to there). An outgoing pick's contribution to
    `net_A` below is `-pick_value(...)`, unweighted.

    Raises `ValueError` if `outgoing.players` contains a player not on
    `team_snapshot.roster` (critic round 14 nit 8) -- a caller-contract
    violation this function can and does check itself, the same "raise
    rather than silently produce a nonsensical `post_roster_count`"
    convention `picks.py::_price_pick` uses for a missing
    `league_snapshot`.

    `pick_slot_ranks` (B3 perf fix, critic round 17) -- forwarded verbatim
    to every `_price_pick` call below; see `_price_pick`'s own docstring."""
    outgoing_player_ids = {rp.id for rp in outgoing.players}
    roster_ids = {rp.id for rp in team_snapshot.roster}
    missing = outgoing_player_ids - roster_ids
    if missing:
        raise ValueError(
            f"outgoing bundle contains player id(s) {sorted(missing)} not on team "
            f"{team_snapshot.id}'s roster"
        )

    illegal = _check_legality(
        team_snapshot, incoming, outgoing, next_season, next_season_cap_settings,
        next_season_room_dollars,
    )
    if illegal is not None:
        return DecisionRecord(
            team_id=team_snapshot.id, philosophy=philosophy, window=window,
            subject=_bundle_subject(incoming, outgoing), verdict=illegal,
            score=0.0, threshold=0.0, terms=(),
        )

    profile = PROFILES[philosophy]
    matrix = build_need_matrix(team_snapshot, tuning=tuning)

    terms: list[ReasonTerm] = []
    for rp in incoming.players:
        fv = fit_value_for(
            rp, matrix, league_rng_seed=league_rng_seed, team_id=team_snapshot.id, season=season,
            phase=phase, philosophy=philosophy, window=window, cap_settings=cap_settings,
            tuning=tuning,
        )
        terms.append(
            ReasonTerm(label=f"in:player:{rp.id}", input_value=fv, weight=1.0, contribution=fv)
        )
    for pick in incoming.picks:
        pv = _price_pick(
            pick, philosophy, window, season=season, rookie_scale=rookie_scale,
            cap_settings=cap_settings, league_snapshot=league_snapshot, tuning=tuning,
            pick_slot_ranks=pick_slot_ranks,
        )
        terms.append(
            ReasonTerm(label=f"in:pick:{pick.id}", input_value=pv, weight=1.0, contribution=pv)
        )

    for rp in outgoing.players:
        fv = fit_value_for(
            rp, matrix, league_rng_seed=league_rng_seed, team_id=team_snapshot.id, season=season,
            phase=phase, philosophy=philosophy, window=window, cap_settings=cap_settings,
            tuning=tuning,
        )
        terms.append(
            ReasonTerm(
                label=f"out:player:{rp.id}", input_value=fv, weight=-profile.loyalty,
                contribution=-profile.loyalty * fv,
            )
        )
    for pick in outgoing.picks:
        pv = _price_pick(
            pick, philosophy, window, season=season, rookie_scale=rookie_scale,
            cap_settings=cap_settings, league_snapshot=league_snapshot, tuning=tuning,
            pick_slot_ranks=pick_slot_ranks,
        )
        terms.append(
            ReasonTerm(label=f"out:pick:{pick.id}", input_value=pv, weight=-1.0, contribution=-pv)
        )

    bonus = cap_delta_bonus(
        incoming, outgoing, window, next_season, next_season_room_dollars,
        next_season_cap_settings.salary_cap, tuning=tuning,
    )
    terms.append(
        ReasonTerm(label="cap_delta_bonus", input_value=bonus, weight=1.0, contribution=bonus)
    )

    score = sum(term.contribution for term in terms)
    outgoing_fit_sum = sum(term.input_value for term in terms if term.label.startswith("out:"))
    threshold = max(tuning.min_abs_gain, tuning.min_rel_gain * outgoing_fit_sum)
    verdict = "accept" if score >= threshold else "reject"

    return DecisionRecord(
        team_id=team_snapshot.id, philosophy=philosophy, window=window,
        subject=_bundle_subject(incoming, outgoing), verdict=verdict,
        score=score, threshold=threshold, terms=tuple(terms),
    )
