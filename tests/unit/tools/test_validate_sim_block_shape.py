"""docs/design/block-attribution-v2.md BA-4.1 / Commit 0: the block-shape metrics
`tools/validate_sim.py` gained -- `blk_per36_top10_ratio`, `blk_per36_p95_rotation`,
`players_over_2_bpg` (reported only) and `guard_blk_per36` (a floor control). Built on
a hand-filled `SeasonAccumulator`, so every expected value is arithmetic, not a sim."""

from __future__ import annotations

import pytest

from tools import validate_sim as vs

_GAMES = 10


def _accumulator(players: list[tuple[int, float, int]]) -> vs.SeasonAccumulator:
    """`players`: (player id, minutes per game, season blocks), all on team 1."""
    acc = vs.SeasonAccumulator(
        games=_GAMES,
        total_seconds=_GAMES * 2880.0,
        team_games=2 * _GAMES,
        team_possessions_raw=2000,
        team_possessions_normalized_to_48=2000.0,
        fga_rim=10,
        fgm_rim=5,
        fga_mid=10,
        fgm_mid=4,
        fga_three=10,
        fgm_three=3,
        fta=10,
        ftm=8,
        oreb=1,
        dreb=3,
    )
    acc.games_played[1] = _GAMES
    for pid, mpg, blocks in players:
        acc.player_team[pid] = 1
        acc.player_minutes[pid] = mpg * _GAMES
        acc.player_blocks[pid] = blocks
    return acc


def _team():
    return vs._build_teams(vs._load_teams_meta(), league_id=1)[:1]


def test_top10_ratio_is_first_over_tenth_in_the_rotation_pool() -> None:
    # 36 mpg, so season blocks / 10 games == blk/36: 4.0, 3.6, ..., 0.4, then 0.2.
    players = [(i, 36.0, 40 - 4 * i) for i in range(10)] + [(10, 36.0, 2)]
    # A bench player (10 mpg) blocking at 9.0/36 must not enter the pool.
    players.append((11, 10.0, 25))
    metrics = vs.compute_metrics(_accumulator(players), _team())
    assert metrics["blk_per36_max"] == pytest.approx(4.0)
    assert metrics["blk_per36_top10_ratio"] == pytest.approx(4.0 / 0.4)


def test_top10_ratio_is_absent_below_ten_rotation_players() -> None:
    players = [(i, 36.0, 10 + i) for i in range(9)]
    metrics = vs.compute_metrics(_accumulator(players), _team())
    assert "blk_per36_top10_ratio" not in metrics


def test_p95_uses_the_modules_own_percentile_over_the_rotation_pool() -> None:
    players = [(i, 36.0, i) for i in range(21)]  # blk/36 = 0.0 .. 2.0
    metrics = vs.compute_metrics(_accumulator(players), _team())
    assert metrics["blk_per36_p95_rotation"] == pytest.approx(1.9)


def test_players_over_2_bpg_counts_every_player_not_only_the_rotation() -> None:
    players = [(1, 30.0, 20), (2, 30.0, 19), (3, 10.0, 25), (4, 30.0, 5)]
    metrics = vs.compute_metrics(_accumulator(players), _team())
    assert metrics["players_over_2_bpg"] == 2.0  # 2.0 exactly and 2.5; not 1.9


def test_guard_blk_per36_pools_pg_and_sg_rotation_players_only() -> None:
    players = [(1, 36.0, 2), (2, 36.0, 6), (3, 36.0, 30), (4, 10.0, 20)]
    positions = {1: "PG", 2: "SG", 3: "C", 4: "PG"}
    metrics = vs.compute_metrics(_accumulator(players), _team(), positions=positions)
    assert metrics["guard_blk_per36"] == pytest.approx((0.2 + 0.6) / 2)


def test_guard_blk_per36_is_not_invented_without_positions() -> None:
    metrics = vs.compute_metrics(_accumulator([(1, 36.0, 2)]), _team())
    assert "guard_blk_per36" not in metrics


def test_the_three_gated_shape_metrics_have_bands_and_the_reported_one_does_not() -> None:
    reference = vs.load_reference()
    assert (
        reference["blk_per36_top10_ratio"]["band_low"],
        reference["blk_per36_top10_ratio"]["band_high"],
    ) == (1.35, 1.80)
    assert (
        reference["blk_per36_p95_rotation"]["band_low"],
        reference["blk_per36_p95_rotation"]["band_high"],
    ) == (1.6, 2.4)
    assert reference["guard_blk_per36"]["band_low"] == 0.20
    assert "players_over_2_bpg" not in reference
    assert "players_over_2_bpg" in vs._REPORTED_ONLY_METRICS


def test_the_season_path_hands_compute_metrics_every_players_position(monkeypatch) -> None:
    """Without this the guard control silently SKIPs on every real run: the
    integration band test is `slow` and excluded from the default suite."""
    full_schedule = vs.build_schedule
    monkeypatch.setattr(vs, "build_schedule", lambda *a, **k: full_schedule(*a, **k)[:1])
    metrics, _league, games, _acc = vs.simulate_season_detailed(
        league_id=1, season=2026, rng_seed=5, players_per_team=14, tuning=vs.TuningProfile()
    )
    assert games == 1
    assert "guard_blk_per36" in metrics
