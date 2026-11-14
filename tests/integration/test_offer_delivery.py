"""Live-Postgres proof for `dba2/bot/offer_delivery.py` -- the delivery drain
(design §6.5, §7.4 test 6) and `OfferActionButton.callback` driven against the
real `offer_service` (§7.4 test 14's integration half, NIT 12).

Discord is doubled: a fake bot/channel records what would be posted, a fake
`Interaction` records what would be replied. No gateway. The drain's
at-least-once behaviour is pinned as INTENDED here (§6.5) -- a re-post after a
lost write-back is not a bug these tests tolerate but a property they assert.

Requires `docker compose up -d` and a `.env`/DATABASE_URL, like test_smoke.py.
"""
from __future__ import annotations

import random

import discord
import pytest

from dba2.bot.interaction_errors import GENERIC_FAILURE_MESSAGE
from dba2.bot.offer_delivery import (
    Delivery,
    OfferActionButton,
    deliver_offer,
    drain_undelivered_offers,
)
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import channel_repo, league_repo, offer_repo
from dba2.domain.phase import Phase, TradeStatus
from dba2.presentation.offers import EXPIRY_SUFFIX

from .test_offer_service import (
    _COMMISSIONER,
    _H1,
    _H2,
    _OUTSIDER,
    _h1_to_h2,
    _offer,
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


# --- Discord doubles -------------------------------------------------------


class _FakeMessage:
    def __init__(self, message_id: int, channel: _FakeChannel) -> None:
        self.id = message_id
        self.channel = channel


class _FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: list[dict] = []
        self.fail_with: Exception | None = None
        self._next = random.randint(10**6, 10**9)

    async def send(self, **kwargs) -> _FakeMessage:
        if self.fail_with is not None:
            raise self.fail_with
        self._next += 1
        self.sent.append(kwargs)
        return _FakeMessage(self._next, self)


class _FakeBot:
    """`get_channel` is the cache; `fetch_channel` is Discord's HTTP lookup and
    raises `NotFound` for a channel that no longer exists."""

    def __init__(self) -> None:
        self.channels: dict[int, _FakeChannel] = {}

    def add_channel(self) -> _FakeChannel:
        channel = _FakeChannel(random.randint(10**9, 2 * 10**9))
        self.channels[channel.id] = channel
        return channel

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        if channel_id not in self.channels:
            raise discord.NotFound.__new__(discord.NotFound)
        return self.channels[channel_id]


async def _give_gm_channel(s, bot: _FakeBot, team_code: str) -> _FakeChannel:
    channel = bot.add_channel()
    await channel_repo.create_gm_channel(
        s.pool, league_id=s.league_id, team_id=s.team[team_code], channel_id=channel.id
    )
    return channel


class _Response:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._done = True
        self.calls.append(f"defer(ephemeral={ephemeral})")

    async def send_message(self, content=None, *, ephemeral: bool = False, **_kw) -> None:
        self._done = True
        self.calls.append(f"send_message({content!r}, ephemeral={ephemeral})")


class _Followup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, *, ephemeral: bool = False, **_kw) -> None:
        self.sent.append({"content": content, "ephemeral": ephemeral})


class _Interaction:
    def __init__(self, *, guild_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.id = random.randint(1, 10**9)
        self.user = type("U", (), {"id": user_id})()
        self.response = _Response()
        self.followup = _Followup()


async def _click(s, action: str, offer_id: int, *, by: int) -> _Interaction:
    league = await league_repo.get_league(s.pool, s.league_id)
    interaction = _Interaction(guild_id=league.discord_guild_id, user_id=by)
    await OfferActionButton(action, offer_id).callback(interaction)
    return interaction


# --- the drain -------------------------------------------------------------


async def test_drain_posts_the_offer_records_where_and_a_second_drain_posts_nothing(s):
    """§7.4 test 6: `message_id IS NULL` is the queue; delivery writes it."""
    bot = _FakeBot()
    channel = await _give_gm_channel(s, bot, "H2")
    offer = await _h1_to_h2(s)

    first = await drain_undelivered_offers(bot, s.pool, league_id=s.league_id)
    assert first.outcomes == {offer.id: Delivery.DELIVERED}
    assert len(channel.sent) == 1
    row = await offer_repo.get_trade_offer(s.pool, offer.id)
    assert row.channel_id == channel.id
    assert row.message_id is not None

    second = await drain_undelivered_offers(bot, s.pool, league_id=s.league_id)
    assert second.outcomes == {}
    assert len(channel.sent) == 1


async def test_a_lost_writeback_reposts_the_offer_which_is_the_accepted_at_least_once(s):
    """§6.5 -- a crash between the Discord post and the id write-back must
    re-post rather than leave a row that claims a delivery that did not
    happen. Pinned as intended, not tolerated."""
    bot = _FakeBot()
    channel = await _give_gm_channel(s, bot, "H2")
    offer = await _h1_to_h2(s)
    await drain_undelivered_offers(bot, s.pool, league_id=s.league_id)
    await s.pool.execute("UPDATE trade_offers SET message_id = NULL WHERE id = $1", offer.id)

    again = await drain_undelivered_offers(bot, s.pool, league_id=s.league_id)
    assert again.outcomes == {offer.id: Delivery.DELIVERED}
    assert len(channel.sent) == 2


async def test_the_post_mentions_only_the_manager_and_carries_three_buttons_and_the_panel(s):
    bot = _FakeBot()
    channel = await _give_gm_channel(s, bot, "H2")
    offer = await _h1_to_h2(s)
    await drain_undelivered_offers(bot, s.pool, league_id=s.league_id)

    sent = channel.sent[0]
    assert sent["content"].startswith(f"<@{_H2}>")
    mentions = sent["allowed_mentions"]
    assert mentions.everyone is False and mentions.roles is False
    assert [u.id for u in mentions.users] == [_H2]
    assert [b.custom_id for b in sent["view"].children] == [
        f"dba2:offer:accept:{offer.id}",
        f"dba2:offer:reject:{offer.id}",
        f"dba2:offer:counter:{offer.id}",
    ]
    assert f"#{offer.id}" in sent["embed"].title
    assert {f.name for f in sent["embed"].fields} == {"H1 sends", "H2 sends"}


async def test_an_ai_initiated_offer_is_delivered_like_any_other(s):
    """The reason the queue exists: `run_phase_hook` writes AI offers with no
    Discord in reach; they must surface the same way."""
    bot = _FakeBot()
    channel = await _give_gm_channel(s, bot, "H1")
    ai_offer = await _offer(
        s, "AI", "H1", give=(s.players["AI"][0],), get=(s.players["H1"][0],), proposed_by=None
    )
    report = await drain_undelivered_offers(bot, s.pool, league_id=s.league_id)
    assert report.outcomes == {ai_offer.id: Delivery.DELIVERED}
    assert "AI GM" in channel.sent[0]["embed"].description


async def test_an_offer_with_no_gm_channel_stays_queued_and_the_others_still_go(s):
    bot = _FakeBot()
    channel = await _give_gm_channel(s, bot, "H2")
    no_channel = await _offer(
        s, "H2", "H1", give=(s.players["H2"][0],), get=(s.players["H1"][0],), proposed_by=_H2
    )
    deliverable = await _h1_to_h2(s)

    report = await drain_undelivered_offers(bot, s.pool, league_id=s.league_id)
    assert report.outcomes == {
        no_channel.id: Delivery.NO_GM_CHANNEL,
        deliverable.id: Delivery.DELIVERED,
    }
    assert len(channel.sent) == 1
    assert (await offer_repo.get_trade_offer(s.pool, no_channel.id)).message_id is None


async def test_an_offer_addressed_to_an_ai_team_is_not_posted(s):
    """An AI-managed team reads no inbox; there is nobody to mention."""
    bot = _FakeBot()
    await _give_gm_channel(s, bot, "AI")
    offer = await _offer(
        s, "H1", "AI", give=(s.players["H1"][0],), get=(s.players["AI"][0],), proposed_by=_H1
    )
    assert await deliver_offer(bot, s.pool, offer.id) is Delivery.NO_HUMAN_RECIPIENT


async def test_an_offer_resolved_before_the_drain_reaches_it_is_not_posted(s):
    """The drain lists first and posts later; a stale list must not put live
    buttons on a resolved offer."""
    bot = _FakeBot()
    channel = await _give_gm_channel(s, bot, "H2")
    offer = await _h1_to_h2(s, status=TradeStatus.REJECTED)
    assert await deliver_offer(bot, s.pool, offer.id) is Delivery.NOT_PENDING
    assert channel.sent == []


async def test_a_channel_that_no_longer_exists_leaves_the_offer_queued(s):
    """§13.4's named risk -- someone deleted the GM channel. Recorded as
    unreachable and retried next drain; never a crash, never a false
    `message_id`."""
    bot = _FakeBot()
    channel = await _give_gm_channel(s, bot, "H2")
    del bot.channels[channel.id]
    offer = await _h1_to_h2(s)
    assert await deliver_offer(bot, s.pool, offer.id) is Delivery.CHANNEL_UNREACHABLE
    assert (await offer_repo.get_trade_offer(s.pool, offer.id)).message_id is None


async def test_a_forbidden_post_or_an_unexpected_error_never_stops_the_drain(s):
    bot = _FakeBot()
    forbidden = await _give_gm_channel(s, bot, "H2")
    forbidden.fail_with = discord.Forbidden.__new__(discord.Forbidden)
    first = await _h1_to_h2(s)
    boom = await _give_gm_channel(s, bot, "H1")
    boom.fail_with = RuntimeError("Discord is having a day")
    second = await _offer(
        s, "H2", "H1", give=(s.players["H2"][1],), get=(s.players["H1"][1],), proposed_by=_H2
    )

    report = await drain_undelivered_offers(bot, s.pool, league_id=s.league_id)
    assert report.outcomes == {
        first.id: Delivery.CHANNEL_UNREACHABLE,
        second.id: Delivery.FAILED,
    }
    for offer_id in (first.id, second.id):
        assert (await offer_repo.get_trade_offer(s.pool, offer_id)).message_id is None


# --- the button, against the real service ----------------------------------


async def test_accept_click_by_the_receiving_manager_executes_the_trade(s):
    offer = await _h1_to_h2(s)
    interaction = await _click(s, "accept", offer.id, by=_H2)

    assert interaction.response.calls == ["defer(ephemeral=True)"]
    assert interaction.followup.sent == [
        {"content": f"Offer #{offer.id} accepted -- the trade is done.", "ephemeral": True}
    ]
    assert await _status(s, offer.id) is TradeStatus.ACCEPTED
    assert await _team_of(s, s.players["H1"][0]) == s.team["H2"]
    assert len(await _txns(s)) == 1


async def test_reject_click_rejects_and_moves_nothing(s):
    offer = await _h1_to_h2(s)
    interaction = await _click(s, "reject", offer.id, by=_H2)
    assert interaction.followup.sent[0]["content"] == f"Offer #{offer.id} rejected."
    assert await _status(s, offer.id) is TradeStatus.REJECTED
    assert await _txns(s) == []


@pytest.mark.parametrize(
    "who", [_H1, _OUTSIDER, _COMMISSIONER], ids=["proposer", "outsider", "commissioner"]
)
async def test_a_click_by_anyone_but_the_receiving_manager_is_denied_ephemerally(s, who):
    """Authority comes from the database on every click, never from the message
    (§1): a button anyone can see is a button anyone can press. The commissioner
    who does not manage the receiving team is the B3 case -- denied here, with
    `/veto-offer` as their lever."""
    offer = await _h1_to_h2(s)
    interaction = await _click(s, "accept", offer.id, by=who)

    assert len(interaction.followup.sent) == 1
    assert interaction.followup.sent[0]["ephemeral"] is True
    assert "accepted" not in interaction.followup.sent[0]["content"]
    assert await _status(s, offer.id) is TradeStatus.PROPOSED
    assert await _txns(s) == []


async def test_a_second_click_reports_the_current_state_and_writes_nothing(s):
    """NIT 12: buttons are not edited or disabled after resolution; a later
    click lands on `respond`'s "already answered" -- and must not read as
    success."""
    offer = await _h1_to_h2(s)
    await _click(s, "accept", offer.id, by=_H2)
    again = await _click(s, "accept", offer.id, by=_H2)
    assert again.followup.sent == [
        {
            "content": f"Offer #{offer.id} was already accepted -- nothing was changed.",
            "ephemeral": True,
        }
    ]
    assert len(await _txns(s)) == 1


async def test_a_click_in_a_closed_phase_gets_the_expiry_sentence(s):
    """§5.3 NIT 10 -- an offer made at the deadline and answered after it hits
    `require_phase` first; the sentence is what makes that message about the
    offer."""
    offer = await _h1_to_h2(s)
    await league_repo.set_current_phase(s.pool, s.league_id, Phase.POST_DEADLINE)
    interaction = await _click(s, "accept", offer.id, by=_H2)
    text = interaction.followup.sent[0]["content"]
    assert text.endswith(EXPIRY_SUFFIX)
    assert await _status(s, offer.id) is TradeStatus.PROPOSED


async def test_a_click_for_an_offer_that_does_not_exist_says_so(s):
    interaction = await _click(s, "accept", 987_654_321, by=_H2)
    assert interaction.followup.sent == [{"content": "No such offer.", "ephemeral": True}]


async def test_a_click_from_a_server_with_no_league_says_so(s):
    interaction = _Interaction(guild_id=1, user_id=_H2)
    await OfferActionButton("accept", 1).callback(interaction)
    assert interaction.response.calls == ["defer(ephemeral=True)"]
    assert "no league is set up for this server" in interaction.followup.sent[0]["content"]


async def test_an_unexpected_failure_still_gets_a_reply_because_the_library_swallows_it(
    s, monkeypatch
):
    """`ViewStore` catches whatever the callback raises and only logs it
    (`discord/ui/view.py:1038-1041`); the human sees "This interaction failed".
    So the callback must have replied BEFORE anything can escape it."""
    from dba2.bot import offer_delivery

    async def boom(*_a, **_kw):
        raise RuntimeError("asyncpg connection reset")

    monkeypatch.setattr(offer_delivery.offer_service, "respond", boom)
    league = await league_repo.get_league(s.pool, s.league_id)
    interaction = _Interaction(guild_id=league.discord_guild_id, user_id=_H2)
    try:
        await OfferActionButton("accept", 1).callback(interaction)
    except Exception:  # what ViewStore does
        pass
    assert interaction.followup.sent == [{"content": GENERIC_FAILURE_MESSAGE, "ephemeral": True}]


@pytest.mark.parametrize("action", ["accept", "reject"])
async def test_a_click_in_another_leagues_server_cannot_answer_this_leagues_offer(s, action):
    """The offer id lives in the button's `custom_id`, and a `custom_id` can be
    replayed from any guild the bot is in. The league comes from the CLICKER's
    guild, so an id that belongs to another league is "no such offer" -- even
    for a user id that manages the receiving team in the offer's own league."""
    other = await _scenario(s.pool)
    try:
        offer = await _h1_to_h2(s)
        league_b = await league_repo.get_league(s.pool, other.league_id)
        interaction = _Interaction(guild_id=league_b.discord_guild_id, user_id=_H2)
        await OfferActionButton(action, offer.id).callback(interaction)

        assert interaction.followup.sent == [{"content": "No such offer.", "ephemeral": True}]
        assert await _status(s, offer.id) is TradeStatus.PROPOSED
        assert await _txns(s) == []
        assert await _txns(other) == []
    finally:
        await s.pool.execute("DELETE FROM leagues WHERE id = $1", other.league_id)
