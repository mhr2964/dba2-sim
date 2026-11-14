"""`dba2/bot/gates.py` -- the real wiring of `command_gates.is_command_
legal` into a live handler (Decision P5-17, §1 finding 3)."""
from __future__ import annotations

import pytest

from dba2.bot.gates import (
    CommandNotLegalNow,
    ensure_command_legal,
    ensure_commissioner,
    ensure_team_manager,
)
from dba2.domain.league import League
from dba2.domain.phase import Phase
from dba2.domain.team import GmPhilosophy, Team, Timeline
from dba2.phase.permissions import PermissionDenied


def test_ensure_command_legal_is_silent_for_a_legal_phase():
    ensure_command_legal("standings", Phase.REGULAR_SEASON)


def test_ensure_command_legal_raises_for_an_illegal_phase():
    with pytest.raises(CommandNotLegalNow, match=r"propose-trade is not available during DRAFT"):
        ensure_command_legal("propose_trade", Phase.DRAFT)


def test_ensure_command_legal_raises_key_error_for_an_unregistered_command():
    """§1 finding 3's own real teeth: an unregistered command should
    fail loudly. `ensure_command_legal` deliberately does not catch this
    -- it is a shipped-with-no-gate bug (Decision P5-17), not a
    legal-phase rejection, and must not look like one."""
    with pytest.raises(KeyError):
        ensure_command_legal("nonexistent_command", Phase.REGULAR_SEASON)


def _league(**overrides) -> League:
    defaults = dict(
        id=1,
        discord_guild_id=1,
        name="Test League",
        start_season_year=2026,
        current_season=2026,
        current_phase=Phase.REGULAR_SEASON,
        phase_entered_at=None,
        phase_data={},
        commissioner_user_id=100,
        rng_seed=1,
        created_at=None,
        archived_at=None,
        advance_claimed_at=None,
        advance_claimed_by=None,
        hook_completed_cycle_index=-1,
        entry_work_completed_index=-1,
        closing_work_completed_index=-1,
    )
    defaults.update(overrides)
    return League(**defaults)


def test_ensure_commissioner_is_silent_for_the_commissioner():
    ensure_commissioner(100, _league(commissioner_user_id=100), action="setup")


def test_ensure_commissioner_raises_for_a_non_commissioner():
    with pytest.raises(PermissionDenied):
        ensure_commissioner(999, _league(commissioner_user_id=100), action="setup")


def _team(manager_user_id: int | None) -> Team:
    return Team(
        id=7, league_id=1, code="AAA", name="A", city="A", conference="East",
        division="Atlantic", manager_user_id=manager_user_id,
        gm_philosophy=GmPhilosophy.EGALITARIAN, timeline=Timeline.RETOOL, home_market_size=3,
    )


def test_ensure_team_manager_is_silent_for_the_manager_and_the_managing_commissioner():
    ensure_team_manager(200, _league(commissioner_user_id=100), _team(200), action="offer_respond")
    ensure_team_manager(100, _league(commissioner_user_id=100), _team(100), action="offer_respond")


def test_ensure_team_manager_denies_a_non_owning_commissioner_unlike_can_act_for_team():
    """The bot layer and the service layer share ONE predicate
    (`permissions.require_team_manager`), so this rejection is the service's."""
    with pytest.raises(PermissionDenied, match="/veto-offer"):
        ensure_team_manager(
            100, _league(commissioner_user_id=100), _team(200), action="propose_trade"
        )
