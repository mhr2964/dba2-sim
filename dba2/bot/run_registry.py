"""The in-memory hold on the `/advance-run` that is going, per league
(docs/design/advance-run.md AR-4).

**What it guarantees, and what it does not.** At most ONE run per league per process, and a
cancel flag the run reads BETWEEN presses (`/advance-run-stop`, and a shutdown): the press in
flight finishes and commits, because a press is the unit of atomicity and a torn entry is only
recoverable through the next press's work prefix. **Across processes it is not a guarantee** --
like `block_registry`, it is per process. The guarantee there is the per-press claim: two runs in
two processes interleave presses, they never overlap one (R3).

**No locks, no I/O.** Every mutating method is synchronous and does only in-memory dict work, and
the bot runs on one asyncio event loop, so no other coroutine can interleave inside a method --
the identical single-event-loop argument `block_registry.py:27-35` makes. `begin` is the whole
single-run guard: a check and an insert with no `await` between them. The one `async` method,
`wait_for_all`, only waits on events; it changes no registry state.

**Shutdown awaits, it does not merely flag.** Setting `cancel` and closing the connection would
tear down the very press the flag was meant to let finish (the shutdown lands *inside* a press
almost every time: a run holds the press path for ~166 consecutive presses). `client.close` sets
every run's `cancel` and then awaits `wait_for_all` with `SHUTDOWN_GRACE_SECONDS` -- one press's
worth, `FREE_AGENCY`'s hook being the longest measured -- before `super().close()`. A press that
outlives the grace is still cancelled mid-press and leaves its claim to `/advance-unlock`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from dba2.domain.phase import Phase

SHUTDOWN_GRACE_SECONDS = 4.0
"""How long `client.close` waits for the runs to end after setting their cancel flags."""


@dataclass
class LeagueRun:
    """One league's run. `cancel` is set by `/advance-run-stop` or by shutdown and read by the
    run between presses; `finished` is set by `RunRegistry.end` once the whole command has
    ended (its last press AND its posts), which is what shutdown awaits. `presses` and `phase`
    are the run's own progress, updated by it after each press so a refused second run can say
    how far the first has got."""

    league_id: int
    user_id: int
    started_at: datetime
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    presses: int = 0
    phase: Phase | None = None


class RunRegistry:
    """Per-league holder of at most one `LeagueRun`. Synchronous and single-event-loop-only;
    see the module docstring for why that needs no locking."""

    def __init__(self) -> None:
        self._runs: dict[int, LeagueRun] = {}

    def begin(
        self, *, league_id: int, user_id: int, started_at: datetime, phase: Phase | None = None
    ) -> LeagueRun | None:
        """Register a run for `league_id`, or return `None` if one is already going (read
        `current` for who holds it). Check and insert with no `await` between them: this IS
        the single-run guard."""
        if league_id in self._runs:
            return None
        run = LeagueRun(league_id=league_id, user_id=user_id, started_at=started_at, phase=phase)
        self._runs[league_id] = run
        return run

    def current(self, league_id: int) -> LeagueRun | None:
        return self._runs.get(league_id)

    def request_stop(self, league_id: int) -> LeagueRun | None:
        """Ask the league's run to stop after its current press; returns it, or `None` when
        nothing is running."""
        run = self._runs.get(league_id)
        if run is not None:
            run.cancel.set()
        return run

    def end(self, run: LeagueRun) -> None:
        """Release `run`'s slot and mark it finished. Idempotent, and it frees the slot only
        if `run` still holds it, so a stale caller cannot evict a newer run."""
        if self._runs.get(run.league_id) is run:
            del self._runs[run.league_id]
        run.finished.set()

    def active(self) -> tuple[LeagueRun, ...]:
        return tuple(self._runs.values())

    def cancel_all(self) -> int:
        """Set every run's cancel flag (shutdown); returns how many runs were going."""
        runs = self.active()
        for run in runs:
            run.cancel.set()
        return len(runs)

    async def wait_for_all(self, grace: float = SHUTDOWN_GRACE_SECONDS) -> bool:
        """Wait up to `grace` seconds for every run going right now to end. `True` when they
        all did; `False` when the grace ran out first (the caller decides what that means --
        `client.close` logs it and carries on closing). Waits on the runs' own `finished`
        events, so it never cancels a run itself."""
        pending = [run.finished for run in self.active()]
        if not pending:
            return True
        try:
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in pending)), timeout=grace
            )
        except TimeoutError:
            return False
        return True
