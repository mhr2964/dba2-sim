"""Phase work and its two completion checkpoints (phase-5-stage-e-advance.md
P5-99.b.1, P5-99.r, P5-99.r.1, P5-99.r.2, build-plan commit 9), over real rows.

Three things are under proof: that each phase's entry/closing work runs where the
per-phase table says (the `PRESEASON` pair with its games guard, both agency
checkpoints with the cap-settings assertion, the stats rebuild), that the
per-player guard makes a checkpoint interrupted after N players converge without
folding anyone twice (B1 case a), and that a press which crashed after the pointer
moved is finished by the NEXT press of ANY kind, a `Blocked` one included (B1
case b).

`gm_service.run_phase_hook` is stubbed throughout: the hook is commit 8's subject
and is proved there. `run_agency_checkpoint`, `ensure_defaults_for_season`,
`generate_regular_season` are the REAL functions over real rows -- the point is
that the press calls them correctly.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import random
from datetime import date

import pytest

from dba2.core.errors import PhaseError
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, league_repo, motivation_repo, team_repo
from dba2.domain.game import GameType
from dba2.domain.phase import Phase
from dba2.domain.team import GmPhilosophy, Timeline
from dba2.phase import service as phase_service
from dba2.services import (
    agency_service,
    gm_service,
    league_service,
    ratings_service,
    season_service,
    stats_service,
)
from dba2.services.season_service import PressKind
from tests.integration._press_fixtures import add_played_game, press
from tests.integration.test_agency_service import _make_contract, _make_player, _make_team
from tests.integration.test_agency_service import _make_league as _make_agency_league

_SEASON = 2026
_COMMISSIONER = 1

# Direct-advance order for building a league's real history without pressing.
_WALK = [
    Phase.PRESEASON,
    Phase.REGULAR_SEASON,
    Phase.TRADE_DEADLINE,
    Phase.POST_DEADLINE,
    Phase.REGULAR_SEASON_COMPLETE,
    Phase.PLAYIN,
    Phase.PLAYOFFS_R1,
    Phase.PLAYOFFS_R2,
    Phase.CONFERENCE_FINALS,
    Phase.FINALS,
    Phase.AWARDS,
    Phase.DRAFT_LOTTERY,
    Phase.DRAFT,
    Phase.POST_DRAFT_TRADES,
    Phase.PROGRESSION,
]


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


@pytest.fixture(autouse=True)
def _stub_the_hook(monkeypatch):
    """The hook is commit 8's, and its real body would need a full league."""

    async def hook(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        return gm_service.PhaseHookResult(league_id=league_id, season=_SEASON, phase=phase)

    monkeypatch.setattr(gm_service, "run_phase_hook", hook)


async def _walk_to(pool, league_id: int, phase: Phase) -> None:
    for target in _WALK:
        await phase_service.advance(pool, league_id, target, _COMMISSIONER)
        if target is phase:
            return
    raise AssertionError(f"{phase} is not in the direct-advance walk")


async def _league_with_thirty_teams(pool) -> int:
    """`league_service.create_league`: 30 real teams and the creation season's
    cap-settings row, no players, no schedule."""
    league = await league_service.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Phase Work Test League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=7,
    )
    return league.id


async def _league_with_a_player_on_every_team(pool) -> int:
    """Thirty real teams, one rostered player each: the least a REAL
    `ensure_defaults_for_season` will derive strategy defaults from (it refuses a
    team with no players). `PRESEASON`'s entry work runs both real functions."""
    league_id = await _league_with_thirty_teams(pool)
    for team in await team_repo.list_teams_for_league(pool, league_id):
        await _make_player(pool, league_id, team.id)
    await ratings_service.derive_tendencies_for_league(pool, league_id)
    return league_id


async def _league_of_players(pool) -> tuple[int, list[int], list[int]]:
    """A league with two teams, four rostered contracted players and their
    motivation rows bootstrapped, plus its cap-settings row: everything a real
    `run_agency_checkpoint` needs. Returns `(league_id, team_ids, player_ids)`."""
    league_id = await _make_agency_league(pool)
    team_a = await _make_team(
        pool, league_id, "PA", gm_philosophy=GmPhilosophy.STAR_MAXER, timeline=Timeline.WIN_NOW
    )
    team_b = await _make_team(
        pool, league_id, "PB", gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER, timeline=Timeline.REBUILD
    )
    player_ids = []
    for team_id, overall, age, salary in (
        (team_a, 88, 28, 30_000_000),
        (team_a, 74, 26, 12_000_000),
        (team_b, 66, 23, 3_000_000),
        (team_b, 81, 30, 25_000_000),
    ):
        player_id = await _make_player(pool, league_id, team_id, overall_cached=overall, age=age)
        await _make_contract(
            pool, league_id, team_id, player_id, start_season=_SEASON, salaries=(salary, salary)
        )
        player_ids.append(player_id)
    await agency_service.bootstrap_league_motivation(
        pool, league_id=league_id, league_rng_seed=4242, season=_SEASON
    )
    return league_id, [team_a, team_b], player_ids


async def _drop(pool, league_id: int) -> None:
    await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)


async def _checkpoints(pool, league_id: int) -> tuple[int, int, int]:
    league = await league_repo.get_league(pool, league_id)
    return (
        league.hook_completed_cycle_index,
        league.entry_work_completed_index,
        league.closing_work_completed_index,
    )


async def _event_counts(pool, player_ids: list[int]) -> dict[int, int]:
    return {
        pid: len(await motivation_repo.list_motivation_events_for_player(pool, pid))
        for pid in player_ids
    }


async def _one_scheduled_game(pool, league_id: int, team_ids: list[int]):
    """A single `scheduled` regular-season game at `game_index` 0 between two
    hand-made teams that have no strategy rows: the schedule phase's games target is
    unmet (so the press is a Block press) and the game cannot sim (so that press
    refuses at the sim, AFTER its work prefix)."""
    return await game_repo.create_game(
        pool,
        league_id=league_id,
        season=_SEASON,
        game_index=0,
        game_date=date(2026, 11, 1),
        home_team_id=team_ids[0],
        away_team_id=team_ids[1],
        game_type=GameType.REGULAR,
        rng_seed=1,
    )


# --- PRESEASON: the schedule pair, games-guarded --------------------------------


async def test_a_preseason_press_on_an_unscheduled_league_writes_the_full_schedule(pool):
    league_id = await _league_with_a_player_on_every_team(pool)
    try:
        report = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert (report.from_phase, report.to_phase) == (Phase.SETUP, Phase.PRESEASON)
        games = await game_repo.list_games_for_league_season(pool, league_id, _SEASON)
        assert len(games) == 1230
        strategies = await pool.fetchval(
            "SELECT COUNT(*) FROM team_strategy WHERE team_id IN "
            "(SELECT id FROM teams WHERE league_id = $1)",
            league_id,
        )
        assert strategies == 30, "ensure_defaults_for_season ran, before the schedule"
        assert (await _checkpoints(pool, league_id))[1] == 0, "entry work checkpointed at entry 0"
    finally:
        await _drop(pool, league_id)


async def test_a_second_run_of_the_preseason_entry_work_does_not_raise_on_the_existing_schedule(
    pool,
):
    """The games guard is load-bearing, not defensive: `generate_regular_season`
    raises `ValidationError` on an existing schedule. A crash between the work and
    its checkpoint re-enters the whole list on the next press (P5-99.r.3), so the
    second run happens for real. Removing the guard makes this press raise."""
    league_id = await _league_with_a_player_on_every_team(pool)
    try:
        await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        # "committed, before its checkpoint": the work is done and the checkpoint is not.
        await pool.execute(
            "UPDATE leagues SET entry_work_completed_index = -1 WHERE id = $1", league_id
        )

        second = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert (second.from_phase, second.to_phase) == (Phase.PRESEASON, Phase.REGULAR_SEASON)
        games = await game_repo.list_games_for_league_season(pool, league_id, _SEASON)
        assert len(games) == 1230, "the schedule was not regenerated on top of itself"
        assert (await _checkpoints(pool, league_id))[1] == 0
    finally:
        await _drop(pool, league_id)


# --- TRADE_DEADLINE / PROGRESSION: the agency checkpoints ----------------------


async def test_a_trade_deadline_press_returns_the_checkpoint_result_and_sets_the_entry_checkpoint(
    pool,
):
    league_id, teams, player_ids = await _league_of_players(pool)
    try:
        await _walk_to(pool, league_id, Phase.REGULAR_SEASON)
        await add_played_game(pool, league_id, (teams[0], teams[1]))

        report = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert (report.kind, report.to_phase) == (PressKind.ENTRY, Phase.TRADE_DEADLINE)
        assert report.checkpoint_result is not None
        assert report.checkpoint_result.players_evaluated == len(player_ids)
        assert report.checkpoint_result.players_skipped == 0
        assert (await _checkpoints(pool, league_id))[1] == report.entry_index == 2
        for pid in player_ids:
            m = await motivation_repo.get_player_motivation(pool, pid)
            assert (m.last_evaluated_season, m.last_checkpoint_phase) == (
                _SEASON,
                Phase.TRADE_DEADLINE,
            )
    finally:
        await _drop(pool, league_id)


async def test_a_progression_press_runs_the_second_checkpoint_under_its_own_phase(pool):
    league_id, _teams, player_ids = await _league_of_players(pool)
    try:
        await _walk_to(pool, league_id, Phase.POST_DRAFT_TRADES)

        report = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert (report.kind, report.to_phase) == (PressKind.ENTRY, Phase.PROGRESSION)
        assert report.checkpoint_result.players_evaluated == len(player_ids)
        assert (await _checkpoints(pool, league_id))[1] == report.entry_index == 14
        m = await motivation_repo.get_player_motivation(pool, player_ids[0])
        assert m.last_checkpoint_phase is Phase.PROGRESSION
    finally:
        await _drop(pool, league_id)


async def test_a_league_with_no_cap_settings_row_refuses_with_the_named_message(pool, monkeypatch):
    """`run_agency_checkpoint` needs a persisted `league_cap_settings` row for the
    season (`load_gm_inputs` requires one), so the press asserts it and refuses
    with a message that names the missing row, rather than letting a `NotFoundError`
    out of the middle of a checkpoint. The checkpoint itself never starts."""
    checkpoint_calls = []

    async def spy(*args, **kwargs):
        checkpoint_calls.append(kwargs)
        raise AssertionError("the checkpoint must not start without cap settings")

    monkeypatch.setattr(agency_service, "run_agency_checkpoint", spy)
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="No Cap Settings League",
        start_season_year=_SEASON,
        current_season=_SEASON,
        commissioner_user_id=_COMMISSIONER,
        rng_seed=1,
    )
    try:
        await _walk_to(pool, league.id, Phase.REGULAR_SEASON)
        await add_played_game(pool, league.id)

        with pytest.raises(PhaseError, match="league_cap_settings"):
            await press(pool, league_id=league.id, actor_user_id=_COMMISSIONER)

        assert checkpoint_calls == []
        state = await league_repo.get_league(pool, league.id)
        assert state.advance_claimed_at is None, "the claim was released on the refusal"
        assert state.entry_work_completed_index == -1, "unfinished work is not checkpointed"
    finally:
        await _drop(pool, league.id)


# --- B1 (a): a checkpoint interrupted after N players ---------------------------


async def test_an_interrupted_checkpoint_re_pressed_evaluates_every_player_exactly_once(
    pool, monkeypatch
):
    """The per-player guard, proved on the rows it protects. The state write and a
    player's event inserts share one transaction, so a crash at player 3 leaves
    players 1-2 fully applied and 3-4 untouched. Re-pressed, the checkpoint must
    apply the remainder WITHOUT re-folding the two already done -- asserted on
    `player_motivation_events` row counts and on a satisfaction value, not on a
    return count. Without the guard the first two players' events double."""
    league_id, teams, player_ids = await _league_of_players(pool)
    try:
        await _walk_to(pool, league_id, Phase.REGULAR_SEASON)
        await add_played_game(pool, league_id, (teams[0], teams[1]))

        real_update = motivation_repo.update_motivation_state
        writes = {"n": 0}

        async def dies_at_the_third_player(*args, **kwargs):
            writes["n"] += 1
            if writes["n"] > 2:
                raise RuntimeError("simulated crash at player 3")
            return await real_update(*args, **kwargs)

        monkeypatch.setattr(motivation_repo, "update_motivation_state", dies_at_the_third_player)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        monkeypatch.setattr(motivation_repo, "update_motivation_state", real_update)

        assert (await league_repo.get_league(pool, league_id)).current_phase is (
            Phase.TRADE_DEADLINE
        ), "the pointer moved before the work crashed"
        assert (await _checkpoints(pool, league_id))[1] == -1, "the entry work never finished"
        done = []
        for pid in player_ids:
            m = await motivation_repo.get_player_motivation(pool, pid)
            if m.last_checkpoint_phase is Phase.TRADE_DEADLINE:
                done.append(pid)
        assert len(done) == 2, "exactly the two players before the crash were applied"
        counts_before = await _event_counts(pool, player_ids)
        satisfaction_before = {
            pid: (await motivation_repo.get_player_motivation(pool, pid)).satisfaction_winning
            for pid in done
        }
        assert any(counts_before[pid] > 0 for pid in done), (
            "the interrupted players must have written events, or this proves nothing"
        )

        # The next press: its work prefix finishes the interrupted entry's checkpoint
        # before anything else (its one-game schedule is already played, so the gate then
        # allows it and the press is an Entry).
        resumed = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert resumed.kind is PressKind.ENTRY
        counts_after = await _event_counts(pool, player_ids)
        for pid in done:
            assert counts_after[pid] == counts_before[pid], f"player {pid} was folded twice"
            m = await motivation_repo.get_player_motivation(pool, pid)
            assert m.satisfaction_winning == satisfaction_before[pid], (
                f"player {pid}'s satisfaction moved on the resumed pass"
            )
        for pid in player_ids:
            m = await motivation_repo.get_player_motivation(pool, pid)
            assert m.last_checkpoint_phase is Phase.TRADE_DEADLINE, (
                f"player {pid} was never evaluated"
            )
        assert any(counts_after[pid] > counts_before[pid] for pid in player_ids if pid not in done)
        assert (await _checkpoints(pool, league_id))[1] == 2

        # ...and a third pass is a no-op for everyone.
        again = await agency_service.run_agency_checkpoint(
            pool, league_id=league_id, season=_SEASON, phase=Phase.TRADE_DEADLINE
        )
        assert (again.players_evaluated, again.players_skipped) == (0, len(player_ids))
        assert await _event_counts(pool, player_ids) == counts_after
    finally:
        await _drop(pool, league_id)


# --- B1 (b): finished by the next press of any kind, a Blocked one included -----


async def test_work_left_undone_after_the_pointer_moved_is_finished_by_the_next_press_of_any_kind(
    pool,
):
    """A press that crashed after the pointer moved and before the entry work
    finished leaves `entry_work_completed_index < entry_index`. The NEXT press --
    here a Block press that then refuses at the sim (its game cannot sim), so the
    work below can only have come from the prefix -- runs the work prefix first, so
    the remaining players are evaluated rather than stranded. Removing the entry-work
    checkpoint (or the prefix that reads it) leaves them unevaluated. (Build-plan
    commit 10 moved this case from a `Blocked` press to a Block one: an unmet games
    target is a Block press now, and no phase with entry work has any other blocker.
    The `Blocked` kind's prefix is proved by the hook-retry cases at `WAIVERS`.)"""
    league_id, team_ids, player_ids = await _league_of_players(pool)
    try:
        await _walk_to(pool, league_id, Phase.TRADE_DEADLINE)  # pointer moved, work never ran
        assert (await _checkpoints(pool, league_id))[1] == -1
        await _one_scheduled_game(pool, league_id, team_ids)  # the press will refuse at the sim

        with pytest.raises(season_service.GamesFailedToSimError):
            await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert (await _checkpoints(pool, league_id))[1] == 2
        for pid in player_ids:
            m = await motivation_repo.get_player_motivation(pool, pid)
            assert m.last_checkpoint_phase is Phase.TRADE_DEADLINE

        counts = await _event_counts(pool, player_ids)
        with pytest.raises(season_service.GamesFailedToSimError):
            await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        assert await _event_counts(pool, player_ids) == counts, (
            "a finished entry's work is not re-entered"
        )
    finally:
        await _drop(pool, league_id)


# --- the stats-rebuild slot, and B-1's empty-list case ---------------------------


@pytest.fixture
def rebuild_calls(monkeypatch):
    calls: list[tuple] = []

    async def spy(ex, league_id, season, game_type):
        calls.append((league_id, season, game_type))
        return 0

    monkeypatch.setattr(stats_service, "rebuild_player_season_stats", spy)
    return calls


async def test_regular_season_complete_rebuilds_the_stats_once_its_target_is_met(
    pool, rebuild_calls
):
    """Closing work is keyed by the phase's own games target: with a schedule and no
    game still scheduled in it the target is met on arrival, the work prefix of the very next press
    runs the rebuild, and `closing_work_completed_index` follows the entry index.
    PLAYIN, which owns no work, then neither re-runs it nor advances the
    checkpoint (B-1)."""
    league_id = await _league_with_thirty_teams(pool)
    try:
        await _walk_to(pool, league_id, Phase.REGULAR_SEASON_COMPLETE)
        await add_played_game(pool, league_id)

        into_playin = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        assert into_playin.to_phase is Phase.PLAYIN
        assert rebuild_calls == [(league_id, _SEASON, GameType.REGULAR)]
        assert (await _checkpoints(pool, league_id))[2] == 4, "keyed by RSC's entry index"

        out_of_playin = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)
        assert out_of_playin.to_phase is Phase.PLAYOFFS_R1
        assert len(rebuild_calls) == 1
        assert await _checkpoints(pool, league_id) == (-1, -1, 4), (
            "PLAYIN and PLAYOFFS_R1 own no work, so neither checkpoint moved"
        )
    finally:
        await _drop(pool, league_id)


async def test_the_rebuild_does_not_run_or_checkpoint_while_the_regular_season_is_unfinished(
    pool, rebuild_calls
):
    """The closing checkpoint is monotone, so a rebuild run BEFORE the games target
    is met would mark the closing work done and the press that finally plays the
    last game would never rebuild. The prefix therefore treats closing work as due
    only once the phase's own target is met. (The press here is a Block press that
    refuses at the sim: the game cannot sim.)"""
    league_id, team_ids, _players = await _league_of_players(pool)
    try:
        await _walk_to(pool, league_id, Phase.REGULAR_SEASON_COMPLETE)
        await _one_scheduled_game(pool, league_id, team_ids)

        with pytest.raises(season_service.GamesFailedToSimError):
            await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert rebuild_calls == []
        assert (await _checkpoints(pool, league_id))[2] == -1
    finally:
        await _drop(pool, league_id)


async def test_a_press_at_a_phase_with_no_work_reads_no_pointer_and_advances_no_checkpoint(pool):
    league_id = await _league_with_thirty_teams(pool)
    try:
        await _walk_to(pool, league_id, Phase.PLAYIN)
        before = await _checkpoints(pool, league_id)

        report = await press(pool, league_id=league_id, actor_user_id=_COMMISSIONER)

        assert report.to_phase is Phase.PLAYOFFS_R1
        assert report.checkpoint_result is None
        assert await _checkpoints(pool, league_id) == before
    finally:
        await _drop(pool, league_id)


# --- the two new writers ------------------------------------------------------


async def test_the_two_completion_checkpoints_only_ever_move_forward(pool):
    league_id = await _league_with_thirty_teams(pool)
    try:
        for setter, index in (
            (league_repo.set_entry_work_completed_index, 1),
            (league_repo.set_closing_work_completed_index, 2),
        ):
            assert await setter(pool, league_id, 5) is True
            assert await setter(pool, league_id, 5) is False, "equal is a no-op"
            assert await setter(pool, league_id, index) is False, "never backwards"
        league = await league_repo.get_league(pool, league_id)
        assert (league.entry_work_completed_index, league.closing_work_completed_index) == (5, 5)
        assert league.hook_completed_cycle_index == -1, "a different column, untouched"
    finally:
        await _drop(pool, league_id)
