"""The trade-offer review surface's gate entries (docs/design/phase-5-trade-
offer-review-surface.md §8). `is_command_legal` raises `KeyError` on an
unregistered command by design, so a missing entry is a shipped-with-no-gate
bug; and the phase set is a LEAGUE RULE, so the exact membership is pinned
here -- changing it should be a deliberate edit of this file too.
"""
from __future__ import annotations

import pytest

from dba2.domain.phase import Phase
from dba2.phase.command_gates import (
    ALL_PHASES,
    COMMAND_GATES,
    TRADE_OPEN_PHASES,
    is_command_legal,
)

_TRADE_CLOSED = {
    Phase.POST_DEADLINE,
    Phase.REGULAR_SEASON_COMPLETE,
    Phase.PLAYIN,
    Phase.PLAYOFFS_R1,
    Phase.PLAYOFFS_R2,
    Phase.CONFERENCE_FINALS,
    Phase.FINALS,
    Phase.DRAFT,
}


def test_trade_open_phases_is_all_phases_minus_the_eight_closed_ones():
    assert TRADE_OPEN_PHASES == ALL_PHASES - _TRADE_CLOSED
    assert len(_TRADE_CLOSED) == 8 and _TRADE_CLOSED <= ALL_PHASES


def test_the_ai_trade_windows_are_inside_the_human_trade_window():
    """`run_phase_hook`'s trade branch fires only at these two phases; the
    human and AI paths must agree that a trade is legal there."""
    assert {Phase.REGULAR_SEASON, Phase.TRADE_DEADLINE} <= TRADE_OPEN_PHASES


@pytest.mark.parametrize("command", ["propose_trade", "offer_respond", "counter_offer"])
def test_the_three_trade_binding_commands_share_the_trade_open_set(command):
    assert COMMAND_GATES[command] == TRADE_OPEN_PHASES


@pytest.mark.parametrize("command", ["offers", "veto_offer"])
def test_the_read_and_the_commissioner_escape_hatch_are_legal_in_every_phase(command):
    assert COMMAND_GATES[command] == ALL_PHASES


def test_a_veto_is_legal_in_a_phase_that_closes_trading():
    assert not is_command_legal("offer_respond", Phase.PLAYOFFS_R1)
    assert is_command_legal("veto_offer", Phase.PLAYOFFS_R1)


def test_setup_is_open_for_trading_by_inheritance_not_by_the_nba_calendar():
    """§8's NIT 12 disclosure: `SETUP` is open because the subtraction leaves
    it open, the same way `ROSTER_TRANSACTION_PHASES` leaves it open."""
    assert Phase.SETUP in TRADE_OPEN_PHASES


def test_draft_is_legal_in_exactly_one_phase_and_agrees_with_the_roster_table():
    """P5-98.j: the table's only single-phase entry. Its reason is the
    complement of `ROSTER_TRANSACTION_PHASES`' own -- waive/release/sign are
    closed during DRAFT because "`apply_draft` is that phase's own exclusive
    roster-changing mechanism", and `/draft` IS that mechanism. The two sets
    are asserted together here because a change to either one alone would
    leave the league with two roster-changing mechanisms in one phase, or
    none."""
    assert COMMAND_GATES["draft"] == frozenset({Phase.DRAFT})
    assert is_command_legal("draft", Phase.DRAFT)
    assert not any(is_command_legal("draft", p) for p in Phase if p is not Phase.DRAFT)
    assert not is_command_legal("waive", Phase.DRAFT)
    # GM-6/GM-7/GM-8/GM-9/GM-10 (docs/design/gm-command-surface.md): 31 -> 34
    # -- set_strategy/set_role/set_directive/timeline renamed to gm_strategy/
    # gm_role/gm_directive/gm_timeline (net zero), plus gm_auto, gm_status
    # and gm_player added (net +3).
    assert len(COMMAND_GATES) == 34


@pytest.mark.parametrize(
    "command",
    [
        "advance",
        "advance_to",
        "advance_force",
        "advance_unlock",
        "advance_postpone_game",
        "advance_restore_game",
        "advance_run",
        "advance_run_stop",
    ],
)
def test_the_stage_e_commands_and_the_run_pair_are_legal_in_every_phase(command):
    """P5-99.a: commissioner escape hatches are never sometimes-available. The exit gate,
    not `COMMAND_GATES`, refuses a press -- they answer different questions. `/advance-run`
    and its stop (advance-run.md AR-10) are the same: what refuses a run is its route check
    and stop conditions."""
    assert COMMAND_GATES[command] == ALL_PHASES


def test_watch_is_legal_in_every_phase():
    """P5-97.r: watching a replay of a final game is never illegitimate, and a
    phase with nothing to watch answers with an honest sentence, not a phase
    error."""
    assert COMMAND_GATES["watch"] == ALL_PHASES
    assert all(is_command_legal("watch", phase) for phase in Phase)
