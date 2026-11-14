"""`tools/newsroom_ai.py` and `media_service.preview_story_for_ai`
(docs/design/media-v2-step8-anthropic-writer.md S8-15 item 7). The print
mode makes no call; the live mode here uses a fake runner. Nothing spawns a
real `claude` process.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it."""
from __future__ import annotations

import json

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.services import media_service
from tests.integration.test_media_ai_pass import (
    Runner,
    _config,
    _make_league,
    _rows,
    _seed_cycle,
)
from tools import newsroom_ai


def _args(*argv):
    return newsroom_ai._parse_args(list(argv))


@pytest.mark.parametrize(
    "argv",
    [
        ["--print-prompt"],  # needs a story
        ["--live"],  # needs a story or a cycle
        ["--live", "--story-id", "1", "--cycle", "2", "--league-id", "1", "--apply"],
        ["--live", "--cycle", "2", "--apply"],  # needs a league
        ["--live", "--cycle", "2", "--league-id", "1"],  # the real pass needs --apply
        ["--live", "--story-id", "1", "--apply"],  # --apply is cycle-only
        ["--print-prompt", "--live", "--story-id", "1"],  # exclusive modes
    ],
)
def test_bad_argument_combinations_are_rejected(argv):
    with pytest.raises(SystemExit):
        _args(*argv)


def test_valid_argument_combinations_parse():
    assert _args("--print-prompt", "--story-id", "3").story_id == 3
    assert _args("--live", "--story-id", "3").live
    assert _args("--live", "--cycle", "2", "--league-id", "1", "--apply").apply


@pytest.mark.asyncio
async def test_print_prompt_mode_renders_the_exact_prompt_and_makes_no_call():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            library, stories = await _seed_cycle(pool, league_id, [0.9])
            story = stories["A"]
            preview = await media_service.preview_story_for_ai(
                pool, story_id=story.id, library=library, config=_config(), live=False
            )
            assert preview.outcome is None and preview.gate is None
            assert "You are " in preview.system_prompt
            assert "Return an object with exactly two string fields" in preview.system_prompt
            payload = json.loads(preview.payload)
            assert payload["story"]["subject"]["team_name"] == "A City A Team"
            assert payload["tier"] == story.format_tier
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_live_preview_gates_the_text_and_writes_nothing():
    pool = await get_pool()
    try:
        league_id = await _make_league(pool)
        try:
            library, stories = await _seed_cycle(pool, league_id, [0.9, 0.8])
            before = await _rows(pool, league_id)
            good = await media_service.preview_story_for_ai(
                pool, story_id=stories["A"].id, library=library, config=_config(),
                live=True, runner=Runner(),
            )
            assert good.gate.passed and good.would_be_body.endswith("in a fine run.")
            assert good.outcome.telemetry.input_tokens == 1500
            bad = await media_service.preview_story_for_ai(
                pool, story_id=stories["B"].id, library=library, config=_config(),
                live=True, runner=Runner(bad_codes={"B"}),
            )
            assert not bad.gate.passed and bad.gate.gate == "proper_nouns"
            failed = await media_service.preview_story_for_ai(
                pool, story_id=stories["A"].id, library=library, config=_config(),
                live=True, runner=Runner(exc=TimeoutError()),
            )
            assert failed.outcome.category == "timeout" and failed.gate is None
            assert await _rows(pool, league_id) == before  # the tool never writes
        finally:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_an_unknown_story_is_a_loud_lookup_error():
    pool = await get_pool()
    try:
        with pytest.raises(LookupError):
            await media_service.preview_story_for_ai(
                pool, story_id=-1, library=media_service.load_media_library(),
                config=_config(), live=False,
            )
    finally:
        await close_pool()
