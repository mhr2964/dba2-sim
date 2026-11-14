"""GM-3b.2(a)'s completeness half (docs/design/gm-command-surface.md,
row 28b): an AST scan of `gm_command_service.py` asserting every
`_resolve_and_authorize(` call passes `refuse_under_auto_pilot`
explicitly, and that the set of `True` sites is exactly `{waive,
release, sign_free_agent}`.

**Named tension, not silently inconsistent (round 3 critic, MINOR-7).**
GM-3b.2(a) rejects a command-keyed alias map (a frozenset of refused
command names, checked against the `command` string) specifically
because "no second vocabulary exists to drift." This test's own
hardcoded `{"waive", "release", "sign_free_agent"}` set IS a second
vocabulary, in the same shape as `tests/unit/test_roster_mutation_lock_
discipline.py`'s `RMLP_ENTRY_POINTS`. Kept anyway, deliberately: a
completeness TEST that can go red is a tripwire, not a runtime
vocabulary a real caller depends on -- the distinction the rejected
option was actually rejected over. A ninth `_resolve_and_authorize`
caller that forgets the keyword fails this test's first assertion, not
silently; one that passes the wrong bool fails the second.

Modelled on `RMLP_ENTRY_POINTS`/`_check_gate_entry_points`'s identical
job for `open_league_gate` callers, but self-contained here rather than
sharing that module's much larger, RMLP-specific AST machinery.
"""
from __future__ import annotations

import ast
from pathlib import Path

_GM_COMMAND_SERVICE_PATH = (
    Path(__file__).resolve().parents[2] / "dba2" / "services" / "gm_command_service.py"
)

_EXPECTED_TRUE_SITES = frozenset({"waive", "release", "sign_free_agent"})


def _resolve_and_authorize_calls() -> list[ast.Call]:
    tree = ast.parse(_GM_COMMAND_SERVICE_PATH.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_resolve_and_authorize"
    ]


def _keyword_value(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _literal_str(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _literal_bool(node: ast.expr) -> bool | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, bool) else None


def test_every_resolve_and_authorize_call_passes_refuse_under_auto_pilot_explicitly():
    calls = _resolve_and_authorize_calls()
    assert len(calls) >= 8, (
        "expected at least the 8 known call sites (waive, release, sign_free_agent, "
        f"draft, gm_strategy, gm_role, gm_directive, gm_timeline); found {len(calls)}"
    )
    missing_command = []
    missing_flag = []
    for call in calls:
        command_node = _keyword_value(call, "command")
        command = _literal_str(command_node) if command_node is not None else None
        flag_node = _keyword_value(call, "refuse_under_auto_pilot")
        if command_node is None:
            missing_command.append(f"line {call.lineno}")
            continue
        if flag_node is None:
            missing_flag.append(command or f"line {call.lineno}")
    assert not missing_command, (
        f"_resolve_and_authorize call(s) with no command=: {missing_command}"
    )
    assert not missing_flag, (
        f"_resolve_and_authorize call(s) missing refuse_under_auto_pilot=: {missing_flag} -- "
        "a new caller must pass it explicitly (GM-3b.2(a): a required keyword, not a default, "
        "so omitting it is a TypeError at the call site, not a silent allow)"
    )


def test_the_true_refuse_under_auto_pilot_sites_are_exactly_waive_release_sign_free_agent():
    calls = _resolve_and_authorize_calls()
    true_commands = set()
    false_commands = set()
    for call in calls:
        command_node = _keyword_value(call, "command")
        flag_node = _keyword_value(call, "refuse_under_auto_pilot")
        if command_node is None or flag_node is None:
            continue
        command = _literal_str(command_node)
        flag = _literal_bool(flag_node)
        if command is None or flag is None:
            # A non-literal command or flag can't be checked statically --
            # the first test's "missing" check doesn't apply here, and a
            # dynamic value is out of this test's reach by design (every
            # real call site today uses literals for both).
            continue
        (true_commands if flag else false_commands).add(command)

    assert true_commands == _EXPECTED_TRUE_SITES, (
        f"refuse_under_auto_pilot=True sites are {sorted(true_commands)}, "
        f"expected exactly {sorted(_EXPECTED_TRUE_SITES)}"
    )
    assert "draft" in false_commands, (
        "'draft' must pass refuse_under_auto_pilot=False -- it is refused one layer "
        "deeper, by GM-3's own branch in gm_service.apply_human_draft_pick"
    )
    assert true_commands.isdisjoint(false_commands)
