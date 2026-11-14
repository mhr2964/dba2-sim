"""`game_feed.sweep_league` -- the startup sweep (Stage D commit 9,
docs/design/phase-5-stage-d-broadcast.md P5-97.q item 6, M4/M12).

The four `SweepShape`s, idempotence, one-bad-row isolation, league scoping, and
the rule that a sweep touches only what this process does not own. Fakes as in
`test_game_feed.py`; at startup the registry is empty and the scheduler idle,
which is the state every test here starts from unless it says otherwise.
"""

from __future__ import annotations

import pytest

from dba2.bot import game_feed
from dba2.data.repositories import broadcast_repo
from dba2.domain.broadcast import BroadcastStatus, SweepShape
from dba2.domain.game import GameStatus
from tests.unit.bot._game_feed_fakes import (
    LEAGUE,
    SCORES,
    FakeBot,
    FakeChannel,
    Store,
    assert_no_mentions,
    forbidden,
    open_in_memory,
)
from tests.unit.bot.test_game_feed import EX, ROOT, World, result_line

Outcome = game_feed.RevealOutcome
LIVE, QUEUED = BroadcastStatus.LIVE, BroadcastStatus.QUEUED
FINISHED, INTERRUPTED, PENDING = (
    BroadcastStatus.FINISHED,
    BroadcastStatus.INTERRUPTED,
    BroadcastStatus.PENDING,
)


@pytest.fixture
def world(monkeypatch) -> World:
    games = FakeChannel()
    thread = FakeChannel(7001, first_message_id=8000)
    store = Store().install(monkeypatch)
    for game_id in SCORES:
        store.add_game(game_id)
    return World(FakeBot(games, thread), games, thread, store)


def _sweep(world: World):
    return game_feed.sweep_league(world.bot, EX, league_id=LEAGUE)


@pytest.mark.parametrize("status", [LIVE, QUEUED])
async def test_an_interrupted_broadcast_gets_the_interrupted_panel_then_is_marked_then_revealed(
    world, status
):
    world.store.add_row(1, status=status, root_message_id=ROOT, thread_id=7001, message_id=8001)
    report = await _sweep(world)
    (entry,) = report.entries
    assert (entry.shape, entry.reveal) == (SweepShape.INTERRUPTED_BROADCAST, Outcome.REVEALED)
    # the broadcast message: the interrupted panel, from persisted data
    ((message_id, kwargs),) = world.thread.ops("edit")
    embed = kwargs["embed"]
    assert message_id == 8001 and kwargs["content"] is None
    assert "Broadcast interrupted" in embed.description
    assert embed.title == "AWY 117 - 131 HOM"
    assert "Line Score" in [f.name for f in embed.fields]
    assert_no_mentions(kwargs)
    # ... then the row moved, then the result was revealed in #games
    assert world.store.called("transition_status")[0]["expected"] is status
    row = world.store.row(1)
    assert row.status is INTERRUPTED and row.reveal_posted_at is not None
    assert world.games.content[ROOT] == result_line(1)


async def test_the_status_is_swapped_before_the_panel_is_edited_and_before_the_reveal(
    world, monkeypatch
):
    """The swap comes first so a broadcast that finished since the candidate list
    was read (the swap loses) never has its final frame overwritten."""
    order: list[str] = []
    world.store.add_row(1, status=LIVE, root_message_id=ROOT, thread_id=7001, message_id=8001)
    real_thread_edit = world.thread._edit
    real_games_edit = world.games._edit
    real_transition = world.store.transition_status

    async def thread_edit(message_id, kwargs):
        order.append("thread-edit")
        await real_thread_edit(message_id, kwargs)

    async def games_edit(message_id, kwargs):
        order.append("reveal")
        await real_games_edit(message_id, kwargs)

    async def transition(ex, **kwargs):
        order.append("transition")
        return await real_transition(ex, **kwargs)

    monkeypatch.setattr(world.thread, "_edit", thread_edit)
    monkeypatch.setattr(world.games, "_edit", games_edit)
    monkeypatch.setattr(broadcast_repo, "transition_status", transition)
    await _sweep(world)
    assert order == ["transition", "thread-edit", "reveal"]


async def test_an_interrupted_broadcast_whose_panel_edit_fails_is_still_marked_and_revealed(
    world, caplog
):
    world.store.add_row(1, status=LIVE, root_message_id=ROOT, thread_id=7001, message_id=8001)
    world.thread.edit_errors[8001] = forbidden()  # e.g. the thread has been archived
    report = await _sweep(world)
    assert report.entries[0].reveal is Outcome.REVEALED
    assert world.store.row(1).status is INTERRUPTED
    assert any("Forbidden" in r.getMessage() for r in caplog.records)


async def test_an_interrupted_row_that_never_got_its_panel_is_edited_by_the_sweep(world):
    """F2b: a graceful stop writes `live` -> `interrupted` (the finalizer) but the
    process may die before -- or Discord refuse -- the panel edit. On restart the
    row is `UNREVEALED_ENDED`, and the viewer's thread must not stay frozen."""
    world.store.add_row(
        1, status=INTERRUPTED, root_message_id=ROOT, thread_id=7001, message_id=8001
    )
    report = await _sweep(world)
    (entry,) = report.entries
    assert (entry.shape, entry.reveal) == (SweepShape.UNREVEALED_ENDED, Outcome.REVEALED)
    ((message_id, kwargs),) = world.thread.ops("edit")
    assert message_id == 8001 and kwargs["content"] is None
    assert "Broadcast interrupted" in kwargs["embed"].description
    assert_no_mentions(kwargs)
    assert world.store.called("transition_status") == [], "already interrupted: no swap"
    assert world.games.content[ROOT] == result_line(1)
    again = await _sweep(world)
    assert again.entries == () and len(world.thread.ops("edit")) == 1  # idempotent


async def test_that_panel_edit_is_best_effort(world, caplog):
    world.store.add_row(
        1, status=INTERRUPTED, root_message_id=ROOT, thread_id=7001, message_id=8001
    )
    world.thread.edit_errors[8001] = forbidden()
    report = await _sweep(world)
    assert report.entries[0].reveal is Outcome.REVEALED
    assert any("Forbidden" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("status", [FINISHED, PENDING])
async def test_a_row_that_did_not_end_interrupted_gets_no_panel_edit(world, status):
    world.store.add_row(1, status=status, root_message_id=ROOT, thread_id=7001, message_id=8001)
    await _sweep(world)
    assert world.thread.calls == [], "a finished broadcast's final frame must stay"


async def test_an_interrupted_row_with_no_thread_on_record_is_just_revealed(world):
    world.store.add_row(1, status=INTERRUPTED, root_message_id=ROOT)
    report = await _sweep(world)
    assert report.entries[0].reveal is Outcome.REVEALED and world.thread.calls == []


async def test_a_crashed_reveal_is_completed_without_claiming_again(world):
    world.store.add_row(1, status=FINISHED, root_message_id=ROOT, claimed=True)
    report = await _sweep(world)
    assert [(e.shape, e.reveal) for e in report.entries] == [
        (SweepShape.CRASHED_REVEAL, Outcome.REVEALED)
    ]
    assert world.store.called("claim_reveal") == []
    assert world.games.content[ROOT] == result_line(1)
    assert world.store.row(1).reveal_posted_at is not None


async def test_an_orphaned_pending_row_with_a_root_message_is_revealed_by_editing_it(world):
    world.store.add_row(1, status=PENDING, root_message_id=ROOT)
    report = await _sweep(world)
    assert [(e.shape, e.reveal) for e in report.entries] == [
        (SweepShape.ORPHANED_PENDING, Outcome.REVEALED)
    ]
    assert world.games.content[ROOT] == result_line(1) and world.games.ops("send") == []


async def test_an_orphaned_pending_row_with_no_root_message_is_posted_fresh(world):
    world.store.add_row(1, status=PENDING, root_message_id=None)
    await _sweep(world)
    assert [kw["content"] for _, kw in world.games.ops("send")] == [result_line(1)]
    assert world.games.ops("edit") == []
    assert world.store.row(1).root_message_id == 5000
    assert world.store.row(1).reveal_posted_at is not None


@pytest.mark.parametrize("status", [FINISHED, INTERRUPTED])
async def test_an_ended_but_never_revealed_row_is_revealed(world, status):
    world.store.add_row(1, status=status, root_message_id=ROOT)
    report = await _sweep(world)
    assert [(e.shape, e.reveal) for e in report.entries] == [
        (SweepShape.UNREVEALED_ENDED, Outcome.REVEALED)
    ]
    assert world.games.content[ROOT] == result_line(1)


async def test_a_second_sweep_posts_nothing_new(world):
    world.store.add_row(1, status=LIVE, root_message_id=ROOT, thread_id=7001, message_id=8001)
    world.store.add_row(2, status=PENDING, root_message_id=None)
    world.store.add_row(3, status=FINISHED, root_message_id=ROOT + 2, claimed=True)
    first = await _sweep(world)
    assert first.revealed == 3
    calls = (len(world.games.calls), len(world.thread.calls))
    second = await _sweep(world)
    assert second.entries == () and second.revealed == 0
    assert (len(world.games.calls), len(world.thread.calls)) == calls


async def test_one_bad_row_does_not_block_the_rest(world, caplog):
    world.store.add_row(1, status=LIVE, root_message_id=ROOT, thread_id=7001, message_id=8001)
    world.store.add_row(2, status=PENDING, root_message_id=ROOT + 1)
    world.store.add_row(3, status=FINISHED, root_message_id=ROOT + 2)
    world.store.raises["transition_status"] = RuntimeError("db hiccup")  # only game 1 needs it
    report = await _sweep(world)
    by_game = {e.game_id: e for e in report.entries}
    assert by_game[1].error and "RuntimeError" in by_game[1].error and by_game[1].reveal is None
    assert by_game[2].reveal is Outcome.REVEALED and by_game[3].reveal is Outcome.REVEALED
    assert report.failed == 1 and report.revealed == 2
    assert any("sweep: game 1" in r.getMessage() for r in caplog.records)
    assert world.store.row(1).reveal_claimed_at is None, "the failed row is left for the next sweep"


async def test_a_reveal_that_cannot_reach_discord_is_completed_by_the_next_sweep(world):
    world.store.add_row(1, status=PENDING, root_message_id=None)
    world.store.games_channel_id = None  # #games is missing / unresolvable this startup
    first = await _sweep(world)
    assert first.entries[0].reveal is Outcome.POST_FAILED
    assert world.store.row(1).reveal_claimed_at is not None
    assert world.store.row(1).reveal_posted_at is None
    world.store.games_channel_id = 9001  # the reconcile recreated it
    second = await _sweep(world)
    assert [(e.shape, e.reveal) for e in second.entries] == [
        (SweepShape.CRASHED_REVEAL, Outcome.REVEALED)
    ]
    assert len(world.games.ops("send")) == 1, "posted exactly once across both sweeps"


async def test_a_game_that_is_not_final_is_left_unclaimed(world):
    world.store.add_game(1, status=GameStatus.SCHEDULED)
    world.store.add_row(1, status=PENDING, root_message_id=ROOT)
    report = await _sweep(world)
    assert report.entries[0].reveal is Outcome.NOT_REVEALABLE
    assert world.store.row(1).reveal_claimed_at is None and world.games.calls == []


async def test_the_sweep_is_league_scoped(world):
    world.store.add_game(60, league_id=2)
    world.store.add_row(60, league_id=2, status=PENDING, root_message_id=ROOT + 9)
    world.store.add_row(1, status=PENDING, root_message_id=ROOT)
    report = await _sweep(world)
    assert [e.game_id for e in report.entries] == [1]
    assert world.store.row(60).reveal_claimed_at is None


async def test_a_sweep_leaves_a_broadcast_this_process_still_owns_and_an_open_block_alone(world):
    """`on_ready` re-fires on a gateway reconnect, mid-process: the sweep must not
    interrupt a live broadcast or reveal a game in a block that is still open."""
    world.store.add_row(1, status=LIVE, root_message_id=ROOT, thread_id=7001, message_id=8001)
    world.scheduler.live.add((LEAGUE, 1))
    world.store.add_row(2, status=PENDING, root_message_id=ROOT + 1)
    open_in_memory(world.bot, 2)
    world.store.add_row(3, status=PENDING, root_message_id=ROOT + 2)  # in no open block
    report = await _sweep(world)
    by_game = {e.game_id: e for e in report.entries}
    assert by_game[1].skipped_owned and by_game[2].skipped_owned
    assert by_game[3].reveal is Outcome.REVEALED and not by_game[3].skipped_owned
    assert world.thread.calls == []
    assert world.store.row(1).status is LIVE and world.store.row(2).reveal_claimed_at is None
    assert world.games.content.get(ROOT) is None and world.games.content.get(ROOT + 1) is None


async def test_every_sweep_send_and_edit_carries_no_mentions(world):
    world.store.add_row(1, status=LIVE, root_message_id=ROOT, thread_id=7001, message_id=8001)
    world.store.add_row(2, status=PENDING, root_message_id=None)
    await _sweep(world)
    assert world.games.calls and world.thread.calls
    for _, _, kwargs in [*world.games.calls, *world.thread.calls]:
        assert_no_mentions(kwargs)
