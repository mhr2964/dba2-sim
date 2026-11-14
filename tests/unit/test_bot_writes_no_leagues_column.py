"""`dba2/bot/` writes no `leagues` column (phase-5-stage-e-advance.md P5-99.m.1).

The claim, the three completion checkpoints and the pointer are all `season_service`'s.
What the cog owns is the record that a Discord message went out, and that record is NOT
in `leagues`: it is `media_stories.posted_at`, written one story at a time through
`media_service.mark_wire_story_posted` (P5-99.s.1). The rule is stated as a test so the
shape F17 found -- a bot-layer `leagues` write nobody listed, racing the service's own
-- cannot come back quietly: writing `hook_completed_cycle_index` from the cog "because
that is where the post happens" is exactly the tempting move.

Two independent detectors, because either alone has a blind spot:

- a raw SQL string in `dba2/bot/` that writes `leagues`;
- a call to a `league_repo` writer, or to the phase service's pointer movers, from
  `dba2/bot/` -- which is how a write would actually arrive, through the repository
  function rather than a hand-written statement.

Each has a twin that adds one and must trip, and an empty scan fails loudly.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# This file writes scratch files into `dba2/` or scans it (`tests/conftest.py`): one xdist
# group keeps every such writer and reader on one worker, run serially.
pytestmark = pytest.mark.xdist_group("source-tree")

BOT_DIR = Path(__file__).resolve().parents[2] / "dba2" / "bot"

_LEAGUES_WRITE_SQL = re.compile(r"\b(UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+leagues\b", re.I)

# `league_repo`'s writers of a `leagues` row (or of the `phase_history` its pointer moves
# with), and the two phase-service functions that move the pointer. The reads
# (`get_league`, `get_league_by_guild_id`, `get_league_state_for_claim_check`,
# `count_phase_entries`, ...) are not here: reading is the bot's job.
_LEAGUE_WRITERS = frozenset(
    {
        "create_league",
        "archive_league",
        "claim_advance",
        "release_advance",
        "set_hook_completed_cycle_index",
        "set_entry_work_completed_index",
        "set_closing_work_completed_index",
        "set_current_season",
        "set_current_phase",
        "record_phase_transition",
    }
)
_POINTER_MOVERS = frozenset({"advance", "force_advance"})


def _violations(tree: ast.AST, label: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _LEAGUES_WRITE_SQL.search(node.value):
                found.append(f"{label}:{node.lineno}: SQL that writes `leagues`")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            owner_name = owner.id if isinstance(owner, ast.Name) else None
            if owner_name == "league_repo" and node.func.attr in _LEAGUE_WRITERS:
                found.append(f"{label}:{node.lineno}: league_repo.{node.func.attr}(...)")
            if owner_name == "phase_service" and node.func.attr in _POINTER_MOVERS:
                found.append(f"{label}:{node.lineno}: phase_service.{node.func.attr}(...)")
    return found


def _bot_files() -> list[Path]:
    return sorted(BOT_DIR.rglob("*.py"))


def test_the_scan_sees_the_bot_package():
    files = _bot_files()
    assert len(files) > 10, f"only {len(files)} files under {BOT_DIR}: scanning nothing"
    assert any(f.name == "surfaces.py" for f in files)


def test_nothing_under_dba2_bot_writes_a_leagues_column():
    violations: list[str] = []
    for path in _bot_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(_violations(tree, str(path.relative_to(BOT_DIR.parent.parent))))
    assert not violations, (
        "dba2/bot/ must write no `leagues` column (P5-99.m.1) -- the claim, the checkpoints "
        f"and the pointer are season_service's: {violations}"
    )


def test_the_sql_detector_trips_on_a_bot_module_that_writes_leagues():
    statement = "UPDATE leagues SET hook_completed_cycle_index = 5 WHERE id = $1"
    rogue = ast.parse(
        f"async def post(ex, league_id):\n    await ex.execute({statement!r}, league_id)\n"
    )
    assert _violations(rogue, "rogue.py")


def test_the_call_detector_trips_on_a_league_repo_writer_and_on_a_pointer_mover():
    for source in (
        "async def f(ex):\n    await league_repo.set_closing_work_completed_index(ex, 1, 2)\n",
        "async def f(ex):\n    await league_repo.claim_advance(ex, 1, 2)\n",
        "async def f(ex):\n    await phase_service.advance(ex, 1, None, 2)\n",
    ):
        assert _violations(ast.parse(source), "rogue.py"), source


def test_a_read_from_league_repo_is_not_a_violation():
    reader = ast.parse(
        "async def f(ex):\n"
        "    league = await league_repo.get_league_by_guild_id(ex, 1)\n"
        "    return await league_repo.count_phase_entries(ex, league.id, 2026)\n"
    )
    assert _violations(reader, "reader.py") == []
