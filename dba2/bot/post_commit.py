"""`run_after_commit`/`honest_suffix` -- the shared "a follow-up step must
never turn an already-committed write into a false 'nothing happened'
message" pattern (Stage C's own B6/§42.1 shape, docs/design/phase-5-
discord-presentation.md).

`roster.py::_safe_team_panel`/`_safe_get_player` and `commissioner.py::
_reconcile_gm_channel_after_commit` were independent, pre-existing copies
of the identical idea -- extracted here (Stage D' critic, round 2 nit)
before a third copy (`offers.py`, the trade-offer review surface, not
built this dispatch) made it three. Lives in `bot/` (not `services/`)
because this is Discord-reply UX shaping, not a service-layer contract --
the service layer's write has already committed and returned before any
of this runs.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from dba2.core.logging import get_logger

log = get_logger(__name__)


async def run_after_commit[T](coro: Callable[[], Awaitable[T]], *, log_context: str) -> T | None:
    """Runs `coro()` strictly AFTER a write has already committed -- never
    raises. A failure here means the DB change is real but this particular
    follow-up (a re-fetch for a confirmation panel, a Discord channel
    reconcile, whatever the caller is doing) couldn't complete; the
    caller's own `honest_suffix`-built message is the right way to
    disclose that, never a swallowed exception and never a false "nothing
    happened" framing (Stage C's own six-critic-round case study in why a
    narrow guard keeps recurring, docs/design/phase-5-discord-
    presentation.md §42.1). `log_context` is a short, human-readable
    description of what was being attempted, logged alongside the
    traceback (`log.exception`) so an operator can tell which follow-up
    failed without re-deriving it from a bare stack trace.

    Returns `None` on any exception. If `coro`'s own successful return
    value can legitimately be `None` too (a bare side effect with no
    return value, e.g. `commissioner.py`'s Discord-channel reconcile
    call), the caller's `coro` should return an explicit non-`None`
    sentinel on success (`True`, the fetched row, etc.) so `result is
    None` unambiguously means "the follow-up failed," never "it
    succeeded and returned nothing.\""""
    try:
        return await coro()
    except Exception:
        log.exception("post-commit follow-up failed: %s", log_context)
        return None


def honest_suffix(detail: str) -> str:
    """The shared wording for a follow-up failure. `roster.py`'s panel/
    player suffixes and `commissioner.py`'s GM-channel suffix were three
    independently-worded copies of the identical fact (the write went
    through; only this specific follow-up didn't) -- one template now,
    filled in per call site with what specifically couldn't be shown or
    completed."""
    return f" (The change went through, but {detail} -- error logged.)"
