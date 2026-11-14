"""`open_block`'s ordering, re-open and header rules (Stage D fix round).

* A block is registered only after its rows exist: the docstring said "a block
  whose rows do not exist is not open", but registration ran first, so a
  database error left a registered block whose games had no `game_broadcasts`
  row (and `/watch` would then admit a game the reveal path knew nothing of).
* Re-opening the SAME block id with a shorter `games` list used to drop the
  omitted games from the registry wholesale, which made them "closed" -- and so
  revealable while their block was still open. It now MERGES: a game already in
  the block stays until the block closes.
* The slate header is posted once per block: a re-open after every game post
  failed (the header had gone out) posted a second header.
"""

from __future__ import annotations

import pytest

from dba2.bot import game_feed
from tests.unit.bot._game_feed_fakes import (
    LEAGUE,
    SCORES,
    FakeBot,
    FakeChannel,
    Store,
    block_game,
    http_error,
)
from tests.unit.bot.test_game_feed import EX, World, matchup


@pytest.fixture
def world(monkeypatch) -> World:
    games = FakeChannel()
    thread = FakeChannel(7001, first_message_id=8000)
    store = Store().install(monkeypatch)
    for game_id in SCORES:
        store.add_game(game_id)
    return World(FakeBot(games, thread), games, thread, store)


def _open(world: World, *game_ids: int, block_id: str = "2026-11-01"):
    return game_feed.open_block(
        world.bot,
        EX,
        league_id=LEAGUE,
        season=2026,
        block_id=block_id,
        games=[block_game(g) for g in game_ids],
    )


# --- rows first, then the block ---------------------------------------------------


async def test_a_database_error_creating_the_rows_leaves_no_registered_block(world):
    world.store.raises["create_pending_broadcasts"] = RuntimeError("db down")
    with pytest.raises(RuntimeError, match="db down"):
        await _open(world, 1, 2)
    assert world.bot.blocks.current(LEAGUE) is None, "a block without rows must not be open"
    assert world.games.calls == [], "and nothing was posted"


async def test_the_block_is_not_registered_until_its_rows_exist(world):
    seen: list[object] = []

    async def look(n, kwargs):
        seen.append(world.bot.blocks.current(LEAGUE))

    world.store.hooks["create_pending_broadcasts"] = look
    await _open(world, 1)
    assert seen == [None], "registered before the rows existed"
    assert world.bot.blocks.current(LEAGUE) is not None


async def test_a_previous_block_is_still_closed_before_the_new_rows_are_written(world):
    await _open(world, 1, block_id="2026-11-01")
    world.store.raises["create_pending_broadcasts"] = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        await _open(world, 2, block_id="2026-11-02")
    assert world.bot.blocks.current(LEAGUE) is None
    assert world.store.row(1).reveal_posted_at is not None, "the previous block's reveal happened"


# --- re-opening the same block merges ------------------------------------------------


async def test_reopening_the_same_block_with_fewer_games_keeps_the_rest_registered(world):
    await _open(world, 1, 2, 3)
    report = await _open(world, 1)
    block = world.bot.blocks.current(LEAGUE)
    assert block is not None and set(block.games) == {1, 2, 3}
    assert report.registered == 3
    for game_id in (1, 2, 3):
        assert not world.bot.blocks.is_closed(LEAGUE, game_id)
    assert world.leaked_scores() == []


async def test_reopening_the_same_block_with_a_new_game_adds_it(world):
    await _open(world, 1, 2)
    report = await _open(world, 2, 3)
    block = world.bot.blocks.current(LEAGUE)
    assert block is not None and set(block.games) == {1, 2, 3}
    assert (report.registered, report.rows_created) == (3, 1)
    assert report.already_posted == (2,) and report.posted == (3,)


async def test_a_different_block_id_still_replaces_the_previous_block(world):
    await _open(world, 1, 2, block_id="2026-11-01")
    await _open(world, 3, block_id="2026-11-02")
    block = world.bot.blocks.current(LEAGUE)
    assert block is not None and block.block_id == "2026-11-02" and set(block.games) == {3}


# --- one header per block --------------------------------------------------------------


async def test_a_reopen_after_every_game_post_failed_does_not_post_a_second_header(world):
    world.games.fail_send[1] = http_error()
    world.games.fail_send[2] = http_error()
    first = await _open(world, 1, 2)
    assert first.header_posted and [f.game_id for f in first.failed] == [1, 2]
    second = await _open(world, 1, 2)
    assert second.header_posted is False and second.posted == (1, 2)
    headers = [
        kw["content"]
        for _, kw in world.games.ops("send")
        if kw["content"] == game_feed.build_slate_header("2026-11-01", 2)
    ]
    assert len(headers) == 1, "the slate header was posted twice"
    assert all(world.store.row(g).root_message_id is not None for g in (1, 2))
    assert matchup(2) in world.games.content.values()


async def test_a_failed_header_is_not_re_added_once_a_game_has_a_line(world):
    world.games.fail_send[0] = http_error()  # the header itself
    first = await _open(world, 1)
    assert first.header_posted is False and first.posted == (1,)
    second = await _open(world, 1)
    assert second.header_posted is False, "a game already has a line: the header is not re-added"


async def test_a_header_that_failed_with_no_game_posted_is_retried(world):
    world.games.fail_send[0] = http_error()
    world.games.fail_send[1] = http_error()
    first = await _open(world, 1)
    assert first.header_posted is False and first.posted == ()
    second = await _open(world, 1)
    assert second.header_posted is True and second.posted == (1,)
