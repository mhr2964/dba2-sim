"""`recompute_overall` / `recompute_overall_for_league` -- Decision 12's
single writer for `players.overall_cached` (phase-0-foundation.md §4.3.1,
Decision 12, §7.4) -- plus `derive_tendencies_for_player` /
`derive_tendencies_for_league` (docs/design/phase-1-step-7-calibration.md
§11.3), the write-path wrappers around `sim.derivation.derive_tendencies`
so `player_tendencies` gets the same "one pure function, one writer" shape
Decision 12 already established for `overall_cached`.

"one pure function in `sim/ratings.py` computes it position-weighted from
`player_ratings`, exactly one writer (`ratings_service.recompute_overall`)
persists it, the column name says 'cached,' and a test recomputes every
player's overall and asserts equality with the stored value." --
`player_repo.set_overall_cached` is the write primitive (already built,
step 7); this module is the only caller meant to reach it under normal
operation. Nothing else in the repo or service layer should call
`set_overall_cached` directly.
"""
from __future__ import annotations

from datetime import UTC, datetime
from random import Random

from dba2.core.errors import NotFoundError
from dba2.core.rng import rng_for
from dba2.data.db import Executor, transaction
from dba2.data.repositories import league_repo, player_repo, ratings_repo
from dba2.domain.player import Position
from dba2.domain.ratings import PlayerRatings, PlayerTendencies
from dba2.sim.derivation import derive_tendencies
from dba2.sim.ratings import compute_overall


async def _persist_recomputed_overall(
    ex: Executor, ratings: PlayerRatings, position: Position, computed_at: datetime
) -> int:
    """Shared compute-then-write step for both entry points below, so the
    "compute via sim.ratings, write via player_repo" pairing exists in
    exactly one place rather than being duplicated between the single- and
    league-scoped callers (this package's recurring "duplicate logic under
    two names" bug class)."""
    overall = compute_overall(ratings, position)
    await player_repo.set_overall_cached(ex, ratings.player_id, overall, computed_at)
    return overall


async def recompute_overall(
    ex: Executor, player_id: int, *, computed_at: datetime | None = None
) -> int:
    """Fetches `player_id`'s `PlayerRatings` and `Player.primary_position`,
    recomputes `overall_cached` via `sim.ratings.compute_overall`, persists
    it via `player_repo.set_overall_cached`, and returns the computed
    value. `compute_overall` is position-weighted (Phase 1 step 9) and so
    needs `primary_position` -- fetched here the same way
    `derive_tendencies_for_player` below already fetches it, rather than
    `compute_overall` itself doing any I/O.

    Raises `NotFoundError` if the player has no `player_ratings` row, or no
    `players` row at all -- `player_ratings`'s 55 rating columns are all
    `NOT NULL` (§4.3.2), so there is no legal "empty" overall to fall back
    to; this mirrors every other lookup-by-id function in this codebase's
    not-found convention rather than writing a meaningless default.
    """
    ratings = await ratings_repo.get_player_ratings(ex, player_id)
    if ratings is None:
        raise NotFoundError(f"Player {player_id} has no player_ratings row")

    player = await player_repo.get_player(ex, player_id)
    if player is None:
        raise NotFoundError(f"Player {player_id} has no players row")

    return await _persist_recomputed_overall(
        ex,
        ratings,
        player.primary_position,
        computed_at if computed_at is not None else datetime.now(UTC),
    )


async def recompute_overall_for_league(ex: Executor, league_id: int) -> None:
    """Recomputes and persists `overall_cached` for every player in
    `league_id`, via `ratings_repo.get_for_league`'s bulk loader (§5's
    stated repo contract -- avoids an N+1 of per-player reads) plus
    `player_repo.list_players_for_league` for each player's
    `primary_position` (the same position-lookup pairing
    `derive_tendencies_for_league` below already uses), all inside one
    `transaction(ex)` block so a mid-batch failure can't leave some
    players' caches recomputed against a timestamp their siblings never
    got. This is what a dev tool (`tools/seed_dev_league.py`) or a future
    admin command actually calls, rather than looping `recompute_overall`
    itself with no shared atomicity boundary.
    """
    async with transaction(ex) as conn:
        all_ratings = await ratings_repo.get_for_league(conn, league_id)
        players = await player_repo.list_players_for_league(conn, league_id)
        position_by_player = {p.id: p.primary_position for p in players}
        computed_at = datetime.now(UTC)
        for ratings in all_ratings:
            position = position_by_player.get(ratings.player_id)
            if position is None:
                raise NotFoundError(
                    f"Player {ratings.player_id} has a player_ratings row "
                    f"but no players row in league {league_id}"
                )
            await _persist_recomputed_overall(conn, ratings, position, computed_at)


async def _persist_derived_tendencies(
    ex: Executor, ratings: PlayerRatings, position: Position, rng: Random | None
) -> PlayerTendencies:
    """Shared compute-then-write step for both entry points below --
    mirrors `_persist_recomputed_overall`'s pairing above (§8.1's pure
    `derive_tendencies` call, then `ratings_repo.upsert_player_tendencies`
    writes it)."""
    values = derive_tendencies(ratings, position, rng=rng)
    return await ratings_repo.upsert_player_tendencies(
        ex, player_id=ratings.player_id, league_id=ratings.league_id, **values
    )


async def derive_tendencies_for_player(
    ex: Executor, player_id: int, *, rng: Random | None = None
) -> PlayerTendencies:
    """Fetches `player_id`'s `PlayerRatings` and `Player.primary_position`,
    calls `sim.derivation.derive_tendencies`, persists the 17-field result
    via `ratings_repo.upsert_player_tendencies`, and returns the row.

    Raises `NotFoundError` if the player has no `player_ratings` row or no
    `players` row at all -- mirrors `recompute_overall`'s not-found
    convention (§11.3): tendencies are derived *from* ratings, so a
    missing ratings row is the same "nothing to derive from" case.
    """
    ratings = await ratings_repo.get_player_ratings(ex, player_id)
    if ratings is None:
        raise NotFoundError(f"Player {player_id} has no player_ratings row")

    player = await player_repo.get_player(ex, player_id)
    if player is None:
        raise NotFoundError(f"Player {player_id} has no players row")

    return await _persist_derived_tendencies(ex, ratings, player.primary_position, rng)


async def derive_tendencies_for_league(ex: Executor, league_id: int) -> int:
    """Derives and persists `player_tendencies` for every player in
    `league_id`, inside one `transaction(ex)` block -- the same
    single/bulk pairing `recompute_overall`/`recompute_overall_for_league`
    already established (§11.3). This is the bulk re-derive path for an
    already-persisted league (e.g. a future admin command) -- the seed
    tool generates a brand-new league and calls the single-player
    `derive_tendencies_for_player` per player instead (critic round 18,
    B18-1: this docstring previously claimed the seed tool calls this
    function, which stopped being true when round 11 rewired the tool).

    Per-player noise comes from `core.rng.rng_for(league.rng_seed,
    "tendencies", player_id)` (Decision P1-19 item 2, Decision P1-21's
    derivation-chain extension to a generation stream) -- never a bare
    `Random()`. Returns the number of rows written.
    """
    async with transaction(ex) as conn:
        league = await league_repo.get_league(conn, league_id)
        if league is None:
            raise NotFoundError(f"League {league_id} does not exist")

        all_ratings = await ratings_repo.get_for_league(conn, league_id)
        players = await player_repo.list_players_for_league(conn, league_id)
        position_by_player = {p.id: p.primary_position for p in players}

        rows_written = 0
        for ratings in all_ratings:
            position = position_by_player.get(ratings.player_id)
            if position is None:
                raise NotFoundError(
                    f"Player {ratings.player_id} has a player_ratings row "
                    f"but no players row in league {league_id}"
                )
            rng = rng_for(league.rng_seed, "tendencies", ratings.player_id)
            await _persist_derived_tendencies(conn, ratings, position, rng)
            rows_written += 1
        return rows_written
