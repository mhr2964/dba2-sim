"""`tools/generate_media_corpus.py` -- BLOCKING 5 of this fix round: this
file had zero test coverage anywhere before this dispatch (`grep -rln
"generate_media_corpus" tests/` returned nothing). Targets exactly the
logic this fix round touched, per this dispatch's own scoping instruction
("don't try to cover the whole file exhaustively"):

1. **BLOCKING 1** -- `_ArmAccumulator.record_stories`'s corrected
   `k_dedicated` reconstruction resolves the SPECIFIC dedicated template
   whose triggers hold for a candidate's own frame, never a sum across
   every dedicated template sharing a (persona, kind) pair. Mirrors the
   critic's own 3-story reproduction directly: a winning-streak story, a
   losing-streak story, then a fallback story, all one voice/one cycle.
2. **BLOCKING 2** -- `_build_library_lookups`'s load-time guard requires
   `>= 1` dedicated template for every (persona, kind) pair, `ungated_
   kinds` included, and a real, checkable pairwise-disjointness check
   (`_assert_pairwise_disjoint`) fires for any pair with more than one --
   both a genuinely-disjoint pair (passes) and a constructed overlapping
   pair (fails loudly). **`ungated_kinds` no longer keeps a separate,
   stricter `== 1` rule** (docs/design/media-v2-newsroom.md MV-1c.6's own
   disclosed residual risk, closed at "Build order" step 6): an angle-split
   pair is exactly as safe as a trigger-split (sign-complementary `streak`)
   pair, proven by the same disjointness check either way.

**Retyped to `StoryPackage` (docs/design/media-v2-newsroom.md MV-1c,
"Build order" step 3a).** `StoryPackage` fixtures are built through the
real production path -- `derive_frame` -> `dba2/media/detect/legacy.py::
adapt_frame` -> `dba2.services.media_service.seal_package` -- the retyped
analogue of this file's prior "shortest real path to a `StoryFrame`"
convention. `seal_package` itself is pure Python (no I/O; it takes already-
resolved name MAPS as plain dicts), so this file needs no DB connection to
build one, exactly as before."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from dba2.domain.media import MediaStory, StoryKind
from dba2.domain.media_facts import StoryPackage
from dba2.domain.phase import Phase
from dba2.media.detect import legacy
from dba2.media.frame import StoryCandidate, derive_frame
from dba2.media.salience import SalienceCandidate, ScoredCandidate
from dba2.media.select import MediaPersona, MediaTemplate, MediaTrigger
from dba2.services import media_service
from tools.generate_media_corpus import (
    _FLOAT_FEATURES,
    _ArmAccumulator,
    _assert_pairwise_disjoint,
    _build_library_lookups,
    _feature_interval,
)

_SEASON = 2026
_PHASE = Phase.REGULAR_SEASON
_CYCLE_INDEX = 0


def _package(candidate: StoryCandidate) -> StoryPackage:
    """The shortest real path from a hand-built `StoryCandidate` to a
    sealed `StoryPackage` -- see `tests/unit/test_media_salience.py`'s own
    identical helper for the full reasoning."""
    frame = derive_frame(candidate)
    draft = legacy.adapt_frame(frame, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX)
    assert draft is not None, f"candidate for {candidate.kind} was insufficient -- fix the fixture"
    team_names = {candidate.team_id: "A Team"} if candidate.team_id is not None else {}
    player_names = {candidate.player_id: "A Player"} if candidate.player_id is not None else {}
    sealed = media_service.seal_package(draft, team_names=team_names, player_names=player_names)
    assert sealed is not None, f"candidate for {candidate.kind} failed to seal -- fix the fixture"
    return sealed


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _template(
    template_id: str,
    *,
    kinds: tuple[str, ...] = (),
    voices: tuple[str, ...] = (),
    triggers: tuple[MediaTrigger, ...] = (),
    weight: float = 1.0,
    angles: tuple[str, ...] = (),
    tiers: tuple[str, ...] = (),
) -> MediaTemplate:
    return MediaTemplate(
        id=template_id, kinds=kinds, voices=voices, triggers=triggers,
        weight=weight, text="line", slots=(), angles=angles, tiers=tiers,
    )


def _persona(key: str, **weights: float) -> MediaPersona:
    return MediaPersona(key=key, story_kind_weights=dict(weights))


def _streak_package(streak_length: int) -> StoryPackage:
    return _package(
        StoryCandidate(
            kind=StoryKind.STREAK, team_id=1, team_games_played_this_season=20,
            has_pulse_snapshot=True, streak_length=streak_length,
        )
    )


def _scored(streak_length: int) -> ScoredCandidate:
    candidate = SalienceCandidate(package=_streak_package(streak_length))
    return ScoredCandidate(candidate=candidate, score=1.0)


def _story(
    *, story_id: int, voice: str, template_id: str, story_kind: StoryKind = StoryKind.STREAK
) -> MediaStory:
    return MediaStory(
        id=story_id, league_id=1, season=2026, phase=Phase.REGULAR_SEASON, cycle_index=0,
        story_kind=story_kind, voice=voice, template_id=template_id, slot_bindings={},
        rendered_text="x", salience=1.0, subject_player_id=None, subject_team_id=1,
        writer_id=None, created_at=datetime.now(UTC),
    )


def _standings_surge_package() -> StoryPackage:
    return _package(
        StoryCandidate(
            kind=StoryKind.STANDINGS_SURGE, team_id=1, team_games_played_this_season=20,
            has_pulse_snapshot=True, standings_position=1, standings_position_at_last_pulse=4,
        )
    )


def _scored_standings_surge() -> ScoredCandidate:
    candidate = SalienceCandidate(package=_standings_surge_package())
    return ScoredCandidate(candidate=candidate, score=1.0)


def _publications(
    stories: tuple[MediaStory, ...], candidates: tuple[ScoredCandidate, ...]
) -> tuple[media_service.Publication, ...]:
    """docs/design/media-v2-newsroom.md MV-1c.5, "Build order" step 3b:
    `_ArmAccumulator.record_stories` now takes `Publication` tuples, never
    a separate `(stories, candidates)` pair to zip -- this file's own
    fixtures still build `stories`/`candidates` as two parallel tuples
    (unchanged, real production data), so this helper is the one place
    that pairs them into the carried shape the real service now returns."""
    return tuple(
        media_service.Publication(candidate_sequence=i, scored=candidate, story=story)
        for i, (story, candidate) in enumerate(zip(stories, candidates, strict=True))
    )


def _sign_split_streak_library() -> media_service.LoadedMediaLibrary:
    """A small-but-complete library (one fallback + one dedicated template
    per `StoryKind`), except `streak`, which gets `analyst`'s own two
    sign-complementary dedicated templates -- the identical shape Decision
    P4-96's real authoring remedy produced in the shipped library, minimal
    enough to hand-verify by inspection."""
    templates: list[MediaTemplate] = [
        _template(f"fallback_{kind.value}", kinds=(kind.value,), voices=(), weight=1.0)
        for kind in StoryKind
    ]
    templates += [
        _template(
            f"dedicated_{kind.value}", kinds=(kind.value,), voices=(f"persona_{kind.value}",),
            weight=2.0,
        )
        for kind in StoryKind
        if kind is not StoryKind.STREAK
    ]
    templates += [
        _template(
            "analyst_streak_surge", kinds=("streak",), voices=("analyst",), weight=2.0,
            triggers=(MediaTrigger(feature="streak_length", op="gte", value=5),),
        ),
        _template(
            "analyst_streak_slide", kinds=("streak",), voices=("analyst",), weight=2.0,
            triggers=(MediaTrigger(feature="streak_length", op="lte", value=-5),),
        ),
    ]
    personas = [
        _persona(f"persona_{kind.value}", **{kind.value: 1.0})
        for kind in StoryKind
        if kind is not StoryKind.STREAK
    ]
    personas.append(_persona("analyst", streak=1.0))
    templates_tuple = tuple(templates)
    return media_service.LoadedMediaLibrary(
        templates=templates_tuple, personas=tuple(personas),
        repeat_penalty=0.3, persona_exclusivity_floor=0.5,
        fallback_by_kind=media_service._fallback_templates_by_kind(templates_tuple),
    )


# ---------------------------------------------------------------------------
# BLOCKING 1 -- k_dedicated is the SPECIFIC dedicated template's own running
# count, never a sum across the pair.
# ---------------------------------------------------------------------------


def test_k_dedicated_reconstructs_the_true_sufficient_statistic_not_a_sum():
    """The critic's own real reproduction, replayed by hand: a winning-
    streak story, then a losing-streak story, then a forced-fallback
    winning-streak story, all `analyst`-voiced, one cycle. The buggy
    summed reconstruction would read `d=0, 1, 2` (the losing-streak draw's
    running count polluted by the winning-streak template's own prior use,
    and the third draw inflated further); the true, corrected depths are
    `d=0, 0, 1` -- the second draw's own dedicated template
    (`analyst_streak_slide`) has never fired yet, and the third draw's own
    dedicated template (`analyst_streak_surge`) has fired exactly once
    before it, regardless of what the sign-complementary template did."""
    lookups = _build_library_lookups(_sign_split_streak_library())
    acc = _ArmAccumulator()

    stories = (
        _story(story_id=1, voice="analyst", template_id="analyst_streak_surge"),
        _story(story_id=2, voice="analyst", template_id="analyst_streak_slide"),
        _story(story_id=3, voice="analyst", template_id="fallback_streak"),
    )
    candidates = (_scored(9), _scored(-9), _scored(9))

    acc.record_stories(_publications(stories, candidates), lookups=lookups)

    arm = acc.to_arm()
    assert arm.by_pair_depth["0"].selections == 2
    assert arm.by_pair_depth["0"].fallback_selections == 0
    assert arm.by_pair_depth["1"].selections == 1
    assert arm.by_pair_depth["1"].fallback_selections == 1
    assert "2" not in arm.by_pair_depth  # the pre-fix bug's own inflated depth


def test_k_dedicated_is_zero_when_no_candidate_of_this_sign_has_fired_yet_even_mid_cycle():
    """A losing-streak draw that comes FIRST in the cycle (no winning-
    streak draw ahead of it) must also read `d=0` -- confirms the fix does
    not merely fix the ORDER from the reproduction above, but genuinely
    keys off the candidate's own sign."""
    lookups = _build_library_lookups(_sign_split_streak_library())
    acc = _ArmAccumulator()

    stories = (_story(story_id=1, voice="analyst", template_id="analyst_streak_slide"),)
    candidates = (_scored(-7),)

    acc.record_stories(_publications(stories, candidates), lookups=lookups)

    arm = acc.to_arm()
    assert arm.by_pair_depth["0"].selections == 1


# ---------------------------------------------------------------------------
# Decision P4-107 item 6 (P4-111) -- `trigger_held_selections`/`trigger_
# held_fallback_selections`, additive alongside `ungated_kind_selections`/
# `ungated_kind_fallback_selections`, and always a superset of them in the
# same bin.
# ---------------------------------------------------------------------------


def test_trigger_held_selections_generalizes_ungated_kind_selections_to_every_kind():
    """Three selections, all landing in `by_pair_depth["0"]` (the global
    `d` bin is shared across every pair, by design -- `by_pair_depth`'s own
    docstring): (1) a `streak` (GATED kind) draw whose own dedicated
    template's trigger HOLDS -- `trigger_held_selections` must count it,
    `ungated_kind_selections` must NOT, since `streak` is not in
    `ungated_kinds` in this library; (2) a `streak` draw whose trigger
    MISSES, forced to the fallback -- neither field counts it, and it IS a
    real fallback; (3) a `standings_surge` (UNGATED kind) draw -- both
    fields count it, since an ungated kind's trigger holds vacuously.
    `trigger_held_selections` (2) is a strict superset of `ungated_kind_
    selections` (1) in this one bin, exactly as `PairDepthBin`'s own
    docstring claims."""
    lookups = _build_library_lookups(_sign_split_streak_library())
    acc = _ArmAccumulator()

    stories = (
        # (1) trigger HELD -- streak_length=9 clears analyst_streak_surge's
        # own `gte 5` trigger.
        _story(story_id=1, voice="analyst", template_id="analyst_streak_surge"),
        # (2) trigger MISSED -- streak_length=2 clears neither
        # analyst_streak_surge's `gte 5` nor analyst_streak_slide's
        # `lte -5`, so the only real survivor is the fallback.
        _story(story_id=2, voice="analyst", template_id="fallback_streak"),
        # (3) an ungated kind's own dedicated draw -- no triggers to miss.
        _story(
            story_id=3, voice="persona_standings_surge",
            template_id="dedicated_standings_surge", story_kind=StoryKind.STANDINGS_SURGE,
        ),
    )
    candidates = (_scored(9), _scored(2), _scored_standings_surge())

    acc.record_stories(_publications(stories, candidates), lookups=lookups)

    bin_0 = acc.to_arm().by_pair_depth["0"]
    assert bin_0.selections == 3
    assert bin_0.fallback_selections == 1  # story 2 only
    assert bin_0.ungated_kind_selections == 1  # story 3 only
    assert bin_0.ungated_kind_fallback_selections == 0
    assert bin_0.trigger_held_selections == 2  # stories 1 and 3
    assert bin_0.trigger_held_fallback_selections == 0
    # The subset relationship `PairDepthBin`'s own docstring claims, holding
    # on real data rather than merely asserted in prose.
    assert bin_0.ungated_kind_selections <= bin_0.trigger_held_selections


# ---------------------------------------------------------------------------
# BLOCKING 2 -- the load-time guard's ungated/gated split, and the real
# pairwise-disjointness check.
# ---------------------------------------------------------------------------


def test_ungated_kind_guard_still_rejects_an_unprovable_second_template():
    """docs/design/media-v2-newsroom.md MV-1c.6, "Build order" step 6: the
    old hard `== 1` rule for `ungated_kinds` is gone (see the passing
    counterpart below), but a second dedicated template that is NOT
    provably disjoint from the first (no triggers, no angles, no tiers --
    genuinely overlapping) still raises, now via `_assert_pairwise_
    disjoint`'s own message rather than the old ungated-specific one."""
    library = _sign_split_streak_library()
    extra = _template(
        "dedicated_standings_surge_2",
        kinds=(StoryKind.STANDINGS_SURGE.value,),
        voices=(f"persona_{StoryKind.STANDINGS_SURGE.value}",),
        weight=2.0,
    )
    new_templates = (*library.templates, extra)
    library = media_service.LoadedMediaLibrary(
        templates=new_templates, personas=library.personas,
        repeat_penalty=library.repeat_penalty,
        persona_exclusivity_floor=library.persona_exclusivity_floor,
        fallback_by_kind=media_service._fallback_templates_by_kind(new_templates),
    )
    with pytest.raises(ValueError, match="could not be PROVEN disjoint"):
        _build_library_lookups(library)


def test_ungated_kind_guard_allows_an_angle_disjoint_second_template():
    """MV-1c.6's own residual risk, closed: an `ungated` kind (no triggers)
    whose two dedicated templates ARE angle-disjoint loads successfully --
    the real, motivating shape (`hot_take`/`trade_broken_promise`'s own
    `PROMISE_BROKEN`/`TRADE_MADE` split, `seeds/media_phrases.json`),
    reproduced minimally here rather than only exercised through the real
    shipped library."""
    library = _sign_split_streak_library()
    kind = StoryKind.STANDINGS_SURGE
    persona_key = f"persona_{kind.value}"
    original = next(t for t in library.templates if t.id == f"dedicated_{kind.value}")
    split_a = media_service.select.MediaTemplate(
        id="dedicated_standings_surge_surge", kinds=original.kinds, voices=original.voices,
        triggers=(), weight=2.0, text="line", slots=(), angles=("surge",), tiers=(),
    )
    split_b = media_service.select.MediaTemplate(
        id="dedicated_standings_surge_collapse", kinds=original.kinds, voices=original.voices,
        triggers=(), weight=2.0, text="line", slots=(), angles=("collapse",), tiers=(),
    )
    new_templates = tuple(t for t in library.templates if t.id != original.id) + (split_a, split_b)
    library = media_service.LoadedMediaLibrary(
        templates=new_templates, personas=library.personas,
        repeat_penalty=library.repeat_penalty,
        persona_exclusivity_floor=library.persona_exclusivity_floor,
        fallback_by_kind=media_service._fallback_templates_by_kind(new_templates),
    )
    lookups = _build_library_lookups(library)
    assert lookups.dedicated_template_ids_by_pair[(persona_key, kind.value)] == (
        "dedicated_standings_surge_collapse", "dedicated_standings_surge_surge",
    )
    assert kind.value in lookups.ungated_kinds


def test_gated_kind_guard_relaxes_to_at_least_one():
    """A GATED kind (the sign-split `streak` pair) is allowed more than one
    dedicated template per (persona, kind) -- the load-time guard must not
    raise, and both ids are recorded."""
    lookups = _build_library_lookups(_sign_split_streak_library())
    assert lookups.dedicated_template_ids_by_pair[("analyst", "streak")] == (
        "analyst_streak_slide", "analyst_streak_surge",
    )
    assert "streak" not in lookups.ungated_kinds


def test_assert_pairwise_disjoint_passes_for_genuinely_disjoint_triggers():
    library = _sign_split_streak_library()
    templates = [t for t in library.templates if t.voices == ("analyst",)]
    assert len(templates) == 2
    _assert_pairwise_disjoint("analyst", "streak", templates)  # must not raise


def test_assert_pairwise_disjoint_fails_loudly_for_an_overlapping_pair():
    """A constructed pair whose ranges genuinely overlap (`gte 3` and
    `gte -3` both hold for `streak_length=5`) must raise, never silently
    pass -- the exact failure mode this check exists to prevent."""
    overlapping = [
        _template(
            "t_a", kinds=("streak",), voices=("analyst",), weight=2.0,
            triggers=(MediaTrigger(feature="streak_length", op="gte", value=3),),
        ),
        _template(
            "t_b", kinds=("streak",), voices=("analyst",), weight=2.0,
            triggers=(MediaTrigger(feature="streak_length", op="gte", value=-3),),
        ),
    ]
    with pytest.raises(ValueError, match="could not be PROVEN disjoint"):
        _assert_pairwise_disjoint("analyst", "streak", overlapping)


def test_assert_pairwise_disjoint_fails_loudly_when_it_cannot_reason_about_the_trigger_shape():
    """Two dedicated templates sharing no interval-reasoning feature at all
    (no shared feature) must fail loudly too -- an unverifiable pair is
    never treated as disjoint by default."""
    unverifiable = [
        _template(
            "t_a", kinds=("streak",), voices=("analyst",), weight=2.0,
            triggers=(MediaTrigger(feature="streak_length", op="gte", value=5),),
        ),
        _template(
            "t_b", kinds=("streak",), voices=("analyst",), weight=2.0,
            triggers=(MediaTrigger(feature="standings_position", op="lte", value=3),),
        ),
    ]
    with pytest.raises(ValueError, match="could not be PROVEN disjoint"):
        _assert_pairwise_disjoint("analyst", "streak", unverifiable)


def test_integer_interval_raises_loudly_for_an_unsupported_operator():
    """`in` is outside the narrow interval-reasoning this check performs --
    must raise rather than silently returning an interval it cannot back
    up."""
    template = _template(
        "t", kinds=("streak",), voices=("analyst",), weight=2.0,
        triggers=(MediaTrigger(feature="streak_length", op="in", value=(1, 2, 3)),),
    )
    with pytest.raises(ValueError, match="load-time.*disjointness check only reasons"):
        _feature_interval(template, "streak_length")


# ---------------------------------------------------------------------------
# BLOCKING 2 (round-23 critic fix) -- feature-type-aware interval widening,
# the wrong-exception-type fix, the "try every shared feature" fix, and the
# real runtime backstop (`record_stories`'s `held_ids` assertion).
# ---------------------------------------------------------------------------


def test_float_features_matches_the_real_registrys_declared_float_keys():
    """MV-1c.5: `_FLOAT_FEATURES` is derived from `FACT_KEYS_BY_KIND`, not
    `StoryFrame`'s own field annotations. **Was `== frozenset()` through
    build step 3a** -- the original eleven `StoryKind` members declare no
    `FLOAT` key at all (MV-8's "full shipped vocabulary" table) -- but
    MV-2's new detectors (docs/design/media-v2-newsroom.md, "Build order"
    step 4: `HOT_STREAK`/`COLD_STRETCH`/`DEFENSIVE_STANDOUT`'s per-36/
    true-shooting/attribution facts) are real `FLOAT` keys, so this is no
    longer empty -- updated here rather than left to silently drift, and
    `test_float_features_derivation_contains_every_float_key_and_no_int_
    key` below is what actually exercises the DERIVATION logic (against a
    hand-built registry, so it never passes vacuously)."""
    assert _FLOAT_FEATURES == frozenset(
        {
            "window_points_per36", "baseline_points_per36",
            "window_true_shooting", "baseline_true_shooting",
            "window_stocks_per36", "baseline_stocks_per36",
            "attribution_three_point_volume", "attribution_rim_pressure",
            "attribution_free_throws", "attribution_playmaking",
            "attribution_on_ball_defense", "attribution_rim_protection",
            "attribution_defensive_rebounding",
            "biggest_need_value", "biggest_glut_value",
            "stark_need_value", "stark_need_supply", "stark_glut_value",
            "aging_core_mean_age",
            "avg_possessions", "three_share",
            "team_glut_value", "league_biggest_need_value",
            "mean_age_top8",
        }
    )


def test_float_features_derivation_contains_every_float_key_and_no_int_key():
    """MV-1c.5's own required test: asserted against a hand-built two-key
    registry (never the real one, which has zero `FLOAT` keys today) so
    this cannot pass vacuously. Exercises the SAME derivation expression
    `tools/generate_media_corpus.py::_FLOAT_FEATURES` is built from,
    applied to a registry a test controls."""
    from dba2.domain.media import StoryKind as _StoryKind
    from dba2.domain.media_facts import FactType as _FactType

    hand_built_registry = {
        _StoryKind.MILESTONE: {"a_float_key": _FactType.FLOAT, "an_int_key": _FactType.INT},
    }
    derived = frozenset(
        key
        for kind_map in hand_built_registry.values()
        for key, fact_type in kind_map.items()
        if fact_type is _FactType.FLOAT
    )
    assert derived == frozenset({"a_float_key"})
    assert "an_int_key" not in derived


def test_float_feature_gt_lt_does_not_get_unsound_integer_widening(monkeypatch):
    """The critic's own real reproduction: `award_share_estimate gt 0.5` and
    `award_share_estimate lt 0.9` genuinely OVERLAP (both hold at `0.7`) --
    the OLD integer `+-1` widening produced nonsensical intervals `(1.5,
    inf)` and `(-inf, -0.1)` that falsely certified them disjoint (`-0.1 <
    1.5`). The current, float-aware logic must correctly refuse to certify
    this pair as disjoint. **`award_share_estimate` is not a real key under
    the current registry** (no `StoryKind` declares a `FLOAT` key at all,
    see the two tests above) -- `_FLOAT_FEATURES` is monkeypatched here so
    this test keeps exercising `_feature_interval`'s FLOAT-path arithmetic
    directly, independent of which real kind eventually gets a float key."""
    monkeypatch.setattr(
        "tools.generate_media_corpus._FLOAT_FEATURES", frozenset({"award_share_estimate"})
    )
    t_gt = _template(
        "t_gt", kinds=("award_race",), voices=("analyst",), weight=2.0,
        triggers=(MediaTrigger(feature="award_share_estimate", op="gt", value=0.5),),
    )
    t_lt = _template(
        "t_lt", kinds=("award_race",), voices=("analyst",), weight=2.0,
        triggers=(MediaTrigger(feature="award_share_estimate", op="lt", value=0.9),),
    )
    with pytest.raises(ValueError, match="could not be PROVEN disjoint"):
        _assert_pairwise_disjoint("analyst", "award_race", [t_gt, t_lt])


def test_float_feature_interval_reads_a_genuine_open_bound_not_a_widened_one(monkeypatch):
    """Direct check of `_feature_interval`'s own float-path output (not just
    the pairwise check's pass/fail) -- `gt 0.5` on a float feature must
    yield an OPEN lower bound at exactly `0.5`, never the integer path's
    `value + 1` widening."""
    monkeypatch.setattr(
        "tools.generate_media_corpus._FLOAT_FEATURES", frozenset({"award_share_estimate"})
    )
    template = _template(
        "t", kinds=("award_race",), voices=("analyst",), weight=2.0,
        triggers=(MediaTrigger(feature="award_share_estimate", op="gt", value=0.5),),
    )
    interval = _feature_interval(template, "award_share_estimate")
    assert interval.lo == 0.5
    assert interval.lo_inclusive is False
    assert interval.hi == float("inf")


def test_float_feature_genuinely_disjoint_ranges_still_certify_correctly(monkeypatch):
    """The float path must still PROVE real disjointness when it holds --
    `lte 0.3` and `gt 0.5` never overlap for any real float, and the fix
    must not have overcorrected into refusing every float pair."""
    monkeypatch.setattr(
        "tools.generate_media_corpus._FLOAT_FEATURES", frozenset({"award_share_estimate"})
    )
    t_low = _template(
        "t_low", kinds=("award_race",), voices=("analyst",), weight=2.0,
        triggers=(MediaTrigger(feature="award_share_estimate", op="lte", value=0.3),),
    )
    t_high = _template(
        "t_high", kinds=("award_race",), voices=("analyst",), weight=2.0,
        triggers=(MediaTrigger(feature="award_share_estimate", op="gt", value=0.5),),
    )
    _assert_pairwise_disjoint("analyst", "award_race", [t_low, t_high])  # must not raise


def test_eq_trigger_on_a_non_numeric_value_raises_value_error_not_type_error():
    """The critic's own real shipped-library shape (`request_state eq
    "trade_requested"`) -- a non-numeric `eq` value must raise the SAME
    documented `ValueError`, never an uncaught `TypeError`."""
    template = _template(
        "t", kinds=("player_unrest",), voices=("insider",), weight=2.0,
        triggers=(MediaTrigger(feature="request_state", op="eq", value="trade_requested"),),
    )
    with pytest.raises(ValueError, match="non-numeric"):
        _feature_interval(template, "request_state")


def test_assert_pairwise_disjoint_tries_every_shared_feature_not_just_the_first():
    """A pair sharing TWO features -- the alphabetically-FIRST
    (`award_share_estimate`) cannot be reasoned about (a non-numeric `eq`
    value), but the SECOND (`streak_length`) genuinely proves disjointness.
    The prior version raised on the first, alphabetically-sorted feature it
    could not reason about, never reaching the second; the fix must try
    both and succeed via the one that can prove it."""
    t_a = _template(
        "t_a", kinds=("streak",), voices=("analyst",), weight=2.0,
        triggers=(
            MediaTrigger(feature="award_share_estimate", op="eq", value="not_a_number"),
            MediaTrigger(feature="streak_length", op="gte", value=5),
        ),
    )
    t_b = _template(
        "t_b", kinds=("streak",), voices=("analyst",), weight=2.0,
        triggers=(
            MediaTrigger(feature="award_share_estimate", op="eq", value="not_a_number"),
            MediaTrigger(feature="streak_length", op="lte", value=-5),
        ),
    )
    _assert_pairwise_disjoint("analyst", "streak", [t_a, t_b])  # must not raise


def test_held_ids_assertion_is_a_real_defense_in_depth_backstop():
    """`_ArmAccumulator.record_stories`'s `len(held_ids) > 1` assertion --
    the REAL runtime backstop that catches a bad case even if the
    disjointness check itself failed to reject it -- has its own direct
    test, bypassing the load-time check entirely (a hand-built `_LibraryLookups`
    naming two dedicated templates that BOTH genuinely hold for one
    candidate's own frame, never going through `_build_library_lookups`,
    which would have rejected this pair itself)."""
    from tools.generate_media_corpus import _LibraryLookups

    dedicated_a = _template(
        "dedicated_a", kinds=("streak",), voices=("analyst",), weight=2.0,
        triggers=(MediaTrigger(feature="streak_length", op="gte", value=-5),),
    )
    dedicated_b = _template(
        "dedicated_b", kinds=("streak",), voices=("analyst",), weight=2.0,
        triggers=(MediaTrigger(feature="streak_length", op="lte", value=5),),
    )
    fallback = _template("fallback_streak", kinds=("streak",), voices=(), weight=1.0)
    lookups = _LibraryLookups(
        is_fallback_template={"dedicated_a": False, "dedicated_b": False, "fallback_streak": True},
        fallback_template_id_by_kind={"streak": "fallback_streak"},
        dedicated_template_ids_by_pair={("analyst", "streak"): ("dedicated_a", "dedicated_b")},
        template_by_id={
            "dedicated_a": dedicated_a, "dedicated_b": dedicated_b, "fallback_streak": fallback,
        },
        ungated_kinds=(),
        declared_fallback_weight_share=1 / 3,
    )
    acc = _ArmAccumulator()
    # streak_length=0 holds for BOTH dedicated_a (gte -5) and dedicated_b
    # (lte 5) -- exactly the "more than one dedicated template's triggers
    # held" case the assertion exists to catch.
    stories = (_story(story_id=1, voice="analyst", template_id="dedicated_a"),)
    candidates = (_scored(0),)
    with pytest.raises(AssertionError, match="more than one dedicated template's triggers held"):
        acc.record_stories(_publications(stories, candidates), lookups=lookups)


# ---------------------------------------------------------------------------
# MV-1c.6 (docs/design/media-v2-newsroom.md, "the one revision 5 found") --
# angle-split templates share no trigger feature at all, so trigger-only
# disjointness reasoning cannot certify them and `held_ids`/`eligible_ids`
# resolved by `triggers_hold` alone would double-count. Both closed by
# routing through `select.template_eligible`, the one exported predicate
# that also reasons about `angles`.
# ---------------------------------------------------------------------------


def test_assert_pairwise_disjoint_passes_for_two_templates_differing_only_by_angle():
    """No shared trigger feature at all (neither template declares a
    trigger) -- the pre-fix trigger-only reasoning would find `shared_
    features` empty, never enter the loop, and raise `ValueError` on a
    library that is perfectly correct. The angle/tier branch (checked
    FIRST, before any trigger reasoning) proves this pair disjoint on
    `angles` alone."""
    angle_split = [
        _template(
            "t_surge", kinds=("standings_surge",), voices=("analyst",), weight=2.0,
            angles=("surge",),
        ),
        _template(
            "t_collapse", kinds=("standings_surge",), voices=("analyst",), weight=2.0,
            angles=("collapse",),
        ),
    ]
    _assert_pairwise_disjoint("analyst", "standings_surge", angle_split)  # must not raise


def test_assert_pairwise_disjoint_still_raises_for_two_templates_sharing_an_angle():
    """The angle/tier branch requires BOTH sides non-wildcard with an EMPTY
    intersection -- two templates that share an angle (or where either is
    wildcard) fall through to the (here, unprovable) trigger reasoning,
    never silently certified disjoint by angle alone."""
    same_angle = [
        _template(
            "t_a", kinds=("standings_surge",), voices=("analyst",), weight=2.0,
            angles=("surge",),
        ),
        _template(
            "t_b", kinds=("standings_surge",), voices=("analyst",), weight=2.0,
            angles=("surge",),
        ),
    ]
    with pytest.raises(ValueError, match="could not be PROVEN disjoint"):
        _assert_pairwise_disjoint("analyst", "standings_surge", same_angle)


def test_record_stories_resolves_exactly_one_eligible_id_for_an_angle_split_pair():
    """The `record_stories` half of MV-1c.6: two trigger-less, angle-split
    templates both 'held' under the old `triggers_hold`-only reasoning
    (vacuously true for a template with no triggers), which would have
    raised the `len(held_ids) > 1` defense-in-depth assertion on a
    perfectly correct library. `select.template_eligible` resolves the
    package's own `angle` (via `dba2/media/detect/legacy.py`'s real
    kind-to-angle mapping) against each candidate template's declared
    `angles`, so exactly one is ever eligible for a real package."""
    from tools.generate_media_corpus import _LibraryLookups

    # `STANDINGS_SURGE` packages always carry Angle.SURGE (`legacy.py`'s own
    # `_ANGLE_BY_KIND`) -- `t_surge` is eligible, `t_collapse` never is.
    t_surge = _template(
        "t_surge", kinds=("standings_surge",), voices=("analyst",), weight=2.0,
        angles=("surge",),
    )
    t_collapse = _template(
        "t_collapse", kinds=("standings_surge",), voices=("analyst",), weight=2.0,
        angles=("collapse",),
    )
    fallback = _template("fallback_standings_surge", kinds=("standings_surge",), voices=())
    lookups = _LibraryLookups(
        is_fallback_template={
            "t_surge": False, "t_collapse": False, "fallback_standings_surge": True,
        },
        fallback_template_id_by_kind={"standings_surge": "fallback_standings_surge"},
        dedicated_template_ids_by_pair={("analyst", "standings_surge"): ("t_collapse", "t_surge")},
        template_by_id={
            "t_surge": t_surge, "t_collapse": t_collapse,
            "fallback_standings_surge": fallback,
        },
        ungated_kinds=(),
        declared_fallback_weight_share=1 / 3,
    )
    acc = _ArmAccumulator()
    stories = (
        _story(
            story_id=1, voice="analyst", template_id="t_surge",
            story_kind=StoryKind.STANDINGS_SURGE,
        ),
    )
    candidates = (_scored_standings_surge(),)
    acc.record_stories(_publications(stories, candidates), lookups=lookups)  # must not raise
    assert acc.to_arm().by_pair_depth["0"].selections == 1


# ---------------------------------------------------------------------------
# MV-14.4 item 0 (docs/design/media-v2-newsroom.md) -- `stories_by_kind_tier`,
# additive alongside `stories_by_kind`: the corpus recorded no tier at all
# before this, so every "today's ARTICLE count" claim was inference.
# ---------------------------------------------------------------------------


def test_stories_by_kind_tier_counts_each_kind_tier_pair_from_the_persisted_row():
    """Three stories, two kinds, two tiers -- `format_tier` is read straight off
    each `publication.story` (`MediaStory.format_tier`, already persisted by
    `select_and_bind_story`), never re-derived. Two `STREAK` stories at different
    tiers must land in two separate tier buckets under the same kind, and a second
    kind (`STANDINGS_SURGE`) must not pollute `STREAK`'s own counts."""
    lookups = _build_library_lookups(_sign_split_streak_library())
    acc = _ArmAccumulator()

    blurb_streak = _story(story_id=1, voice="analyst", template_id="analyst_streak_surge")
    article_streak = dataclasses.replace(
        _story(story_id=2, voice="analyst", template_id="analyst_streak_surge"),
        format_tier="article",
    )
    surge_story = _story(
        story_id=3, voice="persona_standings_surge", template_id="dedicated_standings_surge",
        story_kind=StoryKind.STANDINGS_SURGE,
    )
    stories = (blurb_streak, article_streak, surge_story)
    candidates = (_scored(9), _scored(9), _scored_standings_surge())

    acc.record_stories(_publications(stories, candidates), lookups=lookups)

    arm = acc.to_arm()
    assert arm.by_kind_tier["streak"] == {"blurb": 1, "article": 1}
    assert arm.by_kind_tier["standings_surge"] == {"blurb": 1}


def test_stories_by_kind_tier_round_trips_through_to_json_from_json():
    from tools.gm_harness.media_corpus import MediaCorpusArm

    lookups = _build_library_lookups(_sign_split_streak_library())
    acc = _ArmAccumulator()
    acc.record_stories(
        _publications(
            (_story(story_id=1, voice="analyst", template_id="analyst_streak_surge"),),
            (_scored(9),),
        ),
        lookups=lookups,
    )
    arm = acc.to_arm()

    round_tripped = MediaCorpusArm.from_json(arm.to_json())
    assert round_tripped.by_kind_tier == arm.by_kind_tier


def test_stories_by_kind_tier_from_json_defaults_to_empty_for_a_pre_mv11_artifact():
    """A committed corpus written before this field existed has no `by_kind_tier`
    key at all -- `from_json` must default to `{}`, never `KeyError`, matching
    every other MV-14.4-era additive field's own backward-compatibility contract."""
    from tools.gm_harness.media_corpus import MediaCorpusArm

    lookups = _build_library_lookups(_sign_split_streak_library())
    acc = _ArmAccumulator()
    acc.record_stories(
        _publications(
            (_story(story_id=1, voice="analyst", template_id="analyst_streak_surge"),),
            (_scored(9),),
        ),
        lookups=lookups,
    )
    payload = acc.to_arm().to_json()
    del payload["by_kind_tier"]

    round_tripped = MediaCorpusArm.from_json(payload)
    assert round_tripped.by_kind_tier == {}
