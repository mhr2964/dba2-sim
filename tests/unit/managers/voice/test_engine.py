"""`managers/voice/engine.py` -- §11 step 15's frame-agnostic selection
core, tested directly against minimal structural stand-ins (never
`NegotiationFrame`/`Trigger`, so a real bug in either of THOSE classes
cannot mask a bug in this module's own generic logic) -- `_trigger_holds`
and `select_template` only ever need `.feature`/`.op`/`.value` and
`.id`/`.weight`/`.triggers` respectively, per their own docstrings'
"structural, not inheritance" contract.

**round 9 critic fix (blocking finding 2).** `test_select.py`'s own
parametrized operator tests (`test_each_closed_operator_evaluated_
against_the_frame`) never exercise a `None` actual value, because
`NegotiationFrame`'s eleven numeric fields are never `None` on any input
(illegal or not) -- this was true before this fix and remains true, so
those tests are untouched and still the right coverage for `select.py`'s
own caller. `dba2/media/frame.py::StoryFrame` is the first real caller
whose vocabulary includes genuinely `None` fields on a normal (non-short-
circuited) frame, and evaluating an ordering operator against one used to
raise `TypeError` inside a list comprehension, killing the entire
selection pass -- see `engine.py::_trigger_holds`'s own docstring for the
full failure mode and this file's tests for the fix's regression
coverage, added directly at this module's own boundary rather than only
through `dba2/media/`'s tests, since `engine.py` must never import
`frame.py` (Decision P4-20) and its own tests should not need to either.
"""
from __future__ import annotations

import operator
import random
from dataclasses import dataclass
from typing import Any

import pytest

from dba2.managers.voice import engine


@dataclass(frozen=True)
class _Trigger:
    feature: str
    op: str
    value: Any


@dataclass(frozen=True)
class _Template:
    id: str
    weight: float
    triggers: tuple[_Trigger, ...] = ()


@dataclass(frozen=True)
class _Frame:
    """A minimal stand-in with exactly the attributes a test needs --
    proof that `_trigger_holds`/`select_template` need nothing more than
    `getattr`-able attributes, per `engine.py`'s own docstring."""

    value: Any = None


# ---------------------------------------------------------------------------
# _trigger_holds -- the None-actual-value guard on the four ordering ops.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", ["lt", "lte", "gte", "gt"])
def test_none_actual_does_not_hold_for_every_ordering_operator(op):
    """The critic's exact reproduction: `op_funcs[op](None, 5)` used to
    raise `TypeError` for all four of these operators. It must now read
    as "does not hold" (`False`), never raise."""
    trigger = _Trigger(feature="value", op=op, value=5)
    assert engine._trigger_holds(trigger, _Frame(value=None)) is False


@pytest.mark.parametrize("op", ["lt", "lte", "gte", "gt"])
def test_none_trigger_value_does_not_hold_either(op):
    """The mirror case -- a trigger's own `.value` is `None` rather than
    the frame's actual. `operator.ge(5, None)` raises the identical
    `TypeError`; guarded the same way."""
    trigger = _Trigger(feature="value", op=op, value=None)
    assert engine._trigger_holds(trigger, _Frame(value=5)) is False


def test_eq_against_none_is_unaffected_by_the_guard():
    """`eq` is deliberately NOT in `_trigger_holds`'s own function-local
    `ordering_ops` set -- `operator.eq` is well-defined for `None` and does
    not raise, so a template author checking "this field really is `None`"
    must still get a real `True`, not a blanket `False` from an over-broad
    guard."""
    trigger = _Trigger(feature="value", op="eq", value=None)
    assert engine._trigger_holds(trigger, _Frame(value=None)) is True
    assert engine._trigger_holds(trigger, _Frame(value=5)) is False


def test_in_against_a_none_actual_is_unaffected_by_the_guard():
    trigger = _Trigger(feature="value", op="in", value=(1, 2, 3))
    assert engine._trigger_holds(trigger, _Frame(value=None)) is False


def test_in_against_a_none_trigger_value_does_not_raise():
    """A further gap the round 9 critic's own re-verification pass found
    (not one of the original 4 blocking findings): `actual_value in value`
    raises `TypeError: argument of type 'NoneType' is not iterable` when
    `value` (the trigger's own `.value`, not the frame's actual) is `None`
    -- a `None` actual alone is harmless (`None in (1, 2, 3)` is a
    well-defined `False`, covered by the test above). `in` has its own
    dedicated `trigger.value is None` guard, separate from `ordering_ops`,
    since `in` is not an ordering operator."""
    trigger = _Trigger(feature="value", op="in", value=None)
    assert engine._trigger_holds(trigger, _Frame(value=5)) is False
    assert engine._trigger_holds(trigger, _Frame(value=None)) is False


@pytest.mark.parametrize("op", ["lt", "lte", "gte", "gt"])
def test_ordering_operator_still_holds_for_two_real_numbers(op):
    """The guard must not swallow the real, non-degenerate case -- proven
    per operator, mirroring test_select.py's own parametrized coverage."""
    op_funcs = {"lt": operator.lt, "lte": operator.le, "gte": operator.ge, "gt": operator.gt}
    trigger = _Trigger(feature="value", op=op, value=5)
    frame_below = _Frame(value=4)
    frame_above = _Frame(value=6)
    assert engine._trigger_holds(trigger, frame_below) == op_funcs[op](4, 5)
    assert engine._trigger_holds(trigger, frame_above) == op_funcs[op](6, 5)


# ---------------------------------------------------------------------------
# select_template -- the critic's own top-level concern: a single
# null-field trigger must not take down the whole selection pass, and the
# no-trigger fallback (Decision P4-25) must still be reachable.
# ---------------------------------------------------------------------------


def test_a_null_field_trigger_does_not_crash_the_whole_selection_pass():
    """Before the fix, `select_template`'s list comprehension raised
    `TypeError` on the first null-field ordering trigger it evaluated,
    which would have killed selection for every template in the library,
    not just the one triggered on the null field -- reproduced directly
    here rather than only through `dba2/media/`'s own integration test."""
    null_field_template = _Template(
        id="null_field", weight=1.0, triggers=(_Trigger(feature="value", op="gte", value=5),)
    )
    fallback = _Template(id="fallback", weight=1.0, triggers=())

    chosen = engine.select_template(
        _Frame(value=None),
        (null_field_template, fallback),
        dimension_matches=lambda template, frame: True,
        use_counts={},
        repeat_penalty=0.5,
        rng=random.Random(1),
    )
    assert chosen.id == "fallback"


# ---------------------------------------------------------------------------
# triggers_hold -- Decision P4-107 item 6's extraction (the identical
# `salience.above_floor` shape: one shared predicate, never a second,
# hand-reimplemented copy). Both `all(_trigger_holds(...) for ...)`
# correctness AND real wiring into `select_template` are covered below --
# a passing correctness test alone would not prove the extraction is
# actually CALLED, only that it exists (the exact "dead extraction" gap
# the dispatch names).
# ---------------------------------------------------------------------------


def test_triggers_hold_true_for_a_template_with_no_triggers():
    """Vacuous truth -- `all(())` is `True` -- is what makes every
    `ungated_kinds` selection's survivor set unconditionally `{dedicated,
    fallback}` (Decision P4-103 Part B)."""
    template = _Template(id="ungated", weight=2.0, triggers=())
    assert engine.triggers_hold(template, _Frame(value=None)) is True


def test_triggers_hold_requires_every_trigger_to_hold():
    template = _Template(
        id="gated",
        weight=2.0,
        triggers=(
            _Trigger(feature="value", op="gte", value=5),
            _Trigger(feature="value", op="lte", value=10),
        ),
    )
    assert engine.triggers_hold(template, _Frame(value=7)) is True
    assert engine.triggers_hold(template, _Frame(value=3)) is False
    assert engine.triggers_hold(template, _Frame(value=11)) is False


def test_triggers_hold_matches_trigger_holds_evaluated_by_hand():
    """`triggers_hold` must be exactly `all(_trigger_holds(tr, frame) for
    tr in template.triggers)` -- never a second, independently-written
    predicate that happens to agree today."""
    triggers = (
        _Trigger(feature="value", op="gt", value=1),
        _Trigger(feature="value", op="lt", value=100),
    )
    template = _Template(id="t", weight=1.0, triggers=triggers)
    for value in (0, 1, 2, 50, 99, 100, 200, None):
        frame = _Frame(value=value)
        expected = all(engine._trigger_holds(tr, frame) for tr in triggers)
        assert engine.triggers_hold(template, frame) == expected


def test_triggers_hold_is_actually_wired_into_select_template_not_a_dead_extraction():
    """Mutation-proof: monkeypatch `engine.triggers_hold` to always report
    `False` (a real, observable behavior change if and only if `select_
    template`'s own step-3 filter actually calls the module-level name)
    and confirm a template that would otherwise win now never survives --
    proof this is a live call, not a dead, unreferenced extraction."""
    dedicated = _Template(id="dedicated", weight=100.0, triggers=())
    fallback = _Template(id="fallback", weight=1.0, triggers=())

    chosen = engine.select_template(
        _Frame(value=1),
        (dedicated, fallback),
        dimension_matches=lambda template, frame: True,
        use_counts={},
        repeat_penalty=0.5,
        rng=random.Random(1),
    )
    assert chosen.id == "dedicated"  # overwhelmingly likely at weight 100 vs 1

    # Monkeypatch the MODULE-LEVEL name, not a local reference --
    # `select_template`'s own body calls the bare name `triggers_hold(t,
    # frame)`, resolved against `engine`'s own globals at CALL time (never
    # frozen at def time), so reassigning `engine.triggers_hold` is a real
    # interception of the live call site, not a no-op on a copy.
    original = engine.triggers_hold
    engine.triggers_hold = lambda template, frame: False
    try:
        # With `triggers_hold` forced `False`, EVERY template fails step 3
        # -- `trigger_survivors` is empty and `rng.choices([], weights=[],
        # k=1)` raises `IndexError`. That raise is itself the proof: a
        # DEAD extraction (one `select_template` never actually calls)
        # would leave `dedicated` selectable exactly as before, and this
        # block would return normally instead of raising.
        with pytest.raises(IndexError):
            engine.select_template(
                _Frame(value=1),
                (dedicated, fallback),
                dimension_matches=lambda template, frame: True,
                use_counts={},
                repeat_penalty=0.5,
                rng=random.Random(1),
            )
    finally:
        engine.triggers_hold = original

    # And the mutation is reversible -- the ORIGINAL, real `triggers_hold`
    # restored, `dedicated` is selectable again exactly as at the top of
    # this test, proving the patch (not some other change) was the cause.
    chosen_after_restore = engine.select_template(
        _Frame(value=1),
        (dedicated, fallback),
        dimension_matches=lambda template, frame: True,
        use_counts={},
        repeat_penalty=0.5,
        rng=random.Random(1),
    )
    assert chosen_after_restore.id == "dedicated"
