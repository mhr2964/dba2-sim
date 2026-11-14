"""Session-wide test configuration.

**`-n` without `--dist loadgroup` is refused.** Several unit files write scratch files into
`dba2/` (or rewrite real source files there) and others scan the whole tree with `rglob`;
run on different pytest-xdist workers at once, a scanner meets a half-written or just-deleted
scratch file and fails, or a mutator "restores" another's mutation. Those files carry
`pytest.mark.xdist_group("source-tree")`, which xdist honours only under `--dist loadgroup`;
under any other distribution the marks are silently ignored and the races come back as
flakes nobody can reproduce. So the one supported parallel unit run is

    python -m pytest tests/unit -q -p no:cacheprovider -n 10 --dist loadgroup

and this hook turns the unsupported ones into an immediate, explained error.
"""

from __future__ import annotations

import pytest


def refuse_parallel_run_without_loadgroup(config) -> None:
    """Raises `pytest.UsageError` when this session distributes tests across xdist workers
    with anything but `--dist loadgroup`. A worker process (which has `workerinput`) is
    skipped: its controller already decided."""
    if hasattr(config, "workerinput"):
        return
    workers = getattr(config.option, "numprocesses", None)
    if workers and getattr(config.option, "dist", None) != "loadgroup":
        raise pytest.UsageError(
            "parallel runs need `--dist loadgroup`: the tests that write scratch files into "
            "dba2/ or scan it are grouped with pytest.mark.xdist_group('source-tree'), and "
            "xdist ignores the group under any other distribution (tests/conftest.py). "
            "Run: python -m pytest tests/unit -q -p no:cacheprovider -n 10 --dist loadgroup"
        )


def pytest_configure(config) -> None:
    refuse_parallel_run_without_loadgroup(config)
