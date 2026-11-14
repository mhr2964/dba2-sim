"""Live-Postgres proof for `services/offer_service.py` -- docs/design/phase-5-
trade-offer-review-surface.md §7.4's numbered tests (1-13 and 15; test 14 is
the Discord button, a later slice), the legality-parity test (§12 step 10),
and the extra behaviours the service specifies (counter, veto, expiry, the
`apply_trade`-failure -> `EXPIRED` translation).

Every test here that carries a `# twin:` comment is a MUTATION TWIN in this
repo's sense (`test_rmlp_concurrency.py`'s test 13 twin is the precedent): a
test that neutralises the one mechanism the primary test exists to prove --
by monkeypatch, never by editing production code -- and asserts the failure
the mechanism prevents actually happens. A primary test with no twin proves
only that a correct implementation passes; the twin proves the assertion has
teeth.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg
import pytest

from dba2.core.errors import (
    ConcurrencyError,
    NotFoundError,
    PhaseError,
    ValidationError,
)
from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import (
    contract_repo,
    draft_repo,
    league_repo,
    offer_repo,
    player_repo,
    ratings_repo,
    team_repo,
    txn_repo,
)
from dba2.domain.contract import ContractStatus, ContractType, SigningMethod
from dba2.domain.phase import Phase, TradeStatus
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import RatingSource
from dba2.domain.team import GmPhilosophy, Timeline
from dba2.managers.decisions import trade as trade_decisions
from dba2.managers.explain import DecisionRecord
from dba2.managers.negotiation import NegotiationStance
from dba2.phase.permissions import PermissionDenied
from dba2.services import (
    gm_service,
    league_claim,
    league_service,
    offer_service,
    roster_lock,
    season_service,
)
from dba2.services.offer_service import OfferAction
from dba2.services.roster_lock import LockMode
from tests.fixtures.managers import birth_date_for_age
from tests.fixtures.sim import make_ratings

_SEASON = 2026
_COMMISSIONER = 1
_H1 = 101
_H2 = 102
_OUTSIDER = 555

_RATINGS_NON_COLUMN_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
)


def _ratings_columns(seed: int) -> dict[str, int]:
    base = make_ratings(seed)
    return {
        f.name: getattr(base, f.name)
        for f in dataclasses.fields(base)
        if f.name not in _RATINGS_NON_COLUMN_FIELDS
    }


@dataclass
class _S:
    """One scenario: a TRADE_DEADLINE league of four teams. H1 and H2 are
    human-managed, AI has no manager, CM is managed by the commissioner --
    the B3 case a `role is TEAM_MANAGER` predicate silently locks out."""

    pool: object
    league_id: int
    team: dict[str, int]
    players: dict[str, list[int]]
    picks: dict[str, int]


async def _make_league(pool, *, phase: Phase, roster_max: int = 6) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Offer Service Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=2026_19,
        current_phase=phase,
    )
    await league_repo.create_cap_settings(
        pool,
        league_id=league.id,
        season=_SEASON,
        salary_cap=150_000_000,
        luxury_tax=180_000_000,
        apron_1=190_000_000,
        apron_2=200_000_000,
        max_pct_0_6=25,
        max_pct_7_9=30,
        max_pct_10_plus=35,
        supermax_pct=35,
        min_salary=1_200_000,
        mle_amount=13_000_000,
        bi_annual_amount=5_000_000,
        raise_pct_bird=8,
        raise_pct_non_bird=5,
        roster_min=2,
        roster_max=roster_max,
    )
    return league.id


async def _make_team(pool, league_id: int, code: str, *, manager: int | None = None) -> int:
    team = await team_repo.create_team(
        pool,
        league_id=league_id,
        code=code,
        name=f"{code} Team",
        city=f"{code} City",
        conference="East",
        division="Atlantic",
        home_market_size=3,
        gm_philosophy=GmPhilosophy.EGALITARIAN,
        timeline=Timeline.RETOOL,
        manager_user_id=manager,
    )
    return team.id


async def _make_player(
    pool,
    league_id: int,
    team_id: int,
    *,
    last_name: str,
    overall: int = 70,
    status: RosterStatus = RosterStatus.ACTIVE,
) -> int:
    player = await player_repo.create_player(
        pool,
        league_id=league_id,
        first_name="Marcus",
        last_name=last_name,
        primary_position=Position.SF,
        height_in=79,
        weight_lb=215,
        wingspan_in=82,
        birth_date=birth_date_for_age(27, _SEASON),
        roster_status=status,
        overall_cached=overall,
        overall_computed_at=datetime.now(UTC),
        team_id=team_id,
    )
    await ratings_repo.upsert_player_ratings(
        pool,
        player_id=player.id,
        league_id=league_id,
        source=RatingSource.GENERATED,
        ratings=_ratings_columns(player.id),
    )
    contract = await contract_repo.create_contract(
        pool,
        league_id=league_id,
        player_id=player.id,
        team_id=team_id,
        signed_in_season=_SEASON,
        start_season=_SEASON,
        end_season=_SEASON + 1,
        total_years=2,
        contract_type=ContractType.STANDARD,
        signing_method=SigningMethod.CAP_SPACE,
    )
    for season in (_SEASON, _SEASON + 1):
        await contract_repo.create_contract_year(
            pool, contract_id=contract.id, season=season, salary=4_000_000,
            guaranteed_amount=4_000_000,
        )
    return player.id


async def _scenario(pool, *, phase: Phase = Phase.TRADE_DEADLINE, roster_max: int = 6) -> _S:
    league_id = await _make_league(pool, phase=phase, roster_max=roster_max)
    managers = {"H1": _H1, "H2": _H2, "AI": None, "CM": _COMMISSIONER}
    s = _S(pool=pool, league_id=league_id, team={}, players={}, picks={})
    for code, manager in managers.items():
        team_id = await _make_team(pool, league_id, code, manager=manager)
        s.team[code] = team_id
        s.players[code] = [
            await _make_player(
                pool, league_id, team_id, last_name=f"{code}{i}", overall=80 - 6 * i
            )
            for i in range(4)
        ]
        pick = await draft_repo.create_draft_pick(
            pool, league_id=league_id, season=_SEASON + 1, round=1,
            original_team_id=team_id, current_team_id=team_id,
        )
        s.picks[code] = pick.id
    return s


@pytest.fixture
async def s():
    pool = await get_pool()
    scenario = await _scenario(pool)
    try:
        yield scenario
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", scenario.league_id)
        await close_pool()


async def _offer(
    s: _S,
    from_code: str,
    to_code: str,
    *,
    give: tuple[int, ...],
    get: tuple[int, ...],
    give_picks: tuple[int, ...] = (),
    get_picks: tuple[int, ...] = (),
    proposed_by: int | None,
    phase: Phase = Phase.TRADE_DEADLINE,
    season: int = _SEASON,
    status: TradeStatus = TradeStatus.PROPOSED,
    counters_offer_id: int | None = None,
):
    """A PROPOSED row written straight through the repo -- the same call
    `run_phase_hook`'s offer loop makes -- so a test can start from an
    existing offer without routing through `propose`."""
    return await offer_repo.create_trade_offer(
        s.pool,
        league_id=s.league_id,
        season=season,
        phase=phase,
        from_team_id=s.team[from_code],
        to_team_id=s.team[to_code],
        proposed_by=proposed_by,
        status=status,
        players_from_to=give,
        players_to_from=get,
        picks_from_to=give_picks,
        picks_to_from=get_picks,
        counters_offer_id=counters_offer_id,
    )


async def _h1_to_h2(s: _S, **kw):
    """A legal 1-for-1 human->human offer: H1 sends its player 0, wants H2's."""
    return await _offer(
        s, "H1", "H2", give=(s.players["H1"][0],), get=(s.players["H2"][0],),
        proposed_by=_H1, **kw,
    )


async def _accept(s: _S, offer_id: int, *, by: int = _H2):
    return await offer_service.respond(
        s.pool, league_id=s.league_id, offer_id=offer_id, action=OfferAction.ACCEPT,
        actor_user_id=by,
    )


async def _txns(s: _S):
    return await txn_repo.list_transactions_for_league(s.pool, s.league_id)


async def _team_of(s: _S, player_id: int) -> int:
    return (await player_repo.get_player(s.pool, player_id)).team_id


async def _status(s: _S, offer_id: int) -> TradeStatus:
    return (await offer_repo.get_trade_offer(s.pool, offer_id)).status


def _stub_ai(monkeypatch, *, verdict: str, seen: list[int] | None = None):
    """Replaces `evaluate_trade` with a fixed-verdict record. `seen`
    collects every `team_snapshot.id` it is called with (test 11)."""

    def fake(team_snapshot, incoming, outgoing, philosophy, window, **_kw):
        if seen is not None:
            seen.append(team_snapshot.id)
        return DecisionRecord(
            team_id=team_snapshot.id, philosophy=philosophy, window=window,
            subject=f"team:{team_snapshot.id}", verdict=verdict,
            score=100.0 if verdict == "accept" else 0.0, threshold=1.0, terms=(),
        )

    monkeypatch.setattr(trade_decisions, "evaluate_trade", fake)


async def _propose(s: _S, from_code: str, to_code: str, *, give, get, by: int,
                   give_picks=(), get_picks=()):
    return await offer_service.propose(
        s.pool,
        league_id=s.league_id,
        from_team_id=s.team[from_code],
        to_team_id=s.team[to_code],
        players_from_to=tuple(give),
        players_to_from=tuple(get),
        picks_from_to=tuple(give_picks),
        picks_to_from=tuple(get_picks),
        actor_user_id=by,
    )


@asynccontextmanager
async def _held(pool, setup):
    """Hold `setup(conn)`'s locks on a SECOND pooled connection, in a second
    task, for the duration of the `with` block. `data/db.py`'s re-entrancy
    guard is task-scoped, so a test that opened the holder's transaction in
    its own task would trip `TransactionError` the moment the code under test
    opened its own -- a separate task is what makes the two-connection shape
    legal (§7.4's note on tests 4a-4c)."""
    ready, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with transaction(pool) as conn:
            await setup(conn)
            ready.set()
            await release.wait()

    task = asyncio.create_task(holder())
    waiter = asyncio.create_task(ready.wait())
    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if not ready.is_set():
        await task  # the holder failed before it held anything: surface why
    try:
        yield
    finally:
        release.set()
        waiter.cancel()
        await task


# ---------------------------------------------------------------------------
# propose: the AI answers synchronously, in the proposing transaction
# ---------------------------------------------------------------------------


async def test_propose_to_a_human_leaves_a_proposed_row_and_moves_nothing(s):
    result = await _propose(s, "H1", "H2", give=[s.players["H1"][0]], get=[s.players["H2"][0]],
                            by=_H1)
    assert result.changed is True
    assert result.offer.status is TradeStatus.PROPOSED
    assert result.offer.proposed_by == _H1
    assert result.offer.season == _SEASON and result.offer.phase is Phase.TRADE_DEADLINE
    assert result.offer.message_id is None
    assert await _txns(s) == []
    assert await _team_of(s, s.players["H1"][0]) == s.team["H1"]


async def test_propose_to_an_ai_that_accepts_executes_the_trade_in_one_transaction(
    s, monkeypatch
):
    _stub_ai(monkeypatch, verdict="accept")
    give, get = s.players["H1"][0], s.players["AI"][0]
    result = await _propose(s, "H1", "AI", give=[give], get=[get], by=_H1,
                            give_picks=[s.picks["H1"]])
    assert result.offer.status is TradeStatus.ACCEPTED
    assert result.offer.resolved_by is None and result.offer.resolved_at is not None
    assert await _team_of(s, give) == s.team["AI"]
    assert await _team_of(s, get) == s.team["H1"]
    assert (await draft_repo.get_draft_pick(s.pool, s.picks["H1"])).current_team_id == s.team["AI"]
    txns = await _txns(s)
    assert len(txns) == 1
    assert txns[0].source_offer_id == result.offer.id
    assert txns[0].actor_user_id == _H1


async def test_propose_to_an_ai_that_declines_records_a_rejected_offer(s, monkeypatch):
    _stub_ai(monkeypatch, verdict="reject")
    result = await _propose(s, "H1", "AI", give=[s.players["H1"][0]], get=[s.players["AI"][0]],
                            by=_H1)
    assert result.offer.status is TradeStatus.REJECTED
    assert result.offer.resolved_by is None
    assert result.detail and "AI" in result.detail
    assert await _txns(s) == []
    assert await _team_of(s, s.players["H1"][0]) == s.team["H1"]


async def test_propose_with_the_real_evaluator_runs_end_to_end(s):
    """No stub: exercises `evaluate_trade`'s real argument set. Either verdict
    is a legitimate AI answer; what must hold is that it resolves."""
    result = await _propose(s, "H1", "AI", give=[s.players["H1"][0]], get=[s.players["AI"][3]],
                            by=_H1)
    assert result.offer.status in (TradeStatus.ACCEPTED, TradeStatus.REJECTED)


# ---------------------------------------------------------------------------
# §7.4 test 1 -- crash injection, accept path
# ---------------------------------------------------------------------------


async def test_1_a_crash_inside_apply_trade_leaves_the_offer_proposed_and_retry_succeeds(
    s, monkeypatch
):
    offer = await _h1_to_h2(s)
    real = txn_repo.create_trade_transaction

    async def boom(*_a, **_kw):
        raise RuntimeError("injected crash after the player/contract/pick writes")

    monkeypatch.setattr(txn_repo, "create_trade_transaction", boom)
    with pytest.raises(RuntimeError, match="injected crash"):
        await _accept(s, offer.id)

    assert await _status(s, offer.id) is TradeStatus.PROPOSED
    assert await _team_of(s, s.players["H1"][0]) == s.team["H1"]
    assert await _team_of(s, s.players["H2"][0]) == s.team["H2"]
    assert await _txns(s) == []

    monkeypatch.setattr(txn_repo, "create_trade_transaction", real)
    result = await _accept(s, offer.id)
    assert result.offer.status is TradeStatus.ACCEPTED
    assert len(await _txns(s)) == 1
    assert await _team_of(s, s.players["H1"][0]) == s.team["H2"]


# ---------------------------------------------------------------------------
# §7.4 test 2 -- crash injection, propose path (the orphan test)
# ---------------------------------------------------------------------------


async def test_2_a_crash_after_the_insert_leaves_no_offer_row(s, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("injected crash in the AI-answer step")

    monkeypatch.setattr(offer_service, "_stance_for", boom)
    _stub_ai(monkeypatch, verdict="accept")
    with pytest.raises(RuntimeError, match="injected crash"):
        await _propose(s, "H1", "AI", give=[s.players["H1"][0]], get=[s.players["AI"][0]],
                       by=_H1)
    assert await offer_repo.list_trade_offers_for_league(s.pool, s.league_id) == []


# ---------------------------------------------------------------------------
# §7.4 test 3 -- two concurrent accepts
# ---------------------------------------------------------------------------


async def test_3_two_concurrent_accepts_apply_exactly_one_trade(s):
    offer = await _h1_to_h2(s)
    results = await asyncio.gather(
        _accept(s, offer.id), _accept(s, offer.id), return_exceptions=True
    )
    assert len(await _txns(s)) == 1  # the invariant that is NOT a disjunction
    assert await _status(s, offer.id) is TradeStatus.ACCEPTED
    winners = [r for r in results if not isinstance(r, Exception) and r.changed]
    assert len(winners) == 1
    for r in results:
        if isinstance(r, Exception):
            assert isinstance(r, ConcurrencyError)  # lost on the 3s lock_timeout
        elif not r.changed:
            assert "already accepted" in r.detail  # lost on the re-read
    assert await _team_of(s, s.players["H1"][0]) == s.team["H2"]  # moved once, not back


# ---------------------------------------------------------------------------
# §7.4 tests 4a-4d -- the gate and the claim, deterministically
# ---------------------------------------------------------------------------


async def test_4a_exclusive_gate_held_refuses_the_accept(s):
    offer = await _h1_to_h2(s)

    async def take_exclusive_gate(conn):
        await roster_lock.open_league_gate(conn, league_id=s.league_id, mode=LockMode.EXCLUSIVE)

    async with _held(s.pool, take_exclusive_gate):
        with pytest.raises(ConcurrencyError, match="AI pass"):
            await _accept(s, offer.id)
    assert await _status(s, offer.id) is TradeStatus.PROPOSED
    assert await _txns(s) == []
    assert await _team_of(s, s.players["H1"][0]) == s.team["H1"]


async def test_4b_shared_gate_held_stops_the_ai_trade_branch(s, monkeypatch):
    monkeypatch.setitem(roster_lock._GATE_TIMEOUT_S, LockMode.EXCLUSIVE, "100ms")

    async def take_shared_gate(conn):
        await roster_lock.open_league_gate(conn, league_id=s.league_id, mode=LockMode.SHARED)

    async with _held(s.pool, take_shared_gate):
        # The gate conflict's own message: any other ConcurrencyError (a row
        # lock, a claim) would satisfy a bare type match and prove nothing.
        with pytest.raises(ConcurrencyError, match="roster gate is still in use"):
            await gm_service.run_phase_hook(
                s.pool, league_id=s.league_id, phase=Phase.TRADE_DEADLINE, actor_user_id=1
            )


async def _hold_claim(pool, league_id: int, *, held: asyncio.Event, release: asyncio.Event):
    """Connection 1: an UNCOMMITTED `UPDATE leagues SET advance_claimed_at` --
    what a Stage E press does. Holds the row lock until `release` is set."""
    async with transaction(pool) as conn:
        await conn.execute(
            "UPDATE leagues SET advance_claimed_at = NOW(), advance_claimed_by = 99 "
            "WHERE id = $1",
            league_id,
        )
        held.set()
        await release.wait()


async def test_4c_i_a_claim_held_briefly_blocks_the_accept_then_refuses_it(s):
    offer = await _h1_to_h2(s)
    held, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_claim(s.pool, s.league_id, held=held, release=release))
    await held.wait()
    task = asyncio.create_task(_accept(s, offer.id))
    done, _pending = await asyncio.wait({task}, timeout=0.5)
    assert not done, "the accept did not block on the leagues row -- the claim TOCTOU is open"
    release.set()
    await holder
    with pytest.raises(ConcurrencyError, match="advancing"):
        await task
    assert await _status(s, offer.id) is TradeStatus.PROPOSED
    assert await _txns(s) == []


async def test_4c_ii_a_claim_held_past_lock_timeout_is_a_designed_concurrency_error(
    s, monkeypatch
):
    monkeypatch.setitem(roster_lock._GATE_TIMEOUT_S, LockMode.SHARED, "200ms")
    offer = await _h1_to_h2(s)
    held, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_claim(s.pool, s.league_id, held=held, release=release))
    await held.wait()
    try:
        # Type, not message: a leagues-row conflict reports under the generic
        # entity wording (OS-13), and pinning that string would need
        # rewriting the day OS-13 closes.
        with pytest.raises(ConcurrencyError):
            await _accept(s, offer.id)
    finally:
        release.set()
        await holder
    assert await _status(s, offer.id) is TradeStatus.PROPOSED
    assert await _txns(s) == []


async def test_4c_ii_twin_without_the_decorator_a_raw_asyncpg_error_escapes(s, monkeypatch):
    """# twin: `respond.__wrapped__` is the function with
    `@maps_lock_contention` removed. Without it the raw 55P03 propagates,
    misses every known-domain-error tuple, and the human is told "something
    went wrong" for the one case this design has a sentence for."""
    monkeypatch.setitem(roster_lock._GATE_TIMEOUT_S, LockMode.SHARED, "200ms")
    offer = await _h1_to_h2(s)
    held, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_claim(s.pool, s.league_id, held=held, release=release))
    await held.wait()
    try:
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await offer_service.respond.__wrapped__(
                s.pool, league_id=s.league_id, offer_id=offer.id, action=OfferAction.ACCEPT,
                actor_user_id=_H2,
            )
    finally:
        release.set()
        await holder


async def test_4d_a_committed_claim_refuses_the_accept(s):
    offer = await _h1_to_h2(s)
    await s.pool.execute(
        "UPDATE leagues SET advance_claimed_at = NOW(), advance_claimed_by = 99 WHERE id = $1",
        s.league_id,
    )
    with pytest.raises(ConcurrencyError, match="advancing"):
        await _accept(s, offer.id)
    assert await _status(s, offer.id) is TradeStatus.PROPOSED


async def test_a_committed_claim_also_refuses_propose_and_veto(s):
    offer = await _h1_to_h2(s)
    await s.pool.execute(
        "UPDATE leagues SET advance_claimed_at = NOW(), advance_claimed_by = 99 WHERE id = $1",
        s.league_id,
    )
    with pytest.raises(ConcurrencyError, match="advancing"):
        await _propose(s, "H2", "H1", give=[s.players["H2"][1]], get=[s.players["H1"][1]],
                       by=_H2)
    with pytest.raises(ConcurrencyError, match="advancing"):
        await offer_service.veto(
            s.pool, league_id=s.league_id, offer_id=offer.id, actor_user_id=_COMMISSIONER
        )


# ---------------------------------------------------------------------------
# The claim the REAL writer takes (Stage E build-plan commit 3, OS-7).
#
# `test_4c_*` above hold the claim with a hand-written `UPDATE`. That proved the
# reader; it could not prove the writer, because until commit 3 there was no
# writer. These are the same three shapes -- blocks then refuses, refuses past
# the timeout as a designed error, refuses once committed -- with the claim
# taken by `league_repo.claim_advance` (the statement `_claim_league` runs) and
# by `season_service._claim_league` itself, plus the release that lets the
# accept through.
# ---------------------------------------------------------------------------


async def _hold_real_claim(pool, league_id: int, *, held: asyncio.Event, release: asyncio.Event):
    async with transaction(pool) as conn:
        assert await league_repo.claim_advance(conn, league_id, 99) is not None
        held.set()
        await release.wait()


async def test_a_claim_taken_by_the_real_writer_blocks_the_accept_then_refuses_it(s):
    offer = await _h1_to_h2(s)
    held, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_real_claim(s.pool, s.league_id, held=held, release=release))
    await held.wait()
    task = asyncio.create_task(_accept(s, offer.id))
    done, _pending = await asyncio.wait({task}, timeout=0.5)
    assert not done, "the accept did not block on the leagues row -- the claim TOCTOU is open"
    release.set()
    await holder
    with pytest.raises(ConcurrencyError) as refused:
        await task
    assert str(refused.value) == league_claim.CLAIM_HELD_MESSAGE
    assert await _status(s, offer.id) is TradeStatus.PROPOSED
    assert await _txns(s) == []


async def test_a_claim_taken_by_the_real_writer_held_past_lock_timeout_is_a_designed_error(
    s, monkeypatch
):
    monkeypatch.setitem(roster_lock._GATE_TIMEOUT_S, LockMode.SHARED, "200ms")
    offer = await _h1_to_h2(s)
    held, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_real_claim(s.pool, s.league_id, held=held, release=release))
    await held.wait()
    try:
        with pytest.raises(ConcurrencyError):
            await _accept(s, offer.id)
    finally:
        release.set()
        await holder
    assert await _status(s, offer.id) is TradeStatus.PROPOSED


async def test_a_committed_claim_from_claim_league_refuses_the_accept_until_it_is_released(s):
    offer = await _h1_to_h2(s)
    claimed_at = await season_service._claim_league(s.pool, s.league_id, 99)
    with pytest.raises(ConcurrencyError) as refused:
        await _accept(s, offer.id)
    assert str(refused.value) == league_claim.CLAIM_HELD_MESSAGE
    assert await _status(s, offer.id) is TradeStatus.PROPOSED

    assert await season_service._release_claim(s.pool, s.league_id, 99, claimed_at) is True
    await _accept(s, offer.id)
    assert await _status(s, offer.id) is TradeStatus.ACCEPTED


# ---------------------------------------------------------------------------
# §7.4 test 5 -- restart survival, end to end
# ---------------------------------------------------------------------------


async def test_5_an_offer_written_by_the_real_phase_hook_survives_a_full_restart(monkeypatch):
    """The literal D'-16 obligation: an AI-initiated offer produced through
    the real `run_phase_hook` trade branch, then every in-process object
    dropped (the pool is closed and a fresh one opened -- nothing else
    carries over), is still listed by `list_offers` and still executes on
    `respond`. Mirrors `test_gm_service.py`'s own synthetic-human test for the
    hook half, monkeypatching only candidate generation."""
    pool = await get_pool()
    league_id = None
    try:
        league_id = await _make_league(pool, phase=Phase.REGULAR_SEASON)
        team_ai = await _make_team(pool, league_id, "AI")
        team_human = await _make_team(pool, league_id, "HU", manager=_H1)
        star = await _make_player(pool, league_id, team_human, last_name="Star", overall=88)
        await _make_player(pool, league_id, team_human, last_name="Bench", overall=64)
        filler = await _make_player(pool, league_id, team_ai, last_name="Filler", overall=68)
        await _make_player(pool, league_id, team_ai, last_name="Depth", overall=60)

        candidate = gm_service.CandidatePackage(
            team_a_id=team_ai, team_b_id=team_human,
            incoming_a=gm_service.AssetBundle(), outgoing_a=gm_service.AssetBundle(),
        )

        def fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
            if (proposer_id, partner_id) == (team_ai, team_human):
                star_rp = snap.team_by_id(team_human).player_by_id(star)
                filler_rp = snap.team_by_id(team_ai).player_by_id(filler)
                return (
                    dataclasses.replace(
                        candidate,
                        incoming_a=gm_service.AssetBundle(players=(star_rp,)),
                        outgoing_a=gm_service.AssetBundle(players=(filler_rp,)),
                    ),
                )
            return ()

        def fake_evaluate(team_snapshot, incoming, outgoing, philosophy, window, **_kw):
            return DecisionRecord(
                team_id=team_snapshot.id, philosophy=philosophy, window=window,
                subject=f"team:{team_snapshot.id}", verdict="accept", score=100.0,
                threshold=0.0, terms=(),
            )

        with monkeypatch.context() as m:
            m.setattr(gm_service, "propose_packages", fake_propose_packages)
            m.setattr(trade_decisions, "evaluate_trade", fake_evaluate)
            m.setattr(trade_decisions, "_check_legality", lambda *_a, **_k: None)
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await gm_service.run_phase_hook(
                pool, league_id=league_id, phase=Phase.REGULAR_SEASON, actor_user_id=1
            )

        # --- the restart: nothing in-process survives but the database ---
        await close_pool()
        pool = await get_pool()

        listed = await offer_service.list_offers(
            pool, league_id=league_id, team_id=team_human, actor_user_id=_H1
        )
        assert len(listed.received) == 1 and listed.sent == ()
        offer = listed.received[0]
        assert offer.proposed_by is None and offer.status is TradeStatus.PROPOSED

        result = await offer_service.respond(
            pool, league_id=league_id, offer_id=offer.id, action=OfferAction.ACCEPT,
            actor_user_id=_H1,
        )
        assert result.offer.status is TradeStatus.ACCEPTED, result.detail
        assert (await player_repo.get_player(pool, star)).team_id == team_ai
        assert (await player_repo.get_player(pool, filler)).team_id == team_human
        assert len(await txn_repo.list_transactions_for_league(pool, league_id)) == 1
    finally:
        if league_id is not None:
            gm_service.artifact_path(league_id, _SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


# ---------------------------------------------------------------------------
# §7.4 test 6 -- delivery idempotence (the repo half; the drain is bot/)
# ---------------------------------------------------------------------------


async def test_6_the_undelivered_queue_is_message_id_null_and_redelivery_is_accepted(s):
    a = await _h1_to_h2(s)
    b = await _offer(s, "H2", "H1", give=(s.players["H2"][1],), get=(s.players["H1"][1],),
                     proposed_by=_H2)
    queue = await offer_repo.list_undelivered_offers(s.pool, s.league_id)
    assert [o.id for o in queue] == [a.id, b.id]

    await offer_repo.set_offer_message(s.pool, a.id, channel_id=10, message_id=20)
    queue = await offer_repo.list_undelivered_offers(s.pool, s.league_id)
    assert [o.id for o in queue] == [b.id]
    delivered = await offer_repo.get_trade_offer(s.pool, a.id)
    assert (delivered.channel_id, delivered.message_id) == (10, 20)

    # The crash between post and write-back: message_id is NULL again, and
    # the accepted at-least-once behaviour is that the drain re-posts it.
    await s.pool.execute("UPDATE trade_offers SET message_id = NULL WHERE id = $1", a.id)
    queue = await offer_repo.list_undelivered_offers(s.pool, s.league_id)
    assert [o.id for o in queue] == [a.id, b.id]


async def test_the_undelivered_queue_excludes_resolved_offers(s):
    a = await _h1_to_h2(s)
    await offer_repo.resolve_offer_cas(
        s.pool, a.id, expected_status=TradeStatus.PROPOSED, new_status=TradeStatus.REJECTED,
        resolved_by=_H2,
    )
    assert await offer_repo.list_undelivered_offers(s.pool, s.league_id) == []


# ---------------------------------------------------------------------------
# §7.4 test 7 -- expiry
# ---------------------------------------------------------------------------


async def test_7_an_offer_expires_on_the_first_read_after_a_phase_change_not_before(s):
    offer = await _h1_to_h2(s)
    same = await offer_service.list_offers(
        s.pool, league_id=s.league_id, team_id=s.team["H2"], actor_user_id=_H2
    )
    assert [o.id for o in same.received] == [offer.id]

    # A block-press-shaped re-entry: the pointer is re-written to the SAME
    # phase. (season, phase) is unchanged, so the offer survives.
    await league_repo.set_current_phase(s.pool, s.league_id, Phase.TRADE_DEADLINE)
    still = await offer_service.list_offers(
        s.pool, league_id=s.league_id, team_id=s.team["H2"], actor_user_id=_H2
    )
    assert [o.id for o in still.received] == [offer.id]

    await league_repo.set_current_phase(s.pool, s.league_id, Phase.REGULAR_SEASON)
    after = await offer_service.list_offers(
        s.pool, league_id=s.league_id, team_id=s.team["H2"], actor_user_id=_H2
    )
    assert after.received == ()
    row = await offer_repo.get_trade_offer(s.pool, offer.id)
    assert row.status is TradeStatus.EXPIRED and row.resolved_at is not None
    assert row.resolved_by is None

    stamp = row.resolved_at
    await offer_service.list_offers(
        s.pool, league_id=s.league_id, team_id=s.team["H2"], actor_user_id=_H2
    )
    assert (await offer_repo.get_trade_offer(s.pool, offer.id)).resolved_at == stamp  # no-op


async def test_7_b_answering_a_stale_offer_in_a_still_open_phase_writes_expired(s):
    offer = await _h1_to_h2(s, phase=Phase.TRADE_DEADLINE)
    await league_repo.set_current_phase(s.pool, s.league_id, Phase.REGULAR_SEASON)
    result = await _accept(s, offer.id)
    assert result.offer.status is TradeStatus.EXPIRED
    assert "moved on" in result.detail
    assert await _txns(s) == []
    assert await _team_of(s, s.players["H1"][0]) == s.team["H1"]


async def test_7_c_answering_in_a_closed_phase_is_a_phase_error_and_writes_nothing(s):
    offer = await _h1_to_h2(s)
    await league_repo.set_current_phase(s.pool, s.league_id, Phase.POST_DEADLINE)
    with pytest.raises(PhaseError):
        await _accept(s, offer.id)
    assert await _status(s, offer.id) is TradeStatus.PROPOSED


async def test_list_offers_splits_received_from_sent_and_hides_resolved_offers(s):
    received = await _offer(
        s, "H2", "H1", give=(s.players["H2"][0],), get=(s.players["H1"][0],), proposed_by=_H2,
    )
    sent = await _offer(
        s, "H1", "H2", give=(s.players["H1"][1],), get=(s.players["H2"][1],), proposed_by=_H1,
    )
    closed = await _offer(
        s, "H2", "H1", give=(s.players["H2"][2],), get=(s.players["H1"][2],), proposed_by=_H2,
    )
    await offer_repo.resolve_offer_cas(
        s.pool, closed.id, expected_status=TradeStatus.PROPOSED,
        new_status=TradeStatus.REJECTED, resolved_by=_H1,
    )
    listed = await offer_service.list_offers(
        s.pool, league_id=s.league_id, team_id=s.team["H1"], actor_user_id=_H1
    )
    assert [o.id for o in listed.received] == [received.id]
    assert [o.id for o in listed.sent] == [sent.id]
    with pytest.raises(PermissionDenied):  # a manager may read only their own team
        await offer_service.list_offers(
            s.pool, league_id=s.league_id, team_id=s.team["H1"], actor_user_id=_H2
        )


async def test_the_sweep_reads_the_leagues_current_phase_not_a_callers_stale_one(s):
    """BL-D. Advance the league between a caller's read and the sweep: the
    sweep must compare against the phase it reads UNDER its own lock. A
    parameterised sweep carrying the caller's stale phase would expire every
    PROPOSED row in the NEW phase, including one the AI's post-advance hook
    just inserted."""
    old = await _h1_to_h2(s, phase=Phase.TRADE_DEADLINE)
    stale_read = await league_repo.get_league(s.pool, s.league_id)  # the caller's read
    await league_repo.set_current_phase(s.pool, s.league_id, Phase.REGULAR_SEASON)  # advance
    fresh = await _offer(
        s, "H2", "H1", give=(s.players["H2"][1],), get=(s.players["H1"][1],),
        proposed_by=None, phase=Phase.REGULAR_SEASON,
    )

    expired = await offer_repo.expire_stale_offers_for_league(s.pool, s.league_id)

    assert expired == 1
    assert await _status(s, old.id) is TradeStatus.EXPIRED
    assert await _status(s, fresh.id) is TradeStatus.PROPOSED

    # twin: the parameterised shape, fed the caller's stale phase, expires
    # the fresh row -- the defect the no-parameter signature exists to prevent.
    await s.pool.execute(
        "UPDATE trade_offers SET status = 'expired' WHERE league_id = $1 AND status = 'proposed' "
        "AND (season, phase) IS DISTINCT FROM ($2::int, $3::text)",
        s.league_id, stale_read.current_season, stale_read.current_phase.value,
    )
    assert await _status(s, fresh.id) is TradeStatus.EXPIRED


async def test_the_sweep_waits_out_an_advance_in_flight_and_then_uses_the_new_phase(s):
    """The blocking half of BL-D: an advance holds `leagues FOR UPDATE`; the
    sweep's `FOR SHARE` queues behind it, and when the advance commits the
    sweep reads the NEW phase."""
    old = await _h1_to_h2(s, phase=Phase.TRADE_DEADLINE)
    fresh = await _offer(
        s, "H2", "H1", give=(s.players["H2"][1],), get=(s.players["H1"][1],),
        proposed_by=None, phase=Phase.REGULAR_SEASON,
    )
    locked, release = asyncio.Event(), asyncio.Event()

    async def advance():
        async with transaction(s.pool) as conn:
            await league_repo.get_phase_state_for_update(conn, s.league_id)
            locked.set()
            await release.wait()
            await league_repo.set_current_phase(conn, s.league_id, Phase.REGULAR_SEASON)

    adv = asyncio.create_task(advance())
    await locked.wait()
    sweep = asyncio.create_task(offer_repo.expire_stale_offers_for_league(s.pool, s.league_id))
    done, _ = await asyncio.wait({sweep}, timeout=0.5)
    assert not done, "the sweep did not queue behind the advance's FOR UPDATE"
    release.set()
    await adv
    assert await sweep == 1
    assert await _status(s, old.id) is TradeStatus.EXPIRED
    assert await _status(s, fresh.id) is TradeStatus.PROPOSED


# ---------------------------------------------------------------------------
# §7.4 test 8 -- the proposer re-affirms its own side (must-fire)
# ---------------------------------------------------------------------------


async def _ai_offer(s: _S):
    """An AI-initiated offer to H1: AI sends its player 0, wants H1's."""
    return await _offer(
        s, "AI", "H1", give=(s.players["AI"][0],), get=(s.players["H1"][0],), proposed_by=None,
    )


async def test_8_an_ai_proposer_that_no_longer_wants_the_deal_expires_it(s, monkeypatch):
    offer = await _ai_offer(s)
    _stub_ai(monkeypatch, verdict="reject")
    result = await _accept(s, offer.id, by=_H1)
    assert result.offer.status is TradeStatus.EXPIRED
    assert "no longer wants" in result.detail
    assert await _txns(s) == []
    assert await _team_of(s, s.players["AI"][0]) == s.team["AI"]


@pytest.mark.parametrize(
    ("stance", "token", "detail", "alarm"),
    [
        (
            NegotiationStance.REJECT_ILLEGAL, "illegal:roster_max",
            "The league's rules changed under this deal.", "invariant violation",
        ),
        (NegotiationStance.COUNTER, None, "no longer wants this deal.", "OS-4"),
        (NegotiationStance.EXPIRE, None, "no longer wants this deal.", None),
        (NegotiationStance.REJECT_TOO_FAR, None, "no longer wants this deal.", None),
    ],
)
async def test_8_every_non_accept_stance_on_the_ai_proposer_branch_expires_with_its_own_cause(
    s, monkeypatch, caplog, stance, token, detail, alarm
):
    """The AI-proposer branch shares the stance map (and its invariant alarms)
    with the AI-partner branch; the deliberate difference is that a plain
    refusal is EXPIRED, not REJECTED."""
    offer = await _ai_offer(s)
    _stub_ai(monkeypatch, verdict="accept")
    monkeypatch.setattr(offer_service, "_stance_for", lambda a, b: (stance, token))
    with caplog.at_level(logging.ERROR, logger=offer_service.__name__):
        result = await _accept(s, offer.id, by=_H1)
    assert result.offer.status is TradeStatus.EXPIRED
    assert result.offer.illegal_token == token
    assert detail in result.detail
    assert await _txns(s) == []
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    if alarm is None:
        assert errors == []
    else:
        assert len(errors) == 1
        assert alarm in errors[0]


async def test_8_control_an_ai_proposer_that_still_wants_it_executes(s, monkeypatch):
    offer = await _ai_offer(s)
    _stub_ai(monkeypatch, verdict="accept")
    result = await _accept(s, offer.id, by=_H1)
    assert result.offer.status is TradeStatus.ACCEPTED
    assert result.offer.resolved_by == _H1
    assert len(await _txns(s)) == 1


async def test_8_twin_without_the_reaffirmation_step_the_stale_ai_offer_executes(
    s, monkeypatch
):
    """# twin: `_stance_for` forced to ACCEPT is step 10 deleted -- and the
    trade executes against an AI that would refuse it."""
    offer = await _ai_offer(s)
    _stub_ai(monkeypatch, verdict="reject")
    monkeypatch.setattr(
        offer_service, "_stance_for", lambda a, b: (NegotiationStance.ACCEPT, None)
    )
    result = await _accept(s, offer.id, by=_H1)
    assert result.offer.status is TradeStatus.ACCEPTED
    assert len(await _txns(s)) == 1


# ---------------------------------------------------------------------------
# §7.4 test 9 -- package validation, integration half
# ---------------------------------------------------------------------------


async def test_9_a_duplicated_id_is_refused_with_no_row_and_no_transaction(s, monkeypatch):
    _stub_ai(monkeypatch, verdict="accept")
    p = s.players["H1"][0]
    with pytest.raises(ValidationError, match=str(p)):
        await _propose(s, "H1", "AI", give=[p, p], get=[s.players["AI"][0]], by=_H1)
    assert await offer_repo.list_trade_offers_for_league(s.pool, s.league_id) == []
    assert await _txns(s) == []


async def test_9_twin_without_the_duplicate_check_an_asset_row_is_written_twice(
    s, monkeypatch
):
    """# twin: `_validate_package_ids` neutralised. `create_trade_transaction`
    has no de-dup and no unique index, so one player moved yields two asset
    rows -- the corruption the check prevents."""
    _stub_ai(monkeypatch, verdict="accept")
    monkeypatch.setattr(offer_service, "_validate_package_ids", lambda **_kw: None)
    p = s.players["H1"][0]
    q = s.players["AI"][0]
    result = await _propose(s, "H1", "AI", give=[p, p], get=[q], by=_H1)
    assert result.offer.status is TradeStatus.ACCEPTED
    (txn,) = await _txns(s)
    assets = await txn_repo.list_transaction_assets(s.pool, txn.id)
    assert len(assets) == 3  # p twice + q once, for TWO distinct players moved


# ---------------------------------------------------------------------------
# §7.4 test 10 -- the stance map is total
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stance", "expected"),
    [
        (NegotiationStance.ACCEPT, TradeStatus.ACCEPTED),
        (NegotiationStance.REJECT_TOO_FAR, TradeStatus.REJECTED),
        # The one to get right: the stance EXPIRE is not the status EXPIRED.
        (NegotiationStance.EXPIRE, TradeStatus.REJECTED),
        (NegotiationStance.REJECT_DEADLOCK, TradeStatus.REJECTED),
        (NegotiationStance.REJECT_ILLEGAL, TradeStatus.EXPIRED),
        (NegotiationStance.COUNTER, TradeStatus.REJECTED),
    ],
)
async def test_10_every_stance_resolves_the_row(s, monkeypatch, stance, expected):
    _stub_ai(monkeypatch, verdict="accept")
    token = "illegal:roster_max" if stance is NegotiationStance.REJECT_ILLEGAL else None
    monkeypatch.setattr(offer_service, "_stance_for", lambda a, b: (stance, token))
    result = await _propose(s, "H1", "AI", give=[s.players["H1"][0]], get=[s.players["AI"][0]],
                            by=_H1)
    assert result.offer.status is expected
    assert result.offer.status is not TradeStatus.PROPOSED
    assert result.offer.illegal_token == token
    assert (len(await _txns(s)) == 1) is (expected is TradeStatus.ACCEPTED)


# ---------------------------------------------------------------------------
# §7.4 test 11 -- a human's own team is never evaluated
# ---------------------------------------------------------------------------


async def test_11_a_humans_team_is_never_passed_to_evaluate_trade(s, monkeypatch):
    seen: list[int] = []
    real = trade_decisions.evaluate_trade

    def recording(team_snapshot, *args, **kwargs):
        seen.append(team_snapshot.id)
        return real(team_snapshot, *args, **kwargs)

    monkeypatch.setattr(trade_decisions, "evaluate_trade", recording)
    await _propose(s, "H1", "AI", give=[s.players["H1"][0]], get=[s.players["AI"][3]], by=_H1)
    assert seen and set(seen) == {s.team["AI"]}
    assert s.team["H1"] not in seen


async def test_11_a_human_to_human_accept_never_calls_evaluate_trade(s, monkeypatch):
    seen: list[int] = []
    _stub_ai(monkeypatch, verdict="accept", seen=seen)
    offer = await _h1_to_h2(s)
    result = await _accept(s, offer.id)
    assert result.offer.status is TradeStatus.ACCEPTED
    assert seen == []


async def test_11_twin_a_valuation_of_the_humans_team_lets_a_model_veto_the_human(
    s, monkeypatch
):
    """# twin: round 1's shape -- an AI model of the human's own team as
    `record_a`. If that model says "reject", a package the AI partner wants
    is refused, overriding the human's own decision."""
    _stub_ai(monkeypatch, verdict="accept")
    real_consent = offer_service._consent_record

    def modelled(team, *, offer_id):
        return dataclasses.replace(real_consent(team, offer_id=offer_id), verdict="reject")

    monkeypatch.setattr(offer_service, "_consent_record", modelled)
    result = await _propose(s, "H1", "AI", give=[s.players["H1"][0]], get=[s.players["AI"][0]],
                            by=_H1)
    assert result.offer.status is TradeStatus.REJECTED  # the human's own proposal, refused


# ---------------------------------------------------------------------------
# §7.4 test 12 -- who may act
# ---------------------------------------------------------------------------


async def test_12_the_commissioner_who_also_manages_the_team_may_propose_for_it(s, monkeypatch):
    _stub_ai(monkeypatch, verdict="reject")
    result = await _propose(s, "CM", "AI", give=[s.players["CM"][0]], get=[s.players["AI"][0]],
                            by=_COMMISSIONER)
    assert result.offer.proposed_by == _COMMISSIONER


async def test_12_a_commissioner_who_does_not_manage_the_team_is_denied_all_four_actions(s):
    offer = await _h1_to_h2(s)
    with pytest.raises(PermissionDenied, match="veto-offer"):
        await _propose(s, "H1", "H2", give=[s.players["H1"][1]], get=[s.players["H2"][1]],
                       by=_COMMISSIONER)
    for action in (OfferAction.ACCEPT, OfferAction.REJECT):
        with pytest.raises(PermissionDenied, match="veto-offer"):
            await offer_service.respond(
                s.pool, league_id=s.league_id, offer_id=offer.id, action=action,
                actor_user_id=_COMMISSIONER,
            )
    with pytest.raises(PermissionDenied):
        await offer_service.counter(
            s.pool, league_id=s.league_id, offer_id=offer.id, actor_user_id=_COMMISSIONER,
            players_from_to=(s.players["H2"][1],), players_to_from=(s.players["H1"][1],),
            picks_from_to=(), picks_to_from=(),
        )
    assert await _status(s, offer.id) is TradeStatus.PROPOSED


async def test_12_a_commissioner_may_read_and_veto(s):
    offer = await _h1_to_h2(s)
    listed = await offer_service.list_offers(
        s.pool, league_id=s.league_id, team_id=s.team["H2"], actor_user_id=_COMMISSIONER
    )
    assert [o.id for o in listed.received] == [offer.id]
    result = await offer_service.veto(
        s.pool, league_id=s.league_id, offer_id=offer.id, actor_user_id=_COMMISSIONER
    )
    assert result.offer.status is TradeStatus.VETOED
    assert result.offer.resolved_by == _COMMISSIONER


async def test_12_only_the_commissioner_may_veto_and_only_a_manager_may_answer(s):
    offer = await _h1_to_h2(s)
    with pytest.raises(PermissionDenied):
        await offer_service.veto(
            s.pool, league_id=s.league_id, offer_id=offer.id, actor_user_id=_H2
        )
    with pytest.raises(PermissionDenied):
        await _accept(s, offer.id, by=_OUTSIDER)
    with pytest.raises(PermissionDenied):
        await _accept(s, offer.id, by=_H1)  # the PROPOSER is not the receiving manager
    assert await _status(s, offer.id) is TradeStatus.PROPOSED


async def test_12_nobody_may_propose_for_an_unmanaged_team(s):
    with pytest.raises(PermissionDenied):
        await _propose(s, "AI", "H1", give=[s.players["AI"][0]], get=[s.players["H1"][0]],
                       by=_COMMISSIONER)


async def _offer_count(s: _S) -> int:
    return await s.pool.fetchval(
        "SELECT count(*) FROM trade_offers WHERE league_id = $1", s.league_id
    )


async def test_12_a_manager_removed_while_propose_waits_for_its_locks_is_denied(
    s, monkeypatch
):
    # The early authorize-before-lock check passes (H1 is the manager); then
    # the commissioner removes H1 -- through the real service path, committed
    # -- in the window before propose's tier-1 lock. The AI would accept, so a
    # propose that trusted its stale early check would move players.
    _stub_ai(monkeypatch, verdict="accept")
    real_check_claim = offer_service._check_claim
    removed = False

    async def check_claim_then_remove_the_manager(conn, **kw):
        nonlocal removed
        result = await real_check_claim(conn, **kw)
        if not removed:
            removed = True
            # Its own task: `transaction()`'s re-entrancy guard is task-scoped
            # and this call runs inside propose's open transaction.
            await asyncio.create_task(
                league_service.remove_manager(
                    s.pool, league_id=s.league_id, team_id=s.team["H1"],
                    actor_user_id=_COMMISSIONER,
                )
            )
        return result

    monkeypatch.setattr(offer_service, "_check_claim", check_claim_then_remove_the_manager)
    with pytest.raises(PermissionDenied):
        await _propose(s, "H1", "AI", give=[s.players["H1"][0]], get=[s.players["AI"][0]],
                       by=_H1)
    assert removed
    assert await _offer_count(s) == 0
    assert await _txns(s) == []
    assert await _team_of(s, s.players["H1"][0]) == s.team["H1"]


async def test_permission_is_checked_before_the_offer_is_read_so_state_does_not_leak(s):
    """An unauthorized caller gets PermissionDenied for a real offer and
    NotFoundError-shaped silence for nothing else: they learn neither
    whether an offer exists nor what state it is in."""
    offer = await _h1_to_h2(s)
    await offer_repo.resolve_offer_cas(
        s.pool, offer.id, expected_status=TradeStatus.PROPOSED,
        new_status=TradeStatus.REJECTED, resolved_by=_H2,
    )
    with pytest.raises(PermissionDenied):
        await _accept(s, offer.id, by=_OUTSIDER)


async def test_one_message_covers_a_missing_offer_and_one_in_another_league(s):
    other = await _scenario(s.pool)
    try:
        foreign = await _offer(
            other, "H1", "H2", give=(other.players["H1"][0],), get=(other.players["H2"][0],),
            proposed_by=_H1,
        )
        with pytest.raises(NotFoundError) as wrong_league:
            await _accept(s, foreign.id)
        with pytest.raises(NotFoundError) as missing:
            await _accept(s, 987_654_321)
        assert str(wrong_league.value) == str(missing.value)
        assert await _status(other, foreign.id) is TradeStatus.PROPOSED
    finally:
        await s.pool.execute("DELETE FROM leagues WHERE id = $1", other.league_id)


# ---------------------------------------------------------------------------
# §7.4 test 13 -- accept-time INJURED
# ---------------------------------------------------------------------------


async def test_13_a_player_injured_after_the_offer_expires_it_naming_the_player(s):
    offer = await _h1_to_h2(s)
    await player_repo.set_team_and_status(
        s.pool, s.players["H2"][0], s.team["H2"], RosterStatus.INJURED
    )
    result = await _accept(s, offer.id)
    assert result.offer.status is TradeStatus.EXPIRED
    assert "Marcus H20" in result.detail and "INJURED" in result.detail
    assert await _txns(s) == []

    again = await _accept(s, offer.id)  # the second attempt must not leave it PROPOSED either
    assert again.changed is False
    assert again.offer.status is TradeStatus.EXPIRED
    assert await _status(s, offer.id) is TradeStatus.EXPIRED


async def test_proposing_an_injured_or_two_way_player_is_refused_naming_them(s):
    await player_repo.set_team_and_status(
        s.pool, s.players["H1"][0], s.team["H1"], RosterStatus.TWO_WAY
    )
    with pytest.raises(ValidationError, match="Marcus H10.*TWO_WAY"):
        await _propose(s, "H1", "H2", give=[s.players["H1"][0]], get=[s.players["H2"][0]],
                       by=_H1)
    assert await offer_repo.list_trade_offers_for_league(s.pool, s.league_id) == []


async def test_proposing_the_partners_player_as_if_it_were_yours_is_refused(s):
    with pytest.raises(ValidationError, match="not on H1's active roster"):
        await _propose(s, "H1", "H2", give=[s.players["H2"][0]], get=[s.players["H2"][1]],
                       by=_H1)


async def test_proposing_a_pick_you_do_not_own_is_refused(s):
    with pytest.raises(ValidationError, match=f"Pick {s.picks['H2']} is not currently owned"):
        await _propose(s, "H1", "H2", give=[s.players["H1"][0]], get=[s.players["H2"][0]],
                       by=_H1, give_picks=[s.picks["H2"]])


async def test_a_player_from_another_league_is_refused_by_the_lock_helper(s):
    other = await _scenario(s.pool)
    try:
        with pytest.raises(ValidationError, match="belongs to league"):
            await _propose(s, "H1", "H2", give=[s.players["H1"][0]],
                           get=[other.players["H2"][0]], by=_H1)
    finally:
        await s.pool.execute("DELETE FROM leagues WHERE id = $1", other.league_id)


# ---------------------------------------------------------------------------
# §7.4 test 15 -- veto contends and fails fast
# ---------------------------------------------------------------------------


async def test_15_a_veto_behind_a_held_offer_row_fails_fast_with_concurrency_error(
    s, monkeypatch
):
    monkeypatch.setitem(roster_lock._GATE_TIMEOUT_S, LockMode.SHARED, "300ms")
    offer = await _h1_to_h2(s)
    locked, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with transaction(s.pool) as conn:
            await offer_repo.get_trade_offer_for_update(conn, offer.id)
            locked.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await locked.wait()
    try:
        with pytest.raises(ConcurrencyError):
            await asyncio.wait_for(
                offer_service.veto(
                    s.pool, league_id=s.league_id, offer_id=offer.id,
                    actor_user_id=_COMMISSIONER,
                ),
                timeout=5,
            )
    finally:
        release.set()
        await task
    assert await _status(s, offer.id) is TradeStatus.PROPOSED


async def test_15_twin_a_gate_less_veto_hangs_behind_the_held_row(s, monkeypatch):
    """# twin: `open_league_gate` neutralised to a stub that issues no `SET
    LOCAL lock_timeout` -- round 2's gate-less veto. It then waits on the
    server default (forever) instead of failing in `lock_timeout`."""

    async def no_timeout_gate(conn, *, league_id, mode):
        league = await league_repo.get_league(conn, league_id)
        return roster_lock.LeagueGate(league_id, league.current_phase, league.current_season, mode)

    offer = await _h1_to_h2(s)
    monkeypatch.setattr(roster_lock, "open_league_gate", no_timeout_gate)
    locked, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with transaction(s.pool) as conn:
            await offer_repo.get_trade_offer_for_update(conn, offer.id)
            locked.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await locked.wait()
    veto = asyncio.create_task(
        offer_service.veto(
            s.pool, league_id=s.league_id, offer_id=offer.id, actor_user_id=_COMMISSIONER
        )
    )
    try:
        done, _ = await asyncio.wait({veto}, timeout=1.5)
        assert not done, "the gate-less veto returned/raised instead of hanging"
    finally:
        release.set()
        await task
        await veto  # it completes once the row is released


# ---------------------------------------------------------------------------
# veto, respond(reject), counter
# ---------------------------------------------------------------------------


async def test_veto_already_answered_reports_the_state_and_writes_nothing(s):
    offer = await _h1_to_h2(s)
    await _accept(s, offer.id)
    result = await offer_service.veto(
        s.pool, league_id=s.league_id, offer_id=offer.id, actor_user_id=_COMMISSIONER
    )
    assert result.changed is False
    assert result.offer.status is TradeStatus.ACCEPTED


async def test_veto_of_an_offer_in_another_league_is_not_found(s):
    other = await _scenario(s.pool)
    try:
        foreign = await _offer(
            other, "H1", "H2", give=(other.players["H1"][0],), get=(other.players["H2"][0],),
            proposed_by=_H1,
        )
        with pytest.raises(NotFoundError):
            await offer_service.veto(
                s.pool, league_id=s.league_id, offer_id=foreign.id, actor_user_id=_COMMISSIONER
            )
        assert await _status(other, foreign.id) is TradeStatus.PROPOSED
    finally:
        await s.pool.execute("DELETE FROM leagues WHERE id = $1", other.league_id)


async def test_veto_works_in_every_phase_including_a_closed_one(s):
    offer = await _h1_to_h2(s)
    await league_repo.set_current_phase(s.pool, s.league_id, Phase.PLAYOFFS_R1)
    result = await offer_service.veto(
        s.pool, league_id=s.league_id, offer_id=offer.id, actor_user_id=_COMMISSIONER
    )
    assert result.offer.status is TradeStatus.VETOED


async def test_veto_never_takes_a_team_or_player_lock(s):
    offer = await _h1_to_h2(s)

    async def lock_every_team_and_player(conn):
        await conn.fetch("SELECT id FROM teams WHERE league_id = $1 FOR NO KEY UPDATE", s.league_id)
        await conn.fetch(
            "SELECT id FROM players WHERE league_id = $1 FOR NO KEY UPDATE", s.league_id
        )

    # Someone else holds every team and player row; a veto still goes straight
    # through because it acquires none of them.
    async with _held(s.pool, lock_every_team_and_player):
        result = await asyncio.wait_for(
            offer_service.veto(
                s.pool, league_id=s.league_id, offer_id=offer.id, actor_user_id=_COMMISSIONER
            ),
            timeout=5,
        )
    assert result.offer.status is TradeStatus.VETOED


async def test_reject_writes_rejected_with_the_actor_and_no_trade(s):
    offer = await _h1_to_h2(s)
    result = await offer_service.respond(
        s.pool, league_id=s.league_id, offer_id=offer.id, action=OfferAction.REJECT,
        actor_user_id=_H2,
    )
    assert result.offer.status is TradeStatus.REJECTED
    assert result.offer.resolved_by == _H2 and result.offer.resolved_at is not None
    assert await _txns(s) == []


async def test_a_second_answer_reports_the_current_state_and_writes_nothing(s):
    offer = await _h1_to_h2(s)
    await offer_service.respond(
        s.pool, league_id=s.league_id, offer_id=offer.id, action=OfferAction.REJECT,
        actor_user_id=_H2,
    )
    again = await _accept(s, offer.id)
    assert again.changed is False and again.offer.status is TradeStatus.REJECTED
    assert await _txns(s) == []


async def test_counter_resolves_the_original_and_inserts_a_linked_new_row(s):
    original = await _h1_to_h2(s)
    result = await offer_service.counter(
        s.pool, league_id=s.league_id, offer_id=original.id, actor_user_id=_H2,
        players_from_to=(s.players["H2"][1],), players_to_from=(s.players["H1"][1],),
        picks_from_to=(), picks_to_from=(),
    )
    new = result.offer
    assert new.status is TradeStatus.PROPOSED
    assert new.counters_offer_id == original.id
    assert (new.from_team_id, new.to_team_id) == (s.team["H2"], s.team["H1"])
    assert new.proposed_by == _H2
    reread = await offer_repo.get_trade_offer(s.pool, original.id)
    assert reread.status is TradeStatus.COUNTERED and reread.resolved_by == _H2


async def test_counter_of_an_ai_offer_is_answered_by_the_ai_in_the_same_transaction(
    s, monkeypatch
):
    _stub_ai(monkeypatch, verdict="accept")
    original = await _ai_offer(s)
    result = await offer_service.counter(
        s.pool, league_id=s.league_id, offer_id=original.id, actor_user_id=_H1,
        players_from_to=(s.players["H1"][1],), players_to_from=(s.players["AI"][1],),
        picks_from_to=(), picks_to_from=(),
    )
    assert result.offer.status is TradeStatus.ACCEPTED
    assert result.offer.counters_offer_id == original.id
    (txn,) = await _txns(s)
    assert txn.source_offer_id == result.offer.id
    assert (await offer_repo.get_trade_offer(s.pool, original.id)).status is TradeStatus.COUNTERED


async def test_a_counter_at_the_chain_bound_is_refused_and_resolves_nothing(s):
    root = await _offer(
        s, "H1", "H2", give=(s.players["H1"][0],), get=(s.players["H2"][0],), proposed_by=_H1,
        status=TradeStatus.COUNTERED,
    )
    c1 = await _offer(
        s, "H2", "H1", give=(s.players["H2"][1],), get=(s.players["H1"][1],), proposed_by=_H2,
        status=TradeStatus.COUNTERED, counters_offer_id=root.id,
    )
    c2 = await _offer(
        s, "H1", "H2", give=(s.players["H1"][2],), get=(s.players["H2"][2],), proposed_by=_H1,
        status=TradeStatus.COUNTERED, counters_offer_id=c1.id,
    )
    c3 = await _offer(
        s, "H2", "H1", give=(s.players["H2"][3],), get=(s.players["H1"][3],), proposed_by=_H2,
        counters_offer_id=c2.id,
    )
    with pytest.raises(ValidationError, match="countered as many times"):
        await offer_service.counter(
            s.pool, league_id=s.league_id, offer_id=c3.id, actor_user_id=_H1,
            players_from_to=(s.players["H1"][0],), players_to_from=(s.players["H2"][0],),
            picks_from_to=(), picks_to_from=(),
        )
    assert await _status(s, c3.id) is TradeStatus.PROPOSED


async def test_a_counter_with_an_illegal_package_raises_and_leaves_the_original_proposed(s):
    original = await _h1_to_h2(s)
    with pytest.raises(ValidationError, match="not currently owned"):
        await offer_service.counter(
            s.pool, league_id=s.league_id, offer_id=original.id, actor_user_id=_H2,
            players_from_to=(s.players["H2"][1],), players_to_from=(s.players["H1"][1],),
            picks_from_to=(s.picks["H1"],), picks_to_from=(),  # H2 does not own H1's pick
        )
    assert await _status(s, original.id) is TradeStatus.PROPOSED


# ---------------------------------------------------------------------------
# GM-3b/GM-3b.1: auto-pilot refuses propose and counter, never respond
# (docs/design/gm-command-surface.md)
# ---------------------------------------------------------------------------


async def _set_auto_pilot(s: _S, code: str) -> None:
    await team_repo.set_auto_pilot(
        s.pool, s.team[code], auto_pilot=True, gm_philosophy=GmPhilosophy.EGALITARIAN
    )


async def test_propose_by_an_auto_piloted_teams_own_manager_is_refused_before_validation(s):
    """Mutation 3d: asserted at the prelude, before `_validate_package_
    ids` -- a test that only checked "no offer row was written" would
    also pass against a refusal wrongly sited inside the transaction, so
    a DELIBERATELY illegal package (H1 doesn't own this pick) still
    produces the auto-pilot message, not the package-validation one."""
    await _set_auto_pilot(s, "H1")
    with pytest.raises(PermissionDenied, match="on auto-pilot"):
        await offer_service.propose(
            s.pool, league_id=s.league_id, from_team_id=s.team["H1"], to_team_id=s.team["H2"],
            players_from_to=(s.players["H1"][0],), players_to_from=(s.players["H2"][0],),
            picks_from_to=(s.picks["H2"],), picks_to_from=(),  # illegal: H1 doesn't own this
            actor_user_id=_H1,
        )
    assert await offer_repo.list_trade_offers_for_league(s.pool, s.league_id) == []


async def test_counter_by_an_auto_piloted_teams_own_manager_is_refused(s):
    """Mutation 3e: three assertions, because GM-3b.1's line runs between
    them -- (a) refused, (b) the original is still PROPOSED (not
    COUNTERED, proving the refusal lands before `_write_terminal`), and
    (c) the same team can still accept/reject through `/offer-respond`,
    proving the refusal did not leak into the shared `_lock_offer_for_
    answer` helper `respond` also calls."""
    original = await _h1_to_h2(s)
    await _set_auto_pilot(s, "H2")

    with pytest.raises(PermissionDenied, match="on auto-pilot"):
        await offer_service.counter(
            s.pool, league_id=s.league_id, offer_id=original.id, actor_user_id=_H2,
            players_from_to=(s.players["H2"][1],), players_to_from=(s.players["H1"][1],),
            picks_from_to=(), picks_to_from=(),
        )
    assert await _status(s, original.id) is TradeStatus.PROPOSED

    result = await _accept(s, original.id)
    assert result.offer.status is TradeStatus.ACCEPTED


async def test_counter_by_an_auto_piloted_teams_own_manager_can_still_reject(s):
    original = await _h1_to_h2(s)
    await _set_auto_pilot(s, "H2")
    with pytest.raises(PermissionDenied, match="on auto-pilot"):
        await offer_service.counter(
            s.pool, league_id=s.league_id, offer_id=original.id, actor_user_id=_H2,
            players_from_to=(s.players["H2"][1],), players_to_from=(s.players["H1"][1],),
            picks_from_to=(), picks_to_from=(),
        )
    result = await offer_service.respond(
        s.pool, league_id=s.league_id, offer_id=original.id, action=OfferAction.REJECT,
        actor_user_id=_H2,
    )
    assert result.offer.status is TradeStatus.REJECTED


async def test_propose_by_a_non_auto_piloted_manager_still_succeeds(s):
    """Control: the refusal is conditional on auto_pilot, not a blanket
    regression on every propose call."""
    result = await _propose(
        s, "H1", "H2", give=(s.players["H1"][0],), get=(s.players["H2"][0],), by=_H1,
    )
    assert result.offer.status is TradeStatus.PROPOSED


# ---------------------------------------------------------------------------
# legality and apply_trade failures resolve to EXPIRED, never leave PROPOSED
# ---------------------------------------------------------------------------


async def _extra_player(s: _S, code: str, n: int):
    return await _make_player(s.pool, s.league_id, s.team[code], last_name=f"{code}X{n}")


async def test_accept_time_legality_failure_expires_with_the_illegal_token(s):
    offer = await _h1_to_h2(s)
    # H2 gains two bodies after the offer was made: roster_max is 6, so a
    # 1-for-1 that was legal at 4 players is illegal at 6+... push it over.
    await _extra_player(s, "H2", 1)
    await _extra_player(s, "H2", 2)
    await _extra_player(s, "H2", 3)
    result = await _accept(s, offer.id)
    assert result.offer.status is TradeStatus.EXPIRED
    assert result.offer.illegal_token == "illegal:roster_max"
    assert "H2" in result.detail
    assert await _txns(s) == []


async def test_propose_time_legality_failure_raises_and_writes_no_row(s):
    await _extra_player(s, "H2", 1)
    await _extra_player(s, "H2", 2)
    with pytest.raises(ValidationError, match="illegal|not legal"):
        # H2 is at 6/6; a 1-for-2 that adds a body to H2 is illegal for H2.
        await _propose(
            s, "H1", "H2", give=[s.players["H1"][0], s.players["H1"][1]],
            get=[s.players["H2"][0]], by=_H1,
        )
    assert await offer_repo.list_trade_offers_for_league(s.pool, s.league_id) == []


async def test_an_apply_trade_revalidation_failure_persists_expired_with_its_message(s):
    """§5.4: `apply_trade` raises `ValidationError` from its own rule 3
    (the traded player has no active contract). It runs in a savepoint, so the
    outer transaction survives to write the EXPIRED row that records why."""
    offer = await _h1_to_h2(s)
    (contract,) = await contract_repo.list_contracts_for_player(s.pool, s.players["H1"][0])
    await contract_repo.terminate_contract(
        s.pool, contract.id, status=ContractStatus.WAIVED, terminated_in_season=_SEASON
    )
    result = await _accept(s, offer.id)
    assert result.offer.status is TradeStatus.EXPIRED
    assert "no active contract" in result.detail
    assert await _txns(s) == []
    assert await _team_of(s, s.players["H2"][0]) == s.team["H2"]  # nothing half-applied


# ---------------------------------------------------------------------------
# legality parity (§12 step 10)
# ---------------------------------------------------------------------------


async def test_the_offer_paths_legality_call_has_the_same_argument_shape_as_the_ais(
    s, monkeypatch
):
    """Both gates must call `_check_legality` with the same basis: pre-trade
    roster off the snapshot, the SAME incoming/outgoing bundles, `ctx.next_
    season`, its cap settings and that team's `room_by_team`. A human-specific
    basis would be stricter (AI offers unacceptable by construction) or looser
    (the D4-1 hole). The AI's gate is exercised through the real
    `_offer_candidate_clears_gates`."""
    real = trade_decisions._check_legality
    offer_calls: list[tuple] = []
    ai_calls: list[tuple] = []
    sink = offer_calls

    def recording(team_snapshot, incoming, outgoing, next_season, settings, room):
        sink.append((
            team_snapshot.id, tuple(sorted(p.id for p in incoming.players)),
            tuple(sorted(p.id for p in incoming.picks)),
            tuple(sorted(p.id for p in outgoing.players)),
            tuple(sorted(p.id for p in outgoing.picks)),
            len(team_snapshot.roster), next_season, settings, room,
        ))
        return real(team_snapshot, incoming, outgoing, next_season, settings, room)

    monkeypatch.setattr(trade_decisions, "_check_legality", recording)
    give, get = s.players["H1"][0], s.players["H2"][0]
    await _propose(s, "H1", "H2", give=[give], get=[get], by=_H1, give_picks=[s.picks["H1"]])

    snapshot, ctx = await gm_service.load_gm_inputs(s.pool, league_id=s.league_id, season=_SEASON)
    a, b = snapshot.team_by_id(s.team["H1"]), snapshot.team_by_id(s.team["H2"])
    pick = next(p for p in a.draft_picks if p.id == s.picks["H1"])
    pending = gm_service.PendingOffer(
        from_team_id=a.id, to_team_id=b.id,
        package=gm_service.CandidatePackage(
            team_a_id=a.id, team_b_id=b.id,
            incoming_a=gm_service.AssetBundle(players=(b.player_by_id(get),)),
            outgoing_a=gm_service.AssetBundle(players=(a.player_by_id(give),), picks=(pick,)),
        ),
        record=DecisionRecord(
            team_id=a.id, philosophy=GmPhilosophy.EGALITARIAN,
            window=ctx.window_by_team[a.id], subject="parity", verdict="accept",
            score=1.0, threshold=0.0, terms=(),
        ),
    )
    sink = ai_calls
    assert gm_service._offer_candidate_clears_gates(
        pending, snapshot=snapshot, next_season=ctx.next_season,
        next_season_cap_settings=ctx.next_season_cap_settings,
        roster_by_team={t.id: t.roster for t in snapshot.teams},
        room_remaining=ctx.room_by_team, consumed_players=(), consumed_picks=(),
    )

    assert len(offer_calls) == 2 and len(ai_calls) == 2
    assert sorted(offer_calls, key=lambda c: c[0]) == sorted(ai_calls, key=lambda c: c[0])


# ---------------------------------------------------------------------------
# The authority-check coupling: `_lock_offer_for_answer` reads the recipient's
# manager UNLOCKED (steps 3b/4), and that is safe only because
# `league_service.remove_manager`/`assign_manager` expire the team's PROPOSED
# offers in the SAME transaction as `set_manager`, which step 6's
# `FOR UPDATE` re-read then observes. These two tests pin that coupling.
# ---------------------------------------------------------------------------


async def _answer_as_h2(s: _S, kind: str, offer_id: int):
    if kind == "respond":
        return await _accept(s, offer_id, by=_H2)
    return await offer_service.counter(
        s.pool, league_id=s.league_id, offer_id=offer_id, actor_user_id=_H2,
        players_from_to=(s.players["H2"][1],), players_to_from=(s.players["H1"][1],),
        picks_from_to=(), picks_to_from=(),
    )


async def _remove_h2_manager_while_h2_answers(s: _S, monkeypatch, kind: str, *, keep_expire: bool):
    """Connection 1 runs the real `remove_manager` and is held right after its
    `set_manager` write, before its offer-expiry step and before commit.
    Connection 2 answers as the manager being removed (its unlocked manager
    read still sees H2), must queue behind conn 1's team-row lock, and is only
    released when conn 1 commits. The barrier sits on `set_manager`, not on the
    expiry, so it fires identically when `keep_expire=False` drops the expiry
    write (the mutant)."""
    offer = await _h1_to_h2(s)
    real_set_manager = team_repo.set_manager
    held, release = asyncio.Event(), asyncio.Event()

    async def set_manager_then_hold(ex, team_id, user_id):
        await real_set_manager(ex, team_id, user_id)
        held.set()
        await release.wait()

    monkeypatch.setattr(team_repo, "set_manager", set_manager_then_hold)
    if not keep_expire:
        async def dropped(ex, team_id):
            return None

        monkeypatch.setattr(offer_repo, "expire_offers_for_team", dropped)
    remover = asyncio.create_task(
        league_service.remove_manager(
            s.pool, league_id=s.league_id, team_id=s.team["H2"], actor_user_id=_COMMISSIONER
        )
    )
    await asyncio.wait_for(held.wait(), timeout=5)
    answer = asyncio.create_task(_answer_as_h2(s, kind, offer.id))
    try:
        done, _ = await asyncio.wait({answer}, timeout=0.7)
        assert not done, "the answer did not queue behind the uncommitted manager removal"
    finally:
        release.set()
        await remover
    return offer, answer


@pytest.mark.parametrize("kind", ["respond", "counter"])
async def test_an_answer_racing_a_manager_removal_finds_the_offer_expired(s, monkeypatch, kind):
    offer, answer = await _remove_h2_manager_while_h2_answers(
        s, monkeypatch, kind, keep_expire=True
    )
    result = await answer
    assert result.changed is False
    assert result.offer.id == offer.id
    assert result.offer.status is TradeStatus.EXPIRED
    assert "already expired" in result.detail
    assert await _txns(s) == []
    assert await _status(s, offer.id) is TradeStatus.EXPIRED
    assert (await offer_repo.get_trade_offer(s.pool, offer.id)).resolved_by is None
    assert len(await offer_repo.list_trade_offers_for_league(s.pool, s.league_id)) == 1
    assert (await team_repo.get_team(s.pool, s.team["H2"])).manager_user_id is None


@pytest.mark.parametrize("kind", ["respond", "counter"])
async def test_an_answer_racing_a_manager_removal_twin_without_the_expiry_acts_as_a_removed_manager(
    s, monkeypatch, kind
):
    """# twin: `expire_offers_for_team` dropped from `remove_manager` (the
    barrier kept, so the timing is unchanged). The unlocked manager read is
    then the only authority check, it is stale, and the removed manager's
    answer takes effect -- the defect the same-transaction expiry prevents."""
    offer, answer = await _remove_h2_manager_while_h2_answers(
        s, monkeypatch, kind, keep_expire=False
    )
    result = await answer
    assert result.changed is True  # the removed manager's answer was applied
    assert (await team_repo.get_team(s.pool, s.team["H2"])).manager_user_id is None
    if kind == "respond":
        assert result.offer.status is TradeStatus.ACCEPTED
        assert len(await _txns(s)) == 1
    else:
        assert (await offer_repo.get_trade_offer(s.pool, offer.id)).status is TradeStatus.COUNTERED
