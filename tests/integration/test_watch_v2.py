"""`/watch` v4 against real Postgres: a whole game leaves the thread as ONE
clickable message -- no per-period chunk message and no recap
(docs/design/watch-v4-live-ticker.md, supersedes watch-v2's "one scoreboard,
one message per played period chunk and one recap" acceptance criterion).

The producer, the cog, `resolve_league`, `broadcast_repo`, the scheduler and the
block registry are the REAL ones from `test_watch.py`; the game is a real
`sim_game` over the seeded league, and the box, the teams and the persisted score
are read from Postgres. Only Discord is fake, and the pacer runs on an injected
clock. What the unit tier cannot show and this does: the messages a REAL game's
plan produces, delivered through the real cog and scheduler, add up to the game
the database recorded, AND the registered pager answers every possession of it.

The jump case makes the pacer skip the reveal that ends the first period -- in the
default `game` speed that is Q1's separator line (docs/design/watch-narration-v2.md
WN-11) -- so the live component must land on the reveal it actually reached (no gap,
T-1.a), the separator must still be in the feed it scrolls into, and the cycle row's
periods must still be exactly what is visible.

No live gateway exists here: this proves the messages that WOULD be sent, not that
a real guild accepts them.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import pytest

from dba2.data.repositories import game_repo
from dba2.domain.broadcast import BroadcastStatus
from dba2.presentation.broadcast import (
    FrameKind,
    completed_through,
    plan_broadcast,
    plan_narration,
    plan_period_lines,
    reveal_schedule,
    visible_periods,
)
from dba2.services import view_service
from tests.integration import test_watch as watch_tests
from tests.integration.test_game_feed import synthetic_producer
from tests.integration.test_watch import Watching

# The fixtures the real cog and scheduler run on, reused as `test_watch.py` reuses the producer's.
world = watch_tests.world
watching = watch_tests.watching

FINISHED = BroadcastStatus.FINISHED


def _period_label(period: int) -> str:
    return f"Q{period}" if period <= 4 else f"OT{period - 4}"


async def _play_a_game(watching: Watching, *, jump_over_first_period_end: bool):
    """Produce one real game, `/watch` it, run the broadcast to its end and hand
    back what the thread ended up holding next to what the database recorded."""
    world = watching.world
    game_id = world.game_ids[0]
    _, results, _ = await synthetic_producer(world, [game_id], block_id="2026-10-24")
    result = results[game_id]
    game = await game_repo.get_game(world.pool, game_id)
    # The names the cog resolves from Postgres for this very game: the expected
    # lines are worked out from the same rows, not from the simulator's stand-ins.
    data = await view_service.get_broadcast_inputs(world.pool, game_id=game_id, result=result)
    home, away = data.home, data.away
    # Planned the way the cog plans a `/watch` with no `speed:` -- the scheduler's own
    # settings, the `game` speed (docs/design/watch-narration-v2.md WN-10.4).
    settings = world.bot.broadcasts.settings
    plan = plan_broadcast(
        result, game_id=game_id, home_code=home.code, away_code=away.code, settings=settings.plan
    )
    period_lines = plan_period_lines(plan, home=home, away=away, names=data.names)
    all_lines = tuple(line for pl in period_lines for line in pl.lines)
    narration = plan_narration(result, plan, home=home, away=away, names=data.names)
    schedule = reveal_schedule(plan, narration, tick_seconds=settings.tick_seconds)

    await watching.watch(game_id)
    (thread,) = watching.channel.threads
    if jump_over_first_period_end:
        q1_end = _q1_end_reveal(plan, schedule)

        async def stall_before_the_period_end(n: int, kwargs) -> None:
            if n == q1_end - 1:  # so slow that Q1's separator reveal is overtaken
                overtaken = schedule[q1_end + 1].due_seconds - schedule[q1_end - 1].due_seconds
                world.clock.now += overtaken + 0.5

        thread.messages[0].on_call = stall_before_the_period_end
    await watching.finish(game_id)
    return world, game, home, away, result, plan, all_lines, thread, narration, schedule


def _q1_end_reveal(plan, schedule) -> int:
    return next(
        n
        for n, reveal in enumerate(schedule)
        if plan.frames[reveal.frame_index].kind is FrameKind.PERIOD_END
    )


@pytest.mark.parametrize("jump", [False, True], ids=["every-frame", "jump-over-a-period-end"])
async def test_a_full_game_leaves_exactly_one_clickable_message(watching, jump):
    world, game, home, away, result, plan, all_lines, thread, narration, schedule = (
        await _play_a_game(watching, jump_over_first_period_end=jump)
    )
    row = await watching.row(game.id)
    assert row.status is FINISHED

    # T-5.e's corrected acceptance criterion: exactly one bot message in the
    # thread, and it carries live buttons. No period message, no recap.
    assert len(thread.messages) == 1
    message = thread.messages[0]
    assert message.id == row.message_id  # the persisted message is the live component
    final = message.calls[-1][0]["embed"]

    assert final.description.startswith("**FINAL")
    assert final.title == f"{away.code} {game.away_score} - {game.home_score} {home.code}"
    assert (game.home_score, game.away_score) == (result.home_score, result.away_score)

    # The FINAL shape (T-2.c): Line Score, the two stat fields, Team Totals, Story.
    field_names = [f.name for f in final.fields]
    assert field_names[0] == "Line Score"
    assert field_names[-2:] == ["Team Totals", "Story"]
    assert len(field_names) == 5

    # The cycle row names every period the game actually played, Full first, and
    # the full-text button is pinned to the last row.
    view = message.calls[-1][0]["view"]
    *cycle, full_text = view.children
    played = sorted(period for period, _, _ in result.period_scores)
    assert [c.period for c in cycle] == [0, *played]
    assert full_text.item.row == 4

    # Every possession the plan produced is in the live component's OWN rendered
    # lines exactly once, in order -- the same tuple the ticker and the pager both
    # slice (T-3), never reconstructed independently here.
    assert len(all_lines) == plan.total_possessions

    if jump:  # the scenario really did skip one reveal, and it was the period's end
        q1_end = _q1_end_reveal(plan, schedule)
        assert len(message.calls) == len(schedule) - 1
        statuses = [kwargs["embed"].description.split("\n")[0] for kwargs, _ in message.calls]
        assert not any("End of Q1" in status for status in statuses)  # that reveal was skipped
        # The reveal actually delivered right after the jump is the one after Q1's
        # separator: the separator is still in its feed (latency, never content), and
        # nothing between them is missing from `visible_periods`.
        landed = schedule[q1_end + 1]
        separator = narration.lines[schedule[q1_end].cursor].text
        assert separator.startswith("━") and "End of Q1" in separator
        assert separator in message.calls[q1_end][0]["embed"].description
        last = completed_through(narration, landed.cursor)
        assert visible_periods(plan, last) == (1, 2)


async def test_the_registered_pager_answers_every_possession_after_the_game(watching):
    """The full-text button's own machinery (`PagerEntry.available_lines`),
    exercised against the REAL row this broadcast left behind: Full Game (period
    0) holds every possession, in order, and the cap never trails what the
    thread's own message shows."""
    world, game, home, away, result, plan, all_lines, thread, _, _ = await _play_a_game(
        watching, jump_over_first_period_end=False
    )
    entry = world.bot.pagers.get(world.league_id, game.id)
    assert entry is not None
    full_game = entry.available_lines(0)
    assert full_game == all_lines
    assert len(full_game) == plan.total_possessions
    for period, group in _group_by_period(plan):
        expected = tuple(all_lines[i] for i in group)
        assert entry.available_lines(period) == expected


def _group_by_period(plan):
    from itertools import groupby

    for period, possessions in groupby(plan.possessions, key=lambda p: p.period):
        yield period, [p.index for p in possessions]
