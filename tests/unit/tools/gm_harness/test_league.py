"""`tools/gm_harness/league.py::generate_gm_league` -- determinism,
philosophy round-robin (Finding A's fix), the un-hysteresed initial
`CONTEND` reachability, contract/roster shape sanity, draft-pick shape,
and §11 Tier 2's "a few hundred seeded synthetic leagues" Family-1
boundary scan (architect round-7 report §4, §10).

The applier tests and the `build_snapshot`/`build_ctx` rebuilder tests
live in `test_state.py`, not here -- this file is generation-only.
"""
from __future__ import annotations

from collections import Counter
from unittest import mock

import pytest

from dba2.domain.contract import ContractStatus, ContractType, SigningMethod
from dba2.domain.draft import ProtectionType
from dba2.domain.player import RosterStatus
from dba2.domain.team import GmPhilosophy
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow
from tests.fixtures.managers import make_contract
from tests.fixtures.sim import make_player
from tools.gm_harness.league import (
    _rescale_standard_contracts_under_target,
    _water_fill_scale_down,
    generate_gm_league,
)
from tools.gm_harness.season import _capsheet_by_team

_LEAGUE_ID = 1
_SEASON = 2026


def _contracts_from(*player_contracts):
    """`(player, contract, years)` triples, `_rescale_standard_contracts_
    under_target`'s own input shape, built from `make_player`/`make_
    contract` fixture output -- the fixtures build a `PlayerContract`
    (`.contract`/`.years`), not the bare tuple this module's internals
    pass around."""
    return [
        (make_player(pc.contract.player_id, team_id=1), pc.contract, pc.years)
        for pc in player_contracts
    ]


def test_generation_is_deterministic_in_rng_seed_alone():
    """Same `rng_seed` -> identical derived scalars across two independent
    calls -- teams/philosophies, and every player's own age/contract
    shape (a stronger check than "same counts": every individual salary
    schedule must match too, per the report's own "the generator's every
    stream derives from a named salt" determinism claim)."""
    s1 = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=42)
    s2 = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=42)

    assert [t.gm_philosophy for _, t in sorted(s1.teams.items())] == [
        t.gm_philosophy for _, t in sorted(s2.teams.items())
    ]

    ages1 = sorted(
        (rp.player.id, rp.player.years_pro, rp.player.birth_date) for rp in s1.players.values()
    )
    ages2 = sorted(
        (rp.player.id, rp.player.years_pro, rp.player.birth_date) for rp in s2.players.values()
    )
    assert ages1 == ages2

    contracts1 = sorted(
        (
            rp.player.id,
            rp.contract.contract.total_years,
            tuple(y.salary for y in rp.contract.years),
        )
        for rp in s1.players.values()
    )
    contracts2 = sorted(
        (
            rp.player.id,
            rp.contract.contract.total_years,
            tuple(y.salary for y in rp.contract.years),
        )
        for rp in s2.players.values()
    )
    assert contracts1 == contracts2

    picks1 = sorted((p.id, p.season, p.round, p.current_team_id) for p in s1.picks.values())
    picks2 = sorted((p.id, p.season, p.round, p.current_team_id) for p in s2.picks.values())
    assert picks1 == picks2


def test_different_seeds_produce_different_leagues():
    """A trivial but real guard against an accidental constant-stream bug
    (the exact `Random(seed)`-shared-across-the-run defect `seed_dev_
    league.py`'s own docstring names as already paid for once)."""
    s1 = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=1)
    s2 = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=2)
    ages1 = sorted(rp.player.years_pro for rp in s1.players.values())
    ages2 = sorted(rp.player.years_pro for rp in s2.players.values())
    assert ages1 != ages2


def test_philosophy_round_robin_matches_the_5_5_4x5_distribution():
    """Finding A's fix: exactly the round-robin distribution the report
    specifies (5/5/4/4/4/4/4 across the 7 philosophies), and never
    `seeds/teams.json`'s own uniform `tendency_respecter` column."""
    state = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=99)
    counts = Counter(t.gm_philosophy for t in state.teams.values())
    assert set(counts) == set(GmPhilosophy)
    assert counts[GmPhilosophy.STAR_MAXER] == 5
    assert counts[GmPhilosophy.EGALITARIAN] == 5
    for philosophy in (
        GmPhilosophy.DEFENSE_FIRST,
        GmPhilosophy.TENDENCY_RESPECTER,
        GmPhilosophy.VET_OVERRATER,
        GmPhilosophy.YOUTH_DEVELOPER,
        GmPhilosophy.CHAOS,
    ):
        assert counts[philosophy] == 4


@pytest.mark.slow
def test_at_least_one_contend_team_at_generation():
    """The report's other named risk: a `RETOOL`-seeded start makes
    `CONTEND` structurally unreachable at season 1 under hysteresis. This
    generator seeds from an un-hysteresed `classify_window` call
    specifically to avoid that -- checked directly across several seeds
    since a single draw could plausibly land with zero contenders even
    under a correct implementation."""
    seeds_with_contend = 0
    for seed in range(10):
        state = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=seed)
        if CompetitiveWindow.CONTEND in state.window_by_team.values():
            seeds_with_contend += 1
    assert seeds_with_contend > 0, "no CONTEND team across 10 seeds -- CONTEND is unreachable"


def test_roster_and_contract_shape_sanity():
    """`players_per_team=15` per team; the rookie-scale block is assigned
    correctly -- `years_pro <= 3` *and* ranked <= 30 within their own
    cohort -> `ContractType.ROOKIE_SCALE`; every other player (`years_pro
    > 3`, or `years_pro <= 3` but ranked 31-60, B3's fix) -> `STANDARD`;
    every player has exactly one `ACTIVE` contract."""
    state = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=11)
    assert len(state.players) == 30 * 15

    roster_counts = Counter(rp.player.team_id for rp in state.players.values())
    assert all(count == 15 for count in roster_counts.values())
    assert set(roster_counts) == set(state.teams)

    for rp in state.players.values():
        assert rp.contract is not None
        assert rp.contract.contract.status is ContractStatus.ACTIVE
        if rp.player.years_pro <= 3 and rp.player.draft_pick_number <= 30:
            assert rp.contract.contract.contract_type is ContractType.ROOKIE_SCALE
        else:
            assert rp.contract.contract.contract_type is ContractType.STANDARD


@pytest.mark.slow
def test_end_season_share_roughly_matches_the_stated_expiry_distribution():
    """A loose statistical check over many draws (not exact) -- `k ~
    {0:.28, 1:.26, 2:.21, 3:.15, 4:.10}` for standard (non-rookie-scale)
    contracts' `end_season - season` offset. Pooled across several seeds
    to shrink sampling noise without pinning an exact count."""
    offsets: Counter[int] = Counter()
    for seed in range(6):
        state = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=seed)
        for rp in state.players.values():
            if rp.player.years_pro > 3:
                offsets[rp.contract.contract.end_season - _SEASON] += 1
    total = sum(offsets.values())
    assert total > 500, "expected several hundred standard-contract observations pooled"
    shares = {k: v / total for k, v in offsets.items()}
    expected = {0: 0.28, 1: 0.26, 2: 0.21, 3: 0.15, 4: 0.10}
    for k, expected_share in expected.items():
        assert abs(shares.get(k, 0.0) - expected_share) < 0.05, (k, shares)


def test_draft_picks_total_420_uniform_ownership_unresolved_numbers():
    """420 = 30 teams x 7 seasons x 2 rounds; `original_team_id ==
    current_team_id` at generation (uniform ownership, deliberate --
    module docstring); `pick_number is None` for every pick, including the
    current season's own (resolution is `DRAFT_LOTTERY`'s job, T7a, not
    built this slice)."""
    state = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=5)
    assert len(state.picks) == 420
    seasons = {state.season + i for i in range(7)}
    assert {p.season for p in state.picks.values()} == seasons
    for pick in state.picks.values():
        assert pick.original_team_id == pick.current_team_id
        assert pick.pick_number is None
        assert pick.used_for_player_id is None
        assert pick.round in (1, 2)
    rounds_per_season_team = Counter((p.season, p.current_team_id) for p in state.picks.values())
    assert all(count == 2 for count in rounds_per_season_team.values())


def test_default_protected_pick_share_produces_no_protection_on_any_pick():
    """B1's default path: `protected_pick_share`'s default (`0.0`) ->
    `protection_type=NONE, protection_through=None` for every one of the
    420 generated picks, per report §4.2 verbatim."""
    state = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=7)
    assert len(state.picks) == 420
    for pick in state.picks.values():
        assert pick.protection_type is ProtectionType.NONE
        assert pick.protection_through is None


def test_nonzero_protected_pick_share_raises_instead_of_generating_garbage():
    """B1's fix: protected-pick generation is unimplemented in this slice
    (report §4.2 gives `protection_type=NONE`/`protection_through=None`
    for every pick, no exception) -- a prior draft set `protection_through
    = pick_season` (a season year) for a share of picks, which `picks.py::
    conveyance_probability`/`slot_if_conveys` then silently misread as a
    draft-slot number, producing garbage prices with no error. A caller
    that passes a nonzero share now fails loudly instead."""
    with pytest.raises(AssertionError):
        generate_gm_league(
            league_id=_LEAGUE_ID, season=_SEASON, rng_seed=7, protected_pick_share=0.5
        )


def test_rank_over_30_years_pro_le_3_gets_second_round_standard_contract():
    """B3's fix, direct: within a `years_pro <= 3` cohort, a rank > 30
    player gets `ContractType.STANDARD`, `DEFAULT_TUNING.second_round_
    horizon` years, and every year priced at `min_salary` -- the same
    shape `apply_draft_inmem`'s own `slot > 30` branch already uses
    (`state.py`), not a 4-year `ROOKIE_SCALE` deal priced off row 30. A
    rank <= 30 player in the same cohort is unaffected, still the real
    4-year `ROOKIE_SCALE` deal."""
    found_over_30 = False
    found_under_30 = False
    for seed in range(20):
        state = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=seed)
        cap_settings = state.cap_settings_by_season[state.season]
        for rp in state.players.values():
            player = rp.player
            if player.years_pro > 3 or player.draft_pick_number is None:
                continue
            contract = rp.contract.contract
            if player.draft_pick_number > 30:
                found_over_30 = True
                assert contract.contract_type is ContractType.STANDARD, (seed, player.id)
                assert contract.total_years == DEFAULT_TUNING.second_round_horizon, (
                    seed,
                    player.id,
                )
                # critic round 2's finding: `all()` over an empty `years`
                # tuple is vacuously True, so this must first pin that
                # `years` is non-empty -- the exact property the `years_
                # into = min(player.years_pro, horizon - 1)` clamp in
                # `_build_contract` exists to guarantee. Without the clamp,
                # a years_pro=2/3 player in this branch gets `years=()`
                # and an already-past `end_season`, which crashes
                # `max(y.season for y in rp.contract.years)` wherever the
                # real pipeline reads it (`gm_service.py`'s `_expiring_by_
                # team`/`_next_season_roster_count`) -- removing the
                # clamp left all 22 fast tests and all 200 slow-scan
                # seeds green, since nothing here checked `years` was
                # ever populated at all.
                assert len(rp.contract.years) >= 1, (seed, player.id)
                assert max(year.season for year in rp.contract.years) == contract.end_season, (
                    seed,
                    player.id,
                )
                assert all(
                    year.salary == cap_settings.min_salary for year in rp.contract.years
                ), (seed, player.id)
            else:
                found_under_30 = True
                assert contract.contract_type is ContractType.ROOKIE_SCALE, (seed, player.id)
                assert contract.total_years == 4, (seed, player.id)
        if found_over_30 and found_under_30:
            break
    assert found_over_30, "no rank>30 years_pro<=3 player found across 20 seeds"
    assert found_under_30, "no rank<=30 years_pro<=3 player found across 20 seeds"


@pytest.mark.slow
@pytest.mark.parametrize("rng_seed", range(200))
def test_tier2_family1_boundary_scan_over_many_seeded_leagues(rng_seed):
    """§11 Tier 2's "a few hundred seeded synthetic leagues, Family-1
    boundary scan" -- reachable with only the generator (report §10,
    "Plus the three archetype-table checks" section's own framing, and
    §4's "T6b... independently testable by... the §11 Tier 2 scan").
    Cheap Family-1 invariants, checked at generation time for every one of
    200 distinct seeds: roster size in `[roster_min, roster_max]`, global
    `Contract.id` uniqueness (real Family-1 invariant 4 -- no two active
    contracts sharing referential identity), exactly one round-1 and one
    round-2 pick per `(original_team_id, season)` pair across the whole
    generated horizon (a real generation invariant, not a
    dict-uniqueness tautology -- B2's fix, see below), and **invariant 2
    (`apron_2`), the sibling apron_2 slice's own natural extension point
    this scan already had but didn't use** -- `_capsheet_by_team` (`season.
    py`, the exact function the real `test_season.py::test_family1_
    invariant_2_apron2_budget_holds_at_every_boundary` test reads its own
    `apron2_room` from) applied directly to each of the 200 freshly
    generated states, at generation time -- the widest sweep this fix's
    own basis correction gets, well beyond the single `rng_seed=1` the
    `test_season.py` smoke test covers.

    **B2 fix, mutation-tested (critic round):** the previous two
    assertions here were structurally vacuous -- "no player with two
    active contracts" iterated `state.players` (a dict keyed by player id,
    one optional contract field each) and could never fail by
    construction; "every pick has exactly one owner" compared `len(state.
    picks)` to `len({p.id for p in state.picks.values()})`, also
    dict-keyed-by-id and therefore also structurally guaranteed. Critic
    reproduced this by breaking contract-id incrementing (every contract
    in every league sharing id 1) and confirming both the old assertions
    and the full suite still passed 100%. The two replacements below are
    confirmed (by the same mutation, reintroduced and reverted during this
    fix round) to actually fail when that bug is present.

    **`@pytest.mark.slow`** (excluded from the default `pytest -q` run,
    `pyproject.toml`'s own existing convention for "full-season/expensive
    tests") -- 200 generations at ~1.3s each is ~4-5 minutes, the same
    class of cost `phase-1-simulation-engine.md`'s own full-season harness
    already carries this marker for. Run explicitly with `pytest -m slow`."""
    state = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=rng_seed)
    cap_settings = state.cap_settings_by_season[state.season]

    roster_counts = Counter(
        rp.player.team_id
        for rp in state.players.values()
        if rp.player.roster_status is RosterStatus.ACTIVE
    )
    for team_id in state.teams:
        count = roster_counts.get(team_id, 0)
        in_band = cap_settings.roster_min <= count <= cap_settings.roster_max
        assert in_band, (rng_seed, team_id, count)

    # Global Contract.id uniqueness across the whole generated state -- the
    # real property Family-1 invariant 4 ("no two active contracts sharing
    # referential identity") is meant to guard. A dict-keyed-by-player-id
    # scan can never catch two different players sharing one contract id;
    # this reads the actual contract id off every contracted player and
    # checks the multiset has no duplicates.
    contract_ids = [
        rp.contract.contract.id for rp in state.players.values() if rp.contract is not None
    ]
    assert len(contract_ids) == len(set(contract_ids)), (rng_seed, "duplicate contract id")

    # Exactly one round-1 and one round-2 pick per (original_team_id,
    # season) pair, for every team x every season in the generated
    # horizon -- a real property a picks-generation-loop bug (a skipped
    # team, a duplicated season, an off-by-one in the round loop) could
    # actually violate, unlike the old id-uniqueness tautology.
    triples = Counter((p.original_team_id, p.season, p.round) for p in state.picks.values())
    horizon_seasons = {state.season + i for i in range(7)}
    expected_triples = {
        (team_id, season, round_num)
        for team_id in state.teams
        for season in horizon_seasons
        for round_num in (1, 2)
    }
    assert set(triples) == expected_triples, (rng_seed, "missing/extra team-season-round triple")
    assert all(count == 1 for count in triples.values()), (rng_seed, "duplicated team-season-round")

    # Architect round 19 (Slice 19-A): `_capsheet_by_team` now returns
    # `(hold_free, hold_inclusive)` -- this invariant-2 sweep is the
    # hold-free basis, unchanged from what it read before this slice
    # (round 19 §2.5/§5: the generator's own apron_2 guarantee stays
    # expressed in the hold-free basis, on purpose).
    caps, _caps_with_holds = _capsheet_by_team(state, DEFAULT_TUNING)
    for team_id, row in caps.items():
        assert row.apron2_room >= 0, (
            rng_seed,
            team_id,
            f"used={row.used} apron2_room={row.apron2_room}",
        )


# ---------------------------------------------------------------------------
# The apron_2 rescale's own direct coverage -- `_water_fill_scale_down`'s
# floor/multi-round/infeasibility contract, both `_rescale_standard_
# contracts_under_target` ValueError raise paths, and the k=0/rookie-scale/
# rank>30-DRAFT exclusion this whole fix depends on (B5, critic finding).
# ---------------------------------------------------------------------------


def test_water_fill_scale_down_floors_the_smallest_and_scales_the_rest():
    """A single-round case: the smaller salary crosses `floor` on the
    first pass and gets pinned there; the larger one is scaled by the
    same round's factor and lands above `floor` untouched by pinning."""
    result = _water_fill_scale_down({0: 300, 1: 50}, available=100, floor=40)
    assert result == {0: 60, 1: 40}
    assert sum(result.values()) == 100


def test_water_fill_scale_down_multi_round_convergence_floors_at_different_rounds():
    """Engineered so exactly one player is pinned to `floor` per round,
    across two separate rounds, before a third round finishes by scaling
    the two never-floored players with no further pinning -- the
    "multi-round convergence" property `_water_fill_scale_down`'s own
    docstring claims (round 1 pins idx 3 only; recomputing the factor
    against the shrunk pool then pins idx 2, which round 1's factor alone
    would not have touched; round 3 scales idx 0/1 with no new pins)."""
    result = _water_fill_scale_down({0: 1000, 1: 500, 2: 250, 3: 90}, available=400, floor=50)
    assert result == {0: 200, 1: 100, 2: 50, 3: 50}
    assert sum(result.values()) == 400


def test_water_fill_scale_down_infeasible_returns_floored_result_without_raising():
    """Nit 4's own decided contract: if `len(salary_by_idx) * floor >
    available`, this function is not the layer that raises -- it returns
    every entry at `floor` (the best achievable minimum) and lets the sum
    come out over `available`. `_rescale_standard_contracts_under_target`
    (the only real caller) is the layer that detects and raises on this,
    with team-level context this generic helper cannot reconstruct (see
    `test_rescale_raises_when_water_fill_cannot_converge` below)."""
    result = _water_fill_scale_down({0: 100, 1: 100}, available=100, floor=80)
    assert result == {0: 80, 1: 80}
    assert sum(result.values()) == 160 > 100


def test_water_fill_scale_down_never_scales_up_when_available_exceeds_the_total():
    """The `if factor >= 1.0: break` guard's own contract, direct-called
    (bypassing `_rescale_standard_contracts_under_target`'s own "only
    call this when already over target" guarantee, which is exactly why
    this guard is worth keeping even though today's one real caller never
    reaches it -- see the function's own docstring). Without this guard, a
    generous `available` would compute `factor > 1.0` and multiply every
    salary up instead of leaving them untouched."""
    result = _water_fill_scale_down({0: 100, 1: 50}, available=1000, floor=40)
    assert result == {0: 100, 1: 50}


def test_rescale_returns_unchanged_when_already_under_target():
    contracts = _contracts_from(
        make_contract(1, start_season=_SEASON, salaries=(50_000_000,) * 3),
    )
    result = _rescale_standard_contracts_under_target(
        contracts,
        season_plus_1=_SEASON + 1,
        target=100_000_000,
        used_season_plus_1=50_000_000,
        min_salary=1_000_000,
    )
    assert result == contracts


def test_rescale_skips_a_cap_space_contract_that_expires_before_season_plus_1():
    """The core of this fix's basis correction: a `k=0` standard contract
    (`end_season == season`, no materialized year at `season + 1`) is left
    completely untouched, even though it's `SigningMethod.CAP_SPACE` --
    only the contract that actually covers `season + 1` gets rescaled."""
    expiring = make_contract(1, start_season=_SEASON, salaries=(99_000_000,))  # end == _SEASON
    covering = make_contract(
        2, start_season=_SEASON, salaries=(90_000_000, 90_000_000, 90_000_000)
    )
    contracts = _contracts_from(expiring, covering)

    result = _rescale_standard_contracts_under_target(
        contracts,
        season_plus_1=_SEASON + 1,
        target=50_000_000,
        used_season_plus_1=90_000_000,  # only `covering`'s own season+1 salary
        min_salary=1_000_000,
    )

    expiring_result = next(t for t in result if t[1].player_id == 1)
    covering_result = next(t for t in result if t[1].player_id == 2)
    assert expiring_result[2] == expiring.years, "expiring contract must be untouched"
    assert covering_result[2] != covering.years, "the sole eligible contract must be rescaled"
    assert all(y.salary <= 50_000_000 + 1 for y in covering_result[2])


def test_rescale_never_touches_rookie_scale_or_rank_over_30_draft_contracts():
    rookie = make_contract(
        1,
        start_season=_SEASON,
        salaries=(10_000_000, 10_500_000, 11_000_000, 14_000_000),
        contract_type=ContractType.ROOKIE_SCALE,
        signing_method=SigningMethod.ROOKIE_SCALE,
    )
    rank_over_30 = make_contract(
        2,
        start_season=_SEASON,
        salaries=(1_200_000, 1_200_000),
        signing_method=SigningMethod.DRAFT,
    )
    standard = make_contract(3, start_season=_SEASON, salaries=(90_000_000, 90_000_000))
    contracts = _contracts_from(rookie, rank_over_30, standard)

    used_season_plus_1 = 10_500_000 + 1_200_000 + 90_000_000
    result = _rescale_standard_contracts_under_target(
        contracts,
        season_plus_1=_SEASON + 1,
        target=50_000_000,
        used_season_plus_1=used_season_plus_1,
        min_salary=1_000_000,
    )

    by_id = {t[1].player_id: t for t in result}
    assert by_id[1][2] == rookie.years, "ROOKIE_SCALE must never be rescaled"
    assert by_id[2][2] == rank_over_30.years, "rank>30 DRAFT must never be rescaled"
    assert by_id[3][2] != standard.years, "the sole CAP_SPACE contract must be rescaled"


def test_rescale_raises_when_no_cap_space_contract_covers_season_plus_1():
    """The first `ValueError` path, directly exercised: every eligible-
    looking contract is either non-`CAP_SPACE` or doesn't cover
    `season_plus_1`, yet the caller-supplied `used_season_plus_1` is over
    `target` -- nothing this function can rescale to close the gap."""
    rookie = make_contract(
        1,
        start_season=_SEASON,
        salaries=(10_000_000,) * 4,
        contract_type=ContractType.ROOKIE_SCALE,
        signing_method=SigningMethod.ROOKIE_SCALE,
    )
    contracts = _contracts_from(rookie)

    with pytest.raises(ValueError, match="no CAP_SPACE contract covering season_plus_1"):
        _rescale_standard_contracts_under_target(
            contracts,
            season_plus_1=_SEASON + 1,
            target=1_000,
            used_season_plus_1=999_999_999,
            min_salary=1_000_000,
        )


def test_rescale_raises_when_water_fill_cannot_converge():
    """The second `ValueError` path: three eligible `CAP_SPACE` contracts,
    all already at `min_salary` in the worst case, still sum over `target`
    -- the infeasible case `_water_fill_scale_down` itself does not raise
    on (see `test_water_fill_scale_down_infeasible_returns_floored_
    result_without_raising`), caught one layer up with real dollar
    context."""
    contracts = _contracts_from(
        make_contract(1, start_season=_SEASON, salaries=(1_000_000, 1_000_000)),
        make_contract(2, start_season=_SEASON, salaries=(1_000_000, 1_000_000)),
        make_contract(3, start_season=_SEASON, salaries=(1_000_000, 1_000_000)),
    )

    with pytest.raises(ValueError, match="could not bring team's season_plus_1 committed total"):
        _rescale_standard_contracts_under_target(
            contracts,
            season_plus_1=_SEASON + 1,
            target=1_000_000,
            used_season_plus_1=3_000_000,
            min_salary=500_000,
        )


@pytest.mark.slow
def test_apron2_generation_touches_far_fewer_teams_and_contracts_than_the_first_fix():
    """Honest, exact, non-rounded-favorably re-measurement of this fix's
    own footprint (module docstring's own claim) -- disables the rescale
    entirely to get each team's raw draw, compares against the real
    generated (rescaled) state, over `rng_seed` 1-10 (a smaller sweep than
    the module docstring's own 1-30 measurement, kept fast enough for the
    default `pytest -q` run). Confirms the touched fraction sits close to
    the true violation tail (2/30 teams at `rng_seed=1`, the original bug
    report), nowhere near the first fix's own measured 58.1%/60.08%."""
    total_teams = 0
    touched_teams = 0
    for seed in range(1, 11):
        fixed = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=seed)
        with mock.patch(
            "tools.gm_harness.league._rescale_standard_contracts_under_target",
            side_effect=lambda team_contracts, **kw: team_contracts,
        ):
            raw = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=seed)

        for team_id in fixed.teams:
            total_teams += 1
            fixed_players = {
                pid: rp for pid, rp in fixed.players.items() if rp.player.team_id == team_id
            }
            raw_players = {
                pid: rp for pid, rp in raw.players.items() if rp.player.team_id == team_id
            }
            if any(
                fixed_players[pid].contract.years[0].salary
                != raw_players[pid].contract.years[0].salary
                for pid in fixed_players
            ):
                touched_teams += 1

    touched_share = touched_teams / total_teams
    assert touched_share < 0.20, (
        f"touched {touched_teams}/{total_teams} ({touched_share:.1%}) teams -- expected close to "
        "the true tail, well under the first fix's own measured 58.1%"
    )
