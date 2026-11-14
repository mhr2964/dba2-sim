"""`presentation/advance.py` -- the press report as a `Panel`
(docs/design/phase-5-stage-e-advance.md P5-99.a, P5-99.m, build-plan commit 14).

**Goldens, written as literals**, by the rule `test_draft.py` states: a test that
rebuilds the expected string the way the renderer does agrees with the renderer's own
bug. The presentation layer takes plain values only -- never a service type -- so
every input here is a string, a number or a `Phase`.

Two properties beyond the wording: a panel is always inside Discord's embed limits
however many blockers or notes a press names (a `Panel` raises over them, and a raise
there would cost the person the whole reply), and the four press outcomes are told apart
by title so a Blocked press can never read like a success.
"""

from __future__ import annotations

import pytest

from dba2.domain.phase import Phase
from dba2.presentation.advance import (
    AdvanceToSummary,
    ForceSummary,
    PressOutcome,
    PressSummary,
    build_advance_to_panel,
    build_force_panel,
    build_press_panel,
)
from dba2.presentation.panel import MAX_TOTAL_CHARS, PanelColor


def test_an_entry_press_names_both_phases_and_what_it_did():
    panel = build_press_panel(
        PressSummary(
            outcome=PressOutcome.ENTRY,
            from_phase=Phase.SETUP,
            to_phase=Phase.PRESEASON,
            done=("Expired 2 stale trade offer(s).", "Media cycle 0 finished."),
        )
    )

    assert panel.title == "Advanced to PRESEASON"
    assert panel.description == (
        "**SETUP** -> **PRESEASON**\n- Expired 2 stale trade offer(s).\n- Media cycle 0 finished."
    )
    assert panel.fields == ()
    assert panel.color is PanelColor.POSITIVE


def test_a_block_press_says_what_still_stands_between_the_league_and_the_next_phase():
    panel = build_press_panel(
        PressSummary(
            outcome=PressOutcome.BLOCK,
            from_phase=Phase.REGULAR_SEASON,
            done=("Simulated 3 game(s) on 2026-10-25.",),
            gate_lines=(
                "2 game(s) this phase must play before it can be left are not played yet.",
            ),
        )
    )

    assert panel.title == "Simulated a game day in REGULAR_SEASON"
    assert panel.description == "- Simulated 3 game(s) on 2026-10-25."
    assert [(f.name, f.value) for f in panel.fields] == [
        (
            "Before REGULAR_SEASON can be left",
            "- 2 game(s) this phase must play before it can be left are not played yet.",
        )
    ]
    assert panel.color is PanelColor.NEUTRAL


def test_a_block_press_that_met_its_target_says_the_league_moves_on_next():
    panel = build_press_panel(
        PressSummary(
            outcome=PressOutcome.BLOCK,
            from_phase=Phase.REGULAR_SEASON,
            done=("Simulated 1 game(s) on 2026-11-03.",),
        )
    )

    assert [(f.name, f.value) for f in panel.fields] == [
        ("Before REGULAR_SEASON can be left", "Nothing -- press /advance again to move on.")
    ]


def test_a_blocked_press_names_every_blocker_and_says_the_league_did_not_move():
    panel = build_press_panel(
        PressSummary(
            outcome=PressOutcome.BLOCKED,
            from_phase=Phase.DRAFT_LOTTERY,
            gate_lines=(
                "the draft cannot be entered yet: this season has no drafts row.",
                "second",
            ),
        )
    )

    assert panel.title == "Blocked in DRAFT_LOTTERY"
    assert panel.description == "The league did not move."
    assert [(f.name, f.value) for f in panel.fields] == [
        (
            "What is in the way",
            "- the draft cannot be entered yet: this season has no drafts row.\n- second",
        )
    ]
    assert panel.color is PanelColor.NEGATIVE


def test_a_blocked_press_still_reports_the_work_it_finished_first():
    panel = build_press_panel(
        PressSummary(
            outcome=PressOutcome.BLOCKED,
            from_phase=Phase.WAIVERS,
            done=("Finished the AI pass left over from a previous press.",),
            gate_lines=("stop",),
        )
    )

    assert panel.description == (
        "The league did not move.\n- Finished the AI pass left over from a previous press."
    )


def test_a_draft_resume_press_says_where_the_draft_stands():
    panel = build_press_panel(
        PressSummary(
            outcome=PressOutcome.DRAFT_RESUME,
            from_phase=Phase.DRAFT,
            done=("The AI made 4 pick(s).",),
            gate_lines=("the draft is not finished: team 7 is on the clock.",),
        )
    )

    assert panel.title == "Draft resumed"
    assert panel.description == "- The AI made 4 pick(s)."
    assert [(f.name, f.value) for f in panel.fields] == [
        ("Before DRAFT can be left", "- the draft is not finished: team 7 is on the clock.")
    ]


def test_notices_make_a_warning_and_are_listed_apart_from_what_happened():
    panel = build_press_panel(
        PressSummary(
            outcome=PressOutcome.ENTRY,
            from_phase=Phase.PLAYIN,
            to_phase=Phase.PLAYOFFS_R1,
            notices=("The wire post failed (error logged).",),
        )
    )

    assert panel.description == "**PLAYIN** -> **PLAYOFFS_R1**"
    assert [(f.name, f.value) for f in panel.fields] == [
        ("Did not go out", "- The wire post failed (error logged).")
    ]
    assert panel.color is PanelColor.WARNING


@pytest.mark.parametrize("outcome", list(PressOutcome))
def test_a_flood_of_lines_still_builds_a_panel_inside_discords_limits(outcome):
    """Ten game ids per blocker sentence, a hook that touched 300 players, a wire that
    failed 40 times: none may make `Panel` raise, because the raise would land after the
    press committed and cost the person the whole reply."""
    long_line = "x" * 900
    panel = build_press_panel(
        PressSummary(
            outcome=outcome,
            from_phase=Phase.REGULAR_SEASON,
            to_phase=Phase.TRADE_DEADLINE if outcome is PressOutcome.ENTRY else None,
            done=tuple(long_line for _ in range(30)),
            gate_lines=tuple(long_line for _ in range(30)),
            notices=tuple(long_line for _ in range(30)),
        )
    )

    total = (
        len(panel.title)
        + len(panel.description)
        + sum(len(f.name) + len(f.value) for f in panel.fields)
    )
    assert total <= MAX_TOTAL_CHARS
    assert "more" in panel.description or "more" in "".join(f.value for f in panel.fields)


# --- /advance-to and /advance-force (build-plan commit 15) --------------------------


def test_an_advance_to_that_reached_its_target_shows_the_route_it_took():
    panel = build_advance_to_panel(
        AdvanceToSummary(
            from_phase=Phase.PLAYIN,
            requested=Phase.PLAYOFFS_R2,
            entered=(Phase.PLAYOFFS_R1, Phase.PLAYOFFS_R2),
            reached=True,
        )
    )

    assert panel.title == "Advanced to PLAYOFFS_R2"
    assert panel.description == "**PLAYIN** -> **PLAYOFFS_R1** -> **PLAYOFFS_R2**"
    assert panel.color is PanelColor.POSITIVE


def test_an_advance_to_that_stopped_short_names_the_phase_it_stopped_before_and_why():
    panel = build_advance_to_panel(
        AdvanceToSummary(
            from_phase=Phase.PLAYOFFS_R1,
            requested=Phase.DRAFT_LOTTERY,
            entered=(Phase.PLAYOFFS_R2, Phase.FINALS),
            reached=False,
            stop_reason="the next phase, DRAFT_LOTTERY, does work; press /advance to enter it.",
        )
    )

    assert panel.title == "Stopped short of DRAFT_LOTTERY"
    assert panel.description == (
        "**PLAYOFFS_R1** -> **PLAYOFFS_R2** -> **FINALS**\n"
        "the next phase, DRAFT_LOTTERY, does work; press /advance to enter it."
    )
    assert panel.color is PanelColor.NEUTRAL


def test_an_advance_to_that_could_not_move_says_the_league_did_not_move_and_lists_the_gate():
    panel = build_advance_to_panel(
        AdvanceToSummary(
            from_phase=Phase.REGULAR_SEASON_COMPLETE,
            requested=Phase.PLAYOFFS_R1,
            entered=(),
            reached=False,
            stop_reason="REGULAR_SEASON_COMPLETE cannot be left yet.",
            gate_lines=("the regular season is not finished: 1 game(s) are still scheduled.",),
        )
    )

    assert panel.description == (
        "The league did not move.\nREGULAR_SEASON_COMPLETE cannot be left yet."
    )
    assert [(f.name, f.value) for f in panel.fields] == [
        (
            "What is in the way",
            "- the regular season is not finished: 1 game(s) are still scheduled.",
        )
    ]


def test_an_advance_to_the_phase_the_league_is_in_says_so():
    panel = build_advance_to_panel(
        AdvanceToSummary(from_phase=Phase.PLAYIN, requested=Phase.PLAYIN, entered=(), reached=True)
    )

    assert panel.title == "Already in PLAYIN"
    assert panel.description == "Nothing to do."


def test_a_force_panel_names_what_it_overrode_and_what_it_skipped_and_that_nothing_ran():
    panel = build_force_panel(
        ForceSummary(
            from_phase=Phase.DRAFT,
            to_phase=Phase.POST_DRAFT_TRADES,
            reason="league is stuck",
            overrode=("the draft is not finished: team 7 is on the clock.",),
            skipped_work=("DRAFT closing work",),
        )
    )

    assert panel.title == "Forced DRAFT -> POST_DRAFT_TRADES"
    assert (
        panel.description == "**DRAFT** -> **POST_DRAFT_TRADES** (forced)\nReason: league is stuck"
    )
    assert [(f.name, f.value) for f in panel.fields] == [
        ("Blockers it overrode", "- the draft is not finished: team 7 is on the clock."),
        ("Work it skipped", "- DRAFT closing work"),
    ]
    assert "No hook, phase work, sim or media ran" in panel.footer
    assert panel.color is PanelColor.WARNING


def test_a_force_with_nothing_to_override_says_none_rather_than_dropping_the_field():
    panel = build_force_panel(
        ForceSummary(
            from_phase=Phase.PLAYIN,
            to_phase=Phase.PLAYOFFS_R2,
            reason="skip a round",
            overrode=(),
            skipped_work=(),
        )
    )

    assert [(f.name, f.value) for f in panel.fields] == [
        ("Blockers it overrode", "None."),
        ("Work it skipped", "None."),
    ]


def test_a_force_flood_still_fits_discords_limits():
    long_line = "y" * 900
    panel = build_force_panel(
        ForceSummary(
            from_phase=Phase.DRAFT,
            to_phase=Phase.POST_DRAFT_TRADES,
            reason="r" * 1500,
            overrode=tuple(long_line for _ in range(30)),
            skipped_work=tuple(long_line for _ in range(30)),
        )
    )

    total = (
        len(panel.title)
        + len(panel.description)
        + len(panel.footer)
        + sum(len(f.name) + len(f.value) for f in panel.fields)
    )
    assert total <= MAX_TOTAL_CHARS
