"""§10.1's three retuned minutes-tier bands (Decision S7-1b,
docs/design/phase-1-step-7-calibration.md §10.1, §12 step 6's own verify
list) -- "the three §10.1 tier-mean bands hold on a seeded 14-man league" --
plus the RC-4 per-rank curve's own acceptance criteria
(`docs/design/auto-gm-role-construction.md` RC-10.1, revision 2, 2026-09-23).

`_minutes_shares` is deterministic and roster-size-driven (no RNG), so a
single no-role roster at each N is enough to pin the exact realized values
RC-4.3's table states, not just a loose band -- the band is asserted too,
since that is the number a future retune's regression test should read.

**Pre-RC-4 state, recorded rather than silently dropped:** the flat
5/3/rest tier model (31.0/22.0/3.5 raw) normalized a 14-man roster to
30.74/21.82/3.47. RC-4 replaced it with a 15-entry per-absolute-rank curve;
the point targets below are the curve's own measured output, and the three
*bands* -- copied from a closed critic-reviewed decision
(`nba_reference_distributions.json`'s own `source`) -- are unchanged.
"""
from __future__ import annotations

import pytest

from dba2.domain.player import Position
from dba2.sim.context import build_sim_team
from dba2.sim.game import _MINUTES_CURVE, _minutes_curve_raw, _minutes_shares
from dba2.sim.tuning import DEFAULT_TUNING
from tests.fixtures.sim import make_sim_player, make_team, make_team_strategy

_POSITIONS = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)

# §10.1's committed tier-mean bands -- a closed decision, does not move.
_STARTER_BAND = (28.5, 33.0)
_ROTATION_BAND = (19.0, 24.0)
_BENCH_BAND = (2.0, 6.0)

# RC-4.3's measured *target* point values at N=14 (not the old 30.74/21.82/
# 3.47 -- those were the flat-tier model's targets, pre-RC-4).
_STARTER_TARGET = 30.92
_ROTATION_TARGET = 21.00
_BENCH_TARGET = 3.733

# RC-4.3's N-sweep target table (Σ raw / f(N) / tier means), N=10..15.
# Roster sizes 12-15 are legal (`seeds/cap_settings.json:18-19`); 10-11 are
# included only for the rotation-pool-size and monotonicity guards, which
# RC-4.1 states hold at every N the sim can produce, legal or transient.
_LEGAL_ROSTER_SIZES = (12, 13, 14, 15)

# RC-15's selection quantum: `_select_lineup` cannot realize a target below
# roughly one block-per-period (~0.97 mpg); RC-4's curve bottoms out at 0.8
# raw (rank 15) for exactly this reason, which holds the *target* close to
# that floor at every N in 12-14. **Measured finding, not in the design
# doc's own table:** at N=15 the normalizer `f(15) = 0.99668` pulls rank
# 15's raw 0.8 down to a target of 0.797 -- the doc's RC-10.1 acceptance
# bullet states "every target >= 0.9 mpg," which is off by ~0.1 mpg at this
# one cell. 0.75 is the real floor this table produces at every legal N (a
# minimum *target* assertion threshold below the realized RC-15 quantum,
# not the quantum itself); RC-15's quantum still rounds that 0.797 target
# up to ~1 realized block regardless, so the design's actual guarantee (no
# *realized* exposure below ~1 mpg) is unaffected -- only the aspirational
# target-side number in the doc was slightly optimistic at the one extra
# rank N=15 exercises.
_MIN_RAW_TARGET_FLOOR = 0.75

# `tools/validate_sim.py:83`'s own rotation-pool cutoff -- cited here so a
# future anchor-table sweep cannot silently break `p10/p50/p90_ppg_rotation`
# by moving rank 8 or rank 9 across it (RC-4.3/RC-10.1).
_ROTATION_MPG_THRESHOLD = 15.0
_ROTATION_POOL_SIZE = 8


def _no_role_team(n: int):
    """`n` players, no `PlayerRole` rows (every player falls through to
    `_minutes_shares`' curve branch), distinct descending `overall_cached`
    so the rank sort has a real order to work with -- same distinct-value
    discipline `tests/fixtures/sim.py` documents for its own roster knobs,
    kept local to this module rather than widening the shared 10-player
    fixture (RC-10.1: not touched by this commit)."""
    players = tuple(
        make_sim_player(
            100 + i,
            tuning=DEFAULT_TUNING,
            primary_position=_POSITIONS[i % len(_POSITIONS)],
            overall_cached=90 - 2 * i,
            with_role=False,
        )
        for i in range(n)
    )
    team = make_team(1)
    strategy = make_team_strategy(1)
    return build_sim_team(team, strategy, players)


def _fourteen_man_no_role_team():
    return _no_role_team(14)


def _tier_means(team) -> tuple[float, float, float]:
    shares = _minutes_shares(team)
    minutes_by_id = {pid: share * 240.0 for pid, share in shares.items()}
    ranked_ids = [p.id for p in sorted(team.players, key=lambda p: -minutes_by_id[p.id])]
    n = len(ranked_ids)
    starter_minutes = [minutes_by_id[pid] for pid in ranked_ids[:5]]
    rotation_minutes = [minutes_by_id[pid] for pid in ranked_ids[5:8]]
    bench_minutes = [minutes_by_id[pid] for pid in ranked_ids[8:n]]
    starter_mean = sum(starter_minutes) / len(starter_minutes)
    rotation_mean = sum(rotation_minutes) / len(rotation_minutes)
    bench_mean = sum(bench_minutes) / len(bench_minutes) if bench_minutes else 0.0
    return starter_mean, rotation_mean, bench_mean


def test_ranks_one_through_five_still_sum_to_154_6():
    """docs/design/per-36-spread-compression.md SC-8.4's own named guard:
    a future anchor re-solve (M1/M2/whatever comes after) can freely move
    the individual rank-1..5 raw values -- SC-7 Commit 4 already did, twice
    (36.0 screened, 35.5 shipped) -- but must not silently change their
    *sum*, since `starter_mpg`'s mean-preservation guarantee
    (`test_fourteen_man_league_tier_means_match_section_10_1` below) is
    proved by construction from this one identity, not re-derived from the
    individual raw values."""
    assert sum(_MINUTES_CURVE[:5]) == pytest.approx(154.6, abs=1e-9)


def test_fourteen_man_league_tier_means_match_section_10_1():
    team = _fourteen_man_no_role_team()
    starter_mean, rotation_mean, bench_mean = _tier_means(team)

    assert starter_mean == pytest.approx(_STARTER_TARGET, abs=0.01)
    assert rotation_mean == pytest.approx(_ROTATION_TARGET, abs=0.01)
    assert bench_mean == pytest.approx(_BENCH_TARGET, abs=0.01)

    assert _STARTER_BAND[0] <= starter_mean <= _STARTER_BAND[1]
    assert _ROTATION_BAND[0] <= rotation_mean <= _ROTATION_BAND[1]
    assert _BENCH_BAND[0] <= bench_mean <= _BENCH_BAND[1]

    # Every rostered player's *target* share is positive -- a 14th man at
    # ~3.7 mpg can legitimately DNP in a given simulated game (bench-tier
    # rank-selection variance), but the target itself is never zero.
    shares = _minutes_shares(team)
    assert all(v > 0 for v in shares.values())


def test_team_minutes_sum_to_240_at_fourteen_players():
    """The Tier-2 identity (`team minutes sum to 240 + 25*overtime_periods`)
    holds for `_minutes_shares`' targets at N=14, not just the 10-player
    fixture's own coverage."""
    team = _fourteen_man_no_role_team()
    shares = _minutes_shares(team)
    assert sum(shares.values()) == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("n", _LEGAL_ROSTER_SIZES)
def test_all_three_tier_means_stay_in_band_at_every_legal_roster_size(n):
    """RC-4.1's B1 guard: `roster_min: 14`, `roster_max: 15`
    (`seeds/cap_settings.json:18-19`), and N=13/12 besides -- the range
    reachable when `game_sim_service.build_context` filters to
    `RosterStatus.ACTIVE`. All three bands must hold at every N in that
    range (RC-4.3's own sweep proves it; this test is the regression
    guard)."""
    team = _no_role_team(n)
    starter_mean, rotation_mean, bench_mean = _tier_means(team)
    assert _STARTER_BAND[0] <= starter_mean <= _STARTER_BAND[1], (n, starter_mean)
    assert _ROTATION_BAND[0] <= rotation_mean <= _ROTATION_BAND[1], (n, rotation_mean)
    assert _BENCH_BAND[0] <= bench_mean <= _BENCH_BAND[1], (n, bench_mean)


@pytest.mark.parametrize("n", _LEGAL_ROSTER_SIZES)
def test_the_rotation_pool_is_exactly_eight_players_at_every_legal_roster_size(n):
    """RC-4.3's B2 guard, and the most important test in this file: exactly
    8 ranks clear `tools/validate_sim.py:83`'s `_ROTATION_MPG_THRESHOLD`
    (15.0 mpg) -- rank 8 above, rank 9 below -- at every legal roster size.
    A future anchor-table sweep that silently drifts rank 8 or rank 9 across
    that line changes `p90/p50/p10_ppg_rotation`'s pool size, not just its
    composition."""
    team = _no_role_team(n)
    shares = _minutes_shares(team)
    minutes = sorted(shares.values(), reverse=True)
    rotation_size = sum(1 for m in minutes if m * 240.0 >= _ROTATION_MPG_THRESHOLD)
    assert rotation_size == _ROTATION_POOL_SIZE, (n, [m * 240.0 for m in minutes])


@pytest.mark.parametrize("n", (12, 13, 14, 15, 16))
def test_minutes_curve_is_strictly_decreasing_in_rank(n):
    """No ties, no inversions -- N=16 is included (one past the legal
    `roster_max` of 15) specifically to exercise RC-4.2's beyond-table
    geometric-decay branch."""
    raws = [_minutes_curve_raw(i) for i in range(1, n + 1)]
    assert raws == sorted(raws, reverse=True)
    assert len(set(raws)) == len(raws)


@pytest.mark.parametrize("n", _LEGAL_ROSTER_SIZES)
def test_no_rank_is_targeted_below_the_selection_quantum(n):
    """RC-15's guard: `_select_lineup` seats every unplayed player at least
    once per period (~14.5s per possession-block, ~0.97 mpg minimum
    realizable exposure, RC-15.1) regardless of how small his target is.
    Targeting anyone below that quantum just donates unpredictable minutes
    upward on realization; RC-4's curve bottoms out at 0.8 raw (rank 15)
    for exactly this reason. Asserted on the normalized *target*, not the
    raw table, since normalization is what a caller actually reads."""
    team = _no_role_team(n)
    shares = _minutes_shares(team)
    minutes = [v * 240.0 for v in shares.values()]
    assert min(minutes) >= _MIN_RAW_TARGET_FLOOR, (n, minutes)
