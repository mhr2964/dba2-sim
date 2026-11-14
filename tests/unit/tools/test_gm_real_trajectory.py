"""RPD-8-7/8-8: the real trajectory takes an explicit builder version.

`None` must stay byte-for-byte the pre-versioning behaviour (every row of every
season file, the legacy 15 fields) until a caller opts in; a version reads only that
version's rows (legacy rows have no `meta.builder_version` and count as 1), and a
career with fewer than two seasons of it falls through (`None`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tools._gm_real_trajectory as traj
from tools import _real_fields as rf

_LEGACY_FIFTEEN = frozenset(
    {
        "durability",
        "ball_security",
        "draw_foul",
        "free_throw",
        "passing_vision",
        "passing_accuracy",
        "offensive_rebounding",
        "defensive_rebounding",
        "foul_discipline",
        "hands",
        "rim_protection",
        "consistency",
        "on_ball_defense",
        "screen_setting",
        "motor",
    }
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    traj._career_curves.cache_clear()
    yield
    traj._career_curves.cache_clear()


# ---------------------------------------------------------------- field accessor


def test_legacy_field_list_is_the_original_fifteen_names() -> None:
    assert traj._REAL_SOURCED_FIELDS == _LEGACY_FIFTEEN
    assert len(traj._REAL_SOURCED_FIELDS) == 15


def test_field_accessor_per_version() -> None:
    assert traj.real_sourced_fields(None) == _LEGACY_FIFTEEN
    assert traj.real_sourced_fields(1) == _LEGACY_FIFTEEN
    assert traj.real_sourced_fields(2) == rf.REAL_FIELD_SET
    assert len(traj.real_sourced_fields(2)) == 30
    assert traj.real_sourced_fields(1) < traj.real_sourced_fields(2)
    assert traj.real_sourced_fields(rf.REAL_BUILDER_VERSION) == rf.REAL_FIELD_SET


@pytest.mark.parametrize("bad", [0, 3, -1])
def test_field_accessor_rejects_an_undefined_version(bad: int) -> None:
    with pytest.raises(ValueError, match="builder_version"):
        traj.real_sourced_fields(bad)


def test_legacy_list_derives_from_the_shared_module() -> None:
    assert traj._REAL_SOURCED_FIELDS == frozenset(
        (*rf.BOX_SCORE_REAL_FIELDS, *rf.ADVANCED_REAL_FIELDS)
    )


@pytest.mark.parametrize("version", [None, 1, 2])
def test_stub_split_arithmetic_holds_for_every_version(version: int | None) -> None:
    from tools import _gm_progression_stub as stub

    real = traj.real_sourced_fields(version)
    synthetic = [f for f in stub._NUDGE_FIELDS if f not in real]
    assert len(synthetic) == 53 - len(real)
    # every real-sourced name is a nudge-eligible field, so the split is a partition
    assert real <= set(stub._NUDGE_FIELDS)


def test_stub_trajectory_is_flipped_to_version_two_with_the_thirty_field_split() -> None:
    """RPD-8 is wired: the stub reads only version-2 seasons and trajects the 30 real
    fields; the synthetic complement is the other 23 (one constant drives both)."""
    from tools import _gm_progression_stub as stub

    assert stub._REAL_TRAJECTORY_BUILDER_VERSION == 2
    assert stub._REAL_TRAJECTORY_BUILDER_VERSION == rf.REAL_BUILDER_VERSION
    assert set(stub._SYNTHETIC_FIELDS_FOR_REAL_PLAYERS) == (
        set(stub._NUDGE_FIELDS) - rf.REAL_FIELD_SET
    )
    assert len(stub._SYNTHETIC_FIELDS_FOR_REAL_PLAYERS) == 23
    assert not set(stub._SYNTHETIC_FIELDS_FOR_REAL_PLAYERS) & rf.REAL_FIELD_SET


def test_stub_trajectory_on_the_committed_seeds_is_version_two_only() -> None:
    """On the real files: a version-2 curve has the 30 fields and only ages from
    rebuilt seasons; a career that lived wholly in 1996-2008 has no curve."""
    from tools import _gm_progression_stub as stub

    version = stub._REAL_TRAJECTORY_BUILDER_VERSION
    curves = traj._career_curves(builder_version=version)
    assert curves
    ref, curve = next(iter(curves.items()))
    assert len(curve) >= 2
    assert set(curve[0][1]) == rf.REAL_FIELD_SET
    delta = traj.expected_delta_at_age(ref, curve[0][0], builder_version=version)
    assert delta is not None
    assert set(delta) == rf.REAL_FIELD_SET

    legacy_only = {
        p["external_ref"]
        for p in json.loads((traj._SEEDS_DIR / "2000.json").read_text("utf-8"))["players"]
    } - set(curves)
    assert legacy_only, "expected 2000 players with no version-2 career"
    ref = next(iter(sorted(legacy_only)))
    assert traj.expected_delta_at_age(ref, 25, builder_version=version) is None


# ---------------------------------------------------- None == legacy on real seeds


def _reference_curves() -> dict[str, tuple[tuple[int, dict[str, float]], ...]]:
    """The pre-versioning `_career_curves()`, restated: every row of every file, the
    legacy 15 fields, no version filter."""
    raw: dict[str, list] = {}
    for path in sorted(traj._SEEDS_DIR.glob("[12][0-9][0-9][0-9].json")):
        seed = json.loads(path.read_text(encoding="utf-8"))
        for player in seed["players"]:
            fields = {
                name: float(value)
                for name, value in player["ratings"].items()
                if name in _LEGACY_FIFTEEN
            }
            raw.setdefault(player["external_ref"], []).append(
                (player["season"], player["draft_year"], fields)
            )
    out = {}
    for ref, entries in raw.items():
        draft_year = entries[0][1]
        effective = draft_year if draft_year is not None else min(e[0] for e in entries)
        out[ref] = tuple(
            sorted(((s - effective + 21, f) for s, _, f in entries), key=lambda p: p[0])
        )
    return out


def test_none_is_byte_identical_to_the_legacy_all_files_behaviour_on_real_seeds() -> None:
    reference = _reference_curves()
    assert reference, "expected seeds/real_players to be present"
    assert traj._career_curves(builder_version=None) == reference
    # the zero-argument spelling every old caller used
    assert traj._career_curves() == reference

    for ref in list(reference)[::37]:
        curve = reference[ref]
        for age in range(curve[0][0] - 1, curve[-1][0] + 2):
            got = traj.expected_delta_at_age(ref, age)
            assert got == traj.expected_delta_at_age(ref, age, builder_version=None)
            if got is None:
                continue
            assert set(got) == set(curve[0][1])
            for name, value in got.items():
                points = tuple((a, f[name]) for a, f in curve)
                expected = traj._interp(points, age + 1) - traj._interp(points, age)
                assert value == expected  # exact: same arithmetic, no tolerance


# --------------------------------------------------- version filtering, tmp seeds


def _ratings(base: float, fields: frozenset[str] | set[str]) -> dict[str, int]:
    return {name: round(base) for name in fields} | {"unrelated_field": 1}


def _row(ref: str, season: int, draft_year: int | None, base: float, version: int | None):
    """A seed row: a version-2 row carries all 30 real fields plus meta.builder_version;
    a legacy row carries the 15 and no builder_version (`version=None`)."""
    fields = rf.REAL_FIELD_SET if version == 2 else _LEGACY_FIFTEEN
    row = {
        "external_ref": ref,
        "season": season,
        "draft_year": draft_year,
        "ratings": _ratings(base, fields),
    }
    row["meta"] = {"builder_version": version} if version is not None else {"band": "box"}
    return row


def _write_seasons(directory: Path, rows_by_season: dict[int, list[dict]]) -> None:
    for season, rows in rows_by_season.items():
        (directory / f"{season}.json").write_text(
            json.dumps({"season": season, "players": rows}), encoding="utf-8"
        )


@pytest.fixture
def mixed_seeds(tmp_path, monkeypatch):
    """Drafted 2010 (age 21 in 2010). "mixed": legacy 2010 (70) and 2011 (72), then
    version-2 2012 (85) and 2013 (88). "one_v2": two legacy seasons and ONE version-2.
    "legacy_only": no version-2 rows. "no_meta": a row with `meta` absent entirely."""
    no_meta = _row("no_meta", 2010, 2010, 60, None)
    del no_meta["meta"]
    no_meta_2 = _row("no_meta", 2011, 2010, 64, None)
    no_meta_2["meta"] = None
    _write_seasons(
        tmp_path,
        {
            2010: [
                _row("mixed", 2010, 2010, 70, None),
                _row("one_v2", 2010, 2010, 70, None),
                _row("legacy_only", 2010, 2010, 66, None),
                no_meta,
            ],
            2011: [
                _row("mixed", 2011, 2010, 72, None),
                _row("one_v2", 2011, 2010, 71, 1),  # an explicit version 1 is legacy too
                _row("legacy_only", 2011, 2010, 68, None),
                no_meta_2,
            ],
            2012: [
                _row("mixed", 2012, 2010, 85, 2),
                _row("one_v2", 2012, 2010, 84, 2),
            ],
            2013: [_row("mixed", 2013, 2010, 88, 2)],
        },
    )
    monkeypatch.setattr(traj, "_SEEDS_DIR", tmp_path)
    return tmp_path


def test_version_two_reads_only_version_two_rows_and_all_thirty_fields(mixed_seeds) -> None:
    curve = traj._career_curves(builder_version=2)["mixed"]
    assert [age for age, _ in curve] == [23, 24]  # 2012, 2013 only
    assert set(curve[0][1]) == rf.REAL_FIELD_SET
    assert all(v == 85.0 for v in curve[0][1].values())

    # inside the version-2 span: exact one-season delta, 30 fields, no legacy scale leakage
    delta = traj.expected_delta_at_age("mixed", 23, builder_version=2)
    assert delta is not None
    assert set(delta) == rf.REAL_FIELD_SET
    assert all(v == 3.0 for v in delta.values())

    # ages that only the legacy rows covered are outside the version-2 span
    assert traj.expected_delta_at_age("mixed", 21, builder_version=2) is None
    assert traj.expected_delta_at_age("mixed", 22, builder_version=2) is None
    assert traj.expected_delta_at_age("mixed", 24, builder_version=2) is None


def test_version_one_reads_only_legacy_rows_and_fifteen_fields(mixed_seeds) -> None:
    curve = traj._career_curves(builder_version=1)["mixed"]
    assert [age for age, _ in curve] == [21, 22]  # 2010, 2011 only
    assert set(curve[0][1]) == _LEGACY_FIFTEEN

    delta = traj.expected_delta_at_age("mixed", 21, builder_version=1)
    assert delta == dict.fromkeys(_LEGACY_FIFTEEN, 2.0)
    assert traj.expected_delta_at_age("mixed", 22, builder_version=1) is None


def test_none_still_interpolates_across_the_mixed_career(mixed_seeds) -> None:
    """The exact hazard the version exists for: with `None` the legacy-to-v2 jump
    (72 -> 85) reads as growth. Pinned so a `None` caller's behaviour cannot drift."""
    delta = traj.expected_delta_at_age("mixed", 22, builder_version=None)
    assert delta is not None
    assert set(delta) == _LEGACY_FIFTEEN  # v2 rows only contribute the legacy fifteen
    assert all(v == 13.0 for v in delta.values())  # 85 - 72
    assert traj.expected_delta_at_age("mixed", 22) == delta


def test_career_with_fewer_than_two_seasons_of_the_version_has_no_trajectory(mixed_seeds) -> None:
    assert "one_v2" not in traj._career_curves(builder_version=2)
    assert traj.expected_delta_at_age("one_v2", 21, builder_version=2) is None
    assert traj.expected_delta_at_age("one_v2", 22, builder_version=2) is None
    # ...while the legacy view of the same player still has a curve
    assert traj.expected_delta_at_age("one_v2", 21, builder_version=1) == dict.fromkeys(
        _LEGACY_FIFTEEN, 1.0
    )


def test_a_career_with_no_rows_of_the_version_falls_through(mixed_seeds) -> None:
    assert traj.expected_delta_at_age("legacy_only", 21, builder_version=2) is None
    assert traj.expected_delta_at_age("legacy_only", 21, builder_version=1) == dict.fromkeys(
        _LEGACY_FIFTEEN, 2.0
    )
    assert traj.expected_delta_at_age("nobody", 21, builder_version=2) is None


def test_rows_without_meta_count_as_version_one(mixed_seeds) -> None:
    assert traj._row_builder_version({"ratings": {}}) == 1
    assert traj._row_builder_version({"meta": None}) == 1
    assert traj._row_builder_version({"meta": {"builder_version": True}}) == 1
    assert traj._row_builder_version({"meta": {"builder_version": 2}}) == 2
    assert traj.expected_delta_at_age("no_meta", 21, builder_version=1) == dict.fromkeys(
        _LEGACY_FIFTEEN, 4.0
    )
    assert "no_meta" not in traj._career_curves(builder_version=2)


def test_each_version_is_cached_separately(mixed_seeds) -> None:
    v1 = traj._career_curves(builder_version=1)
    v2 = traj._career_curves(builder_version=2)
    none = traj._career_curves(builder_version=None)
    assert v1 is traj._career_curves(builder_version=1)
    assert v2 is traj._career_curves(builder_version=2)
    assert v1 is not v2
    assert none is not v1
    assert none is not v2
    assert set(v1["mixed"][0][1]) != set(v2["mixed"][0][1])


def test_an_unsupported_version_is_refused(mixed_seeds) -> None:
    with pytest.raises(ValueError, match="builder_version"):
        traj.expected_delta_at_age("mixed", 23, builder_version=3)
