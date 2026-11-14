"""`game_feed`'s open / reveal / close / on_ended half (Stage D commit 9,
docs/design/phase-5-stage-d-broadcast.md §5, P5-97.m/.n, §10 Tier 2). The
startup sweep is `test_game_feed_sweep.py`; the static caller-set pins are
`test_game_feed_pins.py`.

No DB, no gateway: the repositories are `Store` (an in-memory model with the
real CAS semantics), `#games` is a `FakeChannel` that records every send and
edit, the block registry is the REAL `BlockRegistry`. What this file pins is the
B1/B2 truth table (a score appears only when the block is closed AND nothing is
broadcasting the game), the two-phase reveal, `open_block`'s idempotence and its
degradation on a failed post, and the mutation twins that show those rules have
teeth (each scenario is run on the real module, then on a private copy with one
rule deleted, where it must go red).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from dba2.bot import game_feed
from dba2.bot.broadcast import BroadcastScheduler, BroadcastSettings
from dba2.data.repositories import broadcast_repo
from dba2.domain.broadcast import BroadcastStatus
from dba2.domain.game import GameStatus
from tests.unit.bot._broadcast_fakes import sim_result
from tests.unit.bot._game_feed_fakes import (
    LEAGUE,
    SCORES,
    FakeBot,
    FakeChannel,
    MutationAnchorError,
    Store,
    StubScheduler,
    assert_no_mentions,
    block_game,
    forbidden,
    http_error,
    not_found,
    open_in_memory,
    variant,
)

EX = object()  # the fake executor: the fake repositories ignore it
ROOT = 5001
FINISHED, INTERRUPTED = BroadcastStatus.FINISHED, BroadcastStatus.INTERRUPTED
Outcome = game_feed.RevealOutcome


def matchup(game_id: int) -> str:
    return f"Game {game_id} · AWY @ HOM — not yet played"


def result_line(game_id: int) -> str:
    home, away = SCORES[game_id]
    return f"Game {game_id} · AWY {away} @ HOM {home} — final"


@dataclass
class World:
    bot: FakeBot
    games: FakeChannel
    thread: FakeChannel
    store: Store

    @property
    def scheduler(self) -> StubScheduler:
        return self.bot.broadcasts

    def leaked_scores(self) -> list[str]:
        """Every score digit-string that appears anywhere `#games` was written."""
        text = self.games.all_text()
        return [str(n) for pair in SCORES.values() for n in pair if str(n) in text]


@pytest.fixture
def world(monkeypatch) -> World:
    games = FakeChannel()
    thread = FakeChannel(7001, first_message_id=8000)
    store = Store().install(monkeypatch)
    for game_id in SCORES:
        store.add_game(game_id)
    return World(FakeBot(games, thread), games, thread, store)


def _ended(world: World, game_id: int, status: BroadcastStatus = FINISHED, module=game_feed):
    return module.on_ended(world.bot, league_id=LEAGUE, game_id=game_id, status=status, executor=EX)


# --- the B1/B2 truth table: block open|closed x broadcast none|live|queued|finished|interrupted

BROADCASTS = ["none", "live", "queued", "finished", "interrupted"]


def _seed_row(world: World, game_id: int, broadcast: str) -> None:
    status = {
        "none": BroadcastStatus.PENDING,
        "live": BroadcastStatus.LIVE,
        "queued": BroadcastStatus.QUEUED,
        "finished": FINISHED,
        "interrupted": INTERRUPTED,
    }[broadcast]
    threaded = broadcast != "none"
    world.store.add_row(
        game_id,
        status=status,
        root_message_id=ROOT,
        thread_id=7001 if threaded else None,
        message_id=8001 if threaded else None,
    )
    if broadcast in ("live", "queued"):
        world.scheduler.live.add((LEAGUE, game_id))


@pytest.mark.parametrize("broadcast", BROADCASTS)
async def test_with_the_block_open_nothing_reveals_whatever_the_broadcast_did(world, broadcast):
    _seed_row(world, 1, broadcast)
    open_in_memory(world.bot, 1)
    if broadcast in ("finished", "interrupted"):
        await _ended(world, 1, INTERRUPTED if broadcast == "interrupted" else FINISHED)
        assert world.games.content[ROOT] == matchup(1), (
            "the root line goes back to 'not yet played'"
        )
    await game_feed.sweep_league(world.bot, EX, league_id=LEAGUE)  # a reconnect's on_ready
    row = world.store.row(1)
    assert row.reveal_claimed_at is None and row.reveal_posted_at is None
    assert world.leaked_scores() == []
    assert world.thread.calls == [], "the sweep must not touch a broadcast this process owns"


@pytest.mark.parametrize("broadcast", BROADCASTS)
async def test_with_the_block_closed_a_game_reveals_only_once_nothing_broadcasts_it(
    world, broadcast
):
    _seed_row(world, 1, broadcast)
    open_in_memory(world.bot, 1)
    report = await game_feed.close_block(world.bot, EX, league_id=LEAGUE)
    if broadcast in ("live", "queued"):
        assert report.still_broadcasting == (1,) and report.outcomes == {}
        assert world.store.row(1).reveal_claimed_at is None
        assert world.leaked_scores() == []
        world.scheduler.live.discard((LEAGUE, 1))  # the broadcast ends, block already closed
        await _ended(world, 1)
    else:
        assert report.still_broadcasting == () and report.outcomes == {1: Outcome.REVEALED}
    assert world.games.content[ROOT] == result_line(1)
    assert world.store.row(1).reveal_posted_at is not None
    assert [kw["content"] for _, kw in world.games.ops("edit")] == [result_line(1)]


@pytest.mark.parametrize("status", [FINISHED, INTERRUPTED])
async def test_an_interrupted_broadcast_follows_the_same_reveal_rule_as_a_finished_one(
    world, status
):
    world.store.add_row(1, status=status, root_message_id=ROOT)
    open_in_memory(world.bot, 1)
    await _ended(world, 1, status)
    assert world.store.row(1).reveal_claimed_at is None
    world.bot.blocks.close(LEAGUE)
    await _ended(world, 1, status)
    assert world.games.content[ROOT] == result_line(1)


async def test_a_close_racing_the_restore_edit_still_reveals(world, monkeypatch):
    """`close_block` skipped the game as still broadcasting, so if it closes the
    block while `on_ended` is restoring the matchup line, nothing else would
    ever reveal it -- `on_ended` asks again after the edit."""
    world.store.add_row(1, status=FINISHED, root_message_id=ROOT)
    open_in_memory(world.bot, 1)
    real_edit = game_feed.edit_line

    async def edit_then_close(channel, message_id, content):
        await real_edit(channel, message_id, content)
        world.bot.blocks.close(LEAGUE)

    monkeypatch.setattr(game_feed, "edit_line", edit_then_close)
    await _ended(world, 1)
    assert world.games.content[ROOT] == result_line(1)


async def test_close_block_uses_the_real_schedulers_live_or_queued_answer(monkeypatch):
    games = FakeChannel()

    async def never(**_kwargs):
        raise AssertionError("no broadcast runs in this test")

    scheduler = BroadcastScheduler(BroadcastSettings(), on_ended=never)
    bot = FakeBot(games, scheduler=scheduler)
    store = Store().install(monkeypatch)
    for game_id in (1, 2):
        store.add_game(game_id)
        store.add_row(game_id, root_message_id=ROOT + game_id)
    open_in_memory(bot, 1, 2)
    assert scheduler.admit(league_id=LEAGUE, game_id=2, user_id=10).value == "admitted"
    report = await game_feed.close_block(bot, EX, league_id=LEAGUE)
    assert report.still_broadcasting == (2,) and set(report.outcomes) == {1}
    assert store.row(2).reveal_claimed_at is None


async def test_closing_a_league_with_no_open_block_does_nothing(world):
    report = await game_feed.close_block(world.bot, EX, league_id=LEAGUE)
    assert report.block_id is None and report.outcomes == {} and world.games.calls == []


# --- open_block --------------------------------------------------------------


async def _open(world: World, *game_ids: int, block_id: str = "2026-11-01"):
    return await game_feed.open_block(
        world.bot,
        EX,
        league_id=LEAGUE,
        season=2026,
        block_id=block_id,
        games=[block_game(g) for g in game_ids],
    )


async def test_open_block_registers_creates_rows_and_posts_a_header_and_a_root_per_game(world):
    report = await _open(world, 1, 2, 3)
    assert (report.registered, report.rows_created, report.header_posted) == (3, 3, True)
    assert report.posted == (1, 2, 3) and report.failed == () and report.previous_block is None
    sends = [kw["content"] for _, kw in world.games.ops("send")]
    assert sends == [
        game_feed.build_slate_header("2026-11-01", 3),
        matchup(1),
        matchup(2),
        matchup(3),
    ]
    for game_id, message_id in zip((1, 2, 3), (5001, 5002, 5003), strict=True):
        row = world.store.row(game_id)
        assert row.status is BroadcastStatus.PENDING and row.root_message_id == message_id
        assert row.reveal_claimed_at is None
    assert world.bot.blocks.current(LEAGUE).block_id == "2026-11-01"
    assert world.leaked_scores() == []


async def test_open_block_persists_each_root_id_right_after_its_own_post(world, monkeypatch):
    """Not one transaction around N posts (P5-97.v): game 1's id is written
    before game 2 is posted, so a crash mid-slate keeps what was already posted."""
    order: list[str] = []
    real_send = world.games.send
    real_set = world.store.set_root_message

    async def send(**kwargs):
        order.append("send:" + kwargs["content"][:6])
        return await real_send(**kwargs)

    async def set_root(ex, **kwargs):
        order.append(f"persist:{kwargs['game_id']}")
        return await real_set(ex, **kwargs)

    monkeypatch.setattr(world.games, "send", send)
    monkeypatch.setattr(broadcast_repo, "set_root_message", set_root)
    await _open(world, 1, 2)
    assert order == ["send:**2026", "send:Game 1", "persist:1", "send:Game 2", "persist:2"]


async def test_the_pending_rows_exist_before_the_first_discord_post(world, monkeypatch):
    """P5-14: the rows are written (a statement of their own) before any Discord
    call, so no Discord call can sit inside the write."""
    seen: list[int] = []
    real_send = world.games.send

    async def send(**kwargs):
        seen.append(len(world.store.rows))
        return await real_send(**kwargs)

    monkeypatch.setattr(world.games, "send", send)
    await _open(world, 1, 2, 3)
    assert seen == [3, 3, 3, 3]


async def test_a_reopen_of_the_same_block_is_idempotent_and_reveals_nothing(world):
    await _open(world, 1, 2, 3)
    sent_before = len(world.games.ops("send"))
    second = await _open(world, 1, 2, 3)
    assert second.rows_created == 0 and second.posted == () and second.header_posted is False
    assert second.already_posted == (1, 2, 3) and second.previous_block is None
    assert len(world.games.ops("send")) == sent_before and world.games.ops("edit") == []
    assert world.store.called("claim_reveal") == [], "re-opening must not reveal what it reopens"
    assert len(world.store.rows) == 3


async def test_one_failed_post_is_reported_and_the_rest_still_post(world):
    world.games.fail_send = {2: http_error()}  # send 0 = header, 1 = game 1, 2 = game 2
    report = await _open(world, 1, 2, 3)
    assert report.posted == (1, 3) and report.header_posted is True
    assert [(f.game_id, "HTTPException" in f.reason) for f in report.failed] == [(2, True)]
    assert world.store.row(2).root_message_id is None
    assert world.store.row(1).root_message_id is not None
    assert world.store.row(3).root_message_id is not None
    # A repeat is the lazy repair: only the game with no line is posted, no new header.
    again = await _open(world, 1, 2, 3)
    assert again.posted == (2,) and again.already_posted == (1, 3) and again.header_posted is False
    assert world.store.row(2).root_message_id is not None


async def test_a_failed_header_does_not_stop_the_games_and_names_the_permissions(world):
    world.games.fail_send = {0: forbidden()}
    report = await _open(world, 1, 2)
    assert report.header_posted is False and report.posted == (1, 2)
    (failure,) = report.failed
    assert failure.game_id is None and "Forbidden" in failure.reason
    assert "send_messages" in failure.reason


async def test_a_post_that_cannot_be_recorded_is_a_failure_not_an_abort(world, monkeypatch):
    real = world.store.set_root_message
    calls: list[int] = []

    async def flaky(ex, **kwargs):
        calls.append(kwargs["game_id"])
        if kwargs["game_id"] == 1:
            raise RuntimeError("db down")
        return await real(ex, **kwargs)

    monkeypatch.setattr(broadcast_repo, "set_root_message", flaky)
    report = await _open(world, 1, 2)
    assert report.posted == (2,) and [f.game_id for f in report.failed] == [1]
    assert "not recorded" in report.failed[0].reason and calls == [1, 2]


async def test_an_unavailable_games_channel_degrades_instead_of_raising(world):
    world.store.games_channel_id = None
    report = await _open(world, 1, 2)
    assert report.rows_created == 2 and report.posted == () and not report.header_posted
    assert {f.game_id for f in report.failed} == {None, 1, 2}
    assert world.games.calls == []
    assert all(world.store.row(g).root_message_id is None for g in (1, 2))


async def test_a_game_outside_the_league_is_reported_and_skipped(world):
    report = await _open(world, 1, 99)
    assert report.posted == (1,)
    assert [(f.game_id, "no game_broadcasts row" in f.reason) for f in report.failed] == [
        (99, True)
    ]


async def test_a_block_with_a_repeated_game_is_refused_before_any_write(world):
    with pytest.raises(ValueError, match="appears twice"):
        await _open(world, 1, 1)
    assert world.store.rows == {} and world.games.calls == []


async def test_opening_the_next_block_reveals_the_previous_blocks_idle_games(world):
    await _open(world, 1, 2, block_id="2026-11-01")
    world.scheduler.live.add((LEAGUE, 2))  # game 2 is still being watched
    report = await _open(world, 3, block_id="2026-11-02")
    previous = report.previous_block
    assert previous.block_id == "2026-11-01"
    assert previous.outcomes == {1: Outcome.REVEALED} and previous.still_broadcasting == (2,)
    assert world.games.content[5001] == result_line(1)
    assert world.games.content[5002] == matchup(2)  # left for its own on_ended
    assert world.bot.blocks.current(LEAGUE).block_id == "2026-11-02"
    world.scheduler.live.discard((LEAGUE, 2))
    await _ended(world, 2)  # the watched game's broadcast ends after the block closed
    assert world.games.content[5002] == result_line(2)


async def test_every_open_and_close_send_or_edit_carries_no_mentions(world):
    await _open(world, 1, 2, 3)
    await game_feed.close_block(world.bot, EX, league_id=LEAGUE)
    assert len(world.games.calls) == 7  # header + 3 roots, then 3 reveals
    for _, _, kwargs in world.games.calls:
        assert_no_mentions(kwargs)


# --- the two-phase reveal ----------------------------------------------------


async def test_a_zero_row_claim_posts_nothing(world):
    world.store.add_row(1, root_message_id=ROOT, claimed=True)  # someone else owns it
    outcome = await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert outcome is Outcome.NOT_OWNED
    assert world.games.calls == [] and world.store.called("mark_reveal_posted") == []


async def test_a_second_reveal_of_the_same_game_posts_nothing(world):
    world.store.add_row(1, root_message_id=ROOT)
    first = await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    second = await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert (first, second) == (Outcome.REVEALED, Outcome.NOT_OWNED)
    assert len(world.games.ops("edit")) == 1


async def test_a_discord_failure_between_claim_and_mark_leaves_it_claimed_not_posted(world, caplog):
    world.store.add_row(1, root_message_id=ROOT)
    world.games.edit_errors[ROOT] = forbidden()
    outcome = await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    row = world.store.row(1)
    assert outcome is Outcome.POST_FAILED
    assert row.reveal_claimed_at is not None and row.reveal_posted_at is None
    assert world.store.called("mark_reveal_posted") == []
    assert any("Forbidden" in r.getMessage() for r in caplog.records), "failure logged by name"
    # ... and the sweep completes it exactly once, without claiming again.
    del world.games.edit_errors[ROOT]
    claims_before = len(world.store.called("claim_reveal"))
    report = await game_feed.sweep_league(world.bot, EX, league_id=LEAGUE)
    assert [e.reveal for e in report.entries] == [Outcome.REVEALED]
    assert len(world.store.called("claim_reveal")) == claims_before
    assert world.store.row(1).reveal_posted_at is not None
    again = await game_feed.sweep_league(world.bot, EX, league_id=LEAGUE)
    assert again.entries == ()
    assert [kw["content"] for _, kw in world.games.ops("edit")][-1] == result_line(1)
    assert len(world.games.ops("edit")) == 2  # the failed attempt and the one that landed


async def test_a_mark_that_fails_after_the_post_is_recovered_by_the_sweep(world):
    world.store.add_row(1, root_message_id=ROOT)
    world.store.raises["mark_reveal_posted"] = RuntimeError("db down")
    outcome = await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert outcome is Outcome.MARK_FAILED and world.games.content[ROOT] == result_line(1)
    del world.store.raises["mark_reveal_posted"]
    await game_feed.sweep_league(world.bot, EX, league_id=LEAGUE)
    assert world.store.row(1).reveal_posted_at is not None
    assert world.games.content[ROOT] == result_line(1)  # re-edited, not duplicated
    assert world.games.ops("send") == []


async def test_a_game_with_no_root_message_is_posted_fresh_and_the_id_recorded(world):
    world.store.add_row(1, root_message_id=None)
    outcome = await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert outcome is Outcome.REVEALED
    assert [kw["content"] for _, kw in world.games.ops("send")] == [result_line(1)]
    assert world.games.ops("edit") == []
    assert world.store.row(1).root_message_id == 5000


async def test_a_deleted_root_message_is_replaced_by_a_fresh_post(world):
    world.store.add_row(1, root_message_id=ROOT)
    world.games.edit_errors[ROOT] = not_found()
    outcome = await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert outcome is Outcome.REVEALED
    assert world.games.ops("send")[0][1]["content"] == result_line(1)
    assert world.store.row(1).root_message_id == 5000


async def test_a_game_that_is_not_final_is_not_claimed_or_revealed(world):
    world.store.add_game(1, status=GameStatus.SCHEDULED)
    world.store.add_row(1, root_message_id=ROOT)
    outcome = await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert outcome is Outcome.NOT_REVEALABLE
    assert world.store.row(1).reveal_claimed_at is None and world.games.calls == []


async def test_a_game_id_from_another_league_is_not_revealable(world):
    world.store.add_game(50, league_id=2)
    world.store.add_row(50, league_id=2, root_message_id=ROOT)
    outcome = await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=50)
    assert outcome is Outcome.NOT_REVEALABLE and world.games.calls == []


async def test_the_score_comes_from_the_persisted_game_not_an_in_memory_result(world):
    result = sim_result()
    assert (result.home_score, result.away_score) != SCORES[1], "the two must differ to prove it"
    world.store.add_row(1, root_message_id=ROOT)
    world.bot.blocks.register_block(
        league_id=LEAGUE, block_id="2026-11-01", season=2026, games=[block_game(1, result=result)]
    )
    await game_feed.close_block(world.bot, EX, league_id=LEAGUE)
    assert world.games.content[ROOT] == result_line(1)


async def test_a_reveal_never_raises_for_an_unexpected_failure(world, caplog):
    world.store.add_row(1, root_message_id=ROOT)
    world.store.raises["get_game"] = RuntimeError("db down")
    outcome = await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert outcome is Outcome.ERRORED and world.games.calls == []
    assert any("reveal failed unexpectedly" in r.getMessage() for r in caplog.records)


async def test_a_hanging_discord_call_is_bounded(world, monkeypatch):
    import asyncio

    monkeypatch.setattr(game_feed, "DISCORD_CALL_TIMEOUT_SECONDS", 0.01)
    world.store.add_row(1, root_message_id=ROOT)

    async def hang(message_id, kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(world.games, "_edit", hang)
    outcome = await asyncio.wait_for(
        game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1), 5
    )
    assert outcome is Outcome.POST_FAILED
    assert world.store.row(1).reveal_posted_at is None


# --- on_ended never raises ---------------------------------------------------


async def test_on_ended_swallows_and_logs_a_discord_failure(world, caplog):
    world.store.add_row(1, status=FINISHED, root_message_id=ROOT)
    open_in_memory(world.bot, 1)
    world.games.edit_errors[ROOT] = forbidden()
    await _ended(world, 1)  # must not raise
    assert any("Forbidden" in r.getMessage() for r in caplog.records)
    assert world.store.row(1).reveal_claimed_at is None


async def test_on_ended_with_no_root_message_restores_nothing(world):
    world.store.add_row(1, status=FINISHED, root_message_id=None)
    open_in_memory(world.bot, 1)
    await _ended(world, 1)
    assert world.games.calls == []


async def test_on_ended_survives_a_failure_to_reach_the_database(world, caplog):
    world.store.raises["get_broadcast"] = RuntimeError("db down")
    open_in_memory(world.bot, 1)
    await _ended(world, 1)
    assert any("on_ended" in r.getMessage() for r in caplog.records)


async def test_every_reveal_send_and_edit_carries_no_mentions(world):
    world.store.add_row(1, root_message_id=ROOT)
    world.store.add_row(2, root_message_id=None)
    for game_id in (1, 2):
        await game_feed.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=game_id)
    assert len(world.games.calls) == 2
    for _, _, kwargs in world.games.calls:
        assert_no_mentions(kwargs)


# --- mutation twins ----------------------------------------------------------
#
# Each scenario runs on the real module (must pass) and on a private copy with
# one rule deleted (must fail). The control run proves a red is the rule's
# absence and not a loading failure.

_IS_CLOSED_GUARD = (
    "        if bot.blocks.is_closed(league_id, game_id):\n"
    "            await reveal_game(bot, ex, league_id=league_id, game_id=game_id)\n"
    "            return\n"
)
_CLAIM_CAS = (
    "                claimed_id = await broadcast_repo.claim_reveal(\n"
    "                    ex, league_id=league_id, game_id=game_id\n"
    "                )\n"
)
_LIVE_SKIP = (
    "        if not _may_reveal(bot, league_id, game_id):\n"
    "            still.append(game_id)\n"
    "            continue\n"
)
_SEND_MENTIONS = "channel.send(content=content, allowed_mentions=discord.AllowedMentions.none())"


async def scenario_finish_with_the_block_open_reveals_nothing(module, world: World) -> None:
    world.store.add_row(1, status=FINISHED, root_message_id=ROOT)
    open_in_memory(world.bot, 1)
    await _ended(world, 1, module=module)
    assert world.store.row(1).reveal_claimed_at is None, "revealed while the block was open"
    assert world.leaked_scores() == [] and world.games.content[ROOT] == matchup(1)


async def scenario_a_second_reveal_posts_nothing(module, world: World) -> None:
    world.store.add_row(1, root_message_id=ROOT, claimed=True)
    await module.reveal_game(world.bot, EX, league_id=LEAGUE, game_id=1)
    assert world.games.calls == [], "posted although the reveal was already claimed"


async def scenario_close_block_leaves_a_live_game_alone(module, world: World) -> None:
    world.store.add_row(1, status=BroadcastStatus.LIVE, root_message_id=ROOT)
    world.scheduler.live.add((LEAGUE, 1))
    open_in_memory(world.bot, 1)
    await module.close_block(world.bot, EX, league_id=LEAGUE)
    assert world.leaked_scores() == [], "revealed a game that is still being broadcast"


async def scenario_every_slate_post_carries_no_mentions(module, world: World) -> None:
    await module.open_block(
        world.bot, EX, league_id=LEAGUE, season=2026, block_id="b", games=[block_game(1)]
    )
    assert world.games.calls, "the scenario must post something for the check to mean anything"
    for _, _, kwargs in world.games.calls:
        assert_no_mentions(kwargs)


_TWINS = [
    (
        scenario_finish_with_the_block_open_reveals_nothing,
        _IS_CLOSED_GUARD,
        _IS_CLOSED_GUARD.replace("if bot.blocks.is_closed(league_id, game_id):", "if True:"),
    ),
    (
        scenario_a_second_reveal_posts_nothing,
        _CLAIM_CAS,
        "                claimed_id = (\n"
        "                    await broadcast_repo.get_broadcast(ex, league_id=league_id, "
        "game_id=game_id)\n"
        "                ).id\n",
    ),
    (scenario_close_block_leaves_a_live_game_alone, _LIVE_SKIP, ""),
    (
        scenario_every_slate_post_carries_no_mentions,
        _SEND_MENTIONS,
        "channel.send(content=content)",
    ),
]
_TWIN_IDS = ["reveal-while-block-open", "skip-the-claim-cas", "reveal-a-live-game", "drop-mentions"]


def _fresh_world(monkeypatch) -> World:
    games = FakeChannel()
    store = Store().install(monkeypatch)
    store.add_game(1)
    return World(FakeBot(games), games, FakeChannel(7001), store)


@pytest.mark.parametrize(("scenario", "old", "new"), _TWINS, ids=_TWIN_IDS)
async def test_twin_control_the_scenario_passes_on_the_real_and_an_unmutated_copy(
    scenario, old, new, monkeypatch
):
    for module in (game_feed, variant()):
        await scenario(module, _fresh_world(monkeypatch))


@pytest.mark.parametrize(("scenario", "old", "new"), _TWINS, ids=_TWIN_IDS)
async def test_twin_with_the_rule_deleted_the_scenario_goes_red(scenario, old, new, monkeypatch):
    mutated = variant((old, new))
    with pytest.raises(AssertionError):
        await scenario(mutated, _fresh_world(monkeypatch))


def test_a_stale_twin_anchor_is_an_error_not_a_pass():
    with pytest.raises(MutationAnchorError):
        variant(("this text is not in game_feed", "x"))
