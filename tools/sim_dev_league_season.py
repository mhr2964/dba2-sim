"""CLI: simulate one full regular season of real games for a
`tools/seed_dev_league.py`-created dev league, so `player_season_stats.
minutes_played` becomes real rather than empty -- the last prerequisite
RPD-6's minutes-factor signal needs (docs/design/real-player-data-
integration.md, item 11).

**No new engine, no new schedule generator -- this composes three already-
built, already-tested production services** that nothing had previously
wired into one convenience path for a dev league (each is independently
real and used elsewhere: `schedule_service.generate_regular_season` is
Phase 1's own 1,230-game schedule builder, `game_sim_service.
sim_scheduled_games` is the real possession-sim engine, `stats_service.
rebuild_player_season_stats` is the real `player_season_stats` aggregate
builder). Run in this fixed order because each depends on the previous
step's writes:

    python -m tools.sim_dev_league_season --league-id 89

`--season` defaults to the league's own `current_season` (read fresh, not
cached) -- the season that's about to be progressed by `tools/
advance_dev_league_season.py`, so the natural sequence for a real dev
league is: sim this season's games -> advance to next season (which reads
this season's now-real minutes as RPD-6's minutes-factor input) -> repeat.

Raises whatever `generate_regular_season` raises (`ValidationError`) if
`league_id`/`season` already has a schedule -- this tool does not own a
re-generation workflow, matching that function's own stated scope.
"""
from __future__ import annotations

import argparse
import asyncio

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo
from dba2.domain.game import GameType
from dba2.services import game_sim_service, schedule_service, stats_service, strategy_service


async def sim_dev_league_season(
    *, league_id: int, season: int | None = None
) -> tuple[int, int, int]:
    """Returns `(season, games_simmed, failed_game_count)`."""
    pool = await get_pool()

    if season is None:
        league = await league_repo.get_league(pool, league_id)
        if league is None:
            raise ValueError(f"no league with id {league_id}")
        season = league.current_season

    # `seed_dev_league.py` only calls this for its own creation season --
    # a league advanced past that by `advance_dev_league_season.py` has no
    # team_strategy row for the new season yet, and `sim_scheduled_games`
    # needs one per team to build a game's strategy snapshot. Idempotent
    # (0 rows written on a season that already has them), so safe to call
    # unconditionally rather than only on a first-ever sim of a league.
    await strategy_service.ensure_defaults_for_season(pool, league_id, season)
    await schedule_service.generate_regular_season(pool, league_id, season)
    batch_result = await game_sim_service.sim_scheduled_games(pool, league_id, season)
    await stats_service.rebuild_player_season_stats(pool, league_id, season, GameType.REGULAR)

    return season, batch_result.games_simmed, len(batch_result.failed_game_ids)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Simulate one full regular season of real games for a dev league, so "
            "player_season_stats.minutes_played becomes real (RPD-6's minutes-factor input)."
        )
    )
    parser.add_argument("--league-id", type=int, required=True)
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="Defaults to the league's own current_season.",
    )
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    try:
        season, games_simmed, failed = await sim_dev_league_season(
            league_id=args.league_id, season=args.season
        )
        print(
            f"League {args.league_id} season {season}: {games_simmed} games simmed, "
            f"{failed} failed."
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
