"""The season-loop driver (architect round-7 report §5.3) plus the scalar-
only recorder types §6.1 defines -- `run_season`/`run_seed`, `BoundaryRecord`/
`SeasonRecord`/`RunRecord`/`Acquisition`, and `check_trade_conservation`.

**10-step phase sequence (9 from §5.3, plus T7a2's own `WAIVERS` step --
`run_season`'s own body follows this order and nothing else):**
`PRESEASON` (classify+apply) -> `REGULAR_SEASON` (trade window #1,
conservation-checked per trade) -> `TRADE_DEADLINE` (classify+apply, then
override `ctx.window_by_team` with this call's own fresh classification
before trade window #2, reproducing `run_phase_hook`'s round-23 CONTEND
fix) -> games (skippable) -> `DRAFT_LOTTERY` (classify+apply, then resolve
this season's pick numbers by `window_score` rank, worst-first, assigned
to `original_team_id`'s rank) -> `DRAFT` (re-keyed prospect class, `run_
draft_phase`, `apply_draft_inmem` pairwise) -> `RESIGN_WINDOW` (pending
offers only, nothing applied) -> `FREE_AGENCY` (real pool, `run_free_
agency`, `apply_signing_inmem`) -> `WAIVERS` (T7a2, architect round 8
§7.3 -- `run_waivers`, `apply_waiver_inmem`, one cut per team per pass
above `roster_max`; placed after `FREE_AGENCY`'s own `season_end`
snapshot and immediately before the season boundary, the exact `FREE_
AGENCY -> WAIVERS -> PRESEASON` graph edge `phase/graph.py:38-39`
declares legal) -> season boundary (`progress_one_season(season=S+1, ...)`,
**not** `season=S` -- see this module's own `run_season` docstring for why
that ordering is load-bearing, not arbitrary, exactly reproducing §5.3's
own reasoning).

**Two additions beyond §5.3/§6.1's literal text, both necessary rather than
optional, both flagged here rather than silently introduced:**

1. **`run_season` takes an extra `first_season: int` keyword-only parameter**
   not present in the report's own signature sketch. §5.5's re-keying
   formula (`new_id = 900_000 + (S - first_season) * 1_000 + i`) and this
   module's own cap-settings-anchor lookup (see `_ensure_cap_settings`) both
   need `first_season` to do their job correctly; `HarnessState` itself
   carries no "run's own first season" field (only its own current,
   ever-advancing `season`), so there is no way to derive it from `state`
   alone. `run_seed` (the only real caller) already has `first_season` on
   hand and threads it through.
2. **`BoundaryRecord` carries an extra `label: str` field** beyond §6.1's
   six named fields. `phase: Phase` alone cannot distinguish "free agency
   open" from "season end" -- both boundaries are captured at `Phase.
   FREE_AGENCY`, per this module's own checkpoint set below -- so a bare
   `phase` field cannot answer "which of the two `FREE_AGENCY`-phase
   boundaries is this" for a `T7b` metric that specifically needs one or the
   other (metric 10's "measured at free-agency open," metric 13/14's "end of
   run"). The six checkpoints §5.3/§6.1 themselves enumerate as the
   required minimum -- `"PRESEASON"`, `"post_trade_window_1"`, `"TRADE_
   DEADLINE"`, `"post_draft"` (folding `DRAFT_LOTTERY`'s own pick-number
   resolution and `DRAFT`'s own execution into one checkpoint, per the
   dispatch's own `"DRAFT_LOTTERY/post-draft"` naming), `"free_agency_open"`
   (captured before any `FREE_AGENCY` signing applies -- the same instant
   `RESIGN_WINDOW` leaves the league in, since that phase applies nothing),
   and `"season_end"` (captured after every `FREE_AGENCY` signing applies,
   before this same module's own `WAIVERS` step runs -- **the same instant
   §5.3's own phase list separately calls "post-FREE_AGENCY,"** recorded
   once under the `"season_end"` label rather than duplicated) -- plus a
   **seventh, T7a2's own addition: `"post_waivers"`** (architect round 8
   §7.3/§9.2), captured after every cut this season's `WAIVERS` step
   applies, immediately before the season-boundary progression call. It is
   deliberately its own checkpoint, not folded into `"season_end"`: `T7b`'s
   own band-file correction (round 8 §9.2 item 2) reads Family-1 invariant
   1's roster-size ceiling as binding starting at `WAIVERS`, not before --
   `"season_end"` (pre-cut) and `"post_waivers"` (post-cut) have to stay
   two distinct, separately-checkable snapshots for that invariant to be
   statable at all.

**A third addition, `SeasonRecord.phase_timings: Mapping[str, float]`** --
not named in §6.1's own field list (that section is conceptually T7b's
metrics-consumption contract), but the dispatch's own separate "per-phase
wall-clock timing" requirement says "recorded on `SeasonRecord` or logged,
your call on exact shape." Recorded here as one `time.monotonic()`-bracketed
float per step, keyed by the same step names `run_season`'s own body uses
(`"PRESEASON"`, `"REGULAR_SEASON"`, `"TRADE_DEADLINE"`, `"GAMES"`,
`"DRAFT_LOTTERY"`, `"DRAFT"`, `"RESIGN_WINDOW"`, `"FREE_AGENCY"`,
`"WAIVERS"` (T7a2's own addition), `"SEASON_BOUNDARY"` -- ten in total now,
nine at this section's original writing), so the eventual real `validate_
gm.py` run can report actuals per the report's own §8 cost-estimate
section rather than inheriting its estimate.

**A fourth, smaller judgment call: the games step's tendencies stream.**
§5.4 says `PlayerTendencies` are "built on demand ... only when games run,"
reusing `validate_sim.py`'s own `_build_team_strategies`/`build_schedule`/
`build_context`/`simulate_game` path wholesale -- but that file's own
tendencies draw is salted on `(team_index, slot_index)`, a generation-time
roster *position* that is meaningless once trades/signings/the draft have
moved players between teams and slots across a multi-season run (the same
"team-index/slot-index don't survive a trade" problem `state.py`'s own
`build_snapshot` roster definition already sidesteps by keying on `player.
id`, not slot). This module instead salts on `(season, player_id)`:
`rng_for(seed, "gm_harness", "tendencies", season, player_id)` -- still
fully deterministic in `(seed, season, player_id)`, and stable under
whichever team a player is currently on, which the generation-time
convention cannot offer once a league is no longer freshly generated.

**A fifth judgment call, on the free-agency pool's in-memory
reconstruction.** `_free_agency_pool_inmem` below reimplements `gm_service.
_load_free_agency_pool`'s exact documented merge rule -- "every already-
`FREE_AGENT` player in `state.players` PLUS every rostered player `expiring_
by_team` names" -- directly against `HarnessState`/`LeagueSnapshot`, since
that real function is `async def`/`Executor`-shaped and cannot be called
from an in-memory loop with zero Postgres.

**Zero I/O beyond what `tools/validate_sim.py`'s own game-sim path already
performs when games run** (§5.4's own honesty requirement) -- verified by
this module's own import list: no `dba2.data`, no `Executor`, no `await`,
matching `tools/gm_harness/state.py`/`league.py`'s own zero-I/O posture.
Transitively imports `dba2.services.gm_service` (`asyncpg` at load time,
never called) and the two quarantined stubs under `tools/`
(`_gm_draft_class_stub.generate_draft_class`, `_gm_progression_stub.
progress_one_season`) -- both already-established, already-tested
dependencies this slice is explicitly instructed to reuse, not reimplement.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from dba2.agency.motivation import (
    INITIAL_MORALE,
    INITIAL_REQUEST_STATE,
    INITIAL_SATISFACTION,
    INITIAL_TRUST,
    advance_request_state,
    compute_morale,
    decay_trust,
    dwell_is_open,
    evaluate_satisfaction,
    fold_satisfaction_deltas,
    fold_trust_deltas,
    generate_traits,
    pressure_from_morale,
    resolve_request_opened_in_season,
)
from dba2.agency.situation import PlayerSituation, window_success_score
from dba2.core.rng import rng_for
from dba2.domain.draft import DraftPick
from dba2.domain.motivation import MotivationAxis, PlayerMotivation
from dba2.domain.phase import Phase
from dba2.domain.player import RosterStatus
from dba2.domain.ratings import PlayerTendencies
from dba2.domain.team import GmPhilosophy
from dba2.managers import capsheet, memory, window
from dba2.managers.capsheet import SeasonCapProjection
from dba2.managers.decisions import draft as draft_decisions
from dba2.managers.needs import build_need_matrix, tier_for_overall
from dba2.managers.philosophy import PROFILES
from dba2.managers.scouting import player_age
from dba2.managers.snapshot import LeagueSnapshot, RosterPlayer, TeamSnapshot, build_roster_player
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow, GmTuning
from dba2.managers.valuation import effective_talent_ovr, market_salary_share
from dba2.services.gm_service import (
    MAX_NEGOTIATION_ROUNDS,
    FreeAgencyResult,
    SigningExecution,
    TradeExecution,
    TradeWindowResult,
    WaiverExecution,
    WaiverWindowResult,
    _eligible_draft_picks_sorted,
    _expiring_by_team,
    classify_all_windows,
    encode_decision_record,
    run_draft_phase,
    run_free_agency,
    run_resign_window,
    run_trade_window,
    run_waivers,
)
from dba2.services.phrase_service import LoadedPhraseLibrary
from dba2.sim.derivation import RATING_GROUPS, derive_tendencies
from dba2.sim.game import simulate_game
from dba2.sim.tuning import TuningProfile
from tools._gm_draft_class_stub import generate_draft_class
from tools._gm_progression_stub import progress_one_season
from tools.gm_harness.league import generate_gm_league
from tools.gm_harness.state import (
    HarnessState,
    apply_draft_inmem,
    apply_signing_inmem,
    apply_trade_inmem,
    apply_waiver_inmem,
    apply_windows_inmem,
    build_ctx,
    build_snapshot,
)
from tools.gm_harness.state import (
    _aggregate_capsheet_inputs as _aggregate_capsheet_inputs_inmem,
)
from tools.validate_sim import LeagueData, _build_team_strategies, build_context
from tools.validate_sim import build_schedule as _build_game_schedule

# ---------------------------------------------------------------------------
# The recorder types (report §6.1) -- scalars only, never a snapshot object.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Acquisition:
    """One player joining one team's roster this season, whichever channel
    -- `metrics.py` (T7b) needs the acquiring team's own window *at the
    moment of acquisition* for metrics 11/15's cohort statistics, which is
    why this is captured inline during the loop rather than reconstructed
    later from boundary snapshots."""

    team_id: int
    player_id: int
    overall: int
    age: int
    channel: str
    """`"trade"` | `"signing"` | `"draft"` -- report §6.1's own three values."""
    window_at_acquisition: CompetitiveWindow


@dataclass(frozen=True)
class BoundaryRecord:
    """One per (season, named checkpoint) -- report §6.1's six fields plus
    `label` (this module's own addition, see module docstring)."""

    phase: Phase
    label: str
    roster_ids_by_team: Mapping[int, tuple[int, ...]]
    overalls_by_team: Mapping[int, tuple[int, ...]]
    """Descending `overall_cached` per team."""
    window_by_team: Mapping[int, CompetitiveWindow]
    capsheet_by_team: Mapping[int, SeasonCapProjection]
    """`season + 1` row, `include_holds=False`. Every metric except M10
    reads this one -- see `capsheet_with_holds_by_team` below (architect
    round 19 §2.5/§5) for why M10's own basis moved to a second, additional
    field instead of this one being flipped in place."""
    pick_owner_by_pick: Mapping[int, int]
    """`current_team_id` per pick id -- Family-1 invariant 3's conservation
    basis at the boundary-snapshot granularity (per-trade checking is
    `check_trade_conservation`'s own, separate job -- see this module's own
    docstring)."""
    contract_count_by_player: Mapping[int, int]
    """Invariant 4's basis. Structurally always `<= 1` in this harness --
    `HarnessState.players` holds exactly one `RosterPlayer` (with at most
    one `contract`) per player id, so two simultaneous active contracts for
    the same player id cannot be represented by this data model at all.
    Still recorded, for shape parity with `metrics.py`'s scanner and so a
    genuine data-model regression (e.g. a future change that lets two ids
    collide) would surface here as a real `> 1` count."""
    capsheet_with_holds_by_team: Mapping[int, SeasonCapProjection] | None = None
    """Architect round 19 §2.5/§5/§7 (Slice 19-A) -- the identical `season +
    1` row `capsheet_by_team` computes, from the same `_aggregate_capsheet_
    inputs` pass, but with `include_holds=True` (`_CAP_HOLD_MULT`-sized
    holds, `state.py::_aggregate_capsheet_inputs`'s own docstring). A
    **second, additional** field, not a flag flip on `capsheet_by_team`
    itself: round 19 §2.2/§2.5 found that a global `include_holds=True`
    flip would very probably fail M2 (Family-1 invariant 2) near-certainly
    at season 1 (the generator's own `apron_2` pin leaves its touched tail
    only $6,546,456 of margin, well inside a realistic hold), and that the
    generator's own `apron_2` guarantee (`league.py:636`) is itself
    expressed in the hold-free basis -- so flipping this field in place
    would decouple that guarantee from the invariant it exists to satisfy.
    `compute_m10` is this field's **sole** counted reader (`metrics.py`);
    `compute_m2` reads it only for a reported-never-gated diagnostic
    (`detail["hold_inclusive_violations"]`). **Defaulted last, default
    `None`, deliberately** -- so a `BoundaryRecord` unpickled from before
    this field existed (every `artifacts/validation_gm/**.pkl` record on
    disk as of this slice) still loads, just without this attribute's own
    real value; `compute_m10`/`compute_m2` both fall back and stamp
    `detail["holds_basis"]`/report `None` respectively rather than
    fabricating a number for a record that never computed one (round 19
    §5's "pickle-compatibility trap," named explicitly)."""


@dataclass(frozen=True)
class SeasonRecord:
    """Report §6.1's own field list, plus `phase_timings` (this module's
    own addition, see module docstring)."""

    season: int
    boundaries: tuple[BoundaryRecord, ...]
    trades_by_window: tuple[tuple[TradeExecution, ...], ...]
    """Exactly 2 entries -- `REGULAR_SEASON`'s window, then `TRADE_
    DEADLINE`'s window."""
    packages_evaluated: tuple[int, ...]
    mutual_accepts: tuple[int, ...]
    """Both 2-tuples, index-aligned with `trades_by_window`."""
    signings: tuple[SigningExecution, ...]
    draft_picks: tuple[draft_decisions.DraftPickResult, ...]
    waivers: tuple[WaiverExecution, ...]
    """T7a2's own addition (architect round 8 §7.1's own file-table entry
    for this module: `"SeasonRecord.waivers"`) -- every cut this season's
    `WAIVERS` step executed, mirroring `signings`'/`draft_picks`' own
    "the real executions, not just the candidates scanned" shape (`run_
    waivers` has no scanned-but-rejected candidates to carry the way
    `run_trade_window`'s own `records` does -- see `WaiverWindowResult`'s
    own docstring)."""
    acquisitions: tuple[Acquisition, ...]
    minutes_weighted_age_by_team: Mapping[int, float]
    """`window.mean_age(team_snapshot, season)`, at the `"season_end"`
    checkpoint."""
    def_group_mean_by_player: Mapping[int, float]
    """Mean of the 12 `ON_BALL_DEF` (4) + `TEAM_DEF` (8) rating fields, per
    rostered player, at the `"season_end"` checkpoint."""
    future_first_net_by_team: Mapping[int, int]
    """Firsts owned for seasons `> season` minus own-originated firsts for
    those seasons owned by someone else, at the `"season_end"` checkpoint."""
    win_totals: Mapping[int, int] | None
    """`None` under `--skip-games`."""
    jsonl_lines: tuple[str, ...]
    """`encode_decision_record` output, in emission order across every
    phase this season, one continuous `seq` counter for the whole season
    (this module's own scoping choice -- `run_phase_hook`'s own `seq`
    resets per hook *call*, and one `run_season` call is this harness's
    equivalent of "every hook call this season," so one continuous counter
    here is the direct analogue, not a deviation)."""
    conservation_violations: tuple[str, ...]
    """Filled inline by `check_trade_conservation`, around every `apply_
    trade_inmem` call this season -- see that function's own docstring."""
    reconstruction_violations: tuple[str, ...]
    """A fourth addition beyond §6.1's own field list (module docstring's
    other two -- `label`, `phase_timings` -- explain the same posture):
    report §6.2 item 5 (Family-1 invariant 5) reads `"record.reconstructs()
    on every emitted DecisionRecord to 1e-6 -- counted at emission time,
    recorded as a violation count"` -- "counted at emission time" has
    nowhere else to live, since `jsonl_lines` holds already-*encoded*
    strings (`encode_decision_record`'s own JSON output), not the live
    `DecisionRecord` objects `.reconstructs()` needs. Populated inline by
    this module's own `_emit` helper, the single funnel every emitted
    record passes through (`Phase.PRESEASON`'s/`Phase.TRADE_DEADLINE`'s
    window-classification records, `run_trade_window`'s own records for
    every *scanned* candidate not just executed ones, `run_free_agency`'s
    records, and every draft pick's own record) -- one entry per failing
    record, empty when clean."""
    phase_timings: Mapping[str, float]
    turns_evaluated: tuple[int, ...] = ()
    evaluations_performed: tuple[int, ...] = ()
    counter_opportunities: tuple[int, ...] = ()
    counters_admitted: tuple[int, ...] = ()
    counter_closed_executions: tuple[int, ...] = ()
    outcome_census: tuple[Mapping[str, int], ...] = ()
    expired_stalled_count: tuple[int, ...] = ()
    negotiation_depth_violations: tuple[int, ...] = ()
    """Phase 3 §11 step 7's own eight new `TradeWindowResult` fields
    (Decision P3-12, M23/M23'/M24/M25/M29/M30's live inputs), carried
    through at the same 2-tuple/index-aligned-with-`trades_by_window` shape
    `packages_evaluated`/`mutual_accepts` already use -- one entry per
    trade window this season (`REGULAR_SEASON`, then `TRADE_DEADLINE`).

    **Placed last, each defaulted to `()`, deliberately -- the same
    `BoundaryRecord.capsheet_with_holds_by_team`/`compute_m10`/`compute_m2`
    "pickle-compatibility trap" pattern this module's own module docstring
    already names (see that field's docstring for the full mechanism).** A
    `SeasonRecord` unpickled from before this step (every `artifacts/
    validation_gm/**.pkl` record on disk as of this slice, e.g. the
    committed `m14_seeds_v2` corpus) never had these eight keys in its
    pickled `__dict__` at all -- unlike `capsheet_with_holds_by_team`'s
    single `None` default, a *plain* (non-factory) `()` default on a
    dataclass field is stored as a class attribute, so instance attribute
    lookup on an old, key-less unpickled instance falls back to it
    automatically (no `__setstate__`/`getattr(..., default)` shim needed) --
    `season.turns_evaluated` on such a record reads `()`, not an
    `AttributeError`, and `dataclasses.replace` (which reads every current
    field via `getattr` before re-`__init__`-ing) succeeds on it too.
    `_pool_negotiation_totals` (`metrics.py`) sums/iterates each of these
    tuples per season -- `sum(())` and iterating `()` both no-op to `0`/
    nothing, so an old, counters-off-shaped pickle pools to all-zero
    totals, which is exactly the "no negotiation opened" reading `compute_
    m23`/`compute_m23p`/`compute_m30`'s own zero-denominator `skip_reason`
    guards (Decision P3-37) already exist to catch -- SKIP, not a crash,
    and no behavior change on a real step-7-generated record, whose
    `run_season` call always supplies all eight fields explicitly (below).
    Ordering matters here in a way it did not for `capsheet_with_holds_by_
    team`: `BoundaryRecord` had no required field after it, but `SeasonRecord`
    does (`phase_timings` and everything above), so these eight -- the
    only defaulted fields this dataclass carries -- must sit after every
    non-defaulted one, per dataclass field-ordering rules, rather than
    inline where §11 step 7 first introduced them (right after `mutual_
    accepts`, which the field list above no longer reflects)."""
    channel3_decision_sites: tuple[int, ...] = ()
    channel3_reachability: tuple[int, ...] = ()
    channel3_flips: tuple[int, ...] = ()
    channel3_abs_shade_sum: tuple[float, ...] = ()
    channel3_abs_shade_row_count: tuple[int, ...] = ()
    """B4 fix (critic round) -- M28 ch3's four `TradeWindowResult`
    fields plus `mean_abs_shade`'s two-part numerator/denominator, carried
    through at the identical 2-tuple/index-aligned-with-`trades_by_window`
    shape the eight fields directly above already use. Placed last for the
    same pickle-compatibility reason (see the docstring above this one):
    a `SeasonRecord` unpickled from before these five fields existed reads
    `()` for each, and `_pool_negotiation_totals`'s `sum(())` no-ops to
    `0`, giving M28 the same "no negotiation opened"/zero-population
    `skip_reason` reading the eight-field guard already established,
    rather than an `AttributeError`. `run_season` always supplies all
    five explicitly for a record it builds itself (below)."""
    channel2_decision_sites: tuple[int, ...] = ()
    channel2_reachability: tuple[int, ...] = ()
    channel2_flips: tuple[int, ...] = ()
    """§11 step 13 -- M28 ch2's three `TradeWindowResult` fields (Decision
    P3-28's replacement, §8.3), carried through at the identical 2-tuple/
    index-aligned-with-`trades_by_window` shape channel 3's own fields
    above already use, for the same pickle-compatibility reason: a
    `SeasonRecord` unpickled from before this step reads `()` for each,
    `_pool_negotiation_totals`'s `sum(())` no-ops to `0`, and M28 keeps
    reading the same zero-population state it already had. `run_season`
    always supplies all three explicitly for a record it builds itself
    (below)."""
    channel1_up_decision_sites: tuple[int, ...] = ()
    channel1_up_reachability: tuple[int, ...] = ()
    channel1_up_flips: tuple[int, ...] = ()
    channel1_down_decision_sites: tuple[int, ...] = ()
    channel1_down_reachability: tuple[int, ...] = ()
    channel1_down_flips: tuple[int, ...] = ()
    """§11 step 17 -- M28 ch1's six `TradeWindowResult` fields (Decision
    P3-36/P3-38/P3-40), carried through at the identical 2-tuple/index-
    aligned-with-`trades_by_window` shape channel 3's and channel 2's own
    fields above already use, for the same pickle-compatibility reason: a
    `SeasonRecord` unpickled from before this step reads `()` for each,
    `_pool_negotiation_totals`'s `sum(())` no-ops to `0`, and M28 keeps
    reading the same zero-population state it already had. `run_season`
    always supplies all six explicitly for a record it builds itself
    (below)."""
    channel3_max_observed: tuple[float, ...] = ()
    channel2_max_observed: tuple[float, ...] = ()
    channel1_up_max_observed: tuple[float, ...] = ()
    channel1_down_max_observed: tuple[float, ...] = ()
    far_rejects_observed: tuple[int, ...] = ()
    """§11 step 17 item 2 (Decision P3-41) -- the four `max_observed`
    diagnostics plus the triage's own `far_rejects_observed` counter, at
    the identical 2-tuple/index-aligned-with-`trades_by_window` shape
    every other Family-5 field above already uses, for the same pickle-
    compatibility reason: a `SeasonRecord` unpickled from before this
    step reads `()` for each. `_pool_negotiation_totals` pools the four
    `max_observed` fields with `max`, not `+=` (an empty `()` contributes
    `0.0` via the running-max seed, never a crash on `max()` of nothing --
    see that function's own docstring), and `far_rejects_observed` with
    `+=` like every other integer counter. `run_season` always supplies
    all five explicitly for a record it builds itself (below)."""
    phrase_selections_total: tuple[int, ...] = ()
    phrase_fallback_selections: tuple[int, ...] = ()
    phrase_selections_by_situation: tuple[Mapping[str, int], ...] = ()
    phrase_fallback_by_situation: tuple[Mapping[str, int], ...] = ()
    phrase_selections_by_philosophy_template: tuple[Mapping[str, Mapping[str, int]], ...] = ()
    """§11 step 16 -- M26 (`phrase_fallback_share`) and M27 (`voice_
    differentiation`)'s five live `TradeWindowResult` fields (§9.1,
    Decision P3-19/P3-31), carried through at the identical 2-tuple/
    index-aligned-with-`trades_by_window` shape every other Family-5
    field above already uses. Placed LAST **as of step 16** for the same
    pickle-compatibility reason this module's own module docstring and
    every prior Family-5 field addition already name (see `turns_
    evaluated`'s own docstring, above, for the full mechanism): a
    `SeasonRecord` unpickled from before this step (every `artifacts/
    validation_gm/**.pkl` record on disk as of this step, since dialogue
    was not wired into `validate_gm.py`'s own real corpus run until now)
    never had these five keys in its pickled `__dict__` at all -- a
    plain, non-factory `()` default on a dataclass field is stored as a
    class attribute, so instance attribute lookup on an old, key-less
    unpickled instance falls back to it automatically, no `__setstate__`/
    `getattr(..., default)` shim needed. `_pool_phrase_totals`
    (`metrics.py`) sums/merges each of these tuples per season --
    `sum(())` and iterating `()` both no-op to `0`/nothing, so an old,
    dialogue-off-shaped pickle pools to all-zero totals, which is exactly
    the `total_selections == 0` / `either cohort's selection count is 0`
    `skip_reason` guards (Decision P3-37) `compute_m26`/`compute_m27`
    already carry -- SKIP, not a crash, and no behavior change on a real
    step-16-generated record, whose `run_season` call always supplies all
    five explicitly (below).

    **Dated correction (§11 Stage D step 16's second fix round):** a
    sixth field, `phrase_selections_by_philosophy_team_template`, was
    added here for a team-id-keyed within-cohort escalation null and then
    orphaned when that null was replaced by a binomial-thinning split
    over THIS field's own cohort-pooled distribution (`_within_cohort_
    binomial_thin_tv`) -- it had zero readers anywhere in the codebase
    and was removed (from `gm_service.py`'s `_NegotiationTally`/
    `TradeWindowResult`, this field, and `metrics.py`'s matching dead-code
    note) rather than left in place, since it was never committed prior
    to its own removal (reverting a superseded part of an in-progress
    change, not unrelated-refactor churn)."""
    agency_request_state_counts: tuple[Mapping[str, int], ...] = ()
    """Phase 4 Decision P4-35 (Stage C step 14) -- one `{RequestState.
    value: count}` snapshot per agency checkpoint this season (2-tuple,
    index-aligned with Decision P4-11's own two checkpoints -- trade
    deadline, then offseason), over every player in `state.motivation` at
    that instant. `compute_m31`'s (`metrics.py`) real input for the
    request-rate presence-and-plausibility band. Placed LAST for the same
    pickle-compatibility reason every prior Family-5/Phase-4 field
    addition above already names: a `SeasonRecord` unpickled from before
    this step reads `()`, and `compute_m31`'s own zero-population
    `skip_reason` guard (mirroring `compute_m26`/`compute_m27`'s
    established shape) turns that into `SKIP`, not a crash. `agency_
    enabled=False` also reads `()` here (no checkpoint runs at all), the
    same "mechanism absent reads exactly like an old pickle" property
    Decision P4-34's zero-mechanism reading depends on."""
    agency_cumulative_transition_count_by_player: tuple[tuple[int, int], ...] = ()
    """Phase 4 Decision P4-35 (Stage C step 14) -- a snapshot of `Harness
    State.transition_count_by_player` (that field's own docstring has the
    full "why a separate counter" reasoning), taken once at season end,
    as a plain `(player_id, count)` tuple-of-pairs rather than a `dict`
    for the identical pickle-compatibility reason `()` is used as the
    empty default everywhere else on this dataclass. Cumulative across
    the WHOLE run so far (not a per-season delta) -- the LAST season's
    own snapshot is therefore the real input `compute_m32`'s churn-bound
    metric reads for "did any player oscillate across the ladder more
    than N times in this run," Decision P4-35's own direct measurement of
    whether hysteresis works."""


@dataclass(frozen=True)
class RunRecord:
    rng_seed: int
    philosophy_by_team: Mapping[int, GmPhilosophy]
    season1_top1_minus_8th_by_team: Mapping[int, float]
    """Metric 14's paired-delta baseline -- `top1_ovr - 8th_ovr` per team,
    at season 1's very start (before `PRESEASON` even runs)."""
    seasons: tuple[SeasonRecord, ...]


# ---------------------------------------------------------------------------
# Family-1 invariant 3 -- a runtime check, called around every apply_trade_
# inmem, not reconstructed post-hoc from boundary snapshots (report §6.1).
# ---------------------------------------------------------------------------


def check_trade_conservation(
    before: LeagueSnapshot, after: LeagueSnapshot, execution: TradeExecution
) -> tuple[str, ...]:
    """Asserts (1) every id `execution` claims moved actually did move --
    off the sender's roster/pick list in `after`, onto the receiver's --
    and (2) the league-wide multiset of player ids and pick ids is
    unchanged by this one trade (nothing vanished, nothing duplicated).
    Returns a tuple of human-readable violation strings, empty when clean.

    `before`/`after` must be `build_snapshot(state)` calls bracketing
    exactly this one `apply_trade_inmem(state, execution)` call -- calling
    this post-hoc across a whole trade *window* (many trades) would only
    catch net drift, never pin a violation to the specific trade that
    caused it (report §6.1's own reasoning for why this must be a runtime
    check, not a post-hoc one)."""
    violations: list[str] = []

    before_a = before.team_by_id(execution.team_a_id)
    before_b = before.team_by_id(execution.team_b_id)
    after_a = after.team_by_id(execution.team_a_id)
    after_b = after.team_by_id(execution.team_b_id)

    before_a_players = {rp.id for rp in before_a.roster}
    before_b_players = {rp.id for rp in before_b.roster}
    after_a_players = {rp.id for rp in after_a.roster}
    after_b_players = {rp.id for rp in after_b.roster}

    for pid in execution.players_a_to_b:
        if pid not in before_a_players:
            violations.append(f"player {pid}: claimed a->b move but was not on team_a before")
        if pid in after_a_players:
            violations.append(f"player {pid}: claimed a->b move but is still on team_a after")
        if pid not in after_b_players:
            violations.append(f"player {pid}: claimed a->b move but is not on team_b after")
    for pid in execution.players_b_to_a:
        if pid not in before_b_players:
            violations.append(f"player {pid}: claimed b->a move but was not on team_b before")
        if pid in after_b_players:
            violations.append(f"player {pid}: claimed b->a move but is still on team_b after")
        if pid not in after_a_players:
            violations.append(f"player {pid}: claimed b->a move but is not on team_a after")

    before_a_picks = {p.id for p in before_a.draft_picks}
    before_b_picks = {p.id for p in before_b.draft_picks}
    after_a_picks = {p.id for p in after_a.draft_picks}
    after_b_picks = {p.id for p in after_b.draft_picks}

    for pid in execution.picks_a_to_b:
        if pid not in before_a_picks:
            violations.append(f"pick {pid}: claimed a->b move but was not owned by team_a before")
        if pid in after_a_picks:
            violations.append(f"pick {pid}: claimed a->b move but is still owned by team_a after")
        if pid not in after_b_picks:
            violations.append(f"pick {pid}: claimed a->b move but is not owned by team_b after")
    for pid in execution.picks_b_to_a:
        if pid not in before_b_picks:
            violations.append(f"pick {pid}: claimed b->a move but was not owned by team_b before")
        if pid in after_b_picks:
            violations.append(f"pick {pid}: claimed b->a move but is still owned by team_b after")
        if pid not in after_a_picks:
            violations.append(f"pick {pid}: claimed b->a move but is not owned by team_a after")

    before_all_players = {rp.id for t in before.teams for rp in t.roster}
    after_all_players = {rp.id for t in after.teams for rp in t.roster}
    if before_all_players != after_all_players:
        violations.append(
            "league-wide rostered player id set changed across a trade "
            f"(vanished={before_all_players - after_all_players}, "
            f"appeared={after_all_players - before_all_players})"
        )

    before_all_picks = {p.id for t in before.teams for p in t.draft_picks}
    after_all_picks = {p.id for t in after.teams for p in t.draft_picks}
    if before_all_picks != after_all_picks:
        violations.append(
            "league-wide draft pick id set changed across a trade "
            f"(vanished={before_all_picks - after_all_picks}, "
            f"appeared={after_all_picks - before_all_picks})"
        )

    return tuple(violations)


# ---------------------------------------------------------------------------
# Small pure helpers -- boundary-record assembly, acquisitions, pick-lottery
# resolution, the future-first-net statistic, and the in-memory free-agency
# pool reconstruction (judgment call 5, module docstring).
# ---------------------------------------------------------------------------

_DEF_GROUP_FIELDS: tuple[str, ...] = RATING_GROUPS["ON_BALL_DEF"] + RATING_GROUPS["TEAM_DEF"]
# **Report §6.2 item 16 says "12 fields" (ON_BALL_DEF (4) + TEAM_DEF (8))
# -- that arithmetic is stale against the actual, committed `RATING_GROUPS`
# table** (`dba2/sim/derivation.py`): `ON_BALL_DEF` is indeed 4 fields, but
# `TEAM_DEF` is 9 (`screen_navigation, help_iq, rotation_speed, rim_
# protection, closeout_speed, closeout_control, scheme_discipline,
# switchability, defensive_rebounding`), for a true total of 13, not 12.
# Followed here as the actual two full groups the report names ("the 12
# fields in RATING_GROUPS['ON_BALL_DEF'] + RATING_GROUPS['TEAM_DEF']" --
# the operative instruction is "use these two whole groups," not "trim to
# exactly 12"), rather than silently dropping one real field to force the
# report's own miscounted total. Flagged in this slice's own build report
# as a finding for whoever owns `phase-2-manager-minds.md` §10's metric 16
# text next, not silently reconciled here.
assert len(_DEF_GROUP_FIELDS) == 13, "ON_BALL_DEF (4) + TEAM_DEF (9) must be 13 fields"

_PROSPECT_ID_BASE = 900_000
_PROSPECT_ID_SEASON_STRIDE = 1_000


def _dummy_created_at(season: int) -> datetime:
    """Matches `state.py`'s own `_dummy_created_at` convention -- a small,
    trivial helper, kept local rather than importing that module's private
    name for a one-liner (unlike `_aggregate_capsheet_inputs`, below, where
    reusing the real implementation is what avoids a second, driftable copy
    of real cap-sheet math)."""
    return datetime(season, 1, 1, tzinfo=UTC)


def _def_group_mean(rp: RosterPlayer) -> float:
    return sum(getattr(rp.ratings, f) for f in _DEF_GROUP_FIELDS) / len(_DEF_GROUP_FIELDS)


def _overalls_by_team(snapshot: LeagueSnapshot) -> dict[int, tuple[int, ...]]:
    return {
        ts.id: tuple(sorted((rp.player.overall_cached for rp in ts.roster), reverse=True))
        for ts in snapshot.teams
    }


def _top1_minus_8th_by_team(snapshot: LeagueSnapshot) -> dict[int, float]:
    """Metric 14's baseline gap, per team -- teams with fewer than 8
    rostered players are dropped (undefined gap; `metrics.py`'s own edge
    case per report §6.2 item 14, reproduced here at the recording site
    too so `RunRecord` never carries a fabricated value for a team this
    thin). Every generated league has 15-man rosters, so this drop is a
    defensive no-op at the harness's own default `players_per_team`, not
    an expected occurrence."""
    result: dict[int, float] = {}
    for ts in snapshot.teams:
        overalls = sorted((rp.player.overall_cached for rp in ts.roster), reverse=True)
        if len(overalls) >= 8:
            result[ts.id] = float(overalls[0] - overalls[7])
    return result


def _capsheet_by_team(
    state: HarnessState, tuning: GmTuning
) -> tuple[dict[int, SeasonCapProjection], dict[int, SeasonCapProjection]]:
    """The full `SeasonCapProjection` row at `state.season + 1`, per team,
    in **two** bases -- `(hold_free, hold_inclusive)` -- from **one**
    `_aggregate_capsheet_inputs` call (architect round 19 §5 call-site
    table: "a second `project_team_cap_sheet` call on the *same*
    `inputs_by_team`, arithmetic only"). `hold_free` is byte-identical to
    what this function returned before round 19 (`include_holds=False`,
    unchanged basis, unchanged call) -- `build_ctx`'s own `room_by_team`
    computation narrowed to just `.room`; reusing the identical `_
    aggregate_capsheet_inputs` basis (`state.py`) rather than a second,
    driftable transcription of the same aggregation math, exactly the same
    "one basis" argument `state.py`'s own module docstring already makes
    for `build_ctx` itself. `hold_inclusive` is the same rows with
    `include_holds=True` -- `_aggregate_capsheet_inputs`'s own `cap_holds`
    is `0` at every row but `season + 1` (round 19 §4.5), so `hold_
    inclusive` and `hold_free` are identical at every other row by
    construction and can only differ at the one row every caller of this
    function actually reads."""
    inputs_by_team = _aggregate_capsheet_inputs_inmem(state, tuning)
    real_anchor = {state.season: state.cap_settings_by_season[state.season]}
    hold_free: dict[int, SeasonCapProjection] = {}
    hold_inclusive: dict[int, SeasonCapProjection] = {}
    for team_id, inputs in inputs_by_team.items():
        projections_free = capsheet.project_team_cap_sheet(
            inputs, real_anchor, tuning=tuning, include_holds=False
        )
        hold_free[team_id] = next(p for p in projections_free if p.season == state.season + 1)
        projections_with_holds = capsheet.project_team_cap_sheet(
            inputs, real_anchor, tuning=tuning, include_holds=True
        )
        hold_inclusive[team_id] = next(
            p for p in projections_with_holds if p.season == state.season + 1
        )
    return hold_free, hold_inclusive


def _contract_count_by_player(state: HarnessState) -> dict[int, int]:
    return {pid: (1 if rp.contract is not None else 0) for pid, rp in state.players.items()}


def _build_boundary_record(
    state: HarnessState, *, phase: Phase, label: str, tuning: GmTuning
) -> BoundaryRecord:
    snapshot = build_snapshot(state)
    capsheet_by_team, capsheet_with_holds_by_team = _capsheet_by_team(state, tuning)
    return BoundaryRecord(
        phase=phase,
        label=label,
        roster_ids_by_team={ts.id: tuple(rp.id for rp in ts.roster) for ts in snapshot.teams},
        overalls_by_team=_overalls_by_team(snapshot),
        window_by_team=dict(state.window_by_team),
        capsheet_by_team=capsheet_by_team,
        pick_owner_by_pick={p.id: p.current_team_id for p in state.picks.values()},
        contract_count_by_player=_contract_count_by_player(state),
        capsheet_with_holds_by_team=capsheet_with_holds_by_team,
    )


def _acquisition(state: HarnessState, *, team_id: int, player_id: int, channel: str) -> Acquisition:
    """Builds the `Acquisition` record AND resets `state.tenure_seasons_
    by_player[player_id]` to `0` (Decision P4-63) -- every channel this
    function is called for (trade, draft, signing) is, by construction,
    the one place this harness observes a player joining a team. A
    same-team re-sign resets tenure too (a disclosed simplification: the
    harness's own contract-expiry model routes every re-sign through the
    free-agency pool exactly like an external signing, so there is no
    "still on the same roster, never actually left" distinction to
    preserve here)."""
    rp = state.players[player_id]
    state.tenure_seasons_by_player[player_id] = 0
    return Acquisition(
        team_id=team_id,
        player_id=player_id,
        overall=rp.player.overall_cached,
        age=player_age(rp.player.birth_date, state.season),
        channel=channel,
        window_at_acquisition=state.window_by_team[team_id],
    )


def _trade_acquisitions(state: HarnessState, execution: TradeExecution) -> tuple[Acquisition, ...]:
    """One `Acquisition` per player moved by this trade, from the
    *receiving* side's perspective -- `players_a_to_b` are acquisitions for
    `team_b_id`, `players_b_to_a` are acquisitions for `team_a_id`. Called
    after `apply_trade_inmem`, so `_acquisition`'s own `state.players[...]`
    read sees each player's post-move `team_id`/`overall_cached`."""
    acquired = tuple(
        _acquisition(state, team_id=execution.team_b_id, player_id=pid, channel="trade")
        for pid in execution.players_a_to_b
    )
    acquired += tuple(
        _acquisition(state, team_id=execution.team_a_id, player_id=pid, channel="trade")
        for pid in execution.players_b_to_a
    )
    return acquired


# ---------------------------------------------------------------------------
# Phase 4 §11 Stage B step 10 -- the harness's own motivation bootstrap and
# two-per-season checkpoint (Decisions P4-6, P4-9, P4-13, P4-56/P4-71,
# P4-63). Write-only: nothing this stage feeds back into any GM decision.
# ---------------------------------------------------------------------------


def _bootstrap_motivation_inmem(state: HarnessState) -> None:
    """Decision P4-6's trait generator, run for every player in `state.
    players` who has no `state.motivation` entry yet -- called once
    before a fresh league's first checkpoint and again after every draft
    (Step 6, below), so a rookie's motivation exists before the next
    checkpoint tries to read it. Idempotent, same as `agency_service.
    bootstrap_league_motivation`'s DB-path sibling: a player who already
    has an entry is left untouched."""
    for player_id, rp in state.players.items():
        if player_id in state.motivation:
            continue
        age = float(player_age(rp.player.birth_date, state.season))
        traits = generate_traits(
            state.rng_seed, player_id, age=age, overall=rp.player.overall_cached
        )
        state.motivation[player_id] = PlayerMotivation(
            player_id=player_id,
            league_id=state.league_id,
            weight_winning=traits.weight_winning,
            weight_role_usage=traits.weight_role_usage,
            weight_loyalty=traits.weight_loyalty,
            weight_money=traits.weight_money,
            weight_legacy=traits.weight_legacy,
            hometown_team_id=traits.hometown_team_id,
            market_preference=traits.market_preference,
            star_leverage=traits.star_leverage,
            satisfaction_winning=INITIAL_SATISFACTION,
            satisfaction_role_usage=INITIAL_SATISFACTION,
            satisfaction_loyalty=INITIAL_SATISFACTION,
            satisfaction_money=INITIAL_SATISFACTION,
            satisfaction_legacy=INITIAL_SATISFACTION,
            morale=INITIAL_MORALE,
            trust_in_management=INITIAL_TRUST,
            request_state=INITIAL_REQUEST_STATE,
            request_opened_in_season=None,
            last_evaluated_season=state.season,
        )


def _build_harness_player_situation(
    state: HarnessState,
    team_snapshot: TeamSnapshot,
    roster_player: RosterPlayer,
    motivation: PlayerMotivation,
    *,
    tuning: GmTuning,
) -> PlayerSituation:
    """The harness's own `PlayerSituation` builder (P4-9's table),
    mirroring `services/agency_service.py::build_player_situation`'s
    shape but reading everything from already-in-memory `HarnessState`
    rather than a DB round trip. Two fields are disclosed vacuities in
    this corpus, not approximated: `minutes_share` (the harness tracks no
    per-player minutes anywhere -- `_run_games` returns only team win
    totals) and `team_accolades` (the harness has no award-tracking
    mechanism at all). Both stay `None` -- P4-9's "absent is neutral,
    never zero" rule, not a guess."""
    window = state.window_by_team.get(team_snapshot.id)
    team_success = window_success_score(window)

    matrix = build_need_matrix(team_snapshot, tuning=tuning)
    tier = tier_for_overall(roster_player.player.overall_cached)
    position = roster_player.player.primary_position
    role_fit = matrix.need[position][tier] - matrix.glut[position][tier]

    # Round-5 critic finding B2, as corrected by round-5's own re-
    # verification -- see `services/agency_service.py::build_player_
    # situation`'s identical comment for the full derivation. Short
    # version: `market_value_for` returns §4.5's multi-season discounted
    # trade-equity score, not a dollar figure comparable to one season's
    # `year.salary`; `market_salary_share * salary_cap` is the real,
    # already-production market-salary curve (§4.4, `managers/decisions/
    # signing.py`'s own `market` variable) that IS dollar-comparable.
    pay_vs_market: float | None = None
    if roster_player.contract is not None:
        year = next((y for y in roster_player.contract.years if y.season == state.season), None)
        cap_settings = state.cap_settings_by_season.get(state.season)
        if year is not None and cap_settings is not None:
            profile = PROFILES[team_snapshot.team.gm_philosophy]
            effective_ovr = effective_talent_ovr(roster_player, profile)
            market_salary = market_salary_share(float(effective_ovr), cap_settings) * (
                cap_settings.salary_cap
            )
            if market_salary > 0:
                pay_vs_market = year.salary / market_salary

    # Decision P4-63 -- the harness's own per-player tenure mirror, the
    # one PlayerSituation field this corpus CAN supply that the DB path
    # (Stage B, agency_service.py) currently cannot.
    tenure_seasons = float(state.tenure_seasons_by_player.get(roster_player.id, 0))

    market_match: float | None = None
    if motivation.hometown_team_id is not None:
        market_match = 1.0 if motivation.hometown_team_id == team_snapshot.id else 0.0

    return PlayerSituation(
        team_success=team_success,
        role_fit=role_fit,
        minutes_share=None,
        pay_vs_market=pay_vs_market,
        tenure_seasons=tenure_seasons,
        market_match=market_match,
        team_accolades=None,
    )


_MOTIVATION_AXIS_STATE_FIELDS: tuple[tuple[MotivationAxis, str], ...] = (
    (MotivationAxis.WINNING, "satisfaction_winning"),
    (MotivationAxis.ROLE_USAGE, "satisfaction_role_usage"),
    (MotivationAxis.LOYALTY, "satisfaction_loyalty"),
    (MotivationAxis.MONEY, "satisfaction_money"),
    (MotivationAxis.LEGACY, "satisfaction_legacy"),
)
_MOTIVATION_WEIGHT_FIELDS: tuple[tuple[MotivationAxis, str], ...] = (
    (MotivationAxis.WINNING, "weight_winning"),
    (MotivationAxis.ROLE_USAGE, "weight_role_usage"),
    (MotivationAxis.LOYALTY, "weight_loyalty"),
    (MotivationAxis.MONEY, "weight_money"),
    (MotivationAxis.LEGACY, "weight_legacy"),
)


def _run_agency_checkpoint_inmem(
    state: HarnessState, snapshot: LeagueSnapshot, *, tuning: GmTuning
) -> int:
    """Decision P4-11's checkpoint (exactly two a season: trade deadline
    and offseason -- the caller decides which by when it calls this,
    Step 3/Step 8 below), harness-side (Stage B step 10). Write-only:
    updates `state.motivation`/`state.motivation_dwell` only, reads
    nothing back into `snapshot`/`ctx`. Returns the number of ladder
    transitions this checkpoint produced."""
    transitions = 0
    for team_snapshot in snapshot.teams:
        for roster_player in team_snapshot.roster:
            motivation = state.motivation.get(roster_player.id)
            if motivation is None:
                # Bootstrap missed this player (should not happen once
                # `_bootstrap_motivation_inmem` runs after every draft) --
                # skip rather than fabricate traits at checkpoint time.
                continue

            weights = {
                axis: getattr(motivation, field) for axis, field in _MOTIVATION_WEIGHT_FIELDS
            }
            current_satisfactions = {
                axis: getattr(motivation, field) for axis, field in _MOTIVATION_AXIS_STATE_FIELDS
            }

            situation = _build_harness_player_situation(
                state, team_snapshot, roster_player, motivation, tuning=tuning
            )
            requested = evaluate_satisfaction(weights, situation)
            new_satisfactions, _written = fold_satisfaction_deltas(current_satisfactions, requested)
            new_morale = compute_morale(weights, new_satisfactions)
            # Round-5 critic finding B4: the write boundary must actually
            # call the saturating fold's clamp, not just `decay_trust` --
            # `fold_trust_deltas` is otherwise dead code in production
            # (Decision P4-45), the harness's own mirror of the same
            # defect `services/agency_service.py` had.
            new_trust, _trust_written = fold_trust_deltas(
                decay_trust(motivation.trust_in_management), ()
            )

            last_transition_season = state.motivation_dwell.get(roster_player.id)
            pressure = pressure_from_morale(new_morale)
            dwell_open = dwell_is_open(
                current_season=state.season, last_transition_season=last_transition_season
            )
            years_remaining = (
                roster_player.contract.contract.end_season - state.season
                if roster_player.contract is not None
                else None
            )
            new_request_state, transitioned = advance_request_state(
                motivation.request_state,
                pressure=pressure,
                dwell_open=dwell_open,
                contract_years_remaining=years_remaining,
            )

            # Round-5 critic finding B1: same pure, rung-based rule the DB
            # path uses (`resolve_request_opened_in_season`) -- this
            # harness twin previously reimplemented the same broken
            # string-literal logic independently, which is how the same
            # bug landed in both places.
            request_opened_in_season = resolve_request_opened_in_season(
                previous_state=motivation.request_state,
                new_state=new_request_state,
                season=state.season,
                previous_request_opened_in_season=motivation.request_opened_in_season,
            )
            if transitioned:
                transitions += 1
                state.motivation_dwell[roster_player.id] = state.season
                # Decision P4-35's churn-bound metric input -- see
                # `HarnessState.transition_count_by_player`'s own
                # docstring for why this is a separate counter from
                # `motivation_dwell` above, not derived from it.
                state.transition_count_by_player[roster_player.id] = (
                    state.transition_count_by_player.get(roster_player.id, 0) + 1
                )

            state.motivation[roster_player.id] = dataclasses.replace(
                motivation,
                satisfaction_winning=new_satisfactions[MotivationAxis.WINNING],
                satisfaction_role_usage=new_satisfactions[MotivationAxis.ROLE_USAGE],
                satisfaction_loyalty=new_satisfactions[MotivationAxis.LOYALTY],
                satisfaction_money=new_satisfactions[MotivationAxis.MONEY],
                satisfaction_legacy=new_satisfactions[MotivationAxis.LEGACY],
                morale=new_morale,
                trust_in_management=new_trust,
                request_state=new_request_state,
                request_opened_in_season=request_opened_in_season,
                last_evaluated_season=state.season,
            )
    return transitions


def _request_state_counts(state: HarnessState) -> Mapping[str, int]:
    """Decision P4-35 (Stage C step 14): a `{RequestState.value: count}`
    snapshot over every player `state.motivation` currently holds, at the
    instant it is called -- `compute_m31`'s real input. Called right
    after `_run_agency_checkpoint_inmem` at each of `run_season`'s two
    checkpoint sites, never independently, so it always reads a
    POST-checkpoint state."""
    counts: dict[str, int] = {}
    for motivation in state.motivation.values():
        key = motivation.request_state.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def _count_mutual_accepts(records: Sequence) -> int:
    """`run_trade_window` appends `(record_a, record_b)` for every scanned
    candidate, in order, and discards its own internal "both sides
    accepted" tally -- reconstructed here from the exposed `records`
    sequence alone (no re-evaluation, purely a scalar count over already-
    returned data) so metric 19's `mutual_accepts / packages_evaluated`
    has a real numerator to read."""
    assert len(records) % 2 == 0, "records must be (record_a, record_b) pairs, in order"
    return sum(
        1
        for i in range(0, len(records), 2)
        if records[i].verdict == "accept" and records[i + 1].verdict == "accept"
    )


def _resolve_pick_numbers(
    state: HarnessState, *, snapshot: LeagueSnapshot, tuning: GmTuning
) -> None:
    """DRAFT_LOTTERY's own pick-number resolution (report §5.3 step 5):
    rank teams ascending by `window_score` (worst first), assign round-1
    `pick_number` 1..30 and round-2 31..60 to each team's own `state.
    season` picks by that rank -- **by `original_team_id`'s rank, not
    `current_team_id`'s** (a traded pick conveys the team that traded it
    away's competitive slot). Mutates `DraftPick.pick_number` in place
    (`DraftPick` is a plain, mutable dataclass, `apply_draft_inmem`'s own
    precedent for direct mutation)."""
    scores = {ts.id: window.window_score(ts, snapshot, tuning=tuning) for ts in snapshot.teams}
    worst_first = sorted(scores, key=lambda tid: scores[tid])

    pick_by_key: dict[tuple[int, int], DraftPick] = {}
    for pick in state.picks.values():
        if pick.season == state.season:
            pick_by_key[(pick.round, pick.original_team_id)] = pick

    for round_num, offset in ((1, 0), (2, 30)):
        for rank, team_id in enumerate(worst_first, start=1):
            pick = pick_by_key.get((round_num, team_id))
            if pick is not None:
                pick.pick_number = rank + offset


def _future_first_net_by_team(state: HarnessState) -> dict[int, int]:
    """Firsts owned for seasons `> state.season` minus own-originated
    firsts for those seasons owned by someone else, per team (report §6.1's
    `future_first_net_by_team`, metric 12's own basis)."""
    net: dict[int, int] = dict.fromkeys(state.teams, 0)
    for pick in state.picks.values():
        if pick.round != 1 or pick.season <= state.season:
            continue
        net[pick.current_team_id] = net.get(pick.current_team_id, 0) + 1
        if pick.original_team_id != pick.current_team_id:
            net[pick.original_team_id] = net.get(pick.original_team_id, 0) - 1
    return net


def _rekey_prospects(
    prospects: tuple[RosterPlayer, ...], *, season: int, first_season: int
) -> tuple[RosterPlayer, ...]:
    """§5.5's exact formula: `new_id = 900_000 + (season - first_season) *
    1_000 + i`, `i` the prospect's own `1..class_size` id from `generate_
    draft_class` -- applied via `dataclasses.replace` on both `Player.id`
    and `PlayerRatings.player_id`, **before this function's caller ever
    hands the result to `run_draft_phase`** (which is the only valuation
    call site in this loop -- `prospect_fit_value_for` salts its scouting
    draw on `player_id`, so re-keying after any valuation call would
    silently change every scouted price)."""
    base = _PROSPECT_ID_BASE + (season - first_season) * _PROSPECT_ID_SEASON_STRIDE
    rekeyed: list[RosterPlayer] = []
    for rp in prospects:
        new_id = base + rp.player.id
        new_player = dataclasses.replace(rp.player, id=new_id)
        new_ratings = dataclasses.replace(rp.ratings, player_id=new_id)
        rekeyed.append(build_roster_player(new_player, new_ratings, contract=None))
    return tuple(rekeyed)


def _free_agency_pool_inmem(
    state: HarnessState,
    snapshot: LeagueSnapshot,
    expiring_by_team: Mapping[int, tuple[int, ...]],
) -> tuple[RosterPlayer, ...]:
    """`gm_service._load_free_agency_pool`'s exact documented merge, in
    memory (module docstring, judgment call 5): every already-`FREE_AGENT`
    player in `state.players`, plus every rostered player `expiring_by_
    team` names (read off `snapshot`, the same already-loaded source the
    real function reads its own second half from)."""
    already_fa = tuple(
        rp for rp in state.players.values() if rp.player.roster_status is RosterStatus.FREE_AGENT
    )
    expiring = tuple(
        snapshot.team_by_id(team_id).player_by_id(player_id)
        for team_id, player_ids in expiring_by_team.items()
        for player_id in player_ids
    )
    return already_fa + expiring


def _ensure_cap_settings(state: HarnessState, *, first_season: int, tuning: GmTuning) -> None:
    """Ensures `state.cap_settings_by_season[state.season + 1]` exists,
    projecting it from the run's one real anchor row (`state.cap_settings_
    by_season[first_season]`, generation's own real `seeds/cap_settings.
    json` row, never overwritten) via `capsheet.project_cap_settings` --
    always from that single anchor, never chained off the latest already-
    projected row, so a season N figure is bit-identical whether it was
    projected directly or reached by walking every intermediate season
    (the two are mathematically equivalent modulo per-field `round()`
    accumulation order, and projecting from the one fixed anchor is what
    `generate_gm_league` itself already does for the initial horizon)."""
    target_season = state.season + 1
    if target_season in state.cap_settings_by_season:
        return
    anchor = state.cap_settings_by_season[first_season]
    state.cap_settings_by_season[target_season] = capsheet.project_cap_settings(
        anchor, target_season, tuning=tuning
    )


# ---------------------------------------------------------------------------
# Games (report §5.4) -- reuses tools/validate_sim.py's schedule/context/sim
# path wholesale; only the LeagueData assembly (PlayerTendencies/TeamStrategy
# on demand, from the current HarnessState) is new.
# ---------------------------------------------------------------------------


def _build_league_data_for_games(state: HarnessState, *, seed: int) -> LeagueData:
    snapshot = build_snapshot(state)
    teams = tuple(sorted(state.teams.values(), key=lambda t: t.id))
    players_by_team: dict[int, tuple] = {}
    ratings_by_player: dict[int, object] = {}
    tendencies_by_player: dict[int, PlayerTendencies] = {}
    all_players = []

    for ts in snapshot.teams:
        players_by_team[ts.id] = tuple(rp.player for rp in ts.roster)
        for rp in ts.roster:
            ratings_by_player[rp.id] = rp.ratings
            # Judgment call 4 (module docstring): salted on (season,
            # player_id), not the generation-time (team_index, slot_index)
            # validate_sim.py itself uses -- that salt is meaningless once
            # a player has been traded/signed/drafted onto a different
            # team/slot than the one they were generated into.
            tendencies_rng = rng_for(seed, "gm_harness", "tendencies", state.season, rp.id)
            tendencies_values = derive_tendencies(
                rp.ratings, rp.player.primary_position, rng=tendencies_rng
            )
            tendencies_by_player[rp.id] = PlayerTendencies(
                player_id=rp.id,
                league_id=state.league_id,
                updated_at=_dummy_created_at(state.season),
                **tendencies_values,
            )
        all_players.extend(players_by_team[ts.id])

    strategy_by_team = _build_team_strategies(
        teams,
        all_players,
        ratings_by_player,
        tendencies_by_player,
        league_id=state.league_id,
        season=state.season,
    )

    return LeagueData(
        league_id=state.league_id,
        season=state.season,
        teams=teams,
        players_by_team=players_by_team,
        ratings_by_player=ratings_by_player,
        tendencies_by_player=tendencies_by_player,
        strategy_by_team=strategy_by_team,
    )


def _run_games(state: HarnessState, *, seed: int) -> dict[int, int]:
    """Sims the full schedule once, on the current (post-trade-deadline)
    rosters, via `validate_sim.py`'s own `build_schedule`/`build_context`/
    `simulate_game` -- no second game-sim path (report §5.4's own
    constraint). Returns win totals, keyed by team id; a tie is
    unreachable (`GameResult` always resolves through overtime to a real
    winner -- `sim/game.py`'s own contract, not re-verified here)."""
    league_data = _build_league_data_for_games(state, seed=seed)
    teams = sorted(state.teams.values(), key=lambda t: t.id)
    schedule = _build_game_schedule(teams, league_seed=seed, season=state.season)
    tuning_profile = TuningProfile()

    win_totals: dict[int, int] = dict.fromkeys(state.teams, 0)
    for game_spec in schedule:
        context = build_context(league_data, game_spec, tuning_profile)
        result = simulate_game(context, rng_seed=game_spec.rng_seed)
        if result.home_score > result.away_score:
            win_totals[result.home_team_id] += 1
        else:
            win_totals[result.away_team_id] += 1
    return win_totals


# ---------------------------------------------------------------------------
# run_season / run_seed
# ---------------------------------------------------------------------------


def run_season(
    state: HarnessState,
    *,
    season: int,
    seed: int,
    skip_games: bool,
    tuning: GmTuning = DEFAULT_TUNING,
    first_season: int,
    max_negotiation_rounds: int = MAX_NEGOTIATION_ROUNDS,
    head_to_head_enabled: bool = True,
    agency_enabled: bool = True,
    media_enabled: bool = True,
    phrase_library: LoadedPhraseLibrary | None = None,
) -> SeasonRecord:
    """Runs one season's full 9-step phase sequence against `state`,
    mutating it in place, and returns a `SeasonRecord` of everything that
    happened -- see module docstring for the exact step order and every
    judgment call. `season`/`seed` must equal `state.season`/`state.
    rng_seed` (a caller-contract check, mirroring `load_gm_inputs`'s own
    "season must equal league.current_season" guard) -- `state` is the one
    source of truth this function actually advances; the explicit
    parameters exist so a caller's own intent is checked against it, not so
    a caller can point this function at a season it disagrees with.

    **Why the season-boundary progression call at the very end reads
    `season=state.season + 1`, never `state.season` (report §5.3, restated
    here at the one call site that actually matters):** `progress_one_
    season` expires a contract when `end_season < season`. Called with
    `state.season + 1`, it expires exactly the contracts whose last year
    was `state.season` -- **after** `RESIGN_WINDOW`/`FREE_AGENCY` already
    had their one chance this season to renew them. Any earlier call would
    make `_expiring_by_team` return empty for every subsequent season's
    `RESIGN_WINDOW`, since the expiring player would already be off his
    roster before that phase ever runs -- silently turning `run_free_
    agency`'s entire round-0 pending-offer merge into dead code. Do **not**
    "fix" this into releasing an unrenewed expiring player mid-season
    instead of at the boundary -- his contract genuinely covers the season
    that just played out; §5.3 is explicit that this is correct as
    designed, not a bug.

    **Decision P3-48 (§11 step 17).** `head_to_head_enabled` is forwarded,
    unmodified, to **every** `build_ctx` call this function makes -- not
    only the two trade-window ones -- because suppressing memory for only
    the call sites someone happened to think of would leave "the
    subsystem is off for this run" false for every other phase. It does
    **not** touch `state.head_to_head.update(...)` (the write path, after
    each trade window) or the season-boundary `memory.decay_head_to_head_
    rows` call below -- both stay completely unconditional in every run,
    suppressed or not; this flag only ever changes what `build_ctx` hands
    back to a reader.

    **Decision P4-2 (Phase 4 §11 Stage A step 1), as amended by Stage B
    step 10.** `agency_enabled`/`media_enabled` are forwarded, unmodified,
    to **every** `build_ctx` call this function makes, the identical
    "every call site, not just the ones someone happened to think of"
    shape `head_to_head_enabled` already establishes above. Both default
    `True` so every existing call site stays byte-identical.
    **`media_enabled` is still a genuine no-op** -- `GmContext` carries no
    media field for `build_ctx` to suppress, because Stage D has not
    built the media engine yet. **`agency_enabled` is no longer a no-op**:
    this function's own `_bootstrap_motivation_inmem`/
    `_run_agency_checkpoint_inmem` calls, below, gate directly on it --
    `False` genuinely suppresses trait generation and the checkpoint, not
    at `build_ctx`'s construction site (`GmContext` still carries no
    agency field, and nothing added one this stage), but at the two real
    call sites Stage B's mechanism actually has. `agency_enabled=False`
    is Decision P4-35's control-equivalence baseline, and the digest
    byte-identity check that decision names depends on this flag
    genuinely doing something.

    `phrase_library` (§11 step 15) -- `None` (the default) forwarded,
    unmodified, to **both** `run_trade_window` calls this function makes
    (the only two call sites that read it -- unlike `head_to_head_
    enabled`, dialogue is not a `build_ctx` concern, so no other phase
    call needs it), exactly the same plain-optional-parameter shape
    Decision P3-48 established one layer down: every existing caller that
    omits it sees byte-identical behavior (Decision P3-20)."""
    if season != state.season:
        raise ValueError(f"run_season called with season={season} but state.season={state.season}")
    if seed != state.rng_seed:
        raise ValueError(f"run_season called with seed={seed} but state.rng_seed={state.rng_seed}")

    phase_timings: dict[str, float] = {}
    jsonl_lines: list[str] = []
    boundaries: list[BoundaryRecord] = []
    conservation_violations: list[str] = []
    reconstruction_violations: list[str] = []
    acquisitions: list[Acquisition] = []
    seq = 0

    def _emit(records: Sequence, phase: Phase) -> None:
        nonlocal seq
        for record in records:
            if not record.reconstructs():
                reconstruction_violations.append(
                    f"phase={phase.value} seq={seq} subject={record.subject} "
                    f"error={record.reconstruction_error():.9f}"
                )
            jsonl_lines.append(
                encode_decision_record(
                    record, league_id=state.league_id, season=state.season, phase=phase, seq=seq
                )
            )
            seq += 1

    # Decision P4-35 (Stage C step 14) -- one `{RequestState.value: count}`
    # snapshot per checkpoint this season, appended to by each of the two
    # `_run_agency_checkpoint_inmem` call sites below (never populated at
    # all when `agency_enabled=False`, matching `SeasonRecord.agency_
    # request_state_counts`'s own "mechanism absent reads like an old
    # pickle" contract).
    agency_request_state_counts: list[Mapping[str, int]] = []

    # Decision P4-6/P4-2 (Phase 4 §11 Stage B step 10) -- bootstrap
    # motivation for any player `state.motivation` doesn't have an entry
    # for yet (a freshly generated league's whole roster, the first time
    # this runs). Gated on `agency_enabled` -- with the flag off, no
    # mechanism exists for this season at all, matching P4-2's "suppress
    # at context-construction time" principle extended to this harness-
    # only entry point.
    if agency_enabled:
        _bootstrap_motivation_inmem(state)

    # -- Step 1: PRESEASON ---------------------------------------------
    t0 = time.monotonic()
    snapshot = build_snapshot(state)
    ctx = build_ctx(
        state,
        phase=Phase.PRESEASON,
        tuning=tuning,
        head_to_head_enabled=head_to_head_enabled,
        agency_enabled=agency_enabled,
        media_enabled=media_enabled,
    )
    window_pass_1 = classify_all_windows(
        snapshot,
        previous_by_team=state.window_by_team,
        philosophy_by_team=ctx.philosophy_by_team,
        tuning=tuning,
    )
    apply_windows_inmem(state, window_pass_1)
    _emit(window_pass_1.records, Phase.PRESEASON)
    boundaries.append(
        _build_boundary_record(state, phase=Phase.PRESEASON, label="PRESEASON", tuning=tuning)
    )
    phase_timings["PRESEASON"] = time.monotonic() - t0

    # -- Step 2: REGULAR_SEASON -- trade window #1 -----------------------
    t0 = time.monotonic()
    snapshot = build_snapshot(state)
    ctx = build_ctx(
        state,
        phase=Phase.REGULAR_SEASON,
        tuning=tuning,
        head_to_head_enabled=head_to_head_enabled,
        agency_enabled=agency_enabled,
        media_enabled=media_enabled,
    )
    trade_result_1: TradeWindowResult = run_trade_window(
        snapshot, ctx, max_negotiation_rounds=max_negotiation_rounds, phrase_library=phrase_library
    )
    # Decision P3-22/P3-24 (§11 step 11) -- merge this window's updated
    # head-to-head rows straight into the harness's own persistent store.
    # `trade_result_1.head_to_head` is already the FULL updated row per
    # touched pair (`ctx.head_to_head`'s pre-window value plus this
    # window's own deltas), never a bare delta, so a plain `dict.update`
    # is the correct merge -- no read-modify-write needed here.
    state.head_to_head.update(trade_result_1.head_to_head)
    for execution in trade_result_1.executions:
        before = build_snapshot(state)
        apply_trade_inmem(state, execution)
        after = build_snapshot(state)
        conservation_violations.extend(check_trade_conservation(before, after, execution))
        acquisitions.extend(_trade_acquisitions(state, execution))
    _emit(trade_result_1.records, Phase.REGULAR_SEASON)
    boundaries.append(
        _build_boundary_record(
            state, phase=Phase.REGULAR_SEASON, label="post_trade_window_1", tuning=tuning
        )
    )
    phase_timings["REGULAR_SEASON"] = time.monotonic() - t0
    mutual_accepts_1 = _count_mutual_accepts(trade_result_1.records)

    # -- Step 3: TRADE_DEADLINE -- classify+apply, then trade window #2 -
    t0 = time.monotonic()
    snapshot = build_snapshot(state)
    ctx = build_ctx(
        state,
        phase=Phase.TRADE_DEADLINE,
        tuning=tuning,
        head_to_head_enabled=head_to_head_enabled,
        agency_enabled=agency_enabled,
        media_enabled=media_enabled,
    )
    window_pass_2 = classify_all_windows(
        snapshot,
        previous_by_team=state.window_by_team,
        philosophy_by_team=ctx.philosophy_by_team,
        tuning=tuning,
    )
    apply_windows_inmem(state, window_pass_2)
    _emit(window_pass_2.records, Phase.TRADE_DEADLINE)

    snapshot = build_snapshot(state)
    ctx = build_ctx(
        state,
        phase=Phase.TRADE_DEADLINE,
        tuning=tuning,
        head_to_head_enabled=head_to_head_enabled,
        agency_enabled=agency_enabled,
        media_enabled=media_enabled,
    )
    # The round-23 CONTEND fix, reproduced (a no-op here -- see module
    # docstring's own step-3 summary): `apply_windows_inmem` never loses
    # CONTEND the way `apply_windows`'s Timeline round-trip does, so `ctx.
    # window_by_team` above is already `window_pass_2.window_by_team`'s own
    # values. Overridden anyway, so the two code paths stay behaviourally
    # identical rather than one silently relying on an in-memory-only
    # coincidence.
    ctx = dataclasses.replace(ctx, window_by_team=dict(window_pass_2.window_by_team))
    trade_result_2: TradeWindowResult = run_trade_window(
        snapshot, ctx, max_negotiation_rounds=max_negotiation_rounds, phrase_library=phrase_library
    )
    state.head_to_head.update(trade_result_2.head_to_head)
    for execution in trade_result_2.executions:
        before = build_snapshot(state)
        apply_trade_inmem(state, execution)
        after = build_snapshot(state)
        conservation_violations.extend(check_trade_conservation(before, after, execution))
        acquisitions.extend(_trade_acquisitions(state, execution))
    _emit(trade_result_2.records, Phase.TRADE_DEADLINE)
    boundaries.append(
        _build_boundary_record(
            state, phase=Phase.TRADE_DEADLINE, label="TRADE_DEADLINE", tuning=tuning
        )
    )
    phase_timings["TRADE_DEADLINE"] = time.monotonic() - t0
    mutual_accepts_2 = _count_mutual_accepts(trade_result_2.records)

    # Decision P4-11's first of two checkpoints a season (Phase 4 §11
    # Stage B step 10) -- the trade deadline, rebuilt AFTER both trade
    # windows' executions so every satisfaction read sees each player's
    # post-trade team. Write-only: nothing below reads this back into
    # `ctx`/`snapshot` this stage.
    if agency_enabled:
        checkpoint_snapshot = build_snapshot(state)
        _run_agency_checkpoint_inmem(state, checkpoint_snapshot, tuning=tuning)
        agency_request_state_counts.append(_request_state_counts(state))

    # -- Step 4: games (skippable) ---------------------------------------
    t0 = time.monotonic()
    win_totals: dict[int, int] | None = None
    if not skip_games:
        win_totals = _run_games(state, seed=seed)
    phase_timings["GAMES"] = time.monotonic() - t0

    # -- Step 5: DRAFT_LOTTERY -- classify+apply, then resolve pick numbers
    t0 = time.monotonic()
    snapshot = build_snapshot(state)
    ctx = build_ctx(
        state,
        phase=Phase.DRAFT_LOTTERY,
        tuning=tuning,
        head_to_head_enabled=head_to_head_enabled,
        agency_enabled=agency_enabled,
        media_enabled=media_enabled,
    )
    window_pass_3 = classify_all_windows(
        snapshot,
        previous_by_team=state.window_by_team,
        philosophy_by_team=ctx.philosophy_by_team,
        tuning=tuning,
    )
    apply_windows_inmem(state, window_pass_3)
    _emit(window_pass_3.records, Phase.DRAFT_LOTTERY)
    # window_score is a pure function of (league_snapshot, tuning) alone,
    # independent of the .window field just classified above -- the
    # pre-classification snapshot is a valid, cheaper basis for it (no
    # rebuild needed).
    _resolve_pick_numbers(state, snapshot=snapshot, tuning=tuning)
    phase_timings["DRAFT_LOTTERY"] = time.monotonic() - t0

    # -- Step 6: DRAFT ----------------------------------------------------
    t0 = time.monotonic()
    raw_prospects = generate_draft_class(
        season=state.season, seed=seed, league_id=state.league_id, tuning=tuning
    )
    prospects = _rekey_prospects(raw_prospects, season=state.season, first_season=first_season)
    state.prospects.update({rp.id: rp for rp in prospects})

    this_season_picks = [p for p in state.picks.values() if p.season == state.season]
    draft_order = tuple(_eligible_draft_picks_sorted(this_season_picks))
    snapshot = build_snapshot(state)
    ctx = build_ctx(
        state,
        phase=Phase.DRAFT,
        tuning=tuning,
        head_to_head_enabled=head_to_head_enabled,
        agency_enabled=agency_enabled,
        media_enabled=media_enabled,
    )
    draft_result = run_draft_phase(snapshot, ctx, prospects=prospects, draft_order=draft_order)

    pick_by_number = {p.pick_number: p for p in draft_order}
    for pick_result in draft_result.picks:
        pick = pick_by_number[pick_result.pick_number]
        apply_draft_inmem(state, pick, pick_result)
        acquisitions.append(
            _acquisition(
                state,
                team_id=pick_result.team_id,
                player_id=pick_result.prospect_id,
                channel="draft",
            )
        )
    _emit(tuple(r.record for r in draft_result.picks), Phase.DRAFT)
    boundaries.append(
        _build_boundary_record(state, phase=Phase.DRAFT, label="post_draft", tuning=tuning)
    )
    phase_timings["DRAFT"] = time.monotonic() - t0

    # Decision P4-6 -- every drafted rookie needs a motivation entry
    # before the offseason checkpoint (below) tries to read one.
    if agency_enabled:
        _bootstrap_motivation_inmem(state)

    # -- Step 7: RESIGN_WINDOW -- pending offers only, nothing applied ---
    t0 = time.monotonic()
    snapshot = build_snapshot(state)
    ctx = build_ctx(
        state,
        phase=Phase.RESIGN_WINDOW,
        tuning=tuning,
        head_to_head_enabled=head_to_head_enabled,
        agency_enabled=agency_enabled,
        media_enabled=media_enabled,
    )
    expiring_by_team = _expiring_by_team(snapshot, state.season)
    run_resign_window(snapshot, ctx, expiring_by_team=expiring_by_team)
    phase_timings["RESIGN_WINDOW"] = time.monotonic() - t0
    # No jsonl emission -- run_resign_window's own return type discards the
    # DecisionRecord it built internally (gm_service.py's own documented
    # reasoning, matching run_phase_hook's RESIGN_WINDOW branch verbatim).

    boundaries.append(
        _build_boundary_record(
            state, phase=Phase.FREE_AGENCY, label="free_agency_open", tuning=tuning
        )
    )

    # -- Step 8: FREE_AGENCY -----------------------------------------------
    t0 = time.monotonic()
    snapshot = build_snapshot(state)
    ctx = build_ctx(
        state,
        phase=Phase.FREE_AGENCY,
        tuning=tuning,
        head_to_head_enabled=head_to_head_enabled,
        agency_enabled=agency_enabled,
        media_enabled=media_enabled,
    )
    expiring_by_team = _expiring_by_team(snapshot, state.season)
    pending_offers = run_resign_window(snapshot, ctx, expiring_by_team=expiring_by_team)
    pool = _free_agency_pool_inmem(state, snapshot, expiring_by_team)
    free_agency_result: FreeAgencyResult = run_free_agency(
        snapshot, ctx, pool=pool, pending_offers=pending_offers
    )
    for signing in free_agency_result.signings:
        apply_signing_inmem(state, signing)
        acquisitions.append(
            _acquisition(
                state, team_id=signing.team_id, player_id=signing.player_id, channel="signing"
            )
        )
    _emit(free_agency_result.records, Phase.FREE_AGENCY)
    phase_timings["FREE_AGENCY"] = time.monotonic() - t0

    # season_end -- the same instant §5.3 separately calls "post-FREE_
    # AGENCY" (nothing runs between FREE_AGENCY finishing and the boundary
    # below) -- recorded once, under this one label (module docstring).
    snapshot = build_snapshot(state)

    # Decision P4-11's second of two checkpoints a season (Phase 4 §11
    # Stage B step 10) -- the offseason checkpoint, at the same instant
    # this module already calls "season_end." Write-only, same as the
    # trade-deadline checkpoint above.
    if agency_enabled:
        _run_agency_checkpoint_inmem(state, snapshot, tuning=tuning)
        agency_request_state_counts.append(_request_state_counts(state))

    minutes_weighted_age_by_team = {
        ts.id: window.mean_age(ts, state.season) for ts in snapshot.teams
    }
    def_group_mean_by_player = {
        rp.id: _def_group_mean(rp) for ts in snapshot.teams for rp in ts.roster
    }
    future_first_net_by_team = _future_first_net_by_team(state)
    boundaries.append(
        _build_boundary_record(state, phase=Phase.FREE_AGENCY, label="season_end", tuning=tuning)
    )

    # -- Step 8b: WAIVERS -- T7a2 (architect round 8 §7.3). Closes the real
    # FREE_AGENCY -> WAIVERS -> PRESEASON graph edge (phase/graph.py:38-39)
    # -- placed here, right after "season_end"'s own snapshot and
    # immediately before the season-boundary progression call below, per
    # that report's own placement instruction ("immediately after step 8
    # (FREE_AGENCY), before step 9 (the season boundary)"). Deliberately
    # AFTER "season_end", not before: that checkpoint's own metrics (mean
    # age, def-group mean, future-first net) are meant to read the roster
    # free agency actually built, not the post-cut roster -- T7b's own
    # §9.2 band-file correction keeps "post_waivers" a separate measured
    # point from "season_end" for exactly this reason (module docstring).
    t0 = time.monotonic()
    snapshot = build_snapshot(state)
    ctx = build_ctx(
        state,
        phase=Phase.WAIVERS,
        tuning=tuning,
        head_to_head_enabled=head_to_head_enabled,
        agency_enabled=agency_enabled,
        media_enabled=media_enabled,
    )
    waiver_result: WaiverWindowResult = run_waivers(snapshot, ctx)
    for execution in waiver_result.executions:
        apply_waiver_inmem(state, execution)
    _emit(waiver_result.records, Phase.WAIVERS)
    boundaries.append(
        _build_boundary_record(state, phase=Phase.WAIVERS, label="post_waivers", tuning=tuning)
    )
    phase_timings["WAIVERS"] = time.monotonic() - t0

    # -- Step 9: season boundary ------------------------------------------
    t0 = time.monotonic()
    progressed = progress_one_season(
        tuple(state.players.values()), season=state.season + 1, seed=seed, tuning=tuning
    )
    state.players = {rp.id: rp for rp in progressed}
    state.season = state.season + 1
    _ensure_cap_settings(state, first_season=first_season, tuning=tuning)
    # Decision P3-25 (§11 step 11) -- every gm_head_to_head counter (this
    # harness's own in-memory rows) decays by H2H_DECAY at the season
    # boundary, mirroring `_gm_progression_stub.progress_one_season`'s own
    # placement immediately above: both are per-season state transitions
    # applied once, right here, never mid-season. `season=state.season`
    # is read AFTER the `state.season = state.season + 1` rollover two
    # lines up, so every row's own `updated_season` is restamped to the
    # season the harness just rolled INTO (NIT 8, round 13 correction) --
    # not the season that just ended.
    state.head_to_head = memory.decay_head_to_head_rows(state.head_to_head, season=state.season)
    # Decision P4-63 -- the tenure mirror's other half of `_acquisition`'s
    # reset-to-0: every player still rostered (not a free agent/prospect)
    # gets one more full season credited. Read AFTER `state.players` was
    # replaced by `progressed` above, same "season the harness just
    # rolled INTO" timing NIT 8 already established for head-to-head decay.
    for rp in state.players.values():
        if rp.player.team_id is not None:
            state.tenure_seasons_by_player[rp.id] = state.tenure_seasons_by_player.get(rp.id, 0) + 1
    phase_timings["SEASON_BOUNDARY"] = time.monotonic() - t0

    return SeasonRecord(
        season=season,
        boundaries=tuple(boundaries),
        trades_by_window=(trade_result_1.executions, trade_result_2.executions),
        packages_evaluated=(trade_result_1.packages_evaluated, trade_result_2.packages_evaluated),
        mutual_accepts=(mutual_accepts_1, mutual_accepts_2),
        turns_evaluated=(trade_result_1.turns_evaluated, trade_result_2.turns_evaluated),
        evaluations_performed=(
            trade_result_1.evaluations_performed,
            trade_result_2.evaluations_performed,
        ),
        counter_opportunities=(
            trade_result_1.counter_opportunities,
            trade_result_2.counter_opportunities,
        ),
        counters_admitted=(trade_result_1.counters_admitted, trade_result_2.counters_admitted),
        counter_closed_executions=(
            trade_result_1.counter_closed_executions,
            trade_result_2.counter_closed_executions,
        ),
        outcome_census=(trade_result_1.outcome_census, trade_result_2.outcome_census),
        expired_stalled_count=(
            trade_result_1.expired_stalled_count,
            trade_result_2.expired_stalled_count,
        ),
        negotiation_depth_violations=(
            trade_result_1.negotiation_depth_violations,
            trade_result_2.negotiation_depth_violations,
        ),
        channel3_decision_sites=(
            trade_result_1.channel3_decision_sites,
            trade_result_2.channel3_decision_sites,
        ),
        channel3_reachability=(
            trade_result_1.channel3_reachability,
            trade_result_2.channel3_reachability,
        ),
        channel3_flips=(
            trade_result_1.channel3_flips,
            trade_result_2.channel3_flips,
        ),
        channel3_abs_shade_sum=(
            trade_result_1.channel3_abs_shade_sum,
            trade_result_2.channel3_abs_shade_sum,
        ),
        channel3_abs_shade_row_count=(
            trade_result_1.channel3_abs_shade_row_count,
            trade_result_2.channel3_abs_shade_row_count,
        ),
        channel2_decision_sites=(
            trade_result_1.channel2_decision_sites,
            trade_result_2.channel2_decision_sites,
        ),
        channel2_reachability=(
            trade_result_1.channel2_reachability,
            trade_result_2.channel2_reachability,
        ),
        channel2_flips=(
            trade_result_1.channel2_flips,
            trade_result_2.channel2_flips,
        ),
        channel1_up_decision_sites=(
            trade_result_1.channel1_up_decision_sites,
            trade_result_2.channel1_up_decision_sites,
        ),
        channel1_up_reachability=(
            trade_result_1.channel1_up_reachability,
            trade_result_2.channel1_up_reachability,
        ),
        channel1_up_flips=(
            trade_result_1.channel1_up_flips,
            trade_result_2.channel1_up_flips,
        ),
        channel1_down_decision_sites=(
            trade_result_1.channel1_down_decision_sites,
            trade_result_2.channel1_down_decision_sites,
        ),
        channel1_down_reachability=(
            trade_result_1.channel1_down_reachability,
            trade_result_2.channel1_down_reachability,
        ),
        channel1_down_flips=(
            trade_result_1.channel1_down_flips,
            trade_result_2.channel1_down_flips,
        ),
        channel3_max_observed=(
            trade_result_1.channel3_max_observed,
            trade_result_2.channel3_max_observed,
        ),
        channel2_max_observed=(
            trade_result_1.channel2_max_observed,
            trade_result_2.channel2_max_observed,
        ),
        channel1_up_max_observed=(
            trade_result_1.channel1_up_max_observed,
            trade_result_2.channel1_up_max_observed,
        ),
        channel1_down_max_observed=(
            trade_result_1.channel1_down_max_observed,
            trade_result_2.channel1_down_max_observed,
        ),
        far_rejects_observed=(
            trade_result_1.far_rejects_observed,
            trade_result_2.far_rejects_observed,
        ),
        phrase_selections_total=(
            trade_result_1.phrase_selections_total,
            trade_result_2.phrase_selections_total,
        ),
        phrase_fallback_selections=(
            trade_result_1.phrase_fallback_selections,
            trade_result_2.phrase_fallback_selections,
        ),
        phrase_selections_by_situation=(
            trade_result_1.phrase_selections_by_situation,
            trade_result_2.phrase_selections_by_situation,
        ),
        phrase_fallback_by_situation=(
            trade_result_1.phrase_fallback_by_situation,
            trade_result_2.phrase_fallback_by_situation,
        ),
        phrase_selections_by_philosophy_template=(
            trade_result_1.phrase_selections_by_philosophy_template,
            trade_result_2.phrase_selections_by_philosophy_template,
        ),
        agency_request_state_counts=tuple(agency_request_state_counts),
        agency_cumulative_transition_count_by_player=tuple(state.transition_count_by_player.items()),
        signings=free_agency_result.signings,
        draft_picks=draft_result.picks,
        waivers=waiver_result.executions,
        acquisitions=tuple(acquisitions),
        minutes_weighted_age_by_team=minutes_weighted_age_by_team,
        def_group_mean_by_player=def_group_mean_by_player,
        future_first_net_by_team=future_first_net_by_team,
        win_totals=win_totals,
        jsonl_lines=tuple(jsonl_lines),
        conservation_violations=tuple(conservation_violations),
        reconstruction_violations=tuple(reconstruction_violations),
        phase_timings=phase_timings,
    )


def run_seed(
    *,
    league_id: int,
    rng_seed: int,
    first_season: int,
    seasons: int,
    players_per_team: int,
    skip_games: bool,
    tuning: GmTuning = DEFAULT_TUNING,
    max_negotiation_rounds: int = MAX_NEGOTIATION_ROUNDS,
    head_to_head_enabled: bool = True,
    agency_enabled: bool = True,
    media_enabled: bool = True,
    phrase_library: LoadedPhraseLibrary | None = None,
) -> RunRecord:
    """Generates one league (`generate_gm_league`) and runs `seasons`
    consecutive `run_season` calls against it, returning one `RunRecord`.
    The only public entry point a `metrics.py`/`validate_gm.py` caller (T7b,
    not built this slice) needs -- everything else in this module is either
    a helper or `run_season` itself.

    **Decision P3-48 (§11 step 17).** `head_to_head_enabled` is forwarded,
    unmodified, to every `run_season` call this function makes -- this is
    the signature every external caller (`tools/generate_gm_decision_log_
    digest.py`, `tools/validate_gm.py`'s control-check block, and any test
    calling `run_seed` directly) actually sees. Defaulted `True` so every
    existing call site is byte-identical to today; see `state.py::build_
    ctx`'s own docstring for what the flag does and does not affect.

    **Decision P4-2 (Phase 4 §11 Stage A step 1), as amended by Stage B
    step 10.** `agency_enabled`/`media_enabled` are forwarded, unmodified,
    to every `run_season` call this function makes -- the same "this is
    the signature every external caller actually sees" shape as `head_to_
    head_enabled` above. Both default `True`. **`media_enabled` is still
    a genuine no-op** (Stage D has not built the media engine). **`agency_
    enabled` is a real mechanism switch as of Stage B** -- `False`
    suppresses the trait bootstrap and the satisfaction/morale/trust/
    ladder checkpoint on every call this function makes, per `run_season`'s
    own docstring above. Both exist so `tools/generate_gm_decision_log_
    digest.py::_run_configured_seed` can pin them explicitly `False`
    (Decision P4-55) the same way it already pins `head_to_head_enabled=
    False` for Decision P3-48 -- for `agency_enabled` that pin is now a
    real control run, not a documentation placeholder.

    `phrase_library` (§11 step 15) -- `None` (the default) forwarded,
    unmodified, to every `run_season` call this function makes, the
    identical plain-optional-parameter shape as `head_to_head_enabled`
    one step up: every existing call site that omits it stays byte-
    identical (Decision P3-20)."""
    state = generate_gm_league(
        league_id=league_id,
        season=first_season,
        rng_seed=rng_seed,
        players_per_team=players_per_team,
        tuning=tuning,
    )
    philosophy_by_team = {t.id: t.gm_philosophy for t in state.teams.values()}
    season1_top1_minus_8th_by_team = _top1_minus_8th_by_team(build_snapshot(state))

    season_records: list[SeasonRecord] = []
    for i in range(seasons):
        season = first_season + i
        season_records.append(
            run_season(
                state,
                season=season,
                seed=rng_seed,
                skip_games=skip_games,
                tuning=tuning,
                first_season=first_season,
                max_negotiation_rounds=max_negotiation_rounds,
                head_to_head_enabled=head_to_head_enabled,
                agency_enabled=agency_enabled,
                media_enabled=media_enabled,
                phrase_library=phrase_library,
            )
        )

    return RunRecord(
        rng_seed=rng_seed,
        philosophy_by_team=philosophy_by_team,
        season1_top1_minus_8th_by_team=season1_top1_minus_8th_by_team,
        seasons=tuple(season_records),
    )


# ---------------------------------------------------------------------------
# --dump-record smoke path (T7a's own testability line: "1 seed x 1 season x
# skip-games completes, record has the documented shape"). Not tools/
# validate_gm.py -- that CLI, with GO/NO-GO evaluation against seeds/gm_
# validation_bands.json, is T7b's job.
# ---------------------------------------------------------------------------


def _dump_record_json(record: RunRecord) -> str:
    return json.dumps(dataclasses.asdict(record), indent=2, default=str)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="T7a smoke path: run_seed(...) and dump the resulting "
        "RunRecord's shape as JSON, so a human can eyeball it end to end "
        "before T7b's metrics layer exists."
    )
    parser.add_argument("--league-id", type=int, default=1)
    parser.add_argument("--rng-seed", type=int, default=1)
    parser.add_argument("--first-season", type=int, default=2026)
    parser.add_argument("--seasons", type=int, default=1)
    parser.add_argument("--players-per-team", type=int, default=15)
    parser.add_argument(
        "--with-games",
        action="store_true",
        help="Run the full game schedule too (default: skip-games -- the smoke path's own point).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    record = run_seed(
        league_id=args.league_id,
        rng_seed=args.rng_seed,
        first_season=args.first_season,
        seasons=args.seasons,
        players_per_team=args.players_per_team,
        skip_games=not args.with_games,
        tuning=DEFAULT_TUNING,
    )
    print(_dump_record_json(record))


if __name__ == "__main__":
    main()
