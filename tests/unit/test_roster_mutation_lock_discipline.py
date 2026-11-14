"""The F6 guard -- the Roster Mutation Lock Protocol (RMLP) discipline
test (design: inline RMLP revision-2 + docs/design/_rmlp_addendum_a.md,
§7.4, REWRITTEN by Decision 4a and amended by Addendum A2/A3/A4). AST-based
over `dba2/`, in the style of `test_layering.py` -- this is what converts
the protocol from a document into an enforced rule ("a guaranteed behavior
is a hook or an always-loaded rule, not a written instruction").

**Four explicit tables, the single source of truth** -- every check below
is keyed off the SAME tables, so a future author can't satisfy one check by
editing a list another check doesn't consult:

    RMLP_MUTATORS          -- qualified function name -> expected lock-set shape
    RMLP_LOCK_INHERITORS   -- qualified function names that intentionally inherit locks
    RMLP_ENTRY_POINTS      -- qualified function names permitted to call open_league_gate
    RMLP_FUTURE_ADOPTERS   -- qualified function names of writers deliberately not yet adopted
    WRITE_SHAPED           -- the 8 repo-layer writer functions this protocol scopes over

**Tier 3 -- `trade_offers` rows (docs/design/phase-5-trade-offer-review-
surface.md §6.3).** RMLP's canonical acquisition order is tier 0 (the league
gate), the `leagues` row, tier 1 (teams), tier 2 (players), and now tier 3:
`trade_offers`, ALWAYS acquired after tiers 1 and 2, never before. It closes
the `league_service.assign_manager` inversion (`teams` then `trade_offers`),
which a CAS-first offer path would have reversed into a real deadlock. The
only path that touches a `trade_offers` row without holding tier 1/2 is
`offer_service.veto`, and it is safe precisely because it takes none -- a
transaction that holds only tier 0 and tier 3 cannot be the "holds tier 3,
wants tier 1" half of any cycle. The offer-service checks at the bottom of
this file (`test_veto_never_acquires_...` and its neighbours) pin the rest.

**`tools/` exclusion is an OPERATIONAL rule, not a structural one** (§7.4's
own text): `tools/` lives outside `dba2/`, so this AST scan (rooted at
`DBA2_DIR`) naturally never sees it -- that is a scope limitation of this
test, not a claim that `tools/` is safe. The actual safety claim is
"only run these tools while the bot is not running against the same
database," recorded in `tools/`'s own module docstrings; if that ever
becomes hard to guarantee, the fix is for the tools to open the exclusive
gate themselves (`roster_lock.open_league_gate` already supports it).

**Import/call resolution, the mechanism every check below depends on.**
Every function under `dba2/` is indexed once, keyed by its fully-dotted
qualified name (e.g. `dba2.services.roster_service.sign`). For each
function, every `ast.Call` in its body is resolved to a target qualified
name using that FILE's own `from X import Y [as Z]` statements -- a bare
call `apply_signing(...)` resolves via `from dba2.services.gm_service
import apply_signing`; an attribute call `contract_repo.create_contract(
...)` resolves via `from dba2.data.repositories import contract_repo`
(treating the imported name as a submodule, the attribute as the function
within it); a bare call to a function defined in the SAME file resolves
without needing any import at all. A call that can't be resolved this way
(a method call on a local variable, e.g. `conn.execute(...)`) is simply
never a match for anything this file checks -- it does not raise, and is
not itself evidence of anything.

**Indexing recurses into class bodies (fix round, 2026-09-18 -- B1).**
`_index_functions` used to walk only `ast.Module.body`, which sees a
top-level `def`/`async def` but not a method defined inside a `class`
statement -- so a rogue writer or a rogue `open_league_gate` call hidden
inside a class method (exactly the shape `dba2/bot/cogs/roster.py`'s
`RosterCommands` already has, and the shape the design's own §11
disclosed-deferred register names for the next mutation site, a future
trade-offer-accept cog) was invisible to checks 3, 5, and 6 -- reproduced
live, disclosed in the build report, and closed here: `_index_functions`
now recurses into every `ast.ClassDef` (nested classes too, though none
exist under `dba2/` today), keying each method by its full dotted path,
e.g. `dba2.bot.cogs.roster.RosterCommands.waive`. `same_module_functions`
(the set used to resolve a bare same-file call) intentionally stays
scoped to TOP-LEVEL functions only -- a method is never called by its
bare name (`self.method(...)` is an `Attribute` call on a local variable,
which `_resolve_call` already treats as unresolvable, exactly like
`conn.execute(...)`), so widening that set would not change any
resolution and would only invite a false match against an unrelated
top-level function that happens to share a method's name.
"""
from __future__ import annotations

import ast
import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# This file writes scratch files into `dba2/` or scans it (`tests/conftest.py`): one xdist
# group keeps every such writer and reader on one worker, run serially.
pytestmark = pytest.mark.xdist_group("source-tree")

REPO_ROOT = Path(__file__).resolve().parents[2]
DBA2_DIR = REPO_ROOT / "dba2"

# ---------------------------------------------------------------------------
# The four tables (plus WRITE_SHAPED) -- the single source of truth.
# ---------------------------------------------------------------------------

ACQUIRE = "dba2.services.roster_lock.acquire"
OPEN_LEAGUE_GATE = "dba2.services.roster_lock.open_league_gate"
MAPS_LOCK_CONTENTION = "dba2.services.roster_lock.maps_lock_contention"
# The one production locker of the `drafts` row (Decision P5-98.g), and the
# subject of check 9 below: it takes that lock inside whatever transaction
# its caller owns, so it must never open the league gate.
RUN_DRAFT_CYCLE = "dba2.services.gm_service.run_draft_cycle"

# Check 10 (round-1 critic's blocking issue B1) -- the gate-less
# transactions. A transaction that opens the league gate gets its `SET LOCAL
# lock_timeout` from `open_league_gate` (Decision 3); a transaction that
# opens NO gate and still takes a blocking lock gets nothing, and the server
# default is wait-forever. Each member below takes one: `run_draft_cycle`
# the `drafts` row, `_resume_the_ai_draft` the `leagues` row through
# `league_claim.check_claim` and then the `drafts` row through the cycle, and
# Stage E's `season_service` functions the `leagues` row itself. **Each Stage E
# qualname joins in the commit that CREATES its function, never earlier**
# (phase-5-stage-e-advance.md P5-99.t.2, M-5): check 10 treats a listed-but-
# absent qualname as a violation.
# `_resume_the_ai_draft` shipped without the statement and waited 8.5
# seconds on a press's uncommitted `UPDATE leagues`, pinning a pool
# connection and a person's deferred reply to somebody else's write.
GATELESS_LOCK_TRANSACTIONS: frozenset[str] = frozenset(
    {
        RUN_DRAFT_CYCLE,
        "dba2.services.gm_command_service._resume_the_ai_draft",
        "dba2.services.season_service._claim_league",
        "dba2.services.season_service._release_claim",
        "dba2.services.season_service._move_pointer",
        # Created in build-plan commit 9, not 10 as the plan tabulated: the work
        # prefix needs the current entry's `entry_index`, which is this function's
        # locked read (P5-99.c.1). It joins here because it is created here.
        "dba2.services.season_service._read_pointer_locked",
        # Created in build-plan commit 10, with the postpone/restore commands they
        # serve: each takes the `leagues` row `FOR SHARE` through `check_claim`
        # (P5-99.t.1) and opens no gate.
        "dba2.services.season_service._postpone_game",
        "dba2.services.season_service._restore_game",
        # Created in build-plan commit 13 with `/advance-unlock`'s service side: it takes
        # the `leagues` row `FOR UPDATE` to read the claim's age and clear it, and opens
        # no gate. The plan's per-commit table (P5-99.t.2) did not list it; the rule it
        # states -- every transaction this stage adds is gate-less and lock-taking -- does.
        "dba2.services.season_service._unlock_claim",
        # Created in build-plan commit 15 with `/advance-force`: the forced pointer move
        # takes the `leagues` row `FOR UPDATE` (through `_lock_and_count_entries`) and
        # opens no gate.
        "dba2.services.season_service._force_pointer",
        # The four settings setters each gained their own transaction in
        # P5-99.t: `set_lock_timeout` first, then the claim-only `check_claim`.
        "dba2.services.gm_command_service.set_strategy",
        "dba2.services.gm_command_service.set_role",
        "dba2.services.gm_command_service.set_directive",
        "dba2.services.gm_command_service.set_timeline",
    }
)
_SET_LOCK_TIMEOUT_MARKER = "set_lock_timeout"

# Hand-curated (Addendum A5's own rule: any new writer of players/contracts/
# contract_years/dead_cap_charges/cap_holds must be added here or given a
# recorded exclusion -- see that addendum for `set_years_pro_and_retirement`/
# `create_player`/`set_overall_cached`'s recorded exclusions, none of which
# touch team_id/roster_status/contract status/dead-cap money).
WRITE_SHAPED: frozenset[str] = frozenset(
    {
        "dba2.data.repositories.contract_repo.create_contract",
        "dba2.data.repositories.contract_repo.terminate_contract",
        "dba2.data.repositories.contract_repo.set_contract_team",
        "dba2.data.repositories.contract_repo.set_option_decision",
        "dba2.data.repositories.contract_repo.create_dead_cap_charge",
        "dba2.data.repositories.contract_repo.create_cap_hold",
        "dba2.data.repositories.player_repo.set_team_and_status",
        "dba2.data.repositories.draft_repo.set_current_team",
    }
)

# Per-mutator required identifiers (check 2): the AST expression passed for
# `teams=`/`players=` must MENTION every dotted-attribute string listed here
# (via `ast.unparse` containment) -- deliberately brittle against refactors,
# per §7.4 check 2's own stated intent.
RMLP_MUTATORS: dict[str, dict[str, tuple[str, ...]]] = {
    "dba2.services.roster_service.sign": {
        "teams": ("team_id",),
        "players": ("player_id",),
    },
    "dba2.services.roster_service.waive": {
        "teams": ("discovered.team_id",),
        "players": ("discovered.player_id",),
    },
    "dba2.services.roster_service.release": {
        "teams": ("discovered.team_id",),
        "players": ("discovered.player_id",),
    },
    "dba2.services.gm_service.apply_signing": {
        "teams": ("execution.team_id",),
        "players": ("execution.player_id",),
    },
    "dba2.services.gm_service.apply_trade": {
        "teams": ("execution.team_a_id", "execution.team_b_id"),
        "players": ("execution.players_a_to_b", "execution.players_b_to_a"),
    },
}

# check 4: (qualname -> required marker text). "from caller" markers need at
# least one RMLP_MUTATORS caller of this function; "from callee" markers
# (Addendum A2/A3's `apply_waivers`) need this function's OWN body to call an
# RMLP_MUTATORS member.
RMLP_LOCK_INHERITORS: dict[str, str] = {
    "dba2.services.gm_service._move_active_contract": "RMLP: inherits locks from caller",
    "dba2.services.gm_service.apply_waivers": (
        "RMLP: inherits locks from callee (roster_service.waive)"
    ),
}

RMLP_ENTRY_POINTS: frozenset[str] = frozenset(
    {
        "dba2.services.gm_command_service.waive_player",
        "dba2.services.gm_command_service.release_player",
        "dba2.services.gm_command_service.sign_free_agent",
        "dba2.services.gm_service.run_phase_hook",
        # The trade-offer review surface (docs/design/phase-5-trade-offer-
        # review-surface.md §3, §7.5): four gate-openers, each in its OWN body.
        # `veto` is one although it never calls `acquire` -- `open_league_gate`
        # issues its `SET LOCAL lock_timeout` (it is no longer the codebase's
        # only issuer: P5-98.g lifted the statement into `roster_lock.set_lock_
        # timeout` for the draft's gate-less `drafts` lock), and a gate-less
        # veto would wait forever behind a slow accept. `list_offers` is not: it
        # opens no gate (it is a read; its one write is the expiry sweep).
        "dba2.services.offer_service.propose",
        "dba2.services.offer_service.respond",
        "dba2.services.offer_service.counter",
        "dba2.services.offer_service.veto",
        # `/draft`'s service half (docs/design/phase-5-stage-d-prime-draft.md,
        # Decision P5-98.g). `veto`'s precedent exactly: a draft pick mutates
        # no RMLP table -- `apply_draft` writes `draft_picks.used_for_player_
        # id`, a `draft_selections` row and the advisory pointer, and moves no
        # player and writes no contract -- so the gate is taken for its `SET
        # LOCAL lock_timeout` and its post-gate `(phase, season)` read. Its
        # SECOND transaction (the AI resume) opens no gate at all, which is
        # what check 9 pins on `run_draft_cycle`.
        "dba2.services.gm_command_service.draft_pick",
    }
)
# run_phase_hook's own per-branch gate count (§7.4 check 6's "per-branch
# count assertion") -- exactly 3 roster-mutating branches (trade, FREE_
# AGENCY, WAIVERS) open the gate; RESIGN_WINDOW/DRAFT do not.
_RUN_PHASE_HOOK_EXPECTED_GATE_CALLS = 3

RMLP_FUTURE_ADOPTERS: frozenset[str] = frozenset(
    {
        "dba2.data.repositories.contract_repo.set_option_decision",
        "dba2.data.repositories.contract_repo.create_cap_hold",
    }
)


# ---------------------------------------------------------------------------
# Indexing: parse every dba2/ file once, resolve every call in every
# function body to a qualified target (or None if unresolvable).
# ---------------------------------------------------------------------------


def _module_dotted_name(path: Path) -> str:
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _flatten_attr(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _flatten_attr(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _from_imports_in_file(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """`local_name -> (source_module, imported_name)` for every `from X
    import Y [as Z]` in `tree` -- absolute imports only (nothing under
    `dba2/services/` or `dba2/data/repositories/` uses a relative import
    for anything this file needs to resolve, verified by grep)."""
    imports: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                imports[local] = (node.module, alias.name)
    return imports


@dataclass(frozen=True)
class ResolvedCall:
    target: str | None
    lineno: int
    node: ast.Call


@dataclass
class FunctionInfo:
    qualname: str
    node: ast.AST
    path: Path
    docstring: str
    calls: list[ResolvedCall] = field(default_factory=list)


def _resolve_call(
    call: ast.Call,
    *,
    from_imports: dict[str, tuple[str, str]],
    same_module_functions: set[str],
    this_module: str,
) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        name = func.id
        if name in same_module_functions:
            return f"{this_module}.{name}"
        if name in from_imports:
            source_module, imported_name = from_imports[name]
            return f"{source_module}.{imported_name}"
        return None
    if isinstance(func, ast.Attribute):
        dotted = _flatten_attr(func)
        if dotted is None:
            return None
        first, _, rest = dotted.partition(".")
        if not rest:
            return None
        if first in from_imports:
            source_module, imported_name = from_imports[first]
            return f"{source_module}.{imported_name}.{rest}"
        return None
    return None


def _iter_defs_with_dotted_suffix(
    body: list[ast.stmt], prefix: str = ""
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every function/method reachable from `body`, recursing into
    `ClassDef` bodies (B1 fix, see this file's module docstring) but NOT
    into function bodies -- a closure defined inside another function is
    never itself a call site this protocol needs to see; every real call
    site in this codebase is a module-level function or a class method.
    Returns `(dotted_suffix, node)` pairs, `dotted_suffix` being the name
    path from the module root (`"RosterCommands.waive"` for a method,
    `"sign"` for a top-level function) -- the caller prefixes this with
    the module's own dotted name to get the full qualname."""
    results: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = f"{prefix}.{node.name}" if prefix else node.name
            results.append((name, node))
        elif isinstance(node, ast.ClassDef):
            class_prefix = f"{prefix}.{node.name}" if prefix else node.name
            results.extend(_iter_defs_with_dotted_suffix(node.body, class_prefix))
    return results


def _index_functions() -> dict[str, FunctionInfo]:
    functions: dict[str, FunctionInfo] = {}
    py_files = sorted(DBA2_DIR.rglob("*.py"))
    parsed: list[tuple[Path, str, ast.Module, set[str]]] = []
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        this_module = _module_dotted_name(path)
        # Deliberately TOP-LEVEL only -- see this file's module docstring
        # ("Indexing recurses into class bodies") for why a method is never
        # a candidate for bare-name same-module call resolution.
        top_level_funcs = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        parsed.append((path, this_module, tree, top_level_funcs))

    for path, this_module, tree, top_level_funcs in parsed:
        from_imports = _from_imports_in_file(tree)
        for name_suffix, node in _iter_defs_with_dotted_suffix(tree.body):
            qualname = f"{this_module}.{name_suffix}"
            docstring = ast.get_docstring(node) or ""
            info = FunctionInfo(qualname=qualname, node=node, path=path, docstring=docstring)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    target = _resolve_call(
                        sub,
                        from_imports=from_imports,
                        same_module_functions=top_level_funcs,
                        this_module=this_module,
                    )
                    info.calls.append(ResolvedCall(target=target, lineno=sub.lineno, node=sub))
            functions[qualname] = info
    return functions


# ---------------------------------------------------------------------------
# Checks 1, 2, 3 (+3b), 5 -- factored as pure functions over the indexed
# map, per §7.4 check 7's own requirement (the mutation twin needs to feed
# these a mutated in-memory map, not touch the filesystem).
# ---------------------------------------------------------------------------


def _check_adoption(functions: dict[str, FunctionInfo]) -> list[str]:
    """Check 1 -- every RMLP_MUTATORS member calls `acquire`, before the
    first call to any WRITE_SHAPED member in the same body."""
    violations: list[str] = []
    for qualname in RMLP_MUTATORS:
        info = functions.get(qualname)
        if info is None:
            violations.append(f"{qualname}: not found in the indexed dba2/ functions at all")
            continue
        acquire_calls = [c for c in info.calls if c.target == ACQUIRE]
        if not acquire_calls:
            violations.append(f"{qualname}: no call to {ACQUIRE} found in its body")
            continue
        first_acquire_line = min(c.lineno for c in acquire_calls)
        write_calls = [c for c in info.calls if c.target in WRITE_SHAPED]
        if write_calls:
            first_write_line = min(c.lineno for c in write_calls)
            if first_write_line < first_acquire_line:
                violations.append(
                    f"{qualname}: a WRITE_SHAPED call (line {first_write_line}) precedes "
                    f"its acquire() call (line {first_acquire_line})"
                )
    return violations


def _is_empty_literal(node: ast.AST) -> bool:
    return (
        (isinstance(node, (ast.List, ast.Tuple, ast.Set)) and not node.elts)
        or (isinstance(node, ast.Dict) and not node.keys)
        or (
            isinstance(node, ast.Call)
            and _flatten_attr(node.func) in {"set", "list", "tuple", "dict"}
            and not node.args
        )
    )


def _check_lock_shape(functions: dict[str, FunctionInfo]) -> list[str]:
    """Check 2 -- for each mutator, the `acquire(...)` call's `teams=`/
    `players=` keyword arguments must both be present, non-empty-literal,
    and each must (via source-text containment) mention every required
    identifier for that mutator. The `league_id=` keyword must be the bare
    `Name` node `league_id` (Addendum A2's uniform rule across all five)."""
    violations: list[str] = []
    for qualname, required in RMLP_MUTATORS.items():
        info = functions.get(qualname)
        if info is None:
            continue  # already reported by check 1
        acquire_calls = [c for c in info.calls if c.target == ACQUIRE]
        if not acquire_calls:
            continue  # already reported by check 1
        call = acquire_calls[0].node
        kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}

        league_id_value = kwargs.get("league_id")
        if league_id_value is None:
            violations.append(f"{qualname}: acquire() call has no league_id= keyword")
        elif not (isinstance(league_id_value, ast.Name) and league_id_value.id == "league_id"):
            violations.append(
                f"{qualname}: acquire()'s league_id= must be the bare Name 'league_id', "
                f"got {ast.unparse(league_id_value)!r}"
            )

        for arg_name, required_identifiers in (
            ("teams", required["teams"]),
            ("players", required["players"]),
        ):
            value = kwargs.get(arg_name)
            if value is None:
                violations.append(f"{qualname}: acquire() call has no {arg_name}= keyword")
                continue
            if _is_empty_literal(value):
                violations.append(
                    f"{qualname}: acquire()'s {arg_name}= is an empty literal "
                    f"({ast.unparse(value)})"
                )
                continue
            source = ast.unparse(value)
            for identifier in required_identifiers:
                if identifier not in source:
                    violations.append(
                        f"{qualname}: acquire()'s {arg_name}= ({source!r}) does not "
                        f"mention required identifier {identifier!r}"
                    )
    return violations


def _check_closure(functions: dict[str, FunctionInfo]) -> list[str]:
    """Check 3 (REWRITTEN, Addendum A4) -- CALLERS := every function whose
    body calls a WRITE_SHAPED member, minus WRITE_SHAPED members themselves
    (check 3b's flat-writer assumption). Assertion: CALLERS ==
    RMLP_MUTATORS ∪ RMLP_LOCK_INHERITORS. RMLP_FUTURE_ADOPTERS is
    deliberately NOT in this equality -- its members are WRITERS, not
    callers of writers, so by construction they're absent from CALLERS;
    check 5 carries their entire enforcement burden.

    **Disclosed mechanical gap-fill (not in either source document, found
    at build time): a "from callee" lock inheritor (Addendum A2/A3's
    `apply_waivers`) is, BY CONSTRUCTION, never itself a caller of a
    WRITE_SHAPED member -- its entire reason for existing in RMLP_LOCK_
    INHERITORS is that it delegates to an RMLP_MUTATOR (`roster_service.
    waive`) instead of calling WRITE_SHAPED directly. Addendum A4's own
    equality (`CALLERS == RMLP_MUTATORS ∪ RMLP_LOCK_INHERITORS`), read
    literally, is therefore inconsistent with Addendum A2's placement of
    `apply_waivers` in `RMLP_LOCK_INHERITORS` -- the equality would demand
    `apply_waivers` appear in `CALLERS`, which it structurally cannot.
    Resolved here by excluding "from callee" members from the expected
    side of this specific equality (they remain fully covered by check
    4's own marker + call-graph enforcement, which is the check actually
    designed for their shape) -- "from caller" members (`_move_active_
    contract`) are unaffected and still required to appear in CALLERS."""
    callers = {
        qualname
        for qualname, info in functions.items()
        if qualname not in WRITE_SHAPED and any(c.target in WRITE_SHAPED for c in info.calls)
    }
    caller_style_inheritors = {
        qualname
        for qualname, marker in RMLP_LOCK_INHERITORS.items()
        if marker.startswith("RMLP: inherits locks from caller")
    }
    expected = set(RMLP_MUTATORS) | caller_style_inheritors
    missing_from_tables = callers - expected
    listed_but_not_real_callers = expected - callers
    violations: list[str] = []
    if missing_from_tables:
        violations.append(
            "functions call a WRITE_SHAPED member but are in none of "
            f"RMLP_MUTATORS/RMLP_LOCK_INHERITORS: {sorted(missing_from_tables)}"
        )
    if listed_but_not_real_callers:
        violations.append(
            "functions listed in RMLP_MUTATORS/RMLP_LOCK_INHERITORS but do not actually "
            f"call any WRITE_SHAPED member: {sorted(listed_but_not_real_callers)}"
        )
    return violations


def _check_flat_writers(functions: dict[str, FunctionInfo]) -> list[str]:
    """Check 3b -- no WRITE_SHAPED member's body calls another WRITE_SHAPED
    member. This is the precondition that makes check 3's "minus WRITE_
    SHAPED members themselves" exclusion safe rather than a loophole."""
    violations: list[str] = []
    for qualname in WRITE_SHAPED:
        info = functions.get(qualname)
        if info is None:
            continue
        for call in info.calls:
            if call.target in WRITE_SHAPED:
                violations.append(f"{qualname} calls another WRITE_SHAPED member: {call.target}")
    return violations


def _check_future_adopters(functions: dict[str, FunctionInfo]) -> list[str]:
    """Check 5 -- every RMLP_FUTURE_ADOPTERS member must (precondition,
    Addendum A4) be a member of WRITE_SHAPED, and must have ZERO callers
    under `dba2/`."""
    violations: list[str] = []
    for qualname in RMLP_FUTURE_ADOPTERS:
        if qualname not in WRITE_SHAPED:
            violations.append(
                f"{qualname}: listed in RMLP_FUTURE_ADOPTERS but not in WRITE_SHAPED "
                "(Addendum A4 precondition)"
            )
        callers = [
            caller_qualname
            for caller_qualname, info in functions.items()
            if caller_qualname != qualname
            and any(c.target == qualname for c in info.calls)
        ]
        if callers:
            violations.append(
                f"{qualname}: listed as a future adopter (zero callers expected) but is "
                f"actually called by: {sorted(callers)}"
            )
    return violations


# ---------------------------------------------------------------------------
# Checks 4, 6, 8 -- operate directly on the real AST nodes (not exercised by
# check 7's twin, which is scoped to checks 1/2/3/5 per the design's own
# explicit twin list).
# ---------------------------------------------------------------------------


def _check_lock_inheritance(functions: dict[str, FunctionInfo]) -> list[str]:
    """Check 4 -- a function may be in RMLP_LOCK_INHERITORS only if (a) its
    docstring contains the required marker text, (b) the call-graph
    condition matching that marker's shape holds, and (c) it does not
    itself call open_league_gate."""
    violations: list[str] = []
    for qualname, marker in RMLP_LOCK_INHERITORS.items():
        info = functions.get(qualname)
        if info is None:
            violations.append(f"{qualname}: not found in the indexed dba2/ functions at all")
            continue
        if marker not in info.docstring:
            violations.append(f"{qualname}: docstring missing required marker {marker!r}")

        if marker.startswith("RMLP: inherits locks from caller"):
            has_mutator_caller = any(
                any(c.target == qualname for c in mutator_info.calls)
                for mutator_qualname, mutator_info in functions.items()
                if mutator_qualname in RMLP_MUTATORS
            )
            if not has_mutator_caller:
                violations.append(
                    f"{qualname}: 'from caller' marker but no RMLP_MUTATORS member calls it"
                )
        elif marker.startswith("RMLP: inherits locks from callee"):
            calls_a_mutator = any(c.target in RMLP_MUTATORS for c in info.calls)
            if not calls_a_mutator:
                violations.append(
                    f"{qualname}: 'from callee' marker but its own body calls no "
                    "RMLP_MUTATORS member"
                )
        else:
            violations.append(f"{qualname}: marker {marker!r} matches neither accepted form")

        if any(c.target == OPEN_LEAGUE_GATE for c in info.calls):
            violations.append(f"{qualname}: a lock inheritor must never call open_league_gate")
    return violations


def _statement_containing(node: ast.AST, tree: ast.AST) -> ast.stmt | None:
    """The smallest enclosing statement of `node` within `tree`, found via
    a parent-tracking walk -- used by check 6 to test whether an
    `open_league_gate` call is a DIRECT child statement of an `async with`
    body (not nested inside an `if`/`for` inside it)."""
    parent_of: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_of[id(child)] = parent
    current: ast.AST = node
    while not isinstance(current, ast.stmt):
        parent = parent_of.get(id(current))
        if parent is None:
            return None
        current = parent
    return current


def _check_gate_entry_points(functions: dict[str, FunctionInfo]) -> list[str]:
    """Check 6 -- every `open_league_gate` call in `dba2/` must be inside a
    function named in RMLP_ENTRY_POINTS, must be a direct child statement
    of a non-nested `async with transaction(...)` body, and no
    RMLP_MUTATORS member may call it. Plus `run_phase_hook`'s own
    per-branch count assertion."""
    violations: list[str] = []
    run_phase_hook_gate_count = 0
    for qualname, info in functions.items():
        gate_calls = [c for c in info.calls if c.target == OPEN_LEAGUE_GATE]
        if not gate_calls:
            continue
        if qualname in RMLP_MUTATORS:
            violations.append(
                f"{qualname}: an RMLP_MUTATORS member must never call open_league_gate"
            )
            continue
        if qualname not in RMLP_ENTRY_POINTS:
            violations.append(
                f"{qualname}: calls open_league_gate but is not in RMLP_ENTRY_POINTS"
            )
            continue
        if qualname == "dba2.services.gm_service.run_phase_hook":
            run_phase_hook_gate_count += len(gate_calls)
        for call in gate_calls:
            stmt = _statement_containing(call.node, info.node)
            if stmt is None:
                violations.append(f"{qualname}: could not locate the statement for a gate call")
                continue
            # Find the nearest enclosing AsyncWith whose body directly
            # contains `stmt`.
            enclosing_with: ast.AsyncWith | None = None
            for sub in ast.walk(info.node):
                if isinstance(sub, ast.AsyncWith) and stmt in sub.body:
                    enclosing_with = sub
                    break
            if enclosing_with is None:
                violations.append(
                    f"{qualname}: open_league_gate call is not a direct child statement of "
                    "an async with block"
                )
                continue
            # Assert that AsyncWith is not itself nested inside another
            # AsyncWith within the same function.
            nested_inside_another = False
            for outer in ast.walk(info.node):
                if (
                    isinstance(outer, ast.AsyncWith)
                    and outer is not enclosing_with
                    and any(
                        enclosing_with is inner or enclosing_with in ast.walk(inner)
                        for inner in outer.body
                    )
                ):
                    nested_inside_another = True
            if nested_inside_another:
                violations.append(
                    f"{qualname}: open_league_gate's async with block is nested inside "
                    "another async with block in the same function"
                )
    if run_phase_hook_gate_count != _RUN_PHASE_HOOK_EXPECTED_GATE_CALLS:
        violations.append(
            f"run_phase_hook: expected {_RUN_PHASE_HOOK_EXPECTED_GATE_CALLS} "
            f"open_league_gate calls (trade/FREE_AGENCY/WAIVERS branches), found "
            f"{run_phase_hook_gate_count}"
        )
    return violations


def _check_run_draft_cycle_takes_no_gate(functions: dict[str, FunctionInfo]) -> list[str]:
    """Check 9 (Decision P5-98.g) -- `gm_service.run_draft_cycle`'s body
    contains ZERO `open_league_gate` calls.

    **Check 6 cannot see this, which is why it exists.** Check 6's count
    assertion sums gate calls lexically inside `run_phase_hook` only, and
    its other arm is satisfied the moment a builder adds a new gate-opener
    to `RMLP_ENTRY_POINTS` -- so a gate opened inside the cycle would pass
    both arms while breaking the thing that matters: the cycle runs inside
    `/draft`'s SECOND transaction, and `open_league_gate` may only be the
    first statement of an outermost one (an advisory lock first taken in a
    savepoint is released when that savepoint aborts, `roster_lock`'s F9).
    A draft pick also mutates no RMLP table, so the gate would be ceremony
    with a real failure mode attached.

    The function's absence is a violation too: this check is worthless if
    a rename silently turns it into a lookup that finds nothing."""
    info = functions.get(RUN_DRAFT_CYCLE)
    if info is None:
        return [
            f"{RUN_DRAFT_CYCLE}: not found under dba2/ -- this check cannot pin a function "
            "that is not there (was it renamed?)"
        ]
    gate_calls = [c for c in info.calls if c.target == OPEN_LEAGUE_GATE]
    if gate_calls:
        return [
            f"{RUN_DRAFT_CYCLE}: opens the league gate at line(s) "
            f"{[c.lineno for c in gate_calls]} -- it runs inside /draft's second transaction "
            "and mutates no RMLP table (Decision P5-98.g)"
        ]
    return []


def _transaction_blocks(node: ast.AST) -> list[ast.AsyncWith]:
    """Every `async with transaction(...)` block lexically inside `node`.
    Matched by `ast.unparse` on the context expression, check 2's own
    deliberately-brittle style: the two functions check 10 scopes over both
    write `async with transaction(ex) as conn:` verbatim, and a rename that
    breaks this match is a rename that should be read by a human."""
    blocks: list[ast.AsyncWith] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.AsyncWith):
            continue
        for item in sub.items:
            expr = item.context_expr
            if isinstance(expr, ast.Call) and ast.unparse(expr).startswith("transaction("):
                blocks.append(sub)
                break
    return blocks


def _is_set_lock_timeout_statement(stmt: ast.stmt) -> bool:
    """`stmt` IS `await <something>.set_lock_timeout(...)`, at the top level
    of whatever body it came from -- not a statement that merely contains
    such a call somewhere inside it (round-2 critic's M-B).

    Nothing here walks deeper than `Expr -> Await -> Call`, and that is the
    whole point: a `try:` wrapping the call and a lock-taking read together
    unparses to a string containing `set_lock_timeout`, so the substring
    test this replaced accepted exactly the arrangement it existed to
    forbid."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Await):
        return False
    call = stmt.value.value
    if not isinstance(call, ast.Call):
        return False
    dotted = _flatten_attr(call.func)
    return dotted is not None and dotted.split(".")[-1] == _SET_LOCK_TIMEOUT_MARKER


def _check_gateless_transactions_bound_their_waits(
    functions: dict[str, FunctionInfo],
) -> list[str]:
    """Check 10 (Decision P5-98.g, round-1 critic's B1) -- every
    GATELESS_LOCK_TRANSACTIONS member's `async with transaction(...)` block
    must have `await roster_lock.set_lock_timeout(...)` as the FIRST
    statement of its body, before any statement that can take a lock.

    **Why first and not merely present.** The statement bounds the waits of
    the statements that FOLLOW it: `SET LOCAL lock_timeout` issued after a
    blocking lock has already been requested does nothing for that lock.
    `_resume_the_ai_draft`'s very next statement is `check_claim`'s `leagues
    ... FOR SHARE`, which is precisely the lock the critic measured waiting
    8.5 seconds on.

    **Why the first statement must BE the call and not merely contain one**
    (round-2 critic's M-B): see `_is_set_lock_timeout_statement`. The
    `try`/`except` that makes the resume's failures into skipped cycles sits
    OUTSIDE its `async with` block, which is what keeps this shape
    satisfiable -- a builder who moves that `try` inside has to keep the
    timeout statement ahead of it.

    A member's absence is a violation too: a check that silently matches
    nothing after a rename is worse than no check."""
    violations: list[str] = []
    for qualname in sorted(GATELESS_LOCK_TRANSACTIONS):
        info = functions.get(qualname)
        if info is None:
            violations.append(
                f"{qualname}: not found under dba2/ -- check 10 cannot pin a function "
                "that is not there (was it renamed?)"
            )
            continue
        blocks = _transaction_blocks(info.node)
        if not blocks:
            violations.append(
                f"{qualname}: opens no transaction() block -- it is listed as a gate-less "
                "transaction that takes a lock, so either the listing or the code is wrong"
            )
            continue
        for block in blocks:
            first = block.body[0] if block.body else None
            if first is None or not _is_set_lock_timeout_statement(first):
                violations.append(
                    f"{qualname}: the transaction() block at line {block.lineno} does not "
                    f"AWAIT roster_lock.{_SET_LOCK_TIMEOUT_MARKER} as its FIRST statement "
                    f"(found: {'an empty body' if first is None else ast.unparse(first)!r}) "
                    "-- a gate-less transaction that takes a lock waits forever without it"
                )
    return violations


def _check_maps_lock_contention_decorator(functions: dict[str, FunctionInfo]) -> list[str]:
    """Check 8 (Addendum A3) -- every RMLP_MUTATORS member must carry the
    `@roster_lock.maps_lock_contention` decorator."""
    violations: list[str] = []
    for qualname in RMLP_MUTATORS:
        info = functions.get(qualname)
        if info is None:
            continue
        decorator_names = set()
        for dec in getattr(info.node, "decorator_list", []):
            dotted = _flatten_attr(dec)
            if dotted:
                decorator_names.add(dotted)
        if "roster_lock.maps_lock_contention" not in decorator_names:
            violations.append(
                f"{qualname}: missing @roster_lock.maps_lock_contention decorator "
                f"(found decorators: {sorted(decorator_names)})"
            )
    return violations


# ---------------------------------------------------------------------------
# Tests -- run every check against the REAL, current dba2/ tree.
# ---------------------------------------------------------------------------


def test_all_rmlp_mutators_call_acquire_before_any_write():
    functions = _index_functions()
    violations = _check_adoption(functions)
    assert not violations, f"RMLP check 1 (adoption) failed: {violations}"


def test_all_rmlp_mutators_have_a_non_trivial_correctly_shaped_lock_set():
    functions = _index_functions()
    violations = _check_lock_shape(functions)
    assert not violations, f"RMLP check 2 (lock shape) failed: {violations}"


def test_every_write_shaped_caller_is_in_a_table():
    functions = _index_functions()
    violations = _check_closure(functions)
    assert not violations, f"RMLP check 3 (closure) failed: {violations}"


def test_no_write_shaped_member_calls_another():
    functions = _index_functions()
    violations = _check_flat_writers(functions)
    assert not violations, f"RMLP check 3b (flat writers) failed: {violations}"


def test_lock_inheritors_are_correctly_marked_and_reachable():
    functions = _index_functions()
    violations = _check_lock_inheritance(functions)
    assert not violations, f"RMLP check 4 (lock inheritance) failed: {violations}"


def test_future_adopters_have_zero_callers():
    functions = _index_functions()
    violations = _check_future_adopters(functions)
    assert not violations, f"RMLP check 5 (future adopters) failed: {violations}"


def test_open_league_gate_is_called_only_from_entry_points_correctly_placed():
    functions = _index_functions()
    violations = _check_gate_entry_points(functions)
    assert not violations, f"RMLP check 6 (gate entry points) failed: {violations}"


def test_run_draft_cycle_never_opens_the_league_gate():
    functions = _index_functions()
    violations = _check_run_draft_cycle_takes_no_gate(functions)
    assert not violations, f"RMLP check 9 (run_draft_cycle takes no gate) failed: {violations}"


def test_every_gateless_locking_transaction_sets_its_own_lock_timeout_first():
    functions = _index_functions()
    violations = _check_gateless_transactions_bound_their_waits(functions)
    assert not violations, f"RMLP check 10 (gate-less lock_timeout) failed: {violations}"


def _mutated_copy(original: FunctionInfo, node: ast.AST) -> FunctionInfo:
    return FunctionInfo(
        qualname=original.qualname,
        node=node,
        path=original.path,
        docstring=original.docstring,
        calls=original.calls,
    )


def test_mutation_8_moving_the_resumes_lock_timeout_after_the_claim_read_fails_check_10():
    """Check 7's second twin for check 10 (round-2 critic's M-B), and the
    one the FIRST twin cannot stand in for.

    Deleting the call is the easy mutation. The real one is MOVING it: wrap
    the claim read in a `try` and put `set_lock_timeout` inside it, and that
    `try` becomes the block's first statement -- whose `ast.unparse` still
    CONTAINS the string `set_lock_timeout`, so a substring test on the first
    statement passes while the `leagues ... FOR SHARE` inside the same `try`
    runs before the timeout is set, unbounded. Check 10 therefore requires
    the first statement to BE `await roster_lock.set_lock_timeout(...)` --
    an `Expr(Await(Call(...)))` at the top level of the block, nothing
    deeper."""
    functions = _index_functions()
    qualname = "dba2.services.gm_command_service._resume_the_ai_draft"
    original = functions[qualname]
    mutated_node = copy.deepcopy(original.node)
    moved = False
    for block in _transaction_blocks(mutated_node):
        timeout_calls = [
            stmt for stmt in block.body if _SET_LOCK_TIMEOUT_MARKER in ast.unparse(stmt)
        ]
        rest = [stmt for stmt in block.body if stmt not in timeout_calls]
        if not timeout_calls or not rest:
            continue
        # The exact shape the substring test cannot see: the call is still
        # lexically first, but nested inside a `try` whose body takes a lock
        # before it.
        block.body = [
            ast.Try(
                body=[rest[0], *timeout_calls],
                handlers=[],
                orelse=[],
                finalbody=[],
            ),
            *rest[1:],
        ]
        ast.fix_missing_locations(mutated_node)
        moved = True
    assert moved, "the mutation did not apply, so this twin proves nothing"

    mutated = dict(functions)
    mutated[qualname] = _mutated_copy(original, mutated_node)
    violations = _check_gateless_transactions_bound_their_waits(mutated)
    assert violations, (
        "check 10 has no teeth -- moving set_lock_timeout behind a lock inside a `try` "
        "that is itself the block's first statement went uncaught"
    )


def test_mutation_7_deleting_the_resumes_lock_timeout_fails_check_10():
    """Check 7's twin for check 10, on the function that shipped without the
    statement: strip the `set_lock_timeout` call from an in-memory COPY of
    the real AST and the check must say so."""
    functions = _index_functions()
    qualname = "dba2.services.gm_command_service._resume_the_ai_draft"
    original = functions[qualname]
    mutated_node = copy.deepcopy(original.node)
    for block in _transaction_blocks(mutated_node):
        block.body = [
            stmt for stmt in block.body if _SET_LOCK_TIMEOUT_MARKER not in ast.unparse(stmt)
        ]
    mutated = dict(functions)
    mutated[qualname] = FunctionInfo(
        qualname=original.qualname,
        node=mutated_node,
        path=original.path,
        docstring=original.docstring,
        calls=original.calls,
    )
    violations = _check_gateless_transactions_bound_their_waits(mutated)
    assert violations, (
        "check 10 has no teeth -- deleting the resume's set_lock_timeout went uncaught"
    )


# ---------------------------------------------------------------------------
# Check 10, module-scoped (Stage E, phase-5-stage-e-advance.md P5-99.t.2, §5).
#
# `GATELESS_LOCK_TRANSACTIONS` is hand-curated, so it covers exactly what
# someone remembered to list. `season_service` is the one module where the rule
# is universal -- EVERY transaction it opens takes the `leagues` row lock and
# none opens a gate (P5-99.c.1) -- so this variant needs no list at all: it
# reads the module's own source and holds every `transaction()` block in it to
# check 10's shape. It is what covers the three checkpoint writers the design
# deliberately leaves unlisted, and whatever a later commit adds.
#
# It names no function in the module, only the module's file, so it lands with
# the first transaction `season_service` opens and never refers to a symbol a
# later commit creates.
# ---------------------------------------------------------------------------

SEASON_SERVICE_PATH = DBA2_DIR / "services" / "season_service.py"


def _blocks_with_any_transaction_spelling(tree: ast.AST) -> list[ast.AsyncWith]:
    """Every `async with` whose context expression calls anything named
    `transaction(` -- `transaction(pool)`, `db.transaction(pool)`,
    `conn.transaction()` alike. `_transaction_blocks` only recognises the first
    spelling, deliberately; comparing the two counts is what stops a rename
    from turning the module-scoped check into one that matches nothing."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncWith)
        and any("transaction(" in ast.unparse(item.context_expr) for item in node.items)
    ]


def _check_module_transactions_bound_their_waits(tree: ast.AST, *, label: str) -> list[str]:
    violations: list[str] = []
    recognised = _transaction_blocks(tree)
    every_spelling = _blocks_with_any_transaction_spelling(tree)
    if not every_spelling:
        violations.append(
            f"{label}: no transaction() block found at all -- the module-scoped check would "
            "pass vacuously (was the module renamed, or its transactions moved?)"
        )
    if len(every_spelling) != len(recognised):
        unmatched = [b.lineno for b in every_spelling if b not in recognised]
        violations.append(
            f"{label}: the transaction() block(s) at line(s) {unmatched} are spelled in a way "
            "this check does not recognise (only a bare `transaction(...)` is) -- a "
            "transaction it cannot see is one it cannot hold to `set_lock_timeout`-first"
        )
    for block in recognised:
        first = block.body[0] if block.body else None
        if first is None or not _is_set_lock_timeout_statement(first):
            violations.append(
                f"{label}: the transaction() block at line {block.lineno} does not AWAIT "
                f"roster_lock.{_SET_LOCK_TIMEOUT_MARKER} as its FIRST statement "
                f"(found: {'an empty body' if first is None else ast.unparse(first)!r}) -- "
                "every transaction season_service opens takes the leagues row lock, and the "
                "server default is wait-forever"
            )
    return violations


def _season_service_tree() -> ast.Module:
    source = SEASON_SERVICE_PATH.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(SEASON_SERVICE_PATH))


def test_every_season_service_transaction_sets_its_own_lock_timeout_first():
    violations = _check_module_transactions_bound_their_waits(
        _season_service_tree(), label="season_service"
    )
    assert not violations, f"RMLP check 10 (module-scoped, season_service) failed: {violations}"


def test_module_scoped_check_10_fires_on_an_unlisted_transaction_without_the_timeout():
    """The point of the module-scoped variant: a new private writer nobody
    remembered to add to `GATELESS_LOCK_TRANSACTIONS` is caught anyway."""
    tree = _season_service_tree()
    rogue = ast.parse(
        "async def _record_something(pool):\n"
        "    async with transaction(pool) as conn:\n"
        "        await conn.execute('UPDATE leagues SET name = name')\n"
    )
    tree.body.extend(rogue.body)
    violations = _check_module_transactions_bound_their_waits(tree, label="season_service")
    assert violations, (
        "the module-scoped check has no teeth -- an unlisted bare transaction passed"
    )


def test_module_scoped_check_10_fires_when_the_timeout_is_not_the_first_statement():
    tree = _season_service_tree()
    moved = False
    for block in _transaction_blocks(tree):
        if len(block.body) >= 2:
            block.body[0], block.body[1] = block.body[1], block.body[0]
            moved = True
            break
    assert moved, "the mutation did not apply, so this twin proves nothing"
    assert _check_module_transactions_bound_their_waits(tree, label="season_service"), (
        "the module-scoped check has no teeth -- a lock-taking statement ahead of "
        "set_lock_timeout passed"
    )


def test_module_scoped_check_10_fires_on_a_transaction_spelling_it_cannot_recognise():
    tree = _season_service_tree()
    tree.body.extend(
        ast.parse(
            "async def _record_something(conn):\n"
            "    async with conn.transaction():\n"
            "        await conn.execute('UPDATE leagues SET name = name')\n"
        ).body
    )
    assert _check_module_transactions_bound_their_waits(tree, label="season_service"), (
        "the module-scoped check has no teeth -- a `conn.transaction()` block is invisible to it"
    )


def test_module_scoped_check_10_fires_on_a_module_with_no_transactions():
    assert _check_module_transactions_bound_their_waits(
        ast.parse("async def f():\n    return 1\n"), label="season_service"
    ), "a module with nothing to check must fail loudly, not pass"


@pytest.mark.parametrize(
    "qualname",
    [
        "dba2.services.season_service._claim_league",
        "dba2.services.season_service._release_claim",
        "dba2.services.season_service._move_pointer",
        "dba2.services.season_service._read_pointer_locked",
        "dba2.services.season_service._postpone_game",
        "dba2.services.season_service._restore_game",
        "dba2.services.season_service._unlock_claim",
        "dba2.services.season_service._force_pointer",
        "dba2.services.gm_command_service.set_strategy",
        "dba2.services.gm_command_service.set_role",
        "dba2.services.gm_command_service.set_directive",
        "dba2.services.gm_command_service.set_timeline",
    ],
)
def test_mutation_moving_a_gateless_transactions_lock_timeout_down_one_line_fails_check_10(
    qualname,
):
    """The twin every `GATELESS_LOCK_TRANSACTIONS` member ships with (P5-99.t.2):
    swap the timeout with the statement after it in an in-memory COPY and the
    listed-member check must go red."""
    assert qualname in GATELESS_LOCK_TRANSACTIONS
    functions = _index_functions()
    original = functions[qualname]
    mutated_node = copy.deepcopy(original.node)
    swapped = False
    for block in _transaction_blocks(mutated_node):
        if len(block.body) >= 2 and _is_set_lock_timeout_statement(block.body[0]):
            block.body[0], block.body[1] = block.body[1], block.body[0]
            swapped = True
    assert swapped, "the mutation did not apply, so this twin proves nothing"
    mutated = dict(functions)
    mutated[qualname] = _mutated_copy(original, mutated_node)
    assert _check_gateless_transactions_bound_their_waits(mutated), (
        f"check 10 has no teeth on {qualname} -- moving set_lock_timeout below the next "
        "statement went uncaught"
    )


def test_all_rmlp_mutators_carry_the_maps_lock_contention_decorator():
    functions = _index_functions()
    violations = _check_maps_lock_contention_decorator(functions)
    assert not violations, f"RMLP check 8 (decorator) failed: {violations}"


# ---------------------------------------------------------------------------
# Check 7 -- the discipline test's own mutation twin. Four mutations (the
# three from revision 2's spec, plus Addendum A4's fourth), each applied to
# an in-memory COPY of the indexed map -- never touching the filesystem.
# ---------------------------------------------------------------------------


def test_mutation_1_deleting_the_acquire_call_from_waive_fails_check_1():
    functions = _index_functions()
    qualname = "dba2.services.roster_service.waive"
    original = functions[qualname]
    mutated = dict(functions)
    mutated[qualname] = FunctionInfo(
        qualname=original.qualname,
        node=original.node,
        path=original.path,
        docstring=original.docstring,
        calls=[c for c in original.calls if c.target != ACQUIRE],
    )
    violations = _check_adoption(mutated)
    assert violations, "check 1 has no teeth -- deleting waive()'s acquire() call went uncaught"


def test_mutation_2_emptying_apply_trades_lock_args_fails_check_2():
    functions = _index_functions()
    qualname = "dba2.services.gm_service.apply_trade"
    original = functions[qualname]
    empty_call_src = "roster_lock.acquire(conn, league_id=league_id, teams=[], players=[])"
    empty_call_node = ast.parse(empty_call_src, mode="eval").body
    assert isinstance(empty_call_node, ast.Call)
    mutated = dict(functions)
    mutated[qualname] = FunctionInfo(
        qualname=original.qualname,
        node=original.node,
        path=original.path,
        docstring=original.docstring,
        calls=[
            ResolvedCall(target=ACQUIRE, lineno=c.lineno, node=empty_call_node)
            if c.target == ACQUIRE
            else c
            for c in original.calls
        ],
    )
    violations = _check_lock_shape(mutated)
    assert violations, (
        "check 2 has no teeth -- emptying apply_trade's teams=/players= went uncaught"
    )


def test_mutation_3_an_unlisted_writer_caller_fails_check_3():
    functions = _index_functions()
    synthetic_qualname = "dba2.services._scratch.rogue_mutator"
    synthetic_call = ResolvedCall(
        target="dba2.data.repositories.player_repo.set_team_and_status",
        lineno=1,
        node=ast.parse("player_repo.set_team_and_status(conn, 1, 2, 'active')", mode="eval").body,
    )
    mutated = dict(functions)
    mutated[synthetic_qualname] = FunctionInfo(
        qualname=synthetic_qualname,
        node=ast.parse("async def rogue_mutator(): pass").body[0],
        path=Path("synthetic"),
        docstring="",
        calls=[synthetic_call],
    )
    violations = _check_closure(mutated)
    assert violations, (
        "check 3 has no teeth -- a new, untabled caller of a WRITE_SHAPED "
        "member went uncaught"
    )


def test_mutation_4_a_caller_of_a_future_adopter_fails_checks_3_and_5():
    """Addendum A4's fourth mutation twin -- proves checks 3 and 5 TOGETHER
    cover the case revision 2's single equality tried (and failed) to cover
    alone: the moment a future adopter gets a real caller, check 5's
    zero-callers assertion fails AND check 3's closure fails (the new
    caller is untabled)."""
    functions = _index_functions()
    future_adopter = "dba2.data.repositories.contract_repo.set_option_decision"
    synthetic_qualname = "dba2.services._scratch.rogue_option_caller"
    synthetic_call = ResolvedCall(
        target=future_adopter,
        lineno=1,
        node=ast.parse(
            "contract_repo.set_option_decision(conn, 1, 2026, OptionDecision.EXERCISED)",
            mode="eval",
        ).body,
    )
    mutated = dict(functions)
    mutated[synthetic_qualname] = FunctionInfo(
        qualname=synthetic_qualname,
        node=ast.parse("async def rogue_option_caller(): pass").body[0],
        path=Path("synthetic"),
        docstring="",
        calls=[synthetic_call],
    )
    closure_violations = _check_closure(mutated)
    future_adopter_violations = _check_future_adopters(mutated)
    assert closure_violations, (
        "check 3 has no teeth against a new caller of a future adopter"
    )
    assert future_adopter_violations, (
        "check 5 has no teeth against a new caller of a future adopter"
    )


# ---------------------------------------------------------------------------
# Mutation 5 -- B1 (this fix round's own critic-reproduced gap): a rogue
# writer or gate-opener hidden inside a CLASS METHOD, exactly the shape
# `dba2/bot/cogs/roster.py`'s `RosterCommands` (and the future trade-offer-
# accept cog the design's own §11 register names) already has. Unlike
# mutations 1-4 above, this writes a REAL scratch file under `dba2/` and
# calls the REAL `_index_functions()` -- the defect this proves was fixed
# lived in the INDEXER's own tree walk (`tree.body` only, never recursing
# into `ClassDef`), not in any check function, so a synthetic in-memory
# `FunctionInfo` (mutations 1-4's own technique) would prove nothing about
# whether the real indexer can even find a method in the first place.
# ---------------------------------------------------------------------------


def test_mutation_5_a_rogue_write_inside_a_class_method_fails_checks_3_and_6():
    """B1's live repro. A scratch module under `dba2/bot/cogs/` (the real
    directory the next mutation site will live in) defines a class with
    two methods: one that calls a `WRITE_SHAPED` member directly, with no
    protocol adoption (check 3's forbidden shape); one that calls
    `roster_lock.open_league_gate` directly, from outside `RMLP_ENTRY_
    POINTS` (check 6's forbidden shape). Before the indexer's B1 fix,
    `_index_functions()` would not even contain either method's qualname
    -- both checks would report zero violations, not because the rogue
    code was judged safe, but because neither check could see it at all."""
    scratch = DBA2_DIR / "bot" / "cogs" / "_rmlp_discipline_test_scratch.py"
    scratch.write_text(
        "from dba2.data.repositories import player_repo\n"
        "from dba2.services import roster_lock\n"
        "\n"
        "\n"
        "class RogueCog:\n"
        "    async def rogue_write(self, conn):\n"
        "        await player_repo.set_team_and_status(conn, 1, 2, 'active')\n"
        "\n"
        "    async def rogue_gate(self, conn, league_id):\n"
        "        await roster_lock.open_league_gate(\n"
        "            conn, league_id=league_id, mode='exclusive'\n"
        "        )\n",
        encoding="utf-8",
    )
    try:
        functions = _index_functions()
        write_qualname = "dba2.bot.cogs._rmlp_discipline_test_scratch.RogueCog.rogue_write"
        gate_qualname = "dba2.bot.cogs._rmlp_discipline_test_scratch.RogueCog.rogue_gate"
        assert write_qualname in functions, (
            "B1 unfixed -- the indexer still does not see class methods at all"
        )
        assert gate_qualname in functions, (
            "B1 unfixed -- the indexer still does not see class methods at all"
        )

        closure_violations = _check_closure(functions)
        assert any(write_qualname in v for v in closure_violations), (
            "check 3 has no teeth against a rogue WRITE_SHAPED call hidden inside a "
            "class method -- B1's disclosed gap"
        )

        gate_violations = _check_gate_entry_points(functions)
        assert any(gate_qualname in v for v in gate_violations), (
            "check 6 has no teeth against a class method calling open_league_gate "
            "directly -- B1's disclosed gap"
        )
    finally:
        scratch.unlink()


def test_mutation_5b_a_class_method_caller_of_a_future_adopter_fails_checks_3_and_5():
    """B1's live repro, mirroring mutation 4's shape (a caller of an
    `RMLP_FUTURE_ADOPTERS` member) but hidden inside a class method instead
    of a module-level function -- proves check 5, not only check 3, is
    also no longer blind to this shape."""
    scratch = DBA2_DIR / "bot" / "cogs" / "_rmlp_discipline_test_scratch_5b.py"
    scratch.write_text(
        "from dba2.data.repositories import contract_repo\n"
        "\n"
        "\n"
        "class RogueOptionCog:\n"
        "    async def rogue_option_caller(self, conn):\n"
        "        await contract_repo.set_option_decision(conn, 1, 2026, 'exercised')\n",
        encoding="utf-8",
    )
    try:
        functions = _index_functions()
        qualname = (
            "dba2.bot.cogs._rmlp_discipline_test_scratch_5b.RogueOptionCog.rogue_option_caller"
        )
        assert qualname in functions, (
            "B1 unfixed -- the indexer still does not see class methods at all"
        )
        closure_violations = _check_closure(functions)
        future_adopter_violations = _check_future_adopters(functions)
        assert any(qualname in v for v in closure_violations), (
            "check 3 has no teeth against a class-method caller of a future adopter "
            "-- B1's disclosed gap"
        )
        assert any(
            "set_option_decision" in v for v in future_adopter_violations
        ), (
            "check 5 has no teeth against a class-method caller of a future adopter "
            "-- B1's disclosed gap"
        )
    finally:
        scratch.unlink()


# ---------------------------------------------------------------------------
# The trade-offer review surface (docs/design/phase-5-trade-offer-review-
# surface.md §7.5, §13.5). Check 6 above already pins the four entry points'
# gate placement. Three things in that slice have NO static guard from checks
# 1-8 -- the shared core's discipline, the decorators, and veto's no-`acquire`
# rule -- so each gets its own check here (functions over the indexed map, so
# each has an in-memory mutation twin, exactly like checks 1-3).
# ---------------------------------------------------------------------------

APPLY_TRADE = "dba2.services.gm_service.apply_trade"
OFFER_CORE = "dba2.services.offer_service._resolve_locked"
OFFER_LEGALITY = "dba2.services.offer_service._first_illegal_side"
CHECK_LEGALITY = "dba2.managers.decisions.trade._check_legality"
OFFER_ENTRY_POINTS = (
    "dba2.services.offer_service.propose",
    "dba2.services.offer_service.respond",
    "dba2.services.offer_service.counter",
    "dba2.services.offer_service.veto",
)
# `apply_trade` has exactly two production callers, and every one of them must
# run the AI's own `_check_legality` immediately upstream (§13.5): D4-1 stays
# open in `apply_trade` itself (it disclaims cap/roster-max/salary-matching
# legality), so the guarantee is a property of the CALLER SET, not of the
# applier. A third caller would silently reopen the gap.
EXPECTED_APPLY_TRADE_CALLERS: frozenset[str] = frozenset(
    {"dba2.services.gm_service.run_phase_hook", OFFER_CORE}
)
EXPECTED_OFFER_CORE_CALLERS: frozenset[str] = frozenset(
    {
        "dba2.services.offer_service.propose",
        "dba2.services.offer_service.respond",
        "dba2.services.offer_service.counter",
    }
)


def _callers_of(functions: dict[str, FunctionInfo], target: str) -> set[str]:
    return {q for q, info in functions.items() if any(c.target == target for c in info.calls)}


def _check_apply_trade_callers(functions: dict[str, FunctionInfo]) -> list[str]:
    """`apply_trade`'s caller set is pinned, and the offer core runs its
    legality gate before it -- and that gate really is `_check_legality`."""
    violations: list[str] = []
    callers = _callers_of(functions, APPLY_TRADE)
    if callers != EXPECTED_APPLY_TRADE_CALLERS:
        violations.append(
            f"apply_trade callers are {sorted(callers)}, expected exactly "
            f"{sorted(EXPECTED_APPLY_TRADE_CALLERS)} (§13.5) -- a new caller must run "
            "_check_legality immediately upstream and be added here deliberately"
        )
    core = functions.get(OFFER_CORE)
    if core is None:
        return [*violations, f"{OFFER_CORE}: not found in the indexed dba2/ functions"]
    apply_lines = [c.lineno for c in core.calls if c.target == APPLY_TRADE]
    legality_lines = [c.lineno for c in core.calls if c.target == OFFER_LEGALITY]
    if not apply_lines:
        violations.append(f"{OFFER_CORE}: does not call apply_trade")
    elif not legality_lines or min(legality_lines) > min(apply_lines):
        violations.append(
            f"{OFFER_CORE}: no {OFFER_LEGALITY} call precedes its apply_trade call "
            "(every apply_trade caller must run the AI's own legality gate first)"
        )
    legality = functions.get(OFFER_LEGALITY)
    if legality is None or not any(c.target == CHECK_LEGALITY for c in legality.calls):
        violations.append(f"{OFFER_LEGALITY}: does not call {CHECK_LEGALITY}")
    return violations


def _check_offer_core_callers(functions: dict[str, FunctionInfo]) -> list[str]:
    """`_resolve_locked` has exactly three callers -- `respond`, `propose`'s
    AI-partner path, `counter`'s AI-partner path. `veto` is not one."""
    callers = _callers_of(functions, OFFER_CORE)
    if callers != EXPECTED_OFFER_CORE_CALLERS:
        return [
            f"{OFFER_CORE} callers are {sorted(callers)}, expected exactly "
            f"{sorted(EXPECTED_OFFER_CORE_CALLERS)}"
        ]
    return []


OFFER_MODULE_PREFIX = "dba2.services.offer_service."
OFFER_VETO = "dba2.services.offer_service.veto"


def _reachable_targets(
    functions: dict[str, FunctionInfo], root: str
) -> dict[str, list[str]]:
    """Every call target reachable from `root`, mapped to the call path that
    reaches it. Follows ONLY same-module helpers (`offer_service`'s own
    functions): a helper there is code this slice owns, so a lock hidden one
    hop down is still `root`'s lock; following into `roster_lock`/`gm_service`
    would make the answer depend on code this check does not police."""
    reached: dict[str, list[str]] = {}
    visited = {root}
    frontier = [(root, [root])]
    while frontier:
        current, path = frontier.pop()
        info = functions.get(current)
        if info is None:
            continue
        for call in info.calls:
            if call.target is None:
                continue
            reached.setdefault(call.target, [*path, call.target])
            if (
                call.target.startswith(OFFER_MODULE_PREFIX)
                and call.target in functions
                and call.target not in visited
            ):
                visited.add(call.target)
                frontier.append((call.target, [*path, call.target]))
    return reached


def _check_veto_takes_no_team_locks(functions: dict[str, FunctionInfo]) -> list[str]:
    """`veto` may lock the offer row directly ONLY because it holds no tier-1
    or tier-2 lock (§6.3). It must never `acquire`, never reach the core, and
    never reach `apply_trade` -- directly or through any `offer_service`
    helper it calls (a lock moved into `_check_claim` is still veto's lock)."""
    if OFFER_VETO not in functions:
        return [f"{OFFER_VETO}: not found"]
    forbidden = {ACQUIRE, OFFER_CORE, APPLY_TRADE}
    return [
        f"veto reaches {target} via {' -> '.join(path)} -- it must never take a "
        "team/player lock or reach the accept core"
        for target, path in _reachable_targets(functions, OFFER_VETO).items()
        if target in forbidden
    ]


# §5.4: the tier-1/tier-2 locks are taken by `offer_service` ITSELF, before it
# reaches anything that writes. `apply_trade` also calls `acquire`, but inside
# its own SAVEPOINT -- and a rollback to that savepoint (the
# `ValidationError` -> `EXPIRED` path) releases the locks it took, so a service
# that leaned on the applier's acquire would write its terminal row unlocked.
OFFER_LOCK_FOR_ANSWER = "dba2.services.offer_service._lock_offer_for_answer"
OFFER_WRITE_TERMINAL = "dba2.services.offer_service._write_terminal"
OFFER_INSERT = "dba2.services.offer_service._insert_offer"
# Reaching any of these is what the locks exist to precede.
_LOCKED_ONLY_REACHES = frozenset({OFFER_CORE, APPLY_TRADE, OFFER_WRITE_TERMINAL, OFFER_INSERT})
OFFER_ACQUIRERS = ("dba2.services.offer_service.propose", OFFER_LOCK_FOR_ANSWER)
OFFER_LOCK_CALLERS = (
    "dba2.services.offer_service.respond",
    "dba2.services.offer_service.counter",
)


def _is_unconditional_awaited_statement(call: ast.Call, func: ast.AST) -> bool:
    """`call` is the awaited value of a statement (`x = await f()` / `await
    f()`) whose every enclosing statement up to `func` is a `with`/`async with`
    -- i.e. it runs on every path, not inside an `if`/`try`/loop."""
    parent_of: dict[int, ast.AST] = {}
    for parent in ast.walk(func):
        for child in ast.iter_child_nodes(parent):
            parent_of[id(child)] = parent
    stmt = _statement_containing(call, func)
    if not isinstance(stmt, (ast.Assign, ast.Expr)):
        return False
    value = stmt.value
    if not (isinstance(value, ast.Await) and value.value is call):
        return False
    current: ast.AST = stmt
    while True:
        parent = parent_of.get(id(current))
        if parent is func:
            return True
        if not isinstance(parent, (ast.With, ast.AsyncWith)):
            return False
        current = parent


def _check_offer_tier_locks_are_taken_by_the_offer_service(
    functions: dict[str, FunctionInfo],
) -> list[str]:
    """`propose` and `_lock_offer_for_answer` each hold an unconditional
    `roster_lock.acquire` statement before any `_resolve_locked` /
    `apply_trade` / terminal-write / insert reach; `respond` and `counter`
    each call `_lock_offer_for_answer` the same way (they never acquire
    directly, so THEIR tier-1/2 locks are that call)."""
    violations: list[str] = []
    checks = [(q, ACQUIRE, "roster_lock.acquire") for q in OFFER_ACQUIRERS] + [
        (q, OFFER_LOCK_FOR_ANSWER, "_lock_offer_for_answer") for q in OFFER_LOCK_CALLERS
    ]
    for qualname, lock_target, lock_name in checks:
        info = functions.get(qualname)
        if info is None:
            violations.append(f"{qualname}: not found")
            continue
        lock_calls = [c for c in info.calls if c.target == lock_target]
        if not lock_calls:
            violations.append(
                f"{qualname}: no {lock_name} call -- tier-1/2 locks must be taken by "
                "offer_service itself, not left to apply_trade's own acquire inside its "
                "savepoint (§5.4)"
            )
            continue
        first = min(lock_calls, key=lambda c: c.lineno)
        if not _is_unconditional_awaited_statement(first.node, info.node):
            violations.append(
                f"{qualname}: the {lock_name} call (line {first.lineno}) is not an "
                "unconditional awaited statement -- some path reaches a write without the locks"
            )
        early = sorted(
            {
                c.target
                for c in info.calls
                if c.target in _LOCKED_ONLY_REACHES and c.lineno < first.lineno
            }
        )
        if early:
            violations.append(
                f"{qualname}: reaches {early} before its {lock_name} call (line {first.lineno})"
            )
    return violations


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _check_legality_verdict_gates_apply_trade(
    functions: dict[str, FunctionInfo],
) -> list[str]:
    """`_check_apply_trade_callers` proves a `_first_illegal_side` CALL precedes
    `apply_trade` -- not that its result is ever acted on. Require the result
    to be bound and tested by an `if` that sits between the call and
    `apply_trade` and whose own body (top level) returns or raises: delete the
    `return` in the failure branch and an illegal trade is applied anyway."""
    core = functions.get(OFFER_CORE)
    if core is None:
        return [f"{OFFER_CORE}: not found"]
    legality_calls = {id(c.node) for c in core.calls if c.target == OFFER_LEGALITY}
    apply_lines = [c.lineno for c in core.calls if c.target == APPLY_TRADE]
    if not apply_lines:
        return [f"{OFFER_CORE}: does not call apply_trade"]
    bound: set[str] = set()
    bind_line: int | None = None
    for node in ast.walk(core.node):
        if isinstance(node, ast.Assign) and id(node.value) in legality_calls:
            bound |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            bind_line = node.lineno if bind_line is None else min(bind_line, node.lineno)
    if not bound or bind_line is None:
        return [f"{OFFER_CORE}: the {OFFER_LEGALITY} result is not bound to a name"]
    for node in ast.walk(core.node):
        if (
            isinstance(node, ast.If)
            and bind_line < node.lineno < min(apply_lines)
            and _names_in(node.test) & bound
            and any(isinstance(s, (ast.Return, ast.Raise)) for s in node.body)
        ):
            return []
    return [
        f"{OFFER_CORE}: no `if` testing the {OFFER_LEGALITY} result, before apply_trade, "
        "returns or raises -- an illegal trade would be applied"
    ]


def _check_offer_entry_points_decorated(functions: dict[str, FunctionInfo]) -> list[str]:
    """All four entry points carry `@roster_lock.maps_lock_contention` (§6.2,
    BL-4): the statements this slice adds sit outside `roster_lock`'s own two
    mapping sites, so without it a 55P03 escapes as a raw asyncpg error. Check
    8 above requires the decorator on `RMLP_MUTATORS` members only and is
    silent about anyone else."""
    violations = []
    for qualname in OFFER_ENTRY_POINTS:
        info = functions.get(qualname)
        if info is None:
            violations.append(f"{qualname}: not found")
            continue
        names = {_flatten_attr(dec) for dec in getattr(info.node, "decorator_list", [])}
        if "roster_lock.maps_lock_contention" not in names:
            violations.append(f"{qualname}: missing @roster_lock.maps_lock_contention")
    return violations


def test_apply_trades_callers_are_pinned_and_the_offer_core_checks_legality_first():
    assert not _check_apply_trade_callers(_index_functions())


def test_the_offer_core_has_exactly_three_callers_and_veto_is_not_one():
    assert not _check_offer_core_callers(_index_functions())


def test_veto_never_acquires_or_reaches_the_accept_core():
    assert not _check_veto_takes_no_team_locks(_index_functions())


def test_the_four_offer_entry_points_carry_the_lock_contention_decorator():
    assert not _check_offer_entry_points_decorated(_index_functions())


def test_the_offer_core_never_opens_a_gate_and_the_entry_points_do():
    """Complements check 6 (which forbids a gate call anywhere outside
    `RMLP_ENTRY_POINTS`): the core must contain none, and each of the four
    entry points must contain one."""
    functions = _index_functions()
    assert not any(c.target == OPEN_LEAGUE_GATE for c in functions[OFFER_CORE].calls)
    for qualname in OFFER_ENTRY_POINTS:
        assert any(c.target == OPEN_LEAGUE_GATE for c in functions[qualname].calls), qualname


def _copy_with(
    info: FunctionInfo, *, calls: list[ResolvedCall] | None = None, node=None
) -> FunctionInfo:
    return FunctionInfo(
        qualname=info.qualname,
        node=node if node is not None else info.node,
        path=info.path,
        docstring=info.docstring,
        calls=calls if calls is not None else info.calls,
    )


def test_mutation_6_deleting_the_cores_legality_call_fails_the_apply_trade_check():
    functions = _index_functions()
    core = functions[OFFER_CORE]
    mutated = dict(functions)
    mutated[OFFER_CORE] = _copy_with(
        core, calls=[c for c in core.calls if c.target != OFFER_LEGALITY]
    )
    assert _check_apply_trade_callers(mutated), (
        "the apply_trade caller check has no teeth -- deleting the core's legality call "
        "went uncaught"
    )


def test_mutation_7_a_third_apply_trade_caller_fails_the_apply_trade_check():
    functions = _index_functions()
    synthetic = "dba2.services._scratch.rogue_trader"
    mutated = dict(functions)
    mutated[synthetic] = FunctionInfo(
        qualname=synthetic,
        node=ast.parse("async def rogue_trader(): pass").body[0],
        path=Path("synthetic"),
        docstring="",
        calls=[
            ResolvedCall(
                target=APPLY_TRADE,
                lineno=1,
                node=ast.parse("gm_service.apply_trade(conn, execution, 1, 2)", mode="eval").body,
            )
        ],
    )
    assert _check_apply_trade_callers(mutated), (
        "the apply_trade caller check has no teeth -- a third caller went uncaught"
    )


def test_mutation_8_a_veto_that_acquires_fails_the_veto_check():
    functions = _index_functions()
    veto = functions["dba2.services.offer_service.veto"]
    extra = ResolvedCall(
        target=ACQUIRE,
        lineno=1,
        node=ast.parse(
            "roster_lock.acquire(conn, league_id=1, teams=[], players=[])", mode="eval"
        ).body,
    )
    mutated = dict(functions)
    mutated["dba2.services.offer_service.veto"] = _copy_with(veto, calls=[*veto.calls, extra])
    assert _check_veto_takes_no_team_locks(mutated)


def test_mutation_9_a_veto_calling_the_core_fails_the_core_caller_check():
    functions = _index_functions()
    veto = functions["dba2.services.offer_service.veto"]
    extra = ResolvedCall(
        target=OFFER_CORE, lineno=1, node=ast.parse("_resolve_locked()", mode="eval").body
    )
    mutated = dict(functions)
    mutated["dba2.services.offer_service.veto"] = _copy_with(veto, calls=[*veto.calls, extra])
    assert _check_offer_core_callers(mutated)
    assert _check_veto_takes_no_team_locks(mutated)


def test_mutation_10_removing_the_decorator_from_an_entry_point_fails_the_decorator_check():
    functions = _index_functions()
    qualname = "dba2.services.offer_service.respond"
    info = functions[qualname]
    stripped = copy.deepcopy(info.node)
    stripped.decorator_list = []
    mutated = dict(functions)
    mutated[qualname] = _copy_with(info, node=stripped)
    violations = _check_offer_entry_points_decorated(mutated)
    assert any(qualname in v for v in violations)


def test_the_offer_service_takes_its_own_tier_locks_before_any_write():
    assert not _check_offer_tier_locks_are_taken_by_the_offer_service(_index_functions())


def test_the_legality_verdict_is_acted_on_before_apply_trade():
    assert not _check_legality_verdict_gates_apply_trade(_index_functions())


def _copy_mutating(
    info: FunctionInfo,
    *,
    doomed: Callable[[ast.stmt], bool],
    replace: Callable[[ast.stmt], list[ast.stmt]],
) -> FunctionInfo:
    """A mutated in-memory AST of `info`'s function: every statement `doomed`
    selects (judged on the ORIGINAL tree) is swapped for `replace(copy)` in a
    deep copy, and the call index is rebuilt over the surviving copied nodes.
    Nothing touches the filesystem."""
    copied = copy.deepcopy(info.node)
    mapping = {id(o): c for o, c in zip(ast.walk(info.node), ast.walk(copied), strict=True)}
    doomed_copies = {
        id(mapping[id(s)]) for s in ast.walk(info.node) if isinstance(s, ast.stmt) and doomed(s)
    }
    assert doomed_copies, "the mutation matched nothing -- the twin would prove nothing"
    for node in ast.walk(copied):
        for field_name in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field_name, None)
            if not isinstance(stmts, list):
                continue
            rewritten: list[ast.stmt] = []
            for stmt in stmts:
                rewritten.extend(replace(stmt) if id(stmt) in doomed_copies else [stmt])
            setattr(node, field_name, rewritten or [ast.Pass()])
    alive = {id(n) for n in ast.walk(copied)}
    calls = [
        ResolvedCall(target=c.target, lineno=c.lineno, node=mapping[id(c.node)])
        for c in info.calls
        if id(mapping[id(c.node)]) in alive
    ]
    return _copy_with(info, calls=calls, node=copied)


def _statement_wrapping_a_call_to(info: FunctionInfo, target: str) -> Callable[[ast.stmt], bool]:
    call_ids = {id(c.node) for c in info.calls if c.target == target}
    return lambda stmt: isinstance(stmt, (ast.Assign, ast.Expr)) and any(
        id(n) in call_ids for n in ast.walk(stmt)
    )


@pytest.mark.parametrize("qualname", OFFER_ACQUIRERS)
def test_mutation_11_deleting_the_acquire_statement_fails_the_tier_lock_check(qualname):
    functions = _index_functions()
    info = functions[qualname]
    mutated = dict(functions)
    mutated[qualname] = _copy_mutating(
        info, doomed=_statement_wrapping_a_call_to(info, ACQUIRE), replace=lambda _s: []
    )
    violations = _check_offer_tier_locks_are_taken_by_the_offer_service(mutated)
    assert any(qualname in v and "no roster_lock.acquire call" in v for v in violations), (
        f"the tier-lock check has no teeth -- deleting {qualname}'s acquire went uncaught"
    )


@pytest.mark.parametrize("qualname", OFFER_ACQUIRERS)
def test_mutation_11b_an_acquire_under_an_if_fails_the_tier_lock_check(qualname):
    functions = _index_functions()
    info = functions[qualname]
    mutated = dict(functions)
    mutated[qualname] = _copy_mutating(
        info,
        doomed=_statement_wrapping_a_call_to(info, ACQUIRE),
        replace=lambda s: [ast.If(test=ast.Name(id="maybe", ctx=ast.Load()), body=[s], orelse=[])],
    )
    violations = _check_offer_tier_locks_are_taken_by_the_offer_service(mutated)
    assert any(qualname in v and "unconditional" in v for v in violations)


@pytest.mark.parametrize("qualname", OFFER_LOCK_CALLERS)
def test_mutation_11c_dropping_the_answer_lock_call_fails_the_tier_lock_check(qualname):
    functions = _index_functions()
    info = functions[qualname]
    mutated = dict(functions)
    mutated[qualname] = _copy_mutating(
        info,
        doomed=_statement_wrapping_a_call_to(info, OFFER_LOCK_FOR_ANSWER),
        replace=lambda _s: [],
    )
    violations = _check_offer_tier_locks_are_taken_by_the_offer_service(mutated)
    assert any(qualname in v for v in violations)


def test_mutation_12_deleting_the_return_in_the_legality_failure_branch_fails_the_check():
    functions = _index_functions()
    core = functions[OFFER_CORE]
    legality_ids = {id(c.node) for c in core.calls if c.target == OFFER_LEGALITY}
    bound = {
        t.id
        for n in ast.walk(core.node)
        if isinstance(n, ast.Assign) and id(n.value) in legality_ids
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    doomed_returns = {
        id(s)
        for n in ast.walk(core.node)
        if isinstance(n, ast.If) and _names_in(n.test) & bound
        for s in n.body
        if isinstance(s, ast.Return)
    }
    mutated = dict(functions)
    mutated[OFFER_CORE] = _copy_mutating(
        core, doomed=lambda s: id(s) in doomed_returns, replace=lambda _s: []
    )
    assert _check_legality_verdict_gates_apply_trade(mutated), (
        "the legality-verdict check has no teeth -- deleting the failure branch's return "
        "went uncaught"
    )


def test_mutation_13_a_veto_that_reaches_acquire_through_a_helper_fails_the_veto_check():
    functions = _index_functions()
    veto = functions[OFFER_VETO]
    helper = f"{OFFER_MODULE_PREFIX}_scratch_lock_helper"
    mutated = dict(functions)
    mutated[helper] = FunctionInfo(
        qualname=helper,
        node=ast.parse("async def _scratch_lock_helper(): pass").body[0],
        path=Path("synthetic"),
        docstring="",
        calls=[
            ResolvedCall(
                target=ACQUIRE,
                lineno=1,
                node=ast.parse(
                    "roster_lock.acquire(conn, league_id=1, teams=[], players=[])", mode="eval"
                ).body,
            )
        ],
    )
    hop = ResolvedCall(
        target=helper, lineno=1, node=ast.parse("_scratch_lock_helper()", mode="eval").body
    )
    mutated[OFFER_VETO] = _copy_with(veto, calls=[*veto.calls, hop])
    violations = _check_veto_takes_no_team_locks(mutated)
    assert any(helper in v and ACQUIRE in v for v in violations)


def test_mutation_14_a_gate_opened_inside_run_draft_cycle_fails_check_9():
    """Check 9's own twin, in this file's established style: the mutation is
    applied to an in-memory COPY of the indexed map, never to the filesystem.
    Without it, check 9 is an assertion nobody has watched fail -- and the
    reason it exists is precisely that the check it replaces (check 6's gate
    counter) passes against this same mutation."""
    functions = _index_functions()
    original = functions[RUN_DRAFT_CYCLE]
    assert not _check_run_draft_cycle_takes_no_gate(functions)
    gate_call = ast.parse(
        "roster_lock.open_league_gate(conn, league_id=1, mode=roster_lock.LockMode.SHARED)",
        mode="eval",
    ).body
    assert isinstance(gate_call, ast.Call)
    mutated = dict(functions)
    mutated[RUN_DRAFT_CYCLE] = FunctionInfo(
        qualname=original.qualname,
        node=original.node,
        path=original.path,
        docstring=original.docstring,
        calls=[*original.calls, ResolvedCall(target=OPEN_LEAGUE_GATE, lineno=1, node=gate_call)],
    )
    violations = _check_run_draft_cycle_takes_no_gate(mutated)
    assert violations, "check 9 has no teeth -- a gate opened inside run_draft_cycle went uncaught"
    # And the check it is NOT allowed to lean on: check 6 is blind to this
    # mutation (the gate call is not lexically inside run_phase_hook, and
    # the entry-point arm would be satisfied by one table edit), which is
    # the whole reason check 9 is a separate assertion.
    assert not any(RUN_DRAFT_CYCLE in v for v in _check_gate_entry_points(functions))


def test_mutation_14b_a_renamed_run_draft_cycle_fails_check_9():
    """The other way this check dies quietly: the function moves or is
    renamed, the lookup finds nothing, and an assertion over an empty set
    passes forever."""
    functions = _index_functions()
    mutated = {k: v for k, v in functions.items() if k != RUN_DRAFT_CYCLE}
    assert _check_run_draft_cycle_takes_no_gate(mutated), (
        "check 9 passes against a run_draft_cycle that does not exist"
    )
