"""Live I/O proof for `services/stats_service.py`
(phase-1-simulation-engine.md §8.3/§9 Tier 4, §12 step 8).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as every other tests/integration/ module.
"""
from __future__ import annotations

import random
from collections import defaultdict
from decimal import Decimal

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, player_repo, stats_repo
from dba2.domain.game import GameType
from dba2.domain.player import RosterStatus
from dba2.services import game_sim_service, schedule_service, stats_service
from tools import seed_dev_league

_BOX_FIELDS: tuple[str, ...] = (
    "points",
    "fga_rim", "fgm_rim", "fga_mid", "fgm_mid", "fga_three", "fgm_three",
    "fta", "ftm",
    "oreb", "dreb", "ast", "stl", "blk", "tov", "pf",
)


async def _seed_and_sim_n_games(
    pool, *, name: str, rng_seed: int, n_games: int
) -> tuple[int, list]:
    league_id = await seed_dev_league.seed_dev_league(
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name=name,
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=rng_seed,
    )
    await schedule_service.generate_regular_season(pool, league_id, 2026)
    games = await game_repo.list_games_for_league_season(pool, league_id, 2026)
    simmed = []
    for game in games[:n_games]:
        simmed.append(await game_sim_service.sim_game(pool, game.id))
    return league_id, simmed


@pytest.mark.asyncio
async def test_rebuild_player_season_stats_equals_the_sum_of_per_game_rows():
    pool = await get_pool()
    league_id = None
    try:
        league_id, _results = await _seed_and_sim_n_games(
            pool, name="Season Aggregate Rebuild League", rng_seed=601, n_games=5
        )

        written = await stats_service.rebuild_player_season_stats(
            pool, league_id, 2026, GameType.REGULAR
        )
        assert written > 0

        games = await game_repo.list_games_for_league_season(pool, league_id, 2026)
        final_game_ids = [g.id for g in games if g.status.value == "final"]

        expected_by_player: dict[int, dict] = defaultdict(
            lambda: {"games_played": 0, "minutes": Decimal("0"), **{f: 0 for f in _BOX_FIELDS}}
        )
        for game_id in final_game_ids:
            for row in await stats_repo.list_player_game_stats_for_game(pool, game_id):
                agg = expected_by_player[row.player_id]
                agg["games_played"] += 1
                agg["minutes"] += row.minutes_played
                for f in _BOX_FIELDS:
                    agg[f] += getattr(row, f)

        assert written == len(expected_by_player)

        for player_id, expected in expected_by_player.items():
            season_row = await stats_repo.get_player_season_stats(
                pool, league_id, player_id, 2026, GameType.REGULAR
            )
            assert season_row is not None
            assert season_row.games_played == expected["games_played"]
            assert season_row.minutes_played == expected["minutes"]
            for f in _BOX_FIELDS:
                assert getattr(season_row, f) == expected[f], (player_id, f)
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_rebuild_player_season_stats_is_idempotent_and_reflects_a_resim():
    """A second rebuild call with unchanged inputs reproduces the same
    aggregate (wholesale-rewrite, not accumulate -- calling it twice must
    not double the totals), and a rebuild after a resim reflects the new
    per-game rows rather than the old ones."""
    pool = await get_pool()
    league_id = None
    try:
        league_id, results = await _seed_and_sim_n_games(
            pool, name="Season Aggregate Idempotent League", rng_seed=602, n_games=2
        )
        games = await game_repo.list_games_for_league_season(pool, league_id, 2026)
        target_game = games[0]

        await stats_service.rebuild_player_season_stats(pool, league_id, 2026, GameType.REGULAR)
        first_pass = {
            row.player_id: row
            for player_id in {line.player_id for line in results[0].player_box_lines}
            for row in [
                await stats_repo.get_player_season_stats(
                    pool, league_id, player_id, 2026, GameType.REGULAR
                )
            ]
        }

        written_again = await stats_service.rebuild_player_season_stats(
            pool, league_id, 2026, GameType.REGULAR
        )
        for player_id, row in first_pass.items():
            second_row = await stats_repo.get_player_season_stats(
                pool, league_id, player_id, 2026, GameType.REGULAR
            )
            assert second_row.points == row.points
            assert second_row.games_played == row.games_played

        # Resim target_game, then rebuild again -- the aggregate must move
        # to match the new per-game rows, not keep stacking on the old ones.
        resim_result = await game_sim_service.sim_game(pool, target_game.id, resim=True)
        await stats_service.rebuild_player_season_stats(pool, league_id, 2026, GameType.REGULAR)

        final_game_ids = [g.id for g in games if g.status.value == "final"]
        expected_points: dict[int, int] = defaultdict(int)
        expected_games: dict[int, int] = defaultdict(int)
        for game_id in final_game_ids:
            for row in await stats_repo.list_player_game_stats_for_game(pool, game_id):
                expected_points[row.player_id] += row.points
                expected_games[row.player_id] += 1

        for line in resim_result.player_box_lines:
            season_row = await stats_repo.get_player_season_stats(
                pool, league_id, line.player_id, 2026, GameType.REGULAR
            )
            assert season_row is not None
            assert season_row.points == expected_points[line.player_id]
            assert season_row.games_played == expected_games[line.player_id]

        assert written_again > 0
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_rebuild_clears_a_stale_season_row_when_a_player_drops_out_of_a_resim():
    """Critic round 19 B19-1's exact reproduction: `game_sim_service.
    build_context` re-reads the *live* roster at re-sim time, so marking a
    game participant `injured` between the original sim and a resim drops
    them out of the re-simmed game's aggregate entirely. The old
    upsert-only rebuild left that player's `player_season_stats` row from
    the first rebuild permanently stale (a GP/points/minutes line with zero
    backing `player_game_stats` rows). The fixed `rebuild_player_season_
    stats` must delete that row, not just skip writing a new one."""
    pool = await get_pool()
    league_id = None
    try:
        league_id, results = await _seed_and_sim_n_games(
            pool, name="Stale Season Row On Injury League", rng_seed=603, n_games=1
        )
        game = (await game_repo.list_games_for_league_season(pool, league_id, 2026))[0]

        await stats_service.rebuild_player_season_stats(pool, league_id, 2026, GameType.REGULAR)

        victim_line = results[0].player_box_lines[0]
        victim_id = victim_line.player_id
        victim = await player_repo.get_player(pool, victim_id)

        # Sanity: the victim really has a season row before the injury.
        before = await stats_repo.get_player_season_stats(
            pool, league_id, victim_id, 2026, GameType.REGULAR
        )
        assert before is not None
        assert before.games_played == 1

        # The exact status build_context's own docstring says it excludes.
        await player_repo.set_team_and_status(
            pool, victim_id, victim.team_id, RosterStatus.INJURED
        )

        await game_sim_service.sim_game(pool, game.id, resim=True)

        # The victim no longer appears in the resimmed game's stat rows.
        remaining_rows = await stats_repo.list_player_game_stats_for_game(pool, game.id)
        assert not any(r.player_id == victim_id for r in remaining_rows), (
            "victim should have zero player_game_stats rows after being "
            "excluded from the resim's roster"
        )

        await stats_service.rebuild_player_season_stats(pool, league_id, 2026, GameType.REGULAR)

        after = await stats_repo.get_player_season_stats(
            pool, league_id, victim_id, 2026, GameType.REGULAR
        )
        assert after is None, (
            "rebuild_player_season_stats left a stale player_season_stats row "
            "for a player with zero backing player_game_stats rows (B19-1)"
        )

        # A still-qualifying player's season row is untouched by the delete.
        survivor_id = next(
            line.player_id
            for line in results[0].player_box_lines
            if line.player_id != victim_id
        )
        survivor_row = await stats_repo.get_player_season_stats(
            pool, league_id, survivor_id, 2026, GameType.REGULAR
        )
        assert survivor_row is not None
        assert survivor_row.games_played == 1
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()
