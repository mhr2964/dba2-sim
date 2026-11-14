"""Shared fakes for `dba2/bot/game_feed.py`'s unit tests: a `#games` channel that
records every send and edit, a bot around it, an in-memory stand-in for the
repositories the module reads and writes (with the real repos' CAS semantics),
and a loader for a mutated private copy of the module (the mutation twins).

No DB, no gateway. The in-memory store is a MODEL of `broadcast_repo` --
`tests/integration/test_broadcast_repo.py` proves the real one does the same --
so a unit test here proves `game_feed`'s decisions, not SQL.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import discord

from dba2.bot import game_feed
from dba2.bot.block_registry import BlockGame, BlockRegistry
from dba2.bot.broadcast import DEFAULT_BROADCAST_SETTINGS
from dba2.bot.watch_pager import PagerRegistry
from dba2.data.repositories import broadcast_repo, channel_repo, game_repo, stats_repo, team_repo
from dba2.domain.broadcast import BroadcastStatus, GameBroadcast
from dba2.domain.channel import ChannelPurpose, LeagueChannel
from dba2.domain.game import Game, GameStatus, GameType
from dba2.domain.stats import GamePeriodScore
from tests.unit.presentation._broadcast_helpers import make_team

LEAGUE = 1
GAMES_CHANNEL_ID = 9001
HOME_TEAM = make_team(1, "HOM")
AWAY_TEAM = make_team(2, "AWY")
NOW = datetime(2026, 11, 1, tzinfo=UTC)

# Score digits that appear in no game id, team code, block id or header, so a
# leak of a score into a spoiler-free line is a plain substring check.
SCORES: dict[int, tuple[int, int]] = {1: (131, 117), 2: (149, 103), 3: (99, 86), 4: (157, 141)}


Hook = Callable[[int, dict[str, Any]], Awaitable[None]]
"""`(call_number, kwargs)`, `call_number` counting that one operation from 0. A
fake that never suspends can never be interleaved, so every fake here lets a
test run a hook at a named call point -- before the call takes effect -- to
suspend it, hang it, or run another coroutine's step in its place."""


async def ticks(count: int = 1) -> None:
    """Yield to the event loop `count` times: how a test makes one await slow."""
    for _ in range(count):
        await asyncio.sleep(0)


def slow(count: int = 20, *, only_call: int | None = None) -> Hook:
    """A hook that suspends the call for `count` loop iterations (every call, or
    only call number `only_call`)."""

    async def hook(n: int, kwargs: dict[str, Any]) -> None:
        if only_call is None or n == only_call:
            await ticks(count)

    return hook


class _Response:
    status = 500
    reason = "Internal Server Error"


def http_error(message: str = "discord failed") -> discord.HTTPException:
    return discord.HTTPException(_Response(), message)


def forbidden() -> discord.Forbidden:
    class _Forbidden(_Response):
        status = 403

    return discord.Forbidden(_Forbidden(), "Missing Permissions")


def not_found() -> discord.NotFound:
    class _NotFound(_Response):
        status = 404

    return discord.NotFound(_NotFound(), "Unknown Message")


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class FakePartialMessage:
    def __init__(self, channel: FakeChannel, message_id: int) -> None:
        self._channel = channel
        self.id = message_id

    async def edit(self, **kwargs: Any) -> None:
        await self._channel._edit(self.id, kwargs)


class FakeChannel:
    """A text channel (or thread) recording every `send`/`edit` as
    `(op, message_id, kwargs)` in `calls`, holding each message's current
    `content` in `content`. `fail_send`/`fail_edit` are call-number -> exception
    maps (call numbers count that op only, from 0); `edit_errors` maps a message
    id to an exception raised on every edit of it."""

    def __init__(self, channel_id: int = GAMES_CHANNEL_ID, *, first_message_id: int = 5000):
        self.id = channel_id
        self._next_id = first_message_id
        self.calls: list[tuple[str, int | None, dict[str, Any]]] = []
        self.content: dict[int, str | None] = {}
        self.embeds: dict[int, Any] = {}
        self.fail_send: dict[int, BaseException] = {}
        self.fail_edit: dict[int, BaseException] = {}
        self.edit_errors: dict[int, BaseException] = {}
        self.hooks: dict[str, Hook] = {}
        """`"send"` / `"edit"` -> a `Hook` run before that call takes effect."""
        self._sends = 0
        self._edits = 0

    async def send(self, **kwargs: Any) -> FakeMessage:
        n, self._sends = self._sends, self._sends + 1
        self.calls.append(("send", None, kwargs))
        if "send" in self.hooks:
            await self.hooks["send"](n, kwargs)
        if n in self.fail_send:
            raise self.fail_send[n]
        message = FakeMessage(self._next_id)
        self._next_id += 1
        self.content[message.id] = kwargs.get("content")
        return message

    def get_partial_message(self, message_id: int) -> FakePartialMessage:
        return FakePartialMessage(self, message_id)

    async def _edit(self, message_id: int, kwargs: dict[str, Any]) -> None:
        n, self._edits = self._edits, self._edits + 1
        self.calls.append(("edit", message_id, kwargs))
        if "edit" in self.hooks:
            await self.hooks["edit"](n, {"message_id": message_id, **kwargs})
        if n in self.fail_edit:
            raise self.fail_edit[n]
        if message_id in self.edit_errors:
            raise self.edit_errors[message_id]
        self.content[message_id] = kwargs.get("content")
        if "embed" in kwargs:
            self.embeds[message_id] = kwargs["embed"]

    # -- conveniences ---------------------------------------------------------

    def ops(self, op: str) -> list[tuple[int | None, dict[str, Any]]]:
        return [(mid, kw) for o, mid, kw in self.calls if o == op]

    def all_text(self) -> str:
        """Every string this channel was ever asked to show."""
        parts = [str(kw.get("content")) for _, _, kw in self.calls]
        for _, _, kw in self.calls:
            embed = kw.get("embed")
            if embed is not None:
                parts.append(str(embed.to_dict()))
        return "\n".join(parts)


class StubScheduler:
    """`is_live_or_queued` over explicit sets -- the one method `game_feed`
    reads -- plus `settings`, which the cycle click's cooldown reads: floored at
    `watch_pager._CLICK_COOLDOWN_FLOOR_SECONDS` (4.0s) rather than
    `bot.broadcasts.settings.tick_seconds` directly, since a preset's tick can now
    run faster than that floor (watch-v5 WT-2.a). The real scheduler's own behaviour
    is `test_broadcast_scheduler.py`'s business; a test that needs it uses
    `BroadcastScheduler` directly."""

    def __init__(self, settings: Any = None) -> None:
        self.live: set[tuple[int, int]] = set()
        self.settings = settings if settings is not None else DEFAULT_BROADCAST_SETTINGS

    def is_live_or_queued(self, league_id: int, game_id: int) -> bool:
        return (league_id, game_id) in self.live


class FakeBot:
    """The three things `game_feed` asks of the bot: `blocks`, `broadcasts`
    and channel lookup by id (cache, then `fetch_channel`); plus `pagers`, which
    `/watch` registers a game's pager with."""

    def __init__(self, *channels: FakeChannel, scheduler: Any = None) -> None:
        self.blocks = BlockRegistry()
        self.broadcasts = scheduler if scheduler is not None else StubScheduler()
        self.pagers = PagerRegistry()
        self.channels: dict[int, FakeChannel] = {c.id: c for c in channels}
        self.fetched: list[int] = []

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> FakeChannel:
        self.fetched.append(channel_id)
        raise not_found()


@dataclass
class Store:
    """The repository surface `game_feed` uses, in memory, with the real
    repositories' semantics: `claim_reveal` matches only an unclaimed row
    (the CAS), `mark_reveal_posted` only a claimed-unposted one, and
    `create_pending_broadcasts` is `ON CONFLICT DO NOTHING`. League-scoped,
    like the real ones. `raises` maps a function name to an exception raised on
    every call of it."""

    rows: dict[int, GameBroadcast] = field(default_factory=dict)
    games: dict[int, Game] = field(default_factory=dict)
    period_rows: dict[int, list[GamePeriodScore]] = field(default_factory=dict)
    games_channel_id: int | None = GAMES_CHANNEL_ID
    raises: dict[str, BaseException] = field(default_factory=dict)
    hooks: dict[str, Hook] = field(default_factory=dict)
    """Repository function name -> a `Hook` run when it is called, before it
    takes effect and before any `raises` entry: a slow database round trip."""
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _next_row_id: int = 1

    # -- seeding --------------------------------------------------------------

    def add_game(
        self,
        game_id: int,
        *,
        status: GameStatus = GameStatus.FINAL,
        scores: tuple[int, int] | None = None,
        league_id: int = LEAGUE,
    ) -> Game:
        home, away = scores if scores is not None else SCORES.get(game_id, (120, 110))
        game = Game(
            id=game_id,
            league_id=league_id,
            season=2026,
            game_index=game_id,
            game_date=date(2026, 11, 1),
            home_team_id=HOME_TEAM.id,
            away_team_id=AWAY_TEAM.id,
            home_score=home if status is GameStatus.FINAL else None,
            away_score=away if status is GameStatus.FINAL else None,
            status=status,
            game_type=GameType.REGULAR,
            rng_seed=game_id,
            simmed_at=NOW if status is GameStatus.FINAL else None,
            engine_version=1,
            roster_digest=None,
            overtime_periods=0,
        )
        self.games[game_id] = game
        # A two-period line score that sums to the final, for the interrupted panel.
        if status is GameStatus.FINAL:
            self.period_rows[game_id] = [
                GamePeriodScore(game_id, league_id, HOME_TEAM.id, 1, home - 50),
                GamePeriodScore(game_id, league_id, HOME_TEAM.id, 2, 50),
                GamePeriodScore(game_id, league_id, AWAY_TEAM.id, 1, away - 40),
                GamePeriodScore(game_id, league_id, AWAY_TEAM.id, 2, 40),
            ]
        return game

    def add_row(
        self,
        game_id: int,
        *,
        block_id: str = "2026-11-01",
        status: BroadcastStatus = BroadcastStatus.PENDING,
        root_message_id: int | None = None,
        thread_id: int | None = None,
        message_id: int | None = None,
        claimed: bool = False,
        posted: bool = False,
        league_id: int = LEAGUE,
    ) -> GameBroadcast:
        row = GameBroadcast(
            id=self._next_row_id,
            league_id=league_id,
            game_id=game_id,
            block_id=block_id,
            root_message_id=root_message_id,
            thread_id=thread_id,
            message_id=message_id,
            status=status,
            reveal_claimed_at=NOW if (claimed or posted) else None,
            reveal_posted_at=NOW if posted else None,
            created_at=NOW,
        )
        self._next_row_id += 1
        self.rows[game_id] = row
        return row

    def row(self, game_id: int) -> GameBroadcast:
        return self.rows[game_id]

    async def _enter(self, name: str, **kwargs: Any) -> None:
        number = sum(1 for n, _ in self.calls if n == name)
        self.calls.append((name, kwargs))
        if name in self.hooks:
            await self.hooks[name](number, kwargs)
        if name in self.raises:
            raise self.raises[name]

    def called(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]

    # -- the repository surface ------------------------------------------------

    async def create_pending_broadcasts(self, ex, *, league_id, block_id, game_ids) -> int:
        await self._enter("create_pending_broadcasts", game_ids=list(game_ids))
        created = 0
        for game_id in sorted(set(game_ids)):
            game = self.games.get(game_id)
            if game is None or game.league_id != league_id or game_id in self.rows:
                continue
            self.add_row(game_id, block_id=block_id, league_id=league_id)
            created += 1
        return created

    async def get_broadcast(self, ex, *, league_id, game_id) -> GameBroadcast | None:
        await self._enter("get_broadcast", game_id=game_id)
        row = self.rows.get(game_id)
        return row if row is not None and row.league_id == league_id else None

    async def set_root_message(self, ex, *, league_id, game_id, root_message_id) -> int:
        await self._enter("set_root_message", game_id=game_id)
        row = self.rows.get(game_id)
        if row is None or row.league_id != league_id:
            return 0
        self.rows[game_id] = dataclasses.replace(row, root_message_id=root_message_id)
        return 1

    async def claim_reveal(self, ex, *, league_id, game_id) -> int | None:
        await self._enter("claim_reveal", game_id=game_id)
        row = self.rows.get(game_id)
        if row is None or row.league_id != league_id or row.reveal_claimed_at is not None:
            return None
        self.rows[game_id] = dataclasses.replace(row, reveal_claimed_at=NOW)
        return row.id

    async def mark_reveal_posted(self, ex, *, league_id, broadcast_id) -> int:
        await self._enter("mark_reveal_posted", broadcast_id=broadcast_id)
        for game_id, row in self.rows.items():
            if (
                row.id == broadcast_id
                and row.league_id == league_id
                and row.reveal_claimed_at is not None
                and row.reveal_posted_at is None
            ):
                self.rows[game_id] = dataclasses.replace(row, reveal_posted_at=NOW)
                return 1
        return 0

    async def list_sweep_candidates(self, ex, *, league_id) -> list[GameBroadcast]:
        await self._enter("list_sweep_candidates")
        return sorted(
            (
                r
                for r in self.rows.values()
                if r.league_id == league_id and r.reveal_posted_at is None
            ),
            key=lambda r: r.id,
        )

    async def record_thread(self, ex, *, league_id, game_id, thread_id, message_id) -> int:
        """`UPDATE ... WHERE status = 'pending' AND thread_id IS NULL`."""
        await self._enter(
            "record_thread", game_id=game_id, thread_id=thread_id, message_id=message_id
        )
        row = self.rows.get(game_id)
        if (
            row is None
            or row.league_id != league_id
            or row.status is not BroadcastStatus.PENDING
            or row.thread_id is not None
        ):
            return 0
        self.rows[game_id] = dataclasses.replace(
            row, thread_id=thread_id, message_id=message_id, status=BroadcastStatus.QUEUED
        )
        return 1

    async def transition_status(self, ex, *, league_id, game_id, expected, new) -> int:
        await self._enter("transition_status", game_id=game_id, expected=expected, new=new)
        row = self.rows.get(game_id)
        if row is None or row.league_id != league_id or row.status is not expected:
            return 0
        self.rows[game_id] = dataclasses.replace(row, status=new)
        return 1

    async def get_league_channel(self, ex, *, league_id, purpose) -> LeagueChannel | None:
        await self._enter("get_league_channel", purpose=purpose)
        if purpose is not ChannelPurpose.GAMES or self.games_channel_id is None:
            return None
        return LeagueChannel(
            id=1,
            league_id=league_id,
            purpose=purpose,
            channel_id=self.games_channel_id,
            message_id=None,
            created_at=NOW,
        )

    async def get_game(self, ex, game_id) -> Game | None:
        await self._enter("get_game", game_id=game_id)
        return self.games.get(game_id)

    async def get_team(self, ex, team_id):
        return {HOME_TEAM.id: HOME_TEAM, AWAY_TEAM.id: AWAY_TEAM}.get(team_id)

    async def list_game_period_scores(self, ex, game_id) -> list[GamePeriodScore]:
        return list(self.period_rows.get(game_id, []))

    def install(self, monkeypatch) -> Store:
        for module, names in (
            (
                broadcast_repo,
                (
                    "create_pending_broadcasts",
                    "get_broadcast",
                    "set_root_message",
                    "claim_reveal",
                    "mark_reveal_posted",
                    "list_sweep_candidates",
                    "transition_status",
                    "record_thread",
                ),
            ),
            (channel_repo, ("get_league_channel",)),
            (game_repo, ("get_game",)),
            (team_repo, ("get_team",)),
            (stats_repo, ("list_game_period_scores",)),
        ):
            for name in names:
                monkeypatch.setattr(module, name, getattr(self, name))
        return self


def block_game(game_id: int, *, result: Any = None) -> BlockGame:
    """A `BlockGame` with no result by default: `game_feed` must never need one."""
    return BlockGame(
        game_id=game_id,
        home_team_id=HOME_TEAM.id,
        away_team_id=AWAY_TEAM.id,
        home_code=HOME_TEAM.code,
        away_code=AWAY_TEAM.code,
        game_date=date(2026, 11, 1),
        result=result,
    )


def open_in_memory(bot: FakeBot, *game_ids: int, block_id: str = "2026-11-01") -> None:
    """Register a block without going through `open_block`."""
    bot.blocks.register_block(
        league_id=LEAGUE, block_id=block_id, season=2026, games=[block_game(g) for g in game_ids]
    )


def assert_no_mentions(kwargs: dict[str, Any]) -> None:
    mentions = kwargs.get("allowed_mentions")
    assert isinstance(mentions, discord.AllowedMentions), f"no allowed_mentions in {kwargs!r}"
    assert not (mentions.everyone or mentions.roles or mentions.replied_user or mentions.users)


class MutationAnchorError(Exception):
    """Not an `AssertionError`: a twin expects the SCENARIO to fail, and a stale
    anchor must not be able to pass for that."""


GAME_FEED_SOURCE = Path(game_feed.__file__)


def variant(*mutations: tuple[str, str]) -> types.ModuleType:
    """A private copy of `dba2/bot/game_feed.py` with each `(old, new)` mutation
    applied, loaded under a scratch name so the real module is untouched. Every
    anchor must occur exactly once (a twin that mutates nothing proves nothing).
    With no mutations it is the control -- the twin's scenario must PASS on it,
    or "red" could just be a loading failure."""
    source = GAME_FEED_SOURCE.read_text(encoding="utf-8")
    for old, new in mutations:
        if source.count(old) != 1:
            raise MutationAnchorError(f"mutation anchor {old!r} occurs {source.count(old)}x")
        source = source.replace(old, new)
    module = types.ModuleType("_bot_game_feed_variant")
    module.__package__ = "dba2.bot"  # its `from .block_registry import ...`
    sys.modules[module.__name__] = module  # `@dataclass` resolves annotations through it
    try:
        exec(compile(source, "<bot game_feed variant>", "exec"), module.__dict__)
    finally:
        del sys.modules[module.__name__]
    return module
