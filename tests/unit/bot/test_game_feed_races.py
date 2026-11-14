"""`game_feed` under interleaving (the Stage D fix round, docs/design/
phase-5-stage-d-broadcast.md P5-97.m/.q).

The other `game_feed` tests run on fakes that never suspend, so an await that is
slow in production -- a database round trip, a Discord edit -- was never slow
here and no two of these coroutines ever interleaved. Every scenario in this
file makes ONE call point slow with a `Hook` from `_game_feed_fakes` (`slow`,
`ticks`) and lets a second coroutine run in the gap. Each was written red first
against the code that did the wrong thing; the fix is named in its docstring,
and each has a mutation twin (a private copy of `game_feed` with the fix undone
must fail it).

The invariant under test (P5-97.m): a game's score is public in `#games` only
when its block is closed AND it has no live, queued or admitted broadcast.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from dba2.bot import game_feed
from dba2.bot.broadcast import Admission, BroadcastScheduler, BroadcastSettings
from dba2.data.repositories import broadcast_repo
from dba2.domain.broadcast import BroadcastStatus, SweepShape
from tests.unit.bot._game_feed_fakes import (
    LEAGUE,
    SCORES,
    FakeBot,
    FakeChannel,
    Store,
    open_in_memory,
    slow,
    ticks,
    variant,
)
from tests.unit.bot.test_game_feed import EX, ROOT, World, matchup, result_line

INTERRUPTED, FINISHED = BroadcastStatus.INTERRUPTED, BroadcastStatus.FINISHED
Outcome = game_feed.RevealOutcome


def make_world(monkeypatch) -> World:
    """A world whose bot holds the REAL `BroadcastScheduler`, so `admit` and
    `release_unspawned` are the production answers to "is anyone broadcasting?"."""
    games = FakeChannel()
    thread = FakeChannel(7001, first_message_id=8000)
    store = Store().install(monkeypatch)
    for game_id in SCORES:
        store.add_game(game_id)
    bot = FakeBot(games, thread)

    async def never(**_kwargs):
        raise AssertionError("no broadcast task runs in these tests")

    bot.broadcasts = BroadcastScheduler(BroadcastSettings(), on_ended=never)
    return World(bot, games, thread, store)


@pytest.fixture
def world(monkeypatch) -> World:
    return make_world(monkeypatch)


def _ended(world: World, game_id: int = 1, status: BroadcastStatus = INTERRUPTED, module=game_feed):
    return module.on_ended(world.bot, league_id=LEAGUE, game_id=game_id, status=status, executor=EX)


def _admit(world: World, user_id: int, game_id: int = 1) -> None:
    assert (
        world.bot.broadcasts.admit(league_id=LEAGUE, game_id=game_id, user_id=user_id)
        is Admission.ADMITTED
    )


# --- F1: the reveal predicate is shared and includes "admitted" ---------------


async def scenario_a_late_on_ended_does_not_reveal_a_game_another_watch_holds(
    module, world: World, monkeypatch
) -> None:
    """A's `/watch` failed after `admit`, so its `on_ended` restores the line
    (one slow database read). Meanwhile B is admitted and the block closes:
    `close_block` correctly leaves the game to B. A's `on_ended` used to ask
    only "is the block closed?" on its second look and revealed the score to
    `#games` while B was still watching. Fix: one predicate, `_may_reveal`."""
    world.store.add_row(1, root_message_id=ROOT)
    open_in_memory(world.bot, 1)
    world.store.hooks["get_game"] = slow(20, only_call=0)  # the restore's load
    _admit(world, 10)
    world.bot.broadcasts.release_unspawned(LEAGUE, 1)  # A's /watch failed after admit
    ended = asyncio.create_task(_ended(world, module=module))
    await ticks(2)
    _admit(world, 11)  # B is admitted while A is still restoring
    report = await module.close_block(world.bot, EX, league_id=LEAGUE)
    assert report.still_broadcasting == (1,) and report.outcomes == {}
    await ended
    assert world.leaked_scores() == [], "the score reached #games while B held the game"
    assert world.store.row(1).reveal_claimed_at is None
    assert world.bot.broadcasts.is_live_or_queued(LEAGUE, 1)

    # B's broadcast ends; ITS on_ended is the one that reveals, exactly once.
    world.bot.broadcasts.release_unspawned(LEAGUE, 1)
    await _ended(world, status=FINISHED, module=module)
    assert world.games.content[ROOT] == result_line(1)
    assert [kw["content"] for _, kw in world.games.ops("edit")].count(result_line(1)) == 1


async def test_an_on_ended_for_a_closed_block_reveals_nothing_while_another_broadcast_holds_it(
    world,
):
    world.store.add_row(1, root_message_id=ROOT)
    _admit(world, 11)  # block already closed (none registered), B admitted
    await _ended(world)
    assert world.leaked_scores() == [] and world.store.row(1).reveal_claimed_at is None
    assert world.games.calls == [], "nor may it restore the line B shows as 'watching now'"


async def test_a_sweep_never_reveals_a_game_a_broadcast_holds(world):
    world.store.add_row(1, status=BroadcastStatus.PENDING, root_message_id=ROOT)
    _admit(world, 11)
    report = await game_feed.sweep_league(world.bot, EX, league_id=LEAGUE)
    assert [e.skipped_owned for e in report.entries] == [True]
    assert world.leaked_scores() == []


# --- F4: a restore never overwrites what a reveal or a newer broadcast wrote --


async def scenario_a_slow_restore_does_not_overwrite_a_result_close_block_posted(
    module, world: World, monkeypatch
) -> None:
    """`on_ended` (block still open) is loading the row to restore the matchup
    line when the block closes and `close_block` reveals the game. The restore's
    edit used to land AFTER the result and put "not yet played" back for good --
    the row is marked posted, so nothing would ever repair it. Fix: the restore
    holds the game's lock and re-checks the block after its last await."""
    world.store.add_row(1, status=FINISHED, root_message_id=ROOT)
    open_in_memory(world.bot, 1)
    world.store.hooks["get_game"] = slow(20, only_call=0)  # the restore's load
    ended = asyncio.create_task(_ended(world, status=FINISHED, module=module))
    await ticks(2)
    report = await module.close_block(world.bot, EX, league_id=LEAGUE)
    await ended
    assert report.outcomes == {1: Outcome.REVEALED}
    assert world.games.content[ROOT] == result_line(1)
    assert world.games.content[ROOT] != matchup(1)
    assert world.store.row(1).reveal_posted_at is not None
    # The lock alone already makes the result win (the reveal waits, then edits
    # last). What the recheck adds is that the restore does not write at all: it
    # saw the block close during its load, so it never puts "not yet played" up.
    assert [kw["content"] for _, kw in world.games.ops("edit")] == [result_line(1)], (
        "the restore wrote a line the block's close had already handed to the reveal"
    )


async def scenario_a_restore_edit_already_in_flight_is_not_overtaken_by_the_reveal(
    module, world: World, monkeypatch
) -> None:
    """The restore has passed its recheck and its Discord edit is SLOW and
    already in flight when `close_block` reveals the game. The recheck cannot
    help here (it ran before the block closed); only the per-game lock can: the
    reveal waits for the restore's edit, then edits last, so the line ends on the
    result. Without the lock the reveal's (fast) edit lands first and the
    restore's stale "not yet played" lands after it, for good -- the row is
    marked posted, so nothing repairs it."""
    world.store.add_row(1, status=FINISHED, root_message_id=ROOT)
    open_in_memory(world.bot, 1)
    world.games.hooks["edit"] = slow(20, only_call=0)  # the restore's own edit
    ended = asyncio.create_task(_ended(world, status=FINISHED, module=module))
    for _ in range(10):  # until the restore is inside its edit
        if world.games.ops("edit"):
            break
        await ticks(1)
    assert [kw["content"] for _, kw in world.games.ops("edit")] == [matchup(1)], (
        "the restore's edit is not in flight; the scenario is not set up"
    )
    assert world.games.content.get(ROOT) != matchup(1), "the restore's edit already landed"
    report = await module.close_block(world.bot, EX, league_id=LEAGUE)
    await ended
    assert report.outcomes == {1: Outcome.REVEALED}
    assert world.games.content[ROOT] == result_line(1), "the restore landed after the result"
    assert [kw["content"] for _, kw in world.games.ops("edit")] == [matchup(1), result_line(1)]
    assert world.store.row(1).reveal_posted_at is not None


async def scenario_a_restore_does_not_overwrite_the_line_of_a_newer_broadcast(
    module, world: World, monkeypatch
) -> None:
    world.store.add_row(1, status=FINISHED, root_message_id=ROOT)
    open_in_memory(world.bot, 1)
    world.store.hooks["get_game"] = slow(20, only_call=0)
    ended = asyncio.create_task(_ended(world, status=FINISHED, module=module))
    await ticks(2)
    _admit(world, 11)  # a new /watch took the game while the restore was loading
    await ended
    assert world.games.calls == [], "the restore edited a line a live broadcast now owns"


# --- F5: the sweep swaps the status first and edits only if it won ------------


async def scenario_a_sweep_leaves_the_panel_of_a_broadcast_that_finished_since_the_list(
    module, world: World, monkeypatch
) -> None:
    """The candidate list is a snapshot: a broadcast that finished after it was
    read is no longer `live`. The sweep used to edit its message to the
    interrupted panel first and ignore the failed status swap, clobbering the
    final frame."""
    world.store.add_row(
        1, status=BroadcastStatus.LIVE, root_message_id=ROOT, thread_id=7001, message_id=8001
    )
    real_list = world.store.list_sweep_candidates

    async def list_then_finish(ex, *, league_id):
        rows = await real_list(ex, league_id=league_id)
        await world.store.transition_status(
            ex, league_id=league_id, game_id=1, expected=BroadcastStatus.LIVE, new=FINISHED
        )
        return rows

    monkeypatch.setattr(broadcast_repo, "list_sweep_candidates", list_then_finish)
    report = await module.sweep_league(world.bot, EX, league_id=LEAGUE)
    assert world.thread.ops("edit") == [], (
        "the final frame was overwritten by the interrupted panel"
    )
    assert world.store.row(1).status is FINISHED
    (entry,) = report.entries
    assert entry.shape is SweepShape.INTERRUPTED_BROADCAST and entry.skipped_owned


# --- F6: the sweep does not resume a claim whose claimant is mid-flight -------


async def scenario_a_reconnect_sweep_does_not_double_post_a_reveal_in_flight(
    module, world: World, monkeypatch
) -> None:
    """`on_ready` re-fires on a gateway reconnect. A reveal that has claimed the
    row and is still waiting on Discord looks exactly like a crashed reveal to
    the sweep (`claimed`, not `posted`); resuming it posted the result twice.

    Two rules meet here and each owns a different assertion. The per-game LOCK
    prevents the duplicate post (`len(send) == 1`): a resume would queue behind
    the reveal and find the row already posted. The `_reveal_in_flight`
    predicate only decides what the sweep REPORTS and whether it queues a
    resume at all (`skipped_owned`). This scenario's twin undoes the predicate,
    so it goes red on the report line, not on the send count; the two tests
    below it pin each claim separately."""
    world.store.add_row(1, status=FINISHED, root_message_id=None)  # a fresh post is not idempotent
    world.games.hooks["send"] = slow(20, only_call=0)
    reveal = asyncio.create_task(module.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1))
    await ticks(3)  # claimed, and inside the slow send
    assert world.store.row(1).reveal_claimed_at is not None
    assert world.store.row(1).reveal_posted_at is None
    report = await module.sweep_league(world.bot, EX, league_id=LEAGUE)
    assert await reveal == Outcome.REVEALED  # `==`: a twin has its own enum class
    assert [(e.shape, e.skipped_owned) for e in report.entries] == [
        (SweepShape.CRASHED_REVEAL, True)
    ]
    assert len(world.games.ops("send")) == 1, "the result was posted twice"
    assert world.store.row(1).reveal_posted_at is not None


async def test_two_concurrent_reveals_of_one_game_post_once(world):
    world.store.add_row(1, status=FINISHED, root_message_id=ROOT)
    world.games.hooks["edit"] = slow(20, only_call=0)
    first = asyncio.create_task(game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1))
    await ticks(3)
    second = await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert (await first, second) == (Outcome.REVEALED, Outcome.NOT_OWNED)
    assert len(world.games.ops("edit")) == 1


async def test_the_per_game_lock_is_dropped_when_the_work_is_done(world):
    world.store.add_row(1, status=FINISHED, root_message_id=ROOT)
    await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert game_feed._guards == {} and not game_feed._reveal_in_flight(LEAGUE, 1)


async def _sweep_during_a_slow_first_post(module, world: World):
    """A reveal that must post fresh is inside its slow `send` when the sweep
    runs; returns the sweep report after the reveal finished."""
    world.store.add_row(1, status=FINISHED, root_message_id=None)
    world.games.hooks["send"] = slow(20, only_call=0)
    reveal = asyncio.create_task(module.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1))
    await ticks(3)
    assert world.store.row(1).reveal_claimed_at is not None
    report = await module.sweep_league(world.bot, EX, league_id=LEAGUE)
    await reveal
    return report


async def test_the_lock_alone_keeps_the_sweep_from_double_posting(monkeypatch):
    """`_reveal_in_flight` blind, lock kept: the sweep does try to resume the
    reveal, but it queues behind the lock, finds the row posted and posts
    nothing. Only the report differs."""
    mutated = variant((_IN_FLIGHT, "    return False\n"))
    world = make_world(monkeypatch)
    report = await _sweep_during_a_slow_first_post(mutated, world)
    assert len(world.games.ops("send")) == 1
    assert world.store.row(1).reveal_posted_at is not None
    assert [e.skipped_owned for e in report.entries] == [False]


async def test_without_the_lock_the_sweep_double_posts_a_reveal_in_flight(monkeypatch):
    """The reveal's lock removed: `_guards` is never filled, so the sweep also
    cannot see the reveal in flight, resumes it, and the result is posted twice."""
    mutated = variant(
        (_REVEAL_LOCK, _REVEAL_LOCK.replace("_exclusive(league_id, game_id)", "_nolock()"))
    )
    mutated._nolock = contextlib.nullcontext  # type: ignore[attr-defined]
    world = make_world(monkeypatch)
    await _sweep_during_a_slow_first_post(mutated, world)
    assert len(world.games.ops("send")) == 2


# --- the scenarios on the real module, then on twins with the fix undone -------

_SECOND_LOOK = (
    "        if _may_reveal(bot, league_id, game_id):\n"
    "            await reveal_game(bot, ex, league_id=league_id, game_id=game_id)\n"
)
_RESTORE_RECHECK = (
    "            if bot.broadcasts.is_live_or_queued(league_id, game_id) or bot.blocks.is_closed(\n"
    "                league_id, game_id\n"
    "            ):\n"
    "                return\n"
)
_RESTORE_LOCK = (
    "    async with _exclusive(league_id, game_id):\n"
    "        row = await broadcast_repo.get_broadcast(ex, league_id=league_id, game_id=game_id)\n"
)
_REVEAL_LOCK = (
    "    async with _exclusive(league_id, game_id):\n"
    "        try:\n"
    "            persisted = await _load_persisted_game(ex, league_id, game_id)\n"
)
_SWEEP_CAS_RESULT = "                if not moved:\n"
_IN_FLIGHT = "    return (league_id, game_id) in _guards\n"

_SCENARIOS = [
    scenario_a_late_on_ended_does_not_reveal_a_game_another_watch_holds,
    scenario_a_slow_restore_does_not_overwrite_a_result_close_block_posted,
    scenario_a_restore_edit_already_in_flight_is_not_overtaken_by_the_reveal,
    scenario_a_restore_does_not_overwrite_the_line_of_a_newer_broadcast,
    scenario_a_sweep_leaves_the_panel_of_a_broadcast_that_finished_since_the_list,
    scenario_a_reconnect_sweep_does_not_double_post_a_reveal_in_flight,
]
_SCENARIO_IDS = [
    "late-on-ended",
    "slow-restore-vs-close",
    "restore-edit-in-flight-vs-close",
    "restore-vs-newer",
    "sweep-cas",
    "in-flight",
]


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=_SCENARIO_IDS)
async def test_the_scenario_passes_on_the_real_module_and_an_unmutated_copy(scenario, monkeypatch):
    for module in (game_feed, variant()):
        await scenario(module, make_world(monkeypatch), monkeypatch)


_RECHECK_OFF = (_RESTORE_RECHECK, "            pass\n")
_LOCK_OFF = (_RESTORE_LOCK, _RESTORE_LOCK.replace("_exclusive(league_id, game_id)", "_nolock()"))

# (scenario, mutations, id) -- each undoes exactly the rule the scenario is about.
# The restore has two independent rules, the lock and the recheck; each has a
# scenario that only its own removal turns red (and a "both undone" twin).
_TWINS = [
    (  # on_ended's second look asks only "is the block closed?" again
        scenario_a_late_on_ended_does_not_reveal_a_game_another_watch_holds,
        [(_SECOND_LOOK, _SECOND_LOOK.replace("_may_reveal(bot, league_id, game_id)", "True"))],
        "late-on-ended",
    ),
    (  # the restore neither locks nor re-checks
        scenario_a_slow_restore_does_not_overwrite_a_result_close_block_posted,
        [_RECHECK_OFF, _LOCK_OFF],
        "slow-restore-vs-close/no-lock-no-recheck",
    ),
    (  # the recheck alone: the restore writes a line the close already handed on
        scenario_a_slow_restore_does_not_overwrite_a_result_close_block_posted,
        [_RECHECK_OFF],
        "slow-restore-vs-close/recheck-only",
    ),
    (  # the lock alone: a slow edit already in flight lands after the result
        scenario_a_restore_edit_already_in_flight_is_not_overtaken_by_the_reveal,
        [_LOCK_OFF],
        "restore-edit-in-flight-vs-close/lock-only",
    ),
    (  # the restore does not look for a newer broadcast after its last await
        scenario_a_restore_does_not_overwrite_the_line_of_a_newer_broadcast,
        [_RECHECK_OFF],
        "restore-vs-newer",
    ),
    (  # the sweep ignores the status swap's result
        scenario_a_sweep_leaves_the_panel_of_a_broadcast_that_finished_since_the_list,
        [(_SWEEP_CAS_RESULT, "                if False:\n")],
        "sweep-cas",
    ),
    (  # the sweep cannot see a reveal in flight (report only; see the F6 docstring)
        scenario_a_reconnect_sweep_does_not_double_post_a_reveal_in_flight,
        [(_IN_FLIGHT, "    return False\n")],
        "in-flight",
    ),
]


@pytest.mark.parametrize(
    ("scenario", "mutations"), [t[:2] for t in _TWINS], ids=[t[2] for t in _TWINS]
)
async def test_twin_with_the_fix_undone_the_scenario_goes_red(scenario, mutations, monkeypatch):
    mutated = variant(*mutations)
    # `_nolock` stands in for the lock in the lock twins: locks nothing.
    mutated._nolock = contextlib.nullcontext  # type: ignore[attr-defined]
    with pytest.raises(AssertionError):
        await scenario(mutated, make_world(monkeypatch), monkeypatch)


# The other half of the 2x2: undoing ONE restore rule leaves the other rule's
# scenario green, so each scenario pins exactly the rule its twin names.
_ORTHOGONAL = [
    (scenario_a_restore_edit_already_in_flight_is_not_overtaken_by_the_reveal, [_RECHECK_OFF]),
    (scenario_a_slow_restore_does_not_overwrite_a_result_close_block_posted, [_LOCK_OFF]),
]


@pytest.mark.parametrize(
    ("scenario", "mutations"),
    _ORTHOGONAL,
    ids=["in-flight-edit-without-recheck", "slow-restore-without-lock"],
)
async def test_undoing_the_other_restore_rule_leaves_the_scenario_green(
    scenario, mutations, monkeypatch
):
    mutated = variant(*mutations)
    mutated._nolock = contextlib.nullcontext  # type: ignore[attr-defined]
    await scenario(mutated, make_world(monkeypatch), monkeypatch)
