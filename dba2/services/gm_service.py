"""AI GM decision orchestration -- the pure plan layer (slice T3) plus the
async/DB-backed I/O apply layer on top of it (slice T4), both docs/design/
phase-2-manager-minds.md, Phase 2 step 10.

**Two layers, one file, deliberately.** The plan layer (everything above
`load_gm_inputs`, below) takes a `LeagueSnapshot` plus already-loaded
context (`GmContext`) and returns *descriptions* of mutations (ids,
salaries, records) -- it persists and executes nothing, and every one of
its functions is checked to have zero I/O: no `ex`/`Executor` parameter, no
`await`, no `dba2.data` import, deterministic given its inputs. The I/O
layer (`run_phase_hook`, `load_gm_inputs`, and the five appliers --
`apply_trade`, `apply_signing`, `apply_draft`, `apply_windows`,
`apply_waivers` (T7a2)) turns those descriptions into real Postgres
writes. This split is what lets both a real DB-backed service (this
module) and an in-memory validation harness (T6-T7, built across several
later dispatches) call the exact same decision logic under two different
appliers -- the same split `capsheet_service.py`/`managers.
capsheet` and `free_agency_service.py`/`managers.decisions.signing` already
establish, except here both halves share one file: `services/` (unlike
`managers/`) is not barred from `dba2.data` in general, and `free_agency_
service.py::rank_offers` already sets the precedent for a genuinely pure
function living in a `services/` module alongside async I/O functions.

**Nit 8, deferred (critic round 17).** `from dba2.services.free_agency_
service import rank_offers` above still transitively imports that whole
module -- Python executes a module's top-level code (including its
`import dba2.data.db`/`team_repo`) the first time *any* name is imported
from it, regardless of which name a caller asks for. `rank_offers` itself
is pure, but it is defined in the same file as `construct_offer_for_team`
(the async, `Executor`-taking half), so there is no import spelling that
reaches `rank_offers` without also pulling in `asyncpg` -- unlike `run_
free_agency`/`run_resign_window` above, which genuinely need nothing else
from that module. This will matter for the in-memory validation harness
(T6/T7), which wants this module importable with zero I/O dependencies
at all. The real fix is moving `rank_offers` into a pure module (most
naturally `managers/decisions/signing.py`, alongside `construct_offer`,
which already has no I/O) and updating `free_agency_service.py` to import
it from there -- a genuine refactor across two files' own public surface,
out of this fix-only dispatch's scope. Flagged here rather than silently
left, per critic's own instruction.

**Two judgment calls, both about which season's `LeagueCapSettings`/`room`
a formula should read, flagged because the design doc's own text never
resolves them for a caller holding both a current- and a next-season row at
once (`GmContext` carries both):**

1. **`trade.fit_value_for`/`trade.evaluate_trade`'s `cap_settings`/`season`
   params always read `ctx.cap_settings`/`ctx.season` (current), never the
   `next_season_*` pair** -- this mirrors `trade.py`'s own internal split
   exactly: `evaluate_trade` itself takes `cap_settings`/`season` (the
   valuation basis, "today's market") *and*, separately,
   `next_season_cap_settings`/`next_season_room_dollars`/`next_season` (the
   legality-gate basis, "next season's books") as five distinct parameters
   -- there is no ambiguity to resolve here, `trade.py` already made this
   call and this module just passes the right ctx field to the right
   parameter.
2. **`signing.construct_offer`'s single `cap_settings`/`season`/`room`
   triple reads `ctx.next_season_cap_settings`/`next_season = ctx.season +
   1`/`ctx.room_by_team` (or the caller's own `room_remaining`) throughout
   `run_resign_window` and `run_free_agency`.** Unlike `evaluate_trade`,
   `construct_offer` (and the `legal_capacity`/`offer_salary` formulas it
   calls) has only *one* `cap_settings` param, shared between market pricing
   and the `legal_capacity`/`min_salary` dollar comparison against `room` --
   and `room` is explicitly the team's *next-season* projected figure
   (`GmContext.room_by_team`'s own docstring, mirroring `trade.py::cap_
   delta_bonus`'s `next_season_room_dollars` convention). A `room` dollar
   figure for season N+1 compared against a `min_salary`/`mle_amount` from
   season N would be internally inconsistent, so `cap_settings` here must be
   the same season `room` is -- `next_season_cap_settings`. `season` follows
   for the same reason `run_resign_window`'s own spec text already commits
   to (`ctx.season + 1` is the literal season named for "does not cover"
   expiry) -- a signed deal's first year is `next_season`, so age/market
   pricing for that deal reads as of `next_season` too.

**A third, smaller call: `SigningExecution.signing_method` tagging.** Not
named by the architect spec beyond "check `SigningMethod` for the right
value." Read as: whichever bucket -- cap room, the MLE, the bi-annual, or
the minimum exception -- actually covered this specific signing's salary,
checked room-first, then largest-unused-exception-first (`_finalize_
signing`, below); see the B1/B1b fix note for why this can no longer be a
sign-of-`room` guess.

**B1/B1b fix, third round (critic round 18).** Two prior fix attempts
(critic rounds 16 and 17) each re-checked affordability more often
against `signing.legal_capacity(room_remaining[team_id], ...)` -- but
`legal_capacity` is a correct, pure, *stateless* primitive: for any
`room <= 0` it always returns the same constant (the largest of the MLE/
bi-annual/min_salary amounts), with zero memory of whether a team already
used that exception earlier in the same free-agency pass. No amount of
re-checking against it can bound how many MLE-sized (or bi-annual-sized)
contracts one team signs across a pass -- critic's round-18 repro: 1 team,
$30M room, 10 max-worthy free agents, $156.9M committed, 9 of the 10
signings tagged MLE. The real fix needed state `legal_capacity` has no
business owning: `run_free_agency`'s own per-pass `exceptions_used`
dict (`{team_id: {"mle", "bi_annual"}}` -- whichever this team has
already spent this pass), read and mutated by two `gm_service`-local
functions, `_effective_capacity` (the real ceiling for a NEW offer right
now, given room plus whichever once-per-pass exceptions remain unused)
and `_finalize_signing` (which bucket actually covers an accepted
signing's salary, mutating `room_remaining`/`exceptions_used`
accordingly). Both offer construction and the accept-time affordability
check now read `_effective_capacity`, and the accept-time tag comes from
`_finalize_signing`'s own bucket choice -- so the accept decision and the
tag can never disagree (B1b), and the minimum exception (real NBA rules:
available to any team, unlimited, no once-per-season cap) is deliberately
left untracked and never decrements `room_remaining`.

**A fourth: `run_free_agency`'s "legal_capacity > 0" round-eligibility
gate is, by construction, always true.** `signing.legal_capacity` is
documented to never return less than `min_salary` for any input (its own
module docstring's "fix round" note) -- so every team with an open roster
slot is always eligible to make an offer, never excluded on cap grounds
alone. Implemented literally anyway (computed via `signing.legal_capacity`
and compared `> 0`), per the architect spec's explicit text, rather than
silently dropped as dead code -- if `legal_capacity`'s floor is ever
retuned to allow a true zero, this gate starts doing real work with no
change needed here.

**A fifth: `pending_offers` (an already-existing incumbent-team offer, from
`run_resign_window`) is merged into free agency's first round only**, for
whichever pool players it names -- `run_resign_window`'s own docstring is
explicit that Bird-rights/incumbent advantage is out of scope beyond
`rank_offers`'s existing window tie-break, so a pending offer competes on
identical footing with every fresh round-1 offer, never re-added in a later
round (an expired incumbent offer does not linger; a team that still wants
that player after round 1 must out-bid via a fresh offer like anyone else).

Zero I/O in every function built this dispatch: no `dba2.data` import, no
`ex`/`Executor` parameter, no `await`, no bare `random.Random()` (only the
deterministic `rng_for` derivation reached transitively through
`fit_value_for`/`construct_offer`), no `open()`, no `os.environ`,
no `datetime.now()`.
"""
from __future__ import annotations

import contextlib
import json
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from dba2.core.errors import ConcurrencyError, NotFoundError, ValidationError
from dba2.core.rng import rng_for
from dba2.data.db import Executor, transaction
from dba2.data.repositories import (
    contract_repo,
    draft_repo,
    league_repo,
    offer_repo,
    player_repo,
    ratings_repo,
    role_repo,
    team_repo,
    txn_repo,
)
from dba2.domain.contract import ContractStatus, ContractType, ContractYear, SigningMethod
from dba2.domain.draft import DraftPick
from dba2.domain.head_to_head import HeadToHead
from dba2.domain.league import LeagueCapSettings
from dba2.domain.motivation import PlayerMotivation
from dba2.domain.phase import Phase, TradeStatus
from dba2.domain.player import RosterStatus
from dba2.domain.team import GmPhilosophy, Team, Timeline, TimelineSource
from dba2.domain.transaction import TransactionAssetKind, TransactionType
from dba2.managers import capsheet, memory, negotiation, window
from dba2.managers.decisions import draft as draft_decisions
from dba2.managers.decisions import signing as signing_decisions
from dba2.managers.decisions import trade as trade_decisions
from dba2.managers.decisions import waivers as waivers_decisions
from dba2.managers.decisions.signing import SigningOffer, construct_offer
from dba2.managers.explain import DecisionRecord, ReasonTerm
from dba2.managers.needs import NeedMatrix, build_need_matrix
from dba2.managers.philosophy import PROFILES
from dba2.managers.picks import RookieScaleTable, rank_teams_by_projected_slot
from dba2.managers.scouting import player_age
from dba2.managers.snapshot import (
    AssetBundle,
    LeagueSnapshot,
    PlayerContract,
    RosterPlayer,
    TeamSnapshot,
    build_league_snapshot,
    build_roster_player,
    build_team_snapshot,
)
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow, GmTuning
from dba2.managers.voice.frame import SituationToken, derive_frame
from dba2.managers.voice.select import PhraseSelection, select_phrase
from dba2.phase import permissions
from dba2.phase.service import require_phase
from dba2.services import roster_lock, roster_service
from dba2.services.agency_service import (
    load_motivation_for_league,
    load_trade_request_pressure_for_league,
)
from dba2.services.capsheet_service import project_league_cap_sheets
from dba2.services.free_agency_service import rank_offers
from dba2.services.phrase_service import LoadedPhraseLibrary

# ---------------------------------------------------------------------------
# Context and mutation-description types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GmContext:
    """Every already-loaded fact one phase's worth of AI GM decisions needs,
    for the whole league at once -- the I/O layer's (T4's) one job is
    assembling this from repository reads; every function below reads only
    this plus a `LeagueSnapshot`. See module docstring for the `cap_
    settings` vs `next_season_cap_settings` split judgment call."""

    league_id: int
    season: int
    phase: Phase
    league_rng_seed: int
    cap_settings: LeagueCapSettings
    next_season_cap_settings: LeagueCapSettings
    rookie_scale: RookieScaleTable
    room_by_team: Mapping[int, int]
    """Next-season room dollars per team, computed with `include_holds=
    False` (`trade.py`'s own documented caller obligation)."""
    apron1_room_by_team: Mapping[int, int]
    """Next-season `apron_1 - used` per team (`SeasonCapProjection.
    apron1_room`, same `include_holds=False`/`season + 1` basis as `room_
    by_team`, from the same `project_team_cap_sheet`/`project_league_cap_
    sheets` pass) -- slice 12-A's (Decision P2-20, architect round 11
    §5.3/round 12 §5.4) sole new context field, the ceiling basis for the
    incumbent (Bird-rights) capacity bucket. `apron_1`, not `apron_2` --
    round 11 §5.3's own choice, kept because `apron_1` is where the real
    CBA's re-signing freedom actually stops and Decision P2-20 already
    treats both aprons as hard trade blocks, so using the softer apron
    here would invert that severity ordering."""
    philosophy_by_team: Mapping[int, GmPhilosophy]
    window_by_team: Mapping[int, CompetitiveWindow]
    tuning: GmTuning = DEFAULT_TUNING
    retirement_age: int | None = None
    """A player who will be strictly older than this at `next_season` is
    treated as not occupying a next-season roster slot (`_occupies_next_
    season`, below), on top of that predicate's existing contract-expiry
    check -- Decision P2-37, architect round 18 §4.7 Shape 1. `None` (the
    default) means "no retirement knowledge, behave exactly as before":
    every caller that does not pass this field is byte-identical to pre-
    P2-37 behaviour, matching this module's own established convention for
    optional context (`apron1_room_by_team`'s slice-12-A addition, above).
    This is a plain caller-supplied *value*, never an import of `tools/_gm_
    progression_stub.py::_RETIREMENT_AGE` -- that constant stays quarantined
    to the harness (`tests/unit/test_layering.py` still forbids `dba2/`
    from importing `tools/`); the in-memory harness fills this field from
    its own stub's constant (`tools/gm_harness/state.py::build_ctx`), the
    real DB path leaves it `None` until Phase 7's real progression system
    exists to source it from."""
    room_for_signings_by_team: Mapping[int, int] | None = None
    """Decision P2-39 (architect round 21 §4). The **same** next-season
    room figure as `room_by_team`, except computed with `include_holds=
    True` -- i.e. it accounts for a team's own outstanding cap holds
    (unsigned draft picks, Bird-rights placeholders for its own expiring
    free agents) as if they were real contracts. Read only by `run_free_
    agency`'s `room_remaining` seed (architect round 21 §4.3); every other
    room-basis field (`room_by_team`, `apron1_room_by_team`) and every
    other caller (`run_resign_window`, the whole trade-legality/valuation
    path) is explicitly untouched by this field's existence. `None` (the
    default) means "no hold-aware figure available, seed `room_remaining`
    from `room_by_team` instead" -- the same "unset means behave exactly
    as before" convention `retirement_age` already establishes, above.
    Production `load_gm_inputs` deliberately leaves this `None` (see that
    function's own docstring for why); the in-memory harness's `build_ctx`
    is the only populator today."""
    head_to_head: Mapping[tuple[int, int], HeadToHead] | None = None
    """Decision P3-24 -- every `(team_id, opponent_id)` row this league has
    ever recorded, keyed exactly that way (never `(opponent_id, team_id)`
    -- Decision P3-22's rows are directed). `None` (the default) means "no
    head-to-head memory at all," matching this module's own established
    convention for optional context (`retirement_age`/`room_for_signings_
    by_team`, above): every caller that does not pass this field is
    byte-identical to pre-Phase-3 behaviour, because nothing built before
    §11 step 12 (channel 3) reads it and `memory.shade(record, p_bar)`
    already treats `record is None` as a strict no-op. `managers/` cannot
    fetch this itself (`managers` may not import `dba2.data`), so a
    caller-supplied mapping is the only legal shape -- the harness
    counterpart is `HarnessState.head_to_head` (`tools/gm_harness/
    state.py`, mirroring `dead_cap_by_team_season`'s own `field(default_
    factory=dict)` construction), populated into this field by `build_
    ctx`. **Step 11 (this step) only wires the writer** -- `run_trade_
    window` reads this field as the seed for `TradeWindowResult.head_to_
    head`'s per-window write path (Decision P3-22/P3-35/P3-42) regardless
    of whether it is `None` (seeded from an empty mapping in that case);
    the READER side (`shade()`, channels 1-3) is steps 12/13/17's job, not
    this one's."""
    h2h_base_accept_rate: float | None = None
    """Decision P3-26's live `p_bar` -- the population's own current mean
    `Σ accepts_by_opponent / Σ opened` over every row in `head_to_head`,
    computed ONCE per trade window from the DECAYED values already in the
    store at the window's own start (before that window's own new
    negotiations are folded in -- `compute_h2h_base_accept_rate`, below,
    reads exactly `head_to_head`, never a mid-window accumulator). `None`
    when there is no memory (`head_to_head is None`) or when the
    population's total `opened` is `0` -- both cases make every shade-
    reading channel a strict no-op, by construction (`shade`'s own `p_bar
    is None` branch), never falling back to a pinned constant (Decision
    P3-26 is precisely the decision that there is no such constant --
    `H2H_BASE_ACCEPT_R` was cut). This is a *measurement of the current
    population*, never a tuning constant, which is why it travels as
    context and not in `GmTuning` -- a caller-supplied value, exactly
    like `head_to_head` above, never computed inside `managers/`."""
    motivation_by_player: Mapping[int, PlayerMotivation] | None = None
    """Decision P4-16 (Phase 4 §11 Stage C step 11): every free agent's
    `player_motivation` row this phase's decisions might weight an offer
    by, keyed by `player_id`. `None` (the default) means "no motivation
    read model available, behave exactly as before" -- the same
    established convention `retirement_age`/`room_for_signings_by_team`/
    `head_to_head` already use above, so every existing caller that never
    populates this field gets `free_agency_service.rank_offers`'s
    original Decision P2-21 placeholder (money-only ranking) byte-for-
    byte. `managers/` cannot fetch this itself (`dba2.managers` may never
    import `dba2.agency` -- `tests/unit/test_layering.py::test_managers_
    module_source_never_imports_agency`, Decision P4-8's directed rule),
    and `dba2.services` code may not import `dba2.data.repositories.
    motivation_repo` directly except `agency_service.py` (the retargeted
    P2-21 denylist, Decision P4-16 itself) -- so a caller-supplied mapping
    assembled through `agency_service.load_motivation_for_league` (the
    "agency service's own read model" P4-16 names) is the only legal
    shape. The production path (`run_phase_hook`'s `FREE_AGENCY` branch)
    populates this via that loader; the in-memory harness (`tools/gm_
    harness/state.py::build_ctx`) mirrors it from `HarnessState.
    motivation`. **Both are gated on `run_phase_hook`'s own `agency_
    enabled` keyword (critic B1 fix -- prior to that fix this docstring
    claimed a production gate that did not exist: `run_phase_hook` took
    no such parameter, and this field was populated unconditionally)**,
    exactly like `head_to_head_enabled` gates `head_to_head` above --
    one branch, at exactly that one assignment, per construction site."""
    trade_request_pressure_by_player: Mapping[int, float] | None = None
    """Decision P4-17, shape pinned by Decision P4-51 (Phase 4 §11 Stage
    C step 12): a bound trade request reaching GMs at PROPOSAL time (this
    field, consumed by `propose_packages`' target ranking below), never
    at valuation time -- `managers/decisions/trade.py::fit_value`'s own
    signature is unchanged, matching P4-17's "changes no signature under
    `managers/`" claim. `None` (the default) means "no agency knowledge,
    behave exactly as before," the same established convention `retirement_
    age`/`room_for_signings_by_team`/`head_to_head`/`motivation_by_player`
    all use above. **Sparse by design, not a dense per-player mapping**:
    contains ONLY players whose trade request actually binds -- Decision
    P4-15's `star_leverage` gate has already been applied upstream (`dba2.
    agency.motivation.trade_request_pressure`), so a player absent from
    this mapping is neutral, never an implicit `0.0` (there is no
    `0.0`-means-neutral convention to get wrong -- every value present is
    a strictly positive pressure magnitude on a player who has one).
    Populated the same way as `motivation_by_player`: production's `run_
    phase_hook` (`REGULAR_SEASON`/`TRADE_DEADLINE`, wherever `run_trade_
    window` actually runs) via `agency_service.load_trade_request_
    pressure_for_league`, the harness's `build_ctx` from `HarnessState.
    motivation`. Both are gated on `run_phase_hook`'s own `agency_enabled`
    keyword (critic B1 fix), same as `motivation_by_player` above."""
    self_managed_team_ids: frozenset[int] = frozenset()
    """Decision P5-44 (docs/design/phase-5-discord-presentation.md §24.1/
    §24.2), predicate extended by GM-2 (docs/design/gm-command-surface.md)
    -- every team id that is *self*-managed: `Team.manager_user_id is not
    None` **and** `not Team.auto_pilot`, derived ONCE in `load_gm_inputs`
    from data it has already loaded (no new query, no change to that
    function's documented 10-round-trip I/O budget). An auto-piloted team
    keeps its manager -- it is still human-MANAGED, in the sense that the
    manager owns it, keeps its channel and keeps every permission -- but
    it is not SELF-managed, because that manager has delegated the actual
    decisions to the AI; every consumer below that reads this set to mean
    "the AI decides for this team" needs exactly the negation of *this*
    predicate, not of bare `manager_user_id is not None`. The empty
    default (`frozenset()`) is the whole inertness argument, the same
    "unset means behave exactly as before" convention `retirement_age`/
    `head_to_head`/`motivation_by_player` all use above: every `if
    team_id in ctx.self_managed_team_ids: continue` below is a branch
    never taken when this is empty, which is every league this project
    has ever generated (every hand-constructed `Team` in this repository
    passes `manager_user_id=None`, and no writer of a non-`None` value
    exists yet).

    **A pure decision function must never read `Team.manager_user_id` off
    a `TeamSnapshot` itself** -- that would put a Discord identity column
    inside the manager-minds logic and create a second definition of "who
    is human," exactly the shape Decision P5-5 (this module's own
    display-layer restatement of Decision P2-33) bans. This field is the
    one legal source of that fact for every function below.

    `tools/gm_harness/state.py::build_ctx` takes the default deliberately
    -- the harness measures AI-vs-AI metric families (M1-M36) and a human
    arm has no control to compare against, so it sweeps the AI-only arm
    forever unless a future harness slice deliberately populates this
    field."""
    manual_timeline_team_ids: frozenset[int] = frozenset()
    """Decision P5-67 (docs/design/phase-5-discord-presentation.md §29) --
    every team id whose `Team.timeline_source is TimelineSource.MANUAL`,
    derived ONCE in `load_gm_inputs` from the same `teams` rows `self_
    managed_team_ids` already reads (no new query). **A different
    question from `self_managed_team_ids`, deliberately kept as its own
    field**: a commissioner may pin an AI team's timeline, and a human
    manager may leave their own team on auto -- collapsing the two into
    one flag would silently couple "who is human" to "whose timeline is
    pinned," which is not the same fact.

    Consumed by `classify_all_windows`' own `pinned_team_ids` parameter
    (passed straight through as `ctx.manual_timeline_team_ids` by `run_
    phase_hook`): a pinned team's classification pass is carried through
    from its own stored value rather than reclassified, and emits no
    `DecisionRecord` (§29.2's own reasoning -- a record whose `window` is
    the human's value but whose `terms` are the classifier's reasoning
    would describe a decision nobody made). Empty default, same "unset
    means behave exactly as before" convention every other `GmContext`
    addition above uses -- every league this project has ever generated
    has `timeline_source = 'auto'` on all 30 teams until `/timeline`
    (Stage D', unbuilt before this field) ever writes `'manual'`."""

    @property
    def next_season(self) -> int:
        """The one season every legality/room/signing-effective-date figure
        in this module reads (see module docstring) -- named as a property
        rather than recomputed as `ctx.season + 1` at each call site."""
        return self.season + 1


GM_BEHAVIOR_SCOPE_VERSION: int = 2
"""Decision P4-46 (docs/design/phase-4-media-and-player-agency.md §17.3/
§18.3), the Family-3 citation staleness predicate's SECOND conjunct,
alongside `managers.tuning.GM_TUNING_VERSION`. Tracks a GM-behavior-
surface change that happens entirely under `dba2/services/` (never under
`managers/`) -- `rank_offers` (Decision P4-16) and the pressure scalar on
this class (Decision P4-17) -- which `GM_TUNING_VERSION` cannot see by
its own definition (`managers/tuning.py:19-22`: bumped only for a
`managers/` constant/weight-table change) and must not be repurposed to
see: bumping it for a services-scoped change would make the constant mean
two different things depending on which commit you read, and would stale
every RS3 precision citation as collateral for a signal none of them are
about (P4-46's own rejected-option reasoning). **Service-scoped, same
`FUTILITY_STREAK`/`COUNTER_SEARCH_WIDTH`-style convention below**:
declaring or bumping this constant does NOT bump `GM_TUNING_VERSION`.

Starts at `2`, not `1`, in the commit that first declares it. Every
Family-3 citation in `seeds/gm_validation_bands.json` as of this commit
carries no `behavior_scope` key at all (measured, Decision P4-73:
`grep -c behavior_scope seeds/gm_validation_bands.json` -> `0`), so
whatever number a first-ever `1` would have held is unobservable by any
existing citation -- P4-46's "advanced in the same commit" requirement is
satisfied by this constant never having shipped an un-advanced value.
`tools/gm_harness/metrics.py`'s shared Family-3 predicate (Decision
P4-72) reads this constant directly; `tools/precertify_family3.py`
prints it alongside every measured value (Decision P4-68) so a human
transcribing a re-precertification copies both from the same line of the
same run's output. The next real GM-behavior-surface change under
`services/` bumps this to `3`."""


def compute_h2h_base_accept_rate(
    head_to_head: Mapping[tuple[int, int], HeadToHead] | None,
) -> float | None:
    """Decision P3-26's `p_bar`, verbatim: `(Σ accepts_by_opponent over all
    870 rows) / (Σ opened over all 870 rows)`. Lives here, not `managers/
    memory.py`, because it needs the loaded `head_to_head` mapping and
    `managers/` may not import `dba2.data` to fetch it itself (`GmContext`
    already carries it, per Decision P3-24) -- this is arithmetic over
    values `GmContext` holds, not an I/O read, so it is a plain, pure
    function of its one argument, callable from `managers/` code as easily
    as from here (nothing here imports `dba2.data`).

    `None` in exactly the two cases Decision P3-26 names: no memory loaded
    at all (`head_to_head is None`), or a loaded-but-empty/all-zero
    population (`Σ opened == 0`, the "45-team expansion draft, day one"
    case) -- both make `shade()` a strict no-op by construction (its own
    `p_bar is None` branch), never a division by zero here."""
    if head_to_head is None:
        return None
    total_opened = sum(row.opened for row in head_to_head.values())
    if total_opened <= 0.0:
        return None
    total_accepts = sum(row.accepts_by_opponent for row in head_to_head.values())
    return total_accepts / total_opened


def _channel3_mean_abs_shade_inputs(
    head_to_head: Mapping[tuple[int, int], HeadToHead] | None,
    h2h_base_accept_rate: float | None,
) -> tuple[float, int]:
    """B3 fix (critic round) -- inputs for §9.2's `mean_abs_shade`
    saturation diagnostic. **The population is the head-to-head ROWS
    themselves (up to 870 of them), never the per-turn ch3 decision
    sites.** A prior version of this docstring attributed a quoted
    phrase ("the same population... or a superset") to §9.2 that does
    not appear anywhere in the design doc -- that citation was
    fabricated and is removed. The real support (critic round, second
    pass, since line 2165's own "across the 870 rows" clause turns out
    to grammatically modify a different item in its list, not
    `mean_abs_shade` -- an over-read the first fix round made): line
    1539 ("ordered pairs differ in `accepts_by_opponent / opened` for
    reasons that are fully present with the mechanism off"), line 2034
    ("`mean|shade|` is the dispersion of opponents' accept rates
    **across ordered pairs**"), and line 3350 ("every pair pinned at
    `shade = ±1`") -- all describe a per-pair/per-row dispersion
    statistic, never a per-decision-site one. `p_bar` (line 1520,
    `Σ accepts_by_opponent / Σ opened` "over all 870 rows") is the
    other row-population precedent this function's shape follows.

    `shade()` is a pure function of one row and `h2h_base_accept_rate`
    (its own `n == 0` branch reads `shade == 0.0` when a row has no
    `opened` mass yet), so this is well-defined even on a window that
    opens zero negotiations -- no per-turn decision site needs to exist
    for a row's `shade` to be computable. **Not the same `None`
    contract as `compute_h2h_base_accept_rate`**: this returns `(0.0,
    0)` only when `head_to_head` itself is `None`/empty (no memory
    loaded at all). When `head_to_head` has rows but `Σ opened <= 0`
    (`compute_h2h_base_accept_rate`'s *other* `None` case), this
    function still has 870 real rows to average over -- each reads
    `shade() == 0.0` via `p_bar=None`, so it returns `(0.0, 870)`, not
    `(0.0, 0)`. Numerically the mean is `0.0` either way; the row
    count differs and callers must not assume the two functions'
    `None`/zero cases line up."""
    if not head_to_head:
        return 0.0, 0
    rows = tuple(head_to_head.values())
    total = sum(abs(memory.shade(row, h2h_base_accept_rate)) for row in rows)
    return total, len(rows)


@dataclass(frozen=True)
class TradeExecution:
    """One executed trade -- ids only, never object references (a
    deliberate choice: it is what makes this value JSON-dumpable and usable
    by both a real DB applier and an in-memory harness applier, T4/T6-T7,
    neither built this dispatch)."""

    team_a_id: int
    team_b_id: int
    players_a_to_b: tuple[int, ...]
    players_b_to_a: tuple[int, ...]
    picks_a_to_b: tuple[int, ...]
    picks_b_to_a: tuple[int, ...]
    net_a: float
    net_b: float
    record_a: DecisionRecord
    record_b: DecisionRecord


@dataclass(frozen=True)
class SigningExecution:
    """One executed (or floor-forced) free-agent/re-signing offer."""

    team_id: int
    player_id: int
    years: int
    annual_salary: int
    start_season: int
    signing_method: SigningMethod
    record: DecisionRecord


@dataclass(frozen=True)
class WaiverExecution:
    """One executed waiver cut -- ids only, never object references
    (matching `TradeExecution`/`SigningExecution`'s own convention, T7a2,
    architect round 8 §7.1). `contract_id` is the cut player's own
    (real or in-memory) active `Contract.id` at decision time -- the I/O
    applier passes it straight to `roster_service.waive(contract_id=...)`,
    which re-reads that contract's own `contract_years` rows itself
    (never re-derived here); the in-memory applier (`tools/gm_harness/
    state.py::apply_waiver_inmem`) uses it only to assert it still matches
    the player's current contract before mutating."""

    team_id: int
    player_id: int
    contract_id: int
    waived_in_season: int
    record: DecisionRecord


@dataclass(frozen=True)
class WindowPassResult:
    window_by_team: Mapping[int, CompetitiveWindow]
    """Every team's final (post-hysteresis) `CompetitiveWindow`."""
    records: tuple[DecisionRecord, ...]
    """One `DecisionRecord` per team, `window.py::score_terms`-backed --
    reconstructs to floating-point precision (see `classify_all_windows`)."""


@dataclass(frozen=True)
class CandidatePackage:
    """One proposed trade package, from the proposer's own perspective --
    `team_a_id` always the proposer. `incoming_a`/`outgoing_a` are exactly
    the bundles `trade.evaluate_trade` takes for team A; team B's own call
    swaps them (see `run_trade_window`)."""

    team_a_id: int
    team_b_id: int
    incoming_a: AssetBundle
    outgoing_a: AssetBundle


@dataclass(frozen=True)
class PendingOffer:
    """One AI-initiated trade candidate addressed to a human-managed
    partner, collected during `run_trade_window`'s ordered-pair loop
    instead of being resolved by `_run_negotiation` (Decision P5-55, as
    corrected by P5-80/P5-91) -- `run_trade_window` never accepts on the
    human's behalf; a human answers this later, entirely outside this
    pure function's own scope (P5-59 is the I/O layer's job, not T3's).

    `from_team_id`/`to_team_id` are `package.team_a_id`/`package.team_b_id`
    renamed for a caller-facing name that does not presuppose an
    in-memory negotiation's own transient `a`/`b` convention -- `package`
    stays the proposer's own `CandidatePackage`, unmodified, exactly as
    `propose_packages` built it. `record` is always the PROPOSER's own
    `DecisionRecord` (`_evaluate_proposer_side`, Decision P5-56) -- no
    `DecisionRecord` reachable through this type ever carries the human
    partner's `team_id`, because no such record is ever computed on this
    path at all."""

    from_team_id: int
    to_team_id: int
    package: CandidatePackage
    record: DecisionRecord


@dataclass(frozen=True)
class TradeWindowResult:
    executions: tuple[TradeExecution, ...]
    records: tuple[DecisionRecord, ...]
    """Every evaluated NEGOTIATED candidate's records, both sides -- now one
    `(record_a, record_b)` pair per NEGOTIATION TURN, not only per seed
    package (Phase 3 §11 step 7): a negotiation that counters still
    contributes exactly one `(accept, accept)` pair at its own terminal turn
    (every other turn has at least one non-"accept" verdict, since
    `respond()`'s cell rule is total and `(accept, accept)` is always
    terminal) -- so `season.py::_count_mutual_accepts`'s own "one accept per
    negotiation" reading is preserved without that helper's own logic
    changing at all.

    **AI-to-human diverted candidates (P5-55/P5-56) are never appended
    here, in either half of the pair** (round-7 critic fix, blocking issue
    1): a diverted candidate has no partner-side record at all (P5-56 bans
    computing one), so it can never satisfy this field's own `(record_a,
    record_b)`-pair-per-turn contract -- appending only `record_a` for it,
    as an earlier version of this path did, breaks `_count_mutual_accepts`'s
    `len(records) % 2 == 0` assertion outright on an odd diverted count and
    silently mis-pairs records on an even one. See `offer_records` below for
    where a diverted candidate's own proposer-side record actually goes."""
    packages_evaluated: int
    """Total NEGOTIATED `CandidatePackage`s scanned (= negotiations opened,
    Decision P3-12/§4.1's B-1 discipline: every seed package opens exactly
    one negotiation, including illegal and round-0-accept ones) -- kept to
    its exact Phase 2 meaning; turn-level cost is carried by `turns_
    evaluated`/`evaluations_performed` below, never folded into this field.

    **AI-to-human diverted candidates never increment this field** (round-7
    critic fix): a diverted candidate never opens a negotiation at all (no
    `_run_negotiation` call is ever made for one), so counting it here would
    violate this field's own "= negotiations opened" definition -- see
    `offers_evaluated` below for the diverted candidate's own, separate
    counter, and P5-57's four gates (not this field) for whether a diverted
    candidate becomes a real offer."""
    turns_evaluated: int = 0
    """Decision P3-12 -- total `NegotiationTurn` count across every
    NEGOTIATED candidate this window opened (protocol depth, reported only;
    a diverted candidate never opens a negotiation, so it contributes
    nothing here, matching `packages_evaluated`'s own exclusion)."""
    evaluations_performed: int = 0
    """Decision P3-12 -- the exact count of real `trade_decisions.
    evaluate_trade` calls this window's NEGOTIATED path made, including
    every call `build_counter`'s own search makes internally (§13.3's `M30 =
    evaluations_performed / packages_evaluated` cost governor's numerator).
    Measured by wrapping `trade_decisions.evaluate_trade` for the scope of
    this call (see `_count_real_evaluations` below) rather than threaded
    through `build_counter`'s own, already-closed/tested return contract
    -- `packages_evaluated == 0` implies `evaluations_performed == 0`
    (nothing to measure), never a division concern here since this field
    is a raw count, not a ratio.

    **Diverted candidates' own real `evaluate_trade` call is subtracted out
    of this field** (round-7 critic fix) -- see `offer_evaluations_
    performed` below, which carries it instead, so M30 stays scoped to the
    AI-vs-AI negotiated universe it was built to cost-govern and the
    `packages_evaluated == 0` implication above stays true even in a window
    made entirely of diverted candidates."""
    counter_opportunities: int = 0
    """M23''s denominator (Decision P3-12, round 12) -- count of
    negotiations whose ROUND-0 turn produced a `COUNTER` stance (i.e.
    §4.2's rule reached its last line), counted once per negotiation, never
    once per turn."""
    counters_admitted: int = 0
    """M23''s numerator -- of `counter_opportunities` above, how many had
    `build_counter` actually return a package on that same round-0
    transition (never re-counted on a later round's own counter, if the
    negotiation counters again)."""
    counter_closed_executions: int = 0
    """M24 -- count of EXECUTED trades (post greedy-selection) whose
    negotiation closed at `round_index >= 1`. Joined by `id(record_a)`
    against the negotiation that produced each executed package's own
    final `DecisionRecord` -- `TradeExecution`'s own shape is untouched
    (§4.4), so this join happens here, once, rather than by threading a
    new field through the execution loop."""
    outcome_census: Mapping[str, int] = field(default_factory=dict)
    """The five-cell round-0 outcome partition (§4.2/step 1's own
    `_classify_cell` naming: `illegal`, `both_reject_legal`,
    `A_reject_B_accept`, `A_accept_B_reject`, `both_accept`), tallied
    purely off round 0's raw verdict pair for every NEGOTIATED seed
    package -- never re-tallied on a later turn, so this is directly
    comparable to step 1's census Output 0. `sum(outcome_census.values())
    == packages_evaluated` for exactly this reason.

    **AI-to-human diverted candidates are never tallied here** (round-7
    critic fix): `_classify_cell` needs a REAL round-0 verdict pair, and
    P5-56 bans ever computing a real partner-side verdict for a diverted
    candidate (only the pure `_PackageProbe` prediction `propose_packages`
    already used to build the candidate exists, which is not a
    `DecisionRecord` and was never a `_classify_cell` input even for a
    negotiated candidate) -- there is no round-0 cell a diverted candidate
    could honestly land in. `offers_evaluated` below is the diverted
    candidate's own counter; P5-57's four gates, not a census cell, are
    what decide whether one becomes a real offer."""
    expired_stalled_count: int = 0
    """Live count of negotiations this window closed `EXPIRED:stalled`
    (Decision P3-9's oscillation guard) -- pre-registered near-zero per
    §4.2's own "a large count is a constructor defect" note, reported for
    step 7's/step 9's verify, never gated."""
    variant_pairs_compared: int = 0
    variant_verdict_flips: int = 0
    """§11 step 11's own required, diagnostic-only measurement -- see
    `_NegotiationTally.variant_verdict_flips`'s own docstring for the full
    derivation. `variant_verdict_flips / variant_pairs_compared` is this
    window's live variant-1/variant-2 flip rate; nothing gates on it."""
    negotiation_depth_violations: int = 0
    """M25 -- a Family-1-grade hard invariant (band `0`): negotiations
    exceeding `max_negotiation_rounds` or ending non-terminal, checked
    live per negotiation (see `_NegotiationTally.depth_violations`)."""
    channel3_decision_sites: int = 0
    """M28 ch3's denominator (Decision P3-36/P3-38, §9.2, step 12) -- see
    `_NegotiationTally.channel3_decision_sites`'s own docstring for the
    full derivation. Reported only; M28's `counted_in_go` stays `false`
    until step 17 wires channels 1 and 2 alongside this one."""
    channel3_reachability: int = 0
    """Of `channel3_decision_sites`, sites where the acting side's own
    `shade` toward its partner was nonzero -- see `_NegotiationTally.
    channel3_reachability`."""
    channel3_flips: int = 0
    """Of `channel3_decision_sites`, sites where shading changed the
    `gap < -counter_band` verdict against that site's own unshaded
    baseline -- see `_NegotiationTally.channel3_flips`."""
    channel3_abs_shade_sum: float = 0.0
    """Σ`abs(shade)` over `channel3_abs_shade_row_count` **head-to-head
    rows** -- `channel3_abs_shade_sum / channel3_abs_shade_row_count` is
    `mean_abs_shade`, the §9.2 saturation diagnostic (escalates, reported
    not gated, above the `0.392` threshold carried in `seeds/gm_
    validation_bands.json`'s M28 row).

    **B3 fix (critic round): the population is the head-to-head ROWS
    (`ctx.head_to_head`, up to 870 of them), never `channel3_decision_
    sites`.** A prior version of this docstring cited design-doc line
    ~2165 for this, but that line's "across the 870 rows" clause
    grammatically modifies a different item in its own list, not
    `mean_abs_shade` -- an over-read, corrected in `_channel3_mean_abs_
    shade_inputs`'s own docstring (see there for the real supporting
    lines: 1539, 2034, 3350, plus `p_bar`'s own row-population precedent
    at line ~1520). This is well-defined even on a window that opens
    zero negotiations, because `shade()` is a pure function of a row and
    `ctx.h2h_base_accept_rate` (its own `n == 0` branch reads `shade ==
    0.0`) -- it needs no negotiation to have happened. See `_channel3_
    mean_abs_shade_inputs`, computed once per window directly from
    `ctx.head_to_head`, never accumulated per decision site."""
    channel3_max_observed: float = 0.0
    """Decision P3-41 (§11 step 17 item 2) -- running MAX of `abs(shade)`
    over `channel3_decision_sites` -- see `_NegotiationTally.channel3_
    max_observed`."""
    channel3_abs_shade_row_count: int = 0
    """Denominator for `channel3_abs_shade_sum` above -- `len(ctx.head_to_
    head)` at this window's own start, `0` only when `ctx.head_to_head`
    is itself `None`/empty. **Not the same `None` contract as
    `compute_h2h_base_accept_rate`**: when `ctx.head_to_head` has rows
    but their `Σ opened` is `0` (that function's *other* `None` case),
    this field is still the real row count (up to 870), not `0` -- see
    `_channel3_mean_abs_shade_inputs`'s own docstring for the full
    distinction. Carried as its own field, rather than reusing
    `channel3_decision_sites`, because the two populations are different
    in kind (rows vs. decision sites) and a shared denominator would
    silently conflate them the next time either changes."""
    channel2_decision_sites: int = 0
    """M28 ch2's denominator (Decision P3-28's replacement, §8.3, step 13)
    -- count of REAL `build_counter` searches this window (every turn that
    reached the `stance == COUNTER, not stalled` branch and actually
    called `build_counter`), regardless of whether that search admitted a
    candidate -- see `_NegotiationTally.channel2_decision_sites`. Reported
    only; `counted_in_go` for M28 does not flip until step 17 (all three
    channels)."""
    channel2_reachability: int = 0
    """Of `channel2_decision_sites` above, how many had a positive `shade`
    for the acting side's own memory of its partner (§9.2's ch2
    reachability row: "searches where shade > 0 for the acting pair" --
    the `max(shade, 0.0)` floor means a non-positive shade cannot move the
    stop point at all) -- see `_NegotiationTally.channel2_reachability`."""
    channel2_flips: int = 0
    """Of `channel2_decision_sites` above, how many had the axis's own
    `baseline_index` (first candidate meeting the pre-step-13 `0.0` bar)
    differ from its `live_index` (first candidate meeting the live `self_
    margin` bar) -- including one existing and the other not -- see
    `_NegotiationTally.channel2_flips` and `_Ch2MarginObservation`."""
    channel2_max_observed: float = 0.0
    """Decision P3-41 (§11 step 17 item 2) -- running MAX of `abs(shade)`
    over `channel2_decision_sites` -- see `_NegotiationTally.channel2_
    max_observed`."""
    channel1_up_decision_sites: int = 0
    """M28 ch1-up's denominator (Decision P3-36/P3-38/P3-40, §9.2, step
    17) -- every ordered pair, every window -- see `_NegotiationTally.
    channel1_up_decision_sites`."""
    channel1_up_reachability: int = 0
    """Of `channel1_up_decision_sites`, pair-windows where decayed
    `executions >= 1.0` AND `far_reject_streak < FUTILITY_STREAK` --
    computed from `ctx.head_to_head`'s own row, never from the production
    call's taken branch -- see `_NegotiationTally.channel1_up_
    reachability`."""
    channel1_up_flips: int = 0
    """Of `channel1_up_decision_sites`, pair-windows where the real
    per-pair target-count call returned `3` against that site's own
    `head_to_head = None` baseline of `2` -- see `_NegotiationTally.
    channel1_up_flips`."""
    channel1_down_decision_sites: int = 0
    """M28 ch1-down's denominator -- same site population as `channel1_up_
    decision_sites` (§9.2's own table) -- see `_NegotiationTally.channel1_
    down_decision_sites`."""
    channel1_down_reachability: int = 0
    """Of `channel1_down_decision_sites`, pair-windows where decayed
    `far_reject_streak >= FUTILITY_STREAK` -- `down`'s own raw predicate,
    nothing outranks it -- see `_NegotiationTally.channel1_down_
    reachability`."""
    channel1_down_flips: int = 0
    """Of `channel1_down_decision_sites`, pair-windows where the real
    per-pair target-count call returned `1` -- see `_NegotiationTally.
    channel1_down_flips`."""
    channel1_up_max_observed: float = 0.0
    """Decision P3-41 (§11 step 17 item 2, critic finding B7-3) --
    running MAX of decayed `executions` over `channel1_up_decision_sites`
    -- see `_NegotiationTally.channel1_up_max_observed`."""
    channel1_down_max_observed: float = 0.0
    """Decision P3-41 -- running MAX of decayed `far_reject_streak` over
    `channel1_down_decision_sites` -- see `_NegotiationTally.channel1_
    down_max_observed`."""
    far_rejects_observed: int = 0
    """Decision P3-41's triage counter -- see `_NegotiationTally.far_
    rejects_observed` and `_is_far_negotiation_reject`'s own docstring
    for the deliberate independence from the h2h writer."""
    phrase_selections_total: int = 0
    """§11 step 16 -- M26/M27's shared denominator, this window's total --
    see `_NegotiationTally.phrase_selections_total`. Reported only from
    this field; `counted_in_go` for both M26 and M27 flips at step 16."""
    phrase_fallback_selections: int = 0
    """Of `phrase_selections_total`, fallback-template selections -- see
    `_NegotiationTally.phrase_fallback_selections`."""
    phrase_selections_by_situation: Mapping[str, int] = field(default_factory=dict)
    """M26's per-situation breakdown, this window -- see `_Negotiation
    Tally.phrase_selections_by_situation`."""
    phrase_fallback_by_situation: Mapping[str, int] = field(default_factory=dict)
    """Of `phrase_selections_by_situation`, the fallback-only counterpart
    -- see `_NegotiationTally.phrase_fallback_by_situation`."""
    phrase_selections_by_philosophy_template: Mapping[str, Mapping[str, int]] = field(
        default_factory=dict
    )
    """M27's own input, this window -- keyed `philosophy.value ->
    template_id -> count` -- see `_NegotiationTally.phrase_selections_by_
    philosophy_template`."""
    head_to_head: Mapping[tuple[int, int], HeadToHead] = field(default_factory=dict)
    """Decision P3-22/P3-35/P3-42's write path (§11 step 11) -- carries
    EVERY row `ctx.head_to_head` supplied on entry (a clone, never the
    caller's own row objects), plus a fresh zero row for any new pair this
    window's negotiations or executions touched for the first time, each
    as the row's FULL UPDATED state -- never a bare delta -- since
    season-boundary decay (Decision P3-25) and cross-window persistence
    both need a full row to work from. **Not sparse relative to the
    caller's own already-loaded history**: a pair `ctx.head_to_head`
    already carried is always present here too, touched or not this
    window, because `_H2HAccumulator` seeds its whole `rows` mapping from
    `ctx.head_to_head` up front (round 13 correction -- this docstring
    previously claimed "only pairs touched this window," which a caller
    doing a partial-key merge could have relied on incorrectly; the actual
    contract is "sparse only relative to the full 870-row universe," i.e.
    a pair truly never seen by either `ctx.head_to_head` or this window is
    the only thing absent). The caller (`tools/gm_harness/season.py`, and
    eventually a production caller) applies this by merging it into its
    own persistent store (`HarnessState.head_to_head.update(...)`, or the
    repo's `upsert_head_to_head_for_league` in production) -- this field
    is a per-window report, never itself the store."""
    offers: tuple[PendingOffer, ...] = ()
    """Decision P5-57 -- AI-initiated offers to human-managed partners
    that cleared all four gates (proposer self-accept, proposer legality,
    partner legality, not consumed by this window's own greedy execution
    loop), at most one per ordered pair, at most `MAX_AI_OFFERS_PER_HUMAN_
    TEAM_PER_WINDOW` per human team. **Appended, defaulted** -- every
    existing caller (`tools/gm_harness/season.py`, every existing
    integration test) is untouched, and a league with no human-managed
    teams produces an empty tuple (never entering the divert at all,
    §28.1) -- the same inertness shape `self_managed_team_ids`'s own
    empty default gives `GmContext`. Never resolved by this function --
    turning one of these into an executed trade or a rejected offer is
    the I/O layer's job (`run_phase_hook`, P5-59), entirely outside
    `run_trade_window`'s own pure scope."""
    offers_evaluated: int = 0
    """**Added round 7 (critic fix, blocking issue 1).** Total AI-to-human
    diverted `CandidatePackage`s scanned this window -- one per candidate
    `propose_packages` returned for an ordered pair whose partner is
    human-managed, whether or not it goes on to clear P5-57's four gates
    into `offers`. The diverted-candidate counterpart to `packages_
    evaluated`, kept as its own field rather than folded in because a
    diverted candidate never opens a negotiation (`packages_evaluated`'s
    own "= negotiations opened" definition would be false of it). Zero
    whenever `ctx.self_managed_team_ids` is empty, for the same inertness
    reason `offers` is empty then."""
    offer_evaluations_performed: int = 0
    """**Added round 7 (critic fix, blocking issue 1).** Real
    `trade_decisions.evaluate_trade` calls made evaluating a diverted
    candidate's own PROPOSER side -- exactly one per `offers_evaluated`
    entry (Decision P5-56: no partner-side call is ever made for one), so
    `offer_evaluations_performed == offers_evaluated` always. Kept separate
    from `evaluations_performed` so `M30 = evaluations_performed /
    packages_evaluated` (§13.3) stays scoped to the AI-vs-AI negotiated
    universe it was built to cost-govern."""
    offer_records: tuple[DecisionRecord, ...] = ()
    """**Added round 7 (critic fix, blocking issue 1).** Every diverted
    candidate's own proposer-side `DecisionRecord` (Decision P5-56), one
    per `offers_evaluated` entry, whether or not it survives P5-57's four
    gates into `offers` -- mirrors `records`' own "every evaluated
    candidate's record, not only the winners" comprehensiveness for the
    negotiated path, without merging into `records` itself, because
    `records` carries `season.py::_count_mutual_accepts`'s `(record_a,
    record_b)`-pair-per-negotiation-turn contract and no diverted
    candidate can satisfy it (P5-56 bans ever computing its partner-side
    half). `record.team_id` here is always the PROPOSER's -- never the
    human partner's, matching `records`' own "zero records carrying the
    human's team_id" guarantee exactly, just through a different field.
    **The caller (`run_phase_hook`, P5-59) persists this alongside
    `records` to the decision-log artifact** (`_write_decision_records`
    writes both in one continuous `seq`), so an AI's genuine decision to
    propose a trade to a human still reaches the durable log -- P5-56's
    own "the proposer's own record IS logged" -- just not through the
    field whose whole contract is pairing."""


@dataclass(frozen=True)
class FreeAgencyResult:
    signings: tuple[SigningExecution, ...]
    records: tuple[DecisionRecord, ...]
    """Every offer constructed this pass (winning or not), plus every
    floor-forced signing's own record -- not only the signings that landed."""


@dataclass(frozen=True)
class DraftResult:
    picks: tuple[draft_decisions.DraftPickResult, ...]


@dataclass(frozen=True)
class WaiverWindowResult:
    executions: tuple[WaiverExecution, ...]
    records: tuple[DecisionRecord, ...]
    """One `DecisionRecord` per cut -- `run_waivers` never scans/records a
    non-executed candidate the way `run_trade_window` does for a rejected
    package, since a waiver cut has no accept/reject branch (§7.2: the
    cut candidate is always the trigger loop's own selection, not a
    proposal that can be declined)."""


# ---------------------------------------------------------------------------
# ValuationCache -- bounds run_trade_window's cost (§10's perf note)
# ---------------------------------------------------------------------------


@dataclass
class ValuationCache:
    """A per-pass cache of `fit_value_for` results (keyed `(team_id,
    player_id)`), pick prices (keyed `(team_id, pick_id)`), and (B3 perf
    fix, critic round 17) the league-wide `{team_id: projected_slot}`
    ranking `picks.py::projected_slot_from_window_score` needs for every
    unresolved pick -- all filled lazily. Valid for exactly one pass --
    construct a fresh one per call site, never reused across two different
    `run_trade_window` calls (no invalidation logic exists because none is
    needed).

    **The `_pick_slot_ranks` fix, concretely.** Before this fix, every one
    of `_price_pick`'s calls for an unresolved pick re-ran `projected_
    slot_from_window_score` -- a full 30-team `window_score` ranking --
    from scratch, even though the ranking depends only on `(league_
    snapshot, tuning)`, both fixed for a whole `run_trade_window` pass.
    Critic measured 102,090 `window_score` calls (3,403 pick-pricing
    calls x 30 teams) driving a 65.54s pass past the design doc's 20s
    budget (`phase-2-manager-minds.md:940`); with the pass's one ranking
    computed once and reused, `run_trade_window`'s own direct `evaluate_
    trade` calls (see `pick_slot_ranks()` below) and this cache's `pick_
    price` both read the same memoized ranking."""

    _fit_value: dict[tuple[int, int], float] = field(default_factory=dict)
    _pick_price: dict[tuple[int, int], float] = field(default_factory=dict)
    _pick_slot_ranks: dict[int, int] | None = field(default=None, repr=False)
    _need_matrix: dict[int, NeedMatrix] = field(default_factory=dict)

    def pick_slot_ranks(self, snapshot: LeagueSnapshot, tuning: GmTuning) -> Mapping[int, int]:
        """The whole pass's `{team_id: projected_slot}` ranking, computed
        once off `(snapshot, tuning)` and memoized -- both `pick_price`
        below and `run_trade_window`'s own direct `evaluate_trade` calls
        read this same value, never recompute it."""
        if self._pick_slot_ranks is None:
            self._pick_slot_ranks = rank_teams_by_projected_slot(snapshot, tuning=tuning)
        return self._pick_slot_ranks

    def need_matrix(self, team_snapshot: TeamSnapshot, tuning: GmTuning) -> NeedMatrix:
        """`build_need_matrix(team_snapshot, tuning)`, keyed by `team_id`
        and memoized (Nit 5, critic round 17) -- `propose_packages` was
        rebuilding a proposer's own `NeedMatrix` from scratch for every
        one of the 870 ordered team pairs a 30-team league's `run_trade_
        window` scans, despite this cache existing for exactly this kind
        of per-pass reuse. A team's `NeedMatrix` depends only on its own
        roster, which `run_trade_window`'s scanning pass never mutates
        (only the later greedy selection loop's local `roster_by_team`
        copies change, never `snapshot` itself), so one `NeedMatrix` per
        team is valid for the whole pass."""
        key = team_snapshot.id
        if key not in self._need_matrix:
            self._need_matrix[key] = build_need_matrix(team_snapshot, tuning=tuning)
        return self._need_matrix[key]

    def fit_value(
        self,
        team_id: int,
        roster_player: RosterPlayer,
        matrix: NeedMatrix,
        ctx: GmContext,
    ) -> float:
        """**Cache-key contract (round 9 §2 F5's named trap).** The cache
        key is `(team_id, roster_player.id)` -- it does NOT include
        `matrix`. `NeedMatrix` itself carries no `team_id` field, so
        nothing here can assert the pairing is correct; the caller alone
        is responsible for the invariant that makes this key valid:
        **`matrix` must always be `cache.need_matrix(<the TeamSnapshot for
        `team_id`>, ...)`.** Passing a different team's matrix (e.g. `team
        A`'s `matrix_a` while keying on `partner_id`) silently poisons this
        cache entry for the rest of the pass -- the returned value stays
        plausible, deterministic, and wrong, with no test able to catch it
        after the fact. `propose_packages` reads B's own roster with B's
        own matrix (`cache.fit_value(partner_id, rp, matrix_b, ctx)`) for
        exactly this reason -- never `matrix_a` paired with `partner_id`."""
        key = (team_id, roster_player.id)
        if key not in self._fit_value:
            self._fit_value[key] = trade_decisions.fit_value_for(
                roster_player,
                matrix,
                league_rng_seed=ctx.league_rng_seed,
                team_id=team_id,
                season=ctx.season,
                phase=ctx.phase,
                philosophy=ctx.philosophy_by_team[team_id],
                window=ctx.window_by_team[team_id],
                cap_settings=ctx.cap_settings,
                tuning=ctx.tuning,
            )
        return self._fit_value[key]

    def fit_values(
        self,
        team_snapshot: TeamSnapshot,
        players: Iterable[RosterPlayer],
        ctx: GmContext,
    ) -> dict[int, float]:
        """The structural fix for round 9 F5's/round 10 F5-§5.6's cache-key
        mispairing hazard: `team_id` and `matrix` are both derived from
        **one** `team_snapshot` argument here, so the wrong-matrix pairing
        (`matrix_a` paired with `partner_id`, or its round-10-introduced
        mirror -- `team_snapshot`'s own matrix paired with a *different*
        team's `players`) becomes unrepresentable at this call site rather
        than merely documented against on `fit_value` above. `propose_
        packages` must route ALL of its valuation reads through this
        accessor and never call `fit_value` directly -- `fit_value` itself
        stays public for other callers/tests, with its own docstring
        contract intact."""
        matrix = self.need_matrix(team_snapshot, ctx.tuning)
        return {rp.id: self.fit_value(team_snapshot.id, rp, matrix, ctx) for rp in players}

    def pick_price(
        self,
        team_id: int,
        pick: DraftPick,
        ctx: GmContext,
        snapshot: LeagueSnapshot,
    ) -> float:
        key = (team_id, pick.id)
        if key not in self._pick_price:
            self._pick_price[key] = trade_decisions._price_pick(
                pick,
                ctx.philosophy_by_team[team_id],
                ctx.window_by_team[team_id],
                season=ctx.season,
                rookie_scale=ctx.rookie_scale,
                cap_settings=ctx.cap_settings,
                league_snapshot=snapshot,
                tuning=ctx.tuning,
                pick_slot_ranks=self.pick_slot_ranks(snapshot, ctx.tuning),
            )
        return self._pick_price[key]


# ---------------------------------------------------------------------------
# Window classification pass
# ---------------------------------------------------------------------------


def classify_all_windows(
    snapshot: LeagueSnapshot,
    *,
    previous_by_team: Mapping[int, CompetitiveWindow],
    philosophy_by_team: Mapping[int, GmPhilosophy],
    tuning: GmTuning = DEFAULT_TUNING,
    pinned_team_ids: frozenset[int] = frozenset(),
) -> WindowPassResult:
    """Per team: `window.classify_window` -> `window.apply_hysteresis` for
    the final value, plus a `DecisionRecord` built from `window.score_terms`
    -- `score` is `sum(term.contribution for term in terms)` rather than a
    second `window.window_score` call, so `record.reconstructs()` holds
    exactly by construction, not merely by `score_terms`' own tested
    guarantee.

    `pinned_team_ids` (Decision P5-67) -- a team in this set is CARRIED
    THROUGH from `previous_by_team[team_id]` (its own stored value)
    rather than reclassified: no `classify_window`, no `apply_hysteresis`,
    no `score_terms` call, and no `DecisionRecord` emitted for it.
    Emitting no record is the honest choice -- a record whose `window` is
    the human's pinned value but whose `terms` are the classifier's own
    reasoning would describe a decision nobody made, and `record.
    reconstructs()` would still (uselessly) pass on it. Empty default
    (`frozenset()`) makes every existing caller, including the harness,
    byte-identical to before this parameter existed."""
    window_by_team: dict[int, CompetitiveWindow] = {}
    records: list[DecisionRecord] = []
    for team_snapshot in snapshot.teams:
        team_id = team_snapshot.id
        if team_id in pinned_team_ids:
            window_by_team[team_id] = previous_by_team[team_id]
            continue
        proposed = window.classify_window(team_snapshot, snapshot, tuning=tuning)
        final = window.apply_hysteresis(
            previous_by_team[team_id], proposed, philosophy_by_team[team_id]
        )
        terms = window.score_terms(team_snapshot, snapshot, tuning=tuning)
        score = sum(term.contribution for term in terms)
        window_by_team[team_id] = final
        records.append(
            DecisionRecord(
                team_id=team_id,
                philosophy=philosophy_by_team[team_id],
                window=final,
                subject=f"team:{team_id}",
                verdict=final.value,
                score=score,
                threshold=0.0,
                terms=terms,
            )
        )
    return WindowPassResult(window_by_team=window_by_team, records=tuple(records))


# ---------------------------------------------------------------------------
# Trade candidate generation
# ---------------------------------------------------------------------------

TRADE_TARGETS_PER_PARTNER = 2
TRADE_OUTGOING_VARIANTS = 2
_MAX_OUTGOING_PLAYERS = 3

FUTILITY_STREAK: float = 4.0
"""Decision P3-40's ladder threshold (§8.4/§9.2, §11 step 17) -- channel
1's `down` branch fires when an ordered pair's decayed `far_reject_
streak` reaches this value (`managers.memory.target_count_for_pair`'s own
first rung). **Service-scoped** (placed beside `TRADE_TARGETS_PER_
PARTNER`/`_MAX_OUTGOING_PLAYERS`/`COUNTER_SEARCH_WIDTH`, not under
`managers/`), so declaring it does NOT bump `GM_TUNING_VERSION` (§3's
module layout, confirmed by `test_constant_inventory.py`'s own
`managers/`-only scan) -- it gates `TRADE_TARGETS_PER_PARTNER`, a service
constant, and Decision P3-40 places its own threshold at the same layer
for that reason. Never changed to make a gate pass (§9.4 item 1): §11
step 17 escalates an unreachable `down` branch to the user instead."""

TRADE_REQUEST_PRESSURE_BOOST_CAP: float = 0.25
"""Decision P4-17 (Phase 4 §11 Stage C step 12): `propose_packages`'
target ranking (below) boosts a player's own `fv_a[rp.id]` MULTIPLICA-
TIVELY on a bound trade request -- `value * (1 + boost)` when `value >=
0`, `value * (1 - boost)` when `value < 0` (critic nit 1 fix -- the
single `(1 + boost)` form inverted direction for a negative `fv_a`,
making a maximal trade request rank that player LESS attractive as a
target, backwards from this decision's own intent; both branches now
move `value` toward "more likely to be ranked as a target" regardless of
its sign). The same shape `managers/decisions/trade.py::fit_value`
already uses for its own `need_bonus` term, chosen so the boost scales
with the player's own fit value rather than a raw pressure unit that
would either swamp a bench player's ranking or do nothing at all to a
superstar's.

`pressure` (`dba2.agency.motivation.pressure_from_morale`) is **not
non-negative by construction as a bare function call** -- `pressure_
from_morale(morale) = -morale` returns a negative value for any positive
(happy) `morale`, regardless of `request_state` (critic nit 2 finding).
What IS true, and is the actual guarantee this boost relies on: **every
real caller only reaches this code path via `trade_request_pressure`**
(`dba2/agency/motivation.py`), which gates on `RequestState.TRADE_
REQUESTED`/`HOLDOUT` (Decision P4-51) -- and reaching either of those
rungs via the ladder itself already required `pressure_from_morale(
morale)` to have cleared that rung's own positive enter bar (rung 2/3,
`LADDER_BARS`). A player whose morale later recovers without a fresh
checkpoint re-evaluating their rung cannot occur within one checkpoint
call (the rung and the pressure reading are computed from the same
`morale` in the same pass) -- so a negative reading is not reachable at
today's rung bars in practice, but it is not enforced by any type or
assertion, and a future rung-bar retune that lowers rung 2/3's enter
bar below `0` would make it reachable for real. `pressure` is linearly
mapped from `[0, 100]` to `[0, TRADE_REQUEST_PRESSURE_BOOST_CAP]`,
capped at a 25% ranking boost, assuming the in-practice non-negative
range. **Service-scoped** (same convention as `FUTILITY_STREAK` above),
so declaring it does NOT bump `GM_TUNING_VERSION`. Disclosed, not
calibrated -- same status as every other first-draft Phase 4 coefficient
(P4-9/P4-35's own "expected to be swept")."""


def _boosted_target_rank_value(value: float, pressure: float | None) -> float:
    """`propose_packages`' target-ranking boost (Decision P4-17), pulled
    out to a plain function so critic nit 1's direction fix is unit-
    testable directly rather than only through a full `propose_packages`
    fixture -- pure arithmetic, no `ctx`/`cache` dependency.

    `pressure is None` (no bound request, or `ctx.trade_request_pressure_
    by_player` absent) returns `value` unchanged -- exactly today's own
    `-fv_a[rp.id]` ranking, byte-for-byte, for every pre-Phase-4 caller.
    Otherwise boosts `value` toward "more likely to be ranked as a
    target" in whichever direction that means for its own sign: `value *
    (1 + boost)` when `value >= 0` (a larger positive value), `value *
    (1 - boost)` when `value < 0` (a smaller-magnitude negative value,
    moved toward zero). See `TRADE_REQUEST_PRESSURE_BOOST_CAP`'s own
    docstring for why the single `(1 + boost)` form was wrong for a
    negative `value`."""
    if pressure is None:
        return value
    boost = min(pressure / 100.0, 1.0) * TRADE_REQUEST_PRESSURE_BOOST_CAP
    return value * ((1.0 + boost) if value >= 0.0 else (1.0 - boost))


@dataclass(frozen=True)
class _PartnerTerms:
    """Everything about B (the partner) that A's outgoing constructor needs
    to read in B's own currency -- assembled once per ordered pair by
    `propose_packages` (architect round 10 §5.2). `fv_by_player_id` covers
    **both** rosters (A's and B's) read through B's own `NeedMatrix` (round
    10 F7 -- this is free: the full 30x450 cache cross product is already
    populated by a normal pass). `players_ascending` is B's own roster
    ascending by B's own `fit_value`, tie-broken by `rp.id` -- exactly the
    sequence `_build_throw_ins` already contracts for, passed through
    unchanged so there is one definition of it, not two."""

    team_id: int
    loyalty: float
    window: CompetitiveWindow
    room_dollars: int
    over_cap: bool
    fv_by_player_id: Mapping[int, float]
    players_ascending: tuple[RosterPlayer, ...]


@dataclass(frozen=True)
class _PackageProbe:
    """One `_probe_package` evaluation of a trial outgoing set -- round 10
    §5.2. `value_gap` is `S_in_B - V_req` (architect round 10 F1/F8):
    non-negative means B's real `evaluate_trade` acceptance test would
    pass, up to floating-point summation order (§5.7 item 1 asserts this
    equality directly against the real function)."""

    throw_ins: tuple[RosterPlayer, ...]
    value_gap: float
    matching_floor_ok: bool
    matching_ceiling_ok: bool


def _probe_package(
    target: RosterPlayer,
    outgoing_players: Sequence[RosterPlayer],
    *,
    partner: _PartnerTerms,
    incoming_pick_value: float = 0.0,
    next_season: int,
    next_season_cap_settings: LeagueCapSettings,
    proposer_over_cap: bool,
    tuning: GmTuning,
) -> _PackageProbe:
    """A pure function predicting B's own `evaluate_trade` verdict on the
    package `{outgoing_players} -> A, {target, throw-ins} -> B` **exactly**
    (architect round 10 F8), by calling the real `_build_throw_ins` (never
    modeling it, round 10 F2) and the real `trade_decisions.cap_delta_
    bonus` (never re-deriving the formula, Decision P2-33's standing rule).

    `cap_delta_bonus`'s bundles are from B's own point of view: B receives
    `outgoing_players` (`incoming`), B sends `target` plus the throw-ins
    (`outgoing`) -- getting this backwards flips a term worth up to ~116 VP
    (round 10 F1 item 3).

    `matching_floor_ok`/`matching_ceiling_ok` are round 10 F5/F6's two
    salary-matching constraints, each gated on whether the relevant side is
    over the cap (`_check_legality`'s own `used_pre > salary_cap` test,
    reduced to `room < 0` since `ctx.room_by_team` IS `next_season_room_
    dollars` -- round 10 F5's derivation): the A-side floor is a stop
    condition the constructor's phase 2 must reach; the B-side ceiling is
    an admission rule every candidate in both phases must pass, never a
    stop condition (round 10 F6's table).

    **Decision P2-37 does not reach this function.** `_occupies_next_
    season` is called below at its own default (`retirement_age=None`) --
    the trade-construction pipeline is out of that decision's scope (it
    named only the occupancy predicate's two other consumers, the roster-
    floor enforcement and the waiver ceiling trigger); critic round 51 (B1)
    found a prior build round's attempt to thread `retirement_age` through
    this whole chain broke `_build_throw_ins`'s non-occupying admission
    pass's documented `salary_next == 0` premise, and it was not load-
    bearing for the M1 fix either. Do not re-add it here.

    `incoming_pick_value` (Decision P3-5/P3-11, Phase 3) is the constant
    price of a draft pick B receives alongside `outgoing_players` -- added
    to `s_in_b` unweighted, matching `evaluate_trade`'s own `in:pick` term
    weight of `+1.0` (`trade.py:674`). It defaults to `0.0` so every
    Phase 2 call site (no pick involved) is untouched. There is
    deliberately no symmetric `outgoing_pick_value` for `s_out_b`: Decision
    P3-11 cuts it both because the pick set is frozen for a negotiation's
    whole life (making a second scalar unreachable -- B's outgoing bundle
    is always `target` plus throw-ins, players only, in every code path
    `propose_packages` can produce) and because, if it were ever reachable,
    the correct form would not be a plain addition to `s_out_b` at all: in
    `v_req`'s own terms it would need `+ P_out` (matching `out:pick`'s
    `-1.0` weight, unweighted by `partner.loyalty` -- do NOT write
    `- P_out`, that flips the sign the exact way this function's own
    history has flipped it before), plus a matching `+ P_out` inside the
    `max(min_abs_gain, min_rel_gain * ...)` threshold term, since picks
    raise `outgoing_fit_sum`/`threshold` too (`trade.py:708-709`). Do not
    add it here."""
    outgoing_occupying = sum(
        1 for rp in outgoing_players if _occupies_next_season(rp, next_season)
    )
    throw_ins = _build_throw_ins(
        target,
        outgoing_players,
        partner.players_ascending,
        next_season,
        outgoing_occupying=outgoing_occupying,
    )

    s_in_b = (
        sum(partner.fv_by_player_id[o.id] for o in outgoing_players)
        + incoming_pick_value
    )
    s_out_b = partner.fv_by_player_id[target.id] + sum(
        partner.fv_by_player_id[x.id] for x in throw_ins
    )
    cap_delta = trade_decisions.cap_delta_bonus(
        AssetBundle(players=tuple(outgoing_players)),
        AssetBundle(players=(target, *throw_ins)),
        partner.window,
        next_season,
        partner.room_dollars,
        next_season_cap_settings.salary_cap,
        tuning=tuning,
    )
    v_req = (
        partner.loyalty * s_out_b
        + max(tuning.min_abs_gain, tuning.min_rel_gain * s_out_b)
        - cap_delta
    )
    value_gap = s_in_b - v_req

    w = trade_decisions.salary_next(target, next_season) + sum(
        trade_decisions.salary_next(x, next_season) for x in throw_ins
    )
    out_sal = sum(trade_decisions.salary_next(o, next_season) for o in outgoing_players)
    matching_floor_ok = (not proposer_over_cap) or (
        w <= out_sal * trade_decisions._TRADE_MATCHING_PCT + trade_decisions._TRADE_MATCHING_FLAT
    )
    matching_ceiling_ok = (not partner.over_cap) or (
        out_sal <= w * trade_decisions._TRADE_MATCHING_PCT + trade_decisions._TRADE_MATCHING_FLAT
    )

    return _PackageProbe(
        throw_ins=throw_ins,
        value_gap=value_gap,
        matching_floor_ok=matching_floor_ok,
        matching_ceiling_ok=matching_ceiling_ok,
    )


def _value_priority_orders(
    a_players_ascending: Sequence[RosterPlayer],
    *,
    fv_a_by_player_id: Mapping[int, float],
    fv_b_by_player_id: Mapping[int, float],
) -> tuple[tuple[RosterPlayer, ...], tuple[RosterPlayer, ...]]:
    """The two candidate orderings architect round 10 F3/F4 derive:

    `ratio_order` -- the joint-surplus Dantzig greedy, cost-minimising for
    A among candidates that can help B at all. A tier-0 preamble (`fv_A <=
    0.0 < fv_B`, round 9's own salary-dump channel seen from A's side) goes
    first, ordered by `fv_B` descending -- the ratio itself is meaningless
    there (a non-positive divisor), never a `ZeroDivisionError` and never a
    sign inversion since the `> 0.0` partition below guarantees `fv_A > 0`
    for every ratio actually computed. Tier 1 (`fv_A > 0.0`) is ordered by
    `fv_B / fv_A` descending -- the fractional-relaxation optimum for "most
    value to B per unit of cost to A."

    `delivery_order` -- `fv_B` descending, count-minimising rather than
    cost-minimising: the ordering most likely to clear B's bar within
    `_MAX_OUTGOING_PLAYERS`'s hard cardinality cap, used as `_build_
    outgoing_variant1`'s fallback when the ratio ordering cannot (round 10
    F4).

    Both orderings first drop every candidate with `fv_B <= 0.0` (round 10
    F3's sign argument: such a player cannot help clear B's bar and only
    moves the running sum away from it) and both are total/deterministic,
    every key ending in `a.id` (round 10 §5.5's determinism rule -- no set
    is ever iterated for ordering here)."""
    pool = [a for a in a_players_ascending if fv_b_by_player_id[a.id] > 0.0]
    tier0 = sorted(
        (a for a in pool if fv_a_by_player_id[a.id] <= 0.0),
        key=lambda a: (-fv_b_by_player_id[a.id], a.id),
    )
    tier1 = sorted(
        (a for a in pool if fv_a_by_player_id[a.id] > 0.0),
        key=lambda a: (-(fv_b_by_player_id[a.id] / fv_a_by_player_id[a.id]), a.id),
    )
    ratio_order = tuple(tier0 + tier1)
    delivery_order = tuple(sorted(pool, key=lambda a: (-fv_b_by_player_id[a.id], a.id)))
    return ratio_order, delivery_order


def _run_value_phase(
    order: Sequence[RosterPlayer],
    target: RosterPlayer,
    *,
    partner: _PartnerTerms,
    next_season: int,
    next_season_cap_settings: LeagueCapSettings,
    proposer_over_cap: bool,
    tuning: GmTuning,
) -> tuple[list[RosterPlayer], set[int], _PackageProbe | None]:
    """One bounded pass over `order` (architect round 10 §5.3 step 2) --
    factored out so `_build_outgoing_variant1` can run it twice (ratio
    order, then delivery order as a fallback) without duplicating the
    admission/stop logic. Returns `(chosen, chosen_ids, last_probe)` --
    `last_probe` is `None` iff nothing was ever admitted."""
    chosen: list[RosterPlayer] = []
    chosen_ids: set[int] = set()
    last_probe: _PackageProbe | None = None
    for c in order:
        if len(chosen) >= _MAX_OUTGOING_PLAYERS:
            break
        trial = chosen + [c]
        probe = _probe_package(
            target,
            trial,
            partner=partner,
            next_season=next_season,
            next_season_cap_settings=next_season_cap_settings,
            proposer_over_cap=proposer_over_cap,
            tuning=tuning,
        )
        if not probe.matching_ceiling_ok:
            # Admission rule (round 10 F5/F6), never a stop condition --
            # skip this candidate and keep scanning the same ordering.
            continue
        chosen = trial
        chosen_ids.add(c.id)
        last_probe = probe
        if probe.value_gap >= 0.0:
            break
    return chosen, chosen_ids, last_probe


def _build_outgoing_variant1(
    target: RosterPlayer,
    a_players_ascending: Sequence[RosterPlayer],
    next_season: int,
    *,
    partner: _PartnerTerms,
    fv_a_by_player_id: Mapping[int, float],
    next_season_cap_settings: LeagueCapSettings,
    proposer_over_cap: bool,
    tuning: GmTuning,
) -> tuple[RosterPlayer, ...]:
    """Architect round 10 §5.3 -- value-targeted outgoing construction in
    B's own currency, replacing the pre-round-10 one-sided ascending-`fv_A`
    walk. The first three positional parameters and the return type are
    unchanged deliberately (this still builds variant 1's player tuple,
    both variants share it, and every existing call site's shape is
    preserved); the six new keyword-only parameters carry what the value
    target and the two matching constraints need.

    **Decision P2-37 does not reach this function** -- see `_probe_
    package`'s own docstring for why the trade-construction pipeline stays
    out of scope.

    **Phase 1 (value bar, run once per ordering, ratio first).** Walks
    `_value_priority_orders`'s `ratio_order`; if it cannot clear B's bar
    (`_PackageProbe.value_gap >= 0.0`) within `_MAX_OUTGOING_PLAYERS`, retry
    once against `delivery_order`. If neither clears it, keep the ratio
    pass's best-effort result and skip phase 2 entirely (round 10 §5.3 step
    3 -- a package that fails B's bar gains nothing from salary padding,
    and phase 2's own admission rule would refuse every candidate anyway
    since it requires the bar to still hold).

    **Phase 2 (A-side matching floor), only if the kept pass succeeded and
    its own `matching_floor_ok` is `False`.** Walks `a_players_ascending`
    (today's ordering, reused unchanged -- round 10 F3's inversion works
    FOR this phase: A's ascending-`fv_A` head is the biggest-salary
    overpays, exactly what a salary floor needs). A candidate is admitted
    only if it both respects the ceiling and does not un-clear the value
    bar (round 10 F6's second admission rule, the subtlest hole in the
    whole design) -- appending stops the moment the floor is met.

    **Degradation (round 10 §5.3 step 5), never raises, never returns
    `None`, never returns an empty tuple for a non-empty roster:** an empty
    `a_players_ascending` returns `()`; if nothing was ever admitted (every
    candidate refused by the ceiling, or B values none of A's players
    positively), returns `(a_players_ascending[0],)` -- today's minimum
    behaviour, a single cheapest-by-`fv_A` player. If the bar was never
    cleared, the best-effort set is returned anyway (`evaluate_trade`
    remains the sole authority on legality/acceptance; this generator has
    never guaranteed either).

    **Termination.** Two bounded `for` loops (phase 1 run at most twice,
    phase 2 once), each over a finite tuple, each committing at most one
    player per iteration, hard-capped at `_MAX_OUTGOING_PLAYERS` -- no
    `while`, no recursion, no fixed-point iteration (round 10 F2's
    circularity is broken by exactly-probing `_build_throw_ins` at each
    trial, never by iterating toward one)."""
    if not a_players_ascending:
        return ()

    ratio_order, delivery_order = _value_priority_orders(
        a_players_ascending,
        fv_a_by_player_id=fv_a_by_player_id,
        fv_b_by_player_id=partner.fv_by_player_id,
    )

    chosen, chosen_ids, last_probe = _run_value_phase(
        ratio_order,
        target,
        partner=partner,
        next_season=next_season,
        next_season_cap_settings=next_season_cap_settings,
        proposer_over_cap=proposer_over_cap,
        tuning=tuning,
    )
    succeeded = last_probe is not None and last_probe.value_gap >= 0.0

    if not succeeded:
        chosen_d, chosen_ids_d, last_probe_d = _run_value_phase(
            delivery_order,
            target,
            partner=partner,
            next_season=next_season,
            next_season_cap_settings=next_season_cap_settings,
            proposer_over_cap=proposer_over_cap,
            tuning=tuning,
        )
        if last_probe_d is not None and last_probe_d.value_gap >= 0.0:
            chosen, chosen_ids, last_probe = chosen_d, chosen_ids_d, last_probe_d
            succeeded = True
        # else: keep the ratio pass's best effort, skip phase 2 entirely.

    if succeeded and last_probe is not None and not last_probe.matching_floor_ok:
        for c in a_players_ascending:
            if len(chosen) >= _MAX_OUTGOING_PLAYERS:
                break
            if c.id in chosen_ids:
                continue
            trial = chosen + [c]
            probe = _probe_package(
                target,
                trial,
                partner=partner,
                next_season=next_season,
                next_season_cap_settings=next_season_cap_settings,
                proposer_over_cap=proposer_over_cap,
                tuning=tuning,
            )
            if not (probe.matching_ceiling_ok and probe.value_gap >= 0.0):
                continue
            chosen = trial
            chosen_ids.add(c.id)
            last_probe = probe
            if probe.matching_floor_ok:
                break

    if not chosen:
        # Known, spec-sanctioned gap (round 10 §5.3 step 5, critic round 42
        # nit): this single-player fallback bypasses the ceiling admission
        # rule (`_PackageProbe.matching_ceiling_ok`) entirely -- it never
        # calls `_probe_package` at all -- because `chosen` only reaches
        # this branch when every real candidate was already refused by the
        # ceiling or the `fv_B > 0` pool was empty, so there is no
        # ceiling-respecting candidate left to prefer over it. Empirically
        # inert as of round 35/42's measurement (B-side `illegal:salary_
        # matching` = 0 over 1,740 targets at `rng_seed=1`), not an
        # oversight.
        return (a_players_ascending[0],)
    return tuple(chosen)


def _build_throw_ins(
    target: RosterPlayer,
    outgoing_players: Sequence[RosterPlayer],
    b_players_ascending: Sequence[RosterPlayer],
    next_season: int,
    *,
    outgoing_occupying: int,
) -> tuple[RosterPlayer, ...]:
    """`N - 1` roster-neutral bodies from B's own roster to send back with
    `target`, where `N = len(outgoing_players)` -- the fix for the
    `roster_max`/`roster_min` deadlock architect round 9 measured (`docs/
    ledger/reports/phase-2-architect-round-9.md` §2, §5). Keeping both
    sides' roster counts flat (`|incoming| == |outgoing|` on both legs)
    drives every `illegal:roster_max`/`illegal:roster_min` verdict to zero
    by construction, not probabilistically.

    A pure function of its arguments, no `ValuationCache`/`GmContext`
    dependency, directly unit-testable against hand-built rosters --
    mirrors `_build_outgoing_variant1`'s own shape deliberately (the
    caller does the sorting and passes an already-ordered sequence).

    `b_players_ascending` must be B's own roster, ascending by B's own
    `fit_value` (`cache.fit_value(partner_id, ..., matrix_b, ...)`, never
    A's read of B -- round 9 §2 F5's acceptance-arithmetic argument: B is
    the binding side of `evaluate_trade`'s mutual-accept test, so the
    throw-in selection must minimise the cost *to B*, not the gain to A),
    tie-broken by `rp.id`, `target` excluded by the caller or not -- this
    function skips it either way.

    **Round 12 occupancy-balance rule** (`docs/ledger/reports/phase-2-
    architect-round-12.md` §5.3 step 2). Body-count neutrality (the
    invariant this function always guaranteed) is `_check_legality`'s own
    basis (`len(roster)`), but it is NOT the basis free agency, the roster
    floor, and the waiver trigger read (`_next_season_roster_count` /
    `_occupies_next_season`) -- a body-count-neutral trade could still
    move real next-season roster slots between the two teams if the
    throw-ins it selects skew toward expiring (non-occupying) bodies,
    which the old budget/backfill passes did systematically (`salary_
    next` scores an expiring player at `0`, so it was always cheapest).

    `outgoing_occupying` (caller-supplied -- the count of `outgoing_
    players` for which `_occupies_next_season` is true) plus whether
    `target` itself occupies determine how many of the `k = N - 1`
    throw-ins MUST occupy a next-season slot (`k_occ`) versus must NOT
    (`k - k_occ`), so that B's own next-season occupancy delta nets to
    zero -- and, because bodies are already exactly balanced, A's nets to
    zero too. `eligible` is partitioned into an `occupying` and a
    `non_occupying` sub-list, each preserving `b_players_ascending`'s own
    order. The occupying class is served by the SAME two-phase algorithm
    this function always used (a budget-respecting value pass, then a
    cheapest-first backfill for any shortfall). The non-occupying class
    has no backfill branch at all -- see that pass's own comment below
    for why one is impossible, not just unwritten.

    **Correction (critic round 43, B1):** this docstring previously
    claimed running the occupying class's value pass **before** the
    non-occupying class's against a shared `remaining_budget` ("occupying-
    class first") "is what keeps the resulting package matching-legal"
    (round 12 §8 RR1). That claim is false and has been removed, not
    softened. `trade_decisions.salary_next` returns `0` for every player
    `_occupies_next_season` is `False` for, by construction -- exactly
    the non-occupying class -- so the non-occupying value pass never
    decrements `remaining_budget` regardless of when it runs. Which
    class's value pass goes first therefore cannot change any selection,
    on any input: proved analytically, by an A/B budget-order swap across
    a real 1,740-target sweep (0 differences across 5,238 legality calls),
    and by swapping the two loops directly and re-running the suite (no
    test failed, including the one that claimed to kill this exact
    reversal). The occupying-first order is kept in the code below purely
    for readability (it groups the class with real dollar consequences
    first) -- it is not load-bearing today. A final cross-class fallback
    fills any remaining shortfall (a class whose own eligible pool is
    smaller than its quota) from whichever class still has candidates,
    cheapest-first -- preserving this function's original degrade-
    without-raising contract exactly.

    Selection within each class/pass (round 9 §5.3, unchanged): a
    budget-respecting value pass first (cheapest-to-B within the salary-
    matching headroom `outgoing_players` leaves after absorbing `target`),
    then a cheapest-first backfill pass for any shortfall, ignoring the
    budget -- `_check_legality` remains the sole authority on whether the
    resulting package is actually legal (this generator has never
    guaranteed legality, same as `_build_outgoing_variant1`, and this
    round adds no legality check here either). Degrades to fewer than
    `N - 1` players without raising if B's eligible pool is smaller (a
    small `--players-per-team` harness run must not crash here).

    **Decision P2-37 does not reach this function** -- see `_probe_
    package`'s own docstring for why the trade-construction pipeline stays
    out of scope. Critic round 51 (B1) proved this matters concretely:
    threading a `retirement_age` through here breaks the non-occupying
    value pass's own documented premise below (`salary_next == 0` for
    every non-occupying candidate) the moment a player is reclassified
    non-occupying by age while their contract still covers `next_season`
    -- their `salary_next` is then nonzero, and the unconditional-admission
    pass would go over budget silently."""
    k = len(outgoing_players) - 1
    if k <= 0:
        return ()

    outgoing_salary = sum(
        trade_decisions.salary_next(rp, next_season) for rp in outgoing_players
    )
    target_salary = trade_decisions.salary_next(target, next_season)
    budget = (
        outgoing_salary * trade_decisions._TRADE_MATCHING_PCT
        + trade_decisions._TRADE_MATCHING_FLAT
        - target_salary
    )
    budget = max(0, budget)

    rank_by_id = {rp.id: i for i, rp in enumerate(b_players_ascending)}
    eligible = [rp for rp in b_players_ascending if rp.id != target.id]

    t_occ = 1 if _occupies_next_season(target, next_season) else 0
    k_occ = max(0, min(k, outgoing_occupying - t_occ))
    k_non = k - k_occ

    occupying = [rp for rp in eligible if _occupies_next_season(rp, next_season)]
    non_occupying = [rp for rp in eligible if not _occupies_next_season(rp, next_season)]

    def _cheapest_first_key(rp: RosterPlayer) -> tuple[int, int, int]:
        return (trade_decisions.salary_next(rp, next_season), rank_by_id[rp.id], rp.id)

    chosen: list[RosterPlayer] = []
    chosen_ids: set[int] = set()
    remaining_budget = budget

    # Value pass, occupying-class first (readability only -- see this
    # function's own docstring correction: the order is not load-bearing).
    occ_taken = 0
    for rp in occupying:
        if occ_taken >= k_occ:
            break
        salary = trade_decisions.salary_next(rp, next_season)
        if salary <= remaining_budget:
            chosen.append(rp)
            chosen_ids.add(rp.id)
            remaining_budget -= salary
            occ_taken += 1

    # Non-occupying "value pass": every candidate here has salary_next ==
    # 0 by construction (trade_decisions.salary_next returns 0 for exactly
    # the players _occupies_next_season is False for -- the definition of
    # this class), so every candidate the loop reaches is admitted
    # unconditionally, in b_players_ascending order (B's own cheapest-to-
    # B-first order), until k_non is filled or the pool runs out.
    #
    # Critic round 43 (B1): this class deliberately has NO backfill branch.
    # One is provably impossible to ever reach, not merely redundant: this
    # pass always takes every candidate it iterates over (the budget check
    # can never fail), so by the time it stops, either non_taken == k_non
    # (quota met, nothing to backfill) or the entire non_occupying pool has
    # already been consumed into `chosen` (so a hypothetical backfill's own
    # "not yet chosen" remainder would be empty). A prior version of this
    # function had a backfill block here that could never execute under
    # any input; it has been deleted rather than left as dead code guarded
    # by a comment, since (unlike the occupying class) there is no
    # currently-foreseeable change to this class's own invariant that
    # would make it reachable -- `k_non`'s shortfall case is already fully
    # handled by the cross-class fallback below.
    non_taken = 0
    for rp in non_occupying:
        if non_taken >= k_non:
            break
        chosen.append(rp)
        chosen_ids.add(rp.id)
        non_taken += 1

    # Cheapest-first backfill for the occupying class's own shortfall --
    # budget-ignoring, same as the pre-round-12 single pass. The non-
    # occupying class has no equivalent branch; see that pass's comment.
    if occ_taken < k_occ:
        remainder = sorted(
            (rp for rp in occupying if rp.id not in chosen_ids), key=_cheapest_first_key
        )
        for rp in remainder:
            if occ_taken >= k_occ:
                break
            chosen.append(rp)
            chosen_ids.add(rp.id)
            occ_taken += 1

    # Cross-class fallback, last: if either class's own eligible pool was
    # smaller than its quota, fill the remainder from whichever class
    # still has candidates, cheapest-first -- never raises, may still
    # return short of `k` if B's whole eligible pool is smaller than `k`
    # (the pre-round-12 degrade-without-raising contract, unchanged).
    if len(chosen) < k:
        remainder = sorted(
            (rp for rp in eligible if rp.id not in chosen_ids), key=_cheapest_first_key
        )
        for rp in remainder:
            if len(chosen) >= k:
                break
            chosen.append(rp)
            chosen_ids.add(rp.id)

    return tuple(chosen)


def _highest_value_future_first_round_pick(
    team_snapshot: TeamSnapshot,
    ctx: GmContext,
    cache: ValuationCache,
    snapshot: LeagueSnapshot,
) -> DraftPick | None:
    candidates = [
        p
        for p in team_snapshot.draft_picks
        if p.round == 1 and p.season > ctx.season and not p.conveyed
    ]
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda p: (-cache.pick_price(team_snapshot.id, p, ctx, snapshot), p.id),
    )
    return ranked[0]


def _channel1_target_count(
    ctx: GmContext, proposer_id: int, partner_id: int
) -> tuple[int, HeadToHead | None]:
    """Channel 1's one real decision site (Decision P3-40, §8.4/§9.2, §11
    step 17) -- looks up the ACTING side's own row of its partner
    (`ctx.head_to_head.get((proposer_id, partner_id))`, `None` for a pair
    with no row yet or when there is no memory subsystem at all -- the
    same `None`-means-"no memory" contract `shade()`'s own call site
    already uses, §9.2's condition (B)), then applies `managers.memory.
    target_count_for_pair`'s pure ladder against the service-scoped
    `FUTILITY_STREAK` threshold.

    **Called from two places on the identical `(ctx, proposer_id,
    partner_id)` inputs: `propose_packages` (to decide `targets`, below)
    and `run_trade_window` (M28 ch1's own decision-site tally, once per
    ordered pair per window).** A pure function of its own arguments, so
    the two calls agree BY CONSTRUCTION, never merely by observation --
    this is what lets `run_trade_window` read `target_count` for its tally
    without `propose_packages` needing a new parameter (which would touch
    every existing `monkeypatch.setattr(gm_service, "propose_packages",
    ...)` test double's own fixed 5-argument signature, an unrelated
    blast radius this step does not need)."""
    h2h_row = (
        ctx.head_to_head.get((proposer_id, partner_id)) if ctx.head_to_head is not None else None
    )
    return memory.target_count_for_pair(h2h_row, FUTILITY_STREAK), h2h_row


def propose_packages(
    proposer_id: int,
    partner_id: int,
    snapshot: LeagueSnapshot,
    ctx: GmContext,
    cache: ValuationCache,
) -> tuple[CandidatePackage, ...]:
    """The trade candidate generator, exact rule from the ordered pair
    `(A=proposer, B=partner)`: rank B's roster by A's own `fit_value_for`,
    take the top `target_count` (Decision P3-40's ladder, `_channel1_
    target_count`, above -- `TRADE_TARGETS_PER_PARTNER` at `head_to_head
    = None`/no memory, its own unchanged default); for each target, build
    up to `TRADE_OUTGOING_VARIANTS` outgoing bundles from A (see
    module-level helpers for each variant's own rule).

    **Architect round 10 §5.4.** `_build_outgoing_variant1` now builds A's
    outgoing set against a value target read in B's own currency (`fv_b`),
    not A's own ascending `fit_value` alone -- every valuation read below
    goes through `cache.fit_values` (never a bare `cache.fit_value` call),
    which derives both `team_id` and `matrix` from one `TeamSnapshot`
    argument so the round-10-introduced `(partner_id, A's roster)` cache-
    key pairing (round 10 §5.6) is structurally unrepresentable rather than
    merely documented against."""
    team_a = snapshot.team_by_id(proposer_id)
    team_b = snapshot.team_by_id(partner_id)
    next_season = ctx.next_season
    all_players = (*team_a.roster, *team_b.roster)

    # A's read of both rosters, and B's read of both rosters (round 10
    # F7) -- the union of every ordered pair's own reads over a full pass
    # already covers the full 30x450 team/player cross product, so this
    # adds no new `fit_value_for` computation, only dictionary lookups.
    fv_a = cache.fit_values(team_a, all_players, ctx)
    fv_b = cache.fit_values(team_b, all_players, ctx)

    # Decision P4-17 (Stage C step 12): a bound trade request surfaces its
    # own player as a target more readily, at PROPOSAL time (this ranking
    # key) rather than valuation time -- `fv_a` itself (`cache.fit_values`,
    # `managers/decisions/trade.py::fit_value`) is untouched, matching
    # P4-17's "changes no signature under `managers/`" claim.
    # `ctx.trade_request_pressure_by_player` is `None`/sparse for every
    # pre-Phase-4 caller and every player without a bound request, so
    # `_target_rank_key` reduces to today's own `-fv_a[rp.id]` exactly in
    # that case -- `TRADE_REQUEST_PRESSURE_BOOST_CAP`'s own docstring
    # has the boost formula.
    pressure_by_player = ctx.trade_request_pressure_by_player or {}

    def _target_rank_key(rp: RosterPlayer) -> tuple[float, int]:
        value = _boosted_target_rank_value(fv_a[rp.id], pressure_by_player.get(rp.id))
        return (-value, rp.id)

    ranked_b = sorted(team_b.roster, key=_target_rank_key)
    target_count, _h2h_row = _channel1_target_count(ctx, proposer_id, partner_id)
    targets = ranked_b[:target_count]

    a_players_ascending = sorted(team_a.roster, key=lambda rp: (fv_a[rp.id], rp.id))
    # B's own read of B's own roster (round 9 §2 F5, closed structurally by
    # `fit_values` now rather than by caller discipline alone).
    b_players_ascending = sorted(team_b.roster, key=lambda rp: (fv_b[rp.id], rp.id))

    partner = _PartnerTerms(
        team_id=partner_id,
        loyalty=PROFILES[ctx.philosophy_by_team[partner_id]].loyalty,
        window=ctx.window_by_team[partner_id],
        room_dollars=ctx.room_by_team[partner_id],
        over_cap=ctx.room_by_team[partner_id] < 0,
        fv_by_player_id=fv_b,
        players_ascending=tuple(b_players_ascending),
    )
    proposer_over_cap = ctx.room_by_team[proposer_id] < 0
    # `_value_priority_orders` is recomputed once per target inside `_build_
    # outgoing_variant1` rather than hoisted here -- the exact signature
    # architect round 10 §5.2 specifies takes no precomputed-orders
    # parameter, and the per-target cost (two ~15-element sorts x 2 targets
    # x up to 870 ordered pairs) is the "acceptable" branch round 10 §5.3
    # step 1 itself names, not the "preferable" hoisted one; §5.8's wall-
    # time measurement is the check on this judgment call, not a guess.

    top_pick = _highest_value_future_first_round_pick(team_a, ctx, cache, snapshot)

    candidates: list[CandidatePackage] = []
    for target in targets:
        variant1_players = _build_outgoing_variant1(
            target,
            a_players_ascending,
            next_season,
            partner=partner,
            fv_a_by_player_id=fv_a,
            next_season_cap_settings=ctx.next_season_cap_settings,
            proposer_over_cap=proposer_over_cap,
            tuning=ctx.tuning,
        )
        variant1_occupying = sum(
            1 for rp in variant1_players if _occupies_next_season(rp, next_season)
        )
        throw_ins = _build_throw_ins(
            target,
            variant1_players,
            b_players_ascending,
            next_season,
            outgoing_occupying=variant1_occupying,
        )
        variant1_bundle = AssetBundle(players=variant1_players)
        incoming_bundle = AssetBundle(players=(target, *throw_ins))
        candidates.append(
            CandidatePackage(
                team_a_id=proposer_id,
                team_b_id=partner_id,
                incoming_a=incoming_bundle,
                outgoing_a=variant1_bundle,
            )
        )

        if top_pick is not None:
            # No variant2-duplicates-variant1 guard here (Nit 3, critic
            # round 17 -- a prior guard comparing `_asset_key` of the two
            # bundles was dead code, removed rather than fixed on
            # reflection): `variant2_bundle` always carries `picks=
            # (top_pick,)` and `variant1_bundle` always carries `picks=
            # ()` (this branch's own `if top_pick is not None` guard is
            # what makes variant2 exist at all), so the two bundles can
            # never be equal -- there is no duplication case for a guard
            # to catch, structurally, not just at today's constants.
            variant2_bundle = AssetBundle(players=variant1_players, picks=(top_pick,))
            candidates.append(
                CandidatePackage(
                    team_a_id=proposer_id,
                    team_b_id=partner_id,
                    incoming_a=incoming_bundle,
                    outgoing_a=variant2_bundle,
                )
            )

    return tuple(candidates)


# ---------------------------------------------------------------------------
# Phase 3, §11 Stage B step 6 -- build_counter (docs/design/phase-3-inter-
# manager-society.md §5.1/§5.3/§11 step 6). Reuses `_probe_package`,
# `_value_priority_orders` and `_build_throw_ins` verbatim -- no new
# acceptance arithmetic, no new predictor, exactly the reuse claim §5.1
# states. Deliberately standalone: NOT wired into `run_trade_window` --
# that is step 7's own job (§11's own step boundary). `managers/
# negotiation.py`/`managers/memory.py` are untouched by this step.
# ---------------------------------------------------------------------------

COUNTER_SEARCH_WIDTH = 3
"""Bounds the number of real `evaluate_trade` calls one `build_counter`
call may spend (§5.1 step 4) -- a cost constant, never a quality one, and
the only place in this constructor where an approximation (the `gap_A'`
ranking heuristic) can still cost a real repair. **Service-scoped**
(placed beside `TRADE_TARGETS_PER_PARTNER`/`_MAX_OUTGOING_PLAYERS`, not
under `managers/`), so declaring it does NOT bump `GM_TUNING_VERSION`
(§3's module layout)."""


def _give_less_candidates(
    outgoing: Sequence[RosterPlayer],
) -> list[tuple[tuple[RosterPlayer, ...], tuple[RosterPlayer, ...]]]:
    """§5.1 step 1's corrected enumeration -- **every non-empty proper
    subset** of `outgoing`, with no exclusion by construction-time
    provenance (round 13's provenance-keyed exclusion was withdrawn:
    `_build_outgoing_variant1` returns a bare `tuple(chosen)` with no such
    marker to key on, §5.1 (b)/§5.3's own negative property). Pure
    enumeration, no probe call here -- that is pass 2's job.

    Returns `[(survivors, dropped), ...]` -- `survivors` is `outgoing`
    with the drop set `dropped` removed, **order-preserved** (the
    exactness note closing §5.1's withdrawal block: summing in
    construction's own order is what keeps `_probe_package` exact to
    `1e-9`). Exactly `2^N - 2` entries for `N = len(outgoing) >= 2` -- 2 at
    `N = 2`, 6 at `N = 3` (T3, §11 step 6) -- and `[]` for `N < 2` (no
    non-empty proper subset exists; §4.2's own give-less availability rule
    already requires `|O| >= 2`, and this function is what makes "no legal
    move" fall out of the enumeration itself rather than a special case)."""
    n = len(outgoing)
    if n < 2:
        return []
    candidates: list[tuple[tuple[RosterPlayer, ...], tuple[RosterPlayer, ...]]] = []
    for mask in range(1, (1 << n) - 1):
        dropped = tuple(rp for i, rp in enumerate(outgoing) if mask & (1 << i))
        survivors = tuple(rp for i, rp in enumerate(outgoing) if not (mask & (1 << i)))
        candidates.append((survivors, dropped))
    return candidates


def _rank_give_less_candidates(
    outgoing: Sequence[RosterPlayer],
    target: RosterPlayer,
    *,
    partner: _PartnerTerms,
    incoming_pick_value: float,
    next_season: int,
    next_season_cap_settings: LeagueCapSettings,
    proposer_over_cap: bool,
    tuning: GmTuning,
    fv_a_by_player_id: Mapping[int, float],
    loyalty_a: float,
    score_a: float,
    s_out_a: float,
) -> list[tuple[tuple[float, float, tuple[int, ...]], tuple[RosterPlayer, ...], _PackageProbe]]:
    """§5.1's give-less passes 1-3: enumerate every non-empty proper
    subset (pass 1, no call), probe every survivor with the exact B-side
    verdict as a hard admission gate (pass 2, §5.1 step 2), then rank the
    probe-admitted survivors by `gap_A'` (pass 3). **Ranking never
    excludes** (§5.1 step 3, §5.3's own negative invariant): a negative
    `gap_A'` ranks last, it is never dropped from the returned list.
    Returns every probe-admitted candidate, sorted ascending by rank key
    (best -- i.e. highest `gap_A'`, then highest probe `value_gap`, then
    lowest sorted dropped-id tuple -- first). Split out of `build_counter`
    so its own ranking-inclusion property is directly inspectable by a
    test without paying for `build_counter`'s pass-4 real `evaluate_trade`
    calls (§11 step 6's B6 coverage)."""
    ranked: list[
        tuple[tuple[float, float, tuple[int, ...]], tuple[RosterPlayer, ...], _PackageProbe]
    ] = []
    for survivors, dropped in _give_less_candidates(outgoing):
        probe = _probe_package(
            target,
            survivors,
            partner=partner,
            incoming_pick_value=incoming_pick_value,
            next_season=next_season,
            next_season_cap_settings=next_season_cap_settings,
            proposer_over_cap=proposer_over_cap,
            tuning=tuning,
        )
        if not (
            probe.matching_ceiling_ok
            and probe.matching_floor_ok
            and probe.value_gap >= 0.0
        ):
            continue
        # gap_A' ranks; it never excludes. Every term here is arithmetic
        # the seed package's own DecisionRecord/fv map already holds --
        # no new call.
        d_score_a = sum(loyalty_a * fv_a_by_player_id[x.id] for x in dropped)
        s_out_a_prime = s_out_a - sum(fv_a_by_player_id[x.id] for x in dropped)
        threshold_a_prime = max(tuning.min_abs_gain, tuning.min_rel_gain * s_out_a_prime)
        gap_a_prime = (score_a + d_score_a) - threshold_a_prime
        key = (-gap_a_prime, -probe.value_gap, tuple(sorted(x.id for x in dropped)))
        ranked.append((key, survivors, probe))
    ranked.sort(key=lambda item: item[0])
    return ranked


@dataclass
class _Ch2MarginObservation:
    """Step 13's optional out-parameter for `build_counter` (§8.3/§11 step
    13, channel 2's flip counter) -- lets the caller observe, WITHOUT
    altering `build_counter`'s own return contract or its own admission
    decision, which candidate index (in the axis's own real, bounded
    search order) would have satisfied the pre-step-13 `0.0` stop bar
    versus the live `self_margin` bar, on the ONE real walk each axis
    already performs. `build_counter` itself never reads these fields back
    -- this is purely a side-channel recording of an observation the real
    walk already makes while deciding its own, unchanged return value.
    Mirrors `_NegotiationTally`'s own "small mutable accumulator threaded
    through by the caller, never returned" shape, so a caller that does
    not care about channel 2 (every pre-step-13 test, every non-`_run_
    negotiation` caller) never has to touch it -- `None` by default."""

    baseline_index: int | None = None
    """First index (in the axis's own bounded search order --
    `ranked[:COUNTER_SEARCH_WIDTH]` for give-less, `ratio_order` for
    ask-more, both zero-based) whose real candidate satisfies the pre-
    step-13 `gap >= 0.0` / `value_gap >= 0.0` bar -- independent of
    `self_margin`, and independent of whether that candidate is the one
    actually admitted. `None` if no candidate within the search width ever
    meets it."""
    live_index: int | None = None
    """First index whose real candidate satisfies the LIVE `self_margin`
    bar -- the same index `build_counter` actually stops/returns at (or
    `None`, matching a `None` return). Always `>= baseline_index` when
    both exist (`self_margin >= 0.0`, so a higher bar can only be met at
    the same position or later); can be `None` while `baseline_index` is
    not (the margin pushed the stop point past the end of the search
    width). Always exactly equal to `baseline_index` when `self_margin ==
    0.0` -- the two bars are then the same bar."""


def build_counter(
    package: CandidatePackage,
    record_a: DecisionRecord,
    record_b: DecisionRecord,
    axis: str,
    *,
    team_a: TeamSnapshot,
    partner: _PartnerTerms,
    fv_a_by_player_id: Mapping[int, float],
    incoming_pick_value: float,
    snapshot: LeagueSnapshot,
    ctx: GmContext,
    self_margin: float = 0.0,
    ch2_observation: _Ch2MarginObservation | None = None,
    pick_slot_ranks: Mapping[int, int] | None = None,
) -> tuple[CandidatePackage, DecisionRecord] | None:
    """§5.1's counter constructor, both axes, cell-selected by the caller
    (`axis`) -- §4.2's `respond()`/step-7's negotiation loop determines
    *which* axis and *whether* one is even available; this function's own
    job is only to search it. Returns `(counter, record_a_on_counter)` on
    success (the real `DecisionRecord` A's own `evaluate_trade` call
    produced on the winning candidate -- the same "unpack both, never a
    bare return" convention `construct_offer` already uses), or `None` if
    no legal move exists on this axis, or every probed/evaluated candidate
    was refused (§11 step 6's "no legal move" test: this must read as *no
    counter*, never an empty one).

    `package`/`record_a`/`record_b` are the SEED turn's own triple --
    `record_a` A's real acceptance decision on `package`, `record_b` B's
    (§4.1's `NegotiationTurn` shape). `team_a` must be `package.team_a_id`'s
    own roster; `partner` must be `package.team_b_id`'s own `_PartnerTerms`
    (the same structure `propose_packages` already builds once per ordered
    pair) -- both asserted below rather than trusted, since a caller
    passing the wrong side's snapshot would silently corrupt every
    downstream legality check. `fv_a_by_player_id` is A's own valuation of
    (at least) A's whole roster -- "arithmetic the constructor already
    holds" per §5.1 step 3, i.e. `propose_packages`'s own `fv_a` map, not
    re-derived here. `incoming_pick_value` is mandatory (no default) and
    reused unchanged on every probe call on both axes (§5.1's "mandatory
    on both axes, not optional" -- Decision P3-11 freezes the pick set for
    the negotiation's whole life, so this is the one scalar callers must
    thread through every turn). `self_margin` defaults to `0.0` -- a no-op
    hook for step 13's channel 2 (§8.3): "it changes only the
    **constructing side's own** stop condition" -- the give-less axis's
    constructing side is A (its final real-`evaluate_trade` stop reads
    `self_margin`); the ask-more axis's constructing side is B (its
    probe-based walk stop reads `self_margin`), so A's own final
    `evaluate_trade` check on that axis uses A's plain acceptance bar
    (`verdict == "accept"`, i.e. `gap >= 0`) rather than `self_margin` --
    `self_margin` is never applied to the side that is not doing the
    constructing, on either axis.

    **Legality before success on every stop condition (Decision P3-6's
    illegal-stop trap, §5.3's own restatement).** An `illegal:*`
    `DecisionRecord` carries `score = threshold = 0.0`
    (`trade.py:648-652`), so `gap = 0.0 >= self_margin = 0.0` would satisfy
    a gap-only stop test on the very first illegal candidate. Every real
    `evaluate_trade` result below is checked `not verdict.startswith(
    "illegal:")` before its gap is read at all -- never after.

    `ch2_observation` (§11 step 13) is an optional, caller-owned side
    channel (see `_Ch2MarginObservation`'s own docstring) that this
    function fills in with the baseline (`0.0`-bar)/live (`self_margin`-
    bar) candidate indices from whichever axis's real search actually ran
    -- observed inline during the ONE real walk each axis already performs
    below, never a second, independently re-derived walk (a real risk this
    codebase has hit bugs from before: two walks over the same search
    space can drift). It never changes this function's own return value or
    stop condition on any axis -- callers that pass `None` (every caller
    before step 13) see byte-identical behavior.

    **No candidate is excluded by any arithmetic other than the real
    `_probe_package` (the B-side verdict, exact) and the real
    `evaluate_trade` (the A-side verdict, exact) -- §5.3's own negative
    invariant.** The `gap_A'` ranking score below (give-less axis, pass 3)
    only orders the probe-admitted survivors; a negative `gap_A'` ranks
    last and is never dropped. `COUNTER_SEARCH_WIDTH` bounds how many
    ranked survivors reach a real `evaluate_trade`, which is a cost bound,
    not an exclusion rule.
    """
    if axis not in ("give_less", "ask_more"):
        raise ValueError(f"build_counter: unknown axis {axis!r}")
    if team_a.id != package.team_a_id:
        raise ValueError("build_counter: team_a must be the seed package's own team_a_id")
    if partner.team_id != package.team_b_id:
        raise ValueError("build_counter: partner must be the seed package's own team_b_id")

    outgoing = package.outgoing_a.players
    target = package.incoming_a.players[0]
    next_season = ctx.next_season
    proposer_over_cap = ctx.room_by_team[team_a.id] < 0
    philosophy_a = ctx.philosophy_by_team[team_a.id]
    window_a = ctx.window_by_team[team_a.id]
    loyalty_a = PROFILES[philosophy_a].loyalty
    score_a = record_a.score
    s_out_a = sum(t.input_value for t in record_a.terms if t.label.startswith("out:"))

    def _evaluate_a(
        outgoing_players: Sequence[RosterPlayer], throw_ins: Sequence[RosterPlayer]
    ) -> tuple[CandidatePackage, DecisionRecord]:
        """One real `trade_decisions.evaluate_trade` call on A's side --
        the sole A-side arbiter this constructor ever uses (§5.1's own
        cost note: `_probe_package` calls replace `evaluate_trade` calls,
        never the other way around). Pick tuples on both legs are carried
        forward from `package` **unchanged** -- Decision P3-11's pick
        invariance, asserted rather than merely intended by construction
        (§5.3's pick-invariance invariant)."""
        incoming_bundle = AssetBundle(
            players=(target, *throw_ins), picks=package.incoming_a.picks
        )
        outgoing_bundle = AssetBundle(
            players=tuple(outgoing_players), picks=package.outgoing_a.picks
        )
        record = trade_decisions.evaluate_trade(
            team_a,
            incoming_bundle,
            outgoing_bundle,
            philosophy_a,
            window_a,
            league_rng_seed=ctx.league_rng_seed,
            season=ctx.season,
            phase=ctx.phase,
            cap_settings=ctx.cap_settings,
            next_season=next_season,
            next_season_cap_settings=ctx.next_season_cap_settings,
            next_season_room_dollars=ctx.room_by_team[team_a.id],
            rookie_scale=ctx.rookie_scale,
            league_snapshot=snapshot,
            tuning=ctx.tuning,
            pick_slot_ranks=pick_slot_ranks,
        )
        counter = CandidatePackage(
            team_a_id=package.team_a_id,
            team_b_id=package.team_b_id,
            incoming_a=incoming_bundle,
            outgoing_a=outgoing_bundle,
        )
        return counter, record

    if axis == "give_less":
        # Passes 1-3 -- enumerate, probe (hard gate), and rank (never
        # excludes) -- extracted to its own function so §11 step 6's B6
        # test can inspect the full ranked candidate list directly,
        # including negative-`gap_A'` survivors, without paying for pass
        # 4's real `evaluate_trade` calls.
        ranked = _rank_give_less_candidates(
            outgoing,
            target,
            partner=partner,
            incoming_pick_value=incoming_pick_value,
            next_season=next_season,
            next_season_cap_settings=ctx.next_season_cap_settings,
            proposer_over_cap=proposer_over_cap,
            tuning=ctx.tuning,
            fv_a_by_player_id=fv_a_by_player_id,
            loyalty_a=loyalty_a,
            score_a=score_a,
            s_out_a=s_out_a,
        )

        # Pass 4 -- spend the width budget in rank order, one real
        # evaluate_trade per candidate, verdict-before-gap (Decision
        # P3-6/C16's illegal-stop trap).
        #
        # §11 step 13's `baseline_index` is observed inline, on this same
        # walk, independent of `self_margin` -- the first LEGAL candidate
        # (Decision P3-6's illegal-stop discipline applies identically to
        # the observation as to the real stop condition below) whose own
        # `gap >= 0.0`. It never changes which candidate this loop returns
        # or when it returns it.
        baseline_index: int | None = None
        for idx, (_key, survivors, probe) in enumerate(ranked[:COUNTER_SEARCH_WIDTH]):
            counter, record = _evaluate_a(survivors, probe.throw_ins)
            gap = record.score - record.threshold
            legal = not record.verdict.startswith("illegal:")
            if baseline_index is None and legal and gap >= 0.0:
                baseline_index = idx
            if legal and gap >= self_margin:
                if ch2_observation is not None:
                    ch2_observation.baseline_index = baseline_index
                    ch2_observation.live_index = idx
                return counter, record
        if ch2_observation is not None:
            ch2_observation.baseline_index = baseline_index
            ch2_observation.live_index = None
        return None

    # axis == "ask_more" -- B is the actor, growing O rather than
    # enumerating subsets of it (§5.1: "growing a set was never tried
    # during construction," so this is a sequential walk, not an
    # exhaustive enumeration -- T3 is give-less-only for exactly this
    # reason). Candidates are A's roster players not already in O.
    #
    # §5.1's own mnemonic for the walk order ("most value to me per unit
    # of cost to him," me=B, him=A) reduces to the identical ratio
    # give-less's own mnemonic does ("most value to B per unit of cost to
    # A") -- both name the same two sides in the same numerator/
    # denominator roles, so this call passes `_value_priority_orders` the
    # same map assignment `_build_outgoing_variant1` already uses
    # (`fv_a_by_player_id` = cost-to-A, `fv_b_by_player_id` = value-to-B).
    # A literal parameter swap would instead filter the pool on `fv_A >
    # 0` (helps A, not B) and order tier-0 by cost-to-A descending among
    # players B does not value at all -- backwards for an axis whose only
    # goal is clearing B's bar at minimum extra cost to A. Round 30
    # confirms this directly (critic B8): the "swap" instruction in
    # earlier design prose was wrong, not merely unclear -- the correct
    # call is the unswapped one used here, because "most value to B per
    # unit cost to A" already IS the ask-more axis's own want.
    outgoing_ids = {rp.id for rp in outgoing}
    candidates_pool = [rp for rp in team_a.roster if rp.id not in outgoing_ids]
    ratio_order, _delivery_order = _value_priority_orders(
        candidates_pool,
        fv_a_by_player_id=fv_a_by_player_id,
        fv_b_by_player_id=partner.fv_by_player_id,
    )

    # Round 30 correction (critic B1): each trial is `O ∪ {x_i}` for a
    # single asset from `ratio_order`, probed INDEPENDENTLY against the
    # fixed, unmodified base `outgoing` -- never a cumulative growth of
    # the base across trials. A prior build round grew a running `grown`
    # list, which could permanently commit to an early accepted-but-
    # insufficient candidate and never probe a later single-asset
    # candidate that alone would have cleared B's bar.
    final_probe: _PackageProbe | None = None
    final_trial: list[RosterPlayer] | None = None
    # §11 step 13's `baseline_index`/`live_index`, observed inline on this
    # same walk (never a second, independently re-derived one) -- see
    # `_Ch2MarginObservation`'s own docstring. `baseline_index` is the
    # first ceiling/floor-admitted candidate's own `value_gap >= 0.0`,
    # independent of `self_margin`; `live_index` is set only at the real
    # break below, so it is `None` on any path that never breaks.
    baseline_index: int | None = None
    live_index: int | None = None
    if len(outgoing) < _MAX_OUTGOING_PLAYERS:
        for idx, c in enumerate(ratio_order):
            trial = [*outgoing, c]
            probe = _probe_package(
                target,
                trial,
                partner=partner,
                incoming_pick_value=incoming_pick_value,
                next_season=next_season,
                next_season_cap_settings=ctx.next_season_cap_settings,
                proposer_over_cap=proposer_over_cap,
                tuning=ctx.tuning,
            )
            if not (probe.matching_ceiling_ok and probe.matching_floor_ok):
                # Same three-part admission rule as give-less -- skip and
                # keep scanning `ratio_order`, never a stop condition
                # (mirrors `_run_value_phase`'s own ceiling-skip
                # discipline).
                continue
            if baseline_index is None and probe.value_gap >= 0.0:
                baseline_index = idx
            if probe.value_gap >= self_margin:
                # B is the actor on this axis -- `self_margin` gates B's
                # own stop condition here, exactly as §8.3 states
                # ("changes only the constructing side's own stop
                # condition"). Stop at the FIRST admitted single-asset
                # candidate in ratio_order -- never accumulate.
                final_probe = probe
                final_trial = trial
                live_index = idx
                break

    if ch2_observation is not None:
        ch2_observation.baseline_index = baseline_index
        ch2_observation.live_index = live_index

    if final_probe is None or final_trial is None:
        return None

    counter, record = _evaluate_a(final_trial, final_probe.throw_ins)
    # A is NOT the constructing side on this axis, so its own stop
    # condition reads A's plain acceptance bar, never `self_margin`
    # (§8.3's "only the constructing side's own stop condition") --
    # `verdict == "accept"` already carries "not illegal" (an illegal
    # record's verdict starts with "illegal:", never "accept"), so this
    # single comparison is Decision P3-6/C16's illegal-stop check and the
    # gap check at once.
    if record.verdict == "accept":
        return counter, record
    return None


# ---------------------------------------------------------------------------
# Phase 3, §11 Stage B step 7 -- the negotiation loop itself (docs/design/
# phase-3-inter-manager-society.md §4.1/§4.2/§11 step 7). `NegotiationTurn`/
# `Negotiation` are §4.1's own pseudocode shape, adapted to what this module
# actually has on hand: `situation` is `NegotiationSituation` below (a
# lightweight, SERVICES-scoped stand-in for the ten-token `SituationToken`
# enum `managers/voice/frame.py` builds at step 14 -- see this section's own
# note on why nine of the ten tokens are derivable now and the tenth is not).
# `phrase`/`phrase_speaker_id`/`opening_phrase` are wired at §11 step 15 --
# see `_run_negotiation`'s own dialogue block, below the loop's outcome
# determination -- and stay `None` on every call that omits `phrase_library`
# (the byte-identity default, Decision P3-20).
# `MAX_NEGOTIATION_ROUNDS` is declared HERE, not in `managers/negotiation.py`
# (Decision P3-8) -- a services-scoped constant, so `test_constant_
# inventory.py` sees no new `managers/`-scoped surface from this step and no
# `GM_TUNING_VERSION` bump is needed.
# ---------------------------------------------------------------------------

MAX_NEGOTIATION_ROUNDS = 3
"""Decision P3-8 -- round 0 (the initiator's opening), rounds 1-2 (up to
two counters), round 3 (final answer only -- `COUNTER` structurally
unavailable once `rounds_remaining <= 0`). A plain module-level constant
here (services), never a `managers/`-scoped one, specifically so M29's
control pass can call `run_trade_window(..., max_negotiation_rounds=0)`
as a PARAMETER without editing this constant -- editing it would bump
`GM_TUNING_VERSION` and re-arm M14's live SE gate for no reason."""


class NegotiationSituation(StrEnum):
    """Nine of the ten `SituationToken` values `managers/voice/frame.py`
    built at step 14, named here because §4.2's own prose already uses
    these exact names for the finer-grained accept/counter/expire split
    `NegotiationStance` alone cannot carry (`ACCEPT` splits by round,
    `COUNTER` splits by axis, `EXPIRE` splits by cause). **Deliberately not
    the full ten-token enum**: the tenth token (`open`) needed the
    phrase-trigger vocabulary (philosophy/window/h2h context this module
    has no way to supply), which steps 14/15 have since built --
    reconciling the two enums remains future work (`SituationToken` itself
    is the ten-token superset this module's own values map onto 1:1 via
    `SituationToken(situation.value)`, used at the dialogue call sites
    below), not attempted here, exactly the trap this module's own
    `managers/negotiation.py` sibling already declined for the full
    `NegotiationFrame`."""

    ACCEPT_ROUND0 = "accept_round0"
    ACCEPT_AFTER_COUNTER = "accept_after_counter"
    REJECT_ILLEGAL = "reject_illegal"
    REJECT_DEADLOCK = "reject_deadlock"
    REJECT_TOO_FAR = "reject_too_far"
    COUNTER_GIVE_LESS = "counter_give_less"
    COUNTER_ASK_MORE = "counter_ask_more"
    EXPIRE_ROUNDS = "expire_rounds"
    EXPIRE_STALLED = "expire_stalled"


class NegotiationOutcome(StrEnum):
    """§4.1's own four-member `Negotiation.outcome` codomain, verbatim."""

    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class NegotiationTurn:
    """§4.1's `NegotiationTurn` pseudocode, field-for-field. `phrase` is
    this turn's own OUTCOME line (§11 step 15) -- one per turn, spoken by
    whichever side `_outcome_phrase_speaker_id` names, never `None` once
    `phrase_library` is supplied to `_run_negotiation`; stays `None` on
    every call that omits it (the byte-identity default, Decision P3-20).
    `phrase_speaker_id` names which side's own record `phrase`'s frame was
    derived from -- not derivable from `actor_id` alone (§11 step 15's own
    finding: the outcome speaker may be either side, independent of who
    just moved)."""

    round_index: int
    actor_id: int
    """Whoever's move put `package` on the table this turn -- the
    initiator for round 0, whichever side's `build_counter` call produced
    this turn's package otherwise."""
    package: CandidatePackage
    record_a: DecisionRecord
    record_b: DecisionRecord
    stance: negotiation.NegotiationStance
    situation: NegotiationSituation
    added: tuple[str, ...]
    removed: tuple[str, ...]
    phrase: PhraseSelection | None = None
    phrase_speaker_id: int | None = None


@dataclass(frozen=True)
class Negotiation:
    """§4.1's `Negotiation` pseudocode, field-for-field. Scoped to one seed
    package and lives entirely inside one `run_trade_window` call -- never
    persisted past it (no consumer needs the full per-turn record before
    steps 10-13's head-to-head memory; this step's own metrics are all
    scalar tallies, computed live as each `Negotiation` completes).
    `opening_phrase` is the ONE `open`-situation line this negotiation ever
    produces (§11 step 15) -- spoken by the initiator (`initiator_id`) off
    round 0's own `record_a`, suppressed when round 0 terminates
    `REJECT_ILLEGAL` (see `_run_negotiation`'s own dialogue block for why).
    `None` on every call that omits `phrase_library`."""

    initiator_id: int
    responder_id: int
    seed_subject: str
    frozen_picks: tuple[int, ...]
    turns: tuple[NegotiationTurn, ...]
    outcome: NegotiationOutcome
    opening_phrase: PhraseSelection | None = None


def _asset_keys(package: CandidatePackage) -> frozenset[str]:
    """A stable asset-key set for `NegotiationTurn.added`/`removed` --
    `_bundle_subject`'s own `pN`/`kN` convention, over BOTH legs of the
    package (a counter can change either A's outgoing set or B's throw-ins,
    which live on `incoming_a`)."""
    ids = [f"p{rp.id}" for rp in (*package.outgoing_a.players, *package.incoming_a.players)]
    ids += [f"k{p.id}" for p in (*package.outgoing_a.picks, *package.incoming_a.picks)]
    return frozenset(ids)


def _classify_cell(verdict_a: str, verdict_b: str) -> str:
    """Step 1's own five-cell partition (`artifacts/scratch_phase3_step1_
    census.py::_classify_cell`, reproduced verbatim so this window's live
    census is directly comparable to step 1's committed Output 0) --
    round-0-only, read off the raw verdict pair before any gap/round
    check. Legality first (Decision P3-6's ordering, restated here since
    this function reads verdicts directly rather than through `respond()`)."""
    if verdict_a.startswith("illegal:") or verdict_b.startswith("illegal:"):
        return "illegal"
    if verdict_a == "accept" and verdict_b == "accept":
        return "both_accept"
    if verdict_a == "reject" and verdict_b == "accept":
        return "A_reject_B_accept"
    if verdict_a == "accept" and verdict_b == "reject":
        return "A_accept_B_reject"
    if verdict_a == "reject" and verdict_b == "reject":
        return "both_reject_legal"
    raise ValueError(f"unreachable verdict pair: {verdict_a!r}, {verdict_b!r}")


@contextlib.contextmanager
def _count_real_evaluations():
    """A per-call-scope spy on `trade_decisions.evaluate_trade`, yielding a
    one-element list whose `[0]` is the live call count. This is the only
    way to get an EXACT `evaluations_performed` count (Decision P3-12)
    that includes `build_counter`'s own internal search calls without
    reopening that already-closed/tested function's return contract to
    thread a counter through it (§13.3's own cost model gives an ESTIMATE,
    `2 + 4*c*R̄` -- an upper bound used to argue `MAX_NEGOTIATION_ROUNDS`,
    not a substitute for measuring the real count `M30` gates on).
    `trade_decisions.evaluate_trade` is looked up by attribute at every
    call site (`_evaluate_a`'s closure included), so patching the module
    attribute for this context's lifetime is visible to every caller,
    direct or nested, with no other code change. `run_trade_window` is
    synchronous and single-threaded, so this is safe for exactly the
    scope it is used at (one call, restored in `finally` even on an
    exception) -- it must never be left installed across two overlapping
    `run_trade_window` calls."""
    counter = [0]
    original = trade_decisions.evaluate_trade

    def _counted(*args, **kwargs):
        counter[0] += 1
        return original(*args, **kwargs)

    trade_decisions.evaluate_trade = _counted
    try:
        yield counter
    finally:
        trade_decisions.evaluate_trade = original


def _evaluate_proposer_side(
    team_a: TeamSnapshot,
    a_id: int,
    package: CandidatePackage,
    *,
    snapshot: LeagueSnapshot,
    ctx: GmContext,
    pick_slot_ranks: Mapping[int, int] | None,
) -> DecisionRecord:
    """The proposer's own `evaluate_trade` call -- extracted from
    `_evaluate_both_sides` (Decision P5-56) so the AI-to-human divert
    (`run_trade_window`'s candidate loop, below) can evaluate only its own
    side, never the human partner's, without a second copy-pasted
    fifteen-keyword-argument call site (Decision P2-33/P5-5's ban on a
    hand-reimplemented twin). `_evaluate_both_sides` below calls this for
    its own `record_a` half; there is exactly one implementation, so the
    two can never disagree. A pure, order-preserving extraction -- no
    argument, no value and no call order changes versus what this
    function's body did inline before -- proven by the byte-identical
    `seeds/gm_decision_log_digest.json` re-run this commit's own proof
    obligation already requires."""
    return trade_decisions.evaluate_trade(
        team_a,
        package.incoming_a,
        package.outgoing_a,
        ctx.philosophy_by_team[a_id],
        ctx.window_by_team[a_id],
        league_rng_seed=ctx.league_rng_seed,
        season=ctx.season,
        phase=ctx.phase,
        cap_settings=ctx.cap_settings,
        next_season=ctx.next_season,
        next_season_cap_settings=ctx.next_season_cap_settings,
        next_season_room_dollars=ctx.room_by_team[a_id],
        rookie_scale=ctx.rookie_scale,
        league_snapshot=snapshot,
        tuning=ctx.tuning,
        pick_slot_ranks=pick_slot_ranks,
    )


def _evaluate_both_sides(
    team_a: TeamSnapshot,
    team_b: TeamSnapshot,
    a_id: int,
    b_id: int,
    package: CandidatePackage,
    *,
    snapshot: LeagueSnapshot,
    ctx: GmContext,
    pick_slot_ranks: Mapping[int, int] | None,
) -> tuple[DecisionRecord, DecisionRecord]:
    """One turn's `(record_a, record_b)` pair -- the exact two `evaluate_
    trade` calls Phase 2's own round-0-only loop already made, reused
    verbatim for every turn (round 0 or a counter's own re-evaluation).
    `package` is always expressed from the initiator's own perspective
    (§4.1), so B's own call swaps `incoming_a`/`outgoing_a`, exactly as
    Phase 2's loop already did. `record_a` is `_evaluate_proposer_side`,
    unchanged in every argument and every value (Decision P5-56)."""
    record_a = _evaluate_proposer_side(
        team_a, a_id, package, snapshot=snapshot, ctx=ctx, pick_slot_ranks=pick_slot_ranks
    )
    record_b = trade_decisions.evaluate_trade(
        team_b,
        package.outgoing_a,
        package.incoming_a,
        ctx.philosophy_by_team[b_id],
        ctx.window_by_team[b_id],
        league_rng_seed=ctx.league_rng_seed,
        season=ctx.season,
        phase=ctx.phase,
        cap_settings=ctx.cap_settings,
        next_season=ctx.next_season,
        next_season_cap_settings=ctx.next_season_cap_settings,
        next_season_room_dollars=ctx.room_by_team[b_id],
        rookie_scale=ctx.rookie_scale,
        league_snapshot=snapshot,
        tuning=ctx.tuning,
        pick_slot_ranks=pick_slot_ranks,
    )
    return record_a, record_b


@dataclass
class _NegotiationTally:
    """Scalar counters one `_run_negotiation` call folds into, mirroring
    `TradeWindowResult`'s own new fields one-for-one -- kept as its own
    small mutable accumulator (rather than returning a tuple of six ints)
    so a caller cannot silently transpose two same-typed counters at the
    call site."""

    turns_evaluated: int = 0
    counter_opportunities: int = 0
    counters_admitted: int = 0
    expired_stalled: int = 0
    depth_violations: int = 0
    """M25 -- Family-1-grade hard invariant, checked live rather than
    reconstructed from a persisted turn log (no `Negotiation` is kept past
    the call that builds it, §11 step 7's own scope: nothing downstream of
    this step needs the full per-turn record yet). Incremented once per
    negotiation whose own terminal `round_index` exceeds the `max_
    negotiation_rounds` it was actually run under, or that reaches this
    tally's own bookkeeping in a non-terminal state (structurally
    unreachable given `_run_negotiation`'s own `while` loop only exits on
    a terminal outcome -- checked anyway, since M25's whole job is to
    catch exactly this kind of "should be impossible" regression)."""
    outcome_census: dict[str, int] = field(default_factory=dict)
    variant_pairs_compared: int = 0
    variant_verdict_flips: int = 0
    """§11 step 11's own required, diagnostic-only measurement (design doc
    line ~2938 item 1): "the variant-1/variant-2 verdict-flip rate is
    measured live and reported." `propose_packages` builds, per target, a
    `variant1` bundle (no pick) and -- whenever a `top_pick` exists -- a
    `variant2` bundle (same players, plus `top_pick`), both sharing the
    exact same `incoming_a` object instance (see that function's own
    loop). Paired here by `id(cand.incoming_a)` within one ordered pair's
    own candidate list (never across ordered pairs -- a fresh pairing dict
    is scoped per `(a_id, b_id)` call), comparing each pair's own ROUND-0
    `record_b.verdict` (the responder's verdict on the FIRST turn, before
    any counter) -- the same population and same reading Decision P3-34's
    now-withdrawn bootstrap input measured (§7's "critic round 4 measured
    B's round-0 verdict across 86,362 real variant-1/variant-2 target
    pairs"). `variant_pairs_compared` is the denominator (every target
    that had both a variant1 and a variant2 candidate this window);
    `variant_verdict_flips` counts how many of those pairs disagreed.
    Nothing downstream gates on this ratio (Decision P3-34 is withdrawn) --
    reported only, exactly like `expired_stalled_count` above."""

    channel3_decision_sites: int = 0
    """M28 ch3's denominator (Decision P3-36/P3-38, §9.2, step 12) --
    count of turns where §4.2's rule reached cell 3 ("both legal, exactly
    one side short") and evaluated `gap < -counter_band`, regardless of
    the turn's eventual stance (REJECT_TOO_FAR, EXPIRE or COUNTER all
    reach this evaluation identically -- it is the FIRST check in cell
    3's own ladder). Reported only; `counted_in_go` for M28 does not flip
    until step 17 (all three channels)."""
    channel3_reachability: int = 0
    """Of `channel3_decision_sites` above, how many had a nonzero `shade`
    for the acting side's own memory of its partner (§9.2's ch3
    reachability row: "sites where shade != 0 for the acting pair") --
    the conjunct step 17 reads (`flips >= 1 OR reachability == 0`) is
    this step's own denominator for the 0.5%-inertness reading, not a
    gate built here."""
    channel3_flips: int = 0
    """Of `channel3_decision_sites` above, how many turns had `(gap <
    -band_base) != (gap < -band_eff)` -- Decision P3-36's decision-site
    counterfactual: did shading change whether THIS turn's own `gap <
    -counter_band` check would have judged the package a far reject,
    compared against that same site's own unshaded baseline
    (`band_base = COUNTER_BAND_MULT * threshold`, `shade = 0`)."""
    channel3_max_observed: float = 0.0
    """Decision P3-41 (§9.2's `detail.max_observed` row, §11 step 17 item
    2) -- running MAX of `abs(shade)` over every channel-3 decision site
    this window, read off the identical `shade` value `channel3_
    reachability` already keys on at the same call site. `max_observed ==
    0.0` on a run whose census shows real memory rows is the never-
    written signature for THIS branch's own predicate input, mirroring
    channel 1's `executions`/`far_reject_streak` reading one field over."""
    # B3 fix (critic round) -- `mean_abs_shade` is NOT accumulated here.
    # §9.2 (line ~2165) lists it among "the demoted diagnostics ... across
    # the 870 rows," the same population `p_bar`/`Σw`/`n_eff` already use
    # -- not the per-turn ch3 decision-site population this tally counts.
    # See `_channel3_mean_abs_shade_inputs`, computed once per window
    # directly from `ctx.head_to_head` in `run_trade_window`, below.

    channel2_decision_sites: int = 0
    """M28 ch2's denominator (Decision P3-28's replacement, §8.3, §11 step
    13) -- incremented once per REAL `build_counter` call this window (the
    `stance == COUNTER, not stalled` branch of `_run_negotiation`'s own
    loop), regardless of whether the search admits a candidate. Unlike
    channel 3's decision sites (every turn reaching cell 3's own
    evaluation, whether or not a counter is attempted), channel 2 only has
    a decision site where a real search actually runs -- at `max_
    negotiation_rounds = 0` `build_counter` never runs at all, so this
    field's own control-pass reading is genuinely `0` (§11 step 13's own
    "this step, not the M29 control pass, is where channel 2's non-vacuous
    zero-reading has to come from" -- the real zero is this step's counters-
    on, `head_to_head = None` pass, not the control pass)."""
    channel2_reachability: int = 0
    """Of `channel2_decision_sites` above, how many had a positive `shade`
    (`> 0.0`, not merely nonzero -- §8.3's `max(shade, 0.0)` floor means a
    non-positive shade can never move the stop point) for the acting
    side's own memory of its partner, read off the identical `shade` value
    channel 3 already computed at the same `_run_negotiation` call site
    (§9.2's ch2 reachability row)."""
    channel2_flips: int = 0
    """Of `channel2_decision_sites` above, how many had `build_counter`'s
    own `_Ch2MarginObservation.baseline_index != live_index` on the axis
    that actually ran -- the search's first `0.0`-bar candidate differed
    from its first `self_margin`-bar candidate, including one existing and
    the other not (Decision P3-36's decision-site counterfactual, channel
    2's own version)."""
    channel2_max_observed: float = 0.0
    """Decision P3-41 (§11 step 17 item 2) -- running MAX of `abs(shade)`
    over every channel-2 decision site this window, read off the same
    `shade` value channel 3 already computed at the shared `_run_
    negotiation` call site (one memory fact, read by both channels)."""

    channel1_up_decision_sites: int = 0
    """M28 ch1-up's denominator (Decision P3-36/P3-38/P3-40, §9.2, §11
    step 17) -- every ORDERED PAIR, every trade window is a decision site
    for channel 1 (unlike ch2/ch3, this has nothing to do with negotiation
    turns: the target count is decided once, before any package for that
    pair is even constructed). Incremented once per `run_trade_window`
    ordered-pair iteration, from `_channel1_target_count`'s own call site,
    below -- identically to `channel1_down_decision_sites`, since both
    branches share one denominator (`same site`, §9.2's own table)."""
    channel1_up_reachability: int = 0
    """Of `channel1_up_decision_sites` above, sites where `up`'s own
    predicate held AND `down`'s did not (Decision P3-40's precedence rule,
    verbatim): decayed `executions >= 1.0` and `far_reject_streak <
    FUTILITY_STREAK`, read directly off `ctx.head_to_head`'s own row for
    this pair -- **never from which branch the production ladder call
    actually took**, so an unwired `up` reader still raises this count
    (the whole point of a reachability counter, §9.2)."""
    channel1_up_flips: int = 0
    """Of `channel1_up_decision_sites` above, sites where THIS window's
    real `_channel1_target_count` call returned `3` -- the up flip,
    Decision P3-36's decision-site counterfactual: `live_target_count ==
    3` against the `head_to_head = None` baseline of `2`."""
    channel1_down_decision_sites: int = 0
    """M28 ch1-down's denominator -- see `channel1_up_decision_sites`
    above; identically incremented at the same call site (both branches
    share one decision-site population, §9.2's own table)."""
    channel1_down_reachability: int = 0
    """Of `channel1_down_decision_sites` above, sites where `down`'s own
    predicate held: decayed `far_reject_streak >= FUTILITY_STREAK`, read
    directly off `ctx.head_to_head`'s own row for this pair. `down` has no
    higher-precedence branch above it, so its reachability is just its own
    raw predicate (Decision P3-40) -- unlike `channel1_up_reachability`,
    no second condition applies."""
    channel1_down_flips: int = 0
    """Of `channel1_down_decision_sites` above, sites where THIS window's
    real `_channel1_target_count` call returned `1` -- the down flip.
    Mutually exclusive with `channel1_up_flips` by construction (the
    ladder returns exactly one value per site): a site with `live == 2`
    flips neither counter."""
    channel1_up_max_observed: float = 0.0
    """Decision P3-41 (§9.2's `detail.max_observed` row, §11 step 17 item
    2, critic finding B7-3) -- running MAX of decayed `executions` read
    off `ctx.head_to_head`'s own row at every channel-1 decision site this
    window (the same value `channel1_up_reachability`'s own predicate
    reads). `max_observed == 0.0` on a run whose M18 reads real trades/
    season is the never-written signature for `executions` specifically:
    the trade happened and the memory did not hear about it."""
    channel1_down_max_observed: float = 0.0
    """Decision P3-41 -- running MAX of decayed `far_reject_streak` read
    off `ctx.head_to_head`'s own row at every channel-1 decision site this
    window (the same value `channel1_down_reachability`'s own predicate
    reads). `max_observed == 0.0` on a run with real observed far rejects
    (`far_rejects_observed >= 1`, below) is the never-written signature
    for `far_reject_streak` specifically."""
    far_rejects_observed: int = 0
    """Decision P3-41's triage counter (§11 step 17's own assertion,
    critical trap disclosed at `_is_far_negotiation_reject`'s own
    docstring) -- count of real negotiations this window whose terminal
    state is an opponent merit-reject with `gap < -counter_band`,
    incremented at `_run_negotiation`'s own call site, INDEPENDENTLY of
    `_record_negotiation_in_h2h`'s own write (never derived from the h2h
    writer's own output), so a broken/disabled writer cannot zero this
    counter the same way it would zero `far_reject_streak` itself."""

    phrase_selections_total: int = 0
    """§11 step 16 -- M26 (`phrase_fallback_share`)/M27 (`voice_
    differentiation`)'s shared denominator (Decision P3-19/P3-31, §9.1).
    Every real `_select_negotiation_phrase`/`select_phrase` call this
    negotiation makes increments this once, for BOTH the `open` line and
    the outcome line -- folded in by `_record_phrase_selection`, the
    shared funnel every call site in `_run_negotiation` below routes
    through (mirroring `_channel1_target_count`'s own call-site
    convention: the selection call itself stays pure, the CALLER folds
    its result into `tally`)."""
    phrase_fallback_selections: int = 0
    """Of `phrase_selections_total` above, how many selected a fallback
    template -- `template_id.startswith("fallback_")`, the exact
    convention `phrase_service.py::_validate_fallback_coverage` already
    enforces at load time (every fallback template's own id is seeded
    with this prefix in `seeds/gm_phrases.json`), so this string check is
    safe and total over a validated library."""
    phrase_selections_by_situation: dict[str, int] = field(default_factory=dict)
    """M26's own required per-situation breakdown (§11 step 16's verify
    clause) -- `phrase_selections_total`, broken out by situation token
    string (`str(situation_token)`, the identical stringification `_select_
    negotiation_phrase`'s own RNG-salt component already uses for this
    same value)."""
    phrase_fallback_by_situation: dict[str, int] = field(default_factory=dict)
    """`phrase_fallback_selections` above, broken out the same way --
    `phrase_fallback_by_situation[situation] / phrase_selections_by_
    situation[situation]` is that situation's own fallback share, the
    number M26's `detail` reports per token."""
    phrase_selections_by_philosophy_template: dict[str, dict[str, int]] = field(
        default_factory=dict
    )
    """M27's own input (§9.1, Decision P3-31) -- keyed `philosophy.value ->
    template_id -> count`. The SPEAKER's own philosophy, read off the
    real `DecisionRecord.philosophy` the selection was actually made for
    (never `frame.philosophy` -- matching `_select_negotiation_phrase`'s
    own docstring, which requires the REAL `record_a`/`record_b`, never
    channel 3's shaded stand-in). Keyed generally, not just `star_maxer`/
    `egalitarian` (M27's own two named cohorts, §9.1), so this same field
    is reusable for any future philosophy pair a later phase might want."""


def _zero_h2h_row(league_id: int, team_id: int, opponent_id: int, season: int) -> HeadToHead:
    """A freshly-opened `(team_id, opponent_id)` row -- every counter `0.0`,
    per Decision P3-22's schema; `last_gap_by_opponent`/`last_season_
    interacted` `None` (never recorded yet); `updated_season` seeded to the
    window's own season so a row that this window touches at all carries a
    real season even if none of its writers happen to touch that field
    (they all do, in practice -- see `_record_negotiation_in_h2h`, below --
    this is just the row's starting point before any write)."""
    return HeadToHead(
        league_id=league_id, team_id=team_id, opponent_id=opponent_id,
        opened=0.0, received=0.0, accepts_by_opponent=0.0, rejects_by_opponent=0.0,
        accepts_by_self=0.0, rejects_by_self=0.0, counters_given=0.0,
        counters_received=0.0, executions=0.0, refused_illegal=0.0,
        far_reject_streak=0.0, gap_by_opponent_sum=0.0, last_gap_by_opponent=None,
        last_season_interacted=None, updated_season=season,
    )


@dataclass
class _H2HAccumulator:
    """Decision P3-22's per-window write accumulator (§11 step 11) --
    seeded from `ctx.head_to_head` (or empty, if the caller supplied
    none: the WRITER runs regardless of whether the reader side is wired
    yet, only `shade()`/`p_bar` care about `None`), threaded through every
    `_run_negotiation` call one `run_trade_window` pass makes and mutated
    in place as each negotiation/execution completes. `run_trade_window`
    reads `self.rows` back as `TradeWindowResult.head_to_head` when the
    pass finishes -- the row objects returned ARE the ones this
    accumulator mutated (no second copy), since `HeadToHead` is a plain,
    non-frozen `dataclass` (unlike most of this module's own result
    types) and every seed row was already cloned via `dataclasses.replace`
    at construction, so mutating it here never reaches back into the
    caller's own `ctx.head_to_head` mapping."""

    league_id: int
    season: int
    rows: dict[tuple[int, int], HeadToHead] = field(default_factory=dict)

    def row(self, team_id: int, opponent_id: int) -> HeadToHead:
        key = (team_id, opponent_id)
        existing = self.rows.get(key)
        if existing is not None:
            return existing
        fresh = _zero_h2h_row(self.league_id, team_id, opponent_id, self.season)
        self.rows[key] = fresh
        return fresh


def _is_far_negotiation_reject(record_b: DecisionRecord) -> bool:
    """The far-reject predicate `far_reject_streak` is keyed on (§7,
    `_record_negotiation_in_h2h`'s own docstring): a closed negotiation's
    RESPONDER did not accept, AND that same terminal `record_b`'s own gap
    read against its own `counter_band` is more negative than the
    (negated) band -- "opponent merit-rejected FAR," never merely short.
    Takes ONLY the closed negotiation's last turn's `record_b` -- the
    exact input `_record_negotiation_in_h2h`'s own branch already reads
    (never `record_a`, never the axis/cell that produced the rejection;
    that function's own docstring works out why `record_b` alone is
    correct here).

    **Extracted into a standalone, module-level pure function and called
    from TWO INDEPENDENT places, deliberately (§11 step 17's triage gate,
    Decision P3-41) -- this is the one place in this step's own spec with
    a real trap, so it is documented carefully:** (a) inside
    `_record_negotiation_in_h2h` itself, replacing its own inline `gap_
    opp_signed < -band_b` check with a call to this shared definition
    (one predicate, not two that could silently drift); (b) at `_run_
    negotiation`'s own call site, immediately BESIDE -- never inside --
    wherever it calls the h2h-writing function, incrementing `tally.far_
    rejects_observed` there, independently of whatever the writer does
    with the same boolean. **This independence is the entire point**: if
    `_record_negotiation_in_h2h` were ever broken, disabled, or silently
    made a no-op, `far_rejects_observed` must still read nonzero on a run
    where far rejects demonstrably occurred, so the triage assertion
    (`far_rejects_observed >= 1 AND ch1_down.max_observed == 0.0`) can
    catch exactly that bug. A `far_rejects_observed` computed FROM the
    writer's own output (e.g. counting rows whose `far_reject_streak`
    just moved) would read zero under precisely the "writer is
    broken/disabled" bug this triage exists to catch, making the triage
    pass vacuously -- the same B6-2 shape Decision P3-41 already names
    for the reachability conjunct itself.

    **Disclosed limitation, matching this design's own established
    honesty convention for exactly this class of guard (§9.2's B6-2):**
    this closes the "the WRITER is broken" gap, never the "the PREDICATE
    is wrong" gap -- a wrong shared predicate (as opposed to a broken
    writer) still zeroes both `far_rejects_observed` and the h2h row's
    own `far_reject_streak` identically, since both call sites now share
    this one implementation. That residual gap is accepted here, exactly
    as B6-2 accepted the mirror-image gap for the reachability conjunct
    (a wrong reachability formula would still agree with a wrong
    production ladder)."""
    return (
        record_b.verdict != "accept"
        and (record_b.score - record_b.threshold) < -negotiation.counter_band(record_b)
    )


def _record_negotiation_in_h2h(negotiation_result: Negotiation, h2h: _H2HAccumulator) -> None:
    """Decision P3-22/P3-35's write path over one completed `Negotiation`
    -- `initiator_id`/`responder_id` are `Negotiation`'s own field names
    (confirmed as the real initiator/responder by `propose_packages`'s own
    `CandidatePackage(team_a_id=proposer_id, ...)` construction, preserved
    unchanged through every counter `build_counter` emits) and are read
    directly rather than re-derived.

    **Illegal termination (Decision P3-35) is the one branch that returns
    early.** `reject_illegal` is always exactly the negotiation's only
    turn (`negotiation.respond()` checks legality before anything else,
    unconditionally terminal) -- checking the LAST turn's `situation`
    rather than only the first is equivalent and reads more directly at
    the call site below. Only `refused_illegal` (initiator's own row)
    moves; `opened` is never incremented for it, and nothing on the
    responder's row moves either.

    **Every other branch reads `last.record_a`/`last.record_b` -- the
    terminal turn's own evaluated records -- never the terminal
    `NegotiationSituation` token, per round 12's C4 (§7): `REJECT_TOO_FAR`
    fires from two branches (`gap < -counter_band`, the FAR cause, and "no
    legal repair axis available," the NEAR cause) and the token alone
    cannot distinguish them.

    **`far_reject_streak`/`gap_by_opponent_sum`/`last_gap_by_opponent`
    branch on `b_accepts` (the RESPONDER's own verdict) alone -- never on
    `negotiation.respond()`'s cell-3 "one side short" actor selection.**
    This is a correction (round 13, critic finding B1): an earlier build
    mirrored `respond()`'s own `actor_record = ra if not a_accepts else
    rb` dispatch here, on the theory that `far_reject_streak` should read
    "the same gap `respond()` itself read at this turn." That theory is
    wrong for this field specifically. `respond()`'s cell-3 actor is
    whichever side is SHORT and must move to keep the negotiation alive --
    on the give-less axis (`a_accepts=False, b_accepts=True`) that actor
    is A, the initiator itself. But `row_ir` (`h2h.row(a_id, b_id)`) is
    A's memory of B, and §7 defines `far_reject_streak` as counting "an
    `opened` negotiation ending in an **opponent** merit reject" -- B's
    rejection, never A's own shortfall. When `b_accepts=True` on that
    axis, B accepted; nothing about B's disposition failed, so reading
    A's own (possibly deeply negative) gap and incrementing on it inverts
    the statistic entirely: it would count "A itself couldn't find a
    package A liked" as "B rejected A by a wide margin," which is not
    just wrong but backwards from what actually happened. §7's own
    property 2 makes this the correct read mechanically, not just by
    argument: "any opponent accept resets the streak to 0... this is a
    derivation, not a preference."

    The corrected rule, keyed on `b_accepts` alone:

    - `b_accepts == True` (opponent accepted, on EITHER axis, including
      `a_accepts and b_accepts`) -- reset to `0.0`. No gap read at all;
      an accept carries no rejection information.
    - `b_accepts == False` (opponent rejected -- covers the ask-more axis
      `a_accepts=True, b_accepts=False`, AND `REJECT_DEADLOCK` where both
      reject) -- always read `rb`'s OWN gap against `rb`'s OWN
      `counter_band`, never `ra`'s, regardless of which axis or cell
      produced the rejection: increment on `gap < -band` (far), else
      reset (near). `REJECT_DEADLOCK` needs no special case once the
      condition is keyed on `b_accepts` alone -- it already falls into
      this same branch, evaluated identically.

    `gap_by_opponent_sum`/`last_gap_by_opponent` update in lockstep with
    the same `b_accepts == False` condition -- both are defined over "any
    opponent merit-rejection" (§7), the same population the increment/
    reset condition above reads.

    **`accepts_by_opponent`/`rejects_by_opponent` (initiator's row) and
    `accepts_by_self`/`rejects_by_self` (responder's row) are both read
    off `record_b.verdict` alone** -- `HeadToHead`'s own schema names them
    "the opponent's final-turn verdict," and `record_b` is always the
    RESPONDER's own evaluation (`_evaluate_both_sides` keys `record_b` to
    `b_id`/`team_b` unconditionally). This is why the same event writes
    both rows in lockstep (§10 Tier 2's coverage table): `record_b.verdict
    == "accept"` is simultaneously "did the opponent accept my offer" (on
    the initiator's `(I, R)` row) and "did I (the responder) accept the
    offer made to me" (on the responder's `(R, I)` row) -- one real-world
    fact, two labels. `record_a.verdict` (the initiator's OWN decision) is
    never read by this function at all -- `far_reject_streak`'s corrected
    rule above is keyed on `record_b.verdict` alone, and no other counter
    here needs A's own verdict either.

    **`counters_given`/`counters_received` move together, one increment
    per `COUNTER`-stance turn, on the initiator's/responder's row
    respectively** -- the design states the two counters must move
    together but does not name which of the two paired counters belongs
    to which directed row; this is a judgment call (flagged in this
    step's own report), the natural symmetric reading given the schema
    carries exactly one `counters_given`/`counters_received` pair per row,
    not one per axis/actor."""
    a_id, b_id = negotiation_result.initiator_id, negotiation_result.responder_id
    turns = negotiation_result.turns
    last = turns[-1]
    row_ir = h2h.row(a_id, b_id)  # (I)nitiator -> (R)esponder

    if last.situation == NegotiationSituation.REJECT_ILLEGAL:
        row_ir.refused_illegal += 1.0
        row_ir.last_season_interacted = h2h.season
        row_ir.updated_season = h2h.season
        return

    row_ri = h2h.row(b_id, a_id)  # (R)esponder -> (I)nitiator
    row_ir.opened += 1.0
    row_ri.received += 1.0

    rb = last.record_b
    b_accepts = rb.verdict == "accept"

    if b_accepts:
        row_ir.accepts_by_opponent += 1.0
        row_ri.accepts_by_self += 1.0
    else:
        row_ir.rejects_by_opponent += 1.0
        row_ri.rejects_by_self += 1.0

    if b_accepts:
        # B1 fix (round 13, critic finding B1) -- ANY opponent accept
        # resets the streak, on either axis, with no gap read at all.
        # `a_accepts` is irrelevant here: even on the give-less axis
        # (`a_accepts=False`), B accepting means B did not reject, so
        # there is nothing for `far_reject_streak` -- an OPPONENT-reject
        # counter -- to count.
        row_ir.far_reject_streak = 0.0
    else:
        # B rejected on merit -- the only population this field counts.
        # Covers both the ask-more axis (`a_accepts=True`) and
        # REJECT_DEADLOCK (`a_accepts=False`) identically: always read
        # `rb`'s OWN gap against `rb`'s OWN band, never `ra`'s.
        gap_opp_signed = rb.score - rb.threshold
        if _is_far_negotiation_reject(rb):
            row_ir.far_reject_streak += 1.0
        else:
            row_ir.far_reject_streak = 0.0
        gap_opp = abs(gap_opp_signed)
        row_ir.gap_by_opponent_sum += gap_opp
        row_ir.last_gap_by_opponent = gap_opp

    for turn in turns:
        if turn.stance == negotiation.NegotiationStance.COUNTER:
            row_ir.counters_given += 1.0
            row_ri.counters_received += 1.0

    row_ir.last_season_interacted = h2h.season
    row_ir.updated_season = h2h.season
    row_ri.last_season_interacted = h2h.season
    row_ri.updated_season = h2h.season


def _substitute_shaded_record(
    actor_record: DecisionRecord, band_eff: float, band: float
) -> DecisionRecord:
    """Channel 3's synthetic-record substitution (Decision P3-26/§8.2,
    step 12) -- `threshold` rescaled so `counter_band()`'s own unmodified
    arithmetic naturally evaluates to `band_eff` at `respond()`, `score`
    shifted by the identical delta so `gap = score - threshold` is
    UNCHANGED. See `_run_negotiation`'s own call site for the full "why a
    substituted record, not a `respond()` parameter" argument.

    **B5 fix (critic round) -- DISCLOSED, not silent: this record's
    `terms` are NOT rescaled to match its shifted `score`/`threshold`, so
    `Σ terms == score` (`explain.py::DecisionRecord.reconstruction_
    error()`/`.reconstructs()`) is VIOLATED whenever `shade != 0` (i.e.
    `band_eff != band`) -- up to ~25 VP of reconstruction error on
    realistic inputs. This is safe ONLY because of how tightly scoped the
    substitution is: the returned object is passed to exactly one
    `negotiation.respond()` call and discarded -- `respond()` reads only
    `.verdict`/`.score`/`.threshold`, never sums `.terms` and never calls
    `.reconstructs()` -- and `turns`/`h2h`/`build_counter` all keep
    reading the REAL, untouched `record_a`/`record_b`, never this object.
    If a future caller ever needs `.reconstructs()` on a `respond()`
    argument, or to persist/log this synthetic record, this violation
    becomes load-bearing and `terms` must be rescaled too -- see
    `test_head_to_head_writer.py::
    test_channel3_synthetic_record_violates_reconstruction_identity_when_shaded`
    for this exact tradeoff asserted mechanically (`reconstructs() is
    False` here when `shade != 0`, `True` when `shade == 0`)."""
    shaded_threshold = band_eff / negotiation.COUNTER_BAND_MULT
    return replace(
        actor_record,
        threshold=shaded_threshold,
        score=actor_record.score - (actor_record.threshold - shaded_threshold),
    )


# ---------------------------------------------------------------------------
# §11 step 15 -- dialogue selection wired into the live negotiation loop.
# One outcome line per `NegotiationTurn`, plus exactly one `open` line per
# `Negotiation` (this dispatch's own architect round; see docs/design/
# phase-3-inter-manager-society.md §11 step 15's own dispatch brief for the
# full worked argument). Both helpers below are pure functions of arguments
# already on hand inside `_run_negotiation`'s loop -- no new bookkeeping.
# ---------------------------------------------------------------------------


def _outcome_phrase_speaker_id(
    *,
    situation: NegotiationSituation,
    record_a: DecisionRecord,
    record_b: DecisionRecord,
    a_id: int,
    b_id: int,
    actor_id: int,
    short_side_id: int | None,
) -> int:
    """The OUTCOME line's speaker for one turn -- three branches, checked
    in this order, all reachable only from state `_run_negotiation`'s loop
    already computes this turn:

    1. `REJECT_ILLEGAL` -> whichever side's OWN record is the illegal one
       (`record.verdict.startswith("illegal:")`), `record_a` checked
       first. This is what guarantees the selected record actually short-
       circuits inside `derive_frame` (so `illegal_token` is populated) --
       picking the other side produces a `reject_illegal` frame with
       `illegal_token=None`, which can never match a triggered per-
       `illegal_token` template and silently inflates the fallback rate.
    2. `short_side_id is not None` -> the short side itself -- §4.2 cell
       3's population (`counter_*`, `reject_too_far`, `expire_*`): exactly
       one side was short of the deal it needed, and that side is the one
       whose own record the outcome line is spoken from.
    3. Otherwise (`accept_*`, `reject_deadlock` -- both cell-3 predicates
       false, so neither side was "short") -> the NON-actor: `b_id` if
       `actor_id == a_id` else `a_id`. `actor_id` is the turn's own
       `NegotiationTurn.actor_id` (whoever's move put `package` on the
       table this turn), never re-derived here."""
    if situation == NegotiationSituation.REJECT_ILLEGAL:
        return a_id if record_a.verdict.startswith("illegal:") else b_id
    if short_side_id is not None:
        return short_side_id
    return b_id if actor_id == a_id else a_id


def _select_negotiation_phrase(
    *,
    record: DecisionRecord,
    speaker_id: int,
    listener_id: int,
    situation_token: SituationToken,
    stance: negotiation.NegotiationStance,
    round_index: int,
    ctx: GmContext,
    seed_subject: str,
    phrase_library: LoadedPhraseLibrary,
    phrase_use_counts: dict[int, dict[str, int]],
) -> PhraseSelection:
    """One `derive_frame` + `select_phrase` call, for exactly one speaker's
    own record -- a frame is inherently one-sided (`philosophy`/`window`/
    the share fields all come off one record), so this is called once per
    selection, never once per turn (a round-0 legal turn makes TWO calls,
    `open` and the round's own outcome, both off `record_a` since the
    initiator always opens -- see `_run_negotiation`'s own dialogue block).

    `record` must be the REAL `record_a`/`record_b` the turn actually
    carries, never a channel-3 synthetic/shaded stand-in (`respond_record_
    a`/`respond_record_b`) -- those deliberately violate `Σ terms ==
    score` and would corrupt `top_in_share`/`cap_relief_share`. Every
    caller in `_run_negotiation` below passes the loop's own `record_a`/
    `record_b`, never the shaded pair.

    `phrase_use_counts` is `{speaker team_id: {template_id: prior use
    count}}`, scoped to ONE negotiation (not one window -- §6.3 step 5's
    own "in this window" wording is corrected by this dispatch, see the
    design doc's own appended correction: per-window scoping at the
    corpus's real per-team-per-window selection volume erases authored
    template weights via the repeat penalty's own decay long before a
    single negotiation could ever say the same line three times, which is
    what the mechanism exists to prevent).

    The RNG salt is Decision P3-20's own 7-tuple PLUS the situation token
    itself (as a plain string, `str(situation_token)`) -- this dispatch's
    own correction: on the modal give-less round-0 path the SAME speaker
    (`a_id`) makes two selections in the same round (`open` and `counter_
    give_less`), and without the appended 8th component both draws would
    start from an identical salt/stream. `situation_token` here is the
    token being PASSED IN for this selection, never `frame.situation` --
    Decision P3-15's illegality short-circuit may have rewritten the
    frame's own `situation` to `reject_illegal`, and using the post-
    rewrite value would reintroduce exactly the collision this fix exists
    to prevent."""
    head_to_head_row = (
        ctx.head_to_head.get((speaker_id, listener_id)) if ctx.head_to_head is not None else None
    )
    frame = derive_frame(
        record, stance=stance, situation=situation_token, round_index=round_index,
        head_to_head=head_to_head_row, p_bar=ctx.h2h_base_accept_rate,
    )
    rng = rng_for(
        ctx.league_rng_seed, "phrase", speaker_id, ctx.season, ctx.phase,
        seed_subject, round_index, str(situation_token),
    )
    use_counts = phrase_use_counts.setdefault(speaker_id, {})
    selection = select_phrase(
        frame, phrase_library.templates, use_counts=use_counts,
        repeat_penalty=phrase_library.repeat_penalty, rng=rng,
    )
    use_counts[selection.template_id] = use_counts.get(selection.template_id, 0) + 1
    return selection


def _record_phrase_selection(
    tally: _NegotiationTally,
    selection: PhraseSelection,
    *,
    situation_token: SituationToken,
    philosophy: GmPhilosophy,
    team_id: int,
) -> None:
    """§11 step 16 -- the shared funnel every real `_select_negotiation_
    phrase` call site in `_run_negotiation` routes its own return value
    through, folding M26/M27's live counters into `tally` (Decision P3-19/
    P3-31, §9.1). Mirrors `_channel1_target_count`'s own call-site
    convention exactly: `_select_negotiation_phrase` itself stays a pure
    selection call with no `tally` parameter, and the CALLER (never the
    pure helper) accumulates -- never accumulated inside `_select_
    negotiation_phrase` itself.

    `situation_token` is the token the selection was actually made FOR
    (never re-derived from `selection` itself, which carries no situation
    of its own) -- stringified with the identical `str(...)` conversion
    `_select_negotiation_phrase`'s own RNG salt already uses for this same
    value, so M26's per-situation `detail` keys match that convention.

    `philosophy` is the SPEAKER's own philosophy for this selection --
    every call site below passes the real `record_a`/`outcome_speaker_
    record` (never `respond_record_a`/`respond_record_b`, channel 3's
    shaded stand-ins) `.philosophy` field, matching `_select_negotiation_
    phrase`'s own docstring requirement that `record` is always the real
    speaker's own record.

    `team_id` is that SAME speaker's own team id -- every call site below
    passes the identical `a_id`/`outcome_phrase_speaker_id` value it
    already computed to select `philosophy` from. **No longer folded into
    a team-keyed accumulation** (the team-id-keyed within-cohort split
    this fed was withdrawn in favor of a binomial-thinning split over the
    cohort-pooled distribution, §11 Stage D step 16's second fix round --
    `_within_cohort_binomial_thin_tv`; the field this populated,
    `phrase_selections_by_philosophy_team_template`, had zero remaining
    readers and was removed)."""
    tally.phrase_selections_total += 1
    situation = str(situation_token)
    tally.phrase_selections_by_situation[situation] = (
        tally.phrase_selections_by_situation.get(situation, 0) + 1
    )
    if selection.template_id.startswith("fallback_"):
        tally.phrase_fallback_selections += 1
        tally.phrase_fallback_by_situation[situation] = (
            tally.phrase_fallback_by_situation.get(situation, 0) + 1
        )
    per_philosophy = tally.phrase_selections_by_philosophy_template.setdefault(
        philosophy.value, {}
    )
    per_philosophy[selection.template_id] = per_philosophy.get(selection.template_id, 0) + 1


def _run_negotiation(
    cand: CandidatePackage,
    *,
    a_id: int,
    b_id: int,
    team_a: TeamSnapshot,
    team_b: TeamSnapshot,
    partner: _PartnerTerms,
    fv_a_by_player_id: Mapping[int, float],
    incoming_pick_value: float,
    snapshot: LeagueSnapshot,
    ctx: GmContext,
    pick_slot_ranks: Mapping[int, int] | None,
    max_negotiation_rounds: int,
    tally: _NegotiationTally,
    h2h: _H2HAccumulator,
    phrase_library: LoadedPhraseLibrary | None = None,
) -> Negotiation:
    """Runs one seed package's negotiation to a terminal state (§4.2's
    total per-turn rule via `negotiation.respond()`, `build_counter()` for
    the actual repair search), folding every scalar this step's Family-5
    metrics need into `tally` as it goes -- and, new at §11 step 11,
    folding Decision P3-22's head-to-head counter deltas into `h2h` once
    the negotiation reaches its terminal state (`_record_negotiation_in_
    h2h`, above). Round 0 with `max_negotiation_rounds=0` reduces to
    exactly Phase 2's own accept/reject read (trap #5/§11 step 7's own
    verify): `respond()`'s `rounds_remaining <= 0` branch fires before the
    axis-availability check, so a would-be `COUNTER` at round 0 becomes
    `EXPIRE` instead and is simply never added to `mutual` -- identical to
    Phase 2's own "not both accept, so not mutual" outcome for that same
    package.

    `phrase_library` -- `None` (the default) means dialogue is OFF and
    every `NegotiationTurn.phrase`/`.phrase_speaker_id`/`Negotiation.
    opening_phrase` this call produces stays `None`, with the phrase-
    selection block below never entered at all: no `derive_frame`/
    `select_phrase`/`rng_for("phrase", ...)` call happens, so this is
    byte-identical to pre-step-15 behavior (Decision P3-20's own
    invariant, §11 step 15's verify clause). Supplying a real
    `LoadedPhraseLibrary` (`services/phrase_service.py::load_library()`)
    turns dialogue on for this negotiation only."""
    package = cand
    record_a, record_b = _evaluate_both_sides(
        team_a, team_b, a_id, b_id, package, snapshot=snapshot, ctx=ctx,
        pick_slot_ranks=pick_slot_ranks,
    )
    round0_cell = _classify_cell(record_a.verdict, record_b.verdict)
    tally.outcome_census[round0_cell] = tally.outcome_census.get(round0_cell, 0) + 1
    seed_subject = record_a.subject
    """Captured once, here -- round 0's own value, matching `Negotiation.
    seed_subject` below exactly (`record_a.subject` can change round to
    round as the package changes, so this must be captured before the
    turn loop, not re-read from the loop's own per-round `record_a`).
    Also the RNG salt's own `seed_subject` component (§11 step 15) for
    every phrase selection this negotiation makes, at every round."""

    frozen_picks = tuple(
        p.id for p in (*package.outgoing_a.picks, *package.incoming_a.picks)
    )
    turns: list[NegotiationTurn] = []
    turn_actor_id = a_id
    stall_previous_actor: int | None = None
    stall_previous_gap = 0.0
    round_index = 0
    outcome = NegotiationOutcome.OPEN
    negotiation_opening_phrase: PhraseSelection | None = None
    phrase_use_counts: dict[int, dict[str, int]] = {}
    """§11 step 15 -- `{speaker team_id: {template_id: prior use count}}`,
    scoped to this ONE negotiation (not this window -- see `_select_
    negotiation_phrase`'s own docstring for the corrected scope and why)."""

    while True:
        rounds_remaining = max_negotiation_rounds - round_index

        # Channel 3 (Decision P3-26/§8.2, step 12) -- determine whether
        # this turn reaches §4.2's cell 3 ("both legal, exactly one side
        # short") BEFORE calling `respond()`, duplicating exactly the
        # two-line legality/accept test `respond()` runs internally --
        # the same precedent `_record_negotiation_in_h2h` already sets
        # for independently re-deriving "which side, which axis" rather
        # than trusting a returned token that cannot distinguish REJECT_
        # TOO_FAR's two causes. This lets the shaded band be computed and
        # substituted into THIS SAME turn's own real decision (the
        # mechanism itself), not merely recorded after the fact -- §8.2's
        # "the band decides whether a counter is attempted" is only true
        # if the real decision reads it.
        #
        # **Why a substituted record, not a `band_override` parameter on
        # `respond()`.** A new keyword parameter under `dba2/managers/` --
        # even a `None`-defaulted one -- is itself a new tracked
        # declaration under Decision P3-30's mechanical inventory guard
        # (`test_constant_inventory.py`, site (c): every `FunctionDef`
        # keyword default), which forbids any addition after step 4's one
        # `GM_TUNING_VERSION` bump; confirmed by measurement, not
        # assumption -- adding `band_override` there trips the committed
        # snapshot. Instead, `respond()` and `counter_band()` stay BYTE-
        # IDENTICAL to their pre-step-12 selves, and the acting side's own
        # record is substituted with a synthetic copy: `threshold` rescaled
        # so `counter_band()`'s own unmodified arithmetic naturally
        # evaluates to `band_eff`, and `score` shifted by the identical
        # delta so `gap = score - threshold` is UNCHANGED. The synthetic
        # record is scoped to this ONE `respond()` call -- `turns`, `h2h`,
        # and `build_counter` all keep reading the REAL, unshaded
        # `record_a`/`record_b` below.
        illegal_turn = (
            record_a.verdict.startswith("illegal:") or record_b.verdict.startswith("illegal:")
        )
        a_accepts = record_a.verdict == "accept"
        b_accepts = record_b.verdict == "accept"
        cell3 = not illegal_turn and a_accepts != b_accepts

        short_side_id: int | None = None
        axis: str | None = None
        gap = 0.0
        band = 0.0
        band_eff = 0.0
        self_margin = 0.0
        shade = 0.0
        stalled = False
        respond_record_a, respond_record_b = record_a, record_b
        if cell3:
            if not a_accepts:  # (reject, accept) -- A short, give-less axis
                short_side_id, other_side_id = a_id, b_id
                axis, actor_record = "give_less", record_a
            else:  # (accept, reject) -- B short, ask-more axis
                short_side_id, other_side_id = b_id, a_id
                axis, actor_record = "ask_more", record_b
            gap = actor_record.score - actor_record.threshold
            band = negotiation.counter_band(actor_record)
            # The acting (short) side's own memory of THIS negotiation
            # partner -- `ctx.head_to_head.get(...)` returns `None` for a
            # pair with no row yet, which `memory.shade` already treats
            # identically to `n == 0` (its own documented contract), so
            # "no memory at all" and "unknown opponent" both read `shade
            # == 0.0` -> `band_eff == band` here, with no special-casing.
            h2h_row = (
                ctx.head_to_head.get((short_side_id, other_side_id))
                if ctx.head_to_head is not None else None
            )
            shade = memory.shade(h2h_row, ctx.h2h_base_accept_rate)
            band_eff = negotiation.counter_band_eff(actor_record, shade)
            # Channel 2 (Decision P3-28's replacement, §8.3, §11 step 13) --
            # the counter self-margin, read off the SAME `shade`/`actor_
            # record` channel 3 just computed above (this is one memory
            # fact, not two independent readings of it). The `max(shade,
            # 0.0)` floor is deliberate one-sidedness (§8.3): a negative
            # shade (an opponent who accepts less than average) must NOT
            # produce a negative margin -- channel 2 only ever demands
            # more, never accepts less. `shade <= 0.0` or no memory at all
            # (`shade == 0.0`, `h2h_row is None`) both give exactly `0.0`,
            # byte-identical to pre-step-13 `build_counter` behavior.
            self_margin = (
                negotiation.H2H_MARGIN_SHADE * max(shade, 0.0) * actor_record.threshold
            )

            # M28 ch3's decision-site counters (Decision P3-36/P3-38, §9.2)
            # -- reported only, nothing gates on them until step 17.
            # `mean_abs_shade` is NOT accumulated here -- its population is
            # the 870 head-to-head rows, not decision sites (B3 fix; see
            # `_channel3_mean_abs_shade_inputs`).
            tally.channel3_decision_sites += 1
            if shade != 0.0:
                tally.channel3_reachability += 1
            if (gap < -band) != (gap < -band_eff):
                tally.channel3_flips += 1
            tally.channel3_max_observed = max(tally.channel3_max_observed, abs(shade))

            if band_eff != band:
                shaded_actor_record = _substitute_shaded_record(actor_record, band_eff, band)
                if axis == "give_less":
                    respond_record_a = shaded_actor_record
                else:
                    respond_record_b = shaded_actor_record

        stance = negotiation.respond(
            respond_record_a, respond_record_b,
            rounds_remaining=rounds_remaining,
            max_outgoing_players=_MAX_OUTGOING_PLAYERS,
        )

        if stance == negotiation.NegotiationStance.COUNTER:
            # B2 fix (critic round, channel-3 scope discipline) -- `band`
            # here MUST stay the real, UNSHADED `counter_band(actor_record)`
            # computed at line ~2372, never `band_eff`. Channel 3 (Decision
            # P3-26/§8.2) is scoped to exactly one decision: "the band
            # decides whether a counter is attempted" (`REJECT_TOO_FAR` vs
            # `COUNTER`, via the substituted record `respond()` sees above).
            # Stall detection is a separate mechanism `is_stalled` itself
            # documents as reading "the acting side's own `counter_band
            # (actor_record)` at the CURRENT turn" -- the unshaded one --
            # and channel 3 does not get a second, silent vote here just
            # because a band-shaped float is lying around. Do not
            # reintroduce `band = band_eff` above this call.
            stalled = negotiation.is_stalled(
                actor=short_side_id, previous_actor=stall_previous_actor,
                previous_gap=stall_previous_gap, gap=gap, band=band,
            )
            if round_index == 0:
                tally.counter_opportunities += 1

        next_package: CandidatePackage | None = None
        next_record_a: DecisionRecord | None = None
        next_record_b: DecisionRecord | None = None

        if stance == negotiation.NegotiationStance.ACCEPT:
            situation = (
                NegotiationSituation.ACCEPT_ROUND0 if round_index == 0
                else NegotiationSituation.ACCEPT_AFTER_COUNTER
            )
            outcome = NegotiationOutcome.ACCEPTED
        elif stance == negotiation.NegotiationStance.REJECT_ILLEGAL:
            situation, outcome = NegotiationSituation.REJECT_ILLEGAL, NegotiationOutcome.REJECTED
        elif stance == negotiation.NegotiationStance.REJECT_DEADLOCK:
            situation, outcome = NegotiationSituation.REJECT_DEADLOCK, NegotiationOutcome.REJECTED
        elif stance == negotiation.NegotiationStance.REJECT_TOO_FAR:
            situation, outcome = NegotiationSituation.REJECT_TOO_FAR, NegotiationOutcome.REJECTED
        elif stance == negotiation.NegotiationStance.EXPIRE:
            situation, outcome = NegotiationSituation.EXPIRE_ROUNDS, NegotiationOutcome.EXPIRED
        elif stalled:
            situation, outcome = NegotiationSituation.EXPIRE_STALLED, NegotiationOutcome.EXPIRED
            tally.expired_stalled += 1
        else:
            # stance == COUNTER, not stalled -- attempt the real repair.
            # Channel 2 (§8.3, §11 step 13) -- `self_margin` (computed
            # above, in the `cell3` block that always runs before a
            # `COUNTER` stance is reachable) shades this search's own stop
            # condition; `ch2_observation` is this ONE call's own side-
            # channel for the baseline/live index comparison (never reused
            # across calls -- one decision site, one observation object).
            ch2_observation = _Ch2MarginObservation()
            counter_result = build_counter(
                package, record_a, record_b, axis,
                team_a=team_a, partner=partner, fv_a_by_player_id=fv_a_by_player_id,
                incoming_pick_value=incoming_pick_value, snapshot=snapshot, ctx=ctx,
                self_margin=self_margin, ch2_observation=ch2_observation,
                pick_slot_ranks=pick_slot_ranks,
            )
            # M28 ch2's decision-site counters (Decision P3-28's
            # replacement/P3-36/P3-38, §9.2) -- reported only, nothing
            # gates on them until step 17. `channel2_decision_sites` counts
            # every REAL `build_counter` call (this branch), never every
            # cell-3 turn (channel 3's own, broader population) --
            # `max_negotiation_rounds = 0` never reaches this branch at
            # all, which is exactly why this field's control-pass reading
            # is genuinely `0`, not vacuously so (§11 step 13's own note).
            tally.channel2_decision_sites += 1
            if shade > 0.0:
                tally.channel2_reachability += 1
            if ch2_observation.baseline_index != ch2_observation.live_index:
                tally.channel2_flips += 1
            tally.channel2_max_observed = max(tally.channel2_max_observed, abs(shade))
            if counter_result is None:
                # §4.2's own "no legal move available" cause of REJECT_TOO_FAR,
                # discovered by the real search rather than the structural
                # |O| check `respond()` uses -- see this step's own build
                # report for why this mapping, not a new token, is correct.
                situation = NegotiationSituation.REJECT_TOO_FAR
                outcome = NegotiationOutcome.REJECTED
            else:
                next_package, next_record_a = counter_result
                # The receiving side's own real evaluation of the emitted
                # counter (§13.3 step 2's "+1") -- ALWAYS B, regardless of
                # axis: `build_counter`'s own `_evaluate_a` always evaluates
                # from team_a's perspective (give-less: A is the actor and
                # this IS A's stop condition; ask-more: B is the actor but
                # the constructor's own final check is still "does A still
                # accept the grown package," §5.1's own ask-more docstring),
                # so the one record `build_counter` never produces is B's.
                next_record_b = trade_decisions.evaluate_trade(
                    team_b, next_package.outgoing_a, next_package.incoming_a,
                    ctx.philosophy_by_team[b_id], ctx.window_by_team[b_id],
                    league_rng_seed=ctx.league_rng_seed, season=ctx.season,
                    phase=ctx.phase, cap_settings=ctx.cap_settings,
                    next_season=ctx.next_season,
                    next_season_cap_settings=ctx.next_season_cap_settings,
                    next_season_room_dollars=ctx.room_by_team[b_id],
                    rookie_scale=ctx.rookie_scale, league_snapshot=snapshot,
                    tuning=ctx.tuning, pick_slot_ranks=pick_slot_ranks,
                )
                situation = (
                    NegotiationSituation.COUNTER_GIVE_LESS if axis == "give_less"
                    else NegotiationSituation.COUNTER_ASK_MORE
                )
                outcome = NegotiationOutcome.OPEN
                if round_index == 0:
                    tally.counters_admitted += 1

        # §11 step 15 -- dialogue. `None` when `phrase_library` is `None`
        # (the byte-identity default): this whole block is skipped, so no
        # `derive_frame`/`select_phrase`/`rng_for("phrase", ...)` call ever
        # happens on that path. `record_a`/`record_b` here are THIS turn's
        # own real records (never `respond_record_a`/`respond_record_b`,
        # channel 3's synthetic/shaded stand-ins, which are scoped to the
        # `respond()` call above and discarded).
        outcome_phrase: PhraseSelection | None = None
        outcome_phrase_speaker_id: int | None = None
        if phrase_library is not None:
            outcome_situation_token = SituationToken(situation.value)
            outcome_phrase_speaker_id = _outcome_phrase_speaker_id(
                situation=situation, record_a=record_a, record_b=record_b,
                a_id=a_id, b_id=b_id, actor_id=turn_actor_id, short_side_id=short_side_id,
            )
            # Round 0's own `open` line -- the initiator, off `record_a`,
            # suppressed when round 0 itself terminates `REJECT_ILLEGAL`
            # (that illegal round-0 record already gets rewritten to
            # `reject_illegal` by `derive_frame`'s own short-circuit
            # regardless of what situation is passed to it -- emitting a
            # separate `open` line too would produce two `reject_illegal`-
            # flavored lines from the same team in the same breath).
            # `stance` here is round 0's own resolved stance -- the
            # eventual outcome, the only value available to pass since an
            # `open` frame needs SOME `stance` and the proposer's own
            # state at open time is not yet meaningful (`phrase_service.
            # py`'s own load-time validator forbids a template from
            # triggering on `stance` for `open`, precisely because this
            # value is the outcome's, not the opener's).
            if round_index == 0 and situation != NegotiationSituation.REJECT_ILLEGAL:
                negotiation_opening_phrase = _select_negotiation_phrase(
                    record=record_a, speaker_id=a_id, listener_id=b_id,
                    situation_token=SituationToken.OPEN, stance=stance,
                    round_index=round_index, ctx=ctx, seed_subject=seed_subject,
                    phrase_library=phrase_library, phrase_use_counts=phrase_use_counts,
                )
                _record_phrase_selection(
                    tally, negotiation_opening_phrase,
                    situation_token=SituationToken.OPEN, philosophy=record_a.philosophy,
                    team_id=a_id,
                )
            outcome_speaker_record = (
                record_a if outcome_phrase_speaker_id == a_id else record_b
            )
            outcome_listener_id = b_id if outcome_phrase_speaker_id == a_id else a_id
            outcome_phrase = _select_negotiation_phrase(
                record=outcome_speaker_record, speaker_id=outcome_phrase_speaker_id,
                listener_id=outcome_listener_id, situation_token=outcome_situation_token,
                stance=stance, round_index=round_index, ctx=ctx, seed_subject=seed_subject,
                phrase_library=phrase_library, phrase_use_counts=phrase_use_counts,
            )
            _record_phrase_selection(
                tally, outcome_phrase,
                situation_token=outcome_situation_token,
                philosophy=outcome_speaker_record.philosophy,
                team_id=outcome_phrase_speaker_id,
            )

        previous_keys = _asset_keys(turns[-1].package) if turns else frozenset()
        current_keys = _asset_keys(package)
        added = tuple(sorted(current_keys - previous_keys))
        removed = tuple(sorted(previous_keys - current_keys))

        turns.append(
            NegotiationTurn(
                round_index=round_index, actor_id=turn_actor_id, package=package,
                record_a=record_a, record_b=record_b, stance=stance, situation=situation,
                added=added, removed=removed,
                phrase=outcome_phrase, phrase_speaker_id=outcome_phrase_speaker_id,
            )
        )
        tally.turns_evaluated += 1

        if outcome != NegotiationOutcome.OPEN:
            break

        # COUNTER, admitted -- advance to the next round.
        stall_previous_actor, stall_previous_gap = short_side_id, gap
        turn_actor_id = short_side_id
        round_index += 1
        package, record_a, record_b = next_package, next_record_a, next_record_b

    # M25's own hard invariant, checked live (Decision P3-19's "every turn
    # carries a situation token" is already structurally true -- every
    # `NegotiationTurn` above is built with a real `situation` value, never
    # a default -- so this is the two conditions that ARE reachable if a
    # future change to this function breaks one of its own invariants).
    if round_index > max_negotiation_rounds or outcome == NegotiationOutcome.OPEN:
        tally.depth_violations += 1

    negotiation_result = Negotiation(
        initiator_id=a_id, responder_id=b_id, seed_subject=seed_subject,
        frozen_picks=frozen_picks, turns=tuple(turns), outcome=outcome,
        opening_phrase=negotiation_opening_phrase,
    )
    _record_negotiation_in_h2h(negotiation_result, h2h)
    # Decision P3-41's triage counter (§11 step 17, item 2) -- the SAME
    # far-reject predicate `_record_negotiation_in_h2h`'s own `far_reject_
    # streak` branch evaluates, called INDEPENDENTLY here (this call never
    # reads anything the writer just wrote) so a broken/disabled writer
    # still leaves this counter correctly nonzero -- see `_is_far_
    # negotiation_reject`'s own docstring for the full argument. Excludes
    # `REJECT_ILLEGAL` identically to the writer's own early return: an
    # illegal termination's `record_b` never reaches the writer's far-
    # reject branch either, so counting it here would count an event
    # outside the population the writer itself (and the design's own §7
    # definition) counts.
    last_turn = negotiation_result.turns[-1]
    if (
        last_turn.situation != NegotiationSituation.REJECT_ILLEGAL
        and _is_far_negotiation_reject(last_turn.record_b)
    ):
        tally.far_rejects_observed += 1
    return negotiation_result


# ---------------------------------------------------------------------------
# run_trade_window
# ---------------------------------------------------------------------------

MAX_TRADES_PER_TEAM_PER_WINDOW = 3
MAX_TRADES_PER_WINDOW = 30
MAX_AI_OFFERS_PER_HUMAN_TEAM_PER_WINDOW = 3
"""Decision P5-57 -- a service-scoped cost governor, beside `MAX_TRADES_
PER_WINDOW`/`MAX_TRADES_PER_TEAM_PER_WINDOW` above (Decision P3-8's own
placement rule), never a `managers/`-scoped constant:
`tests/unit/managers/test_constant_inventory.py` walks `dba2/managers/`
only, so this constant is outside that guard and bumps no `GM_TUNING_
VERSION` (verified, not assumed -- see that test module's own directory
walk). At two AI trade windows a season (`REGULAR_SEASON`/`TRADE_
DEADLINE`, once each per Decision P5-73's cycle-ordinal rule), the
default of `3` yields at most six AI-initiated offers per human team per
season -- a trickle, not a firehose (Decision P5-83)."""


def _filler_delta(
    pre_roster: Sequence[RosterPlayer],
    outgoing_players: Sequence[RosterPlayer],
    incoming_players: Sequence[RosterPlayer],
    next_season: int,
    cap_settings: LeagueCapSettings,
) -> int:
    """`pre_filler - post_filler`, computed the exact way `trade.py::_check_
    legality`'s own `used_post` formula computes it internally (Nit 1,
    critic round 18) -- needed here so `run_trade_window`'s per-pass room
    write-back matches what `_check_legality` itself just modeled for this
    same trade, not just the raw outgoing/incoming salary delta. Reuses
    `trade._contract_covers_season` (the same next-season-contract-coverage
    basis `_check_legality`'s own filler math and `capsheet_service.py`'s
    `next_season_room_dollars` provenance both already require) rather than
    reinventing a roster-count basis here."""
    pre_contracted = sum(
        1 for rp in pre_roster if trade_decisions._contract_covers_season(rp, next_season)
    )
    outgoing_contracted = sum(
        1 for rp in outgoing_players if trade_decisions._contract_covers_season(rp, next_season)
    )
    incoming_contracted = sum(
        1 for rp in incoming_players if trade_decisions._contract_covers_season(rp, next_season)
    )
    post_contracted = pre_contracted - outgoing_contracted + incoming_contracted
    pre_filler = max(0, cap_settings.roster_min - pre_contracted) * cap_settings.min_salary
    post_filler = max(0, cap_settings.roster_min - post_contracted) * cap_settings.min_salary
    return pre_filler - post_filler


def run_trade_window(
    snapshot: LeagueSnapshot,
    ctx: GmContext,
    *,
    max_negotiation_rounds: int = MAX_NEGOTIATION_ROUNDS,
    phrase_library: LoadedPhraseLibrary | None = None,
) -> TradeWindowResult:
    """Scans every ordered team pair via `propose_packages`; each seed
    package opens exactly one negotiation (§4.1's B-1 discipline), run to a
    terminal state by `_run_negotiation` (§4.2's total per-turn rule via
    `negotiation.respond()`, `build_counter()` for the actual repair
    search); a negotiation terminating `ACCEPTED` contributes its final
    package/records to `mutual`, exactly as a Phase 2 mutual accept did.
    Selection/execution below this point is Decision P3-12/§4.4's own
    explicit "everything downstream is untouched" list -- the global sort,
    the greedy execution loop, `consumed_players`/`consumed_picks`, both
    per-team-trade caps, the `_check_legality` re-check and `_filler_
    delta`'s write-back are byte-for-byte what Phase 2 already had.

    `max_negotiation_rounds` is a PARAMETER, never a `managers/`-scoped
    constant (Decision P3-8) -- passing `0` reduces every negotiation to
    exactly Phase 2's own round-0-only accept/reject read (`respond()`'s
    `rounds_remaining <= 0` branch fires before any axis is ever built),
    which is what lets M29's control pass and this step's own byte-
    identical-to-step-2b test exist without editing a constant that would
    bump `GM_TUNING_VERSION`. See module docstring for the `cap_settings`/
    `season` judgment call.

    **Replay precondition (round 13 correction, B2) -- this function IS a
    pure function of its arguments (never mutates `ctx`, never mutates
    `snapshot`, and calling it twice with byte-identical arguments
    produces byte-identical `TradeWindowResult`s, `head_to_head` included)
    but that purity does NOT make head-to-head replay-safe on its own.**
    `TradeWindowResult.head_to_head` is a per-window DELTA-shaped write
    (this window's own `opened`/`accepts_by_opponent`/etc. increments,
    folded onto whatever `ctx.head_to_head` already carried) -- calling
    this function a SECOND time against a `ctx` that already carries the
    FIRST call's own merged result (the shape `tools/gm_harness/season.py`
    builds via `state.head_to_head.update(trade_result.head_to_head)`
    followed by `build_ctx`'s own re-read of `state.head_to_head`) re-runs
    the SAME negotiations against the SAME starting `ctx.head_to_head`
    baseline and re-applies the SAME deltas on top of a store that already
    has them, genuinely double-counting (`opened` moving 2.0 -> 4.0 on a
    real reproduction, not merely a theoretical concern). **Avoiding that
    double-count is the CALLER's responsibility, not this function's**:
    a caller (a phase-hook retry, a crash-and-resume) must not re-run a
    trade window whose result has already been merged into its own
    persistent store for the same window. This function has no way to
    detect "was this exact window already applied" from its own
    arguments alone -- `ctx.head_to_head` legitimately looks the same
    whether it reflects "state before this window" or "state after this
    window, about to be replayed by mistake." Decision P3-25's season-
    boundary DECAY applier (`memory.decay_head_to_head`/`_rows`) is a
    narrower, genuinely self-guarding case -- season transitions are
    unambiguous in a way "was this window already applied" is not -- and
    is idempotent under a replayed call by construction (see that
    function's own docstring); this function's own per-window write is
    not, and is not claimed to be.

    `phrase_library` (§11 step 15) -- `None` (the default) forwarded
    verbatim to every `_run_negotiation` call this function makes, exactly
    like `head_to_head_enabled`'s own Decision P3-48 precedent one layer
    up (`run_season`/`run_seed`, `tools/gm_harness/season.py`): every
    existing call site that omits this parameter is byte-identical to
    today's behavior (Decision P3-20), and a caller opts into dialogue by
    passing a real `services/phrase_service.py::load_library()` result."""
    cache = ValuationCache()
    pick_slot_ranks = cache.pick_slot_ranks(snapshot, ctx.tuning)
    next_season = ctx.next_season
    team_ids = [t.id for t in snapshot.teams]

    all_records: list[DecisionRecord] = []
    mutual: list[tuple[CandidatePackage, float, DecisionRecord, DecisionRecord]] = []
    packages_evaluated = 0
    closed_round_by_record_id: dict[int, int] = {}
    """M24's join key -- `id(final_record_a)` -> the `round_index` the
    negotiation that produced it closed at. `TradeExecution`'s own shape
    stays untouched (§4.4); this dict is read once, after selection, by
    `id(execution.record_a)` (§4.4/Decision P3-12's own instruction: join,
    don't add a field)."""
    pending_offer_candidates: list[PendingOffer] = []
    """Decision P5-57 -- every candidate collected by the AI-to-human
    divert below, gated/ranked/capped AFTER the greedy execution loop
    closes (so the `consumed_players`/`consumed_picks` filter sees this
    window's own real final state, not a mid-scan snapshot of it) into
    `TradeWindowResult.offers`. Empty whenever `ctx.self_managed_team_ids`
    is empty -- the divert branch below is never entered in that case."""
    offers_evaluated = 0
    offer_evaluations_performed = 0
    diverted_records: list[DecisionRecord] = []
    """Round-7 critic fix (blocking issue 1) -- every diverted candidate's
    own proposer-side record, kept OUT of `all_records` so `TradeWindow
    Result.records` keeps its `(record_a, record_b)`-pair-per-negotiation-
    turn contract intact even when this window diverts. Becomes `TradeWindow
    Result.offer_records` below, unconditionally (whether or not the
    candidate survives P5-57's four gates into `offers` -- mirrors
    `all_records`' own "every evaluated candidate" comprehensiveness for
    the negotiated path)."""
    tally = _NegotiationTally()
    # Decision P3-22's per-window write accumulator (§11 step 11), seeded
    # from a CLONE of `ctx.head_to_head`'s rows (`dataclasses.replace`,
    # never the caller's own row objects) so mutating a row here can never
    # reach back into the mapping `ctx` itself carries. Runs regardless of
    # whether `ctx.head_to_head` is `None` -- the writer is unconditional;
    # only the reader side (`shade()`/`p_bar`, steps 12/13/17) treats
    # `None` as "no memory, strict no-op".
    h2h_acc = _H2HAccumulator(
        league_id=ctx.league_id,
        season=ctx.season,
        rows={key: replace(row) for key, row in (ctx.head_to_head or {}).items()},
    )

    with _count_real_evaluations() as eval_counter:
        for a_id in team_ids:
            for b_id in team_ids:
                if a_id == b_id:
                    continue
                if a_id in ctx.self_managed_team_ids:
                    # Decision P5-44/P5-45's PROPOSER-side skip, unchanged
                    # by P5-55/P5-80/P5-91 (those decisions replace only
                    # the PARTNER-side half below) -- the AI never decides
                    # what a human-managed team offers, so this ordered
                    # pair produces no candidate, no ch1 decision-site
                    # tally entry and no `_PartnerTerms`/`fit_values`
                    # construction. Kept at this exact early placement
                    # (P5-45's own original insertion point, before any
                    # of the instrumentation below) because nothing about
                    # this half of the ruling ever changed across rounds.
                    continue
                team_a = snapshot.team_by_id(a_id)
                team_b = snapshot.team_by_id(b_id)
                # The same `_PartnerTerms`/`fv_a` construction `propose_
                # packages` does internally for this exact ordered pair
                # (§5.2) -- recomputed here, once per pair, so `build_
                # counter` has them without changing `propose_packages`'s
                # own return contract. `cache.fit_values` is memoized by
                # `(team_id, player_id)`, so this costs dict lookups only:
                # `propose_packages`'s own call below already primed every
                # entry this recomputation reads.
                all_players = (*team_a.roster, *team_b.roster)
                fv_a = cache.fit_values(team_a, all_players, ctx)
                fv_b = cache.fit_values(team_b, all_players, ctx)
                b_players_ascending = sorted(team_b.roster, key=lambda rp: (fv_b[rp.id], rp.id))
                partner = _PartnerTerms(
                    team_id=b_id,
                    loyalty=PROFILES[ctx.philosophy_by_team[b_id]].loyalty,
                    window=ctx.window_by_team[b_id],
                    room_dollars=ctx.room_by_team[b_id],
                    over_cap=ctx.room_by_team[b_id] < 0,
                    fv_by_player_id=fv_b,
                    players_ascending=tuple(b_players_ascending),
                )
                # Decision P3-34's flip-rate measurement (§11 step 11, item
                # 1) -- `propose_packages` shares one `incoming_a` object
                # instance across a target's variant1/variant2 pair, so
                # `id(cand.incoming_a)` groups them without re-deriving the
                # pairing from asset contents. Scoped fresh per ordered
                # pair: a target's variant1/variant2 never straddles two
                # different `(a_id, b_id)` calls.
                variant_round0_verdict: dict[int, str] = {}

                # M28 ch1's own decision-site tally (Decision P3-36/P3-38/
                # P3-40, §9.2, §11 step 17) -- every ordered pair, every
                # window is a decision site, independent of whether this
                # pair ever opens a negotiation. `_channel1_target_count`
                # is the SAME call `propose_packages` makes below on the
                # identical `(ctx, a_id, b_id)` inputs (a pure function of
                # its own arguments), so `target_count` here IS the real
                # call's own live value, not a re-derivation that could
                # silently disagree with it. Reachability is read directly
                # off `h2h_row`'s own two fields -- never off `target_
                # count` -- so an unwired reader branch still raises its
                # own reachability count (§9.2's own instruction: "never
                # from which branch the production ladder call actually
                # took").
                target_count, h2h_row = _channel1_target_count(ctx, a_id, b_id)
                far_reject_streak = h2h_row.far_reject_streak if h2h_row is not None else 0.0
                executions = h2h_row.executions if h2h_row is not None else 0.0
                tally.channel1_up_decision_sites += 1
                tally.channel1_down_decision_sites += 1
                down_reachable = far_reject_streak >= FUTILITY_STREAK
                up_reachable = executions >= 1.0 and not down_reachable
                if down_reachable:
                    tally.channel1_down_reachability += 1
                if up_reachable:
                    tally.channel1_up_reachability += 1
                if target_count == 1:
                    tally.channel1_down_flips += 1
                elif target_count == 3:
                    tally.channel1_up_flips += 1
                tally.channel1_up_max_observed = max(tally.channel1_up_max_observed, executions)
                tally.channel1_down_max_observed = max(
                    tally.channel1_down_max_observed, far_reject_streak
                )

                for cand in propose_packages(a_id, b_id, snapshot, ctx, cache):
                    if b_id in ctx.self_managed_team_ids:
                        # Decision P5-55/P5-56, insertion point corrected
                        # by P5-80, skip list corrected by P5-91. The AI
                        # genuinely proposes to a human partner -- this
                        # candidate really was built and really is
                        # evaluated (`offers_evaluated`/`offer_evaluations_
                        # performed` below are unconditional) -- but
                        # nothing here may resolve it automatically. No
                        # `_run_negotiation` call (that would run the
                        # human's own side to a terminal state on their
                        # behalf); no partner-side `evaluate_trade` at all
                        # (P5-56 -- no `DecisionRecord` on this path may
                        # ever carry a human team's id); no turn-record
                        # append; no `mutual` append (P5-91's positive
                        # restatement: "on the diverted branch the loop
                        # body ends after the gates and the offer append;
                        # nothing from the old `:3751` to `:3768` runs" --
                        # there is no `result` on this branch to read any
                        # of that from). The candidate becomes an offer
                        # candidate instead, gated/ranked/capped by the
                        # caller after the greedy execution loop closes
                        # (P5-57). Round-7 critic fix (blocking issue 1):
                        # a diverted candidate never opens a negotiation,
                        # so it must not increment `packages_evaluated`
                        # (that field's own "= negotiations opened"
                        # contract) and its own `record_a` must not join
                        # `all_records` (that field's own `(record_a,
                        # record_b)`-pair-per-turn contract, which
                        # `season.py::_count_mutual_accepts` asserts on --
                        # a diverted candidate has no `record_b` at all).
                        # `incoming_pick_value` is never computed for this
                        # branch either -- it exists only to feed `_run_
                        # negotiation`/`build_counter`, which this branch
                        # never calls.
                        offers_evaluated += 1
                        record_a = _evaluate_proposer_side(
                            team_a, a_id, cand,
                            snapshot=snapshot, ctx=ctx, pick_slot_ranks=pick_slot_ranks,
                        )
                        offer_evaluations_performed += 1
                        diverted_records.append(record_a)
                        pending_offer_candidates.append(
                            PendingOffer(
                                from_team_id=a_id, to_team_id=b_id, package=cand, record=record_a
                            )
                        )
                        continue
                    packages_evaluated += 1
                    incoming_pick_value = sum(
                        cache.pick_price(b_id, pick, ctx, snapshot)
                        for pick in cand.outgoing_a.picks
                    )
                    result = _run_negotiation(
                        cand, a_id=a_id, b_id=b_id, team_a=team_a, team_b=team_b,
                        partner=partner, fv_a_by_player_id=fv_a,
                        incoming_pick_value=incoming_pick_value, snapshot=snapshot, ctx=ctx,
                        pick_slot_ranks=pick_slot_ranks,
                        max_negotiation_rounds=max_negotiation_rounds, tally=tally,
                        h2h=h2h_acc, phrase_library=phrase_library,
                    )
                    round0_b_verdict = result.turns[0].record_b.verdict
                    incoming_key = id(cand.incoming_a)
                    if incoming_key in variant_round0_verdict:
                        tally.variant_pairs_compared += 1
                        if variant_round0_verdict.pop(incoming_key) != round0_b_verdict:
                            tally.variant_verdict_flips += 1
                    else:
                        variant_round0_verdict[incoming_key] = round0_b_verdict
                    for turn in result.turns:
                        all_records.append(turn.record_a)
                        all_records.append(turn.record_b)
                    if result.outcome == NegotiationOutcome.ACCEPTED:
                        final_turn = result.turns[-1]
                        net_sum = final_turn.record_a.score + final_turn.record_b.score
                        mutual.append(
                            (final_turn.package, net_sum, final_turn.record_a, final_turn.record_b)
                        )
                        closed_round_by_record_id[id(final_turn.record_a)] = final_turn.round_index

    # Round-7 critic fix (blocking issue 1) -- `eval_counter[0]` is a
    # GLOBAL spy on `trade_decisions.evaluate_trade` for this whole call,
    # so it includes every diverted candidate's own real evaluate_trade
    # call (`_evaluate_proposer_side`, exactly one per `offers_evaluated`
    # entry per Decision P5-56). Subtracting those back out keeps
    # `evaluations_performed` scoped to the NEGOTIATED universe M30
    # (`evaluations_performed / packages_evaluated`) was built to cost-
    # govern, and keeps `packages_evaluated == 0 implies evaluations_
    # performed == 0` true even in a window made entirely of diverted
    # candidates.
    evaluations_performed = eval_counter[0] - offer_evaluations_performed

    mutual.sort(
        key=lambda item: (
            -item[1],
            min(item[0].team_a_id, item[0].team_b_id),
            max(item[0].team_a_id, item[0].team_b_id),
            item[2].subject,
        )
    )

    consumed_players: set[int] = set()
    consumed_picks: set[int] = set()
    trades_per_team: dict[int, int] = defaultdict(int)
    roster_by_team: dict[int, list[RosterPlayer]] = {
        t.id: list(t.roster) for t in snapshot.teams
    }
    room_remaining: dict[int, int] = dict(ctx.room_by_team)
    """**B2 fix (critic round 17).** `roster_by_team` above was already
    correctly rebuilt from post-execution state each iteration, but the
    legality re-check below kept reading `ctx.room_by_team` -- the
    original, pre-window room figure, never updated as trades executed
    within this same pass. Every dollar-based legality rule
    (`apron_1`/`apron_2`/`salary_matching`) derives its `used_pre` from
    that one room figure (`trade.py::_check_legality`), so two
    individually-legal trades executed back-to-back could combine to
    breach an apron neither trade would breach alone (critic's exact
    repro: a team $4,000,001 below `apron_1`, two sequential +$4,000,000
    trades, each legal against stale room, ending $3,999,999 over
    `apron_1`). `room_remaining` tracks each team's own room exactly the
    way `run_free_agency`'s identically-named dict already does, updated
    after every execution below by the salary delta that execution just
    caused for each side.

    **Nit 1 fix (critic round 18).** The salary-delta write-back alone
    omits the `minimum_filler` adjustment `_check_legality`'s own `used_
    post` computation makes whenever a trade moves a team's contracted-
    player count across `roster_min` -- critic measured a real, legal
    3-for-1 trade (15 -> 13 contracted players, `roster_min=14`) leaving
    `room_remaining` $1,272,870 (one `min_salary`) too high versus what
    `_check_legality` itself just modeled internally. `_filler_delta`
    (below) reproduces that same `pre_filler`/`post_filler` adjustment so
    the write-back matches `_check_legality`'s own math exactly, not just
    the raw outgoing/incoming salary difference."""
    executions: list[TradeExecution] = []

    for cand, _net_sum, record_a, record_b in mutual:
        if len(executions) >= MAX_TRADES_PER_WINDOW:
            break
        a_id, b_id = cand.team_a_id, cand.team_b_id
        outgoing_ids = {rp.id for rp in cand.outgoing_a.players}
        incoming_ids = {rp.id for rp in cand.incoming_a.players}
        pick_ids = {p.id for p in cand.outgoing_a.picks} | {p.id for p in cand.incoming_a.picks}
        if (outgoing_ids | incoming_ids) & consumed_players:
            continue
        if pick_ids & consumed_picks:
            continue
        if trades_per_team[a_id] >= MAX_TRADES_PER_TEAM_PER_WINDOW:
            continue
        if trades_per_team[b_id] >= MAX_TRADES_PER_TEAM_PER_WINDOW:
            continue

        current_team_a = TeamSnapshot(
            team=snapshot.team_by_id(a_id).team,
            roster=tuple(roster_by_team[a_id]),
            draft_picks=snapshot.team_by_id(a_id).draft_picks,
            cap_settings=snapshot.team_by_id(a_id).cap_settings,
            window=snapshot.team_by_id(a_id).window,
        )
        current_team_b = TeamSnapshot(
            team=snapshot.team_by_id(b_id).team,
            roster=tuple(roster_by_team[b_id]),
            draft_picks=snapshot.team_by_id(b_id).draft_picks,
            cap_settings=snapshot.team_by_id(b_id).cap_settings,
            window=snapshot.team_by_id(b_id).window,
        )
        illegal_a = trade_decisions._check_legality(
            current_team_a,
            cand.incoming_a,
            cand.outgoing_a,
            next_season,
            ctx.next_season_cap_settings,
            room_remaining[a_id],
        )
        illegal_b = trade_decisions._check_legality(
            current_team_b,
            cand.outgoing_a,
            cand.incoming_a,
            next_season,
            ctx.next_season_cap_settings,
            room_remaining[b_id],
        )
        if illegal_a is not None or illegal_b is not None:
            continue

        roster_by_team[a_id] = [
            rp for rp in roster_by_team[a_id] if rp.id not in outgoing_ids
        ] + list(cand.incoming_a.players)
        roster_by_team[b_id] = [
            rp for rp in roster_by_team[b_id] if rp.id not in incoming_ids
        ] + list(cand.outgoing_a.players)
        trades_per_team[a_id] += 1
        trades_per_team[b_id] += 1
        consumed_players |= outgoing_ids | incoming_ids
        consumed_picks |= pick_ids

        # B2 fix -- update each side's tracked room by this trade's own
        # salary delta (`salary_next`, the same `next_season` basis
        # `_check_legality`'s own `used_pre`/`used_post` read), so the
        # NEXT candidate's legality re-check above sees this trade's
        # effect, not the pre-window figure.
        outgoing_salary_a = sum(
            trade_decisions.salary_next(rp, next_season) for rp in cand.outgoing_a.players
        )
        incoming_salary_a = sum(
            trade_decisions.salary_next(rp, next_season) for rp in cand.incoming_a.players
        )
        # Nit 1 fix (critic round 18) -- `_check_legality`'s own `used_post`
        # also adjusts for a `minimum_filler` delta whenever a trade moves a
        # team's contracted-player count across `roster_min`; the raw salary
        # delta alone under/over-states the room this trade actually frees
        # or spends whenever that crossing happens. `current_team_a`/`_b`
        # above still hold each side's PRE-trade roster (captured before
        # `roster_by_team` is mutated below), the exact basis `_filler_
        # delta` needs to match `_check_legality`'s own `pre_contracted`.
        filler_delta_a = _filler_delta(
            current_team_a.roster, cand.outgoing_a.players, cand.incoming_a.players,
            next_season, ctx.next_season_cap_settings,
        )
        filler_delta_b = _filler_delta(
            current_team_b.roster, cand.incoming_a.players, cand.outgoing_a.players,
            next_season, ctx.next_season_cap_settings,
        )
        room_remaining[a_id] += outgoing_salary_a - incoming_salary_a + filler_delta_a
        room_remaining[b_id] += incoming_salary_a - outgoing_salary_a + filler_delta_b

        executions.append(
            TradeExecution(
                team_a_id=a_id,
                team_b_id=b_id,
                players_a_to_b=tuple(rp.id for rp in cand.outgoing_a.players),
                players_b_to_a=tuple(rp.id for rp in cand.incoming_a.players),
                picks_a_to_b=tuple(p.id for p in cand.outgoing_a.picks),
                picks_b_to_a=tuple(p.id for p in cand.incoming_a.picks),
                net_a=record_a.score,
                net_b=record_b.score,
                record_a=record_a,
                record_b=record_b,
            )
        )
        # Decision P3-42 -- `executions` counts only trades that executed
        # out of a negotiation `team_id` OPENED, i.e. only the INITIATOR's
        # row. `a_id` here is `cand.team_a_id`, which is always the
        # negotiation's own initiator (`propose_packages`'s own `team_a_id
        # =proposer_id` construction, preserved unchanged through every
        # counter -- see `_record_negotiation_in_h2h`'s own docstring). The
        # responder's row (`b_id, a_id`) is never touched here: a mutual
        # accept already incremented `accepts_by_opponent` on THIS same
        # row when the negotiation closed (above, inside `_run_negotiation`
        # via `_record_negotiation_in_h2h`), which is what makes "every
        # increment of `executions` is preceded by an `accepts_by_opponent`
        # increment on the same row" (§7, property 2) true by construction
        # rather than by coincidence.
        execution_row = h2h_acc.row(a_id, b_id)
        execution_row.executions += 1.0
        execution_row.last_season_interacted = ctx.season
        execution_row.updated_season = ctx.season

    counter_closed_executions = sum(
        1
        for execution in executions
        if closed_round_by_record_id.get(id(execution.record_a), 0) >= 1
    )

    # B3 fix (critic round) -- `mean_abs_shade`'s population is the
    # head-to-head ROWS this window STARTED with (`ctx.head_to_head`, the
    # same snapshot `ctx.h2h_base_accept_rate` was itself computed from --
    # see that field's own "computed ONCE ... at the window's own start"
    # contract), never the per-turn ch3 decision sites `tally` counts.
    channel3_abs_shade_sum, channel3_abs_shade_row_count = _channel3_mean_abs_shade_inputs(
        ctx.head_to_head, ctx.h2h_base_accept_rate,
    )

    offers = _gate_rank_and_cap_pending_offers(
        pending_offer_candidates,
        snapshot=snapshot,
        next_season=next_season,
        next_season_cap_settings=ctx.next_season_cap_settings,
        roster_by_team=roster_by_team,
        room_remaining=room_remaining,
        consumed_players=consumed_players,
        consumed_picks=consumed_picks,
    )

    return TradeWindowResult(
        executions=tuple(executions),
        records=tuple(all_records),
        offers=offers,
        offers_evaluated=offers_evaluated,
        offer_evaluations_performed=offer_evaluations_performed,
        offer_records=tuple(diverted_records),
        packages_evaluated=packages_evaluated,
        turns_evaluated=tally.turns_evaluated,
        evaluations_performed=evaluations_performed,
        counter_opportunities=tally.counter_opportunities,
        counters_admitted=tally.counters_admitted,
        counter_closed_executions=counter_closed_executions,
        outcome_census=dict(tally.outcome_census),
        expired_stalled_count=tally.expired_stalled,
        variant_pairs_compared=tally.variant_pairs_compared,
        variant_verdict_flips=tally.variant_verdict_flips,
        negotiation_depth_violations=tally.depth_violations,
        channel3_decision_sites=tally.channel3_decision_sites,
        channel3_reachability=tally.channel3_reachability,
        channel3_flips=tally.channel3_flips,
        channel3_max_observed=tally.channel3_max_observed,
        channel3_abs_shade_sum=channel3_abs_shade_sum,
        channel3_abs_shade_row_count=channel3_abs_shade_row_count,
        channel2_decision_sites=tally.channel2_decision_sites,
        channel2_reachability=tally.channel2_reachability,
        channel2_flips=tally.channel2_flips,
        channel2_max_observed=tally.channel2_max_observed,
        channel1_up_decision_sites=tally.channel1_up_decision_sites,
        channel1_up_reachability=tally.channel1_up_reachability,
        channel1_up_flips=tally.channel1_up_flips,
        channel1_up_max_observed=tally.channel1_up_max_observed,
        channel1_down_decision_sites=tally.channel1_down_decision_sites,
        channel1_down_reachability=tally.channel1_down_reachability,
        channel1_down_flips=tally.channel1_down_flips,
        channel1_down_max_observed=tally.channel1_down_max_observed,
        far_rejects_observed=tally.far_rejects_observed,
        phrase_selections_total=tally.phrase_selections_total,
        phrase_fallback_selections=tally.phrase_fallback_selections,
        phrase_selections_by_situation=dict(tally.phrase_selections_by_situation),
        phrase_fallback_by_situation=dict(tally.phrase_fallback_by_situation),
        phrase_selections_by_philosophy_template={
            philosophy: dict(by_template)
            for philosophy, by_template in tally.phrase_selections_by_philosophy_template.items()
        },
        head_to_head=dict(h2h_acc.rows),
    )


def _offer_candidate_clears_gates(
    candidate: PendingOffer,
    *,
    snapshot: LeagueSnapshot,
    next_season: int,
    next_season_cap_settings: LeagueCapSettings,
    roster_by_team: Mapping[int, Sequence[RosterPlayer]],
    room_remaining: Mapping[int, int],
    consumed_players: Iterable[int],
    consumed_picks: Iterable[int],
) -> bool:
    """Decision P5-57's four gates, checked in the order the design doc
    states them, against the window's own POST-EXECUTION state -- the
    same `roster_by_team`/`room_remaining` the greedy execution loop
    above finishes with, never the pre-window figures `propose_packages`
    originally saw (the whole reason this runs after that loop, not
    inside the candidate-scan loop where the candidate was collected)."""
    if candidate.record.verdict != "accept":  # gate 1 -- the AI accepts its own side
        return False
    cand = candidate.package
    outgoing_ids = {rp.id for rp in cand.outgoing_a.players}
    incoming_ids = {rp.id for rp in cand.incoming_a.players}
    pick_ids = {p.id for p in cand.outgoing_a.picks} | {p.id for p in cand.incoming_a.picks}
    consumed_players_set = set(consumed_players)
    consumed_picks_set = set(consumed_picks)
    if (outgoing_ids | incoming_ids) & consumed_players_set:  # gate 4
        return False
    if pick_ids & consumed_picks_set:  # gate 4
        return False

    a_id, b_id = candidate.from_team_id, candidate.to_team_id
    current_team_a = TeamSnapshot(
        team=snapshot.team_by_id(a_id).team,
        roster=tuple(roster_by_team[a_id]),
        draft_picks=snapshot.team_by_id(a_id).draft_picks,
        cap_settings=snapshot.team_by_id(a_id).cap_settings,
        window=snapshot.team_by_id(a_id).window,
    )
    current_team_b = TeamSnapshot(
        team=snapshot.team_by_id(b_id).team,
        roster=tuple(roster_by_team[b_id]),
        draft_picks=snapshot.team_by_id(b_id).draft_picks,
        cap_settings=snapshot.team_by_id(b_id).cap_settings,
        window=snapshot.team_by_id(b_id).window,
    )
    illegal_a = trade_decisions._check_legality(  # gate 2 -- proposer legality
        current_team_a,
        cand.incoming_a,
        cand.outgoing_a,
        next_season,
        next_season_cap_settings,
        room_remaining[a_id],
    )
    illegal_b = trade_decisions._check_legality(  # gate 3 -- the human side's legality
        current_team_b,
        cand.outgoing_a,
        cand.incoming_a,
        next_season,
        next_season_cap_settings,
        room_remaining[b_id],
    )
    return illegal_a is None and illegal_b is None


def _gate_rank_and_cap_pending_offers(
    candidates: Sequence[PendingOffer],
    *,
    snapshot: LeagueSnapshot,
    next_season: int,
    next_season_cap_settings: LeagueCapSettings,
    roster_by_team: Mapping[int, Sequence[RosterPlayer]],
    room_remaining: Mapping[int, int],
    consumed_players: Iterable[int],
    consumed_picks: Iterable[int],
) -> tuple[PendingOffer, ...]:
    """Decision P5-57's own selection/ordering, applied in two stages once
    per window, after the greedy execution loop closes:

    1. **Per ordered pair, at most one offer survives** -- among this
       pair's own candidates that clear all four gates
       (`_offer_candidate_clears_gates`), the one maximising the
       proposer's own surplus `record.score - record.threshold`, tied
       broken by `record.subject` (a stable string).
    2. **Per human team, at most `MAX_AI_OFFERS_PER_HUMAN_TEAM_PER_WINDOW`
       survive** -- of the per-pair winners addressed to that team,
       ranked by the identical surplus, same tie-break, with `from_team_
       id` as a final, purely mechanical tie-break for full determinism
       (the design doc names surplus and `record.subject`; two different
       proposers landing on both at once needs a third key to stay a
       function of its own inputs alone, per this project's own Family-1
       determinism discipline)."""
    if not candidates:
        return ()

    by_pair: dict[tuple[int, int], list[PendingOffer]] = defaultdict(list)
    for candidate in candidates:
        by_pair[(candidate.from_team_id, candidate.to_team_id)].append(candidate)

    consumed_players_set = set(consumed_players)
    consumed_picks_set = set(consumed_picks)

    pair_winners: list[PendingOffer] = []
    for pair_candidates in by_pair.values():
        gated = [
            c
            for c in pair_candidates
            if _offer_candidate_clears_gates(
                c,
                snapshot=snapshot,
                next_season=next_season,
                next_season_cap_settings=next_season_cap_settings,
                roster_by_team=roster_by_team,
                room_remaining=room_remaining,
                consumed_players=consumed_players_set,
                consumed_picks=consumed_picks_set,
            )
        ]
        if not gated:
            continue
        pair_winners.append(
            max(gated, key=lambda c: (c.record.score - c.record.threshold, c.record.subject))
        )

    by_partner: dict[int, list[PendingOffer]] = defaultdict(list)
    for winner in pair_winners:
        by_partner[winner.to_team_id].append(winner)

    final_offers: list[PendingOffer] = []
    for partner_candidates in by_partner.values():
        ranked = sorted(
            partner_candidates,
            key=lambda c: (
                -(c.record.score - c.record.threshold),
                c.record.subject,
                c.from_team_id,
            ),
        )
        final_offers.extend(ranked[:MAX_AI_OFFERS_PER_HUMAN_TEAM_PER_WINDOW])

    # Deterministic overall ordering -- `by_partner`'s own dict iteration
    # order is insertion order (first-offer-seen per partner), stable but
    # not itself a documented contract; sort the final tuple by the same
    # keys one more time so the RETURNED order does not depend on dict
    # insertion order at all.
    final_offers.sort(
        key=lambda c: (
            c.to_team_id,
            -(c.record.score - c.record.threshold),
            c.record.subject,
            c.from_team_id,
        )
    )
    return tuple(final_offers)


def _incumbent_prior_salary(roster_player: RosterPlayer, season: int) -> int | None:
    """`roster_player`'s own currently-expiring contract-year salary for
    `season` (the season on the books right now, i.e. `ctx.season`) --
    the Bird-rights incumbent gate `legal_capacity`/`_effective_capacity`
    read via their `incumbent_prior_salary` keyword (module docstring,
    signing.py's interpretation call 3). Returns `None` -- never raises --
    for a contract-less player or one with no contract-year matching
    `season`, so a caller can pass its result straight through as the
    gate without a separate existence check; a genuine incumbent (any
    `_expiring_by_team` member) always has both, but this function must
    stay defensive for callers that construct a "pending offer" directly
    (bypassing `_expiring_by_team`) for reasons unrelated to Bird rights,
    e.g. this module's own dedup-regression test fixtures."""
    if roster_player.contract is None:
        return None
    for year in roster_player.contract.years:
        if year.season == season:
            return year.salary
    return None


# ---------------------------------------------------------------------------
# run_resign_window
# ---------------------------------------------------------------------------


def run_resign_window(
    snapshot: LeagueSnapshot,
    ctx: GmContext,
    *,
    expiring_by_team: Mapping[int, tuple[int, ...]],
) -> tuple[SigningOffer, ...]:
    """For each team, for each of its own expiring players (`expiring_by_
    team` supplies the ids; this function does not determine expiry
    itself), one `signing.construct_offer`. Pending offers only -- no
    execution. See module docstring for why `cap_settings`/`season`/`room`
    here read `next_season`, not `ctx.season`.

    **Slice 12-A (Decision P2-20, round 11 §5.3, round 12 §5.4).** Every
    player `expiring_by_team` names is, by construction, this team's own
    incumbent -- `room` above is replaced by `signing.legal_capacity`'s
    own incumbent-aware ceiling, priced against `ctx.apron1_room_by_team`
    (a static snapshot here, not the decrementing per-pass tracker `run_
    free_agency`'s round-0 recompute uses -- this function builds PENDING
    offers only, one per player, with no notion of a shared pass to
    decrement against). **This is why the two are documented as agreeing
    in kind, not byte-for-byte** (see this function's own pre-existing
    note on `run_free_agency`'s round-0 recompute, `gm_service.py`'s T3
    docstring): both admit the Bird bucket at the same ceiling formula,
    but only the round-0 recompute tracks a shared, mutating apron-room
    ledger across every incumbent in the same team's own pass.

    **The hard `apron_2` clamp, and the skip it forces (architect round
    14 §5.3).** `legal_capacity`'s new clamp applies here too, at the same
    static `ctx.apron1_room_by_team` basis. When the resulting `capacity`
    falls below `min_salary`, this function builds **no pending offer at
    all** for that player -- not a cosmetic skip: a team at `apron_2` has
    no Bird rights it can exercise, so the pair correctly never enters
    `pending_pairs`, and `run_free_agency` therefore never treats it as
    Bird-eligible either (that self-consistency is the point). It also
    keeps H5's identity intact on this path (a capacity below `min_salary`
    must never reach `construct_offer`, which would silently re-inflate a
    non-positive `room` into a full MLE offer)."""
    next_season = ctx.next_season
    offers: list[SigningOffer] = []
    for team_snapshot in snapshot.teams:
        team_id = team_snapshot.id
        if team_id in ctx.self_managed_team_ids:
            # Decision P5-45's RESIGN_WINDOW row, unchanged across every
            # later round -- no automatic re-sign offer to any of a human
            # team's own expiring players. Its expiring players still
            # enter the free-agency pool via `_load_free_agency_pool`
            # and other teams can and will sign them.
            continue
        expiring_ids = expiring_by_team.get(team_id, ())
        if not expiring_ids:
            continue
        matrix = build_need_matrix(team_snapshot, tuning=ctx.tuning)
        philosophy = ctx.philosophy_by_team[team_id]
        team_window = ctx.window_by_team[team_id]
        room = ctx.room_by_team[team_id]
        apron_room = ctx.apron1_room_by_team[team_id]  # [] not .get() -- same nit as elsewhere
        for player_id in expiring_ids:
            rp = team_snapshot.player_by_id(player_id)
            prior_salary = _incumbent_prior_salary(rp, ctx.season)
            capacity = signing_decisions.legal_capacity(
                room,
                ctx.next_season_cap_settings,
                incumbent_prior_salary=prior_salary,
                incumbent_years_pro=(rp.player.years_pro if prior_salary is not None else None),
                apron_room=(apron_room if prior_salary is not None else None),
            )
            if capacity < ctx.next_season_cap_settings.min_salary:
                continue  # no legal room under apron_2 -- no pending offer at all
            offer, _record = construct_offer(
                rp,
                matrix,
                team_id=team_id,
                philosophy=philosophy,
                window=team_window,
                cap_settings=ctx.next_season_cap_settings,
                room=capacity,
                season=next_season,
                tuning=ctx.tuning,
            )
            offers.append(offer)
    return tuple(offers)


# ---------------------------------------------------------------------------
# run_free_agency
# ---------------------------------------------------------------------------

MAX_FA_ROUNDS = 8
FA_TARGETS_PER_TEAM = 5


def _effective_capacity(
    team_id: int,
    room_remaining: Mapping[int, int],
    exceptions_used: Mapping[int, set[str]],
    cap_settings: LeagueCapSettings,
    *,
    incumbent_prior_salary: int | None = None,
    incumbent_years_pro: int | None = None,
    apron_room_remaining: Mapping[int, int] | None = None,
) -> int:
    """The real maximum salary `team_id` could offer a NEW contract RIGHT
    NOW, accounting for room plus whichever once-per-pass exceptions it
    has not used yet this free-agency pass (B1 fix, critic round 18 --
    third round on this bug).

    **Why `signing.legal_capacity` alone cannot answer this question.**
    That function is a correct, pure, *stateless* primitive: for any
    `room <= 0` it always returns the same constant (the largest of MLE/
    bi-annual/min_salary), with zero memory of whether a team already
    used that exception earlier in this same pass. Re-checking more often
    against `legal_capacity` (the prior two fix attempts' approach) cannot
    fix this -- the state this bug needs (which exceptions a team has
    already spent) has to live here, in `run_free_agency`'s own per-pass
    `exceptions_used` tracking, not in the stateless step-9 primitive.

    **Largest-of, not sum-of** (§9.3, consistent with `signing.legal_
    capacity`'s own "largest-available-first" interpretation and Decision
    P2-20's precedent): a team gets whichever single bucket is largest
    among what it has not already spent, never room AND a full unused MLE
    simultaneously.

    **Slice 12-A (Decision P2-20, round 11 §5.3, round 12 §5.4).** When
    `incumbent_prior_salary` is not `None` (this call is pricing one of
    `run_free_agency`'s own `pending_by_player` incumbents), the return
    value before the hard clamp below is `max(the ladder above, bird_
    ceiling)` -- the identical `signing_decisions.bird_ceiling` formula
    `signing.legal_capacity`'s own incumbent branch calls (module
    docstring there, interpretation call 3). Called directly here (not
    delegated to `legal_capacity` as a whole) because this function's own
    `ladder` already reflects the STATEFUL per-pass `exceptions_used`
    ledger that the stateless `legal_capacity` cannot see -- delegating
    the whole computation would silently drop that state for every
    incumbent call, the exact bug class three critic rounds already
    closed for the non-incumbent ladder (see this function's own
    docstring above).

    **The hard `apron_2` clamp (architect round 14 §5.2/§5.3).** `apron_
    room` is now read for **non-incumbents too**, because the hard
    ceiling binds every signing method, not just Bird -- the return value
    is clamped to `signing_decisions.apron2_headroom(cap_settings, apron_
    room)` whenever `apron_room_remaining` is supplied, mirroring `legal_
    capacity`'s own new clamp exactly. The three `run_free_agency` call
    sites already pass `apron_room_remaining=` unconditionally, so no
    call-site change is needed for this half of the fix."""
    used = exceptions_used.get(team_id, set())
    room = room_remaining.get(team_id, 0)
    candidates = [room]
    if "mle" not in used:
        candidates.append(cap_settings.mle_amount)
    if "bi_annual" not in used:
        candidates.append(cap_settings.bi_annual_amount)
    candidates.append(cap_settings.min_salary)  # always available, no once-per-pass limit
    ladder = max(candidates)

    # Direct indexing, NOT .get() -- critic round 47 nit: a missing team
    # must raise, never silently yield an unbounded ceiling. Every seed
    # covers snapshot.teams.
    apron_room = None if apron_room_remaining is None else apron_room_remaining[team_id]

    if incumbent_prior_salary is None:
        bucket = ladder
    else:
        assert incumbent_years_pro is not None, (
            "_effective_capacity's incumbent branch requires incumbent_years_pro whenever "
            "incumbent_prior_salary is provided"
        )
        bucket = max(
            ladder,
            signing_decisions.bird_ceiling(
                cap_settings, incumbent_years_pro=incumbent_years_pro, apron_room=apron_room
            ),
        )
    if apron_room is None:
        return bucket
    return min(bucket, signing_decisions.apron2_headroom(cap_settings, apron_room))


def _finalize_signing(
    team_id: int,
    annual_salary: int,
    room_remaining: dict[int, int],
    exceptions_used: dict[int, set[str]],
    cap_settings: LeagueCapSettings,
    *,
    incumbent_prior_salary: int | None = None,
    incumbent_years_pro: int | None = None,
    apron_room_remaining: dict[int, int] | None = None,
) -> SigningMethod:
    """Mutates `room_remaining`/`exceptions_used`/`apron_room_remaining`
    for whichever bucket actually covers `annual_salary`, in priority
    order: room first (it is "free" -- consumes no once-per-season
    exception), then (slice 12-A) the Bird bucket if this is an incumbent
    re-sign exceeding room AND the Bird ceiling itself covers the salary
    (architect round 14 B3 fix), then whichever once-per-pass exception is
    both unused and large enough, then the always-available, untracked
    minimum exception (B1b fix -- the tag is now derived from which
    bucket actually covered this specific salary, never guessed from the
    sign of `room`). Callers must only call this after confirming `annual_
    salary <= _effective_capacity(...)` for this same `team_id` (with the
    identical `incumbent_*`/`apron_room_remaining` arguments) -- one of
    these branches always applies under that precondition.

    **Guard first, before any mutation (round 14 §5.3)**: the pass
    invariant (`used_final <= apron_2`, round 14 §5.2's three-line proof)
    defends itself here rather than resting on caller discipline. Every
    admitted signing must satisfy `annual_salary <= apron2_headroom(...)`.

    **`charge_apron()` on every branch (B2, round 14 §5.3)** -- including
    MLE/bi-annual/minimum/cap-space, which the pre-fix tree never charged
    (critic round 47 B2: 40% of dollars written were invisible to the
    ledger). Only `_enforce_roster_floor`'s signings are exempt, and
    deliberately so (round 14 §3 H2's `Δused = 0` proof for a floor
    signing -- see that function's own call site, no ledger argument
    passed there at all).

    **The Bird branch, before MLE/bi-annual, now gated by the Bird
    ceiling itself (round 11 §5.3's ordering, round 14 B3's missing
    conjunct).** Taken only when `annual_salary` exceeds room, `incumbent_
    prior_salary` is not `None`, AND `annual_salary <= bird_ceiling(...)`
    -- the identical expression `_effective_capacity` used to authorize
    this salary in the first place (round 14 §5.3 note 1: the tag site and
    the capacity site must never be allowed to disagree). An incumbent
    whose salary the Bird ceiling does NOT cover **falls through to the
    ordinary once-per-pass ladder** below, exactly like any other free
    agent -- that fallthrough, not a ceiling on the Bird branch alone, is
    what bounds a pass once Bird rights are exhausted (round 14 §3 H1).
    `room_remaining[team_id]` is decremented by the full `annual_salary`
    and **may go negative -- that is the point** (this is the one signing
    method in this ladder that is not itself room-bounded).

    **`room` is deliberately still not decremented by MLE/bi-annual/
    minimum** (pre-existing, deliberate semantics -- "room" is cap space;
    using an exception does not consume cap space -- bounded instead by
    `exceptions_used`). That is not the apron ledger and must not be
    conflated with it; only `apron_room_remaining` is a faithful dollar
    ledger, and it is charged by every branch regardless of whether `room`
    itself moves.

    **Per-incumbent, not once-per-pass, is preserved by construction**
    (round 14 §5.3 note 4): the BIRD branch consults a *dollar ledger*,
    never `exceptions_used`. A team with $60M of apron_1 headroom can
    Bird-sign three $20M incumbents in the same pass; only the ledger,
    eventually, stops it."""
    if apron_room_remaining is not None:
        headroom = signing_decisions.apron2_headroom(cap_settings, apron_room_remaining[team_id])
        assert annual_salary <= headroom, (
            f"_finalize_signing would breach apron_2: salary {annual_salary} exceeds "
            f"apron_2 headroom {headroom} for team {team_id}"
        )

    used = exceptions_used.setdefault(team_id, set())
    room = room_remaining.get(team_id, 0)

    def charge_apron() -> None:
        if apron_room_remaining is not None:
            apron_room_remaining[team_id] -= annual_salary

    # 1. room -- free, consumes no exception
    if annual_salary <= room:
        room_remaining[team_id] = room - annual_salary
        charge_apron()  # B2
        return SigningMethod.CAP_SPACE

    # 2. Bird -- incumbent only, and ONLY when the Bird ceiling authorizes
    # it (B3: the missing conjunct)
    if incumbent_prior_salary is not None:
        assert incumbent_years_pro is not None, (
            "_finalize_signing's incumbent branch requires incumbent_years_pro whenever "
            "incumbent_prior_salary is provided"
        )
        apron_room = None if apron_room_remaining is None else apron_room_remaining[team_id]
        ceiling = signing_decisions.bird_ceiling(
            cap_settings, incumbent_years_pro=incumbent_years_pro, apron_room=apron_room
        )
        if annual_salary <= ceiling:
            room_remaining[team_id] = room - annual_salary  # may go negative -- the point
            charge_apron()
            return SigningMethod.BIRD
        # else: fall through. The incumbent rejoins the ordinary once-per-
        # pass ladder, which is exactly what bounds the pass once Bird
        # rights are spent.

    # 3./4./5. unchanged in structure; each now charges the apron ledger (B2)
    if "mle" not in used and annual_salary <= cap_settings.mle_amount:
        used.add("mle")
        charge_apron()
        return SigningMethod.MLE
    if "bi_annual" not in used and annual_salary <= cap_settings.bi_annual_amount:
        used.add("bi_annual")
        charge_apron()
        return SigningMethod.BI_ANNUAL
    # The minimum exception has no once-per-season limit in real NBA rules
    # (any team can sign unlimited minimum contracts) -- no usage tracking,
    # and it still does not decrement `room_remaining` (signing at the
    # minimum does not consume meaningful "capacity" in this simplified
    # model) -- but it DOES charge the apron ledger (round 14 §5.3 note 3):
    # a minimum contract really does add salary to the book, and the
    # invariant needs it. §3 H2's filler credit means this over-charges by
    # at most `min_salary` for a team below `roster_min`, in the safe
    # direction. Reachable only when `annual_salary <= min_salary` under
    # this function's own documented precondition (critic round 18, nit
    # 2) -- asserted rather than left to fail silently as a wrong tag,
    # since a future regression that violates the precondition upstream
    # (e.g. an `_effective_capacity` computation drifting out of sync with
    # this function's own priority order) should surface here, not as a
    # quietly mislabeled `SigningMethod`.
    assert annual_salary <= cap_settings.min_salary, (
        f"_finalize_signing's precondition (annual_salary <= _effective_capacity(...)) "
        f"was violated: salary {annual_salary} exceeds every bucket including min_salary "
        f"{cap_settings.min_salary} for team {team_id}"
    )
    charge_apron()
    return SigningMethod.MIN_EXCEPTION


def _enforce_roster_floor(
    snapshot: LeagueSnapshot,
    ctx: GmContext,
    remaining_pool: dict[int, RosterPlayer],
    roster_count: dict[int, int],
    room_remaining: dict[int, int],
    matrix_by_team: Mapping[int, NeedMatrix],
) -> tuple[tuple[SigningExecution, ...], tuple[DecisionRecord, ...]]:
    """Any team still below `roster_min` (read off `next_season_cap_
    settings`, the season this roster is being built for) signs the
    highest-`fit_value` remaining pool player at `min_salary` for 1 year,
    repeated until that team is at floor or the pool is empty.

    **Deliberately apron-neutral (architect round 14 §3 H2), no ledger
    argument at all.** A floor signing writes `SigningExecution` directly
    rather than going through `_finalize_signing`, and every floor
    signing is at exactly `min_salary` for a team below `roster_min` --
    `used`'s own `minimum_filler` term (`capsheet.py`) means `Δused = 0`
    identically for such a signing, so charging the `apron_room_
    remaining` ledger here would be wrong, not merely redundant (it would
    silently under-fund the next pass). This omission is deliberate, not
    forgotten.

    **B2 fix (critic round 51).** A floor signing always writes its
    `SigningExecution` (a team CAN legally sign a player about to age
    past `ctx.retirement_age` -- that is not illegal, and the signing
    must still happen so the team gets *some* body), but `roster_count`
    is only incremented if the signed player will actually occupy a
    next-season slot (`_occupies_next_season`, same basis this function's
    own `while` condition and every other consumer reads) -- otherwise a
    signing that will not physically be there next season would let this
    loop believe the floor was satisfied when it was not, the identical
    overcount class Decision P2-37 exists to fix, just on the write side.
    `remaining_pool.pop(best_id)` above still always removes the chosen
    candidate regardless of this guard, so the loop's own termination
    (bounded by `remaining_pool` shrinking every iteration, `while ...
    and remaining_pool` as the backstop) is unaffected -- a team that can
    only find non-occupying candidates simply drains the shared pool
    without ever satisfying its own floor and exits the `while` loop when
    `remaining_pool` is empty (a real, honest gap left below floor, not a
    silent loop).

    **Contract staleness trap, caught rather than shipped.** `rp` here is
    still the pool's pre-signing `RosterPlayer` -- for the `_expiring_by_
    team` half of `_load_free_agency_pool`'s pool (a still-`ACTIVE`
    rostered player whose contract's last year is genuinely THIS season),
    `rp.contract` is that real, about-to-lapse contract, whose last
    covered season is `next_season - 1` by definition. Calling `_occupies_
    next_season` directly on `rp` would trip its contract-expiry branch
    for every single such player regardless of age, since that branch
    returns `False` before the retirement-age branch is ever reached --
    wrongly treating every ordinary re-signing of an expiring incumbent as
    non-occupying. The floor signing below always writes a fresh 1-year
    deal starting at `next_season`, which by construction covers
    `next_season` -- the OLD contract is irrelevant to this question, only
    age is. `replace(rp, contract=None)` strips it so `_occupies_next_
    season` reduces to exactly its retirement-age branch here."""
    next_season = ctx.next_season
    roster_min = ctx.next_season_cap_settings.roster_min
    min_salary = ctx.next_season_cap_settings.min_salary
    signings: list[SigningExecution] = []
    records: list[DecisionRecord] = []

    for team_snapshot in snapshot.teams:
        team_id = team_snapshot.id
        while roster_count[team_id] < roster_min and remaining_pool:
            matrix = matrix_by_team[team_id]
            best_id = max(
                remaining_pool,
                key=lambda pid: (
                    trade_decisions.fit_value_for(
                        remaining_pool[pid],
                        matrix,
                        league_rng_seed=ctx.league_rng_seed,
                        team_id=team_id,
                        season=ctx.season,
                        phase=ctx.phase,
                        philosophy=ctx.philosophy_by_team[team_id],
                        window=ctx.window_by_team[team_id],
                        cap_settings=ctx.cap_settings,
                        tuning=ctx.tuning,
                    ),
                    -pid,
                ),
            )
            rp = remaining_pool.pop(best_id)
            term = ReasonTerm(
                label="min_salary_floor",
                input_value=float(min_salary),
                weight=1.0,
                contribution=float(min_salary),
            )
            record = DecisionRecord(
                team_id=team_id,
                philosophy=ctx.philosophy_by_team[team_id],
                window=ctx.window_by_team[team_id],
                subject=f"player:{rp.id}",
                verdict="floor_sign",
                score=float(min_salary),
                threshold=0.0,
                terms=(term,),
            )
            signings.append(
                SigningExecution(
                    team_id=team_id,
                    player_id=rp.id,
                    years=1,
                    annual_salary=min_salary,
                    start_season=next_season,
                    signing_method=SigningMethod.MIN_EXCEPTION,
                    record=record,
                )
            )
            records.append(record)
            if _occupies_next_season(replace(rp, contract=None), next_season, ctx.retirement_age):
                roster_count[team_id] += 1
            room_remaining[team_id] -= min_salary

    return tuple(signings), tuple(records)


def _occupies_next_season(
    roster_player: RosterPlayer,
    next_season: int,
    retirement_age: int | None = None,
) -> bool:
    """Whether `roster_player` will still physically occupy a roster slot
    at `next_season` -- the shared occupancy predicate `_next_season_
    roster_count` (below) sums over, extracted so `_build_throw_ins`
    (architect round 12 §5.3 step 1, `docs/ledger/reports/phase-2-
    architect-round-12.md`) can ask the identical question per-candidate
    without duplicating the expression a third time.

    `True` unless `roster_player` holds a contract whose last covered
    season is genuinely THIS season (`next_season - 1`) -- a contract-less
    rostered player counts (nothing under `dba2/` ever transitions a
    contract-less `ACTIVE` roster player off a team), and so does a player
    whose contract has already lapsed (an already-expired contract is not
    a "this season" expiry, same basis `_expiring_by_team` uses) -- **or**,
    if `retirement_age` is given, unless `roster_player` will be strictly
    older than `retirement_age` at `next_season` (Decision P2-37, architect
    round 18 §4.7 Shape 1). `retirement_age` defaults to `None`, matching
    "no retirement knowledge, behave exactly as before" -- see `GmContext.
    retirement_age`'s own docstring for the quarantine boundary this
    respects (a caller-supplied value, never a `tools/` import).

    **Do not confuse this with `trade.py::_contract_covers_season`, and do
    not merge the two -- they are deliberately different facts that
    disagree on two real cases.** `_contract_covers_season` is a
    *financial* basis (does a `Contract` row exist for `season`, matching
    `capsheet_service.py`'s own `roster_count` provenance): a
    contract-less rostered player fails it (covers no season) even though
    they physically occupy a slot, and an already-lapsed contract also
    fails it. `_occupies_next_season` is a *physical* basis (does a body
    stay on the roster): both of those players occupy a slot regardless of
    what their contract situation says. Architect round 12 exists because
    a basis divergence exactly like this one (`_build_throw_ins`
    guaranteeing neutrality in `len(roster)` while every real consumer
    read `_next_season_roster_count`) went unnoticed for a full build
    round -- the fix must not quietly create a third, undocumented basis
    to confuse with the other two."""
    if (
        roster_player.contract is not None
        and max(y.season for y in roster_player.contract.years) == next_season - 1
    ):
        return False
    if retirement_age is not None:
        age_next_season = player_age(roster_player.player.birth_date, next_season)
        if age_next_season > retirement_age:
            return False
    return True


def _next_season_roster_count(
    team_snapshot: TeamSnapshot,
    next_season: int,
    retirement_age: int | None = None,
) -> int:
    """How many of `team_snapshot`'s currently-rostered players will still
    physically occupy a roster slot at `next_season` -- the basis `run_
    free_agency`'s own roster-max gates and `_enforce_roster_floor`'s floor
    gate both need, since neither is asking "who's under contract" but
    "who's still here."

    **Round 12 note.** This is now a thin `sum` over `_occupies_next_
    season` (above) -- the two must never drift apart; `_occupies_next_
    season`'s own docstring explains why it is not the same fact as
    `trade.py::_contract_covers_season`.

    **Decision P2-37.** `retirement_age`, when given, is threaded straight
    through to `_occupies_next_season` -- see that function's own
    docstring. `None` (the default) is byte-identical to pre-P2-37
    behaviour.

    **B1 fix (critic round 19).** A rostered player counts toward next
    season UNLESS they hold a contract that is genuinely ending THIS
    season -- exactly `_expiring_by_team`'s own exact-equality expiry test
    (`max(y.season for y in rp.contract.years) == season`, `season =
    next_season - 1`), not the broader `< next_season`. A rostered player
    with `contract is None` counts -- nothing under `dba2/` ever
    transitions a contract-less `ACTIVE` roster player off a team (there's
    no contract to expire, and they aren't `FREE_AGENT`), so they will
    definitely still occupy that slot next season. Excluding them (the
    prior, backwards version of this expression) let `run_free_agency`
    sign on top of them without limit -- reproduced live against dev data
    where every rostered player in the seeded dev league carries zero
    contracts (`tools/seed_dev_league.py` never generates any), a
    13-contract-less-body team over-signed to 28 physical bodies against
    `roster_max=15`.

    **Round-2 nit, fixed here rather than left latent.** A `< next_season`
    exclusion (round 1's fix) also drops a player whose contract's last
    year is already BEFORE the current season -- a lapsed contract nobody
    rolled over. `_expiring_by_team`'s own `== season` test would never
    route that player to free agency either (its own exact-equality
    guard), so nothing removes them -- the identical B1 shape, one season
    further back. Currently unreachable (`leagues.current_season` is only
    ever written at league creation; nothing under `dba2/` increments it
    yet), but the not-yet-built multi-season harness (T6b/T7a) is exactly
    the kind of caller that WILL age a league forward and make this real,
    so it is fixed now rather than left to resurface as a second B1.

    A `Contract` is a financial/cap fact layered on top of roster
    occupancy, not the thing that determines it -- `roster_status`/
    `team_id` are the physical-presence fields. The only thing contract
    data should decide here is whether a slot is *about to free up* (a
    genuine expiry this season), never whether an already-rostered body
    counts at all."""
    return sum(
        1
        for rp in team_snapshot.roster
        if _occupies_next_season(rp, next_season, retirement_age)
    )


def _can_sign_at_all(
    team_id: int, apron_room_remaining: Mapping[int, int], cap_settings: LeagueCapSettings
) -> bool:
    """A team within one minimum salary of `apron_2` cannot legally sign
    anyone in this pass. The threshold is `min_salary` rather than `0`
    because `construct_offer` re-applies `legal_capacity` to whatever it
    is handed and returns the FULL MLE ladder for any non-positive input
    (architect round 14 §3 H5) -- skipping here keeps `legal_capacity(
    capacity) == capacity` an identity for every value this function ever
    passes into `construct_offer`."""
    return (
        signing_decisions.apron2_headroom(cap_settings, apron_room_remaining[team_id])
        >= cap_settings.min_salary
    )


def run_free_agency(
    snapshot: LeagueSnapshot,
    ctx: GmContext,
    *,
    pool: tuple[RosterPlayer, ...],
    pending_offers: tuple[SigningOffer, ...],
) -> FreeAgencyResult:
    """`MAX_FA_ROUNDS` rounds: every team with `legal_capacity > 0` (always
    true, see module docstring) and next-season roster occupancy below
    `next_season_cap_settings.roster_max` offers its top `FA_TARGETS_PER_
    TEAM` remaining pool players by that team's own `fit_value`; resolution
    goes by descending best-offer `guaranteed_total` (ties -> ascending
    player id), `free_agency_service.rank_offers` picking the winner. Stops
    when a round signs nobody; finishes with `_enforce_roster_floor`.

    **T6a fix.** `roster_count` is seeded via `_next_season_roster_count`,
    not today's raw `len(t.roster)` body count -- every signing this
    function writes starts at `ctx.next_season`, and both roster-max gates
    below (`roster_count[team_id] >= next_season_cap_settings.roster_max`)
    compare against `next_season_cap_settings`, so the seed has to answer
    "how many bodies will this team actually occupy next season" not "how
    many does it have today" and not "how many are under contract."
    Today's raw body count overcounts one way: nothing under `dba2/` yet
    transitions an expiring player off a roster mid-season
    (`_load_free_agency_pool`'s own docstring -- "no season-rollover job
    exists yet in this codebase"), so a player whose contract's last year
    is `ctx.season` still shows up in `len(t.roster)` despite having zero
    legal claim on a next-season slot -- `_next_season_roster_count`
    excludes them. But a raw "is there a contract row covering
    `next_season`" count overcounts the OTHER way and undercounts real
    occupancy: `_next_season_roster_count`'s own docstring (B1, critic
    round 19) has the full reasoning for why a rostered player with
    `contract is None` still counts as occupying a slot.

    **B1/B1b fix, third round (critic round 18).** The first two fix
    attempts re-checked affordability more often against `signing.legal_
    capacity(room_remaining[team_id], ...)` -- but that function is a
    pure, stateless primitive that, for any `room <= 0`, always returns
    the same constant (the largest of MLE/bi-annual/min_salary), with no
    memory of whether a team already used that exception earlier in this
    same pass. No amount of re-checking against it can bound how many
    MLE-sized contracts one team signs across a pass -- critic reproduced
    a 1-team, $30M-room, 10-free-agent scenario committing $156.9M, 9 of
    10 signings tagged MLE. `exceptions_used` below is the missing
    state: a per-team, per-pass record of which once-per-season
    exceptions (`"mle"`/`"bi_annual"`) a team has already spent, read and
    mutated by `_effective_capacity`/`_finalize_signing` -- both offer
    construction and the accept-time check now read the same real
    ceiling, and the accept-time tag is derived from whichever bucket
    actually covered the salary (B1b), never guessed from the sign of
    `room`.

    **Slice 12-A (Decision P2-20, round 11 §5.3, round 12 §5.4).** Every
    `(team_id, player_id)` pair that is one of `pending_offers`'s own
    pairs -- i.e. one of `run_resign_window`'s own incumbents -- is priced
    with the Bird bucket available, at round 0's dedicated recompute AND
    (round 12 §5.4 amendment 3, G4) at any later round the same pair
    resurfaces via the general per-team offer loop, since `offered_this_
    round` resets every round and a leftover incumbent can be re-targeted
    by its own original team.

    **Pass invariant (architect round 14 §5.2, verbatim).** Let `L` be
    `apron_room_remaining[team]` and `GAP = next_season_cap_settings.
    apron_2 - next_season_cap_settings.apron_1`. Every signing that passes
    through `_finalize_signing` is admitted only if `s <= L + GAP`, and
    decrements `L` by `s`. `_enforce_roster_floor`'s signings neither
    decrement `L` nor change `used` (round 14 §3 H2). Therefore `L >=
    -GAP` after any number of signings, and `used_final = used_0 + Σs -
    min_salary·c = (apron_1 - L_0) + (L_0 - L_final) - min_salary·c =
    apron_1 - L_final - min_salary·c <= apron_1 + GAP = apron_2`.
    Corollary: a team whose `L_0 < -GAP` (already over `apron_2` on
    arrival) is admitted for nothing, so the pass cannot deepen an
    inherited breach either. `apron_room_remaining` seeds once from `ctx.
    apron1_room_by_team`, is decremented by every `_finalize_signing`-
    routed signing regardless of method (B2, round 14), and is allowed to
    go negative (bounded at `-GAP` by the invariant above).

    **Decision P2-39 (architect round 21 §4.3).** `room_remaining` seeds
    from `ctx.room_for_signings_by_team` when the caller has supplied one
    (`is not None` -- never `or`, since an empty mapping is a legitimate,
    if degenerate, input that must not silently fall back to the hold-
    free basis), else from `ctx.room_by_team` exactly as before. This is
    the *only* line this decision touches; every downstream affordability/
    legality read in this function (`_effective_capacity`, `legal_room`,
    the accept-time re-check, `_finalize_signing`, `_enforce_roster_
    floor`) reads `room_remaining`, never `ctx`, so changing this one seed
    changes every signing decision without touching any of those call
    sites. `apron_room_remaining` (immediately below) is deliberately
    **not** reseeded from the same basis -- it stays on `ctx.apron1_room_
    by_team`, hold-free, because it is the `apron_2` ledger the pass
    invariant above is proved against (M2's own basis, round 14 §5.2), and
    `signing.py::apron2_headroom`'s own docstring already documents that
    ledger as reading the identical hold-free basis `compute_m2` reads --
    making it hold-aware here would decouple this function's own guarantee
    from the invariant it exists to satisfy. `run_resign_window` and the
    whole trade-legality/valuation path (`dba2/managers/decisions/
    trade.py`) are unaffected by this decision and remain on `ctx.room_by_
    team` exactly as before."""
    next_season_cap_settings = ctx.next_season_cap_settings
    remaining_pool: dict[int, RosterPlayer] = {rp.id: rp for rp in pool}
    signing_capacity_basis: Mapping[int, int] = (
        ctx.room_for_signings_by_team
        if ctx.room_for_signings_by_team is not None
        else ctx.room_by_team
    )
    room_remaining: dict[int, int] = dict(signing_capacity_basis)
    exceptions_used: dict[int, set[str]] = {t.id: set() for t in snapshot.teams}
    # Slice 12-A (round 11 §5.3): a per-pass, decrementing mirror of `room_
    # remaining` for the apron ledger -- seeded from `ctx.apron1_room_by_
    # team`, mutated by every `_finalize_signing`-routed branch (round 14
    # B2), read by `_effective_capacity`'s hard clamp and the Bird ceiling.
    apron_room_remaining: dict[int, int] = dict(ctx.apron1_room_by_team)
    roster_count: dict[int, int] = {
        t.id: _next_season_roster_count(t, ctx.next_season, ctx.retirement_age)
        for t in snapshot.teams
    }
    matrix_by_team: dict[int, NeedMatrix] = {
        t.id: build_need_matrix(t, tuning=ctx.tuning) for t in snapshot.teams
    }

    pending_by_player: dict[int, list[SigningOffer]] = defaultdict(list)
    for offer in pending_offers:
        pending_by_player[offer.player_id].append(offer)
    # Slice 12-A: "the eligibility set is (team_id, player_id) ∈ pending_
    # by_player's pairs" (round 11 §5.3) -- costs no new plumbing, `pending_
    # offers` already carries exactly the incumbent pairs `run_resign_
    # window` built. Never re-derived from `_expiring_by_team` (round 11's
    # own explicit warning against a second definition of the same fact).
    pending_pairs: set[tuple[int, int]] = {(o.team_id, o.player_id) for o in pending_offers}

    signings: list[SigningExecution] = []
    records: list[DecisionRecord] = []

    for round_num in range(MAX_FA_ROUNDS):
        if not remaining_pool:
            break

        offers_by_player: dict[int, list[SigningOffer]] = defaultdict(list)
        offer_records: dict[tuple[int, int], DecisionRecord] = {}
        offered_this_round: set[tuple[int, int]] = set()
        """(team_id, player_id) pairs already offered this round -- Nit 6
        fix. Without this, a team whose pending-offer player (from `run_
        resign_window`) also independently lands in that same team's own
        top-`FA_TARGETS_PER_TEAM` targets got a second `construct_offer`
        call for the identical player in the same round, silently
        overwriting `offer_records[(team_id, player_id)]` while both
        offers were recorded in `offers_by_player`."""

        if round_num == 0:
            # Recomputed (never replayed verbatim) so every pending offer
            # carries a real `DecisionRecord` -- `run_resign_window`'s own
            # return type discards the record it built (module docstring).
            # **Not byte-for-byte with `run_resign_window`'s own offer**
            # (critic round 18, nit 1): that function prices against raw
            # `ctx.room_by_team[team_id]`, while this recompute prices
            # against `_effective_capacity(...)`, which can exceed raw
            # room once an unused exception is larger (e.g. `room=$1`
            # prices at `min_salary` there, `mle_amount` here). This is
            # the more consistent figure -- an incumbent re-signing in
            # round 0 should see the same exception ledger every other
            # team's round-0 offer sees -- but it is a real behavior
            # difference from `run_resign_window`'s own pending offer,
            # not a byte-identical replay of it.
            for player_id, pending in pending_by_player.items():
                if player_id not in remaining_pool:
                    continue
                rp = remaining_pool[player_id]
                # Slice 12-A: every `pending_by_player` entry is, by
                # definition, an incumbent re-sign (`pending_pairs` above)
                # -- price it against the same Bird-aware ceiling `run_
                # resign_window`'s own pending offer used (agreeing in
                # kind, not byte-for-byte -- see that function's docstring).
                prior_salary = _incumbent_prior_salary(rp, ctx.season)
                years_pro = rp.player.years_pro if prior_salary is not None else None
                for pending_offer in pending:
                    team_id = pending_offer.team_id
                    # Round 14 §5.3: a team within one min_salary of apron_2
                    # cannot legally sign anyone this pass -- skip before
                    # construct_offer (H5's trap: a non-positive capacity
                    # passed into construct_offer re-inflates to the full
                    # MLE ladder, not a small offer).
                    if not _can_sign_at_all(
                        team_id, apron_room_remaining, next_season_cap_settings
                    ):
                        continue
                    offer, record = construct_offer(
                        rp,
                        matrix_by_team[team_id],
                        team_id=team_id,
                        philosophy=ctx.philosophy_by_team[team_id],
                        window=ctx.window_by_team[team_id],
                        cap_settings=next_season_cap_settings,
                        room=_effective_capacity(
                            team_id, room_remaining, exceptions_used, next_season_cap_settings,
                            incumbent_prior_salary=prior_salary,
                            incumbent_years_pro=years_pro,
                            apron_room_remaining=apron_room_remaining,
                        ),
                        season=ctx.next_season,
                        tuning=ctx.tuning,
                    )
                    offers_by_player[player_id].append(offer)
                    offer_records[(team_id, player_id)] = record
                    records.append(record)
                    offered_this_round.add((team_id, player_id))

        for team_snapshot in snapshot.teams:
            team_id = team_snapshot.id
            if team_id in ctx.self_managed_team_ids:
                # Decision P5-45's FREE_AGENCY bidder-loop row, unchanged
                # across every later round -- a human-managed team places
                # no bids and signs nobody here. (The round-0 incumbent
                # loop above needs no matching check: every `pending_by_
                # player` entry traces back to `run_resign_window`'s own
                # output, which already excludes human-managed teams --
                # see that function's own skip -- so no pending offer here
                # can ever name a human team's id.)
                continue
            legal_room = signing_decisions.legal_capacity(
                room_remaining[team_id], next_season_cap_settings
            )
            if legal_room <= 0:
                continue
            if roster_count[team_id] >= next_season_cap_settings.roster_max:
                continue
            # Round 14 §5.3: same apron_2 gate as the round-0 incumbent
            # loop above. The ledger does not mutate during offer
            # construction (all signings in this round resolve only after
            # every offer this round is built, below) -- one gate per loop
            # entry is sufficient; do not "helpfully" add a per-target
            # re-check inside the `for rp in targets:` loop below.
            if not _can_sign_at_all(team_id, apron_room_remaining, next_season_cap_settings):
                continue

            matrix = matrix_by_team[team_id]
            philosophy = ctx.philosophy_by_team[team_id]
            team_window = ctx.window_by_team[team_id]
            candidates = [
                rp
                for rp in remaining_pool.values()
                if (team_id, rp.id) not in offered_this_round
            ]
            targets = sorted(
                candidates,
                key=lambda rp: (
                    -trade_decisions.fit_value_for(
                        rp,
                        matrix,
                        league_rng_seed=ctx.league_rng_seed,
                        team_id=team_id,
                        season=ctx.season,
                        phase=ctx.phase,
                        philosophy=philosophy,
                        window=team_window,
                        cap_settings=ctx.cap_settings,
                        tuning=ctx.tuning,
                    ),
                    rp.id,
                ),
            )[:FA_TARGETS_PER_TEAM]

            for rp in targets:
                # Slice 12-A: a leftover incumbent from an earlier round's
                # unaccepted round-0 offer can resurface here (`offered_
                # this_round` resets every round) -- still price it against
                # the Bird bucket if `(team_id, rp.id)` is one of `run_
                # resign_window`'s own pending pairs, not just at round 0.
                is_pending = (team_id, rp.id) in pending_pairs
                prior_salary = _incumbent_prior_salary(rp, ctx.season) if is_pending else None
                years_pro = rp.player.years_pro if prior_salary is not None else None
                offer, record = construct_offer(
                    rp,
                    matrix,
                    team_id=team_id,
                    philosophy=philosophy,
                    window=team_window,
                    cap_settings=next_season_cap_settings,
                    room=_effective_capacity(
                        team_id, room_remaining, exceptions_used, next_season_cap_settings,
                        incumbent_prior_salary=prior_salary,
                        incumbent_years_pro=years_pro,
                        apron_room_remaining=apron_room_remaining,
                    ),
                    season=ctx.next_season,
                    tuning=ctx.tuning,
                )
                offers_by_player[rp.id].append(offer)
                offer_records[(team_id, rp.id)] = record
                records.append(record)
                offered_this_round.add((team_id, rp.id))

        best_guaranteed: dict[int, int] = {
            pid: max(o.guaranteed_total for o in offers)
            for pid, offers in offers_by_player.items()
            if offers
        }
        players_with_offers = sorted(best_guaranteed, key=lambda pid: (-best_guaranteed[pid], pid))

        signed_any = False
        for player_id in players_with_offers:
            if player_id not in remaining_pool:
                continue
            rp = remaining_pool[player_id]
            offers = tuple(offers_by_player[player_id])
            # Decision P4-16: `motivation`/`matrix_by_team` are `None`
            # whenever `ctx.motivation_by_player` is `None` (the default,
            # every pre-Phase-4 caller) -- `rank_offers` falls back to its
            # original Decision P2-21 placeholder byte-for-byte in that
            # case. `matrix_by_team` itself is already built above, once
            # per team, for offer construction -- reused here rather than
            # rebuilt.
            player_motivation = (
                ctx.motivation_by_player.get(rp.id) if ctx.motivation_by_player else None
            )
            winner = rank_offers(
                offers, rp.player, motivation=player_motivation, matrix_by_team=matrix_by_team
            )[0]
            team_id = winner.team_id
            if roster_count[team_id] >= next_season_cap_settings.roster_max:
                continue  # lapses -- player stays in the pool for next round

            # B1/B1b fix, third round (critic round 18 -- see run_free_
            # agency's own docstring for why the prior two attempts, both
            # re-checking against the stateless `signing.legal_capacity`,
            # could not fix this). `winner.annual_salary` was baked in
            # against `_effective_capacity(...)` as it stood at the START
            # of this round's offer construction -- if this same team
            # already won an earlier player this round, or has already
            # used an exception this pass, that baked-in salary may no
            # longer be affordable against the team's now-current
            # capacity. Re-derive the CURRENT ceiling and treat an offer
            # that no longer fits under it the same way an over-`roster_
            # max` offer is already treated above: it lapses, the player
            # stays in the pool, and a fresh, affordably-capped offer gets
            # built for them next round. `_finalize_signing` derives the
            # tag from whichever bucket actually covers this exact salary
            # (B1b) and mutates `room_remaining`/`exceptions_used`
            # accordingly, so the accept decision and the tag can never
            # disagree.
            # Slice 12-A: re-derive the SAME incumbent gate offer construction
            # used for this exact (team_id, player_id) pair, so the accept-
            # time re-check and `_finalize_signing`'s tag can never disagree
            # with what the winning offer was actually priced against.
            is_pending = (team_id, player_id) in pending_pairs
            prior_salary = _incumbent_prior_salary(rp, ctx.season) if is_pending else None
            # §5.6 item 8: one local resolution, passed identically to both
            # calls below, so the capacity check and the finalize tag can
            # never drift out of agreement on which incumbent this is.
            years_pro = rp.player.years_pro if prior_salary is not None else None
            current_capacity = _effective_capacity(
                team_id, room_remaining, exceptions_used, next_season_cap_settings,
                incumbent_prior_salary=prior_salary,
                incumbent_years_pro=years_pro,
                apron_room_remaining=apron_room_remaining,
            )
            if winner.annual_salary > current_capacity:
                continue  # no longer affordable this round -- lapses to next round

            record = offer_records[(team_id, player_id)]
            signing_method = _finalize_signing(
                team_id, winner.annual_salary, room_remaining, exceptions_used,
                next_season_cap_settings,
                incumbent_prior_salary=prior_salary,
                incumbent_years_pro=years_pro,
                apron_room_remaining=apron_room_remaining,
            )
            signings.append(
                SigningExecution(
                    team_id=team_id,
                    player_id=player_id,
                    years=winner.years,
                    annual_salary=winner.annual_salary,
                    start_season=ctx.next_season,
                    signing_method=signing_method,
                    record=record,
                )
            )
            # B2 fix (critic round 51): a signing that will not physically
            # occupy a next-season slot (`rp` past `ctx.retirement_age` at
            # `ctx.next_season`) must not be counted toward `roster_count`
            # -- the same overcount class Decision P2-37 fixed on the read
            # side, now closed on the write side too. The signing itself
            # is unconditional above (legal to sign a player about to
            # retire); only the bookkeeping this loop's own `roster_max`
            # gate and `_enforce_roster_floor`'s floor loop read is guarded.
            # `replace(rp, contract=None)` strips `rp`'s stale pre-signing
            # contract (real and non-None for the `_expiring_by_team` half
            # of the pool, last year == `ctx.next_season - 1` by
            # definition) before the check -- see `_enforce_roster_floor`'s
            # own docstring for why calling `_occupies_next_season` on the
            # raw `rp` would wrongly trip its contract-expiry branch for
            # every ordinary re-signing of an expiring incumbent,
            # regardless of age. The fresh deal being signed here always
            # starts at `ctx.next_season`, so it covers `next_season` by
            # construction -- only age is actually in question.
            if _occupies_next_season(
                replace(rp, contract=None), ctx.next_season, ctx.retirement_age
            ):
                roster_count[team_id] += 1
            del remaining_pool[player_id]
            signed_any = True

        if not signed_any:
            break

    # No apron_room_remaining argument -- deliberate (round 14 §3 H2:
    # every floor signing is Δused = 0, so it must not touch the ledger).
    floor_signings, floor_records = _enforce_roster_floor(
        snapshot, ctx, remaining_pool, roster_count, room_remaining, matrix_by_team
    )
    signings.extend(floor_signings)
    records.extend(floor_records)

    return FreeAgencyResult(signings=tuple(signings), records=tuple(records))


# ---------------------------------------------------------------------------
# run_draft_phase
# ---------------------------------------------------------------------------


def run_draft_phase(
    snapshot: LeagueSnapshot,
    ctx: GmContext,
    *,
    prospects: tuple[RosterPlayer, ...],
    draft_order: tuple[DraftPick, ...],
) -> DraftResult:
    """Builds each team's `NeedMatrix` once and delegates straight to
    `draft.run_draft` -- no new machinery, per the architect spec.

    `draft_order` takes the real, filtered `DraftPick` rows themselves
    (`_eligible_draft_picks_sorted`'s own output), not bare team ids --
    each resulting `DraftSlot.pick_number` is sourced from that pick's own
    real, resolved `pick_number`, never re-derived from this tuple's own
    position (`draft_decisions.draft.py`'s own module/`DraftSlot`
    docstrings: a filtered or resumed `draft_order` is a normal, expected
    input shape here, and list position does not track real pick number
    for either)."""
    slots = tuple(
        draft_decisions.DraftSlot(
            team_id=pick.current_team_id,
            philosophy=ctx.philosophy_by_team[pick.current_team_id],
            window=ctx.window_by_team[pick.current_team_id],
            matrix=build_need_matrix(
                snapshot.team_by_id(pick.current_team_id), tuning=ctx.tuning
            ),
            pick_number=pick.pick_number,
        )
        for pick in draft_order
    )
    results = draft_decisions.run_draft(
        prospects,
        slots,
        league_rng_seed=ctx.league_rng_seed,
        season=ctx.season,
        cap_settings=ctx.cap_settings,
        rookie_scale=ctx.rookie_scale,
        tuning=ctx.tuning,
    )
    return DraftResult(picks=results)


# ---------------------------------------------------------------------------
# run_waivers -- T7a2, architect round 8 §7.3. Closes the FREE_AGENCY ->
# WAIVERS -> PRESEASON graph edge (phase/graph.py:38-39) with a real
# decision: the roster-ceiling gap T7a's smoke run disclosed (deterministic
# roster_max breach at every post-draft/free-agency boundary, no release
# mechanism anywhere in the GM-decision layer) is a production defect, not
# a harness artifact (report §4) -- `run_free_agency` gates every signing
# on next-season occupancy, never trims a team back down once it is full.
# ---------------------------------------------------------------------------


def run_waivers(snapshot: LeagueSnapshot, ctx: GmContext) -> WaiverWindowResult:
    """§7.3's trigger: for each team, while its next-season roster
    occupancy exceeds `next_season_cap_settings.roster_max`, cut one
    player via `waivers.select_waive_candidate` (§7.2's fallback rule --
    see that module's own docstring for why the preferred `cap_delta_
    bonus`-based scoring was rejected). No discretionary cuts below
    `roster_max` -- flexibility cuts are a named, deliberately-declined
    option (report §6's own closing paragraph: "how much roster
    flexibility a `star_maxer` keeps" is a philosophy dial with no ground
    truth to tune it against yet).

    **B1 fix.** The trigger's basis is `_next_season_roster_count(team_
    snapshot, ctx.next_season)` -- next-season occupancy, the same helper
    and calling convention `run_free_agency`/`_enforce_roster_floor` use
    for exactly this purpose (see that function's own docstring, T6a) --
    not raw `len(team_snapshot.roster)`. Today's raw body count still
    includes every player whose contract is genuinely ending THIS season
    (`ctx.season`); nothing under `dba2/` transitions them off the roster
    mid-season, but they are leaving for free at the boundary regardless
    of anything WAIVERS does, so gating the cut count on their presence
    over-triggers this loop and (paired with `select_waive_candidate`'s
    own pool exclusion of the same players, see that function's docstring)
    previously spent real dead cap cutting players who cost nothing to
    just let expire.

    Two hard guards, both asserted rather than silently trusted (§7.3):
    a cut may never take a team below `roster_min` (structurally
    unreachable given the trigger only ever stops at `roster_max >=
    roster_min`, asserted anyway rather than left to arithmetic), and the
    same player id may never be selected twice in one team's own pass
    (`already_cut`, threaded through every call to `select_waive_
    candidate` and re-checked here)."""
    executions: list[WaiverExecution] = []
    records: list[DecisionRecord] = []
    roster_max = ctx.next_season_cap_settings.roster_max
    roster_min = ctx.next_season_cap_settings.roster_min

    for team_snapshot in snapshot.teams:
        team_id = team_snapshot.id
        if team_id in ctx.self_managed_team_ids:
            # Decision P5-45's WAIVERS row, unchanged across every later
            # round -- no automatic cut, even when next-season occupancy
            # exceeds `roster_max`. The human keeps an over-max roster
            # until they waive someone themselves; `_check_legality`'s
            # `roster_max` rule still rejects trade packages that would
            # grow it further.
            continue
        matrix = build_need_matrix(team_snapshot, tuning=ctx.tuning)
        already_cut: set[int] = set()
        active_count = _next_season_roster_count(
            team_snapshot, ctx.next_season, ctx.retirement_age
        )

        while active_count - len(already_cut) > roster_max:
            cut, record = waivers_decisions.select_waive_candidate(
                team_snapshot,
                matrix,
                league_rng_seed=ctx.league_rng_seed,
                season=ctx.season,
                phase=ctx.phase,
                philosophy=ctx.philosophy_by_team[team_id],
                window=ctx.window_by_team[team_id],
                cap_settings=ctx.cap_settings,
                already_cut=frozenset(already_cut),
                tuning=ctx.tuning,
            )
            assert cut.id not in already_cut, (
                f"select_waive_candidate returned player {cut.id} for team {team_id} twice "
                f"in the same waiver pass"
            )
            post_count = active_count - len(already_cut) - 1
            assert post_count >= roster_min, (
                f"run_waivers trigger invariant violated: cutting player {cut.id} would leave "
                f"team {team_id} at {post_count} players, below roster_min={roster_min}"
            )
            already_cut.add(cut.id)
            assert cut.contract is not None, (
                f"select_waive_candidate returned contract-less player {cut.id} for team "
                f"{team_id} -- its own candidate filter must never let this happen"
            )
            executions.append(
                WaiverExecution(
                    team_id=team_id,
                    player_id=cut.id,
                    contract_id=cut.contract.contract.id,
                    waived_in_season=ctx.season,
                    record=record,
                )
            )
            records.append(record)

    return WaiverWindowResult(executions=tuple(executions), records=tuple(records))


# ---------------------------------------------------------------------------
# JSONL encoder
# ---------------------------------------------------------------------------


def encode_decision_record(
    record: DecisionRecord,
    *,
    league_id: int,
    season: int,
    phase: Phase,
    seq: int,
) -> str:
    """One JSON line, fixed key order, no timestamp/wall-clock field
    anywhere -- Family-1 invariant 6 (byte-identical JSONL from the same
    seed+phase sequence) depends on this staying deterministic. Writing the
    line anywhere is T4's job, not this function's."""
    obj = {
        "league_id": league_id,
        "season": season,
        "phase": phase.value,
        "seq": seq,
        "team_id": record.team_id,
        "philosophy": record.philosophy.value,
        "window": record.window.value,
        "subject": record.subject,
        "verdict": record.verdict,
        "score": record.score,
        "threshold": record.threshold,
        "terms": [
            {
                "label": term.label,
                "input_value": term.input_value,
                "weight": term.weight,
                "contribution": term.contribution,
            }
            for term in record.terms
        ],
    }
    return json.dumps(obj, separators=(",", ":"))


# ---------------------------------------------------------------------------
# T4 -- the I/O apply layer. Everything below this line does real Postgres
# I/O; nothing above it does (module docstring). `load_gm_inputs` assembles
# a `LeagueSnapshot`/`GmContext` pair, `run_phase_hook` dispatches one
# phase's worth of T3 decision functions and applies their results via the
# five appliers, and each applier turns one T3-produced mutation
# description into real repository writes.
# ---------------------------------------------------------------------------

# Real NBA teams may trade first-round picks up to 7 seasons into the
# future -- the season range `draft_repo.list_draft_picks_for_league_range`
# (a season-range-shaped bulk loader, `load_gm_inputs` round trip 7) needs,
# absent any "every pick a team currently owns, any season" league-scale
# query that would let this module avoid picking a horizon at all (the
# per-team equivalent, `list_draft_picks_for_team`, is exactly the N+1-
# across-teams shape step 7's own bulk loaders exist to avoid). A judgment
# call, flagged: 7 is the real rule's own ceiling, not an arbitrary round
# number, and `TeamSnapshot.draft_picks` (Decision P2-5) itself carries no
# season bound of its own to size this against.
_MAX_FUTURE_PICK_YEARS = 7

# Decision P2-13's map (`window.py::_TIMELINE_MAP`) is many-to-one
# (`CONTEND` and `WIN_NOW` both persist as `Timeline.WIN_NOW`) -- there is
# no lossless inverse. **Judgment call:** `load_gm_inputs` needs *some*
# `CompetitiveWindow` to seed `apply_hysteresis`'s `previous` argument the
# first time a phase hook classifies a team newly loaded from the DB, and
# the persisted `Timeline` is the only fact on hand to derive it from. Read
# as the identity map on the three names that survive persistence
# (`REBUILD`/`RETOOL`/`WIN_NOW`), never guessing `CONTEND` back from
# `win_now` -- a team that is genuinely contending re-earns `CONTEND` at
# its very next classification (`classify_window` reads the roster fresh
# every call; `apply_hysteresis` only caps how FAR a re-classification can
# move in one evaluation, and `CONTEND` is adjacent to `WIN_NOW` in
# `_BAND_ORDER`, so the cap costs at most one extra evaluation cycle to
# re-climb, never a lost classification).
_TIMELINE_TO_WINDOW: dict[Timeline, CompetitiveWindow] = {
    Timeline.REBUILD: CompetitiveWindow.REBUILD,
    Timeline.RETOOL: CompetitiveWindow.RETOOL,
    Timeline.WIN_NOW: CompetitiveWindow.WIN_NOW,
}
assert set(_TIMELINE_TO_WINDOW) == set(Timeline), (
    "_TIMELINE_TO_WINDOW must cover exactly the three Timeline values"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ARTIFACTS_GM_DIR = _REPO_ROOT / "artifacts" / "gm"


# ---------------------------------------------------------------------------
# load_gm_inputs
# ---------------------------------------------------------------------------


def self_managed_team_ids(teams: Sequence[Team]) -> frozenset[int]:
    """GM-2's predicate (`docs/design/gm-command-surface.md`), extracted
    (RC-22.2, `docs/design/auto-gm-role-construction.md`, C9) into one
    function with two callers: `load_gm_inputs` below and `role_service`'s
    PRESEASON pass, which cannot call `load_gm_inputs` itself (it requires
    a persisted `league_cap_settings` row that only exists for a league's
    creation season -- see `season_service._require_cap_settings`'s own
    docstring). One derivation, two callers -- GM-2's actual invariant was
    "no second copy of the predicate," not "no second caller."

    A team is self-managed when it has a manager AND that manager has not
    delegated to the AI."""
    return frozenset(t.id for t in teams if t.manager_user_id is not None and not t.auto_pilot)


async def load_gm_inputs(
    ex: Executor, *, league_id: int, season: int
) -> tuple[LeagueSnapshot, GmContext]:
    """Assembles one phase hook's worth of already-loaded facts for the
    whole league at once -- a `LeagueSnapshot` (Decision P2-5's join) and
    the `GmContext` every T3 decision function reads alongside it.

    Exactly 10 round trips of its own (no N+1 across teams) -- (1) `league_
    repo.get_league`, (2) `team_repo.list_teams_for_league`, (3) `player_
    repo.list_players_for_league`, (4) `ratings_repo.get_for_league`,
    (5) `contract_repo.list_active_contracts_for_league`, (6) `contract_
    repo.list_contract_years_for_league`, (7) `draft_repo.list_draft_
    picks_for_league_range` (see `_MAX_FUTURE_PICK_YEARS`'s own docstring
    for the season-range judgment call), (8) `league_repo.get_cap_settings`
    for `season`, (9) a second, separate `league_repo.get_cap_settings`
    call for `season + 1` (falling back to `managers.capsheet.project_cap_
    settings` for the latter per Decision P2-16 if no real row exists yet
    -- `capsheet_service.py`'s own documented behavior), (10) `contract_
    repo.list_rookie_scale`. **Nit 2 fix (critic round 19): this was
    previously miscounted as "9" by folding (8) and (9) into one bullet --
    they are two separate `await` round trips, not one.** An eleventh,
    separate call -- `capsheet_service.project_league_cap_sheets` (a whole
    delegated service call, not counted in this function's own 10, exactly
    as `GmContext.room_by_team`'s own docstring and this module's own T3
    docstring both anticipate) -- supplies both `room_by_team` and (slice
    12-A, round 11 §5.3) `apron1_room_by_team`, two narrowings of the same
    already-fetched `SeasonCapProjection` rows, not a second I/O round
    trip -- `room_by_team` read with `include_holds=False` per that
    field's documented caller obligation, `apron1_room_by_team` from the
    identical rows' `.apron1_room`.

    Raises `NotFoundError` if the league does not exist or if `season`
    itself has no real `league_cap_settings` row -- mirrors `capsheet_
    service.project_league_cap_sheets`'s own "raise rather than fabricate"
    call for the identical situation. Raises `ValidationError` if `season`
    does not equal `league.current_season` (critic round 19, nit 8) --
    `room_by_team`'s own `project_league_cap_sheets` call always anchors
    its projection horizon to `league.current_season`, never to this
    function's own `season` parameter, so a caller passing a mismatched
    `season` would otherwise hit a bare, unhelpful `StopIteration` out of
    the `room_by_team` dict comprehension below once `season + 1` falls
    outside that horizon -- surfaced here instead as a clear caller-
    contract violation.

    **A roster is "every player with `roster_status == ACTIVE` on this
    team"** -- `game_sim_service.py`'s own documented interpretive call
    for "a team's playable roster right now," reused verbatim here rather
    than restated as a fresh judgment call: `TWO_WAY`/`INJURED`/
    `FREE_AGENT`/`RETIRED`/`DRAFT_ELIGIBLE` are all excluded from
    `TeamSnapshot.roster` for the same reasoning that module already gives.

    **`ctx.window_by_team` seeds `apply_hysteresis`'s `previous` argument
    from each team's persisted `Timeline`, reverse-mapped via `_TIMELINE_
    TO_WINDOW`** -- see that dict's own docstring for the lossy-map
    judgment call. `ctx.phase` reads `league.current_phase` directly (this
    function takes no `phase` parameter of its own); `run_phase_hook`
    checks that its own `phase` argument agrees with the league's actual
    persisted phase before ever calling this function.

    **Decision P2-39 (architect round 21 §4.1).** `GmContext.room_for_
    signings_by_team` is deliberately left `None` here (its dataclass
    default), never wired to a real `include_holds=True` projection.
    Nothing anywhere under `dba2/` writes a real `cap_holds` row today --
    the table has been schema-only since migration 003 (Phase 0 Decision
    19), and nothing assigned in Phase 2 populates it -- so `project_
    league_cap_sheets(..., include_holds=True)` would return, for every
    team, a value **provably identical** to `room_by_team`'s existing
    `include_holds=False` figure, while doubling `capsheet_service.py`'s
    own documented 150-call `get_cap_hold_total` N+1 to 300 round trips
    for a guaranteed no-op. Whichever future step first writes real
    `CapHold` rows owns wiring this field here, and owes a bulk `cap_
    holds` loader at the same time -- this function should not grow one
    now for a value nothing can populate."""
    league = await league_repo.get_league(ex, league_id)  # 1
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    if season != league.current_season:
        raise ValidationError(
            f"load_gm_inputs called with season={season} but league {league_id}'s "
            f"current_season is {league.current_season} -- room_by_team's cap-sheet "
            f"projection is anchored to league.current_season and cannot answer for "
            f"any other season"
        )

    teams = await team_repo.list_teams_for_league(ex, league_id)  # 2
    players = await player_repo.list_players_for_league(ex, league_id)  # 3
    ratings = await ratings_repo.get_for_league(ex, league_id)  # 4
    contracts = await contract_repo.list_active_contracts_for_league(ex, league_id)  # 5
    contract_years = await contract_repo.list_contract_years_for_league(ex, league_id)  # 6
    pick_seasons = tuple(range(season, season + _MAX_FUTURE_PICK_YEARS))
    picks = await draft_repo.list_draft_picks_for_league_range(ex, league_id, pick_seasons)  # 7

    cap_settings = await league_repo.get_cap_settings(ex, league_id, season)  # 8a
    if cap_settings is None:
        raise NotFoundError(
            f"No league_cap_settings row for league {league_id}, season {season}"
        )
    next_season_cap_settings = await league_repo.get_cap_settings(ex, league_id, season + 1)  # 8b
    if next_season_cap_settings is None:
        next_season_cap_settings = capsheet.project_cap_settings(
            cap_settings, season + 1, tuning=DEFAULT_TUNING
        )

    rookie_scale_rows = await contract_repo.list_rookie_scale(ex)  # 10
    rookie_scale: RookieScaleTable = {row.pick_number: row for row in rookie_scale_rows}

    room_projections = await project_league_cap_sheets(
        ex, league_id=league_id, tuning=DEFAULT_TUNING, include_holds=False
    )
    room_by_team: dict[int, int] = {
        team.id: next(p.room for p in room_projections[team.id] if p.season == season + 1)
        for team in teams
    }
    # Slice 12-A (round 11 §5.3): the identical `SeasonCapProjection` rows
    # `room_by_team` above already reads, narrowed to `.apron1_room`
    # instead of `.room` -- one `project_league_cap_sheets` call, two
    # fields, never a second I/O round trip.
    apron1_room_by_team: dict[int, int] = {
        team.id: next(
            p.apron1_room for p in room_projections[team.id] if p.season == season + 1
        )
        for team in teams
    }

    ratings_by_player = {r.player_id: r for r in ratings}

    years_by_contract: dict[int, list[ContractYear]] = defaultdict(list)
    for cy in contract_years:
        years_by_contract[cy.contract_id].append(cy)
    contract_by_player: dict[int, PlayerContract] = {}
    for c in contracts:
        years = tuple(sorted(years_by_contract.get(c.id, []), key=lambda y: y.season))
        contract_by_player[c.player_id] = PlayerContract(contract=c, years=years)

    players_by_team: dict[int, list] = defaultdict(list)
    for p in players:
        if p.team_id is not None and p.roster_status is RosterStatus.ACTIVE:
            players_by_team[p.team_id].append(p)

    picks_by_team: dict[int, list[DraftPick]] = defaultdict(list)
    for pick in picks:
        picks_by_team[pick.current_team_id].append(pick)

    philosophy_by_team = {t.id: t.gm_philosophy for t in teams}
    window_by_team = {t.id: _TIMELINE_TO_WINDOW[t.timeline] for t in teams}
    # Decision P5-44, predicate updated by GM-2 (docs/design/gm-command-
    # surface.md) -- derived from `teams` (already loaded, call 2 above),
    # no new query. Every hand-constructed `Team` in `tools/` passes
    # `manager_user_id=None` (§24.1), so this is `frozenset()` for every
    # league this project has ever generated. A team is self-managed when
    # it has a manager AND that manager has not delegated to the AI.
    # RC-22.2 (docs/design/auto-gm-role-construction.md, C9): the predicate
    # itself now lives in the module-level `self_managed_team_ids` function
    # above, so `role_service`'s PRESEASON pass shares this exact
    # derivation without a second copy.
    self_managed_ids = self_managed_team_ids(teams)
    # Decision P5-67 -- derived from the SAME `teams` rows, no new query.
    # A separate question from `self_managed_team_ids` (see `GmContext.
    # manual_timeline_team_ids`'s own docstring): `frozenset()` for every
    # league this project has ever generated, until `/timeline` writes a
    # `timeline_source = 'manual'` row.
    manual_timeline_team_ids = frozenset(
        t.id for t in teams if t.timeline_source is TimelineSource.MANUAL
    )

    team_snapshots = []
    for team in teams:
        roster = tuple(
            build_roster_player(p, ratings_by_player[p.id], contract=contract_by_player.get(p.id))
            for p in sorted(players_by_team.get(team.id, ()), key=lambda p: p.id)
        )
        team_snapshots.append(
            build_team_snapshot(
                team,
                roster,
                tuple(sorted(picks_by_team.get(team.id, ()), key=lambda pk: pk.id)),
                cap_settings,
                window=window_by_team[team.id],
            )
        )

    snapshot = build_league_snapshot(league_id, season, tuple(team_snapshots))
    ctx = GmContext(
        league_id=league_id,
        season=season,
        phase=league.current_phase,
        league_rng_seed=league.rng_seed,
        cap_settings=cap_settings,
        next_season_cap_settings=next_season_cap_settings,
        rookie_scale=rookie_scale,
        room_by_team=room_by_team,
        apron1_room_by_team=apron1_room_by_team,
        philosophy_by_team=philosophy_by_team,
        window_by_team=window_by_team,
        tuning=DEFAULT_TUNING,
        self_managed_team_ids=self_managed_ids,
        manual_timeline_team_ids=manual_timeline_team_ids,
    )
    return snapshot, ctx


async def _load_roster_players_by_status(
    ex: Executor, league_id: int, roster_status: RosterStatus
) -> tuple[RosterPlayer, ...]:
    """Every `league_id` player at `roster_status`, wrapped into a
    `RosterPlayer` with `contract=None` -- correct both for a true free
    agent (no active contract row, by definition) and a `draft_eligible`
    prospect (§9.4: "by definition has no contracts row"). A second,
    separate `ratings_repo.get_for_league` call, deliberately not shared
    with `load_gm_inputs`'s own (this helper is not part of that
    function's 10-round-trip budget, and there is no bulk by-player-ids
    ratings loader to fetch only this narrower set with) -- acceptable for
    a pool that is a small subset of the league, not the 30-team hot path
    §10's perf envelope targets."""
    players = await player_repo.list_players_for_league(ex, league_id, roster_status=roster_status)
    ratings = await ratings_repo.get_for_league(ex, league_id)
    ratings_by_player = {r.player_id: r for r in ratings}
    return tuple(
        build_roster_player(p, ratings_by_player[p.id], contract=None) for p in players
    )


def _expiring_by_team(
    snapshot: LeagueSnapshot, season: int
) -> dict[int, tuple[int, ...]]:
    """A rostered player is "expiring" for `run_resign_window`'s purposes
    when their contract's last `contract_years` row IS `season` -- the
    season on the books right now is the last one already signed, so
    `season + 1` needs a new deal or the player reaches free agency.
    Determined here, not inside `run_resign_window` itself (T3's own
    docstring: "this function does not determine expiry itself"), purely
    from the already-loaded `LeagueSnapshot` -- zero extra I/O."""
    result: dict[int, list[int]] = defaultdict(list)
    for team_snapshot in snapshot.teams:
        for rp in team_snapshot.roster:
            if rp.contract is not None and max(y.season for y in rp.contract.years) == season:
                result[team_snapshot.id].append(rp.id)
    return {team_id: tuple(sorted(ids)) for team_id, ids in result.items()}


async def _load_free_agency_pool(
    ex: Executor,
    league_id: int,
    snapshot: LeagueSnapshot,
    expiring_by_team: Mapping[int, tuple[int, ...]],
) -> tuple[RosterPlayer, ...]:
    """The pool `run_free_agency` shops: every already-`FREE_AGENT` player
    in the league, plus every rostered player `_expiring_by_team` names
    (read straight out of `snapshot`, zero extra I/O for that half).
    **Judgment call:** nothing under `dba2/` transitions a player to
    `RosterStatus.FREE_AGENT`/clears `team_id` when their contract's last
    season elapses (no season-rollover job exists yet in this codebase),
    so `roster_status == FREE_AGENT` alone would miss the actual, common
    way a player reaches this pool -- an expiring contract on a still-
    `ACTIVE` roster slot. Both sources are merged here rather than only
    reading `roster_status`."""
    already_fa = await _load_roster_players_by_status(ex, league_id, RosterStatus.FREE_AGENT)
    expiring = tuple(
        snapshot.team_by_id(team_id).player_by_id(player_id)
        for team_id, player_ids in expiring_by_team.items()
        for player_id in player_ids
    )
    return already_fa + expiring


async def _load_draft_prospects(ex: Executor, league_id: int) -> tuple[RosterPlayer, ...]:
    """§9.4's `prospects` input -- every `draft_eligible` player in the
    league (Gap F: nothing under `dba2/` generates one yet; this simply
    reads whatever exists, empty or not)."""
    return await _load_roster_players_by_status(ex, league_id, RosterStatus.DRAFT_ELIGIBLE)


# ---------------------------------------------------------------------------
# The five appliers (apply_waivers, T7a2, defined below apply_windows)
# ---------------------------------------------------------------------------


async def _move_active_contract(ex: Executor, contract_id: int, team_id: int) -> None:
    """A traded player's own active contract moves with them --
    `contract_repo.set_contract_team`'s trade-side counterpart to `player_
    repo.set_team_and_status`.

    RMLP: inherits locks from caller

    **Takes the already-revalidated `contract_id` directly (build step 7),
    not a `player_id` to re-look-up.** Before this change this function
    re-looked-up contracts by player and reassigned WHATEVER active
    contract it found -- the second half of finding F1: `apply_trade`'s
    own revalidation (rule 2, contract ownership) has just identified
    exactly the right contract under lock, so re-deriving it here was
    both redundant and a "reassigns whatever it finds" hazard. Passing
    the id down removes that hazard structurally instead of guarding it.
    Called only from `apply_trade`, which holds both teams' and all
    traded players' tier-1/tier-2 locks for the whole transaction --
    correct, not a missed adoption (`tests/unit/
    test_roster_mutation_lock_discipline.py` check 4's founding member)."""
    await contract_repo.set_contract_team(ex, contract_id, team_id)


@roster_lock.maps_lock_contention
async def apply_trade(
    ex: Executor,
    execution: TradeExecution,
    season: int,
    phase: Phase,
    *,
    league_id: int,
    actor_user_id: int | None = None,
    source_offer_id: int | None = None,
    note: str | None = None,
) -> None:
    """Executes one already-decided `TradeExecution`: moves every traded
    player (`player_repo.set_team_and_status`) and their active contract
    (`_move_active_contract`) to the receiving side, and reassigns every
    traded pick's `current_team_id` (`draft_repo.set_current_team`) --
    all inside one transaction, so a mid-write failure leaves neither
    roster half-updated (§5's "a trade's writes ... run inside one
    transaction" is exactly what `data/db.py::transaction` exists for).

    docs/design/phase-5-discord-presentation.md §18 (Decision P5-34, as
    amended by P5-74): the trade's own `league_transactions` row is
    written last, inside this SAME transaction, so a trade and its
    record commit or roll back together -- there is no trade with no
    record (§1 finding 8, the gap this decision exists to close).
    `season`/`phase` are REQUIRED positional arguments (both columns are
    `NOT NULL`; a defaulted `None` would insert a rejected NULL, per
    P5-74's own "no third option" rule) and `league_id` is a required
    keyword-only argument for the identical reason `apply_signing`
    already takes one: `TradeExecution` carries no `league_id` of its
    own (ids only, that dataclass's own docstring), and every caller of
    this function already has it on hand. **There are exactly two
    callers: `run_phase_hook`'s trade branch, and `offer_service.
    _resolve_locked` -- the human trade-offer surface's single accept
    core, reached by `respond`, `propose`'s AI-partner path and
    `counter`'s AI-partner path (docs/design/phase-5-trade-offer-review-
    surface.md §13.5).** Each runs the AI's own `_check_legality` on both
    sides immediately upstream, from the same snapshot, under the same
    locks, which is how this function's disclosed non-revalidation of
    cap/roster-max/salary-matching legality (below) is compensated at
    every call site; `tests/unit/test_roster_mutation_lock_discipline.py`
    pins the caller set at these two, so a third caller has to be added
    there deliberately.
    `actor_user_id`/`source_offer_id`/`note` stay optional because their
    columns are nullable. **`cycle_index` is deliberately NOT a
    parameter here yet**
    -- migration 013's own docstring discloses why (`league_transactions.
    cycle_index` is deferred to Stage E's orchestrator, the only correct
    single-derivation site for it per Decision P5-71).

    **RMLP §6.5, bug (e) + F1 (build step 7).**
    - **Discover:** none needed as I/O -- `execution` already carries
      every id; lock targets are `teams=[team_a_id, team_b_id]`,
      `players=players_a_to_b + players_b_to_a`.
    - **Lock:** one `acquire` call with BOTH teams and ALL players,
      upfront, as one set -- never per-player inside a loop (that pattern
      is exactly what would create intra-transaction out-of-order
      acquisition).
    - **Revalidate, four rules, under lock:** (1) player ownership -- the
      locked player's `team_id` must equal the sending team; (2) contract
      ownership -- their `ACTIVE` contract's `team_id` must equal the
      sending team; (3) contract existence -- a traded player with NO
      `ACTIVE` contract at revalidation time (e.g. waived/released
      between decision and application) raises, rather than silently
      leaving a rostered player with no contract, a state no legality
      gate/cap computation/roster count in this codebase expects and
      that has no legitimate producer under `dba2/`; (4) status legality
      -- the locked player's `roster_status` must be `ACTIVE`, `TWO_WAY`,
      or `INJURED` (the only tradeable states; `RETIRED` may still carry
      a `team_id` so this is not redundant with rule 1). Traded picks are
      revalidated the same way against `current_team_id` -- picks need no
      lock tier of their own because a pick is only tradeable by the team
      that owns it, and that team's tier-1 lock is already held (sound
      only because `draft_repo.set_current_team` has no caller outside
      the protocol, per the discipline test's closure check).
    - **Write (CHANGED):** `_move_active_contract` now takes the
      REVALIDATED contract id, not a player id (see that function's own
      docstring). **Roster status is PRESERVED, not forced to `ACTIVE`**
      -- `RosterStatus` has `TWO_WAY` and `INJURED` as legitimate
      rostered, tradeable states; forcing `ACTIVE` would silently heal an
      injured player and silently convert a two-way player into a
      standard roster spot, and `roster_status` is what roster-max
      counting and the cap gates read, so the second case is a real
      cap/roster-count corruption, not cosmetic. This is a disclosed
      behavior change from the pre-RMLP code, which unconditionally wrote
      `RosterStatus.ACTIVE`.
    - **NOT revalidated, named per the honest-scoping rule (§3.0):**
      receiving-team roster-max, cap/apron legality, and trade
      salary-matching. Disclosed gap, not closed this round -- pre-
      existing (this function never checked these), and closing it means
      duplicating the AI's own trade-legality basis inside the applier, a
      divergence class this repo has already paid for once (`tests/
      integration/test_gm_command_service_legality_matches_ai_basis.py`).
      Because this function now holds tier-1 locks on both teams for the
      whole transaction, a future revalidation here needs no further
      protocol work first -- the atomicity it would need already exists.
    """
    async with transaction(ex) as conn:
        # `players=` inlines both tuples directly (rather than through an
        # intermediate variable) so the discipline test's check 2 (`tests/
        # unit/test_roster_mutation_lock_discipline.py`) can statically
        # verify this lock set actually covers every traded player -- a
        # deliberately brittle-against-refactors static shape check.
        lock_set = await roster_lock.acquire(
            conn,
            league_id=league_id,
            teams=[execution.team_a_id, execution.team_b_id],
            players=execution.players_a_to_b + execution.players_b_to_a,
        )

        moves: list[tuple[int, int, int]] = []  # (player_id, sending_team, receiving_team)
        for player_id in execution.players_a_to_b:
            moves.append((player_id, execution.team_a_id, execution.team_b_id))
        for player_id in execution.players_b_to_a:
            moves.append((player_id, execution.team_b_id, execution.team_a_id))

        contract_id_by_player: dict[int, int] = {}
        for player_id, sending_team_id, _receiving_team_id in moves:
            player = lock_set.players_by_id[player_id]
            if player.team_id != sending_team_id:
                raise ValidationError(
                    f"Player {player_id} belongs to team {player.team_id}, not the "
                    f"expected sending team {sending_team_id} -- the trade was not applied."
                )
            if player.roster_status not in (
                RosterStatus.ACTIVE,
                RosterStatus.TWO_WAY,
                RosterStatus.INJURED,
            ):
                raise ValidationError(
                    f"Player {player_id} has roster_status={player.roster_status.value}, "
                    "which is not tradeable -- the trade was not applied."
                )
            contracts = await contract_repo.list_contracts_for_player(conn, player_id)
            active_contract = next(
                (c for c in contracts if c.status is ContractStatus.ACTIVE), None
            )
            if active_contract is None:
                raise ValidationError(
                    f"Player {player_id} has no active contract -- they were waived or "
                    "released after this trade was decided; the trade was not applied."
                )
            if active_contract.team_id != sending_team_id:
                raise ValidationError(
                    f"Player {player_id}'s active contract belongs to team "
                    f"{active_contract.team_id}, not the expected sending team "
                    f"{sending_team_id} -- the trade was not applied."
                )
            contract_id_by_player[player_id] = active_contract.id

        pick_moves: list[tuple[int, int, int]] = []  # (pick_id, sending_team, receiving_team)
        for pick_id in execution.picks_a_to_b:
            pick_moves.append((pick_id, execution.team_a_id, execution.team_b_id))
        for pick_id in execution.picks_b_to_a:
            pick_moves.append((pick_id, execution.team_b_id, execution.team_a_id))
        for pick_id, sending_team_id, _receiving_team_id in pick_moves:
            pick = await draft_repo.get_draft_pick(conn, pick_id)
            if pick is None or pick.current_team_id != sending_team_id:
                actual = pick.current_team_id if pick is not None else None
                raise ValidationError(
                    f"Pick {pick_id} is not currently owned by team {sending_team_id} "
                    f"(actual owner: {actual}) -- the trade was not applied."
                )

        for player_id, _sending_team_id, receiving_team_id in moves:
            player = lock_set.players_by_id[player_id]
            await player_repo.set_team_and_status(
                conn, player_id, receiving_team_id, player.roster_status
            )
            # PD-8 (docs/design/gm-command-surface.md): a trade is the
            # sharpest edge this decision names -- a rival GM's shot_diet/
            # usage_mode/defense_mode orders otherwise keep applying to a
            # player who no longer plays for them. `player.team_id !=
            # receiving_team_id` is always true here (a trade always moves
            # between two different teams), but the guard is written
            # explicitly rather than assumed, matching every other PD-8
            # call site.
            if player.team_id != receiving_team_id:
                await role_repo.clear_player_directive(
                    conn, league_id=league_id, player_id=player_id
                )
            await _move_active_contract(conn, contract_id_by_player[player_id], receiving_team_id)
        for pick_id, _sending_team_id, receiving_team_id in pick_moves:
            await draft_repo.set_current_team(conn, pick_id, receiving_team_id)

        await txn_repo.create_trade_transaction(
            conn,
            league_id=league_id,
            season=season,
            phase=phase,
            team_a_id=execution.team_a_id,
            team_b_id=execution.team_b_id,
            players_a_to_b=execution.players_a_to_b,
            players_b_to_a=execution.players_b_to_a,
            picks_a_to_b=execution.picks_a_to_b,
            picks_b_to_a=execution.picks_b_to_a,
            actor_user_id=actor_user_id,
            source_offer_id=source_offer_id,
            note=note,
        )


@roster_lock.maps_lock_contention
async def apply_signing(ex: Executor, execution: SigningExecution, *, league_id: int) -> None:
    """Executes one already-decided `SigningExecution` via `roster_
    service.sign` -- a flat `annual_salary` across `execution.years`
    seasons starting at `execution.start_season` (mirrors `SigningOffer.
    guaranteed_total`'s own flat-schedule assumption). `league_id` is a
    caller-supplied fact (`SigningExecution` carries none of its own, ids-
    only per that dataclass's docstring) -- every caller of this function
    (`run_phase_hook`) already has it on hand. `contract_type` is always
    `ContractType.STANDARD` here -- rookie-scale signings are `apply_
    draft`'s concern (not built this dispatch), never this one's.

    **Terminates the player's existing `ACTIVE` contract first, if any,
    but ONLY if it has genuinely expired** (`end_season <
    execution.start_season`) — RMLP build step 4, closing bug (d).
    `status=EXPIRED`, `terminated_in_season=execution.start_season - 1`,
    then calls `sign()`. A genuine wiring finding from connecting T3's
    pure layer to real Postgres, not a T3 bug: `_load_free_agency_pool`'s
    own documented judgment call means this module's FA pool includes
    players whose contract's *last* `contract_years` row is the season
    that just ended -- their `contracts` row is still `status='active'`
    in the DB at the moment this signing executes. Without terminating it
    first, `roster_service.sign`'s own "one active contract per player"
    guard (Phase 0 step-8 critic round, B2) would raise `ValidationError`
    on every single re-signing and on every departure from an expiring
    deal. A player already `FREE_AGENT` (no active contract row) is
    unaffected -- there is nothing to terminate.

    **The missing half of that fix, closed here: an `ACTIVE` contract
    that has NOT yet expired (`end_season >= start_season`) must never be
    silently terminated -- it must raise.** Before this guard, any AI
    free-agency execution for a player still genuinely under contract
    (a real bug in the decision layer, or a stale/duplicate execution)
    would silently void a live deal with no error, corrupting the cap
    sheet with no trace. A contract satisfying the expiry guard has, by
    construction, no `contract_years` row at or after `start_season` --
    hence no remaining guaranteed money, hence correctly no dead-cap row
    (do not "fix" this by adding one). This also makes `terminated_in_
    season = start_season - 1` always `>= end_season >= signed_in_season`,
    which is the precondition migration 017c's `CHECK` constraint depends
    on (build step 8 has a hard dependency on this guard landing first).

    `terminate_contract`'s return is checked: a `False` here is now (build
    step 7) an invariant violation, not a race -- the player lock is held
    (see below) and the contract list is read under it, so a concurrent
    terminator of the same contract would itself be blocked on the same
    player lock.

    **RMLP §6.4 (build step 7) -- `acquire` runs BEFORE reading `list_
    contracts_for_player`, moved UP out of `sign()` to the first statement
    of this function's own transaction.** This is the whole fix for "the
    lock (inside `sign()`) is reachable only after the very state this
    function already read and decided to terminate" -- previously the
    player lock came from `sign()`, downstream of this function's own
    unlocked `list_contracts_for_player` read and termination decision.
    `sign()` keeps its own `acquire` call (a no-op re-acquisition, see
    `roster_lock`'s idempotent-nesting property) so it stays correct when
    called directly."""
    async with transaction(ex) as conn:
        await roster_lock.acquire(
            conn, league_id=league_id, teams=[execution.team_id], players=[execution.player_id]
        )
        existing = await contract_repo.list_contracts_for_player(conn, execution.player_id)
        active_contract = next(
            (c for c in existing if c.status is ContractStatus.ACTIVE), None
        )
        if active_contract is not None:
            if active_contract.end_season >= execution.start_season:
                raise ValidationError(
                    f"Player {execution.player_id} is under contract with team "
                    f"{active_contract.team_id} through season "
                    f"{active_contract.end_season} -- waive or release that contract "
                    "before signing a new one."
                )
            terminated = await contract_repo.terminate_contract(
                conn,
                active_contract.id,
                status=ContractStatus.EXPIRED,
                terminated_in_season=execution.start_season - 1,
            )
            if not terminated:
                raise ValidationError(
                    f"Contract {active_contract.id} is not active -- it was terminated "
                    "by another action."
                )

        year_salaries = {
            execution.start_season + i: execution.annual_salary for i in range(execution.years)
        }
        await roster_service.sign(
            conn,
            league_id=league_id,
            player_id=execution.player_id,
            team_id=execution.team_id,
            signed_in_season=execution.start_season,
            contract_type=ContractType.STANDARD,
            signing_method=execution.signing_method,
            year_salaries=year_salaries,
        )


async def apply_draft(
    ex: Executor,
    *,
    draft_id: int,
    pick: DraftPick,
    result: draft_decisions.DraftPickResult,
    next_round: int,
    next_pick_number: int,
) -> None:
    """Executes one already-decided `DraftPickResult` against the real
    `pick: DraftPick` row it was selected for: `draft_repo.set_used_for_
    player`, `draft_repo.create_draft_selection`, then `draft_repo.
    advance_draft_pointer` to `(next_round, next_pick_number)` -- the
    caller's job to compute (`run_phase_hook` passes the next pick in
    the same draft's own order, or one past the last pick's own number
    when this was the final selection).

    **Signature judgment call, flagged per the dispatch instructions:**
    `DraftPickResult` (step 9) carries `pick_number`/`team_id`/
    `prospect_id`/`value`/`record` but no `pick_id`, `draft_id`, or
    `round` -- none of which this applier can do its job without
    (`create_draft_selection`/`advance_draft_pointer` both require a real
    `draft_id`, and `create_draft_selection` also wants `round`/`pick_
    asset_id`). Taking the real `pick: DraftPick` row plus `draft_id`/
    `next_round`/`next_pick_number` as explicit parameters (rather than
    the dispatch's literal `pick_id`/`player_id`/`team_id` three-argument
    sketch) is what makes every one of those three repo calls well-formed
    -- `run_phase_hook`'s own DRAFT branch is what has all of these values
    on hand at once (it built `draft_order` from the same `pick` rows).
    `player_id`/`team_id` are still both present, as `result.prospect_id`/
    `result.team_id`.

    **Does not move the drafted player onto the team's roster or sign a
    rookie-scale contract** -- deliberately out of scope: the dispatch's
    own literal repo-call list for this applier names exactly these three
    `draft_repo` writes and nothing under `player_repo`/`contract_repo`/
    `roster_service`. A drafted prospect's rookie-scale signing is left as
    a real, open gap for whichever later slice owns POST_DRAFT_TRADES/
    rookie signings, not silently patched in here."""
    async with transaction(ex) as conn:
        await draft_repo.set_used_for_player(conn, pick.id, result.prospect_id)
        await draft_repo.create_draft_selection(
            conn,
            draft_id=draft_id,
            pick_number=pick.pick_number,
            round=pick.round,
            team_id=result.team_id,
            player_id=result.prospect_id,
            pick_asset_id=pick.id,
        )
        await draft_repo.advance_draft_pointer(
            conn, draft_id, current_round=next_round, current_pick_number=next_pick_number
        )


_DRAFT_FINISHED_MESSAGE = "Every pick in this draft has already been made."
_DRAFT_UNRESOLVED_MESSAGE = (
    "Some picks have no slot yet -- the lottery hasn't finished resolving this draft."
)


async def apply_human_draft_pick(
    conn,
    *,
    league_id: int,
    season: int,
    draft_id: int,
    team_id: int,
    prospect_id: int,
    actor_user_id: int,
) -> DraftPick:
    """One human's draft pick, applied through `apply_draft` -- the same
    applier the AI uses, with the same pointer arithmetic (Decision P5-98.d,
    docs/design/phase-5-stage-d-prime-draft.md).

    RMLP: inherits locks from caller

    **Takes a `team_id` and resolves the pick itself; it never accepts a
    pick.** A signature that took the pick would let a caller resolve one
    team from a command argument, authorize against that team, and then
    apply a *different* team's pick -- the two facts were never bound to
    each other. Resolving `picks[0]` here makes "the team you are authorized
    for" and "the team on the clock" the same object by construction, and
    leaves no argument a caller can get wrong.

    **Who is on the clock is the eligible list, never the `drafts` row's
    pointer.** That pointer is advisory: it is written by `apply_draft` and
    by nothing else, and on the first pick of a fresh draft it still holds
    `create_draft`'s `(1, 1)` default, which is right only by coincidence --
    if pick 1 is lottery-unresolved or already used, `(1, 1)` names a slot
    that is not on the clock.

    **The caller owns the transaction and the `drafts` row lock** (`draft_
    repo.get_draft_for_season_for_update`), and must take the lock BEFORE
    the read this function makes: every refusal below is a decision about a
    board that another applier must not be changing underneath it. This
    function opens no gate and takes no lock of its own -- it mutates no
    RMLP table (`apply_draft` writes `draft_picks.used_for_player_id`, a
    `draft_selections` row and the pointer; no player moves, no contract is
    written).

    **Returns the `DraftPick` row it applied** -- the caller needs its
    `round`/`pick_number`/`current_team_id` to announce the pick, and
    re-reading it after the write would read a pick that `apply_draft` has
    just filtered out of the eligible list.

    **Writes a `league_transactions` row, not a `DecisionRecord`** (Decision
    P5-98.e). `DecisionRecord` has no actor field, a human pick has no terms
    to reconstruct, and the JSONL decision log is the AI's artifact --
    `gm_command_service`'s own module docstring already says a human
    signing's placeholder record is "never persisted, never logged, and does
    not claim a human's signing was an AI decision", and OS-5 ruled the same
    way for human trades. The placeholder built below exists only to satisfy
    `DraftPickResult`'s required field and goes no further than
    `apply_draft`, which never reads it."""
    all_picks = await draft_repo.list_draft_picks_for_season(conn, league_id, season)
    picks = _eligible_draft_picks_sorted(all_picks)
    if not picks:
        # An empty eligible list is NOT the same as a finished draft:
        # `_eligible_draft_picks_sorted` also filters out every pick whose
        # `pick_number` the lottery has not resolved (its B1 rule), so a
        # draft with picks still owed can present an empty list. Telling
        # that person "every pick has already been made" would be a lie
        # with a different fix (the lottery, upstream) than the one the
        # sentence implies.
        if any(p.used_for_player_id is None for p in all_picks):
            raise ValidationError(_DRAFT_UNRESOLVED_MESSAGE)
        raise ValidationError(_DRAFT_FINISHED_MESSAGE)

    pick = picks[0]
    on_the_clock = await team_repo.get_team(conn, pick.current_team_id)
    if on_the_clock is None:
        raise NotFoundError(f"team {pick.current_team_id} does not exist")
    if pick.current_team_id != team_id:
        raise ValidationError(
            f"{on_the_clock.code} is on the clock -- round {pick.round}, "
            f"pick {pick.pick_number}."
        )

    league = await league_repo.get_league(conn, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    permissions.require_can_act_for_team(
        actor_user_id,
        commissioner_user_id=league.commissioner_user_id,
        team_manager_user_id=on_the_clock.manager_user_id,
        team_id=team_id,
    )
    # GM-3 (docs/design/gm-command-surface.md) -- auto-pilot's own branch,
    # separate from the AI-managed check below and placed before it.
    # Widening that check's conjunction to `manager_user_id is None or
    # auto_pilot` would be a no-op (2.3): for an auto-piloted team the
    # manager IS set, so `is_team_manager(actor)` is True for that
    # manager, `not is_team_manager(...)` is False, and the whole
    # conjunction short-circuits to False -- the auto-piloted manager
    # would sail straight through and hand-pick a slot the AI is also
    # about to draft. A separate branch is required to catch BOTH the
    # manager (who the existing conjunction deliberately lets through)
    # and a commissioner (who would otherwise collide with the AI's own
    # next pick -- P5-48's override is bounded to a STALLED human, and an
    # auto-piloted team is not stalled: the AI takes its pick at the next
    # `DRAFT_RESUME` press).
    #
    # Predicate narrowed to `manager_user_id is not None and auto_pilot`
    # (not bare `auto_pilot`) for the same stranded-row reason GM-3b.2(b)
    # narrows its own predicate: a bare `auto_pilot` check placed here
    # would also catch GM-5's disclosed stranded row (`manager_user_id
    # IS NULL`, `auto_pilot = TRUE`), and a commissioner hitting THAT row
    # would be told to run `/gm auto` or `/remove-manager` -- unfollowable
    # advice for a team with no GM to run the former and no manager left
    # to remove with the latter. Requiring a manager sends a stranded row
    # past this branch entirely, into the existing, already-correct
    # AI-managed check below, which reads as plain "AI-managed" for a
    # NULL-manager team.
    if on_the_clock.manager_user_id is not None and on_the_clock.auto_pilot:
        if permissions.is_team_manager(
            actor_user_id, team_manager_user_id=on_the_clock.manager_user_id
        ):
            raise permissions.PermissionDenied(
                f"{on_the_clock.code} is on auto-pilot -- the AI is making its picks. "
                "Run `/gm auto enabled:off` to pick yourself."
            )
        raise permissions.PermissionDenied(
            f"{on_the_clock.code} is on auto-pilot -- the AI is making its picks. "
            f"Only {on_the_clock.code}'s own GM can turn that off (`/gm auto`), or use "
            "`/remove-manager` to hand the team to the AI outright."
        )
    # P5-48's commissioner override is bounded to a stalled HUMAN. The union
    # `require_can_act_for_team` allows is too wide on its own: a
    # commissioner picking for an AI-managed team is a human deciding for
    # the AI, which P5-44 and P5-55 both reject by name. A commissioner who
    # also manages this team IS its manager and takes the ordinary path --
    # `is_team_manager` asks the fact, never a resolved role.
    if not permissions.is_team_manager(
        actor_user_id, team_manager_user_id=on_the_clock.manager_user_id
    ) and on_the_clock.manager_user_id is None:
        raise permissions.PermissionDenied(
            f"{on_the_clock.code} is AI-managed -- the AI makes its own picks."
        )

    prospect = await player_repo.get_player(conn, prospect_id)
    if (
        prospect is None
        or prospect.league_id != league_id
        or prospect.roster_status is not RosterStatus.DRAFT_ELIGIBLE
    ):
        raise ValidationError(
            f"Player {prospect_id} is not a draft-eligible prospect in this league."
        )
    # The taken set, derived from the picks already in hand rather than from
    # a second query (Decision P5-98.c): `apply_draft` writes `set_used_for_
    # player` and `create_draft_selection` in one transaction, so
    # `used_for_player_id` and the selection rows cannot disagree. This is
    # what stops a prospect being drafted twice in ONE draft -- nothing in
    # the schema does (`UNIQUE (draft_id, pick_number)` protects the slot,
    # not the player), and a drafted prospect stays `DRAFT_ELIGIBLE` because
    # `apply_draft` places nobody on a roster.
    taken_at = next(
        (p.pick_number for p in all_picks if p.used_for_player_id == prospect_id), None
    )
    if taken_at is not None:
        raise ValidationError(f"Player {prospect_id} was taken at pick {taken_at}.")

    next_round, next_pick_number = _next_pick_pointer(picks, 0)
    await apply_draft(
        conn,
        draft_id=draft_id,
        pick=pick,
        result=draft_decisions.DraftPickResult(
            pick_number=pick.pick_number,
            team_id=team_id,
            prospect_id=prospect_id,
            value=0.0,
            # Inert placeholder, never persisted and never logged -- see this
            # function's docstring. `terms=()` makes `reconstructs()` hold
            # trivially, which is honest: nothing was evaluated.
            record=DecisionRecord(
                team_id=team_id,
                philosophy=on_the_clock.gm_philosophy,
                window=_TIMELINE_TO_WINDOW[on_the_clock.timeline],
                subject=f"player:{prospect_id}",
                verdict="human_draft_pick",
                score=0.0,
                threshold=0.0,
                terms=(),
            ),
        ),
        next_round=next_round,
        next_pick_number=next_pick_number,
    )
    transaction_row = await txn_repo.create_league_transaction(
        conn,
        league_id=league_id,
        season=season,
        phase=Phase.DRAFT,
        txn_type=TransactionType.DRAFT_PICK,
        actor_user_id=actor_user_id,
        details={
            "team_id": team_id,
            "player_id": prospect_id,
            "round": pick.round,
            "pick_number": pick.pick_number,
        },
    )
    # Two asset rows, the pick and the player, both landing ON the picking
    # team: `from_team_id` is None because neither comes from another team
    # (the pick was already this team's, and a prospect has no team).
    await txn_repo.add_transaction_asset(
        conn,
        transaction_id=transaction_row.id,
        asset_kind=TransactionAssetKind.PICK,
        draft_pick_id=pick.id,
        from_team_id=None,
        to_team_id=team_id,
    )
    await txn_repo.add_transaction_asset(
        conn,
        transaction_id=transaction_row.id,
        asset_kind=TransactionAssetKind.PLAYER,
        player_id=prospect_id,
        from_team_id=None,
        to_team_id=team_id,
    )
    return pick


async def apply_windows(ex: Executor, result: WindowPassResult) -> None:
    """Writes every team's final (post-hysteresis) `CompetitiveWindow` to
    `teams.timeline` via `window.to_timeline` -- Decision P2-13's
    many-to-one persistence map, applied here rather than re-derived by a
    caller.

    **Iterated in ascending `team_id` (RMLP Addendum A5 NIT1), not dict-
    insertion order.** Each `set_timeline` takes an implicit `FOR NO KEY
    UPDATE` on its row, which conflicts with RMLP's own tier-1 locks;
    ascending order is RMLP's canonical tier-1 order, so this loop cannot
    form a wait cycle with a protocol participant. Do not change to an
    unordered iteration. This is deterministic and changes no observable
    outcome; it does NOT close D2-1 (`apply_windows` still commits in its
    own transaction, separate from the trade branch, so TRADE_DEADLINE's
    half-apply gap remains exactly as disclosed)."""
    async with transaction(ex) as conn:
        for team_id, final_window in sorted(result.window_by_team.items()):
            await team_repo.set_timeline(conn, team_id, window.to_timeline(final_window))


async def apply_waivers(ex: Executor, execution: WaiverExecution, *, league_id: int) -> None:
    """Executes one already-decided `WaiverExecution` via the real,
    already-tested `roster_service.waive` (Phase 0's own "single most
    valuable test" target, per that module's docstring) -- `stretch=
    False` deliberately (T7a2, architect round 8 §7.4). The stretch path
    is real and tested, but it adds a decision dial with no §10 metric
    behind it yet; named here as future/step-11 scope, not built this
    slice. `roster_service.waive` itself re-reads `execution.contract_id`'s
    own remaining `contract_years` rows and writes every `dead_cap_
    charges` row, terminates the contract to `WAIVED`, and moves the
    player to `FREE_AGENT` with `team_id=None` -- nothing here duplicates
    that math or re-derives it from `execution`.

    RMLP: inherits locks from callee (roster_service.waive)

    **Gains a keyword-only `league_id` parameter (Addendum A2), forwarded
    straight to `roster_service.waive`'s own new parameter.** `run_phase_
    hook`'s WAIVERS branch already has `league_id` in scope (it passes it
    to `_write_decision_records` too). `WaiverExecution` itself is
    unchanged -- ids only, consistent with `TradeExecution`/
    `SigningExecution`'s own convention. This function issues no
    `WRITE_SHAPED` call of its own and acquires no lock itself -- it is a
    lock INHERITOR by delegation, not a mutator; `roster_service.waive`
    is what actually calls `roster_lock.acquire`."""
    await roster_service.waive(
        ex,
        league_id=league_id,
        contract_id=execution.contract_id,
        waived_in_season=execution.waived_in_season,
        stretch=False,
    )


# ---------------------------------------------------------------------------
# JSONL artifact writer
# ---------------------------------------------------------------------------


def artifact_path(league_id: int, season: int) -> Path:
    """`artifacts/gm/league_{league_id}_season_{season}.jsonl` -- one
    append-only decision log per (league, season). `artifacts/` is
    gitignored (confirmed -- `tools/validate_sim.py` already writes under
    it)."""
    return ARTIFACTS_GM_DIR / f"league_{league_id}_season_{season}.jsonl"


def _write_decision_records(
    records: Sequence[DecisionRecord],
    *,
    league_id: int,
    season: int,
    phase: Phase,
    seq_start: int,
) -> int:
    """Appends `records` to `artifact_path(league_id, season)`, one JSON
    line per record, `\\n`-terminated, `encoding="utf-8"`, `newline=""`
    explicitly -- a bare text-mode `open(..., "a")` on Windows would
    translate every `\\n` to `\\r\\n` on write, silently breaking Family-1
    invariant 6's byte-identical-output requirement (flagged as critical
    in the architect's own design memo). A no-op that touches no file at
    all when `records` is empty -- a no-op phase must genuinely write
    nothing, not create an empty file. Returns the next unused `seq`, so
    a caller stringing several branches together in one `run_phase_hook`
    call keeps one continuous sequence across all of them.

    **`seq`'s real scope (critic round 19, nit 7): unique within one
    `run_phase_hook` invocation's own writes only, not across the whole
    artifact file.** `run_phase_hook` starts a fresh `seq = 0` on every
    call, so `(phase, seq)` is not a stable identity across two different
    calls that both append to the same league-season file -- e.g. two
    separate `REGULAR_SEASON` trade-window hook calls in the same season
    each write their own records starting at `seq=0`. A reader needing to
    tell two calls' records apart needs some other key (line order / file
    position), not `(phase, seq)` alone -- the "one continuous sequence"
    phrase above describes only a single call's own internal ordering
    across its own branches, never the artifact file as a whole."""
    if not records:
        return seq_start
    path = artifact_path(league_id, season)
    path.parent.mkdir(parents=True, exist_ok=True)
    seq = seq_start
    with open(path, "a", encoding="utf-8", newline="") as fh:
        for record in records:
            line = encode_decision_record(
                record, league_id=league_id, season=season, phase=phase, seq=seq
            )
            fh.write(line + "\n")
            seq += 1
    return seq


# ---------------------------------------------------------------------------
# run_phase_hook
# ---------------------------------------------------------------------------


def _eligible_draft_picks_sorted(all_picks: Sequence[DraftPick]) -> list[DraftPick]:
    """The DRAFT branch's own pick list, filtered and ordered -- pulled out
    as its own function (critic round 19) so its `(round, pick_number, id)`
    tiebreak is directly unit-testable against a hand-built input list, not
    only observable through a real Postgres round trip whose own row-return
    order for tied `(round, pick_number)` rows happens, in practice, to
    already match ascending `id` order for a freshly-inserted table (which
    would make a same-tiebreak mutation invisible to an end-to-end-only
    test).

    **B1 fix:** `draft_picks.pick_number` is nullable (a pick's slot isn't
    resolved until the lottery runs), but `draft_selections.pick_number` is
    NOT NULL -- a pick with `pick_number is None` is simply not ready to be
    on the clock yet (an upstream sequencing question -- `DRAFT_LOTTERY`
    hasn't fully run -- not this function's job to fix), so it is filtered
    out here rather than passed through to a NOT-NULL insert that would
    crash.

    **B2 fix:** a pick with `used_for_player_id is not None` has already
    been drafted -- filtering it out too is what makes the DRAFT branch
    idempotent on a second invocation, instead of re-drafting the same
    picks and hitting `draft_selections`' own `(draft_id, pick_number)`
    unique constraint.

    **Tiebreak fix:** the explicit `(round, pick_number, id)` sort key is
    never left to `draft_repo.list_draft_picks_for_season`'s own `ORDER BY
    round, pick_number` (an already-closed step-7 module, not modified
    here) -- that query carries no `id` tiebreak, which under duplicate
    `pick_number`s makes the resulting order Postgres-arbitrary, a direct
    hole in Family-1 invariant 6's determinism requirement. This function
    makes the DRAFT branch's own consumption deterministic regardless of
    whatever order the query happens to return ties in."""
    return sorted(
        (p for p in all_picks if p.pick_number is not None and p.used_for_player_id is None),
        key=lambda p: (p.round, p.pick_number, p.id),
    )


def _next_pick_pointer(picks: Sequence[DraftPick], i: int) -> tuple[int, int]:
    """The `(current_round, current_pick_number)` `apply_draft` must write
    after applying `picks[i]` -- the expression that lived inline in
    `execute_draft`'s applier loop, moved verbatim (Decision P5-98.a) so
    the draft's pointer arithmetic has exactly ONE implementation. A
    second, hand-reimplemented twin in the human-pick applier is the
    defect class P5-39 bans and P4-103/P4-107 have already paid for
    twice; `apply_draft` itself takes the pointer as parameters and its
    own docstring calls computing it "the caller's job", so the only
    place that job can be done once is here.

    **`picks` is ALWAYS the unfiltered output of `_eligible_draft_picks_
    sorted`, never a list truncated at a human-managed team's pick.**
    The last-element branch returns `(this pick's round, this pick's
    number + 1)`, so a caller that truncates the basis at the halt makes
    the last AI pick write `(round, number + 1)` instead of the human's
    own `(round, pick_number)` -- identical whenever pick numbers are
    contiguous within a round, and WRONG at a round boundary, because
    `draft_selections`' `UNIQUE (draft_id, pick_number)` (`alembic/
    versions/004_draft.py:82`) makes pick numbers unique across the whole
    draft: the pick after round 1 pick 30 is round 2 pick **31**, and the
    truncated basis answers `(1, 31)`. The halt truncates the
    `draft_order` handed to `run_draft_phase`; it never truncates this
    function's basis.

    **A pre-existing defect is preserved deliberately, because an
    extraction must preserve one:** the last-element branch names a slot
    that need not exist -- a draft that has drafted through round 1 with
    round 2 still lottery-unresolved (and therefore filtered out by
    `_eligible_draft_picks_sorted`'s B1) ends up pointing at `(1, 31)`.
    That is harmless only because the stored pointer is advisory: every
    reader derives whose turn it is from `_eligible_draft_picks_sorted(
    ...)[0]`, never from the `drafts` row."""
    if i + 1 < len(picks):
        next_pick = picks[i + 1]
        return next_pick.round, next_pick.pick_number
    # `pick_number` is guaranteed not `None` here -- `picks` is already
    # filtered to exclude every unresolved pick (B1 fix).
    return picks[i].round, picks[i].pick_number + 1


async def execute_draft(
    ex: Executor, *, league_id: int, season: int, snapshot: LeagueSnapshot, ctx: GmContext
) -> DraftResult | None:
    """Stage D step 22 (design doc §37, Decision P4-113 item 1):
    `run_phase_hook`'s own `Phase.DRAFT` branch body, extracted verbatim
    minus the `load_gm_inputs` call and the `_write_decision_records`
    call -- both stay with the hook (it needs `snapshot`/`ctx` before
    this function can run, and it writes the JSONL log after this
    function returns, from this function's own `draft_result.picks`). A
    **behaviour-preserving extraction, not a rewrite**: every line below
    is copied from the branch it replaces, in the same order, with no new
    logic -- see Decision P4-114's proof obligation (`tests/integration/
    test_gm_service.py`'s six existing DRAFT-branch cases pass unedited,
    and `seeds/gm_decision_log_digest.json` regenerates byte-identical).

    Reused directly by `tools/dev_league_draft.py` (Stage D step 22's
    corpus-generator call site, Option C per the design doc's §37.2) so
    that driver never hand-reimplements this branch's own B1/B2/tiebreak-
    correct pick selection and pointer arithmetic -- the same "no
    hand-reimplemented twin" ban this document has already enforced twice
    (P4-103 Part A, P4-107 item 6).

    Returns `None` for both "no eligible picks this call" (every pick is
    either unresolved or already used) and "eligible picks exist but no
    prospects were loaded" (Gap F) -- one consistent meaning for "nothing
    was drafted," matching the branch's own pre-extraction contract (nit
    4 fix, preserved here).

    **Two behaviours were added after that extraction, both of which are
    inert in a league with no human managers** (Decisions P5-98.b/c,
    docs/design/phase-5-stage-d-prime-draft.md): the AI stops at the first
    pick owned by a team in `ctx.self_managed_team_ids` instead of
    drafting through it, and the prospect pool excludes every player
    already taken in THIS draft. With `ctx.self_managed_team_ids` empty --
    every league this project has ever generated, the harness and the
    digest corpus among them -- the halt index is `len(picks)`, and the
    taken set is empty on every call that finds an undrafted board, which
    before the halt was every call that found eligible picks at all (the
    AI drafted each of them in one pass, and a second invocation finds an
    empty eligible list through the B2 filter). See the comments at the
    two sites for why the truncation touches only the pure layer's
    `draft_order`."""
    draft_row = await draft_repo.get_draft_for_season(ex, league_id, season)
    if draft_row is None:
        raise NotFoundError(f"No draft for league {league_id}, season {season}")
    all_picks = await draft_repo.list_draft_picks_for_season(ex, league_id, season)
    # B1/B2/tiebreak fixes (critic round 19) -- see
    # `_eligible_draft_picks_sorted`'s own docstring.
    picks = _eligible_draft_picks_sorted(all_picks)
    draft_result: DraftResult | None = None
    if picks:
        # The halt (Decision P5-98.b): the AI drafts up to, and never
        # through, the first pick owned by a human-managed team. `halt` is
        # the index of that pick, or `len(picks)` when no human owns one.
        #
        # **Only the `draft_order` handed to the pure layer is truncated.**
        # Every other use of `picks` -- above all `_next_pick_pointer(picks,
        # i)` below -- keeps reading the FULL list, and that placement is
        # the whole correctness of this halt. `_next_pick_pointer`'s
        # last-element branch answers `(this pick's round, this pick's
        # number + 1)`, so a truncated basis makes the last AI pick write
        # `(1, 31)` where the human's own pick is round 2 pick 31 --
        # `draft_selections`' `UNIQUE (draft_id, pick_number)` makes pick
        # numbers unique across the whole draft, so the pick after round 1
        # pick 30 is round 2 pick 31 and the two coincide only inside a
        # round. With the full basis, the last AI pick writes the human's
        # own `(round, pick_number)` and the pointer already names their
        # pick when they arrive.
        #
        # When `halt == 0` a human is already on the clock: the pure layer
        # is handed an empty order, returns no picks, and the
        # `candidate_result.picks` branch below never opens its transaction
        # -- so a draft waiting on a person writes no pointer and no
        # selection at all, rather than a pointer nobody may act on.
        #
        # Behaviour is preserved EXACTLY when `self_managed_team_ids` is
        # empty (every league this project has ever generated, including
        # the harness and the digest corpus): `halt` is `len(picks)` and
        # `picks[:halt]` is `picks`.
        halt = next(
            (i for i, p in enumerate(picks) if p.current_team_id in ctx.self_managed_team_ids),
            len(picks),
        )
        # The draft-scoped taken set (Decision P5-98.c), derived from the
        # picks already in hand rather than from a second query. `run_draft`
        # pops each winner from an in-memory pool, so "nobody is drafted
        # twice" holds within ONE call -- and the halt is what makes a draft
        # run in more than one. `apply_draft` deliberately leaves a drafted
        # prospect `DRAFT_ELIGIBLE` on no roster (placement is a `tools/`
        # function, open call U2), so without this filter every prospect
        # taken earlier in this same draft is back in the pool on resume,
        # and nothing in the schema stops him being taken twice: `UNIQUE
        # (draft_id, pick_number)` protects the slot, not the player.
        taken = {p.used_for_player_id for p in all_picks if p.used_for_player_id is not None}
        pool = await _load_draft_prospects(ex, league_id)
        prospects = tuple(p for p in pool if p.id not in taken)
        candidate_result = run_draft_phase(
            snapshot, ctx, prospects=prospects, draft_order=tuple(picks[:halt])
        )
        # Nit 4 fix (critic round): `candidate_result.picks` is empty
        # whenever `picks` was non-empty but `prospects` was empty --
        # e.g. no draft-eligible players loaded yet (Gap F, `_load_
        # draft_prospects`'s own docstring). That is a no-op exactly
        # like the "no eligible picks" case below, so it gets the same
        # representation: `draft_result` stays `None`, one consistent
        # meaning for "nothing was drafted" across every branch here,
        # never a `DraftResult(picks=())` that a caller would have to
        # special-case against the documented `None` contract.
        if candidate_result.picks:
            draft_result = candidate_result
            async with transaction(ex) as conn:
                for i, pick_result in enumerate(draft_result.picks):
                    pick = picks[i]
                    next_round, next_pick_number = _next_pick_pointer(picks, i)
                    await apply_draft(
                        conn,
                        draft_id=draft_row.id,
                        pick=pick,
                        result=pick_result,
                        next_round=next_round,
                        next_pick_number=next_pick_number,
                    )
    # else: no eligible picks this call -- either the lottery hasn't
    # resolved every current-round pick's slot yet, or every pick has
    # already been used (B1/B2 fixes above) -- a legitimate no-op,
    # `draft_result` stays `None` exactly like any other phase this
    # hook declines to act on, and nothing is written.
    return draft_result


DRAFT_CONTENTION_MESSAGE = (
    "Another pick is being applied to this draft right now -- try again in a moment."
)
"""The `drafts` row's own 55P03 wording, public because BOTH sites that take
that lock map it at the statement (Decision P5-98.g) -- this module's
`run_draft_cycle` and `gm_command_service.draft_pick`'s first transaction.
One string, so the two cannot drift into telling a person two different
things about the same wait."""


class DraftCycleState(StrEnum):
    """What the draft board looks like AFTER one cycle of the AI applier
    ran -- never a statement about the list the cycle started with
    (Decision P5-98.h). A halt index describes the past; a caller can only
    act on the present, and `/draft`'s reply, the on-the-clock ping and
    Stage E's exit gate are all reads of this value."""

    WAITING_ON_HUMAN = "waiting_on_human"
    """The eligible list is non-empty and `picks[0]` belongs to a
    human-managed team: the AI stopped where it was supposed to."""

    COMPLETE = "complete"
    """The eligible list is empty AND every pick in the draft is used.
    **The second half is not redundant** -- `_eligible_draft_picks_sorted`
    also filters out picks the lottery has not given a `pick_number`, so
    "the list is empty" alone reports a draft with picks still owed as
    finished, and Stage E would then let the league leave DRAFT with real
    picks abandoned."""

    STALLED = "stalled"
    """Anything else: the draft cannot continue and nobody can make it
    continue by picking. Carries a `reason`, because the two shapes have
    different fixes."""

    SKIPPED = "skipped"
    """The cycle did not run, or did not finish -- everything its caller can
    meet between a human's already-committed pick and the resume that
    follows it (Decision P5-98.g/h: the league claim was taken, the phase
    moved, the resume lost a lock, or the resume raised). Never produced by
    `run_draft_cycle` itself, which is passed a connection whose caller has
    already decided it may run; the checks live in `gm_command_service.
    draft_pick`, because a Stage E press legitimately holds the claim while
    running the DRAFT hook and a claim check inside the cycle would make the
    press refuse its own work.

    **Every one of the four is a SKIP and not a raise for one reason: the
    human's pick is durable before the resume starts** (round-1 critic's
    blocking issue B2). A resume that raises past its caller tells a person
    their command failed while their pick stands, and -- because the cog's
    own `except` fires before the announcement -- takes the `#games` line
    and the next person's ping down with it."""


class DraftCycleReason(StrEnum):
    """Why a `STALLED`/`SKIPPED` cycle is what it is -- two different
    problems per state, with two different fixes, so the caller can say
    which one a person is looking at instead of one shrug for both."""

    POOL_EXHAUSTED = "pool_exhausted"
    """An AI team is on the clock and the AI drafted nobody, which after
    the taken-set filter (P5-98.c) can only mean no available prospect
    remains -- Gap F, or a draft class smaller than the draft."""

    UNRESOLVED_PICKS = "unresolved_picks"
    """The eligible list is empty but picks are still owed: the lottery
    has not resolved their `pick_number`. The fix is upstream, in
    `DRAFT_LOTTERY`, which `_eligible_draft_picks_sorted`'s own B1
    docstring already says."""

    CLAIM_HELD = "claim_held"
    """`SKIPPED` only: Stage E's advance claim was held when the resume
    tried to run."""

    PHASE_MOVED = "phase_moved"
    """`SKIPPED` only: the league was no longer in DRAFT when the resume
    tried to run."""

    RESUME_CONTENDED = "resume_contended"
    """`SKIPPED` only: the resume lost a bounded wait for a lock -- the
    `leagues` row a press was updating, or the `drafts` row another applier
    held. Both are the same fact to the person who picked: the pick stands,
    the AI has not continued, and the next `/draft` or `/advance` carries on
    from the board as it is."""

    RESUME_FAILED = "resume_failed"
    """`SKIPPED` only: the resume raised something that is nobody's expected
    outcome (a pool timeout, a connection reset, a bug in the AI pass). The
    traceback is logged where it happened; this reason exists so the reply
    can be honest about the draft having stopped instead of either claiming
    the pick failed or claiming the AI ran."""


@dataclass(frozen=True)
class DraftCycleOutcome:
    """One `run_draft_cycle` call's result: what it drafted, and the state
    of the board it left behind.

    `draft`/`records` are carried in every state, including the ones that
    report a problem -- a cycle that made four picks and then found an
    exhausted pool really did make those four picks, and both its caller's
    decision log and its announcement have to see them. `records` is
    separate from `draft` because **the cycle does not write the decision
    log; its caller does** (P5-98.h): `run_phase_hook` appends them with
    its own running `seq`, and `/draft` writes them after its second
    transaction commits, so no blocking file write lands inside an open
    transaction holding the `drafts` row.

    The pick fields describe `picks[0]` of the post-write eligible list
    where one exists -- who is on the clock now, never who was."""

    state: DraftCycleState
    draft: DraftResult | None = None
    records: tuple[DecisionRecord, ...] = ()
    reason: DraftCycleReason | None = None
    pick_id: int | None = None
    round: int | None = None
    pick_number: int | None = None
    team_id: int | None = None
    manager_user_id: int | None = None


@dataclass(frozen=True)
class DraftCompletion:
    """What `draft_completion_state` says about a board: its `state`, the
    `reason` where the state is `STALLED`, and the `pick` on the clock (the
    first of the eligible list) whenever one is -- `None` for `COMPLETE` and for
    `UNRESOLVED_PICKS`, where nobody is on the clock."""

    state: DraftCycleState
    reason: DraftCycleReason | None = None
    pick: DraftPick | None = None


def draft_completion_state(
    all_picks: Sequence[DraftPick], self_managed_team_ids: Collection[int]
) -> DraftCompletion:
    """Where a draft board stands, as a pure function of its picks and of which
    teams a person manages -- extracted from `run_draft_cycle` (Stage E build
    plan commit 2, P5-39) so that function and the exit gate (P5-99.e) cannot
    disagree about what a finished draft is. Two implementations of "is the
    draft done" is how Stage E would let a league leave `DRAFT` with picks owed.

    An AI team left on the clock means the applier had nobody to give it: the
    halt would have stopped at a human, and the taken-set filter is what makes
    "nobody left" the only remaining explanation (`POOL_EXHAUSTED`). An empty
    eligible list is NOT completion by itself -- the lottery may not have
    resolved a pick's `pick_number` yet, which `_eligible_draft_picks_sorted`
    filters out (`UNRESOLVED_PICKS`). An empty `all_picks` is vacuously
    `COMPLETE`: refusing to enter a phase with no board is the pre-entry
    check's job (P5-99.h), not this predicate's."""
    picks = _eligible_draft_picks_sorted(all_picks)
    if picks:
        pick = picks[0]
        human = pick.current_team_id in self_managed_team_ids
        return DraftCompletion(
            state=DraftCycleState.WAITING_ON_HUMAN if human else DraftCycleState.STALLED,
            reason=None if human else DraftCycleReason.POOL_EXHAUSTED,
            pick=pick,
        )
    if any(p.used_for_player_id is None for p in all_picks):
        return DraftCompletion(
            state=DraftCycleState.STALLED, reason=DraftCycleReason.UNRESOLVED_PICKS
        )
    return DraftCompletion(state=DraftCycleState.COMPLETE)


@roster_lock.maps_lock_contention
async def run_draft_cycle(ex: Executor, *, league_id: int, season: int) -> DraftCycleOutcome:
    """Runs the AI draft forward as far as it may go, and reports where it
    stopped -- `run_phase_hook`'s own DRAFT branch, extracted so the hook
    and `/draft`'s post-pick resume are one implementation rather than two
    (Decision P5-98.h). A behaviour-preserving extraction plus one new
    thing: the `drafts` row lock, and the outcome derived under it.

    **The lock wraps read -> decide -> write, not just the write.** The
    first statements of this function's transaction are `set_lock_timeout`
    and `draft_repo.get_draft_for_season_for_update`; `load_gm_inputs`,
    `execute_draft` (handed this same connection, so its board read, its
    `_eligible_draft_picks_sorted` call and its applier loop all run under
    the lock, its inner `transaction(conn)` nesting by savepoint) and the
    post-write re-read all follow inside it. Locking only the write would
    leave the decision made against a board another applier had already
    changed -- the TOCTOU `apply_human_draft_pick`'s `picks[0]` assertion
    closes one layer down, reopened one layer up. **The cost, owned:** the
    row lock is held across `load_gm_inputs`' eleven round trips and the
    pure pass, and for that span every other pick applier on this league's
    draft waits and then fails at 3 seconds. Two concurrent appliers on one
    draft is precisely what must not happen, and a bounded wait is what
    wait-forever was not.

    **It opens no league gate, deliberately, and an AST assertion in
    `tests/unit/test_roster_mutation_lock_discipline.py` pins that.** A
    draft pick mutates no RMLP table (`apply_draft` writes
    `draft_picks.used_for_player_id`, a `draft_selections` row and the
    pointer; no player moves and no contract is written), and the gate may
    only be opened as the first statement of an outermost transaction --
    which `/draft`'s second transaction is not. The consequence is that
    nothing sets `lock_timeout` for this transaction unless this function
    does, so it sets its own through the one helper that owns that
    statement (`roster_lock.set_lock_timeout`), at the shared tier's own
    value: a gate-less transaction would otherwise wait forever on the
    first blocking lock it took.

    **The `drafts` lock's own 55P03 is mapped here, at the statement.**
    `maps_lock_contention` (still applied, as the backstop for any other
    statement) would give it `_ENTITY_CONTENTION_MESSAGE`, which names a
    team or a player -- the wrong noun for a non-entity row, OS-13's
    disclosed residue, closed here for this one lock.

    **The outcome is derived from the board as it now stands**, by
    re-reading the picks after the applier has written, still under the
    lock -- never from the halt index `execute_draft` computed, which is a
    statement about the list this cycle started with. See
    `DraftCycleState` for what each state means and why `COMPLETE` is not
    "the eligible list is empty".

    `ex` may be a pool (the hook path, which owns no transaction) or an
    open connection (`/draft`'s tx2, which has already taken `leagues FOR
    SHARE` and re-checked the claim and the phase) -- the canonical order
    is gate -> `leagues FOR SHARE` -> `drafts FOR UPDATE`, and this
    function only ever takes the last of the three."""
    async with transaction(ex) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        try:
            draft_row = await draft_repo.get_draft_for_season_for_update(conn, league_id, season)
        except roster_lock.LOCK_CONTENTION_EXCEPTIONS as exc:
            raise ConcurrencyError(DRAFT_CONTENTION_MESSAGE) from exc
        if draft_row is None:
            raise NotFoundError(f"No draft for league {league_id}, season {season}")

        snapshot, ctx = await load_gm_inputs(conn, league_id=league_id, season=season)
        draft_result = await execute_draft(
            conn, league_id=league_id, season=season, snapshot=snapshot, ctx=ctx
        )
        records = tuple(r.record for r in draft_result.picks) if draft_result is not None else ()

        all_picks = await draft_repo.list_draft_picks_for_season(conn, league_id, season)
        completion = draft_completion_state(all_picks, ctx.self_managed_team_ids)
        pick = completion.pick
        if pick is None:
            return DraftCycleOutcome(
                state=completion.state,
                draft=draft_result,
                records=records,
                reason=completion.reason,
            )
        on_the_clock = await team_repo.get_team(conn, pick.current_team_id)
        return DraftCycleOutcome(
            state=completion.state,
            draft=draft_result,
            records=records,
            reason=completion.reason,
            pick_id=pick.id,
            round=pick.round,
            pick_number=pick.pick_number,
            team_id=pick.current_team_id,
            manager_user_id=None if on_the_clock is None else on_the_clock.manager_user_id,
        )


@dataclass(frozen=True)
class PhaseHookResult:
    """Whatever combination of T3 decision-layer results the dispatched
    phase actually touched -- every field `None` except the ones this
    phase's own branch populated. **Judgment call on shape:** a single
    "here is everything that happened" dataclass rather than a tagged
    union, since a real phase (`TRADE_DEADLINE`) can populate more than
    one field at once (window classification AND a trade window both run
    there) -- a union could not represent that case."""

    league_id: int
    season: int
    phase: Phase
    window_pass: WindowPassResult | None = None
    trade_window: TradeWindowResult | None = None
    resign_offers: tuple[SigningOffer, ...] | None = None
    free_agency: FreeAgencyResult | None = None
    draft: DraftResult | None = None
    draft_cycle: DraftCycleOutcome | None = None
    """The DRAFT branch's own outcome (Decision P5-98.h) -- where the draft
    stopped and why, which `draft` alone cannot say: a `None` `draft` means
    "nothing was drafted" for four different reasons, and Stage E's exit
    gate has to tell "the draft is finished" from "the draft is waiting on
    a person" from "the draft cannot continue at all". Named for the cycle
    rather than `draft_waiting_on`, which would name one of its four states
    and read as a boolean-ish field on the other three."""
    waivers: WaiverWindowResult | None = None


async def run_phase_hook(
    ex: Executor, *, league_id: int, phase: Phase, actor_user_id: int, agency_enabled: bool = True
) -> PhaseHookResult:
    """Runs whichever AI GM decision(s) belong to `phase`, applies them,
    and appends every `DecisionRecord` produced to this league/season's
    JSONL log (`artifact_path`).

    **`actor_user_id` is accepted but not read anywhere in this function's
    own body (critic round 19, nit 1) -- reserved, not dead.** This
    function's own docstring already establishes it never calls `phase_
    service.advance`, so there is no `phase_history`-style audit row
    (`league_repo.record_phase_transition`, which is where `phase/
    service.py`'s own `advance`/`force_advance` actually persist their
    `actor_user_id`) for this call to write into -- a phase hook run is
    not itself a phase transition. Nothing else this function touches has
    a sensible slot for "who triggered this run" either: `DecisionRecord`
    is a per-team AI GM decision, not a per-invocation admin action, and
    the JSONL envelope (`encode_decision_record`) is Family-1 invariant
    6's deterministic, wall-clock-free artifact -- an actor field there
    would be exactly the kind of caller-identity metadata that
    invariant is designed to exclude. Kept as an explicit parameter
    anyway, for whichever later slice adds a commissioner-triggered-hook
    audit trail alongside `phase_history` (this function's own docstring
    already documents phase hooks as commissioner-triggered, Phase 5) --
    not forced into a place it does not yet belong.

    **This function never calls `phase_service.advance`.** It runs AI GM
    decisions WITHIN the league's current phase; moving the league to its
    NEXT phase is a separate, later, commissioner-triggered action
    (Phase 5), not this function's job -- however "phase hook" might read
    at a glance. `phase` must equal `league.current_phase` (checked via
    `phase_service.require_phase`, raising `PhaseError` otherwise) --
    this function runs decisions FOR the phase the league is actually in,
    never for a phase it merely might be about to enter.

    Dispatch, per §12 item 10's own hook list: window classification at
    `PRESEASON`/`TRADE_DEADLINE`/`DRAFT_LOTTERY`; trade windows at
    `REGULAR_SEASON` and `TRADE_DEADLINE`; re-sign/free-agency offers at
    `RESIGN_WINDOW` and `FREE_AGENCY`; draft execution at `DRAFT`; waiver
    cuts at `WAIVERS` (T7a2, architect round 8 §7.3 -- not part of §12
    item 10's original hook list, added to close the `FREE_AGENCY ->
    WAIVERS -> PRESEASON` graph edge `phase/graph.py:38-39` already
    declares legal but that nothing handled). Every other `Phase` value
    (`PLAYIN`/`PLAYOFFS_R1`/etc. -- Phase-1-only concerns with no AI-GM-
    decision meaning) returns an empty `PhaseHookResult` and writes
    nothing -- a no-op boundary, not an error.

    **`RESIGN_WINDOW`'s own branch has no DB write and appends nothing to
    the JSONL log -- a real, considered judgment call, not an oversight.**
    `run_resign_window` (T3) is documented as producing pending offers
    only, no execution, and it discards the `DecisionRecord` it builds
    internally (`run_free_agency`'s own docstring states this explicitly:
    "`run_resign_window`'s own return type discards the record it
    built"). There is therefore nothing this branch could execute and
    nothing it could log even if it wanted to -- the actual re-signing
    happens only through `FREE_AGENCY`'s own round-1 merge, which
    recomputes fresh, record-carrying offers for the same expiring
    players rather than reusing anything `RESIGN_WINDOW`'s own hook call
    produced (T3's own nit, `run_free_agency`'s docstring: "not byte-for-
    byte with `run_resign_window`'s own offer"). `RESIGN_WINDOW`'s hook
    is kept as its own branch anyway (rather than folded away) because it
    still returns a real, inspectable `resign_offers` preview in
    `PhaseHookResult` -- useful to a caller even though nothing persists.

    **At `TRADE_DEADLINE`, window classification and the trade window each
    call `load_gm_inputs` separately (not shared, for the snapshot/room/
    philosophy facts each branch needs its own copy of), but the trade
    branch's `ctx.window_by_team` is then overwritten with the SAME call's
    own freshly-classified `window_pass.window_by_team` -- never left to
    the second `load_gm_inputs` call's own reloaded-from-DB value (critic
    round 19 fix; this paragraph previously, incorrectly, claimed the
    reload alone already achieved this).** The reload alone cannot: `apply_
    windows` persists via `window.to_timeline`, which collapses `Compet
    itiveWindow.CONTEND` into the single stored `Timeline.WIN_NOW` value
    (Decision P2-13's lossy three-valued map), and `_TIMELINE_TO_WINDOW`'s
    own reverse map -- by construction, `Timeline` has only three values --
    can never produce `CONTEND` back out of that column. A team classified
    `CONTEND` by the first branch would therefore have been silently
    downgraded to `WIN_NOW` for the second branch's own valuation in EVERY
    case, not merely lagged "one cycle behind" (critic's own investigation:
    the originally assumed characterization was too mild -- this was a
    permanent, unconditional loss, not a hysteresis delay). Fixed by
    threading `window_pass.window_by_team` (the in-memory classification
    result, never round-tripped through `Timeline`) into the trade branch's
    `ctx` via `dataclasses.replace` whenever `window_pass` was already
    computed earlier in this same call -- `REGULAR_SEASON`'s own trade
    branch is unaffected (`window_pass` is `None` there, since no
    classification runs at that phase, so its `ctx.window_by_team` keeps
    reading whatever `teams.timeline` actually holds, same as before this
    fix). This still costs a second full snapshot load within the same
    hook call (~2x this phase's own I/O versus the other phases; left as
    is rather than threading one shared, partially-patched snapshot
    through both branches, which would add real complexity for a single-
    phase-per-season cost) -- only `window_by_team` is overridden, not the
    rest of `ctx` or `snapshot`.

    **The trade branch also persists this window's AI-initiated offers to
    human-managed teams (Decision P5-59, docs/design/phase-5-discord-
    presentation.md §28.4).** `run_trade_window`'s own `TradeWindowResult.
    offers` (Decision P5-57) is written as real `trade_offers` rows in a
    transaction of its own, separate from the `apply_trade` transaction
    immediately above it -- an offer is not part of any executed trade,
    and a failure recording offers must not roll back trades that really
    happened. Every row is `proposed_by = NULL`, `status = TradeStatus.
    PROPOSED`. Empty, and this second transaction never opens, whenever
    `ctx.self_managed_team_ids` is empty -- every league this project has
    ever generated.

    **`agency_enabled` (critic B1 fix) is this function's own real kill
    switch for Stage C's agency consequences -- production's counterpart
    to `tools/gm_harness/state.py::build_ctx`'s identically-named
    parameter, not merely a docstring claim about one.** Before this fix,
    `GmContext.motivation_by_player`'s and `.trade_request_pressure_by_
    player`'s own docstrings asserted both were "gated on `agency_enabled`
    exactly like `head_to_head_enabled` gates `head_to_head`," but no such
    parameter existed anywhere in this function's signature -- the `FREE_
    AGENCY` and `REGULAR_SEASON`/`TRADE_DEADLINE` branches below called
    `load_motivation_for_league`/`load_trade_request_pressure_for_league`
    unconditionally, so production had no way to turn agency off. Defaults
    `True` so every existing caller (none exist yet outside tests --
    `run_phase_hook` is not wired to any route as of Stage C, reserved for
    Phase 5's commissioner-triggered hook) keeps today's behavior; a
    caller that passes `False` gets both fields left at `GmContext`'s own
    `None` default, which every downstream reader (`rank_offers`,
    `propose_packages`' target ranking) already treats as "no agency
    knowledge, behave exactly as before" by construction -- no additional
    branching needed at the consumer side.

    **RMLP Decision 2 (build step 7) -- the trade/FREE_AGENCY/WAIVERS
    branches each hold the EXCLUSIVE tier-0 gate across their entire
    read-decide-write span, not just their writes.** While one of these
    branches is running, `/waive`, `/release`, and `/sign-free-agent` for
    this league are unavailable -- not merely for the branch's writes, but
    for the whole span including `load_gm_inputs` and the decision
    engines. A human command issued in that window waits up to its 3s
    `lock_timeout` and then fails with a clean `ConcurrencyError` ("this
    league is running its AI pass"). This is a deliberate, disclosed
    tradeoff, not an oversight: it makes "a human command lands mid-
    computation and stales the AI's decision" unreachable BY
    CONSTRUCTION (every participant takes the shared gate as the first
    statement of its own transaction, and an exclusive grant is only
    given when no shared or exclusive holder exists, so the instant the
    grant returns, no participant transaction can be in flight) rather
    than merely less likely. Phase hooks are commissioner-triggered, not
    continuous, so the window is bounded and rare; the 3s timeout is
    deliberately not raised to "wait it out" -- a quick, honest rejection
    is the pool-healthy behavior (F7's pool-starvation math). See
    `dba2/services/roster_lock.py`'s module docstring for the full
    mutual-exclusion argument this depends on."""
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    require_phase(league.current_phase, phase)
    season = league.current_season

    window_pass: WindowPassResult | None = None
    trade_window_result: TradeWindowResult | None = None
    resign_offers: tuple[SigningOffer, ...] | None = None
    free_agency_result: FreeAgencyResult | None = None
    draft_result: DraftResult | None = None
    draft_cycle: DraftCycleOutcome | None = None
    waiver_result: WaiverWindowResult | None = None
    seq = 0

    if phase in (Phase.PRESEASON, Phase.TRADE_DEADLINE, Phase.DRAFT_LOTTERY):
        snapshot, ctx = await load_gm_inputs(ex, league_id=league_id, season=season)
        window_pass = classify_all_windows(
            snapshot,
            previous_by_team=ctx.window_by_team,
            philosophy_by_team=ctx.philosophy_by_team,
            tuning=ctx.tuning,
            # Decision P5-67 -- a pinned team's classification is carried
            # through from its own stored value, never reclassified.
            pinned_team_ids=ctx.manual_timeline_team_ids,
        )
        await apply_windows(ex, window_pass)
        seq = _write_decision_records(
            window_pass.records, league_id=league_id, season=season, phase=phase, seq_start=seq
        )

    if phase in (Phase.REGULAR_SEASON, Phase.TRADE_DEADLINE):
        # RMLP Decision 2 (build step 7) -- the gate + transaction boundary
        # moves UP to enclose decision computation, not just the writes.
        # Holding the exclusive gate across the whole read-decide-write
        # span makes "a human command lands mid-computation and makes this
        # branch's decision stale" unreachable BY CONSTRUCTION, not merely
        # less likely -- see run_phase_hook's own docstring (this function,
        # below) for the full argument. Every loader is passed `conn`, not
        # `ex` -- verified safe: none of load_gm_inputs/load_trade_request_
        # pressure_for_league open a transaction of their own.
        async with transaction(ex) as conn:
            trade_gate = await roster_lock.open_league_gate(
                conn, league_id=league_id, mode=roster_lock.LockMode.EXCLUSIVE
            )
            snapshot, ctx = await load_gm_inputs(
                conn, league_id=league_id, season=trade_gate.season
            )
            if window_pass is not None:
                # CONTEND fix (critic round 19) -- `ctx.window_by_team`
                # above was just reloaded from `teams.timeline`, which can
                # never hold `CompetitiveWindow.CONTEND` (see this
                # function's own docstring for the lossy-persistence
                # mechanism). Overwrite it with this SAME call's own
                # in-memory classification result, which was never
                # round-tripped through that column, so a CONTEND team's
                # trade valuation actually sees CONTEND.
                ctx = replace(ctx, window_by_team=window_pass.window_by_team)
            # Decision P4-17 (Phase 4 Stage C step 12): `run_trade_window`
            # is this field's own documented consumer (`propose_packages`'
            # target ranking) -- loaded here, not inside `load_gm_inputs`
            # itself, for the identical reason `motivation_by_player` is
            # loaded only in the `FREE_AGENCY` branch above: every other
            # phase reads `ctx` too and none of the rest need this query.
            # Gated on `agency_enabled` (critic B1 fix) -- `False` skips
            # the query entirely and leaves `ctx.trade_request_pressure_
            # by_player` at `GmContext`'s own `None` default.
            if agency_enabled:
                trade_request_pressure_by_player = await load_trade_request_pressure_for_league(
                    conn, league_id=league_id
                )
                ctx = replace(
                    ctx, trade_request_pressure_by_player=trade_request_pressure_by_player
                )
            trade_window_result = run_trade_window(snapshot, ctx)
            for execution in trade_window_result.executions:
                await apply_trade(conn, execution, trade_gate.season, phase, league_id=league_id)
        seq = _write_decision_records(
            # Round-7 critic fix (blocking issue 1) -- `records` no longer
            # carries a diverted candidate's own proposer-side record
            # (that would break its `(record_a, record_b)`-pair-per-turn
            # contract); `offer_records` carries it instead, concatenated
            # here so P5-56's "the proposer's own record IS logged" still
            # holds against the durable decision-log artifact -- one
            # continuous `seq` across both, same as every other branch
            # below that writes more than one record source.
            trade_window_result.records + trade_window_result.offer_records,
            league_id=league_id, season=trade_gate.season, phase=phase, seq_start=seq,
        )
        # Decision P5-59 -- one transaction for all of this window's
        # AI-initiated offers (never zero-vs-partial: either every offer
        # this window produced lands, or none does), separate from the
        # `apply_trade` transaction above -- an offer is not part of any
        # executed trade, and a failure recording offers must not roll
        # back trades that really happened. `proposed_by=None` (P5-35's
        # already-reserved shape), `status=TradeStatus.PROPOSED`. Empty
        # whenever `ctx.self_managed_team_ids` is empty (every league
        # this project has ever generated), so this loop runs zero times
        # and opens no transaction in that case. Deliberately NOT under
        # the exclusive gate above (D2-1, disclosed) -- an offer is not a
        # roster mutation.
        if trade_window_result.offers:
            async with transaction(ex) as conn:
                for offer in trade_window_result.offers:
                    await offer_repo.create_trade_offer(
                        conn,
                        league_id=league_id,
                        season=trade_gate.season,
                        phase=phase,
                        from_team_id=offer.from_team_id,
                        to_team_id=offer.to_team_id,
                        proposed_by=None,
                        status=TradeStatus.PROPOSED,
                        players_from_to=tuple(rp.id for rp in offer.package.outgoing_a.players),
                        players_to_from=tuple(rp.id for rp in offer.package.incoming_a.players),
                        picks_from_to=tuple(p.id for p in offer.package.outgoing_a.picks),
                        picks_to_from=tuple(p.id for p in offer.package.incoming_a.picks),
                    )

    if phase is Phase.RESIGN_WINDOW:
        snapshot, ctx = await load_gm_inputs(ex, league_id=league_id, season=season)
        expiring_by_team = _expiring_by_team(snapshot, season)
        resign_offers = run_resign_window(snapshot, ctx, expiring_by_team=expiring_by_team)

    if phase is Phase.FREE_AGENCY:
        # RMLP Decision 2 (build step 7) -- same restructuring as the
        # trade branch above: gate + transaction boundary moved up to
        # enclose decision computation.
        async with transaction(ex) as conn:
            fa_gate = await roster_lock.open_league_gate(
                conn, league_id=league_id, mode=roster_lock.LockMode.EXCLUSIVE
            )
            snapshot, ctx = await load_gm_inputs(conn, league_id=league_id, season=fa_gate.season)
            # Decision P4-16: `load_gm_inputs` itself deliberately never
            # populates `motivation_by_player` (every other phase reads
            # `ctx` too, and none of them need it -- see `GmContext.
            # motivation_by_player`'s own docstring) -- a phase-specific
            # loader, called only here, exactly like `_load_free_agency_
            # pool` immediately below. `replace(...)` mirrors the
            # identical `window_by_team` override earlier in this same
            # function. Gated on `agency_enabled` (critic B1 fix) --
            # `False` skips the query entirely and leaves `ctx.
            # motivation_by_player` at `GmContext`'s own `None` default.
            if agency_enabled:
                motivation_by_player = await load_motivation_for_league(conn, league_id=league_id)
                ctx = replace(ctx, motivation_by_player=motivation_by_player)
            expiring_by_team = _expiring_by_team(snapshot, fa_gate.season)
            pending_offers = run_resign_window(snapshot, ctx, expiring_by_team=expiring_by_team)
            pool = await _load_free_agency_pool(conn, league_id, snapshot, expiring_by_team)
            free_agency_result = run_free_agency(
                snapshot, ctx, pool=pool, pending_offers=pending_offers
            )
            for signing in free_agency_result.signings:
                await apply_signing(conn, signing, league_id=league_id)
        seq = _write_decision_records(
            free_agency_result.records,
            league_id=league_id, season=fa_gate.season, phase=phase, seq_start=seq,
        )

    if phase is Phase.DRAFT:
        # Stage D step 22 (Decision P4-113/P4-114) extracted the branch
        # body into `execute_draft`; Decision P5-98.h extracts the branch
        # itself -- `load_gm_inputs` plus that call plus the `drafts` row
        # lock around both -- into `run_draft_cycle`, because `/draft`'s
        # post-pick resume has to run the identical sequence and a second
        # copy of it is how the two surfaces start disagreeing about what
        # a finished draft is.
        #
        # **The decision-log write stays HERE, at the hook's own level.**
        # That is where it already was, which makes it the stronger
        # behaviour-preservation claim, and it keeps a blocking file write
        # out of `/draft`'s open transaction: the cycle returns its
        # records and its caller writes them, with its own running `seq`
        # and no `seq_start` threaded into the extraction.
        draft_cycle = await run_draft_cycle(ex, league_id=league_id, season=season)
        draft_result = draft_cycle.draft
        if draft_result is not None:
            seq = _write_decision_records(
                draft_cycle.records,
                league_id=league_id, season=season, phase=phase, seq_start=seq,
            )

    if phase is Phase.WAIVERS:
        # RMLP Decision 2 (build step 7) -- same restructuring as the
        # trade/FREE_AGENCY branches above.
        async with transaction(ex) as conn:
            waivers_gate = await roster_lock.open_league_gate(
                conn, league_id=league_id, mode=roster_lock.LockMode.EXCLUSIVE
            )
            snapshot, ctx = await load_gm_inputs(
                conn, league_id=league_id, season=waivers_gate.season
            )
            waiver_result = run_waivers(snapshot, ctx)
            for execution in waiver_result.executions:
                await apply_waivers(conn, execution, league_id=league_id)
        seq = _write_decision_records(
            waiver_result.records,
            league_id=league_id, season=waivers_gate.season, phase=phase, seq_start=seq,
        )

    return PhaseHookResult(
        league_id=league_id,
        season=season,
        phase=phase,
        window_pass=window_pass,
        trade_window=trade_window_result,
        resign_offers=resign_offers,
        free_agency=free_agency_result,
        draft=draft_result,
        draft_cycle=draft_cycle,
        waivers=waiver_result,
    )
