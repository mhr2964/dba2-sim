"""player_ratings, player_tendencies, player_ratings_history —
`ratings_repo`'s tables (§5).

phase-0-foundation.md §4.3.2-4.3.4, §5. §5's stated repo contract: bulk
`get_for_team` / `get_for_league` loaders, because Phase 1's possession loop
needs one league-snapshot load, not per-player queries — this is not
optional, it is the module's one hard requirement.

`_RATING_COLUMN_NAMES` is the single source of truth for the 57
rating-block column names, in the same order as migration 002's
`_RATING_COLUMNS` SQL fragment and `domain.ratings._RatingColumns`'s field
order. `upsert_player_ratings`/`create_ratings_history_snapshot` take a
`ratings: dict[str, int]` covering exactly those 57 names (validated
against `_RATING_COLUMN_NAMES` before any SQL runs) rather than 57 separate
keyword parameters — the wholesale-rewrite write pattern the rating block
actually has (progression/generation replace the whole set at once, per
Decision 10) makes a dict the honest shape for the write side; the read
side still selects an explicit column list, never `SELECT *`.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import asyncpg

from dba2.core.errors import ValidationError
from dba2.data.db import Executor
from dba2.domain.ratings import (
    PlayerRatings,
    PlayerRatingsHistory,
    PlayerTendencies,
    RatingHistoryReason,
    RatingSource,
)

_RATING_COLUMN_NAMES: tuple[str, ...] = (
    # physical / athletic
    "speed", "acceleration", "lateral_quickness", "strength", "vertical",
    "stamina", "durability",
    # on-ball creation (beat 2b, offense)
    "handle", "ball_security", "first_step", "change_of_direction",
    "screen_use", "draw_foul",
    # finishing (beats 3 and 5, offense)
    "finishing_rim", "contact_finishing", "floater", "post_scoring",
    # shooting, split by catch vs. off-dribble (beat 5)
    "catch_shoot_3", "off_dribble_3", "catch_shoot_mid", "off_dribble_mid",
    "free_throw",
    # playmaking (beats 3, 4)
    "passing_vision", "passing_accuracy", "pass_under_pressure",
    "short_roll_passing",
    # screening (beat 2a, offense)
    "screen_setting", "screen_timing", "roll_ability", "catch_hands",
    # off-ball offense (beat 4; spacing aggregate)
    "off_ball_movement", "relocation", "cutting", "offensive_rebounding",
    # on-ball defense (beat 2b, defense)
    "on_ball_defense", "hands", "post_defense", "foul_discipline",
    # team / help defense (beats 2a, 3, 4, 5)
    "screen_navigation", "help_iq", "rotation_speed", "rim_protection",
    "closeout_speed", "closeout_control", "scheme_discipline",
    "switchability", "defensive_rebounding",
    # mental
    "basketball_iq", "composure", "consistency", "motor", "coachability",
    # development (Pillar 7 / progression)
    "potential", "potential_variance", "peak_age_start", "peak_age_end",
    "work_ethic",
)

_PLAYER_RATINGS_COLUMNS = (
    "player_id, league_id, source, derived_from_season, confidence, updated_at, "
    + ", ".join(_RATING_COLUMN_NAMES)
)

_PLAYER_RATINGS_HISTORY_COLUMNS = (
    "player_id, season, league_id, reason, recorded_at, "
    + ", ".join(_RATING_COLUMN_NAMES)
)

_PLAYER_TENDENCIES_COLUMNS = """
    player_id, league_id, updated_at,
    shot_freq_rim, shot_freq_mid, shot_freq_three, shot_freq_post,
    drive_freq, pullup_vs_catch, iso_freq, pnr_ball_freq, pnr_screen_freq,
    transition_freq, pass_freq,
    spot_up_freq, cut_freq, crash_oreb_freq,
    help_gamble_freq, steal_gamble_freq, foul_freq
"""


def _validate_ratings_dict(ratings: dict[str, int]) -> None:
    given = set(ratings.keys())
    expected = set(_RATING_COLUMN_NAMES)
    if given != expected:
        missing = expected - given
        extra = given - expected
        raise ValidationError(
            f"ratings dict does not match the 57-column rating block "
            f"(missing={sorted(missing)}, extra={sorted(extra)})"
        )


def _rating_kwargs(r: asyncpg.Record) -> dict[str, int]:
    return {name: r[name] for name in _RATING_COLUMN_NAMES}


def _map_player_ratings(r: asyncpg.Record) -> PlayerRatings:
    return PlayerRatings(
        player_id=r["player_id"],
        league_id=r["league_id"],
        source=RatingSource(r["source"]),
        derived_from_season=r["derived_from_season"],
        confidence=r["confidence"],
        updated_at=r["updated_at"],
        **_rating_kwargs(r),
    )


def _map_player_ratings_history(r: asyncpg.Record) -> PlayerRatingsHistory:
    return PlayerRatingsHistory(
        player_id=r["player_id"],
        season=r["season"],
        league_id=r["league_id"],
        reason=RatingHistoryReason(r["reason"]),
        recorded_at=r["recorded_at"],
        **_rating_kwargs(r),
    )


def _map_player_tendencies(r: asyncpg.Record) -> PlayerTendencies:
    return PlayerTendencies(
        player_id=r["player_id"],
        league_id=r["league_id"],
        updated_at=r["updated_at"],
        shot_freq_rim=r["shot_freq_rim"],
        shot_freq_mid=r["shot_freq_mid"],
        shot_freq_three=r["shot_freq_three"],
        shot_freq_post=r["shot_freq_post"],
        drive_freq=r["drive_freq"],
        pullup_vs_catch=r["pullup_vs_catch"],
        iso_freq=r["iso_freq"],
        pnr_ball_freq=r["pnr_ball_freq"],
        pnr_screen_freq=r["pnr_screen_freq"],
        transition_freq=r["transition_freq"],
        pass_freq=r["pass_freq"],
        spot_up_freq=r["spot_up_freq"],
        cut_freq=r["cut_freq"],
        crash_oreb_freq=r["crash_oreb_freq"],
        help_gamble_freq=r["help_gamble_freq"],
        steal_gamble_freq=r["steal_gamble_freq"],
        foul_freq=r["foul_freq"],
    )


async def upsert_player_ratings(
    ex: Executor,
    *,
    player_id: int,
    league_id: int,
    source: RatingSource,
    ratings: dict[str, int],
    derived_from_season: int | None = None,
    confidence: Decimal = Decimal("1.00"),
) -> PlayerRatings:
    """Wholesale replace — the only write pattern the rating block has
    (Decision 10: rewritten wholesale at progression time)."""
    _validate_ratings_dict(ratings)
    values = [ratings[name] for name in _RATING_COLUMN_NAMES]
    rating_placeholders = ", ".join(f"${i + 6}" for i in range(len(_RATING_COLUMN_NAMES)))
    rating_update_clause = ", ".join(
        f"{name} = EXCLUDED.{name}" for name in _RATING_COLUMN_NAMES
    )
    row = await ex.fetchrow(
        f"""
        INSERT INTO player_ratings
            (player_id, league_id, source, derived_from_season, confidence,
             updated_at, {", ".join(_RATING_COLUMN_NAMES)})
        VALUES ($1, $2, $3, $4, $5, NOW(), {rating_placeholders})
        ON CONFLICT (player_id) DO UPDATE SET
            league_id = EXCLUDED.league_id,
            source = EXCLUDED.source,
            derived_from_season = EXCLUDED.derived_from_season,
            confidence = EXCLUDED.confidence,
            updated_at = NOW(),
            {rating_update_clause}
        RETURNING {_PLAYER_RATINGS_COLUMNS}
        """,
        player_id,
        league_id,
        source.value,
        derived_from_season,
        confidence,
        *values,
    )
    return _map_player_ratings(row)


async def get_player_ratings(ex: Executor, player_id: int) -> PlayerRatings | None:
    row = await ex.fetchrow(
        f"SELECT {_PLAYER_RATINGS_COLUMNS} FROM player_ratings WHERE player_id = $1",
        player_id,
    )
    return _map_player_ratings(row) if row is not None else None


async def get_for_team(ex: Executor, team_id: int) -> list[PlayerRatings]:
    """Bulk loader — §5's stated repo contract. Joins `players` only to
    filter by `team_id` (not owned here); every selected column is
    `player_ratings`'s own."""
    rows = await ex.fetch(
        f"""
        SELECT {", ".join(f"pr.{c}" for c in _PLAYER_RATINGS_COLUMNS.replace(" ", "").split(","))}
        FROM player_ratings pr
        JOIN players p ON p.id = pr.player_id
        WHERE p.team_id = $1
        """,
        team_id,
    )
    return [_map_player_ratings(r) for r in rows]


async def get_for_league(ex: Executor, league_id: int) -> list[PlayerRatings]:
    """Bulk loader — §5's stated repo contract. Single league-snapshot
    query for Phase 1's possession-loop sim batch."""
    rows = await ex.fetch(
        f"SELECT {_PLAYER_RATINGS_COLUMNS} FROM player_ratings WHERE league_id = $1",
        league_id,
    )
    return [_map_player_ratings(r) for r in rows]


async def create_ratings_history_snapshot(
    ex: Executor,
    *,
    player_id: int,
    season: int,
    league_id: int,
    reason: RatingHistoryReason,
    ratings: dict[str, int],
    recorded_at: datetime | None = None,
) -> PlayerRatingsHistory:
    _validate_ratings_dict(ratings)
    values = [ratings[name] for name in _RATING_COLUMN_NAMES]
    rating_placeholders = ", ".join(f"${i + 6}" for i in range(len(_RATING_COLUMN_NAMES)))
    row = await ex.fetchrow(
        f"""
        INSERT INTO player_ratings_history
            (player_id, season, league_id, reason, recorded_at,
             {", ".join(_RATING_COLUMN_NAMES)})
        VALUES ($1, $2, $3, $4, COALESCE($5, NOW()), {rating_placeholders})
        RETURNING {_PLAYER_RATINGS_HISTORY_COLUMNS}
        """,
        player_id,
        season,
        league_id,
        reason.value,
        recorded_at,
        *values,
    )
    return _map_player_ratings_history(row)


async def get_ratings_history_for_player(
    ex: Executor, player_id: int
) -> list[PlayerRatingsHistory]:
    rows = await ex.fetch(
        f"""
        SELECT {_PLAYER_RATINGS_HISTORY_COLUMNS} FROM player_ratings_history
        WHERE player_id = $1
        ORDER BY season
        """,
        player_id,
    )
    return [_map_player_ratings_history(r) for r in rows]


async def upsert_player_tendencies(
    ex: Executor,
    *,
    player_id: int,
    league_id: int,
    shot_freq_rim: int,
    shot_freq_mid: int,
    shot_freq_three: int,
    shot_freq_post: int,
    drive_freq: int,
    pullup_vs_catch: int,
    iso_freq: int,
    pnr_ball_freq: int,
    pnr_screen_freq: int,
    transition_freq: int,
    pass_freq: int,
    spot_up_freq: int,
    cut_freq: int,
    crash_oreb_freq: int,
    help_gamble_freq: int,
    steal_gamble_freq: int,
    foul_freq: int,
) -> PlayerTendencies:
    row = await ex.fetchrow(
        f"""
        INSERT INTO player_tendencies
            (player_id, league_id, updated_at,
             shot_freq_rim, shot_freq_mid, shot_freq_three, shot_freq_post,
             drive_freq, pullup_vs_catch, iso_freq, pnr_ball_freq,
             pnr_screen_freq, transition_freq, pass_freq,
             spot_up_freq, cut_freq, crash_oreb_freq,
             help_gamble_freq, steal_gamble_freq, foul_freq)
        VALUES ($1, $2, NOW(), $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                $14, $15, $16, $17, $18, $19)
        ON CONFLICT (player_id) DO UPDATE SET
            league_id = EXCLUDED.league_id,
            updated_at = NOW(),
            shot_freq_rim = EXCLUDED.shot_freq_rim,
            shot_freq_mid = EXCLUDED.shot_freq_mid,
            shot_freq_three = EXCLUDED.shot_freq_three,
            shot_freq_post = EXCLUDED.shot_freq_post,
            drive_freq = EXCLUDED.drive_freq,
            pullup_vs_catch = EXCLUDED.pullup_vs_catch,
            iso_freq = EXCLUDED.iso_freq,
            pnr_ball_freq = EXCLUDED.pnr_ball_freq,
            pnr_screen_freq = EXCLUDED.pnr_screen_freq,
            transition_freq = EXCLUDED.transition_freq,
            pass_freq = EXCLUDED.pass_freq,
            spot_up_freq = EXCLUDED.spot_up_freq,
            cut_freq = EXCLUDED.cut_freq,
            crash_oreb_freq = EXCLUDED.crash_oreb_freq,
            help_gamble_freq = EXCLUDED.help_gamble_freq,
            steal_gamble_freq = EXCLUDED.steal_gamble_freq,
            foul_freq = EXCLUDED.foul_freq
        RETURNING {_PLAYER_TENDENCIES_COLUMNS}
        """,
        player_id,
        league_id,
        shot_freq_rim,
        shot_freq_mid,
        shot_freq_three,
        shot_freq_post,
        drive_freq,
        pullup_vs_catch,
        iso_freq,
        pnr_ball_freq,
        pnr_screen_freq,
        transition_freq,
        pass_freq,
        spot_up_freq,
        cut_freq,
        crash_oreb_freq,
        help_gamble_freq,
        steal_gamble_freq,
        foul_freq,
    )
    return _map_player_tendencies(row)


async def get_player_tendencies(ex: Executor, player_id: int) -> PlayerTendencies | None:
    row = await ex.fetchrow(
        f"SELECT {_PLAYER_TENDENCIES_COLUMNS} FROM player_tendencies WHERE player_id = $1",
        player_id,
    )
    return _map_player_tendencies(row) if row is not None else None


async def get_tendencies_for_league(ex: Executor, league_id: int) -> list[PlayerTendencies]:
    """Bulk loader mirroring `get_for_league` (phase-1-simulation-engine.md
    §8.2) — only `get_player_tendencies` (per-player) existed before this,
    which would be an N+1 across a ~450-player league snapshot; the
    possession loop needs one league-snapshot load, matching this module's
    stated bulk-loader contract."""
    rows = await ex.fetch(
        f"SELECT {_PLAYER_TENDENCIES_COLUMNS} FROM player_tendencies WHERE league_id = $1",
        league_id,
    )
    return [_map_player_tendencies(r) for r in rows]
