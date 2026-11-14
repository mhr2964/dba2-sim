"""RMLP Decision 1 evidence (`docs/design/_rmlp_addendum_a.md` amending the
inline RMLP revision-2 design, §3.1a) -- build step 5's required proof that
`FOR NO KEY UPDATE` at tiers 1/2 (`team_repo.lock_team_row`, `player_repo.
lock_player_row`) kills the F8 deadlock class against `game_sim_service`,
landed BEFORE the rest of the protocol builds on top of it.

F8: `game_sim_service._persist_game_result` inserts `player_game_stats` rows
(each taking an implicit `FOR KEY SHARE` on the referenced `players` row via
its `player_id` FK) BEFORE `team_game_stats` rows (`FOR KEY SHARE` on
`teams` via `team_id`) -- players-then-teams, the exact reverse of RMLP's
teams-then-players canonical order. Under the old `FOR UPDATE` mode, that
reversal is a live deadlock class: a protocol participant holding `FOR
UPDATE` on a team while requesting `FOR UPDATE` on a player can cycle
against a sim transaction that holds `KEY SHARE` on that player while
requesting `KEY SHARE` on that team. `FOR NO KEY UPDATE` does not conflict
with `FOR KEY SHARE` at all, so neither wait edge of that cycle can form.

`test_sim_vs_mutation_non_interference_produces_zero_deadlocks` is §7.2 test
12 (the positive case, real `lock_team_row`/`lock_player_row` calls, `FOR NO
KEY UPDATE`). `test_sim_vs_mutation_twin_with_for_update_reproduces_deadlocks`
is its required mutation twin (§7.1: "a concurrency test without its twin is
not evidence") -- it does NOT call the renamed repo functions; it issues the
identical lock statements by hand with `FOR UPDATE` instead, faithfully
reproducing the pre-Decision-1 mode, and asserts the deadlock class
reappears. Per the design's own instruction (§7.2 test 12), the "sim" side
here is a faithful stand-in issuing the exact same `player_game_stats`-then-
`team_game_stats` insert order `_persist_game_result` does, not the full game
engine.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import asyncio
import random
from datetime import UTC, date, datetime
from decimal import Decimal

import asyncpg
import pytest

from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import (
    contract_repo,
    game_repo,
    league_repo,
    player_repo,
    stats_repo,
    team_repo,
)
from dba2.data.repositories.stats_repo import _BOX_SCORE_COLUMN_NAMES
from dba2.domain.contract import ContractType, SigningMethod
from dba2.domain.game import GameType
from dba2.domain.player import Position, RosterStatus

_TRIALS = 50
# Comfortably shorter than a real deadlock_timeout (Postgres default 1s) so
# a run that genuinely deadlocks doesn't make this test slow -- a deadlock
# is detected and one side aborted quickly regardless of this value; this
# only bounds how long a run that's merely BLOCKED (not deadlocked) waits
# before the test itself gives up and reports a hang rather than a false
# "zero deadlocks" pass.
_LOCK_TIMEOUT_MS = 2000


def _flat_box(value: int) -> dict[str, int]:
    return {name: value for name in _BOX_SCORE_COLUMN_NAMES}


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="RMLP Lock Mode Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    return league.id


async def _make_team(pool, league_id: int, code: str) -> int:
    team = await team_repo.create_team(
        pool,
        league_id=league_id,
        code=code,
        name=f"{code} Team",
        city=f"{code} City",
        conference="East",
        division="Atlantic",
        home_market_size=3,
    )
    return team.id


async def _make_rostered_player(pool, league_id: int, team_id: int) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Lock",
        last_name="Mode",
        primary_position=Position.SF,
        height_in=79,
        weight_lb=215,
        wingspan_in=82,
        birth_date=date(1998, 3, 3),
        roster_status=RosterStatus.ACTIVE,
        overall_cached=75,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    contract = await contract_repo.create_contract(
        pool,
        league_id=league_id,
        player_id=player.id,
        team_id=team_id,
        signed_in_season=2026,
        start_season=2026,
        end_season=2027,
        total_years=2,
        contract_type=ContractType.STANDARD,
        signing_method=SigningMethod.CAP_SPACE,
    )
    await contract_repo.create_contract_year(
        pool, contract_id=contract.id, season=2026, salary=1_000_000, guaranteed_amount=1_000_000
    )
    return player.id


async def _sim_stand_in(pool, *, game_id: int, league_id: int, team_id: int, player_id: int):
    """The exact `_persist_game_result` insert order: player_game_stats
    (implicit `FOR KEY SHARE` on `players` via `player_id`), THEN, after a
    delay that guarantees overlap with the mutation side, team_game_stats
    (implicit `FOR KEY SHARE` on `teams` via `team_id`) -- both inside one
    transaction, matching `game_sim_service.py:276-303`."""
    async with transaction(pool) as conn:
        await conn.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT_MS}ms'")
        await stats_repo.create_player_game_stats_many(
            conn,
            [
                {
                    "game_id": game_id,
                    "league_id": league_id,
                    "player_id": player_id,
                    "team_id": team_id,
                    "minutes_played": Decimal("30.0"),
                    "plus_minus": 4,
                    "box": _flat_box(1),
                }
            ],
        )
        await asyncio.sleep(0.05)
        await stats_repo.create_team_game_stats_many(
            conn,
            [
                {
                    "game_id": game_id,
                    "league_id": league_id,
                    "team_id": team_id,
                    "possessions": 95,
                    "box": _flat_box(1),
                }
            ],
        )


async def _rmlp_mutation_real(pool, *, team_id: int, player_id: int):
    """Team-then-player, the real RMLP tier-1/tier-2 order, using the real
    (post-rename, `FOR NO KEY UPDATE`) repo functions -- exactly the order
    `gm_command_service.sign_free_agent` -> `roster_service.sign` already
    takes in production."""
    async with transaction(pool) as conn:
        await conn.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT_MS}ms'")
        await team_repo.lock_team_row(conn, team_id)
        await asyncio.sleep(0.05)
        await player_repo.lock_player_row(conn, player_id)


async def _rmlp_mutation_for_update_twin(pool, *, team_id: int, player_id: int):
    """The mutation twin: identical team-then-player order, but hand-issues
    `FOR UPDATE` instead of calling the renamed (`FOR NO KEY UPDATE`) repo
    functions -- faithfully reproducing the pre-Decision-1 mode without
    needing a second code path in production."""
    async with transaction(pool) as conn:
        await conn.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT_MS}ms'")
        await conn.fetchrow("SELECT id FROM teams WHERE id = $1 FOR UPDATE", team_id)
        await asyncio.sleep(0.05)
        await conn.fetchrow("SELECT id FROM players WHERE id = $1 FOR UPDATE", player_id)


async def _run_one_trial(pool, league_id, team_id, player_id, mutation_coro, other_team_id):
    game = await game_repo.create_game(
        pool,
        league_id=league_id,
        season=2026,
        game_index=random.randint(1, 1_000_000_000),
        game_date=date(2026, 11, 10),
        home_team_id=team_id,
        away_team_id=other_team_id,
        game_type=GameType.REGULAR,
        rng_seed=random.randint(1, 1_000_000),
    )
    results = await asyncio.gather(
        _sim_stand_in(pool, game_id=game.id, league_id=league_id, team_id=team_id,
                       player_id=player_id),
        mutation_coro(pool, team_id=team_id, player_id=player_id),
        return_exceptions=True,
    )
    return results


@pytest.mark.asyncio
async def test_sim_vs_mutation_non_interference_produces_zero_deadlocks():
    """RMLP §7.2 test 12 -- the positive case. `_TRIALS` rounds of a sim
    stand-in (players-then-teams) racing a real RMLP team-then-player lock
    acquisition on the SAME team/player, using the real, renamed, `FOR NO
    KEY UPDATE` repo functions. Assert zero `DeadlockDetectedError` (40P01)
    across every trial -- F8's cycle cannot form because `FOR NO KEY
    UPDATE` does not conflict with `FOR KEY SHARE`."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "SVM")
            other_team_id = await _make_team(pool, league_id, "SVO")
            player_id = await _make_rostered_player(pool, league_id, team_id)

            deadlocks = 0
            other_errors = []
            for _ in range(_TRIALS):
                results = await _run_one_trial(
                    pool, league_id, team_id, player_id, _rmlp_mutation_real, other_team_id
                )
                for r in results:
                    if isinstance(r, asyncpg.exceptions.DeadlockDetectedError):
                        deadlocks += 1
                    elif isinstance(r, Exception):
                        other_errors.append(r)

            assert deadlocks == 0, (
                f"expected zero 40P01 deadlocks across {_TRIALS} trials with FOR NO KEY "
                f"UPDATE, got {deadlocks} -- Decision 1's deadlock-class claim is false"
            )
            assert not other_errors, f"unexpected non-deadlock errors: {other_errors!r}"
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_sim_vs_mutation_twin_with_for_update_reproduces_deadlocks():
    """The required mutation twin (§7.1) -- identical scenario, but the
    mutation side hand-issues `FOR UPDATE` (the pre-Decision-1 mode)
    instead of calling the renamed repo functions. Recorded numbers (this
    run, printed below): with `FOR NO KEY UPDATE` (the test above),
    0/{_TRIALS} trials deadlock. With `FOR UPDATE` (this twin), at least
    one trial across {_TRIALS} reproduces a 40P01 -- confirming the mode
    change is load-bearing and not merely cosmetic."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "TWM")
            other_team_id = await _make_team(pool, league_id, "TWO")
            player_id = await _make_rostered_player(pool, league_id, team_id)

            deadlocks = 0
            other_errors = []
            for _ in range(_TRIALS):
                results = await _run_one_trial(
                    pool,
                    league_id,
                    team_id,
                    player_id,
                    _rmlp_mutation_for_update_twin,
                    other_team_id,
                )
                for r in results:
                    if isinstance(r, asyncpg.exceptions.DeadlockDetectedError):
                        deadlocks += 1
                    elif isinstance(r, Exception):
                        other_errors.append(r)

            print(f"\n[RMLP twin] FOR UPDATE mode: {deadlocks}/{_TRIALS} trials deadlocked")
            assert deadlocks > 0, (
                f"expected the F8 deadlock class to reappear under FOR UPDATE across "
                f"{_TRIALS} trials, got 0 -- either the twin doesn't reproduce the "
                "pre-Decision-1 scenario faithfully, or F8's argument doesn't hold; "
                "investigate rather than delete this test"
            )
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
