"""Tier 1: `managers/decisions/trade.py` -- §9.1/§9.2's trade formula and
§12 step 8's own verify list, followed bullet-for-bullet (docs/design/
phase-2-manager-minds.md).

`cap_delta_bonus`'s own worked example ("a REBUILD team wanting 20% cap
room, holding 2%, taking on a dump shedding 10% of cap earns 0.90 * 0.10
* 1000 * 0.60 = 54 VP", §9.2) is pinned as a direct regression test rather
than only exercised indirectly through `evaluate_trade`.
"""
from __future__ import annotations

import dataclasses

import pytest

from dba2.domain.draft import DraftPick, ProtectionType
from dba2.domain.phase import Phase
from dba2.domain.player import Position
from dba2.domain.team import GmPhilosophy
from dba2.managers.decisions.trade import (
    _check_legality,
    _price_pick,
    cap_delta_bonus,
    evaluate_trade,
    fit_value_for,
    salary_next,
)
from dba2.managers.explain import DecisionRecord
from dba2.managers.needs import build_need_matrix, clamp_need_bonus, need_bonus
from dba2.managers.philosophy import PROFILES
from dba2.managers.picks import (
    expected_slot,
    pick_value,
    projected_slot_from_window_score,
    protected_pick_value,
)
from dba2.managers.snapshot import (
    AssetBundle,
    build_league_snapshot,
    build_team_snapshot,
)
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow
from dba2.managers.valuation import effective_talent_ovr, market_value_for
from tests.fixtures.managers import CAP_SETTINGS, ROOKIE_SCALE, make_contract, make_roster_player
from tests.fixtures.sim import make_team

_SEASON = 2026
_RNG_SEED = 2026_08
_PHASE = Phase.TRADE_DEADLINE
_LEAGUE_ID = 1


def _make_pick(
    pick_id: int,
    *,
    season: int,
    original_team_id: int,
    current_team_id: int,
    pick_number: int | None,
    round_: int = 1,
    protection_type: ProtectionType = ProtectionType.NONE,
    protection_through: int | None = None,
) -> DraftPick:
    return DraftPick(
        id=pick_id,
        league_id=_LEAGUE_ID,
        season=season,
        round=round_,
        original_team_id=original_team_id,
        current_team_id=current_team_id,
        pick_number=pick_number,
        used_for_player_id=None,
        protection_type=protection_type,
        protection_through=protection_through,
        swap_with_pick_id=None,
        conveyed=False,
        rolls_over_to_season=None,
    )


def _filler_roster(team_id: int, count: int, *, avoid_position: Position, base_id: int):
    """`count` unremarkable players, none at `avoid_position`, so a bundle
    that brings in a player at `avoid_position` reads as addressing a real
    need rather than fighting a glut."""
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


# ---------------------------------------------------------------------------
# cap_delta_bonus -- §9.2's own worked example, pinned directly
# ---------------------------------------------------------------------------


def test_cap_delta_bonus_matches_the_worked_example():
    """A REBUILD team (desired_room 0.20) holding 2% cap room, taking on a
    trade that sheds 10% of cap in outgoing salary, earns exactly
    `0.90 * 0.10 * 1000 * 0.60 = 54` VP (§9.2)."""
    cap_next = 100_000_000
    room_next = 2_000_000  # 2% of cap_next
    star = make_roster_player(
        1, age=28, season=_SEASON, overall_cached=80, primary_position=Position.C,
        peak_age_start=27, peak_age_end=31,
        contract=make_contract(1, start_season=_SEASON, salaries=(10_000_000,)),
    )
    outgoing = AssetBundle(players=(star,))
    incoming = AssetBundle(players=())
    # outgoing sheds 10% of cap_next; incoming brings back nothing.
    outgoing_salary = int(0.10 * cap_next)
    star = dataclasses.replace(
        star,
        contract=make_contract(1, start_season=_SEASON, salaries=(outgoing_salary,)),
    )
    outgoing = AssetBundle(players=(star,))

    bonus = cap_delta_bonus(
        incoming, outgoing, CompetitiveWindow.REBUILD, _SEASON, room_next, cap_next,
        tuning=DEFAULT_TUNING,
    )
    assert bonus == pytest.approx(54.0, abs=0.05)


def test_salary_next_reads_the_matching_contract_year_and_zero_otherwise():
    rp = make_roster_player(
        2, age=28, season=_SEASON, overall_cached=75, primary_position=Position.SF,
        peak_age_start=27, peak_age_end=31,
        contract=make_contract(2, start_season=_SEASON, salaries=(5_000_000, 6_000_000)),
    )
    assert salary_next(rp, _SEASON) == 5_000_000
    assert salary_next(rp, _SEASON + 1) == 6_000_000
    assert salary_next(rp, _SEASON + 5) == 0  # contract does not cover this season

    draft_eligible = make_roster_player(
        3, age=19, season=_SEASON, overall_cached=60, primary_position=Position.PG,
        peak_age_start=27, peak_age_end=31,
    )
    assert salary_next(draft_eligible, _SEASON) == 0


# ---------------------------------------------------------------------------
# The canonical mutually-beneficial trade: a contender's picks for a
# rebuilder's aging star, accepted on both sides.
# ---------------------------------------------------------------------------


def _contender_and_rebuilder():
    """Team A (CONTEND, star_maxer): 14 players, none at SF, so the
    incoming star fills a real hole. Team B (REBUILD, youth_developer):
    15 players including a 33-year-old SF star -- old enough that REBUILD's
    steep age_exponent (1.50) and youth_developer's vet_bias (0.70, which
    steepens the age curve's above-29 reshape further) punish him hard,
    while CONTEND's shallow age_exponent (0.70) does not, the asymmetry
    §4.3's own "two GMs can honestly disagree" design point exists for."""
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

    pick_1 = _make_pick(9001, season=_SEASON, original_team_id=1, current_team_id=1, pick_number=2)
    pick_2 = _make_pick(9002, season=_SEASON, original_team_id=1, current_team_id=1, pick_number=5)

    return team_a, team_b, snapshot_a, snapshot_b, star, (pick_1, pick_2)


def test_contenders_picks_for_a_rebuilders_star_is_accepted_by_both_sides():
    team_a, team_b, snapshot_a, snapshot_b, star, picks = _contender_and_rebuilder()

    incoming_for_a = AssetBundle(players=(star,))
    outgoing_for_a = AssetBundle(picks=picks)
    record_a = evaluate_trade(
        snapshot_a, incoming_for_a, outgoing_for_a,
        GmPhilosophy.STAR_MAXER, CompetitiveWindow.CONTEND,
        league_rng_seed=_RNG_SEED, season=_SEASON, phase=_PHASE, cap_settings=CAP_SETTINGS,
        next_season=_SEASON, next_season_cap_settings=CAP_SETTINGS,
        next_season_room_dollars=40_000_000, rookie_scale=ROOKIE_SCALE,
    )
    assert record_a.verdict == "accept", (record_a.score, record_a.threshold, record_a.terms)
    assert record_a.reconstructs()

    incoming_for_b = AssetBundle(picks=picks)
    outgoing_for_b = AssetBundle(players=(star,))
    record_b = evaluate_trade(
        snapshot_b, incoming_for_b, outgoing_for_b,
        GmPhilosophy.YOUTH_DEVELOPER, CompetitiveWindow.REBUILD,
        league_rng_seed=_RNG_SEED, season=_SEASON, phase=_PHASE, cap_settings=CAP_SETTINGS,
        next_season=_SEASON, next_season_cap_settings=CAP_SETTINGS,
        next_season_room_dollars=60_000_000, rookie_scale=ROOKIE_SCALE,
    )
    assert record_b.verdict == "accept", (record_b.score, record_b.threshold, record_b.terms)
    assert record_b.reconstructs()


# ---------------------------------------------------------------------------
# A strictly-lopsided package is rejected by the losing side.
# ---------------------------------------------------------------------------


def test_a_strictly_lopsided_package_is_rejected_by_the_losing_side():
    team_a, team_b, snapshot_a, snapshot_b, star, picks = _contender_and_rebuilder()

    cheap_pick = _make_pick(
        9003, season=_SEASON, original_team_id=1, current_team_id=1, pick_number=58
    )
    incoming_for_b = AssetBundle(picks=(cheap_pick,))
    outgoing_for_b = AssetBundle(players=(star,))

    record_b = evaluate_trade(
        snapshot_b, incoming_for_b, outgoing_for_b,
        GmPhilosophy.YOUTH_DEVELOPER, CompetitiveWindow.REBUILD,
        league_rng_seed=_RNG_SEED, season=_SEASON, phase=_PHASE, cap_settings=CAP_SETTINGS,
        next_season=_SEASON, next_season_cap_settings=CAP_SETTINGS,
        next_season_room_dollars=60_000_000, rookie_scale=ROOKIE_SCALE,
    )
    assert record_b.verdict == "reject", (record_b.score, record_b.threshold)
    assert record_b.score < record_b.threshold
    assert record_b.reconstructs()


# ---------------------------------------------------------------------------
# A salary-matching violation is rejected regardless of value.
# ---------------------------------------------------------------------------


def _matching_scenario(incoming_salary: int):
    team_a = make_team(1)
    cheap_player = make_roster_player(
        4000, age=27, season=_SEASON, overall_cached=68, primary_position=Position.PG, team_id=1,
        peak_age_start=27, peak_age_end=31,
        contract=make_contract(4000, team_id=1, start_season=_SEASON, salaries=(2_000_000,)),
    )
    roster_a = (cheap_player,) + _filler_roster(1, 14, avoid_position=Position.SF, base_id=5000)
    snapshot_a = build_team_snapshot(team_a, roster_a, (), CAP_SETTINGS)

    incoming = make_roster_player(
        4001, age=27, season=_SEASON, overall_cached=95, primary_position=Position.SF, team_id=2,
        peak_age_start=27, peak_age_end=31,
        contract=make_contract(4001, team_id=2, start_season=_SEASON, salaries=(incoming_salary,)),
    )
    return snapshot_a, cheap_player, incoming


def test_a_salary_matching_violation_is_rejected_regardless_of_value():
    # Team A is already over the cap (used_pre = cap - room) but nowhere
    # near apron_1 pre- or post-trade -- isolates the matching rule from
    # the apron_1/apron_2 ceilings (B2), which are checked separately.
    # Incoming ($10M) is nowhere close to outgoing ($2M * 1.25 + $100k =
    # $2.6M) -- a real matching violation, and still a great value trade by
    # VP alone (a 95-overall for a 68-overall + cash), proving "regardless
    # of value" actually means it.
    snapshot_a, cheap_player, superstar = _matching_scenario(10_000_000)
    record = evaluate_trade(
        snapshot_a, AssetBundle(players=(superstar,)), AssetBundle(players=(cheap_player,)),
        GmPhilosophy.STAR_MAXER, CompetitiveWindow.CONTEND,
        league_rng_seed=_RNG_SEED, season=_SEASON, phase=_PHASE, cap_settings=CAP_SETTINGS,
        next_season=_SEASON, next_season_cap_settings=CAP_SETTINGS,
        next_season_room_dollars=-5_000_000, rookie_scale=ROOKIE_SCALE,
    )
    assert record.verdict == "illegal:salary_matching"
    assert record.score == 0.0
    assert record.threshold == 0.0
    assert record.terms == ()
    assert record.reconstructs()


def test_the_same_scenario_is_accepted_once_the_incoming_salary_satisfies_matching():
    # Nit 7: the illegal test above only shows the violation is rejected --
    # it never shows the identical scenario would clear legality once
    # matching is satisfied, so "regardless of value" was only asserted in
    # a comment. Same teams/roster shape, incoming salary dropped to $2.5M
    # (<= $2M * 1.25 + $100k = $2.6M) -- legal, and (an even better deal by
    # VP, since the same 95-overall now costs far less) accepted.
    snapshot_a, cheap_player, cheap_incoming_star = _matching_scenario(2_500_000)
    record = evaluate_trade(
        snapshot_a, AssetBundle(players=(cheap_incoming_star,)),
        AssetBundle(players=(cheap_player,)),
        GmPhilosophy.STAR_MAXER, CompetitiveWindow.CONTEND,
        league_rng_seed=_RNG_SEED, season=_SEASON, phase=_PHASE, cap_settings=CAP_SETTINGS,
        next_season=_SEASON, next_season_cap_settings=CAP_SETTINGS,
        next_season_room_dollars=-5_000_000, rookie_scale=ROOKIE_SCALE,
    )
    assert record.verdict == "accept", (record.score, record.threshold, record.terms)
    assert record.reconstructs()


# ---------------------------------------------------------------------------
# Every verdict's terms sum to its reported net (Decision P2-18).
# ---------------------------------------------------------------------------


def test_every_verdict_reconstructs_across_several_scenarios():
    team_a, team_b, snapshot_a, snapshot_b, star, picks = _contender_and_rebuilder()
    scenarios = [
        (snapshot_a, AssetBundle(players=(star,)), AssetBundle(picks=picks),
         GmPhilosophy.STAR_MAXER, CompetitiveWindow.CONTEND, 40_000_000),
        (snapshot_b, AssetBundle(picks=picks), AssetBundle(players=(star,)),
         GmPhilosophy.YOUTH_DEVELOPER, CompetitiveWindow.REBUILD, 60_000_000),
        (snapshot_b, AssetBundle(), AssetBundle(players=(star,)),
         GmPhilosophy.EGALITARIAN, CompetitiveWindow.CONTEND, 60_000_000),
    ]
    for snapshot, incoming, outgoing, philosophy, window, room in scenarios:
        record = evaluate_trade(
            snapshot, incoming, outgoing, philosophy, window,
            league_rng_seed=_RNG_SEED, season=_SEASON, phase=_PHASE, cap_settings=CAP_SETTINGS,
            next_season=_SEASON, next_season_cap_settings=CAP_SETTINGS,
            next_season_room_dollars=room, rookie_scale=ROOKIE_SCALE,
        )
        assert record.reconstructs(), (record.verdict, record.score, record.threshold, record.terms)


# ---------------------------------------------------------------------------
# The returned type carries no counter-package field at all (Phase 3's
# boundary, enforced structurally).
# ---------------------------------------------------------------------------


def test_decision_record_has_no_counter_package_field():
    field_names = {f.name for f in dataclasses.fields(DecisionRecord)}
    assert field_names == {
        "team_id", "philosophy", "window", "subject", "verdict", "score", "threshold", "terms",
    }
    for forbidden in ("counter", "counter_offer", "counter_package"):
        assert forbidden not in field_names


# ---------------------------------------------------------------------------
# fit_value_for -- chaos jitter is bounded and only applied for GmPhilosophy.CHAOS
# ---------------------------------------------------------------------------


def _chaos_jitter_fixture():
    # A multi-season contract (not just a single, `_SEASON`-only year) so
    # `_chaos_ratio`'s own `season + 1` sweep (proving `season` is
    # load-bearing in the chaos salt) still prices a real, non-expired
    # contract year rather than dividing by a zeroed-out baseline.
    rp = make_roster_player(
        6000, age=27, season=_SEASON, overall_cached=78, primary_position=Position.PF,
        peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            6000, start_season=_SEASON, salaries=(10_000_000, 10_500_000, 11_000_000)
        ),
    )
    team = make_team(1)
    roster = (rp,) + _filler_roster(1, 5, avoid_position=Position.C, base_id=7000)
    snapshot = build_team_snapshot(team, roster, (), CAP_SETTINGS)
    matrix = build_need_matrix(snapshot)
    return rp, matrix


def _chaos_ratio(rp, matrix, *, team_id, season, phase, window):
    """`fit_value_for`'s actual `CHAOS` output divided by an independently
    recomputed pre-jitter baseline (the exact same `market_value_for`/
    `need_bonus`/`clamp_need_bonus` call `fit_value_for` itself makes) --
    isolates the jitter multiplier from `team_id`/`asset_id`/`season`'s
    *other*, separate effect on the base value (through `scout_player`'s
    own RNG salt), so a mutation that drops the jitter multiplication
    entirely, drops `phase` from its salt, or breaks the salt in any other
    way shows up as a ratio that stops moving or stops matching -- a bare
    `isinstance(..., float)` or `base >= 0.0` assertion (critic round 14
    B3's finding: the old version of this test) survives every one of
    those mutations untouched."""
    profile = PROFILES[GmPhilosophy.CHAOS]
    ovr = effective_talent_ovr(rp, profile)
    mv = market_value_for(
        rp, league_rng_seed=_RNG_SEED, team_id=team_id, season=season,
        philosophy=GmPhilosophy.CHAOS, window=window, cap_settings=CAP_SETTINGS,
    )
    bonus = clamp_need_bonus(need_bonus(rp, ovr, matrix, profile))
    baseline = mv * (1.0 + bonus)
    actual = fit_value_for(
        rp, matrix, league_rng_seed=_RNG_SEED, team_id=team_id, season=season, phase=phase,
        philosophy=GmPhilosophy.CHAOS, window=window, cap_settings=CAP_SETTINGS,
    )
    return actual / baseline


def test_chaos_jitter_only_applies_to_the_chaos_philosophy():
    rp, matrix = _chaos_jitter_fixture()

    non_chaos = fit_value_for(
        rp, matrix, league_rng_seed=_RNG_SEED, team_id=1, season=_SEASON, phase=_PHASE,
        philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=CompetitiveWindow.RETOOL,
        cap_settings=CAP_SETTINGS,
    )
    # Deterministic: calling twice with identical arguments (non-chaos)
    # returns an identical value -- no hidden randomness leaking in.
    non_chaos_again = fit_value_for(
        rp, matrix, league_rng_seed=_RNG_SEED, team_id=1, season=_SEASON, phase=_PHASE,
        philosophy=GmPhilosophy.TENDENCY_RESPECTER, window=CompetitiveWindow.RETOOL,
        cap_settings=CAP_SETTINGS,
    )
    assert non_chaos == pytest.approx(non_chaos_again)


def test_chaos_jitter_ratio_is_deterministic_and_load_bearing_on_every_salt_component():
    """Critic round 14 B3: `test_trade.py:355-359`'s own claim that `phase`
    is load-bearing was only ever checked with `isinstance(..., float)`.
    Replaced with real inequality assertions, extended to `team_id`/
    `asset_id`/`season` too -- all four of `rng_for`'s per-asset salt
    arguments (besides the fixed `"chaos"` tag and `league_rng_seed`
    itself) -- via `_chaos_ratio`'s baseline division, which isolates the
    jitter itself from each identifier's separate effect on `scout_
    player`'s own draw."""
    rp, matrix = _chaos_jitter_fixture()

    base_ratio = _chaos_ratio(
        rp, matrix, team_id=1, season=_SEASON, phase=_PHASE, window=CompetitiveWindow.RETOOL,
    )
    base_ratio_again = _chaos_ratio(
        rp, matrix, team_id=1, season=_SEASON, phase=_PHASE, window=CompetitiveWindow.RETOOL,
    )
    assert base_ratio == pytest.approx(base_ratio_again), "same salt -> same deterministic draw"

    other_phase_ratio = _chaos_ratio(
        rp, matrix, team_id=1, season=_SEASON, phase=Phase.FREE_AGENCY,
        window=CompetitiveWindow.RETOOL,
    )
    assert other_phase_ratio != pytest.approx(base_ratio), "phase must be load-bearing in the salt"

    other_team_ratio = _chaos_ratio(
        rp, matrix, team_id=2, season=_SEASON, phase=_PHASE, window=CompetitiveWindow.RETOOL,
    )
    assert other_team_ratio != pytest.approx(base_ratio), "team_id must be load-bearing in the salt"

    other_season_ratio = _chaos_ratio(
        rp, matrix, team_id=1, season=_SEASON + 1, phase=_PHASE, window=CompetitiveWindow.RETOOL,
    )
    assert other_season_ratio != pytest.approx(base_ratio), (
        "season must be load-bearing in the salt"
    )

    other_asset = dataclasses.replace(rp, player=dataclasses.replace(rp.player, id=6001))
    other_asset_ratio = _chaos_ratio(
        other_asset, matrix, team_id=1, season=_SEASON, phase=_PHASE,
        window=CompetitiveWindow.RETOOL,
    )
    assert other_asset_ratio != pytest.approx(base_ratio), (
        "asset_id must be load-bearing in the salt"
    )


def test_chaos_jitter_stays_within_its_clamp_and_reaches_both_rails_over_many_draws():
    """Critic round 14 B3 mutations 1/2 (drop `phase` from the salt;
    disable the jitter entirely, `value *= 1.0`) both survive the old
    `base >= 0.0` assertion untouched. A wide sweep of `team_id` (whose
    *other* effect, through `scout_player`, is divided back out by `_chaos_
    ratio`) reconstructs enough independent jitter draws to check the
    documented distribution in full: bounded to `[0.5, 1.6]` (§6), mean
    close to 1.0 (`1 + N(0, 0.25)`, clamped), and both clamp rails actually
    reached over the sweep, not just theoretically reachable."""
    rp, matrix = _chaos_jitter_fixture()

    draws = [
        _chaos_ratio(
            rp, matrix, team_id=team_id, season=_SEASON, phase=_PHASE,
            window=CompetitiveWindow.RETOOL,
        )
        for team_id in range(1, 801)
    ]

    assert all(0.5 - 1e-6 <= j <= 1.6 + 1e-6 for j in draws), (min(draws), max(draws))
    assert sum(draws) / len(draws) == pytest.approx(1.0, abs=0.1)
    assert min(draws) == pytest.approx(0.5, abs=1e-6), "lower clamp rail must be reached"
    assert max(draws) == pytest.approx(1.6, abs=1e-6), "upper clamp rail must be reached"


# ---------------------------------------------------------------------------
# B1 (critic round 14) -- an unresolved round-2 pick's projected slot is
# offset by 30 before it is priced; a round-1 pick from the same team/
# season is not.
# ---------------------------------------------------------------------------


def _league_ranked_by_overall(overalls: tuple[int, ...]):
    """Mirrors `test_picks.py::_league_ranked_by_overall` -- team `i`
    (1-indexed) carries a single, contract-less `RosterPlayer` at
    `overalls[i - 1]` overall, so `projected_slot_from_window_score`'s
    ranking is a clean, controllable function of `overalls` alone."""
    teams = []
    for team_id, ovr in enumerate(overalls, start=1):
        roster = (
            make_roster_player(
                90_000 + team_id, age=26, season=_SEASON, overall_cached=ovr,
                peak_age_start=27, peak_age_end=31,
            ),
        )
        snapshot = build_team_snapshot(make_team(team_id), roster, (), CAP_SETTINGS)
        teams.append(snapshot)
    return build_league_snapshot(1, _SEASON, tuple(teams))


def test_price_pick_offsets_an_unresolved_round_2_pick_by_30_slots():
    """B1: `_price_pick` never read `pick.round` -- an unresolved round-2
    pick priced identically to a round-1 pick from the same team/season
    (critic's repro: both at 103.4957082055697 VP), a 2-3x overvaluation
    against §12 step 5's own slot-45 sanity band (34.6-47.1 VP, default
    philosophy). A 30-team league ranked strictly by overall puts the
    15th-weakest team at `projected_slot = 15`; offset by 30 for round 2
    lands it exactly on slot 45."""
    overalls = tuple(60 + i for i in range(30))
    league = _league_ranked_by_overall(overalls)
    target_team_id = 15

    pick_round1 = _make_pick(
        9101, season=_SEASON, original_team_id=target_team_id, current_team_id=target_team_id,
        pick_number=None, round_=1,
    )
    pick_round2 = _make_pick(
        9102, season=_SEASON, original_team_id=target_team_id, current_team_id=target_team_id,
        pick_number=None, round_=2,
    )

    value_round1 = _price_pick(
        pick_round1, GmPhilosophy.TENDENCY_RESPECTER, CompetitiveWindow.RETOOL,
        season=_SEASON, rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS,
        league_snapshot=league,
    )
    value_round2 = _price_pick(
        pick_round2, GmPhilosophy.TENDENCY_RESPECTER, CompetitiveWindow.RETOOL,
        season=_SEASON, rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS,
        league_snapshot=league,
    )

    assert value_round2 != pytest.approx(value_round1)
    assert value_round2 < value_round1, (value_round1, value_round2)

    # Landed on slot 45 exactly (years_out=0 -> expected_slot is the
    # identity) -- check against §12 step 5's own published band at
    # default philosophy, all four windows.
    for window, band in (
        (CompetitiveWindow.CONTEND, 34.6),
        (CompetitiveWindow.WIN_NOW, 37.3),
        (CompetitiveWindow.RETOOL, 42.1),
        (CompetitiveWindow.REBUILD, 47.1),
    ):
        value = _price_pick(
            pick_round2, GmPhilosophy.TENDENCY_RESPECTER, window,
            season=_SEASON, rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS,
            league_snapshot=league,
        )
        assert value == pytest.approx(band, abs=0.05), (window, value)


def test_price_pick_round_2_offset_is_applied_after_expected_slots_regression():
    """Round 15 B1: the first B1 fix applied `+30` *before* `expected_
    slot`'s regression toward 15.5 (round 1's own mean), which regressed
    an already-shifted 31-60 value back toward round 1's center instead of
    round 2's own (45.5) -- non-monotone in `years_out` and 2.1-2.6x over
    `pick_value(31, years_out)` for any future pick. Applying the offset
    *after* regression (this fix) must be monotone non-increasing in
    `years_out` and converge toward 45.5 (round 2's mean), the same shape
    §12 step 5 already requires of `expected_slot` itself for round 1."""
    overalls = tuple(60 + i for i in range(30))
    league = _league_ranked_by_overall(overalls)
    target_team_id = 30  # weakest team -> projected_slot = 1 within round 2

    prices = []
    for years_out in range(4):
        pick = _make_pick(
            9200 + years_out, season=_SEASON + years_out, original_team_id=target_team_id,
            current_team_id=target_team_id, pick_number=None, round_=2,
        )
        prices.append(
            _price_pick(
                pick, GmPhilosophy.TENDENCY_RESPECTER, CompetitiveWindow.RETOOL,
                season=_SEASON, rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS,
                league_snapshot=league,
            )
        )

    for later, earlier in zip(prices[1:], prices[:-1], strict=True):
        assert later < earlier, prices  # monotone non-increasing in years_out

    # Every round-2 price must sit inside the true pick_value(31..60) curve
    # -- never above pick_value(31, years_out), the most valuable slot in
    # the round at that horizon (the exact inflation round 15 found).
    for years_out, price in enumerate(prices):
        ceiling = pick_value(
            31, years_out, GmPhilosophy.TENDENCY_RESPECTER, CompetitiveWindow.RETOOL,
            rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS,
        )
        assert price <= ceiling + 0.01, (years_out, price, ceiling)


def test_price_pick_raises_without_a_league_snapshot_for_an_unresolved_pick():
    """Round 15 nit 4: `_price_pick`'s `ValueError` guard for an unresolved
    pick (`pick_number is None`) with no `league_snapshot` supplied is
    named in the module docstring as this module's contract, but nothing
    exercised it -- neutering the guard survived the full suite."""
    pick = _make_pick(
        9199, season=_SEASON, original_team_id=1, current_team_id=1,
        pick_number=None, round_=1,
    )
    with pytest.raises(ValueError, match="league_snapshot"):
        _price_pick(
            pick, GmPhilosophy.TENDENCY_RESPECTER, CompetitiveWindow.RETOOL,
            season=_SEASON, rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS,
            league_snapshot=None,
        )


def test_price_pick_prices_a_protected_pick_through_protected_pick_value():
    """Round 15 nit 3: the `protected_pick_type != NONE` branch (calling
    `picks.py::protected_pick_value` instead of `pick_value` directly) had
    no coverage at all -- replacing `exp_slot` with a literal survived the
    full suite. Cross-checked against an independent call to `protected_
    pick_value` with the same inputs `_price_pick` itself computes."""
    overalls = tuple(60 + i for i in range(30))
    league = _league_ranked_by_overall(overalls)
    target_team_id = 15

    pick = _make_pick(
        9198, season=_SEASON, original_team_id=target_team_id, current_team_id=target_team_id,
        pick_number=None, round_=1, protection_type=ProtectionType.TOP_N,
        protection_through=5,
    )
    value = _price_pick(
        pick, GmPhilosophy.TENDENCY_RESPECTER, CompetitiveWindow.RETOOL,
        season=_SEASON, rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS,
        league_snapshot=league,
    )

    unprotected = _price_pick(
        dataclasses.replace(pick, id=9197, protection_type=ProtectionType.NONE),
        GmPhilosophy.TENDENCY_RESPECTER, CompetitiveWindow.RETOOL,
        season=_SEASON, rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS,
        league_snapshot=league,
    )
    expected_slot_value = expected_slot(
        float(projected_slot_from_window_score(league.team_by_id(target_team_id), league)), 0,
    )
    independent = protected_pick_value(
        expected_slot_value, 5, GmPhilosophy.TENDENCY_RESPECTER, CompetitiveWindow.RETOOL,
        rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS, years_out=0,
    )

    assert value == pytest.approx(independent)
    assert value != pytest.approx(unprotected)  # protection must actually change the price


# ---------------------------------------------------------------------------
# B2 (critic round 14) -- apron_1/apron_2 are independent hard ceilings,
# not a matching-ratio band; nit 1 -- used_post's filler component is
# netted, not double-counted.
# ---------------------------------------------------------------------------


def _legality_scenario(
    *, roster_count: int, cap_settings=CAP_SETTINGS, outgoing_salary: int = 0,
):
    team = make_team(1)
    roster = _filler_roster(1, roster_count, avoid_position=Position.C, base_id=9600)
    snapshot = build_team_snapshot(team, roster, (), cap_settings)
    outgoing_player = make_roster_player(
        9700, age=27, season=_SEASON, overall_cached=65, primary_position=Position.PG, team_id=1,
        peak_age_start=27, peak_age_end=31,
        contract=make_contract(9700, team_id=1, start_season=_SEASON, salaries=(outgoing_salary,)),
    )
    return snapshot, outgoing_player


def _incoming_player(salary: int):
    return make_roster_player(
        9701, age=27, season=_SEASON, overall_cached=90, primary_position=Position.PG, team_id=2,
        peak_age_start=27, peak_age_end=31,
        contract=make_contract(9701, team_id=2, start_season=_SEASON, salaries=(salary,)),
    )


def test_apron_1_ceiling_blocks_a_trade_that_crosses_it_without_crossing_apron_2():
    """B2: critic's own repro -- an under-cap team absorbing $90M for $2M
    lands $6.7M over `apron_1` (`used_post=202,647,000` vs
    `apron_1=195,945,000`) while staying under `apron_2` -- and the old
    banded-matching code never rejected it, because `apron_1` was only
    ever read as a switch between two matching ratios, never as a ceiling
    in its own right."""
    snapshot, outgoing_player = _legality_scenario(roster_count=14, outgoing_salary=2_000_000)
    incoming = _incoming_player(90_000_000)

    illegal = _check_legality(
        snapshot, AssetBundle(players=(incoming,)), AssetBundle(players=(outgoing_player,)),
        _SEASON, CAP_SETTINGS, 40_000_000,
    )
    assert illegal == "illegal:apron_1"


def test_apron_2_ceiling_blocks_a_trade_that_crosses_it():
    snapshot, outgoing_player = _legality_scenario(roster_count=14, outgoing_salary=2_000_000)
    incoming = _incoming_player(110_000_000)

    illegal = _check_legality(
        snapshot, AssetBundle(players=(incoming,)), AssetBundle(players=(outgoing_player,)),
        _SEASON, CAP_SETTINGS, 40_000_000,
    )
    assert illegal == "illegal:apron_2"


def test_roster_max_gate_blocks_a_trade_that_pushes_the_roster_over_the_limit():
    snapshot, _ = _legality_scenario(roster_count=15)  # already at roster_max
    incoming = _incoming_player(2_000_000)

    illegal = _check_legality(
        snapshot, AssetBundle(players=(incoming,)), AssetBundle(), _SEASON, CAP_SETTINGS,
        40_000_000,
    )
    assert illegal == "illegal:roster_max"


def test_illegal_roster_min_is_reachable_and_distinct_from_illegal_apron_2():
    """Round 15 nit 8/critic's dead-code finding: `illegal:roster_min`'s
    inner condition (`used_post > apron_2`) is byte-identical to the very
    next unconditional `illegal:apron_2` check, so deleting the whole
    `roster_min` branch changes no accept/reject *outcome* -- only the
    token returned when a team is simultaneously short-staffed (post_
    roster_count < roster_min) and over apron_2. Nothing previously
    exercised that combination, so the branch read as dead. This
    constructs it directly: trading away 4 of a 15-man roster's players
    drops the team to 11 bodies (< roster_min=14) while used_post still
    clears apron_2, so the `roster_min` label -- not `apron_2` -- must be
    what's returned."""
    team = make_team(1)
    roster = _filler_roster(1, 15, avoid_position=Position.C, base_id=9600)
    snapshot = build_team_snapshot(team, roster, (), CAP_SETTINGS)
    outgoing = AssetBundle(players=roster[:4])  # 4 * $3,000,000 = $12,000,000

    # used_pre = salary_cap - room = 154,647,000 - (-65,353,000) = 220,000,000.
    illegal = _check_legality(
        snapshot, AssetBundle(), outgoing, _SEASON, CAP_SETTINGS, -65_353_000,
    )
    assert illegal == "illegal:roster_min", illegal


_FILLER_TEST_SETTINGS = dataclasses.replace(
    CAP_SETTINGS, roster_min=14, min_salary=1_000_000, salary_cap=50_000_000,
    apron_1=999_999_999, apron_2=92_000_000,
)


def test_used_post_nets_the_pre_trade_filler_rather_than_double_counting_it():
    """Nit 1: `used_pre` (from `next_season_room_dollars`) already bakes in
    `capsheet.py`'s pre-trade `minimum_filler` for this 12-player (2-short-
    of-`roster_min`) roster ($2M, 2 * $1M). The old code added a *second*,
    full post-trade filler charge ($3M, 3 * $1M for the 11-player post-
    trade roster) on top of that without netting the pre-trade one back
    out -- these numbers are chosen so the old, double-counted figure
    ($93M) crosses this test's scaled-down `apron_2` ($92M) while the
    correct, netted figure ($91M) does not, so the fix changes the actual
    verdict, not just an internal number nobody reads."""
    settings = _FILLER_TEST_SETTINGS
    team = make_team(1)
    outgoing_player = make_roster_player(
        9800, age=27, season=_SEASON, overall_cached=65, primary_position=Position.PG, team_id=1,
        peak_age_start=27, peak_age_end=31,
        contract=make_contract(9800, team_id=1, start_season=_SEASON, salaries=(5_000_000,)),
    )
    roster = (outgoing_player,) + _filler_roster(1, 11, avoid_position=Position.C, base_id=9900)
    snapshot = build_team_snapshot(team, roster, (), settings)

    # used_pre = salary_cap - room = 50,000,000 - (-45,000,000) = 95,000,000.
    illegal = _check_legality(
        snapshot, AssetBundle(), AssetBundle(players=(outgoing_player,)),
        _SEASON, settings, -45_000_000,
    )
    assert illegal is None, illegal


def test_filler_math_uses_next_season_contract_coverage_not_todays_body_count():
    """Round 15 B2: the fix above nets `pre_filler`/`post_filler` correctly,
    but both were computed off `len(team_snapshot.roster)` (today's bodies)
    -- `capsheet_service.py`'s own `roster_count` for a projected season is
    players with a contract-year row covering *that season*, which can
    differ from today's roster (an about-to-expire contract counts as a
    body today but not for `next_season`). This roster has 15 bodies but
    only 10 are under contract for `next_season = _SEASON + 1` (5 carry a
    1-year deal that ends at `_SEASON`); trading away one of the 10
    next-season-contracted players (not one of the 5 expiring ones) drops
    `post_contracted` from 10 to 9, crossing the roster_min=14 shortfall
    from -4 to -5 bodies -- a real $1M filler increase the old, body-count
    basis (15 -> 14, never crossing 14) would have missed entirely.
    `apron_2` is set between the two implied `used_post` figures (old,
    wrong: $92M; correct: $93M) so the fix flips the actual verdict, not
    just an internal number nobody reads."""
    settings = dataclasses.replace(
        CAP_SETTINGS, roster_min=14, min_salary=1_000_000, salary_cap=50_000_000,
        apron_1=999_999_999, apron_2=92_500_000,
    )
    team = make_team(1)
    next_season = _SEASON + 1

    long_term = tuple(
        make_roster_player(
            9700 + i, age=26, season=_SEASON, overall_cached=64,
            primary_position=[p for p in Position if p is not Position.C][i % 4],
            team_id=1, peak_age_start=27, peak_age_end=31,
            contract=make_contract(
                9700 + i, team_id=1, start_season=_SEASON, salaries=(3_000_000, 3_000_000),
            ),
        )
        for i in range(10)
    )
    short_term = tuple(
        make_roster_player(
            9800 + i, age=26, season=_SEASON, overall_cached=64,
            primary_position=[p for p in Position if p is not Position.C][i % 4],
            team_id=1, peak_age_start=27, peak_age_end=31,
            contract=make_contract(
                9800 + i, team_id=1, start_season=_SEASON, salaries=(2_000_000,)
            ),
        )
        for i in range(5)
    )
    roster = long_term + short_term
    snapshot = build_team_snapshot(team, roster, (), settings)

    outgoing_player = long_term[0]  # contracted through next_season
    # used_pre = salary_cap - room = 50,000,000 - (-45,000,000) = 95,000,000.
    illegal = _check_legality(
        snapshot, AssetBundle(), AssetBundle(players=(outgoing_player,)),
        next_season, settings, -45_000_000,
    )
    assert illegal == "illegal:apron_2", illegal


# ---------------------------------------------------------------------------
# Nit 3 -- loyalty applies to outgoing players only, never to outgoing picks.
# ---------------------------------------------------------------------------


def test_loyalty_multiplies_outgoing_player_fit_value_but_never_outgoing_pick_value():
    """§6's own text: "`loyalty` multiplies the `fit_value` of *outgoing*
    players only." Verified two ways on the same bundle: the pick term's
    `weight` is exactly `-1.0` (not `-profile.loyalty`), and (to also catch
    a mutation that breaks the call pricing the pick itself, not just the
    loyalty weighting) its `input_value` matches an independently computed
    `pick_value`."""
    outgoing_player = make_roster_player(
        8300, age=27, season=_SEASON, overall_cached=70, primary_position=Position.C, team_id=1,
        peak_age_start=27, peak_age_end=31,
        contract=make_contract(8300, team_id=1, start_season=_SEASON, salaries=(2_000_000,)),
    )
    roster = (outgoing_player,) + _filler_roster(1, 14, avoid_position=Position.C, base_id=8400)
    snapshot = build_team_snapshot(make_team(1), roster, (), CAP_SETTINGS)
    pick = _make_pick(9200, season=_SEASON, original_team_id=1, current_team_id=1, pick_number=10)

    record = evaluate_trade(
        snapshot, AssetBundle(), AssetBundle(players=(outgoing_player,), picks=(pick,)),
        GmPhilosophy.VET_OVERRATER, CompetitiveWindow.RETOOL,
        league_rng_seed=_RNG_SEED, season=_SEASON, phase=_PHASE, cap_settings=CAP_SETTINGS,
        next_season=_SEASON, next_season_cap_settings=CAP_SETTINGS,
        next_season_room_dollars=40_000_000, rookie_scale=ROOKIE_SCALE,
    )
    profile = PROFILES[GmPhilosophy.VET_OVERRATER]
    assert profile.loyalty != 1.0, (
        "the test is only meaningful if loyalty actually scales something"
    )

    player_term = next(t for t in record.terms if t.label == f"out:player:{outgoing_player.id}")
    assert player_term.weight == pytest.approx(-profile.loyalty)
    assert player_term.contribution == pytest.approx(-profile.loyalty * player_term.input_value)

    pick_term = next(t for t in record.terms if t.label == f"out:pick:{pick.id}")
    assert pick_term.weight == pytest.approx(-1.0)
    expected_pick_value = pick_value(
        10, 0, GmPhilosophy.VET_OVERRATER, CompetitiveWindow.RETOOL,
        rookie_scale=ROOKIE_SCALE, cap_settings=CAP_SETTINGS,
    )
    assert pick_term.input_value == pytest.approx(expected_pick_value)
    assert pick_term.contribution == pytest.approx(-expected_pick_value)


# ---------------------------------------------------------------------------
# Nit 6 -- cap_delta_bonus contributes a nonzero term inside evaluate_trade.
# ---------------------------------------------------------------------------


def test_cap_delta_bonus_contributes_a_nonzero_term_inside_evaluate_trade():
    """Every existing `evaluate_trade` scenario produces a 0.0/-0.0
    `cap_delta_bonus` term -- the team already holds more room than it
    wants, so `room_hunger` clamps to 0. Built the way `cap_delta_bonus`'s
    own worked-example test is (a REBUILD team holding little room,
    shedding real salary) so the bonus integrates into `score` end-to-end,
    not just in isolation."""
    star = make_roster_player(
        7500, age=29, season=_SEASON, overall_cached=88, primary_position=Position.SF, team_id=1,
        peak_age_start=27, peak_age_end=31,
        contract=make_contract(7500, team_id=1, start_season=_SEASON, salaries=(40_000_000,)),
    )
    roster = (star,) + _filler_roster(1, 14, avoid_position=Position.PG, base_id=7600)
    snapshot = build_team_snapshot(make_team(1), roster, (), CAP_SETTINGS)

    record = evaluate_trade(
        snapshot, AssetBundle(), AssetBundle(players=(star,)),
        GmPhilosophy.YOUTH_DEVELOPER, CompetitiveWindow.REBUILD,
        league_rng_seed=_RNG_SEED, season=_SEASON, phase=_PHASE, cap_settings=CAP_SETTINGS,
        next_season=_SEASON, next_season_cap_settings=CAP_SETTINGS,
        next_season_room_dollars=3_000_000, rookie_scale=ROOKIE_SCALE,
    )
    bonus_term = next(t for t in record.terms if t.label == "cap_delta_bonus")
    assert bonus_term.contribution > 1.0, record.terms
    assert record.reconstructs()


# ---------------------------------------------------------------------------
# Nit 8 -- outgoing players must actually belong to the team's roster.
# ---------------------------------------------------------------------------


def test_evaluate_trade_raises_if_an_outgoing_player_is_not_on_the_roster():
    roster = _filler_roster(1, 14, avoid_position=Position.C, base_id=8500)
    snapshot = build_team_snapshot(make_team(1), roster, (), CAP_SETTINGS)

    not_on_roster = make_roster_player(
        8600, age=27, season=_SEASON, overall_cached=70, primary_position=Position.C, team_id=1,
        peak_age_start=27, peak_age_end=31,
        contract=make_contract(8600, team_id=1, start_season=_SEASON, salaries=(2_000_000,)),
    )
    with pytest.raises(ValueError, match="not on team"):
        evaluate_trade(
            snapshot, AssetBundle(), AssetBundle(players=(not_on_roster,)),
            GmPhilosophy.TENDENCY_RESPECTER, CompetitiveWindow.RETOOL,
            league_rng_seed=_RNG_SEED, season=_SEASON, phase=_PHASE, cap_settings=CAP_SETTINGS,
            next_season=_SEASON, next_season_cap_settings=CAP_SETTINGS,
            next_season_room_dollars=40_000_000, rookie_scale=ROOKIE_SCALE,
        )
