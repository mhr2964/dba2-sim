"""RMLP §7.4 check 2b -- the design's own required companion to check 2,
never built until this fix round (2026-09-18). See `docs/design/
_rmlp_addendum_a.md`'s own per-table obligation list ("`RMLP_MUTATORS` ->
checks 1, 2, 2b, 8") and `docs/design/phase-5-discord-presentation.md`
§44.6's disclosure list, which named tests 5/7/8/10 as skipped but never
named this gap -- corrected there in the same commit as this file.

**Why check 2 alone is not enough (the design's own reasoning).** Check 2
(`tests/unit/test_roster_mutation_lock_discipline.py::
test_all_rmlp_mutators_have_a_non_trivial_correctly_shaped_lock_set`) is a
STATIC check: it verifies the `acquire(...)` call's `teams=`/`players=`
source text MENTIONS the right identifier, via `ast.unparse` substring
containment. That is deliberately brittle against refactors (its own
docstring says so), but it cannot evaluate a VALUE -- `teams=[other_
object.team_id]` would still satisfy check 2 for a mutator that should be
locking a different team than `other_object` names, so long as the
identifier `team_id` appears somewhere in the expression's source text.
Check 2b is the only thing that catches a genuinely wrong-entity lock set
at runtime: it wraps `roster_lock.acquire` with a wrapper that records
the REAL ids it locked, then wraps each `WRITE_SHAPED` repo function with
an assertion, run at call time, that the entity it is about to write was
actually present in that recorded set.

**Scope, matching the design's own instruction.** "Run each of the 5 real
mutators once under this fixture on a real happy-path scenario" -- not
full branch coverage (that is every OTHER RMLP test's job). Instruments
every `WRITE_SHAPED` repo function reachable from a real mutator's happy
path (`create_contract`, `terminate_contract`, `set_contract_team`,
`create_dead_cap_charge`, `player_repo.set_team_and_status`);
`contract_repo.set_option_decision`/`create_cap_hold` are `RMLP_FUTURE_
ADOPTERS` with zero real callers today (`test_roster_mutation_lock_
discipline.py`'s own check 5), so wrapping them here would never fire --
registered anyway (`_instrument_write_shaped`), for whichever future
caller adopts them first. `draft_repo.set_current_team` (traded picks) is
not exercised -- the `apply_trade` happy path below trades players only,
matching the "one happy path, not every branch" scope; B3's own new
`apply_trade` tests in `test_rmlp_concurrency.py` cover the revalidation
rules this file does not need to duplicate.

**The recorder is test-local, never module-level ambient state** (built
fresh inside each test, torn down with it) -- this project's own
established caution about ambient per-connection state causing bugs
(`data/db.py`'s 60-line `_active_conn` docstring; `roster_lock.py`'s own
`_gate_is_held` being scoped to `pg_backend_pid()` for the identical
reason).

**This is test-only scaffolding, never a change to production code.**
Every wrapper below is installed via `monkeypatch.setattr` on the
`contract_repo`/`player_repo`/`roster_lock` MODULE objects -- every real
call site in `dba2/` reaches these functions via a dotted attribute
lookup on that module (`roster_lock.acquire(...)`, `contract_repo.
create_contract(...)`), resolved fresh at call time, so patching the
module's own attribute is what makes the wrapper visible to the real
mutator code without editing a single line of it.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import contract_repo, league_repo, player_repo, team_repo
from dba2.domain.contract import ContractType, SigningMethod
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.team import GmPhilosophy
from dba2.managers.explain import DecisionRecord
from dba2.managers.tuning import CompetitiveWindow
from dba2.services import gm_service, roster_lock, roster_service
from tests.fixtures.rmlp import rmlp_gate

_MANAGER = 42


async def _make_league(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Lock Coverage Test League",
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


async def _make_player(pool, league_id: int, team_id: int | None = None) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="LC",
        last_name="Test",
        primary_position=Position.SF,
        height_in=80,
        weight_lb=220,
        wingspan_in=83,
        birth_date=date(1997, 3, 3),
        roster_status=RosterStatus.ACTIVE if team_id else RosterStatus.FREE_AGENT,
        overall_cached=75,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    return player.id


async def _make_active_contract(
    pool, league_id, team_id, player_id, *, salary=1_000_000, guaranteed=None
) -> int:
    contract = await contract_repo.create_contract(
        pool,
        league_id=league_id,
        player_id=player_id,
        team_id=team_id,
        signed_in_season=2026,
        start_season=2026,
        end_season=2026,
        total_years=1,
        contract_type=ContractType.STANDARD,
        signing_method=SigningMethod.CAP_SPACE,
    )
    await contract_repo.create_contract_year(
        pool,
        contract_id=contract.id,
        season=2026,
        salary=salary,
        guaranteed_amount=salary if guaranteed is None else guaranteed,
    )
    return contract.id


def _dummy_record(team_id: int) -> DecisionRecord:
    return DecisionRecord(
        team_id=team_id,
        philosophy=GmPhilosophy.EGALITARIAN,
        window=CompetitiveWindow.RETOOL,
        subject=f"team:{team_id}",
        verdict="test",
        score=0.0,
        threshold=0.0,
        terms=(),
    )


# ---------------------------------------------------------------------------
# The recorder + wrappers
# ---------------------------------------------------------------------------


@dataclass
class _LockRecorder:
    """Test-local -- see this file's module docstring's "the recorder is
    test-local" paragraph. Populated by `_wrap_acquire`'s recording
    wrapper; read by every `_assert_entity_was_locked` call the write-side
    wrappers below make."""

    team_ids: set[int] = field(default_factory=set)
    player_ids: set[int] = field(default_factory=set)


def _assert_entity_was_locked(
    recorder: _LockRecorder,
    *,
    site: str,
    team_id: int | None = None,
    player_id: int | None = None,
) -> None:
    """Check 2b's own assertion. Run immediately before a `WRITE_SHAPED`
    repo function's real body executes, checking the entity it is about to
    touch was actually present in the recorded lock set for the currently-
    running mutator -- the runtime check this file exists to add, catching
    a wrong-entity `acquire()` call that check 2's static substring check
    cannot (see module docstring)."""
    if team_id is not None and team_id not in recorder.team_ids:
        raise AssertionError(
            f"RMLP check 2b: {site} is about to write team {team_id}, which was "
            f"never in the recorded lock set (teams={sorted(recorder.team_ids)}) -- "
            "the acquire() call for this mutator locked the wrong entity."
        )
    if player_id is not None and player_id not in recorder.player_ids:
        raise AssertionError(
            f"RMLP check 2b: {site} is about to write player {player_id}, which was "
            f"never in the recorded lock set (players={sorted(recorder.player_ids)}) "
            "-- the acquire() call for this mutator locked the wrong entity."
        )


def _wrap_acquire(recorder: _LockRecorder, real_acquire):
    async def _acquire(conn, *, league_id, teams, players):
        result = await real_acquire(conn, league_id=league_id, teams=teams, players=players)
        recorder.team_ids.update(teams)
        recorder.player_ids.update(players)
        return result

    return _acquire


def _wrap_create_contract(recorder: _LockRecorder, real_fn):
    async def _create_contract(conn, **kwargs):
        _assert_entity_was_locked(
            recorder,
            site="contract_repo.create_contract",
            team_id=kwargs.get("team_id"),
            player_id=kwargs.get("player_id"),
        )
        return await real_fn(conn, **kwargs)

    return _create_contract


def _wrap_set_team_and_status(recorder: _LockRecorder, real_fn):
    async def _set_team_and_status(conn, player_id, team_id, roster_status):
        _assert_entity_was_locked(
            recorder,
            site="player_repo.set_team_and_status",
            team_id=team_id,
            player_id=player_id,
        )
        return await real_fn(conn, player_id, team_id, roster_status)

    return _set_team_and_status


def _wrap_create_dead_cap_charge(recorder: _LockRecorder, real_fn):
    async def _create_dead_cap_charge(conn, **kwargs):
        _assert_entity_was_locked(
            recorder,
            site="contract_repo.create_dead_cap_charge",
            team_id=kwargs.get("team_id"),
            player_id=kwargs.get("player_id"),
        )
        return await real_fn(conn, **kwargs)

    return _create_dead_cap_charge


def _wrap_terminate_contract(recorder: _LockRecorder, real_fn, real_get_contract):
    async def _terminate_contract(conn, contract_id, *, status, terminated_in_season):
        # `terminate_contract` takes only `contract_id` -- the entity it is
        # about to write is resolved via a plain read of the CURRENT row
        # (never itself WRITE_SHAPED, so no re-entrant wrapping risk);
        # `terminate_contract` only ever changes status/termination
        # columns, never team_id/player_id, so this pre-write read
        # accurately names the entity the write is about to touch.
        contract = await real_get_contract(conn, contract_id)
        if contract is not None:
            _assert_entity_was_locked(
                recorder,
                site="contract_repo.terminate_contract",
                team_id=contract.team_id,
                player_id=contract.player_id,
            )
        return await real_fn(
            conn, contract_id, status=status, terminated_in_season=terminated_in_season
        )

    return _terminate_contract


def _wrap_set_contract_team(recorder: _LockRecorder, real_fn, real_get_contract):
    async def _set_contract_team(conn, contract_id, team_id):
        contract = await real_get_contract(conn, contract_id)
        _assert_entity_was_locked(
            recorder,
            site="contract_repo.set_contract_team",
            team_id=team_id,
            player_id=contract.player_id if contract is not None else None,
        )
        return await real_fn(conn, contract_id, team_id)

    return _set_contract_team


def _wrap_create_cap_hold(recorder: _LockRecorder, real_fn):
    async def _create_cap_hold(conn, **kwargs):
        _assert_entity_was_locked(
            recorder,
            site="contract_repo.create_cap_hold",
            team_id=kwargs.get("team_id"),
            player_id=kwargs.get("player_id"),
        )
        return await real_fn(conn, **kwargs)

    return _create_cap_hold


def _wrap_set_option_decision(recorder: _LockRecorder, real_fn, real_get_contract):
    async def _set_option_decision(conn, contract_id, season, option_decision):
        contract = await real_get_contract(conn, contract_id)
        if contract is not None:
            _assert_entity_was_locked(
                recorder,
                site="contract_repo.set_option_decision",
                team_id=contract.team_id,
                player_id=contract.player_id,
            )
        return await real_fn(conn, contract_id, season, option_decision)

    return _set_option_decision


def _instrument_write_shaped(monkeypatch) -> _LockRecorder:
    """Installs the recording `acquire` wrapper plus an assert-then-
    delegate wrapper for every `WRITE_SHAPED` member this file can
    reasonably instrument (see module docstring for the two named
    exceptions and why). Returns a fresh, test-local `_LockRecorder`."""
    recorder = _LockRecorder()
    monkeypatch.setattr(roster_lock, "acquire", _wrap_acquire(recorder, roster_lock.acquire))
    monkeypatch.setattr(
        contract_repo,
        "create_contract",
        _wrap_create_contract(recorder, contract_repo.create_contract),
    )
    monkeypatch.setattr(
        contract_repo,
        "terminate_contract",
        _wrap_terminate_contract(
            recorder, contract_repo.terminate_contract, contract_repo.get_contract
        ),
    )
    monkeypatch.setattr(
        contract_repo,
        "set_contract_team",
        _wrap_set_contract_team(
            recorder, contract_repo.set_contract_team, contract_repo.get_contract
        ),
    )
    monkeypatch.setattr(
        contract_repo,
        "create_dead_cap_charge",
        _wrap_create_dead_cap_charge(recorder, contract_repo.create_dead_cap_charge),
    )
    monkeypatch.setattr(
        contract_repo,
        "create_cap_hold",
        _wrap_create_cap_hold(recorder, contract_repo.create_cap_hold),
    )
    monkeypatch.setattr(
        contract_repo,
        "set_option_decision",
        _wrap_set_option_decision(
            recorder, contract_repo.set_option_decision, contract_repo.get_contract
        ),
    )
    monkeypatch.setattr(
        player_repo,
        "set_team_and_status",
        _wrap_set_team_and_status(recorder, player_repo.set_team_and_status),
    )
    return recorder


# ---------------------------------------------------------------------------
# The 5 real mutators, each run once on a real happy-path scenario.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_2b_sign_writes_only_the_locked_team_and_player(monkeypatch):
    recorder = _instrument_write_shaped(monkeypatch)
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "C2BA")
            player_id = await _make_player(pool, league_id)
            async with rmlp_gate(pool, league_id) as conn:
                await roster_service.sign(
                    conn,
                    league_id=league_id,
                    player_id=player_id,
                    team_id=team_id,
                    signed_in_season=2026,
                    contract_type=ContractType.STANDARD,
                    signing_method=SigningMethod.CAP_SPACE,
                    year_salaries={2026: 1_000_000},
                )
            assert recorder.team_ids == {team_id}
            assert recorder.player_ids == {player_id}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_check_2b_waive_writes_only_the_locked_team_and_player(monkeypatch):
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "C2BB")
            player_id = await _make_player(pool, league_id, team_id)
            # Fixture setup runs UNINSTRUMENTED -- only the mutator call
            # below is the write this test is actually checking; wrapping
            # `create_contract` before this setup call would trip check
            # 2b's assertion on a write this test never claims to cover.
            contract_id = await _make_active_contract(pool, league_id, team_id, player_id)
            recorder = _instrument_write_shaped(monkeypatch)
            async with rmlp_gate(pool, league_id) as conn:
                charges = await roster_service.waive(
                    conn, league_id=league_id, contract_id=contract_id, waived_in_season=2026
                )
            # Guaranteed money was set on the contract, so this run must
            # actually exercise create_dead_cap_charge, not skip it.
            assert charges
            assert recorder.team_ids == {team_id}
            assert recorder.player_ids == {player_id}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_check_2b_release_writes_only_the_locked_team_and_player(monkeypatch):
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "C2BC")
            player_id = await _make_player(pool, league_id, team_id)
            # No guaranteed money -- release() raises if any remaining
            # season still carries a guarantee (§4.1's own guard). Fixture
            # setup runs UNINSTRUMENTED, same reasoning as the waive test
            # above.
            contract_id = await _make_active_contract(
                pool, league_id, team_id, player_id, guaranteed=0
            )
            recorder = _instrument_write_shaped(monkeypatch)
            async with rmlp_gate(pool, league_id) as conn:
                await roster_service.release(
                    conn, league_id=league_id, contract_id=contract_id, released_in_season=2026
                )
            assert recorder.team_ids == {team_id}
            assert recorder.player_ids == {player_id}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_check_2b_apply_signing_writes_only_the_locked_team_and_player(monkeypatch):
    recorder = _instrument_write_shaped(monkeypatch)
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "C2BD")
            player_id = await _make_player(pool, league_id)  # free agent, no existing contract
            execution = gm_service.SigningExecution(
                team_id=team_id,
                player_id=player_id,
                years=1,
                annual_salary=1_000_000,
                start_season=2026,
                signing_method=SigningMethod.CAP_SPACE,
                record=_dummy_record(team_id),
            )
            async with rmlp_gate(pool, league_id) as conn:
                await gm_service.apply_signing(conn, execution, league_id=league_id)
            assert recorder.team_ids == {team_id}
            assert recorder.player_ids == {player_id}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_check_2b_apply_trade_writes_only_the_locked_teams_and_players(monkeypatch):
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_a = await _make_team(pool, league_id, "C2BEA")
            team_b = await _make_team(pool, league_id, "C2BEB")
            player_a = await _make_player(pool, league_id, team_a)
            await _make_active_contract(pool, league_id, team_a, player_a)
            player_b = await _make_player(pool, league_id, team_b)
            await _make_active_contract(pool, league_id, team_b, player_b)
            # Fixture setup runs UNINSTRUMENTED, same reasoning as waive/
            # release above.
            recorder = _instrument_write_shaped(monkeypatch)

            execution = gm_service.TradeExecution(
                team_a_id=team_a,
                team_b_id=team_b,
                players_a_to_b=(player_a,),
                players_b_to_a=(player_b,),
                picks_a_to_b=(),
                picks_b_to_a=(),
                net_a=1.0,
                net_b=1.0,
                record_a=_dummy_record(team_a),
                record_b=_dummy_record(team_b),
            )
            async with rmlp_gate(pool, league_id) as conn:
                await gm_service.apply_trade(
                    conn, execution, 2026, Phase.TRADE_DEADLINE, league_id=league_id
                )
            assert recorder.team_ids == {team_a, team_b}
            assert recorder.player_ids == {player_a, player_b}
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# The mutation twin -- proves check 2b actually has teeth against the
# class of bug it exists for (see module docstring).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_2b_catches_a_wrong_entity_lock_set(monkeypatch):
    """Simulates `teams=[other_object.team_id]` -- a mutator whose `acquire`
    call names the WRONG team -- without touching production code: the
    recording wrapper installed here still delegates to the real `acquire`
    (so Postgres-level locking stays correct), but deliberately records a
    different team id than the one actually passed, exactly modeling what
    check 2's own static substring check cannot see. `create_contract`'s
    check-2b assertion must catch it before any row is written."""
    recorder = _LockRecorder()
    real_acquire = roster_lock.acquire

    async def _lying_acquire(conn, *, league_id, teams, players):
        result = await real_acquire(conn, league_id=league_id, teams=teams, players=players)
        recorder.team_ids.update(t + 9_000_000 for t in teams)
        recorder.player_ids.update(players)
        return result

    monkeypatch.setattr(roster_lock, "acquire", _lying_acquire)
    monkeypatch.setattr(
        contract_repo,
        "create_contract",
        _wrap_create_contract(recorder, contract_repo.create_contract),
    )

    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            team_id = await _make_team(pool, league_id, "C2BZ")
            player_id = await _make_player(pool, league_id)
            with pytest.raises(AssertionError, match="RMLP check 2b"):
                async with rmlp_gate(pool, league_id) as conn:
                    await roster_service.sign(
                        conn,
                        league_id=league_id,
                        player_id=player_id,
                        team_id=team_id,
                        signed_in_season=2026,
                        contract_type=ContractType.STANDARD,
                        signing_method=SigningMethod.CAP_SPACE,
                        year_salaries={2026: 1_000_000},
                    )
            # Nothing was written -- the assertion fired before the insert.
            contracts = await contract_repo.list_contracts_for_player(pool, player_id)
            assert not contracts
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()
