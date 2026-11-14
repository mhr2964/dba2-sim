"""games — `game_repo`'s table (§5: "games, schedule queries").
`game_period_scores` is a `stats_repo` table (line-score stat data, §8.2's
module assignment) even though it FKs to `games` — see `stats_repo.py`'s
module docstring, not this one.

phase-0-foundation.md §4.7. Phase 0 was schema-only for `games`: nothing
wrote a score or flipped `status`/`simmed_at` yet. phase-1-simulation-engine.md
§8.2 is the business logic that now owns that: `set_status` (validated by
the caller against `entity_states.GAME_TRANSITIONS` before it's ever
called, per Decision P1-26), `finalize_game` (a second writer of
`games.status`, so unlike `set_status` it cannot rely on the caller —
it enforces `GAME_TRANSITIONS`' one legal edge into `FINAL`
(`IN_PROGRESS -> FINAL`) itself, without importing `entity_states`
(`data/` may not import `phase/`, Decision 4) — see its docstring),
`list_scheduled_games`, and `create_games_many` for the bulk schedule
insert.

`(league_id, season, game_index)` is UNIQUE (migration 006), which is what
makes `list_games_for_league_season`'s `ORDER BY game_index` deterministic
on its own; the `id` tiebreak added there and on
`list_games_for_team_season` is defensive, matching §5's "add a tiebreak so
ties can't come back in undefined order" rule rather than relying on a
constraint the query itself doesn't filter tightly enough to guarantee (a
team's `game_date` isn't DB-enforced unique).

`list_games_for_team_season` gained a required `league_id` keyword-only
parameter in migration 008's companion change (Amendment 1, critic B4): the
`games (league_id, season, home_team_id/away_team_id)` indexes (migration
008) have an unconstrained leading column and are never chosen by the
planner without it — this is a breaking signature change, not purely
additive (phase-1-simulation-engine.md §8.2).
"""
from __future__ import annotations

from datetime import date, datetime

import asyncpg

from dba2.core.errors import ValidationError
from dba2.data.db import Executor
from dba2.domain.game import Game, GameStatus, GameType

_GAME_COLUMNS = """
    id, league_id, season, game_index, game_date, home_team_id, away_team_id,
    home_score, away_score, status, game_type, rng_seed, simmed_at,
    engine_version, roster_digest, overtime_periods
"""

_CREATE_GAMES_MANY_FIELDS: tuple[str, ...] = (
    "league_id",
    "season",
    "game_index",
    "game_date",
    "home_team_id",
    "away_team_id",
    "game_type",
    "rng_seed",
)


def _map_game(r: asyncpg.Record) -> Game:
    return Game(
        id=r["id"],
        league_id=r["league_id"],
        season=r["season"],
        game_index=r["game_index"],
        game_date=r["game_date"],
        home_team_id=r["home_team_id"],
        away_team_id=r["away_team_id"],
        home_score=r["home_score"],
        away_score=r["away_score"],
        status=GameStatus(r["status"]),
        game_type=GameType(r["game_type"]),
        rng_seed=r["rng_seed"],
        simmed_at=r["simmed_at"],
        engine_version=r["engine_version"],
        roster_digest=r["roster_digest"],
        overtime_periods=r["overtime_periods"],
    )


async def create_game(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    game_index: int,
    game_date,
    home_team_id: int,
    away_team_id: int,
    game_type: GameType,
    rng_seed: int,
    status: GameStatus = GameStatus.SCHEDULED,
    home_score: int | None = None,
    away_score: int | None = None,
) -> Game:
    row = await ex.fetchrow(
        f"""
        INSERT INTO games
            (league_id, season, game_index, game_date, home_team_id,
             away_team_id, home_score, away_score, status, game_type, rng_seed)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING {_GAME_COLUMNS}
        """,
        league_id,
        season,
        game_index,
        game_date,
        home_team_id,
        away_team_id,
        home_score,
        away_score,
        status.value,
        game_type.value,
        rng_seed,
    )
    return _map_game(row)


async def get_game(ex: Executor, game_id: int) -> Game | None:
    row = await ex.fetchrow(f"SELECT {_GAME_COLUMNS} FROM games WHERE id = $1", game_id)
    return _map_game(row) if row is not None else None


async def list_games_for_league_season(
    ex: Executor, league_id: int, season: int
) -> list[Game]:
    rows = await ex.fetch(
        f"""
        SELECT {_GAME_COLUMNS} FROM games
        WHERE league_id = $1 AND season = $2
        ORDER BY game_index, id
        """,
        league_id,
        season,
    )
    return [_map_game(r) for r in rows]


async def list_games_for_team_season(
    ex: Executor, team_id: int, season: int, *, league_id: int
) -> list[Game]:
    """`league_id` is required and keyword-only — a breaking signature
    change (Amendment 1, critic B4), not purely additive: critic round 12
    measured this query doing a sequential scan (887 buffer hits vs. 21),
    and the `ix_games_league_season_home_team`/`_away_team` indexes
    (migration 008) have an unconstrained leading column and are never
    chosen by the planner without a `league_id` predicate."""
    rows = await ex.fetch(
        f"""
        SELECT {_GAME_COLUMNS} FROM games
        WHERE league_id = $1 AND (home_team_id = $2 OR away_team_id = $2) AND season = $3
        ORDER BY game_date, id
        """,
        league_id,
        team_id,
        season,
    )
    return [_map_game(r) for r in rows]


async def set_status(
    ex: Executor, game_id: int, *, from_status: GameStatus, to_status: GameStatus
) -> bool:
    """Decision P1-26: legality of `from_status -> to_status` is checked by
    the caller (a service) against `entity_states.GAME_TRANSITIONS` before
    this is ever called — this function does not re-check it. `AND status =
    $2` is a guard against a stale `from_status` (a concurrent write already
    moved the row), not a legality check: the same no-op-on-mismatch pattern
    `contract_repo.terminate_contract`/`draft_repo.complete_lottery`/
    `injury_repo.resolve_injury` already use (Phase 0 step-7 critic round,
    B1's bug class) — a mismatched guard is a silent no-op, not an error,
    because at this layer there is no way to distinguish "stale caller" from
    "already applied by someone else".

    Returns whether a row moved. Most callers ignore it (the sim's own
    `in_progress` write treats a mismatch as somebody else's move); a caller that
    has to tell its user "nothing happened" -- Stage E's postpone/restore -- reads
    it, since the silent no-op is otherwise indistinguishable from success."""
    row = await ex.fetchrow(
        "UPDATE games SET status = $3 WHERE id = $1 AND status = $2 RETURNING id",
        game_id,
        from_status.value,
        to_status.value,
    )
    return row is not None


async def finalize_game(
    ex: Executor,
    game_id: int,
    *,
    home_score: int,
    away_score: int,
    overtime_periods: int,
    engine_version: int,
    roster_digest: str | None,
    simmed_at: datetime | None = None,
) -> None:
    """Writes the finalized score plus engine/replay metadata and flips
    `status` to `final`. This IS a second writer of `games.status` besides
    `set_status`, including on the normal `sim_game` flow: `set_status`
    moves the row to `in_progress`, but the later `in_progress -> final`
    write belongs to this function, not `set_status` (§7.2's "the only
    writer" line describes the intent, not this implementation — corrected
    here rather than left to mislead the next reader of this function).
    Since `finalize_game` is a real writer of this column, it enforces its
    own legality check below rather than trusting that a caller only ever
    reaches it from a legal state.

    Critic round 11 B1 reproduced a fresh `scheduled` game jumping straight
    to `final` through this function with no check at all — a transition
    `entity_states.GAME_TRANSITIONS`/`test_entity_states.py:50` both
    declare illegal. The obvious fix would call
    `entity_states.is_legal_transition(GAME_TRANSITIONS, ...)` directly,
    but `data/` may not import `phase/` (Decision 4's layering rule,
    `tests/unit/test_layering.py` — `set_status`'s own docstring already
    prose-documents `GAME_TRANSITIONS` without importing it, for the same
    reason). So this checks the one fact that matters without importing
    the table: per `GAME_TRANSITIONS`, `IN_PROGRESS` is the *only* legal
    source status for `-> FINAL`. Reads the current status first: no-ops
    (does not raise) if already `final` — the same no-op-on-repeat guard
    `terminate_contract`/`complete_lottery`/`resolve_injury` use, since a
    stray second call on an already-final game is a legitimate repeat, not
    a caller error — but raises `ValidationError` for any other non-
    `in_progress` status, since that *is* a caller skipping straight to
    `final` outside the state machine. The write's own `WHERE status =
    'in_progress'` clause is a compare-and-swap guard against a concurrent
    write racing between the read and this write (same stale-guard
    semantics `set_status` uses), not a second legality check — if the row
    changed between the two statements, the `UPDATE` silently no-ops
    rather than corrupting a status a concurrent caller already moved."""
    current = await ex.fetchrow("SELECT status FROM games WHERE id = $1", game_id)
    if current is None:
        return  # no such game — matches the prior silent no-op-on-missing-row behavior
    current_status = GameStatus(current["status"])
    if current_status == GameStatus.FINAL:
        return  # already final — idempotent no-op, not a silent overwrite
    if current_status != GameStatus.IN_PROGRESS:
        raise ValidationError(
            f"Cannot finalize game {game_id}: status is {current_status.value}, not "
            "in_progress — GAME_TRANSITIONS only allows IN_PROGRESS -> FINAL"
        )
    await ex.execute(
        """
        UPDATE games
        SET home_score = $2, away_score = $3, overtime_periods = $4,
            engine_version = $5, roster_digest = $6, status = 'final',
            simmed_at = COALESCE($7, NOW())
        WHERE id = $1 AND status = 'in_progress'
        """,
        game_id,
        home_score,
        away_score,
        overtime_periods,
        engine_version,
        roster_digest,
        simmed_at,
    )


async def list_scheduled_games(
    ex: Executor, league_id: int, season: int, game_type: GameType
) -> list[Game]:
    """`status = 'scheduled'` only — the batch-sim entry point's input
    query (§8.3: `game_sim_service.sim_scheduled_games`), not a generic
    status filter."""
    rows = await ex.fetch(
        f"""
        SELECT {_GAME_COLUMNS} FROM games
        WHERE league_id = $1 AND season = $2 AND game_type = $3 AND status = $4
        ORDER BY game_index, id
        """,
        league_id,
        season,
        game_type.value,
        GameStatus.SCHEDULED.value,
    )
    return [_map_game(r) for r in rows]


async def get_max_game_index(
    ex: Executor, league_id: int, season: int, game_type: GameType
) -> int | None:
    """The highest `game_index` of ANY status for the `(league, season, game_type)`,
    or `None` when there is no schedule. An aggregate over the unique
    `(league_id, season, game_index)` index, so a postponement never moves a games
    target the way it would if the maximum were taken over scheduled games only."""
    return await ex.fetchval(
        "SELECT MAX(game_index) FROM games WHERE league_id = $1 AND season = $2 AND game_type = $3",
        league_id,
        season,
        game_type.value,
    )


async def list_scheduled_game_keys(
    ex: Executor,
    league_id: int,
    season: int,
    game_type: GameType,
    *,
    through_game_index: int,
) -> list[tuple[int, int]]:
    """`(id, game_index)` of the `scheduled` games at or below `through_game_index`,
    in schedule order -- the exit gate's whole need of a game. A projection, not
    `list_scheduled_games`: the gate is evaluated on every press and a block press
    is one of ~144 a season, so it neither maps a full `Game` per row nor reads the
    games past the phase's own target."""
    rows = await ex.fetch(
        """
        SELECT id, game_index FROM games
        WHERE league_id = $1 AND season = $2 AND game_type = $3 AND status = $4
              AND game_index <= $5
        ORDER BY game_index, id
        """,
        league_id,
        season,
        game_type.value,
        GameStatus.SCHEDULED.value,
        through_game_index,
    )
    return [(r["id"], r["game_index"]) for r in rows]


async def get_next_scheduled_game_day(
    ex: Executor, league_id: int, season: int, game_type: GameType
) -> tuple[date, int, int] | None:
    """The earliest unplayed `game_date` among `scheduled` games, as `(game_date,
    first_game_index, last_game_index)` over the scheduled games sharing it -- what
    a block press needs to bound its sim (P5-99.i), in two aggregate reads instead of
    the whole schedule. `None` when nothing is scheduled. `game_index` is
    date-ordered (A-24), so the first scheduled game in index order is on the
    earliest date."""
    first = await ex.fetchrow(
        """
        SELECT game_date FROM games
        WHERE league_id = $1 AND season = $2 AND game_type = $3 AND status = $4
        ORDER BY game_index, id
        LIMIT 1
        """,
        league_id,
        season,
        game_type.value,
        GameStatus.SCHEDULED.value,
    )
    if first is None:
        return None
    day = await ex.fetchrow(
        """
        SELECT MIN(game_index) AS first_index, MAX(game_index) AS last_index FROM games
        WHERE league_id = $1 AND season = $2 AND game_type = $3 AND status = $4
              AND game_date = $5
        """,
        league_id,
        season,
        game_type.value,
        GameStatus.SCHEDULED.value,
        first["game_date"],
    )
    return first["game_date"], day["first_index"], day["last_index"]


async def list_final_game_ids_after_date(
    ex: Executor, league_id: int, season: int, game_type: GameType, game_date: date
) -> list[int]:
    """Ids of the `final` games of the `(league, season, game_type)` on a date STRICTLY
    LATER than `game_date`, in schedule order: the evidence that the league has played
    past `game_date`. A restore of a postponed game reads it (P5-99.u.1 refusal 3); a
    final game ON `game_date` is deliberately not evidence -- a date the block press
    half-played is where the block cursor still stands."""
    rows = await ex.fetch(
        """
        SELECT id FROM games
        WHERE league_id = $1 AND season = $2 AND game_type = $3 AND status = $4
              AND game_date > $5
        ORDER BY game_index, id
        """,
        league_id,
        season,
        game_type.value,
        GameStatus.FINAL.value,
        game_date,
    )
    return [r["id"] for r in rows]


def _create_games_many_value(game: dict, field: str):
    return game[field].value if field == "game_type" else game[field]


async def create_games_many(ex: Executor, games: list[dict]) -> list[Game]:
    """Bulk insert via `UNNEST` — one round trip for a whole generated
    schedule (1,230 rows/season) instead of 1,230 single-row `INSERT`s
    (§8.2, §11's persistence-budget reasoning). Every dict must carry
    exactly `_CREATE_GAMES_MANY_FIELDS`; `status` is always `scheduled` and
    `home_score`/`away_score` are always `NULL` — a freshly generated
    schedule has no result yet, so neither is a bulk-insert parameter."""
    if not games:
        return []
    for g in games:
        given = set(g.keys())
        expected = set(_CREATE_GAMES_MANY_FIELDS)
        if given != expected:
            raise ValidationError(
                f"game dict does not match the {len(_CREATE_GAMES_MANY_FIELDS)}-field "
                f"create_games_many schema (missing={sorted(expected - given)}, "
                f"extra={sorted(given - expected)})"
            )
    rows = await ex.fetch(
        f"""
        INSERT INTO games
            (league_id, season, game_index, game_date, home_team_id,
             away_team_id, game_type, rng_seed, status)
        SELECT league_id, season, game_index, game_date, home_team_id,
               away_team_id, game_type, rng_seed, 'scheduled'
        FROM UNNEST(
            $1::int[], $2::int[], $3::int[], $4::date[],
            $5::int[], $6::int[], $7::text[], $8::bigint[]
        ) AS t(league_id, season, game_index, game_date, home_team_id,
               away_team_id, game_type, rng_seed)
        RETURNING {_GAME_COLUMNS}
        """,
        *(
            [_create_games_many_value(g, field) for g in games]
            for field in _CREATE_GAMES_MANY_FIELDS
        ),
    )
    return [_map_game(r) for r in rows]
