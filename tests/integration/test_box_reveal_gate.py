"""`/box`'s reveal gate (phase-5-stage-e-advance.md P5-99.n, build-plan row 16),
against real rows: `/box <game_id>` may not show a score the league has not posted.

`/box` is the ONE gated surface, by the user's scope decision (§8 call 1): a person
who types `/box 412` is asking for one specific score, while `/standings` and the other
aggregates stay public, because gating them would make the standings lag one press
behind always. So this file proves four things about the real command callback:

- a game whose `game_broadcasts` row exists and is unrevealed is REFUSED, ephemerally,
  in a sentence that says it is not yet revealed and how it gets revealed -- and the
  refusal carries no score and no embed;
- a game whose reveal was POSTED answers with its box score, as before;
- a game with no `game_broadcasts` row (in no block, so nothing holds its score back)
  answers with its box score, as before;
- `/standings` answers while a game is unrevealed: the aggregates are untouched.

And one structural thing: the cog asks `game_feed.box_score_is_public`, the shared
predicate that sits beside `_may_reveal`, and defines "revealed" nowhere itself -- a
second private definition would let the two drift, which is the failure this design
exists to prevent. Proved by behaviour (a patched predicate changes the answer) and by
reading the cog's source.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import ast
import random
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dba2.bot import game_feed
from dba2.bot.cogs import read as read_cog
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import broadcast_repo, league_repo
from tests.integration._press_fixtures import COMMISSIONER, SEASON, add_played_game, drop


class _Response:
    def __init__(self) -> None:
        self._done = False
        self.sent: list[dict] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, *, embed=None, ephemeral=False, **_kwargs) -> None:
        self._done = True
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class _Followup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, *, embed=None, ephemeral=False, **_kwargs) -> None:
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class _Interaction:
    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=COMMISSIONER)
        self.response = _Response()
        self.followup = _Followup()

    def replies(self) -> list[dict]:
        return [*self.response.sent, *self.followup.sent]


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


class _World:
    """One league with three FINAL games: `unrevealed` and `revealed` each have a
    `game_broadcasts` row (the second with its reveal posted), `unblocked` has none."""

    def __init__(self, guild_id: int, unrevealed: int, revealed: int, unblocked: int) -> None:
        self.guild_id = guild_id
        self.unrevealed = unrevealed
        self.revealed = revealed
        self.unblocked = unblocked


async def _build(pool) -> tuple[int, _World]:
    guild_id = random.randint(10_000_000, 2_000_000_000)
    league = await league_repo.create_league(
        pool,
        discord_guild_id=guild_id,
        name="Box Gate Test League",
        start_season_year=SEASON,
        current_season=SEASON,
        commissioner_user_id=COMMISSIONER,
        rng_seed=1,
    )
    first = await add_played_game(pool, league.id, game_index=0)
    teams = (first.home_team_id, first.away_team_id)
    second = await add_played_game(pool, league.id, teams, game_index=1)
    third = await add_played_game(pool, league.id, teams, game_index=2)
    await broadcast_repo.create_pending_broadcasts(
        pool, league_id=league.id, block_id="b1", game_ids=[first.id, second.id]
    )
    claimed = await broadcast_repo.claim_reveal(pool, league_id=league.id, game_id=second.id)
    assert claimed is not None
    await broadcast_repo.mark_reveal_posted(pool, league_id=league.id, broadcast_id=claimed)
    return league.id, _World(guild_id, first.id, second.id, third.id)


async def _box(world: _World, game_id: int) -> _Interaction:
    interaction = _Interaction(world.guild_id)
    cog = read_cog.ReadCommands(bot=None)  # type: ignore[arg-type]
    await read_cog.ReadCommands.box.callback(cog, interaction, game_id)
    return interaction


async def test_box_refuses_an_unrevealed_game_ephemerally_and_says_how_it_gets_revealed(pool):
    league_id, world = await _build(pool)
    try:
        interaction = await _box(world, world.unrevealed)

        (reply,) = interaction.replies()
        assert reply["embed"] is None, "an unrevealed game's box score must not be sent"
        assert reply["ephemeral"] is True
        text = reply["content"]
        assert f"{world.unrevealed}" in text
        assert "hasn't been revealed yet" in text
        # It names the actual reveal path, in the /watch code's own terms: the block of games
        # closing on the next /advance that plays a new block, and nobody still watching.
        assert "/advance" in text and "#games" in text
        without_id = re.sub(rf"\b{world.unrevealed}\b", "", text)
        assert not re.search(r"\b(100|90)\b", without_id), "the refusal leaked the score"
    finally:
        await drop(pool, league_id)


async def test_box_answers_for_a_game_whose_reveal_was_posted(pool):
    league_id, world = await _build(pool)
    try:
        interaction = await _box(world, world.revealed)

        (reply,) = interaction.replies()
        assert reply["embed"] is not None
        assert reply["ephemeral"] is False
    finally:
        await drop(pool, league_id)


async def test_box_answers_for_a_game_with_no_broadcast_row(pool):
    league_id, world = await _build(pool)
    try:
        interaction = await _box(world, world.unblocked)

        (reply,) = interaction.replies()
        assert reply["embed"] is not None
        assert reply["ephemeral"] is False
    finally:
        await drop(pool, league_id)


async def test_a_claimed_but_unposted_reveal_is_still_unrevealed(pool):
    """A crashed reveal (claimed, never posted) has not made the score public, so /box
    keeps refusing until `reveal_posted_at` is set -- the same basis `/watch` uses."""
    league_id, world = await _build(pool)
    try:
        claimed = await broadcast_repo.claim_reveal(
            pool, league_id=league_id, game_id=world.unrevealed
        )
        assert claimed is not None
        interaction = await _box(world, world.unrevealed)

        (reply,) = interaction.replies()
        assert reply["embed"] is None and reply["ephemeral"] is True
    finally:
        await drop(pool, league_id)


async def test_standings_answers_while_a_game_is_unrevealed(pool):
    league_id, world = await _build(pool)
    try:
        interaction = _Interaction(world.guild_id)
        cog = read_cog.ReadCommands(bot=None)  # type: ignore[arg-type]
        await read_cog.ReadCommands.standings.callback(cog, interaction, None)

        (reply,) = interaction.replies()
        assert reply["embed"] is not None, "/standings is public: it must not be gated on reveal"
    finally:
        await drop(pool, league_id)


async def test_the_cog_asks_the_shared_predicate_and_nothing_else_decides_reveal(pool):
    """A patched predicate changes /box's answer in BOTH directions, so the cog's decision
    is the predicate's and not a private copy of it."""
    league_id, world = await _build(pool)
    try:
        with patch.object(game_feed, "box_score_is_public", return_value=False):
            hidden = await _box(world, world.revealed)
        with patch.object(game_feed, "box_score_is_public", return_value=True):
            shown = await _box(world, world.unrevealed)

        (hidden_reply,) = hidden.replies()
        (shown_reply,) = shown.replies()
        assert hidden_reply["embed"] is None and hidden_reply["ephemeral"] is True
        assert shown_reply["embed"] is not None
    finally:
        await drop(pool, league_id)


def test_the_read_cog_holds_no_definition_of_revealed_of_its_own():
    """Source-level half of the pin: `read.py` names none of the columns or properties that
    define "revealed", and its `box` calls `box_score_is_public`."""
    source = Path(read_cog.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attributes & {"reveal_posted_at", "reveal_claimed_at", "is_revealed"}
    box = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "box")
    called = {
        n.func.attr
        for n in ast.walk(box)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "box_score_is_public" in called
