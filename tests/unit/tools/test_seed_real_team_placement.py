"""`tools.seed_dev_league._real_players_on_their_real_teams`: each team gets its own
franchise's players by minutes played, and a thin team is an error, never a silent
shortfall filled from another team."""

from __future__ import annotations

import pytest

from tools import seed_dev_league


def _row(ref: str, code: str, minutes: float) -> dict:
    return {"external_ref": ref, "team": {"abbreviation": code}, "meta": {"minutes_total": minutes}}


def test_each_team_gets_its_own_top_players_by_minutes() -> None:
    pool = [
        _row("a1", "AAA", 100),
        _row("a2", "AAA", 900),
        _row("a3", "AAA", 500),
        _row("b1", "BBB", 300),
        _row("b2", "BBB", 200),
    ]
    placed = seed_dev_league._real_players_on_their_real_teams(
        pool, team_codes=["AAA", "BBB"], per_team=2
    )
    assert [r["external_ref"] for r in placed[0]] == ["a2", "a3"]
    assert [r["external_ref"] for r in placed[1]] == ["b1", "b2"]


def test_a_team_short_of_real_players_is_an_error() -> None:
    pool = [_row("a1", "AAA", 100), _row("a2", "AAA", 90), _row("b1", "BBB", 50)]
    with pytest.raises(ValueError, match="BBB"):
        seed_dev_league._real_players_on_their_real_teams(
            pool, team_codes=["AAA", "BBB"], per_team=2
        )


def test_a_team_with_no_real_players_at_all_is_an_error() -> None:
    with pytest.raises(ValueError, match="ZZZ"):
        seed_dev_league._real_players_on_their_real_teams(
            [_row("a1", "AAA", 1)], team_codes=["ZZZ"], per_team=1
        )


def test_brooklyn_is_found_under_the_data_files_bkn_code() -> None:
    placed = seed_dev_league._real_players_on_their_real_teams(
        [_row("n1", "BKN", 10)], team_codes=["BRK"], per_team=1
    )
    assert [r["external_ref"] for r in placed[0]] == ["n1"]
