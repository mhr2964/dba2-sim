"""RPD-8-2..9: the offline builder, one test per design decision, on small synthetic raw
fixtures (no seeds on disk, no network)."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from random import Random
from statistics import mean, pstdev
from types import SimpleNamespace

import pytest

from dba2.domain.player import Position
from dba2.domain.ratings import PlayerRatings, RatingSource
from dba2.sim.derivation import POSITION_RATING_OFFSETS, RATING_FIELD_ORDER, synthetic_ratings
from dba2.sim.ratings import compute_overall
from tools import _gm_real_player_build as build
from tools import _gm_real_player_etl as etl
from tools import _gm_real_player_fetch_raw as fetch_raw
from tools import _real_fields as rf
from tools import seed_dev_league
from tools._gm_real_player_report import spearman

_BDL_POSITIONS = ["G", "G-F", "F", "F-C", "C"]
_TEAMS = ["AAA", "BBB", "CCC", "BRK"]


def _entry(
    pid: int,
    *,
    minutes: float,
    games: int,
    quality: float = 0.0,
    position: str = "F",
    team: str = "AAA",
    rng: Random | None = None,
) -> dict:
    """One raw player entry whose per-36 production rises with `quality`."""
    rng = rng or Random(pid)
    pts36 = max(2.0, 15.0 + 5.0 * quality + rng.gauss(0, 1.5))
    factor = minutes / 36.0
    fga = pts36 * 0.85 * factor
    fg3a = fga * rng.uniform(0.05, 0.5)
    fta = pts36 * 0.25 * factor
    totals = {
        "pts": pts36 * factor,
        "fgm": fga * (0.42 + 0.02 * quality),
        "fga": fga,
        "fg3m": fg3a * rng.uniform(0.28, 0.42),
        "fg3a": fg3a,
        "ftm": fta * rng.uniform(0.6, 0.9),
        "fta": fta,
        "oreb": rng.uniform(0.3, 3.0) * factor,
        "dreb": rng.uniform(1.0, 7.0) * factor,
        "ast": max(0.2, 3.0 + quality + rng.gauss(0, 1.5)) * factor,
        "stl": rng.uniform(0.3, 1.8) * factor,
        "blk": rng.uniform(0.1, 1.8) * factor,
        "turnover": rng.uniform(1.0, 3.5) * factor,
        "pf": rng.uniform(1.5, 4.0) * factor,
    }
    scores = [round(6.0 + 4.0 * quality + rng.gauss(0, 4.0), 1) for _ in range(games)]
    return {
        "external_ref": str(pid),
        "first_name": "F",
        "last_name": f"L{pid}",
        "bdl_position": position,
        "height_in": 78,
        "weight_lb": 210,
        "draft_year": 2010 + pid % 7,
        "first_team": team,
        "first_game_date": "2015-10-30",
        "games_played": games,
        "minutes_total": round(minutes, 2),
        "totals": {k: round(v, 2) for k, v in totals.items()},
        "game_scores": scores,
    }


def _pool(season: int, n: int = 140, seed: int = 1, *, advanced: bool = False) -> dict:
    rng = Random(seed * 1000 + season)
    entries = []
    for pid in range(1, n + 1):
        quality = rng.gauss(0, 1)
        games = rng.randint(20, 82)
        mpg = min(38.0, max(9.0, 24.0 + 5.0 * quality + rng.gauss(0, 4.0)))
        entries.append(
            _entry(
                pid,
                minutes=games * mpg,
                games=games,
                quality=quality,
                position=_BDL_POSITIONS[pid % 5],
                team=_TEAMS[pid % 4],
                rng=rng,
            )
        )
    if advanced:
        for entry in entries:
            entry["advanced"] = {
                "rows": entry["games_played"],
                "sums": {
                    "matchup_fgm": 4.0 + (int(entry["external_ref"]) % 13),
                    "matchup_fga": 30.0,
                    "screen_assists": float(int(entry["external_ref"]) % 17),
                    "loose_balls_recovered_total": float(int(entry["external_ref"]) % 9),
                    "charges_drawn": float(int(entry["external_ref"]) % 4),
                },
                "counts": {},
            }
    return {
        "raw_format_version": 1,
        "season": season,
        "advanced_present": advanced,
        "players": entries,
    }


def _legacy_row(ref: str, season: int, *, minutes: float, adv: dict | None, team: str) -> dict:
    ratings = dict.fromkeys(RATING_FIELD_ORDER, 70)
    if adv is not None:
        ratings.update(adv)
    return {
        "external_ref": ref,
        "season": season,
        "ratings": ratings,
        "meta": {
            "minutes_total": minutes,
            "band": "advanced_tracking" if adv is not None else "box_score_only",
        },
        "team": {
            "id": 7,
            "abbreviation": team,
            "city": f"City{team}",
            "name": f"Name{team}",
            "full_name": f"City{team} Name{team}",
        },
    }


def _write_legacy(legacy_dir: Path, season: int, rows: list[dict]) -> None:
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / f"{season}.json").write_text(
        json.dumps({"season": season, "players": rows}), encoding="utf-8"
    )


def _team_rows(season: int) -> list[dict]:
    return [
        _legacy_row("0", season, minutes=0.0, adv=None, team=code) for code in (*_TEAMS[:3], "BKN")
    ]


def _dirs(tmp_path: Path, seasons: dict[int, dict], legacy: dict[int, list[dict]] | None = None):
    raw_dir, legacy_dir = tmp_path / "raw", tmp_path / "legacy"
    raw_dir.mkdir()
    for season, doc in seasons.items():
        (raw_dir / f"{season}.json").write_text(json.dumps(doc), encoding="utf-8")
    for season in seasons:
        rows = (legacy or {}).get(season, [])
        _write_legacy(legacy_dir, season, [*_team_rows(season), *rows])
    return raw_dir, legacy_dir


# ------------------------------------------------------------------------------ blend


def test_blend_golden_matches_the_etl_for_full_and_thin_seasons() -> None:
    """RPD-8-4: identical to today's ETL for 1500+ minute players, and for thin
    players with no priors (unfilled part shrinks toward 0 exactly as before)."""
    rows = []
    rng = Random(5)
    for pid in range(1, 61):
        games = rng.randint(3, 82)
        mpg = rng.uniform(6, 36)
        for game in range(games):
            rows.append(
                {
                    "min": f"{int(mpg)}:00",
                    "pts": rng.randint(0, 35),
                    "fgm": rng.randint(0, 12),
                    "fga": rng.randint(4, 20),
                    "fg3m": rng.randint(0, 4),
                    "fg3a": rng.randint(0, 8),
                    "ftm": rng.randint(0, 8),
                    "fta": rng.randint(0, 10),
                    "oreb": rng.randint(0, 5),
                    "dreb": rng.randint(0, 9),
                    "ast": rng.randint(0, 10),
                    "stl": rng.randint(0, 4),
                    "blk": rng.randint(0, 4),
                    "turnover": rng.randint(0, 6),
                    "pf": rng.randint(0, 6),
                    "player": {
                        "id": pid,
                        "first_name": "A",
                        "last_name": f"P{pid}",
                        "position": "G",
                        "height": "6-3",
                        "weight": "195",
                        "draft_year": 2012,
                    },
                    "team": {"abbreviation": "AAA"},
                    "game": {"date": f"2015-11-{1 + game % 28:02d}"},
                }
            )
    v1 = {p["external_ref"]: p for p in etl.build_season_seed(2015, rows)["players"]}
    raw = fetch_raw.aggregate_raw_season(2015, rows)
    evidence = build.season_evidence(2015, raw, build.LegacyIndex(Path("/nonexistent")))
    checked_full = checked_thin = 0
    for ref, player in evidence.players.items():
        used0, takes = build.blend_takes(player.minutes, [0.0, 0.0, 0.0])
        for name in rf.BOX_SCORE_REAL_FIELDS:
            z_eff = build.blend_z(player.z[name], used0, takes, [None, None, None])
            assert round(min(max(70 + 12 * z_eff, 25), 99)) == v1[ref]["ratings"][name]
        if player.minutes >= 1500:
            checked_full += 1
        else:
            checked_thin += 1
    assert checked_full >= 5 and checked_thin >= 5  # both regimes are actually exercised


def test_blend_takes_tatum_case_fills_from_the_prior_at_full_weight() -> None:
    used0, takes = build.blend_takes(522.0, [2623.0, 0.0, 0.0])
    assert used0 == 522.0
    assert takes == pytest.approx([978.0, 0.0, 0.0])  # 2623 * 0.7 = 1836 > 978, so it never binds


def test_blend_takes_decay_binds_when_the_prior_is_short_and_walks_back_most_recent_first() -> None:
    used0, takes = build.blend_takes(0.0, [1000.0, 1000.0, 1000.0])
    assert used0 == 0.0
    assert takes[0] == pytest.approx(700.0)  # 1000 * 0.7
    assert takes[1] == pytest.approx(490.0)  # 1000 * 0.7**2
    assert takes[2] == pytest.approx(1500.0 - 700.0 - 490.0)  # only the remaining gap
    assert sum(takes) == pytest.approx(1500.0)


def test_blend_takes_full_season_takes_nothing_and_lookback_is_three_seasons() -> None:
    assert build.blend_takes(1800.0, [2000.0, 2000.0, 2000.0]) == (1500.0, [0.0, 0.0, 0.0])
    _, takes = build.blend_takes(100.0, [0.0, 0.0, 0.0, 5000.0])  # a 4th prior is ignored
    assert len(takes) == 3


def test_blend_z_weights_by_minutes_and_treats_a_missing_prior_metric_as_unfilled() -> None:
    assert build.blend_z(1.0, 750.0, [750.0], [3.0]) == pytest.approx((750 + 2250) / 1500)
    assert build.blend_z(1.0, 750.0, [750.0], [None]) == pytest.approx(0.5)


# ------------------------------------------------------------------------------ ladder / map


def test_ladder_reproduces_the_tier_mixture_and_is_monotone() -> None:
    ladder = build.tier_ladder()
    assert len(ladder) == 420
    assert all(a >= b for a, b in zip(ladder, ladder[1:], strict=False))
    assert mean(ladder) == pytest.approx(69.714, abs=0.01)
    assert pstdev(ladder) == pytest.approx(7.39, abs=0.05)
    assert abs(mean(ladder[:30]) - 84.0) <= 3.0


def test_talent_map_is_monotone_through_the_defined_tail_and_hits_the_floor() -> None:
    n = 510
    values = [build.talent_for_rank(r, n) for r in range(1, n + 2)]
    assert all(a >= b for a, b in zip(values, values[1:], strict=False))
    assert values[419] == pytest.approx(build.tier_ladder()[-1])
    assert values[n - 1] == pytest.approx(40.0)  # the last qualified rank
    assert values[n] == 40.0  # past the pool
    assert build.talent_for_rank(1, n) <= 97.0


def test_talent_map_for_a_pool_smaller_than_the_ladder_uses_the_top_slots() -> None:
    assert build.talent_for_rank(1, 50) == build.talent_for_rank(1, 800)
    assert build.talent_for_rank(50, 50) == build.tier_ladder()[49]


# ------------------------------------------------------------------------------ regression


def test_ols_recovers_a_known_linear_relationship() -> None:
    xs = [[1.0, float(i), float(i % 3)] for i in range(30)]
    ys = [2.0 + 3.0 * row[1] - 1.5 * row[2] for row in xs]
    coefs = build.fit_ols(xs, ys)
    assert coefs == pytest.approx([2.0, 3.0, -1.5], abs=1e-6)


def test_residual_u_removes_impact_and_position_and_is_standardised() -> None:
    rng = Random(3)
    ids = [str(i) for i in range(400)]
    pos = {i: build.POSITIONS[int(i) % 5] for i in ids}
    impact = {i: rng.gauss(0, 1) for i in ids}
    shift = {p: 1.5 * k for k, p in enumerate(build.POSITIONS)}
    values = {i: 2.0 * impact[i] + shift[pos[i]] + rng.gauss(0, 0.7) for i in ids}
    u = build.residual_u(values, impact, pos, ids)
    assert pstdev(u.values()) == pytest.approx(1.0, abs=1e-6)
    assert mean(u.values()) == pytest.approx(0.0, abs=1e-6)
    for p in build.POSITIONS:  # no position residue
        assert mean(u[i] for i in ids if pos[i] == p) == pytest.approx(0.0, abs=1e-6)
    cov = mean(u[i] * impact[i] for i in ids) - mean(u.values()) * mean(impact.values())
    assert cov == pytest.approx(0.0, abs=1e-6)  # no impact residue


def test_residual_u_degenerate_inputs_do_not_crash() -> None:
    pos = {"1": Position.PG, "2": Position.PG}
    assert build.residual_u({"1": 1.0, "2": 2.0}, {"1": 0.0, "2": 1.0}, pos, ["1", "2"]) == {
        "1": 0.0,
        "2": 0.0,
    }
    ids = [str(i) for i in range(10)]
    same = dict.fromkeys(ids, 0.5)
    pos = dict.fromkeys(ids, Position.C)
    assert set(build.residual_u(same, {i: float(i) for i in ids}, pos, ids).values()) == {0.0}


# ------------------------------------------------------------------------------ season build


@pytest.fixture
def built(tmp_path: Path):
    seasons = {2014: _pool(2014, seed=2), 2015: _pool(2015, seed=3)}
    raw_dir, legacy_dir = _dirs(tmp_path, seasons)
    seeds, stats = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    return seeds[2015], stats[2015], raw_dir, legacy_dir


def _overall(row: dict) -> int:
    return compute_overall(SimpleNamespace(**row["ratings"]), Position(row["primary_position"]))


def test_fit_solver_lands_every_player_within_one_point_of_round_t(built) -> None:
    seed, stats, *_ = built
    misses = [r for r in seed["players"] if abs(_overall(r) - round(r["meta"]["talent"])) > 1]
    assert misses == []
    assert stats.fit_outside_tolerance == 0
    exact = sum(_overall(r) == round(r["meta"]["talent"]) for r in seed["players"])
    assert exact / len(seed["players"]) >= 0.95


def test_hidden_fields_sit_at_the_mapped_talent_like_the_legacy_synthetic(built) -> None:
    """Critic B2: same-noise `synthetic_ratings` at T is the reference. Its repair pass
    moves a field by a few points at most, whereas generating at T' would move the
    hidden pair by the whole T' - T (up to +-13 here). Mean gap stays within 1.0 of the
    legacy one, and the displayed overall is still round(T) for every row."""
    seed, *_ = built
    gaps, legacy_gaps, moved = [], [], []
    for row in seed["players"]:
        talent = row["meta"]["talent"]
        position = Position(row["primary_position"])
        assert _overall(row) == round(talent)
        legacy = synthetic_ratings(talent, position, Random(f"{row['external_ref']}:2015"))
        legacy_overall = compute_overall(SimpleNamespace(**legacy), position)
        gaps.append(row["ratings"]["potential"] - _overall(row))
        legacy_gaps.append(legacy["potential"] - legacy_overall)
        moved.append(abs(row["ratings"]["potential"] - legacy["potential"]))
    assert len(gaps) > 100
    assert abs(mean(gaps) - mean(legacy_gaps)) < 1.0
    assert max(moved) <= 6 and mean(moved) < 2.0
    # the correction really is large here, so the checks above can fail
    shifts = [abs(r["meta"]["synthetic_talent"] - r["meta"]["talent"]) for r in seed["players"]]
    assert max(shifts) > 8


def test_no_position_bias_in_overall_minus_talent(built) -> None:
    seed, *_ = built
    for position in build.POSITIONS:
        deltas = [
            _overall(r) - r["meta"]["talent"]
            for r in seed["players"]
            if r["primary_position"] == position.value
        ]
        assert len(deltas) > 10
        assert abs(mean(deltas)) <= 0.5


def test_real_fields_carry_the_position_offset(built) -> None:
    """Same-quality players at PG and C differ in a real field by about the offset
    difference, e.g. rim_protection is far higher for a C (RPD-8-6, critic B4)."""
    seed, *_ = built
    by_pos = {p: [] for p in build.POSITIONS}
    for row in seed["players"]:
        by_pos[Position(row["primary_position"])].append(row["ratings"]["rim_protection"])
    gap = mean(by_pos[Position.C]) - mean(by_pos[Position.PG])
    expected = (
        POSITION_RATING_OFFSETS[Position.C]["rim_protection"]
        - POSITION_RATING_OFFSETS[Position.PG]["rim_protection"]
    )
    # PG is now half of the 'G' group (M3), so the fixture's PG mean is noisier
    assert gap == pytest.approx(expected, abs=10.0)
    assert gap > 20


def test_talent_is_a_monotone_function_of_impact_among_the_qualified(built) -> None:
    seed, *_ = built
    qualified = sorted(
        (r for r in seed["players"] if r["meta"]["qualified"]),
        key=lambda r: (-r["meta"]["impact"], r["external_ref"]),
    )
    talents = [r["meta"]["talent"] for r in qualified]
    assert all(a >= b - 1e-9 for a, b in zip(talents, talents[1:], strict=False))
    assert [r["meta"]["impact_rank"] for r in qualified] == list(range(1, len(qualified) + 1))


def test_unqualified_players_are_placed_by_the_map_but_capped_at_the_bench_ceiling(
    tmp_path: Path,
) -> None:
    doc = _pool(2015, n=140, seed=4)
    # a 2-game, 80-minute hot streak: highest impact in the pool by construction
    doc["players"].append(_entry(999, minutes=80.0, games=2, quality=6.0, position="G"))
    doc["players"][-1]["game_scores"] = [60.0, 60.0]
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: doc})
    seeds, _ = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    row = next(r for r in seeds[2015]["players"] if r["external_ref"] == "999")
    assert row["meta"]["qualified"] is False
    assert row["meta"]["effective_minutes"] < 300
    assert row["meta"]["talent"] <= 66.0
    assert row["meta"]["impact_rank"] >= 1
    star = min(seeds[2015]["players"], key=lambda r: r["meta"]["impact_rank"])
    assert star["meta"]["talent"] > 80


def test_a_thin_injury_season_is_lifted_by_the_prior_season(tmp_path: Path) -> None:
    prior, now = _pool(2014, seed=6), _pool(2015, seed=7)
    star_prior = _entry(500, minutes=2600.0, games=70, quality=3.0, position="F")
    star_now = _entry(500, minutes=520.0, games=16, quality=3.0, position="F")
    prior["players"].append(star_prior)
    now["players"].append(star_now)
    raw_dir, legacy_dir = _dirs(tmp_path, {2014: prior, 2015: now})
    seeds, _ = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    row = next(r for r in seeds[2015]["players"] if r["external_ref"] == "500")
    meta = row["meta"]
    assert meta["blend_filled_minutes"] == pytest.approx(980.0, abs=0.1)
    assert meta["qualified"] and meta["impact_rank"] <= 15
    assert meta["minutes_per_game"] == pytest.approx(520.0 / 16, abs=0.01)
    assert row["confidence"] == pytest.approx(round(0.4 + 0.3 * 1.0, 2))  # used + filled = 1500


def test_missing_prior_seasons_are_reported(built) -> None:
    _, stats, *_ = built
    assert 2013 in stats.missing_priors and 2012 in stats.missing_priors
    assert 2014 not in stats.missing_priors


def test_meta_records_which_prior_raw_seasons_fed_the_blend(tmp_path: Path) -> None:
    """Critic minor 3: two rebuilds of 2015 off different raw sets must differ on disk."""
    seasons = {2013: _pool(2013, seed=1), 2014: _pool(2014, seed=2), 2015: _pool(2015, seed=3)}
    raw_dir, legacy_dir = _dirs(tmp_path, seasons)
    seeds, _ = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    assert {tuple(r["meta"]["prior_seasons"]) for r in seeds[2015]["players"]} == {(2014, 2013)}
    (raw_dir / "2013.json").unlink()
    fewer, _ = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    assert {tuple(r["meta"]["prior_seasons"]) for r in fewer[2015]["players"]} == {(2014,)}


def test_build_is_deterministic(tmp_path: Path) -> None:
    seasons = {2014: _pool(2014, seed=2), 2015: _pool(2015, seed=3)}
    raw_dir, legacy_dir = _dirs(tmp_path, seasons)
    first, _ = build.build_seasons([2014, 2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    second, _ = build.build_seasons([2015, 2014], raw_dir=raw_dir, legacy_dir=legacy_dir)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_tuning_weights_change_impact_ranking(tmp_path: Path) -> None:
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: _pool(2015, seed=3)})
    base, _ = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    minutes_only = build.Tuning(w_gmsc=0.0, w_ts=0.0, w_mpg=1.0)
    other, _ = build.build_seasons(
        [2015], raw_dir=raw_dir, legacy_dir=legacy_dir, tuning=minutes_only
    )
    assert [r["meta"]["impact_rank"] for r in base[2015]["players"]] != [
        r["meta"]["impact_rank"] for r in other[2015]["players"]
    ]


# ------------------------------------------------------------------------------ advanced


def test_advanced_z_is_recovered_from_a_legacy_row() -> None:
    weight = 0.5
    rating = 70 + 12 * 1.5 * weight
    index = build.LegacyIndex(Path("/nonexistent"))
    index._cache[2015] = {
        "1": _legacy_row(
            "1", 2015, minutes=750.0, adv=dict.fromkeys(rf.ADVANCED_REAL_FIELDS, rating), team="A"
        )
    }
    z = index.advanced_z(2015, "1")
    assert z == pytest.approx(dict.fromkeys(rf.ADVANCED_REAL_FIELDS, 1.5))
    assert index.advanced_z(2015, "2") is None


def _index_with(minutes: float, rating: int) -> build.LegacyIndex:
    index = build.LegacyIndex(Path("/nonexistent"))
    index._cache[2015] = {
        "1": _legacy_row(
            "1", 2015, minutes=minutes, adv=dict.fromkeys(rf.ADVANCED_REAL_FIELDS, rating), team="A"
        )
    }
    return index


def test_recovered_advanced_z_is_clamped_to_what_percentile_z_can_emit() -> None:
    """Critic M2: 99 at w=0.3 inverts to z=8.06; the ETL's own z tops out at 3.09."""
    limit = build.RECOVERED_Z_LIMIT
    assert limit == pytest.approx(3.0902, abs=1e-3)
    high = _index_with(450.0, 99).advanced_z(2015, "1")
    assert high == pytest.approx(dict.fromkeys(rf.ADVANCED_REAL_FIELDS, limit))
    low = _index_with(450.0, 25).advanced_z(2015, "1")
    assert low == pytest.approx(dict.fromkeys(rf.ADVANCED_REAL_FIELDS, -limit))
    inside = _index_with(1500.0, 76).advanced_z(2015, "1")  # w=1: (76-70)/12 = 0.5
    assert inside == pytest.approx(dict.fromkeys(rf.ADVANCED_REAL_FIELDS, 0.5))


def test_a_thin_legacy_season_is_treated_as_league_average_not_amplified_rounding() -> None:
    """w < 0.15 (under 225 minutes): a 28-minute player's rating 79 would invert to 41."""
    zero = dict.fromkeys(rf.ADVANCED_REAL_FIELDS, 0.0)
    assert _index_with(28.0, 79).advanced_z(2015, "1") == zero
    assert _index_with(224.9, 99).advanced_z(2015, "1") == zero
    at_edge = _index_with(225.0, 74).advanced_z(2015, "1")  # w = 0.15 exactly: recovered
    assert at_edge == pytest.approx(dict.fromkeys(rf.ADVANCED_REAL_FIELDS, 4 / (12 * 0.15)))


def test_a_version_two_row_is_never_inverted_but_serves_its_stored_z() -> None:
    index = build.LegacyIndex(Path("/nonexistent"))
    row = _legacy_row(
        "1", 2015, minutes=2000.0, adv=dict.fromkeys(rf.ADVANCED_REAL_FIELDS, 95), team="A"
    )
    row["meta"]["builder_version"] = 2
    index._cache[2015] = {"1": row}
    assert index.advanced_z(2015, "1") is None  # no stored z: not inverted from a v2 rating
    row["meta"]["advanced_z"] = {"on_ball_defense": 0.4, "screen_setting": 0.1, "motor": -0.2}
    assert index.advanced_z(2015, "1") == {
        "on_ball_defense": 0.4,
        "screen_setting": 0.1,
        "motor": -0.2,
    }


def test_advanced_fields_are_real_only_where_the_band_exists(tmp_path: Path) -> None:
    doc = _pool(2015, seed=3)
    legacy_rows = [
        _legacy_row(
            e["external_ref"],
            2015,
            minutes=e["minutes_total"],
            adv=dict.fromkeys(rf.ADVANCED_REAL_FIELDS, 75),
            team="AAA",
        )
        for e in doc["players"][:60]
    ]
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: doc}, {2015: legacy_rows})
    seeds, stats = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    assert stats[2015].advanced_recovered == 60
    bands = {r["external_ref"]: r["meta"]["band"] for r in seeds[2015]["players"]}
    assert sum(b == "advanced_tracking" for b in bands.values()) == 60
    with_adv = next(r for r in seeds[2015]["players"] if r["meta"]["band"] == "advanced_tracking")
    assert set(with_adv["meta"]["advanced_z"]) == set(rf.ADVANCED_REAL_FIELDS)
    assert with_adv["confidence"] >= 0.55
    without = next(r for r in seeds[2015]["players"] if r["meta"]["band"] == "box_score_only")
    assert "advanced_z" not in without["meta"] and without["confidence"] <= 0.7


def test_raw_advanced_sums_are_preferred_over_recovery(tmp_path: Path) -> None:
    doc = _pool(2015, seed=3, advanced=True)
    legacy_rows = [
        _legacy_row(
            e["external_ref"],
            2015,
            minutes=e["minutes_total"],
            adv=dict.fromkeys(rf.ADVANCED_REAL_FIELDS, 99),
            team="AAA",
        )
        for e in doc["players"]
    ]
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: doc}, {2015: legacy_rows})
    _, stats = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    assert stats[2015].advanced_raw == len(doc["players"])
    assert stats[2015].advanced_recovered == 0


# ------------------------------------------------------------------------------ distinct fields

MIN_PAIR_SD = 3.0  # rating points, position offsets removed


def _pairwise_offset_free_sds(rows: list[dict]) -> dict[tuple[str, str], float]:
    """SD over `rows` of (rating_a - offset_a) - (rating_b - offset_b) for every pair of
    real fields, using only rows where both fields are real (the advanced three are
    real only in the advanced band)."""
    out = {}
    for a, b in itertools.combinations(rf.REAL_FIELDS, 2):
        diffs = []
        for row in rows:
            real = (
                rf.REAL_FIELDS
                if row["meta"]["band"] == "advanced_tracking"
                else tuple(f for f in rf.REAL_FIELDS if f not in rf.ADVANCED_REAL_FIELDS)
            )
            if a in real and b in real:
                offsets = POSITION_RATING_OFFSETS[Position(row["primary_position"])]
                diffs.append((row["ratings"][a] - offsets[a]) - (row["ratings"][b] - offsets[b]))
        if len(diffs) > 30:
            out[(a, b)] = pstdev(diffs)
    return out


_REAL_2023_RAW = build.RAW_DIR / "2023.json"


@pytest.fixture(scope="module")
def real_2023() -> dict:
    seeds, _ = build.build_seasons([2023])
    return seeds[2023]


@pytest.mark.skipif(not _REAL_2023_RAW.exists(), reason="raw BDL 2023 evidence not on disk")
def test_no_two_real_fields_are_near_duplicates_on_a_real_season(real_2023: dict) -> None:
    """Critic B3, on real 2023 evidence (the committed raw file; the legacy seeds are only
    read for the advanced z and team objects): every pair of the 30 real fields differs
    by at least 3 rating points of SD once the position offsets are removed."""
    sds = _pairwise_offset_free_sds(real_2023["players"])
    assert len(sds) == 435
    worst = sorted((sd, pair) for pair, sd in sds.items())[:3]
    assert worst[0][0] >= MIN_PAIR_SD, worst


def test_the_distinctness_check_goes_red_when_two_fields_get_the_same_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation twin of the test above: hand finishing_rim's weights to catch_shoot_mid
    (RPD-8-5's original 2P%-only situation) and the pair's SD collapses to rounding noise."""
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: _pool(2015, seed=3, advanced=True)})
    legacy = {
        2015: [
            _legacy_row(
                str(pid),
                2015,
                minutes=1500.0,
                adv=dict.fromkeys(rf.ADVANCED_REAL_FIELDS, 70),
                team="AAA",
            )
            for pid in range(1, 141)
        ]
    }
    _write_legacy(legacy_dir, 2015, [*_team_rows(2015), *legacy[2015]])
    twin = {**rf.FIELD_METRIC_WEIGHTS, "catch_shoot_mid": rf.FIELD_METRIC_WEIGHTS["finishing_rim"]}
    monkeypatch.setattr(build, "FIELD_METRIC_WEIGHTS", twin)
    seeds, _ = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    sds = _pairwise_offset_free_sds(seeds[2015]["players"])
    assert sds[("finishing_rim", "catch_shoot_mid")] < 1.0
    assert min(sds.values()) < MIN_PAIR_SD


def test_height_is_an_unshrunk_evidence_metric() -> None:
    entries = _pool(2015, n=40, seed=3)["players"]
    metrics = build.raw_metrics(entries)
    assert all(metrics[e["external_ref"]]["height"] == e["height_in"] for e in entries)


# ------------------------------------------------------------------------------ position


def _facts(label: str, height: float, ast_share: float, reb36: float, qualified: bool = True):
    return build.PositionFacts(label, height, ast_share, reb36, qualified)


def _mixed_pool() -> dict[str, build.PositionFacts]:
    pool: dict[str, build.PositionFacts] = {}
    for i in range(10):  # ten 'G': assist share rises with i
        pool[f"g{i}"] = _facts("G", 74 + i % 3, 0.10 + 0.03 * i, 3.0)
    for i in range(10):  # ten 'F': size and rebounding rise with i
        pool[f"f{i}"] = _facts("F", 77 + i * 0.5, 0.12, 4.0 + 0.6 * i)
    for i in range(4):  # 'G-F' wings
        pool[f"gf{i}"] = _facts("G-F", 76 + 1.5 * i, 0.22 - 0.03 * i, 4.5)
    for i in range(4):  # 'F-G' wings
        pool[f"fg{i}"] = _facts("F-G", 76 + 1.5 * i, 0.22 - 0.03 * i, 4.5)
    pool["c"] = _facts("C", 84, 0.15, 11.0)
    pool["fc"] = _facts("F-C", 82, 0.15, 9.0)
    pool["cf"] = _facts("C-F", 82, 0.15, 9.0)
    return pool


def test_guards_split_into_pg_and_sg_by_assist_share() -> None:
    """Critic M3: BDL 'G' is no longer blindly PG."""
    out = build.resolve_positions(_mixed_pool())
    assert [out[f"g{i}"] for i in range(10)] == [Position.SG] * 5 + [Position.PG] * 5


def test_forwards_split_into_sf_and_pf_by_height_and_rebounding() -> None:
    out = build.resolve_positions(_mixed_pool())
    assert [out[f"f{i}"] for i in range(10)] == [Position.SF] * 5 + [Position.PF] * 5


def test_wings_split_by_height_and_assists_and_the_label_order_breaks_ties() -> None:
    out = build.resolve_positions(_mixed_pool())
    assert out["gf0"] == out["fg0"] == Position.SG  # short, high assists
    assert out["gf3"] == out["fg3"] == Position.SF  # tall, few assists
    # identical bodies, opposite labels: 'F-G' leans forward, 'G-F' leans guard
    twins = {
        "a": _facts("G-F", 78, 0.18, 5.0),
        "b": _facts("F-G", 78, 0.18, 5.0),
        "x": _facts("G-F", 75, 0.24, 4.0),
        "y": _facts("F-G", 81, 0.12, 6.0),
    }
    result = build.resolve_positions(twins)
    assert result["a"] == Position.SG and result["b"] == Position.SF


def test_bigs_and_unknown_labels_keep_the_etl_mapping_and_it_ignores_dict_order() -> None:
    pool = _mixed_pool()
    out = build.resolve_positions(pool)
    assert out["c"] == Position.C and out["fc"] == out["cf"] == Position.PF
    assert build.resolve_positions({"z": _facts("", 80, 0.1, 5.0), **pool})["z"] == Position.SF
    assert build.resolve_positions(dict(reversed(list(pool.items())))) == out


def test_a_pool_with_no_qualified_player_falls_back_to_the_etl_mapping() -> None:
    pool = {"g": _facts("G", 75, 0.3, 3.0, False), "f": _facts("F", 80, 0.1, 8.0, False)}
    assert build.resolve_positions(pool) == {"g": Position.PG, "f": Position.SF}


@pytest.mark.skipif(not _REAL_2023_RAW.exists(), reason="raw BDL 2023 evidence not on disk")
def test_real_2023_position_mix_is_balanced(real_2023: dict) -> None:
    """Critic M3 target: no position below ~10% or above ~30% of the qualified pool
    (was 242 PG / 207 SF / 52 C / 38 SG / 33 PF of 572). C is bounded by the 'C' label
    plus nothing else, so it sits just under 10%; the floor here is 8%."""
    qualified = [r for r in real_2023["players"] if r["meta"]["qualified"]]
    for position in build.POSITIONS:
        share = sum(r["primary_position"] == position.value for r in qualified) / len(qualified)
        assert 0.08 <= share <= 0.30, (position, share)


# ------------------------------------------------------------------------------ non-shooters


def _non_shooter_pool() -> tuple[dict, dict]:
    """A season pool plus a Ben-Simmons-2023-like guard: real minutes, real production,
    zero three-point attempts (his 2 threes' worth of fga moved inside the arc)."""
    doc = _pool(2015, n=160, seed=5)
    simmons = _entry(700, minutes=1800.0, games=60, quality=0.0, position="G", team="AAA")
    simmons["totals"]["fg3a"] = 0.0
    simmons["totals"]["fg3m"] = 0.0
    doc["players"].append(simmons)
    return doc, simmons


def test_a_zero_attempt_shooter_reads_below_average_on_the_three_point_fields(
    tmp_path: Path,
) -> None:
    """Critic minor 7: with a league-average 3P% prior and 0.3 on volume, a 0-3PA player
    read near average (Simmons 2023: catch_shoot_3 73 at T 68.4)."""
    doc, _ = _non_shooter_pool()
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: doc})
    seeds, _ = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    rows = seeds[2015]["players"]
    row = next(r for r in rows if r["external_ref"] == "700")
    offsets = POSITION_RATING_OFFSETS[Position(row["primary_position"])]
    qualified = [r for r in rows if r["meta"]["qualified"]]
    peers = [r for r in qualified if r["primary_position"] == row["primary_position"]]
    for name in ("catch_shoot_3", "off_dribble_3"):
        baseline = row["meta"]["talent"] + offsets[name]
        assert row["ratings"][name] <= baseline - 6, name
        assert row["ratings"][name] <= mean(r["ratings"][name] for r in peers) - 6, name
    # and the volume-led field puts him near the bottom of the pool, not the middle
    below = sum(r["ratings"]["catch_shoot_3"] < row["ratings"]["catch_shoot_3"] for r in qualified)
    assert below / len(qualified) < 0.25


def test_three_point_percentage_is_shrunk_toward_below_the_league_mean() -> None:
    entries = _pool(2015, n=60, seed=5)["players"]
    entries[0]["totals"]["fg3a"] = entries[0]["totals"]["fg3m"] = 0.0
    metrics = build.raw_metrics(entries)
    league_made = sum(e["totals"]["fg3m"] for e in entries)
    league_att = sum(e["totals"]["fg3a"] for e in entries)
    zero = metrics[entries[0]["external_ref"]]["three_pct"]
    assert zero == pytest.approx(league_made / league_att - build.THREE_PT_PRIOR_DEFICIT)
    # a heavy-volume shooter is barely moved by the deficit
    heavy = dict(entries[1])
    heavy["totals"] = {**heavy["totals"], "fg3a": 900.0, "fg3m": 324.0}
    heavy_pct = build.raw_metrics([heavy, *entries[2:]])[heavy["external_ref"]]["three_pct"]
    assert heavy_pct > 0.32


# ------------------------------------------------------------------------------ team


def test_team_comes_from_first_team_with_brk_mapped_to_bkn(tmp_path: Path) -> None:
    doc = _pool(2015, seed=3)
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: doc})
    seeds, _ = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    for row, entry in zip(
        sorted(seeds[2015]["players"], key=lambda r: r["external_ref"]),
        sorted(doc["players"], key=lambda e: e["external_ref"]),
        strict=True,
    ):
        expected = "BKN" if entry["first_team"] == "BRK" else entry["first_team"]
        assert row["team"]["abbreviation"] == expected
        assert row["team"]["city"] and row["team"]["name"]  # matchup_server reads both


@pytest.mark.parametrize("missing", [None, ""])
def test_a_row_with_no_first_team_is_a_hard_error_naming_player_and_season(
    tmp_path: Path, missing: str | None
) -> None:
    """Critic minor 6: the loaders index row['team'] unguarded, so a silently missing key
    would surface later as a KeyError with no player in it."""
    doc = _pool(2015, n=30, seed=3)
    doc["players"][4]["first_team"] = missing
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: doc})
    with pytest.raises(ValueError, match=r"L5 .*external_ref 5.*season 2015.*first_team"):
        build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)


def test_team_code_map_is_the_one_seed_dev_league_uses() -> None:
    assert build.REAL_DATA_TEAM_CODE == seed_dev_league._REAL_DATA_TEAM_CODE


def test_an_unknown_team_gets_a_bare_object_rather_than_no_team(tmp_path: Path) -> None:
    doc = _pool(2015, n=30, seed=3)
    doc["players"][0]["first_team"] = "ZZZ"
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: doc})
    seeds, _ = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    row = next(r for r in seeds[2015]["players"] if r["external_ref"] == "1")
    assert row["team"]["abbreviation"] == "ZZZ"


# ------------------------------------------------------------------------------ peak ages


def _mini_seed(season: int, ref: str, draft_year: int | None, ratings_level: int) -> dict:
    return {
        "season": season,
        "players": [
            {
                "external_ref": ref,
                "primary_position": "SF",
                "draft_year": draft_year,
                "ratings": dict.fromkeys(RATING_FIELD_ORDER, ratings_level),
            }
        ],
    }


def test_peak_window_is_the_near_peak_band_over_the_seasons_given() -> None:
    seeds = {
        2010: _mini_seed(2010, "1", 2008, 60),
        2011: _mini_seed(2011, "1", 2008, 70),
        2012: _mini_seed(2012, "1", 2008, 68),  # within 3 of the best (70): near peak
        2013: _mini_seed(2013, "1", 2008, 64),  # more than 3 below: out
    }
    assert build.apply_peak_ages(seeds) == 1
    ratings = seeds[2010]["players"][0]["ratings"]
    assert (ratings["peak_age_start"], ratings["peak_age_end"]) == (24, 25)  # ages 2011/2012


def test_a_single_season_gives_a_zero_width_window_and_undrafted_anchor_on_first_season() -> None:
    seeds = {2015: _mini_seed(2015, "9", None, 70)}
    build.apply_peak_ages(seeds)
    ratings = seeds[2015]["players"][0]["ratings"]
    assert ratings["peak_age_start"] == ratings["peak_age_end"] == 21


def test_only_the_seasons_handed_in_count_so_legacy_seasons_never_enter(tmp_path: Path) -> None:
    """A rebuilt season is judged on its own; nothing here reads other seed files."""
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: _pool(2015, seed=3)})
    seeds, _ = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    for row in seeds[2015]["players"]:
        assert row["ratings"]["peak_age_start"] == row["ratings"]["peak_age_end"]


def _peaks(seed: dict) -> dict[str, tuple[int, int]]:
    return {
        r["external_ref"]: (r["ratings"]["peak_age_start"], r["ratings"]["peak_age_end"])
        for r in seed["players"]
    }


def test_a_partial_rebuild_leaves_every_peak_age_as_the_all_build_had_it(tmp_path: Path) -> None:
    """RPD-8-9 / critic B1: a career-best season outside the requested range must still
    count, or a 2023-2025 rebuild silently moves 2025's career windows."""
    seasons = {s: _pool(s, n=100, seed=s) for s in (2013, 2014, 2015, 2016)}
    raw_dir, legacy_dir = _dirs(tmp_path, seasons)
    out = tmp_path / "out"
    everything, _ = build.build_seasons(list(seasons), raw_dir=raw_dir, legacy_dir=legacy_dir)
    build.write_seeds(everything, out)

    alone, _ = build.build_seasons([2015, 2016], raw_dir=raw_dir, legacy_dir=legacy_dir)
    assert _peaks(alone[2016]) != _peaks(everything[2016])  # the trap is real on this fixture

    partial, _ = build.build_seasons(
        [2015, 2016], raw_dir=raw_dir, legacy_dir=legacy_dir, peak_context_dir=out
    )
    for season in (2015, 2016):
        assert _peaks(partial[season]) == _peaks(everything[season])
    # the context seasons are read, never rewritten
    assert set(partial) == {2015, 2016}


def test_peak_context_ignores_legacy_and_mixed_files_and_the_season_being_rebuilt(
    tmp_path: Path,
) -> None:
    v2 = {"season": 2013, "players": [{"meta": {"builder_version": 2}}]}
    v1 = {"season": 2014, "players": [{"meta": {}}]}
    mixed = {"season": 2015, "players": [{"meta": {"builder_version": 2}}, {"meta": {}}]}
    rebuilt = {"season": 2016, "players": [{"meta": {"builder_version": 2}}]}
    for doc in (v2, v1, mixed, rebuilt):
        (tmp_path / f"{doc['season']}.json").write_text(json.dumps(doc), encoding="utf-8")
    assert set(build.load_version_two_context(tmp_path, exclude=[2016])) == {2013}
    assert build.load_version_two_context(tmp_path / "missing", exclude=[]) == {}


@pytest.mark.parametrize("draft_year", [1963, 1982, 2012, 1990])
def test_an_implausible_draft_year_anchors_on_the_earliest_season_like_an_undrafted_player(
    draft_year: int,
) -> None:
    """Critic minor 5: a 1963 draft year in a 2011 season made a 69/69 peak window.
    Draft years after the first season and more than 19 years before it are garbage."""
    seeds = {
        2011: _mini_seed(2011, "1", draft_year, 70),
        2012: _mini_seed(2012, "1", draft_year, 65),
    }
    build.apply_peak_ages(seeds)
    ratings = seeds[2011]["players"][0]["ratings"]
    assert (ratings["peak_age_start"], ratings["peak_age_end"]) == (21, 21)


def test_plausible_draft_years_still_anchor_the_age() -> None:
    for draft_year, age in ((2011, 21), (2010, 22), (1992, 40)):  # 1992: 19 years before
        seeds = {2011: _mini_seed(2011, "1", draft_year, 70)}
        build.apply_peak_ages(seeds)
        assert seeds[2011]["players"][0]["ratings"]["peak_age_start"] == age


def test_apply_peak_ages_reads_context_but_writes_only_the_requested_seasons() -> None:
    context = {2010: _mini_seed(2010, "1", 2008, 80), 2011: _mini_seed(2011, "1", 2008, 60)}
    seeds = {2012: _mini_seed(2012, "1", 2008, 62)}
    assert build.apply_peak_ages(seeds, context) == 1
    assert (seeds[2012]["players"][0]["ratings"]["peak_age_start"]) == 23  # the 2010 peak
    assert "peak_age_start" not in context[2010]["players"][0]["ratings"]


# ------------------------------------------------------------------------------ contracts


def test_seed_rows_carry_every_key_the_loaders_read(built) -> None:
    seed, *_ = built
    assert {"season", "players"} <= set(seed)
    rf.require_builder_version(seed["players"], 2015, allow_legacy=False)
    for row in seed["players"]:
        # seed_dev_league / box_score_real_matchup / matchup_server / _career_curves
        for key in (
            "external_ref",
            "season",
            "draft_year",
            "first_name",
            "last_name",
            "primary_position",
            "height_in",
            "weight_lb",
            "wingspan_in",  # RPC-10
            "confidence",
            "derived_from_season",
            "source",
        ):
            assert key in row, key
        assert row["team"]["abbreviation"]
        assert row["team"]["name"] and row["team"]["city"]
        assert isinstance(row["wingspan_in"], int)
        assert row["height_in"] - 1 <= row["wingspan_in"] <= row["height_in"] + 9
        assert set(row["ratings"]) == set(RATING_FIELD_ORDER) | {"peak_age_start", "peak_age_end"}
        assert all(isinstance(v, int) for v in row["ratings"].values())
        assert all(25 <= row["ratings"][f] <= 99 for f in RATING_FIELD_ORDER)
        meta = row["meta"]
        for key in (
            "minutes_total",
            "games_played",
            "band",
            "builder_version",
            "minutes_per_game",
            "impact_rank",
            "blend_filled_minutes",
        ):
            assert key in meta, key
        assert meta["builder_version"] == rf.REAL_BUILDER_VERSION
        assert row["derived_from_season"] == row["season"] == 2015
        # the row must actually hydrate a PlayerRatings, the way both loaders build one
        PlayerRatings(
            player_id=1,
            league_id=1,
            source=RatingSource.REAL_STAT_DERIVED,
            derived_from_season=row["derived_from_season"],
            confidence=Decimal(str(row["confidence"])),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            **row["ratings"],
        )
        assert Position(row["primary_position"])


def test_every_real_field_is_clamped_to_the_rating_range(built) -> None:
    seed, *_ = built
    assert all(
        25 <= row["ratings"][name] <= 99 for row in seed["players"] for name in rf.REAL_FIELDS
    )


# ------------------------------------------------------------------------------ cli


def test_cli_writes_a_scratch_dir_and_refuses_the_committed_seeds_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: _pool(2015, n=80, seed=3)})
    out = tmp_path / "out"
    code = build.main(
        [
            "build",
            "2015",
            "--raw-dir",
            str(raw_dir),
            "--legacy-dir",
            str(legacy_dir),
            "--out-dir",
            str(out),
        ]
    )
    assert code == 0
    assert json.loads((out / "2015.json").read_text(encoding="utf-8"))["season"] == 2015
    assert "season=2015" in capsys.readouterr().out
    code = build.main(
        ["build", "2015", "--raw-dir", str(raw_dir), "--out-dir", str(build.LEGACY_DIR)]
    )
    assert code == 1
    assert "refusing" in capsys.readouterr().err


def test_the_committed_seeds_dir_needs_allow_flag_and_all_together(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial build into seeds/real_players would leave its seasons inconsistent with
    the rest (critic B1); the guard fires before anything is written. The 'seeds dir' is
    a tmp stand-in so no committed file is ever at risk."""
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: _pool(2015, n=80, seed=3)})
    fake_seeds = tmp_path / "fake_seeds"
    monkeypatch.setattr(build, "LEGACY_DIR", fake_seeds)
    base = ["build", "--raw-dir", str(raw_dir), "--legacy-dir", str(legacy_dir)]
    base += ["--out-dir", str(fake_seeds)]
    assert build.main([*base, "2015", "--allow-seeds-dir"]) == 1
    assert "use --all" in capsys.readouterr().err
    assert not fake_seeds.exists()
    assert build.main([*base, "--all", "--allow-seeds-dir"]) == 0
    assert (fake_seeds / "2015.json").exists()


def test_default_out_dir_is_not_inside_the_committed_seeds() -> None:
    assert build.LEGACY_DIR.resolve() not in build.DEFAULT_OUT_DIR.resolve().parents
    assert build.DEFAULT_OUT_DIR.resolve() != build.LEGACY_DIR.resolve()


def test_a_season_with_no_raw_file_is_an_error(tmp_path: Path) -> None:
    raw_dir, legacy_dir = _dirs(tmp_path, {2015: _pool(2015, n=30, seed=3)})
    with pytest.raises(FileNotFoundError, match="2016"):
        build.build_seasons([2016], raw_dir=raw_dir, legacy_dir=legacy_dir)


def test_default_tuning_is_pinned_so_a_change_is_deliberate() -> None:
    assert build.DEFAULT_TUNING == build.Tuning(
        w_gmsc=0.45, w_ts=0.25, w_mpg=0.30, u_spread=4.75, swap_evidence_weight=0.70
    )
    assert build.SWAP_FIELDS == ("rim_protection",)
    assert build.DEFAULT_TUNING.w_gmsc + build.DEFAULT_TUNING.w_ts + build.DEFAULT_TUNING.w_mpg == (
        pytest.approx(1.0)
    )


# ------------------------------------------------------------------------------ RPC-4 swap


def test_the_swap_hands_the_highest_value_to_the_strongest_evidence() -> None:
    """RPC-4 (docs/design/real-player-rating-calibration.md): same multiset, evidence
    order; a tie in the key goes to the lower external_ref."""
    values = {"a": 99, "b": 85, "c": 70, "d": 70}
    evidence = {"a": -1.0, "b": 2.0, "c": 0.5, "d": 0.5}
    out = build.evidence_rank_swap(values, evidence, weight=1.0)
    assert out == {"b": 99, "c": 85, "d": 70, "a": 70}
    assert sorted(out.values()) == sorted(values.values())


def test_the_swap_at_weight_zero_is_the_level_formula_and_blends_in_between() -> None:
    values = {"a": 99, "b": 85, "c": 70, "d": 60}
    evidence = {"a": -1.0, "b": 2.0, "c": 0.5, "d": 1.5}
    assert build.evidence_rank_swap(values, evidence, weight=0.0) == values
    half = build.evidence_rank_swap(values, evidence, weight=0.5)
    assert half["a"] != 99 or half["b"] != 85  # something moved
    assert sorted(half.values()) == sorted(values.values())
    assert build.evidence_rank_swap({"a": 50}, {"a": 3.0}) == {"a": 50}


def test_swap_cells_stay_inside_their_position_and_skip_unqualified_rows() -> None:
    real = {
        "c1": {"rim_protection": 99, "hands": 60},
        "c2": {"rim_protection": 80, "hands": 61},
        "g1": {"rim_protection": 55, "hands": 70},
        "g2": {"rim_protection": 40, "hands": 71},
        "c3": {"rim_protection": 97, "hands": 62},  # unqualified
    }
    evidence = {"rim_protection": {"c1": -1.0, "c2": 1.0, "g1": -0.5, "g2": 3.0, "c3": 9.0}}
    position = {
        "c1": Position.C,
        "c2": Position.C,
        "c3": Position.C,
        "g1": Position.PG,
        "g2": Position.PG,
    }
    out = build.swap_cells(real, evidence, position, ["c1", "c2", "g1", "g2"], weight=1.0)
    assert (out["c1"]["rim_protection"], out["c2"]["rim_protection"]) == (80, 99)
    assert (out["g1"]["rim_protection"], out["g2"]["rim_protection"]) == (40, 55)
    assert out["c3"]["rim_protection"] == 97  # never pulled into the cell
    assert all(out[r]["hands"] == real[r]["hands"] for r in real)  # other fields untouched
    assert real["c1"]["rim_protection"] == 99  # the input is not mutated


def test_the_built_season_carries_the_swap_and_its_provenance(built) -> None:
    """Per (position) cell of qualified rows the rim_protection multiset is the level
    formula's, the order follows the evidence more closely than before, and every row
    records both the ranking key and the value it replaced (the report's T13)."""
    seed, *_ = built
    rows = [r for r in seed["players"] if r["meta"]["qualified"]]
    for row in seed["players"]:
        assert {"rim_protection_evidence_z", "rim_protection_level_value"} <= set(row["meta"])
        if not row["meta"]["qualified"]:
            assert row["ratings"]["rim_protection"] == row["meta"]["rim_protection_level_value"]
    for position in build.POSITIONS:
        cell = [r for r in rows if r["primary_position"] == position.value]
        if len(cell) < 5:
            continue
        final = [r["ratings"]["rim_protection"] for r in cell]
        level = [r["meta"]["rim_protection_level_value"] for r in cell]
        assert sorted(final) == sorted(level)
        evidence = [r["meta"]["rim_protection_evidence_z"] for r in cell]
        assert spearman(final, evidence) >= spearman(level, evidence)


# ------------------------------------------------------------------------------ RPC-10 wingspan


def test_wingspan_is_height_plus_the_synthetic_offset_moved_by_reach_and_clamped() -> None:
    assert build.wingspan_estimate(80, 0.0) == 84  # round(83.5) under banker's rounding
    assert build.wingspan_estimate(80, 1.0) == 85
    assert build.wingspan_estimate(80, 10.0) == 89  # height + 9
    assert build.wingspan_estimate(80, -10.0) == 79  # height - 1


def test_position_residual_centres_each_position_and_standardises() -> None:
    values = {"c1": 3.0, "c2": 1.0, "g1": -1.0, "g2": -3.0}
    position = {"c1": Position.C, "c2": Position.C, "g1": Position.PG, "g2": Position.PG}
    u = build.position_residual(values, position, list(values))
    assert u == {"c1": 1.0, "c2": -1.0, "g1": 1.0, "g2": -1.0}


def test_the_better_shot_blocker_is_the_longer_player_in_a_built_season(built) -> None:
    """RPC-10: within a position, wingspan offset follows the blocks-and-rebounds
    evidence, and the qualified pool stays centred on the synthetic +3.5."""
    seed, *_ = built
    rows = [r for r in seed["players"] if r["meta"]["qualified"]]
    offsets = [r["wingspan_in"] - r["height_in"] for r in rows]
    assert mean(offsets) == pytest.approx(build.WINGSPAN_OFFSET_MEAN, abs=0.3)
    evidence = [r["meta"]["rim_protection_evidence_z"] for r in rows]
    assert spearman(offsets, evidence) > 0.3


def test_seed_dev_league_reads_a_stored_wingspan_and_draws_only_without_one() -> None:
    row = {"height_in": 80, "wingspan_in": 88}
    assert (
        seed_dev_league.real_player_wingspan_in(row, rng_seed=1, team_index=0, slot_index=0) == 88
    )
    legacy = {"height_in": 80}
    drawn = {
        seed_dev_league.real_player_wingspan_in(legacy, rng_seed=1, team_index=t, slot_index=0)
        for t in range(30)
    }
    assert len(drawn) > 1 and all(79 <= w <= 89 for w in drawn)  # keyed by the slot
