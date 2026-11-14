"""Decision P3-30's mechanical single-bump guard (docs/design/phase-3-
inter-manager-society.md §10, §11 step 4).

Walks every `.py` file under `dba2/managers/` with stdlib `ast` and
produces a sorted `(qualified_name, canonical_value_repr)` inventory,
compared against the committed `constant_inventory.json`. `GM_TUNING_
VERSION` bumps on any change to a constant or weight table anywhere
under `managers/` (`tuning.py:17-22`'s own rule, read verbatim) -- this
file is what makes "any change" mechanical rather than a promise a
reviewer has to remember to check by eye.

**One recursive canonicaliser, `_canon_value`, applied to every value
expression the declaration sites below feed it** -- §10's own framing:
"the canonicaliser is where all the coverage actually lives," not the
declaration sites. Node forms handled, in the order §10 states them:

- `Constant` (int/float/bool/str/None) -> the value itself.
- `UnaryOp` (`USub`/`UAdd`/`Invert`/`Not`) over a numeric operand ->
  folded (`-0.90` canonicalises identically to a literal `-0.90`).
- `BinOp` (`Add`/`Sub`/`Mult`/`Div`/`FloorDiv`/`Mod`/`Pow`) over two
  numeric operands -> evaluated (`6 / 30` canonicalises to the same
  float as a literal `0.2` -- Python's `/` gives both the identical
  nearest-double, so this is not an approximation).
- `Tuple`/`List`/`Set`/`Dict` literals -> recurse into every element
  (both key and value, for a `Dict`).
- `Attribute` on a `Name`/`Attribute` chain (e.g.
  `CompetitiveWindow.CONTEND`) -> the dotted name as a string, tagged
  `attr:` so it can never collide with a same-text string constant's own
  canonical form.
- **`Name` referring to "a module-level declaration"** -- §10's own
  wording, read literally rather than narrowed to same-file (a critic
  finding on this dispatch's first pass: `tuning: GmTuning =
  DEFAULT_TUNING`-shaped defaults are common across `managers/`, and
  `DEFAULT_TUNING` almost never lives in the module doing the
  referencing). Resolved two ways, tried in order:
    1. `node.id` is declared at THIS module's own top level -> `ref:
       <name>` (unqualified -- §10's own examples, `_NO_TILT`/
       `_DEFENSE_FIRST_TILT`, are exactly this same-file case).
    2. `node.id` is a locally-imported alias (`from dba2.managers.X
       import Y [as Z]`, absolute imports only -- confirmed by direct
       grep that no module under `managers/` uses a relative import)
       whose target `Y` is itself a module-level declaration in `X` ->
       `ref:<X's dotted module name>.<Y>`, fully qualified so it can
       never collide with a same-named local declaration in a different
       module.
  Anything else (a builtin, a bare class-name reference outside a call,
  an import of something that isn't itself a tracked module-level
  declaration) falls through to the hatch.
- `Call` to a recognised immutability wrapper (`MappingProxyType`,
  `frozenset`, `tuple`, `dict`, `list`, `set`) -- **zero-argument calls
  now canonicalise to the empty container they construct** (`frozenset()`
  -> the empty set, `tuple()` -> the empty tuple, ...) rather than
  falling through to the hatch; a lone positional argument and no
  keywords still recurses transparently, matching §10's original "recurse
  into the single argument" text for the one-argument case it was
  written against.
- `Call` to `field(...)` -> `default=` recurses directly; `default_
  factory=<lambda: X>` or `default_factory=<a module-level Name>`
  recurses into `X`/resolves the `ref:`; neither present -> `NO_DEFAULT`.
- **`Call` to a recognised dataclass constructor** -- a bare `Name`
  resolving to a `ClassDef` ANYWHERE under `managers/` carrying a
  dataclass decorator in any of its four spellings (`@dataclass`,
  `@dataclasses.dataclass`, `@dataclass(...)`, `@dataclasses.dataclass
  (...)` -- every real site in this codebase today uses the last form,
  which is exactly the form a decorator check that only matches the
  bare-`Name` spelling would miss entirely, per §10's own history) ->
  recurses into keyword values and positional values (matched by index
  against the class's own field order), canonicalising the call exactly
  as if it were the dict literal it is.
- **Anything else** (a comprehension, an unresolved call, an f-string, a
  conditional expression, a `Subscript` type alias, ...) -> the escape
  hatch: `UNANALYZABLE:<qualified_name>:<first 16 hex chars of
  sha256(ast.unparse(value_node))>`. `ast.unparse` normalises formatting
  and drops comments, so a pure reformat/comment edit does NOT move this
  string; any change to the expression itself does.

**The declaration sites feeding it, per §10:**

- (a) module-level `Assign`/`AnnAssign` to a single `Name` -- unfiltered
  by type, so a type alias (`RookieScaleTable = Mapping[int,
  RookieScale]`) is swept in too, reaching the hatch via its `Subscript`
  value (expected: it never changes, and teaching the walker to
  distinguish "type alias" from "constant" risks dropping a real one).
- (b) every class body (not only `@dataclass`-decorated ones), recording
  each `AnnAssign`/`Assign` field -- this is what brings enum members
  (`CompetitiveWindow.REBUILD = "rebuild"`) and defaultless dataclass
  fields into the inventory, and is deliberately independent of the
  dataclass-decorator check above (so even a broken decorator check
  still surfaces every class's field list).
- (c) numeric/string/tuple defaults in any `FunctionDef`'s
  `args.defaults`/`kw_defaults`, at module or class scope.
- (d) `field(default=...)`/`field(default_factory=...)` -- handled
  inside the canonicaliser itself (site (b)'s AnnAssign value IS a
  `field(...)` call in that case), not as a separate top-level pass.

**The hatch population is measured, not assumed** -- `test_constant_
inventory_hatch_population_is_pinned_at_its_measured_size` below asserts
the real, current count and lists every member, so a future change to
the tree (or a future fix to the canonicaliser that shrinks it further)
is a deliberate, reviewed diff rather than a silent drift the way the
walker's own history already produced once (round 4's name-only hatch
absorbing a real value change with no signal at all).

Mutation-tested below against Decision P3-30's own eight named
mutations, per HANDOFF's own instruction ("a real gap in the AST
import-checker survived one full build+critic round before being caught
this way" -- the same discipline applied here).
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import operator
from dataclasses import dataclass
from pathlib import Path

import pytest

# This file writes scratch files into `dba2/` or scans it (`tests/conftest.py`): one xdist
# group keeps every such writer and reader on one worker, run serially.
pytestmark = pytest.mark.xdist_group("source-tree")

REPO_ROOT = Path(__file__).resolve().parents[3]
MANAGERS_DIR = REPO_ROOT / "dba2" / "managers"
INVENTORY_PATH = Path(__file__).resolve().parent / "constant_inventory.json"

_WRAPPER_NAMES = frozenset({"MappingProxyType", "frozenset", "tuple", "dict", "list", "set"})
_WRAPPER_EMPTY_KIND = {
    "MappingProxyType": "dict",
    "frozenset": "set",
    "tuple": "tuple",
    "dict": "dict",
    "list": "list",
    "set": "set",
}
_BINOP_FUNCS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_NUMERIC_OPS = (ast.USub, ast.UAdd, ast.Invert)


def _is_numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class _Unanalyzable(Exception):
    """Raised by `_canon_value` on any node form it does not recognise --
    caught exactly once, at the top of the declaration that reached it,
    so a nested failure collapses the WHOLE declaration to the hash
    hatch rather than a partial structure with one field silently
    replaced (§10's own history: this is precisely how round 4's walker
    stayed silent on the `PROFILES` mutation it was written to catch)."""


# ---------------------------------------------------------------------------
# Structural helpers
# ---------------------------------------------------------------------------


def _flatten_dotted(node: ast.AST) -> str | None:
    """`Name`/`Attribute` chain -> dotted string, or `None` if `node`
    isn't one (a call result, a subscript, etc.) -- mirrors `tests/unit/
    test_layering.py::_flatten_attr`'s own technique."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _flatten_dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _is_dataclass_decorator(node: ast.AST) -> bool:
    """True for any of the four spellings §10 names: `@dataclass`,
    `@dataclasses.dataclass`, `@dataclass(...)`, `@dataclasses.dataclass
    (...)`. Every real `@dataclass` site under `managers/` today uses the
    last form -- a check that only matched the bare-`Name` form would
    match zero of them."""
    target = node.func if isinstance(node, ast.Call) else node
    dotted = _flatten_dotted(target)
    return dotted in ("dataclass", "dataclasses.dataclass")


def _module_dotted_name(path: Path) -> str:
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parent_map: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_map[child] = node
    return parent_map


def _scope_path(
    parent_map: dict[ast.AST, ast.AST], node: ast.AST, module_dotted: str
) -> str:
    """`node`'s own full dotted path (INCLUDING its own name), through
    every enclosing `ClassDef`/`FunctionDef`, rooted at the module --
    e.g. a `PhilosophyProfile` class -> `dba2.managers.philosophy.
    PhilosophyProfile`; a `reconstructs` method on `DecisionRecord` ->
    `dba2.managers.explain.DecisionRecord.reconstructs`."""
    parts = [node.name]
    current: ast.AST | None = parent_map.get(node)
    while current is not None:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(current.name)
        current = parent_map.get(current)
    parts.reverse()
    return ".".join([module_dotted, *parts])


def _class_field_order(class_def: ast.ClassDef) -> list[str]:
    """Field names in declaration order, from `AnnAssign` entries only --
    matches what `@dataclass` itself treats as a field (a bare `Assign`
    inside a dataclass body is not a field, per Python's own rules)."""
    return [
        stmt.target.id
        for stmt in class_def.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    ]


def _collect_dataclasses(py_files: list[Path]) -> dict[str, list[str]]:
    """Class name -> field order, for every `@dataclass`-decorated class
    ANYWHERE under `managers/` -- global, not per-module, since a
    dataclass-constructor call can reference a class declared in a
    different file than the one the call itself lives in (not exercised
    today, but §10's own rule ("a `Name` that resolves to a `ClassDef`
    under `managers/`") does not scope it to the same file)."""
    registry: dict[str, list[str]] = {}
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                _is_dataclass_decorator(d) for d in node.decorator_list
            ):
                registry.setdefault(node.name, _class_field_order(node))
    return registry


def _module_level_names(tree: ast.Module) -> set[str]:
    """Every name a module-level `Assign`/`AnnAssign` (with a value)
    declares, at THIS module's own top level."""
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
        ):
            names.add(stmt.target.id)
    return names


def _import_aliases(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """local alias -> (source module dotted name, original name), for
    every `from <module> import <name> [as <alias>]` in `tree` --
    absolute imports only (`node.level == 0`); confirmed by direct grep
    that no module under `managers/` uses a relative import, so
    `test_layering.py`'s own relative-import resolver is not needed
    here. Only used to resolve a bare `Name` that isn't a same-module
    declaration (§10's "Name referring to a module-level declaration,"
    read without an implicit same-file restriction the design's own text
    never states)."""
    aliases: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                local_name = alias.asname or alias.name
                aliases[local_name] = (node.module, alias.name)
    return aliases


def _collect_all_module_names(py_files: list[Path]) -> dict[str, set[str]]:
    """module dotted name -> that module's own `_module_level_names` --
    the global table cross-module `Name` resolution checks an imported
    alias's target against."""
    result: dict[str, set[str]] = {}
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        result[_module_dotted_name(path)] = _module_level_names(tree)
    return result


@dataclass(frozen=True)
class _CanonContext:
    """Everything `_canon_value` needs beyond the AST node itself --
    bundled so adding a resolution rule (like cross-module `Name`
    resolution, added on this dispatch's second pass) touches one
    parameter, not every call site's signature."""

    dataclass_fields: dict[str, list[str]]
    module_names: set[str]
    import_aliases: dict[str, tuple[str, str]]
    all_module_names: dict[str, set[str]]


# ---------------------------------------------------------------------------
# The canonicaliser
# ---------------------------------------------------------------------------


def _canon_value(node: ast.AST, ctx: _CanonContext):
    """Returns a canonical Python-ish structure (numeric/str/bool/None
    passthrough; `("tuple"|"list"|"set", (...))`; `("dict", ((k, v),
    ...))`; `("dataclass", class_name, ((field, value), ...))`; or a
    tagged string for `ref:`/`attr:`), or raises `_Unanalyzable`. Never
    itself produces the hash-hatch form -- that is `_canon_or_hatch`'s
    job, one level up, so a failure anywhere in a nested structure
    collapses the WHOLE top-level declaration rather than one field."""
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, (int, float, bool, str)) or value is None:
            return value
        raise _Unanalyzable

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _canon_value(node.operand, ctx)
        if isinstance(node.op, _UNARY_NUMERIC_OPS):
            operand = _canon_value(node.operand, ctx)
            if isinstance(operand, bool) or not isinstance(operand, (int, float)):
                raise _Unanalyzable
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return +operand
            return ~operand
        raise _Unanalyzable

    if isinstance(node, ast.BinOp) and type(node.op) in _BINOP_FUNCS:
        left = _canon_value(node.left, ctx)
        right = _canon_value(node.right, ctx)
        if _is_numeric(left) and _is_numeric(right):
            return _BINOP_FUNCS[type(node.op)](left, right)
        raise _Unanalyzable

    if isinstance(node, ast.Tuple):
        return ("tuple", tuple(_canon_value(e, ctx) for e in node.elts))
    if isinstance(node, ast.List):
        return ("list", tuple(_canon_value(e, ctx) for e in node.elts))
    if isinstance(node, ast.Set):
        return ("set", tuple(_canon_value(e, ctx) for e in node.elts))
    if isinstance(node, ast.Dict):
        pairs = []
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            if key_node is None:  # a `**spread` inside a dict literal
                raise _Unanalyzable
            pairs.append((_canon_value(key_node, ctx), _canon_value(value_node, ctx)))
        return ("dict", tuple(pairs))

    if isinstance(node, ast.Attribute):
        dotted = _flatten_dotted(node)
        if dotted is not None:
            return f"attr:{dotted}"
        raise _Unanalyzable

    if isinstance(node, ast.Name):
        if node.id in ctx.module_names:
            return f"ref:{node.id}"
        imported = ctx.import_aliases.get(node.id)
        if imported is not None:
            source_module, original_name = imported
            if original_name in ctx.all_module_names.get(source_module, ()):
                return f"ref:{source_module}.{original_name}"
        raise _Unanalyzable

    if isinstance(node, ast.Call):
        func_id = node.func.id if isinstance(node.func, ast.Name) else None
        if func_id in _WRAPPER_NAMES and not node.keywords:
            if len(node.args) == 1:
                return _canon_value(node.args[0], ctx)
            if len(node.args) == 0:
                return (_WRAPPER_EMPTY_KIND[func_id], ())
            raise _Unanalyzable
        if func_id == "field":
            return _canon_field_call(node, ctx)
        if func_id is not None and func_id in ctx.dataclass_fields:
            return _canon_dataclass_call(node, func_id, ctx)
        raise _Unanalyzable

    raise _Unanalyzable


def _canon_field_call(node: ast.Call, ctx: _CanonContext):
    """`field(default=X)` -> `canon(X)`; `field(default_factory=<lambda:
    X>)` -> `canon(X)`; `field(default_factory=<a resolvable Name>)` ->
    the same `Name` resolution `_canon_value` itself uses (`philosophy.
    py`'s own `group_tilt: ... = field(default_factory=lambda:
    _NO_TILT)` is the lambda-wrapped form); neither keyword present ->
    `"NO_DEFAULT"`."""
    default = None
    default_factory = None
    for kw in node.keywords:
        if kw.arg == "default":
            default = kw.value
        elif kw.arg == "default_factory":
            default_factory = kw.value
    if default is not None:
        return _canon_value(default, ctx)
    if default_factory is not None:
        if isinstance(default_factory, ast.Lambda):
            return _canon_value(default_factory.body, ctx)
        if isinstance(default_factory, ast.Name):
            return _canon_value(default_factory, ctx)
        raise _Unanalyzable
    return "NO_DEFAULT"


def _canon_dataclass_call(node: ast.Call, class_name: str, ctx: _CanonContext):
    """`PhilosophyProfile(kappa=6.0, loyalty=1.02, ...)` ->
    `("dataclass", "PhilosophyProfile", (("kappa", 6.0), ("loyalty",
    1.02), ...))` -- canonicalised exactly as if it were the dict literal
    it is, in the CLASS's own field order (not the call's argument
    order), positional args matched by index against that same order."""
    fields = ctx.dataclass_fields[class_name]
    values: dict[str, object] = {}
    for index, arg in enumerate(node.args):
        if index >= len(fields):
            raise _Unanalyzable
        values[fields[index]] = _canon_value(arg, ctx)
    for kw in node.keywords:
        if kw.arg is None:  # a `**spread`
            raise _Unanalyzable
        values[kw.arg] = _canon_value(kw.value, ctx)
    ordered = tuple((name, values[name]) for name in fields if name in values)
    return ("dataclass", class_name, ordered)


def _canon_repr(value) -> str:
    """The canonical structure `_canon_value` returns, rendered to a
    stable string for storage/diffing."""
    if isinstance(value, tuple) and len(value) == 2 and value[0] in ("tuple", "list", "set"):
        kind, elements = value
        return f"{kind}({', '.join(_canon_repr(e) for e in elements)})"
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "dict":
        _, items = value
        return f"dict({', '.join(f'{_canon_repr(k)}: {_canon_repr(v)}' for k, v in items)})"
    if isinstance(value, tuple) and len(value) == 3 and value[0] == "dataclass":
        _, class_name, fields = value
        return f"{class_name}({', '.join(f'{n}={_canon_repr(v)}' for n, v in fields)})"
    return repr(value)


def _canon_or_hatch(node: ast.AST, qualified_name: str, ctx: _CanonContext) -> str:
    try:
        value = _canon_value(node, ctx)
    except _Unanalyzable:
        source = ast.unparse(node)
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        return f"UNANALYZABLE:{qualified_name}:{digest}"
    return _canon_repr(value)


# ---------------------------------------------------------------------------
# Declaration sites (a)-(d)
# ---------------------------------------------------------------------------


def _module_entries(
    path: Path, dataclass_fields: dict[str, list[str]], all_module_names: dict[str, set[str]]
) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_dotted = _module_dotted_name(path)
    parent_map = _build_parent_map(tree)
    ctx = _CanonContext(
        dataclass_fields=dataclass_fields,
        module_names=all_module_names[module_dotted],
        import_aliases=_import_aliases(tree),
        all_module_names=all_module_names,
    )
    entries: dict[str, str] = {}

    # Site (a): module-level Assign/AnnAssign to a single Name.
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    qualified = f"{module_dotted}.{target.id}"
                    entries[qualified] = _canon_or_hatch(stmt.value, qualified, ctx)
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
        ):
            qualified = f"{module_dotted}.{stmt.target.id}"
            entries[qualified] = _canon_or_hatch(stmt.value, qualified, ctx)

    # Site (b): every class body (not only @dataclass-decorated ones).
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        class_qualified = _scope_path(parent_map, node, module_dotted)
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                qualified = f"{class_qualified}.{stmt.target.id}"
                annotation_source = ast.unparse(stmt.annotation)
                if stmt.value is None:
                    entries[qualified] = f"annotation={annotation_source}|default=NO_DEFAULT"
                else:
                    default_repr = _canon_or_hatch(stmt.value, qualified, ctx)
                    entries[qualified] = f"annotation={annotation_source}|default={default_repr}"
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        qualified = f"{class_qualified}.{target.id}"
                        value_repr = _canon_or_hatch(stmt.value, qualified, ctx)
                        entries[qualified] = f"annotation=|default={value_repr}"

    # Site (c): numeric/string/tuple defaults in any FunctionDef's
    # args.defaults/kw_defaults, at module or class scope.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        func_qualified = _scope_path(parent_map, node, module_dotted)
        positional = node.args.posonlyargs + node.args.args
        defaults = node.args.defaults
        offset = len(positional) - len(defaults)
        for i, default in enumerate(defaults):
            param = positional[offset + i]
            qualified = f"{func_qualified}.{param.arg}"
            entries[qualified] = _canon_or_hatch(default, qualified, ctx)
        for param, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
            if default is None:
                continue
            qualified = f"{func_qualified}.{param.arg}"
            entries[qualified] = _canon_or_hatch(default, qualified, ctx)

    return entries


def generate_inventory() -> dict[str, str]:
    """The full `dba2/managers/` inventory -- the same function backs
    both this test file's comparison and the mutation tests below, so
    they can never drift apart by construction (the same discipline
    `tests/fixtures/managers.py::generate_archetype_table` already uses
    for the golden value/pick/prospect tables)."""
    py_files = sorted(MANAGERS_DIR.rglob("*.py"))
    dataclass_fields = _collect_dataclasses(py_files)
    all_module_names = _collect_all_module_names(py_files)
    entries: dict[str, str] = {}
    for path in py_files:
        entries.update(_module_entries(path, dataclass_fields, all_module_names))
    return entries


# ---------------------------------------------------------------------------
# The standing test: the real tree matches the committed snapshot.
# ---------------------------------------------------------------------------


def test_constant_inventory_matches_committed_snapshot():
    actual = generate_inventory()
    expected = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    changed = sorted(
        k for k in (set(actual) & set(expected)) if actual[k] != expected[k]
    )
    assert not added and not removed and not changed, (
        "dba2/managers/ has drifted from the committed constant_inventory.json "
        "(Decision P3-30) -- regenerate it and confirm by eye that no VALUE "
        f"moved, only the inventory. added={added} removed={removed} "
        f"changed={[(k, expected[k], actual[k]) for k in changed]}"
    )


def test_constant_inventory_has_at_least_the_pre_phase_3_floor_of_152_entries():
    """§10's own floor for the pre-Phase-3 tree ("critic round 5's actual
    run produced 254 entries... larger than round 5's own >= 152 floor").
    This dispatch's own tree additionally carries Phase 3 step 4's six
    new `managers/`-scoped constants (`negotiation.py`/`memory.py`), so
    the real count here is well above 254 too -- asserted at the more
    conservative, design-stated floor rather than a guessed exact
    figure, per §10's own "the committed file's size is a step-4 output,
    not a design-time input.\""""
    assert len(generate_inventory()) >= 152


def test_profiles_and_window_params_descend_structurally_rather_than_hatch():
    """This dispatch's own item 1 -- verify the walker against the two
    real dict-of-dataclass-constructor tables `PROFILES`/`WINDOW_PARAMS`
    and confirm it descends into their values' keyword arguments rather
    than falling through to the hash hatch."""
    entries = generate_inventory()
    profiles = entries["dba2.managers.philosophy.PROFILES"]
    window_params = entries["dba2.managers.tuning.WINDOW_PARAMS"]
    assert not profiles.startswith("UNANALYZABLE")
    assert not window_params.startswith("UNANALYZABLE")
    assert "kappa=6.0" in profiles
    assert "loyalty=1.02" in profiles
    assert "ref:_DEFENSE_FIRST_TILT" in profiles or "group_tilt" in profiles
    assert "gamma=0.93" in window_params


def test_rookie_scale_table_type_alias_lands_in_the_hash_hatch():
    """§10's nit 10: `RookieScaleTable = Mapping[int, RookieScale]` is
    swept in by site (a) (unfiltered by type) and reaches the hatch via
    its `Subscript` value, which has no canonicaliser rule -- expected
    and harmless, named here so a reader of the committed file is not
    left guessing."""
    entries = generate_inventory()
    assert entries["dba2.managers.picks.RookieScaleTable"].startswith(
        "UNANALYZABLE:dba2.managers.picks.RookieScaleTable:"
    )


def test_cross_module_name_reference_resolves_instead_of_hatching():
    """Critic finding B1 on this dispatch's first pass: a `Name` whose
    declaration lives in a DIFFERENT `managers/` module (e.g. `tuning:
    GmTuning = DEFAULT_TUNING`, where `DEFAULT_TUNING` is declared in
    `tuning.py` but referenced as a bare imported name in `picks.py`/
    `trade.py`/every other consumer) must resolve to a qualified `ref:`,
    not fall through to the hash hatch -- §10's own canonicaliser table
    says "a module-level declaration," not "a declaration in the SAME
    module," and the design's own examples (`_NO_TILT`, `_DEFENSE_FIRST_
    TILT`) just happen to both be same-file, which is not the same claim
    as the rule being scoped to same-file."""
    entries = generate_inventory()
    # _canon_repr's fallback is plain repr(), so a tagged string comes back
    # single-quoted -- the same convention "ref:_DEFENSE_FIRST_TILT" already
    # carries inside PROFILES' own entry.
    key = "dba2.managers.picks.pick_value.tuning"
    assert entries[key] == "'ref:dba2.managers.tuning.DEFAULT_TUNING'"
    key2 = "dba2.managers.decisions.trade.evaluate_trade.tuning"
    assert entries[key2] == "'ref:dba2.managers.tuning.DEFAULT_TUNING'"


def test_zero_argument_wrapper_call_canonicalises_to_the_empty_container():
    """Critic finding B1 on this dispatch's first pass: `already_cut:
    frozenset[int] = frozenset()` (`decisions/waivers.py`) is a
    zero-argument wrapper call -- §10's own text ("recurse into the
    single argument") was written against the one-argument case and
    doesn't say what to do at zero, but falling through to the hatch for
    an empty-container construction throws away real structure a
    canonicaliser is supposed to capture (there IS a value here: the
    empty set)."""
    entries = generate_inventory()
    key = "dba2.managers.decisions.waivers.select_waive_candidate.already_cut"
    assert entries[key] == "set()"


def test_constant_inventory_hatch_population_is_pinned_at_its_measured_size():
    """§10's own text sizes the `ast.unparse`-version-instability risk
    against "the five declarations currently in the hatch" (§11 step 14
    added the fifth, `PhraseLibrary` -- the design doc's own amendment to
    Decision P3-30 explains why this addition needs no `GM_TUNING_VERSION`
    bump) -- that claim is a measurement, not a design constant, and this
    test is what keeps it honest against the REAL, current tree rather
    than a number typed into a docstring once and never re-checked
    (exactly the failure mode this whole file exists to prevent, turned on
    itself). Lists every member so a future change to the population is a
    reviewed diff."""
    entries = generate_inventory()
    hatched = sorted(k for k, v in entries.items() if v.startswith("UNANALYZABLE:"))
    assert hatched == [
        "dba2.managers.picks.RookieScaleTable",
        "dba2.managers.picks._ENTRY_ANCHORS",
        "dba2.managers.picks._PEAK_ANCHORS",
        "dba2.managers.voice.select.PhraseLibrary",
        "dba2.managers.window._RAW_INPUT_FIELDS",
    ], (
        f"hatch population changed (now {len(hatched)}, listing {hatched}) -- either a "
        "real new UNANALYZABLE declaration was added (fine, update this list after "
        "confirming by eye) or the canonicaliser regressed and started hatching "
        "something it used to resolve structurally (not fine -- investigate first)"
    )


# ---------------------------------------------------------------------------
# Mutation testing -- Decision P3-30's eight named mutations. Each
# temporarily edits a REAL file under managers/ (or adds/removes a
# scratch file), regenerates the inventory, and restores the original
# content in a `finally` block -- the same "write a deliberately-
# violating scratch file, confirm the test fails, clean up" pattern
# `tests/unit/test_layering.py::test_relative_imports_are_resolved_and_
# caught` already uses for the identical reason (HANDOFF: "a real gap in
# the AST import-checker survived one full build+critic round before
# being caught this way").
# ---------------------------------------------------------------------------

PHILOSOPHY_PATH = MANAGERS_DIR / "philosophy.py"
TUNING_PATH = MANAGERS_DIR / "tuning.py"
WINDOW_PATH = MANAGERS_DIR / "window.py"
_SCRATCH_PATH = MANAGERS_DIR / "_constant_inventory_mutation_scratch.py"

# The mutation tests below rewrite the REAL `tuning.py`/`window.py`/`philosophy.py` on disk
# (and write a scratch module beside them) while the reading tests walk the same directory,
# so two of this file's tests running at once on different pytest-xdist workers corrupt each
# other: a reader sees a mutated constant, and two mutators interleaving can "restore" the
# OTHER one's mutation. The module's `xdist_group("source-tree")` mark is what prevents it:
# every test of this file, and every other test that writes scratch files into `dba2/` or
# scans it, runs on ONE worker, serially (`--dist loadgroup`, enforced by `tests/conftest.py`).
# This file used to carry an `O_EXCL` lock-file fixture for the same job across workers; it
# was machine-global, its stale-break was racy and it guarded this file only, so it is gone.


@contextlib.contextmanager
def _mutate_file(path: Path, *substitutions: tuple[str, str]):
    """Applies each `(old, new)` substitution in sequence to `path`'s
    real, on-disk content, asserting `old` appears exactly once (so a
    substitution can never silently touch the wrong site), then restores
    the original content on exit regardless of what happens inside the
    `with` block."""
    original = path.read_text(encoding="utf-8")
    mutated = original
    for old, new in substitutions:
        count = mutated.count(old)
        assert count == 1, f"{path}: expected exactly one occurrence of {old!r}, found {count}"
        mutated = mutated.replace(old, new)
    path.write_text(mutated, encoding="utf-8")
    try:
        yield
    finally:
        path.write_text(original, encoding="utf-8")


def test_mutation_1_add_a_module_level_scalar_is_detected():
    baseline = generate_inventory()
    _SCRATCH_PATH.write_text(
        "SCRATCH_CONSTANT_INVENTORY_MUTATION: float = 1.0\n", encoding="utf-8"
    )
    try:
        mutated = generate_inventory()
    finally:
        _SCRATCH_PATH.unlink()
    assert mutated != baseline
    key = "dba2.managers._constant_inventory_mutation_scratch.SCRATCH_CONSTANT_INVENTORY_MUTATION"
    assert mutated.get(key) == "1.0"
    assert key not in baseline


def test_mutation_2_change_one_number_inside_profiles_is_detected():
    """Round 4's own named mutation, which round 4's own walker could not
    see (§10's history) -- `PROFILES`' `kappa=6.0` -> `6.1`."""
    baseline = generate_inventory()
    with _mutate_file(
        PHILOSOPHY_PATH,
        ("kappa=6.0, upside_weight=0.55", "kappa=6.1, upside_weight=0.55"),
    ):
        mutated = generate_inventory()
    key = "dba2.managers.philosophy.PROFILES"
    assert mutated[key] != baseline[key]
    # The diff names the specific field that moved, not just a hash.
    assert "kappa=6.1" in mutated[key]
    assert "kappa=6.0" in baseline[key]


def test_mutation_3_change_one_number_inside_window_params_is_detected():
    baseline = generate_inventory()
    with _mutate_file(
        TUNING_PATH,
        ("gamma=0.93, window_upside=1.40", "gamma=0.94, window_upside=1.40"),
    ):
        mutated = generate_inventory()
    key = "dba2.managers.tuning.WINDOW_PARAMS"
    assert mutated[key] != baseline[key]
    assert "gamma=0.94" in mutated[key]
    assert "gamma=0.93" in baseline[key]


def test_mutation_4_change_one_gmtuning_field_default_is_detected():
    baseline = generate_inventory()
    with _mutate_file(
        TUNING_PATH,
        ("vp_per_cap_season: float = 1000.0", "vp_per_cap_season: float = 1000.1"),
    ):
        mutated = generate_inventory()
    key = "dba2.managers.tuning.GmTuning.vp_per_cap_season"
    assert mutated[key] != baseline[key]
    assert "1000.1" in mutated[key]
    assert "1000.0" in baseline[key]


def test_mutation_5_unaryop_change_abs_guard_score_floor_is_detected():
    """The `UnaryOp` case: `_ABS_GUARD_SCORE_FLOOR = -0.90` -> `-0.91`."""
    baseline = generate_inventory()
    with _mutate_file(
        WINDOW_PATH, ("_ABS_GUARD_SCORE_FLOOR = -0.90", "_ABS_GUARD_SCORE_FLOOR = -0.91")
    ):
        mutated = generate_inventory()
    key = "dba2.managers.window._ABS_GUARD_SCORE_FLOOR"
    assert baseline[key] == "-0.9"
    assert mutated[key] == "-0.91"
    assert mutated[key] != baseline[key]


def test_mutation_6_binop_change_and_its_inverse_rewrite():
    """The `BinOp` case: `_CONTEND_FRACTION = 6 / 30` -> `7 / 30` MUST
    fail (differ); rewriting `6 / 30` as the literal `0.2` MUST pass
    (canonicalise identically), since the constant did not change --
    Python's `/` gives `6 / 30` and `0.2` the identical nearest-double,
    so this is a real identity, not an approximation."""
    baseline = generate_inventory()
    key = "dba2.managers.window._CONTEND_FRACTION"
    assert baseline[key] == "0.2"

    with _mutate_file(WINDOW_PATH, ("_CONTEND_FRACTION = 6 / 30", "_CONTEND_FRACTION = 7 / 30")):
        mutated_forward = generate_inventory()
    assert mutated_forward[key] != baseline[key]
    assert mutated_forward[key] == "0.23333333333333334"

    with _mutate_file(WINDOW_PATH, ("_CONTEND_FRACTION = 6 / 30", "_CONTEND_FRACTION = 0.2")):
        mutated_inverse = generate_inventory()
    assert mutated_inverse[key] == baseline[key] == "0.2"
    assert mutated_inverse == baseline, (
        "rewriting 6/30 as the literal 0.2 must leave the ENTIRE inventory "
        "unchanged, not only this one key"
    )


def test_mutation_7_add_patience_field_to_philosophyprofile_fails_on_both_counts():
    """Decision P3-44 ruled this exact field is NOT added to
    `PhilosophyProfile` -- this test exercises the guard against the
    hypothetical anyway, since §14 question 1's proposal is Decision
    P3-30's own worked example of why the dataclass-constructor rule
    matters: round 3/4's walkers missed both the new dataclass FIELD and
    the seven new numbers inside `PROFILES` that a real addition would
    carry."""
    baseline = generate_inventory()
    loyalty_lines = (
        "pick_pref=0.80, vet_bias=1.00, risk=1.20, loyalty=1.02,",
        "pick_pref=1.10, vet_bias=1.00, risk=0.85, loyalty=1.10,",
        "pick_pref=1.00, vet_bias=1.00, risk=0.95, loyalty=1.06,",
        "pick_pref=1.00, vet_bias=1.00, risk=1.00, loyalty=1.06,",
        "pick_pref=0.75, vet_bias=1.35, risk=0.90, loyalty=1.15,",
        "pick_pref=1.25, vet_bias=0.70, risk=1.15, loyalty=1.05,",
        "pick_pref=1.00, vet_bias=1.00, risk=1.60, loyalty=1.00,",
    )
    substitutions = [(line, f"{line} patience=0.5,") for line in loyalty_lines]
    substitutions.append(
        (
            "    group_tilt: Mapping[str, float] = field(default_factory=lambda: _NO_TILT)",
            "    group_tilt: Mapping[str, float] = field(default_factory=lambda: _NO_TILT)\n"
            "    patience: float = 0.5",
        )
    )
    with _mutate_file(PHILOSOPHY_PATH, *substitutions):
        mutated = generate_inventory()

    field_key = "dba2.managers.philosophy.PhilosophyProfile.patience"
    profiles_key = "dba2.managers.philosophy.PROFILES"
    # Count 1: the new dataclass field itself.
    assert field_key not in baseline
    assert field_key in mutated
    # Count 2: the seven new numbers inside PROFILES.
    assert mutated[profiles_key] != baseline[profiles_key]
    assert mutated[profiles_key].count("patience=0.5") == 7


def test_mutation_8_escape_hatch_changes_on_content_and_is_stable_under_reformat():
    """The escape hatch's own test: add a module-level constant whose
    value the canonicaliser cannot fold, then change that expression --
    must fail (the hatch's hash must move). Separately, confirm a pure
    reformat/comment edit -- semantically identical, byte-different
    source -- does NOT move the hatch's hash, since `ast.unparse`
    normalises both away."""
    baseline_source = "SCRATCH_HATCH: tuple[int, ...] = tuple(x for x in range(3))\n"
    changed_source = "SCRATCH_HATCH: tuple[int, ...] = tuple(x for x in range(4))\n"
    reformatted_source = (
        "SCRATCH_HATCH: tuple[int, ...] = tuple(\n"
        "    x for x in range(3)\n"  # reflowed across lines, no comment
        ")  # a harmless comment, dropped entirely by ast.unparse\n"
    )
    key = "dba2.managers._constant_inventory_mutation_scratch.SCRATCH_HATCH"

    _SCRATCH_PATH.write_text(baseline_source, encoding="utf-8")
    try:
        baseline = generate_inventory()
    finally:
        _SCRATCH_PATH.unlink()
    assert baseline[key].startswith("UNANALYZABLE:")

    _SCRATCH_PATH.write_text(changed_source, encoding="utf-8")
    try:
        changed = generate_inventory()
    finally:
        _SCRATCH_PATH.unlink()
    assert changed[key] != baseline[key], "changing range(3) -> range(4) must move the hatch hash"

    _SCRATCH_PATH.write_text(reformatted_source, encoding="utf-8")
    try:
        reformatted = generate_inventory()
    finally:
        _SCRATCH_PATH.unlink()
    assert reformatted[key] == baseline[key], (
        "a pure reformat/comment edit must NOT move the hatch hash -- "
        "ast.unparse normalises both away"
    )


# ---------------------------------------------------------------------------
# Regeneration helper -- not a test. Run directly
# (`python -m tests.unit.managers.test_constant_inventory`) to rewrite
# constant_inventory.json from the live tree after a legitimate
# managers/ constant change (which must also bump GM_TUNING_VERSION).
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    inventory = generate_inventory()
    INVENTORY_PATH.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {INVENTORY_PATH} ({len(inventory)} entries)")
