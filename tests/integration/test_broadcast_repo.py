"""`broadcast_repo` against real Postgres -- migration 019's `game_broadcasts`
table and every repository function the Stage D broadcast layer calls
(docs/design/phase-5-stage-d-broadcast.md §8, P5-97.m/.q/.v/.w).

The bot layer built on these functions is proven in later commits; this file
pins the storage contract: idempotent creation, the two-phase reveal CAS
(including its zero-row path and the claimed-but-unposted state), the status
machine, league scoping, and the three (plus one) shapes the startup sweep
selects.

Every test that names a second league does so to prove a function handed
league A's id can neither see nor change league B's row.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import asyncio
import random
from datetime import date

import asyncpg
import pytest

from dba2.core.errors import ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import broadcast_repo, game_repo, league_repo, team_repo
from dba2.domain.broadcast import BroadcastStatus, SweepShape
from dba2.domain.game import GameType
from dba2.domain.phase import Phase

_BLOCK = "2026-11-01"

_Q, _L, _F, _I = (
    BroadcastStatus.QUEUED,
    BroadcastStatus.LIVE,
    BroadcastStatus.FINISHED,
    BroadcastStatus.INTERRUPTED,
)


async def _seed_league(pool, name: str, *, games: int):
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name=name,
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
        current_phase=Phase.REGULAR_SEASON,
    )
    teams = []
    for code in ("BA", "BB"):
        team = await team_repo.create_team(
            pool, league_id=league.id, code=code, name=f"{code} Team", city=code,
            conference="East", division="Atlantic", home_market_size=3,
        )
        teams.append(team.id)
    game_ids = []
    for i in range(games):
        game = await game_repo.create_game(
            pool,
            league_id=league.id,
            season=2026,
            game_index=i + 1,
            game_date=date(2026, 11, 1),
            home_team_id=teams[0],
            away_team_id=teams[1],
            game_type=GameType.REGULAR,
            rng_seed=100 + i,
        )
        game_ids.append(game.id)
    return league.id, game_ids


@pytest.fixture
async def world():
    """(pool, league_a, games_a, league_b, games_b) -- three games in A, two
    in B, so every scoping assertion has a real other-league row to leak."""
    pool = await get_pool()
    a, games_a = await _seed_league(pool, "Broadcast Repo League A", games=3)
    b, games_b = await _seed_league(pool, "Broadcast Repo League B", games=2)
    try:
        yield pool, a, games_a, b, games_b
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = ANY($1::int[])", [a, b])
        await close_pool()


@pytest.fixture
async def big_world():
    """League A with six games (one per sweep shape plus a revealed one) and
    league B with two, all of B's rows unrevealed -- so a leak is visible."""
    pool = await get_pool()
    a, games_a = await _seed_league(pool, "Broadcast Sweep League A", games=6)
    b, games_b = await _seed_league(pool, "Broadcast Sweep League B", games=2)
    try:
        yield pool, a, games_a, b, games_b
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = ANY($1::int[])", [a, b])
        await close_pool()


async def _pending(pool, league_id, game_ids, block=_BLOCK):
    inserted = await broadcast_repo.create_pending_broadcasts(
        pool, league_id=league_id, block_id=block, game_ids=game_ids
    )
    assert inserted == len(game_ids)


async def _walk(pool, league_id, game_id, *path):
    """Walk a row through legal transitions via the real writer."""
    for old, new in zip(path, path[1:], strict=False):
        assert await broadcast_repo.transition_status(
            pool, league_id=league_id, game_id=game_id, expected=old, new=new
        ) == 1


async def _queued(pool, league_id, game_id):
    await _pending(pool, league_id, [game_id])
    assert await broadcast_repo.record_thread(
        pool, league_id=league_id, game_id=game_id, thread_id=1, message_id=2
    ) == 1


async def _get(pool, league_id, game_id):
    return await broadcast_repo.get_broadcast(pool, league_id=league_id, game_id=game_id)


# ---------------------------------------------------------------------------
# create_pending_broadcasts
# ---------------------------------------------------------------------------


async def test_a_fresh_row_is_pending_with_no_discord_objects(world):
    pool, a, games_a, _b, _gb = world
    await _pending(pool, a, games_a[:1])
    row = await _get(pool, a, games_a[0])
    assert row is not None
    assert (row.league_id, row.game_id, row.block_id) == (a, games_a[0], _BLOCK)
    assert row.status is BroadcastStatus.PENDING
    assert row.root_message_id is None and row.thread_id is None and row.message_id is None
    assert row.reveal_claimed_at is None and row.reveal_posted_at is None
    assert row.created_at is not None


async def test_create_is_idempotent_second_run_inserts_zero(world):
    pool, a, games_a, _b, _gb = world
    first = await broadcast_repo.create_pending_broadcasts(
        pool, league_id=a, block_id=_BLOCK, game_ids=games_a
    )
    second = await broadcast_repo.create_pending_broadcasts(
        pool, league_id=a, block_id=_BLOCK, game_ids=games_a
    )
    assert (first, second) == (3, 0)
    assert await pool.fetchval(
        "SELECT count(*) FROM game_broadcasts WHERE league_id = $1", a
    ) == 3


async def test_a_partial_overlap_inserts_only_the_new_games(world):
    pool, a, games_a, _b, _gb = world
    await _pending(pool, a, games_a[:2])
    inserted = await broadcast_repo.create_pending_broadcasts(
        pool, league_id=a, block_id=_BLOCK, game_ids=games_a
    )
    assert inserted == 1


async def test_re_creating_never_rewinds_or_rehomes_an_existing_row(world):
    """`ON CONFLICT DO NOTHING`, not an upsert: a retried `open_block` must not
    reset a broadcast that has begun, nor move it to another block."""
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _pending(pool, a, [game])
    assert await broadcast_repo.record_thread(
        pool, league_id=a, game_id=game, thread_id=11, message_id=12
    ) == 1
    inserted = await broadcast_repo.create_pending_broadcasts(
        pool, league_id=a, block_id="2026-11-02", game_ids=[game]
    )
    assert inserted == 0
    row = await _get(pool, a, game)
    assert row.status is BroadcastStatus.QUEUED
    assert row.block_id == _BLOCK
    assert (row.thread_id, row.message_id) == (11, 12)


async def test_create_with_no_games_inserts_nothing(world):
    pool, a, _ga, _b, _gb = world
    assert await broadcast_repo.create_pending_broadcasts(
        pool, league_id=a, block_id=_BLOCK, game_ids=[]
    ) == 0


async def test_create_refuses_a_game_from_another_league(world):
    """League A asking for league B's games writes nothing -- no cross-league
    row can be created even though the FKs alone would allow it."""
    pool, a, games_a, b, games_b = world
    inserted = await broadcast_repo.create_pending_broadcasts(
        pool, league_id=a, block_id=_BLOCK, game_ids=[games_a[0], *games_b]
    )
    assert inserted == 1
    assert await pool.fetchval(
        "SELECT count(*) FROM game_broadcasts WHERE game_id = ANY($1::int[])", games_b
    ) == 0
    assert await _get(pool, b, games_b[0]) is None


async def test_the_unique_game_id_constraint_is_real(world):
    pool, a, games_a, _b, _gb = world
    await _pending(pool, a, games_a[:1])
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(
            "INSERT INTO game_broadcasts (league_id, game_id, block_id, status) "
            "VALUES ($1, $2, 'x', 'pending')",
            a, games_a[0],
        )


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


async def test_get_broadcast_is_league_scoped(world):
    pool, a, games_a, b, _gb = world
    await _pending(pool, a, games_a[:1])
    assert await _get(pool, a, games_a[0]) is not None
    # league B holding league A's game id sees nothing.
    assert await _get(pool, b, games_a[0]) is None
    # a game in no block at all.
    assert await _get(pool, a, games_a[1]) is None


async def test_list_for_block_filters_by_block_and_league_in_game_order(world):
    pool, a, games_a, b, games_b = world
    await _pending(pool, a, [games_a[2], games_a[0]], block="blk-1")
    await _pending(pool, a, [games_a[1]], block="blk-2")
    await _pending(pool, b, games_b, block="blk-1")
    rows = await broadcast_repo.list_broadcasts_for_block(pool, league_id=a, block_id="blk-1")
    assert [r.game_id for r in rows] == sorted([games_a[0], games_a[2]])
    assert {r.league_id for r in rows} == {a}
    other = await broadcast_repo.list_broadcasts_for_block(pool, league_id=b, block_id="blk-1")
    assert [r.game_id for r in other] == sorted(games_b)
    assert await broadcast_repo.list_broadcasts_for_block(
        pool, league_id=a, block_id="missing"
    ) == []


# ---------------------------------------------------------------------------
# root message, thread
# ---------------------------------------------------------------------------


async def test_set_root_message_writes_overwrites_and_is_league_scoped(world):
    pool, a, games_a, b, _gb = world
    game = games_a[0]
    await _pending(pool, a, [game])
    assert await broadcast_repo.set_root_message(
        pool, league_id=a, game_id=game, root_message_id=555
    ) == 1
    assert (await _get(pool, a, game)).root_message_id == 555
    assert await broadcast_repo.set_root_message(
        pool, league_id=a, game_id=game, root_message_id=556
    ) == 1
    # league B naming league A's game changes nothing.
    assert await broadcast_repo.set_root_message(
        pool, league_id=b, game_id=game, root_message_id=999
    ) == 0
    assert (await _get(pool, a, game)).root_message_id == 556
    # no row for a game in no block.
    assert await broadcast_repo.set_root_message(
        pool, league_id=a, game_id=games_a[1], root_message_id=1
    ) == 0


async def test_record_thread_moves_pending_to_queued_once(world):
    pool, a, games_a, b, _gb = world
    game = games_a[0]
    await _pending(pool, a, [game])
    # wrong league: no-op.
    assert await broadcast_repo.record_thread(
        pool, league_id=b, game_id=game, thread_id=1, message_id=2
    ) == 0
    assert (await _get(pool, a, game)).status is BroadcastStatus.PENDING
    assert await broadcast_repo.record_thread(
        pool, league_id=a, game_id=game, thread_id=70, message_id=71
    ) == 1
    row = await _get(pool, a, game)
    assert (row.status, row.thread_id, row.message_id) == (_Q, 70, 71)
    # a racing second /watch loses: nothing is overwritten.
    assert await broadcast_repo.record_thread(
        pool, league_id=a, game_id=game, thread_id=80, message_id=81
    ) == 0
    row = await _get(pool, a, game)
    assert (row.thread_id, row.message_id) == (70, 71)


async def test_record_thread_refuses_a_row_that_is_no_longer_pending(world):
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _queued(pool, a, game)
    await _walk(pool, a, game, _Q, _L)
    assert await broadcast_repo.record_thread(
        pool, league_id=a, game_id=game, thread_id=9, message_id=9
    ) == 0
    row = await _get(pool, a, game)
    assert (row.status, row.thread_id) == (_L, 1)


async def test_record_thread_on_a_missing_row_is_zero(world):
    pool, a, games_a, _b, _gb = world
    assert await broadcast_repo.record_thread(
        pool, league_id=a, game_id=games_a[0], thread_id=1, message_id=2
    ) == 0


# ---------------------------------------------------------------------------
# status transitions
# ---------------------------------------------------------------------------


async def test_the_happy_path_walks_queued_live_finished(world):
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _queued(pool, a, game)
    await _walk(pool, a, game, _Q, _L, _F)
    assert (await _get(pool, a, game)).status is _F


async def test_a_queued_or_live_row_can_be_interrupted(world):
    pool, a, games_a, _b, _gb = world
    await _queued(pool, a, games_a[0])
    await _walk(pool, a, games_a[0], _Q, _I)
    await _queued(pool, a, games_a[1])
    await _walk(pool, a, games_a[1], _Q, _L, _I)
    for g in games_a[:2]:
        assert (await _get(pool, a, g)).status is _I


@pytest.mark.parametrize(
    "expected,new",
    [
        (BroadcastStatus.PENDING, _L),
        (_Q, _F),
        (_L, _Q),
        (_F, _L),
        (_F, _I),
        (_I, _F),
    ],
)
async def test_an_illegal_transition_raises_and_writes_nothing(world, expected, new):
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _queued(pool, a, game)
    before = await _get(pool, a, game)
    with pytest.raises(ValidationError):
        await broadcast_repo.transition_status(
            pool, league_id=a, game_id=game, expected=expected, new=new
        )
    assert await _get(pool, a, game) == before


async def test_a_terminal_row_cannot_be_moved_by_a_legal_looking_cas(world):
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _queued(pool, a, game)
    await _walk(pool, a, game, _Q, _L, _F)
    # (live -> interrupted) is a legal pair, but the row is `finished`: the CAS
    # guard, not the legality table, must refuse it.
    assert await broadcast_repo.transition_status(
        pool, league_id=a, game_id=game, expected=_L, new=_I
    ) == 0
    assert (await _get(pool, a, game)).status is _F


async def test_a_stale_expected_status_is_a_zero_row_cas(world):
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _queued(pool, a, game)
    assert await broadcast_repo.transition_status(
        pool, league_id=a, game_id=game, expected=_Q, new=_L
    ) == 1
    # the same move again: someone (us) already made it.
    assert await broadcast_repo.transition_status(
        pool, league_id=a, game_id=game, expected=_Q, new=_L
    ) == 0


async def test_transition_status_is_league_scoped(world):
    pool, a, games_a, b, _gb = world
    game = games_a[0]
    await _queued(pool, a, game)
    assert await broadcast_repo.transition_status(
        pool, league_id=b, game_id=game, expected=_Q, new=_L
    ) == 0
    assert (await _get(pool, a, game)).status is _Q


# ---------------------------------------------------------------------------
# the two-phase reveal CAS
# ---------------------------------------------------------------------------


async def test_the_first_claim_wins_and_the_second_returns_nothing(world):
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _pending(pool, a, [game])
    row_id = (await _get(pool, a, game)).id
    first = await broadcast_repo.claim_reveal(pool, league_id=a, game_id=game)
    second = await broadcast_repo.claim_reveal(pool, league_id=a, game_id=game)
    assert first == row_id
    assert second is None
    assert (await _get(pool, a, game)).reveal_claimed_at is not None


async def test_a_failed_second_claim_does_not_move_the_first_claims_timestamp(world):
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _pending(pool, a, [game])
    await broadcast_repo.claim_reveal(pool, league_id=a, game_id=game)
    stamp = (await _get(pool, a, game)).reveal_claimed_at
    assert await broadcast_repo.claim_reveal(pool, league_id=a, game_id=game) is None
    assert (await _get(pool, a, game)).reveal_claimed_at == stamp


async def test_a_claimed_but_unposted_row_is_a_crashed_reveal(world):
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _pending(pool, a, [game])
    await broadcast_repo.claim_reveal(pool, league_id=a, game_id=game)
    row = await _get(pool, a, game)
    assert row.reveal_claimed_at is not None
    assert row.reveal_posted_at is None
    assert not row.is_revealed
    assert row.sweep_shape is SweepShape.CRASHED_REVEAL


async def test_mark_posted_completes_the_reveal_exactly_once(world):
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _pending(pool, a, [game])
    row_id = await broadcast_repo.claim_reveal(pool, league_id=a, game_id=game)
    assert await broadcast_repo.mark_reveal_posted(pool, league_id=a, broadcast_id=row_id) == 1
    row = await _get(pool, a, game)
    assert row.reveal_posted_at is not None and row.is_revealed
    assert row.sweep_shape is None
    stamp = row.reveal_posted_at
    # a second mark is a no-op and does not move the timestamp.
    assert await broadcast_repo.mark_reveal_posted(pool, league_id=a, broadcast_id=row_id) == 0
    assert (await _get(pool, a, game)).reveal_posted_at == stamp
    # and a posted reveal cannot be claimed again.
    assert await broadcast_repo.claim_reveal(pool, league_id=a, game_id=game) is None


async def test_an_unclaimed_row_cannot_be_marked_posted(world):
    """Skipping the claim would defeat the CAS: `reveal_posted_at` may only
    follow `reveal_claimed_at`."""
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _pending(pool, a, [game])
    row_id = (await _get(pool, a, game)).id
    assert await broadcast_repo.mark_reveal_posted(pool, league_id=a, broadcast_id=row_id) == 0
    row = await _get(pool, a, game)
    assert row.reveal_posted_at is None and row.reveal_claimed_at is None


async def test_the_reveal_cas_is_league_scoped(world):
    pool, a, games_a, b, _gb = world
    game = games_a[0]
    await _pending(pool, a, [game])
    row_id = (await _get(pool, a, game)).id
    # league B cannot claim or mark league A's row.
    assert await broadcast_repo.claim_reveal(pool, league_id=b, game_id=game) is None
    assert await broadcast_repo.claim_reveal(pool, league_id=a, game_id=game) == row_id
    assert await broadcast_repo.mark_reveal_posted(pool, league_id=b, broadcast_id=row_id) == 0
    row = await _get(pool, a, game)
    assert row.reveal_posted_at is None
    assert row.reveal_claimed_at is not None


async def test_claim_on_a_game_in_no_block_returns_nothing(world):
    pool, a, games_a, _b, _gb = world
    assert await broadcast_repo.claim_reveal(pool, league_id=a, game_id=games_a[0]) is None


@pytest.mark.parametrize(
    "path",
    [
        [],
        [_Q],
        [_Q, _L],
        [_Q, _L, _F],
        [_Q, _L, _I],
    ],
    ids=["pending", "queued", "live", "finished", "interrupted"],
)
async def test_claim_works_from_every_status(world, path):
    """The CAS is on `reveal_claimed_at` alone: a game nobody watched
    (`pending`), a finished broadcast and an interrupted one reveal alike."""
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _pending(pool, a, [game])
    if path:
        assert await broadcast_repo.record_thread(
            pool, league_id=a, game_id=game, thread_id=1, message_id=2
        ) == 1
        await _walk(pool, a, game, *path)
    assert await broadcast_repo.claim_reveal(pool, league_id=a, game_id=game) is not None
    assert await broadcast_repo.claim_reveal(pool, league_id=a, game_id=game) is None


async def test_concurrent_claims_have_exactly_one_winner(world):
    """The CAS under real contention: ten claimers race for one row on
    separate pool connections; Postgres serialises the row update."""
    pool, a, games_a, _b, _gb = world
    game = games_a[0]
    await _pending(pool, a, [game])
    results = await asyncio.gather(
        *[broadcast_repo.claim_reveal(pool, league_id=a, game_id=game) for _ in range(10)]
    )
    assert sum(r is not None for r in results) == 1


# ---------------------------------------------------------------------------
# the startup sweep's selection
# ---------------------------------------------------------------------------


async def _seed_every_shape(pool, league_id, games):
    """Six rows: an interrupted broadcast (`live`), another (`queued`), a
    crashed reveal, an orphaned `pending`, an unrevealed-ended, and one fully
    revealed (which must NOT be selected). Returns {name: game_id}."""
    live, queued, crashed, orphan, ended, done = games
    await _pending(pool, league_id, games)
    for g, path in (
        (live, [_Q, _L]),
        (queued, [_Q]),
        (ended, [_Q, _L, _F]),
        (done, [_Q, _L, _F]),
    ):
        assert await broadcast_repo.record_thread(
            pool, league_id=league_id, game_id=g, thread_id=g, message_id=g
        ) == 1
        await _walk(pool, league_id, g, *path)
    await broadcast_repo.claim_reveal(pool, league_id=league_id, game_id=crashed)
    done_id = await broadcast_repo.claim_reveal(pool, league_id=league_id, game_id=done)
    await broadcast_repo.mark_reveal_posted(pool, league_id=league_id, broadcast_id=done_id)
    return {"live": live, "queued": queued, "crashed": crashed, "orphan": orphan,
            "ended": ended, "done": done}


async def test_the_sweep_selects_each_drain_shape_and_only_those(big_world):
    pool, a, games_a, b, games_b = big_world
    ids = await _seed_every_shape(pool, a, games_a)
    await _pending(pool, b, games_b)  # league B: two orphaned pendings
    rows = await broadcast_repo.list_sweep_candidates(pool, league_id=a)
    by_game = {r.game_id: r for r in rows}
    # the revealed game is NOT a candidate.
    assert ids["done"] not in by_game
    assert set(by_game) == {ids[k] for k in ("live", "queued", "crashed", "orphan", "ended")}
    assert by_game[ids["live"]].sweep_shape is SweepShape.INTERRUPTED_BROADCAST
    assert by_game[ids["queued"]].sweep_shape is SweepShape.INTERRUPTED_BROADCAST
    assert by_game[ids["crashed"]].sweep_shape is SweepShape.CRASHED_REVEAL
    assert by_game[ids["orphan"]].sweep_shape is SweepShape.ORPHANED_PENDING
    assert by_game[ids["ended"]].sweep_shape is SweepShape.UNREVEALED_ENDED


async def test_the_sweep_returns_oldest_first(big_world):
    pool, a, games_a, _b, _gb = big_world
    await _seed_every_shape(pool, a, games_a)
    rows = await broadcast_repo.list_sweep_candidates(pool, league_id=a)
    assert [r.id for r in rows] == sorted(r.id for r in rows)


async def test_the_sweep_is_league_scoped(big_world):
    pool, a, games_a, b, games_b = big_world
    ids = await _seed_every_shape(pool, a, games_a)
    await _pending(pool, b, games_b)
    rows_b = await broadcast_repo.list_sweep_candidates(pool, league_id=b)
    assert {r.game_id for r in rows_b} == set(games_b)
    assert {r.league_id for r in rows_b} == {b}
    rows_a = await broadcast_repo.list_sweep_candidates(pool, league_id=a)
    assert {r.league_id for r in rows_a} == {a}
    assert not {r.game_id for r in rows_a} & set(games_b)
    assert ids["orphan"] in {r.game_id for r in rows_a}


async def test_the_sweep_is_empty_for_a_league_with_no_rows(world):
    pool, a, _ga, _b, _gb = world
    assert await broadcast_repo.list_sweep_candidates(pool, league_id=a) == []


async def test_draining_a_sweep_row_removes_it_from_the_next_sweep(big_world):
    """The selection converges: claim + mark-posted takes a row out."""
    pool, a, games_a, _b, _gb = big_world
    ids = await _seed_every_shape(pool, a, games_a)
    before = await broadcast_repo.list_sweep_candidates(pool, league_id=a)
    row_id = await broadcast_repo.claim_reveal(pool, league_id=a, game_id=ids["orphan"])
    await broadcast_repo.mark_reveal_posted(pool, league_id=a, broadcast_id=row_id)
    after = await broadcast_repo.list_sweep_candidates(pool, league_id=a)
    assert {r.game_id for r in before} - {r.game_id for r in after} == {ids["orphan"]}


# ---------------------------------------------------------------------------
# schema facts the design states
# ---------------------------------------------------------------------------


async def test_the_sweep_index_is_partial_and_matches_the_drain_predicate(world):
    pool, _a, _ga, _b, _gb = world
    indexdef = await pool.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'game_broadcasts' "
        "AND indexname = 'ix_game_broadcasts_sweep'"
    )
    assert indexdef is not None
    assert "(league_id, status)" in indexdef
    assert "reveal_posted_at IS NULL" in indexdef


async def test_deleting_a_game_cascades_its_broadcast_row(world):
    pool, a, games_a, _b, _gb = world
    await _pending(pool, a, games_a[:1])
    await pool.execute("DELETE FROM games WHERE id = $1", games_a[0])
    assert await _get(pool, a, games_a[0]) is None


async def test_deleting_a_league_cascades_its_broadcast_rows(world):
    pool, a, games_a, _b, _gb = world
    await _pending(pool, a, games_a)
    await pool.execute("DELETE FROM leagues WHERE id = $1", a)
    assert await pool.fetchval(
        "SELECT count(*) FROM game_broadcasts WHERE game_id = ANY($1::int[])", games_a
    ) == 0
