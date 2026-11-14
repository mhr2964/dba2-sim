"""`dba2/domain/media_prompt.py` -- byte-for-byte golden tests
(docs/design/media-v2-step8-anthropic-writer.md S8-15 item 1).

The prompt is a contract: these catch a dropped fence, a reordered payload
(which silently destroys prompt-cache hits), a missing budget number, and the
tangent clause appearing when there is no brief. Goldens live in
`tests/fixtures/media_prompt/`; a deliberate prompt change means regenerating
them and reading the diff -- that diff IS the prompt review."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dba2.domain.media import StoryKind
from dba2.domain.media_context import LeagueBrief
from dba2.domain.media_facts import Angle, Fact, FactSource, StoryPackage, Subject, SubjectKind
from dba2.domain.media_prompt import (
    _PHASE_PLAIN,
    BRIEF_FENCE,
    FACT_FENCE,
    NO_QUOTE_FENCE,
    RESPONSE_SCHEMA,
    RETRY_NOTE_HEADER,
    RETRY_NOTE_ITEM_CHARS,
    RETRY_NOTE_MAX_ITEMS,
    build_payload,
    build_persona_card,
    build_retry_note,
    max_tangents,
    response_schema_json,
    serialize_payload,
    with_retry_note,
)
from dba2.domain.media_writer import FormatTier
from dba2.domain.phase import Phase
from dba2.media.write.budget import budget_for
from tests.unit._media_ai_fixtures import make_brief, make_package, make_persona

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "media_prompt"


def _card(tier: FormatTier, *, has_brief: bool) -> str:
    budget = budget_for(tier, make_package().story_kind)
    return build_persona_card(
        make_persona(), tier, title_max=budget.title_max,
        description_max=budget.description_max, has_brief=has_brief,
    )


def _payload_text(tier: FormatTier, brief: LeagueBrief | None) -> str:
    budget = budget_for(tier, make_package().story_kind)
    payload = build_payload(
        make_package(), brief, tier, title_max=budget.title_max,
        description_max=budget.description_max,
    )
    return serialize_payload(payload)


def _golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


def test_persona_card_blurb_with_brief_matches_golden():
    assert _card(FormatTier.BLURB, has_brief=True) == _golden("card_blurb_brief.txt")


def test_persona_card_article_with_brief_matches_golden():
    assert _card(FormatTier.ARTICLE, has_brief=True) == _golden("card_article_brief.txt")


def test_persona_card_without_brief_matches_golden():
    assert _card(FormatTier.BLURB, has_brief=False) == _golden("card_blurb_nobrief.txt")


def test_payload_with_brief_matches_golden():
    assert _payload_text(FormatTier.BLURB, make_brief()) == _golden("payload_blurb_brief.json")


def test_payload_without_brief_matches_golden():
    assert _payload_text(FormatTier.BLURB, None) == _golden("payload_blurb_nobrief.json")


def test_tangent_clause_absent_without_a_brief_and_present_with_one():
    with_brief = _card(FormatTier.BLURB, has_brief=True)
    without = _card(FormatTier.BLURB, has_brief=False)
    assert "at most 1 of its entries" in with_brief
    assert "entries" not in without and "forced link" not in without


def test_tangent_cap_is_derived_from_tier_not_configured():
    assert max_tangents(FormatTier.BLURB) == 1
    assert max_tangents(FormatTier.ARTICLE) == 2
    assert "at most 2 of its entries" in _card(FormatTier.ARTICLE, has_brief=True)


@pytest.mark.parametrize("tier", list(FormatTier))
def test_every_card_carries_the_fences_and_the_real_budget_numbers(tier):
    card = _card(tier, has_brief=False)
    budget = budget_for(tier, make_package().story_kind)
    for fence in (FACT_FENCE, BRIEF_FENCE, NO_QUOTE_FENCE):
        assert fence in card
    assert f"at most {budget.title_max} characters" in card
    assert f"at most {budget.description_max}" in card
    assert "Never name a source." in card  # the persona's own fence


def test_payload_brief_key_is_an_explicit_null_never_omitted():
    payload = json.loads(_payload_text(FormatTier.BLURB, None))
    assert "league_brief" in payload and payload["league_brief"] is None


def test_payload_derived_key_is_always_a_list_and_carries_no_float_repr():
    """G2-7/G2-10: present and `[]` when nothing derives (a missing key
    invites invention), and every value a rendered string so no float repr
    reaches the bytes."""
    empty = json.loads(_payload_text(FormatTier.BLURB, None))
    assert empty["derived"] == []
    derived = json.loads(_payload_text(FormatTier.BLURB, make_brief()))["derived"]
    assert isinstance(derived, list) and derived
    for entry in derived:
        assert set(entry) == {"about", "key", "label", "value_text"}
        assert isinstance(entry["value_text"], str)
        assert all(isinstance(v, int) for v in entry["about"].values())


def test_card_points_the_model_at_the_derived_list():
    card = _card(FormatTier.ARTICLE, has_brief=True)
    assert "The `derived` list holds the differences and rates already worked out" in card
    assert "only in the direction the `derived` standings move states" in card


def test_payload_hides_the_signed_delta_and_shows_the_move_in_words():
    """Gates-v2 live run: +4 (a fall) was written "up 4" in 6 of 8 profiles.
    The payload drops the signed fact and carries the direction phrase; the
    package itself keeps the fact (the gate still binds it)."""
    team = Subject(kind=SubjectKind.TEAM, team_id=5, team_name="Golden State Warriors")
    package = StoryPackage(
        story_kind=StoryKind.TEAM_PROFILE, angle=Angle.FULL_PICTURE, subject=team,
        facts=(
            Fact("standings_position", "Standings position", "23", 23.0,
                 FactSource.PULSE_SNAPSHOT),
            Fact("standings_position_delta", "Change since last pulse", "4", 4.0,
                 FactSource.PULSE_SNAPSHOT),
        ),
        season=2026, phase=Phase.REGULAR_SEASON, cycle_index=3,
    )
    payload = build_payload(package, None, FormatTier.BLURB, title_max=80, description_max=350)
    assert [f["key"] for f in payload["story"]["facts"]] == ["standings_position"]
    assert payload["derived"] == [{
        "key": "standings_move", "label": "Standings move since last pulse",
        "value_text": "fell 4 spots, from 19th to 23rd", "about": {"team_id": 5},
    }]
    assert package.number("standings_position_delta") == 4.0


def test_payload_key_order_is_stable_sorted():
    text = _payload_text(FormatTier.BLURB, make_brief())
    assert list(json.loads(text)) == sorted(json.loads(text))
    assert text == _payload_text(FormatTier.BLURB, make_brief())  # same bytes run to run


def test_response_schema_is_the_two_field_contract_and_compact():
    assert RESPONSE_SCHEMA["required"] == ["headline", "body"]
    assert RESPONSE_SCHEMA["additionalProperties"] is False
    assert " " not in response_schema_json()
    assert json.loads(response_schema_json()) == RESPONSE_SCHEMA


def test_league_brief_round_trips_through_prompt_json():
    brief = make_brief()
    again = LeagueBrief.from_prompt_json(json.loads(json.dumps(brief.to_prompt_json())))
    assert again == brief
    assert again.allowed_numbers() == brief.allowed_numbers()
    assert again.allowed_proper_nouns() == brief.allowed_proper_nouns()


def test_league_brief_rehydration_rejects_the_empty_default_and_malformed_input():
    with pytest.raises(ValueError):
        LeagueBrief.from_prompt_json({})  # migration 025's `'{}'` default = no brief
    with pytest.raises(KeyError):
        LeagueBrief.from_prompt_json({"standings": []})


def test_every_phase_has_a_plain_language_phrase_in_the_card():
    assert set(_PHASE_PLAIN) == set(Phase)
    card = _card(FormatTier.ARTICLE, has_brief=True)
    assert "TRADE_DEADLINE is the trade deadline" in card
    assert "DRAFT_LOTTERY is the draft lottery" in card


def test_card_forbids_computing_numbers_and_meta_commentary_and_stock_openers():
    card = _card(FormatTier.ARTICLE, has_brief=True)
    assert "Never compute one" in card and "never as a figure you worked out" in card
    assert "Never write about what the facts lack" in card
    assert "never a target" in card
    assert "Vary how you open and close" in card
    assert "never invent lore" in card


# -- the retry note (docs/design/media-v2-step8-gates-v2.md G2-18) -------------

_RETRY_CASES = (
    ("numbers_bound", (
        "9 -> Phoenix Suns; bound to Denver Nuggets",
        "record 24-13 -> Los Angeles Lakers; bound to Denver Nuggets",
        "spots '14 places' -> Minnesota Timberwolves; bound to nothing",
    )),
    ("claims", (
        "move direction 'up' '4 spots' -> Golden State Warriors; bound to Golden State Warriors",
        "games left '1 game left' -> Los Angeles Clippers, Phoenix Suns; bound to nothing",
    )),
    ("numbers", ("47.7",)),
    ("proper_nouns", ("Tyrese Holloway",)),
    ("quoted_speech", ('"We will win this"',)),
)


def _retry_notes_text() -> str:
    return "\n=====\n".join(build_retry_note(gate, lines) for gate, lines in _RETRY_CASES)


def test_retry_notes_match_golden():
    """The retry note is prompt text: a wording change is reviewed as this
    golden's diff, like the cards."""
    assert _retry_notes_text() == _golden("retry_notes.txt")


def test_retry_note_is_deterministic_and_dedupes():
    lines = ("9 -> Phoenix Suns; bound to Denver Nuggets",) * 2
    note = build_retry_note("numbers_bound", lines)
    assert note == build_retry_note("numbers_bound", lines)
    assert note.count("- 9: not a number") == 1


def test_retry_note_is_bounded_in_items_and_characters():
    many = tuple(f"{n} -> Phoenix Suns; bound to nothing" for n in range(1, 9))
    note = build_retry_note("numbers_bound", many)
    items = [line for line in note.splitlines() if line.startswith("- ")]
    assert len(items) == RETRY_NOTE_MAX_ITEMS + 1
    assert items[-1] == "- and 3 more of the same kind"
    long_note = build_retry_note("proper_nouns", ("Aaa " * 100,))
    item = next(line for line in long_note.splitlines() if line.startswith("- "))
    assert len(item) == 2 + RETRY_NOTE_ITEM_CHARS and item.endswith("...")
    assert "\n- Aaa Aaa" in long_note  # whitespace collapsed, one line per item


def test_retry_note_carries_only_the_header_the_gate_lines_and_the_footer():
    """Nothing but the rejection: no payload, no brief, no rejected draft."""
    brief = make_brief()
    note = build_retry_note("proper_nouns", ("Tyrese Holloway",))
    assert note.splitlines() == [
        RETRY_NOTE_HEADER,
        "- Tyrese Holloway: a name that is not in the facts",
        "Use only names that appear in the JSON.",
    ]
    for row in brief.to_prompt_json()["standings"]:
        assert row["team_name"] not in note


def test_retry_note_reads_the_real_bound_gate_lines():
    """The note parses verify.py's `what -> about; bound to owners` lines, so
    pin that contract against the real gate rather than a copied string."""
    from dba2.media.write.verify import verify_numbers_bound

    package = make_package()
    result = verify_numbers_bound(package, "Headline", "They added 777 in cap space.")
    assert not result.passed and " -> " in result.offending[0]
    note = build_retry_note(result.gate, result.offending)
    assert "- 777: not a number the facts give for " in note
    assert "no fact you were given holds it" in note


def test_with_retry_note_leaves_the_payload_bytes_alone():
    payload = _payload_text(FormatTier.BLURB, make_brief())
    assert with_retry_note(payload, None) == payload
    assert with_retry_note(payload, "") == payload
    joined = with_retry_note(payload, "NOTE\n")
    assert joined == payload + "\n\nNOTE\n"
    assert json.JSONDecoder().raw_decode(joined)[0] == json.loads(payload)
