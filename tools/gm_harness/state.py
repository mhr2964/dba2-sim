"""`HarnessState` -- the mutable, single-source-of-truth in-memory league
(architect round-7 report §5.1) -- plus its two rebuilders (`build_
snapshot`, `build_ctx`) and the five in-memory appliers that mirror
`services/gm_service.py`'s real, Postgres-backed ones one-for-one (§5.2,
plus `apply_waiver_inmem`, T7a2's own later addition).

**Deliberately not frozen.** `LeagueSnapshot`/`TeamSnapshot`/`RosterPlayer`
stay frozen (`managers/snapshot.py`'s own convention) and get *rebuilt*
from this state at each phase boundary, never mutated in place -- the same
split `gm_service.py` already draws between its T3 pure-plan layer (reads
a snapshot) and its T4 I/O-apply layer (mutates the database, which the
next `load_gm_inputs` call re-reads into a fresh snapshot). The five
appliers below are this module's T4-equivalent: they mutate `HarnessState`
in place, and a caller rebuilds a fresh snapshot/ctx afterward via `build_
snapshot`/`build_ctx`, exactly as `run_phase_hook` re-derives its next
snapshot from the database after every applier call.

**`build_ctx`'s cap-sheet aggregation is the one genuinely new piece of
math here** -- everything else in this module is a direct, mechanical
transcription of an existing DB-backed function's own shape. See `_
aggregate_capsheet_inputs`'s own docstring for the exact basis (a direct
transcription of `capsheet_service.py:105-121`, plus T6a's `_next_season_
roster_count` correction generalized across the whole projection horizon,
not just the single next-season point T6a itself needed).

Zero Postgres I/O -- this module only ever reads/writes plain Python
objects already held on `HarnessState`. It does, transitively, import
`dba2.services.gm_service` (for `GmContext`/`TradeExecution`/
`SigningExecution`/`WindowPassResult`, reused rather than redefined) --
that module itself imports `asyncpg` at load time (its own docstring's
Nit 8), which is fine here: `tools/` carries none of `managers/`'s zero-
I/O purity rules, it just never *calls* any of `gm_service.py`'s `async
def` functions.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime

from dba2.agency.motivation import trade_request_pressure
from dba2.domain.contract import (
    Contract,
    ContractStatus,
    ContractType,
    ContractYear,
    OptionDecision,
    OptionType,
    SigningMethod,
)
from dba2.domain.draft import DraftPick
from dba2.domain.head_to_head import HeadToHead
from dba2.domain.league import LeagueCapSettings
from dba2.domain.motivation import PlayerMotivation
from dba2.domain.phase import Phase
from dba2.domain.player import RosterStatus
from dba2.domain.team import Team
from dba2.managers import capsheet
from dba2.managers.capsheet import CapSheetTeamSeasonInputs
from dba2.managers.decisions import draft as draft_decisions
from dba2.managers.decisions.signing import bird_max_pct_tier
from dba2.managers.picks import RookieScaleTable
from dba2.managers.snapshot import (
    LeagueSnapshot,
    PlayerContract,
    RosterPlayer,
    build_league_snapshot,
    build_team_snapshot,
)
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow, GmTuning
from dba2.services.gm_service import (
    GmContext,
    SigningExecution,
    TradeExecution,
    WaiverExecution,
    WindowPassResult,
    compute_h2h_base_accept_rate,
)
from tools._gm_progression_stub import _RETIREMENT_AGE


def _dummy_created_at(season: int) -> datetime:
    """A deterministic, season-anchored stand-in for `created_at` --
    `datetime.now()` is forbidden (no real-clock read may enter a
    reproducible generation), mirroring `tools/validate_sim.py`'s/`tools/
    _gm_draft_class_stub.py`'s own `datetime(season, ..., tzinfo=UTC)`
    convention for every generated timestamp field. Lives here, not in
    `league.py`, so both modules can import it without a circular
    `league.py` <-> `state.py` dependency (`league.py` already imports
    `HarnessState` from this module)."""
    return datetime(season, 1, 1, tzinfo=UTC)


@dataclass
class HarnessState:
    """Mutable, single source of truth (report §5.1's exact field list).
    `players` includes every status a real league carries (`FREE_AGENT`/
    `RETIRED` included, though this harness never produces the latter) --
    `team_id`/`roster_status` live on the nested, still-mutable `player.
    player.team_id`/`.roster_status`, per `RosterPlayer`'s own frozen-
    wrapper-over-mutable-`Player` shape (`managers/snapshot.py`)."""

    teams: dict[int, Team]
    players: dict[int, RosterPlayer]
    prospects: dict[int, RosterPlayer]
    picks: dict[int, DraftPick]
    cap_settings_by_season: dict[int, LeagueCapSettings]
    rookie_scale: RookieScaleTable
    window_by_team: dict[int, CompetitiveWindow]
    league_id: int
    season: int
    rng_seed: int
    next_contract_id: int
    next_pick_id: int
    next_player_id: int
    dead_cap_by_team_season: dict[tuple[int, int], int] = field(default_factory=dict)
    """`(team_id, season) -> total dead-cap dollars charged`, populated by
    `apply_waiver_inmem` (T7a2, architect round 8 §7.4) -- the in-memory
    counterpart to a real `dead_cap_charges` table, summed rather than
    kept as individual rows since `_aggregate_capsheet_inputs` (below)
    only ever needs the per-`(team, season)` total, never a per-charge
    breakdown. Defaults to an empty dict (`field(default_factory=dict)`,
    not a bare `{}` default -- the standard mutable-default-argument trap)
    so `generate_gm_league`'s own existing `HarnessState(...)` call site
    (`tools/gm_harness/league.py`, deliberately not touched this slice --
    see that module's own docstring, F8) needs no edit to keep
    constructing a valid state."""
    head_to_head: dict[tuple[int, int], HeadToHead] = field(default_factory=dict)
    """`(team_id, opponent_id) -> HeadToHead` -- Decision P3-24's harness
    counterpart to `GmContext.head_to_head`, mirroring `dead_cap_by_team_
    season`'s own `field(default_factory=dict)` construction exactly (§11
    step 11). Populated by merging each `TradeWindowResult.head_to_head`
    (§11 step 11's write path) into this dict after every trade window
    (`tools/gm_harness/season.py`), and rewritten wholesale by `memory.
    decay_head_to_head_rows` at every season boundary (Decision P3-25).
    Starts empty for a freshly generated league -- `generate_gm_league`'s
    own `HarnessState(...)` call site needs no edit, same as `dead_cap_by_
    team_season` above."""
    motivation: dict[int, PlayerMotivation] = field(default_factory=dict)
    """`player_id -> PlayerMotivation` -- Phase 4 Decision P4-13's harness
    mirror of the real, Postgres-backed `player_motivation` table, the
    identical shape `head_to_head` above already establishes for a
    different table. Populated by `tools/gm_harness/season.py`'s
    bootstrap call (Decision P4-6's trait generator, run once per player
    at league generation) and rewritten at each of Decision P4-11's two
    checkpoints a season (trade deadline / offseason). Starts empty for a
    freshly generated league, same as `head_to_head`."""
    motivation_dwell: dict[int, int] = field(default_factory=dict)
    """`player_id -> last_transition_season` -- the harness's own mirror
    of what the real `player_motivation_events` table's `ladder_
    transition` rows exist to answer (Decision P4-56): the season a
    player's `RequestState` last moved. The harness keeps no event log at
    all (Decision P4-10/P4-44's audit trail is a real-DB-path concern),
    so this dict IS the harness's dwell bookkeeping directly, rather than
    a synthetic event history nothing else reads. **A player absent from
    this dict has never transitioned -- `None`, never `0` (Decision
    P4-71's sentinel rule)**; `tools/gm_harness/season.py`'s checkpoint
    reads a missing key as `None` via `.get(player_id)`, never defaults
    it to `0`."""
    transition_count_by_player: dict[int, int] = field(default_factory=dict)
    """`player_id -> cumulative RequestState-ladder transition count`, for
    the WHOLE run so far -- Decision P4-35's agency churn-bound metric's
    real input ("a player may not oscillate across the ladder more than N
    times... the direct measurement of whether hysteresis works").
    `motivation_dwell` above answers "when did this player last
    transition"; it cannot answer "how many times has this player
    transitioned, total" -- a broken hysteresis/dwell implementation and
    a working one can share the same `motivation_dwell` reading at any
    single instant but differ sharply in this count over a multi-season
    run. A player absent from this dict has transitioned zero times --
    unlike `motivation_dwell`'s `None`-sentinel rule (P4-71), a real,
    meaningful zero is the correct absent-reading here, matching `tenure_
    seasons_by_player`'s own "absence is a real zero" convention below,
    not `motivation_dwell`'s "absence is no-signal" one."""
    tenure_seasons_by_player: dict[int, int] = field(default_factory=dict)
    """`player_id -> full seasons completed on the player's CURRENT team`
    -- Decision P4-63: the harness stores no acquisition season anywhere
    else (`RosterPlayer` carries no join date, no prior-team list), so
    `PlayerSituation.tenure_seasons` would stay permanently mute in the
    one corpus that actually gates Phase 4's agency metrics without this
    field. Reset to `0` the moment a player joins a new team (trade,
    signing, draft, waiver claim -- every channel `tools/gm_harness/
    season.py`'s acquisition tracking already observes) and incremented
    by `1` for every player still on the same team at the season
    boundary. A player absent from this dict is a fresh-enough case that
    `PlayerSituation.tenure_seasons` reads `0.0`, not `None` -- unlike
    `motivation_dwell`, absence here is a real, meaningful zero, not
    "no signal"."""


# ---------------------------------------------------------------------------
# Rebuilders
# ---------------------------------------------------------------------------


def build_snapshot(state: HarnessState) -> LeagueSnapshot:
    """One `TeamSnapshot` per team -- roster = every `ACTIVE` player with
    `team_id == team.id`, sorted by id; `draft_picks` = every pick with
    `current_team_id == team.id`, sorted by id -- matching `load_gm_
    inputs`'s own roster definition verbatim, including its `TWO_WAY`/
    `INJURED` exclusion (`game_sim_service.py`'s documented "a team's
    playable roster right now" call, reused there and here)."""
    cap_settings = state.cap_settings_by_season[state.season]
    team_snapshots = []
    for team in sorted(state.teams.values(), key=lambda t: t.id):
        roster = tuple(
            sorted(
                (
                    rp
                    for rp in state.players.values()
                    if rp.player.team_id == team.id
                    and rp.player.roster_status is RosterStatus.ACTIVE
                ),
                key=lambda rp: rp.id,
            )
        )
        team_picks = tuple(
            sorted(
                (p for p in state.picks.values() if p.current_team_id == team.id),
                key=lambda p: p.id,
            )
        )
        team_snapshots.append(
            build_team_snapshot(
                team, roster, team_picks, cap_settings, window=state.window_by_team[team.id]
            )
        )
    return build_league_snapshot(state.league_id, state.season, tuple(team_snapshots))


_CAP_HOLD_MULT: float = 0.354874
"""Decision P2-36's cap-hold multiplier (architect round 19 §4.3 Formula A,
`docs/ledger/reports/phase-2-architect-round-19.md`) -- a **harness-only
stand-in for a production mechanism that does not exist anywhere in
`dba2/`**. Grep-confirmed (round 19 §4.1): no cap-hold formula, stub, or
partial implementation exists in this codebase or the design doc -- the
`cap_holds` table is schema-only (migration 003, Phase 0 Decision 19), and
no mechanism that would write a `CapHold` row is scoped anywhere in
Phase 2 (progression/draft classes are Phase 7's job, §13). This constant
and the formula it feeds are therefore a measurement device this harness
needs *now* to instrument M10/M2 honestly, standing in for whatever real
cap-hold generation eventually ships -- the same standing as `tools/_gm_
progression_stub.py` under Decision P2-23. See round 19 §4.1 for the full
"nothing to inherit" finding.

**Why the real NBA's 1.9-2.5x prior-salary multiplier is NOT used here
(round 19 §4.2).** Those multipliers are large *because renunciation
exists*: a team that wants the cap room a hold occupies renounces it and
loses Bird rights to that player, in exchange for the room back. This
harness has **no renounce decision anywhere** -- no code path drops a
hold once assigned. Transplanting CBA-scale multipliers without a
renunciation mechanism would put mean team holds several times above
M10's own `0.15 * cap` buyer threshold, driving M10's buyer count *through*
its band and out the bottom (`band_low: 3`) -- converting a fail-high into
a fail-low, the opposite failure mode, not a fix.

**Value derivation, an initial estimate for the pending decision point to
react to, not a final tuned value (round 19 §7's own explicit framing).**
Orchestrator round 29's M19-A measurement (`docs/ledger/phase-2.md`,
`artifacts/scratch_m19a_cap_hold_feasibility.py`) found `H_min =
$4,000,000` -- the smallest per-expiring-player hold that brings M10's
mean buyer count down to its own ceiling of 12, using a roster-count-based
proxy for the expiring population (that measurement had no per-player
salaries on hand).

**Recalibrated against the NET, post-`filler_credit` average (backend-dev
round 55, critic round 52 B1 -- the first calibration's own mistake).** The
round-54 value above (`0.282960`) was solved from `H_min / mean_gross_
expiring_salary` -- i.e. against each player's raw, pre-netting hold. That
is the wrong quantity: `H_min` is orchestrator round 29's own target for
the dollar figure that actually lands in `used`/`room` for a hold-carrying
team, which is `cap_holds` *after* `filler_credit` nets out (round 19
§4.4's own "no double-charge" formula, both above). `filler_credit`
subtracts a real, systematic share of the gross total (it isn't team-
specific noise -- every team with any roster slack under `roster_min`
nets some of its holds against it), so a multiplier solved against the
gross figure alone reliably overshoots the net target. Measured fresh
this round (`artifacts/scratch_round52_net_hold_mult.py`, gitignored;
same 3-seeds-x-3-seasons-each/`_expiring_by_team`-per-boundary
methodology as round 54's own script, extended to also capture each
team's `season + 1` `roster_count` and run the *exact*
`_cap_holds_for_team` formula -- clamp, floor, and `filler_credit`
included -- offline per candidate multiplier, against the same 1,263
expiring-player population round 54 measured, using this same round's N2-
fixed `hold_season_settings` basis, `capsheet.project_cap_settings(state.
cap_settings_by_season[state.season], state.season + 1, tuning)`, not the
stale dict lookup N2 removes): bisecting on the net average across every
(seed, season, team) cell finds `_CAP_HOLD_MULT = 0.354874` lands the net
average at **$4,000,000.13** -- 1.00003x `H_min`, i.e. as close to exact
as this measurement's own rounding noise allows. For comparison, the
round-54 gross-basis value (`0.282960`) produces a net average of only
$3,020,527 (0.76x `H_min`) under this same, corrected `hold_season_
settings` basis -- confirming the original calibration's target quantity,
not just its exact number, was wrong. `_CAP_HOLD_MULT * (a team's average
expiring salary), after the `min_salary` floor / `max_salary` ceiling
clamp and the `filler_credit` netting (both below) are applied, ~= H_min`
on average -- **this** is the quantity the constant is now sized against,
not the pre-netting gross hold. **This is disclosed, per round 19 §8 R1,
as a tuned quantity sized to close M10, not as CBA realism** -- the
decision point after this slice reads the real `hold_inclusive_mean` this
slice now produces off an actual gate run and may ask for a different
multiplier; nothing here is final.

**Deliberately lives here, in `tools/gm_harness/`, and not in `dba2/
managers/tuning.py` (round 19 §6, confirmed by round 18 and round 19
both).** `tuning.py`'s own `GM_TUNING_VERSION` bump rule is scoped to "any
change to a constant or weight table anywhere under `managers/`" -- this
is a harness measurement constant synthesizing what a not-yet-built
production `cap_holds` table would hold, not a `managers/` decision
weight, so it does not bump `GM_TUNING_VERSION`."""


def _aggregate_capsheet_inputs(
    state: HarnessState, tuning: GmTuning
) -> dict[int, tuple[CapSheetTeamSeasonInputs, ...]]:
    """The in-memory equivalent of `capsheet_service.py:105-121`'s bulk
    aggregation, transcribed from `PlayerContract.years` instead of three
    `contract_repo` bulk loaders. **`dead_cap` is no longer always
    `0` (T7a2, architect round 8 §7.4/§9)** -- it now reads `state.dead_
    cap_by_team_season`, populated by `apply_waiver_inmem` whenever a cut
    executes; metric 9's prior "structurally vacuous, nothing in this
    harness ever produces a `dead_cap_charges` row" finding is closed by
    this same slice, not just documented as a future gap.

    **`cap_holds` (architect round 19 §4.3-§4.5, Slice 19-A) -- nonzero
    only at the `season + 1` row, `0` at every other horizon row.** A hold
    for a player expiring at the end of season `S` exists only at `S + 1`
    (`S + 2` on, he is either signed -- and inside `committed_salary` --
    or gone); round 19 §4.5 confirms every current consumer of this
    aggregation only ever reads the `season + 1` row anyway, so populating
    only that one row is both domain-correct and costs no other row
    anything. Formula A, exactly (round 19 §4.3): for each player `p`
    whose contract's own last `contract_years` row is `state.season` (the
    identical predicate `gm_service.py::_expiring_by_team` uses --
    reproduced here directly against `state.players`, not via that
    function, per round 19 §7 item 2's own instruction, since this
    aggregation already iterates `state.players` and has every contract
    year in hand),

        hold(p) = clamp(_CAP_HOLD_MULT * p's own final contracted-year
                         salary, min_salary(S+1), max_salary(p, S+1))

    where `max_salary(p, S+1) = bird_max_pct_tier(p.years_pro, cap_
    settings(S+1)) / 100 * salary_cap(S+1)` -- the identical years-of-
    service tier `dba2.managers.decisions.signing.bird_max_pct_tier`
    already applies to every other incumbent-ceiling calculation in this
    codebase (reused directly, not re-derived a third time alongside that
    module's own and `tools/gm_harness/league.py::_max_pct_tier`'s
    existing duplicate). Team total, **netted against the minimum-roster
    charge those same empty slots already generate in `used` via
    `capsheet.py`'s own `minimum_filler` term** -- omitting this double-
    charges every hold-carrying slot by up to `min_salary(S+1)` each
    (round 19 §4.4, a new finding this slice's own tests pin directly):

        cap_holds(team, S+1) = max(0, sum_p hold(p) - filler_credit)
        filler_credit = min(n_holds, max(0, roster_min(S+1) -
                             roster_count(S+1))) * min_salary(S+1)

    `roster_count(S+1)` is the same value this function already computes
    for that row (below) -- `filler_credit` is applied at that exact
    point in the per-season loop, not recomputed.

    **`roster_count`'s own correction, generalized across the whole
    horizon (not just the single next-season point T6a's own fix
    needed).** The real DB aggregation counts "distinct players with an
    active contract-year covering season S" -- which under-counts a
    contract-less rostered player for every season, the exact T6a bug
    (`gm_service.py::_next_season_roster_count`'s own docstring) reproduced
    here one level up if left uncorrected. Fixed the same way: a rostered
    (`team_id` set, `ACTIVE`) player with `contract is None` counts toward
    `roster_count` at *every* season in the horizon (nothing under `dba2/`
    ever evicts a contract-less rostered player -- there is no fact that
    would make them stop occupying a slot at some particular future
    season), while a contracted player counts only for the seasons their
    own contract years actually cover, unchanged from the real path."""
    horizon_seasons = tuple(state.season + i for i in range(tuning.horizon))
    horizon_set = set(horizon_seasons)
    hold_season = state.season + 1
    committed: dict[tuple[int, int], int] = {}
    team_option: dict[tuple[int, int], int] = {}
    player_option: dict[tuple[int, int], int] = {}
    contract_covered_players: dict[tuple[int, int], set[int]] = {}
    contractless_rostered_count: dict[int, int] = {}
    expiring_by_team: dict[int, list[tuple[int, int]]] = {}
    """`team_id -> [(final_year_salary, years_pro), ...]` for players whose
    contract's own last `contract_years` row is `state.season` -- the hold
    population, `_expiring_by_team`'s own predicate (`gm_service.py`)."""

    for rp in state.players.values():
        if rp.contract is not None:
            team_id = rp.contract.contract.team_id
            last_season = max(year.season for year in rp.contract.years)
            if last_season == state.season:
                final_salary = next(
                    year.salary for year in rp.contract.years if year.season == state.season
                )
                expiring_by_team.setdefault(team_id, []).append(
                    (final_salary, rp.player.years_pro)
                )
            for year in rp.contract.years:
                if year.season not in horizon_set:
                    continue
                key = (team_id, year.season)
                committed[key] = committed.get(key, 0) + year.salary
                contract_covered_players.setdefault(key, set()).add(rp.id)
                if year.option_type is OptionType.TEAM:
                    team_option[key] = team_option.get(key, 0) + year.salary
                elif year.option_type is OptionType.PLAYER:
                    player_option[key] = player_option.get(key, 0) + year.salary
        elif rp.player.team_id is not None and rp.player.roster_status is RosterStatus.ACTIVE:
            contractless_rostered_count[rp.player.team_id] = (
                contractless_rostered_count.get(rp.player.team_id, 0) + 1
            )

    # `hold_season`'s cap settings (min_salary/roster_min/salary_cap/the
    # max_pct_* tiers) -- **always projected forward from `state.season`'s
    # own row (round 52 N2 fix), never read back out of `state.cap_
    # settings_by_season[hold_season]`.** That dict entry, when present (a
    # real running league populates it via `_ensure_cap_settings`, which
    # projects from the run's one true `first_season` anchor,
    # `round(base * growth ** (hold_season - first_season))`, a *single*
    # rounding), is a genuinely different figure from what `project_team_
    # cap_sheet`'s own `used`/`room`/`minimum_filler` computation for this
    # exact `hold_season` row derives downstream (`build_ctx`'s/`_capsheet_
    # by_team`'s own `real_anchor = {state.season: cap_settings}`, which
    # always re-projects `hold_season` from `state.season`'s *already-
    # rounded* row -- `round(round(base * growth ** k) * growth)`, a
    # *second* rounding on top of the first). The two can differ by $1 in
    # some seasons purely from that double-rounding, which used to leak
    # into `filler_credit`'s own `min_salary(S+1)` (critic round 52 N1's
    # finding -- confirmed by hand this round: reverting this fix and
    # diffing `filler_credit` against a team whose `state.season` cap
    # settings were themselves already a projected, non-integer-growth-
    # factor row reproduces a $1 divergence). Deriving it here the same
    # way `project_team_cap_sheet` does -- straight from `state.season`'s
    # own row -- makes this the one source of truth for `hold_season`'s
    # cap settings within this whole computation (clamps *and*
    # `filler_credit` alike), matching exactly what `used`/`room` actually
    # charge downstream.
    hold_season_settings = capsheet.project_cap_settings(
        state.cap_settings_by_season[state.season], hold_season, tuning=tuning
    )

    def _cap_holds_for_team(team_id: int, roster_count_at_hold_season: int) -> int:
        entries = expiring_by_team.get(team_id)
        if not entries:
            return 0
        total_hold = 0
        for final_salary, years_pro in entries:
            max_pct = bird_max_pct_tier(years_pro, hold_season_settings)
            max_salary = max_pct / 100.0 * hold_season_settings.salary_cap
            raw_hold = _CAP_HOLD_MULT * final_salary
            total_hold += round(min(max(raw_hold, hold_season_settings.min_salary), max_salary))
        n_holds = len(entries)
        empty_slots = max(0, hold_season_settings.roster_min - roster_count_at_hold_season)
        filler_credit = min(n_holds, empty_slots) * hold_season_settings.min_salary
        # `max(0, ...)` is a defensive floor, not a reachable branch: the
        # `min_salary` floor above already guarantees `total_hold >=
        # n_holds * min_salary >= min(n_holds, empty_slots) * min_salary
        # == filler_credit`, so `total_hold - filler_credit` cannot go
        # negative (critic round 52 N3, Mutation E -- provably dead,
        # deliberately not chased for test coverage: any test asserting
        # this floor binds would require `total_hold < filler_credit`,
        # which the floor above makes unreachable by construction).
        return max(0, total_hold - filler_credit)

    result: dict[int, tuple[CapSheetTeamSeasonInputs, ...]] = {}
    for team_id in state.teams:
        rows = []
        for season in horizon_seasons:
            key = (team_id, season)
            contractless = contractless_rostered_count.get(team_id, 0)
            roster_count = len(contract_covered_players.get(key, ())) + contractless
            cap_holds = (
                _cap_holds_for_team(team_id, roster_count) if season == hold_season else 0
            )
            rows.append(
                CapSheetTeamSeasonInputs(
                    team_id=team_id,
                    season=season,
                    committed_salary=committed.get(key, 0),
                    dead_cap=state.dead_cap_by_team_season.get(key, 0),
                    cap_holds=cap_holds,
                    team_option_salary=team_option.get(key, 0),
                    player_option_salary=player_option.get(key, 0),
                    roster_count=roster_count,
                )
            )
        result[team_id] = tuple(rows)
    return result


def build_ctx(
    state: HarnessState,
    *,
    phase: Phase,
    tuning: GmTuning = DEFAULT_TUNING,
    head_to_head_enabled: bool = True,
    agency_enabled: bool = True,
    media_enabled: bool = True,
) -> GmContext:
    """The in-memory equivalent of `load_gm_inputs`'s `GmContext` half --
    `room_by_team` is the interesting piece (`_aggregate_capsheet_inputs`
    above plus the real, pure `capsheet.project_team_cap_sheet`, `room` at
    `season + 1`, `include_holds=False` -- `GmContext.room_by_team`'s
    documented caller obligation). `apron1_room_by_team` (slice 12-A,
    round 11 §5.3) is the identical `SeasonCapProjection` row's
    `.apron1_room`, computed alongside `room_by_team` in the same loop.

    **Decision P2-37.** `retirement_age` is populated from this harness's
    own `tools/_gm_progression_stub.py::_RETIREMENT_AGE` -- a `tools/`
    module importing another `tools/` module, which `tests/unit/test_
    layering.py`'s quarantine (only `dba2/` -> `tools/` is forbidden) does
    not touch. This is a plain `int` value crossing into `GmContext`, never
    the harness's progression *logic* itself.

    **Decision P2-39 (architect round 21 §4.2).** `room_for_signings_by_
    team` is a second, `include_holds=True` projection over the **same**
    `inputs_by_team` this function already built above -- `_aggregate_
    capsheet_inputs` is not called twice, so the expensive part (iterating
    `state.players`) is not duplicated; the second call is pure arithmetic
    over `tuning.horizon` rows per team. `room_by_team`/`apron1_room_by_
    team` keep reading only the existing `include_holds=False` projection,
    byte-for-byte unchanged. This mirrors `season.py::_capsheet_by_team`
    exactly (one aggregation pass, two arithmetic projections), which is
    what makes `ctx.room_for_signings_by_team[t]` and `boundary.capsheet_
    with_holds_by_team[t].room` the same number by construction at
    `free_agency_open` -- see this repo's ledger, architect round 21 §4.2,
    for why that agreement matters.

    **Decision P3-24/P3-26 (§11 step 11).** `head_to_head` is a genuinely
    independent copy of `state.head_to_head` -- both the top-level dict
    AND every row object are copied (`dataclasses.replace` per row), never
    a live alias into the harness's own dict or its own row objects.
    (Round 13 correction, NIT 4: an earlier build copied only the
    top-level dict via `dict(state.head_to_head)`; since `HeadToHead` is a
    non-frozen dataclass, that left `ctx.head_to_head[k] is state.head_to_
    head[k]` `True` for every row -- nothing exploited that today, since
    `gm_service._H2HAccumulator` re-clones every row again on its own
    before ever mutating one, but a future caller that mutated a `ctx.
    head_to_head` row directly, without going through `_H2HAccumulator`,
    would have silently corrupted `state`'s own store. Cloning here too is
    one dict comprehension over a mapping this call already reads in
    full.) `h2h_base_accept_rate` is `gm_service.compute_h2h_base_accept_
    rate` over that same copy, computed once per `build_ctx` call (cheap --
    a two-pass sum over however many rows this league has touched so far,
    never a query). Both are assembled unconditionally, exactly like
    `retirement_age` above, whether or not the caller is about to run a
    trade window with them.

    **Decision P3-48 (§11 step 17).** `head_to_head_enabled` is a reader-
    side-only suppression flag, keyword-only, defaulted `True` so every
    existing call site is byte-identical to today. When `False`, the
    returned `GmContext` gets `head_to_head=None` and `h2h_base_accept_
    rate=None` instead of the clone-and-compute above -- **one branch, at
    exactly those two assignments**, so suppression can never be half-
    applied by a second, separate skip path elsewhere. This does **not**
    change what this harness writes or accumulates: `state.head_to_head`
    itself (the write path, `season.py`'s own `state.head_to_head.update
    (...)` calls, and the season-boundary decay applier) keeps
    accumulating exactly as it always has, in every run, suppressed or
    not -- suppression only affects what THIS function hands back to a
    reader, at the moment of construction. Because this function is
    called fresh at every phase/window (never cached across a run), a
    suppressed run reads `head_to_head=None` at every single call, not
    just the first -- so there is no per-window re-accumulation loophole
    for a caller to fall through.

    **Decision P4-2 (Phase 4 §11 Stage A step 1), superseded at THIS
    function's own construction site by Decisions P4-16/P4-17 (Stage C
    steps 11/12).** `agency_enabled`/`media_enabled` are accepted here,
    keyword-only, defaulted `True`. **`agency_enabled` is no longer a
    no-op here**: `GmContext.motivation_by_player`/`trade_request_
    pressure_by_player` are populated from `state.motivation` (Stage B
    step 10's own mirror) when `agency_enabled` is `True`, `None` when it
    is `False` -- the same "one branch, at exactly those two assignments"
    shape `head_to_head_enabled` already establishes above. `agency_
    enabled` was ALREADY not a no-op for the run as a whole as of Stage B
    step 10 (`season.py`'s `_bootstrap_motivation_inmem`/`_run_agency_
    checkpoint_inmem` calls gate on it directly, outside this function);
    this is the first place `build_ctx` ITSELF reads the flag. `media_
    enabled` stays a genuine no-op everywhere until Stage D exists."""
    cap_settings = state.cap_settings_by_season[state.season]
    next_season_cap_settings = state.cap_settings_by_season.get(state.season + 1)
    if next_season_cap_settings is None:
        next_season_cap_settings = capsheet.project_cap_settings(
            cap_settings, state.season + 1, tuning=tuning
        )

    inputs_by_team = _aggregate_capsheet_inputs(state, tuning)
    # Only the anchor season is passed as a "real" row -- every other
    # season in `state.cap_settings_by_season` was itself grown via
    # `project_cap_settings` at generation time (or at a season boundary,
    # T7a's job), so `project_team_cap_sheet` should mark it `projected=
    # True` too, not `False` -- passing the whole dict here would mark
    # every horizon season as real by dict-membership alone. Both paths
    # call the identical pure `project_cap_settings(anchor, season,
    # tuning)`, so the recomputed figures match `state.cap_settings_by_
    # season` exactly; only the `projected` flag differs, correctly.
    real_anchor = {state.season: cap_settings}
    room_by_team: dict[int, int] = {}
    apron1_room_by_team: dict[int, int] = {}
    room_for_signings_by_team: dict[int, int] = {}
    for team_id, inputs in inputs_by_team.items():
        projections = capsheet.project_team_cap_sheet(
            inputs, real_anchor, tuning=tuning, include_holds=False
        )
        # Slice 12-A (round 11 §5.3): `apron1_room_by_team` is the same
        # `SeasonCapProjection` row `room_by_team` already computes,
        # narrowed to `.apron1_room` instead of `.room` -- no second
        # `project_team_cap_sheet` call.
        projection_next = next(p for p in projections if p.season == state.season + 1)
        room_by_team[team_id] = projection_next.room
        apron1_room_by_team[team_id] = projection_next.apron1_room
        # Decision P2-39 (architect round 21 §4.2): a second, `include_
        # holds=True` projection on the SAME `inputs` -- `inputs_by_team`
        # is not recomputed. `room_by_team`/`apron1_room_by_team` above
        # stay on the `include_holds=False` call only.
        projections_with_holds = capsheet.project_team_cap_sheet(
            inputs, real_anchor, tuning=tuning, include_holds=True
        )
        projection_next_with_holds = next(
            p for p in projections_with_holds if p.season == state.season + 1
        )
        room_for_signings_by_team[team_id] = projection_next_with_holds.room

    philosophy_by_team = {t.id: t.gm_philosophy for t in state.teams.values()}

    # Decision P3-24/P3-26 (§11 step 11): `head_to_head` is an independent
    # copy of `state.head_to_head` -- dict AND row objects both cloned
    # (`dataclasses.replace` per row, since `HeadToHead` is non-frozen) --
    # never the harness's own live dict or its own row objects (a caller
    # mutating `ctx.head_to_head` must not reach back into `state`), and
    # `h2h_base_accept_rate` is computed from that SAME snapshot, once,
    # here -- the population's own current (decayed, pre-this-window) mean,
    # per Decision P3-26's "computed once per trade window ... from the
    # rows it has already loaded" instruction. Computed for every phase
    # `build_ctx` is called for (not only trade windows), matching `
    # retirement_age`'s own "assembled unconditionally, read only where it
    # matters" convention above.
    # Decision P3-48 (§11 step 17): the ONE branch this suppression flag
    # is allowed to take, at exactly these two assignments -- see this
    # function's own docstring for why no second skip path may exist.
    # `state.head_to_head` itself is never touched here either way.
    if head_to_head_enabled:
        head_to_head = {key: dataclasses.replace(row) for key, row in state.head_to_head.items()}
        h2h_base_accept_rate = compute_h2h_base_accept_rate(head_to_head)
    else:
        head_to_head = None
        h2h_base_accept_rate = None

    # Decision P4-16/P4-17 (Phase 4 §11 Stage C steps 11/12) -- the ONE
    # branch `agency_enabled` is allowed to take at this function's own
    # construction site, exactly like `head_to_head_enabled` above (this
    # function's own docstring: "one branch, at exactly those two
    # assignments, so suppression can never be half-applied by a second,
    # separate skip path elsewhere"). `state.motivation` is copied
    # (shallow dict copy -- `PlayerMotivation` rows are never mutated
    # through `ctx`, unlike `HeadToHead` rows, so a per-row clone buys
    # nothing here), never handed out as a live alias into `HarnessState`
    # itself. `trade_request_pressure_by_player` is built from the SAME
    # copy via the SAME pure gate/magnitude function `agency_service.
    # load_trade_request_pressure_for_league` calls on the DB path --
    # one implementation, two callers.
    if agency_enabled:
        motivation_by_player = dict(state.motivation)
        trade_request_pressure_by_player: dict[int, float] = {}
        for player_id, motivation in motivation_by_player.items():
            pressure = trade_request_pressure(
                request_state=motivation.request_state,
                star_leverage=motivation.star_leverage,
                morale=motivation.morale,
            )
            if pressure is not None:
                trade_request_pressure_by_player[player_id] = pressure
    else:
        motivation_by_player = None
        trade_request_pressure_by_player = None

    return GmContext(
        league_id=state.league_id,
        season=state.season,
        phase=phase,
        league_rng_seed=state.rng_seed,
        cap_settings=cap_settings,
        next_season_cap_settings=next_season_cap_settings,
        rookie_scale=state.rookie_scale,
        room_by_team=room_by_team,
        apron1_room_by_team=apron1_room_by_team,
        philosophy_by_team=philosophy_by_team,
        window_by_team=dict(state.window_by_team),
        tuning=tuning,
        retirement_age=_RETIREMENT_AGE,
        room_for_signings_by_team=room_for_signings_by_team,
        head_to_head=head_to_head,
        h2h_base_accept_rate=h2h_base_accept_rate,
        motivation_by_player=motivation_by_player,
        trade_request_pressure_by_player=trade_request_pressure_by_player,
    )


# ---------------------------------------------------------------------------
# The five in-memory appliers (report §5.2, plus T7a2's own apply_waiver_
# inmem) -- one-for-one with gm_service.py's real DB appliers (apply_trade /
# apply_signing / apply_draft / apply_windows / apply_waivers), operating on
# HarnessState instead of an Executor/transaction.
# ---------------------------------------------------------------------------


def apply_trade_inmem(state: HarnessState, execution: TradeExecution) -> None:
    """Per moved player: `team_id`/`roster_status` set on the still-
    mutable nested `Player`, and -- the gap the original architect round
    named first -- the player's own active contract's `team_id` moves with
    them too (`contract.contract.team_id`, `Contract` being a plain,
    mutable dataclass; `apply_trade`'s own `_move_active_contract`
    counterpart). Per moved pick: `current_team_id` reassigned (`DraftPick`
    is also a plain, mutable dataclass -- no `dataclasses.replace` needed
    for either mutation)."""
    for player_id in execution.players_a_to_b:
        rp = state.players[player_id]
        rp.player.team_id = execution.team_b_id
        rp.player.roster_status = RosterStatus.ACTIVE
        if rp.contract is not None:
            rp.contract.contract.team_id = execution.team_b_id
    for player_id in execution.players_b_to_a:
        rp = state.players[player_id]
        rp.player.team_id = execution.team_a_id
        rp.player.roster_status = RosterStatus.ACTIVE
        if rp.contract is not None:
            rp.contract.contract.team_id = execution.team_a_id
    for pick_id in execution.picks_a_to_b:
        state.picks[pick_id].current_team_id = execution.team_b_id
    for pick_id in execution.picks_b_to_a:
        state.picks[pick_id].current_team_id = execution.team_a_id


def apply_signing_inmem(state: HarnessState, execution: SigningExecution) -> None:
    """Terminate any existing contract, then create -- mirrors `apply_
    signing`'s own documented order. "Terminate" here literally means "set
    to `None`": `RosterPlayer` is frozen, so the old `PlayerContract` (if
    any) is simply not referenced by the replacement object built below,
    never mutated to an `EXPIRED` status the way the real DB applier does
    (there is no separate "expired contracts" ledger this harness reads,
    so there is nothing to preserve it for)."""
    rp = state.players[execution.player_id]
    contract_id = state.next_contract_id
    contract = Contract(
        id=contract_id,
        league_id=state.league_id,
        player_id=execution.player_id,
        team_id=execution.team_id,
        signed_in_season=execution.start_season,
        start_season=execution.start_season,
        end_season=execution.start_season + execution.years - 1,
        total_years=execution.years,
        contract_type=ContractType.STANDARD,
        signing_method=execution.signing_method,
        no_trade_clause=False,
        trade_kicker_pct=0,
        status=ContractStatus.ACTIVE,
        terminated_in_season=None,
        created_at=_dummy_created_at(state.season),
        terminated_at=None,
    )
    years = tuple(
        ContractYear(
            contract_id=contract_id,
            season=execution.start_season + i,
            salary=execution.annual_salary,
            guaranteed_amount=execution.annual_salary,
            option_type=OptionType.NONE,
            option_decision=OptionDecision.PENDING,
        )
        for i in range(execution.years)
    )
    rp.player.team_id = execution.team_id
    rp.player.roster_status = RosterStatus.ACTIVE
    state.players[execution.player_id] = dataclasses.replace(
        rp, contract=PlayerContract(contract=contract, years=years)
    )
    state.next_contract_id += 1


def apply_draft_inmem(
    state: HarnessState, pick: DraftPick, result: draft_decisions.DraftPickResult
) -> None:
    """**Deliberately diverges from `apply_draft`.** That function's own
    docstring states plainly that it "does not move the drafted player
    onto the team's roster or sign a rookie-scale contract" -- a real,
    open gap left for whichever later slice owns it. This harness cannot
    leave that gap: without it, drafted players never join a roster, and
    metrics 1/11/12/22 would all read wrong. Moves the prospect from
    `state.prospects` to `state.players`, and installs a real rookie-scale
    (`pick_number <= 30`) or short second-round (`31-60`, `SECOND_ROUND_
    HORIZON` years at `min_salary`, matching `picks.py`'s own horizon
    rule) contract -- see this module's own docstring, "record as a
    finding for whichever later slice closes T4's gap"."""
    if pick.pick_number is None:
        raise ValueError("apply_draft_inmem requires a resolved pick.pick_number")

    pick.used_for_player_id = result.prospect_id
    prospect_rp = state.prospects.pop(result.prospect_id)
    prospect_rp.player.team_id = result.team_id
    prospect_rp.player.roster_status = RosterStatus.ACTIVE
    prospect_rp.player.draft_pick_number = pick.pick_number
    prospect_rp.player.draft_year = state.season
    prospect_rp.player.years_pro = 0

    start_season = state.season + 1
    slot = pick.pick_number
    if slot <= 30:
        row = state.rookie_scale[slot]
        salaries = (row.year_1_salary, row.year_2_salary, row.year_3_salary, row.year_4_salary)
        contract_type = ContractType.ROOKIE_SCALE
    else:
        next_season_cap_settings = state.cap_settings_by_season.get(start_season)
        if next_season_cap_settings is None:
            next_season_cap_settings = capsheet.project_cap_settings(
                state.cap_settings_by_season[state.season], start_season, tuning=DEFAULT_TUNING
            )
        salaries = tuple(
            next_season_cap_settings.min_salary for _ in range(DEFAULT_TUNING.second_round_horizon)
        )
        contract_type = ContractType.STANDARD

    contract_id = state.next_contract_id
    contract = Contract(
        id=contract_id,
        league_id=state.league_id,
        player_id=result.prospect_id,
        team_id=result.team_id,
        signed_in_season=start_season,
        start_season=start_season,
        end_season=start_season + len(salaries) - 1,
        total_years=len(salaries),
        contract_type=contract_type,
        signing_method=SigningMethod.DRAFT,
        no_trade_clause=False,
        trade_kicker_pct=0,
        status=ContractStatus.ACTIVE,
        terminated_in_season=None,
        created_at=_dummy_created_at(state.season),
        terminated_at=None,
    )
    years = tuple(
        ContractYear(
            contract_id=contract_id,
            season=start_season + i,
            salary=salary,
            guaranteed_amount=salary,
            option_type=OptionType.NONE,
            option_decision=OptionDecision.PENDING,
        )
        for i, salary in enumerate(salaries)
    )
    state.players[result.prospect_id] = dataclasses.replace(
        prospect_rp, contract=PlayerContract(contract=contract, years=years)
    )
    state.next_contract_id += 1


def apply_windows_inmem(state: HarnessState, window_pass: WindowPassResult) -> None:
    """`state.window_by_team = dict(window_pass.window_by_team)` -- no
    `to_timeline`, no round-trip, no `CONTEND`-collapse (see `league.py`'s
    module docstring for why this harness is immune to T4's critic
    round-23 finding by construction)."""
    state.window_by_team = dict(window_pass.window_by_team)


def apply_waiver_inmem(state: HarnessState, execution: WaiverExecution) -> None:
    """Mirrors `roster_service.waive`'s own observable effects exactly
    (`dba2/services/roster_service.py:121-`) -- unlike `apply_draft_
    inmem`'s own *deliberate* divergence from its real-DB counterpart
    (that one exists to patch a real, open gap `apply_draft` leaves;
    there is no equivalent gap here for this applier to work around, so
    it follows the real function's documented semantics exactly, the same
    "one-for-one" contract `apply_trade_inmem`/`apply_signing_inmem`
    already hold). T7a2, architect round 8 §7.4 -- this function's own
    dead-cap bookkeeping is that report's own named highest-risk spot
    (§10's risk table: "the in-memory dead-cap ledger silently disagrees
    with `roster_service.waive`"), so every line below is checked against
    that function's own docstring (lines 128-149), not against this
    module's own prior conventions alone:

    - `contract.status = WAIVED`, `contract.terminated_in_season =
      waived_in_season` -- `Contract` is a plain, mutable dataclass
      (`apply_trade_inmem`'s own `contract.contract.team_id = ...`
      precedent), so both are set directly on the still-live object,
      never replaced.
    - For each contract-year with `season >= waived_in_season` AND
      `guaranteed_amount > 0`, that year's `guaranteed_amount` is added
      to `state.dead_cap_by_team_season[(team_id, year.season)]` --
      `roster_service.waive`'s own not-stretched branch, one `dead_cap_
      charges` row per such year there (`reason=WAIVE_GUARANTEED`),
      summed into one running per-`(team, season)` total here rather than
      kept as individual rows (this harness has no `DeadCapCharge`-shaped
      ledger at all, deliberately -- `_aggregate_capsheet_inputs` only
      ever needs the total). A year with `guaranteed_amount == 0` (an
      unguaranteed year) contributes nothing -- the exact same `continue`
      the real function's own loop applies.
    - `player.team_id = None`, `player.roster_status = FREE_AGENT`.
    - The player's `PlayerContract` is dropped (`contract=None` via
      `dataclasses.replace` -- `RosterPlayer` is frozen, matching `apply_
      signing_inmem`'s own "terminate == not referenced by the
      replacement object" convention: there is no separate "expired/
      waived contracts" ledger this harness reads, so there is nothing to
      preserve the old `PlayerContract` object for), leaving the player
      in `state.players` as a free agent -- exactly what makes `_free_
      agency_pool_inmem` (`tools/gm_harness/season.py`) pick him up next
      season's free agency, the behavior §7.4 names as the point of
      leaving him in `state.players` at all (a waived player is
      re-signable, not deleted).

    **Nit 3 fix.** Two guards mirroring `roster_service.waive`'s own
    real-DB validation exactly (`dba2/services/roster_service.py:154,159`
    -- that function raises `ValidationError` for both; asserted here
    instead, matching this function's own existing `contract_id`-mismatch
    guard's convention, `tools/`'s local caller-contract-violation idiom
    rather than importing a `dba2.core.errors` exception type into a
    harness module): a contract that is already non-`ACTIVE` must never
    be waived a second time (without this, a caller replaying the same
    `WaiverExecution` -- or a real double-selection bug upstream --
    would silently double-charge `state.dead_cap_by_team_season`, the
    exact "silently disagrees with `roster_service.waive`" risk this
    function's own docstring names above), and a contract with `team_id
    is None` has no team to charge dead cap to at all."""
    rp = state.players[execution.player_id]
    assert rp.contract is not None and rp.contract.contract.id == execution.contract_id, (
        f"apply_waiver_inmem: player {execution.player_id}'s current contract "
        f"({rp.contract.contract.id if rp.contract else None}) does not match "
        f"WaiverExecution.contract_id={execution.contract_id}"
    )
    contract = rp.contract.contract
    assert contract.status is ContractStatus.ACTIVE, (
        f"apply_waiver_inmem: contract {contract.id} is not ACTIVE "
        f"(status={contract.status!r}) -- cannot waive a contract twice, matching "
        f"roster_service.waive's own guard"
    )
    assert contract.team_id is not None, (
        f"apply_waiver_inmem: contract {contract.id} has no team_id to charge dead "
        f"cap to, matching roster_service.waive's own guard"
    )
    contract.status = ContractStatus.WAIVED
    contract.terminated_in_season = execution.waived_in_season

    for year in rp.contract.years:
        if year.season >= execution.waived_in_season and year.guaranteed_amount > 0:
            key = (execution.team_id, year.season)
            state.dead_cap_by_team_season[key] = (
                state.dead_cap_by_team_season.get(key, 0) + year.guaranteed_amount
            )

    rp.player.team_id = None
    rp.player.roster_status = RosterStatus.FREE_AGENT
    state.players[execution.player_id] = dataclasses.replace(rp, contract=None)
