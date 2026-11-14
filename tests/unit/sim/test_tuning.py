"""Decision P1-16's mechanical test, "scoped precisely because the first
version of it wasn't": (a) every name in an ability-vector weight dict is
either a literal `PlayerRatings` field or one of the two Decision P1-13
anchor-normalized physicals (`wingspan_in`/`height_in`) -- never a
tendency, role, or strategy field; (b) every `PlayerTendencies` field a
resolution beat actually reads (vector or Modifiers column alike) is a
member of the allowlist (originally five names -- `steal_gamble_freq`/
`foul_freq`/`pullup_vs_catch`/`crash_oreb_freq`/`help_gamble_freq` --
grown to eight by the B3 fix's design-doc amendment adding
`shot_freq_rim`/`shot_freq_mid`/`shot_freq_three`; see
`_ALLOWLISTED_TENDENCY_FIELDS` below for the current, authoritative set).
This was the subject of 2 separate blocking bugs in this design's review
history (§4.7's Decision text) -- not explicitly one of steps 1-3's named
"Verify" bullets, but squarely inside this dispatch's hard constraints, so
it is proactively enforced here rather than left to be discovered when
beats 4/5 are built.

B4 fix (critic-proven): both guards used to be hand-maintained lists --
`_ABILITY_VECTORS` enumerated 19 named attributes by hand, and
`_RESOLUTION_BEAT_FILES` named exactly `screen.py`/`on_ball.py`/`help.py`.
Critic proved both are blind to (a) a brand-new `TuningProfile` field with
a tendency in its ability vector (invisible until someone remembers to
add it to the hardcoded list) and (b) the `getattr(x.tendencies,
"field_name")` idiom, which evades a check that only looks for
`x.tendencies.field_name` as a literal attribute access -- and
`beats/setup.py` already uses that exact idiom in 2 places today (legally
-- it's a selection beat, unrestricted per Decision P1-8 -- but the old
checker's blind spot was real regardless of whether anything was actually
wrong there yet). Both guards below are rebuilt to derive from the actual
source instead: the ability-vector check introspects `TuningProfile`'s
real dataclass fields (recursively, through every nested tuning
dataclass) for anything typed `WeightVector`, so a newly-added
weight-vector field is automatically in scope, not opt-in; the tendency
check walks `dba2/sim/beats/*.py`'s actual files (excluding `setup.py`,
the one selection beat Decision P1-16 explicitly carves out) instead of a
hardcoded filename tuple, so step 4's new beat files (`rotation.py`,
`resolution.py`, `rebound.py`) are automatically in scope when they're
added -- and the AST walk now also catches the `getattr(obj,
"field_name")` string-literal call shape, not just literal attribute
access.

B4-2 fix (critic-proven): "typed `WeightVector`" above was still opt-in in
disguise -- critic proved a field annotated `Mapping[str, float]` (the
type `WeightVector` aliased to, before this fix) sailed through the
ability-vector guard with a tendency field sitting in it, because the old
`_iter_weight_vector_fields` matched on the *annotation string*
(`f.type == "WeightVector"`), and `SetupTuning.action_type_base_weight`
proves that spelling choice is a live convention in this codebase, not a
hypothetical. `WeightVector` is now a real class (`dba2/sim/tuning.py`),
constructed only by that module's `_wv()` helper, and the guard below
checks `isinstance(value, WeightVector)` against each field's actual
runtime value -- so detection no longer depends on how a future field is
annotated, only on which constructor built its value, which is what makes
"automatically in scope, not opt-in" true this time: a same-shaped
`Mapping[str, float]`-annotated field is only excluded if it was
*actually* built some other way (like `action_type_base_weight`'s plain
`MappingProxyType(...)`), not by annotation-string luck.
"""
from __future__ import annotations

import ast
import dataclasses
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from dba2.domain.ratings import PlayerRatings, PlayerTendencies
from dba2.domain.role import PlayerDirective
from dba2.sim.tuning import DEFAULT_TUNING, WeightVector, _wv

REPO_ROOT = Path(__file__).resolve().parents[3]
BEATS_DIR = REPO_ROOT / "dba2" / "sim" / "beats"

_RATING_FIELD_NAMES = {f.name for f in dataclasses.fields(PlayerRatings)}
_HONORARY_ABILITY_VECTOR_PHYSICALS = {"wingspan_in", "height_in"}
_ALLOWED_ABILITY_VECTOR_NAMES = _RATING_FIELD_NAMES | _HONORARY_ABILITY_VECTOR_PHYSICALS

# B3 fix (critic-proven): Decision P1-16's original 5-name allowlist grew
# 3 more names (`shot_freq_rim`/`shot_freq_mid`/`shot_freq_three`) via a
# design-doc amendment (phase-1-simulation-engine.md §4.4/§4.7) -- zone
# selection is the same *kind* of thing `pullup_vs_catch` already permits
# on the same Resolution(shot) row (a tendency field steering a branch/
# selection, never entering an ability-score vector), it just wasn't on
# the enumerated list. See `tuning.ResolutionShotTuning`'s docstring and
# `beats/resolution.py::select_shot_zone` for the full history: an earlier
# version of this dispatch moved zone selection into `playbook.py`
# specifically to evade this guard's file-scoped reach rather than
# widening the allowlist, and critic proved that was an evasion, not a
# legitimate design call.
_ALLOWLISTED_TENDENCY_FIELDS = {
    "steal_gamble_freq",
    "foul_freq",
    "pullup_vs_catch",
    "crash_oreb_freq",
    "help_gamble_freq",
    "shot_freq_rim",
    "shot_freq_mid",
    "shot_freq_three",
}
_TENDENCY_FIELD_NAMES = {f.name for f in dataclasses.fields(PlayerTendencies)}

# The one selection beat Decision P1-16 explicitly carves out as
# unrestricted (setup, role assignment) -- everything else under
# `beats/` is resolution-shaped and subject to `_ALLOWLISTED_TENDENCY_FIELDS`.
# `__init__.py` is not a beat at all. Both exclusions are structural
# (module layout / Decision P1-16's own text), not a per-beat opt-in list
# that a new resolution beat could fall outside of by omission.
_UNRESTRICTED_BEAT_FILENAMES = {"setup.py", "__init__.py"}


def _iter_weight_vector_fields(instance: object, path: str = "") -> list[tuple[str, object]]:
    """Recursively walk `instance`'s dataclass fields (and every nested
    dataclass field) collecting every field whose *runtime value* is a
    `WeightVector` instance -- the marker class `tuning._wv()` is the one
    and only constructor for (see the B4-2 comment above for why this
    replaced the old `f.type == "WeightVector"` annotation-string check).
    Deliberately keys off the value, not the declared annotation: a field
    annotated `Mapping[str, float]` whose value was actually built by
    `_wv()` is still found (proving detection survives an annotation
    spelling change); a field annotated `WeightVector` whose value was
    somehow *not* built by `_wv()` would not be (there is no such field in
    this codebase today, but the mechanism doesn't rely on there never
    being one)."""
    found: list[tuple[str, object]] = []
    for f in dataclasses.fields(instance):
        value = getattr(instance, f.name)
        label = f"{path}{f.name}" if not path else f"{path}.{f.name}"
        if isinstance(value, WeightVector):
            found.append((label, value))
        elif dataclasses.is_dataclass(value):
            found.extend(_iter_weight_vector_fields(value, path=label))
    return found


def test_every_ability_vector_field_is_a_rating_field_or_an_anchor_normalized_physical():
    ability_vectors = _iter_weight_vector_fields(DEFAULT_TUNING)
    assert ability_vectors, (
        "introspection found zero WeightVector-typed fields on TuningProfile -- "
        "the discovery mechanism itself is broken, not just an empty result"
    )
    offenders = [
        f"{label}: {field_name}"
        for label, weights in ability_vectors
        for field_name in weights
        if field_name not in _ALLOWED_ABILITY_VECTOR_NAMES
    ]
    assert not offenders, (
        f"Decision P1-16 violation -- non-PlayerRatings field in an ability vector: {offenders}"
    )


def test_real_tuning_fields_built_outside__wv_are_not_mistaken_for_ability_vectors():
    """`SetupTuning.action_type_base_weight` is annotated `Mapping[str,
    float]` -- the exact shape critic proved evades an annotation-string
    check -- and its keys (`"pnr_ball"`, `"iso"`, ...) are action-type
    names, not `PlayerRatings` fields, so if the guard ever mistakenly
    swept it in, `test_every_ability_vector_field_is_a_rating_field_or_an_
    anchor_normalized_physical` above would fail loudly. This test names
    that expectation directly instead of leaving it as an implicit
    side-effect of the other test passing."""
    ability_vector_labels = {label for label, _ in _iter_weight_vector_fields(DEFAULT_TUNING)}
    assert "setup.action_type_base_weight" not in ability_vector_labels


def test_ability_vector_discovery_is_keyed_on_runtime_type_not_annotation_spelling():
    """B4-2 regression, critic's exact reproduction: a `Mapping[str,
    float]`-annotated field (not `WeightVector`) carrying a tendency field
    name must still be caught -- because it's a genuine `WeightVector`
    *value* (built via `_wv()`), regardless of what its annotation says.
    A sibling field with the identical `Mapping[str, float]` annotation
    but built by hand (never routed through `_wv()`) -- the shape
    `action_type_base_weight` actually uses -- must NOT be caught, proving
    the guard isn't just newly over-inclusive of every `Mapping`-shaped
    field; it's keyed on the constructor, not the annotation, in both
    directions."""

    @dataclasses.dataclass(frozen=True)
    class _FakeTuningWithEvasiveAnnotation:
        # Annotated identically to `action_type_base_weight` -- the shape
        # critic proved evaded the old `f.type == "WeightVector"` check --
        # but *is* a real ability vector (built via `_wv()`), smuggling in
        # a disallowed tendency field (`steal_gamble_freq`) the way
        # critic's reproduction did.
        sneaky_ability_vector: Mapping[str, float] = dataclasses.field(
            default_factory=lambda: _wv({"steal_gamble_freq": 1.0})
        )
        # Same annotation, but genuinely not an ability vector (never
        # passed through `_wv()`) -- must stay excluded.
        not_an_ability_vector: Mapping[str, float] = dataclasses.field(
            default_factory=lambda: MappingProxyType({"steal_gamble_freq": 1.0})
        )

    found = dict(_iter_weight_vector_fields(_FakeTuningWithEvasiveAnnotation()))
    assert "sneaky_ability_vector" in found, (
        "a Mapping[str, float]-annotated field built via _wv() was not detected -- "
        "the guard is still keyed on annotation spelling, not runtime type"
    )
    assert "not_an_ability_vector" not in found, (
        "a Mapping[str, float]-annotated field NOT built via _wv() was falsely "
        "flagged -- the guard is over-inclusive of every Mapping-shaped field"
    )

    # And the legitimately-typed `WeightVector` fields the rest of this
    # module already relies on are still found -- no regression from the
    # annotation-string check to the runtime-type check.
    real = _iter_weight_vector_fields(DEFAULT_TUNING)
    assert len(real) == 27, (
        "WeightVector field count on DEFAULT_TUNING changed -- either a real "
        f"regression or this count needs updating to match: found {len(real)} "
        "(docs/design/pick-and-pop.md added 3: rotation_pop.offense_weights, "
        "rotation_pop.defense_weights, rotation_branch.pop_shooting_weights; "
        "docs/design/turnover-recovery.md added 1: loose_ball.weights)"
    )
    assert "loose_ball.weights" in dict(real), (
        "TR-5.B.16: the loose-ball vector must be auto-discovered by the "
        "ability-vector guard, not merely counted"
    )


def _resolution_beat_files() -> tuple[Path, ...]:
    files = tuple(
        sorted(
            p for p in BEATS_DIR.glob("*.py") if p.name not in _UNRESTRICTED_BEAT_FILENAMES
        )
    )
    assert files, f"found zero beat files under {BEATS_DIR} -- glob/exclusion is broken"
    return files


def _tendency_field_accesses(path: Path) -> set[str]:
    """Every tendency-field read in `path` this check can see, two shapes:

    1. literal `<something>.tendencies.<field>` attribute access (the
       original check).
    2. `getattr(<something>.tendencies, "<field>")` -- the evasion critic
       proved works today: no `ast.Attribute` node for the field name
       exists in this shape at all, so shape 1 alone is structurally
       blind to it, not just under-tested against it.

    Both are restricted to string literals matching an actual
    `PlayerTendencies` field name, same as the original check, so an
    unrelated `.tendencies.foo`/`getattr(x, "foo")` on some other object
    is not miscounted. Alias-proofing (a local variable holding
    `player.tendencies` before the access) is out of scope here, same as
    critic's own explicit non-blocking carve-out for `test_layering.py`'s
    import-aliasing gap -- this closes the specific `getattr` string-
    literal pattern already live in this codebase, not every conceivable
    evasion of a static AST check.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _TENDENCY_FIELD_NAMES
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "tendencies"
        ):
            found.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "tendencies"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value in _TENDENCY_FIELD_NAMES
        ):
            found.add(node.args[1].value)
    return found


def test_resolution_beats_only_read_allowlisted_tendency_fields():
    """Decision P1-16: "Selection beats (setup, role assignment) are
    unrestricted"; only the resolution-shaped beats (screen/on_ball/help
    today, rotation/resolution/rebound once step 4-5 lands) are checked
    here -- `beats/setup.py` deliberately reads tendency fields outside
    this allowlist (e.g. `pnr_ball_freq`, `spot_up_freq`), including via
    `getattr(player.tendencies, <dynamic field name>)` in 2 places, both
    legal under Decision P1-8 and excluded from this check by the same
    `_UNRESTRICTED_BEAT_FILENAMES` structural rule the ability-vector
    check has no equivalent of needing (setup.py has no offense/defense
    weight vectors at all)."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {field_name}"
        for path in _resolution_beat_files()
        for field_name in _tendency_field_accesses(path)
        if field_name not in _ALLOWLISTED_TENDENCY_FIELDS
    ]
    assert not offenders, (
        "Decision P1-16 violation -- non-allowlisted tendency field read by a "
        f"resolution beat: {offenders}"
    )


# docs/design/gm-command-surface.md PD-9: Decision P1-16's guard extended
# to `.directive.<field>` reads, rescoped in the design doc's own revision
# 2 from an (unimplementable) dataflow check to the same *read allowlist*
# shape the tendency guard above already uses. **A per-file allowlist,
# not a global one** -- `defense_mode` is legitimate in `help.py` and
# would be a real mistake in `resolution.py` (and vice versa for
# `shot_diet`), so a single global set would wave both through.
_DIRECTIVE_FIELD_NAMES = {f.name for f in dataclasses.fields(PlayerDirective)}
_DIRECTIVE_FIELD_ALLOWLIST: dict[str, frozenset[str]] = {
    "resolution.py": frozenset({"shot_diet"}),
    "help.py": frozenset({"defense_mode"}),
}
# PD-9's own disclosed limit (R10): `setup.py` is structurally carved out
# of `_resolution_beat_files()` (it is a selection beat, per Decision
# P1-16), so `usage_mode` -- PD-5's own directive hook, which lives in
# `setup.py::_role_weight` -- has NO mechanical guard at all. Not a gap in
# this test; a gap in what P1-16's carve-out can honestly cover, named
# here rather than left for a reader to assume a green suite proves
# otherwise.


def _directive_field_accesses(path: Path) -> set[str]:
    """Structural twin of `_tendency_field_accesses` above, pointed at
    `.directive.<field>` instead of `.tendencies.<field>` -- same two
    shapes (literal attribute access and the `getattr(x.directive,
    "<field>")` evasion), same string-literal restriction to real
    `PlayerDirective` field names, same alias-proofing carve-out."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _DIRECTIVE_FIELD_NAMES
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "directive"
        ):
            found.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "directive"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value in _DIRECTIVE_FIELD_NAMES
        ):
            found.add(node.args[1].value)
    return found


def test_resolution_beats_only_read_their_own_allowlisted_directive_fields():
    """PD-9's rescoped guard. Mutation 16: a `.directive.clutch_mode` read
    added to `rotation.py` (or a `.directive.defense_mode` read added to
    `resolution.py`, the wrong-file case) must make this fail, naming the
    file and the field."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {field_name}"
        for path in _resolution_beat_files()
        for field_name in _directive_field_accesses(path)
        if field_name not in _DIRECTIVE_FIELD_ALLOWLIST.get(path.name, frozenset())
    ]
    assert not offenders, (
        "PD-9 violation -- directive field read by a beat not on that beat's own "
        f"allowlist: {offenders}"
    )


def test_directive_field_allowlist_names_are_real_playerdirective_fields():
    """A typo in `_DIRECTIVE_FIELD_ALLOWLIST` (e.g. `"shot_diets"`) would
    silently allow nothing and forbid nothing -- both the allowed field and
    a genuine violation would read as "not in the allowlist" and this
    guard's own offender list would be indistinguishable from a real
    letdown. Pinned against the live field set instead of trusted by eye."""
    for filename, fields in _DIRECTIVE_FIELD_ALLOWLIST.items():
        unknown = fields - _DIRECTIVE_FIELD_NAMES
        assert not unknown, f"{filename}'s allowlist names non-fields: {unknown}"
