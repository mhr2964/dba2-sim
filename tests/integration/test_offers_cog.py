"""Live-Postgres proof that the five offer commands work end to end through
the cog (design §4, §5.3 NIT 10, §6.4, §6.5): a fake bot/channel records what
would be posted and a fake `Interaction` what would be replied; the service,
the repositories and Postgres are real.

What the unit suite (`tests/unit/bot/test_offers_cog.py`) cannot show is
that the ids a command passes are the ones the database then acts on, that a
propose really lands in the receiving team's GM channel, and that `/offers`
runs its sweep BEFORE its drain.

Requires `docker compose up -d` and a `.env`/DATABASE_URL, like test_smoke.py.
"""
from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from dba2.bot.cogs import offers as cog_mod
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, offer_repo
from dba2.domain.phase import Phase, TradeStatus
from dba2.presentation.offers import EXPIRY_SUFFIX

from .test_offer_delivery import _FakeBot, _give_gm_channel
from .test_offer_service import (
    _COMMISSIONER,
    _H1,
    _H2,
    _h1_to_h2,
    _scenario,
    _status,
    _team_of,
    _txns,
)


@pytest.fixture
async def s():
    pool = await get_pool()
    scenario = await _scenario(pool)
    try:
        yield scenario
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", scenario.league_id)
        await close_pool()


class _Response:
    def __init__(self) -> None:
        self._done = False
        self.messages: list[str] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        assert ephemeral is True
        self._done = True

    async def send_message(self, content=None, *, ephemeral: bool = False, **_kw) -> None:
        assert ephemeral is True
        self._done = True
        self.messages.append(content)


class _Followup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, *, embed=None, ephemeral: bool = False, **_kw) -> None:
        assert ephemeral is True
        self.sent.append({"content": content, "embed": embed})


class _Interaction:
    def __init__(self, guild_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.id = random.randint(1, 10**9)
        self.user = SimpleNamespace(id=user_id)
        self.response = _Response()
        self.followup = _Followup()

    @property
    def text(self) -> str:
        return (self.response.messages + [m["content"] for m in self.followup.sent])[0]


async def _ix(s, user_id: int) -> _Interaction:
    league = await league_repo.get_league(s.pool, s.league_id)
    return _Interaction(league.discord_guild_id, user_id)


def _cog(bot: _FakeBot) -> cog_mod.OfferCommands:
    return cog_mod.OfferCommands(bot=bot)


async def test_propose_writes_the_row_and_posts_it_to_the_receivers_gm_channel(s):
    bot = _FakeBot()
    channel = await _give_gm_channel(s, bot, "H2")
    cog = _cog(bot)
    ix = await _ix(s, _H1)

    await cog.propose_trade.callback(
        cog, ix, "h1", "h2",  # codes are case-insensitive
        send_player_1=s.players["H1"][0], get_player_1=s.players["H2"][0],
        send_pick_1=s.picks["H1"],
    )

    offers = await offer_repo.list_trade_offers_for_league(s.pool, s.league_id)
    assert len(offers) == 1
    offer = offers[0]
    assert offer.status is TradeStatus.PROPOSED and offer.proposed_by == _H1
    assert offer.package["players_from_to"] == (s.players["H1"][0],)
    assert offer.package["players_to_from"] == (s.players["H2"][0],)
    assert offer.package["picks_from_to"] == (s.picks["H1"],)
    assert offer.message_id is not None and offer.channel_id == channel.id
    assert len(channel.sent) == 1
    assert ix.followup.sent[0]["content"] == (
        f"Offer #{offer.id} sent; it is waiting for H2's manager."
    )
    assert ix.followup.sent[0]["embed"] is not None


async def test_propose_by_someone_who_does_not_manage_the_team_writes_nothing(s):
    bot = _FakeBot()
    cog = _cog(bot)
    ix = await _ix(s, _H2)  # manages H2, not H1
    await cog.propose_trade.callback(
        cog, ix, "H1", "H2", send_player_1=s.players["H1"][0], get_player_1=s.players["H2"][0]
    )
    assert await offer_repo.list_trade_offers_for_league(s.pool, s.league_id) == []
    assert "manager" in ix.text.lower()


async def test_offers_lists_the_pending_offer_for_the_receiver_and_the_sender(s):
    bot = _FakeBot()
    await _give_gm_channel(s, bot, "H2")
    offer = await _h1_to_h2(s)
    cog = _cog(bot)

    received = await _ix(s, _H2)
    await cog.offers.callback(cog, received, "H2")
    embed = received.followup.sent[0]["embed"]
    assert [f.name for f in embed.fields] == [f"Received #{offer.id} from H1"]

    sent = await _ix(s, _H1)
    await cog.offers.callback(cog, sent, "H1")
    assert [f.name for f in sent.followup.sent[0]["embed"].fields] == [f"Sent #{offer.id} to H2"]


async def test_offers_sweeps_a_stale_offer_before_it_can_be_drained(s):
    """§4.3 order: sweep, reads, THEN drain. An offer made in an earlier phase
    that was never delivered must not be posted with live buttons -- it must be
    expired by the sweep and shown nowhere."""
    bot = _FakeBot()
    channel = await _give_gm_channel(s, bot, "H2")
    stale = await _h1_to_h2(s)
    await league_repo.set_current_phase(s.pool, s.league_id, Phase.POST_DEADLINE)
    cog = _cog(bot)

    ix = await _ix(s, _H2)
    await cog.offers.callback(cog, ix, "H2")

    assert await _status(s, stale.id) is TradeStatus.EXPIRED
    assert channel.sent == []
    assert ix.followup.sent[0]["embed"].description == "No pending trade offers."


async def test_offers_drain_delivers_an_offer_that_was_written_while_the_bot_was_away(s):
    bot = _FakeBot()
    channel = await _give_gm_channel(s, bot, "H2")
    offer = await _h1_to_h2(s)
    assert offer.message_id is None
    cog = _cog(bot)
    await cog.offers.callback(cog, await _ix(s, _H2), "H2")
    assert len(channel.sent) == 1
    assert (await offer_repo.get_trade_offer(s.pool, offer.id)).message_id is not None


async def test_offer_respond_accept_executes_the_trade_and_a_repeat_says_already_answered(s):
    bot = _FakeBot()
    cog = _cog(bot)
    offer = await _h1_to_h2(s)
    choice = SimpleNamespace(value="accept")

    first = await _ix(s, _H2)
    await cog.offer_respond.callback(cog, first, offer.id, choice)
    assert first.text == f"Offer #{offer.id} accepted -- the trade is done."
    assert await _team_of(s, s.players["H1"][0]) == s.team["H2"]

    again = await _ix(s, _H2)
    await cog.offer_respond.callback(cog, again, offer.id, choice)
    assert again.text == f"Offer #{offer.id} was already accepted -- nothing was changed."
    assert len(await _txns(s)) == 1


async def test_offer_respond_in_a_closed_phase_explains_that_the_offer_expired(s):
    """NIT 10 through the real service: the PhaseError about the command gets
    the sentence about the offer, and nothing is applied."""
    bot = _FakeBot()
    cog = _cog(bot)
    offer = await _h1_to_h2(s)
    await league_repo.set_current_phase(s.pool, s.league_id, Phase.POST_DEADLINE)
    ix = await _ix(s, _H2)
    await cog.offer_respond.callback(cog, ix, offer.id, SimpleNamespace(value="accept"))
    assert ix.text.endswith(EXPIRY_SUFFIX)
    assert await _status(s, offer.id) is TradeStatus.PROPOSED
    assert await _txns(s) == []


async def test_offer_respond_by_the_wrong_person_is_denied_and_touches_nothing(s):
    bot = _FakeBot()
    cog = _cog(bot)
    offer = await _h1_to_h2(s)
    ix = await _ix(s, _H1)
    await cog.offer_respond.callback(cog, ix, offer.id, SimpleNamespace(value="accept"))
    assert await _status(s, offer.id) is TradeStatus.PROPOSED
    assert "accepted" not in ix.text


async def test_counter_offer_resolves_the_original_and_delivers_the_counter_to_the_sender(s):
    bot = _FakeBot()
    to_h1 = await _give_gm_channel(s, bot, "H1")
    cog = _cog(bot)
    original = await _h1_to_h2(s)

    ix = await _ix(s, _H2)
    await cog.counter_offer.callback(
        cog, ix, original.id,
        send_player_1=s.players["H2"][1], get_player_1=s.players["H1"][1],
    )

    assert await _status(s, original.id) is TradeStatus.COUNTERED
    offers = await offer_repo.list_trade_offers_for_league(s.pool, s.league_id)
    new = next(o for o in offers if o.counters_offer_id == original.id)
    assert (new.from_team_id, new.to_team_id) == (s.team["H2"], s.team["H1"])
    assert new.status is TradeStatus.PROPOSED
    assert new.package["players_from_to"] == (s.players["H2"][1],)
    assert new.message_id is not None and len(to_h1.sent) == 1
    assert ix.followup.sent[0]["content"] == (
        f"Countered offer #{original.id} with offer #{new.id}; it is waiting for H1's manager."
    )


async def test_veto_offer_kills_a_pending_offer_and_only_for_the_commissioner(s):
    bot = _FakeBot()
    cog = _cog(bot)
    offer = await _h1_to_h2(s)

    denied = await _ix(s, _H2)
    await cog.veto_offer.callback(cog, denied, offer.id)
    assert await _status(s, offer.id) is TradeStatus.PROPOSED

    ok = await _ix(s, _COMMISSIONER)
    await cog.veto_offer.callback(cog, ok, offer.id)
    assert ok.text == f"Offer #{offer.id} vetoed."
    assert await _status(s, offer.id) is TradeStatus.VETOED

    again = await _ix(s, _COMMISSIONER)
    await cog.veto_offer.callback(cog, again, offer.id)
    assert again.text == f"Offer #{offer.id} was already vetoed -- nothing was changed."


async def test_veto_works_in_a_phase_where_trading_is_closed(s):
    """§8: `veto_offer` is `ALL_PHASES`."""
    bot = _FakeBot()
    cog = _cog(bot)
    offer = await _h1_to_h2(s)
    await league_repo.set_current_phase(s.pool, s.league_id, Phase.PLAYOFFS_R1)
    ix = await _ix(s, _COMMISSIONER)
    await cog.veto_offer.callback(cog, ix, offer.id)
    assert await _status(s, offer.id) is TradeStatus.VETOED


async def test_a_command_from_another_leagues_server_cannot_touch_this_leagues_offer(s):
    """Every command resolves the league from the invoking guild, so an offer
    id from another league is "no such offer" for respond, counter and veto --
    even for user ids that are authorised on the offer's own league (the
    receiving manager, the commissioner)."""
    other = await _scenario(s.pool)
    try:
        cog = _cog(_FakeBot())
        offer = await _h1_to_h2(s)
        league_b = await league_repo.get_league(s.pool, other.league_id)

        respond = _Interaction(league_b.discord_guild_id, _H2)
        await cog.offer_respond.callback(cog, respond, offer.id, SimpleNamespace(value="accept"))
        counter = _Interaction(league_b.discord_guild_id, _H2)
        await cog.counter_offer.callback(
            cog, counter, offer.id,
            send_player_1=other.players["H2"][1], get_player_1=other.players["H1"][1],
        )
        veto = _Interaction(league_b.discord_guild_id, _COMMISSIONER)
        await cog.veto_offer.callback(cog, veto, offer.id)

        for ix in (respond, counter, veto):
            assert ix.text == "No such offer."
        assert await _status(s, offer.id) is TradeStatus.PROPOSED
        assert await _txns(s) == [] and await _txns(other) == []
        assert len(await offer_repo.list_trade_offers_for_league(s.pool, s.league_id)) == 1
        assert await offer_repo.list_trade_offers_for_league(s.pool, other.league_id) == []
    finally:
        await s.pool.execute("DELETE FROM leagues WHERE id = $1", other.league_id)
