"""Live I/O proof for `dba2/bot/draft_feed.py` -- the draft's two posts, and
the order they happen in (docs/design/phase-5-stage-d-prime-draft.md §4
commit 7, P5-98.i). Real Postgres for the channel rows, in-process fakes for
Discord: nothing in this stage has run against a live gateway.

**The three claims worth a test each.**

1. **The ping goes FIRST, and it is the only message allowed to mention
   anyone.** A `#games` line somebody is not watching is not a notification,
   so the GM channel is written before the public record; and the ping's
   `allowed_mentions` names exactly the manager, because Discord's default
   would let an `@everyone` inside any other part of a message become a real
   ping.
2. **Neither post can abort the other, and neither can turn a committed pick
   into an error.** A league can lose either channel at any time, so each
   half is wrapped independently -- and the reply discloses the gap through
   `honest_suffix` rather than claiming nothing happened.
3. **Announce-once.** A second `/draft` posts ITS pick and no earlier one.
   The lines come from the cycle's own outcome, never from a re-read of
   `draft_selections` -- which is also pinned statically in
   `tests/unit/bot/test_draft_feed_pins.py`.

**The fakes suspend.** Every fake `send` yields to the event loop before it
records, so nothing here passes because a fake happened to be synchronous.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from unittest.mock import patch

import discord

from dba2.bot import draft_feed
from dba2.bot.cogs import draft as draft_cog
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import (
    channel_repo,
    draft_repo,
    league_repo,
    player_repo,
    ratings_repo,
    team_repo,
)
from dba2.domain.channel import ChannelPurpose
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import RatingSource
from dba2.domain.team import GmPhilosophy, Timeline
from dba2.services import gm_command_service, gm_service
from tests.fixtures.managers import birth_date_for_age
from tests.fixtures.sim import make_ratings

_SEASON = 2026
_COMMISSIONER = 1
_TOR_MANAGER = 101
_CHI_MANAGER = 202
_GAMES_CHANNEL_ID = 900_001
_TOR_GM_CHANNEL_ID = 900_002
_CHI_GM_CHANNEL_ID = 900_003

_RATINGS_NON_COLUMN_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
)

_message_ids = itertools.count(5_000_000)


def _ratings_columns(seed: int) -> dict[str, int]:
    base = make_ratings(seed)
    return {
        f.name: getattr(base, f.name)
        for f in dataclasses.fields(base)
        if f.name not in _RATINGS_NON_COLUMN_FIELDS
    }


# ---------------------------------------------------------------------------
# Discord fakes -- every send suspends before it records.
# ---------------------------------------------------------------------------


@dataclass
class _Sent:
    channel_id: int
    content: str
    allowed_mentions: object


class _FakeMessage:
    def __init__(self) -> None:
        self.id = next(_message_ids)


class _FakeChannel:
    """A channel that can take a message. `get_partial_message` exists
    because `game_feed._resolve_channel` refuses a channel without it -- a
    category or a voice channel cannot hold the `#games` record."""

    def __init__(self, channel_id: int, log: list[_Sent], *, fails: bool = False) -> None:
        self.id = channel_id
        self._log = log
        self._fails = fails

    async def send(self, content: str = "", *, allowed_mentions=None, **kwargs) -> _FakeMessage:
        await asyncio.sleep(0)  # a real send suspends; a fake that cannot hides interleaving
        if self._fails:
            raise RuntimeError(f"channel {self.id} refused the message")
        self._log.append(
            _Sent(channel_id=self.id, content=content, allowed_mentions=allowed_mentions)
        )
        return _FakeMessage()

    def get_partial_message(self, message_id: int):
        raise AssertionError("the draft edits nothing; it posts new messages only")


@dataclass
class _FakeBot:
    """`get_channel` only; `fetch_channel` is never needed because every
    channel this test registers is 'cached'."""

    channels: dict[int, _FakeChannel] = field(default_factory=dict)
    sent: list[_Sent] = field(default_factory=list)

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        channel = self.channels.get(channel_id)
        if channel is None:
            raise RuntimeError(f"no channel {channel_id}")
        return channel


@dataclass(frozen=True)
class _Fixture:
    league_id: int
    guild_id: int
    draft_id: int
    teams: dict[str, int]
    prospects: tuple[int, ...]
    bot: _FakeBot


async def _build(
    pool,
    *,
    games_channel: bool = True,
    gm_channels: bool = True,
    games_channel_fails: bool = False,
    gm_channels_fail: bool = False,
) -> _Fixture:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Draft Feed Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=2026_10,
        current_phase=Phase.DRAFT,
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
        roster_min=14,
        roster_max=15,
    )
    managers = {"TOR": _TOR_MANAGER, "CHI": _CHI_MANAGER}
    teams: dict[str, int] = {}
    for code in ("TOR", "BOS", "CHI"):
        team = await team_repo.create_team(
            pool,
            league_id=league.id,
            code=code,
            name=f"{code} Team",
            city=f"{code} City",
            conference="East",
            division="Atlantic",
            home_market_size=3,
            manager_user_id=managers.get(code),
            gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER,
            timeline=Timeline.RETOOL,
        )
        teams[code] = team.id
    # TOR picks, BOS is the AI in between, CHI is the next person -- twice
    # over, so a second `/draft` has something of its own to announce.
    board = ((1, 30, "TOR"), (2, 31, "BOS"), (2, 32, "CHI"), (3, 33, "TOR"))
    draft = await draft_repo.create_draft(
        pool, league_id=league.id, season=_SEASON, total_picks=len(board)
    )
    for round_, pick_number, code in board:
        await draft_repo.create_draft_pick(
            pool,
            league_id=league.id,
            season=_SEASON,
            round=round_,
            original_team_id=teams[code],
            current_team_id=teams[code],
            pick_number=pick_number,
        )
    prospects = []
    for i in range(5):
        player = await player_repo.create_player(
            pool,
            league_id=league.id,
            first_name="Prospect",
            last_name=f"Number{i}",
            primary_position=Position.SF,
            height_in=79,
            weight_lb=215,
            wingspan_in=82,
            birth_date=birth_date_for_age(19, _SEASON),
            roster_status=RosterStatus.DRAFT_ELIGIBLE,
            overall_cached=70 - i,
            overall_computed_at=datetime.now(UTC),
            team_id=None,
        )
        await ratings_repo.upsert_player_ratings(
            pool,
            player_id=player.id,
            league_id=league.id,
            source=RatingSource.GENERATED,
            ratings=_ratings_columns(player.id),
        )
        prospects.append(player.id)

    bot = _FakeBot()
    if games_channel:
        await channel_repo.create_league_channel(
            pool, league_id=league.id, purpose=ChannelPurpose.GAMES, channel_id=_GAMES_CHANNEL_ID
        )
        bot.channels[_GAMES_CHANNEL_ID] = _FakeChannel(
            _GAMES_CHANNEL_ID, bot.sent, fails=games_channel_fails
        )
    if gm_channels:
        for code, channel_id in (("TOR", _TOR_GM_CHANNEL_ID), ("CHI", _CHI_GM_CHANNEL_ID)):
            await channel_repo.create_gm_channel(
                pool, league_id=league.id, team_id=teams[code], channel_id=channel_id
            )
            bot.channels[channel_id] = _FakeChannel(channel_id, bot.sent, fails=gm_channels_fail)
    return _Fixture(
        league_id=league.id,
        guild_id=league.discord_guild_id,
        draft_id=draft.id,
        teams=teams,
        prospects=tuple(prospects),
        bot=bot,
    )


async def _cleanup(pool, f: _Fixture) -> None:
    gm_service.artifact_path(f.league_id, _SEASON).unlink(missing_ok=True)
    await pool.execute("DELETE FROM leagues WHERE id = $1", f.league_id)


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self._done = False
        self.sent_messages: list[dict] = []

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred = True
        self._done = True

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, message: str, *, ephemeral: bool = False) -> None:
        self._done = True
        self.sent_messages.append({"content": message})


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, *, embed=None, ephemeral: bool = False, **kwargs):
        self.sent.append({"content": content, "embed": embed})


class _FakeInteraction:
    def __init__(self, *, guild_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.guild = None
        self.user = _FakeUser(user_id)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.command = "draft"
        self.id = random.randint(1, 10**9)


async def _draft(f: _Fixture, *, code: str, prospect: int, actor: int):
    cog = draft_cog.DraftCommands(bot=f.bot)
    interaction = _FakeInteraction(guild_id=f.guild_id, user_id=actor)
    await cog.draft.callback(cog, interaction, code, f.prospects[prospect])
    return interaction


async def _draft_an_available_prospect(pool, f: _Fixture, *, code: str, actor: int):
    """The AI's own choice among the pool depends on ratings seeded from
    real player ids, so which prospect it takes is not fixed run to run --
    a second command that named a hard-coded index would be refused
    ("taken at pick 31") in some runs and not others. This asks the board."""
    taken = {
        s.player_id for s in await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
    }
    available = [p for p in f.prospects if p not in taken]
    assert available, "the fixture ran out of prospects"
    cog = draft_cog.DraftCommands(bot=f.bot)
    interaction = _FakeInteraction(guild_id=f.guild_id, user_id=actor)
    await cog.draft.callback(cog, interaction, code, available[0])
    return interaction


# ---------------------------------------------------------------------------
# The two posts, and their order.
# ---------------------------------------------------------------------------


async def test_the_manager_is_pinged_first_and_games_gets_the_record_second():
    """P5-98.i's order, and its one mention. The ping is the only way a
    person learns it is their turn, so it goes first; `#games` carries the
    picks and the clock with nothing that can ping."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            interaction = await _draft(f, code="TOR", prospect=0, actor=_TOR_MANAGER)

            assert [s.channel_id for s in f.bot.sent] == [_CHI_GM_CHANNEL_ID, _GAMES_CHANNEL_ID]

            ping, public = f.bot.sent
            assert ping.content.startswith(f"<@{_CHI_MANAGER}> **CHI are on the clock**")
            assert "/draft code:CHI" in ping.content
            assert isinstance(ping.allowed_mentions, discord.AllowedMentions)
            assert [u.id for u in ping.allowed_mentions.users] == [_CHI_MANAGER]
            assert ping.allowed_mentions.everyone is False
            assert ping.allowed_mentions.roles is False

            assert "<@" not in public.content
            assert "**Round 1, pick 30:** TOR select Prospect Number0 (manager's pick)." in (
                public.content
            )
            assert "**Round 2, pick 31:** BOS select " in public.content
            assert public.content.endswith("**CHI are on the clock** -- round 2, pick 32.")

            # The reply is a public success with a panel, and discloses no
            # announcement failure, because there was none.
            reply = interaction.followup.sent[0]
            assert reply["embed"] is not None
            assert "couldn't be posted" not in (reply["content"] or "")
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_second_pick_announces_only_its_own_lines():
    """Announce-once. The second `/draft` re-posts nothing from the first --
    which is what a re-read of `draft_selections` would have done, and is
    why the lines come from the cycle's own outcome instead."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            await _draft(f, code="TOR", prospect=0, actor=_TOR_MANAGER)
            first_post = f.bot.sent[1].content
            f.bot.sent.clear()

            second = await _draft_an_available_prospect(pool, f, code="CHI", actor=_CHI_MANAGER)
            assert second.followup.sent, (
                f"the second pick was refused: {second.response.sent_messages}"
            )

            public = [s for s in f.bot.sent if s.channel_id == _GAMES_CHANNEL_ID]
            assert len(public) == 1
            second_post = public[0]
            assert "pick 32:" in second_post.content
            assert "pick 30:" not in second_post.content, "the first pick was announced twice"
            assert "pick 31:" not in second_post.content, "the AI's pick was announced twice"
            assert "pick 30:" in first_post, "the first post proves nothing if it was empty"
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_team_with_no_gm_channel_is_logged_and_the_public_line_still_posts():
    """`Delivery.NO_GM_CHANNEL`'s precedent: not an error, and not a reason
    to skip the public record. The reply says the announcement was
    incomplete rather than claiming the pick failed."""
    pool = await get_pool()
    try:
        f = await _build(pool, gm_channels=False)
        try:
            interaction = await _draft(f, code="TOR", prospect=0, actor=_TOR_MANAGER)

            assert [s.channel_id for s in f.bot.sent] == [_GAMES_CHANNEL_ID]
            content = interaction.followup.sent[0]["content"]
            assert "TOR select Prospect Number0" in content
            assert "couldn't be posted in full" in content
            assert "no change was made" not in content.lower()

            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            assert [s.pick_number for s in selections] == [30, 31]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_league_with_no_games_channel_still_pings_the_person_on_the_clock():
    """The other half of the independence claim: `GamesChannelUnavailable`
    is a logged warning, and it must not swallow the ping that had already
    gone out."""
    pool = await get_pool()
    try:
        f = await _build(pool, games_channel=False)
        try:
            interaction = await _draft(f, code="TOR", prospect=0, actor=_TOR_MANAGER)

            assert [s.channel_id for s in f.bot.sent] == [_CHI_GM_CHANNEL_ID]
            assert "couldn't be posted in full" in interaction.followup.sent[0]["content"]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_gm_channel_that_refuses_the_ping_does_not_stop_the_public_record():
    """The independence claim from the ping's side, and the one that needs a
    real Discord failure rather than a missing row: the GM channel exists,
    the bot can reach it, and the send itself fails. Without each half being
    wrapped on its own, that failure would take the `#games` post -- the one
    everybody else in the league reads -- down with it."""
    pool = await get_pool()
    try:
        f = await _build(pool, gm_channels_fail=True)
        try:
            interaction = await _draft(f, code="TOR", prospect=0, actor=_TOR_MANAGER)

            assert [s.channel_id for s in f.bot.sent] == [_GAMES_CHANNEL_ID]
            content = interaction.followup.sent[0]["content"]
            assert "TOR select Prospect Number0" in content
            assert "couldn't be posted in full" in content
            assert "no change was made" not in content.lower()
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_games_channel_that_refuses_the_message_never_raises():
    """A Discord failure on the public post is a logged warning carrying
    `describe_discord_failure`'s reason -- never an exception reaching a
    person whose pick already committed.

    Driven straight at `draft_feed` rather than through the cog, because
    the cog's own `run_after_commit` would catch a raise and the test would
    then pass against a feed that raises -- the thing this pins."""
    pool = await get_pool()
    try:
        f = await _build(pool, games_channel_fails=True)
        try:
            interaction = await _draft(f, code="TOR", prospect=0, actor=_TOR_MANAGER)

            assert [s.channel_id for s in f.bot.sent] == [_CHI_GM_CHANNEL_ID]
            content = interaction.followup.sent[0]["content"]
            assert "TOR select Prospect Number0" in content
            assert "couldn't be posted in full" in content

            report = await draft_feed.notify_on_the_clock(
                f.bot,
                pool,
                league_id=f.league_id,
                season=_SEASON,
                outcome=gm_service.DraftCycleOutcome(state=gm_service.DraftCycleState.COMPLETE),
                human_pick=draft_feed.AnnouncedPick(
                    team_id=f.teams["TOR"], player_id=f.prospects[0], pick_number=30, round=1
                ),
            )
            assert report.posted is False and report.complete is False
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_finished_draft_pings_nobody_and_says_nothing_about_being_paused():
    """`COMPLETE` has no manager to ping and is not paused -- it is over. The
    public record still gets the picks (the draft's history is not
    conditional on somebody being next), and nothing else: this exact-
    equality assertion is what keeps M2's paused line scoped to the states
    that really are stuck."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            # Drive the feed directly with a finished cycle: the command-level
            # path to COMPLETE is covered in test_draft_command.py, and what
            # matters here is that no ping is attempted.
            outcome = gm_service.DraftCycleOutcome(state=gm_service.DraftCycleState.COMPLETE)
            report = await draft_feed.notify_on_the_clock(
                f.bot,
                await get_pool(),
                league_id=f.league_id,
                season=_SEASON,
                outcome=outcome,
                human_pick=draft_feed.AnnouncedPick(
                    team_id=f.teams["TOR"],
                    player_id=f.prospects[0],
                    pick_number=30,
                    round=1,
                    by_manager=True,
                ),
            )

            assert report.ping_owed is False and report.complete is True
            assert [s.channel_id for s in f.bot.sent] == [_GAMES_CHANNEL_ID]
            assert f.bot.sent[0].content == (
                "**Round 1, pick 30:** TOR select Prospect Number0 (manager's pick)."
            )
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_stalled_cycle_tells_the_league_the_draft_is_paused_and_why():
    """Round-1 critic's M2, and a DELIBERATE change to what this file used to
    assert: a `STALLED` cycle posted its picks and then said nothing, so the
    public record showed a normal-looking draft that had simply stopped --
    with nobody on the clock, nobody pinged, and no way for a league reading
    `#games` to know it needed the commissioner. One short line now follows
    the picks, inside the SAME per-cycle message, with no mention in it."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            outcome = gm_service.DraftCycleOutcome(
                state=gm_service.DraftCycleState.STALLED,
                reason=gm_service.DraftCycleReason.POOL_EXHAUSTED,
            )
            report = await draft_feed.notify_on_the_clock(
                f.bot,
                pool,
                league_id=f.league_id,
                season=_SEASON,
                outcome=outcome,
                human_pick=draft_feed.AnnouncedPick(
                    team_id=f.teams["TOR"],
                    player_id=f.prospects[0],
                    pick_number=30,
                    round=1,
                    by_manager=True,
                ),
            )

            assert report.ping_owed is False and report.posted is True
            assert [s.channel_id for s in f.bot.sent] == [_GAMES_CHANNEL_ID]
            post = f.bot.sent[0]
            assert post.content == (
                "**Round 1, pick 30:** TOR select Prospect Number0 (manager's pick).\n"
                "**The draft is paused** -- no draft-eligible prospects remain, so the "
                "commissioner needs to add a draft class."
            )
            assert "<@" not in post.content
            assert len(post.content) <= 2000
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_skipped_resume_is_public_too_and_names_its_own_reason():
    """The other half of M2, and the one that matters most in practice: the
    resume is best-effort, so a lost lock or a failed resume stops the draft
    with every pick before it looking perfectly normal. The `#games` line
    says which reason it was -- the person who picked gets the same fact in
    their own (ephemeral) reply, and nobody else would otherwise ever see
    it."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            outcome = gm_service.DraftCycleOutcome(
                state=gm_service.DraftCycleState.SKIPPED,
                reason=gm_service.DraftCycleReason.RESUME_FAILED,
            )
            await draft_feed.notify_on_the_clock(
                f.bot,
                pool,
                league_id=f.league_id,
                season=_SEASON,
                outcome=outcome,
                human_pick=draft_feed.AnnouncedPick(
                    team_id=f.teams["TOR"],
                    player_id=f.prospects[0],
                    pick_number=30,
                    round=1,
                    by_manager=True,
                ),
            )

            post = f.bot.sent[0]
            assert post.content.startswith("**Round 1, pick 30:** TOR select")
            # DELIBERATELY reworded for round 2's minor 3: this line used to
            # end "run `/draft` again or ask the commissioner to advance the
            # draft", and `#games` is read by everybody -- the next pick
            # after a stopped resume usually belongs to an AI team, so
            # almost nobody reading it could act on the first half.
            assert post.content.endswith(
                "**The draft is paused** -- the AI could not continue (the error is "
                "logged), so ask the commissioner to continue the draft."
            )
            assert "/draft" not in post.content
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_claim_held_cycle_reports_an_advance_in_games_not_a_pause():
    """Round-2 critic's minor 5, at the feed. `SKIPPED(claim_held)` means a
    commissioner's press is advancing the league RIGHT NOW and the draft
    continues from it -- so the public line must not tell the league the
    draft has stopped and must not send anybody looking for the person who
    is already doing something about it."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            outcome = gm_service.DraftCycleOutcome(
                state=gm_service.DraftCycleState.SKIPPED,
                reason=gm_service.DraftCycleReason.CLAIM_HELD,
            )
            await draft_feed.notify_on_the_clock(
                f.bot,
                pool,
                league_id=f.league_id,
                season=_SEASON,
                outcome=outcome,
                human_pick=draft_feed.AnnouncedPick(
                    team_id=f.teams["TOR"],
                    player_id=f.prospects[0],
                    pick_number=30,
                    round=1,
                    by_manager=True,
                ),
            )

            post = f.bot.sent[0]
            assert post.content.startswith("**Round 1, pick 30:** TOR select")
            assert post.content.endswith(
                "**The commissioner is advancing the league** -- the draft continues from there."
            )
            assert "paused" not in post.content.lower()
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_pick_whose_resume_failed_is_still_announced_through_the_cog():
    """B2 all the way through the real command: the resume raises, the pick
    stands, and the cog still reaches the announcement -- before that fix
    the cog's own `except` fired first, so this post never happened and
    nobody was ever told. The reply says what happened without claiming the
    pick failed, and `#games` carries the pick plus M2's paused line."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            with patch.object(
                gm_command_service,
                "run_draft_cycle",
                side_effect=RuntimeError("the resume exploded"),
            ):
                interaction = await _draft(f, code="TOR", prospect=0, actor=_TOR_MANAGER)

            public = [s for s in f.bot.sent if s.channel_id == _GAMES_CHANNEL_ID]
            assert len(public) == 1, "the pick was committed and never announced"
            assert "**Round 1, pick 30:** TOR select Prospect Number0 (manager's pick)." in (
                public[0].content
            )
            assert "**The draft is paused**" in public[0].content

            reply = interaction.followup.sent[0]["content"]
            assert reply.startswith("TOR select Prospect Number0")
            assert "the AI couldn't continue" in reply
            assert "no change was made" not in reply.lower()

            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            assert [s.pick_number for s in selections] == [30]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_round_labels_come_from_the_picks_own_season_not_the_leagues():
    """Round-1 critic's minor 8. The cog used to hand `announce_cycle` the
    `league.current_season` it read BEFORE the command deferred -- an
    unlocked read, while the pick itself was written under the league gate
    at `gate.season`. A press that advances the league between the two makes
    them disagree, and the board read that supplies ROUND LABELS then finds
    no picks: every AI line in the post silently loses its round. The pick's
    own row carries the season it was spent in, so that is what the
    announcement uses."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            real_resolve = draft_cog.resolve_league

            async def _a_league_whose_season_moved_on(*args, **kwargs):
                # What an unlocked pre-defer read is worth: the object the
                # cog holds can disagree with the season the pick is
                # actually spent in. The service re-resolves the league
                # under the gate, so the pick still lands in _SEASON.
                league = await real_resolve(*args, **kwargs)
                return dataclasses.replace(league, current_season=_SEASON + 1)

            with patch.object(
                draft_cog, "resolve_league", side_effect=_a_league_whose_season_moved_on
            ):
                await _draft(f, code="TOR", prospect=0, actor=_TOR_MANAGER)

            public = [s for s in f.bot.sent if s.channel_id == _GAMES_CHANNEL_ID]
            assert len(public) == 1
            assert "**Round 2, pick 31:** BOS select " in public[0].content, (
                "the AI's pick lost its round label -- the announcement read the "
                "league's season, not the pick's"
            )
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_pick_number_two_rows_disagree_about_gets_no_round_rather_than_a_wrong_one():
    """Round-1 critic's minor 6. `draft_picks` has no unique constraint on
    `(league_id, season, pick_number)` (migration 004 -- only `drafts` and
    `draft_selections` carry one), so two rows really can claim the same
    pick number, and the round map built from them used to keep whichever
    row came last. `DraftPickResult` carries a pick number and no pick id,
    so there is no id to key the map by; the honest answer is to drop the
    ambiguous number and let the line degrade to `Pick 31:`."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            await draft_repo.create_draft_pick(
                pool,
                league_id=f.league_id,
                season=_SEASON,
                round=9,
                original_team_id=f.teams["BOS"],
                current_team_id=f.teams["BOS"],
                pick_number=31,
            )

            rounds = await draft_feed._rounds_by_pick_number(pool, f.league_id, _SEASON)

            assert 31 not in rounds, "a pick number two rows disagree about got a round anyway"
            assert rounds[30] == 1 and rounds[32] == 2, "the unambiguous ones are still mapped"
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_feed_refuses_to_run_inside_an_open_transaction():
    """P5-14, at runtime: a caller that hands in a connection with a
    transaction open would put a Discord call inside it."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            from dba2.data.db import transaction

            outcome = gm_service.DraftCycleOutcome(state=gm_service.DraftCycleState.COMPLETE)
            async with transaction(pool) as conn:
                raised = False
                try:
                    await draft_feed.notify_on_the_clock(
                        f.bot, conn, league_id=f.league_id, season=_SEASON, outcome=outcome
                    )
                except ValueError as exc:
                    raised = "P5-14" in str(exc)
                assert raised, "a Discord call was allowed inside an open transaction"
            assert f.bot.sent == []
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()
