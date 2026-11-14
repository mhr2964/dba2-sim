"""The `/advance` cog's newsroom-AI step (docs/design/media-v2-step8-anthropic-
writer.md S8-1, S8-11): inert when the flag is off, one honest notice when the
pass was partial or failed, and never able to fail the press."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dba2.bot.cogs import advance
from dba2.core.config import Config
from dba2.services import media_service
from dba2.services.media_service import RewriteSummary

_LEAGUE = SimpleNamespace(id=7, current_season=2026)


def _config(**kw) -> Config:
    return Config(database_url="postgresql://unused/unused", **kw)


def test_full_pass_and_capped_pass_say_nothing():
    assert advance._ai_pass_notice(RewriteSummary(attempted=4, accepted=4)) is None
    # Skipped because of MEDIA_AI_MAX_STORIES_PER_CYCLE is configuration, not failure.
    assert advance._ai_pass_notice(RewriteSummary(attempted=6, accepted=6, skipped=3)) is None
    assert advance._ai_pass_notice(RewriteSummary()) is None
    assert advance._ai_pass_notice(RewriteSummary(disabled_reason="disabled")) is None


def test_partial_pass_names_the_counts():
    text = advance._ai_pass_notice(RewriteSummary(attempted=6, accepted=3, calls=6))
    assert text == (
        "The newsroom AI wrote 3 of 6 stories (6 calls); the rest posted in the house "
        "style (logged)."
    )


def test_partial_pass_with_retries_names_the_retries_and_the_calls():
    """G2-18: how many stories needed a gate retry and the calls spent."""
    text = advance._ai_pass_notice(
        RewriteSummary(attempted=6, accepted=5, calls=10, retried=3, accepted_on_retry=2)
    )
    assert text == (
        "The newsroom AI wrote 5 of 6 stories (3 stories needed a retry, 10 calls in all); "
        "the rest posted in the house style (logged)."
    )
    one = advance._ai_pass_notice(RewriteSummary(attempted=2, accepted=1, calls=1, retried=1))
    assert "(1 story needed a retry, 1 call in all)" in one


def test_a_full_pass_that_retried_still_says_nothing():
    assert advance._ai_pass_notice(
        RewriteSummary(attempted=6, accepted=6, calls=9, retried=3, accepted_on_retry=3)
    ) is None


def test_a_pass_cut_short_by_its_call_budget_says_so_even_when_every_attempt_landed():
    text = advance._ai_pass_notice(
        RewriteSummary(attempted=4, accepted=4, calls=8, call_budget=8, retried=2,
                       budget_exhausted=True, skipped=2)
    )
    assert "wrote 4 of 4 stories (it reached its budget of 8 calls; 2 stories needed" in text


def test_aborted_pass_says_it_stopped_early():
    text = advance._ai_pass_notice(
        RewriteSummary(attempted=3, accepted=0, aborted=True, calls=3)
    )
    assert "0 of 3" in text and "stopped early" in text and "3 calls" in text


def test_a_pass_that_could_not_start_says_why():
    text = advance._ai_pass_notice(RewriteSummary(disabled_reason="claude CLI not found"))
    assert "could not run (claude CLI not found)" in text


async def test_flag_off_is_inert_never_loading_the_library_or_calling_the_pass(monkeypatch):
    monkeypatch.setattr(advance, "get_config", lambda: _config(media_ai_enabled=False))

    async def boom(*a, **k):
        raise AssertionError("must not be called with the flag off")

    monkeypatch.setattr(media_service, "rewrite_cycle_stories_with_ai", boom)
    monkeypatch.setattr(advance, "_media_library", boom)
    assert await advance._rewrite_wire_with_ai(object(), _LEAGUE, 4) is None


async def test_flag_on_runs_the_pass_for_the_leagues_season_and_cycle(monkeypatch):
    config = _config(media_ai_enabled=True)
    monkeypatch.setattr(advance, "get_config", lambda: config)
    seen = {}

    async def fake_library():
        return "LIB"

    async def fake_pass(pool, **kwargs):
        seen.update(kwargs)
        return RewriteSummary(attempted=2, accepted=1)

    monkeypatch.setattr(advance, "_media_library", fake_library)
    monkeypatch.setattr(media_service, "rewrite_cycle_stories_with_ai", fake_pass)
    notice = await advance._rewrite_wire_with_ai(object(), _LEAGUE, 4)
    assert seen == {
        "league_id": 7, "season": 2026, "cycle_index": 4, "library": "LIB", "config": config,
    }
    assert "1 of 2" in notice


async def test_a_crashing_pass_never_raises_and_yields_a_notice(monkeypatch):
    monkeypatch.setattr(advance, "get_config", lambda: _config(media_ai_enabled=True))

    async def fake_library():
        return "LIB"

    async def crash(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(advance, "_media_library", fake_library)
    monkeypatch.setattr(media_service, "rewrite_cycle_stories_with_ai", crash)
    notice = await advance._rewrite_wire_with_ai(object(), _LEAGUE, 4)
    assert notice is not None and "house style" in notice


@pytest.mark.parametrize("bad_config", [RuntimeError("no DATABASE_URL")])
async def test_an_unreadable_config_cannot_fail_the_press(monkeypatch, bad_config):
    def raising():
        raise bad_config

    monkeypatch.setattr(advance, "get_config", raising)
    assert "house style" in await advance._rewrite_wire_with_ai(object(), _LEAGUE, 4)
