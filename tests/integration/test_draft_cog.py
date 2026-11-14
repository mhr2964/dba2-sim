"""Live I/O proof for `dba2/bot/cogs/draft.py` -- `/draft`'s Discord half,
driven through the real command callback against real Postgres with
`test_roster_cog.py`'s fake-interaction pattern (docs/design/phase-5-stage-
d-prime-draft.md §4 commit 6).

**What a cog test can and cannot say here.** It can say that every reply is
ephemeral and that the DEFER is ephemeral too, that `defer()` happens before
the write rather than after it, that a second press is told who is actually
on the clock instead of drafting twice, and that the autocomplete answers
`[]` rather than raising. It cannot say anything about a live gateway:
nothing in this stage has run against one (§6 risk 5), and these fakes are
in-process objects that record what was sent.

**The ephemeral defer is round-1's M1 and a deviation from the design**
(which called for a public success reply): a followup after a public defer
is itself public, so a public defer in front of an ephemeral refusal left a
public "thinking..." placeholder nothing ever replaced. The league's record
of a pick is the `#games` announcement (`test_draft_feed.py`), which is
posted exactly once per cycle.

The one behaviour worth naming, because it is easy to get backwards:
**`SKIPPED` is not an error.** The pick committed and only the AI's resume
did not, so it is a success reply that says so, not a refusal -- covered at
the service level in `test_draft_command.py`, where the races that produce
it can be made deterministic.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it --
same requirement as test_smoke.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
import random
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from unittest.mock import patch

import discord

from dba2.bot.cogs import draft as draft_cog
from dba2.bot.context import NoLeagueForGuild
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
from dba2.services import gm_service
from tests.fixtures.managers import birth_date_for_age
from tests.fixtures.sim import make_ratings

_SEASON = 2026
_COMMISSIONER = 1
_TOR_MANAGER = 101
_CHI_MANAGER = 202
_A_STRANGER = 303

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


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeResponse:
    """Every method suspends before it records (round-1 critic's minor on
    these fakes, `test_draft_feed.py`'s own rule carried here): a fake that
    cannot suspend hides any interleaving a real `await` would expose, so a
    test can pass against code that is only correct when nothing yields."""

    def __init__(self) -> None:
        self.deferred = False
        self.deferred_ephemeral: bool | None = None
        self.deferred_at: int | None = None
        self._done = False
        self.sent_messages: list[dict] = []

    async def defer(self, *, ephemeral: bool = False) -> None:
        await asyncio.sleep(0)
        self.deferred = True
        self.deferred_ephemeral = ephemeral
        self._done = True

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, message: str, *, ephemeral: bool = False) -> None:
        await asyncio.sleep(0)
        self._done = True
        self.sent_messages.append({"content": message, "ephemeral": ephemeral})


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(
        self,
        content: str | None = None,
        *,
        embed=None,
        ephemeral: bool = False,
        allowed_mentions=None,
        **kwargs,
    ):
        await asyncio.sleep(0)
        self.sent.append(
            {
                "content": content,
                "embed": embed,
                "ephemeral": ephemeral,
                "allowed_mentions": allowed_mentions,
            }
        )


class _FakeChannel:
    """A channel that can take a message -- `get_partial_message` exists
    because `game_feed._resolve_channel` refuses a channel without it."""

    def __init__(self, channel_id: int, log: list) -> None:
        self.id = channel_id
        self._log = log

    async def send(self, content: str = "", *, allowed_mentions=None, **kwargs):
        await asyncio.sleep(0)
        self._log.append((self.id, content))
        return _FakeMessage()

    def get_partial_message(self, message_id: int):
        raise AssertionError("the draft edits nothing; it posts new messages only")


class _FakeMessage:
    id = 7_000_001


@dataclass
class _FakeBot:
    """Enough of a bot for the announcement to really run -- which is what
    makes the cog's CLEAN reply branch (no `honest_suffix` disclosure)
    reachable at all in this file (round-1 critic's minor 1). Every other
    test here passes `bot=None`, so every one of them exercises the
    announcement-failed branch and none of them would notice if the
    disclosure were inverted."""

    channels: dict[int, _FakeChannel] = dc_field(default_factory=dict)
    sent: list = dc_field(default_factory=list)

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        await asyncio.sleep(0)
        channel = self.channels.get(channel_id)
        if channel is None:
            raise RuntimeError(f"no channel {channel_id}")
        return channel


class _FakeInteraction:
    def __init__(self, *, guild_id: int | None, user_id: int) -> None:
        self.guild_id = guild_id
        self.guild = None
        self.user = _FakeUser(user_id)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.command = "draft"
        self.id = random.randint(1, 10**9)

    @property
    def reply(self) -> str:
        """The single message this interaction produced, wherever it went."""
        sent = [*self.response.sent_messages, *self.followup.sent]
        assert len(sent) == 1, f"expected exactly one reply, got {sent}"
        return sent[0]["content"] or ""

    @property
    def reply_was_ephemeral(self) -> bool:
        sent = [*self.response.sent_messages, *self.followup.sent]
        assert len(sent) == 1, f"expected exactly one reply, got {sent}"
        return bool(sent[0]["ephemeral"])


@dataclass(frozen=True)
class _Fixture:
    league_id: int
    guild_id: int
    draft_id: int
    teams: dict[str, int]
    prospects: tuple[int, ...]


async def _build(
    pool,
    *,
    picks: tuple[tuple[int, int | None, str], ...] = (
        (1, 30, "TOR"),
        (2, 31, "BOS"),
        (2, 32, "CHI"),
    ),
    prospects: int = 3,
    phase: Phase = Phase.DRAFT,
    ranked: bool = False,
) -> _Fixture:
    """TOR (a person), BOS (AI, so the resume has work) and CHI (the next
    person, so the cycle has somewhere to stop). `ranked=True` gives the
    prospects `draft_classes` rows, which is the only thing the autocomplete
    orders by."""
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Draft Cog Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=2026_10,
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
    draft = await draft_repo.create_draft(
        pool, league_id=league.id, season=_SEASON, total_picks=len(picks)
    )
    for round_, pick_number, code in picks:
        await draft_repo.create_draft_pick(
            pool,
            league_id=league.id,
            season=_SEASON,
            round=round_,
            original_team_id=teams[code],
            current_team_id=teams[code],
            pick_number=pick_number,
        )
    prospect_ids = []
    for i in range(prospects):
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
        if ranked:
            await draft_repo.create_draft_class_entry(
                pool,
                league_id=league.id,
                class_year=_SEASON,
                player_id=player.id,
                mock_rank=i + 1,
            )
        prospect_ids.append(player.id)
    return _Fixture(
        league_id=league.id,
        guild_id=league.discord_guild_id,
        draft_id=draft.id,
        teams=teams,
        prospects=tuple(prospect_ids),
    )


async def _cleanup(pool, f: _Fixture) -> None:
    gm_service.artifact_path(f.league_id, _SEASON).unlink(missing_ok=True)
    await pool.execute("DELETE FROM leagues WHERE id = $1", f.league_id)


def _cog() -> draft_cog.DraftCommands:
    return draft_cog.DraftCommands(bot=None)


# ---------------------------------------------------------------------------
# The command.
# ---------------------------------------------------------------------------


async def test_a_pick_defers_ephemerally_and_answers_with_who_is_next():
    """`defer()` before the write, because the pick plus the AI resume is
    past Discord's three-second budget -- and EPHEMERALLY (round-1 critic's
    M1): a followup after a public defer is itself public, so a public defer
    in front of an ephemeral refusal leaves a public "thinking..."
    placeholder nothing ever replaces. The league's record of the pick is
    the `#games` announcement, not this reply. The sentence names the next
    person's team, which is what makes the draft move without a
    commissioner press."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            cog = _cog()

            await cog.draft.callback(cog, interaction, "TOR", f.prospects[0])

            assert interaction.response.deferred is True
            assert interaction.response.deferred_ephemeral is True
            assert interaction.reply_was_ephemeral is True
            reply = interaction.reply
            assert reply.startswith("TOR select Prospect Number")
            assert "round 1, pick 30" in reply
            assert "CHI are on the clock -- round 2, pick 32." in reply
            assert "The AI made 1 pick behind you." in reply
            assert "couldn't be looked up" not in reply

            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            assert [s.pick_number for s in selections] == [30, 31]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_write_never_runs_before_the_response_is_deferred():
    """The ordering itself, not just its end state: `defer()` must already
    have happened when the service is entered, or Discord has timed the
    interaction out by the time the pick returns."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            cog = _cog()
            seen: list[bool] = []
            real = draft_cog.gm_command_service.draft_pick

            async def _recording_draft_pick(*args, **kwargs):
                seen.append(interaction.response.deferred)
                return await real(*args, **kwargs)

            with patch.object(
                draft_cog.gm_command_service, "draft_pick", side_effect=_recording_draft_pick
            ):
                await cog.draft.callback(cog, interaction, "TOR", f.prospects[0])

            assert seen == [True], "the service ran before defer() -- the interaction can expire"
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_command_is_refused_ephemerally_outside_the_draft_phase():
    """The bot-layer gate fires BEFORE `defer()`, so the refusal is an
    ephemeral `send_message` and nothing is deferred -- P5-17's own reason
    for `COMMAND_GATES` existing at this layer as well as the service's."""
    pool = await get_pool()
    try:
        f = await _build(pool, phase=Phase.POST_DRAFT_TRADES)
        try:
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            cog = _cog()

            await cog.draft.callback(cog, interaction, "TOR", f.prospects[0])

            assert interaction.response.deferred is False
            assert interaction.reply_was_ephemeral is True
            assert "/draft is not available during POST_DRAFT_TRADES" in interaction.reply
            assert await draft_repo.list_draft_selections_for_draft(pool, f.draft_id) == []
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_someone_elses_team_is_refused_ephemerally_and_writes_nothing():
    """P5-38's bot-layer half: a person who manages no team here is told so
    immediately, before any transaction is opened."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_A_STRANGER)
            cog = _cog()

            await cog.draft.callback(cog, interaction, "TOR", f.prospects[0])

            assert interaction.response.deferred is False
            assert interaction.reply_was_ephemeral is True
            assert await draft_repo.list_draft_selections_for_draft(pool, f.draft_id) == []
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_an_unknown_team_code_is_refused_before_anything_is_deferred():
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            cog = _cog()

            await cog.draft.callback(cog, interaction, "ZZZ", f.prospects[0])

            assert interaction.response.deferred is False
            assert "No team with code 'ZZZ' in this league." in interaction.reply
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_commissioner_may_pick_for_a_human_managed_team_on_the_clock():
    """P5-48's override, all the way through the command -- the same path,
    a different `actor_user_id`, and no second command."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_COMMISSIONER)
            cog = _cog()

            await cog.draft.callback(cog, interaction, "TOR", f.prospects[0])

            assert interaction.reply_was_ephemeral is True
            assert interaction.reply.startswith("TOR select Prospect Number")
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_second_press_is_told_who_is_actually_on_the_clock():
    """The double-press. The first press's resume moved the clock to CHI, so
    the second is refused with the service's own sentence -- ephemerally,
    and with no second selection written.

    This is the refusal that arrives AFTER `defer()`, which is what M1 is
    about: the followup carrying it can only be ephemeral if the defer was,
    so both are asserted here."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            cog = _cog()
            first = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            await cog.draft.callback(cog, first, "TOR", f.prospects[0])

            second = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            await cog.draft.callback(cog, second, "TOR", f.prospects[2])

            assert second.response.deferred_ephemeral is True
            assert second.reply_was_ephemeral is True
            assert second.reply == "CHI is on the clock -- round 2, pick 32."
            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            assert [s.pick_number for s in selections] == [30, 31]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_stalled_resume_still_reports_a_committed_pick():
    """`STALLED` is not a failure of the command: the person's pick landed,
    and the sentence says why nothing followed it -- naming the fix (a draft
    class) rather than shrugging."""
    pool = await get_pool()
    try:
        f = await _build(pool, picks=((1, 1, "TOR"), (1, 2, "BOS")), prospects=1)
        try:
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            cog = _cog()

            await cog.draft.callback(cog, interaction, "TOR", f.prospects[0])

            assert interaction.reply_was_ephemeral is True
            assert "No draft-eligible prospects remain" in interaction.reply
            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            assert [s.pick_number for s in selections] == [1]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_prospect_with_an_unbounded_name_still_gets_a_reply():
    """Round-1 critic's blocking issue B3, through the real callback:
    `players.last_name` is `TEXT` and nothing between the row and the embed
    bounded it, so a 300-character name made `Panel.__post_init__` raise
    over its 256-character title limit -- after the pick had committed, and
    outside every never-raising wrapper the cog has. The person's pick
    landed and they got no reply at all."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            await pool.execute(
                "UPDATE players SET last_name = $1 WHERE id = $2", "Q" * 300, f.prospects[0]
            )
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            cog = _cog()

            await cog.draft.callback(cog, interaction, "TOR", f.prospects[0])

            assert interaction.reply.startswith("TOR select Prospect QQQ")
            assert len(interaction.reply) <= 2000
            sent = [*interaction.response.sent_messages, *interaction.followup.sent]
            assert sent[0].get("embed") is not None, (
                "the long name cost the panel, not just the raise"
            )
            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            assert [s.pick_number for s in selections] == [30, 31]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_reply_that_cannot_be_built_falls_back_to_plain_text():
    """The other half of B3: clipping fixes the one overflow we found, and
    the wrapper is what makes the NEXT one survivable. Everything after the
    commit is a follow-up, so a panel that cannot be built or sent degrades
    to a plain-text followup and never reaches the person as a failure."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            cog = _cog()

            with patch.object(
                draft_cog, "panel_to_embed", side_effect=RuntimeError("the embed exploded")
            ):
                await cog.draft.callback(cog, interaction, "TOR", f.prospects[0])

            sent = [*interaction.response.sent_messages, *interaction.followup.sent]
            assert len(sent) == 1
            assert sent[0].get("embed") is None
            assert sent[0]["content"].startswith("TOR select Prospect Number")
            assert "no change was made" not in sent[0]["content"].lower()
            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            assert [s.pick_number for s in selections] == [30, 31]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_send_that_reached_discord_and_then_raised_is_not_sent_twice():
    """Round-2 critic's minor 1. The fallback used to fire on ANY exception
    from the first attempt, including one raised by `followup.send` itself
    -- and a `send` that reaches Discord and then times out waiting for the
    response has already posted the message. The person then got the
    confirmation twice. The panel is built BEFORE the one and only send
    attempt now, so the fallback covers the case it was written for (an
    embed that cannot be built at all) and nothing else."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            cog = _cog()
            real_send = interaction.followup.send

            async def _record_then_raise(*args, **kwargs):
                await real_send(*args, **kwargs)
                raise TimeoutError("Discord took the message and then stopped answering")

            interaction.followup.send = _record_then_raise

            await cog.draft.callback(cog, interaction, "TOR", f.prospects[0])

            assert len(interaction.followup.sent) == 1, (
                "the message that already reached Discord was posted a second time"
            )
            assert interaction.followup.sent[0]["embed"] is not None
            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            assert [s.pick_number for s in selections] == [30, 31]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_a_failed_name_lookup_never_claims_the_pick_did_not_happen():
    """Every read after the write is a follow-up: a failure degrades the
    prospect's NAME to an id and says so, and never produces the "no change
    was made" wording a real failure gets (Stage C's B6 shape)."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            cog = _cog()

            # `player_repo` is one shared module object, and the service's
            # own pre-write validation read goes through it too -- patching
            # it naively would fail the PICK and never reach the follow-up
            # this test is about. Same flaky-wrapper technique
            # `test_roster_cog.py` uses: the first call (the validation) is
            # real, everything after it (the cog's own post-write re-fetch)
            # raises.
            real_get_player = player_repo.get_player
            calls = {"n": 0}

            async def _flaky_get_player(ex, pid):
                calls["n"] += 1
                if calls["n"] == 1:
                    return await real_get_player(ex, pid)
                raise RuntimeError("the player read exploded")

            with patch.object(draft_cog.player_repo, "get_player", side_effect=_flaky_get_player):
                await cog.draft.callback(cog, interaction, "TOR", f.prospects[0])

            reply = interaction.reply
            assert reply.startswith(f"TOR select player {f.prospects[0]} -- round 1, pick 30.")
            assert "couldn't be looked up" in reply
            assert "no change was made" not in reply.lower()
            selections = await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            assert [s.pick_number for s in selections] == [30, 31]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


_GAMES_CHANNEL_ID = 910_001
_CHI_GM_CHANNEL_ID = 910_002


async def test_a_pick_whose_announcement_lands_discloses_nothing_and_pings_nobody_itself():
    """Round-1 critic's minors 1 and 2, together, because they are one
    reply: with the channels registered and a bot that can reach them, the
    announcement really runs, so the CLEAN branch of the reply is exercised
    -- no `honest_suffix` disclosure at all, which every other test in this
    file (they pass `bot=None`) can never see. And the reply itself carries
    `AllowedMentions.none()`: the GM-channel ping is the only message in
    this whole command allowed to mention anybody."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            bot = _FakeBot()
            await channel_repo.create_league_channel(
                pool,
                league_id=f.league_id,
                purpose=ChannelPurpose.GAMES,
                channel_id=_GAMES_CHANNEL_ID,
            )
            bot.channels[_GAMES_CHANNEL_ID] = _FakeChannel(_GAMES_CHANNEL_ID, bot.sent)
            await channel_repo.create_gm_channel(
                pool,
                league_id=f.league_id,
                team_id=f.teams["CHI"],
                channel_id=_CHI_GM_CHANNEL_ID,
            )
            bot.channels[_CHI_GM_CHANNEL_ID] = _FakeChannel(_CHI_GM_CHANNEL_ID, bot.sent)
            cog = draft_cog.DraftCommands(bot=bot)
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)

            await cog.draft.callback(cog, interaction, "TOR", f.prospects[0])

            reply = interaction.followup.sent[0]
            assert "The change went through, but" not in (reply["content"] or ""), (
                "the announcement succeeded, so the reply must disclose nothing"
            )
            assert "couldn't be posted" not in reply["content"]
            assert "couldn't be looked up" not in reply["content"]
            mentions = reply["allowed_mentions"]
            assert isinstance(mentions, discord.AllowedMentions)
            assert (mentions.everyone, mentions.roles, mentions.users) == (False, False, False)
            assert [channel_id for channel_id, _ in bot.sent] == [
                _CHI_GM_CHANNEL_ID,
                _GAMES_CHANNEL_ID,
            ]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# The sentence for where the draft now stands -- every state, every reason
# (round-1 critic's minor 3). `_cycle_text` is the wording half of this cog,
# and only three of its branches were reachable from a command-level test.
# ---------------------------------------------------------------------------


async def test_every_cycle_state_has_its_own_sentence():
    pool = await get_pool()
    try:
        cog = _cog()
        outcome = gm_service.DraftCycleOutcome

        complete, looked_up = await cog._cycle_text(
            pool, outcome(state=gm_service.DraftCycleState.COMPLETE)
        )
        assert complete.strip() == (
            "That was the last pick -- every pick in this draft has now been made."
        )
        assert looked_up is True

        stalled_pool, _ = await cog._cycle_text(
            pool,
            outcome(
                state=gm_service.DraftCycleState.STALLED,
                reason=gm_service.DraftCycleReason.POOL_EXHAUSTED,
            ),
        )
        assert "No draft-eligible prospects remain" in stalled_pool

        stalled_lottery, _ = await cog._cycle_text(
            pool,
            outcome(
                state=gm_service.DraftCycleState.STALLED,
                reason=gm_service.DraftCycleReason.UNRESOLVED_PICKS,
            ),
        )
        assert "the lottery hasn't finished" in stalled_lottery
    finally:
        await close_pool()


async def test_every_skipped_reason_has_a_sentence_and_none_claims_the_pick_failed():
    """EVERY `DraftCycleReason` member, iterated from the enum itself
    (round-2 critic's minor 2) rather than the four this cog expects to see:
    a reason added service-side and forgotten here used to fall through to
    "the commissioner is advancing the league", which is a specific claim
    about a specific situation and would simply have been false. The
    fallback is generic and honest now, and this loop is what notices when
    a new member needs its own sentence."""
    pool = await get_pool()
    try:
        cog = _cog()
        reason = gm_service.DraftCycleReason

        sentences = {}
        for member in reason:
            text, looked_up = await cog._cycle_text(
                pool,
                gm_service.DraftCycleOutcome(
                    state=gm_service.DraftCycleState.SKIPPED, reason=member
                ),
            )
            assert looked_up is True
            assert text.strip(), f"{member} produced no sentence at all"
            assert "no change was made" not in text.lower()
            sentences[member] = text

        # The four a SKIPPED cycle can really carry each say their own
        # thing; the two that belong to STALLED fall to the generic line,
        # which is the same one a member nobody has worded yet gets.
        specific = (
            reason.CLAIM_HELD,
            reason.PHASE_MOVED,
            reason.RESUME_CONTENDED,
            reason.RESUME_FAILED,
        )
        assert len({sentences[m] for m in specific}) == 4, (
            f"two SKIPPED reasons share a sentence: {sentences}"
        )
        assert "commissioner is advancing the league" in sentences[reason.CLAIM_HELD]
        assert "left the draft" in sentences[reason.PHASE_MOVED]
        for member in (reason.RESUME_CONTENDED, reason.RESUME_FAILED):
            assert "Your pick stands" in sentences[member]
            # Minor 3: "run /draft again" is unactionable after a failed
            # resume -- the next pick is usually an AI team's, and that
            # person cannot make it.
            assert "/draft" not in sentences[member]
            assert "ask the commissioner" in sentences[member]

        generic, _ = await cog._cycle_text(
            pool, gm_service.DraftCycleOutcome(state=gm_service.DraftCycleState.SKIPPED)
        )
        assert "Your pick stands" in generic
        assert "commissioner is advancing the league" not in generic
        for member in (reason.POOL_EXHAUSTED, reason.UNRESOLVED_PICKS):
            assert sentences[member] == generic, (
                f"{member} is not a SKIPPED reason, so it must get the generic line"
            )
    finally:
        await close_pool()


async def test_the_on_the_clock_sentence_degrades_to_an_id_when_the_team_cannot_be_read():
    """`WAITING_ON_HUMAN` is the only branch that needs a second read, so it
    is the only one that can half-fail: the sentence still names the round
    and the pick, and the caller is told the lookup did not run."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            cog = _cog()
            cycle = gm_service.DraftCycleOutcome(
                state=gm_service.DraftCycleState.WAITING_ON_HUMAN,
                team_id=f.teams["CHI"],
                round=2,
                pick_number=32,
            )

            text, looked_up = await cog._cycle_text(pool, cycle)
            assert text == " CHI are on the clock -- round 2, pick 32."
            assert looked_up is True

            with patch.object(
                draft_cog.team_repo, "get_team", side_effect=RuntimeError("the team read exploded")
            ):
                degraded, looked_up = await cog._cycle_text(pool, cycle)
            assert degraded == f" team {f.teams['CHI']} are on the clock -- round 2, pick 32."
            assert looked_up is False
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# The autocomplete -- the codebase's first, and non-authoritative.
# ---------------------------------------------------------------------------


async def test_the_autocomplete_offers_the_board_in_mock_rank_order():
    """Name, position and mock rank only -- never a rating (§7 L11), and
    ordered by the one board both a human and the AI may legitimately see."""
    pool = await get_pool()
    try:
        f = await _build(pool, ranked=True)
        try:
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            cog = _cog()

            choices = await cog.draft_player_autocomplete(interaction, "")

            assert [c.value for c in choices] == list(f.prospects)
            assert choices[0].name == "#1 Prospect Number0 (SF)"
            assert all("70" not in c.name for c in choices), "a rating reached the dropdown"
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_autocomplete_drops_a_prospect_the_draft_has_already_taken():
    """A drafted prospect stays `DRAFT_ELIGIBLE` on no roster (§7 L1), so the
    board has to exclude him by `draft_selections` -- the same per-draft
    scope the applier's own taken-set filter uses."""
    pool = await get_pool()
    try:
        f = await _build(pool, ranked=True)
        try:
            cog = _cog()
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            await cog.draft.callback(cog, interaction, "TOR", f.prospects[0])

            choices = await cog.draft_player_autocomplete(
                _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER), ""
            )

            taken = {
                s.player_id
                for s in await draft_repo.list_draft_selections_for_draft(pool, f.draft_id)
            }
            assert taken, "the fixture drafted nobody, so this proves nothing"
            assert not (taken & {c.value for c in choices})
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_autocomplete_filters_by_what_has_been_typed():
    pool = await get_pool()
    try:
        f = await _build(pool, ranked=True)
        try:
            cog = _cog()
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)

            assert [
                c.value for c in await cog.draft_player_autocomplete(interaction, "number1")
            ] == [f.prospects[1]]
            assert await cog.draft_player_autocomplete(interaction, "nobody") == []
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_autocomplete_treats_wildcards_as_characters_somebody_typed():
    """Round-1 critic's minor 9. `query` is bound as a parameter (no
    injection), but it is concatenated with `'%'` INSIDE the SQL and fed to
    `ILIKE`, where `%`, `_` and `\\` are wildcards. So `Numb%` matched every
    prospect, `Numbe_0` matched `Number0`, and a person typing a name with
    an underscore in it got a dropdown that ignored the underscore. They are
    characters somebody typed, not a pattern language the dropdown offers.

    **Driven at the repository, not at the autocomplete** (round-2 critic's
    minor 6). The cog's callback answers `[]` for ANY exception, by design
    -- so an empty dropdown proves the wildcard was escaped or proves the
    SQL is broken, and the assertion cannot tell which. `list_available_
    prospects` raises, so an `ESCAPE` clause that stopped parsing would fail
    this test instead of passing it. The cog's own path is exercised by the
    control below, which must still return the whole board."""
    pool = await get_pool()
    try:
        f = await _build(pool, ranked=True)
        try:

            async def _board(query: str):
                return [
                    p.player_id
                    for p in await draft_repo.list_available_prospects(
                        pool, league_id=f.league_id, season=_SEASON, query=query
                    )
                ]

            # The control: the same statement, the same code path, a prefix
            # with no metacharacter in it. If the SQL were broken this would
            # raise rather than quietly return nothing.
            assert await _board("Numb") == list(f.prospects)

            assert await _board("Numb%") == []
            assert await _board("Numbe_0") == []
            assert await _board("Number\\") == []
            assert await _board("Number1") == [f.prospects[1]]

            # And through the cog, where an exception would be swallowed --
            # so this one is only meaningful as a positive.
            cog = _cog()
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)
            assert [
                c.value for c in await cog.draft_player_autocomplete(interaction, "Number1")
            ] == [f.prospects[1]]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_autocomplete_finds_a_name_that_really_contains_a_wildcard():
    """The other side of minor 9: an escaped `%` must still MATCH a literal
    `%` in a name -- escaping that only ever excludes is a different bug."""
    pool = await get_pool()
    try:
        f = await _build(pool, ranked=True)
        try:
            await pool.execute(
                "UPDATE players SET last_name = $1 WHERE id = $2", "Per%cent", f.prospects[2]
            )
            cog = _cog()
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)

            assert [c.value for c in await cog.draft_player_autocomplete(interaction, "Per%c")] == [
                f.prospects[2]
            ]
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()


async def test_the_autocomplete_answers_an_empty_list_for_a_guild_with_no_league():
    """Discord gives the callback about three seconds and shows an empty
    dropdown either way, so `[]` is the explicit path rather than the
    accidental one."""
    await get_pool()  # the callback opens one itself; this keeps the fixture shape
    try:
        cog = _cog()
        interaction = _FakeInteraction(guild_id=None, user_id=_TOR_MANAGER)
        assert await cog.draft_player_autocomplete(interaction, "") == []
    finally:
        await close_pool()


async def test_the_autocomplete_answers_an_empty_list_when_anything_else_raises():
    """Not only `NoLeagueForGuild`: a board read that fails for any reason
    at all must not reach Discord as an exception."""
    pool = await get_pool()
    try:
        f = await _build(pool)
        try:
            cog = _cog()
            interaction = _FakeInteraction(guild_id=f.guild_id, user_id=_TOR_MANAGER)

            with patch.object(
                draft_cog.view_service,
                "get_draft_board",
                side_effect=RuntimeError("the board read exploded"),
            ):
                assert await cog.draft_player_autocomplete(interaction, "") == []

            # And the guild-less case really does go through its own branch.
            with patch.object(
                draft_cog, "resolve_league", side_effect=NoLeagueForGuild("no league")
            ):
                assert await cog.draft_player_autocomplete(interaction, "") == []
        finally:
            await _cleanup(pool, f)
    finally:
        await close_pool()
