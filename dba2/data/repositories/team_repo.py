"""teams, team_strategy, team_manager_history — `team_repo`'s tables (§5).

phase-0-foundation.md §4.2.
"""
from __future__ import annotations

import asyncpg

from dba2.data.db import Executor
from dba2.domain.team import (
    DefensiveScheme,
    GmPhilosophy,
    OffensiveScheme,
    Team,
    TeamManagerHistory,
    TeamStrategy,
    Timeline,
    TimelineSource,
)

_TEAM_COLUMNS = """
    id, league_id, code, name, city, conference, division, manager_user_id,
    gm_philosophy, timeline, home_market_size, timeline_source, auto_pilot
"""

_TEAM_STRATEGY_COLUMNS = """
    league_id, team_id, season, pace_target, offensive_scheme,
    defensive_scheme, help_aggression, three_point_bias, crash_oreb_bias,
    set_by
"""

_TEAM_MANAGER_HISTORY_COLUMNS = """
    id, team_id, user_id, assigned_at, removed_at, assigned_by
"""


def _map_team(r: asyncpg.Record) -> Team:
    return Team(
        id=r["id"],
        league_id=r["league_id"],
        code=r["code"],
        name=r["name"],
        city=r["city"],
        conference=r["conference"],
        division=r["division"],
        manager_user_id=r["manager_user_id"],
        gm_philosophy=GmPhilosophy(r["gm_philosophy"]),
        timeline=Timeline(r["timeline"]),
        home_market_size=r["home_market_size"],
        timeline_source=TimelineSource(r["timeline_source"]),
        auto_pilot=r["auto_pilot"],
    )


def _map_team_strategy(r: asyncpg.Record) -> TeamStrategy:
    return TeamStrategy(
        league_id=r["league_id"],
        team_id=r["team_id"],
        season=r["season"],
        pace_target=r["pace_target"],
        offensive_scheme=OffensiveScheme(r["offensive_scheme"]),
        defensive_scheme=DefensiveScheme(r["defensive_scheme"]),
        help_aggression=r["help_aggression"],
        three_point_bias=r["three_point_bias"],
        crash_oreb_bias=r["crash_oreb_bias"],
        set_by=r["set_by"],
    )


def _map_team_manager_history(r: asyncpg.Record) -> TeamManagerHistory:
    return TeamManagerHistory(
        id=r["id"],
        team_id=r["team_id"],
        user_id=r["user_id"],
        assigned_at=r["assigned_at"],
        removed_at=r["removed_at"],
        assigned_by=r["assigned_by"],
    )


async def create_team(
    ex: Executor,
    *,
    league_id: int,
    code: str,
    name: str,
    city: str,
    conference: str,
    division: str,
    home_market_size: int,
    manager_user_id: int | None = None,
    gm_philosophy: GmPhilosophy = GmPhilosophy.TENDENCY_RESPECTER,
    timeline: Timeline = Timeline.RETOOL,
) -> Team:
    row = await ex.fetchrow(
        f"""
        INSERT INTO teams
            (league_id, code, name, city, conference, division,
             manager_user_id, gm_philosophy, timeline, home_market_size)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING {_TEAM_COLUMNS}
        """,
        league_id,
        code,
        name,
        city,
        conference,
        division,
        manager_user_id,
        gm_philosophy.value,
        timeline.value,
        home_market_size,
    )
    return _map_team(row)


async def get_team(ex: Executor, team_id: int) -> Team | None:
    row = await ex.fetchrow(f"SELECT {_TEAM_COLUMNS} FROM teams WHERE id = $1", team_id)
    return _map_team(row) if row is not None else None


async def lock_team_row(ex: Executor, team_id: int) -> Team | None:
    """`SELECT ... FOR NO KEY UPDATE` -- the row lock a caller composing a
    check-then-write critical section against this team's own aggregate
    state (e.g. `gm_command_service.sign_free_agent`'s roster-max/cap-
    legality gates, docs/design/phase-5-discord-presentation.md §43.5's
    B1 fix; RMLP's tier-1 lock, `dba2/services/roster_lock.py`) must take
    BEFORE reading that state, so a second concurrent caller acting on the
    same team blocks until the first's transaction commits (or rolls
    back) and only then re-reads state that reflects the first's write --
    instead of both readers seeing "room available" and both writers
    succeeding, overfilling the roster past `roster_max`.

    **RMLP Decision 1 -- `FOR NO KEY UPDATE`, not `FOR UPDATE` (renamed
    from `get_team_for_update`).** `FOR UPDATE` at this tier created a
    live deadlock class against `game_sim_service`, which inserts
    `player_game_stats` before `team_game_stats` (the exact reverse of
    RMLP's teams-then-players canonical order) and is not and never will
    be an RMLP participant -- `FOR NO KEY UPDATE` conflicts with itself
    (so every participant-vs-participant guarantee this row lock ever
    provided is unchanged) but NOT with the `FOR KEY SHARE` an FK insert
    (e.g. a `team_game_stats` row) takes, which is what makes the sim and
    a roster mutation on the same team fully concurrent instead of able
    to deadlock. See `roster_lock.py`'s module docstring for the full
    argument and the 40P01 triage rule. Nothing this codebase writes to
    `teams` updates its key column (`id`) or deletes the row, so `FOR NO
    KEY UPDATE` is never a weaker lock than what the write itself needs
    -- this only escalates the lock's timing (acquired before the read,
    not implicitly at the write), never its strength.

    Mirrors `league_repo.get_phase_state_for_update`'s identical pattern
    (that module's own docstring: "the row lock that serializes ... read-
    check-write critical section") -- that function is intentionally left
    on `FOR UPDATE`/its own name, out of RMLP's scope (§11 item D1-1).
    Must be called with a `Connection` already inside an open
    `transaction()` -- same requirement as every locking read in this
    codebase."""
    row = await ex.fetchrow(
        f"SELECT {_TEAM_COLUMNS} FROM teams WHERE id = $1 FOR NO KEY UPDATE", team_id
    )
    return _map_team(row) if row is not None else None


async def get_team_by_code(ex: Executor, league_id: int, code: str) -> Team | None:
    row = await ex.fetchrow(
        f"SELECT {_TEAM_COLUMNS} FROM teams WHERE league_id = $1 AND code = $2",
        league_id,
        code,
    )
    return _map_team(row) if row is not None else None


async def list_teams_for_league(ex: Executor, league_id: int) -> list[Team]:
    rows = await ex.fetch(
        f"SELECT {_TEAM_COLUMNS} FROM teams WHERE league_id = $1 ORDER BY code",
        league_id,
    )
    return [_map_team(r) for r in rows]


async def set_manager(ex: Executor, team_id: int, manager_user_id: int | None) -> None:
    await ex.execute(
        "UPDATE teams SET manager_user_id = $2 WHERE id = $1", team_id, manager_user_id
    )


async def clear_auto_pilot(ex: Executor, team_id: int) -> None:
    """`league_service.remove_manager`'s own writer (GM-5, docs/design/
    gm-command-surface.md) -- called in the SAME transaction as `set_
    manager(ex, team_id, None)`, so a team can never be left with `auto_
    pilot = TRUE` and no manager past that transaction's commit. Does not
    touch `gm_philosophy`: unlike `/gm auto`'s own `set_auto_pilot`, this
    path is not the manager delegating anything -- it is the manager being
    removed, and the philosophy column keeps its value for whoever the AI
    (or a future manager) inherits next, per GM-8's own "turning auto off
    does not reset gm_philosophy" rule."""
    await ex.execute("UPDATE teams SET auto_pilot = FALSE WHERE id = $1", team_id)


async def set_auto_pilot(
    ex: Executor, team_id: int, *, auto_pilot: bool, gm_philosophy: GmPhilosophy
) -> Team:
    """`/gm auto`'s own writer (GM-8, docs/design/gm-command-surface.md).
    Writes `teams.auto_pilot` and `teams.gm_philosophy` together, in one
    statement -- `/gm auto` is the only command that ever sets either, so
    there is no ordering hazard between the two columns to worry about.
    Never touches `manager_user_id` (P5-41's ONE writer of that column
    stays `league_service`); GM-1 is explicit that auto-pilot carries no
    composite meaning with, and no CHECK coupling to, that column."""
    row = await ex.fetchrow(
        f"""
        UPDATE teams SET auto_pilot = $2, gm_philosophy = $3 WHERE id = $1
        RETURNING {_TEAM_COLUMNS}
        """,
        team_id,
        auto_pilot,
        gm_philosophy.value,
    )
    assert row is not None  # team_id validated by caller before this write
    return _map_team(row)


async def set_timeline(ex: Executor, team_id: int, timeline: Timeline) -> None:
    """`apply_windows`' own writer (Decision P2-13) -- touches ONLY the
    `timeline` column, never `timeline_source`. This is load-bearing for
    Decision P5-67: a pinned team's classification pass round-trips its
    own stored value back through this same function (§29.2's own
    provable-no-op argument), and that round trip must never flip
    `timeline_source` back to `'auto'` underneath a human's pin. A human
    override's writer is the separate `set_team_timeline`, below."""
    await ex.execute(
        "UPDATE teams SET timeline = $2 WHERE id = $1", team_id, timeline.value
    )


async def set_team_timeline(
    ex: Executor,
    team_id: int,
    *,
    timeline: Timeline | None,
    source: TimelineSource,
) -> Team:
    """The `/timeline` command's own writer (Decision P5-66/P5-68) --
    the ONE other writer of `teams.timeline`/`timeline_source` besides
    `set_timeline`'s classifier path, above. `timeline=None` is `/timeline
    auto`'s own shape: only `timeline_source` flips back to `'auto'`;
    `teams.timeline` is left exactly where the human's last manual value
    (or the classifier's last write) left it, so the NEXT classification
    pass reads it as `previous_by_team` and hysteresis damps the
    transition out of the pin exactly as it damps any other (§29.2's own
    "no special case" text)."""
    if timeline is None:
        row = await ex.fetchrow(
            f"""
            UPDATE teams SET timeline_source = $2 WHERE id = $1
            RETURNING {_TEAM_COLUMNS}
            """,
            team_id,
            source.value,
        )
    else:
        row = await ex.fetchrow(
            f"""
            UPDATE teams SET timeline = $2, timeline_source = $3 WHERE id = $1
            RETURNING {_TEAM_COLUMNS}
            """,
            team_id,
            timeline.value,
            source.value,
        )
    assert row is not None  # team_id validated by caller before this write
    return _map_team(row)


async def upsert_team_strategy(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    season: int,
    pace_target: int,
    offensive_scheme: OffensiveScheme,
    defensive_scheme: DefensiveScheme,
    help_aggression: int,
    three_point_bias: int,
    crash_oreb_bias: int,
    set_by: int | None = None,
) -> TeamStrategy:
    row = await ex.fetchrow(
        f"""
        INSERT INTO team_strategy
            (league_id, team_id, season, pace_target, offensive_scheme,
             defensive_scheme, help_aggression, three_point_bias,
             crash_oreb_bias, set_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (league_id, team_id, season) DO UPDATE SET
            pace_target = EXCLUDED.pace_target,
            offensive_scheme = EXCLUDED.offensive_scheme,
            defensive_scheme = EXCLUDED.defensive_scheme,
            help_aggression = EXCLUDED.help_aggression,
            three_point_bias = EXCLUDED.three_point_bias,
            crash_oreb_bias = EXCLUDED.crash_oreb_bias,
            set_by = EXCLUDED.set_by
        RETURNING {_TEAM_STRATEGY_COLUMNS}
        """,
        league_id,
        team_id,
        season,
        pace_target,
        offensive_scheme.value,
        defensive_scheme.value,
        help_aggression,
        three_point_bias,
        crash_oreb_bias,
        set_by,
    )
    return _map_team_strategy(row)


async def list_team_strategies_for_league_season(
    ex: Executor, *, league_id: int, season: int
) -> list[TeamStrategy]:
    """docs/design/media-v2-newsroom.md MV-11.2's own bulk-read requirement:
    `SCHEME_IDENTITY`'s loader needs every team's declared strategy for one
    league-season to rank `pace_target` league-wide, and `get_team_strategy`
    above is a per-team `fetchrow` -- 30 round trips a cycle otherwise, the
    exact per-team-loop shape step 6's own "one shared `LeagueSnapshot` per
    cycle" discipline forbids. `team_strategy` carries `league_id` directly
    (no join needed), so this is one `WHERE league_id = $1 AND season = $2`
    statement, mirroring `list_teams_for_league`'s own bulk shape."""
    rows = await ex.fetch(
        f"""
        SELECT {_TEAM_STRATEGY_COLUMNS} FROM team_strategy
        WHERE league_id = $1 AND season = $2
        """,
        league_id,
        season,
    )
    return [_map_team_strategy(r) for r in rows]


async def get_team_strategy(
    ex: Executor, team_id: int, season: int
) -> TeamStrategy | None:
    row = await ex.fetchrow(
        f"""
        SELECT {_TEAM_STRATEGY_COLUMNS} FROM team_strategy
        WHERE team_id = $1 AND season = $2
        """,
        team_id,
        season,
    )
    return _map_team_strategy(row) if row is not None else None


async def record_manager_assignment(
    ex: Executor,
    *,
    team_id: int,
    user_id: int | None,
    assigned_by: int,
) -> TeamManagerHistory:
    row = await ex.fetchrow(
        f"""
        INSERT INTO team_manager_history (team_id, user_id, assigned_by)
        VALUES ($1, $2, $3)
        RETURNING {_TEAM_MANAGER_HISTORY_COLUMNS}
        """,
        team_id,
        user_id,
        assigned_by,
    )
    return _map_team_manager_history(row)


async def get_open_manager_assignment(ex: Executor, team_id: int) -> TeamManagerHistory | None:
    """The one `removed_at IS NULL` row for a team, if any -- what `league_
    service.assign_manager`/`remove_manager` (Decision P5-41) read before
    deciding whether there is an existing assignment to close."""
    row = await ex.fetchrow(
        f"""
        SELECT {_TEAM_MANAGER_HISTORY_COLUMNS} FROM team_manager_history
        WHERE team_id = $1 AND removed_at IS NULL
        """,
        team_id,
    )
    return _map_team_manager_history(row) if row is not None else None


async def close_manager_assignment(ex: Executor, history_id: int) -> None:
    """Sets `removed_at = NOW()` on an open (`removed_at IS NULL`) history row.

    A no-op on a row that's already closed — the `AND removed_at IS NULL`
    guard is load-bearing, not decorative: without it, a second call
    silently overwrites the original removal timestamp on the audit trail
    with no error (Phase 0 step-7 critic round, B1)."""
    await ex.execute(
        "UPDATE team_manager_history SET removed_at = NOW() "
        "WHERE id = $1 AND removed_at IS NULL",
        history_id,
    )


async def list_manager_history_for_team(
    ex: Executor, team_id: int
) -> list[TeamManagerHistory]:
    rows = await ex.fetch(
        f"""
        SELECT {_TEAM_MANAGER_HISTORY_COLUMNS} FROM team_manager_history
        WHERE team_id = $1
        ORDER BY assigned_at
        """,
        team_id,
    )
    return [_map_team_manager_history(r) for r in rows]


async def list_manager_history_for_league(
    ex: Executor, league_id: int
) -> list[TeamManagerHistory]:
    """Phase 5's transactions merge (docs/design/phase-5-discord-
    presentation.md §22.5/B-12, Decision P5-33) needs every manager
    assignment/removal in a LEAGUE, not one team at a time --
    `team_manager_history` carries no `league_id` of its own (migration
    001's own shape), so this joins through `teams` rather than adding a
    denormalized column to an already-shipped table. Ordered by
    `assigned_at` for the same reason `list_manager_history_for_team`
    is -- the merge's own ordering key is computed by its caller, not
    here."""
    rows = await ex.fetch(
        """
        SELECT tmh.id, tmh.team_id, tmh.user_id, tmh.assigned_at,
               tmh.removed_at, tmh.assigned_by
        FROM team_manager_history tmh
        JOIN teams t ON t.id = tmh.team_id
        WHERE t.league_id = $1
        ORDER BY tmh.assigned_at
        """,
        league_id,
    )
    return [_map_team_manager_history(r) for r in rows]
