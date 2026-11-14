"""League, cap-settings, and phase-history dataclasses — `league_repo`'s
tables per §5's module map (leagues, league_cap_settings, phase state,
phase_history).

phase-0-foundation.md §4.1, §6.2.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from dba2.domain.phase import Phase


@dataclass
class League:
    """`advance_claimed_at`/`advance_claimed_by`/`hook_completed_cycle_
    index` (migration 013, docs/design/phase-5-discord-presentation.md
    §35.2, Decisions P5-72/P5-78/P5-90) are Stage E's orchestrator claim
    -- `advance_claimed_at`/`advance_claimed_by` gained their writer in
    Stage E build-plan commit 3 (`season_service._claim_league`) and are
    `NULL` on every league until an `/advance` press claims one;
    `hook_completed_cycle_index` gained its writer in commit 8
    (`season_service._record_hook_completed`, monotone, keyed by the cycle
    ordinal and written only after a cycle-bearing phase's hook) and starts at
    `-1` ("no cycle's hook has completed yet").

    `entry_work_completed_index` and `closing_work_completed_index` (migration
    020, phase-5-stage-e-advance.md P5-99.r.1) are the other two completion
    checkpoints, in the same shape and with the same `-1` start -- but **keyed by
    `entry_index`, not `cycle_ordinal`**, because phase work exists at phases
    that have no ordinal. Adjacent columns, different keys: never compare one
    against the other's number. Each advances only when its phase owns work of
    that kind (`season_service.work_outstanding`)."""

    id: int
    discord_guild_id: int
    name: str
    start_season_year: int
    current_season: int
    current_phase: Phase
    phase_entered_at: datetime
    phase_data: dict
    commissioner_user_id: int
    rng_seed: int
    created_at: datetime
    archived_at: datetime | None
    advance_claimed_at: datetime | None
    advance_claimed_by: int | None
    hook_completed_cycle_index: int
    entry_work_completed_index: int
    closing_work_completed_index: int


@dataclass(frozen=True)
class LeagueClaimState:
    """`league_repo.get_league_state_for_claim_check`'s return shape -- the
    slice of `leagues` a human-initiated write needs under the row's `FOR
    SHARE` lock: the phase pointer, Stage E's advance claim (`advance_claimed_
    at IS NOT NULL` means a press is running) and the commissioner id.
    Not table-backed (it is a narrowing of `League`, like `CapSummary` is a
    computed shape), so it has no entry in `test_schema_drift.py`'s registry."""

    current_phase: Phase
    current_season: int
    advance_claimed_at: datetime | None
    advance_claimed_by: int | None
    commissioner_user_id: int


@dataclass(frozen=True)
class AdvanceClaimSnapshot:
    """`league_repo.get_advance_claim_for_update`'s return shape: what
    `/advance-unlock` needs of the `leagues` row, read under its `FOR UPDATE`
    lock -- the claim, its age **measured at the database's own clock** (the
    claim's timestamp was written with `NOW()`, so its age is compared with
    `NOW()`, not with a Python clock that may disagree), where the pointer is, and
    what `hook_completed_cycle_index` says. `claim_age` is `None` when no claim is
    held. Not table-backed (a narrowing of `League`, like `LeagueClaimState`), so
    it has no entry in `test_schema_drift.py`'s registry."""

    current_phase: Phase
    current_season: int
    advance_claimed_at: datetime | None
    advance_claimed_by: int | None
    claim_age: timedelta | None
    hook_completed_cycle_index: int


@dataclass
class LeagueCapSettings:
    """One row per (league, season) — Decision 8: cap constants are
    per-season rows, not Python constants, so a contract signed in season N
    stays evaluable against season N's cap forever."""

    league_id: int
    season: int
    salary_cap: int
    luxury_tax: int
    apron_1: int
    apron_2: int
    max_pct_0_6: int
    max_pct_7_9: int
    max_pct_10_plus: int
    supermax_pct: int
    min_salary: int
    mle_amount: int
    bi_annual_amount: int
    raise_pct_bird: int
    raise_pct_non_bird: int
    roster_min: int
    roster_max: int


@dataclass
class CapSummary:
    """`cap_service.get_cap_summary`'s return shape -- §4.4's cap-summary
    formula broken out by term so a caller (or a test) can see which part
    of `used` a dollar came from, not just the total.

    `cap_holds` is always populated (the real total, regardless of
    `include_holds`) so a caller can see what holds *would* add; only
    `used`/`room` are gated by `include_holds`, per Decision 19."""

    team_id: int
    season: int
    salary_cap: int
    committed_salary: int
    dead_cap: int
    cap_holds: int
    include_holds: bool
    used: int
    room: int


@dataclass
class PhaseHistory:
    """§6.2 — the league timeline; also what makes a `force_advance` call
    auditable (`forced` + `reason`)."""

    id: int
    league_id: int
    season: int
    from_phase: Phase | None
    to_phase: Phase
    occurred_at: datetime
    actor_user_id: int
    forced: bool
    reason: str | None
