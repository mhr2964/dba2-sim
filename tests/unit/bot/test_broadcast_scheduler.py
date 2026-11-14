"""`BroadcastScheduler`, its admission rules and the guarded task (Stage D
commit 7, docs/design/phase-5-stage-d-broadcast.md §6 P5-97.j/.q, §10 Tier 2).

No DB and no gateway: `broadcast_repo.transition_status` is replaced by a
recorder (`Repo`), `on_ended` by a list, the message/thread by the fakes. The
tasks run on a constant fake clock and a yield-only sleep, so a 45-frame
broadcast is instant and never skips a frame; the pacer's own timing is
`test_broadcast_pacer.py`'s business.

What is deliberately NOT here: the proof that the startup sweep converges a
row a dead task left `live` or `queued`. That is `game_feed.sweep_league`'s
(commit 9), and `test_a_task_cancelled_before_its_first_step_writes_nothing`
below pins the honest half of it -- this module does not claim its finalizer
always runs.

Mutation twins are scenario functions of the module under test, run on the real
module and, expected to fail, on a `variant()` with one rule deleted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from dba2.bot import broadcast
from dba2.data.repositories import broadcast_repo
from dba2.domain.broadcast import BroadcastStatus
from tests.unit.bot._broadcast_fakes import (
    FakeMessage,
    FakeThread,
    assert_no_mentions,
    failing,
    http_error,
    make_inputs,
    plan_for,
    variant,
)

LEAGUE = 1
QUEUED, LIVE = BroadcastStatus.QUEUED, BroadcastStatus.LIVE
FINISHED, INTERRUPTED = BroadcastStatus.FINISHED, BroadcastStatus.INTERRUPTED
FRAMES = len(plan_for(6).frames)  # 45


@dataclass
class Repo:
    """Stands in for `broadcast_repo.transition_status`: records every call in
    `events` (shared with `on_ended`, so ordering is checkable) and answers
    from `answers`, defaulting to 1 (the CAS matched)."""

    events: list = field(default_factory=list)
    answers: dict = field(default_factory=dict)
    raises: dict = field(default_factory=dict)
    gates: dict = field(default_factory=dict)
    """`(expected, new)` -> an `asyncio.Event` the write waits on before it answers."""

    async def transition_status(self, ex, *, league_id, game_id, expected, new):
        self.events.append(("transition", game_id, expected, new))
        await asyncio.sleep(0)
        if (expected, new) in self.gates:
            await self.gates[(expected, new)].wait()
        if (expected, new) in self.raises:
            raise self.raises[(expected, new)]
        return self.answers.get((expected, new), 1)

    def transitions(self, game_id: int | None = None):
        return [
            (e[2], e[3]) for e in self.events if e[0] == "transition" and game_id in (None, e[1])
        ]


@pytest.fixture
def repo(monkeypatch):
    fake = Repo()
    monkeypatch.setattr(broadcast_repo, "transition_status", fake.transition_status)
    return fake


async def _yield(_seconds: float) -> None:
    await asyncio.sleep(0)


class Harness:
    """A scheduler wired to fakes, plus the jobs it was given."""

    def __init__(self, module, repo: Repo, *, on_ended=None, **settings) -> None:
        self.module = module
        self.repo = repo

        async def record_end(*, league_id, game_id, status):
            self.repo.events.append(("ended", game_id, status))

        self.scheduler = module.BroadcastScheduler(
            module.BroadcastSettings(**settings),
            on_ended=on_ended or record_end,
            clock=lambda: 0.0,
            sleep=_yield,
        )
        self.messages: dict[int, FakeMessage] = {}
        self.threads: dict[int, FakeThread] = {}
        self.active: set[int] = set()
        self.max_active = 0

    def job(
        self, game_id: int, user_id: int = 10, *, script=None, plan=None, message=None, fail_if=None
    ):
        """`fail_if(request)` fails chosen requests of the scoreboard, the period
        messages and the recap alike; request numbers count them all in order
        (the fakes share one ledger)."""
        thread = FakeThread(thread_id=2000 + game_id, fail_if=fail_if)
        message = message or FakeMessage(
            on_call=self._track(game_id, script),
            message_id=1000 + game_id,
            ledger=thread.ledger,
            fail_if=fail_if,
        )
        self.messages[game_id] = message
        self.threads[game_id] = thread
        return self.module.BroadcastJob(
            league_id=LEAGUE,
            game_id=game_id,
            user_id=user_id,
            plan=plan or plan_for(6),
            inputs=make_inputs(self.module),
            message=message,
            thread=thread,
            db=object(),
        )

    def _track(self, game_id: int, script):
        """`on_call` for a job's message: counts the jobs that are between their
        first and last frame edit, which is the real concurrency of pacing."""

        async def on_call(n: int, kwargs) -> None:
            # The interrupted panel is not a frame: it ends a broadcast, it is not pacing.
            over = "Broadcast interrupted" in (kwargs["embed"].description or "")
            if n == 0 and not over:
                self.active.add(game_id)
                self.max_active = max(self.max_active, len(self.active))
            if script is not None:
                await script(n, kwargs)
            if n == FRAMES - 1 or over:
                self.active.discard(game_id)

        return on_call

    async def start(self, game_id: int, user_id: int = 10, **kwargs):
        job = self.job(game_id, user_id, **kwargs)
        admission = self.scheduler.admit(league_id=LEAGUE, game_id=game_id, user_id=user_id)
        assert admission == self.module.Admission.ADMITTED
        return await self.scheduler.spawn(job=job)


def _harness(module, repo, **settings) -> Harness:
    return Harness(module, repo, **settings)


# --- admission --------------------------------------------------------------


def test_admit_says_admitted_then_already_live_for_the_same_game(repo):
    h = _harness(broadcast, repo)
    admit = h.scheduler.admit
    assert admit(league_id=LEAGUE, game_id=1, user_id=10) == broadcast.Admission.ADMITTED
    assert admit(league_id=LEAGUE, game_id=1, user_id=11) == broadcast.Admission.ALREADY_LIVE
    assert h.scheduler.is_live_or_queued(LEAGUE, 1)
    assert not h.scheduler.is_live_or_queued(LEAGUE, 2)
    assert h.scheduler.depth == 1


def test_a_game_is_keyed_by_league_and_game_together(repo):
    h = _harness(broadcast, repo)
    assert h.scheduler.admit(league_id=1, game_id=5, user_id=10) == broadcast.Admission.ADMITTED
    assert h.scheduler.admit(league_id=2, game_id=5, user_id=11) == broadcast.Admission.ADMITTED
    assert h.scheduler.is_live_or_queued(2, 5) and not h.scheduler.is_live_or_queued(3, 5)


def test_a_full_scheduler_rejects_and_counts_nothing(repo):
    """Capacity is slots plus WAITING: 2 running + 3 waiting = 5 admitted."""
    h = _harness(broadcast, repo, max_concurrent_broadcasts=2, max_queue_depth=3)
    for game_id in (1, 2, 3, 4, 5):
        assert (
            h.scheduler.admit(league_id=LEAGUE, game_id=game_id, user_id=game_id)
            == broadcast.Admission.ADMITTED
        )
    assert h.scheduler.admit(league_id=LEAGUE, game_id=6, user_id=6) == (
        broadcast.Admission.REJECTED_FULL
    )
    assert h.scheduler.depth == 5 and not h.scheduler.is_live_or_queued(LEAGUE, 6)


def test_the_default_capacity_is_four_running_plus_twelve_waiting(repo):
    h = _harness(broadcast, repo, max_live_broadcasts_per_user=99)
    for game_id in range(1, 17):
        assert h.scheduler.admit(league_id=LEAGUE, game_id=game_id, user_id=game_id) == (
            broadcast.Admission.ADMITTED
        )
    assert h.scheduler.admit(league_id=LEAGUE, game_id=17, user_id=17) == (
        broadcast.Admission.REJECTED_FULL
    )


def test_a_queue_depth_of_zero_admits_only_what_fits_in_the_slots(repo):
    h = _harness(broadcast, repo, max_concurrent_broadcasts=1, max_queue_depth=0)
    assert h.scheduler.admit(league_id=LEAGUE, game_id=1, user_id=10) == (
        broadcast.Admission.ADMITTED
    )
    assert h.scheduler.admit(league_id=LEAGUE, game_id=2, user_id=11) == (
        broadcast.Admission.REJECTED_FULL
    )


def test_one_user_cannot_hold_more_than_the_per_user_cap(repo):
    h = _harness(broadcast, repo, max_live_broadcasts_per_user=2)
    ok, cap = broadcast.Admission.ADMITTED, broadcast.Admission.REJECTED_USER_CAP
    assert h.scheduler.admit(league_id=LEAGUE, game_id=1, user_id=10) == ok
    assert h.scheduler.admit(league_id=LEAGUE, game_id=2, user_id=10) == ok
    assert h.scheduler.admit(league_id=LEAGUE, game_id=3, user_id=10) == cap
    assert h.scheduler.admit(league_id=LEAGUE, game_id=3, user_id=11) == ok  # someone else is fine
    assert h.scheduler.depth == 3


async def test_a_finished_broadcast_gives_the_user_their_place_back(repo):
    h = _harness(broadcast, repo, max_live_broadcasts_per_user=1)
    task = await h.start(1)
    assert h.scheduler.admit(league_id=LEAGUE, game_id=2, user_id=10) == (
        broadcast.Admission.REJECTED_USER_CAP
    )
    await task
    assert h.scheduler.admit(league_id=LEAGUE, game_id=2, user_id=10) == (
        broadcast.Admission.ADMITTED
    )


async def test_spawn_needs_a_matching_admit_and_only_one_spawn(repo):
    h = _harness(broadcast, repo)
    with pytest.raises(ValueError, match="without a matching admit"):
        await h.scheduler.spawn(job=h.job(1))
    h.scheduler.admit(league_id=LEAGUE, game_id=1, user_id=10)
    with pytest.raises(ValueError, match="without a matching admit"):
        await h.scheduler.spawn(job=h.job(1, user_id=99))  # a different user's job
    await h.scheduler.spawn(job=h.job(1))
    with pytest.raises(ValueError, match="without a matching admit"):
        await h.scheduler.spawn(job=h.job(1))
    assert h.scheduler.depth == 1


def test_settings_are_the_ones_the_scheduler_enforces(repo):
    h = _harness(broadcast, repo, max_live_broadcasts_per_user=3)
    assert h.scheduler.settings.max_live_broadcasts_per_user == 3
    assert h.scheduler.settings.plan == broadcast.BroadcastSettings().plan


def test_an_unspawned_admission_can_be_given_back_and_taken_again(repo):
    """`/watch` admits before it creates the thread; if the thread cannot be
    made the reservation must not leak (depth, the per-user cap, and the
    `is_live_or_queued` answer `close_block` reads)."""
    h = _harness(broadcast, repo, max_live_broadcasts_per_user=1)
    ok = broadcast.Admission.ADMITTED
    assert h.scheduler.admit(league_id=LEAGUE, game_id=1, user_id=10) == ok
    assert h.scheduler.release_unspawned(LEAGUE, 1) is True
    assert h.scheduler.depth == 0 and not h.scheduler.is_live_or_queued(LEAGUE, 1)
    assert h.scheduler.admit(league_id=LEAGUE, game_id=2, user_id=10) == ok  # cap slot is back
    assert h.scheduler.release_unspawned(LEAGUE, 1) is False  # not admitted: nothing to give
    assert h.scheduler.depth == 1


async def test_a_spawned_broadcast_is_not_released_from_outside(repo):
    h = _harness(broadcast, repo)
    task = await h.start(1)
    assert h.scheduler.release_unspawned(LEAGUE, 1) is False
    assert h.scheduler.depth == 1 and h.scheduler.live(LEAGUE, 1) is not None
    await task
    assert h.scheduler.depth == 0


async def test_live_reports_the_spawned_broadcast_and_its_ids(repo):
    h = _harness(broadcast, repo)
    assert h.scheduler.live(LEAGUE, 7) is None
    h.scheduler.admit(league_id=LEAGUE, game_id=7, user_id=10)
    assert h.scheduler.live(LEAGUE, 7) is None  # admitted, not yet spawned
    task = await h.scheduler.spawn(job=h.job(7))
    live = h.scheduler.live(LEAGUE, 7)
    assert (live.league_id, live.game_id, live.user_id) == (LEAGUE, 7, 10)
    assert (live.thread_id, live.message_id, live.task) == (2007, 1007, task)
    await task
    assert h.scheduler.live(LEAGUE, 7) is None


# --- a normal broadcast -----------------------------------------------------


async def scenario_depth_returns_to_zero_after_completion(module, repo) -> None:
    h = _harness(module, repo)
    task = await h.start(1)
    assert h.scheduler.depth == 1 and h.scheduler.is_live_or_queued(LEAGUE, 1)
    await task
    assert h.scheduler.depth == 0, "the depth counter must come back down"
    assert not h.scheduler.is_live_or_queued(LEAGUE, 1)
    assert h.scheduler.live(LEAGUE, 1) is None
    assert repo.transitions() == [(QUEUED, LIVE), (LIVE, FINISHED)]


async def test_depth_returns_to_zero_after_completion(repo):
    await scenario_depth_returns_to_zero_after_completion(broadcast, repo)


async def test_a_broadcast_edits_every_frame_then_finishes_then_tells_on_ended(repo):
    h = _harness(broadcast, repo)
    await (await h.start(1))
    assert len(h.messages[1].calls) == FRAMES
    assert repo.events == [
        ("transition", 1, QUEUED, LIVE),
        ("transition", 1, LIVE, FINISHED),
        ("ended", 1, FINISHED),  # after the status write, with the status it wrote
    ]
    for kwargs, _ in h.messages[1].calls:
        assert_no_mentions(kwargs)


async def test_a_broadcast_that_posted_its_final_as_a_new_message_is_still_finished(repo):
    h = _harness(broadcast, repo)
    task = await h.start(1, script=failing(FRAMES - 1, FRAMES, FRAMES + 1))
    await task
    assert repo.transitions() == [(QUEUED, LIVE), (LIVE, FINISHED)]
    sent = h.threads[1].sent
    assert len(sent) == 1  # the one fresh final-panel message; no recap exists any more
    assert sent[-1]["embed"].title == h.messages[1].calls[-1][0]["embed"].title
    for kwargs in sent:
        assert_no_mentions(kwargs)


async def test_a_final_frame_that_could_not_be_delivered_ends_interrupted(repo):
    h = _harness(broadcast, repo)
    job = h.job(1, script=failing(FRAMES - 1, FRAMES, FRAMES + 1))
    h.threads[1].fail = True
    h.scheduler.admit(league_id=LEAGUE, game_id=1, user_id=10)
    await (await h.scheduler.spawn(job=job))
    assert repo.transitions() == [(QUEUED, LIVE), (LIVE, INTERRUPTED)]
    assert repo.events[-1] == ("ended", 1, INTERRUPTED)


async def test_three_failed_ticks_end_interrupted_with_the_interrupted_panel(repo):
    """Tick 0 is one request, ticks 1 and 2 a period post and a scoreboard edit:
    five requests, every one failing, is three failed ticks."""
    h = _harness(broadcast, repo)
    await (await h.start(1, fail_if=lambda r: r.number < 5))
    calls = h.messages[1].calls
    assert len(calls) == 4  # three abandoned frames, then the interrupted panel
    panel = calls[3][0]["embed"]
    assert "Broadcast interrupted" in panel.description
    assert_no_mentions(calls[3][0])
    assert repo.transitions() == [(QUEUED, LIVE), (LIVE, INTERRUPTED)]
    assert h.scheduler.depth == 0


# --- an exception in the body -----------------------------------------------


def _empty_plan():
    plan = plan_for(6)
    return type(plan)(**{**plan.__dict__, "frames": ()})


async def test_an_exception_in_the_body_is_logged_and_answered_with_the_interrupted_panel(
    repo, caplog
):
    h = _harness(broadcast, repo)
    task = await h.start(1, plan=_empty_plan())  # the pacer raises ValueError
    await task  # the task itself does not fail: the exception was handled
    assert task.exception() is None
    calls = h.messages[1].calls
    assert len(calls) == 1
    assert "Broadcast interrupted" in calls[0][0]["embed"].description
    assert_no_mentions(calls[0][0])
    assert repo.transitions() == [(QUEUED, LIVE), (LIVE, INTERRUPTED)]
    assert repo.events[-1] == ("ended", 1, INTERRUPTED)
    assert h.scheduler.depth == 0
    failure = [r for r in caplog.records if r.getMessage() == "broadcast game 1 failed"]
    assert failure and failure[0].exc_info and failure[0].exc_info[0] is ValueError


async def test_a_failing_interrupted_edit_cannot_escape_or_stop_the_finalizer(repo):
    h = _harness(broadcast, repo)

    async def always_fail(n, kwargs):
        raise http_error()

    task = await h.start(1, plan=_empty_plan(), script=always_fail)
    await task
    assert task.exception() is None
    assert len(h.messages[1].calls) == 1  # one bounded attempt, not a retry loop
    assert repo.transitions() == [(QUEUED, LIVE), (LIVE, INTERRUPTED)]
    assert h.scheduler.depth == 0


async def test_a_hanging_interrupted_edit_is_bounded_by_its_own_timeout(repo):
    h = _harness(broadcast, repo, interrupted_edit_timeout_seconds=0.01)

    async def hang(n, kwargs):
        await asyncio.Event().wait()

    task = await h.start(1, plan=_empty_plan(), script=hang)
    await asyncio.wait_for(task, 5)
    assert repo.transitions() == [(QUEUED, LIVE), (LIVE, INTERRUPTED)]


async def scenario_depth_returns_to_zero_after_an_exception(module, repo) -> None:
    h = _harness(module, repo)
    task = await h.start(1, plan=_empty_plan())
    await task
    assert h.scheduler.depth == 0, "an exception in the body must not leak the depth counter"
    assert not h.scheduler.is_live_or_queued(LEAGUE, 1)


async def test_depth_returns_to_zero_after_an_exception(repo):
    await scenario_depth_returns_to_zero_after_an_exception(broadcast, repo)


async def test_an_exception_before_the_live_write_is_interrupted_from_queued(repo):
    repo.raises[(QUEUED, LIVE)] = RuntimeError("db down")
    h = _harness(broadcast, repo)
    await (await h.start(1))
    assert repo.transitions() == [(QUEUED, LIVE), (QUEUED, INTERRUPTED)]
    assert len(h.messages[1].calls) == 1  # just the interrupted panel


async def test_a_row_someone_else_moved_is_not_broadcast_into(repo):
    repo.answers[(QUEUED, LIVE)] = 0
    h = _harness(broadcast, repo)
    task = await h.start(1)
    await task
    assert h.messages[1].calls == []
    assert repo.transitions() == [(QUEUED, LIVE)]  # no further write on a row it does not own
    assert h.scheduler.depth == 0
    # ... but on_ended still runs: the /watch that spawned it had put "watching now" on the
    # game's #games line, and the block may have closed meanwhile (F8). It is idempotent.
    assert [e for e in repo.events if e[0] == "ended"] == [("ended", 1, INTERRUPTED)]


async def test_a_status_write_that_fails_is_logged_and_on_ended_still_runs(repo, caplog):
    repo.raises[(LIVE, FINISHED)] = RuntimeError("db down")
    h = _harness(broadcast, repo)
    task = await h.start(1)
    await task
    assert task.exception() is None
    assert repo.events[-1] == ("ended", 1, FINISHED)
    assert any("could not record" in r.getMessage() for r in caplog.records)


async def test_an_on_ended_that_raises_is_logged_not_propagated(repo, caplog):
    async def boom(*, league_id, game_id, status):
        raise RuntimeError("reveal failed")

    h = Harness(broadcast, repo, on_ended=boom)
    task = await h.start(1)
    await task
    assert task.exception() is None and h.scheduler.depth == 0
    assert any("on_ended failed" in r.getMessage() for r in caplog.records)


async def test_a_non_exception_base_exception_is_still_reported_then_reraised(repo):
    """`SystemExit`/`KeyboardInterrupt` must still stop the loop, but not before
    the game's panel and row are settled. Run directly, not as a task, so the
    exception reaches `pytest.raises` instead of the event loop."""

    class Stop(BaseException):
        pass

    async def stop(n, kwargs):
        if n == 2:
            raise Stop

    h = _harness(broadcast, repo)
    h.scheduler.admit(league_id=LEAGUE, game_id=1, user_id=10)
    with pytest.raises(Stop):
        await h.scheduler._run_broadcast_guarded(h.job(1, script=stop))
    assert "Broadcast interrupted" in h.messages[1].calls[-1][0]["embed"].description
    assert repo.transitions() == [(QUEUED, LIVE), (LIVE, INTERRUPTED)]
    assert h.scheduler.depth == 0


# --- the semaphore ----------------------------------------------------------


async def test_the_semaphore_bounds_concurrent_broadcasts_and_is_fully_used(repo):
    h = _harness(broadcast, repo, max_concurrent_broadcasts=2, max_live_broadcasts_per_user=9)
    tasks = [await h.start(game_id) for game_id in range(1, 7)]
    await asyncio.gather(*tasks)
    assert h.max_active == 2, "never above the bound, and the bound is actually reached"
    assert h.scheduler.depth == 0
    assert all(len(h.messages[g].calls) == FRAMES for g in range(1, 7))


async def test_spawn_returns_before_any_slot_is_free(repo):
    """The semaphore is acquired INSIDE the task: a full house does not block
    `/watch` (design P5-97.j) -- the second spawn returns at once, unstarted."""
    gate = asyncio.Event()

    async def hold(n, kwargs):
        if n == 1:
            await gate.wait()

    h = _harness(broadcast, repo, max_concurrent_broadcasts=1)
    first = await h.start(1, script=hold)
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(h.messages[1].calls) == 2  # game 1 holds the only slot, parked mid-pace
    second = await asyncio.wait_for(h.start(2), timeout=1)
    for _ in range(20):
        await asyncio.sleep(0)
    assert not second.done() and h.messages[2].calls == []  # queued: waiting on the slot
    assert h.scheduler.live(LEAGUE, 2) is not None and h.scheduler.depth == 2
    assert repo.transitions(2) == []  # not `live` until it has a slot
    gate.set()
    await asyncio.gather(first, second)
    assert h.max_active == 1
    assert repo.transitions(2) == [(QUEUED, LIVE), (LIVE, FINISHED)]


# --- cancellation -----------------------------------------------------------


async def _park(h: Harness, game_id: int, gate: asyncio.Event, **kwargs):
    async def hold(n, kw):
        if n == 2:
            await gate.wait()

    return await h.start(game_id, script=hold, **kwargs)


async def _settle(times: int = 30) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def scenario_depth_returns_to_zero_after_cancellation(module, repo) -> None:
    h = _harness(module, repo)
    task = await _park(h, 1, asyncio.Event())
    await _settle()
    assert len(h.messages[1].calls) == 3
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.scheduler.depth == 0, "a cancelled broadcast must give its depth count back"
    assert not h.scheduler.is_live_or_queued(LEAGUE, 1)


async def test_depth_returns_to_zero_after_cancellation(repo):
    await scenario_depth_returns_to_zero_after_cancellation(broadcast, repo)


async def test_cancelling_mid_pace_reraises_frees_the_slot_and_still_writes_interrupted(repo):
    h = _harness(broadcast, repo, max_concurrent_broadcasts=1)
    task = await _park(h, 1, asyncio.Event())
    await _settle()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    # The shielded write landed although the task was cancelled ...
    assert repo.transitions(1) == [(QUEUED, LIVE), (LIVE, INTERRUPTED)]
    # ... it is not reported to the reveal seam (the process is going away) ...
    assert not any(e[0] == "ended" for e in repo.events)
    # ... and the semaphore was released: the next broadcast gets the slot.
    await asyncio.wait_for(await h.start(2), timeout=5)
    assert repo.transitions(2) == [(QUEUED, LIVE), (LIVE, FINISHED)]


async def test_a_cancelled_queued_broadcast_is_interrupted_from_queued_and_frees_nothing_extra(
    repo,
):
    """Cancelled while WAITING for a slot: it never held one, so it must not
    release one (an over-released semaphore would let two broadcasts run)."""
    gate = asyncio.Event()
    h = _harness(broadcast, repo, max_concurrent_broadcasts=1, max_live_broadcasts_per_user=9)
    first = await _park(h, 1, gate)
    await _settle()
    waiting = await h.start(2)
    await _settle()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert repo.transitions(2) == [(QUEUED, INTERRUPTED)]
    assert h.scheduler.depth == 1  # only game 1 is left
    gate.set()
    await first
    rest = [await h.start(g) for g in (3, 4, 5)]
    await asyncio.gather(*rest)
    assert h.max_active == 1


async def test_a_task_cancelled_before_its_first_step_writes_nothing_but_leaks_nothing(repo):
    """Its `try` was never entered, so its `finally` cannot run: the row stays
    `queued` (the startup sweep's to fix -- this module does not claim
    otherwise), but the in-memory admission is released by the done-callback."""
    h = _harness(broadcast, repo)
    task = await h.start(1)
    task.cancel()  # no await since spawn: the body never ran
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert repo.events == []
    assert h.scheduler.depth == 0 and not h.scheduler.is_live_or_queued(LEAGUE, 1)
    assert h.scheduler.admit(league_id=LEAGUE, game_id=1, user_id=10) == (
        broadcast.Admission.ADMITTED
    )


async def test_shutdown_cancels_every_broadcast_and_waits_for_them_to_unwind(repo):
    h = _harness(broadcast, repo, max_concurrent_broadcasts=2, max_live_broadcasts_per_user=9)
    gate = asyncio.Event()
    tasks = [await _park(h, g, gate) for g in (1, 2, 3)]  # two live, one queued
    await _settle()
    await h.scheduler.shutdown()
    assert all(t.cancelled() for t in tasks)
    assert h.scheduler.depth == 0
    assert sorted(repo.transitions(1) + repo.transitions(2)) == sorted(
        [(QUEUED, LIVE), (LIVE, INTERRUPTED)] * 2
    )
    assert repo.transitions(3) == [(QUEUED, INTERRUPTED)]
    assert not any(e[0] == "ended" for e in repo.events)


def _is_interrupted_panel(call) -> bool:
    kwargs, _at = call
    embed = kwargs.get("embed")
    return (
        embed is not None
        and "Broadcast interrupted" in (embed.description or "")
        and kwargs["content"] is None
    )


async def scenario_cancelling_a_broadcast_edits_the_viewers_message_to_interrupted(
    module, repo
) -> None:
    """F2a: a graceful stop cancels the task; the viewer's thread used to stay
    frozen mid-game because the cancel path never touched the message."""
    h = _harness(module, repo)
    task = await _park(h, 1, asyncio.Event())
    await _settle()
    assert len(h.messages[1].calls) == 3
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled(), "the CancelledError must still propagate"
    assert _is_interrupted_panel(h.messages[1].calls[-1]), "the panel was not edited"
    assert_no_mentions(h.messages[1].calls[-1][0])


async def test_cancelling_a_broadcast_edits_the_viewers_message_to_interrupted(repo):
    await scenario_cancelling_a_broadcast_edits_the_viewers_message_to_interrupted(broadcast, repo)


async def test_a_graceful_shutdown_leaves_no_live_or_queued_viewer_message_frozen(repo):
    h = _harness(broadcast, repo, max_concurrent_broadcasts=1, max_live_broadcasts_per_user=9)
    gate = asyncio.Event()
    await _park(h, 1, gate)  # live, parked mid-pace
    await _settle()
    await h.start(2)  # queued: its message still says "waiting for a slot"
    await _settle()
    await h.scheduler.shutdown()
    assert _is_interrupted_panel(h.messages[1].calls[-1])
    assert _is_interrupted_panel(h.messages[2].calls[-1])
    assert repo.transitions(1) == [(QUEUED, LIVE), (LIVE, INTERRUPTED)]
    assert repo.transitions(2) == [(QUEUED, INTERRUPTED)]


async def test_a_hanging_interrupted_edit_on_cancel_is_bounded_and_the_task_still_ends_cancelled(
    repo,
):
    """The edit runs shielded and inside `interrupted_edit_timeout_seconds`: a
    Discord that has stopped answering cannot hold the shutdown."""
    hang = asyncio.Event()

    async def script(n, kwargs):
        if n == 2:
            await asyncio.Event().wait()  # parked mid-pace
        if n == 3:
            await hang.wait()  # the interrupted-panel edit never answers

    h = _harness(broadcast, repo, interrupted_edit_timeout_seconds=0.05)
    task = await h.start(1, script=script)
    await _settle()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert task.cancelled()
    assert repo.transitions(1) == [(QUEUED, LIVE), (LIVE, INTERRUPTED)]
    assert h.scheduler.depth == 0


async def test_a_second_cancel_during_the_interrupted_edit_still_records_the_row(repo):
    """The edit is shielded from the cancel that started it AND from a second
    one; the row write is shielded as it always was."""
    hang = asyncio.Event()

    async def script(n, kwargs):
        if n == 2:
            await asyncio.Event().wait()
        if n == 3:
            await hang.wait()

    h = _harness(broadcast, repo)
    task = await h.start(1, script=script)
    await _settle()
    task.cancel()
    await _settle()  # inside the interrupted edit now
    assert len(h.messages[1].calls) == 4 and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert task.cancelled()
    assert repo.transitions(1) == [(QUEUED, LIVE), (LIVE, INTERRUPTED)]
    hang.set()
    await _settle()


# --- shutdown waits for finalizers that already left the registry (F9) ---------


async def scenario_shutdown_waits_for_a_finalizer_already_past_forget(module, repo) -> None:
    gate = asyncio.Event()
    repo.gates[(LIVE, FINISHED)] = gate  # the finalizer's status write hangs
    h = _harness(module, repo)
    task = await h.start(1)
    await _settle(200)  # the pacer is done; the task sits in its finalizer, past _forget
    assert h.scheduler.live(LEAGUE, 1) is None and not task.done()
    stopping = asyncio.create_task(h.scheduler.shutdown())
    await _settle()
    assert not stopping.done(), "shutdown returned while a finalizer was still writing"
    gate.set()
    await asyncio.wait_for(stopping, timeout=5)
    assert task.done() and h.scheduler.depth == 0
    assert ("ended", 1, FINISHED) in repo.events, "the finalizer was cut off"


async def test_shutdown_waits_for_a_finalizer_already_past_forget(repo):
    await scenario_shutdown_waits_for_a_finalizer_already_past_forget(broadcast, repo)


async def test_shutdown_gives_up_on_a_stuck_finalizer_after_its_bound(repo, monkeypatch):
    monkeypatch.setattr(broadcast, "SHUTDOWN_FINALIZER_TIMEOUT_SECONDS", 0.05)
    gate = asyncio.Event()
    repo.gates[(LIVE, FINISHED)] = gate
    h = _harness(broadcast, repo)
    task = await h.start(1)
    await _settle(200)
    await asyncio.wait_for(h.scheduler.shutdown(), timeout=2)  # returns although it is stuck
    assert not task.done()
    gate.set()
    await asyncio.wait_for(task, timeout=2)


# --- a row someone else moved still gets its on_ended (F8) ----------------------


async def test_a_broadcast_that_finds_its_row_moved_still_tells_on_ended_once(repo):
    repo.answers[(QUEUED, LIVE)] = 0
    h = _harness(broadcast, repo)
    await (await h.start(1))
    assert [e for e in repo.events if e[0] == "ended"] == [("ended", 1, INTERRUPTED)]
    assert h.messages[1].calls == [] and h.scheduler.depth == 0


async def test_a_cancelled_broadcast_that_never_started_does_not_tell_on_ended(repo):
    h = _harness(broadcast, repo)
    task = await h.start(1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not any(e[0] == "ended" for e in repo.events)


# --- the default clock and sleep --------------------------------------------


async def test_the_default_clock_and_sleep_run_a_real_broadcast(repo):
    """No injected clock or sleep: `loop.time` and `asyncio.sleep`, at a 1 ms
    tick so 31 frames take ~30 ms."""
    settings = broadcast.BroadcastSettings(
        tick_seconds=0.001, plan=plan_for(10).settings, edit_timeout_seconds=5
    )

    async def ended(*, league_id, game_id, status):
        repo.events.append(("ended", game_id, status))

    scheduler = broadcast.BroadcastScheduler(settings, on_ended=ended)
    h = Harness(broadcast, repo)
    job = h.job(1, plan=plan_for(10))
    scheduler.admit(league_id=LEAGUE, game_id=1, user_id=10)
    await asyncio.wait_for(await scheduler.spawn(job=job), timeout=10)
    assert job.message.calls and repo.transitions()[-1] == (LIVE, FINISHED)
    assert scheduler.depth == 0


# --- mutation twins ---------------------------------------------------------

_DEPTH_DECREMENT = "            self._depth -= 1\n"
_FORGET_IN_FINALLY = "            self._forget(key)\n"


async def test_twin_control_every_scheduler_scenario_passes_on_an_unmutated_copy(repo):
    control = variant()
    await scenario_depth_returns_to_zero_after_completion(control, repo)
    await scenario_depth_returns_to_zero_after_an_exception(control, repo)
    await scenario_depth_returns_to_zero_after_cancellation(control, repo)


@pytest.mark.parametrize(
    "scenario",
    [
        scenario_depth_returns_to_zero_after_completion,
        scenario_depth_returns_to_zero_after_an_exception,
        scenario_depth_returns_to_zero_after_cancellation,
    ],
    ids=["completion", "exception", "cancellation"],
)
async def test_twin_without_the_depth_decrement_every_path_leaks_the_counter(repo, scenario):
    """§10 twin: the counter's decrement is the one line that gives an admission
    back. Both its callers (the finalizer and the done-callback) go through
    `_forget`, so deleting it there must fail every path's scenario."""
    mutated = variant((_DEPTH_DECREMENT, "            pass\n"))
    with pytest.raises(AssertionError, match="depth"):
        await scenario(mutated, repo)


async def test_twin_the_done_callback_is_what_saves_a_never_started_task(repo):
    """Delete the done-callback and the never-started task leaks its admission,
    which proves the callback (not the finalizer) covers that path."""
    mutated = variant(
        (
            "        task.add_done_callback(lambda _task: self._forget_if_current(key, live))\n",
            "",
        )
    )
    h = _harness(mutated, repo)
    task = await h.start(1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert h.scheduler.depth == 1  # leaked, as the twin predicts


def test_twin_capacity_that_ignores_the_slots_rejects_the_fifth_default_broadcast(repo):
    """§10 twin: `max_queue_depth` means WAITING only, so the bound must add the
    slots. Revert the check to the queue depth alone and the default 4 + 12
    capacity shrinks to 12."""
    control = variant()
    mutated = variant(
        (
            "self._settings.max_concurrent_broadcasts + self._settings.max_queue_depth\n",
            "self._settings.max_queue_depth\n",
        )
    )
    for module, expect_full in ((control, False), (mutated, True)):
        h = _harness(module, repo, max_live_broadcasts_per_user=99)
        results = [h.scheduler.admit(league_id=LEAGUE, game_id=g, user_id=g) for g in range(1, 17)]
        assert (module.Admission.REJECTED_FULL in results) is expect_full


async def test_twin_release_that_ignores_spawn_would_orphan_a_running_broadcast(repo):
    """The guard `key in self._live` is what stops `release_unspawned` from
    dropping a running task's bookkeeping; without it the running game reads as
    idle and could be admitted a second time."""
    control = variant()
    mutated = variant(
        ("if key not in self._admitted or key in self._live:", "if key not in self._admitted:")
    )
    for module, expect_orphaned in ((control, False), (mutated, True)):
        h = _harness(module, repo)
        task = await h.start(1)
        released = h.scheduler.release_unspawned(LEAGUE, 1)
        assert released is expect_orphaned
        assert h.scheduler.is_live_or_queued(LEAGUE, 1) is (not expect_orphaned)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


_CANCEL_EDIT = "                await self._edit_interrupted_uncancellable(job, progress)\n"
_FINALIZER_WAIT = "        running = [fin for fin in self._finalizing if not fin.done()]\n"


async def test_twin_control_the_fix_round_scenarios_pass_on_an_unmutated_copy(repo):
    control = variant()
    await scenario_cancelling_a_broadcast_edits_the_viewers_message_to_interrupted(control, repo)
    await scenario_shutdown_waits_for_a_finalizer_already_past_forget(control, repo)


async def test_twin_without_the_cancel_path_edit_the_viewers_message_stays_frozen(repo):
    mutated = variant((_CANCEL_EDIT, "                pass\n"))
    with pytest.raises(AssertionError, match="panel was not edited"):
        await scenario_cancelling_a_broadcast_edits_the_viewers_message_to_interrupted(
            mutated, repo
        )


async def test_twin_shutdown_that_ignores_running_finalizers_returns_while_one_writes(repo):
    mutated = variant((_FINALIZER_WAIT, "        running = []\n"))
    with pytest.raises(AssertionError, match="shutdown returned while a finalizer"):
        await scenario_shutdown_waits_for_a_finalizer_already_past_forget(mutated, repo)


# --- the recap is GONE in watch-v4 (T-6): folded into the final component -----------------------
#
# Everything that used to live here (a finished broadcast posts a recap after the final
# scoreboard and before `on_ended`, the recap links every period message, a slow or failing
# recap does not hold the concurrency slot or change the outcome, an interrupted broadcast
# marks the newest period message "interrupted here" and leaves frozen ones alone) has no
# v4 successor: there is no recap message and no period message to mark. The single
# interrupted-panel edit `_edit_interrupted` now makes is covered below, and its "exactly
# once, even under a cancel mid-report" invariant is unchanged from v2/v3.


# --- one interrupted edit, and the slot is not held for the recap (critic minors 10, 11) --------


async def scenario_a_cancel_during_the_edit_failures_report_makes_no_second_interrupted_edit(
    module, repo
) -> None:
    """Three scoreboard edits fail (ticks 0-2), so the task reports the broadcast
    interrupted; that edit hangs and a cancel lands in it. The `BaseException`
    path does not report again after being cancelled mid-report, and neither may
    this one: exactly one interrupted-panel edit reaches the scoreboard."""
    hang = asyncio.Event()

    async def script(n, kwargs):
        if n == 3:
            await hang.wait()  # the first interrupted-panel edit never answers

    h = _harness(module, repo)
    task = await h.start(1, script=script, fail_if=lambda r: r.message_id == 1001 and r.number < 3)
    await _settle()
    assert len(h.messages[1].calls) == 4 and not task.done()  # 3 abandoned frames + the report
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert task.cancelled()
    assert len(h.messages[1].calls) == 4, "the interrupted edit ran a second time"
    assert repo.transitions(1) == [(QUEUED, LIVE), (LIVE, INTERRUPTED)]
    hang.set()
    await _settle()


async def test_a_cancel_during_the_edit_failures_report_makes_no_second_interrupted_edit(repo):
    await scenario_a_cancel_during_the_edit_failures_report_makes_no_second_interrupted_edit(
        broadcast, repo
    )


_REPORTED_GUARD = "            if end is not BroadcastStatus.FINISHED and not reported:\n"


async def test_twin_control_the_single_report_scenario_passes_on_an_unmutated_copy(repo):
    await scenario_a_cancel_during_the_edit_failures_report_makes_no_second_interrupted_edit(
        variant(), repo
    )


async def test_twin_without_the_reported_guard_the_interrupted_edit_runs_twice(repo):
    mutated = variant((_REPORTED_GUARD, "            if end is not BroadcastStatus.FINISHED:\n"))
    with pytest.raises(AssertionError, match="second time"):
        await scenario_a_cancel_during_the_edit_failures_report_makes_no_second_interrupted_edit(
            mutated, repo
        )


## The "parked recap does not hold the slot but still holds the reveal" scenario and its
## three twins lived here (v2/v3): with `_post_recap` deleted and nothing posted after the
## final component edit, there is no longer a slow background post between `end =
## BroadcastStatus.FINISHED` and the slot's release for a stop to park in, so the scenario
## has no v4 analogue.
