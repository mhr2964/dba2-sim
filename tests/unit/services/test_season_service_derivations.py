"""The two derived counters' pure halves (phase-5-stage-e-advance.md P5-99.d.1).

`entry_index` and `cycle_ordinal` are both derived from `phase_history` counts
and are different numbers for most of a season, so the derivations are separate
functions and their results are never held in one variable. F15 is the reason
`cycle_ordinal` is `None` at the ten non-cycle-bearing phases rather than "the
count so far": a non-cycle-bearing entry that borrowed the next cycle's number
would let a hook checkpoint written there silently disable that cycle's hook.
"""

from __future__ import annotations

import pytest

from dba2.core.errors import PhaseError
from dba2.domain.phase import Phase
from dba2.services import season_service

# Written out by hand from the parent's cycle-bearing table (P5-99.b.3), not
# derived from the code under test -- so the set cannot be quietly regenerated
# from what it checks.
_NINE_CYCLE_BEARING = frozenset(
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
_TEN_NON_CYCLE_BEARING = frozenset(Phase) - _NINE_CYCLE_BEARING - {Phase.SETUP}


def test_the_cycle_bearing_set_is_exactly_the_parents_nine():
    assert season_service.CYCLE_BEARING_PHASES == _NINE_CYCLE_BEARING
    assert len(_TEN_NON_CYCLE_BEARING) == 10


# SETUP is never entered in-season, but it has no ordinal either.
@pytest.mark.parametrize("phase", sorted(_TEN_NON_CYCLE_BEARING | {Phase.SETUP}))
@pytest.mark.parametrize("entering", [True, False])
@pytest.mark.parametrize("counted", [0, 1, 4, 6])
def test_a_non_cycle_bearing_phase_has_no_ordinal_at_all(phase, entering, counted):
    """F15: `None`, never the count so far -- 'the count so far' is the NEXT
    cycle's ordinal, and writing a checkpoint under it disables that cycle."""
    assert (
        season_service.derive_cycle_ordinal(phase, counted, entering=entering) is None
    )


@pytest.mark.parametrize("phase", sorted(_NINE_CYCLE_BEARING))
def test_a_cycle_bearing_phase_reads_the_count_on_entry_and_one_less_on_a_block_press(phase):
    assert season_service.derive_cycle_ordinal(phase, 5, entering=True) == 5
    assert season_service.derive_cycle_ordinal(phase, 5, entering=False) == 4


def test_entry_index_reads_the_count_on_entry_and_one_less_on_a_block_press():
    assert season_service.derive_entry_index(0, entering=True) == 0
    assert season_service.derive_entry_index(13, entering=True) == 13
    assert season_service.derive_entry_index(13, entering=False) == 12


@pytest.mark.parametrize(
    "call",
    [
        lambda: season_service.derive_entry_index(0, entering=False),
        lambda: season_service.derive_cycle_ordinal(Phase.DRAFT, 0, entering=False),
    ],
)
def test_a_block_press_with_no_entry_behind_it_is_refused_not_given_minus_one(call):
    """A block press reads `count - 1`, which is only meaningful when the entry
    it is inside was counted. -1 would be a valid-looking index that no entry
    ever had. The refusal is a `PhaseError` a person can read (critic m3): a league
    can reach this state without any bug of ours -- placed mid-season by hand, or
    carried into a new season before Phase 7's rollover exists, so the season has no
    `phase_history` row for the phase it stands in -- and the cog maps a `PhaseError`
    to an ephemeral reply where a bare `ValueError` would be an unhandled crash."""
    with pytest.raises(PhaseError, match="phase_history"):
        call()


def test_the_two_counters_disagree_where_the_design_says_they_must():
    """`entry_index` and `cycle_ordinal` for the same entry: PROGRESSION is the
    fifteenth entry and has no ordinal; RESIGN_WINDOW is the seventeenth and is
    cycle 6 -- adjacent columns, different numbers, which is why they never
    share a name."""
    assert season_service.derive_entry_index(14, entering=True) == 14
    assert season_service.derive_cycle_ordinal(Phase.PROGRESSION, 6, entering=True) is None
    assert season_service.derive_entry_index(16, entering=True) == 16
    assert season_service.derive_cycle_ordinal(Phase.RESIGN_WINDOW, 6, entering=True) == 6
