"""`advance_to` and `force` (phase-5-stage-e-advance.md P5-99.f, P5-99.g, build-plan
commit 15), over real rows.

`/advance-to` never executes a working phase's press: it steps while the NEXT press would
be a no-op Entry press and stops -- possibly short of the target -- the moment the next one
would do work, saying which phase stopped it. It runs no hook, no phase work, no sim and
posts nothing, and it REFUSES TO START while the current entry has outstanding work
(B-1's predicate: a phase with nothing to do never has anything outstanding, or it could
never take a step at the eight no-op phases).

`/advance-force` is a pointer move, not a press: it takes the claim, runs no hook, no
phase work, no sim and no media, names every blocker it overrode and every unit of work it
walked away from in the audited reason, and refuses, even under force: a target that is not
strictly FORWARD of the current phase along the season route (`route_to`'s own rule --
`SETUP`, `PRESEASON`, the phase it is in, any earlier phase, and anything after `WAIVERS`),
and a league with no regular-season schedule that stands in `PRESEASON` or a schedule phase
(or would enter one).

**Decision, no schedule and force:** the spec says force overrides every blocker. But once
the pointer leaves `PRESEASON` or a schedule phase with no schedule, nothing in the bot can
ever create or play those games -- there is no route back to `PRESEASON`, and the block press
refuses a season with no games -- so the season is stranded with no in-bot exit. That is not
about a media cycle: it is that every door is closed (`/advance` is blocked by
`SCHEDULE_MISSING`, `/advance-force` is refused for the same reason, `PRESEASON` is never a
target). An unmet games target, by contrast, stays overridable and is named: the games still
exist and a person can play them out.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

from datetime import date

import pytest

from dba2.core.errors import ConcurrencyError, PhaseError, ValidationError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo
from dba2.domain.phase import Phase
from dba2.phase.permissions import PermissionDenied
from dba2.services import gm_service, season_service
from dba2.services.season_service import BlockerCode
from tests.integration._press_fixtures import (
    COMMISSIONER,
    SEASON,
    add_draft_board,
    add_game,
    add_played_game,
    add_two_teams,
    bare_league_at,
    drop,
)

_OTHER_USER = 99


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


@pytest.fixture(autouse=True)
def hook_calls(monkeypatch):
    """Neither command may run a hook; a spy makes "never" assertable."""
    calls: list[Phase] = []

    async def hook(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        calls.append(phase)
        return gm_service.PhaseHookResult(league_id=league_id, season=SEASON, phase=phase)

    monkeypatch.setattr(gm_service, "run_phase_hook", hook)
    return calls


async def _advance_to(pool, league_id: int, to_phase: Phase, *, actor: int = COMMISSIONER):
    return await season_service.advance_to(
        pool, league_id=league_id, actor_user_id=actor, to_phase=to_phase
    )


async def _force(
    pool, league_id: int, to_phase: Phase, *, reason: str = "operator override", actor=COMMISSIONER
):
    return await season_service.force(
        pool, league_id=league_id, actor_user_id=actor, to_phase=to_phase, reason=reason
    )


async def _phase(pool, league_id: int) -> Phase:
    return (await league_repo.get_league(pool, league_id)).current_phase


async def _checkpoints(pool, league_id: int) -> tuple[int, int, int]:
    league = await league_repo.get_league(pool, league_id)
    return (
        league.entry_work_completed_index,
        league.hook_completed_cycle_index,
        league.closing_work_completed_index,
    )


async def _claim_is_free(pool, league_id: int) -> bool:
    return (await league_repo.get_league(pool, league_id)).advance_claimed_at is None


async def _last_transition(pool, league_id: int):
    return await pool.fetchrow(
        "SELECT from_phase, to_phase, forced, reason FROM phase_history "
        "WHERE league_id = $1 ORDER BY id DESC LIMIT 1",
        league_id,
    )


# --- advance_to ---------------------------------------------------------------------


async def test_advance_to_steps_into_a_no_op_phase_and_runs_nothing(pool, hook_calls):
    """`PLAYIN` owns no work and its checkpoints stay at -1 forever, so this is also B-1's
    case: read as a bare `checkpoint < index`, the refusal to start would fire here and
    `/advance-to` could never take a single step at a no-op phase."""
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        before = await _checkpoints(pool, league_id)

        report = await _advance_to(pool, league_id, Phase.PLAYOFFS_R1)

        assert report.entered == (Phase.PLAYOFFS_R1,)
        assert report.reached is True
        assert report.stopped_before is None
        assert await _phase(pool, league_id) is Phase.PLAYOFFS_R1
        assert hook_calls == []
        assert await _checkpoints(pool, league_id) == before, "no checkpoint moved"
        assert await _claim_is_free(pool, league_id)
        last = await _last_transition(pool, league_id)
        assert (last["from_phase"], last["to_phase"], last["forced"]) == (
            "PLAYIN",
            "PLAYOFFS_R1",
            False,
        )
    finally:
        await drop(pool, league_id)


async def test_advance_to_stops_before_the_first_working_phase_and_says_which(pool, hook_calls):
    """`PLAYOFFS_R1 -> ... -> AWARDS` are all no-ops; `DRAFT_LOTTERY` is cycle-bearing (a
    hook and a media cycle), so the walk stops in `AWARDS` and names it."""
    league_id = await bare_league_at(pool, Phase.PLAYOFFS_R1)
    try:
        report = await _advance_to(pool, league_id, Phase.DRAFT_LOTTERY)

        assert report.entered == (
            Phase.PLAYOFFS_R2,
            Phase.CONFERENCE_FINALS,
            Phase.FINALS,
            Phase.AWARDS,
        )
        assert report.reached is False
        assert report.stopped_before is Phase.DRAFT_LOTTERY
        assert report.stop_reason is not None and "DRAFT_LOTTERY" in report.stop_reason
        assert await _phase(pool, league_id) is Phase.AWARDS
        assert hook_calls == []
        assert await _claim_is_free(pool, league_id)
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (Phase.POST_DRAFT_TRADES, Phase.PROGRESSION),
        (Phase.OPTION_DECISIONS, Phase.RESIGN_WINDOW),
        (Phase.AWARDS, Phase.DRAFT_LOTTERY),
    ],
)
async def test_advance_to_takes_no_step_when_the_very_next_phase_does_work(pool, start, target):
    """`PROGRESSION` (an agency checkpoint), `RESIGN_WINDOW` (a hook and a media cycle) and
    `DRAFT_LOTTERY` are all working phases; none is ever entered by `/advance-to`."""
    league_id = await bare_league_at(pool, start)
    try:
        report = await _advance_to(pool, league_id, target)

        assert report.entered == ()
        assert report.reached is False
        assert report.stopped_before is target
        assert target.value in report.stop_reason
        assert await _phase(pool, league_id) is start
    finally:
        await drop(pool, league_id)


async def test_advance_to_the_phase_the_league_is_already_in_moves_nothing(pool):
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        report = await _advance_to(pool, league_id, Phase.PLAYIN)

        assert report.entered == () and report.reached is True
        assert await _phase(pool, league_id) is Phase.PLAYIN
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize("target", [Phase.PLAYIN, Phase.SETUP])
async def test_advance_to_refuses_a_target_with_no_route_within_the_season(pool, target):
    league_id = await bare_league_at(pool, Phase.AWARDS)
    try:
        with pytest.raises(PhaseError, match="route"):
            await _advance_to(pool, league_id, target)

        assert await _phase(pool, league_id) is Phase.AWARDS
        assert await _claim_is_free(pool, league_id)
    finally:
        await drop(pool, league_id)


async def test_advance_to_from_waivers_gives_the_phase_7_message(pool):
    league_id = await bare_league_at(pool, Phase.WAIVERS)
    try:
        with pytest.raises(PhaseError) as refused:
            await _advance_to(pool, league_id, Phase.PLAYIN)

        assert str(refused.value) == season_service.WAIVERS_EDGE_MESSAGE
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize(
    ("phase", "column"),
    [
        (Phase.PROGRESSION, "entry_work_completed_index"),
        (Phase.DRAFT_LOTTERY, "closing_work_completed_index"),
    ],
)
async def test_advance_to_refuses_to_start_while_the_current_entry_has_outstanding_work(
    pool, phase, column
):
    """A command whose whole promise is "this does no work" must not quietly finish a
    crashed agency checkpoint, and must not walk past one either (F16): it refuses and
    names the phase and the fix -- one `/advance`, which does the work first. The two
    cases are the two kinds of work a phase can owe: entry work (`PROGRESSION`'s agency
    checkpoint) and closing work (`DRAFT_LOTTERY`'s media cycle)."""
    league_id = await bare_league_at(pool, phase)
    try:
        await pool.execute(f"UPDATE leagues SET {column} = -1 WHERE id = $1", league_id)
        before = await _checkpoints(pool, league_id)

        with pytest.raises(PhaseError) as refused:
            await _advance_to(pool, league_id, Phase.OPTION_DECISIONS)

        assert phase.value in str(refused.value)
        assert "/advance" in str(refused.value)
        assert await _phase(pool, league_id) is phase
        assert await _checkpoints(pool, league_id) == before
        assert await _claim_is_free(pool, league_id)
    finally:
        await drop(pool, league_id)


async def test_advance_to_does_not_refuse_a_phase_whose_work_is_all_done(pool):
    """B-1's real case at a phase that OWNS a pointer. `DRAFT_LOTTERY` has closing work and
    a hook but no entry work; once both are done nothing is outstanding, so `/advance-to`
    does not refuse -- it reports the working phase that stops it. Read as a bare
    `checkpoint < index`, the never-advancing entry checkpoint (-1) would read "owed"
    here forever and this would raise instead."""
    league_id = await bare_league_at(pool, Phase.DRAFT_LOTTERY)
    try:
        entry_index = (await league_repo.count_phase_entries(pool, league_id, SEASON)) - 1
        await league_repo.set_closing_work_completed_index(pool, league_id, entry_index)

        report = await _advance_to(pool, league_id, Phase.POST_DRAFT_TRADES)

        assert report.entered == ()
        assert report.stopped_before is Phase.DRAFT
        assert "DRAFT" in report.stop_reason
    finally:
        await drop(pool, league_id)


async def test_advance_to_stops_at_a_gate_that_refuses_and_names_the_blockers(pool):
    """`REGULAR_SEASON_COMPLETE` with its closing work done but a game still scheduled:
    leaving it is refused by the exit gate, so the walk takes no step and says why."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON_COMPLETE)
    try:
        await add_played_game(pool, league_id)
        teams = tuple(
            r["id"]
            for r in await pool.fetch(
                "SELECT id FROM teams WHERE league_id = $1 ORDER BY id LIMIT 2", league_id
            )
        )
        await add_game(pool, league_id, teams, game_index=1, game_date=date(2026, 10, 21))
        entry_index = (await league_repo.count_phase_entries(pool, league_id, SEASON)) - 1
        await league_repo.set_closing_work_completed_index(pool, league_id, entry_index)

        report = await _advance_to(pool, league_id, Phase.PLAYOFFS_R1)

        assert report.entered == ()
        assert report.reached is False
        assert {b.code for b in report.blockers} >= {BlockerCode.REGULAR_SEASON_UNFINISHED}
        assert await _phase(pool, league_id) is Phase.REGULAR_SEASON_COMPLETE
    finally:
        await drop(pool, league_id)


async def _mark_closing_work_done(pool, league_id: int) -> None:
    entry_index = (await league_repo.count_phase_entries(pool, league_id, SEASON)) - 1
    await league_repo.set_closing_work_completed_index(pool, league_id, entry_index)


async def test_advance_to_shows_the_blockers_when_the_gate_refuses_a_step_into_a_working_phase(
    pool,
):
    """Critic 3, minor 4: the walk used to test "the next phase does work" BEFORE the exit
    gate, so a league that could not leave its phase at all was told to press `/advance` to
    enter the working phase -- and `/advance` would then be blocked. The gate is asked first,
    so the blockers are what the commissioner sees."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        await _mark_closing_work_done(pool, league_id)
        teams = await add_two_teams(pool, league_id)
        await add_game(pool, league_id, teams, game_index=0, game_date=date(2026, 10, 20))

        report = await _advance_to(pool, league_id, Phase.TRADE_DEADLINE)

        assert report.entered == ()
        assert report.stopped_before is Phase.TRADE_DEADLINE
        assert [b.code for b in report.blockers] == [BlockerCode.GAMES_TARGET_NOT_REACHED]
        assert "cannot be left" in report.stop_reason
        assert "does work" not in report.stop_reason
    finally:
        await drop(pool, league_id)


async def test_advance_to_refuses_to_start_with_the_ai_hook_owed_but_not_a_deferred_one(pool):
    """Critic 3, minor 5: `/advance-to` and `/advance-force` used to disagree about "owed" (the
    first ignored the hook, the second named it). One definition now: a crashed hook is owed
    work `/advance-to` would walk past for good, so it refuses to start (at `DRAFT`, whose next
    phase is a no-op one). The single exception is the hook JC-13 defers to a block press at
    `REGULAR_SEASON`: it is owed by design, not by a crash."""
    league_id = await bare_league_at(pool, Phase.DRAFT)
    try:
        await _mark_closing_work_done(pool, league_id)
        await pool.execute(
            "UPDATE leagues SET hook_completed_cycle_index = -1 WHERE id = $1", league_id
        )

        with pytest.raises(PhaseError) as refused:
            await _advance_to(pool, league_id, Phase.POST_DRAFT_TRADES)

        assert "AI hook" in str(refused.value)
        assert "closing work" not in str(refused.value)
        assert "/advance" in str(refused.value)
        assert await _phase(pool, league_id) is Phase.DRAFT
    finally:
        await drop(pool, league_id)

    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        await _mark_closing_work_done(pool, league_id)
        await pool.execute(
            "UPDATE leagues SET hook_completed_cycle_index = -1 WHERE id = $1", league_id
        )
        await add_played_game(pool, league_id)

        report = await _advance_to(pool, league_id, Phase.TRADE_DEADLINE)

        assert report.entered == () and report.stopped_before is Phase.TRADE_DEADLINE
    finally:
        await drop(pool, league_id)


async def test_force_and_advance_to_share_one_definition_of_owed_work(pool):
    """The same league, the same entry: what `/advance-to` refuses to start over is exactly
    what `/advance-force` says it walked away from (the deferred hook aside)."""
    league_id = await bare_league_at(pool, Phase.DRAFT)
    try:
        await pool.execute(
            "UPDATE leagues SET hook_completed_cycle_index = -1, "
            "closing_work_completed_index = -1 WHERE id = $1",
            league_id,
        )

        with pytest.raises(PhaseError) as refused:
            await _advance_to(pool, league_id, Phase.POST_DRAFT_TRADES)
        report = await _force(pool, league_id, Phase.POST_DRAFT_TRADES, reason="skip")

        assert "AI hook" in str(refused.value) and "closing work" in str(refused.value)
        assert report.skipped_work == ("DRAFT AI hook", "DRAFT closing work")
    finally:
        await drop(pool, league_id)


async def test_advance_to_is_commissioner_only_and_takes_no_claim_for_a_refusal(pool):
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        with pytest.raises(PermissionDenied):
            await _advance_to(pool, league_id, Phase.PLAYOFFS_R1, actor=_OTHER_USER)

        assert await _phase(pool, league_id) is Phase.PLAYIN
        assert await _claim_is_free(pool, league_id)
    finally:
        await drop(pool, league_id)


async def test_advance_to_refuses_while_another_advance_holds_the_claim(pool):
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        claimed_at = await season_service._claim_league(pool, league_id, _OTHER_USER)

        with pytest.raises(ConcurrencyError):
            await _advance_to(pool, league_id, Phase.PLAYOFFS_R1)

        assert await _phase(pool, league_id) is Phase.PLAYIN
        holder = await league_repo.get_league(pool, league_id)
        assert holder.advance_claimed_by == _OTHER_USER, "the other press's claim is untouched"
        await season_service._release_claim(pool, league_id, _OTHER_USER, claimed_at)
    finally:
        await drop(pool, league_id)


# --- force ------------------------------------------------------------------------------


async def test_force_names_the_blockers_it_overrode_and_writes_them_to_the_audit_row(
    pool, hook_calls
):
    league_id = await bare_league_at(pool, Phase.DRAFT)
    try:
        await add_draft_board(pool, league_id, picks=((1, 1, "TOR"),), managers={"TOR": 101})
        before = await _checkpoints(pool, league_id)

        report = await _force(pool, league_id, Phase.POST_DRAFT_TRADES, reason="league is stuck")

        assert [b.code for b in report.overrode] == [BlockerCode.DRAFT_NOT_COMPLETE]
        assert report.from_phase is Phase.DRAFT and report.to_phase is Phase.POST_DRAFT_TRADES
        assert await _phase(pool, league_id) is Phase.POST_DRAFT_TRADES
        last = await _last_transition(pool, league_id)
        assert last["forced"] is True
        assert "league is stuck" in last["reason"]
        assert report.overrode[0].message in last["reason"], "the audit row names the blocker"
        assert report.recorded_reason == last["reason"]
        assert hook_calls == [], "a pointer move: no hook"
        assert await _checkpoints(pool, league_id) == before, "no checkpoint written"
        assert await _claim_is_free(pool, league_id)
    finally:
        await drop(pool, league_id)


async def test_force_into_the_draft_names_the_board_check_it_overrode(pool):
    """The destination check of `DRAFT_LOTTERY -> DRAFT` is not an exit blocker, but a
    forced entry into a phase with no board skips it -- so it is named, like every other
    thing force walked past."""
    league_id = await bare_league_at(pool, Phase.DRAFT_LOTTERY)
    try:
        report = await _force(pool, league_id, Phase.DRAFT, reason="we will fix the class")

        assert [b.code for b in report.overrode] == [BlockerCode.DRAFT_BOARD_NOT_READY]
        assert await _phase(pool, league_id) is Phase.DRAFT
        assert report.overrode[0].message in (await _last_transition(pool, league_id))["reason"]
    finally:
        await drop(pool, league_id)


async def test_force_names_the_outstanding_work_it_walks_away_from(pool):
    """It does not run the work prefix and does not refuse on outstanding work -- it is
    the override -- but it says what it skipped, in the report and the audit row: forced-
    past work is skipped permanently (nothing later re-visits a passed entry)."""
    league_id = await bare_league_at(pool, Phase.TRADE_DEADLINE)
    try:
        await add_played_game(pool, league_id)
        await pool.execute(
            "UPDATE leagues SET entry_work_completed_index = -1, hook_completed_cycle_index = -1, "
            "closing_work_completed_index = -1 WHERE id = $1",
            league_id,
        )

        report = await _force(pool, league_id, Phase.POST_DEADLINE, reason="skip the deadline")

        text = " ".join(report.skipped_work)
        assert "TRADE_DEADLINE" in text
        assert any("entry" in w for w in report.skipped_work)
        assert any("hook" in w for w in report.skipped_work)
        assert any("closing" in w for w in report.skipped_work)
        last = await _last_transition(pool, league_id)
        for work in report.skipped_work:
            assert work in last["reason"]
        assert await _checkpoints(pool, league_id) == (-1, -1, -1), "nothing was completed"
    finally:
        await drop(pool, league_id)


async def test_force_with_nothing_to_override_records_the_bare_reason(pool):
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        report = await _force(pool, league_id, Phase.PLAYOFFS_R2, reason="skip a round")

        assert report.overrode == () and report.skipped_work == ()
        assert (await _last_transition(pool, league_id))["reason"] == "skip a round"
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize("current", [Phase.PLAYIN, Phase.WAIVERS, Phase.REGULAR_SEASON])
async def test_force_refuses_preseason_even_under_force_and_writes_nothing(pool, current):
    league_id = await bare_league_at(pool, current)
    try:
        with pytest.raises(PhaseError, match="PRESEASON"):
            await _force(pool, league_id, Phase.PRESEASON)

        assert await _phase(pool, league_id) is current
        assert await _claim_is_free(pool, league_id)
    finally:
        await drop(pool, league_id)


async def test_force_does_not_override_a_missing_schedule(pool):
    """The decision in this module's docstring, and its twin: the refusal is about the
    missing schedule and nothing else -- the same force from the same phase succeeds once
    the season has a game."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        with pytest.raises(PhaseError, match="schedule"):
            await _force(pool, league_id, Phase.REGULAR_SEASON_COMPLETE)

        assert await _phase(pool, league_id) is Phase.REGULAR_SEASON
        assert await _claim_is_free(pool, league_id)

        await add_played_game(pool, league_id)
        report = await _force(pool, league_id, Phase.REGULAR_SEASON_COMPLETE)

        assert report.overrode == ()
        assert await _phase(pool, league_id) is Phase.REGULAR_SEASON_COMPLETE
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (Phase.POST_DEADLINE, Phase.REGULAR_SEASON_COMPLETE),
        (Phase.POST_DEADLINE, Phase.DRAFT_LOTTERY),
        (Phase.REGULAR_SEASON_COMPLETE, Phase.PLAYIN),
    ],
)
async def test_force_out_of_a_scheduleless_schedule_phase_is_refused_and_moves_nothing(
    pool, hook_calls, current, target
):
    """The refusal's rationale is the stranded season (no route back, so nothing can ever
    create or play the games), pinned by the observable: after the refused force the league
    is where it was, its history has no new row, and nothing ran. The targets include ones
    that are themselves NOT schedule phases (`DRAFT_LOTTERY`, `PLAYIN`): the refusal is about
    where the league stands, not only where it is going."""
    league_id = await bare_league_at(pool, current)
    try:
        history_before = await _history_rows(pool, league_id)

        with pytest.raises(PhaseError, match="schedule") as refused:
            await _force(pool, league_id, target)

        assert "stranded" in str(refused.value)
        assert await _phase(pool, league_id) is current
        assert await _history_rows(pool, league_id) == history_before
        assert hook_calls == []
        assert await _claim_is_free(pool, league_id)
    finally:
        await drop(pool, league_id)


async def _history_rows(pool, league_id: int) -> int:
    return await pool.fetchval("SELECT count(*) FROM phase_history WHERE league_id = $1", league_id)


@pytest.mark.parametrize("target", [Phase.REGULAR_SEASON, Phase.DRAFT_LOTTERY])
async def test_force_out_of_preseason_with_its_entry_work_undone_is_refused_not_stranded(
    pool, target
):
    """Critic 3, B2, reached by the force attempt itself: a bare league at `PRESEASON`
    whose entry work (the schedule's builder) has not run. Forcing it on would leave
    `REGULAR_SEASON` with zero games, from which every exit is closed -- `/advance` is
    blocked forever, force is refused for the missing schedule, `PRESEASON` is never a
    target. So the force is refused HERE, the league is unchanged, and the refusal tells the
    commissioner to run `/advance`, whose prefix runs the entry work that builds the
    schedule."""
    league_id = await bare_league_at(pool, Phase.PRESEASON)
    try:
        await pool.execute(
            "UPDATE leagues SET entry_work_completed_index = -1 WHERE id = $1", league_id
        )
        history_before = await _history_rows(pool, league_id)

        with pytest.raises(PhaseError) as refused:
            await _force(pool, league_id, target)

        assert "Run /advance instead" in str(refused.value)
        assert "PRESEASON" in str(refused.value)
        assert await _phase(pool, league_id) is Phase.PRESEASON
        assert await _history_rows(pool, league_id) == history_before
        assert await _claim_is_free(pool, league_id)
    finally:
        await drop(pool, league_id)


async def test_force_out_of_preseason_is_allowed_once_the_season_has_a_schedule(pool):
    """The twin of the refusal above: the refusal is about the missing schedule and nothing
    else."""
    league_id = await bare_league_at(pool, Phase.PRESEASON)
    try:
        await add_played_game(pool, league_id)

        report = await _force(pool, league_id, Phase.REGULAR_SEASON)

        assert report.from_phase is Phase.PRESEASON
        assert await _phase(pool, league_id) is Phase.REGULAR_SEASON
    finally:
        await drop(pool, league_id)


async def test_force_from_setup_into_a_schedule_phase_with_no_schedule_is_refused(pool):
    """`SETUP -> REGULAR_SEASON` is a graph edge force may take, and with no schedule it is
    the same stranded state as forcing out of `PRESEASON` (this is a deviation from the
    letter of the decision, which names only the current phase: the destination is the
    other way to reach that state)."""
    league_id = await bare_league_at(pool, Phase.SETUP)
    try:
        with pytest.raises(PhaseError, match="schedule"):
            await _force(pool, league_id, Phase.REGULAR_SEASON)

        assert await _phase(pool, league_id) is Phase.SETUP
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize(
    "target", [Phase.DRAFT, Phase.AWARDS, Phase.FREE_AGENCY, Phase.WAIVERS, Phase.PLAYIN]
)
async def test_force_from_setup_to_any_phase_with_no_schedule_is_refused(pool, target):
    """`SETUP -> DRAFT` (and every other target) skips PRESEASON, the only entry work that
    builds the schedule, so the season is stranded the same way `SETUP -> REGULAR_SEASON`
    strands it -- the refusal is about where the league stands, not where it is going."""
    league_id = await bare_league_at(pool, Phase.SETUP)
    try:
        history_before = await _history_rows(pool, league_id)

        with pytest.raises(PhaseError, match="schedule") as refused:
            await _force(pool, league_id, target)

        assert "stranded" in str(refused.value)
        assert "Run /advance" in str(refused.value)
        assert await _phase(pool, league_id) is Phase.SETUP
        assert await _history_rows(pool, league_id) == history_before
        assert await _claim_is_free(pool, league_id)
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize("target", [Phase.DRAFT, Phase.REGULAR_SEASON])
async def test_force_from_setup_is_allowed_once_the_season_has_a_schedule(pool, target):
    """The twin of the refusal above: the refusal is about the missing schedule and nothing
    else, so a season that HAS games is forced out of `SETUP` as before."""
    league_id = await bare_league_at(pool, Phase.SETUP)
    try:
        await add_played_game(pool, league_id)

        report = await _force(pool, league_id, target)

        assert report.from_phase is Phase.SETUP
        assert await _phase(pool, league_id) is target
    finally:
        await drop(pool, league_id)


async def test_force_still_overrides_an_unmet_games_target_and_names_it(pool):
    """The one thing the no-schedule refusal is NOT: a schedule that exists but has not been
    played is overridden (the spec's letter, P5-99.g) and named in the report and the audit
    row."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        await add_game(pool, league_id, teams, game_index=0, game_date=date(2026, 10, 20))

        report = await _force(pool, league_id, Phase.TRADE_DEADLINE)

        assert [b.code for b in report.overrode] == [BlockerCode.GAMES_TARGET_NOT_REACHED]
        assert await _phase(pool, league_id) is Phase.TRADE_DEADLINE
        assert report.overrode[0].message in (await _last_transition(pool, league_id))["reason"]
    finally:
        await drop(pool, league_id)


async def test_force_refuses_setup_as_a_target_and_writes_nothing(pool):
    """Critic 3, B1: `SETUP` is "only ever a source". Forced into, the next `/advance` would
    take `_successor(SETUP) = PRESEASON` -- a second `PRESEASON` history row, a second cycle
    ordinal, a second hook and a second media cycle for the same season."""
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        history_before = await _history_rows(pool, league_id)

        with pytest.raises(PhaseError) as refused:
            await _force(pool, league_id, Phase.SETUP)

        assert str(refused.value) == season_service.FORCE_SETUP_MESSAGE
        assert await _phase(pool, league_id) is Phase.PLAYIN
        assert await _history_rows(pool, league_id) == history_before
        assert await _claim_is_free(pool, league_id)
    finally:
        await drop(pool, league_id)


async def test_a_refused_target_takes_no_claim_at_all(pool, monkeypatch):
    """The target rule is checked from the first read too, so a refusal never touches the
    claim (a person refused for `SETUP` must not see the league flicker "busy")."""
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        claims: list[int] = []
        real = season_service._claim_league

        async def spy(pool_, league_id_, claimed_by):
            claims.append(league_id_)
            return await real(pool_, league_id_, claimed_by)

        monkeypatch.setattr(season_service, "_claim_league", spy)

        with pytest.raises(PhaseError):
            await _force(pool, league_id, Phase.SETUP)

        assert claims == []
    finally:
        await drop(pool, league_id)


async def test_the_target_rule_is_checked_again_under_the_claim_against_the_phase_then(
    pool, monkeypatch
):
    """The first check reads the league BEFORE the claim; whoever moves the league in between
    changes what "forward" means. Here the league is moved on after the claim is taken (a
    `tools/` advance), so the target that was ahead is now behind -- and the refusal is the
    claimed read's."""
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        real = season_service._claim_league

        async def claim_then_move(pool_, league_id_, claimed_by):
            claimed_at = await real(pool_, league_id_, claimed_by)
            await pool_.execute(
                "UPDATE leagues SET current_phase = $2 WHERE id = $1",
                league_id_,
                Phase.FINALS.value,
            )
            return claimed_at

        monkeypatch.setattr(season_service, "_claim_league", claim_then_move)

        with pytest.raises(PhaseError) as refused:
            await _force(pool, league_id, Phase.PLAYOFFS_R1)

        assert str(refused.value) == season_service.force_not_forward_message(
            Phase.FINALS, Phase.PLAYOFFS_R1
        )
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (Phase.DRAFT, Phase.REGULAR_SEASON),
        (Phase.AWARDS, Phase.PLAYIN),
        (Phase.WAIVERS, Phase.DRAFT),
        (Phase.FREE_AGENCY, Phase.RESIGN_WINDOW),
    ],
)
async def test_force_refuses_a_target_that_is_not_ahead_of_the_current_phase(pool, current, target):
    """A backwards target (or anything out of `WAIVERS`, whose only edge crosses the season
    boundary) would replay phase work and history rows that already exist. The refusal
    names both phases and is not the `PRESEASON`, `SETUP` or same-phase sentence."""
    league_id = await bare_league_at(pool, current)
    try:
        history_before = await _history_rows(pool, league_id)

        with pytest.raises(PhaseError) as refused:
            await _force(pool, league_id, target)

        assert str(refused.value) == season_service.force_not_forward_message(current, target)
        assert await _phase(pool, league_id) is current
        assert await _history_rows(pool, league_id) == history_before
        assert await _claim_is_free(pool, league_id)
    finally:
        await drop(pool, league_id)


async def test_force_to_the_phase_the_league_is_already_in_is_refused(pool):
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        with pytest.raises(PhaseError, match="already"):
            await _force(pool, league_id, Phase.PLAYIN)
    finally:
        await drop(pool, league_id)


@pytest.mark.parametrize("reason", ["", "   "])
async def test_force_requires_a_reason(pool, reason):
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        with pytest.raises(ValidationError):
            await _force(pool, league_id, Phase.PLAYOFFS_R1, reason=reason)

        assert await _phase(pool, league_id) is Phase.PLAYIN
    finally:
        await drop(pool, league_id)


async def test_force_is_commissioner_only(pool):
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        with pytest.raises(PermissionDenied):
            await _force(pool, league_id, Phase.PLAYOFFS_R1, actor=_OTHER_USER)

        assert await _phase(pool, league_id) is Phase.PLAYIN
    finally:
        await drop(pool, league_id)


async def test_force_refuses_while_another_advance_holds_the_claim(pool):
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:
        claimed_at = await season_service._claim_league(pool, league_id, _OTHER_USER)

        with pytest.raises(ConcurrencyError):
            await _force(pool, league_id, Phase.PLAYOFFS_R1)

        assert await _phase(pool, league_id) is Phase.PLAYIN
        await season_service._release_claim(pool, league_id, _OTHER_USER, claimed_at)
    finally:
        await drop(pool, league_id)


async def test_a_force_that_fails_still_releases_the_claim(pool, monkeypatch):
    league_id = await bare_league_at(pool, Phase.PLAYIN)
    try:

        async def boom(*args, **kwargs):
            raise RuntimeError("the pointer write failed")

        monkeypatch.setattr(season_service, "_force_pointer", boom)

        with pytest.raises(RuntimeError, match="pointer write"):
            await _force(pool, league_id, Phase.PLAYOFFS_R1)

        assert await _claim_is_free(pool, league_id)
        assert await _phase(pool, league_id) is Phase.PLAYIN
    finally:
        await drop(pool, league_id)
