"""player_roles, player_directives — `role_repo`'s tables (§5). Pillar 2
scaffolding, ported from v1 unchanged.

phase-0-foundation.md §4.6. `role` has no enumerated value set in the
doc, so it stays plain `str` here, matching `dba2.domain.role`. The
directive mode columns are PD-1-retyped (docs/design/gm-command-surface.md,
2026-09-23): stored as `TEXT` still (migration 023 adds a CHECK per
column rather than a DB-level enum type), but mapped to/from
`dba2.domain.role`'s closed `StrEnum`s here, the one place the `TEXT`
<-> `Enum` boundary crosses. `role_mode` is gone (PD-6): migration 023
drops the column.
"""
from __future__ import annotations

from decimal import Decimal

import asyncpg

from dba2.data.db import Executor
from dba2.domain.role import (
    ClutchMode,
    DefenseMode,
    PlayerDirective,
    PlayerRole,
    ShotDiet,
    UsageMode,
)

_PLAYER_ROLE_COLUMNS = """
    id, league_id, team_id, season, player_id, role, touch_share,
    rationale, assigned_by, locked, assigned_at
"""

_PLAYER_DIRECTIVE_COLUMNS = """
    id, league_id, player_id, shot_diet, usage_mode, defense_mode,
    clutch_mode, set_by, updated_at
"""


def _map_player_role(r: asyncpg.Record) -> PlayerRole:
    return PlayerRole(
        id=r["id"],
        league_id=r["league_id"],
        team_id=r["team_id"],
        season=r["season"],
        player_id=r["player_id"],
        role=r["role"],
        touch_share=r["touch_share"],
        rationale=r["rationale"],
        assigned_by=r["assigned_by"],
        locked=r["locked"],
        assigned_at=r["assigned_at"],
    )


def _map_player_directive(r: asyncpg.Record) -> PlayerDirective:
    return PlayerDirective(
        id=r["id"],
        league_id=r["league_id"],
        player_id=r["player_id"],
        shot_diet=ShotDiet(r["shot_diet"]) if r["shot_diet"] is not None else None,
        usage_mode=UsageMode(r["usage_mode"]) if r["usage_mode"] is not None else None,
        defense_mode=DefenseMode(r["defense_mode"]) if r["defense_mode"] is not None else None,
        clutch_mode=ClutchMode(r["clutch_mode"]) if r["clutch_mode"] is not None else None,
        set_by=r["set_by"],
        updated_at=r["updated_at"],
    )


async def upsert_player_role(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    season: int,
    player_id: int,
    role: str,
    touch_share: Decimal,
    rationale: str | None = None,
    assigned_by: int | None = None,
    locked: bool = False,
) -> PlayerRole:
    """UNIQUE (league_id, team_id, season, player_id) — one role assignment
    per player per team per season; re-assigning within the same season
    overwrites the prior row rather than accumulating duplicates."""
    row = await ex.fetchrow(
        f"""
        INSERT INTO player_roles
            (league_id, team_id, season, player_id, role, touch_share,
             rationale, assigned_by, locked)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (league_id, team_id, season, player_id) DO UPDATE SET
            role = EXCLUDED.role,
            touch_share = EXCLUDED.touch_share,
            rationale = EXCLUDED.rationale,
            assigned_by = EXCLUDED.assigned_by,
            locked = EXCLUDED.locked,
            assigned_at = NOW()
        RETURNING {_PLAYER_ROLE_COLUMNS}
        """,
        league_id,
        team_id,
        season,
        player_id,
        role,
        touch_share,
        rationale,
        assigned_by,
        locked,
    )
    return _map_player_role(row)


async def get_player_role(
    ex: Executor, league_id: int, team_id: int, season: int, player_id: int
) -> PlayerRole | None:
    row = await ex.fetchrow(
        f"""
        SELECT {_PLAYER_ROLE_COLUMNS} FROM player_roles
        WHERE league_id = $1 AND team_id = $2 AND season = $3 AND player_id = $4
        """,
        league_id,
        team_id,
        season,
        player_id,
    )
    return _map_player_role(row) if row is not None else None


async def list_player_roles_for_team(ex: Executor, team_id: int, season: int) -> list[PlayerRole]:
    rows = await ex.fetch(
        f"""
        SELECT {_PLAYER_ROLE_COLUMNS} FROM player_roles
        WHERE team_id = $1 AND season = $2
        ORDER BY touch_share DESC
        """,
        team_id,
        season,
    )
    return [_map_player_role(r) for r in rows]


async def set_role_locked(ex: Executor, role_id: int, locked: bool) -> None:
    """A reversible toggle, not a one-time event — no re-run guard needed
    (unlike `draft_repo.complete_lottery`/`team_repo.close_manager_assignment`,
    locking/unlocking a role is meant to be flippable back and forth)."""
    await ex.execute(
        "UPDATE player_roles SET locked = $2 WHERE id = $1", role_id, locked
    )


async def upsert_player_directive(
    ex: Executor,
    *,
    league_id: int,
    player_id: int,
    shot_diet: ShotDiet | None = None,
    usage_mode: UsageMode | None = None,
    defense_mode: DefenseMode | None = None,
    clutch_mode: ClutchMode | None = None,
    set_by: int | None = None,
) -> PlayerDirective:
    """UNIQUE (league_id, player_id) — one directive row per player.
    `role_mode` dropped (PD-6). `.value` unwrapped explicitly at the call
    boundary, matching `team_repo`'s own `gm_philosophy.value`/`timeline
    .value` convention for the other `StrEnum`-backed `TEXT` columns."""
    row = await ex.fetchrow(
        f"""
        INSERT INTO player_directives
            (league_id, player_id, shot_diet, usage_mode, defense_mode,
             clutch_mode, set_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (league_id, player_id) DO UPDATE SET
            shot_diet = EXCLUDED.shot_diet,
            usage_mode = EXCLUDED.usage_mode,
            defense_mode = EXCLUDED.defense_mode,
            clutch_mode = EXCLUDED.clutch_mode,
            set_by = EXCLUDED.set_by,
            updated_at = NOW()
        RETURNING {_PLAYER_DIRECTIVE_COLUMNS}
        """,
        league_id,
        player_id,
        shot_diet.value if shot_diet is not None else None,
        usage_mode.value if usage_mode is not None else None,
        defense_mode.value if defense_mode is not None else None,
        clutch_mode.value if clutch_mode is not None else None,
        set_by,
    )
    return _map_player_directive(row)


async def clear_player_directive(ex: Executor, *, league_id: int, player_id: int) -> None:
    """PD-8: the fix for `player_directives` being league-scoped with no
    `team_id`/`season` (`UNIQUE (league_id, player_id)`, migration 005) --
    a directive set by one GM otherwise keeps applying after the player is
    traded to a rival, or signs elsewhere as a free agent. Called by
    `gm_service.apply_trade` and `roster_service.sign`, inside each
    applier's own transaction, whenever the team actually changes -- a
    no-op delete (zero rows affected) when the player never had a directive
    set is not an error.

    Deliberately NOT called by `roster_service.waive`/`release` (As-built
    note, docs/design/gm-command-surface.md §10): both only null out
    `team_id` via `set_team_and_status`, which is not a genuine team-to-
    team move -- a free agent has no `team_id` a directive could keep
    wrongly applying to, and no game ever simulates a `team_id IS NULL`
    player, so there is nothing here to clear until `sign` (or a future
    trade) gives the player a new team, at which point this function runs
    on that path instead."""
    await ex.execute(
        "DELETE FROM player_directives WHERE league_id = $1 AND player_id = $2",
        league_id,
        player_id,
    )


async def get_player_directive(
    ex: Executor, league_id: int, player_id: int
) -> PlayerDirective | None:
    row = await ex.fetchrow(
        f"""
        SELECT {_PLAYER_DIRECTIVE_COLUMNS} FROM player_directives
        WHERE league_id = $1 AND player_id = $2
        """,
        league_id,
        player_id,
    )
    return _map_player_directive(row) if row is not None else None


async def list_player_directives_for_league(ex: Executor, league_id: int) -> list[PlayerDirective]:
    """Bulk loader, same N+1 reason as `ratings_repo.get_tendencies_for_league`
    (phase-1-simulation-engine.md §8.2) — avoids a per-player
    `get_player_directive` query across a league snapshot."""
    rows = await ex.fetch(
        f"SELECT {_PLAYER_DIRECTIVE_COLUMNS} FROM player_directives WHERE league_id = $1",
        league_id,
    )
    return [_map_player_directive(r) for r in rows]
