"""The per-viewer, ephemeral pager and the live component's cycle row
(docs/design/watch-v4-live-ticker.md; supersedes the period-message button of
docs/design/watch-v3-spacing-and-pager.md W-10..W-14).

**A Discord message does not scroll, so the scroll is a second message.** The ONE
shared, publicly visible live component in the thread is never edited by a viewer
directly (the pacer re-renders it from the plan every tick, and a cycle click
re-renders it too, T-5.d); the "View full text" button opens an EPHEMERAL message
only the clicker sees, and that message's four buttons page through the selected
period's plays, eight to a page.

**The spoiler cap is what the thread carries, and it outlives the broadcast.** A pager
may show no line AFTER `ComponentState.shown_through` (`live_line_cap`), read from the
game's `PaceProgress`. The entry keeps that very object, so the cap is still exact after
the scheduler has forgotten the game: `FINAL_LOST`, an interrupted edit that failed and a
cancelled broadcast all leave a thread that never got a final score, and reading "no live
broadcast" as "no cap" (the design's first draft) would have shown the rest of the quarter
before `#games` reveals. There is no uncap branch at all: a game whose result is public
is not paged, it takes the degraded path and gets the box score.

**State is in the `custom_id`, the class is registered once.** Every button here is a
`DynamicItem` (the `offer_delivery.OfferActionButton` pattern): `ViewStore` matches them
by `pattern.fullmatch`, builds a fresh instance per click, and nothing about any game
lives in the button. The templates are mutually exclusive (`dispatch_dynamic_items`
runs EVERY matching pattern) and every `from_custom_id` is total, because a raise there is
swallowed with no ack (`discord/ui/view.py:1011-1015`).

**A view here is only a payload.** An ephemeral followup's view gets a 15-minute timeout
(`webhook/async_.py:1876`), and `ViewStore.remove_view` pops a view's dynamic-item patterns
when it ends (`view.py:975-981`) -- which would unregister every class for the whole bot.
It does not: that pop is guarded by `if dispatch_info and snapshot`, and a view made only of
`DynamicItem`s never gets a `dispatch_info` entry (`add_view`, `view.py:964-965`), so a
timeout removes nothing. That is a library fact these views depend on, so a test pins it (a
view that mixed in an ordinary button would lose the guard). `build_component_view` and
`build_pager_view` both construct with `timeout=None` for the same reason a rebuilt-every-tick
view must never start a timeout task of its own.

**The cycle click's callback replies to its own failures** (nothing a dynamic item's
callback raises reaches the human), refuses before it renders anything if the shared
`ComponentState` says the broadcast is terminal, superseded or never painted, clamps its
target period against `visible_periods`, coalesces into the next pacer tick when one is
guaranteed (`ComponentState.more_ticks`) or paints immediately otherwise, and orders its own
edit against `_edit_interrupted`'s through `ComponentState.edit_lock` (T-5.d, "Ordering").
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from dba2.core.logging import get_logger
from dba2.data.db import get_pool
from dba2.data.repositories import broadcast_repo, game_repo
from dba2.domain.game import GameStatus
from dba2.presentation.broadcast import (
    PeriodLines,
    clamp_page,
    cycle_label,
    page_count,
    render_pager_page,
)
from dba2.services import view_service

from . import game_feed
from .broadcast import ComponentViewRequest
from .context import NoLeagueForGuild, resolve_league
from .embeds import panel_to_embed
from .interaction_errors import reply_command_failure

if TYPE_CHECKING:
    from dba2.presentation.panel import Panel

    from .broadcast import ComponentState, PaceProgress

log = get_logger(__name__)

_GONE = (
    "That quarter's play-by-play is gone — a replay only lives in memory while its block "
    "is open. The plays the broadcast posted are still in this thread."
)
_GONE_REVEALED = _GONE + " Here's the final box score."
_OTHER_SERVER = "That button belongs to another server's league."
_NO_SUCH_PERIOD = "That quarter isn't part of game {game_id}."
_NO_SUCH_GAME = "No game {game_id} in this league."
_NOT_FINISHED = "Game {game_id} isn't finished yet, so there is nothing to browse."
# The sentence `/watch` gives a game with no `game_broadcasts` row (cogs/broadcast.py).
_NOT_IN_A_BLOCK = (
    "That game isn't watchable — it isn't part of a block of games with a replay, and a "
    "replay only lives in memory while its block is open."
)
_CLOSED = "Closed."

# The cycle click's own refusals (watch-v4 T-5.d, T-7).
_NO_ENTRY = _GONE
_SUPERSEDED = "This broadcast finished in a newer message below."
_NOTHING_YET = "Nothing has been broadcast yet."
_ENDED = "This broadcast has ended."

_PAGE_TEMPLATE = (
    r"dba2:watch:page:(?P<league_id>\d+):(?P<game_id>\d+):(?P<period>\d+)"
    r":(?P<page>\d+):(?P<action>older|newer|latest|close)"
)
_CYCLE_TEMPLATE = r"dba2:watch:cycle:(?P<league_id>\d+):(?P<game_id>\d+):(?P<period>\d+)"
_FULL_TEMPLATE = r"dba2:watch:full:(?P<league_id>\d+):(?P<game_id>\d+)"

ACTIONS = ("older", "newer", "latest", "close")
_ACTION_LOOK = {
    "older": ("◀ Older", discord.ButtonStyle.secondary),
    "newer": ("▶ Newer", discord.ButtonStyle.secondary),
    "latest": ("⟳ Latest", discord.ButtonStyle.primary),
    "close": ("✖ Close", discord.ButtonStyle.secondary),
}

# T-2.b: the cycle row is pinned to rows 0-3 (20 slots), the full-text button to row 4.
_FULL_TEXT_ROW = 4
_MAX_CYCLE_PERIODS = 19  # plus `Full` itself: 20 buttons across rows 0-3
_RECENT_OT_KEEP = 15
_REGULATION_PERIODS = 4  # mirrors presentation/broadcast.py -- the OT ceiling's own cutoff

# T-5.d step 6 / T-5.d "Ordering": how long a cycle click waits for `edit_lock` before
# giving up and deferring with nothing written. `_edit_interrupted` waits the same span
# (`bot/broadcast.py::_INTERRUPTED_LOCK_WAIT_SECONDS`) for the opposite reason.
_CLICK_LOCK_WAIT_SECONDS = 1.0

# watch-v5 WT-2.a: the click-coalescing cooldown used to just track `tick_seconds`
# (equal to it by definition, T-4.a.2). A 1.5s tick would shrink it to 1.5s too --
# double the click-edit rate on the single tightest resource -- so the cooldown is
# floored at today's tick instead, keeping click behaviour exactly where v4 analysed
# it: at most 1 click-driven edit per floored window, click behaviour unchanged.
_CLICK_COOLDOWN_FLOOR_SECONDS = 4.0


def page_custom_id(league_id: int, game_id: int, period: int, page: int, action: str) -> str:
    return f"dba2:watch:page:{league_id}:{game_id}:{period}:{page}:{action}"


def cycle_custom_id(league_id: int, game_id: int, period: int) -> str:
    return f"dba2:watch:cycle:{league_id}:{game_id}:{period}"


def full_custom_id(league_id: int, game_id: int) -> str:
    return f"dba2:watch:full:{league_id}:{game_id}"


# --- what a pager may show -----------------------------------------------------------


@dataclass(frozen=True)
class PagerPeriod:
    label: str
    first_index: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class PagerSource:
    """The plan's rendered lines regrouped by period, plus a synthetic period 0
    (`"Full Game"`, every line in order) that lets the full-text button reuse this
    machinery for the Full Game selection too (watch-v4 T-5.f). Read-only and
    shared by every viewer."""

    matchup: str
    periods: Mapping[int, PagerPeriod]

    @classmethod
    def from_periods(cls, matchup: str, lines: Sequence[PeriodLines]) -> PagerSource:
        periods: dict[int, PagerPeriod] = {
            pl.period: PagerPeriod(label=pl.label, first_index=pl.first_index, lines=pl.lines)
            for pl in lines
        }
        all_lines = tuple(line for pl in lines for line in pl.lines)
        periods[0] = PagerPeriod(label="Full Game", first_index=0, lines=all_lines)
        return cls(matchup, periods)


def live_line_cap(progress: PaceProgress) -> int:
    """Index of the newest possession the live component has shown, `-1` if
    none: `ComponentState.shown_through`, advanced only after a render Discord
    ACCEPTED (watch-v4 T-5.d). No longer needs `PagerSource`: the cap is the
    same one number for every period, not a per-chunk one."""
    return progress.component.shown_through


@dataclass
class PagerEntry:
    """One watched game. `progress` is the pacer's own `PaceProgress` (not a copy: a
    copy would freeze the cap at -1), kept after the broadcast is over so the cap survives
    `FINAL_LOST`, a failed interrupted edit and a cancellation.

    `component` and `visible` are closures `cogs/broadcast.py::_start` builds over the
    same plan, teams, names, result and narration the broadcast job got (watch-v4
    T-5.d): `component(frame_index, cursor, selected_period)` renders the SAME
    `render_live_component` the pacer uses, at the same narration line
    (docs/design/watch-narration-v2.md WN-9.1), and `visible(last_line)` is
    `visible_periods(plan, last_line)`. The cycle click renders through these; it never
    holds the plan itself."""

    guild_id: int
    source: PagerSource
    progress: PaceProgress
    component: Callable[[int, int, int], Panel]
    visible: Callable[[int], tuple[int, ...]]

    def available_lines(self, period: int) -> tuple[str, ...] | None:
        """The period's lines the thread has shown, or `None` for a period the game never
        had. Never a line after `live_line_cap`."""
        entry = self.source.periods.get(period)
        if entry is None:
            return None
        cap = live_line_cap(self.progress)
        return entry.lines[: max(0, min(len(entry.lines), cap - entry.first_index + 1))]


def game_is_open(bot: Any, league_id: int, game_id: int) -> bool:
    """A pager answers only while the game's block is open OR its broadcast is live or
    queued. The second half is not optional: `close_block` leaves a still-broadcasting
    game alone, so a commissioner `/advance` mid-watch closes the block while plays are
    still arriving."""
    return bot.blocks.get(league_id, game_id) is not None or bot.broadcasts.is_live_or_queued(
        league_id, game_id
    )


class PagerRegistry:
    """`bot.pagers`, per bot like `bot.blocks`. Synchronous, single event loop.

    Bounded by pruning on `register`: a game that is neither in an open block nor being
    broadcast is dropped. Correctness never depends on the prune (every click re-derives
    availability with `game_is_open`), so no hook in `game_feed` is needed."""

    def __init__(self) -> None:
        self._entries: dict[tuple[int, int], PagerEntry] = {}

    def register(
        self,
        league_id: int,
        game_id: int,
        entry: PagerEntry,
        *,
        keep: Callable[[int, int], bool],
    ) -> None:
        """Hold `entry` for the game (replacing an older one) and drop every OTHER entry
        whose game `keep` says is finished with."""
        key = (league_id, game_id)
        for other in [k for k in self._entries if k != key and not keep(*k)]:
            del self._entries[other]
        self._entries[key] = entry

    def get(self, league_id: int, game_id: int) -> PagerEntry | None:
        return self._entries.get((league_id, game_id))

    def __len__(self) -> int:
        return len(self._entries)


# --- the cycle row's view builder (watch-v4 T-2.a, T-2.b, T-6) ------------------------


def _capped_periods(periods: tuple[int, ...]) -> tuple[int, ...]:
    """`Full` plus at most 19 periods (T-2.b): beyond that, keep `Q1..Q4` and the 15
    most recent overtimes, dropping the earlier ones (still reachable in the line
    score, not in the cycle row)."""
    if len(periods) <= _MAX_CYCLE_PERIODS:
        return periods
    regulation = tuple(p for p in periods if p <= _REGULATION_PERIODS)
    overtimes = tuple(p for p in periods if p > _REGULATION_PERIODS)
    keep = max(0, _MAX_CYCLE_PERIODS - len(regulation))
    return regulation + (overtimes[-keep:] if keep else ())


def build_component_view(
    league_id: int, game_id: int, request: ComponentViewRequest
) -> discord.ui.View:
    """The ONE builder of the live component's buttons (watch-v4 T-6): the pacer
    reaches it through `component_view_for` and the cycle click calls it directly, so
    neither caller decides the highlight, the row placement or the OT cap. `timeout=None`
    (critic 14): this view is rebuilt every tick for the whole broadcast, and again on
    every click, so `discord.py`'s 180s default would start a throwaway timeout task
    every time."""
    view = discord.ui.View(timeout=None)
    for period in (0, *_capped_periods(request.periods)):
        primary = period == request.selected
        style = discord.ButtonStyle.primary if primary else discord.ButtonStyle.secondary
        view.add_item(
            WatchCycleButton(league_id, game_id, period, style=style, disabled=request.disabled)
        )
    view.add_item(WatchFullTextButton(league_id, game_id))
    return view


def _clamp_period(period: int, visible: tuple[int, ...]) -> int:
    """T-2.a's clamp rule: `period` if it is `0` or visible, else the largest visible
    period below it, else `0`. Never raises."""
    if period == 0 or period in visible:
        return period
    lower = [p for p in visible if p < period]
    return max(lower) if lower else 0


# --- the buttons ----------------------------------------------------------------------


class WatchCycleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_CYCLE_TEMPLATE,
):
    """One button of the cycle row: `Full`, `Q1`, `Q2`, ... `period` is the button's
    OWN target (a fixed label), not the current selection -- the click clamps it against
    `visible_periods` (watch-v4 T-2.a, T-5.d)."""

    def __init__(
        self,
        league_id: int,
        game_id: int,
        period: int,
        *,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=cycle_label(period),
                style=style,
                custom_id=cycle_custom_id(league_id, game_id, period),
                disabled=disabled,
            )
        )
        self.league_id, self.game_id, self.period = league_id, game_id, period

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
        /,
    ) -> WatchCycleButton:
        return cls(int(match["league_id"]), int(match["game_id"]), int(match["period"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await _handle_cycle_click(interaction, self.league_id, self.game_id, self.period)
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label=_label("cycle", self))


async def _handle_cycle_click(
    interaction: discord.Interaction, league_id: int, game_id: int, period: int
) -> None:
    """The click's full path (watch-v4 T-5.d, steps 1-14)."""
    bot = interaction.client
    entry: PagerEntry | None = bot.pagers.get(league_id, game_id)
    if entry is None:
        # Step 1: no entry -- the v3 "replay is gone" ephemeral family; nothing edited.
        await interaction.response.send_message(_NO_ENTRY, ephemeral=True)
        return
    if interaction.guild_id is None or interaction.guild_id != entry.guild_id:
        await interaction.response.send_message(_OTHER_SERVER, ephemeral=True)
        return

    # Step 2: one read of the shared state. Everything below reads this one object.
    state: ComponentState = entry.progress.component

    # Step 3: the refusal gates, all synchronous. `interaction.message` is `None`
    # handled explicitly, never silently skipped (critic round 1, MINOR 10): a click
    # this handler cannot identify a message for is refused the same as a stale one,
    # rather than falling through as if it had matched `state.message_id`.
    message = interaction.message
    if message is None or message.id != state.message_id:
        await interaction.response.send_message(_SUPERSEDED, ephemeral=True)
        return
    if state.frame_index < 0:
        await interaction.response.send_message(_NOTHING_YET, ephemeral=True)
        return
    if state.terminal:
        await interaction.response.send_message(_ENDED, ephemeral=True)
        return

    # Step 4: resolve and clamp, once.
    clamped = _clamp_period(period, entry.visible(state.line_index))

    # Step 5: coalesce or paint (T-4.a; watch-v5 WT-2.a floors this at
    # `_CLICK_COOLDOWN_FLOOR_SECONDS` so a faster tick cannot shrink the click cooldown
    # below what v4 analysed).
    tick_seconds = max(bot.broadcasts.settings.tick_seconds, _CLICK_COOLDOWN_FLOOR_SECONDS)
    now = time.monotonic()
    if state.more_ticks and (now - state.last_edit_monotonic) < tick_seconds:
        state.selected_period = clamped  # W8
        await interaction.response.defer()
        return

    # Step 6: bounded lock acquire.
    try:
        await asyncio.wait_for(state.edit_lock.acquire(), _CLICK_LOCK_WAIT_SECONDS)
    except TimeoutError:
        await interaction.response.defer()
        return
    try:
        # Step 7: re-check terminal UNDER the lock (the M3 fix, T-5.d "Ordering").
        if state.terminal:
            await interaction.response.send_message(_ENDED, ephemeral=True)
            return
        # Step 8: write the selection, inside the lock.
        state.selected_period = clamped  # W8
        # Snapshot `line_index` HERE, before step 9 renders and step 12 awaits: the pacer
        # takes no lock, so W1 can advance `state.line_index` while this click's edit is in
        # flight. Every use below the render must read this snapshot, never `state.line_index`
        # again -- a fresh read at W7 (step 13) would cap `shown_through` at whatever the
        # pacer had reached by the time Discord answered, not what this click painted, and
        # let the full-text pager reveal possessions this render never showed.
        painted_line_index = state.line_index
        # Step 9: build the panel, same frame AND narration line the pacer is painting or
        # has painted (the two are written together at W1, before any await).
        panel = entry.component(state.frame_index, state.cursor, state.selected_period)
        # Step 10: build a fresh view from the same inputs the pacer's own tick uses.
        view: discord.ui.View | Any = discord.utils.MISSING
        try:
            request = ComponentViewRequest(
                periods=entry.visible(painted_line_index),
                selected=state.selected_period,
                disabled=state.terminal,
            )
            view = build_component_view(league_id, game_id, request)
        except Exception:
            # Step 11: keep the panel, drop the view -- omit `view=`, never `view=None`.
            log.error(
                "watch cycle game %s: could not build the view; editing the panel bare",
                game_id,
                exc_info=True,
            )
        # Step 12: one edit, both halves.
        await interaction.response.edit_message(
            content=None,
            embed=panel_to_embed(panel),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # Step 13: W7, still under the lock -- against the SNAPSHOT, never a fresh
        # `state.line_index` (the spoiler-cap leak: see the comment above).
        state.shown_through = max(state.shown_through, painted_line_index)
        state.last_edit_monotonic = time.monotonic()
    finally:
        state.edit_lock.release()


class WatchFullTextButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_FULL_TEMPLATE,
):
    """`📄 Full text`, always the bottom row (T-2.b). Scoped to whatever period is
    CURRENTLY selected (`state.selected_period`), never to a period baked into the
    `custom_id` -- so a held-open message answers with the component's live selection,
    not the one it had when the message was rendered (watch-v4 T-5.f)."""

    def __init__(self, league_id: int, game_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="📄 Full text",
                style=discord.ButtonStyle.secondary,
                custom_id=full_custom_id(league_id, game_id),
                row=_FULL_TEXT_ROW,
            )
        )
        self.league_id, self.game_id = league_id, game_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
        /,
    ) -> WatchFullTextButton:
        return cls(int(match["league_id"]), int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            entry: PagerEntry | None = interaction.client.pagers.get(self.league_id, self.game_id)
            period = entry.progress.component.selected_period if entry is not None else 0
            answer = _answer_from_entry(
                interaction, self.league_id, self.game_id, period, action="latest", page=1
            )
            if answer is None:
                answer = await _gone_answer(interaction, self.league_id, self.game_id)
            await interaction.followup.send(**answer.send_kwargs(), ephemeral=True)
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label=_label("full", self))


class WatchPageButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_PAGE_TEMPLATE,
):
    """One of the pager's Older / Newer / Latest / Close buttons. `page` is the page the
    message shows now; the click computes the target and clamps it."""

    def __init__(
        self,
        league_id: int,
        game_id: int,
        period: int,
        page: int,
        action: str,
        *,
        disabled: bool = False,
    ) -> None:
        if action not in _ACTION_LOOK:
            raise ValueError(f"unknown pager action {action!r}")
        label, style = _ACTION_LOOK[action]
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                custom_id=page_custom_id(league_id, game_id, period, page, action),
                disabled=disabled,
            )
        )
        self.league_id, self.game_id, self.period = league_id, game_id, period
        self.page, self.action = page, action

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
        /,
    ) -> WatchPageButton:
        return cls(
            int(match["league_id"]),
            int(match["game_id"]),
            int(match["period"]),
            int(match["page"]),
            match["action"],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if self.action == "close":
                await interaction.response.edit_message(content=_CLOSED, embed=None, view=None)
                return
            answer = _answer_from_entry(
                interaction,
                self.league_id,
                self.game_id,
                self.period,
                action=self.action,
                page=self.page,
            )
            if answer is not None:
                # One request, nothing awaited before it: the lines are in memory.
                await interaction.response.edit_message(**answer.edit_kwargs())
                return
            await interaction.response.defer()  # DEFERRED_MESSAGE_UPDATE: edit in place later
            answer = await _gone_answer(interaction, self.league_id, self.game_id)
            await interaction.edit_original_response(**answer.edit_kwargs())
        except Exception as exc:
            await reply_command_failure(interaction, exc, log_label=_label("page", self))


def _label(kind: str, button: Any) -> str:
    return f"watch pager {kind} game {button.game_id}"


# --- answers -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Answer:
    """What one click says: a page (embed + view), or a plain sentence (which may carry
    the revealed box score's embed)."""

    content: str | None = None
    embed: discord.Embed | None = None
    view: discord.ui.View | None = None

    def send_kwargs(self) -> dict[str, Any]:
        """`followup.send` wants a key ABSENT rather than `None` for a view."""
        out: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions.none()}
        if self.content is not None:
            out["content"] = self.content
        if self.embed is not None:
            out["embed"] = self.embed
        if self.view is not None:
            out["view"] = self.view
        return out

    def edit_kwargs(self) -> dict[str, Any]:
        """An edit REPLACES the message: an absent embed or view must be cleared."""
        return {"content": self.content, "embed": self.embed, "view": self.view}


def _target_page(action: str, page: int, pages: int) -> int:
    """`pages` is counted from the cap as it is NOW, so Newer and Latest both see plays that
    arrived after the viewer's message was rendered."""
    if action == "older":
        return page - 1
    if action == "newer":
        return page + 1
    return pages  # latest (and the open click): the newest page


def _answer_from_entry(
    interaction: discord.Interaction,
    league_id: int,
    game_id: int,
    period: int,
    *,
    action: str,
    page: int,
) -> _Answer | None:
    """Synchronous, so a navigation click can answer with one request. `None` means this
    process cannot serve the game (no entry, or its block closed and nobody is
    broadcasting it): the caller takes the degraded path, which reads the database."""
    bot = interaction.client
    entry: PagerEntry | None = bot.pagers.get(league_id, game_id)
    if entry is None:
        return None
    # The guild check comes BEFORE `game_is_open`: otherwise a click from another server
    # could tell "this game is being watched" (pages or an error) from "the replay is gone".
    if interaction.guild_id is None or interaction.guild_id != entry.guild_id:
        return _Answer(content=_OTHER_SERVER)
    if not game_is_open(bot, league_id, game_id):
        return None
    lines = entry.available_lines(period)
    if lines is None:
        return _Answer(content=_NO_SUCH_PERIOD.format(game_id=game_id))
    pages = page_count(len(lines))
    shown = clamp_page(_target_page(action, page, pages), len(lines))
    panel = render_pager_page(
        label=entry.source.periods[period].label,
        matchup=entry.source.matchup,
        lines=lines,
        page=shown,
    )
    return _Answer(
        embed=panel_to_embed(panel),
        view=build_pager_view(league_id, game_id, period, shown, pages),
    )


async def _gone_answer(interaction: discord.Interaction, league_id: int, game_id: int) -> _Answer:
    """The degraded path: the game has no pager here. Says so, and adds the box score only
    when `game_feed.box_score_is_public` -- the one definition of "revealed" -- and only
    after the league (from the interaction's guild) and the game (final, in that league)
    have been checked, because `box_score_is_public(None)` is True for a game in no
    block and a forged id must not read another league's box score. A game with no
    broadcast row gets `/watch`'s "not watchable" sentence and no box score at all."""
    pool = await get_pool()
    try:
        league = await resolve_league(pool, interaction)
    except NoLeagueForGuild:
        return _Answer(content=_OTHER_SERVER)
    if league.id != league_id:
        return _Answer(content=_OTHER_SERVER)
    game = await game_repo.get_game(pool, game_id)
    if game is None or game.league_id != league.id:
        return _Answer(content=_NO_SUCH_GAME.format(game_id=game_id))
    if game.status is not GameStatus.FINAL:
        return _Answer(content=_NOT_FINISHED.format(game_id=game_id))
    row = await broadcast_repo.get_broadcast(pool, league_id=league_id, game_id=game_id)
    if row is None:
        # `box_score_is_public(None)` is True ("in no block, nothing holds the score
        # back"), but a pager click only ever comes from a broadcast's own button, so a
        # game with no row here is a forged id or a row that vanished; `/watch` answers it
        # the same way and never reads a box score for it.
        return _Answer(content=_NOT_IN_A_BLOCK)
    if game_feed.box_score_is_public(row):
        panel = await view_service.get_boxscore_panel(pool, game_id=game_id)
        return _Answer(content=_GONE_REVEALED, embed=panel_to_embed(panel))
    return _Answer(content=_GONE)


# --- views ----------------------------------------------------------------------------------


def build_pager_view(
    league_id: int, game_id: int, period: int, page: int, pages: int
) -> discord.ui.View:
    """The pager's row for `page` of `pages`. Older is disabled on page 1 and Newer on the
    last page (cosmetic, since a stale click clamps). **Latest is never disabled**: a
    quarter browsed while it is live keeps growing, and Latest is the one button that
    re-reads the thread's position (`live_line_cap`) and re-renders even when the viewer is
    already on the newest page. Disabling it there froze a viewer at the plays that existed
    when they clicked (with 8 plays or fewer, every button but Close was dead)."""
    view = discord.ui.View(timeout=None)
    for action in ACTIONS:
        disabled = (action == "older" and page <= 1) or (action == "newer" and page >= pages)
        view.add_item(
            WatchPageButton(league_id, game_id, period, page, action, disabled=disabled)
        )
    return view
