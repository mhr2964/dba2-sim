"""`seeds/gm_phrases.json`'s schema, loader, and load-time validator --
Decision P3-16/P3-17/P3-18/P3-19/P3-31 (docs/design/phase-3-inter-manager-
society.md §6.1-§6.3), §11 step 15.

**Why this lives outside `managers/`.** `managers/` may not call `open(`
(Decision P2-2(b)'s purity extension, `tests/unit/test_layering.py`), so a
JSON library physically cannot be loaded from inside it (Decision P3-17
item 2). This module is the loader, and it is deliberately dumb the same
way `tools/gm_harness/bands.py` is dumb about `seeds/gm_validation_bands.
json`: it owns the shape and the validation, `managers/voice/select.py`
owns selection, and neither duplicates the other's job. `select.py`'s own
docstring already defers exactly this validation to here (Decision P3-18).

**Decision P3-16.** `select_phrase` returns a `PhraseSelection`, never a
rendered string -- `managers/` may not emit English (Decision P2-18).
`render()` below is the interpolation step, a `services/`-only concern
(it needs player/pick *names*, a `domain` read Phase 5 will eventually
supply -- out of scope here, see `render`'s own docstring). It is NOT
called from the live negotiation loop in Phase 3 (Decision P3-1: dialogue
is logged, never rendered to text, this phase).

Zero coupling to `dba2.services.gm_service` -- this module is imported BY
`gm_service.py`, never the reverse.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from dba2.domain.team import GmPhilosophy
from dba2.managers.voice.frame import NegotiationFrame, SituationToken
from dba2.managers.voice.select import PhraseLibrary, PhraseSelection, PhraseTemplate, Trigger

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PHRASE_LIBRARY_PATH = _REPO_ROOT / "seeds" / "gm_phrases.json"
"""Mirrors `tools/gm_harness/bands.py::DEFAULT_BANDS_PATH`'s own path
convention exactly -- same `seeds/` precedent Decision P3-17 item 3 cites."""

_VALID_OPS = frozenset({"lt", "lte", "gte", "gt", "eq", "in"})
"""§6.3 step 3's closed six-operator set, verbatim -- the same set
`voice/engine.py::_trigger_holds` evaluates against at selection time
(moved there from `select.py` at Phase 4 Stage D step 15's extraction,
Decision P4-20). Declared once here (not re-derived) since `engine.py`'s
own copy is deliberately function-local, to keep that file's footprint
small under `test_constant_inventory.py`'s `managers/`-scoped guard; no
such guard applies here."""

_VALID_FEATURES = frozenset(f.name for f in dataclasses.fields(NegotiationFrame))
"""Every real field on the FULL `NegotiationFrame` (`managers/voice/
frame.py`) -- the ten numeric fields inherited from `managers/negotiation.
py`'s own base class plus the nine full-frame fields, since `dataclasses.
fields` on a `@dataclass(frozen=True)` subclass already walks the MRO and
returns both inherited and locally-declared fields in declaration order.
This is Decision P3-18's own vocabulary: a `Trigger.feature` outside this
set is a load-time error, never a silently-false predicate."""


@dataclasses.dataclass(frozen=True)
class LoadedPhraseLibrary:
    """The validated, in-memory library -- `templates` plus the two
    knobs Decision P3-17 puts in the file rather than in code
    (`repeat_penalty`, `select_phrase`'s own required parameter; `exclusive_
    mass_share`, Decision P3-31's authoring floor, read and asserted here
    but otherwise unused by `managers/`)."""

    templates: PhraseLibrary
    repeat_penalty: float
    exclusive_mass_share: float


def _parse_trigger(row: dict) -> Trigger:
    return Trigger(feature=row["feature"], op=row["op"], value=row["value"])


def _parse_template(row: dict) -> PhraseTemplate:
    return PhraseTemplate(
        id=row["id"],
        philosophies=tuple(row.get("philosophies", ())),
        situations=tuple(row.get("situations", ())),
        windows=tuple(row.get("windows", ())),
        triggers=tuple(_parse_trigger(t) for t in row.get("triggers", ())),
        weight=row["weight"],
        text=row["text"],
        slots=tuple(row.get("slots", ())),
    )


def _validate_positive_weights(templates: PhraseLibrary) -> None:
    """**Nit N7, §11 step 15's follow-up round.** `weight` was previously
    unvalidated: zero, negative, or non-numeric weights loaded fine and
    only surfaced as a `ValueError` from `rng.choices` at SELECTION time
    (a negative weight, or all-zero weights on the survivor list), far
    from the authoring mistake that caused it. Required: every template's
    `weight` must be a positive (`> 0`), real number."""
    for template in templates:
        if isinstance(template.weight, bool) or not isinstance(template.weight, (int, float)):
            raise ValueError(
                f"gm_phrases.json: template {template.id!r} has a non-numeric weight "
                f"{template.weight!r}"
            )
        if template.weight <= 0:
            raise ValueError(
                f"gm_phrases.json: template {template.id!r} has a non-positive weight "
                f"{template.weight!r} -- every template's weight must be > 0"
            )


def _validate_repeat_penalty(payload: dict, path: Path) -> float:
    """**Nit N1, §11 step 15's follow-up round.** `repeat_penalty` was
    previously read as `float(payload["repeat_penalty"])` with no
    validation at all: a missing key raised a raw `KeyError` (no library
    path, no explanation), and a value outside `[0.0, 1.0)` silently
    inverted the mechanism's own intent -- `select.py::select_phrase`
    computes `effective_weight = weight * (repeat_penalty ** prior_uses)`,
    so a value `>= 1.0` turns repeated use into a BONUS instead of a
    penalty, and a negative value makes `effective_weight` oscillate in
    sign. The upper bound is exclusive: `1.0` itself is "no penalty at
    all" -- `select.py`'s own docstring states the mechanism exists
    specifically so a multi-round negotiation does not repeat a line, so a
    value that disables that entirely is rejected rather than silently
    accepted as the library's only ever-used repeat-penalty value."""
    if "repeat_penalty" not in payload:
        raise ValueError(f"{path}: missing required top-level key 'repeat_penalty'")
    repeat_penalty = payload["repeat_penalty"]
    if isinstance(repeat_penalty, bool) or not isinstance(repeat_penalty, (int, float)):
        raise ValueError(f"{path}: 'repeat_penalty' must be a number, got {repeat_penalty!r}")
    if not (0.0 <= repeat_penalty < 1.0):
        raise ValueError(
            f"{path}: 'repeat_penalty' ({repeat_penalty}) must be in [0.0, 1.0) -- a value "
            ">= 1.0 turns repeated template use into a bonus instead of a penalty, and a "
            "negative value makes the effective weight oscillate in sign"
        )
    return float(repeat_penalty)


def _validate_trigger_vocabulary(templates: PhraseLibrary) -> None:
    """Decision P3-18 -- a `Trigger` naming an unknown `feature`/`op` is a
    load-time error. This is the ONE validation Decision P3-18 explicitly
    assigns here rather than to `select.py` (that module's own docstring
    states it trusts an already-validated library)."""
    for template in templates:
        for trigger in template.triggers:
            if trigger.feature not in _VALID_FEATURES:
                raise ValueError(
                    f"gm_phrases.json: template {template.id!r} triggers on unknown "
                    f"feature {trigger.feature!r} -- not a field on NegotiationFrame "
                    f"(valid: {sorted(_VALID_FEATURES)})"
                )
            if trigger.op not in _VALID_OPS:
                raise ValueError(
                    f"gm_phrases.json: template {template.id!r} uses unknown operator "
                    f"{trigger.op!r} -- must be one of {sorted(_VALID_OPS)}"
                )


def _validate_fallback_coverage(templates: PhraseLibrary) -> None:
    """Decision P3-19 -- exactly one no-trigger, all-philosophy fallback
    template per each of the ten `SituationToken` members. A fallback
    template for token `S` is one with `triggers == ()`, `philosophies ==
    ()`, `windows == ()` (true wildcard -- critic finding B5, §11 step 15's
    follow-up round), and `situations` either wildcard (`()`) or explicitly
    containing `S.value` -- the same membership test `select.py::
    _template_matches_dimensions` applies at selection time, restricted to
    the no-trigger/all-philosophy/all-window shape Decision P3-19 requires.

    **Why `windows` must also be wildcard, not just checked at selection
    time.** `select.py::select_phrase` filters on `windows` exactly like
    `philosophies`/`situations` (`_template_matches_dimensions`) -- a
    template with a non-empty `windows` list is NOT a survivor on a
    mismatched window, regardless of how bare its `triggers`/`philosophies`
    are. Before this fix, such a template could still be counted as the
    situation's designated fallback here, passing validation, then leaving
    ZERO survivors at `select_phrase` on any turn whose `window` does not
    match -- reproduced by the critic with a hand-built corrupt fixture: an
    `IndexError`/`ValueError` from `rng.choices` on an empty `survivors`
    list, at runtime, on a real trade window. A true fallback must survive
    every dimension filter unconditionally, which requires `windows == ()`
    exactly as it already requires `philosophies == ()`."""
    missing: list[str] = []
    duplicated: list[str] = []
    for token in SituationToken:
        fallbacks = [
            t for t in templates
            if not t.triggers and not t.philosophies and not t.windows
            and (not t.situations or token.value in t.situations)
        ]
        if len(fallbacks) == 0:
            missing.append(token.value)
        elif len(fallbacks) > 1:
            duplicated.append(f"{token.value} ({sorted(t.id for t in fallbacks)})")
    if missing or duplicated:
        parts = []
        if missing:
            parts.append(f"missing a fallback for: {missing}")
        if duplicated:
            parts.append(f"more than one fallback for: {duplicated}")
        raise ValueError(
            "gm_phrases.json: Decision P3-19 requires exactly one no-trigger, "
            "all-philosophy fallback template per situation token -- " + "; ".join(parts)
        )


def _validate_exclusive_mass_share(payload: dict, path: Path) -> float:
    """Decision P3-31 -- `exclusive_mass_share`'s load-time obligation is
    exactly "the file declares a value and it meets the `0.25` authoring
    floor" (§9.1's own text: the TV-distance derivation is a step-16
    measurement concern, not something this loader re-derives).

    **Nit N6, §11 step 15's follow-up round.** Previously took the value
    pre-extracted (`payload["exclusive_mass_share"]` at the call site), so
    a missing key raised a bare `KeyError` with no file path -- the exact
    inconsistency `_validate_repeat_penalty` was fixed against under N1,
    for the identical defect one key over. Takes `payload`/`path` now, the
    same signature shape as `_validate_repeat_penalty`, for the same
    friendly-error reason."""
    if "exclusive_mass_share" not in payload:
        raise ValueError(f"{path}: missing required top-level key 'exclusive_mass_share'")
    exclusive_mass_share = payload["exclusive_mass_share"]
    if not isinstance(exclusive_mass_share, (int, float)) or isinstance(exclusive_mass_share, bool):
        raise ValueError(
            f"{path}: top-level 'exclusive_mass_share' must be a number"
        )
    if exclusive_mass_share < 0.25:
        raise ValueError(
            f"{path}: 'exclusive_mass_share' ({exclusive_mass_share}) is below "
            "Decision P3-31's 0.25 authoring floor"
        )
    return float(exclusive_mass_share)


def _validate_exclusive_mass_share_by_philosophy(
    templates: PhraseLibrary, exclusive_mass_share: float
) -> None:
    """**Critic finding B4, §11 step 15's follow-up round** -- the check
    above only validates the declared top-level NUMBER against the 0.25
    floor; it never checks that the library's actual template weights
    satisfy §9.1/§13's own real rule: "each philosophy must draw at least
    `exclusive_mass_share` of its selection weight from templates no other
    philosophy can select." A library can declare `0.25` and pass the
    check above while every one of its philosophies actually draws far

    **Scope, stated explicitly rather than left for a reader to infer
    (re-verification nit 3):** this ratio is computed GLOBALLY, over every
    template regardless of `situations`, not per-situation. §9.1's own TV
    derivation is a pooled-selection-distribution argument, so a global
    check is the right proxy for it -- but it means a philosophy could in
    principle carry zero exclusive templates within some one narrow
    situation while still clearing this floor overall. Re-verification
    measured the shipped library's realized per-philosophy pairwise TV
    directly (not just this proxy) at a minimum of 0.5062, comfortably
    above M27's `band_low = 0.125`, so this gap is not live today; it
    would only matter if a future edit hollowed out one situation's
    exclusive coverage for one philosophy while padding another's.
    less real exclusive weight -- measured on the pre-fix shipped library,
    `vet_overrater` 0.156, `tendency_respecter`/`chaos` 0.120,
    `defense_first`/`youth_developer` 0.090, all below the declared floor,
    with only `star_maxer`/`egalitarian` barely clearing it.

    For each `GmPhilosophy` member `P`:

        exclusive_weight(P) = Σ weight(t) for t in templates
                               where t.philosophies == (P.value,) exactly
                               -- P is the ONLY philosophy that can select t
        selectable_weight(P) = Σ weight(t) for t in templates
                                where t.philosophies == ()               (wildcard)
                                or P.value in t.philosophies             (membership,
                                matching `select.py::_template_matches_dimensions`'s
                                own semantics exactly -- a multi-philosophy
                                template such as `philosophies: ["star_maxer",
                                "egalitarian"]` IS selectable by star_maxer even
                                though it isn't star_maxer-EXCLUSIVE, and belongs
                                in this denominator for the same reason it
                                belongs in `select_phrase`'s own survivor set)
        ratio(P) = exclusive_weight(P) / selectable_weight(P)

    Raises if `ratio(P) < exclusive_mass_share` for any philosophy, or if
    `selectable_weight(P) == 0` (a philosophy with no selectable templates
    at all cannot clear any positive floor, and dividing by zero would
    otherwise mask that as a pass).

    **A tuple-equality denominator (`t.philosophies == (P.value,)`, checked
    only on the exclusive side) previously double-served as an incorrect
    stand-in for the selectable-side membership test too, silently
    excluding any multi-philosophy template from `selectable_weight` --
    caught only on re-verification, when a hand-built multi-philosophy
    fixture inflated `ratio` to pass at 25x below the real floor. The
    shipped library has zero multi-philosophy templates today, so this was
    latent, not live -- but the check exists precisely so a future edit
    can't introduce one silently, and the old form would have let it."""
    failing: list[str] = []
    for philosophy in GmPhilosophy:
        exclusive_weight = sum(
            t.weight for t in templates if t.philosophies == (philosophy.value,)
        )
        selectable_weight = sum(
            t.weight for t in templates
            if not t.philosophies or philosophy.value in t.philosophies
        )
        if selectable_weight <= 0:
            failing.append(f"{philosophy.value} (no selectable templates)")
            continue
        ratio = exclusive_weight / selectable_weight
        if ratio < exclusive_mass_share:
            failing.append(f"{philosophy.value} ({ratio:.3f})")
    if failing:
        raise ValueError(
            "gm_phrases.json: §9.1/§13's exclusive-mass-share rule requires each "
            f"philosophy draw at least {exclusive_mass_share} of its selectable weight from "
            "templates no other philosophy can select -- failing: " + ", ".join(sorted(failing))
        )


def _validate_open_never_triggers_on_stance(templates: PhraseLibrary) -> None:
    """**Scope extension to Decision P3-18, added at this dispatch (§11
    step 15) -- disclosed here, not silently folded into P3-18's own
    vocabulary check.** No template whose `situations` includes `open`
    (or wildcards every situation) may carry a trigger on `stance`.

    `derive_frame` needs SOME `stance` value for every frame it builds,
    `open` selections included -- there is no "no stance yet" value in the
    closed `NegotiationStance` vocabulary, and the caller (`gm_service.py`
    §11 step 15's wiring) passes the round's own EVENTUAL outcome stance
    for lack of anything else available at the moment the opening line is
    chosen. That value does NOT reflect the proposer's own state at open
    time -- it reflects how the round happens to resolve, which the
    proposer cannot know when opening. A template author reading `stance`
    on an `open` frame would reasonably assume it describes the opener's
    own posture; it does not, so triggering on it there would silently
    correlate `open` line selection with information the opener does not
    have. Every other dimension/trigger on an `open` frame reads real,
    available-at-open-time state (the frame's other fields all come off
    the proposer's own `record_a` before any response) -- `stance` is the
    one field this rule singles out."""
    for template in templates:
        applies_to_open = (
            not template.situations or SituationToken.OPEN.value in template.situations
        )
        if applies_to_open and any(tr.feature == "stance" for tr in template.triggers):
            raise ValueError(
                f"gm_phrases.json: template {template.id!r} applies to the 'open' situation "
                "but triggers on 'stance' -- an 'open' frame's stance reflects the round's "
                "eventual outcome, not the opener's own state at open time (see this "
                "module's own _validate_open_never_triggers_on_stance docstring)"
            )


def load_library(path: Path = DEFAULT_PHRASE_LIBRARY_PATH) -> LoadedPhraseLibrary:
    """Reads, parses, and validates `seeds/gm_phrases.json`, raising
    `ValueError` (never silently dropping) on any violation -- Decision
    P3-18's own standing rule: "a typo that silently disables a template is
    exactly the defect class this project has paid for repeatedly."

    **Corrected count, §11 step 15's follow-up round (nit N6) -- there are
    not "five" load-time checks, there are eight: seven `_validate_*`
    functions plus one inline duplicate-id check below, none of which this
    docstring previously named accurately.**

    1. Duplicate template ids (inline, below -- not its own function).
    2. `_validate_trigger_vocabulary` -- Decision P3-18, unknown
       feature/operator.
    3. `_validate_fallback_coverage` -- Decision P3-19, exactly one
       no-trigger/all-philosophy/all-window fallback per situation token
       (window-scoped fix, critic finding B5).
    4. `_validate_open_never_triggers_on_stance` -- this dispatch's own
       scope extension to Decision P3-18.
    5. `_validate_exclusive_mass_share` -- Decision P3-31's declared-number
       floor (`>= 0.25`).
    6. `_validate_exclusive_mass_share_by_philosophy` -- critic finding B4,
       the REAL per-philosophy check §9.1/§13 actually states, which (5)
       alone never enforced.
    7. `_validate_positive_weights` -- nit N7, every `weight` must be a
       positive number.
    8. `_validate_repeat_penalty` -- nit N1, `repeat_penalty` is required
       and must lie in `[0.0, 1.0)`."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    templates = tuple(_parse_template(row) for row in payload["templates"])

    ids = [t.id for t in templates]
    if len(ids) != len(set(ids)):
        raise ValueError("gm_phrases.json has a duplicate template id")

    _validate_trigger_vocabulary(templates)
    _validate_fallback_coverage(templates)
    _validate_open_never_triggers_on_stance(templates)
    _validate_positive_weights(templates)
    exclusive_mass_share = _validate_exclusive_mass_share(payload, path)
    _validate_exclusive_mass_share_by_philosophy(templates, exclusive_mass_share)
    repeat_penalty = _validate_repeat_penalty(payload, path)

    return LoadedPhraseLibrary(
        templates=templates,
        repeat_penalty=repeat_penalty,
        exclusive_mass_share=exclusive_mass_share,
    )


def render(selection: PhraseSelection, library: LoadedPhraseLibrary) -> str:
    """Decision P3-16's interpolation step -- `template.text.format(**
    selection.slot_bindings)`. **Not called from the live negotiation loop
    in Phase 3** (Decision P3-1: dialogue is logged, never rendered, this
    phase) -- this function exists for completeness/tests/Phase 5, which is
    why every template authored for this dispatch uses `slots: []`:
    `select_phrase` always returns `slot_bindings={}` (step 14's own
    documented scope boundary), so a template with a `{placeholder}` in its
    text and no binding data would fail to render today. Raises `KeyError`
    (via the lookup below) if `selection.template_id` does not name a
    template in `library` -- not a documented production path, since every
    real `PhraseSelection` originates from `select_phrase` choosing a
    template out of the same library passed here."""
    by_id = {t.id: t for t in library.templates}
    template = by_id[selection.template_id]
    return template.text.format(**selection.slot_bindings)
