"""E-24 / E-25: one zero-human season, `PRESEASON` to `WAIVERS`, under `/advance` alone
(phase-5-stage-e-advance.md P5-99.q items 1 and 2, build-plan row 18) -- driven through
`season_run.run_to`, the loop `/advance-run` promotes out of this file's old hand loop
(docs/design/advance-run.md §3, acceptance 1). `run_to` presses `season_service.press` and
nothing else; see the walk section below for the shape its target rule forces.

**SLOW, and deselected by the default run** (`-m 'not slow'` in `pyproject.toml`, the same
marker `test_sim_validation.py` and the gm-service sweep use): it seeds a real 30-team league
and plays all 1,230 games. Run it with

    python -m pytest tests/integration/test_full_season_walk.py -m slow -p no:cacheprovider -s

**What "alone" means.** After setup the test calls `season_run.run_to` (a loop of
`season_service.press`, the function `/advance` calls) and one hand press -- and nothing else
moves the league. Setup is `seed_dev_league` plus, once the
walk reaches `DRAFT_LOTTERY`, `tools.dev_league_draft`'s `ensure_draft_class` / `ensure_draft`:
nothing under `dba2/` can create a draft (F1), and the `DRAFT_LOTTERY -> DRAFT` edge refuses
without a board, so that step is the one named exception (P5-99.q item 1). The test PROVES the
exception is needed: a press at `DRAFT_LOTTERY` before the board exists is refused and moves
nothing. Publication is the cog's job, so the test does what the cog does after each press
that finished a media cycle -- `surfaces.post_new_wire_stories` for that cycle into a fake wire
channel -- and nothing more.

**Every number asserted is counted, not assumed.** The counts come from spies that call
through (so the real work runs) on the seams the spec names, and from the database afterwards;
where the spec's estimate was wrong the assertion carries the measured value and the spec was
corrected (the block-press count; `run_resign_window` and `classify_all_windows` are each also
called by a hook the spec did not name, so they are counted PER HOOK).

Requires `docker compose up -d` and a `.env`/DATABASE_URL pointed at it.
"""

from __future__ import annotations

import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import pytest

from dba2.bot import surfaces
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import (
    channel_repo,
    draft_repo,
    game_repo,
    league_repo,
    media_repo,
)
from dba2.domain.channel import ChannelPurpose
from dba2.domain.game import GameStatus, GameType
from dba2.domain.phase import Phase
from dba2.media.pulse import PulseSnapshot, TeamPulseState
from dba2.services import (
    agency_service,
    gm_service,
    media_service,
    season_run,
    season_service,
    stats_service,
)
from tests.integration._press_fixtures import COMMISSIONER, LIBRARY, SEASON, press
from tools import seed_dev_league
from tools.dev_league_draft import ensure_draft, ensure_draft_class

_WIRE_CHANNEL_ID = 515151
_DRAFT_ROUNDS = 2
_DRAFT_CLASS_SIZE = 75  # more prospects than the 60 picks, so the AI can always draft
_MAX_PRESSES = 400  # the season is ~190; a runaway loop fails loudly instead of hanging

# The nineteen phases a season enters, in walk order -- written out from the parent spec's
# graph, not derived from `season_service`, so the walk is compared against something the
# code under test did not produce.
_EXPECTED_ENTRIES = [
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
    Phase.OPTION_DECISIONS,
    Phase.RESIGN_WINDOW,
    Phase.FREE_AGENCY,
    Phase.WAIVERS,
]
_EXPECTED_CYCLE_PHASES = [
    Phase.PRESEASON,
    Phase.REGULAR_SEASON,
    Phase.TRADE_DEADLINE,
    Phase.POST_DEADLINE,
    Phase.DRAFT_LOTTERY,
    Phase.DRAFT,
    Phase.RESIGN_WINDOW,
    Phase.FREE_AGENCY,
    Phase.WAIVERS,
]


class _WireChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, *, embed) -> None:
        self.sent.append(embed.description or embed.title)


class _Guild:
    id = 9

    def __init__(self, channel: _WireChannel) -> None:
        self._channel = channel

    def get_channel(self, channel_id: int):
        return self._channel if channel_id == _WIRE_CHANNEL_ID else None


@dataclass
class _Spies:
    """What the walk observed, by wrapping the real functions (each calls through)."""

    entry_work_advances: list[int] = field(default_factory=list)
    closing_work_advances: list[int] = field(default_factory=list)
    hooks: list[Phase] = field(default_factory=list)
    resign_window_in_hook: list[Phase] = field(default_factory=list)
    classify_windows_in_hook: list[Phase] = field(default_factory=list)
    trade_windows_in_hook: list[Phase] = field(default_factory=list)
    agency_checkpoints: list[Phase] = field(default_factory=list)
    rebuilds: list[tuple[int, GameType]] = field(default_factory=list)
    media_pulse_cycles: list[int] = field(default_factory=list)
    media_selection_cycles: list[int] = field(default_factory=list)
    current_hook_phase: Phase | None = None


def _install_spies(monkeypatch) -> _Spies:
    spies = _Spies()

    def wrap_recorder(module, name, kind_list):
        real = getattr(module, name)

        async def spy(pool, league_id, entry_index):
            advanced = await real(pool, league_id, entry_index)
            if advanced:
                kind_list.append(entry_index)
            return advanced

        monkeypatch.setattr(module, name, spy)

    wrap_recorder(season_service, "_record_entry_work_completed", spies.entry_work_advances)
    wrap_recorder(season_service, "_record_closing_work_completed", spies.closing_work_advances)

    real_hook = gm_service.run_phase_hook

    async def hook_spy(*args, **kwargs):
        phase = kwargs["phase"]
        spies.hooks.append(phase)
        spies.current_hook_phase = phase
        try:
            return await real_hook(*args, **kwargs)
        finally:
            spies.current_hook_phase = None

    monkeypatch.setattr(gm_service, "run_phase_hook", hook_spy)

    def sync_spy(name, bucket):
        real = getattr(gm_service, name)

        def spy(*args, **kwargs):
            bucket.append(spies.current_hook_phase)
            return real(*args, **kwargs)

        monkeypatch.setattr(gm_service, name, spy)

    sync_spy("run_resign_window", spies.resign_window_in_hook)
    sync_spy("classify_all_windows", spies.classify_windows_in_hook)
    sync_spy("run_trade_window", spies.trade_windows_in_hook)

    real_checkpoint = agency_service.run_agency_checkpoint

    async def checkpoint_spy(*args, **kwargs):
        spies.agency_checkpoints.append(kwargs["phase"])
        return await real_checkpoint(*args, **kwargs)

    monkeypatch.setattr(agency_service, "run_agency_checkpoint", checkpoint_spy)

    real_rebuild = stats_service.rebuild_player_season_stats

    async def rebuild_spy(ex, league_id, season, game_type):
        spies.rebuilds.append((season, game_type))
        return await real_rebuild(ex, league_id, season, game_type)

    monkeypatch.setattr(stats_service, "rebuild_player_season_stats", rebuild_spy)

    real_pulse, real_selection = (
        media_service.run_media_pulse_cycle,
        media_service.run_media_selection_cycle,
    )

    async def pulse_spy(*args, **kwargs):
        spies.media_pulse_cycles.append(kwargs["cycle_index"])
        return await real_pulse(*args, **kwargs)

    async def selection_spy(*args, **kwargs):
        spies.media_selection_cycles.append(kwargs["cycle_index"])
        return await real_selection(*args, **kwargs)

    monkeypatch.setattr(media_service, "run_media_pulse_cycle", pulse_spy)
    monkeypatch.setattr(media_service, "run_media_selection_cycle", selection_spy)
    return spies


def _expected_block_presses(games_by_date: list[tuple[object, list[int]]]) -> int:
    """How many game-day block presses a schedule needs, derived from the schedule and the
    four phase targets alone: a press sims the next unplayed date, capped at the phase's own
    games target, so a date a target boundary falls inside is played over TWO presses (one per
    phase). `games_by_date` is `[(date, [game_index, ...]), ...]`, oldest first."""
    max_index = max(i for _, indexes in games_by_date for i in indexes)
    targets = [
        season_service.games_target_index(season_service.GAMES_TARGET_FRACTION[phase], max_index)
        for phase in (
            Phase.REGULAR_SEASON,
            Phase.TRADE_DEADLINE,
            Phase.POST_DEADLINE,
            Phase.REGULAR_SEASON_COMPLETE,
        )
    ]
    presses = 0
    played_through = -1  # the highest game index already played
    for target in targets:
        for _, indexes in games_by_date:
            if max(indexes) <= played_through:
                continue
            if min(indexes) > target:
                break
            presses += 1
            played_through = min(max(indexes), target)
    return presses


async def _build_draft_board(pool, league_id: int, rng_seed: int) -> None:
    """The named exception (P5-99.q item 1): `tools/dev_league_draft.py`, run once when the
    walk stands at `DRAFT_LOTTERY`. Pick order comes from the persisted standings snapshot of
    the last media cycle -- the same source the corpus driver uses, read instead of recomputed."""
    rows = await media_repo.get_prior_pulse_snapshot_teams(
        pool, league_id=league_id, season=SEASON, before_cycle_index=10_000
    )
    assert rows, "no pulse snapshot exists to order the draft by"
    snapshot = PulseSnapshot(
        cycle_index=rows[0].cycle_index,
        teams={
            r.team_id: TeamPulseState(
                team_id=r.team_id,
                wins=r.wins,
                losses=r.losses,
                standings_position=r.standings_position,
                streak_length=r.streak_length,
                win_pct=r.wins / (r.wins + r.losses) if (r.wins + r.losses) else 0.0,
            )
            for r in rows
        },
    )
    await ensure_draft_class(
        pool, league_id=league_id, season=SEASON, class_size=_DRAFT_CLASS_SIZE, rng_seed=rng_seed
    )
    await ensure_draft(
        pool, league_id=league_id, season=SEASON, snapshot=snapshot, rounds=_DRAFT_ROUNDS
    )


@pytest.mark.slow
async def test_a_zero_human_league_walks_preseason_to_waivers_under_advance_alone(monkeypatch):
    started = time.perf_counter()
    pool = await get_pool()
    league_id = None
    try:
        spies = _install_spies(monkeypatch)
        rng_seed = 20_260_921
        league_id = await seed_dev_league.seed_dev_league(
            discord_guild_id=random.randint(10_000_000, 2_000_000_000),
            name="Full Season Walk League",
            start_season_year=SEASON,
            current_season=SEASON,
            commissioner_user_id=COMMISSIONER,
            rng_seed=rng_seed,
        )
        await channel_repo.create_league_channel(
            pool, league_id=league_id, purpose=ChannelPurpose.WIRE, channel_id=_WIRE_CHANNEL_ID
        )
        wire = _WireChannel()
        guild = _Guild(wire)
        seeded = time.perf_counter()

        # --- the walk: `run_to` twice and ONE hand press, from SETUP -----------------------
        # docs/design/advance-run.md §3 acceptance 1. AR-5's target rule forces this shape: the
        # run stops on the ENTRY into its target and never presses while standing in it, so
        # (1) `run_to(WAIVERS)` stops with `NEEDS_A_DRAFT_BOARD` at `DRAFT_LOTTERY` (the F1
        # exception, exactly as before), (2) the test builds the board and runs again, which
        # stops with `TARGET_REACHED` on the entry press into `WAIVERS`, and (3) one hand press
        # is the `WAIVERS_EDGE` refusal -- the second of the two `blocked` kinds. Every counter
        # below is accumulated from both runs' steps plus that press, by the one `record`.
        entries: list[Phase] = []
        kinds: Counter[str] = Counter()
        block_presses_by_phase: Counter[Phase] = Counter()
        checkpoint_at: dict[Phase, object] = {}
        media_published: list[int] = []
        press_seconds: defaultdict[Phase, float] = defaultdict(float)
        rebuild_press_phase: list[Phase] = []
        presses = 0
        rebuilds_seen = 0

        async def record(report, seconds: float) -> None:
            nonlocal presses, rebuilds_seen
            presses += 1
            press_seconds[report.from_phase] += seconds
            kinds[report.kind.value] += 1
            if len(spies.rebuilds) > rebuilds_seen:
                rebuild_press_phase.append(report.from_phase)
            rebuilds_seen = len(spies.rebuilds)

            if report.kind is season_service.PressKind.BLOCK:
                block_presses_by_phase[report.from_phase] += 1
            if report.kind is season_service.PressKind.ENTRY:
                assert report.to_phase is not None
                entries.append(report.to_phase)
                if report.checkpoint_result is not None and report.to_phase in (
                    Phase.TRADE_DEADLINE,
                    Phase.PROGRESSION,
                ):
                    checkpoint_at[report.to_phase] = report.checkpoint_result
            if report.media_cycle_index is not None:
                # What the cog does after a press that finished a media cycle.
                await surfaces.post_new_wire_stories(
                    guild,
                    pool,
                    league_id=league_id,
                    season=SEASON,
                    cycle_index=report.media_cycle_index,
                )
                media_published.append(report.media_cycle_index)

        async def on_step(step: season_run.RunStep) -> None:
            await record(step.report, step.seconds)

        async def run_to_waivers() -> season_run.RunReport:
            return await season_run.run_to(
                pool,
                league_id=league_id,
                actor_user_id=COMMISSIONER,
                to_phase=Phase.WAIVERS,
                library=LIBRARY,
                caps=season_run.RunCaps(max_presses=_MAX_PRESSES),  # a runaway fails, not hangs
                should_continue=lambda: True,
                on_step=on_step,
            )

        first_run = await run_to_waivers()
        # F1: nothing under dba2/ can create a draft, so the DRAFT_LOTTERY edge refuses.
        assert first_run.stop is season_run.RunStop.NEEDS_A_DRAFT_BOARD, first_run.stop
        board_refusal = first_run.last_report
        assert board_refusal is not None and board_refusal.from_phase is Phase.DRAFT_LOTTERY
        await _build_draft_board(pool, league_id, rng_seed)

        second_run = await run_to_waivers()
        assert second_run.stop is season_run.RunStop.TARGET_REACHED, second_run.stop
        entry_into_waivers = second_run.last_report
        assert entry_into_waivers is not None
        assert (entry_into_waivers.kind, entry_into_waivers.to_phase) == (
            season_service.PressKind.ENTRY,
            Phase.WAIVERS,
        )

        # The run never presses at its own target, so the WAIVERS edge is a hand press.
        t0 = time.perf_counter()
        refusal = await press(pool, league_id=league_id)
        await record(refusal, time.perf_counter() - t0)

        # --- what the league looks like now ------------------------------------------------
        league = await league_repo.get_league(pool, league_id)
        assert league is not None
        finished = time.perf_counter()

        # WAIVERS -> PRESEASON is refused with the Phase 7 message, and the walk stopped there.
        assert refusal is not None and refusal.from_phase is Phase.WAIVERS
        assert [b.code for b in refusal.verdict.blockers] == [
            season_service.BlockerCode.WAIVERS_EDGE
        ]
        assert refusal.verdict.blockers[0].message == season_service.WAIVERS_EDGE_MESSAGE
        assert "Phase 7" in refusal.verdict.blockers[0].message
        assert league.current_phase is Phase.WAIVERS and league.current_season == SEASON

        # The F1 exception is a real one: the press at DRAFT_LOTTERY refused without a board.
        assert board_refusal is not None
        assert [b.code for b in board_refusal.verdict.blockers] == [
            season_service.BlockerCode.DRAFT_BOARD_NOT_READY
        ]

        # Nineteen phase entries, in the graph's order (and the first one entered from SETUP).
        assert entries == _EXPECTED_ENTRIES
        assert await league_repo.count_phase_entries(pool, league_id, SEASON) == 19
        assert kinds["entry"] == 19

        # The four counted invariants (P5-99.q item 1).
        assert spies.media_pulse_cycles == list(range(9)), spies.media_pulse_cycles
        assert spies.media_selection_cycles == list(range(9)), spies.media_selection_cycles
        cycles_in_db = await pool.fetchval(
            "SELECT COUNT(DISTINCT cycle_index) FROM media_pulse_snapshot "
            "WHERE league_id = $1 AND season = $2",
            league_id,
            SEASON,
        )
        assert cycles_in_db == 9
        assert len(spies.closing_work_advances) == 10, spies.closing_work_advances
        assert len(spies.entry_work_advances) == 3, spies.entry_work_advances
        assert league.closing_work_completed_index == 18  # WAIVERS' entry index, the last advance
        assert league.entry_work_completed_index == 14  # PROGRESSION's, the last of the three
        # Each hook once, and only the nine cycle-bearing phases have one (F15).
        assert spies.hooks == _EXPECTED_CYCLE_PHASES, spies.hooks
        assert league.hook_completed_cycle_index == 8
        # `run_resign_window` and `classify_all_windows` are ALSO called by hooks the spec did
        # not name (`FREE_AGENCY` prices its own re-sign offers; `PRESEASON`/`TRADE_DEADLINE`
        # classify windows), so "ran exactly once" is counted per owning hook: the
        # RESIGN_WINDOW hook and the DRAFT_LOTTERY hook each ran theirs exactly once.
        assert spies.resign_window_in_hook.count(Phase.RESIGN_WINDOW) == 1
        assert spies.classify_windows_in_hook.count(Phase.DRAFT_LOTTERY) == 1
        assert Counter(spies.resign_window_in_hook) == {
            Phase.RESIGN_WINDOW: 1,
            Phase.FREE_AGENCY: 1,
        }
        assert Counter(spies.classify_windows_in_hook) == {
            Phase.PRESEASON: 1,
            Phase.TRADE_DEADLINE: 1,
            Phase.DRAFT_LOTTERY: 1,
        }
        # Two AI trade windows (the first delayed to a fraction of the season, JC-13).
        assert Counter(spies.trade_windows_in_hook) == {
            Phase.REGULAR_SEASON: 1,
            Phase.TRADE_DEADLINE: 1,
        }

        # Block presses: the count derived independently from the schedule and the targets.
        all_games = await game_repo.list_games_for_league_season(pool, league_id, SEASON)
        regular = [g for g in all_games if g.game_type is GameType.REGULAR]
        assert len(regular) == 1230 and all(g.status is GameStatus.FINAL for g in regular)
        by_date: dict = defaultdict(list)
        for g in regular:
            by_date[g.game_date].append(g.game_index)
        schedule = sorted(by_date.items())
        expected_blocks = _expected_block_presses(schedule)
        block_presses = sum(block_presses_by_phase.values())
        assert block_presses == expected_blocks, (block_presses, expected_blocks)
        # The same numbers as measured literals (2026-09-22, seed 20_260_921), NOT derived from
        # `GAMES_TARGET_FRACTION`: the derivation above shares that constant with the code under
        # test, so changing a phase's target would move both sides and stay green. These do not
        # move unless the walk's behaviour does -- 123 game dates (docs/design/
        # schedule-date-fairness.md's SDF-1 day-fill packs to the 1230/10 floor, replacing the
        # old id-biased slotting's 144), all ten games wide, and a date a target boundary falls
        # inside is played over two presses, hence 126.
        assert len(schedule) == 123
        assert block_presses == 126, block_presses
        assert {p.value: n for p, n in block_presses_by_phase.items()} == {
            "REGULAR_SEASON": 56,
            "TRADE_DEADLINE": 19,
            "POST_DEADLINE": 20,
            "REGULAR_SEASON_COMPLETE": 31,
        }, block_presses_by_phase
        assert kinds["entry"] + kinds["block"] == 145  # the presses that did work
        assert kinds["blocked"] == 2  # the DRAFT board refusal and the WAIVERS edge
        assert kinds["draft_resume"] == 0  # zero humans: the AI finished the draft on entry

        # Both P4-11 agency checkpoints ran, once each, and returned a result.
        assert spies.agency_checkpoints == [Phase.TRADE_DEADLINE, Phase.PROGRESSION]
        assert set(checkpoint_at) == {Phase.TRADE_DEADLINE, Phase.PROGRESSION}
        for result in checkpoint_at.values():
            assert result.players_evaluated > 0 and result.players_skipped == 0
        # Measured: all 420 rostered players evaluated at each checkpoint, no ladder move.
        assert {
            p.value: (r.players_evaluated, r.ladder_transitions) for p, r in checkpoint_at.items()
        } == {
            "TRADE_DEADLINE": (420, 0),
            "PROGRESSION": (420, 0),
        }

        # One stats rebuild, at REGULAR_SEASON_COMPLETE's closing press.
        assert spies.rebuilds == [(SEASON, GameType.REGULAR)]
        assert rebuild_press_phase == [Phase.REGULAR_SEASON_COMPLETE]

        # Nine wire publications; every story posted once and marked.
        assert media_published == list(range(9)), media_published
        stories = await media_repo.list_media_stories_for_league_season(
            pool, league_id=league_id, season=SEASON
        )
        assert stories, "the season produced no wire stories at all"
        # Measured (2026-09-22, seed 20_260_921, post schedule-date-fairness fix): 12/10/6/11
        # stories in cycles 1-4 and none in any other cycle. This literal moved from a flat
        # 12/12/12/12 (48 total) when docs/design/schedule-date-fairness.md's day-fill replaced
        # the old id-biased date slotting -- the cycle-triggering mechanism itself is untouched
        # (still exactly 9 wire publications, asserted above), but which teams cross a
        # STANDINGS_SURGE/COLLAPSE/STREAK threshold between two cycles depends on which games
        # were actually played in that window, and the schedule fix changes that by design.
        # Reproduced identically on two independent runs, so this is a re-measured literal, not
        # flakiness.
        assert Counter(s.cycle_index for s in stories) == {1: 12, 2: 10, 3: 6, 4: 11}
        assert len(stories) == 39
        assert all(s.posted_at is not None for s in stories)
        assert len(wire.sent) == len(stories), (len(wire.sent), len(stories))
        assert (
            await media_repo.list_unposted_media_stories(pool, league_id=league_id, season=SEASON)
            == []
        )

        # E-25's achievable half: every player with games played has a season-stats row.
        played = {
            r["player_id"]
            for r in await pool.fetch(
                """
                SELECT DISTINCT pgs.player_id FROM player_game_stats pgs
                JOIN games g ON g.id = pgs.game_id
                WHERE g.league_id = $1 AND g.season = $2 AND g.game_type = $3
                  AND g.status = 'final'
                """,
                league_id,
                SEASON,
                GameType.REGULAR.value,
            )
        }
        season_rows = {
            r["player_id"]: r["games_played"]
            for r in await pool.fetch(
                "SELECT player_id, games_played FROM player_season_stats "
                "WHERE league_id = $1 AND season = $2 AND game_type = $3",
                league_id,
                SEASON,
                GameType.REGULAR.value,
            )
        }
        assert played and played <= season_rows.keys(), sorted(played - season_rows.keys())[:10]
        assert all(season_rows[p] > 0 for p in played)
        # (the drafted class exists and was executed by the AI hook)
        assert await draft_repo.get_draft_for_season(pool, league_id, SEASON) is not None
        selections = await pool.fetchval(
            "SELECT COUNT(*) FROM draft_selections ds JOIN drafts d ON d.id = ds.draft_id "
            "WHERE d.league_id = $1 AND d.season = $2",
            league_id,
            SEASON,
        )
        assert selections == 30 * _DRAFT_ROUNDS  # the AI drafted the whole board on entry

        agency = {
            p.value: (r.players_evaluated, r.ladder_transitions) for p, r in checkpoint_at.items()
        }
        print(
            "\nE-24 WALK MEASURED:"
            f"\n  presses={presses} kinds={dict(kinds)}"
            f"\n  block presses={block_presses} by phase="
            f"{ {p.value: n for p, n in block_presses_by_phase.items()} }"
            f" (distinct game dates={len(schedule)}, expected from schedule={expected_blocks})"
            f"\n  entries={len(entries)} media cycles={len(spies.media_pulse_cycles)}"
            f" closing advances={len(spies.closing_work_advances)}"
            f" entry advances={len(spies.entry_work_advances)}"
            f"\n  stories={len(stories)} wire sends={len(wire.sent)}"
            f" stories per cycle={dict(Counter(s.cycle_index for s in stories))}"
            f"\n  agency (evaluated, ladder transitions)={agency}"
            f"\n  players with games={len(played)} draft selections={selections}"
            f"\n  seed+setup={seeded - started:.1f}s walk={finished - seeded:.1f}s"
            f" total={time.perf_counter() - started:.1f}s"
            f"\n  seconds by phase={ {p.value: round(s, 1) for p, s in press_seconds.items()} }"
        )
    finally:
        if league_id is not None:
            gm_service.artifact_path(league_id, SEASON).unlink(missing_ok=True)
            await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)
        await close_pool()
