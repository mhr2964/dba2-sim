"""JC-13: the first AI trade window is delayed to a block press past a games fraction
(phase-5-stage-e-advance.md P5-99.k, P5-99.d.1 M1, build-plan commit 12), over real rows.

The `REGULAR_SEASON` Entry press runs NO hook. The first Block press whose post-sim
schedule has played every game up to the threshold runs `REGULAR_SEASON`'s hook and
writes `hook_completed_cycle_index = cycle_ordinal` -- an ordinal counted on the
Block path, under the row lock, not one carried from the entry press. Later block
presses do not run it again. What a crash between the sim and the hook does to that
promise is the last case: the next press finishes the hook first, once.

`gm_service.run_phase_hook` is a recording spy: what is proved is WHEN the press
calls the hook, against WHAT state, and what it writes around it. The hook's own
trade-window behaviour is `test_gm_service.py`'s.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import game_repo, league_repo
from dba2.domain.game import GameStatus
from dba2.domain.phase import Phase
from dba2.services import gm_service, season_service
from dba2.services.season_service import PressKind
from tests.integration._press_fixtures import (
    COMMISSIONER,
    SEASON,
    drop,
    press,
    sim_league_at,
)


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


@dataclass
class _Call:
    phase: Phase
    db_phase: Phase
    hook_checkpoint: int
    closing_checkpoint: int
    games_final: int


class _Hook:
    def __init__(self) -> None:
        self.calls: list[_Call] = []
        self.raise_next = False


@pytest.fixture
def hook(monkeypatch):
    spy = _Hook()

    async def run(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        league = await league_repo.get_league(ex, league_id)
        games = await game_repo.list_games_for_league_season(ex, league_id, SEASON)
        spy.calls.append(
            _Call(
                phase=phase,
                db_phase=league.current_phase,
                hook_checkpoint=league.hook_completed_cycle_index,
                closing_checkpoint=league.closing_work_completed_index,
                games_final=sum(1 for g in games if g.status is GameStatus.FINAL),
            )
        )
        if spy.raise_next:
            spy.raise_next = False
            raise RuntimeError("the process died mid-hook")
        return gm_service.PhaseHookResult(league_id=league_id, season=SEASON, phase=phase)

    monkeypatch.setattr(gm_service, "run_phase_hook", run)
    return spy


@pytest.fixture
def no_media(monkeypatch):
    """The subject is the hook; the closing media cycle over a real league is
    `test_season_service_media_cycle.py`'s."""
    monkeypatch.setitem(season_service._WORK_UNITS, season_service.WorkKind.CLOSING, {})


async def _a_league_about_to_enter_regular_season(pool, *, keep_games: int) -> int:
    """A real, sim-able league at `PRESEASON` whose cycle-0 hook and entry work are
    done -- so the next press is the `REGULAR_SEASON` Entry press."""
    return await sim_league_at(pool, Phase.PRESEASON, keep_games=keep_games)


async def _hook_checkpoint(pool, league_id: int) -> int:
    return (await league_repo.get_league(pool, league_id)).hook_completed_cycle_index


async def test_entry_press_runs_no_hook_and_the_first_block_past_the_fraction_does(
    pool, hook, no_media
):
    """15 games: the threshold index is 2 (round(14 * 0.25 * 0.45)); the generated
    schedule's first dates carry 1, 1, 2, 2 games, so game index 2 is played by the THIRD
    block press. The two before it run nothing; the third runs the hook once, after
    its sim, and writes the checkpoint as the ordinal of the cycle it is inside: 1
    (`PRESEASON` was cycle 0). The fourth runs nothing."""
    league_id = await _a_league_about_to_enter_regular_season(pool, keep_games=15)
    try:
        assert await _hook_checkpoint(pool, league_id) == 0, "cycle 0's hook is done"

        entry = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        assert (entry.kind, entry.to_phase) == (PressKind.ENTRY, Phase.REGULAR_SEASON)
        assert entry.cycle_ordinal == 1
        assert entry.hook_result is None
        assert hook.calls == []
        assert await _hook_checkpoint(pool, league_id) == 0

        first = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        second = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        for report in (first, second):
            assert report.kind is PressKind.BLOCK
            assert report.hook_result is None
        assert hook.calls == []
        assert await _hook_checkpoint(pool, league_id) == 0

        third = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert third.kind is PressKind.BLOCK
        assert third.cycle_ordinal == 1
        assert third.hook_result is not None
        assert third.resumed_hook_result is None, "this press's OWN hook, not a prefix retry"
        (call,) = hook.calls
        assert call.phase is Phase.REGULAR_SEASON
        assert call.db_phase is Phase.REGULAR_SEASON
        assert call.games_final == 4, "the hook ran AFTER the block's sim (games 0-3 final)"
        assert call.hook_checkpoint == 0, "the checkpoint is written after the hook"
        assert await _hook_checkpoint(pool, league_id) == 1, (
            "written FROM THE BLOCK PATH as the cycle's ordinal (M1)"
        )

        fourth = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert fourth.kind is PressKind.BLOCK
        assert fourth.hook_result is None and fourth.resumed_hook_result is None
        assert len(hook.calls) == 1, "later block presses do not run it again"
        assert await _hook_checkpoint(pool, league_id) == 1
    finally:
        await drop(pool, league_id)


async def test_a_block_past_fraction_and_target_runs_the_hook_before_closing_work(
    pool, hook, monkeypatch
):
    """Two games: the threshold index and the target index are both 0, so the first
    block press crosses both. Hook, then closing work -- the order `_finish_entry`
    gives an Entry press -- so cycle 1's media reads the trades the window made."""
    ran_media_after_hook: list[bool] = []

    async def fake_media(ctx):
        ran_media_after_hook.append(bool(hook.calls))

    monkeypatch.setitem(
        season_service._WORK_UNITS,
        season_service.WorkKind.CLOSING,
        {Phase.REGULAR_SEASON: (fake_media,)},
    )
    league_id = await _a_league_about_to_enter_regular_season(pool, keep_games=2)
    try:
        await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        block = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert block.kind is PressKind.BLOCK
        assert block.hook_result is not None
        assert block.media_cycle_index == 1
        assert ran_media_after_hook == [True]
        (call,) = hook.calls
        assert (
            call.closing_checkpoint
            < (await league_repo.count_phase_entries(pool, league_id, SEASON)) - 1
        ), "the hook ran before the closing checkpoint was written"
        assert await _hook_checkpoint(pool, league_id) == 1
    finally:
        await drop(pool, league_id)


async def test_a_crash_between_the_sim_and_the_hook_is_finished_once_by_the_next_press(
    pool, hook, no_media
):
    """The retry (`_finish_entry`, the work prefix) must know the hook's due-ness is
    now "the threshold has been played", not "the entry exists". The third press
    sims the threshold's games and its hook dies: the games are committed and the
    checkpoint is not. The fourth press's PREFIX finishes that hook -- exactly once,
    before its own sim, against the same entry -- and the fifth runs nothing."""
    league_id = await _a_league_about_to_enter_regular_season(pool, keep_games=15)
    try:
        for _ in range(3):  # the entry press and the first two blocks: nothing due yet
            await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        assert hook.calls == []

        hook.raise_next = True
        with pytest.raises(RuntimeError, match="died mid-hook"):
            await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
        league = await league_repo.get_league(pool, league_id)
        assert league.hook_completed_cycle_index == 0, "a hook that did not finish is not recorded"
        assert league.advance_claimed_at is None
        assert len(hook.calls) == 1

        fourth = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert fourth.kind is PressKind.BLOCK
        assert fourth.resumed_hook_result is not None
        assert fourth.resumed_hook_result.phase is Phase.REGULAR_SEASON
        assert fourth.hook_result is None, "the retry happened in the prefix, not twice"
        assert len(hook.calls) == 2, "one attempt that died, one retry"
        retry = hook.calls[1]
        assert retry.db_phase is Phase.REGULAR_SEASON
        assert retry.games_final == 4, "the retry ran before this press's own sim"
        assert await _hook_checkpoint(pool, league_id) == 1

        fifth = await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)

        assert fifth.resumed_hook_result is None and fifth.hook_result is None
        assert len(hook.calls) == 2
    finally:
        await drop(pool, league_id)
