"""Live I/O proof for `dba2/services/league_claim.py` -- the league-level
guard moved out of `offer_service` so `/draft` can share it (Decision
P5-98.g step 2, docs/design/phase-5-stage-d-prime-draft.md §4 commit 2).

`offer_service`'s own claim tests (`test_offer_service.py`, §7.4 tests
4a-4d) still prove the guard through that surface, unedited; they reach it
through four commands, a gate, an offer row and a permission check. These
drive the shared function itself, which is what a second caller will bind
to -- a moved rule that is only ever observed through its first caller is
a rule the second caller can silently diverge from.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""

from __future__ import annotations

import random

import pytest

from dba2.core.errors import ConcurrencyError
from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import league_repo
from dba2.domain.phase import Phase
from dba2.services import league_claim, roster_lock
from dba2.services.roster_lock import LockMode

_SEASON = 2026


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="League Claim Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=1,
        rng_seed=1,
    )
    return league.id


async def test_check_claim_returns_the_locked_state_when_nothing_holds_the_league():
    """The ordinary path: the row is locked `FOR SHARE`, no claim is held,
    the gate's `(phase, season)` still agrees, and the caller gets the
    locked state back -- `commissioner_user_id` included, which is why a
    permission check downstream may read it from here rather than taking a
    second, unlocked look."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            async with transaction(pool) as conn:
                gate = await roster_lock.open_league_gate(
                    conn, league_id=league_id, mode=LockMode.SHARED
                )
                state = await league_claim.check_claim(conn, gate=gate, league_id=league_id)
            assert state.advance_claimed_at is None
            assert (state.current_phase, state.current_season) == (gate.phase, gate.season)
            assert state.commissioner_user_id == 1
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_check_claim_refuses_while_stage_es_advance_claim_is_held():
    """A committed `advance_claimed_at` is a press in flight, and the
    refusal carries the shared wording -- the message a person reads must
    not depend on which surface refused them."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            await pool.execute(
                "UPDATE leagues SET advance_claimed_at = NOW(), advance_claimed_by = 99 "
                "WHERE id = $1",
                league_id,
            )
            with pytest.raises(ConcurrencyError) as excinfo:
                async with transaction(pool) as conn:
                    gate = await roster_lock.open_league_gate(
                        conn, league_id=league_id, mode=LockMode.SHARED
                    )
                    await league_claim.check_claim(conn, gate=gate, league_id=league_id)
            assert str(excinfo.value) == league_claim.CLAIM_HELD_MESSAGE
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


async def test_check_claim_refuses_a_gate_whose_phase_no_longer_matches_the_locked_row():
    """The second half of the guard, and the half `offer_service`'s claim
    tests never reach: the league advanced between the gate read and this
    lock. Driven by handing the function a `LeagueGate` that disagrees with
    the row, which is exactly the state a stale gate would carry -- and
    `require_phase_agreement=False` (what `veto` passes) must tolerate the
    same disagreement, so the two arms are asserted together."""
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            async with transaction(pool) as conn:
                gate = await roster_lock.open_league_gate(
                    conn, league_id=league_id, mode=LockMode.SHARED
                )
                stale = roster_lock.LeagueGate(
                    league_id=league_id,
                    phase=Phase.TRADE_DEADLINE
                    if gate.phase is not Phase.TRADE_DEADLINE
                    else Phase.DRAFT,
                    season=gate.season,
                    mode=gate.mode,
                )
                with pytest.raises(ConcurrencyError) as excinfo:
                    await league_claim.check_claim(conn, gate=stale, league_id=league_id)
                assert str(excinfo.value) == league_claim.LEAGUE_ADVANCED_MESSAGE
                tolerated = await league_claim.check_claim(
                    conn, gate=stale, league_id=league_id, require_phase_agreement=False
                )
                assert tolerated.current_phase == gate.phase
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
