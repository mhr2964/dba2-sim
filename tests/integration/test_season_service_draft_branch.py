"""The DRAFT branch of the press (phase-5-stage-e-advance.md P5-99.h, P5-99.b.2 rows 2
and 5, build-plan commit 13), over real rows.

Two things, at the two ends of the phase:

- **The pre-entry check** (row 5). The `DRAFT_LOTTERY -> DRAFT` edge is refused, with the
  pointer where it was, unless a `drafts` row exists, at least one pick has a resolved
  `pick_number` and at least one prospect is available. Without it a league enters `DRAFT`
  with nothing to draft and the exit gate then holds it there (F1: nothing under `dba2/`
  can create a board, so nothing could ever finish it).
- **The resume press** (row 2). A press in `DRAFT` with the board not `COMPLETE` re-runs
  the DRAFT hook and nothing else -- ahead of the gate, because "not `COMPLETE`" is also
  an exit blocker and a gate-first order would refuse every resume. It runs even when
  `hook_completed_cycle_index` says the entry's hook already ran: a narrow, stated
  exception to P5-73 whose ground is that the DRAFT branch merges nothing to double-count.

The real `gm_service.run_phase_hook` runs over a real 2-team league for the cases whose
subject is the board; a recording spy stands in where only WHEN the press calls it matters.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import draft_repo, league_repo
from dba2.domain.phase import Phase
from dba2.services import gm_service, season_service
from dba2.services.gm_service import DraftCycleState
from dba2.services.season_service import BlockerCode, PressKind
from tests.integration._press_fixtures import (
    COMMISSIONER,
    SEASON,
    add_draft_board,
    bare_league_at,
    drop,
    press,
)

_HUMAN = 202


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


@pytest.fixture(autouse=True)
def no_closing_work(monkeypatch):
    """The subject is the DRAFT branch; cycle 4's and 5's media cycles over these small
    leagues are `test_season_service_media_cycle.py`'s."""
    monkeypatch.setitem(season_service._WORK_UNITS, season_service.WorkKind.CLOSING, {})


@pytest.fixture
def hook_calls(monkeypatch):
    """A recording stand-in for `run_phase_hook`, for the cases that only need to know
    WHEN it is called."""
    calls: list[Phase] = []

    async def spy(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        calls.append(phase)
        return gm_service.PhaseHookResult(league_id=league_id, season=SEASON, phase=phase)

    monkeypatch.setattr(gm_service, "run_phase_hook", spy)
    return calls


async def _state(pool, league_id: int) -> tuple[Phase, int, bool]:
    league = await league_repo.get_league(pool, league_id)
    return (
        league.current_phase,
        league.hook_completed_cycle_index,
        league.advance_claimed_at is None,
    )


async def _history(pool, league_id: int) -> int:
    return await league_repo.count_phase_entries(pool, league_id, SEASON)


async def _set_entry_hook_done(pool, league_id: int, cycle: int) -> None:
    await pool.execute(
        "UPDATE leagues SET hook_completed_cycle_index = $2 WHERE id = $1", league_id, cycle
    )


async def _used_picks(pool, league_id: int) -> int:
    picks = await draft_repo.list_draft_picks_for_season(pool, league_id, SEASON)
    return sum(1 for p in picks if p.used_for_player_id is not None)


# --- the pre-entry check (row 5) -----------------------------------------------------


async def test_entering_draft_with_no_drafts_row_refuses_and_leaves_the_pointer(pool, hook_calls):
    """The must-fire case: with the check removed this press moves the pointer into
    `DRAFT` and the gate holds the league there for good. The refusal names the missing
    precondition, the one tool that creates a board today and the open questions that
    would replace it -- and the very next press succeeds once the row exists."""
    league_id = await bare_league_at(pool, Phase.DRAFT_LOTTERY)
    try:
        board = await add_draft_board(pool, league_id, draft_row=False)
        history_before = await _history(pool, league_id)

        refused = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert refused.kind is PressKind.BLOCKED
        assert refused.to_phase is None
        assert [b.code for b in refused.verdict.blockers] == [BlockerCode.DRAFT_BOARD_NOT_READY]
        message = refused.verdict.blockers[0].message
        assert "drafts row" in message
        assert "tools/dev_league_draft.py" in message
        assert "U1" in message and "U2" in message
        assert await _history(pool, league_id) == history_before, "no phase_history row"
        assert await _state(pool, league_id) == (Phase.DRAFT_LOTTERY, 4, True), (
            "the pointer stayed at DRAFT_LOTTERY and the claim was released"
        )

        await draft_repo.create_draft(
            pool, league_id=league_id, season=SEASON, total_picks=len(board.picks)
        )
        entered = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert (entered.kind, entered.to_phase) == (PressKind.ENTRY, Phase.DRAFT)
        assert (await _state(pool, league_id))[0] is Phase.DRAFT
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize(
    ("build", "named", "not_named"),
    [
        ({"draft_row": False}, ["drafts row"], ["resolved pick", "prospect"]),
        ({"picks": ((1, None, "TOR"),)}, ["resolved pick"], ["drafts row", "prospect"]),
        ({"prospects": 0}, ["available prospect"], ["drafts row", "resolved pick"]),
        (
            {"draft_row": False, "picks": ((1, None, "TOR"),), "prospects": 0},
            ["drafts row", "resolved pick", "available prospect"],
            [],
        ),
    ],
    ids=["no-drafts-row", "no-resolved-pick", "no-prospect", "all-three"],
)
async def test_the_refusal_names_exactly_the_preconditions_that_are_missing(
    pool, hook_calls, build, named, not_named
):
    league_id = await bare_league_at(pool, Phase.DRAFT_LOTTERY)
    try:
        await add_draft_board(pool, league_id, **build)

        report = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert report.kind is PressKind.BLOCKED
        (blocker,) = report.verdict.blockers
        for fragment in named:
            assert fragment in blocker.message
        for fragment in not_named:
            assert fragment not in blocker.message
        assert (await _state(pool, league_id))[0] is Phase.DRAFT_LOTTERY
    finally:
        await drop(pool, league_id)


# --- the resume press (row 2) --------------------------------------------------------


async def _league_in_draft(pool, **board) -> tuple[int, object]:
    """A league whose real history reaches `DRAFT`, whose entry hook is recorded as done
    (cycle 5) -- so a press here is a pure resume -- with the board `board` describes."""
    league_id = await bare_league_at(pool, Phase.DRAFT)
    await _set_entry_hook_done(pool, league_id, 5)
    return league_id, await add_draft_board(pool, league_id, **board)


async def test_a_resume_press_advances_a_mid_draft_board_and_complete_unblocks_the_gate(pool):
    """The board is AI-only and untouched, and the entry hook is recorded as done: only
    a resume that runs regardless of the checkpoint moves it. The press finishes the
    draft, leaves the pointer in `DRAFT` (a resume moves nothing), reports the gate as
    open -- and the NEXT press is the Entry into `POST_DRAFT_TRADES`."""
    league_id, board = await _league_in_draft(
        pool, picks=((1, 1, "TOR"), (2, 2, "TOR"), (3, 3, "TOR")), prospects=3
    )
    try:
        gate = season_service.evaluate_exit(
            await season_service.load_gate_facts(
                pool, await league_repo.get_league(pool, league_id)
            )
        )
        assert [b.code for b in gate.blockers] == [BlockerCode.DRAFT_NOT_COMPLETE]
        history_before = await _history(pool, league_id)

        resumed = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert resumed.kind is PressKind.DRAFT_RESUME
        assert resumed.to_phase is None
        assert resumed.hook_result is not None
        assert resumed.hook_result.phase is Phase.DRAFT
        assert resumed.hook_result.draft_cycle.state is DraftCycleState.COMPLETE
        assert resumed.verdict.allowed is True, "COMPLETE unblocks the gate"
        assert await _used_picks(pool, league_id) == 3
        assert await _history(pool, league_id) == history_before
        assert await _state(pool, league_id) == (Phase.DRAFT, 5, True)

        after = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert (after.kind, after.to_phase) == (PressKind.ENTRY, Phase.POST_DRAFT_TRADES)
    finally:
        await drop(pool, league_id)


async def test_a_resume_press_stops_at_a_person_and_repeats_harmlessly_until_they_pick(pool):
    """The first press drafts the AI team's pick and stops with the human on the clock --
    the state the on-the-clock ping is built from. A second press re-runs the hook and
    changes nothing (the eligible list is re-derived every call). Once the person's pick
    is on the board the gate opens and the next press is an Entry."""
    league_id, board = await _league_in_draft(
        pool,
        picks=((1, 1, "TOR"), (2, 2, "BOS")),
        managers={"BOS": _HUMAN},
    )
    try:
        first = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert first.kind is PressKind.DRAFT_RESUME
        cycle = first.hook_result.draft_cycle
        assert cycle.state is DraftCycleState.WAITING_ON_HUMAN
        assert cycle.manager_user_id == _HUMAN
        assert cycle.pick_id == board.picks[1].id
        assert first.verdict.allowed is False
        assert await _used_picks(pool, league_id) == 1

        second = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert second.kind is PressKind.DRAFT_RESUME
        assert second.hook_result.draft_cycle.state is DraftCycleState.WAITING_ON_HUMAN
        assert second.hook_result.draft is None, "nothing left for the AI to draft"
        assert await _used_picks(pool, league_id) == 1
        assert (await _state(pool, league_id))[0] is Phase.DRAFT

        await draft_repo.set_used_for_player(pool, board.picks[1].id, board.prospects[1])
        third = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert (third.kind, third.to_phase) == (PressKind.ENTRY, Phase.POST_DRAFT_TRADES)
    finally:
        await drop(pool, league_id)


async def test_a_resume_press_drafts_through_an_auto_piloted_team_and_the_gate_opens(pool):
    """GM-2 (docs/design/gm-command-surface.md), mutation 2b -- the real DRAFT
    exit-gate assertion §5's own mutation-2 rewrite retracted from
    `test_season_service_gate_loader.py` (that file's site changes only a
    SENTENCE, never the gate). This is `test_a_resume_press_stops_at_a_
    person_and_repeats_harmlessly_until_they_pick`'s own negative: BOS's
    manager has delegated to the AI, so `execute_draft`'s halt
    (`gm_service.py`, reading the SAME extended `self_managed_team_ids`
    predicate GM-2 changes) must draft through BOS's pick instead of
    stopping at it, and the board must reach COMPLETE -- open, not
    blocked -- in one press, with no human ever on the clock."""
    league_id, board = await _league_in_draft(
        pool,
        picks=((1, 1, "TOR"), (2, 2, "BOS")),
        managers={"BOS": _HUMAN},
        auto_pilot=frozenset({"BOS"}),
    )
    try:
        resumed = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert resumed.kind is PressKind.DRAFT_RESUME
        assert resumed.hook_result.draft_cycle.state is DraftCycleState.COMPLETE
        assert resumed.verdict.allowed is True, "an auto-piloted team's pick unblocks the gate"
        assert await _used_picks(pool, league_id) == 2

        after = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert (after.kind, after.to_phase) == (PressKind.ENTRY, Phase.POST_DRAFT_TRADES)
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize("hook_owed", [True, False], ids=["prefix-ran-it", "resume-ran-it"])
async def test_a_draft_that_cannot_finish_is_a_resume_press_that_calls_the_hook_exactly_once(
    pool, hook_calls, hook_owed
):
    """An unresolved pick keeps the board short of `COMPLETE`. If the entry's hook is
    still owed, the work prefix runs it and THAT is this press's resume -- a second call
    would be a pointless re-run; if it is recorded, the resume itself runs it. Either way
    exactly one call, kind `DRAFT_RESUME`, pointer and claim as they were."""
    league_id = await bare_league_at(pool, Phase.DRAFT)
    try:
        if hook_owed:  # the fixture recorded DRAFT's own hook; put it back one cycle
            await _set_entry_hook_done(pool, league_id, 4)
        await add_draft_board(pool, league_id, picks=((1, None, "TOR"),))

        report = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert report.kind is PressKind.DRAFT_RESUME
        assert [b.code for b in report.verdict.blockers] == [BlockerCode.DRAFT_NOT_COMPLETE]
        assert hook_calls == [Phase.DRAFT]
        assert report.hook_result is not None
        assert report.resumed_hook_result is None, "one hook result, reported once"
        assert await _state(pool, league_id) == (Phase.DRAFT, 5, True)
    finally:
        await drop(pool, league_id)


async def test_a_complete_draft_is_an_entry_press_not_a_resume(pool, hook_calls):
    league_id = await bare_league_at(pool, Phase.DRAFT)
    try:
        await _set_entry_hook_done(pool, league_id, 5)
        await add_draft_board(pool, league_id, picks=((1, 1, "TOR"),), used=(0,))

        report = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert (report.kind, report.to_phase) == (PressKind.ENTRY, Phase.POST_DRAFT_TRADES)
        assert hook_calls == [], "POST_DRAFT_TRADES has no hook and the resume did not run"
    finally:
        await drop(pool, league_id)


async def test_the_draft_hook_is_not_re_run_by_a_press_in_any_other_phase(pool, hook_calls):
    """The exception to P5-73 is `DRAFT`'s alone: a press elsewhere with its entry's hook
    recorded runs nothing (here `DRAFT_LOTTERY`, whose board is complete on paper)."""
    league_id = await bare_league_at(pool, Phase.DRAFT_LOTTERY)
    try:
        await add_draft_board(pool, league_id, picks=((1, 1, "TOR"),), used=(0,))

        report = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert (report.kind, report.to_phase) == (PressKind.ENTRY, Phase.DRAFT)
        assert hook_calls == [Phase.DRAFT], "the entry's own hook, once"
    finally:
        await drop(pool, league_id)
