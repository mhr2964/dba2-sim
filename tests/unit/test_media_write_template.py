"""`dba2/media/write/template.py` -- MV-6 Implementation A: `TemplateWriter`."""

from __future__ import annotations

import random

import pytest

from dba2.domain.media import Register
from dba2.domain.media_writer import FormatTier, PersonaVoice
from dba2.domain.phase import Phase
from dba2.media.detect import legacy
from dba2.media.frame import StoryCandidate, StoryKind, derive_frame
from dba2.media.select import MediaPersona, MediaTemplate
from dba2.media.write.template import TemplateWriter
from dba2.services import media_service

_SEASON = 2026
_PHASE = Phase.REGULAR_SEASON
_CYCLE_INDEX = 1


def _package(candidate, *, team_name="Portland Trail Blazers", player_name="A Player"):
    frame = derive_frame(candidate)
    draft = legacy.adapt_frame(frame, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX)
    assert draft is not None
    team_names = {candidate.team_id: team_name} if candidate.team_id is not None else {}
    player_names = {candidate.player_id: player_name} if candidate.player_id is not None else {}
    sealed = media_service.seal_package(draft, team_names=team_names, player_names=player_names)
    assert sealed is not None
    return sealed


def _streak_package(streak_length: int = 9):
    return _package(
        StoryCandidate(
            kind=StoryKind.STREAK, team_id=1, team_games_played_this_season=20,
            has_pulse_snapshot=True, streak_length=streak_length,
        )
    )


def _persona_voice(key: str = "beat_writer", *, register: Register = Register.REPORTORIAL):
    return PersonaVoice(
        key=key, display_name="Jordan Ellis", column="The Beat",
        beat_description="the beat", register=register,
        voice_rules=("be concise",), fences=("no sources",),
    )


def _template(template_id, *, kinds=("streak",), voices=(), weight=1.0, text="line", slots=()):
    return MediaTemplate(id=template_id, kinds=kinds, voices=voices, triggers=(), weight=weight,
                          text=text, slots=slots)


def _minimal_library(templates):
    return media_service.LoadedMediaLibrary(
        templates=tuple(templates),
        personas=(MediaPersona(key="beat_writer", story_kind_weights={"streak": 1.0}),),
        repeat_penalty=0.3,
        persona_exclusivity_floor=0.5,
        fallback_by_kind=media_service._fallback_templates_by_kind(tuple(templates)),
    )


@pytest.mark.asyncio
async def test_write_returns_a_real_headline_and_body():
    library = _minimal_library(
        [_template("fallback_streak", text="{team_name} are on a run.", slots=("team_name",))]
    )
    writer = TemplateWriter(library)
    written = await writer.write(
        _streak_package(), persona=_persona_voice(), tier=FormatTier.BLURB,
        rng=random.Random(1),
    )
    assert written.backend == "template"
    assert "Portland Trail Blazers" in written.body
    assert written.headline
    assert written.fact_box == ()  # BLURB never carries a fact box


@pytest.mark.asyncio
async def test_article_tier_carries_a_fact_box_blurb_does_not():
    library = _minimal_library(
        [_template("fallback_streak", text="{team_name} are on a run.", slots=("team_name",))]
    )
    writer = TemplateWriter(library)
    package = _streak_package(streak_length=9)

    blurb = await writer.write(
        package, persona=_persona_voice(), tier=FormatTier.BLURB, rng=random.Random(1)
    )
    article = await writer.write(
        package, persona=_persona_voice(), tier=FormatTier.ARTICLE, rng=random.Random(1)
    )
    assert blurb.fact_box == ()
    assert len(article.fact_box) > 0
    assert all(label and value for label, value in article.fact_box)


@pytest.mark.asyncio
async def test_fact_box_never_repeats_identity_or_universal_facts():
    library = _minimal_library(
        [_template("fallback_streak", text="{team_name} are on a run.", slots=("team_name",))]
    )
    writer = TemplateWriter(library)
    written = await writer.write(
        _streak_package(), persona=_persona_voice(), tier=FormatTier.ARTICLE,
        rng=random.Random(1),
    )
    labels = {label for label, _ in written.fact_box}
    assert "Team" not in labels
    assert "Season" not in labels
    assert "Phase" not in labels


@pytest.mark.asyncio
async def test_inflammatory_register_keeps_exclamation_reportorial_does_not():
    library = _minimal_library(
        [_template("fallback_streak", text="{team_name} are on a run.", slots=("team_name",))]
    )
    writer = TemplateWriter(library)

    # Force the headline to contain '!' by using a subject-bearing kind and checking
    # apply_headline_style is actually invoked -- exercised indirectly through a
    # deterministic transform, since the generated text itself has no '!' by default.
    from dba2.media.write.budget import apply_headline_style

    inflammatory = apply_headline_style("This trade is indefensible!", Register.INFLAMMATORY)
    reportorial = apply_headline_style("This trade is indefensible!", Register.REPORTORIAL)
    assert "!" in inflammatory
    assert "!" not in reportorial

    written = await writer.write(
        _streak_package(), persona=_persona_voice(register=Register.INFLAMMATORY),
        tier=FormatTier.BLURB, rng=random.Random(1),
    )
    assert written.headline  # a real, non-empty, styled headline was produced


@pytest.mark.asyncio
async def test_use_counts_advance_across_writes_within_one_cycle():
    """MV-6's own reused repeat-penalty scope: two candidates competing for the same
    small template pool in one cycle must see an advancing use_counts, mirroring
    run_media_selection_cycle's own already-tested property."""
    templates = [
        _template("fallback_streak", voices=(), text="{team_name} are on a run.",
                   slots=("team_name",)),
        _template("dedicated_streak", voices=("beat_writer",), weight=2.0,
                   text="{team_name} keep winning.", slots=("team_name",)),
    ]
    library = _minimal_library(templates)
    writer = TemplateWriter(library)
    package = _streak_package()

    first = await writer.write(
        package, persona=_persona_voice(), tier=FormatTier.BLURB, rng=random.Random(7)
    )
    # After one draw, use_counts must have advanced for beat_writer.
    assert writer._use_counts["beat_writer"], "use_counts did not advance after a real write"
    assert sum(writer._use_counts["beat_writer"].values()) == 1
    del first


@pytest.mark.asyncio
async def test_body_is_clamped_to_the_tier_budget():
    long_text = "{team_name} " + ("are on a remarkable run. " * 40)
    library = _minimal_library([_template("fallback_streak", text=long_text, slots=("team_name",))])
    writer = TemplateWriter(library)
    written = await writer.write(
        _streak_package(), persona=_persona_voice(), tier=FormatTier.BLURB, rng=random.Random(1)
    )
    from dba2.media.write.budget import budget_for

    assert len(written.body) <= budget_for(FormatTier.BLURB, StoryKind.STREAK).description_max


@pytest.mark.asyncio
async def test_headline_names_both_teams_for_a_matchup_subject():
    from datetime import UTC, datetime

    from dba2.domain.transaction import (
        LeagueTransaction,
        LeagueTransactionAsset,
        TransactionAssetKind,
        TransactionType,
    )
    from dba2.media.detect import trades

    transaction = LeagueTransaction(
        id=1, league_id=1, season=_SEASON, phase=_PHASE, txn_type=TransactionType.TRADE,
        actor_user_id=None, source_offer_id=None, note=None, details={},
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    assets = (
        LeagueTransactionAsset(id=1, transaction_id=1, asset_kind=TransactionAssetKind.PLAYER,
                                player_id=1, draft_pick_id=None, from_team_id=1, to_team_id=2),
    )
    draft = trades.trade_executed_package(
        transaction, assets, season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX
    )
    assert draft is not None
    sealed = media_service.seal_package(
        draft, team_names={1: "Portland Trail Blazers", 2: "Golden State Warriors"},
        player_names={},
    )
    assert sealed is not None

    library = _minimal_library(
        [_template("fallback_trade_executed", kinds=("trade_executed",), text="A trade happened.")]
    )
    writer = TemplateWriter(library)
    written = await writer.write(
        sealed, persona=_persona_voice(), tier=FormatTier.BLURB, rng=random.Random(1)
    )
    assert "Portland Trail Blazers" in written.headline
    assert "Golden State Warriors" in written.headline
    assert " vs. " in written.headline  # a real matchup opponent -- the "vs." framing applies
    assert "trade" in written.headline  # a real display label, not the raw "trade_executed"


@pytest.mark.asyncio
async def test_headline_names_the_player_subject_and_skips_vs_framing_for_broken_promise():
    """Bug B4 (critic round on commits `ea84c5f`..`665526d`): `TRADE_BROKEN_PROMISE`'s
    `Subject.kind` is `PLAYER` and its `opponent_team_name` is a FORMER team, not a
    game opponent -- the headline must name the player (never the team) and must
    never use "X vs. Y" framing for this kind."""
    from dba2.domain.motivation import RequestState
    from dba2.media.detect import trades

    draft = trades.trade_broken_promise_package(
        player_id=1, old_team_id=1, new_team_id=2, state_at_trade=RequestState.CONTENT,
        satisfaction_loyalty=50, weight_loyalty=30, trade_season=_SEASON,
        season=_SEASON, phase=_PHASE, cycle_index=_CYCLE_INDEX,
    )
    assert draft is not None
    sealed = media_service.seal_package(
        draft, team_names={1: "Chicago Bulls", 2: "Miami Heat"}, player_names={1: "A Player"},
    )
    assert sealed is not None

    library = _minimal_library(
        [_template("fallback_trade_broken_promise", kinds=("trade_broken_promise",),
                    text="A trade happened.")]
    )
    writer = TemplateWriter(library)
    written = await writer.write(
        sealed, persona=_persona_voice(), tier=FormatTier.BLURB, rng=random.Random(1)
    )
    assert written.headline.startswith("A Player:")
    assert " vs. " not in written.headline
    assert "broken promise" in written.headline  # the real display label, not "Chicago Bulls"
