"""The press core's pure tables (phase-5-stage-e-advance.md P5-99.b.1, P5-99.b.2,
build-plan commit 8): which phase an Entry press enters, and which phases own
phase work.

The work map (build-plan commit 9 filled it, commit 11 completed it): an
independent, hand-written copy of P5-99.b.3's entry-work and closing-work columns,
asserted over all 20 `Phase` members and both kinds -- so `PLAYIN` and its siblings
are proved to have nothing outstanding, not just assumed. B-1's
`outstanding = phase_has_work AND checkpoint < index` is the pure predicate below.

**The closing column is TEN phases (P5-99.q item 1):** the nine cycle-bearing phases,
whose closing work is the media cycle, plus `REGULAR_SEASON_COMPLETE`, whose closing
work is the stats rebuild and which bears no cycle. The nine are written out again
here, by hand, rather than taken from `season_service.CYCLE_BEARING_PHASES`, so the
map cannot be quietly regenerated from the code it checks. Each map carries a
mutation twin: flip one phase in the expected map and the comparison must go red.
"""

from __future__ import annotations

import pytest

from dba2.core.errors import PhaseError
from dba2.domain.phase import Phase
from dba2.phase.graph import NEXT_PHASES
from dba2.services import season_service
from dba2.services.season_service import (
    Blocker,
    BlockerCode,
    ExitVerdict,
    PressKind,
    WorkKind,
)

# Written out by hand: the single phase an Entry press enters from each phase.
# The two branch points resolve by the branch rule (F12), never alphabetically
# (`REGULAR_SEASON_COMPLETE` sorts before `TRADE_DEADLINE`).
_EXPECTED_SUCCESSOR = {
    Phase.SETUP: Phase.PRESEASON,
    Phase.PRESEASON: Phase.REGULAR_SEASON,
    Phase.REGULAR_SEASON: Phase.TRADE_DEADLINE,
    Phase.TRADE_DEADLINE: Phase.POST_DEADLINE,
    Phase.POST_DEADLINE: Phase.REGULAR_SEASON_COMPLETE,
    Phase.REGULAR_SEASON_COMPLETE: Phase.PLAYIN,
    Phase.PLAYIN: Phase.PLAYOFFS_R1,
    Phase.PLAYOFFS_R1: Phase.PLAYOFFS_R2,
    Phase.PLAYOFFS_R2: Phase.CONFERENCE_FINALS,
    Phase.CONFERENCE_FINALS: Phase.FINALS,
    Phase.FINALS: Phase.AWARDS,
    Phase.AWARDS: Phase.DRAFT_LOTTERY,
    Phase.DRAFT_LOTTERY: Phase.DRAFT,
    Phase.DRAFT: Phase.POST_DRAFT_TRADES,
    Phase.POST_DRAFT_TRADES: Phase.PROGRESSION,
    Phase.PROGRESSION: Phase.OPTION_DECISIONS,
    Phase.OPTION_DECISIONS: Phase.RESIGN_WINDOW,
    Phase.RESIGN_WINDOW: Phase.FREE_AGENCY,
    Phase.FREE_AGENCY: Phase.WAIVERS,
}


@pytest.mark.parametrize(("phase", "expected"), sorted(_EXPECTED_SUCCESSOR.items()))
def test_an_entry_press_enters_the_one_named_successor(phase, expected):
    assert season_service._successor(phase) is expected
    assert expected in NEXT_PHASES[phase], "the successor must be a legal graph edge"


def test_waivers_has_no_successor_because_it_crosses_the_season_boundary():
    assert Phase.WAIVERS not in _EXPECTED_SUCCESSOR
    with pytest.raises(PhaseError, match="Phase 7"):
        season_service._successor(Phase.WAIVERS)


def test_every_phase_but_waivers_has_an_expected_successor():
    assert set(_EXPECTED_SUCCESSOR) == set(Phase) - {Phase.WAIVERS}


# Written out by hand from P5-99.b.3's "Entry work" and "Closing work" columns.
_EXPECTED_ENTRY_WORK = frozenset({Phase.PRESEASON, Phase.TRADE_DEADLINE, Phase.PROGRESSION})
_EXPECTED_MEDIA_CYCLE = frozenset(
    {
        Phase.PRESEASON,
        Phase.REGULAR_SEASON,
        Phase.TRADE_DEADLINE,
        Phase.POST_DEADLINE,
        Phase.DRAFT_LOTTERY,
        Phase.DRAFT,
        Phase.RESIGN_WINDOW,
        Phase.FREE_AGENCY,
        Phase.WAIVERS,
    }
)
_EXPECTED_CLOSING_WORK = _EXPECTED_MEDIA_CYCLE | {Phase.REGULAR_SEASON_COMPLETE}
_EXPECTED_WORK = {
    WorkKind.ENTRY: _EXPECTED_ENTRY_WORK,
    WorkKind.CLOSING: _EXPECTED_CLOSING_WORK,
}


@pytest.mark.parametrize("kind", list(WorkKind))
@pytest.mark.parametrize("phase", sorted(Phase))
def test_a_phase_owns_exactly_the_work_the_map_names(phase, kind):
    """All 20 phases x both kinds, not just the ones that do work: this is what
    proves `PLAYIN` (and every other no-op phase) has nothing outstanding."""
    assert season_service.phase_has_work(kind, phase) is (phase in _EXPECTED_WORK[kind])


def test_entry_work_lives_at_three_phases_and_none_of_them_is_playin():
    assert len(_EXPECTED_ENTRY_WORK) == 3
    assert Phase.PLAYIN not in _EXPECTED_ENTRY_WORK | _EXPECTED_CLOSING_WORK


def test_closing_work_lives_at_ten_phases_nine_media_cycles_and_one_stats_rebuild():
    assert len(_EXPECTED_MEDIA_CYCLE) == 9
    assert len(_EXPECTED_CLOSING_WORK) == 10
    assert Phase.REGULAR_SEASON_COMPLETE in _EXPECTED_CLOSING_WORK - _EXPECTED_MEDIA_CYCLE


@pytest.mark.parametrize("phase", sorted(Phase))
def test_a_media_cycle_is_closing_work_at_exactly_the_nine_cycle_bearing_phases(phase):
    """Which phases run a media cycle (P5-99.b.3, §5 item 3): read off the unit list, not
    off `phase_has_work`, so `REGULAR_SEASON_COMPLETE` -- closing work that is NOT a media
    cycle -- is proved to own the rebuild and no media cycle."""
    units = season_service._WORK_UNITS[WorkKind.CLOSING].get(phase, ())
    assert (season_service._run_media_cycle in units) is (phase in _EXPECTED_MEDIA_CYCLE)


def test_the_stats_rebuild_is_regular_season_completes_only_closing_work():
    units = season_service._WORK_UNITS[WorkKind.CLOSING][Phase.REGULAR_SEASON_COMPLETE]
    assert units == (season_service._rebuild_regular_season_stats,)


@pytest.mark.parametrize("kind", list(WorkKind))
@pytest.mark.parametrize("phase", sorted(Phase))
def test_flipping_one_phase_in_the_expected_map_makes_the_comparison_fail(phase, kind):
    """The mutation twin: an expected map that disagrees with the code about any single
    phase is not equal to it, so the comparison above cannot pass vacuously."""
    owned = {p for p in Phase if season_service.phase_has_work(kind, p)}
    flipped = set(_EXPECTED_WORK[kind]) ^ {phase}
    assert owned != flipped


def test_the_work_map_is_the_expected_map_and_no_other_phase_appears_in_it():
    for kind in WorkKind:
        owned = {p for p in Phase if season_service.phase_has_work(kind, p)}
        assert owned == set(_EXPECTED_WORK[kind])


# --- outstanding = phase_has_work AND checkpoint < index (B-1) ----------------


@pytest.mark.parametrize("phase", sorted(Phase))
@pytest.mark.parametrize("kind", list(WorkKind))
def test_a_phase_with_no_work_of_a_kind_is_never_outstanding_however_stale_its_checkpoint(
    phase, kind
):
    """The two conditions, always: a bare `checkpoint < index` comparison reads
    "outstanding" forever at `PLAYIN`. Checkpoint -1 against index 18 is as stale
    as it gets."""
    outstanding = season_service.work_outstanding(kind, phase, checkpoint=-1, entry_index=18)
    assert outstanding is season_service.phase_has_work(kind, phase)


@pytest.mark.parametrize("kind", list(WorkKind))
def test_work_is_outstanding_only_while_the_checkpoint_is_behind_the_entry(kind):
    phase = next(iter(sorted(_EXPECTED_WORK[kind])))
    assert season_service.work_outstanding(kind, phase, checkpoint=-1, entry_index=0) is True
    assert season_service.work_outstanding(kind, phase, checkpoint=1, entry_index=2) is True
    assert season_service.work_outstanding(kind, phase, checkpoint=2, entry_index=2) is False
    assert season_service.work_outstanding(kind, phase, checkpoint=5, entry_index=2) is False


# --- the press-kind table: 20 phases x gate state, exactly one kind each (P5-99.b.2) --------
#
# `derive_press_kind` is the first-match derivation of rows 1-4 and the fall-through to an
# Entry press. Row 5 (the DRAFT destination check) needs the board: `draft_board_blockers`
# is its pure half, proved in `test_season_service_draft_branch.py`, and the last case
# below pins that exactly one edge can trigger it. The expected table is written out by
# hand, one gate state at a time, from the spec's rows -- not computed from the code's
# own rules.


def _verdict(*codes: BlockerCode) -> ExitVerdict:
    blockers = tuple(Blocker(code=c, message=c.value) for c in codes)
    return ExitVerdict(allowed=not blockers, blockers=blockers)


_C = BlockerCode
# gate state -> the verdict it is, and the kind it must give at (a) the four schedule phases,
# (b) DRAFT, (c) WAIVERS and (d) every other phase. Row order in the spec is 1 (WAIVERS), 2
# (DRAFT resume), 3 (Block), 4 (gate refuses), then Entry.
_FOUR = frozenset(
    {
        Phase.REGULAR_SEASON,
        Phase.TRADE_DEADLINE,
        Phase.POST_DEADLINE,
        Phase.REGULAR_SEASON_COMPLETE,
    }
)
_K = PressKind
_GATE_STATES = {
    # name: (verdict, kind at the four, at DRAFT, at WAIVERS, elsewhere)
    "allowed": (_verdict(), _K.ENTRY, _K.ENTRY, _K.BLOCKED, _K.ENTRY),
    "target_unmet": (
        _verdict(_C.GAMES_TARGET_NOT_REACHED),
        _K.BLOCK,
        _K.BLOCKED,
        _K.BLOCKED,
        _K.BLOCKED,
    ),
    "target_unmet_and_unfinished": (
        _verdict(_C.GAMES_TARGET_NOT_REACHED, _C.REGULAR_SEASON_UNFINISHED),
        _K.BLOCK,
        _K.BLOCKED,
        _K.BLOCKED,
        _K.BLOCKED,
    ),
    "unfinished_alone": (
        _verdict(_C.REGULAR_SEASON_UNFINISHED),
        _K.BLOCKED,
        _K.BLOCKED,
        _K.BLOCKED,
        _K.BLOCKED,
    ),
    "schedule_missing": (
        _verdict(_C.SCHEDULE_MISSING),
        _K.BLOCKED,
        _K.BLOCKED,
        _K.BLOCKED,
        _K.BLOCKED,
    ),
    "draft_unfinished": (
        _verdict(_C.DRAFT_NOT_COMPLETE),
        _K.BLOCKED,
        _K.DRAFT_RESUME,
        _K.BLOCKED,
        _K.BLOCKED,
    ),
    "waivers_edge": (_verdict(_C.WAIVERS_EDGE), _K.BLOCKED, _K.BLOCKED, _K.BLOCKED, _K.BLOCKED),
    "draft_board_not_ready": (
        _verdict(_C.DRAFT_BOARD_NOT_READY),
        _K.BLOCKED,
        _K.BLOCKED,
        _K.BLOCKED,
        _K.BLOCKED,
    ),
}


def _expected_kind(phase: Phase, gate_state: str) -> PressKind:
    _verdict_, at_four, at_draft, at_waivers, elsewhere = _GATE_STATES[gate_state]
    if phase in _FOUR:
        return at_four
    if phase is Phase.DRAFT:
        return at_draft
    if phase is Phase.WAIVERS:
        return at_waivers
    return elsewhere


@pytest.mark.parametrize("gate_state", sorted(_GATE_STATES))
@pytest.mark.parametrize("phase", sorted(Phase))
def test_every_phase_and_gate_state_derives_exactly_the_one_kind_the_table_names(phase, gate_state):
    verdict = _GATE_STATES[gate_state][0]
    kind = season_service.derive_press_kind(phase, verdict)
    assert isinstance(kind, PressKind)
    assert kind is _expected_kind(phase, gate_state)


def test_the_table_covers_all_twenty_phases_and_every_blocker_code():
    assert len(sorted(Phase)) == 20
    used = {b.code for (verdict, *_rest) in _GATE_STATES.values() for b in verdict.blockers}
    assert used == set(BlockerCode), "a blocker code the table never tries is a row nobody derived"


def test_a_gate_first_order_would_refuse_every_block_and_every_resume():
    """The row ORDER is part of the decision (rows 2 and 3 precede row 4): a target-unmet
    verdict is `not allowed`, so a first-match procedure that asked the gate first would
    return BLOCKED for every ordinary game-day press and every unfinished draft."""
    assert not _GATE_STATES["target_unmet"][0].allowed
    assert not _GATE_STATES["draft_unfinished"][0].allowed
    for phase in _FOUR:
        assert season_service.derive_press_kind(phase, _verdict(_C.GAMES_TARGET_NOT_REACHED)) is (
            PressKind.BLOCK
        )
    assert season_service.derive_press_kind(Phase.DRAFT, _verdict(_C.DRAFT_NOT_COMPLETE)) is (
        PressKind.DRAFT_RESUME
    )


def test_the_only_phase_whose_successor_is_draft_is_the_one_row_5_guards():
    """Row 5 (P5-99.h): the destination board check belongs to the edge into `DRAFT`.
    One phase has that edge, so one phase can turn an Entry into a refusal for it."""
    into_draft = [
        p for p in Phase if p is not Phase.WAIVERS and season_service._successor(p) is Phase.DRAFT
    ]
    assert into_draft == [Phase.DRAFT_LOTTERY]


def test_preseason_entry_work_runs_role_construction_before_the_schedule():
    """RC-22.1's own C11 guard (`docs/design/auto-gm-role-construction.md`):
    role construction must run AFTER `_ensure_team_strategies` and BEFORE
    `_generate_schedule` -- `_run_work_units` re-enters the whole tuple on
    a crash, and a schedule already on disk would make `_has_scheduled_
    games` true, silently skipping role construction forever for that
    league-season. Asserted on the real tuple, by name, not by position
    alone, so a reordering during a future edit is caught even if the
    tuple's length stays 3."""
    units = season_service._WORK_UNITS[WorkKind.ENTRY][Phase.PRESEASON]
    names = [unit.__name__ for unit in units]
    assert names == ["_ensure_team_strategies", "_construct_ai_roles", "_generate_schedule"]
