"""`dba2/bot/block_registry.py` -- the one-open-block-per-league, result-capped,
in-memory hold on a block's simmed games (design docs/design/phase-5-stage-d-
broadcast.md §5, P5-97.o/.m).

Every test drives the registry the way Stage E's producer will: a synthetic
producer builds `BlockGame`s and registers them. `GameResult`s here are
minimal (empty beat log) because the registry never looks inside one -- only
whether one is held, and by identity.

The mutation twins named in the commit body (delete the unknown-means-closed
rule, remove the cap, keep the old block on replacement, key the league
wrongly, ignore the league in `get`) were each shown red against these tests
and restored byte-for-byte.
"""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from dba2.bot import block_registry
from dba2.bot.block_registry import Block, BlockGame, BlockRegistry
from dba2.sim.events import GameResult

CAP = BlockRegistry.MAX_RESULTS_PER_BLOCK
DAY = date(2026, 10, 22)


def _result(home_id: int = 1, away_id: int = 2) -> GameResult:
    return GameResult(
        home_team_id=home_id,
        away_team_id=away_id,
        home_score=100,
        away_score=90,
        period_scores=(),
        overtime_periods=0,
        player_box_lines=(),
        team_box_lines=(),
        beat_log=(),
    )


def _producer(first_game_id: int, count: int, *, with_results: bool = True) -> list[BlockGame]:
    """A synthetic producer's output: `count` consecutively-numbered games."""
    return [
        BlockGame(
            game_id=gid,
            home_team_id=2 * gid,
            away_team_id=2 * gid + 1,
            home_code=f"H{gid}",
            away_code=f"A{gid}",
            game_date=DAY,
            result=_result(2 * gid, 2 * gid + 1) if with_results else None,
        )
        for gid in range(first_game_id, first_game_id + count)
    ]


def _register(
    reg: BlockRegistry, league_id: int, games: list[BlockGame], *, block_id: str = "2026-10-22"
) -> Block:
    return reg.register_block(league_id=league_id, block_id=block_id, season=1, games=games)


def _results_held(block: Block) -> int:
    return sum(1 for g in block.games.values() if g.result is not None)


# --- sizes: 1, 13, 16, 17, 550 -------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected_held"),
    [(1, 1), (13, 13), (16, 16), (17, 16), (550, 16)],
)
def test_block_sizes_hold_at_most_the_cap_of_results(count: int, expected_held: int):
    reg = BlockRegistry()
    block = _register(reg, 1, _producer(1, count))

    assert len(block.games) == count  # every game stays registered
    assert _results_held(block) == expected_held
    assert _results_held(block) <= CAP


def test_cap_is_sixteen():
    assert BlockRegistry.MAX_RESULTS_PER_BLOCK == 16


def test_games_beyond_the_cap_keep_their_blockgame_with_result_none():
    reg = BlockRegistry()
    games = _producer(100, 17)
    _register(reg, 1, games)

    first = reg.get(1, 100)
    last_kept = reg.get(1, 115)
    degraded = reg.get(1, 116)
    assert first is not None and first.result is games[0].result
    assert last_kept is not None and last_kept.result is games[15].result
    assert degraded is not None
    assert degraded.result is None
    # ...but the rest of the BlockGame survives intact.
    assert (degraded.game_id, degraded.home_code, degraded.away_code, degraded.game_date) == (
        116, "H116", "A116", DAY,
    )
    assert degraded.home_team_id == games[16].home_team_id


def test_degrading_copies_and_never_mutates_the_producers_objects():
    reg = BlockRegistry()
    games = _producer(1, 17)
    _register(reg, 1, games)
    assert games[16].result is not None  # the producer's own object is untouched


def test_550_game_reading_keeps_registration_order_and_memory_bound():
    reg = BlockRegistry()
    block = _register(reg, 1, _producer(1, 550))

    assert list(block.games) == list(range(1, 551))
    assert _results_held(block) == CAP
    held_ids = [gid for gid, g in block.games.items() if g.result is not None]
    assert held_ids == list(range(1, CAP + 1))  # the FIRST 16 keep results
    # Memory-bound arithmetic: 969 records/game x 16 games x ~300 B ~= 4.65 MB.
    assert 969 * _results_held(block) * 300 < 5_000_000


def test_games_already_lacking_a_result_do_not_use_up_result_slots():
    reg = BlockRegistry()
    games = _producer(1, 5, with_results=False) + _producer(6, CAP)
    block = _register(reg, 1, games)

    assert _results_held(block) == CAP
    assert all(block.games[g].result is None for g in range(1, 6))
    assert all(block.games[g].result is not None for g in range(6, 6 + CAP))


def test_a_repeated_game_id_raises_rather_than_silently_replacing():
    reg = BlockRegistry()
    games = _producer(1, 3)
    with pytest.raises(ValueError, match="game 2 appears twice"):
        _register(reg, 1, [*games, games[1]])
    assert reg.current(1) is None  # a rejected registration opens nothing


# --- the one-open-block bound and replacement ----------------------------------


def test_register_returns_the_block_it_opened():
    reg = BlockRegistry()
    block = reg.register_block(league_id=7, block_id="2026-10-22", season=3, games=_producer(1, 2))

    assert reg.current(7) is block
    assert (block.block_id, block.league_id, block.season) == ("2026-10-22", 7, 3)


def test_registering_a_second_block_replaces_the_first_wholesale():
    reg = BlockRegistry()
    old = _register(reg, 1, _producer(1, 13), block_id="2026-10-22")
    new = _register(reg, 1, _producer(101, 3), block_id="2026-10-23")

    assert reg.current(1) is new
    assert reg.current(1) is not old
    assert new.block_id == "2026-10-23"
    # None of the old block's games is reachable any more...
    for gid in old.games:
        assert reg.get(1, gid) is None
        assert reg.is_closed(1, gid) is True
    # ...and all of the new block's are open.
    for gid in new.games:
        assert reg.get(1, gid) is not None
        assert reg.is_closed(1, gid) is False


def test_replacement_reports_which_games_were_still_open():
    """The producer's order (design P5-97.n step 1): read what is open, close
    it, then register. `close` returns the block whose games are exactly the
    ones still open at that moment -- the games the caller must reveal."""
    reg = BlockRegistry()
    _register(reg, 1, _producer(1, 13), block_id="2026-10-22")

    still_open = reg.close(1)
    assert still_open is not None
    assert list(still_open.games) == list(range(1, 14))
    assert all(reg.is_closed(1, gid) for gid in still_open.games)  # closed from now on

    _register(reg, 1, _producer(101, 2), block_id="2026-10-23")
    assert [g for g in range(1, 14) if not reg.is_closed(1, g)] == []
    assert list(reg.current(1).games) == [101, 102]  # type: ignore[union-attr]


def test_replacing_directly_leaves_the_previous_block_readable_by_whoever_held_it():
    reg = BlockRegistry()
    old = _register(reg, 1, _producer(1, 4))
    _register(reg, 1, _producer(101, 1), block_id="2026-10-23")

    # The dropped block object is not torn down; only the registry forgets it.
    assert list(old.games) == [1, 2, 3, 4]
    assert all(g.result is not None for g in old.games.values())


def test_at_most_one_block_is_open_per_league_across_many_registrations():
    reg = BlockRegistry()
    for day in range(1, 8):
        _register(reg, 1, _producer(day * 100, 3), block_id=f"2026-10-{day:02d}")
        current = reg.current(1)
        assert current is not None
        assert current.block_id == f"2026-10-{day:02d}"
        # Only the current block's games are open; every earlier day's are closed.
        for earlier in range(1, day):
            assert all(reg.is_closed(1, earlier * 100 + i) for i in range(3))


# --- close ---------------------------------------------------------------------


def test_close_pops_and_returns_the_block_and_is_idempotent():
    reg = BlockRegistry()
    block = _register(reg, 1, _producer(1, 2))

    assert reg.close(1) is block
    assert reg.current(1) is None
    assert reg.get(1, 1) is None
    assert reg.close(1) is None  # already closed: nothing to hand back


def test_close_of_a_league_with_no_block_is_none():
    assert BlockRegistry().close(99) is None


# --- is_closed truth table -----------------------------------------------------


def test_is_closed_open_game_is_false():
    reg = BlockRegistry()
    _register(reg, 1, _producer(1, 3))
    assert reg.is_closed(1, 1) is False
    assert reg.is_closed(1, 3) is False


def test_is_closed_cap_degraded_game_of_an_open_block_is_false():
    """`result=None` does not mean closed: the game's block is still open, so
    its score must stay out of `#games` (design P5-97.m)."""
    reg = BlockRegistry()
    _register(reg, 1, _producer(1, 17))
    degraded = reg.get(1, 17)
    assert degraded is not None and degraded.result is None
    assert reg.is_closed(1, 17) is False


def test_is_closed_after_close_block_is_true():
    reg = BlockRegistry()
    _register(reg, 1, _producer(1, 3))
    reg.close(1)
    assert reg.is_closed(1, 1) is True


def test_is_closed_for_an_evicted_block_is_true():
    reg = BlockRegistry()
    _register(reg, 1, _producer(1, 3))
    _register(reg, 1, _producer(11, 3), block_id="2026-10-23")
    assert reg.is_closed(1, 1) is True   # evicted
    assert reg.is_closed(1, 11) is False  # its replacement


def test_is_closed_for_a_league_with_no_block_is_true():
    """After a restart memory is empty: every block counts as closed."""
    assert BlockRegistry().is_closed(1, 1) is True


def test_is_closed_for_a_game_the_open_block_never_held_is_true():
    reg = BlockRegistry()
    _register(reg, 1, _producer(1, 3))
    assert reg.is_closed(1, 999) is True


def test_a_fresh_registry_is_empty_and_two_registries_share_nothing():
    a, b = BlockRegistry(), BlockRegistry()
    _register(a, 1, _producer(1, 2))
    assert b.current(1) is None
    assert b.is_closed(1, 1) is True


# --- per-league isolation ------------------------------------------------------


def test_one_league_registering_never_affects_another():
    reg = BlockRegistry()
    a_block = _register(reg, 1, _producer(1, 5), block_id="2026-10-22")
    b_block = _register(reg, 2, _producer(101, 5), block_id="2026-10-22")

    # B registering again replaces only B's block.
    b_new = _register(reg, 2, _producer(201, 2), block_id="2026-10-23")
    assert reg.current(1) is a_block
    assert reg.current(2) is b_new
    assert all(reg.is_closed(2, g) for g in b_block.games)
    assert all(not reg.is_closed(1, g) for g in a_block.games)

    # Closing A leaves B untouched.
    reg.close(1)
    assert reg.current(2) is b_new
    assert all(not reg.is_closed(2, g) for g in b_new.games)


def test_lookup_by_league_and_game_never_returns_another_leagues_game():
    reg = BlockRegistry()
    _register(reg, 1, _producer(1, 5))
    _register(reg, 2, _producer(101, 5))

    assert reg.get(1, 101) is None  # league 2's game, asked through league 1
    assert reg.get(2, 1) is None    # league 1's game, asked through league 2
    assert reg.get(3, 1) is None    # a league with no block
    assert reg.is_closed(1, 101) is True
    assert reg.is_closed(2, 1) is True
    own = reg.get(2, 101)
    assert own is not None and own.game_id == 101


def test_the_same_game_id_in_two_leagues_resolves_to_each_leagues_own_game():
    reg = BlockRegistry()
    a = _producer(1, 1)
    b = [BlockGame(1, 50, 51, "ZZZ", "YYY", DAY, _result(50, 51))]
    _register(reg, 1, a)
    _register(reg, 2, b)

    got_a, got_b = reg.get(1, 1), reg.get(2, 1)
    assert got_a is not None and got_b is not None
    assert got_a.home_code == "H1"
    assert got_b.home_code == "ZZZ"


def test_the_result_cap_is_per_block_not_shared_across_leagues():
    reg = BlockRegistry()
    a = _register(reg, 1, _producer(1, CAP))
    b = _register(reg, 2, _producer(1001, CAP))
    assert _results_held(a) == CAP
    assert _results_held(b) == CAP


# --- B1: eviction never orphans a running task ---------------------------------


def test_evicting_a_block_does_not_disturb_a_task_that_captured_its_game():
    """A broadcast task captures its `BlockGame` by value at start and reads
    the registry afterwards only through `is_closed`. Eviction must therefore
    leave the captured game intact and only flip `is_closed` to True."""
    reg = BlockRegistry()
    _register(reg, 1, _producer(1, 3))

    captured = reg.get(1, 2)  # what a task takes at start
    assert captured is not None and captured.result is not None
    result_before = captured.result

    assert reg.is_closed(1, 2) is False
    _register(reg, 1, _producer(11, 3), block_id="2026-10-23")  # /advance again

    assert reg.is_closed(1, 2) is True
    assert captured.result is result_before
    assert captured.game_id == 2
    assert reg.get(1, 2) is None  # the registry itself no longer serves it


# --- layering: no I/O, no discord ---------------------------------------------


def test_block_registry_module_imports_neither_discord_nor_asyncio_nor_data():
    tree = ast.parse(Path(block_registry.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    for name in imported:
        top = name.split(".")[0]
        assert top not in {"discord", "asyncio", "threading", "sqlite3", "asyncpg"}, name
        assert not name.startswith(("dba2.data", "dba2.services")), name
    assert not any(isinstance(n, ast.AsyncFunctionDef | ast.Await) for n in ast.walk(tree))
