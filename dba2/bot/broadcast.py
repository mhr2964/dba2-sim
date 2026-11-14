"""The Discord half of Stage D's `/watch` broadcast: how fast a game is shown,
and the task that walks a frame plan onto a thread -- one scoreboard message
edited every tick plus one message per period chunk that is posted when its
first play arrives and frozen when the period ends
(docs/design/phase-5-stage-d-broadcast.md §6, Decisions P5-97.h/.i/.q;
docs/design/watch-v2-scrollable-broadcast.md).

**The plan is pure; this module owns every clock and every await.**
`dba2/presentation/broadcast.py` decides WHAT each frame says and never sleeps
(a layering test forbids `asyncio`/`time` there), so everything that depends on
wall-clock time lives here, behind two injected callables -- `clock`
(`loop.time`-shaped) and `sleep` -- so a 40-frame broadcast runs in
milliseconds in a unit test and the pacer's arithmetic can be driven by a fake
clock.

**Deadline pacing, not accumulated sleeps (P5-97.i).** The pacer walks a
schedule of reveals (`presentation.broadcast.reveal_schedule`,
docs/design/watch-narration-v2.md WN-10): in `fast` one per frame at
`started + i * tick_seconds`, in the two reading modes one per LINE at the
cumulative reading time of the lines before it. Before each reveal it asks
which reveal is really due; if that is past the current one it SKIPS FORWARD to
it, so a slow channel costs latency and never content: every render is a full
snapshot of the plan and the narration up to the target, never of which reveals
were delivered (watch-v2 W-8). Each tick RECONCILES the
period messages before it edits the scoreboard: a chunk that never posted is
posted, a chunk showing fewer lines than the target frame implies (or not yet
frozen when its period is over) is edited, in period order. So a jump over a
PERIOD_END frame, or over a whole period, leaves no gap, and a request that
failed is simply retried on the next tick. The pacer owns that message set; its
record lives in the caller's `PaceProgress` so it outlives a cancellation.

**The FINAL frame is never skipped and is retried (P5-97.q item 2).** It is the
one frame whose loss the viewer would notice, so its scoreboard edit gets
`final_edit_attempts` bounded edits with a short backoff, then a fresh message
in the thread, and only if that fails too is the broadcast reported
interrupted. The recap that follows is the scheduler's, not the pacer's.

**The scheduler bounds the whole thing (P5-97.j).** `BroadcastScheduler` admits
a broadcast (or refuses it), then runs it as a task whose FIRST statement is
acquiring the concurrency semaphore -- so `/watch` never blocks on a slot and a
queued broadcast is just a task waiting on it. Admission is one plain integer
counter, checked and incremented with no `await` between the two: on a single
event loop that adjacency is the whole correctness argument.

**The finalizer is best-effort and the startup sweep is the guarantee (P5-97.q
item 3).** `_run_broadcast_guarded` cleans up in a `finally`, but a `finally`
does NOT always run -- a process kill, loop shutdown, or a `Task.cancel()`
landing at the wrong await all bypass it. `game_feed.sweep_league` (commit 9)
is what makes a stuck `queued`/`live` row converge -- and repairs a panel the
finalizer never got to edit; this block only makes the common case fast, and
nothing here may be read as a promise that it ran. On a graceful stop
(`shutdown`) the cancelled task makes one bounded, shielded attempt to edit its
viewer's message to the interrupted panel, so a stop does not leave a thread
frozen mid-game; the attempt is best-effort like the rest.

**Every send and edit passes `AllowedMentions.none()` (P5-97.s).** A team or
player name is a database string; a league that names a team `@everyone` must
not be able to ping a guild through a scoreboard. The two helpers that touch
Discord (`_bounded_edit`, `_bounded_send`) are the only places a message is
written, so the rule has exactly two sites.

**Edits also pass `content=None`.** The message the pacer edits is `/watch`'s
placeholder ("Waiting for a broadcast slot ..."), which is `content`; an
embed-only edit would leave that text above the scoreboard.
"""

from __future__ import annotations

import asyncio
import bisect
import math
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

import discord

from dba2.core.logging import get_logger
from dba2.data.db import Executor
from dba2.data.repositories import broadcast_repo
from dba2.domain.broadcast import BroadcastStatus
from dba2.domain.team import Team
from dba2.presentation.broadcast import (
    FAST_PLAN_SETTINGS,
    FULL_PLAN_SETTINGS,
    GAME_PLAN_SETTINGS,
    BroadcastPlan,
    FrameKind,
    Narration,
    PlanSettings,
    completed_through,
    plan_narration,
    render_interrupted,
    render_live_component,
    reveal_schedule,
    visible_periods,
)
from dba2.presentation.panel import Panel
from dba2.sim.events import GameResult

from .embeds import panel_to_embed

log = get_logger(__name__)


def _require_finite_positive(owner: str, name: str, value: object) -> None:
    """A tick or timeout of 0 is an infinite busy-loop against Discord's global
    50 req/s ceiling; NaN and infinity make the arithmetic meaningless (`nan`
    fails every comparison, so `not (value > 0)` is the check that catches it)."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not value > 0
    ):
        raise ValueError(f"{owner}.{name} must be a finite number > 0, got {value!r}")


def _require_int_at_least(owner: str, name: str, value: object, floor: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < floor:
        raise ValueError(f"{owner}.{name} must be an integer >= {floor}, got {value!r}")


@dataclass(frozen=True)
class BroadcastSettings:
    """League-feel knobs for the broadcast (P5-97.h) -- not `Config`, which
    stays deployment secrets only. The default is the `game` speed
    (docs/design/watch-narration-v2.md WN-10.4): one line per edit on the
    reading-time schedule, about 13 minutes a game; `FAST_FORWARD` (today's
    batch, ~1.5 minutes) and `WATCH_ALONG` (the full narration) are the other two
    speeds, reached through `settings_for_speed`. `tick_seconds` paces only a
    frame-reveal plan (`fast`); a reading mode is paced by its plan's reading
    constants and never edits closer than `read_min_seconds` (2.0 s).

    No Discord rate number is hard-coded: Discord publishes none (buckets are
    discovered from response headers and `discord.py` already backs off on a
    429). The one fixed figure is the global 50 requests/second per bot: four
    `fast` broadcasts are `4 / 1.5s = 2.67` edits/second, 5.3% of it (watch-v5
    WT-2); four in a reading mode stay under 4%. Against the per-message bucket's
    own 5-second window (5 edits), with clicks at the 4 s cooldown: on AVERAGE `fast`
    runs 3.3 + 1.25 = 4.6 edits per 5 seconds and the reading modes about 2.6; the
    WORST 5-second window holds 4 `fast` reveals (the 1.5 s tick) or 3 reading-mode
    reveals (the 2.0 s floor), plus 2 clicks -- 6 edits (120% of the bucket) and 5
    (100%). Both pinned in `test_broadcast_settings.py`. `discord.py` stalls rather
    than errors when a bucket is exhausted (`http.py:456-483`), so the failure mode
    is a late reveal or a jump, not a 429; `progress.skipped` is the number that
    says whether the pacer is outrunning Discord. `max_queue_depth`
    bounds only the broadcasts WAITING for a slot: admission is refused once
    live plus waiting reaches `max_concurrent_broadcasts + max_queue_depth`
    (default 4 running + 12 waiting), so `0` means "no waiting" -- the
    broadcasts that fit in the slots still run."""

    tick_seconds: float = 1.5
    plan: PlanSettings = GAME_PLAN_SETTINGS  # the `game` speed (WN-10.4)
    max_concurrent_broadcasts: int = 4
    max_queue_depth: int = 12
    max_live_broadcasts_per_user: int = 2
    edit_timeout_seconds: float = 10.0
    interrupted_edit_timeout_seconds: float = 5.0
    final_edit_attempts: int = 3

    def __post_init__(self) -> None:
        """Refuse a setting the pacer would misuse rather than reject, and name
        it: a `tick_seconds` of 0 busy-loops against Discord, a
        `possessions_per_frame` of 0 is a `ZeroDivisionError` in the planner
        (design M1)."""
        owner = "BroadcastSettings"
        _require_finite_positive(owner, "tick_seconds", self.tick_seconds)
        _require_finite_positive(owner, "edit_timeout_seconds", self.edit_timeout_seconds)
        _require_finite_positive(
            owner, "interrupted_edit_timeout_seconds", self.interrupted_edit_timeout_seconds
        )
        _require_int_at_least(owner, "final_edit_attempts", self.final_edit_attempts, 1)
        _require_int_at_least(owner, "max_concurrent_broadcasts", self.max_concurrent_broadcasts, 1)
        _require_int_at_least(owner, "max_queue_depth", self.max_queue_depth, 0)
        _require_int_at_least(
            owner, "max_live_broadcasts_per_user", self.max_live_broadcasts_per_user, 1
        )
        if not isinstance(self.plan, PlanSettings):
            raise ValueError(f"{owner}.plan must be a PlanSettings, got {self.plan!r}")
        # `PlanSettings` validates itself when constructed; these re-checks are
        # for a plan that got past that (`object.__setattr__`, a subclass), so
        # the fields the design names cannot reach the planner unchecked.
        for name in (
            "possessions_per_frame",
            "momentum_window_possessions",
            "ticker_window_lines",
        ):
            _require_int_at_least(f"{owner}.plan", name, getattr(self.plan, name), 1)


class WatchSpeed(StrEnum):
    """The viewer's `/watch speed:` choice (docs/design/watch-narration-v2.md
    WN-10.4). Names are the viewer's, not the preset constants'."""

    FAST = "fast"
    """Today's FAST_FORWARD: four possessions per 1.5 s tick, one line each, no
    separators -- about a minute and a half."""
    GAME = "game"
    """One line per possession, one line per edit on the reading-time schedule, with
    period separators -- about 13 minutes. The default."""
    FULL = "full"
    """The full narration (who has the ball, the screen, the help, the pass), one
    line per edit -- about 44 minutes ("watch-along"; measured 44.2 over seeds 0-49,
    against the design's 34, WN-16)."""


DEFAULT_BROADCAST_SETTINGS = BroadcastSettings()  # the `game` speed
FAST_FORWARD = BroadcastSettings(tick_seconds=1.5, plan=FAST_PLAN_SETTINGS)
WATCH_ALONG = BroadcastSettings(plan=FULL_PLAN_SETTINGS)
_SPEED_PRESETS: Mapping[WatchSpeed, BroadcastSettings] = {
    WatchSpeed.FAST: FAST_FORWARD,
    WatchSpeed.FULL: WATCH_ALONG,
}


def settings_for_speed(base: BroadcastSettings, speed: WatchSpeed) -> BroadcastSettings:
    """The pacing settings for one broadcast at `speed`. A speed is a TICK and a PLAN
    only: admission, timeouts and the FINAL ladder stay `base`'s, the scheduler's own.
    `game` is `base` itself -- the scheduler is built with the default speed's
    settings (`DEFAULT_BROADCAST_SETTINGS` in production), so a test that builds one
    with a custom plan keeps it."""
    preset = _SPEED_PRESETS.get(speed)
    if preset is None:
        return base
    return replace(base, tick_seconds=preset.tick_seconds, plan=preset.plan)


MAX_CONSECUTIVE_EDIT_FAILURES = 3
"""Three TICKS in a row whose edit of the ONE live component failed or timed out end the
broadcast through the interrupted path (P5-97.i item 4, docs/design/watch-v4-live-ticker.md
T-7). A v4 tick is exactly one request (T-4) -- there is no separate period message or
recap any more, so this is simply "the component's own edit failed three times running";
a success resets the count. Not a setting: a league-feel knob for "how broken is Discord
before we stop" would have no sensible non-default."""

SHUTDOWN_FINALIZER_TIMEOUT_SECONDS = 10.0
"""How long `BroadcastScheduler.shutdown` waits for finalizers (a status write
and the reveal) that were already running when it was called. A bound, not a
promise: the startup sweep converges whatever a stuck finalizer left."""

_FINAL_RETRY_BACKOFF_SECONDS = 1.0
"""Before final-frame attempt `n` (n >= 1) the pacer waits `n` of these: 1s, 2s
at the default three attempts. Short on purpose -- the broadcast is already at
its end and the viewer is watching the last frame."""

_INTERRUPTED_LOCK_WAIT_SECONDS = 1.0
"""How long `_edit_interrupted` waits for `ComponentState.edit_lock` before
painting anyway (docs/design/watch-v4-live-ticker.md T-5.d, "Ordering"): it
must never be delayed indefinitely by a viewer holding the lock across a
cycle-click edit. The same bound `watch_pager.py`'s click path uses to wait
for the same lock, named separately because the two live in different
modules."""

_SUPERSEDED_POINTER_TEXT = "This broadcast continued in a new message."
"""T-5.e part 3: the best-effort sentence the OLD message gets once the FINAL
frame had to be posted fresh. Cosmetic only -- gate 1 of T-5.d already makes
the old message's buttons dead regardless of whether this edit lands."""


class BroadcastMessage(Protocol):
    """The thread message the pacer edits: `/watch`'s placeholder, a real
    channel message and never an interaction followup (P5-97.l). Duck-typed so
    a test needs no gateway; `discord.Message` satisfies it."""

    id: int
    jump_url: str

    async def edit(
        self,
        *,
        content: str | None,
        embed: discord.Embed,
        allowed_mentions: discord.AllowedMentions,
        view: discord.ui.View | None = ...,
    ) -> Any: ...


class BroadcastThread(Protocol):
    """Where the period messages and the recap are posted, and where the final
    frame goes as a NEW message when editing it keeps failing. `send` returns
    the message it posted: the pacer edits it and the recap links to it.
    `discord.Thread` satisfies it."""

    id: int

    async def send(
        self,
        *,
        embed: discord.Embed,
        allowed_mentions: discord.AllowedMentions,
        view: discord.ui.View = ...,
    ) -> BroadcastMessage: ...


@dataclass(frozen=True)
class BroadcastInputs:
    """What the renderers need besides the plan and a frame: read from the
    database once, before the task starts (design §4.2), so the task itself
    never queries."""

    home: Team
    away: Team
    names: Mapping[int, str]
    result: GameResult


class PaceOutcome(StrEnum):
    FINISHED = "finished"
    """The final frame landed by editing the broadcast message."""
    FINISHED_AS_NEW_MESSAGE = "finished_as_new_message"
    """Every final-frame edit failed; the final frame was posted as a new
    message in the thread. Still a finished broadcast (P5-97.q item 2)."""
    EDIT_FAILURES = "edit_failures"
    """`MAX_CONSECUTIVE_EDIT_FAILURES` ticks in a row had their scoreboard edit
    fail; the caller ends the broadcast through the interrupted path."""
    FINAL_LOST = "final_lost"
    """The final frame could neither be edited in nor posted; the caller
    records `interrupted`."""


@dataclass
class ComponentState:
    """The one shared, mutable object the pacer writes and a cycle click reads
    (docs/design/watch-v4-live-ticker.md T-5.a): the live component's identity,
    what it is showing, what it is finished showing, and the shared cycle
    selection. `PaceProgress.component` is the only home for it -- the pager
    already gets `PaceProgress` itself, by reference, so this is the one seam
    where the pacer's truth and the pager's truth are already the same bytes.

    Every write site is enumerated in T-5.b (W0-W8); nothing here is written
    from any other place. `edit_lock` orders a cycle click's edit against the
    interrupted panel's (T-5.d, "Ordering") -- held by the click and by
    `BroadcastScheduler._edit_interrupted`, never by the pacer's own loop,
    which must never wait on a viewer."""

    message_id: int | None = None
    """The id of the message that IS the live component right now. Written at
    W0/W4; a click whose `interaction.message.id` disagrees is refused (T-5.e)."""
    frame_index: int = -1
    """Index into `plan.frames` of the frame the component shows or is being
    painted with. `-1` means never painted (a task cancelled before its first
    statement): a click then refuses rather than rendering nothing."""
    cursor: int = -1
    """The flat narration line the component shows through (docs/design/watch-
    narration-v2.md WN-9.1): one per reveal in a reading mode, a whole frame's worth
    in `fast`. Written with `frame_index` at W1; the click renders at it."""
    line_index: int = -1
    """The newest possession whose ending that cursor shows (`completed_through`) --
    what the stats slice, the line score, the cycle row and the pager's spoiler cap
    read, so none of them counts a possession whose TERMINAL line is not on screen.
    At a frame's own cursor it is that frame's `last_line_index`. The click NEVER
    computes one of its own; it reads this."""
    shown_through: int = -1
    """`line_index` of the last render Discord ACCEPTED -- the pager's spoiler
    cap. Advances only after a successful edit (W2/W3/W4/W7), never on a
    failure, so the full-text pager can never show a play the component has
    not."""
    selected_period: int = 0
    """The shared cycle selection; `0` is Full Game. Written only by a cycle
    click (W8), and only when a paint is guaranteed."""
    terminal: bool = False
    """The broadcast has been finalized as interrupted/lost; no live-looking
    render may follow. Written by W5/W6; gates a click both before and after
    the click's own lock acquire (T-5.d, "Ordering")."""
    more_ticks: bool = False
    """The pacer will edit this message again. T-4.a's coalescing gate: `False`
    for the whole FINAL frame's delivery (W1) and after W6, so a click past
    that point always paints instead of deferring into a tick that will never
    come (T-4.a.1)."""
    last_edit_monotonic: float = 0.0
    """`time.monotonic()` as of the last click edit (W7) -- T-4.a's cooldown
    stamp. The pacer's own ticks do not read or write this; they tick on their
    own deadline schedule regardless."""
    edit_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    """Orders the click's edit against `_edit_interrupted`'s (T-5.d,
    "Ordering"). Constructing one outside a running loop is safe on 3.12."""


@dataclass
class PaceProgress:
    """What the pacer got done so far, owned by the CALLER so it survives the
    pacer being cancelled or raising: the guarded task reads `delivered[-1]` to
    say where an interrupted replay stopped, and `component` is the ONE object
    a cycle click reads and writes (T-5.a)."""

    delivered: list[int] = field(default_factory=list)
    """`index` of every frame whose component edit succeeded, in order. The
    component's message is the persisted one; its frame is what the
    interrupted panel's "Stopped at" reads."""
    skipped: int = 0
    """Reveals jumped over because the pacer was late (never attempted): frames in
    `fast`, LINES in a reading mode (docs/design/watch-narration-v2.md WN-10.5)."""
    abandoned: int = 0
    """Frames attempted whose component edit failed or timed out (never
    retried as that frame; the next tick renders from the plan again)."""
    scoreboard: BroadcastMessage | None = None
    """The message carrying the live component: `message`, or the new message
    the FINAL panel was posted as when every edit failed."""
    component: ComponentState = field(default_factory=ComponentState)
    """The pacer's and the pager's one shared truth (T-5.a)."""


@dataclass(frozen=True)
class ComponentViewRequest:
    """What a view of the live component's buttons needs: which periods are
    visible, which is selected, and whether the row is disabled (the terminal
    view `_edit_interrupted` builds). The direct analogue of v2's
    `ViewFor(PeriodChunk)` seam (docs/design/watch-v4-live-ticker.md T-6):
    `bot/broadcast.py` still builds no buttons and imports nothing from the
    pager, it only fills and hands out this small value object."""

    periods: tuple[int, ...]
    selected: int
    disabled: bool


ComponentViewFor = Callable[[ComponentViewRequest], "discord.ui.View | None"]
"""Builds the live component's buttons, or `None`. Injected by `/watch`
(`bot/watch_pager.py::build_component_view`) so this module knows nothing of
the pager."""


def _component_view_or_missing(
    component_view_for: ComponentViewFor | None, request: ComponentViewRequest, label: str
) -> discord.ui.View | Any:
    """An absent builder, a raising one, OR one that returns `None` all cost the buttons,
    never the message: every one of the three maps to `discord.utils.MISSING` (OMIT the
    `view=` kwarg), never `None`, which would STRIP an existing row --
    `_bounded_edit`'s convention, T-6. `ComponentViewFor`'s own return type is
    `discord.ui.View | None` (T-2.b: "a `None` from it means omit the `view=` kwarg, not
    `view=None`"), so forwarding a raw `None` here would strip the row every tick it fired."""
    if component_view_for is None:
        return discord.utils.MISSING
    try:
        view = component_view_for(request)
    except Exception:
        log.error("broadcast %s: could not build the component view", label, exc_info=True)
        return discord.utils.MISSING
    return discord.utils.MISSING if view is None else view


async def _bounded_edit(
    message: BroadcastMessage,
    panel: Panel,
    timeout: float,
    *,
    view: discord.ui.View | None = discord.utils.MISSING,
) -> None:
    """`view`'s default is `discord.utils.MISSING`, never `None` (critic 10):
    forwarding `view=None` sets `components: []` and STRIPS whatever row is
    there, while omitting the kwarg (the default) preserves it. An explicit
    `None` STRIPS the row -- used by exactly one caller (T-5.e part 3, the
    best-effort pointer edit on a superseded message). **This is the OPPOSITE
    of `_bounded_send`'s convention below**, deliberately: a send has no
    existing row to preserve, so its `None` means "omit, send no buttons";
    an edit's `None` means "strip". Do not "tidy" one to match the other."""
    extra: dict[str, Any] = {} if view is discord.utils.MISSING else {"view": view}
    await asyncio.wait_for(
        message.edit(
            content=None,
            embed=panel_to_embed(panel),
            allowed_mentions=discord.AllowedMentions.none(),
            **extra,
        ),
        timeout,
    )


async def _bounded_send(
    thread: BroadcastThread,
    panel: Panel,
    timeout: float,
    view: discord.ui.View | None = None,
) -> BroadcastMessage:
    """`view`'s default is `None`, meaning OMIT the key -- a send has no
    existing row to strip, so it needs no third state (T-6, critic2 5; see
    `_bounded_edit`'s opposite convention above)."""
    extra: dict[str, Any] = {} if view is None else {"view": view}
    return await asyncio.wait_for(
        thread.send(
            embed=panel_to_embed(panel),
            allowed_mentions=discord.AllowedMentions.none(),
            **extra,
        ),
        timeout,
    )


async def _deliver_final(
    panel: Panel,
    *,
    view: discord.ui.View | Any,
    message: BroadcastMessage,
    thread: BroadcastThread,
    settings: BroadcastSettings,
    sleep: Callable[[float], Awaitable[None]],
    label: str,
    progress: PaceProgress,
) -> PaceOutcome:
    """P5-97.q item 2: `final_edit_attempts` bounded edits with a short
    backoff, then a new message in the thread (which becomes
    `progress.scoreboard`, so it is the message the click path's gate 1
    compares against, T-5.e). `view` is `_bounded_edit`'s convention (`MISSING`
    omits, a `View` sets, `None` strips) and is translated to `_bounded_send`'s
    (`None` omits) before the new-message post. On that path (`FINISHED_AS_NEW_MESSAGE`)
    the OLD message also gets a best-effort, cosmetic edit to a one-line pointer panel
    with an EXPLICIT `view=None` (T-5.e part 3) -- gate 1 already refuses every click on
    it regardless, so a failure of this edit is logged and swallowed, never propagated,
    and never changes the returned `PaceOutcome`. `except Exception` covers the
    Discord/network failures (`HTTPException`, `TimeoutError`, a dropped
    connection) and is logged with its traceback; it does not catch
    `CancelledError`."""
    state = progress.component
    for attempt in range(settings.final_edit_attempts):
        if attempt:
            await sleep(_FINAL_RETRY_BACKOFF_SECONDS * attempt)
        try:
            await _bounded_edit(message, panel, settings.edit_timeout_seconds, view=view)
        except Exception:
            log.warning(
                "broadcast %s: final frame edit %d/%d failed",
                label,
                attempt + 1,
                settings.final_edit_attempts,
                exc_info=True,
            )
        else:
            state.shown_through = state.line_index  # W3
            return PaceOutcome.FINISHED
    try:
        send_view = None if view is discord.utils.MISSING else view
        posted = await _bounded_send(thread, panel, settings.edit_timeout_seconds, send_view)
    except Exception:
        log.error(
            "broadcast %s: final frame could not be edited in or posted as a new message",
            label,
            exc_info=True,
        )
        state.terminal = True  # W5
        return PaceOutcome.FINAL_LOST
    progress.scoreboard = posted
    state.shown_through = state.line_index  # W4
    state.message_id = posted.id  # W4
    log.warning("broadcast %s: final frame posted as a new message after failed edits", label)
    # T-5.e part 3: best-effort, cosmetic -- an explicit `view=None` STRIPS the old
    # message's now-dead row (`_bounded_edit`'s edit convention, T-6). Gate 1 of T-5.d
    # already refuses every click on this message regardless of whether this edit lands,
    # so a failure here changes nothing about the outcome.
    try:
        pointer = Panel(title=panel.title, description=_SUPERSEDED_POINTER_TEXT)
        await _bounded_edit(message, pointer, settings.edit_timeout_seconds, view=None)
    except Exception:
        log.warning(
            "broadcast %s: could not point the superseded message at the new one",
            label,
            exc_info=True,
        )
    return PaceOutcome.FINISHED_AS_NEW_MESSAGE


async def pace_broadcast(
    plan: BroadcastPlan,
    inputs: BroadcastInputs,
    *,
    message: BroadcastMessage,
    thread: BroadcastThread,
    settings: BroadcastSettings,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    progress: PaceProgress,
    narration: Narration | None = None,
    component_view_for: ComponentViewFor | None = None,
) -> PaceOutcome:
    """Walk `plan` onto the thread on a deadline schedule (P5-97.i): `message`
    is the ONE live component, edited every reveal (docs/design/watch-v4-live-
    ticker.md), carrying the score, the ticker, the stats block for whatever
    period is selected and the cycle row's buttons.

    The reveals are `reveal_schedule(plan, narration)` (docs/design/watch-
    narration-v2.md WN-10.2): in `fast` (`RevealUnit.FRAME`) one per frame, due at
    `started + i * tick_seconds` exactly as before; in a reading mode
    (`RevealUnit.LINE`) one per LINE, each due when the lines before it have had
    their reading time, with PERIOD_END and FINAL frames one reveal each. Each
    iteration sleeps until the current reveal's deadline (never a negative time,
    never accumulated), then reads the clock and, if a later reveal is already due,
    jumps to it -- counting the ones it skipped -- but never past the FINAL frame,
    which is always delivered.

    A reveal is ONE request: the component's `Message.edit`, carrying both the
    embed and the buttons, bounded by `edit_timeout_seconds`.
    `MAX_CONSECUTIVE_EDIT_FAILURES` reveals in a row whose edit failed end the
    broadcast with `PaceOutcome.EDIT_FAILURES`; a success resets the count.
    `progress.delivered` holds the frames whose edit succeeded, once each, in order.

    `narration` is the game rendered once as lines (`plan_narration`) when the
    caller already built it (`/watch` renders once and shares it with the cycle
    click, so both read one truth); `None` renders it here. The ticker and the
    header read it every reveal (docs/design/watch-narration-v2.md WN-9).
    `component_view_for` builds the cycle row's buttons from a
    `ComponentViewRequest` filled from `progress.component` every reveal, so a
    click and a reveal can never disagree about the highlight.

    Raises `ValueError` for a plan with no frames or that does not end in a
    FINAL frame (an empty beat log plans none): there is nothing honest to
    broadcast, and the guarded task turns the raise into the interrupted
    panel. Anything a renderer raises propagates the same way; only the
    Discord requests are caught here."""
    frames = plan.frames
    if not frames or frames[-1].kind is not FrameKind.FINAL:
        raise ValueError(
            f"game {plan.game_id}: a broadcast needs a plan ending in a FINAL frame, "
            f"got {len(frames)} frame(s)"
        )
    label = f"game {plan.game_id}"
    if narration is None:
        narration = plan_narration(
            inputs.result, plan, home=inputs.home, away=inputs.away, names=inputs.names
        )
    schedule = reveal_schedule(plan, narration, tick_seconds=settings.tick_seconds)
    dues = [reveal.due_seconds for reveal in schedule]
    final_step = len(schedule) - 1
    state = progress.component
    progress.scoreboard = message
    state.message_id = message.id  # W0
    started = clock()
    edit_failures = 0  # consecutive reveals whose component edit failed
    step = 0
    while step <= final_step:
        await sleep(max(0.0, started + dues[step] - clock()))
        # The newest reveal already due. `bisect` over the schedule can land ON its
        # last entry, never past it: the FINAL frame is never skipped, however late.
        due_step = bisect.bisect_right(dues, clock() - started) - 1
        if due_step > step:
            progress.skipped += due_step - step
            step = due_step
        reveal = schedule[step]
        frame = frames[reveal.frame_index]
        state.frame_index = reveal.frame_index  # W1
        state.cursor = reveal.cursor  # W1
        state.line_index = completed_through(narration, reveal.cursor)  # W1
        state.more_ticks = step < final_step  # W1
        panel = render_live_component(
            frame,
            plan,
            selected_period=state.selected_period,
            home=inputs.home,
            away=inputs.away,
            names=inputs.names,
            result=inputs.result,
            narration=narration,
            cursor=reveal.cursor,
        )
        request = ComponentViewRequest(
            periods=visible_periods(plan, state.line_index),
            selected=state.selected_period,
            disabled=state.terminal,
        )
        view = _component_view_or_missing(component_view_for, request, label)
        if step == final_step:
            outcome = await _deliver_final(
                panel,
                view=view,
                message=message,
                thread=thread,
                settings=settings,
                sleep=sleep,
                label=label,
                progress=progress,
            )
            if outcome is PaceOutcome.FINISHED:
                progress.delivered.append(reveal.frame_index)
            return outcome
        try:
            await _bounded_edit(message, panel, settings.edit_timeout_seconds, view=view)
        except Exception:
            log.warning(
                "broadcast %s: reveal %d (frame %d) abandoned",
                label,
                step,
                reveal.frame_index,
                exc_info=True,
            )
            progress.abandoned += 1
            edit_failures += 1
            if edit_failures >= MAX_CONSECUTIVE_EDIT_FAILURES:
                log.error(
                    "broadcast %s: %d consecutive component edits failed, ending the broadcast",
                    label,
                    edit_failures,
                )
                return PaceOutcome.EDIT_FAILURES
        else:
            if not progress.delivered or progress.delivered[-1] != reveal.frame_index:
                progress.delivered.append(reveal.frame_index)
            edit_failures = 0
            state.shown_through = state.line_index  # W2
        step += 1
    raise RuntimeError(f"broadcast {label}: the pacer left its loop without delivering FINAL")


class Admission(StrEnum):
    ADMITTED = "admitted"
    ALREADY_LIVE = "already_live"
    REJECTED_FULL = "rejected_full"
    REJECTED_USER_CAP = "rejected_user_cap"


@dataclass(frozen=True)
class BroadcastJob:
    """Everything one broadcast task needs, captured BY VALUE when `/watch`
    spawns it (design B1): the plan, the inputs and the two Discord objects.
    After this the task asks the database only for status writes -- it opens no
    transaction and makes no Discord call inside one (P5-14) -- and asks
    nothing of the block registry, so evicting a block cannot pull data out
    from under it. `message` and `thread` carry the ids `LiveBroadcast` reports."""

    league_id: int
    game_id: int
    user_id: int
    plan: BroadcastPlan
    inputs: BroadcastInputs
    message: BroadcastMessage
    thread: BroadcastThread
    db: Executor
    narration: Narration | None = None
    """The game rendered once as lines when the caller rendered it already
    (`None`: the pacer renders it). `/watch` shares it with the cycle click."""
    component_view_for: ComponentViewFor | None = None
    """Builds the live component's cycle-row buttons (see `ComponentViewFor`);
    `None` posts none."""
    settings: BroadcastSettings | None = None
    """The pacing settings this broadcast was planned with (`settings_for_speed`:
    the viewer's `/watch speed:`); `None` paces at the scheduler's own. Admission is
    always the scheduler's."""


@dataclass
class LiveBroadcast:
    league_id: int
    game_id: int
    user_id: int
    thread_id: int
    message_id: int
    task: asyncio.Task[None]
    progress: PaceProgress = field(default_factory=PaceProgress)
    """The pacer's own record, the very object it mutates: the pager reads what the
    thread really carries from it (watch-v3 W-11). `spawn` builds it."""


class BroadcastEnded(Protocol):
    """The seam commit 9 fills: what happens once a broadcast is over.

    Called after the task's status write, with the status it recorded --
    `FINISHED` or `INTERRUPTED` (an interrupted game is over too, and its
    result is safe to reveal). Also called with `INTERRUPTED`, and nothing
    broadcast, when the task found its row already moved off `queued`: the
    `/watch` that spawned it had put "watching now" on the game's line, and the
    block may have closed since. The implementation reveals the game's result in
    `#games` only if `registry.is_closed(league_id, game_id)` (P5-97.m), and
    otherwise edits the root line back to the matchup line. Kept out of this
    module so the pacer owns no `#games` knowledge and no block registry.
    Never called for a cancelled task (the process is going away and the
    startup sweep reveals what is left), and an exception from it is logged,
    not propagated."""

    async def __call__(self, *, league_id: int, game_id: int, status: BroadcastStatus) -> None: ...


class BroadcastScheduler:
    """Admission control and the task that runs each admitted broadcast
    (P5-97.j). One instance hangs off the bot; single event loop only."""

    def __init__(
        self,
        settings: BroadcastSettings,
        *,
        on_ended: BroadcastEnded,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._on_ended = on_ended
        self._clock = clock  # None means the running loop's `time`, read in the task
        self._sleep = sleep
        self._sem = asyncio.Semaphore(settings.max_concurrent_broadcasts)
        self._depth = 0
        self._admitted: dict[tuple[int, int], int] = {}  # (league_id, game_id) -> user_id
        self._live: dict[tuple[int, int], LiveBroadcast] = {}
        # Finalizers (status write + `on_ended`) that are running. A task leaves
        # `_live` when its finalizer STARTS, so this is the only place `shutdown`
        # can find one that is already under way.
        self._finalizing: set[asyncio.Task[None]] = set()

    @property
    def depth(self) -> int:
        """Admitted, unfinished broadcasts: live plus waiting for a slot."""
        return self._depth

    @property
    def settings(self) -> BroadcastSettings:
        """The settings this scheduler enforces. `/watch` plans a game with
        `settings.plan` and words a refusal with `max_live_broadcasts_per_user`,
        so the cog reads them from here instead of holding a second copy."""
        return self._settings

    def admit(self, *, league_id: int, game_id: int, user_id: int) -> Admission:
        """Reserve a place for one broadcast, or say why not. `ADMITTED` MUST be
        followed by `spawn` with no `await` in between -- the reservation is
        what keeps a second `/watch` of the same game out, and nothing else
        releases it.

        `depth` counts live plus waiting broadcasts, so the bound is
        `max_concurrent_broadcasts + max_queue_depth` (`max_queue_depth` is the
        WAITING part only, design P5-97.j).

        Synchronous on purpose: the depth check and its increment have no
        `await` between them, so on one event loop no other coroutine can
        interleave and two callers cannot both take the last place."""
        key = (league_id, game_id)
        if key in self._admitted:
            return Admission.ALREADY_LIVE
        capacity = self._settings.max_concurrent_broadcasts + self._settings.max_queue_depth
        if self._depth >= capacity:
            return Admission.REJECTED_FULL
        held_by_user = sum(1 for owner in self._admitted.values() if owner == user_id)
        if held_by_user >= self._settings.max_live_broadcasts_per_user:
            return Admission.REJECTED_USER_CAP
        self._admitted[key] = user_id
        self._depth += 1
        return Admission.ADMITTED

    async def spawn(self, *, job: BroadcastJob) -> asyncio.Task[None]:
        """Start the admitted broadcast's task and return at once -- it does
        not wait for a slot (the task's first statement does). `async` to fix
        the caller's contract, though it has no `await`: the reservation
        becoming a registered task is one uninterruptible step."""
        key = (job.league_id, job.game_id)
        if self._admitted.get(key) != job.user_id or key in self._live:
            raise ValueError(f"spawn for game {job.game_id} without a matching admit")
        progress = PaceProgress()
        task = asyncio.create_task(
            self._run_broadcast_guarded(job, progress),
            name=f"broadcast-{job.league_id}-{job.game_id}",
        )
        live = LiveBroadcast(
            league_id=job.league_id,
            game_id=job.game_id,
            user_id=job.user_id,
            thread_id=job.thread.id,
            message_id=job.message.id,
            task=task,
            progress=progress,
        )
        self._live[key] = live
        # The net under the finalizer: a task cancelled BEFORE its first step
        # never enters its `try`, so its `finally` never runs and the game
        # would stay admitted (and counted) forever. A done-callback runs for
        # every task that ends, started or not.
        task.add_done_callback(lambda _task: self._forget_if_current(key, live))
        return task

    def release_unspawned(self, league_id: int, game_id: int) -> bool:
        """Give back an `admit` that will not be followed by `spawn` -- `/watch`
        admits BEFORE it creates the game's thread (so a refusal costs no
        thread), and the thread's creation can fail. `True` when a reservation
        was returned. A game that is not admitted, or was already spawned, is
        left alone and reports `False`: a running broadcast is ended by its own
        finalizer, never from here."""
        key = (league_id, game_id)
        if key not in self._admitted or key in self._live:
            return False
        self._forget(key)
        return True

    def live(self, league_id: int, game_id: int) -> LiveBroadcast | None:
        """The spawned broadcast for this game, live or waiting for a slot."""
        return self._live.get((league_id, game_id))

    def is_live_or_queued(self, league_id: int, game_id: int) -> bool:
        """True from `admit` until the task's finalizer runs. This is what
        `close_block` asks before revealing a game (P5-97.m)."""
        return (league_id, game_id) in self._admitted

    def _forget(self, key: tuple[int, int]) -> None:
        """Drop a game's admission, its depth count and its live entry. Idempotent:
        the count is taken back only if the admission was still there, so the
        finalizer and the done-callback can both call it."""
        if self._admitted.pop(key, None) is not None:
            self._depth -= 1
        self._live.pop(key, None)

    def _forget_if_current(self, key: tuple[int, int], live: LiveBroadcast) -> None:
        """The done-callback: forget the game only if it is still this task's.
        The finalizer normally got there first; the guard keeps a callback that
        fires late from clearing a newer admission of the same game."""
        if self._live.get(key) is live:
            self._forget(key)

    async def shutdown(self) -> None:
        """Cancel every spawned broadcast and wait for each to finish
        unwinding (each edits its viewer's message to the interrupted panel and
        its finalizer writes `interrupted`), then wait -- at most
        `SHUTDOWN_FINALIZER_TIMEOUT_SECONDS` -- for finalizers that were
        already running and so are no longer in the registry. Whatever a
        cancelled or stuck task could not record is the startup sweep's to
        converge."""
        tasks = [live.task for live in self._live.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        running = [fin for fin in self._finalizing if not fin.done()]
        if running:
            _done, stuck = await asyncio.wait(running, timeout=SHUTDOWN_FINALIZER_TIMEOUT_SECONDS)
            if stuck:
                log.warning(
                    "shutdown: %d broadcast finalizer(s) still running after %gs; "
                    "the startup sweep will converge them",
                    len(stuck),
                    SHUTDOWN_FINALIZER_TIMEOUT_SECONDS,
                )

    async def _run_broadcast_guarded(
        self, job: BroadcastJob, progress: PaceProgress | None = None
    ) -> None:
        """The task body (P5-97.q items 1-3). Cancellation and any other failure
        each get one bounded interrupted-panel edit -- a cancellation's is
        shielded -- so a broadcast that dies or is stopped never leaves a
        scoreboard frozen mid-game; cancellation then re-raises, and any other
        failure is logged with its traceback, so a task exception never waits
        for garbage collection to be seen.

        `owned` is the status the row should hold and is `None` when this task
        must not write one; `end` is the status to record.

        The slot is `async with self._sem:` -- the task's first statement, so
        a queued broadcast is just a task waiting here -- and is therefore
        released by leaving that block, on every exit including cancellation
        and before the `finally`. The interrupted-panel edit for a
        cancellation or an exception runs after the slot is given up (one
        bounded edit, not worth holding a slot for); the edit for
        `EDIT_FAILURES` runs INSIDE the slot, still bounded by its own timeout,
        and is made once (`reported`), whatever cancels it. The recap runs after
        the slot is released but still before the `finally`, so the reveal waits
        for it and the next queued broadcast does not.
        The rest of what other callers see (the depth counter, the registry of
        live broadcasts) is released FIRST in the `finally`, in synchronous
        statements that cannot be interrupted; only then does the best-effort,
        shielded write of the row's status happen, as a tracked finalizer
        (`_finalizing`) so `shutdown` can wait for it. Nothing here is
        guaranteed to run -- see the module docstring."""
        key = (job.league_id, job.game_id)
        label = f"game {job.game_id}"
        if progress is None:  # `spawn` passes the one `LiveBroadcast.progress` holds
            progress = PaceProgress()
        owned: BroadcastStatus | None = BroadcastStatus.QUEUED
        end: BroadcastStatus | None = None
        cancelled = False
        reported = False  # the EDIT_FAILURES branch already made its interrupted edit
        try:
            async with self._sem:
                moved = await broadcast_repo.transition_status(
                    job.db,
                    league_id=job.league_id,
                    game_id=job.game_id,
                    expected=BroadcastStatus.QUEUED,
                    new=BroadcastStatus.LIVE,
                )
                if not moved:
                    # Someone else (the sweep, a second writer) moved the row off
                    # `queued`; broadcasting into a row we do not own would only
                    # fight them.
                    log.warning("broadcast %s: row is no longer queued, not broadcasting", label)
                    owned = None
                    end = BroadcastStatus.INTERRUPTED  # told to on_ended below, not written
                    return
                owned = BroadcastStatus.LIVE
                outcome = await pace_broadcast(
                    job.plan,
                    job.inputs,
                    message=job.message,
                    thread=job.thread,
                    settings=job.settings or self._settings,
                    clock=self._clock or asyncio.get_running_loop().time,
                    sleep=self._sleep,
                    progress=progress,
                    narration=job.narration,
                    component_view_for=job.component_view_for,
                )
                if outcome in (PaceOutcome.FINISHED, PaceOutcome.FINISHED_AS_NEW_MESSAGE):
                    end = BroadcastStatus.FINISHED
                else:
                    end = BroadcastStatus.INTERRUPTED
                    if outcome is PaceOutcome.EDIT_FAILURES:
                        reported = True
                        await self._edit_interrupted(job, progress)
        except asyncio.CancelledError:
            cancelled = True
            if end is not BroadcastStatus.FINISHED and not reported:
                # A stop that lands while the recap posts must not paint the
                # interrupted panel over a scoreboard that already says FINAL, and
                # one that lands during the EDIT_FAILURES report has already
                # started that edit: the same as the `BaseException` path below,
                # it is not made a second time.
                await self._edit_interrupted_uncancellable(job, progress)
            raise
        except BaseException as exc:
            log.error("broadcast %s failed", label, exc_info=True)
            end = BroadcastStatus.INTERRUPTED
            try:
                await self._edit_interrupted(job, progress)
            except asyncio.CancelledError:
                cancelled = True  # cancelled while reporting: same as cancelled outright
                raise
            if not isinstance(exc, Exception):
                raise  # SystemExit / KeyboardInterrupt still stop the loop
        finally:
            self._forget(key)
            if owned is not None:
                # A task that never reached an outcome (cancelled, or dying
                # outside the handler) leaves the game `interrupted`.
                recorded = end if end is not None else BroadcastStatus.INTERRUPTED
                await self._finalize(
                    self._record_end(
                        job, expected=owned, new=recorded, notify=not cancelled, label=label
                    )
                )
            elif end is not None and not cancelled:
                # The row was not ours to write (someone moved it off `queued`),
                # but the game's `#games` line still needs its owner's answer.
                await self._finalize(self._notify_ended(job, end, label))
            # watch-v5 WT-7/WT-8: `skipped` is incremented at a late tick's frame jump
            # and read nowhere else in `dba2/` source. At the faster 1.5s tick the
            # combined pacer-plus-click peak can exceed the per-message bucket
            # (WT-2.a), so this is the one number that says whether Discord is
            # keeping up with this broadcast, not just whether it finished.
            # docs/design/watch-narration-v2.md WN-15 R5: `skipped` counts reveals --
            # LINES in a reading mode -- so the log names the unit and the frame the
            # broadcast last landed on.
            log.info(
                "broadcast %s: skipped=%d %s(s) abandoned=%d last_frame=%s",
                label,
                progress.skipped,
                job.plan.settings.reveal.value,
                progress.abandoned,
                progress.delivered[-1] if progress.delivered else None,
            )

    async def _finalize(self, work: Coroutine[Any, Any, None]) -> None:
        """Run a finalizer shielded from cancellation AND tracked, so `shutdown`
        can wait for one that started before it was called."""
        finalizer = asyncio.ensure_future(work)
        self._finalizing.add(finalizer)
        finalizer.add_done_callback(self._finalizing.discard)
        await asyncio.shield(finalizer)

    async def _edit_interrupted_uncancellable(
        self, job: BroadcastJob, progress: PaceProgress
    ) -> None:
        """The cancellation path's one interrupted-panel edit. It is bounded by
        `interrupted_edit_timeout_seconds` (like every interrupted edit) and
        shielded, so a second cancel arriving while it runs neither aborts it
        nor lets it outlive the task unbounded. It never replaces the
        `CancelledError` the caller re-raises."""
        edit = asyncio.ensure_future(self._edit_interrupted(job, progress))
        try:
            await asyncio.shield(edit)
        except asyncio.CancelledError:
            pass  # cancelled again while waiting: the bounded edit finishes on its own

    async def _edit_interrupted(self, job: BroadcastJob, progress: PaceProgress) -> None:
        """One bounded attempt to replace a dying broadcast's message with the
        interrupted panel and a TERMINAL view (cycle buttons disabled, full
        text live) -- W6 (docs/design/watch-v4-live-ticker.md T-5.b, T-5.d
        "Ordering"). `terminal`/`more_ticks` are set SYNCHRONOUSLY, before any
        await, so a click that has not yet acquired `edit_lock` is guaranteed
        to see `terminal` when it does; the lock acquire itself is bounded by
        the same `_CLICK_LOCK_WAIT_SECONDS`-shaped wait the click uses and
        PAINTS ANYWAY on timeout -- this edit must never be delayed
        indefinitely by a viewer. `CancelledError` is not an `Exception` and
        still propagates."""
        state = progress.component
        state.terminal = True  # W6
        state.more_ticks = False  # W6
        try:
            acquired = True
            try:
                await asyncio.wait_for(state.edit_lock.acquire(), _INTERRUPTED_LOCK_WAIT_SECONDS)
            except TimeoutError:
                acquired = False
            try:
                last = job.plan.frames[progress.delivered[-1]] if progress.delivered else None
                result = job.inputs.result
                panel = render_interrupted(
                    last,
                    home=job.inputs.home,
                    away=job.inputs.away,
                    home_score=result.home_score,
                    away_score=result.away_score,
                    period_scores=result.period_scores,
                )
                request = ComponentViewRequest(
                    periods=visible_periods(job.plan, state.line_index),
                    selected=state.selected_period,
                    disabled=True,
                )
                label = f"game {job.game_id}"
                view = _component_view_or_missing(job.component_view_for, request, label)
                await _bounded_edit(
                    job.message,
                    panel,
                    self._settings.interrupted_edit_timeout_seconds,
                    view=view,
                )
            finally:
                if acquired:
                    state.edit_lock.release()
        except Exception:
            log.error(
                "broadcast game %s: could not edit in the interrupted panel",
                job.game_id,
                exc_info=True,
            )

    async def _record_end(
        self,
        job: BroadcastJob,
        *,
        expected: BroadcastStatus,
        new: BroadcastStatus,
        notify: bool,
        label: str,
    ) -> None:
        """Write the row's final status, then hand over to `on_ended`. Runs
        under `asyncio.shield` so a cancellation arriving mid-write does not
        tear it in half. A write that fails or finds the row moved is logged
        and the sweep converges it; `on_ended` still runs, because the reveal
        does not depend on the status (design B1) and its own claim CAS makes a
        second attempt harmless."""
        try:
            changed = await broadcast_repo.transition_status(
                job.db,
                league_id=job.league_id,
                game_id=job.game_id,
                expected=expected,
                new=new,
            )
            if not changed:
                log.warning("broadcast %s: row was not %s when recording %s", label, expected, new)
        except Exception:
            log.error(
                "broadcast %s: could not record %s; the startup sweep will",
                label,
                new,
                exc_info=True,
            )
        if notify:
            await self._notify_ended(job, new, label)

    async def _notify_ended(self, job: BroadcastJob, status: BroadcastStatus, label: str) -> None:
        try:
            await self._on_ended(league_id=job.league_id, game_id=job.game_id, status=status)
        except Exception:
            log.error("broadcast %s: on_ended failed", label, exc_info=True)
