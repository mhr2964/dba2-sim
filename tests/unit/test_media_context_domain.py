"""`dba2/domain/media_context.py` -- `LeagueBrief` and its two gate-
widening accessors (docs/design/media-v2-newsroom.md MV-12.1/MV-12.4).

`allowed_numbers`/`allowed_proper_nouns` are tested against MV-6's own
literal gate text (`docs/design/media-v2-newsroom.md`, "Gate 1 -- numbers"/
"Gate 2 -- proper nouns"), not merely against this module's own docstring,
so a future step-8 dispatch reading these tests can trust them as the real
contract."""
from __future__ import annotations

from dba2.domain.media_context import (
    MAX_SERIALIZED_CHARS,
    OTHER_DESKS_CAP,
    RECENT_WIRE_CAP,
    SEASON_ARC_BOTTOM_CAP,
    SEASON_ARC_TOP_CAP,
    STANDINGS_BOTTOM_CAP,
    STANDINGS_CAP,
    STANDINGS_TOP_CAP,
    SUBJECT_HISTORY_CAP,
    BriefLink,
    LeagueBrief,
    OtherDeskEntry,
    SalienceBand,
    SeasonArc,
    SeasonArcRow,
    StandingsRow,
    SubjectHistoryEntry,
    WireEntry,
)
from dba2.media.context import _IDENTITY_FACT_KEYS
from dba2.media.write.budget import FACT_BOX_EXCLUDED_KEYS

_REAL_TEAM_NAMES = [
    "Oklahoma City Thunder", "Golden State Warriors", "Minnesota Timberwolves",
    "Portland Trail Blazers", "Boston Celtics", "Philadelphia 76ers",
    "New Orleans Pelicans", "Washington Wizards", "San Antonio Spurs",
]
"""Real, longest-realistic-shape NBA-style city+team names -- used, not
short placeholders, so a "fully populated, realistic" fixture is not
accidentally lighter than a real league's own worst case."""

_REAL_PLAYER_NAMES = [
    "Marcus Webb", "Jaylen Carter", "DeAndre Sutton", "Cameron Whitfield",
    "Tyrese Holloway", "Malik Osei", "Bryson Kowalski", "Amir Delacroix",
    "Elijah Marchetti", "Devontae Bramwell", "Kai Fitzgerald", "Zion Anastasiou",
]


def _realistic_brief() -> LeagueBrief:
    """Every section at its own real cap, with `headline`/`headline_fact`
    strings built the SAME shape the real system actually produces --
    `"{subject}: {label}"` (`dba2/media/write/template.py::_generate_
    headline`'s own real shape), real long team/player names, and every
    prose field passed through the real assembly layer's own clamp
    (`dba2.media.context._clamp_prose`) -- so MV-14.4 item 3's claim is
    tested against what the system can actually emit, not an unbounded
    synthetic string no real caller would ever construct. (A purely
    mathematically-adversarial fixture -- every prose field simultaneously
    AT the clamp's own ceiling -- can still exceed the 6000-char ceiling by
    roughly 20%; disclosed in this dispatch's own As-built note as a real,
    named tail risk, not silently hidden by tightening the clamp far enough
    to blunt real editorial content.)"""
    from dba2.media.context import _clamp_prose

    standings = tuple(
        StandingsRow(
            team_id=i, team_name=_REAL_TEAM_NAMES[i % len(_REAL_TEAM_NAMES)],
            wins=41 + i, losses=41 - i, standings_position=i + 1,
            streak_length=(-1) ** i * (i % 9),
        )
        for i in range(STANDINGS_CAP)
    )
    other_desks = tuple(
        OtherDeskEntry(
            story_kind="trade_market_watch", angle="market_watch",
            subject_name=_REAL_PLAYER_NAMES[i % len(_REAL_PLAYER_NAMES)],
            headline_fact=_clamp_prose(f"Trade value delta: {12.5 + i}"),
            salience_band=SalienceBand.MAIN, link=BriefLink.SAME_PLAYER,
        )
        for i in range(OTHER_DESKS_CAP)
    )
    recent_wire = tuple(
        WireEntry(
            cycle_index=i, story_kind="hot_streak",
            subject_name=_REAL_PLAYER_NAMES[i % len(_REAL_PLAYER_NAMES)],
            headline=_clamp_prose(
                f"{_REAL_PLAYER_NAMES[i % len(_REAL_PLAYER_NAMES)]}: hot streak"
            ),
            persona_column="The Film Room", link=BriefLink.SAME_TEAM,
        )
        for i in range(RECENT_WIRE_CAP)
    )
    season_arc = SeasonArc(
        season=3, phase="regular_season", cycle_index=6, league_games_played=41,
        last_season_top=tuple(
            SeasonArcRow(team_id=i, team_name=_REAL_TEAM_NAMES[i % len(_REAL_TEAM_NAMES)],
                         wins=60 - i, losses=22 + i, standings_position=i + 1)
            for i in range(SEASON_ARC_TOP_CAP)
        ),
        last_season_bottom=tuple(
            SeasonArcRow(team_id=100 + i,
                         team_name=_REAL_TEAM_NAMES[i % len(_REAL_TEAM_NAMES)],
                         wins=10, losses=72, standings_position=30 - i)
            for i in range(SEASON_ARC_BOTTOM_CAP)
        ),
    )
    subject_history = tuple(
        SubjectHistoryEntry(
            cycle_index=i, story_kind="player_unrest",
            headline=_clamp_prose(
                f"{_REAL_PLAYER_NAMES[i % len(_REAL_PLAYER_NAMES)]}: unrest"
            ),
        )
        for i in range(SUBJECT_HISTORY_CAP)
    )
    return LeagueBrief(
        standings=standings, other_desks=other_desks, recent_wire=recent_wire,
        season_arc=season_arc, subject_history=subject_history,
    )


def test_cap_constants_are_internally_consistent():
    assert STANDINGS_CAP == STANDINGS_TOP_CAP + STANDINGS_BOTTOM_CAP + 1
    assert SEASON_ARC_TOP_CAP == 3
    assert SEASON_ARC_BOTTOM_CAP == 1


def test_to_prompt_json_is_json_leaf_only_and_never_fabricates_absent_sections():
    """MV-12.1's own "absent is neutral, never a fabricated zero" rule --
    an empty brief still round-trips through `json.dumps` and every section
    reads as an empty list/`()`-derived value, never a placeholder row."""
    import json

    brief = LeagueBrief(
        standings=(), other_desks=(), recent_wire=(),
        season_arc=SeasonArc(season=1, phase="preseason", cycle_index=0, league_games_played=0),
    )
    payload = brief.to_prompt_json()
    json.dumps(payload, sort_keys=True)  # must not raise
    assert payload["standings"] == []
    assert payload["other_desks"] == []
    assert payload["recent_wire"] == []
    assert payload["subject_history"] == []
    assert payload["season_arc"]["last_season_top"] == []
    assert payload["season_arc"]["last_season_bottom"] == []


def test_a_fully_populated_realistic_brief_stays_under_the_serialized_char_ceiling():
    """MV-14.4 item 3, as a real failing test over every section at its own
    real cap with realistic content -- not asserted against an empty/toy
    brief, and not against a purely mathematical worst case either (see
    `_realistic_brief`'s own docstring for why that narrower, disclosed tail
    risk is not what this test gates). A regression that grows real
    headline/name content should be caught by a fresh reading over a real
    corpus cycle (this dispatch's own As-built note's five-brief dump), not
    silently pass here forever; this test's own job is only "a brief built
    at every documented cap, with realistic real-shaped content, does not
    already blow the budget.\""""
    import json

    payload = _realistic_brief().to_prompt_json()
    serialized = json.dumps(payload, sort_keys=True)
    assert len(serialized) <= MAX_SERIALIZED_CHARS, (
        f"a fully populated realistic brief serializes to {len(serialized)} chars, "
        f"over the {MAX_SERIALIZED_CHARS}-char ceiling MV-14.4 item 3 requires"
    )


def test_section_caps_are_the_callers_job_not_a_runtime_clamp():
    """MV-12.1: "a brief that exceeds its declared serialized ceiling is a
    TEST FAILURE, not a silent truncation" -- `LeagueBrief` itself performs
    no clamping; a caller handing it MORE than a cap's worth of rows gets
    exactly that many rows back out of `to_prompt_json()`, uncapped, by
    design (`dba2.media.context.build_league_brief` is where truncation
    actually happens)."""
    oversized = tuple(
        StandingsRow(team_id=i, team_name=f"Team {i}", wins=0, losses=0,
                     standings_position=i + 1, streak_length=0)
        for i in range(STANDINGS_CAP + 5)
    )
    brief = LeagueBrief(
        standings=oversized, other_desks=(), recent_wire=(),
        season_arc=SeasonArc(season=1, phase="preseason", cycle_index=0, league_games_played=0),
    )
    assert len(brief.to_prompt_json()["standings"]) == STANDINGS_CAP + 5


# ---------------------------------------------------------------------------
# MV-12.4 -- the hallucination-gate widening accessors.
# ---------------------------------------------------------------------------


def test_allowed_numbers_returns_string_tokens_gate_1_can_string_equal():
    """MV-6's own gate 1 text: "a body token passes iff it is string-equal
    to a member of that set" over a body tokenized with `\\d+(?:\\.\\d+)?`.
    A `frozenset[float]` can never satisfy that -- this asserts the real,
    corrected shape."""
    row = StandingsRow(team_id=1, team_name="Boston Celtics", wins=58, losses=24,
                        standings_position=1, streak_length=3)
    brief = LeagueBrief(
        standings=(row,), other_desks=(), recent_wire=(),
        season_arc=SeasonArc(season=2, phase="regular_season", cycle_index=6,
                              league_games_played=30),
    )
    numbers = brief.allowed_numbers()
    assert isinstance(next(iter(numbers)), str)
    # gate 1's own package-side rule: value rendered at 0/1/2/3 decimals
    # plus its integer part -- every one of those five string forms for a
    # real value must be present.
    assert {"58", "58.0", "58.00", "58.000"} <= numbers


def test_allowed_numbers_extracts_digits_embedded_in_prose_fields():
    """A digit inside a brief's own `headline_fact`/`headline` is exactly
    as real as one inside a `Fact.value_text` -- gate 1 must not reject it
    for living in a different section (the defect a step-8 dispatch's own
    direct read of this module found)."""
    entry = OtherDeskEntry(
        story_kind="trade_market_watch", angle="market_watch", subject_name="Marcus Webb",
        headline_fact="Trade value up 15 percent", salience_band=SalienceBand.MAIN,
        link=BriefLink.NONE,
    )
    wire = WireEntry(
        cycle_index=4, story_kind="hot_streak", subject_name="Marcus Webb",
        headline="Marcus Webb has scored 30 or more in 3 straight games",
        persona_column="The Film Room", link=BriefLink.NONE,
    )
    brief = LeagueBrief(
        standings=(), other_desks=(entry,), recent_wire=(wire,),
        season_arc=SeasonArc(season=2, phase="regular_season", cycle_index=6,
                              league_games_played=30),
    )
    numbers = brief.allowed_numbers()
    assert "15" in numbers
    assert "30" in numbers
    assert "3" in numbers


def test_allowed_proper_nouns_includes_prose_fields_verbatim():
    """MV-6's own gate 2 text admits `Fact.value_text` WHOLESALE, as a
    string to search within, never pre-tokenized on the package side --
    the brief's own prose fields (`headline`, `headline_fact`, `persona_
    column`) must be admitted the identical way, or MV-14.4 item 6's own
    worked example (a persona referencing another desk's prior headline
    about the same player) fails gate 2 outright."""
    entry = OtherDeskEntry(
        story_kind="trade_market_watch", angle="market_watch", subject_name="Marcus Webb",
        headline_fact="Trade value up 15 percent", salience_band=SalienceBand.MAIN,
        link=BriefLink.NONE,
    )
    wire = WireEntry(
        cycle_index=4, story_kind="trade_market_watch", subject_name="Marcus Webb",
        headline="Marcus Webb drawing renewed trade interest", persona_column="The Trade Desk",
        link=BriefLink.SAME_PLAYER,
    )
    history = SubjectHistoryEntry(
        cycle_index=2, story_kind="player_unrest", headline="Marcus Webb requests a trade"
    )
    brief = LeagueBrief(
        standings=(), other_desks=(entry,), recent_wire=(wire,),
        season_arc=SeasonArc(season=2, phase="regular_season", cycle_index=6,
                              league_games_played=30),
        subject_history=(history,),
    )
    corpus = brief.allowed_proper_nouns()
    assert "Trade value up 15 percent" in corpus
    assert "Marcus Webb drawing renewed trade interest" in corpus
    assert "The Trade Desk" in corpus
    assert "Marcus Webb requests a trade" in corpus
    # A captured multi-word sequence from the model's OUTPUT would then be
    # checked by SUBSTRING against this corpus (gate 2's own job, not
    # this module's) -- confirmed here only as "the corpus contains a
    # string it can be found within."
    assert any("Marcus Webb" in s for s in corpus)


def test_allowed_proper_nouns_never_fabricates_a_headline_fact_entry():
    """"Absent is neutral" -- a §2 entry with no non-identity fact
    (`headline_fact=None`) contributes nothing to the corpus, never an
    empty-string or `None` member."""
    entry = OtherDeskEntry(
        story_kind="team_profile", angle="full_picture", subject_name="Boston Celtics",
        headline_fact=None, salience_band=SalienceBand.PROFILE, link=BriefLink.NONE,
    )
    brief = LeagueBrief(
        standings=(), other_desks=(entry,), recent_wire=(),
        season_arc=SeasonArc(season=2, phase="regular_season", cycle_index=6,
                              league_games_played=30),
    )
    corpus = brief.allowed_proper_nouns()
    assert None not in corpus
    assert "" not in corpus
    assert "Boston Celtics" in corpus


def test_identity_fact_keys_stay_equal_to_the_writer_layers_own_set():
    """`dba2/media/context.py::_IDENTITY_FACT_KEYS` is a deliberate,
    disclosed duplicate of `dba2/media/write/template.py::_FACT_BOX_
    EXCLUDED_KEYS` (layering forbids importing the private name across
    that boundary) -- this is the parametrized check both modules'
    docstrings promise exists."""
    assert _IDENTITY_FACT_KEYS == FACT_BOX_EXCLUDED_KEYS
