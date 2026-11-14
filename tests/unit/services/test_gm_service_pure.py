"""`services/gm_service.py`'s pure plan layer -- Phase 2 step 10, slice T3's
own verify list (docs/design/phase-2-manager-minds.md §12 step 10).

`run_trade_window`'s selection/cap-enforcement tests monkeypatch `propose_
packages` and `trade.evaluate_trade`/`trade._check_legality` rather than
relying on the real trade formula to happen to produce a specific mutual-
accept shape -- the formula itself is already exhaustively covered by
`tests/unit/managers/decisions/test_trade.py`; these tests isolate `run_
trade_window`'s own selection/cap logic from that formula's numeric
behavior, which is the more direct and less brittle way to prove the caps
actually bind.

**Critic round 17's fix-round additions, below.** Two of the fully-
monkeypatched tests above left a real gap (critic's Nit 1): nothing
exercised `run_trade_window` against the REAL `trade.evaluate_trade`/
`trade._check_legality` at once, so a bug in how the two compose (B2:
the post-execution legality re-check reading a stale, never-updated room
figure across a pass) went undetected. The tests below close that gap
deliberately: `test_run_trade_window_executes_a_genuine_mutually_
beneficial_trade_under_real_formulas` leaves both real; the B2 regression
leaves `_check_legality` real too (only `evaluate_trade` is forced to
"accept," since B2 is about the legality gate composing correctly across
a pass, not about value-formula correctness, which `test_trade.py` already
owns). `run_free_agency`'s B1/B1b regression and the variant-2 pick
coverage (Nit 2) also use real formulas throughout -- neither needs
monkeypatching to pin down deterministically.
"""
from __future__ import annotations

import dataclasses
import functools
import json
import random
import time
from collections.abc import Mapping

import pytest

from dba2.domain.draft import DraftPick, ProtectionType
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.team import GmPhilosophy
from dba2.managers.capsheet import CapSheetTeamSeasonInputs, compute_team_season_cap_sheet
from dba2.managers.explain import DecisionRecord, ReasonTerm
from dba2.managers.needs import build_need_matrix
from dba2.managers.negotiation import counter_band
from dba2.managers.snapshot import (
    AssetBundle,
    RosterPlayer,
    build_league_snapshot,
    build_roster_player,
    build_team_snapshot,
)
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow
from dba2.services import gm_service
from dba2.services.gm_service import (
    MAX_AI_OFFERS_PER_HUMAN_TEAM_PER_WINDOW,
    MAX_TRADES_PER_TEAM_PER_WINDOW,
    MAX_TRADES_PER_WINDOW,
    TRADE_REQUEST_PRESSURE_BOOST_CAP,
    CandidatePackage,
    GmContext,
    PendingOffer,
    ValuationCache,
    _boosted_target_rank_value,
    classify_all_windows,
    encode_decision_record,
    propose_packages,
    run_draft_phase,
    run_free_agency,
    run_resign_window,
    run_trade_window,
    run_waivers,
)
from tests.fixtures.managers import CAP_SETTINGS, ROOKIE_SCALE, make_contract, make_roster_player
from tests.fixtures.sim import make_team

_SEASON = 2026
_RNG_SEED = 2026_10
_LEAGUE_ID = 1
_WINDOWS_CYCLE: tuple[CompetitiveWindow, ...] = (
    CompetitiveWindow.CONTEND,
    CompetitiveWindow.WIN_NOW,
    CompetitiveWindow.RETOOL,
    CompetitiveWindow.REBUILD,
)


# ---------------------------------------------------------------------------
# Shared fixtures -- a small, real (non-monkeypatched) synthetic league
# ---------------------------------------------------------------------------


def _filler_roster(team_id: int, count: int, *, avoid_position: Position, base_id: int):
    """`count` unremarkable players, none at `avoid_position` -- mirrors
    `test_trade.py`'s own helper of the same name/shape."""
    positions = [p for p in Position if p is not avoid_position]
    roster = []
    for i in range(count):
        roster.append(
            make_roster_player(
                base_id + i, age=26 + (i % 4), season=_SEASON, overall_cached=64 + (i % 8),
                team_id=team_id, primary_position=positions[i % len(positions)],
                peak_age_start=27, peak_age_end=31,
                contract=make_contract(
                    base_id + i, team_id=team_id, start_season=_SEASON, salaries=(3_000_000,)
                ),
            )
        )
    return tuple(roster)


def _make_pick(
    pick_id: int,
    *,
    season: int,
    original_team_id: int,
    current_team_id: int,
    pick_number: int | None,
    round_: int = 1,
    conveyed: bool = False,
) -> DraftPick:
    """Mirrors `test_trade.py::_make_pick`'s own shape -- kept local rather
    than imported cross-test-module, per this codebase's existing
    per-file-fixture convention."""
    return DraftPick(
        id=pick_id,
        league_id=_LEAGUE_ID,
        season=season,
        round=round_,
        original_team_id=original_team_id,
        current_team_id=current_team_id,
        pick_number=pick_number,
        used_for_player_id=None,
        protection_type=ProtectionType.NONE,
        protection_through=None,
        swap_with_pick_id=None,
        conveyed=conveyed,
        rolls_over_to_season=None,
    )


def _build_team_snapshot(
    team_id: int, *, avoid_position: Position, base_id: int, roster_size: int = 12
):
    team = make_team(team_id)
    roster = _filler_roster(
        team_id, roster_size, avoid_position=avoid_position, base_id=base_id
    )
    return build_team_snapshot(team, roster, (), CAP_SETTINGS)


def _make_small_league(n_teams: int = 4, *, roster_size: int = 12):
    """`roster_size` defaults to 12 -- deliberately below `CAP_SETTINGS.
    roster_min` (14), which is what makes `enforce_roster_floor`'s own test
    scenario reachable without a second, larger league fixture."""
    positions = list(Position)
    teams = tuple(
        _build_team_snapshot(
            i + 1, avoid_position=positions[i % len(positions)], base_id=1000 * (i + 1),
            roster_size=roster_size,
        )
        for i in range(n_teams)
    )
    return build_league_snapshot(_LEAGUE_ID, _SEASON, teams)


def _make_ctx(
    snapshot,
    *,
    phase: Phase = Phase.TRADE_DEADLINE,
    room: int = 20_000_000,
    apron1_room: int = CAP_SETTINGS.apron_1,
    retirement_age: int | None = None,
    room_for_signings_by_team: Mapping[int, int] | None = None,
) -> GmContext:
    """`apron1_room` (slice 12-A, round 11 §5.3) defaults to the fixture's
    full `apron_1` -- generous enough that the Bird bucket's own ceiling
    never becomes the binding constraint in any test that does not
    deliberately override it, matching this helper's existing `room`
    default's own "generous unless overridden" convention. `retirement_
    age` (Decision P2-37) defaults to `None`, matching `GmContext`'s own
    default -- every existing caller of this helper sees zero behaviour
    change. `room_for_signings_by_team` (Decision P2-39, architect round
    21) also defaults to `None`, the identical "unset means behave exactly
    as before" convention -- callers that want the hold-aware basis pass
    their own mapping explicitly."""
    team_ids = [t.id for t in snapshot.teams]
    philosophy_by_team = {tid: GmPhilosophy.TENDENCY_RESPECTER for tid in team_ids}
    window_by_team = {
        tid: _WINDOWS_CYCLE[i % len(_WINDOWS_CYCLE)] for i, tid in enumerate(team_ids)
    }
    room_by_team = {tid: room for tid in team_ids}
    apron1_room_by_team = {tid: apron1_room for tid in team_ids}
    next_season_cap_settings = dataclasses.replace(CAP_SETTINGS, season=CAP_SETTINGS.season + 1)
    return GmContext(
        league_id=_LEAGUE_ID,
        season=_SEASON,
        phase=phase,
        league_rng_seed=_RNG_SEED,
        cap_settings=CAP_SETTINGS,
        next_season_cap_settings=next_season_cap_settings,
        rookie_scale=ROOKIE_SCALE,
        room_by_team=room_by_team,
        apron1_room_by_team=apron1_room_by_team,
        philosophy_by_team=philosophy_by_team,
        window_by_team=window_by_team,
        retirement_age=retirement_age,
        room_for_signings_by_team=room_for_signings_by_team,
    )


def _make_prospect(player_id: int, *, age: int, overall: int):
    rp = make_roster_player(
        player_id, age=age, season=_SEASON, overall_cached=overall, team_id=None,
        primary_position=Position.SG, peak_age_start=26, peak_age_end=30,
    )
    player = dataclasses.replace(rp.player, roster_status=RosterStatus.DRAFT_ELIGIBLE, team_id=None)
    return build_roster_player(player, rp.ratings, contract=None)


def _summarize_packages(packages: tuple[CandidatePackage, ...]):
    return tuple(
        (
            c.team_a_id,
            c.team_b_id,
            tuple(sorted(rp.id for rp in c.incoming_a.players)),
            tuple(sorted(rp.id for rp in c.outgoing_a.players)),
            tuple(sorted(p.id for p in c.outgoing_a.picks)),
        )
        for c in packages
    )


# ---------------------------------------------------------------------------
# _boosted_target_rank_value -- critic nit 1 fix (P4-17's trade-request boost)
# ---------------------------------------------------------------------------


def test_boosted_target_rank_value_no_pressure_returns_value_unchanged():
    """`pressure is None` -- no bound request, or `trade_request_pressure_
    by_player` absent entirely -- must be a strict no-op, byte-for-byte
    identical to `-fv_a[rp.id]`'s pre-Phase-4 ranking."""
    assert _boosted_target_rank_value(12.5, None) == 12.5
    assert _boosted_target_rank_value(-7.25, None) == -7.25


def test_boosted_target_rank_value_boosts_a_positive_value_upward():
    value = _boosted_target_rank_value(10.0, pressure=100.0)
    assert value == pytest.approx(10.0 * (1.0 + TRADE_REQUEST_PRESSURE_BOOST_CAP))
    assert value > 10.0


def test_boosted_target_rank_value_moves_a_negative_value_toward_zero_not_away():
    """Critic nit 1: the un-fixed `value *= 1.0 + boost` form made a
    negative `fv_a` MORE negative under a bound trade request, which
    ranks that player as LESS likely to be targeted -- backwards from
    Decision P4-17's own intent that a trade request should make its own
    player surface as a target more readily, regardless of sign. The
    fixed form must move a negative value toward zero (a smaller
    magnitude), the same "more likely to be picked as a target"
    direction the positive branch already moves in."""
    base = -10.0
    boosted = _boosted_target_rank_value(base, pressure=100.0)
    assert boosted == pytest.approx(base * (1.0 - TRADE_REQUEST_PRESSURE_BOOST_CAP))
    assert boosted > base, (
        "a maximal trade request must not make a negative-value player rank worse"
    )
    assert boosted < 0.0, "the boost must not overshoot past zero at the boost cap"


def test_boosted_target_rank_value_zero_pressure_is_a_no_op():
    assert _boosted_target_rank_value(10.0, pressure=0.0) == 10.0
    assert _boosted_target_rank_value(-10.0, pressure=0.0) == -10.0


# ---------------------------------------------------------------------------
# propose_packages -- determinism
# ---------------------------------------------------------------------------


def test_propose_packages_is_deterministic_at_a_fixed_seed():
    snapshot = _make_small_league()
    ctx = _make_ctx(snapshot)

    result1 = propose_packages(1, 2, snapshot, ctx, gm_service.ValuationCache())
    result2 = propose_packages(1, 2, snapshot, ctx, gm_service.ValuationCache())

    assert len(result1) > 0
    assert _summarize_packages(result1) == _summarize_packages(result2)


def test_propose_packages_never_offers_more_than_the_cap_of_outgoing_players():
    """§6 item 2 (architect round 10 blast-radius pass). Pre-round-10, `N`
    on this fixture was almost always 1 (matching fired on the proposer's
    first, cheapest player); post-round-10 the value bar pushes it higher
    instead -- measured `N = 2` for both of this fixture's own candidates
    (critic round 42 nit: not "usually the cap," corrected here to the
    measured fact) -- so the companion `N > 1` assertion below is what
    keeps this test non-tautological rather than a `<= 3` check that would
    pass identically against a constructor that always returns a single
    player."""
    snapshot = _make_small_league()
    ctx = _make_ctx(snapshot)
    result = propose_packages(1, 2, snapshot, ctx, gm_service.ValuationCache())
    assert result  # sanity: this pair actually generates candidates
    for candidate in result:
        assert len(candidate.outgoing_a.players) <= 3
    assert any(len(c.outgoing_a.players) > 1 for c in result), (
        "fixture must actually exercise N > 1 for this test to mean anything "
        "post-round-10 (value-targeted construction)"
    )


# ---------------------------------------------------------------------------
# propose_packages / _build_throw_ins -- roster-neutral throw-ins (round 9,
# docs/ledger/reports/phase-2-architect-round-9.md §5). The N-for-1 shape
# (`incoming_a.players` always length 1) is exactly what this fix replaces:
# the proposer now gets `N - 1` roster-neutral bodies back from the
# partner, so `len(incoming_a.players) == len(outgoing_a.players)` always.
# ---------------------------------------------------------------------------


def _salary_player(player_id: int, salary: int, *, team_id: int) -> RosterPlayer:
    """A single unremarkable player at a stated `next_season`-covering
    salary -- local to this section since `_build_throw_ins` is a pure
    function of caller-supplied, already-ordered sequences, so its own
    tests need full control over each candidate's `salary_next`
    independent of the real `fit_value` formula (the caller does the
    sorting; these tests hand-order `b_players_ascending` directly rather
    than deriving it from a real `ValuationCache`)."""
    return make_roster_player(
        player_id, age=27, season=_SEASON, overall_cached=70, team_id=team_id,
        primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            player_id, team_id=team_id, start_season=_SEASON, salaries=(salary, salary),
        ),
    )


def _player(player_id: int, *, overall: int, salary: int, team_id: int) -> RosterPlayer:
    """Same shape as `_salary_player`, with a caller-controlled `overall`
    -- §11 step 13's probe-exactness test needs a genuinely stronger real
    player (not an identically-rated one) so the real `ValuationCache.
    fit_value_for` formula assigns it a meaningfully different value."""
    return make_roster_player(
        player_id, age=27, season=_SEASON, overall_cached=overall, team_id=team_id,
        primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            player_id, team_id=team_id, start_season=_SEASON, salaries=(salary, salary),
        ),
    )


def _expiring_player(player_id: int, salary: int, *, team_id: int) -> RosterPlayer:
    """Same shape as `_salary_player`, but a 1-year contract covering only
    `_SEASON` -- `_occupies_next_season(rp, _SEASON + 1)` is `False` for
    this player (its last covered year, `_SEASON`, equals `next_season -
    1`), the "expiring body" round 12's throw-in fix exists to stop
    `_build_throw_ins` from preferring by construction (`salary_next`
    returns `0` for it, same as `_salary_player`'s multi-year players
    return a real dollar figure once their contract stops covering `next_
    season` -- this helper isolates the occupancy question from the
    salary question, since `salary_next` alone cannot distinguish
    "occupies but happens to be cheap" from "doesn't occupy at all")."""
    return make_roster_player(
        player_id, age=27, season=_SEASON, overall_cached=70, team_id=team_id,
        primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            player_id, team_id=team_id, start_season=_SEASON, salaries=(salary,),
        ),
    )


def _make_roster_neutral_fixture():
    """A proposer whose 5 cheap ($1M) players force `_build_outgoing_
    variant1` to cap out at `_MAX_OUTGOING_PLAYERS = 3` against a partner
    whose 2 targets each cost $20M -- every candidate this fixture produces
    has `N = 3`, a real, non-trivial roster-neutrality case (unlike `_make_
    small_league`, where `N` is smaller and the headline invariant would be
    checked only in a more degenerate form).

    **§6 item 3 (architect round 10 blast-radius pass).** Pre-round-10, the
    reason `N == 3` here was "matching never triggers before the cap
    does" -- a fact about `_build_outgoing_variant1`'s old salary-matching
    stop condition, which no longer exists. Post-round-10, `N == 3` still
    holds on this fixture (verified: identical `fv` values across the
    proposer's 5 players tie-break on `id`, so both the ratio and delivery
    orderings agree with the old ascending-`fv_A` order and every $20M
    target is far out of reach of any 1-or-2-player combination of $1M
    proposer bodies -- the value bar never clears, so the constructor
    exhausts its best-effort cap of 3 either way) -- but `== 3` is now
    incidental to that specific salary ratio, not structural to the
    algorithm the way it was before. The real, structural invariant this
    fixture exists to check is roster-neutrality (`len(incoming_a.players)
    == len(outgoing_a.players)`), not the literal count -- see the
    relaxed `>= 2` assertion below."""
    proposer_roster = tuple(
        _salary_player(1000 + i, 1_000_000, team_id=1) for i in range(5)
    )
    proposer = build_team_snapshot(make_team(1), proposer_roster, (), CAP_SETTINGS)

    target_players = tuple(
        make_roster_player(
            2000 + i, age=27, season=_SEASON, overall_cached=90, team_id=2,
            primary_position=Position.C, peak_age_start=27, peak_age_end=31,
            contract=make_contract(
                2000 + i, team_id=2, start_season=_SEASON, salaries=(20_000_000, 20_000_000),
            ),
        )
        for i in range(2)
    )
    throw_in_pool = tuple(
        _salary_player(2100 + i, 1_000_000 + i * 200_000, team_id=2) for i in range(6)
    )
    partner = build_team_snapshot(make_team(2), target_players + throw_in_pool, (), CAP_SETTINGS)

    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (proposer, partner))
    return snapshot, _make_ctx(snapshot)


def test_propose_packages_incoming_a_stays_roster_neutral_with_outgoing_a():
    """§5.6 item 1 -- replaces this file's former `len(incoming_a.players)
    == 1` assertion, which round 9's fix makes false by design whenever
    `variant1_players` has more than one body. This is the fix's headline
    invariant: `run_trade_window`'s `roster_max`/`roster_min` gates fail
    every N-for-1 package with N >= 2 at `roster_max` occupancy, and
    roster-neutrality is what drives both verdicts to zero structurally."""
    snapshot, ctx = _make_roster_neutral_fixture()
    result = propose_packages(1, 2, snapshot, ctx, ValuationCache())
    assert result  # sanity: this pair actually generates candidates
    for candidate in result:
        assert len(candidate.outgoing_a.players) >= 2, (
            "fixture must actually exercise N > 1 for this test to mean anything"
        )
        assert len(candidate.incoming_a.players) == len(candidate.outgoing_a.players)


def test_propose_packages_incoming_a_order_is_deterministic_across_two_passes():
    """§5.6 item 7, determinism half. `_summarize_packages`'s existing
    determinism test (`test_propose_packages_is_deterministic_at_a_fixed_
    seed`, above) sorts `incoming_a.players` ids before comparing, so it
    cannot catch a throw-in-ordering regression. `TradeExecution.players_
    b_to_a` is NOT sorted (`_bundle_subject`'s own internal sort, `trade.
    py:552-558`, is a separate concern from the tuple `evaluate_trade`/
    `TradeExecution` actually carry), so the raw, un-sorted order is
    observable downstream and must itself be deterministic across two
    independent passes.

    Renamed from `..._order_is_deterministic_and_order_sensitive` (critic
    round-9-fix-round B2): comparing two runs of the same deterministic
    function against each other only proves determinism, not correct
    order -- a full `tuple(reversed(chosen))` mutation of `_build_throw_
    ins`'s return still passed the old version of this test, since both
    runs would be reversed identically. The genuine order-sensitive
    assertion now lives in the sibling test below, which pins the actual
    expected tuple against an independently hand-derived expectation."""
    snapshot, ctx = _make_roster_neutral_fixture()
    result1 = propose_packages(1, 2, snapshot, ctx, ValuationCache())
    result2 = propose_packages(1, 2, snapshot, ctx, ValuationCache())
    incoming1 = tuple(tuple(rp.id for rp in c.incoming_a.players) for c in result1)
    incoming2 = tuple(tuple(rp.id for rp in c.incoming_a.players) for c in result2)
    assert incoming1 == incoming2
    assert any(len(ids) > 1 for ids in incoming1), (
        "fixture must actually exercise throw-ins for this test to mean anything"
    )


def test_propose_packages_pins_target_then_throw_in_order_against_a_hand_built_fixture():
    """§5.6 item 7, order-correctness half (critic round-9-fix-round B2).
    The expected order below is derived independently of `_build_throw_
    ins` -- NOT by calling it -- from `_make_roster_neutral_fixture`'s own
    hand-assigned salaries, so this test would genuinely fail under a
    `_build_throw_ins` mutation, unlike a test that reconstructs its
    expectation by calling the same (possibly-mutated) function under
    test.

    Derivation (§6 item 5, architect round 10 blast-radius pass -- rewritten
    to route through the new constructor's phases explicitly, since the
    old pre-round-10 derivation's own reasoning path -- "matching never
    triggers before the cap does" -- describes a stop condition that no
    longer exists). By analysis the *outcome* is unchanged even though the
    *path* to it is new:

    1. The proposer's 5 players are identical ($1,000,000 salary, same
       `overall_cached`/position pool), so every one of them has the same
       `fv_A` and the same `fv_B` -- both `_value_priority_orders`'
       `ratio_order` and `delivery_order` therefore tie-break on `a.id`
       alone and agree on the exact same sequence: ids 1000, 1001, 1002,
       1003, 1004.
    2. Neither ordering can clear either $20,000,000 target's value bar --
       five $1,000,000 bodies' combined `fv_B` is nowhere near a 90-overall
       star's requirement -- so phase 1 runs to `_MAX_OUTGOING_PLAYERS = 3`
       under the ratio ordering, fails to clear, retries under the
       (identical) delivery ordering, fails again, and keeps the ratio
       pass's best-effort result: ids 1000, 1001, 1002.
    3. `room=20_000_000` (`_make_ctx`'s default) is positive for both
       teams, so neither side is over the cap and phase 2 (the matching
       floor) never runs -- `variant1_players` is exactly `(1000, 1001,
       1002)`, matching the pre-round-10 result.
    4. With `outgoing_salary = $3,000,000`, `_build_throw_ins`'s own budget
       is `3,000,000 * 1.25 + 100,000 - 20,000,000`, deeply negative and
       clamped to 0 -- unchanged from before, since `_build_throw_ins`
       itself is not touched by this fix. Nothing survives the value pass
       at a $0 budget, so BOTH throw-ins come from backfill's cheapest-
       `salary_next`-first rule over the remaining eligible pool: the
       fixture's throw-in pool salaries are $1,000,000/$1,200,000/
       $1,400,000/$1,600,000/$1,800,000/$2,000,000 (ids 2100-2105) plus the
       OTHER $20,000,000 target (never cheap enough to be picked) -- the
       two cheapest, in salary order, are id 2100 then id 2101, for every
       target.

    Target-first-then-throw-ins-in-`_build_throw_ins`'s-own-return-order is
    `propose_packages`'s own assembly rule (`incoming_bundle = AssetBundle(
    players=(target, *throw_ins))`).

    A `tuple(reversed(chosen))` mutation of `_build_throw_ins`'s return
    flips the expected `(target_id, 2100, 2101)` to `(target_id, 2101,
    2100)`, which this test's exact-tuple assertion catches -- verified
    locally per the critic's own repro."""
    snapshot, ctx = _make_roster_neutral_fixture()
    result = propose_packages(1, 2, snapshot, ctx, ValuationCache())

    packages_by_target = {c.incoming_a.players[0].id: c for c in result}
    assert set(packages_by_target) == {2000, 2001}, (
        "fixture sanity: both partner targets must each produce exactly one candidate"
    )

    for target_id, candidate in packages_by_target.items():
        ids = tuple(rp.id for rp in candidate.incoming_a.players)
        assert ids == (target_id, 2100, 2101), (
            f"expected target-then-cheapest-backfill order (target_id, 2100, 2101), "
            f"got {ids!r}"
        )


def test_build_throw_ins_returns_no_throw_ins_for_a_single_outgoing_player():
    """§5.6 item 2 -- the `N = 1` path stays byte-identical: `k = len(
    outgoing_players) - 1 <= 0` returns the empty tuple immediately,
    before any budget/selection logic runs, so a package that already
    matched on `_build_outgoing_variant1`'s first player is untouched by
    this fix."""
    next_season = _SEASON + 1
    target = _salary_player(1, 3_000_000, team_id=2)
    outgoing_players = (_salary_player(2, 3_000_000, team_id=1),)
    b_players_ascending = (_salary_player(3, 1_000_000, team_id=2),)

    result = gm_service._build_throw_ins(
        target, outgoing_players, b_players_ascending, next_season,
        outgoing_occupying=len(outgoing_players),
    )

    assert result == ()


def test_build_throw_ins_budget_binds_and_the_resulting_package_is_legal_on_as_side():
    """§5.6 item 3, mutation-tested. `outgoing_players` totals $2M ->
    budget = 2,000,000 * 1.25 + 100,000 - target_salary(2,000,000) =
    600,000, strictly below `overpay`'s $5M salary -- the value pass must
    skip it and fall through to `cheap` ($500K, fits). Also proves the
    resulting package is legal on A's side under the real `_check_
    legality` with A genuinely over the cap (`used_pre > salary_cap`, the
    one condition that makes the matching rule live at all)."""
    next_season = _SEASON + 1
    target = _salary_player(1, 2_000_000, team_id=2)
    outgoing_players = (
        _salary_player(10, 1_000_000, team_id=1),
        _salary_player(11, 1_000_000, team_id=1),
    )
    overpay = _salary_player(2, 5_000_000, team_id=2)  # B's lowest-fv_B body, an overpay
    cheap = _salary_player(3, 500_000, team_id=2)
    b_players_ascending = (overpay, cheap)

    result = gm_service._build_throw_ins(
        target, outgoing_players, b_players_ascending, next_season,
        outgoing_occupying=len(outgoing_players),
    )
    assert overpay not in result
    assert result == (cheap,)

    next_season_cap_settings = dataclasses.replace(CAP_SETTINGS, season=CAP_SETTINGS.season + 1)
    filler_a = _filler_roster_covering_next_season(1, 12, avoid_position=Position.C, base_id=11_000)
    team_a = build_team_snapshot(make_team(1), filler_a + outgoing_players, (), CAP_SETTINGS)
    incoming = AssetBundle(players=(target, *result))
    outgoing = AssetBundle(players=outgoing_players)
    verdict = gm_service.trade_decisions._check_legality(
        team_a, incoming, outgoing, next_season, next_season_cap_settings,
        next_season_room_dollars=-1,  # forces `used_pre > salary_cap` so matching is live
    )
    assert verdict is None, (
        f"expected the balanced package to be legal on A's side, got {verdict!r}"
    )

    # Mutation: widen the budget (bigger outgoing salaries) and confirm the
    # SAME overpay body is now selected -- proves the prior rejection came
    # from the budget, not from `overpay` never being a candidate at all.
    wide_outgoing = (
        _salary_player(20, 4_000_000, team_id=1),
        _salary_player(21, 4_000_000, team_id=1),
    )
    widened = gm_service._build_throw_ins(
        target, wide_outgoing, b_players_ascending, next_season,
        outgoing_occupying=len(wide_outgoing),
    )
    assert overpay in widened


def test_build_throw_ins_budget_is_a_running_total_not_a_per_item_cap():
    """Critic round-9-fix-round B1. Every prior `_build_throw_ins` test uses
    `k=1` (the subtraction is observed at most once) or `k=2` with the
    budget clamped to 0 (subtraction from zero is unobservable either way)
    -- nothing distinguished `remaining_budget -= salary` (a real running
    total) from that line being a no-op. This fixture forces `k=2` against
    a strictly positive budget where candidate 1 and candidate 2 EACH fit
    a fresh, non-decremented $3,000,000 budget on their own ($2,000,000
    apiece), but their sum ($4,000,000) does not -- so a correct running
    subtraction must reject candidate 2 from the value pass after
    candidate 1 is taken (remaining budget: $1,000,000), forcing it to
    fall through toward backfill, where a cheaper third body ($1,500,000,
    still over the exhausted $1,000,000 remaining budget too, so it is
    also backfilled rather than value-passed) wins on backfill's own
    cheapest-first rule instead.

    Locally verified (per the critic's own repro): commenting out this
    module's `remaining_budget -= salary` line makes this test fail --
    the mutated value pass never decrements, so candidate 2 fits the
    still-full $3,000,000 budget on the very next iteration and is taken
    directly into the value pass, producing `(cand_1, cand_2)` instead of
    the correct `(cand_1, cheaper_backfill)`."""
    next_season = _SEASON + 1
    # outgoing_salary = 4,000,000 -> budget = 4,000,000 * 1.25 + 100,000
    # - target_salary(2,100,000) = 3,000,000.
    target = _salary_player(1, 2_100_000, team_id=2)
    outgoing_players = (
        _salary_player(10, 1_500_000, team_id=1),
        _salary_player(11, 1_500_000, team_id=1),
        _salary_player(12, 1_000_000, team_id=1),
    )
    cand_1 = _salary_player(2, 2_000_000, team_id=2)  # rank 1 -- taken first
    cand_2 = _salary_player(3, 2_000_000, team_id=2)  # rank 2 -- fits fresh budget, not remaining
    cheaper_backfill = _salary_player(4, 1_500_000, team_id=2)  # rank 3, cheaper than cand_2
    b_players_ascending = (cand_1, cand_2, cheaper_backfill)

    result = gm_service._build_throw_ins(
        target, outgoing_players, b_players_ascending, next_season,
        outgoing_occupying=len(outgoing_players),
    )

    assert cand_1 in result
    assert cand_2 not in result, (
        "cand_2 fits a FRESH $3,000,000 budget but not the $1,000,000 "
        "remaining after cand_1 is taken -- a correctly-decrementing "
        "budget must reject it from the value pass"
    )
    assert result == (cand_1, cheaper_backfill), (
        "cand_2 being excluded must route selection to backfill's own "
        "cheapest-first body, not silently drop the slot"
    )


def test_build_throw_ins_prefers_lower_fv_b_when_both_fit_the_budget():
    """§5.6 item 4, mutation-tested. Two equally-cheap B bodies, both well
    within budget -- the value pass must take the one earlier in `b_
    players_ascending` (B's own lower-`fit_value` read), not merely
    whichever fits first by coincidence of salary."""
    next_season = _SEASON + 1
    target = _salary_player(1, 2_000_000, team_id=2)
    outgoing_players = (
        _salary_player(10, 3_000_000, team_id=1),
        _salary_player(11, 3_000_000, team_id=1),
    )
    lower_fv = _salary_player(2, 1_000_000, team_id=2)
    higher_fv = _salary_player(3, 1_000_000, team_id=2)

    result = gm_service._build_throw_ins(
        target, outgoing_players, (lower_fv, higher_fv), next_season,
        outgoing_occupying=len(outgoing_players),
    )
    assert result == (lower_fv,)

    # Mutation: swap the fv_B ordering and confirm the selection follows it.
    swapped = gm_service._build_throw_ins(
        target, outgoing_players, (higher_fv, lower_fv), next_season,
        outgoing_occupying=len(outgoing_players),
    )
    assert swapped == (higher_fv,)


def test_build_throw_ins_backfill_is_cheapest_first_when_nothing_fits_the_budget():
    """§5.6 item 5. `target`'s $10M salary against a tiny $200K outgoing
    pair leaves a negative, budget-clamped-to-0 headroom -- nothing fits
    the value pass, so the backfill branch must still return `k` bodies,
    cheapest-`salary_next`-first, ignoring `fit_value` order entirely."""
    next_season = _SEASON + 1
    target = _salary_player(1, 10_000_000, team_id=2)
    outgoing_players = (
        _salary_player(10, 100_000, team_id=1),
        _salary_player(11, 100_000, team_id=1),
        _salary_player(12, 100_000, team_id=1),
    )
    expensive_low_fv = _salary_player(2, 5_000_000, team_id=2)
    mid_cost = _salary_player(3, 50_000, team_id=2)
    cheapest = _salary_player(4, 20_000, team_id=2)
    b_players_ascending = (expensive_low_fv, mid_cost, cheapest)

    result = gm_service._build_throw_ins(
        target, outgoing_players, b_players_ascending, next_season,
        outgoing_occupying=len(outgoing_players),
    )
    assert result == (cheapest, mid_cost)


def test_build_throw_ins_degrades_without_raising_when_bs_pool_is_smaller_than_k():
    """§5.6 item 6. `outgoing_players` asks for `k = 2` throw-ins, but B's
    roster carries only the target plus one other body -- the generator
    must return that one body, not raise; legality of the resulting
    (non-neutral) package is `_check_legality`'s job, not this
    generator's, exactly as `_build_outgoing_variant1`'s own docstring
    already establishes for its own cap-reached case."""
    next_season = _SEASON + 1
    target = _salary_player(1, 2_000_000, team_id=2)
    other = _salary_player(2, 500_000, team_id=2)
    outgoing_players = (
        _salary_player(10, 1_000_000, team_id=1),
        _salary_player(11, 1_000_000, team_id=1),
        _salary_player(12, 1_000_000, team_id=1),
    )

    result = gm_service._build_throw_ins(
        target, outgoing_players, (target, other), next_season,
        outgoing_occupying=len(outgoing_players),
    )
    assert result == (other,)


# ---------------------------------------------------------------------------
# Architect round 12 (docs/ledger/reports/phase-2-architect-round-12.md
# §5.3/§5.5) -- `_build_throw_ins`'s occupancy-balance rule. Tests 2-6 of
# §5.5's 7; test 1 (the `_occupies_next_season` property test) lives next
# to `_next_season_roster_count`'s own tests further down this file; test
# 7 (the harness-level waiver-count headline test) lives in `test_season.
# py`, written after the real post-fix measurement per the memo's own
# discipline.
# ---------------------------------------------------------------------------


def test_throw_ins_are_occupancy_neutral_for_the_partner():
    """§5.5 test 2. A sends 3 occupying players (`outgoing_occupying=3`);
    `target` occupies (`t_occ=1`), so `k_occ = clamp(3 - 1, 0, 2) = 2` --
    BOTH of the 2 returned throw-ins must occupy a next-season slot. B's
    eligible pool puts a cheap, non-occupying (expiring) candidate FIRST
    in `b_players_ascending` -- the exact shape that made the pre-round-12
    single-pass value selection prefer it (it is cheapest, `salary_next ==
    0`) even though taking it would leave B's next-season occupancy short
    by one slot.

    **Kills:** reverting to `salary_next`-only selection (would pick the
    expiring candidate over `occ_2`, since it is cheaper and earlier in
    `b_players_ascending`, leaving only 1 of 2 throw-ins occupying)."""
    next_season = _SEASON + 1
    target = _salary_player(1, 3_000_000, team_id=2)  # occupies
    outgoing_players = (
        _salary_player(10, 2_000_000, team_id=1),
        _salary_player(11, 2_000_000, team_id=1),
        _salary_player(12, 2_000_000, team_id=1),
    )  # all 3 occupy
    non_occ_cheap = _expiring_player(2, 200_000, team_id=2)  # cheapest, does NOT occupy
    occ_1 = _salary_player(3, 1_000_000, team_id=2)
    occ_2 = _salary_player(4, 1_000_000, team_id=2)
    b_players_ascending = (non_occ_cheap, occ_1, occ_2)

    result = gm_service._build_throw_ins(
        target, outgoing_players, b_players_ascending, next_season,
        outgoing_occupying=3,
    )

    assert len(result) == 2
    assert non_occ_cheap not in result, (
        "the cheapest B candidate does not occupy a next-season slot -- taking it "
        "would leave B's occupancy delta short by one, exactly the regression "
        "this rule exists to close"
    )
    occupying_count = sum(1 for rp in result if gm_service._occupies_next_season(rp, next_season))
    assert occupying_count == 2, f"expected both throw-ins to occupy, got {occupying_count} of 2"
    assert {rp.id for rp in result} == {occ_1.id, occ_2.id}


def test_throw_ins_are_occupancy_neutral_when_the_target_is_expiring():
    """§5.5 test 3. `target` itself is a rental (expiring, `t_occ = 0`),
    with `outgoing_occupying = 3` and `k = 3` -- correct `k_occ = clamp(3 -
    0, 0, 3) = 3` (all 3 throw-ins must occupy). A hardcoded `t_occ = 1`
    (ignoring whether `target` actually occupies) would instead compute
    `k_occ = clamp(3 - 1, 0, 3) = 2`, opening a spurious 1-slot non-
    occupying quota that a cheap expiring B candidate would fill.

    **Kills:** hardcoding `t_occ = 1` -- exactly the "target is itself a
    rental" under-balancing case round 12 §5.5 names."""
    next_season = _SEASON + 1
    target = _expiring_player(1, 5_000_000, team_id=2)  # does NOT occupy
    outgoing_players = (
        _salary_player(10, 2_000_000, team_id=1),
        _salary_player(11, 2_000_000, team_id=1),
        _salary_player(12, 2_000_000, team_id=1),
        _expiring_player(13, 2_000_000, team_id=1),  # non-occupying outgoing body
    )  # outgoing_occupying = 3 (the expiring one doesn't count)
    non_occ = _expiring_player(2, 500_000, team_id=2)
    occ_1 = _salary_player(3, 1_000_000, team_id=2)
    occ_2 = _salary_player(4, 1_000_000, team_id=2)
    occ_3 = _salary_player(5, 1_000_000, team_id=2)
    b_players_ascending = (non_occ, occ_1, occ_2, occ_3)

    result = gm_service._build_throw_ins(
        target, outgoing_players, b_players_ascending, next_season,
        outgoing_occupying=3,
    )

    assert len(result) == 3
    assert non_occ not in result, (
        "with target itself expiring, all 3 throw-ins must occupy -- a hardcoded "
        "t_occ=1 would open a 1-slot non-occupying quota and admit this cheap "
        "expiring candidate instead"
    )
    occupying_count = sum(1 for rp in result if gm_service._occupies_next_season(rp, next_season))
    assert occupying_count == 3, f"expected all 3 throw-ins to occupy, got {occupying_count} of 3"
    assert {rp.id for rp in result} == {occ_1.id, occ_2.id, occ_3.id}


def test_throw_ins_still_take_the_partners_own_cheapest_within_each_class():
    """§5.5 test 4, round 12's own "most dangerous plausible mis-build"
    guard. Three occupying B candidates, identical salary (so the budget
    cannot discriminate among them) but distinct positions in `b_players_
    ascending` (B's own lowest-`fit_value`-first order) -- the value pass
    within the occupying class must take the two EARLIEST in that order,
    not merely any two that fit the budget.

    **Kills:** a fix that satisfies occupancy by taking B's *good* (high-
    `fit_value`) players instead of its cheapest-to-B ones -- round 12 §5.5
    names this explicitly as the fix most likely to collapse mutual
    acceptance and take M18/M19 down with it."""
    next_season = _SEASON + 1
    target = _salary_player(1, 500_000, team_id=2)  # occupies
    outgoing_players = (
        _salary_player(10, 500_000, team_id=1),
        _salary_player(11, 500_000, team_id=1),
        _salary_player(12, 500_000, team_id=1),
    )  # all 3 occupy
    occ_low1 = _salary_player(2, 500_000, team_id=2)   # B's least-valued occupying body
    occ_low2 = _salary_player(3, 500_000, team_id=2)   # B's second-least-valued
    occ_high3 = _salary_player(4, 500_000, team_id=2)  # B's most-valued occupying body
    b_players_ascending = (occ_low1, occ_low2, occ_high3)

    result = gm_service._build_throw_ins(
        target, outgoing_players, b_players_ascending, next_season,
        outgoing_occupying=3,
    )

    assert result == (occ_low1, occ_low2), (
        "the occupying class's own value pass must take B's cheapest-to-B "
        "(lowest-fit_value, earliest-in-ascending-order) candidates first, "
        "identical salary rules out the budget as the discriminator here"
    )
    assert occ_high3 not in result


def test_throw_in_class_falls_back_when_one_class_is_exhausted():
    """§5.5 test 5. `outgoing_occupying=2`, `target` occupies -> `k_occ =
    clamp(2 - 1, 0, 2) = 1`, `k_non = 1` -- but B's entire eligible pool is
    occupying (zero non-occupying candidates exist), so the non-occupying
    class's own quota can never be met from within its own class. The
    cross-class fallback must fill that missing slot from the occupying
    class's own remaining candidate rather than returning short.

    **Kills:** a strict per-class quota that returns short (`len(result) ==
    1` instead of `2`) and never raises but silently starves package
    supply -- the round-9 deadlock, reintroduced through a side door."""
    next_season = _SEASON + 1
    target = _salary_player(1, 1_000_000, team_id=2)  # occupies
    outgoing_players = (
        _salary_player(10, 1_000_000, team_id=1),
        _salary_player(11, 1_000_000, team_id=1),
        _expiring_player(12, 1_000_000, team_id=1),
    )  # outgoing_occupying = 2 (the expiring one doesn't count)
    occ_a = _salary_player(2, 300_000, team_id=2)
    occ_b = _salary_player(3, 300_000, team_id=2)
    b_players_ascending = (occ_a, occ_b)  # zero non-occupying candidates
    assert all(gm_service._occupies_next_season(rp, next_season) for rp in b_players_ascending), (
        "fixture sanity: B's eligible pool must be entirely occupying for this "
        "test to exercise the cross-class fallback rather than the ordinary path"
    )

    result = gm_service._build_throw_ins(
        target, outgoing_players, b_players_ascending, next_season,
        outgoing_occupying=2,
    )

    assert len(result) == 2, (
        "B has no non-occupying candidates at all -- the cross-class fallback "
        "must fill the non-occupying class's own unmet quota from the occupying "
        "class rather than returning only 1 throw-in"
    )
    assert {rp.id for rp in result} == {occ_a.id, occ_b.id}


def test_throw_ins_non_occupying_class_takes_bs_lowest_fit_value_candidates_in_order():
    """Critic round 43, B2. Not one of round 12 §5.5's original 7 named
    tests -- added because none of them exercise the non-occupying
    class's own iteration order with a non-trivial (`k_non >= 2`) quota
    and a non-empty pool: tests 2/3/4 all have `k_non == 0`, and test 5's
    `k_non == 1` pool is empty by construction (it exists to exercise the
    cross-class fallback instead). The gap was real: reversing the non-
    occupying class's own loop (`for rp in reversed(non_occupying):`)
    changes the selected throw-in set in 2,422 of 5,238 real sweep calls
    (46%) yet the full default suite and the full `-m slow` set both pass
    unchanged under that mutation, because every existing test's fixture
    happens to avoid the `k_non >= 2` + non-empty-pool shape.

    `target` occupies (`t_occ=1`) and `outgoing_occupying=1` is passed
    directly (the function trusts the caller's count, never recomputes it
    from `outgoing_players`) -> `k_occ = clamp(1 - 1, 0, 2) = 0`, `k_non =
    2`. B's eligible pool is 3 non-occupying (expiring) candidates in
    `b_players_ascending` order representing 3 genuinely distinct `fit_
    value` ranks (their position in the list, same proxy test 4 above
    uses) -- deliberately given DESCENDING current-season salaries
    (highest first, lowest last) so a reader can't mistake "selects the
    first two in ascending fit_value order" for "selects the two cheapest
    by dollar amount"; both would coincide if salaries were ascending
    too, which would make this test as weak as the ones that missed the
    gap. Since `salary_next` is 0 for all three regardless (none cover
    `next_season`), only list order can be doing the discriminating.

    **Kills:** reversing the non-occupying class's own iteration order
    (`for rp in reversed(non_occupying):`) -- mutation-verified directly
    against this test before landing it (fails under the reversal,
    passes restored)."""
    next_season = _SEASON + 1
    target = _salary_player(1, 500_000, team_id=2)  # occupies
    outgoing_players = (
        _salary_player(10, 500_000, team_id=1),
        _salary_player(11, 500_000, team_id=1),
        _salary_player(12, 500_000, team_id=1),
    )  # length 3 -> k = 2; only 1 of these need actually occupy since
    # outgoing_occupying is caller-supplied below, not recomputed here
    non_1 = _expiring_player(2, 800_000, team_id=2)  # B's lowest fit_value non-occupying body
    non_2 = _expiring_player(3, 400_000, team_id=2)  # B's second-lowest
    non_3 = _expiring_player(4, 100_000, team_id=2)  # B's highest fit_value non-occupying body
    b_players_ascending = (non_1, non_2, non_3)  # zero occupying candidates
    assert not any(
        gm_service._occupies_next_season(rp, next_season) for rp in b_players_ascending
    ), "fixture sanity: B's eligible pool must be entirely non-occupying for this test"

    result = gm_service._build_throw_ins(
        target, outgoing_players, b_players_ascending, next_season,
        outgoing_occupying=1,
    )

    assert len(result) == 2
    assert result == (non_1, non_2), (
        "the non-occupying class must take B's own lowest-fit_value candidates "
        "first (ascending b_players_ascending order), not the highest, and not "
        "an order the current-season salary figures alone could produce"
    )
    assert non_3 not in result


# ---------------------------------------------------------------------------
# Critic round 51 (B1) -- Decision P2-37 does not reach the trade-
# construction pipeline. A prior build round threaded `retirement_age`
# through `_probe_package`/`_run_value_phase`/`_build_outgoing_variant1`/
# `_build_throw_ins` on top of `propose_packages`; the critic proved it was
# neither load-bearing for the M1 fix nor safe (it broke `_build_throw_
# ins`'s non-occupying admission pass's documented `salary_next == 0`
# premise the moment a player is reclassified non-occupying by age while
# their contract still covers `next_season`). The threading was removed
# entirely -- these tests guard against it silently coming back.
# ---------------------------------------------------------------------------


def test_trade_chain_functions_reject_a_retirement_age_keyword():
    """None of the trade-construction pipeline's functions accept a
    `retirement_age` parameter any more (Decision P2-37 does not reach
    this pipeline) -- calling any of them with the keyword must raise
    `TypeError` immediately, a strong, cheap guard against a future edit
    silently re-adding the parameter (the exact shape a prior build round
    added and critic round 51 (B1) reverted)."""
    next_season = _SEASON + 1
    target = _salary_player(1, 3_000_000, team_id=2)
    outgoing_players = (_salary_player(10, 3_000_000, team_id=1),)
    b_players_ascending = (_salary_player(2, 1_000_000, team_id=2),)
    fv_by_player_id = {
        target.id: 50.0, b_players_ascending[0].id: 5.0, outgoing_players[0].id: 10.0,
    }
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.WIN_NOW, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_by_player_id, players_ascending=b_players_ascending,
    )

    with pytest.raises(TypeError):
        gm_service._build_throw_ins(
            target, outgoing_players, b_players_ascending, next_season,
            outgoing_occupying=1, retirement_age=40,
        )
    with pytest.raises(TypeError):
        gm_service._probe_package(
            target, outgoing_players, partner=partner, next_season=next_season,
            next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=False, tuning=DEFAULT_TUNING,
            retirement_age=40,
        )
    with pytest.raises(TypeError):
        gm_service._run_value_phase(
            outgoing_players, target, partner=partner, next_season=next_season,
            next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=False, tuning=DEFAULT_TUNING,
            retirement_age=40,
        )
    with pytest.raises(TypeError):
        gm_service._build_outgoing_variant1(
            target, outgoing_players, next_season, partner=partner,
            fv_a_by_player_id=fv_by_player_id, next_season_cap_settings=CAP_SETTINGS,
            proposer_over_cap=False, tuning=DEFAULT_TUNING, retirement_age=40,
        )


def test_build_throw_ins_classifies_a_near_retirement_contract_covering_candidate_as_occupying():
    """Critic round 51 (B1), reproduced directly against the fixed code.
    `near_retirement` is age 41 at `next_season` (would misclassify as
    non-occupying if a `retirement_age=40` had leaked through, since the
    reverted threading's age check fires regardless of contract state) but
    genuinely holds a contract COVERING `next_season` -- a real, nonzero
    `salary_next` of $40,000,000. `_build_throw_ins` classifies it purely
    on the contract-only basis (no `retirement_age` parameter exists any
    more to do otherwise), so it lands in the OCCUPYING class, not the
    non-occupying, assumed-zero-cost admission pass.

    `target` occupies (`t_occ=1`) and `outgoing_occupying=0` (both outgoing
    players are genuinely expiring) forces `k_occ = clamp(1 - 1, 0, 1) =
    0`, `k_non = 1` -- the occupying class's own pass never runs at all
    (`occ_taken >= k_occ` is true from the start), so `near_retirement`
    being (correctly) classified into that class means it is never even
    considered for the one open slot. `genuinely_expiring` (real `salary_
    next == 0`, listed second) fills it instead.

    **What this proves, concretely:** the selected throw-in's summed
    `salary_next` is `$0`, matching the critic's own reproduction of the
    CORRECT baseline exactly. Had the reverted `retirement_age=40`
    threading still been live, `near_retirement` -- listed FIRST in `b_
    players_ascending` -- would have reclassified as non-occupying on age
    alone and been admitted unconditionally by the non-occupying pass's
    zero-cost assumption, landing a real $40,000,000 in the package
    invisibly (the critic's own `over_budget=True` finding)."""
    next_season = _SEASON + 1
    target = _salary_player(1, 3_000_000, team_id=2)  # occupies -> t_occ = 1
    outgoing_players = (
        _expiring_player(10, 1_000_000, team_id=1),
        _expiring_player(11, 1_000_000, team_id=1),
    )  # both non-occupying -> outgoing_occupying = 0
    near_retirement = make_roster_player(
        2, age=41, season=next_season, overall_cached=70, team_id=2,
        primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            2, team_id=2, start_season=_SEASON, salaries=(3_000_000, 40_000_000),
        ),  # covers _SEASON and next_season -- occupying on the contract-only basis
    )
    genuinely_expiring = _expiring_player(3, 0, team_id=2)
    b_players_ascending = (near_retirement, genuinely_expiring)  # near_retirement listed FIRST
    assert gm_service._occupies_next_season(near_retirement, next_season) is True, (
        "fixture sanity: near_retirement must be occupying on the contract-only basis "
        "for this test to mean anything (no retirement_age parameter exists to override it)"
    )

    result = gm_service._build_throw_ins(
        target, outgoing_players, b_players_ascending, next_season,
        outgoing_occupying=0,
    )

    assert near_retirement not in result, (
        "a contract-covering candidate must never be admitted through the non-occupying, "
        "assumed-zero-cost pass regardless of age -- Decision P2-37 does not reach this "
        "function at all"
    )
    assert result == (genuinely_expiring,)
    assert sum(gm_service.trade_decisions.salary_next(rp, next_season) for rp in result) == 0


@pytest.mark.slow
def test_occupancy_rule_does_not_raise_illegal_salary_matching_on_the_partner_side():
    """§5.5 test 6. Reuses rounds 35/42's ~1,740-target sweep methodology
    (a real, generated 30-team league; every ordered team pair x `TRADE_
    TARGETS_PER_PARTNER=2` targets -- `30 * 29 * 2 == 1740`) at `rng_
    seed=1`, the exact seed and scan shape those rounds measured B-side
    `illegal:salary_matching` at **0**, pre-round-12-fix. Mirrors `run_
    trade_window`'s own scan loop directly rather than calling it,
    counting each TARGET once (a target with a `top_pick` produces two
    `CandidatePackage`s sharing one, identical throw-in set -- counting
    packages would double-count some targets and not others, corrupting
    the 1,740 figure this test's own sanity assert depends on).

    This is a real measurement against the real production call chain
    (`propose_packages` -> `_build_throw_ins` -> `evaluate_trade` ->
    `_check_legality`), not an assertion about a hand-built fixture --
    round 12 §5.5 test 6 is explicit that it must be.

    **Correction (critic round 43, B1):** this test's docstring originally
    claimed it "kills" reversing the occupying-class-first budget-
    consumption order (round 12 §8 RR1's own framing). That target is
    mathematically impossible to kill: every non-occupying candidate has
    `salary_next == 0` by construction (see `_build_throw_ins`'s own
    corrected docstring), so the non-occupying value pass never
    consumes shared budget regardless of which class's pass runs first --
    confirmed by directly reversing the two loops in `_build_throw_ins`
    and re-running this exact test, which still passed. The false "Kills"
    claim has been removed rather than reworded to sound true.

    What this test actually verifies, and does not overclaim to be a
    mutation-killing unit test: a real empirical property of the whole
    occupancy-balance rule against actual production traffic -- that
    across a large, realistic sweep of real packages, B-side `illegal:
    salary_matching` stays at (here) or near the same ~0 baseline rounds
    35/42 measured before round 12's fix existed. It is a regression
    guard against the occupancy-balance rule *as a whole* pushing real
    salary out of B's matching window somewhere the smaller hand-built
    fixture tests (tests 2-5 above) wouldn't catch, not a guard against
    any one specific code path or ordering choice inside it."""
    from tools.gm_harness.league import generate_gm_league
    from tools.gm_harness.state import build_ctx, build_snapshot

    state = generate_gm_league(league_id=1, season=_SEASON, rng_seed=1, players_per_team=15)
    snapshot = build_snapshot(state)
    ctx = build_ctx(state, phase=Phase.REGULAR_SEASON)
    cache = gm_service.ValuationCache()
    next_season = ctx.next_season
    team_ids = [t.id for t in snapshot.teams]

    targets_scanned = 0
    b_side_salary_matching = 0
    for a_id in team_ids:
        for b_id in team_ids:
            if a_id == b_id:
                continue
            team_b = snapshot.team_by_id(b_id)
            seen_targets: set[int] = set()
            for cand in propose_packages(a_id, b_id, snapshot, ctx, cache):
                target_id = cand.incoming_a.players[0].id
                if target_id in seen_targets:
                    continue  # variant2 shares variant1's throw-ins -- one target, once
                seen_targets.add(target_id)
                targets_scanned += 1
                record_b = gm_service.trade_decisions.evaluate_trade(
                    team_b,
                    cand.outgoing_a,
                    cand.incoming_a,
                    ctx.philosophy_by_team[b_id],
                    ctx.window_by_team[b_id],
                    league_rng_seed=ctx.league_rng_seed,
                    season=ctx.season,
                    phase=ctx.phase,
                    cap_settings=ctx.cap_settings,
                    next_season=next_season,
                    next_season_cap_settings=ctx.next_season_cap_settings,
                    next_season_room_dollars=ctx.room_by_team[b_id],
                    rookie_scale=ctx.rookie_scale,
                    league_snapshot=snapshot,
                    tuning=ctx.tuning,
                )
                if record_b.verdict == "illegal:salary_matching":
                    b_side_salary_matching += 1

    assert targets_scanned == 1740, (
        f"fixture sanity: expected the same 30x29x2 target count rounds 35/42 "
        f"measured, got {targets_scanned} -- the sweep shape must match round "
        f"35/42's own before this test's comparison to their recorded 0 means "
        f"anything"
    )
    assert b_side_salary_matching == 0, (
        f"pre-round-12-fix (rounds 35/42, same seed/shape) measured 0 B-side "
        f"illegal:salary_matching verdicts -- got {b_side_salary_matching} "
        f"post-fix; round 12 §8 RR1 predicted this stays at or near zero "
        f"because the occupying class (the one with real salary_next dollars) "
        f"is served first against the shared budget"
    )


def test_valuation_cache_fit_value_key_ignores_the_matrix_argument_poisoning_hazard():
    """§5.6 item 8 -- demonstrates round 9 §2 F5's named cache-poisoning
    trap concretely, not just in the docstring: `ValuationCache.fit_
    value`'s key is `(team_id, roster_player.id)` alone, so passing B's
    player through A's `NeedMatrix` (the exact mistake -- `matrix_a`
    paired with `partner_id` -- the docstring now warns against) returns
    the value computed by whichever call happened first, silently, for
    the rest of the pass."""
    snapshot = _make_small_league()
    ctx = _make_ctx(snapshot)
    team_a = snapshot.team_by_id(1)
    team_b = snapshot.team_by_id(2)
    cache = ValuationCache()
    matrix_a = cache.need_matrix(team_a, ctx.tuning)
    matrix_b = cache.need_matrix(team_b, ctx.tuning)
    assert matrix_a != matrix_b  # sanity: the two teams' rosters actually differ

    rp = team_b.roster[0]
    # The trap: matrix_a (team A's own read) paired with team_id=2 (team B).
    value_via_wrong_matrix = cache.fit_value(2, rp, matrix_a, ctx)
    value_via_right_matrix = cache.fit_value(2, rp, matrix_b, ctx)
    assert value_via_right_matrix == value_via_wrong_matrix, (
        "the cache key omits `matrix`, so this second call must silently reuse the "
        "first call's (wrong-matrix) cached value instead of recomputing against "
        "matrix_b -- this IS the poisoning hazard, demonstrated rather than assumed"
    )

    fresh_cache = ValuationCache()
    true_value_with_right_matrix = fresh_cache.fit_value(2, rp, matrix_b, ctx)
    assert true_value_with_right_matrix != value_via_wrong_matrix, (
        "sanity: matrix_a and matrix_b must actually produce different fit_value "
        "figures for this player, or the trap demonstrated above proves nothing"
    )


# ---------------------------------------------------------------------------
# Architect round 10 -- value-targeted outgoing construction (cause 2).
# `_probe_package`, `_value_priority_orders`, `_build_outgoing_variant1`'s
# new body, and `ValuationCache.fit_values` -- docs/ledger/reports/
# phase-2-architect-round-10.md §5.7's sixteen named tests, in order.
# ---------------------------------------------------------------------------


def _filler_roster_covering_next_season_probe(
    team_id: int, count: int, *, avoid_position: Position, base_id: int, salary: int = 3_000_000
):
    """Local alias of the identically-shaped helper defined later in this
    file (`_filler_roster_covering_next_season`, used by the run_trade_
    window B2 regression) -- duplicated here rather than reordered/hoisted
    so this section reads top-to-bottom against round 10's own §5.7 list
    without a forward reference; both build 2-year (`_SEASON`, `_SEASON +
    1`) contracts so `_check_legality`'s real filler math sees a stable
    contracted count."""
    positions = [p for p in Position if p is not avoid_position]
    roster = []
    for i in range(count):
        roster.append(
            make_roster_player(
                base_id + i, age=26 + (i % 4), season=_SEASON, overall_cached=64 + (i % 8),
                team_id=team_id, primary_position=positions[i % len(positions)],
                peak_age_start=27, peak_age_end=31,
                contract=make_contract(
                    base_id + i, team_id=team_id, start_season=_SEASON, salaries=(salary, salary),
                ),
            )
        )
    return tuple(roster)


def _make_probe_fixture():
    """A real two-team fixture for §5.7 item 1 -- the headline test. Team
    A (proposer): 14 filler bodies. Team B (partner): 13 filler bodies plus
    one real target (a 70-overall, $5M/yr center) -- both rosters sit at
    `roster_min` (14), so a roster-neutral swap keeps both legal. `room_
    by_team[2] = 0` and `window_by_team[2] = WIN_NOW` (`desired_room =
    0.03`) together give B a genuinely nonzero `room_hunger`, so `cap_
    delta_bonus` is a real, nonzero term here -- not clamped to 0 the way
    a `CONTEND`-window fixture would be -- which is what makes this
    fixture able to catch a dropped-`cap_delta` mutation (§5.7 item 1's own
    kill list). The target is deliberately mid-tier rather than a
    superstar: `s_out_b` (target + throw-ins, B's own currency) comes out
    to ~455 VP here, comfortably under architect round 10 F1's stated
    `666.7` VP crossover -- so `min_rel_gain * s_out_b` (~27) sits below
    `min_abs_gain` (40) and the `max(...)` floor actually binds on this
    fixture (critic round 42 B2: the original 88-overall/$18M target
    produced `s_out_b = 2161.7`, where the floor never binds and a mutation
    dropping it entirely is a semantic no-op on this test)."""
    proposer_roster = _filler_roster_covering_next_season_probe(
        1, 14, avoid_position=Position.C, base_id=1000
    )
    partner_filler = _filler_roster_covering_next_season_probe(
        2, 13, avoid_position=Position.C, base_id=2000
    )
    target = make_roster_player(
        2900, age=27, season=_SEASON, overall_cached=70, team_id=2,
        primary_position=Position.C, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            2900, team_id=2, start_season=_SEASON, salaries=(5_000_000, 5_000_000),
        ),
    )
    partner_roster = partner_filler + (target,)

    team_a = build_team_snapshot(make_team(1), proposer_roster, (), CAP_SETTINGS)
    team_b = build_team_snapshot(make_team(2), partner_roster, (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_a, team_b))

    ctx = dataclasses.replace(
        _make_ctx(snapshot),
        room_by_team={1: 20_000_000, 2: 0},
        philosophy_by_team={1: GmPhilosophy.TENDENCY_RESPECTER, 2: GmPhilosophy.YOUTH_DEVELOPER},
        window_by_team={1: CompetitiveWindow.RETOOL, 2: CompetitiveWindow.WIN_NOW},
    )
    return snapshot, ctx, target


def test_probe_package_value_gap_equals_evaluate_trades_own_score_minus_threshold():
    """§5.7 item 1 -- the headline test (round 10 F8). `_probe_package`'s
    `value_gap` must equal `record_b.score - record_b.threshold` from the
    REAL `trade_decisions.evaluate_trade` call, up to floating-point
    summation order -- not a hand-modeled expectation. Verified locally to
    fail loudly under exactly the mutations round 10 F8/§5.7 item 1 name:
    dropping `cap_delta`, swapping its incoming/outgoing bundles, omitting
    the `MIN_ABS_GAIN` floor, applying `loyalty` to the wrong sum, reading
    `fv_a` where `fv_b` belongs, and omitting the throw-ins from
    `s_out_b` -- each moves the two sides apart by orders of magnitude
    more than the `1e-6` tolerance below."""
    snapshot, ctx, target = _make_probe_fixture()
    team_a = snapshot.team_by_id(1)
    team_b = snapshot.team_by_id(2)
    cache = ValuationCache()
    next_season = ctx.next_season
    all_players = (*team_a.roster, *team_b.roster)
    fv_b = cache.fit_values(team_b, all_players, ctx)
    b_players_ascending = sorted(team_b.roster, key=lambda rp: (fv_b[rp.id], rp.id))

    partner = gm_service._PartnerTerms(
        team_id=2,
        loyalty=gm_service.PROFILES[ctx.philosophy_by_team[2]].loyalty,
        window=ctx.window_by_team[2],
        room_dollars=ctx.room_by_team[2],
        over_cap=ctx.room_by_team[2] < 0,
        fv_by_player_id=fv_b,
        players_ascending=tuple(b_players_ascending),
    )
    outgoing_players = (team_a.roster[0], team_a.roster[1])
    probe = gm_service._probe_package(
        target, outgoing_players, partner=partner, next_season=next_season,
        next_season_cap_settings=ctx.next_season_cap_settings,
        proposer_over_cap=ctx.room_by_team[1] < 0, tuning=ctx.tuning,
    )

    record_b = gm_service.trade_decisions.evaluate_trade(
        team_b,
        AssetBundle(players=outgoing_players),
        AssetBundle(players=(target, *probe.throw_ins)),
        ctx.philosophy_by_team[2], ctx.window_by_team[2],
        league_rng_seed=ctx.league_rng_seed, season=ctx.season, phase=ctx.phase,
        cap_settings=ctx.cap_settings, next_season=next_season,
        next_season_cap_settings=ctx.next_season_cap_settings,
        next_season_room_dollars=ctx.room_by_team[2],
        rookie_scale=ctx.rookie_scale, league_snapshot=snapshot, tuning=ctx.tuning,
    )
    assert not record_b.verdict.startswith("illegal:"), (
        "fixture sanity: the probed package must actually be legal on B's side, or "
        "record_b.score/threshold are both trivially 0.0 and this test proves nothing"
    )
    assert abs(probe.value_gap - (record_b.score - record_b.threshold)) <= 1e-6


# ---------------------------------------------------------------------------
# Phase 3, §11 Stage B step 5 -- `_probe_package`'s pick-value scalar
# (Decision P3-5/P3-11): `incoming_pick_value`, added unweighted to
# `s_in_b`, matching `evaluate_trade`'s own `in:pick` weight of `+1.0`.
# ---------------------------------------------------------------------------


def test_probe_package_incoming_pick_value_matches_evaluate_trades_own_pick_bearing_gap():
    """§11 Stage B step 5's verify #3 -- with `incoming_pick_value`
    *passed*, `_probe_package`'s predicted `value_gap` must equal the real
    `evaluate_trade`'s `score - threshold` on the equivalent pick-bearing
    package, checked as a **magnitude** assertion (`abs(...) < 1e-9`),
    never as a sign agreement -- §1 item 5's own 164 VP defect is the
    standing reminder that a sign test passes on exactly this class of
    bug. Reuses `_make_probe_fixture` (the headline test's own fixture)
    plus one already-slotted pick B receives alongside `outgoing_
    players`, priced in B's own currency via the real `trade_decisions.
    _price_pick` -- never a hand-picked constant -- exactly as `evaluate_
    trade`'s own `in:pick` term prices it."""
    snapshot, ctx, target = _make_probe_fixture()
    team_a = snapshot.team_by_id(1)
    team_b = snapshot.team_by_id(2)
    cache = ValuationCache()
    next_season = ctx.next_season
    all_players = (*team_a.roster, *team_b.roster)
    fv_b = cache.fit_values(team_b, all_players, ctx)
    b_players_ascending = sorted(team_b.roster, key=lambda rp: (fv_b[rp.id], rp.id))

    partner = gm_service._PartnerTerms(
        team_id=2,
        loyalty=gm_service.PROFILES[ctx.philosophy_by_team[2]].loyalty,
        window=ctx.window_by_team[2],
        room_dollars=ctx.room_by_team[2],
        over_cap=ctx.room_by_team[2] < 0,
        fv_by_player_id=fv_b,
        players_ascending=tuple(b_players_ascending),
    )
    outgoing_players = (team_a.roster[0], team_a.roster[1])
    # An already-slotted pick (real `pick_number`) so `_price_pick` needs no
    # `LeagueSnapshot`-based projection -- isolates this test from the
    # standings-ranking machinery, which §5.7 item 1's own fixture doesn't
    # exercise either.
    pick = DraftPick(
        id=9001, league_id=_LEAGUE_ID, season=next_season, round=1,
        original_team_id=1, current_team_id=1, pick_number=5,
        used_for_player_id=None, protection_type=ProtectionType.NONE,
        protection_through=None, swap_with_pick_id=None, conveyed=False,
        rolls_over_to_season=None,
    )
    p_in = gm_service.trade_decisions._price_pick(
        pick, ctx.philosophy_by_team[2], ctx.window_by_team[2],
        season=ctx.season, rookie_scale=ctx.rookie_scale, cap_settings=ctx.cap_settings,
        league_snapshot=snapshot, tuning=ctx.tuning,
    )
    assert p_in > 0.0, "fixture sanity: the pick must carry real value or this test proves nothing"

    probe = gm_service._probe_package(
        target, outgoing_players, partner=partner, incoming_pick_value=p_in,
        next_season=next_season, next_season_cap_settings=ctx.next_season_cap_settings,
        proposer_over_cap=ctx.room_by_team[1] < 0, tuning=ctx.tuning,
    )

    record_b = gm_service.trade_decisions.evaluate_trade(
        team_b,
        AssetBundle(players=outgoing_players, picks=(pick,)),
        AssetBundle(players=(target, *probe.throw_ins)),
        ctx.philosophy_by_team[2], ctx.window_by_team[2],
        league_rng_seed=ctx.league_rng_seed, season=ctx.season, phase=ctx.phase,
        cap_settings=ctx.cap_settings, next_season=next_season,
        next_season_cap_settings=ctx.next_season_cap_settings,
        next_season_room_dollars=ctx.room_by_team[2],
        rookie_scale=ctx.rookie_scale, league_snapshot=snapshot, tuning=ctx.tuning,
    )
    assert not record_b.verdict.startswith("illegal:"), (
        "fixture sanity: the probed pick-bearing package must actually be legal on "
        "B's side, or record_b.score/threshold are both trivially 0.0 and the "
        "magnitude assertion below would pass vacuously"
    )
    assert abs(probe.value_gap - (record_b.score - record_b.threshold)) < 1e-9


def test_probe_package_pick_value_defaults_to_zero_and_is_additive_on_s_in_b():
    """A direct, hand-computed regression on the scalar's own arithmetic
    (Decision P3-5): passing `incoming_pick_value` moves `value_gap` by
    exactly that amount relative to the same call without it -- and the
    unpassed default (`0.0`) reproduces the pre-Phase-3 value exactly,
    which is the property every existing (pickless) call site of `_probe_
    package` relies on for byte-identical output. `loyalty != 1.0` on
    `partner` is deliberate, not incidental: it is what makes this test
    able to catch the exact `(loyalty - 1) * P_out`-shaped error Decision
    P3-11 rules out (a naive `+ partner.loyalty * incoming_pick_value`
    would pass a `loyalty == 1.0` fixture identically to the correct
    unweighted `+ incoming_pick_value` and this test would not discriminate
    them -- the magnitude test elsewhere in this file uses a real,
    non-uniform `partner.loyalty` too, but this one should not rely on
    that alone)."""
    next_season = _SEASON + 1
    target = _salary_player(1, 5_000_000, team_id=2)
    outgoing_players = (_salary_player(10, 3_000_000, team_id=1),)
    b_players_ascending = (_salary_player(2, 1_000_000, team_id=2),)
    fv_by_player_id = {
        target.id: 50.0, b_players_ascending[0].id: 5.0, outgoing_players[0].id: 10.0,
    }
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.15, window=CompetitiveWindow.WIN_NOW, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_by_player_id, players_ascending=b_players_ascending,
    )
    kwargs = dict(
        partner=partner, next_season=next_season, next_season_cap_settings=CAP_SETTINGS,
        proposer_over_cap=False, tuning=DEFAULT_TUNING,
    )

    baseline = gm_service._probe_package(target, outgoing_players, **kwargs)
    defaulted = gm_service._probe_package(
        target, outgoing_players, incoming_pick_value=0.0, **kwargs
    )
    with_pick = gm_service._probe_package(
        target, outgoing_players, incoming_pick_value=42.0, **kwargs
    )

    assert defaulted.value_gap == baseline.value_gap
    assert abs((with_pick.value_gap - baseline.value_gap) - 42.0) < 1e-9


def test_propose_packages_bs_outgoing_bundle_never_carries_a_pick():
    """§11 Stage B step 5's verify #4 -- the reachability argument Decision
    P3-11 rests on for cutting `outgoing_pick_value`: B's outgoing bundle
    (`cand.incoming_a`, from A's own perspective -- what A receives) must
    carry `picks == ()` in every candidate `propose_packages` can produce.
    Swept across a real, generated league's full ordered-pair cross
    product rather than asserted from a single hand-built case, so a
    future change to `propose_packages` that accidentally gives B an
    outgoing pick is caught immediately rather than silently reviving the
    parameter this design explicitly declined to build."""
    from tools.gm_harness.league import generate_gm_league
    from tools.gm_harness.state import build_ctx, build_snapshot

    state = generate_gm_league(league_id=1, season=_SEASON, rng_seed=1, players_per_team=15)
    snapshot = build_snapshot(state)
    ctx = build_ctx(state, phase=Phase.REGULAR_SEASON)
    cache = gm_service.ValuationCache()
    team_ids = [t.id for t in snapshot.teams]

    candidates_scanned = 0
    a_side_pick_bearing = 0
    for a_id in team_ids:
        for b_id in team_ids:
            if a_id == b_id:
                continue
            for cand in propose_packages(a_id, b_id, snapshot, ctx, cache):
                candidates_scanned += 1
                assert cand.incoming_a.picks == (), (
                    f"B (team {b_id}) carries an outgoing pick in a candidate "
                    f"proposed by A (team {a_id}) -- Decision P3-11 assumes this is "
                    f"unreachable; if it now happens, outgoing_pick_value needs to "
                    f"be built after all"
                )
                if cand.outgoing_a.picks:
                    a_side_pick_bearing += 1

    assert candidates_scanned > 0
    assert a_side_pick_bearing > 0, (
        "fixture sanity: this sweep must include at least one variant2 candidate "
        "(A's outgoing bundle carrying the pick), or the absence check above isn't "
        "exercising a corpus where a pick is ever in play at all"
    )


def test_probe_package_uses_the_same_throw_ins_the_caller_will_ship():
    """§5.7 item 2 -- `_probe_package.throw_ins` must be byte-identical to
    a direct `_build_throw_ins` call on the same arguments (round 10 F2's
    consistency guarantee), never a model of it."""
    next_season = _SEASON + 1
    target = _salary_player(1, 5_000_000, team_id=2)
    outgoing_players = (
        _salary_player(10, 3_000_000, team_id=1), _salary_player(11, 3_000_000, team_id=1),
    )
    overpay = _salary_player(2, 8_000_000, team_id=2)
    cheap = _salary_player(3, 500_000, team_id=2)
    b_players_ascending = (overpay, cheap)
    fv_by_player_id = {
        target.id: 50.0, overpay.id: -30.0, cheap.id: 5.0,
        outgoing_players[0].id: 10.0, outgoing_players[1].id: 10.0,
    }
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.WIN_NOW, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_by_player_id, players_ascending=b_players_ascending,
    )
    probe = gm_service._probe_package(
        target, outgoing_players, partner=partner, next_season=next_season,
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=False, tuning=DEFAULT_TUNING,
    )
    expected_occupying = sum(
        1 for rp in outgoing_players if gm_service._occupies_next_season(rp, next_season)
    )
    expected = gm_service._build_throw_ins(
        target, outgoing_players, b_players_ascending, next_season,
        outgoing_occupying=expected_occupying,
    )
    assert probe.throw_ins == expected


def test_value_priority_orders_ranks_by_partner_value_per_unit_of_proposer_cost():
    """§5.7 item 3 -- the single most important test in this section
    (round 10 F3): the Dantzig ratio (`fv_B / fv_A` descending) must win
    over raw `fv_B`. `X` has the higher ratio (2.0 vs 1.5) despite `Y`
    having a lower absolute `fv_B` -- a regression to `a_players_
    ascending` (Option B, round 10 §3) would put neither `X` nor `Y` in
    this relative order at all, since ascending-`fv_A` order here is
    `(Y, X)` (Y's `fv_A=10` is smaller than X's `fv_A=100`)."""
    x = _salary_player(1, 1_000_000, team_id=1)
    y = _salary_player(2, 1_000_000, team_id=1)
    fv_a = {x.id: 100.0, y.id: 10.0}
    fv_b = {x.id: 200.0, y.id: 15.0}
    ratio_order, _delivery_order = gm_service._value_priority_orders(
        (y, x), fv_a_by_player_id=fv_a, fv_b_by_player_id=fv_b,
    )
    assert ratio_order[0] is x


def test_value_priority_orders_puts_negative_value_to_proposer_players_first():
    """§5.7 item 4 -- the tier-0 free-surplus preamble (round 10 F3): a
    candidate A doesn't want (`fv_A <= 0`) but B does (`fv_B > 0`) must
    outrank a positive-`fv_A` candidate with a larger `fv_B`, since the
    ratio itself is meaningless for the tier-0 candidate (a non-positive
    divisor)."""
    surplus = _salary_player(1, 1_000_000, team_id=1)
    costly = _salary_player(2, 1_000_000, team_id=1)
    fv_a = {surplus.id: -50.0, costly.id: 5.0}
    fv_b = {surplus.id: 30.0, costly.id: 100.0}
    ratio_order, _delivery_order = gm_service._value_priority_orders(
        (surplus, costly), fv_a_by_player_id=fv_a, fv_b_by_player_id=fv_b,
    )
    assert ratio_order[0] is surplus


def test_value_priority_orders_excludes_players_the_partner_values_at_or_below_zero():
    """§5.7 item 5 -- round 10 F3's sign argument, pinned in the suite:
    a candidate B values at or below zero can only move the running sum
    away from B's bar and must appear in neither ordering."""
    excluded = _salary_player(1, 1_000_000, team_id=1)
    included = _salary_player(2, 1_000_000, team_id=1)
    fv_a = {excluded.id: 10.0, included.id: 10.0}
    fv_b = {excluded.id: -80.0, included.id: 40.0}
    ratio_order, delivery_order = gm_service._value_priority_orders(
        (excluded, included), fv_a_by_player_id=fv_a, fv_b_by_player_id=fv_b,
    )
    assert excluded not in ratio_order
    assert excluded not in delivery_order
    assert included in ratio_order
    assert included in delivery_order


def test_value_priority_orders_tie_breaks_on_player_id():
    """§5.7 item 6 -- both orderings' keys must end in `a.id` (round 10
    §5.5's determinism rule), or a genuine tie is metric-6-unsafe."""
    p1 = _salary_player(5, 1_000_000, team_id=1)
    p2 = _salary_player(3, 1_000_000, team_id=1)
    fv_a = {p1.id: 10.0, p2.id: 10.0}
    fv_b = {p1.id: 20.0, p2.id: 20.0}
    ratio_order, delivery_order = gm_service._value_priority_orders(
        (p1, p2), fv_a_by_player_id=fv_a, fv_b_by_player_id=fv_b,
    )
    assert ratio_order == (p2, p1)
    assert delivery_order == (p2, p1)


def test_build_outgoing_stops_at_the_first_set_that_clears_the_partner_bar():
    """§5.7 item 7 -- a single candidate whose `fv_B` alone clears the bar
    must be returned alone, not padded to the cap."""
    next_season = _SEASON + 1
    target = _salary_player(1, 5_000_000, team_id=2)
    cand_good = _salary_player(10, 1_000_000, team_id=1)
    cand_other = _salary_player(11, 1_000_000, team_id=1)
    fv_a = {cand_good.id: 10.0, cand_other.id: 10.0}
    fv_b = {target.id: 100.0, cand_good.id: 150.0, cand_other.id: 80.0}
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_b, players_ascending=(target,),
    )
    result = gm_service._build_outgoing_variant1(
        target, (cand_good, cand_other), next_season, partner=partner, fv_a_by_player_id=fv_a,
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=False, tuning=DEFAULT_TUNING,
    )
    assert result == (cand_good,)


def test_build_outgoing_falls_back_to_delivery_order_when_ratio_order_cannot_clear_within_cap():
    """§5.7 item 8 (round 10 F4) -- three tiny-but-high-ratio candidates
    burn the ratio ordering's whole cap without clearing the bar; the
    `fv_B`-descending fallback clears it in 2. Deleting the second pass
    would leave this fixture returning the tiny trio's best effort
    instead."""
    next_season = _SEASON + 1
    target = _salary_player(1, 5_000_000, team_id=2)
    tiny1 = _salary_player(201, 500_000, team_id=1)
    tiny2 = _salary_player(202, 500_000, team_id=1)
    tiny3 = _salary_player(203, 500_000, team_id=1)
    big1 = _salary_player(204, 500_000, team_id=1)
    big2 = _salary_player(205, 500_000, team_id=1)
    fv_a = {tiny1.id: 1.0, tiny2.id: 1.0, tiny3.id: 1.0, big1.id: 50.0, big2.id: 50.0}
    fv_b = {
        target.id: 100.0, tiny1.id: 10.0, tiny2.id: 9.0, tiny3.id: 8.0,
        big1.id: 100.0, big2.id: 90.0,
    }
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_b, players_ascending=(target,),
    )
    a_players_ascending = (tiny1, tiny2, tiny3, big1, big2)
    result = gm_service._build_outgoing_variant1(
        target, a_players_ascending, next_season, partner=partner, fv_a_by_player_id=fv_a,
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=False, tuning=DEFAULT_TUNING,
    )
    assert result == (big1, big2)
    clears = gm_service._probe_package(
        target, result, partner=partner, next_season=next_season,
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=False, tuning=DEFAULT_TUNING,
    )
    assert clears.value_gap >= 0.0


def test_build_outgoing_skips_a_candidate_that_would_breach_the_partners_matching_ceiling():
    """§5.7 item 9 (round 10 F5/F6) -- the fix's own new failure mode.
    `fat` has by far the best ratio but its salary alone breaches B's
    matching ceiling; the admission rule must skip it (never a stop
    condition) and fall through to `good`. Mutual mutation control with
    the sibling test below."""
    next_season = _SEASON + 1
    target = _salary_player(1, 5_000_000, team_id=2)
    fat = _salary_player(301, 20_000_000, team_id=1)
    good = _salary_player(302, 1_000_000, team_id=1)
    fv_a = {fat.id: 10.0, good.id: 10.0}
    fv_b = {target.id: 100.0, fat.id: 1000.0, good.id: 50.0}
    partner_over_cap = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=True, fv_by_player_id=fv_b, players_ascending=(target,),
    )
    result = gm_service._build_outgoing_variant1(
        target, (fat, good), next_season, partner=partner_over_cap, fv_a_by_player_id=fv_a,
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=False, tuning=DEFAULT_TUNING,
    )
    assert fat not in result
    final_probe = gm_service._probe_package(
        target, result, partner=partner_over_cap, next_season=next_season,
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=False, tuning=DEFAULT_TUNING,
    )
    assert final_probe.matching_ceiling_ok is True


def test_build_outgoing_ignores_the_matching_ceiling_when_the_partner_is_under_the_cap():
    """§5.7 item 10 -- same fixture as the sibling test above, `partner.
    over_cap = False`: `fat` must now be taken (it has the best ratio and
    clears the bar alone). Neither test alone distinguishes "the rule
    works" from "there was only one real candidate" -- together they do."""
    next_season = _SEASON + 1
    target = _salary_player(1, 5_000_000, team_id=2)
    fat = _salary_player(301, 20_000_000, team_id=1)
    good = _salary_player(302, 1_000_000, team_id=1)
    fv_a = {fat.id: 10.0, good.id: 10.0}
    fv_b = {target.id: 100.0, fat.id: 1000.0, good.id: 50.0}
    partner_under_cap = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_b, players_ascending=(target,),
    )
    result = gm_service._build_outgoing_variant1(
        target, (fat, good), next_season, partner=partner_under_cap, fv_a_by_player_id=fv_a,
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=False, tuning=DEFAULT_TUNING,
    )
    assert fat in result


def test_build_outgoing_appends_salary_until_the_matching_floor_is_met():
    """§5.7 item 11 -- `good` alone clears B's value bar but is far too
    cheap to satisfy A's own matching floor (A is over the cap). Phase 1's
    `ratio_order` takes `good` first (`fv_B/fv_A` = 1000/10 = 100, vs
    `extra1`'s 1/1000 = 0.001), so `chosen = [good]` already clears the
    bar before phase 2 ever runs -- `extra1` is never "ordered ahead of
    `good`" anywhere (critic round 42 B2's nit: the prior docstring here
    was self-contradictory, claiming exactly that in one clause and "it
    appears second there" in the next). Phase 2 then walks
    `a_players_ascending` in the literal given order `(good, extra1)`,
    skips `good` via `chosen_ids` (already chosen in phase 1), and appends
    `extra1` -- its huge salary satisfies A's matching floor, and its own
    tiny positive `fv_B` keeps `s_in_b` well above threshold so the bar
    stays cleared."""
    next_season = _SEASON + 1
    target = _salary_player(1, 20_000_000, team_id=2)
    good = _salary_player(401, 1_000_000, team_id=1)
    extra1 = _salary_player(402, 20_000_000, team_id=1)
    fv_a = {good.id: 10.0, extra1.id: 1000.0}
    fv_b = {target.id: 100.0, good.id: 1000.0, extra1.id: 1.0}
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_b, players_ascending=(target,),
    )
    result = gm_service._build_outgoing_variant1(
        target, (good, extra1), next_season, partner=partner, fv_a_by_player_id=fv_a,
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=True, tuning=DEFAULT_TUNING,
    )
    assert set(rp.id for rp in result) == {good.id, extra1.id}
    final_probe = gm_service._probe_package(
        target, result, partner=partner, next_season=next_season,
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=True, tuning=DEFAULT_TUNING,
    )
    assert final_probe.matching_floor_ok is True


def test_build_outgoing_phase_two_refuses_a_filler_that_would_un_clear_the_partner_bar():
    """§5.7 item 12 (round 10 F6's second admission rule) -- the subtlest
    hole in the whole design. `bad_filler` carries a huge salary (would
    satisfy A's matching floor) but a hugely negative `fv_B` (would
    un-clear the bar `good` alone already cleared); phase 2 must refuse
    it and leave the returned set still clearing the bar, even though the
    floor then goes unmet."""
    next_season = _SEASON + 1
    target = _salary_player(1, 20_000_000, team_id=2)
    good = _salary_player(501, 1_000_000, team_id=1)
    bad_filler = _salary_player(502, 30_000_000, team_id=1)
    fv_a = {good.id: 10.0, bad_filler.id: 500.0}
    fv_b = {target.id: 100.0, good.id: 1000.0, bad_filler.id: -2000.0}
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_b, players_ascending=(target,),
    )
    result = gm_service._build_outgoing_variant1(
        target, (good, bad_filler), next_season, partner=partner, fv_a_by_player_id=fv_a,
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=True, tuning=DEFAULT_TUNING,
    )
    assert bad_filler not in result
    final_probe = gm_service._probe_package(
        target, result, partner=partner, next_season=next_season,
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=True, tuning=DEFAULT_TUNING,
    )
    assert final_probe.value_gap >= 0.0


def test_propose_packages_populates_the_partners_cache_entries_with_the_partners_own_matrix():
    """§5.7 item 13 -- the real cache-key guard `HANDOFF.md`'s trap asks
    for, closing round 10 F5/§5.6's `(partner_id, A's roster)` mispairing
    hazard directly (`ValuationCache.fit_values` closes it structurally;
    this test closes it empirically). Uses `_make_small_league`, whose
    `test_valuation_cache_fit_value_key_ignores_the_matrix_argument_
    poisoning_hazard` sibling test above already establishes team 1's and
    team 2's `NeedMatrix`es genuinely differ."""
    snapshot = _make_small_league()
    ctx = _make_ctx(snapshot)
    team_a = snapshot.team_by_id(1)
    team_b = snapshot.team_by_id(2)
    matrix_a = build_need_matrix(team_a, tuning=ctx.tuning)
    matrix_b = build_need_matrix(team_b, tuning=ctx.tuning)
    assert matrix_a != matrix_b  # sanity: reconfirmed for this fixture pair

    cache = ValuationCache()
    propose_packages(1, 2, snapshot, ctx, cache)

    any_differs = False
    for a in team_a.roster:
        expected_under_b = gm_service.trade_decisions.fit_value_for(
            a, matrix_b, league_rng_seed=ctx.league_rng_seed, team_id=2, season=ctx.season,
            phase=ctx.phase, philosophy=ctx.philosophy_by_team[2], window=ctx.window_by_team[2],
            cap_settings=ctx.cap_settings, tuning=ctx.tuning,
        )
        expected_under_a = gm_service.trade_decisions.fit_value_for(
            a, matrix_a, league_rng_seed=ctx.league_rng_seed, team_id=2, season=ctx.season,
            phase=ctx.phase, philosophy=ctx.philosophy_by_team[2], window=ctx.window_by_team[2],
            cap_settings=ctx.cap_settings, tuning=ctx.tuning,
        )
        assert (2, a.id) in cache._fit_value
        assert abs(cache._fit_value[(2, a.id)] - expected_under_b) < 1e-9
        if abs(expected_under_a - expected_under_b) > 1e-9:
            any_differs = True
    assert any_differs, (
        "fixture sanity: at least one of A's players must get a genuinely different "
        "fit_value under matrix_a vs matrix_b, or this test cannot discriminate the "
        "mispairing it exists to catch"
    )


def test_valuation_cache_fit_values_derives_the_matrix_from_the_snapshot_it_is_given():
    """§5.7 item 14 -- direct unit test of the new accessor: `fit_values`'
    output for every player must equal a manually-assembled `fit_value(
    team.id, rp, need_matrix(team), ctx)`. Kills an implementation that
    takes a separate `matrix` argument (or a caller-supplied team_id
    decoupled from the snapshot), reopening round 10 F5/§5.6's hazard."""
    snapshot = _make_small_league()
    ctx = _make_ctx(snapshot)
    team = snapshot.team_by_id(1)
    cache = ValuationCache()
    result = cache.fit_values(team, team.roster, ctx)
    matrix = build_need_matrix(team, tuning=ctx.tuning)
    for rp in team.roster:
        assert result[rp.id] == cache.fit_value(team.id, rp, matrix, ctx)


def test_build_outgoing_degrades_without_raising_when_the_partner_values_nothing_the_proposer_has():
    """§5.7 item 15 -- every `fv_B(a) <= 0`: the constructor must degrade
    to `(a_players_ascending[0],)` (today's minimum behaviour), never
    raise and never return an empty tuple for a non-empty roster. The
    empty-roster case must return `()`, also without raising -- the small
    `--players-per-team` harness path round 9 §5.3 step 6 already
    established this rule for."""
    next_season = _SEASON + 1
    target = _salary_player(1, 5_000_000, team_id=2)
    a1 = _salary_player(10, 1_000_000, team_id=1)
    a2 = _salary_player(11, 1_000_000, team_id=1)
    fv_b = {target.id: 100.0, a1.id: -10.0, a2.id: -5.0}
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_b, players_ascending=(target,),
    )
    result = gm_service._build_outgoing_variant1(
        target, (a1, a2), next_season, partner=partner,
        fv_a_by_player_id={a1.id: 10.0, a2.id: 10.0},
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=False, tuning=DEFAULT_TUNING,
    )
    assert result == (a1,)

    empty_result = gm_service._build_outgoing_variant1(
        target, (), next_season, partner=partner, fv_a_by_player_id={},
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=False, tuning=DEFAULT_TUNING,
    )
    assert empty_result == ()


def test_build_outgoing_never_repeats_a_player_or_exceeds_the_outgoing_cap():
    """§5.7 item 16 -- a property assertion over a 6-candidate pool with
    both sides over the cap (so both phases run): no player may appear
    twice in the returned set (kills phase 2 re-admitting a phase-1
    pick -- the `chosen_ids` guard), and the cap is never exceeded."""
    next_season = _SEASON + 1
    target = _salary_player(1, 20_000_000, team_id=2)
    players = tuple(
        _salary_player(100 + i, 2_000_000 + i * 500_000, team_id=1) for i in range(6)
    )
    fv_a = {p.id: 5.0 + i for i, p in enumerate(players)}
    fv_b = {p.id: 50.0 + i * 10 for i, p in enumerate(players)}
    fv_by_player_id = {target.id: 30.0, **fv_b}
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.10, window=CompetitiveWindow.WIN_NOW, room_dollars=0,
        over_cap=True, fv_by_player_id=fv_by_player_id, players_ascending=(target,),
    )
    result = gm_service._build_outgoing_variant1(
        target, players, next_season, partner=partner, fv_a_by_player_id=fv_a,
        next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=True, tuning=DEFAULT_TUNING,
    )
    ids = [rp.id for rp in result]
    assert len(ids) == len(set(ids)), "no player may appear twice in the outgoing set"
    assert len(result) <= gm_service._MAX_OUTGOING_PLAYERS


# ---------------------------------------------------------------------------
# classify_all_windows -- reconstruction
# ---------------------------------------------------------------------------


def test_classify_all_windows_records_reconstruct_for_every_team():
    snapshot = _make_small_league()
    previous_by_team = {t.id: CompetitiveWindow.RETOOL for t in snapshot.teams}
    philosophy_by_team = {t.id: GmPhilosophy.TENDENCY_RESPECTER for t in snapshot.teams}

    result = classify_all_windows(
        snapshot, previous_by_team=previous_by_team, philosophy_by_team=philosophy_by_team,
    )

    assert len(result.records) == len(snapshot.teams)
    assert set(result.window_by_team) == {t.id for t in snapshot.teams}
    for record in result.records:
        assert record.reconstructs(tolerance=1e-6), record.reconstruction_error()
        assert record.verdict == result.window_by_team[record.team_id].value


def test_classify_all_windows_carries_a_pinned_team_through_unchanged():
    """Decision P5-67 -- a pinned team's window is CARRIED THROUGH from
    its own `previous_by_team` value, never reclassified, and gets no
    `DecisionRecord` (a record would describe a decision nobody made).
    Every other team is classified normally in the same pass, proving
    `pinned_team_ids` is scoped per-team, not a global switch."""
    snapshot = _make_small_league()
    team_ids = [t.id for t in snapshot.teams]
    pinned_id = team_ids[0]
    # A deliberately "wrong" previous value -- if the pin were not honored,
    # `classify_window`'s own real proposal for this synthetic roster would
    # not, in general, land back on WIN_NOW, so an unpinned re-classification
    # would very likely change it.
    previous_by_team = {t.id: CompetitiveWindow.WIN_NOW for t in snapshot.teams}
    philosophy_by_team = {t.id: GmPhilosophy.TENDENCY_RESPECTER for t in snapshot.teams}

    result = classify_all_windows(
        snapshot,
        previous_by_team=previous_by_team,
        philosophy_by_team=philosophy_by_team,
        pinned_team_ids=frozenset({pinned_id}),
    )

    assert result.window_by_team[pinned_id] == CompetitiveWindow.WIN_NOW
    assert {r.team_id for r in result.records} == set(team_ids) - {pinned_id}
    assert len(result.records) == len(team_ids) - 1
    # every non-pinned team was classified normally and still reconstructs
    for record in result.records:
        assert record.reconstructs(tolerance=1e-6), record.reconstruction_error()


# ---------------------------------------------------------------------------
# encode_decision_record -- determinism, no wall-clock field
# ---------------------------------------------------------------------------


def test_encode_decision_record_is_byte_identical_and_carries_no_timestamp():
    record = DecisionRecord(
        team_id=1, philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=CompetitiveWindow.WIN_NOW,
        subject="player:42", verdict="offer", score=12.5, threshold=10.0,
        terms=(ReasonTerm(label="market_salary", input_value=12.5, weight=1.0, contribution=12.5),),
    )

    line1 = encode_decision_record(
        record, league_id=1, season=_SEASON, phase=Phase.FREE_AGENCY, seq=7
    )
    line2 = encode_decision_record(
        record, league_id=1, season=_SEASON, phase=Phase.FREE_AGENCY, seq=7
    )

    assert line1 == line2
    assert "timestamp" not in line1
    assert "generated_at" not in line1

    payload = json.loads(line1)
    assert list(payload.keys()) == [
        "league_id", "season", "phase", "seq", "team_id", "philosophy", "window",
        "subject", "verdict", "score", "threshold", "terms",
    ]
    assert payload["terms"][0] == {
        "label": "market_salary", "input_value": 12.5, "weight": 1.0, "contribution": 12.5,
    }


# ---------------------------------------------------------------------------
# run_resign_window
# ---------------------------------------------------------------------------


def test_run_resign_window_constructs_an_offer_for_an_expiring_player():
    snapshot = _make_small_league(n_teams=1)
    ctx = _make_ctx(snapshot, phase=Phase.RESIGN_WINDOW)
    team = snapshot.teams[0]
    expiring_player = team.roster[0]

    offers = run_resign_window(
        snapshot, ctx, expiring_by_team={team.id: (expiring_player.id,)}
    )

    assert len(offers) == 1
    offer = offers[0]
    assert offer.team_id == team.id
    assert offer.player_id == expiring_player.id
    assert 1 <= offer.years <= 5
    assert offer.annual_salary > 0


def test_run_resign_window_skips_teams_with_no_expiring_players():
    snapshot = _make_small_league(n_teams=2)
    ctx = _make_ctx(snapshot, phase=Phase.RESIGN_WINDOW)
    team = snapshot.teams[0]
    offers = run_resign_window(
        snapshot, ctx, expiring_by_team={team.id: (team.roster[0].id,)}
    )
    assert all(o.team_id == team.id for o in offers)


def test_run_resign_window_builds_no_offer_for_a_team_with_no_legal_room_under_apron_2():
    """Round 49's own regression gap (critic round 49 B1-a): re-injecting
    `if False:` in place of `gm_service.py:1528`'s `if capacity <
    ctx.next_season_cap_settings.min_salary: continue` left the full 918-
    test suite passing unchanged -- nothing named `run_resign_window`
    directly exercised the skip gate. This fixture is critic round 48's
    own repro figure, `apron1_room = -30,000,000` ($18.1M past `apron_2`
    on this fixture's basis), which drives `signing.legal_capacity`'s hard
    `apron_2` clamp to `-18,121,000` -- well below `min_salary` -- so the
    skip must fire and this team's sole expiring incumbent must produce
    zero pending offers, not a full-MLE offer (the exact silent
    re-inflation the gate exists to prevent, architect round 14 §5.3's
    named H5 trap).

    **Kills the exact mutation critic round 49 named**: with the real
    `if capacity < ...: continue` restored, this asserts `len(offers) ==
    0`; temporarily replacing that line with `if False: continue` makes
    this test fail (`AssertionError: assert 1 == 0`, a $8,546,329 offer
    built from the negative, still-clamped `capacity`) -- verified by
    hand, then reverted."""
    snapshot = _make_small_league(n_teams=1)
    ctx = _make_ctx(snapshot, phase=Phase.RESIGN_WINDOW, apron1_room=-30_000_000)
    team = snapshot.teams[0]
    expiring_player = team.roster[0]

    offers = run_resign_window(
        snapshot, ctx, expiring_by_team={team.id: (expiring_player.id,)}
    )

    assert offers == ()


# ---------------------------------------------------------------------------
# run_free_agency
# ---------------------------------------------------------------------------


def test_run_free_agency_signs_a_max_worthy_free_agent_at_a_large_offer():
    snapshot = _make_small_league(n_teams=1)
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=60_000_000)
    star_fa = make_roster_player(
        9999, age=27, season=_SEASON, overall_cached=92, primary_position=Position.C,
        team_id=None, peak_age_start=27, peak_age_end=31,
    )

    result = run_free_agency(snapshot, ctx, pool=(star_fa,), pending_offers=())

    assert len(result.signings) == 1
    signing = result.signings[0]
    assert signing.player_id == star_fa.id
    assert signing.team_id == snapshot.teams[0].id
    # A 92-overall free agent with $60M of room should draw a well-above-
    # minimum offer -- not pinned to an exact dollar figure (the formula
    # itself is `signing.py`'s own tested surface), just a sanity bound.
    assert signing.annual_salary > CAP_SETTINGS.min_salary * 5
    assert any(r.subject == f"player:{star_fa.id}" for r in result.records)


def test_run_free_agency_merges_a_pending_offer_into_round_one():
    snapshot = _make_small_league(n_teams=1)
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=5_000_000)
    team = snapshot.teams[0]
    matrix = build_need_matrix(team, tuning=ctx.tuning)
    expiring_player = make_roster_player(
        9998, age=29, season=_SEASON, overall_cached=70, primary_position=Position.C,
        team_id=None, peak_age_start=27, peak_age_end=31,
    )
    pending_offer, _record = gm_service.construct_offer(
        expiring_player, matrix, team_id=team.id, philosophy=ctx.philosophy_by_team[team.id],
        window=ctx.window_by_team[team.id], cap_settings=ctx.next_season_cap_settings,
        room=ctx.room_by_team[team.id], season=ctx.next_season,
    )

    result = run_free_agency(
        snapshot, ctx, pool=(expiring_player,), pending_offers=(pending_offer,)
    )

    assert len(result.signings) == 1
    assert result.signings[0].player_id == expiring_player.id
    assert result.signings[0].team_id == team.id
    # Nit 6 (critic round 17): this scenario's whole point is that
    # `expiring_player` is BOTH a pending (round-1-merged) offer target AND
    # this same team's own #1 fit-value target from the pool (its only
    # pool player) -- before the fix, both code paths independently called
    # `construct_offer` for the identical (team, player) pair, so exactly
    # one team double-offered the same free agent in round 1.
    player_records = [r for r in result.records if r.subject == f"player:{expiring_player.id}"]
    assert len(player_records) == 1, "team must not double-offer the same player in one round"


def test_enforce_roster_floor_signs_up_to_roster_min():
    snapshot = _make_small_league(n_teams=1)
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY)
    team = snapshot.teams[0]
    needed = ctx.next_season_cap_settings.roster_min - len(team.roster)
    assert needed > 0, "fixture must start below roster_min for this test to mean anything"

    remaining_pool = {
        rp.id: rp
        for rp in (
            make_roster_player(
                8000 + i, age=25, season=_SEASON, overall_cached=60 + i,
                primary_position=Position.PG, team_id=None,
                peak_age_start=27, peak_age_end=31,
            )
            for i in range(needed + 3)  # more pool depth than strictly needed
        )
    }
    # NOT `_next_season_roster_count` -- deliberately. `roster_count` is an
    # in/out parameter `_enforce_roster_floor` mutates, not something it
    # derives, so an arbitrary starting count is legitimate for exercising
    # this function's own loop/salary/years/pool-depletion mechanics in
    # isolation. The real production seed basis is `_next_season_roster_
    # count` (see `run_free_agency`'s own tests for that).
    roster_count = {team.id: len(team.roster)}
    room_remaining = {team.id: ctx.room_by_team[team.id]}
    matrix_by_team = {team.id: build_need_matrix(team, tuning=ctx.tuning)}

    signings, records = gm_service._enforce_roster_floor(
        snapshot, ctx, remaining_pool, roster_count, room_remaining, matrix_by_team
    )

    assert len(signings) == needed
    assert len(records) == needed
    assert roster_count[team.id] == ctx.next_season_cap_settings.roster_min
    assert all(s.annual_salary == ctx.next_season_cap_settings.min_salary for s in signings)
    assert all(s.years == 1 for s in signings)
    assert all(r.reconstructs(tolerance=1e-6) for r in records)
    assert len(remaining_pool) == 3  # exactly `needed` were popped


def test_enforce_roster_floor_does_not_credit_a_signing_that_will_not_occupy_next_season():
    """Critic round 51 (B2). `_enforce_roster_floor:1913` (per that round's
    own line numbers) incremented `roster_count` unconditionally for every
    floor signing, with no retirement check at all -- the identical
    overcount class Decision P2-37 fixed on the read side (`_occupies_
    next_season`), left open on the write side. Fixture: the team's ENTIRE
    remaining pool is a single free agent who will be age 41 at `next_
    season`, past `ctx.retirement_age=40` -- the exact overcount shape the
    known reproducer (`rng_seed=2636832537024722024`, team 28, season
    2029) hit.

    **The signing must still happen** (legal to sign a player about to
    retire -- this function's docstring's own B2 correction says so
    explicitly), but `roster_count` must NOT credit it, so the team is
    left honestly short of `roster_min` rather than believing the floor
    was satisfied. `remaining_pool.pop` still runs regardless of the
    guard, so the loop terminates via pool exhaustion (`while ... and
    remaining_pool`), not via a false floor-satisfied read -- exactly the
    termination argument this fix's own docstring makes."""
    snapshot = _make_small_league(n_teams=1)
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, retirement_age=40)
    team = snapshot.teams[0]
    needed = ctx.next_season_cap_settings.roster_min - len(team.roster)
    assert needed > 0, "fixture must start below roster_min for this test to mean anything"

    near_retirement = make_roster_player(
        8500, age=41, season=ctx.next_season, overall_cached=60,
        primary_position=Position.PG, team_id=None, peak_age_start=27, peak_age_end=31,
    )
    remaining_pool = {near_retirement.id: near_retirement}
    starting_count = len(team.roster)
    roster_count = {team.id: starting_count}
    room_remaining = {team.id: ctx.room_by_team[team.id]}
    matrix_by_team = {team.id: build_need_matrix(team, tuning=ctx.tuning)}

    signings, records = gm_service._enforce_roster_floor(
        snapshot, ctx, remaining_pool, roster_count, room_remaining, matrix_by_team
    )

    assert len(signings) == 1, "the near-retirement free agent must still be signed"
    assert signings[0].player_id == near_retirement.id
    assert roster_count[team.id] == starting_count, (
        "a signing that will not occupy a next-season slot must not be credited "
        "toward roster_count -- crediting it would let this loop (or a caller "
        "reading roster_count afterward) believe the floor was met when it was not"
    )
    assert roster_count[team.id] < ctx.next_season_cap_settings.roster_min, (
        "the team is genuinely still below floor -- an honest gap, not a silent overcount"
    )
    assert remaining_pool == {}, "the sole candidate must still be popped from the shared pool"


def test_run_free_agency_paid_loop_does_not_credit_a_near_retirement_signing_toward_roster_max():
    """Critic round 51 (B2), the second write site. `run_free_agency`'s own
    paid-signing resolution loop (`:2377` per that round's own line
    numbers) incremented `roster_count` unconditionally too -- the same
    overcount, reachable through the normal (non-floor) signing path.

    One team, 14 real covering bodies (`roster_max=15`, one open slot),
    `ctx.retirement_age=40`. The pool holds exactly two free agents:
    `near_retirement` (age 41 at `next_season`, `overall_cached=92` so it
    is priced well above `second_candidate` and is therefore resolved
    FIRST in `players_with_offers`' descending-`guaranteed_total` order)
    and `second_candidate` (age 27, ordinary). If `near_retirement`'s
    signing wrongly credits `roster_count` (the bug), the team hits
    `roster_max` immediately and `second_candidate`'s offer is skipped by
    this SAME round's own `roster_count[team_id] >= roster_max` gate
    (`:2318`), and never revisited (nothing decrements `roster_count`
    afterward, and the offer-construction gate at the top of every later
    round reads the same permanently-inflated count) -- only 1 signing
    total. Fixed (uncredited): the team still shows 14/15 after signing
    `near_retirement`, so `second_candidate`'s offer clears the gate in
    the very same round's resolution loop and both are signed -- 2
    signings total, `roster_max` reached honestly by the SECOND, real
    occupying body.

    **`second_candidate`'s `overall_cached` re-probed under Slice 22-B**
    (architect round 23 §4.2-§4.3, Decision P2-42 -- `signing.py`'s
    `_YEARS_BASE_LO`/`_YEARS_BASE_HI` moved 1/4 -> 3/6). The fixture's own
    ordering guarantee (`near_retirement` priced above `second_candidate`)
    is a `guaranteed_total = annual_salary * years` comparison, and this
    slice roughly tripled a CONTEND-window (`_WINDOWS_CYCLE[0]`, this
    fixture's one team) mid-career signing's `years` (1 -> 3 at age 27) --
    at the previous `overall_cached=70`, `second_candidate`'s inflated
    `guaranteed_total` (~$63.6M) overtook `near_retirement`'s room-capped
    ~$50M ceiling and flipped the resolution order, which silently defeated
    this test's own premise rather than failing loudly on the property it
    exists to guard. Lowered to `50` -- on `market_salary_share`'s own
    curve this sits on the `min_salary` floor plateau (`ovr <= 55` all
    price identically at `$1,272,870`), so `second_candidate`'s
    `guaranteed_total` stays below ~$10.2M even at the outer bounds of
    `offer_years`'/`willingness`'s own clamps (`years <= 5`,
    `willingness <= 1.60`) -- an ordering that no future `offer_years`
    retune within its own `[1, 5]` clamp can flip back, unlike the
    previous `70` figure, which depended on a since-moved constant without
    saying so."""
    team = make_team(1)
    roster = _build_mixed_roster(
        1, n_covering=14, n_expiring=0, avoid_position=Position.PG, base_id=7000,
    )
    team_snapshot = build_team_snapshot(team, roster, (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_snapshot,))
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=50_000_000, retirement_age=40)
    assert gm_service._next_season_roster_count(team_snapshot, ctx.next_season) == 14, (
        "fixture sanity: exactly one open roster_max slot before either free agent signs"
    )

    near_retirement = make_roster_player(
        9100, age=41, season=ctx.next_season, overall_cached=92,
        primary_position=Position.PG, team_id=None, peak_age_start=27, peak_age_end=31,
    )
    second_candidate = make_roster_player(
        9101, age=27, season=_SEASON, overall_cached=50,
        primary_position=Position.PG, team_id=None, peak_age_start=27, peak_age_end=31,
    )

    result = run_free_agency(
        snapshot, ctx, pool=(near_retirement, second_candidate), pending_offers=()
    )

    signed_ids = {s.player_id for s in result.signings}
    assert near_retirement.id in signed_ids, (
        "signing a player about to age past retirement is legal and must still happen"
    )
    assert second_candidate.id in signed_ids, (
        "second_candidate's offer must not be blocked by a roster_max gate that near_"
        "retirement's own signing has no legitimate claim on -- it will not physically "
        "occupy a next-season slot, so it must not count toward the ceiling either"
    )
    assert len(result.signings) == 2


def test_run_free_agency_never_lets_a_team_spend_its_room_twice_in_one_round():
    """B1/B1b regression (critic round 17). Critic's exact repro shape: one
    team, $30,000,000 of room, multiple max-worthy free agents (effective
    ovr 92 -> `market_salary_share` pinned at its 35%-of-cap ceiling, so
    every offer's pre-floor figure comfortably clears $30M regardless of
    need/willingness) -- pre-fix, every offer that team made in the round
    was constructed against the same untouched $30M `room_remaining`, so
    both offers landed at the identical $30,000,000 CAP_SPACE ceiling and
    the resolution loop accepted both, a $60,000,000 commitment against
    $30,000,000 of real room. Post-fix: the first signing legitimately
    spends the $30M CAP_SPACE room; the second's already-baked $30,000,000
    offer is re-checked against the team's now-current capacity (the MLE
    ceiling, since room is exhausted) at accept time, is no longer
    affordable, and lapses to the next round, where a freshly-priced,
    MLE-capped offer is what actually signs."""
    snapshot = _make_small_league(n_teams=1)
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=30_000_000)

    star_1 = make_roster_player(
        9001, age=27, season=_SEASON, overall_cached=92, primary_position=Position.C,
        team_id=None, peak_age_start=27, peak_age_end=31,
    )
    star_2 = make_roster_player(
        9002, age=27, season=_SEASON, overall_cached=92, primary_position=Position.PF,
        team_id=None, peak_age_start=27, peak_age_end=31,
    )

    result = run_free_agency(snapshot, ctx, pool=(star_1, star_2), pending_offers=())

    fa_signings = [s for s in result.signings if s.player_id in (star_1.id, star_2.id)]
    assert len(fa_signings) == 2, "both stars are max-worthy and the pool has room for both"

    total_committed = sum(s.annual_salary for s in fa_signings)
    # The pre-fix bug produced $60,000,000 (both signings at the identical,
    # stale $30M ceiling) -- the fixed figure must be strictly less, and
    # bounded by one CAP_SPACE signing plus one MLE-capped signing, not
    # "however many times the stale room could be reused."
    assert total_committed < 60_000_000
    assert total_committed == 30_000_000 + CAP_SETTINGS.mle_amount

    salaries = sorted(s.annual_salary for s in fa_signings)
    assert salaries == [CAP_SETTINGS.mle_amount, 30_000_000]

    methods = {s.annual_salary: s.signing_method for s in fa_signings}
    assert methods[30_000_000] == gm_service.SigningMethod.CAP_SPACE
    assert methods[CAP_SETTINGS.mle_amount] == gm_service.SigningMethod.MLE
    # B1b: the tag must agree with the real ceiling for that method -- the
    # MLE-tagged signing's own salary IS the real `mle_amount`, not some
    # other, larger figure smuggled through under a wrong tag.
    assert methods[CAP_SETTINGS.mle_amount] == gm_service.SigningMethod.MLE


def test_run_free_agency_bounds_exceptions_to_once_per_pass_across_many_rounds():
    """B1/B1b, THIRD round (critic round 18). The two-free-agent test above
    passed under BOTH the round-17 "fix" and the real bug it failed to
    catch -- with only one more signable player after the first CAP_SPACE
    signing, the round-17 fix's re-check against `signing.legal_capacity`
    (a pure, stateless function that always returns the same constant for
    any `room <= 0`) coincidentally lands on the right number once. That
    function has no memory of whether an exception was already used, so it
    cannot bound repeated use across MORE than one subsequent signing --
    critic's real repro needed 10 free agents to expose it: 1 team, $30M
    room, 10 max-worthy (92-overall) free agents, roster starting well
    below `roster_max` (15) so all 10 can be signed. Pre-fix (mutation-
    tested independently, see the mutation test below): well over $100M
    committed, with the MLE-sized ceiling reused for signing after signing.
    Post-fix: exactly one CAP_SPACE, one MLE-capped, one bi-annual-capped,
    and the rest at the real, always-available minimum -- the per-pass
    `exceptions_used` state `_effective_capacity`/`_finalize_signing` own
    is what actually bounds this, not a faster re-check of the same
    stateless primitive."""
    snapshot = _make_small_league(n_teams=1, roster_size=3)
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=30_000_000)
    pool = tuple(
        make_roster_player(
            9100 + i, age=27, season=_SEASON, overall_cached=92,
            primary_position=list(Position)[i % len(list(Position))],
            team_id=None, peak_age_start=27, peak_age_end=31,
        )
        for i in range(10)
    )

    result = run_free_agency(snapshot, ctx, pool=pool, pending_offers=())

    fa_signings = [s for s in result.signings if 9100 <= s.player_id < 9110]
    assert len(fa_signings) == 10, "roster starts well below roster_max -- all 10 should sign"

    from collections import Counter

    method_counts = Counter(s.signing_method for s in fa_signings)
    assert method_counts[gm_service.SigningMethod.CAP_SPACE] == 1
    assert method_counts[gm_service.SigningMethod.MLE] == 1
    assert method_counts[gm_service.SigningMethod.BI_ANNUAL] == 1
    assert method_counts[gm_service.SigningMethod.MIN_EXCEPTION] == 7

    salaries_by_method = {s.signing_method: s.annual_salary for s in fa_signings}
    assert salaries_by_method[gm_service.SigningMethod.CAP_SPACE] == 30_000_000
    assert salaries_by_method[gm_service.SigningMethod.MLE] == CAP_SETTINGS.mle_amount
    assert salaries_by_method[gm_service.SigningMethod.BI_ANNUAL] == CAP_SETTINGS.bi_annual_amount
    assert all(
        s.annual_salary == CAP_SETTINGS.min_salary
        for s in fa_signings
        if s.signing_method == gm_service.SigningMethod.MIN_EXCEPTION
    )

    total_committed = sum(s.annual_salary for s in fa_signings)
    # Bounded: one of each once-per-pass exception plus room plus unlimited
    # real minimum-salary signings -- nowhere near critic's original
    # $156,936,000 (9 signings smuggled through at MLE-sized dollars).
    assert total_committed == (
        30_000_000 + CAP_SETTINGS.mle_amount + CAP_SETTINGS.bi_annual_amount
        + 7 * CAP_SETTINGS.min_salary
    )
    assert total_committed < 100_000_000


# ---------------------------------------------------------------------------
# Slice 12-A -- the incumbent (Bird-rights) bucket (Decision P2-20,
# architect round 11 §5.3, round 12 §5.4). Round 11 §5.5 items 1-3 live in
# `test_signing.py` (`legal_capacity` itself); items 4-8 and 10 live here
# (`_effective_capacity`/`_finalize_signing`/`run_resign_window`/`run_
# free_agency`, all of which carry per-pass STATE `legal_capacity` cannot
# see). **Item 9, the harness-level headline test, lives in `tests/unit/
# tools/gm_harness/test_season.py`** -- matching round 12's own precedent
# for its analogous headline test (`test_waiver_count_returns_to_the_no_
# trade_baseline_across_five_seasons`), not this file; a full 5-season
# committed-salary-book measurement needs the real season loop
# (`tools/gm_harness/season.py::run_seed`), which this file's hand-built
# small-league fixtures do not run.
# ---------------------------------------------------------------------------


def test_bird_bucket_is_per_incumbent_not_once_per_pass():
    """Round 11 §5.5 item 4 (unmodified in property/assertions; architect
    round 14 §5.3 mechanically requires `incumbent_years_pro` at the
    `_finalize_signing` call sites now that the Bird branch must evaluate
    the identical `bird_ceiling` expression `_effective_capacity` used --
    the two calls below now pass the same `incumbent_years_pro` already
    threaded to `_effective_capacity` two lines above each, nothing about
    the scenario or its assertions changes). Two incumbents on one
    over-cap team, both re-signed above room in the same pass, with ample
    apron room (never the binding constraint here -- see the companion
    apron-scarce test below for that). **Kills:** reusing `exceptions_
    used`'s once-per-pass machinery for the new bucket, which would make
    it a third MLE and cap the effect at one player."""
    team_id = 1
    room_remaining = {team_id: -80_000_000}
    exceptions_used: dict[int, set[str]] = {team_id: set()}
    apron_room_remaining = {team_id: 200_000_000}

    tier_10plus = round(CAP_SETTINGS.max_pct_10_plus / 100.0 * CAP_SETTINGS.salary_cap)
    tier_7_9 = round(CAP_SETTINGS.max_pct_7_9 / 100.0 * CAP_SETTINGS.salary_cap)

    capacity_1 = gm_service._effective_capacity(
        team_id, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=20_000_000, incumbent_years_pro=12,
        apron_room_remaining=apron_room_remaining,
    )
    assert capacity_1 == tier_10plus
    method_1 = gm_service._finalize_signing(
        team_id, capacity_1, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=20_000_000, incumbent_years_pro=12,
        apron_room_remaining=apron_room_remaining,
    )
    assert method_1 == gm_service.SigningMethod.BIRD

    # Second incumbent, same team, same pass -- must ALSO reach its own
    # full Bird ceiling (a different years-of-service tier), not a
    # leftover/exhausted MLE-sized fallback the way a once-per-pass
    # exception would leave it.
    capacity_2 = gm_service._effective_capacity(
        team_id, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=15_000_000, incumbent_years_pro=8,
        apron_room_remaining=apron_room_remaining,
    )
    assert capacity_2 == tier_7_9
    method_2 = gm_service._finalize_signing(
        team_id, capacity_2, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=15_000_000, incumbent_years_pro=8,
        apron_room_remaining=apron_room_remaining,
    )
    assert method_2 == gm_service.SigningMethod.BIRD
    assert apron_room_remaining[team_id] == 200_000_000 - capacity_1 - capacity_2
    assert exceptions_used[team_id] == set(), (
        "the Bird bucket must never touch exceptions_used -- that ledger is the MLE/"
        "bi-annual once-per-pass machinery, a different bucket entirely"
    )


def test_finalize_signing_tags_bird_and_decrements_apron_room():
    """Round 11 §5.5 item 5 (unmodified in property/assertions; same
    mechanical `incumbent_years_pro` threading as item 4 above --
    `incumbent_years_pro=10` gives a tier ceiling (`max_pct_10_plus`,
    54,126,450) comfortably above the `apron_room=50,000,000` clamp, so
    the binding ceiling is still `apron_room` and the $30M salary still
    authorizes BIRD, exactly as before). **Kills:** tagging `MLE`, or
    leaving apron room untouched (which would let one team re-sign its
    whole roster past `apron_2`)."""
    team_id = 1
    room_remaining = {team_id: -10_000_000}
    exceptions_used: dict[int, set[str]] = {team_id: set()}
    apron_room_remaining = {team_id: 50_000_000}
    salary = 30_000_000

    method = gm_service._finalize_signing(
        team_id, salary, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=12_000_000, incumbent_years_pro=10,
        apron_room_remaining=apron_room_remaining,
    )

    assert method == gm_service.SigningMethod.BIRD
    assert room_remaining[team_id] == -10_000_000 - salary, (
        "room may go negative -- that is the point"
    )
    assert apron_room_remaining[team_id] == 50_000_000 - salary
    assert exceptions_used[team_id] == set()


def test_a_non_incumbent_free_agent_cannot_reach_the_bird_bucket():
    """Round 11 §5.5 item 6. A pool free agent carrying a real contract-
    year matching `ctx.season` (so `_incumbent_prior_salary` WOULD return a
    salary if it were ever called for this player) but who is NOT one of
    `run_free_agency`'s own `pending_offers` pairs -- i.e. not this team's
    own re-sign. **Kills:** deriving eligibility from "the player's
    previous team" or "does this player have any contract" instead of
    strictly the pending-offer pair, which would let any team Bird-sign
    anyone who was ever under contract."""
    snapshot = _make_small_league(n_teams=1)
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=0)
    team = snapshot.teams[0]
    stale_contract = make_contract(
        9500, team_id=team.id, start_season=_SEASON, salaries=(25_000_000,)
    )
    fa_with_a_stale_contract = make_roster_player(
        9500, age=27, season=_SEASON, overall_cached=92, primary_position=Position.C,
        team_id=None, peak_age_start=27, peak_age_end=31, contract=stale_contract,
    )

    result = run_free_agency(
        snapshot, ctx, pool=(fa_with_a_stale_contract,), pending_offers=()
    )

    signing = next(s for s in result.signings if s.player_id == fa_with_a_stale_contract.id)
    assert signing.signing_method != gm_service.SigningMethod.BIRD
    ladder_only = max(
        CAP_SETTINGS.mle_amount, CAP_SETTINGS.bi_annual_amount, CAP_SETTINGS.min_salary
    )
    assert signing.annual_salary <= ladder_only, (
        "an ineligible player must be bounded by today's ladder, never the Bird tier ceiling"
    )


def test_run_resign_window_and_round_zero_price_the_same_incumbent_the_same_way():
    """Round 11 §5.5 item 7, rewritten per architect round 14 §5.5 (critic
    B5): the original only asserted the tag, so both of the critic's own
    mutations survived while the fixture's price silently collapsed
    $38,661,750 -> $14,104,000 (-63.5%). Now asserts **dollars**, not just
    the tag, at both call sites. **Kills, verified against the critic's
    own two mutations:** reverting `run_resign_window`'s `room=capacity`
    to `room=room`; dropping the incumbent kwargs from round 0's `_
    effective_capacity`. Neither may survive."""
    # Part 1: the two capacity formulas agree in kind at fresh/unspent
    # state -- `signing.legal_capacity` (`run_resign_window`'s own basis)
    # vs `gm_service._effective_capacity` (round 0's basis). They are
    # documented as NOT byte-identical once a pass has spent state, but at
    # round 0, with nothing yet spent, they must be numerically equal.
    # Asserted against an INDEPENDENTLY hand-derived number, not merely
    # against each other: `max_pct_7_9` (30%) x `salary_cap` (154,647,000)
    # = 46,394,100, the binding term at `apron_room = 60,000,000` (which
    # exceeds it, so the tier ceiling -- not the apron clamp -- binds; the
    # hard apron_2 clamp at `apron_room + GAP = 71,879,000` does not bind
    # either).
    room = -30_000_000
    apron_room = 60_000_000
    prior_salary = 12_000_000
    years_pro = 9
    expected_capacity = round(CAP_SETTINGS.max_pct_7_9 / 100.0 * CAP_SETTINGS.salary_cap)
    assert expected_capacity == 46_394_100

    cap_via_legal_capacity = gm_service.signing_decisions.legal_capacity(
        room, CAP_SETTINGS, incumbent_prior_salary=prior_salary,
        incumbent_years_pro=years_pro, apron_room=apron_room,
    )
    room_remaining = {1: room}
    exceptions_used: dict[int, set[str]] = {1: set()}
    apron_room_remaining = {1: apron_room}
    cap_via_effective_capacity = gm_service._effective_capacity(
        1, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=prior_salary, incumbent_years_pro=years_pro,
        apron_room_remaining=apron_room_remaining,
    )
    assert cap_via_legal_capacity == expected_capacity
    assert cap_via_effective_capacity == expected_capacity

    # Part 2: end-to-end -- an over-cap team's own incumbent, priced via
    # `run_resign_window`'s pending offer, then merged into `run_free_
    # agency`'s round-0 recompute for the SAME (team, player) pair. If
    # either call site were missed, the final executed signing would fall
    # back to an MLE-capped signing instead of a BIRD-tagged one -- and
    # (B5) now asserted by dollars at both sites, not just the tag.
    team = make_team(1)
    incumbent = make_roster_player(
        9600, age=31, season=_SEASON, overall_cached=90, primary_position=Position.C,
        team_id=team.id, peak_age_start=27, peak_age_end=31,
        contract=make_contract(9600, team_id=team.id, start_season=_SEASON, salaries=(20_000_000,)),
    )
    roster = (incumbent,) + _filler_roster(team.id, 11, avoid_position=Position.C, base_id=1000)
    team_snapshot = build_team_snapshot(team, roster, (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_snapshot,))
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=-40_000_000, apron1_room=80_000_000)
    # `incumbent.player.years_pro` is `make_roster_player`'s own default
    # (5, see tests/fixtures/sim.py) -> `max_pct_0_6` (25%) x salary_cap =
    # 38,661,750, comfortably below both the (generous) apron_1 headroom
    # and its own apron_2-basis hard clamp, so the Bird ceiling itself is
    # the binding term at both call sites below.
    expected_salary = round(CAP_SETTINGS.max_pct_0_6 / 100.0 * CAP_SETTINGS.salary_cap)
    assert expected_salary == 38_661_750

    pending_offers = run_resign_window(snapshot, ctx, expiring_by_team={team.id: (incumbent.id,)})
    assert len(pending_offers) == 1
    # `run_resign_window`'s own offer, priced against `room=capacity` --
    # kills reverting that call site back to `room=room` (critic's first
    # mutation), which would collapse this to the plain MLE ladder
    # (`mle_amount = 14,104,000`) instead.
    assert pending_offers[0].annual_salary == expected_salary

    result = run_free_agency(snapshot, ctx, pool=(incumbent,), pending_offers=pending_offers)

    signing = next(s for s in result.signings if s.player_id == incumbent.id)
    assert signing.signing_method == gm_service.SigningMethod.BIRD, (
        "an over-cap incumbent re-sign must execute via the Bird bucket end-to-end, through "
        "BOTH run_resign_window's pending offer AND run_free_agency's round-0 recompute"
    )
    # Round 0's recomputed offer, priced against `_effective_capacity(...,
    # incumbent_prior_salary=..., incumbent_years_pro=...)` -- kills
    # dropping those incumbent kwargs from round 0 (critic's second
    # mutation), which would likewise collapse this to `mle_amount`.
    # `record.threshold` is `construct_offer`'s own `float(capacity)`, the
    # capacity exposed directly rather than re-derived.
    assert signing.annual_salary == expected_salary
    assert signing.record.threshold == float(expected_salary)


def test_effective_capacity_precondition_still_holds_for_finalize_signing():
    """Round 11 §5.5 item 8, rewritten per architect round 14 §5.5. Only
    becomes meaningful under this fix -- pre-fix, an incumbent never
    reached the `MIN_EXCEPTION` assert at all, since the old Bird branch
    fired unconditionally (round 14 §3 H1). Randomized sweep of (room,
    apron_room, incumbent_prior_salary/years_pro) at the interesting
    boundary `L in [-GAP, +mle_amount]` (not a uniform `[-40M, 100M]`
    sweep, which rarely lands near it) -- trials whose capacity falls
    below `min_salary` are skipped (the caller's own precondition, `_can_
    sign_at_all`, now excludes them; feeding a value `construct_offer`
    would never legally receive into the room branch is garbage-in, not a
    real trial). Confirms: (a) `_finalize_signing` never raises; (b) the
    tag matches an INDEPENDENTLY derived expectation -- not the old
    decorative `method in SigningMethod`, which every enum member
    trivially satisfies; (c) the apron ledger never falls below `-GAP`
    (round 14 §5.2's invariant, spot-checked at the unit level too).
    **Kills:** a capacity/finalize divergence introduced by the new
    conjunct -- the same bug class three critic rounds already closed at
    step 10 T3 for the non-incumbent ladder."""
    gap = CAP_SETTINGS.apron_2 - CAP_SETTINGS.apron_1
    rng = random.Random(20260902)
    trials = 0
    while trials < 200:
        room = rng.randint(-80_000_000, 80_000_000)
        is_incumbent = rng.random() < 0.5
        incumbent_prior_salary = rng.randint(1_000_000, 60_000_000) if is_incumbent else None
        incumbent_years_pro = rng.randint(0, 15) if is_incumbent else None
        if not is_incumbent and rng.random() < 0.3:
            apron_room: int | None = None  # non-incumbent trials sometimes carry no ledger
        elif rng.random() < 0.6:
            apron_room = rng.randint(-gap, CAP_SETTINGS.mle_amount)  # the interesting boundary
        else:
            apron_room = rng.randint(-gap, gap * 4)

        team_id = 1
        room_remaining = {team_id: room}
        exceptions_used: dict[int, set[str]] = {team_id: set()}
        apron_room_remaining = {team_id: apron_room} if apron_room is not None else None

        capacity = gm_service._effective_capacity(
            team_id, room_remaining, exceptions_used, CAP_SETTINGS,
            incumbent_prior_salary=incumbent_prior_salary,
            incumbent_years_pro=incumbent_years_pro,
            apron_room_remaining=apron_room_remaining,
        )
        if capacity < CAP_SETTINGS.min_salary:
            continue
        trials += 1

        # Independently derived expectation, in the SAME priority order
        # `_finalize_signing` uses, computed from this trial's own raw
        # inputs rather than by calling `_finalize_signing` a second time.
        bird_ceiling = gm_service.signing_decisions.bird_ceiling
        if capacity <= room:
            expected_method = gm_service.SigningMethod.CAP_SPACE
        elif incumbent_prior_salary is not None and capacity <= bird_ceiling(
            CAP_SETTINGS, incumbent_years_pro=incumbent_years_pro, apron_room=apron_room
        ):
            expected_method = gm_service.SigningMethod.BIRD
        elif capacity <= CAP_SETTINGS.mle_amount:
            expected_method = gm_service.SigningMethod.MLE
        elif capacity <= CAP_SETTINGS.bi_annual_amount:
            expected_method = gm_service.SigningMethod.BI_ANNUAL
        else:
            expected_method = gm_service.SigningMethod.MIN_EXCEPTION

        method = gm_service._finalize_signing(
            team_id, capacity, room_remaining, exceptions_used, CAP_SETTINGS,
            incumbent_prior_salary=incumbent_prior_salary,
            incumbent_years_pro=incumbent_years_pro,
            apron_room_remaining=apron_room_remaining,
        )
        assert method == expected_method
        if apron_room_remaining is not None:
            assert apron_room_remaining[team_id] >= -gap


def test_can_sign_at_all_excludes_a_team_strictly_between_zero_and_min_salary_of_headroom():
    """Architect round 14 §8 R2's explicitly-requested mutation test,
    confirmed missing by critic round 48 B3 (`mutating _can_sign_at_all's
    >= cap_settings.min_salary to > 0 SURVIVES the whole unit suite`).

    **H5's exact gap.** `construct_offer` re-applies `signing.legal_
    capacity` to whatever `room` it is handed, and that function's own
    `room > 0` branch floors ANY positive room up to `min_salary`
    (`legal_capacity`'s own docstring: "Floored at min_salary even when
    room > 0 ... a tiny positive room ... may not undercut it") -- so a
    capacity that is positive but below `min_salary` does not pass through
    `construct_offer` unchanged; it is silently re-inflated. `_can_sign_at_
    all`'s whole job is to keep `legal_capacity(capacity) == capacity` an
    identity for every value it ever hands to `construct_offer` -- which
    requires excluding this gap too, not just non-positive headroom. A
    `> 0` threshold would let a team with, say, `apron2_headroom =
    379,000` (comfortably positive, but $893,870 short of `min_salary`)
    through the gate.

    **Kills:** weakening `>= cap_settings.min_salary` to `> 0` (this
    fixture's own `apron_room` sits deliberately in the gap: `apron2_
    headroom = -11,500,000 + 11,879,000 = 379,000`, `0 < 379,000 <
    min_salary (1,272,870)` -- the mutated condition evaluates `True`
    here, the correct one `False`)."""
    team_id = 1
    apron_room_remaining = {team_id: -11_500_000}
    headroom = gm_service.signing_decisions.apron2_headroom(
        CAP_SETTINGS, apron_room_remaining[team_id]
    )
    assert 0 < headroom < CAP_SETTINGS.min_salary, (
        "fixture must land the headroom strictly inside the (0, min_salary) gap, "
        "or this test cannot discriminate >= min_salary from > 0"
    )
    assert not gm_service._can_sign_at_all(team_id, apron_room_remaining, CAP_SETTINGS)


def test_the_bird_ledger_cannot_be_walked_past_the_apron_2_headroom():
    """Round 11 §5.5 item 10a (architect round 14 §5.4/§5.5, replacing the
    old item 10 -- see the module-level note above item 11). Critic B3's
    own reproduction fixture (`room_remaining = -60,000,000`, `apron_room_
    remaining = 20,000,000`, both incumbents `years_pro = 12`, salaries
    drawn at capacity), continued past the second incumbent to a third,
    executed against the real `_effective_capacity`/`_finalize_signing`
    pair. **Kills:** dropping the `annual_salary <= bird_ceiling` conjunct
    (the third signing would still tag BIRD and drive the ledger to
    -$23.8M instead of refusing); dropping the hard `apron_2` clamp (the
    second signing would draw the full `mle_amount` unclamped and land the
    ledger at -$14.1M instead of exactly `-GAP`)."""
    gap = CAP_SETTINGS.apron_2 - CAP_SETTINGS.apron_1
    assert gap == 11_879_000
    team_id = 1
    room_remaining = {team_id: -60_000_000}
    exceptions_used: dict[int, set[str]] = {team_id: set()}
    apron_room_remaining = {team_id: 20_000_000}
    tier_35pct = round(CAP_SETTINGS.max_pct_10_plus / 100.0 * CAP_SETTINGS.salary_cap)
    assert tier_35pct == 54_126_450

    # Incumbent 1: Bird ceiling (min(tier, apron_room)) is the binding
    # term -- draws the full $20,000,000 apron headroom.
    capacity_1 = gm_service._effective_capacity(
        team_id, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=1, incumbent_years_pro=12,
        apron_room_remaining=apron_room_remaining,
    )
    assert capacity_1 == 20_000_000
    method_1 = gm_service._finalize_signing(
        team_id, capacity_1, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=1, incumbent_years_pro=12,
        apron_room_remaining=apron_room_remaining,
    )
    assert method_1 == gm_service.SigningMethod.BIRD
    assert apron_room_remaining[team_id] == 0

    # Incumbent 2: apron ledger is now 0, so bird_ceiling = min(tier, 0)
    # <= 0 -- the incumbent falls through toward the ordinary once-per-pass
    # MLE ladder (round 14 B3's whole point), but the HARD apron_2 clamp
    # (round 14 §5.4's own worked table, row 2) now also binds: apron2_
    # headroom(apron_room=0) = 0 + GAP = 11,879,000, strictly below `mle_
    # amount` (14,104,000) -- so the ladder itself is clamped down to
    # 11,879,000 before the MLE tag is even reached.
    capacity_2 = gm_service._effective_capacity(
        team_id, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=1, incumbent_years_pro=12,
        apron_room_remaining=apron_room_remaining,
    )
    assert capacity_2 == gap == 11_879_000
    assert capacity_2 < CAP_SETTINGS.mle_amount
    method_2 = gm_service._finalize_signing(
        team_id, capacity_2, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=1, incumbent_years_pro=12,
        apron_room_remaining=apron_room_remaining,
    )
    assert method_2 == gm_service.SigningMethod.MLE
    assert exceptions_used[team_id] == {"mle"}
    assert apron_room_remaining[team_id] == -gap  # ledger floor, exactly -GAP

    # Incumbent 3: capacity is now below min_salary -- the caller's own
    # precondition (`_can_sign_at_all`) would skip this team entirely
    # rather than reach `_finalize_signing` at all.
    capacity_3 = gm_service._effective_capacity(
        team_id, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=1, incumbent_years_pro=12,
        apron_room_remaining=apron_room_remaining,
    )
    assert capacity_3 < CAP_SETTINGS.min_salary
    assert not gm_service._can_sign_at_all(team_id, apron_room_remaining, CAP_SETTINGS)


def test_run_free_agency_never_leaves_a_team_over_apron_2():
    """Round 11 §5.5 item 10b (architect round 14 §5.5, rebuilt fix round --
    critic round 48 B2). A pass-level property test on the real `run_free_
    agency`: CAP_SPACE, BIRD, MLE, BI_ANNUAL, and MIN_EXCEPTION all fire in
    one pass (asserted explicitly -- otherwise the test would be vacuous,
    exactly how the old item 10 went wrong per critic B4, and exactly how
    this test's own prior version went wrong per critic round 48 B2: its
    `apron1_room` and its recomputed post-pass `used` had no relation to
    each other -- 79.7% slack, CAP_SPACE structurally unreachable because
    `room` started already negative, and only 2 of 5 `charge_apron()`
    branches were actually kill-verified).

    **The fixture, rebased so the ledger's starting point and the
    recomputed `used` actually relate.** A single `locked_player` -- a
    2-year contract already covering `ctx.next_season`, never named in
    `expiring_by_team` or `pool`, so `run_resign_window`/`run_free_agency`
    never touch it -- carries `CAP_SETTINGS.apron_1 - apron1_room` in
    committed salary, i.e. exactly the pre-existing `used` this fixture's
    synthetic `ctx.apron1_room_by_team` figure is supposed to represent.
    `room` starts small and POSITIVE (`2,000,000`, unlike the prior
    version's already-negative `-40,000,000`, which made `annual_salary <=
    room` structurally unreachable and starved CAP_SPACE entirely) so a
    cheap pool player can actually price under it. One incumbent (`bird_
    incumbent`, salary-irrelevant -- Bird-eligible via `_incumbent_prior_
    salary`) plus a small, deliberately price-ordered pool (`ovr=68` prices
    above `mle_amount`'s own ceiling and gets capped exactly to it (MLE);
    `ovr=40` prices under `room` (CAP_SPACE); `ovr=60`, priced lower still
    once `mle`/`room` are otherwise spoken for, lands on MIN_EXCEPTION, not
    BI_ANNUAL -- BI_ANNUAL instead goes to one of the ten `ovr=45` filler
    players (critic round 49 nit 1: the fillers are not uniformly MIN_
    EXCEPTION volume, one of them takes the BI_ANNUAL rung instead), with
    the remaining fillers supplying MIN_EXCEPTION volume to reach `roster_
    min` without needing `_enforce_roster_floor` at all, which would
    reintroduce an untested `minimum_filler` credit into the recompute
    below) drives every rung of the ladder in one real pass. **Measured,
    deterministic (no rng in this call chain), and re-verified after this
    fix (critic round 49 B2-a: re-confirmed via a temporary debug print on
    the real `apron_room_remaining` local, removed after use)**: `apron1_
    room=20,000,000` yields `apron_room_remaining` finishing at
    `-11,455,830` -- $423,170 short of the hard `-GAP` floor
    (`-11,879,000`) -- and the REAL `compute_team_season_cap_sheet`
    recompute below lands at `apron2_room=423,170`, i.e. the pass genuinely
    tightens to within ~0.2% of `apron_2` (`used=207,400,830` vs
    `apron_2=207,824,000`), not 79.7% slack. (Self-check: `apron2_room ≡
    ledger + GAP`, architect round 14 §3 H4 -- `-11,455,830 + 11,879,000 =
    423,170`, matching the recompute exactly.)

    **Kills, mutation-verified one at a time (a disposable `DBA2_DEBUG_
    SKIP_CHARGE`-gated build of `_finalize_signing`, reverted, never
    committed) by dropping each of the 5 `charge_apron()` calls in turn**:
    CAP_SPACE -> `apron2_room=-1,383,163`; BIRD -> `-849,700`; BI_ANNUAL ->
    `-1,383,163`; MIN_EXCEPTION -> `-849,700` -- all four flip the
    `apron2_room >= 0` assertion below. MLE also dies, but one assertion
    earlier (critic round 49 nit 2): dropping its charge means MLE never
    gets used at all (the ladder never reaches it once uncapped), so it
    fails the non-vacuousness `methods ==` assertion above, not the
    `apron2_room >= 0` one -- still a genuine kill of the same finding,
    just at a different assertion than the other four. Unlike the prior
    version's 3 silent survivors, all 5 are now load-bearing."""
    team = make_team(1)
    bird_incumbent = make_roster_player(
        2001, age=29, season=_SEASON, overall_cached=65,
        primary_position=Position.SG, team_id=team.id,
        peak_age_start=26, peak_age_end=30,
        contract=make_contract(
            2001, team_id=team.id, start_season=_SEASON, salaries=(9_000_000,)
        ),
    )
    incumbents = (bird_incumbent,)

    apron1_room = 20_000_000
    # A "locked" player -- a 2-year contract already covering `ctx.next_
    # season`, NOT part of `expiring_by_team`/`pool`, so `run_resign_
    # window`/`run_free_agency` never touch it. Its salary is exactly the
    # pre-existing `used` this fixture's `apron1_room` implies
    # (`apron_1 - apron1_room`), so the post-pass recomputed `used` below
    # and the ledger's own starting point actually relate (critic round 48
    # B2's core complaint about the prior version)."""
    locked_salary = CAP_SETTINGS.apron_1 - apron1_room
    locked_player = make_roster_player(
        2500, age=30, season=_SEASON, overall_cached=82,
        primary_position=Position.PF, team_id=team.id,
        peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            2500, team_id=team.id, start_season=_SEASON, salaries=(locked_salary, locked_salary)
        ),
    )

    team_snapshot = build_team_snapshot(team, incumbents + (locked_player,), (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_snapshot,))
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=2_000_000, apron1_room=apron1_room)

    pending_offers = run_resign_window(
        snapshot, ctx, expiring_by_team={team.id: tuple(rp.id for rp in incumbents)}
    )
    # `pool` must include the incumbents themselves -- `run_free_agency`
    # only ever bids on players present in its own `remaining_pool` (built
    # from this argument), and `pending_offers` alone does not add anyone
    # to it (`_load_free_agency_pool`'s real production contract: "every
    # rostered player `_expiring_by_team` names", merged with fresh FAs,
    # not a disjoint set). Omitting them here would make the Bird branch
    # structurally unreachable regardless of what the ledger allows.
    pool_players = tuple(
        _make_prospect(pid, age=24, overall=ovr)
        for pid, ovr in (
            (3001, 68),  # prices above mle_amount -- caps exactly to it
            (3002, 60),  # BI_ANNUAL-range once mle is spoken for
            (3003, 40),  # cheap -- prices under `room`
        ) + tuple((3100 + i, 45) for i in range(10))  # MIN_EXCEPTION volume
    )
    pool = incumbents + pool_players
    result = run_free_agency(snapshot, ctx, pool=pool, pending_offers=pending_offers)

    methods = {s.signing_method for s in result.signings}
    assert methods == {
        gm_service.SigningMethod.CAP_SPACE,
        gm_service.SigningMethod.BIRD,
        gm_service.SigningMethod.MLE,
        gm_service.SigningMethod.BI_ANNUAL,
        gm_service.SigningMethod.MIN_EXCEPTION,
    }, (
        "the fixture must reach every ladder rung, or this test is vacuous "
        "(critic B4, and critic round 48 B2's own finding that CAP_SPACE never fired)"
    )

    # Recompute `used`/`apron2_room` through the REAL `compute_team_season_
    # cap_sheet` call from the written signings -- never a reimplementation
    # of its `used = committed_salary + dead_cap + minimum_filler + holds`
    # formula. `compute_team_season_cap_sheet` itself takes a flat `CapSheet
    # TeamSeasonInputs` (the bulk-loaded/aggregated shape both the real DB
    # path and the harness feed it), not a `TeamSnapshot` -- aggregated
    # here directly from the post-pass contracts (`locked_player` and every
    # incumbent keep their original contract unless `result.signings`
    # re-signed them; every signed pool player gets the contract `run_free_
    # agency` actually wrote), the same basis `tools/gm_harness/state.py::
    # _aggregate_capsheet_inputs` uses (committed salary summed per
    # contract-year covering `next_season`, one contract-covered player per
    # `roster_count`).
    contract_by_player: dict[int, object] = {
        rp.id: rp.contract for rp in incumbents + (locked_player,)
    }
    for s in result.signings:
        contract_by_player[s.player_id] = make_contract(
            s.player_id, team_id=team.id, start_season=ctx.next_season,
            salaries=(s.annual_salary,) * s.years,
        )
    committed_salary = 0
    roster_count = 0
    for contract in contract_by_player.values():
        if contract is None:
            continue
        for year in contract.years:
            if year.season == ctx.next_season:
                committed_salary += year.salary
                roster_count += 1

    inputs = CapSheetTeamSeasonInputs(
        team_id=team.id, season=ctx.next_season, committed_salary=committed_salary,
        dead_cap=0, cap_holds=0, team_option_salary=0, player_option_salary=0,
        roster_count=roster_count,
    )
    sheet = compute_team_season_cap_sheet(
        inputs, ctx.next_season_cap_settings, projected=False, include_holds=False
    )
    assert sheet.apron2_room >= 0, (
        f"apron_2 invariant violated: apron2_room={sheet.apron2_room} after a real "
        f"run_free_agency pass"
    )
    # Non-vacuousness, made structurally checkable rather than trusted to a
    # docstring (critic round 48 B2): the pass must land CLOSE to the
    # apron_2 boundary, not deep in slack -- this is what makes every
    # `charge_apron()` branch above actually load-bearing to the assertion.
    assert sheet.apron2_room < 5_000_000, (
        f"fixture is too slack to be a meaningful apron_2 gate test: "
        f"apron2_room={sheet.apron2_room} is not within a few million of the boundary"
    )


def test_apron_room_remaining_falls_for_every_signing_method_not_just_bird():
    """Item 11 (architect round 14 §5.5). One team, four signings through
    the real `_finalize_signing`, one per non-Bird ladder rung -- CAP_
    SPACE, MLE, BI_ANNUAL, MIN_EXCEPTION (BIRD itself is already covered
    at length by items 4/5/10a/10b above; this test's whole point is the
    OTHER four methods, which the pre-fix tree never charged at all --
    critic round 47 B2: 40% of dollars written were invisible to the
    ledger). Ample `apron_room_remaining` throughout (`200,000,000`, far
    above the sum of all four salaries) so the hard `apron_2` clamp is
    never itself the thing under test here -- that is items 10a/10b's job.
    **Kills:** B2's exact shape (charging only the BIRD branch, or any
    proper subset of the four); charging `room_remaining` instead of the
    apron ledger (this test asserts the LEDGER's decrement, never `room_
    remaining`, which is a different, pre-existing figure by design --
    see `_finalize_signing`'s own docstring note 2)."""
    team_id = 1
    room_remaining = {team_id: 5_000_000}
    exceptions_used: dict[int, set[str]] = {team_id: set()}
    apron_room_remaining = {team_id: 200_000_000}
    salaries = [
        5_000_000,  # <= room -- CAP_SPACE
        CAP_SETTINGS.mle_amount,  # MLE
        CAP_SETTINGS.bi_annual_amount,  # BI_ANNUAL
        CAP_SETTINGS.min_salary,  # MIN_EXCEPTION (both other exceptions now spent)
    ]

    methods = [
        gm_service._finalize_signing(
            team_id, salary, room_remaining, exceptions_used, CAP_SETTINGS,
            apron_room_remaining=apron_room_remaining,
        )
        for salary in salaries
    ]

    assert methods == [
        gm_service.SigningMethod.CAP_SPACE,
        gm_service.SigningMethod.MLE,
        gm_service.SigningMethod.BI_ANNUAL,
        gm_service.SigningMethod.MIN_EXCEPTION,
    ], "fixture must reach every non-Bird rung, or this test cannot discriminate B2's scope"
    assert apron_room_remaining[team_id] == 200_000_000 - sum(salaries), (
        "the ledger must fall by the SUM of every signing's salary, regardless of method -- "
        "a mutation that skips charge_apron() on any one branch would leave this short"
    )


def test_roster_floor_signings_do_not_charge_the_apron_ledger_and_do_not_move_used():
    """Item 12 (architect round 14 §5.5, §3 H2's `Δused = 0` proof for a
    floor signing, made concrete). Asserts BOTH halves the memo names:
    (a) an `apron_room_remaining` ledger kept entirely outside `_enforce_
    roster_floor`'s own call signature is untouched by construction (the
    function cannot mutate what it is never handed -- `run_free_agency`'s
    own call site passes no such argument at all, §5.3's "no apron_room_
    remaining argument -- deliberate" comment); (b) the recomputed `used`,
    through the REAL `compute_team_season_cap_sheet`, is unchanged
    end-to-end -- never a reimplementation of `used`'s own formula.

    **The `Δused = 0` arithmetic, concrete**: before, `roster_count` is
    below `roster_min` by `needed`, so `minimum_filler = needed *
    min_salary` and `committed_salary = 0` (no contract covers `next_
    season` yet) -- `used_before = needed * min_salary`. After, `_enforce_
    roster_floor` signs exactly `needed` players at `min_salary` each,
    `roster_count` reaches `roster_min` (`minimum_filler = 0`), and
    `committed_salary = needed * min_salary` -- `used_after = needed *
    min_salary`, identical to `used_before`. **Kills:** "fixing" the
    omission by charging the floor (which would silently under-fund the
    next pass, the wrong direction per §3 H2's own proof), and any future
    change to the floor's salary/count bookkeeping that breaks `Δused =
    0`."""
    snapshot = _make_small_league(n_teams=1)
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY)
    team = snapshot.teams[0]
    roster_min = ctx.next_season_cap_settings.roster_min
    min_salary = ctx.next_season_cap_settings.min_salary
    starting_count = 10
    needed = roster_min - starting_count
    assert needed > 0, "fixture must start below roster_min for this test to mean anything"

    remaining_pool = {
        rp.id: rp
        for rp in (
            make_roster_player(
                8100 + i, age=25, season=_SEASON, overall_cached=60 + i,
                primary_position=Position.PG, team_id=None,
                peak_age_start=27, peak_age_end=31,
            )
            for i in range(needed + 3)
        )
    }
    roster_count = {team.id: starting_count}
    room_remaining = {team.id: ctx.room_by_team[team.id]}
    matrix_by_team = {team.id: build_need_matrix(team, tuning=ctx.tuning)}
    # Kept entirely outside `_enforce_roster_floor`'s own call below --
    # never passed in, so any post-call divergence from this exact dict
    # would itself prove the function reached into state it has no
    # reference to, which is impossible by construction, not merely
    # untested.
    apron_room_remaining_untouched = {team.id: 50_000_000}
    apron_room_remaining_snapshot_before = dict(apron_room_remaining_untouched)

    inputs_before = CapSheetTeamSeasonInputs(
        team_id=team.id, season=ctx.next_season, committed_salary=0,
        dead_cap=0, cap_holds=0, team_option_salary=0, player_option_salary=0,
        roster_count=starting_count,
    )
    sheet_before = compute_team_season_cap_sheet(
        inputs_before, ctx.next_season_cap_settings, projected=False, include_holds=False
    )

    signings, _records = gm_service._enforce_roster_floor(
        snapshot, ctx, remaining_pool, roster_count, room_remaining, matrix_by_team
    )

    assert apron_room_remaining_untouched == apron_room_remaining_snapshot_before, (
        "an apron ledger never passed to _enforce_roster_floor must be bit-for-bit unchanged"
    )
    assert len(signings) == needed
    assert roster_count[team.id] == roster_min

    committed_salary_after = sum(s.annual_salary for s in signings)  # each is min_salary, 1 year
    inputs_after = CapSheetTeamSeasonInputs(
        team_id=team.id, season=ctx.next_season, committed_salary=committed_salary_after,
        dead_cap=0, cap_holds=0, team_option_salary=0, player_option_salary=0,
        roster_count=roster_count[team.id],
    )
    sheet_after = compute_team_season_cap_sheet(
        inputs_after, ctx.next_season_cap_settings, projected=False, include_holds=False
    )

    assert committed_salary_after == needed * min_salary
    assert sheet_after.used == sheet_before.used, (
        f"floor signings must be exactly apron-neutral (Delta used = 0) -- "
        f"used went {sheet_before.used} -> {sheet_after.used}"
    )


def test_an_incumbent_whose_salary_exceeds_the_bird_ceiling_consumes_the_ordinary_exception():
    """Item 13 (architect round 14 §5.5) -- the smallest reproduction of
    B3 itself: an incumbent whose Bird ceiling does not cover `mle_
    amount`, priced at exactly `mle_amount`. **Kills:** dropping the
    `annual_salary <= bird_ceiling` conjunct -- without it this incumbent
    tags BIRD unconditionally regardless of whether the ceiling covers the
    salary, and `exceptions_used` never registers the MLE spend, exactly
    critic round 47's B3 reproduction one signing deep.

    **Deliberate departure from the memo's own literal "ledger seeded at
    exactly 0" phrasing, disclosed rather than silently reinterpreted**:
    at `apron_room_remaining = 0`, `apron2_headroom = GAP = 11,879,000`,
    strictly BELOW `mle_amount` (14,104,000) -- `_finalize_signing`'s own
    guard-first assert (round 14 §5.3) would reject `mle_amount` outright
    at that ledger value, which is a different bug class (H5) than the
    one this test targets. `apron_room_remaining = 2,225,000` is used
    instead -- `mle_amount - GAP` exactly, the tightest ledger value at
    which `mle_amount` is still legal under the hard clamp (`headroom ==
    mle_amount` exactly) while still being far below the tier ceiling, so
    the SOFT Bird ceiling (not the hard clamp) is what makes this
    incumbent fall through. `2,225,000` is not an arbitrary number -- it
    is architect round 14 §3 H3's own named overshoot figure ("breaches
    apron_2 by up to $2,225,000"), the exact gap between `mle_amount` and
    `GAP` this whole memo exists to close."""
    gap = CAP_SETTINGS.apron_2 - CAP_SETTINGS.apron_1
    boundary_apron_room = CAP_SETTINGS.mle_amount - gap
    assert boundary_apron_room == 2_225_000
    team_id = 1
    room_remaining = {team_id: -50_000_000}
    exceptions_used: dict[int, set[str]] = {team_id: set()}
    apron_room_remaining = {team_id: boundary_apron_room}

    method = gm_service._finalize_signing(
        team_id, CAP_SETTINGS.mle_amount, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=1, incumbent_years_pro=12,
        apron_room_remaining=apron_room_remaining,
    )

    assert method == gm_service.SigningMethod.MLE
    assert exceptions_used[team_id] == {"mle"}


def test_run_free_agency_exceptions_used_bound_reverts_to_the_original_overcommit(monkeypatch):
    """Mutation test for the fix above: reverting `_effective_capacity` to
    the pre-round-18 stateless `signing.legal_capacity` call (no per-pass
    `exceptions_used` memory) must break the state-tracking guarantee --
    proving `test_run_free_agency_bounds_exceptions_to_once_per_pass_
    across_many_rounds` above actually depends on the fix, not on some
    other, coincidental property of the scenario.

    **Surfaces as a loud `AssertionError`, not a silent dollar overcommit**
    (critic round 18's own nit 2 fix, applied after this test was
    originally written): `_finalize_signing`'s minimum-exception fallthrough
    now asserts `annual_salary <= min_salary` rather than returning a wrong
    tag quietly. Under this mutation, `_effective_capacity` keeps reporting
    the MLE-sized ceiling as available forever, so `construct_offer` keeps
    building MLE-sized offers past the real per-pass MLE/bi-annual limit;
    `_finalize_signing` itself is NOT mutated, so it correctly recognizes
    those exceptions are already used and falls through toward minimum --
    where the new assert fires on the still-MLE-sized salary before a wrong
    total could ever be committed. That crash IS the proof the fix matters:
    with the real `_effective_capacity`, this precondition is structurally
    guaranteed and never fires (see the boundary-value test below)."""

    def _stateless_capacity(team_id, room_remaining, exceptions_used, cap_settings, **_kwargs):
        # `**_kwargs` swallows slice 12-A's `incumbent_prior_salary`/
        # `incumbent_years_pro`/`apron_room_remaining` -- every real call
        # site now passes them, and this replacement's whole point is to
        # drop the STATEFUL exceptions-used memory, not the incumbent
        # branch, so it must keep accepting (and ignoring) them rather
        # than crashing with a spurious `TypeError` that would mask the
        # `AssertionError` this test actually asserts on.
        return gm_service.signing_decisions.legal_capacity(
            room_remaining.get(team_id, 0), cap_settings
        )

    monkeypatch.setattr(gm_service, "_effective_capacity", _stateless_capacity)

    snapshot = _make_small_league(n_teams=1, roster_size=3)
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=30_000_000)
    pool = tuple(
        make_roster_player(
            9200 + i, age=27, season=_SEASON, overall_cached=92,
            primary_position=list(Position)[i % len(list(Position))],
            team_id=None, peak_age_start=27, peak_age_end=31,
        )
        for i in range(10)
    )

    with pytest.raises(AssertionError, match="_finalize_signing's precondition"):
        run_free_agency(snapshot, ctx, pool=pool, pending_offers=())


# ---------------------------------------------------------------------------
# run_free_agency / _enforce_roster_floor -- T6a regression
#
# `roster_count`'s seed basis is `gm_service._next_season_roster_count`, not
# today's raw `len(t.roster)` body count and not "does a Contract row cover
# `ctx.next_season`" (architect design-round diagnosis, then corrected by
# critic round 19's B1 fix -- see that function's own docstring). Every
# signing `run_free_agency` writes starts at `ctx.next_season`, and both
# roster-max gates it reads compare against `next_season_cap_settings` -- so
# a team with several rostered players whose contracts expire at
# `ctx.season` (not covering `ctx.next_season`) has real open next-season
# slots that `len(t.roster)` alone hides, while a team with contract-less
# `ACTIVE` rostered players (no `Contract` row at all) still has those
# players occupying real slots that a naive "contract covers next_season"
# count would wrongly treat as open. `_build_mixed_roster` below builds the
# expiring-vs-covering shape: some players whose contract's last year is
# `ctx.season` ("expiring," mirrors `_expiring_by_team`'s own definition)
# and some whose contract's last year is `ctx.next_season` ("covering").
# Its `n_contractless` parameter (B1 regression, added this round)
# adds the third shape: rostered players with `contract is None`.
# ---------------------------------------------------------------------------


def _build_mixed_roster(
    team_id: int,
    *,
    n_covering: int,
    n_expiring: int,
    avoid_position: Position,
    base_id: int,
    n_contractless: int = 0,
    n_lapsed: int = 0,
):
    """`n_covering` players whose contract's last year is `ctx.next_season`
    (genuinely occupy a next-season slot) plus `n_expiring` players whose
    contract's last year is `ctx.season` (expiring -- `len(t.roster)`
    counts them as rostered today, but they have no legal claim on a
    next-season slot; this is the exact gap the T6a fix closes) plus
    `n_contractless` players with `contract=None` (B1 regression, critic
    round 19) -- rostered, `ACTIVE`, with no `Contract` row at all, which
    `_next_season_roster_count` must still count as occupying a slot since
    nothing removes them, plus `n_lapsed` players whose contract's last
    year is already BEFORE `ctx.season` (round-2 nit -- a stale contract
    nobody rolled over; `_expiring_by_team`'s own `== ctx.season` test
    would never route them to free agency either, so they must count the
    same as a contract-less player, not be dropped like a genuine
    expiry)."""
    positions = [p for p in Position if p is not avoid_position]
    roster = []
    pid = base_id
    for i in range(n_lapsed):
        roster.append(
            make_roster_player(
                pid, age=30 + (i % 4), season=_SEASON, overall_cached=64 + (i % 8),
                team_id=team_id, primary_position=positions[i % len(positions)],
                peak_age_start=27, peak_age_end=31,
                contract=make_contract(
                    pid, team_id=team_id, start_season=_SEASON - 3,
                    salaries=(3_000_000, 3_000_000),  # last year is _SEASON - 2 -- lapsed
                ),
            )
        )
        pid += 1
    for i in range(n_covering):
        roster.append(
            make_roster_player(
                pid, age=26 + (i % 4), season=_SEASON, overall_cached=64 + (i % 8),
                team_id=team_id, primary_position=positions[i % len(positions)],
                peak_age_start=27, peak_age_end=31,
                contract=make_contract(
                    pid, team_id=team_id, start_season=_SEASON,
                    salaries=(3_000_000, 3_000_000),  # covers _SEASON and _SEASON + 1
                ),
            )
        )
        pid += 1
    for i in range(n_expiring):
        roster.append(
            make_roster_player(
                pid, age=26 + (i % 4), season=_SEASON, overall_cached=64 + (i % 8),
                team_id=team_id, primary_position=positions[(n_covering + i) % len(positions)],
                peak_age_start=27, peak_age_end=31,
                contract=make_contract(
                    pid, team_id=team_id, start_season=_SEASON,
                    salaries=(3_000_000,),  # last year is _SEASON only -- expiring
                ),
            )
        )
        pid += 1
    for i in range(n_contractless):
        roster.append(
            make_roster_player(
                pid, age=26 + (i % 4), season=_SEASON, overall_cached=64 + (i % 8),
                team_id=team_id,
                primary_position=positions[(n_covering + n_expiring + i) % len(positions)],
                peak_age_start=27, peak_age_end=31,
                contract=None,  # no Contract row at all -- still occupies a slot
            )
        )
        pid += 1
    return tuple(roster)


def test_run_free_agency_signs_a_team_with_open_next_season_slots_masked_by_expiring_contracts():
    """The exact T6a scenario. 15 rostered players (== `roster_max`), but
    only 10 have a contract covering `ctx.next_season` -- 5 are expiring at
    `ctx.season`. Under the pre-fix `len(t.roster)` seed this team reads as
    `15 >= roster_max=15` -- already full -- and `run_free_agency` signs it
    NOBODY, no matter how many free agents are available (this is not
    hypothetical: re-run this exact scenario with `roster_count` seeded
    from `len(t.roster)` instead and every assertion below fails, `result.
    signings` is empty). The corrected, next-season-coverage basis reads
    this team as having exactly 5 genuinely open slots, and real free-agent
    signings actually fill them."""
    team = make_team(1)
    roster = _build_mixed_roster(
        1, n_covering=10, n_expiring=5, avoid_position=Position.C, base_id=1000
    )
    snapshot = build_league_snapshot(
        _LEAGUE_ID, _SEASON, (build_team_snapshot(team, roster, (), CAP_SETTINGS),)
    )
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=30_000_000)
    assert len(roster) == CAP_SETTINGS.roster_max, (
        "fixture must sit exactly at roster_max by raw body count"
    )

    # 5 real free agents, same "92-overall, market-pinned" shape as the
    # B1/B1b exceptions-bounding test above, so the exact CAP_SPACE / MLE /
    # BI_ANNUAL / MIN_EXCEPTION composition is deterministic.
    pool = tuple(
        make_roster_player(
            9500 + i, age=27, season=_SEASON, overall_cached=92,
            primary_position=list(Position)[i % len(list(Position))],
            team_id=None, peak_age_start=27, peak_age_end=31,
        )
        for i in range(5)
    )

    result = run_free_agency(snapshot, ctx, pool=pool, pending_offers=())

    assert len(result.signings) == 5, "all 5 genuinely open next-season slots should fill"
    assert {s.player_id for s in result.signings} == {rp.id for rp in pool}

    from collections import Counter

    method_counts = Counter(s.signing_method for s in result.signings)
    assert method_counts[gm_service.SigningMethod.CAP_SPACE] == 1
    assert method_counts[gm_service.SigningMethod.MLE] == 1
    assert method_counts[gm_service.SigningMethod.BI_ANNUAL] == 1
    assert method_counts[gm_service.SigningMethod.MIN_EXCEPTION] == 2

    total_committed = sum(s.annual_salary for s in result.signings)
    assert total_committed == (
        30_000_000 + CAP_SETTINGS.mle_amount + CAP_SETTINGS.bi_annual_amount
        + 2 * CAP_SETTINGS.min_salary
    )


def test_run_free_agency_signs_nobody_for_a_genuinely_full_team():
    """The fix must not over-correct: a team where every rostered player's
    contract already covers `ctx.next_season` (nothing expiring) is
    genuinely full at `roster_max` and must still sign nobody, even with a
    strong, real free-agent pool on offer."""
    team = make_team(1)
    roster = _build_mixed_roster(
        1, n_covering=15, n_expiring=0, avoid_position=Position.C, base_id=2000
    )
    snapshot = build_league_snapshot(
        _LEAGUE_ID, _SEASON, (build_team_snapshot(team, roster, (), CAP_SETTINGS),)
    )
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=30_000_000)
    assert len(roster) == CAP_SETTINGS.roster_max

    pool = tuple(
        make_roster_player(
            9600 + i, age=27, season=_SEASON, overall_cached=92,
            primary_position=list(Position)[i % len(list(Position))],
            team_id=None, peak_age_start=27, peak_age_end=31,
        )
        for i in range(3)
    )

    result = run_free_agency(snapshot, ctx, pool=pool, pending_offers=())

    assert result.signings == (), "a genuinely full team (nothing expiring) must sign nobody"


def test_run_free_agency_does_not_over_sign_a_team_with_contract_less_rostered_bodies():
    """B1 fix (critic round 19). Mirrors the critic's own live-data repro
    shape: a team with several `ACTIVE` rostered players that carry no
    `Contract` row at all (`tools/seed_dev_league.py` never generates
    contracts, so this is the actual dev-league shape, not a contrived
    edge case), plus a mix of genuinely covering and genuinely expiring
    contracts. `_next_season_roster_count` = 3 covering + 5 contract-less =
    8 (the 2 expiring are excluded) -- `roster_max - 8 == 7` real open
    slots. Under the pre-fix, backwards `contract is not None` exclusion,
    the seed would have read only the 3 covering players as occupying
    slots (contract-less bodies invisible to it) and let this team sign up
    to `roster_max - 3 == 12` free agents on top of its 8 real physical
    bodies -- 20 total against a 15-player roster_max, the exact over-
    signing the critic reproduced live. This test proves the corrected
    basis stops at exactly 7, never more, regardless of how many free
    agents are on offer."""
    team = make_team(1)
    roster = _build_mixed_roster(
        1, n_covering=3, n_expiring=2, n_contractless=5,
        avoid_position=Position.C, base_id=5000,
    )
    snapshot = build_league_snapshot(
        _LEAGUE_ID, _SEASON, (build_team_snapshot(team, roster, (), CAP_SETTINGS),)
    )
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=0)
    assert gm_service._next_season_roster_count(snapshot.teams[0], ctx.next_season) == 8, (
        "fixture sanity: 3 covering + 5 contract-less count, the 2 expiring do not"
    )

    # 10 real free agents -- more than the 7 genuinely open slots, so a
    # regression back to the backwards exclusion (which would allow up to
    # 12 signings) has room to actually manifest as a wrong count here.
    pool = tuple(
        make_roster_player(
            9700 + i, age=27, season=_SEASON, overall_cached=92,
            primary_position=list(Position)[i % len(list(Position))],
            team_id=None, peak_age_start=27, peak_age_end=31,
        )
        for i in range(10)
    )

    result = run_free_agency(snapshot, ctx, pool=pool, pending_offers=())

    assert len(result.signings) == 7, (
        "exactly roster_max(15) - next_season_roster_count(8) = 7 signings, "
        "never more -- the contract-less bodies must not be signed over"
    )

    from collections import Counter

    assert Counter(s.signing_method for s in result.signings) == {
        gm_service.SigningMethod.MLE: 1,
        gm_service.SigningMethod.BI_ANNUAL: 1,
        gm_service.SigningMethod.MIN_EXCEPTION: 5,
    }
    assert all(r.reconstructs(tolerance=1e-6) for r in result.records)


def test_next_season_roster_count_counts_covering_expiring_and_contractless_correctly():
    """Pins `gm_service._next_season_roster_count` directly (critic round
    19's B2 fix -- the prior version of this test built its own copy of the
    production expression by hand instead of calling the real function, so
    it could not fail on a mutation of the actual seed logic). 2 covering
    (contract's last year is `ctx.next_season`), 3 expiring (contract's
    last year is `ctx.season` only), 4 contract-less (`contract=None`) --
    covering and contract-less both count toward next season, expiring does
    not: `2 + 4 == 6`, not `9` (raw body count) and not `2` (the pre-B1,
    backwards "contract is not None" basis, which would drop the 4
    contract-less bodies entirely)."""
    team = make_team(1)
    roster = _build_mixed_roster(
        1, n_covering=2, n_expiring=3, n_contractless=4,
        avoid_position=Position.PG, base_id=3000,
    )
    team_snapshot = build_team_snapshot(team, roster, (), CAP_SETTINGS)
    assert len(roster) == 9, "fixture sanity: 2 covering + 3 expiring + 4 contract-less"

    result = gm_service._next_season_roster_count(team_snapshot, _SEASON + 1)

    assert result == 6


def test_next_season_roster_count_counts_a_lapsed_contract_same_as_contractless():
    """Round-2 nit fix: a player whose contract's last year is already
    BEFORE `ctx.season` (a stale contract nobody rolled over -- unreachable
    today since nothing increments `leagues.current_season` yet, but the
    exact shape the not-yet-built multi-season harness will produce) must
    count toward next season's roster the same as a contract-less player,
    not be silently dropped like a genuine `== ctx.season` expiry. 2
    covering + 1 expiring + 3 lapsed: `2 + 3 == 5`, not `2` (the round-1
    fix's own `< next_season` basis, which wrongly excludes lapsed
    contracts the same way it once wrongly excluded contract-less ones)."""
    team = make_team(1)
    roster = _build_mixed_roster(
        1, n_covering=2, n_expiring=1, n_lapsed=3,
        avoid_position=Position.PG, base_id=3100,
    )
    team_snapshot = build_team_snapshot(team, roster, (), CAP_SETTINGS)
    assert len(roster) == 6, "fixture sanity: 2 covering + 1 expiring + 3 lapsed"

    result = gm_service._next_season_roster_count(team_snapshot, _SEASON + 1)

    assert result == 5


def test_occupies_next_season_excludes_a_player_past_retirement_age_when_set():
    """Decision P2-37 / architect round 18 §4.7 Shape 1. A player with a
    contract that COVERS `next_season` (so the pre-P2-37 contract-expiry
    check alone would say `True`) but who will be strictly older than
    `retirement_age` at `next_season` must be excluded once `retirement_
    age` is supplied -- this is the exact overcount `_enforce_roster_
    floor` consumed at the reproducer (`rng_seed=2636832537024722024`,
    team 28, season 2029). Age 41 at `next_season` against `retirement_
    age=40` (`_gm_progression_stub.py`'s own `is_retiring = age > 40`
    boundary, matched exactly, not `>=`)."""
    rp = make_roster_player(
        5000, age=41, season=_SEASON + 1, overall_cached=70, team_id=1,
        contract=make_contract(
            5000, team_id=1, start_season=_SEASON,
            salaries=(3_000_000, 3_000_000),  # covers _SEASON and _SEASON + 1
        ),
    )

    assert gm_service._occupies_next_season(rp, _SEASON + 1, retirement_age=40) is False


def test_occupies_next_season_retirement_age_none_is_byte_identical_to_pre_p2_37():
    """The same age-41-at-`next_season` player, covering contract, but
    `retirement_age=None` (the default) -- must read exactly as it did
    before Decision P2-37 existed: `True`, contract-expiry basis only, no
    age check performed at all. This is the fast, cheap guard that a
    caller which never supplies `retirement_age` (every pre-P2-37 call
    site before this round's build) sees zero behaviour change."""
    rp = make_roster_player(
        5001, age=41, season=_SEASON + 1, overall_cached=70, team_id=1,
        contract=make_contract(
            5001, team_id=1, start_season=_SEASON,
            salaries=(3_000_000, 3_000_000),  # covers _SEASON and _SEASON + 1
        ),
    )

    assert gm_service._occupies_next_season(rp, _SEASON + 1) is True
    assert gm_service._occupies_next_season(rp, _SEASON + 1, retirement_age=None) is True


def test_occupies_next_season_retirement_age_boundary_is_strictly_greater_than():
    """A player who will be EXACTLY `retirement_age` at `next_season` (not
    strictly older) still occupies the slot -- matches `_gm_progression_
    stub.py`'s own `is_retiring = age > _RETIREMENT_AGE` strict
    inequality exactly, not `>=`."""
    rp = make_roster_player(
        5002, age=40, season=_SEASON + 1, overall_cached=70, team_id=1,
        contract=make_contract(
            5002, team_id=1, start_season=_SEASON,
            salaries=(3_000_000, 3_000_000),  # covers _SEASON and _SEASON + 1
        ),
    )

    assert gm_service._occupies_next_season(rp, _SEASON + 1, retirement_age=40) is True


# ---------------------------------------------------------------------------
# Architect round 12 (docs/ledger/reports/phase-2-architect-round-12.md
# §5.5) -- `_occupies_next_season` extraction + `_build_throw_ins`'s
# occupancy-balance rule. Test 1, the property test, lives here next to
# `_next_season_roster_count`'s own tests; tests 2-6 live back up in the
# `_build_throw_ins` section above.
# ---------------------------------------------------------------------------


def test_occupies_next_season_agrees_with_next_season_roster_count_exactly():
    """§5.5 test 1. `_occupies_next_season` is a pure extraction of `_next_
    season_roster_count`'s own comprehension body (round 12 §5.3 step 1) --
    this property must hold for every roster shape the extraction's source
    function was ever proven correct against: covering, expiring,
    contract-less, and lapsed-contract players, in every mixture and
    count. Randomized over 200 trials at a fixed seed (not `hypothesis`,
    not used elsewhere in this codebase) rather than hand-picking a few
    combinations, so the property is checked broadly rather than pinned
    to the handful of shapes `_build_mixed_roster`'s own named tests
    happen to use.

    **Honest scope note.** Since `_next_season_roster_count` is now a thin
    `sum` over `_occupies_next_season` (both defined in the same module),
    this equality is self-referential against the SHARED implementation --
    it cannot, on its own, prove the refactor preserved the pre-round-12
    behaviour of `_next_season_roster_count` (a mutation to `_occupies_
    next_season` itself moves both sides identically, verified locally:
    flipping its `!=` to `==` leaves this test green). That behaviour-
    preservation claim is instead carried by `_next_season_roster_count`'s
    own pre-existing pinned-value tests (`test_next_season_roster_count_
    counts_covering_expiring_and_contractless_correctly` et al., unedited
    by this round and still asserting exact literal counts against real
    fixtures), which still pass unchanged.

    **What this test DOES kill, verified locally:** the two definitions
    drifting apart in a future edit -- e.g. `_next_season_roster_count`
    gaining its own local adjustment instead of routing through `_occupies_
    next_season` (a `+ 1` mutation added directly to its `return` line
    fails this test at trial 0, `6 != 7`) -- exactly the defect class
    (`_build_throw_ins`'s basis silently diverging from `_next_season_
    roster_count`'s) this whole round exists to close, guarded here so it
    cannot recur unnoticed a second time."""
    rng = random.Random(120926)
    trials = 200
    for trial in range(trials):
        n_covering = rng.randint(0, 5)
        n_expiring = rng.randint(0, 5)
        n_contractless = rng.randint(0, 5)
        n_lapsed = rng.randint(0, 5)
        if n_covering + n_expiring + n_contractless + n_lapsed == 0:
            continue  # an empty roster proves nothing about the predicate
        roster = _build_mixed_roster(
            1, n_covering=n_covering, n_expiring=n_expiring, n_contractless=n_contractless,
            n_lapsed=n_lapsed, avoid_position=Position.PG, base_id=90_000 + trial * 100,
        )
        team_snapshot = build_team_snapshot(make_team(1), roster, (), CAP_SETTINGS)
        next_season = _SEASON + 1

        expected = gm_service._next_season_roster_count(team_snapshot, next_season)
        actual = sum(
            1 for rp in team_snapshot.roster if gm_service._occupies_next_season(rp, next_season)
        )
        assert actual == expected, (
            f"trial {trial}: n_covering={n_covering} n_expiring={n_expiring} "
            f"n_contractless={n_contractless} n_lapsed={n_lapsed} -- "
            f"_occupies_next_season sum ({actual}) disagreed with "
            f"_next_season_roster_count ({expected})"
        )


def test_run_free_agency_enforces_the_roster_floor_through_the_real_call_chain():
    """B2 fix (critic round 19): the prior version of this test built its
    own `roster_count` dict and handed it straight to `_enforce_roster_
    floor`, calling neither `run_free_agency` nor `_next_season_roster_
    count` -- reverting the real B1 fix left it passing, proving it could
    not catch a regression in the actual seed logic. This version drives
    the real end-to-end `run_free_agency` and proves `_enforce_roster_
    floor`'s floor-fill genuinely fires as part of that real call chain.

    2 teams, both starting with 2 contract-less rostered bodies (`_next_
    season_roster_count` == 2 for each, well below `roster_min` == 14) and
    identical `avoid_position` (both compete for the same free-agent
    shape), given a shared pool of 26 real free agents and zero cap room
    each -- deliberately scarce relative to combined league-wide demand
    (2 teams x up to 13 open slots each). Team 2's fixture happens to win
    more of the contested bids (real `fit_value`/rng-derived tie-breaking,
    not manufactured) and reaches `roster_max` (15) entirely through normal
    FA rounds; team 1 falls 5 slots short of `roster_min` (14) via normal
    rounds alone, and only reaches it because `_enforce_roster_floor`
    floor-signs the remaining 5 at `min_salary` after normal rounds
    terminate with pool still nonempty. This exact fixture/count was
    verified against the real implementation, not hand-derived."""
    teams = []
    for team_id, base_id in ((1, 3000), (2, 4000)):
        team = make_team(team_id)
        roster = _build_mixed_roster(
            team_id, n_covering=0, n_expiring=0, n_contractless=2,
            avoid_position=Position.C, base_id=base_id,
        )
        teams.append(build_team_snapshot(team, roster, (), CAP_SETTINGS))
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, tuple(teams))
    ctx = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=0)

    for t in snapshot.teams:
        assert gm_service._next_season_roster_count(t, ctx.next_season) == 2, (
            "fixture sanity: both teams start with exactly 2 contract-less bodies "
            "counting toward next season, well below roster_min"
        )

    pool = tuple(
        make_roster_player(
            9000 + i, age=27, season=_SEASON, overall_cached=92,
            primary_position=list(Position)[i % len(list(Position))],
            team_id=None, peak_age_start=27, peak_age_end=31,
        )
        for i in range(26)
    )

    result = run_free_agency(snapshot, ctx, pool=pool, pending_offers=())

    from collections import Counter

    signings_by_team: dict[int, list] = {1: [], 2: []}
    for s in result.signings:
        signings_by_team[s.team_id].append(s)

    assert len(signings_by_team[1]) == 12
    assert len(signings_by_team[2]) == 13
    assert Counter(s.signing_method for s in signings_by_team[1]) == {
        gm_service.SigningMethod.MLE: 1,
        gm_service.SigningMethod.BI_ANNUAL: 1,
        gm_service.SigningMethod.MIN_EXCEPTION: 10,
    }
    assert Counter(s.signing_method for s in signings_by_team[2]) == {
        gm_service.SigningMethod.MLE: 1,
        gm_service.SigningMethod.BI_ANNUAL: 1,
        gm_service.SigningMethod.MIN_EXCEPTION: 11,
    }

    floor_records_by_team = Counter(
        r.team_id for r in result.records if r.verdict == "floor_sign"
    )
    assert floor_records_by_team == {1: 5}, (
        "only team 1 needs floor-fill -- team 2 reaches roster_max through "
        "normal FA rounds alone"
    )

    final_count = {
        t.id: gm_service._next_season_roster_count(t, ctx.next_season) + len(signings_by_team[t.id])
        for t in snapshot.teams
    }
    assert final_count == {1: CAP_SETTINGS.roster_min, 2: CAP_SETTINGS.roster_max}
    assert all(r.reconstructs(tolerance=1e-6) for r in result.records)


# ---------------------------------------------------------------------------
# run_waivers -- T7a2 (architect round 8 §7.3), B4 fix. Every sibling pure-
# plan function in this module (`run_trade_window`, `run_free_agency`, `run_
# resign_window`, `run_draft_phase`, `_enforce_roster_floor`, `_next_season_
# roster_count`) has direct tests here; `run_waivers`/`WaiverExecution`/
# `WaiverWindowResult` had none before this fix, breaking that established
# convention. `_build_mixed_roster` (above, `run_free_agency`'s own T6a
# fixture) is reused wholesale -- it already builds the exact "covering vs.
# expiring vs. contract-less" shapes `run_waivers`'s own B1 fix (`_next_
# season_roster_count`-based trigger, expiring-this-season candidates
# excluded from the cut pool) needs to be exercised meaningfully.
# ---------------------------------------------------------------------------


def test_run_waivers_trims_to_next_season_roster_count_target_not_raw_body_count():
    """B1's own direct regression, at the `run_waivers` call boundary
    rather than `select_waive_candidate`'s own unit level (`tests/unit/
    managers/decisions/test_waivers.py` already covers that primitive
    directly). 17 `n_covering` + 3 `n_expiring` = 20 raw rostered bodies,
    but `_next_season_roster_count` == 17 (the 3 expiring players have no
    legal claim on a next-season slot and are also excluded from the cut
    pool entirely -- `select_waive_candidate`'s own B1 fix). `roster_max`
    == 15, so the trigger must cut exactly `17 - 15 == 2` players -- not
    `20 - 15 == 5` (the pre-fix, raw-body-count basis) and not `0` (which
    would happen if the trigger read the already-under-15 `n_covering`
    figure some other wrong way). Every cut must come from the 17
    covering players; none of the 3 expiring players may ever be selected
    (they are not in the eligible pool at all)."""
    team = make_team(1)
    roster = _build_mixed_roster(
        1, n_covering=17, n_expiring=3, avoid_position=Position.C, base_id=7000
    )
    snapshot = build_league_snapshot(
        _LEAGUE_ID, _SEASON, (build_team_snapshot(team, roster, (), CAP_SETTINGS),)
    )
    ctx = _make_ctx(snapshot, phase=Phase.WAIVERS)
    team_snapshot = snapshot.teams[0]
    assert len(team_snapshot.roster) == 20, "fixture sanity: 17 covering + 3 expiring"
    assert gm_service._next_season_roster_count(team_snapshot, ctx.next_season) == 17, (
        "fixture sanity: only the 17 covering players count toward next season"
    )

    result = run_waivers(snapshot, ctx)

    assert len(result.executions) == 2
    assert len(result.records) == 2
    expiring_ids = {rp.id for rp in roster[17:20]}
    covering_ids = {rp.id for rp in roster[:17]}
    cut_ids = {e.player_id for e in result.executions}
    assert cut_ids.isdisjoint(expiring_ids), (
        "an expiring-this-season player must never be selected for a cut"
    )
    assert cut_ids <= covering_ids
    assert all(e.team_id == 1 for e in result.executions)
    assert all(e.waived_in_season == _SEASON for e in result.executions)
    assert all(r.verdict == "waive" and r.reconstructs(tolerance=1e-6) for r in result.records)


def test_run_waivers_already_cut_prevents_selecting_the_same_player_twice():
    """The `already_cut` threading (§7.3's own second hard guard) at the
    `run_waivers` integration level -- 3 cuts required from one team in a
    single pass, forcing `select_waive_candidate` to be called 3 times
    with a growing `already_cut` set. Every execution's `player_id` must
    be distinct; a regression that stopped threading `already_cut`
    forward would let the same (lowest-fit-value) candidate be re-
    selected every iteration instead, collapsing 3 cuts down to 1 real
    player id repeated 3 times."""
    team = make_team(1)
    roster = _build_mixed_roster(
        1, n_covering=18, n_expiring=0, avoid_position=Position.C, base_id=7100
    )
    snapshot = build_league_snapshot(
        _LEAGUE_ID, _SEASON, (build_team_snapshot(team, roster, (), CAP_SETTINGS),)
    )
    ctx = _make_ctx(snapshot, phase=Phase.WAIVERS)
    assert gm_service._next_season_roster_count(snapshot.teams[0], ctx.next_season) == 18

    result = run_waivers(snapshot, ctx)

    assert len(result.executions) == 3  # 18 - roster_max(15)
    cut_ids = [e.player_id for e in result.executions]
    assert len(set(cut_ids)) == 3, "every cut must select a distinct player"


def test_run_waivers_asserts_when_a_cut_would_drop_a_team_below_roster_min():
    """The `roster_min` floor guard (§7.3: "asserted rather than silently
    trusted") is structurally unreachable under any real `LeagueCapSettings`
    (`roster_max >= roster_min` always holds for a legal league) -- only
    reachable here by deliberately constructing an invalid `next_season_
    cap_settings` with `roster_max < roster_min`, forcing the trigger loop
    to keep cutting past the floor it is meant to respect."""
    team = make_team(1)
    roster = _build_mixed_roster(
        1, n_covering=20, n_expiring=0, avoid_position=Position.C, base_id=7200
    )
    snapshot = build_league_snapshot(
        _LEAGUE_ID, _SEASON, (build_team_snapshot(team, roster, (), CAP_SETTINGS),)
    )
    ctx = _make_ctx(snapshot, phase=Phase.WAIVERS)
    broken_next_season_cap_settings = dataclasses.replace(
        ctx.next_season_cap_settings, roster_max=10, roster_min=14
    )
    ctx = dataclasses.replace(ctx, next_season_cap_settings=broken_next_season_cap_settings)

    with pytest.raises(AssertionError, match="below roster_min"):
        run_waivers(snapshot, ctx)


def test_run_waivers_execution_fields_match_the_selected_candidate():
    """`WaiverExecution`'s fields (ids only, never object references -- the
    same `TradeExecution`/`SigningExecution` convention) must be populated
    from the real selected candidate, not a placeholder: `team_id` is the
    team being trimmed, `contract_id` is that exact player's own active
    contract id, `waived_in_season` is `ctx.season` (not `ctx.next_season`
    -- the cut executes THIS season even though the roster-count target it
    is measured against is next season's), and `record` is the same
    `DecisionRecord` `select_waive_candidate` returned alongside the cut."""
    team = make_team(1)
    roster = _build_mixed_roster(
        1, n_covering=16, n_expiring=0, avoid_position=Position.C, base_id=7300
    )
    snapshot = build_league_snapshot(
        _LEAGUE_ID, _SEASON, (build_team_snapshot(team, roster, (), CAP_SETTINGS),)
    )
    ctx = _make_ctx(snapshot, phase=Phase.WAIVERS)
    assert gm_service._next_season_roster_count(snapshot.teams[0], ctx.next_season) == 16

    result = run_waivers(snapshot, ctx)

    assert len(result.executions) == 1
    execution = result.executions[0]
    cut_rp = next(rp for rp in roster if rp.id == execution.player_id)
    assert execution.team_id == 1
    assert cut_rp.contract is not None
    assert execution.contract_id == cut_rp.contract.contract.id
    assert execution.waived_in_season == _SEASON
    assert execution.record is result.records[0]
    assert execution.record.subject == f"player:{execution.player_id}"
    assert execution.record.verdict == "waive"
    assert execution.record.reconstructs(tolerance=1e-6)


def test_run_waivers_signs_nobody_when_already_at_or_under_roster_max():
    """No trigger, no executions -- the loop condition's own negative case,
    proving `run_waivers` is a genuine no-op (not just "cuts 0 by
    coincidence") when a team's next-season occupancy is already legal."""
    team = make_team(1)
    roster = _build_mixed_roster(
        1, n_covering=15, n_expiring=0, avoid_position=Position.C, base_id=7400
    )
    snapshot = build_league_snapshot(
        _LEAGUE_ID, _SEASON, (build_team_snapshot(team, roster, (), CAP_SETTINGS),)
    )
    ctx = _make_ctx(snapshot, phase=Phase.WAIVERS)
    assert gm_service._next_season_roster_count(snapshot.teams[0], ctx.next_season) == 15

    result = run_waivers(snapshot, ctx)

    assert result.executions == ()
    assert result.records == ()


def test_effective_capacity_and_finalize_signing_tag_boundary_values():
    """B1b tag-boundary regression (critic round 18): the tag must always
    match whichever bucket actually covered the signed salary, never a
    sign-of-`room` guess. Table swept across critic's exact boundary
    values around `min_salary` ($1,272,870): a `room` too small to cover
    even the minimum salary must fall through to the largest unused
    exception (MLE, since it dominates bi-annual), while `room` at or
    above `min_salary` covers it directly via CAP_SPACE."""
    cases = [
        (1, gm_service.SigningMethod.MLE),
        (500_000, gm_service.SigningMethod.MLE),
        (CAP_SETTINGS.min_salary - 1, gm_service.SigningMethod.MLE),
        (CAP_SETTINGS.min_salary, gm_service.SigningMethod.CAP_SPACE),
        (2_000_000, gm_service.SigningMethod.CAP_SPACE),
    ]
    for room, expected_method in cases:
        room_remaining = {1: room}
        exceptions_used: dict[int, set[str]] = {1: set()}
        capacity = gm_service._effective_capacity(1, room_remaining, exceptions_used, CAP_SETTINGS)
        salary = min(capacity, CAP_SETTINGS.min_salary)
        method = gm_service._finalize_signing(
            1, salary, room_remaining, exceptions_used, CAP_SETTINGS
        )
        assert method == expected_method, f"room={room}: expected {expected_method}, got {method}"
        if expected_method == gm_service.SigningMethod.CAP_SPACE:
            assert room_remaining[1] == room - salary
            assert exceptions_used[1] == set()
        else:
            assert room_remaining[1] == room, "a non-CAP_SPACE bucket must not touch room"
            assert "mle" in exceptions_used[1]


# ---------------------------------------------------------------------------
# run_draft_phase
# ---------------------------------------------------------------------------


def test_run_draft_phase_assigns_distinct_prospects_and_every_pick_reconstructs():
    snapshot = _make_small_league(n_teams=4)
    ctx = _make_ctx(snapshot, phase=Phase.DRAFT)
    prospects = tuple(_make_prospect(9000 + i, age=19 + (i % 3), overall=70 - i) for i in range(6))
    team_cycle = tuple(t.id for t in snapshot.teams) * 2  # 8 slots, only 6 prospects
    draft_order = tuple(
        _make_pick(
            5000 + i, season=_SEASON, original_team_id=team_id, current_team_id=team_id,
            pick_number=i + 1,
        )
        for i, team_id in enumerate(team_cycle)
    )

    result = run_draft_phase(snapshot, ctx, prospects=prospects, draft_order=draft_order)

    assert len(result.picks) == len(prospects)  # stops once the pool is exhausted
    picked_ids = [p.prospect_id for p in result.picks]
    assert len(set(picked_ids)) == len(picked_ids)
    assert all(pick.record.reconstructs(tolerance=1e-6) for pick in result.picks)


# ---------------------------------------------------------------------------
# _next_pick_pointer -- P5-89 instrument 1 (Decision P5-98.a)
# ---------------------------------------------------------------------------


def _pointer_basis() -> tuple[DraftPick, ...]:
    """A four-pick eligible list whose third and fourth picks sit in round
    2 -- the round boundary is the whole point. `draft_selections` carries
    `UNIQUE (draft_id, pick_number)`, so pick numbers are unique across the
    WHOLE draft: the pick after round 1 pick 30 is round 2 pick 31, not
    round 1 pick 31. A pointer implementation that assumes contiguity
    inside one round is right on rows 1 and 4 and wrong on row 2."""
    return (
        _make_pick(5001, season=_SEASON, original_team_id=1, current_team_id=1, pick_number=29),
        _make_pick(5002, season=_SEASON, original_team_id=2, current_team_id=2, pick_number=30),
        _make_pick(
            5003, season=_SEASON, original_team_id=2, current_team_id=2, pick_number=31, round_=2
        ),
        _make_pick(
            5004, season=_SEASON, original_team_id=1, current_team_id=1, pick_number=32, round_=2
        ),
    )


@pytest.mark.parametrize(
    ("index", "expected"),
    [
        (0, (1, 30)),  # mid-list: the next pick's own (round, pick_number)
        (1, (2, 31)),  # the next element crosses a round boundary
        (3, (2, 33)),  # last element: (this round, this number + 1)
    ],
    ids=["mid_list", "crosses_round_boundary", "last_element"],
)
def test_next_pick_pointer_reads_the_next_pick_or_one_past_the_last(index, expected):
    """The extracted pointer arithmetic, driven directly against a
    hand-built list -- the table `execute_draft`'s inline expression could
    never have (its only observation point was a real Postgres round trip
    through the DRAFT branch, and the one committed pointer assertion,
    `test_gm_service.py:1009`, is `!= (1, 1)`).

    Every expectation is a hand-written literal, never a re-derivation
    from the input list: a test that recomputes the answer the same way
    the code does agrees with the code's own bug."""
    assert gm_service._next_pick_pointer(_pointer_basis(), index) == expected


def test_next_pick_pointer_on_a_one_element_list_points_one_past_it():
    """The degenerate basis -- the only pick is also the last pick, so the
    last-element branch is the only branch, and it must not read off the
    end of the list."""
    picks = (
        _make_pick(
            5005, season=_SEASON, original_team_id=3, current_team_id=3, pick_number=60, round_=2
        ),
    )
    assert gm_service._next_pick_pointer(picks, 0) == (2, 61)


# ---------------------------------------------------------------------------
# run_trade_window -- selection/cap logic, isolated from the trade formula
# ---------------------------------------------------------------------------


def _accept_everything_record(team_snapshot, incoming, outgoing, philosophy, window, **_kwargs):
    subject = (
        "in:" + ",".join(str(rp.id) for rp in incoming.players)
        + "|out:" + ",".join(str(rp.id) for rp in outgoing.players)
    )
    return DecisionRecord(
        team_id=team_snapshot.id, philosophy=philosophy, window=window,
        subject=subject, verdict="accept", score=100.0, threshold=0.0, terms=(),
    )


def _always_legal(*_args, **_kwargs):
    return None


def _five_team_cap_binding_scenario():
    """Team 1 has 4 independent, asset-disjoint, unconditionally-accepted
    trade opportunities (one with each of teams 2-5) -- one more than
    `MAX_TRADES_PER_TEAM_PER_WINDOW` (3) allows, so exactly one must be
    rejected by the cap alone (every candidate is legal and mutually
    accepted by construction)."""
    team_ids = (1, 2, 3, 4, 5)
    teams = tuple(
        _build_team_snapshot(tid, avoid_position=Position.PG, base_id=1000 * tid)
        for tid in team_ids
    )
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, teams)
    ctx = _make_ctx(snapshot)

    fixed_candidates: dict[tuple[int, int], tuple[CandidatePackage, ...]] = {}
    for partner in (2, 3, 4, 5):
        a_player = snapshot.team_by_id(1).roster[partner]  # a distinct A player per partner
        b_player = snapshot.team_by_id(partner).roster[0]
        fixed_candidates[(1, partner)] = (
            CandidatePackage(
                team_a_id=1, team_b_id=partner,
                incoming_a=AssetBundle(players=(b_player,)),
                outgoing_a=AssetBundle(players=(a_player,)),
            ),
        )

    def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
        return fixed_candidates.get((proposer_id, partner_id), ())

    return snapshot, ctx, _fake_propose_packages


def test_run_trade_window_enforces_the_per_team_and_window_caps(monkeypatch):
    snapshot, ctx, fake_propose_packages = _five_team_cap_binding_scenario()
    monkeypatch.setattr(gm_service, "propose_packages", fake_propose_packages)
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)
    monkeypatch.setattr(gm_service.trade_decisions, "_check_legality", _always_legal)

    result = run_trade_window(snapshot, ctx)

    assert result.packages_evaluated == 4
    assert len(result.executions) == MAX_TRADES_PER_TEAM_PER_WINDOW
    team1_trades = sum(1 for e in result.executions if 1 in (e.team_a_id, e.team_b_id))
    assert team1_trades == MAX_TRADES_PER_TEAM_PER_WINDOW
    assert len(result.executions) <= gm_service.MAX_TRADES_PER_WINDOW
    # every evaluated candidate's records are returned, not only executed ones
    assert len(result.records) == 2 * result.packages_evaluated


def test_run_trade_window_selection_is_stable_under_team_order_permutation(monkeypatch):
    snapshot, ctx, fake_propose_packages = _five_team_cap_binding_scenario()
    monkeypatch.setattr(gm_service, "propose_packages", fake_propose_packages)
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)
    monkeypatch.setattr(gm_service.trade_decisions, "_check_legality", _always_legal)

    forward = run_trade_window(snapshot, ctx)
    reversed_snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, tuple(reversed(snapshot.teams)))
    reversed_result = run_trade_window(reversed_snapshot, ctx)

    def _summarize(result):
        return tuple(
            sorted(
                (e.team_a_id, e.team_b_id, e.players_a_to_b, e.players_b_to_a)
                for e in result.executions
            )
        )

    assert _summarize(forward) == _summarize(reversed_result)
    assert len(forward.executions) == MAX_TRADES_PER_TEAM_PER_WINDOW


def test_run_trade_window_never_consumes_an_asset_twice(monkeypatch):
    team_ids = (1, 2, 3)
    teams = tuple(
        _build_team_snapshot(tid, avoid_position=Position.PG, base_id=1000 * tid)
        for tid in team_ids
    )
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, teams)
    ctx = _make_ctx(snapshot)

    shared_player = snapshot.team_by_id(1).roster[0]
    b_player = snapshot.team_by_id(2).roster[0]
    c_player = snapshot.team_by_id(3).roster[0]

    fixed_candidates = {
        (1, 2): (
            CandidatePackage(
                team_a_id=1, team_b_id=2,
                incoming_a=AssetBundle(players=(b_player,)),
                outgoing_a=AssetBundle(players=(shared_player,)),
            ),
        ),
        (1, 3): (
            CandidatePackage(
                team_a_id=1, team_b_id=3,
                incoming_a=AssetBundle(players=(c_player,)),
                outgoing_a=AssetBundle(players=(shared_player,)),  # same asset, again
            ),
        ),
    }

    def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
        return fixed_candidates.get((proposer_id, partner_id), ())

    monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)
    monkeypatch.setattr(gm_service.trade_decisions, "_check_legality", _always_legal)

    result = run_trade_window(snapshot, ctx)

    assert len(result.executions) == 1
    all_outgoing_players = [pid for e in result.executions for pid in e.players_a_to_b]
    assert len(all_outgoing_players) == len(set(all_outgoing_players))
    assert result.executions[0].team_b_id == 2  # (1, 2) sorts ahead of (1, 3) on the tie-break


# ---------------------------------------------------------------------------
# run_trade_window -- real formulas end-to-end (Nit 1, critic round 17)
# ---------------------------------------------------------------------------


def test_run_trade_window_executes_a_genuine_mutually_beneficial_trade_under_real_formulas(
    monkeypatch,
):
    """Nit 1 (critic round 17): every `run_trade_window` test above
    monkeypatches BOTH `evaluate_trade` and `_check_legality`, so nothing
    exercises the real formula layer through `run_trade_window` at all.
    Mirrors `test_trade.py::_contender_and_rebuilder`'s own proven-accept-
    both-sides trade (a CONTEND team's positional hole filled by a REBUILD
    team's aging star, paid for in picks) -- only `propose_packages` is
    faked here (candidate *generation* is `propose_packages`'s own,
    already-covered concern), so `evaluate_trade`/`_check_legality` run
    for real, end-to-end, through `run_trade_window`'s own selection and
    execution loop."""
    team_a = make_team(1)
    team_b = make_team(2)

    star = make_roster_player(
        100, age=33, season=_SEASON, overall_cached=90, primary_position=Position.SF,
        team_id=2, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            100, team_id=2, start_season=_SEASON,
            salaries=(38_000_000, 40_000_000, 42_000_000),
        ),
    )
    roster_a = _filler_roster(1, 14, avoid_position=Position.SF, base_id=1000)
    roster_b = (star,) + _filler_roster(2, 14, avoid_position=Position.SF, base_id=2000)

    snapshot_a = build_team_snapshot(team_a, roster_a, (), CAP_SETTINGS)
    snapshot_b = build_team_snapshot(team_b, roster_b, (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (snapshot_a, snapshot_b))

    pick_1 = _make_pick(9001, season=_SEASON, original_team_id=1, current_team_id=1, pick_number=2)
    pick_2 = _make_pick(9002, season=_SEASON, original_team_id=1, current_team_id=1, pick_number=5)

    ctx = dataclasses.replace(
        _make_ctx(snapshot),
        room_by_team={1: 40_000_000, 2: 60_000_000},
        philosophy_by_team={1: GmPhilosophy.STAR_MAXER, 2: GmPhilosophy.YOUTH_DEVELOPER},
        window_by_team={1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD},
    )

    candidate = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(star,)),
        outgoing_a=AssetBundle(picks=(pick_1, pick_2)),
    )

    def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
        return (candidate,) if (proposer_id, partner_id) == (1, 2) else ()

    monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)

    result = run_trade_window(snapshot, ctx)

    assert len(result.executions) == 1
    execution = result.executions[0]
    assert execution.team_a_id == 1
    assert execution.team_b_id == 2
    assert execution.players_b_to_a == (star.id,)
    assert set(execution.picks_a_to_b) == {pick_1.id, pick_2.id}
    assert execution.record_a.verdict == "accept"
    assert execution.record_b.verdict == "accept"
    assert execution.record_a.reconstructs()
    assert execution.record_b.reconstructs()


# ---------------------------------------------------------------------------
# run_trade_window -- Phase 5 Stage A step A-23's human-managed-team skip
# (Decisions P5-44 through P5-91): the one-sided divert, the four gates,
# the per-human-team cap, and head-to-head inertness. Reuses the exact
# "genuine mutually beneficial trade" fixture shape immediately above --
# only `ctx.self_managed_team_ids` changes -- so a real, proven-accept-
# both-sides candidate under real formulas is what gets diverted, not a
# candidate that merely looks plausible.
# ---------------------------------------------------------------------------


def _human_offer_fixture(monkeypatch):
    """The identical fixture `test_run_trade_window_executes_a_genuine_
    mutually_beneficial_trade_under_real_formulas` uses, factored out so
    every test below shares one proven-accept candidate rather than each
    inventing its own and risking a formula-dependent false negative."""
    team_a = make_team(1)
    team_b = make_team(2)

    star = make_roster_player(
        100, age=33, season=_SEASON, overall_cached=90, primary_position=Position.SF,
        team_id=2, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            100, team_id=2, start_season=_SEASON,
            salaries=(38_000_000, 40_000_000, 42_000_000),
        ),
    )
    roster_a = _filler_roster(1, 14, avoid_position=Position.SF, base_id=1000)
    roster_b = (star,) + _filler_roster(2, 14, avoid_position=Position.SF, base_id=2000)

    snapshot_a = build_team_snapshot(team_a, roster_a, (), CAP_SETTINGS)
    snapshot_b = build_team_snapshot(team_b, roster_b, (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (snapshot_a, snapshot_b))

    pick_1 = _make_pick(9001, season=_SEASON, original_team_id=1, current_team_id=1, pick_number=2)
    pick_2 = _make_pick(9002, season=_SEASON, original_team_id=1, current_team_id=1, pick_number=5)

    ctx = dataclasses.replace(
        _make_ctx(snapshot),
        room_by_team={1: 40_000_000, 2: 60_000_000},
        philosophy_by_team={1: GmPhilosophy.STAR_MAXER, 2: GmPhilosophy.YOUTH_DEVELOPER},
        window_by_team={1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD},
    )

    candidate = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(star,)),
        outgoing_a=AssetBundle(picks=(pick_1, pick_2)),
    )

    def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
        return (candidate,) if (proposer_id, partner_id) == (1, 2) else ()

    monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)
    return snapshot, ctx, candidate, star, pick_1, pick_2


def test_run_trade_window_diverts_ai_to_human_offer_instead_of_auto_resolving(monkeypatch):
    """Decision P5-55 (corrected by P5-80/P5-91)/P5-56/P5-57, as corrected
    by round-7's P5-92 -- team 2 human-managed, team 1 AI: the AI
    genuinely proposes (a real candidate really is built and evaluated,
    `offers_evaluated == 1`), but nothing auto-resolves it. Zero
    executions, one `PendingOffer` carrying the proposer's own record and
    the unmodified package, and `result.offer_records` carries ONLY the
    proposer's record -- never one naming team 2 (P5-56's ban, mechanically
    checked).

    **Round-7 critic fix (blocking issue 1).** `result.records` and
    `result.packages_evaluated` must NOT see this diverted candidate at
    all -- an earlier version of this test asserted `packages_evaluated ==
    1` and `len(records) == 1`, which is exactly the corruption an
    independent critic re-verification found: `records`' own docstring
    asserts a `(record_a, record_b)`-pair-per-negotiation-turn contract
    that `tools/gm_harness/season.py::_count_mutual_accepts` asserts
    `len(records) % 2 == 0` on, and a diverted candidate (one unpaired
    `record_a`, no `record_b`) breaks that outright on an odd count and
    silently mis-pairs on an even one (see the two-candidate case below)."""
    snapshot, ctx, candidate, star, pick_1, pick_2 = _human_offer_fixture(monkeypatch)
    ctx = dataclasses.replace(ctx, self_managed_team_ids=frozenset({2}))

    result = run_trade_window(snapshot, ctx)

    assert result.executions == ()
    assert result.packages_evaluated == 0
    assert result.offers_evaluated == 1
    assert result.offer_evaluations_performed == 1
    assert len(result.offers) == 1
    offer = result.offers[0]
    assert isinstance(offer, PendingOffer)
    assert offer.from_team_id == 1
    assert offer.to_team_id == 2
    assert offer.package is candidate
    assert offer.record.verdict == "accept"
    assert offer.record.team_id == 1
    assert offer.record.reconstructs()

    assert result.records == ()
    assert len(result.offer_records) == 1
    assert result.offer_records[0] is offer.record
    assert all(r.team_id != 2 for r in result.offer_records)


def test_run_trade_window_diverted_candidates_never_break_records_pairing_contract(
    monkeypatch,
):
    """Round-7 critic fix (blocking issue 1) -- the real repro. Two
    diverted candidates (an EVEN count, the mis-pairing failure mode: an
    odd count asserts outright, but an even one used to pass
    `_count_mutual_accepts`'s `len(records) % 2 == 0` while silently
    mis-pairing every record from that point on). Two AI proposers, one
    human partner, each proposing one candidate -- `result.records` must
    stay empty (nothing negotiated), `result.offers_evaluated == 2`, and
    `len(result.offer_records) == 2`, one per proposer, matching what
    `season.py::_count_mutual_accepts` and the `packages_evaluated`/
    `outcome_census` metrics this window feeds would need to read
    correctly against a real harness season."""
    team_a = make_team(1)
    team_c = make_team(3)
    human_team = make_team(2)

    asset_a = make_roster_player(
        700, age=27, season=_SEASON, overall_cached=72, primary_position=Position.PG,
        team_id=1, peak_age_start=27, peak_age_end=31,
        contract=make_contract(700, team_id=1, start_season=_SEASON, salaries=(3_000_000,)),
    )
    asset_c = make_roster_player(
        701, age=27, season=_SEASON, overall_cached=72, primary_position=Position.PG,
        team_id=3, peak_age_start=27, peak_age_end=31,
        contract=make_contract(701, team_id=3, start_season=_SEASON, salaries=(3_000_000,)),
    )
    roster_a = (asset_a,) + _filler_roster(1, 13, avoid_position=Position.PG, base_id=10_000)
    roster_c = (asset_c,) + _filler_roster(3, 13, avoid_position=Position.PG, base_id=30_000)
    roster_human = _filler_roster(2, 14, avoid_position=Position.PG, base_id=20_000)

    snapshot = build_league_snapshot(
        _LEAGUE_ID, _SEASON,
        (
            build_team_snapshot(team_a, roster_a, (), CAP_SETTINGS),
            build_team_snapshot(human_team, roster_human, (), CAP_SETTINGS),
            build_team_snapshot(team_c, roster_c, (), CAP_SETTINGS),
        ),
    )

    cand_a = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=()), outgoing_a=AssetBundle(players=(asset_a,)),
    )
    cand_c = CandidatePackage(
        team_a_id=3, team_b_id=2,
        incoming_a=AssetBundle(players=()), outgoing_a=AssetBundle(players=(asset_c,)),
    )

    def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
        if (proposer_id, partner_id) == (1, 2):
            return (cand_a,)
        if (proposer_id, partner_id) == (3, 2):
            return (cand_c,)
        return ()

    monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)
    monkeypatch.setattr(gm_service.trade_decisions, "_check_legality", _always_legal)

    ctx = dataclasses.replace(_make_ctx(snapshot), self_managed_team_ids=frozenset({2}))

    result = run_trade_window(snapshot, ctx)

    # The exact repro: an independent critic re-verification proved this
    # assertion is what `_count_mutual_accepts` runs, and that it either
    # crashes (odd diverted count) or silently mis-pairs (even count, this
    # case) against the pre-fix shape.
    assert len(result.records) % 2 == 0
    assert result.records == ()
    assert result.packages_evaluated == 0
    # `packages_evaluated == 0` must still imply `evaluations_performed ==
    # 0` (that field's own documented contract) even though two real
    # `evaluate_trade` calls really did happen -- they are diverted-branch
    # calls, scoped to `offer_evaluations_performed` instead.
    assert result.evaluations_performed == 0
    assert result.offers_evaluated == 2
    assert result.offer_evaluations_performed == 2
    assert len(result.offer_records) == 2
    assert {r.team_id for r in result.offer_records} == {1, 3}
    assert sum(result.outcome_census.values()) == 0


def test_run_trade_window_mixed_negotiated_and_diverted_candidates_in_one_window(monkeypatch):
    """Re-verification critic finding (post-`c15cff5`): the test directly
    above is all-diverted only -- its `records` field is trivially empty
    (`()`), so it can't exercise the `(record_a, record_b)`-pair-per-turn
    contract across an ACTUAL MIX of negotiated and diverted candidates in
    the same window, which is exactly what `c15cff5`'s own `EXPECTED`
    block claimed was covered ("`_count_mutual_accepts`'s `len(records) %
    2 == 0` assert holds on any window mixing negotiated and diverted
    candidates"). No such test existed until now.

    Three teams: 1 and 3 are AI, 2 is human-managed. `propose_packages` is
    monkeypatched to yield, in the SAME window: a negotiated AI-vs-AI
    candidate each direction (1->3 and 3->1, using four disjoint players
    so both can genuinely execute -- nothing about this test depends on
    either one winning the selection pass) and a diverted AI-to-human
    candidate each direction (1->2 and 3->2). `evaluate_trade` accepts
    unconditionally (`_accept_everything_record`) so every negotiation
    closes in exactly one round-0 turn, and `_check_legality` is bypassed
    -- both already-established techniques from the tests above, not new
    machinery."""
    team_a = make_team(1)
    team_c = make_team(3)
    human_team = make_team(2)

    asset_a1 = make_roster_player(
        700, age=27, season=_SEASON, overall_cached=72, primary_position=Position.PG,
        team_id=1, peak_age_start=27, peak_age_end=31,
        contract=make_contract(700, team_id=1, start_season=_SEASON, salaries=(3_000_000,)),
    )
    asset_a2 = make_roster_player(
        702, age=28, season=_SEASON, overall_cached=70, primary_position=Position.PG,
        team_id=1, peak_age_start=27, peak_age_end=31,
        contract=make_contract(702, team_id=1, start_season=_SEASON, salaries=(3_000_000,)),
    )
    asset_a3 = make_roster_player(
        704, age=25, season=_SEASON, overall_cached=68, primary_position=Position.PG,
        team_id=1, peak_age_start=27, peak_age_end=31,
        contract=make_contract(704, team_id=1, start_season=_SEASON, salaries=(3_000_000,)),
    )
    asset_c1 = make_roster_player(
        701, age=27, season=_SEASON, overall_cached=72, primary_position=Position.PG,
        team_id=3, peak_age_start=27, peak_age_end=31,
        contract=make_contract(701, team_id=3, start_season=_SEASON, salaries=(3_000_000,)),
    )
    asset_c2 = make_roster_player(
        703, age=28, season=_SEASON, overall_cached=70, primary_position=Position.PG,
        team_id=3, peak_age_start=27, peak_age_end=31,
        contract=make_contract(703, team_id=3, start_season=_SEASON, salaries=(3_000_000,)),
    )
    asset_c3 = make_roster_player(
        705, age=25, season=_SEASON, overall_cached=68, primary_position=Position.PG,
        team_id=3, peak_age_start=27, peak_age_end=31,
        contract=make_contract(705, team_id=3, start_season=_SEASON, salaries=(3_000_000,)),
    )

    roster_a = (asset_a1, asset_a2, asset_a3) + _filler_roster(
        1, 11, avoid_position=Position.PG, base_id=10_000
    )
    roster_c = (asset_c1, asset_c2, asset_c3) + _filler_roster(
        3, 11, avoid_position=Position.PG, base_id=30_000
    )
    roster_human = _filler_roster(2, 14, avoid_position=Position.PG, base_id=20_000)

    snapshot = build_league_snapshot(
        _LEAGUE_ID, _SEASON,
        (
            build_team_snapshot(team_a, roster_a, (), CAP_SETTINGS),
            build_team_snapshot(human_team, roster_human, (), CAP_SETTINGS),
            build_team_snapshot(team_c, roster_c, (), CAP_SETTINGS),
        ),
    )

    fixed_candidates = {
        (1, 3): CandidatePackage(
            team_a_id=1, team_b_id=3,
            incoming_a=AssetBundle(players=(asset_c1,)),
            outgoing_a=AssetBundle(players=(asset_a1,)),
        ),
        (3, 1): CandidatePackage(
            team_a_id=3, team_b_id=1,
            incoming_a=AssetBundle(players=(asset_a2,)),
            outgoing_a=AssetBundle(players=(asset_c2,)),
        ),
        (1, 2): CandidatePackage(
            team_a_id=1, team_b_id=2,
            incoming_a=AssetBundle(players=()),
            outgoing_a=AssetBundle(players=(asset_a3,)),
        ),
        (3, 2): CandidatePackage(
            team_a_id=3, team_b_id=2,
            incoming_a=AssetBundle(players=()),
            outgoing_a=AssetBundle(players=(asset_c3,)),
        ),
    }

    def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
        cand = fixed_candidates.get((proposer_id, partner_id))
        return (cand,) if cand is not None else ()

    monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)
    monkeypatch.setattr(gm_service.trade_decisions, "_check_legality", _always_legal)

    ctx = dataclasses.replace(_make_ctx(snapshot), self_managed_team_ids=frozenset({2}))

    result = run_trade_window(snapshot, ctx)

    # The negotiated half (1<->3, both directions): two candidates opened,
    # each closing in exactly one round-0 turn under `_accept_everything_
    # record`, so `records` holds two (record_a, record_b) pairs.
    assert result.packages_evaluated == 2
    assert len(result.records) == 4
    assert len(result.records) % 2 == 0, "the exact invariant _count_mutual_accepts asserts on"
    assert result.evaluations_performed == 4
    assert sum(result.outcome_census.values()) == 2

    # Reimplements `tools/gm_harness/season.py::_count_mutual_accepts`'s
    # own algorithm inline (no cross-layer import from a `tests/unit/
    # services/` file into `tools/`) -- both negotiated candidates here
    # were accepted by both sides, so both pairs must read as mutual
    # accepts, proving the pairing survives genuinely mixed with the
    # diverted candidates below, not merely present alongside empty ones.
    mutual_accepts = sum(
        1
        for i in range(0, len(result.records), 2)
        if result.records[i].verdict == "accept" and result.records[i + 1].verdict == "accept"
    )
    assert mutual_accepts == 2
    assert all(r.team_id != 2 for r in result.records)

    # The diverted half (1->2, 3->2): neither touches `records`/`packages_
    # evaluated`/`outcome_census`/`evaluations_performed` (asserted already
    # above via the fixed totals) -- its own accounting lives only in the
    # three offer-scoped fields.
    assert result.offers_evaluated == 2
    assert result.offer_evaluations_performed == 2
    assert len(result.offer_records) == 2
    assert {r.team_id for r in result.offer_records} == {1, 3}
    assert all(r.team_id != 2 for r in result.offer_records)

    # Both negotiated trades genuinely executed -- disjoint assets, so
    # neither the consumed-players filter nor the per-team/window caps
    # block either one, proving the mixed window's negotiated half isn't
    # merely evaluated but selection/execution-viable too.
    assert len(result.executions) == 2
    executed_pairs = {(e.team_a_id, e.team_b_id) for e in result.executions}
    assert executed_pairs == {(1, 3), (3, 1)}


def test_run_trade_window_never_proposes_as_a_human_managed_team(monkeypatch):
    """Decision P5-44/P5-45's proposer-side half, unchanged across every
    later round -- when team 1 (the proposer in the shared fixture) is
    itself human-managed, `propose_packages` must never even be CALLED
    for that ordered pair (the AI never decides what a human's team
    offers), which this test proves directly rather than merely observing
    an empty result that a different bug could also produce."""
    snapshot, ctx, *_ = _human_offer_fixture(monkeypatch)
    ctx = dataclasses.replace(ctx, self_managed_team_ids=frozenset({1}))

    calls: list[tuple[int, int]] = []
    real_fake = gm_service.propose_packages

    def _spy(proposer_id, partner_id, snap, ctx_, cache):
        calls.append((proposer_id, partner_id))
        return real_fake(proposer_id, partner_id, snap, ctx_, cache)

    monkeypatch.setattr(gm_service, "propose_packages", _spy)

    result = run_trade_window(snapshot, ctx)

    assert (1, 2) not in calls
    assert result.executions == ()
    assert result.offers == ()
    assert result.packages_evaluated == 0
    assert result.records == ()


def test_run_trade_window_offer_gate_filters_out_an_asset_consumed_elsewhere_in_the_window(
    monkeypatch,
):
    """Decision P5-57 gate 4 -- an AI team can offer a human partner an
    asset at scan time and then trade that SAME asset to a third, AI
    team later in the same greedy execution pass; the offer must not
    survive once the asset it names is no longer real. Three teams: team
    1 (AI) proposes the star to team 2 (human) AND, separately, a real
    AI-AI trade elsewhere in the window consumes the star out from under
    that offer."""
    team_a = make_team(1)
    team_b = make_team(2)
    team_c = make_team(3)

    star = make_roster_player(
        100, age=33, season=_SEASON, overall_cached=90, primary_position=Position.SF,
        team_id=1, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            100, team_id=1, start_season=_SEASON,
            salaries=(38_000_000, 40_000_000, 42_000_000),
        ),
    )
    filler_for_star = make_roster_player(
        200, age=24, season=_SEASON, overall_cached=70, primary_position=Position.SF,
        team_id=3, peak_age_start=27, peak_age_end=31,
        contract=make_contract(200, team_id=3, start_season=_SEASON, salaries=(3_000_000,)),
    )
    roster_a = (star,) + _filler_roster(1, 13, avoid_position=Position.SF, base_id=1000)
    roster_b = _filler_roster(2, 14, avoid_position=Position.SF, base_id=2000)
    roster_c = (filler_for_star,) + _filler_roster(3, 13, avoid_position=Position.SF, base_id=3000)

    snapshot_a = build_team_snapshot(team_a, roster_a, (), CAP_SETTINGS)
    snapshot_b = build_team_snapshot(team_b, roster_b, (), CAP_SETTINGS)
    snapshot_c = build_team_snapshot(team_c, roster_c, (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (snapshot_a, snapshot_b, snapshot_c))

    offer_candidate = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=()),
        outgoing_a=AssetBundle(players=(star,)),
    )
    real_trade_candidate = CandidatePackage(
        team_a_id=1, team_b_id=3,
        incoming_a=AssetBundle(players=(filler_for_star,)),
        outgoing_a=AssetBundle(players=(star,)),
    )

    def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
        if (proposer_id, partner_id) == (1, 2):
            return (offer_candidate,)
        if (proposer_id, partner_id) == (1, 3):
            return (real_trade_candidate,)
        return ()

    monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)
    monkeypatch.setattr(gm_service.trade_decisions, "_check_legality", _always_legal)

    ctx = dataclasses.replace(_make_ctx(snapshot), self_managed_team_ids=frozenset({2}))

    result = run_trade_window(snapshot, ctx)

    # The real AI-AI trade (team 1 -> team 3) executed and consumed the
    # star; the offer naming that same star to team 2 must not survive
    # gate 4, even though it was collected during the scan.
    assert any(e.team_a_id == 1 and e.team_b_id == 3 for e in result.executions)
    assert result.offers == ()


def test_run_trade_window_caps_offers_per_human_team_per_window(monkeypatch):
    """Decision P5-57 -- at most `MAX_AI_OFFERS_PER_HUMAN_TEAM_PER_WINDOW`
    offers survive per human team per window, keeping the highest-surplus
    (`record.score - record.threshold`) candidates. Four AI proposers, one
    human partner, four independent legal candidates (disjoint assets, so
    every one of them clears gate 4) -- more than the cap."""
    assert MAX_AI_OFFERS_PER_HUMAN_TEAM_PER_WINDOW == 3
    n_proposers = MAX_AI_OFFERS_PER_HUMAN_TEAM_PER_WINDOW + 1
    human_team_id = 100

    human_team = make_team(human_team_id)
    human_roster = _filler_roster(
        human_team_id, 14, avoid_position=Position.C, base_id=100_000
    )
    proposer_teams = []
    proposer_rosters = {}
    candidates_by_pair = {}
    for i in range(n_proposers):
        pid = i + 1
        asset = make_roster_player(
            500 + i, age=28, season=_SEASON, overall_cached=70 + i, primary_position=Position.C,
            team_id=pid, peak_age_start=27, peak_age_end=31,
            contract=make_contract(
                500 + i, team_id=pid, start_season=_SEASON, salaries=(3_000_000,)
            ),
        )
        roster = (asset,) + _filler_roster(pid, 13, avoid_position=Position.C, base_id=pid * 10_000)
        proposer_teams.append(make_team(pid))
        proposer_rosters[pid] = roster
        candidates_by_pair[(pid, human_team_id)] = CandidatePackage(
            team_a_id=pid, team_b_id=human_team_id,
            incoming_a=AssetBundle(players=()),
            outgoing_a=AssetBundle(players=(asset,)),
        )

    team_snapshots = [
        build_team_snapshot(proposer_teams[i], proposer_rosters[i + 1], (), CAP_SETTINGS)
        for i in range(n_proposers)
    ] + [build_team_snapshot(human_team, human_roster, (), CAP_SETTINGS)]
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, tuple(team_snapshots))

    def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
        cand = candidates_by_pair.get((proposer_id, partner_id))
        return (cand,) if cand is not None else ()

    monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)
    monkeypatch.setattr(gm_service.trade_decisions, "_check_legality", _always_legal)

    # A varying score per proposer (not `_accept_everything_record`'s
    # fixed 100.0) so "keep the highest-surplus candidates" is a real,
    # checkable claim rather than an assertion that happens to pass on a
    # tie -- team 1's offer scores highest, team `n_proposers`'s lowest.
    def _fake_evaluate_trade(team_snapshot, incoming, outgoing, philosophy, window, **_kwargs):
        return DecisionRecord(
            team_id=team_snapshot.id, philosophy=philosophy, window=window,
            subject=f"team:{team_snapshot.id}",
            verdict="accept",
            score=float(100 - team_snapshot.id),
            threshold=0.0,
            terms=(),
        )

    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _fake_evaluate_trade)

    room_by_team = {human_team_id: 60_000_000}
    philosophy_by_team = {human_team_id: GmPhilosophy.YOUTH_DEVELOPER}
    window_by_team = {human_team_id: CompetitiveWindow.REBUILD}
    for i in range(n_proposers):
        pid = i + 1
        room_by_team[pid] = 40_000_000
        philosophy_by_team[pid] = GmPhilosophy.STAR_MAXER
        window_by_team[pid] = CompetitiveWindow.CONTEND

    ctx = dataclasses.replace(
        _make_ctx(snapshot),
        room_by_team=room_by_team,
        philosophy_by_team=philosophy_by_team,
        window_by_team=window_by_team,
        self_managed_team_ids=frozenset({human_team_id}),
    )

    result = run_trade_window(snapshot, ctx)

    assert result.executions == ()
    assert len(result.offers) == MAX_AI_OFFERS_PER_HUMAN_TEAM_PER_WINDOW
    assert all(o.to_team_id == human_team_id for o in result.offers)
    surpluses = [o.record.score - o.record.threshold for o in result.offers]
    assert surpluses == sorted(surpluses, reverse=True)
    # The lowest-surplus proposer (highest team id, per `_fake_evaluate_
    # trade`'s `100 - team_id` construction) is the one the cap drops --
    # proves the KEPT set is the top-N, not merely internally sorted.
    kept_from_team_ids = {o.from_team_id for o in result.offers}
    assert n_proposers not in kept_from_team_ids
    assert 1 in kept_from_team_ids


def test_run_trade_window_offer_path_writes_no_head_to_head_delta(monkeypatch):
    """Decision P5-58 as corrected by P5-81 -- an AI-to-human offer writes
    no head-to-head delta at creation (nothing enters `_H2HAccumulator`
    for a diverted pair, because it is never appended to `mutual` and
    `_run_negotiation` -- the only caller that writes to the accumulator
    via `_record_negotiation_in_h2h` -- is never invoked on this path).
    Seeded with a real pre-existing row for the (1, 2) pair so a bug that
    accidentally wrote to it would show up as a changed value, not merely
    a spuriously-absent one."""
    from dba2.domain.head_to_head import HeadToHead

    snapshot, ctx, *_ = _human_offer_fixture(monkeypatch)
    seed_row = HeadToHead(
        league_id=_LEAGUE_ID, team_id=1, opponent_id=2,
        opened=5.0, received=0.0, accepts_by_opponent=2.0, rejects_by_opponent=3.0,
        accepts_by_self=0.0, rejects_by_self=0.0, counters_given=0.0, counters_received=0.0,
        executions=1.0, refused_illegal=0.0, far_reject_streak=0.0, gap_by_opponent_sum=0.0,
        last_gap_by_opponent=None, last_season_interacted=_SEASON - 1, updated_season=_SEASON - 1,
    )
    ctx = dataclasses.replace(
        ctx, self_managed_team_ids=frozenset({2}), head_to_head={(1, 2): seed_row}
    )

    result = run_trade_window(snapshot, ctx)

    assert len(result.offers) == 1
    written_row = result.head_to_head[(1, 2)]
    assert written_row.opened == seed_row.opened
    assert written_row.executions == seed_row.executions
    assert written_row.accepts_by_opponent == seed_row.accepts_by_opponent
    assert written_row.rejects_by_opponent == seed_row.rejects_by_opponent
    # Nit fix (round-7 critic pass): the forward direction being inert is
    # not proof nothing was written -- a bug that wrote a fresh REVERSE-
    # direction row (the human "responding" to itself, or any other
    # accidental (2, 1) write) would pass every assertion above. `_H2H
    # Accumulator` only ever creates a fresh zero row for a pair its own
    # window actually touches (`opened`), and (2, 1) is never touched by a
    # diverted candidate (team 2 never proposes -- it is the partner, not
    # the proposer), so it must be entirely absent from the report, not
    # merely zero.
    assert (2, 1) not in result.head_to_head


# ---------------------------------------------------------------------------
# run_trade_window -- B2 regression (critic round 17): the post-execution
# legality re-check must track each team's room as trades execute within
# one pass, not the pre-window figure.
# ---------------------------------------------------------------------------


def _filler_roster_covering_next_season(
    team_id: int, count: int, *, avoid_position: Position, base_id: int, salary: int = 3_000_000
):
    """Like `_filler_roster` above, except every contract is a 2-year deal
    (`_SEASON`, `_SEASON + 1`) so `_check_legality`'s real filler math
    (which counts contracts covering `next_season`, not `len(roster)`) sees
    the same contracted count before and after a 1-for-1 swap -- needed for
    a test that leaves `_check_legality` real (B2's own regression), unlike
    every other trade-window test in this file, which monkeypatches it
    away and never needs this distinction."""
    positions = [p for p in Position if p is not avoid_position]
    roster = []
    for i in range(count):
        roster.append(
            make_roster_player(
                base_id + i, age=26 + (i % 4), season=_SEASON, overall_cached=64 + (i % 8),
                team_id=team_id, primary_position=positions[i % len(positions)],
                peak_age_start=27, peak_age_end=31,
                contract=make_contract(
                    base_id + i, team_id=team_id, start_season=_SEASON,
                    salaries=(salary, salary),
                ),
            )
        )
    return tuple(roster)


def test_run_trade_window_catches_an_apron_breach_from_two_sequential_trades_within_one_pass(
    monkeypatch,
):
    """B2 regression, critic's exact repro shape. Team 1 starts
    $4,000,001 below `apron_1`; two sequential, individually-legal-at-
    their-own-moment trades each add $4,000,000 net salary to team 1 --
    the first lands team 1 one dollar under `apron_1` (still legal), the
    second would put team 1 $3,999,999 OVER `apron_1`. Pre-fix, the
    post-execution legality re-check read `ctx.room_by_team[1]` --
    team 1's ORIGINAL, pre-window room -- for both checks, so the second
    trade's `used_pre`/`used_post` never reflected the first trade's own
    effect and it was (wrongly) ruled legal. `evaluate_trade` is
    monkeypatched to force "accept" (this test is about the legality gate
    composing correctly across a pass, not about value-formula
    correctness, which `test_trade.py` owns) -- `_check_legality` is left
    real."""
    apron_1 = CAP_SETTINGS.apron_1
    used_pre_initial = apron_1 - 4_000_001
    room_1_initial = CAP_SETTINGS.salary_cap - used_pre_initial

    a_out1 = make_roster_player(
        5001, age=27, season=_SEASON, overall_cached=70, team_id=1,
        primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            5001, team_id=1, start_season=_SEASON, salaries=(16_000_000, 16_000_000),
        ),
    )
    a_out2 = make_roster_player(
        5002, age=27, season=_SEASON, overall_cached=70, team_id=1,
        primary_position=Position.SG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            5002, team_id=1, start_season=_SEASON, salaries=(16_000_000, 16_000_000),
        ),
    )
    b_in1 = make_roster_player(
        6001, age=27, season=_SEASON, overall_cached=70, team_id=2,
        primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            6001, team_id=2, start_season=_SEASON, salaries=(20_000_000, 20_000_000),
        ),
    )
    c_in2 = make_roster_player(
        7001, age=27, season=_SEASON, overall_cached=70, team_id=3,
        primary_position=Position.SG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            7001, team_id=3, start_season=_SEASON, salaries=(20_000_000, 20_000_000),
        ),
    )

    filler_a = _filler_roster_covering_next_season(1, 13, avoid_position=Position.C, base_id=11_000)
    filler_b = _filler_roster_covering_next_season(2, 13, avoid_position=Position.C, base_id=12_000)
    filler_c = _filler_roster_covering_next_season(3, 13, avoid_position=Position.C, base_id=13_000)

    team_1 = build_team_snapshot(make_team(1), filler_a + (a_out1, a_out2), (), CAP_SETTINGS)
    team_2 = build_team_snapshot(make_team(2), filler_b + (b_in1,), (), CAP_SETTINGS)
    team_3 = build_team_snapshot(make_team(3), filler_c + (c_in2,), (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_1, team_2, team_3))

    ctx = dataclasses.replace(
        _make_ctx(snapshot),
        room_by_team={1: room_1_initial, 2: 200_000_000, 3: 200_000_000},
    )

    trade1 = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(b_in1,)),
        outgoing_a=AssetBundle(players=(a_out1,)),
    )
    trade2 = CandidatePackage(
        team_a_id=1, team_b_id=3,
        incoming_a=AssetBundle(players=(c_in2,)),
        outgoing_a=AssetBundle(players=(a_out2,)),
    )
    fixed_candidates = {(1, 2): (trade1,), (1, 3): (trade2,)}

    def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
        return fixed_candidates.get((proposer_id, partner_id), ())

    monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)
    # `_check_legality` is deliberately left real -- it is exactly what B2 fixes.

    result = run_trade_window(snapshot, ctx)

    assert len(result.executions) == 1, (
        "only the first trade should execute -- the second must be caught by "
        "apron_1 once team 1's room reflects the first trade's own effect"
    )
    assert result.executions[0].team_b_id == 2

    # Confirm directly, via the real formula, that BOTH trades executing
    # would have breached apron_1 by critic's exact reported figure -- the
    # fix's job is to have caught this, not merely to have executed fewer
    # trades for some other reason.
    used_pre_both = used_pre_initial + 4_000_000 + 4_000_000
    assert used_pre_both - apron_1 == 3_999_999


# ---------------------------------------------------------------------------
# run_trade_window -- Nit 1 regression (critic round 18): the room write-
# back must include the same `minimum_filler` delta `_check_legality`'s
# own `used_post` computation applies internally.
# ---------------------------------------------------------------------------


def test_run_trade_window_room_writeback_matches_check_legalitys_filler_math(monkeypatch):
    """Critic's exact repro shape: a legal 3-for-1 trade drops team 1 from
    15 to 13 contracted (next-season-covering) players, crossing `roster_
    min` (14) -- `_check_legality`'s own `used_post` formula (`used_pre -
    pre_filler + post_filler - outgoing_salary + incoming_salary`) folds in
    a `minimum_filler` adjustment the raw salary delta alone does not.
    Pre-fix, `room_remaining`'s write-back used only `outgoing_salary -
    incoming_salary`, leaving it exactly one `min_salary` ($1,272,870) too
    high versus what `_check_legality` itself just modeled for this same
    trade -- confirmed here by re-deriving `_check_legality`'s own `used_
    post`-implied post-trade room independently and comparing."""
    team_1_roster = _filler_roster_covering_next_season(
        1, 15, avoid_position=Position.C, base_id=21_000
    )
    team_2_roster = _filler_roster_covering_next_season(
        2, 12, avoid_position=Position.C, base_id=22_000
    )
    outgoing_a = team_1_roster[:3]
    incoming_a = (team_2_roster[0],)

    team_1 = build_team_snapshot(make_team(1), team_1_roster, (), CAP_SETTINGS)
    team_2 = build_team_snapshot(make_team(2), team_2_roster, (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_1, team_2))

    room_1_initial = 20_000_000
    ctx = dataclasses.replace(
        _make_ctx(snapshot),
        room_by_team={1: room_1_initial, 2: 200_000_000},
    )
    next_season = ctx.next_season

    candidate = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=incoming_a),
        outgoing_a=AssetBundle(players=outgoing_a),
    )

    def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
        return (candidate,) if (proposer_id, partner_id) == (1, 2) else ()

    monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)
    # `_check_legality` is left real -- both the legality gate this test
    # exercises AND the independent `used_post` figure below must come from
    # the real formula, not a stand-in.

    result = run_trade_window(snapshot, ctx)

    assert len(result.executions) == 1, "the trade is legal and must execute"

    outgoing_salary = sum(
        gm_service.trade_decisions.salary_next(rp, next_season) for rp in outgoing_a
    )
    incoming_salary = sum(
        gm_service.trade_decisions.salary_next(rp, next_season) for rp in incoming_a
    )
    settings = ctx.next_season_cap_settings
    used_pre = settings.salary_cap - room_1_initial
    pre_contracted = sum(
        1 for rp in team_1_roster
        if gm_service.trade_decisions._contract_covers_season(rp, next_season)
    )
    outgoing_contracted = sum(
        1 for rp in outgoing_a
        if gm_service.trade_decisions._contract_covers_season(rp, next_season)
    )
    incoming_contracted = sum(
        1 for rp in incoming_a
        if gm_service.trade_decisions._contract_covers_season(rp, next_season)
    )
    post_contracted = pre_contracted - outgoing_contracted + incoming_contracted
    pre_filler = max(0, settings.roster_min - pre_contracted) * settings.min_salary
    post_filler = max(0, settings.roster_min - post_contracted) * settings.min_salary
    used_post = used_pre - pre_filler + post_filler - outgoing_salary + incoming_salary
    check_legality_implied_room_post = settings.salary_cap - used_post

    # This scenario must actually cross roster_min for the fix to matter.
    assert pre_filler != post_filler

    gm_service_tracked_delta = gm_service._filler_delta(
        team_1_roster, list(outgoing_a), list(incoming_a), next_season, settings
    )
    gm_service_tracked_room_post = (
        room_1_initial + (outgoing_salary - incoming_salary) + gm_service_tracked_delta
    )
    assert gm_service_tracked_room_post == check_legality_implied_room_post


def test_filler_delta_mutation_reproduces_the_stale_room_figure():
    """Mutation test: the raw salary delta alone (no `_filler_delta`
    adjustment) must be off by exactly one `min_salary` versus `_check_
    legality`'s own implied post-trade room for the same crossing-roster_
    min scenario -- proving the test above actually depends on the Nit 1
    fix, not on some other property of the scenario."""
    team_1_roster = _filler_roster_covering_next_season(
        1, 15, avoid_position=Position.C, base_id=23_000
    )
    # Exactly one player out (15 -> 14 contracted) crosses `roster_min`
    # (14) by exactly one slot, so the filler delta below is exactly one
    # `min_salary` -- isolating the fix's effect to a single, checkable unit.
    outgoing_a = team_1_roster[:2]
    next_season = _SEASON + 1
    settings = dataclasses.replace(CAP_SETTINGS, season=CAP_SETTINGS.season + 1)
    outgoing_salary = sum(
        gm_service.trade_decisions.salary_next(rp, next_season) for rp in outgoing_a
    )
    room_before = 20_000_000

    raw_delta_only = room_before + outgoing_salary  # no incoming, no filler_delta
    filler_delta = gm_service._filler_delta(
        team_1_roster, list(outgoing_a), [], next_season, settings
    )
    fixed_room_post = room_before + outgoing_salary + filler_delta

    assert raw_delta_only - fixed_room_post == settings.min_salary


# ---------------------------------------------------------------------------
# run_trade_window -- MAX_TRADES_PER_WINDOW actually binds (Nit 9)
# ---------------------------------------------------------------------------


def test_run_trade_window_window_wide_cap_actually_binds_with_more_than_30_candidates(monkeypatch):
    """Nit 9 (critic round 17): the existing per-team-cap test only ever
    generates 4 candidates, so its own `assert len(...) <= MAX_TRADES_PER_
    WINDOW` (30) was trivially true and never exercised the window-wide cap
    at all. 31 fully disjoint team pairs (62 teams, one asset-disjoint,
    unconditionally-accepted, unconditionally-legal 1-for-1 swap per pair,
    no per-team cap anywhere near binding since every team appears in
    exactly one trade) -- `MAX_TRADES_PER_WINDOW` is the only thing that
    can still cap this at 30."""
    n_pairs = 31
    team_ids = tuple(range(1, 2 * n_pairs + 1))
    teams = tuple(
        _build_team_snapshot(tid, avoid_position=Position.PG, base_id=1000 * tid, roster_size=1)
        for tid in team_ids
    )
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, teams)
    ctx = _make_ctx(snapshot)

    fixed_candidates: dict[tuple[int, int], tuple[CandidatePackage, ...]] = {}
    for k in range(n_pairs):
        a_id, b_id = 2 * k + 1, 2 * k + 2
        a_player = snapshot.team_by_id(a_id).roster[0]
        b_player = snapshot.team_by_id(b_id).roster[0]
        fixed_candidates[(a_id, b_id)] = (
            CandidatePackage(
                team_a_id=a_id, team_b_id=b_id,
                incoming_a=AssetBundle(players=(b_player,)),
                outgoing_a=AssetBundle(players=(a_player,)),
            ),
        )

    def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
        return fixed_candidates.get((proposer_id, partner_id), ())

    monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)
    monkeypatch.setattr(gm_service.trade_decisions, "_check_legality", _always_legal)

    result = run_trade_window(snapshot, ctx)

    assert result.packages_evaluated == n_pairs
    assert len(result.executions) == MAX_TRADES_PER_WINDOW
    assert len(result.executions) < n_pairs, "the cap must actually have turned away a candidate"


# ---------------------------------------------------------------------------
# run_trade_window -- Phase 3 §11 step 7's negotiation loop
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_run_trade_window_negotiation_activity_is_real_and_the_census_is_a_true_partition():
    """On a real, unmonkeypatched 30-team corpus (`_build_counter_corpus`'s
    own snapshot/ctx, reused here rather than re-generating a second
    league), the negotiation loop must show real activity -- at least one
    counter opportunity opens and at least one is admitted -- and every
    scalar Decision P3-12/§11 step 7 adds must be internally consistent:
    the five-cell outcome census sums to exactly `packages_evaluated`
    (round-0-only, a true partition), `evaluations_performed` exceeds the
    Phase-2 baseline of `2 * packages_evaluated` (proving at least one
    extra `evaluate_trade` call happened), `negotiation_depth_violations`
    is the hard-invariant `0`, and `EXPIRED:stalled` stays near zero
    (§4.2's own pre-registered expectation -- a large count is a
    constructor defect, not a tuning signal)."""
    snapshot, ctx, _cache, _pick_slot_ranks, _give_less = _build_counter_corpus()
    result = run_trade_window(snapshot, ctx)

    assert result.counter_opportunities > 0
    assert result.counters_admitted > 0
    assert result.counters_admitted <= result.counter_opportunities
    assert result.evaluations_performed > 2 * result.packages_evaluated
    assert result.negotiation_depth_violations == 0
    assert result.expired_stalled_count < 0.01 * result.packages_evaluated, (
        "a large EXPIRED:stalled count is a build_counter constructor defect, per §4.2's own "
        "pre-registered expectation -- not a tuning signal"
    )
    assert sum(result.outcome_census.values()) == result.packages_evaluated
    assert set(result.outcome_census) <= {
        "illegal", "both_reject_legal", "A_reject_B_accept", "A_accept_B_reject", "both_accept",
    }
    assert result.counter_closed_executions <= len(result.executions)
    # `records` is one (record_a, record_b) pair per TURN now, not per
    # package (module docstring's own updated claim) -- turns_evaluated
    # counts turns, so the pairing invariant is exactly 2x that, never
    # 2x packages_evaluated once any negotiation counters.
    assert len(result.records) == 2 * result.turns_evaluated
    assert result.turns_evaluated >= result.packages_evaluated


@pytest.mark.slow
def test_run_trade_window_max_negotiation_rounds_zero_never_calls_build_counter():
    """Decision P3-8's own load-bearing property: `max_negotiation_rounds=0`
    must make `COUNTER` structurally unreachable at round 0 (`respond()`'s
    `rounds_remaining <= 0` branch fires before the axis check), so `build_
    counter` must never be called at all. Monkeypatches `build_counter` to
    raise if invoked -- a real, mechanical proof rather than an inference
    from the digest test's own byte-identical reading."""
    snapshot, ctx, _cache, _pick_slot_ranks, _give_less = _build_counter_corpus()

    def _explode(*args, **kwargs):
        raise AssertionError("build_counter must never be called at max_negotiation_rounds=0")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gm_service, "build_counter", _explode)
        result = run_trade_window(snapshot, ctx, max_negotiation_rounds=0)

    assert result.counter_opportunities == 0
    assert result.counters_admitted == 0
    assert result.evaluations_performed == 2 * result.packages_evaluated
    assert result.turns_evaluated == result.packages_evaluated


@pytest.mark.slow
def test_run_trade_window_a_negotiation_that_never_concedes_expires_stalled_not_forever():
    """Decision P3-9's oscillation guard, forced deterministically: monkey-
    patches `build_counter` to always return the SAME package/record it was
    handed (zero concession, `gap` unchanged turn over turn) whenever a
    real seed package opens a counter. `is_stalled` must fire on the second
    such turn (the first has no `previous_actor` to compare against) and
    the negotiation must terminate `EXPIRED:stalled` rather than looping
    until `MAX_NEGOTIATION_ROUNDS` -- proving the loop cannot spin forever
    on a constructor that stops making real progress."""
    snapshot, ctx, _cache, _pick_slot_ranks, give_less = _build_counter_corpus()
    assert give_less, "corpus must contain at least one give-less-cell package to drive this"

    def _no_op_counter(package, record_a, record_b, axis, **kwargs):
        return package, record_a

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gm_service, "build_counter", _no_op_counter)
        result = run_trade_window(snapshot, ctx)

    # At least the fixture's own selected package must have stalled --
    # its round-0 cell is a real give-less opportunity and the patched
    # constructor never concedes, so `is_stalled` must fire by round 2
    # at the latest (round 1 has no previous same-actor turn to compare).
    assert result.expired_stalled_count >= 1
    assert result.negotiation_depth_violations == 0


# ---------------------------------------------------------------------------
# propose_packages -- variant 2 / unresolved future-first-round picks (Nit 2)
# ---------------------------------------------------------------------------


def test_propose_packages_variant2_carries_the_proposers_highest_value_future_first_pick():
    """Nit 2 (critic round 17): the whole variant-2/pick branch (`_highest_
    value_future_first_round_pick`, `ValuationCache.pick_price`, the
    `outgoing_a.picks` plumbing) had zero test coverage -- every existing
    fixture team in this file carries an empty `picks` tuple. Team 10 (the
    proposer) holds five draft picks, only one of which is eligible
    (`round == 1`, `season > ctx.season`, not conveyed): a conveyed future
    first (excluded), a future second (excluded, wrong round), a past
    first (excluded, not future), one future first originating from the
    league's WEAKEST team (high projected value -- a worse team's pick
    projects to an early, valuable slot), and one future first originating
    from the STRONGEST team (low projected value). `_highest_value_future_
    first_round_pick` must select the weak team's pick over the strong
    team's, not merely "a pick" -- and `propose_packages` must carry
    exactly that pick into its variant-2 candidates."""
    weak_team = build_team_snapshot(
        make_team(1),
        (make_roster_player(
            70_001, age=26, season=_SEASON, overall_cached=55, primary_position=Position.C,
            team_id=1, peak_age_start=27, peak_age_end=31,
        ),),
        (), CAP_SETTINGS,
    )
    strong_team = build_team_snapshot(
        make_team(2),
        (make_roster_player(
            70_002, age=26, season=_SEASON, overall_cached=95, primary_position=Position.C,
            team_id=2, peak_age_start=27, peak_age_end=31,
        ),),
        (), CAP_SETTINGS,
    )

    proposer_picks = (
        _make_pick(  # eligible, high value (originates from the weak team)
            9001, season=_SEASON + 2, original_team_id=1, current_team_id=10, pick_number=None,
        ),
        _make_pick(  # eligible, low value (originates from the strong team)
            9002, season=_SEASON + 2, original_team_id=2, current_team_id=10, pick_number=None,
        ),
        _make_pick(  # excluded -- already conveyed
            9003, season=_SEASON + 2, original_team_id=1, current_team_id=10, pick_number=None,
            conveyed=True,
        ),
        _make_pick(  # excluded -- round 2, not round 1
            9004, season=_SEASON + 2, original_team_id=1, current_team_id=10, pick_number=None,
            round_=2,
        ),
        _make_pick(  # excluded -- not a FUTURE pick (season == ctx.season)
            9005, season=_SEASON, original_team_id=1, current_team_id=10, pick_number=None,
        ),
    )
    proposer_roster = _filler_roster(10, 12, avoid_position=Position.C, base_id=10_000)
    proposer = build_team_snapshot(make_team(10), proposer_roster, proposer_picks, CAP_SETTINGS)

    partner_roster = _filler_roster(20, 12, avoid_position=Position.C, base_id=20_000)
    partner = build_team_snapshot(make_team(20), partner_roster, (), CAP_SETTINGS)

    snapshot = build_league_snapshot(
        _LEAGUE_ID, _SEASON, (weak_team, strong_team, proposer, partner)
    )
    ctx = _make_ctx(snapshot)
    cache = ValuationCache()

    top_pick = gm_service._highest_value_future_first_round_pick(proposer, ctx, cache, snapshot)
    assert top_pick is not None
    assert top_pick.id == 9001, "the weak-team-originated pick must price above the strong team's"

    candidates = propose_packages(10, 20, snapshot, ctx, cache)
    assert candidates  # sanity: this pair actually generates candidates
    variant2_candidates = [c for c in candidates if c.outgoing_a.picks]
    assert variant2_candidates, "at least one candidate must carry the proposer's top pick"
    for candidate in variant2_candidates:
        assert candidate.outgoing_a.picks == (top_pick,)


# ---------------------------------------------------------------------------
# Decision P2-39 (architect round 21 §4.7) -- `room_for_signings_by_team`,
# `run_free_agency`'s `room_remaining` seed. Tests 6 (harness agreement) and
# 7 (multi-season M1/M2 hard invariants) live in `tests/unit/tools/gm_
# harness/test_season.py` -- they need a real generated league, which this
# module's hand-built fixtures deliberately never build.
# ---------------------------------------------------------------------------


def test_room_remaining_seed_falls_back_to_room_by_team_when_room_for_signings_is_none():
    """§4.7 test 1, part (a). `room_for_signings_by_team=None` (the
    default -- no caller has supplied a hold-aware figure) must seed `room_
    remaining` from `ctx.room_by_team`, producing the identical run a caller
    who explicitly passes a mapping equal to `room_by_team` would get.
    *Mutation: delete the `is None` fallback entirely (e.g. always read
    `ctx.room_for_signings_by_team`) -- `dict(None)` raises `TypeError`,
    so this test fails loudly rather than silently.*"""
    snapshot = _make_small_league(n_teams=1)
    team_id = snapshot.teams[0].id
    room = 30_000_000
    star_1 = make_roster_player(
        9201, age=27, season=_SEASON, overall_cached=92, primary_position=Position.C,
        team_id=None, peak_age_start=27, peak_age_end=31,
    )
    star_2 = make_roster_player(
        9202, age=27, season=_SEASON, overall_cached=92, primary_position=Position.PF,
        team_id=None, peak_age_start=27, peak_age_end=31,
    )

    ctx_none = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=room)
    ctx_equal = _make_ctx(
        snapshot, phase=Phase.FREE_AGENCY, room=room,
        room_for_signings_by_team={team_id: room},
    )

    result_none = run_free_agency(snapshot, ctx_none, pool=(star_1, star_2), pending_offers=())
    result_equal = run_free_agency(snapshot, ctx_equal, pool=(star_1, star_2), pending_offers=())

    salaries_none = sorted((s.player_id, s.annual_salary) for s in result_none.signings)
    salaries_equal = sorted((s.player_id, s.annual_salary) for s in result_equal.signings)
    assert salaries_none == salaries_equal, (
        "None must behave exactly like a caller-supplied mapping equal to room_by_team -- "
        f"got {salaries_none} vs {salaries_equal}"
    )
    assert salaries_none == [
        (star_1.id, 30_000_000), (star_2.id, CAP_SETTINGS.mle_amount),
    ], "sanity: the fixture must still reproduce the pre-P2-39 CAP_SPACE/MLE split"


def test_room_remaining_seed_treats_an_empty_room_for_signings_mapping_as_real_not_falsy():
    """§4.7 test 1, part (b) -- the `or`-fallback trap named in architect
    round 21 §4.3 requirement 1. An empty mapping is a legitimate
    (degenerate) input, distinct from `None` -- e.g. a genuinely incomplete
    per-team population -- and must be honored as-is, never silently
    replaced. On a league with real teams, an empty `room_for_signings_by_
    team` is missing every team's entry, so the correctly-seeded `room_
    remaining` (built from that same empty mapping) raises `KeyError` the
    first time any downstream call indexes it by `team_id` -- proving the
    empty mapping actually reached `room_remaining` rather than being
    swapped out. *Mutation: seed via `ctx.room_for_signings_by_team or ctx.
    room_by_team` -- an empty dict is falsy, so the mutant silently reuses
    the fully-populated `room_by_team` instead, the run completes with a
    normal $60M-priced signing, and this test's `pytest.raises` fails to
    fire.*"""
    snapshot = _make_small_league(n_teams=1)
    star = make_roster_player(
        9203, age=27, season=_SEASON, overall_cached=92, primary_position=Position.C,
        team_id=None, peak_age_start=27, peak_age_end=31,
    )
    ctx = _make_ctx(
        snapshot, phase=Phase.FREE_AGENCY, room=60_000_000, room_for_signings_by_team={},
    )

    with pytest.raises(KeyError):
        run_free_agency(snapshot, ctx, pool=(star,), pending_offers=())


def test_room_for_signings_by_team_strictly_lowers_signings_never_raises_them():
    """§4.7 test 2. On a fixture where `room_for_signings_by_team` is
    strictly smaller than `room_by_team`, at least one signing's `annual_
    salary` must be strictly lower and none may be higher. *Mutation:
    revert the seed to `dict(ctx.room_by_team)` -- must fail (both runs
    become identical, breaching the "strictly lower" half)."""
    snapshot = _make_small_league(n_teams=1)
    team_id = snapshot.teams[0].id
    star_1 = make_roster_player(
        9204, age=27, season=_SEASON, overall_cached=92, primary_position=Position.C,
        team_id=None, peak_age_start=27, peak_age_end=31,
    )
    star_2 = make_roster_player(
        9205, age=27, season=_SEASON, overall_cached=92, primary_position=Position.PF,
        team_id=None, peak_age_start=27, peak_age_end=31,
    )

    ctx_generous = _make_ctx(snapshot, phase=Phase.FREE_AGENCY, room=30_000_000)
    ctx_constrained = _make_ctx(
        snapshot, phase=Phase.FREE_AGENCY, room=30_000_000,
        room_for_signings_by_team={team_id: 5_000_000},
    )

    generous = run_free_agency(snapshot, ctx_generous, pool=(star_1, star_2), pending_offers=())
    constrained = run_free_agency(
        snapshot, ctx_constrained, pool=(star_1, star_2), pending_offers=()
    )

    generous_by_player = {s.player_id: s.annual_salary for s in generous.signings}
    constrained_by_player = {s.player_id: s.annual_salary for s in constrained.signings}
    assert set(generous_by_player) == set(constrained_by_player) == {star_1.id, star_2.id}

    assert any(
        constrained_by_player[pid] < generous_by_player[pid] for pid in generous_by_player
    ), "the constrained basis must strictly lower at least one signing's salary"
    assert all(
        constrained_by_player[pid] <= generous_by_player[pid] for pid in generous_by_player
    ), "the constrained basis must never raise any signing's salary"
    # Concrete numbers, not just an inequality -- $30M room prices the first
    # star at the full CAP_SPACE room and the second at the MLE; $5M room
    # (below even the MLE ladder) prices the first at MLE and the second at
    # the bi-annual, both strictly lower.
    assert sorted(generous_by_player.values()) == [CAP_SETTINGS.mle_amount, 30_000_000]
    assert sorted(constrained_by_player.values()) == [
        CAP_SETTINGS.bi_annual_amount, CAP_SETTINGS.mle_amount,
    ]


def test_apron_room_remaining_stays_hold_free_regardless_of_room_for_signings_by_team():
    """§4.7 test 3 -- **the most important test in this slice** (architect
    round 21 §4.7/§4.8's own framing): the structural M2 guard. `apron_
    room_remaining` (and therefore the Bird ceiling it clamps, and the hard
    `apron_2` gate every signing passes through) must stay seeded from
    `ctx.apron1_room_by_team` no matter what `ctx.room_for_signings_by_team`
    holds -- including a wildly different, deeply negative value that would
    crush the Bird ceiling to nothing if it ever leaked into the apron
    ledger. *Mutation: seed `apron_room_remaining` from `ctx.room_for_
    signings_by_team` (or from `ctx.room_for_signings_by_team` when it is
    not `None`) instead of `ctx.apron1_room_by_team` -- the incumbent's
    Bird-priced salary collapses (or the signing is blocked outright by the
    now-negative apron_2 clamp), and this test's exact-dollar assertion
    fails.*

    Mirrors `test_run_resign_window_and_round_zero_price_the_same_
    incumbent_the_same_way`'s own fixture (a 5-years-pro incumbent, 25%
    tier ceiling of $38,661,750, comfortably inside a generous $80M `apron1_
    room`), with `room_for_signings_by_team` pinned to -$900M -- an order
    of magnitude below both `room` and `apron1_room`, so any leak into the
    apron ledger is unmissable."""
    team = make_team(1)
    incumbent = make_roster_player(
        9600, age=31, season=_SEASON, overall_cached=90, primary_position=Position.C,
        team_id=team.id, peak_age_start=27, peak_age_end=31,
        contract=make_contract(9600, team_id=team.id, start_season=_SEASON, salaries=(20_000_000,)),
    )
    roster = (incumbent,) + _filler_roster(team.id, 11, avoid_position=Position.C, base_id=1000)
    team_snapshot = build_team_snapshot(team, roster, (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_snapshot,))
    ctx = _make_ctx(
        snapshot, phase=Phase.FREE_AGENCY, room=-40_000_000, apron1_room=80_000_000,
        room_for_signings_by_team={team.id: -900_000_000},
    )
    expected_salary = round(CAP_SETTINGS.max_pct_0_6 / 100.0 * CAP_SETTINGS.salary_cap)
    assert expected_salary == 38_661_750

    pending_offers = run_resign_window(snapshot, ctx, expiring_by_team={team.id: (incumbent.id,)})
    result = run_free_agency(snapshot, ctx, pool=(incumbent,), pending_offers=pending_offers)

    signing = next(s for s in result.signings if s.player_id == incumbent.id)
    assert signing.signing_method == gm_service.SigningMethod.BIRD
    assert signing.annual_salary == expected_salary, (
        "the incumbent's Bird-priced salary must be set by the hold-free apron1_room_by_team "
        f"basis (expected {expected_salary}), not perturbed by room_for_signings_by_team's "
        f"-$900M -- got {signing.annual_salary}"
    )


def test_run_resign_window_is_unaffected_by_room_for_signings_by_team():
    """§4.7 test 4. `run_resign_window` must read `ctx.room_by_team`
    exactly as before, whether or not `ctx.room_for_signings_by_team` is
    populated. Fixture pins `room` (50M) strictly above the incumbent's
    Bird tier ceiling (~38.66M) so the *ladder*, not the Bird bucket, is the
    binding term -- the scenario architect round 21 §4.3's one-line diff
    could actually perturb if it were mistakenly threaded here too.
    *Mutation: point this function's own `room = ctx.room_by_team[team_id]`
    at `ctx.room_for_signings_by_team[team_id]` instead -- the populated-
    ctx run collapses from $50M to the $38,661,750 Bird ceiling while the
    unpopulated run stays at $50M, breaking the identity assertion below.*"""
    team = make_team(1)
    incumbent = make_roster_player(
        9601, age=31, season=_SEASON, overall_cached=92, primary_position=Position.C,
        team_id=team.id, peak_age_start=27, peak_age_end=31,
        contract=make_contract(9601, team_id=team.id, start_season=_SEASON, salaries=(20_000_000,)),
    )
    roster = (incumbent,) + _filler_roster(team.id, 11, avoid_position=Position.C, base_id=2000)
    team_snapshot = build_team_snapshot(team, roster, (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_snapshot,))

    ctx_without = _make_ctx(
        snapshot, phase=Phase.FREE_AGENCY, room=50_000_000, apron1_room=100_000_000,
    )
    ctx_with = _make_ctx(
        snapshot, phase=Phase.FREE_AGENCY, room=50_000_000, apron1_room=100_000_000,
        room_for_signings_by_team={team.id: -900_000_000},
    )

    offers_without = run_resign_window(
        snapshot, ctx_without, expiring_by_team={team.id: (incumbent.id,)}
    )
    offers_with = run_resign_window(
        snapshot, ctx_with, expiring_by_team={team.id: (incumbent.id,)}
    )

    assert len(offers_without) == 1
    assert len(offers_with) == 1
    assert offers_without[0].annual_salary == 50_000_000, (
        "sanity: the room-based ladder (not the ~$38.66M Bird ceiling) must be binding here"
    )
    assert offers_with[0].annual_salary == offers_without[0].annual_salary, (
        "run_resign_window must be byte-identical with and without room_for_signings_by_team "
        f"populated -- got {offers_with[0].annual_salary} vs {offers_without[0].annual_salary}"
    )


def test_trade_path_is_unaffected_by_room_for_signings_by_team(monkeypatch):
    """§4.7 test 5. On a fixture where the two bases sharply differ, `run_
    trade_window`'s real `_check_legality`/`cap_delta_bonus` verdicts and
    terms must be identical whether or not `ctx.room_for_signings_by_team`
    is populated -- the whole trade-legality/valuation path reads only
    `ctx.room_by_team` (architect round 21 §4.4). Reuses `test_run_trade_
    window_executes_a_genuine_mutually_beneficial_trade_under_real_
    formulas`'s own fixture verbatim. *Mutation: point `_check_legality`'s
    `next_season_room_dollars` at `ctx.room_for_signings_by_team` instead
    of `ctx.room_by_team` -- the trade-path run's `cap_delta_bonus`
    contribution term diverges from the no-field-populated run's, failing
    the equality assertion below.*"""
    team_a = make_team(1)
    team_b = make_team(2)

    star = make_roster_player(
        100, age=33, season=_SEASON, overall_cached=90, primary_position=Position.SF,
        team_id=2, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            100, team_id=2, start_season=_SEASON,
            salaries=(38_000_000, 40_000_000, 42_000_000),
        ),
    )
    roster_a = _filler_roster(1, 14, avoid_position=Position.SF, base_id=1000)
    roster_b = (star,) + _filler_roster(2, 14, avoid_position=Position.SF, base_id=2000)

    snapshot_a = build_team_snapshot(team_a, roster_a, (), CAP_SETTINGS)
    snapshot_b = build_team_snapshot(team_b, roster_b, (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (snapshot_a, snapshot_b))

    pick_1 = _make_pick(9001, season=_SEASON, original_team_id=1, current_team_id=1, pick_number=2)
    pick_2 = _make_pick(9002, season=_SEASON, original_team_id=1, current_team_id=1, pick_number=5)

    def _make_trade_ctx(room_for_signings_by_team):
        return dataclasses.replace(
            _make_ctx(snapshot, room_for_signings_by_team=room_for_signings_by_team),
            room_by_team={1: 40_000_000, 2: 60_000_000},
            philosophy_by_team={1: GmPhilosophy.STAR_MAXER, 2: GmPhilosophy.YOUTH_DEVELOPER},
            window_by_team={1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD},
        )

    candidate = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(star,)),
        outgoing_a=AssetBundle(picks=(pick_1, pick_2)),
    )

    def _fake_propose_packages(proposer_id, partner_id, snap, ctx_, cache):
        return (candidate,) if (proposer_id, partner_id) == (1, 2) else ()

    monkeypatch.setattr(gm_service, "propose_packages", _fake_propose_packages)

    ctx_without = _make_trade_ctx(None)
    # Sharply different from room_by_team ({1: 40M, 2: 60M}) so any leak
    # would be unmissable.
    ctx_with = _make_trade_ctx({1: -900_000_000, 2: -900_000_000})

    result_without = run_trade_window(snapshot, ctx_without)
    result_with = run_trade_window(snapshot, ctx_with)

    assert len(result_without.executions) == 1 == len(result_with.executions)
    exec_without = result_without.executions[0]
    exec_with = result_with.executions[0]
    assert exec_without.record_a.verdict == exec_with.record_a.verdict == "accept"
    assert exec_without.record_b.verdict == exec_with.record_b.verdict == "accept"

    def _cap_delta_bonus_term(record):
        return next(t.contribution for t in record.terms if t.label == "cap_delta_bonus")

    assert _cap_delta_bonus_term(exec_without.record_a) == _cap_delta_bonus_term(
        exec_with.record_a
    ), "cap_delta_bonus must not move when room_for_signings_by_team is populated"
    assert _cap_delta_bonus_term(exec_without.record_b) == _cap_delta_bonus_term(
        exec_with.record_b
    )


def test_incumbent_bird_ceiling_still_binds_when_room_for_signings_is_deeply_negative():
    """§4.7 test 8. Pins architect round 21 §4.5's derived claim: a team
    whose hold-inclusive room seed is deeply negative still prices its own
    incumbent at `bird_ceiling`, because `_effective_capacity`'s ladder is a
    `max(...)` that includes the Bird term, not a `room`-gated branch.
    *Mutation: drop `bird_ceiling` from `_effective_capacity`'s `max` --
    the incumbent collapses to the plain MLE-ladder capacity instead, and
    the exact-dollar assertion below fails.*"""
    team_id = 1
    # room_remaining seeded as if from a deeply negative room_for_
    # signings_by_team basis -- far below even the ordinary MLE/bi-annual
    # ladder, not merely "over cap."
    room_remaining = {team_id: -900_000_000}
    exceptions_used: dict[int, set[str]] = {team_id: set()}
    apron_room_remaining = {team_id: 80_000_000}

    tier_0_6 = round(CAP_SETTINGS.max_pct_0_6 / 100.0 * CAP_SETTINGS.salary_cap)
    assert tier_0_6 == 38_661_750

    capacity = gm_service._effective_capacity(
        team_id, room_remaining, exceptions_used, CAP_SETTINGS,
        incumbent_prior_salary=12_000_000, incumbent_years_pro=5,
        apron_room_remaining=apron_room_remaining,
    )
    assert capacity == tier_0_6, (
        f"a deeply negative room_remaining must not suppress the Bird ceiling -- "
        f"expected {tier_0_6}, got {capacity}"
    )


# ---------------------------------------------------------------------------
# Phase 3, §11 Stage B step 6 -- `build_counter` (docs/design/phase-3-
# inter-manager-society.md §5.1/§5.3/§11 step 6). The give-less-axis
# fixtures below scan a real, harness-generated league for packages
# meeting each named test's own structural precondition (never a hand-
# modeled `fit_value`/probe number) -- the same "real formulas over
# hand-built numbers" convention this file's own module docstring already
# establishes for `run_trade_window`'s real-formula tests. The ask-more
# axis is hand-built instead (§4.3 measures its live availability at
# 0.11% of all packages -- genuinely absent from a single 30-team scan, as
# the corpus this suite fixes on confirms directly) and isolates the
# search logic from A's real acceptance arithmetic via the same `_accept_
# everything_record` monkeypatch `run_trade_window`'s own selection tests
# already use, since A's real formula is `test_trade.py`'s job, not this
# axis's.
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _build_counter_corpus():
    """One real, deterministic 30-team league (`tools.gm_harness.league.
    generate_gm_league`, `rng_seed=1`, `players_per_team=15` -- the same
    generator/seed `test_propose_packages_bs_outgoing_bundle_never_
    carries_a_pick` already exercises) plus every ordered pair's real
    `propose_packages` output, each evaluated with the real `trade_
    decisions.evaluate_trade` on both sides. Cached for the whole test
    session (`lru_cache`, no arguments) -- the full 30x29 ordered-pair
    scan with two real `evaluate_trade` calls per candidate costs real
    wall time (~3,480 candidates), and every test below only *reads*
    this corpus, never mutates it.

    Returns `(snapshot, ctx, cache, pick_slot_ranks, give_less)` --
    `give_less` is a tuple of dicts, one per package landing in §4.2's
    give-less cell (`record_a` reject-and-legal, `record_b` accept),
    carrying every field a `build_counter` call plus each named test's own
    fixture-selection predicate needs."""
    from tools.gm_harness.league import generate_gm_league
    from tools.gm_harness.state import build_ctx, build_snapshot

    state = generate_gm_league(
        league_id=1, season=_SEASON, rng_seed=1, players_per_team=15,
    )
    snapshot = build_snapshot(state)
    ctx = build_ctx(state, phase=Phase.REGULAR_SEASON)
    cache = ValuationCache()
    pick_slot_ranks = cache.pick_slot_ranks(snapshot, ctx.tuning)
    next_season = ctx.next_season
    team_ids = [t.id for t in snapshot.teams]

    give_less = []
    for a_id in team_ids:
        for b_id in team_ids:
            if a_id == b_id:
                continue
            for cand in propose_packages(a_id, b_id, snapshot, ctx, cache):
                team_a = snapshot.team_by_id(a_id)
                team_b = snapshot.team_by_id(b_id)
                all_players = (*team_a.roster, *team_b.roster)
                fv_a = cache.fit_values(team_a, all_players, ctx)
                fv_b = cache.fit_values(team_b, all_players, ctx)
                b_players_ascending = sorted(
                    team_b.roster, key=lambda rp: (fv_b[rp.id], rp.id)
                )
                partner = gm_service._PartnerTerms(
                    team_id=b_id,
                    loyalty=gm_service.PROFILES[ctx.philosophy_by_team[b_id]].loyalty,
                    window=ctx.window_by_team[b_id],
                    room_dollars=ctx.room_by_team[b_id],
                    over_cap=ctx.room_by_team[b_id] < 0,
                    fv_by_player_id=fv_b,
                    players_ascending=tuple(b_players_ascending),
                )
                record_a = gm_service.trade_decisions.evaluate_trade(
                    team_a, cand.incoming_a, cand.outgoing_a,
                    ctx.philosophy_by_team[a_id], ctx.window_by_team[a_id],
                    league_rng_seed=ctx.league_rng_seed, season=ctx.season,
                    phase=ctx.phase, cap_settings=ctx.cap_settings,
                    next_season=next_season,
                    next_season_cap_settings=ctx.next_season_cap_settings,
                    next_season_room_dollars=ctx.room_by_team[a_id],
                    rookie_scale=ctx.rookie_scale, league_snapshot=snapshot,
                    tuning=ctx.tuning, pick_slot_ranks=pick_slot_ranks,
                )
                record_b = gm_service.trade_decisions.evaluate_trade(
                    team_b, cand.outgoing_a, cand.incoming_a,
                    ctx.philosophy_by_team[b_id], ctx.window_by_team[b_id],
                    league_rng_seed=ctx.league_rng_seed, season=ctx.season,
                    phase=ctx.phase, cap_settings=ctx.cap_settings,
                    next_season=next_season,
                    next_season_cap_settings=ctx.next_season_cap_settings,
                    next_season_room_dollars=ctx.room_by_team[b_id],
                    rookie_scale=ctx.rookie_scale, league_snapshot=snapshot,
                    tuning=ctx.tuning, pick_slot_ranks=pick_slot_ranks,
                )
                if record_a.verdict.startswith("illegal:"):
                    continue
                if record_a.verdict != "reject" or record_b.verdict != "accept":
                    continue
                give_less.append(
                    dict(
                        a_id=a_id, b_id=b_id, cand=cand, team_a=team_a,
                        partner=partner, fv_a=fv_a, record_a=record_a,
                        record_b=record_b,
                    )
                )
    return snapshot, ctx, cache, pick_slot_ranks, tuple(give_less)


def _probe(ctx, target, players, partner, a_id, *, pick=0.0):
    """A thin wrapper around the real `_probe_package` -- fixture-search
    convenience only, never a substitute for calling the real function
    inside `build_counter` itself."""
    return gm_service._probe_package(
        target, players, partner=partner, incoming_pick_value=pick,
        next_season=ctx.next_season, next_season_cap_settings=ctx.next_season_cap_settings,
        proposer_over_cap=ctx.room_by_team[a_id] < 0, tuning=ctx.tuning,
    )


def test_build_counter_give_less_emits_a_counter_on_a_construction_invariant_fixture():
    """§11 step 6's construction-invariant test (round 12's C15). Drives a
    real, pick-free, `N = 2` give-less package -- both outgoing assets
    from the value pass, with the prefix drop (dropping the last-added
    asset) independently confirmed refused by the real probe, which is
    precisely round 13 nit 4's own fixture-composition requirement stated
    as a checkable condition rather than assumed -- through the real
    `build_counter` and asserts a counter is emitted. Under the pre-
    round-12 spec ("drop the last-added asset," which leaves exactly this
    refused prefix) this test fails deterministically -- that is the
    point of writing it (§5.1's withdrawal block)."""
    snapshot, ctx, cache, pick_slot_ranks, give_less = _build_counter_corpus()
    for entry in give_less:
        cand = entry["cand"]
        outgoing_players = cand.outgoing_a.players
        if len(outgoing_players) != 2 or cand.outgoing_a.picks:
            continue
        target = cand.incoming_a.players[0]
        seed_probe = _probe(ctx, target, outgoing_players, entry["partner"], entry["a_id"])
        prefix_probe = _probe(ctx, target, (outgoing_players[0],), entry["partner"], entry["a_id"])
        if not (seed_probe.matching_floor_ok and prefix_probe.value_gap < 0.0):
            continue  # a phase-2 filler is among the two -- not this fixture's shape
        result = gm_service.build_counter(
            cand, entry["record_a"], entry["record_b"], "give_less",
            team_a=entry["team_a"], partner=entry["partner"],
            fv_a_by_player_id=entry["fv_a"], incoming_pick_value=0.0,
            snapshot=snapshot, ctx=ctx, pick_slot_ranks=pick_slot_ranks,
        )
        if result is None:
            continue  # A's own real evaluate_trade refused this particular repair
        return
    pytest.fail(
        "corpus contains no pick-free, filler-free, N=2 give-less package whose "
        "sole probe-admitted candidate A's own real evaluate_trade also accepts"
    )


def test_build_counter_give_less_makes_exactly_2n_minus_2_probes_no_pre_filter():
    """T3 (§11 step 6) -- the no-pre-filter structural test, give-less-axis
    only (the ask-more axis's own bound is data-dependent, §5.1/T3's own
    scope note). Asserts `_probe_package`'s call count inside ONE `build_
    counter` invocation equals `2^N - 2` exactly -- 2 at `N = 2`, 6 at
    `N = 3` -- proving nothing is pre-filtered before the probe. A static-
    score-as-hard-gate implementation (round 13's, withdrawn at round 14)
    would make fewer."""
    snapshot, ctx, cache, pick_slot_ranks, give_less = _build_counter_corpus()
    for n, expected in ((2, 2), (3, 6)):
        for entry in give_less:
            cand = entry["cand"]
            if len(cand.outgoing_a.players) != n:
                continue
            call_count = 0
            real_probe = gm_service._probe_package

            def _counting_probe(*args, _real=real_probe, **kwargs):
                nonlocal call_count
                call_count += 1
                return _real(*args, **kwargs)

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(gm_service, "_probe_package", _counting_probe)
                gm_service.build_counter(
                    cand, entry["record_a"], entry["record_b"], "give_less",
                    team_a=entry["team_a"], partner=entry["partner"],
                    fv_a_by_player_id=entry["fv_a"], incoming_pick_value=0.0,
                    snapshot=snapshot, ctx=ctx, pick_slot_ranks=pick_slot_ranks,
                )
            assert call_count == expected, (
                f"N={n}: expected exactly {expected} _probe_package calls, got {call_count}"
            )
            break
        else:
            pytest.fail(f"corpus contains no N={n} give-less package")


def test_build_counter_give_less_continues_past_an_illegal_evaluate_trade_stop():
    """The illegal-stop test (§5.3's legality-before-success invariant,
    Decision P3-6/C16). Forces the FIRST real `evaluate_trade` call
    `build_counter` makes (the top-ranked candidate) to `illegal:salary_
    matching` -- `score = threshold = 0.0`, so a `gap >= self_margin`-only
    stop condition would accept it immediately -- and asserts the search
    continues to a later candidate rather than stopping there."""
    snapshot, ctx, cache, pick_slot_ranks, give_less = _build_counter_corpus()
    for entry in give_less:
        cand = entry["cand"]
        outgoing_players = cand.outgoing_a.players
        if len(outgoing_players) != 3:
            continue
        target = cand.incoming_a.players[0]
        admitted = sum(
            1
            for survivors, _dropped in gm_service._give_less_candidates(outgoing_players)
            if (
                (p := _probe(ctx, target, survivors, entry["partner"], entry["a_id"]))
                and p.matching_ceiling_ok and p.matching_floor_ok and p.value_gap >= 0.0
            )
        )
        if admitted < 2:
            continue

        real_evaluate = gm_service.trade_decisions.evaluate_trade
        calls: list[DecisionRecord] = []

        def _faking_evaluate(*args, _real=real_evaluate, _calls=calls, **kwargs):
            record = _real(*args, **kwargs)
            _calls.append(record)
            if len(_calls) == 1:
                return dataclasses.replace(
                    record, verdict="illegal:salary_matching", score=0.0, threshold=0.0,
                )
            return record

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gm_service.trade_decisions, "evaluate_trade", _faking_evaluate)
            result = gm_service.build_counter(
                cand, entry["record_a"], entry["record_b"], "give_less",
                team_a=entry["team_a"], partner=entry["partner"],
                fv_a_by_player_id=entry["fv_a"], incoming_pick_value=0.0,
                snapshot=snapshot, ctx=ctx, pick_slot_ranks=pick_slot_ranks,
            )
        assert len(calls) >= 2, "the search must continue past the forced illegal record"
        if result is not None:
            assert not result[1].verdict.startswith("illegal:")
        return
    pytest.fail("corpus contains no N=3 give-less package with >=2 probe-admitted candidates")


def test_build_counter_give_less_t1_prime_pick_hatch_emission():
    """T1' (§11 step 6, round 15) -- the pick-hatch emission test. Finds a
    real, pick-bearing `N = 2` give-less package whose prefix drop (last-
    added asset dropped) is refused by the real probe WITHOUT the pick
    (`value_gap < 0`, the construction invariant) but admitted WITH it
    (§5.1's identity `value_gap_counter(P) = value_gap_constr(P) + P_in`)
    -- and asserts `build_counter` probes and emits exactly that
    candidate. Round 16's two fixture preconditions are asserted directly
    rather than assumed: (a) the target candidate really is admitted
    (`matching_floor_ok`/`matching_ceiling_ok`/`value_gap >= 0`, all
    three, not just the pick math); (b) the sibling candidate (dropping
    the first-added asset instead) is NOT also admitted, so there is
    nothing to outrank the target with at `N = 2`."""
    snapshot, ctx, cache, pick_slot_ranks, give_less = _build_counter_corpus()
    for entry in give_less:
        cand = entry["cand"]
        outgoing_players = cand.outgoing_a.players
        if len(outgoing_players) != 2 or not cand.outgoing_a.picks:
            continue
        a_id, b_id = entry["a_id"], entry["b_id"]
        target = cand.incoming_a.players[0]
        prefix, other = (outgoing_players[0],), (outgoing_players[1],)

        prefix_nopick = _probe(ctx, target, prefix, entry["partner"], a_id, pick=0.0)
        if prefix_nopick.value_gap >= 0.0:
            continue  # invariant not exercised on this candidate -- try the next

        pick = cand.outgoing_a.picks[0]
        p_in = gm_service.trade_decisions._price_pick(
            pick, ctx.philosophy_by_team[b_id], ctx.window_by_team[b_id],
            season=ctx.season, rookie_scale=ctx.rookie_scale,
            cap_settings=ctx.cap_settings, league_snapshot=snapshot,
            tuning=ctx.tuning, pick_slot_ranks=pick_slot_ranks,
        )
        prefix_pick = _probe(ctx, target, prefix, entry["partner"], a_id, pick=p_in)
        # Precondition (a) -- the target candidate is really admitted.
        if not (
            prefix_pick.matching_ceiling_ok
            and prefix_pick.matching_floor_ok
            and prefix_pick.value_gap >= 0.0
        ):
            continue
        other_pick = _probe(ctx, target, other, entry["partner"], a_id, pick=p_in)
        other_admitted = (
            other_pick.matching_ceiling_ok
            and other_pick.matching_floor_ok
            and other_pick.value_gap >= 0.0
        )
        # Precondition (b) -- nothing outranks the target at N=2.
        if other_admitted:
            continue

        result = gm_service.build_counter(
            cand, entry["record_a"], entry["record_b"], "give_less",
            team_a=entry["team_a"], partner=entry["partner"],
            fv_a_by_player_id=entry["fv_a"], incoming_pick_value=p_in,
            snapshot=snapshot, ctx=ctx, pick_slot_ranks=pick_slot_ranks,
        )
        assert result is not None, "the pick-hatch-only-feasible prefix must be emitted"
        counter, _record = result
        assert tuple(rp.id for rp in counter.outgoing_a.players) == tuple(rp.id for rp in prefix)
        # B7 -- pick invariance (§5.3, Decision P3-11) on a genuinely
        # pick-BEARING package, not the vacuous `() == ()` case every
        # other give-less pick-invariance assertion in this file runs on.
        assert cand.outgoing_a.picks, "fixture sanity: the seed's own pick tuple must be non-empty"
        assert counter.incoming_a.picks == cand.incoming_a.picks
        assert counter.outgoing_a.picks == cand.outgoing_a.picks
        return
    pytest.fail("corpus contains no pick-hatch-only-feasible N=2 give-less package")


def test_build_counter_give_less_t2_genuinely_new_candidate_emission():
    """T2 (§11 step 6) -- the genuinely-new-candidate test, the one that
    catches wrongful exclusion. Finds a real, pick-free `N = 2` give-less
    package whose FIRST-added-asset drop (a non-prefix subset, never
    probed during original construction at all) is B-feasible under the
    real probe WHILE its static `gap_B' = slack_B - sum(fv_B(dropped))`
    (the withdrawn round-13 formula: `slack_b = score_b - threshold_b` on
    the seed record, `dropped` = the first-added asset) is NEGATIVE, and
    its complementary prefix drop is refused (the construction invariant,
    guaranteed and reconfirmed directly) -- and asserts `build_counter`
    probes and emits the genuinely-new candidate. Without the `gap_B' < 0`
    filter, this fixture-selection loop can land on a candidate the
    withdrawn static gate would ALSO have admitted (both the probe and the
    static score agreeing), which makes the test pass even under a
    reintroduced round-13 static-score hard gate -- the exact defect this
    test exists to catch. Under round 13's withdrawn static-score gate
    this class of candidate could be wrongly excluded before any probe;
    under this implementation it is not, because nothing here is excluded
    except by the real probe and the real `evaluate_trade`."""
    snapshot, ctx, cache, pick_slot_ranks, give_less = _build_counter_corpus()
    for entry in give_less:
        cand = entry["cand"]
        outgoing_players = cand.outgoing_a.players
        if len(outgoing_players) != 2 or cand.outgoing_a.picks:
            continue
        a_id = entry["a_id"]
        target = cand.incoming_a.players[0]
        prefix, other = (outgoing_players[0],), (outgoing_players[1],)

        prefix_probe = _probe(ctx, target, prefix, entry["partner"], a_id)
        if prefix_probe.value_gap >= 0.0:
            continue  # fixture sanity: the prefix must be refused (the invariant)
        other_probe = _probe(ctx, target, other, entry["partner"], a_id)
        if not (
            other_probe.matching_ceiling_ok
            and other_probe.matching_floor_ok
            and other_probe.value_gap >= 0.0
        ):
            continue

        # The missing B2 filter: the withdrawn round-13 static score must
        # ALSO disagree with the real probe on this exact candidate, or a
        # reintroduced static-score hard gate would pass this test by
        # coincidence rather than being caught by it. `dropped` here is
        # the first-added asset -- the one `other`'s drop set removes.
        slack_b = entry["record_b"].score - entry["record_b"].threshold
        dropped = (outgoing_players[0],)
        d_score_b = sum(entry["partner"].fv_by_player_id[x.id] for x in dropped)
        gap_b_prime = slack_b - d_score_b
        if gap_b_prime >= 0.0:
            continue  # static score would also admit this -- not discriminating

        result = gm_service.build_counter(
            cand, entry["record_a"], entry["record_b"], "give_less",
            team_a=entry["team_a"], partner=entry["partner"],
            fv_a_by_player_id=entry["fv_a"], incoming_pick_value=0.0,
            snapshot=snapshot, ctx=ctx, pick_slot_ranks=pick_slot_ranks,
        )
        if result is None:
            continue  # A's own real evaluate_trade refused this repair -- try the next
        counter, record = result
        assert not record.verdict.startswith("illegal:")
        assert tuple(rp.id for rp in counter.outgoing_a.players) == tuple(rp.id for rp in other)
        return
    pytest.fail("corpus contains no genuinely-new-candidate (T2) N=2 give-less package")


def test_rank_give_less_candidates_never_excludes_a_negative_gap_a_prime_candidate():
    """§5.3's own most-emphasized negative invariant -- "no candidate is
    excluded from `build_counter`'s search by any arithmetic other than
    the real `_probe_package` and the real `evaluate_trade`" -- had zero
    test coverage before this one. Two mutations the critic tried (skip
    candidates with negative `gap_A'` entirely; reverse the ranking order)
    both passed the whole suite unnoticed.

    Two hand-built single-survivor candidates, both probe-admitted
    (`matching_ceiling_ok`/`matching_floor_ok`/`value_gap >= 0`, all
    real): dropping `y` gives a POSITIVE `gap_A'` (A's own valuation of
    `y` is high, so losing it helps A's own bar a lot); dropping `x`
    gives a NEGATIVE `gap_A'` (A places zero value on `x`, so losing it
    helps not at all against A's threshold). Asserts both survive into
    the ranked list -- proving the negative-`gap_A'` candidate is
    genuinely still in the search rather than silently dropped before
    ranking -- and that the positive-`gap_A'` candidate ranks strictly
    ahead of it, which a reversed-order mutation would also fail."""
    next_season = _SEASON + 1
    target = _salary_player(1, 5_000_000, team_id=2)
    x = _salary_player(101, 1_000_000, team_id=1)
    y = _salary_player(102, 1_000_000, team_id=1)
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id={target.id: 50.0, x.id: 200.0, y.id: 200.0},
        players_ascending=(target,),
    )

    # Fixture sanity -- both single-survivor candidates really are
    # probe-admitted, so the test isolates the ranking property rather
    # than the admission gate.
    for survivors in ((x,), (y,)):
        probe = gm_service._probe_package(
            target, survivors, partner=partner, next_season=next_season,
            next_season_cap_settings=CAP_SETTINGS, proposer_over_cap=False,
            tuning=DEFAULT_TUNING,
        )
        assert probe.matching_ceiling_ok is True
        assert probe.matching_floor_ok is True
        assert probe.value_gap >= 0.0

    # loyalty_a=1.0, score_a=-100, s_out_a=300 (all hand-supplied scalars
    # -- this function takes the seed record's own arithmetic as
    # arguments rather than a real DecisionRecord). fv_a[y]=500 makes
    # dropping y push gap_A' strongly positive; fv_a[x]=0 makes dropping
    # x leave gap_A' exactly at its pre-drop (negative) value.
    ranked = gm_service._rank_give_less_candidates(
        (x, y), target, partner=partner, incoming_pick_value=0.0,
        next_season=next_season, next_season_cap_settings=CAP_SETTINGS,
        proposer_over_cap=False, tuning=DEFAULT_TUNING,
        fv_a_by_player_id={x.id: 0.0, y.id: 500.0},
        loyalty_a=1.0, score_a=-100.0, s_out_a=300.0,
    )
    assert len(ranked) == 2, "both single-survivor candidates must survive into the ranked list"

    ranked_survivor_ids = [tuple(rp.id for rp in survivors) for _key, survivors, _probe in ranked]
    # The drop-y candidate (gap_A' = +360) must rank strictly ahead of the
    # drop-x candidate (gap_A' = -140) -- present, not dropped, in the
    # correct order.
    assert ranked_survivor_ids == [(x.id,), (y.id,)], (
        f"expected the positive-gap_A' candidate (survivors=(x,)) ranked ahead of the "
        f"negative-gap_A' one (survivors=(y,)), got {ranked_survivor_ids!r}"
    )
    negative_gap_key = ranked[1][0]
    positive_gap_key = ranked[0][0]
    assert positive_gap_key[0] < negative_gap_key[0], (
        "the negative-gap_A' candidate's own key must rank strictly worse "
        "(a smaller -gap_A' sorts first)"
    )


def test_build_counter_give_less_no_legal_move_at_n1_returns_none_not_empty():
    """§11 step 6's "no legal move" test, give-less shape (`N = 1`). No
    non-empty proper subset of a single-element outgoing set exists
    (§4.2's own give-less availability rule, `|outgoing_players| >= 2`) -- `build_
    counter` must return `None`, never an empty/degenerate counter."""
    target = _salary_player(1, 5_000_000, team_id=2)
    only_out = _salary_player(10, 3_000_000, team_id=1)
    team_a = build_team_snapshot(make_team(1), (only_out,), (), CAP_SETTINGS)
    team_b = build_team_snapshot(make_team(2), (target,), (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_a, team_b))
    ctx = _make_ctx(snapshot)
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id={target.id: 50.0, only_out.id: 5.0},
        players_ascending=(target,),
    )
    package = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(target,)),
        outgoing_a=AssetBundle(players=(only_out,)),
    )
    fake_record = DecisionRecord(
        team_id=1, philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=CompetitiveWindow.CONTEND,
        subject="x", verdict="reject", score=0.0, threshold=40.0, terms=(),
    )
    result = gm_service.build_counter(
        package, fake_record, fake_record, "give_less",
        team_a=team_a, partner=partner, fv_a_by_player_id={only_out.id: 5.0},
        incoming_pick_value=0.0, snapshot=snapshot, ctx=ctx,
    )
    assert result is None


def test_build_counter_give_less_rejects_a_candidate_failing_matching_floor_ok():
    """§11 step 6's own explicit coverage requirement: "assert the
    admission rule rejects a candidate failing `matching_floor_ok`, not
    only `matching_ceiling_ok`." Before this test, deleting the `matching_
    floor_ok` conjunct from `build_counter`'s admission rule made nothing
    fail -- only `matching_ceiling_ok` had any live coverage.

    A (the acting side, over the cap) has two $1M outgoing players; the
    target carries a $30M salary. Every single-survivor give-less
    candidate at `N = 2` clears `matching_ceiling_ok` (the partner, B, is
    NOT over the cap, so the ceiling conjunct is trivially true) and
    clears `value_gap >= 0.0` (the `fv` values are chosen so the survivor
    alone outvalues the target to B), but fails `matching_floor_ok` --
    A is over the cap and the incoming salary (`target`'s $30M, no throw-
    ins at `N - 1 = 0`) is far more than `1.25x + $100k` of the $1M
    survivor it would send out. With no candidate admitted on either
    side of the enumeration, `build_counter` must return `None` rather
    than an illegally-matched counter."""
    next_season = _SEASON + 1
    target = _salary_player(1, 30_000_000, team_id=2)
    x = _salary_player(101, 1_000_000, team_id=1)
    y = _salary_player(102, 1_000_000, team_id=1)
    team_a = build_team_snapshot(make_team(1), (x, y), (), CAP_SETTINGS)
    team_b = build_team_snapshot(make_team(2), (target,), (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_a, team_b))
    ctx = _make_ctx(snapshot, room=-5_000_000)  # every team, including A, over the cap
    fv_b = {target.id: 60.0, x.id: 200.0, y.id: 200.0}
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False,  # B is NOT over the cap -- matching_ceiling_ok is trivially True
        fv_by_player_id=fv_b, players_ascending=(target,),
    )

    # Sanity: confirm the fixture actually isolates matching_floor_ok --
    # both single-survivor candidates clear the ceiling and the value bar
    # but fail the floor, so a pass here for the wrong reason (e.g. no
    # candidate ever clears value_gap) cannot masquerade as coverage.
    for survivors in ((x,), (y,)):
        probe = gm_service._probe_package(
            target, survivors, partner=partner, next_season=next_season,
            next_season_cap_settings=ctx.next_season_cap_settings,
            proposer_over_cap=True, tuning=ctx.tuning,
        )
        assert probe.matching_ceiling_ok is True
        assert probe.value_gap >= 0.0
        assert probe.matching_floor_ok is False

    package = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(target,)),
        outgoing_a=AssetBundle(players=(x, y)),
    )
    fake_record = DecisionRecord(
        team_id=1, philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=CompetitiveWindow.CONTEND,
        subject="x", verdict="reject", score=0.0, threshold=40.0, terms=(),
    )
    # `evaluate_trade` itself independently re-derives the same salary-
    # matching illegality (it is `_probe_package`'s own real-formula
    # counterpart), so asserting only `result is None` does not
    # discriminate a probe-stage admission bug from a downstream illegal-
    # stop catching it after the fact -- a build that deletes `matching_
    # floor_ok` from the admission conjunct would still land here on
    # `None` via `evaluate_trade`'s own `illegal:salary_matching` verdict.
    # Count the real `evaluate_trade` calls directly: a correct build
    # excludes BOTH candidates at the probe/admission stage and never
    # reaches pass 4 at all.
    real_evaluate = gm_service.trade_decisions.evaluate_trade
    calls = 0

    def _counting_evaluate(*args, _real=real_evaluate, **kwargs):
        nonlocal calls
        calls += 1
        return _real(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gm_service.trade_decisions, "evaluate_trade", _counting_evaluate)
        result = gm_service.build_counter(
            package, fake_record, fake_record, "give_less",
            team_a=team_a, partner=partner, fv_a_by_player_id={x.id: 5.0, y.id: 5.0},
            incoming_pick_value=0.0, snapshot=snapshot, ctx=ctx,
        )
    assert result is None
    assert calls == 0, (
        "build_counter reached a real evaluate_trade call on a candidate that "
        "should have been excluded at the matching_floor_ok admission stage"
    )


def test_build_counter_ask_more_no_legal_move_at_n3_returns_none_not_empty():
    """§11 step 6's "no legal move" test, ask-more shape (`N = 3`, already
    at `_MAX_OUTGOING_PLAYERS`). `build_counter` must return `None`
    immediately -- there is no room to append anything."""
    target = _salary_player(1, 5_000_000, team_id=2)
    outgoing = tuple(_salary_player(10 + i, 1_000_000, team_id=1) for i in range(3))
    team_a = build_team_snapshot(make_team(1), outgoing, (), CAP_SETTINGS)
    team_b = build_team_snapshot(make_team(2), (target,), (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_a, team_b))
    ctx = _make_ctx(snapshot)
    fv_by_player_id = {target.id: 50.0, **{rp.id: 5.0 for rp in outgoing}}
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_by_player_id, players_ascending=(target,),
    )
    package = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(target, *outgoing[:2])),
        outgoing_a=AssetBundle(players=outgoing),
    )
    fake_record = DecisionRecord(
        team_id=2, philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=CompetitiveWindow.CONTEND,
        subject="x", verdict="reject", score=0.0, threshold=40.0, terms=(),
    )
    result = gm_service.build_counter(
        package, fake_record, fake_record, "ask_more",
        team_a=team_a, partner=partner,
        fv_a_by_player_id={rp.id: 5.0 for rp in outgoing},
        incoming_pick_value=0.0, snapshot=snapshot, ctx=ctx,
    )
    assert result is None


def test_build_counter_ask_more_grows_outgoing_and_stops_at_self_margin(monkeypatch):
    """The ask-more axis's own core search (§5.1: "B holds the target
    fixed and appends one asset from A's roster... stopping the moment
    B's `value_gap >= self_margin`"). Hand-built rather than harness-
    scanned -- §4.3 measures live ask-more availability at 0.11% of all
    packages, genuinely absent from the single 30-team corpus this file's
    give-less fixtures already confirm draws from (§11 step 6's own
    pre-registration: a zero live count is the expected reading, not a
    build defect). A's own real `evaluate_trade` is monkeypatched to
    always accept -- the same `_accept_everything_record` isolation
    `run_trade_window`'s own selection tests already use -- so this test
    is purely about the search finding and appending the right asset, not
    about A's real acceptance arithmetic (`test_trade.py`'s own job).

    B7 -- the seed package's own outgoing leg carries a genuinely
    non-empty pick (`A`'s own 2027 first-rounder, going to `B` alongside
    the outgoing players), so this test's pick-invariance assertion is
    not the vacuous `() == ()` every prior ask-more assertion in this
    file ran on."""
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)

    target = _salary_player(1, 5_000_000, team_id=2)
    seed_out = _salary_player(10, 1_000_000, team_id=1)
    extra = _salary_player(11, 1_000_000, team_id=1)
    b_body = _salary_player(2, 1_000_000, team_id=2)
    team_a = build_team_snapshot(make_team(1), (seed_out, extra), (), CAP_SETTINGS)
    team_b = build_team_snapshot(make_team(2), (target, b_body), (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_a, team_b))
    ctx = _make_ctx(snapshot)

    fv_by_player_id = {target.id: 10.0, seed_out.id: 20.0, extra.id: 200.0, b_body.id: 1.0}
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_by_player_id, players_ascending=(b_body,),
    )
    seed_probe = gm_service._probe_package(
        target, (seed_out,), partner=partner, next_season=ctx.next_season,
        next_season_cap_settings=ctx.next_season_cap_settings, proposer_over_cap=False,
        tuning=ctx.tuning,
    )
    assert seed_probe.value_gap < 0.0, "fixture sanity: the seed alone must be B-infeasible"

    seed_pick = _make_pick(
        901, season=_SEASON + 1, original_team_id=1, current_team_id=1, pick_number=15,
    )
    package = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(target,)),
        outgoing_a=AssetBundle(players=(seed_out,), picks=(seed_pick,)),
    )
    fake_record = DecisionRecord(
        team_id=2, philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=CompetitiveWindow.CONTEND,
        subject="x", verdict="reject", score=0.0, threshold=40.0, terms=(),
    )
    result = gm_service.build_counter(
        package, fake_record, fake_record, "ask_more",
        team_a=team_a, partner=partner, fv_a_by_player_id={seed_out.id: 20.0, extra.id: 10.0},
        incoming_pick_value=0.0, snapshot=snapshot, ctx=ctx,
    )
    assert result is not None
    counter, record = result
    assert record.verdict == "accept"
    assert {rp.id for rp in counter.outgoing_a.players} == {seed_out.id, extra.id}
    assert len(counter.incoming_a.players) == len(counter.outgoing_a.players), (
        "roster-body neutrality -- _build_throw_ins's own invariant"
    )
    assert counter.incoming_a.picks == package.incoming_a.picks
    assert package.outgoing_a.picks == (seed_pick,), (
        "fixture sanity: the seed's pick tuple is non-empty"
    )
    assert counter.outgoing_a.picks == package.outgoing_a.picks


# ---------------------------------------------------------------------------
# §11 step 13 -- channel 2 (§8.3): the counter self-margin and its flip
# counter (`_Ch2MarginObservation`). These tests exercise `build_counter`'s
# own new observation logic directly, isolated from the real ranking/
# probing arithmetic (which every prior test in this section already
# covers) via a monkeypatched `_rank_give_less_candidates` returning a
# fixed, known-gap candidate list -- the give-less-axis test below is about
# THIS step's own inline-observation logic, not about which candidates a
# real corpus happens to rank first.
# ---------------------------------------------------------------------------


def test_build_counter_give_less_ch2_observation_tracks_baseline_and_live_indices(monkeypatch):
    """§11 step 13's channel 2 flip counter, give-less axis. Three
    candidates, in search order, with known, controlled gaps: index 0
    fails even the pre-step-13 `0.0` bar (`gap=-5.0`); index 1 clears
    `0.0` but not a `self_margin=5.0` bar (`gap=3.0`); index 2 clears both
    (`gap=10.0`). `baseline_index` (the first `gap >= 0.0` candidate) is
    always `1`, independent of `self_margin` -- the three assertions below
    walk `self_margin` from `0.0` (no flip: `live_index == baseline_index`)
    through `5.0` (flip: `live_index` moves to `2`) to `20.0` (flip: no
    candidate clears it, `live_index is None` while `baseline_index`
    still exists) -- exactly the three shapes §11 step 13's own verify
    names ("including one existing and the other not")."""
    target = _salary_player(1, 5_000_000, team_id=2)
    p0 = _salary_player(10, 1_000_000, team_id=1)
    p1 = _salary_player(11, 1_000_000, team_id=1)
    p2 = _salary_player(12, 1_000_000, team_id=1)
    outgoing_full = (p0, p1, p2)
    team_a = build_team_snapshot(make_team(1), outgoing_full, (), CAP_SETTINGS)
    team_b = build_team_snapshot(make_team(2), (target,), (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_a, team_b))
    ctx = _make_ctx(snapshot)
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id={target.id: 10.0}, players_ascending=(),
    )
    package = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(target,)),
        outgoing_a=AssetBundle(players=outgoing_full),
    )
    fake_record = DecisionRecord(
        team_id=1, philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=CompetitiveWindow.CONTEND,
        subject="x", verdict="reject", score=0.0, threshold=40.0, terms=(),
    )

    survivors_by_index = {0: (p0,), 1: (p1,), 2: (p2,)}
    gap_by_index = {0: -5.0, 1: 3.0, 2: 10.0}
    threshold = 40.0
    fake_probe = gm_service._PackageProbe(
        throw_ins=(), value_gap=0.0, matching_floor_ok=True, matching_ceiling_ok=True,
    )
    fake_ranked = [
        ((float(idx),), survivors_by_index[idx], fake_probe) for idx in range(3)
    ]
    monkeypatch.setattr(gm_service, "_rank_give_less_candidates", lambda *a, **k: fake_ranked)

    survivors_to_index = {
        tuple(sorted(rp.id for rp in v)): k for k, v in survivors_by_index.items()
    }

    def _fake_evaluate_trade(team_snapshot, incoming, outgoing, philosophy, window, **_kwargs):
        idx = survivors_to_index[tuple(sorted(rp.id for rp in outgoing.players))]
        gap = gap_by_index[idx]
        verdict = "accept" if gap >= 0.0 else "reject"
        return DecisionRecord(
            team_id=team_snapshot.id, philosophy=philosophy, window=window,
            subject=f"cand{idx}", verdict=verdict, score=threshold + gap, threshold=threshold,
            terms=(),
        )

    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _fake_evaluate_trade)
    fv_a_by_player_id = {p0.id: 1.0, p1.id: 1.0, p2.id: 1.0}

    # -- self_margin = 0.0 (default, byte-identical to every pre-step-13
    # caller): the `0.0` bar and the live bar are the SAME bar. --
    obs_default = gm_service._Ch2MarginObservation()
    result_default = gm_service.build_counter(
        package, fake_record, fake_record, "give_less",
        team_a=team_a, partner=partner, fv_a_by_player_id=fv_a_by_player_id,
        incoming_pick_value=0.0, snapshot=snapshot, ctx=ctx, ch2_observation=obs_default,
    )
    assert result_default is not None
    assert {rp.id for rp in result_default[0].outgoing_a.players} == {p1.id}
    assert obs_default.baseline_index == 1
    assert obs_default.live_index == 1

    # -- self_margin = 5.0: baseline stays 1, live moves to 2 -- a flip,
    # both indices exist. The emitted candidate must also move to index 2
    # (real behavior change, not just an observation-side effect). --
    obs_flip = gm_service._Ch2MarginObservation()
    result_flip = gm_service.build_counter(
        package, fake_record, fake_record, "give_less",
        team_a=team_a, partner=partner, fv_a_by_player_id=fv_a_by_player_id,
        incoming_pick_value=0.0, snapshot=snapshot, ctx=ctx,
        self_margin=5.0, ch2_observation=obs_flip,
    )
    assert result_flip is not None
    assert {rp.id for rp in result_flip[0].outgoing_a.players} == {p2.id}
    assert obs_flip.baseline_index == 1
    assert obs_flip.live_index == 2
    assert obs_flip.baseline_index != obs_flip.live_index

    # -- self_margin = 20.0: nothing clears it -- live_index is None while
    # baseline_index still exists, and `build_counter` genuinely returns
    # `None` (no legal move under this margin), not merely a missing
    # observation. --
    obs_no_live = gm_service._Ch2MarginObservation()
    result_none = gm_service.build_counter(
        package, fake_record, fake_record, "give_less",
        team_a=team_a, partner=partner, fv_a_by_player_id=fv_a_by_player_id,
        incoming_pick_value=0.0, snapshot=snapshot, ctx=ctx,
        self_margin=20.0, ch2_observation=obs_no_live,
    )
    assert result_none is None
    assert obs_no_live.baseline_index == 1
    assert obs_no_live.live_index is None


def test_build_counter_ask_more_ch2_observation_tracks_baseline_and_live_indices(monkeypatch):
    """§11 step 13's channel 2 flip counter, ask-more axis -- mirrors the
    give-less test immediately above, using this axis's own real,
    unmonkeypatched `ratio_order` walk (two real candidates, `c1` ranked
    first with a B-insufficient `value_gap`, `c2` ranked second and
    B-sufficient) rather than a monkeypatched ranking function, since the
    ask-more axis's own search has no equivalent ranking helper to
    replace. `A`'s own side is monkeypatched to always accept (isolating
    this test from A-side acceptance arithmetic, the same isolation every
    other ask-more test in this file already uses)."""
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)

    target = _salary_player(1, 5_000_000, team_id=2)
    seed_out = _salary_player(10, 1_000_000, team_id=1)
    c1 = _salary_player(11, 1_000_000, team_id=1)
    c2 = _salary_player(12, 1_000_000, team_id=1)
    b_body = _salary_player(2, 1_000_000, team_id=2)
    team_a = build_team_snapshot(make_team(1), (seed_out, c1, c2), (), CAP_SETTINGS)
    team_b = build_team_snapshot(make_team(2), (target, b_body), (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_a, team_b))
    ctx = _make_ctx(snapshot)

    # c1: fv_B/fv_A = 8/1 = 8.0 (ranked first by ratio, but B-insufficient
    # alone -- its own fv_B is small). c2: fv_B/fv_A = 300/50 = 6.0 (ranked
    # second despite the larger raw fv_B, and B-sufficient alone).
    fv_by_player_id = {
        target.id: 10.0, seed_out.id: 20.0, c1.id: 8.0, c2.id: 300.0, b_body.id: 1.0,
    }
    fv_a_by_player_id = {seed_out.id: 20.0, c1.id: 1.0, c2.id: 50.0}
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_by_player_id, players_ascending=(b_body,),
    )
    ratio_order, _ = gm_service._value_priority_orders(
        [c1, c2], fv_a_by_player_id=fv_a_by_player_id, fv_b_by_player_id=fv_by_player_id,
    )
    assert ratio_order == (c1, c2), "fixture sanity: c1 must be walked before c2"

    package = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(target,)),
        outgoing_a=AssetBundle(players=(seed_out,)),
    )
    fake_record = DecisionRecord(
        team_id=2, philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=CompetitiveWindow.CONTEND,
        subject="x", verdict="reject", score=0.0, threshold=40.0, terms=(),
    )

    probe_c1 = gm_service._probe_package(
        target, (seed_out, c1), partner=partner, next_season=ctx.next_season,
        next_season_cap_settings=ctx.next_season_cap_settings, proposer_over_cap=False,
        tuning=ctx.tuning,
    )
    probe_c2 = gm_service._probe_package(
        target, (seed_out, c2), partner=partner, next_season=ctx.next_season,
        next_season_cap_settings=ctx.next_season_cap_settings, proposer_over_cap=False,
        tuning=ctx.tuning,
    )
    assert probe_c1.value_gap < 0.0, "fixture sanity: O ∪ {c1} alone must be B-insufficient"
    assert probe_c2.value_gap >= 0.0, "fixture sanity: O ∪ {c2} alone must be B-sufficient"

    # -- self_margin = 0.0: c1 never clears even the `0.0` bar, so
    # `baseline_index` is 1 (c2's own position in `ratio_order`), and the
    # live bar is the same bar -- no flip. --
    obs_default = gm_service._Ch2MarginObservation()
    result_default = gm_service.build_counter(
        package, fake_record, fake_record, "ask_more",
        team_a=team_a, partner=partner, fv_a_by_player_id=fv_a_by_player_id,
        incoming_pick_value=0.0, snapshot=snapshot, ctx=ctx, ch2_observation=obs_default,
    )
    assert result_default is not None
    assert {rp.id for rp in result_default[0].outgoing_a.players} == {seed_out.id, c2.id}
    assert obs_default.baseline_index == 1
    assert obs_default.live_index == 1

    # -- self_margin above c2's own value_gap: nothing in the width clears
    # it -- live_index is None while baseline_index (c2's position) still
    # exists, and `build_counter` genuinely returns `None`. --
    obs_no_live = gm_service._Ch2MarginObservation()
    result_none = gm_service.build_counter(
        package, fake_record, fake_record, "ask_more",
        team_a=team_a, partner=partner, fv_a_by_player_id=fv_a_by_player_id,
        incoming_pick_value=0.0, snapshot=snapshot, ctx=ctx,
        self_margin=probe_c2.value_gap + 1.0, ch2_observation=obs_no_live,
    )
    assert result_none is None
    assert obs_no_live.baseline_index == 1
    assert obs_no_live.live_index is None


def test_build_counter_ask_more_probe_exactness_holds_with_self_margin_live():
    """§11 step 13's own single most important verify: probe exactness
    (§5.3) survives a live, nonzero `self_margin`. `self_margin` only
    raises the bar the ask-more axis's own probe-based search must clear
    before stopping (§8.3: "changes only the constructing side's own stop
    condition") -- it must never let a package through that a REAL,
    unapproximated `evaluate_trade` call on B's side would reject. This
    drives a real ask-more search with `self_margin > 0` (never `0.0`, the
    only value every pre-step-13 test used) against B's own REAL fit
    values (`ValuationCache.fit_values`, the exact function a real
    `evaluate_trade` call also uses internally -- a hand-picked, arbitrary
    `fv_by_player_id` would make `_probe_package`'s own prediction
    disagree with the real formula and prove nothing about exactness),
    then makes an INDEPENDENT, real `evaluate_trade` call on B's side
    against the exact emitted counter and confirms B's real verdict is
    'accept' -- not merely the probe's own prediction of one. A's own side
    is monkeypatched to always accept (isolating this test from A-side
    acceptance arithmetic -- the real check under test here is B's side,
    never A's). `extra` is a much stronger player than `seed_out` (rather
    than an identically-rated one) so that growing `O` genuinely increases
    B's own real fit-value assessment of the incoming bundle -- two
    interchangeable players do not reliably flip a real acceptance
    decision the way a real, meaningfully different asset does."""
    target = _player(1, overall=60, salary=5_000_000, team_id=2)
    seed_out = _player(10, overall=55, salary=1_000_000, team_id=1)
    extra = _player(11, overall=92, salary=3_000_000, team_id=1)
    b_body = _player(2, overall=60, salary=1_000_000, team_id=2)
    team_a = build_team_snapshot(make_team(1), (seed_out, extra), (), CAP_SETTINGS)
    team_b = build_team_snapshot(make_team(2), (target, b_body), (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_a, team_b))
    ctx = _make_ctx(snapshot)

    cache = ValuationCache()
    all_players = (*team_a.roster, *team_b.roster)
    fv_b = cache.fit_values(team_b, all_players, ctx)
    b_players_ascending = sorted(team_b.roster, key=lambda rp: (fv_b[rp.id], rp.id))
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=gm_service.PROFILES[ctx.philosophy_by_team[2]].loyalty,
        window=ctx.window_by_team[2], room_dollars=ctx.room_by_team[2],
        over_cap=ctx.room_by_team[2] < 0, fv_by_player_id=fv_b,
        players_ascending=tuple(b_players_ascending),
    )
    seed_only_probe = gm_service._probe_package(
        target, (seed_out,), partner=partner, next_season=ctx.next_season,
        next_season_cap_settings=ctx.next_season_cap_settings, proposer_over_cap=False,
        tuning=ctx.tuning,
    )
    assert seed_only_probe.value_gap < 0.0, "fixture sanity: the seed alone must be B-infeasible"
    o_plus_extra_probe = gm_service._probe_package(
        target, (seed_out, extra), partner=partner, next_season=ctx.next_season,
        next_season_cap_settings=ctx.next_season_cap_settings, proposer_over_cap=False,
        tuning=ctx.tuning,
    )
    assert o_plus_extra_probe.value_gap > 0.0, "fixture sanity: O ∪ {extra} must clear B's bar"

    package = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(target,)),
        outgoing_a=AssetBundle(players=(seed_out,)),
    )
    fake_record = DecisionRecord(
        team_id=2, philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=CompetitiveWindow.CONTEND,
        subject="x", verdict="reject", score=0.0, threshold=40.0, terms=(),
    )

    # A nonzero self_margin, strictly below the admitted candidate's own
    # value_gap so the SAME candidate is still expected to admit -- this
    # test is about self_margin's exactness property, not about forcing a
    # different candidate to be selected (the two tests above already
    # cover that).
    self_margin = o_plus_extra_probe.value_gap / 2.0
    assert self_margin > 0.0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)
        result = gm_service.build_counter(
            package, fake_record, fake_record, "ask_more",
            team_a=team_a, partner=partner, fv_a_by_player_id={seed_out.id: 20.0, extra.id: 10.0},
            incoming_pick_value=0.0, snapshot=snapshot, ctx=ctx, self_margin=self_margin,
        )
    assert result is not None
    counter, record_a_on_counter = result
    assert record_a_on_counter.verdict == "accept"
    assert {rp.id for rp in counter.outgoing_a.players} == {seed_out.id, extra.id}, (
        "fixture sanity: self_margin below the admitted gap must not force a bigger package"
    )

    # The INDEPENDENT, real check -- B's own real evaluate_trade on the
    # EXACT emitted counter, never the probe's own prediction of it. This
    # is the one call in this test that is NOT monkeypatched.
    real_record_b = gm_service.trade_decisions.evaluate_trade(
        team_b, counter.outgoing_a, counter.incoming_a,
        ctx.philosophy_by_team[2], ctx.window_by_team[2],
        league_rng_seed=ctx.league_rng_seed, season=ctx.season, phase=ctx.phase,
        cap_settings=ctx.cap_settings, next_season=ctx.next_season,
        next_season_cap_settings=ctx.next_season_cap_settings,
        next_season_room_dollars=ctx.room_by_team[2],
        rookie_scale=ctx.rookie_scale, league_snapshot=snapshot, tuning=ctx.tuning,
    )
    assert real_record_b.verdict == "accept", (
        "self_margin only raises A's own demand -- it must never emit a package B's "
        "own REAL evaluate_trade call would reject"
    )
    assert real_record_b.score - real_record_b.threshold == pytest.approx(
        o_plus_extra_probe.value_gap, abs=1e-6
    ), "probe exactness (§5.3) must hold to 1e-6 with self_margin live, not just at 0.0"


def test_build_counter_ask_more_probes_each_candidate_independently_against_the_fixed_base(
    monkeypatch,
):
    """B1 (critic round 30) -- each ask-more trial must be `O ∪ {x_i}` for
    a single `ratio_order` candidate, probed independently against the
    UNMODIFIED seed base `O`, never a cumulative `O`, `O ∪ {x1}`,
    `O ∪ {x1, x2}`, ... walk. A prior build round grew a running `grown`
    list instead, which could permanently commit to an early accepted-
    but-insufficient asset and never try a later single-asset candidate
    that alone would have cleared B's bar.

    `|O| = 2` -- both existing ask-more tests use `|O| = 1` or `|O| = 3`,
    neither of which would have caught this: at `|O| = 1` the buggy walk
    can still reach `O ∪ {x1, x2}` (2 additions fit under `_MAX_OUTGOING_
    PLAYERS = 3`) and might stumble onto an admitted package anyway; at
    `|O| = 3` no addition is possible at all. At `|O| = 2`, exactly one
    addition fits under the cap -- so the cumulative bug's own `len(grown)
    >= _MAX_OUTGOING_PLAYERS` check stops the walk after committing the
    FIRST candidate (`c1`, matching-legal but B-insufficient) and never
    tries `c2` at all, even independently. `c2` alone (`O ∪ {c2}`, never
    `O ∪ {c1, c2}`) is genuinely admitted. `ratio_order` is confirmed
    `[c1, c2]` directly, so `c1` is walked first."""
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)

    target = _salary_player(1, 5_000_000, team_id=2)
    p1 = _salary_player(10, 1_000_000, team_id=1)
    p2 = _salary_player(11, 1_000_000, team_id=1)
    c1 = _salary_player(12, 500_000, team_id=1)
    c2 = _salary_player(13, 500_000, team_id=1)
    b_body = _salary_player(2, 1_000_000, team_id=2)
    team_a = build_team_snapshot(make_team(1), (p1, p2, c1, c2), (), CAP_SETTINGS)
    team_b = build_team_snapshot(make_team(2), (target, b_body), (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_a, team_b))
    ctx = _make_ctx(snapshot)

    fv_b = {
        target.id: 100.0, p1.id: 5.0, p2.id: 5.0, c1.id: 20.0, c2.id: 200.0, b_body.id: 1.0,
    }
    fv_a = {p1.id: 5.0, p2.id: 5.0, c1.id: 1.0, c2.id: 50.0}
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_b, players_ascending=(b_body,),
    )

    ratio_order, _ = gm_service._value_priority_orders(
        [c1, c2], fv_a_by_player_id=fv_a, fv_b_by_player_id=fv_b,
    )
    assert ratio_order == (c1, c2), "fixture sanity: c1 must be walked before c2"

    # Fixture sanity, matching the critic's exact counterexample shape:
    # O ∪ {c1} alone is matching-legal but B-insufficient; O ∪ {c2} alone
    # (the fixed base O, never O ∪ {c1, c2}) is genuinely admitted.
    o_plus_c1 = gm_service._probe_package(
        target, (p1, p2, c1), partner=partner, next_season=ctx.next_season,
        next_season_cap_settings=ctx.next_season_cap_settings, proposer_over_cap=False,
        tuning=ctx.tuning,
    )
    assert o_plus_c1.matching_ceiling_ok and o_plus_c1.matching_floor_ok
    assert o_plus_c1.value_gap < 0.0
    o_plus_c2 = gm_service._probe_package(
        target, (p1, p2, c2), partner=partner, next_season=ctx.next_season,
        next_season_cap_settings=ctx.next_season_cap_settings, proposer_over_cap=False,
        tuning=ctx.tuning,
    )
    assert o_plus_c2.matching_ceiling_ok and o_plus_c2.matching_floor_ok
    assert o_plus_c2.value_gap >= 0.0

    package = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(target,)),
        outgoing_a=AssetBundle(players=(p1, p2)),
    )
    fake_record = DecisionRecord(
        team_id=2, philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=CompetitiveWindow.CONTEND,
        subject="x", verdict="reject", score=0.0, threshold=40.0, terms=(),
    )
    result = gm_service.build_counter(
        package, fake_record, fake_record, "ask_more",
        team_a=team_a, partner=partner, fv_a_by_player_id=fv_a,
        incoming_pick_value=0.0, snapshot=snapshot, ctx=ctx,
    )
    assert result is not None, (
        "the real, admittable O ∪ {c2} repair must be found -- a cumulative "
        "walk permanently commits to c1 and never reaches it"
    )
    counter, record = result
    assert record.verdict == "accept"
    assert {rp.id for rp in counter.outgoing_a.players} == {p1.id, p2.id, c2.id}, (
        "the emitted counter must be O ∪ {c2}, never O ∪ {c1, c2} or O ∪ {c1}"
    )


def test_build_counter_ask_more_walk_stops_at_the_first_admitted_candidate(monkeypatch):
    """§11 step 6's T3 scope note, ask-more half: this axis is not an
    exhaustive enumeration, so no exact probe count is asserted -- instead
    "the walk stops at the first admitted one (so a second admitted
    candidate is never probed)" is asserted directly. `extra1` alone
    already clears B's bar (`self_margin = 0.0`); `extra2` -- ranked
    second in `ratio_order`, an even larger `fv_B` but a worse `fv_B /
    fv_A` ratio -- must never reach a `_probe_package` call at all."""
    monkeypatch.setattr(gm_service.trade_decisions, "evaluate_trade", _accept_everything_record)

    target = _salary_player(1, 5_000_000, team_id=2)
    seed_out = _salary_player(10, 1_000_000, team_id=1)
    extra1 = _salary_player(11, 1_000_000, team_id=1)
    extra2 = _salary_player(12, 1_000_000, team_id=1)
    b_body = _salary_player(2, 1_000_000, team_id=2)
    team_a = build_team_snapshot(make_team(1), (seed_out, extra1, extra2), (), CAP_SETTINGS)
    team_b = build_team_snapshot(make_team(2), (target, b_body), (), CAP_SETTINGS)
    snapshot = build_league_snapshot(_LEAGUE_ID, _SEASON, (team_a, team_b))
    ctx = _make_ctx(snapshot)

    # extra1: fv_B/fv_A = 200/10 = 20.0 (ranked first). extra2: fv_B/fv_A =
    # 300/50 = 6.0 (ranked second, despite the larger raw fv_B=300).
    fv_by_player_id = {
        target.id: 10.0, seed_out.id: 20.0, extra1.id: 200.0, extra2.id: 300.0, b_body.id: 1.0,
    }
    fv_a_by_player_id = {seed_out.id: 20.0, extra1.id: 10.0, extra2.id: 50.0}
    partner = gm_service._PartnerTerms(
        team_id=2, loyalty=1.0, window=CompetitiveWindow.CONTEND, room_dollars=20_000_000,
        over_cap=False, fv_by_player_id=fv_by_player_id, players_ascending=(b_body,),
    )
    ratio_order, _ = gm_service._value_priority_orders(
        [extra1, extra2], fv_a_by_player_id=fv_a_by_player_id, fv_b_by_player_id=fv_by_player_id,
    )
    assert ratio_order == (extra1, extra2), "fixture sanity: extra1 must be walked first"

    package = CandidatePackage(
        team_a_id=1, team_b_id=2,
        incoming_a=AssetBundle(players=(target,)),
        outgoing_a=AssetBundle(players=(seed_out,)),
    )
    fake_record = DecisionRecord(
        team_id=2, philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=CompetitiveWindow.CONTEND,
        subject="x", verdict="reject", score=0.0, threshold=40.0, terms=(),
    )

    probed_sets: list[tuple[int, ...]] = []
    real_probe = gm_service._probe_package

    def _recording_probe(target_arg, players, *args, _real=real_probe, **kwargs):
        probed_sets.append(tuple(rp.id for rp in players))
        return _real(target_arg, players, *args, **kwargs)

    monkeypatch.setattr(gm_service, "_probe_package", _recording_probe)
    result = gm_service.build_counter(
        package, fake_record, fake_record, "ask_more",
        team_a=team_a, partner=partner, fv_a_by_player_id=fv_a_by_player_id,
        incoming_pick_value=0.0, snapshot=snapshot, ctx=ctx,
    )
    assert result is not None
    assert {rp.id for rp in result[0].outgoing_a.players} == {seed_out.id, extra1.id}
    assert not any(extra2.id in probed for probed in probed_sets), (
        "the walk must stop at the first admitted candidate (extra1) and never probe "
        "a trial set including extra2"
    )


def test_build_counter_preserves_5_3_invariants_on_the_construction_invariant_fixture():
    """§5.3's own invariant list, checked directly on a real emitted
    counter (the same construction-invariant fixture the first test in
    this section already confirms emits one): roster-body neutrality,
    both salary-matching constraints, asset membership, pick invariance,
    and probe exactness for the partner side."""
    snapshot, ctx, cache, pick_slot_ranks, give_less = _build_counter_corpus()
    for entry in give_less:
        cand = entry["cand"]
        outgoing_players = cand.outgoing_a.players
        if len(outgoing_players) != 2 or cand.outgoing_a.picks:
            continue
        a_id, b_id = entry["a_id"], entry["b_id"]
        target = cand.incoming_a.players[0]
        seed_probe = _probe(ctx, target, outgoing_players, entry["partner"], a_id)
        prefix_probe = _probe(ctx, target, (outgoing_players[0],), entry["partner"], a_id)
        if not (seed_probe.matching_floor_ok and prefix_probe.value_gap < 0.0):
            continue
        result = gm_service.build_counter(
            cand, entry["record_a"], entry["record_b"], "give_less",
            team_a=entry["team_a"], partner=entry["partner"],
            fv_a_by_player_id=entry["fv_a"], incoming_pick_value=0.0,
            snapshot=snapshot, ctx=ctx, pick_slot_ranks=pick_slot_ranks,
        )
        if result is None:
            continue
        counter, record_a_on_counter = result

        # Roster-body/occupancy neutrality (`_build_throw_ins`'s own
        # invariant, re-asserted rather than assumed).
        assert len(counter.incoming_a.players) == len(counter.outgoing_a.players)

        # Both salary-matching constraints, verified by re-running the
        # real legality check on the exact counter emitted.
        illegal = gm_service.trade_decisions._check_legality(
            entry["team_a"], counter.incoming_a, counter.outgoing_a, ctx.next_season,
            ctx.next_season_cap_settings, ctx.room_by_team[a_id],
        )
        assert illegal is None, f"counter must be legal on A's side, got {illegal!r}"

        # Asset membership -- every outgoing player is really on A's
        # roster (evaluate_trade already raises ValueError otherwise; this
        # re-checks the property directly rather than trusting silence).
        roster_ids = {rp.id for rp in entry["team_a"].roster}
        assert {rp.id for rp in counter.outgoing_a.players} <= roster_ids

        # Pick invariance (Decision P3-11) -- identical pick tuples,
        # both legs, seed vs. counter.
        assert counter.incoming_a.picks == cand.incoming_a.picks
        assert counter.outgoing_a.picks == cand.outgoing_a.picks

        # Probe exactness, partner side only: the winning candidate's own
        # probe value_gap must equal B's real evaluate_trade score minus
        # threshold, to floating-point precision.
        team_b = snapshot.team_by_id(b_id)
        winning_probe = _probe(ctx, target, counter.outgoing_a.players, entry["partner"], a_id)
        record_b_on_counter = gm_service.trade_decisions.evaluate_trade(
            team_b, counter.outgoing_a, counter.incoming_a,
            ctx.philosophy_by_team[b_id], ctx.window_by_team[b_id],
            league_rng_seed=ctx.league_rng_seed, season=ctx.season, phase=ctx.phase,
            cap_settings=ctx.cap_settings, next_season=ctx.next_season,
            next_season_cap_settings=ctx.next_season_cap_settings,
            next_season_room_dollars=ctx.room_by_team[b_id],
            rookie_scale=ctx.rookie_scale, league_snapshot=snapshot, tuning=ctx.tuning,
            pick_slot_ranks=pick_slot_ranks,
        )
        assert not record_b_on_counter.verdict.startswith("illegal:")
        assert abs(
            winning_probe.value_gap - (record_b_on_counter.score - record_b_on_counter.threshold)
        ) < 1e-6
        return
    pytest.fail("corpus contains no pick-free, filler-free, N=2 give-less package")


def _incoming_pick_value_for(entry, ctx, snapshot, pick_slot_ranks) -> float:
    """B5 fix 1 -- the real `incoming_pick_value` for a give-less package,
    priced in the PARTNER's (B's) own philosophy/window, matching §5.1's
    mandatory-on-both-axes instruction and T1'/step-5's own currency-
    direction convention (never the constructing side's, A's). `0.0` for
    a pick-free package -- `_probe_package`'s own documented default."""
    cand = entry["cand"]
    picks = cand.outgoing_a.picks
    if not picks:
        return 0.0
    b_id = entry["b_id"]
    return sum(
        gm_service.trade_decisions._price_pick(
            pick, ctx.philosophy_by_team[b_id], ctx.window_by_team[b_id],
            season=ctx.season, rookie_scale=ctx.rookie_scale,
            cap_settings=ctx.cap_settings, league_snapshot=snapshot,
            tuning=ctx.tuning, pick_slot_ranks=pick_slot_ranks,
        )
        for pick in picks
    )


def test_build_counter_give_less_hit_rate_and_stage_by_stage_report(capsys):
    """§11 step 6's report-only measurements -- NOT asserted against
    `r = 0.2040`/`[0.18, 0.24]` (step 1d's own band, measured on a
    different corpus/methodology across 45 seeds x 5 seasons; this is a
    single 30-team snapshot's give-less cell), printed for a human
    reviewer to compare shape against, and the per-stage rejection counts
    that make a band breach diagnosable (§11 step 6's own list: matching-
    ceiling, matching-floor, probe value_gap, illegal verdict, self_margin,
    width-budget-exhausted).

    Two B5 fixes over a prior version of this report: (1) `incoming_pick_
    value` is computed for real on every pick-bearing package (§5.1: it is
    "mandatory on both axes, not optional") rather than passed as a flat
    `0.0`, which systematically over-rejected the half of the corpus that
    carries a pick; (2) the denominator is filtered to **in-band-and-
    available** packages (real `counter_band()` from `managers/
    negotiation.py`, at the shipped `COUNTER_BAND_MULT`), not merely
    axis-available ones -- step 1d's own `r = 0.2040` is defined over the
    in-band population specifically, and a broader denominator structurally
    dilutes the rate.

    B4's fix: `stage_counts["illegal"]` and `["self_margin"]` are no
    longer dead. The real `evaluate_trade` calls `build_counter` makes for
    THIS package are captured by wrapping `trade_decisions.evaluate_trade`
    for the duration of that one call, and every captured record that is
    NOT the one `build_counter` ultimately returned is classified: an
    `illegal:*` verdict increments `stage_counts["illegal"]`; a legal
    verdict whose `gap < self_margin` (`0.0`, no memory) increments
    `stage_counts["self_margin"]`.

    On this corpus the measured rate (`0.2406`) is actually slightly
    ABOVE the frozen band's own upper edge (`0.24`) -- honestly recorded
    below with the SE-unit distance from step 1d's `r = 0.2040`, per §11
    step 6's own instruction that an excursion at this non-gate step is
    "recorded rather than blocking," never silenced or rounded away.

    The probe-count/wall-time instrumentation is scoped to ONLY the real
    `build_counter` invocation per package (patched in immediately before
    the call, patched back out immediately after) -- NOT the manual
    stage-disposition scan above it, which calls the real, unpatched
    `_probe_package`. Sharing one patched reference across both loops
    previously double-counted every probe (908 reported vs. `build_
    counter`'s own real 454) -- exactly the failure mode §11 step 6's own
    T3 note warns against."""
    snapshot, ctx, cache, pick_slot_ranks, give_less = _build_counter_corpus()
    available = [entry for entry in give_less if len(entry["cand"].outgoing_a.players) >= 2]

    # B5 fix 2 -- filter to in-band-and-available. "In-band" mirrors
    # negotiation.respond()'s own REJECT_TOO_FAR predicate exactly:
    # gap_a >= -counter_band(record_a), the acting side's own real record.
    opportunities = [
        entry for entry in available
        if (entry["record_a"].score - entry["record_a"].threshold)
        >= -counter_band(entry["record_a"])
    ]

    stage_counts = {
        "matching_ceiling": 0, "matching_floor": 0, "probe_gap": 0,
        "illegal": 0, "self_margin": 0, "budget_exhausted": 0,
    }
    emitted = 0
    probe_call_count = 0
    probe_wall_time = 0.0
    real_probe = gm_service._probe_package
    real_evaluate = gm_service.trade_decisions.evaluate_trade

    def _counting_probe(*args, _real=real_probe, **kwargs):
        nonlocal probe_call_count, probe_wall_time
        probe_call_count += 1
        t0 = time.perf_counter()
        result = _real(*args, **kwargs)
        probe_wall_time += time.perf_counter() - t0
        return result

    report_start = time.perf_counter()
    for entry in opportunities:
        cand = entry["cand"]
        outgoing_players = cand.outgoing_a.players
        target = cand.incoming_a.players[0]
        p_in = _incoming_pick_value_for(entry, ctx, snapshot, pick_slot_ranks)

        # The manual stage-disposition scan below calls the REAL, un-
        # patched `_probe_package` (via the `_probe` helper) -- it is the
        # test's own fixture-inspection code, not `build_counter`'s own
        # invocation, and must not feed the probe-count/wall-time
        # instrumentation below (critic-caught: sharing one patched
        # reference across both loops double-counted every probe -- 908
        # reported vs. `build_counter`'s own real 454, exactly 2x -- the
        # precise failure mode §11 step 6's own T3 note warns against:
        # "scope it to the invocation rather than to the test function").
        survivors_probed = []
        for survivors, _dropped in gm_service._give_less_candidates(outgoing_players):
            p = _probe(ctx, target, survivors, entry["partner"], entry["a_id"], pick=p_in)
            if not p.matching_ceiling_ok:
                stage_counts["matching_ceiling"] += 1
            elif not p.matching_floor_ok:
                stage_counts["matching_floor"] += 1
            elif p.value_gap < 0.0:
                stage_counts["probe_gap"] += 1
            else:
                survivors_probed.append(survivors)
        width = gm_service.COUNTER_SEARCH_WIDTH
        if len(survivors_probed) > width:
            stage_counts["budget_exhausted"] += len(survivors_probed) - width

        # B4 fix -- capture every real evaluate_trade call THIS package's
        # own build_counter invocation makes, and classify every one that
        # is not the emitted candidate.
        calls_this_pkg: list[DecisionRecord] = []

        def _capturing_evaluate(*args, _real=real_evaluate, _calls=calls_this_pkg, **kwargs):
            record = _real(*args, **kwargs)
            _calls.append(record)
            return record

        # Both patches are scoped to ONLY this one `build_counter` call --
        # the probe counter/timer above is patched in and immediately
        # patched back out per package, so it only ever observes calls
        # made from inside `build_counter`'s own invocation.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gm_service, "_probe_package", _counting_probe)
            mp.setattr(gm_service.trade_decisions, "evaluate_trade", _capturing_evaluate)
            result = gm_service.build_counter(
                cand, entry["record_a"], entry["record_b"], "give_less",
                team_a=entry["team_a"], partner=entry["partner"],
                fv_a_by_player_id=entry["fv_a"], incoming_pick_value=p_in,
                snapshot=snapshot, ctx=ctx, pick_slot_ranks=pick_slot_ranks,
            )

        emitted_record = result[1] if result is not None else None
        for record in calls_this_pkg:
            if record is emitted_record:
                continue
            if record.verdict.startswith("illegal:"):
                stage_counts["illegal"] += 1
            elif (record.score - record.threshold) < 0.0:  # self_margin == 0.0
                stage_counts["self_margin"] += 1
        if result is not None:
            emitted += 1
    report_wall_time = time.perf_counter() - report_start

    hit_rate = emitted / len(opportunities) if opportunities else 0.0
    probe_share = probe_wall_time / report_wall_time if report_wall_time else 0.0

    # §11 step 6's own required comparands -- step 1d's measured r, the
    # frozen band, and the frozen band's own gated-seed-count SE (Decision
    # P3-32's SE3, established when the band was frozen). This step is
    # NOT a gate run: "a reading outside the band is recorded rather than
    # blocking" (§11 step 6) -- so an excursion is reported honestly, in
    # SE units, rather than silenced or asserted away.
    _step1d_r = 0.2040
    _step1d_se3 = 0.0080166
    _band_low, _band_high = 0.18, 0.24
    diff_from_step1d = hit_rate - _step1d_r
    se_units = diff_from_step1d / _step1d_se3
    diff_from_band_high = hit_rate - _band_high
    in_band = _band_low <= hit_rate <= _band_high

    with capsys.disabled():
        print(
            f"\n[build_counter report] give-less axis-available={len(available)} "
            f"in-band-and-available opportunities={len(opportunities)} emitted={emitted} "
            f"hit_rate={hit_rate:.4f}\n"
            f"[build_counter report] comparands (different corpus/methodology, "
            f"printed for shape comparison only, never asserted): step 1d's measured "
            f"r={_step1d_r:.4f} (band [{_band_low}, {_band_high}]); the retired "
            f"closed-form census figure 0.240; the closed form's own known error "
            f"rates -- 2.83% wrongly excluded, 0.21% wrongly admitted; per-seed "
            f"dispersion is not available on this single-snapshot corpus, unlike "
            f"step 1d's real 5-seed measurement\n"
            f"[build_counter report] measured hit rate {hit_rate:.4f} is "
            f"{se_units:+.2f} SE3 {'above' if diff_from_step1d >= 0 else 'below'} "
            f"the frozen r={_step1d_r:.4f}, and "
            f"{'' if diff_from_band_high <= 0 else '+'}{diff_from_band_high:.4f} "
            f"{'above' if diff_from_band_high > 0 else 'within'} the frozen band's "
            f"own upper edge {_band_high} -- "
            f"{'inside the frozen band' if in_band else 'RECORDED per §11 step 6, not '}"
            f"{'' if in_band else 'blocking at this non-gate step'}\n"
            f"[build_counter report] stage-by-stage candidate disposition: {stage_counts}\n"
            f"[build_counter report] _probe_package calls={probe_call_count} "
            f"(scoped to build_counter's own invocation only) "
            f"wall_time={probe_wall_time:.4f}s of report total {report_wall_time:.4f}s "
            f"({probe_share:.2%})\n"
        )
    assert len(opportunities) > 0, "fixture sanity: the scan must find give-less opportunities"
    # The lower edge is the discriminating bound: reverting either half of
    # the B5 fix in isolation measurably drops the rate below it (critic-
    # measured 0.1353 with the pick-pricing half reverted, 0.2180 with the
    # in-band filter half reverted) -- an upper bound is NOT asserted,
    # since a reading above the band is an expected, disclosed, non-
    # blocking recording at this step, not a build defect to hide by
    # loosening the bound until it passes.
    assert hit_rate >= _band_low, (
        f"corrected hit_rate={hit_rate:.4f} is below the frozen band's own lower "
        f"edge ({_band_low}) -- stop and report rather than assuming the fix is complete"
    )


def test_self_managed_team_ids_matches_the_manager_and_not_auto_pilot_predicate():
    """RC-22.2 (`docs/design/auto-gm-role-construction.md`, C9): GM-2's
    predicate is extracted into `gm_service.self_managed_team_ids` so
    `role_service`'s PRESEASON pass and `load_gm_inputs` share one
    derivation. Three teams: a real self-managed team (manager, not auto-
    pilot), a manager who has delegated to the AI (auto-pilot), and an
    unmanaged team -- only the first is self-managed."""
    managed = dataclasses.replace(make_team(1), manager_user_id=501, auto_pilot=False)
    auto_piloted = dataclasses.replace(make_team(2), manager_user_id=502, auto_pilot=True)
    unmanaged = dataclasses.replace(make_team(3), manager_user_id=None)

    result = gm_service.self_managed_team_ids([managed, auto_piloted, unmanaged])

    assert result == frozenset({managed.id})
