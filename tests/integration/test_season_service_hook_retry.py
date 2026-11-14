"""The work prefix retries the CURRENT entry's hook after a crash (phase-5-stage-e-
advance.md P5-99.l "after tx1, before the hook", P5-99.b.1, build-plan commit 10),
over real `phase_history` rows.

Until this commit the prefix finished a crashed entry's entry work and closing
work but not its hook, so a press that died between moving the pointer and
finishing the hook left the cycle without its hook for good: the checkpoint read
"not done" and nothing ever asked. The retry is one more step of the prefix,
ordered between the entry work and the closing work, and it obeys the two rules
that keep the hook checkpoint honest -- it is keyed by `cycle_ordinal`, and it
exists only for the nine cycle-bearing phases (F15).

`gm_service.run_phase_hook` is a recording spy whose next call can be told to
raise: the real hook's behaviour is proved in `test_gm_service.py`; what is proved
here is WHEN the press calls it, from WHICH press kind, and how many times a
crashed cycle ends up running it.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo
from dba2.domain.phase import Phase
from dba2.services import gm_service, season_service
from dba2.services.season_service import PressKind
from tests.integration._press_fixtures import (
    COMMISSIONER,
    SEASON,
    add_game,
    add_two_teams,
    drop,
    press,
    walk_to,
)


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


@dataclass
class _Attempt:
    phase: Phase
    db_phase: Phase
    raised: bool


class _Hook:
    def __init__(self) -> None:
        self.attempts: list[_Attempt] = []
        self.raise_next = False

    def completed(self, phase: Phase) -> int:
        return sum(1 for a in self.attempts if a.phase is phase and not a.raised)


@pytest.fixture
def hook(monkeypatch):
    spy = _Hook()

    async def run(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        league = await league_repo.get_league(ex, league_id)
        raised = spy.raise_next
        spy.attempts.append(_Attempt(phase, league.current_phase, raised))
        if raised:
            spy.raise_next = False
            raise RuntimeError("the process died mid-hook")
        return gm_service.PhaseHookResult(league_id=league_id, season=SEASON, phase=phase)

    monkeypatch.setattr(gm_service, "run_phase_hook", run)
    return spy


@pytest.fixture(autouse=True)
def no_phase_work(monkeypatch):
    """The subject is the hook, and the phases below with entry work run a real
    agency checkpoint that needs a bootstrapped league."""
    monkeypatch.setitem(season_service._WORK_UNITS, season_service.WorkKind.ENTRY, {})
    monkeypatch.setitem(season_service._WORK_UNITS, season_service.WorkKind.CLOSING, {})


async def _league_at(pool, phase: Phase) -> int:
    """Real history to `phase` by direct advances, and NO checkpoint marked: the
    hook of the entry the league is standing in has not run."""
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Hook Retry Test League",
        start_season_year=SEASON,
        current_season=SEASON,
        commissioner_user_id=COMMISSIONER,
        rng_seed=1,
    )
    await walk_to(pool, league.id, phase)
    return league.id


async def _press(pool, league_id: int):
    return await press(pool, league_id=league_id, actor_user_id=COMMISSIONER)


async def test_a_hook_that_died_mid_press_is_completed_exactly_once_by_the_next_press(pool, hook):
    """P5-99.l's crash table: the pointer moved into `RESIGN_WINDOW`, the hook raised,
    the claim was released and `hook_completed_cycle_index` stayed at its old value.
    The next press finishes THAT entry's hook first -- with the pointer still at
    `RESIGN_WINDOW` -- and only then does its own advance. Without the retry the
    cycle's `run_resign_window` never runs."""
    league_id = await _league_at(pool, Phase.OPTION_DECISIONS)
    try:
        hook.raise_next = True
        with pytest.raises(RuntimeError, match="died mid-hook"):
            await _press(pool, league_id)
        league = await league_repo.get_league(pool, league_id)
        assert league.current_phase is Phase.RESIGN_WINDOW
        assert league.hook_completed_cycle_index == -1
        assert league.advance_claimed_at is None
        assert hook.completed(Phase.RESIGN_WINDOW) == 0

        report = await _press(pool, league_id)

        assert report.kind is PressKind.ENTRY
        assert (report.from_phase, report.to_phase) == (Phase.RESIGN_WINDOW, Phase.FREE_AGENCY)
        assert report.resumed_hook_result is not None
        assert report.resumed_hook_result.phase is Phase.RESIGN_WINDOW
        assert report.hook_result is not None and report.hook_result.phase is Phase.FREE_AGENCY
        assert [(a.phase, a.db_phase, a.raised) for a in hook.attempts] == [
            (Phase.RESIGN_WINDOW, Phase.RESIGN_WINDOW, True),
            (Phase.RESIGN_WINDOW, Phase.RESIGN_WINDOW, False),
            (Phase.FREE_AGENCY, Phase.FREE_AGENCY, False),
        ], "the retry ran BEFORE the pointer moved, against the entry it belongs to"
        assert hook.completed(Phase.RESIGN_WINDOW) == 1
        league = await league_repo.get_league(pool, league_id)
        assert league.hook_completed_cycle_index == 7

        third = await _press(pool, league_id)

        assert third.resumed_hook_result is None
        assert hook.completed(Phase.RESIGN_WINDOW) == 1, "exactly once, not once per press"
        assert hook.completed(Phase.FREE_AGENCY) == 1
        assert hook.completed(Phase.WAIVERS) == 1
    finally:
        await drop(pool, league_id)


async def test_a_blocked_press_completes_a_crashed_hook_too(pool, hook):
    """`WAIVERS` is a `Blocked` press by design (the Phase 7 message), and the prefix
    runs on every kind of press: a crash right after entering `WAIVERS` is finished by
    the press that then refuses to leave it."""
    league_id = await _league_at(pool, Phase.WAIVERS)
    try:
        report = await _press(pool, league_id)

        assert report.kind is PressKind.BLOCKED
        assert report.resumed_hook_result is not None
        assert report.resumed_hook_result.phase is Phase.WAIVERS
        assert hook.completed(Phase.WAIVERS) == 1
        assert (await league_repo.get_league(pool, league_id)).hook_completed_cycle_index == 8

        again = await _press(pool, league_id)

        assert again.kind is PressKind.BLOCKED
        assert again.resumed_hook_result is None
        assert hook.completed(Phase.WAIVERS) == 1
    finally:
        await drop(pool, league_id)


async def test_a_block_press_completes_a_crashed_hook_before_it_sims(pool, hook):
    """A press that refuses at the sim has still finished the prefix: the crashed
    entry's hook ran and was checkpointed, and the next press does not run it again."""
    league_id = await _league_at(pool, Phase.POST_DEADLINE)
    try:
        teams = await add_two_teams(pool, league_id)
        await add_game(pool, league_id, teams, game_index=0, game_date=date(2026, 11, 1))

        with pytest.raises(season_service.GamesFailedToSimError):
            await _press(pool, league_id)
        assert hook.completed(Phase.POST_DEADLINE) == 1
        assert (await league_repo.get_league(pool, league_id)).hook_completed_cycle_index == 3

        with pytest.raises(season_service.GamesFailedToSimError):
            await _press(pool, league_id)
        assert hook.completed(Phase.POST_DEADLINE) == 1
    finally:
        await drop(pool, league_id)


async def test_a_press_at_a_non_cycle_bearing_phase_never_runs_or_checkpoints_a_hook(pool, hook):
    """F15: the hook checkpoint is written only from the nine cycle-bearing phases.
    The prefix must not invent a hook for `PLAYIN` from a stale `-1`."""
    league_id = await _league_at(pool, Phase.PLAYIN)
    try:
        report = await _press(pool, league_id)

        assert report.to_phase is Phase.PLAYOFFS_R1
        assert report.resumed_hook_result is None
        assert hook.attempts == []
        assert (await league_repo.get_league(pool, league_id)).hook_completed_cycle_index == -1
    finally:
        await drop(pool, league_id)


async def test_a_crash_between_the_hook_and_its_checkpoint_reruns_the_hook_R2(
    pool, hook, monkeypatch
):
    """Pins the residue the design DISCLOSES (R2), so it cannot drift into being either
    hidden or worse: the hook finished, the checkpoint write did not, and the next press
    runs that hook again. At-least-once for this one window, exactly-once everywhere
    else."""
    league_id = await _league_at(pool, Phase.OPTION_DECISIONS)
    real_record = season_service._record_hook_completed
    failures = {"left": 1}

    async def dying_record(pool_, league_id_, cycle_ordinal):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("the process died before the checkpoint")
        return await real_record(pool_, league_id_, cycle_ordinal)

    monkeypatch.setattr(season_service, "_record_hook_completed", dying_record)
    try:
        with pytest.raises(RuntimeError, match="before the checkpoint"):
            await _press(pool, league_id)
        assert hook.completed(Phase.RESIGN_WINDOW) == 1
        assert (await league_repo.get_league(pool, league_id)).hook_completed_cycle_index == -1

        await _press(pool, league_id)

        assert hook.completed(Phase.RESIGN_WINDOW) == 2
        assert (await league_repo.get_league(pool, league_id)).hook_completed_cycle_index == 7
    finally:
        await drop(pool, league_id)
