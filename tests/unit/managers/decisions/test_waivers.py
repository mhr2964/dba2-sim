"""Tier 1: `managers/decisions/waivers.py` -- T7a2's own cut decision
(docs/ledger/reports/phase-2-architect-round-8.md §7.2), the fallback rule
disclosed in that module's own docstring: rank ascending by `fit_value_
for`, take the lowest three, cut the one with the smallest remaining
guarantee.
"""
from __future__ import annotations

import pytest

from dba2.domain.contract import ContractStatus
from dba2.domain.phase import Phase
from dba2.domain.player import Position
from dba2.domain.team import GmPhilosophy
from dba2.managers.decisions.waivers import _remaining_guarantee, select_waive_candidate
from dba2.managers.needs import build_need_matrix
from dba2.managers.snapshot import build_team_snapshot
from dba2.managers.tuning import CompetitiveWindow
from tests.fixtures.managers import CAP_SETTINGS, make_contract, make_roster_player
from tests.fixtures.sim import make_team

_SEASON = 2026
_RNG_SEED = 2026_08
_PHASE = Phase.WAIVERS
_TEAM_ID = 1


def _snapshot(roster):
    return build_team_snapshot(make_team(_TEAM_ID), roster, (), CAP_SETTINGS)


def _matrix(snapshot):
    return build_need_matrix(snapshot)


def _select(snapshot, *, already_cut=frozenset()):
    matrix = _matrix(snapshot)
    return select_waive_candidate(
        snapshot,
        matrix,
        league_rng_seed=_RNG_SEED,
        season=_SEASON,
        phase=_PHASE,
        philosophy=GmPhilosophy.EGALITARIAN,
        window=CompetitiveWindow.RETOOL,
        cap_settings=CAP_SETTINGS,
        already_cut=already_cut,
    )


# ---------------------------------------------------------------------------
# _remaining_guarantee -- roster_service.waive's own definition, matched
# ---------------------------------------------------------------------------


def test_remaining_guarantee_sums_only_seasons_on_or_after_waived_in_season():
    contract = make_contract(
        1, team_id=_TEAM_ID, start_season=_SEASON, salaries=(5_000_000, 6_000_000, 7_000_000)
    )
    rp = make_roster_player(1, age=27, season=_SEASON, overall_cached=75, contract=contract)
    # Fully guaranteed by make_contract's own default -- the full 3-year sum
    # counts when waived_in_season is the deal's first year.
    assert _remaining_guarantee(rp, _SEASON) == 18_000_000
    # Waiving one season in drops the already-elapsed first year.
    assert _remaining_guarantee(rp, _SEASON + 1) == 13_000_000
    # Waived exactly in the final season still counts that season (>=, not >).
    assert _remaining_guarantee(rp, _SEASON + 2) == 7_000_000
    # Past the contract's own last year -- nothing remains.
    assert _remaining_guarantee(rp, _SEASON + 3) == 0


def test_remaining_guarantee_zero_for_a_contractless_player():
    rp = make_roster_player(1, age=27, season=_SEASON, overall_cached=75, contract=None)
    assert _remaining_guarantee(rp, _SEASON) == 0


def test_remaining_guarantee_excludes_unguaranteed_years():
    from dba2.domain.contract import OptionType

    contract = make_contract(
        1,
        team_id=_TEAM_ID,
        start_season=_SEASON,
        salaries=(5_000_000, 6_000_000),
        option_types=(OptionType.NONE, OptionType.PLAYER),
    )
    rp = make_roster_player(1, age=27, season=_SEASON, overall_cached=75, contract=contract)
    # make_contract's own rule: a non-NONE option year is guaranteed_amount=0.
    assert _remaining_guarantee(rp, _SEASON) == 5_000_000


# ---------------------------------------------------------------------------
# select_waive_candidate -- the fallback rule
# ---------------------------------------------------------------------------


def _roster_player(
    player_id: int,
    *,
    overall: int,
    salaries: tuple[int, ...],
    position=Position.SG,
    option_types=None,
):
    return make_roster_player(
        player_id,
        age=27,
        season=_SEASON,
        overall_cached=overall,
        team_id=_TEAM_ID,
        primary_position=position,
        peak_age_start=27,
        peak_age_end=31,
        contract=make_contract(
            player_id,
            team_id=_TEAM_ID,
            start_season=_SEASON,
            salaries=salaries,
            option_types=option_types,
        ),
    )


def test_select_waive_candidate_cuts_the_lowest_fit_value_player_when_guarantees_tie():
    """3 candidates, all sharing the same 2-year, equal-salary (equal
    remaining-guarantee) contract that extends past `_SEASON` (so none is
    excluded by the B1 expiring-this-season filter) -- the remaining-
    guarantee tiebreak can't distinguish them, so the cut must be whichever
    has the lowest `fit_value_for` (a low `overall_cached` reliably
    produces the lowest fit value at a fixed position/philosophy)."""
    roster = (
        _roster_player(1, overall=90, salaries=(3_000_000, 3_000_000)),
        _roster_player(2, overall=55, salaries=(3_000_000, 3_000_000)),  # weakest talent
        _roster_player(3, overall=80, salaries=(3_000_000, 3_000_000)),
    )
    snapshot = _snapshot(roster)
    cut, record = _select(snapshot)
    assert cut.id == 2
    assert record.subject == "player:2"
    assert record.verdict == "waive"
    assert record.reconstructs()


def test_select_waive_candidate_prefers_smallest_remaining_guarantee_within_the_pool():
    """B3 fix (critic rebuild): the prior version of this fixture gave all
    3 candidates the SAME `overall_cached` (55), which made "the single
    lowest `fit_value_for`" among them entirely a coincidence of `scout_
    player`'s own per-id scouting noise -- critic independently recomputed
    `fit_value_for` on that fixture and found player 2 (the one the test
    expected to be cut) WAS the single lowest fit_value after all, meaning
    the test never actually exercised the tiebreak; the ranking step alone
    would have produced the identical answer.

    This fixture is rebuilt so the two dimensions are deliberately
    opposed, verified against the real `fit_value_for` (not guessed):
    player 2 (overall=50) has the single lowest fit_value of the three
    (~1.7, vs. ~433 for player 3 and ~2363 for player 1 at this exact
    scenario -- computed directly against `fit_value_for` before writing
    this assertion) -- but the LARGEST remaining guarantee (15M, a 3-year
    deal). Player 1 (overall=90, the highest fit value of the three) has
    the SMALLEST remaining guarantee (2M, a cheap 2-year deal). All 3
    candidates are the whole roster, so the pool (`_WAIVE_CANDIDATE_POOL
    == 3`) always contains all of them regardless of ranking -- this test
    isolates the tiebreak alone, not pool narrowing (see the dedicated
    pool-size test below for that). The fallback rule must cut player 1
    (smallest remaining guarantee), NOT player 2 (lowest fit_value) --
    proving the tiebreak actually overrides the ranking step, not just
    duplicates it."""
    roster = (
        _roster_player(1, overall=90, salaries=(1_000_000, 1_000_000)),  # 2M left -- smallest
        _roster_player(2, overall=50, salaries=(5_000_000, 5_000_000, 5_000_000)),  # 15M left
        _roster_player(3, overall=70, salaries=(3_000_000, 3_000_000)),  # 6M left
    )
    snapshot = _snapshot(roster)
    cut, record = _select(snapshot)
    assert cut.id == 1
    assert record.reconstructs()
    # The record's own terms name the real dollars, not a placeholder.
    dead_cap_term = next(t for t in record.terms if t.label == "dead_cap_created")
    assert dead_cap_term.input_value == 2_000_000.0


def test_select_waive_candidate_pool_size_excludes_a_smaller_guarantee_player_outside_top_three():
    """B3 fix, the pool-SIZE half (distinct from the tiebreak test above):
    4 candidates, verified ascending by real `fit_value_for` (computed
    directly, not guessed): player 1 (~17.5) < player 2 (~231.6) < player
    3 (~1035.0) < player 4 (~3053.3) -- `_WAIVE_CANDIDATE_POOL == 3` keeps
    only players 1-3 in the pool; player 4 is excluded purely by ranking.

    Player 4 carries the smallest remaining guarantee of all FOUR
    candidates (1M) -- if pool size did not matter (e.g. a mutation
    widening or deleting `_WAIVE_CANDIDATE_POOL`), player 4 would win the
    tiebreak and get cut. It must NOT be selected. Within the real pool
    (players 1-3), player 2 carries the smallest remaining guarantee (4M,
    smaller than player 1's 10M and player 3's 12M) and must be the one
    selected -- proving both that the pool is capped at 3 AND that the
    tiebreak still operates correctly within that capped pool."""
    roster = (
        _roster_player(1, overall=50, salaries=(5_000_000, 5_000_000)),  # 10M left, in pool
        _roster_player(  # 4M left, in pool -- smallest in-pool
            2, overall=65, salaries=(2_000_000, 2_000_000)
        ),
        _roster_player(3, overall=80, salaries=(6_000_000, 6_000_000)),  # 12M left, in pool
        _roster_player(  # 1M left -- smallest overall, but NOT in pool
            4, overall=95, salaries=(500_000, 500_000)
        ),
    )
    snapshot = _snapshot(roster)
    cut, record = _select(snapshot)
    assert cut.id == 2
    assert record.reconstructs()
    dead_cap_term = next(t for t in record.terms if t.label == "dead_cap_created")
    assert dead_cap_term.input_value == 4_000_000.0


def test_select_waive_candidate_excludes_already_cut_players():
    roster = (
        _roster_player(1, overall=55, salaries=(3_000_000, 3_000_000)),
        _roster_player(2, overall=56, salaries=(3_000_000, 3_000_000)),
        _roster_player(3, overall=90, salaries=(3_000_000, 3_000_000)),
    )
    snapshot = _snapshot(roster)
    first_cut, _ = _select(snapshot)
    second_cut, _ = _select(snapshot, already_cut=frozenset({first_cut.id}))
    assert second_cut.id != first_cut.id


def test_select_waive_candidate_excludes_contractless_players():
    contractless = make_roster_player(
        9, age=25, season=_SEASON, overall_cached=50, team_id=_TEAM_ID, contract=None
    )
    contracted = _roster_player(10, overall=80, salaries=(3_000_000, 3_000_000))
    snapshot = _snapshot((contractless, contracted))
    cut, _ = _select(snapshot)
    assert cut.id == 10


def test_select_waive_candidate_raises_when_no_eligible_candidate_remains():
    roster = (_roster_player(1, overall=70, salaries=(3_000_000,)),)
    snapshot = _snapshot(roster)
    with pytest.raises(ValueError):
        _select(snapshot, already_cut=frozenset({1}))


def test_select_waive_candidate_raises_when_no_eligible_candidate_is_contractless_only():
    contractless = make_roster_player(
        9, age=25, season=_SEASON, overall_cached=50, team_id=_TEAM_ID, contract=None
    )
    snapshot = _snapshot((contractless,))
    with pytest.raises(ValueError):
        _select(snapshot)


def test_select_waive_candidate_excludes_players_expiring_this_season():
    """B1 fix: a contract whose last year IS `_SEASON` (`_expiring_by_
    team`'s own exact-equality test) must never reach the candidate pool
    -- that player is leaving the roster for free at the season boundary
    regardless of anything this function does, so cutting them would only
    convert a $0 departure into a real dead-cap charge. Player 1's 1-year
    deal ends exactly at `_SEASON` and must be skipped even though it is
    (by far) the weakest fit; player 2's 2-year deal survives into
    `_SEASON + 1` and must be the one selected."""
    expiring_this_season = _roster_player(1, overall=40, salaries=(3_000_000,))
    still_rostered_next_season = _roster_player(2, overall=60, salaries=(3_000_000, 3_000_000))
    snapshot = _snapshot((expiring_this_season, still_rostered_next_season))
    cut, _ = _select(snapshot)
    assert cut.id == 2


def test_select_waive_candidate_raises_when_only_expiring_candidate_remains():
    """Same B1 fix, the raise path: a lone candidate whose contract ends
    exactly at `_SEASON` leaves nothing eligible, same as a lone
    contract-less candidate does."""
    expiring_this_season = _roster_player(1, overall=70, salaries=(3_000_000,))
    snapshot = _snapshot((expiring_this_season,))
    with pytest.raises(ValueError):
        _select(snapshot)


def test_select_waive_candidate_asserts_loudly_on_a_non_active_contract():
    """§7.3's own guard: a WAIVED/EXPIRED contract must never reach the
    cut-candidate pool -- asserted, not silently filtered."""
    import dataclasses

    bad = _roster_player(1, overall=70, salaries=(3_000_000, 3_000_000))
    bad = dataclasses.replace(
        bad,
        contract=dataclasses.replace(
            bad.contract,
            contract=dataclasses.replace(bad.contract.contract, status=ContractStatus.WAIVED),
        ),
    )
    snapshot = _snapshot((bad,))
    with pytest.raises(AssertionError):
        _select(snapshot)


def test_select_waive_candidate_record_terms_sum_to_score():
    roster = (
        _roster_player(1, overall=55, salaries=(4_000_000, 4_000_000)),
        _roster_player(2, overall=60, salaries=(5_000_000, 5_000_000)),
        _roster_player(3, overall=65, salaries=(6_000_000, 6_000_000)),
        _roster_player(4, overall=90, salaries=(1_000_000, 1_000_000)),
    )
    snapshot = _snapshot(roster)
    cut, record = _select(snapshot)
    assert record.reconstruction_error() < 1e-6
    assert record.threshold == 0.0
    assert len(record.terms) == 2
    assert {t.label for t in record.terms} == {"fit_value_lost", "dead_cap_created"}
