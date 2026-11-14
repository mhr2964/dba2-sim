"""leagues, league_cap_settings, phase state, phase_history — `league_repo`'s
tables per §5's module map.

phase-0-foundation.md §4.1, §5, §6.2.

Owns the two `phase_history`/`leagues.current_phase` writes that
`phase/service.py::_write_transition` took as a documented pre-approved
shortcut before this module existed (see that module's docstring) —
`record_phase_transition` and `set_current_phase` below are exactly the two
functions its docstring named. `get_phase_state_for_update` also moves here
per the module map's "phase state reads/writes" ownership, even though the
prompt's relocation note only named the two `execute()` calls: leaving the
`SELECT ... FOR UPDATE` as inline SQL in `phase/service.py` while every other
`leagues`/`phase_history` statement goes through this module would split
ownership of the same table across two files for no reason.

Enum-from-TEXT-column gotcha (`current_phase`, applies to every module that
hits a TEXT-backed enum column): no DB CHECK constrains these columns, so a
bad stored value raises a bare `ValueError` from `Phase(...)`-style
construction. Every repository module in this package lets that raise
uncaught rather than wrapping it — consistent with `phase/service.py`'s own
pre-existing `Phase(row["current_phase"])` call, which this module's
docstring is itself relocating logic next to.

`phase_data` is JSONB with no custom codec registered on the pool
(`data/db.py` doesn't call `conn.set_type_codec` for json/jsonb), so asyncpg
sends/receives it as a plain `str` — this module marshals it to/from `dict`
at the repo boundary via `json.dumps`/`json.loads` so nothing above `data/`
has to know that.
"""
from __future__ import annotations

import json
from collections.abc import Collection
from datetime import datetime

import asyncpg

from dba2.data.db import Executor
from dba2.domain.league import (
    AdvanceClaimSnapshot,
    League,
    LeagueCapSettings,
    LeagueClaimState,
    PhaseHistory,
)
from dba2.domain.phase import Phase

_LEAGUE_COLUMNS = """
    id, discord_guild_id, name, start_season_year, current_season,
    current_phase, phase_entered_at, phase_data, commissioner_user_id,
    rng_seed, created_at, archived_at, advance_claimed_at,
    advance_claimed_by, hook_completed_cycle_index, entry_work_completed_index,
    closing_work_completed_index
"""

_CAP_SETTINGS_COLUMNS = """
    league_id, season, salary_cap, luxury_tax, apron_1, apron_2,
    max_pct_0_6, max_pct_7_9, max_pct_10_plus, supermax_pct, min_salary,
    mle_amount, bi_annual_amount, raise_pct_bird, raise_pct_non_bird,
    roster_min, roster_max
"""

_PHASE_HISTORY_COLUMNS = """
    id, league_id, season, from_phase, to_phase, occurred_at,
    actor_user_id, forced, reason
"""


def _map_league(r: asyncpg.Record) -> League:
    return League(
        id=r["id"],
        discord_guild_id=r["discord_guild_id"],
        name=r["name"],
        start_season_year=r["start_season_year"],
        current_season=r["current_season"],
        current_phase=Phase(r["current_phase"]),
        phase_entered_at=r["phase_entered_at"],
        phase_data=json.loads(r["phase_data"]),
        commissioner_user_id=r["commissioner_user_id"],
        rng_seed=r["rng_seed"],
        created_at=r["created_at"],
        archived_at=r["archived_at"],
        advance_claimed_at=r["advance_claimed_at"],
        advance_claimed_by=r["advance_claimed_by"],
        hook_completed_cycle_index=r["hook_completed_cycle_index"],
        entry_work_completed_index=r["entry_work_completed_index"],
        closing_work_completed_index=r["closing_work_completed_index"],
    )


def _map_cap_settings(r: asyncpg.Record) -> LeagueCapSettings:
    return LeagueCapSettings(
        league_id=r["league_id"],
        season=r["season"],
        salary_cap=r["salary_cap"],
        luxury_tax=r["luxury_tax"],
        apron_1=r["apron_1"],
        apron_2=r["apron_2"],
        max_pct_0_6=r["max_pct_0_6"],
        max_pct_7_9=r["max_pct_7_9"],
        max_pct_10_plus=r["max_pct_10_plus"],
        supermax_pct=r["supermax_pct"],
        min_salary=r["min_salary"],
        mle_amount=r["mle_amount"],
        bi_annual_amount=r["bi_annual_amount"],
        raise_pct_bird=r["raise_pct_bird"],
        raise_pct_non_bird=r["raise_pct_non_bird"],
        roster_min=r["roster_min"],
        roster_max=r["roster_max"],
    )


def _map_phase_history(r: asyncpg.Record) -> PhaseHistory:
    return PhaseHistory(
        id=r["id"],
        league_id=r["league_id"],
        season=r["season"],
        from_phase=Phase(r["from_phase"]) if r["from_phase"] is not None else None,
        to_phase=Phase(r["to_phase"]),
        occurred_at=r["occurred_at"],
        actor_user_id=r["actor_user_id"],
        forced=r["forced"],
        reason=r["reason"],
    )


async def create_league(
    ex: Executor,
    *,
    discord_guild_id: int,
    name: str,
    start_season_year: int,
    current_season: int,
    commissioner_user_id: int,
    rng_seed: int,
    current_phase: Phase = Phase.SETUP,
    phase_data: dict | None = None,
) -> League:
    row = await ex.fetchrow(
        f"""
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             current_phase, phase_data, commissioner_user_id, rng_seed)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING {_LEAGUE_COLUMNS}
        """,
        discord_guild_id,
        name,
        start_season_year,
        current_season,
        current_phase.value,
        json.dumps(phase_data if phase_data is not None else {}),
        commissioner_user_id,
        rng_seed,
    )
    return _map_league(row)


async def get_league(ex: Executor, league_id: int) -> League | None:
    row = await ex.fetchrow(
        f"SELECT {_LEAGUE_COLUMNS} FROM leagues WHERE id = $1", league_id
    )
    return _map_league(row) if row is not None else None


async def get_league_by_guild_id(ex: Executor, discord_guild_id: int) -> League | None:
    row = await ex.fetchrow(
        f"SELECT {_LEAGUE_COLUMNS} FROM leagues WHERE discord_guild_id = $1",
        discord_guild_id,
    )
    return _map_league(row) if row is not None else None


async def archive_league(ex: Executor, league_id: int) -> None:
    await ex.execute(
        "UPDATE leagues SET archived_at = NOW() WHERE id = $1", league_id
    )


async def get_phase_state_for_update(
    ex: Executor, league_id: int
) -> tuple[Phase, int] | None:
    """`SELECT current_phase, current_season ... FOR UPDATE` — the row lock
    that serializes `phase/service.py`'s read-check-write critical section
    (its own concurrency regression tests depend on this exact locking
    behavior; do not drop `FOR UPDATE`). Must be called with a `Connection`
    already inside an open `transaction()` — same requirement as every
    `FOR UPDATE` read.
    """
    row = await ex.fetchrow(
        "SELECT current_phase, current_season FROM leagues WHERE id = $1 FOR UPDATE",
        league_id,
    )
    if row is None:
        return None
    return Phase(row["current_phase"]), row["current_season"]


async def get_league_state_for_claim_check(
    ex: Executor, league_id: int
) -> LeagueClaimState | None:
    """`SELECT ... FROM leagues WHERE id = $1 FOR SHARE` -- the one read the
    trade-offer review surface takes its league-level guard from
    (docs/design/phase-5-trade-offer-review-surface.md §3, §6.2): the phase
    pointer, Stage E's advance claim, and `commissioner_user_id` (the input a
    permission check needs, so it comes from the SAME locked read rather than a
    second unlocked one).

    **`FOR SHARE`, not `FOR UPDATE`.** `FOR SHARE` conflicts with both `FOR
    UPDATE` and `FOR NO KEY UPDATE`, so it is mutually exclusive with
    `phase/service.advance`'s `get_phase_state_for_update` and with a press's
    `UPDATE leagues SET advance_claimed_at`: either this reader holds the
    lock and the writer waits, or the writer commits first and this reader
    sees the new state. Either way there is no third ordering (P5-90 rule 2).
    But two `FOR SHARE` readers do not block each other, so two offer
    resolutions on disjoint teams still run concurrently.

    A NEW function rather than a change to `get_phase_state_for_update`,
    whose docstring says its exact locking behaviour has regression tests that
    depend on it and which returns only `(phase, season)`. Must be called with
    a `Connection` already inside an open `transaction()`; the lock is held
    to commit."""
    row = await ex.fetchrow(
        """
        SELECT current_phase, current_season, advance_claimed_at, advance_claimed_by,
               commissioner_user_id
        FROM leagues WHERE id = $1 FOR SHARE
        """,
        league_id,
    )
    if row is None:
        return None
    return LeagueClaimState(
        current_phase=Phase(row["current_phase"]),
        current_season=row["current_season"],
        advance_claimed_at=row["advance_claimed_at"],
        advance_claimed_by=row["advance_claimed_by"],
        commissioner_user_id=row["commissioner_user_id"],
    )


async def get_advance_claim_for_update(ex: Executor, league_id: int) -> AdvanceClaimSnapshot | None:
    """`/advance-unlock`'s read: the claim, its age at the database's clock, the pointer
    and the hook checkpoint, `FOR UPDATE`.

    **`FOR UPDATE`, not `FOR SHARE`:** the caller reads, decides and then clears the claim
    in one transaction, and two concurrent unlocks that each took `FOR SHARE` and then
    wanted to write would deadlock on the upgrade. `FOR UPDATE` also serialises the read
    against a press's own `UPDATE leagues SET advance_claimed_at` and against
    `release_advance`, so the age it reports is the age of the claim it then clears.
    Must be called with a `Connection` already inside an open `transaction()` that set
    `lock_timeout` first; the lock is held to commit. `claim_age` is `NOW() -
    advance_claimed_at` computed in SQL (`NULL` with no claim)."""
    row = await ex.fetchrow(
        """
        SELECT current_phase, current_season, advance_claimed_at, advance_claimed_by,
               NOW() - advance_claimed_at AS claim_age, hook_completed_cycle_index
        FROM leagues WHERE id = $1 FOR UPDATE
        """,
        league_id,
    )
    if row is None:
        return None
    return AdvanceClaimSnapshot(
        current_phase=Phase(row["current_phase"]),
        current_season=row["current_season"],
        advance_claimed_at=row["advance_claimed_at"],
        advance_claimed_by=row["advance_claimed_by"],
        claim_age=row["claim_age"],
        hook_completed_cycle_index=row["hook_completed_cycle_index"],
    )


async def claim_advance(ex: Executor, league_id: int, claimed_by: int) -> datetime | None:
    """Takes Stage E's advance claim: sets `advance_claimed_at`/`advance_claimed_by`
    only where no claim is held, and returns the timestamp it wrote -- the value
    `release_advance` must present to release this exact claim. `None` means the
    claim was already held (or the league does not exist); the caller tells the two
    apart with a read.

    The `advance_claimed_at IS NULL` predicate is the whole mutual exclusion between
    two presses -- two `/advance` presses a second apart race on this one statement,
    and Postgres lets exactly one through. It takes the row lock, so it waits behind
    any `FOR SHARE` a human write holds: the caller must have set `lock_timeout`
    first. Never called from `bot/` (P5-99.m.1).
    """
    row = await ex.fetchrow(
        "UPDATE leagues SET advance_claimed_at = NOW(), advance_claimed_by = $2 "
        "WHERE id = $1 AND advance_claimed_at IS NULL RETURNING advance_claimed_at",
        league_id,
        claimed_by,
    )
    return None if row is None else row["advance_claimed_at"]


async def release_advance(
    ex: Executor, league_id: int, claimed_by: int, claimed_at: datetime
) -> bool:
    """Clears the advance claim ONLY if it is still the one `(claimed_by,
    claimed_at)` describes, and reports whether it did.

    **The timestamp is the fence, not decoration.** `/advance-unlock` can break a
    claim and another press can then take a new one; a release matching on the
    claimant alone would let the first press clear the second's live claim, and a
    third press could then start mid-block. Matching the exact `advance_claimed_at`
    the claim returned makes a stale release a zero-row no-op.
    """
    row = await ex.fetchrow(
        "UPDATE leagues SET advance_claimed_at = NULL, advance_claimed_by = NULL "
        "WHERE id = $1 AND advance_claimed_by = $2 AND advance_claimed_at = $3 "
        "RETURNING id",
        league_id,
        claimed_by,
        claimed_at,
    )
    return row is not None


async def set_hook_completed_cycle_index(ex: Executor, league_id: int, cycle_ordinal: int) -> bool:
    """Records that the hook of cycle `cycle_ordinal` completed, and reports
    whether the column moved.

    **Monotone by predicate:** `hook_completed_cycle_index < $2` makes a second
    press (or a stale writer) unable to move it backwards, and makes writing the
    value it already holds a zero-row no-op. It takes the row lock, so the caller
    must be inside a transaction that set `lock_timeout` first (the module-scoped
    discipline check holds `season_service` to that). Never called from `bot/`
    (P5-99.m.1).
    """
    row = await ex.fetchrow(
        "UPDATE leagues SET hook_completed_cycle_index = $2 "
        "WHERE id = $1 AND hook_completed_cycle_index < $2 RETURNING id",
        league_id,
        cycle_ordinal,
    )
    return row is not None


async def set_entry_work_completed_index(ex: Executor, league_id: int, entry_index: int) -> bool:
    """Records that the entry work of the phase entry `entry_index` completed
    (P5-99.r.1), and reports whether the column moved. Monotone by predicate and
    row-locking, exactly like `set_hook_completed_cycle_index` -- and keyed by
    `entry_index`, NOT the cycle ordinal that column takes."""
    row = await ex.fetchrow(
        "UPDATE leagues SET entry_work_completed_index = $2 "
        "WHERE id = $1 AND entry_work_completed_index < $2 RETURNING id",
        league_id,
        entry_index,
    )
    return row is not None


async def set_closing_work_completed_index(ex: Executor, league_id: int, entry_index: int) -> bool:
    """Records that the closing work of the phase entry `entry_index` completed
    (P5-99.r.1), and reports whether the column moved. Monotone by predicate and
    row-locking, keyed by `entry_index`."""
    row = await ex.fetchrow(
        "UPDATE leagues SET closing_work_completed_index = $2 "
        "WHERE id = $1 AND closing_work_completed_index < $2 RETURNING id",
        league_id,
        entry_index,
    )
    return row is not None


async def set_current_season(ex: Executor, league_id: int, season: int) -> None:
    """The season-boundary counterpart to `set_current_phase` -- callers
    doing a real season advance should read the prior value via
    `get_phase_state_for_update`'s row lock first, inside the same
    transaction, to avoid two concurrent advances racing on the same
    league."""
    await ex.execute(
        "UPDATE leagues SET current_season = $2 WHERE id = $1", league_id, season
    )


async def set_current_phase(ex: Executor, league_id: int, to_phase: Phase) -> None:
    await ex.execute(
        "UPDATE leagues SET current_phase = $2, phase_entered_at = NOW() WHERE id = $1",
        league_id,
        to_phase.value,
    )


async def record_phase_transition(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    from_phase: Phase | None,
    to_phase: Phase,
    actor_user_id: int,
    forced: bool,
    reason: str | None,
) -> None:
    await ex.execute(
        """
        INSERT INTO phase_history
            (league_id, season, from_phase, to_phase, actor_user_id, forced, reason)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        league_id,
        season,
        from_phase.value if from_phase is not None else None,
        to_phase.value,
        actor_user_id,
        forced,
        reason,
    )


async def list_phase_history(ex: Executor, league_id: int) -> list[PhaseHistory]:
    rows = await ex.fetch(
        f"""
        SELECT {_PHASE_HISTORY_COLUMNS} FROM phase_history
        WHERE league_id = $1
        ORDER BY occurred_at
        """,
        league_id,
    )
    return [_map_phase_history(r) for r in rows]


async def count_phase_entries(
    ex: Executor,
    league_id: int,
    season: int,
    to_phases: Collection[Phase] | None = None,
) -> int:
    """How many `phase_history` rows this `(league, season)` has: one per phase
    entry, forced or not (P5-99.d.1). `to_phases` narrows the count to entries
    INTO those phases -- the cycle-bearing set gives the ordinal's count -- and
    an empty collection counts nothing, distinct from `None`, which counts all.

    One `COUNT(*)` on `ix_phase_history_league_season` (migration 013). It
    counts what has been written, so a caller that needs the count to agree with
    the pointer takes it inside the same transaction as
    `get_phase_state_for_update`'s row lock; `list_phase_history` cannot serve,
    it has no season filter."""
    if to_phases is None:
        return await ex.fetchval(
            "SELECT COUNT(*) FROM phase_history WHERE league_id = $1 AND season = $2",
            league_id,
            season,
        )
    return await ex.fetchval(
        """
        SELECT COUNT(*) FROM phase_history
        WHERE league_id = $1 AND season = $2 AND to_phase = ANY($3::text[])
        """,
        league_id,
        season,
        [p.value for p in to_phases],
    )


async def create_cap_settings(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    salary_cap: int,
    luxury_tax: int,
    apron_1: int,
    apron_2: int,
    max_pct_0_6: int,
    max_pct_7_9: int,
    max_pct_10_plus: int,
    supermax_pct: int,
    min_salary: int,
    mle_amount: int,
    bi_annual_amount: int,
    raise_pct_bird: int,
    raise_pct_non_bird: int,
    roster_min: int,
    roster_max: int,
) -> LeagueCapSettings:
    row = await ex.fetchrow(
        f"""
        INSERT INTO league_cap_settings
            (league_id, season, salary_cap, luxury_tax, apron_1, apron_2,
             max_pct_0_6, max_pct_7_9, max_pct_10_plus, supermax_pct,
             min_salary, mle_amount, bi_annual_amount, raise_pct_bird,
             raise_pct_non_bird, roster_min, roster_max)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
        RETURNING {_CAP_SETTINGS_COLUMNS}
        """,
        league_id,
        season,
        salary_cap,
        luxury_tax,
        apron_1,
        apron_2,
        max_pct_0_6,
        max_pct_7_9,
        max_pct_10_plus,
        supermax_pct,
        min_salary,
        mle_amount,
        bi_annual_amount,
        raise_pct_bird,
        raise_pct_non_bird,
        roster_min,
        roster_max,
    )
    return _map_cap_settings(row)


async def get_cap_settings(
    ex: Executor, league_id: int, season: int
) -> LeagueCapSettings | None:
    row = await ex.fetchrow(
        f"""
        SELECT {_CAP_SETTINGS_COLUMNS} FROM league_cap_settings
        WHERE league_id = $1 AND season = $2
        """,
        league_id,
        season,
    )
    return _map_cap_settings(row) if row is not None else None
