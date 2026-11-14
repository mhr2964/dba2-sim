"""Decision 4 (phase-0-foundation.md §3): `domain/` must import nothing
from `dba2` except `core`, and must be importable with `asyncpg`
uninstalled — the strongest form of that rule.

Runs the check in a subprocess (not by poking `sys.modules` in-process)
because pytest's own collection may have already imported `asyncpg`
transitively (e.g. via `dba2.data.db` in another test module) before this
test runs, which would make an in-process `sys.modules['asyncpg'] = None`
check pass for the wrong reason (asyncpg already cached, never re-imported)
rather than actually proving domain/ never asks for it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_DOMAIN_MODULES = [
    "dba2.domain.phase",
    "dba2.domain.league",
    "dba2.domain.team",
    "dba2.domain.player",
    "dba2.domain.ratings",
    "dba2.domain.contract",
    "dba2.domain.draft",
    "dba2.domain.role",
    "dba2.domain.motivation",
    "dba2.domain.award",
    "dba2.domain.game",
    "dba2.domain.stats",
    "dba2.domain.injury",
    "dba2.domain.head_to_head",
    "dba2.domain.media",
    "dba2.domain.trade_offer",
    "dba2.domain.broadcast",
]

_SCRIPT = f"""
import sys
sys.modules['asyncpg'] = None  # any subsequent `import asyncpg` raises TypeError
for name in {_DOMAIN_MODULES!r}:
    __import__(name)
print("OK")
"""


def test_domain_imports_cleanly_with_asyncpg_uninstalled():
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_domain_module_source_imports_nothing_from_dba2_except_core():
    """AST-level check (Decision 4's own enforcement mechanism, per §3):
    walk every domain/*.py file's imports and assert none of them reach
    into a `dba2` submodule other than `dba2.domain` (peer imports) or
    `dba2.core`."""
    import ast

    domain_dir = REPO_ROOT / "dba2" / "domain"
    offenders: list[str] = []

    for path in sorted(domain_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    if module.startswith("dba2") and not (
                        module.startswith("dba2.domain") or module.startswith("dba2.core")
                    ):
                        offenders.append(f"{path.name}: import {module}")
                continue

            if module and module.startswith("dba2") and not (
                module.startswith("dba2.domain") or module.startswith("dba2.core")
            ):
                offenders.append(f"{path.name}: from {module} import ...")

    assert not offenders, f"domain/ imports outside domain/core: {offenders}"
