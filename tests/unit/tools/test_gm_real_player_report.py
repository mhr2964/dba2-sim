"""RPD-8 acceptance report: the ranking/tie semantics, the drag and clamp measurements,
the tuning/validation separation, and one end-to-end run on a builder output."""

from __future__ import annotations

import json
from pathlib import Path
from random import Random

import pytest

from dba2.sim.derivation import RATING_FIELD_ORDER
from tools import _gm_real_player_build as build
from tools import _gm_real_player_report as report
from tools import _real_fields as rf

# ------------------------------------------------------------------------------ ranking


def test_spearman_of_identical_reversed_and_tied_orderings() -> None:
    assert report.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert report.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert report.spearman([1, 1, 1], [1, 2, 3]) == 0.0  # no variation: defined as 0
    assert report.rank_average_ties([5, 5, 1]) == [2.5, 2.5, 1.0]


def test_placements_report_both_ends_of_a_tie() -> None:
    place = report.placements({"a": 90, "b": 85, "c": 85, "d": 85, "e": 70})
    assert (place["a"].rank_best, place["a"].rank_worst) == (1, 1)
    assert (place["b"].rank_best, place["b"].rank_worst) == (2, 4)
    assert place["e"].rank_best == place["e"].rank_worst == 5


def test_a_boundary_tie_is_marked_tie_dependent_not_silently_passed() -> None:
    place = report.placements({"a": 90, "b": 85, "c": 85, "d": 85})["c"]
    assert report.status_within(place, 4) == "PASS"
    assert report.status_within(place, 2) == "PASS (tie-dependent)"
    assert report.status_within(place, 1) == "FAIL"


def test_names_match_through_accents_and_ambiguity_is_an_error() -> None:
    seed = {
        "season": 2023,
        "players": [
            {"first_name": "Nikola", "last_name": "Jokić", "external_ref": "1"},
            {"first_name": "Twin", "last_name": "Name", "external_ref": "2"},
            {"first_name": "Twin", "last_name": "Name", "external_ref": "3"},
        ],
    }
    assert report.find_player(seed, "Nikola Jokic")["external_ref"] == "1"
    assert report.find_player(seed, "Nobody Here") is None
    with pytest.raises(ValueError, match="matches 2 rows"):
        report.find_player(seed, "Twin Name")


# ------------------------------------------------------------------------------ drag


def _row(ref: str, *, real: int, synth: int, t: float, t_prime: float, band: str = "box"):
    fields = set(rf.REAL_FIELDS)
    if band == "box":
        fields -= set(rf.ADVANCED_REAL_FIELDS)
    return {
        "external_ref": ref,
        "primary_position": "SF",
        "ratings": {f: (real if f in fields else synth) for f in RATING_FIELD_ORDER},
        "meta": {
            "qualified": True,
            "band": "box_score_only" if band == "box" else "advanced_tracking",
            "talent": t,
            "synthetic_talent": t_prime,
            "impact_rank": 1,
        },
    }


def test_drag_counts_players_whose_synthetic_mean_strays_from_the_real_mean() -> None:
    rows = [_row(str(i), real=70, synth=70, t=70, t_prime=70) for i in range(19)]
    rows.append(_row("far", real=90, synth=60, t=70, t_prime=52))  # gap of about 30
    stats = report.drag_stats({"players": rows})
    assert stats["n"] == 20
    assert stats["share_within"] == pytest.approx(19 / 20)
    assert stats["t_prime_minus_t_sd"] > 3.5
    assert stats["t_prime_minus_t_mean"] == pytest.approx(-18 / 20)


def test_real_fields_for_a_box_score_row_exclude_the_advanced_three() -> None:
    row = _row("1", real=80, synth=50, t=70, t_prime=70)
    for name in rf.ADVANCED_REAL_FIELDS:
        assert row["ratings"][name] == 50  # synthetic in this band
    assert set(report.real_field_names(row)) == set(rf.REAL_FIELDS) - set(rf.ADVANCED_REAL_FIELDS)
    advanced = _row("2", real=80, synth=50, t=70, t_prime=70, band="adv")
    assert set(report.real_field_names(advanced)) == set(rf.REAL_FIELDS)


# ------------------------------------------------------------------------------ clamp


def _career(ratings_by_season: dict[int, int], field: str = "rim_protection") -> dict[int, dict]:
    seeds = {}
    for season, rating in ratings_by_season.items():
        seeds[season] = {
            "season": season,
            "players": [
                {
                    "external_ref": "1",
                    "season": season,
                    "draft_year": 2010,
                    "ratings": {field: rating},
                    "meta": {"talent": 60.0},
                }
            ],
        }
    return seeds


def test_clamp_rate_counts_deltas_past_the_progression_cap() -> None:
    small = report.clamp_measurement(_career({2015: 60, 2016: 63}), ["rim_protection"])
    assert (small["calls"], small["clamp_rate"]) == (1, 0.0)
    big = report.clamp_measurement(_career({2015: 60, 2016: 72}), ["rim_protection"])
    assert big["clamp_rate"] == 1.0 and big["mean_abs"] == pytest.approx(12.0)


def test_clamp_measurement_interpolates_across_a_missing_season() -> None:
    # 60 -> 76 over two seasons is +8 per age step exactly: at the cap, not past it
    gap = report.clamp_measurement(_career({2015: 60, 2017: 76}), ["rim_protection"])
    assert gap["calls"] == 2 and gap["clamp_rate"] == 0.0
    assert gap["mean_abs"] == pytest.approx(8.0)


def test_clamp_measurement_can_strip_the_talent_component() -> None:
    seeds = _career({2015: 60, 2016: 72})
    seeds[2016]["players"][0]["meta"]["talent"] = 72.0  # the whole jump is talent
    u_only = report.clamp_measurement(
        seeds,
        ["rim_protection"],
        value_of=lambda row, f: row["ratings"][f] - row["meta"]["talent"],
    )
    # 60 - 60 = 0 in 2015 and 72 - 72 = 0 in 2016
    assert u_only["clamp_rate"] == 0.0 and u_only["mean_abs"] == 0.0


def test_a_one_season_career_makes_no_calls() -> None:
    assert report.clamp_measurement(_career({2015: 60}), ["rim_protection"])["calls"] == 0


def _clamp_career(band_by_season: dict[int, str]) -> dict[int, dict]:
    """One player whose motor (advanced-only) rating swings 30 points a season and whose
    rim_protection moves 1; the swing is only real in advanced-band seasons."""
    seeds = {}
    for i, (season, band) in enumerate(sorted(band_by_season.items())):
        seeds[season] = {
            "season": season,
            "players": [
                {
                    "external_ref": "1",
                    "season": season,
                    "draft_year": 2010,
                    "ratings": {"motor": 40 + 30 * (i % 2), "rim_protection": 60 + i},
                    "meta": {"talent": 60.0, "band": band},
                }
            ],
        }
    return seeds


def test_clamp_counts_the_advanced_three_only_where_they_are_measured() -> None:
    """Critic minor 2: motor is synthetic in box-score-only rows and must not add calls."""
    fields = ["motor", "rim_protection"]
    mixed = _clamp_career({2015: "box_score_only", 2016: "box_score_only", 2017: "box_score_only"})
    assert report.clamp_measurement(mixed, fields)["calls"] == 2  # rim_protection only
    measured = _clamp_career({s: "advanced_tracking" for s in (2015, 2016, 2017)})
    result = report.clamp_measurement(measured, fields)
    assert result["calls"] == 4 and result["clamp_rate"] == 0.5  # both motor deltas are 30


def test_a_row_without_a_band_counts_every_field_it_carries() -> None:
    seeds = _career({2015: 60, 2016: 72}, field="motor")  # the older fixtures have no band
    assert report.clamp_measurement(seeds, ["motor"])["calls"] == 1


def test_a_solver_miss_fails_the_report_and_the_exit_code(
    out_dir: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Critic minor 1: the fit-tolerance line could only be PASS or INFO, so a broken
    solver was a green run."""
    out, legacy_dir = out_dir
    seeds = report.load_seasons(out)
    row = seeds[2015]["players"][0]
    row["meta"]["talent"] = round(row["meta"]["talent"]) + 7.0  # overall now 7 off round(T)
    by_name = {c.name: c for c in report.check_solver(seeds)}
    check = by_name["solver 2015 fit tolerance"]
    assert check.status == "FAIL" and "1 of" in check.detail
    (out / "2015.json").write_text(json.dumps(seeds[2015]), encoding="utf-8")
    assert report.main(["report", str(out), "--legacy-dir", str(legacy_dir)]) == 1
    assert "[FAIL] solver 2015 fit tolerance" in capsys.readouterr().out


def test_extreme_pegging_is_reported_but_never_fails(out_dir: tuple[Path, Path]) -> None:
    seeds = report.load_seasons(out_dir[0])
    row = seeds[2015]["players"][0]
    real = set(report.real_field_names(row))
    for name in RATING_FIELD_ORDER:
        if name not in real:
            row["ratings"][name] = 99
    check = report.check_extreme_pegging(seeds)
    assert check.status == "INFO"
    assert "1 of " in check.detail and "2015: 1" in check.detail
    assert "worst row 100%" in check.detail


def _band_row(ref: int, talent: float, *, delta: float, position: str = "SF") -> dict:
    offsets = report.POSITION_RATING_OFFSETS[report.Position(position)]
    return {
        "external_ref": str(ref),
        "primary_position": position,
        "ratings": {f: round(talent + offsets[f] + delta) for f in RATING_FIELD_ORDER},
        "meta": {"qualified": True, "band": "advanced_tracking", "talent": talent},
    }


def test_evidence_share_is_the_evidence_term_over_the_offset_free_rating_variance() -> None:
    # T alternates 74/78 (variance 4); the evidence term alternates -3/+3 (variance 9)
    rows = [
        _band_row(i, 74.0 + 4.0 * (i % 2), delta=3.0 * (1 if (i // 2) % 2 else -1))
        for i in range(120)
    ]
    shares = report.evidence_share({2015: {"players": rows}})
    assert set(shares) == set(rf.REAL_FIELDS)
    assert all(share == pytest.approx(9 / 13, abs=0.03) for share in shares.values())
    outside = [_band_row(i, 60.0, delta=3.0) for i in range(60)]
    assert report.evidence_share({2015: {"players": outside}}) == {}  # nobody in the band
    # ...but the whole-pool form has no band, and a wide talent spread dilutes the evidence
    spread = [_band_row(i, 50.0 + i * 0.5, delta=3.0 * (1 if i % 2 else -1)) for i in range(60)]
    pool = report.evidence_share({2015: {"players": spread}}, band=None)
    assert all(0.0 < share < 0.2 for share in pool.values())
    check = report.check_evidence_share({2015: {"players": rows}})
    assert check.status == "INFO" and "69." in check.detail


# ------------------------------------------------------------------------------ sets


def test_tuning_and_validation_sets_never_share_a_player() -> None:
    tuning_names = {name for name, _ in report.TUNING_CASES}
    validation_names = {"Jayson Tatum", "Jaylen Brown", "Ja Morant", "Anthony Davis", "Joel Embiid"}
    validation_names |= set(report.STARS_TOP15)
    assert not tuning_names & validation_names
    assert {season for _, season in report.TUNING_CASES} <= set(range(2014, 2022))


def _raw_season() -> dict:
    """A small raw season for the builder (synthetic, no seeds on disk)."""
    rng = Random(11)
    players = []
    for pid in range(1, 120):
        quality = rng.gauss(0, 1)
        games = rng.randint(30, 82)
        minutes = games * min(38.0, max(10.0, 24.0 + 5.0 * quality + rng.gauss(0, 3.0)))
        factor = minutes / 36.0
        pts36 = max(4.0, 15.0 + 5.0 * quality + rng.gauss(0, 1.5))
        fga = pts36 * 0.85 * factor
        players.append(
            {
                "external_ref": str(pid),
                "first_name": "F",
                "last_name": f"L{pid}",
                "bdl_position": ("G", "G-F", "F", "F-C", "C")[pid % 5],
                "height_in": 78,
                "weight_lb": 200,
                "draft_year": 2012,
                "first_team": "AAA",
                "first_game_date": "2015-10-30",
                "games_played": games,
                "minutes_total": round(minutes, 2),
                "totals": {
                    "pts": round(pts36 * factor, 1),
                    "fgm": round(fga * 0.44, 1),
                    "fga": round(fga, 1),
                    "fg3m": round(fga * 0.12, 1),
                    "fg3a": round(fga * 0.3, 1),
                    "ftm": round(pts36 * 0.2 * factor, 1),
                    "fta": round(pts36 * 0.25 * factor, 1),
                    "oreb": round(rng.uniform(0.3, 3) * factor, 1),
                    "dreb": round(rng.uniform(1, 7) * factor, 1),
                    "ast": round(max(0.2, 3 + quality + rng.gauss(0, 1)) * factor, 1),
                    "stl": round(rng.uniform(0.3, 1.8) * factor, 1),
                    "blk": round(rng.uniform(0.1, 1.8) * factor, 1),
                    "turnover": round(rng.uniform(1, 3.5) * factor, 1),
                    "pf": round(rng.uniform(1.5, 4) * factor, 1),
                },
                "game_scores": [round(6 + 4 * quality + rng.gauss(0, 4), 1) for _ in range(games)],
            }
        )
    return {"raw_format_version": 1, "season": 2015, "advanced_present": False, "players": players}


@pytest.fixture
def out_dir(tmp_path: Path) -> tuple[Path, Path]:
    raw_dir, legacy_dir, out = tmp_path / "raw", tmp_path / "legacy", tmp_path / "out"
    raw_dir.mkdir()
    legacy_dir.mkdir()
    (raw_dir / "2015.json").write_text(json.dumps(_raw_season()), encoding="utf-8")
    (legacy_dir / "2015.json").write_text(json.dumps({"players": []}), encoding="utf-8")
    seeds, _ = build.build_seasons([2015], raw_dir=raw_dir, legacy_dir=legacy_dir)
    build.write_seeds(seeds, out)
    return out, legacy_dir


def test_validation_lines_appear_only_when_asked_for(out_dir: tuple[Path, Path]) -> None:
    seeds = report.load_seasons(out_dir[0])
    without = report.build_report(seeds, {}, validation=False)
    assert not any(c.name.startswith("validation") for c in without)
    with_validation = report.build_report(seeds, {}, validation=True)
    statuses = {c.status for c in with_validation if c.name.startswith("validation")}
    assert statuses == {"SKIPPED"}  # the fixture has none of those player-seasons


def test_end_to_end_on_a_builder_output(
    out_dir: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    out, legacy_dir = out_dir
    checks = report.build_report(report.load_seasons(out), {}, validation=False)
    by_name = {c.name: c for c in checks}
    assert by_name["input"].status == "PASS"
    assert by_name["solver 2015 position bias"].status == "PASS"
    assert by_name["solver 2015 Spearman(I, overall)"].status == "PASS"
    assert by_name["solver 2015 fit tolerance"].status == "PASS"
    assert any(c.name.startswith("baseline synthetic league") for c in checks)
    assert any(c.name.startswith("RPD-6 clamp rate") for c in checks)

    code = report.main(["report", str(out), "--legacy-dir", str(legacy_dir)])
    printed = capsys.readouterr().out
    assert "[PASS] input" in printed
    assert code == (1 if any(c.status == "FAIL" for c in checks) else 0)


def test_a_legacy_input_is_rejected_by_the_input_check_alone() -> None:
    seeds = {2015: {"season": 2015, "players": [{"meta": {}, "ratings": {}}]}}
    checks = report.build_report(seeds, {}, validation=True)
    assert [c.name for c in checks] == ["input"] and checks[0].status == "FAIL"


def test_the_report_never_reads_the_environment() -> None:
    source = Path(report.__file__).read_text(encoding="utf-8")
    assert "environ" not in source and "getenv" not in source


def test_field_shape_skips_fields_that_are_synthetic_for_every_row() -> None:
    rows = [_row(str(i), real=60 + i, synth=50, t=70, t_prime=70) for i in range(6)]
    shape = report.field_shape(rows)
    assert set(rf.ADVANCED_REAL_FIELDS).isdisjoint(shape["pooled_sd"])  # box-score band only
    assert "rim_protection" in shape["pooled_sd"]
    assert min(shape["pooled_sd"].values()) > 0.0


# ------------------------------------------------------------------------------ RPC-12 Tier 1


def test_raw_truth_is_the_raw_season_per_36_and_percentages(out_dir: tuple[Path, Path]) -> None:
    """docs/design/real-player-rating-calibration.md RPC-12: ground truth comes from the raw
    totals on disk, one row per external_ref."""
    raw_dir = out_dir[0].parent / "raw"
    truth = report.load_raw_truth(raw_dir, [2015, 2016])
    assert set(truth) == {2015}  # no 2016 raw file: absent, not an error
    entry = _raw_season()["players"][0]
    row = truth[2015][entry["external_ref"]]
    totals = entry["totals"]
    assert row["blk36"] == pytest.approx(totals["blk"] * 36.0 / entry["minutes_total"])
    assert row["ft_pct"] == pytest.approx(totals["ftm"] / totals["fta"])


def test_t1_passes_when_the_field_orders_like_blocks_and_fails_when_it_does_not(
    out_dir: tuple[Path, Path],
) -> None:
    out = out_dir[0]
    seeds = report.load_seasons(out)
    truth = report.load_raw_truth(out.parent / "raw", [2015])
    for row in seeds[2015]["players"]:  # a perfectly block-ordered field
        row["ratings"]["rim_protection"] = min(
            99, 25 + round(truth[2015][row["external_ref"]]["blk36"] * 30)
        )
    assert report.check_t1(seeds, truth).status == "PASS"
    for row in seeds[2015]["players"]:  # ...and one ordered against blocks
        row["ratings"]["rim_protection"] = max(
            25, 99 - round(truth[2015][row["external_ref"]]["blk36"] * 30)
        )
    assert report.check_t1(seeds, truth).status == "FAIL"
    assert report.check_t1(seeds, {}).status == "SKIPPED"


def test_the_bars_that_must_not_move_fail_when_the_field_leaks_outside_its_cell(
    out_dir: tuple[Path, Path],
) -> None:
    """T6/T10 compare against the baseline seeds: a permutation within a (season,
    position) cell leaves both unchanged; raising one value does not."""
    import copy

    base = report.load_seasons(out_dir[0])
    same = copy.deepcopy(base)
    rows = [r for r in report.qualified_rows(same[2015]) if r["primary_position"] == "C"]
    rows[0]["ratings"]["rim_protection"], rows[1]["ratings"]["rim_protection"] = (
        rows[1]["ratings"]["rim_protection"],
        rows[0]["ratings"]["rim_protection"],
    )
    assert report.check_t10(same, base).status == "PASS"
    assert report.check_t6(same, base).status == "PASS"
    rows[0]["ratings"]["rim_protection"] = 99 if rows[0]["ratings"]["rim_protection"] < 99 else 60
    assert report.check_t10(same, base).status == "FAIL"
    assert report.check_t6(same, base).status == "FAIL"


def test_percentile_is_linear_interpolation_and_drag_p95_uses_the_group_mean_gap() -> None:
    assert report.percentile_of([0.0, 10.0], 95.0) == pytest.approx(9.5)
    assert report.percentile_of([1.0, 2.0, 3.0, 4.0, 5.0], 50.0) == 3.0
    rows = [_row(str(i), real=70, synth=70 + (i % 10), t=70, t_prime=70) for i in range(20)]
    gaps = report.drag_gaps({"players": rows})
    assert sorted(gaps)[-1] == pytest.approx(9.0)  # |mean(synth) - mean(real)|, per row


def test_permutation_report_needs_swap_provenance_and_catches_a_leaked_value() -> None:
    def row(ref: int, level: int, final: int, z: float) -> dict:
        return {
            "external_ref": str(ref),
            "first_name": "F",
            "last_name": f"L{ref}",
            "primary_position": "C",
            "ratings": {"rim_protection": final},
            "meta": {
                "qualified": True,
                "rim_protection_level_value": level,
                "rim_protection_evidence_z": z,
            },
        }

    swapped = {2015: {"players": [row(1, 99, 80, -1.0), row(2, 80, 99, 2.0), row(3, 90, 90, 0.5)]}}
    [cell] = report.permutation_cells(swapped)
    assert cell["multiset_kept"] and cell["rho_new"] == pytest.approx(1.0)
    assert cell["rho_old"] < cell["rho_new"]
    assert cell["up"] == (19, "F L2") and cell["down"] == (-19, "F L1")
    assert report.check_t13(swapped)[0].status == "PASS"
    swapped[2015]["players"][2]["ratings"]["rim_protection"] = 91
    assert report.check_t13(swapped)[0].status == "FAIL"
    no_provenance = {2015: {"players": [{**row(1, 1, 1, 0.0), "meta": {"qualified": True}}]}}
    assert report.check_t13(no_provenance)[0].status == "SKIPPED"


def test_calibration_mode_reads_only_version_two_seasons_and_leaves_the_rpd8_report_alone(
    out_dir: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    out, legacy_dir = out_dir
    (out / "2008.json").write_text(
        json.dumps({"season": 2008, "players": [{"meta": {}, "ratings": {}}]}), encoding="utf-8"
    )
    assert set(report.version_two_only(report.load_seasons(out))) == {2015}
    report.main(["report", str(out), "--calibration", "--raw-dir", str(out.parent / "raw")])
    printed = capsys.readouterr().out
    assert "T1 spearman(rim_protection, real blk/36)" in printed
    assert "T5" not in printed  # named players only with --validation
    assert "solver 2015" not in printed
