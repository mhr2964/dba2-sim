"""game_broadcasts -- `broadcast_repo`'s one table (migration 019; see
`dba2/domain/broadcast.py` for the row shape and the status vocabulary).

docs/design/phase-5-stage-d-broadcast.md §8, P5-97.m/.q/.v/.w. Called directly
from the bot layer (`bot/game_feed.py`, `bot/cogs/broadcast.py`,
`bot/broadcast.py`) with no services-layer wrapper, the same shape as
`channel_repo` -- this is Discord-side bookkeeping, not a business rule. No
Discord call ever happens in here, and none may happen inside a transaction
that calls in here (P5-14): every function is a single statement that commits
on its own when handed a `Pool`, and the reveal is deliberately three separate
statements (`claim_reveal`, the Discord post, `mark_reveal_posted`), never one
transaction around a post.

**Every function takes `league_id` and filters on it.** `game_id` is globally
unique, so the league predicate is not needed to find a row -- it is here so a
caller holding league A's context can never read or change league B's row by
passing a game id from the wrong league. This table has no roster writes, so
none of the RMLP tier locks apply (`trade_offers`'s docstring explains the
tiers; `game_broadcasts` sits outside them).

The write functions return the affected-row count (or the row id for the
reveal claim, matching the design's `RETURNING id`): `0` means the guard did
not match -- someone else moved the row first, the row is not in this league,
or the requested move is not one this row can make -- and NOTHING was
written. The caller decides whether that is "report the current state" or
"stop".
"""
from __future__ import annotations

from collections.abc import Sequence

import asyncpg

from dba2.core.errors import ValidationError
from dba2.data.db import Executor
from dba2.domain.broadcast import (
    BroadcastStatus,
    GameBroadcast,
    is_legal_broadcast_transition,
)

_BROADCAST_COLUMNS = """
    id, league_id, game_id, block_id, root_message_id, thread_id, message_id,
    status, reveal_claimed_at, reveal_posted_at, created_at
"""


def _map_broadcast(r: asyncpg.Record) -> GameBroadcast:
    return GameBroadcast(
        id=r["id"],
        league_id=r["league_id"],
        game_id=r["game_id"],
        block_id=r["block_id"],
        root_message_id=r["root_message_id"],
        thread_id=r["thread_id"],
        message_id=r["message_id"],
        status=BroadcastStatus(r["status"]),
        reveal_claimed_at=r["reveal_claimed_at"],
        reveal_posted_at=r["reveal_posted_at"],
        created_at=r["created_at"],
    )


def _affected(command_tag: str) -> int:
    return int(command_tag.rsplit(" ", 1)[-1])


async def create_pending_broadcasts(
    ex: Executor, *, league_id: int, block_id: str, game_ids: Sequence[int]
) -> int:
    """One `status = 'pending'` row per game in `game_ids`, and the count
    actually inserted (M10). `ON CONFLICT (game_id) DO NOTHING`: a re-run
    `/advance`, a retried `open_block` or a re-registered block neither fails
    nor duplicates, and a second identical call returns `0`. An already-present
    row is left exactly as it is -- in particular its `block_id` and status are
    NOT overwritten, so re-opening cannot rewind a broadcast that has begun.

    **A game that does not belong to `league_id` is silently not inserted**
    (the rows come from a `SELECT ... FROM games WHERE league_id = $1`), so a
    cross-league row cannot exist -- nothing else in the schema keeps
    `game_broadcasts.league_id` equal to `games.league_id`. The consequence is
    that a returned count below `len(set(game_ids))` means "already present OR
    not in this league", and the caller cannot tell which from the count
    alone; `open_block` only ever passes its own league's games, so for it the
    shortfall is always "already present". An empty `game_ids` returns `0`
    without touching the database."""
    if not game_ids:
        return 0
    rows = await ex.fetch(
        """
        INSERT INTO game_broadcasts (league_id, game_id, block_id, status)
        SELECT $1::bigint, g.id, $2::text, $4::text
        FROM games g
        WHERE g.league_id = $1 AND g.id = ANY($3::int[])
        ORDER BY g.id
        ON CONFLICT (game_id) DO NOTHING
        RETURNING id
        """,
        league_id,
        block_id,
        list(game_ids),
        BroadcastStatus.PENDING.value,
    )
    return len(rows)


async def get_broadcast(
    ex: Executor, *, league_id: int, game_id: int
) -> GameBroadcast | None:
    """The row `/watch`'s state machine is keyed on (P5-97.w) and the one the
    reveal, the pacer's finalizer and the sweep re-read. `None` for a game in
    no block -- and for a game id that belongs to another league."""
    row = await ex.fetchrow(
        f"SELECT {_BROADCAST_COLUMNS} FROM game_broadcasts "
        "WHERE league_id = $1 AND game_id = $2",
        league_id,
        game_id,
    )
    return _map_broadcast(row) if row is not None else None


async def list_broadcasts_for_block(
    ex: Executor, *, league_id: int, block_id: str
) -> list[GameBroadcast]:
    """Every row of one block, `game_id` order -- what `open_block`/
    `close_block` walk to reveal "every game of block N that has no live or
    queued broadcast" (P5-97.m), and what the slate post walks to persist each
    root message id. The caller filters on `status` and the reveal columns;
    the reveal CAS makes a redundant attempt harmless. No index backs this
    (`ix_game_broadcasts_sweep` is partial on `reveal_posted_at IS NULL`, so
    it cannot serve a fully-revealed block): a league holds at most a few
    hundred rows per season and a block is read at most once per `/advance`."""
    rows = await ex.fetch(
        f"""
        SELECT {_BROADCAST_COLUMNS} FROM game_broadcasts
        WHERE league_id = $1 AND block_id = $2
        ORDER BY game_id
        """,
        league_id,
        block_id,
    )
    return [_map_broadcast(r) for r in rows]


async def set_root_message(
    ex: Executor, *, league_id: int, game_id: int, root_message_id: int
) -> int:
    """Write-back of a game's `#games` line, one small write per post
    (P5-97.v -- deliberately not one transaction around N Discord posts).
    Overwrites unconditionally, like `offer_repo.set_offer_message`: a
    duplicate root line left by a crash between the post and this write is
    cosmetic, and the newer id is the one the reveal should edit."""
    result = await ex.execute(
        """
        UPDATE game_broadcasts SET root_message_id = $3
        WHERE league_id = $1 AND game_id = $2
        """,
        league_id,
        game_id,
        root_message_id,
    )
    return _affected(result)


async def record_thread(
    ex: Executor, *, league_id: int, game_id: int, thread_id: int, message_id: int
) -> int:
    """`/watch`'s persist step (P5-97.w step 6): store the thread and its
    placeholder message and move `pending` -> `queued` in ONE statement,
    guarded by `status = 'pending' AND thread_id IS NULL`. Returns `1` for the
    caller that recorded it, `0` when the row already has a thread (a second
    `/watch` raced this one) or is not `pending` -- the loser then holds a
    thread nothing points at and should link the winner's instead of
    spawning. Persisting the thread id and the status together is the point:
    a row can never be `queued` without a thread, nor hold a thread while
    still `pending`."""
    result = await ex.execute(
        """
        UPDATE game_broadcasts
        SET thread_id = $3, message_id = $4, status = $5
        WHERE league_id = $1 AND game_id = $2 AND status = $6 AND thread_id IS NULL
        """,
        league_id,
        game_id,
        thread_id,
        message_id,
        BroadcastStatus.QUEUED.value,
        BroadcastStatus.PENDING.value,
    )
    return _affected(result)


async def transition_status(
    ex: Executor,
    *,
    league_id: int,
    game_id: int,
    expected: BroadcastStatus,
    new: BroadcastStatus,
) -> int:
    """Compare-and-swap `status` from `expected` to `new`. Returns `1` when
    this caller made the move, `0` when the row was no longer in `expected`
    (or is not in this league) and NOTHING was written.

    **An illegal `(expected, new)` pair raises `ValidationError` before any
    SQL runs** -- `BROADCAST_TRANSITIONS` (`domain/broadcast.py`) is the one
    definition of legal. That is a programming error, distinct from losing a
    race (which is the `0` return). `pending -> queued` is deliberately not
    refused here but has its own writer, `record_thread`, because that move
    must carry the thread ids with it; using this function for it is legal
    and would leave a `queued` row with no thread, so the bot layer must not."""
    if not is_legal_broadcast_transition(expected, new):
        raise ValidationError(
            f"illegal broadcast status transition {expected.value!r} -> {new.value!r}"
        )
    result = await ex.execute(
        """
        UPDATE game_broadcasts SET status = $4
        WHERE league_id = $1 AND game_id = $2 AND status = $3
        """,
        league_id,
        game_id,
        expected.value,
        new.value,
    )
    return _affected(result)


async def claim_reveal(ex: Executor, *, league_id: int, game_id: int) -> int | None:
    """Phase one of the two-phase reveal CAS (P5-97.m, M4): stamp
    `reveal_claimed_at = NOW()` iff it is still `NULL`, and return the row id.
    `None` means the row is absent, in another league, or someone else already
    owns this reveal -- the caller posts NOTHING.

    The `AND reveal_claimed_at IS NULL` guard is the whole mutual exclusion:
    the winner posts to Discord and then calls `mark_reveal_posted`; every
    other caller (the broadcast finalizer, `close_block`, `sweep_league`, in
    any order) gets `None`. It does not care about `status`, so it works the
    same on a game nobody watched, a finished broadcast and an interrupted
    one. A row this function left claimed-but-unposted is the crashed-reveal
    shape the sweep recovers (`SweepShape.CRASHED_REVEAL`) -- by posting and
    calling `mark_reveal_posted` directly, NOT by claiming again."""
    return await ex.fetchval(
        """
        UPDATE game_broadcasts SET reveal_claimed_at = NOW()
        WHERE league_id = $1 AND game_id = $2 AND reveal_claimed_at IS NULL
        RETURNING id
        """,
        league_id,
        game_id,
    )


async def mark_reveal_posted(ex: Executor, *, league_id: int, broadcast_id: int) -> int:
    """Phase two: the result line reached `#games`. Keyed on the id
    `claim_reveal` returned (the design's `WHERE id = $1`), plus the league.
    Guarded by `reveal_claimed_at IS NOT NULL AND reveal_posted_at IS NULL`:
    a row nobody claimed cannot be marked posted (that would skip the CAS),
    and a second mark is `0` and does not move the timestamp. Returns the
    affected-row count."""
    result = await ex.execute(
        """
        UPDATE game_broadcasts SET reveal_posted_at = NOW()
        WHERE league_id = $1 AND id = $2
          AND reveal_claimed_at IS NOT NULL AND reveal_posted_at IS NULL
        """,
        league_id,
        broadcast_id,
    )
    return _affected(result)


async def list_sweep_candidates(ex: Executor, *, league_id: int) -> list[GameBroadcast]:
    """Every row of `league_id` whose result has not been published --
    `reveal_posted_at IS NULL`, exactly `ix_game_broadcasts_sweep`'s partial
    predicate -- oldest first. This is the startup sweep's whole input
    (P5-97.q item 6); each row's `sweep_shape` says what to do with it:

    * `live`/`queued` -> `INTERRUPTED_BROADCAST` (the process died mid-broadcast)
    * claimed, unposted -> `CRASHED_REVEAL`
    * `pending`, unclaimed -> `ORPHANED_PENDING`
    * `finished`/`interrupted`, unclaimed -> `UNREVEALED_ENDED`

    One query and a classifier rather than four queries, so the shapes cannot
    overlap or leave a gap between them by construction. The fourth shape is
    not in the design's three-item list; see `SweepShape.UNREVEALED_ENDED` for
    why the design's own reveal table requires it. Safe to call only at
    startup, when memory holds no block and no live task owns any of these
    rows."""
    rows = await ex.fetch(
        f"""
        SELECT {_BROADCAST_COLUMNS} FROM game_broadcasts
        WHERE league_id = $1 AND reveal_posted_at IS NULL
        ORDER BY id
        """,
        league_id,
    )
    return [_map_broadcast(r) for r in rows]
