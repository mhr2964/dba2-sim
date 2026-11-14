"""`records_repo` — franchise + all-time records and leaderboards (§5).

phase-0-foundation.md §4.7, §5: "reads stats tables, owns no writes to
them." This module has no table of its own and no migration — every query
here is a read against `stats_repo`'s `player_season_stats` table, returned
as `stats_repo`'s own `PlayerSeasonStats` dataclass (§5: "mappers return
`dba2.domain` dataclasses," and the prompt's explicit constraint for this
module — no new dataclass invented here). It imports `stats_repo`'s
`_BOX_SCORE_COLUMN_NAMES` rather than hand-listing the 16 box columns a
third time (mirroring `ratings_repo`'s column-sharing pattern, extended
across the module boundary the same way the existing test suite already
reaches into `ratings_repo._RATING_COLUMN_NAMES`).

A player's full season-stats log (the raw material for "career totals" —
summed client-side, since a summed career line isn't a real `(season,
game_type)` row and forcing it into `PlayerSeasonStats` would misrepresent
those two fields, so no module fabricates one) is `stats_repo`'s own
`list_player_season_stats_for_player` — that query is a plain per-player
read against the table `stats_repo` owns, not a "record" or "leaderboard"
in any sense, so it lives there and only there (Phase 0 step-7 critic
round, B2: this module originally duplicated that exact query under a
different name and a divergent keyword-only signature — deleted). This
module stays to the one query shape that's genuinely about records/
leaderboards: a top-N-by-stat leaderboard.

`stat` is a caller-supplied column name interpolated into `ORDER BY` — it is
validated against `_LEADERBOARD_STAT_COLUMNS` before ever reaching SQL,
per §5's "explicit column allowlists for any dynamic ORDER BY."
"""
from __future__ import annotations

from dba2.core.errors import ValidationError
from dba2.data.db import Executor
from dba2.data.repositories.stats_repo import (
    _BOX_SCORE_COLUMN_NAMES,
    _PLAYER_SEASON_STATS_COLUMNS,
    _map_player_season_stats,
)
from dba2.domain.game import GameType
from dba2.domain.stats import PlayerSeasonStats

_LEADERBOARD_STAT_COLUMNS: frozenset[str] = frozenset(_BOX_SCORE_COLUMN_NAMES) | {
    "games_played",
    "minutes_played",
}


async def list_top_player_season_stat_for_league(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    game_type: GameType,
    stat: str,
    limit: int = 10,
) -> list[PlayerSeasonStats]:
    """Top-`limit` players by a single materialized season-stat column, for
    one league/season/game_type. `stat` must be one of the 16 box-score
    columns or `games_played`/`minutes_played` — anything else raises
    `ValidationError` before the query runs."""
    if stat not in _LEADERBOARD_STAT_COLUMNS:
        raise ValidationError(
            f"'{stat}' is not a valid leaderboard stat column "
            f"(allowed={sorted(_LEADERBOARD_STAT_COLUMNS)})"
        )
    rows = await ex.fetch(
        f"""
        SELECT {_PLAYER_SEASON_STATS_COLUMNS} FROM player_season_stats
        WHERE league_id = $1 AND season = $2 AND game_type = $3
        ORDER BY {stat} DESC, player_id
        LIMIT $4
        """,
        league_id,
        season,
        game_type.value,
        limit,
    )
    return [_map_player_season_stats(r) for r in rows]
