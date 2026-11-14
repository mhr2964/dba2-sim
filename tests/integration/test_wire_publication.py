"""Per-story wire publication (phase-5-stage-e-advance.md P5-99.s.1, P5-99.s.3,
P5-99.m.1, F17, build-plan commit 11), over real rows and a fake Discord channel.

Publication state is its own fact, separate from "the media selection finished":
`media_stories.posted_at`, written one story at a time by `surfaces.post_new_wire_
stories` through `media_service.mark_wire_story_posted` immediately after that story's
`channel.send` returns. Two facts, two owners: the service never asserts a Discord
fact, and the cog never writes `leagues` -- the mark lives on the story, not on the
league.

The guarantees under test, in the order the design states them:

- **each story is posted once and marked per story** -- a call posts exactly the
  unposted stories and a second call posts none;
- **an interrupted publication resumes at story granularity** -- after k stories the
  next call posts exactly the remainder and re-posts none. Marking a whole CYCLE
  instead loses everything after the first story;
- **a press whose publication never ran** still publishes on a later call (the
  stories are just unposted);
- **a mark that fails does not stop the others** and costs at most one duplicate --
  the disclosed R12 bound, send-then-mark being non-atomic.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import random

import pytest

from dba2.bot import surfaces
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import channel_repo, league_repo, media_repo
from dba2.domain.channel import ChannelPurpose
from dba2.domain.media import StoryKind
from dba2.domain.phase import Phase
from dba2.services import media_service, view_service
from tests.integration._press_fixtures import COMMISSIONER, SEASON, drop

_CHANNEL_ID = 424242


class _FakeChannel:
    """Records what `channel.send` was given. `fail_on_send` (1-based) makes that send
    raise BEFORE recording, as a Discord outage does: nothing was posted."""

    def __init__(self, fail_on_send: int | None = None) -> None:
        self.titles: list[str] = []
        self.fail_on_send = fail_on_send
        self._sends = 0

    async def send(self, *, embed) -> None:
        self._sends += 1
        if self._sends == self.fail_on_send:
            raise RuntimeError("discord is down")
        self.titles.append(embed.description or embed.title)


class _FakeGuild:
    id = 7

    def __init__(self, channel: _FakeChannel) -> None:
        self._channel = channel

    def get_channel(self, channel_id: int):
        return self._channel if channel_id == _CHANNEL_ID else None


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


async def _league_with_a_wire_channel(pool) -> int:
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Wire Test League",
        start_season_year=SEASON,
        current_season=SEASON,
        commissioner_user_id=COMMISSIONER,
        rng_seed=1,
    )
    await channel_repo.create_league_channel(
        pool, league_id=league.id, purpose=ChannelPurpose.WIRE, channel_id=_CHANNEL_ID
    )
    return league.id


async def _story(pool, league_id: int, text: str, *, cycle_index: int = 0):
    return await media_repo.create_media_story(
        pool,
        league_id=league_id,
        season=SEASON,
        phase=Phase.PRESEASON,
        cycle_index=cycle_index,
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


async def _posted_flags(pool, league_id: int) -> dict[str, bool]:
    stories = await media_repo.list_media_stories_for_league_season(
        pool, league_id=league_id, season=SEASON
    )
    return {s.rendered_text: s.posted_at is not None for s in stories}


async def _post(pool, league_id: int, channel: _FakeChannel, **kwargs) -> int:
    return await surfaces.post_new_wire_stories(
        _FakeGuild(channel), pool, league_id=league_id, season=SEASON, **kwargs
    )


# --- the repo and view pieces ----------------------------------------------------------


async def test_the_unposted_read_returns_only_unposted_stories_oldest_cycle_first(pool):
    league_id = await _league_with_a_wire_channel(pool)
    try:
        a = await _story(pool, league_id, "a", cycle_index=0)
        b = await _story(pool, league_id, "b", cycle_index=1)
        c = await _story(pool, league_id, "c", cycle_index=1)
        assert await media_repo.mark_media_story_posted(pool, a.id) is True

        everything = await media_repo.list_unposted_media_stories(
            pool, league_id=league_id, season=SEASON
        )
        one_cycle = await media_repo.list_unposted_media_stories(
            pool, league_id=league_id, season=SEASON, cycle_index=1
        )
        nothing = await media_repo.list_unposted_media_stories(
            pool, league_id=league_id, season=SEASON, cycle_index=0
        )

        assert [s.id for s in everything] == [b.id, c.id]
        assert [s.id for s in one_cycle] == [b.id, c.id]
        assert nothing == []
    finally:
        await drop(pool, league_id)


async def test_the_mark_is_fenced_and_a_second_mark_moves_nothing(pool):
    """`WHERE posted_at IS NULL`: the first mark wins and keeps its timestamp; a second
    (a sweep racing a press) reports False and does not rewrite it."""
    league_id = await _league_with_a_wire_channel(pool)
    try:
        story = await _story(pool, league_id, "a")

        assert await media_service.mark_wire_story_posted(pool, story.id) is True
        first = (await media_repo.get_media_story(pool, story.id)).posted_at
        assert first is not None
        assert await media_service.mark_wire_story_posted(pool, story.id) is False
        assert (await media_repo.get_media_story(pool, story.id)).posted_at == first
    finally:
        await drop(pool, league_id)


async def test_the_unposted_panels_pair_each_panel_with_its_story_id_in_order(pool):
    league_id = await _league_with_a_wire_channel(pool)
    try:
        a = await _story(pool, league_id, "first")
        b = await _story(pool, league_id, "second")
        await media_repo.mark_media_story_posted(pool, a.id)

        paired = await view_service.get_unposted_wire_panels(
            pool, league_id=league_id, season=SEASON
        )

        assert [story_id for story_id, _panel in paired] == [b.id]
        assert paired[0][1].description == "second"
    finally:
        await drop(pool, league_id)


# --- post_new_wire_stories ---------------------------------------------------------------


async def test_each_unposted_story_is_posted_once_and_marked_and_a_second_call_posts_none(pool):
    league_id = await _league_with_a_wire_channel(pool)
    try:
        for text in ("one", "two", "three"):
            await _story(pool, league_id, text)
        channel = _FakeChannel()

        assert await _post(pool, league_id, channel) == 3
        assert await _post(pool, league_id, channel) == 0

        assert len(channel.titles) == 3, "posted once each, never again"
        assert await _posted_flags(pool, league_id) == {"one": True, "two": True, "three": True}
    finally:
        await drop(pool, league_id)


async def test_a_publication_interrupted_after_k_stories_posts_exactly_the_rest_next_time(pool):
    """B2's case: the third send raises (Discord down mid-publication). Stories 1 and 2
    were posted AND marked; 3 and 4 are not. The next call posts exactly 3 and 4 and
    re-posts neither of the first two. Marking the whole cycle up front would make the
    failed call's remainder invisible to every later one -- lost stories."""
    league_id = await _league_with_a_wire_channel(pool)
    try:
        for text in ("one", "two", "three", "four"):
            await _story(pool, league_id, text)
        down = _FakeChannel(fail_on_send=3)

        with pytest.raises(RuntimeError, match="discord is down"):
            await _post(pool, league_id, down)
        assert len(down.titles) == 2
        assert await _posted_flags(pool, league_id) == {
            "one": True,
            "two": True,
            "three": False,
            "four": False,
        }

        up = _FakeChannel()
        assert await _post(pool, league_id, up) == 2

        assert len(up.titles) == 2
        assert "three" in up.titles[0] and "four" in up.titles[1]
        assert all((await _posted_flags(pool, league_id)).values())
    finally:
        await drop(pool, league_id)


async def test_a_press_whose_publication_never_ran_is_published_by_a_later_call(pool):
    """The stories exist (the press's selection finished) but nothing posted them -- the
    process died between the checkpoint and the cog. Nothing is lost: every one is
    unposted, so the next call, of any cycle scope, posts them all."""
    league_id = await _league_with_a_wire_channel(pool)
    try:
        for text in ("a", "b"):
            await _story(pool, league_id, text, cycle_index=3)
        channel = _FakeChannel()

        assert await _post(pool, league_id, channel) == 2

        assert len(channel.titles) == 2
    finally:
        await drop(pool, league_id)


async def test_a_call_scoped_to_a_cycle_posts_only_that_cycles_unposted_stories(pool):
    league_id = await _league_with_a_wire_channel(pool)
    try:
        await _story(pool, league_id, "old", cycle_index=1)
        await _story(pool, league_id, "new", cycle_index=2)
        channel = _FakeChannel()

        assert await _post(pool, league_id, channel, cycle_index=2) == 1

        assert await _posted_flags(pool, league_id) == {"old": False, "new": True}
    finally:
        await drop(pool, league_id)


async def test_a_mark_that_fails_does_not_stop_the_other_stories_and_costs_one_duplicate(
    pool, monkeypatch
):
    """Send-then-mark is not atomic (R12). The mark of story 2 raises: it is logged, the
    rest still post and are marked, and story 2 -- posted but unmarked -- is the one and
    only story a later call posts again. Marking BEFORE sending would turn the same crash
    into a story that never posts at all, which is the failure this design exists to
    remove."""
    league_id = await _league_with_a_wire_channel(pool)
    try:
        stories = [await _story(pool, league_id, t) for t in ("one", "two", "three")]
        real_mark = media_service.mark_wire_story_posted

        async def flaky(ex, story_id):
            if story_id == stories[1].id:
                raise RuntimeError("the mark could not be written")
            return await real_mark(ex, story_id)

        monkeypatch.setattr(media_service, "mark_wire_story_posted", flaky)
        channel = _FakeChannel()

        assert await _post(pool, league_id, channel) == 3
        assert len(channel.titles) == 3
        assert await _posted_flags(pool, league_id) == {"one": True, "two": False, "three": True}

        monkeypatch.setattr(media_service, "mark_wire_story_posted", real_mark)
        again = _FakeChannel()
        assert await _post(pool, league_id, again) == 1
        assert "two" in again.titles[0], "the duplicate is exactly the unmarked story"
    finally:
        await drop(pool, league_id)


async def test_no_wire_channel_registered_is_still_a_named_error(pool):
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="No Wire Channel League",
        start_season_year=SEASON,
        current_season=SEASON,
        commissioner_user_id=COMMISSIONER,
        rng_seed=1,
    )
    try:
        with pytest.raises(RuntimeError, match="no wire channel registered"):
            await surfaces.post_new_wire_stories(
                _FakeGuild(_FakeChannel()), pool, league_id=league.id, season=SEASON
            )
    finally:
        await drop(pool, league.id)


async def test_the_count_is_the_sends_that_completed_through_the_real_sender_and_each_is_marked(
    pool,
):
    """`posted` is what `post_wire_panels` reports for each story's own send -- no stub between
    the two: three stories through a real (fake-channel) sender are three counted, three sent
    and three marked, and a second call finds nothing left."""
    league_id = await _league_with_a_wire_channel(pool)
    try:
        for name in ("one", "two", "three"):
            await _story(pool, league_id, name)
        channel = _FakeChannel()

        assert await _post(pool, league_id, channel) == 3
        assert len(channel.titles) == 3
        assert await _posted_flags(pool, league_id) == {"one": True, "two": True, "three": True}
        assert await _post(pool, league_id, _FakeChannel()) == 0
    finally:
        await drop(pool, league_id)
