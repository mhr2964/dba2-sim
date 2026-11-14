"""`offer_repo` against real Postgres -- migration 018's six columns and the
repository functions the trade-offer review surface reads and writes them
through (docs/design/phase-5-trade-offer-review-surface.md §2, §12 steps 1-2).

The service-level behaviour built on these functions is proven in
`test_offer_service.py`; this file pins the storage contract itself: what a
fresh row looks like, what each writer stamps, and what each reader returns.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""
from __future__ import annotations

import random

import pytest

from dba2.core.errors import NotFoundError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, offer_repo, team_repo
from dba2.domain.phase import Phase, TradeStatus


@pytest.fixture
async def league():
    """(pool, league_id, team_a_id, team_b_id, team_c_id) -- three teams, so
    a both-sides read can tell "received", "sent" and "neither" apart."""
    pool = await get_pool()
    created = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Offer Repo Test League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
        current_phase=Phase.TRADE_DEADLINE,
    )
    teams = []
    for code in ("OA", "OB", "OC"):
        team = await team_repo.create_team(
            pool, league_id=created.id, code=code, name=f"{code} Team", city=code,
            conference="East", division="Atlantic", home_market_size=3,
        )
        teams.append(team.id)
    try:
        yield pool, created.id, *teams
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", created.id)
        await close_pool()


async def _make(pool, league_id, from_id, to_id, *, phase=Phase.TRADE_DEADLINE, season=2026,
                proposed_by=None, counters_offer_id=None):
    return await offer_repo.create_trade_offer(
        pool,
        league_id=league_id,
        season=season,
        phase=phase,
        from_team_id=from_id,
        to_team_id=to_id,
        proposed_by=proposed_by,
        status=TradeStatus.PROPOSED,
        players_from_to=(1,),
        players_to_from=(2,),
        picks_from_to=(),
        picks_to_from=(),
        counters_offer_id=counters_offer_id,
    )


async def test_a_fresh_offer_is_unresolved_and_undelivered(league):
    pool, league_id, a, b, _c = league
    offer = await _make(pool, league_id, a, b)
    assert offer.counters_offer_id is None
    assert offer.resolved_at is None and offer.resolved_by is None
    assert offer.illegal_token is None
    assert offer.channel_id is None and offer.message_id is None


async def test_counters_offer_id_round_trips_and_is_nulled_when_the_original_is_deleted(league):
    pool, league_id, a, b, _c = league
    original = await _make(pool, league_id, a, b)
    counter = await _make(pool, league_id, b, a, proposed_by=7, counters_offer_id=original.id)
    assert (await offer_repo.get_trade_offer(pool, counter.id)).counters_offer_id == original.id

    await pool.execute("DELETE FROM trade_offers WHERE id = $1", original.id)
    survivor = await offer_repo.get_trade_offer(pool, counter.id)
    assert survivor is not None and survivor.counters_offer_id is None  # SET NULL, not CASCADE


async def test_expire_offers_for_team_stamps_resolved_at_and_leaves_resolved_by_null(league):
    pool, league_id, a, b, c = league
    sent = await _make(pool, league_id, a, b)
    received = await _make(pool, league_id, c, a)
    unrelated = await _make(pool, league_id, b, c)

    await offer_repo.expire_offers_for_team(pool, a)

    for offer_id in (sent.id, received.id):
        row = await offer_repo.get_trade_offer(pool, offer_id)
        assert row.status is TradeStatus.EXPIRED
        assert row.resolved_at is not None
        assert row.resolved_by is None  # a rule resolved it; nobody clicked
    untouched = await offer_repo.get_trade_offer(pool, unrelated.id)
    assert untouched.status is TradeStatus.PROPOSED and untouched.resolved_at is None


async def test_get_trade_offer_returns_none_for_a_missing_id(league):
    pool, *_ = league
    assert await offer_repo.get_trade_offer(pool, 987_654_321) is None


async def test_list_offers_for_team_both_sides_filters_by_side_and_status(league):
    pool, league_id, a, b, c = league
    received = await _make(pool, league_id, b, a)
    sent = await _make(pool, league_id, a, c)
    closed = await _make(pool, league_id, c, a)
    await _make(pool, league_id, b, c)  # neither side is `a`
    await offer_repo.resolve_offer_cas(
        pool, closed.id, expected_status=TradeStatus.PROPOSED,
        new_status=TradeStatus.REJECTED, resolved_by=9,
    )

    everything = await offer_repo.list_offers_for_team_both_sides(
        pool, league_id=league_id, team_id=a
    )
    assert [o.id for o in everything] == [received.id, sent.id, closed.id]

    proposed = await offer_repo.list_offers_for_team_both_sides(
        pool, league_id=league_id, team_id=a, status=TradeStatus.PROPOSED
    )
    assert [o.id for o in proposed] == [received.id, sent.id]


async def test_resolve_offer_cas_stamps_every_field_and_reports_one_row(league):
    pool, league_id, a, b, _c = league
    offer = await _make(pool, league_id, a, b)
    affected = await offer_repo.resolve_offer_cas(
        pool, offer.id, expected_status=TradeStatus.PROPOSED, new_status=TradeStatus.EXPIRED,
        resolved_by=None, illegal_token="illegal:roster_max",
    )
    assert affected == 1
    row = await offer_repo.get_trade_offer(pool, offer.id)
    assert row.status is TradeStatus.EXPIRED
    assert row.illegal_token == "illegal:roster_max"
    assert row.resolved_at is not None and row.resolved_by is None


async def test_resolve_offer_cas_against_the_wrong_expected_status_writes_nothing(league):
    pool, league_id, a, b, _c = league
    offer = await _make(pool, league_id, a, b)
    first = await offer_repo.resolve_offer_cas(
        pool, offer.id, expected_status=TradeStatus.PROPOSED, new_status=TradeStatus.REJECTED,
        resolved_by=5,
    )
    second = await offer_repo.resolve_offer_cas(
        pool, offer.id, expected_status=TradeStatus.PROPOSED, new_status=TradeStatus.ACCEPTED,
        resolved_by=6,
    )
    assert (first, second) == (1, 0)
    row = await offer_repo.get_trade_offer(pool, offer.id)
    assert row.status is TradeStatus.REJECTED and row.resolved_by == 5


async def test_set_offer_message_writes_both_ids_and_overwrites(league):
    pool, league_id, a, b, _c = league
    offer = await _make(pool, league_id, a, b)
    await offer_repo.set_offer_message(pool, offer.id, channel_id=11, message_id=22)
    await offer_repo.set_offer_message(pool, offer.id, channel_id=11, message_id=33)
    row = await offer_repo.get_trade_offer(pool, offer.id)
    assert (row.channel_id, row.message_id) == (11, 33)


async def test_list_undelivered_offers_is_the_proposed_null_message_queue(league):
    pool, league_id, a, b, c = league
    first = await _make(pool, league_id, a, b)
    delivered = await _make(pool, league_id, a, c)
    resolved = await _make(pool, league_id, b, c)
    await offer_repo.set_offer_message(pool, delivered.id, channel_id=1, message_id=2)
    await offer_repo.resolve_offer_cas(
        pool, resolved.id, expected_status=TradeStatus.PROPOSED, new_status=TradeStatus.VETOED,
        resolved_by=1,
    )
    queue = await offer_repo.list_undelivered_offers(pool, league_id)
    assert [o.id for o in queue] == [first.id]


async def test_the_sweep_expires_only_offers_from_another_season_or_phase(league):
    pool, league_id, a, b, c = league
    current = await _make(pool, league_id, a, b)  # (2026, TRADE_DEADLINE): the league's own
    other_phase = await _make(pool, league_id, a, c, phase=Phase.REGULAR_SEASON)
    other_season = await _make(pool, league_id, b, c, season=2025)
    resolved = await _make(pool, league_id, b, a, phase=Phase.REGULAR_SEASON)
    await offer_repo.resolve_offer_cas(
        pool, resolved.id, expected_status=TradeStatus.PROPOSED, new_status=TradeStatus.REJECTED,
        resolved_by=3,
    )

    assert await offer_repo.expire_stale_offers_for_league(pool, league_id) == 2

    assert (await offer_repo.get_trade_offer(pool, current.id)).status is TradeStatus.PROPOSED
    for stale in (other_phase, other_season):
        row = await offer_repo.get_trade_offer(pool, stale.id)
        assert row.status is TradeStatus.EXPIRED
        assert row.resolved_at is not None and row.resolved_by is None
    # An already-resolved row is never re-touched (no outgoing edge).
    assert (await offer_repo.get_trade_offer(pool, resolved.id)).status is TradeStatus.REJECTED
    assert await offer_repo.expire_stale_offers_for_league(pool, league_id) == 0


async def test_the_sweep_of_an_unknown_league_raises(league):
    pool, *_ = league
    with pytest.raises(NotFoundError):
        await offer_repo.expire_stale_offers_for_league(pool, 987_654_321)
