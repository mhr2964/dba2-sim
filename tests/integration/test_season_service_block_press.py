"""The block press and the failed-game policy (phase-5-stage-e-advance.md P5-99.b.2
row 3, P5-99.i, P5-99.u, P5-99.e.1, build-plan commit 10), over real rows.

Row 3 of the press-kind table is ordered AHEAD of the exit gate on purpose: an
unmet games target IS the gate's schedule blocker, and a block press is what
resolves it, so checking the gate first would refuse every ordinary game-day
press. What these cases prove, in order: a press sims exactly the next unplayed
`game_date` and no more; the bound is capped at the phase's own target; the
stats rebuild fires on the press that finishes the regular season (and no media
cycle runs there: `REGULAR_SEASON_COMPLETE` is not cycle-bearing); a game that
cannot sim makes the press refuse with the cause, retries exactly once, and does
not loop; and the block path's ordinal is counted under the row lock.

`gm_service.run_phase_hook` is stubbed: the hook is proved elsewhere and the
fixtures set its checkpoint, so nothing here is also a hook case.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import asyncio
import random
from datetime import date

import pytest

from dba2.core.errors import PhaseError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, league_repo, media_repo, team_repo
from dba2.domain.game import GameStatus, GameType
from dba2.domain.phase import Phase
from dba2.phase import service as phase_service
from dba2.services import game_sim_service, gm_service, season_service, stats_service
from dba2.services.season_service import BlockerCode, PressKind
from tests.integration._press_fixtures import (
    COMMISSIONER,
    SEASON,
    add_game,
    add_two_teams,
    bare_league_at,
    drop,
    press,
    regular_games_by_date,
    sim_league_at,
)
from tests.integration.test_season_service_press import _wait_until_a_statement_waits_on_a_row_lock


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


@pytest.fixture(autouse=True)
def hook_calls(monkeypatch):
    calls: list[Phase] = []

    async def hook(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        calls.append(phase)
        return gm_service.PhaseHookResult(league_id=league_id, season=SEASON, phase=phase)

    monkeypatch.setattr(gm_service, "run_phase_hook", hook)
    return calls


@pytest.fixture
def no_phase_work(monkeypatch):
    """Entering `TRADE_DEADLINE` runs a real agency checkpoint that needs a
    bootstrapped league; the cases that cross into it are about the block cursor."""
    monkeypatch.setitem(season_service._WORK_UNITS, season_service.WorkKind.ENTRY, {})


@pytest.fixture
def sim_calls(monkeypatch):
    """Counts `sim_scheduled_games` calls and passes each straight through."""
    calls: list[dict] = []
    real = game_sim_service.sim_scheduled_games

    async def counting(ex, league_id, season, **kwargs):
        calls.append(dict(kwargs))
        return await real(ex, league_id, season, **kwargs)

    monkeypatch.setattr(game_sim_service, "sim_scheduled_games", counting)
    return calls


async def _statuses(pool, league_id: int) -> dict[int, GameStatus]:
    games = await game_repo.list_games_for_league_season(pool, league_id, SEASON)
    return {g.id: g.status for g in games}


async def _phase_and_entries(pool, league_id: int) -> tuple[Phase, int]:
    league = await league_repo.get_league(pool, league_id)
    return league.current_phase, await league_repo.count_phase_entries(pool, league_id, SEASON)


# --- one press = one game date -------------------------------------------------


async def test_gate_facts_for_another_phase_are_refused_not_trusted(pool):
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        league = await league_repo.get_league(pool, league_id)

        with pytest.raises(RuntimeError, match="TRADE_DEADLINE"):
            await season_service._games_target_met(
                pool, league, facts=season_service.GateFacts(phase=Phase.TRADE_DEADLINE)
            )
    finally:
        await drop(pool, league_id)


async def test_a_block_press_that_meets_its_target_reads_the_gate_facts_three_times_not_four(
    pool, hook_calls, monkeypatch
):
    """Critic 2, nit 10: the facts were loaded once for the press's kind, once after the sim
    for the report's verdict, and once more inside `_games_target_met` to decide whether the
    closing work is due -- the same read as the one just before it (nothing between them can
    change a game: the claim excludes every human write and the hook plays none). The block
    press hands the post-sim facts down. The prefix's own check (closing work owed, target not
    yet met) is the first read, the press's kind the second, the post-sim read the third."""
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=2)
    try:
        loads: list[Phase] = []
        real = season_service.load_gate_facts

        async def counting(ex, league):
            loads.append(league.current_phase)
            return await real(ex, league)

        monkeypatch.setattr(season_service, "load_gate_facts", counting)

        report = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert report.kind is PressKind.BLOCK
        assert report.media_cycle_index is not None, "the block met the target: closing work ran"
        assert len(loads) == 3
    finally:
        await drop(pool, league_id)


async def test_a_block_press_sims_exactly_the_next_unplayed_game_date_and_no_more(pool, hook_calls):
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
    try:
        days = await regular_games_by_date(pool, league_id)
        (first_date, first_games), (second_date, second_games) = days[0], days[1]
        before = await _phase_and_entries(pool, league_id)

        report = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert report.kind is PressKind.BLOCK
        assert report.from_phase is Phase.REGULAR_SEASON and report.to_phase is None
        assert report.block is not None
        assert report.block.block_id == first_date.isoformat(), "the documented block id (F7)"
        assert report.block.season == SEASON
        assert {g.game_id for g in report.block.games} == {g.id for g in first_games}
        assert report.failed_games == ()
        statuses = await _statuses(pool, league_id)
        assert all(statuses[g.id] is GameStatus.FINAL for g in first_games)
        assert all(statuses[g.id] is GameStatus.SCHEDULED for g in second_games), "no more"
        assert await _phase_and_entries(pool, league_id) == before, "a block moves no pointer"
        assert (await league_repo.get_league(pool, league_id)).advance_claimed_at is None
        assert hook_calls == []
        # The gate is evaluated on a block press too, and its verdict is in the reply:
        assert report.verdict.allowed is False
        assert [b.code for b in report.verdict.blockers] == [BlockerCode.GAMES_TARGET_NOT_REACHED]

        second = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert second.kind is PressKind.BLOCK
        assert second.block.block_id == second_date.isoformat()
        assert {g.game_id for g in second.block.games} == {g.id for g in second_games}
    finally:
        await drop(pool, league_id)


async def test_the_bound_is_capped_at_the_phases_target_and_the_split_day_shares_one_block_id(
    pool, no_phase_work
):
    """15 games: `REGULAR_SEASON`'s target index is 6 and `TRADE_DEADLINE`'s is 8, and
    game-date 5 holds indexes 6, 7 and 8. The block that ends `REGULAR_SEASON` sims
    index 6 ONLY; `TRADE_DEADLINE`'s first block sims 7 and 8 -- and both carry the
    same block id, so `open_block` merges them into one `#games` block."""
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
    try:
        days = await regular_games_by_date(pool, league_id)
        split_date, split_games = days[4]
        assert [g.game_index for g in split_games] == [6, 7, 8]
        for _ in range(4):
            await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        last_of_regular_season = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert last_of_regular_season.kind is PressKind.BLOCK
        assert [g.game_id for g in last_of_regular_season.block.games] == [split_games[0].id]
        statuses = await _statuses(pool, league_id)
        assert statuses[split_games[1].id] is GameStatus.SCHEDULED
        assert statuses[split_games[2].id] is GameStatus.SCHEDULED
        assert last_of_regular_season.verdict.allowed is True, "the block met the target"

        into_deadline = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        assert into_deadline.kind is PressKind.ENTRY
        assert into_deadline.to_phase is Phase.TRADE_DEADLINE

        first_deadline_block = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        assert first_deadline_block.kind is PressKind.BLOCK
        assert [g.game_id for g in first_deadline_block.block.games] == [
            split_games[1].id,
            split_games[2].id,
        ]
        assert (
            first_deadline_block.block.block_id
            == last_of_regular_season.block.block_id
            == split_date.isoformat()
        )
    finally:
        await drop(pool, league_id)


# --- REGULAR_SEASON_COMPLETE: the rebuild, and no media cycle ------------------


async def test_the_stats_rebuild_fires_on_the_press_that_reaches_the_end_and_no_media_runs(
    pool, monkeypatch
):
    """Closing work is due only once the phase's OWN target is met (`_games_target_met`,
    reused, not reimplemented): the two presses before the last leave the rebuild
    unrun and its checkpoint at -1, and the press that finalizes the last game runs
    it exactly once and sets the checkpoint to the entry's index. There is no media
    cycle here -- `REGULAR_SEASON_COMPLETE` is not cycle-bearing."""
    rebuilds: list[tuple] = []
    real_rebuild = stats_service.rebuild_player_season_stats

    async def spy(ex, league_id, season, game_type):
        rebuilds.append((league_id, season, game_type))
        return await real_rebuild(ex, league_id, season, game_type)

    monkeypatch.setattr(stats_service, "rebuild_player_season_stats", spy)
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON_COMPLETE, keep_games=4)
    try:
        entry_index = (await league_repo.count_phase_entries(pool, league_id, SEASON)) - 1
        for _ in range(2):
            report = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
            assert report.kind is PressKind.BLOCK
            assert rebuilds == []
            league = await league_repo.get_league(pool, league_id)
            assert league.closing_work_completed_index == -1

        last = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert last.kind is PressKind.BLOCK
        assert last.verdict.allowed is True
        assert rebuilds == [(league_id, SEASON, GameType.REGULAR)]
        league = await league_repo.get_league(pool, league_id)
        assert league.closing_work_completed_index == entry_index
        with_games = await pool.fetchval(
            "SELECT count(DISTINCT player_id) FROM player_game_stats pgs "
            "JOIN games g ON g.id = pgs.game_id WHERE g.league_id = $1",
            league_id,
        )
        with_season_row = await pool.fetchval(
            "SELECT count(*) FROM player_season_stats WHERE league_id = $1 AND season = $2",
            league_id,
            SEASON,
        )
        assert with_games > 0
        assert with_season_row == with_games, "E-25: every player with games has a season row"
        assert (
            await media_repo.list_media_stories_for_league_season(
                pool, league_id=league_id, season=SEASON
            )
            == []
        )

        into_playin = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        assert into_playin.kind is PressKind.ENTRY and into_playin.to_phase is Phase.PLAYIN
        assert len(rebuilds) == 1, "the checkpoint stops the rebuild running again"
    finally:
        await drop(pool, league_id)


# --- a game that cannot sim ------------------------------------------------------


async def test_a_deterministically_failing_game_refuses_the_press_with_its_cause(pool, sim_calls):
    """A team row with no strategy fails in `sim_scheduled_games` every time. The
    press retries the bound ONCE (a lock timeout or a blip is the ordinary
    failure), then refuses -- naming the id and the cause from that call's own
    `BatchResult` -- rather than returning "success, nothing happened" and
    inviting a loop. The claim is released, nothing moved, and the same press
    repeats the refusal: progress needs the postponement."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        game = await add_game(pool, league_id, teams, game_index=0, game_date=date(2026, 11, 1))
        before = await _phase_and_entries(pool, league_id)

        with pytest.raises(season_service.GamesFailedToSimError) as refused:
            await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert refused.value.game_ids == (game.id,)
        assert [f.game_id for f in refused.value.failures] == [game.id]
        assert "strategy" in refused.value.failures[0].cause
        assert str(game.id) in str(refused.value)
        assert "not played yet" in str(refused.value), "the gate line, in the same reply"
        assert len(sim_calls) == 2, "one bounded retry, and only one"
        assert (await _statuses(pool, league_id))[game.id] is GameStatus.SCHEDULED
        assert await _phase_and_entries(pool, league_id) == before
        assert (await league_repo.get_league(pool, league_id)).advance_claimed_at is None

        with pytest.raises(season_service.GamesFailedToSimError):
            await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        assert await _phase_and_entries(pool, league_id) == before, "repeating changes nothing"
    finally:
        await drop(pool, league_id)


async def test_the_gate_names_the_failing_game_as_unplayed_never_as_failed(pool):
    """P5-99.e.1: nothing persists a failure, so after the press the gate can only say
    "not played yet" about the game -- which is also true of a game nobody has tried."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        game = await add_game(pool, league_id, teams, game_index=0, game_date=date(2026, 11, 1))
        with pytest.raises(season_service.GamesFailedToSimError):
            await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        league = await league_repo.get_league(pool, league_id)
        verdict = season_service.evaluate_exit(await season_service.load_gate_facts(pool, league))

        assert verdict.allowed is False
        (blocker,) = verdict.blockers
        assert blocker.code is BlockerCode.GAMES_TARGET_NOT_REACHED
        assert blocker.game_ids == (game.id,)
        assert "not played yet" in blocker.message and "fail" not in blocker.message
    finally:
        await drop(pool, league_id)


async def test_postponing_the_failing_game_lets_the_next_press_make_progress(pool, no_phase_work):
    """The two-press progress test: without the press-level refusal the same press
    would repeat forever, and without the postponement the refusal would too. After
    the postponement the schedule blocker is gone, so the next press is an ordinary
    Entry press."""
    league_id = await bare_league_at(pool, Phase.REGULAR_SEASON)
    try:
        teams = await add_two_teams(pool, league_id)
        game = await add_game(pool, league_id, teams, game_index=0, game_date=date(2026, 11, 1))
        with pytest.raises(season_service.GamesFailedToSimError):
            await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        await season_service.postpone_game(
            pool,
            league_id=league_id,
            actor_user_id=COMMISSIONER,
            game_id=game.id,
            reason="a team has no strategy row",
        )
        report = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert report.kind is PressKind.ENTRY
        assert report.to_phase is Phase.TRADE_DEADLINE
        assert (await _statuses(pool, league_id))[game.id] is GameStatus.POSTPONED
    finally:
        await drop(pool, league_id)


async def test_one_failing_game_in_a_day_does_not_stop_the_others_and_is_reported(
    pool, sim_calls, monkeypatch
):
    """A partial failure is a press that progressed: the day's other games are final
    and the report lists the one that was not (with its cause), so the press is not a
    refusal. The NEXT press finds only the failed game left in that day, finalizes
    nothing, and refuses -- the loop guard at its narrowest."""
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
    try:
        days = await regular_games_by_date(pool, league_id)
        for _ in range(2):  # game dates 1 and 2 hold one game each
            await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        _third_date, (doomed, survivor) = days[2]
        await pool.execute(
            "DELETE FROM team_strategy WHERE team_id = $1 AND season = $2",
            doomed.home_team_id,
            SEASON,
        )
        sim_calls.clear()

        report = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert report.kind is PressKind.BLOCK
        assert [g.game_id for g in report.block.games] == [survivor.id]
        assert report.failed_games == (doomed.id,)
        assert [f.game_id for f in report.failures] == [doomed.id]
        assert len(sim_calls) == 2, "the failure was retried once"

        with pytest.raises(season_service.GamesFailedToSimError) as refused:
            await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        assert refused.value.game_ids == (doomed.id,)

        await season_service.postpone_game(
            pool,
            league_id=league_id,
            actor_user_id=COMMISSIONER,
            game_id=doomed.id,
            reason="no strategy row",
        )
        after = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        assert after.kind is PressKind.BLOCK
        assert after.block.block_id == days[3][0].isoformat(), "the cursor moved on"
    finally:
        await drop(pool, league_id)


async def test_a_transient_failure_is_absorbed_by_the_one_retry(pool, sim_calls, monkeypatch):
    """The ordinary failure is transient. The first `sim_scheduled_games` call loses
    one team's strategy row (as a snapshot read that raced a write would); the second
    call, the retry, sees it. The press finishes the whole day and reports nothing
    failed."""
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
    try:
        days = await regular_games_by_date(pool, league_id)
        for _ in range(2):
            await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        _date, day_games = days[2]
        victim = day_games[0].home_team_id
        sim_calls.clear()
        real_get = team_repo.get_team_strategy

        async def flaky(ex, team_id, season):
            if len(sim_calls) == 1 and team_id == victim:
                return None
            return await real_get(ex, team_id, season)

        monkeypatch.setattr(team_repo, "get_team_strategy", flaky)

        report = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert len(sim_calls) == 2
        assert report.failed_games == ()
        assert {g.game_id for g in report.block.games} == {g.id for g in day_games}
        statuses = await _statuses(pool, league_id)
        assert all(statuses[g.id] is GameStatus.FINAL for g in day_games)
    finally:
        await drop(pool, league_id)


# --- the block path's counters are read under the row lock (M1, M-6) -------------


async def test_the_block_path_reads_its_counters_under_the_row_lock(pool):
    """`_read_pointer_locked` is the Block/Draft-resume/Blocked path's read of
    `entry_index` and `cycle_ordinal`. A `tools/`-style writer holds the `leagues`
    row, walks the pointer `RESIGN_WINDOW -> FREE_AGENCY -> RESIGN_WINDOW` and
    commits while the read waits for the lock: counted BEFORE the lock, the read
    misses both entries and returns the indexes the writer has since moved past."""
    league_id = await bare_league_at(pool, Phase.RESIGN_WINDOW)
    holder = await pool.acquire()
    try:
        entries_before = await league_repo.count_phase_entries(pool, league_id, SEASON)
        cycles_before = await league_repo.count_phase_entries(
            pool, league_id, SEASON, season_service.CYCLE_BEARING_PHASES
        )
        tx = holder.transaction()
        await tx.start()
        await holder.fetchval("SELECT id FROM leagues WHERE id = $1 FOR UPDATE", league_id)
        reader = asyncio.create_task(
            season_service._read_pointer_locked(
                pool, league_id, expected_phase=Phase.RESIGN_WINDOW, expected_season=SEASON
            )
        )
        await _wait_until_a_statement_waits_on_a_row_lock(pool, holder_pid=holder.get_server_pid())
        await phase_service.force_advance(
            holder, league_id, Phase.FREE_AGENCY, COMMISSIONER, reason="concurrent jump"
        )
        await phase_service.force_advance(
            holder, league_id, Phase.RESIGN_WINDOW, COMMISSIONER, reason="and back"
        )
        await tx.commit()

        pointer = await asyncio.wait_for(reader, timeout=5)

        assert pointer.entry_index == entries_before + 2 - 1
        assert pointer.cycle_ordinal == cycles_before + 2 - 1
    finally:
        await pool.release(holder)
        await drop(pool, league_id)


# --- a schedule phase with no schedule (critic m2) -----------------------------------


@pytest.mark.parametrize(
    "phase",
    [
        Phase.REGULAR_SEASON,
        Phase.TRADE_DEADLINE,
        Phase.POST_DEADLINE,
        Phase.REGULAR_SEASON_COMPLETE,
    ],
)
async def test_a_schedule_phase_with_no_schedule_is_blocked_and_runs_no_closing_work(pool, phase):
    """With no game there is nothing to reach a target WITH: the press is `Blocked`
    naming the missing schedule, and the phase's closing work (media cycle, or the stats
    rebuild at `REGULAR_SEASON_COMPLETE`) does not run -- its checkpoint stays behind, so
    a cycle is never recorded over zero games (the monotone checkpoint would make the
    press that finally plays them skip it)."""
    league_id = await bare_league_at(pool, phase)
    try:
        report = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert report.kind is PressKind.BLOCKED
        assert [b.code for b in report.verdict.blockers] == [BlockerCode.SCHEDULE_MISSING]
        league = await league_repo.get_league(pool, league_id)
        assert league.current_phase is phase
        assert league.closing_work_completed_index == -1, "no closing work ran"
        assert report.media_cycle_index is None
        stories = await media_repo.list_media_stories_for_league_season(
            pool, league_id=league_id, season=SEASON, cycle_index=0
        )
        assert stories == []
        assert league.advance_claimed_at is None
    finally:
        await drop(pool, league_id)


# --- a league with no phase_history for the phase it stands in (critic m3) --------------


async def test_a_press_in_a_league_placed_mid_season_by_hand_is_a_named_refusal(pool):
    """`entry_index` is `count - 1` on every press that does not move the pointer, which
    needs the entry it is inside to be counted. A league created straight at a mid-season
    phase has no `phase_history` row at all (the state Phase 7's rollover, not built, would
    also leave a new season in): the press refuses in words, releases its claim and writes
    nothing, instead of raising a bare `ValueError`."""
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="No History Test League",
        start_season_year=SEASON,
        current_season=SEASON,
        commissioner_user_id=COMMISSIONER,
        rng_seed=1,
        current_phase=Phase.REGULAR_SEASON,
    )
    try:
        with pytest.raises(PhaseError, match="phase_history"):
            await press(pool, league_id=league.id, actor_user_id=COMMISSIONER)

        assert (await league_repo.get_league(pool, league.id)).advance_claimed_at is None
        assert await league_repo.count_phase_entries(pool, league.id, SEASON) == 0
    finally:
        await drop(pool, league.id)
