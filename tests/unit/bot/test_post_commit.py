"""`dba2/bot/post_commit.py` -- the shared "run a follow-up after a write
has already committed, never let it raise, produce an honest partial-
success suffix on failure" pattern (Stage D' critic, round 2 nit).
`roster.py`/`commissioner.py`'s own integration tests exercise this
indirectly through real commands; this file is the direct, fast unit
proof of the two functions themselves."""
from __future__ import annotations

import pytest

from dba2.bot.post_commit import honest_suffix, run_after_commit


async def test_run_after_commit_returns_the_coroutines_own_result_on_success():
    async def _ok() -> str:
        return "fetched"

    assert await run_after_commit(_ok, log_context="test") == "fetched"


async def test_run_after_commit_returns_none_and_does_not_raise_on_any_exception():
    async def _boom() -> str:
        raise RuntimeError("simulated post-commit failure")

    # Must not raise -- that is the entire contract this function exists
    # to provide (a failed follow-up must never re-propagate past an
    # already-committed write).
    assert await run_after_commit(_boom, log_context="test") is None


async def test_run_after_commit_distinguishes_a_real_none_success_from_a_failure_via_sentinel():
    """`commissioner.py::_reconcile_gm_channel_after_commit`'s own real
    shape: a wrapped coroutine with no meaningful return value must return
    an explicit non-`None` sentinel on success, since `run_after_commit`
    itself returns `None` on failure -- without the sentinel the two are
    indistinguishable to the caller."""

    async def _side_effect_only() -> bool:
        return True  # explicit sentinel, not a bare `None` return

    result = await run_after_commit(_side_effect_only, log_context="test")
    assert result is True


def test_honest_suffix_states_the_write_succeeded_and_names_the_detail():
    suffix = honest_suffix("the updated team panel couldn't be shown")
    assert "went through" in suffix
    assert "the updated team panel couldn't be shown" in suffix
    assert "error logged" in suffix


def test_honest_suffix_is_one_shared_template_for_every_caller():
    """The whole point of extracting this (round 2 nit): `roster.py`'s two
    suffixes and `commissioner.py`'s one must render from the identical
    template, differing only in `detail`."""
    panel = honest_suffix("the updated team panel couldn't be shown")
    player = honest_suffix("the updated player info couldn't be shown")
    prefix = " (The change went through, but "
    suffix = " -- error logged.)"
    assert panel.startswith(prefix) and panel.endswith(suffix)
    assert player.startswith(prefix) and player.endswith(suffix)


@pytest.mark.parametrize("detail", ["", "something specific failed"])
def test_honest_suffix_always_starts_with_a_leading_space(detail: str) -> None:
    # Every call site appends this directly to an existing sentence
    # (`f"Waived.{_PANEL_FOLLOWUP_FAILURE_SUFFIX}"`) -- a missing leading
    # space would glue it onto the prior sentence's final period.
    assert honest_suffix(detail).startswith(" ")
