"""The games-target column of P5-99.b.3 and its tie to the corpus generator's own copy of the
same nine numbers (phase-5-stage-e-advance.md section 5, item 5).

`GAMES_TARGET_FRACTION` holds only the four schedule phases: the other sixteen have no target
that can be unmet (`SETUP` and `PRESEASON` come before the schedule is played, every later
phase after it), so nothing reads one for them and there is no total map in production. The
hand-written table below still lists all twenty, so a phase that GAINED a target would show up
here as a phase the code names and the table calls met.

`tools/generate_media_corpus.py` stages a season's sim across its nine cycles by
`_SEASON_PROGRESS_BY_CYCLE`, keyed by cycle index; the orchestrator keys the same fractions
by `Phase`, and `dba2/` may not import `tools/` (the tools quarantine), so the numbers exist
twice. This test may import `tools/` (`tests/unit/tools/` already does) and is the only thing
that ties the copies together: it fails the day someone tunes one.

The expected map is written out by hand from the parent's table, not read from the code.
"""

from __future__ import annotations

import pytest

from dba2.domain.phase import Phase
from dba2.services import season_service
from tools import generate_media_corpus

# Written out by hand from P5-99.b.3's "Games target" column.
_EXPECTED_TARGET = {
    Phase.SETUP: 0.0,
    Phase.PRESEASON: 0.0,
    Phase.REGULAR_SEASON: 0.45,
    Phase.TRADE_DEADLINE: 0.60,
    Phase.POST_DEADLINE: 0.75,
    Phase.REGULAR_SEASON_COMPLETE: 1.0,
    Phase.PLAYIN: 1.0,
    Phase.PLAYOFFS_R1: 1.0,
    Phase.PLAYOFFS_R2: 1.0,
    Phase.CONFERENCE_FINALS: 1.0,
    Phase.FINALS: 1.0,
    Phase.AWARDS: 1.0,
    Phase.DRAFT_LOTTERY: 1.0,
    Phase.DRAFT: 1.0,
    Phase.POST_DRAFT_TRADES: 1.0,
    Phase.PROGRESSION: 1.0,
    Phase.OPTION_DECISIONS: 1.0,
    Phase.RESIGN_WINDOW: 1.0,
    Phase.FREE_AGENCY: 1.0,
    Phase.WAIVERS: 1.0,
}


def test_the_four_schedule_phases_have_the_target_the_table_names_and_no_other_phase_has_one():
    assert set(_EXPECTED_TARGET) == set(Phase), "the hand-written map must cover all 20 phases"
    assert season_service.SCHEDULE_CONSUMING_PHASES == {
        Phase.REGULAR_SEASON,
        Phase.TRADE_DEADLINE,
        Phase.POST_DEADLINE,
        Phase.REGULAR_SEASON_COMPLETE,
    }
    for phase in Phase:
        if phase in season_service.SCHEDULE_CONSUMING_PHASES:
            assert season_service.GAMES_TARGET_FRACTION[phase] == _EXPECTED_TARGET[phase], phase
        else:
            assert phase not in season_service.GAMES_TARGET_FRACTION, phase


@pytest.mark.parametrize("phase", sorted(season_service.SCHEDULE_CONSUMING_PHASES))
def test_flipping_one_phase_in_the_expected_target_map_makes_the_comparison_fail(phase):
    """The mutation twin: a table that disagrees with the code about any single schedule
    phase is not equal to it, so the comparison above cannot pass vacuously."""
    flipped = dict(_EXPECTED_TARGET)
    flipped[phase] = 0.5 if flipped[phase] != 0.5 else 0.25
    actual = dict(season_service.GAMES_TARGET_FRACTION)
    assert any(actual[p] != flipped[p] for p in actual)


def _target(phase: Phase) -> float:
    """The corpus-tie's reading of a phase's target: the code's own for the four schedule
    phases, and the table's for the rest (the corpus stages its cycles for phases the
    orchestrator gives no target to -- `PRESEASON` before the schedule, the off-season ones
    after it)."""
    return season_service.GAMES_TARGET_FRACTION.get(phase, _EXPECTED_TARGET[phase])


def test_the_targets_are_never_below_the_phase_before_them_in_a_season():
    """The fractions only rise along the regular season: a phase cannot be left with less
    played than the phase before it needed."""
    order = [
        Phase.PRESEASON,
        Phase.REGULAR_SEASON,
        Phase.TRADE_DEADLINE,
        Phase.POST_DEADLINE,
        Phase.REGULAR_SEASON_COMPLETE,
    ]
    fractions = [_target(p) for p in order]
    assert fractions == sorted(fractions)


# The corpus generator's cycles, by hand: its ninth cycle list has `TRADE_DEADLINE` twice
# (its two trade windows); the orchestrator's second one is `POST_DEADLINE` -- the ONE
# documented substitution.
_CORPUS_CYCLE_PHASES = (
    Phase.PRESEASON,
    Phase.REGULAR_SEASON,
    Phase.TRADE_DEADLINE,
    Phase.POST_DEADLINE,  # the corpus's second TRADE_DEADLINE
    Phase.DRAFT_LOTTERY,
    Phase.DRAFT,
    Phase.RESIGN_WINDOW,
    Phase.FREE_AGENCY,
    Phase.WAIVERS,
)


def test_the_corpus_generators_cycle_list_is_what_the_substitution_assumes():
    corpus = generate_media_corpus._CYCLE_PHASES
    assert len(corpus) == len(_CORPUS_CYCLE_PHASES) == 9
    substituted = tuple(
        Phase.POST_DEADLINE if (i == 3 and p is Phase.TRADE_DEADLINE) else p
        for i, p in enumerate(corpus)
    )
    assert substituted == _CORPUS_CYCLE_PHASES


def test_the_nine_cycle_bearing_targets_equal_the_corpus_progress_index_for_index():
    progress = generate_media_corpus._SEASON_PROGRESS_BY_CYCLE
    assert len(progress) == 9
    ours = tuple(_target(p) for p in _CORPUS_CYCLE_PHASES)
    assert ours == progress
    assert set(_CORPUS_CYCLE_PHASES) == season_service.CYCLE_BEARING_PHASES


def test_tuning_one_side_of_the_tie_breaks_it(monkeypatch):
    """The twin of the comparison above: move ONE of the orchestrator's four schedule
    fractions and the index-for-index equality must fail."""
    monkeypatch.setitem(season_service.GAMES_TARGET_FRACTION, Phase.TRADE_DEADLINE, 0.61)
    ours = tuple(_target(p) for p in _CORPUS_CYCLE_PHASES)
    assert ours != generate_media_corpus._SEASON_PROGRESS_BY_CYCLE
