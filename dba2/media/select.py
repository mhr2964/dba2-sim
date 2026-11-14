"""`dba2/media/select.py` -- the media persona/template selector and real
slot-binding resolution. Decisions P4-26 (persona archetypes, weighted
choice), P4-38 (the selection mechanism, not the named-writer layer --
that is step 19's job), P4-21/P4-22/P4-23/P4-54 (slot resolution, the
media RNG salt), docs/design/phase-4-media-and-player-agency.md §6.2, §11
Stage D step 18.

**Retyped to `StoryPackage` (docs/design/media-v2-newsroom.md MV-1b(c),
MV-1c, "Build order" step 3a).** Revisions 1-3's `StoryFrame`-typed spine
is confined to `dba2/media/detect/legacy.py`; every function below now
reads a `StoryPackage`'s typed `Fact`s instead of a `StoryFrame`'s fixed
field list. This is a forced consequence of the retype, not an optional
extra: once `SalienceCandidate`/`select_persona`/`select_media_template`
all operate on packages (MV-1b(a)/(c)), there is no `StoryFrame` left
downstream for `bind_slots` to read -- carrying both shapes through the
same pipeline "kept in sync by hand" is exactly MV-1b's own Option 1,
rejected for the eleven new kinds it could never reach a template for
(and it would be equally true here).

**What this module is, in relation to `managers/voice/select.py`.** This is
the "future media wrapper" `dba2/managers/voice/engine.py`'s own docstring
names explicitly. It reuses `engine.select_template` for the trigger-
evaluation/determinism-sort/repeat-penalty/weighted-draw core, and owns its
own "step 2" -- now `template_eligible` below, MV-1c.1/MV-1c.6's one
exported eligibility predicate -- exactly the way `managers/voice/
select.py::_template_matches_dimensions` owns negotiation's.

**`template_eligible` is the ONE definition of "this template is eligible
for this package in this voice at this tier"** (MV-1c.6) -- kind, voice,
angle, tier, and slot presence, all five clauses in one exported function.
It is exported (not private) because `tools/generate_media_corpus.py` must
evaluate the identical predicate rather than a partial, independently
reimplemented one (the same "no second seam" move Decision P4-103 Part A
and P4-107 item 6 already made, applied a third time).

**The trigger view (MV-1c.1)** -- `PackageTriggerView` wraps a
`StoryPackage` so `engine._trigger_holds`'s `getattr(frame, trigger.
feature)` can keep working unchanged: a key resolves by its GLOBALLY
declared `FactType` (`INT`/`FLOAT` -> `value_number`, `TEXT` -> `value_
text`), an unrecognized or absent name returns `None` (never raises), and
three namespaced pseudo-features (`package_story_kind`/`package_angle`/
`package_fact_count`) are exposed under a prefix no real fact key may use.

**The fallback is resolved by lookup, not trusted to survive a filter**
(MV-1c.2) -- `select_media_template` computes the real eligible-and-
triggered survivor set itself before ever calling `engine.select_template`;
an empty set returns the caller-supplied `fallback_by_kind[package.
story_kind]` directly rather than letting `engine.select_template`'s own
`rng.choices` raise `IndexError` on an empty population.

**Why persona selection and template selection are NOT one combined pure
function.** Decision P4-54 salts the TEMPLATE draw with the already-chosen
persona's own key, so the template draw's own RNG stream cannot be built
until the persona draw's OUTPUT is known -- `dba2/services/media_
service.py::select_and_bind_story` is the real orchestration sequencing
that builds `persona_rng`, calls `select_persona`, builds `template_rng`
from the result, then calls `select_media_template`/`bind_slots` in turn.

Zero I/O, the same purity rules every other module under `dba2/media/`
carries (Decision P4-31; `tests/unit/test_layering.py`): no `dba2.data`, no
`datetime.now()`, no bare `random.Random(`, no `open(`, no `os.environ`. A
`random.Random` is only ever RECEIVED, never constructed here. Imports
`dba2.managers.voice.engine` (the frame-agnostic selection core, unchanged
by this retype -- see its own module docstring), `dba2.domain.media`
(`StoryKind`), `dba2.domain.media_facts` (`StoryPackage`/`FactType`/the
registries), `dba2.domain.media_writer` (`FormatTier`), and stdlib.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import (
    FACT_KEYS_BY_KIND,
    UNIVERSAL_FACT_KEYS,
    Angle,
    FactType,
    StoryPackage,
)
from dba2.domain.media_writer import FormatTier
from dba2.managers.voice import engine

DRAMA_ANGLES = frozenset(
    {
        Angle.PROMISE_BROKEN, Angle.UNREST, Angle.HOLDOUT, Angle.TRADE_MADE,
        Angle.REVENGE, Angle.COLLAPSE, Angle.GOING_COLD, Angle.SCHEME_TENSION,
        Angle.ROSTER_IMBALANCE, Angle.RIVALRY,
    }
)
"""MV-3h's own closed set, verbatim -- the frozenset `hot_take`'s drama-bias
multiplier tests membership against (`media_service`'s own per-candidate
`persona_multipliers` composition: `1.6` if `package.angle in DRAMA_ANGLES`
else `1.0`, a later build step's wiring). A bias, not a filter -- `select_
persona` itself reads only the multiplier `media_service` hands it through
`persona_multipliers`; this module never inspects a package's `Angle`
directly, keeping the frozenset the one place the membership test lives."""

# ---------------------------------------------------------------------------
# MediaTrigger / MediaTemplate / MediaPersona -- pure shapes, no loading
# logic (Decision P3-17 item 2's rule, extended to media by Decision P4-21:
# `dba2/media/` may not call `open(`, so a JSON library physically cannot be
# loaded from inside this module). Declared independently from `managers/
# voice/select.py`'s `Trigger`/`PhraseTemplate` rather than reused -- P4-21
# is explicit that the two libraries are independently tunable, "not shared
# constants," and sharing a dataclass would couple their schemas the same
# way a shared knob would couple their behavior.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaTrigger:
    """Structurally identical to `managers/voice/select.py::Trigger` --
    `feature`/`op`/`value`, evaluated by `engine._trigger_holds` via
    `getattr(view, feature)` on the trigger view below -- but declared
    separately (see module docstring) rather than imported. `feature` must
    resolve to a real declared fact key (or a `package_`-prefixed pseudo-
    feature) and `op` one of the closed six-operator set; both are
    validated at load time (`dba2/services/media_service.py::load_media_
    library`), not here (Decision P3-18's split, extended)."""

    feature: str
    op: str
    value: Any


@dataclass(frozen=True)
class MediaTemplate:
    """One media-library entry. `kinds`/`voices` are each wildcard-or-
    membership dimensions (empty tuple matches everything), the same
    convention `managers/voice/select.py::PhraseTemplate.philosophies`
    uses -- `kinds` against `StoryPackage.story_kind.value`, `voices`
    against the ALREADY-DRAWN persona key. `angles`/`tiers` (MV-1b(c),
    revision 4) follow the identical wildcard-or-membership convention,
    against `StoryPackage.angle.value` and the caller-supplied `tier.value`
    respectively -- no shipped template declares either non-empty in this
    build step (the current eleven `StoryKind` members' own library), so
    both are always wildcard today. A template's `voices == ()` is the
    wildcard every per-kind fallback template uses; MV-1c.2 additionally
    requires a real fallback's `angles`/`tiers` to ALSO be `()` (checked at
    load, `media_service.py`, a later build step's validator work)."""

    id: str
    kinds: tuple[str, ...]
    voices: tuple[str, ...]
    triggers: tuple[MediaTrigger, ...]
    weight: float
    text: str
    slots: tuple[str, ...]
    angles: tuple[str, ...] = ()
    tiers: tuple[str, ...] = ()


MediaTemplateLibrary = tuple[MediaTemplate, ...]


@dataclass(frozen=True)
class MediaPersona:
    """One of Decision P4-53's exactly-five closed archetypes --
    `beat_writer`/`hot_take`/`analyst`/`insider`/`historian`. `story_kind_
    weights` is the per-story-kind preference vector Decision P4-26
    requires, keyed by `StoryKind.value` (a plain `str`, matching how the
    library's own JSON keys arrive): a kind absent from this mapping, or
    present at `0.0`, means this persona is never drawn for it. **This
    class does not enforce the closed five-key persona SET, or that every
    `StoryKind` has at least one persona with a positive weight** -- both
    are load-time library-wide invariants (`dba2/services/media_service.py`
    's own validators), not something one persona's own shape can check in
    isolation."""

    key: str
    story_kind_weights: Mapping[str, float]


@dataclass(frozen=True)
class StorySelection:
    """The final, real answer for one candidate story: which persona
    covered it, which template that persona/kind pair drew, and the real,
    resolved slot values -- Decision P4-22's `(template_id, slot_bindings)`
    shape, with `voice` added since media has a persona layer between
    "which kind" and "which template." Never a rendered string; `dba2/
    services/media_service.py::render_story` does the `.format()` step."""

    voice: str
    template_id: str
    slot_bindings: Mapping[str, str]


# ---------------------------------------------------------------------------
# select_persona -- Decision P4-26's "the persona is drawn by weighted
# choice over personas whose preference vector is non-zero for that kind."
# ---------------------------------------------------------------------------


def select_persona(
    story_kind: StoryKind,
    personas: Sequence[MediaPersona],
    *,
    rng: random.Random,
    persona_multipliers: Mapping[str, float] | None = None,
    eligible_personas: frozenset[str] | None = None,
) -> MediaPersona:
    """Weighted choice among every persona whose `story_kind_weights` names
    `story_kind` at a positive weight -- **retyped from `(frame, ...)` to
    `(story_kind, ...)`** (MV-1b(c)): this function only ever read `frame.
    kind.value`, so handing it the kind directly is the narrowest honest
    signature, and it is what lets a later build step's band/profile
    publication paths call it without inventing a stand-in package field.
    **Sorted by `.key` before the draw** -- the identical "deterministic
    regardless of the caller's own iteration order" reason `engine.select_
    template`'s own `template_id` sort exists for.

    Two new keyword-only parameters (MV-3h, not yet consumed by any real
    caller in this build step -- the hot-take band and cadence multipliers
    are a later build step's work): `persona_multipliers` scales an
    eligible persona's own weight (e.g. a phase's cadence multiplier),
    defaulting to `1.0` for any persona not named; `eligible_personas`,
    when given, additionally restricts the eligible set to that closed set
    of keys (e.g. a band candidate's `{"hot_take"}`) on top of the
    positive-weight filter -- both default to a no-op so every call in this
    build step behaves exactly as `select_persona(frame, ...)` did.

    Raises `AssertionError` if NO persona is eligible -- `dba2/services/
    media_service.py::load_media_library`'s own coverage validation
    (every `StoryKind` must have at least one persona with a positive
    weight) is what should make this unreachable in production; a caller
    that bypasses that loader (a hand-built adversarial library, e.g. in a
    test) can still reach it, and a loud, explanatory crash here is the
    correct behavior for that case -- silently returning an arbitrary
    persona for a kind nothing actually claims to cover would misattribute
    every story of that kind to a voice with no real editorial stake in it.
    """
    eligible = [
        persona
        for persona in personas
        if persona.story_kind_weights.get(story_kind.value, 0.0) > 0.0
        and (eligible_personas is None or persona.key in eligible_personas)
    ]
    if not eligible:
        raise AssertionError(
            f"no persona has a positive preference weight for StoryKind."
            f"{story_kind.name} -- load_media_library's own coverage validation "
            "should make this unreachable for a real library"
        )
    survivors = sorted(eligible, key=lambda persona: persona.key)
    weights = [
        persona.story_kind_weights[story_kind.value]
        * (1.0 if persona_multipliers is None else persona_multipliers.get(persona.key, 1.0))
        for persona in survivors
    ]
    return rng.choices(survivors, weights=weights, k=1)[0]


# ---------------------------------------------------------------------------
# The trigger view -- MV-1c.1's four rules, made real. Wraps a StoryPackage
# so `engine._trigger_holds`'s `getattr(frame, trigger.feature)` keeps
# working unchanged (that module never inspects its `frame` argument's
# shape itself, per its own docstring).
# ---------------------------------------------------------------------------

_PACKAGE_PSEUDO_FEATURES = frozenset({"package_story_kind", "package_angle", "package_fact_count"})

_DECLARED_FACT_TYPES: dict[str, FactType] = dict(UNIVERSAL_FACT_KEYS)
for _kind_keys in FACT_KEYS_BY_KIND.values():
    _DECLARED_FACT_TYPES.update(_kind_keys)
"""The GLOBAL declared type for every fact key across every kind
(`UNIVERSAL_FACT_KEYS` plus the union of `FACT_KEYS_BY_KIND`'s values) --
well-defined because a key has exactly one declared type across every kind
that declares it (MV-1c.1 rule 2, asserted by a pure unit test over the
registries themselves). Built once, at import time, so the trigger view
below does a single dict lookup per attribute read rather than scanning
every kind's entry on every trigger evaluation."""


class PackageTriggerView:
    """MV-1c.1's trigger view -- `engine.select_template` receives one of
    these as its `frame` argument, never the package itself, so a template
    author's `feature` name resolves through this class's own `__getattr__`
    rather than `StoryPackage`'s real attributes (`story_kind`/`angle`/
    `facts`/... are NOT what a trigger reads; a fact KEY is). **Exported
    (not `_`-prefixed)** because `tools/generate_media_corpus.py`'s own
    `record_stories` reconstruction must evaluate the SAME `engine.
    triggers_hold(template, view)` call `select_media_template` makes
    internally, over the SAME view shape -- the identical "no second seam"
    reasoning `template_eligible`'s own export already follows."""

    __slots__ = ("_package",)

    def __init__(self, package: StoryPackage) -> None:
        self._package = package

    def __getattr__(self, name: str) -> Any:
        """Rule 4: three pseudo-features, namespaced under `package_` so a
        real fact key (the registries assert none begins with that prefix)
        can never collide with one. Rule 1: type precedence is declared,
        never inferred -- `INT`/`FLOAT` reads `value_number`, `TEXT` reads
        `value_text`. Rule 3: absent is total -- an unrecognized name, or a
        recognized-but-absent-on-this-package key, both return `None`,
        never raise."""
        if name == "package_story_kind":
            return self._package.story_kind.value
        if name == "package_angle":
            return self._package.angle.value
        if name == "package_fact_count":
            return len(self._package.facts)
        fact_type = _DECLARED_FACT_TYPES.get(name)
        if fact_type is None:
            return None
        if fact_type is FactType.TEXT:
            return self._package.text(name)
        return self._package.number(name)


# ---------------------------------------------------------------------------
# template_eligible -- MV-1c.6's one exported eligibility predicate. Five
# clauses: kind, voice, angle, tier, slot presence.
# ---------------------------------------------------------------------------


def template_eligible(
    template: MediaTemplate, package: StoryPackage, *, voice: str, tier: FormatTier
) -> bool:
    """The ONLY definition of "this template is eligible for this package
    in this voice at this tier" (MV-1c.6) -- `select_media_template` hands
    this to `engine.select_template` as its `dimension_matches` argument
    (via a thin closure, since that function's own signature is `(template,
    frame) -> bool`); `tools/generate_media_corpus.py` calls this SAME
    function rather than a partial, independently reimplemented one."""
    if template.kinds and package.story_kind.value not in template.kinds:
        return False
    if template.voices and voice not in template.voices:
        return False
    if template.angles and package.angle.value not in template.angles:
        return False
    if template.tiers and tier.value not in template.tiers:
        return False
    for slot in template.slots:
        if package.text(slot) is None:
            return False
    return True


# ---------------------------------------------------------------------------
# select_media_template -- step 2 (the five `template_eligible` dimensions)
# plus a delegation to engine.select_template for steps 3-6, mirroring
# managers/voice/select.py::select_phrase's own split exactly.
# ---------------------------------------------------------------------------


def select_media_template(
    package: StoryPackage,
    templates: Sequence[MediaTemplate],
    *,
    voice: str,
    tier: FormatTier,
    fallback_by_kind: Mapping[StoryKind, MediaTemplate],
    use_counts: Mapping[str, int],
    repeat_penalty: float,
    rng: random.Random,
) -> MediaTemplate:
    """Delegates steps 3-6 (trigger evaluation, the `template_id`
    determinism sort, the repeat penalty, the seeded weighted draw) to
    `engine.select_template`, unchanged. `voice`/`tier` are already known
    (the persona draw and, in this build step, a caller-supplied constant
    -- MV-4's real tier-selection rule is a later build step's work).

    **MV-1c.2: the fallback is resolved by LOOKUP, never trusted to survive
    a filter.** This function computes the real eligible-AND-triggered
    survivor set itself, using the SAME two predicates `engine.
    select_template` would apply (`template_eligible` for dimensions,
    `engine.triggers_hold` for triggers) -- if that set is empty, it
    returns `fallback_by_kind[package.story_kind]` directly and never calls
    `engine.select_template` at all, so `rng.choices` can never be handed
    an empty population. `fallback_by_kind` is caller-supplied data (`dba2/
    services/media_service.py::LoadedMediaLibrary.fallback_by_kind`, a
    later build step's loader field) -- this module stays a pure function
    of its own parameters, never reaching into a `LoadedMediaLibrary`
    object itself."""
    trigger_view = PackageTriggerView(package)

    def matcher(candidate_template: Any, candidate_view: Any) -> bool:
        return template_eligible(candidate_template, package, voice=voice, tier=tier)

    survivors = [
        t for t in templates if matcher(t, trigger_view) and engine.triggers_hold(t, trigger_view)
    ]
    if not survivors:
        return fallback_by_kind[package.story_kind]

    return engine.select_template(
        trigger_view,
        templates,
        dimension_matches=matcher,
        use_counts=use_counts,
        repeat_penalty=repeat_penalty,
        rng=rng,
    )


# ---------------------------------------------------------------------------
# bind_slots -- real slot-binding resolution. Retyped: the `team_name`/
# `player_name` parameters go away (`Subject` already carries both names,
# resolved once at seal time, MV-1b(c)); the nine `_slot_*` resolvers and
# `SLOT_NAMES` are deleted, and with them the degrade-to-placeholder
# behavior ("several", "the team") -- replaced by `template_eligible`'s own
# slot-presence clause above, which is what makes "pick a template whose
# facts exist" real (MV-6) instead of "render a template with a vague word
# in it."
# ---------------------------------------------------------------------------


def bind_slots(template: MediaTemplate, package: StoryPackage) -> Mapping[str, str]:
    """Every key in `template.slots` gets a value from `Fact.value_text` --
    render-ready by construction (MV-1b(c)), including the two special-
    cased "render the absolute value of a signed count" facts
    (`streak_length`/`motivation_magnitude`) `dba2/media/detect/legacy.py`
    already pre-renders that way, so this function itself does no
    per-field formatting logic at all.

    Raises `AssertionError` for a slot this package's `facts` do not carry
    -- `template_eligible`'s own slot-presence clause (or, for a FORCED
    fallback, MV-1c.2's load-time guarantee that every fallback's `slots`
    are a subset of its kind's `REQUIRED_FACT_KEYS_BY_KIND`) should make
    this unreachable for any template a real caller actually selected; a
    hand-built adversarial call (a test) can still reach it, and a loud,
    explanatory crash here is the correct behavior, matching this
    codebase's standing "should never happen, defensive" shape (`dba2/
    media/frame.py`'s kind-group check, `salience.py::_kind_weight`)."""
    bindings: dict[str, str] = {}
    for slot in template.slots:
        value = package.text(slot)
        if value is None:
            raise AssertionError(
                f"template {template.id!r} declares slot {slot!r}, which the package's own "
                f"facts (kind {package.story_kind.value!r}) do not carry -- template_eligible's "
                "own slot-presence clause should make this unreachable for a template actually "
                "selected"
            )
        bindings[slot] = value
    return bindings
