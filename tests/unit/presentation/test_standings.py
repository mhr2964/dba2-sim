"""`presentation/standings.py`'s own tests -- Phase 5 Stage B step 6
(docs/design/phase-5-discord-presentation.md §11, §9.1's golden files,
§9.2's limit invariants, §13.3's "watch for a panel builder computing its
own win-loss record" warning) plus docs/design/standings-v2-conference-
split.md SV-1 through SV-5. No DB, no Discord -- `StandingsRow` is plain
data the test constructs by hand, the same way `view_service`'s own
integration test proves the real mapping from a `TeamPulseState` into one
of these, never here."""
from __future__ import annotations

import pytest

from dba2.presentation.standings import StandingsRow, build_standings_panel
from tests.unit.presentation._golden_helpers import load_golden, panel_to_dict


def _row(
    team_id: int,
    code: str,
    city: str,
    name: str,
    conference: str,
    division: str,
    wins: int,
    losses: int,
    conference_rank: int,
    win_pct: float,
    games_behind: float | None,
    standings_position: int,
) -> StandingsRow:
    return StandingsRow(
        team_id=team_id,
        code=code,
        city=city,
        name=name,
        conference=conference,
        division=division,
        wins=wins,
        losses=losses,
        standings_position=standings_position,
        conference_rank=conference_rank,
        win_pct=win_pct,
        games_behind=games_behind,
    )


def _fixture_rows() -> list[StandingsRow]:
    """Two conferences, two teams each, each conference independently
    ranked -- BOS leads East at `.786`; NYK is 7.0 back. LAL leads West at
    `.629`; GSW is 4.0 back."""
    return [
        _row(1, "BOS", "Boston", "Celtics", "East", "Atlantic", 55, 15, 1, 55 / 70, None, 1),
        _row(2, "NYK", "New York", "Knicks", "East", "Atlantic", 48, 22, 2, 48 / 70, 7.0, 2),
        _row(3, "LAL", "Los Angeles", "Lakers", "West", "Pacific", 44, 26, 1, 44 / 70, None, 3),
        _row(4, "GSW", "Golden State", "Warriors", "West", "Pacific", 40, 30, 2, 40 / 70, 4.0, 4),
    ]


# --- golden files -----------------------------------------------------------------------------


def test_standings_panel_matches_committed_golden_element_wise():
    panel = build_standings_panel(league_name="Test League", season=2026, rows=_fixture_rows())
    assert panel_to_dict(panel) == load_golden("presentation_standings.json")


def test_standings_panel_filtered_by_conference_matches_committed_golden():
    panel = build_standings_panel(
        league_name="Test League", season=2026, rows=_fixture_rows(), group="East"
    )
    assert panel_to_dict(panel) == load_golden("presentation_standings_east.json")


# --- layout: two fields by default, one when narrowed ------------------------------------------


def test_standings_panel_default_group_renders_two_conference_fields_no_description():
    panel = build_standings_panel(league_name="Test League", season=2026, rows=_fixture_rows())
    assert panel.description == ""
    assert [f.name for f in panel.fields] == ["East", "West"]
    assert all(f.inline for f in panel.fields)


def test_standings_panel_narrowed_group_renders_one_field():
    panel = build_standings_panel(
        league_name="Test League", season=2026, rows=_fixture_rows(), group="West"
    )
    assert panel.description == ""
    assert [f.name for f in panel.fields] == ["West"]


def test_standings_panel_preserves_given_order_never_resorts():
    """Decision P5-5/§13.3's own named failure mode, checked directly:
    feeding one conference's rows already ranked WORST-first must render
    worst-first -- proof this module never re-sorts by wins/losses/win_pct
    itself, which would silently paper over a caller bug (or a real
    divergence from `media_service`'s own ranking) instead of surfacing
    it."""
    east_worst_first = list(reversed([r for r in _fixture_rows() if r.conference == "East"]))
    panel = build_standings_panel(
        league_name="Test League", season=2026, rows=east_worst_first, group="East"
    )
    lines = panel.fields[0].value.splitlines()
    assert "Knicks" in lines[2]
    assert "Celtics" in lines[3]


def test_standings_panel_unknown_group_raises():
    with pytest.raises(ValueError, match="No teams match"):
        build_standings_panel(league_name="L", season=2026, rows=_fixture_rows(), group="Nowhere")


def test_standings_panel_empty_rows_renders_one_empty_header_only_field():
    panel = build_standings_panel(league_name="L", season=2026, rows=[])
    assert panel.description == ""
    (field,) = panel.fields
    assert field.value.count("\n") == 2  # ``` + header + ```


# --- SV-2: percentage formatting -----------------------------------------------------------


def test_standings_panel_pct_strips_leading_zero_and_keeps_full_1_000():
    rows = [
        _row(1, "BOS", "Boston", "Celtics", "East", "A", 3, 1, 1, 0.75, None, 1),
        _row(2, "BKN", "Brooklyn", "Nets", "West", "B", 14, 0, 1, 1.0, None, 2),
    ]
    panel = build_standings_panel(league_name="L", season=2026, rows=rows)
    east_value = panel.fields[0].value
    west_value = panel.fields[1].value
    assert ".750" in east_value and ".000" not in east_value
    assert "1.000" in west_value


def test_standings_panel_zero_games_played_team_renders_dash_pct_and_dash_gb():
    rows = [
        _row(1, "EXP", "Expansion", "Team", "East", "Atlantic", 0, 0, 1, 0.0, None, 1),
    ]
    panel = build_standings_panel(league_name="L", season=2026, rows=rows)
    value = panel.fields[0].value
    assert " 0 - 0" in value
    assert "-.--" in value
    assert ".000" not in value  # never reads as "winless"


# --- SV-3: games-behind ----------------------------------------------------------------------


def test_standings_panel_gb_leader_and_zero_games_both_render_dash():
    rows = [
        _row(1, "BOS", "Boston", "Celtics", "East", "A", 10, 0, 1, 1.0, None, 1),
        _row(2, "EXP", "Expansion", "Team", "East", "A", 0, 0, 2, 0.0, None, 2),
    ]
    panel = build_standings_panel(league_name="L", season=2026, rows=rows, group="East")
    lines = panel.fields[0].value.splitlines()
    assert lines[2].split()[2] == "-"  # the leader's own GB column
    assert lines[3].split()[1] == "-.--"  # the zero-games team's Pct column
    assert lines[3].split()[2] == "-"  # the zero-games team's own GB column
    assert lines[3].rstrip().endswith("Expansion Team")


def test_standings_panel_gb_floors_at_zero_never_renders_negative():
    """SV-3's own worked example: a 14-0 leader against a 35-15 second
    team computes `((14-35)+(15-0))/2 = -3.0` under the naive formula --
    floored to `0.0` here, never rendered negative."""
    rows = [
        _row(1, "BOS", "Boston", "Celtics", "West", "A", 14, 0, 1, 1.0, None, 1),
        _row(2, "NYK", "New York", "Knicks", "West", "A", 35, 15, 2, 0.7, 0.0, 2),
    ]
    panel = build_standings_panel(league_name="L", season=2026, rows=rows, group="West")
    value = panel.fields[0].value
    assert "0.0" in value
    assert "-3.0" not in value and "-3" not in value


def test_standings_panel_a_genuine_tie_shows_one_dash_and_one_zero_point_zero():
    """A real tie for the conference lead: exactly one team ranks first
    (`-`), the other shows `0.0` -- it HAS a record, and that record ties
    the leader's."""
    rows = [
        _row(1, "BOS", "Boston", "Celtics", "East", "A", 10, 10, 1, 0.5, None, 1),
        _row(2, "NYK", "New York", "Knicks", "East", "A", 10, 10, 2, 0.5, 0.0, 2),
    ]
    panel = build_standings_panel(league_name="L", season=2026, rows=rows, group="East")
    lines = panel.fields[0].value.splitlines()
    assert lines[2].split()[2] == "-"
    assert lines[3].split()[2] == "0.0"


# --- field-width bound (Layout, "checked, not assumed") -----------------------------------------


def test_standings_panel_thirty_teams_max_length_names_stays_within_field_limit():
    """Layout's own adversarial corpus: 15 teams per conference, each named
    with the real 30-team seed's longest real `city`+`name` combo
    (`Minnesota Timberwolves`, 22 characters) -- every conference field
    must stay under `PanelField.value`'s 1024-char ceiling."""
    rows = [
        _row(
            i,
            f"T{i:02d}",
            "Minnesota",
            "Timberwolves",
            "East" if i < 15 else "West",
            "Atlantic",
            50 - (i % 15),
            30 + (i % 15),
            (i % 15) + 1,
            (50 - (i % 15)) / 80,
            None if (i % 15) == 0 else float(i % 15),
            i + 1,
        )
        for i in range(30)
    ]
    panel = build_standings_panel(league_name="L" * 50, season=2026, rows=rows)
    assert len(panel.fields) == 2
    for field in panel.fields:
        assert len(field.value) <= 1024
    assert len(panel.title) <= 256
