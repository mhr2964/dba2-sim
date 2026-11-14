"""`dba2/services/season_run.py` -- `/advance-run`'s stop map and loop, with no database
(docs/design/advance-run.md AR-1 to AR-6, §3).

`run_stop_reason` is pure, so its table is asserted row by row and its totality over the
three enums it reads (`PressKind`, `BlockerCode`, `DraftCycleState`) by walking their
product. The loop, `run_to`, is driven with a scripted `press`: what is asserted is the
loop's own job (the target rule, the caps, the cancel, the failure and the no-progress net),
not the press.

**The no-progress guard is tested over synthetic counters, and that is a weaker guarantee
than a league fixture, labelled as one** (AR-6): no league can drive it today, because every
press that changes nothing is either named by a stop row or raises.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from dba2.core.errors import ConcurrencyError, PhaseError
from dba2.domain.phase import Phase
from dba2.phase.graph import route_to
from dba2.phase.permissions import PermissionDenied
from dba2.services import season_run, season_service
from dba2.services.game_sim_service import GameFailure, SimmedGame
from dba2.services.gm_service import (
    DraftCycleOutcome,
    DraftCycleReason,
    DraftCycleState,
    PhaseHookResult,
)
from dba2.services.season_run import (
    NO_PROGRESS_LIMIT,
    RunCaps,
    RunState,
    RunStop,
    made_progress,
    run_stop_reason,
    run_to,
)
from dba2.services.season_service import (
    Blocker,
    BlockerCode,
    ExitVerdict,
    PressKind,
    PressReport,
    SimmedBlock,
)

_COMMISSIONER = 1
_LEAGUE_ID = 7
_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
_ALLOWED = ExitVerdict(allowed=True, blockers=())
_NO_CAPS = RunCaps()


def _state(**overrides) -> RunState:
    base = RunState(presses=0, idle_streak=0, now=_NOW, cancelled=False)
    return replace(base, **overrides)


def _blocked(*codes: BlockerCode, phase: Phase = Phase.DRAFT_LOTTERY) -> PressReport:
    return PressReport(
        kind=PressKind.BLOCKED,
        from_phase=phase,
        verdict=ExitVerdict(
            allowed=False, blockers=tuple(Blocker(code=c, message=f"msg {c.value}") for c in codes)
        ),
    )


def _entry(to_phase: Phase, from_phase: Phase = Phase.PRESEASON, **overrides) -> PressReport:
    return PressReport(
        kind=PressKind.ENTRY,
        from_phase=from_phase,
        verdict=_ALLOWED,
        to_phase=to_phase,
        entry_index=1,
        **overrides,
    )


def _simmed(game_id: int, day: date) -> SimmedGame:
    return SimmedGame(
        game_id=game_id,
        game_date=day,
        home_team_id=1,
        away_team_id=2,
        result=SimpleNamespace(marker=game_id),
    )


def _block(day: date = date(2026, 10, 25), games: int = 2, **overrides) -> PressReport:
    base = PressReport(
        kind=PressKind.BLOCK,
        from_phase=Phase.REGULAR_SEASON,
        verdict=ExitVerdict(
            allowed=False,
            blockers=(Blocker(BlockerCode.GAMES_TARGET_NOT_REACHED, "games not played"),),
        ),
        block=SimmedBlock(
            block_id=day.isoformat(),
            season=2026,
            games=tuple(_simmed(i, day) for i in range(games)),
        ),
    )
    return replace(base, **overrides)


def _draft_resume(
    state: DraftCycleState, reason: DraftCycleReason | None = None, picks: int = 0
) -> PressReport:
    draft = SimpleNamespace(picks=tuple(range(picks))) if picks else None
    hook = PhaseHookResult(
        league_id=_LEAGUE_ID,
        season=2026,
        phase=Phase.DRAFT,
        draft=draft,
        draft_cycle=DraftCycleOutcome(state=state, reason=reason, team_id=5),
    )
    return PressReport(
        kind=PressKind.DRAFT_RESUME,
        from_phase=Phase.DRAFT,
        verdict=ExitVerdict(allowed=False, blockers=()),
        hook_result=hook,
    )


# --- the stop table, row by row ---------------------------------------------------------


def test_row_1_an_entry_into_the_target_reaches_it():
    report = _entry(Phase.WAIVERS, from_phase=Phase.FREE_AGENCY)
    assert (
        run_stop_reason(report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state())
        is RunStop.TARGET_REACHED
    )


def test_an_entry_into_a_phase_short_of_the_target_goes_on():
    report = _entry(Phase.REGULAR_SEASON)
    assert run_stop_reason(report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state()) is None


def test_a_press_that_leaves_the_league_past_the_target_stops_the_run():
    """A move by another command in a claim gap (`/advance-force` past the target): the press
    that finds the league there is still one press, and it must be the last."""
    report = _block(from_phase=Phase.POST_DEADLINE)
    stop = run_stop_reason(report, target=Phase.TRADE_DEADLINE, caps=_NO_CAPS, state=_state())
    assert stop is RunStop.TARGET_PASSED


def test_an_entry_past_the_target_is_also_a_passed_target():
    report = _entry(Phase.PLAYIN, from_phase=Phase.POST_DEADLINE)
    stop = run_stop_reason(report, target=Phase.TRADE_DEADLINE, caps=_NO_CAPS, state=_state())
    assert stop is RunStop.TARGET_PASSED


def test_a_press_that_finds_the_league_standing_in_the_target_reaches_it():
    report = _block(from_phase=Phase.POST_DEADLINE)
    stop = run_stop_reason(report, target=Phase.POST_DEADLINE, caps=_NO_CAPS, state=_state())
    assert stop is RunStop.TARGET_REACHED


def test_a_target_still_ahead_of_where_the_league_stands_goes_on():
    report = _block(from_phase=Phase.REGULAR_SEASON)
    assert run_stop_reason(report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state()) is None


def test_every_phase_an_undisturbed_run_stands_in_still_has_a_route_to_its_target():
    """`_standing_stop` must never fire on a run nobody disturbed: for every start and target
    with a route, each phase the route enters has a route to that target (or is it)."""
    checked = 0
    for start, target in itertools.product(Phase, Phase):
        route = route_to(start, target)
        if not route:
            continue
        for entered in route:
            assert route_to(entered, target) is not None, (start, entered, target)
            checked += 1
    assert checked > 100


def test_row_2_a_blocked_press_stops_with_blocked():
    report = _blocked(BlockerCode.REGULAR_SEASON_UNFINISHED, phase=Phase.REGULAR_SEASON_COMPLETE)
    assert (
        run_stop_reason(report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state())
        is RunStop.BLOCKED
    )


def test_row_3_a_missing_draft_board_needs_a_draft_board():
    report = _blocked(BlockerCode.DRAFT_BOARD_NOT_READY)
    assert (
        run_stop_reason(report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state())
        is RunStop.NEEDS_A_DRAFT_BOARD
    )


def test_row_4_the_waivers_edge_is_season_over():
    report = _blocked(BlockerCode.WAIVERS_EDGE, phase=Phase.WAIVERS)
    assert (
        run_stop_reason(report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state())
        is RunStop.SEASON_OVER
    )


def test_a_blocked_press_with_several_blockers_is_named_by_the_most_specific_stop():
    both = _blocked(BlockerCode.DRAFT_NOT_COMPLETE, BlockerCode.DRAFT_BOARD_NOT_READY)
    edge = _blocked(BlockerCode.GAMES_TARGET_NOT_REACHED, BlockerCode.WAIVERS_EDGE)
    assert (
        run_stop_reason(both, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state())
        is RunStop.NEEDS_A_DRAFT_BOARD
    )
    assert (
        run_stop_reason(edge, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state())
        is RunStop.SEASON_OVER
    )


def test_a_blocked_press_naming_no_blocker_is_still_a_stop():
    report = _blocked()
    assert (
        run_stop_reason(report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state())
        is RunStop.BLOCKED
    )


def test_row_5_a_draft_waiting_on_a_human_needs_a_pick():
    report = _draft_resume(DraftCycleState.WAITING_ON_HUMAN)
    assert (
        run_stop_reason(report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state())
        is RunStop.NEEDS_A_HUMAN_PICK
    )


@pytest.mark.parametrize("state", [DraftCycleState.STALLED, DraftCycleState.SKIPPED])
def test_row_6_a_stalled_or_skipped_draft_is_draft_stalled(state):
    report = _draft_resume(state, DraftCycleReason.POOL_EXHAUSTED)
    assert (
        run_stop_reason(report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state())
        is RunStop.DRAFT_STALLED
    )


def _draft_hook(state: DraftCycleState, reason: DraftCycleReason | None = None) -> PhaseHookResult:
    return PhaseHookResult(
        league_id=_LEAGUE_ID,
        season=2026,
        phase=Phase.DRAFT,
        draft_cycle=DraftCycleOutcome(state=state, reason=reason, team_id=5),
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (DraftCycleState.WAITING_ON_HUMAN, RunStop.NEEDS_A_HUMAN_PICK),
        (DraftCycleState.STALLED, RunStop.DRAFT_STALLED),
        (DraftCycleState.SKIPPED, RunStop.DRAFT_STALLED),
        (DraftCycleState.COMPLETE, None),
    ],
)
def test_an_entry_into_draft_stops_on_the_cycle_it_carries(state, expected):
    """The entry already announced this cycle; pressing on would be a `DRAFT_RESUME` that
    re-runs the hook and pings the same person again (duplicate on-the-clock ping)."""
    report = _entry(
        Phase.DRAFT,
        from_phase=Phase.DRAFT_LOTTERY,
        hook_result=_draft_hook(state, DraftCycleReason.POOL_EXHAUSTED),
    )
    stop = run_stop_reason(report, target=Phase.POST_DRAFT_TRADES, caps=_NO_CAPS, state=_state())
    assert stop is expected


def test_an_entry_into_the_target_is_reached_even_when_it_carries_a_waiting_cycle():
    report = _entry(
        Phase.DRAFT,
        from_phase=Phase.DRAFT_LOTTERY,
        hook_result=_draft_hook(DraftCycleState.WAITING_ON_HUMAN),
    )
    stop = run_stop_reason(report, target=Phase.DRAFT, caps=_NO_CAPS, state=_state())
    assert stop is RunStop.TARGET_REACHED


def test_an_entry_out_of_draft_is_not_stopped_by_the_hook_a_work_prefix_finished():
    """`resumed_hook_result` is the DRAFT hook the prefix finished for the phase being left;
    the entered phase's own hook is `hook_result` and carries no cycle."""
    report = _entry(
        Phase.POST_DRAFT_TRADES,
        from_phase=Phase.DRAFT,
        resumed_hook_result=_draft_hook(DraftCycleState.WAITING_ON_HUMAN),
    )
    stop = run_stop_reason(report, target=Phase.FREE_AGENCY, caps=_NO_CAPS, state=_state())
    assert stop is None


def test_a_completed_draft_lets_the_run_go_on():
    report = _draft_resume(DraftCycleState.COMPLETE, picks=3)
    assert run_stop_reason(report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state()) is None


def test_a_draft_resume_with_no_cycle_is_left_to_the_no_progress_net():
    report = replace(_draft_resume(DraftCycleState.COMPLETE), hook_result=None)
    assert run_stop_reason(report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state()) is None


def test_the_draft_cycle_of_a_prefix_hook_is_read_too():
    hook = _draft_resume(DraftCycleState.STALLED).hook_result
    report = replace(
        _draft_resume(DraftCycleState.COMPLETE), hook_result=None, resumed_hook_result=hook
    )
    assert (
        run_stop_reason(report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state())
        is RunStop.DRAFT_STALLED
    )


def test_a_block_press_goes_on():
    assert run_stop_reason(_block(), target=Phase.WAIVERS, caps=_NO_CAPS, state=_state()) is None


def test_row_8_a_cancel_stops_the_run():
    assert (
        run_stop_reason(
            _block(), target=Phase.WAIVERS, caps=_NO_CAPS, state=_state(cancelled=True)
        )
        is RunStop.CANCELLED
    )


def test_a_cancel_before_the_first_press_stops_the_run():
    assert (
        run_stop_reason(None, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state(cancelled=True))
        is RunStop.CANCELLED
    )


def test_row_9_the_press_cap_stops_the_run_at_exactly_the_cap():
    caps = RunCaps(max_presses=3)
    below = run_stop_reason(_block(), target=Phase.WAIVERS, caps=caps, state=_state(presses=2))
    at = run_stop_reason(_block(), target=Phase.WAIVERS, caps=caps, state=_state(presses=3))
    assert below is None
    assert at is RunStop.CAP_REACHED


def test_row_9_the_deadline_stops_the_run_from_the_moment_it_passes():
    caps = RunCaps(deadline=_NOW)
    before = run_stop_reason(
        _block(), target=Phase.WAIVERS, caps=caps, state=_state(now=_NOW - timedelta(seconds=1))
    )
    at = run_stop_reason(_block(), target=Phase.WAIVERS, caps=caps, state=_state(now=_NOW))
    assert before is None
    assert at is RunStop.CAP_REACHED


def test_row_10_the_no_progress_net_fires_at_the_limit_over_synthetic_counters():
    """The weaker guarantee (AR-6): the net is asserted over hand-set counters."""
    assert NO_PROGRESS_LIMIT == 2
    below = run_stop_reason(
        _block(games=0), target=Phase.WAIVERS, caps=_NO_CAPS, state=_state(idle_streak=1)
    )
    at = run_stop_reason(
        _block(games=0), target=Phase.WAIVERS, caps=_NO_CAPS, state=_state(idle_streak=2)
    )
    assert below is None
    assert at is RunStop.NO_PROGRESS


# --- precedence: what the league says comes before the counters --------------------------


def test_reaching_the_target_is_never_reported_as_a_cap_or_a_cancel():
    report = _entry(Phase.WAIVERS, from_phase=Phase.FREE_AGENCY)
    state = _state(presses=999, idle_streak=5, cancelled=True)
    caps = RunCaps(max_presses=1, deadline=_NOW - timedelta(hours=1))
    assert run_stop_reason(report, target=Phase.WAIVERS, caps=caps, state=state) is (
        RunStop.TARGET_REACHED
    )


def test_a_stalled_draft_is_named_before_the_no_progress_net():
    report = _draft_resume(DraftCycleState.STALLED, DraftCycleReason.POOL_EXHAUSTED)
    assert (
        run_stop_reason(
            report, target=Phase.WAIVERS, caps=_NO_CAPS, state=_state(idle_streak=2)
        )
        is RunStop.DRAFT_STALLED
    )


def test_no_progress_is_named_before_a_cancel_or_a_cap():
    state = _state(idle_streak=2, cancelled=True, presses=999)
    caps = RunCaps(max_presses=1)
    stop = run_stop_reason(_block(games=0), target=Phase.WAIVERS, caps=caps, state=state)
    assert stop is RunStop.NO_PROGRESS


# --- totality over PressKind x BlockerCode x DraftCycleState -----------------------------


def _report_for(kind: PressKind, code: BlockerCode, state: DraftCycleState) -> PressReport:
    if kind is PressKind.ENTRY:
        return _entry(Phase.REGULAR_SEASON)
    if kind is PressKind.BLOCK:
        return _block()
    if kind is PressKind.BLOCKED:
        return _blocked(code)
    return _draft_resume(state, DraftCycleReason.POOL_EXHAUSTED)


def test_every_press_kind_blocker_code_and_draft_state_has_an_answer():
    """Walks the whole product: no combination raises, and every answer is a `RunStop` or
    `None`. A member added to any enum with no row fails the IMPORT (next test)."""
    seen: set[RunStop | None] = set()
    for kind, code, state in itertools.product(PressKind, BlockerCode, DraftCycleState):
        stop = run_stop_reason(
            _report_for(kind, code, state), target=Phase.WAIVERS, caps=_NO_CAPS, state=_state()
        )
        assert stop is None or isinstance(stop, RunStop), (kind, code, state)
        seen.add(stop)
    assert {
        RunStop.BLOCKED,
        RunStop.NEEDS_A_DRAFT_BOARD,
        RunStop.SEASON_OVER,
        RunStop.NEEDS_A_HUMAN_PICK,
        RunStop.DRAFT_STALLED,
        None,
    } <= seen


@pytest.mark.parametrize(
    ("name", "enum", "mapping"),
    [
        ("press-kind", PressKind, season_run._STOP_BY_KIND),
        ("blocker-code", BlockerCode, season_run._STOP_BY_BLOCKER),
        ("draft-state", DraftCycleState, season_run._STOP_BY_DRAFT_STATE),
    ],
)
def test_a_member_with_no_row_fails_the_import_pin(name, enum, mapping):
    """Mutation 7's shape: drop one member from each map and the pin raises for it."""
    member = next(iter(enum))
    without = {k: v for k, v in mapping.items() if k is not member}
    with pytest.raises(RuntimeError, match=member.value):
        season_run._require_a_row_for_every_member(name, enum, without)


@pytest.mark.parametrize(
    ("name", "enum", "mapping"),
    [
        ("press-kind", PressKind, season_run._STOP_BY_KIND),
        ("blocker-code", BlockerCode, season_run._STOP_BY_BLOCKER),
        ("draft-state", DraftCycleState, season_run._STOP_BY_DRAFT_STATE),
    ],
)
def test_the_real_maps_pass_their_pins_and_an_unknown_key_fails_them(name, enum, mapping):
    season_run._require_a_row_for_every_member(name, enum, mapping)
    with pytest.raises(RuntimeError, match="unknown"):
        season_run._require_a_row_for_every_member(name, enum, {**mapping, "ghost": None})


# --- made_progress ----------------------------------------------------------------------


def test_progress_is_a_pointer_move_a_game_a_draft_pick_or_a_media_cycle():
    assert made_progress(_entry(Phase.REGULAR_SEASON))
    assert made_progress(_block(games=1))
    assert made_progress(_block(games=0, media_cycle_index=1, block=None))
    assert made_progress(_draft_resume(DraftCycleState.COMPLETE, picks=1))
    assert not made_progress(_block(games=0))
    assert not made_progress(_block(games=0, block=None))
    assert not made_progress(_draft_resume(DraftCycleState.COMPLETE))
    assert not made_progress(_blocked(BlockerCode.REGULAR_SEASON_UNFINISHED))


# --- run_to, with a scripted press --------------------------------------------------------


class _World:
    def __init__(self, monkeypatch, *, phase: Phase, script: list) -> None:
        self.league = SimpleNamespace(
            id=_LEAGUE_ID, current_phase=phase, commissioner_user_id=_COMMISSIONER
        )
        self.script = list(script)
        self.press_calls = 0
        self.steps: list = []

        async def get_league(pool, league_id):
            return self.league

        async def press(pool, *, league_id, actor_user_id, library, **kwargs):
            self.press_calls += 1
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        monkeypatch.setattr(season_run.league_repo, "get_league", get_league)
        monkeypatch.setattr(season_service, "press", press)

    async def on_step(self, step) -> None:
        self.steps.append(step)

    async def run(self, *, to_phase=Phase.POST_DEADLINE, caps=_NO_CAPS, should_continue=None, **kw):
        return await run_to(
            "pool",
            league_id=_LEAGUE_ID,
            actor_user_id=kw.pop("actor_user_id", _COMMISSIONER),
            to_phase=to_phase,
            library=object(),
            caps=caps,
            should_continue=should_continue or (lambda: True),
            on_step=self.on_step,
            **kw,
        )


def _walk_to_post_deadline() -> list[PressReport]:
    return [
        _entry(Phase.REGULAR_SEASON, from_phase=Phase.PRESEASON),
        _block(date(2026, 10, 20)),
        _block(date(2026, 10, 21)),
        _entry(Phase.TRADE_DEADLINE, from_phase=Phase.REGULAR_SEASON),
        _entry(Phase.POST_DEADLINE, from_phase=Phase.TRADE_DEADLINE),
    ]


async def test_a_run_stops_on_the_entry_into_the_target_and_reports_every_step(monkeypatch):
    world = _World(monkeypatch, phase=Phase.PRESEASON, script=_walk_to_post_deadline())

    report = await world.run(to_phase=Phase.POST_DEADLINE)

    assert report.stop is RunStop.TARGET_REACHED
    assert report.presses == 5 and world.press_calls == 5
    assert [s.number for s in report.steps] == [1, 2, 3, 4, 5]
    assert world.steps == list(report.steps)  # on_step saw each press, in order
    assert report.phases_entered == (
        Phase.REGULAR_SEASON,
        Phase.TRADE_DEADLINE,
        Phase.POST_DEADLINE,
    )
    assert report.final_phase is Phase.POST_DEADLINE
    assert (report.game_days, report.games) == (2, 4)
    assert (report.first_game_date, report.last_game_date) == ("2026-10-20", "2026-10-21")
    assert report.presses_by_kind == {
        PressKind.ENTRY: 3,
        PressKind.BLOCK: 2,
        PressKind.BLOCKED: 0,
        PressKind.DRAFT_RESUME: 0,
    }


async def test_a_run_whose_league_is_moved_past_the_target_in_a_claim_gap_stops_at_once(
    monkeypatch,
):
    """Press 2 finds the league in POST_DEADLINE (an `/advance-force` landed between the two
    presses) with TRADE_DEADLINE as the target: it must be the last press of the run, not the
    first of hundreds."""
    script = [
        _block(date(2026, 10, 20)),
        _block(date(2026, 10, 21), from_phase=Phase.POST_DEADLINE),
        _block(date(2026, 10, 22), from_phase=Phase.POST_DEADLINE),
        _block(date(2026, 10, 23), from_phase=Phase.POST_DEADLINE),
    ]
    world = _World(monkeypatch, phase=Phase.REGULAR_SEASON, script=script)

    report = await world.run(to_phase=Phase.TRADE_DEADLINE)

    assert report.stop is RunStop.TARGET_PASSED
    assert report.presses == 2 and world.press_calls == 2
    assert report.final_phase is Phase.POST_DEADLINE


async def test_a_run_never_presses_while_standing_in_its_target(monkeypatch):
    world = _World(monkeypatch, phase=Phase.POST_DEADLINE, script=[])

    report = await world.run(to_phase=Phase.POST_DEADLINE)

    assert report.stop is RunStop.TARGET_REACHED
    assert report.steps == () and world.press_calls == 0


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (Phase.POST_DEADLINE, Phase.REGULAR_SEASON),  # backwards
        (Phase.REGULAR_SEASON, Phase.SETUP),  # never targets SETUP
        (Phase.WAIVERS, Phase.PRESEASON),  # never across the season boundary
    ],
)
async def test_a_target_with_no_route_is_refused_before_any_press(monkeypatch, start, target):
    world = _World(monkeypatch, phase=start, script=[])

    with pytest.raises(PhaseError, match="/advance-run only goes forward"):
        await world.run(to_phase=target)

    assert world.press_calls == 0


async def test_a_run_from_waivers_to_waivers_is_already_there_not_an_edge_press(monkeypatch):
    world = _World(monkeypatch, phase=Phase.WAIVERS, script=[])

    report = await world.run(to_phase=Phase.WAIVERS)

    assert report.stop is RunStop.TARGET_REACHED and world.press_calls == 0


async def test_a_non_commissioner_is_refused_before_any_press(monkeypatch):
    world = _World(monkeypatch, phase=Phase.PRESEASON, script=_walk_to_post_deadline())

    with pytest.raises(PermissionDenied):
        await world.run(actor_user_id=99)

    assert world.press_calls == 0


async def test_a_missing_league_is_refused(monkeypatch):
    world = _World(monkeypatch, phase=Phase.PRESEASON, script=[])
    world.league = None

    from dba2.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await world.run()


async def test_the_press_cap_ends_the_run_after_exactly_that_many_presses(monkeypatch):
    world = _World(monkeypatch, phase=Phase.PRESEASON, script=_walk_to_post_deadline())

    report = await world.run(caps=RunCaps(max_presses=2))

    assert report.stop is RunStop.CAP_REACHED
    assert report.presses == 2 and world.press_calls == 2


async def test_the_deadline_is_read_between_presses_and_never_interrupts_one(monkeypatch):
    world = _World(monkeypatch, phase=Phase.PRESEASON, script=_walk_to_post_deadline())
    times = iter(_NOW + timedelta(seconds=s) for s in (0, 1, 2, 3, 4, 5))

    caps = RunCaps(deadline=_NOW + timedelta(seconds=2))
    report = await world.run(caps=caps, clock=lambda: next(times))

    # Read at 0 and 1 (two presses go), then at 2 (the deadline): the run stops.
    assert report.stop is RunStop.CAP_REACHED
    assert report.presses == 2 and world.press_calls == 2


async def test_a_deadline_already_past_makes_no_press_at_all(monkeypatch):
    world = _World(monkeypatch, phase=Phase.PRESEASON, script=_walk_to_post_deadline())

    report = await world.run(caps=RunCaps(deadline=_NOW), clock=lambda: _NOW)

    assert report.stop is RunStop.CAP_REACHED
    assert world.press_calls == 0 and report.steps == ()


async def test_a_cancel_is_read_between_presses_so_the_press_in_flight_commits(monkeypatch):
    world = _World(monkeypatch, phase=Phase.PRESEASON, script=_walk_to_post_deadline())

    async def stop_after_the_second_press(step) -> None:
        world.steps.append(step)
        cancelled.append(step.number >= 2)

    cancelled: list[bool] = []
    world.on_step = stop_after_the_second_press  # type: ignore[method-assign]

    report = await world.run(should_continue=lambda: not (cancelled and cancelled[-1]))

    assert report.stop is RunStop.CANCELLED
    assert report.presses == 2 and world.press_calls == 2


async def test_a_press_that_raises_ends_the_run_with_the_error_and_keeps_the_steps(monkeypatch):
    boom = ConcurrencyError("This league is busy with another action right now")
    world = _World(
        monkeypatch,
        phase=Phase.PRESEASON,
        script=[_entry(Phase.REGULAR_SEASON), boom],
    )

    report = await world.run()

    assert report.stop is RunStop.PRESS_FAILED
    assert report.error is boom
    assert report.presses == 1 and world.press_calls == 2


async def test_a_games_failed_to_sim_error_is_a_press_failure_not_a_no_progress_stop(monkeypatch):
    failure = season_service.GamesFailedToSimError(
        game_ids=(5,),
        failures=(GameFailure(game_id=5, cause="missing strategy snapshot"),),
        gate_lines=("1 game(s) not played",),
    )
    world = _World(monkeypatch, phase=Phase.REGULAR_SEASON, script=[failure])

    report = await world.run(to_phase=Phase.TRADE_DEADLINE)

    assert report.stop is RunStop.PRESS_FAILED and report.error is failure


async def test_a_cancellation_of_the_run_task_propagates_untouched(monkeypatch):
    world = _World(monkeypatch, phase=Phase.PRESEASON, script=[asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await world.run()


async def test_two_presses_in_a_row_that_change_nothing_end_the_run_as_no_progress(monkeypatch):
    """The net over synthetic reports: a block press that finalized no game and no draft pick
    -- which no league produces (AR-6), because it would raise instead."""
    idle = [_block(games=0, block=None) for _ in range(5)]
    world = _World(monkeypatch, phase=Phase.REGULAR_SEASON, script=idle)

    report = await world.run(to_phase=Phase.TRADE_DEADLINE)

    assert report.stop is RunStop.NO_PROGRESS
    assert report.presses == NO_PROGRESS_LIMIT and world.press_calls == NO_PROGRESS_LIMIT


async def test_a_press_that_made_progress_resets_the_no_progress_count(monkeypatch):
    script = [
        _block(games=0, block=None),
        _block(games=2),  # progress: resets the streak
        _block(games=0, block=None),
        _block(games=2),
        _entry(Phase.TRADE_DEADLINE, from_phase=Phase.REGULAR_SEASON),
    ]
    world = _World(monkeypatch, phase=Phase.REGULAR_SEASON, script=script)

    report = await world.run(to_phase=Phase.TRADE_DEADLINE)

    assert report.stop is RunStop.TARGET_REACHED and report.presses == 5


async def test_a_stalled_draft_ends_the_run_on_the_stall_with_its_reason(monkeypatch):
    stalled = _draft_resume(DraftCycleState.STALLED, DraftCycleReason.POOL_EXHAUSTED)
    world = _World(monkeypatch, phase=Phase.DRAFT, script=[stalled, stalled])

    report = await world.run(to_phase=Phase.POST_DRAFT_TRADES)

    assert report.stop is RunStop.DRAFT_STALLED and report.presses == 1
    assert report.draft_stop is not None
    assert report.draft_stop.reason is DraftCycleReason.POOL_EXHAUSTED


async def test_a_draft_board_refusal_ends_the_run_and_a_second_run_can_continue(monkeypatch):
    refusal = _blocked(BlockerCode.DRAFT_BOARD_NOT_READY)
    world = _World(
        monkeypatch,
        phase=Phase.DRAFT_LOTTERY,
        script=[refusal],
    )
    first = await world.run(to_phase=Phase.POST_DRAFT_TRADES)
    assert first.stop is RunStop.NEEDS_A_DRAFT_BOARD and first.presses == 1

    world.script = [
        _entry(Phase.DRAFT, from_phase=Phase.DRAFT_LOTTERY),
        _entry(Phase.POST_DRAFT_TRADES, from_phase=Phase.DRAFT),
    ]
    second = await world.run(to_phase=Phase.POST_DRAFT_TRADES)
    assert second.stop is RunStop.TARGET_REACHED and second.presses == 2


# --- the report's derived views -----------------------------------------------------------


def test_a_split_game_day_is_one_game_day_and_two_presses():
    day = date(2026, 12, 3)
    steps = tuple(
        season_run.RunStep(number=i + 1, report=_block(day, games=2), seconds=0.1) for i in range(2)
    )
    report = season_run.RunReport(
        from_phase=Phase.REGULAR_SEASON,
        target=Phase.TRADE_DEADLINE,
        stop=RunStop.CANCELLED,
        steps=steps,
        elapsed_seconds=1.0,
    )
    assert report.presses == 2 and report.game_days == 1 and report.games == 4


def test_failed_games_and_media_cycles_and_expired_offers_are_read_from_the_steps():
    failing = _block(
        failed_games=(9, 10),
        failures=(GameFailure(game_id=9, cause="lock timeout"),),
        media_cycle_index=3,
        expired_offers=2,
    )
    report = season_run.RunReport(
        from_phase=Phase.REGULAR_SEASON,
        target=Phase.TRADE_DEADLINE,
        stop=RunStop.CANCELLED,
        steps=(
            season_run.RunStep(1, failing, 0.1),
            season_run.RunStep(2, _entry(Phase.TRADE_DEADLINE), 0.1),
        ),
        elapsed_seconds=1.0,
    )
    assert report.failed_games == ((9, "lock timeout"), (10, "cause not recorded"))
    assert report.media_cycles == (3,)
    assert report.expired_offers == 2


def test_ai_counts_are_credited_per_phase_from_both_hook_results():
    trades = SimpleNamespace(executions=(1, 2, 3))
    signings = SimpleNamespace(signings=(1,) * 4)
    waivers = SimpleNamespace(executions=(1,))
    reg = PhaseHookResult(
        league_id=1, season=2026, phase=Phase.REGULAR_SEASON, trade_window=trades
    )
    fa = PhaseHookResult(
        league_id=1,
        season=2026,
        phase=Phase.FREE_AGENCY,
        free_agency=signings,
        resign_offers=(1, 2),
    )
    wv = PhaseHookResult(league_id=1, season=2026, phase=Phase.WAIVERS, waivers=waivers)
    quiet = PhaseHookResult(league_id=1, season=2026, phase=Phase.PRESEASON)
    steps = (
        season_run.RunStep(1, _entry(Phase.REGULAR_SEASON, hook_result=quiet), 0.1),
        season_run.RunStep(2, _block(hook_result=reg), 0.1),
        season_run.RunStep(
            3,
            _entry(
                Phase.WAIVERS,
                from_phase=Phase.FREE_AGENCY,
                hook_result=wv,
                resumed_hook_result=fa,
            ),
            0.1,
        ),
    )
    report = season_run.RunReport(
        from_phase=Phase.PRESEASON,
        target=Phase.WAIVERS,
        stop=RunStop.TARGET_REACHED,
        steps=steps,
        elapsed_seconds=1.0,
    )
    assert report.ai_counts_by_phase() == {
        Phase.REGULAR_SEASON: season_run.AiCounts(trades=3),
        Phase.FREE_AGENCY: season_run.AiCounts(signings=4, resign_offers=2),
        Phase.WAIVERS: season_run.AiCounts(waiver_cuts=1),
    }
