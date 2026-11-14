"""Static pins on `dba2/bot/game_feed.py` (Stage D commit 9,
docs/design/phase-5-stage-d-broadcast.md P5-97.m "Enforced in one place, three
ways", P5-97.s, P5-14): who may call the one publisher of a score, who may call
the one renderer that accepts a score, that no score-bearing renderer is on the
slate path, that every send/edit passes `AllowedMentions`, and that no
transaction wraps a Discord call.

AST, not grep, so a docstring that explains a rule does not trip it -- and each
pin has a mutation twin that plants a violation in a scratch copy of the REAL
module and checks the SAME helper flags it ("a rule that silently matches
nothing is worse than no rule").
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from dba2.bot import game_feed

# This file writes scratch files into `dba2/` or scans it (`tests/conftest.py`): one xdist
# group keeps every such writer and reader on one worker, run serially.
pytestmark = pytest.mark.xdist_group("source-tree")

DBA2_DIR = Path(game_feed.__file__).resolve().parents[1]
GAME_FEED = Path(game_feed.__file__).resolve()
SCORE_BEARING = {
    "build_result_line",
    "render_interrupted",
    "render_live_component",
    "reveal_game",
}


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _call_sites(tree: ast.AST, name: str) -> list[str]:
    """The innermost enclosing function of every call to `name`."""
    sites: list[str] = []

    def visit(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else enclosing
            )
            if isinstance(child, ast.Call) and _called_name(child) == name:
                sites.append(enclosing)
            visit(child, inner)

    visit(tree, "<module>")
    return sites


def _all_dba2_call_sites(name: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in sorted(DBA2_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend((path.relative_to(DBA2_DIR).as_posix(), s) for s in _call_sites(tree, name))
    return found


def _tree(path: Path = GAME_FEED) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# --- the caller-set pins ------------------------------------------------------


def test_reveal_game_is_called_from_exactly_on_ended_close_block_and_sweep_league():
    sites = _all_dba2_call_sites("reveal_game")
    assert {(f, s) for f, s in sites} == {
        ("bot/game_feed.py", "on_ended"),
        ("bot/game_feed.py", "close_block"),
        ("bot/game_feed.py", "sweep_league"),
    }
    assert sorted(s for _, s in sites) == ["close_block", "on_ended", "on_ended", "sweep_league"]


def test_the_reveal_predicate_is_asked_by_on_ended_close_block_and_the_sweep():
    """P5-97.m is ONE predicate (`_may_reveal`: block closed AND nothing
    broadcasting the game). The three callers of `reveal_game` each ask it, the
    sweep through `_owned_here`; a caller that asks a weaker question of its own
    is how a score once reached `#games` while a broadcast held the game."""
    assert sorted(_call_sites(_tree(), "_may_reveal")) == [
        "_owned_here",
        "close_block",
        "on_ended",
    ]
    assert _call_sites(_tree(), "_owned_here") == ["sweep_league", "sweep_league"]


def test_the_reveal_predicate_pin_has_teeth():
    real = GAME_FEED.read_text(encoding="utf-8")
    weakened = real.replace(
        "        if _may_reveal(bot, league_id, game_id):\n            await reveal_game",
        "        if bot.blocks.is_closed(league_id, game_id):\n            await reveal_game",
    )
    assert weakened != real
    assert "on_ended" not in _call_sites(ast.parse(weakened), "_may_reveal")


def test_build_result_line_has_exactly_one_call_site_inside_reveal_game():
    assert _all_dba2_call_sites("build_result_line") == [("bot/game_feed.py", "reveal_game")]


def test_no_other_module_reaches_the_score_bearing_publisher_or_renderer_by_name():
    """`reveal_game` and `build_result_line` are imported nowhere but `game_feed`
    itself (and `build_result_line`'s defining module), so a second publisher
    would have to import the renderer, which this catches."""
    offenders: list[str] = []
    for path in sorted(DBA2_DIR.rglob("*.py")):
        if path.name in ("game_feed.py", "games.py"):
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom) and any(
                a.name in ("build_result_line", "reveal_game") for a in node.names
            ):
                offenders.append(path.relative_to(DBA2_DIR).as_posix())
    assert offenders == []


def _slate_path_reachable(tree: ast.Module) -> set[str]:
    """Names called from `open_block` directly or through helpers, NOT following
    `close_block` -- the one deliberate edge from opening a block to revealing
    the previous one (P5-97.n step 1)."""
    functions = {
        n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    seen: set[str] = set()
    todo = ["open_block"]
    called: set[str] = set()
    while todo:
        name = todo.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Call):
                callee = _called_name(node)
                if callee is None:
                    continue
                called.add(callee)
                if callee != "close_block":
                    todo.append(callee)
    return called


def test_no_score_bearing_renderer_is_reachable_from_the_slate_path():
    called = _slate_path_reachable(_tree())
    assert "build_matchup_line" in called and "build_slate_header" in called
    assert "close_block" in called, "the previous block's reveal is open_block's only score edge"
    assert not (called & SCORE_BEARING), called & SCORE_BEARING
    # ... and the slate lines are the score-free ones: a SlateGame has no score field.
    from dataclasses import fields

    from dba2.presentation.games import SlateGame

    assert {f.name for f in fields(SlateGame)} == {"game_id", "home_code", "away_code", "game_date"}


def test_the_slate_path_pin_has_teeth(tmp_path):
    scratch = tmp_path / "game_feed.py"
    real = GAME_FEED.read_text(encoding="utf-8")
    assert not (_slate_path_reachable(ast.parse(real)) & SCORE_BEARING)
    planted = real.replace(
        "            message_id = await send_line(channel, build_matchup_line(slate))",
        "            message_id = await send_line(channel, build_result_line(slate, "
        "home_score=1, away_score=2))",
    )
    assert planted != real
    scratch.write_text(planted, encoding="utf-8")
    assert _slate_path_reachable(_tree(scratch)) & SCORE_BEARING


def test_the_caller_pin_has_teeth():
    real = GAME_FEED.read_text(encoding="utf-8")
    planted = (
        real
        + "\n\nasync def sneaky(bot, ex):\n    await reveal_game(bot, ex, league_id=1, game_id=1)\n"
    )
    assert "sneaky" in _call_sites(ast.parse(planted), "reveal_game")
    planted = (
        real + "\n\ndef sneaky():\n    return build_result_line(None, home_score=1, away_score=2)\n"
    )
    assert _call_sites(ast.parse(planted), "build_result_line") == ["reveal_game", "sneaky"]


# --- mentions -----------------------------------------------------------------


def _sends_and_edits_without_mentions(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("send", "edit")
        and "allowed_mentions" not in {k.arg for k in node.keywords}
    ]


def test_every_send_and_edit_in_game_feed_passes_allowed_mentions():
    tree = _tree()
    sites = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("send", "edit")
    ]
    assert len(sites) >= 3, "the check must actually see the module's sends and edits"
    assert _sends_and_edits_without_mentions(tree) == []


def test_the_mentions_pin_has_teeth():
    planted = GAME_FEED.read_text(encoding="utf-8") + (
        "\n\nasync def leak(channel):\n    await channel.send(content='@everyone')\n"
    )
    assert _sends_and_edits_without_mentions(ast.parse(planted))


# --- no Discord call inside a transaction; no results, no services ------------


def _transaction_names(tree: ast.AST) -> list[str]:
    forbidden = {"transaction", "acquire"}
    return [
        f"line {n.lineno}"
        for n in ast.walk(tree)
        if (isinstance(n, ast.Name) and n.id in forbidden)
        or (isinstance(n, ast.Attribute) and n.attr in forbidden)
        or (isinstance(n, ast.ImportFrom) and any(a.name in forbidden for a in n.names))
    ]


def test_game_feed_opens_no_transaction_of_its_own():
    """Every repository call is one statement that commits by itself, so there
    is no transaction here for a Discord call to sit inside (P5-14)."""
    assert _transaction_names(_tree()) == []
    planted = ast.parse(GAME_FEED.read_text(encoding="utf-8") + "\nx = transaction(pool)\n")
    assert _transaction_names(planted)


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def test_game_feed_reads_no_in_memory_game_result_and_calls_no_service():
    """Scores come from persisted rows (P5-42: no `GameResult` survives a
    restart), and this is Discord-side bookkeeping, not a business rule, so no
    `dba2.sim` or `dba2.services` import."""
    imported = _imported_modules(_tree())
    assert not {m for m in imported if m.startswith(("dba2.sim", "dba2.services"))}


@pytest.mark.parametrize("planted", ["import dba2.sim.events\n", "from dba2.services import x\n"])
def test_the_import_pin_has_teeth(planted):
    tree = ast.parse(planted + GAME_FEED.read_text(encoding="utf-8"))
    assert {m for m in _imported_modules(tree) if m.startswith(("dba2.sim", "dba2.services"))}
