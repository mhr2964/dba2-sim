"""Fixtures for every bot unit test."""

from __future__ import annotations

import asyncio

import pytest

from dba2.bot import game_feed


@pytest.fixture(autouse=True)
def no_real_reveal_backoff(monkeypatch):
    """`game_feed` waits between reveal retries (`_retry_sleep`); a unit test that
    fails a Discord call on purpose must not spend that time. Yield once, as a
    real sleep would, so other coroutines still run."""

    async def instant(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(game_feed, "_retry_sleep", instant)
