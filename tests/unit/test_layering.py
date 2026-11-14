"""Decision 4 (phase-0-foundation.md §3), the full rule -- this file's own
name is the one Decision 4 itself specifies: "layering is enforced by a
test, not by convention. A `tests/unit/test_layering.py` walks the package
with stdlib `ast` and asserts: `domain` imports nothing from `dba2` except
`core`; `data` imports `domain`/`core` only; `services` may import
`data`/`domain`/`core`/`phase`/`sim`/`managers`; `sim` may not import
`data`; `managers` may not import `data`; only `bot` and `tools` may
import `services` from outside."

`test_domain_layering.py` already covers the `domain` clause (the strongest
form: importable with asyncpg uninstalled, plus this same AST technique).
This file covers the other clauses and does not re-check or duplicate the
domain one.

`sim` in the services allowlist: Decision 12 requires exactly that edge
(`ratings_service.recompute_overall` -> `sim.ratings.compute_overall`) and
it introduces no cycle (`sim` never imports `services`). Decision 4's own
bullet list in the design doc was amended to say so during Phase 0 step 9's
critic review, so this is no longer a discrepancy between the doc and the
code -- see phase-0-foundation.md §3, Decision 4.

`managers` in the services allowlist: Decision P2-2(a) (phase-2-manager-
minds.md §3, Gap A) requires exactly that edge (`gm_service` -> `managers/`,
not built this dispatch) and it introduces no cycle (`managers` never
imports `services`). Decision P2-2(b) additionally gives `managers` the
same denylist and purity rules `sim` already carries (no `dba2.data`, no
`datetime.now()`, no bare `random.Random()`, no `open()`, no
`os.environ`), plus one rule `sim` does not need: `managers` may import
`dba2.sim` (it needs `sim.ratings` and `sim.derivation`'s constants) but
never `sim.game`, `sim.possession`, or anything under `sim/beats/` -- a GM
does not run a simulation to make a decision.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# This file writes scratch files into `dba2/` or scans it (`tests/conftest.py`): one xdist
# group keeps every such writer and reader on one worker, run serially.
pytestmark = pytest.mark.xdist_group("source-tree")

REPO_ROOT = Path(__file__).resolve().parents[2]
DBA2_DIR = REPO_ROOT / "dba2"
DATA_DIR = DBA2_DIR / "data"
SERVICES_DIR = DBA2_DIR / "services"
SIM_DIR = DBA2_DIR / "sim"
MANAGERS_DIR = DBA2_DIR / "managers"
BOT_DIR = DBA2_DIR / "bot"
TOOLS_DIR = REPO_ROOT / "tools"
# Decision P4-60 (phase-4-media-and-player-agency.md §11 Stage B step 5):
# the two new Phase 4 pure packages. `agency/` gets real content this
# stage; `media/` stays the empty Phase 0 scaffold until Stage D, but its
# own layering rule (P4-31's one-way valve) lands now, in the SAME step as
# the `_SERVICES_ALLOWED_PREFIXES` widening below -- P4-60's own
# reasoning: widening an allowlist needs its compensating denylist written
# in the same step, never after it.
AGENCY_DIR = DBA2_DIR / "agency"
MEDIA_DIR = DBA2_DIR / "media"
# Phase 5 Stage A step 4 (phase-5-discord-presentation.md §11, Decisions
# P5-1/P5-3): the fifth pure package, behind a fifth service
# (`view_service`, not built this step). Its compensating denylist (the
# `discord`-import valve) lands in the SAME step below, per Decision
# P4-60's "widening an allowlist needs its compensating denylist written
# in the same step, never after it" -- the same rule P4-60 itself cites.
PRESENTATION_DIR = DBA2_DIR / "presentation"

# services -> sim: see module docstring (Decision 12's required edge).
# services -> managers: see module docstring (Decision P2-2(a)'s required edge).
# services -> agency/media: Decision P4-60 -- `agency_service.py` needs
# `dba2.agency` (P4-8/P4-9/P4-13) and `media_service.py` needs `dba2.media`
# (P4-31, which names it the ONE permitted importer -- enforced below by
# `test_nothing_but_media_service_imports_media`, not by this allowlist
# alone).
# services -> presentation: Phase 5 Stage B step 7 (phase-5-discord-
# presentation.md §11, Decision P5-3) -- `view_service.py` needs `dba2.
# presentation` to turn real repository/service reads into `Panel`s, the
# identical "one named importer" shape `media_service.py` already has for
# `dba2.media`, enforced below by `test_nothing_but_view_service_imports_
# presentation`, not by this allowlist alone. Its compensating denylist
# (that test, plus its mutation test) lands in the SAME step as this
# widening, per Decision P4-60's own rule.
_SERVICES_ALLOWED_PREFIXES = (
    "dba2.services", "dba2.data", "dba2.domain", "dba2.core", "dba2.phase", "dba2.sim",
    "dba2.managers", "dba2.agency", "dba2.media", "dba2.presentation",
)
_DATA_ALLOWED_PREFIXES = ("dba2.data", "dba2.domain", "dba2.core")
_SIM_FORBIDDEN_PREFIXES = ("dba2.data",)
# Decision P2-2(b): managers may not import data, and may not reach into
# the simulation engine's internals (sim.ratings/sim.derivation only).
_MANAGERS_FORBIDDEN_PREFIXES = (
    "dba2.data", "dba2.sim.game", "dba2.sim.possession", "dba2.sim.beats",
)
# Decision P4-8: `dba2/agency/` is a new pure package with the identical
# rule set `managers/` carries -- no `dba2.data`, and it may read
# `dba2.managers` as TYPES (`NeedMatrix`/`CompetitiveWindow`) but never
# reach into the sim engine's internals, same denylist shape as managers.
_AGENCY_ALLOWED_PREFIXES = ("dba2.agency", "dba2.domain", "dba2.core", "dba2.managers")
_AGENCY_FORBIDDEN_PREFIXES = (
    "dba2.data", "dba2.sim.game", "dba2.sim.possession", "dba2.sim.beats",
)
# Decision P4-8: the directed rule -- `managers/` may never import
# `dba2.agency` (a GM valuation reading a player's mood would put player
# agency inside the byte-identity digest's blast radius through the back
# door).
_MANAGERS_AGENCY_FORBIDDEN_PREFIXES = ("dba2.agency",)
# Decision P4-31: `dba2/media/` may import `domain`/`core`/`managers`
# (types) and `agency` (types); nothing under `dba2/` may import
# `dba2.media` except `dba2/services/media_service.py`.
_MEDIA_ALLOWED_PREFIXES = (
    "dba2.media", "dba2.domain", "dba2.core", "dba2.managers", "dba2.agency",
)
_MEDIA_SERVICE_PATH = SERVICES_DIR / "media_service.py"
_VIEW_SERVICE_PATH = SERVICES_DIR / "view_service.py"
# Decision P4-52: `dba2/agency/` never imports `managers/decisions/
# signing.py` -- the one enforced non-goal keeping free-agent negotiation
# machinery out (§12's "single most likely creep").
_AGENCY_SIGNING_FORBIDDEN_PREFIXES = ("dba2.managers.decisions.signing",)
# Decision P5-3: `dba2/presentation/` may import `dba2.domain`, `dba2.core`
# and "dba2.sim's pure event types only" -- `BeatRecord`/`GameResult`/
# `PlayerBoxLine` all live in `dba2/sim/events.py` and nowhere else, so the
# allowlist is scoped to that one module rather than to `dba2.sim` broadly
# (which would also admit `sim.game`/`sim.possession`/`sim.beats`, exactly
# the imperative engine internals P5-3 does not open the door to).
_PRESENTATION_ALLOWED_PREFIXES = (
    "dba2.presentation", "dba2.domain", "dba2.core", "dba2.sim.events",
)


def _real_top_level_dba2_packages() -> tuple[str, ...]:
    """Every real top-level package directory under `dba2/` (has an
    `__init__.py`) -- listed off the filesystem, not hand-maintained, so a
    future new top-level package (e.g. a Phase 7 rollover package) is
    automatically covered by the mutation tests below without anyone
    remembering to add it by name.

    Critic re-verification of the round-2 fix (§42.1's Blocking Issue 1):
    the exemption-widening mutation tests for the `discord`-import and
    `dba2.services` one-way valves each only ever planted their scratch
    violation in two pre-chosen packages (`dba2/sim/`, `dba2/managers/`),
    so they proved a widened exemption is caught IF it happens to widen to
    exactly those two -- not the general claim their own docstrings made
    ("or anywhere else"). Live repro: widening `_discord_import_offenders`'s
    exemption to also admit `dba2/agency/`, with a real `import discord`
    planted there, left the full suite green; the identical shape widening
    `_services_one_way_valve_offenders` to admit `dba2/phase/` did too.
    This helper lets each valve's mutation test parametrize over every real
    candidate TOP-LEVEL package instead of two. (Scoped to top-level
    packages only -- critic round 4, nit N2: this only scans `dba2/*/`,
    so a widening to a real SUB-package, e.g. `dba2/data/repositories/`,
    is not caught by any parametrization built on this helper and was
    never claimed to be.)"""
    return tuple(
        sorted(
            p.name
            for p in DBA2_DIR.iterdir()
            if p.is_dir() and (p / "__init__.py").exists()
        )
    )


def _module_dotted_name(path: Path) -> str:
    """The fully dotted module name `path` would have if imported, e.g.
    `dba2/sim/ratings.py` -> `dba2.sim.ratings`, `dba2/sim/__init__.py` ->
    `dba2.sim`."""
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package_for(path: Path) -> str:
    """The `__package__` a relative import inside `path` resolves against
    -- itself for a package's `__init__.py`, its parent otherwise. Needed
    to resolve `from ..data.repositories import x`-style relative imports
    (see `_resolve_relative` below)."""
    if path.name == "__init__.py":
        return _module_dotted_name(path)
    dotted = _module_dotted_name(path)
    return dotted.rsplit(".", 1)[0] if "." in dotted else ""


def _resolve_relative(module: str | None, level: int, package: str) -> str:
    """Mirrors `importlib._bootstrap._resolve_name`'s algorithm: `from
    ..data.repositories import x` (level=2) inside a module whose package
    is `dba2.sim` resolves to `dba2.data.repositories`, exactly as Python
    itself resolves it at runtime."""
    bits = package.rsplit(".", level - 1)
    base = bits[0]
    return f"{base}.{module}" if module else base


def _dba2_imports_in_file(path: Path) -> list[str]:
    """Every `dba2...`-rooted module named by an import statement in
    `path`, absolute or relative -- the same AST-walk technique
    `test_domain_layering.py::test_domain_module_source_imports_nothing_from_dba2_except_core`
    already uses, generalized to return the full list of hits instead of
    filtering inline (each caller below applies its own allow/deny rule).

    Relative imports (`from ..data.repositories import x`) are resolved to
    their absolute dotted name before the `dba2` prefix check -- Phase 0
    step-9 critic round, B1: the earlier version only matched
    `node.module.startswith("dba2")`, which a relative `ImportFrom` never
    does (its `node.module` is the unqualified tail, e.g. `data.repositories`,
    with the leading dots carried in `node.level` instead), so every
    relative import silently bypassed every rule in this file. Reproduced
    live: `from ..data.repositories import player_repo` inside `dba2/sim/`
    resolved and ran at runtime but was invisible to every test here."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package_for(path)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            resolved = (
                _resolve_relative(node.module, node.level, package)
                if node.level
                else node.module
            )
            if resolved and resolved.startswith("dba2"):
                modules.append(resolved)
                # `from dba2.sim import game` names dba2.sim.game just as much as
                # `import dba2.sim.game` does -- Phase 2 critic round 6, B1: the
                # earlier version only recorded `resolved` (here, "dba2.sim"), so
                # a denylist rule for "dba2.sim.game" never matched this spelling.
                # Reproduced live: `from dba2.sim import game` inside
                # `dba2/managers/` evaded Decision P2-2(b) entirely.
                for alias in node.names:
                    if alias.name != "*":
                        modules.append(f"{resolved}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("dba2"):
                    modules.append(alias.name)
    return modules


def _matches_prefix(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _offenders_outside_allowlist(
    py_files: list[Path], allowed_prefixes: tuple[str, ...]
) -> list[str]:
    offenders: list[str] = []
    for path in py_files:
        for module in _dba2_imports_in_file(path):
            if not any(_matches_prefix(module, p) for p in allowed_prefixes):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: import {module}")
    return offenders


def _offenders_on_denylist(py_files: list[Path], forbidden_prefixes: tuple[str, ...]) -> list[str]:
    offenders: list[str] = []
    for path in py_files:
        for module in _dba2_imports_in_file(path):
            if any(_matches_prefix(module, p) for p in forbidden_prefixes):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: import {module}")
    return offenders


def test_data_module_source_imports_only_domain_and_core():
    """"`data` imports `domain`/`core` only" -- `dba2.data` itself
    (peer imports, e.g. `records_repo` importing from `stats_repo`, both
    under `data/repositories/`) is allowed alongside `domain`/`core`."""
    py_files = sorted(DATA_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {DATA_DIR} -- path wrong?"
    offenders = _offenders_outside_allowlist(py_files, _DATA_ALLOWED_PREFIXES)
    assert not offenders, f"data/ imports outside domain/core/data: {offenders}"


def test_services_module_source_imports_only_data_domain_core_phase_sim():
    """"`services` may import `data`/`domain`/`core`/`phase`/`sim`" (see
    module docstring for why `sim` is included) -- `dba2.services` itself
    is allowed for one service composing another."""
    py_files = sorted(SERVICES_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {SERVICES_DIR} -- path wrong?"
    offenders = _offenders_outside_allowlist(py_files, _SERVICES_ALLOWED_PREFIXES)
    assert not offenders, f"services/ imports outside its allowed layers: {offenders}"


def test_sim_module_source_never_imports_data():
    """"`sim` may not import `data`" -- a denylist, not an allowlist
    (unlike `data`/`services` above): `sim` may still import `domain`/
    `core`/itself/anything else this rule doesn't name; only `dba2.data` is
    forbidden."""
    py_files = sorted(SIM_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {SIM_DIR} -- path wrong?"
    offenders = _offenders_on_denylist(py_files, _SIM_FORBIDDEN_PREFIXES)
    assert not offenders, f"sim/ imports dba2.data, violating Decision 4: {offenders}"


def test_managers_module_source_never_imports_data_or_sim_engine_internals():
    """Decision P2-2(b): `managers` may not import `dba2.data`, and may
    not import `sim.game`/`sim.possession`/anything under `sim/beats/` --
    a GM does not run a simulation to make a decision. `managers` may
    still import `dba2.sim` itself (`sim.ratings`, `sim.derivation`) and
    everything else this rule doesn't name."""
    py_files = sorted(MANAGERS_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {MANAGERS_DIR} -- path wrong?"
    offenders = _offenders_on_denylist(py_files, _MANAGERS_FORBIDDEN_PREFIXES)
    assert not offenders, (
        f"managers/ imports a forbidden module, violating Decision P2-2(b): {offenders}"
    )


# ---------------------------------------------------------------------------
# Decision P4-8/P4-31/P4-52/P4-60 -- the two new Phase 4 pure packages
# ---------------------------------------------------------------------------


def test_agency_module_source_imports_only_domain_core_and_managers():
    """Decision P4-8: `dba2/agency/` may import `dba2.domain`, `dba2.core`,
    and `dba2.managers` (types only -- `NeedMatrix`/`CompetitiveWindow`) --
    nothing else under `dba2`."""
    py_files = sorted(AGENCY_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {AGENCY_DIR} -- path wrong?"
    offenders = _offenders_outside_allowlist(py_files, _AGENCY_ALLOWED_PREFIXES)
    assert not offenders, f"agency/ imports outside its allowed layers: {offenders}"


def test_agency_module_source_never_imports_data_or_sim_engine_internals():
    """Decision P4-8: same denylist `managers/` carries -- no `dba2.data`,
    no reaching into the sim engine's internals."""
    py_files = sorted(AGENCY_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {AGENCY_DIR} -- path wrong?"
    offenders = _offenders_on_denylist(py_files, _AGENCY_FORBIDDEN_PREFIXES)
    assert not offenders, (
        f"agency/ imports a forbidden module, violating Decision P4-8: {offenders}"
    )


def test_managers_module_source_never_imports_agency():
    """Decision P4-8's DIRECTED rule: `dba2.managers` may never import
    `dba2.agency` -- a GM valuation that reads a player's mood would put
    player agency inside the byte-identity digest's blast radius through
    the back door."""
    py_files = sorted(MANAGERS_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {MANAGERS_DIR} -- path wrong?"
    offenders = _offenders_on_denylist(py_files, _MANAGERS_AGENCY_FORBIDDEN_PREFIXES)
    assert not offenders, (
        f"managers/ must never import dba2.agency, violating Decision P4-8: {offenders}"
    )


def test_agency_module_source_never_imports_signing():
    """Decision P4-52: `dba2/agency/` never imports `managers/decisions/
    signing.py` -- the enforced non-goal keeping multi-round free-agent
    negotiation machinery out (§12: "the single most likely creep")."""
    py_files = sorted(AGENCY_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {AGENCY_DIR} -- path wrong?"
    offenders = _offenders_on_denylist(py_files, _AGENCY_SIGNING_FORBIDDEN_PREFIXES)
    assert not offenders, (
        f"agency/ must never import managers/decisions/signing.py, violating "
        f"Decision P4-52: {offenders}"
    )


def test_media_module_source_imports_only_domain_core_managers_and_agency():
    """Decision P4-31: `dba2/media/` may import `domain`/`core`/`managers`
    (types) and `agency` (types). `dba2/media/` is still the empty Phase 0
    scaffold at this stage (Stage D builds real content) -- this rule
    lands now anyway, per P4-60's own reasoning: the compensating control
    for the allowlist widening below must land in the same step as the
    widening, never after it."""
    py_files = sorted(MEDIA_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {MEDIA_DIR} -- path wrong?"
    offenders = _offenders_outside_allowlist(py_files, _MEDIA_ALLOWED_PREFIXES)
    assert not offenders, f"media/ imports outside its allowed layers: {offenders}"


def _media_one_way_valve_offenders(py_files: list[Path]) -> list[str]:
    """The REAL check `test_nothing_but_media_service_imports_media` runs,
    extracted so its own mutation test below (`test_media_one_way_valve_
    is_mutation_tested`) exercises this exact function rather than a
    re-implementation of it -- the identical B4 defect the presentation
    valve's own `_presentation_one_way_valve_offenders` extraction fixed,
    left standing here (Blocking Issue B, critic re-verification of the
    Stage C fix round): the mutation test that shipped with the B4 fix
    re-implemented its own inline offender loop for THIS valve, so it
    proved nothing about whether the ACTUAL production check (the one
    `test_nothing_but_media_service_imports_media` runs) still had teeth."""
    offenders: list[str] = []
    for path in py_files:
        if path == _MEDIA_SERVICE_PATH or MEDIA_DIR in path.parents:
            continue
        for module in _dba2_imports_in_file(path):
            if _matches_prefix(module, "dba2.media"):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: import {module}")
    return offenders


def test_nothing_but_media_service_imports_media():
    """Decision P4-31's one-way valve: nothing under `dba2/` may import
    `dba2.media` except `dba2/services/media_service.py`. After Decision
    P4-60 widens `_SERVICES_ALLOWED_PREFIXES` to admit `dba2.media` for
    every file under `services/`, THIS denylist is the only thing still
    enforcing "one permitted importer" -- it stops being belt-and-braces
    and becomes load-bearing (P4-60's own note).

    **Round 10 introduced, and round 11 withdrew, a `tools/` widening
    here (Decision P4-86, withdrawn by P4-87 -- see docs/design/phase-4-
    media-and-player-agency.md §25).** Round 10 widened this scan to
    `TOOLS_DIR` and added `tools/gm_harness/state.py` as a second
    permitted importer, for a `HarnessState.media_pulse_snapshot` field
    that turned out to have zero readers, zero writers, and zero test
    references anywhere in the repo. Round 11's re-verification deleted
    that dead field (and its now-unneeded `from dba2.media.pulse import
    PulseSnapshot`), which removed the only real import `tools/` ever had
    of `dba2.media` -- so this scan and its scope are back to `DBA2_DIR`
    only, exactly as P4-31 originally specified. Widen it again, with a
    real mutation test proving the widened half has teeth, only if a
    real, load-bearing `tools/` consumer of `dba2.media` actually shows
    up (e.g. when step 18's persona/selection engine gets wired into
    `tools/gm_harness/season.py`'s cycle loop)."""
    offenders = _media_one_way_valve_offenders(sorted(DBA2_DIR.rglob("*.py")))
    assert not offenders, f"only dba2/services/media_service.py may import dba2.media: {offenders}"


def test_agency_layering_rule_is_mutation_tested(tmp_path):
    """HANDOFF's standing rule: "if a future layering rule seems to have
    'no teeth,' mutation-test it before trusting it" -- a real gap in the
    AST import-checker once survived a full build+critic round before
    being caught this way. Writes a scratch file under `dba2/agency/`
    that imports a forbidden module (`dba2.data`) and confirms the
    denylist check actually flags it."""
    scratch = AGENCY_DIR / "_layering_test_scratch.py"
    scratch.write_text("from dba2.data.repositories import player_repo\n", encoding="utf-8")
    try:
        offenders = _offenders_on_denylist([scratch], _AGENCY_FORBIDDEN_PREFIXES)
        assert offenders, "the agency/ denylist check has no teeth -- it missed a real violation"
    finally:
        scratch.unlink()


def test_agency_signing_denylist_is_mutation_tested(tmp_path):
    """Same mutation-test discipline, for Decision P4-52's narrower rule."""
    scratch = AGENCY_DIR / "_layering_test_scratch_signing.py"
    scratch.write_text(
        "from dba2.managers.decisions.signing import legal_capacity\n", encoding="utf-8"
    )
    try:
        offenders = _offenders_on_denylist([scratch], _AGENCY_SIGNING_FORBIDDEN_PREFIXES)
        assert offenders, (
            "the agency/signing denylist check has no teeth -- it missed a real violation"
        )
    finally:
        scratch.unlink()


def test_managers_agency_denylist_is_mutation_tested(tmp_path):
    """Same mutation-test discipline, for Decision P4-8's directed
    `managers ↛ agency` rule."""
    scratch = MANAGERS_DIR / "_layering_test_scratch.py"
    scratch.write_text("from dba2.agency import motivation\n", encoding="utf-8")
    try:
        offenders = _offenders_on_denylist([scratch], _MANAGERS_AGENCY_FORBIDDEN_PREFIXES)
        assert offenders, (
            "the managers/agency denylist check has no teeth -- it missed a real violation"
        )
    finally:
        scratch.unlink()


def test_media_one_way_valve_is_mutation_tested(tmp_path):
    """Same mutation-test discipline, for Decision P4-31's one-way valve
    -- a scratch file under `dba2/services/` (NOT `media_service.py`)
    that imports `dba2.media` must be caught. Runs the REAL `_media_one_
    way_valve_offenders` (not a re-implementation) -- see that helper's
    own docstring for why this matters."""
    scratch = SERVICES_DIR / "_layering_test_scratch.py"
    scratch.write_text("from dba2.media import frame\n", encoding="utf-8")
    try:
        offenders = _media_one_way_valve_offenders([scratch])
        assert offenders, (
            "the media one-way-valve denylist check has no teeth -- it missed a real violation"
        )
    finally:
        scratch.unlink()


_MEDIA_VALVE_CANDIDATE_NON_EXEMPT_PACKAGES = tuple(
    name for name in _real_top_level_dba2_packages() if name != "media"
)


@pytest.mark.parametrize("package_name", _MEDIA_VALVE_CANDIDATE_NON_EXEMPT_PACKAGES)
def test_media_one_way_valve_exemption_is_mutation_tested(package_name, tmp_path):
    """Mirrors `test_presentation_one_way_valve_bot_exemption_is_mutation_
    tested`'s own reasoning for the sibling media valve, widened the same
    way per §42.1's Blocking Issue 1: the mutation test above only ever
    writes its scratch file under `dba2/services/`, so it could not catch
    an over-widened `_MEDIA_SERVICE_PATH`/`MEDIA_DIR` exemption, and a
    fixed pair of scratch packages (`dba2/sim/`, `dba2/managers/`) proves
    only that those two specific widenings are caught, not the general
    case. Parametrized over EVERY real top-level package under `dba2/`
    except `media` itself (`_media_one_way_valve_offenders`'s own
    `MEDIA_DIR in path.parents` skip makes a scratch file placed inside
    `dba2/media/` itself a meaningless case, not a real one) -- not a
    fixed pair. (Scoped to top-level packages only -- critic round 4/5,
    nit N2: `_real_top_level_dba2_packages` only scans `dba2/*/`, so a
    widening to a real SUB-package, e.g. `dba2/data/repositories/`, is
    not caught by this parametrization and was never claimed to be.)"""
    scratch = DBA2_DIR / package_name / "_layering_test_scratch_media.py"
    scratch.write_text("from dba2.media import frame\n", encoding="utf-8")
    try:
        offenders = _media_one_way_valve_offenders([scratch])
        assert offenders, (
            f"the media one-way-valve's exemption has no teeth against "
            f"dba2/{package_name}/ -- widening the real exemption to admit this "
            f"package would go uncaught (found {offenders})"
        )
    finally:
        scratch.unlink()


def _presentation_one_way_valve_offenders(py_files: list[Path]) -> list[str]:
    """The REAL check `test_nothing_but_view_service_and_bot_imports_
    presentation` runs, extracted so its own mutation test below (`test_
    presentation_one_way_valve_bot_exemption_is_mutation_tested`) exercises
    this exact function rather than a re-implementation of it -- Stage C
    critic round's Blocking Issue 4: the mutation test that existed before
    (`test_view_service_one_way_valve_is_mutation_tested`) re-implemented
    its own inline offender loop and never touched `BOT_DIR` at all, so
    the critic could mutate THIS exemption (e.g. widen it to also admit
    `dba2/sim/` and `dba2/managers/`) and the test suite would not notice."""
    offenders: list[str] = []
    for path in py_files:
        if path == _VIEW_SERVICE_PATH or PRESENTATION_DIR in path.parents:
            continue
        if BOT_DIR in path.parents:
            continue
        for module in _dba2_imports_in_file(path):
            if _matches_prefix(module, "dba2.presentation"):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: import {module}")
    return offenders


def test_nothing_but_view_service_and_bot_imports_presentation():
    """The `dba2.presentation` mirror of `test_nothing_but_media_service_
    imports_media`, WIDENED once (Stage C, phase-5-discord-presentation.md
    §3, Decision P5-4): `dba2/services/view_service.py` remains one
    permitted importer, and `dba2/bot/` is the second -- Decision P5-4's
    own text names `bot/embeds.py` as "the single, small, boring adapter"
    that turns a `Panel` into a `discord.Embed`, which structurally
    requires importing `dba2.presentation.panel.Panel` (`bot/surfaces.py`
    needs the same type for its own `Panel`-carrying return values). This
    is the identical widening shape Decision P4-60 itself names ("widening
    an allowlist needs its compensating denylist written in the same
    step") -- `_SERVICES_ALLOWED_PREFIXES` already admits `dba2.
    presentation` broadly; this denylist (plus its mutation test below) is
    what still enforces "only these two may import it," now over two
    permitted importers instead of one. (Round 10's identical widening of
    the sibling `dba2.media` valve to admit a `tools/` importer was later
    withdrawn, P4-87, because that importer turned out to have zero real
    readers -- this widening differs: `bot/embeds.py` is a real,
    load-bearing consumer from the moment Stage C ships it.)"""
    offenders = _presentation_one_way_valve_offenders(sorted(DBA2_DIR.rglob("*.py")))
    assert not offenders, (
        f"only dba2/services/view_service.py and dba2/bot/ may import "
        f"dba2.presentation: {offenders}"
    )


def test_view_service_one_way_valve_is_mutation_tested(tmp_path):
    """Same mutation-test discipline as `test_media_one_way_valve_is_
    mutation_tested` -- a scratch file under `dba2/services/` (NOT
    `view_service.py`) that imports `dba2.presentation` must be caught.
    Runs the REAL `_presentation_one_way_valve_offenders` (not a re-
    implementation) -- Stage C critic round's Blocking Issue 4: the
    version of this test that shipped with `f5463ba` re-implemented its
    own inline offender loop here, which meant it proved nothing about
    whether the ACTUAL production check (the one `test_nothing_but_view_
    service_and_bot_imports_presentation` runs) still had teeth."""
    scratch = SERVICES_DIR / "_layering_test_scratch.py"
    scratch.write_text("from dba2.presentation import panel\n", encoding="utf-8")
    try:
        offenders = _presentation_one_way_valve_offenders([scratch])
        assert offenders, (
            "the presentation one-way-valve denylist check has no teeth -- "
            "it missed a real violation"
        )
    finally:
        scratch.unlink()


_PRESENTATION_VALVE_CANDIDATE_NON_EXEMPT_PACKAGES = tuple(
    name for name in _real_top_level_dba2_packages() if name not in ("bot", "presentation")
)


@pytest.mark.parametrize("package_name", _PRESENTATION_VALVE_CANDIDATE_NON_EXEMPT_PACKAGES)
def test_presentation_one_way_valve_bot_exemption_is_mutation_tested(package_name, tmp_path):
    """Stage C critic round's Blocking Issue 4, and §42.1's Blocking Issue
    1 (a later critic re-verification found the identical narrow-pair
    shape here too, and its own docstring made the same false "or
    anywhere else" claim `_real_top_level_dba2_packages`'s docstring
    describes for the discord/services valves): the mutation test that
    shipped with the earlier fix only ever wrote its scratch file under
    `dba2/sim/` and `dba2/managers/`, proving the check catches a widening
    TO those two specific packages, not the general claim its own text
    made.

    Parametrized over EVERY real top-level package under `dba2/` except
    `bot` and `presentation` themselves (the two packages/importers the
    exemption is actually supposed to admit) -- not a fixed pair. Each
    instance writes a scratch file importing `dba2.presentation` under
    that one package and runs the REAL helper (`_presentation_one_way_
    valve_offenders`, not a re-implementation) against it. Today `BOT_
    DIR`'s exemption is scoped to exactly `dba2/bot/` and `_VIEW_SERVICE_
    PATH`, so every instance is flagged; if a future edit ever widened
    that exemption to cover any other real TOP-LEVEL `dba2/` package, the
    corresponding instance fails. (Scoped to top-level packages only --
    critic round 4, nit N2: `_real_top_level_dba2_packages` only scans
    `dba2/*/`, so a widening to a real SUB-package, e.g. `dba2/data/
    repositories/`, is not caught by this parametrization and was never
    claimed to be.)"""
    scratch = DBA2_DIR / package_name / "_layering_test_scratch_presentation.py"
    scratch.write_text("from dba2.presentation import panel\n", encoding="utf-8")
    try:
        offenders = _presentation_one_way_valve_offenders([scratch])
        assert offenders, (
            f"the presentation one-way-valve's BOT_DIR/view_service exemption has "
            f"no teeth against dba2/{package_name}/ -- widening the real exemption "
            f"to admit this package would go uncaught (found {offenders})"
        )
    finally:
        scratch.unlink()


def test_presentation_module_source_imports_only_domain_core_and_sim_events():
    """Decision P5-3: `dba2/presentation/` is a pure package restricted to
    `dba2.domain`, `dba2.core` and `dba2.sim.events` -- see module-level
    `_PRESENTATION_ALLOWED_PREFIXES` comment for why the `sim` edge is
    scoped to `events` specifically rather than all of `dba2.sim`."""
    py_files = sorted(PRESENTATION_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {PRESENTATION_DIR} -- path wrong?"
    offenders = _offenders_outside_allowlist(py_files, _PRESENTATION_ALLOWED_PREFIXES)
    assert not offenders, f"presentation/ imports outside its allowed layers: {offenders}"


def test_presentation_layering_rule_is_mutation_tested(tmp_path):
    """Same mutation-test discipline the rest of this file uses (HANDOFF's
    standing rule: "if a future layering rule seems to have 'no teeth,'
    mutation-test it before trusting it"). Writes a scratch file under
    `dba2/presentation/` that imports a forbidden module (`dba2.data`) and
    confirms the allowlist check actually flags it."""
    scratch = PRESENTATION_DIR / "_layering_test_scratch.py"
    scratch.write_text("from dba2.data.repositories import player_repo\n", encoding="utf-8")
    try:
        offenders = _offenders_outside_allowlist([scratch], _PRESENTATION_ALLOWED_PREFIXES)
        assert offenders, (
            "the presentation/ allowlist check has no teeth -- it missed a real violation"
        )
    finally:
        scratch.unlink()


def test_presentation_module_source_has_no_io_side_effects():
    """Decision P5-3's purity extension: no module under `dba2/
    presentation/` may call `datetime.now()`, construct a bare
    `random.Random()`, read a file, or touch a connection -- the identical
    rule `sim/`/`managers/`/`agency/`/`media/` already carry."""
    py_files = sorted(PRESENTATION_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {PRESENTATION_DIR} -- path wrong?"
    offenders = _purity_offenders(py_files)
    assert not offenders, (
        f"presentation/ has I/O-shaped side effects, violating Decision P5-3: {offenders}"
    )


# Stage D commit 1 (phase-5-stage-d-broadcast.md §3, P5-97.t): the broadcast
# planner is pure, so it must be runnable to completion in a unit test --
# pacing (`asyncio`, `time`, `sleep`, `await`, `async def`) lives in
# `dba2/bot/broadcast.py` and nowhere in this module. AST-based, not a
# substring grep, so a docstring that explains the rule does not trip it.
#
# What "nowhere" covers: the static imports, every reference to the names
# below, and the dynamic-import spellings -- a bare `importlib` import (it has
# no use in a pure planner except loading something else), `importlib.
# import_module(...)`, `__import__(...)`, and `exec`/`eval` (which can run
# `import asyncio` from a string). What it does NOT cover: a route that never
# writes any of these names, such as `getattr(builtins, "__imp" + "ort__")`.
# The rule is a tripwire against the honest mistake, not a sandbox.
_BROADCAST_PATH = PRESENTATION_DIR / "broadcast.py"
_BROADCAST_FORBIDDEN_NAMES = frozenset(
    {"asyncio", "time", "sleep", "importlib", "__import__", "import_module", "exec", "eval"}
)


def _clock_and_concurrency_offenders(path: Path) -> list[str]:
    """The REAL check `test_presentation_broadcast_owns_no_clock_and_no_
    concurrency` runs, extracted so its mutation twin exercises this exact
    function on a scratch copy rather than a re-implementation. Reports
    `path.name`, not a repo-relative path, because the twin's scratch copy
    lives under `tmp_path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _BROADCAST_FORBIDDEN_NAMES:
                    offenders.append(f"{path.name}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] in _BROADCAST_FORBIDDEN_NAMES:
                offenders.append(f"{path.name}: from {node.module} import ...")
            for alias in node.names:
                if alias.name in _BROADCAST_FORBIDDEN_NAMES:
                    offenders.append(f"{path.name}: imports the name {alias.name}")
        elif isinstance(node, ast.Name) and node.id in _BROADCAST_FORBIDDEN_NAMES:
            offenders.append(f"{path.name}: references {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _BROADCAST_FORBIDDEN_NAMES:
            offenders.append(f"{path.name}: references .{node.attr}")
        elif isinstance(node, ast.Await):
            offenders.append(f"{path.name}: await at line {node.lineno}")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith)):
            offenders.append(f"{path.name}: {type(node).__name__} at line {node.lineno}")
    return offenders


def test_presentation_broadcast_owns_no_clock_and_no_concurrency():
    """Design §3/P5-97.t: `presentation/broadcast.py` contains no `asyncio`,
    `time`, `sleep`, `await` or `async def`, and no dynamic-import spelling
    (`importlib`, `import_module`, `__import__`, `exec`, `eval`) that could
    smuggle one in -- see the comment on `_BROADCAST_FORBIDDEN_NAMES` for what
    the tripwire does not cover."""
    assert _BROADCAST_PATH.exists(), f"{_BROADCAST_PATH} is missing -- path wrong?"
    offenders = _clock_and_concurrency_offenders(_BROADCAST_PATH)
    assert not offenders, (
        f"presentation/broadcast.py owns a clock or concurrency, violating P5-97.t: {offenders}"
    )


@pytest.mark.parametrize(
    "planted",
    [
        "import asyncio\n",
        "import asyncio.tasks\n",
        "from asyncio import sleep\n",
        "import time\n",
        "from time import monotonic\n",
        "_x = time.monotonic()\n",
        "_x.sleep(1)\n",
        "async def _planted():\n    pass\n",
        "async def _planted():\n    async with _x:\n        pass\n",
        "await _x\n",
        # dynamic imports (N6): each is a way to reach `asyncio`/`time` that no
        # static-import check sees
        "import importlib\n",
        "import importlib.util\n",
        "from importlib import import_module\n",
        "from importlib.util import find_spec\n",
        "_m = importlib.import_module('asyncio')\n",
        "_m = _x.import_module('time')\n",
        "_m = __import__('time')\n",
        "exec('import asyncio')\n",
        "eval('__import__(\"time\")')\n",
    ],
)
def test_broadcast_clock_and_concurrency_rule_is_mutation_tested(planted, tmp_path):
    """P5-97.t's own requirement: "a rule that silently matches nothing is
    worse than no rule". Plants each forbidden token in a scratch copy of the
    REAL module and confirms the REAL helper flags it -- and that the
    unmodified copy is clean, so a flag is attributable to the planted text."""
    scratch = tmp_path / "broadcast.py"
    real_source = _BROADCAST_PATH.read_text(encoding="utf-8")
    scratch.write_text(real_source, encoding="utf-8")
    assert not _clock_and_concurrency_offenders(scratch), "unmodified copy must be clean"
    scratch.write_text(real_source + "\n" + planted, encoding="utf-8")
    assert _clock_and_concurrency_offenders(scratch), (
        f"the no-clock/no-concurrency check has no teeth -- it missed {planted!r}"
    )


def _discord_import_offenders(py_files: list[Path]) -> list[str]:
    """The REAL check `test_discord_is_imported_nowhere_under_dba2_except_
    bot` runs, extracted so its own mutation tests below exercise this
    exact function rather than a re-implementation of it -- Blocking Issue
    B, critic re-verification of the Stage C fix round: the mutation test
    that existed before this extraction re-implemented its own inline
    scan directly against a scratch file, never routing through the real
    per-file `BOT_DIR` exemption at all -- so it proved nothing about
    whether the ACTUAL production check still enforced that exemption's
    boundary. The critic proved this live: widening the real exemption to
    admit two more packages, and planting a genuine `import discord`
    inside one of them, left the full layering suite green. Uses `_all_
    imports_in_file` (the unprefixed scanner the `tools/` quarantine tests
    already use), not `_dba2_imports_in_file`, because `discord` is not a
    `dba2...`-rooted module."""
    offenders: list[str] = []
    for path in py_files:
        if BOT_DIR in path.parents:
            continue
        for module in _all_imports_in_file(path):
            if module == "discord" or module.startswith("discord."):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: import {module}")
    return offenders


def test_discord_is_imported_nowhere_under_dba2_except_bot():
    """Decision P5-1: "`dba2/bot/` is Phase 5's home, and `discord` may be
    imported nowhere else under `dba2/`." The mirror-image rule to
    `test_nothing_under_dba2_except_bot_imports_services` -- that one
    guards the inward edge (only `bot/` may import `dba2.services`), this
    one guards the outward edge (only `bot/` may import the third-party
    `discord` package)."""
    offenders = _discord_import_offenders(sorted(DBA2_DIR.rglob("*.py")))
    assert not offenders, (
        f"discord may be imported nowhere under dba2/ except dba2/bot/, "
        f"violating Decision P5-1: {offenders}"
    )


def test_discord_import_denylist_is_mutation_tested(tmp_path):
    """Same mutation-test discipline the rest of this file uses --
    HANDOFF's own citation of "a real gap in the AST import-checker
    survived one full build+critic round before being caught this way"
    (Decision P5-1's own text), required "in the same step" as the rule
    itself. Writes a scratch file under `dba2/presentation/` (a directory
    with no legitimate reason to ever import `discord`) containing both
    an `import discord` and a `from discord import Embed` form, and
    confirms the REAL helper (`_discord_import_offenders`, not a
    re-implementation) flags both."""
    scratch = PRESENTATION_DIR / "_layering_test_scratch_discord.py"
    scratch.write_text("import discord\nfrom discord import Embed\n", encoding="utf-8")
    try:
        offenders = _discord_import_offenders([scratch])
        assert len(offenders) >= 2, (
            f"the discord-import denylist check has no teeth -- it missed a real "
            f"violation (found {offenders})"
        )
    finally:
        scratch.unlink()


_DISCORD_VALVE_CANDIDATE_NON_EXEMPT_PACKAGES = tuple(
    name for name in _real_top_level_dba2_packages() if name != "bot"
)


@pytest.mark.parametrize("package_name", _DISCORD_VALVE_CANDIDATE_NON_EXEMPT_PACKAGES)
def test_discord_import_bot_exemption_is_mutation_tested(package_name, tmp_path):
    """§42.1's Blocking Issue 1 (critic re-verification of the round-2
    fix): the version of this test that shipped with the round-2 fix only
    ever wrote its scratch file under two pre-chosen packages (`dba2/sim/`,
    `dba2/managers/`), which proves the check catches a widening TO those
    two specific packages, not the general claim its own docstring made
    ("or anywhere else"). The critic proved live that widening the real
    exemption to admit `dba2/agency/` instead, with a real `import discord`
    planted there, left the full suite green (see `_real_top_level_dba2_
    packages`'s own docstring for the exact repro).

    Parametrized over EVERY real top-level package under `dba2/` except
    `bot` itself (the one package the exemption is actually supposed to
    admit) -- not a fixed pair. Each instance writes a scratch file with a
    real `import discord` under that one package and runs the REAL helper
    (`_discord_import_offenders`, not a re-implementation) against it.
    Today `BOT_DIR`'s exemption is scoped to exactly `dba2/bot/`, so every
    instance is flagged; if a future edit ever widened that exemption to
    cover any other real TOP-LEVEL `dba2/` package, the corresponding
    instance fails. (Scoped to top-level packages only -- critic round 4,
    nit N2: `_real_top_level_dba2_packages` only scans `dba2/*/`, so a
    widening to a real SUB-package, e.g. `dba2/data/repositories/`, is
    not caught by this parametrization and was never claimed to be.)"""
    scratch = DBA2_DIR / package_name / "_layering_test_scratch_discord.py"
    scratch.write_text("import discord\n", encoding="utf-8")
    try:
        offenders = _discord_import_offenders([scratch])
        assert offenders, (
            f"the discord-import valve's BOT_DIR exemption has no teeth against "
            f"dba2/{package_name}/ -- widening the real exemption to admit this "
            f"package would go uncaught (found {offenders})"
        )
    finally:
        scratch.unlink()


def _flatten_attr(node: ast.AST) -> str | None:
    """Flatten a `Name`/`Attribute` chain to its dotted string (e.g.
    `ast.Attribute(value=Name('random'), attr='seed')` -> `"random.seed"`),
    mirroring `_resolve_relative`'s "read the AST the way Python itself
    would resolve it" spirit for the purity checks below. Anything that
    isn't a plain dotted name/attribute chain (a call result, a subscript,
    etc.) returns `None` rather than guessing."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _flatten_attr(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


# phase-1-simulation-engine.md §9 / Decision P1-1: "no module under sim/
# may call datetime.now(), construct a bare random.Random(), read a file,
# or touch a connection: time and randomness are parameters." Matches
# both the bare-imported-name form (`from datetime import datetime;
# datetime.now()`) and the module-qualified form (`import datetime;
# datetime.datetime.now()`) via a dotted-suffix check, and the leading
# `random.`-qualified form only for `random.seed`/`random.Random` so a
# beat's own semantically-unrelated `foo.seed(...)`/`SomeOtherRandom(...)`
# helper (should either ever exist) is never a false positive.
_FORBIDDEN_CALL_PREDICATES = (
    (
        "datetime.now()",
        lambda dotted: dotted in {"datetime.now"} or dotted.endswith(".datetime.now"),
    ),
    (
        "a bare random.Random() construction",
        lambda dotted: dotted in {"Random", "random.Random"},
    ),
    ("random.seed()", lambda dotted: dotted in {"random.seed"}),
    ("the open() builtin", lambda dotted: dotted == "open"),
)


def _purity_offenders(py_files: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                dotted = _flatten_attr(node.func)
                if dotted is None:
                    continue
                for label, predicate in _FORBIDDEN_CALL_PREDICATES:
                    if predicate(dotted):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}: calls {label}")
            elif isinstance(node, (ast.Attribute, ast.Name)):
                dotted = _flatten_attr(node)
                if dotted == "os.environ" or (dotted and dotted.endswith(".os.environ")):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: references os.environ")
    return offenders


def test_sim_module_source_has_no_io_side_effects():
    """§9's purity extension to Decision P1-1: "no module under sim/ may
    call datetime.now(), construct a bare random.Random(), read a file,
    or touch a connection: time and randomness are parameters." Closes
    the I/O surface the plain `dba2.data`-import denylist above doesn't
    cover -- a module could be perfectly free of `dba2.data` imports and
    still read the wall clock, seed the global RNG, or open a file
    directly."""
    py_files = sorted(SIM_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {SIM_DIR} -- path wrong?"
    offenders = _purity_offenders(py_files)
    assert not offenders, f"sim/ has I/O-shaped side effects, violating Decision P1-1: {offenders}"


def test_managers_module_source_has_no_io_side_effects():
    """Decision P2-2(b)'s purity extension, the same rule §9 of Phase 1
    gives `sim/`: no module under `managers/` may call `datetime.now()`,
    construct a bare `random.Random()`, read a file, or touch a
    connection -- time and randomness are parameters, matching Decision
    P1-1's rule transplanted onto `managers/`."""
    py_files = sorted(MANAGERS_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {MANAGERS_DIR} -- path wrong?"
    offenders = _purity_offenders(py_files)
    assert not offenders, (
        f"managers/ has I/O-shaped side effects, violating Decision P2-2(b): {offenders}"
    )


def test_agency_module_source_has_no_io_side_effects():
    """Decision P4-8's purity extension: no module under `dba2/agency/`
    may call `datetime.now()`, construct a bare `random.Random()`, read a
    file, or touch a connection -- the identical rule `managers/`/`sim/`
    already carry."""
    py_files = sorted(AGENCY_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {AGENCY_DIR} -- path wrong?"
    offenders = _purity_offenders(py_files)
    assert not offenders, (
        f"agency/ has I/O-shaped side effects, violating Decision P4-8: {offenders}"
    )


def test_media_module_source_has_no_io_side_effects():
    """Decision P4-31's purity extension (P4-8's rule, carried to the
    other new Phase 4 package): no module under `dba2/media/` may call
    `datetime.now()`, construct a bare `random.Random()`, read a file, or
    touch a connection. **This test did not exist through Stage D step
    15** -- `test_media_module_source_imports_only_domain_core_managers_
    and_agency`/`test_nothing_but_media_service_imports_media` cover
    import DIRECTION, but nothing enforced this purity axis for `media/`
    until step 16 added real content and this test alongside it (the
    project's own history already has one case of a new directory sitting
    uncovered by a layering rule for a full stage before being caught --
    Decision P4-60's own citation of that risk is why this gap was checked
    for directly here rather than assumed closed)."""
    py_files = sorted(MEDIA_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {MEDIA_DIR} -- path wrong?"
    offenders = _purity_offenders(py_files)
    assert not offenders, (
        f"media/ has I/O-shaped side effects, violating Decision P4-31: {offenders}"
    )


def test_sim_module_source_never_imports_or_names_decimal():
    """Decision P1-5: "No module under sim/ imports decimal or names
    Decimal at all", turned into "a mechanical test in the same AST style
    as the existing purity checks rather than a convention someone
    remembers." Catches both `import decimal`/`from decimal import ...`
    (via plain import-statement inspection, no dba2-prefix filtering
    needed since `decimal` is a stdlib module) and any bare `Decimal`
    name reference (a type annotation, an `isinstance` check, or a
    constructor call all show up as `ast.Name(id="Decimal")` somewhere in
    the tree)."""
    py_files = sorted(SIM_DIR.rglob("*.py"))
    assert py_files, f"no .py files found under {SIM_DIR} -- path wrong?"
    offenders: list[str] = []
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == "decimal" for alias in node.names):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: import decimal")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "decimal":
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: from decimal import ...")
            elif isinstance(node, ast.Name) and node.id == "Decimal":
                offenders.append(f"{path.relative_to(REPO_ROOT)}: names Decimal")
    assert not offenders, (
        f"sim/ imports or names decimal.Decimal, violating Decision P1-5: {offenders}"
    )


def _services_one_way_valve_offenders(py_files: list[Path]) -> list[str]:
    """The REAL check `test_nothing_under_dba2_except_bot_imports_services`
    runs, extracted so its own mutation test below exercises this exact
    function rather than a re-implementation of it -- Blocking Issue B,
    critic re-verification of the Stage C fix round: this check had NO
    mutation test at all before this extraction, the same "no teeth"
    defect the discord-import and media valves independently had."""
    offenders: list[str] = []
    for path in py_files:
        if BOT_DIR in path.parents or SERVICES_DIR in path.parents:
            continue
        for module in _dba2_imports_in_file(path):
            if _matches_prefix(module, "dba2.services"):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: import {module}")
    return offenders


def test_nothing_under_dba2_except_bot_imports_services():
    """"only `bot` and `tools` may import `services` from outside" -- the
    "from outside" half: within `dba2/` itself, only `dba2/bot/` may
    import `dba2.services`. `dba2/services/` importing itself (one service
    composing another) is excluded here as the legitimate peer-import case
    `test_services_module_source_imports_only_data_domain_core_phase_sim`
    above already allows and checks on its own terms."""
    offenders = _services_one_way_valve_offenders(sorted(DBA2_DIR.rglob("*.py")))
    assert not offenders, (
        f"only dba2/bot/ may import dba2.services from elsewhere under dba2/: {offenders}"
    )


def test_services_one_way_valve_is_mutation_tested(tmp_path):
    """Same mutation-test discipline the rest of this file uses, added
    fresh for this check (Blocking Issue B) -- a scratch file under
    `dba2/sim/` (neither `dba2/bot/` nor `dba2/services/`) that imports
    `dba2.services` must be caught by the REAL helper."""
    scratch = SIM_DIR / "_layering_test_scratch_services.py"
    scratch.write_text("from dba2.services import league_service\n", encoding="utf-8")
    try:
        offenders = _services_one_way_valve_offenders([scratch])
        assert offenders, (
            "the services one-way-valve denylist check has no teeth -- it missed a "
            "real violation"
        )
    finally:
        scratch.unlink()


_SERVICES_VALVE_CANDIDATE_NON_EXEMPT_PACKAGES = tuple(
    name for name in _real_top_level_dba2_packages() if name not in ("bot", "services")
)


@pytest.mark.parametrize("package_name", _SERVICES_VALVE_CANDIDATE_NON_EXEMPT_PACKAGES)
def test_services_one_way_valve_exemption_is_mutation_tested(package_name, tmp_path):
    """§42.1's Blocking Issue 1 (critic re-verification of the round-2
    fix): the version of this test that shipped with the round-2 fix only
    ever wrote its scratch file under `dba2/managers/`, proving the check
    catches a widening TO that one specific package, not the general claim
    that any widening is caught. The critic proved live that widening the
    real exemption to admit `dba2/phase/` instead, with a real `from dba2.
    services import league_service` planted there, left the full suite
    green (see `_real_top_level_dba2_packages`'s own docstring for the
    exact repro).

    Parametrized over EVERY real top-level package under `dba2/` except
    `bot` and `services` themselves (the two packages the exemption is
    actually supposed to admit) -- not a fixed single package. Today the
    exemption is scoped to exactly those two, so every instance is
    flagged; if a future edit ever widened it to admit any other real
    TOP-LEVEL `dba2/` package, the corresponding instance fails. (Scoped
    to top-level packages only -- critic round 4, nit N2: `_real_top_
    level_dba2_packages` only scans `dba2/*/`, so a widening to a real
    SUB-package, e.g. `dba2/data/repositories/`, is not caught by this
    parametrization and was never claimed to be.)"""
    scratch = DBA2_DIR / package_name / "_layering_test_scratch_services.py"
    scratch.write_text("from dba2.services import league_service\n", encoding="utf-8")
    try:
        offenders = _services_one_way_valve_offenders([scratch])
        assert offenders, (
            f"the services one-way-valve denylist check has no teeth against "
            f"dba2/{package_name}/ -- widening the real exemption to admit this "
            f"package would go uncaught (found {offenders})"
        )
    finally:
        scratch.unlink()


def test_relative_imports_are_resolved_and_caught(tmp_path):
    """Phase 0 step-9 critic round, B1's regression guard: a relative
    import of a forbidden module must be caught exactly like the
    equivalent absolute import would be. Writes a scratch file under
    `dba2/sim/` (so its resolved package is `dba2.sim`, matching critic's
    own reproduction) containing `from ..data.repositories import
    player_repo` and `from ..services import ratings_service`, and asserts
    `_dba2_imports_in_file` resolves both to their absolute dotted names
    rather than missing them."""
    scratch = SIM_DIR / "_layering_test_scratch.py"
    scratch.write_text(
        "from ..data.repositories import player_repo\n"
        "from ..services import ratings_service\n",
        encoding="utf-8",
    )
    try:
        resolved = _dba2_imports_in_file(scratch)
        assert "dba2.data.repositories" in resolved
        assert "dba2.services" in resolved
    finally:
        scratch.unlink()


def test_tools_is_allowed_to_import_services():
    """"only `bot` and `tools` may import `services` from outside" -- the
    "tools/ is allowed to" half. `tools/` lives outside `dba2/` (§3's
    layout), so it can't be checked by an AST walk rooted at `dba2/`; this
    positively confirms at least one `tools/*.py` file actually does import
    `dba2.services` (e.g. `tools/seed_dev_league.py` -> `league_service`,
    `ratings_service`) rather than the "allowed to" half of the rule going
    untested because nothing in `tools/` happens to exercise it."""
    py_files = sorted(TOOLS_DIR.glob("*.py"))
    assert py_files, f"no .py files found under {TOOLS_DIR} -- path wrong?"
    importers = [
        path
        for path in py_files
        if any(_matches_prefix(m, "dba2.services") for m in _dba2_imports_in_file(path))
    ]
    assert importers, (
        "expected at least one tools/*.py file to import dba2.services -- "
        "found none, so the 'tools/ is allowed to import services' half of "
        "Decision 4's rule is currently untested"
    )


# ---------------------------------------------------------------------------
# Decision P2-32 -- tools/_gm_draft_class_stub.py's quarantine
# ---------------------------------------------------------------------------


def _all_imports_in_file(path: Path) -> list[str]:
    """Every dotted module name a file imports, absolute or relative,
    with **no `dba2`-prefix filter** -- `_dba2_imports_in_file` above
    exists specifically to scope down to `dba2...`-rooted imports for the
    layering rules that only care about those; `tools/_gm_draft_class_
    stub.py` lives outside `dba2/` entirely, so a caller checking for an
    import *of* it needs every import a file has, not only the `dba2`-
    rooted ones. Absolute imports only (this stub is never imported via a
    relative import from anywhere reachable under `dba2/`, so `_resolve_
    relative`'s package-context machinery isn't needed here)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
            for alias in node.names:
                if alias.name != "*":
                    modules.append(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
    return modules


def test_nothing_under_dba2_imports_the_gm_draft_class_stub():
    """Decision P2-32: "same quarantine, same import test" as its sibling
    P2-23 progression stub (`tools/_gm_progression_stub.py`, built and
    quarantine-tested below, in `test_nothing_under_dba2_imports_the_gm_
    progression_stub`) -- `tools/_gm_draft_class_stub.py` is harness-only
    and must never be reachable from `dba2/`."""
    offenders: list[str] = []
    for path in sorted(DBA2_DIR.rglob("*.py")):
        for module in _all_imports_in_file(path):
            if module == "tools._gm_draft_class_stub" or module.startswith(
                "tools._gm_draft_class_stub."
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: import {module}")
    assert not offenders, (
        f"tools/_gm_draft_class_stub.py must never be imported from under dba2/: {offenders}"
    )


def test_nothing_under_dba2_imports_the_gm_progression_stub():
    """Decision P2-23: same quarantine, same import test as the sibling
    draft-class stub -- `tools/_gm_progression_stub.py` is harness-only
    and must never be reachable from `dba2/`."""
    offenders: list[str] = []
    for path in sorted(DBA2_DIR.rglob("*.py")):
        for module in _all_imports_in_file(path):
            if module == "tools._gm_progression_stub" or module.startswith(
                "tools._gm_progression_stub."
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: import {module}")
    assert not offenders, (
        f"tools/_gm_progression_stub.py must never be imported from under dba2/: {offenders}"
    )


# ---------------------------------------------------------------------------
# Architect round-7 report §4.1 -- tools/gm_harness/'s own reachability
# check (not the P2-23/P2-32 quarantine convention itself -- this package
# is a starting-state generator, the same job seed_dev_league.py/
# validate_sim.py::generate_league already do un-prefixed with no
# quarantine test -- but it transitively imports both quarantined stubs,
# so it would be cheap for a future dba2/ module to reach into it for a
# convenient fixture. Same `_all_imports_in_file` AST-scan pattern.)
# ---------------------------------------------------------------------------


def test_nothing_under_dba2_imports_the_gm_harness():
    """Architect round-7 report §4.1: `tools/gm_harness/` generates
    starting state for the in-memory GM validation harness and must never
    be reachable from `dba2/`."""
    offenders: list[str] = []
    for path in sorted(DBA2_DIR.rglob("*.py")):
        for module in _all_imports_in_file(path):
            if module == "tools.gm_harness" or module.startswith("tools.gm_harness."):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: import {module}")
    assert not offenders, (
        f"tools/gm_harness/ must never be imported from under dba2/: {offenders}"
    )


# ---------------------------------------------------------------------------
# Decision P4-16 -- the retargeted P2-21 denylist. Originally "free_agency_
# service.py never imports motivation_repo" (a whole-file check on one
# file); Phase 4's `rank_offers` replacement reads motivation data through
# `agency_service.py`'s own read model instead of importing the repo
# itself, so the guarantee is now repo-wide: ONLY `agency_service.py` may
# import `motivation_repo` -- the same one-reader-of-one-table shape
# `test_nothing_but_media_service_imports_media` already establishes for
# Decision P4-31's media valve, applied here to Decision P4-16's.
# ---------------------------------------------------------------------------

_AGENCY_SERVICE_PATH = SERVICES_DIR / "agency_service.py"
_MOTIVATION_REPO_FORBIDDEN_PREFIXES = ("dba2.data.repositories.motivation_repo",)


def test_only_agency_service_imports_motivation_repo():
    """Decision P4-16: `rank_offers` is replaced with a motivation-weighted
    version, but never by importing `motivation_repo` directly -- it reads
    an already-loaded `PlayerMotivation` parameter instead (`GmContext.
    motivation_by_player`, assembled by `agency_service.load_motivation_
    for_league`). This is a stronger form of the original P2-21 guarantee
    (a whole-file check scoped to `free_agency_service.py` alone): every
    file under `dba2/` except `agency_service.py` itself is checked, so a
    future module reaching for `motivation_repo` directly (free agency or
    otherwise) trips this the same way a stray import inside `free_agency_
    service.py` used to."""
    assert _AGENCY_SERVICE_PATH.exists(), f"{_AGENCY_SERVICE_PATH} does not exist"
    # Critic nit 5 fix: reuses the same `_offenders_on_denylist` helper
    # `test_motivation_repo_denylist_is_mutation_tested` below actually
    # exercises, rather than a separate inline `rglob` loop -- before this
    # fix, the mutation test proved teeth for a helper this check did not
    # call, so a regression in the real check's own (previously separate)
    # loop logic had no mutation coverage at all.
    py_files = [p for p in sorted(DBA2_DIR.rglob("*.py")) if p != _AGENCY_SERVICE_PATH]
    offenders = _offenders_on_denylist(py_files, _MOTIVATION_REPO_FORBIDDEN_PREFIXES)
    assert not offenders, (
        f"only dba2/services/agency_service.py may import motivation_repo, "
        f"violating Decision P4-16: {offenders}"
    )


def test_motivation_repo_denylist_is_mutation_tested(tmp_path):
    """Same mutation-test discipline the rest of this file uses: writes a
    scratch file under `dba2/services/` (a directory whose files, unlike
    `dba2/agency/`, legitimately import `dba2.data` in general) that
    imports `motivation_repo` directly, and confirms the retargeted P4-16
    denylist actually flags it."""
    scratch = SERVICES_DIR / "_layering_test_scratch_motivation_repo.py"
    scratch.write_text(
        "from dba2.data.repositories import motivation_repo\n", encoding="utf-8"
    )
    try:
        offenders = _offenders_on_denylist([scratch], _MOTIVATION_REPO_FORBIDDEN_PREFIXES)
        assert offenders, (
            "the motivation_repo denylist check has no teeth -- it missed a real violation"
        )
    finally:
        scratch.unlink()
