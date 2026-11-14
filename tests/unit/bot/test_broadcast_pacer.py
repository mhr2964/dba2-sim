"""The deadline pacer and the FINAL-frame ladder (docs/design/watch-v4-live-ticker.md,
supersedes docs/design/phase-5-stage-d-broadcast.md §6 P5-97.i/.q and /watch v2/v3's
per-period-message model): the pacer edits ONE live component every tick, carrying the
embed and the cycle-row view together, and writes `ComponentState` at exactly the sites
T-5.b enumerates (W0-W2).

Every scenario is a function of the MODULE under test (`broadcast` or a mutated
`variant`), so a mutation twin is the same scenario run against a copy with one rule
deleted, expected to fail. The control tests run the scenario on an UNMUTATED copy
first: "red" that could be a loading failure proves nothing.

The pacer runs on `FakeClock` -- `clock()` and `sleep` are injected, so a 45-frame
broadcast takes milliseconds. The only real time is `asyncio.wait_for`'s timeout, set
to 10 ms where a test hangs an edit.

`FakeThread.ledger` holds every request of the run in arrival order; with one request a
tick, `requests_at()` groups exactly one per tick (a final-ladder retry sleeps between
requests, so it gets its own group).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import discord
import pytest

from dba2.bot import broadcast
from dba2.bot.broadcast import ComponentViewRequest
from dba2.presentation.broadcast import (
    CHAIN_LINE_KINDS,
    FULL_PLAN_SETTINGS,
    GAME_PLAN_SETTINGS,
    FrameKind,
    completed_through,
    last_line_index,
    reveal_schedule,
    visible_periods,
)
from tests.unit.bot._broadcast_fakes import (
    AWAY,
    HOME,
    FakeClock,
    FakeMessage,
    FakeThread,
    Request,
    assert_no_mentions,
    failing,
    fake_component_view_for,
    make_inputs,
    narration_for,
    plan_for,
    plan_with,
    sim_result,
    variant,
)
from tests.unit.presentation._broadcast_helpers import make_team, names_for

TICK = 1.5
"""The real pacer reads its tick from `BroadcastSettings.tick_seconds`
(`run_pace`'s `BroadcastSettings(plan=plan.settings, **setting_overrides)` below reads
`DEFAULT_BROADCAST_SETTINGS`'s own field when not overridden); this is a bare mirror of
that default, not a read of it, so it must be edited by hand whenever the default moves
(watch-v5 WT-7 item 8, round 2 blocking). Every timing assertion in this file is written
symbolically in terms of `TICK`, so this is the only edit this file needs."""
START = 1000.0  # `FakeClock`'s default start: the pacer reads it as `started`


@dataclass
class Run:
    outcome: Any
    progress: Any
    message: FakeMessage
    thread: FakeThread
    clock: FakeClock
    plan: Any
    view_calls: list[ComponentViewRequest]

    @property
    def final_index(self) -> int:
        return len(self.plan.frames) - 1

    def successful_embeds(self, *failed_calls: int):
        return [
            kwargs["embed"]
            for n, (kwargs, _) in enumerate(self.message.calls)
            if n not in failed_calls
        ]

    def ticks(self) -> list[list[Request]]:
        return self.thread.requests_at()


async def run_pace(
    module=broadcast,
    *,
    per_frame: int = 6,
    script=None,
    fail_if=None,
    thread_fail: bool = False,
    latency: float = 0.0,
    component_view_for=None,
    narration=None,
    **setting_overrides,
) -> Run:
    """`script(clock)` is the component message's `on_call` (per-call failures and
    latency, as before); `fail_if(request)` fails chosen requests, and `latency` is
    spent by every request."""
    plan = plan_for(per_frame)
    clock = FakeClock()
    thread = FakeThread(clock=clock, fail=thread_fail, fail_if=fail_if, latency=latency)
    message = FakeMessage(
        clock,
        script(clock) if script else None,
        ledger=thread.ledger,
        fail_if=fail_if,
        latency=latency,
    )
    settings = module.BroadcastSettings(plan=plan.settings, **setting_overrides)
    progress = module.PaceProgress()
    view_calls: list[ComponentViewRequest] = []
    outcome = await module.pace_broadcast(
        plan,
        make_inputs(module),
        message=message,
        thread=thread,
        settings=settings,
        clock=clock,
        sleep=clock.sleep,
        progress=progress,
        narration=narration if narration is not None else narration_for(plan),
        component_view_for=component_view_for or fake_component_view_for(view_calls),
    )
    return Run(outcome, progress, message, thread, clock, plan, view_calls)


def latency(seconds: float, *fail: int):
    return lambda clock: failing(*fail, latency=seconds, clock=clock)


def jump(at_call: int, to_frame: int, from_frame: int | None = None):
    source = at_call if from_frame is None else from_frame

    def factory(clock: FakeClock):
        async def on_call(n: int, kwargs) -> None:
            if n == at_call:
                clock.now += (to_frame - source) * TICK + 0.5

        return on_call

    return factory


def _title(frame) -> str:
    return f"{AWAY.code} {frame.away_score} - {frame.home_score} {HOME.code}"


def _frame_index(plan, kind: FrameKind, *, period: int | None = None) -> int:
    return next(
        f.index for f in plan.frames if f.kind is kind and (period is None or f.period == period)
    )


# --- on-time delivery -------------------------------------------------------


async def scenario_on_time_delivers_every_frame_on_its_deadline(module) -> None:
    run = await run_pace(module)
    n = len(run.plan.frames)
    assert run.outcome == module.PaceOutcome.FINISHED
    assert run.progress.delivered == list(range(n))
    assert (run.progress.skipped, run.progress.abandoned) == (0, 0)
    assert len(run.message.calls) == n
    start = run.message.calls[0][1]
    assert [at for _, at in run.message.calls] == [start + i * TICK for i in range(n)]
    for embed, frame in zip(run.successful_embeds(), run.plan.frames, strict=True):
        assert embed.title == _title(frame)
    assert run.progress.scoreboard is run.message
    assert run.progress.component.terminal is False
    assert run.progress.component.more_ticks is False  # W1 set it False for the FINAL frame


async def test_every_frame_is_edited_on_its_deadline_when_edits_are_instant():
    await scenario_on_time_delivers_every_frame_on_its_deadline(broadcast)


async def test_duration_is_frames_times_tick_not_the_sum_of_edit_latencies():
    run = await run_pace(script=latency(1.0))
    assert run.progress.skipped == 0
    final_at = run.message.calls[-1][1]
    assert final_at == run.message.calls[0][1] + run.final_index * TICK


# --- lateness: FINAL is never skipped, the pacer never sleeps negative (MINOR 5) ---------------
#
# MINOR 5 (critic round 1): `min(due_index, final_index)` (the FINAL clamp) and
# `max(0.0, ...)` (the sleep floor) are unchanged live code that the v4 migration left
# with no test at all -- these two scenarios and their mutation twins covered exactly
# that in v2/v3 and are restored here, adapted to `run_pace`'s v4 shape.


async def scenario_final_is_never_skipped_however_late(module) -> None:
    """A single edit that takes 1000s puts the clock ~667 ticks past a 45-frame plan
    (at `TICK = 1.5`): the pacer must jump ONTO the final frame, not past it (an
    unclamped jump would index past the plan and raise)."""
    run = await run_pace(module, script=latency(1000.0))
    assert run.progress.delivered == [0, run.final_index]
    assert run.progress.skipped == run.final_index - 1
    assert run.message.calls[-1][0]["embed"].title == _title(run.plan.frames[-1])
    assert run.plan.frames[-1].kind is FrameKind.FINAL


async def test_final_is_never_skipped_however_late():
    await scenario_final_is_never_skipped_however_late(broadcast)


async def test_a_late_pacer_never_asks_for_a_negative_sleep():
    run = await run_pace(script=latency(10.0))  # `FakeClock.sleep` itself asserts >= 0
    assert run.clock.sleeps and all(s >= 0.0 for s in run.clock.sleeps)
    assert 0.0 in run.clock.sleeps  # late frames sleep zero, not a negative


_DUE_STEP = "        due_step = bisect.bisect_right(dues, clock() - started) - 1\n"
_SLEEP_FLOOR = "        await sleep(max(0.0, started + dues[step] - clock()))\n"


async def test_twin_an_off_by_one_due_reveal_lands_past_final():
    """The FINAL-never-skipped guarantee is now the schedule lookup itself (docs/design/
    watch-narration-v2.md WN-10.5): `bisect_right(...) - 1` can land ON the last reveal,
    never past it. Dropping the `- 1` must break the scenario above -- a late pacer then
    indexes `schedule[len(schedule)]`."""
    mutated = variant(
        (_DUE_STEP, "        due_step = bisect.bisect_right(dues, clock() - started)\n")
    )
    with pytest.raises(Exception):  # noqa: B017 -- the failure mode is an IndexError
        await scenario_final_is_never_skipped_however_late(mutated)


async def test_twin_without_the_zero_floor_a_late_pacer_sleeps_a_negative_time():
    mutated = variant((_SLEEP_FLOOR, "        await sleep(started + dues[step] - clock())\n"))
    with pytest.raises(AssertionError, match="negative sleep"):
        await run_pace(mutated, script=latency(10.0))


# --- a malicious team name cannot ping through the scoreboard (MINOR 5) -------------------------


async def test_a_team_named_at_here_cannot_ping_through_the_scoreboard():
    """MINOR 5 (critic round 1): restores v2/v3's team-name-injection coverage, which the
    v4 migration dropped for no design reason. A team's `code` is a database string and
    nothing sanitizes the `@` out of it before it reaches the embed (`_CODE_CHARS` caps
    LENGTH, not content -- `"@here"` fits inside it uncut); `AllowedMentions.none()`
    (P5-97.s) is what actually stops a malicious league name from pinging a guild
    through the live component, not input scrubbing."""
    plan = plan_for(6)
    clock = FakeClock()
    thread = FakeThread(clock=clock)
    message = FakeMessage(clock, ledger=thread.ledger)
    settings = broadcast.BroadcastSettings(plan=plan.settings)
    progress = broadcast.PaceProgress()
    result = sim_result()
    evil_home = make_team(1, "@here")

    outcome = await broadcast.pace_broadcast(
        plan,
        broadcast.BroadcastInputs(
            home=evil_home, away=AWAY, names=names_for(result), result=result
        ),
        message=message,
        thread=thread,
        settings=settings,
        clock=clock,
        sleep=clock.sleep,
        progress=progress,
        narration=narration_for(plan),
    )
    assert outcome == broadcast.PaceOutcome.FINISHED
    embed = message.calls[-1][0]["embed"]
    assert "@here" in embed.title, "the injected code must actually reach the embed"
    for kwargs, _ in message.calls:
        assert_no_mentions(kwargs)


@pytest.mark.parametrize("per_frame", [10, 6, 2])
async def test_the_pacer_walks_a_plan_of_every_preset_size(per_frame):
    run = await run_pace(per_frame=per_frame)
    assert run.progress.delivered == list(range(len(run.plan.frames)))


async def test_a_plan_without_a_final_frame_is_refused_before_any_request():
    plan = plan_for(6)
    truncated = type(plan)(**{**plan.__dict__, "frames": plan.frames[:-1]})
    clock = FakeClock()
    thread = FakeThread(clock=clock)
    message = FakeMessage(clock, ledger=thread.ledger)
    with pytest.raises(ValueError, match="FINAL"):
        await broadcast.pace_broadcast(
            truncated,
            make_inputs(),
            message=message,
            thread=thread,
            settings=broadcast.BroadcastSettings(),
            clock=clock,
            sleep=clock.sleep,
            progress=broadcast.PaceProgress(),
        )
    assert message.calls == [] and thread.ledger == []


# --- exactly one request a tick (T-8 #10) ---------------------------------------------


async def test_exactly_one_request_per_tick_over_a_whole_game_boundary_and_jumped_ticks_included():
    plan = plan_for(6)
    q2_end = _frame_index(plan, FrameKind.PERIOD_END, period=2)
    run = await run_pace(script=jump(q2_end - 1, q2_end + 1))
    assert run.progress.skipped == 1
    for tick in run.ticks():
        assert len(tick) == 1, tick
    assert len(run.ticks()) == len(run.progress.delivered)


async def test_every_edit_carries_both_the_embed_and_the_view():
    run = await run_pace()
    for tick in run.ticks():
        (request,) = tick
        assert "embed" in request.kwargs and "view" in request.kwargs
    assert len(run.view_calls) == len(run.plan.frames)


async def test_the_view_request_reflects_visible_periods_and_the_shared_selection():
    calls: list[ComponentViewRequest] = []
    run = await run_pace(component_view_for=fake_component_view_for(calls))
    for frame, request in zip(run.plan.frames, calls, strict=True):
        last = last_line_index(frame, run.plan)
        assert request.periods == visible_periods(run.plan, last)
        assert request.selected == 0  # nothing ever selects for the pacer
        assert request.disabled is False


async def test_a_view_builder_returning_none_omits_the_view_key_not_strips_it():
    """MAJOR 3 (critic round 1): `ComponentViewFor`'s own return type is
    `discord.ui.View | None`, and T-2.b says a `None` means OMIT the `view=` kwarg -- but
    `_component_view_or_missing` used to forward a raw `None` straight to `_bounded_edit`,
    whose `None` means STRIP the row (T-6). Restores v2's
    `test_a_view_builder_returning_none_posts_the_message_bare`, adapted to v4's
    `component_view_for`/`_bounded_edit` shapes."""
    run = await run_pace(component_view_for=lambda request: None)
    assert run.outcome == broadcast.PaceOutcome.FINISHED
    for kwargs, _ in run.message.calls:
        assert "view" not in kwargs, "a None-returning view builder stripped the row"


async def test_forwarding_a_raw_none_from_the_view_builder_instead_of_missing_is_caught():
    mutated = variant(
        (
            "    return discord.utils.MISSING if view is None else view\n",
            "    return view\n",
        )
    )
    run = await run_pace(mutated, component_view_for=lambda request: None)
    assert any(
        "view" in kwargs and kwargs["view"] is None for kwargs, _ in run.message.calls
    ), "the mutation was not exercised"


# --- the write sites, enumerated (watch-v4 T-5.b, T-8 #11) ------------------------------------


async def test_w0_writes_message_id_before_the_loop():
    run = await run_pace()
    assert run.progress.component.message_id == run.message.id


async def test_w1_writes_frame_line_index_and_more_ticks_before_the_edit():
    """Every non-final tick must have `more_ticks=True` when it was made (the FINAL
    frame flips it `False`, W1); the frame/line index track `last_line_index`."""
    run = await run_pace()
    assert run.progress.component.frame_index == run.final_index
    assert run.progress.component.line_index == last_line_index(run.plan.frames[-1], run.plan)
    assert run.progress.component.more_ticks is False


async def test_w1_more_ticks_is_true_at_every_non_final_tick_as_it_happens():
    """MINOR 8 (critic round 1): the test above only reads `more_ticks` AFTER the whole
    run, where it is unconditionally `False` (the FINAL frame's own W1 write) -- it never
    checked the docstring's claim that every non-final tick sees `True` AT THE TIME it
    was made. This captures `state.more_ticks` from `component_view_for`, which
    `pace_broadcast` always calls strictly after W1's write and strictly before that
    tick's edit, with no `await` between -- so the value it sees is exactly what W1 just
    wrote for that tick, not a later one."""
    plan = plan_for(6)
    clock = FakeClock()
    thread = FakeThread(clock=clock)
    message = FakeMessage(clock, ledger=thread.ledger)
    settings = broadcast.BroadcastSettings(plan=plan.settings)
    progress = broadcast.PaceProgress()
    seen: list[bool] = []

    def component_view_for(request):
        seen.append(progress.component.more_ticks)
        return None

    outcome = await broadcast.pace_broadcast(
        plan,
        make_inputs(),
        message=message,
        thread=thread,
        settings=settings,
        clock=clock,
        sleep=clock.sleep,
        progress=progress,
        narration=narration_for(plan),
        component_view_for=component_view_for,
    )
    assert outcome == broadcast.PaceOutcome.FINISHED
    assert len(seen) == len(plan.frames)
    assert seen[:-1] == [True] * (len(seen) - 1), "every non-final tick must see more_ticks=True"
    assert seen[-1] is False, "the FINAL frame's own tick must see more_ticks=False"


async def test_shown_through_advances_only_on_a_successful_edit_never_on_a_failure():
    """T-8 #11: one parametrised check per row of T-5.b's table that this module can
    reach without a click (W2, W3, W5) -- an omission was the original finding, so this
    enumerates rather than samples."""
    # W2: a successful mid-loop edit.
    run = await run_pace()
    assert run.progress.component.shown_through == last_line_index(run.plan.frames[-1], run.plan)

    # Every mid-loop tick failing entirely (EDIT_FAILURES): the cap never left -1.
    all_fail = await run_pace(fail_if=lambda r: True)
    assert all_fail.outcome == broadcast.PaceOutcome.EDIT_FAILURES
    assert all_fail.progress.component.shown_through == -1

    # W5: FINAL_LOST -- every final attempt AND the new-message post fail. The cap
    # must stay at the last successful mid-loop tick and `terminal` becomes True.
    n = len(plan_for(6).frames)
    lost = await run_pace(thread_fail=True, fail_if=lambda r: r.number >= n - 1)
    assert lost.outcome == broadcast.PaceOutcome.FINAL_LOST
    assert lost.progress.component.terminal is True
    assert lost.progress.component.shown_through == last_line_index(lost.plan.frames[-2], lost.plan)


async def test_w3_and_w4_advance_the_cap_on_finished_and_finished_as_new_message():
    # W3: happy path, FINISHED.
    run = await run_pace()
    assert run.outcome == broadcast.PaceOutcome.FINISHED
    assert run.progress.component.shown_through == last_line_index(run.plan.frames[-1], run.plan)

    # W4: every final edit fails, so it posts as a new message.
    n = len(plan_for(6).frames)
    new_message_run = await run_pace(script=lambda clock: failing(n - 1, n, n + 1))
    assert new_message_run.outcome == broadcast.PaceOutcome.FINISHED_AS_NEW_MESSAGE
    assert new_message_run.progress.component.shown_through == last_line_index(
        new_message_run.plan.frames[-1], new_message_run.plan
    )
    assert new_message_run.progress.component.message_id != new_message_run.message.id


async def test_dropping_the_cap_advance_is_caught_by_the_edit_failures_scenario():
    """Mutation twin (T-8's own list: "advance shown_through before the edit -> (12)
    red"): moving the write from the success-only `else:` branch (W2) to run
    unconditionally every tick would let a broadcast whose every edit FAILED still
    claim it showed something -- exactly the `EDIT_FAILURES` scenario above, with the
    guard removed."""
    mutated = variant(
        (
            "            edit_failures = 0\n"
            "            state.shown_through = state.line_index  # W2\n",
            "            edit_failures = 0\n"
            "        state.shown_through = state.line_index  # W2, MUTATED: unconditional\n",
        )
    )
    control = await run_pace(mutated, fail_if=lambda r: True)
    assert control.progress.component.shown_through != -1, "the mutation was not exercised"


async def test_edit_failures_sets_terminal_only_through_edit_interrupted_not_the_pacer():
    """`EDIT_FAILURES` itself writes nothing (T-5.b): the pacer's own loop never sets
    `terminal`; only `_edit_interrupted` (the scheduler) does, at W6."""
    run = await run_pace(fail_if=lambda r: True)
    assert run.outcome == broadcast.PaceOutcome.EDIT_FAILURES
    assert run.progress.component.terminal is False  # not set by the pacer itself
    assert run.progress.component.shown_through == -1  # nothing ever succeeded


# --- jumps: no play is dropped between consecutive ticks (T-1.a's contract) --------------------


async def test_a_jump_over_a_period_end_still_advances_the_line_index_correctly():
    plan = plan_for(6)
    q2_end = _frame_index(plan, FrameKind.PERIOD_END, period=2)
    run = await run_pace(script=jump(q2_end - 1, q2_end + 1))
    assert run.progress.skipped == 1 and q2_end not in run.progress.delivered
    landed = run.plan.frames[q2_end + 1]
    # the tick that landed on q2_end + 1 rendered THAT frame's title, never q2_end's
    landing_position = run.progress.delivered.index(q2_end + 1)
    assert run.successful_embeds()[landing_position].title == _title(landed)
    # and by the end of the (successful) run the state reflects the final frame
    assert run.progress.component.line_index == last_line_index(run.plan.frames[-1], run.plan)


async def test_a_jump_over_a_whole_period_leaves_visible_periods_non_decreasing():
    plan = plan_for(6)
    q1_end = _frame_index(plan, FrameKind.PERIOD_END, period=1)
    q3_first = next(f.index for f in plan.frames if f.kind is FrameKind.PLAY and f.period == 3)
    calls: list[ComponentViewRequest] = []
    run = await run_pace(
        script=jump(q1_end, q3_first), component_view_for=fake_component_view_for(calls)
    )
    assert run.progress.skipped == q3_first - q1_end - 1
    seen: list[set[int]] = [set(c.periods) for c in calls]
    for earlier, later in zip(seen, seen[1:], strict=False):
        assert earlier <= later, "a period disappeared from the cycle row after a jump"
    assert seen[-1] == {1, 2, 3, 4, 5}  # every period, by FINAL


# --- request-budget accounting (T-4, corrected: 1 flat, not 2-3 as in v2/v3) -------------------


async def test_a_run_of_failed_edits_ends_the_broadcast_after_three_consecutive_failures():
    run = await run_pace(fail_if=lambda r: True)
    assert run.outcome == broadcast.PaceOutcome.EDIT_FAILURES
    assert run.progress.abandoned == broadcast.MAX_CONSECUTIVE_EDIT_FAILURES
    assert len(run.message.calls) == broadcast.MAX_CONSECUTIVE_EDIT_FAILURES


async def test_a_scoreboard_success_resets_the_consecutive_failure_count():
    n = len(plan_for(6).frames)
    run = await run_pace(fail_if=lambda r: r.number in (0, 1, 3, 4))
    assert run.outcome == broadcast.PaceOutcome.FINISHED
    assert run.progress.abandoned == 4
    assert len(run.message.calls) == n


async def test_no_mentions_are_ever_allowed():
    run = await run_pace()
    for kwargs, _ in run.message.calls:
        assert_no_mentions(kwargs)


# --- the final ladder (unchanged shape, P5-97.q item 2) -----------------------------------------


async def test_the_final_ladder_retries_with_backoff_before_giving_up():
    n = len(plan_for(6).frames)
    run = await run_pace(script=lambda clock: failing(n - 1, n, n + 1))
    # 3 failed final attempts (default final_edit_attempts), then a new message, then
    # the OLD message's best-effort pointer edit (T-5.e part 3, MAJOR 4) -- a 4th call.
    assert run.outcome == broadcast.PaceOutcome.FINISHED_AS_NEW_MESSAGE
    final_calls = run.message.calls[n - 1 :]
    assert len(final_calls) == 4


async def test_final_lost_when_neither_the_edit_nor_the_new_message_succeed():
    n = len(plan_for(6).frames)
    run = await run_pace(thread_fail=True, fail_if=lambda r: r.number >= n - 1)
    assert run.outcome == broadcast.PaceOutcome.FINAL_LOST
    assert run.progress.component.terminal is True


async def test_finished_as_new_message_points_the_old_message_at_the_new_one():
    """MAJOR 4 (critic round 1): T-5.e part 3 was never implemented -- the old message
    kept an enabled-looking button row forever after every final edit failed, even
    though a click on it was already refused (gate 1). Now a best-effort edit strips
    the row with an explicit `view=None` and a short pointer sentence."""
    n = len(plan_for(6).frames)
    run = await run_pace(script=lambda clock: failing(n - 1, n, n + 1))
    assert run.outcome == broadcast.PaceOutcome.FINISHED_AS_NEW_MESSAGE

    pointer_kwargs, _at = run.message.calls[-1]
    assert pointer_kwargs["view"] is None, "the old message's row must be explicitly stripped"
    assert broadcast._SUPERSEDED_POINTER_TEXT in pointer_kwargs["embed"].description


async def test_a_failed_pointer_edit_is_best_effort_and_does_not_change_the_outcome():
    """MAJOR 4, second half: the pointer edit is cosmetic. A failure to point the old
    message at the new one must be swallowed and must not affect `PaceOutcome`."""
    n = len(plan_for(6).frames)
    run = await run_pace(script=lambda clock: failing(n - 1, n, n + 1, n + 2))
    assert run.outcome == broadcast.PaceOutcome.FINISHED_AS_NEW_MESSAGE
    # 3 failed final attempts, then the (also failed) pointer edit: still 4 calls.
    assert len(run.message.calls) == n + 3


async def test_finished_as_new_message_new_message_carries_a_view():
    """MAJOR 6 (critic round 1): nothing asserted that the FINISHED_AS_NEW_MESSAGE post
    carried a view at all -- 'omit the view on `_bounded_send` in `_deliver_final`' could
    have passed silently."""
    n = len(plan_for(6).frames)
    run = await run_pace(script=lambda clock: failing(n - 1, n, n + 1))
    assert run.outcome == broadcast.PaceOutcome.FINISHED_AS_NEW_MESSAGE
    sent = run.thread.sent[-1]
    assert sent["view"] is not None and sent["view"] is not discord.utils.MISSING


# --- the reading modes: one line per reveal (docs/design/watch-narration-v2.md WN-10) -----


@dataclass
class ReadingRun:
    outcome: Any
    progress: Any
    message: FakeMessage
    clock: FakeClock
    plan: Any
    narration: Any
    schedule: tuple


async def run_reading(module=broadcast, *, plan_settings=GAME_PLAN_SETTINGS, script=None):
    plan = plan_with(plan_settings)
    narration = narration_for(plan)
    clock = FakeClock()
    thread = FakeThread(clock=clock)
    message = FakeMessage(clock, script(clock) if script else None, ledger=thread.ledger)
    settings = module.BroadcastSettings(plan=plan.settings)
    progress = module.PaceProgress()
    outcome = await module.pace_broadcast(
        plan,
        make_inputs(module),
        message=message,
        thread=thread,
        settings=settings,
        clock=clock,
        sleep=clock.sleep,
        progress=progress,
        narration=narration,
    )
    schedule = reveal_schedule(plan, narration, tick_seconds=settings.tick_seconds)
    return ReadingRun(outcome, progress, message, clock, plan, narration, schedule)


@pytest.mark.parametrize(
    "plan_settings", [GAME_PLAN_SETTINGS, FULL_PLAN_SETTINGS], ids=["game", "full"]
)
async def test_a_reading_mode_edits_once_per_line_on_its_reading_deadline(plan_settings):
    """T8 (WN-10.2): one edit per reveal, each at `started + due`; every PLAY/PERIOD_END
    edit shows exactly one new line, marked as the newest; every frame is delivered
    once, in order; no two edits closer than the 2.0 s floor."""
    run = await run_reading(plan_settings=plan_settings)
    assert run.outcome == broadcast.PaceOutcome.FINISHED
    assert (run.progress.skipped, run.progress.abandoned) == (0, 0)
    assert len(run.message.calls) == len(run.schedule)
    start = run.message.calls[0][1]
    assert [at for _, at in run.message.calls] == [start + r.due_seconds for r in run.schedule]
    gaps = [b - a for (_, a), (_, b) in zip(run.message.calls, run.message.calls[1:], strict=False)]
    # Each gap is a difference of two cumulative float sums, so an exact 2.0 s hold can
    # read 1.9999999999999 (seed 12's game does); 1e-9 s is float noise, not a faster edit.
    assert min(gaps) >= 2.0 - 1e-9
    shown = -1
    for (kwargs, _), reveal in zip(run.message.calls, run.schedule, strict=True):
        frame = run.plan.frames[reveal.frame_index]
        description = kwargs["embed"].description
        if frame.kind in (FrameKind.PLAY, FrameKind.PERIOD_END) and reveal.cursor > shown:
            assert reveal.cursor == shown + 1, "a reveal showed more than one new line"
            assert description.endswith("> " + run.narration.lines[reveal.cursor].text)
        shown = max(shown, reveal.cursor)
    assert run.progress.delivered == list(range(len(run.plan.frames)))


async def test_a_reading_modes_duration_is_its_schedules_last_deadline():
    run = await run_reading(plan_settings=FULL_PLAN_SETTINGS, script=latency(1.0))
    assert run.progress.skipped == 0
    final_at = run.message.calls[-1][1]
    assert final_at == run.message.calls[0][1] + run.schedule[-1].due_seconds


async def test_a_late_reading_pacer_skips_lines_never_content_and_lands_on_final():
    """WN-10.5: a stall jumps to the due LINE, `skipped` counts lines, the jump
    reveals several lines in one edit (the ticker shows the last window regardless),
    the header lands on the jumped-to line's score, and FINAL is never skipped."""
    stall_at = 40

    def factory(clock: FakeClock):
        async def on_call(n: int, kwargs) -> None:
            if n == stall_at:
                clock.now += 30.0

        return on_call

    run = await run_reading(plan_settings=FULL_PLAN_SETTINGS, script=factory)
    assert run.outcome == broadcast.PaceOutcome.FINISHED
    assert run.progress.skipped > 0
    assert len(run.message.calls) == len(run.schedule) - run.progress.skipped
    assert run.message.calls[-1][0]["embed"].description.startswith("**FINAL")


async def test_w1_writes_the_cursor_and_the_completed_possession_at_every_reveal():
    """WN-9.1: `ComponentState.cursor` is the reveal's line; `line_index` is the newest
    possession whose TERMINAL that line shows -- a pre-line never completes its own
    possession, so the pager cap and the stats never run ahead of the feed."""
    seen: list[tuple[int, int]] = []
    holder: dict[str, Any] = {}

    def factory(clock: FakeClock):
        async def on_call(n: int, kwargs) -> None:
            state = holder["progress"].component
            seen.append((state.cursor, state.line_index))

        return on_call

    original = broadcast.PaceProgress

    class Recording(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            holder["progress"] = self

    module = variant()
    module.PaceProgress = Recording
    run = await run_reading(module, plan_settings=FULL_PLAN_SETTINGS, script=factory)
    assert len(seen) == len(run.schedule)
    for (cursor, line_index), reveal in zip(seen, run.schedule, strict=True):
        assert cursor == reveal.cursor
        assert line_index == completed_through(run.narration, cursor)
    lines = run.narration.lines
    assert any(
        lines[c].kind in CHAIN_LINE_KINDS and li == lines[c].possession - 1
        for c, li in seen
        if c >= 0
    )
