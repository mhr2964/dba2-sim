"""league_transactions, league_transaction_assets -- `txn_repo`'s tables
(migration 013, Decision P5-31).

`details` is `JSONB`, marshalled to/from `dict` at this boundary via
`json.dumps`/`json.loads` -- no custom type codec is registered on the
pool (`dba2/data/db.py`), the same disclosed convention `media_repo.py`'s
`slot_bindings` and `league_repo.py`'s `phase_data` columns already use.

`create_trade_transaction` is the one composed writer `gm_service.
apply_trade` calls (Decision P5-34, as amended by P5-74): one header row
plus one asset row per player/pick moved, built directly from the same
four id tuples `TradeExecution` already carries -- there is no second
definition of "what moved" between the trade's own execution and its
log row.
"""
from __future__ import annotations

import json
from collections.abc import Sequence

import asyncpg

from dba2.data.db import Executor
from dba2.domain.phase import Phase
from dba2.domain.transaction import (
    LeagueTransaction,
    LeagueTransactionAsset,
    TransactionAssetKind,
    TransactionType,
)

_TRANSACTION_COLUMNS = """
    id, league_id, season, phase, txn_type, actor_user_id,
    source_offer_id, note, details, created_at
"""

_TRANSACTION_ASSET_COLUMNS = """
    id, transaction_id, asset_kind, player_id, draft_pick_id,
    from_team_id, to_team_id
"""


def _map_transaction(r: asyncpg.Record) -> LeagueTransaction:
    return LeagueTransaction(
        id=r["id"],
        league_id=r["league_id"],
        season=r["season"],
        phase=Phase(r["phase"]),
        txn_type=TransactionType(r["txn_type"]),
        actor_user_id=r["actor_user_id"],
        source_offer_id=r["source_offer_id"],
        note=r["note"],
        details=json.loads(r["details"]),
        created_at=r["created_at"],
    )


def _map_transaction_asset(r: asyncpg.Record) -> LeagueTransactionAsset:
    return LeagueTransactionAsset(
        id=r["id"],
        transaction_id=r["transaction_id"],
        asset_kind=TransactionAssetKind(r["asset_kind"]),
        player_id=r["player_id"],
        draft_pick_id=r["draft_pick_id"],
        from_team_id=r["from_team_id"],
        to_team_id=r["to_team_id"],
    )


async def create_league_transaction(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    phase: Phase,
    txn_type: TransactionType,
    actor_user_id: int | None = None,
    source_offer_id: int | None = None,
    note: str | None = None,
    details: dict | None = None,
) -> LeagueTransaction:
    row = await ex.fetchrow(
        f"""
        INSERT INTO league_transactions
            (league_id, season, phase, txn_type, actor_user_id,
             source_offer_id, note, details)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING {_TRANSACTION_COLUMNS}
        """,
        league_id,
        season,
        phase.value,
        txn_type.value,
        actor_user_id,
        source_offer_id,
        note,
        json.dumps(details if details is not None else {}),
    )
    return _map_transaction(row)


async def add_transaction_asset(
    ex: Executor,
    *,
    transaction_id: int,
    asset_kind: TransactionAssetKind,
    from_team_id: int | None,
    to_team_id: int | None,
    player_id: int | None = None,
    draft_pick_id: int | None = None,
) -> LeagueTransactionAsset:
    """One row. Caller must supply exactly `player_id` (kind `PLAYER`) or
    `draft_pick_id` (kind `PICK`) -- the presence-correspondence half of
    that rule is enforced here in Python, not by a database CHECK
    (migration 013's own docstring: a CHECK for "the matching id is
    non-`None`" is incompatible with `ON DELETE SET NULL` on the same
    columns, since a purged player's own cascade nulls `player_id` while
    `asset_kind` stays `'player'`, which that CHECK would then reject on
    the FK's own UPDATE). The OTHER half -- a `PLAYER` row never carries
    a `draft_pick_id`, and vice versa -- IS a database CHECK (migration
    014, `ck_league_transaction_assets_kind_excludes_wrong_column`),
    because `ON DELETE SET NULL` can only remove a value, never populate
    the wrong column, so that half was never in conflict and did not
    need to move here. See `dba2/domain/transaction.py`'s
    `LeagueTransactionAsset` docstring for the full split."""
    if asset_kind is TransactionAssetKind.PLAYER and (
        player_id is None or draft_pick_id is not None
    ):
        raise ValueError("asset_kind=PLAYER requires player_id and no draft_pick_id")
    if asset_kind is TransactionAssetKind.PICK and (
        draft_pick_id is None or player_id is not None
    ):
        raise ValueError("asset_kind=PICK requires draft_pick_id and no player_id")
    row = await ex.fetchrow(
        f"""
        INSERT INTO league_transaction_assets
            (transaction_id, asset_kind, player_id, draft_pick_id,
             from_team_id, to_team_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING {_TRANSACTION_ASSET_COLUMNS}
        """,
        transaction_id,
        asset_kind.value,
        player_id,
        draft_pick_id,
        from_team_id,
        to_team_id,
    )
    return _map_transaction_asset(row)


async def create_trade_transaction(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    phase: Phase,
    team_a_id: int,
    team_b_id: int,
    players_a_to_b: Sequence[int],
    players_b_to_a: Sequence[int],
    picks_a_to_b: Sequence[int],
    picks_b_to_a: Sequence[int],
    actor_user_id: int | None = None,
    source_offer_id: int | None = None,
    note: str | None = None,
) -> LeagueTransaction:
    """One header row (`txn_type = TRADE`) plus one asset row per player
    and pick moved, in that order -- every player first, then every
    pick, `a_to_b` before `b_to_a` within each, matching `TradeExecution`
    field order. Caller (`gm_service.apply_trade`) is responsible for
    wrapping this in its own transaction; this function issues its
    inserts sequentially on whatever `Executor` it is given and does not
    open one itself, so it composes correctly inside `apply_trade`'s
    existing `async with transaction(ex) as conn:` block via `data/
    db.py`'s documented savepoint nesting."""
    transaction_row = await create_league_transaction(
        ex,
        league_id=league_id,
        season=season,
        phase=phase,
        txn_type=TransactionType.TRADE,
        actor_user_id=actor_user_id,
        source_offer_id=source_offer_id,
        note=note,
    )
    for player_id in players_a_to_b:
        await add_transaction_asset(
            ex,
            transaction_id=transaction_row.id,
            asset_kind=TransactionAssetKind.PLAYER,
            player_id=player_id,
            from_team_id=team_a_id,
            to_team_id=team_b_id,
        )
    for player_id in players_b_to_a:
        await add_transaction_asset(
            ex,
            transaction_id=transaction_row.id,
            asset_kind=TransactionAssetKind.PLAYER,
            player_id=player_id,
            from_team_id=team_b_id,
            to_team_id=team_a_id,
        )
    for pick_id in picks_a_to_b:
        await add_transaction_asset(
            ex,
            transaction_id=transaction_row.id,
            asset_kind=TransactionAssetKind.PICK,
            draft_pick_id=pick_id,
            from_team_id=team_a_id,
            to_team_id=team_b_id,
        )
    for pick_id in picks_b_to_a:
        await add_transaction_asset(
            ex,
            transaction_id=transaction_row.id,
            asset_kind=TransactionAssetKind.PICK,
            draft_pick_id=pick_id,
            from_team_id=team_b_id,
            to_team_id=team_a_id,
        )
    return transaction_row


async def get_league_transaction(ex: Executor, transaction_id: int) -> LeagueTransaction | None:
    row = await ex.fetchrow(
        f"SELECT {_TRANSACTION_COLUMNS} FROM league_transactions WHERE id = $1",
        transaction_id,
    )
    return _map_transaction(row) if row is not None else None


async def list_transaction_assets(
    ex: Executor, transaction_id: int
) -> list[LeagueTransactionAsset]:
    rows = await ex.fetch(
        f"""
        SELECT {_TRANSACTION_ASSET_COLUMNS} FROM league_transaction_assets
        WHERE transaction_id = $1
        ORDER BY id
        """,
        transaction_id,
    )
    return [_map_transaction_asset(r) for r in rows]


async def list_transactions_for_league(
    ex: Executor, league_id: int
) -> list[LeagueTransaction]:
    """Every transaction ever logged for the league, oldest first,
    unscoped by season -- `view_service.get_league_transactions_feed`
    filters this whole-league result down to one season in Python, the
    same client-side filter it applies to `league_repo.list_phase_
    history`'s and `team_repo.list_manager_history_for_league`'s own
    equally unscoped reads (neither takes a `season` parameter either)."""
    rows = await ex.fetch(
        f"""
        SELECT {_TRANSACTION_COLUMNS} FROM league_transactions
        WHERE league_id = $1
        ORDER BY created_at, id
        """,
        league_id,
    )
    return [_map_transaction(r) for r in rows]


# ---------------------------------------------------------------------------
# docs/design/media-v2-newsroom.md MV-2a -- the media scanner's own trade
# reads. Reuses `league_transactions`/`league_transaction_assets` unchanged
# (no new column, no new table); every function below is a plain read
# against the existing schema.
# ---------------------------------------------------------------------------


async def list_trades_for_league_season(
    ex: Executor, *, league_id: int, season: int
) -> list[LeagueTransaction]:
    """Every real trade transaction for one league-season, oldest first --
    MV-2a's own trade-facts spine. `txn_type = 'trade'` narrows out every
    non-trade transaction type this table also carries (signings, waivers,
    etc. -- `TransactionType`'s own open vocabulary); the `WHERE league_id
    = $1 AND season = $2 ... ORDER BY created_at` shape hits `ix_league_
    transactions_league_season_created` exactly (MV-2a's own citation),
    with no new index needed."""
    rows = await ex.fetch(
        f"""
        SELECT {_TRANSACTION_COLUMNS} FROM league_transactions
        WHERE league_id = $1 AND season = $2 AND txn_type = $3
        ORDER BY created_at, id
        """,
        league_id,
        season,
        TransactionType.TRADE.value,
    )
    return [_map_transaction(r) for r in rows]


async def list_assets_for_transactions(
    ex: Executor, transaction_ids: Sequence[int]
) -> dict[int, list[LeagueTransactionAsset]]:
    """Every asset row for a whole BATCH of transaction ids, in one round
    trip -- MV-2a's own fix for the per-trade N+1 that calling
    `list_transaction_assets` once per transaction would otherwise cause
    (a season's worth of trades scanned every media cycle). `ANY($1)` over
    an `int[]` array parameter, never a caller-built `IN (...)` string --
    this codebase's standing parameterization rule. `transaction_ids = ()`
    returns `{}` without a round trip: `ANY($1::int[])` against an empty
    array is well-defined SQL (matches nothing), but skipping the query
    entirely is cheaper and avoids handing asyncpg a type-ambiguous empty
    Python list."""
    if not transaction_ids:
        return {}
    rows = await ex.fetch(
        f"""
        SELECT {_TRANSACTION_ASSET_COLUMNS} FROM league_transaction_assets
        WHERE transaction_id = ANY($1::int[])
        ORDER BY transaction_id, id
        """,
        list(transaction_ids),
    )
    by_transaction: dict[int, list[LeagueTransactionAsset]] = {tid: [] for tid in transaction_ids}
    for row in rows:
        asset = _map_transaction_asset(row)
        by_transaction.setdefault(asset.transaction_id, []).append(asset)
    return by_transaction


async def list_trades_involving_player(
    ex: Executor, *, league_id: int, player_id: int
) -> list[tuple[LeagueTransaction, LeagueTransactionAsset]]:
    """Every trade (header, asset) pair this player was moved in, across
    the league's whole history, oldest first -- MV-2a's own revenge-game
    and broken-promise spine (`dba2/media/detect/trades.py`'s eventual "did
    HE ask for this, and was it granted" and "has HE been traded to a team
    he is about to face again" reconstructions, both later build steps).
    Joins on `league_transaction_assets.player_id`, which migration 021's
    own `ix_league_transaction_assets_player` partial index (`WHERE
    player_id IS NOT NULL`) makes an index scan rather than the sequential
    scan this table's default `transaction_id`-only index would force. A
    since-purged player's own asset rows are `player_id IS NULL` (`ON
    DELETE SET NULL`) and therefore never match this predicate -- this
    function has nothing to say about a player who no longer exists to be
    the subject of a story, which is the correct, disclosed behavior, not
    a gap."""
    rows = await ex.fetch(
        """
        SELECT
            t.id AS t_id, t.league_id AS t_league_id, t.season AS t_season,
            t.phase AS t_phase, t.txn_type AS t_txn_type, t.actor_user_id AS t_actor_user_id,
            t.source_offer_id AS t_source_offer_id, t.note AS t_note, t.details AS t_details,
            t.created_at AS t_created_at,
            a.id AS a_id, a.transaction_id AS a_transaction_id, a.asset_kind AS a_asset_kind,
            a.player_id AS a_player_id, a.draft_pick_id AS a_draft_pick_id,
            a.from_team_id AS a_from_team_id, a.to_team_id AS a_to_team_id
        FROM league_transaction_assets a
        JOIN league_transactions t ON t.id = a.transaction_id
        WHERE t.league_id = $1 AND a.player_id = $2
        ORDER BY t.created_at, t.id, a.id
        """,
        league_id,
        player_id,
    )
    result: list[tuple[LeagueTransaction, LeagueTransactionAsset]] = []
    for r in rows:
        transaction = LeagueTransaction(
            id=r["t_id"],
            league_id=r["t_league_id"],
            season=r["t_season"],
            phase=Phase(r["t_phase"]),
            txn_type=TransactionType(r["t_txn_type"]),
            actor_user_id=r["t_actor_user_id"],
            source_offer_id=r["t_source_offer_id"],
            note=r["t_note"],
            details=json.loads(r["t_details"]),
            created_at=r["t_created_at"],
        )
        asset = LeagueTransactionAsset(
            id=r["a_id"],
            transaction_id=r["a_transaction_id"],
            asset_kind=TransactionAssetKind(r["a_asset_kind"]),
            player_id=r["a_player_id"],
            draft_pick_id=r["a_draft_pick_id"],
            from_team_id=r["a_from_team_id"],
            to_team_id=r["a_to_team_id"],
        )
        result.append((transaction, asset))
    return result
