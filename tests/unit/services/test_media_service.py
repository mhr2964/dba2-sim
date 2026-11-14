"""`dba2/services/media_service.py`'s Stage D step 18 addition --
`load_media_library` (Decisions P4-21/P4-25/P4-26/P4-53) and the pure
helpers around it (`_subject_identity`, `render_story`). §11 Stage D step
18.

Mirrors `tests/unit/services/test_phrase_service.py`'s own structure: a
real, valid library loads; each load-time validation gets its own
deliberately-corrupt fixture. The real DB-backed half of this step
(`select_and_bind_story`'s own name-lookup reads) is proven in `tests/
integration/test_media_service.py` instead -- this file never touches a
connection (`load_media_library` takes a `Path`, never an `Executor`).
"""
from __future__ import annotations

import copy
import dataclasses
import inspect
import json

import pytest

from dba2.domain.media_facts import Angle, StoryPackage, Subject, SubjectKind
from dba2.domain.phase import Phase
from dba2.media.detect import legacy
from dba2.media.frame import StoryCandidate, StoryKind, derive_frame
from dba2.media.salience import SalienceCandidate, ScoredCandidate
from dba2.media.select import MediaPersona, MediaTemplate, StorySelection, bind_slots
from dba2.services import media_service
from dba2.services.media_service import (
    _CYCLE_BEARING_PHASE_VALUES,
    DEFAULT_MEDIA_PHRASE_LIBRARY_PATH,
    LoadedMediaLibrary,
    PhaseCadence,
    _cycles_since_last_seen,
    _eligible_personas_under_cap,
    _has_cap_headroom,
    _hot_take_kinds,
    _persona_multipliers_for,
    _subject_identity,
    _subject_prominence,
    _validate_media_persona_exclusivity,
    load_media_library,
    render_story,
    run_media_pulse_cycle,
    seal_package,
    with_voices_disabled,
)

_PERSONA_KEYS = ("beat_writer", "hot_take", "analyst", "insider", "historian")
_SEASON = 2026
_PHASE = Phase.REGULAR_SEASON
_CYCLE_INDEX = 3


def _sealed_package(
    candidate: StoryCandidate, *, team_name: str = "A Team", player_name: str = "A Player"
) -> StoryPackage:
    """The shortest REAL path from a hand-built `StoryCandidate` to a
    sealed `StoryPackage` -- the identical helper `tests/unit/test_media_
    select.py`/`tests/unit/test_media_salience.py` each already own
    (duplicated here rather than imported, matching this codebase's
    existing convention of each test file owning its own small fixture
    helpers). Used below to rebuild the two `render_story` capitalization
    tests on a real sealed name instead of a hand-built `slot_bindings`
    dict, now that `select.py`'s degrade-to-`"the team"` placeholder
    machinery (`_SLOT_RESOLVERS`/`SLOT_NAMES`) no longer exists to produce
    one."""
    frame = derive_frame(candidate)
    draft = legacy.adapt_frame(frame, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX)
    assert draft is not None, f"candidate for {candidate.kind} was insufficient -- fix the fixture"
    team_names = {candidate.team_id: team_name} if candidate.team_id is not None else {}
    player_names = {candidate.player_id: player_name} if candidate.player_id is not None else {}
    sealed = seal_package(draft, team_names=team_names, player_names=player_names)
    assert sealed is not None, f"candidate for {candidate.kind} failed to seal -- fix the fixture"
    return sealed


def _minimal_valid_payload() -> dict:
    """One fallback per `StoryKind` (Decision P4-25) plus one exclusive
    template per (persona, kind) pair the persona is assigned, weighted to
    clear the real per-persona exclusivity floor (each persona's own
    relevant-kinds ratio is `2.0 / (2.0 + 1.0) = 0.667 >= 0.5`, per kind)
    -- the smallest payload `load_media_library` accepts. Every one of
    `StoryKind`'s 11 members is assigned to exactly one of the 5 personas,
    round-robin by index, so every kind clears `_validate_media_every_
    kind_has_a_persona` and every persona has >= 1 relevant kind."""
    kinds = list(StoryKind)
    kinds_by_persona: dict[str, list[str]] = {key: [] for key in _PERSONA_KEYS}
    for index, kind in enumerate(kinds):
        kinds_by_persona[_PERSONA_KEYS[index % len(_PERSONA_KEYS)]].append(kind.value)

    templates = [
        {
            "id": f"fallback_{kind.value}", "kinds": [kind.value], "voices": [],
            "triggers": [], "weight": 1.0, "text": f"line for {kind.value}", "slots": [],
        }
        for kind in kinds
    ]
    personas = []
    for persona_key, owned_kinds in kinds_by_persona.items():
        personas.append(
            {
                "key": persona_key, "story_kind_weights": {k: 1.0 for k in owned_kinds},
                "column": f"{persona_key} column", "beat_description": f"{persona_key} beat",
                "register": "reportorial", "voice_rules": ["be concise"], "fences": [],
            }
        )
        for kind_value in owned_kinds:
            templates.append(
                {
                    "id": f"exclusive_{persona_key}_{kind_value}", "kinds": [kind_value],
                    "voices": [persona_key], "triggers": [], "weight": 2.0,
                    "text": f"line for {persona_key}/{kind_value}", "slots": [],
                }
            )
    phase_cadence = {
        persona_key: {phase_value: [1.0, 3] for phase_value in _CYCLE_BEARING_PHASE_VALUES}
        for persona_key in _PERSONA_KEYS
    }
    return {
        "repeat_penalty": 0.25, "persona_exclusivity_floor": 0.5,
        "personas": personas, "templates": templates,
        "phase_cadence": phase_cadence, "team_profile_quota": 3,
    }


def _write(tmp_path, payload: dict, name: str = "lib.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _find_template(payload: dict, template_id: str) -> dict:
    """docs/design/media-v2-newsroom.md, "Build order" step 3a: several
    vocabulary-mutation fixtures below used to mutate `templates[0]`
    (the `standings_surge` fallback) without caring which kind it covered,
    because the pre-retype `_VALID_FEATURES` was one flat list shared by
    every kind. Now that a feature/slot must be valid for the TEMPLATE'S
    OWN kind (MV-1b(d)), and a fallback's own slots are additionally
    checked against `REQUIRED_FACT_KEYS_BY_KIND` (MV-1c.2), a mutation
    aimed at one specific check has to land on a template whose kind
    actually admits the vocabulary the test is exercising, and (for the
    slot-vocabulary/placeholder checks) on a NON-fallback template so
    `_validate_media_fallback_coverage`'s own, earlier-running check does
    not intercept it first. Looked up by id rather than by a fragile
    positional index into `_minimal_valid_payload`'s own construction
    order."""
    for template in payload["templates"]:
        if template["id"] == template_id:
            return template
    raise AssertionError(f"no template with id {template_id!r} in this payload")


# ---------------------------------------------------------------------------
# A real, valid library loads.
# ---------------------------------------------------------------------------


def test_the_real_shipped_library_loads_successfully():
    library = load_media_library()
    assert library.repeat_penalty == 0.3
    assert 0.0 <= library.persona_exclusivity_floor <= 1.0
    assert len(library.personas) == 5
    assert len(library.templates) > 0


def test_default_path_points_at_seeds_media_phrases_json():
    assert DEFAULT_MEDIA_PHRASE_LIBRARY_PATH.name == "media_phrases.json"
    assert DEFAULT_MEDIA_PHRASE_LIBRARY_PATH.parent.name == "seeds"


def test_minimal_valid_payload_loads(tmp_path):
    path = _write(tmp_path, _minimal_valid_payload())
    library = load_media_library(path)
    assert len(library.personas) == 5
    assert len(library.templates) == len(list(StoryKind)) * 2


# ---------------------------------------------------------------------------
# Decision P4-53 -- the persona set is exactly five, closed.
# ---------------------------------------------------------------------------


def test_raises_on_a_sixth_persona(tmp_path):
    payload = _minimal_valid_payload()
    payload["personas"].append({"key": "columnist", "story_kind_weights": {}})
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="columnist"):
        load_media_library(path)


def test_raises_on_a_missing_persona(tmp_path):
    payload = _minimal_valid_payload()
    payload["personas"] = [p for p in payload["personas"] if p["key"] != "historian"]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="beat_writer"):
        load_media_library(path)


def test_raises_on_a_duplicate_persona_key(tmp_path):
    payload = _minimal_valid_payload()
    payload["personas"].append(copy.deepcopy(payload["personas"][0]))
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="duplicate persona key"):
        load_media_library(path)


# ---------------------------------------------------------------------------
# This loader's own added invariant -- every StoryKind needs >= 1 persona.
# ---------------------------------------------------------------------------


def test_raises_when_a_story_kind_has_no_persona_at_all(tmp_path):
    payload = _minimal_valid_payload()
    for persona in payload["personas"]:
        persona["story_kind_weights"].pop(StoryKind.HOLDOUT_WATCH.value, None)
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="holdout_watch"):
        load_media_library(path)


# ---------------------------------------------------------------------------
# Vocabulary checks -- kinds, voices, triggers, slots.
# ---------------------------------------------------------------------------


def test_raises_on_unknown_kind_in_a_persona_preference_vector(tmp_path):
    payload = _minimal_valid_payload()
    payload["personas"][0]["story_kind_weights"]["not_a_real_kind"] = 1.0
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="not_a_real_kind"):
        load_media_library(path)


def test_raises_on_unknown_kind_in_a_template(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"][0]["kinds"] = ["not_a_real_kind"]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="not_a_real_kind"):
        load_media_library(path)


def test_raises_on_unknown_voice_in_a_template(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"][0]["voices"] = ["not_a_real_persona"]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="not_a_real_persona"):
        load_media_library(path)


def test_raises_on_unknown_trigger_feature(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"][0]["triggers"] = [
        {"feature": "not_a_real_field", "op": "gte", "value": 0.5}
    ]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="not_a_real_field"):
        load_media_library(path)


def test_raises_on_unknown_trigger_operator(tmp_path):
    payload = _minimal_valid_payload()
    # `standings_position_delta` is a real, valid feature for the
    # `standings_surge` fallback's own kind (MV-1b(d)'s per-kind check) --
    # isolates the mutation to the OPERATOR alone.
    payload["templates"][0]["triggers"] = [
        {"feature": "standings_position_delta", "op": "between", "value": 5}
    ]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="between"):
        load_media_library(path)


def test_raises_on_unknown_slot_name(tmp_path):
    payload = _minimal_valid_payload()
    # A non-fallback template (MV-1c.2's own fallback-slot check would
    # otherwise intercept this first, on a different message).
    _find_template(payload, "exclusive_beat_writer_standings_surge")["slots"] = [
        "not_a_real_slot"
    ]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="not_a_real_slot"):
        load_media_library(path)


# ---------------------------------------------------------------------------
# Round 12 critic fix (blocking finding 4) -- a `text` placeholder with no
# matching declared `slots` entry raises at load time, not `KeyError` at
# `render_story`'s `.format()` call.
# ---------------------------------------------------------------------------


def test_raises_when_text_uses_a_placeholder_not_declared_in_slots(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"][0]["text"] = "{team_name} is undefeated"
    # `slots` left at `[]` (this template's default) -- the placeholder is
    # present in `text` but never declared.
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="team_name"):
        load_media_library(path)


def test_a_declared_slot_unused_in_text_is_not_an_error(tmp_path):
    """The reverse direction (`_validate_media_slot_vocabulary`, above) is
    a separate, pre-existing check. A declared-but-unused slot is wasteful
    authoring, never a load-time error -- only an UNDECLARED placeholder
    that actually appears in `text` is."""
    payload = _minimal_valid_payload()
    payload["templates"][0]["text"] = "static text, no placeholders here"
    payload["templates"][0]["slots"] = ["team_name"]
    path = _write(tmp_path, payload)
    load_media_library(path)  # must not raise


def test_raises_on_an_auto_numbered_placeholder_even_with_empty_slots(tmp_path):
    """Round 13 critic fix (nit 4): an auto-numbered field (`"{}"`, empty
    field name) is unsupported regardless of `slots` -- `render_story`
    calls `template.text.format(**slot_bindings)` with no positional
    arguments, so this raises `IndexError` at render time. The shipped
    round-12 validator filtered on bare `if field_name` truthiness, which
    treats `""` as absent and let this load clean."""
    payload = _minimal_valid_payload()
    payload["templates"][0]["text"] = "{}'s locker room is loud today"
    payload["templates"][0]["slots"] = []
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="positional"):
        load_media_library(path)


def test_raises_on_an_explicit_positional_placeholder(tmp_path):
    """The explicit-index sibling of the auto-numbered case above
    (`"{0}"`) -- also unsupported by `render_story`'s `**slot_bindings`-
    only `.format()` call, also raised as the same 'positional' error
    rather than (incorrectly) checked against declared `slots`."""
    payload = _minimal_valid_payload()
    payload["templates"][0]["text"] = "{0}'s locker room is loud today"
    payload["templates"][0]["slots"] = []
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="positional"):
        load_media_library(path)


def test_raises_on_a_placeholder_nested_inside_a_format_spec(tmp_path):
    """Round 13 critic fix (nit 4): `string.Formatter().parse` only parses
    ONE level -- a replacement field nested inside a format spec
    (`"{streak_length:{team_name}}"`) is returned as a raw substring of
    `format_spec`, never itself parsed, so a missing `team_name` slot was
    invisible to the shipped round-12 validator and loaded clean, raising
    `KeyError` at `render_story`'s real `.format()` call instead."""
    payload = _minimal_valid_payload()
    # A non-fallback STREAK template -- `streak_length`/`team_name` are
    # both real, valid keys for that kind (MV-1b(d)), so this isolates the
    # mutation to the nested-format-spec placeholder-coverage gap alone.
    template = _find_template(payload, "exclusive_analyst_streak")
    template["text"] = "{streak_length:{team_name}} in a row"
    template["slots"] = ["streak_length"]  # team_name undeclared
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="team_name"):
        load_media_library(path)


# ---------------------------------------------------------------------------
# Decision P4-25 -- exactly one no-trigger, wildcard-voice fallback per kind.
# ---------------------------------------------------------------------------


def test_raises_when_a_story_kind_has_no_fallback(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"] = [
        t for t in payload["templates"] if t["id"] != "fallback_streak"
    ]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="streak"):
        load_media_library(path)


def test_raises_when_a_story_kind_has_two_fallbacks(tmp_path):
    payload = _minimal_valid_payload()
    duplicate = copy.deepcopy(payload["templates"][0])
    duplicate["id"] = duplicate["id"] + "_duplicate"
    payload["templates"].append(duplicate)
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="more than one fallback"):
        load_media_library(path)


def test_a_voice_scoped_fallback_shaped_template_does_not_count_as_the_fallback(tmp_path):
    """A true fallback must have `voices == ()` -- a persona-scoped
    template, however bare its triggers, never satisfies Decision P4-25's
    coverage requirement on its own (the identical rule `phrase_service.py`
    's own window-scoped-fallback fix already established for
    negotiation)."""
    payload = _minimal_valid_payload()
    payload["templates"] = [
        t for t in payload["templates"] if t["id"] != "fallback_streak"
    ]
    payload["templates"].append(
        {
            "id": "voice_scoped_not_a_real_fallback", "kinds": ["streak"],
            "voices": ["beat_writer"], "triggers": [], "weight": 1.0, "text": "x", "slots": [],
        }
    )
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="streak"):
        load_media_library(path)


# ---------------------------------------------------------------------------
# The persona-exclusivity floor -- both the declared-number check and the
# real per-persona ratio check.
# ---------------------------------------------------------------------------


def test_raises_when_persona_exclusivity_floor_is_missing(tmp_path):
    payload = _minimal_valid_payload()
    del payload["persona_exclusivity_floor"]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="persona_exclusivity_floor"):
        load_media_library(path)


def test_raises_when_persona_exclusivity_floor_is_out_of_range(tmp_path):
    payload = _minimal_valid_payload()
    payload["persona_exclusivity_floor"] = 1.5
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="0.0, 1.0"):
        load_media_library(path)


def test_raises_when_a_persona_has_zero_exclusive_templates(tmp_path):
    """Critic-finding-B4-shaped check, extended to media: a persona with
    ONLY the shared fallback available for its own relevant kind(s) cannot
    clear a positive floor."""
    payload = _minimal_valid_payload()
    payload["templates"] = [
        t for t in payload["templates"] if not t["id"].startswith("exclusive_beat_writer_")
    ]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="beat_writer"):
        load_media_library(path)


def test_raises_when_every_fallback_weight_is_inflated_including_a_personas_own(tmp_path):
    """A sanity check on the mechanism itself (not the re-scoping design
    -- see the next test for that): inflating EVERY fallback's weight,
    including the ones for kinds a persona itself owns, drowns out that
    persona's own exclusive templates and must still be caught."""
    payload = _minimal_valid_payload()
    for template in payload["templates"]:
        if not template["voices"]:
            template["weight"] = 1000.0
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="failing"):
        load_media_library(path)


def test_multi_voice_template_counts_toward_the_selectable_denominator():
    """Round 12 critic fix (blocking finding 2): a template naming more
    than one voice (`voices: ["beat_writer", "insider"]`) is selectable by
    EITHER persona (`select.py::_template_matches_story`'s own membership
    check), so it must count toward BOTH personas' `selectable_weight` --
    not just whichever persona a tuple-equality check on `voices ==
    (P.key,)` would (wrongly) treat as its sole owner. Before the fix, this
    multi-voice template's weight was excluded from `beat_writer`'s
    denominator entirely (`not t.voices` is `False` for a non-empty tuple,
    so the old `fallback_weight` sum skipped it, and `exclusive_weight`
    also skips it since its `voices` tuple has two entries, not one),
    silently inflating the ratio to `1.0` regardless of real exclusivity."""
    beat_writer = MediaPersona(key="beat_writer", story_kind_weights={"streak": 1.0})
    exclusive = MediaTemplate(
        id="exclusive", kinds=("streak",), voices=("beat_writer",), triggers=(), weight=2.0,
        text="x", slots=(),
    )
    multi_voice = MediaTemplate(
        id="multi_voice", kinds=("streak",), voices=("beat_writer", "insider"), triggers=(),
        weight=50.0, text="x", slots=(),
    )
    # Real ratio: 2.0 / (2.0 + 50.0) == 0.038 -- must fail a 0.5 floor.
    with pytest.raises(ValueError, match="beat_writer"):
        _validate_media_persona_exclusivity((exclusive, multi_voice), (beat_writer,), 0.5)
    # Confirms the fix changed the real NUMBER, not just the failure
    # message -- a floor only the fixed (lower) ratio clears still passes.
    _validate_media_persona_exclusivity((exclusive, multi_voice), (beat_writer,), 0.03)


def test_wildcard_kinds_fallback_counts_toward_the_selectable_denominator():
    """Round 12 critic fix (blocking finding 3): a wildcard-`kinds`
    (`kinds == ()`) fallback is selectable for EVERY kind by EVERY persona
    (`_validate_media_fallback_coverage` explicitly accepts one, and
    `select.py::_template_matches_story` makes it selectable regardless of
    `frame.kind`), so it must count toward every persona's `selectable_
    weight`. Before the fix, `set(t.kinds) & relevant_kinds` was empty for
    `t.kinds == ()` (the empty set intersects nothing), excluding it from
    the denominator entirely and passing at ratio `1.0` no matter how
    little real exclusive weight a persona actually had."""
    beat_writer = MediaPersona(key="beat_writer", story_kind_weights={"streak": 1.0})
    exclusive = MediaTemplate(
        id="exclusive", kinds=("streak",), voices=("beat_writer",), triggers=(), weight=2.0,
        text="x", slots=(),
    )
    wildcard_fallback = MediaTemplate(
        id="fallback_any", kinds=(), voices=(), triggers=(), weight=50.0, text="x", slots=(),
    )
    with pytest.raises(ValueError, match="beat_writer"):
        _validate_media_persona_exclusivity((exclusive, wildcard_fallback), (beat_writer,), 0.5)
    _validate_media_persona_exclusivity((exclusive, wildcard_fallback), (beat_writer,), 0.03)


def test_exclusivity_ratio_scoping_holds_when_only_irrelevant_fallbacks_are_inflated():
    """The real, positive proof for the re-scoping decision, exercising
    `_validate_media_persona_exclusivity` directly (white-box, bypassing
    the full loader -- inflating one kind's fallback necessarily changes
    THAT kind's own owning persona's ratio too under this fixture's
    one-kind-one-owner shape, which would make a full-loader round trip
    fail for an unrelated reason and obscure what this test is actually
    checking): a single persona (`beat_writer`, relevant kinds `{"streak"}`)
    must see an UNCHANGED ratio whether or not an unrelated kind's
    (`"milestone"`) fallback weight is inflated, because that fallback is
    never in `beat_writer`'s own selectable set."""
    beat_writer = MediaPersona(key="beat_writer", story_kind_weights={"streak": 1.0})
    exclusive = MediaTemplate(
        id="exclusive", kinds=("streak",), voices=("beat_writer",), triggers=(), weight=2.0,
        text="x", slots=(),
    )
    own_fallback = MediaTemplate(
        id="fallback_streak", kinds=("streak",), voices=(), triggers=(), weight=1.0,
        text="x", slots=(),
    )
    unrelated_fallback_cheap = MediaTemplate(
        id="fallback_milestone", kinds=("milestone",), voices=(), triggers=(), weight=1.0,
        text="x", slots=(),
    )
    unrelated_fallback_inflated = dataclasses.replace(unrelated_fallback_cheap, weight=1000.0)

    # 2.0 / (2.0 + 1.0) == 0.667, regardless of the unrelated fallback's
    # own weight -- must not raise either way, at a floor of 0.5.
    _validate_media_persona_exclusivity(
        (exclusive, own_fallback, unrelated_fallback_cheap), (beat_writer,), 0.5
    )
    _validate_media_persona_exclusivity(
        (exclusive, own_fallback, unrelated_fallback_inflated), (beat_writer,), 0.5
    )


def test_dead_off_kind_exclusive_template_does_not_inflate_the_numerator():
    """Round 13 critic fix (the one blocking bug round 12's own fix
    introduced) -- the critic's exact repro. `beat_writer`'s real
    exclusive share is thin (`2.0` against a `52.0` denominator, ratio
    `0.038`), which must fail a `0.5` floor -- but round 12's shipped fix
    computed `exclusive_weight` with NO kind filter at all while the
    denominator (`_selectable_weight_for_persona`) DID filter by kind, so
    a DEAD off-kind exclusive template (`kinds=("award_race",)`, a kind
    `beat_writer` has no positive weight for -- `select_persona` can never
    draw `beat_writer` for it, so no real selection can ever reach this
    template) inflated the numerator to `1002.0` (ratio `19.27`),
    incorrectly passing. Both sums must now share the identical kind
    filter (`_selectable_weight_for_persona`'s `exclusive_only` switch), so
    the dead template is excluded from BOTH and the ratio reflects the
    real, thin `0.038` share."""
    beat_writer = MediaPersona(key="beat_writer", story_kind_weights={"streak": 1.0})
    exclusive = MediaTemplate(
        id="exclusive", kinds=("streak",), voices=("beat_writer",), triggers=(), weight=2.0,
        text="x", slots=(),
    )
    fallback = MediaTemplate(
        id="fallback_streak", kinds=("streak",), voices=(), triggers=(), weight=50.0,
        text="x", slots=(),
    )
    dead_off_kind_exclusive = MediaTemplate(
        id="dead_exclusive_award_race", kinds=("award_race",), voices=("beat_writer",),
        triggers=(), weight=1000.0, text="x", slots=(),
    )
    with pytest.raises(ValueError, match="beat_writer"):
        _validate_media_persona_exclusivity(
            (exclusive, fallback, dead_off_kind_exclusive), (beat_writer,), 0.5
        )


def test_dead_off_kind_exclusive_templates_do_not_rescue_an_under_authored_library():
    """Round 13 critic fix, the critic's second repro: a library shaped
    like `test_multi_voice_template_counts_toward_the_selectable_
    denominator`'s own under-authored mutation, scaled to five personas
    (20 all-five-voice weight-50 templates; true selectable share per
    persona is thin, well under a `0.5` floor) must STILL be rejected once
    one dead off-kind exclusive template per persona is added. Before this
    fix, one such dead template per persona inflated every ratio (the
    critic measured 9.8-9.9 against the real shipped library; this
    fixture's own pre-fix ratio is `1002.0 / 1002.0 == 1.0`) and the
    mutated, genuinely under-authored library loaded clean again."""
    personas = tuple(
        MediaPersona(key=key, story_kind_weights={"streak": 1.0}) for key in _PERSONA_KEYS
    )
    exclusive_templates = tuple(
        MediaTemplate(
            id=f"exclusive_{key}", kinds=("streak",), voices=(key,), triggers=(), weight=2.0,
            text="x", slots=(),
        )
        for key in _PERSONA_KEYS
    )
    multi_voice_templates = tuple(
        MediaTemplate(
            id=f"multi_voice_{i}", kinds=("streak",), voices=_PERSONA_KEYS, triggers=(),
            weight=50.0, text="x", slots=(),
        )
        for i in range(20)
    )
    dead_off_kind_exclusive_templates = tuple(
        MediaTemplate(
            id=f"dead_exclusive_{key}", kinds=("award_race",), voices=(key,), triggers=(),
            weight=1000.0, text="x", slots=(),
        )
        for key in _PERSONA_KEYS
    )
    with pytest.raises(ValueError, match="failing"):
        _validate_media_persona_exclusivity(
            exclusive_templates + multi_voice_templates + dead_off_kind_exclusive_templates,
            personas,
            0.5,
        )


# ---------------------------------------------------------------------------
# Positive weights / repeat penalty -- mirrors phrase_service.py's own
# equivalent checks exactly.
# ---------------------------------------------------------------------------


def test_raises_on_non_positive_weight(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"][0]["weight"] = 0.0
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="non-positive weight"):
        load_media_library(path)


def test_raises_when_repeat_penalty_is_out_of_range(tmp_path):
    payload = _minimal_valid_payload()
    payload["repeat_penalty"] = 1.0
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="0.0, 1.0"):
        load_media_library(path)


def test_raises_on_duplicate_template_id(tmp_path):
    payload = _minimal_valid_payload()
    duplicate = copy.deepcopy(payload["templates"][0])
    payload["templates"].append(duplicate)
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="duplicate template id"):
        load_media_library(path)


# ---------------------------------------------------------------------------
# _subject_identity -- Decision P4-54's (subject_kind, subject_id) salt.
# **Retyped from `(frame)` to `(subject)`** (MV-8, revision 5, "Build
# order" step 3a) -- `Subject` fixtures are hand-built directly, the
# shortest real path now that this function reads `Subject`, not
# `StoryFrame` (mirrors this file's own pre-retype convention of building
# the narrowest input this function itself reads).
# ---------------------------------------------------------------------------


def test_subject_identity_prefers_player_when_both_ids_are_present():
    subject = Subject(kind=SubjectKind.PLAYER, team_id=7, player_id=3)
    assert _subject_identity(subject) == ("player", 3)


def test_subject_identity_falls_back_to_team_when_no_player_id():
    subject = Subject(kind=SubjectKind.TEAM, team_id=7)
    assert _subject_identity(subject) == ("team", 7)


def test_subject_identity_matchup_returns_a_distinct_subject_kind_string():
    """MV-8, revision 5: a `MATCHUP` subject salts on a distinct string so
    it can never collide with a `TEAM` package for the same team -- no
    current `StoryKind` produces one yet, but the branch is real."""
    subject = Subject(kind=SubjectKind.MATCHUP, team_id=7)
    assert _subject_identity(subject) == ("matchup", 7)


def test_subject_identity_raises_when_neither_id_is_present():
    subject = Subject(kind=SubjectKind.TEAM, team_id=None, player_id=None)
    with pytest.raises(AssertionError, match="neither player_id nor team_id"):
        _subject_identity(subject)


# ---------------------------------------------------------------------------
# render_story -- pure interpolation, mirrors phrase_service.py::render.
# ---------------------------------------------------------------------------


def test_render_story_interpolates_the_chosen_templates_text():
    library = load_media_library()
    template = next(t for t in library.templates if t.id == "beat_writer_streak_win")
    selection = StorySelection(
        voice="beat_writer", template_id=template.id,
        slot_bindings={"team_name": "Portland Trail Blazers", "streak_length": "9"},
    )
    assert render_story(selection, library) == "Portland Trail Blazers have won 9 in a row."


def test_render_story_raises_on_an_unknown_template_id():
    library = load_media_library()
    selection = StorySelection(voice="beat_writer", template_id="not_a_real_id", slot_bindings={})
    with pytest.raises(KeyError):
        render_story(selection, library)


def test_render_story_capitalizes_a_lowercase_sealed_team_name_at_sentence_start():
    """Round 12 critic fix (nit 2), rebuilt on a real sealed name (media-v2-
    newsroom MV-9, revision 5's "five placeholder-behavior assertions" --
    `tests/unit/services/test_media_service.py:625-643`, "rebuilt on a real
    sealed name"). `select.py`'s old degrade-to-`"the team"` placeholder
    machinery (`_SLOT_RESOLVERS`/`SLOT_NAMES`) is deleted along with the
    behavior it produced -- an absent name now makes a template INELIGIBLE
    rather than rendering a placeholder (MV-1b(c)) -- so this can no longer
    be triggered by a degraded/missing name. `render_story`'s own docstring
    is explicit that the fix still matters for a REAL name, though: nothing
    capitalizes a city/team/player name for this purpose, so a real, sealed
    `team_name` that happens to be lowercase-first (this codebase's own
    `Subject.team_name` is arbitrary league-supplied text, not guaranteed
    title-cased) hits the identical sentence-initial fragment risk. Built
    through the real `StoryCandidate` -> `seal_package` -> `bind_slots`
    path (`_sealed_package`), not a hand-built `slot_bindings` dict, so this
    exercises the real mechanism that produces a `team_name` binding today."""
    library = load_media_library()
    template = next(t for t in library.templates if t.id == "fallback_standings_surge")
    package = _sealed_package(
        StoryCandidate(
            kind=StoryKind.STANDINGS_SURGE, team_id=1, team_games_played_this_season=20,
            has_pulse_snapshot=True, standings_position=3, standings_position_at_last_pulse=8,
        ),
        team_name="trail blazers",
    )
    selection = StorySelection(
        voice="beat_writer", template_id=template.id, slot_bindings=bind_slots(template, package),
    )
    assert render_story(selection, library) == "Trail blazers are trending up in the standings."


def test_render_story_does_not_capitalize_a_mid_sentence_sealed_team_name():
    """The fix touches only the rendered string's first character -- a
    template where a real, sealed `team_name` lands mid-sentence must keep
    that name's own casing untouched. Rebuilt on a real sealed name for the
    same reason `test_render_story_capitalizes_a_lowercase_sealed_team_
    name_at_sentence_start` above is (MV-9's "rebuilt on a real sealed
    name")."""
    library = load_media_library()
    template = next(t for t in library.templates if t.id == "insider_trade_reaction_picks")
    package = _sealed_package(
        StoryCandidate(kind=StoryKind.TRADE_REACTION, team_id=1, trade_in_player_ids=(1,)),
        team_name="the team",
    )
    selection = StorySelection(
        voice="insider", template_id=template.id, slot_bindings=bind_slots(template, package),
    )
    rendered = render_story(selection, library)
    assert rendered == (
        "Sources say the team view this trade as a step toward next year's board, "
        "not just this one."
    )


# ---------------------------------------------------------------------------
# with_voices_disabled -- Decision P4-97's null-control transformation
# (§30.6, modeled on `tools/validate_gm.py::with_philosophies_disabled`).
# ---------------------------------------------------------------------------


def test_with_voices_disabled_forces_every_templates_voices_to_wildcard():
    library = LoadedMediaLibrary(
        templates=(
            MediaTemplate(
                id="t1", kinds=("streak",), voices=("beat_writer",), triggers=(), weight=1.0,
                text="x", slots=(),
            ),
            MediaTemplate(
                id="t2", kinds=("streak",), voices=(), triggers=(), weight=1.0, text="x",
                slots=(),
            ),
        ),
        personas=(),
        repeat_penalty=0.5,
        persona_exclusivity_floor=0.5,
        fallback_by_kind={},
    )
    disabled = with_voices_disabled(library)
    assert [t.id for t in disabled.templates] == ["t1", "t2"]
    assert all(t.voices == () for t in disabled.templates)
    for original, transformed in zip(library.templates, disabled.templates, strict=True):
        assert transformed.kinds == original.kinds
        assert transformed.triggers == original.triggers
        assert transformed.weight == original.weight
        assert transformed.text == original.text
        assert transformed.slots == original.slots
    # The real, already-loaded library object is never mutated in place.
    assert library.templates[0].voices == ("beat_writer",)


def test_with_voices_disabled_preserves_the_real_fallback_by_kind_mapping():
    """M2 regression (media-v2-newsroom step 3a critic round): this used to
    call `_fallback_templates_by_kind(new_templates)` -- re-deriving the map
    from the TRANSFORMED (every-voices-wildcarded) template list. Every
    fallback already has `voices == ()` (Decision P4-25), so that reasoning
    looked safe, but `_fallback_templates_by_kind`'s own per-kind scan is
    first-match-wins over every no-trigger, wildcard-voice template --
    wildcarding every OTHER template's `voices` too makes any no-trigger
    dedicated template look like a fallback candidate for that same scan.
    On the real shipped library this silently resolved a template other
    than the kind's real `fallback_<kind>` for six of eleven kinds, and
    re-validating the transformed templates against `_validate_media_
    fallback_coverage` actually raises `ValueError` ("more than one
    fallback for: ...") -- proof the transformed library violates the
    exactly-one-fallback-per-kind invariant the real library satisfies.
    The fix keeps the untransformed library's own `fallback_by_kind`
    (computed once, correctly, at load time) and only re-points each entry
    at its own already-transformed object."""
    library = load_media_library()
    disabled = with_voices_disabled(library)
    assert disabled.fallback_by_kind == library.fallback_by_kind
    assert {kind: t.id for kind, t in disabled.fallback_by_kind.items()} == {
        kind: t.id for kind, t in library.fallback_by_kind.items()
    }


def test_with_voices_disabled_keeps_the_real_fallback_over_a_first_scanned_dedicated_template():
    """The precise, hand-built repro of the M2 bug -- a dedicated, no-
    trigger template listed BEFORE the kind's real fallback, so `_fallback_
    templates_by_kind`'s first-match-wins scan would pick it once every
    template's `voices` is wildcarded, unless `fallback_by_kind` is
    preserved by real identity rather than re-derived."""
    fallback = MediaTemplate(
        id="fallback_streak", kinds=("streak",), voices=(), triggers=(), weight=1.0,
        text="fallback line", slots=(),
    )
    dedicated = MediaTemplate(
        id="dedicated_streak", kinds=("streak",), voices=("beat_writer",), triggers=(),
        weight=1.0, text="dedicated line", slots=(),
    )
    library = LoadedMediaLibrary(
        templates=(dedicated, fallback),  # dedicated scans first
        personas=(),
        repeat_penalty=0.5,
        persona_exclusivity_floor=0.5,
        fallback_by_kind={StoryKind.STREAK: fallback},
    )
    disabled = with_voices_disabled(library)
    assert disabled.fallback_by_kind[StoryKind.STREAK].id == "fallback_streak"


# ---------------------------------------------------------------------------
# docs/design/media-v2-newsroom.md MV-1c.4, "Build order" step 3b -- the
# pulse/selection split. MV-1c.4/B5's own named acceptance test: the pulse
# cycle's signature must stay exactly what it was before the split, because
# the corpus tool's "one pulse call correctly feeds both arms" invariant
# depends on this function being unable to see the library or the phase.
# ---------------------------------------------------------------------------


def test_run_media_pulse_cycle_signature_is_unchanged_by_the_selection_split():
    params = list(inspect.signature(run_media_pulse_cycle).parameters)
    assert params == ["ex", "league_id", "season", "cycle_index"], (
        "run_media_pulse_cycle must gain neither `phase` nor `library` -- MV-1c.4's own rule "
        "is that everything arm-dependent (scoring, the library, novelty history) moves to "
        "the SELECTION side, and this signature is the provable form of that rule"
    )


def _subject(team_id: int) -> Subject:
    return Subject(kind=SubjectKind.TEAM, team_id=team_id, team_name="A Team")


class TestCyclesSinceLastSeen:
    """`media_service._cycles_since_last_seen` -- MV-1c.4/MV-10's real
    mechanism for `run_media_selection_cycle`'s `subject_last_seen`
    parameter. No production caller populates the map yet (season_service
    and the corpus tool both pass `None` in this build step), but the
    function itself is real and tested here directly."""

    def test_absent_subject_reads_as_none_not_a_fabricated_zero(self):
        assert _cycles_since_last_seen({}, subject=_subject(1), cycle_index=5) is None

    def test_a_real_last_seen_cycle_becomes_a_real_distance(self):
        last_seen = {("team", 1): 3}
        assert _cycles_since_last_seen(last_seen, subject=_subject(1), cycle_index=7) == 4

    def test_seen_this_same_cycle_reads_zero_not_none(self):
        last_seen = {("team", 1): 5}
        assert _cycles_since_last_seen(last_seen, subject=_subject(1), cycle_index=5) == 0

    def test_a_malformed_future_last_seen_clamps_to_zero_not_negative(self):
        """A caller-supplied `last_seen` cycle AFTER the current one is malformed input --
        this function still returns a real, non-negative int (`salience._novelty`'s own
        `max(cycles_since_last_seen, 0)` would clamp it anyway, but this function does not
        rely on its one caller for that)."""
        last_seen = {("team", 1): 9}
        assert _cycles_since_last_seen(last_seen, subject=_subject(1), cycle_index=5) == 0

    def test_a_different_subject_in_the_map_does_not_affect_this_one(self):
        last_seen = {("team", 2): 0}
        assert _cycles_since_last_seen(last_seen, subject=_subject(1), cycle_index=5) is None


# ---------------------------------------------------------------------------
# `Publication`/`MediaSelectionCycleResult` -- MV-1c.5's carried pairing.
# ---------------------------------------------------------------------------


def test_publication_and_selection_result_are_frozen_and_carry_the_named_fields():
    """A cheap construction-time proof that the two new result shapes exist with the
    fields MV-1c.5 names -- the real, DB-backed proof that `run_media_selection_cycle`
    actually populates them lives in `tests/integration/test_media_service.py`."""
    package = seal_package(
        legacy.adapt_frame(
            derive_frame(
                StoryCandidate(
                    kind=StoryKind.STREAK, team_id=1, team_games_played_this_season=20,
                    has_pulse_snapshot=True, streak_length=9,
                )
            ),
            season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
        ),
        team_names={1: "A Team"},
        player_names={},
    )
    assert package is not None
    scored = ScoredCandidate(candidate=SalienceCandidate(package=package), score=0.9)
    publication = media_service.Publication(candidate_sequence=0, scored=scored, story=None)
    assert publication.candidate_sequence == 0
    assert publication.scored is scored

    result = media_service.MediaSelectionCycleResult(
        publications=(publication,),
        stories=(),
        candidates_scored=1,
        candidates_above_floor=1,
        plan_size=1,
        capped_out=0,
        fallback_forced=0,
        band_published=0,
        profiles_published=0,
    )
    assert result.plan_size == 1
    assert result.capped_out == 0 == result.fallback_forced == result.band_published


# ---------------------------------------------------------------------------
# docs/design/media-v2-newsroom.md "Build order" step 5 -- persona metadata,
# phase_cadence, team_profile_quota, and the two acceptance criteria the
# coordinator named directly: load_media_library must return without
# raising, AND hot_take's exclusivity ratio must be >= 0.5.
# ---------------------------------------------------------------------------


def test_the_shipped_library_still_loads_after_step_5():
    """The coordinator's own first named acceptance criterion, checked directly."""
    library = load_media_library()
    assert library.persona_metadata
    assert library.phase_cadence
    assert library.team_profile_quota == 3


def test_hot_takes_exclusivity_ratio_is_at_least_half():
    """MV-3h.0's own load-enforced constraint, checked directly rather than only
    relying on load_media_library not raising (which already implies it, via
    _validate_media_persona_exclusivity -- this test names the real number)."""
    library = load_media_library()
    hot_take = next(p for p in library.personas if p.key == "hot_take")
    relevant_kinds = set(hot_take.story_kind_weights)
    exclusive = media_service._selectable_weight_for_persona(
        library.templates, persona_key="hot_take", relevant_kinds=relevant_kinds,
        exclusive_only=True,
    )
    selectable = media_service._selectable_weight_for_persona(
        library.templates, persona_key="hot_take", relevant_kinds=relevant_kinds,
        exclusive_only=False,
    )
    assert selectable > 0
    assert exclusive / selectable >= 0.5


def test_hot_take_covers_exactly_the_mv_3_declared_twelve_kinds():
    library = load_media_library()
    hot_take = next(p for p in library.personas if p.key == "hot_take")
    covered = {k for k, w in hot_take.story_kind_weights.items() if w > 0.0}
    assert covered == {
        "trade_executed", "trade_broken_promise", "trade_reaction", "trade_fallout",
        "trade_market_watch", "revenge_game", "player_unrest", "holdout_watch",
        "cold_stretch", "standings_collapse", "rivalry_matchup", "roster_construction",
    }
    assert len(covered) == 12


def test_every_persona_has_real_metadata_in_the_shipped_library():
    library = load_media_library()
    assert set(library.persona_metadata) == set(_PERSONA_KEYS)
    for metadata in library.persona_metadata.values():
        assert metadata.column
        assert metadata.beat_description
        assert isinstance(metadata.register, media_service.Register)
        assert len(metadata.voice_rules) >= 1


def test_only_hot_take_and_beat_writer_declare_a_fixed_display_name():
    library = load_media_library()
    assert library.persona_metadata["hot_take"].display_name == "Duke Sabatino"
    assert library.persona_metadata["beat_writer"].display_name == "Jordan Ellis"
    for key in ("insider", "analyst", "historian"):
        assert library.persona_metadata[key].display_name is None


def test_phase_cadence_covers_every_persona_and_every_cycle_bearing_phase():
    library = load_media_library()
    assert set(library.phase_cadence) == set(_PERSONA_KEYS)
    for per_phase in library.phase_cadence.values():
        assert set(per_phase) == _CYCLE_BEARING_PHASE_VALUES
        for cadence in per_phase.values():
            assert cadence.weight_multiplier > 0
            assert cadence.max_per_cycle > 0


def test_insiders_trade_deadline_weight_exceeds_regular_season_and_post_deadline():
    """MV-3's own cadence table, the commissioner's literal requirement pinned as
    a test: the insider's shape actually changes across the season."""
    library = load_media_library()
    insider = library.phase_cadence["insider"]
    trade_deadline = insider["TRADE_DEADLINE"].weight_multiplier
    assert trade_deadline > insider["REGULAR_SEASON"].weight_multiplier
    assert trade_deadline > insider["POST_DEADLINE"].weight_multiplier


def test_hot_take_has_a_positive_cadence_weight_in_every_phase():
    """MV-3h's own 'always available' property, pinned so a later edit cannot
    silently make hot_take seasonal."""
    library = load_media_library()
    for cadence in library.phase_cadence["hot_take"].values():
        assert cadence.weight_multiplier > 0


# ---------------------------------------------------------------------------
# resolve_persona_voice
# ---------------------------------------------------------------------------


def test_resolve_persona_voice_uses_the_declared_display_name_when_present():
    metadata = media_service.PersonaMetadata(
        column="No Filter", display_name="Duke Sabatino", beat_description="loud",
        register=media_service.Register.INFLAMMATORY, voice_rules=("be loud",), fences=(),
    )
    voice = media_service.resolve_persona_voice(metadata, "hot_take", generated_name="Alex Bishop")
    assert voice.display_name == "Duke Sabatino"
    assert voice.key == "hot_take"
    assert voice.column == "No Filter"


def test_resolve_persona_voice_falls_back_to_the_generated_name_when_absent():
    metadata = media_service.PersonaMetadata(
        column="The Trade Desk", display_name=None, beat_description="trades",
        register=media_service.Register.REPORTORIAL, voice_rules=("be factual",), fences=(),
    )
    voice = media_service.resolve_persona_voice(metadata, "insider", generated_name="Alex Bishop")
    assert voice.display_name == "Alex Bishop"


# ---------------------------------------------------------------------------
# Persona-metadata / phase_cadence / team_profile_quota validators
# ---------------------------------------------------------------------------


def test_raises_when_a_persona_is_missing_column(tmp_path):
    payload = _minimal_valid_payload()
    del payload["personas"][0]["column"]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="column"):
        load_media_library(path)


def test_raises_on_an_unknown_register(tmp_path):
    payload = _minimal_valid_payload()
    payload["personas"][0]["register"] = "sarcastic"
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="register"):
        load_media_library(path)


def test_raises_on_empty_voice_rules(tmp_path):
    payload = _minimal_valid_payload()
    payload["personas"][0]["voice_rules"] = []
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="voice_rules"):
        load_media_library(path)


def test_empty_fences_is_allowed():
    """Not every persona needs a fence -- only hot_take's own is mandated by MV-3h."""
    payload = _minimal_valid_payload()
    payload["personas"][0]["fences"] = []
    # must not raise
    media_service._validate_media_persona_metadata(payload["personas"])


def test_raises_on_a_non_string_display_name(tmp_path):
    payload = _minimal_valid_payload()
    payload["personas"][0]["display_name"] = 12345
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="display_name"):
        load_media_library(path)


def test_raises_when_phase_cadence_is_missing(tmp_path):
    payload = _minimal_valid_payload()
    del payload["phase_cadence"]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="phase_cadence"):
        load_media_library(path)


def test_raises_when_phase_cadence_omits_a_persona(tmp_path):
    payload = _minimal_valid_payload()
    del payload["phase_cadence"]["hot_take"]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="phase_cadence"):
        load_media_library(path)


def test_raises_when_phase_cadence_omits_a_cycle_bearing_phase(tmp_path):
    payload = _minimal_valid_payload()
    del payload["phase_cadence"]["hot_take"]["WAIVERS"]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="phase_cadence"):
        load_media_library(path)


def test_raises_on_a_non_positive_cadence_weight(tmp_path):
    payload = _minimal_valid_payload()
    payload["phase_cadence"]["hot_take"]["WAIVERS"] = [0.0, 3]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="weight"):
        load_media_library(path)


def test_raises_on_a_non_positive_cadence_cap(tmp_path):
    payload = _minimal_valid_payload()
    payload["phase_cadence"]["hot_take"]["WAIVERS"] = [1.0, 0]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="max_per_cycle"):
        load_media_library(path)


def test_raises_when_team_profile_quota_is_missing(tmp_path):
    payload = _minimal_valid_payload()
    del payload["team_profile_quota"]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="team_profile_quota"):
        load_media_library(path)


def test_raises_when_team_profile_quota_is_not_positive(tmp_path):
    payload = _minimal_valid_payload()
    payload["team_profile_quota"] = 0
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="team_profile_quota"):
        load_media_library(path)


# ---------------------------------------------------------------------------
# docs/design/media-v2-newsroom.md "Build order" step 6 -- the pure helpers
# `run_media_selection_cycle` composes: `_hot_take_kinds`, `_has_cap_
# headroom`/`_persona_multipliers_for`/`_eligible_personas_under_cap`
# (MV-3h/MV-1c.3), `_subject_prominence` (MV-5).
# ---------------------------------------------------------------------------


def test_hot_take_kinds_reads_the_real_shipped_beat():
    library = load_media_library()
    kinds = _hot_take_kinds(library.personas)
    assert kinds == {
        StoryKind.TRADE_EXECUTED, StoryKind.TRADE_BROKEN_PROMISE, StoryKind.TRADE_REACTION,
        StoryKind.TRADE_FALLOUT, StoryKind.TRADE_MARKET_WATCH, StoryKind.REVENGE_GAME,
        StoryKind.PLAYER_UNREST, StoryKind.HOLDOUT_WATCH, StoryKind.COLD_STRETCH,
        StoryKind.STANDINGS_COLLAPSE, StoryKind.RIVALRY_MATCHUP, StoryKind.ROSTER_CONSTRUCTION,
    }


def test_hot_take_kinds_empty_when_no_hot_take_persona_exists():
    assert _hot_take_kinds(()) == frozenset()


def _library_with_cadence(phase_cadence: dict[str, dict[str, PhaseCadence]]) -> LoadedMediaLibrary:
    personas = (
        MediaPersona(key="insider", story_kind_weights={"trade_executed": 1.0}),
        MediaPersona(key="hot_take", story_kind_weights={"trade_executed": 1.0}),
    )
    return LoadedMediaLibrary(
        templates=(), personas=personas, repeat_penalty=0.3, persona_exclusivity_floor=0.5,
        fallback_by_kind={}, phase_cadence=phase_cadence,
    )


def test_has_cap_headroom_true_when_library_names_no_cadence_for_this_persona_or_phase():
    library = _library_with_cadence({})
    assert _has_cap_headroom(library, "insider", Phase.REGULAR_SEASON, {}) is True
    assert _has_cap_headroom(library, "insider", Phase.REGULAR_SEASON, {"insider": 999}) is True


def test_has_cap_headroom_false_once_draws_so_far_reaches_the_real_cap():
    library = _library_with_cadence(
        {"insider": {"REGULAR_SEASON": PhaseCadence(weight_multiplier=1.0, max_per_cycle=2)}}
    )
    assert _has_cap_headroom(library, "insider", Phase.REGULAR_SEASON, {"insider": 1}) is True
    assert _has_cap_headroom(library, "insider", Phase.REGULAR_SEASON, {"insider": 2}) is False


def test_persona_multipliers_for_reads_the_real_cadence_weight():
    library = _library_with_cadence(
        {"insider": {"REGULAR_SEASON": PhaseCadence(weight_multiplier=2.5, max_per_cycle=5)}}
    )
    multipliers = _persona_multipliers_for(library, phase=Phase.REGULAR_SEASON, angle=Angle.STREAK)
    assert multipliers["insider"] == pytest.approx(2.5)
    assert multipliers["hot_take"] == pytest.approx(1.0), "no cadence cell named -- default 1.0"


def test_persona_multipliers_for_applies_the_drama_bias_to_hot_take_only():
    library = _library_with_cadence(
        {"hot_take": {"REGULAR_SEASON": PhaseCadence(weight_multiplier=1.4, max_per_cycle=2)}}
    )
    multipliers = _persona_multipliers_for(
        library, phase=Phase.REGULAR_SEASON, angle=Angle.PROMISE_BROKEN
    )
    assert multipliers["hot_take"] == pytest.approx(1.4 * 1.6)
    assert multipliers["insider"] == pytest.approx(1.0)


def test_persona_multipliers_for_no_drama_bias_on_a_calm_angle():
    library = _library_with_cadence(
        {"hot_take": {"REGULAR_SEASON": PhaseCadence(weight_multiplier=1.4, max_per_cycle=2)}}
    )
    multipliers = _persona_multipliers_for(library, phase=Phase.REGULAR_SEASON, angle=Angle.STREAK)
    assert multipliers["hot_take"] == pytest.approx(1.4)


def test_eligible_personas_under_cap_excludes_a_persona_with_no_positive_weight():
    library = _library_with_cadence({})
    eligible = _eligible_personas_under_cap(
        library, StoryKind.TRADE_EXECUTED, phase=Phase.REGULAR_SEASON,
        group_restriction=None, draws_so_far={},
    )
    assert eligible == {"insider", "hot_take"}


def test_eligible_personas_under_cap_applies_the_group_restriction():
    library = _library_with_cadence({})
    eligible = _eligible_personas_under_cap(
        library, StoryKind.TRADE_EXECUTED, phase=Phase.REGULAR_SEASON,
        group_restriction=frozenset({"hot_take"}), draws_so_far={},
    )
    assert eligible == {"hot_take"}


def test_eligible_personas_under_cap_empty_once_every_eligible_persona_is_at_cap():
    library = _library_with_cadence(
        {
            "insider": {"REGULAR_SEASON": PhaseCadence(weight_multiplier=1.0, max_per_cycle=1)},
            "hot_take": {"REGULAR_SEASON": PhaseCadence(weight_multiplier=1.0, max_per_cycle=1)},
        }
    )
    eligible = _eligible_personas_under_cap(
        library, StoryKind.TRADE_EXECUTED, phase=Phase.REGULAR_SEASON,
        group_restriction=None, draws_so_far={"insider": 1, "hot_take": 1},
    )
    assert eligible == frozenset()


def test_subject_prominence_none_for_a_team_subject():
    package = _sealed_package(
        StoryCandidate(
            kind=StoryKind.STREAK, team_id=1, team_games_played_this_season=20,
            has_pulse_snapshot=True, streak_length=9,
        )
    )
    assert _subject_prominence(package, {}) is None


def _sealed_player_unrest_package(player_id: int) -> StoryPackage:
    from dba2.domain.motivation import MotivationAxis

    return _sealed_package(
        StoryCandidate(
            kind=StoryKind.PLAYER_UNREST, player_id=player_id,
            player_games_played_this_season=0, motivation_axis=MotivationAxis.ROLE_USAGE,
            motivation_magnitude=-40,
        )
    )


def test_subject_prominence_scales_overall_cached_into_zero_one():
    package = _sealed_player_unrest_package(7)
    assert _subject_prominence(package, {7: 60}) == pytest.approx(0.0)
    assert _subject_prominence(package, {7: 95}) == pytest.approx(1.0)
    assert _subject_prominence(package, {7: 77.5}) == pytest.approx(0.5)


def test_subject_prominence_none_when_player_id_absent_from_the_bulk_map():
    package = _sealed_player_unrest_package(7)
    assert _subject_prominence(package, {}) is None
