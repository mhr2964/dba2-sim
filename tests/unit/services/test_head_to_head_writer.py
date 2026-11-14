"""Tier 2: Decision P3-41's two required head-to-head writer fixtures
(docs/design/phase-3-inter-manager-society.md §7, §10 Tier 2, §11 step 11)
-- driven through the REAL negotiation loop (`gm_service._run_negotiation`
/`run_trade_window`) and the REAL memory-update path
(`gm_service._record_negotiation_in_h2h`), never by hand-constructing a
`HeadToHead` row and asserting it holds the value it was built with.

**Why one snapshot per arm, not one shared roster for the whole fixture.**
Fixture 1's own text asks for four far-merit-rejects, a fifth mutual
accept, a B-initiated negotiation, an in-band counter, and a no-axis
near-reject -- five different terminal states on the same `(A, B)`
ordered pair. Engineering all five from ONE static 30-player roster pair
turned out to be numerically intractable within this step's own budget
(the real value formula's `kappa`-driven star curve makes the "both sides
clear their own $40 VP floor" region a needle's width wide once loyalty/
need multipliers compound) -- each arm below swaps in its own small,
purpose-built pair of assets at an otherwise-unused roster slot, verified
against the REAL `evaluate_trade`/`respond()`/`build_counter` pipeline,
never a hand-built `DecisionRecord`. What stays constant across every arm,
and is what the design's own assertions actually check, is the pair of
identities (`team_id=1`, `opponent_id=2`) and the single `_H2HAccumulator`
every arm writes into -- so the running `far_reject_streak`/`opened`/etc.
these assertions read is the real, cumulative product of the real writer,
not a per-arm reset.
"""
from __future__ import annotations

import dataclasses
import unittest.mock as mock

import pytest

from dba2.domain.draft import DraftPick, ProtectionType
from dba2.domain.phase import Phase
from dba2.domain.player import Position
from dba2.domain.team import GmPhilosophy
from dba2.managers import memory
from dba2.managers.explain import DecisionRecord, ReasonTerm
from dba2.managers.negotiation import COUNTER_BAND_MULT, NegotiationStance, counter_band
from dba2.managers.snapshot import AssetBundle, build_league_snapshot, build_team_snapshot
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow
from dba2.services import gm_service
from dba2.services.gm_service import (
    CandidatePackage,
    GmContext,
    NegotiationOutcome,
    ValuationCache,
    compute_h2h_base_accept_rate,
)
from tests.fixtures.managers import CAP_SETTINGS, ROOKIE_SCALE, make_contract, make_roster_player
from tests.fixtures.sim import make_team

SEASON = 2026
LEAGUE_ID = 1
RNG_SEED = 999
A_ID, B_ID = 1, 2


def _filler_roster(team_id: int, count: int, base_id: int, avoid_position: Position | None = None):
    positions = [p for p in Position if p is not avoid_position]
    roster = []
    for i in range(count):
        roster.append(
            make_roster_player(
                base_id + i, age=27 + (i % 3), season=SEASON, overall_cached=60 + (i % 5),
                team_id=team_id, primary_position=positions[i % len(positions)],
                peak_age_start=27, peak_age_end=31,
                contract=make_contract(
                    base_id + i, team_id=team_id, start_season=SEASON, salaries=(2_000_000,)
                ),
            )
        )
    return tuple(roster)


def _make_pick(pick_id: int, season: int, team_id: int, pick_number: int) -> DraftPick:
    return DraftPick(
        id=pick_id, league_id=LEAGUE_ID, season=season, round=1,
        original_team_id=team_id, current_team_id=team_id, pick_number=pick_number,
        used_for_player_id=None, protection_type=ProtectionType.NONE, protection_through=None,
        swap_with_pick_id=None, conveyed=False, rolls_over_to_season=None,
    )


def _ctx_for(snapshot) -> GmContext:
    return GmContext(
        league_id=LEAGUE_ID, season=SEASON, phase=Phase.REGULAR_SEASON,
        league_rng_seed=RNG_SEED, cap_settings=CAP_SETTINGS, next_season_cap_settings=CAP_SETTINGS,
        rookie_scale=ROOKIE_SCALE,
        room_by_team={A_ID: 60_000_000, B_ID: 60_000_000},
        apron1_room_by_team={A_ID: 60_000_000, B_ID: 60_000_000},
        philosophy_by_team={A_ID: GmPhilosophy.TENDENCY_RESPECTER, B_ID: GmPhilosophy.EGALITARIAN},
        window_by_team={A_ID: CompetitiveWindow.RETOOL, B_ID: CompetitiveWindow.CONTEND},
        tuning=DEFAULT_TUNING,
    )


def _partner_terms(team_id: int, ctx: GmContext, snapshot, fv_by_player_id):
    players_ascending = sorted(snapshot.roster, key=lambda rp: (fv_by_player_id[rp.id], rp.id))
    return gm_service._PartnerTerms(
        team_id=team_id,
        loyalty=gm_service.PROFILES[ctx.philosophy_by_team[team_id]].loyalty,
        window=ctx.window_by_team[team_id],
        room_dollars=ctx.room_by_team[team_id],
        over_cap=ctx.room_by_team[team_id] < 0,
        fv_by_player_id=fv_by_player_id,
        players_ascending=tuple(players_ascending),
    )


def _base_rosters():
    """Team A (15 mediocre fillers, no PG on the roster -- a real PG
    target therefore reads as addressing a need) and team B (two real
    stars plus 12 mediocre fillers, no C on the roster). Both teams have
    plenty of cap room and stay well clear of every apron/roster-count
    gate for every trade this module constructs."""
    team_a = make_team(A_ID)
    team_b = make_team(B_ID)
    roster_a = _filler_roster(A_ID, 15, base_id=1000, avoid_position=Position.PG)
    star1 = make_roster_player(
        2000, age=27, season=SEASON, overall_cached=92, team_id=B_ID,
        primary_position=Position.SF, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            2000, team_id=B_ID, start_season=SEASON, salaries=(30_000_000, 32_000_000)
        ),
    )
    star2 = make_roster_player(
        2001, age=28, season=SEASON, overall_cached=90, team_id=B_ID,
        primary_position=Position.PF, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            2001, team_id=B_ID, start_season=SEASON, salaries=(28_000_000, 29_000_000)
        ),
    )
    fillers_b = _filler_roster(B_ID, 13, base_id=2010, avoid_position=Position.C)
    return team_a, team_b, roster_a, star1, star2, fillers_b


# ---------------------------------------------------------------------------
# Fixture 1 -- far_reject_streak, plus every §10 Tier 2 coverage extension
# named against this same fixture.
# ---------------------------------------------------------------------------


def test_fixture_1_far_reject_streak_and_every_coverage_extension():
    team_a, team_b, roster_a, star1, star2, fillers_b = _base_rosters()

    # The 5th (mutual-accept) trade's own dedicated assets -- a genuinely
    # win-win pair the real formula accepts on both sides (search-derived;
    # see module docstring). Swapped into roster slot 0 (A) and joining B's
    # roster in place of one otherwise-unused filler.
    sweetener5 = make_roster_player(
        1500, age=34, season=SEASON, overall_cached=79.64089305787854, team_id=A_ID,
        primary_position=Position.C, peak_age_start=27, peak_age_end=31,
        contract=make_contract(1500, team_id=A_ID, start_season=SEASON, salaries=(3_000_000,)),
    )
    target5 = make_roster_player(
        2500, age=25, season=SEASON, overall_cached=76.57522051344074, team_id=B_ID,
        primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(2500, team_id=B_ID, start_season=SEASON, salaries=(8_000_000,)),
    )
    pick_a = _make_pick(9001, SEASON + 1, A_ID, pick_number=20)

    roster_a_full = (sweetener5,) + roster_a[1:]
    roster_b_full = (star1, star2, target5) + fillers_b[:-1]
    assert len(roster_a_full) == 15
    assert len(roster_b_full) == 15

    snap_a = build_team_snapshot(team_a, roster_a_full, (pick_a,), CAP_SETTINGS)
    snap_b = build_team_snapshot(team_b, roster_b_full, (), CAP_SETTINGS)
    league_snapshot = build_league_snapshot(LEAGUE_ID, SEASON, (snap_a, snap_b))
    ctx = _ctx_for(league_snapshot)

    cache = ValuationCache()
    pick_slot_ranks = cache.pick_slot_ranks(league_snapshot, ctx.tuning)
    all_players = (*snap_a.roster, *snap_b.roster)
    fv_a = cache.fit_values(snap_a, all_players, ctx)
    fv_b = cache.fit_values(snap_b, all_players, ctx)
    partner_b = _partner_terms(B_ID, ctx, snap_b, fv_b)

    tally = gm_service._NegotiationTally()
    h2h = gm_service._H2HAccumulator(league_id=LEAGUE_ID, season=SEASON)

    # -- A opens exactly four legal negotiations against B --------------
    candidates = gm_service.propose_packages(A_ID, B_ID, league_snapshot, ctx, cache)
    assert len(candidates) == 4, (
        "TRADE_TARGETS_PER_PARTNER=2 targets x 2 variants -- asserted, not assumed"
    )

    computed_gaps: list[float] = []
    for cand in candidates:
        incoming_pick_value = sum(
            cache.pick_price(B_ID, pick, ctx, league_snapshot) for pick in cand.outgoing_a.picks
        )
        result = gm_service._run_negotiation(
            cand, a_id=A_ID, b_id=B_ID, team_a=snap_a, team_b=snap_b,
            partner=partner_b, fv_a_by_player_id=fv_a,
            incoming_pick_value=incoming_pick_value, snapshot=league_snapshot, ctx=ctx,
            pick_slot_ranks=pick_slot_ranks, max_negotiation_rounds=3, tally=tally, h2h=h2h,
        )
        last = result.turns[-1]
        ra, rb = last.record_a, last.record_b
        assert not ra.verdict.startswith("illegal:")
        assert not rb.verdict.startswith("illegal:")
        assert ra.verdict == "accept" and rb.verdict == "reject"
        band_b = counter_band(rb)
        gap_b = rb.score - rb.threshold
        assert gap_b < -band_b, "every far-reject candidate must realise a gap outside its own band"
        computed_gaps.append(abs(gap_b))

    row_ab = h2h.rows[(A_ID, B_ID)]
    row_ba = h2h.rows[(B_ID, A_ID)]

    # Assertion 1
    assert row_ab.far_reject_streak == 4.0
    assert type(row_ab.far_reject_streak) is float
    # Assertion 2
    assert row_ab.opened == 4.0
    assert row_ab.rejects_by_opponent == 4.0
    assert row_ab.accepts_by_opponent == 0.0
    assert row_ab.refused_illegal == 0.0
    # Assertion 3 -- directedness
    assert row_ba.far_reject_streak == 0.0
    # gap_by_opponent_sum / last_gap_by_opponent -- realised gaps, not constants
    assert row_ab.gap_by_opponent_sum == pytest.approx(sum(computed_gaps))
    assert row_ab.last_gap_by_opponent == pytest.approx(computed_gaps[-1])
    assert row_ab.last_season_interacted == SEASON
    assert row_ab.updated_season == SEASON

    # Assertion 5 -- decay, snapshotted BEFORE the reset/no-axis arms below
    # move the streak, per §10 Tier 2's own "fixture 1's 4.0 state is
    # snapshotted before its reset and decay arms run" instruction.
    row_ab_at_4 = dataclasses.replace(row_ab)
    decayed = memory.decay_head_to_head(row_ab_at_4, season=SEASON + 1)
    assert decayed.far_reject_streak == pytest.approx(2.4)
    assert decayed.updated_season == SEASON + 1

    # -- Assertion 4 -- a fifth negotiation, B accepts, streak resets ----
    cand5 = CandidatePackage(
        team_a_id=A_ID, team_b_id=B_ID,
        incoming_a=AssetBundle(players=(target5,)),
        outgoing_a=AssetBundle(players=(sweetener5,)),
    )
    result5 = gm_service._run_negotiation(
        cand5, a_id=A_ID, b_id=B_ID, team_a=snap_a, team_b=snap_b,
        partner=partner_b, fv_a_by_player_id=fv_a, incoming_pick_value=0.0,
        snapshot=league_snapshot, ctx=ctx, pick_slot_ranks=pick_slot_ranks,
        max_negotiation_rounds=3, tally=tally, h2h=h2h,
    )
    assert result5.outcome == NegotiationOutcome.ACCEPTED
    assert row_ab.far_reject_streak == 0.0
    assert row_ab.opened == 5.0
    assert row_ab.accepts_by_opponent == 1.0

    # -- Coverage: a B-initiated negotiation -----------------------------
    # `received`/`accepts_by_self`/`rejects_by_self`, and "one event
    # writes both rows" (`(A,B).received == 1` and `(B,A).opened == 1`
    # asserted together).
    b_give = roster_b_full[-1]
    a_get = roster_a_full[-1]
    fv_b_full = cache.fit_values(snap_b, all_players, ctx)
    partner_a = _partner_terms(A_ID, ctx, snap_a, fv_a)
    cand_b_init = CandidatePackage(
        team_a_id=B_ID, team_b_id=A_ID,
        incoming_a=AssetBundle(players=(a_get,)),
        outgoing_a=AssetBundle(players=(b_give,)),
    )
    result_b_init = gm_service._run_negotiation(
        cand_b_init, a_id=B_ID, b_id=A_ID, team_a=snap_b, team_b=snap_a,
        partner=partner_a, fv_a_by_player_id=fv_b_full, incoming_pick_value=0.0,
        snapshot=league_snapshot, ctx=ctx, pick_slot_ranks=pick_slot_ranks,
        max_negotiation_rounds=3, tally=tally, h2h=h2h,
    )
    last_b_init = result_b_init.turns[-1]
    assert not last_b_init.record_a.verdict.startswith("illegal:")
    assert not last_b_init.record_b.verdict.startswith("illegal:")
    assert row_ab.received == 1.0 and row_ba.opened == 1.0
    # A's own verdict on this received offer determines exactly one of
    # accepts_by_self/rejects_by_self, the other staying 0.
    a_verdict_on_received = last_b_init.record_b.verdict  # record_b is always the responder's
    if a_verdict_on_received == "accept":
        assert row_ab.accepts_by_self == 1.0 and row_ab.rejects_by_self == 0.0
    else:
        assert row_ab.rejects_by_self == 1.0 and row_ab.accepts_by_self == 0.0

    # -- Coverage: an in-band negotiation reaching COUNTER ---------------
    # counters_given/counters_received move together.
    sweetener_ib = make_roster_player(
        1600, age=26, season=SEASON, overall_cached=62.5, team_id=A_ID,
        primary_position=Position.C, peak_age_start=27, peak_age_end=31,
        contract=make_contract(1600, team_id=A_ID, start_season=SEASON, salaries=(2_000_000,)),
    )
    target_ib = make_roster_player(
        2600, age=27, season=SEASON, overall_cached=64.0, team_id=B_ID,
        primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(2600, team_id=B_ID, start_season=SEASON, salaries=(2_000_000,)),
    )
    roster_a_ib = roster_a_full[:5] + (sweetener_ib,) + roster_a_full[6:]
    roster_b_ib = roster_b_full[:5] + (target_ib,) + roster_b_full[6:]
    snap_a_ib = build_team_snapshot(team_a, roster_a_ib, (pick_a,), CAP_SETTINGS)
    snap_b_ib = build_team_snapshot(team_b, roster_b_ib, (), CAP_SETTINGS)
    league_ib = build_league_snapshot(LEAGUE_ID, SEASON, (snap_a_ib, snap_b_ib))
    cache_ib = ValuationCache()
    psr_ib = cache_ib.pick_slot_ranks(league_ib, ctx.tuning)
    all_players_ib = (*snap_a_ib.roster, *snap_b_ib.roster)
    fv_a_ib = cache_ib.fit_values(snap_a_ib, all_players_ib, ctx)
    fv_b_ib = cache_ib.fit_values(snap_b_ib, all_players_ib, ctx)
    partner_b_ib = _partner_terms(B_ID, ctx, snap_b_ib, fv_b_ib)
    cand_ib = CandidatePackage(
        team_a_id=A_ID, team_b_id=B_ID,
        incoming_a=AssetBundle(players=(target_ib,)),
        outgoing_a=AssetBundle(players=(sweetener_ib,)),
    )
    counters_given_before = row_ab.counters_given
    counters_received_before = row_ba.counters_received
    result_ib = gm_service._run_negotiation(
        cand_ib, a_id=A_ID, b_id=B_ID, team_a=snap_a_ib, team_b=snap_b_ib,
        partner=partner_b_ib, fv_a_by_player_id=fv_a_ib, incoming_pick_value=0.0,
        snapshot=league_ib, ctx=ctx, pick_slot_ranks=psr_ib,
        max_negotiation_rounds=3, tally=tally, h2h=h2h,
    )
    assert any(t.stance == NegotiationStance.COUNTER for t in result_ib.turns), (
        "fixture construction error -- this candidate must reach a COUNTER stance"
    )
    assert row_ab.counters_given == counters_given_before + 1.0
    assert row_ba.counters_received == counters_received_before + 1.0

    # -- The no-axis reset arm (round 12, C4) ----------------------------
    # A give-less N=1 package, in-band, with no repair axis available
    # (|O| = 1 < 2) -- REJECT_TOO_FAR via the "no legal move" branch, not
    # the far-gap branch. Must RESET the streak, not increment it: the
    # only assertion in this whole fixture that separates a build reading
    # the gap (correct) from one reading the terminal situation token
    # (the natural-but-wrong implementation).
    a_out = make_roster_player(
        1700, age=27, season=SEASON, overall_cached=66.0, team_id=A_ID,
        primary_position=Position.SG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(1700, team_id=A_ID, start_season=SEASON, salaries=(2_000_000,)),
    )
    b_target = make_roster_player(
        2700, age=27, season=SEASON, overall_cached=63.0, team_id=B_ID,
        primary_position=Position.SG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(2700, team_id=B_ID, start_season=SEASON, salaries=(2_000_000,)),
    )
    roster_a_na = roster_a_full[:6] + (a_out,) + roster_a_full[7:]
    roster_b_na = roster_b_full[:6] + (b_target,) + roster_b_full[7:]
    snap_a_na = build_team_snapshot(team_a, roster_a_na, (pick_a,), CAP_SETTINGS)
    snap_b_na = build_team_snapshot(team_b, roster_b_na, (), CAP_SETTINGS)
    league_na = build_league_snapshot(LEAGUE_ID, SEASON, (snap_a_na, snap_b_na))
    cache_na = ValuationCache()
    psr_na = cache_na.pick_slot_ranks(league_na, ctx.tuning)
    all_players_na = (*snap_a_na.roster, *snap_b_na.roster)
    fv_a_na = cache_na.fit_values(snap_a_na, all_players_na, ctx)
    fv_b_na = cache_na.fit_values(snap_b_na, all_players_na, ctx)
    partner_b_na = _partner_terms(B_ID, ctx, snap_b_na, fv_b_na)
    cand_na = CandidatePackage(
        team_a_id=A_ID, team_b_id=B_ID,
        incoming_a=AssetBundle(players=(b_target,)),
        outgoing_a=AssetBundle(players=(a_out,)),
    )
    streak_before_no_axis = row_ab.far_reject_streak
    result_na = gm_service._run_negotiation(
        cand_na, a_id=A_ID, b_id=B_ID, team_a=snap_a_na, team_b=snap_b_na,
        partner=partner_b_na, fv_a_by_player_id=fv_a_na, incoming_pick_value=0.0,
        snapshot=league_na, ctx=ctx, pick_slot_ranks=psr_na,
        max_negotiation_rounds=3, tally=tally, h2h=h2h,
    )
    last_na = result_na.turns[-1]
    assert last_na.stance == NegotiationStance.REJECT_TOO_FAR
    assert len(result_na.turns) == 1
    ra_na, rb_na = last_na.record_a, last_na.record_b
    assert ra_na.verdict == "reject" and rb_na.verdict == "accept"
    band_a_na = counter_band(ra_na)
    gap_a_na = ra_na.score - ra_na.threshold
    assert gap_a_na >= -band_a_na, "this arm's whole point is an IN-BAND gap"
    assert len(cand_na.outgoing_a.players) == 1, "give-less axis needs |O| < 2 for 'no axis'"
    assert row_ab.far_reject_streak == streak_before_no_axis == 0.0


def test_fixture_1_join_assertion_7_the_real_target_count_call_returns_1():
    """§10 Tier 2's assertion 7 (Decision P3-41, critic finding B7-2) --
    OWNED BY STEP 17, not step 11: the reader (`gm_service._channel1_
    target_count`/`propose_packages`) does not exist until channel 1 is
    built, so this join could not run at step 11. Reproduces fixture 1's
    own construction up to the `(A, B)` row's `far_reject_streak == 4.0`
    state (assertion 1's own state, snapshotted BEFORE fixture 1's reset
    (assertion 4) and decay (assertion 5) arms move it -- per §10 Tier 2's
    own "fixture 1's 4.0 state is snapshotted before its reset and decay
    arms run" instruction), then advances to the next window's decision
    site by handing that row to a FRESH `GmContext.head_to_head` and
    calling the REAL production per-pair target-count path -- `gm_
    service`'s own call site, never `memory.target_count_for_pair`
    directly (which would recreate exactly the hand-built-input weakness
    this decision exists to remove)."""
    team_a, team_b, roster_a, star1, star2, fillers_b = _base_rosters()
    pick_a = _make_pick(9001, SEASON + 1, A_ID, pick_number=20)
    snap_a = build_team_snapshot(team_a, roster_a, (pick_a,), CAP_SETTINGS)
    snap_b = build_team_snapshot(team_b, (star1, star2) + fillers_b, (), CAP_SETTINGS)
    league_snapshot = build_league_snapshot(LEAGUE_ID, SEASON, (snap_a, snap_b))
    ctx = _ctx_for(league_snapshot)

    cache = ValuationCache()
    all_players = (*snap_a.roster, *snap_b.roster)
    fv_a = cache.fit_values(snap_a, all_players, ctx)
    fv_b = cache.fit_values(snap_b, all_players, ctx)
    pick_slot_ranks = cache.pick_slot_ranks(league_snapshot, ctx.tuning)
    partner_b = _partner_terms(B_ID, ctx, snap_b, fv_b)

    tally = gm_service._NegotiationTally()
    h2h = gm_service._H2HAccumulator(league_id=LEAGUE_ID, season=SEASON)

    # -- A opens exactly four legal negotiations against B, all far-rejected
    # (identical to fixture 1's own construction above) --------------------
    candidates = gm_service.propose_packages(A_ID, B_ID, league_snapshot, ctx, cache)
    assert len(candidates) == 4
    for cand in candidates:
        incoming_pick_value = sum(
            cache.pick_price(B_ID, pick, ctx, league_snapshot) for pick in cand.outgoing_a.picks
        )
        result = gm_service._run_negotiation(
            cand, a_id=A_ID, b_id=B_ID, team_a=snap_a, team_b=snap_b,
            partner=partner_b, fv_a_by_player_id=fv_a,
            incoming_pick_value=incoming_pick_value, snapshot=league_snapshot, ctx=ctx,
            pick_slot_ranks=pick_slot_ranks, max_negotiation_rounds=3, tally=tally, h2h=h2h,
        )
        last = result.turns[-1]
        assert last.record_a.verdict == "accept" and last.record_b.verdict == "reject"

    row_ab = h2h.rows[(A_ID, B_ID)]
    assert row_ab.far_reject_streak == 4.0, (
        "fixture construction error -- expected assertion 1's own state"
    )

    # -- Advance to the next window's decision site: a fresh GmContext
    # carrying THIS row (snapshotted at 4.0, before any reset/decay) ------
    next_window_ctx = dataclasses.replace(
        ctx, head_to_head={(A_ID, B_ID): dataclasses.replace(row_ab)},
    )

    # The real production call site (`_channel1_target_count`, exactly
    # what `propose_packages` calls internally) -- not the pure ladder
    # function called directly against a hand-built row.
    target_count, h2h_row = gm_service._channel1_target_count(next_window_ctx, A_ID, B_ID)
    assert h2h_row is row_ab or h2h_row == row_ab
    assert target_count == 1

    # And the real `propose_packages` call reflects it end to end: 1
    # target x 2 variants (team A carries a future first-round pick).
    next_candidates = gm_service.propose_packages(
        A_ID, B_ID, league_snapshot, next_window_ctx, ValuationCache()
    )
    assert len(next_candidates) == 2


# ---------------------------------------------------------------------------
# B1 mutation-verify (round 13 correction) -- direct, minimal repro of the
# exact shape the critic's own finding named: a give-less-axis negotiation
# where B (the opponent, responder) ACCEPTS but A's (the initiator's) own
# gap is deeply negative. Hand-built at the `_record_negotiation_in_h2h`
# boundary rather than through the full negotiation pipeline, specifically
# so this test cannot accidentally only exercise the correct branch the
# way fixture 1's hand-built packages did pre-fix.
# ---------------------------------------------------------------------------


def _dummy_decision_record(team_id: int, *, verdict: str, score: float, threshold: float):
    return DecisionRecord(
        team_id=team_id, philosophy=GmPhilosophy.EGALITARIAN, window=CompetitiveWindow.RETOOL,
        subject="mutation-verify", verdict=verdict, score=score, threshold=threshold, terms=(),
    )


def test_b1_give_less_axis_b_accepts_a_deeply_negative_gap_resets_not_increments():
    """The critic's own B1 repro, reproduced directly: `a_accepts=False`
    (A itself rejects, give-less axis) while `b_accepts=True` (B, the
    opponent, ACCEPTS) -- and A's own gap is deeply negative, well past
    `-counter_band(record_a)`. Pre-fix, `_record_negotiation_in_h2h` chose
    `actor_record = ra` for this cell (mirroring `negotiation.respond()`'s
    own cell-3 "one side short" actor selection) and incremented `row_ir.
    far_reject_streak` off A's own deeply-negative gap -- counting "A
    itself couldn't find a package it liked" as "B rejected A by a wide
    margin," even though B never rejected anything at all this turn.
    Post-fix, the rule is keyed on `b_accepts` alone: ANY opponent accept
    resets the streak, with no gap read at all, on either axis."""
    from dba2.services.gm_service import Negotiation, NegotiationSituation, NegotiationTurn

    package = CandidatePackage(
        team_a_id=A_ID, team_b_id=B_ID, incoming_a=AssetBundle(), outgoing_a=AssetBundle(),
    )
    # A's own record: REJECT, with a gap far outside its own band -- this is
    # exactly the deeply-negative-gap shape the critic's repro names.
    record_a = _dummy_decision_record(A_ID, verdict="reject", score=-500.0, threshold=40.0)
    band_a = counter_band(record_a)
    assert record_a.score - record_a.threshold < -band_a, "fixture must be genuinely far for A"
    # B's own record: ACCEPT -- B never rejected this package at all.
    record_b = _dummy_decision_record(B_ID, verdict="accept", score=100.0, threshold=40.0)

    turn = NegotiationTurn(
        round_index=0, actor_id=A_ID, package=package, record_a=record_a, record_b=record_b,
        stance=NegotiationStance.REJECT_TOO_FAR,
        situation=NegotiationSituation.REJECT_TOO_FAR, added=(), removed=(),
    )
    result = Negotiation(
        initiator_id=A_ID, responder_id=B_ID, seed_subject="mutation-verify",
        frozen_picks=(), turns=(turn,), outcome=NegotiationOutcome.REJECTED,
    )

    h2h = gm_service._H2HAccumulator(league_id=LEAGUE_ID, season=SEASON)
    # Seed a nonzero streak first, so a reset is actually observable rather
    # than a no-op that would pass whether the fix is present or not.
    h2h.row(A_ID, B_ID).far_reject_streak = 3.0

    gm_service._record_negotiation_in_h2h(result, h2h)

    row_ab = h2h.rows[(A_ID, B_ID)]
    assert row_ab.far_reject_streak == 0.0, (
        "B accepted -- far_reject_streak must reset to 0.0, never increment off A's own gap"
    )
    assert row_ab.accepts_by_opponent == 1.0
    assert row_ab.opened == 1.0


# ---------------------------------------------------------------------------
# Fixture 2 -- executions, the trade-completion writer (Decision P3-42).
# ---------------------------------------------------------------------------


def test_fixture_2_executions_writer():
    """`propose_packages` is monkeypatched to return exactly one hand-built,
    mutually-beneficial `CandidatePackage` for the `(C, D)` ordered pair --
    every other function on the path (`_run_negotiation`, `respond`,
    `evaluate_trade`, `run_trade_window`'s own greedy selection and
    `_check_legality` re-check) is real. This is the same monkeypatch
    convention `test_gm_service_pure.py`'s own `run_trade_window` tests
    already use for isolating a specific shape."""
    c_id, d_id = 3, 4
    team_c = make_team(c_id)
    team_d = make_team(d_id)
    roster_c = _filler_roster(c_id, 15, base_id=3000, avoid_position=Position.PG)
    fillers_d = _filler_roster(d_id, 15, base_id=4000, avoid_position=Position.C)

    sweetener = make_roster_player(
        3500, age=34, season=SEASON, overall_cached=79.64089305787854, team_id=c_id,
        primary_position=Position.C, peak_age_start=27, peak_age_end=31,
        contract=make_contract(3500, team_id=c_id, start_season=SEASON, salaries=(3_000_000,)),
    )
    target = make_roster_player(
        4500, age=25, season=SEASON, overall_cached=76.57522051344074, team_id=d_id,
        primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(4500, team_id=d_id, start_season=SEASON, salaries=(8_000_000,)),
    )
    roster_c_full = (sweetener,) + roster_c[1:]
    roster_d_full = (target,) + fillers_d[1:]

    snap_c = build_team_snapshot(team_c, roster_c_full, (), CAP_SETTINGS)
    snap_d = build_team_snapshot(team_d, roster_d_full, (), CAP_SETTINGS)
    league_snapshot = build_league_snapshot(LEAGUE_ID, SEASON, (snap_c, snap_d))

    ctx = GmContext(
        league_id=LEAGUE_ID, season=SEASON, phase=Phase.REGULAR_SEASON,
        league_rng_seed=RNG_SEED, cap_settings=CAP_SETTINGS, next_season_cap_settings=CAP_SETTINGS,
        rookie_scale=ROOKIE_SCALE,
        room_by_team={c_id: 60_000_000, d_id: 60_000_000},
        apron1_room_by_team={c_id: 60_000_000, d_id: 60_000_000},
        philosophy_by_team={c_id: GmPhilosophy.TENDENCY_RESPECTER, d_id: GmPhilosophy.EGALITARIAN},
        window_by_team={c_id: CompetitiveWindow.RETOOL, d_id: CompetitiveWindow.CONTEND},
        tuning=DEFAULT_TUNING,
    )

    cand_cd = CandidatePackage(
        team_a_id=c_id, team_b_id=d_id,
        incoming_a=AssetBundle(players=(target,)),
        outgoing_a=AssetBundle(players=(sweetener,)),
    )

    def fake_propose(proposer_id, partner_id, snapshot, ctx, cache):
        if proposer_id == c_id and partner_id == d_id:
            return (cand_cd,)
        return ()

    with mock.patch.object(gm_service, "propose_packages", side_effect=fake_propose):
        result = gm_service.run_trade_window(league_snapshot, ctx, max_negotiation_rounds=2)

    # The trade actually applied -- checked BEFORE reading memory at all,
    # per Decision P3-41's own "a test that stopped at the mutual accept
    # would pass on a build where the memory update is wired to the
    # accept, not the completion" warning.
    assert len(result.executions) == 1
    execution = result.executions[0]
    assert execution.team_a_id == c_id and execution.team_b_id == d_id

    row_cd = result.head_to_head[(c_id, d_id)]
    row_dc = result.head_to_head[(d_id, c_id)]

    assert row_cd.executions == 1.0
    assert type(row_cd.executions) is float
    assert row_dc.executions == 0.0
    assert row_cd.accepts_by_opponent == 1.0 and row_cd.far_reject_streak == 0.0

    decayed = memory.decay_head_to_head(row_cd, season=SEASON + 1)
    assert decayed.executions == pytest.approx(0.6)
    up_threshold = 1.0  # step 17's own `up` predicate reads executions >= 1 -- not built here
    assert decayed.executions < up_threshold


def test_fixture_2_join_assertion_8_the_real_target_count_call_returns_3():
    """§10 Tier 2's assertion 8 (Decision P3-41, critic finding B7-2) --
    OWNED BY STEP 17, the `up`-side mirror of assertion 7. Reproduces
    fixture 2's own construction (a real mutual accept that actually
    executes between `C` and `D`), reads the resulting `(C, D)` row
    (`executions == 1.0`, `far_reject_streak == 0.0`), advances to the
    next window's decision site, and calls the REAL production per-pair
    target-count path -- never the pure ladder function directly."""
    c_id, d_id = 3, 4
    team_c = make_team(c_id)
    team_d = make_team(d_id)
    roster_c = _filler_roster(c_id, 15, base_id=3000, avoid_position=Position.PG)
    fillers_d = _filler_roster(d_id, 15, base_id=4000, avoid_position=Position.C)

    sweetener = make_roster_player(
        3500, age=34, season=SEASON, overall_cached=79.64089305787854, team_id=c_id,
        primary_position=Position.C, peak_age_start=27, peak_age_end=31,
        contract=make_contract(3500, team_id=c_id, start_season=SEASON, salaries=(3_000_000,)),
    )
    target = make_roster_player(
        4500, age=25, season=SEASON, overall_cached=76.57522051344074, team_id=d_id,
        primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(4500, team_id=d_id, start_season=SEASON, salaries=(8_000_000,)),
    )
    roster_c_full = (sweetener,) + roster_c[1:]
    roster_d_full = (target,) + fillers_d[1:]

    snap_c = build_team_snapshot(team_c, roster_c_full, (), CAP_SETTINGS)
    snap_d = build_team_snapshot(team_d, roster_d_full, (), CAP_SETTINGS)
    league_snapshot = build_league_snapshot(LEAGUE_ID, SEASON, (snap_c, snap_d))

    ctx = GmContext(
        league_id=LEAGUE_ID, season=SEASON, phase=Phase.REGULAR_SEASON,
        league_rng_seed=RNG_SEED, cap_settings=CAP_SETTINGS, next_season_cap_settings=CAP_SETTINGS,
        rookie_scale=ROOKIE_SCALE,
        room_by_team={c_id: 60_000_000, d_id: 60_000_000},
        apron1_room_by_team={c_id: 60_000_000, d_id: 60_000_000},
        philosophy_by_team={c_id: GmPhilosophy.TENDENCY_RESPECTER, d_id: GmPhilosophy.EGALITARIAN},
        window_by_team={c_id: CompetitiveWindow.RETOOL, d_id: CompetitiveWindow.CONTEND},
        tuning=DEFAULT_TUNING,
    )

    cand_cd = CandidatePackage(
        team_a_id=c_id, team_b_id=d_id,
        incoming_a=AssetBundle(players=(target,)),
        outgoing_a=AssetBundle(players=(sweetener,)),
    )

    def fake_propose(proposer_id, partner_id, snapshot, ctx, cache):
        if proposer_id == c_id and partner_id == d_id:
            return (cand_cd,)
        return ()

    with mock.patch.object(gm_service, "propose_packages", side_effect=fake_propose):
        result = gm_service.run_trade_window(league_snapshot, ctx, max_negotiation_rounds=2)

    assert len(result.executions) == 1
    row_cd = result.head_to_head[(c_id, d_id)]
    assert row_cd.executions == 1.0
    assert row_cd.far_reject_streak == 0.0

    # -- Advance to the next window's decision site: a fresh GmContext
    # carrying THIS row -------------------------------------------------
    next_window_ctx = dataclasses.replace(
        ctx, head_to_head={(c_id, d_id): dataclasses.replace(row_cd)},
    )

    target_count, h2h_row = gm_service._channel1_target_count(next_window_ctx, c_id, d_id)
    assert h2h_row is not None and h2h_row.executions == 1.0
    assert target_count == 3

    cache = ValuationCache()
    next_candidates = gm_service.propose_packages(
        c_id, d_id, league_snapshot, next_window_ctx, cache
    )
    # team C carries no future first-round pick in this fixture -- variant2
    # never exists, so 3 targets x 1 variant = 3 candidates exactly.
    assert len(next_candidates) == 3


# ---------------------------------------------------------------------------
# The applier, its algebra, and the reader-side no-op guarantees --
# §11 step 11's own "own verify" list.
# ---------------------------------------------------------------------------


def test_decay_applier_reproduces_the_stated_4_season_weight():
    """§7/§11 step 11: `0.6**4 = 0.1296` for a season-1 event read at
    season 5 (four applications of the decay applier)."""
    from dba2.domain.head_to_head import HeadToHead

    row = HeadToHead(
        league_id=LEAGUE_ID, team_id=A_ID, opponent_id=B_ID,
        opened=1.0, received=0.0, accepts_by_opponent=0.0, rejects_by_opponent=0.0,
        accepts_by_self=0.0, rejects_by_self=0.0, counters_given=0.0, counters_received=0.0,
        executions=0.0, refused_illegal=0.0, far_reject_streak=0.0,
        gap_by_opponent_sum=0.0, last_gap_by_opponent=None,
        last_season_interacted=SEASON, updated_season=SEASON,
    )
    for i in range(4):
        row = memory.decay_head_to_head(row, season=SEASON + i + 1)
    assert row.opened == pytest.approx(0.1296)
    assert row.updated_season == SEASON + 4


def test_decay_applier_leaves_identity_untouched_but_restamps_updated_season():
    """NIT 8 (round 13 correction): `updated_season` used to be left at
    whatever the row already carried, so a row untouched for several
    seasons kept a stale `updated_season` while its counters silently
    decayed underneath it. The decay applier now restamps `updated_season`
    to the harness's own new season index on every call -- asserted here
    against a season DIFFERENT from the row's own pre-existing value, so
    this test cannot pass by the restamped value coincidentally matching
    the input. `last_season_interacted` is a different fact (the last
    season a real write happened) and is deliberately left untouched."""
    from dba2.domain.head_to_head import HeadToHead

    row = HeadToHead(
        league_id=LEAGUE_ID, team_id=A_ID, opponent_id=B_ID,
        opened=4.0, received=1.0, accepts_by_opponent=1.0, rejects_by_opponent=3.0,
        accepts_by_self=0.0, rejects_by_self=1.0, counters_given=1.0, counters_received=1.0,
        executions=1.0, refused_illegal=1.0, far_reject_streak=4.0,
        gap_by_opponent_sum=100.0, last_gap_by_opponent=25.0,
        last_season_interacted=SEASON, updated_season=SEASON,
    )
    decayed = memory.decay_head_to_head(row, season=SEASON + 3)
    assert decayed.league_id == row.league_id
    assert decayed.team_id == row.team_id
    assert decayed.opponent_id == row.opponent_id
    assert decayed.last_season_interacted == row.last_season_interacted
    assert decayed.updated_season == SEASON + 3
    expected_last_gap = row.last_gap_by_opponent * memory.H2H_DECAY
    assert decayed.last_gap_by_opponent == pytest.approx(expected_last_gap)


def test_decay_applier_none_last_gap_by_opponent_stays_none():
    from dba2.domain.head_to_head import HeadToHead

    row = HeadToHead(
        league_id=LEAGUE_ID, team_id=A_ID, opponent_id=B_ID,
        opened=0.0, received=0.0, accepts_by_opponent=0.0, rejects_by_opponent=0.0,
        accepts_by_self=0.0, rejects_by_self=0.0, counters_given=0.0, counters_received=0.0,
        executions=0.0, refused_illegal=0.0, far_reject_streak=0.0,
        gap_by_opponent_sum=0.0, last_gap_by_opponent=None,
        last_season_interacted=None, updated_season=SEASON,
    )
    decayed = memory.decay_head_to_head(row, season=SEASON + 1)
    assert decayed.last_gap_by_opponent is None


def test_decay_head_to_head_rows_maps_over_the_whole_dict():
    from dba2.domain.head_to_head import HeadToHead

    rows = {
        (A_ID, B_ID): HeadToHead(
            league_id=LEAGUE_ID, team_id=A_ID, opponent_id=B_ID,
            opened=10.0, received=0.0, accepts_by_opponent=5.0, rejects_by_opponent=5.0,
            accepts_by_self=0.0, rejects_by_self=0.0, counters_given=0.0, counters_received=0.0,
            executions=0.0, refused_illegal=0.0, far_reject_streak=2.0,
            gap_by_opponent_sum=0.0, last_gap_by_opponent=None,
            last_season_interacted=SEASON, updated_season=SEASON,
        ),
    }
    decayed_rows = memory.decay_head_to_head_rows(rows, season=SEASON + 1)
    assert decayed_rows[(A_ID, B_ID)].opened == pytest.approx(6.0)
    assert decayed_rows[(A_ID, B_ID)].far_reject_streak == pytest.approx(1.2)
    assert decayed_rows[(A_ID, B_ID)].updated_season == SEASON + 1
    # Pure -- the input mapping's own row objects are untouched.
    assert rows[(A_ID, B_ID)].opened == 10.0
    assert rows[(A_ID, B_ID)].updated_season == SEASON


def test_decay_head_to_head_decays_exactly_the_fields_not_in_the_exclusion_set():
    """NIT 6 (round 13 correction): the decay applier derives its decayed-
    field list from `dataclasses.fields(HeadToHead)` minus a small,
    function-local exclusion set, rather than a hand-written list of 13
    keyword assignments. Asserted BEHAVIORALLY here (build a row with
    every field set to a distinct nonzero value, decay it once, and check
    every single field against `HeadToHead`'s own real field list) rather
    than by reading a private module constant directly -- the exclusion
    set is deliberately function-local, not module-level, so it never
    trips `test_constant_inventory.py`'s Decision P3-30 scanner (it is
    pure internal loop bookkeeping, never a tunable surface). This test
    still fails loudly if a future column addition is silently treated as
    bookkeeping or silently dropped -- it exercises every real field on
    the dataclass, not a hardcoded subset."""
    from dba2.domain.head_to_head import HeadToHead

    non_decaying = {
        "league_id", "team_id", "opponent_id", "last_season_interacted", "updated_season",
    }
    all_field_names = {f.name for f in dataclasses.fields(HeadToHead)}
    assert non_decaying <= all_field_names
    decaying_fields = all_field_names - non_decaying
    assert decaying_fields == {
        "opened", "received", "accepts_by_opponent", "rejects_by_opponent",
        "accepts_by_self", "rejects_by_self", "counters_given", "counters_received",
        "executions", "refused_illegal", "far_reject_streak", "gap_by_opponent_sum",
        "last_gap_by_opponent",
    }

    row = HeadToHead(
        league_id=LEAGUE_ID, team_id=A_ID, opponent_id=B_ID,
        opened=1.0, received=2.0, accepts_by_opponent=3.0, rejects_by_opponent=4.0,
        accepts_by_self=5.0, rejects_by_self=6.0, counters_given=7.0, counters_received=8.0,
        executions=9.0, refused_illegal=10.0, far_reject_streak=11.0,
        gap_by_opponent_sum=12.0, last_gap_by_opponent=13.0,
        last_season_interacted=SEASON, updated_season=SEASON,
    )
    decayed = memory.decay_head_to_head(row, season=SEASON + 1)
    for name in decaying_fields:
        assert getattr(decayed, name) == pytest.approx(getattr(row, name) * memory.H2H_DECAY), name
    assert decayed.league_id == row.league_id
    assert decayed.team_id == row.team_id
    assert decayed.opponent_id == row.opponent_id
    assert decayed.last_season_interacted == row.last_season_interacted
    assert decayed.updated_season == SEASON + 1


def test_decay_head_to_head_is_idempotent_under_a_replayed_season_boundary():
    """B2 (round 13 correction) -- a season-boundary hook that crashes and
    retries calls `decay_head_to_head_rows(state.head_to_head, season=N)`
    twice in a row against the SAME underlying rows, with the SAME `N`.
    Pre-fix, that compounded: `0.6` applied twice reads `0.36`, violating
    §10 Tier 4's explicit "the decay applier is idempotent under a
    replayed phase hook" requirement. Post-fix, the second call is a
    genuine no-op because `row.updated_season` already equals `season`
    from the first call."""
    from dba2.domain.head_to_head import HeadToHead

    row = HeadToHead(
        league_id=LEAGUE_ID, team_id=A_ID, opponent_id=B_ID,
        opened=1.0, received=0.0, accepts_by_opponent=0.0, rejects_by_opponent=1.0,
        accepts_by_self=0.0, rejects_by_self=0.0, counters_given=0.0, counters_received=0.0,
        executions=0.0, refused_illegal=0.0, far_reject_streak=1.0,
        gap_by_opponent_sum=10.0, last_gap_by_opponent=10.0,
        last_season_interacted=SEASON, updated_season=SEASON,
    )
    once = memory.decay_head_to_head(row, season=SEASON + 1)
    assert once.opened == pytest.approx(0.6)
    replayed = memory.decay_head_to_head(once, season=SEASON + 1)
    assert replayed.opened == pytest.approx(0.6), "a replayed call at the SAME season must no-op"
    assert replayed == once

    rows = {(A_ID, B_ID): row}
    once_rows = memory.decay_head_to_head_rows(rows, season=SEASON + 1)
    replayed_rows = memory.decay_head_to_head_rows(once_rows, season=SEASON + 1)
    assert replayed_rows[(A_ID, B_ID)].opened == pytest.approx(0.6)
    assert replayed_rows[(A_ID, B_ID)].far_reject_streak == pytest.approx(0.6)


def test_three_sample_sizes_at_season_5_reproduce_the_designs_own_worked_table():
    """§7's own worked table, HEAD-window legal-only event rates, both
    rows -- and the invariant round 3's B-4 error violated: the run's own
    `n_eff` strictly exceeds its own `Σw` at more than one season of
    decayed mass (at exactly one season the two are equal by construction
    -- there is nothing yet to average over)."""
    d = memory.H2H_DECAY
    n_seasons = 5

    def sigma_w(event_rate: float, decay: float, seasons: int) -> float:
        return event_rate * (1 - decay**seasons) / (1 - decay)

    def sigma_w2(event_rate: float, decay: float, seasons: int) -> float:
        return event_rate * (1 - decay ** (2 * seasons)) / (1 - decay**2)

    def n_eff(event_rate: float, decay: float, seasons: int) -> float:
        w = sigma_w(event_rate, decay, seasons)
        w2 = sigma_w2(event_rate, decay, seasons)
        return w * w / w2

    # packages (opened, the shrinkage input): E = 3.144 * 2 = 6.288
    e_packages = 3.144 * 2
    w_packages = sigma_w(e_packages, d, n_seasons)
    w2_packages = sigma_w2(e_packages, d, n_seasons)
    neff_packages = n_eff(e_packages, d, n_seasons)
    assert w_packages == pytest.approx(14.50, abs=0.01)
    assert w2_packages == pytest.approx(9.77, abs=0.01)
    assert neff_packages == pytest.approx(21.53, abs=0.01)
    assert neff_packages > w_packages

    # targets (the independent draw unit): E = 1.572 * 2 = 3.144
    e_targets = 1.572 * 2
    w_targets = sigma_w(e_targets, d, n_seasons)
    w2_targets = sigma_w2(e_targets, d, n_seasons)
    neff_targets = n_eff(e_targets, d, n_seasons)
    assert w_targets == pytest.approx(7.25, abs=0.01)
    assert w2_targets == pytest.approx(4.88, abs=0.01)
    assert neff_targets == pytest.approx(10.76, abs=0.01)
    assert neff_targets > w_targets

    # The invariant itself, decoupled from any specific event rate --
    # n_eff == Σw exactly at one season (nothing to average over yet),
    # and n_eff > Σw for every season count beyond that (round 3's own
    # B-4 error, guarded mechanically rather than re-derived by hand).
    for seasons in (1, 2, 3, 5, 10):
        w = sigma_w(1.0, d, seasons)
        neff = n_eff(1.0, d, seasons)
        if seasons == 1:
            assert neff == pytest.approx(w)
        else:
            assert neff > w


def test_p_bar_none_when_head_to_head_is_none_or_total_opened_is_zero():
    assert compute_h2h_base_accept_rate(None) is None
    from dba2.domain.head_to_head import HeadToHead

    zero_row = HeadToHead(
        league_id=LEAGUE_ID, team_id=A_ID, opponent_id=B_ID,
        opened=0.0, received=0.0, accepts_by_opponent=0.0, rejects_by_opponent=0.0,
        accepts_by_self=0.0, rejects_by_self=0.0, counters_given=0.0, counters_received=0.0,
        executions=0.0, refused_illegal=0.0, far_reject_streak=0.0,
        gap_by_opponent_sum=0.0, last_gap_by_opponent=None,
        last_season_interacted=None, updated_season=SEASON,
    )
    assert compute_h2h_base_accept_rate({(A_ID, B_ID): zero_row}) is None


def test_p_bar_is_the_live_directed_mean_over_every_row():
    from dba2.domain.head_to_head import HeadToHead

    rows = {
        (A_ID, B_ID): HeadToHead(
            league_id=LEAGUE_ID, team_id=A_ID, opponent_id=B_ID,
            opened=10.0, received=0.0, accepts_by_opponent=6.0, rejects_by_opponent=4.0,
            accepts_by_self=0.0, rejects_by_self=0.0, counters_given=0.0, counters_received=0.0,
            executions=0.0, refused_illegal=0.0, far_reject_streak=0.0,
            gap_by_opponent_sum=0.0, last_gap_by_opponent=None,
            last_season_interacted=SEASON, updated_season=SEASON,
        ),
        (B_ID, A_ID): HeadToHead(
            league_id=LEAGUE_ID, team_id=B_ID, opponent_id=A_ID,
            opened=20.0, received=0.0, accepts_by_opponent=10.0, rejects_by_opponent=10.0,
            accepts_by_self=0.0, rejects_by_self=0.0, counters_given=0.0, counters_received=0.0,
            executions=0.0, refused_illegal=0.0, far_reject_streak=0.0,
            gap_by_opponent_sum=0.0, last_gap_by_opponent=None,
            last_season_interacted=SEASON, updated_season=SEASON,
        ),
    }
    p_bar = compute_h2h_base_accept_rate(rows)
    assert p_bar == pytest.approx((6.0 + 10.0) / (10.0 + 20.0))


def test_decision_p3_35_illegal_termination_touches_only_refused_illegal_and_p_bar_excludes_it():
    """Decision P3-35, asserted directly through the real writer: an
    illegal-terminated negotiation increments `refused_illegal` and
    nothing else -- `opened`/`rejects_by_opponent`/`far_reject_streak` are
    untouched -- and the resulting `p_bar` is computed over the LEGAL
    population only, never diluted by the illegal one.

    This is a mechanical, unit-scoped demonstration of the contract, not a
    reproduction of the real 45-seed census figures (both-legal `0.7709`,
    all-packages `~0.63`) -- that comparison is a corpus-level measurement
    §11 step 11's own harness/validate_gm.py runs make, not something a
    fast unit test can regenerate; reproduced here as an inline
    illegal-vs-legal population contrast instead."""
    team_a, team_b, roster_a, star1, star2, fillers_b = _base_rosters()
    roster_b_16 = (star1, star2) + fillers_b + _filler_roster(B_ID, 1, base_id=2100)
    assert len(roster_b_16) == 16  # deliberately over roster_max=15

    snap_a = build_team_snapshot(team_a, roster_a, (), CAP_SETTINGS)
    snap_b_illegal = build_team_snapshot(team_b, roster_b_16, (), CAP_SETTINGS)
    league_illegal = build_league_snapshot(LEAGUE_ID, SEASON, (snap_a, snap_b_illegal))
    ctx = _ctx_for(league_illegal)
    cache = ValuationCache()
    psr = cache.pick_slot_ranks(league_illegal, ctx.tuning)
    all_players = (*snap_a.roster, *snap_b_illegal.roster)
    fv_a = cache.fit_values(snap_a, all_players, ctx)
    fv_b = cache.fit_values(snap_b_illegal, all_players, ctx)
    partner_b = _partner_terms(B_ID, ctx, snap_b_illegal, fv_b)

    # A 2-for-1 trade: A gives up 2 players, receiving 1 -- pushes B's
    # roster from 16 to 17 (illegal:roster_max), a legality failure wholly
    # unrelated to either side's merit read.
    cand_illegal = CandidatePackage(
        team_a_id=A_ID, team_b_id=B_ID,
        incoming_a=AssetBundle(players=(star1,)),
        outgoing_a=AssetBundle(players=(roster_a[0], roster_a[1])),
    )
    tally = gm_service._NegotiationTally()
    h2h = gm_service._H2HAccumulator(league_id=LEAGUE_ID, season=SEASON)
    result_illegal = gm_service._run_negotiation(
        cand_illegal, a_id=A_ID, b_id=B_ID, team_a=snap_a, team_b=snap_b_illegal,
        partner=partner_b, fv_a_by_player_id=fv_a, incoming_pick_value=0.0,
        snapshot=league_illegal, ctx=ctx, pick_slot_ranks=psr,
        max_negotiation_rounds=2, tally=tally, h2h=h2h,
    )
    last = result_illegal.turns[-1]
    assert last.record_b.verdict.startswith("illegal:")
    assert last.stance == NegotiationStance.REJECT_ILLEGAL

    row_ab = h2h.rows[(A_ID, B_ID)]
    assert row_ab.refused_illegal == 1.0
    assert row_ab.opened == 0.0
    assert row_ab.rejects_by_opponent == 0.0
    assert row_ab.far_reject_streak == 0.0

    # A second, LEGAL negotiation on the same pair -- a plain roster_a[2]
    # given away for a modest B filler, evaluated for real. Whatever its
    # own verdict, it is legal, so it must increment `opened` and (since
    # `_evaluate_a` there sees a real accept/reject) leave `refused_
    # illegal` untouched at 1 -- the two counters never share an event.
    a_give = roster_a[2]
    b_receive = fillers_b[0]
    snap_b_15 = build_team_snapshot(team_b, (star1, star2) + fillers_b, (), CAP_SETTINGS)
    league_15 = build_league_snapshot(LEAGUE_ID, SEASON, (snap_a, snap_b_15))
    cache2 = ValuationCache()
    psr2 = cache2.pick_slot_ranks(league_15, ctx.tuning)
    all_players2 = (*snap_a.roster, *snap_b_15.roster)
    fv_a2 = cache2.fit_values(snap_a, all_players2, ctx)
    fv_b2 = cache2.fit_values(snap_b_15, all_players2, ctx)
    partner_b2 = _partner_terms(B_ID, ctx, snap_b_15, fv_b2)
    cand_legal = CandidatePackage(
        team_a_id=A_ID, team_b_id=B_ID,
        incoming_a=AssetBundle(players=(b_receive,)),
        outgoing_a=AssetBundle(players=(a_give,)),
    )
    result_legal = gm_service._run_negotiation(
        cand_legal, a_id=A_ID, b_id=B_ID, team_a=snap_a, team_b=snap_b_15,
        partner=partner_b2, fv_a_by_player_id=fv_a2, incoming_pick_value=0.0,
        snapshot=league_15, ctx=ctx, pick_slot_ranks=psr2,
        max_negotiation_rounds=2, tally=tally, h2h=h2h,
    )
    last_legal = result_legal.turns[-1]
    assert not last_legal.record_a.verdict.startswith("illegal:")
    assert not last_legal.record_b.verdict.startswith("illegal:")

    assert row_ab.refused_illegal == 1.0, "the legal negotiation must not touch refused_illegal"
    assert row_ab.opened == 1.0, "the illegal negotiation must not touch opened"

    # p_bar over this tiny two-row population reads only the LEGAL
    # opened/accepts -- the illegal termination contributes to neither
    # numerator nor denominator (Decision P3-35's "zero information about
    # the opponent's disposition").
    p_bar = compute_h2h_base_accept_rate(h2h.rows)
    expected = row_ab.accepts_by_opponent / row_ab.opened
    assert p_bar == pytest.approx(expected)


# ---------------------------------------------------------------------------
# GmContext.head_to_head = None is a strict no-op on every existing output.
# ---------------------------------------------------------------------------


def test_head_to_head_none_reproduces_every_pre_existing_field_byte_identically():
    """§11 step 11's own verify: `GmContext.head_to_head = None` must not
    perturb a single pre-existing `TradeWindowResult` field -- the writer
    is unconditional (it seeds from `ctx.head_to_head or {}` either way),
    but nothing before this step reads the field, so passing `None`
    instead of an already-loaded (possibly empty) mapping must not change
    any negotiation OUTCOME. Uses this module's own two-team fixture
    (deterministic, no RNG-dependent branch) rather than a full 45-seed
    corpus rerun -- the property under test (nothing reads `ctx.head_to_
    head` yet) does not need one."""
    team_a, team_b, roster_a, star1, star2, fillers_b = _base_rosters()
    snap_a = build_team_snapshot(team_a, roster_a, (), CAP_SETTINGS)
    snap_b = build_team_snapshot(team_b, (star1, star2) + fillers_b, (), CAP_SETTINGS)
    league_snapshot = build_league_snapshot(LEAGUE_ID, SEASON, (snap_a, snap_b))
    ctx_with_none = _ctx_for(league_snapshot)
    assert ctx_with_none.head_to_head is None
    assert ctx_with_none.h2h_base_accept_rate is None

    ctx_with_empty = dataclasses.replace(ctx_with_none, head_to_head={}, h2h_base_accept_rate=None)

    result_none = gm_service.run_trade_window(
        league_snapshot, ctx_with_none, max_negotiation_rounds=2
    )
    result_empty = gm_service.run_trade_window(
        league_snapshot, ctx_with_empty, max_negotiation_rounds=2
    )

    assert result_none.records == result_empty.records
    assert result_none.executions == result_empty.executions
    assert result_none.packages_evaluated == result_empty.packages_evaluated
    assert result_none.outcome_census == result_empty.outcome_census
    assert result_none.head_to_head == result_empty.head_to_head


def test_channel1_real_non_vacuous_zero_at_head_to_head_none():
    """§11 step 17's own version of step 13's "this step, not the M29
    control pass, is where the channel's non-vacuous zero-reading has to
    come from." **Channel 1's true `head_to_head = None` zero is NOT
    provable on `validate_gm.py`'s own M29 control run**, discovered
    during this step's build: that control run's `state.head_to_head`
    accumulates real `far_reject_streak`/`executions` data across its own
    seasons regardless of `max_negotiation_rounds` (`_record_negotiation_
    in_h2h`'s writer is unconditional, §11 step 11), and channel 1's
    decision site sits UPSTREAM of `respond()`'s own `rounds_remaining <=
    0` short-circuit -- unlike channels 2/3, which that short-circuit
    shields from ever consulting a shaded band at `max_negotiation_rounds
    = 0`. So a real M29-shaped control run measurably produces NONZERO
    channel-1 flips (confirmed empirically against the committed Decision
    P3-29(b) baseline seed during this step's build), not the `0` §9.2's
    own three-channel control-pass table assumed. This test establishes
    channel 1's real, non-vacuous zero the same way channel 2's is
    established -- a DEDICATED pass, `head_to_head` explicitly `None`/
    empty, sites definitely positive, flips definitely `0` -- never from
    the M29 corpus run, whose own `extra_pass` conjunct excludes channel 1
    for exactly this reason (see `compute_m28`'s own docstring)."""
    team_a, team_b, roster_a, star1, star2, fillers_b = _base_rosters()
    snap_a = build_team_snapshot(team_a, roster_a, (), CAP_SETTINGS)
    snap_b = build_team_snapshot(team_b, (star1, star2) + fillers_b, (), CAP_SETTINGS)
    league_snapshot = build_league_snapshot(LEAGUE_ID, SEASON, (snap_a, snap_b))
    ctx_with_none = _ctx_for(league_snapshot)
    assert ctx_with_none.head_to_head is None

    result = gm_service.run_trade_window(league_snapshot, ctx_with_none, max_negotiation_rounds=2)

    assert result.channel1_up_decision_sites > 0
    assert result.channel1_down_decision_sites > 0
    assert result.channel1_up_flips == 0
    assert result.channel1_down_flips == 0


def test_far_rejects_observed_survives_a_disabled_h2h_writer_item_2_dead_writer_regression(
    monkeypatch,
):
    """§11 step 17 item 2's own dead-writer regression test -- proves
    `far_rejects_observed` is built at the RIGHT call site (`_run_
    negotiation`'s own, independent of `_record_negotiation_in_h2h`), not
    inside the writer itself. `_record_negotiation_in_h2h` is monkeypatched
    to a full no-op -- the writer is disabled/bypassed -- and the real
    negotiation path (`gm_service.run_trade_window`) is still driven
    through fixture 1's own far-reject roster construction (four real
    far-merit-reject negotiations, A -> B).

    **The two readings must diverge, and that divergence IS the proof.**
    `far_rejects_observed` (this window's tally, incremented at `_run_
    negotiation`'s own call site regardless of what the writer does) must
    read nonzero -- the real far-reject events genuinely occurred. A
    SECOND window, built against a `ctx` that merges window 1's own
    (writer-disabled, therefore untouched) `head_to_head` -- exactly the
    `state.head_to_head.update(...)` shape `tools/gm_harness/season.py`
    uses between windows -- must read `channel1_down_max_observed == 0.0`,
    because `far_reject_streak` was never actually written by the
    disabled writer. If `far_rejects_observed` were instead derived FROM
    the writer's own output, it would read zero here too, exactly the
    vacuous-triage bug Decision P3-41 exists to catch."""
    team_a, team_b, roster_a, star1, star2, fillers_b = _base_rosters()

    sweetener5 = make_roster_player(
        1500, age=34, season=SEASON, overall_cached=79.64089305787854, team_id=A_ID,
        primary_position=Position.C, peak_age_start=27, peak_age_end=31,
        contract=make_contract(1500, team_id=A_ID, start_season=SEASON, salaries=(3_000_000,)),
    )
    target5 = make_roster_player(
        2500, age=25, season=SEASON, overall_cached=76.57522051344074, team_id=B_ID,
        primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(2500, team_id=B_ID, start_season=SEASON, salaries=(8_000_000,)),
    )
    pick_a = _make_pick(9001, SEASON + 1, A_ID, pick_number=20)
    roster_a_full = (sweetener5,) + roster_a[1:]
    roster_b_full = (star1, star2, target5) + fillers_b[:-1]

    snap_a = build_team_snapshot(team_a, roster_a_full, (pick_a,), CAP_SETTINGS)
    snap_b = build_team_snapshot(team_b, roster_b_full, (), CAP_SETTINGS)
    league_snapshot = build_league_snapshot(LEAGUE_ID, SEASON, (snap_a, snap_b))
    ctx = _ctx_for(league_snapshot)

    # The writer disabled/bypassed -- a full no-op, the worst case this
    # regression test exists to catch.
    monkeypatch.setattr(gm_service, "_record_negotiation_in_h2h", lambda *a, **k: None)

    result1 = gm_service.run_trade_window(league_snapshot, ctx, max_negotiation_rounds=3)

    assert result1.far_rejects_observed >= 1, (
        "far_rejects_observed must be nonzero -- the four real far-merit-reject "
        "negotiations genuinely occurred this window, independently of the writer"
    )
    # The writer was truly disabled: no row exists (or exists at its
    # freshly-initialized 0.0) for (A, B) in this window's own written output.
    row_ab_after_disabled_writer = result1.head_to_head.get((A_ID, B_ID))
    assert (
        row_ab_after_disabled_writer is None
        or row_ab_after_disabled_writer.far_reject_streak == 0.0
    )

    # A second window, built against a ctx that merges window 1's own
    # (writer-disabled) head_to_head -- the exact shape season.py's own
    # `state.head_to_head.update(trade_result.head_to_head)` -> `build_ctx`
    # produces between windows.
    ctx2 = dataclasses.replace(ctx, head_to_head=dict(result1.head_to_head))
    result2 = gm_service.run_trade_window(league_snapshot, ctx2, max_negotiation_rounds=3)

    assert result2.channel1_down_max_observed == 0.0, (
        "channel1_down_max_observed must read 0.0 here -- far_reject_streak was never "
        "actually written by the disabled writer, so a SUBSEQUENT window's own real "
        "reader sees nothing. This is the divergence from far_rejects_observed (which "
        "read nonzero above) that the triage gate exists to catch."
    )


def test_run_trade_window_is_pure_same_ctx_twice_is_a_real_but_narrow_guarantee():
    """B2 (round 13 correction) -- this is the REAL, narrow guarantee this
    build actually provides: `run_trade_window` is a pure function of its
    arguments. Calling it twice against the exact same, never-mutated
    `ctx` object produces byte-identical results, and `ctx` itself is
    never mutated. **This is NOT "idempotent replay" in the sense a
    crash-and-retry phase hook needs** -- see the test immediately below,
    which drives the REALISTIC replay shape (a `ctx` rebuilt from a store
    that already merged window 1's own result, exactly as `tools/gm_
    harness/season.py` does between windows) and shows it genuinely
    double-counts. The previous version of this test asserted the same
    thing this one does, under a docstring that overclaimed "idempotent
    replay" -- fixed here to name the guarantee it actually checks."""
    team_a, team_b, roster_a, star1, star2, fillers_b = _base_rosters()
    snap_a = build_team_snapshot(team_a, roster_a, (), CAP_SETTINGS)
    snap_b = build_team_snapshot(team_b, (star1, star2) + fillers_b, (), CAP_SETTINGS)
    league_snapshot = build_league_snapshot(LEAGUE_ID, SEASON, (snap_a, snap_b))
    ctx = _ctx_for(league_snapshot)

    result_1 = gm_service.run_trade_window(league_snapshot, ctx, max_negotiation_rounds=2)
    result_2 = gm_service.run_trade_window(league_snapshot, ctx, max_negotiation_rounds=2)

    assert result_1.head_to_head == result_2.head_to_head
    assert ctx.head_to_head is None, "run_trade_window must never mutate its own ctx"


def test_channel3_flip_counter_is_two_sided_and_null_elsewhere_via_a_controlled_probe():
    """Decision P3-36's ch3 flip counter (§9.2/§11 step 12's own verify),
    driven through the REAL `_run_negotiation` loop -- `_run_negotiation`'s
    own synthetic-record substitution (the mechanism that hands channel
    3's shaded band to `respond()`'s real decision, see that call site's
    own comment for why it is a record substitution rather than a new
    `respond()` parameter) AND `gm_service`'s own flip/reachability/site
    tally arithmetic are both live -- with ONLY `_evaluate_both_sides`
    monkeypatched to return fully-controlled `DecisionRecord`s. `memory.
    shade` itself is NOT patched: a real, controlled `HeadToHead` row and
    a real `h2h_base_accept_rate=0.5` (the one point where §8.1's
    asymmetric ceiling is symmetric, `+-1.0`, so both the widening and
    narrowing arms are reachable from the same probe) drive the real
    shade formula.

    **Why a controlled probe rather than a real trade candidate.**
    `H2H_BAND_SHADE=0.25` opens only a +-25% window around `band_base`.
    Measured directly against this module's own fixture-1 far-reject
    candidates (4 real packages, `max_negotiation_rounds=3`): every one
    misses that window by ~5x (`gap / band_base` ratios of 5.26-5.74),
    so no real asset search this module's budget can afford would
    reliably land inside a window that narrow. `max_negotiation_rounds=0`
    keeps every probe at round 0 and guarantees no `COUNTER` is ever
    attempted (§4.2's own ladder reaches `EXPIRE` or `REJECT_TOO_FAR`
    before the axis check at `rounds_remaining<=0`), so `build_counter`
    is never invoked and `team_a`/`team_b`/`partner`/etc. below are
    never read for real -- only `_evaluate_both_sides`'s mocked return
    value drives the outcome."""
    threshold = 100.0
    band_base = COUNTER_BAND_MULT * threshold  # 300.0
    ctx_base = _ctx_for(None)

    def _probe(gap: float, h2h_row):
        # Ask-more axis: A accepts, B is short/acting -- shade is read off
        # B's own memory of A, `ctx.head_to_head[(B_ID, A_ID)]`.
        record_a = _dummy_decision_record(
            A_ID, verdict="accept", score=1_000_000.0, threshold=threshold,
        )
        record_b = _dummy_decision_record(
            B_ID, verdict="reject", score=threshold + gap, threshold=threshold,
        )
        ctx = dataclasses.replace(
            ctx_base,
            head_to_head={(B_ID, A_ID): h2h_row} if h2h_row is not None else {},
            h2h_base_accept_rate=0.5,
        )
        package = CandidatePackage(
            team_a_id=A_ID, team_b_id=B_ID, incoming_a=AssetBundle(), outgoing_a=AssetBundle(),
        )
        tally = gm_service._NegotiationTally()
        h2h = gm_service._H2HAccumulator(league_id=LEAGUE_ID, season=SEASON)
        with mock.patch.object(
            gm_service, "_evaluate_both_sides", return_value=(record_a, record_b),
        ):
            result = gm_service._run_negotiation(
                package, a_id=A_ID, b_id=B_ID, team_a=None, team_b=None,
                partner=None, fv_a_by_player_id={}, incoming_pick_value=0.0,
                snapshot=None, ctx=ctx, pick_slot_ranks=None,
                max_negotiation_rounds=0, tally=tally, h2h=h2h,
            )
        return tally, result

    from dba2.domain.head_to_head import HeadToHead

    def _row(opened: float, accepts_by_opponent: float) -> HeadToHead:
        return HeadToHead(
            league_id=LEAGUE_ID, team_id=B_ID, opponent_id=A_ID,
            opened=opened, received=0.0, accepts_by_opponent=accepts_by_opponent,
            rejects_by_opponent=opened - accepts_by_opponent, accepts_by_self=0.0,
            rejects_by_self=0.0, counters_given=0.0, counters_received=0.0, executions=0.0,
            refused_illegal=0.0, far_reject_streak=0.0, gap_by_opponent_sum=0.0,
            last_gap_by_opponent=None, last_season_interacted=SEASON, updated_season=SEASON,
        )

    # p_hat=1.0 -> shade ~ +0.996
    row_positive_shade = _row(opened=1000.0, accepts_by_opponent=1000.0)
    # p_hat=0.0 -> shade ~ -0.996
    row_negative_shade = _row(opened=1000.0, accepts_by_opponent=0.0)

    # -- Widening flip: band_base says far, the widened band says not --
    gap_widen_zone = -1.1 * band_base  # 330.0 -- inside [300, ~374.7]
    tally, negotiation_result = _probe(gap_widen_zone, row_positive_shade)
    assert tally.channel3_decision_sites == 1
    assert tally.channel3_reachability == 1
    assert tally.channel3_flips == 1, "shade widening must flip band_base's far-reject verdict"
    assert negotiation_result.turns[-1].stance != NegotiationStance.REJECT_TOO_FAR, (
        "the REAL respond() decision, not just the flip counter, must reflect the wider band"
    )

    # -- Narrowing flip: band_base says NOT far, the narrowed band says far --
    gap_narrow_zone = -0.9 * band_base  # 270.0 -- inside [~225.3, 300]
    tally, negotiation_result = _probe(gap_narrow_zone, row_negative_shade)
    assert tally.channel3_flips == 1, "shade narrowing must flip band_base's in-band verdict"
    assert negotiation_result.turns[-1].stance == NegotiationStance.REJECT_TOO_FAR, (
        "the REAL respond() decision must become a far reject once the band narrows past this gap"
    )

    # -- No flip: comfortably inside both the widened and narrowed bands --
    gap_inside_both = -0.5 * band_base  # 150.0
    for row in (row_positive_shade, row_negative_shade):
        tally, _ = _probe(gap_inside_both, row)
        assert tally.channel3_flips == 0
        assert tally.channel3_decision_sites == 1

    # -- No flip: comfortably outside both the widened and narrowed bands --
    gap_outside_both = -2.0 * band_base  # 600.0
    for row in (row_positive_shade, row_negative_shade):
        tally, _ = _probe(gap_outside_both, row)
        assert tally.channel3_flips == 0

    # -- No memory at all -- `shade == 0.0`, `band_eff == band_base`, never
    # a flip regardless of `gap` (the "no memory -> unshaded" invariant,
    # asserted here at the flip-counter level specifically).
    for gap in (gap_widen_zone, gap_narrow_zone, gap_inside_both, gap_outside_both):
        tally, _ = _probe(gap, None)
        assert tally.channel3_flips == 0
        assert tally.channel3_reachability == 0
        assert tally.channel3_decision_sites == 1
        # `channel3_abs_shade_sum` is no longer accumulated on `_Negotiation
        # Tally` at all (B3 fix) -- its population is the head-to-head ROWS
        # (`ctx.head_to_head`), computed once per window by `_channel3_mean_
        # abs_shade_inputs`, not per decision site here. See `test_channel3_
        # mean_abs_shade_population_is_head_to_head_rows_not_decision_sites`,
        # below, for that function's own direct coverage.


def test_channel2_self_margin_reuses_channel3s_own_shade_and_actor_record(monkeypatch):
    """§11 step 13 (§8.3) -- `self_margin` is computed at the SAME `_run_
    negotiation` call site, from the SAME `shade`/`actor_record` channel 3
    already reads (never a second, independent `memory.shade` call), and
    is passed into a REAL `build_counter` call -- never `max_negotiation_
    rounds=0`, which would keep channel 2 vacuous (§11 step 13's own note:
    the control pass's own zero is vacuous; this step's real zero has to
    come from a counters-on pass instead). `gm_service.build_counter` is
    spied (the real function still runs, real search, real emitted
    counter) so only the `self_margin` value it was called with is
    captured for the assertion, never the search itself replaced."""
    team_a, team_b, roster_a, star1, star2, fillers_b = _base_rosters()
    sweetener_ib = make_roster_player(
        1600, age=26, season=SEASON, overall_cached=62.5, team_id=A_ID,
        primary_position=Position.C, peak_age_start=27, peak_age_end=31,
        contract=make_contract(1600, team_id=A_ID, start_season=SEASON, salaries=(2_000_000,)),
    )
    target_ib = make_roster_player(
        2600, age=27, season=SEASON, overall_cached=64.0, team_id=B_ID,
        primary_position=Position.PG, peak_age_start=27, peak_age_end=31,
        contract=make_contract(2600, team_id=B_ID, start_season=SEASON, salaries=(2_000_000,)),
    )
    roster_a_ib = roster_a[:5] + (sweetener_ib,) + roster_a[6:]
    roster_b_ib = (star1, star2, target_ib) + fillers_b[:-1]
    snap_a_ib = build_team_snapshot(team_a, roster_a_ib, (), CAP_SETTINGS)
    snap_b_ib = build_team_snapshot(team_b, roster_b_ib, (), CAP_SETTINGS)
    league_ib = build_league_snapshot(LEAGUE_ID, SEASON, (snap_a_ib, snap_b_ib))
    cache_ib = ValuationCache()
    psr_ib = cache_ib.pick_slot_ranks(league_ib, DEFAULT_TUNING)
    all_players_ib = (*snap_a_ib.roster, *snap_b_ib.roster)
    fv_a_ib = cache_ib.fit_values(snap_a_ib, all_players_ib, _ctx_for(league_ib))
    fv_b_ib = cache_ib.fit_values(snap_b_ib, all_players_ib, _ctx_for(league_ib))
    partner_b_ib = _partner_terms(B_ID, _ctx_for(league_ib), snap_b_ib, fv_b_ib)
    cand_ib = CandidatePackage(
        team_a_id=A_ID, team_b_id=B_ID,
        incoming_a=AssetBundle(players=(target_ib,)),
        outgoing_a=AssetBundle(players=(sweetener_ib,)),
    )

    # A real, controlled head-to-head row on BOTH directions (identical
    # opened/accepts) -- so whichever side turns out to be the short/
    # acting side on this candidate, its own memory lookup finds a row
    # with a known, positive `shade` (p_hat=0.75 against p_bar=0.5).
    from dba2.domain.head_to_head import HeadToHead

    def _row(team_id: int, opponent_id: int) -> HeadToHead:
        return HeadToHead(
            league_id=LEAGUE_ID, team_id=team_id, opponent_id=opponent_id,
            opened=8.0, received=0.0, accepts_by_opponent=6.0, rejects_by_opponent=2.0,
            accepts_by_self=0.0, rejects_by_self=0.0, counters_given=0.0, counters_received=0.0,
            executions=0.0, refused_illegal=0.0, far_reject_streak=0.0, gap_by_opponent_sum=0.0,
            last_gap_by_opponent=None, last_season_interacted=SEASON, updated_season=SEASON,
        )

    ctx = dataclasses.replace(
        _ctx_for(league_ib),
        head_to_head={(A_ID, B_ID): _row(A_ID, B_ID), (B_ID, A_ID): _row(B_ID, A_ID)},
        h2h_base_accept_rate=0.5,
    )
    expected_shade = memory.shade(ctx.head_to_head[(A_ID, B_ID)], ctx.h2h_base_accept_rate)
    assert expected_shade > 0.0, "fixture sanity: p_hat=0.75 against p_bar=0.5 must be positive"
    # Both directions carry the identical row, so the shade is the same
    # regardless of which side ends up short -- confirmed directly rather
    # than assumed.
    assert expected_shade == memory.shade(
        ctx.head_to_head[(B_ID, A_ID)], ctx.h2h_base_accept_rate
    )

    real_build_counter = gm_service.build_counter
    captured_self_margins: list[float] = []
    captured_records: list[tuple[DecisionRecord, DecisionRecord, str]] = []

    def _spy_build_counter(package, record_a, record_b, axis, **kwargs):
        captured_self_margins.append(kwargs.get("self_margin", 0.0))
        captured_records.append((record_a, record_b, axis))
        return real_build_counter(package, record_a, record_b, axis, **kwargs)

    monkeypatch.setattr(gm_service, "build_counter", _spy_build_counter)

    tally = gm_service._NegotiationTally()
    h2h = gm_service._H2HAccumulator(league_id=LEAGUE_ID, season=SEASON)
    result_ib = gm_service._run_negotiation(
        cand_ib, a_id=A_ID, b_id=B_ID, team_a=snap_a_ib, team_b=snap_b_ib,
        partner=partner_b_ib, fv_a_by_player_id=fv_a_ib, incoming_pick_value=0.0,
        snapshot=league_ib, ctx=ctx, pick_slot_ranks=psr_ib,
        max_negotiation_rounds=3, tally=tally, h2h=h2h,
    )
    assert any(t.stance == NegotiationStance.COUNTER for t in result_ib.turns), (
        "fixture construction error -- this candidate must reach a COUNTER stance"
    )
    assert len(captured_self_margins) >= 1, "build_counter must have been called for real"

    for self_margin_used, (record_a, record_b, axis) in zip(
        captured_self_margins, captured_records, strict=True
    ):
        actor_record = record_a if axis == "give_less" else record_b
        expected_self_margin = (
            gm_service.negotiation.H2H_MARGIN_SHADE * expected_shade * actor_record.threshold
        )
        assert self_margin_used == pytest.approx(expected_self_margin)
        assert 0.0 <= self_margin_used <= 0.25 * actor_record.threshold

    # M28 ch2's tally, updated once per real build_counter call this window.
    assert tally.channel2_decision_sites == len(captured_self_margins)
    assert tally.channel2_reachability == len(captured_self_margins), (
        "shade > 0.0 at every captured call -- every one of them must count as reachable"
    )


def test_is_stalled_reads_the_unshaded_band_not_channel3s_band_eff():
    """B2 fix (critic round) -- channel 3 (Decision P3-26/§8.2, step 12) is
    scoped to exactly one decision, "the band decides whether a counter is
    attempted" -- it must NOT also reach `is_stalled`'s stall-bar
    computation, a second, undisclosed decision point. `is_stalled`'s own
    contract (`negotiation.py` ~419-423) says `band` is "the acting side's
    own `counter_band(actor_record)` at the CURRENT turn" -- the unshaded
    one.

    **Mutation-verify, isolated from channel 3's own legitimate effect on
    `respond()`.** A naive with/without-shade A/B comparison across a real
    corpus is contaminated: shading also (correctly) changes whether
    round 0 itself resolves `REJECT_TOO_FAR` vs. `COUNTER`, so a different
    *population* of negotiations reaches round 1 in each arm, which can
    move `expired_stalled_count` for reasons having nothing to do with
    `is_stalled`. This test holds round 0's outcome IDENTICAL (the round-0
    gap sits deep inside both the unshaded and the widened band, so
    `respond()` reaches `COUNTER` either way) and engineers round 1's own
    partial concession to sit in the narrow zone between the two
    candidate stall thresholds: `MIN_CONCESSION_FRAC * band = 30.0`
    (unshaded, `threshold=100`, `band=300`) vs. `MIN_CONCESSION_FRAC *
    band_eff ~= 37.5` (shaded, `shade ~= +1` via an extreme, real
    `HeadToHead` row against `h2h_base_accept_rate=0.5`). A `33.75`-VP
    concession reads NOT STALLED against the real, unshaded threshold
    (`33.75 >= 30`) and STALLED against the shaded one (`33.75 < 37.5`) --
    reverting `gm_service.py`'s fix (passing `band_eff` to `is_stalled`
    again) flips this test's own outcome from `REJECT_TOO_FAR`/`expired_
    stalled=0` to `EXPIRE_STALLED`/`expired_stalled=1`."""
    from dba2.services.gm_service import NegotiationSituation

    threshold = 100.0

    # Ask-more axis (B short) -- record_a always "accept" with empty terms
    # keeps `_out_player_count(record_a) == 0`, so `axis_available` is
    # trivially true on this axis regardless of round (mirrors the
    # existing channel-3 flip probe's own convention, above).
    record_a0 = _dummy_decision_record(A_ID, verdict="accept", score=1_000_000.0, threshold=0.0)
    # Round 0's gap: -150.0 -- deep inside BOTH the unshaded band (300) and
    # the widened one (~374.99988 at shade ~= +1), so round 0 resolves
    # COUNTER identically regardless of shading; not the boundary this
    # test is isolating.
    record_b0 = _dummy_decision_record(
        B_ID, verdict="reject", score=threshold - 150.0, threshold=threshold,
    )

    # Round 1's records (what `build_counter`/`evaluate_trade` return) --
    # B's own gap moves from -150.0 to -116.25, a 33.75-VP concession.
    record_a1 = _dummy_decision_record(A_ID, verdict="accept", score=1_000_000.0, threshold=0.0)
    record_b1 = _dummy_decision_record(
        B_ID, verdict="reject", score=threshold - 116.25, threshold=threshold,
    )

    package = CandidatePackage(
        team_a_id=A_ID, team_b_id=B_ID, incoming_a=AssetBundle(), outgoing_a=AssetBundle(),
    )
    # An extreme, real `HeadToHead` row for B's own memory of A
    # (`ctx.head_to_head[(B_ID, A_ID)]`, the ask-more axis's own acting
    # side) against a symmetric `h2h_base_accept_rate=0.5` -- `p_hat=1.0`
    # against `p_bar=0.5` clamps `z` to `+1.0`, and `n=1e7` shrinks
    # `n/(n+H2H_SHADE_PRIOR)` to within `4e-7` of `1.0`, giving `shade`
    # indistinguishable from `+1.0` for this test's own arithmetic margins.
    from dba2.domain.head_to_head import HeadToHead

    h2h_row = HeadToHead(
        league_id=LEAGUE_ID, team_id=B_ID, opponent_id=A_ID,
        opened=1.0e7, received=0.0, accepts_by_opponent=1.0e7,
        rejects_by_opponent=0.0, accepts_by_self=0.0, rejects_by_self=0.0,
        counters_given=0.0, counters_received=0.0, executions=0.0,
        refused_illegal=0.0, far_reject_streak=0.0, gap_by_opponent_sum=0.0,
        last_gap_by_opponent=None, last_season_interacted=SEASON, updated_season=SEASON,
    )
    ctx = dataclasses.replace(
        _ctx_for(None), head_to_head={(B_ID, A_ID): h2h_row}, h2h_base_accept_rate=0.5,
    )
    shade = memory.shade(h2h_row, ctx.h2h_base_accept_rate)
    assert shade == pytest.approx(1.0, abs=1e-5), (
        "fixture must reach shade ~= +1 or this test proves nothing"
    )
    band_eff = counter_band(record_b0) * (1 + 0.25 * shade)
    assert 30.0 < 33.75 < 0.10 * band_eff, (
        "the 33.75 VP concession must sit strictly between the unshaded (30.0) and shaded "
        f"(~{0.10 * band_eff:.4f}) stall thresholds, or this test cannot distinguish the two"
    )

    tally = gm_service._NegotiationTally()
    h2h_acc = gm_service._H2HAccumulator(league_id=LEAGUE_ID, season=SEASON)

    with mock.patch.object(
        gm_service, "_evaluate_both_sides", return_value=(record_a0, record_b0),
    ), mock.patch.object(
        gm_service, "build_counter", side_effect=[(package, record_a1), None],
    ), mock.patch.object(
        gm_service.trade_decisions, "evaluate_trade", return_value=record_b1,
    ):
        result = gm_service._run_negotiation(
            package, a_id=A_ID, b_id=B_ID, team_a=None, team_b=None,
            partner=None, fv_a_by_player_id={}, incoming_pick_value=0.0,
            snapshot=None, ctx=ctx, pick_slot_ranks=None,
            max_negotiation_rounds=2, tally=tally, h2h=h2h_acc,
        )

    assert tally.expired_stalled == 0, (
        "is_stalled must read the real, UNSHADED band -- a 33.75 VP concession clears the "
        "real 30.0 VP bar and must NOT stall (reverting to band_eff's ~37.5 VP bar would "
        "stall this negotiation instead)"
    )
    assert result.turns[-1].situation == NegotiationSituation.REJECT_TOO_FAR, (
        "round 1 must reach the real repair attempt (build_counter's own second call, "
        "mocked to return None -- REJECT_TOO_FAR), not EXPIRE_STALLED"
    )


def test_channel3_synthetic_record_violates_reconstruction_identity_when_shaded():
    """B5 fix (critic round) -- `gm_service._substitute_shaded_record`
    (channel 3's synthetic-record substitution, Decision P3-26/§8.2, step
    12) shifts `score`/`threshold` but does NOT rescale `terms`, so `Σ
    terms == score` (`explain.py::DecisionRecord.reconstructs()`) is
    VIOLATED on the returned record whenever `band_eff != band`. Asserted
    mechanically here, per this dispatch's own instruction, rather than
    left to a comment nobody re-checks. Uses REAL, non-empty `terms` that
    genuinely sum to `score` (unlike this file's own `_dummy_decision_
    record` helper, whose `terms=()` would already violate the identity
    before any shading -- that would prove nothing about the substitution
    itself)."""
    threshold = 100.0
    score = threshold + 50.0  # comfortably inside band_base, well-formed record
    terms = (
        ReasonTerm(label="talent", input_value=1.0, weight=score, contribution=score),
    )
    actor_record = DecisionRecord(
        team_id=A_ID, philosophy=GmPhilosophy.EGALITARIAN, window=CompetitiveWindow.RETOOL,
        subject="b5-repro", verdict="reject", score=score, threshold=threshold, terms=terms,
    )
    assert actor_record.reconstructs(), "fixture record must be well-formed before shading"

    band = COUNTER_BAND_MULT * threshold
    band_eff = 1.25 * band  # a real, positive shade widening the band

    unshaded = gm_service._substitute_shaded_record(actor_record, band, band)
    assert unshaded == actor_record, "shade == 0 (band_eff == band) must be a strict no-op"
    assert unshaded.reconstructs()

    shaded = gm_service._substitute_shaded_record(actor_record, band_eff, band)
    assert not shaded.reconstructs(), (
        "the synthetic record's terms are never rescaled -- reconstructs() must read "
        "False once shading actually moves threshold/score (the disclosed B5 tradeoff)"
    )
    # The one property the substitution DOES guarantee, and the reason it is safe for
    # respond()'s own gap-only read despite the reconstruction-identity violation above.
    assert shaded.score - shaded.threshold == pytest.approx(
        actor_record.score - actor_record.threshold
    )


def test_channel3_mean_abs_shade_population_is_head_to_head_rows_not_decision_sites():
    """B3 fix (critic round) -- `gm_service._channel3_mean_abs_shade_
    inputs`'s population is the head-to-head ROWS themselves (§9.2, design
    doc line ~2165's "the demoted diagnostics ... across the 870 rows"),
    never a per-turn decision-site count. Asserted directly against the
    function, independent of whether any negotiation ever runs -- `shade`
    is computable per row with zero decision sites, per §9.2's own point
    that `shade()` needs no negotiation to exist."""
    from dba2.domain.head_to_head import HeadToHead

    def _row(team_id: int, opponent_id: int, opened: float, accepts: float) -> HeadToHead:
        return HeadToHead(
            league_id=LEAGUE_ID, team_id=team_id, opponent_id=opponent_id,
            opened=opened, received=0.0, accepts_by_opponent=accepts,
            rejects_by_opponent=opened - accepts, accepts_by_self=0.0, rejects_by_self=0.0,
            counters_given=0.0, counters_received=0.0, executions=0.0, refused_illegal=0.0,
            far_reject_streak=0.0, gap_by_opponent_sum=0.0, last_gap_by_opponent=None,
            last_season_interacted=SEASON, updated_season=SEASON,
        )

    # No memory at all -- (0.0, 0), never a ZeroDivisionError at the caller.
    assert gm_service._channel3_mean_abs_shade_inputs(None, None) == (0.0, 0)
    assert gm_service._channel3_mean_abs_shade_inputs({}, 0.5) == (0.0, 0)

    p_bar = 0.5
    rows = {
        (1, 2): _row(1, 2, opened=100.0, accepts=90.0),
        (2, 1): _row(2, 1, opened=100.0, accepts=10.0),
        (1, 3): _row(1, 3, opened=0.0, accepts=0.0),  # n=0 -> shade exactly 0.0
    }
    total, row_count = gm_service._channel3_mean_abs_shade_inputs(rows, p_bar)
    expected_total = sum(abs(memory.shade(row, p_bar)) for row in rows.values())
    assert total == pytest.approx(expected_total)
    assert total > 0.0
    # Three ROWS, none of which is a ch3 decision site -- no negotiation ran at all --
    # proving the population is rows, not decision sites (which would read 0 here).
    assert row_count == 3


def test_replaying_a_window_against_its_own_merged_result_double_counts_callers_job_to_prevent():
    """B2 (round 13 correction) -- the realistic replay shape a crash-and-
    retry phase hook actually produces, reproducing `tools/gm_harness/
    season.py`'s own merge idiom exactly: window 1 runs, its result is
    merged into a persistent store (`dict.update`, mirroring `HarnessState.
    head_to_head.update(...)`), a fresh `ctx` is rebuilt from that
    UPDATED store (mirroring `build_ctx`'s own `dict(state.head_to_head)`
    re-read), and the SAME window is replayed against it.

    **This is option (b), disclosed rather than silently accepted:**
    `run_trade_window` provides no replay guard of its own (see its own
    docstring's "Replay precondition" section) -- avoiding this exact
    double-count is the CALLER's job (never replay a window whose result
    was already merged into the store the next `ctx` is built from). This
    test pins the real, measured magnitude of what happens if a caller
    gets that wrong, so a future change that silently makes this WORSE (or
    a future claim that this path is "already idempotent") has a concrete,
    failing check to catch it."""
    team_a, team_b, roster_a, star1, star2, fillers_b = _base_rosters()
    snap_a = build_team_snapshot(team_a, roster_a, (), CAP_SETTINGS)
    snap_b = build_team_snapshot(team_b, (star1, star2) + fillers_b, (), CAP_SETTINGS)
    league_snapshot = build_league_snapshot(LEAGUE_ID, SEASON, (snap_a, snap_b))
    ctx_1 = _ctx_for(league_snapshot)

    result_1 = gm_service.run_trade_window(league_snapshot, ctx_1, max_negotiation_rounds=2)
    assert result_1.head_to_head, "fixture construction error -- this pair must negotiate once"

    # `state.head_to_head.update(trade_result.head_to_head)`, reproduced.
    store: dict[tuple[int, int], object] = {}
    store.update(result_1.head_to_head)

    # `build_ctx`'s own re-read: a fresh ctx built from the UPDATED store.
    ctx_2 = dataclasses.replace(
        ctx_1,
        head_to_head={key: dataclasses.replace(row) for key, row in store.items()},
        h2h_base_accept_rate=compute_h2h_base_accept_rate(store),
    )

    # The SAME window, replayed against the post-merge ctx.
    result_2 = gm_service.run_trade_window(league_snapshot, ctx_2, max_negotiation_rounds=2)
    store.update(result_2.head_to_head)

    any_pair = next(iter(result_1.head_to_head))
    opened_after_one_run = result_1.head_to_head[any_pair].opened
    opened_after_replay = store[any_pair].opened
    assert opened_after_one_run > 0.0
    assert opened_after_replay == pytest.approx(2.0 * opened_after_one_run), (
        "a replayed window against its own already-merged result must double every "
        "directed counter it touched -- this is the exact, disclosed cost of replaying "
        "without the caller's own not-already-applied guard"
    )
