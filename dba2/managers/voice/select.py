"""The phrase selector -- §6.3's six-step algorithm, Decision P3-16/P3-18
(docs/design/phase-3-inter-manager-society.md), §11 step 14.

**Phase 4 Stage D step 15 (Decision P4-20/P4-47/P4-64,
docs/design/phase-4-media-and-player-agency.md) extracted steps 3-6 --
trigger evaluation, the `template_id` sort, the repeat penalty, the
weighted draw -- into `voice/engine.py`, unchanged.** `select_phrase`
below keeps its exact pre-extraction signature and behavior (a committed
golden, `seeds/gm_phrase_selection_golden.json`, captured the prior
commit and replays element-wise identical against this version) and is
now a thin wrapper: it still owns step 2 (`_template_matches_dimensions`)
directly -- that step does NOT move, by design (Decision P4-64: it is the
one part of the algorithm that is genuinely negotiation-specific, and a
future media wrapper will supply its own analogous predicate to the same
`engine.select_template` instead of sharing this one) -- and delegates
steps 3-6 to `engine.select_template`.

`select_phrase` below is steps 2-6 of §6.3 (step 1, "compute the frame," is
`voice/frame.py::derive_frame`'s job -- the frame arrives here as an
argument, already built). **This module does NOT validate that a
`Trigger`'s `feature`/`op` name a real field on `NegotiationFrame`/a real
operator** -- Decision P3-18 assigns that load-time job to
`services/phrase_service.py::load_library()` (Decision P3-18's own step 15
of Phase 3's build order, not Phase 4's -- `load_library` has existed since
then), and duplicating it here would duplicate rather than anticipate that
step's own work. This module trusts its caller passes a library
`load_library()` has already validated.

**`slot_bindings` is always the empty mapping, on every selection,
regardless of whether the chosen template's own `slots` tuple is
non-empty.** Real slot-value resolution needs player/pick identities,
which are not part of the closed `NegotiationFrame` vocabulary (§6.1) and
are not available here -- that binding step is deferred to whenever
dialogue is wired into the service (step 15/16), matching Decision P3-16's
own split ("`select.py` returns a `PhraseSelection`... never a rendered
string; `services/phrase_service.py` interpolates"). No verify clause at
this step tests slot-binding content, and this module never raises on a
template whose `slots` is non-empty -- it simply has nothing to bind yet.

Zero I/O, per Decision P2-2(b)'s purity extension (`tests/unit/
test_layering.py`): no `dba2.data`, no `datetime.now()`, no bare
`random.Random()` construction (a `random.Random` instance is only ever
RECEIVED here, as `select_phrase`'s own `rng` parameter -- Decision P3-20's
independent phrase-RNG salt, `rng_for(league_rng_seed, "phrase", team_id,
season, phase, seed_subject, round_index)` per `dba2/core/rng.py:16-27`,
is the CALLER's job to build, exactly as `managers/` staying "stateless"
already requires for `use_counts` below), no `open()`, no `os.environ`.
Imports only `dba2.managers.voice.frame` (`NegotiationFrame`), `dba2.
managers.voice.engine` (the extracted steps 3-6 core, as of Phase 4 Stage
D step 15), and stdlib.
"""
from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from dba2.managers.voice import engine
from dba2.managers.voice.frame import NegotiationFrame

# ---------------------------------------------------------------------------
# Trigger / PhraseTemplate / PhraseLibrary / PhraseSelection -- pure shapes,
# no loading logic (Decision P3-17: managers/ may not call open(), so a
# JSON library physically cannot be loaded from inside this module).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    """One trigger predicate inside a `PhraseTemplate` -- §6.2's worked
    example: `{"feature": "top_in_share", "op": "gte", "value": 0.75}`.
    `feature` is read off a `NegotiationFrame` via `getattr` at evaluation
    time; `op` is one of the closed six-operator set §6.3 states
    (`lt, lte, gte, gt, eq, in`). **This class does not validate `feature`/
    `op` against any vocabulary** -- Decision P3-18 places that check at
    `phrase_service.py::load_library()`, step 15."""

    feature: str
    op: str
    value: Any


@dataclass(frozen=True)
class PhraseTemplate:
    """One phrase-library entry -- §6.2's worked example, field-for-field.
    An empty tuple on `philosophies`/`situations`/`windows` is a wildcard
    (matches every value of that dimension); a non-empty tuple is a
    closed allow-list, compared against the frame's own
    `philosophy`/`situation`/`window` by plain membership."""

    id: str
    philosophies: tuple[str, ...]
    situations: tuple[str, ...]
    windows: tuple[str, ...]
    triggers: tuple[Trigger, ...]
    weight: float
    text: str
    slots: tuple[str, ...]


PhraseLibrary = tuple[PhraseTemplate, ...]
"""The whole library, as an ordered collection of templates -- no loading
logic, no per-situation indexing: `select_phrase` below does its own
filtering/sorting per call, since `managers/` stays stateless (§6.3 step
5's own rule) and a library is small enough (tens of templates) that a
linear scan per selection costs nothing worth indexing against."""


@dataclass(frozen=True)
class PhraseSelection:
    """Decision P3-16's return shape -- `managers/` never emits English
    (Decision P2-18), so this names WHICH template was chosen and what
    (currently nothing, see this module's own docstring) its slots bind
    to, never a rendered string. `services/phrase_service.py` interpolates
    `text` against `slot_bindings` at render time, outside `managers/`."""

    template_id: str
    slot_bindings: Mapping[str, str]


# ---------------------------------------------------------------------------
# select_phrase -- §6.3 steps 2-6.
# ---------------------------------------------------------------------------

def _template_matches_dimensions(template: PhraseTemplate, frame: NegotiationFrame) -> bool:
    """§6.3 step 2 -- wildcard (`()`) or membership, on each of the three
    dimensions independently; all three must hold."""
    if template.philosophies and frame.philosophy not in template.philosophies:
        return False
    if template.situations and frame.situation not in template.situations:
        return False
    return not (template.windows and frame.window not in template.windows)


def select_phrase(
    frame: NegotiationFrame,
    library: PhraseLibrary,
    *,
    use_counts: Mapping[str, int],
    repeat_penalty: float,
    rng: random.Random,
) -> PhraseSelection:
    """§6.3 steps 2-6, in order -- step 1 (computing `frame`) is the
    caller's job (`voice/frame.py::derive_frame`), not this function's.
    **Steps 3-6 are `engine.select_template`'s job as of Phase 4 Stage D
    step 15 (Decision P4-20/P4-47/P4-64) -- this function now only owns
    step 2 directly and wraps the extracted core for the rest.** Every
    other contract below (parameter meaning, statelessness, RNG
    derivation) is unchanged from before that extraction.

    `use_counts` -- `{template_id: prior use count within this window}`,
    supplied by the caller each call; `managers/` stays stateless (§6.3
    step 5), so this function never accumulates its own state across
    calls. `repeat_penalty` is a REQUIRED keyword argument, never a
    `managers/`-scoped constant or a literal default -- it lives in
    `seeds/gm_phrases.json`, read by `phrase_service.py::load_library()`
    and passed in here by the caller, exactly so a phrase-library edit
    never bumps `GM_TUNING_VERSION` (Decision P3-17).

    `rng` is a `random.Random` the CALLER derives via `rng_for(league_
    rng_seed, "phrase", team_id, season, phase, seed_subject, round_index)`
    (`dba2/core/rng.py:16-27`) before calling this function -- Decision
    P3-20's independently-salted phrase stream. This function never
    constructs a `random.Random` itself (forbidden under `managers/`'s
    purity rule) and never reads any of the salt's own component pieces;
    receiving the already-derived stream is what keeps this module
    agnostic to how the salt tuple is built.

    Algorithm:

    2. **Filter on dimensions** (`_template_matches_dimensions`, still
       owned here, unmoved) -- `frame.philosophy`/`frame.situation`/
       `frame.window` against each template's `philosophies`/`situations`/
       `windows`, empty tuple as wildcard. Passed to `engine.
       select_template` as its `dimension_matches` parameter.
    3. **Filter on triggers** (`engine._trigger_holds`) -- every one of a
       template's `triggers` must hold; a template with no triggers always
       survives this step (the fallback shape, Decision P3-19).
    4. **Sort survivors by `template_id`** -- deterministic ordering,
       independent of `library`'s own iteration order (a tuple, but sorted
       here regardless so a future `PhraseLibrary` shape change cannot
       silently reintroduce iteration-order dependence) and independent of
       `use_counts`'s key order (never iterated, only looked up by key).
    5. **Apply the repeat penalty** -- `effective_weight = template.weight
       * (repeat_penalty ** use_counts.get(template.id, 0))`: multiplicative
       down-weighting per prior use, `1.0` at zero prior uses (weight
       unchanged), `repeat_penalty` itself at one prior use, and so on.
    6. **Weighted choice** -- `rng.choices(survivors, weights=...,
       k=1)[0]`, over the sorted survivor list with its penalty-adjusted
       weights; deterministic given the same `rng` state and the same
       (survivors, weights) sequence, which step 4's sort guarantees is
       itself deterministic.

    Steps 3-6 all live in `engine.select_template` now -- see that
    function's own docstring for the identical algorithm description,
    kept in sync by having only one place that actually implements it.

    `slot_bindings` is always `{}` -- see this module's own docstring for
    why real slot-value resolution is out of scope at this step."""
    chosen = engine.select_template(
        frame,
        library,
        dimension_matches=_template_matches_dimensions,
        use_counts=use_counts,
        repeat_penalty=repeat_penalty,
        rng=rng,
    )
    return PhraseSelection(template_id=chosen.id, slot_bindings={})
