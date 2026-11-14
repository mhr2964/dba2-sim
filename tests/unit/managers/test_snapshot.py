"""Tier 1: `RosterPlayer`/`TeamSnapshot`/`LeagueSnapshot` -- Decision P2-7's
`Decimal` -> `float` boundary and Decision P2-5's join-shaped-type
construction (docs/design/phase-2-manager-minds.md)."""
from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from dba2.managers.snapshot import (
    build_league_snapshot,
    build_roster_player,
    build_team_snapshot,
)
from tests.fixtures.sim import make_player, make_ratings, make_team

_CAP_SETTINGS_KWARGS = dict(
    league_id=1,
    season=2026,
    salary_cap=154_647_000,
    luxury_tax=187_895_000,
    apron_1=195_945_000,
    apron_2=207_824_000,
    max_pct_0_6=25,
    max_pct_7_9=30,
    max_pct_10_plus=35,
    supermax_pct=35,
    min_salary=1_272_870,
    mle_amount=14_104_000,
    bi_annual_amount=5_130_000,
    raise_pct_bird=8,
    raise_pct_non_bird=5,
    roster_min=14,
    roster_max=15,
)


def _make_cap_settings():
    from dba2.domain.league import LeagueCapSettings

    return LeagueCapSettings(**_CAP_SETTINGS_KWARGS)


def test_build_roster_player_converts_decimal_confidence_to_float():
    player = make_player(1)
    ratings = dataclasses.replace(make_ratings(1), confidence=Decimal("0.42"))
    roster_player = build_roster_player(player, ratings)

    assert isinstance(roster_player.confidence_f, float)
    assert roster_player.confidence_f == pytest.approx(0.42)
    # Mirror, not a destructive replace (same contract sim/context.py's
    # build_sim_player already states).
    assert isinstance(ratings.confidence, Decimal)
    assert roster_player.id == player.id
    assert roster_player.contract is None


def test_build_team_snapshot_defaults_window_to_none():
    team = make_team(10)
    cap_settings = _make_cap_settings()
    roster = (build_roster_player(make_player(1, team_id=10), make_ratings(1)),)
    snapshot = build_team_snapshot(team, roster, (), cap_settings)

    assert snapshot.window is None
    assert snapshot.id == 10
    assert snapshot.player_by_id(1) is roster[0]
    with pytest.raises(KeyError):
        snapshot.player_by_id(999)


def test_build_league_snapshot_composes_team_snapshots():
    team_a = make_team(10)
    team_b = make_team(11)
    cap_settings = _make_cap_settings()
    snap_a = build_team_snapshot(team_a, (), (), cap_settings)
    snap_b = build_team_snapshot(team_b, (), (), cap_settings)

    league_snapshot = build_league_snapshot(1, 2026, (snap_a, snap_b))
    assert league_snapshot.team_by_id(10) is snap_a
    assert league_snapshot.team_by_id(11) is snap_b
    with pytest.raises(KeyError):
        league_snapshot.team_by_id(999)
