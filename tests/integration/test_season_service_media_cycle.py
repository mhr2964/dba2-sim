"""The media cycle as closing work (phase-5-stage-e-advance.md P5-99.s, P5-99.s.1,
P5-99.s.2, P5-99.s.5, P5-99.b.3, build-plan commit 11), over real rows.

The nine cycle-bearing phases carry a media cycle as CLOSING work, keyed by the
same `closing_work_completed_index` the stats rebuild uses, so "selection finished"
is one durable fact per phase entry. The two media calls open no transaction of
their own and the snapshot lands before any story (F10), so the crash states are
kept distinct and never conflated into one "did it run?":

- checkpoint set                      -> skip entirely (the next press of any kind);
- checkpoint unset, no story          -> run both from the start: the snapshot upsert
                                         is keyed per team, so the re-run is identical;
- checkpoint unset, stories exist     -> log, SKIP the remainder, set the checkpoint
                                         (`create_media_story` is a bare insert, so a
                                         re-run would duplicate, not resume).

What the SERVICE never does is publish: `season_service` imports nothing from
`dba2.bot`, so a press leaves every story it wrote with `posted_at IS NULL` and the
cog posts them (`test_wire_publication.py`).

`gm_service.run_phase_hook` is stubbed; the two media service calls are recording
delegators to the REAL functions (so the pulse snapshot and the stories are real
rows), except where a case injects the crash it is about.

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import logging

import pytest

from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, media_repo
from dba2.domain.media import StoryKind
from dba2.domain.phase import Phase
from dba2.phase import service as phase_service
from dba2.services import gm_service, media_service, season_service
from dba2.services.season_service import PressKind
from tests.integration._press_fixtures import (
    COMMISSIONER,
    SEASON,
    add_draft_board,
    add_two_teams,
    bare_league_at,
    drop,
    sim_league_at,
)

LIBRARY = media_service.load_media_library()


@pytest.fixture
async def pool():
    pool = await get_pool()
    try:
        yield pool
    finally:
        await close_pool()


@pytest.fixture(autouse=True)
def _stub_the_hook(monkeypatch):
    async def hook(ex, *, league_id, phase, actor_user_id, agency_enabled=True):
        return gm_service.PhaseHookResult(league_id=league_id, season=SEASON, phase=phase)

    monkeypatch.setattr(gm_service, "run_phase_hook", hook)


@pytest.fixture(autouse=True)
def _no_entry_work(monkeypatch):
    """`TRADE_DEADLINE`/`PROGRESSION`/`PRESEASON` entry work is a real agency
    checkpoint or a real schedule; these cases are about the closing work."""
    monkeypatch.setitem(season_service._WORK_UNITS, season_service.WorkKind.ENTRY, {})


class _MediaCalls:
    """Recording delegators to the real media service calls, with an optional
    one-shot crash injected into the selection call (after the real pulse ran)."""

    def __init__(self) -> None:
        self.pulses: list[dict] = []
        self.selections: list[dict] = []
        self.crash_selection_once = False


@pytest.fixture
def media_calls(monkeypatch):
    """Recording delegators for the pulse/selection calls this test file's own
    cases are actually about (press/checkpoint/crash-recovery mechanics) --
    `load_team_profile_packages`/`load_subject_last_seen` (docs/design/
    media-v2-newsroom.md "Build order" step 6) are stubbed to their pre-step-6
    values (`()`/`None`) rather than delegated to the real functions: both need
    a real, fully-seeded league (`league_cap_settings`, real rosters) this
    file's own minimal `bare_league_at`/`add_two_teams` fixtures never build,
    and neither loader's own real behavior is what any case in this file
    tests -- that coverage lives in `tests/integration/test_media_service.py`'s
    own `load_team_profile_packages` tests instead."""
    calls = _MediaCalls()
    real_pulse = media_service.run_media_pulse_cycle
    real_selection = media_service.run_media_selection_cycle

    async def pulse(ex, **kwargs):
        calls.pulses.append(dict(kwargs))
        result = await real_pulse(ex, **kwargs)
        calls.pulses[-1]["result"] = result
        return result

    async def selection(ex, **kwargs):
        calls.selections.append(dict(kwargs))
        if calls.crash_selection_once:
            calls.crash_selection_once = False
            raise RuntimeError("the process died between the snapshot and the stories")
        return await real_selection(ex, **kwargs)

    async def no_profile_candidates(ex, **kwargs):  # noqa: ARG001 -- signature-compatible stub
        return ()

    async def no_subject_last_seen(ex, **kwargs):  # noqa: ARG001 -- signature-compatible stub
        return None

    monkeypatch.setattr(media_service, "run_media_pulse_cycle", pulse)
    monkeypatch.setattr(media_service, "run_media_selection_cycle", selection)
    monkeypatch.setattr(media_service, "load_team_profile_packages", no_profile_candidates)
    monkeypatch.setattr(media_service, "load_subject_last_seen", no_subject_last_seen)
    return calls


async def _press(pool, league_id: int):
    return await season_service.press(
        pool, league_id=league_id, actor_user_id=COMMISSIONER, library=LIBRARY
    )


async def _checkpoints(pool, league_id: int) -> tuple[int, int]:
    league = await league_repo.get_league(pool, league_id)
    return league.hook_completed_cycle_index, league.closing_work_completed_index


async def _story(pool, league_id: int, *, cycle_index: int, phase: Phase):
    return await media_repo.create_media_story(
        pool,
        league_id=league_id,
        season=SEASON,
        phase=phase,
        cycle_index=cycle_index,
        story_kind=StoryKind.STREAK,
        voice="beat_writer",
        template_id="streak.test",
        slot_bindings={},
        rendered_text="a story a crash left behind",
        salience=0.5,
        subject_player_id=None,
        subject_team_id=None,
        writer_id=None,
    )


# --- the closing press ------------------------------------------------------------


async def test_the_closing_press_runs_both_media_calls_in_order_and_sets_the_checkpoint(
    pool, media_calls
):
    """`AWARDS -> DRAFT_LOTTERY`: cycle-bearing (ordinal 4), a target that is already
    met on arrival (P5-99.b.1 item 3 -- decided against the ENTERED phase), so the
    entering press is its own closing press. Pulse first, then selection with the
    pulse's own `candidates`, the phase it is inside, the live library, and fresh
    use counts; then the checkpoint, keyed by the entry's index."""
    league_id = await bare_league_at(pool, Phase.AWARDS)
    try:
        league = await league_repo.get_league(pool, league_id)

        report = await _press(pool, league_id)

        assert report.kind is PressKind.ENTRY and report.to_phase is Phase.DRAFT_LOTTERY
        assert (report.entry_index, report.cycle_ordinal) == (11, 4)
        assert report.media_cycle_index == 4
        (pulse,) = media_calls.pulses
        assert (pulse["league_id"], pulse["season"], pulse["cycle_index"]) == (league_id, SEASON, 4)
        (selection,) = media_calls.selections
        assert selection["cycle_index"] == 4
        assert selection["phase"] is Phase.DRAFT_LOTTERY
        assert selection["season"] == SEASON
        assert selection["league_rng_seed"] == league.rng_seed
        assert selection["library"] is LIBRARY
        assert selection["use_counts"] == {}
        assert selection["candidates"] is pulse["result"].candidates
        assert selection["profile_candidates"] == ()
        assert selection["subject_last_seen"] is None
        assert await _checkpoints(pool, league_id) == (4, 11)
    finally:
        await drop(pool, league_id)


async def test_a_repeat_press_at_the_same_entry_skips_the_media_cycle(pool, media_calls):
    """`FREE_AGENCY -> WAIVERS`: the entering press runs cycle 7's owed media (the
    league was standing in `FREE_AGENCY` with its closing work undone -- the prefix)
    and cycle 8's. `WAIVERS` is a `Blocked` press by design; pressing it again
    re-runs nothing."""
    league_id = await bare_league_at(pool, Phase.FREE_AGENCY)
    try:
        first = await _press(pool, league_id)
        assert first.to_phase is Phase.WAIVERS
        assert [p["cycle_index"] for p in media_calls.pulses] == [7, 8]
        assert first.media_cycle_index == 8

        for _ in range(2):
            again = await _press(pool, league_id)
            assert again.kind is PressKind.BLOCKED
            assert again.media_cycle_index is None
        assert [p["cycle_index"] for p in media_calls.pulses] == [7, 8]
        assert [s["cycle_index"] for s in media_calls.selections] == [7, 8]
    finally:
        await drop(pool, league_id)


async def test_a_blocked_press_finishes_a_media_cycle_that_never_ran(pool, media_calls):
    """P5-99.b.2 row 1: the prefix runs on a `Blocked` press too, so a crashed
    cycle-8 media cycle still finishes at `WAIVERS`."""
    league_id = await bare_league_at(pool, Phase.WAIVERS)
    try:
        assert (await _checkpoints(pool, league_id))[1] == -1

        report = await _press(pool, league_id)

        assert report.kind is PressKind.BLOCKED
        assert report.media_cycle_index == 8
        assert [p["cycle_index"] for p in media_calls.pulses] == [8]
        assert (await _checkpoints(pool, league_id))[1] == 18
    finally:
        await drop(pool, league_id)


# --- re-entrancy: the crash states, kept distinct -----------------------------------


async def test_checkpoint_unset_with_stories_skips_the_remainder_and_sets_the_checkpoint(
    pool, media_calls, caplog
):
    """A crash mid-selection left stories behind and the checkpoint unset. A re-run
    would DUPLICATE them (`create_media_story` is an insert), and media is
    report-only, so the press logs, skips what remains and sets the checkpoint."""
    league_id = await bare_league_at(pool, Phase.AWARDS)
    try:
        await phase_service.advance(pool, league_id, Phase.DRAFT_LOTTERY, COMMISSIONER)
        left_behind = await _story(pool, league_id, cycle_index=4, phase=Phase.DRAFT_LOTTERY)
        # The DRAFT_LOTTERY -> DRAFT edge needs a board (P5-99.h's pre-entry check).
        await add_draft_board(pool, league_id, picks=((1, 1, "TOR"),), used=(0,))

        with caplog.at_level(logging.WARNING, logger=season_service.log.name):
            report = await _press(pool, league_id)

        assert [p["cycle_index"] for p in media_calls.pulses] == [5], (
            "cycle 4 was skipped; only the DRAFT entry this press then made ran its own"
        )
        assert (await _checkpoints(pool, league_id))[1] == 12, "DRAFT's own entry, after 11's"
        assert any("cycle 4" in r.getMessage() for r in caplog.records), "the skip is logged"
        stories = await media_repo.list_media_stories_for_league_season(
            pool, league_id=league_id, season=SEASON
        )
        assert [s.id for s in stories if s.cycle_index == 4] == [left_behind.id]
        assert report.to_phase is Phase.DRAFT
    finally:
        await drop(pool, league_id)


async def test_checkpoint_unset_and_no_stories_reruns_both_calls_and_the_snapshot_is_identical(
    pool, media_calls
):
    """A crash between the pulse snapshot and the first story: the snapshot is
    durable, no story exists, the checkpoint is unset. The next press re-runs BOTH
    calls from the start -- and the snapshot upsert is keyed per team, so what it
    writes is what is already there. **Using the snapshot as the marker instead
    permanently skips a cycle whose stories were never written** (F10)."""
    league_id = await bare_league_at(pool, Phase.AWARDS)
    try:
        home, _away = await add_two_teams(pool, league_id)
        media_calls.crash_selection_once = True
        with pytest.raises(RuntimeError, match="between the snapshot and the stories"):
            await _press(pool, league_id)
        assert (await _checkpoints(pool, league_id))[1] == -1
        snapshot = await media_repo.get_pulse_snapshot_teams(
            pool, league_id=league_id, season=SEASON, cycle_index=4
        )
        assert len(snapshot) == 2, "the pulse snapshot landed before the crash"
        assert (
            await media_repo.list_media_stories_for_league_season(
                pool, league_id=league_id, season=SEASON
            )
            == []
        )

        # The DRAFT_LOTTERY -> DRAFT edge needs a board (P5-99.h's pre-entry check), on a
        # team that already exists so the cycle-4 snapshot compared below keeps its rows.
        await add_draft_board(
            pool, league_id, picks=((1, 1, "PRH"),), used=(0,), teams={"PRH": home}
        )

        await _press(pool, league_id)  # DRAFT_LOTTERY -> DRAFT; its prefix finishes cycle 4

        assert [p["cycle_index"] for p in media_calls.pulses] == [4, 4, 5]
        assert [s["cycle_index"] for s in media_calls.selections] == [4, 4, 5]
        again = await media_repo.get_pulse_snapshot_teams(
            pool, league_id=league_id, season=SEASON, cycle_index=4
        )
        assert sorted(again, key=lambda r: r.team_id) == sorted(snapshot, key=lambda r: r.team_id)
        assert (await _checkpoints(pool, league_id))[1] == 12
    finally:
        await drop(pool, league_id)


# --- the whole thing over a real league ----------------------------------------------


async def test_the_block_press_that_reaches_the_target_runs_cycle_1_and_no_earlier_one_does(
    pool,
):
    """15 games: `REGULAR_SEASON`'s target is index 6, reached on the fifth press. The
    four before it report no media cycle; the fifth runs cycle 1 over the real league --
    a snapshot row for every team -- and leaves every story it wrote unposted (the
    service never publishes)."""
    league_id = await sim_league_at(pool, Phase.REGULAR_SEASON, keep_games=15)
    try:
        entry_index = (await league_repo.count_phase_entries(pool, league_id, SEASON)) - 1
        reports = [await _press(pool, league_id) for _ in range(5)]

        assert [r.kind for r in reports] == [PressKind.BLOCK] * 5
        assert [r.media_cycle_index for r in reports] == [None, None, None, None, 1]
        assert (await _checkpoints(pool, league_id))[1] == entry_index
        snapshot = await media_repo.get_pulse_snapshot_teams(
            pool, league_id=league_id, season=SEASON, cycle_index=1
        )
        assert len(snapshot) == 30
        stories = await media_repo.list_media_stories_for_league_season(
            pool, league_id=league_id, season=SEASON
        )
        assert {s.cycle_index for s in stories} <= {1}
        assert all(s.posted_at is None for s in stories)
        unposted = await media_repo.list_unposted_media_stories(
            pool, league_id=league_id, season=SEASON
        )
        assert [s.id for s in unposted] == [s.id for s in stories]
    finally:
        await drop(pool, league_id)


async def test_the_press_takes_the_media_library_as_a_required_keyword(pool):
    league_id = await bare_league_at(pool, Phase.AWARDS)
    try:
        with pytest.raises(TypeError, match="library"):
            await season_service.press(pool, league_id=league_id, actor_user_id=COMMISSIONER)
    finally:
        await drop(pool, league_id)
