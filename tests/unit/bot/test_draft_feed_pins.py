"""Static pins on `dba2/bot/draft_feed.py` (docs/design/phase-5-stage-d-
prime-draft.md §4 commit 7, P5-98.i): that the draft never touches the
broadcast's own machinery, that it posts new messages and edits nothing,
that the announcement's SET of picks never comes from a `draft_selections`
re-read, and that no Discord call sits inside a transaction.

AST, not grep, so this module's own docstring -- which names `reveal_game`
and `open_block` in the sentence explaining why it never calls them -- does
not trip its own pin. Each pin has a mutation twin that plants a violation
in a scratch copy of the REAL module and checks the SAME helper flags it:
a rule that silently matches nothing is worse than no rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from dba2.bot import draft_feed

DRAFT_FEED = Path(draft_feed.__file__).resolve()

_BROADCAST_ONLY = ("reveal_game", "open_block", "close_block", "edit_line", "on_ended")
"""`#games` state the broadcast owns. P5-97.m's reveal rule turns on them,
so a draft that called one could leak a score or rewrite a slate line; the
draft posts NEW messages and nothing else."""

_ANNOUNCE_ONCE_FORBIDDEN = ("list_draft_selections_for_draft", "get_draft_selection")
"""A re-read of `draft_selections` is what would re-announce the whole
draft on the second `/draft`. The lines come from the cycle's own outcome
plus the human's pick, which is what makes the announcement at-most-once."""

_TRANSACTION_NAMES = {"transaction", "acquire"}


def _tree(source: str | None = None) -> ast.AST:
    return ast.parse(source if source is not None else DRAFT_FEED.read_text(encoding="utf-8"))


def _called_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def test_the_draft_never_calls_the_broadcasts_own_publishers_or_editors():
    assert not (_called_names(_tree()) & set(_BROADCAST_ONLY))


@pytest.mark.parametrize("forbidden", _BROADCAST_ONLY)
def test_that_pin_has_teeth(forbidden):
    planted = DRAFT_FEED.read_text(encoding="utf-8") + (
        f"\n\nasync def leak(bot, channel):\n    await game_feed.{forbidden}(bot, channel)\n"
    )
    assert _called_names(_tree(planted)) & set(_BROADCAST_ONLY)


def test_the_announcement_never_re_reads_the_draft_selections():
    assert not (_called_names(_tree()) & set(_ANNOUNCE_ONCE_FORBIDDEN))


def test_the_announce_once_pin_has_teeth():
    planted = DRAFT_FEED.read_text(encoding="utf-8") + (
        "\n\nasync def replay(ex, draft_id):\n"
        "    return await draft_repo.list_draft_selections_for_draft(ex, draft_id)\n"
    )
    assert _called_names(_tree(planted)) & set(_ANNOUNCE_ONCE_FORBIDDEN)


def _transaction_names(tree: ast.AST) -> list[str]:
    return [
        f"line {n.lineno}"
        for n in ast.walk(tree)
        if (isinstance(n, ast.Name) and n.id in _TRANSACTION_NAMES)
        or (isinstance(n, ast.Attribute) and n.attr in _TRANSACTION_NAMES)
        or (isinstance(n, ast.ImportFrom) and any(a.name in _TRANSACTION_NAMES for a in n.names))
    ]


def test_draft_feed_opens_no_transaction_of_its_own():
    """P5-14, the same pin `game_feed` carries: every repository call here is
    one statement that commits by itself, so there is no transaction for a
    Discord call to sit inside. `_require_no_open_transaction` is the
    runtime half of the same rule, for a caller that hands one in."""
    assert _transaction_names(_tree()) == []


def test_the_transaction_pin_has_teeth():
    planted = DRAFT_FEED.read_text(encoding="utf-8") + "\nx = transaction(pool)\n"
    assert _transaction_names(_tree(planted))
