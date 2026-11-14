"""`/advance-run`'s three renderings -- the summary panel, the score-free `#games` digest and
the progress line (docs/design/advance-run.md AR-8, AR-9) -- built from plain values only.

The digest is the one durable `#games` record of a run, so its test is the spoiler test: it is
built from `RunSummary`, which carries no score, and no score-shaped text may appear in it.
"""

from __future__ import annotations

import re
from dataclasses import replace

from dba2.domain.phase import Phase
from dba2.presentation.advance import (
    STALL_REASONS,
    PhaseAiCounts,
    RunStopKind,
    RunSummary,
    build_run_digest,
    build_run_panel,
    build_run_progress_line,
    run_stop_sentence,
)
from dba2.presentation.panel import MAX_TOTAL_CHARS, PanelColor
from dba2.services.gm_service import DraftCycleReason
from dba2.services.season_run import RunStop

_SCORE_SHAPED = re.compile(r"(?<![\d-])\d{2,3}\s*[-–:]\s*\d{2,3}(?![\d-])")


def _summary(**overrides) -> RunSummary:
    base = RunSummary(
        stop=RunStopKind.TARGET_REACHED,
        from_phase=Phase.PRESEASON,
        target=Phase.TRADE_DEADLINE,
        final_phase=Phase.TRADE_DEADLINE,
        presses=71,
        elapsed_seconds=134.0,
        phases_entered=(Phase.REGULAR_SEASON, Phase.TRADE_DEADLINE),
        presses_by_kind=(("entry", 2), ("block", 69), ("blocked", 0), ("draft_resume", 0)),
        game_days=65,
        games=554,
        first_game_date="2026-10-20",
        last_game_date="2027-01-03",
        ai_counts=(PhaseAiCounts(Phase.REGULAR_SEASON, trades=3),),
        media_cycles=2,
        wire_stories=24,
    )
    return replace(base, **overrides)


def test_the_stop_kinds_are_the_services_stops_one_to_one():
    assert {k.value for k in RunStopKind} == {s.value for s in RunStop}


def test_every_draft_cycle_reason_has_a_phrase_for_the_stall_sentence():
    assert set(STALL_REASONS) == {r.value for r in DraftCycleReason}


def test_every_stop_has_its_own_sentence():
    sentences = {
        stop: run_stop_sentence(
            _summary(
                stop=stop,
                blockers=("the exit gate's own words",),
                on_the_clock="TOR (round 1, pick 5)",
                stall_reason="pool_exhausted",
                error_message="This league is busy",
            )
        )
        for stop in RunStopKind
    }
    assert all(sentences.values())
    assert len(set(sentences.values())) == len(RunStopKind)


def test_the_stop_sentences_say_what_the_design_says():
    assert run_stop_sentence(_summary()) == "The league is in TRADE_DEADLINE."
    assert run_stop_sentence(_summary(presses=0)) == "The league is already in TRADE_DEADLINE."
    assert run_stop_sentence(_summary(stop=RunStopKind.CANCELLED)) == (
        "Stopped by /advance-run-stop."
    )
    assert "tools/dev_league_draft.py" in run_stop_sentence(
        _summary(stop=RunStopKind.NEEDS_A_DRAFT_BOARD)
    )
    assert run_stop_sentence(
        _summary(stop=RunStopKind.SEASON_OVER, blockers=("WAIVERS -> PRESEASON is Phase 7.",))
    ) == ("WAIVERS -> PRESEASON is Phase 7.")
    assert "TOR (round 1, pick 5)" in run_stop_sentence(
        _summary(stop=RunStopKind.NEEDS_A_HUMAN_PICK, on_the_clock="TOR (round 1, pick 5)")
    )
    assert "nobody left for an AI pick" in run_stop_sentence(
        _summary(stop=RunStopKind.DRAFT_STALLED, stall_reason="pool_exhausted")
    )
    assert "the lottery has not given every pick a number" in run_stop_sentence(
        _summary(stop=RunStopKind.DRAFT_STALLED, stall_reason="unresolved_picks")
    )
    assert run_stop_sentence(
        _summary(stop=RunStopKind.PRESS_FAILED, error_message="No game of this block")
    ).endswith("No game of this block")
    assert run_stop_sentence(_summary(stop=RunStopKind.CAP_REACHED, presses=400)) == (
        "Stopped after 400 presses / 2m14s — run /advance-run TRADE_DEADLINE again to continue."
    )
    passed = run_stop_sentence(
        _summary(
            stop=RunStopKind.TARGET_PASSED,
            target=Phase.TRADE_DEADLINE,
            final_phase=Phase.POST_DEADLINE,
        )
    )
    assert "POST_DEADLINE" in passed and "past TRADE_DEADLINE" in passed
    assert run_stop_sentence(_summary(stop=RunStopKind.NO_PROGRESS)) == (
        "Two presses in a row changed nothing; stopping so this does not spin."
    )


def test_the_progress_line_is_the_designs_line():
    line = build_run_progress_line(
        phase=Phase.REGULAR_SEASON,
        game_days=43,
        games=366,
        last_game_date="2026-12-03",
        presses=71,
        elapsed_seconds=134,
    )
    assert line == "REGULAR_SEASON — 43 game days simmed, 366 games, 2026-12-03. 71 presses, 2m14s."


def test_the_progress_line_before_any_game_has_no_date_and_pluralises():
    line = build_run_progress_line(
        phase=Phase.PRESEASON,
        game_days=0,
        games=0,
        last_game_date=None,
        presses=1,
        elapsed_seconds=3.4,
    )
    assert line == "PRESEASON — 0 game days simmed, 0 games. 1 press, 3s."


def test_the_digest_carries_the_summary_and_no_score():
    digest = build_run_digest(_summary())

    assert "Simmed 65 game days, 2026-10-20 → 2027-01-03 (554 games)." in digest
    assert "Entered: REGULAR_SEASON → TRADE_DEADLINE." in digest
    assert "REGULAR_SEASON: 3 trade(s)" in digest
    assert digest.endswith("Scores are in /standings and /box.")
    assert not _SCORE_SHAPED.search(digest), digest
    assert len(digest) <= 2000


def test_the_digest_is_clipped_to_a_discord_message_and_keeps_its_footer():
    huge = _summary(
        phases_entered=tuple(Phase),
        ai_counts=tuple(PhaseAiCounts(p, trades=99, signings=99) for p in Phase) * 4,
        failed_games=tuple(f"game {i}: cause" for i in range(200)),
    )
    digest = build_run_digest(huge)
    assert len(digest) <= 2000
    assert digest.endswith("Scores are in /standings and /box.")


def test_the_digest_of_a_blocked_run_carries_its_blockers_and_notices_verbatim():
    digest = build_run_digest(
        _summary(
            stop=RunStopKind.BLOCKED,
            final_phase=Phase.DRAFT,
            blockers=("the draft is not complete", "board has 3 open picks"),
            notices=("A wire post failed (error logged).",),
        )
    )
    assert "What is in the way:\n- the draft is not complete\n- board has 3 open picks" in digest
    assert "Did not go out:\n- A wire post failed (error logged)." in digest
    assert digest.endswith("Scores are in /standings and /box.")
    assert not _SCORE_SHAPED.search(digest), digest


def test_a_finished_run_without_notices_has_no_extra_digest_sections():
    digest = build_run_digest(_summary(blockers=("stale",)))
    assert "What is in the way" not in digest and "Did not go out" not in digest


def test_the_digest_keeps_its_blockers_and_notices_when_the_facts_are_clipped():
    long_line = "x" * 900
    huge = _summary(
        stop=RunStopKind.BLOCKED,
        phases_entered=tuple(Phase),
        ai_counts=tuple(PhaseAiCounts(p, trades=99, signings=99) for p in Phase) * 4,
        failed_games=tuple(f"game {i}: cause" for i in range(200)),
        blockers=("the exit gate says X",) + (long_line,) * 30,
        notices=("the digest tail must show me",) + (long_line,) * 30,
    )
    digest = build_run_digest(huge)
    assert len(digest) <= 2000
    assert "the exit gate says X" in digest and "the digest tail must show me" in digest
    assert "…and" in digest  # the overflow is summarised, not silently dropped
    assert digest.endswith("Scores are in /standings and /box.")


def test_the_score_pattern_used_above_would_catch_a_score():
    assert _SCORE_SHAPED.search("Final: BOS 101-99 NYK")
    assert not _SCORE_SHAPED.search("2026-10-20 → 2027-01-03")


def test_the_panel_for_a_finished_run():
    panel = build_run_panel(_summary())

    assert panel.title == "Ran to TRADE_DEADLINE"
    assert panel.color is PanelColor.POSITIVE
    assert panel.description.splitlines()[0] == "The league is in TRADE_DEADLINE."
    assert [f.name for f in panel.fields] == ["AI activity"]


def test_the_panel_for_a_run_that_was_already_there():
    panel = build_run_panel(_summary(presses=0, phases_entered=(), game_days=0, games=0))
    assert panel.title == "Already in TRADE_DEADLINE"


def test_a_blocked_run_names_every_blocker_and_says_where_it_stopped():
    panel = build_run_panel(
        _summary(
            stop=RunStopKind.BLOCKED,
            final_phase=Phase.DRAFT,
            blockers=("first reason", "second reason"),
        )
    )
    assert panel.title == "Run to TRADE_DEADLINE stopped in DRAFT"
    (field,) = [f for f in panel.fields if f.name == "What is in the way"]
    assert "first reason" in field.value and "second reason" in field.value
    assert panel.color is PanelColor.WARNING


def test_did_not_go_out_notices_show_and_warn():
    panel = build_run_panel(_summary(notices=("The wire post failed (error logged).",)))
    assert panel.color is PanelColor.WARNING
    assert [f.name for f in panel.fields][-1] == "Did not go out"


def test_the_worst_case_panel_stays_under_discords_limits():
    """Built AFTER hundreds of presses commit, so it must never raise on size."""
    long_line = "x" * 900
    summary = _summary(
        stop=RunStopKind.BLOCKED,
        phases_entered=tuple(Phase),
        ai_counts=tuple(PhaseAiCounts(p, 99, 99, 99, 99) for p in Phase) * 3,
        failed_games=tuple(long_line for _ in range(50)),
        blockers=tuple(long_line for _ in range(50)),
        notices=tuple(long_line for _ in range(50)),
        error_message=long_line,
    )
    panel = build_run_panel(summary)
    total = (
        len(panel.title)
        + len(panel.description)
        + len(panel.footer)
        + sum(len(f.name) + len(f.value) for f in panel.fields)
    )
    assert total <= MAX_TOTAL_CHARS
