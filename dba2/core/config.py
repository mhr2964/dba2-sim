"""Config loader.

Lazy by design: nothing is read from the environment at import time. v1's
core/config.py instantiated a module-level `config = Config.from_env()`,
which meant importing `core.config` anywhere — including in a test that only
wants the error hierarchy — crashed the process if `.env` wasn't populated.
`get_config()` defers that read until something actually needs it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


def _env_bool(name: str, default: bool) -> bool:
    """Strict: an unrecognized value raises rather than silently meaning
    False -- a typo in `MEDIA_AI_ENABLED` must not read as a deliberate off."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise RuntimeError(f"{name}={raw!r} is not a boolean (use true/false).")


def _env_number(name: str, default, cast):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = cast(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name}={raw!r} is not a valid {cast.__name__}.") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive, got {raw!r}.")
    return value


@dataclass(frozen=True)
class Config:
    database_url: str
    discord_token: str | None = None
    log_level: str = "INFO"
    media_ai_enabled: bool = False
    media_ai_cli_path: str | None = None
    media_ai_model: str = "sonnet"
    media_ai_effort: str = "low"
    media_ai_personas: str = ""
    media_ai_max_stories_per_cycle: int = 6
    media_ai_timeout_s: float = 90.0
    media_ai_concurrency: int = 2
    media_ai_max_attempts: int = 3
    media_ai_max_calls_per_cycle: int | None = None
    """The `MEDIA_AI_*` block (docs/design/media-v2-step8-anthropic-writer.md
    S8-3). Holds NO credential on purpose: the AI writer runs on the
    commissioner's `claude` subscription login, and a key present in the
    environment would silently bill the API instead (the writer scrubs it
    from the child process). `media_ai_enabled=False` is today's behavior,
    byte for byte. `media_ai_personas` is comma-separated persona keys, empty
    meaning every persona -- the rollout knob, orthogonal to which subjects a
    persona may cover. `media_ai_max_attempts` is how many calls one story
    may take when the gates reject its text (3 = up to 2 retries; 1 is the
    original no-retry behavior). `media_ai_max_calls_per_cycle` caps the
    calls a whole pass may spend, retries included; unset, it is
    `media_ai_max_stories_per_cycle * media_ai_max_attempts`."""

    @property
    def media_ai_persona_keys(self) -> frozenset[str]:
        """Parsed `media_ai_personas`; empty = no restriction (every persona)."""
        return frozenset(k.strip() for k in self.media_ai_personas.split(",") if k.strip())

    @property
    def media_ai_call_budget(self) -> int:
        """The per-pass call cap: the configured value, else every capped
        story's every attempt. A property rather than a from_env default so a
        `dataclasses.replace` of the cap or the attempts moves it too."""
        if self.media_ai_max_calls_per_cycle is not None:
            return self.media_ai_max_calls_per_cycle
        return self.media_ai_max_stories_per_cycle * self.media_ai_max_attempts

    @classmethod
    def from_env(cls) -> Config:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL not set — copy .env.example to .env and fill it in."
            )
        return cls(
            database_url=database_url,
            discord_token=os.getenv("DISCORD_TOKEN"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            media_ai_enabled=_env_bool("MEDIA_AI_ENABLED", False),
            media_ai_cli_path=os.getenv("MEDIA_AI_CLI_PATH") or None,
            media_ai_model=os.getenv("MEDIA_AI_MODEL") or "sonnet",
            media_ai_effort=os.getenv("MEDIA_AI_EFFORT") or "low",
            media_ai_personas=os.getenv("MEDIA_AI_PERSONAS", ""),
            media_ai_max_stories_per_cycle=_env_number(
                "MEDIA_AI_MAX_STORIES_PER_CYCLE", 6, int
            ),
            media_ai_timeout_s=_env_number("MEDIA_AI_TIMEOUT_S", 90.0, float),
            media_ai_concurrency=_env_number("MEDIA_AI_CONCURRENCY", 2, int),
            media_ai_max_attempts=_env_number("MEDIA_AI_MAX_ATTEMPTS", 3, int),
            media_ai_max_calls_per_cycle=_env_number("MEDIA_AI_MAX_CALLS_PER_CYCLE", None, int),
        )


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config


def reset_config_cache() -> None:
    """Test hook — clears the memoized Config so a test can swap env vars."""
    global _config
    _config = None
