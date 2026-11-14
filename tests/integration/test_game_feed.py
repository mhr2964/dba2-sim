"""`game_feed` end to end against real Postgres, driven by a SYNTHETIC PRODUCER
(Stage D commit 9, docs/design/phase-5-stage-d-broadcast.md §5 P5-97.n, §10 Tier
3, §11 row 9).

Stage E's `/advance` does not exist yet, so the producer is a test helper: it
sims N real games with the real `sim_game` path, hands them to `open_block`,
and plays a broadcast per watched game through the REAL `BroadcastScheduler`
against a fake thread message and an injected clock (a ~45-frame game runs in
milliseconds). Only Discord is fake; every row, status move, claim and score
here is real Postgres. What this proves end to end: no digit of a final score
reaches `#games` while a block is open, a watched game is revealed only once
its block closes, every reveal happens exactly once (the CAS), and a restart
(fresh registry + `sweep_league`) converges every shape a dead process can
leave -- including a broadcast task that was killed before its finalizer ran.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it -- same
requirement as every other tests/integration/ module. No live gateway exists
here: the fakes prove `game_feed`'s decisions and call shapes, not that a real
guild accepts them.
"""

from __future__ import annotations

import asyncio
import functools
import random
from dataclasses import dataclass

import pytest

from dba2.bot import game_feed
from dba2.bot.block_registry import BlockGame
from dba2.bot.broadcast import (
    BroadcastInputs,
    BroadcastJob,
    BroadcastScheduler,
    BroadcastSettings,
)
from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import broadcast_repo, channel_repo, game_repo, team_repo
from dba2.domain.broadcast import BroadcastStatus
from dba2.domain.channel import ChannelPurpose
from dba2.presentation.broadcast import plan_broadcast
from dba2.presentation.games import SlateGame, build_matchup_line, build_slate_header
from dba2.services import game_sim_service, schedule_service
from tests.unit.bot._broadcast_fakes import FakeClock, FakeMessage, FakeThread
from tests.unit.bot._game_feed_fakes import FakeBot, FakeChannel, assert_no_mentions, http_error
from tests.unit.presentation._broadcast_helpers import names_for
from tools import seed_dev_league

GAMES_CHANNEL_ID = 9001
Outcome = game_feed.RevealOutcome
PENDING, QUEUED, LIVE = BroadcastStatus.PENDING, BroadcastStatus.QUEUED, BroadcastStatus.LIVE
FINISHED, INTERRUPTED = BroadcastStatus.FINISHED, BroadcastStatus.INTERRUPTED


@dataclass
class World:
    pool: object
    league_id: int
    season: int
    game_ids: list[int]
    games: FakeChannel
    thread: FakeChannel
    bot: FakeBot
    clock: FakeClock

    def new_bot(self) -> FakeBot:
        """A restarted process: an empty registry and an idle scheduler over the
        SAME Discord channel state and the same database."""
        return _make_bot(self.pool, self.games, self.thread, self.clock)


def _make_bot(pool, games: FakeChannel, thread: FakeChannel, clock: FakeClock) -> FakeBot:
    bot = FakeBot(games, thread)
    bot.broadcasts = BroadcastScheduler(
        BroadcastSettings(max_live_broadcasts_per_user=9),
        on_ended=functools.partial(game_feed.on_ended, bot, executor=pool),
        clock=clock,
        sleep=clock.sleep,
    )
    return bot


@pytest.fixture
async def world():
    pool = await get_pool()
    league_id = await seed_dev_league.seed_dev_league(
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Game Feed Integration League",
        start_season_year=2026,
        current_season=2026,
        commissioner_user_id=1,
        rng_seed=911,
    )
    try:
        await schedule_service.generate_regular_season(pool, league_id, 2026)
        games = await game_repo.list_games_for_league_season(pool, league_id, 2026)
        await channel_repo.create_league_channel(
            pool, league_id=league_id, purpose=ChannelPurpose.GAMES, channel_id=GAMES_CHANNEL_ID
        )
        games_channel = FakeChannel(GAMES_CHANNEL_ID)
        thread = FakeChannel(7001, first_message_id=8000)
        clock = FakeClock()
        yield World(
            pool=pool,
            league_id=league_id,
            season=2026,
            game_ids=[g.id for g in games[:6]],
            games=games_channel,
            thread=thread,
            bot=_make_bot(pool, games_channel, thread, clock),
            clock=clock,
        )
    finally:
        await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()


# --- the synthetic producer ---------------------------------------------------


async def synthetic_producer(world: World, game_ids: list[int], *, block_id: str):
    """What Stage E's `/advance` will do: sim each game through the real
    `sim_game` path, then make ONE call, `open_block`. Returns the report and
    the in-memory results (which the test compares against what was posted)."""
    block_games: list[BlockGame] = []
    results = {}
    for game_id in game_ids:
        result = await game_sim_service.sim_game(world.pool, game_id)
        game = await game_repo.get_game(world.pool, game_id)
        home = await team_repo.get_team(world.pool, game.home_team_id)
        away = await team_repo.get_team(world.pool, game.away_team_id)
        results[game_id] = result
        block_games.append(
            BlockGame(
                game_id=game_id,
                home_team_id=home.id,
                away_team_id=away.id,
                home_code=home.code,
                away_code=away.code,
                game_date=game.game_date,
                result=result,
            )
        )
    report = await game_feed.open_block(
        world.bot,
        world.pool,
        league_id=world.league_id,
        season=world.season,
        block_id=block_id,
        games=block_games,
    )
    return report, results, block_games


async def watch(world: World, block_game: BlockGame, result, *, user_id: int = 10):
    """`/watch`'s persisted half (commit 10 builds the command): record the
    thread and its message, admit, spawn -- then play the broadcast to the end
    on the injected clock."""
    game_id = block_game.game_id
    thread_id, message_id = 7001, 8001 + game_id
    recorded = await broadcast_repo.record_thread(
        world.pool,
        league_id=world.league_id,
        game_id=game_id,
        thread_id=thread_id,
        message_id=message_id,
    )
    assert recorded == 1
    home = await team_repo.get_team(world.pool, block_game.home_team_id)
    away = await team_repo.get_team(world.pool, block_game.away_team_id)
    plan = plan_broadcast(result, game_id=game_id, home_code=home.code, away_code=away.code)
    message = FakeMessage(message_id=message_id)
    job = BroadcastJob(
        league_id=world.league_id,
        game_id=game_id,
        user_id=user_id,
        plan=plan,
        inputs=BroadcastInputs(home=home, away=away, names=names_for(result), result=result),
        message=message,
        thread=FakeThread(thread_id=thread_id),
        db=world.pool,
    )
    assert (
        world.bot.broadcasts.admit(
            league_id=world.league_id, game_id=game_id, user_id=user_id
        ).value
        == "admitted"
    )
    task = await world.bot.broadcasts.spawn(job=job)
    return task, message


async def persisted_result_line(pool, game_id: int) -> str:
    """The expected reveal text, built from the database row (not from the
    in-memory result the producer holds)."""
    game = await game_repo.get_game(pool, game_id)
    home = await team_repo.get_team(pool, game.home_team_id)
    away = await team_repo.get_team(pool, game.away_team_id)
    return f"Game {game_id} · {away.code} {game.away_score} @ {home.code} {game.home_score} — final"


def slate_text(game_id: int, block_game: BlockGame) -> str:
    return build_matchup_line(
        SlateGame(
            game_id=game_id,
            home_code=block_game.home_code,
            away_code=block_game.away_code,
            game_date=block_game.game_date,
        )
    )


async def rows_by_game(world: World):
    """Every broadcast row this league has, by game id (games in no block have none)."""
    found = {}
    for game_id in world.game_ids:
        row = await broadcast_repo.get_broadcast(
            world.pool, league_id=world.league_id, game_id=game_id
        )
        if row is not None:
            found[game_id] = row
    return found


# --- the end-to-end story -----------------------------------------------------


async def test_a_block_is_opened_watched_and_revealed_exactly_once_after_it_closes(world):
    block_a = world.game_ids[:3]
    report, results, block_games = await synthetic_producer(world, block_a, block_id="2026-10-22")

    # open_block: 3 pending rows, a header and 3 root lines, nothing else
    assert (report.rows_created, report.header_posted, report.failed) == (3, True, ())
    rows = await broadcast_repo.list_broadcasts_for_block(
        world.pool, league_id=world.league_id, block_id="2026-10-22"
    )
    assert [r.status for r in rows] == [PENDING] * 3
    assert all(r.root_message_id is not None for r in rows)
    expected_lines = {build_slate_header("2026-10-22", 3)} | {
        slate_text(bg.game_id, bg) for bg in block_games
    }
    posted = {kw["content"] for _, kw in world.games.ops("send")}
    assert posted == expected_lines, "the slate posts the header and matchup lines and NOTHING else"
    for game_id in block_a:
        game = await game_repo.get_game(world.pool, game_id)
        assert (game.home_score, game.away_score) == (
            results[game_id].home_score,
            results[game_id].away_score,
        )

    # watch one game: queued -> live -> finished, zero reveals while the block is open
    watched = block_games[0]
    task, message = await watch(world, watched, results[watched.game_id])
    await asyncio.wait_for(task, 30)
    row = await broadcast_repo.get_broadcast(
        world.pool, league_id=world.league_id, game_id=watched.game_id
    )
    assert row.status is FINISHED and row.reveal_claimed_at is None
    assert len(message.calls) > 10, "the broadcast actually edited its message"
    root = row.root_message_id
    assert world.games.content[root] == slate_text(watched.game_id, watched)
    for r in (await rows_by_game(world)).values():
        if r.game_id in block_a:
            assert r.reveal_claimed_at is None and r.reveal_posted_at is None
    for content in world.games.content.values():
        assert " final" not in content, "a result line appeared while the block was open"
    others = [r for r in (await rows_by_game(world)).values() if r.game_id != watched.game_id]
    assert [r.status for r in others if r.game_id in block_a] == [PENDING, PENDING]

    # block N+1 opens: every game of block N reveals exactly once, the watched one included
    report_b, _, _ = await synthetic_producer(world, world.game_ids[3:4], block_id="2026-10-23")
    previous = report_b.previous_block
    assert previous.block_id == "2026-10-22" and previous.still_broadcasting == ()
    assert previous.outcomes == {g: Outcome.REVEALED for g in block_a}
    for game_id in block_a:
        r = await broadcast_repo.get_broadcast(
            world.pool, league_id=world.league_id, game_id=game_id
        )
        assert r.reveal_claimed_at is not None and r.reveal_posted_at is not None
        assert world.games.content[r.root_message_id] == await persisted_result_line(
            world.pool, game_id
        )

    # the CAS: a second reveal attempt on all three posts nothing
    calls_before = len(world.games.calls)
    for game_id in block_a:
        again = await game_feed.reveal_game(
            world.bot, world.pool, league_id=world.league_id, game_id=game_id
        )
        assert again is Outcome.NOT_OWNED
    assert len(world.games.calls) == calls_before
    for kwargs in [kw for _, _, kw in world.games.calls]:
        assert_no_mentions(kwargs)
    # ... and a sweep has nothing to do for block A (block B's game is still open, so skipped)
    sweep = await game_feed.sweep_league(world.bot, world.pool, league_id=world.league_id)
    assert [e.skipped_owned for e in sweep.entries] == [True]  # only block B's still-open game
    assert len(world.games.calls) == calls_before


async def test_a_block_opened_twice_inserts_rows_once_and_reveals_nothing(world):
    report, _, block_games = await synthetic_producer(
        world, world.game_ids[:3], block_id="2026-10-22"
    )
    sent = len(world.games.ops("send"))
    again = await game_feed.open_block(
        world.bot,
        world.pool,
        league_id=world.league_id,
        season=world.season,
        block_id="2026-10-22",
        games=block_games,
    )
    assert (report.rows_created, again.rows_created) == (3, 0)
    assert again.posted == () and len(world.games.ops("send")) == sent
    assert all(r.reveal_claimed_at is None for r in (await rows_by_game(world)).values())


async def test_one_failed_root_post_leaves_a_null_root_and_the_rest_intact(world):
    world.games.fail_send = {2: http_error()}  # 0 = header, 1 = first game, 2 = second game
    report, _, block_games = await synthetic_producer(
        world, world.game_ids[:3], block_id="2026-10-22"
    )
    assert [f.game_id for f in report.failed] == [block_games[1].game_id]
    rows = await rows_by_game(world)
    assert rows[block_games[1].game_id].root_message_id is None
    assert rows[block_games[0].game_id].root_message_id is not None
    assert rows[block_games[2].game_id].root_message_id is not None


async def test_game_feed_refuses_a_connection_that_is_inside_a_transaction(world):
    """P5-14: a caller-held transaction would put every Discord call inside it."""
    async with transaction(world.pool) as conn:
        with pytest.raises(ValueError, match="inside a database transaction"):
            await game_feed.open_block(
                world.bot,
                conn,
                league_id=world.league_id,
                season=world.season,
                block_id="x",
                games=[],
            )
        with pytest.raises(ValueError, match="inside a database transaction"):
            await game_feed.close_block(world.bot, conn, league_id=world.league_id)
        with pytest.raises(ValueError, match="inside a database transaction"):
            await game_feed.reveal_game(
                world.bot, conn, league_id=world.league_id, game_id=world.game_ids[0]
            )
    assert world.games.calls == []


# --- restart: a fresh registry and sweep_league converge every shape ----------


async def test_a_restart_reveals_every_unrevealed_game_once(world):
    _, _, block_games = await synthetic_producer(world, world.game_ids[:3], block_id="2026-10-22")
    restarted = world.new_bot()  # the process died: nothing in memory, block never closed
    first = await game_feed.sweep_league(restarted, world.pool, league_id=world.league_id)
    assert [e.reveal for e in first.entries] == [Outcome.REVEALED] * 3
    for bg in block_games:
        row = await broadcast_repo.get_broadcast(
            world.pool, league_id=world.league_id, game_id=bg.game_id
        )
        assert world.games.content[row.root_message_id] == await persisted_result_line(
            world.pool, bg.game_id
        )
    calls = len(world.games.calls)
    second = await game_feed.sweep_league(restarted, world.pool, league_id=world.league_id)
    assert second.entries == () and len(world.games.calls) == calls


async def test_a_restart_converges_all_four_sweep_shapes(world):
    ids = world.game_ids[:4]
    world.games.fail_send = {3: http_error()}  # header, g0, g1 post; g2's root fails
    await synthetic_producer(world, ids, block_id="2026-10-22")
    # (only send #3, game 2's root, was scripted to fail)
    live_game, crashed_game, orphan_game, ended_game = ids

    # 1. a broadcast that was live when the process died
    await broadcast_repo.record_thread(
        world.pool, league_id=world.league_id, game_id=live_game, thread_id=7001, message_id=8101
    )
    assert await broadcast_repo.transition_status(
        world.pool, league_id=world.league_id, game_id=live_game, expected=QUEUED, new=LIVE
    )
    # 2. a reveal claimed but never posted
    assert await broadcast_repo.claim_reveal(
        world.pool, league_id=world.league_id, game_id=crashed_game
    )
    # 3. an orphaned pending row with NO root message (its slate post failed) -- game 2 above
    assert (
        await broadcast_repo.get_broadcast(
            world.pool, league_id=world.league_id, game_id=orphan_game
        )
    ).root_message_id is None
    # 4. a broadcast that ended while its block was open, then the process died
    await broadcast_repo.record_thread(
        world.pool, league_id=world.league_id, game_id=ended_game, thread_id=7001, message_id=8102
    )
    for old, new in ((QUEUED, LIVE), (LIVE, FINISHED)):
        assert await broadcast_repo.transition_status(
            world.pool, league_id=world.league_id, game_id=ended_game, expected=old, new=new
        )

    restarted = world.new_bot()
    report = await game_feed.sweep_league(restarted, world.pool, league_id=world.league_id)
    shapes = {e.game_id: (e.shape.value, e.reveal) for e in report.entries}
    assert shapes == {
        live_game: ("interrupted_broadcast", Outcome.REVEALED),
        crashed_game: ("crashed_reveal", Outcome.REVEALED),
        orphan_game: ("orphaned_pending", Outcome.REVEALED),
        ended_game: ("unrevealed_ended", Outcome.REVEALED),
    }
    rows = await rows_by_game(world)
    assert rows[live_game].status is INTERRUPTED
    for game_id in ids:
        assert rows[game_id].reveal_posted_at is not None
    # the dead broadcast's message got the interrupted panel, from persisted data
    ((message_id, kwargs),) = world.thread.ops("edit")
    assert message_id == 8101 and "Broadcast interrupted" in kwargs["embed"].description
    # every result is in #games, the orphan as a FRESH post (it had no root line)
    for game_id in (live_game, crashed_game, ended_game):
        assert world.games.content[rows[game_id].root_message_id] == await persisted_result_line(
            world.pool, game_id
        )
    fresh = [kw["content"] for _, kw in world.games.ops("send") if " final" in kw["content"]]
    assert fresh == [await persisted_result_line(world.pool, orphan_game)]

    calls = (len(world.games.calls), len(world.thread.calls))
    second = await game_feed.sweep_league(restarted, world.pool, league_id=world.league_id)
    assert second.entries == () and (len(world.games.calls), len(world.thread.calls)) == calls
    for _, _, kwargs in [*world.games.calls, *world.thread.calls]:
        assert_no_mentions(kwargs)


async def test_a_killed_broadcast_task_leaves_a_queued_row_the_sweep_converges(world):
    """Design M3: the finalizer does not always run. A task cancelled before its
    first step never enters its `try`, so the row stays `queued` -- and the
    startup sweep, not the finalizer, is what makes the system converge."""
    _, results, block_games = await synthetic_producer(
        world, world.game_ids[:1], block_id="2026-10-22"
    )
    game = block_games[0]
    task, _ = await watch(world, game, results[game.game_id])
    task.cancel()  # no await since spawn: the body never ran
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    row = await broadcast_repo.get_broadcast(
        world.pool, league_id=world.league_id, game_id=game.game_id
    )
    assert row.status is QUEUED and row.reveal_claimed_at is None

    report = await game_feed.sweep_league(world.new_bot(), world.pool, league_id=world.league_id)
    assert [(e.shape.value, e.reveal) for e in report.entries] == [
        ("interrupted_broadcast", Outcome.REVEALED)
    ]
    row = await broadcast_repo.get_broadcast(
        world.pool, league_id=world.league_id, game_id=game.game_id
    )
    assert row.status is INTERRUPTED and row.reveal_posted_at is not None


async def test_a_watched_game_whose_block_closes_mid_broadcast_reveals_on_completion(world):
    """close_block leaves a game that is still broadcasting to its own finalizer,
    which sees the block closed and reveals -- exactly once."""
    _, results, block_games = await synthetic_producer(
        world, world.game_ids[:2], block_id="2026-10-22"
    )
    game = block_games[0]
    task, _ = await watch(world, game, results[game.game_id])
    await asyncio.sleep(0)  # the task is admitted and spawned, not yet finished
    report = await game_feed.close_block(world.bot, world.pool, league_id=world.league_id)
    assert report.still_broadcasting == (game.game_id,)
    assert set(report.outcomes) == {block_games[1].game_id}
    await asyncio.wait_for(task, 30)
    row = await broadcast_repo.get_broadcast(
        world.pool, league_id=world.league_id, game_id=game.game_id
    )
    assert row.status is FINISHED and row.reveal_posted_at is not None
    assert world.games.content[row.root_message_id] == await persisted_result_line(
        world.pool, game.game_id
    )
    reveal_edits = [
        kw["content"] for _, kw in world.games.ops("edit") if " final" in (kw["content"] or "")
    ]
    assert len(reveal_edits) == 2  # one per game, no duplicates
