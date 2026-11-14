"""`rebuild_player_season_stats` -- the materialized `player_season_stats`
aggregate §4.7 promised and nothing wrote before this (phase-1-simulation-
engine.md §8.3).

**Rebuilt wholesale, not accumulated per game** -- §8.3's own text: "matching
`upsert_player_ratings`'s wholesale pattern." `ratings_repo.upsert_player_ratings`
rewrites every rating column on conflict (`ON CONFLICT ... DO UPDATE SET
<every column> = EXCLUDED.<every column>`), never accumulates in place; this
function recomputes a player's full season line from scratch (summing every
`player_game_stats` row for the league/season/game_type) and writes it via
`stats_repo.upsert_player_season_stats`, which already has that identical
"every column, EXCLUDED" wholesale-overwrite shape. So a re-sim (Decision
P1-25 deletes and re-inserts a game's `player_game_stats` rows) can never
leave this aggregate drifted: the next `rebuild_player_season_stats` call
recomputes every touched player's total from the post-re-sim rows, not from
whatever the aggregate previously held.

**Delete-then-reconcile, not upsert-only (critic round 19 B19-1):** an
earlier version of this function only upserted rows for players present in
the newly-computed aggregate and never deleted anyone else's, on the
now-disproven claim that "a re-sim replaces a game's rows for the same
roster" so a player's row count within a season could never shrink to zero.
That is false: `game_sim_service.build_context` re-reads the *live*
`players` table and filters to `roster_status == ACTIVE` at re-sim time
(not the roster that was active during the original sim), so a roster
change between a sim and its re-sim (e.g. a player marked `injured`) can
drop a player out of a game's aggregate entirely. Reproduced live: sim a
game, mark a participant `injured`, resim the same game -- the player's
`player_game_stats` row is gone (roster-sensitive delete-then-reinsert,
Decision P1-25), but their `player_season_stats` row from before the
roster change survived untouched, since the upsert-only rebuild never wrote
to nor deleted it. This function now calls
`stats_repo.delete_stale_player_season_stats` after the upserts, inside the
same `transaction(ex)`, to remove any `player_season_stats` row for this
league/season/game_type whose player is not in the newly-computed
aggregate's survivor set -- including the all-players-gone case, where the
survivor set is empty and every existing row for the league/season/
game_type is deleted.
"""
from __future__ import annotations

import dataclasses
from collections import defaultdict
from decimal import Decimal

from dba2.data.db import Executor, transaction
from dba2.data.repositories import game_repo, stats_repo
from dba2.domain.game import GameStatus, GameType
from dba2.domain.stats import PlayerGameStats

# `PlayerGameStats`'s non-box-score identity fields -- everything else on
# the dataclass is exactly the 16-column box-score block
# `stats_repo._validate_box_score_dict` checks against (mirrors
# `game_sim_service._box_dict`'s identical derive-don't-hardlist reasoning).
_GAME_STATS_IDENTITY_FIELDS = frozenset(
    {"game_id", "league_id", "player_id", "team_id", "minutes_played", "plus_minus"}
)
_BOX_SCORE_FIELD_NAMES: tuple[str, ...] = tuple(
    f.name for f in dataclasses.fields(PlayerGameStats) if f.name not in _GAME_STATS_IDENTITY_FIELDS
)


async def rebuild_player_season_stats(
    ex: Executor, league_id: int, season: int, game_type: GameType
) -> int:
    """Recomputes and upserts one `player_season_stats` row per player who
    has at least one `player_game_stats` row across every `final` game of
    `game_type` in `league_id`/`season`, then deletes any existing
    `player_season_stats` row for this league/season/game_type whose player
    is NOT in that newly-computed set (critic round 19 B19-1) -- all inside
    one `transaction(ex)`. Returns the number of rows upserted (the delete
    count is not returned; callers needing it should query before/after).

    Iterates `final` games one at a time via `stats_repo.
    list_player_game_stats_for_game` (§8.2 adds no bulk "every
    player_game_stats row for a league+season" loader) -- bounded by a
    season's game count (~1,230 for a regular season), acceptable for a
    once-per-rebuild batch job, not the per-possession hot path §11's
    bulk-loader rule exists to protect.

    If `league_id`/`season`/`game_type` has no `final` games at all, this
    returns 0 without touching `player_season_stats` -- since this function
    is the only writer of that table, "zero final games of this type ever"
    implies no row could exist yet, so there is nothing to reconcile.
    """
    async with transaction(ex) as conn:
        season_games = await game_repo.list_games_for_league_season(conn, league_id, season)
        final_game_ids = [
            g.id for g in season_games if g.game_type == game_type and g.status == GameStatus.FINAL
        ]
        if not final_game_ids:
            return 0

        by_player: dict[int, list[PlayerGameStats]] = defaultdict(list)
        for game_id in final_game_ids:
            for row in await stats_repo.list_player_game_stats_for_game(conn, game_id):
                by_player[row.player_id].append(row)

        written = 0
        for player_id, rows in by_player.items():
            box: dict[str, int] = {
                name: sum(getattr(r, name) for r in rows) for name in _BOX_SCORE_FIELD_NAMES
            }
            minutes_total: Decimal = sum((r.minutes_played for r in rows), Decimal("0"))
            await stats_repo.upsert_player_season_stats(
                conn,
                league_id=league_id,
                player_id=player_id,
                season=season,
                game_type=game_type,
                games_played=len(rows),
                minutes_played=minutes_total,
                box=box,
            )
            written += 1

        await stats_repo.delete_stale_player_season_stats(
            conn,
            league_id=league_id,
            season=season,
            game_type=game_type,
            keep_player_ids=list(by_player.keys()),
        )
        return written
