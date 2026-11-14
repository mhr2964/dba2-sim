"""The startup claim report and the wire sweep (phase-5-stage-e-advance.md P5-99.s.4 and
B-5, build-plan row 17), driven through the real `DBABot._reconcile_guild` against real
Postgres rows and a fake Discord guild whose channels record what they were sent.

What a startup owes a league after a crash, and what it must NOT do:

- **A held advance claim is REPORTED and never cleared.** A claim is not proof its presser
  is dead; clearing a live one runs two AI passes at once. `_reconcile_guild` logs a
  WARNING naming the league, the holder and the claim's age, points at `/advance-unlock`,
  and carries on -- the offer drain and the `#games` sweep still run.
- **Unposted wire stories are posted at startup** (`_sweep_wire_at_startup`), one message
  per story through the same path a press uses, so a crash between a cycle's selection and
  its publication does not lose the cycle's wire posts even if nobody presses `/advance`
  for a week.
- **Two guards, because "startup" is not once (B-5).** `on_ready` re-fires on every Discord
  reconnect, so an unguarded sweep would start mid-press and race the press's own
  publication. (1) Once per process per league: a second reconcile posts NOTHING, even for a
  story that appeared in between -- which is what distinguishes the guard from "the rows are
  already marked". (2) Never while the claim is held: the sweep skips and logs.
- **A sweep that raises never fails the reconcile it rides on**, on either of its exits.

Every "posts nothing" assertion is paired with a control that DOES post, and the two guards
each have a mutation twin recorded in the commit body.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import logging
import random
from unittest.mock import AsyncMock, patch

import pytest

from dba2.bot import game_feed, offer_delivery, surfaces
from dba2.bot.client import build_bot
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, media_repo
from dba2.domain.media import StoryKind
from dba2.domain.phase import Phase
from tests.integration._press_fixtures import COMMISSIONER, SEASON
from tests.integration.test_bot_channels import _FakeChannel, _FakeGuild
from tests.integration.test_client_reconcile_guild import _ChannelCreateAlwaysFailsGuild

_HOLDER = 777


class _WireChannel(_FakeChannel):
    """A reconciled channel that records every embed sent to it."""

    def __init__(self, channel_id: int, name: str, overwrites: dict | None = None) -> None:
        super().__init__(channel_id, name, overwrites)
        self.sent: list[str] = []

    async def send(self, *, embed) -> None:
        self.sent.append(embed.description or embed.title)


class _Guild(_FakeGuild):
    async def create_text_channel(self, name: str, *, overwrites=None, **_kwargs):
        self._next_id += 1
        channel = _WireChannel(self._next_id, name, overwrites)
        self._channels[channel.id] = channel
        return channel

    def wire_sent(self) -> list[str]:
        (wire,) = [c for c in self._channels.values() if c.name == "league-wire"]
        return wire.sent


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


async def _league(pool, name: str = "Wire Sweep Test League"):
    return await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name=name,
        start_season_year=SEASON,
        current_season=SEASON,
        commissioner_user_id=COMMISSIONER,
        rng_seed=1,
    )


async def _story(pool, league_id: int, text: str) -> None:
    await media_repo.create_media_story(
        pool,
        league_id=league_id,
        season=SEASON,
        phase=Phase.PRESEASON,
        cycle_index=0,
        story_kind=StoryKind.STREAK,
        voice="beat_writer",
        template_id=f"streak.{text}",
        slot_bindings={},
        rendered_text=text,
        salience=0.5,
        subject_player_id=None,
        subject_team_id=None,
        writer_id=None,
    )


async def _unposted(pool, league_id: int) -> int:
    return len(
        await media_repo.list_unposted_media_stories(pool, league_id=league_id, season=SEASON)
    )


async def _bot():
    bot = build_bot()
    await bot.setup_hook()
    bot.tree.sync = AsyncMock()
    return bot


def _guild_for(league, cls=_Guild):
    guild = cls()
    guild.id = league.discord_guild_id
    return guild


async def _drop(pool, *league_ids: int) -> None:
    for league_id in league_ids:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)


# --- the wire sweep ----------------------------------------------------------------------


async def test_startup_posts_a_leagues_unposted_stories_once_each_and_marks_them(pool):
    league = await _league(pool)
    try:
        await _story(pool, league.id, "first")
        await _story(pool, league.id, "second")
        guild = _guild_for(league)
        bot = await _bot()

        await bot._reconcile_guild(guild, pool)

        assert sorted(guild.wire_sent()) == ["first", "second"]
        assert await _unposted(pool, league.id) == 0
    finally:
        await _drop(pool, league.id)


async def test_a_second_reconcile_for_the_same_guild_posts_nothing_even_for_a_new_story(pool):
    """B-5 case 1: `on_ready` re-fires on a reconnect. A story that appeared after the first
    sweep (a press mid-publication, say) must be left to that press, not swept by the
    reconnect -- so the assertion is about a story that is UNPOSTED at the second reconcile."""
    league = await _league(pool)
    try:
        await _story(pool, league.id, "before")
        guild = _guild_for(league)
        bot = await _bot()
        await bot._reconcile_guild(guild, pool)
        assert guild.wire_sent() == ["before"]

        await _story(pool, league.id, "during a press")
        await bot._reconcile_guild(guild, pool)

        assert guild.wire_sent() == ["before"], "the reconnect swept a story a press may be posting"
        assert await _unposted(pool, league.id) == 1
    finally:
        await _drop(pool, league.id)


async def test_the_once_per_process_state_is_per_league_and_per_bot(pool):
    """Two guilds are independent: sweeping one does not stop the other; and a fresh bot (a
    new process, or the next test) starts with nothing swept."""
    league_a, league_b = await _league(pool, "Sweep A"), await _league(pool, "Sweep B")
    try:
        await _story(pool, league_a.id, "a1")
        await _story(pool, league_b.id, "b1")
        guild_a, guild_b = _guild_for(league_a), _guild_for(league_b)
        bot = await _bot()

        await bot._reconcile_guild(guild_a, pool)
        assert guild_a.wire_sent() == ["a1"] and await _unposted(pool, league_b.id) == 1
        await bot._reconcile_guild(guild_b, pool)
        assert guild_b.wire_sent() == ["b1"]

        await _story(pool, league_a.id, "a2")
        fresh = await _bot()
        await fresh._reconcile_guild(guild_a, pool)
        assert guild_a.wire_sent() == ["a1", "a2"]
    finally:
        await _drop(pool, league_a.id, league_b.id)


async def test_a_league_with_no_unposted_stories_and_no_wire_channel_sweeps_quietly(pool, caplog):
    """The bail-out exit of a guild whose channel creates all fail has no wire channel; with
    nothing to post, that is not an error worth a traceback at every startup."""
    league = await _league(pool)
    try:
        guild = _guild_for(league, _ChannelCreateAlwaysFailsGuild)
        bot = await _bot()
        with caplog.at_level(logging.ERROR, logger="dba2.bot.client"):
            await bot._reconcile_guild(guild, pool)
        assert not [r for r in caplog.records if "wire sweep" in r.getMessage()]
    finally:
        await _drop(pool, league.id)


async def test_a_sweep_on_a_league_whose_claim_is_held_skips_and_logs(pool, caplog):
    """B-5 case 2: a press is publishing under its own claim; the sweep must not race it."""
    league = await _league(pool)
    try:
        await _story(pool, league.id, "held")
        assert await league_repo.claim_advance(pool, league.id, _HOLDER) is not None
        guild = _guild_for(league)
        bot = await _bot()

        with caplog.at_level(logging.INFO, logger="dba2.bot.client"):
            await bot._reconcile_guild(guild, pool)

        assert guild.wire_sent() == []
        assert await _unposted(pool, league.id) == 1
        assert [
            r
            for r in caplog.records
            if "wire sweep" in r.getMessage() and "skipp" in r.getMessage()
        ]
    finally:
        await _drop(pool, league.id)


async def test_the_claim_check_reads_the_claim_at_sweep_time_not_at_reconcile_start(pool):
    """The league row `_reconcile_guild` loaded is seconds old by the time the sweep runs
    (channel and GM-channel reconciles sit between). A claim taken in that gap must still be
    seen, so the sweep re-reads it."""
    league = await _league(pool)
    try:
        await _story(pool, league.id, "late claim")
        guild = _guild_for(league)
        bot = await _bot()
        real_sweep = bot._sweep_games_at_startup

        async def claim_then_sweep(g, p, league_id):
            assert await league_repo.claim_advance(p, league_id, _HOLDER) is not None
            await real_sweep(g, p, league_id)

        with patch.object(bot, "_sweep_games_at_startup", side_effect=claim_then_sweep):
            await bot._reconcile_guild(guild, pool)

        assert guild.wire_sent() == []
    finally:
        await _drop(pool, league.id)


async def test_a_wire_sweep_that_raises_never_fails_the_reconcile(pool):
    league = await _league(pool)
    try:
        await _story(pool, league.id, "boom")
        guild = _guild_for(league)
        bot = await _bot()
        with patch.object(
            surfaces,
            "post_new_wire_stories",
            new_callable=AsyncMock,
            side_effect=RuntimeError("discord is down"),
        ) as posted:
            await bot._reconcile_guild(guild, pool)  # must not raise
        posted.assert_awaited_once()
    finally:
        await _drop(pool, league.id)


async def test_the_wire_sweep_runs_on_both_reconcile_exits_after_the_games_sweep(pool):
    league = await _league(pool)
    healthy = await _league(pool, "Healthy")
    try:
        await _story(pool, league.id, "x")
        await _story(pool, healthy.id, "y")
        order: list[str] = []

        async def games(*_a, **_k):
            order.append("games")
            return game_feed.SweepReport(league_id=league.id, entries=())

        async def wire(*_a, **_k):
            order.append("wire")
            return 0

        bot = await _bot()
        with (
            patch.object(game_feed, "sweep_league", side_effect=games),
            patch.object(surfaces, "post_new_wire_stories", side_effect=wire) as posted,
        ):
            await bot._reconcile_guild(_guild_for(league, _ChannelCreateAlwaysFailsGuild), pool)
            assert order == ["games", "wire"], "the bail-out exit skipped the wire sweep"
            assert posted.await_args.kwargs == {
                "league_id": league.id,
                "season": SEASON,
                "cycle_index": None,
            }
            order.clear()
            await bot._reconcile_guild(_guild_for(healthy), pool)
            assert order == ["games", "wire"]
    finally:
        await _drop(pool, league.id, healthy.id)


# --- the claim report ---------------------------------------------------------------------


async def test_a_held_claim_logs_a_warning_naming_league_holder_and_age_and_is_not_cleared(
    pool, caplog
):
    league = await _league(pool)
    try:
        claimed_at = await league_repo.claim_advance(pool, league.id, _HOLDER)
        assert claimed_at is not None
        guild = _guild_for(league)
        bot = await _bot()

        with (
            patch.object(
                offer_delivery, "drain_undelivered_offers", new_callable=AsyncMock
            ) as drain,
            patch.object(game_feed, "sweep_league", new_callable=AsyncMock) as swept,
            caplog.at_level(logging.WARNING, logger="dba2.bot.client"),
        ):
            drain.return_value = offer_delivery.DrainReport(outcomes={})
            swept.return_value = game_feed.SweepReport(league_id=league.id, entries=())
            await bot._reconcile_guild(guild, pool)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        (report,) = [r for r in warnings if "has an advance claim" in r.getMessage()]
        text = report.getMessage()
        assert f"league {league.id}" in text and f"user {_HOLDER}" in text
        assert "minute" in text and "/advance-unlock" in text
        # Not cleared, by the row itself.
        after = await league_repo.get_league(pool, league.id)
        assert after.advance_claimed_at == claimed_at and after.advance_claimed_by == _HOLDER
        # The rest of the startup still ran.
        drain.assert_awaited_once()
        swept.assert_awaited_once()
    finally:
        await _drop(pool, league.id)


async def test_no_claim_means_no_claim_warning(pool, caplog):
    league = await _league(pool)
    try:
        bot = await _bot()
        with caplog.at_level(logging.WARNING, logger="dba2.bot.client"):
            await bot._reconcile_guild(_guild_for(league), pool)
        assert not [r for r in caplog.records if "has an advance claim" in r.getMessage()]
    finally:
        await _drop(pool, league.id)
