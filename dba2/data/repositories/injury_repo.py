"""injuries — `injury_repo`'s table (§5).

phase-0-foundation.md §4.7. `injury_type`/`severity` stay plain `str` at
this layer too, matching `domain.injury.Injury` — the doc doesn't enumerate
their value sets, so there's no TEXT-backed enum to construct here.
"""
from __future__ import annotations

import asyncpg

from dba2.data.db import Executor
from dba2.domain.injury import Injury

_INJURY_COLUMNS = """
    id, league_id, player_id, game_id, injury_type, severity,
    games_remaining, occurred_in_season, resolved_at
"""


def _map_injury(r: asyncpg.Record) -> Injury:
    return Injury(
        id=r["id"],
        league_id=r["league_id"],
        player_id=r["player_id"],
        game_id=r["game_id"],
        injury_type=r["injury_type"],
        severity=r["severity"],
        games_remaining=r["games_remaining"],
        occurred_in_season=r["occurred_in_season"],
        resolved_at=r["resolved_at"],
    )


async def create_injury(
    ex: Executor,
    *,
    league_id: int,
    player_id: int,
    injury_type: str,
    severity: str,
    occurred_in_season: int,
    game_id: int | None = None,
    games_remaining: int | None = None,
) -> Injury:
    row = await ex.fetchrow(
        f"""
        INSERT INTO injuries
            (league_id, player_id, game_id, injury_type, severity,
             games_remaining, occurred_in_season)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING {_INJURY_COLUMNS}
        """,
        league_id,
        player_id,
        game_id,
        injury_type,
        severity,
        games_remaining,
        occurred_in_season,
    )
    return _map_injury(row)


async def get_injury(ex: Executor, injury_id: int) -> Injury | None:
    row = await ex.fetchrow(
        f"SELECT {_INJURY_COLUMNS} FROM injuries WHERE id = $1", injury_id
    )
    return _map_injury(row) if row is not None else None


async def list_injuries_for_player(ex: Executor, player_id: int) -> list[Injury]:
    rows = await ex.fetch(
        f"""
        SELECT {_INJURY_COLUMNS} FROM injuries
        WHERE player_id = $1
        ORDER BY occurred_in_season, id
        """,
        player_id,
    )
    return [_map_injury(r) for r in rows]


async def resolve_injury(ex: Executor, injury_id: int) -> None:
    """Sets `resolved_at = NOW()` on an unresolved (`resolved_at IS NULL`)
    injury.

    A no-op on an injury that's already resolved — the
    `AND resolved_at IS NULL` guard is load-bearing, not decorative: without
    it, a second call would silently overwrite the true resolution
    timestamp on this one-time fact with no error, the same bug class fixed
    in `team_repo.close_manager_assignment` / `draft_repo.complete_lottery`
    / `contract_repo.terminate_contract` (Phase 0 step-7 critic round, B1)."""
    await ex.execute(
        "UPDATE injuries SET resolved_at = NOW() WHERE id = $1 AND resolved_at IS NULL",
        injury_id,
    )
