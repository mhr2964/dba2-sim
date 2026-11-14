"""Shared fakes for `dba2/bot/broadcast.py`'s tests (`test_broadcast_*.py`): a
message and thread that record every request, hand back the messages they post
and can be told to fail chosen requests, a fake clock whose `sleep` moves it,
one real seeded game planned at the three presets, and a loader for a mutated
private copy of the module under test (the mutation twins).

No DB, no gateway, no network -- `discord.Embed` and `AllowedMentions` are
plain local data."""

from __future__ import annotations

import asyncio
import functools
import sys
import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord

from dba2.bot import broadcast
from dba2.presentation.broadcast import BroadcastPlan, PlanSettings, plan_broadcast
from dba2.sim.events import GameResult
from dba2.sim.game import simulate_game
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_game_context
from tests.unit.presentation._broadcast_helpers import make_team, names_for

GAME_ID = 412
HOME = make_team(1, "HOM")
AWAY = make_team(2, "AWY")


@functools.cache
def sim_result() -> GameResult:
    """One real game (seed 12, an overtime game -- the presentation goldens'
    seed; was seed 6 until the assist-compression fix, then seed 48, then
    briefly 37, then 36, then 5 until RC-4/RC-16's minutes-curve fix
    (docs/design/auto-gm-role-construction.md) moved who's on the floor late
    and moved which seed reaches overtime again for this fixture context.
    PD-10 (docs/design/gm-command-surface.md) then wired `offensive_scheme`
    in -- unlike every prior mover, `OffensiveScheme` has no neutral member
    (PD-10.4), so this fixture's default `MOTION` scheme was never inert --
    and RC-20/the per-36-spread-compression rebound-attribution fix landed
    on the same branch around the same time, moving every seed's late-game
    margin again. Seed 5 no longer reaches overtime under the combined
    engine; seed 41 does, and is back inside the original 0-49 net, see
    `test_pre_change_golden_seeds_0_49.py`'s own note. per-36-spread-
    compression Commit 4 (M1, re-anchoring ranks 1-5 a second time,
    2026-09-23) moved it once more -- seed 41 no longer reaches overtime,
    seed 13 does; pick-and-pop C3 (TUNING_VERSION 18 -> 19) moved it again -- seed 13 no
    longer reaches overtime, seed 68 does; its C4 calibration (19 -> 20) moved it back
    inside 0-49, to seed 18; turnover-recovery (TUNING_VERSION 20 -> 21) left
    none in 0-49 and moved it to seed 146; the weighted recoverer draw (21 -> 22)
    moved it back inside 0-49, to seed 27; block-attribution-v2 (TUNING_VERSION 23 -> 25) moved it
    to seed 21 (scanned 0-99: 21 only); the scale-13 correction (TUNING_VERSION 25 -> 26)
    moved it to seed 12 (scanned 0-99: 12 only)), simmed once per process."""
    return simulate_game(make_game_context(GAME_ID, tuning=DEFAULT_TUNING), rng_seed=12)


@functools.cache
def plan_for(possessions_per_frame: int) -> BroadcastPlan:
    return plan_broadcast(
        sim_result(),
        game_id=GAME_ID,
        home_code=HOME.code,
        away_code=AWAY.code,
        settings=PlanSettings(possessions_per_frame=possessions_per_frame),
    )


def make_inputs(module: types.ModuleType = broadcast):
    result = sim_result()
    return module.BroadcastInputs(home=HOME, away=AWAY, names=names_for(result), result=result)


def period_lines_for(plan: BroadcastPlan):
    """The pager's source for `plan`: the plan's TERSE lines, regrouped by period
    (watch-v4 T-6, docs/design/watch-narration-v2.md WN-9.4)."""
    from dba2.presentation.broadcast import plan_period_lines

    return plan_period_lines(plan, home=HOME, away=AWAY, names=names_for(sim_result()))


def plan_with(settings):
    """A plan of `sim_result()` at any `PlanSettings` -- a `/watch` speed's own plan
    (docs/design/watch-narration-v2.md WN-10.4)."""
    return plan_broadcast(
        sim_result(), game_id=GAME_ID, home_code=HOME.code, away_code=AWAY.code, settings=settings
    )


def narration_for(plan: BroadcastPlan):
    """`pace_broadcast`'s `narration` argument for `plan`: the game rendered once as
    lines at the plan's own level (docs/design/watch-narration-v2.md WN-8)."""
    from dba2.presentation.broadcast import plan_narration

    result = sim_result()
    return plan_narration(result, plan, home=HOME, away=AWAY, names=names_for(result))


def fake_component_view_for(calls: list[Any] | None = None):
    """A `ComponentViewFor` double: records the `ComponentViewRequest` it was
    called with (if `calls` is given) and returns a sentinel object standing in
    for a `discord.ui.View`, so a test can assert it rode on the edit without
    needing a real Discord view."""

    class _FakeView:
        def __init__(self, request: Any) -> None:
            self.request = request

    def component_view_for(request: Any) -> Any:
        if calls is not None:
            calls.append(request)
        return _FakeView(request)

    return component_view_for


class FakeClock:
    """A `loop.time`-shaped callable and a `sleep` that advances it. `sleep`
    refuses a negative duration (the pacer must never ask for one) and yields
    once so other tasks run, as a real sleep would."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        assert seconds >= 0.0, f"the pacer asked for a negative sleep: {seconds}"
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


OnCall = Callable[[int, dict[str, Any]], Awaitable[None]]


@dataclass
class Request:
    """One Discord request the fakes saw: an `edit` of an existing message or a
    `send` that creates one. `number` counts every request of the run in arrival
    order (across the scoreboard, the period messages and the recap), `at` is the
    fake clock when it arrived, and `failed` says whether the fakes made it raise.
    A failed request is still counted: the pacer spent it."""

    number: int
    kind: str
    message_id: int
    kwargs: dict[str, Any]
    at: float
    failed: bool = False


FailIf = Callable[[Request], bool]


class FakeMessage:
    """Records every `edit` (including failed ones) as `(kwargs, clock time)`.
    `on_call(n, kwargs)` runs first, `n` counting from 0, and may advance the
    clock, hang, or raise to script a failure. Without a clock every call is
    recorded at time 0.0. `ledger` is a shared list every request is also
    appended to (the thread's, so one run's requests read in order), `fail_if`
    decides per request whether it raises `HTTPException`, and `latency` seconds
    are spent on the clock by every request, failed or not."""

    def __init__(
        self,
        clock: FakeClock | None = None,
        on_call: OnCall | None = None,
        *,
        message_id: int = 555,
        ledger: list[Request] | None = None,
        fail_if: FailIf | None = None,
        latency: float = 0.0,
    ) -> None:
        self.id = message_id
        self.jump_url = f"https://discord.test/channels/1/777/{message_id}"
        self.clock = clock
        self.on_call = on_call
        self.ledger = ledger
        self.fail_if = fail_if
        self.latency = latency
        self.calls: list[tuple[dict[str, Any], float]] = []

    async def edit(self, **kwargs: Any) -> None:
        n = len(self.calls)
        now = self.clock.now if self.clock else 0.0
        self.calls.append((kwargs, now))
        request = Request(
            len(self.ledger) if self.ledger is not None else n, "edit", self.id, kwargs, now
        )
        if self.ledger is not None:
            self.ledger.append(request)
        if self.clock is not None:
            self.clock.now += self.latency
        if self.fail_if is not None and self.fail_if(request):
            request.failed = True
            raise http_error()
        if self.on_call is not None:
            await self.on_call(n, kwargs)


class FakeThread:
    """A thread that records every `send` (`sent`, the kwargs; `ledger`, the
    `Request`s of the whole run, including the edits of every message it made)
    and RETURNS the message it created, a `FakeMessage` wired to the same
    ledger, clock, failure predicate and latency. `fail=True` makes every send
    raise; `fail_if` fails chosen requests, sends and edits alike."""

    def __init__(
        self,
        *,
        fail: bool = False,
        thread_id: int = 777,
        clock: FakeClock | None = None,
        fail_if: FailIf | None = None,
        latency: float = 0.0,
        first_message_id: int = 9000,
    ) -> None:
        self.id = thread_id
        self.fail = fail
        self.clock = clock
        self.fail_if = fail_if
        self.latency = latency
        self._next_id = first_message_id
        self.sent: list[dict[str, Any]] = []
        self.ledger: list[Request] = []
        self.messages: list[FakeMessage] = []

    async def send(self, **kwargs: Any) -> FakeMessage:
        self.sent.append(kwargs)
        request = Request(
            len(self.ledger), "send", self._next_id, kwargs, self.clock.now if self.clock else 0.0
        )
        self.ledger.append(request)
        if self.clock is not None:
            self.clock.now += self.latency
        if self.fail or (self.fail_if is not None and self.fail_if(request)):
            request.failed = True
            raise http_error("thread send failed")
        message = FakeMessage(
            self.clock,
            message_id=self._next_id,
            ledger=self.ledger,
            fail_if=self.fail_if,
            latency=self.latency,
        )
        message.jump_url = f"https://discord.test/channels/1/{self.id}/{self._next_id}"
        self._next_id += 1
        self.messages.append(message)
        return message

    def requests_at(self) -> list[list[Request]]:
        """The ledger grouped by the fake-clock time each request arrived, in
        time order: with instant requests that is one group per pacer tick (a
        final-ladder retry sleeps between requests, so it gets its own)."""
        groups: dict[float, list[Request]] = {}
        for request in self.ledger:
            groups.setdefault(request.at, []).append(request)
        return [groups[at] for at in sorted(groups)]


class _FakeResponse:
    status = 500
    reason = "Internal Server Error"


def http_error(message: str = "edit failed") -> discord.HTTPException:
    return discord.HTTPException(_FakeResponse(), message)


def failing(*calls: int, latency: float = 0.0, clock: FakeClock | None = None) -> OnCall:
    """An `on_call` that raises `HTTPException` on exactly the given call
    numbers; every call first spends `latency` seconds on `clock`."""
    bad = set(calls)

    async def on_call(n: int, kwargs: dict[str, Any]) -> None:
        if latency and clock is not None:
            clock.now += latency
        if n in bad:
            raise http_error()

    return on_call


def assert_no_mentions(kwargs: dict[str, Any]) -> None:
    """The P5-97.s rule as one assertion: the call carries an
    `AllowedMentions` that permits nothing."""
    mentions = kwargs.get("allowed_mentions")
    assert isinstance(mentions, discord.AllowedMentions), f"no allowed_mentions in {kwargs!r}"
    assert not mentions.everyone
    assert not mentions.roles
    assert not mentions.replied_user
    assert not mentions.users


class MutationAnchorError(Exception):
    """Not an `AssertionError` on purpose: a twin expects the SCENARIO's
    assertion to fail, and a stale anchor must not be able to pass for that."""


def variant(*mutations: tuple[str, str]) -> types.ModuleType:
    """A private copy of `dba2/bot/broadcast.py` with each `(old, new)`
    mutation applied, loaded under a scratch name so the real module is
    untouched. An anchor must occur exactly once: a twin that silently mutates
    nothing proves nothing. With no mutations it is the control -- a twin's
    scenario must PASS on it, or "red" could just be a loading failure."""
    source = Path(broadcast.__file__).read_text(encoding="utf-8")
    for old, new in mutations:
        if source.count(old) != 1:
            raise MutationAnchorError(f"mutation anchor {old!r} is not unique in the source")
        source = source.replace(old, new)
    module = types.ModuleType("_bot_broadcast_variant")
    module.__package__ = "dba2.bot"  # the module's `from .embeds import ...`
    sys.modules[module.__name__] = module  # `@dataclass` resolves annotations through it
    try:
        exec(compile(source, "<bot broadcast variant>", "exec"), module.__dict__)
    finally:
        del sys.modules[module.__name__]
    return module
