"""The `source-tree` xdist group and the guard that makes it enforceable.

A test file that writes scratch files into `dba2/` (or rewrites real source files there) and a
test file that scans the tree with `rglob` must never run at the same moment on two xdist
workers: the scanner meets a half-written or just-deleted scratch file, or one mutator
"restores" another's mutation. `pytest.mark.xdist_group("source-tree")` puts them on one
worker, but only under `--dist loadgroup`; `tests/conftest.py` refuses any other parallel
run. This file pins both halves, and pins that a NEW file which starts scanning or writing the
tree cannot forget the mark.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest as session_conftest

_UNIT_DIR = Path(__file__).resolve().parent
_MARK = 'pytestmark = pytest.mark.xdist_group("source-tree")'

# The writers and readers, by hand: every file that plants a scratch file under `dba2/`, rewrites
# a real source file there, or scans the tree (or a file such a writer rewrites).
_GROUPED = (
    "managers/test_constant_inventory.py",  # writer: rewrites managers/{tuning,window,...}.py
    "test_layering.py",  # writer: scratch modules under agency/, managers/, services/, sim/, ...
    "test_roster_mutation_lock_discipline.py",  # writer: scratch modules under bot/cogs/
    "presentation/test_broadcast_render.py",  # reader: rglob over dba2/
    "presentation/test_games.py",  # reader: through test_broadcast_render's `_call_sites`
    "bot/test_game_feed_pins.py",  # reader: rglob over dba2/
    "bot/test_offers_cog.py",  # reader: rglob over dba2/ and dba2/bot/
    "bot/test_watch_cog.py",  # reader: rglob over dba2/
    "test_bot_writes_no_leagues_column.py",  # reader: rglob over dba2/bot/
    "managers/test_window.py",  # reader of window.py, which test_constant_inventory rewrites
    "services/test_role_service.py",  # reader: rglob over dba2/sim/
)

# What makes a file a writer or a scanner, for the check that catches a newcomer.
_SCANS_OR_WRITES_TREE = re.compile(r"\.rglob\(|_layering_test_scratch|_SCRATCH_PATH|_call_sites")


def test_every_listed_file_carries_the_group_mark():
    missing = [f for f in _GROUPED if _MARK not in (_UNIT_DIR / f).read_text(encoding="utf-8")]
    assert missing == [], f"add `{_MARK}` to: {missing}"


def test_a_unit_test_file_that_scans_or_writes_the_source_tree_is_in_the_group():
    """The newcomer check: any file under `tests/unit/` whose text scans with `rglob` or plants a
    scratch file must carry the mark. (This file's own patterns are exempt, and so are the
    helper modules that are not test files.)"""
    offenders = []
    for path in sorted(_UNIT_DIR.rglob("test_*.py")):
        if path == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8")
        if _SCANS_OR_WRITES_TREE.search(text) and _MARK not in text:
            offenders.append(path.relative_to(_UNIT_DIR).as_posix())
    assert offenders == [], f"scans or writes dba2/ but is not in the group: {offenders}"


def test_the_marker_is_registered_so_it_is_known_without_xdist(pytestconfig):
    assert any(line.startswith("xdist_group") for line in pytestconfig.getini("markers"))


@pytest.mark.parametrize("dist", ["load", "no", "loadscope", None])
def test_a_parallel_run_without_loadgroup_is_refused(dist):
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=10, dist=dist))
    with pytest.raises(pytest.UsageError, match="--dist loadgroup"):
        session_conftest.refuse_parallel_run_without_loadgroup(config)


def test_a_parallel_run_with_loadgroup_is_accepted():
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=10, dist="loadgroup"))
    session_conftest.refuse_parallel_run_without_loadgroup(config)


@pytest.mark.parametrize("workers", [None, 0])
def test_a_serial_run_needs_no_distribution(workers):
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=workers, dist="no"))
    session_conftest.refuse_parallel_run_without_loadgroup(config)


def test_an_xdist_worker_is_not_re_checked():
    config = SimpleNamespace(
        workerinput={"workerid": "gw0"}, option=SimpleNamespace(numprocesses=10, dist="load")
    )
    session_conftest.refuse_parallel_run_without_loadgroup(config)
