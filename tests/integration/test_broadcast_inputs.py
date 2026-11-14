"""`view_service.get_broadcast_inputs` against real Postgres and a real simmed
game (Stage D commit 10, docs/design/phase-5-stage-d-broadcast.md §4.2, §7).

What this proves is the I/O seam only: the teams are the game row's teams, and
`names` holds a surname for every player the beat log can name -- read from the
database, including a player who left his team after the game. The rendering
of those names is `tests/unit/presentation/`'s business.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

import pytest

from dba2.core.errors import NotFoundError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, player_repo
from dba2.domain.player import RosterStatus
from dba2.services import game_sim_service, schedule_service, view_service
from dba2.sim.events import GameResult
from tools import seed_dev_league


@dataclass
class Sim:
    pool: object
    league_id: int
    game_id: int
    result: GameResult


@pytest.fixture
async def sim():
    pool = await get_pool()
    league_id = await seed_dev_league.seed_dev_league(
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Broadcast Inputs Integration League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=913,
    )
    try:
        await schedule_service.generate_regular_season(pool, league_id, 2026)
        games = await game_repo.list_games_for_league_season(pool, league_id, 2026)
        game_id = games[0].id
        result = await game_sim_service.sim_game(pool, game_id)
        yield Sim(pool=pool, league_id=league_id, game_id=game_id, result=result)
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


def _named_ids(result: GameResult) -> set[int]:
    ids = {line.player_id for line in result.player_box_lines}
    for record in result.beat_log:
        ids.update(record.actor_ids.values())
    return ids


async def test_inputs_carry_the_game_rows_teams_and_a_surname_for_every_named_player(sim):
    inputs = await view_service.get_broadcast_inputs(
        sim.pool, game_id=sim.game_id, result=sim.result
    )
    game = await game_repo.get_game(sim.pool, sim.game_id)
    assert (inputs.home.id, inputs.away.id) == (game.home_team_id, game.away_team_id)
    needed = _named_ids(sim.result)
    assert needed, "the simmed game names players"
    assert set(inputs.names) == needed
    for player_id in needed:
        player = await player_repo.get_player(sim.pool, player_id)
        assert inputs.names[player_id] == player.last_name


async def test_a_player_who_left_the_team_after_the_game_is_still_named(sim):
    leaver = sim.result.player_box_lines[0].player_id
    await player_repo.set_team_and_status(sim.pool, leaver, None, RosterStatus.FREE_AGENT)
    inputs = await view_service.get_broadcast_inputs(
        sim.pool, game_id=sim.game_id, result=sim.result
    )
    player = await player_repo.get_player(sim.pool, leaver)
    assert player.team_id is None
    assert inputs.names[leaver] == player.last_name


async def test_a_player_who_no_longer_exists_is_left_out_not_invented(sim):
    ghost = 987_654_321
    box = sim.result.player_box_lines
    result = replace(sim.result, player_box_lines=(*box, replace(box[0], player_id=ghost)))
    inputs = await view_service.get_broadcast_inputs(sim.pool, game_id=sim.game_id, result=result)
    assert ghost not in inputs.names  # the renderer's "Player <id>" fallback covers it
    assert set(inputs.names) == _named_ids(result) - {ghost}


async def test_a_missing_game_is_not_found(sim):
    with pytest.raises(NotFoundError, match="No game"):
        await view_service.get_broadcast_inputs(sim.pool, game_id=2_000_000_000, result=sim.result)


async def test_a_result_from_another_games_teams_is_refused(sim):
    other = replace(
        sim.result, home_team_id=sim.result.away_team_id, away_team_id=sim.result.home_team_id
    )
    with pytest.raises(ValueError, match="in-memory result is for teams"):
        await view_service.get_broadcast_inputs(sim.pool, game_id=sim.game_id, result=other)
