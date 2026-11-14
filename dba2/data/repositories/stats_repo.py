"""player_game_stats, team_game_stats, player_season_stats,
game_period_scores — `stats_repo`'s tables (§5: "season/career
aggregation" plus the line score, `game_period_scores`, migration 008 —
see the paragraph below for why it lives here and not in `game_repo`
despite FK-ing to `games`).

phase-0-foundation.md §4.7. `_BOX_SCORE_COLUMN_NAMES` is the single source
of truth for the 16 box-score column names shared by all three tables (the
zone-FG split plus standard counting stats), in the same order as migration
006's shared `_ZONE_FG_COLUMNS` SQL fragment and `domain.stats._BoxScoreColumns`'s
field order — mirroring `ratings_repo`'s `_RATING_COLUMN_NAMES`/
`_rating_kwargs` pattern so the column set is hand-listed once, not three
times across the three tables that share it. `records_repo` (which reads
these tables but owns none of them, §5) imports this tuple rather than
hand-listing the box columns a fourth time.

`player_game_stats`/`team_game_stats` were per-game inserts, not upserts, in
Phase 0 — their composite PKs (`(game_id, player_id)`, `(game_id, team_id)`)
carried no `ON CONFLICT`-relevant constraint beyond that PK, and a game got
simmed once (§4.7's schema-only framing: nothing wrote these yet, so there
was no re-sim/overwrite workflow to design for). That assumption is now
false — phase-1-simulation-engine.md's Decision P1-25: the tuning loop
re-sims the same schedule dozens of times per calibration run, so
`delete_stats_for_game` deletes all three per-game tables' rows for a game
(`player_game_stats`, `team_game_stats`, and `game_period_scores` — see
below for why the third belongs in the same delete) inside the same
transaction *before* re-inserting, rather than upserting any of them
directly. `player_season_stats` remains the one wholesale-upsert write
here — it's an explicitly materialized aggregate, PK `(league_id,
player_id, season, game_type)`, rebuilt in full by a future `stats_service`
the same way `ratings_repo.upsert_player_ratings` rewrites the rating block
wholesale (Decision 10's pattern), not accumulated in place.

`game_period_scores` (migration 008, phase-1-simulation-engine.md §7.2)
lives here too, not in `game_repo` — it is per-game *stat* data (the line
score), matching §8.2's module assignment. `upsert_game_period_scores`
itself is a genuine upsert (`ON CONFLICT (game_id, team_id, period) DO
UPDATE`), so a re-sim that reproduces the same period count overwrites
those rows in place correctly. But `delete_stats_for_game` still clears
this table first (same as the other two): an upsert can only overwrite
periods the *new* result actually produces, so a re-sim with *fewer*
periods than the original (e.g. OT collapsing to regulation) would
otherwise leave the old, now-stale trailing periods behind with no
upsert ever touching them (critic round 11's B2).

`list_player_game_stats_for_player` gained a required `league_id`
keyword-only parameter in migration 008's companion change (Amendment 1,
critic B4): the `ix_player_game_stats_league_player` index (migration 008)
has an unconstrained leading column and is never chosen by the planner
without it — a breaking signature change, not purely additive
(phase-1-simulation-engine.md §8.2).
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import asyncpg

from dba2.core.errors import ValidationError
from dba2.data.db import Executor
from dba2.domain.game import GameType
from dba2.domain.stats import (
    GamePeriodScore,
    LeaderMode,
    LeaderStat,
    LiveLeaderPage,
    PlayerCareerScoringToDate,
    PlayerFormWindow,
    PlayerGameStats,
    PlayerSeasonStats,
    StatLeaderRow,
    TeamGameStats,
    TeamStyleAggregate,
)

# Phase 5 Stage B step 8 (`/leaders <stat>`, docs/design/phase-5-discord-
# presentation.md §7.1): each `LeaderStat` member maps to a fixed SQL
# expression over `player_season_stats`'s own already-summed columns --
# never a caller-supplied string interpolated into the query. `rebounds`
# is the one multi-column case (`_BoxScoreColumns`'s own "no separate
# total" convention -- oreb/dreb stay split at the table, combined here).
LEADER_STAT_COLUMNS: dict[LeaderStat, str] = {
    LeaderStat.POINTS: "points",
    LeaderStat.REBOUNDS: "(oreb + dreb)",
    LeaderStat.ASSISTS: "ast",
    LeaderStat.STEALS: "stl",
    LeaderStat.BLOCKS: "blk",
    LeaderStat.TURNOVERS: "tov",
}

# docs/design/leaders-v2-per-game-and-paging.md LD2-1: the ORDER BY half of
# `list_live_stat_leaders`'s query, a second closed dict guarded exactly like
# `LEADER_STAT_COLUMNS` above -- never a caller-supplied string. Applied to the
# `totals` CTE's own columns (`t.stat_total`, `t.games_played`, `t.player_id`), not
# reused across queries, so it lives beside `LEADER_STAT_COLUMNS` rather than in
# `domain/stats.py` with `LeaderMode` itself.
_LEADER_ORDER_EXPRESSIONS: dict[LeaderMode, str] = {
    LeaderMode.PER_GAME: "t.stat_total::numeric / t.games_played DESC, t.player_id ASC",
    LeaderMode.TOTAL: "t.stat_total DESC, t.player_id ASC",
}

# LD2-2: 70% of a player's OWN team's games played, never the league-wide MAX
# revision 1 used (see `list_live_stat_leaders`'s own docstring for the proof this
# can never wipe a full-season player). Bound as `$5`, a parameter, never
# interpolated into the query text.
_QUALIFY_FRACTION = 0.70

_BOX_SCORE_COLUMN_NAMES: tuple[str, ...] = (
    "points",
    "fga_rim", "fgm_rim", "fga_mid", "fgm_mid", "fga_three", "fgm_three",
    "fta", "ftm",
    "oreb", "dreb", "ast", "stl", "blk", "tov", "pf",
)

_PLAYER_GAME_STATS_COLUMNS = (
    "game_id, league_id, player_id, team_id, minutes_played, plus_minus, "
    + ", ".join(_BOX_SCORE_COLUMN_NAMES)
)

_TEAM_GAME_STATS_COLUMNS = (
    "game_id, league_id, team_id, possessions, " + ", ".join(_BOX_SCORE_COLUMN_NAMES)
)

_PLAYER_SEASON_STATS_COLUMNS = (
    "league_id, player_id, season, game_type, games_played, minutes_played, "
    "updated_at, " + ", ".join(_BOX_SCORE_COLUMN_NAMES)
)

_GAME_PERIOD_SCORE_COLUMNS = "game_id, league_id, team_id, period, points"


def _validate_box_score_dict(box: dict[str, int]) -> None:
    given = set(box.keys())
    expected = set(_BOX_SCORE_COLUMN_NAMES)
    if given != expected:
        missing = expected - given
        extra = given - expected
        raise ValidationError(
            f"box dict does not match the 16-column box-score block "
            f"(missing={sorted(missing)}, extra={sorted(extra)})"
        )


def _box_score_kwargs(r: asyncpg.Record) -> dict[str, int]:
    return {name: r[name] for name in _BOX_SCORE_COLUMN_NAMES}


def _map_player_game_stats(r: asyncpg.Record) -> PlayerGameStats:
    return PlayerGameStats(
        game_id=r["game_id"],
        league_id=r["league_id"],
        player_id=r["player_id"],
        team_id=r["team_id"],
        minutes_played=r["minutes_played"],
        plus_minus=r["plus_minus"],
        **_box_score_kwargs(r),
    )


def _map_team_game_stats(r: asyncpg.Record) -> TeamGameStats:
    return TeamGameStats(
        game_id=r["game_id"],
        league_id=r["league_id"],
        team_id=r["team_id"],
        possessions=r["possessions"],
        **_box_score_kwargs(r),
    )


def _map_player_season_stats(r: asyncpg.Record) -> PlayerSeasonStats:
    return PlayerSeasonStats(
        league_id=r["league_id"],
        player_id=r["player_id"],
        season=r["season"],
        game_type=GameType(r["game_type"]),
        games_played=r["games_played"],
        minutes_played=r["minutes_played"],
        updated_at=r["updated_at"],
        **_box_score_kwargs(r),
    )


def _map_game_period_score(r: asyncpg.Record) -> GamePeriodScore:
    return GamePeriodScore(
        game_id=r["game_id"],
        league_id=r["league_id"],
        team_id=r["team_id"],
        period=r["period"],
        points=r["points"],
    )


async def create_player_game_stats(
    ex: Executor,
    *,
    game_id: int,
    league_id: int,
    player_id: int,
    team_id: int,
    minutes_played: Decimal,
    plus_minus: int,
    box: dict[str, int],
) -> PlayerGameStats:
    _validate_box_score_dict(box)
    values = [box[name] for name in _BOX_SCORE_COLUMN_NAMES]
    box_placeholders = ", ".join(f"${i + 7}" for i in range(len(_BOX_SCORE_COLUMN_NAMES)))
    row = await ex.fetchrow(
        f"""
        INSERT INTO player_game_stats
            (game_id, league_id, player_id, team_id, minutes_played, plus_minus,
             {", ".join(_BOX_SCORE_COLUMN_NAMES)})
        VALUES ($1, $2, $3, $4, $5, $6, {box_placeholders})
        RETURNING {_PLAYER_GAME_STATS_COLUMNS}
        """,
        game_id,
        league_id,
        player_id,
        team_id,
        minutes_played,
        plus_minus,
        *values,
    )
    return _map_player_game_stats(row)


_PLAYER_GAME_STATS_BULK_FIELDS: tuple[tuple[str, str], ...] = (
    ("game_id", "int"),
    ("league_id", "int"),
    ("player_id", "int"),
    ("team_id", "int"),
    ("minutes_played", "numeric"),
    ("plus_minus", "smallint"),
    *((name, "smallint") for name in _BOX_SCORE_COLUMN_NAMES),
)


def _bulk_field_value(row: dict, field: str):
    """A bulk-insert row dict has the same shape as the singular function's
    kwargs plus a nested `box` dict — this pulls a box column out of `box`
    and everything else out of the row directly, so the caller doesn't have
    to flatten box columns into the row dict by hand."""
    return row["box"][field] if field in _BOX_SCORE_COLUMN_NAMES else row[field]


async def create_player_game_stats_many(
    ex: Executor, rows: list[dict]
) -> list[PlayerGameStats]:
    """Bulk insert via `UNNEST` — one round trip for a whole game's ~26
    player rows instead of 26 single-row `INSERT`s (§8.2, §11's "4 round
    trips per game" persistence budget). Each dict is
    `create_player_game_stats`'s kwargs (`game_id`, `league_id`,
    `player_id`, `team_id`, `minutes_played`, `plus_minus`, `box`); `box`
    is validated the same way the singular function validates it."""
    if not rows:
        return []
    for r in rows:
        _validate_box_score_dict(r["box"])
    fields = _PLAYER_GAME_STATS_BULK_FIELDS
    col_list = ", ".join(name for name, _ in fields)
    casts = ", ".join(f"${i + 1}::{sql_type}[]" for i, (_, sql_type) in enumerate(fields))
    unnest_cols = ", ".join(name for name, _ in fields)
    result = await ex.fetch(
        f"""
        INSERT INTO player_game_stats ({col_list})
        SELECT {unnest_cols} FROM UNNEST({casts}) AS t({unnest_cols})
        RETURNING {_PLAYER_GAME_STATS_COLUMNS}
        """,
        *([_bulk_field_value(r, name) for r in rows] for name, _ in fields),
    )
    return [_map_player_game_stats(r) for r in result]


async def get_player_game_stats(
    ex: Executor, game_id: int, player_id: int
) -> PlayerGameStats | None:
    row = await ex.fetchrow(
        f"""
        SELECT {_PLAYER_GAME_STATS_COLUMNS} FROM player_game_stats
        WHERE game_id = $1 AND player_id = $2
        """,
        game_id,
        player_id,
    )
    return _map_player_game_stats(row) if row is not None else None


async def list_player_game_stats_for_game(ex: Executor, game_id: int) -> list[PlayerGameStats]:
    rows = await ex.fetch(
        f"""
        SELECT {_PLAYER_GAME_STATS_COLUMNS} FROM player_game_stats
        WHERE game_id = $1
        ORDER BY player_id
        """,
        game_id,
    )
    return [_map_player_game_stats(r) for r in rows]


async def list_player_game_stats_for_player(
    ex: Executor, player_id: int, *, league_id: int
) -> list[PlayerGameStats]:
    """`league_id` is required and keyword-only — a breaking signature
    change (Amendment 1, critic B4): the `ix_player_game_stats_league_player`
    index (migration 008) has an unconstrained leading column and is never
    chosen by the planner without a `league_id` predicate."""
    rows = await ex.fetch(
        f"""
        SELECT {_PLAYER_GAME_STATS_COLUMNS} FROM player_game_stats
        WHERE player_id = $1 AND league_id = $2
        ORDER BY game_id
        """,
        player_id,
        league_id,
    )
    return [_map_player_game_stats(r) for r in rows]


async def create_team_game_stats(
    ex: Executor,
    *,
    game_id: int,
    league_id: int,
    team_id: int,
    possessions: int,
    box: dict[str, int],
) -> TeamGameStats:
    _validate_box_score_dict(box)
    values = [box[name] for name in _BOX_SCORE_COLUMN_NAMES]
    box_placeholders = ", ".join(f"${i + 5}" for i in range(len(_BOX_SCORE_COLUMN_NAMES)))
    row = await ex.fetchrow(
        f"""
        INSERT INTO team_game_stats
            (game_id, league_id, team_id, possessions, {", ".join(_BOX_SCORE_COLUMN_NAMES)})
        VALUES ($1, $2, $3, $4, {box_placeholders})
        RETURNING {_TEAM_GAME_STATS_COLUMNS}
        """,
        game_id,
        league_id,
        team_id,
        possessions,
        *values,
    )
    return _map_team_game_stats(row)


_TEAM_GAME_STATS_BULK_FIELDS: tuple[tuple[str, str], ...] = (
    ("game_id", "int"),
    ("league_id", "int"),
    ("team_id", "int"),
    ("possessions", "smallint"),
    *((name, "smallint") for name in _BOX_SCORE_COLUMN_NAMES),
)


async def create_team_game_stats_many(ex: Executor, rows: list[dict]) -> list[TeamGameStats]:
    """Bulk insert via `UNNEST` — one round trip for a game's 2 team rows
    instead of 2 single-row `INSERT`s (§8.2, same reasoning as
    `create_player_game_stats_many`). Each dict is
    `create_team_game_stats`'s kwargs (`game_id`, `league_id`, `team_id`,
    `possessions`, `box`)."""
    if not rows:
        return []
    for r in rows:
        _validate_box_score_dict(r["box"])
    fields = _TEAM_GAME_STATS_BULK_FIELDS
    col_list = ", ".join(name for name, _ in fields)
    casts = ", ".join(f"${i + 1}::{sql_type}[]" for i, (_, sql_type) in enumerate(fields))
    unnest_cols = ", ".join(name for name, _ in fields)
    result = await ex.fetch(
        f"""
        INSERT INTO team_game_stats ({col_list})
        SELECT {unnest_cols} FROM UNNEST({casts}) AS t({unnest_cols})
        RETURNING {_TEAM_GAME_STATS_COLUMNS}
        """,
        *([_bulk_field_value(r, name) for r in rows] for name, _ in fields),
    )
    return [_map_team_game_stats(r) for r in result]


async def get_team_game_stats(ex: Executor, game_id: int, team_id: int) -> TeamGameStats | None:
    row = await ex.fetchrow(
        f"""
        SELECT {_TEAM_GAME_STATS_COLUMNS} FROM team_game_stats
        WHERE game_id = $1 AND team_id = $2
        """,
        game_id,
        team_id,
    )
    return _map_team_game_stats(row) if row is not None else None


async def list_team_game_stats_for_game(ex: Executor, game_id: int) -> list[TeamGameStats]:
    rows = await ex.fetch(
        f"""
        SELECT {_TEAM_GAME_STATS_COLUMNS} FROM team_game_stats
        WHERE game_id = $1
        ORDER BY team_id
        """,
        game_id,
    )
    return [_map_team_game_stats(r) for r in rows]


async def list_team_game_stats_for_team(ex: Executor, team_id: int) -> list[TeamGameStats]:
    rows = await ex.fetch(
        f"""
        SELECT {_TEAM_GAME_STATS_COLUMNS} FROM team_game_stats
        WHERE team_id = $1
        ORDER BY game_id
        """,
        team_id,
    )
    return [_map_team_game_stats(r) for r in rows]


async def upsert_player_season_stats(
    ex: Executor,
    *,
    league_id: int,
    player_id: int,
    season: int,
    game_type: GameType,
    games_played: int,
    minutes_played: Decimal,
    box: dict[str, int],
) -> PlayerSeasonStats:
    """Wholesale replace — `player_season_stats` is a materialized aggregate
    rebuilt in full by a future `stats_service`, not accumulated in place
    (§4.7), the same write pattern as `ratings_repo.upsert_player_ratings`."""
    _validate_box_score_dict(box)
    values = [box[name] for name in _BOX_SCORE_COLUMN_NAMES]
    box_placeholders = ", ".join(f"${i + 7}" for i in range(len(_BOX_SCORE_COLUMN_NAMES)))
    box_update_clause = ", ".join(
        f"{name} = EXCLUDED.{name}" for name in _BOX_SCORE_COLUMN_NAMES
    )
    row = await ex.fetchrow(
        f"""
        INSERT INTO player_season_stats
            (league_id, player_id, season, game_type, games_played,
             minutes_played, updated_at, {", ".join(_BOX_SCORE_COLUMN_NAMES)})
        VALUES ($1, $2, $3, $4, $5, $6, NOW(), {box_placeholders})
        ON CONFLICT (league_id, player_id, season, game_type) DO UPDATE SET
            games_played = EXCLUDED.games_played,
            minutes_played = EXCLUDED.minutes_played,
            updated_at = NOW(),
            {box_update_clause}
        RETURNING {_PLAYER_SEASON_STATS_COLUMNS}
        """,
        league_id,
        player_id,
        season,
        game_type.value,
        games_played,
        minutes_played,
        *values,
    )
    return _map_player_season_stats(row)


async def delete_stale_player_season_stats(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    game_type: GameType,
    keep_player_ids: Sequence[int],
) -> None:
    """The delete half of `stats_service.rebuild_player_season_stats`'s
    delete-then-reconcile rebuild (critic round 19 B19-1): deletes every
    `player_season_stats` row for `(league_id, season, game_type)` whose
    `player_id` is not in `keep_player_ids` -- the newly-recomputed
    aggregate's survivor set. Without this, a player whose entire
    `player_game_stats` log for this league/season/game_type disappears
    between rebuilds (e.g. a roster-status change drops them out of a
    re-sim's aggregate) keeps a permanently stale season row with zero
    backing per-game rows, since `upsert_player_season_stats` only ever
    writes rows for players present in the new aggregate and never touches
    anyone else's.

    `keep_player_ids` may be empty -- Postgres's `!= ALL(ARRAY[]::int[])`
    is vacuously true for every row, so an empty survivor set correctly
    deletes every row for this league/season/game_type rather than being a
    silent no-op."""
    await ex.execute(
        """
        DELETE FROM player_season_stats
        WHERE league_id = $1 AND season = $2 AND game_type = $3
          AND player_id != ALL($4::int[])
        """,
        league_id,
        season,
        game_type.value,
        list(keep_player_ids),
    )


async def get_player_season_stats(
    ex: Executor, league_id: int, player_id: int, season: int, game_type: GameType
) -> PlayerSeasonStats | None:
    row = await ex.fetchrow(
        f"""
        SELECT {_PLAYER_SEASON_STATS_COLUMNS} FROM player_season_stats
        WHERE league_id = $1 AND player_id = $2 AND season = $3 AND game_type = $4
        """,
        league_id,
        player_id,
        season,
        game_type.value,
    )
    return _map_player_season_stats(row) if row is not None else None


async def get_player_season_stats_bulk(
    ex: Executor,
    league_id: int,
    player_ids: Sequence[int],
    season: int,
    game_type: GameType,
) -> dict[int, PlayerSeasonStats]:
    """Batch form of `get_player_season_stats`, keyed by `player_id` -- the
    critic-round MV-11 fix for `media_service.load_event_candidates`'s
    REVENGE_GAME loop, which was calling the single-row form once per
    traded asset (a real N+1 the moment `league_transactions` stops being
    empty). Same "`ANY($1::int[])`, skip the round trip for an empty batch"
    shape as `txn_repo.list_assets_for_transactions` -- this codebase's
    standing parameterization rule, never a caller-built `IN (...)` string.

    A player with no row for this `(league_id, season, game_type)` is
    simply ABSENT from the returned dict, matching `get_career_scoring_to_
    date_for_league`'s own convention (not `get_player_season_stats`'s
    single-row `None`) -- every caller here already does `.get(player_id)`
    and treats a miss as "no stats yet"."""
    if not player_ids:
        return {}
    rows = await ex.fetch(
        f"""
        SELECT {_PLAYER_SEASON_STATS_COLUMNS} FROM player_season_stats
        WHERE league_id = $1 AND player_id = ANY($2::int[]) AND season = $3 AND game_type = $4
        """,
        league_id,
        list(set(player_ids)),
        season,
        game_type.value,
    )
    return {r["player_id"]: _map_player_season_stats(r) for r in rows}


async def list_player_season_stats_for_player(
    ex: Executor, league_id: int, player_id: int
) -> list[PlayerSeasonStats]:
    rows = await ex.fetch(
        f"""
        SELECT {_PLAYER_SEASON_STATS_COLUMNS} FROM player_season_stats
        WHERE league_id = $1 AND player_id = $2
        ORDER BY season, game_type
        """,
        league_id,
        player_id,
    )
    return [_map_player_season_stats(r) for r in rows]


async def get_career_scoring_to_date_for_league(
    ex: Executor, league_id: int, *, season: int, game_type: GameType
) -> dict[int, PlayerCareerScoringToDate]:
    """The first bulk `player_season_stats`-for-a-league loader this
    codebase has ever had (Decision P4-121 item 2, phase-4-media-and-
    player-agency.md §39.1 item 5 — `dba2/media/pulse.py`'s own module
    docstring named this as "whichever future step first wires a story
    kind that genuinely needs a diffed per-player aggregate"'s job, and
    `milestone`'s producer is that step). Shaped after `contract_repo.get_
    committed_salary_for_league_seasons`/`get_dead_cap_totals_for_league_
    seasons` (§39.1 item 6): one `GROUP BY player_id` query, keyed mapping
    out, `int(...)` cast at the `SUM`-returns-`Decimal` boundary, a player
    with no rows in either half simply ABSENT from the returned dict
    rather than present with zeros.

    `career_points_before_season` and `points_in_season`/`games_played_in_
    season` are read in one round trip via conditional aggregation
    (`FILTER (WHERE season < $season)` / `FILTER (WHERE season = $season)`)
    rather than two queries -- both halves are needed by every caller of
    this function (`dba2.media.events.career_scoring_milestone_candidates`'s
    prior-board/current-board split), so there is no real caller for
    either half alone. `game_type`/`league_id` scoped exactly as every
    other `player_season_stats` reader in this file -- a career total that
    silently mixed `REGULAR`/`PLAYOFF` rows would not be the "all-time
    regular-season scoring list" `milestone`'s own templates describe."""
    rows = await ex.fetch(
        """
        SELECT player_id,
               COALESCE(SUM(points) FILTER (WHERE season < $3), 0) AS before_points,
               COALESCE(SUM(points) FILTER (WHERE season = $3), 0) AS season_points,
               COALESCE(SUM(games_played) FILTER (WHERE season = $3), 0) AS season_games
        FROM player_season_stats
        WHERE league_id = $1 AND game_type = $2 AND season <= $3
        GROUP BY player_id
        """,
        league_id,
        game_type.value,
        season,
    )
    return {
        r["player_id"]: PlayerCareerScoringToDate(
            player_id=r["player_id"],
            career_points_before_season=int(r["before_points"]),
            points_in_season=int(r["season_points"]),
            games_played_in_season=int(r["season_games"]),
        )
        for r in rows
    }


async def delete_stats_for_game(ex: Executor, game_id: int) -> None:
    """Decision P1-25: a re-sim deletes this game's box-score and
    period-score rows before re-inserting, inside the same transaction as
    the re-insert (the caller's job, not this function's) — `player_game_stats`
    and `team_game_stats` have composite PKs (`(game_id, player_id)`,
    `(game_id, team_id)`) that reject a second insert with a
    `UniqueViolationError` otherwise, and a game gets simmed dozens of times
    per tuning sweep, not once.

    `game_period_scores` **is** deleted here too — critic round 11 B2 found
    the previous docstring's claim that skipping it "buys it nothing" was
    false: `upsert_game_period_scores`'s `ON CONFLICT (game_id, team_id,
    period) DO UPDATE` only overwrites the `(period, team)` slots the *new*
    sim actually produces, so a re-sim with a different period count (the
    entire reason `overtime_periods` exists as a stored column, §7.2 — a
    game that went to OT on sim 1 and ends in regulation on the re-sim)
    leaves the previous sim's extra period rows behind, silently breaking
    the "period scores sum to the final score" invariant (§9's Tier-4
    acceptance criterion). Deleting it here, before the re-insert, gives it
    the same delete-then-insert shape as the two box-score tables instead
    of leaving it as the one table where a re-sim can under-write.

    `player_season_stats` is untouched — it's the materialized aggregate,
    rebuilt wholesale by `stats_service`, not accumulated per game."""
    await ex.execute("DELETE FROM player_game_stats WHERE game_id = $1", game_id)
    await ex.execute("DELETE FROM team_game_stats WHERE game_id = $1", game_id)
    await ex.execute("DELETE FROM game_period_scores WHERE game_id = $1", game_id)


async def upsert_game_period_scores(
    ex: Executor, period_scores: list[dict]
) -> list[GamePeriodScore]:
    """Bulk upsert via `UNNEST` + `ON CONFLICT DO UPDATE` — a re-sim
    overwrites a game's line score in place, one round trip for all of a
    game's `(period, team)` rows (~8-10/game at 4 periods x 2 teams plus
    overtime) rather than one `INSERT` per row. Each dict: `game_id`,
    `league_id`, `team_id`, `period`, `points`."""
    if not period_scores:
        return []
    result = await ex.fetch(
        f"""
        INSERT INTO game_period_scores (game_id, league_id, team_id, period, points)
        SELECT * FROM UNNEST($1::int[], $2::int[], $3::int[], $4::smallint[], $5::smallint[])
        ON CONFLICT (game_id, team_id, period) DO UPDATE SET points = EXCLUDED.points
        RETURNING {_GAME_PERIOD_SCORE_COLUMNS}
        """,
        [p["game_id"] for p in period_scores],
        [p["league_id"] for p in period_scores],
        [p["team_id"] for p in period_scores],
        [p["period"] for p in period_scores],
        [p["points"] for p in period_scores],
    )
    return [_map_game_period_score(r) for r in result]


async def list_game_period_scores(ex: Executor, game_id: int) -> list[GamePeriodScore]:
    rows = await ex.fetch(
        f"""
        SELECT {_GAME_PERIOD_SCORE_COLUMNS} FROM game_period_scores
        WHERE game_id = $1
        ORDER BY team_id, period
        """,
        game_id,
    )
    return [_map_game_period_score(r) for r in rows]


async def list_player_season_stat_leaders(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    game_type: GameType,
    stat: LeaderStat,
    limit: int = 10,
) -> list[StatLeaderRow]:
    """Phase 5 Stage B step 8's missing `/leaders` read (docs/design/
    phase-5-discord-presentation.md §7.1: "`player_season_stats` exists;
    no leaders query does -- one new repository function"). `stat` selects
    a SQL expression from the fixed `LEADER_STAT_COLUMNS` map above --
    raising `ValidationError` for anything else, the same boundary-
    validation shape `_validate_box_score_dict` already uses in this file
    -- so this can never become a string-concatenation injection surface
    no matter what a caller passes.

    Ties break on `player_id` ascending, so a repeated read (e.g. a golden
    replay, or `/leaders` re-run mid-discussion) is deterministic rather
    than depending on Postgres's own unspecified tie order."""
    column_expr = LEADER_STAT_COLUMNS.get(stat)
    if column_expr is None:
        raise ValidationError(
            f"Unknown LeaderStat {stat!r}; must be one of {sorted(LEADER_STAT_COLUMNS)}"
        )
    rows = await ex.fetch(
        f"""
        SELECT player_id, {column_expr} AS stat_total, games_played
        FROM player_season_stats
        WHERE league_id = $1 AND season = $2 AND game_type = $3
        ORDER BY stat_total DESC, player_id ASC
        LIMIT $4
        """,
        league_id,
        season,
        game_type.value,
        limit,
    )
    return [
        StatLeaderRow(
            player_id=r["player_id"],
            stat_total=r["stat_total"],
            games_played=r["games_played"],
            rank=i,
        )
        for i, r in enumerate(rows, start=1)
    ]


async def list_live_stat_leaders(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    game_type: GameType,
    stat: LeaderStat,
    mode: LeaderMode,
    apply_bar: bool,
    page: int,
    page_size: int = 10,
) -> LiveLeaderPage:
    """docs/design/leaders-v2-per-game-and-paging.md's LD2-1/LD2-2: `/leaders`
    v2's one-statement live read straight off `player_game_stats`, never the
    `player_season_stats` materialized aggregate (leaders-live.md's LD-1,
    unchanged). `LEADER_STAT_COLUMNS` is reused VERBATIM for `{column_expr}`
    (LD2-1: "reused verbatim and unprefixed per leaders-live.md"), and
    `_LEADER_ORDER_EXPRESSIONS[mode]` supplies `{order_expr}` the same
    closed-dict-or-`ValidationError` way.

    The per-game ORDER BY is computed by `ROW_NUMBER() OVER (...)` INSIDE the
    `qualified` CTE, before the `page` CTE's `OFFSET`/`LIMIT` -- LD2-1's
    correctness point: a low-total, high-per-game hot streak is nowhere near
    the top of a totals-ordered `LIMIT`, so limiting before dividing would
    silently omit the real per-game leader.

    **The qualification bar (LD2-2) is per-team, never league-wide.** A
    player qualifies (when `apply_bar` is true) if `games_played >=
    GREATEST(CEIL(0.70 * own_team_games), 1)`, where `own_team_games` is the
    MIN of `team_games`'s own final-game count over every team the player
    has appeared for this season (a traded player never has his count
    measured against a schedule his own games don't support). A full-season
    player's `games_played` always equals his own team's `own_team_games`,
    so `CEIL(0.7n) <= n` for every `n >= 1` -- this can never wipe a
    full-season player, and therefore never wipes a team (LD2-2's proof).
    TOTAL mode passes `apply_bar=False`: a total already encodes playing
    time, so a bar there would only hide real information.

    **`bar b LEFT JOIN page p ON TRUE`, not `FROM page p, bar b` (LD2-1
    [r2/B2]).** `bar` is a bare aggregate over `team_games` with no `GROUP
    BY`, so it always returns exactly one row -- `(NULL, NULL, 0)` for a
    league with no final games. Null-extending an empty `page` onto it
    yields one row with a `NULL` `player_id`; a non-empty `page` yields one
    row per page row, each carrying the SAME bar metadata. Either way,
    `ex.fetch` never returns zero rows here, so `min_team_games`/
    `max_team_games`/`teams_counted` can always be read off row 0 -- the bar
    survives a zero-row page (LD2-6), with no second scalar query.

    Fetches `page_size + 1` rows (`$7`) so `has_more` can be computed without
    a `COUNT`: `has_more = len(page_rows) > page_size` (LD2-4), and the probe
    row is dropped here, before `view_service.get_leaders_panel` resolves any
    name (that is what keeps the click path's per-row N+1 cost at
    `page_size`, not `page_size + 1`, per LD2-3)."""
    column_expr = LEADER_STAT_COLUMNS.get(stat)
    if column_expr is None:
        raise ValidationError(
            f"Unknown LeaderStat {stat!r}; must be one of {sorted(LEADER_STAT_COLUMNS)}"
        )
    order_expr = _LEADER_ORDER_EXPRESSIONS.get(mode)
    if order_expr is None:
        raise ValidationError(
            f"Unknown LeaderMode {mode!r}; must be one of {sorted(_LEADER_ORDER_EXPRESSIONS)}"
        )
    offset = (page - 1) * page_size
    rows = await ex.fetch(
        f"""
        WITH team_games AS (
          SELECT s.team_id, COUNT(*)::int AS team_games_played
          FROM games g
          CROSS JOIN LATERAL (VALUES (g.home_team_id), (g.away_team_id)) AS s(team_id)
          WHERE g.league_id = $1 AND g.season = $2 AND g.game_type = $3 AND g.status = 'final'
          GROUP BY s.team_id
        ), totals AS (
          SELECT pgs.player_id,
                 SUM({column_expr}) AS stat_total,
                 COUNT(*)::int AS games_played,
                 MIN(tg.team_games_played) AS own_team_games
          FROM player_game_stats pgs
          JOIN games g ON g.id = pgs.game_id
          LEFT JOIN team_games tg ON tg.team_id = pgs.team_id
          WHERE pgs.league_id = $1 AND g.season = $2 AND g.game_type = $3 AND g.status = 'final'
          GROUP BY pgs.player_id
        ), qualified AS (
          SELECT t.player_id, t.stat_total, t.games_played, t.own_team_games,
                 ROW_NUMBER() OVER (ORDER BY {order_expr}) AS rank
          FROM totals t
          WHERE t.games_played >= CASE
                  WHEN $4::boolean THEN
                    GREATEST(CEIL($5::numeric * COALESCE(t.own_team_games, t.games_played))::int, 1)
                  ELSE 0
                END
        ), page AS (
          SELECT * FROM qualified ORDER BY rank OFFSET $6 LIMIT $7
        ), bar AS (
          SELECT MIN(team_games_played)::int AS min_team_games,
                 MAX(team_games_played)::int AS max_team_games,
                 COUNT(*)::int               AS teams_counted
          FROM team_games
        )
        SELECT b.min_team_games, b.max_team_games, b.teams_counted,
               p.player_id, p.stat_total, p.games_played, p.own_team_games, p.rank
        FROM bar b
        LEFT JOIN page p ON TRUE
        ORDER BY p.rank
        """,
        league_id,
        season,
        game_type.value,
        apply_bar,
        _QUALIFY_FRACTION,
        offset,
        page_size + 1,
    )
    # `rows` is never empty (see the docstring's B2 proof): row 0 always carries the
    # bar metadata, whether or not any player row survived the join.
    bar_row = rows[0]
    fetched = [
        StatLeaderRow(
            player_id=r["player_id"],
            stat_total=r["stat_total"],
            games_played=r["games_played"],
            rank=r["rank"],
        )
        for r in rows
        if r["player_id"] is not None
    ]
    has_more = len(fetched) > page_size
    return LiveLeaderPage(
        rows=tuple(fetched[:page_size]),
        page=page,
        has_more=has_more,
        bar_applied=apply_bar,
        qualify_fraction=_QUALIFY_FRACTION,
        min_team_games=bar_row["min_team_games"],
        max_team_games=bar_row["max_team_games"],
        teams_counted=bar_row["teams_counted"],
    )


# ---------------------------------------------------------------------------
# docs/design/media-v2-newsroom.md MV-2c/MV-2d/MV-2g -- the media scanner's
# own live-window and style reads. Both reuse `list_live_stat_leaders`'s
# already-proven "live off `player_game_stats`/`team_game_stats`, never the
# materialized `player_season_stats` aggregate" query shape (LD2-1) --
# mid-season a season row is stale or absent (`rebuild_player_season_stats`
# runs only at `REGULAR_SEASON_COMPLETE`).
# ---------------------------------------------------------------------------

_FORM_WINDOW_SUM_COLUMNS: tuple[str, ...] = (
    "fga_rim", "fga_mid", "fga_three", "fgm_three", "fta", "ftm",
    "ast", "stl", "blk", "dreb", "tov",
)
"""The eleven whole-game counting-stat columns `list_recent_form_windows`
sums for both the window and the season -- MV-2c's own field list, minus
`points`/`minutes_played` (summed separately below because they cast to
`float`, never `Decimal`/`int`, per this project's documented asyncpg
`Decimal` trap)."""


async def list_recent_form_windows(
    ex: Executor, *, league_id: int, season: int, game_type: GameType, window: int = 5
) -> list[PlayerFormWindow]:
    """MV-2c's own live-window read: a `ranked` CTE assigns `ROW_NUMBER()
    OVER (PARTITION BY pgs.player_id ORDER BY g.game_date DESC, g.id DESC)`
    over this league-season's FINAL games; `window_agg` sums the box
    columns where `rn <= $window`; `season_agg` sums them all. The pure
    detector layer (`dba2/media/detect/form.py`, a later build step)
    computes `baseline = season - window` by subtraction from these two
    sums -- exact and free, no third CTE.

    A player absent from EITHER aggregate (zero final games this league-
    season) is simply absent from the returned list -- "absent is neutral,
    never a fabricated zero," the same rule `get_career_scoring_to_date_
    for_league` above already follows for the identical reason. `team_id`
    is the player's team as of his most recent game in the window (`rn =
    1`) -- a player traded mid-window still aggregates across both team
    ids in the SUMS (MV-2c's own disclosed rule), but is attributed to one
    team for the fact's own subject."""
    sum_exprs = ", ".join(f"SUM({col})::int AS {col}" for col in _FORM_WINDOW_SUM_COLUMNS)
    rows = await ex.fetch(
        f"""
        WITH ranked AS (
            SELECT pgs.player_id, pgs.team_id, pgs.points, pgs.minutes_played,
                   {", ".join(f"pgs.{col}" for col in _FORM_WINDOW_SUM_COLUMNS)},
                   ROW_NUMBER() OVER (
                       PARTITION BY pgs.player_id ORDER BY g.game_date DESC, g.id DESC
                   ) AS rn
            FROM player_game_stats pgs
            JOIN games g ON g.id = pgs.game_id
            WHERE pgs.league_id = $1 AND g.season = $2 AND g.game_type = $3
                  AND g.status = 'final'
        ), window_agg AS (
            SELECT player_id,
                   (ARRAY_AGG(team_id ORDER BY rn ASC))[1] AS team_id,
                   COUNT(*)::int AS window_games,
                   SUM(points)::float AS window_points,
                   SUM(minutes_played)::float AS window_minutes,
                   {sum_exprs}
            FROM ranked
            WHERE rn <= $4
            GROUP BY player_id
        ), season_agg AS (
            SELECT player_id,
                   COUNT(*)::int AS season_games,
                   SUM(points)::float AS season_points,
                   SUM(minutes_played)::float AS season_minutes,
                   {sum_exprs}
            FROM ranked
            GROUP BY player_id
        )
        SELECT
            w.player_id, w.team_id, w.window_games, s.season_games,
            w.window_points, w.window_minutes,
            {", ".join(f"w.{col} AS window_{col}" for col in _FORM_WINDOW_SUM_COLUMNS)},
            s.season_points, s.season_minutes,
            {", ".join(f"s.{col} AS season_{col}" for col in _FORM_WINDOW_SUM_COLUMNS)}
        FROM window_agg w
        JOIN season_agg s ON s.player_id = w.player_id
        """,
        league_id,
        season,
        game_type.value,
        window,
    )
    return [
        PlayerFormWindow(
            player_id=r["player_id"],
            team_id=r["team_id"],
            window_games=r["window_games"],
            season_games=r["season_games"],
            window_points=r["window_points"],
            window_minutes=r["window_minutes"],
            window_fga_rim=r["window_fga_rim"],
            window_fga_mid=r["window_fga_mid"],
            window_fga_three=r["window_fga_three"],
            window_fgm_three=r["window_fgm_three"],
            window_fta=r["window_fta"],
            window_ftm=r["window_ftm"],
            window_ast=r["window_ast"],
            window_stl=r["window_stl"],
            window_blk=r["window_blk"],
            window_dreb=r["window_dreb"],
            window_tov=r["window_tov"],
            season_points=r["season_points"],
            season_minutes=r["season_minutes"],
            season_fga_rim=r["season_fga_rim"],
            season_fga_mid=r["season_fga_mid"],
            season_fga_three=r["season_fga_three"],
            season_fgm_three=r["season_fgm_three"],
            season_fta=r["season_fta"],
            season_ftm=r["season_ftm"],
            season_ast=r["season_ast"],
            season_stl=r["season_stl"],
            season_blk=r["season_blk"],
            season_dreb=r["season_dreb"],
            season_tov=r["season_tov"],
        )
        for r in rows
    ]


async def list_team_style_aggregates(
    ex: Executor, *, league_id: int, season: int, game_type: GameType
) -> list[TeamStyleAggregate]:
    """MV-2g's own measured-style read, one statement over `team_game_
    stats` joined to `games` (FINAL only), grouped by team.

    `three_share`/`fta_rate` are both denominated on total field-goal
    attempts (`fga_rim + fga_mid + fga_three`), `NULLIF`-guarded against a
    zero-attempt team (a `PRESEASON` team with no games yet is simply
    absent from the result, same "absent is neutral" rule as `list_recent_
    form_windows` above). **`oreb_rate` is a disclosed approximation, not a
    real offensive-rebound percentage** -- a real oreb% needs the
    OPPONENT's `dreb` for the same games, and this codebase persists no
    opponent-shooting/matchup data at all (the same disclosed gap MV-2d's
    own docstring names, RPD-8). This reads `oreb / (oreb + dreb)` -- the
    team's own share of its own total rebounds that were offensive --
    which is directionally useful (a high value means a team crashes the
    glass hard) but is not comparable to a textbook OREB% and must not be
    labeled as one by any later template."""
    rows = await ex.fetch(
        """
        SELECT
            tgs.team_id,
            COUNT(*)::int AS games,
            AVG(tgs.possessions)::float AS avg_possessions,
            SUM(tgs.fga_three)::float
                / NULLIF(SUM(tgs.fga_rim + tgs.fga_mid + tgs.fga_three), 0)::float
                AS three_share,
            SUM(tgs.oreb)::float / NULLIF(SUM(tgs.oreb + tgs.dreb), 0)::float AS oreb_rate,
            SUM(tgs.fta)::float
                / NULLIF(SUM(tgs.fga_rim + tgs.fga_mid + tgs.fga_three), 0)::float
                AS fta_rate,
            (SUM(tgs.tov)::float / NULLIF(SUM(tgs.possessions), 0)::float) * 100 AS tov_per_100
        FROM team_game_stats tgs
        JOIN games g ON g.id = tgs.game_id
        WHERE tgs.league_id = $1 AND g.season = $2 AND g.game_type = $3 AND g.status = 'final'
        GROUP BY tgs.team_id
        """,
        league_id,
        season,
        game_type.value,
    )
    return [
        TeamStyleAggregate(
            team_id=r["team_id"],
            games=r["games"],
            avg_possessions=r["avg_possessions"],
            three_share=r["three_share"],
            oreb_rate=r["oreb_rate"],
            fta_rate=r["fta_rate"],
            tov_per_100=r["tov_per_100"],
        )
        for r in rows
    ]
