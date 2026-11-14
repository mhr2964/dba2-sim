"""The reveal's bounded in-call retry (Stage D fix round, finding F3).

A reveal that failed once used to be stranded: `close_block` / `on_ended` call
`reveal_game` once, the row stayed claimed-not-posted, and only a restart or a
gateway reconnect (the sweep) ever completed it. A single 5xx from Discord or a
blip on the database write is the ordinary failure, so `reveal_game` now retries
the Discord edit (or fresh post) and the `mark_reveal_posted` write a bounded
number of times with a short backoff (`game_feed._retry_sleep`, injectable).

What is NOT retried, and why: `Forbidden` / `NotFound` (permanent for this
call: a missing permission, a deleted message -- the latter is handled by a
fresh post), `GamesChannelUnavailable`, and `TimeoutError` (the call already
cost `DISCORD_CALL_TIMEOUT_SECONDS`; with Discord unresponsive a 16-game
`close_block` would otherwise block the producer for minutes, and an edit that
timed out may have landed).
"""

from __future__ import annotations

import pytest

from dba2.bot import game_feed
from dba2.data.repositories import broadcast_repo
from tests.unit.bot._game_feed_fakes import (
    LEAGUE,
    SCORES,
    FakeBot,
    FakeChannel,
    Store,
    forbidden,
    http_error,
    not_found,
    open_in_memory,
    variant,
)
from tests.unit.bot.test_game_feed import EX, ROOT, World, result_line

Outcome = game_feed.RevealOutcome


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Record the backoff instead of waiting for it."""
    waited: list[float] = []

    async def record(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(game_feed, "_retry_sleep", record, raising=False)
    return waited


@pytest.fixture
def world(monkeypatch, sleeps) -> World:
    games = FakeChannel()
    thread = FakeChannel(7001, first_message_id=8000)
    store = Store().install(monkeypatch)
    for game_id in SCORES:
        store.add_game(game_id)
    return World(FakeBot(games, thread), games, thread, store)


def _reveal(world: World, game_id: int = 1):
    return game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=game_id)


def _fail_edits(world: World, count: int, exc=None) -> None:
    for n in range(count):
        world.games.fail_edit[n] = exc or http_error("discord 500")


async def test_a_reveal_whose_edit_fails_twice_is_completed_by_the_third_attempt(world, sleeps):
    world.store.add_row(1, root_message_id=ROOT)
    _fail_edits(world, 2)
    assert await _reveal(world) is Outcome.REVEALED
    assert len(world.games.ops("edit")) == 3
    assert world.games.content[ROOT] == result_line(1)
    assert world.store.row(1).reveal_posted_at is not None
    assert len(world.store.called("claim_reveal")) == 1, "one claim, however many attempts"
    assert sleeps == [0.5, 1.0], "a short, growing backoff between attempts"


async def test_a_reveal_whose_edit_keeps_failing_gives_up_after_three_and_stays_recoverable(
    world, sleeps
):
    world.store.add_row(1, root_message_id=ROOT)
    _fail_edits(world, 3)
    assert await _reveal(world) is Outcome.POST_FAILED
    assert len(world.games.ops("edit")) == 3 and len(sleeps) == 2
    row = world.store.row(1)
    assert row.reveal_claimed_at is not None and row.reveal_posted_at is None
    # ... and the sweep completes it, as before, without claiming again
    report = await game_feed.sweep_league(world.bot, EX, league_id=LEAGUE)
    assert [e.reveal for e in report.entries] == [Outcome.REVEALED]
    assert world.games.content[ROOT] == result_line(1)


async def test_a_missing_permission_is_not_retried(world, sleeps):
    world.store.add_row(1, root_message_id=ROOT)
    world.games.edit_errors[ROOT] = forbidden()
    assert await _reveal(world) is Outcome.POST_FAILED
    assert len(world.games.ops("edit")) == 1 and sleeps == []


async def test_a_timeout_is_not_retried(world, sleeps):
    world.store.add_row(1, root_message_id=ROOT)
    world.games.edit_errors[ROOT] = TimeoutError()
    assert await _reveal(world) is Outcome.POST_FAILED
    assert len(world.games.ops("edit")) == 1 and sleeps == []


async def test_a_deleted_root_message_is_replaced_without_retrying_the_edit(world, sleeps):
    world.store.add_row(1, root_message_id=ROOT)
    world.games.edit_errors[ROOT] = not_found()
    assert await _reveal(world) is Outcome.REVEALED
    assert len(world.games.ops("edit")) == 1 and len(world.games.ops("send")) == 1 and sleeps == []


async def test_a_fresh_post_that_fails_twice_is_retried_and_posts_once(world, sleeps):
    world.store.add_row(1, root_message_id=None)
    world.games.fail_send[0] = http_error()
    world.games.fail_send[1] = http_error()
    assert await _reveal(world) is Outcome.REVEALED
    assert len(world.games.ops("send")) == 3
    assert [m for m in world.games.content.values() if m == result_line(1)] == [result_line(1)]
    assert world.store.row(1).root_message_id == 5000, "the message that landed is the root"


async def test_a_fresh_post_that_times_out_is_not_retried_it_may_have_landed(world, sleeps):
    world.store.add_row(1, root_message_id=None)
    world.games.fail_send[0] = TimeoutError()
    assert await _reveal(world) is Outcome.POST_FAILED
    assert len(world.games.ops("send")) == 1 and sleeps == []


async def test_a_mark_that_fails_twice_is_retried_and_the_line_is_not_re_edited(
    world, sleeps, monkeypatch
):
    world.store.add_row(1, root_message_id=ROOT)
    real = world.store.mark_reveal_posted
    attempts = {"n": 0}

    async def flaky(ex, *, league_id, broadcast_id):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise RuntimeError("db blip")
        return await real(ex, league_id=league_id, broadcast_id=broadcast_id)

    monkeypatch.setattr(broadcast_repo, "mark_reveal_posted", flaky)
    assert await _reveal(world) is Outcome.REVEALED
    assert attempts["n"] == 3 and sleeps == [0.5, 1.0]
    assert len(world.games.ops("edit")) == 1, "only the write is retried, never the public edit"
    assert world.store.row(1).reveal_posted_at is not None


async def test_a_mark_that_keeps_failing_is_reported_and_left_for_the_sweep(world, sleeps):
    world.store.add_row(1, root_message_id=ROOT)
    world.store.raises["mark_reveal_posted"] = RuntimeError("db down")
    assert await _reveal(world) is Outcome.MARK_FAILED
    assert len(world.store.called("mark_reveal_posted")) == 3
    assert len(world.games.ops("edit")) == 1 and world.games.content[ROOT] == result_line(1)
    del world.store.raises["mark_reveal_posted"]
    await game_feed.sweep_league(world.bot, EX, league_id=LEAGUE)
    assert world.store.row(1).reveal_posted_at is not None


async def test_close_block_completes_a_reveal_that_failed_once(world, sleeps):
    """The scenario the finding names: before, this game stayed claimed-not-posted
    until a restart."""
    world.store.add_row(1, root_message_id=ROOT)
    open_in_memory(world.bot, 1)
    _fail_edits(world, 1)
    report = await game_feed.close_block(world.bot, EX, league_id=LEAGUE)
    assert report.outcomes == {1: Outcome.REVEALED}
    assert world.store.row(1).reveal_posted_at is not None


# --- mutation twins: each retry rule undone must fail its scenario ---------------


async def scenario_an_edit_that_fails_twice_is_completed(module, world: World) -> None:
    world.store.add_row(1, root_message_id=ROOT)
    _fail_edits(world, 2)
    outcome = await module.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert outcome == Outcome.REVEALED, f"not retried: {outcome}"


async def scenario_a_mark_that_fails_twice_is_completed(module, world: World) -> None:
    world.store.add_row(1, root_message_id=ROOT)
    calls = {"n": 0}
    real = world.store.mark_reveal_posted

    async def flaky(ex, *, league_id, broadcast_id):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("db blip")
        return await real(ex, league_id=league_id, broadcast_id=broadcast_id)

    broadcast_repo.mark_reveal_posted = flaky  # restored by `Store.install`'s monkeypatch undo
    outcome = await module.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert outcome == Outcome.REVEALED, f"the write was not retried: {outcome}"


async def scenario_a_timeout_is_not_retried(module, world: World) -> None:
    world.store.add_row(1, root_message_id=ROOT)
    world.games.edit_errors[ROOT] = TimeoutError()
    await module.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert len(world.games.ops("edit")) == 1, "a timed-out edit was retried"


_ATTEMPTS = "REVEAL_ATTEMPTS = 3\n"
_ANY_FAILURE = "                    retry_if=_any_failure,\n"
_TIMEOUT_GUARD = "    if isinstance(exc, (TimeoutError, discord.Forbidden, discord.NotFound)):\n"
_TWINS = [
    (scenario_an_edit_that_fails_twice_is_completed, [(_ATTEMPTS, "REVEAL_ATTEMPTS = 1\n")]),
    (
        scenario_a_mark_that_fails_twice_is_completed,
        [(_ANY_FAILURE, "                    retry_if=lambda exc: False,\n")],
    ),
    (
        scenario_a_timeout_is_not_retried,
        [(_TIMEOUT_GUARD, "    if isinstance(exc, (discord.Forbidden, discord.NotFound)):\n")],
    ),
]
_TWIN_IDS = ["single-attempt", "mark-not-retried", "timeouts-retried"]


def _twin_world(monkeypatch, module) -> World:
    async def instant(_seconds: float) -> None:
        return None

    module._retry_sleep = instant
    games = FakeChannel()
    store = Store().install(monkeypatch)
    for game_id in SCORES:
        store.add_game(game_id)
    return World(FakeBot(games), games, FakeChannel(7001), store)


@pytest.mark.parametrize(("scenario", "mutations"), _TWINS, ids=_TWIN_IDS)
async def test_twin_control_the_retry_scenario_passes_on_the_real_and_an_unmutated_copy(
    scenario, mutations, monkeypatch
):
    for module in (game_feed, variant()):
        await scenario(module, _twin_world(monkeypatch, module))


@pytest.mark.parametrize(("scenario", "mutations"), _TWINS, ids=_TWIN_IDS)
async def test_twin_with_the_retry_rule_undone_the_scenario_goes_red(
    scenario, mutations, monkeypatch
):
    mutated = variant(*mutations)
    with pytest.raises(AssertionError):
        await scenario(mutated, _twin_world(monkeypatch, mutated))
