"""`/watch` end to end against real Postgres (Stage D commit 10,
docs/design/phase-5-stage-d-broadcast.md §7, §10 Tier 3, §11 row 10).

The producer is `test_game_feed.synthetic_producer` (real `sim_game`, real
`open_block`); the cog, `resolve_league`, `view_service`, `broadcast_repo`, the
scheduler and the block registry are all REAL. Only Discord is fake, and the
scheduler runs on an injected clock so a ~45-frame broadcast takes
milliseconds. What this proves that the unit tier cannot: the real
`record_thread` CAS (its 0-row path included), the real `get_broadcast_inputs`
reads feeding a real broadcast, the real box score behind the revealed
fallback, and the row state machine surviving a "restart" (a fresh bot with an
empty registry over the same database and channels).

No live gateway exists here: this proves the cog's decisions and call shapes,
not that a real guild accepts them.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import asyncio
import functools
import random
import re

import pytest

from dba2.bot import game_feed
from dba2.bot.broadcast import BroadcastScheduler, BroadcastSettings
from dba2.bot.cogs.broadcast import BroadcastCommands
from dba2.data.repositories import broadcast_repo, game_repo, league_repo
from dba2.domain.broadcast import BroadcastStatus
from dba2.services import game_sim_service
from tests.integration import test_game_feed as game_feed_tests
from tests.integration.test_game_feed import World, persisted_result_line, synthetic_producer
from tests.unit.bot._game_feed_fakes import FakeBot
from tests.unit.bot._watch_fakes import FakeInteraction, WatchChannel

world = game_feed_tests.world  # the producer's fixture (seeded league, `#games` row), reused
PENDING, QUEUED, LIVE = BroadcastStatus.PENDING, BroadcastStatus.QUEUED, BroadcastStatus.LIVE
FINISHED, INTERRUPTED = BroadcastStatus.FINISHED, BroadcastStatus.INTERRUPTED
Outcome = game_feed.RevealOutcome


class Watching:
    """One "process": a bot (its own registry and scheduler) over the world's
    database and a `#games` channel that can start threads."""

    def __init__(self, world: World, channel: WatchChannel, guild_id: int, *, gate=None) -> None:
        self.world = world
        self.channel = channel
        self.guild_id = guild_id
        self.gate = gate
        self.bot = FakeBot(channel)

        async def sleep(seconds: float) -> None:
            if self.gate is not None:
                await self.gate.wait()
            await world.clock.sleep(seconds)

        self.bot.broadcasts = BroadcastScheduler(
            BroadcastSettings(max_live_broadcasts_per_user=9),
            on_ended=functools.partial(game_feed.on_ended, self.bot, executor=world.pool),
            clock=world.clock,
            sleep=sleep,
        )
        self.cog = BroadcastCommands(self.bot)

    async def watch(self, game_id: int, *, user_id: int = 10, guild_id: int | None = None):
        interaction = FakeInteraction(user_id, guild_id=guild_id or self.guild_id)
        await self.cog.watch.callback(self.cog, interaction, game_id)
        return interaction

    async def finish(self, game_id: int) -> None:
        live = self.bot.broadcasts.live(self.world.league_id, game_id)
        assert live is not None
        await asyncio.wait_for(live.task, 30)

    async def row(self, game_id: int):
        return await broadcast_repo.get_broadcast(
            self.world.pool, league_id=self.world.league_id, game_id=game_id
        )


@pytest.fixture
async def watching(world):
    """The world's producer bot is replaced by one whose `#games` channel can
    start threads (same channel id, so every row the world wrote still points at
    it)."""
    league = await league_repo.get_league(world.pool, world.league_id)
    channel = WatchChannel(world.clock)
    world.games = channel
    process = Watching(world, channel, league.discord_guild_id)
    world.bot = process.bot  # the producer opens blocks on the same bot the cog reads
    yield process
    await process.bot.broadcasts.shutdown()  # cancels a broadcast parked on its gate


def _restart(process: Watching, *, gate=None) -> Watching:
    """A new process: empty registry, idle scheduler, the same database and channel."""
    return Watching(process.world, process.channel, process.guild_id, gate=gate)


def _no_score_in(text: str, world_scores: list[tuple[int, int]]) -> None:
    for home, away in world_scores:
        for token in (home, away):
            assert not re.search(rf"\b{token}\b", text), f"score {token} leaked: {text!r}"


async def _scores(world: World, game_ids) -> list[tuple[int, int]]:
    games = [await game_repo.get_game(world.pool, g) for g in game_ids]
    return [(g.home_score, g.away_score) for g in games]


# --- /watch -> paced broadcast -> block close -> reveal ------------------------------------


async def test_watch_then_broadcast_then_block_close_reveals_each_game_once(watching):
    world = watching.world
    block_a = world.game_ids[:3]
    _, results, block_games = await synthetic_producer(world, block_a, block_id="2026-10-22")
    scores = await _scores(world, block_a)
    watched = block_a[0]

    it = await watching.watch(watched)
    thread = watching.channel.threads[0]
    assert it.reply["ephemeral"] is True
    assert it.text == (
        f"Broadcasting game {watched} in its own thread: {thread.jump_url} It starts right away."
    )
    _no_score_in(it.text, scores)
    row = await watching.row(watched)
    assert (row.status, row.thread_id) == (QUEUED, thread.id)
    assert row.message_id == thread.messages[0].id
    assert watching.channel.thread_requests[0]["name"].endswith(f"game {watched}")
    assert watching.channel.thread_requests[0]["auto_archive_duration"] == 1440
    root_edits = [kw["content"] for _, kw in watching.channel.ops("edit")]
    assert root_edits == [
        f"Game {watched} · {block_games[0].away_code} @ {block_games[0].home_code} — watching now"
    ]

    await watching.finish(watched)
    row = await watching.row(watched)
    assert row.status is FINISHED and row.reveal_claimed_at is None
    message = thread.messages[0]
    assert len(message.calls) > 10
    final_embed = message.calls[-1][0]["embed"].to_dict()
    assert block_games[0].home_code in str(final_embed)  # the real teams, read from Postgres
    # the block is still open: the root line went back to "not yet played"
    assert watching.channel.content[row.root_message_id].endswith("not yet played")
    for content in watching.channel.content.values():
        assert " final" not in content

    # a second /watch of the finished broadcast: link, no re-broadcast
    again = await watching.watch(watched, user_id=11)
    assert again.text.startswith("That broadcast already finished; the thread is here: ")
    assert again.text.endswith(f"/{thread.id}")
    assert len(watching.channel.threads) == 1

    # block B opens: block A reveals, each game exactly once
    report_b, _, _ = await synthetic_producer(world, world.game_ids[3:4], block_id="2026-10-23")
    assert report_b.previous_block.outcomes == {g: Outcome.REVEALED for g in block_a}
    for game_id in block_a:
        r = await watching.row(game_id)
        assert r.reveal_posted_at is not None
        assert watching.channel.content[r.root_message_id] == await persisted_result_line(
            world.pool, game_id
        )
    assert (
        await game_feed.reveal_game(
            watching.bot, world.pool, league_id=world.league_id, game_id=watched
        )
        is Outcome.NOT_OWNED
    )

    # an unwatched, now-revealed game: the box-score fallback, and NO thread
    other = block_a[1]
    fallback = await watching.watch(other)
    assert fallback.text.endswith("Here's the final box score.")
    assert fallback.reply["embed"] is not None and fallback.reply["ephemeral"] is True
    assert (await watching.row(other)).thread_id is None
    assert len(watching.channel.threads) == 1


async def test_a_game_of_the_open_block_can_be_watched_after_others_finished(watching):
    world = watching.world
    ids = world.game_ids[:3]
    await synthetic_producer(world, ids, block_id="2026-10-22")
    for game_id in ids[:2]:
        it = await watching.watch(game_id)
        assert it.text.startswith("Broadcasting game")
        await watching.finish(game_id)
    rows = [await watching.row(g) for g in ids]
    assert [r.status for r in rows] == [FINISHED, FINISHED, PENDING]
    assert len({r.thread_id for r in rows[:2]}) == 2
    assert all(r.reveal_claimed_at is None for r in rows)


# --- a second /watch while live, and concurrent /watches ---------------------------------------


async def test_a_second_watch_while_live_links_the_same_thread(watching):
    world = watching.world
    await synthetic_producer(world, world.game_ids[:1], block_id="2026-10-22")
    watching.gate = asyncio.Event()
    game_id = world.game_ids[0]
    first = await watching.watch(game_id, user_id=10)
    task = watching.bot.broadcasts.live(world.league_id, game_id).task
    second = await watching.watch(game_id, user_id=11)
    thread = watching.channel.threads[0]
    assert second.text == (
        f"Game {game_id} is being broadcast — the thread is here: "
        f"https://discord.com/channels/{watching.guild_id}/{thread.id}"
    )
    assert first.text.startswith("Broadcasting game")
    assert len(watching.channel.threads) == 1
    assert watching.bot.broadcasts.live(world.league_id, game_id).task is task
    row = await watching.row(game_id)
    assert row.thread_id == thread.id and row.status in (QUEUED, LIVE)


def _is_link_to_the_winners_thread(text: str, thread_id: int) -> bool:
    return "is being broadcast" in text and text.endswith(f"/{thread_id}")


async def test_two_concurrent_watches_make_one_thread_and_one_task(watching):
    """Exactly one `/watch` starts the broadcast; the other is refused admission and re-reads
    the row. What that re-read sees depends on how its ONE database round trip interleaves with
    the winner's many (before the winner recorded its thread: "someone is starting it"; after:
    the thread's link), and so does WHICH of the two calls wins -- both are correct answers, so
    the test asserts the invariant (one thread request, one task, one winner, a loser that is
    told the truth) and the deterministic twin below pins the link outcome."""
    world = watching.world
    await synthetic_producer(world, world.game_ids[:1], block_id="2026-10-22")
    game_id = world.game_ids[0]
    watching.gate = asyncio.Event()  # park the winner's broadcast so `finish` finds it live
    a, b = await asyncio.gather(
        watching.watch(game_id, user_id=10), watching.watch(game_id, user_id=11)
    )
    winners = [i for i in (a, b) if i.text.startswith("Broadcasting game")]
    losers = [i for i in (a, b) if not i.text.startswith("Broadcasting game")]
    assert len(winners) == 1 and len(losers) == 1, (a.text, b.text)
    (thread,) = watching.channel.threads
    assert losers[0].text.startswith("Someone is starting game") or _is_link_to_the_winners_thread(
        losers[0].text, thread.id
    ), losers[0].text
    assert len(watching.channel.thread_requests) == 1
    assert watching.bot.broadcasts.depth == 1
    watching.gate.set()
    await watching.finish(game_id)


async def test_a_second_watch_that_reads_the_row_after_the_thread_was_recorded_links_it(
    watching, monkeypatch
):
    """The other half of the race above, made deterministic instead of left to timing: the
    first call is the winner because the second is not started until the first has TAKEN its
    admission (an event set by a wrapper around `admit`, not a scheduling guess), and the
    loser's re-read is held until the winner's `record_thread` has committed, so it must answer
    with the thread's link, not "someone is starting it"."""
    world = watching.world
    await synthetic_producer(world, world.game_ids[:1], block_id="2026-10-22")
    game_id = world.game_ids[0]
    recorded = asyncio.Event()
    admitted = asyncio.Event()
    watching.gate = asyncio.Event()  # park the winner's broadcast so `finish` finds it live
    real_record, real_link = broadcast_repo.record_thread, BroadcastCommands._link_current
    real_admit = BroadcastScheduler.admit

    def admit_then_signal(self, **kwargs):
        try:
            return real_admit(self, **kwargs)
        finally:
            admitted.set()  # the first call to get here owns the game; `admit` itself is sync

    async def record_then_signal(*args, **kwargs):
        try:
            return await real_record(*args, **kwargs)
        finally:
            recorded.set()

    async def link_after_the_winner_recorded(self, *args, **kwargs):
        await recorded.wait()
        return await real_link(self, *args, **kwargs)

    monkeypatch.setattr(broadcast_repo, "record_thread", record_then_signal)
    monkeypatch.setattr(BroadcastCommands, "_link_current", link_after_the_winner_recorded)
    monkeypatch.setattr(BroadcastScheduler, "admit", admit_then_signal)
    first = asyncio.create_task(watching.watch(game_id, user_id=10))
    # the first admission is taken before the second call starts
    await asyncio.wait_for(admitted.wait(), 30)
    second = await asyncio.wait_for(watching.watch(game_id, user_id=11), 30)
    winner = await asyncio.wait_for(first, 30)

    (thread,) = watching.channel.threads
    assert winner.text.startswith("Broadcasting game")
    assert _is_link_to_the_winners_thread(second.text, thread.id), second.text
    assert len(watching.channel.thread_requests) == 1
    watching.gate.set()
    await watching.finish(game_id)


async def test_a_lost_record_thread_race_against_real_postgres_links_the_winner(
    watching, monkeypatch
):
    """Another process's `/watch` wins the real `record_thread` CAS between this
    call's thread creation and its own write: the real statement reports 0 rows."""
    world = watching.world
    await synthetic_producer(world, world.game_ids[:1], block_id="2026-10-22")
    game_id = world.game_ids[0]
    real = broadcast_repo.record_thread

    async def other_process_wins_first(ex, **kwargs):
        won = await real(
            ex,
            league_id=kwargs["league_id"],
            game_id=kwargs["game_id"],
            thread_id=555_001,
            message_id=555_002,
        )
        assert won == 1
        return await real(ex, **kwargs)  # ours: the real guard now matches nothing

    monkeypatch.setattr(broadcast_repo, "record_thread", other_process_wins_first)
    it = await watching.watch(game_id)
    assert it.text.endswith("/555001")
    assert watching.channel.threads[0].deleted is True
    row = await watching.row(game_id)
    assert (row.thread_id, row.message_id, row.status) == (555_001, 555_002, QUEUED)
    assert watching.bot.broadcasts.live(world.league_id, game_id) is None
    assert watching.bot.broadcasts.depth == 0


# --- not watchable ---------------------------------------------------------------------------


async def test_an_orphan_pending_row_that_was_revealed_is_a_box_score_not_a_broadcast(watching):
    world = watching.world
    await synthetic_producer(world, world.game_ids[:2], block_id="2026-10-22")
    await game_feed.close_block(watching.bot, world.pool, league_id=world.league_id)
    game_id = world.game_ids[0]
    row = await watching.row(game_id)
    assert row.status is PENDING and row.reveal_posted_at is not None
    it = await watching.watch(game_id)
    assert it.text.endswith("Here's the final box score.") and it.reply["embed"] is not None
    row = await watching.row(game_id)
    assert row.status is PENDING and row.thread_id is None
    assert watching.channel.threads == [] and watching.bot.broadcasts.depth == 0


async def test_after_a_restart_an_unrevealed_game_is_not_watchable_and_shows_no_score(watching):
    world = watching.world
    ids = world.game_ids[:2]
    await synthetic_producer(world, ids, block_id="2026-10-22")
    scores = await _scores(world, ids)
    restarted = _restart(watching)  # nothing in memory; the block was never closed
    it = await restarted.watch(ids[0])
    assert it.text == (
        "That game isn't watchable — its replay is gone and its result hasn't been posted yet. "
        "It'll appear in #games when the block closes."
    )
    assert it.reply["embed"] is None
    _no_score_in(it.text, scores)
    assert (await restarted.row(ids[0])).thread_id is None
    assert watching.channel.threads == []


async def test_a_played_game_in_no_block_and_an_unplayed_game(watching):
    world = watching.world
    game_id = world.game_ids[0]
    it = await watching.watch(game_id)  # scheduled, no row
    assert "hasn't been played yet" in it.text
    await game_sim_service.sim_game(world.pool, game_id)  # final, but in no block
    it = await watching.watch(game_id)
    assert "isn't part of a block" in it.text and it.reply["embed"] is None
    assert await watching.row(game_id) is None
    assert watching.channel.threads == [] and watching.bot.broadcasts.depth == 0


async def test_a_game_of_another_league_is_not_found(watching):
    world = watching.world
    await synthetic_producer(world, world.game_ids[:1], block_id="2026-10-22")
    other = await league_repo.create_league(
        world.pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Other League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=1,
    )
    try:
        it = await watching.watch(world.game_ids[0], guild_id=other.discord_guild_id)
        assert it.text == f"No game {world.game_ids[0]} in this league."
        assert watching.channel.threads == []
        assert (await watching.row(world.game_ids[0])).thread_id is None
    finally:
        await world.pool.execute("DELETE FROM leagues WHERE id = $1", other.id)


async def test_a_refused_admission_creates_nothing_and_a_retry_succeeds(watching):
    world = watching.world
    await synthetic_producer(world, world.game_ids[:2], block_id="2026-10-22")
    watching.bot.broadcasts = BroadcastScheduler(
        BroadcastSettings(max_concurrent_broadcasts=1, max_queue_depth=0),
        on_ended=functools.partial(game_feed.on_ended, watching.bot, executor=world.pool),
        clock=world.clock,
        sleep=world.clock.sleep,
    )
    watching.bot.broadcasts.admit(league_id=world.league_id, game_id=999_999, user_id=77)
    game_id = world.game_ids[0]
    it = await watching.watch(game_id)
    assert "Too many broadcasts" in it.text and "nothing was created" in it.text
    row = await watching.row(game_id)
    assert (row.status, row.thread_id) == (PENDING, None)
    assert watching.channel.threads == [] and watching.channel.thread_requests == []
    watching.bot.broadcasts.release_unspawned(world.league_id, 999_999)
    retry = await watching.watch(game_id)
    assert retry.text.startswith("Broadcasting game")
    await watching.finish(game_id)


# --- restart: the state machine is keyed on the row, not on memory -------------------------------


async def test_a_restart_mid_broadcast_links_the_old_thread_and_the_sweep_ends_it(watching):
    world = watching.world
    await synthetic_producer(world, world.game_ids[:1], block_id="2026-10-22")
    game_id = world.game_ids[0]
    watching.gate = asyncio.Event()  # the broadcast is parked, then the "process dies"
    await watching.watch(game_id)
    thread = watching.channel.threads[0]
    for _ in range(50):  # let the task take its slot and write `live`, then park on the gate
        if (await watching.row(game_id)).status is LIVE:
            break
        await asyncio.sleep(0)
    assert (await watching.row(game_id)).status is LIVE

    restarted = _restart(watching)
    link = await restarted.watch(game_id, user_id=11)
    assert link.text == (
        f"Game {game_id} is being broadcast — the thread is here: "
        f"https://discord.com/channels/{watching.guild_id}/{thread.id}"
    )
    assert len(watching.channel.threads) == 1, "a restart cannot produce a second thread"
    assert restarted.bot.broadcasts.depth == 0

    report = await game_feed.sweep_league(restarted.bot, world.pool, league_id=world.league_id)
    assert [(e.shape.value, e.reveal) for e in report.entries] == [
        ("interrupted_broadcast", Outcome.REVEALED)
    ]
    row = await restarted.row(game_id)
    assert row.status is INTERRUPTED and row.reveal_posted_at is not None
    done = await restarted.watch(game_id, user_id=11)
    assert done.text.startswith("That broadcast already finished; the thread is here: ")
    assert len(watching.channel.threads) == 1
