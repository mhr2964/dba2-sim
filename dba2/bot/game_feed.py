"""The `#games` channel's life cycle: open a block of games, reveal each result
once it is safe to, close the block, and converge whatever a crash left behind
(docs/design/phase-5-stage-d-broadcast.md §5-§6, Decisions P5-97.m/.n/.q/.v).

**The constraint that shapes everything: a score reaches `#games` only when
BOTH the game's block is closed AND nobody is still broadcasting it (P5-97.m).**
`#games` is public; a game is spoiled only to the people who chose to watch it.
So:

* `reveal_game` is the ONLY publisher of a score to `#games`, and
  `build_result_line` (the only renderer that accepts a score) has exactly one
  call site, inside it. Its callers are pinned at three -- the broadcast
  finalizer (`on_ended`), `close_block`, and `sweep_league` -- by a unit test.
  `open_block` reaches it only through `close_block`.
* The reveal is a two-phase compare-and-swap so a crash between the database
  and Discord loses nothing and duplicates nothing: `claim_reveal` (zero rows
  means someone else owns this reveal -- post NOTHING), then the Discord edit
  (or a fresh post when the game has no root message), then
  `mark_reveal_posted`. A row left claimed-but-unposted is the sweep's
  `CRASHED_REVEAL` shape, and it is completed WITHOUT claiming again
  (`resume_crashed=True`).
* Scores and team codes for a reveal come from PERSISTED rows (`games`,
  `teams`), never from an in-memory `GameResult`, so a reveal works after a
  restart when no result survives (P5-42).

**No Discord call inside a database transaction (P5-14).** Every repository
call here is a single statement that commits on its own; the entry points
refuse a `Connection` that is already inside a transaction, because a
transaction the caller holds open would put every Discord call after it inside
one.

**Failures degrade, they do not abort.** One failed post never stops the rest
of a slate (its `root_message_id` stays NULL and `/watch` or the sweep repairs
it lazily -- `run_after_commit`-style swallowing would hide it, P5-97.v); a
Discord failure between claim and mark leaves the reveal claimed-not-posted for
the sweep; one bad row never blocks the rest of a sweep. Discord failures are
logged by exception name and, for `Forbidden`, by the permissions `#games`
needs. Every send and edit passes `AllowedMentions.none()` (P5-97.s): a team
name is a database string, and a league that names a team `@everyone` must not
be able to ping a guild through a scoreboard.

**One predicate decides whether a score may be public: `_may_reveal`** -- the
block is closed AND `bot.broadcasts` holds no live, queued or admitted broadcast
of the game. `on_ended`, `close_block` and `sweep_league` all ask it, and each
asks it AGAIN after any await it made since the last look, because a `/watch`
can be admitted (or a block closed) in that gap. The game is then revealed by
whichever broadcast ends LAST: every earlier `on_ended` sees the newer
admission and stands down.

**One game is touched by one coroutine at a time.** `reveal_game` and the
matchup-line restore hold a per-game lock (`_exclusive`) for their whole run, so
a restore's slow edit can never land after a result, and the sweep -- which
would otherwise mistake a reveal still in flight for a crashed one and post the
result twice -- can ask `_reveal_in_flight` and leave it alone.

**The sweep only touches what this process does not own.** The design's sweep
runs at startup, when memory is empty and every block counts as closed.
`on_ready` also re-fires on a gateway reconnect, mid-process; running the sweep
then would interrupt live broadcasts and reveal games in an open block. So it
skips any row whose reveal is not allowed (`_may_reveal`) or whose game has a
reveal or restore in flight -- which at startup is nothing, so the design's
behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import aiohttp
import asyncpg
import discord

from dba2.core.logging import get_logger
from dba2.data.db import Executor, get_pool
from dba2.data.repositories import (
    broadcast_repo,
    channel_repo,
    game_repo,
    stats_repo,
    team_repo,
)
from dba2.domain.broadcast import BroadcastStatus, GameBroadcast, SweepShape
from dba2.domain.channel import ChannelPurpose
from dba2.domain.game import Game, GameStatus
from dba2.domain.team import Team
from dba2.presentation.broadcast import render_interrupted
from dba2.presentation.games import (
    SlateGame,
    build_matchup_line,
    build_result_line,
    build_slate_header,
)

from .block_registry import BlockGame
from .embeds import panel_to_embed

log = get_logger(__name__)

DISCORD_CALL_TIMEOUT_SECONDS = 10.0
"""Every Discord call here is bounded: the sweep runs inside `on_ready`'s
reconcile, and an unbounded hang there would stall the whole guild loop."""

_GAMES_CHANNEL_PERMISSIONS = "view_channel, send_messages, read_message_history"

REVEAL_ATTEMPTS = 3
"""Attempts (not retries) for each of the two things a reveal does after it has
claimed the row: the Discord edit / fresh post, and `mark_reveal_posted`. A
single 5xx or a database blip is the ordinary failure, and without this a row
left claimed-not-posted waited for the next sweep (a restart or a reconnect)."""

_RETRY_BACKOFF_SECONDS = 0.5
"""Before attempt `n` (n >= 2) the reveal waits `(n - 1)` of these: 0.5s, 1.0s."""

_retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
"""The backoff's sleep, module-level so a test can replace it."""


def _is_transient_discord(exc: Exception) -> bool:
    """What is worth another attempt: an HTTP or connection failure. NOT
    `TimeoutError` (it already cost `DISCORD_CALL_TIMEOUT_SECONDS`; retrying
    would let a Discord outage hold `close_block` -- and so the producer -- for
    minutes, and the call may have landed; note it IS an `OSError`, hence the
    explicit test), and not `Forbidden` / `NotFound` (a second try cannot
    succeed; `NotFound` on an edit is answered by a fresh post instead)."""
    if isinstance(exc, (TimeoutError, discord.Forbidden, discord.NotFound)):
        return False
    return isinstance(exc, (discord.HTTPException, OSError, aiohttp.ClientError))


def _any_failure(exc: Exception) -> bool:
    return True


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


class RevealOutcome(StrEnum):
    REVEALED = "revealed"
    """The result line is in `#games` and the row is marked posted."""
    NOT_OWNED = "not_owned"
    """The claim matched zero rows (or, on resume, the row is not a crashed
    reveal): someone else owns this reveal and NOTHING was posted."""
    NOT_REVEALABLE = "not_revealable"
    """The game is absent, in another league, or not final: there is no
    result to publish, and nothing was claimed."""
    POST_FAILED = "post_failed"
    """The reveal was claimed but Discord refused it. The row stays
    claimed-not-posted -- the sweep's `CRASHED_REVEAL` shape completes it."""
    MARK_FAILED = "mark_failed"
    """The line reached `#games` but recording that failed; same recovery."""
    ERRORED = "errored"
    """An unexpected failure (logged with its traceback); the row is left as
    the failure found it, and the sweep converges it."""


@dataclass(frozen=True)
class PostFailure:
    game_id: int | None
    """`None` is the slate header."""
    reason: str


@dataclass(frozen=True)
class CloseBlockReport:
    block_id: str | None
    """`None` when the league had no open block."""
    outcomes: dict[int, RevealOutcome]
    """game id -> what its reveal attempt did, for every game revealed here."""
    still_broadcasting: tuple[int, ...]
    """Games left for their own `on_ended`, which will see the block closed."""


@dataclass(frozen=True)
class OpenBlockReport:
    league_id: int
    block_id: str
    registered: int
    rows_created: int
    """New `game_broadcasts` rows; `0` on an idempotent re-open (M10)."""
    header_posted: bool
    posted: tuple[int, ...]
    """Games whose root message was posted (and its id persisted) now."""
    already_posted: tuple[int, ...]
    """Games that already had a root message (a re-open is a repair pass)."""
    failed: tuple[PostFailure, ...]
    previous_block: CloseBlockReport | None


@dataclass(frozen=True)
class SweepEntry:
    game_id: int
    shape: SweepShape | None
    reveal: RevealOutcome | None
    """`None` when the row was skipped or the sweep failed before revealing."""
    skipped_owned: bool = False
    error: str | None = None


@dataclass(frozen=True)
class SweepReport:
    league_id: int
    entries: tuple[SweepEntry, ...]

    @property
    def revealed(self) -> int:
        return sum(1 for e in self.entries if e.reveal is RevealOutcome.REVEALED)

    @property
    def failed(self) -> int:
        return sum(1 for e in self.entries if e.error is not None)


# ---------------------------------------------------------------------------
# Discord plumbing -- the ONLY places #games or a broadcast message is written
# ---------------------------------------------------------------------------


class GamesChannelUnavailable(RuntimeError):
    """The league has no `#games` channel registered, or it does not resolve
    or cannot take a message."""


def describe_discord_failure(exc: BaseException) -> str:
    """A failure by NAME, so an operator can tell which half broke without
    reading a traceback: the same "tell the truth about what failed" contract
    `channels.py` keeps."""
    name = type(exc).__name__
    if isinstance(exc, discord.Forbidden):
        return f"{name}: the bot lacks a permission in #games (needs {_GAMES_CHANNEL_PERMISSIONS})"
    if isinstance(exc, discord.NotFound):
        return f"{name}: the channel or message no longer exists"
    if isinstance(exc, discord.HTTPException):
        return f"{name} (HTTP {getattr(exc, 'status', '?')})"
    if isinstance(exc, TimeoutError):
        return f"{name}: Discord did not answer within {DISCORD_CALL_TIMEOUT_SECONDS:g}s"
    detail = str(exc)
    return f"{name}: {detail}" if detail else name


async def bounded[T](awaitable: Awaitable[T]) -> T:
    return await asyncio.wait_for(awaitable, DISCORD_CALL_TIMEOUT_SECONDS)


async def _attempts[T](
    describe: str,
    game_id: int,
    operation: Callable[[], Awaitable[T]],
    *,
    retry_if: Callable[[Exception], bool],
) -> T:
    """Run `operation` up to `REVEAL_ATTEMPTS` times, backing off between
    attempts, while it fails with an `Exception` that `retry_if` accepts. The
    last failure -- or the first one `retry_if` refuses -- propagates
    unchanged."""
    for attempt in range(1, REVEAL_ATTEMPTS + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt == REVEAL_ATTEMPTS or not retry_if(exc):
                raise
            log.warning(
                "game %s: %s failed (attempt %d/%d), retrying: %s",
                game_id,
                describe,
                attempt,
                REVEAL_ATTEMPTS,
                describe_discord_failure(exc),
            )
            await _retry_sleep(_RETRY_BACKOFF_SECONDS * attempt)
    raise AssertionError("unreachable: the last attempt returns or raises")


async def _resolve_channel(bot: Any, channel_id: int) -> Any:
    """Cache first, then one fetch -- the lookup `offer_delivery` uses. Only a
    channel that can post is returned (a category or voice channel cannot)."""
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bounded(bot.fetch_channel(channel_id))
    if not callable(getattr(channel, "send", None)) or not callable(
        getattr(channel, "get_partial_message", None)
    ):
        raise GamesChannelUnavailable(f"channel {channel_id} cannot take messages")
    return channel


async def games_channel(bot: Any, ex: Executor, league_id: int) -> Any:
    row = await channel_repo.get_league_channel(
        ex, league_id=league_id, purpose=ChannelPurpose.GAMES
    )
    if row is None:
        raise GamesChannelUnavailable(
            f"no #games channel registered for league {league_id} -- "
            "run bot.channels.reconcile_league_channels first"
        )
    return await _resolve_channel(bot, row.channel_id)


async def send_line(channel: Any, content: str) -> int:
    message = await bounded(
        channel.send(content=content, allowed_mentions=discord.AllowedMentions.none())
    )
    return message.id


async def edit_line(channel: Any, message_id: int, content: str) -> None:
    await bounded(
        channel.get_partial_message(message_id).edit(
            content=content, allowed_mentions=discord.AllowedMentions.none()
        )
    )


def _require_no_open_transaction(ex: Executor) -> None:
    """P5-14 as a guard: every Discord call in this module follows a database
    call, so a caller that hands in a connection with a transaction open would
    put them inside it."""
    if isinstance(ex, asyncpg.Connection) and ex.is_in_transaction():
        raise ValueError(
            "game_feed must not be called from inside a database transaction: "
            "Discord is never written to inside one (P5-14)"
        )


def _may_reveal(bot: Any, league_id: int, game_id: int) -> bool:
    """P5-97.m as one question: the game's block is closed AND nobody is still
    broadcasting it. "Nobody" includes an admission that has no task yet (a
    `/watch` between `admit` and `spawn`) -- `is_live_or_queued` is true from
    `admit` until the finalizer ran. Synchronous, so the answer is exact at the
    moment it is read; a caller that awaits afterwards must ask again."""
    return bot.blocks.is_closed(league_id, game_id) and not bot.broadcasts.is_live_or_queued(
        league_id, game_id
    )


def box_score_is_public(broadcast: GameBroadcast | None) -> bool:
    """The ONE definition of "this game's score has been revealed", for every per-game
    surface that could leak it (P5-99.n: `/box`). It sits beside `_may_reveal` on
    purpose and rests on the same fact from the persisted side: `_may_reveal` asks
    whether a reveal MAY happen now (memory: block closed, nobody broadcasting), this
    asks whether it HAS (`game_broadcasts.reveal_posted_at`, via
    `GameBroadcast.is_revealed`). A claimed-but-unposted reveal is not public yet.

    A game with no `game_broadcasts` row is in no block, so nothing is holding its
    score back and it is public. A caller must never restate this from the columns: a
    second definition is how `/box` and `#games` would come to disagree."""
    return broadcast is None or broadcast.is_revealed


@dataclass
class _GameGuard:
    lock: asyncio.Lock
    users: int = 0
    """Coroutines holding or waiting for the lock; the entry is dropped at 0."""


_guards: dict[tuple[int, int], _GameGuard] = {}
"""Process-local, keyed `(league_id, game_id)`. One event loop, one bot, so no
cross-process story is claimed or needed."""


@contextlib.asynccontextmanager
async def _exclusive(league_id: int, game_id: int) -> AsyncIterator[None]:
    """Serialize everything that writes one game's `#games` line. Entries are
    dropped when unused, so the dict holds only games with work in flight."""
    key = (league_id, game_id)
    guard = _guards.setdefault(key, _GameGuard(asyncio.Lock()))
    guard.users += 1
    try:
        async with guard.lock:
            yield
    finally:
        guard.users -= 1
        if guard.users == 0:
            _guards.pop(key, None)


def _reveal_in_flight(league_id: int, game_id: int) -> bool:
    """True while a reveal or a matchup-line restore of this game is running or
    waiting in this process. A row that looks claimed-but-unposted is only a
    CRASHED reveal if this is False."""
    return (league_id, game_id) in _guards


# ---------------------------------------------------------------------------
# Persisted facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PersistedGame:
    game: Game
    home: Team
    away: Team

    @property
    def slate(self) -> SlateGame:
        return SlateGame(
            game_id=self.game.id,
            home_code=self.home.code,
            away_code=self.away.code,
            game_date=self.game.game_date,
        )


async def _load_persisted_game(ex: Executor, league_id: int, game_id: int) -> _PersistedGame | None:
    """The game and its two teams as the database holds them, or `None` (logged)
    when the game is missing, belongs to another league, or a team is gone."""
    game = await game_repo.get_game(ex, game_id)
    if game is None or game.league_id != league_id:
        log.warning("game %s is not in league %s; nothing to show", game_id, league_id)
        return None
    home = await team_repo.get_team(ex, game.home_team_id)
    away = await team_repo.get_team(ex, game.away_team_id)
    if home is None or away is None:
        log.warning("game %s references a missing team; nothing to show", game_id)
        return None
    return _PersistedGame(game=game, home=home, away=away)


# ---------------------------------------------------------------------------
# The reveal -- the ONLY publisher of a score to #games
# ---------------------------------------------------------------------------


async def reveal_game(
    bot: Any,
    ex: Executor,
    *,
    league_id: int,
    game_id: int,
    resume_crashed: bool = False,
) -> RevealOutcome:
    """Publish one game's result to `#games`, at most once (P5-97.m, M4).

    The caller has already decided the game may be revealed (its block is
    closed and it has no live or queued broadcast); this function decides
    nothing about that, it makes the publish safe to attempt from any of three
    callers in any order:

    1. Read the final score and both team codes from the database. A game that
       is not final has no result, so nothing is claimed.
    2. `claim_reveal` -- zero rows means another caller owns this reveal, so
       return and post nothing. (`resume_crashed=True`, used only by the sweep
       for a claimed-but-unposted row, skips the claim it cannot retake and
       requires that state instead.)
    3. Edit the game's root line to the result -- or post it fresh when the row
       has no root message, or the message has been deleted -- then
       `mark_reveal_posted`. Each of those two is attempted up to
       `REVEAL_ATTEMPTS` times with a short backoff (`_attempts`): the edit on
       an HTTP / connection failure, the write on any failure. A fresh post is
       never retried after a timeout, and nothing retries a permission or
       not-found failure.

    A failure that survives the retries is logged and leaves the row
    claimed-not-posted; the sweep's `CRASHED_REVEAL` shape completes it (at the
    next startup or gateway reconnect -- `close_block` / `on_ended` do not
    revisit a game they already tried). Never raises for an `Exception` (it is
    logged and reported as `ERRORED`).

    Runs under the game's `_exclusive` lock: a second reveal (or a restore)
    waits, then finds the row claimed and does nothing, and the sweep leaves a
    game whose reveal is in flight alone (`_reveal_in_flight`)."""
    _require_no_open_transaction(ex)
    async with _exclusive(league_id, game_id):
        try:
            persisted = await _load_persisted_game(ex, league_id, game_id)
            if persisted is None:
                return RevealOutcome.NOT_REVEALABLE
            game = persisted.game
            if (
                game.status is not GameStatus.FINAL
                or game.home_score is None
                or game.away_score is None
            ):
                log.warning("game %s is not final; its result cannot be revealed yet", game_id)
                return RevealOutcome.NOT_REVEALABLE

            if resume_crashed:
                current = await broadcast_repo.get_broadcast(
                    ex, league_id=league_id, game_id=game_id
                )
                if (
                    current is None
                    or current.reveal_claimed_at is None
                    or current.reveal_posted_at is not None
                ):
                    return RevealOutcome.NOT_OWNED
                broadcast_id = current.id
            else:
                claimed_id = await broadcast_repo.claim_reveal(
                    ex, league_id=league_id, game_id=game_id
                )
                if claimed_id is None:
                    return RevealOutcome.NOT_OWNED
                broadcast_id = claimed_id

            # After the claim, so a root message persisted while we were loading
            # (a slate post, `/watch`'s repair) is the one we edit.
            row = await broadcast_repo.get_broadcast(ex, league_id=league_id, game_id=game_id)
            if row is None:  # cannot happen after a successful claim; be loud, not silent
                log.error("game %s: broadcast row vanished after the reveal claim", game_id)
                return RevealOutcome.ERRORED

            line = build_result_line(
                persisted.slate, home_score=game.home_score, away_score=game.away_score
            )
            try:
                await _publish_result_line(
                    bot,
                    ex,
                    league_id=league_id,
                    game_id=game_id,
                    root_id=row.root_message_id,
                    line=line,
                )
            except Exception as exc:
                log.warning(
                    "game %s: reveal claimed but not posted -- the sweep will complete it: %s",
                    game_id,
                    describe_discord_failure(exc),
                    exc_info=not isinstance(exc, (discord.HTTPException, TimeoutError)),
                )
                return RevealOutcome.POST_FAILED

            try:
                marked = await _attempts(
                    "recording the reveal",
                    game_id,
                    lambda: broadcast_repo.mark_reveal_posted(
                        ex, league_id=league_id, broadcast_id=broadcast_id
                    ),
                    retry_if=_any_failure,
                )
            except Exception:
                log.exception(
                    "game %s: result posted but could not be recorded; the sweep will re-edit it",
                    game_id,
                )
                return RevealOutcome.MARK_FAILED
            if not marked:
                log.warning("game %s: reveal posted but the row was already marked", game_id)
            return RevealOutcome.REVEALED
        except Exception:
            log.exception("game %s: reveal failed unexpectedly", game_id)
            return RevealOutcome.ERRORED


async def _publish_result_line(
    bot: Any, ex: Executor, *, league_id: int, game_id: int, root_id: int | None, line: str
) -> None:
    """Edit the root line to `line`, or post it fresh when there is no root
    message (a failed slate post, P5-97.v) or it was deleted. A fresh message's
    id is written back so `/watch` and any later edit find it; that write is
    best-effort -- the line is already public."""
    channel = await _attempts(
        "resolving #games",
        game_id,
        lambda: games_channel(bot, ex, league_id),
        retry_if=_is_transient_discord,
    )
    if root_id is not None:
        try:
            await _attempts(
                "editing the result in",
                game_id,
                lambda: edit_line(channel, root_id, line),
                retry_if=_is_transient_discord,
            )
            return
        except discord.NotFound:
            log.warning(
                "game %s: root message %s is gone; posting the result fresh", game_id, root_id
            )
    fresh_id = await _attempts(
        "posting the result",
        game_id,
        lambda: send_line(channel, line),
        retry_if=_is_transient_discord,
    )
    try:
        await broadcast_repo.set_root_message(
            ex, league_id=league_id, game_id=game_id, root_message_id=fresh_id
        )
    except Exception:
        log.exception("game %s: could not record the new root message %s", game_id, fresh_id)


# ---------------------------------------------------------------------------
# Producer interface (Stage E's one call) and block close
# ---------------------------------------------------------------------------


async def close_block(bot: Any, ex: Executor, *, league_id: int) -> CloseBlockReport:
    """Pop the league's open block and reveal every game in it that has no live
    or queued broadcast (P5-97.m). A game still broadcasting is left for its
    own `on_ended`, which will find the block closed and reveal on completion.
    A league with no open block is a no-op."""
    _require_no_open_transaction(ex)
    block = bot.blocks.close(league_id)
    if block is None:
        return CloseBlockReport(block_id=None, outcomes={}, still_broadcasting=())
    outcomes: dict[int, RevealOutcome] = {}
    still: list[int] = []
    for game_id in block.games:
        if not _may_reveal(bot, league_id, game_id):
            still.append(game_id)
            continue
        outcomes[game_id] = await reveal_game(bot, ex, league_id=league_id, game_id=game_id)
    return CloseBlockReport(
        block_id=block.block_id, outcomes=outcomes, still_broadcasting=tuple(still)
    )


async def open_block(
    bot: Any,
    ex: Executor,
    *,
    league_id: int,
    season: int,
    block_id: str,
    games: Sequence[BlockGame],
) -> OpenBlockReport:
    """Open a new block of simmed games in `#games` (P5-97.n), in order:

    0. Refuse a `games` list that repeats a game, before anything is written.
    1. Close the league's previous block and reveal its idle games. Skipped
       when the open block already has this `block_id`: a retried open must not
       reveal the games it is about to re-open.
    2. Insert one `pending` `game_broadcasts` row per game (idempotent).
    3. Register the block in memory -- AFTER its rows exist. Re-opening the
       SAME `block_id` MERGES: a game the block already holds stays registered
       even if `games` omits it (dropping it would make it "closed", and so
       revealable, while its block is still open), and `games` entries replace
       the held ones. Only a different `block_id` replaces the block wholesale.
    4. Post a header and one root message per game, persisting each
       `root_message_id` as it is posted. A game that already has one is left
       alone, so a re-open repairs a partly failed slate instead of doubling
       it. The header is posted once per block: not again while a game of this
       call has a line, nor after a header this process already posted (a
       re-open after every game post failed).

    A failed post is recorded in the report and leaves `root_message_id` NULL
    for the lazy repair (`/watch`, the sweep). A database error in step 2
    propagates to the producer with NOTHING registered: a block whose rows do
    not exist is not open (a failure after step 3 leaves the block registered
    with its rows in place)."""
    _require_no_open_transaction(ex)
    seen: set[int] = set()
    for game in games:
        if game.game_id in seen:
            raise ValueError(f"game {game.game_id} appears twice in block {block_id!r}")
        seen.add(game.game_id)
    current = bot.blocks.current(league_id)
    previous: CloseBlockReport | None = None
    if current is not None and current.block_id != block_id:
        previous = await close_block(bot, ex, league_id=league_id)

    # A single statement on a connection with no transaction open (checked
    # above): its own committed write, before any Discord call.
    created = await broadcast_repo.create_pending_broadcasts(
        ex, league_id=league_id, block_id=block_id, game_ids=[g.game_id for g in games]
    )
    # Read AFTER the await: a `close_block` may have closed this block meanwhile,
    # and re-registering its old games would resurrect it.
    held = bot.blocks.current(league_id)
    same_block = held if held is not None and held.block_id == block_id else None
    kept = [g for g in same_block.games.values() if g.game_id not in seen] if same_block else []
    registered = bot.blocks.register_block(
        league_id=league_id, block_id=block_id, season=season, games=[*kept, *games]
    )
    header_already_posted = same_block is not None and same_block.header_posted
    registered.header_posted = header_already_posted

    rows: dict[int, GameBroadcast | None] = {}
    for game in games:
        rows[game.game_id] = await broadcast_repo.get_broadcast(
            ex, league_id=league_id, game_id=game.game_id
        )
    to_post: list[BlockGame] = []
    already: list[int] = []
    failures: list[PostFailure] = []
    for game in games:
        row = rows[game.game_id]
        if row is None:
            failures.append(
                PostFailure(game.game_id, "no game_broadcasts row (the game is not in this league)")
            )
        elif row.root_message_id is None:
            to_post.append(game)
        else:
            already.append(game.game_id)
    want_header = bool(games) and not already and not header_already_posted

    channel: Any = None
    if to_post or want_header:
        try:
            channel = await games_channel(bot, ex, league_id)
        except Exception as exc:
            reason = describe_discord_failure(exc)
            log.warning(
                "league %s: cannot open block %s in #games: %s", league_id, block_id, reason
            )
            failures.extend(PostFailure(g.game_id, reason) for g in to_post)
            if want_header:
                failures.append(PostFailure(None, reason))
            return OpenBlockReport(
                league_id=league_id,
                block_id=block_id,
                registered=len(registered.games),
                rows_created=created,
                header_posted=False,
                posted=(),
                already_posted=tuple(already),
                failed=tuple(failures),
                previous_block=previous,
            )

    header_posted = False
    if want_header:
        try:
            await send_line(channel, build_slate_header(block_id, len(games)))
            header_posted = True
            registered.header_posted = True
        except Exception as exc:
            reason = describe_discord_failure(exc)
            log.warning("block %s: slate header not posted: %s", block_id, reason)
            failures.append(PostFailure(None, reason))

    posted: list[int] = []
    for game in to_post:
        slate = SlateGame(
            game_id=game.game_id,
            home_code=game.home_code,
            away_code=game.away_code,
            game_date=game.game_date,
        )
        try:
            message_id = await send_line(channel, build_matchup_line(slate))
        except Exception as exc:
            reason = describe_discord_failure(exc)
            log.warning("game %s: root message not posted: %s", game.game_id, reason)
            failures.append(PostFailure(game.game_id, reason))
            continue
        try:
            await broadcast_repo.set_root_message(
                ex, league_id=league_id, game_id=game.game_id, root_message_id=message_id
            )
        except Exception as exc:
            log.exception(
                "game %s: root message %s posted but not recorded", game.game_id, message_id
            )
            failures.append(
                PostFailure(game.game_id, f"posted as message {message_id} but not recorded: {exc}")
            )
            continue
        posted.append(game.game_id)

    return OpenBlockReport(
        league_id=league_id,
        block_id=block_id,
        registered=len(registered.games),
        rows_created=created,
        header_posted=header_posted,
        posted=tuple(posted),
        already_posted=tuple(already),
        failed=tuple(failures),
        previous_block=previous,
    )


# ---------------------------------------------------------------------------
# The BroadcastEnded seam
# ---------------------------------------------------------------------------


async def on_ended(
    bot: Any,
    *,
    league_id: int,
    game_id: int,
    status: BroadcastStatus,
    executor: Executor | None = None,
) -> None:
    """What happens once a broadcast is over: the concrete `BroadcastEnded`
    (`functools.partial(on_ended, bot)` satisfies the protocol).

    * Another broadcast of the game is admitted (a `/watch` that took it after
      this one started): do NOTHING. That broadcast owns the line ("watching
      now") and its own `on_ended` restores or reveals it -- the game is
      revealed by the LAST broadcast to end.
    * Block still open -> NO reveal; the root line goes back to the matchup
      line (dropping "watching now").
    * Block closed -> `reveal_game`.

    `INTERRUPTED` is treated identically to `FINISHED` (an interrupted game is
    over too). The reveal question is asked again after the restore edit
    (`_may_reveal`, the predicate `close_block` uses), because a `close_block`
    may have run meanwhile -- and it skipped this game as still broadcasting,
    so nothing else would reveal it -- or a new broadcast may have been
    admitted, in which case it must not. Never raises: the scheduler logs an
    exception from here, but a reveal failure is recoverable by the sweep
    either way."""
    try:
        ex = executor if executor is not None else await get_pool()
        if bot.broadcasts.is_live_or_queued(league_id, game_id):
            return
        if bot.blocks.is_closed(league_id, game_id):
            await reveal_game(bot, ex, league_id=league_id, game_id=game_id)
            return
        await _restore_matchup_line(bot, ex, league_id=league_id, game_id=game_id)
        if _may_reveal(bot, league_id, game_id):
            await reveal_game(bot, ex, league_id=league_id, game_id=game_id)
    except Exception:
        log.exception("game %s: on_ended (%s) failed", game_id, status)


async def _restore_matchup_line(bot: Any, ex: Executor, *, league_id: int, game_id: int) -> None:
    """Put a still-unrevealed game's root line back to "not yet played". A game
    with no root message has nothing to restore (`/watch` and the sweep repair
    that). Discord failures are logged; the line then just keeps saying
    "watching now" until the reveal replaces it.

    Holds the game's `_exclusive` lock, so a reveal cannot land between the
    checks below and the edit (it waits for the restore, then overwrites it).
    What can make the edit wrong is checked after the last await before it: the
    row is not claimed, the block is still open and no newer broadcast has been
    admitted. If any of that changed while the row and game were loading, the
    edit is skipped and the owner of the new state (`close_block`, the newer
    broadcast's own `on_ended`) writes the line."""
    async with _exclusive(league_id, game_id):
        row = await broadcast_repo.get_broadcast(ex, league_id=league_id, game_id=game_id)
        if row is None or row.root_message_id is None or row.reveal_claimed_at is not None:
            return
        persisted = await _load_persisted_game(ex, league_id, game_id)
        if persisted is None:
            return
        try:
            channel = await games_channel(bot, ex, league_id)
            if bot.broadcasts.is_live_or_queued(league_id, game_id) or bot.blocks.is_closed(
                league_id, game_id
            ):
                return
            await edit_line(channel, row.root_message_id, build_matchup_line(persisted.slate))
        except Exception as exc:
            log.warning(
                "game %s: could not restore the matchup line: %s",
                game_id,
                describe_discord_failure(exc),
            )


# ---------------------------------------------------------------------------
# The startup sweep
# ---------------------------------------------------------------------------


async def sweep_league(bot: Any, pool: Executor, *, league_id: int) -> SweepReport:
    """Converge every unrevealed `game_broadcasts` row of one league (P5-97.q
    item 6, M4/M12). Called per guild from `_reconcile_guild`; idempotent, so a
    second run posts nothing new (a revealed row leaves the candidate list, and
    a reveal's claim CAS refuses a repeat).

    Per shape:

    * `INTERRUPTED_BROADCAST` (`live`/`queued`, the process died mid-broadcast):
      mark the row `interrupted` with a compare-and-swap FROM the status the
      candidate list showed (a broadcast that finished since is left alone --
      its final frame must not be overwritten); only if that swap won, edit
      the stored broadcast message to the interrupted panel, built from
      persisted data alone; then reveal. The panel edit follows the swap, so a
      crash between the two leaves an `interrupted` row that the next sweep
      finds as `UNREVEALED_ENDED`.
    * `CRASHED_REVEAL` (claimed, not posted): re-edit or re-post the result and
      mark it posted -- without claiming again.
    * `ORPHANED_PENDING` and `UNREVEALED_ENDED`: reveal (a fresh post when the
      row has no root message). An `UNREVEALED_ENDED` row whose status is
      `interrupted` first gets the interrupted panel edited in again -- the
      broadcast was stopped, and a graceful stop's own edit may not have
      landed. (It replaces a panel that did land with the persisted-data-only
      one, which says the same things minus where the replay stopped.)

    One bad row is logged and recorded, never allowed to block the rest. Rows
    this process still owns (a live/queued broadcast, an open block, a reveal
    or restore in flight) are skipped -- see the module docstring."""
    _require_no_open_transaction(pool)
    entries: list[SweepEntry] = []
    for row in await broadcast_repo.list_sweep_candidates(pool, league_id=league_id):
        shape = row.sweep_shape
        try:
            if _owned_here(bot, league_id, row.game_id):
                entries.append(SweepEntry(row.game_id, shape, None, skipped_owned=True))
                continue
            if shape is SweepShape.INTERRUPTED_BROADCAST:
                moved = await broadcast_repo.transition_status(
                    pool,
                    league_id=league_id,
                    game_id=row.game_id,
                    expected=row.status,
                    new=BroadcastStatus.INTERRUPTED,
                )
                if not moved:
                    # The list is a snapshot: the broadcast finished (or another
                    # sweep converged it) since. Whoever moved it owns the panel.
                    entries.append(SweepEntry(row.game_id, shape, None, skipped_owned=True))
                    continue
                await _edit_interrupted_message(bot, pool, row)
            elif shape is SweepShape.UNREVEALED_ENDED and row.status is BroadcastStatus.INTERRUPTED:
                # Already `interrupted`: the finalizer wrote it, but the panel edit
                # may never have landed (a graceful stop, a Discord outage, a
                # crash between the two writes). Idempotent and best-effort: a
                # panel that did land is replaced by the same facts.
                await _edit_interrupted_message(bot, pool, row)
            if _owned_here(bot, league_id, row.game_id):  # awaited since the last look
                entries.append(SweepEntry(row.game_id, shape, None, skipped_owned=True))
                continue
            outcome = await reveal_game(
                bot,
                pool,
                league_id=league_id,
                game_id=row.game_id,
                resume_crashed=row.reveal_claimed_at is not None,
            )
            entries.append(SweepEntry(row.game_id, shape, outcome))
        except Exception as exc:
            log.exception("sweep: game %s (%s) failed; continuing", row.game_id, shape)
            entries.append(
                SweepEntry(row.game_id, shape, None, error=f"{type(exc).__name__}: {exc}")
            )
    return SweepReport(league_id=league_id, entries=tuple(entries))


def _owned_here(bot: Any, league_id: int, game_id: int) -> bool:
    """A row the sweep must not touch: this process has a broadcast of the game,
    its block is open, or a reveal / restore of it is in flight."""
    return not _may_reveal(bot, league_id, game_id) or _reveal_in_flight(league_id, game_id)


async def _edit_interrupted_message(bot: Any, ex: Executor, row: GameBroadcast) -> None:
    """Replace a dead broadcast's message with `render_interrupted`, built from
    the persisted game -- no beat log survives a restart, so it says the game
    is final and where the result is, not where the replay stopped. Best
    effort: a missing message id, an archived thread or a Discord failure is
    logged and the sweep goes on to mark the row and reveal."""
    if row.thread_id is None or row.message_id is None:
        return
    try:
        persisted = await _load_persisted_game(ex, row.league_id, row.game_id)
        if persisted is None:
            return
        game = persisted.game
        if (
            game.status is not GameStatus.FINAL
            or game.home_score is None
            or game.away_score is None
        ):
            return
        by_period: dict[int, dict[int, int]] = {}
        for score in await stats_repo.list_game_period_scores(ex, game.id):
            by_period.setdefault(score.period, {})[score.team_id] = score.points
        period_scores = tuple(
            (period, teams.get(game.home_team_id, 0), teams.get(game.away_team_id, 0))
            for period, teams in sorted(by_period.items())
        )
        panel = render_interrupted(
            None,
            home=persisted.home,
            away=persisted.away,
            home_score=game.home_score,
            away_score=game.away_score,
            period_scores=period_scores,
        )
        thread = await _resolve_channel(bot, row.thread_id)
        await bounded(
            thread.get_partial_message(row.message_id).edit(
                content=None,
                embed=panel_to_embed(panel),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        )
    except Exception as exc:
        log.warning(
            "sweep: game %s: could not edit the interrupted panel: %s",
            row.game_id,
            describe_discord_failure(exc),
        )
