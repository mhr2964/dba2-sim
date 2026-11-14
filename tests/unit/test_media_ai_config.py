"""`MEDIA_AI_*` config fields (docs/design/media-v2-step8-anthropic-writer.md
S8-3). The switch defaults OFF and a typo must never read as a deliberate off."""
from __future__ import annotations

import dataclasses

import pytest

from dba2.core.config import Config

_MEDIA_ENV = (
    "MEDIA_AI_ENABLED", "MEDIA_AI_CLI_PATH", "MEDIA_AI_MODEL", "MEDIA_AI_EFFORT",
    "MEDIA_AI_PERSONAS", "MEDIA_AI_MAX_STORIES_PER_CYCLE", "MEDIA_AI_TIMEOUT_S",
    "MEDIA_AI_CONCURRENCY", "MEDIA_AI_MAX_ATTEMPTS", "MEDIA_AI_MAX_CALLS_PER_CYCLE",
)


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    for name in _MEDIA_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults_are_off_and_match_the_design_table(clean_env):
    cfg = Config.from_env()
    assert cfg.media_ai_enabled is False
    assert cfg.media_ai_cli_path is None
    assert (cfg.media_ai_model, cfg.media_ai_effort) == ("sonnet", "low")
    assert cfg.media_ai_personas == "" and cfg.media_ai_persona_keys == frozenset()
    assert cfg.media_ai_max_stories_per_cycle == 6
    assert cfg.media_ai_timeout_s == 90.0
    assert cfg.media_ai_concurrency == 2
    assert cfg.media_ai_max_attempts == 3
    assert cfg.media_ai_max_calls_per_cycle is None
    assert cfg.media_ai_call_budget == 18  # 6 stories x 3 attempts


def test_call_budget_follows_the_cap_and_attempts_unless_set():
    cfg = Config(database_url="x", media_ai_max_stories_per_cycle=4, media_ai_max_attempts=2)
    assert cfg.media_ai_call_budget == 8
    assert dataclasses.replace(cfg, media_ai_max_attempts=1).media_ai_call_budget == 4
    assert dataclasses.replace(cfg, media_ai_max_calls_per_cycle=5).media_ai_call_budget == 5


def test_config_has_no_credential_field():
    names = {f.name for f in dataclasses.fields(Config)}
    media_names = {n for n in names if n.startswith("media_ai_")}
    assert not any("key" in n or "token" in n or "secret" in n for n in media_names)


def test_every_field_reads_its_env_var(clean_env):
    clean_env.setenv("MEDIA_AI_ENABLED", "TRUE")
    clean_env.setenv("MEDIA_AI_CLI_PATH", "C:/x/claude.cmd")
    clean_env.setenv("MEDIA_AI_MODEL", "opus")
    clean_env.setenv("MEDIA_AI_EFFORT", "medium")
    clean_env.setenv("MEDIA_AI_PERSONAS", " insider , historian ,")
    clean_env.setenv("MEDIA_AI_MAX_STORIES_PER_CYCLE", "3")
    clean_env.setenv("MEDIA_AI_TIMEOUT_S", "12.5")
    clean_env.setenv("MEDIA_AI_CONCURRENCY", "1")
    clean_env.setenv("MEDIA_AI_MAX_ATTEMPTS", "1")
    clean_env.setenv("MEDIA_AI_MAX_CALLS_PER_CYCLE", "7")
    cfg = Config.from_env()
    assert cfg.media_ai_enabled is True
    assert cfg.media_ai_cli_path == "C:/x/claude.cmd"
    assert (cfg.media_ai_model, cfg.media_ai_effort) == ("opus", "medium")
    assert cfg.media_ai_persona_keys == frozenset({"insider", "historian"})
    assert cfg.media_ai_max_stories_per_cycle == 3
    assert cfg.media_ai_timeout_s == 12.5
    assert cfg.media_ai_concurrency == 1
    assert cfg.media_ai_max_attempts == 1
    assert cfg.media_ai_max_calls_per_cycle == 7 and cfg.media_ai_call_budget == 7


@pytest.mark.parametrize(
    "raw,expected", [("false", False), ("0", False), ("", False), ("on", True)]
)
def test_enabled_accepts_the_usual_spellings(clean_env, raw, expected):
    clean_env.setenv("MEDIA_AI_ENABLED", raw)
    assert Config.from_env().media_ai_enabled is expected


@pytest.mark.parametrize(
    "name,raw",
    [
        ("MEDIA_AI_ENABLED", "ture"),
        ("MEDIA_AI_CONCURRENCY", "two"),
        ("MEDIA_AI_CONCURRENCY", "0"),
        ("MEDIA_AI_TIMEOUT_S", "-1"),
        ("MEDIA_AI_MAX_STORIES_PER_CYCLE", "1.5"),
        ("MEDIA_AI_MAX_ATTEMPTS", "0"),
        ("MEDIA_AI_MAX_ATTEMPTS", "three"),
        ("MEDIA_AI_MAX_ATTEMPTS", "2.5"),
        ("MEDIA_AI_MAX_CALLS_PER_CYCLE", "-3"),
        ("MEDIA_AI_MAX_CALLS_PER_CYCLE", "lots"),
    ],
)
def test_a_malformed_value_fails_loudly_instead_of_silently_defaulting(clean_env, name, raw):
    clean_env.setenv(name, raw)
    with pytest.raises(RuntimeError, match=name):
        Config.from_env()
