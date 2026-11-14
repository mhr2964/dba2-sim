"""trade_offers -- `offer_repo`'s one table (migration 012, a minimal
slice of Decision P5-35's own schema; see `dba2/domain/trade_offer.py`
for the full scoping rationale).

docs/design/phase-5-discord-presentation.md §28.4 (Decision P5-59):
`run_phase_hook`'s trade branch is this module's only writer today, one
row per AI-initiated offer to a human-managed partner, `proposed_by =
NULL`, `status = TradeStatus.PROPOSED`. `create_trade_offer` is a
single-row insert, not a bulk `UNNEST` writer -- matching `media_repo.py`'s
own `create_media_story` precedent and its stated reason: a window's own
worth of offers is small (at most `MAX_AI_OFFERS_PER_HUMAN_TEAM_PER_
WINDOW` per human team, i.e. a handful league-wide), never the "many rows,
one shape, no other per-row work" volume `head_to_head_repo`'s bulk upsert
exists for.

**Since migration 018** (docs/design/phase-5-trade-offer-review-surface.md
§2, §12 step 2) this module is also the trade-offer review surface's whole
storage layer: single-row reads (`get_trade_offer`, plus `get_trade_offer_
for_update` -- `trade_offers` is RMLP "tier 3", see that function), a
both-sides inbox read, the status CAS (`resolve_offer_cas`), the lazy expiry
sweep, and the delivery drain's queue (`list_undelivered_offers`,
`set_offer_message`). Nothing in here knows `TRADE_TRANSITIONS`
(`data/` may not import `phase/`) -- `offer_service` checks legality of a
transition and this module performs the write.

`package` is `JSONB`, marshalled to/from `dict` at this boundary via
`json.dumps`/`json.loads` -- no custom type codec is registered on the
pool (`dba2/data/db.py`), the same convention `media_repo.py`'s `slot_
bindings` and `league_repo.py`'s `phase_data` columns already use.
"""
from __future__ import annotations

import json

import asyncpg

from dba2.core.errors import NotFoundError
from dba2.data.db import Executor, transaction
from dba2.data.repositories import league_repo
from dba2.domain.phase import Phase, TradeStatus
from dba2.domain.trade_offer import TradeOffer

_TRADE_OFFER_COLUMNS = """
    id, league_id, season, phase, from_team_id, to_team_id, proposed_by,
    status, package, created_at, counters_offer_id, resolved_at, resolved_by,
    illegal_token, channel_id, message_id
"""


def _map_trade_offer(r: asyncpg.Record) -> TradeOffer:
    raw_package = json.loads(r["package"])
    package = {key: tuple(value) for key, value in raw_package.items()}
    return TradeOffer(
        id=r["id"],
        league_id=r["league_id"],
        season=r["season"],
        phase=Phase(r["phase"]),
        from_team_id=r["from_team_id"],
        to_team_id=r["to_team_id"],
        proposed_by=r["proposed_by"],
        status=TradeStatus(r["status"]),
        package=package,
        created_at=r["created_at"],
        counters_offer_id=r["counters_offer_id"],
        resolved_at=r["resolved_at"],
        resolved_by=r["resolved_by"],
        illegal_token=r["illegal_token"],
        channel_id=r["channel_id"],
        message_id=r["message_id"],
    )


async def create_trade_offer(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    phase: Phase,
    from_team_id: int,
    to_team_id: int,
    proposed_by: int | None,
    status: TradeStatus,
    players_from_to: tuple[int, ...],
    players_to_from: tuple[int, ...],
    picks_from_to: tuple[int, ...],
    picks_to_from: tuple[int, ...],
    counters_offer_id: int | None = None,
) -> TradeOffer:
    """One offer, one insert -- see module docstring for why this is
    single-row rather than bulk. No `ON CONFLICT` clause: two AI teams
    proposing near-identical packages to the same human partner in the
    same window are two real, distinct offers, not a duplicate to
    collapse (P5-57's own per-pair-then-per-team gate/cap already bounds
    how many of these exist before this function is ever called).

    `counters_offer_id` is keyword-only with a `None` default so
    `run_phase_hook`'s existing caller (a root offer, always) compiles
    unchanged; only `offer_service.counter` passes it. Every other
    migration-018 column is written by a later UPDATE (a CAS, the
    delivery drain), never by the insert -- an offer is born `PROPOSED`,
    unresolved and undelivered."""
    package = json.dumps(
        {
            "players_from_to": list(players_from_to),
            "players_to_from": list(players_to_from),
            "picks_from_to": list(picks_from_to),
            "picks_to_from": list(picks_to_from),
        }
    )
    row = await ex.fetchrow(
        f"""
        INSERT INTO trade_offers
            (league_id, season, phase, from_team_id, to_team_id, proposed_by,
             status, package, counters_offer_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING {_TRADE_OFFER_COLUMNS}
        """,
        league_id,
        season,
        phase.value,
        from_team_id,
        to_team_id,
        proposed_by,
        status.value,
        package,
        counters_offer_id,
    )
    return _map_trade_offer(row)


async def list_trade_offers_for_team(
    ex: Executor, *, league_id: int, team_id: int
) -> list[TradeOffer]:
    """Every offer addressed TO `team_id` (never offers it made) -- the
    read shape P5-65's must-fire test and, eventually, a human's `/offers`
    inbox (P5-60) both need. Ordered oldest-first (`id` is monotonic per
    `SERIAL`, matching `media_repo.list_media_stories_for_league_season`'s
    identical ordering choice)."""
    rows = await ex.fetch(
        f"""
        SELECT {_TRADE_OFFER_COLUMNS} FROM trade_offers
        WHERE league_id = $1 AND to_team_id = $2
        ORDER BY id
        """,
        league_id,
        team_id,
    )
    return [_map_trade_offer(r) for r in rows]


async def expire_offers_for_team(ex: Executor, team_id: int) -> None:
    """Blocking Issue 5 (Stage C critic round): the design doc's own
    P5-41 text ("assignment or removal of a manager on EITHER side
    expires that team's outstanding offers") had no writer anywhere in
    this module before this function -- `league_service.assign_manager`/
    `remove_manager` are its only two callers, run inside the SAME
    transaction as the `manager_user_id` write (a status transition is a
    plain DB write, not Discord I/O, so nothing about Decision P5-14
    keeps it out of that transaction).

    Scoped to `PROPOSED` only (§6.4's transition table: `proposed` can
    move to any of the other five, none of them has an outgoing edge --
    an already-`ACCEPTED`/`REJECTED`/`COUNTERED`/`EXPIRED`/`VETOED` row
    is a closed chain link, never touched again). `team_id` on EITHER
    side (`from_team_id` or `to_team_id`) per P5-41's own wording -- every
    offer this schema can hold today is AI-initiated with `to_team_id`
    the human-managed partner (`gm_service.run_phase_hook`'s trade
    branch, the one real writer), so `from_team_id = team_id` is a no-op
    against today's data and exists for the human-initiated offer shape
    P5-59/P5-61 name as still-future, not yet built.

    **Writes `resolved_at = NOW()` since migration 018**, a timestamp this
    function never needed before: `resolved_at` is the column every
    status-leaving-`PROPOSED` path is supposed to stamp, and manager churn
    resolves more rows than human clicks will. `resolved_by` stays `NULL`
    on purpose -- nobody resolved these, a rule did (docs/design/phase-5-
    trade-offer-review-surface.md §2, NIT 8).

    **Must stay in the same transaction as `team_repo.set_manager`.**
    `offer_service._lock_offer_for_answer` reads the recipient's manager
    unlocked and relies on this expiry -- committed atomically with the
    manager change and observed by its offer-row `FOR UPDATE` re-read -- as
    the only thing that stops a just-removed manager from answering. A
    caller that expires offers in a separate transaction reopens that race."""
    await ex.execute(
        "UPDATE trade_offers SET status = $2, resolved_at = NOW() "
        "WHERE (from_team_id = $1 OR to_team_id = $1) AND status = $3",
        team_id,
        TradeStatus.EXPIRED.value,
        TradeStatus.PROPOSED.value,
    )


async def list_trade_offers_for_league(ex: Executor, league_id: int) -> list[TradeOffer]:
    """Every offer in a league, regardless of side -- the whole-window
    shape the synthetic-human integration test reads to assert both the
    positive claim (a real row addressed to the human team) and the
    negative one (zero rows where the human team is `from_team_id`)."""
    rows = await ex.fetch(
        f"SELECT {_TRADE_OFFER_COLUMNS} FROM trade_offers WHERE league_id = $1 ORDER BY id",
        league_id,
    )
    return [_map_trade_offer(r) for r in rows]


async def get_trade_offer(ex: Executor, offer_id: int) -> TradeOffer | None:
    """Plain read, no lock. `offer_service`'s discovery read (§5.3 step 3):
    the caller uses it only to learn which teams/players to lock and must
    re-read with `get_trade_offer_for_update` under those locks before
    believing anything about `status`."""
    row = await ex.fetchrow(
        f"SELECT {_TRADE_OFFER_COLUMNS} FROM trade_offers WHERE id = $1", offer_id
    )
    return _map_trade_offer(row) if row is not None else None


async def get_trade_offer_for_update(ex: Executor, offer_id: int) -> TradeOffer | None:
    """`SELECT ... FOR UPDATE` on one offer row -- `trade_offers` is RMLP
    "tier 3" (docs/design/phase-5-trade-offer-review-surface.md §6.3):
    call this only AFTER the tier-1/tier-2 locks (`roster_lock.acquire`)
    are held, never before. Blocks (bounded by the transaction's
    `lock_timeout`, which `roster_lock.open_league_gate` sets for this
    caller -- a gate-less transaction has to set its own through
    `roster_lock.set_lock_timeout`, P5-98.g) until
    a concurrent resolver of the same row commits, which is what makes a
    `veto` racing an accept lose cleanly instead of interleaving. Must run
    inside an open transaction on a `Connection`."""
    row = await ex.fetchrow(
        f"SELECT {_TRADE_OFFER_COLUMNS} FROM trade_offers WHERE id = $1 FOR UPDATE", offer_id
    )
    return _map_trade_offer(row) if row is not None else None


async def list_offers_for_team_both_sides(
    ex: Executor, *, league_id: int, team_id: int, status: TradeStatus | None = None
) -> list[TradeOffer]:
    """Offers `team_id` received AND sent, oldest-first, optionally
    filtered to one `status` -- `/offers` shows both directions (§4.3).
    `list_trade_offers_for_team` above stays receive-only and unfiltered:
    its existing callers (P5-65's must-fire test) depend on that shape."""
    if status is None:
        rows = await ex.fetch(
            f"""
            SELECT {_TRADE_OFFER_COLUMNS} FROM trade_offers
            WHERE league_id = $1 AND (from_team_id = $2 OR to_team_id = $2)
            ORDER BY id
            """,
            league_id,
            team_id,
        )
    else:
        rows = await ex.fetch(
            f"""
            SELECT {_TRADE_OFFER_COLUMNS} FROM trade_offers
            WHERE league_id = $1 AND (from_team_id = $2 OR to_team_id = $2) AND status = $3
            ORDER BY id
            """,
            league_id,
            team_id,
            status.value,
        )
    return [_map_trade_offer(r) for r in rows]


async def resolve_offer_cas(
    ex: Executor,
    offer_id: int,
    *,
    expected_status: TradeStatus,
    new_status: TradeStatus,
    resolved_by: int | None,
    illegal_token: str | None = None,
) -> int:
    """Compare-and-swap `status` from `expected_status` to `new_status`,
    stamping `resolved_at = NOW()`, `resolved_by` and `illegal_token` in
    the same statement. Returns the affected-row count: `1` means this
    caller made the transition, `0` means the row was no longer in
    `expected_status` (someone else resolved it first) and NOTHING was
    written -- the caller decides whether that is "report the current
    state" or "abort the whole transaction" (`offer_service` §5.3 steps 6
    and 13). This function does not know `TRADE_TRANSITIONS`
    (`data/` may not import `phase/`); the service checks
    `is_legal_transition` first, per the design's own step 13.

    `resolved_by IS NULL` is a real value: the AI answered, or a rule
    (an expiry) did -- nobody clicked."""
    result = await ex.execute(
        """
        UPDATE trade_offers
        SET status = $3, resolved_at = NOW(), resolved_by = $4, illegal_token = $5
        WHERE id = $1 AND status = $2
        """,
        offer_id,
        expected_status.value,
        new_status.value,
        resolved_by,
        illegal_token,
    )
    return int(result.rsplit(" ", 1)[-1])


async def expire_stale_offers_for_league(ex: Executor, league_id: int) -> int:
    """Expires every `PROPOSED` offer in `league_id` whose `(season, phase)`
    is not the league's CURRENT `(season, phase)` -- the lazy expiry rule
    (docs/design/phase-5-trade-offer-review-surface.md §6.4, P5-61 as
    amended by P5-83). Returns how many rows it expired.

    **Takes no season/phase parameters, on purpose (round 4, BL-D).** It
    reads the league's `(current_phase, current_season)` itself, under the
    `leagues` row `FOR SHARE` lock it takes in its own transaction, and
    compares against exactly those values. A caller-supplied pair read on
    the pool before this transaction can be stale by the time the lock is
    granted; the `IS DISTINCT FROM` predicate would then expire every
    `PROPOSED` row in the NEW phase -- including offers the AI's own
    post-advance phase hook had just inserted. The advance's own
    `FOR UPDATE` on that row (`league_repo.get_phase_state_for_update`)
    blocks until this transaction commits, so no advance can slip between
    the read and the write.

    **Never call this from inside `respond`/`propose`/`counter`, or from
    any transaction that holds or will take a team or player lock.**
    `trade_offers` is RMLP tier 3 (§6.3): a transaction that touches it
    before tier 1/2 is the `league_service.assign_manager` lock-order
    inversion. This function opens its OWN transaction so that shape is
    impossible by construction when it is handed a Pool; handed an open
    `Connection` it nests as a savepoint and inherits the caller's locks,
    which is exactly the misuse this paragraph forbids. It is called from
    `/offers` and, when it lands, Stage E's advance transaction (OS-1).

    The `UPDATE` selects victims `ORDER BY id ... FOR UPDATE SKIP LOCKED`:
    a consistent order between two concurrent sweeps, and a row some other
    transaction is resolving right now is skipped rather than waited on --
    correct, since that transaction holds the gate and team locks and runs
    the per-offer staleness check (§5.3 step 7) on that very row itself.
    The `$2::int, $3::text` casts are required: a bare parameter inside a
    row constructor gives the planner nothing to infer its type from and
    the statement fails to prepare."""
    async with transaction(ex) as conn:
        state = await league_repo.get_league_state_for_claim_check(conn, league_id)
        if state is None:
            raise NotFoundError(f"league {league_id} does not exist")
        rows = await conn.fetch(
            """
            UPDATE trade_offers
            SET status = $4, resolved_at = NOW()
            WHERE id IN (
                SELECT id FROM trade_offers
                WHERE league_id = $1
                  AND status = $5
                  AND (season, phase) IS DISTINCT FROM ($2::int, $3::text)
                ORDER BY id
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id
            """,
            league_id,
            state.current_season,
            state.current_phase.value,
            TradeStatus.EXPIRED.value,
            TradeStatus.PROPOSED.value,
        )
        return len(rows)


async def set_offer_message(
    ex: Executor, offer_id: int, *, channel_id: int, message_id: int
) -> None:
    """The delivery drain's write-back (§6.5). Overwrites unconditionally:
    a re-post after the accepted at-least-once duplicate (a crash between
    the Discord post and this write) simply records the newer message."""
    await ex.execute(
        "UPDATE trade_offers SET channel_id = $2, message_id = $3 WHERE id = $1",
        offer_id,
        channel_id,
        message_id,
    )


async def list_undelivered_offers(ex: Executor, league_id: int) -> list[TradeOffer]:
    """`status = 'proposed' AND message_id IS NULL` -- the delivery queue
    (P5-60). Oldest-first so a drain posts in the order offers were made."""
    rows = await ex.fetch(
        f"""
        SELECT {_TRADE_OFFER_COLUMNS} FROM trade_offers
        WHERE league_id = $1 AND status = $2 AND message_id IS NULL
        ORDER BY id
        """,
        league_id,
        TradeStatus.PROPOSED.value,
    )
    return [_map_trade_offer(r) for r in rows]
