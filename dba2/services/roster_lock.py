"""The Roster Mutation Lock Protocol (RMLP) helper -- two entry points with
distinct responsibilities, plus the `@maps_lock_contention` decorator.

**One-sentence contract** (the design's own §3.0, revision 2 as amended by
Addendum A): every write that changes which team a player is on, or the
status/ownership/dead-cap consequences of a contract, runs inside one
transaction that first -- at its outermost level -- opens the league gate
(`open_league_gate`), then acquires through this module's single shared
helper (`acquire`), in canonical order, an exclusive-enough lock on every
team row and every player row the write will touch, then re-reads and
re-validates under those locks every fact its own statements depend on, and
only then writes.

**Why this module lives in `services/`, not `data/repositories/`.** It
composes `team_repo`/`player_repo`/`league_repo` and needs a service-level
error (`ConcurrencyError`) -- keeping it out of `data/repositories/`
preserves the per-repo "touches only its own table" scope-creep guard tests.
Permitted by `test_layering.py`'s `services -> data/domain/core/phase`
allowlist; no allowlist change needed.

**Idempotent nesting is a required property.** Postgres row locks and
advisory `_xact_` locks are per-transaction: re-acquiring a row or lock this
transaction already holds is effectively free and cannot self-deadlock.
That property is what lets each of the five RMLP mutation functions call
`acquire` for its OWN lock set without knowing whether an outer caller
already locked a superset -- it is the single assumption the whole "all five
call the same helper" design rests on. `open_league_gate` is deliberately
the ONE exception: a second call is always evidence of the nesting mistake
F9 describes (below), not the normal composition path, so it raises on
re-entry instead of being idempotent.

**The canonical acquisition order, with tier 3 (docs/design/phase-5-trade-
offer-review-surface.md §6.3).** Tier 0 the league gate, then the `leagues`
row (the offer surface's `FOR SHARE`, `league_repo.get_league_state_for_
claim_check`), tier 1 the team rows, tier 2 the player rows, and **tier 3 the
`trade_offers` rows -- always acquired after tiers 1 and 2, never before.**
`league_service.assign_manager` already takes `teams` then `trade_offers` in
one transaction; a CAS-first offer path (P5-77's original ordering) would be
the exact reverse and a real deadlock. The one path that touches a
`trade_offers` row holding no tier-1/tier-2 lock is `offer_service.veto`, and
it is safe only because it takes none. The edge that must never be created,
for Stage E to inherit: no transaction may hold the `leagues` row lock while
waiting for the tier-0 gate.

**The outermost-only rule for `open_league_gate`, and why (F9).** An
advisory lock first acquired inside a subtransaction (asyncpg's savepoint
nesting, which is what `data/db.py::transaction()` does whenever it is
handed an already-open `Connection`) is released when that subtransaction
aborts -- even though the OUTER transaction is still running and still
holds its tier-1/tier-2 row locks. `open_league_gate` must therefore be the
FIRST statement inside the outermost `async with transaction(...)` block of
an operation, and must never be called from a function that may itself be
called with an already-open transaction. This is what makes Decision 2's
whole "no participant can be in flight while an exclusive gate is held"
argument sound: "participant is in flight" only reliably means "participant
holds the shared gate" if the gate can never be silently released mid-flight
by an inner rollback. Enforced three ways: `open_league_gate` raises on
re-entry (self-check below), `acquire` raises if no gate is held at all, and
`tests/unit/test_roster_mutation_lock_discipline.py`'s check 6 statically
asserts every `open_league_gate` call site is a direct, non-nested child of
an entry point's outermost `transaction()` block.

**The advisory-lock namespace registry.** `RMLP_LOCK_NAMESPACE` below is
this project's ONE registered `classid` for two-key (`pg_advisory_xact_
lock(key1, key2)`) advisory locks. Documented here so a future advisory-lock
user picks a different `classid` rather than silently colliding with RMLP's
tier-0 gate.

**The 40P01 (deadlock) triage rule -- read this before treating any deadlock
log line as an alarm.**

- **Deadlock between two RMLP participants = a protocol bug. Alarm-worthy,
  investigate immediately.** It should be impossible: the exclusive gate
  prevents batch-vs-command cycles (Decision 2), and the canonical tier/id
  ascending order prevents command-vs-command cycles.
- **Deadlock between a participant and a non-participant = expected, rare,
  correctly handled.** RMLP governs its five participants; it does not and
  cannot govern the whole database. Log it, surface `ConcurrencyError`, move
  on.
- **Triage mechanism, concrete, not a judgment call.** `ConcurrencyError`'s
  log line (`maps_lock_contention`, or this module's own mapping in
  `open_league_gate`/`acquire`) records the caller's name, `league_id`, and
  the tier-1/tier-2 lock set. Postgres' own server-side deadlock `DETAIL`
  names both backends' current queries. If the counterpart query is an
  RMLP-shaped lock statement (`SELECT ... FROM teams|players WHERE id = $1
  FOR NO KEY UPDATE`) or an RMLP advisory-lock call, it is case 1 and a bug;
  otherwise it is case 2.
"""
from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

import asyncpg

from dba2.core.errors import ConcurrencyError, NotFoundError, TransactionError, ValidationError
from dba2.core.logging import get_logger
from dba2.data.repositories import league_repo, player_repo, team_repo
from dba2.domain.phase import Phase
from dba2.domain.player import Player
from dba2.domain.team import Team

log = get_logger(__name__)

# This project's one registered advisory-lock `classid` -- see the module
# docstring's "advisory-lock namespace registry" paragraph. Spells "RMLP" in
# ASCII (0x52='R', 0x4D='M', 0x4C='L', 0x50='P'), chosen only to be
# memorable; the value carries no other meaning and must fit Postgres'
# `int4` advisory-lock key type.
RMLP_LOCK_NAMESPACE = 0x524D4C50


class LockMode(Enum):
    """The tier-0 league gate's two modes (§3.1's tier table)."""

    SHARED = "shared"
    EXCLUSIVE = "exclusive"


SHARED_LOCK_TIMEOUT = "3s"
"""The shared tier's `lock_timeout`, under a public name because a
transaction that takes NO gate still has to bound its own waits and must
bound them the same way (Decision P5-98.g). There was no constant here
before: the value existed only as `_GATE_TIMEOUT_S[LockMode.SHARED]`, a
private dict entry, which is not something a second module can read without
reaching into a private name. Tests that shorten the timeout still
`monkeypatch.setitem` the dict, which is why the dict stays the thing
`open_league_gate` reads."""

_GATE_TIMEOUT_S: dict[LockMode, str] = {
    LockMode.SHARED: SHARED_LOCK_TIMEOUT,
    LockMode.EXCLUSIVE: "60s",
}

# Decision 1's two distinct messages -- the helper knows which tier it timed
# out on, and the two cases mean different things to a user (§3.5).
_GATE_SHARED_CONTENTION_MESSAGE = (
    "This league is running its AI pass right now -- try that again in a moment."
)
_GATE_EXCLUSIVE_CONTENTION_MESSAGE = (
    "This league's roster gate is still in use by another roster action -- "
    "try again in a moment."
)
_ENTITY_CONTENTION_MESSAGE = (
    "That team/player is being updated by another action right now -- try "
    "again in a moment."
)

LOCK_CONTENTION_EXCEPTIONS = (
    asyncpg.exceptions.LockNotAvailableError,
    asyncpg.exceptions.DeadlockDetectedError,
)
"""55P03 and 40P01 -- the two errors a bounded wait on a lock can end in.

**Public for the same reason `set_lock_timeout` is** (Decision P5-98.g): a
transaction that takes no gate maps its own lock's contention at the
statement, because the generic entity message this module raises names the
wrong noun for a non-entity row (OS-13). `gm_service.run_draft_cycle` is
the first such caller, for the `drafts` row. The tuple exists once so a
second site cannot map one sqlstate and miss the other."""


@dataclass(frozen=True)
class LeagueGate:
    """Returned by `open_league_gate`. Carries `phase`/`season` -- read
    AFTER the gate is held -- so callers never read either from a pre-gate
    query that could be stale by the time the gate grants (§3.4's "the
    batch's season must come from the LeagueGate" rule)."""

    league_id: int
    phase: Phase
    season: int
    mode: LockMode


@dataclass(frozen=True)
class LockSet:
    """Returned by `acquire`. Deliberately carries only the locked rows --
    never `phase`/`season` (those live on `LeagueGate`, the only object that
    ever read them under the gate) -- so "use the gate's season, never a
    separately-read one" is mechanically obvious at every call site."""

    teams_by_id: Mapping[int, Team]
    players_by_id: Mapping[int, Player]


async def _gate_is_held(conn: asyncpg.Connection, *, league_id: int) -> bool:
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_locks
                WHERE locktype = 'advisory'
                  AND pid = pg_backend_pid()
                  AND classid = $1
                  AND objid = $2
                  AND objsubid = 2
                  AND granted
            )
            """,
            RMLP_LOCK_NAMESPACE,
            league_id,
        )
    )


def _require_connection(conn: object, *, caller: str) -> None:
    if not isinstance(conn, asyncpg.Connection):
        raise TransactionError(
            f"roster_lock.{caller} requires an open Connection (never a Pool) -- "
            "matching data.db.transaction()'s own contract. Open a transaction() "
            "first and pass the yielded Connection down."
        )


async def set_lock_timeout(conn: asyncpg.Connection, timeout: str) -> None:
    """`SET LOCAL lock_timeout` for the caller's transaction -- lifted out of
    `open_league_gate` so the statement and its string exist once (Decision
    P5-98.g).

    **`open_league_gate` is no longer the only caller, and that is the whole
    reason this exists.** A transaction that takes no gate -- the draft's own
    `drafts` row lock is the first (it mutates no RMLP table, so a gate would
    be ceremony) -- would otherwise wait at the server default of
    wait-forever on the first blocking lock it takes. `acquire`'s own
    `current_setting('lock_timeout') == '0'` guard says the same thing from
    the other side: an unbounded wait inside a locked section is the failure,
    not the gate's absence.

    `SET LOCAL` is scoped to the enclosing transaction and reverts at commit,
    which is why a `Connection` (never a `Pool`) is required: on a pool the
    statement runs in its own implicit transaction and is gone before the
    next one."""
    _require_connection(conn, caller="set_lock_timeout")
    await conn.execute(f"SET LOCAL lock_timeout = '{timeout}'")


async def open_league_gate(
    conn: asyncpg.Connection, *, league_id: int, mode: LockMode
) -> LeagueGate:
    """Must be the FIRST statement inside the OUTERMOST `async with
    transaction(...)` block of an operation -- see this module's docstring
    (F9) for why. Never call this from a function that may itself be called
    with an already-open transaction; that is `acquire`'s job to serve
    instead.

    In order: (1) assert this backend does not already hold the gate for
    this league (a hit means `open_league_gate` was called twice, or from a
    nested level -- the F9 anti-pattern); (2) issue the ONE `SET LOCAL
    lock_timeout` for this whole transaction (Decision 3 -- `acquire` never
    touches it, only verifies it); (3) acquire the tier-0 advisory lock in
    the requested mode; (4) read the league's `current_phase`/
    `current_season` AFTER the gate is held.

    **Decision 3, corrected (P5-98.g): this is the only issuer of `SET LOCAL
    lock_timeout` FOR A GATED TRANSACTION, not in the codebase.** A
    transaction that takes no gate -- the draft's `drafts FOR UPDATE` path,
    which mutates no RMLP table -- sets its own through `set_lock_timeout`,
    the helper this function now calls. What stays true, and is what
    Decision 3 was always protecting, is that no transaction sets it twice:
    a gated transaction gets it here and nowhere else, and `acquire` still
    only verifies.
    """
    _require_connection(conn, caller="open_league_gate")

    if await _gate_is_held(conn, league_id=league_id):
        raise TransactionError(
            f"roster_lock.open_league_gate was called a second time for league "
            f"{league_id} on this backend (or called from a nested transaction() "
            "level after an outer call already opened it). The gate must be "
            "opened exactly once, as the first statement of the OUTERMOST "
            "transaction() block. Calling it twice, or from inside a savepoint, "
            "is the exact F9 anti-pattern this module's docstring describes -- "
            "an advisory lock first acquired inside a subtransaction is "
            "released on that subtransaction's rollback even while the outer "
            "transaction (and its tier-1/tier-2 locks) is still live."
        )

    timeout = _GATE_TIMEOUT_S[mode]
    lock_fn = (
        "pg_advisory_xact_lock" if mode is LockMode.EXCLUSIVE else "pg_advisory_xact_lock_shared"
    )
    contention_message = (
        _GATE_EXCLUSIVE_CONTENTION_MESSAGE
        if mode is LockMode.EXCLUSIVE
        else _GATE_SHARED_CONTENTION_MESSAGE
    )
    try:
        await set_lock_timeout(conn, timeout)
        await conn.execute(f"SELECT {lock_fn}($1, $2)", RMLP_LOCK_NAMESPACE, league_id)
    except LOCK_CONTENTION_EXCEPTIONS as exc:
        log.warning(
            "roster_lock.open_league_gate: gate contention for league_id=%s "
            "mode=%s sqlstate=%s",
            league_id,
            mode.value,
            getattr(exc, "sqlstate", None),
        )
        raise ConcurrencyError(contention_message) from exc

    league = await league_repo.get_league(conn, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    return LeagueGate(
        league_id=league_id, phase=league.current_phase, season=league.current_season, mode=mode
    )


async def acquire(
    conn: asyncpg.Connection, *, league_id: int, teams: Iterable[int], players: Iterable[int]
) -> LockSet:
    """The per-operation helper, called by all five RMLP mutation
    functions. Safe to call more than once per transaction for a superset
    of an already-locked set (idempotent nesting, see module docstring) --
    this is what lets `apply_signing` call `acquire` and then call
    `roster_service.sign`, which calls `acquire` again, with the second
    call a cheap no-op re-acquisition.

    In order: (1) assert the gate is held for `league_id` on this backend
    (raises `TransactionError` if not -- turns the documentary precondition
    into a runtime one); (2) assert `lock_timeout` is actually set (raises
    `TransactionError` if it reads `'0'`, the server default of wait-
    forever -- this helper never SETS it, only verifies, per Decision 3);
    (3) lock every team id, ascending, `FOR NO KEY UPDATE`; (4) lock every
    player id, ascending, `FOR NO KEY UPDATE`; (5) assert every locked row's
    `league_id` agrees with the passed `league_id` (Decision 4c) -- a
    programming-error guard, not a user-facing validation, but raised as
    `ValidationError` (already in `bot/cogs/roster.py`'s `_KNOWN_DOMAIN_
    ERRORS`) so the user sees an honest message while `log.error` makes it
    visible as a bug to the operator.

    **Callers outside the five RMLP mutators (OS-11).** `offer_service` (the
    trade-offer review surface, docs/design/phase-5-trade-offer-review-
    surface.md §5.4) is the first non-mutator caller: its shared lock
    prelude and `propose` take the tier-1/tier-2 locks THEMSELVES, before
    `apply_trade` is reached, rather than leaving it to `apply_trade`'s own
    `acquire`. That is deliberate and must not be "fixed" back into the
    applier: `apply_trade` runs inside a savepoint, a rollback to a
    savepoint releases every lock taken after it, and the offer service must
    still hold the locks its `EXPIRED` write's judgment rests on once
    `apply_trade` has failed. `apply_trade`'s own `acquire` then becomes the
    cheap no-op re-acquisition idempotent nesting exists for.
    """
    _require_connection(conn, caller="acquire")

    if not await _gate_is_held(conn, league_id=league_id):
        raise TransactionError(
            "roster_lock.acquire was called with no RMLP gate held for league "
            f"{league_id} on this backend -- call roster_lock.open_league_gate "
            "at your outermost transaction level before mutating roster state."
        )

    lock_timeout = await conn.fetchval("SELECT current_setting('lock_timeout')")
    if lock_timeout == "0":
        raise TransactionError(
            "roster_lock.acquire found lock_timeout unset (current_setting "
            "returned '0', the server default of wait-forever) on this "
            "transaction. acquire() never sets lock_timeout itself -- "
            "open_league_gate sets it for every gated transaction, per "
            "Decision 3 (a gate-less transaction sets its own through "
            "roster_lock.set_lock_timeout, but acquire() requires the gate "
            "anyway). Call open_league_gate at your outermost transaction "
            "level first."
        )

    team_ids = sorted(set(teams))
    player_ids = sorted(set(players))

    teams_by_id: dict[int, Team] = {}
    players_by_id: dict[int, Player] = {}
    try:
        for team_id in team_ids:
            row = await team_repo.lock_team_row(conn, team_id)
            if row is None:
                raise NotFoundError(f"No team {team_id}")
            teams_by_id[team_id] = row
        for player_id in player_ids:
            row = await player_repo.lock_player_row(conn, player_id)
            if row is None:
                raise NotFoundError(f"No player {player_id}")
            players_by_id[player_id] = row
    except LOCK_CONTENTION_EXCEPTIONS as exc:
        log.warning(
            "roster_lock.acquire: row-lock contention for league_id=%s "
            "teams=%s players=%s sqlstate=%s",
            league_id,
            team_ids,
            player_ids,
            getattr(exc, "sqlstate", None),
        )
        raise ConcurrencyError(_ENTITY_CONTENTION_MESSAGE) from exc

    for team_id, team in teams_by_id.items():
        if team.league_id != league_id:
            log.error(
                "roster_lock.acquire: league mismatch -- team %s belongs to "
                "league_id=%s, caller passed league_id=%s",
                team_id,
                team.league_id,
                league_id,
            )
            raise ValidationError(
                f"Team {team_id} belongs to league {team.league_id}, not {league_id}."
            )
    for player_id, player in players_by_id.items():
        if player.league_id != league_id:
            log.error(
                "roster_lock.acquire: league mismatch -- player %s belongs to "
                "league_id=%s, caller passed league_id=%s",
                player_id,
                player.league_id,
                league_id,
            )
            raise ValidationError(
                f"Player {player_id} belongs to league {player.league_id}, not {league_id}."
            )

    return LockSet(teams_by_id=teams_by_id, players_by_id=players_by_id)


def maps_lock_contention[F: Callable[..., Awaitable[object]]](func: F) -> F:
    """Addendum A3 -- applied to each of the five RMLP mutation functions
    (`roster_service.sign`/`waive`/`release`, `gm_service.apply_signing`/
    `apply_trade`) **and to `offer_service`'s four entry points (`propose`,
    `respond`, `counter`, `veto`), for nine callers in all** -- the
    statements those add (`leagues ... FOR SHARE`, the offer-row `FOR UPDATE`,
    the terminal status CAS) sit outside this module's own two mapping sites.
    One residue is disclosed rather than engineered away (OS-13): a lock
    timeout on the `leagues` row or a `trade_offers` row reports under
    `_ENTITY_CONTENTION_MESSAGE` ("That team/player is ..."), which names the
    wrong noun; the advice and the outcome are right. Tests therefore assert
    the exception TYPE for those cases, never the string.

    Wraps the ENTIRE call (hence the entire transaction the
    function opens) and catches `LockNotAvailableError` (55P03) and
    `DeadlockDetectedError` (40P01) from ANY statement in that transaction
    -- not only from this module's own `open_league_gate`/`acquire` calls.
    `contract_repo.terminate_contract`'s CAS `UPDATE ... WHERE status =
    'active'`, `create_dead_cap_charge`'s insert (contending on migration
    017b's unique index), and `create_contract`'s insert (017a) all sit
    outside `roster_lock` and would otherwise escape unmapped.

    An already-raised `ConcurrencyError` (from `open_league_gate`/`acquire`,
    which map their own contention with the correct tier-specific message)
    passes through UNTOUCHED -- this decorator only supplies the generic
    entity message for a RAW asyncpg error from a statement outside the
    helper, which is the correct message by construction (any row those
    statements contend on is one this mutation is writing).

    Nesting needs no special case: when `apply_signing` calls `sign`, both
    are decorated; the inner one maps first (or raises `ConcurrencyError`
    itself via `acquire`) and the outer sees an already-mapped error and
    passes it through.

    Do NOT apply this to `transaction()` itself -- see Addendum A3's "Why
    not transaction()" for the full reasoning (it would change the
    exception type seen by every non-RMLP caller in the project, and would
    map at the innermost nesting level, after the outer scope is already
    dead)."""

    @functools.wraps(func)
    async def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return await func(*args, **kwargs)
        except ConcurrencyError:
            raise
        except LOCK_CONTENTION_EXCEPTIONS as exc:
            log.warning(
                "%s: lock contention (sqlstate=%s) outside roster_lock's own "
                "acquisition calls -- league_id=%s",
                func.__qualname__,
                getattr(exc, "sqlstate", None),
                kwargs.get("league_id"),
            )
            raise ConcurrencyError(_ENTITY_CONTENTION_MESSAGE) from exc

    return wrapper  # type: ignore[return-value]
