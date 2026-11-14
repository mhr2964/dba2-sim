"""player_awards — `award_repo`'s table (§5). Decision 20: a Phase 0 table
because it's the supermax-eligibility input, not a Phase 4 concern, even
though Pillar 4's media layer later reads the same table for free.

phase-0-foundation.md §4.4.

`create_award` is a plain INSERT, not an upsert — the migration's
UNIQUE (league_id, player_id, season, award_type) exists to catch a
duplicate-award bug at the database, not to be silently absorbed by an
ON CONFLICT DO UPDATE. A second insert for the same honor in the same
season is a caller bug and should raise (asyncpg's
`UniqueViolationError`), the same way this package lets a bad TEXT-enum
value raise uncaught rather than being caught and re-shaped.
"""
from __future__ import annotations

from decimal import Decimal

import asyncpg

from dba2.data.db import Executor
from dba2.domain.award import AwardType, PlayerAward

_PLAYER_AWARD_COLUMNS = "id, league_id, player_id, season, award_type, place, vote_share"


def _map_player_award(r: asyncpg.Record) -> PlayerAward:
    return PlayerAward(
        id=r["id"],
        league_id=r["league_id"],
        player_id=r["player_id"],
        season=r["season"],
        award_type=AwardType(r["award_type"]),
        place=r["place"],
        vote_share=r["vote_share"],
    )


async def create_award(
    ex: Executor,
    *,
    league_id: int,
    player_id: int,
    season: int,
    award_type: AwardType,
    place: int | None = None,
    vote_share: Decimal | None = None,
) -> PlayerAward:
    row = await ex.fetchrow(
        f"""
        INSERT INTO player_awards (league_id, player_id, season, award_type, place, vote_share)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING {_PLAYER_AWARD_COLUMNS}
        """,
        league_id,
        player_id,
        season,
        award_type.value,
        place,
        vote_share,
    )
    return _map_player_award(row)


async def get_award(ex: Executor, award_id: int) -> PlayerAward | None:
    row = await ex.fetchrow(
        f"SELECT {_PLAYER_AWARD_COLUMNS} FROM player_awards WHERE id = $1", award_id
    )
    return _map_player_award(row) if row is not None else None


async def list_awards_for_player(
    ex: Executor, player_id: int, *, award_type: AwardType | None = None
) -> list[PlayerAward]:
    """The supermax-eligibility read path (§4.4: MVP in any of the last
    three seasons; DPOY or All-NBA in the prior season or two of the prior
    three) — callers filter the returned list by `season`/`award_type`
    themselves, since a player's award history is small enough that a
    second SQL predicate buys nothing a Python filter doesn't already give
    for free."""
    if award_type is None:
        rows = await ex.fetch(
            f"""
            SELECT {_PLAYER_AWARD_COLUMNS} FROM player_awards
            WHERE player_id = $1
            ORDER BY season
            """,
            player_id,
        )
    else:
        rows = await ex.fetch(
            f"""
            SELECT {_PLAYER_AWARD_COLUMNS} FROM player_awards
            WHERE player_id = $1 AND award_type = $2
            ORDER BY season
            """,
            player_id,
            award_type.value,
        )
    return [_map_player_award(r) for r in rows]


async def list_awards_for_league_season(
    ex: Executor, league_id: int, season: int, *, award_type: AwardType | None = None
) -> list[PlayerAward]:
    if award_type is None:
        rows = await ex.fetch(
            f"""
            SELECT {_PLAYER_AWARD_COLUMNS} FROM player_awards
            WHERE league_id = $1 AND season = $2
            """,
            league_id,
            season,
        )
    else:
        rows = await ex.fetch(
            f"""
            SELECT {_PLAYER_AWARD_COLUMNS} FROM player_awards
            WHERE league_id = $1 AND season = $2 AND award_type = $3
            """,
            league_id,
            season,
            award_type.value,
        )
    return [_map_player_award(r) for r in rows]
