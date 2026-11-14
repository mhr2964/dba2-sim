"""RPC-13 (docs/design/real-player-rating-calibration.md): `tools/validate_sim.py`'s
real-player mode -- hydration mirrors `seed_dev_league`, non-real slots are untouched,
and the fidelity rows rank and grade what they say. No game is simulated here."""

from __future__ import annotations

import pytest

from dba2.domain.ratings import RatingSource
from dba2.sim.derivation import RATING_FIELD_ORDER
from tools import seed_dev_league
from tools import validate_sim as vs

_SEASON = 2025


def _row(ref: str, *, position: str = "C", height: int = 84) -> dict:
    ratings = dict.fromkeys(RATING_FIELD_ORDER, 60)
    ratings.update(peak_age_start=26, peak_age_end=30, rim_protection=91)
    return {
        "external_ref": ref,
        "first_name": "Real",
        "last_name": f"Player{ref}",
        "primary_position": position,
        "height_in": height,
        "weight_lb": 250,
        "draft_year": 2019,
        "season": _SEASON,
        "ratings": ratings,
        "confidence": 0.85,
        "derived_from_season": _SEASON,
        "meta": {"minutes_total": 2000.0},
        "team": {"abbreviation": "ATL"},
    }


def test_real_slots_hydrate_like_seed_dev_league_and_other_slots_do_not_move() -> None:
    real = {0: [_row("11"), _row("12", position="PG", height=74)]}
    plain = vs.generate_league(league_id=1, season=2026, rng_seed=7, players_per_team=14)
    mixed = vs.generate_league(
        league_id=1, season=2026, rng_seed=7, players_per_team=14, real_rows_by_team=real
    )
    first = mixed.players_by_team[1][0]
    assert first.external_ref == "11" and first.last_name == "Player11"
    assert first.wingspan_in == seed_dev_league.real_player_wingspan_in(
        real[0][0], rng_seed=7, team_index=0, slot_index=0
    )
    ratings = mixed.ratings_by_player[first.id]
    assert ratings.source is RatingSource.REAL_STAT_DERIVED
    assert ratings.rim_protection == 91 and ratings.derived_from_season == _SEASON
    # slots 2..13 of team 0 and every slot of the other teams: identical to a synthetic league
    for team_id, players in plain.players_by_team.items():
        start = 2 if team_id == 1 else 0
        for a, b in zip(players[start:], mixed.players_by_team[team_id][start:], strict=True):
            assert a == b
            assert plain.ratings_by_player[a.id] == mixed.ratings_by_player[b.id]
            assert plain.tendencies_by_player[a.id] == mixed.tendencies_by_player[b.id]


def test_real_team_rosters_put_each_franchise_on_its_own_players() -> None:
    teams = vs._build_teams(vs._load_teams_meta(), league_id=1)
    rows = vs.select_real_rosters(_SEASON, teams, league_seed=3, per_team=5, real_team_rosters=True)
    for index, team in enumerate(teams):
        code = seed_dev_league._REAL_DATA_TEAM_CODE.get(team.code, team.code)
        assert [r["team"]["abbreviation"] for r in rows[index]] == [code] * 5


def _totals(name: str, *, position: str, minutes: float, games: int, **counts: int):
    return vs.RealPlayerTotals(
        name=name,
        position=position,
        rim_protection=counts.pop("rim", 70),
        minutes=minutes,
        games=games,
        counts=counts,
    )


def test_fidelity_ranks_the_rotation_pool_and_grades_the_named_anchors() -> None:
    pooled = {
        "1": _totals(
            "Victor Wembanyama",
            position="PF",
            minutes=2400,
            games=82,
            rim=99,
            player_blocks=300,
            player_assists=250,
            player_rebounds=800,
        ),
        "2": _totals(
            "Nikola Jokic",
            position="C",
            minutes=2800,
            games=82,
            rim=80,
            player_blocks=60,
            player_assists=800,
            player_rebounds=1000,
        ),
        "3": _totals(
            "Bench Guy",
            position="PG",
            minutes=300,
            games=82,
            rim=40,
            player_blocks=40,
            player_assists=10,
            player_rebounds=20,
        ),
    }
    for i in range(4, 50):
        pooled[str(i)] = _totals(
            f"Filler {i}",
            position="SF",
            minutes=2000,
            games=82,
            rim=50 + i,
            player_blocks=i,
            player_assists=100 + i,
            player_rebounds=300 + i,
        )
    truth = {
        ref: {
            "blk36": t.counts["player_blocks"] * 36 / t.minutes,
            "reb36": t.counts["player_rebounds"] * 36 / t.minutes,
            "ast36": t.counts["player_assists"] * 36 / t.minutes,
            "stl36": 1.0,
            "tov36": 1.0,
            "pts36": 1.0,
        }
        for ref, t in pooled.items()
    }
    fidelity = vs.compute_real_fidelity(pooled, truth)
    rows = {r["name"]: r for r in fidelity["rows"]}
    assert fidelity["rotation_players"] == 48  # the 300-minute bench guy is out
    assert rows["blk_per36_spearman_vs_real"]["value"] == pytest.approx(1.0)
    assert rows["blk_per36_spearman_vs_real"]["passed"] is True
    assert rows["stl_per36_spearman_vs_real"]["passed"] is None  # informational only
    assert rows["Victor Wembanyama inside sim top 5 by bpg"]["value"] == 1
    assert rows["Nikola Jokic outside sim top 40 by bpg"]["passed"] is False  # 2nd by bpg
    assert rows["Nikola Jokic inside sim top 5 by apg"]["passed"] is True
    assert fidelity["legibility"]["blk"][0].startswith("Victor Wembanyama (PF)")
    assert fidelity["rotation_blk_per36_max_holder"].startswith("Victor Wembanyama")
    lines = vs._real_fidelity_lines({"season": _SEASON, **fidelity})
    assert any("| blk_per36_spearman_vs_real | 1.0000 | 0.70 | PASS |" == line for line in lines)
