"""`presentation/team.py`'s own tests -- Phase 5 Stage B step 6 (docs/
design/phase-5-discord-presentation.md §11, §9.1's golden files, §9.2's
limit invariants). No DB, no Discord -- `Team`/`Player`/`CapSummary` are
real `dba2.domain` dataclasses constructed by hand, the same way `view_
service`'s own integration test proves the real repository/service reads
that feed them, never here."""
from __future__ import annotations

from datetime import UTC, date, datetime

from dba2.domain.league import CapSummary
from dba2.domain.player import Player, Position, RosterStatus
from dba2.domain.team import GmPhilosophy, Team, Timeline, TimelineSource
from dba2.presentation.team import build_team_panel
from tests.unit.presentation._golden_helpers import load_golden, panel_to_dict


def _team() -> Team:
    return Team(
        id=1, league_id=1, code="BOS", city="Boston", name="Celtics", conference="East",
        division="Atlantic", manager_user_id=None, gm_philosophy=GmPhilosophy.EGALITARIAN,
        timeline=Timeline.WIN_NOW, home_market_size=5, timeline_source=TimelineSource.AUTO,
    )


def _player(i: int, *, overall: int, position: Position = Position.PG) -> Player:
    return Player(
        id=i, league_id=1, first_name=f"First{i}", last_name=f"Last{i}", external_ref=None,
        primary_position=position, secondary_position=None, height_in=78, weight_lb=210,
        wingspan_in=80, birth_date=date(1998, 1, 1), draft_year=None, draft_pick_number=None,
        years_pro=5, team_id=1, roster_status=RosterStatus.ACTIVE, overall_cached=overall,
        overall_computed_at=datetime(2026, 1, 1, tzinfo=UTC), retired_in_season=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC), updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _roster() -> list[Player]:
    return [
        _player(1, overall=85, position=Position.PG),
        _player(2, overall=91, position=Position.C),
        _player(3, overall=78, position=Position.SF),
    ]


def _cap_summary() -> CapSummary:
    return CapSummary(
        team_id=1, season=2026, salary_cap=140_000_000, committed_salary=130_000_000,
        dead_cap=2_000_000, cap_holds=0, include_holds=False, used=132_000_000,
        room=8_000_000,
    )


def test_team_panel_matches_committed_golden_element_wise():
    panel = build_team_panel(
        team=_team(), wins=50, losses=20, streak_length=3, standings_position=2,
        roster=_roster(), cap_summary=_cap_summary(),
    )
    assert panel_to_dict(panel) == load_golden("presentation_team.json")


def test_team_panel_roster_sorted_by_overall_descending_never_recomputed():
    """The roster order is a sort over already-given `overall_cached`
    values -- never a recomputed rating (Decision P5-5)."""
    panel = build_team_panel(
        team=_team(), wins=50, losses=20, streak_length=3, standings_position=2,
        roster=_roster(), cap_summary=_cap_summary(),
    )
    lines = panel.description.splitlines()
    assert "First2 Last2" in lines[2]  # overall 91, highest
    assert "First1 Last1" in lines[3]  # overall 85
    assert "First3 Last3" in lines[4]  # overall 78, lowest


def test_team_panel_fifteen_man_max_length_name_roster_stays_within_limits():
    """§9.2's adversarial corpus, applied to a 15-man roster of maximum-
    length names -- the exact case §9.2 names by example."""
    roster = [
        Player(
            id=i, league_id=1, first_name="F" * 40, last_name="L" * 40, external_ref=None,
            primary_position=Position.SF, secondary_position=None, height_in=78,
            weight_lb=210, wingspan_in=80, birth_date=date(1998, 1, 1), draft_year=None,
            draft_pick_number=None, years_pro=5, team_id=1, roster_status=RosterStatus.ACTIVE,
            overall_cached=99 - i, overall_computed_at=datetime(2026, 1, 1, tzinfo=UTC),
            retired_in_season=None, created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for i in range(15)
    ]
    team = Team(
        id=1, league_id=1, code="XXX", city="C" * 60, name="N" * 40, conference="East",
        division="Atlantic", manager_user_id=None, gm_philosophy=GmPhilosophy.EGALITARIAN,
        timeline=Timeline.WIN_NOW, home_market_size=5, timeline_source=TimelineSource.AUTO,
    )
    panel = build_team_panel(
        team=team, wins=50, losses=20, streak_length=3, standings_position=2,
        roster=roster, cap_summary=_cap_summary(),
    )
    assert len(panel.description) <= 4096
    for field in panel.fields:
        assert len(field.value) <= 1024
