"""`/advance-run`'s loop: press `season_service.press` until the league stands in a target
phase or something stops it (docs/design/advance-run.md AR-1 to AR-6).

**Why a module of its own, over the public `press`.** `season_service.py` is the file five
critic rounds signed off, and the loop needs no private helper of it: `press`,
`PressReport`, `permissions.require_commissioner` and `graph.route_to` are all public. The
stop conditions are league rules, so they live here -- under `services/`, where the walk
harness can drive them -- and not in the cog (P5-99.m's spirit). Discord-free: no
`dba2.bot` import, nothing is posted, plain types come back.

**A loop of claims, not one long claim (AR-3).** `press` takes and releases the claim
itself, so a run is ~166 claims. A human write can land between two presses -- exactly what
can happen between two manual `/advance` presses today.

**The target rule.** The run stops on the `ENTRY` press that *enters* the target and never
makes a press while standing in it. The route is computed up front through the same
`_DEFAULT_SUCCESSOR` branch rule `press` follows, so a reachable target cannot be overshot,
and the check is `to_phase is target`, not "target is in the route". A move by another
command in a claim gap can still skip or land in the target, so after every press the phase
the league now stands in is checked too (`_standing_stop`): a target no longer reachable is
`TARGET_PASSED`, one already stood in is `TARGET_REACHED`, and the overshoot is at most one
press.

**Every run ends in exactly one `RunStop`.** `run_stop_reason` is pure and total over the
three enums it reads -- `PressKind`, `BlockerCode`, `DraftCycleState` -- each pinned at import
with a real `raise` (an `assert` is stripped by `python -O`): a member added to any of them
fails the import until a row names it, which is how a real cause could otherwise fall through
to the no-progress net.

**The no-progress guard is a net for the unnamed, and no league fixture can drive it today**
(AR-6): every press that makes no progress is either named (the two draft cases, the blocked
kinds) or raises (`GamesFailedToSimError`). It is tested over synthetic counters.

**Caps are checked BETWEEN presses**, so they are soft by exactly one press: a press is
never interrupted, because a torn entry is only recoverable through the next press's work
prefix (P5-99.b.1).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter

import asyncpg

from dba2.core.errors import NotFoundError, PhaseError
from dba2.core.logging import get_logger
from dba2.data.repositories import league_repo
from dba2.domain.phase import Phase
from dba2.phase import permissions
from dba2.phase.graph import route_to
from dba2.services import season_service
from dba2.services.gm_service import DraftCycleOutcome, DraftCycleState
from dba2.services.media_service import LoadedMediaLibrary
from dba2.services.season_service import BlockerCode, PressKind, PressReport

log = get_logger(__name__)

RUN_MAX_PRESSES = 400
"""The runaway bound the walk test already used (`_MAX_PRESSES`): a season is 168 presses."""

NO_PROGRESS_LIMIT = 2
"""Consecutive presses that changed nothing before the run stops (AR-6)."""


class RunStop(StrEnum):
    """Why a run ended -- exactly one per run, and each has its own sentence (AR-5)."""

    TARGET_REACHED = "target_reached"
    TARGET_PASSED = "target_passed"
    BLOCKED = "blocked"
    NEEDS_A_DRAFT_BOARD = "needs_a_draft_board"
    SEASON_OVER = "season_over"
    NEEDS_A_HUMAN_PICK = "needs_a_human_pick"
    DRAFT_STALLED = "draft_stalled"
    PRESS_FAILED = "press_failed"
    CANCELLED = "cancelled"
    CAP_REACHED = "cap_reached"
    NO_PROGRESS = "no_progress"


@dataclass(frozen=True)
class RunCaps:
    """The two soft caps (AR-2), parameters so a test drives a two-press run without
    patching a clock. `deadline` is `interaction.created_at + 12 minutes` in the cog -- the
    15-minute token is measured from the interaction's creation, not the loop's start --
    and `None` means no deadline."""

    deadline: datetime | None = None
    max_presses: int = RUN_MAX_PRESSES


@dataclass(frozen=True)
class RunState:
    """The run's counters at the moment a stop is asked about: what `run_stop_reason` reads
    besides the last report. `idle_streak` is the consecutive presses that made no progress;
    `now` is passed in so the function stays pure."""

    presses: int
    idle_streak: int
    now: datetime
    cancelled: bool = False


@dataclass(frozen=True)
class RunStep:
    """One press of the run: its 1-based `number`, the `PressReport`, and `seconds` the press
    itself took (the walk test's per-phase timing)."""

    number: int
    report: PressReport
    seconds: float


@dataclass(frozen=True)
class RunReport:
    """What a run did. `error` is set exactly when `stop` is `PRESS_FAILED` (the exception
    `press` raised, already logged with its traceback); `elapsed_seconds` is the whole run's
    wall time. Everything else is derived from `steps`, so nothing here can disagree with
    what the presses reported."""

    from_phase: Phase
    target: Phase
    stop: RunStop
    steps: tuple[RunStep, ...]
    elapsed_seconds: float
    error: Exception | None = None

    @property
    def presses(self) -> int:
        return len(self.steps)

    @property
    def last_report(self) -> PressReport | None:
        return self.steps[-1].report if self.steps else None

    @property
    def final_phase(self) -> Phase:
        """The phase the league stands in now (as far as the run saw it)."""
        last = self.last_report
        return self.from_phase if last is None else standing_phase(last)

    @property
    def phases_entered(self) -> tuple[Phase, ...]:
        return tuple(
            s.report.to_phase
            for s in self.steps
            if s.report.kind is PressKind.ENTRY and s.report.to_phase is not None
        )

    @property
    def presses_by_kind(self) -> dict[PressKind, int]:
        counts = {kind: 0 for kind in PressKind}
        for step in self.steps:
            counts[step.report.kind] += 1
        return counts

    @property
    def game_days(self) -> int:
        """Distinct game dates simmed: a day a games target splits is two presses, one day."""
        return len({s.report.block.block_id for s in self.steps if s.report.block is not None})

    @property
    def games(self) -> int:
        return sum(len(s.report.block.games) for s in self.steps if s.report.block is not None)

    @property
    def first_game_date(self) -> str | None:
        ids = sorted(s.report.block.block_id for s in self.steps if s.report.block is not None)
        return ids[0] if ids else None

    @property
    def last_game_date(self) -> str | None:
        ids = sorted(s.report.block.block_id for s in self.steps if s.report.block is not None)
        return ids[-1] if ids else None

    @property
    def media_cycles(self) -> tuple[int, ...]:
        return tuple(
            s.report.media_cycle_index for s in self.steps if s.report.media_cycle_index is not None
        )

    @property
    def failed_games(self) -> tuple[tuple[int, str], ...]:
        """`(game_id, cause)` for every game a block press failed to sim, in press order. A
        game that failed and was retried successfully by a later press is still listed: it
        is what the run saw."""
        out: list[tuple[int, str]] = []
        for step in self.steps:
            causes = {f.game_id: f.cause for f in step.report.failures}
            out.extend(
                (game_id, causes.get(game_id, "cause not recorded"))
                for game_id in step.report.failed_games
            )
        return tuple(out)

    @property
    def draft_stop(self) -> DraftCycleOutcome | None:
        """The last press's draft cycle outcome -- who is on the clock, or why the board
        stalled -- for the two draft stops' sentences."""
        last = self.last_report
        return draft_cycle_of(last) if last is not None else None

    @property
    def expired_offers(self) -> int:
        return sum(s.report.expired_offers for s in self.steps)

    def ai_counts_by_phase(self) -> dict[Phase, AiCounts]:
        """Per-phase AI trades, signings, waiver cuts and re-sign offers, in first-seen
        order (R2's audit trail). Each hook is credited to the phase it ran FOR
        (`PhaseHookResult.phase`), which for a hook a work prefix finished is the phase the
        league was standing in, not the one the press entered."""
        totals: dict[Phase, list[int]] = {}
        for step in self.steps:
            for hook in (step.report.resumed_hook_result, step.report.hook_result):
                if hook is None:
                    continue
                row = totals.setdefault(hook.phase, [0, 0, 0, 0])
                if hook.trade_window is not None:
                    row[0] += len(hook.trade_window.executions)
                if hook.free_agency is not None:
                    row[1] += len(hook.free_agency.signings)
                if hook.waivers is not None:
                    row[2] += len(hook.waivers.executions)
                if hook.resign_offers is not None:
                    row[3] += len(hook.resign_offers)
        return {
            phase: AiCounts(trades=t, signings=s, waiver_cuts=w, resign_offers=r)
            for phase, (t, s, w, r) in totals.items()
            if t or s or w or r
        }


@dataclass(frozen=True)
class AiCounts:
    trades: int = 0
    signings: int = 0
    waiver_cuts: int = 0
    resign_offers: int = 0


def standing_phase(report: PressReport) -> Phase:
    """The phase the league stands in once `report`'s press has committed: the phase an entry
    moved it into, else the one the press ran in."""
    if report.kind is PressKind.ENTRY and report.to_phase is not None:
        return report.to_phase
    return report.from_phase


def draft_cycle_of(report: PressReport) -> DraftCycleOutcome | None:
    """The draft cycle a press reported, from whichever of its two hook results carries
    one (a work prefix can finish a crashed DRAFT hook, `resumed_hook_result`)."""
    for hook in (report.hook_result, report.resumed_hook_result):
        if hook is not None and hook.draft_cycle is not None:
            return hook.draft_cycle
    return None


def made_progress(report: PressReport) -> bool:
    """AR-6's definition: a pointer move, a game finalized, a draft pick or a media cycle."""
    if report.kind is PressKind.ENTRY:
        return True
    if report.block is not None and report.block.games:
        return True
    if report.media_cycle_index is not None:
        return True
    return any(
        hook is not None and hook.draft is not None and hook.draft.picks
        for hook in (report.hook_result, report.resumed_hook_result)
    )


# --- the stop map: total over PressKind x BlockerCode x DraftCycleState ---------------------

_STOP_BY_BLOCKER: dict[BlockerCode, RunStop] = {
    BlockerCode.SCHEDULE_MISSING: RunStop.BLOCKED,
    BlockerCode.GAMES_TARGET_NOT_REACHED: RunStop.BLOCKED,
    BlockerCode.REGULAR_SEASON_UNFINISHED: RunStop.BLOCKED,
    BlockerCode.DRAFT_NOT_COMPLETE: RunStop.BLOCKED,
    BlockerCode.WAIVERS_EDGE: RunStop.SEASON_OVER,
    BlockerCode.DRAFT_BOARD_NOT_READY: RunStop.NEEDS_A_DRAFT_BOARD,
}
"""Row 2 for every code that is only a blocker; row 3 and row 4 for the two that have a
sentence of their own."""

_BLOCKER_PRIORITY = (RunStop.NEEDS_A_DRAFT_BOARD, RunStop.SEASON_OVER, RunStop.BLOCKED)
"""A blocked verdict can carry several codes; the most specific stop names it."""

_STOP_BY_DRAFT_STATE: dict[DraftCycleState, RunStop | None] = {
    DraftCycleState.WAITING_ON_HUMAN: RunStop.NEEDS_A_HUMAN_PICK,
    DraftCycleState.COMPLETE: None,  # the next press is the entry out of DRAFT
    DraftCycleState.STALLED: RunStop.DRAFT_STALLED,
    # `run_draft_cycle` never produces it (it belongs to `gm_command_service.draft_pick`),
    # and it is still mapped: an unnamed member is how the no-progress net swallows a cause.
    DraftCycleState.SKIPPED: RunStop.DRAFT_STALLED,
}


def _entry_stop(report: PressReport, target: Phase) -> RunStop | None:
    if report.to_phase is target:
        return RunStop.TARGET_REACHED
    # An entry INTO `DRAFT` runs the DRAFT hook and carries its cycle in `hook_result`. The
    # cog has already announced it; if the run pressed on, the next press would be a
    # `DRAFT_RESUME` that re-runs the hook and announces the same person again. Read
    # `hook_result` alone, never `draft_cycle_of`: on an entry OUT of `DRAFT` the DRAFT hook
    # a work prefix finished is `resumed_hook_result` and is COMPLETE, and it is not a reason
    # to stop (`COMPLETE` maps to `None` anyway, so the row is the same for both).
    hook = report.hook_result
    if hook is not None and hook.draft_cycle is not None:
        return _STOP_BY_DRAFT_STATE[hook.draft_cycle.state]
    return None


def _block_stop(report: PressReport, target: Phase) -> RunStop | None:
    return None  # a game day was simmed (or the press raised): the loop goes on


def _blocked_stop(report: PressReport, target: Phase) -> RunStop | None:
    reached = {_STOP_BY_BLOCKER[b.code] for b in report.verdict.blockers}
    for stop in _BLOCKER_PRIORITY:
        if stop in reached:
            return stop
    return RunStop.BLOCKED  # a BLOCKED press that names no blocker is still a stop


def _draft_resume_stop(report: PressReport, target: Phase) -> RunStop | None:
    cycle = draft_cycle_of(report)
    # A `DRAFT_RESUME` always carries its cycle (`PressReport`'s docstring). One that does
    # not is left to the no-progress net rather than guessed at.
    return None if cycle is None else _STOP_BY_DRAFT_STATE[cycle.state]


def _standing_stop(standing: Phase, target: Phase) -> RunStop | None:
    """Where the league stands against the target, whatever the press was. `_entry_stop` sees
    only the press's own entry, so a move made by another command in a claim gap (AR-3) --
    `/advance-force` or `/advance-to` past the target, or into it -- would otherwise be pressed
    straight through, hundreds of unrequested presses until `WAIVERS_EDGE` or a cap. `route_to`
    is pure: `None` is a target the league can no longer reach, `()` one it is standing in."""
    route = route_to(standing, target)
    if route is None:
        return RunStop.TARGET_PASSED
    return None if route else RunStop.TARGET_REACHED


_STOP_BY_KIND: dict[PressKind, Callable[[PressReport, Phase], RunStop | None]] = {
    PressKind.ENTRY: _entry_stop,
    PressKind.BLOCK: _block_stop,
    PressKind.BLOCKED: _blocked_stop,
    PressKind.DRAFT_RESUME: _draft_resume_stop,
}


def _require_a_row_for_every_member(
    name: str, enum: type[StrEnum], mapping: dict
) -> None:
    """Every member of `enum` has a row in `mapping`, checked at import with a real `raise`
    (an `assert` is stripped by `python -O`) -- `cogs/advance.py`'s
    `_require_a_reply_for_every_press_kind` shape."""
    missing = sorted(member.value for member in set(enum) - set(mapping))
    unknown = sorted(str(member) for member in set(mapping) - set(enum))
    if missing or unknown:
        raise RuntimeError(
            f"the run's {name} stop map and {enum.__name__} disagree: no row for {missing}, "
            f"rows for unknown members {unknown}"
        )


_require_a_row_for_every_member("press-kind", PressKind, _STOP_BY_KIND)
_require_a_row_for_every_member("blocker-code", BlockerCode, _STOP_BY_BLOCKER)
_require_a_row_for_every_member("draft-state", DraftCycleState, _STOP_BY_DRAFT_STATE)


def run_stop_reason(
    report: PressReport | None, *, target: Phase, caps: RunCaps, state: RunState
) -> RunStop | None:
    """Why the run must not make another press, or `None` to go on. Pure.

    `report` is the last press (`None` before the first). What the league says comes first --
    a target reached is never reported as a cap, and a stall is never reported as "no
    progress" -- then the no-progress net, the cancel and the caps. Row 7
    (`PRESS_FAILED`) is not decidable here: it is a `press` that raised, and `run_to` owns
    it."""
    if report is not None:
        stop = _STOP_BY_KIND[report.kind](report, target)
        if stop is not None:
            return stop
        stop = _standing_stop(standing_phase(report), target)
        if stop is not None:
            return stop
    if state.idle_streak >= NO_PROGRESS_LIMIT:
        return RunStop.NO_PROGRESS
    if state.cancelled:
        return RunStop.CANCELLED
    if state.presses >= caps.max_presses:
        return RunStop.CAP_REACHED
    if caps.deadline is not None and state.now >= caps.deadline:
        return RunStop.CAP_REACHED
    return None


def run_no_route_message(from_phase: Phase, to_phase: Phase) -> str:
    """`season_service.no_route_message`'s three clauses with this command's name: that one
    names `/advance-to` in its text."""
    return (
        f"There is no route from {from_phase.value} to {to_phase.value} within this season: "
        "/advance-run only goes forward, never crosses the season boundary, and never targets "
        "SETUP."
    )


def plan_route(from_phase: Phase, to_phase: Phase) -> tuple[Phase, ...]:
    """The phases a run from `from_phase` enters on its way to `to_phase`: the same pure
    `graph.route_to` `/advance-to` uses. Empty means the league is already there. Raises
    `PhaseError` when there is no route (a backwards target, `SETUP`, anything across
    `WAIVERS -> PRESEASON`, and every target from `WAIVERS`)."""
    route = route_to(from_phase, to_phase)
    if route is None:
        raise PhaseError(run_no_route_message(from_phase, to_phase))
    return route


def _utc_now() -> datetime:
    return datetime.now(UTC)


type StepCallback = Callable[[RunStep], Awaitable[None]]


async def run_to(
    pool: asyncpg.Pool,
    *,
    league_id: int,
    actor_user_id: int,
    to_phase: Phase,
    library: LoadedMediaLibrary,
    caps: RunCaps,
    should_continue: Callable[[], bool],
    on_step: StepCallback,
    clock: Callable[[], datetime] = _utc_now,
) -> RunReport:
    """Presses `/advance` until the league enters `to_phase` or a `RunStop` says otherwise.

    Commissioner-only, checked here as well as in the cog and by `press` on every press (a
    commissioner demoted mid-run stops on `PRESS_FAILED`). Raises `NotFoundError`,
    `PermissionDenied` or `PhaseError` (no route) BEFORE any press. A league already in
    `to_phase` returns `TARGET_REACHED` with no steps.

    `should_continue` is read between presses (a press is never interrupted). `on_step` is
    awaited after every press, with that press's report, and must not raise: the press has
    committed, so it is the cog's job to degrade a failed Discord call instead of aborting the
    run (a raise from it propagates and ends the run without a report). `clock` exists so a
    test can drive the deadline; it defaults to the real one.

    A `press` that raises ends the run as `PRESS_FAILED` with the exception on the report --
    logged here with its traceback so it is never swallowed -- and a cancellation propagates
    untouched."""
    league = await league_repo.get_league(pool, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")
    permissions.require_commissioner(
        actor_user_id, commissioner_user_id=league.commissioner_user_id, action="advance_run"
    )
    from_phase = league.current_phase
    if not plan_route(from_phase, to_phase):
        return RunReport(
            from_phase=from_phase,
            target=to_phase,
            stop=RunStop.TARGET_REACHED,
            steps=(),
            elapsed_seconds=0.0,
        )

    started = perf_counter()
    steps: list[RunStep] = []
    idle_streak = 0
    last: PressReport | None = None
    error: Exception | None = None
    while True:
        stop = run_stop_reason(
            last,
            target=to_phase,
            caps=caps,
            state=RunState(
                presses=len(steps),
                idle_streak=idle_streak,
                now=clock(),
                cancelled=not should_continue(),
            ),
        )
        if stop is not None:
            break
        press_started = perf_counter()
        try:
            report = await season_service.press(
                pool, league_id=league_id, actor_user_id=actor_user_id, library=library
            )
        except Exception as exc:
            log.warning(
                "advance-run: league %s's press %d failed after %d committed press(es)",
                league_id,
                len(steps) + 1,
                len(steps),
                exc_info=exc,
            )
            stop, error = RunStop.PRESS_FAILED, exc
            break
        step = RunStep(number=len(steps) + 1, report=report, seconds=perf_counter() - press_started)
        steps.append(step)
        last = report
        idle_streak = 0 if made_progress(report) else idle_streak + 1
        await on_step(step)
    return RunReport(
        from_phase=from_phase,
        target=to_phase,
        stop=stop,
        steps=tuple(steps),
        elapsed_seconds=perf_counter() - started,
        error=error,
    )
