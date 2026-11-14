"""Fakes for the `/watch` pager's and live component's tests (`test_watch_pager.py`):
an interaction that records every response, followup and edit IN ORDER (so "deferred
first" and "one request, nothing before it" are checkable), a private mutated copy of
`bot/watch_pager.py` for the mutation twins, and one-call ways to build a `PagerEntry`
(with its `component`/`visible` closures, watch-v4 T-5.d) over a real planned game.

No DB, no gateway: `discord.Embed` and `discord.ui.View` are plain local data.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dba2.bot import watch_pager
from dba2.bot.broadcast import ComponentState, PaceProgress
from dba2.bot.watch_pager import PagerEntry, PagerSource
from dba2.presentation.broadcast import (
    pager_matchup,
    plan_narration,
    plan_period_lines,
    render_live_component,
    visible_periods,
)
from tests.unit.bot._broadcast_fakes import AWAY, GAME_ID, HOME, plan_for, sim_result
from tests.unit.bot._game_feed_fakes import LEAGUE, FakeBot
from tests.unit.presentation._broadcast_helpers import names_for

GUILD = 4242
GAME = GAME_ID  # 412
OTHER_GUILD = 777
MESSAGE_ID = 555


class MutationAnchorError(Exception):
    """Not an `AssertionError`: a twin expects the SCENARIO to fail, and a stale anchor
    must not be able to pass for that."""


class PagerResponse:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self._done = False
        self.deferred: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.messages: list[Any] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self._done = True
        self.deferred.append({"ephemeral": ephemeral, "thinking": thinking})
        self._order.append("defer")

    async def edit_message(self, **kwargs: Any) -> None:
        self._done = True
        self.edits.append(kwargs)
        self._order.append("edit_message")

    async def send_message(self, content=None, **kwargs: Any) -> None:
        self._done = True
        self.messages.append((content, kwargs))
        self._order.append("send_message")


class PagerFollowup:
    def __init__(self, order: list[str], *, failures: int = 0) -> None:
        self._order = order
        self._failures = failures
        self.sent: list[dict[str, Any]] = []
        self.attempts = 0

    async def send(self, content=None, **kwargs: Any) -> None:
        """The first `failures` calls raise (a 404 unknown webhook, an HTTPException, a
        too-large embed) and record nothing, as Discord would have shown nothing."""
        self.attempts += 1
        if self.attempts <= self._failures:
            raise RuntimeError("followup.send failed")
        self.sent.append({"content": content, **kwargs})
        self._order.append("followup.send")


class PagerInteraction:
    """A component interaction on the live component message (or an ephemeral one)."""

    def __init__(
        self,
        bot: Any,
        *,
        guild_id: int | None = GUILD,
        user_id: int = 10,
        followup_failures: int = 0,
        message_id: int | None = MESSAGE_ID,
    ) -> None:
        self.order: list[str] = []
        self.id = 99
        self.client = bot
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(id=message_id) if message_id is not None else None
        self.response = PagerResponse(self.order)
        self.followup = PagerFollowup(self.order, failures=followup_failures)
        self.original_edits: list[dict[str, Any]] = []

    async def edit_original_response(self, **kwargs: Any) -> None:
        self.original_edits.append(kwargs)
        self.order.append("edit_original_response")

    # -- reading what the viewer would see -------------------------------------------

    def last(self) -> dict[str, Any]:
        """The one message-shaped thing this interaction produced, whichever route it took."""
        for group in (self.response.edits, self.original_edits, self.followup.sent):
            if group:
                assert len(group) == 1, group
                return group[0]
        raise AssertionError(f"nothing was sent: order={self.order}")

    def text(self) -> str | None:
        return self.last().get("content")

    def embed(self):
        return self.last().get("embed")


def full_source(possessions_per_frame: int = 2):
    """A `PagerSource` over `_broadcast_fakes.sim_result()`'s real overtime
    game (Q1-Q4 and OT1 -- see that function's own docstring for which seed,
    which has shifted more than once) and the plan it was built from."""
    plan = plan_for(possessions_per_frame)
    result = sim_result()
    lines = plan_period_lines(plan, home=HOME, away=AWAY, names=names_for(result))
    return PagerSource.from_periods(pager_matchup(HOME, AWAY), lines), plan


def component_closures(plan):
    """The `(component, visible)` closures `cogs/broadcast.py::_start` builds over a
    plan (watch-v4 T-5.d): one renderer at a frame AND a narration cursor
    (docs/design/watch-narration-v2.md WN-9.1), the game's own narration, and
    `visible_periods` bound to the same plan."""
    result = sim_result()
    names = names_for(result)
    narration = plan_narration(result, plan, home=HOME, away=AWAY, names=names)

    def component(frame_index: int, cursor: int, selected_period: int):
        return render_live_component(
            plan.frames[frame_index],
            plan,
            selected_period=selected_period,
            home=HOME,
            away=AWAY,
            names=names,
            result=result,
            narration=narration,
            cursor=cursor,
        )

    def visible(last_line: int) -> tuple[int, ...]:
        return visible_periods(plan, last_line)

    return component, visible


def progress_showing(
    *,
    shown_through: int,
    frame_index: int = 0,
    line_index: int | None = None,
    selected_period: int = 0,
    terminal: bool = False,
    more_ticks: bool = False,
    last_edit_monotonic: float = 0.0,
    message_id: int | None = MESSAGE_ID,
) -> PaceProgress:
    """A `PaceProgress` whose `ComponentState` reads as if the pacer (or a click) had
    just painted `frame_index` with the spoiler cap at `shown_through`."""
    progress = PaceProgress()
    state: ComponentState = progress.component
    state.shown_through = shown_through
    state.frame_index = frame_index
    state.line_index = line_index if line_index is not None else shown_through
    state.selected_period = selected_period
    state.terminal = terminal
    state.more_ticks = more_ticks
    state.last_edit_monotonic = last_edit_monotonic
    state.message_id = message_id
    return progress


def entry_showing(shown_through: int, *, guild_id: int = GUILD, **state_kwargs) -> PagerEntry:
    source, plan = full_source()
    component, visible = component_closures(plan)
    progress = progress_showing(shown_through=shown_through, **state_kwargs)
    return PagerEntry(
        guild_id=guild_id, source=source, progress=progress, component=component, visible=visible
    )


def bot_with(entry: PagerEntry | None, *, in_block: bool = True, live: bool = False) -> FakeBot:
    """A bot holding `entry` for game 412; its block open (or not), its broadcast live (or not)."""
    bot = FakeBot()
    if in_block:
        bot.blocks.register_block(
            league_id=LEAGUE,
            block_id="2026-11-01",
            season=2026,
            games=[_block_game()],
        )
    if live:
        bot.broadcasts.live.add((LEAGUE, GAME))
    if entry is not None:
        bot.pagers.register(LEAGUE, GAME, entry, keep=lambda _l, _g: True)
    return bot


def _block_game():
    from tests.unit.bot._game_feed_fakes import block_game

    return block_game(GAME)


def pager_variant(*mutations: tuple[str, str]) -> types.ModuleType:
    """A private copy of `dba2/bot/watch_pager.py` with each `(old, new)` mutation applied,
    loaded under a scratch name. Every anchor must occur exactly once; with none it is the
    control, which every twin's scenario must PASS on."""
    source = Path(watch_pager.__file__).read_text(encoding="utf-8")
    for old, new in mutations:
        if source.count(old) != 1:
            raise MutationAnchorError(f"mutation anchor {old!r} occurs {source.count(old)}x")
        source = source.replace(old, new)
    module = types.ModuleType("_bot_watch_pager_variant")
    module.__package__ = "dba2.bot"  # its `from . import game_feed`
    sys.modules[module.__name__] = module  # `@dataclass` resolves annotations through it
    try:
        exec(compile(source, "<watch pager variant>", "exec"), module.__dict__)
    finally:
        del sys.modules[module.__name__]
    return module
