"""Live I/O proof for `data/repositories/txn_repo.py` -- migration 013's
`league_transactions`/`league_transaction_assets` tables (Decision
P5-31, amended by P5-93/migration 014). `gm_service.apply_trade`'s own
coverage (`tests/integration/test_gm_service.py`) proves the ONE
production writer end to end; this file proves the repo module itself,
in isolation, including both halves of the asset-kind/id-column rule:
the Python-level presence-correspondence guard (migration 013's own
docstring: a CHECK for "the matching id is non-`None`" is incompatible
with `ON DELETE SET NULL`) and the DB-level wrong-column-exclusion CHECK
migration 014 restores (never in conflict with `ON DELETE SET NULL`,
since that clause can only remove a value, never populate the wrong
column).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it
-- same requirement as every other tests/integration/ module that
touches real Postgres.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime

import asyncpg
import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import draft_repo, league_repo, player_repo, team_repo, txn_repo
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.team import GmPhilosophy, Timeline
from dba2.domain.transaction import TransactionAssetKind, TransactionType

_SEASON = 2026


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Txn Repo Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=1,
        rng_seed=1,
        current_phase=Phase.TRADE_DEADLINE,
    )
    return league.id


async def _make_team(pool, league_id: int, code: str) -> int:
    team = await team_repo.create_team(
        pool,
        league_id=league_id,
        code=code,
        name=f"{code} Team",
        city=f"{code} City",
        conference="East",
        division="Atlantic",
        home_market_size=3,
        gm_philosophy=GmPhilosophy.EGALITARIAN,
        timeline=Timeline.RETOOL,
    )
    return team.id


async def _make_player(pool, league_id: int, team_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Test",
        last_name=f"Player{random.randint(0, 10**9)}",
        primary_position=Position.SF,
        height_in=79,
        weight_lb=215,
        wingspan_in=82,
        birth_date=datetime(1998, 1, 1).date(),
        roster_status=RosterStatus.ACTIVE,
        overall_cached=75,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    return player.id


@pytest.mark.asyncio
async def test_create_trade_transaction_writes_header_and_every_asset_row():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "AA")
            team_b = await _make_team(pool, league_id, "BB")
            p_a = await _make_player(pool, league_id, team_a)
            p_b = await _make_player(pool, league_id, team_b)

            txn = await txn_repo.create_trade_transaction(
                pool,
                league_id=league_id,
                season=_SEASON,
                phase=Phase.TRADE_DEADLINE,
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(p_a,),
                players_b_to_a=(p_b,),
                picks_a_to_b=(),
                picks_b_to_a=(),
                actor_user_id=None,
                source_offer_id=None,
            )
            assert txn.txn_type is TransactionType.TRADE
            assert txn.league_id == league_id

            assets = await txn_repo.list_transaction_assets(pool, txn.id)
            assert {(a.player_id, a.from_team_id, a.to_team_id) for a in assets} == {
                (p_a, team_a, team_b),
                (p_b, team_b, team_a),
            }

            fetched = await txn_repo.list_transactions_for_league(pool, league_id)
            assert [t.id for t in fetched] == [txn.id]
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_add_transaction_asset_rejects_mismatched_kind_and_ids():
    """The must-fire case for the Python-level guard migration 013's own
    docstring requires in place of a DB CHECK: called with neither id,
    or with both, for either kind -- every one of the four wrong shapes
    raises `ValueError` before any I/O (no `Executor` needed)."""
    with pytest.raises(ValueError, match="PLAYER"):
        await txn_repo.add_transaction_asset(
            None,
            transaction_id=1,
            asset_kind=TransactionAssetKind.PLAYER,
            from_team_id=1,
            to_team_id=2,
        )
    with pytest.raises(ValueError, match="PLAYER"):
        await txn_repo.add_transaction_asset(
            None,
            transaction_id=1,
            asset_kind=TransactionAssetKind.PLAYER,
            player_id=5,
            draft_pick_id=6,
            from_team_id=1,
            to_team_id=2,
        )
    with pytest.raises(ValueError, match="PICK"):
        await txn_repo.add_transaction_asset(
            None,
            transaction_id=1,
            asset_kind=TransactionAssetKind.PICK,
            from_team_id=1,
            to_team_id=2,
        )
    with pytest.raises(ValueError, match="PICK"):
        await txn_repo.add_transaction_asset(
            None,
            transaction_id=1,
            asset_kind=TransactionAssetKind.PICK,
            player_id=5,
            draft_pick_id=6,
            from_team_id=1,
            to_team_id=2,
        )


@pytest.mark.asyncio
async def test_asset_row_survives_its_players_own_purge_with_player_id_nulled():
    """The real proof `ON DELETE SET NULL` behaves as migration 013's
    docstring claims once the presence-correspondence CHECK is gone: a
    trade involving a since-purged player is still real league history
    -- the asset row survives, `player_id` goes `NULL`, nothing raises.
    Migration 014's wrong-column-exclusion CHECK does not fire here --
    it only ever rejects a row where `draft_pick_id` gets POPULATED on a
    `PLAYER` row, and this purge only ever nulls `player_id`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "CC")
            team_b = await _make_team(pool, league_id, "DD")
            p_a = await _make_player(pool, league_id, team_a)

            txn = await txn_repo.create_trade_transaction(
                pool,
                league_id=league_id,
                season=_SEASON,
                phase=Phase.TRADE_DEADLINE,
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(p_a,),
                players_b_to_a=(),
                picks_a_to_b=(),
                picks_b_to_a=(),
            )

            await pool.execute("DELETE FROM players WHERE id = $1", p_a)

            assets = await txn_repo.list_transaction_assets(pool, txn.id)
            assert len(assets) == 1
            assert assets[0].player_id is None
            assert assets[0].asset_kind is TransactionAssetKind.PLAYER
            assert assets[0].from_team_id == team_a
            assert assets[0].to_team_id == team_b
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_asset_row_survives_its_picks_own_purge_with_draft_pick_id_nulled():
    """The pick-side twin of the player purge test above, and the other
    real-Postgres proof migration 014's own docstring cites: deleting a
    draft pick `ON DELETE SET NULL`s `draft_pick_id` on its own asset
    row -- `asset_kind` stays `PICK`, nothing raises, and migration 014's
    restored wrong-column-exclusion CHECK does not fire (it only rejects
    `player_id` getting POPULATED on a `PICK` row; this purge only nulls
    `draft_pick_id`)."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "EE")
            team_b = await _make_team(pool, league_id, "FF")
            pick = await draft_repo.create_draft_pick(
                pool,
                league_id=league_id,
                season=_SEASON + 1,
                round=1,
                original_team_id=team_a,
                current_team_id=team_a,
            )

            txn = await txn_repo.create_trade_transaction(
                pool,
                league_id=league_id,
                season=_SEASON,
                phase=Phase.TRADE_DEADLINE,
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(),
                players_b_to_a=(),
                picks_a_to_b=(pick.id,),
                picks_b_to_a=(),
            )

            await pool.execute("DELETE FROM draft_picks WHERE id = $1", pick.id)

            assets = await txn_repo.list_transaction_assets(pool, txn.id)
            assert len(assets) == 1
            assert assets[0].draft_pick_id is None
            assert assets[0].asset_kind is TransactionAssetKind.PICK
            assert assets[0].from_team_id == team_a
            assert assets[0].to_team_id == team_b
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_wrong_column_check_rejects_a_player_row_carrying_a_draft_pick_id():
    """Migration 014's restored CHECK is load-bearing, not merely
    redundant with `txn_repo.add_transaction_asset`'s own Python guard
    -- proved by going around that guard on purpose (a raw `INSERT`
    against the table, not a call through the repo function) and
    confirming the database itself is what rejects the wrong-column
    shape, with the constraint's own name in the error."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "GG")
            team_b = await _make_team(pool, league_id, "HH")
            p_a = await _make_player(pool, league_id, team_a)
            pick = await draft_repo.create_draft_pick(
                pool,
                league_id=league_id,
                season=_SEASON + 1,
                round=1,
                original_team_id=team_a,
                current_team_id=team_a,
            )

            txn = await txn_repo.create_league_transaction(
                pool,
                league_id=league_id,
                season=_SEASON,
                phase=Phase.TRADE_DEADLINE,
                txn_type=TransactionType.TRADE,
            )

            with pytest.raises(
                asyncpg.CheckViolationError,
                match="ck_league_transaction_assets_kind_excludes_wrong_column",
            ):
                await pool.execute(
                    """
                    INSERT INTO league_transaction_assets
                        (transaction_id, asset_kind, player_id, draft_pick_id,
                         from_team_id, to_team_id)
                    VALUES ($1, 'player', $2, $3, $4, $5)
                    """,
                    txn.id,
                    p_a,
                    pick.id,
                    team_a,
                    team_b,
                )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# docs/design/media-v2-newsroom.md MV-2a -- the media scanner's own trade
# reads (step 2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_trades_for_league_season_scopes_by_season_and_type():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "IA")
            team_b = await _make_team(pool, league_id, "IB")
            p_a = await _make_player(pool, league_id, team_a)

            this_season = await txn_repo.create_trade_transaction(
                pool, league_id=league_id, season=_SEASON, phase=Phase.TRADE_DEADLINE,
                team_a_id=team_a, team_b_id=team_b, players_a_to_b=(p_a,),
                players_b_to_a=(), picks_a_to_b=(), picks_b_to_a=(),
            )
            # A different season's trade must not leak in.
            await txn_repo.create_trade_transaction(
                pool, league_id=league_id, season=_SEASON + 1, phase=Phase.TRADE_DEADLINE,
                team_a_id=team_a, team_b_id=team_b, players_a_to_b=(),
                players_b_to_a=(), picks_a_to_b=(), picks_b_to_a=(),
            )
            # A non-trade transaction must not leak in either.
            await txn_repo.create_league_transaction(
                pool, league_id=league_id, season=_SEASON, phase=Phase.FREE_AGENCY,
                txn_type=TransactionType.TIMELINE_SET,
            )

            trades = await txn_repo.list_trades_for_league_season(
                pool, league_id=league_id, season=_SEASON
            )
            assert [t.id for t in trades] == [this_season.id]
            assert trades[0].txn_type is TransactionType.TRADE
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_assets_for_transactions_batches_without_an_n_plus_1():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "JA")
            team_b = await _make_team(pool, league_id, "JB")
            p_a = await _make_player(pool, league_id, team_a)
            p_b = await _make_player(pool, league_id, team_b)

            txn_1 = await txn_repo.create_trade_transaction(
                pool, league_id=league_id, season=_SEASON, phase=Phase.TRADE_DEADLINE,
                team_a_id=team_a, team_b_id=team_b, players_a_to_b=(p_a,),
                players_b_to_a=(), picks_a_to_b=(), picks_b_to_a=(),
            )
            txn_2 = await txn_repo.create_trade_transaction(
                pool, league_id=league_id, season=_SEASON, phase=Phase.TRADE_DEADLINE,
                team_a_id=team_b, team_b_id=team_a, players_a_to_b=(p_b,),
                players_b_to_a=(), picks_a_to_b=(), picks_b_to_a=(),
            )

            by_transaction = await txn_repo.list_assets_for_transactions(
                pool, [txn_1.id, txn_2.id]
            )
            assert {a.player_id for a in by_transaction[txn_1.id]} == {p_a}
            assert {a.player_id for a in by_transaction[txn_2.id]} == {p_b}

            assert await txn_repo.list_assets_for_transactions(pool, []) == {}

            # A transaction id with no asset rows still gets an entry --
            # never absent from the returned dict.
            header_only = await txn_repo.create_league_transaction(
                pool, league_id=league_id, season=_SEASON, phase=Phase.FREE_AGENCY,
                txn_type=TransactionType.TIMELINE_SET,
            )
            with_empty = await txn_repo.list_assets_for_transactions(
                pool, [txn_1.id, header_only.id]
            )
            assert with_empty[header_only.id] == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_list_trades_involving_player_finds_every_trade_across_seasons():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "KA")
            team_b = await _make_team(pool, league_id, "KB")
            team_c = await _make_team(pool, league_id, "KC")
            p_a = await _make_player(pool, league_id, team_a)
            other_player = await _make_player(pool, league_id, team_b)

            first_trade = await txn_repo.create_trade_transaction(
                pool, league_id=league_id, season=_SEASON, phase=Phase.TRADE_DEADLINE,
                team_a_id=team_a, team_b_id=team_b, players_a_to_b=(p_a,),
                players_b_to_a=(), picks_a_to_b=(), picks_b_to_a=(),
            )
            # A trade that never touched p_a must not appear.
            await txn_repo.create_trade_transaction(
                pool, league_id=league_id, season=_SEASON, phase=Phase.TRADE_DEADLINE,
                team_a_id=team_b, team_b_id=team_c, players_a_to_b=(other_player,),
                players_b_to_a=(), picks_a_to_b=(), picks_b_to_a=(),
            )
            second_trade = await txn_repo.create_trade_transaction(
                pool, league_id=league_id, season=_SEASON + 1, phase=Phase.TRADE_DEADLINE,
                team_a_id=team_b, team_b_id=team_c, players_a_to_b=(p_a,),
                players_b_to_a=(), picks_a_to_b=(), picks_b_to_a=(),
            )

            found = await txn_repo.list_trades_involving_player(
                pool, league_id=league_id, player_id=p_a
            )
            assert [t.id for t, _a in found] == [first_trade.id, second_trade.id]
            assert [a.from_team_id for _t, a in found] == [team_a, team_b]

            # A purged player's own asset rows (player_id NULLed by the
            # cascade) never match this predicate -- there is nothing left
            # to be the subject of a story about.
            await pool.execute("DELETE FROM players WHERE id = $1", p_a)
            assert await txn_repo.list_trades_involving_player(
                pool, league_id=league_id, player_id=p_a
            ) == []
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
