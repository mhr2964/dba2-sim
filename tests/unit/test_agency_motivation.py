"""`dba2/agency/motivation.py` -- trait generation (P4-6), the satisfaction
evaluator and its saturating fold (P4-10/P4-44/P4-45/P4-65 -- §10 test 1),
morale/trust, and the `RequestState` ladder's hysteresis+dwell mechanism
(P4-14/P4-56/P4-61/P4-70/P4-71 -- §10 test 3).
"""
from __future__ import annotations

import pytest

from dba2.agency.motivation import (
    TRADE_REQUEST_BINDS_STAR_LEVERAGE_THRESHOLD,
    GeneratedTraits,
    LadderBar,
    RequestedDelta,
    WrittenDelta,
    advance_request_state,
    compute_morale,
    decay_trust,
    dwell_is_open,
    fold_satisfaction_deltas,
    fold_trust_deltas,
    generate_traits,
    largest_magnitude_axis,
    pressure_from_morale,
    resolve_request_opened_in_season,
    step_ladder_rung,
    trade_request_pressure,
)
from dba2.domain.motivation import MotivationAxis, RequestState

# ---------------------------------------------------------------------------
# Decision P4-6 -- trait generation
# ---------------------------------------------------------------------------


def test_generate_traits_is_deterministic_from_seed():
    a = generate_traits(12345, player_id=7, age=25.0, overall=80)
    b = generate_traits(12345, player_id=7, age=25.0, overall=80)
    assert a == b


def test_generate_traits_uses_an_independent_salt_from_player_id():
    a = generate_traits(12345, player_id=7, age=25.0, overall=80)
    b = generate_traits(12345, player_id=8, age=25.0, overall=80)
    assert a != b


def test_generate_traits_weights_are_bounded_tendency_range():
    traits = generate_traits(999, player_id=1, age=19.0, overall=60)
    assert isinstance(traits, GeneratedTraits)
    for weight in (
        traits.weight_winning,
        traits.weight_role_usage,
        traits.weight_loyalty,
        traits.weight_money,
        traits.weight_legacy,
        traits.star_leverage,
    ):
        assert 0 <= weight <= 100


def test_generate_traits_star_leverage_is_derived_from_talent_not_drawn():
    """A 12th man's leverage is not a personality trait (Decision P4-6):
    two players with identical `overall` get identical `star_leverage`
    regardless of the (different) RNG draw their other traits use."""
    bench = generate_traits(1, player_id=101, age=27.0, overall=60)
    another_bench = generate_traits(1, player_id=202, age=30.0, overall=60)
    assert bench.star_leverage == another_bench.star_leverage == 0

    star = generate_traits(1, player_id=303, age=27.0, overall=100)
    assert star.star_leverage == 100
    assert star.star_leverage > bench.star_leverage


def test_generate_traits_never_fabricates_a_hometown():
    """Decision P4-12: no real player or generated player gets a
    fabricated hometown."""
    traits = generate_traits(1, player_id=1, age=25.0, overall=75)
    assert traits.hometown_team_id is None


def test_generate_traits_veteran_bias_shifts_loyalty_and_legacy_means():
    """Not a hard assertion on any one draw (RNG noise), but the mean
    shift should be visible in aggregate across many player ids."""
    n = 200
    rookie_loyalty = [
        generate_traits(42, player_id=i, age=19.0, overall=70).weight_loyalty for i in range(n)
    ]
    veteran_loyalty = [
        generate_traits(42, player_id=i, age=36.0, overall=70).weight_loyalty
        for i in range(1000, 1000 + n)
    ]
    assert sum(veteran_loyalty) / n > sum(rookie_loyalty) / n


# ---------------------------------------------------------------------------
# Decision P4-10/P4-44/P4-65 -- the reconstruction invariant (§10 test 1)
# ---------------------------------------------------------------------------


def _current(**overrides: int) -> dict[MotivationAxis, int]:
    base = {axis: 0 for axis in MotivationAxis}
    for axis_name, value in overrides.items():
        base[MotivationAxis[axis_name.upper()]] = value
    return base


def test_reconstruction_invariant_boundary_case_a_saturate_up():
    """§10 test 1(a): a player at `satisfaction_winning = 95` receiving a
    requested `+10`. The written event delta must be `+5`, the state
    must land at `100`, and the note must record the requested `+10`."""
    current = _current(winning=95)
    requested = (RequestedDelta(axis=MotivationAxis.WINNING, requested=10.0, event_type="test"),)

    new_state, written = fold_satisfaction_deltas(current, requested)

    assert new_state[MotivationAxis.WINNING] == 100
    assert len(written) == 1
    w = written[0]
    assert w.applied == 5
    assert "+10" in w.note
    assert "+5" in w.note
    # The invariant itself: after == before + sum(applied).
    assert new_state[MotivationAxis.WINNING] == 95 + sum(
        d.applied for d in written if d.axis is MotivationAxis.WINNING
    )
    # And independently: every written delta respects the range clause.
    assert -100 <= 95 + w.applied <= 100


def test_reconstruction_invariant_boundary_case_b_saturate_down_writes_zero_row():
    """§10 test 1(b): a player at `-100` receiving a requested `-10`
    writes a `0`-delta event row -- not a skipped row. An absent row and
    a zero row reconstruct identically and explain differently."""
    current = _current(winning=-100)
    requested = (RequestedDelta(axis=MotivationAxis.WINNING, requested=-10.0, event_type="test"),)

    new_state, written = fold_satisfaction_deltas(current, requested)

    assert new_state[MotivationAxis.WINNING] == -100
    assert len(written) == 1  # the row IS written, not skipped
    assert written[0].applied == 0
    assert written[0].note is not None  # explains why it saturated to zero


def test_reconstruction_invariant_boundary_case_c_same_checkpoint_saturate_then_reverse():
    """§10 test 1(c): at `95`, a requested `+10` then `-20`, folded in
    EMISSION ORDER, must land at `80` with written deltas `+5, -20` --
    two rows, not one back-derived `-15` row (P4-65: this row-count/
    composition check is what catches a back-deriving writer that pure
    summation alone cannot)."""
    current = _current(winning=95)
    requested = (
        RequestedDelta(axis=MotivationAxis.WINNING, requested=10.0, event_type="test"),
        RequestedDelta(axis=MotivationAxis.WINNING, requested=-20.0, event_type="test"),
    )

    new_state, written = fold_satisfaction_deltas(current, requested)

    assert new_state[MotivationAxis.WINNING] == 80
    assert [w.applied for w in written] == [5, -20]
    assert len(written) == 2  # NOT one row of -15 -- shape matters, not just arithmetic


def test_reconstruction_invariant_mutation_back_deriving_writer_fails_the_shape_check():
    """A writer that computes the new vector and back-derives ONE delta
    would satisfy the sum AND the range clause simultaneously (P4-65 part
    1) -- it is caught by row count/composition, which this test asserts
    directly rather than leaving implicit."""
    current = _current(winning=95)
    back_derived = (RequestedDelta(axis=MotivationAxis.WINNING, requested=-15.0, event_type="bad"),)
    _, written = fold_satisfaction_deltas(current, back_derived)
    # A back-deriving writer's single row does NOT match the real fold's
    # two-row, [+5, -20] shape from the case-c test above.
    assert [w.applied for w in written] != [5, -20]


def test_reconstruction_invariant_holds_across_every_axis_and_never_exceeds_range():
    """A broader sweep: for any sequence of requested deltas on any
    axis, the invariant holds exactly and every intermediate running
    total stays in range."""
    current = _current()
    requested = tuple(
        RequestedDelta(axis=axis, requested=float(v), event_type="sweep")
        for axis in MotivationAxis
        for v in (37.0, -12.0, 91.0, -250.0, 40.0)
    )
    new_state, written = fold_satisfaction_deltas(current, requested)
    for axis in MotivationAxis:
        total_applied = sum(w.applied for w in written if w.axis is axis)
        assert new_state[axis] == current[axis] + total_applied
        assert -100 <= new_state[axis] <= 100
    # The range clause is on the RESULT of each written delta, not on the
    # delta's own magnitude (a single delta can legitimately span more
    # than 100 points, e.g. +100 -> -100) -- replay the fold by hand,
    # per-axis, to check the real property P4-44/P4-65 specify.
    running = dict(current)
    for w in written:
        prior = running[w.axis]
        assert -100 <= prior + w.applied <= 100
        running[w.axis] = prior + w.applied


def test_rounding_happens_per_delta_before_saturation_not_on_a_summed_float():
    """Decision P4-65 part 2: three `+0.4` deltas on one axis in one
    checkpoint. Correct (round-per-delta): `round(0.4) = 0` three times,
    total applied `0`. The wrong shape (sum-then-round-once) would give
    `round(1.2) = 1`."""
    current = _current(winning=0)
    requested = tuple(
        RequestedDelta(axis=MotivationAxis.WINNING, requested=0.4, event_type="rounding")
        for _ in range(3)
    )
    new_state, written = fold_satisfaction_deltas(current, requested)
    assert sum(w.applied for w in written) == 0
    assert new_state[MotivationAxis.WINNING] == 0


def test_written_delta_note_is_none_when_no_saturation_and_no_evaluator_note():
    current = _current(winning=0)
    requested = (RequestedDelta(axis=MotivationAxis.WINNING, requested=5.0, event_type="test"),)
    _, written = fold_satisfaction_deltas(current, requested)
    assert written[0].note is None


# ---------------------------------------------------------------------------
# Decision P4-45 -- morale (stateless) and trust (accumulated, decays)
# ---------------------------------------------------------------------------


def test_compute_morale_is_a_stateless_weighted_rollup():
    weights = {axis: 20 for axis in MotivationAxis}
    satisfactions = {axis: 0 for axis in MotivationAxis}
    satisfactions[MotivationAxis.WINNING] = 100
    # Equal weights, one axis maxed -- morale is that axis's share of the
    # weighted total.
    assert compute_morale(weights, satisfactions) == 20


def test_compute_morale_zero_weight_sum_guard_is_zero_not_zerodivisionerror():
    weights = {axis: 0 for axis in MotivationAxis}
    satisfactions = {axis: 100 for axis in MotivationAxis}
    assert compute_morale(weights, satisfactions) == 0


def test_compute_morale_never_exceeds_the_schema_range():
    weights = {axis: 100 for axis in MotivationAxis}
    satisfactions = {axis: 100 for axis in MotivationAxis}
    assert compute_morale(weights, satisfactions) == 100
    satisfactions = {axis: -100 for axis in MotivationAxis}
    assert compute_morale(weights, satisfactions) == -100


def test_decay_trust_moves_toward_zero():
    assert decay_trust(100) < 100
    assert decay_trust(-100) > -100
    assert decay_trust(0) == 0


def test_fold_trust_deltas_saturates_and_writes_zero_rows_like_satisfaction():
    new_trust, written = fold_trust_deltas(95, (10.0,))
    assert new_trust == 100
    assert written[0].applied == 5

    new_trust, written = fold_trust_deltas(-100, (-10.0,))
    assert new_trust == -100
    assert written[0].applied == 0


def test_fold_trust_deltas_clamps_an_out_of_range_current_even_with_no_requested_deltas():
    """Round-5 critic finding B4: `current` must be clamped on entry, not
    only when a delta happens to move it -- production calls this every
    Stage B checkpoint with an empty `requested` sequence (decay only),
    and the write-boundary clamp Decision P4-45 requires must hold on
    that call shape too."""
    new_trust, written = fold_trust_deltas(150, ())
    assert new_trust == 100
    assert written == ()

    new_trust, written = fold_trust_deltas(-150, ())
    assert new_trust == -100


# ---------------------------------------------------------------------------
# Decision P4-14/P4-56/P4-61/P4-70/P4-71 -- ladder hysteresis+dwell (§10
# test 3), the mandated construction: frozen literal checkpoints, an
# implementation-side mutation, exact-equality assertions.
# ---------------------------------------------------------------------------

# P4-70's guard: 21 < S < 41 < E < 61.
_E = 60.0
_S = 40.0


def test_ladder_bars_guard_admits_the_frozen_fixture():
    assert 21 < _S < 41 < _E < 61


def test_ladder_bar_rejects_a_non_hysteresis_pair():
    with pytest.raises(ValueError):
        LadderBar(enter=40.0, stay=40.0)


def _run_single_rung(checkpoints: list[tuple[int, float]], *, enter: float, stay: float) -> int:
    """Drives `step_ladder_rung` across `(season, value)` checkpoints,
    using `dwell_is_open` exactly as a real caller (agency_service/
    harness) would, and returns the number of transitions observed."""
    currently_in = False
    last_transition_season: int | None = None
    transitions = 0
    for season, value in checkpoints:
        dwell_open = dwell_is_open(
            current_season=season, last_transition_season=last_transition_season
        )
        new_in = step_ladder_rung(
            currently_in=currently_in,
            value=value,
            enter_bar=enter,
            stay_bar=stay,
            dwell_open=dwell_open,
        )
        if new_in != currently_in:
            transitions += 1
            last_transition_season = season
        currently_in = new_in
    return transitions


def _mutated_step(
    *, currently_in: bool, value: float, enter_bar: float, stay_bar: float, dwell_open: bool
) -> bool:
    """The mutation P4-61/P4-70 name: replace the stay-bar comparison
    with the enter-bar comparison while `currently_in`."""
    if currently_in:
        return value >= enter_bar  # BUG: should be stay_bar
    return value >= enter_bar and dwell_open


def _run_single_rung_mutated(checkpoints: list[tuple[int, float]]) -> int:
    currently_in = False
    last_transition_season: int | None = None
    transitions = 0
    for season, value in checkpoints:
        dwell_open = dwell_is_open(
            current_season=season, last_transition_season=last_transition_season
        )
        new_in = _mutated_step(
            currently_in=currently_in,
            value=value,
            enter_bar=_E,
            stay_bar=_S,
            dwell_open=dwell_open,
        )
        if new_in != currently_in:
            transitions += 1
            last_transition_season = season
        currently_in = new_in
    return transitions


def test_ladder_hysteresis_positive_case_exactly_one_transition():
    """P4-70's positive case: literals `61, 41, 61, 41`, one season each
    (P4-11's two-checkpoints-per-season doesn't matter here since no
    downward-then-upward dwell gate is exercised) -- `transitions == 1`,
    never `<= 1` (§18.2 dispute 1)."""
    checkpoints = [(1, 61.0), (1, 41.0), (2, 61.0), (2, 41.0)]
    assert _run_single_rung(checkpoints, enter=_E, stay=_S) == 1


def test_ladder_hysteresis_positive_case_mutation_must_fail():
    """The mutation P4-61/P4-70 name: replace the stay-bar comparison
    with the enter-bar comparison while `currently_in`. Reproduced here
    directly (not by editing production code) to prove the positive
    case's assertion actually depends on the two-bar mechanism."""
    transitions = _run_single_rung_mutated([(1, 61.0), (1, 41.0), (2, 61.0), (2, 41.0)])
    assert transitions >= 2
    assert transitions != 1


def test_ladder_hysteresis_negative_case_exactly_four_transitions():
    """P4-70's negative case: literals `61, 21, 61, 21` across two
    seasons (CP1/CP2 season 1, CP3/CP4 season 2) -- `transitions == 4`.
    CP3's re-entry is only legal because dwell has opened by season 2
    (P4-61 part 2: dwell gates upward only; P4-71: it was open from CP1
    anyway since there were no prior rows)."""
    checkpoints = [(1, 61.0), (1, 21.0), (2, 61.0), (2, 21.0)]
    assert _run_single_rung(checkpoints, enter=_E, stay=_S) == 4


def test_ladder_hysteresis_negative_case_also_fails_the_mutation():
    """P4-70 §3's own disclosure: the negative case does NOT discriminate
    the single-bar mutant (both read `4`) -- named explicitly so the
    positive case is never mistaken for redundant."""
    transitions = _run_single_rung_mutated([(1, 61.0), (1, 21.0), (2, 61.0), (2, 21.0)])
    assert transitions == 4  # does not discriminate -- P4-70 §3's own disclosure




def test_dwell_is_open_with_no_prior_rows_is_open_not_zero_sentinel():
    """Decision P4-71: the sentinel is `None`, never `0` -- a mirror
    initialised to `0` would accidentally also read "open" for every real
    season, which is why this is asserted against both spellings
    directly rather than only through the ladder-fixture tests above."""
    assert dwell_is_open(current_season=1, last_transition_season=None) is True
    # A `0` sentinel would also read open here (season 1 - 0 = 1 >= 1),
    # which is exactly the "accidentally correct" trap P4-71 names -- so
    # this assertion alone doesn't distinguish the two spellings. The
    # real distinguishing case is season 0 itself:
    assert dwell_is_open(current_season=0, last_transition_season=None) is True
    assert dwell_is_open(current_season=0, last_transition_season=0) is False


def test_dwell_is_open_requires_a_full_season_elapsed():
    assert dwell_is_open(current_season=5, last_transition_season=5) is False
    assert dwell_is_open(current_season=6, last_transition_season=5) is True


# ---------------------------------------------------------------------------
# advance_request_state -- the composed, multi-rung ladder
# ---------------------------------------------------------------------------


def test_advance_request_state_escalates_one_rung_at_a_time():
    state, transitioned = advance_request_state(
        RequestState.CONTENT, pressure=100.0, dwell_open=True
    )
    assert transitioned
    assert state is RequestState.UNSETTLED  # not straight to holdout


def test_advance_request_state_no_upward_skip_even_across_two_checkpoints():
    state = RequestState.CONTENT
    for _ in range(3):
        state, _ = advance_request_state(state, pressure=100.0, dwell_open=True)
    assert state is RequestState.HOLDOUT  # took 3 checkpoints, one rung each


def test_advance_request_state_downward_can_clear_multiple_rungs_at_once():
    state, transitioned = advance_request_state(
        RequestState.HOLDOUT, pressure=-100.0, dwell_open=True
    )
    assert transitioned
    assert state is RequestState.CONTENT


def test_advance_request_state_dwell_blocks_upward_only():
    # Escalate once (dwell irrelevant on the first move -- no prior rows).
    state, _ = advance_request_state(RequestState.CONTENT, pressure=100.0, dwell_open=True)
    assert state is RequestState.UNSETTLED
    # Immediately try to escalate again with dwell closed -- blocked.
    state, transitioned = advance_request_state(state, pressure=100.0, dwell_open=False)
    assert not transitioned
    assert state is RequestState.UNSETTLED
    # But a downward move is never dwell-gated.
    state, transitioned = advance_request_state(state, pressure=-100.0, dwell_open=False)
    assert transitioned
    assert state is RequestState.CONTENT


def test_advance_request_state_picks_trade_requested_when_years_remain():
    state, _ = advance_request_state(
        RequestState.UNSETTLED, pressure=100.0, dwell_open=True, contract_years_remaining=3
    )
    assert state is RequestState.TRADE_REQUESTED


def test_advance_request_state_picks_extension_demanded_near_expiry():
    state, _ = advance_request_state(
        RequestState.UNSETTLED, pressure=100.0, dwell_open=True, contract_years_remaining=1
    )
    assert state is RequestState.EXTENSION_DEMANDED


def test_advance_request_state_keeps_existing_flavor_while_holding_rung_2():
    state, transitioned = advance_request_state(
        RequestState.EXTENSION_DEMANDED, pressure=55.0, dwell_open=True, contract_years_remaining=3
    )
    assert not transitioned
    assert state is RequestState.EXTENSION_DEMANDED  # not silently re-flipped to TRADE_REQUESTED


# ---------------------------------------------------------------------------
# Round-5 critic finding B1 -- a de-escalation from HOLDOUT to rung 2 must
# resolve TRADE_REQUESTED/EXTENSION_DEMANDED from the player's real
# contract state, not default to TRADE_REQUESTED because the downward
# branch never received `contract_years_remaining`.
# ---------------------------------------------------------------------------


def test_advance_request_state_downward_deescalation_resolves_flavor_near_expiry():
    """A near-expiry player releasing from HOLDOUT straight to rung 2 must
    land on EXTENSION_DEMANDED, not TRADE_REQUESTED -- before the fix,
    `contract_years_remaining` was silently dropped on this branch and
    `_resolve_request_flavor(None)` always returns TRADE_REQUESTED."""
    state, transitioned = advance_request_state(
        RequestState.HOLDOUT,
        pressure=45.0,  # below rung 3's stay bar (60), above rung 2's (35)
        dwell_open=True,
        contract_years_remaining=1,
    )
    assert transitioned
    assert state is RequestState.EXTENSION_DEMANDED


def test_advance_request_state_downward_deescalation_resolves_flavor_with_term_remaining():
    """The same release, but with real term remaining -- must land on
    TRADE_REQUESTED, proving the fix reads the real value both ways
    rather than hardcoding a single flavor."""
    state, transitioned = advance_request_state(
        RequestState.HOLDOUT, pressure=45.0, dwell_open=True, contract_years_remaining=5
    )
    assert transitioned
    assert state is RequestState.TRADE_REQUESTED


# ---------------------------------------------------------------------------
# Round-5 critic finding B1 -- `resolve_request_opened_in_season`: a
# dwell/ladder transition must never give this column a second meaning.
# ---------------------------------------------------------------------------


def test_resolve_request_opened_in_season_sets_season_on_fresh_open():
    assert (
        resolve_request_opened_in_season(
            previous_state=RequestState.UNSETTLED,
            new_state=RequestState.TRADE_REQUESTED,
            season=2030,
            previous_request_opened_in_season=None,
        )
        == 2030
    )


def test_resolve_request_opened_in_season_unchanged_on_deescalation_that_stays_at_or_above_rung_2():
    """A request that genuinely opened in 2026 and de-escalates from
    HOLDOUT to TRADE_REQUESTED in 2028 must keep reading 2026 -- a dwell
    mechanism recording when a request opened is not the same field as
    when it last moved (Decision P4-56: this column "keeps its own
    job")."""
    assert (
        resolve_request_opened_in_season(
            previous_state=RequestState.HOLDOUT,
            new_state=RequestState.TRADE_REQUESTED,
            season=2028,
            previous_request_opened_in_season=2026,
        )
        == 2026
    )


def test_resolve_request_opened_in_season_clears_on_drop_to_unsettled():
    """Dropping from rung 2 to UNSETTLED (rung 1) has no open request
    either -- the pre-fix rule only cleared the column on a drop all the
    way to CONTENT, leaving it stranded, non-null, at UNSETTLED."""
    assert (
        resolve_request_opened_in_season(
            previous_state=RequestState.TRADE_REQUESTED,
            new_state=RequestState.UNSETTLED,
            season=2028,
            previous_request_opened_in_season=2026,
        )
        is None
    )


def test_resolve_request_opened_in_season_clears_on_drop_to_content():
    assert (
        resolve_request_opened_in_season(
            previous_state=RequestState.UNSETTLED,
            new_state=RequestState.CONTENT,
            season=2028,
            previous_request_opened_in_season=None,
        )
        is None
    )


def test_resolve_request_opened_in_season_no_op_when_state_unchanged():
    assert (
        resolve_request_opened_in_season(
            previous_state=RequestState.CONTENT,
            new_state=RequestState.CONTENT,
            season=2028,
            previous_request_opened_in_season=None,
        )
        is None
    )
    assert (
        resolve_request_opened_in_season(
            previous_state=RequestState.EXTENSION_DEMANDED,
            new_state=RequestState.EXTENSION_DEMANDED,
            season=2028,
            previous_request_opened_in_season=2026,
        )
        == 2026
    )


def test_pressure_from_morale_is_negated():
    assert pressure_from_morale(30) == -30.0
    assert pressure_from_morale(-30) == 30.0


# ---------------------------------------------------------------------------
# Decision P4-17/P4-51 -- the trade-request pressure signal (Stage C step 12)
# ---------------------------------------------------------------------------


def test_trade_request_pressure_is_none_below_the_star_leverage_threshold():
    """Decision P4-15/P4-37: a demand from a player who does not clear
    the binding gate is real (it still feeds media/satisfaction) but does
    not bind a GM's hand -- `None`, never a `0.0` "measured, no pressure"
    reading."""
    below = TRADE_REQUEST_BINDS_STAR_LEVERAGE_THRESHOLD - 1
    assert trade_request_pressure(
        request_state=RequestState.TRADE_REQUESTED, star_leverage=below, morale=-80
    ) is None
    assert trade_request_pressure(
        request_state=RequestState.HOLDOUT, star_leverage=below, morale=-80
    ) is None


def test_trade_request_pressure_is_none_for_non_trade_rungs():
    """`CONTENT`/`UNSETTLED` (below the rung P4-15's gate is about) and
    `EXTENSION_DEMANDED` (biases `rank_offers` at the re-sign window
    instead, Decision P4-18 -- never `propose_packages`) all read `None`,
    even at a star-clearing `star_leverage`."""
    above = TRADE_REQUEST_BINDS_STAR_LEVERAGE_THRESHOLD
    for state in (RequestState.CONTENT, RequestState.UNSETTLED, RequestState.EXTENSION_DEMANDED):
        assert trade_request_pressure(
            request_state=state, star_leverage=above, morale=-80
        ) is None


def test_trade_request_pressure_is_the_morale_pressure_reading_when_bound():
    """A player who clears the binding gate AND sits at a trade-reaching
    rung reads the SAME `pressure_from_morale` magnitude the ladder
    itself already computed -- no second, independently-tunable pressure
    formula."""
    above = TRADE_REQUEST_BINDS_STAR_LEVERAGE_THRESHOLD
    for state in (RequestState.TRADE_REQUESTED, RequestState.HOLDOUT):
        result = trade_request_pressure(request_state=state, star_leverage=above, morale=-40)
        assert result == pressure_from_morale(-40) == 40.0


# ---------------------------------------------------------------------------
# Decision P4-56 -- the largest-magnitude contributing axis
# ---------------------------------------------------------------------------


def test_largest_magnitude_axis_picks_the_biggest_mover():
    written = (
        WrittenDelta(axis=MotivationAxis.WINNING, applied=3, event_type="t", note=None),
        WrittenDelta(axis=MotivationAxis.MONEY, applied=-9, event_type="t", note=None),
    )
    assert largest_magnitude_axis(written) is MotivationAxis.MONEY


def test_largest_magnitude_axis_ties_broken_by_declaration_order():
    written = (
        WrittenDelta(axis=MotivationAxis.LEGACY, applied=5, event_type="t", note=None),
        WrittenDelta(axis=MotivationAxis.WINNING, applied=-5, event_type="t", note=None),
    )
    # WINNING is declared before LEGACY in MotivationAxis.
    assert largest_magnitude_axis(written) is MotivationAxis.WINNING


def test_largest_magnitude_axis_empty_falls_back_to_first_declared():
    assert largest_magnitude_axis(()) is next(iter(MotivationAxis))
