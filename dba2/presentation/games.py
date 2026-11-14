"""The `#games` channel's plain-text lines (docs/design/phase-5-stage-d-
broadcast.md §5, Decisions P5-97.m/.n/.v): a block's header, and one line per
game -- not yet played, being watched, or final.

**The constraint that shapes everything here: a score must be unable to reach
a line by accident.** `#games` is public and a game is only supposed to be
spoiled to the people who chose to watch it, so a result appears in the
channel only when its block has closed and nobody is still watching it
(design P5-97.m). That rule is enforced where it can be made structural:

* `SlateGame` has NO score field. The matchup, watching and header renderers
  take a `SlateGame` (or no game at all), so they have nothing to print --
  a unit test pins the field set, and a property test renders them beside a
  fixture whose score digits appear in no other input.
* `build_result_line` is the ONLY function that accepts a score, and the
  caller that supplies one (`bot/game_feed.reveal_game`, Stage D commit 9)
  is the only publisher; a unit test pins its call sites at one.

Plain text, not a `Panel`: `#games` gets one message per game so a reveal can
edit a single game's own line and a thread can hang off each message (design
B3), so no embed and no embed limit is in play.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class SlateGame:
    """One game as `#games` may describe it BEFORE its result is revealed.
    There is deliberately no score field: that absence is the spoiler rule
    (design P5-97.m). `game_date` is carried for the bot layer's `BlockGame`
    mapping (design §5's exact field set); no line here prints it, because a
    block's date already leads its header."""

    game_id: int
    home_code: str
    away_code: str
    game_date: date


def build_slate_header(block_id: str, game_count: int) -> str:
    """The one optional line above a block's per-game messages. `block_id` is
    the block's date as Stage E names it (design P5-97.o), shown verbatim."""
    noun = "game" if game_count == 1 else "games"
    return (
        f"**{block_id}** — {game_count} {noun} played. Use `/watch <game id>` to follow one "
        f"in its own thread; results are posted here once the block closes."
    )


def _matchup(game: SlateGame) -> str:
    return f"Game {game.game_id} · {game.away_code} @ {game.home_code}"


def build_matchup_line(game: SlateGame) -> str:
    return f"{_matchup(game)} — not yet played"


def build_watching_line(game: SlateGame) -> str:
    return f"{_matchup(game)} — watching now"


def build_result_line(game: SlateGame, *, home_score: int, away_score: int) -> str:
    """The only renderer that accepts a score. Its one caller reveals a game
    whose block is closed and which nobody is still broadcasting (design
    P5-97.m), so reaching this function IS the decision to publish."""
    return (
        f"Game {game.game_id} · {game.away_code} {away_score} @ {game.home_code} {home_score} "
        f"— final"
    )
