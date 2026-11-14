"""`tools/_gm_draft_class_stub.py`'s own generation-correctness -- shape,
determinism, and value ranges (Decision P2-32,
docs/design/phase-2-manager-minds.md §3, §12 step 9).

The import-quarantine assertion ("nothing under `dba2/` imports this
module") lives in `tests/unit/test_layering.py`, matching that file's own
AST-scanning style; this file covers the stub's own *behavior* instead,
which needs no AST walking, just calling the function and checking its
output -- the same "quarantine check vs. the stub's own correctness are
different kinds of test, in different files" split this dispatch's task
description asked for a judgment call on.
"""
from __future__ import annotations

from dba2.domain.player import RosterStatus
from dba2.managers.picks import entry_ovr
from tools._gm_draft_class_stub import generate_draft_class

_SEASON = 2026


def test_generates_exactly_60_prospects_by_default():
    prospects = generate_draft_class(season=_SEASON, seed=1)
    assert len(prospects) == 60


def test_every_prospect_is_draft_eligible_with_no_team_and_no_contract():
    prospects = generate_draft_class(season=_SEASON, seed=1)
    for rp in prospects:
        assert rp.player.roster_status is RosterStatus.DRAFT_ELIGIBLE
        assert rp.player.team_id is None
        assert rp.contract is None


def test_prospect_ids_are_unique():
    prospects = generate_draft_class(season=_SEASON, seed=1)
    assert len({rp.id for rp in prospects}) == len(prospects)


def test_class_size_is_configurable():
    prospects = generate_draft_class(season=_SEASON, seed=1, class_size=12)
    assert len(prospects) == 12


def test_ages_and_peak_windows_stay_in_their_documented_ranges():
    prospects = generate_draft_class(season=_SEASON, seed=3)
    for rp in prospects:
        age = _SEASON - rp.player.birth_date.year
        assert 19 <= age <= 22
        assert 24 <= rp.ratings.peak_age_start <= 28
        assert rp.ratings.peak_age_start < rp.ratings.peak_age_end


def test_overall_cached_stays_inside_the_generator_clamp():
    prospects = generate_draft_class(season=_SEASON, seed=3)
    for rp in prospects:
        assert 25 <= rp.player.overall_cached <= 99


def test_deterministic_at_a_fixed_seed():
    a = generate_draft_class(season=_SEASON, seed=99)
    b = generate_draft_class(season=_SEASON, seed=99)
    assert [rp.player.overall_cached for rp in a] == [rp.player.overall_cached for rp in b]
    assert [rp.player.primary_position for rp in a] == [rp.player.primary_position for rp in b]
    assert [rp.player.birth_date for rp in a] == [rp.player.birth_date for rp in b]


def test_different_seeds_produce_different_classes():
    a = generate_draft_class(season=_SEASON, seed=1)
    b = generate_draft_class(season=_SEASON, seed=2)
    assert [rp.player.overall_cached for rp in a] != [rp.player.overall_cached for rp in b]


def test_no_two_prospects_share_an_identical_ratings_profile():
    """A weaker, stub-local cousin of `test_draft.py`'s own B15 regression
    test (which asserts distinct *VP scores*, needing `draft.py`): here,
    distinct *raw ratings tuples* -- practically guaranteed by the
    generator's own continuous-noise draws, but pinned as a real assertion
    rather than assumed."""
    prospects = generate_draft_class(season=_SEASON, seed=5)
    profiles = {
        tuple(getattr(rp.ratings, field) for field in ("speed", "strength", "basketball_iq"))
        for rp in prospects
    }
    assert len(profiles) == len(prospects)


def test_mutation_check_seed_actually_drives_every_stream():
    """If the stub ever regressed to consuming an un-seeded (or
    module-global) RNG for one of its draws -- e.g. position, physicals,
    or the peak-target noise -- two different seeds would still coincide
    on that one field across the whole class. Checks every field this
    module's own docstring names as RNG-driven varies between two seeds
    somewhere in the class."""
    a = generate_draft_class(season=_SEASON, seed=10)
    b = generate_draft_class(season=_SEASON, seed=11)
    pairs = list(zip(a, b, strict=True))
    assert any(x.player.primary_position != y.player.primary_position for x, y in pairs)
    assert any(x.player.height_in != y.player.height_in for x, y in pairs)
    assert any(x.player.birth_date.year != y.player.birth_date.year for x, y in pairs)
    assert any(x.ratings.peak_age_start != y.ratings.peak_age_start for x, y in pairs)


def test_back_solve_tracks_the_slot_tables_own_entry_to_peak_gap_unbiased():
    """Critic round 18's B4: the back-solve fix (`target_peak - (peak_ovr(i)
    - entry_ovr(i))`, replacing the old `GROWTH_MAX * age_ramp(age, ...)`
    ceiling) had zero test coverage -- reverting it to the pre-fix formula
    left the full suite green. The old formula's overshoot was **age-
    correlated, not a uniform shift**: `GROWTH_MAX * age_ramp` tops out
    ~12 points at age 19 and drops to ~6.86 at age 22 (against the slot
    table's own flat 9-14 point entry-to-peak gap across slots 1-30), so
    an unconditioned class-wide mean washes the trend out (measured only
    +0.61 across the mutated class, nowhere near the ~7-point worst-case
    figure) -- the regression guard has to check **per age**, not overall.
    Prospect index `i` in generation order (1..60) is the target slot the
    back-solve solves against. Measured per-age bias under the real fix:
    19: +0.11, 20: -0.33, 21: -0.04, 22: +0.27 -- flat, no age trend.
    Measured under the pre-fix formula (reproduced while writing this
    test): 19: -1.80, 20: -0.68, 21: +1.44, 22: **+3.47** -- a clear,
    monotone age trend this test's per-age bound catches."""
    from collections import defaultdict

    by_age: dict[int, list[float]] = defaultdict(list)
    for seed in range(1, 21):
        prospects = generate_draft_class(season=_SEASON, seed=seed)
        for i, rp in enumerate(prospects, start=1):
            age = _SEASON - rp.player.birth_date.year
            by_age[age].append(rp.player.overall_cached - entry_ovr(float(i)))

    assert set(by_age) == {19, 20, 21, 22}
    for age, diffs in by_age.items():
        mean_diff = sum(diffs) / len(diffs)
        assert abs(mean_diff) < 2.0, (age, mean_diff, len(diffs))
