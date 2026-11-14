"""`presentation/leaders.py` v2 -- docs/design/leaders-v2-per-game-and-paging.md
LD2-5/LD2-6. No DB, no Discord -- `LeaderRow` is plain data the test constructs by
hand, the same shape `test_standings.py` uses for `StandingsRow`."""
from __future__ import annotations

from dba2.domain.stats import LeaderMode, LeaderStat
from dba2.presentation.leaders import LeaderRow, build_leaders_panel

_QUALIFY_FRACTION = 0.70


def _row(rank: int, name: str, team: str, total: int, games: int, player_id: int = 0) -> LeaderRow:
    return LeaderRow(
        player_id=player_id or rank,
        player_name=name,
        team_code=team,
        stat_total=total,
        games_played=games,
        rank=rank,
    )


def _rows() -> list[LeaderRow]:
    return [
        _row(1, "Luka Doncic", "DAL", 820, 20),
        _row(2, "Nikola Jokic", "DEN", 780, 20),
    ]


def _panel(
    *,
    rows,
    mode=LeaderMode.PER_GAME,
    stat=LeaderStat.POINTS,
    bar_applied=True,
    qualify_fraction=_QUALIFY_FRACTION,
    min_team_games=54,
    max_team_games=72,
):
    return build_leaders_panel(
        league_name="Test League",
        season=2026,
        stat=stat,
        mode=mode,
        rows=rows,
        bar_applied=bar_applied,
        qualify_fraction=qualify_fraction,
        min_team_games=min_team_games,
        max_team_games=max_team_games,
    )


def test_leaders_panel_title_names_the_stat_and_season():
    panel = _panel(rows=_rows())
    assert "Points Leaders" in panel.title
    assert "2026" in panel.title


def test_leaders_panel_preserves_given_order_never_resorts():
    """Decision P5-5: `view_service.get_leaders_panel` already ranked these via
    `stats_repo.list_live_stat_leaders`'s own `ROW_NUMBER`; this module must not
    re-sort -- it prints `row.rank` verbatim, in the order given."""
    reversed_rows = list(reversed(_rows()))
    panel = _panel(rows=reversed_rows)
    assert panel.description.index("Jokic") < panel.description.index("Doncic")


def test_leaders_panel_computes_per_game_average_from_measured_values_only():
    rows = [_row(1, "X", "AAA", 100, 10)]
    panel = _panel(rows=rows, stat=LeaderStat.ASSISTS)
    assert "10.0" in panel.description


def test_leaders_panel_handles_zero_games_played_without_dividing_by_zero():
    rows = [_row(1, "X", "AAA", 0, 0)]
    panel = _panel(rows=rows, stat=LeaderStat.BLOCKS)
    assert "0.0" in panel.description


# --- LD2-5's 47-character line, machine-checked -------------------------------------


def _lines(panel) -> list[str]:
    body = panel.description.strip("`\n")
    return body.split("\n")


def test_every_rendered_line_is_exactly_47_characters_header_included():
    rows = [
        _row(11, "Darius Whitlock", "MEM", 948, 39),
        _row(12, "Andre Sackey", "POR", 916, 38),
        _row(13, "Konstantinos Papadopoulos", "CHA", 880, 37),  # 25 chars -> truncates
        _row(20, "Trey Okonkwo", "SAS", 843, 39),
    ]
    panel = _panel(rows=rows, min_team_games=54, max_team_games=72)
    lines = _lines(panel)
    assert len(lines) == 1 + len(rows)  # header + one line per row
    for line in lines:
        assert len(line) == 47, repr(line)


def test_absolute_ranks_are_printed_on_page_2_never_renumbered_from_one():
    rows = [_row(11, "A", "AAA", 100, 10), _row(12, "B", "BBB", 90, 10)]
    panel = _panel(rows=rows)
    lines = _lines(panel)
    assert lines[1].lstrip().startswith("11")
    assert lines[2].lstrip().startswith("12")


def test_a_name_longer_than_18_characters_truncates_to_17_plus_ellipsis():
    long_name = "Konstantinos Papadopoulos"  # 26 characters
    rows = [_row(13, long_name, "CHA", 880, 37)]
    panel = _panel(rows=rows)
    line = _lines(panel)[1]
    # rank "13" (3) + sep(2) + the 18-wide name field starts at index 5
    name_field = line[5 : 5 + 18]
    assert name_field == long_name[:17] + "…"


def test_a_team_code_longer_than_3_is_sliced_never_shifting_the_column():
    rows = [_row(1, "X", "ABCDE", 100, 10)]
    panel = _panel(rows=rows)
    line = _lines(panel)[1]
    # rank(3) + sep(2) + name(18) + sep(2) = 25 -> the 3-wide team field starts at 25
    team_field = line[25:28]
    assert team_field == "ABC"


# --- footers ------------------------------------------------------------------------


def test_per_game_footer_states_the_qualification_rule_and_team_range():
    panel = _panel(rows=_rows(), mode=LeaderMode.PER_GAME, min_team_games=54, max_team_games=72)
    assert panel.footer == (
        "Per Game · Qualified: 70% of own team's games · Teams: 54–72 played · Rank 1–2"
    )


def test_total_footer_drops_the_bar_entirely():
    panel = _panel(
        rows=_rows(), mode=LeaderMode.TOTAL, bar_applied=False, min_team_games=54, max_team_games=72
    )
    assert panel.footer == "Total · all players · Rank 1–2"


# --- the two empty branches, distinguished -------------------------------------------


def test_no_final_games_at_all_shows_the_v1_message_and_no_footer():
    panel = _panel(rows=[], min_team_games=None, max_team_games=None)
    assert panel.title
    assert panel.description == "No games played yet this season."
    assert panel.footer == ""


def test_games_exist_but_nobody_qualifies_is_a_distinct_message_with_a_footer():
    panel = _panel(rows=[], min_team_games=54, max_team_games=72)
    assert panel.description == "No player has played enough games to qualify yet."
    assert panel.footer == "Per Game · Qualified: 70% of own team's games · Teams: 54–72 played"


def test_the_two_empty_branches_are_never_confused():
    no_games = _panel(rows=[], min_team_games=None, max_team_games=None)
    nobody_qualifies = _panel(rows=[], min_team_games=54, max_team_games=72)
    assert no_games.description != nobody_qualifies.description
    assert no_games.footer != nobody_qualifies.footer
