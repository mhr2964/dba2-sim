"""`/watch` (`dba2/bot/cogs/broadcast.py`) with fakes: what it says and does for
every row state of the persisted `game_broadcasts` machine, every refusal, every
Discord failure, and the spoiler rule (docs/design/phase-5-stage-d-broadcast.md
§7, P5-97.l/.p/.q/.r/.s/.w).

No DB and no gateway: the repositories are the in-memory `Store` (a model of
`broadcast_repo`, whose SQL `tests/integration/test_broadcast_repo.py` proves),
the scheduler is the REAL `BroadcastScheduler` on an injected clock, the block
registry is real, and Discord is `_watch_fakes`. What this proves is the cog's
decisions and the shape of every call it makes -- not that a real guild accepts
them (no live gateway exists).

The twins at the bottom break a private copy of the real cog and require the
scenario that guards the line to go red (design §10's mutation twins).
"""

from __future__ import annotations

import ast
import asyncio
import re
from dataclasses import replace
from pathlib import Path

import pytest

from dba2.bot import client as bot_client
from dba2.bot import game_feed
from dba2.bot.broadcast import BroadcastSettings
from dba2.bot.cogs import broadcast as cog_module
from dba2.bot.context import NoLeagueForGuild
from dba2.bot.interaction_errors import GENERIC_FAILURE_MESSAGE
from dba2.core.errors import NotFoundError
from dba2.data.repositories import broadcast_repo
from dba2.domain.broadcast import BroadcastStatus, SweepShape
from dba2.domain.game import GameStatus
from dba2.domain.phase import Phase
from dba2.phase.command_gates import ALL_PHASES, COMMAND_GATES
from dba2.presentation.broadcast import FAST_PLAN_SETTINGS, FULL_PLAN_SETTINGS, GAME_PLAN_SETTINGS
from dba2.presentation.games import SlateGame, build_matchup_line, build_watching_line
from tests.unit.bot._broadcast_fakes import assert_no_mentions, sim_result
from tests.unit.bot._game_feed_fakes import (
    AWAY_TEAM,
    HOME_TEAM,
    LEAGUE,
    FakeChannel,
    block_game,
    forbidden,
    http_error,
    not_found,
)
from tests.unit.bot._watch_fakes import (
    GID,
    GUILD_ID,
    ROOT_BASE,
    THREAD_BASE,
    THREAD_MESSAGE_BASE,
    FakeInteraction,
    WatchThread,
    World,
    cog_variant,
)

# This file writes scratch files into `dba2/` or scans it (`tests/conftest.py`): one xdist
# group keeps every such writer and reader on one worker, run serially.
pytestmark = pytest.mark.xdist_group("source-tree")

PENDING, QUEUED, LIVE = BroadcastStatus.PENDING, BroadcastStatus.QUEUED, BroadcastStatus.LIVE
FINISHED, INTERRUPTED = BroadcastStatus.FINISHED, BroadcastStatus.INTERRUPTED
ROOT = ROOT_BASE + GID
SLATE = SlateGame(GID, HOME_TEAM.code, AWAY_TEAM.code, block_game(GID).game_date)
MATCHUP, WATCHING = build_matchup_line(SLATE), build_watching_line(SLATE)
URL_OF_THREAD_50 = f"https://discord.com/channels/{GUILD_ID}/{THREAD_BASE + 50}"

# Distinctive final scores: the database's (set per game below) and the in-memory
# sim's. A leak of either into a reply is a whole-word match.
DB_SCORES = (131, 117)


@pytest.fixture
async def worlds(monkeypatch):
    """A factory for `World`s; every one is shut down afterwards so no broadcast
    task outlives its test."""
    made: list[World] = []

    def make(module=cog_module, **kwargs) -> World:
        world = World(monkeypatch, module, **kwargs)
        made.append(world)
        return world

    yield make
    for world in made:
        if world.gate is not None:
            world.gate.set()
        await world.bot.broadcasts.shutdown()


def _score_tokens() -> list[str]:
    result = sim_result()
    return [str(n) for n in (*DB_SCORES, result.home_score, result.away_score)]


def _assert_no_score(interaction) -> None:
    """Nothing this interaction said carries a final score, and none of it is an
    embed (the box score is the only embed a reply may carry)."""
    for reply in interaction.replies:
        assert reply["embed"] is None, "an unrevealed game's reply carried an embed"
        assert reply["ephemeral"] is True
        for token in _score_tokens():
            assert not re.search(rf"\b{token}\b", reply["content"]), (
                f"score {token} leaked into {reply['content']!r}"
            )


# --- the happy path ------------------------------------------------------------


async def test_watch_creates_the_thread_persists_it_and_replies_with_its_link(worlds):
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID)
    w.open_block(GID)
    it = await w.watch()

    assert it.response.deferred == [True]  # ephemeral defer, first
    thread = w.channel.threads[0]
    assert it.text == (
        f"Broadcasting game {GID} in its own thread: {thread.jump_url} It starts right away."
    )
    assert it.reply["ephemeral"] is True and it.response.messages == []
    assert_no_mentions(it.reply)

    # the ONE create_thread call: off the game's root line, with the design's name
    assert w.channel.thread_requests == [
        {"message_id": ROOT, "name": "AWY @ HOM — game 412", "auto_archive_duration": 1440}
    ]
    # the placeholder is a real thread message (never an interaction followup)
    assert len(thread.sent) == 1
    assert thread.sent[0]["content"] == "Starting the broadcast…"
    assert_no_mentions(thread.sent[0])
    # persisted together: thread id, the placeholder's message id, status queued
    row = w.row()
    assert (row.thread_id, row.message_id, row.status) == (
        thread.id,
        THREAD_MESSAGE_BASE,
        QUEUED,
    )
    assert w.store.called("record_thread") == [
        {"game_id": GID, "thread_id": thread.id, "message_id": THREAD_MESSAGE_BASE}
    ]
    # the root line said "watching now" (an edit of the existing line, no new post)
    assert w.channel.ops("send") == []
    assert [(mid, kw["content"]) for mid, kw in w.channel.ops("edit")] == [(ROOT, WATCHING)]
    for _, kwargs in w.channel.ops("edit"):
        assert_no_mentions(kwargs)
    assert w.bot.broadcasts.live(LEAGUE, GID) is not None


async def test_the_started_broadcast_runs_to_finished_and_the_root_goes_back_to_not_yet_played(
    worlds,
):
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID)
    w.open_block(GID)
    await w.watch()
    await w.finish()

    thread = w.channel.threads[0]
    message = thread.messages[0]
    assert len(message.calls) > 10, "the pacer edited the thread's placeholder message"
    assert w.row().status is FINISHED
    assert w.row().reveal_claimed_at is None, "the block is still open: nothing was revealed"
    assert w.channel.content[ROOT] == MATCHUP  # on_ended dropped "watching now"
    assert w.bot.broadcasts.depth == 0


async def test_watch_reveals_a_game_whose_block_closed_while_it_was_broadcasting(worlds):
    """`close_block` skips a game it sees as still broadcasting (P5-97.m), so the
    broadcast's own `on_ended` is what reveals it once it finishes."""
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID)
    w.open_block(GID)
    await w.watch()
    report = await game_feed.close_block(w.bot, "pool", league_id=LEAGUE)
    assert report.still_broadcasting == (GID,) and report.outcomes == {}
    assert w.row().reveal_claimed_at is None
    await w.finish()
    assert w.row().reveal_posted_at is not None
    assert (
        w.channel.content[ROOT] == f"Game {GID} · AWY {DB_SCORES[1]} @ HOM {DB_SCORES[0]} — final"
    )


async def test_a_block_closed_after_the_broadcast_reveals_it_exactly_once(worlds):
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID)
    w.open_block(GID)
    await w.watch()
    await w.finish()
    report = await game_feed.close_block(w.bot, "pool", league_id=LEAGUE)
    assert report.outcomes == {GID: game_feed.RevealOutcome.REVEALED}
    again = await game_feed.reveal_game(w.bot, "pool", league_id=LEAGUE, game_id=GID)
    assert again is game_feed.RevealOutcome.NOT_OWNED


async def test_a_graceful_stop_then_a_restart_leaves_no_thread_frozen(worlds):
    """F2 end to end: /watch, stop the bot mid-game, restart. The stop edits the
    viewer's message to the interrupted panel; if that edit had not landed, the
    restart's sweep does it (the row is `interrupted`, `UNREVEALED_ENDED`)."""
    w = worlds()
    w.gate = asyncio.Event()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID)
    w.open_block(GID)
    await w.watch()
    for _ in range(5):
        await asyncio.sleep(0)
    assert w.row().status is LIVE
    thread = w.channel.threads[0]
    message = thread.messages[0]
    await w.bot.broadcasts.shutdown()  # DBABot.close()
    assert w.row().status is INTERRUPTED
    assert "Broadcast interrupted" in message.calls[-1][0]["embed"].description

    # restart: memory is empty, and (worst case) the stop's edit never reached Discord
    w.bot.blocks.close(LEAGUE)
    reopened = FakeChannel(thread.id, first_message_id=9000)
    w.bot.channels[thread.id] = reopened
    report = await game_feed.sweep_league(w.bot, "pool", league_id=LEAGUE)
    assert [(e.shape, e.reveal) for e in report.entries] == [
        (SweepShape.UNREVEALED_ENDED, game_feed.RevealOutcome.REVEALED)
    ]
    ((message_id, kwargs),) = reopened.ops("edit")
    assert message_id == THREAD_MESSAGE_BASE
    assert "Broadcast interrupted" in kwargs["embed"].description
    assert (
        w.channel.content[ROOT] == f"Game {GID} · AWY {DB_SCORES[1]} @ HOM {DB_SCORES[0]} — final"
    )


async def test_watch_never_awaits_the_broadcast(worlds):
    """The pacer is parked on a gate that only opens after `/watch` has replied:
    if `/watch` awaited the task this test would time out."""
    w = worlds()
    w.gate = asyncio.Event()
    w.ready()
    it = await asyncio.wait_for(w.watch(), 5)
    assert it.text.startswith("Broadcasting game")
    for _ in range(5):
        await asyncio.sleep(0)
    assert not w.task().done()
    assert w.row().status is LIVE
    w.gate.set()
    await w.finish()
    assert w.row().status is FINISHED


@pytest.mark.parametrize(
    ("others", "max_concurrent", "ahead"),
    [(0, 4, 0), (3, 4, 0), (4, 4, 1), (6, 4, 3), (2, 1, 2)],
)
async def test_the_ahead_number_is_computed_once_at_admission(
    worlds, others, max_concurrent, ahead
):
    w = worlds(
        settings=BroadcastSettings(max_concurrent_broadcasts=max_concurrent, max_queue_depth=12)
    )
    for n in range(others):  # other users' places, admitted and not yet spawned
        w.bot.broadcasts.admit(league_id=LEAGUE, game_id=900 + n, user_id=500 + n)
    w.ready()
    it = await w.watch()
    placeholder = w.channel.threads[0].sent[0]["content"]
    if ahead == 0:
        assert placeholder == "Starting the broadcast…"
        assert it.text.endswith("It starts right away.")
    else:
        noun = "game" if ahead == 1 else "games"
        assert placeholder == f"Waiting for a broadcast slot — {ahead} {noun} ahead."
        assert it.text.endswith(f"{ahead} {noun} ahead of it — it starts when a slot frees up.")
    for n in range(others):
        w.bot.broadcasts.release_unspawned(LEAGUE, 900 + n)


# --- the root message ------------------------------------------------------------


async def test_a_missing_root_line_is_posted_persisted_then_edited_to_watching(worlds):
    """P5-97.v's repair path: a failed slate post left `root_message_id` NULL."""
    w = worlds()
    w.add_game(GID)
    w.add_row(GID, root=False)
    w.open_block(GID)
    await w.watch()
    assert [kw["content"] for _, kw in w.channel.ops("send")] == [MATCHUP]
    assert [(mid, kw["content"]) for mid, kw in w.channel.ops("edit")] == [(5000, WATCHING)]
    assert w.row().root_message_id == 5000
    assert w.channel.thread_requests[0]["message_id"] == 5000
    names = [name for name, _ in w.store.calls]
    assert names.index("set_root_message") < names.index("record_thread")


async def test_a_deleted_root_line_is_reposted_fresh(worlds):
    w = worlds()
    w.ready()
    w.channel.edit_errors[ROOT] = not_found()
    it = await w.watch()
    assert it.text.startswith("Broadcasting game")
    assert w.row().root_message_id == 5000
    assert w.channel.thread_requests[0]["message_id"] == 5000
    assert w.channel.content[5000] == WATCHING


# --- the state machine, keyed on the persisted row -----------------------------------


@pytest.mark.parametrize("in_block", [True, False], ids=["block-open", "after-restart"])
@pytest.mark.parametrize("status", [QUEUED, LIVE], ids=["queued", "live"])
async def test_a_queued_or_live_row_is_linked_and_nothing_is_started(worlds, status, in_block):
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID, status=status, thread_id=THREAD_BASE + 50, message_id=8050)
    if in_block:
        w.open_block(GID)
    it = await w.watch()
    assert it.text == f"Game {GID} is being broadcast — the thread is here: {URL_OF_THREAD_50}"
    _assert_started_nothing(w)
    _assert_no_score(it)


@pytest.mark.parametrize("in_block", [True, False], ids=["block-open", "after-restart"])
@pytest.mark.parametrize("status", [FINISHED, INTERRUPTED], ids=["finished", "interrupted"])
async def test_a_finished_row_links_the_thread_and_says_so(worlds, status, in_block):
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID, status=status, thread_id=THREAD_BASE + 50, message_id=8050)
    if in_block:
        w.open_block(GID)
    it = await w.watch()
    assert it.text == f"That broadcast already finished; the thread is here: {URL_OF_THREAD_50}"
    _assert_started_nothing(w)
    _assert_no_score(it)


def _assert_started_nothing(w: World) -> None:
    assert w.channel.calls == [] and w.channel.thread_requests == []
    assert w.bot.broadcasts.depth == 0
    assert w.inputs_calls == [] and w.store.called("record_thread") == []


async def test_a_second_watch_while_live_links_the_same_thread_and_starts_no_second_task(worlds):
    w = worlds()
    w.gate = asyncio.Event()
    w.ready()
    first = await w.watch(user_id=10)
    task = w.task()
    second = await w.watch(user_id=11)
    third = await w.watch(user_id=10)  # the same user, too
    thread = w.channel.threads[0]
    assert second.text == f"Game {GID} is being broadcast — the thread is here: {thread.jump_url}"
    assert third.text == second.text
    assert first.text.startswith("Broadcasting game")
    assert len(w.channel.threads) == 1 and len(w.channel.thread_requests) == 1
    assert w.task() is task and w.bot.broadcasts.depth == 1
    assert len(w.store.called("record_thread")) == 1


async def test_a_second_watch_after_the_broadcast_finished_does_not_rebroadcast(worlds):
    w = worlds()
    w.ready()
    await w.watch()
    await w.finish()
    edits_before = len(w.channel.threads[0].messages[0].calls)
    again = await w.watch(user_id=11)
    assert again.text == (
        f"That broadcast already finished; the thread is here: {w.channel.threads[0].jump_url}"
    )
    assert len(w.channel.threads) == 1
    assert len(w.channel.threads[0].messages[0].calls) == edits_before
    assert w.bot.broadcasts.depth == 0


async def test_two_concurrent_watches_of_one_game_make_one_thread_and_one_task(worlds):
    """Admit-first is what serialises them inside one process: the second
    coroutine reaches `admit` while the first is still creating the thread."""
    w = worlds()
    w.ready()
    a, b = await asyncio.gather(w.watch(user_id=10), w.watch(user_id=11))
    assert a.text.startswith("Broadcasting game")
    assert b.text == (
        f"Someone is starting game {GID}'s broadcast right now — run `/watch {GID}` "
        "again in a few seconds and you'll get its thread."
    )
    assert len(w.channel.thread_requests) == 1 and len(w.store.called("record_thread")) == 1
    assert w.bot.broadcasts.depth == 1


async def test_a_lost_record_thread_race_links_the_winner_and_discards_its_own_thread(
    worlds, monkeypatch
):
    """Another process's `/watch` recorded a thread first (`record_thread` -> 0):
    the loser must not admit-and-spawn, must link the WINNER's thread, and
    deletes the one it made."""
    w = worlds()
    w.ready()
    real = w.store.record_thread

    async def racing(ex, **kwargs):
        w.store.rows[GID] = replace(
            w.store.rows[GID], thread_id=THREAD_BASE + 99, message_id=8099, status=QUEUED
        )
        return await real(ex, **kwargs)

    monkeypatch.setattr(broadcast_repo, "record_thread", racing)
    it = await w.watch()

    assert it.text == (
        f"Game {GID} is being broadcast — the thread is here: "
        f"https://discord.com/channels/{GUILD_ID}/{THREAD_BASE + 99}"
    )
    assert w.channel.threads[0].deleted is True
    assert w.bot.broadcasts.live(LEAGUE, GID) is None
    assert w.bot.broadcasts.depth == 0 and not w.bot.broadcasts.is_live_or_queued(LEAGUE, GID)
    assert w.row().thread_id == THREAD_BASE + 99, "the winner's row is untouched"
    assert [name for name, _ in w.store.calls].count("transition_status") == 0


async def test_a_lost_race_still_links_when_the_thread_cannot_be_deleted(worlds, monkeypatch):
    w = worlds()
    w.ready()
    w.channel.thread_delete_error = forbidden()
    real = w.store.record_thread

    async def racing(ex, **kwargs):
        w.store.rows[GID] = replace(
            w.store.rows[GID], thread_id=THREAD_BASE + 99, message_id=8099, status=LIVE
        )
        return await real(ex, **kwargs)

    monkeypatch.setattr(broadcast_repo, "record_thread", racing)
    it = await w.watch()
    assert str(THREAD_BASE + 99) in it.text
    assert w.channel.threads[0].deleted is False
    assert w.bot.broadcasts.depth == 0


# --- a root message that already has a thread (F7) -------------------------------------
#
# Discord allows ONE thread per message and refuses a second (the 400 / code 160004 reply
# is modelled in `_watch_fakes._ThreadExists`, from memory of the API and NOT verified against
# a live gateway). So a thread that was made but never recorded -- `create_thread` timed out
# client-side after Discord made it, the cancel landed between `create_thread` and
# `record_thread`, or `_delete_thread` failed -- used to make every later `/watch` of that
# game fail permanently. It is adopted instead.


def _orphan(w: World) -> WatchThread:
    orphan = WatchThread(THREAD_BASE + 77, w.clock)
    w.channel.existing_threads[ROOT] = orphan
    return orphan


@pytest.mark.parametrize("where", ["cache", "fetch"])
async def test_a_root_that_already_has_a_thread_adopts_it_instead_of_creating_another(
    worlds, where
):
    w = worlds()
    w.ready()
    orphan = _orphan(w)
    if where == "cache":
        w.channel.cached_threads[ROOT] = orphan
    it = await w.watch()
    assert it.text.startswith("Broadcasting game") and orphan.jump_url in it.text
    assert w.channel.thread_requests == [], "create_thread must not be called for a threaded root"
    assert w.channel.thread_fetches == ([] if where == "cache" else [ROOT])
    assert len(orphan.sent) == 1 and orphan.sent[0]["content"] == "Starting the broadcast…"
    assert w.row().thread_id == orphan.id and w.row().status is QUEUED
    await w.finish()
    assert w.row().status is FINISHED and orphan.deleted is False


async def test_a_thread_discord_made_but_the_client_timed_out_on_is_adopted_by_the_next_watch(
    worlds,
):
    w = worlds()
    w.ready()
    w.channel.timeout_after_create = True
    first = await w.watch()
    assert first.text == GENERIC_FAILURE_MESSAGE
    assert w.row().thread_id is None and w.bot.broadcasts.depth == 0
    second = await w.watch()
    assert second.text.startswith("Broadcasting game"), second.text
    assert len(w.channel.threads) == 1
    assert len(w.channel.thread_requests) == 1, "the retry adopted the thread, it did not ask again"
    assert w.row().thread_id == w.channel.threads[0].id


async def test_a_cancel_between_create_thread_and_record_thread_does_not_strand_the_game(worlds):
    w = worlds()
    w.ready()

    async def cancelled(n, kwargs):
        raise asyncio.CancelledError

    w.store.hooks["record_thread"] = cancelled
    with pytest.raises(asyncio.CancelledError):
        await w.watch()
    assert len(w.channel.threads) == 1 and w.bot.broadcasts.depth == 0
    assert w.row().thread_id is None
    del w.store.hooks["record_thread"]
    second = await w.watch()
    assert second.text.startswith("Broadcasting game"), second.text
    assert len(w.channel.threads) == 1, "the orphan was adopted, not duplicated"
    assert w.row().thread_id == w.channel.threads[0].id


async def test_a_lost_record_thread_race_never_deletes_a_thread_it_adopted(worlds, monkeypatch):
    """The winner of that race holds the SAME thread (one per message), so
    deleting it would destroy the winner's broadcast."""
    w = worlds()
    w.ready()
    orphan = _orphan(w)
    real = w.store.record_thread

    async def racing(ex, **kwargs):
        w.store.rows[GID] = replace(
            w.store.rows[GID], thread_id=orphan.id, message_id=8099, status=QUEUED
        )
        return await real(ex, **kwargs)

    monkeypatch.setattr(broadcast_repo, "record_thread", racing)
    it = await w.watch()
    assert str(orphan.id) in it.text and orphan.deleted is False
    assert w.bot.broadcasts.depth == 0


async def test_a_failure_after_adopting_a_thread_leaves_it_for_the_next_try(worlds):
    w = worlds()
    w.ready()
    orphan = _orphan(w)
    orphan.send_error = forbidden()
    it = await w.watch()
    assert "send_messages_in_threads" in it.text
    assert orphan.deleted is False, "not ours: it was there before this /watch"
    assert w.bot.broadcasts.depth == 0


# --- what is not watchable (P5-97.p) ---------------------------------------------------


async def test_a_game_from_another_league_or_no_game_at_all_is_not_found(worlds):
    w = worlds()
    w.add_game(GID, league_id=2)  # a real game, in league 2
    w.add_row(
        GID, league_id=LEAGUE
    )  # (a cross-league row cannot exist; the check must not rely on it)
    w.open_block(GID)
    for game_id in (GID, 999):
        it = await w.watch(game_id)
        assert it.text == f"No game {game_id} in this league."
    assert w.boxscores == [] and w.channel.calls == [] and w.bot.broadcasts.depth == 0


async def test_a_game_not_yet_played_says_so_even_with_a_row_and_a_block(worlds):
    w = worlds()
    w.add_game(GID, status=GameStatus.SCHEDULED)
    w.add_row(GID)
    w.open_block(GID)
    it = await w.watch()
    assert it.text == (
        f"Game {GID} hasn't been played yet. It'll be watchable after the commissioner "
        "runs `/advance`."
    )
    assert w.bot.broadcasts.depth == 0 and w.channel.calls == []


async def test_a_played_game_in_no_block_is_not_watchable_and_shows_no_box_score(worlds):
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    it = await w.watch()
    assert "isn't watchable" in it.text and w.boxscores == []
    assert w.channel.calls == []
    _assert_no_score(it)


async def test_an_unrevealed_game_whose_block_is_gone_is_not_watchable(worlds):
    """After a restart memory is empty; a pending row has no replay to show and
    its result is not public yet."""
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID)
    it = await w.watch()
    assert it.text == (
        "That game isn't watchable — its replay is gone and its result hasn't been posted yet. "
        "It'll appear in #games when the block closes."
    )
    assert w.boxscores == [] and w.bot.broadcasts.depth == 0 and w.channel.calls == []
    _assert_no_score(it)


async def test_a_cap_degraded_game_is_not_watchable(worlds):
    """Edge (e): registered with `result=None` (the block holds at most
    `MAX_RESULTS_PER_BLOCK` replays)."""
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID)
    w.open_block(GID, degraded=(GID,))
    assert w.bot.blocks.get(LEAGUE, GID).result is None
    it = await w.watch()
    assert "isn't watchable" in it.text
    assert w.boxscores == [] and w.inputs_calls == [] and w.bot.broadcasts.depth == 0
    assert w.channel.calls == [] and w.channel.thread_requests == []
    _assert_no_score(it)


async def test_the_seventeenth_game_of_a_real_block_is_degraded_and_not_watchable(worlds):
    w = worlds()
    last = 17
    for game_id in range(1, last + 1):
        w.add_game(game_id, scores=DB_SCORES)
        w.add_row(game_id)
    w.open_block(*range(1, last + 1))
    assert w.bot.blocks.get(LEAGUE, last).result is None
    assert w.bot.blocks.get(LEAGUE, 16).result is not None
    it = await w.watch(last)
    assert "isn't watchable" in it.text and w.bot.broadcasts.depth == 0
    _assert_no_score(it)


async def test_a_revealed_game_with_no_replay_gets_the_box_score_fallback(worlds):
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID, posted=True)
    it = await w.watch()
    assert it.text == (
        "That game's replay is gone — it only lives in memory while its block is open. "
        "Here's the final box score."
    )
    assert it.embed is not None and it.reply["ephemeral"] is True
    assert w.boxscores == [GID] and w.channel.calls == [] and w.bot.broadcasts.depth == 0


async def test_an_orphan_pending_row_that_was_revealed_is_not_broadcast(worlds):
    """Edge (a): `pending`, no thread, `reveal_posted_at` set. Whether the block
    is gone (a restart) or, inconsistently, still in memory, the result is
    already public: take the revealed fallback, never start a broadcast."""
    for in_block in (False, True):
        w = worlds()
        w.add_game(GID, scores=DB_SCORES)
        w.add_row(GID, posted=True)
        if in_block:
            w.open_block(GID)
        it = await w.watch()
        assert it.text.endswith("Here's the final box score.")
        assert it.embed is not None and w.boxscores == [GID]
        _assert_started_nothing(w)
        assert w.row().status is PENDING and w.row().thread_id is None


async def test_a_claimed_but_unposted_reveal_is_not_broadcast_and_shows_no_score(worlds):
    """The reveal is in flight: the block is closed. Not a broadcast, and not
    yet a public result either, so no box score."""
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID, claimed=True)
    w.open_block(GID)  # even if memory still (wrongly) holds the game
    it = await w.watch()
    assert "isn't watchable" in it.text and w.boxscores == []
    # F3: the block is CLOSED (the claim only exists after it closed), so "when the block
    # closes" would be false; the truth is the result is still being posted.
    assert "when the block closes" not in it.text
    assert it.text == (
        "That game isn't watchable — its replay is gone, and its result is still being posted "
        "to #games. It will show up there once that finishes; if it does not, it is retried "
        "when the bot next reconnects or restarts."
    )
    _assert_started_nothing(w)
    _assert_no_score(it)


# --- refusals: nothing is created, a later /watch can retry (edge c) --------------------


async def test_a_full_scheduler_refuses_before_creating_anything_and_a_retry_succeeds(worlds):
    w = worlds(settings=BroadcastSettings(max_concurrent_broadcasts=1, max_queue_depth=0))
    w.ready()
    w.bot.broadcasts.admit(league_id=LEAGUE, game_id=900, user_id=77)  # takes the only place
    it = await w.watch()
    assert it.text == (
        f"Too many broadcasts are running right now, so game {GID} can't start yet. "
        f"Try `/watch {GID}` again in a few minutes — nothing was created."
    )
    _assert_started_nothing_but_the_other_place(w)
    assert w.row().status is PENDING and w.row().thread_id is None

    w.bot.broadcasts.release_unspawned(LEAGUE, 900)  # the place frees up
    retry = await w.watch()
    assert retry.text.startswith("Broadcasting game")
    assert w.row().status is QUEUED and w.row().thread_id is not None


async def test_the_per_user_cap_refuses_before_creating_anything_and_a_retry_succeeds(worlds):
    w = worlds(settings=BroadcastSettings(max_live_broadcasts_per_user=1))
    w.ready()
    w.bot.broadcasts.admit(league_id=LEAGUE, game_id=900, user_id=10)  # this user's one place
    it = await w.watch(user_id=10)
    assert it.text == (
        "You're already following 1 broadcasts, the most one person can run at once. "
        f"Once one finishes, `/watch {GID}` again — nothing was created."
    )
    _assert_started_nothing_but_the_other_place(w)
    assert w.row().status is PENDING

    w.bot.broadcasts.release_unspawned(LEAGUE, 900)
    assert (await w.watch(user_id=10)).text.startswith("Broadcasting game")


def _assert_started_nothing_but_the_other_place(w: World) -> None:
    assert w.channel.calls == [] and w.channel.thread_requests == []
    assert w.bot.broadcasts.depth == 1  # only the pre-admitted place
    assert not w.bot.broadcasts.is_live_or_queued(LEAGUE, GID)
    assert w.inputs_calls == [] and w.store.called("record_thread") == []


# --- Discord failures name the permission and tell the truth about which half ----------


def _assert_rolled_back(w: World) -> None:
    """After a failed start: the row is still `pending` with no thread (a later
    `/watch` can pick it up), the reservation is gone, and the root line is back
    to "not yet played"."""
    assert w.row().status is PENDING and w.row().thread_id is None
    assert w.bot.broadcasts.depth == 0 and not w.bot.broadcasts.is_live_or_queued(LEAGUE, GID)
    assert w.channel.content[ROOT] == MATCHUP


async def test_forbidden_creating_the_thread_names_create_public_threads(worlds):
    w = worlds()
    w.ready()
    w.channel.thread_error = forbidden()
    it = await w.watch()
    assert it.text == (
        "The bot can't create the thread: it is missing the `create_public_threads` "
        "permission in #games. Nothing was started."
    )
    _assert_rolled_back(w)
    assert w.store.called("record_thread") == []
    # the line went to "watching now" and was put back
    assert [kw["content"] for _, kw in w.channel.ops("edit")] == [WATCHING, MATCHUP]

    w.channel.thread_error = None  # the moderator fixed the permission
    retry = await w.watch()
    assert retry.text.startswith("Broadcasting game")
    assert w.row().status is QUEUED


async def test_forbidden_posting_in_the_thread_names_the_permissions_and_removes_the_thread(worlds):
    w = worlds()
    w.ready()
    w.channel.thread_send_error = forbidden()
    it = await w.watch()
    assert it.text == (
        "The thread was created but the bot can't post in it: it is missing "
        "`send_messages_in_threads` (and `embed_links` for the scoreboard). No broadcast "
        "started. The empty thread was removed."
    )
    assert w.channel.threads[0].deleted is True
    _assert_rolled_back(w)
    assert w.store.called("record_thread") == []


async def test_forbidden_posting_says_the_thread_is_left_when_it_cannot_be_deleted(worlds):
    w = worlds()
    w.ready()
    w.channel.thread_send_error = forbidden()
    w.channel.thread_delete_error = forbidden()
    it = await w.watch()
    assert "send_messages_in_threads" in it.text and "embed_links" in it.text
    assert it.text.endswith("The empty thread is still in #games — a moderator can delete it.")
    assert w.channel.threads[0].deleted is False
    _assert_rolled_back(w)


async def test_forbidden_editing_the_games_line_names_send_messages(worlds):
    w = worlds()
    w.ready()
    w.channel.fail_edit = {0: forbidden()}
    it = await w.watch()
    assert it.text == (
        "The bot can't post in #games: it is missing the `send_messages` permission there "
        "(and `embed_links`). Nothing was started."
    )
    assert w.channel.thread_requests == []
    assert w.row().status is PENDING and w.bot.broadcasts.depth == 0


async def test_forbidden_posting_a_missing_root_line_names_send_messages(worlds):
    w = worlds()
    w.add_game(GID)
    w.add_row(GID, root=False)
    w.open_block(GID)
    w.channel.fail_send = {0: forbidden()}
    it = await w.watch()
    assert "`send_messages`" in it.text and "Nothing was started." in it.text
    assert w.channel.thread_requests == [] and w.bot.broadcasts.depth == 0
    assert w.row().root_message_id is None and w.row().status is PENDING


async def test_a_league_with_no_games_channel_says_so_and_releases_its_place(worlds):
    w = worlds()
    w.ready()
    w.store.games_channel_id = None
    it = await w.watch()
    assert it.text == (
        "This league's #games channel isn't set up or can't be reached, so the broadcast "
        "thread can't be created. Nothing was started."
    )
    assert w.bot.broadcasts.depth == 0 and w.row().status is PENDING


async def test_a_block_closed_while_a_failing_watch_held_its_place_is_revealed_by_that_watch(
    worlds,
):
    """`close_block` skips a game it sees as still broadcasting. If the `/watch`
    holding that place then fails, nothing else would reveal the game (until the
    next restart) -- so the failure path hands it to `on_ended`."""
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID)
    w.open_block(GID)
    w.channel.thread_error = forbidden()

    async def close_the_block() -> None:
        report = await game_feed.close_block(w.bot, "pool", league_id=LEAGUE)
        assert report.still_broadcasting == (GID,)  # skipped: this /watch holds the place

    w.channel.on_create_thread = close_the_block
    it = await w.watch()
    assert "create_public_threads" in it.text
    assert w.row().reveal_posted_at is not None
    assert (
        w.channel.content[ROOT] == f"Game {GID} · AWY {DB_SCORES[1]} @ HOM {DB_SCORES[0]} — final"
    )
    assert w.bot.broadcasts.depth == 0


@pytest.mark.parametrize("step", ["inputs", "thread"])
async def test_an_unexpected_failure_is_logged_answered_generically_and_rolled_back(
    worlds, caplog, step
):
    w = worlds()
    w.ready()
    if step == "inputs":
        w.inputs_error = RuntimeError("connection reset")
    else:
        w.channel.thread_error = http_error("Discord is down")
    with caplog.at_level("ERROR"):
        it = await w.watch()
    assert it.text == GENERIC_FAILURE_MESSAGE
    assert any("watch failed" in r.getMessage() for r in caplog.records)
    _assert_rolled_back(w)
    assert w.store.called("record_thread") == []


async def test_a_known_domain_error_is_shown_verbatim(worlds):
    w = worlds()
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID, posted=True)
    original = cog_module.view_service.get_boxscore_panel

    async def missing(ex, *, game_id):
        raise NotFoundError(f"Game {game_id} references a missing team")

    cog_module.view_service.get_boxscore_panel = missing
    try:
        it = await w.watch()
    finally:
        cog_module.view_service.get_boxscore_panel = original
    assert it.text == f"Game {GID} references a missing team"


async def test_a_dm_or_unknown_guild_is_answered_ephemerally(worlds):
    w = worlds()
    w.resolve_error = NoLeagueForGuild("no league is set up for this server (guild 4242)")
    it = await w.watch()
    assert it.response.deferred == [True]
    assert it.text == "no league is set up for this server (guild 4242)"
    assert it.reply["ephemeral"] is True


async def test_watch_is_available_in_every_phase(worlds):
    for phase in Phase:
        w = worlds(phase=phase)
        w.add_game(GID, status=GameStatus.SCHEDULED)
        it = await w.watch()
        assert "hasn't been played yet" in it.text, phase


# --- the spoiler rule ---------------------------------------------------------------------

_UNREVEALED = {}


def _scenario(fn):
    _UNREVEALED[fn.__name__] = fn
    return fn


@_scenario
def not_played(w):
    w.add_game(GID, status=GameStatus.SCHEDULED)
    w.add_row(GID)
    w.open_block(GID)


@_scenario
def in_no_block(w):
    w.add_game(GID, scores=DB_SCORES)


@_scenario
def pending_after_restart(w):
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID)


@_scenario
def cap_degraded(w):
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID)
    w.open_block(GID, degraded=(GID,))


@_scenario
def reveal_claimed_not_posted(w):
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID, claimed=True)


@_scenario
def queued_link(w):
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID, status=QUEUED, thread_id=THREAD_BASE + 50, message_id=8050)
    w.open_block(GID)


@_scenario
def live_link(w):
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID, status=LIVE, thread_id=THREAD_BASE + 50, message_id=8050)


@_scenario
def finished_link(w):
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID, status=FINISHED, thread_id=THREAD_BASE + 50, message_id=8050)


@_scenario
def started(w):
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID)
    w.open_block(GID)


_SETTINGS = {"refused_full": BroadcastSettings(max_concurrent_broadcasts=1, max_queue_depth=0)}


@_scenario
def refused_full(w):
    started(w)
    w.bot.broadcasts.admit(league_id=LEAGUE, game_id=900, user_id=77)


@_scenario
def forbidden_thread(w):
    started(w)
    w.channel.thread_error = forbidden()


@_scenario
def forbidden_post(w):
    started(w)
    w.channel.thread_send_error = forbidden()


@_scenario
def unexpected_failure(w):
    started(w)
    w.inputs_error = RuntimeError("boom")


@pytest.mark.parametrize("name", sorted(_UNREVEALED))
async def test_no_reply_for_an_unrevealed_game_carries_a_score_or_an_embed(worlds, name):
    """The spoiler pin: `#games` is public, a result is posted only when its block
    closes, and the only reply that may name a score is the revealed fallback.
    Scans every reply of every other path -- including the ones that start a
    broadcast, whose thread the viewer chose to open."""
    w = worlds(settings=_SETTINGS.get(name))
    _UNREVEALED[name](w)
    it = await w.watch()
    assert it.replies, name
    _assert_no_score(it)
    assert w.boxscores == []


# --- registration, gate, and structural pins -----------------------------------------------


def test_the_watch_gate_is_all_phases_and_the_cog_is_registered():
    assert COMMAND_GATES["watch"] == ALL_PHASES
    assert "dba2.bot.cogs.broadcast" in bot_client._EXTENSIONS


async def test_the_command_takes_a_game_id_and_an_optional_speed_choice():
    """docs/design/watch-narration-v2.md WN-10.4: `/watch game_id speed:` -- the speed
    is optional (the default is `game`) and offers exactly the three speeds."""
    bot = bot_client.build_bot()
    await bot.setup_hook()
    commands_by_name = {c.name: c for c in bot.tree.get_commands()}
    game_id, speed = commands_by_name["watch"].parameters
    assert (game_id.name, game_id.required) == ("game_id", True)
    assert (speed.name, speed.required) == ("speed", False)
    assert [choice.value for choice in speed.choices] == ["fast", "game", "full"]


DBA2_DIR = Path(cog_module.__file__).resolve().parents[2]
COG = Path(cog_module.__file__).resolve()


def _calls_named(tree: ast.AST, attr: str) -> list[int]:
    return [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr
    ]


def _create_thread_sites(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found += [
            f"{path.relative_to(root).as_posix()}:{n}" for n in _calls_named(tree, "create_thread")
        ]
    return found


def test_create_thread_has_exactly_one_call_site_in_the_codebase():
    """P5-97.w: "create_thread is called at exactly one place in the codebase,
    guarded by thread_id IS NULL"."""
    sites = _create_thread_sites(DBA2_DIR)
    assert len(sites) == 1 and sites[0].startswith("bot/cogs/broadcast.py:"), sites


def test_the_create_thread_pin_has_teeth(tmp_path):
    planted = "async def f(m):\n    await m.create_thread(name='x')\n"
    (tmp_path / "sneaky.py").write_text(planted, encoding="utf-8")
    (tmp_path / "real.py").write_text(COG.read_text(encoding="utf-8"), encoding="utf-8")
    assert len(_create_thread_sites(tmp_path)) == 2


def _sends_and_edits_without_mentions(tree: ast.AST) -> list[int]:
    return [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("send", "edit")
        and "allowed_mentions" not in {k.arg for k in n.keywords}
    ]


def test_every_send_and_edit_in_the_cog_passes_allowed_mentions():
    tree = ast.parse(COG.read_text(encoding="utf-8"))
    sites = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("send", "edit")
    ]
    assert len(sites) >= 3, "the check must see the cog's real sends"
    assert _sends_and_edits_without_mentions(tree) == []


def test_the_mentions_pin_has_teeth():
    planted = (
        COG.read_text(encoding="utf-8")
        + "\nasync def leak(t):\n    await t.send(content='@everyone')\n"
    )
    assert _sends_and_edits_without_mentions(ast.parse(planted))


def test_the_cog_never_awaits_or_touches_the_broadcast_task():
    """P5-97.q item 4: `/watch` spawns and returns. It imports no `asyncio`, and
    never reads `.task` (the handle a caller could await)."""
    tree = ast.parse(COG.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import)
        for alias in n.names
    } | {
        (n.module or "").split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.level == 0
    }
    assert "asyncio" not in imported
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "task"]


# --- mutation twins ------------------------------------------------------------------------
# Each breaks a private copy of the real cog and demands the guarding scenario go red.
# The control (no mutation) must pass first, or "red" could be a loading failure.

_FALLBACK_GATE = "        if game_feed.box_score_is_public(row):\n"
_LEAGUE_CHECK = "if game is None or game.league_id != league.id:"
_CLAIM_GUARD = (
    "        if row.reveal_claimed_at is not None:\n"
    "            return await self._revealed_or_unwatchable(pool, row)\n"
)
_RECORD_GUARD = "                if recorded:\n"
_FORBIDDEN_RELEASE = (
    '                log.warning("game %s: /watch was refused a permission at step %s", '
    "game_id, step)\n                scheduler.release_unspawned(league_id, game_id)\n"
)


async def scenario_an_unrevealed_game_never_gets_a_box_score(module, worlds):
    for setup in (pending_after_restart, cap_degraded, in_no_block):
        w = worlds(module)
        setup(w)
        it = await w.watch()
        assert w.boxscores == [], f"{setup.__name__}: an unrevealed game was given a box score"
        assert it.embed is None
        _assert_no_score(it)


async def scenario_a_game_from_another_league_is_refused(module, worlds):
    w = worlds(module)
    w.add_game(GID, league_id=2, scores=DB_SCORES)
    w.add_row(GID, posted=True)  # even a revealed row must not open another league's game
    it = await w.watch()
    assert it.text == f"No game {GID} in this league."
    assert w.boxscores == [] and w.channel.calls == []


async def scenario_a_claimed_reveal_is_not_broadcast(module, worlds):
    w = worlds(module)
    w.add_game(GID, scores=DB_SCORES)
    w.add_row(GID, claimed=True)
    w.open_block(GID)
    it = await w.watch()
    assert "isn't watchable" in it.text
    _assert_started_nothing(w)


async def scenario_a_lost_race_spawns_nothing(module, worlds, monkeypatch):
    w = worlds(module)
    w.ready()
    real = w.store.record_thread

    async def racing(ex, **kwargs):
        w.store.rows[GID] = replace(
            w.store.rows[GID], thread_id=THREAD_BASE + 99, message_id=8099, status=QUEUED
        )
        return await real(ex, **kwargs)

    monkeypatch.setattr(broadcast_repo, "record_thread", racing)
    await w.watch()
    assert w.bot.broadcasts.live(LEAGUE, GID) is None, "the loser spawned a broadcast"
    assert w.bot.broadcasts.depth == 0


async def scenario_a_failed_start_gives_its_place_back(module, worlds):
    w = worlds(module)
    w.ready()
    w.channel.thread_error = forbidden()
    await w.watch()
    assert w.bot.broadcasts.depth == 0, "the failed /watch leaked its reservation"


async def test_twin_controls_every_scenario_passes_on_an_unmutated_copy(worlds, monkeypatch):
    control = cog_variant()
    await scenario_an_unrevealed_game_never_gets_a_box_score(control, worlds)
    await scenario_a_game_from_another_league_is_refused(control, worlds)
    await scenario_a_claimed_reveal_is_not_broadcast(control, worlds)
    await scenario_a_lost_race_spawns_nothing(control, worlds, monkeypatch)
    await scenario_a_failed_start_gives_its_place_back(control, worlds)


async def test_twin_without_the_reveal_gate_an_unrevealed_game_gets_a_box_score(worlds):
    mutated = cog_variant((_FALLBACK_GATE, "        if True:\n"))
    with pytest.raises(AssertionError, match="box score"):
        await scenario_an_unrevealed_game_never_gets_a_box_score(mutated, worlds)


async def test_twin_without_the_league_check_another_leagues_game_is_opened(worlds):
    mutated = cog_variant((_LEAGUE_CHECK, "if game is None:"))
    with pytest.raises(AssertionError):
        await scenario_a_game_from_another_league_is_refused(mutated, worlds)


async def test_twin_without_the_claim_guard_a_claimed_reveal_is_broadcast(worlds):
    mutated = cog_variant((_CLAIM_GUARD, "        if False:\n            pass\n"))
    with pytest.raises(AssertionError):
        await scenario_a_claimed_reveal_is_not_broadcast(mutated, worlds)


async def test_twin_without_the_recorded_check_the_race_loser_spawns(worlds, monkeypatch):
    mutated = cog_variant((_RECORD_GUARD, "                if True:\n"))
    with pytest.raises(AssertionError):
        await scenario_a_lost_race_spawns_nothing(mutated, worlds, monkeypatch)


async def test_twin_without_the_release_a_failed_start_leaks_its_place(worlds):
    mutated = cog_variant(
        (_FORBIDDEN_RELEASE, _FORBIDDEN_RELEASE.rsplit("                scheduler", 1)[0])
    )
    with pytest.raises(AssertionError, match="leaked"):
        await scenario_a_failed_start_gives_its_place_back(mutated, worlds)


# --- F7 twins: adopting an existing thread, and never deleting an adopted one -------------

_ADOPT = "                thread = await self._existing_thread(root, game_id)\n"
_DELETE_ONLY_IF_CREATED = (
    "                    if created:\n"
    "                        await self._delete_thread(thread)\n"
    "                    return await self._link_current(interaction, pool, league_id, game_id)\n"
)


async def scenario_an_existing_thread_is_adopted(module, worlds):
    w = worlds(module)
    w.ready()
    orphan = _orphan(w)
    it = await w.watch()
    assert it.text.startswith("Broadcasting game"), it.text
    assert w.channel.thread_requests == [], "created a second thread on a threaded message"
    assert w.row().thread_id == orphan.id


async def scenario_an_adopted_thread_survives_a_lost_race(module, worlds, monkeypatch):
    w = worlds(module)
    w.ready()
    orphan = _orphan(w)
    real = w.store.record_thread

    async def racing(ex, **kwargs):
        w.store.rows[GID] = replace(
            w.store.rows[GID], thread_id=orphan.id, message_id=8099, status=QUEUED
        )
        return await real(ex, **kwargs)

    monkeypatch.setattr(broadcast_repo, "record_thread", racing)
    await w.watch()
    assert orphan.deleted is False, "deleted the thread the race winner is broadcasting in"


async def test_twin_control_the_adoption_scenarios_pass_on_an_unmutated_copy(worlds, monkeypatch):
    control = cog_variant()
    await scenario_an_existing_thread_is_adopted(control, worlds)
    await scenario_an_adopted_thread_survives_a_lost_race(control, worlds, monkeypatch)


async def test_twin_without_the_adoption_lookup_a_threaded_root_cannot_be_watched(worlds):
    mutated = cog_variant((_ADOPT, "                thread = None\n"))
    with pytest.raises(AssertionError):
        await scenario_an_existing_thread_is_adopted(mutated, worlds)


async def test_twin_that_deletes_whatever_lost_the_race_destroys_an_adopted_thread(
    worlds, monkeypatch
):
    mutated = cog_variant(
        (_DELETE_ONLY_IF_CREATED, _DELETE_ONLY_IF_CREATED.replace("if created:", "if True:"))
    )
    with pytest.raises(AssertionError, match="deleted the thread"):
        await scenario_an_adopted_thread_survives_a_lost_race(mutated, worlds, monkeypatch)


# --- nits: public names only, and no bare assert on the link path ---------------------------


def _private_game_feed_names(tree: ast.AST) -> list[str]:
    return [
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "game_feed"
        and n.attr.startswith("_")
    ]


def test_the_cog_reaches_game_feed_only_through_its_public_names():
    assert _private_game_feed_names(ast.parse(COG.read_text(encoding="utf-8"))) == []


def test_the_private_name_pin_has_teeth():
    planted = COG.read_text(encoding="utf-8") + "\n\nx = game_feed._bounded\n"
    assert _private_game_feed_names(ast.parse(planted)) == ["_bounded"]


async def test_a_link_with_no_guild_or_no_thread_is_a_reply_not_an_assertion(worlds, caplog):
    w = worlds()
    row = w.add_row(GID, status=QUEUED, thread_id=THREAD_BASE + 5, message_id=8005)
    no_guild = w.cog._link_reply(FakeInteraction(guild_id=None), row)
    assert "only works inside a server" in no_guild.text
    threadless = w.cog._link_reply(FakeInteraction(), replace(row, thread_id=None))
    assert "no thread on record" in threadless.text
    assert any("no thread" in r.getMessage() for r in caplog.records)


# --- the pager (docs/design/watch-v3-spacing-and-pager.md W-11, W-13) --------------------------


async def _watch_and_open_the_pager(module, worlds) -> None:
    """`/watch` a game, let it finish, and open Q1's pager (via the full-text button,
    scoped by the shared `selected_period`, watch-v4 T-5.f) as a viewer in the same
    guild."""
    from dba2.bot.watch_pager import WatchFullTextButton
    from tests.unit.bot._pager_fakes import PagerInteraction

    w = worlds(module)
    w.ready()
    await w.watch()
    entry = w.bot.pagers.get(LEAGUE, GID)
    assert entry is not None and entry.guild_id == GUILD_ID
    await w.finish()
    entry.progress.component.selected_period = 1
    click = PagerInteraction(w.bot, guild_id=GUILD_ID)
    await WatchFullTextButton(LEAGUE, GID).callback(click)
    assert click.embed().title.startswith("Q1 · plays"), click.last()


async def test_watch_registers_the_pager_with_the_broadcasts_own_progress(worlds):
    w = worlds()
    w.ready()
    await w.watch()
    entry = w.bot.pagers.get(LEAGUE, GID)
    live = w.bot.broadcasts.live(LEAGUE, GID)
    assert entry.progress is live.progress  # the object the pacer mutates, not a copy
    assert entry.source.matchup == "AWY @ HOM"
    assert sorted(entry.source.periods) == [0, 1, 2, 3, 4, 5]  # 0 is the synthetic Full Game key
    await w.finish()
    assert entry.available_lines(1) == entry.source.periods[1].lines  # Q1 whole, at the end
    await _watch_and_open_the_pager(cog_module, worlds)


async def test_a_copied_progress_would_freeze_the_pager_and_is_caught(worlds):
    await _watch_and_open_the_pager(cog_variant(), worlds)  # the control
    copied = cog_variant(
        (
            "                    progress=live.progress,\n",
            "                    progress=type(live.progress)(),\n",
        )
    )
    with pytest.raises(AssertionError):
        await _watch_and_open_the_pager(copied, worlds)


async def test_the_broadcast_posts_only_the_placeholder_and_edits_the_one_component(worlds):
    """watch-v4: no period message exists any more. The thread's only POST is
    `/watch`'s own placeholder; the live component is a single message the pacer
    edits every tick, and every one of those edits after the first carries the
    cycle row's view (T-6: the pacer passes a view on every tick)."""
    w = worlds()
    w.ready()
    await w.watch()
    await w.finish()
    thread = w.channel.threads[0]
    assert len(thread.sent) == 1 and thread.sent[0].get("embed") is None  # the placeholder only
    entry = w.bot.pagers.get(LEAGUE, GID)
    assert entry is not None
    (message,) = thread.messages
    assert len(message.calls) > 1
    for kwargs, _at in message.calls:
        assert "view" in kwargs
        cycle_ids = {c.custom_id for c in kwargs["view"].children}
        assert f"dba2:watch:full:{LEAGUE}:{GID}" in cycle_ids
        assert f"dba2:watch:cycle:{LEAGUE}:{GID}:0" in cycle_ids  # Full is always there


async def test_a_pager_that_cannot_be_registered_costs_the_buttons_never_the_broadcast(
    worlds, caplog
):
    w = worlds()
    w.ready()

    def refuse(*_args, **_kwargs):
        raise RuntimeError("registry broke")

    w.bot.pagers.register = refuse
    it = await w.watch()
    assert it.text.startswith("Broadcasting game")
    await w.finish()
    assert w.row().status is FINISHED
    assert any("could not register the pager" in r.getMessage() for r in caplog.records)


async def test_the_pager_survives_the_block_closing_under_a_running_broadcast(worlds):
    """`close_block` skips a game it sees as still broadcasting: the pager must keep
    answering while plays arrive, and only degrade once the broadcast is over too."""
    from dba2.bot.watch_pager import game_is_open

    w = worlds()
    w.gate = asyncio.Event()
    w.ready()
    await w.watch()
    report = await game_feed.close_block(w.bot, "pool", league_id=LEAGUE)
    assert report.still_broadcasting == (GID,)
    assert game_is_open(w.bot, LEAGUE, GID)  # the block is closed; the broadcast is not over
    w.gate.set()
    await w.finish()
    assert not game_is_open(w.bot, LEAGUE, GID)  # both are over: the click degrades


# --- the viewer's speed (docs/design/watch-narration-v2.md WN-10.4) ------------------------


@pytest.mark.parametrize(
    ("speed", "plan", "tick"),
    [
        (None, GAME_PLAN_SETTINGS, 1.5),
        ("game", GAME_PLAN_SETTINGS, 1.5),
        ("fast", FAST_PLAN_SETTINGS, 1.5),
        ("full", FULL_PLAN_SETTINGS, 1.5),
    ],
)
async def test_the_chosen_speed_plans_and_paces_the_broadcast(
    worlds, monkeypatch, speed, plan, tick
):
    """`/watch speed:` picks the plan and the tick the job is spawned with; admission and
    every other knob stay the scheduler's. No choice is the `game` speed."""
    w = worlds()
    w.ready()
    spawned: list = []
    real_spawn = w.bot.broadcasts.spawn

    async def recording_spawn(*, job):
        spawned.append(job)
        return await real_spawn(job=job)

    monkeypatch.setattr(w.bot.broadcasts, "spawn", recording_spawn)
    await w.watch(speed=speed)
    (job,) = spawned
    assert job.plan.settings == plan
    assert job.settings.plan == plan and job.settings.tick_seconds == tick
    base = w.bot.broadcasts.settings
    assert replace(job.settings, plan=base.plan, tick_seconds=base.tick_seconds) == base
    assert job.narration.level is plan.narration
    await w.finish()
