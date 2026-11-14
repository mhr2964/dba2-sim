"""`dba2/media/select.py` -- Decisions P4-26 (persona weighted choice),
P4-22/P4-38/P4-54 (media's own `select_media_template`/`bind_slots`),
docs/design/phase-4-media-and-player-agency.md §11 Stage D step 18.

**Retyped to `StoryPackage` (docs/design/media-v2-newsroom.md MV-1b(c),
MV-1c, "Build order" step 3a).** `StoryPackage` fixtures below are built
through the REAL production path -- `derive_frame` -> `dba2/media/detect/
legacy.py::adapt_frame` -> `dba2.services.media_service.seal_package` --
the retyped analogue of this file's own prior convention of hand-building
a `StoryFrame` directly via `derive_frame` (this file tests the SELECTOR,
not how its package was produced).

Adversarial cases this dispatch specifically asked for: a story kind with
only one eligible persona (no real weighted choice needed); a candidate
whose package has some but not all the identity fields a chosen template's
slots need; a story kind where the library has no non-fallback template
for the drawn persona (must degrade to the fallback, never crash); the
totality/must-never-raise property for the whole select-persona-then-
select-template-then-bind-slots pipeline; and (MV-1c.2, new to this
retype) a survivor set that is empty even though a fallback exists only in
`fallback_by_kind`, never in the `templates` sequence itself -- the direct
proof `select_media_template` resolves the fallback by LOOKUP, never by
trusting `rng.choices` on a possibly-empty list.
"""
from __future__ import annotations

import random

import pytest

from dba2.domain.media_facts import StoryPackage
from dba2.domain.media_writer import FormatTier
from dba2.domain.motivation import RequestState
from dba2.domain.phase import Phase
from dba2.media.detect import legacy
from dba2.media.frame import StoryCandidate, StoryKind, derive_frame
from dba2.media.select import (
    DRAMA_ANGLES,
    MediaPersona,
    MediaTemplate,
    MediaTrigger,
    bind_slots,
    select_media_template,
    select_persona,
    template_eligible,
)
from dba2.services.media_service import load_media_library, seal_package

_SEASON = 2026
_PHASE = Phase.REGULAR_SEASON
_CYCLE_INDEX = 3
_TIER = FormatTier.BLURB


def _package(
    candidate: StoryCandidate, *, team_name: str = "A Team", player_name: str = "A Player"
) -> StoryPackage:
    """The shortest REAL path from a hand-built `StoryCandidate` to a
    sealed `StoryPackage` -- see `tests/unit/test_media_salience.py`'s own
    identical helper for the full reasoning (duplicated here rather than
    imported, matching this codebase's existing convention of each test
    file owning its own small fixture helpers)."""
    frame = derive_frame(candidate)
    draft = legacy.adapt_frame(frame, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX)
    assert draft is not None, f"candidate for {candidate.kind} was insufficient -- fix the fixture"
    team_names = {candidate.team_id: team_name} if candidate.team_id is not None else {}
    player_names = {candidate.player_id: player_name} if candidate.player_id is not None else {}
    sealed = seal_package(draft, team_names=team_names, player_names=player_names)
    assert sealed is not None, f"candidate for {candidate.kind} failed to seal -- fix the fixture"
    return sealed


def _persona(key: str, **weights: float) -> MediaPersona:
    return MediaPersona(key=key, story_kind_weights=dict(weights))


def _template(
    template_id: str,
    *,
    kinds: tuple[str, ...] = (),
    voices: tuple[str, ...] = (),
    triggers: tuple[MediaTrigger, ...] = (),
    weight: float = 1.0,
    text: str = "line",
    slots: tuple[str, ...] = (),
    angles: tuple[str, ...] = (),
    tiers: tuple[str, ...] = (),
) -> MediaTemplate:
    return MediaTemplate(
        id=template_id, kinds=kinds, voices=voices, triggers=triggers,
        weight=weight, text=text, slots=slots, angles=angles, tiers=tiers,
    )


def _streak_package(streak_length: int = 9, *, team_id: int | None = 1) -> StoryPackage:
    return _package(
        StoryCandidate(
            kind=StoryKind.STREAK, team_id=team_id, team_games_played_this_season=20,
            has_pulse_snapshot=True, streak_length=streak_length,
        ),
        team_name="Portland Trail Blazers",
    )


# ---------------------------------------------------------------------------
# select_persona -- retyped `(frame, ...)` -> `(story_kind, ...)`.
# ---------------------------------------------------------------------------


def test_only_personas_with_a_positive_weight_for_the_kind_are_eligible():
    beat_writer = _persona("beat_writer", streak=1.0)
    hot_take = _persona("hot_take", player_unrest=1.0)  # zero (absent) weight for streak
    for seed in range(20):
        chosen = select_persona(StoryKind.STREAK, (beat_writer, hot_take), rng=random.Random(seed))
        assert chosen.key == "beat_writer"


def test_a_kind_with_only_one_eligible_persona_needs_no_real_weighted_choice():
    """Adversarial case explicitly requested: one eligible persona."""
    only = _persona("historian", streak=0.5)
    for seed in range(10):
        assert select_persona(StoryKind.STREAK, (only,), rng=random.Random(seed)).key == "historian"


def test_zero_weight_is_treated_as_absent_never_eligible():
    zero_weight = _persona("beat_writer", streak=0.0)
    with pytest.raises(AssertionError, match="STREAK"):
        select_persona(StoryKind.STREAK, (zero_weight,), rng=random.Random(1))


def test_no_eligible_persona_raises_loudly_rather_than_silently_picking_one():
    unrelated = _persona("hot_take", player_unrest=1.0)
    with pytest.raises(AssertionError, match="StoryKind.STREAK"):
        select_persona(StoryKind.STREAK, (unrelated,), rng=random.Random(1))


def test_selection_is_deterministic_from_rng_alone_across_repeated_calls():
    a = _persona("beat_writer", streak=1.0)
    b = _persona("historian", streak=1.0)
    results = {
        select_persona(StoryKind.STREAK, (a, b), rng=random.Random(42)).key for _ in range(20)
    }
    assert len(results) == 1


def test_weighted_choice_is_measurably_skewed_by_the_preference_vector():
    heavy = _persona("beat_writer", streak=9.0)
    light = _persona("historian", streak=1.0)
    picks = [
        select_persona(StoryKind.STREAK, (heavy, light), rng=random.Random(seed)).key
        for seed in range(300)
    ]
    heavy_share = picks.count("beat_writer") / len(picks)
    assert heavy_share > 0.8


def test_selection_is_stable_under_persona_tuple_ordering():
    """Mirrors `test_select.py`'s own step-4 determinism test -- iterating
    the SAME personas in a different order must not change which one a
    fixed `rng` state produces, since `select_persona` sorts by `.key`
    before drawing."""
    a = _persona("beat_writer", streak=1.0)
    b = _persona("historian", streak=98.0)
    forward = select_persona(StoryKind.STREAK, (a, b), rng=random.Random(139))
    shuffled = select_persona(StoryKind.STREAK, (b, a), rng=random.Random(139))
    assert forward.key == shuffled.key


def test_eligible_personas_further_restricts_the_positive_weight_filter():
    """MV-3h's own new keyword parameter (not consumed by any real caller
    in this build step) -- when given, a persona must ALSO be in this set,
    on top of carrying a positive weight."""
    a = _persona("beat_writer", streak=1.0)
    b = _persona("historian", streak=1.0)
    chosen = select_persona(
        StoryKind.STREAK, (a, b), rng=random.Random(1), eligible_personas=frozenset({"historian"})
    )
    assert chosen.key == "historian"
    with pytest.raises(AssertionError, match="STREAK"):
        select_persona(
            StoryKind.STREAK, (a, b), rng=random.Random(1),
            eligible_personas=frozenset({"hot_take"}),
        )


def test_persona_multipliers_skew_the_weighted_draw():
    a = _persona("beat_writer", streak=1.0)
    b = _persona("historian", streak=1.0)
    picks = [
        select_persona(
            StoryKind.STREAK, (a, b), rng=random.Random(seed),
            persona_multipliers={"beat_writer": 9.0},
        ).key
        for seed in range(300)
    ]
    assert picks.count("beat_writer") / len(picks) > 0.8


# ---------------------------------------------------------------------------
# template_eligible -- MV-1c.6's one exported eligibility predicate.
# ---------------------------------------------------------------------------


def test_template_eligible_checks_kind_voice_angle_tier_and_slots():
    package = _streak_package()
    matches = _template("t", kinds=("streak",), voices=("beat_writer",), slots=("team_name",))
    assert template_eligible(matches, package, voice="beat_writer", tier=_TIER)

    wrong_kind = _template("t", kinds=("milestone",), voices=())
    assert not template_eligible(wrong_kind, package, voice="beat_writer", tier=_TIER)

    wrong_voice = _template("t", kinds=("streak",), voices=("historian",))
    assert not template_eligible(wrong_voice, package, voice="beat_writer", tier=_TIER)

    wrong_angle = _template("t", kinds=("streak",), voices=(), angles=("collapse",))
    assert not template_eligible(wrong_angle, package, voice="beat_writer", tier=_TIER)

    wrong_tier = _template("t", kinds=("streak",), voices=(), tiers=("article",))
    assert not template_eligible(wrong_tier, package, voice="beat_writer", tier=_TIER)


def test_template_eligible_excludes_a_template_whose_slot_the_package_lacks():
    """MV-6's own replacement for the deleted degrade-to-placeholder
    behavior: a template declaring a slot this package does not carry is
    NOT ELIGIBLE, never rendered with a placeholder."""
    package = _streak_package()  # no player_name fact on a TEAM-subject package
    needs_player_name = _template("t", kinds=("streak",), slots=("player_name",))
    assert not template_eligible(needs_player_name, package, voice="beat_writer", tier=_TIER)


def test_template_eligible_wildcard_kinds_voices_angles_tiers_all_match():
    package = _streak_package()
    wildcard = _template("t")
    assert template_eligible(wildcard, package, voice="anyone", tier=FormatTier.ARTICLE)


# ---------------------------------------------------------------------------
# select_media_template
# ---------------------------------------------------------------------------


def test_wildcard_voice_fallback_matches_any_persona():
    package = _streak_package()
    fallback = _template("fallback_streak", kinds=("streak",), voices=())
    for voice in ("beat_writer", "historian", "hot_take"):
        chosen = select_media_template(
            package, (fallback,), voice=voice, tier=_TIER,
            fallback_by_kind={StoryKind.STREAK: fallback}, use_counts={},
            repeat_penalty=0.25, rng=random.Random(1),
        )
        assert chosen.id == "fallback_streak"


def test_non_wildcard_voice_excludes_a_non_matching_persona():
    package = _streak_package()
    beat_writer_only = _template(
        "beat_writer_only", kinds=("streak",), voices=("beat_writer",), weight=1.0,
    )
    fallback = _template("fallback", kinds=("streak",), voices=(), weight=1.0)
    fallback_by_kind = {StoryKind.STREAK: fallback}

    chosen_for_owner = select_media_template(
        package, (beat_writer_only, fallback), voice="beat_writer", tier=_TIER,
        fallback_by_kind=fallback_by_kind, use_counts={"fallback": 999},
        repeat_penalty=0.01, rng=random.Random(1),
    )
    assert chosen_for_owner.id == "beat_writer_only"

    chosen_for_other = select_media_template(
        package, (beat_writer_only, fallback), voice="historian", tier=_TIER,
        fallback_by_kind=fallback_by_kind, use_counts={}, repeat_penalty=0.25, rng=random.Random(1),
    )
    assert chosen_for_other.id == "fallback"


def test_kind_dimension_also_filters():
    package = _streak_package()
    wrong_kind = _template("wrong_kind", kinds=("milestone",), voices=())
    right_kind = _template("right_kind", kinds=("streak",), voices=())
    fallback_by_kind = {StoryKind.STREAK: right_kind}
    chosen = select_media_template(
        package, (wrong_kind, right_kind), voice="beat_writer", tier=_TIER,
        fallback_by_kind=fallback_by_kind, use_counts={}, repeat_penalty=0.25, rng=random.Random(1),
    )
    assert chosen.id == "right_kind"


def test_a_kind_with_no_persona_owned_template_degrades_to_the_fallback_never_crashes():
    """Adversarial case explicitly requested: the drawn persona has no
    dedicated (non-fallback) template for this kind -- must degrade, never
    raise from an empty survivor list."""
    package = _streak_package()
    other_persona_only = _template("hot_take_only", kinds=("streak",), voices=("hot_take",))
    fallback = _template("fallback", kinds=("streak",), voices=())
    chosen = select_media_template(
        package, (other_persona_only, fallback), voice="beat_writer", tier=_TIER,
        fallback_by_kind={StoryKind.STREAK: fallback}, use_counts={},
        repeat_penalty=0.25, rng=random.Random(1),
    )
    assert chosen.id == "fallback"


def test_trigger_gates_a_dedicated_template_leaving_only_the_fallback():
    package = _streak_package(streak_length=3)  # below the big-streak trigger threshold
    big_streak = _template(
        "big_streak", kinds=("streak",), voices=("beat_writer",),
        triggers=(MediaTrigger(feature="streak_length", op="gte", value=8),),
    )
    fallback = _template("fallback", kinds=("streak",), voices=())
    fallback_by_kind = {StoryKind.STREAK: fallback}
    chosen = select_media_template(
        package, (big_streak, fallback), voice="beat_writer", tier=_TIER,
        fallback_by_kind=fallback_by_kind, use_counts={}, repeat_penalty=0.25, rng=random.Random(1),
    )
    assert chosen.id == "fallback"

    package_big = _streak_package(streak_length=9)
    chosen_big = select_media_template(
        package_big, (big_streak, fallback), voice="beat_writer", tier=_TIER,
        fallback_by_kind=fallback_by_kind, use_counts={"fallback": 999},
        repeat_penalty=0.01, rng=random.Random(1),
    )
    assert chosen_big.id == "big_streak"


def test_survivor_set_empty_resolves_fallback_by_lookup_not_from_templates_list():
    """MV-1c.2, mechanism two -- the direct proof. `templates` here does
    NOT include a wildcard-voice fallback at all (the OLD structural
    guarantee, "the survivor list can never be empty," is deliberately
    broken); only `fallback_by_kind` knows about one. If `select_media_
    template` ever fell back to `engine.select_template`'s own `rng.
    choices` on an empty survivor list, this would raise `IndexError`."""
    package = _streak_package(streak_length=1)
    dedicated = _template(
        "dedicated", kinds=("streak",), voices=("beat_writer",),
        triggers=(MediaTrigger(feature="streak_length", op="gte", value=99),),
    )
    fallback = _template("fallback_streak", kinds=("streak",), voices=())
    chosen = select_media_template(
        package, (dedicated,), voice="beat_writer", tier=_TIER,
        fallback_by_kind={StoryKind.STREAK: fallback}, use_counts={},
        repeat_penalty=0.25, rng=random.Random(1),
    )
    assert chosen.id == "fallback_streak"


def test_angle_mismatch_forces_the_fallback_never_an_index_error():
    """MV-1c.2's own `IndexError` reproduction, closed: a package whose
    angle is NOT one of a dedicated template's declared `angles` is
    ineligible for it -- with no other dedicated template and no wildcard
    fallback in `templates` itself, the survivor set is empty and the
    fallback is resolved by lookup instead of crashing."""
    package = _streak_package()  # angle is Angle.STREAK
    wrong_angle_only = _template(
        "wrong_angle", kinds=("streak",), voices=(), angles=("collapse",),
    )
    fallback = _template("fallback_streak", kinds=("streak",), voices=())
    chosen = select_media_template(
        package, (wrong_angle_only,), voice="beat_writer", tier=_TIER,
        fallback_by_kind={StoryKind.STREAK: fallback}, use_counts={},
        repeat_penalty=0.25, rng=random.Random(1),
    )
    assert chosen.id == "fallback_streak"


# ---------------------------------------------------------------------------
# bind_slots -- retyped `(template, frame, *, team_name, player_name)` ->
# `(template, package)`. The old degrade-to-placeholder tests are GONE with
# the resolvers they pinned (`_SLOT_RESOLVERS`/`SLOT_NAMES`, MV-1b(c)) --
# an absent name now makes a template INELIGIBLE (covered above), never a
# placeholder string.
# ---------------------------------------------------------------------------


def test_bind_slots_resolves_real_values_from_the_package():
    package = _streak_package(streak_length=9)
    template = _template("t", slots=("team_name", "streak_length"))
    bindings = bind_slots(template, package)
    assert bindings == {"team_name": "Portland Trail Blazers", "streak_length": "9"}


def test_bind_slots_renders_the_absolute_value_for_a_signed_streak():
    package = _streak_package(streak_length=-7)
    template = _template("t", slots=("streak_length",))
    assert bind_slots(template, package) == {"streak_length": "7"}


def test_bind_slots_raises_on_a_slot_the_package_does_not_carry():
    """The `bind_slots`-level totality guard `template_eligible`'s own
    slot-presence clause is meant to make unreachable in production --
    exercised directly here, matching this codebase's "should never
    happen, defensive" test convention for the identical failure class."""
    package = _streak_package()  # no player_name fact on a TEAM-subject package
    template = _template("t", slots=("player_name",))
    with pytest.raises(AssertionError, match="player_name"):
        bind_slots(template, package)


def test_bind_slots_raises_on_an_unknown_slot_name():
    package = _streak_package()
    template = _template("t", slots=("not_a_real_slot",))
    with pytest.raises(AssertionError, match="not_a_real_slot"):
        bind_slots(template, package)


def test_bind_slots_zero_is_a_real_zero_for_trade_asset_counts():
    """Unlike the rate/position/delta fields, the trade-asset counts are
    never absent on a `TRADE_REACTION` package (`dba2/media/detect/
    legacy.py`'s own unconditional-emit rule) -- `0` here is a real,
    meaningful "no assets of this kind," not a placeholder."""
    package = _package(
        StoryCandidate(kind=StoryKind.TRADE_REACTION, team_id=1, trade_in_player_ids=(1, 2))
    )
    template = _template("t", slots=("trade_in_players", "trade_out_players", "trade_in_picks"))
    bindings = bind_slots(template, package)
    assert bindings == {
        "trade_in_players": "2", "trade_out_players": "0", "trade_in_picks": "0",
    }


# ---------------------------------------------------------------------------
# Totality: the whole select-persona-then-select-template-then-bind-slots
# pipeline must never raise on any real, sufficient package -- swept
# across every StoryKind with a small, deliberately minimal library.
# ---------------------------------------------------------------------------


# **Scoped to the original eleven kinds -- not `StoryKind`/`list(StoryKind)`
# directly.** `_sufficient_candidate_for_kind` below builds its package
# through the real `derive_frame`/`legacy.adapt_frame` path (this file's own
# module docstring), which by design only exists for those eleven -- MV-2's
# eleven new kinds never reach `StoryFrame` at all (`StoryKind`'s own
# docstring) and each needs its own detector's real, direct `StoryPackage`
# construction, not this generic sweep. Widened to the FULL 22 once every
# new detector exists (docs/design/media-v2-newsroom.md, "Build order" step
# 4's own closing pass) -- see `tests/unit/test_media_frame.py`'s identical
# constant for the full reasoning.
_ORIGINAL_ELEVEN_STORY_KINDS = frozenset(
    {
        StoryKind.STANDINGS_SURGE, StoryKind.STANDINGS_COLLAPSE, StoryKind.STREAK,
        StoryKind.TRADE_REACTION, StoryKind.TRADE_FALLOUT, StoryKind.SIGNING_REACTION,
        StoryKind.MILESTONE, StoryKind.AWARD_RACE, StoryKind.PLAYER_UNREST,
        StoryKind.HOLDOUT_WATCH, StoryKind.ROOKIE_WATCH,
    }
)


def _library_for_the_original_eleven_kinds() -> tuple[MediaTemplate, ...]:
    return tuple(
        _template(f"fallback_{kind.value}", kinds=(kind.value,), voices=())
        for kind in _ORIGINAL_ELEVEN_STORY_KINDS
    )


def _personas_covering_the_original_eleven_kinds() -> tuple[MediaPersona, ...]:
    return tuple(
        _persona(f"persona_{kind.value}", **{kind.value: 1.0})
        for kind in _ORIGINAL_ELEVEN_STORY_KINDS
    )


def _sufficient_candidate_for_kind(kind: StoryKind) -> StoryCandidate:
    """One real, SUFFICIENT `StoryCandidate` per `StoryKind` -- factored out
    (Round 12 critic fix, nit 3) so both totality sweeps below (the
    synthetic-library one and the real-shipped-library one) build the
    identical per-kind candidate shape rather than drifting apart."""
    if kind in (StoryKind.STANDINGS_SURGE, StoryKind.STANDINGS_COLLAPSE):
        return StoryCandidate(
            kind=kind, team_id=1, team_games_played_this_season=20, has_pulse_snapshot=True,
            standings_position=3, standings_position_at_last_pulse=8,
        )
    if kind is StoryKind.STREAK:
        return StoryCandidate(
            kind=kind, team_id=1, team_games_played_this_season=20, has_pulse_snapshot=True,
            streak_length=6,
        )
    if kind in (StoryKind.TRADE_REACTION, StoryKind.TRADE_FALLOUT):
        return StoryCandidate(kind=kind, team_id=1, trade_in_player_ids=(1,))
    if kind is StoryKind.SIGNING_REACTION:
        return StoryCandidate(kind=kind, team_id=1, player_id=1)
    if kind in (StoryKind.MILESTONE, StoryKind.AWARD_RACE, StoryKind.ROOKIE_WATCH):
        return StoryCandidate(kind=kind, player_id=1, player_games_played_this_season=10)
    if kind is StoryKind.HOLDOUT_WATCH:
        return StoryCandidate(
            kind=kind, player_id=1, motivation_magnitude=10, request_state=RequestState.HOLDOUT,
        )
    return StoryCandidate(  # PLAYER_UNREST
        kind=kind, player_id=1, motivation_magnitude=10, request_state=RequestState.UNSETTLED,
    )


@pytest.mark.parametrize("kind", sorted(_ORIGINAL_ELEVEN_STORY_KINDS))
def test_totality_the_full_pipeline_never_raises_for_any_sufficient_kind(kind):
    package = _package(_sufficient_candidate_for_kind(kind))

    personas = _personas_covering_the_original_eleven_kinds()
    templates = _library_for_the_original_eleven_kinds()
    fallback_by_kind = {StoryKind(t.kinds[0]): t for t in templates}

    persona = select_persona(package.story_kind, personas, rng=random.Random(1))
    template = select_media_template(
        package, templates, voice=persona.key, tier=_TIER, fallback_by_kind=fallback_by_kind,
        use_counts={}, repeat_penalty=0.25, rng=random.Random(2),
    )
    bindings = bind_slots(template, package)
    rendered = template.text.format(**bindings)
    assert isinstance(rendered, str)


@pytest.mark.parametrize("kind", sorted(_ORIGINAL_ELEVEN_STORY_KINDS))
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_totality_the_real_shipped_library_never_raises_for_any_sufficient_kind(kind, seed):
    """Round 12 critic fix (nit 3), carried into the retype: sweeps
    `dba2.services.media_service.load_media_library()`'s real personas and
    templates (the identical library `select_and_bind_story` draws from in
    production) across the original eleven `StoryKind`s (see this file's
    `_ORIGINAL_ELEVEN_STORY_KINDS` for why not all 22 yet) and a handful of
    seeds, through the full pure pipeline plus real slot-binding resolution
    against real `Fact`s."""
    library = load_media_library()
    package = _package(_sufficient_candidate_for_kind(kind))

    persona = select_persona(package.story_kind, library.personas, rng=random.Random(seed))
    template = select_media_template(
        package, library.templates, voice=persona.key, tier=_TIER,
        fallback_by_kind=library.fallback_by_kind, use_counts={},
        repeat_penalty=library.repeat_penalty, rng=random.Random(seed + 1),
    )
    bindings = bind_slots(template, package)
    rendered = template.text.format(**bindings)
    assert isinstance(rendered, str)
    assert rendered


# ---------------------------------------------------------------------------
# DRAMA_ANGLES -- MV-3h's own closed set, verbatim.
# ---------------------------------------------------------------------------


def test_drama_angles_matches_mv_3h_exactly():
    from dba2.domain.media_facts import Angle

    assert DRAMA_ANGLES == {
        Angle.PROMISE_BROKEN, Angle.UNREST, Angle.HOLDOUT, Angle.TRADE_MADE,
        Angle.REVENGE, Angle.COLLAPSE, Angle.GOING_COLD, Angle.SCHEME_TENSION,
        Angle.ROSTER_IMBALANCE, Angle.RIVALRY,
    }


def test_drama_angles_excludes_the_calm_angles():
    from dba2.domain.media_facts import Angle

    calm_angles = {
        Angle.SURGE, Angle.STREAK, Angle.CAREER_CLIMB, Angle.ROOKIE_ARRIVAL,
        Angle.HEATING_UP, Angle.LOCKDOWN, Angle.SCHEME_IDENTITY, Angle.MARKET_WATCH,
        Angle.FULL_PICTURE,
    }
    assert not (DRAMA_ANGLES & calm_angles)
