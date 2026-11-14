"""Live I/O proof for `services/schedule_service.py`
(phase-1-simulation-engine.md §8.3/§12 step 8, Decision P1-27).

Uses `league_service.create_league` (30 real NBA teams with real
conference/division data from `seeds/teams.json`) rather than
`tools.seed_dev_league` -- schedule generation reads only `teams`, no
players/ratings, so the heavier full-roster seed would be pure overhead
here.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as every other tests/integration/ module.
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict

import pytest

from dba2.core.errors import ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, team_repo
from dba2.domain.game import GameType
from dba2.services import league_service, schedule_service


async def _make_league(pool) -> int:
    league = await league_service.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Schedule Service Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=99,
    )
    return league.id


@pytest.mark.asyncio
async def test_generate_regular_season_produces_the_correct_multiset_and_balanced_home_away():
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _make_league(pool)

        created = await schedule_service.generate_regular_season(pool, league_id, 2026)
        assert created == 1230

        games = await game_repo.list_games_for_league_season(pool, league_id, 2026)
        assert len(games) == 1230
        assert all(g.game_type is GameType.REGULAR for g in games)
        # game_index is 0-based and contiguous, and rng_seed is a legal
        # 63-bit unsigned value (Decision P1-21 -- games.rng_seed is a
        # signed BIGINT, so a 64-bit unsigned draw would overflow it on
        # roughly half of all games).
        assert sorted(g.game_index for g in games) == list(range(1230))
        assert all(0 <= g.rng_seed < 2**63 for g in games)
        assert len({g.rng_seed for g in games}) == 1230, "every game must get an independent seed"

        teams = await team_repo.list_teams_for_league(pool, league_id)
        team_by_id = {t.id: t for t in teams}

        games_per_team: Counter[int] = Counter()
        home_per_team: Counter[int] = Counter()
        opponent_counts: dict[int, Counter[int]] = defaultdict(Counter)
        for g in games:
            games_per_team[g.home_team_id] += 1
            games_per_team[g.away_team_id] += 1
            home_per_team[g.home_team_id] += 1
            opponent_counts[g.home_team_id][g.away_team_id] += 1
            opponent_counts[g.away_team_id][g.home_team_id] += 1

        assert set(games_per_team.values()) == {82}, "every team must play exactly 82 games"

        for team_id, counts in opponent_counts.items():
            team = team_by_id[team_id]
            n_division = n_conf_four = n_conf_three = n_inter = 0
            for opp_id, n in counts.items():
                opp = team_by_id[opp_id]
                if opp.conference != team.conference:
                    assert n == 2
                    n_inter += 1
                elif opp.division == team.division:
                    assert n == 4
                    n_division += 1
                elif n == 4:
                    n_conf_four += 1
                else:
                    assert n == 3
                    n_conf_three += 1
            assert n_division == 4, team.code
            assert n_conf_four == 6, team.code
            assert n_conf_three == 4, team.code
            assert n_inter == 15, team.code

        # Home/away balance (module docstring's greedy "assign to the
        # underdog" rule) -- not required to be exactly 41/41 (the odd
        # 3-game conference matchups can't split evenly), but every team
        # should land in a tight band around it.
        for team_id in games_per_team:
            home_games = home_per_team[team_id]
            assert 35 <= home_games <= 47, (team_by_id[team_id].code, home_games)

        # Date fairness (docs/design/schedule-date-fairness.md, SDF-1/SDF-3):
        # every team stays within a handful of games of every other team at
        # any point in the season. Measured on the real 30-team set: worst
        # spread 2, so 3 is not aspirational headroom -- a regression to
        # spread 3 would still pass this assertion.
        by_date: dict = defaultdict(list)
        for g in games:
            by_date[g.game_date].append((g.home_team_id, g.away_team_id))
        played: Counter[int] = Counter()
        worst_spread = 0
        for game_date in sorted(by_date):
            for home_id, away_id in by_date[game_date]:
                played[home_id] += 1
                played[away_id] += 1
            counts = [played[t.id] for t in teams]
            worst_spread = max(worst_spread, max(counts) - min(counts))
        assert worst_spread <= 3, f"max-min games-played spread reached {worst_spread}"
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


@pytest.mark.asyncio
async def test_generate_regular_season_refuses_to_regenerate_an_existing_schedule():
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _make_league(pool)
        await schedule_service.generate_regular_season(pool, league_id, 2026)

        with pytest.raises(ValidationError, match="already has"):
            await schedule_service.generate_regular_season(pool, league_id, 2026)

        # Still exactly one season's worth of games -- the rejected second
        # call must not have written anything on its way to raising.
        games = await game_repo.list_games_for_league_season(pool, league_id, 2026)
        assert len(games) == 1230
    finally:
        if league_id is not None:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()
