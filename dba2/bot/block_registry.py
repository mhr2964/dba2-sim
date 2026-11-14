"""The in-memory hold on the block of games most recently simmed for a league
(docs/design/phase-5-stage-d-broadcast.md §5, Decisions P5-97.o/.m/.n).

**Why this exists.** Beat logs are never persisted (P5-42), so a `/watch`
replay can only be built from the `GameResult` the producer simmed a moment
earlier. This registry is where that result waits, and where the reveal rule
("a game's score appears in `#games` only once its block is closed") reads its
"is my block closed?" answer.

**Bounded on purpose, two ways.**

* At most ONE open block per league. `register_block` drops that league's
  previous block wholesale. There is no LRU, no TTL and no sweeper: the only
  two things that end a block are the next block opening and the process
  dying, and a TTL would be a second, invisible way for a replay to vanish.
* At most `MAX_RESULTS_PER_BLOCK` games per block keep a live `GameResult`;
  the rest stay registered with `result=None` ("cap-degraded", not
  watchable). 969 `BeatRecord`s/game x 16 games x ~300 bytes is ~5 MB at the
  cap; a 550-game reading would otherwise hold ~160 MB.

**Eviction never orphans a running broadcast (B1).** A broadcast task
captures its plan, its inputs and its `BlockGame` BY VALUE when it starts and
afterwards asks the registry only `is_closed(...)`. Dropping a block flips
that answer to `True`, which is exactly what the reveal rule wants, and can
never pull data out from under a task.

**No locks, no I/O, no awaits.** Every method is synchronous and does only
in-memory dict work, and the bot runs on one asyncio event loop, so no other
coroutine can interleave inside a method: each call is atomic with respect to
every other. That single-threaded-loop fact is the entire concurrency
argument; if a method ever gains an `await` or is called from another thread,
this argument stops holding and a lock becomes necessary. There is no module
singleton and no `discord`/DB import: one instance hangs off the bot
(`bot.blocks`, wired in Stage D commit 9) so tests get a fresh one and two
bots never share state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date

from dba2.sim.events import GameResult


@dataclass(frozen=True)
class BlockGame:
    """One game of a block, as the producer hands it over. `result` is `None`
    when the game is cap-degraded (see `BlockRegistry.MAX_RESULTS_PER_BLOCK`)
    and so not watchable (design P5-97.p). The producer may also pass `None`
    itself; such a game simply never occupies a result slot."""

    game_id: int
    home_team_id: int
    away_team_id: int
    home_code: str
    away_code: str
    game_date: date
    result: GameResult | None


@dataclass
class Block:
    """A league's currently open block. `block_id` is the block's game-date
    isoformat, from Stage E's block bound (P5-52). `games` is insertion-ordered
    and keyed by `game_id`."""

    block_id: str
    league_id: int
    season: int
    games: dict[int, BlockGame]
    header_posted: bool = False
    """Set by `game_feed.open_block` once this block's slate header is in `#games`,
    so a re-open of the same block does not post a second one."""


class BlockRegistry:
    """Per-league holder of at most one open `Block`. Synchronous and
    single-event-loop-only; see the module docstring for why that needs no
    locking."""

    MAX_RESULTS_PER_BLOCK = 16

    def __init__(self) -> None:
        self._open: dict[int, Block] = {}

    def register_block(
        self,
        *,
        league_id: int,
        block_id: str,
        season: int,
        games: Sequence[BlockGame],
    ) -> Block:
        """Make this league's open block `games`, dropping any previous one
        wholesale. The first `MAX_RESULTS_PER_BLOCK` games that carry a result
        (in the order given) keep it; every later game is registered with
        `result=None`. Raises `ValueError` on a repeated `game_id` rather than
        letting the second silently replace the first. The caller's
        `BlockGame` objects are never mutated (they are frozen); degraded
        games are stored as copies."""
        held: dict[int, BlockGame] = {}
        results_kept = 0
        for game in games:
            if game.game_id in held:
                raise ValueError(f"game {game.game_id} appears twice in block {block_id!r}")
            if game.result is not None:
                if results_kept < self.MAX_RESULTS_PER_BLOCK:
                    results_kept += 1
                else:
                    game = replace(game, result=None)
            held[game.game_id] = game
        block = Block(block_id=block_id, league_id=league_id, season=season, games=held)
        self._open[league_id] = block
        return block

    def current(self, league_id: int) -> Block | None:
        """The league's open block, or `None`."""
        return self._open.get(league_id)

    def get(self, league_id: int, game_id: int) -> BlockGame | None:
        """The game as registered in THIS league's open block, or `None` when
        there is no open block or it does not hold the game. Never consults
        another league's block, so a game id never crosses leagues."""
        block = self._open.get(league_id)
        if block is None:
            return None
        return block.games.get(game_id)

    def close(self, league_id: int) -> Block | None:
        """Pop and return the league's open block (`None` if none). The caller
        does the I/O -- revealing the games that are not still being
        broadcast (design P5-97.m) -- from the returned block."""
        return self._open.pop(league_id, None)

    def is_closed(self, league_id: int, game_id: int) -> bool:
        """The reveal rule's "is this game's block closed?" question (design
        P5-97.m): `True` when no open block of this league holds the game.
        That deliberately includes a league with no block at all, a game that
        was never registered, and a block that has since been replaced or
        closed -- after a restart memory is empty, so every block is closed
        and every unrevealed game is safe to reveal."""
        block = self._open.get(league_id)
        return block is None or game_id not in block.games
