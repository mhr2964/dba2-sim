"""The frame-agnostic selection core -- §6.3 steps 3-6, extracted out of
`select.py` unchanged (Decision P4-20, docs/design/phase-4-media-and-
player-agency.md §11 Stage D step 15; §2.1's Option C).

**What moved here and what did not.** `select.py::select_phrase` was six
steps: (1) compute the frame [the caller's job, unchanged], (2) filter on
the three named dimensions (`philosophy`/`situation`/`window`), (3) filter
on triggers, (4) sort survivors by `template_id`, (5) apply the
multiplicative repeat penalty, (6) seeded weighted draw. **Step 2 does NOT
move here** -- Decision P4-64 is explicit that `_template_matches_
dimensions` is the one step the extraction leaves alone, because it is the
part that is genuinely per-frame (negotiation's three dimensions are not
media's vocabulary, and never will be). Steps 3-6 move, because they are
the part with the measured bugs (the determinism sort, the repeat penalty,
the weighted draw) and the part with no frame-specific vocabulary at all --
`_trigger_holds` already read `getattr(frame, trigger.feature)` generically
before this extraction, and steps 4-6 never looked at a frame at all.

**How a caller that still needs step 2 gets it run in the right place,
without this module knowing what step 2 means.** `select_template` below
takes `dimension_matches` as a parameter -- a caller-supplied predicate
run once per template, in the same combined survivor pass that step 3's
trigger filter runs in. `select.py::select_phrase` passes its own
`_template_matches_dimensions` (unchanged, still living in `select.py`,
still reading `philosophy`/`situation`/`window`); a future media wrapper
(§11 Stage D step 18, not this step) will pass its own predicate over
`StoryFrame`'s vocabulary instead. This module never imports `select.py`,
`frame.py`, or any frame/template class at all -- doing so would make this
module frame-SPECIFIC, exactly the thing extracting it was supposed to
avoid, and would also invert the intended import direction (`select.py`
imports `engine.py`, never the reverse; module layout, §3).

**Typing, deliberately loose.** No `TypeVar`, no `Protocol`, no dataclass,
no module-level constant of any kind lives in this file -- every value
this module's functions need is a required keyword argument with no
default, matching `select.py::select_phrase`'s own "no `managers/`-scoped
constant or literal default, ever" convention. This is not merely a style
preference: `dba2/managers/`'s constant inventory (Decision P3-30, `tests/
unit/managers/test_constant_inventory.py`) walks every module-level
declaration under `managers/` and requires a `GM_TUNING_VERSION` bump on
any change to "a constant or weight table" it finds -- a `TypeVar` or
`Protocol` field is not a tunable in the sense that rule exists to guard,
but the walker cannot tell the difference, and Decision P4-3 already rules
Phase 4 introduces zero `managers/`-scoped constants. A caller passing an
object that merely has `.id`/`.weight`/`.triggers` (each `Trigger`-shaped:
`.feature`/`.op`/`.value`) is satisfied structurally either way; declaring
that structurally in code buys nothing a docstring doesn't already say,
at the cost of a new inventory entry to review and justify not-a-bump for.

Zero I/O, per Decision P2-2(b)'s purity extension (`tests/unit/
test_layering.py`): no `dba2.data`, no `datetime.now()`, no bare
`random.Random()` construction (received only, as `select_template`'s own
`rng` parameter, exactly as `select_phrase` already required of its
callers). Imports stdlib only.
"""
from __future__ import annotations

import operator
import random
from collections.abc import Callable, Mapping, Sequence
from typing import Any


def _trigger_holds(trigger: Any, frame: Any) -> bool:
    """§6.3 step 3 -- one trigger predicate, evaluated by reading
    `trigger.feature` off `frame` via `getattr` and applying `trigger.op`
    from the closed six-operator set §6.3 names. Moved out of `select.py`
    with an unchanged body (byte-for-byte, aside from this docstring's own
    citation update) -- the SIGNATURE changed from `(trigger: Trigger,
    frame: NegotiationFrame)` to `(trigger: Any, frame: Any)`, which is the
    correct and necessary consequence of frame-agnosticism (Decision
    P4-20), not an oversight this docstring's "unchanged" claim covers --
    no vocabulary validation here (Decision P3-18 places that check at
    `phrase_service.py::load_library()`); a caller-supplied
    already-validated library is trusted to name real features/operators.
    The operator table stays function-local, matching `select.py`'s own
    reason for keeping it there before the extraction (`test_constant_
    inventory.py`'s mechanical Decision P3-30 guard) -- **the round 9
    critic fix's own `ordering_ops` set below is function-local for the
    identical reason**, not a module-level constant: a first attempt at
    this fix put it at module scope and tripped `test_constant_inventory.
    py::test_constant_inventory_matches_committed_snapshot` for real
    (`added=['dba2.managers.voice.engine._ORDERING_OPS']`), which is
    exactly the mechanical guard this docstring already cites and exactly
    what Decision P4-3's "Phase 4 introduces zero `managers/`-scoped
    constants" rules out -- moved inline once the guard caught it.

    **round 9 critic fix (blocking finding 2), the one substantive change
    to this function's body since the extraction.** `NegotiationFrame`'s
    own numeric fields are never `None` (every one of §6.1's eleven
    numeric fields is a real float on every input, illegal or not), so
    this case never fired for `select.py`'s own caller. `dba2/media/
    frame.py::StoryFrame` is the first real caller whose vocabulary
    includes fields that are genuinely `None` on a normal, non-short-
    circuited frame (`standings_position`, `season_points_per_game`, and
    others -- see that module's own docstring for which fields have no
    data source wired yet). Evaluating `operator.ge(None, 5)` (or `lt`/
    `gt`/`le`) raises `TypeError`, and because `select_template` evaluates
    every trigger inside one list comprehension, that raise used to kill
    the ENTIRE selection pass for every template in the library -- not
    just skip the one triggered on the null field -- taking down Decision
    P4-25's own no-trigger-fallback guarantee along with it. **A `None`
    actual value on an ordering operator (`lt`/`lte`/`gte`/`gt`) now reads
    as "the trigger does not hold," never a crash** -- the same "`None` is
    neutral, never a value that crashes evaluation" convention `dba2/
    agency/situation.py` already uses for its own not-yet-wired fields
    (Decision P4-9), arriving in a second place for the identical reason.
    `eq` is UNCHANGED (not touched by this fix): `operator.eq(None, x)` is
    well-defined for a `None` `actual` and does not raise, so a template
    author who deliberately writes `{"feature": "insufficient_data", "op":
    "eq", "value": None}` (checking that a frame is NOT short-circuited)
    keeps working exactly as before.

    **A further gap the round 9 critic's own re-verification pass found in
    this fix (not one of the original 4 blocking findings) -- fixed
    directly rather than left as a disclosed sibling gap.** `in`'s own
    None-hazard is on `trigger.value`, not `actual`: `actual_value in
    value` raises `TypeError: argument of type 'NoneType' is not
    iterable` when `value` is `None` (`actual is None` alone is harmless --
    `None in (1, 2, 3)` is a well-defined `False`), and that raise kills
    the whole selection pass exactly like the ordering-operator case
    above, for the identical reason (`select_template`'s one list
    comprehension). A `None` trigger VALUE on `in` now also reads as
    "does not hold." **Left explicitly unfixed, and disclosed rather than
    silently out of scope:** a genuine TYPE mismatch between `actual` and
    `trigger.value` (e.g. `lt` comparing a `str` `actual` against an `int`
    trigger value) still raises on every operator, `in` included --
    broader defensive type-checking here would require validating a
    template's trigger values against the frame's own field types at load
    time (`phrase_service.py::load_library()`'s job, Decision P3-18), not
    a runtime guard in this already-frame-agnostic function. Confirmed
    harmless against the real shipped library (`seeds/gm_phrases.json`: 80
    triggers, zero `None` values, the one nullable `NegotiationFrame`
    field used only with `eq`) -- `StoryFrame`'s own future media library
    (Stage D step 18) is the real reason this is disclosed rather than
    deferred silently."""
    op_funcs = {
        "lt": operator.lt,
        "lte": operator.le,
        "gte": operator.ge,
        "gt": operator.gt,
        "eq": operator.eq,
        "in": lambda actual_value, value: actual_value in value,
    }
    ordering_ops = {"lt", "lte", "gte", "gt"}
    actual = getattr(frame, trigger.feature)
    if trigger.op in ordering_ops and (actual is None or trigger.value is None):
        return False
    if trigger.op == "in" and trigger.value is None:
        return False
    return op_funcs[trigger.op](actual, trigger.value)


def triggers_hold(template: Any, frame: Any) -> bool:
    """Decision P4-107, item 6 (Part A precedent: the identical extraction
    P4-103 already performed for `salience.above_floor`, "never a
    hand-reimplemented twin that could silently drift from the real one").
    `all(_trigger_holds(tr, frame) for tr in template.triggers)` -- a
    template with no triggers always holds (vacuously true, `_trigger_
    holds`'s own step-3 docstring), which is exactly what makes an
    `ungated_kinds` selection's survivor set unconditionally `{dedicated,
    fallback}` (Decision P4-103 Part B).

    `select_template` below calls this ONE function for its own step-3
    filter rather than keeping the inline `all(...)` comprehension it used
    to -- so a caller outside this module (`tools/generate_media_corpus.py`,
    Decision P4-107 item 6's own corpus-side consumer, resolving whether a
    real candidate's own eligible dedicated template's trigger held or the
    selection was forced to fallback by a trigger miss) reads the SAME
    predicate the real selection algorithm evaluates, never a second,
    independently-written copy that could drift from it."""
    return all(_trigger_holds(tr, frame) for tr in template.triggers)


def select_template(
    frame: Any,
    library: Sequence[Any],
    *,
    dimension_matches: Callable[[Any, Any], bool],
    use_counts: Mapping[str, int],
    repeat_penalty: float,
    rng: random.Random,
) -> Any:
    """§6.3 steps 2-6's frame-agnostic core, run in one combined pass --
    step 2 is *invoked* here (as `dimension_matches`) but not *owned*
    here; steps 3-6 are both invoked and owned here, moved unchanged from
    `select.py::select_phrase` (Decision P4-20/P4-47/P4-64). Returns the
    CHOSEN TEMPLATE OBJECT (not a `PhraseSelection`) -- wrapping the
    result into whatever selection shape a caller's own frame/vocabulary
    needs is the caller's job, exactly as `select.py::select_phrase` does
    for negotiation below.

    `frame` -- any object `dimension_matches` and every trigger's
    `feature` can be read off via `getattr`; this module never inspects
    its shape itself.

    `library` -- any sequence of template-shaped objects, each with `.id`
    (`str`), `.weight` (`float`), and `.triggers` (a sequence of objects
    with `.feature`/`.op`/`.value`). `PhraseTemplate`/`Trigger` (`select.
    py`) satisfy this structurally without either class needing to import
    from here.

    `dimension_matches` -- the per-wrapper predicate step 2 stays as
    (Decision P4-64): `select.py::_template_matches_dimensions` for
    negotiation, a media-specific predicate for a future media wrapper.
    Called once per template, in the same pass `_trigger_holds` runs in.

    `use_counts`/`repeat_penalty`/`rng` -- identical contract to `select.
    py::select_phrase`'s own parameters of the same names (see that
    function's docstring for the full statelessness/RNG-derivation
    rationale, unchanged by this extraction): `use_counts` is `{template_
    id: prior use count within this window}`, supplied by the caller each
    call (this module accumulates no state of its own across calls);
    `repeat_penalty` is a required keyword with no default; `rng` is a
    `random.Random` the caller derives and passes in, never constructed
    here.

    Algorithm, unchanged from pre-extraction `select_phrase` steps 2-6:

    2. **Filter on dimensions** (`dimension_matches`, caller-supplied).
    3. **Filter on triggers** (`_trigger_holds`) -- every one of a
       template's `triggers` must hold; a template with no triggers
       always survives this step.
    4. **Sort survivors by `template_id`** -- deterministic ordering,
       independent of `library`'s own iteration order and of `use_
       counts`'s key order (never iterated, only looked up by key).
    5. **Apply the repeat penalty** -- `effective_weight = template.weight
       * (repeat_penalty ** use_counts.get(template.id, 0))`.
    6. **Weighted choice** -- `rng.choices(survivors, weights=..., k=1)
       [0]`, over the sorted survivor list with its penalty-adjusted
       weights; deterministic given the same `rng` state and the same
       (survivors, weights) sequence, which step 4's sort guarantees is
       itself deterministic."""
    dimension_survivors = [t for t in library if dimension_matches(t, frame)]
    trigger_survivors = [t for t in dimension_survivors if triggers_hold(t, frame)]
    survivors = sorted(trigger_survivors, key=lambda t: t.id)

    weights = [
        t.weight * (repeat_penalty ** use_counts.get(t.id, 0)) for t in survivors
    ]
    return rng.choices(survivors, weights=weights, k=1)[0]
