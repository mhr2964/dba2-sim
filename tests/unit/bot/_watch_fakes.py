"""Shared fakes for `/watch` (`dba2/bot/cogs/broadcast.py`): an interaction that
records every reply, a `#games` channel whose messages can start a thread, a
thread that records what is sent to it, and a `World` that wires them to the
in-memory repository `Store`, a real `BlockRegistry` and a REAL
`BroadcastScheduler` on an injected clock.

No DB, no gateway. The `Store` is a model of `broadcast_repo` (its CAS
semantics are proven against Postgres by `tests/integration/test_broadcast_repo.py`),
so a test here proves the cog's decisions and Discord call shapes, not SQL.
"""

from __future__ import annotations

import asyncio
import functools
import sys
import types
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
from discord import app_commands

from dba2.bot import game_feed
from dba2.bot.broadcast import BroadcastScheduler, BroadcastSettings
from dba2.bot.cogs import broadcast as cog_module
from dba2.domain.phase import Phase
from dba2.presentation.games import SlateGame, build_matchup_line
from dba2.presentation.panel import Panel, PanelField
from dba2.services import view_service
from tests.unit.bot._broadcast_fakes import FakeClock, FakeMessage, sim_result
from tests.unit.bot._game_feed_fakes import (
    AWAY_TEAM,
    HOME_TEAM,
    LEAGUE,
    FakeBot,
    FakeChannel,
    FakePartialMessage,
    Store,
    block_game,
    not_found,
)
from tests.unit.presentation._broadcast_helpers import names_for

GUILD_ID = 4242
GID = 412
ROOT_BASE = 3000
THREAD_BASE = 7000
THREAD_MESSAGE_BASE = 8000
BOX_PANEL = Panel(
    title="Box score", fields=(PanelField(name="Final", value="a stand-in for the box score"),)
)


class MutationAnchorError(Exception):
    """Not an `AssertionError`: a twin expects the SCENARIO to fail, and a stale
    anchor must not be able to pass for that."""


class _Response:
    def __init__(self) -> None:
        self._done = False
        self.deferred: list[bool] = []
        self.messages: list[Any] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._done = True
        self.deferred.append(ephemeral)

    async def send_message(self, content=None, **kwargs: Any) -> None:
        self._done = True
        self.messages.append((content, kwargs))


class _Followup:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, content=None, *, embed=None, ephemeral=False, **kwargs: Any) -> None:
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral, **kwargs})


class FakeInteraction:
    def __init__(self, user_id: int = 10, guild_id: int | None = GUILD_ID) -> None:
        self.id = 1
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id)
        self.response = _Response()
        self.followup = _Followup()

    @property
    def replies(self) -> list[dict[str, Any]]:
        return self.followup.sent

    @property
    def reply(self) -> dict[str, Any]:
        assert len(self.replies) == 1 and not self.response.messages, (
            self.replies,
            self.response.messages,
        )
        return self.replies[0]

    @property
    def text(self) -> str:
        return self.reply["content"]

    @property
    def embed(self) -> Any:
        return self.reply["embed"]


class WatchThread:
    """A `discord.Thread`: `send` returns the message the broadcast will edit
    (recorded in `messages`), `delete` removes it."""

    def __init__(
        self,
        thread_id: int,
        clock: FakeClock,
        *,
        send_error: BaseException | None = None,
        delete_error: BaseException | None = None,
    ) -> None:
        self.id = thread_id
        self.jump_url = f"https://discord.com/channels/{GUILD_ID}/{thread_id}"
        self.clock = clock
        self.send_error = send_error
        self.delete_error = delete_error
        self.sent: list[dict[str, Any]] = []
        self.messages: list[FakeMessage] = []
        self.deleted = False

    async def send(self, **kwargs: Any) -> FakeMessage:
        self.sent.append(kwargs)
        if self.send_error is not None:
            raise self.send_error
        message = FakeMessage(self.clock, message_id=THREAD_MESSAGE_BASE + len(self.messages))
        self.messages.append(message)
        return message

    async def delete(self, **_kwargs: Any) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted = True


class _ThreadExists(discord.HTTPException):
    """Discord refusing a second thread on one message. The 400 / code 160004
    shape is from memory of the API, not verified against a live gateway -- the
    fake exists so a test can tell "adopts the existing thread" from "tries to
    create another", not to prove Discord's wording."""

    def __init__(self) -> None:
        class _Bad:
            status = 400
            reason = "Bad Request"

        super().__init__(
            _Bad(),  # type: ignore[arg-type]
            {"code": 160004, "message": "A thread has already been created for this message"},
        )


class WatchPartialMessage(FakePartialMessage):
    """A `#games` message that can start a thread. It yields once first, like
    the real HTTP call, so two `/watch` coroutines really interleave. A message
    holds at most one thread (`WatchChannel.existing_threads`), as on Discord:
    asking for a second is refused, and `thread` / `fetch_thread` find the
    first the way `discord.PartialMessage` does (the cache holds active threads
    only; a fetch finds archived ones too)."""

    @property
    def thread(self) -> WatchThread | None:
        channel: WatchChannel = self._channel  # type: ignore[assignment]
        return channel.cached_threads.get(self.id)

    async def fetch_thread(self) -> WatchThread:
        channel: WatchChannel = self._channel  # type: ignore[assignment]
        channel.thread_fetches.append(self.id)
        await asyncio.sleep(0)
        if channel.fetch_error is not None:
            raise channel.fetch_error
        found = channel.existing_threads.get(self.id)
        if found is None:
            raise not_found()
        return found

    async def create_thread(self, **kwargs: Any) -> WatchThread:
        channel: WatchChannel = self._channel  # type: ignore[assignment]
        channel.thread_requests.append({"message_id": self.id, **kwargs})
        await asyncio.sleep(0)
        if channel.on_create_thread is not None:
            await channel.on_create_thread()
        if channel.thread_error is not None:
            raise channel.thread_error
        if self.id in channel.existing_threads:
            raise _ThreadExists()
        thread = WatchThread(
            THREAD_BASE + len(channel.threads),
            channel.clock,
            send_error=channel.thread_send_error,
            delete_error=channel.thread_delete_error,
        )
        channel.threads.append(thread)
        channel.existing_threads[self.id] = thread
        if channel.timeout_after_create:
            channel.timeout_after_create = False
            raise TimeoutError()  # Discord made it; the client gave up waiting
        return thread


class WatchChannel(FakeChannel):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock
        self.thread_error: BaseException | None = None
        self.on_create_thread: Callable[[], Awaitable[None]] | None = None
        self.thread_send_error: BaseException | None = None
        self.thread_delete_error: BaseException | None = None
        self.thread_requests: list[dict[str, Any]] = []
        self.threads: list[WatchThread] = []
        self.existing_threads: dict[int, WatchThread] = {}
        """message id -> the thread Discord holds for it (made by us or not)."""
        self.cached_threads: dict[int, WatchThread] = {}
        """The subset `PartialMessage.thread` can see without an API call."""
        self.thread_fetches: list[int] = []
        self.fetch_error: BaseException | None = None
        self.timeout_after_create = False

    def get_partial_message(self, message_id: int) -> WatchPartialMessage:
        return WatchPartialMessage(self, message_id)


class World:
    """A league, a bot, a `#games` channel and a `/watch` cog, patched onto
    `module` (the real cog, or a mutated variant for a twin)."""

    def __init__(
        self,
        monkeypatch,
        module: types.ModuleType = cog_module,
        *,
        settings: BroadcastSettings | None = None,
        phase: Phase = Phase.REGULAR_SEASON,
    ) -> None:
        self.clock = FakeClock()
        self.gate: asyncio.Event | None = None
        self.channel = WatchChannel(self.clock)
        self.store = Store().install(monkeypatch)
        self.bot = FakeBot(self.channel)
        self.bot.broadcasts = BroadcastScheduler(
            settings or BroadcastSettings(max_live_broadcasts_per_user=9),
            on_ended=functools.partial(game_feed.on_ended, self.bot, executor="pool"),
            clock=self.clock,
            sleep=self._sleep,
        )
        self.league = SimpleNamespace(id=LEAGUE, current_phase=phase, commissioner_user_id=1)
        self.cog = module.BroadcastCommands(self.bot)
        self.boxscores: list[int] = []
        self.inputs_calls: list[int] = []
        self.inputs_error: BaseException | None = None
        self.resolve_error: BaseException | None = None

        async def get_pool():
            return "pool"

        async def resolve_league(pool, interaction):
            if self.resolve_error is not None:
                raise self.resolve_error
            return self.league

        async def broadcast_inputs(ex, *, game_id, result):
            self.inputs_calls.append(game_id)
            if self.inputs_error is not None:
                raise self.inputs_error
            return view_service.BroadcastInputData(
                home=HOME_TEAM, away=AWAY_TEAM, names=names_for(result)
            )

        async def boxscore_panel(ex, *, game_id):
            self.boxscores.append(game_id)
            return BOX_PANEL

        monkeypatch.setattr(module, "get_pool", get_pool)
        monkeypatch.setattr(module, "resolve_league", resolve_league)
        monkeypatch.setattr(view_service, "get_broadcast_inputs", broadcast_inputs)
        monkeypatch.setattr(view_service, "get_boxscore_panel", boxscore_panel)

    async def _sleep(self, seconds: float) -> None:
        if self.gate is not None:
            await self.gate.wait()
        await self.clock.sleep(seconds)

    # -- seeding --------------------------------------------------------------

    def add_game(self, game_id: int = GID, **kwargs: Any):
        return self.store.add_game(game_id, **kwargs)

    def add_row(self, game_id: int = GID, *, root: bool = True, **kwargs: Any):
        """A `game_broadcasts` row, with its `#games` line already posted (the
        slate) unless `root=False` (a failed slate post)."""
        if root:
            kwargs["root_message_id"] = ROOT_BASE + game_id
            slate = SlateGame(
                game_id, HOME_TEAM.code, AWAY_TEAM.code, block_game(game_id).game_date
            )
            self.channel.content[ROOT_BASE + game_id] = build_matchup_line(slate)
        return self.store.add_row(game_id, **kwargs)

    def open_block(self, *game_ids: int, degraded: tuple[int, ...] = ()) -> None:
        self.bot.blocks.register_block(
            league_id=LEAGUE,
            block_id="2026-11-01",
            season=2026,
            games=[block_game(g, result=None if g in degraded else sim_result()) for g in game_ids],
        )

    def ready(self, game_id: int = GID, **row_kwargs: Any) -> None:
        """The ordinary case: a final game, its pending row and its open block."""
        self.add_game(game_id)
        self.add_row(game_id, **row_kwargs)
        self.open_block(game_id)

    # -- acting ---------------------------------------------------------------

    async def watch(
        self, game_id: int = GID, *, user_id: int = 10, speed: str | None = None
    ) -> FakeInteraction:
        """`/watch game_id [speed:]` -- `speed` is the Choice's value, as Discord sends
        it (docs/design/watch-narration-v2.md WN-10.4); `None` omits the option."""
        interaction = FakeInteraction(user_id)
        choice = None if speed is None else app_commands.Choice(name=speed, value=speed)
        await self.cog.watch.callback(self.cog, interaction, game_id, choice)
        return interaction

    def task(self, game_id: int = GID) -> asyncio.Task[None]:
        live = self.bot.broadcasts.live(LEAGUE, game_id)
        assert live is not None, f"no broadcast task for game {game_id}"
        return live.task

    async def finish(self, game_id: int = GID) -> None:
        await asyncio.wait_for(self.task(game_id), 30)

    def row(self, game_id: int = GID):
        return self.store.row(game_id)

    def games_text(self) -> str:
        return self.channel.all_text()


def cog_variant(*mutations: tuple[str, str]) -> types.ModuleType:
    """A private copy of `dba2/bot/cogs/broadcast.py` with each `(old, new)`
    mutation applied, loaded under a scratch name so the real module is
    untouched. Every anchor must occur exactly once; with no mutations it is
    the control, which every twin's scenario must PASS on."""
    source = Path(cog_module.__file__).read_text(encoding="utf-8")
    for old, new in mutations:
        if source.count(old) != 1:
            raise MutationAnchorError(f"mutation anchor {old!r} occurs {source.count(old)}x")
        source = source.replace(old, new)
    module = types.ModuleType("_bot_cogs_broadcast_variant")
    module.__package__ = "dba2.bot.cogs"  # its `from .. import game_feed`
    sys.modules[module.__name__] = module  # `@dataclass` resolves annotations through it
    try:
        exec(compile(source, "<watch cog variant>", "exec"), module.__dict__)
    finally:
        del sys.modules[module.__name__]
    return module
