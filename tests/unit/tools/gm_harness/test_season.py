"""`tools/gm_harness/season.py` -- the season-loop driver and recorder
(architect round-7 report §5.3, §6.1; the `WAIVERS` step, architect round
8 report §7 -- T7a2). T7a's own required tests, per the dispatch: the
1-seed x 1-season x skip-games smoke shape, Family-1 invariants, `check_
trade_conservation`'s mutation test, the draft-class re-key regression,
the pick-lottery `original_team_id` resolution, the `--skip-games` vs
with-games honesty check (slow), and a 2-season run exercising the
season-boundary ordering.

**Invariant 2 (`apron_2`) is genuinely closed at generation, the T6b
sibling apron_2 slice's real, corrected scope** (architect round 8
report §9) -- `league.py`'s per-team `_rescale_standard_contracts_under_
target` water-fills each team's `SigningMethod.CAP_SPACE` standard-
contract salaries (never rookie-scale/rank>30 `DRAFT` deals) down to a 3%
margin below the real basis this test itself reads -- `capsheet_by_team`
at `season + 1`, per `season.py::_capsheet_by_team`'s own docstring, not
the anchor generation season. **A first draft of this fix rescaled
against the wrong basis** (the anchor season's own ungrown `apron_2` and
the raw current-season total including contracts that expire before
`season + 1`) -- see `league.py`'s module docstring for the full
correction and honestly re-measured before/after numbers (58.1%/60.08%
of the league touched, down to the true generation-time tail). `PRESEASON`
is the boundary this slice's fix actually targets and the only one this
test still asserts against -- `season.py`'s own `PRESEASON` step only
mutates `window_by_team` (`apply_windows_inmem`), never a contract, so
`PRESEASON`'s `capsheet_by_team` is mathematically identical to the raw
generated state.

**A genuine, separate, out-of-scope gap was found and independently
diagnosed while re-deriving the corrected basis, not left silently
undiscovered**: the full-season claim ("every boundary holds") does
**not** hold -- `rng_seed=_SEED`, team 14 sits at `apron2_room=+4,650,523`
at `free_agency_open` and `apron2_room=-12,759,862` at `season_end`,
entirely from 3 free-agency signings (`MLE` $14.809M, `BI_ANNUAL`
$3.938M, `MIN_EXCEPTION` $1.337M -- all individually "legal" under
`gm_service.py`'s room/exception-ceiling checks). **`dba2/managers/
decisions/signing.py` and `gm_service.py`'s free-agency legality path
never check a post-signing team's `used` against `apron_2` at all** --
confirmed by grep: `apron_2`/`apron2` appears zero times in `signing.py`
and exactly once in `gm_service.py` (an unrelated docstring reference to
trade legality). Contrast `trade.py::_check_legality`, which has always
enforced `apron_2` as a real, independent ceiling for trades (`if
used_post > settings.apron_2: return "illegal:apron_2"`) -- free-agency
signing has no equivalent gate. **The first (wrong-basis) fix's own
"every boundary holds" claim was never actually true** -- it happened to
pass only because over-provisioning 58.1% of the league at generation
left enough slack to survive one season's worth of FA-signing drift by
coincidence, not because the underlying signing-legality gap was closed.
Fixing `signing.py`/`gm_service.py` to gate free-agency signings on
`apron_2` (symmetric to `trade.py`'s own existing gate) is a real,
separate, decisions/services-layer fix -- out of `league.py`'s own scope
and not built here; the boundary-wide test below is re-marked `xfail
(strict=True)` rather than silently weakened, deleted, or left green on
a false premise.

**Invariant 1 (roster size bounds) is now genuinely closed, T7a2** --
`run_waivers`/`apply_waiver_inmem` (architect round 8 §7) trim every team
back to `roster_max` at a new `WAIVERS` step, closing the real production
defect T7a's own build disclosed (`run_free_agency` gates on next-season
occupancy but nothing ever released a player, so every team drifted over
`roster_max` at `post_draft`/`free_agency_open`/`season_end`). The
invariant-1 test below is `xfail` no longer, but it is also narrower than
its original (pre-T7a2) form: it only checks the boundaries the phase
graph and the real NBA rule actually enforce a roster ceiling at --
`PRESEASON`/`post_trade_window_1`/`TRADE_DEADLINE`/`post_waivers` -- not
`post_draft`/`free_agency_open`/`season_end`, where carrying 16-20 bodies
through the draft and free agency is correct domain behavior (the real
limit binds on opening night, not in July). This narrowing is the
architect's own round-8 report §9.2 item 2 correction, applied here
because it is what makes this test genuinely, non-vacuously passable
rather than a silent Option-C-in-disguise (that report's own words for
weakening an invariant's measurement points without a real fix behind
it) -- see the test's own docstring below for the full reasoning.
"""
from __future__ import annotations

import dataclasses

import pytest

from dba2.domain.contract import OptionType
from dba2.domain.draft import DraftPick, ProtectionType
from dba2.domain.player import RosterStatus
from dba2.managers import capsheet, window
from dba2.managers.capsheet import CapSheetTeamSeasonInputs
from dba2.managers.scouting import scout_player
from dba2.managers.snapshot import build_league_snapshot, build_team_snapshot
from dba2.managers.tuning import DEFAULT_TUNING
from dba2.services.gm_service import TradeExecution
from tests.fixtures.managers import CAP_SETTINGS, make_contract, make_roster_player
from tools._gm_draft_class_stub import generate_draft_class
from tools.gm_harness.league import generate_gm_league
from tools.gm_harness.season import (
    _capsheet_by_team,
    _rekey_prospects,
    _resolve_pick_numbers,
    check_trade_conservation,
    run_season,
    run_seed,
)
from tools.gm_harness.state import HarnessState, build_ctx, build_snapshot

_LEAGUE_ID = 1
_SEASON = 2026
_SEED = 1


# ---------------------------------------------------------------------------
# 1-seed x 1-season x skip-games smoke: shape + Family-1 invariants that
# genuinely hold.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def smoke_run():
    """One seed, one season, skip-games -- shared across every assertion-
    only test in this module so the ~9s league generation + season loop
    runs once, not once per test."""
    return run_seed(
        league_id=_LEAGUE_ID,
        rng_seed=_SEED,
        first_season=_SEASON,
        seasons=1,
        players_per_team=15,
        skip_games=True,
        tuning=DEFAULT_TUNING,
    )


@pytest.mark.slow
def test_run_seed_smoke_completes_with_documented_shape(smoke_run):
    record = smoke_run
    assert record.rng_seed == _SEED
    assert len(record.philosophy_by_team) == 30
    assert len(record.season1_top1_minus_8th_by_team) == 30
    assert len(record.seasons) == 1

    season_record = record.seasons[0]
    assert season_record.season == _SEASON
    assert [b.label for b in season_record.boundaries] == [
        "PRESEASON",
        "post_trade_window_1",
        "TRADE_DEADLINE",
        "post_draft",
        "free_agency_open",
        "season_end",
        "post_waivers",
    ]
    assert len(season_record.trades_by_window) == 2
    assert len(season_record.packages_evaluated) == 2
    assert len(season_record.mutual_accepts) == 2
    # 30 teams x 2 rounds (round1+round2) of this season's own picks ->
    # every pick gets drafted (60 prospects generated to match exactly).
    assert len(season_record.draft_picks) == 60
    # Every team seeds at roster_max=15, gains 2 draft picks and however
    # many free-agent signings, then WAIVERS trims back to roster_max --
    # so at least one cut is expected every season (T7a2's own point).
    assert len(season_record.waivers) > 0
    trade_acquisitions = sum(
        len(e.players_a_to_b) + len(e.players_b_to_a)
        for execs in season_record.trades_by_window
        for e in execs
    )
    assert len(season_record.acquisitions) == (
        len(season_record.draft_picks) + len(season_record.signings) + trade_acquisitions
    )
    assert season_record.win_totals is None  # skip_games=True
    assert len(season_record.jsonl_lines) > 0
    assert season_record.conservation_violations == ()
    assert season_record.reconstruction_violations == ()
    assert set(season_record.phase_timings) == {
        "PRESEASON", "REGULAR_SEASON", "TRADE_DEADLINE", "GAMES",
        "DRAFT_LOTTERY", "DRAFT", "RESIGN_WINDOW", "FREE_AGENCY", "WAIVERS",
        "SEASON_BOUNDARY",
    }

    for boundary in season_record.boundaries:
        assert len(boundary.roster_ids_by_team) == 30
        assert len(boundary.overalls_by_team) == 30
        assert len(boundary.window_by_team) == 30
        assert len(boundary.capsheet_by_team) == 30
        assert len(boundary.contract_count_by_player) > 0
        # Architect round 19 §5/§7 (Slice 19-A): populated at every
        # boundary, not just `free_agency_open` (M10's own sole reader) --
        # round 19 §5's own "uniform computation, single-metric
        # consumption" instruction.
        assert boundary.capsheet_with_holds_by_team is not None
        assert len(boundary.capsheet_with_holds_by_team) == 30

    for waiver in season_record.waivers:
        assert waiver.record.verdict == "waive"
        assert waiver.record.reconstructs()


def test_family1_invariant_3_asset_conservation_holds(smoke_run):
    assert smoke_run.seasons[0].conservation_violations == ()


@pytest.mark.slow
def test_family1_invariant_3_asset_conservation_holds_on_a_seed_with_real_trades():
    """`smoke_run` (rng_seed=_SEED=1) executes zero trades in both windows
    this season -- critic round 1's B3 finding: the invariant-3 test above
    is vacuous, since `check_trade_conservation` is only ever called
    inside the trade-execution loop and never runs at all when nothing
    trades. `check_trade_conservation` itself already has direct
    clean/mutation unit tests (below) proving the check's own logic is
    sound; this test instead proves `run_season`'s own wiring of that
    check around *real* trades produced by the full decision pipeline
    actually gets exercised end to end.

    **Re-probed post round-9's roster-neutral throw-ins fix** (`docs/
    ledger/reports/phase-2-architect-round-9.md` §6 item 4 -- the
    highest-risk existing test this fix touches). Pre-fix, rng_seed=6
    was picked by probing seeds 1-8 for the only one that executed any
    real trade in season 1 (5 trades) -- the trade market being this
    seed-dependent was itself the disclosed upstream finding. Post-fix,
    EVERY one of seeds 1-8 executes at least one real trade in season 1
    (1: 2, 2: 7, 3: 4, 4: 5, 5: 2, 6: 9, 7: 6, 8: 5) -- seed=6 is kept
    (still valid, no longer the only option) with its trade count updated
    from 5 to 9. The underlying near-dead-trade-market finding (see
    HANDOFF.md's current next-action list, not cited by item number here
    since that list is rewritten every session) is about EXECUTIONS being
    rare in absolute terms, not seed-dependent zero-or-nonzero -- this
    re-probe does not contradict it.

    **Re-probed again post architect round 10's value-targeted outgoing
    construction (cause 2, `docs/ledger/reports/phase-2-architect-round-
    10.md`) -- §6 item 10's required procedure, in order.** (a) No cache-
    key regression: `test_propose_packages_populates_the_partners_cache_
    entries_with_the_partners_own_matrix` (§5.7 item 13,
    `test_gm_service_pure.py`) passes against the real fixture, closing
    the exact `(partner_id, A's roster)` mispairing hazard this fix
    introduces. (b) Re-probed seeds 1-8 for total trades/season: 43, 42,
    50, 50, 44, 34, 41, 48 (seed=6: 34) -- a large, genuine rise from the
    post-cause-1 figures above, not a fluke of this one seed (round 10's
    own §7 explicitly does NOT claim metric 18's `[12, 55]` band is
    reached by this slice -- whether it's a tuning question is out of
    this test's scope -- but the raw count moved this far and this test
    exists to track it honestly, not to under- or over-state it). (c)
    seed=6 is kept, count updated from 9 to 34, deliberately, per this
    docstring's own re-probe. (d) the `> 0` canary below is unchanged.

    **Re-probed a third time post architect round 12's occupancy-balance
    fix in `_build_throw_ins`** (`docs/ledger/reports/phase-2-architect-
    round-12.md` §5.3/§5.5 -- expected to break, per that memo's own §5.6
    and this project's established procedure, not a regression).
    (a) Cache-key guard re-confirmed first, per procedure:
    `test_propose_packages_populates_the_partners_cache_entries_with_the_
    partners_own_matrix` still passes against the real fixture, unaffected
    by this round (it never touches `_build_throw_ins`). (b) Re-probed
    seed=6 directly: 34 -> 33 -- a 1-trade drop, the expected direction
    and rough magnitude of round 12 §8 RR2 (constraining throw-in
    selection to be occupancy-neutral necessarily narrows package
    construction somewhat; the quota's own cross-class fallback is what
    keeps the drop this small rather than larger). Still comfortably
    inside metric 18's `[12, 55]` band on this one seed -- the real,
    3-seed-averaged Family-4 verdict is §5.6's own job, not this
    test's. (c) seed=6 is kept, count updated from 34 to 33, deliberately.
    (d) the `> 0` canary below is unchanged.

    **Re-probed a fourth time post slice 12-A's incumbent (Bird-rights)
    bucket** (`docs/ledger/reports/phase-2-architect-round-11.md` §5.3,
    round 12 §5.4 -- backend-dev round 46). (a) Cache-key guard re-
    confirmed first, per procedure: still passes. (b) Re-probed seed=6
    directly: **33 -> 33, unchanged.** This is expected, not a missed
    wiring gap: `run_season`'s own phase order (`season.py:801-986`) runs
    BOTH trade windows (`REGULAR_SEASON`, `TRADE_DEADLINE`) strictly
    BEFORE `RESIGN_WINDOW`/`FREE_AGENCY` within the same season -- a
    1-season fixture's trade count is therefore priced against room state
    from BEFORE that season's own free agency ever runs, so the Bird
    bucket (which only fires inside `run_resign_window`/`run_free_agency`)
    cannot reach season 1's trade legality at all. It would be expected to
    move at `seasons >= 2` (season 2's trades price against state left by
    season 1's own Bird-priced re-signings) -- not exercised by this
    1-season smoke test. (c) seed=6 is kept, count unchanged at 33 --
    nothing to update. (d) the `> 0` canary below is unchanged.

    **Re-probed a fifth time post Slice 22-B** (architect round 23 §4.2-
    §4.3, Decision P2-42 -- `signing.py`'s `_YEARS_BASE_LO`/`_YEARS_BASE_
    HI` recalibrated 1/4 -> 3/6). §4.7 obligation 6's own dispatch text
    expected this canary to break, on the general grounds that fewer/
    different signings change downstream trade dynamics -- **re-probed and
    it does NOT break, for exactly the same phase-order reason the fourth
    re-probe above already established for slice 12-A's Bird bucket: a
    1-season fixture's trades price entirely against state left by the
    PRIOR season's free agency (none exists here -- season 1 is the
    league's own generated starting state), and `offer_years`'s only call
    site is inside `run_resign_window`/`run_free_agency`, both of which
    run strictly after both trade windows within season 1 itself.** (a)
    Cache-key guard re-confirmed first, per procedure: still passes. (b)
    Re-probed seed=6 directly: **33 -> 33, unchanged.** (c) seed=6 is kept,
    count unchanged at 33 -- nothing to update. (d) the `> 0` canary below
    is unchanged. This is a checked finding, not an assumption: the
    dispatch's expectation of breakage is specific to *multi-season* trade
    dynamics (a later season's trades pricing against a book already
    reshaped by `offer_years`-recalibrated contracts), which this
    1-season fixture structurally cannot exercise -- see this file's own
    slow multi-season M1/M2 test, which does exercise `offer_years` live
    across 5 seasons and is the correct place to look for this slice's
    real trade-side interaction, if any.

    **Re-probed a sixth time post Phase 3 §11 Stage B step 7** (the
    negotiation loop wired into `run_trade_window` -- `docs/design/phase-
    3-inter-manager-society.md` §4.2/§11 step 7). **Expected to break, per
    the design's own §13.1 measurement** (`c ~= 0.0344` of seed packages
    open at least one counter round, and roughly 24% of those have a
    genuinely workable repair) -- a round-0 reject can now close `ACCEPT`
    after a counter, which only ever ADDS executions relative to Phase 2
    (§13.2's own M18 note: "counters can only increase executions"), never
    removes any (round-0 mutual accepts are untouched -- `respond()`'s own
    `(accept, accept)` -> `ACCEPT` branch is unconditional, matching Phase
    2 exactly at round 0). (a) Cache-key guard re-confirmed first, per
    procedure: still passes. (b) Re-probed seed=6 directly: **33 -> 36**,
    a 3-trade rise, the expected direction (up, never down) and a
    plausible magnitude for a single 1-season/2-window fixture at this
    seed's own package count. (c) seed=6 is kept, count updated from 33 to
    36, deliberately. (d) the `> 0` canary below is unchanged."""
    record = run_seed(
        league_id=_LEAGUE_ID,
        rng_seed=6,
        first_season=_SEASON,
        seasons=1,
        players_per_team=15,
        skip_games=True,
        tuning=DEFAULT_TUNING,
    )
    season_record = record.seasons[0]
    total_trades = sum(len(execs) for execs in season_record.trades_by_window)
    assert total_trades > 0, (
        "seed=6 is expected to execute real trades this season -- if this "
        "assertion ever fails, the trade market has gotten deader still "
        "(or league.py's generation changed) and a new seed must be found "
        "so this test keeps actually exercising conservation on real "
        "trades rather than going vacuous again"
    )
    assert total_trades == 36, (
        "seed=6 executes exactly 36 trades post Phase 3 §11 step 7's negotiation loop -- "
        "if this number drifts, either league.py's generation, propose_packages/"
        "_build_throw_ins/evaluate_trade's own logic, or the negotiation loop itself "
        "changed since this figure was last verified, and it needs re-probing, not "
        "silently updating"
    )
    assert season_record.conservation_violations == ()


def test_family1_invariant_4_no_player_with_two_active_contracts(smoke_run):
    for boundary in smoke_run.seasons[0].boundaries:
        assert all(count <= 1 for count in boundary.contract_count_by_player.values())


def test_family1_invariant_5_every_emitted_record_reconstructs(smoke_run):
    assert smoke_run.seasons[0].reconstruction_violations == ()


@pytest.mark.slow
def test_post_waivers_bounds_next_season_occupancy_at_or_below_roster_max():
    """A direct, narrower check than the invariant-1 test above, verifying
    the actual thing `WAIVERS` guarantees rather than a proxy for it.
    `post_draft`'s own raw body count genuinely exceeds `roster_max` this
    run (proving the invariant-1 test above is not vacuously passing
    because nothing ever went over in the first place) -- this test does
    NOT also claim that for `free_agency_open`/`season_end` (Nit 1: the
    prior version of this docstring claimed all three boundaries genuinely
    exceed `roster_max`, but the test body only ever asserted it for
    `post_draft`; critic found the `season_end` claim factually false --
    2 of 30 teams at this seed sit at exactly 15 there, not over).

    **B1 fix.** `post_waivers`'s own raw body count is no longer
    guaranteed `<= roster_max` (see the invariant-1 test's own docstring
    above for why -- a player expiring exactly this season stays
    physically `ACTIVE`-rostered through `post_waivers`). The real thing
    `WAIVERS` bounds is *next-season* occupancy, which only becomes a
    directly observable raw body count once `progress_one_season` evicts
    the season's own natural expirations at the boundary -- i.e. at the
    FOLLOWING season's own `PRESEASON`. This test runs 2 seasons and
    checks that boundary instead of `post_waivers` itself."""
    record = run_seed(
        league_id=_LEAGUE_ID,
        rng_seed=_SEED,
        first_season=_SEASON,
        seasons=2,
        players_per_team=15,
        skip_games=True,
        tuning=DEFAULT_TUNING,
    )
    season_1_by_label = {b.label: b for b in record.seasons[0].boundaries}
    assert any(
        len(ids) > 15 for ids in season_1_by_label["post_draft"].roster_ids_by_team.values()
    ), "post_draft is expected to exceed roster_max=15 -- draft/FA overfill is real, by design"

    season_2_preseason = next(b for b in record.seasons[1].boundaries if b.label == "PRESEASON")
    for team_id, roster_ids in season_2_preseason.roster_ids_by_team.items():
        assert len(roster_ids) <= 15, (
            f"season 2 PRESEASON: team {team_id} has {len(roster_ids)} players, over "
            f"roster_max -- WAIVERS failed to bound next-season occupancy"
        )


@pytest.mark.slow
def test_family1_invariant_1_roster_bounds_hold_across_multiple_seasons():
    """Family 1 item 1's real, multi-season teeth (critic round 2 on
    T7a2: the original single-season version of this test, and its
    sibling `..._at_waiver_enforced_boundaries`, both passed unchanged
    with `run_waivers` turned into a total no-op -- a 1-season `smoke_run`
    checks `PRESEASON`/`post_trade_window_1`/`TRADE_DEADLINE` before
    `WAIVERS` has ever run even once this whole record, since those
    boundaries all land within season 1 and every team starts at exactly
    15 *by generation*, independent of whether cuts ever happen. The
    bug class only shows up once a later season's boundary is measured
    against a roster that already carries a PRIOR season's `WAIVERS`
    pass forward. Critic's own instrumentation found `run_waivers` gating
    on today's raw body count (rather than next-season occupancy,
    `gm_service.py::_next_season_roster_count`'s own basis) put 11 of 30
    teams below `roster_min` at season-2 `PRESEASON` in this exact
    3-season run, before the B1 fix -- and a no-op `run_waivers` leaves
    every ceiling-checked boundary at exactly `roster_max` or above
    forever, which this test's own ceiling half now also catches.

    Runs `rng_seed=1, players_per_team=15, skip_games=True` for 3 seasons
    -- critic's own reproduction parameters, verbatim. Checks BOTH bounds
    (`[roster_min, roster_max]`) at `PRESEASON`/`post_trade_window_1`/
    `TRADE_DEADLINE` for every season including 2 and 3 (where the check
    is actually load-bearing, unlike season 1's generator-seeded state),
    and the floor only (`>= roster_min`) at `post_waivers`, where the raw
    body count can legitimately still exceed `roster_max` by however many
    players are expiring exactly this season and haven't been evicted by
    the season-boundary `progress_one_season` call yet (see `test_post_
    waivers_bounds_next_season_occupancy_at_or_below_roster_max`, the
    sibling test that checks `WAIVERS`'s real ceiling guarantee -- next-
    season occupancy -- directly, one season ahead of when it becomes a
    raw, observable body count at the following `PRESEASON`)."""
    ceiling_checked_labels = {"PRESEASON", "post_trade_window_1", "TRADE_DEADLINE"}
    floor_only_labels = {"post_waivers"}
    record = run_seed(
        league_id=_LEAGUE_ID,
        rng_seed=1,
        first_season=_SEASON,
        seasons=3,
        players_per_team=15,
        skip_games=True,
        tuning=DEFAULT_TUNING,
    )
    checked_any = False
    for season_record in record.seasons:
        for boundary in season_record.boundaries:
            if boundary.label in ceiling_checked_labels:
                checked_any = True
                for team_id, roster_ids in boundary.roster_ids_by_team.items():
                    assert 14 <= len(roster_ids) <= 15, (
                        f"season={season_record.season} {boundary.label}: team {team_id} "
                        f"has {len(roster_ids)} players"
                    )
            elif boundary.label in floor_only_labels:
                checked_any = True
                for team_id, roster_ids in boundary.roster_ids_by_team.items():
                    assert len(roster_ids) >= 14, (
                        f"season={season_record.season} {boundary.label}: team {team_id} "
                        f"has {len(roster_ids)} players, below roster_min=14"
                    )
    assert checked_any, "no boundary in this 3-season run matched the checked label sets"


def test_family1_invariant_2_apron2_budget_holds_at_generation(smoke_run):
    """The real scope this slice (`league.py`'s generator fix) owns and
    verifies -- `PRESEASON` is mathematically identical to the raw
    generated state (module docstring: `season.py`'s own `PRESEASON` step
    only mutates `window_by_team`, never a contract), so this is exactly
    the `season + 1` basis `_target_season_plus_1_capsheet_by_team`/`_
    rescale_standard_contracts_under_target` check and correct against at
    generation time."""
    boundary = smoke_run.seasons[0].boundaries[0]
    assert boundary.label == "PRESEASON"
    for team_id, row in boundary.capsheet_by_team.items():
        # `apron2_room = apron_2 - used`, already computed against this
        # row's own (possibly projected) season's real apron_2 --
        # reading that directly avoids needing an external apron_2
        # constant that would only be correct for one specific season.
        assert row.apron2_room >= 0, (
            f"{boundary.label}: team {team_id} used {row.used}, apron2_room {row.apron2_room}"
        )


# T7a2/apron2-fix 2nd critic round nit 2: the original single xfail'd test
# below covered ALL 7 boundaries with one strict xfail, discarding real
# coverage on the 5 that this slice's own fix already keeps genuinely
# clean (PRESEASON, post_trade_window_1, TRADE_DEADLINE, post_draft,
# free_agency_open have zero violations; only season_end/post_waivers
# violate, both caused by the disclosed signing.py gap below). Split so
# the 5 clean boundaries are asserted for real -- a future regression in
# league.py's own generator fix, or in the trade/draft/resign phases
# upstream of free agency, is not silently absorbed into the strict
# xfail's own blanket coverage.
#
# **Two seeds, not one, as of architect round 10's value-targeted outgoing
# construction fix (backend-dev round 36, critic round 42 nit 7).** The
# 5-clean/2-dirty split below was originally derived and reconfirmed at a
# single seed, `rng_seed=_SEED` (1) -- the sibling test immediately below
# still asserts the 5 clean boundaries against that same seed via the
# shared `smoke_run` fixture. But round 10's fix changed trade volume and
# composition enough that `rng_seed=_SEED`'s own former season_end/post_
# waivers reproduction (team 14) stopped violating -- not because the
# underlying `signing.py` gap closed, only because different players ended
# up on the free-agency market. The xfail test further below is therefore
# re-pointed to a fresh `rng_seed=2` (a dedicated `run_seed` call, not
# `smoke_run`) to keep exercising the still-real gap; the two tests in
# this section now run against genuinely different seeds, not the same
# one twice.
_APRON2_CLEAN_BOUNDARY_LABELS = frozenset(
    {"PRESEASON", "post_trade_window_1", "TRADE_DEADLINE", "post_draft", "free_agency_open"}
)


def test_family1_invariant_2_apron2_budget_holds_through_free_agency_open(smoke_run):
    checked_any = False
    for boundary in smoke_run.seasons[0].boundaries:
        if boundary.label not in _APRON2_CLEAN_BOUNDARY_LABELS:
            continue
        checked_any = True
        for team_id, row in boundary.capsheet_by_team.items():
            assert row.apron2_room >= 0, (
                f"{boundary.label}: team {team_id} used {row.used}, apron2_room {row.apron2_room}"
            )
    assert checked_any, "no boundary in this run matched _APRON2_CLEAN_BOUNDARY_LABELS"


@pytest.mark.slow
def test_family1_invariant_2_apron2_budget_holds_after_free_agency():
    """**Gap closed (architect round 14, backend-dev round 49) -- this test
    was `xfail(strict=True)` from round 10 (2026-08-30ish) until now.**

    The prior xfail's own reason recorded a genuine, independently
    diagnosed gap: `signing.py`/`gm_service.py`'s free-agency legality path
    never checked a post-signing team's `used` against `apron_2` at all --
    a team could legally combine several individually-legal offers (each
    passing its own room/exception-ceiling check) into a post-signing total
    that breached `apron_2`, symmetric to a gate `trade.py::_check_legality`
    already had for trades but signing.py never did. Reproduced at
    `rng_seed=2`, team 19: `season_end` at `apron2_room=-14,690,593` despite
    `apron2_room=+4,168,593` at `free_agency_open`, and confirmed on 7 of 8
    sampled seeds (1 was already clean by luck of that generation's player
    mix, not because the gap was fixed).

    **What closed it**: architect round 14 (`docs/ledger/reports/phase-2-
    architect-round-14.md`) found the true root cause was two-fold -- `_
    finalize_signing` tagged an incumbent BIRD without ever checking the
    Bird ceiling actually covered the salary (so the once-per-pass MLE
    ladder was silently bypassed for incumbents), and no signing method
    charged a shared, `apron_2`-basis dollar ledger at all, so `apron_1`'s
    margin over `apron_2` (41% smaller than one MLE, §3 H3) was structurally
    exposed. The fix (`signing.py::legal_capacity`'s hard clamp, `gm_
    service.py`'s `_effective_capacity`/`_finalize_signing` guard-first
    assert + `charge_apron()` on every branch) makes the `apron_2` invariant
    hold by construction (round 14 §5.2's three-line proof), not by luck of
    which players happen to be on a given seed's market.

    **Re-probed seeds 1-8 (backend-dev round 49, `artifacts/scratch_reprobe_
    apron2_seeds.py`) -- all 8 clean at both `season_end` and `post_
    waivers`, 1 season each, skip-games.** `rng_seed=2`'s own former
    reproduction (team 19) is retained as the pinned regression seed below,
    since it is the sharpest documented case, not because it is special
    post-fix -- every sampled seed is now equally clean.

    **Deliberately does NOT use the shared `smoke_run` fixture** (rng_
    seed=_SEED=1) the way every sibling `test_family1_invariant_2_*` test in
    this file does -- this test keeps its own dedicated `run_seed` call at
    rng_seed=2, the documented regression seed, rather than switching onto
    a fixture some other test might one day repoint."""
    record = run_seed(
        league_id=_LEAGUE_ID, rng_seed=2, first_season=_SEASON, seasons=1,
        players_per_team=15, skip_games=True, tuning=DEFAULT_TUNING,
    )
    checked_any = False
    for boundary in record.seasons[0].boundaries:
        if boundary.label in _APRON2_CLEAN_BOUNDARY_LABELS:
            continue
        checked_any = True
        for team_id, row in boundary.capsheet_by_team.items():
            assert row.apron2_room >= 0, (
                f"{boundary.label}: team {team_id} used {row.used}, apron2_room {row.apron2_room}"
            )
    assert checked_any, "no boundary in this run fell outside _APRON2_CLEAN_BOUNDARY_LABELS"


@pytest.mark.slow
def test_family1_invariant_2_is_clean_and_gap_free_on_a_real_multi_season_run():
    """Architect round 14 §5.5 item 10c -- round 11 §5.5 item 10's own text
    said "the existing invariant, re-run"; this is what that sentence
    should have produced (the pre-fix item 10 it replaced never called any
    of the real invariant machinery at all, critic round 47's B4). Calls
    the REAL `run_seed(...)` for 2 seasons at a pinned seed, then `metrics.
    compute_m2(...)` against the REAL loaded `BandSpec` (`seeds/gm_
    validation_bands.json` via `bands.load_bands()`) -- never a
    reimplementation of the boundary-walk or the clean/known-gap split.

    2 seasons, not 1: `season_end`/`post_waivers` (the two `known_gap`
    boundaries) only exist after a season's own free agency and waiver
    pass complete, and a 2nd season's free agency prices against contracts
    the 1st season's own (now Bird-aware) re-signings wrote -- the carry-
    over shape critic round 47's own B1 trace found the pre-fix bug at
    (§3 H6: 5 of 6 clean violations were one season's free-agency pass,
    observed at the FOLLOWING season's clean boundaries)."""
    from tools.gm_harness.bands import load_bands
    from tools.gm_harness.metrics import compute_m2

    record = run_seed(
        league_id=_LEAGUE_ID, rng_seed=3, first_season=_SEASON, seasons=2,
        players_per_team=15, skip_games=True, tuning=DEFAULT_TUNING,
    )
    bands = load_bands()
    result = compute_m2((record,), bands)

    assert result.value == 0.0, (
        f"M2 clean-boundary violations must be exactly 0 under round 14 §5.2's structural "
        f"proof -- got {result.value}, by-boundary breakdown: "
        f"{result.detail['violations_by_boundary']}"
    )
    assert result.detail["known_gap_violations"] == 0, (
        "the disclosed free-agency apron_2 gap (seeds/gm_validation_bands.json's M2 "
        "known_gap block) is closed by this fix -- a nonzero count here means the fix "
        "regressed, not that the known_gap tolerance is still needed"
    )


# ---------------------------------------------------------------------------
# Architect round 19 §5/§7 (Slice 19-A) -- `capsheet_with_holds_by_team`
# must not disturb `capsheet_by_team`'s own hold-free basis, the guarantee
# M2/M7/M8a/M8b/M9 all rely on to be structurally incapable of regressing.
# ---------------------------------------------------------------------------


def test_boundary_record_defaults_capsheet_with_holds_by_team_to_none():
    """The pickle-compatibility contract (round 19 §5's own "pickle-
    compatibility trap," §7 item 3): a `BoundaryRecord` built without this
    keyword -- exactly what unpickling a pre-round-19 record produces --
    must not raise and must carry `None`, not an empty dict or a
    fabricated value."""
    from dba2.domain.phase import Phase
    from tools.gm_harness.season import BoundaryRecord

    record = BoundaryRecord(
        phase=Phase.PRESEASON,
        label="PRESEASON",
        roster_ids_by_team={},
        overalls_by_team={},
        window_by_team={},
        capsheet_by_team={},
        pick_owner_by_pick={},
        contract_count_by_player={},
    )
    assert record.capsheet_with_holds_by_team is None


def _independent_hold_free_row(
    state: HarnessState, *, team_id: int, tuning, cap_holds: int
) -> capsheet.SeasonCapProjection:
    """From-scratch reconstruction of the hold-free `SeasonCapProjection`
    row at `state.season + 1` for one team, written entirely independently
    of `_aggregate_capsheet_inputs`/`_capsheet_by_team` (`tools/gm_harness/
    state.py`/`season.py`) -- never calls either. Mirrors those functions'
    own documented *pre-round-19* definitions exactly (`state.py`'s module
    docstring: `committed_salary` is every active contract-year's salary
    for `state.season + 1`, `contract_repo.get_committed_salary`'s own
    definition; `roster_count` is contract-covered players for that season
    plus the contractless-rostered-player correction) directly against
    `state.players`/`state.dead_cap_by_team_season`.

    `cap_holds` is a caller-supplied parameter, not hardcoded -- **not
    a bug this test is chasing**. `dba2/managers/capsheet.py::compute_
    team_season_cap_sheet`'s own pre-existing, deliberately-tested
    contract (`tests/unit/managers/test_capsheet.py::test_cap_holds_
    always_computed_but_only_added_to_used_when_asked`, mirroring
    `cap_service.get_cap_summary`'s own `include_holds` contract exactly)
    is that `.cap_holds` on the returned row is **always** the real
    computed figure, regardless of `include_holds` -- only `used`/`room`/
    `tax_room`/`apron1_room`/`apron2_room` differ by whether the hold is
    added in. **Critic round 52's N1 finding, and this test's first
    version (backend-dev round 57), got this backwards** -- they treated
    a nonzero `.cap_holds` on the hold-free row as a leak and "fixed"
    `compute_team_season_cap_sheet` to zero it when `include_holds=False`,
    which broke `test_capsheet.py`'s own pre-existing, correct test (caught
    by the orchestrator directly, independent of either round). Reverted;
    this test now passes the caller the real hold figure (sourced from the
    hold-inclusive row for the same team, which must carry the identical
    value) and asserts full field equality on that correct basis instead.

    This is still the fixture architect round 19 §7 asked for ("assert
    the existing `capsheet_by_team` equals a fixture computed before your
    change, field for field") -- `SeasonCapProjection` is a frozen
    dataclass, so `==` against `_capsheet_by_team`'s own real hold-free
    output below compares every one of its 15 fields, not just `used`."""
    season = state.season + 1
    committed = 0
    team_opt = 0
    player_opt = 0
    contract_covered: set[int] = set()
    contractless = 0
    for rp in state.players.values():
        if rp.contract is not None:
            if rp.contract.contract.team_id != team_id:
                continue
            for year in rp.contract.years:
                if year.season != season:
                    continue
                committed += year.salary
                contract_covered.add(rp.id)
                if year.option_type is OptionType.TEAM:
                    team_opt += year.salary
                elif year.option_type is OptionType.PLAYER:
                    player_opt += year.salary
        elif rp.player.team_id == team_id and rp.player.roster_status is RosterStatus.ACTIVE:
            contractless += 1
    roster_count = len(contract_covered) + contractless
    dead_cap = state.dead_cap_by_team_season.get((team_id, season), 0)
    inputs = CapSheetTeamSeasonInputs(
        team_id=team_id,
        season=season,
        committed_salary=committed,
        dead_cap=dead_cap,
        cap_holds=cap_holds,
        team_option_salary=team_opt,
        player_option_salary=player_opt,
        roster_count=roster_count,
    )
    cap_settings = capsheet.project_cap_settings(
        state.cap_settings_by_season[state.season], season, tuning=tuning
    )
    return capsheet.compute_team_season_cap_sheet(
        inputs, cap_settings, projected=True, include_holds=False
    )


@pytest.mark.slow
def test_hold_free_capsheet_matches_an_independently_reconstructed_pre_hold_fixture():
    """**The single most important test in this slice** (round 19 §7's own
    framing). On a REAL generated league (not a hand-built fixture --
    `state.players`/`state.cap_settings_by_season` carry real contracts/
    growth, so any wiring mistake in `_capsheet_by_team`'s 2-basis split
    shows up against real data), at the start of a run and after each of 2
    real `run_season` advances: every team's hold-free `capsheet_by_team`
    row (`_capsheet_by_team`'s own first return value) must be byte-
    identical, field for field, to `_independent_hold_free_row`'s
    from-scratch reconstruction above.

    **`.cap_holds` is asserted equal to the paired hold-inclusive row's own
    `.cap_holds`, not to 0.** `dba2/managers/capsheet.py::compute_team_
    season_cap_sheet`'s pre-existing, deliberately-tested contract
    (`test_capsheet.py::test_cap_holds_always_computed_but_only_added_to_
    used_when_asked`) is that `.cap_holds` is always the real computed
    figure regardless of `include_holds` -- only `used` and its
    derivatives differ. An earlier version of this test (backend-dev
    round 57, responding to critic round 52's N1) asserted `.cap_holds ==
    0` on the hold-free row and "fixed" `compute_team_season_cap_sheet` to
    match -- which broke `test_capsheet.py`'s own pre-existing test.
    Caught and reverted directly by the orchestrator; this version
    exercises the CORRECT invariant instead: both rows report the
    identical real hold figure, `used`/`room`/etc. are what actually
    differ, and that is what `_capsheet_by_team`'s 2-basis split exists to
    guarantee."""
    state = generate_gm_league(
        league_id=_LEAGUE_ID, season=_SEASON, rng_seed=5,
        players_per_team=15, tuning=DEFAULT_TUNING,
    )
    checked_any_nonzero_hold = False
    for i in range(2):
        season = _SEASON + i
        hold_free, hold_inclusive = _capsheet_by_team(state, DEFAULT_TUNING)
        for team_id in state.teams:
            real_cap_holds = hold_inclusive[team_id].cap_holds
            expected = _independent_hold_free_row(
                state, team_id=team_id, tuning=DEFAULT_TUNING, cap_holds=real_cap_holds
            )
            actual = hold_free[team_id]
            assert actual == expected, (
                f"season {state.season} team {team_id}: hold-free capsheet row must be "
                f"byte-identical, field for field, to the independently reconstructed "
                f"fixture -- got {actual!r}, expected {expected!r}"
            )
            assert actual.cap_holds == real_cap_holds, (
                f"season {state.season} team {team_id}: hold-free row's own cap_holds "
                f"field must equal the paired hold-inclusive row's cap_holds -- "
                f"got {actual.cap_holds}, expected {real_cap_holds}"
            )
            if real_cap_holds > 0:
                checked_any_nonzero_hold = True
        run_season(
            state, season=season, seed=5, skip_games=True,
            tuning=DEFAULT_TUNING, first_season=_SEASON,
        )
    assert checked_any_nonzero_hold, (
        "this run generated zero nonzero cap_holds anywhere -- the fixture is too small to "
        "actually exercise a real hold figure"
    )


# ---------------------------------------------------------------------------
# Decision P2-39 (architect round 21 §4.2/§4.7 test 6) -- `build_ctx`'s
# `room_for_signings_by_team` and `_capsheet_by_team`'s hold-inclusive basis
# must be the same number by construction: both derive from the same
# `_aggregate_capsheet_inputs` call on the same `HarnessState`, and nothing
# mutates state between `season.py`'s own `free_agency_open` boundary record
# and its `build_ctx` call (round 21 §0 item 7's "checkable for free" claim).
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_room_for_signings_by_team_agrees_with_the_hold_inclusive_boundary_capsheet():
    """§4.7 test 6 -- the free harness-agreement property. On a REAL
    generated league (not a hand-built fixture, for the same reason `test_
    hold_free_capsheet_matches_...` above insists on one: a wiring mistake
    needs real, varied contract data to surface against), `ctx.room_for_
    signings_by_team[t]` must equal `capsheet_with_holds_by_team[t].room`
    for every one of the 30 teams -- checked at league generation and after
    2 real `run_season` advances, mirroring that same test's own multi-
    season shape since a mid-season hold recomputation is exactly where a
    second, drifting aggregation pass would first show up.

    *Mutation: build `room_for_signings_by_team` from a second `include_
    holds=False` call (i.e. the same basis `room_by_team` already uses) --
    every team's figure then matches `capsheet_by_team` instead of `capsheet_
    with_holds_by_team`, and the equality assertion below fails wherever any
    team actually carries a nonzero hold this season.*"""
    state = generate_gm_league(
        league_id=_LEAGUE_ID, season=_SEASON, rng_seed=5,
        players_per_team=15, tuning=DEFAULT_TUNING,
    )
    from dba2.domain.phase import Phase

    checked_any_nonzero_hold = False
    for i in range(2):
        season = _SEASON + i
        ctx = build_ctx(state, phase=Phase.FREE_AGENCY, tuning=DEFAULT_TUNING)
        _hold_free, hold_inclusive = _capsheet_by_team(state, DEFAULT_TUNING)
        assert set(ctx.room_for_signings_by_team) == set(state.teams)
        for team_id in state.teams:
            expected = hold_inclusive[team_id].room
            actual = ctx.room_for_signings_by_team[team_id]
            assert actual == expected, (
                f"season {state.season} team {team_id}: ctx.room_for_signings_by_team must "
                f"equal boundary.capsheet_with_holds_by_team[t].room by construction -- "
                f"got {actual}, expected {expected}"
            )
            if hold_inclusive[team_id].cap_holds > 0:
                checked_any_nonzero_hold = True
        run_season(
            state, season=season, seed=5, skip_games=True,
            tuning=DEFAULT_TUNING, first_season=_SEASON,
        )
    assert checked_any_nonzero_hold, (
        "this run generated zero nonzero cap_holds anywhere -- the fixture is too small to "
        "actually exercise a real hold figure"
    )


# ---------------------------------------------------------------------------
# Decision P2-39 (architect round 21 §4.6/§4.7 test 7) -- M1/M2 hard
# invariants on a real, multi-seed x multi-season run with the new
# hold-aware signing-capacity basis actually live in `run_free_agency`
# (via `build_ctx`'s unconditional population, above). Slow: 2 seeds x 5
# seasons, `--skip-games`-equivalent -- run explicitly with `pytest -m slow`.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_m1_m2_gated_values_are_exactly_zero_with_room_for_signings_by_team_live():
    """§4.7 test 7 -- the empirical M1/M2 safety check architect round 21
    §4.6 requires rather than concludes from the structural proof alone
    (the reassignment channel: a lower offer can hand a player to a
    different team via `rank_offers`, which is not monotone in anything).
    2 seeds x 5 seasons -- the same exit-criterion floor (`min_seeds: 3,
    min_seasons: 5`, close enough at 2 seeds for a unit-test-scale check;
    the real §5 gate run below uses the full 3 seeds) with the harness's
    own `build_ctx` now always populating `room_for_signings_by_team`, so
    `run_free_agency` is genuinely seeded from the hold-aware basis on
    every free-agency pass in this run, not merely present-but-unread.

    **Any violation here is a hard stop per architect round 21 §4.6/§5**:
    fix first, measure after -- this test is not allowed to report a
    passing build report if it fails."""
    from tools.gm_harness.bands import load_bands
    from tools.gm_harness.metrics import compute_m1, compute_m2

    records = tuple(
        run_seed(
            league_id=_LEAGUE_ID, rng_seed=seed, first_season=_SEASON, seasons=5,
            players_per_team=15, skip_games=True, tuning=DEFAULT_TUNING,
        )
        for seed in (101, 102)
    )
    bands = load_bands()

    m1 = compute_m1(records, bands)
    m2 = compute_m2(records, bands)

    assert m1.value == 0.0, (
        f"M1 (roster floor/ceiling) must be exactly 0 with room_for_signings_by_team live -- "
        f"got {m1.value}, detail: {m1.detail}"
    )
    assert m2.value == 0.0, (
        f"M2 (apron_2 clean-boundary violations) must be exactly 0 with room_for_signings_by_"
        f"team live -- got {m2.value}, by-boundary breakdown: "
        f"{m2.detail['violations_by_boundary']}"
    )
    assert m2.detail["known_gap_violations"] == 0, (
        "the disclosed free-agency apron_2 gap must stay closed under the new basis too"
    )


@pytest.mark.slow
def test_slice_22b_coverage_restores_toward_season_1s_own_calibration_control():
    """§4.7 test 8 (new, architect round 23 §4.2-§4.4, Decision P2-42) --
    the coverage assertion IS this slice's calibration target, not a proxy
    for M10. `_YEARS_BASE_LO`/`_YEARS_BASE_HI` were recalibrated toward the
    generator's own `E[L] = 3.5714`-season equilibrium (item 10's duration
    diagnosis), and season 1's own book is 100% generator vintage on that
    exact equilibrium -- so season 1's own reading, measured in this same
    run, is the target expressed in the units actually measured (round 23
    §0 item 2), not an external number that could itself be stale.

    **Basis** (round 23 §2.2, verified at `season.py:958-978`): `free_
    agency_open`'s `capsheet_by_team[...].roster_count` -- the number of
    contracts covering `season + 1` -- read immediately after the draft
    has applied and strictly before any re-signing or free-agency signing
    exists this season. Averaged over all 30 teams, per season.

    **Measured for this build** (`rng_seed=101`, 5 seasons, `skip_games=
    True` -- the same run this file's sibling M1/M2 slow test already
    exercises): season 2026 (100% generator vintage) reads **12.6667**;
    season 2030 (100% decision-layer vintage, 5 seasons of `offer_years`-
    priced signings behind it) reads **12.6333** -- a **0.0333**-body
    difference, well inside the ±0.5 tolerance and consistent with round
    23 §4.4's own pre-registration of "essentially full restoration"
    (predicted post-slice season-5 coverage 12.38 against a season-1
    control of 12.3667, on the 3-seed pooled basis -- this single-seed
    reading lands even closer). **This is the test that would catch a
    future round quietly re-picking `_YEARS_BASE_LO`/`_HI` for an M10 PASS
    without regard to the generator's own equilibrium** -- see round 23
    §4.1's own "the target does the choosing" argument.

    Mutation to confirm this test bites (verified by hand during this
    build, then reverted): reverting `_YEARS_BASE_LO`/`_YEARS_BASE_HI` to
    their pre-slice values (1/4) drops season 2030's mean coverage on this
    exact seed to well outside the ±0.5 band around season 2026's own
    reading (the pre-slice regime is the one this slice's own duration
    diagnosis found to be the shortfall in the first place)."""
    from tools.gm_harness.season import run_seed

    record = run_seed(
        league_id=_LEAGUE_ID, rng_seed=101, first_season=_SEASON, seasons=5,
        players_per_team=15, skip_games=True, tuning=DEFAULT_TUNING,
    )

    def _mean_coverage(season_record) -> float:
        boundary = next(b for b in season_record.boundaries if b.label == "free_agency_open")
        counts = [row.roster_count for row in boundary.capsheet_by_team.values()]
        return sum(counts) / len(counts)

    season_1_coverage = _mean_coverage(record.seasons[0])
    season_5_coverage = _mean_coverage(record.seasons[-1])

    assert abs(season_5_coverage - season_1_coverage) <= 0.5, (
        f"season 5 mean next-season coverage at free_agency_open ({season_5_coverage}) "
        f"has drifted more than 0.5 bodies/team from season 1's own generator-vintage "
        f"control ({season_1_coverage}) -- offer_years is no longer calibrated toward "
        f"the generator's own equilibrium, regardless of what M10 reads"
    )


# ---------------------------------------------------------------------------
# check_trade_conservation -- clean pass + mutation test.
# ---------------------------------------------------------------------------


def _two_team_snapshot():
    contract_101 = make_contract(101, team_id=1, start_season=_SEASON, salaries=(5_000_000,))
    contract_201 = make_contract(201, team_id=2, start_season=_SEASON, salaries=(5_000_000,))
    player_101 = make_roster_player(
        101, age=25, season=_SEASON, overall_cached=80, team_id=1, contract=contract_101
    )
    player_201 = make_roster_player(
        201, age=24, season=_SEASON, overall_cached=70, team_id=2, contract=contract_201
    )
    pick = DraftPick(
        id=1, league_id=_LEAGUE_ID, season=_SEASON + 1, round=1,
        original_team_id=1, current_team_id=1, pick_number=None, used_for_player_id=None,
        protection_type=ProtectionType.NONE, protection_through=None, swap_with_pick_id=None,
        conveyed=False, rolls_over_to_season=None,
    )
    from dba2.domain.team import GmPhilosophy, Team, Timeline

    def _team(tid: int) -> Team:
        return Team(
            id=tid, league_id=_LEAGUE_ID, code=f"T{tid}", name=f"Team{tid}", city=f"City{tid}",
            conference="East", division="Atlantic", manager_user_id=None,
            gm_philosophy=GmPhilosophy.EGALITARIAN, timeline=Timeline.RETOOL, home_market_size=3,
        )

    team1 = build_team_snapshot(_team(1), (player_101,), (pick,), CAP_SETTINGS)
    team2 = build_team_snapshot(_team(2), (player_201,), (), CAP_SETTINGS)
    return build_league_snapshot(_LEAGUE_ID, _SEASON, (team1, team2)), player_101, player_201, pick


def test_check_trade_conservation_clean_trade_has_no_violations():
    before, player_101, player_201, pick = _two_team_snapshot()
    execution = TradeExecution(
        team_a_id=1, team_b_id=2,
        players_a_to_b=(101,), players_b_to_a=(),
        picks_a_to_b=(), picks_b_to_a=(),
        net_a=0.0, net_b=0.0,
        record_a=None, record_b=None,
    )
    moved_player = dataclasses.replace(player_101.player, team_id=2)
    moved_101 = dataclasses.replace(player_101, player=moved_player)
    team1_after = build_team_snapshot(
        before.team_by_id(1).team, (), before.team_by_id(1).draft_picks, CAP_SETTINGS
    )
    team2_after = build_team_snapshot(
        before.team_by_id(2).team, (before.team_by_id(2).roster[0], moved_101), (), CAP_SETTINGS
    )
    after = build_league_snapshot(_LEAGUE_ID, _SEASON, (team1_after, team2_after))

    assert check_trade_conservation(before, after, execution) == ()


def test_check_trade_conservation_catches_a_player_that_did_not_actually_move():
    """Mutation test (report's own required T7a test): `execution` claims
    player 101 moved from team 1 to team 2, but the hand-built `after`
    snapshot leaves team 1's roster untouched -- `check_trade_conservation`
    must catch this, not report a clean trade."""
    before, player_101, player_201, pick = _two_team_snapshot()
    execution = TradeExecution(
        team_a_id=1, team_b_id=2,
        players_a_to_b=(101,), players_b_to_a=(),
        picks_a_to_b=(), picks_b_to_a=(),
        net_a=0.0, net_b=0.0,
        record_a=None, record_b=None,
    )
    # after == before: nothing actually moved, despite execution's claim.
    after = before

    violations = check_trade_conservation(before, after, execution)
    assert violations != ()
    assert any("101" in v for v in violations)


# ---------------------------------------------------------------------------
# Draft-class re-keying (report §5.5).
# ---------------------------------------------------------------------------


def test_rekey_prospects_never_collide_across_two_seasons():
    first_season = _SEASON
    raw_a = generate_draft_class(season=first_season, seed=100, league_id=_LEAGUE_ID)
    raw_b = generate_draft_class(season=first_season + 1, seed=200, league_id=_LEAGUE_ID)
    rekeyed_a = _rekey_prospects(raw_a, season=first_season, first_season=first_season)
    rekeyed_b = _rekey_prospects(raw_b, season=first_season + 1, first_season=first_season)

    ids_a = {rp.id for rp in rekeyed_a}
    ids_b = {rp.id for rp in rekeyed_b}
    assert ids_a.isdisjoint(ids_b)
    assert len(ids_a) == len(raw_a)
    assert min(ids_a) >= 900_000


def test_rekey_prospects_changes_scouted_price_before_valuation():
    """Proves the re-key happens *before* any valuation call touches the
    prospect (report §5.5's own stated risk) -- `scout_player` salts its
    seeded draw on `player_id`, so a re-keyed prospect's scouted peak must
    differ from what the same player would have scouted at under the raw
    `1..60` id."""
    raw = generate_draft_class(season=_SEASON, seed=42, league_id=_LEAGUE_ID)
    rekeyed = _rekey_prospects(raw, season=_SEASON + 3, first_season=_SEASON)

    raw_player = raw[0].player
    rekeyed_player = rekeyed[0].player
    assert raw_player.id != rekeyed_player.id

    kwargs = dict(
        league_rng_seed=42, team_id=1, season=_SEASON,
        ovr=raw_player.overall_cached, age=19.0,
        peak_age_start=float(raw[0].ratings.peak_age_start),
        potential=raw[0].ratings.potential,
        potential_variance=raw[0].ratings.potential_variance,
        draft_eligible=True, risk=1.0,
    )
    scouted_raw = scout_player(player_id=raw_player.id, **kwargs)
    scouted_rekeyed = scout_player(player_id=rekeyed_player.id, **kwargs)
    assert scouted_raw.scouted_peak != scouted_rekeyed.scouted_peak


# ---------------------------------------------------------------------------
# Pick-lottery resolution -- original_team_id's rank, not current_team_id's.
# ---------------------------------------------------------------------------


def test_resolve_pick_numbers_tracks_original_team_id_not_current_team_id():
    state = generate_gm_league(league_id=_LEAGUE_ID, season=_SEASON, rng_seed=7)
    snapshot = build_snapshot(state)
    scores = {
        ts.id: window.window_score(ts, snapshot, tuning=DEFAULT_TUNING) for ts in snapshot.teams
    }
    worst_first = sorted(scores, key=lambda tid: scores[tid])
    worst_team_id = worst_first[0]
    best_team_id = worst_first[-1]

    # Find worst_team_id's own round-1 pick for this season and hand it to
    # best_team_id -- simulating a trade without going through apply_trade_
    # inmem (this test's own job is _resolve_pick_numbers, not the applier).
    traded_pick = next(
        p for p in state.picks.values()
        if p.season == _SEASON and p.round == 1 and p.original_team_id == worst_team_id
    )
    traded_pick.current_team_id = best_team_id

    _resolve_pick_numbers(state, snapshot=snapshot, tuning=DEFAULT_TUNING)

    # worst_team_id ranks 1st (worst) among 30 -> round-1 pick_number 1,
    # regardless of who currently owns the pick.
    assert traded_pick.pick_number == 1
    assert traded_pick.current_team_id == best_team_id
    assert traded_pick.original_team_id == worst_team_id


# ---------------------------------------------------------------------------
# --skip-games vs with-games -- every non-game-dependent field identical
# (report §5.4's own honesty claim, verified rather than trusted).
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_skip_games_matches_with_games_on_every_non_game_field():
    kwargs = dict(
        league_id=_LEAGUE_ID, rng_seed=13, first_season=_SEASON, seasons=1,
        players_per_team=15, tuning=DEFAULT_TUNING,
    )
    skip = run_seed(skip_games=True, **kwargs)
    full = run_seed(skip_games=False, **kwargs)

    s_skip, s_full = skip.seasons[0], full.seasons[0]
    assert s_skip.trades_by_window == s_full.trades_by_window
    assert s_skip.signings == s_full.signings
    assert s_skip.draft_picks == s_full.draft_picks
    assert s_skip.acquisitions == s_full.acquisitions
    assert s_skip.minutes_weighted_age_by_team == s_full.minutes_weighted_age_by_team
    assert s_skip.future_first_net_by_team == s_full.future_first_net_by_team
    skip_windows = [b.window_by_team for b in s_skip.boundaries]
    full_windows = [b.window_by_team for b in s_full.boundaries]
    assert skip_windows == full_windows
    assert s_skip.win_totals is None
    assert s_full.win_totals is not None
    # 30 teams x 82 games / 2 (each game counted once, not per side) = 1230
    # games; one win credited per game.
    assert sum(s_full.win_totals.values()) == 30 * 82 // 2


# ---------------------------------------------------------------------------
# 2-season run -- exercises the season-boundary progress_one_season(season=
# S+1) timing (report §5.3's own stated reasoning for why the ordering is
# load-bearing).
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_two_season_run_resolves_season1_expirings_via_resign_or_free_agency():
    state: HarnessState = generate_gm_league(
        league_id=_LEAGUE_ID, season=_SEASON, rng_seed=99, players_per_team=15
    )
    expiring_player_id = next(
        rp.player.id
        for rp in state.players.values()
        if rp.contract is not None
        and max(y.season for y in rp.contract.years) == _SEASON
    )
    # The full season-1-expiring cohort (not just expiring_player_id) --
    # needed for the discriminating check below, since any single player
    # may or may not get re-signed this season (whether they do is
    # rng-derived, orthogonal to the boundary-timing bug being tested).
    season1_expiring_ids = [
        rp.player.id
        for rp in state.players.values()
        if rp.contract is not None
        and max(y.season for y in rp.contract.years) == _SEASON
    ]

    season1_record = run_season(
        state, season=_SEASON, seed=99, skip_games=True, tuning=DEFAULT_TUNING,
        first_season=_SEASON,
    )

    # Discriminates the season-boundary's progress_one_season(season=S+1)
    # timing (report §5.3's own load-bearing ordering, restated in run_
    # season's own docstring): checked here, right after season 1 alone
    # and before season 2 runs, not just after both seasons complete. A
    # mutation to season=S (state.season pre-increment, i.e. still _SEASON
    # at this call site) fails to lapse ANY of these contracts at season
    # 1's own boundary -- its effect is a one-season-late expiry, which an
    # end-of-season-2-only check on one player can miss entirely if that
    # one player happened to get re-signed (a real contract update,
    # independent of the boundary call) rather than reaching free agency.
    # Checking the whole cohort right after season 1 removes the single-
    # player dependence, but the mutation's actual effect is subtler than
    # "every cohort member fails": rng_seed=99 re-signs 67/126 of this
    # cohort during season 1's own FREE_AGENCY, which runs BEFORE the
    # boundary call and is entirely unaffected by the mutation -- those 67
    # pass the loop below whether the boundary bug is present or not. Only
    # the 59/126 that reach FREE_AGENT actually discriminate (verified
    # directly). The explicit count below guards exactly that: without it,
    # a future rng/tuning change that re-signs the whole cohort would make
    # this loop pass vacuously under the mutation, silently reverting to
    # the exact gap this test exists to close.
    free_agent_count = 0
    for pid in season1_expiring_ids:
        mid_run_rp = state.players[pid]
        if mid_run_rp.player.roster_status is RosterStatus.ACTIVE:
            assert mid_run_rp.contract is not None
            assert max(y.season for y in mid_run_rp.contract.years) >= _SEASON + 1, (
                f"player {pid} still ACTIVE on a contract that ended {_SEASON} "
                "after season 1's own boundary already ran"
            )
        else:
            assert mid_run_rp.player.roster_status is RosterStatus.FREE_AGENT
            assert mid_run_rp.contract is None
            free_agent_count += 1
    assert free_agent_count > 0, (
        "expected at least one season-1-expiring player to reach FREE_AGENT "
        "rather than being re-signed -- only the FREE_AGENT branch actually "
        "discriminates the season=S+1 vs season=S boundary-timing bug (the "
        "re-signed branch passes under either); if this ever hits 0, this "
        "test has gone vacuous the same way the invariant-3 test did"
    )

    season2_record = run_season(
        state, season=_SEASON + 1, seed=99, skip_games=True, tuning=DEFAULT_TUNING,
        first_season=_SEASON,
    )

    assert state.season == _SEASON + 2
    final_rp = state.players[expiring_player_id]
    # Either re-signed (still ACTIVE, contract now covers >= season 2) or
    # correctly reached free agency (contract cleared, roster_status ==
    # FREE_AGENT) -- never left ACTIVE with a stale, already-expired
    # contract, which is exactly the ordering bug §5.3 warns against. Never
    # RETIRED either, though this asserts strictly FREE_AGENT rather than
    # allowing it: league.py's generation draw caps age at 19+18+1=38, and
    # _gm_progression_stub.py's retirement threshold is age > 40 (strict)
    # -- two season-boundary increments reaches at most 40, not past it, so
    # RETIRED is structurally unreachable in this 2-season span. A 3rd
    # season would need to account for it.
    if final_rp.player.roster_status is RosterStatus.ACTIVE:
        assert final_rp.contract is not None
        assert max(y.season for y in final_rp.contract.years) >= _SEASON + 1
    else:
        assert final_rp.player.roster_status is RosterStatus.FREE_AGENT
        assert final_rp.contract is None
        assert final_rp.player.team_id is None

    assert season1_record.season == _SEASON
    assert season2_record.season == _SEASON + 1
    assert season1_record.conservation_violations == ()
    assert season2_record.conservation_violations == ()


# ---------------------------------------------------------------------------
# Architect round 12 (docs/ledger/reports/phase-2-architect-round-12.md
# §5.5 test 7) -- the occupancy-balance fix's own headline harness-level
# regression, at the exact seed round 12/round 17's own diagnostic used.
# Written AFTER the real §5.6 measurement, against the measured value,
# per that memo's own explicit discipline (not a hoped-for number).
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_waiver_count_returns_to_the_no_trade_baseline_across_five_seasons():
    """§5.5 test 7. `rng_seed=7416152710283060417` (`rng_for(1,
    "validate_gm", "seed", 0).getrandbits(63)` -- the same seed architect
    round 12/round 17's own diagnostic used), 5 seasons, `skip_games=
    True`. Measured directly for this build (`artifacts/scratch_measure_
    postfix_12w.py`, checkpointed at `artifacts/validation_gm/postfix_
    12w_diagnostic/live/`, cross-checked by calling `run_seed` a second,
    independent time -- both give exactly 16):

    | variant | total waivers (5 seasons) | per season |
    |---|---|---|
    | pre-fix LIVE (round 17's diagnostic) | 18 | [3, 3, 3, 3, 6] |
    | STUBBED (round 17, zero trades at all) | 3 | [2, 0, 1, 0, 0] |
    | post-round-12-fix LIVE (this build) | **16** | [1, 3, 5, 3, 4] |

    **Tolerance, justified honestly -- this is NOT the "~3-6" figure round
    12 §5.6 predicted.** That prediction assumed the SLOT half of the
    anti-correlation (G3/G5, the half this fix closes) was the dominant
    channel; this measurement shows it is not the whole story on this
    seed. The companion M8a measurement (same run, `docs/ledger/phase-
    2.md`'s build-round entry) moved much closer to the STUBBED fixed
    point (season 2030: 0.9356 vs pre-fix 0.8883 vs STUBBED 0.9971) than
    the waiver count did (18 -> 16 is only 17% of the way to STUBBED's 3),
    consistent with round 12 §8 RR7's own framing: this fix closes the
    slot half, the room half (G2, slice 12-A, not built in this
    dispatch) is still open, and G4's occupancy-vs-trigger arithmetic
    does not by itself say which half dominates a specific seed's waiver
    count. Disclosed as a genuine, measured finding for the next
    architect round, not adjusted to fit round 12's own prediction.

    The assertion originally pinned the exact measured value (16) with a
    narrow +/-2 tolerance band, and separately asserted strict improvement
    over the pre-fix baseline (18).

    **Re-probed post slice 12-A (architect round 14, backend-dev round 49)
    -- moved again, deliberately, per the same discipline this file's own
    `rng_seed=6` trade-count canary already established: re-verify the
    thing this test still genuinely guards (that waivers still fire at
    all, are not zeroed out) before touching the pinned number, then
    update deliberately rather than silently widening the tolerance.**
    Re-measured twice, identically both times: **10** (was 16).

    **The mechanism, corrected (critic round 48 B4, fix round 50): `run_
    waivers`'s own trigger is next-season roster HEADCOUNT vs `roster_max`
    (`while active_count - cut > roster_max`, this function's own code
    above), never a salary/apron figure -- the prior docstring's claim
    that this movement traces to "the exact over-commitment `run_waivers`
    exists to correct" (an M9/M7 dollar-based framing) does not match what
    this function actually checks, and the cited M9/M7 movement is itself
    salary-based, not headcount-based; the magnitudes do not line up
    either (aggregate signings 1,445 -> 1,438 across the real 3-seed x
    5-season gate, waivers 50 -> 45 = -10%, versus -37.5% on this pinned
    seed). The plausible headcount-level channel -- disclosed as
    plausible, not exhaustively isolated to this one seed -- is that
    slice 12-A's hard `apron_2` gate (`_can_sign_at_all`, round 14 §5.2)
    and the `annual_salary <= bird_ceiling` conjunct (round 14 B3) can now
    each independently stop a signing from completing during free agency
    (a team excluded from signing at all, or an incumbent's Bird offer
    falling through to a smaller ladder rung it may not clear either);
    fewer completed signings league-wide means fewer teams cross `roster_
    max` by season's end, which is the only thing this function's `while`
    loop ever reacts to. This chain has not been traced team-by-team on
    this specific seed to confirm it is the dominant channel (a future
    round could instrument `run_waivers`'s own trigger and `run_free_
    agency`'s completed-signing count side by side, pre/post slice 12-A,
    on this seed, to close that gap) -- the value below is re-measured and
    stable regardless of which channel dominates.

    The assertion below pins the new measured value (10) with the same
    narrow +/-2 tolerance shape as before -- still a real regression guard
    on THIS mechanism, not a silent pass on "waivers exist in roughly the
    right ballpark." The "must show improvement" half is dropped -- it was
    specific to the occupancy-balance fix's own claim (round 12), not a
    property slice 12-A owes; a future slice could legitimately move this
    count either direction and the pinned band alone is what should catch
    it.

    **Kills:** any change to `_build_throw_ins`'s occupancy rule, `_next_
    season_roster_count`'s basis, `run_waivers`'s trigger, or `signing.py`/
    `gm_service.py`'s Bird-rights gate that would move this seed's waiver
    count materially off 10 without a deliberate, documented re-probe.

    **Re-probed post Slice 22-B** (architect round 23 §4.2-§4.3, Decision
    P2-42 -- `signing.py`'s `_YEARS_BASE_LO`/`_YEARS_BASE_HI` recalibrated
    1/4 -> 3/6). Per this docstring's own kill list above, this is exactly
    the class of change flagged to move this count, and it moved hard.
    (a) Cache-key guard re-confirmed first, per procedure: `test_propose_
    packages_populates_the_partners_cache_entries_with_the_partners_own_
    matrix` still passes, unaffected (it never touches `signing.py`). (b)
    Re-measured twice, identically both times: **23** (was 10), per-season
    `[1, 1, 10, 5, 6]`. **The mechanism, plausible and consistent with this
    slice's own stated intent, not exhaustively traced team-by-team**: this
    slice's own calibration target (see `test_slice_22b_coverage_restores_
    toward_season_1s_own_calibration_control` above) is a HIGHER mean
    next-season contract coverage -- i.e. more bodies carry real, multi-
    year contracts forward into later seasons than the pre-slice 1/4-year
    regime left behind. A fuller, longer-tenured book collides with
    `roster_max` more often once that season's own draft/re-signing/free-
    agency signings land on top of it, which is precisely what `run_
    waivers`'s own headcount trigger (`while active_count - cut > roster_
    max`, this function's own code above) reacts to -- more bodies staying
    under contract for more seasons means more seasons where the incoming
    class of new signings pushes a team over the ceiling, not fewer. This
    is the flip side of the same duration lever the M8a/M10 gate run
    reports on, not a new or independent regression. **Still not exhaustively
    isolated to this one seed** (a future round could instrument `run_
    waivers`'s own trigger and `run_free_agency`'s completed-signing count
    side by side, pre/post 22-B, on this seed, to close that gap) -- the
    value below is re-measured and stable regardless of which channel
    dominates.

    The assertion below pins the new measured value (23) with the same
    narrow +/-2 tolerance shape as every prior re-probe -- still a real
    regression guard on THIS mechanism, not a silent pass on "waivers
    exist in roughly the right ballpark." A future slice could legitimately
    move this count either direction and the pinned band alone is what
    should catch it."""
    record = run_seed(
        league_id=_LEAGUE_ID,
        rng_seed=7416152710283060417,
        first_season=_SEASON,
        seasons=5,
        players_per_team=15,
        skip_games=True,
        tuning=DEFAULT_TUNING,
    )
    total_waivers = sum(len(s.waivers) for s in record.seasons)
    assert 21 <= total_waivers <= 25, (
        f"post-Slice-22-B waiver count measured 23 at build time (backend-dev, Slice 22-B) -- "
        f"got {total_waivers} here; outside [21, 25] means either league.py's generation or "
        f"a downstream decision function changed since this figure was last verified, and "
        f"it needs re-probing, not silently widening the tolerance"
    )


# ---------------------------------------------------------------------------
# Architect round 11 (docs/ledger/reports/phase-2-architect-round-11.md
# §5.5 test 9) -- slice 12-A's own headline harness-level regression, the
# incumbent (Bird-rights) capacity bucket. Same seed as the sibling test
# above (round 12's own diagnostic seed, `rng_for(1, "validate_gm", "seed",
# 0).getrandbits(63)`), same discipline: written AFTER the real §5.6
# measurement, against the measured value, not a hoped-for number.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_league_committed_salary_book_stops_decaying_across_five_seasons():
    """§5.5 test 9. `rng_seed=7416152710283060417`, 5 seasons, `skip_games=
    True`. Measured directly for this build (backend-dev round 46,
    `artifacts/scratch_measure_12a.py`, checkpointed at `artifacts/
    validation_gm/slice_12a_gate/`), reading `committed_salary / salary_cap`
    at the `season_end` boundary per team-season, meaned per season --
    `compute_m8a`'s own exact basis (`tools/gm_harness/metrics.py`):

    | variant | season 2026 | 2027 | 2028 | 2029 | 2030 |
    |---|---|---|---|---|---|
    | pre-fix LIVE (round 15, this seed) | 1.1423 | -- | -- | -- | 0.8862 |
    | post-12-W STUBBED, no Bird bucket (round 12/17) | 1.1417 | 1.0114 | 0.9668 | 0.9900 | 0.9971 |
    | **post-12-A LIVE (this build)** | **1.2214** | 1.1255 | 1.1006 | 1.1225 | **1.0822** |

    **The decay is genuinely arrested, not merely slowed** -- pre-fix the
    book fell 0.1423 (season 2026's own excess over 1.0) most of the way to
    round 11 F4's derived ~0.78-0.83 fixed point by season 2030 (a 22.4%
    relative drop, 1.1423 -> 0.8862); post-fix the book stays in a
    1.08-1.13 band across seasons 2027-2030 and never approaches that old
    fixed point at all (season 2030's 1.0822 is 0.196 above the pre-fix
    figure at the identical season, and 0.22 above the OLD fixed point
    range's own ceiling). This is the claimed effect (round 11 §7: "M8b,
    M10 -- claimed, the whole point").

    **Disclosed honestly, not hidden by this test's own framing**: the
    book now sits ABOVE the design band's own ceiling (M8a's `[0.88,
    1.12]`, `seeds/gm_validation_bands.json`) in 4 of 5 seasons on this
    seed, and the 3-seed pooled run this same build round measured
    (`docs/ledger/phase-2.md`'s build-round entry) found season 2027 at
    1.1494 -- over round 12 §5.4 amendment 2's own 1.12 NO-GO ceiling.
    **This test is a decay-dynamics regression guard, not an M8a band-pass
    assertion** -- `validate_gm.py`'s own M8a metric is the correct,
    already-existing place the band itself is checked; conflating the two
    would silently launder a real overshoot into a green test here.

    Tolerance: +/-0.06 around the measured 1.0822 (season 2030) -- wide
    enough to absorb ordinary floating-point/dict-ordering noise, narrow
    enough to catch a future change that reintroduces material decay or a
    further overshoot on this seed. **Kills:** any regression to `signing.
    py::legal_capacity`'s incumbent branch, `gm_service.py`'s `_effective_
    capacity`/`_finalize_signing` Bird branches, or `run_free_agency`'s/
    `run_resign_window`'s incumbent-pricing wiring that would silently
    restore the pre-fix decay curve (season 2030 near 0.83-0.90) or, in
    the other direction, drive the book meaningfully further above the
    already-measured overshoot without a deliberate, documented re-probe.

    **Re-probed post Phase 3 §11 step 7** (the negotiation loop wired into
    `run_trade_window`, defaulting to enabled). **Expected to move, per
    the same reasoning `test_family1_invariant_3_...`'s own sixth re-probe
    already states**: a 5-season fixture now runs real negotiations in
    every trade window, and counters can only ever ADD executions relative
    to Phase 2 (never remove any -- round-0 mutual accepts are untouched).
    More trades per window, compounded over 5 seasons of `RESIGN_WINDOW`/
    `FREE_AGENCY` pricing against a book already reshaped by one more
    season's worth of trades, moves the season-2030 committed-salary book
    down from 1.0822 to **1.0055** (all five seasons measured: 1.1967,
    1.1147, 1.0235, 1.0201, 1.0055 -- still comfortably above 1.0 in every
    season, so the Bird bucket's own claim this test guards is unaffected;
    only the point estimate this test's own tolerance band is centered on
    moved). Re-centered here, not silently widened -- the same +/-0.06
    tolerance, on the new measured value."""
    record = run_seed(
        league_id=_LEAGUE_ID,
        rng_seed=7416152710283060417,
        first_season=_SEASON,
        seasons=5,
        players_per_team=15,
        skip_games=True,
        tuning=DEFAULT_TUNING,
    )
    from tools.gm_harness.metrics import _boundary, _salary_cap

    ratios_by_season: dict[int, list[float]] = {}
    for season_record in record.seasons:
        b = _boundary(season_record, "season_end")
        for _team_id, row in b.capsheet_by_team.items():
            cap = _salary_cap(row)
            if cap > 0:
                ratios_by_season.setdefault(season_record.season, []).append(
                    row.committed_salary / cap
                )
    means = {s: sum(rs) / len(rs) for s, rs in ratios_by_season.items()}
    final_season = _SEASON + 4
    book_at_season_5 = means[final_season]

    assert 0.9455 <= book_at_season_5 <= 1.1155, (
        f"season {final_season}'s book measured 1.0055 post Phase 3 §11 step 7's negotiation "
        f"loop -- got {book_at_season_5} here; outside [0.9455, 1.1155] means either "
        f"league.py's generation, the negotiation loop, or a downstream decision function "
        f"changed since this figure was last verified, and it needs re-probing, not "
        f"silently widening the tolerance"
    )
    # The core claim: the book must NOT have decayed back toward round 11
    # F4's derived pre-fix fixed point (~0.78-0.83) -- it must instead have
    # stayed comfortably above 1.0 across the whole 5-season run.
    assert all(means[s] > 1.0 for s in means), (
        "the whole point of the Bird bucket is that no season's book falls "
        "back toward the pre-fix ~0.78-0.83 fixed point -- if any season "
        "here is at or below 1.0, the fix has stopped working on this seed"
    )


# ---------------------------------------------------------------------------
# Decision P2-37 / architect round 18 §4 / orchestrator round 28's confirming
# measurement -- the M1 retirement-overcount fix. `_occupies_next_season`
# (gm_service.py) counted a player as occupying a next-season roster slot on
# contract-expiry grounds alone, never accounting for retirement at the
# season rollover; `_enforce_roster_floor` consumed that overcount at its
# most dangerous point (stopping exactly at `roster_min`, zero margin), so a
# team filled to floor by free agency could drop below it at the next
# rollover if it held the league's about-to-retire player. Confirmed live
# (round 28, causes (i)/(ii) eliminated, cause (iii) -- the retirement
# channel -- the smoking gun) at this exact reproducer.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_m1_is_clean_on_the_confirmed_retirement_overcount_reproducer():
    """Architect round 18 §4.6/§4.7, orchestrator round 28's confirming
    measurement. `rng_seed=2636832537024722024`, `need_gain=0.45` (a local
    `dataclasses.replace` on `DEFAULT_TUNING` -- never `tuning.py`'s real
    default, per this dispatch's own instruction; `need_gain` itself stays
    closed per architect round 18 §2.5), 5 seasons, `skip_games=True`.

    **Pre-fix (verified by orchestrator round 28's LIVE instrumented run,
    `docs/ledger/phase-2.md`'s round-28 row): 3 `compute_m1` violations**,
    all at team 28, season 2029, at `PRESEASON`/`post_trade_window_1`/
    `TRADE_DEADLINE` -- exactly the three ceiling-and-floor boundaries that
    precede that season's own free agency, matching architect round 18 §4.2
    Fact 1's accounting identity exactly (a team that enters a season below
    floor stays below floor until that season's free agency; nothing
    in-season can add a body). Team 28's PRESEASON roster size across all 5
    seasons on this seed: `15/15/15/13/15` -- the dip is exactly and only
    season 2029, the first season in which any retirement is arithmetically
    possible in this harness (max generated age 38 at `first_season=2026`;
    `is_retiring = age > 40`, `_gm_progression_stub.py`).

    **Post-fix: must be exactly 0.** `_occupies_next_season` now also
    excludes a player who will be strictly older than `ctx.retirement_age`
    at `next_season` (Decision P2-37), and `tools/gm_harness/state.py::
    build_ctx` populates `retirement_age` from this harness's own
    `_gm_progression_stub.py::_RETIREMENT_AGE`, so the fix is actually
    exercisable through this real `run_seed` call, not just unit-tested in
    isolation.

    **Kills:** any regression to `_occupies_next_season`'s retirement
    check, `_next_season_roster_count`'s threading of `retirement_age`, or
    `build_ctx`'s population of `GmContext.retirement_age` from the
    harness's own stub constant."""
    tuning = dataclasses.replace(DEFAULT_TUNING, need_gain=0.45)
    record = run_seed(
        league_id=_LEAGUE_ID,
        rng_seed=2636832537024722024,
        first_season=_SEASON,
        seasons=5,
        players_per_team=15,
        skip_games=True,
        tuning=tuning,
    )
    from tools.gm_harness.bands import load_bands
    from tools.gm_harness.metrics import compute_m1

    bands = load_bands()
    result = compute_m1((record,), bands)

    assert result.value == 0.0, (
        f"M1 must be exactly 0 on this seed post-P2-37 -- pre-fix this reproducer measured "
        f"3 violations (team 28, season 2029, PRESEASON/post_trade_window_1/TRADE_DEADLINE, "
        f"orchestrator round 28); got {result.value}, by-boundary breakdown: "
        f"{result.detail['violations_by_boundary']}"
    )


# ---------------------------------------------------------------------------
# Round 13 correction -- Phase 3 §11 step 11's own required verify
# deliverables (design doc line ~2938), against a REAL generated league at
# `rng_seed=1, players_per_team=15` (the same league every §7/§8.1 figure
# this repo cites -- `0.7709`/`0.7972`/`{0:780, 2:47, 4:43}` -- was measured
# against), never a hand-built `HeadToHead` row or a hardcoded constant.
# B1's own mutation-verify, and B3 items 1-3 (critic finding: three of this
# step's own required deliverables were never built).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def h2h_window_run():
    """One real trade window (`REGULAR_SEASON`) against a freshly generated
    league, `rng_seed=1`. Shared across every test in this section (module
    scope), matching `smoke_run`'s own convention above -- the ~5s
    generation + real trade-window pass runs once, not once per test."""
    from dba2.domain.phase import Phase
    from dba2.services.gm_service import run_trade_window

    state = generate_gm_league(
        league_id=_LEAGUE_ID, season=_SEASON, rng_seed=1, players_per_team=15,
        tuning=DEFAULT_TUNING,
    )
    snapshot = build_snapshot(state)
    ctx = build_ctx(state, phase=Phase.REGULAR_SEASON, tuning=DEFAULT_TUNING)
    return run_trade_window(snapshot, ctx)


@pytest.mark.slow
def test_far_reject_streak_real_corpus_histogram_matches_the_designs_own_cited_figures(
    h2h_window_run,
):
    """B1's own required mutation-verify, at real-corpus scale rather than
    only `test_head_to_head_writer.py`'s hand-built fixture arm (which
    drives exactly one hand-constructed streak of 4 -- genuinely-far
    rejects increment under BOTH the correct and the pre-fix buggy
    implementation, so that fixture alone cannot distinguish them; only
    real, unscripted data at scale can, because it is the only thing that
    reliably also exercises the give-less-axis "B accepts, A's own gap
    happens to be deeply negative" cell the bug actually inverted).

    Pre-fix (round 13's own B1 finding, measured on this exact seed): 73.1%
    of all `far_reject_streak` increments (1469 of 2010) fired on the WRONG
    branch (keyed off `respond()`'s own cell-3 "actor" -- whichever side is
    short -- rather than off `b_accepts` alone), inflating the `>= 4` row
    count to 323 against the correct 43. Post-fix, the full 870-row
    histogram is measured directly from the real writer and matches the
    design's own §7/§8.4-cited figures exactly."""
    from collections import Counter

    rows = h2h_window_run.head_to_head
    assert len(rows) == 870, "30 teams x 29 ordered partners"
    histogram = dict(Counter(row.far_reject_streak for row in rows.values()))
    assert histogram == {0.0: 780, 2.0: 47, 4.0: 43}
    ge_4 = sum(1 for row in rows.values() if row.far_reject_streak >= 4.0)
    assert ge_4 == 43, (
        "design's own §7/§8.4-cited figure -- pre-fix this measured 323 (7.5x inflated) "
        f"on this same seed; got {ge_4}"
    )


def test_p_bar_lands_near_the_both_legal_expectation_not_the_contaminated_one(h2h_window_run):
    """B3 item 2 -- §11 step 11's own required deliverable (design doc
    line ~2938): "the realised p_bar is compared against both the
    both-legal ... and all-packages ... expectations so a regression to
    the contaminated denominator is visible immediately." Both figures are
    computed from this real window's own raw output, never a hardcoded
    constant standing in for either: `p_bar` via `compute_h2h_base_accept_
    rate` (Decision P3-35's legal-only `opened`/`accepts_by_opponent`), and
    the contaminated comparator independently, straight off `outcome_
    census`'s own round-0 partition (`both_accept` + `A_reject_B_accept`,
    i.e. every round-0 B-accept over EVERY package scanned, illegal
    population included) -- a genuinely separate read of the same run's
    raw data, not a restatement of `p_bar` itself.

    A regression that let illegal packages leak back into `opened`/
    `accepts_by_opponent` (Decision P3-35's whole point) would pull `p_bar`
    toward the contaminated figure -- caught here directly and
    mechanically, on the same real run, every time this test runs."""
    from dba2.services.gm_service import compute_h2h_base_accept_rate

    result = h2h_window_run
    p_bar = compute_h2h_base_accept_rate(result.head_to_head)
    assert p_bar is not None

    census = result.outcome_census
    contaminated = (census["both_accept"] + census["A_reject_B_accept"]) / result.packages_evaluated

    # Pinned to the exact, deterministic figures this seed produces --
    # a real regression re-probe updates these numbers deliberately, per
    # this file's own "measured directly, not hoped-for" convention.
    assert p_bar == pytest.approx(0.7972283005105762, abs=1e-6)
    assert contaminated == pytest.approx(0.6281609195402298, abs=1e-6)

    both_legal_census_figure = 0.7709  # §1 item 10's 45-seed census, both-legal B-accept rate
    assert abs(p_bar - both_legal_census_figure) < 0.03
    assert abs(p_bar - contaminated) > 0.15, (
        "p_bar must sit far from the all-packages (illegal-contaminated) accept rate -- "
        "a collapse toward it is exactly Decision P3-35's excluded failure mode"
    )


def test_variant_flip_rate_is_measured_live_and_reported(h2h_window_run):
    """B3 item 1 -- §11 step 11's own required deliverable (design doc
    line ~2938): "the variant-1/variant-2 verdict-flip rate is measured
    live and reported." Diagnostic only (Decision P3-34 is withdrawn,
    nothing consumes this number downstream) -- this test only asserts the
    measurement actually happened and landed within the same order of
    magnitude as the design's own 86,362-pair citation (0.0544%-0.107%),
    not that it hits that figure exactly on one window's worth of data."""
    result = h2h_window_run
    assert result.variant_pairs_compared > 0
    flip_rate = result.variant_verdict_flips / result.variant_pairs_compared
    assert 0.0 <= flip_rate < 0.01, (
        f"flip rate {flip_rate:.4%} over {result.variant_pairs_compared} pairs is far outside "
        "the design's own ~0.05%-0.11% citation's order of magnitude"
    )


@pytest.mark.slow
def test_three_sample_sizes_at_season_5_from_a_real_5_season_run_not_a_hardcoded_e():
    """B3 item 3 -- §11 step 11's own required deliverable (design doc
    line ~2938): "§7's three sample sizes are each measured and reported
    separately at season 5 ... and the run's own `n_eff` is asserted to
    exceed its own `Σw`." The pre-existing coverage
    (`test_head_to_head_writer.py::test_three_sample_sizes_at_season_5_
    reproduce_the_designs_own_worked_table`) re-derives the closed-form
    algebra from a HARDCODED `E` constant -- close to a tautology, since
    `n_eff > Σw` follows purely from `Σd^k > Σd^{2k}` for `d < 1`
    regardless of whether this build's real decay/accumulation code is
    wired correctly at all. This test instead runs 5 REAL seasons
    (`rng_seed=1, players_per_team=15, skip_games=True`) and measures `Σw`/
    `Σw²`/`n_eff` from one real ordered pair's (`team_id=1, opponent_id=2`)
    OWN `opened` column, produced entirely by the real `_record_
    negotiation_in_h2h` writer and the real `memory.decay_head_to_head`
    applier -- exercising the actual production code path, not re-deriving
    algebra in isolation.

    **Reconstructing `Σw²` from a single running total.** `HeadToHead`
    persists only the CURRENT decayed total (`opened`), never each
    individual event's own age -- but decay commutes with addition, so the
    raw, undecayed events `opened` this season (`delta_S`) are recoverable
    from consecutive real snapshots: `opened_S = (opened_{S-1} + delta_S) *
    H2H_DECAY`, i.e. `delta_S = opened_S / H2H_DECAY - opened_{S-1}`.
    `Σw` (this row's own decayed mass) is then just `opened_5` directly.
    `Σw²` sums each SEASON's own `delta_S` individual unit-weight events at
    their current squared decay (`delta_S * H2H_DECAY ** (2 * age_S)`) --
    `delta_S` to the FIRST power, per §7's own worked-table formula
    (`E · (1 − d^{2n})/(1 − d²)`, not `E² · (...)`), since each of a
    season's `delta_S` events is its own independent unit-weight draw, not
    one lump of magnitude `delta_S`.

    **This harness's own decay call runs at the END of the season whose
    events it is decaying** (`season.py`'s season-boundary step), not at
    the START of the next one -- so the most recent season's own
    contribution here already carries one extra factor of `H2H_DECAY`
    relative to §7's own idealized table (whose `Σw`/`Σw²` formulas assume
    the newest season is unweighted). `n_eff` is scale-invariant under a
    uniform reweighting of every season by the same factor, so this shift
    changes the measured `Σw`/`Σw²` figures but not the qualitative
    `n_eff > Σw` property under test -- disclosed here rather than quietly
    reconciled against the design doc's own table, which was never a
    byte-for-byte target for a REAL run."""
    from dba2.managers import memory

    pair = (1, 2)
    d = memory.H2H_DECAY

    state = generate_gm_league(
        league_id=_LEAGUE_ID, season=_SEASON, rng_seed=1, players_per_team=15,
        tuning=DEFAULT_TUNING,
    )
    opened_by_season: list[float] = []
    for i in range(5):
        run_season(
            state, season=_SEASON + i, seed=1, skip_games=True, tuning=DEFAULT_TUNING,
            first_season=_SEASON,
        )
        row = state.head_to_head.get(pair)
        opened_by_season.append(row.opened if row is not None else 0.0)

    # **Re-pinned 2026-09-11 (a real regression re-probe update, exactly
    # the case this test's own docstring/assertion message anticipated).**
    # The prior series ([4.8, 7.68, 9.408, ...], flat raw deltas of 8 every
    # season) was pinned at `ad1ca3f` (§11 step 11), before channel 1's
    # per-partner target-count ladder (`memory.target_count_for_pair`,
    # Decision P3-40, §8.4, built at §11 step 17 / commit `1b9ea20`)
    # existed. That ladder changed `propose_packages`'s own per-window
    # target count for a pair from the constant `TRADE_TARGETS_PER_PARTNER
    # = 2` to a value that varies with the pair's own trade history --
    # exactly what channel 1 exists to do -- so a flat-8 series is no
    # longer the correct expectation for ANY real pair, by design, not by
    # regression. Root-caused via git-worktree bisection across every
    # intervening commit; directly confirmed causal (not merely
    # correlated) by monkeypatching `_channel1_target_count` back to the
    # pre-ladder constant at HEAD, which restores the exact old series.
    # For this pair specifically: team 1 executed a trade with team 2 in
    # window 1 of season 1 (2 targets x 2 outgoing variants = 4 packages),
    # which sets `executions = 1.0` for the pair -- satisfying the
    # ladder's `up` rung in window 2 (`executions >= 1` and the `down`
    # condition not holding) for **3** targets that window instead of 2,
    # i.e. 6 packages -- 4 + 6 = 10 raw opens in season 1, not 8. Season
    # 5's own delta (6, the one season below the old flat baseline) is the
    # ladder's `down` rung firing for this same pair later in the run --
    # this corpus now exercises both directions of channel 1 for this
    # pair, which a hardcoded flat expectation could never represent.
    assert opened_by_season == pytest.approx(
        [6.0, 8.4, 9.839999999999998, 10.703999999999997, 10.022399999999998]
    ), "measured directly for this build, rng_seed=1 -- a real regression re-probe updates this"

    deltas: list[float] = []
    previous = 0.0
    for opened in opened_by_season:
        deltas.append(opened / d - previous)
        previous = opened
    # No longer a flat [8.0]*5 -- see the correction above. A constant
    # per-season count would now be evidence AGAINST channel 1 being
    # wired (its whole point is a target count that responds to a pair's
    # own trade history), so this assertion checks the real, adaptive
    # series instead of re-asserting flatness.
    assert deltas == pytest.approx([10.0, 8.0, 8.0, 8.0, 6.0]), (
        "this pair's own real per-season raw event count, reconstructed from real snapshots -- "
        "no longer flat now that channel 1's target-count ladder (Decision P3-40) is live"
    )

    n_seasons = len(opened_by_season)
    sigma_w = opened_by_season[-1]
    sigma_w2 = sum(
        delta * d ** (2 * (n_seasons + 1 - season_index))
        for season_index, delta in enumerate(deltas, start=1)
    )
    n_eff = sigma_w**2 / sigma_w2

    assert sigma_w == pytest.approx(10.022399999999998)
    assert sigma_w2 == pytest.approx(3.7648834559999997)
    assert n_eff == pytest.approx(26.680374820080477)
    assert n_eff > sigma_w, (
        f"n_eff ({n_eff}) must strictly exceed Σw ({sigma_w}) on this REAL run's own decayed "
        "head-to-head data -- round 3's own B-4 error, guarded here against the real code path"
    )


# ---------------------------------------------------------------------------
# Channel 3 (Decision P3-26/§8.2, §11 step 12) -- the counter-band shade,
# measured on a REAL, memory-aged corpus. `h2h_window_run` above is
# window 1 -- `ctx.head_to_head` is always empty there (no prior window
# has ever written to it), so `shade` is `0.0` at every one of its sites
# by construction and cannot exercise channel 3 at all. These tests
# instead age `state` through 4 REAL prior seasons first (rng_seed=1,
# skip_games=True, mirroring `test_three_sample_sizes_at_season_5...`
# above), then run ONE MORE real trade window against the resulting,
# genuinely nonzero `ctx.head_to_head`/`ctx.h2h_base_accept_rate`.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def h2h_late_window():
    """A REAL trade window, `ctx.head_to_head` aged over 4 real prior
    seasons -- `dba2.managers.memory.shade` is monkeypatched to a spy
    (never a stub: the real formula still runs) so every value it computes
    is captured directly, giving a precise, real measurement of channel
    3's realised shade/band range without adding any new `TradeWindowResult`
    field beyond the pooled sum/count this step already ships.

    **B3 fix (critic round) -- `observed_shades` now mixes TWO populations,
    in call order.** `run_trade_window` calls `memory.shade` at every real
    per-turn ch3 decision site FIRST (inside the negotiation loop, one call
    per site, `channel3_decision_sites` of them), then ONCE MORE per
    head-to-head ROW, after the loop, inside `_channel3_mean_abs_shade_
    inputs` (`channel3_abs_shade_row_count` of them, §9.2's `mean_abs_
    shade` population -- see that function's own docstring for why the
    population changed). `observed_shades[:channel3_decision_sites]` is
    the decision-site population; `observed_shades[channel3_decision_
    sites:]` is the row population -- the two tests below split on that
    boundary rather than treating `observed_shades` as one list."""
    from unittest import mock

    from dba2.domain.phase import Phase
    from dba2.managers import memory
    from dba2.services import gm_service

    state = generate_gm_league(
        league_id=_LEAGUE_ID, season=_SEASON, rng_seed=1, players_per_team=15,
        tuning=DEFAULT_TUNING,
    )
    for i in range(4):
        run_season(
            state, season=_SEASON + i, seed=1, skip_games=True, tuning=DEFAULT_TUNING,
            first_season=_SEASON,
        )
    snapshot = build_snapshot(state)
    ctx = build_ctx(state, phase=Phase.REGULAR_SEASON, tuning=DEFAULT_TUNING)
    assert ctx.head_to_head, "fixture construction error -- 4 real seasons must leave real memory"
    assert ctx.h2h_base_accept_rate is not None

    observed_shades: list[float] = []
    real_shade = memory.shade

    def _spy_shade(head_to_head, p_bar):
        value = real_shade(head_to_head, p_bar)
        observed_shades.append(value)
        return value

    with mock.patch.object(gm_service.memory, "shade", side_effect=_spy_shade):
        result = gm_service.run_trade_window(snapshot, ctx)
    return snapshot, ctx, result, observed_shades


@pytest.mark.slow
def test_channel3_decision_site_count_matches_the_real_number_of_shade_calls(h2h_late_window):
    """`TradeWindowResult.channel3_decision_sites` must equal exactly the
    number of real per-turn `memory.shade` calls this window made -- the
    spy in the fixture above and this build's own tally are two
    independent counts of the same real run; a mismatch means the tally
    is over- or under-counting decision sites relative to the code path
    that actually computes shade.

    **B3 fix (critic round) -- `observed_shades` also carries one further
    `memory.shade` call per head-to-head ROW** (`_channel3_mean_abs_
    shade_inputs`, called once per window after the negotiation loop,
    never per decision site -- see the fixture's own docstring), so the
    raw call count is `channel3_decision_sites + channel3_abs_shade_
    row_count`, not `channel3_decision_sites` alone."""
    _snapshot, _ctx, result, observed_shades = h2h_late_window
    assert result.channel3_decision_sites > 0, "a real 30-team window must reach cell 3 somewhere"
    assert result.channel3_abs_shade_row_count > 0, "a 5-season-aged corpus must have real rows"
    assert len(observed_shades) == (
        result.channel3_decision_sites + result.channel3_abs_shade_row_count
    )


@pytest.mark.slow
def test_channel3_realised_shade_and_band_range_on_a_real_5_season_corpus(h2h_late_window):
    """§11 step 12's own verify: `COUNTER_BAND_MULT_eff` measured and
    reported on a real run, expected to skew toward `[2.25, ~3.223]` at
    the measured `p_bar` (§8.2's own worked bound) rather than the
    theoretical `[2.25, 3.75]` -- because `shade`'s own positive ceiling
    is capped well below `+1` by `p_bar`'s asymmetry (§8.1) whenever
    `p_bar != 0.5`, which every real measured window is."""
    from dba2.managers import negotiation

    _snapshot, ctx, result, observed_shades = h2h_late_window
    p_bar = ctx.h2h_base_accept_rate
    assert 0.75 <= p_bar <= 0.82, (
        f"p_bar={p_bar} outside the design's own cited 0.7709-0.7972 measured window"
    )
    assert all(-1.0 <= s <= 1.0 for s in observed_shades)

    # B3 fix (critic round) -- split `observed_shades` on the fixture's own
    # documented call-order boundary: decision-site shades drive `band_eff`
    # (this test's own multiplier-range check below), row shades drive
    # `mean_abs_shade` (§9.2's own population, "the 870 head-to-head rows
    # themselves") -- the two are DIFFERENT populations and must not be
    # averaged together.
    decision_site_shades = observed_shades[: result.channel3_decision_sites]
    row_shades = observed_shades[result.channel3_decision_sites :]
    assert len(row_shades) == result.channel3_abs_shade_row_count

    multipliers = [
        negotiation.COUNTER_BAND_MULT * (1.0 + negotiation.H2H_BAND_SHADE * s)
        for s in decision_site_shades
    ]
    lo, hi = min(multipliers), max(multipliers)
    assert 2.25 - 1e-9 <= lo, f"realised floor {lo} must not clear the theoretical 2.25 floor"
    assert hi <= 3.75 + 1e-9, f"realised ceiling {hi} must not exceed the theoretical 3.75 ceiling"
    assert hi <= 3.25, (
        f"realised widening arm {hi} exceeds §8.2's own worked ~3.223 bound at this p_bar"
    )

    mean_abs_shade = result.channel3_abs_shade_sum / result.channel3_abs_shade_row_count
    assert mean_abs_shade == pytest.approx(
        sum(abs(s) for s in row_shades) / len(row_shades)
    )
    assert result.channel3_reachability <= result.channel3_decision_sites
    assert result.channel3_flips <= result.channel3_decision_sites

    print(
        f"[channel 3] real 5-season-aged window: p_bar={p_bar:.6f} "
        f"decision_sites={result.channel3_decision_sites} "
        f"reachability={result.channel3_reachability} flips={result.channel3_flips} "
        f"mean_abs_shade={mean_abs_shade:.6f} (over {result.channel3_abs_shade_row_count} "
        "head-to-head rows) "
        f"realised COUNTER_BAND_MULT_eff range=[{lo:.4f}, {hi:.4f}]"
    )


@pytest.mark.slow
def test_channel3_head_to_head_none_is_a_strict_no_op_on_the_flip_counter(h2h_late_window):
    """The same "no memory -> exactly Phase 2/pre-step-11 behavior"
    invariant every prior step verified, extended to channel 3's own new
    counters: replaying the SAME aged snapshot/ctx with `head_to_head`
    forced to `None` must read zero decision-site shading anywhere --
    `channel3_reachability`/`channel3_flips`/`channel3_abs_shade_sum` all
    exactly `0`, `channel3_decision_sites` unchanged (the SITES are still
    visited -- see `test_channel3_decision_site_count...` above for why a
    positive site count alongside a zero flip count is a measurement, not
    an absence)."""
    from dba2.services import gm_service

    snapshot, ctx, _result_with_memory, _ = h2h_late_window
    ctx_no_memory = dataclasses.replace(ctx, head_to_head=None, h2h_base_accept_rate=None)
    # The SAME real, memory-aged snapshot as the fixture's own live-memory
    # pass -- `run_trade_window` is a pure function of its arguments (see
    # the module-scope purity tests in test_head_to_head_writer.py), so
    # replaying it against the identical snapshot with only `ctx.head_to_
    # head` forced to `None` isolates channel 3's own read of that one
    # field, with nothing else about the corpus changing.
    result_no_memory = gm_service.run_trade_window(snapshot, ctx_no_memory)
    assert result_no_memory.channel3_decision_sites > 0
    assert result_no_memory.channel3_reachability == 0
    assert result_no_memory.channel3_flips == 0
    assert result_no_memory.channel3_abs_shade_sum == 0.0
    assert result_no_memory.channel3_abs_shade_row_count == 0


# ---------------------------------------------------------------------------
# Channel 2 (Decision P3-28's replacement/§8.3, §11 step 13) -- the counter
# self-margin, measured on the SAME real, memory-aged corpus channel 3's own
# tests above already build (`h2h_late_window`, module-scoped -- reusing it
# costs nothing extra). §11 step 13's own note: this step's non-vacuous zero-
# reading (the head-to-head-`None` pass) must come from THIS counters-on
# pass, never the `max_negotiation_rounds=0` control pass, where `build_
# counter` never runs at all and channel 2 has zero decision sites
# vacuously.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_channel2_decision_sites_and_bounded_self_margin_on_a_real_5_season_corpus(
    h2h_late_window,
):
    """`TradeWindowResult.channel2_decision_sites` must be positive on a
    real, memory-aged 30-team window (real `COUNTER` stances do occur),
    and every REAL `self_margin` `build_counter` was called with must
    satisfy §8.3's own bound, `self_margin ∈ [0, 0.25 * threshold]`
    (`H2H_MARGIN_SHADE = 0.25`) -- measured directly by spying `gm_
    service.build_counter` (the real function still runs; only the
    `self_margin` it was called with, plus the acting side's own record,
    is captured) rather than trusted from the tally alone."""
    from unittest import mock

    from dba2.managers import negotiation
    from dba2.services import gm_service

    snapshot, ctx, _result, _observed_shades = h2h_late_window

    real_build_counter = gm_service.build_counter
    captured: list[tuple[float, float]] = []  # (self_margin, actor_record.threshold)

    def _spy_build_counter(package, record_a, record_b, axis, **kwargs):
        self_margin = kwargs.get("self_margin", 0.0)
        actor_record = record_a if axis == "give_less" else record_b
        captured.append((self_margin, actor_record.threshold))
        return real_build_counter(package, record_a, record_b, axis, **kwargs)

    with mock.patch.object(gm_service, "build_counter", side_effect=_spy_build_counter):
        result = gm_service.run_trade_window(snapshot, ctx)

    assert result.channel2_decision_sites > 0, "a real 5-season-aged window must reach COUNTER"
    assert len(captured) == result.channel2_decision_sites
    assert result.channel2_reachability <= result.channel2_decision_sites
    assert result.channel2_flips <= result.channel2_decision_sites

    for self_margin, threshold in captured:
        assert 0.0 <= self_margin, "the max(shade, 0.0) floor -- self_margin is never negative"
        assert self_margin <= negotiation.H2H_MARGIN_SHADE * threshold + 1e-9

    flip_rate = (
        result.channel2_flips / result.channel2_reachability
        if result.channel2_reachability > 0 else None
    )
    print(
        f"[channel 2] real 5-season-aged window: decision_sites={result.channel2_decision_sites} "
        f"reachability={result.channel2_reachability} flips={result.channel2_flips} "
        f"flip_rate={flip_rate}"
    )


@pytest.mark.slow
def test_channel2_head_to_head_none_is_a_strict_no_op_on_self_margin(h2h_late_window):
    """The same "no memory -> exactly Phase 2/pre-step-13 behavior"
    invariant every prior step verified, extended to channel 2's own new
    counters: replaying the SAME aged snapshot/ctx with `head_to_head`
    forced to `None` must read `self_margin == 0.0` at every real `build_
    counter` call (`channel2_reachability`/`channel2_flips` both exactly
    `0`), while `channel2_decision_sites` stays positive -- the SITES are
    still visited (real `COUNTER` stances still occur; channel 3's own
    live shading, unrelated to channel 2, can change which turns reach
    `COUNTER` at all, so this count is not asserted equal to the with-
    memory pass's own count -- only positive)."""
    import dataclasses as _dataclasses
    from unittest import mock

    from dba2.services import gm_service

    snapshot, ctx, _result_with_memory, _ = h2h_late_window
    ctx_no_memory = _dataclasses.replace(ctx, head_to_head=None, h2h_base_accept_rate=None)

    real_build_counter = gm_service.build_counter
    captured_self_margins: list[float] = []

    def _spy_build_counter(package, record_a, record_b, axis, **kwargs):
        captured_self_margins.append(kwargs.get("self_margin", 0.0))
        return real_build_counter(package, record_a, record_b, axis, **kwargs)

    with mock.patch.object(gm_service, "build_counter", side_effect=_spy_build_counter):
        result_no_memory = gm_service.run_trade_window(snapshot, ctx_no_memory)

    assert result_no_memory.channel2_decision_sites > 0
    assert len(captured_self_margins) == result_no_memory.channel2_decision_sites
    assert all(m == 0.0 for m in captured_self_margins)
    assert result_no_memory.channel2_reachability == 0
    assert result_no_memory.channel2_flips == 0


# ---------------------------------------------------------------------------
# Decision P3-48 (§11 step 17 follow-up) -- `head_to_head_enabled`, threaded
# `run_seed -> run_season -> build_ctx`, exercised end to end through the
# real harness entry point rather than only at `build_ctx` in isolation
# (`test_state.py::test_build_ctx_head_to_head_enabled_false_suppresses_
# reader_not_writer` covers that unit-level property).
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_run_seed_head_to_head_enabled_false_yields_real_non_vacuous_zero_channel1_flips():
    """The channel-1 mirror of `test_channel1_real_non_vacuous_zero_at_
    head_to_head_none` (`tests/unit/services/test_head_to_head_writer.py`)
    -- but driven through `run_seed` itself, at `max_negotiation_rounds=0`,
    the exact shape `validate_gm.py`'s own control-check block and
    `generate_gm_decision_log_digest.py` now both use (Decision P3-48).
    Every ordered pair/window is a channel-1 decision site regardless of
    negotiation activity, so `channel1_up_decision_sites`/`channel1_down_
    decision_sites` must be positive even at round 0 -- and with `head_to_
    head_enabled=False` forcing `ctx.head_to_head=None` at every `build_
    ctx` call this run makes, both branches must read exactly `0` flips: a
    REAL zero over many real sites, not the vacuous "0 out of 0" a
    mechanism-absent corpus would produce."""
    record = run_seed(
        league_id=_LEAGUE_ID, rng_seed=_SEED, first_season=_SEASON, seasons=1,
        players_per_team=15, skip_games=True, tuning=DEFAULT_TUNING,
        max_negotiation_rounds=0, head_to_head_enabled=False,
    )
    season = record.seasons[0]
    up_sites = sum(season.channel1_up_decision_sites)
    down_sites = sum(season.channel1_down_decision_sites)
    up_flips = sum(season.channel1_up_flips)
    down_flips = sum(season.channel1_down_flips)

    assert up_sites > 0
    assert down_sites > 0
    assert up_flips == 0
    assert down_flips == 0


@pytest.mark.slow
def test_run_seed_head_to_head_enabled_defaults_true_and_memory_still_accumulates():
    """Default-preservation guard, two levels. `head_to_head_enabled`
    defaults `True` everywhere -- protecting against a future edit that
    quietly flips the default to `False` to make some other test or gate
    read green.

    **Level 1 -- the writer.** `run_season`, called with the flag omitted,
    against a freshly generated (empty) league, must still leave `state.
    head_to_head` real and nonzero -- the same direct-state-inspection
    style `test_three_sample_sizes_at_season_5_from_a_real_5_season_run_
    not_a_hardcoded_e` already uses above.

    **Level 2 -- the reader, at the exact call shape a control/digest run
    uses.** `run_seed`, called with the flag omitted at `max_negotiation_
    rounds=0` over 2 seasons (the committed Decision P3-29(b) baseline's
    own shape), must produce real, nonzero channel-1 flips -- `compute_
    m28`'s own docstring records this exact pre-suppression baseline as
    `channel1_down_flips=130`, `channel1_up_flips=47`; asserted here only
    as `> 0`, not pinned to the exact figure, so this test does not become
    a second, brittle copy of that regression value. A default silently
    flipped to `False` would make BOTH assertions below fail."""
    state = generate_gm_league(
        league_id=_LEAGUE_ID, season=_SEASON, rng_seed=_SEED, players_per_team=15,
        tuning=DEFAULT_TUNING,
    )
    assert state.head_to_head == {}, "fixture construction error -- a fresh league starts empty"

    run_season(state, season=_SEASON, seed=_SEED, skip_games=True, tuning=DEFAULT_TUNING,
               first_season=_SEASON)

    assert state.head_to_head, (
        "state.head_to_head must accumulate real rows on an ordinary run -- head_to_head_"
        "enabled's default must stay True, never silently flipped to suppress the writer"
    )
    assert any(row.opened > 0 for row in state.head_to_head.values())

    record = run_seed(
        league_id=_LEAGUE_ID, rng_seed=_SEED, first_season=_SEASON, seasons=2,
        players_per_team=15, skip_games=True, tuning=DEFAULT_TUNING,
        max_negotiation_rounds=0,
    )
    up_flips = sum(s for season in record.seasons for s in season.channel1_up_flips)
    down_flips = sum(s for season in record.seasons for s in season.channel1_down_flips)
    assert up_flips > 0 or down_flips > 0, (
        "a default-enabled run must read real, non-suppressed head-to-head memory in channel "
        "1 even at max_negotiation_rounds=0 -- head_to_head_enabled's default must stay True"
    )
