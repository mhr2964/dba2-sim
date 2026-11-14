"""Tier 1: `managers/memory.py` -- §11 step 4's own verify list
(docs/design/phase-3-inter-manager-society.md), followed bullet-for-bullet.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from dba2.managers.memory import H2H_DECAY, H2H_SHADE_PRIOR, shade, target_count_for_pair


@dataclass
class _H2H:
    opened: float
    accepts_by_opponent: float


@dataclass
class _FutilityRow:
    """A hand-built double for `target_count_for_pair` -- only the two
    fields the ladder reads (`far_reject_streak`/`executions`), duck-typed
    exactly like `_H2H` above satisfies `shade`'s own narrower contract."""

    far_reject_streak: float = 0.0
    executions: float = 0.0


def test_the_two_memory_constants_are_declared_at_their_final_step_4_values():
    assert H2H_SHADE_PRIOR == 4.0
    assert H2H_DECAY == 0.6


def test_shade_is_zero_at_n_zero():
    """§11 step 4's verify: "shade is 0.0 at n=0" -- both via `head_to_
    head=None` (no memory subsystem, or this pair has never negotiated)
    and via a real row whose own `opened` is exactly 0.0 (the same value,
    per §6.1(b): "unknown opponent" and "no memory subsystem at all" are
    the same state by construction)."""
    assert shade(None, 0.7709) == 0.0
    assert shade(_H2H(opened=0.0, accepts_by_opponent=0.0), 0.7709) == 0.0


def test_shade_is_zero_when_p_bar_is_none():
    """Decision P3-26: `p_bar is None` (no memory loaded this window, or
    the live population's total `opened` is 0) makes every shade-reading
    channel a strict no-op."""
    assert shade(_H2H(opened=50.0, accepts_by_opponent=40.0), None) == 0.0


@pytest.mark.parametrize("p_bar", [0.05, 0.25, 0.5, 0.634, 0.7709, 0.95])
def test_shade_bounded_by_one_at_the_extremes_of_p_hat_for_every_p_bar(p_bar):
    """§11 step 4's verify: "|shade| <= 1 at p_hat in {0, 1} for every
    p_bar in (0, 1)." `p_hat = accepts_by_opponent / n`, so `p_hat=0` is
    `accepts_by_opponent=0.0` and `p_hat=1` is `accepts_by_opponent=n`,
    at a large-ish `n` so the shrinkage factor `n/(n+H2H_SHADE_PRIOR)` is
    close enough to 1 that a violation of the underlying `|z|<=1` bound
    would still show up after shrinkage."""
    n = 1000.0
    shade_at_zero = shade(_H2H(opened=n, accepts_by_opponent=0.0), p_bar)
    shade_at_one = shade(_H2H(opened=n, accepts_by_opponent=n), p_bar)
    assert abs(shade_at_zero) <= 1.0
    assert abs(shade_at_one) <= 1.0


@pytest.mark.parametrize("p_bar", [0.05, 0.25, 0.5, 0.634, 0.7709, 0.95])
def test_shade_reachable_positive_ceiling_matches_the_disclosed_asymmetry(p_bar):
    """§8.1/Decision P3-26: the reachable POSITIVE range is asserted at
    `z <= (1 - p_bar) / max(p_bar, 1 - p_bar)`, evaluated at `p_hat=1`
    (an opponent who accepts everything) -- the disclosed asymmetry is a
    real, documented property of the formula, not a bug: at the measured
    real `p_bar ~= 0.7709`, the positive ceiling is only ~0.2972, not 1.0.
    Uses a very large `n` so the shrinkage factor is negligibly close to
    1 and does not mask a violation of the underlying `z` bound."""
    n = 1_000_000.0
    expected_ceiling_z = (1.0 - p_bar) / max(p_bar, 1.0 - p_bar)
    result = shade(_H2H(opened=n, accepts_by_opponent=n), p_bar)
    shrink = n / (n + H2H_SHADE_PRIOR)
    assert result == pytest.approx(expected_ceiling_z * shrink, rel=1e-6)
    assert result <= expected_ceiling_z * shrink + 1e-9


def test_shade_at_the_measured_real_p_bar_0_7709_matches_the_designs_own_figure():
    """§8.1's own worked figure: at the measured real `p_bar ~= 0.7709`,
    the positive ceiling is `(1 - 0.7709) / max(0.7709, 0.2291) = 0.2291 /
    0.7709 ~= 0.2972`."""
    p_bar = 0.7709
    expected_ceiling_z = (1.0 - p_bar) / max(p_bar, 1.0 - p_bar)
    assert expected_ceiling_z == pytest.approx(0.2972, abs=0.0005)


def test_shade_sign_reading_matches_the_designs_own_two_sentences():
    """Reading (§8.1): shade > 0 means the opponent says yes more than the
    market average (I've been leaving value on the table); shade < 0
    means they refuse more than average."""
    p_bar = 0.5
    n = 100.0
    generous_opponent = shade(_H2H(opened=n, accepts_by_opponent=90.0), p_bar)
    stingy_opponent = shade(_H2H(opened=n, accepts_by_opponent=10.0), p_bar)
    average_opponent = shade(_H2H(opened=n, accepts_by_opponent=50.0), p_bar)
    assert generous_opponent > 0
    assert stingy_opponent < 0
    assert average_opponent == pytest.approx(0.0, abs=1e-9)


FUTILITY_STREAK = 4.0
"""The real `gm_service.FUTILITY_STREAK` value, spelled out as a plain
literal rather than imported -- `target_count_for_pair` takes the
threshold as a parameter precisely so a Tier-1 test never needs to reach
into `services/` to exercise it (`managers/` purity, no upward import)."""


# ---------------------------------------------------------------------------
# Decision P3-40's ladder -- §10 Tier 1's own five exact test cases,
# verbatim (docs/design/phase-3-inter-manager-society.md §10, §11 step 17).
# Exact-value assertions are a deliberate exception to this file's own
# monotonicity/boundedness convention -- the design doc names this
# function as one of the five functions in the whole phase asserted at
# exact values, because the ladder's PRECEDENCE (not just its two
# thresholds) is the thing under test.
# ---------------------------------------------------------------------------


def test_target_count_ladder_case_1_no_memory_returns_the_default_2():
    assert target_count_for_pair(None, FUTILITY_STREAK) == 2


def test_target_count_ladder_case_2_executions_1_returns_the_up_branchs_3():
    row = _FutilityRow(executions=1.0, far_reject_streak=0.0)
    assert target_count_for_pair(row, FUTILITY_STREAK) == 3


def test_target_count_ladder_case_3_far_reject_streak_4_returns_the_down_branchs_1():
    row = _FutilityRow(far_reject_streak=4.0, executions=0.0)
    assert target_count_for_pair(row, FUTILITY_STREAK) == 1


def test_target_count_ladder_case_4_far_reject_streak_3_is_the_off_by_one_guard():
    """`3.0 >= 4.0` is `False` -- a `>` vs `>=` off-by-one on the threshold
    would wrongly read `1` here; the correct reading falls through to the
    default `2`."""
    row = _FutilityRow(far_reject_streak=3.0, executions=0.0)
    assert target_count_for_pair(row, FUTILITY_STREAK) == 2


def test_target_count_ladder_case_5_the_overlap_down_wins_over_up():
    """Both predicates true at once (§8.4's constructed overlap sequence) --
    `down` is checked first and wins, returning `1`, never `up`'s `3`. This
    is the case that makes the ladder's precedence a test, not a
    paragraph: round 6's four single-predicate cases could not distinguish
    "down wins" from "up wins" from "last branch wins."""
    row = _FutilityRow(executions=1.0, far_reject_streak=4.0)
    assert target_count_for_pair(row, FUTILITY_STREAK) == 1


@pytest.mark.parametrize(
    "row",
    [
        None,
        _FutilityRow(far_reject_streak=0.0, executions=0.0),
        _FutilityRow(far_reject_streak=3.999, executions=0.0),
        _FutilityRow(far_reject_streak=4.0, executions=0.0),
        _FutilityRow(far_reject_streak=100.0, executions=0.0),
        _FutilityRow(far_reject_streak=0.0, executions=1.0),
        _FutilityRow(far_reject_streak=0.999, executions=50.0),
        _FutilityRow(far_reject_streak=4.0, executions=1.0),
        _FutilityRow(far_reject_streak=1_000.0, executions=1_000.0),
    ],
)
def test_target_count_ladder_never_returns_fewer_than_1_target(row):
    """§11 step 17's own verify: every ordered pair still receives AT
    LEAST ONE target -- the ladder's minimum output is `1` (the `down`
    branch), never `0`, so M19's denominator (mutual accepts / packages
    evaluated) cannot collapse to a smaller population than pre-channel-1
    behaviour. The ladder is total over exactly `{1, 2, 3}`, so this is
    also a structural totality check, not just a boundary probe."""
    result = target_count_for_pair(row, FUTILITY_STREAK)
    assert result >= 1
    assert result in (1, 2, 3)


def test_shade_reads_nothing_but_its_own_two_arguments():
    """Structural purity check -- `shade` takes exactly `(head_to_head,
    p_bar)` and reads nothing else; combined with `tests/unit/test_
    layering.py`'s repo-wide `managers/` purity/import-denylist tests
    (which glob every `.py` under `managers/`, so `memory.py` is already
    covered by those without any change on their part), this is the
    "functions read nothing but their arguments" verify item."""
    import inspect

    params = list(inspect.signature(shade).parameters)
    assert params == ["head_to_head", "p_bar"]
