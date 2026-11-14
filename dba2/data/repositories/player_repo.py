"""players — `player_repo`'s table, and only that table (§5: "no contracts,
no stats, no records").

phase-0-foundation.md §4.3.1. Every query in this module touches `players`
alone (join columns like `team_id` are read, never used to join out to
`teams`/`contracts`/stats tables here) — the scope-creep guard the design
doc flags as a known risk (§9).
"""
from __future__ import annotations

from datetime import date, datetime

import asyncpg

from dba2.data.db import Executor
from dba2.domain.player import Player, Position, RosterStatus

_PLAYER_COLUMNS = """
    id, league_id, first_name, last_name, external_ref, primary_position,
    secondary_position, height_in, weight_lb, wingspan_in, birth_date,
    draft_year, draft_pick_number, years_pro, team_id, roster_status,
    overall_cached, overall_computed_at, retired_in_season, created_at,
    updated_at
"""


def _map_player(r: asyncpg.Record) -> Player:
    return Player(
        id=r["id"],
        league_id=r["league_id"],
        first_name=r["first_name"],
        last_name=r["last_name"],
        external_ref=r["external_ref"],
        primary_position=Position(r["primary_position"]),
        secondary_position=(
            Position(r["secondary_position"]) if r["secondary_position"] is not None else None
        ),
        height_in=r["height_in"],
        weight_lb=r["weight_lb"],
        wingspan_in=r["wingspan_in"],
        birth_date=r["birth_date"],
        draft_year=r["draft_year"],
        draft_pick_number=r["draft_pick_number"],
        years_pro=r["years_pro"],
        team_id=r["team_id"],
        roster_status=RosterStatus(r["roster_status"]),
        overall_cached=r["overall_cached"],
        overall_computed_at=r["overall_computed_at"],
        retired_in_season=r["retired_in_season"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


async def create_player(
    ex: Executor,
    *,
    league_id: int,
    first_name: str,
    last_name: str,
    primary_position: Position,
    height_in: int,
    weight_lb: int,
    wingspan_in: int,
    birth_date: date,
    roster_status: RosterStatus,
    overall_cached: int,
    overall_computed_at: datetime,
    secondary_position: Position | None = None,
    external_ref: str | None = None,
    draft_year: int | None = None,
    draft_pick_number: int | None = None,
    years_pro: int = 0,
    team_id: int | None = None,
    retired_in_season: int | None = None,
) -> Player:
    row = await ex.fetchrow(
        f"""
        INSERT INTO players
            (league_id, first_name, last_name, external_ref, primary_position,
             secondary_position, height_in, weight_lb, wingspan_in, birth_date,
             draft_year, draft_pick_number, years_pro, team_id, roster_status,
             overall_cached, overall_computed_at, retired_in_season)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
        RETURNING {_PLAYER_COLUMNS}
        """,
        league_id,
        first_name,
        last_name,
        external_ref,
        primary_position.value,
        secondary_position.value if secondary_position is not None else None,
        height_in,
        weight_lb,
        wingspan_in,
        birth_date,
        draft_year,
        draft_pick_number,
        years_pro,
        team_id,
        roster_status.value,
        overall_cached,
        overall_computed_at,
        retired_in_season,
    )
    return _map_player(row)


async def get_player(ex: Executor, player_id: int) -> Player | None:
    row = await ex.fetchrow(f"SELECT {_PLAYER_COLUMNS} FROM players WHERE id = $1", player_id)
    return _map_player(row) if row is not None else None


async def lock_player_row(ex: Executor, player_id: int) -> Player | None:
    """`SELECT ... FOR NO KEY UPDATE` -- the row lock `roster_service.sign`
    takes on the PLAYER (not the signing team) before re-checking "does
    this player already have an active contract," so two concurrent
    signings of the SAME free agent by two DIFFERENT teams can't both pass
    that check (docs/design/phase-5-discord-presentation.md §43.6's B1
    fix; RMLP's tier-2 lock, `dba2/services/roster_lock.py`).

    **RMLP Decision 1 -- `FOR NO KEY UPDATE`, not `FOR UPDATE` (renamed
    from `get_player_for_update`).** Same F8 deadlock argument as
    `team_repo.lock_team_row` (see that function's docstring in full):
    `game_sim_service._persist_game_result` inserts `player_game_stats`
    rows (`FOR KEY SHARE` on each `players` row) before `team_game_stats`
    rows, the reverse of RMLP's order, and `FOR UPDATE` here would let a
    sim transaction and a roster mutation deadlock (40P01) against each
    other. `FOR NO KEY UPDATE` still gives full participant-vs-participant
    mutual exclusion (it conflicts with itself) and is the same mode
    `player_repo.set_team_and_status`'s own plain `UPDATE` already takes
    implicitly (`team_id`/`roster_status` are not key columns of
    `players`), so nothing here is weakened -- only the lock's *timing*
    moves earlier, never its strength.

    `team_repo.lock_team_row` serializes two signings racing for the SAME
    team's roster/cap room, but does nothing when the race is over the
    SAME player across two different teams -- each call locks a different
    team row, so nothing serializes them without this second, independent
    lock on the thing the race is actually about. Mirrors
    `lock_team_row`'s own pattern verbatim: must be called with a
    `Connection` already inside an open `transaction()`, BEFORE reading
    any contract state that a concurrent caller's write could
    invalidate."""
    row = await ex.fetchrow(
        f"SELECT {_PLAYER_COLUMNS} FROM players WHERE id = $1 FOR NO KEY UPDATE", player_id
    )
    return _map_player(row) if row is not None else None


async def list_players_for_team(ex: Executor, team_id: int) -> list[Player]:
    rows = await ex.fetch(
        f"""
        SELECT {_PLAYER_COLUMNS} FROM players
        WHERE team_id = $1
        ORDER BY overall_cached DESC, id
        """,
        team_id,
    )
    return [_map_player(r) for r in rows]


async def list_players_for_league(
    ex: Executor, league_id: int, *, roster_status: RosterStatus | None = None
) -> list[Player]:
    if roster_status is None:
        rows = await ex.fetch(
            f"""
            SELECT {_PLAYER_COLUMNS} FROM players
            WHERE league_id = $1
            ORDER BY overall_cached DESC, id
            """,
            league_id,
        )
    else:
        rows = await ex.fetch(
            f"""
            SELECT {_PLAYER_COLUMNS} FROM players
            WHERE league_id = $1 AND roster_status = $2
            ORDER BY overall_cached DESC, id
            """,
            league_id,
            roster_status.value,
        )
    return [_map_player(r) for r in rows]


async def set_team_and_status(
    ex: Executor, player_id: int, team_id: int | None, roster_status: RosterStatus
) -> None:
    """The one legal way to move a player between rosters / free agency —
    keeps `team_id` and `roster_status` updated together so a caller can't
    leave e.g. `team_id = NULL` with `roster_status = 'active'`."""
    await ex.execute(
        """
        UPDATE players
        SET team_id = $2, roster_status = $3, updated_at = NOW()
        WHERE id = $1
        """,
        player_id,
        team_id,
        roster_status.value,
    )


async def set_years_pro_and_retirement(
    ex: Executor, player_id: int, *, years_pro: int, retired_in_season: int | None
) -> None:
    """The one legal way to update a player's tenure/retirement fields --
    `create_player` is the only other writer of either column. Kept as
    one function (not two) since `tools/_gm_progression_stub.py`'s own
    season-advance always sets both together (years_pro increments every
    season regardless of retirement; retired_in_season is set exactly
    once, at the season a player crosses the retirement age)."""
    await ex.execute(
        """
        UPDATE players
        SET years_pro = $2, retired_in_season = $3, updated_at = NOW()
        WHERE id = $1
        """,
        player_id,
        years_pro,
        retired_in_season,
    )


async def set_overall_cached(
    ex: Executor, player_id: int, overall_cached: int, overall_computed_at: datetime
) -> None:
    """Single-writer cache column (Decision 12) — `sim/ratings.py`'s
    `ratings_service.recompute_overall` is the only intended caller."""
    await ex.execute(
        """
        UPDATE players
        SET overall_cached = $2, overall_computed_at = $3, updated_at = NOW()
        WHERE id = $1
        """,
        player_id,
        overall_cached,
        overall_computed_at,
    )
