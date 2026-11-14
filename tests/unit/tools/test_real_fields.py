"""RPD-8-5/7/8: the shared real-field module and its version refusal."""

from __future__ import annotations

import pytest

from dba2.sim.derivation import RATING_FIELD_ORDER
from tools import _real_fields as rf


def test_builder_version_is_two() -> None:
    assert rf.REAL_BUILDER_VERSION == 2


def test_real_fields_are_real_rating_names_and_none_is_hidden() -> None:
    assert set(rf.REAL_FIELDS) <= set(RATING_FIELD_ORDER)
    assert len(rf.REAL_FIELDS) == len(set(rf.REAL_FIELDS))
    assert not set(rf.REAL_FIELDS) & {"potential", "potential_variance", "peak_age_start"}


def test_existing_fifteen_are_a_subset_of_the_real_fields() -> None:
    assert set(rf.BOX_SCORE_REAL_FIELDS) | set(rf.ADVANCED_REAL_FIELDS) <= set(rf.REAL_FIELDS)
    assert len(rf.BOX_SCORE_REAL_FIELDS) == 12
    assert len(rf.ADVANCED_REAL_FIELDS) == 3


def test_progression_stub_split_arithmetic_still_holds() -> None:
    # `_gm_progression_stub` asserts `53 - len(_REAL_SOURCED_FIELDS)` synthetic
    # nudge fields; with this list that must still be a valid, positive count.
    assert 0 < 53 - len(rf.REAL_FIELDS) < 53


def test_every_weight_row_sums_to_one_and_only_uses_sanctioned_metrics() -> None:
    assert set(rf.FIELD_METRIC_WEIGHTS) == set(rf.REAL_FIELDS)
    for field_name, weights in rf.FIELD_METRIC_WEIGHTS.items():
        assert sum(weights.values()) == pytest.approx(1.0)
        for metric in weights:
            assert field_name in rf.METRIC_FEEDS[metric]


def test_feeds_and_weights_agree_in_both_directions() -> None:
    """A metric may feed a field only through a weight, and every weight is listed."""
    used = {(m, f) for f, w in rf.FIELD_METRIC_WEIGHTS.items() for m in w}
    listed = {(m, f) for m, fed in rf.METRIC_FEEDS.items() for f in fed}
    assert used == listed
    assert set(rf.METRIC_FEEDS) == set(rf.EVIDENCE_METRICS)


def test_no_two_real_fields_share_a_weight_vector_or_a_scaled_copy_of_one() -> None:
    """Critic B3: RPD-8-5's table made nine new fields one metric plus a constant."""
    vectors = {
        name: tuple(sorted((m, round(w / max(weights.values()), 6)) for m, w in weights.items()))
        for name, weights in rf.FIELD_METRIC_WEIGHTS.items()
    }
    seen: dict[tuple, str] = {}
    for name, vector in vectors.items():
        assert vector not in seen, f"{name} and {seen[vector]} have the same evidence blend"
        seen[vector] = name


def test_every_new_field_mixes_at_least_three_metrics_and_none_dominates() -> None:
    """The duplicated nine were all single- or two-metric copies of another field."""
    for name in rf.SCORING_REAL_FIELDS:
        weights = rf.FIELD_METRIC_WEIGHTS[name]
        assert len(weights) >= 3, name
        assert max(weights.values()) <= 0.6 + 1e-9, name  # no field is one metric in disguise


def test_height_is_a_physical_not_a_blended_performance_metric() -> None:
    assert "height" in rf.PHYSICAL_METRICS
    assert "height" not in rf.BLENDED_METRICS
    assert "height" in rf.EVIDENCE_METRICS


def _row(version: int | None) -> dict:
    meta = {} if version is None else {"builder_version": version}
    return {"external_ref": "1", "meta": meta}


def test_refuses_a_legacy_season_naming_it_and_the_fix() -> None:
    with pytest.raises(rf.LegacyRatingsError) as exc:
        rf.require_builder_version([_row(None)], 2025, allow_legacy=False)
    message = str(exc.value)
    assert "season 2025" in message
    assert "_gm_real_player_fetch_raw 2025 2025" in message
    assert "--allow-legacy-ratings" in message


def test_refuses_a_mixed_season_even_if_most_rows_are_current() -> None:
    rows = [_row(2), _row(2), _row(1)]
    with pytest.raises(rf.LegacyRatingsError, match="1 of 3"):
        rf.require_builder_version(rows, 2020, allow_legacy=False)


def test_accepts_current_rows_and_the_legacy_flag_and_empty_seasons() -> None:
    rf.require_builder_version([_row(2), _row(2)], 2020, allow_legacy=False)
    rf.require_builder_version([_row(None), _row(1)], 2020, allow_legacy=True)
    rf.require_builder_version([], 2020, allow_legacy=False)


def test_a_boolean_is_not_a_version() -> None:
    with pytest.raises(rf.LegacyRatingsError):
        rf.require_builder_version([{"meta": {"builder_version": True}}], 2020, allow_legacy=False)
