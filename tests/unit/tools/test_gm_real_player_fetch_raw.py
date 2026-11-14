"""RPD-8-1: the raw-evidence fetch's pure reduction, resumability and key handling.
No network: the two fetchers are replaced with canned rows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import _gm_real_player_fetch_raw as fetch_raw


def _row(pid: int, date: str, team: str, **stats: float) -> dict:
    base = {
        "min": "30:00",
        "pts": 20,
        "fgm": 8,
        "fga": 16,
        "fg3m": 2,
        "fg3a": 5,
        "ftm": 2,
        "fta": 3,
        "oreb": 1,
        "dreb": 5,
        "ast": 4,
        "stl": 1,
        "blk": 0,
        "turnover": 2,
        "pf": 2,
    }
    base.update(stats)
    return {
        **base,
        "player": {
            "id": pid,
            "first_name": "A",
            "last_name": f"P{pid}",
            "position": "G",
            "height": "6-3",
            "weight": "195",
            "draft_year": 2015,
        },
        "team": {"abbreviation": team},
        "game": {"date": date},
    }


def test_totals_games_minutes_and_one_game_score_per_game() -> None:
    rows = [_row(1, "2024-10-25", "BOS"), _row(1, "2024-10-27", "BOS", pts=30)]
    seed = fetch_raw.aggregate_raw_season(2024, rows)
    (player,) = seed["players"]
    assert player["games_played"] == 2
    assert player["minutes_total"] == 60.0
    assert player["totals"]["pts"] == 50.0
    assert player["totals"]["fg3a"] == 10.0
    assert len(player["game_scores"]) == 2


def test_zero_minute_rows_are_not_games() -> None:
    rows = [_row(1, "2024-10-25", "BOS"), _row(1, "2024-10-27", "BOS", min="0:00")]
    (player,) = fetch_raw.aggregate_raw_season(2024, rows)["players"]
    assert player["games_played"] == 1
    assert fetch_raw.aggregate_raw_season(2024, [_row(2, "2024-10-25", "BOS", min="")])[
        "players"
    ] == []


def test_season_opening_team_is_the_earliest_dated_row_whatever_the_row_order() -> None:
    rows = [
        _row(1, "2025-01-10", "NYK"),
        _row(1, "2024-10-25", "BOS"),
        _row(1, "2024-12-01", "BOS"),
    ]
    (player,) = fetch_raw.aggregate_raw_season(2024, rows)["players"]
    assert player["first_team"] == "BOS"
    assert player["first_game_date"] == "2024-10-25"


def test_advanced_fields_are_summed_with_counts_for_every_numeric_key() -> None:
    rows = [_row(1, "2024-10-25", "BOS")]
    advanced = [
        {
            "player": {"id": 1},
            "usage_percentage": 0.30,
            "true_shooting_percentage": None,
            "screen_assists": 2,
            "flag": True,
        },
        {"player": {"id": 1}, "usage_percentage": 0.20, "screen_assists": 1},
        {"player": {"id": 99}, "usage_percentage": 0.5},
    ]
    seed = fetch_raw.aggregate_raw_season(2024, rows, advanced)
    (player,) = seed["players"]
    block = player["advanced"]
    assert seed["advanced_present"] is True
    assert block["rows"] == 2  # the id-99 row has no box-score minutes and is dropped
    assert block["sums"]["usage_percentage"] == pytest.approx(0.5)
    assert block["counts"]["usage_percentage"] == 2
    assert "true_shooting_percentage" not in block["sums"]  # null is not a number
    assert "flag" not in block["sums"]  # a bool is not a number


def test_advanced_is_ignored_before_the_endpoint_floor() -> None:
    rows = [_row(1, "2010-10-25", "BOS")]
    seed = fetch_raw.aggregate_raw_season(2010, rows, [{"player": {"id": 1}, "x": 1}])
    assert seed["advanced_present"] is False
    assert "advanced" not in seed["players"][0]


def test_reduction_is_deterministic() -> None:
    rows = [_row(2, "2024-10-26", "LAL"), _row(1, "2024-10-25", "BOS")]
    first = fetch_raw.aggregate_raw_season(2024, rows)
    second = fetch_raw.aggregate_raw_season(2024, list(reversed(rows)))
    assert [p["external_ref"] for p in first["players"]] == ["1", "2"]
    assert first["players"] == second["players"]  # the sample row is order-dependent by design
    assert fetch_raw.aggregate_raw_season(2024, rows) == first


def test_an_existing_season_is_skipped_unless_forced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def fake_stats(season: int, key: str) -> list[dict]:
        calls.append(season)
        return [_row(1, "2024-10-25", "BOS")]

    monkeypatch.setattr(fetch_raw, "_fetch_season_rows", fake_stats)
    monkeypatch.setattr(fetch_raw, "_fetch_advanced_rows", lambda season, key: [])

    first = fetch_raw.fetch_season(2024, "k", raw_dir=tmp_path)
    assert "1 players" in first
    assert json.loads((tmp_path / "2024.json").read_text(encoding="utf-8"))["season"] == 2024
    assert "skipped" in fetch_raw.fetch_season(2024, "k", raw_dir=tmp_path)
    assert calls == [2024]
    fetch_raw.fetch_season(2024, "k", raw_dir=tmp_path, force=True)
    assert calls == [2024, 2024]
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_key_refuses_and_never_echoes_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BDL_API_KEY", raising=False)
    assert fetch_raw.main(["x", "2024", "2024"]) == 1
    assert "BDL_API_KEY" in capsys.readouterr().err


def test_bad_arguments_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BDL_API_KEY", "not-a-real-key")
    assert fetch_raw.main(["x", "2024"]) == 1
    assert fetch_raw.main(["x", "2025", "2024"]) == 1


def test_no_advanced_skips_the_advanced_endpoint_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def advanced_must_not_be_called(season: int, key: str) -> list[dict]:
        raise AssertionError("the advanced endpoint was called under advanced=False")

    monkeypatch.setattr(
        fetch_raw, "_fetch_season_rows", lambda season, key: [_row(1, "2024-10-25", "BOS")]
    )
    monkeypatch.setattr(fetch_raw, "_fetch_advanced_rows", advanced_must_not_be_called)
    status = fetch_raw.fetch_season(2024, "k", raw_dir=tmp_path, advanced=False)
    assert "advanced=no" in status
    stored = json.loads((tmp_path / "2024.json").read_text(encoding="utf-8"))
    assert stored["advanced_present"] is False
    assert "advanced" not in stored["players"][0]
