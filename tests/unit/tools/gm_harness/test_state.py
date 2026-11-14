"""`tools/gm_harness/state.py` -- `build_snapshot`/`build_ctx` against a
hand-built `HarnessState`, and the five in-memory appliers (`apply_waiver_
inmem`, T7a2), mirroring T4's own applier-test style (`tests/integration/
test_gm_service.py`) minus any DB dependency -- every test here is
pure/in-memory.
"""
from __future__ import annotations

import dataclasses

import pytest

from dba2.domain.contract import ContractStatus, ContractType, SigningMethod
from dba2.domain.draft import DraftPick, ProtectionType
from dba2.domain.phase import Phase
from dba2.domain.player import RosterStatus
from dba2.domain.team import GmPhilosophy, Team, Timeline
from dba2.managers import capsheet
from dba2.managers.decisions import draft as draft_decisions
from dba2.managers.explain import DecisionRecord
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow
from dba2.services.gm_service import (
    SigningExecution,
    TradeExecution,
    WaiverExecution,
    WindowPassResult,
)
from tests.fixtures.managers import CAP_SETTINGS, ROOKIE_SCALE, make_contract, make_roster_player
from tools.gm_harness.state import (
    HarnessState,
    _aggregate_capsheet_inputs,
    apply_draft_inmem,
    apply_signing_inmem,
    apply_trade_inmem,
    apply_waiver_inmem,
    apply_windows_inmem,
    build_ctx,
    build_snapshot,
)

_LEAGUE_ID = 1
_SEASON = 2026


def _team(team_id: int, philosophy: GmPhilosophy) -> Team:
    return Team(
        id=team_id,
        league_id=_LEAGUE_ID,
        code=f"T{team_id}",
        name=f"Team{team_id}",
        city=f"City{team_id}",
        conference="East",
        division="Atlantic",
        manager_user_id=None,
        gm_philosophy=philosophy,
        timeline=Timeline.RETOOL,
        home_market_size=3,
    )


def _dummy_record(team_id: int) -> DecisionRecord:
    return DecisionRecord(
        team_id=team_id,
        philosophy=GmPhilosophy.EGALITARIAN,
        window=CompetitiveWindow.RETOOL,
        subject=f"team:{team_id}",
        verdict="test",
        score=0.0,
        threshold=0.0,
        terms=(),
    )


def _cap_settings_by_season() -> dict[int, object]:
    return {
        _SEASON: dataclasses.replace(CAP_SETTINGS, season=_SEASON),
        _SEASON + 1: dataclasses.replace(CAP_SETTINGS, season=_SEASON + 1),
        _SEASON + 2: dataclasses.replace(CAP_SETTINGS, season=_SEASON + 2),
        _SEASON + 3: dataclasses.replace(CAP_SETTINGS, season=_SEASON + 3),
        _SEASON + 4: dataclasses.replace(CAP_SETTINGS, season=_SEASON + 4),
    }


def _grown_hold_season_settings():
    """The real `hold_season_settings` basis `_aggregate_capsheet_inputs`
    now derives (round 52 N2 fix) -- `capsheet.project_cap_settings(state.
    cap_settings_by_season[_SEASON], _SEASON + 1, tuning)`, i.e. genuinely
    grown by one season of `DEFAULT_TUNING.cap_growth`, not the flat
    `_cap_settings_by_season()` dict row at `_SEASON + 1` (which this
    fixture's `_make_hold_state`-based tests never read since N2 -- that
    dict-row lookup was exactly the bug). Every assertion below that used
    to compare against raw `CAP_SETTINGS` figures now compares against
    this grown row instead, matching what the fixed production code
    actually uses."""
    return capsheet.project_cap_settings(CAP_SETTINGS, _SEASON + 1, tuning=DEFAULT_TUNING)


def _make_state() -> HarnessState:
    """Two teams, one rostered/contracted player each, one draft pick
    owned by team 1, no prospects (individual applier tests add their own
    where needed)."""
    teams = {
        1: _team(1, GmPhilosophy.STAR_MAXER),
        2: _team(2, GmPhilosophy.EGALITARIAN),
    }
    contract_a = make_contract(
        101, team_id=1, start_season=_SEASON - 1, salaries=(9_000_000, 9_500_000)
    )
    player_a = make_roster_player(
        101, age=27, season=_SEASON, overall_cached=82, team_id=1, contract=contract_a
    )
    contract_b = make_contract(201, team_id=2, start_season=_SEASON, salaries=(6_000_000,))
    player_b = make_roster_player(
        201, age=24, season=_SEASON, overall_cached=70, team_id=2, contract=contract_b
    )
    pick = DraftPick(
        id=1,
        league_id=_LEAGUE_ID,
        season=_SEASON,
        round=1,
        original_team_id=1,
        current_team_id=1,
        pick_number=None,
        used_for_player_id=None,
        protection_type=ProtectionType.NONE,
        protection_through=None,
        swap_with_pick_id=None,
        conveyed=False,
        rolls_over_to_season=None,
    )
    return HarnessState(
        teams=teams,
        players={101: player_a, 201: player_b},
        prospects={},
        picks={1: pick},
        cap_settings_by_season=_cap_settings_by_season(),
        rookie_scale=ROOKIE_SCALE,
        window_by_team={1: CompetitiveWindow.WIN_NOW, 2: CompetitiveWindow.RETOOL},
        league_id=_LEAGUE_ID,
        season=_SEASON,
        rng_seed=1,
        next_contract_id=1000,
        next_pick_id=2,
        next_player_id=1000,
    )


# ---------------------------------------------------------------------------
# build_snapshot / build_ctx
# ---------------------------------------------------------------------------


def test_build_snapshot_matches_load_gm_inputs_roster_and_pick_definition():
    state = _make_state()
    snapshot = build_snapshot(state)
    assert {t.id for t in snapshot.teams} == {1, 2}
    team1 = snapshot.team_by_id(1)
    assert [rp.id for rp in team1.roster] == [101]
    assert [p.id for p in team1.draft_picks] == [1]
    team2 = snapshot.team_by_id(2)
    assert [rp.id for rp in team2.roster] == [201]
    assert team2.draft_picks == ()
    assert team1.window is CompetitiveWindow.WIN_NOW
    assert team2.window is CompetitiveWindow.RETOOL


def test_build_snapshot_excludes_non_active_roster_statuses():
    state = _make_state()
    state.players[101].player.roster_status = RosterStatus.INJURED
    snapshot = build_snapshot(state)
    assert snapshot.team_by_id(1).roster == ()


def test_build_ctx_room_by_team_and_philosophy():
    state = _make_state()
    ctx = build_ctx(state, phase=Phase.PRESEASON)
    assert ctx.philosophy_by_team == {1: GmPhilosophy.STAR_MAXER, 2: GmPhilosophy.EGALITARIAN}
    assert set(ctx.room_by_team) == {1, 2}
    assert ctx.cap_settings.season == _SEASON
    assert ctx.next_season_cap_settings.season == _SEASON + 1
    assert ctx.season == _SEASON
    assert ctx.phase is Phase.PRESEASON
    # team 1 committed 9.5M next season (year 2 of a 2-year deal) plus a
    # 14-roster_min filler floor for its other 13 "missing" bodies (this
    # hand-built league only carries one player per team) -- room should
    # be materially below the full cap, not equal to it.
    assert ctx.room_by_team[1] < CAP_SETTINGS.salary_cap


def test_build_ctx_room_by_team_reflects_dead_cap_by_team_season():
    """B2 fix: `_aggregate_capsheet_inputs`'s own `dead_cap=state.dead_cap_
    by_team_season.get(key, 0)` wiring had zero discriminating coverage --
    critic reverted it to a hardcoded `dead_cap=0` and the full `pytest -q
    tests/unit/` suite passed unchanged (byte-identical), proving nothing
    exercised this read. A nonzero dead-cap charge at `(team_id, season +
    1)` (the same season `build_ctx`'s own `room_by_team` reads, via
    `project_team_cap_sheet`'s `p.season == state.season + 1` selection)
    must reduce `room_by_team[team_id]` by EXACTLY that amount --
    `compute_team_season_cap_sheet`'s own formula (`used = committed_
    salary + dead_cap + minimum_filler + ...`, `room = salary_cap - used`)
    is a direct dollar-for-dollar subtraction, not merely "some lower
    number," so the equality assertion below is the real, precise
    discriminator, not a loose bound."""
    state = _make_state()
    ctx_before = build_ctx(state, phase=Phase.PRESEASON)
    room_before = ctx_before.room_by_team[1]

    state.dead_cap_by_team_season[(1, _SEASON + 1)] = 4_000_000
    ctx_after = build_ctx(state, phase=Phase.PRESEASON)
    room_after = ctx_after.room_by_team[1]

    assert room_after == room_before - 4_000_000


def test_build_ctx_roster_count_still_counts_a_contractless_rostered_player():
    """The T6a-equivalent correction (report §5.1's own explicit
    correction): a rostered, contract-less player must still count toward
    `roster_count` (and therefore `minimum_filler`), not be silently
    dropped the way the pre-T6a bug dropped them."""
    state = _make_state()
    contractless = make_roster_player(301, age=22, season=_SEASON, overall_cached=55, team_id=1)
    state.players[301] = contractless
    ctx_before = build_ctx(state, phase=Phase.PRESEASON)

    # Remove the contract-less player again and confirm room changes --
    # proving roster_count (and therefore minimum_filler) actually moved,
    # not that the field is simply ignored.
    del state.players[301]
    ctx_after = build_ctx(state, phase=Phase.PRESEASON)
    assert ctx_before.room_by_team[1] != ctx_after.room_by_team[1]


def test_build_ctx_head_to_head_rows_are_independent_copies_not_live_aliases():
    """NIT 4 (round 13 correction): `build_ctx`'s own docstring used to
    claim `head_to_head` is "never a live alias into the harness's own
    dict" -- true of the top-level dict, but `HeadToHead` is a non-frozen
    dataclass and the old `dict(state.head_to_head)` copy only cloned the
    mapping, leaving `ctx.head_to_head[k] is state.head_to_head[k]` `True`
    for every row. Fixed to clone each row too (`dataclasses.replace`),
    so mutating a row reached off `ctx.head_to_head` must never be visible
    on `state.head_to_head`'s own row."""
    from dba2.domain.head_to_head import HeadToHead

    state = _make_state()
    key = (1, 2)
    state.head_to_head[key] = HeadToHead(
        league_id=_LEAGUE_ID, team_id=1, opponent_id=2,
        opened=4.0, received=0.0, accepts_by_opponent=1.0, rejects_by_opponent=3.0,
        accepts_by_self=0.0, rejects_by_self=0.0, counters_given=0.0, counters_received=0.0,
        executions=0.0, refused_illegal=0.0, far_reject_streak=2.0,
        gap_by_opponent_sum=10.0, last_gap_by_opponent=5.0,
        last_season_interacted=_SEASON, updated_season=_SEASON,
    )
    ctx = build_ctx(state, phase=Phase.PRESEASON)

    assert ctx.head_to_head[key] is not state.head_to_head[key]
    ctx.head_to_head[key].far_reject_streak = 99.0
    assert state.head_to_head[key].far_reject_streak == 2.0


def test_build_ctx_head_to_head_enabled_false_suppresses_reader_not_writer():
    """Decision P3-48 (§11 step 17). The property this test exists to
    catch: a future fix to this same gap applied only at run-start (e.g.
    a one-time snapshot taken before the season loop begins) rather than
    per `build_ctx` call. `state.head_to_head` here is deliberately
    NON-EMPTY -- a naive "clear head_to_head once at the top of a
    suppressed run" implementation would still read empty/`None` on the
    FIRST call and pass a test that only checked an empty starting state;
    calling `build_ctx` directly against a state whose store already has
    real rows, and asserting suppression still wins, is what actually
    proves the flag is consumed at context-construction time, every call,
    per `state.py::build_ctx`'s own docstring."""
    from dba2.domain.head_to_head import HeadToHead

    state = _make_state()
    key = (1, 2)
    state.head_to_head[key] = HeadToHead(
        league_id=_LEAGUE_ID, team_id=1, opponent_id=2,
        opened=4.0, received=0.0, accepts_by_opponent=1.0, rejects_by_opponent=3.0,
        accepts_by_self=0.0, rejects_by_self=0.0, counters_given=0.0, counters_received=0.0,
        executions=0.0, refused_illegal=0.0, far_reject_streak=2.0,
        gap_by_opponent_sum=10.0, last_gap_by_opponent=5.0,
        last_season_interacted=_SEASON, updated_season=_SEASON,
    )

    ctx = build_ctx(state, phase=Phase.PRESEASON, head_to_head_enabled=False)

    assert ctx.head_to_head is None
    assert ctx.h2h_base_accept_rate is None
    # And the writer's own store is untouched -- suppression is reader-
    # side only, never a mutation of `state` itself.
    assert state.head_to_head[key].far_reject_streak == 2.0


# ---------------------------------------------------------------------------
# apply_trade_inmem
# ---------------------------------------------------------------------------


def test_apply_trade_inmem_moves_players_contracts_and_picks():
    state = _make_state()
    execution = TradeExecution(
        team_a_id=1,
        team_b_id=2,
        players_a_to_b=(101,),
        players_b_to_a=(201,),
        picks_a_to_b=(1,),
        picks_b_to_a=(),
        net_a=1.0,
        net_b=1.0,
        record_a=_dummy_record(1),
        record_b=_dummy_record(2),
    )
    apply_trade_inmem(state, execution)

    assert state.players[101].player.team_id == 2
    assert state.players[101].player.roster_status is RosterStatus.ACTIVE
    assert state.players[101].contract.contract.team_id == 2

    assert state.players[201].player.team_id == 1
    assert state.players[201].player.roster_status is RosterStatus.ACTIVE
    assert state.players[201].contract.contract.team_id == 1

    assert state.picks[1].current_team_id == 2


def test_apply_trade_inmem_tolerates_a_contractless_traded_player():
    state = _make_state()
    state.players[101] = dataclasses.replace(state.players[101], contract=None)
    execution = TradeExecution(
        team_a_id=1,
        team_b_id=2,
        players_a_to_b=(101,),
        players_b_to_a=(),
        picks_a_to_b=(),
        picks_b_to_a=(),
        net_a=0.0,
        net_b=0.0,
        record_a=_dummy_record(1),
        record_b=_dummy_record(2),
    )
    apply_trade_inmem(state, execution)
    assert state.players[101].player.team_id == 2
    assert state.players[101].contract is None


# ---------------------------------------------------------------------------
# apply_signing_inmem
# ---------------------------------------------------------------------------


def test_apply_signing_inmem_terminates_old_contract_then_creates_new():
    state = _make_state()
    old_contract = state.players[201].contract
    execution = SigningExecution(
        team_id=1,
        player_id=201,
        years=3,
        annual_salary=7_500_000,
        start_season=_SEASON + 1,
        signing_method=SigningMethod.MLE,
        record=_dummy_record(1),
    )
    apply_signing_inmem(state, execution)

    rp = state.players[201]
    assert rp.player.team_id == 1
    assert rp.player.roster_status is RosterStatus.ACTIVE
    assert rp.contract is not old_contract
    assert rp.contract.contract.contract_type is ContractType.STANDARD
    assert rp.contract.contract.signing_method is SigningMethod.MLE
    assert rp.contract.contract.status is ContractStatus.ACTIVE
    assert rp.contract.contract.start_season == _SEASON + 1
    assert rp.contract.contract.end_season == _SEASON + 3
    assert rp.contract.contract.total_years == 3
    assert [y.season for y in rp.contract.years] == [_SEASON + 1, _SEASON + 2, _SEASON + 3]
    assert all(y.salary == 7_500_000 for y in rp.contract.years)
    assert all(y.guaranteed_amount == 7_500_000 for y in rp.contract.years)


def test_apply_signing_inmem_assigns_a_fresh_unique_contract_id():
    state = _make_state()
    first_next_id = state.next_contract_id
    apply_signing_inmem(
        state,
        SigningExecution(
            team_id=1,
            player_id=201,
            years=1,
            annual_salary=CAP_SETTINGS.min_salary,
            start_season=_SEASON + 1,
            signing_method=SigningMethod.MIN_EXCEPTION,
            record=_dummy_record(1),
        ),
    )
    assert state.players[201].contract.contract.id == first_next_id
    assert state.next_contract_id == first_next_id + 1


# ---------------------------------------------------------------------------
# apply_draft_inmem -- T4 divergence: signs a real rookie-scale contract
# ---------------------------------------------------------------------------


def test_apply_draft_inmem_signs_first_round_rookie_scale_contract():
    state = _make_state()
    prospect = make_roster_player(501, age=19, season=_SEASON, overall_cached=75, team_id=None)
    prospect = dataclasses.replace(
        prospect,
        player=dataclasses.replace(prospect.player, roster_status=RosterStatus.DRAFT_ELIGIBLE),
    )
    state.prospects[501] = prospect
    pick = state.picks[1]
    pick.pick_number = 5

    result = draft_decisions.DraftPickResult(
        pick_number=5, team_id=2, prospect_id=501, value=10.0, record=_dummy_record(2)
    )
    apply_draft_inmem(state, pick, result)

    assert 501 not in state.prospects
    rp = state.players[501]
    assert rp.player.team_id == 2
    assert rp.player.roster_status is RosterStatus.ACTIVE
    assert rp.player.draft_pick_number == 5
    assert rp.contract is not None
    assert rp.contract.contract.contract_type is ContractType.ROOKIE_SCALE
    assert rp.contract.contract.signing_method is SigningMethod.DRAFT
    assert rp.contract.contract.start_season == _SEASON + 1
    assert rp.contract.contract.total_years == 4
    row = ROOKIE_SCALE[5]
    expected = (row.year_1_salary, row.year_2_salary, row.year_3_salary, row.year_4_salary)
    assert tuple(y.salary for y in rp.contract.years) == expected
    assert pick.used_for_player_id == 501


def test_apply_draft_inmem_second_round_gets_short_min_salary_deal():
    state = _make_state()
    prospect = make_roster_player(502, age=22, season=_SEASON, overall_cached=58, team_id=None)
    prospect = dataclasses.replace(
        prospect,
        player=dataclasses.replace(prospect.player, roster_status=RosterStatus.DRAFT_ELIGIBLE),
    )
    state.prospects[502] = prospect
    pick = state.picks[1]
    pick.pick_number = 45

    result = draft_decisions.DraftPickResult(
        pick_number=45, team_id=1, prospect_id=502, value=1.0, record=_dummy_record(1)
    )
    apply_draft_inmem(state, pick, result)

    rp = state.players[502]
    assert rp.contract.contract.contract_type is ContractType.STANDARD
    assert rp.contract.contract.total_years == 2
    next_season_cap = state.cap_settings_by_season[_SEASON + 1]
    assert all(y.salary == next_season_cap.min_salary for y in rp.contract.years)


def test_apply_draft_inmem_raises_on_unresolved_pick_number():
    state = _make_state()
    prospect = make_roster_player(503, age=20, season=_SEASON, overall_cached=60, team_id=None)
    state.prospects[503] = prospect
    pick = state.picks[1]
    assert pick.pick_number is None
    result = draft_decisions.DraftPickResult(
        pick_number=1, team_id=1, prospect_id=503, value=1.0, record=_dummy_record(1)
    )
    with pytest.raises(ValueError):
        apply_draft_inmem(state, pick, result)


# ---------------------------------------------------------------------------
# apply_windows_inmem
# ---------------------------------------------------------------------------


def test_apply_windows_inmem_replaces_window_by_team_wholesale():
    state = _make_state()
    window_pass = WindowPassResult(
        window_by_team={1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD},
        records=(),
    )
    apply_windows_inmem(state, window_pass)
    assert state.window_by_team == {1: CompetitiveWindow.CONTEND, 2: CompetitiveWindow.REBUILD}


# ---------------------------------------------------------------------------
# apply_waiver_inmem (T7a2) -- mirrors roster_service.waive's own
# documented semantics exactly (dba2/services/roster_service.py:121-);
# mutation-tested against the real function's own boundary conditions,
# not just against this file's own reimplementation of them.
# ---------------------------------------------------------------------------


def _dummy_waiver_record(team_id: int, player_id: int) -> DecisionRecord:
    return DecisionRecord(
        team_id=team_id,
        philosophy=GmPhilosophy.EGALITARIAN,
        window=CompetitiveWindow.RETOOL,
        subject=f"player:{player_id}",
        verdict="waive",
        score=0.0,
        threshold=0.0,
        terms=(),
    )


def test_apply_waiver_inmem_terminates_contract_and_frees_the_player():
    state = _make_state()
    # A fresh 3-year fully-guaranteed deal starting THIS season, so every
    # remaining year's own dead-cap charge is exercised, not just one.
    contract = make_contract(
        101, team_id=1, start_season=_SEASON, salaries=(9_000_000, 9_500_000, 10_000_000)
    )
    state.players[101] = make_roster_player(
        101, age=27, season=_SEASON, overall_cached=82, team_id=1, contract=contract
    )
    execution = WaiverExecution(
        team_id=1,
        player_id=101,
        contract_id=contract.contract.id,
        waived_in_season=_SEASON,
        record=_dummy_waiver_record(1, 101),
    )
    apply_waiver_inmem(state, execution)

    rp = state.players[101]
    assert rp.player.team_id is None
    assert rp.player.roster_status is RosterStatus.FREE_AGENT
    assert rp.contract is None
    # The contract object itself (still referenced by `contract`, `Contract`
    # being a plain mutable dataclass) was terminated in place.
    assert contract.contract.status is ContractStatus.WAIVED
    assert contract.contract.terminated_in_season == _SEASON

    assert state.dead_cap_by_team_season[(1, _SEASON)] == 9_000_000
    assert state.dead_cap_by_team_season[(1, _SEASON + 1)] == 9_500_000
    assert state.dead_cap_by_team_season[(1, _SEASON + 2)] == 10_000_000


def test_apply_waiver_inmem_excludes_already_elapsed_seasons():
    """`waived_in_season` mid-deal -- only `season >= waived_in_season`
    years become dead cap, matching `roster_service.waive`'s own `>=`
    boundary exactly (never a strict `>`, which would incorrectly drop
    the season being waived in)."""
    state = _make_state()
    contract = make_contract(
        101, team_id=1, start_season=_SEASON - 1, salaries=(8_000_000, 9_000_000, 9_500_000)
    )
    state.players[101] = make_roster_player(
        101, age=27, season=_SEASON, overall_cached=82, team_id=1, contract=contract
    )
    execution = WaiverExecution(
        team_id=1,
        player_id=101,
        contract_id=contract.contract.id,
        waived_in_season=_SEASON,
        record=_dummy_waiver_record(1, 101),
    )
    apply_waiver_inmem(state, execution)

    assert (1, _SEASON - 1) not in state.dead_cap_by_team_season
    assert state.dead_cap_by_team_season[(1, _SEASON)] == 9_000_000
    assert state.dead_cap_by_team_season[(1, _SEASON + 1)] == 9_500_000


def test_apply_waiver_inmem_skips_unguaranteed_years():
    from dba2.domain.contract import OptionType

    state = _make_state()
    contract = make_contract(
        101,
        team_id=1,
        start_season=_SEASON,
        salaries=(9_000_000, 9_500_000),
        option_types=(OptionType.NONE, OptionType.PLAYER),
    )
    state.players[101] = make_roster_player(
        101, age=27, season=_SEASON, overall_cached=82, team_id=1, contract=contract
    )
    execution = WaiverExecution(
        team_id=1,
        player_id=101,
        contract_id=contract.contract.id,
        waived_in_season=_SEASON,
        record=_dummy_waiver_record(1, 101),
    )
    apply_waiver_inmem(state, execution)
    assert state.dead_cap_by_team_season == {(1, _SEASON): 9_000_000}
    assert (1, _SEASON + 1) not in state.dead_cap_by_team_season


def test_apply_waiver_inmem_sums_multiple_cuts_in_the_same_team_season():
    """Two different players cut in the same pass -- their dead-cap
    contributions to the same `(team, season)` key must ADD, never
    overwrite."""
    state = _make_state()
    contract_a = make_contract(101, team_id=1, start_season=_SEASON, salaries=(5_000_000,))
    contract_c = make_contract(301, team_id=1, start_season=_SEASON, salaries=(3_000_000,))
    state.players[101] = make_roster_player(
        101, age=27, season=_SEASON, overall_cached=82, team_id=1, contract=contract_a
    )
    state.players[301] = make_roster_player(
        301, age=25, season=_SEASON, overall_cached=60, team_id=1, contract=contract_c
    )
    apply_waiver_inmem(
        state,
        WaiverExecution(
            team_id=1,
            player_id=101,
            contract_id=contract_a.contract.id,
            waived_in_season=_SEASON,
            record=_dummy_waiver_record(1, 101),
        ),
    )
    apply_waiver_inmem(
        state,
        WaiverExecution(
            team_id=1,
            player_id=301,
            contract_id=contract_c.contract.id,
            waived_in_season=_SEASON,
            record=_dummy_waiver_record(1, 301),
        ),
    )
    assert state.dead_cap_by_team_season[(1, _SEASON)] == 8_000_000


def test_apply_waiver_inmem_raises_on_a_contract_id_mismatch():
    state = _make_state()
    execution = WaiverExecution(
        team_id=1,
        player_id=101,
        contract_id=999_999,  # not player 101's real contract id
        waived_in_season=_SEASON,
        record=_dummy_waiver_record(1, 101),
    )
    with pytest.raises(AssertionError):
        apply_waiver_inmem(state, execution)


def test_apply_waiver_inmem_raises_on_an_already_waived_contract():
    """Nit 3 fix: mirrors `roster_service.waive`'s own real-DB guard
    (`dba2/services/roster_service.py:154-158`) -- a contract that is
    already non-`ACTIVE` must never be waived a second time. Without this
    guard, a caller replaying the same `WaiverExecution` (or a real
    double-selection bug upstream) would silently double-charge `state.
    dead_cap_by_team_season` instead of failing loudly. Pre-sets the
    contract's own `status` to `WAIVED` directly, reproducing the state a
    real double-call would find it in, without needing to actually run
    `apply_waiver_inmem` twice first."""
    state = _make_state()
    contract = make_contract(101, team_id=1, start_season=_SEASON, salaries=(9_000_000,))
    contract.contract.status = ContractStatus.WAIVED
    state.players[101] = make_roster_player(
        101, age=27, season=_SEASON, overall_cached=82, team_id=1, contract=contract
    )
    execution = WaiverExecution(
        team_id=1,
        player_id=101,
        contract_id=contract.contract.id,
        waived_in_season=_SEASON,
        record=_dummy_waiver_record(1, 101),
    )
    with pytest.raises(AssertionError, match="not ACTIVE"):
        apply_waiver_inmem(state, execution)


def test_apply_waiver_inmem_raises_on_a_contract_with_no_team_id():
    """Nit 3 fix: mirrors `roster_service.waive`'s own real-DB guard
    (`dba2/services/roster_service.py:159-162`) -- a contract with no
    `team_id` has no team to charge dead cap to at all."""
    state = _make_state()
    contract = make_contract(101, team_id=1, start_season=_SEASON, salaries=(9_000_000,))
    contract.contract.team_id = None
    state.players[101] = make_roster_player(
        101, age=27, season=_SEASON, overall_cached=82, team_id=1, contract=contract
    )
    execution = WaiverExecution(
        team_id=1,
        player_id=101,
        contract_id=contract.contract.id,
        waived_in_season=_SEASON,
        record=_dummy_waiver_record(1, 101),
    )
    with pytest.raises(AssertionError, match="no team_id"):
        apply_waiver_inmem(state, execution)


# Critic round 2 on T7a2 (`docs/ledger/phase-2.md`) found the two "mutation
# tests" formerly here never called the real `apply_waiver_inmem` at all --
# each defined a local, hand-copied broken function and asserted against ITS
# behavior, which cannot fail if the real function regresses (the exact
# "test never calls the real fixed logic" shape this project has already
# named in rounds 27/31/33). Removed rather than kept as decoration.
#
# The genuine discriminating coverage already lives in
# `test_apply_waiver_inmem_terminates_contract_and_frees_the_player` above,
# which calls the real function and asserts exact per-season dollar amounts
# across all 3 contract-years (9.0M/9.5M/10.0M) -- confirmed during this fix
# round by mutating the real function in place and observing that test fail
# under both bugs the removed tests only pretended to catch: (1) `year.
# season > waived_in_season` instead of `>=` drops the waived-in-season
# year's own charge; (2) charging only the first remaining year instead of
# every one drops the later years. Not re-encoded as a permanent test here
# -- a second, parallel copy of the real function's own logic is exactly
# what made the removed tests worthless in the first place.


# ---------------------------------------------------------------------------
# `_aggregate_capsheet_inputs`'s new `cap_holds` term (architect round 19
# §4.3-§4.5, Slice 19-A backend-dev round 54).
# ---------------------------------------------------------------------------


def _make_hold_state(
    *,
    n_committed_players: int = 0,
    n_expiring_players: int = 0,
    expiring_salary: int = 10_000_000,
    expiring_years_pro: int = 5,
) -> HarnessState:
    """One team (id=1). `n_committed_players` players each carry a 2-year
    contract covering `_SEASON`/`_SEASON + 1` (so `roster_count(_SEASON +
    1) == n_committed_players` exactly, no contract-less rostered players
    in the mix). `n_expiring_players` players each carry a 1-year contract
    covering `_SEASON` only, at `expiring_salary`/`expiring_years_pro` --
    `_expiring_by_team`'s own predicate (`max(contract_years.season) ==
    state.season`), the hold population."""
    teams = {1: _team(1, GmPhilosophy.STAR_MAXER)}
    players: dict[int, object] = {}
    pid = 1
    for _ in range(n_committed_players):
        contract = make_contract(
            pid, team_id=1, start_season=_SEASON, salaries=(5_000_000, 5_000_000)
        )
        players[pid] = make_roster_player(
            pid, age=27, season=_SEASON, overall_cached=70, team_id=1, contract=contract
        )
        pid += 1
    for _ in range(n_expiring_players):
        contract = make_contract(pid, team_id=1, start_season=_SEASON, salaries=(expiring_salary,))
        rp = make_roster_player(
            pid, age=27, season=_SEASON, overall_cached=70, team_id=1, contract=contract
        )
        rp.player.years_pro = expiring_years_pro
        players[pid] = rp
        pid += 1
    return HarnessState(
        teams=teams,
        players=players,
        prospects={},
        picks={},
        cap_settings_by_season=_cap_settings_by_season(),
        rookie_scale=ROOKIE_SCALE,
        window_by_team={1: CompetitiveWindow.RETOOL},
        league_id=_LEAGUE_ID,
        season=_SEASON,
        rng_seed=1,
        next_contract_id=1000,
        next_pick_id=1,
        next_player_id=1000,
    )


def _hold_row(state: HarnessState, *, season: int):
    rows = _aggregate_capsheet_inputs(state, DEFAULT_TUNING)[1]
    return next(r for r in rows if r.season == season)


def test_cap_holds_is_zero_at_every_horizon_row_except_season_plus_1():
    """Mutation: drop the `season == hold_season` guard (always call `_cap_
    holds_for_team` regardless of `season`) -- `hold_season_settings` is
    fixed to the `_SEASON + 1` row regardless of which horizon row is being
    built, so the mutated code would compute the identical nonzero hold at
    EVERY row, failing every one of the `== 0` assertions below, not just
    one. Verified by hand during this round (temporarily applied, observed
    the failure, reverted -- see this round's own build report)."""
    state = _make_hold_state(n_expiring_players=1, expiring_salary=10_000_000)
    rows = _aggregate_capsheet_inputs(state, DEFAULT_TUNING)[1]
    assert [r.season for r in rows] == [_SEASON + i for i in range(5)]
    for r in rows:
        if r.season == _SEASON + 1:
            assert r.cap_holds > 0, "the one row this formula is domain-correct at must be nonzero"
        else:
            assert r.cap_holds == 0, f"season {r.season} must carry no hold"


def test_cap_holds_is_zero_for_a_team_with_no_expiring_players():
    state = _make_hold_state(n_committed_players=3, n_expiring_players=0)
    assert _hold_row(state, season=_SEASON + 1).cap_holds == 0


def test_max_salary_clamp_binds_on_a_supermax_sized_expiring_contract():
    """`roster_count = roster_min = 14` zeroes `filler_credit` (`empty_
    slots = max(0, 14 - 14) = 0`), so `cap_holds` here is the raw clamped
    per-player hold, unobscured by the netting term -- isolates the `max_
    salary` clamp on its own. `expiring_salary` is set absurdly high
    (999,999,999) so the clamp binds regardless of `_CAP_HOLD_MULT`'s own
    exact measured value (round 19 §4.3: `_CAP_HOLD_MULT * salary` only
    needs to exceed `max_salary` -- true here by roughly an order of
    magnitude even at `_CAP_HOLD_MULT = 1.0`). Mutation: remove the `max`
    clamp -- `cap_holds` would then be `round(_CAP_HOLD_MULT * 999,999,999)`,
    tens of millions above the asserted value, failing.

    Expected value is against `_grown_hold_season_settings()` (round 52
    N2), not raw `CAP_SETTINGS` -- `hold_season_settings` is genuinely
    grown by one season now, never the flat dict-row lookup this fixture's
    own `_SEASON + 1` entry used to (silently) supply."""
    state = _make_hold_state(
        n_committed_players=14, n_expiring_players=1,
        expiring_salary=999_999_999, expiring_years_pro=10,  # max_pct_10_plus tier
    )
    row = _hold_row(state, season=_SEASON + 1)
    grown = _grown_hold_season_settings()
    expected_max_salary = round(grown.max_pct_10_plus / 100.0 * grown.salary_cap)
    assert row.cap_holds == expected_max_salary


def test_min_salary_floor_binds_on_a_sub_minimum_expiring_salary():
    """Same `roster_count = roster_min = 14` isolation as the max-clamp
    test above. `expiring_salary=1` makes `_CAP_HOLD_MULT * 1` round to
    (approximately) 0 regardless of the exact multiplier, well under `min_
    salary` -- the floor must bring it back up. Mutation: remove the `min`
    floor -- `cap_holds` would round to 0 or 1, not `min_salary`, failing.

    Expected value is `_grown_hold_season_settings().min_salary` (round 52
    N2), not raw `CAP_SETTINGS.min_salary` -- see that helper's own
    docstring."""
    state = _make_hold_state(
        n_committed_players=14, n_expiring_players=1, expiring_salary=1, expiring_years_pro=5,
    )
    row = _hold_row(state, season=_SEASON + 1)
    assert row.cap_holds == _grown_hold_season_settings().min_salary


def test_filler_credit_fully_nets_holds_that_fit_inside_the_existing_empty_slots():
    """architect round 19 §4.4/§7's own worked shape: `roster_count=7`,
    `roster_min=14` (7 empty slots) -- with exactly 7 minimum-salary
    expiring players (`_CAP_HOLD_MULT < 1` floors every one of their raw
    holds to `min_salary`, matching round 19's own "minimum-salary expiring
    player" framing), `filler_credit` fully absorbs the 7 holds and `used`
    (via the real `capsheet.project_team_cap_sheet`, `include_holds=True`)
    must come out byte-identical to the hold-free `used` -- the "no
    double-charge" property round 19 §4.4/§7 names as this slice's single
    most important test. Mutation: remove `filler_credit` entirely --
    `used` would be `+= 7 * min_salary` instead, failing the equality."""
    state = _make_hold_state(
        n_committed_players=7,
        n_expiring_players=7,
        expiring_salary=CAP_SETTINGS.min_salary,
        expiring_years_pro=5,
    )
    inputs = _aggregate_capsheet_inputs(state, DEFAULT_TUNING)[1]
    real_anchor = {_SEASON: state.cap_settings_by_season[_SEASON]}
    row_free = next(
        p
        for p in capsheet.project_team_cap_sheet(
            inputs, real_anchor, tuning=DEFAULT_TUNING, include_holds=False
        )
        if p.season == _SEASON + 1
    )
    row_holds = next(
        p
        for p in capsheet.project_team_cap_sheet(
            inputs, real_anchor, tuning=DEFAULT_TUNING, include_holds=True
        )
        if p.season == _SEASON + 1
    )
    assert row_holds.cap_holds == 0
    assert row_holds.used == row_free.used


def test_filler_credit_only_partially_nets_holds_that_exceed_the_empty_slots():
    """One more expiring player than `test_filler_credit_fully_nets_...`
    above (8 vs. 7 empty slots) -- round 19 §7's own literal fixture
    (`roster_count=7`, `roster_min=14`, 8 minimum-salary expiring players).
    **Disclosed departure from that memo's own literal assertion** ("`used`
    equal to today's used, not `used + 8 * min_salary`"): applying round
    19 §4.3's own formula exactly (`filler_credit = min(n_holds, empty_
    slots) * min_salary`) to this fixture gives `filler_credit = min(8, 7)
    * min_salary = 7 * min_salary`, netting only 7 of the 8 holds -- the
    8th is a genuinely new dollar amount, not a double-charge (the memo's
    own `roster_min`-based formula and its own 7-vs-8 worked numbers are
    mutually inconsistent by exactly one `min_salary`; `test_filler_
    credit_fully_nets_...` above already pins the memo's own core claim
    exactly at the n_holds == empty_slots boundary where no such
    inconsistency exists). What this test asserts instead -- the real
    formula-consistent claim, and still a direct, sharp refutation of the
    naive "no netting at all" bug the memo's own mutation targets: `used`
    rises by exactly ONE `min_salary`, not eight. Mutation: remove `filler_
    credit` entirely -- `used` would rise by `8 * min_salary`, failing.

    Expected `cap_holds`/`used` delta is `_grown_hold_season_settings().
    min_salary` (round 52 N2), not raw `CAP_SETTINGS.min_salary` -- see
    that helper's own docstring."""
    state = _make_hold_state(
        n_committed_players=7,
        n_expiring_players=8,
        expiring_salary=CAP_SETTINGS.min_salary,
        expiring_years_pro=5,
    )
    inputs = _aggregate_capsheet_inputs(state, DEFAULT_TUNING)[1]
    real_anchor = {_SEASON: state.cap_settings_by_season[_SEASON]}
    row_free = next(
        p
        for p in capsheet.project_team_cap_sheet(
            inputs, real_anchor, tuning=DEFAULT_TUNING, include_holds=False
        )
        if p.season == _SEASON + 1
    )
    row_holds = next(
        p
        for p in capsheet.project_team_cap_sheet(
            inputs, real_anchor, tuning=DEFAULT_TUNING, include_holds=True
        )
        if p.season == _SEASON + 1
    )
    grown_min_salary = _grown_hold_season_settings().min_salary
    assert row_holds.cap_holds == grown_min_salary
    assert row_holds.used == row_free.used + grown_min_salary


# ---------------------------------------------------------------------------
# Critic round 52 fixes -- N2 (`filler_credit`'s `min_salary(S+1)` basis)
# and N3 (tier-selection / final-vs-first-contracted-year test gaps).
# ---------------------------------------------------------------------------


def test_filler_credit_min_salary_ignores_a_stale_cap_settings_by_season_dict_row():
    """N2 (critic round 52): `filler_credit`'s (and every other clamp's)
    `min_salary(S+1)` must come from `capsheet.project_cap_settings(state.
    season's own row, S+1, tuning)` -- the exact basis `project_team_cap_
    sheet` itself uses downstream for `used`/`room`'s own `minimum_filler`
    -- never a separate `state.cap_settings_by_season.get(S+1)` dict
    lookup. Simulated here by deliberately overwriting `state.cap_settings_
    by_season[_SEASON + 1]` with an arbitrary, clearly-wrong row (`min_
    salary=999_999`, nowhere close to the real grown figure) -- exactly
    what a real multi-season run's own `_ensure_cap_settings` would have
    already populated that dict slot with by this point (single-rounded
    from the run's true `first_season` anchor, genuinely different from
    what `project_team_cap_sheet` computes for the same row from `state.
    season`'s own, later anchor -- critic round 52's own "$1 double-
    rounding" finding, exaggerated here to an unmissable magnitude rather
    than a $1 diff, so this test does not depend on hitting a specific
    rounding-boundary season). If the dict lookup were still consulted,
    `cap_holds` would pick up the stale `999,999` floor instead of the
    real, grown `min_salary`. Mutation: reintroduce `hold_season_settings
    = state.cap_settings_by_season.get(hold_season) or project_cap_
    settings(...)` -- this test would then read the stale row and fail."""
    state = _make_hold_state(
        n_committed_players=14, n_expiring_players=1, expiring_salary=1, expiring_years_pro=5,
    )
    state.cap_settings_by_season[_SEASON + 1] = dataclasses.replace(
        CAP_SETTINGS, season=_SEASON + 1, min_salary=999_999
    )
    row = _hold_row(state, season=_SEASON + 1)
    grown_min_salary = _grown_hold_season_settings().min_salary
    assert row.cap_holds == grown_min_salary, (
        f"cap_holds must floor at the project_cap_settings-derived min_salary "
        f"({grown_min_salary}), not the stale state.cap_settings_by_season[{_SEASON + 1}] "
        f"dict row's min_salary (999,999) -- got {row.cap_holds}"
    )


def test_max_salary_clamp_uses_years_pro_tier_not_a_hardcoded_max_pct_10_plus():
    """N3 Mutation B (critic round 52): every existing hold fixture uses
    `expiring_years_pro=10`+ (`test_max_salary_clamp_binds_...`'s own
    fixture, both `filler_credit` tests' fixtures), which happens to
    select the same `max_pct_10_plus` tier a hardcoded fallback would too
    -- no existing test ever exercised a *different* years-of-service
    tier. `expiring_years_pro=3` here selects `max_pct_0_6` (25%),
    genuinely different from `max_pct_10_plus` (35%) in `tests/fixtures/
    managers.py::CAP_SETTINGS` -- the two clamped `max_salary` figures
    differ by 10 percentage points of the cap, unmistakably distinguishing
    "uses the real years-of-service tier" from "always uses the 10+
    tier." Re-verified by hand this round: `dba2/managers/decisions/
    signing.py::bird_max_pct_tier` (the real function `_cap_holds_for_
    team` calls) correctly branches `years_pro <= 6 -> max_pct_0_6`,
    `<= 9 -> max_pct_7_9`, else `max_pct_10_plus` -- genuinely correct for
    a low-years-of-service player, not just untested until now. Mutation:
    hardcode `_cap_holds_for_team`'s own `max_pct` to always read `hold_
    season_settings.max_pct_10_plus` -- this test's own `expected_max_
    salary` (25%-of-cap-based) would then mismatch the mutated code's
    35%-of-cap-based clamp, failing."""
    state = _make_hold_state(
        n_committed_players=14, n_expiring_players=1,
        expiring_salary=999_999_999, expiring_years_pro=3,  # max_pct_0_6 tier
    )
    row = _hold_row(state, season=_SEASON + 1)
    grown = _grown_hold_season_settings()
    expected_max_salary = round(grown.max_pct_0_6 / 100.0 * grown.salary_cap)
    assert row.cap_holds == expected_max_salary, (
        f"a years_pro=3 expiring player's hold must clamp at the max_pct_0_6 tier "
        f"({expected_max_salary}), not a hardcoded max_pct_10_plus tier -- got {row.cap_holds}"
    )


def test_hold_basis_uses_final_contracted_year_salary_not_first_year():
    """N3 Mutation C (critic round 52): every existing hold fixture uses a
    1-year (or flat-salary) expiring contract, where first-year and final-
    year salary are identical -- never exercising architect round 19
    §4.3's own spec, "`salary_p(S)` is `p`'s own `contract_years` salary
    for season `S` -- his last contracted year." Constructs a genuinely
    non-flat, 2-year expiring contract (`_SEASON - 1` -> `_SEASON`,
    salaries `(1, 999_999_999)` -- first-year salary near-zero, final-year
    salary supermax-sized) so the two bases produce wildly different
    clamped holds: reading the final year correctly clamps at `max_salary`
    (`expiring_years_pro=10` keeps it on the same `max_pct_10_plus` tier
    `test_max_salary_clamp_binds_...` already isolates); reading the first
    year instead would floor at `min_salary` (`_CAP_HOLD_MULT * 1` rounds
    to ~0). Re-verified by hand this round: `_aggregate_capsheet_inputs`'s
    own `final_salary = next(year.salary for year in rp.contract.years if
    year.season == state.season)` already reads the contract's *last*
    year correctly (`state.season` is that expiring contract's own final
    covered season, by the `_expiring_by_team` predicate's own
    definition) -- genuinely correct, not just untested until now.
    Mutation: swap that lookup for `rp.contract.years[0].salary` (the
    contract's *first* year) -- `cap_holds` would then drop to the grown
    `min_salary` floor, failing this test's own `== expected_max_salary`
    assertion by tens of millions."""
    teams = {1: _team(1, GmPhilosophy.STAR_MAXER)}
    players: dict[int, object] = {}
    pid = 1
    for _ in range(14):
        contract = make_contract(
            pid, team_id=1, start_season=_SEASON, salaries=(5_000_000, 5_000_000)
        )
        players[pid] = make_roster_player(
            pid, age=27, season=_SEASON, overall_cached=70, team_id=1, contract=contract
        )
        pid += 1
    expiring_contract = make_contract(
        pid, team_id=1, start_season=_SEASON - 1, salaries=(1, 999_999_999),
    )
    expiring_rp = make_roster_player(
        pid, age=30, season=_SEASON, overall_cached=80, team_id=1, contract=expiring_contract
    )
    expiring_rp.player.years_pro = 10  # max_pct_10_plus tier
    players[pid] = expiring_rp
    state = HarnessState(
        teams=teams,
        players=players,
        prospects={},
        picks={},
        cap_settings_by_season=_cap_settings_by_season(),
        rookie_scale=ROOKIE_SCALE,
        window_by_team={1: CompetitiveWindow.RETOOL},
        league_id=_LEAGUE_ID,
        season=_SEASON,
        rng_seed=1,
        next_contract_id=1000,
        next_pick_id=1,
        next_player_id=1000,
    )
    row = _hold_row(state, season=_SEASON + 1)
    grown = _grown_hold_season_settings()
    expected_max_salary = round(grown.max_pct_10_plus / 100.0 * grown.salary_cap)
    assert row.cap_holds == expected_max_salary, (
        f"hold must be computed from the expiring contract's FINAL contracted-year salary "
        f"(999,999,999, clamped to max_salary={expected_max_salary}), not its first year's "
        f"salary (1, which would floor at min_salary instead) -- got {row.cap_holds}"
    )
