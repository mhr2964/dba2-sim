"""`dba2/media/write/verify.py` -- the three gates, tabled
(docs/design/media-v2-step8-anthropic-writer.md S8-10, S8-15 item 1).

Includes the ASSERTED BLIND SPOTS: cases the gates must PASS because they
cannot see them. Asserting them (MV-9's own precedent: "the guard accepting
a bare ordinal") is what stops a future reader believing this is a
fact-checker."""
from __future__ import annotations

import pytest

from dba2.domain.media import StoryKind
from dba2.domain.media_bindings import TeamAliases
from dba2.domain.media_context import (
    BriefLink,
    LeagueBrief,
    OtherDeskEntry,
    SalienceBand,
    SeasonArc,
    StandingsRow,
)
from dba2.domain.media_facts import (
    Angle,
    Fact,
    FactSource,
    StoryPackage,
    Subject,
    SubjectKind,
)
from dba2.domain.phase import Phase
from dba2.media.write.verify import (
    NumberPolicy,
    allowed_number_tokens,
    allowed_proper_noun_corpus,
    verify_claims,
    verify_no_invented_numbers,
    verify_no_invented_proper_nouns,
    verify_no_quoted_speech,
    verify_numbers_bound,
    verify_story_text,
)
from tests.unit._media_ai_fixtures import make_brief, make_package, make_persona

PKG = make_package()
PERSONA = make_persona()
BRIEF = make_brief()


def _nums(body, headline="Trade", brief=None):
    return verify_no_invented_numbers(PKG, headline, body, brief=brief)


def _nouns(body, headline="Trade", brief=None, team_names=()):
    return verify_no_invented_proper_nouns(
        PKG, headline, body, persona=PERSONA, brief=brief, team_names=team_names
    )


# -- gate 1 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Portland sent 3 assets.",  # value_text token
        "A 12.5-point swing.",  # exact value
        "A 12.50-point swing.",  # value_number at 2 decimals -> "12.50" allowed
        "A 13-point swing.",  # "13" is NOT an allowed rendering of 12.5 (see reject table)
    ][:3],
)
def test_gate1_accepts_package_numbers_at_every_precision(body):
    assert _nums(body).passed


@pytest.mark.parametrize("token,expected", [("12", "12"), ("0.6", "0.6"), ("0.613", "0.613")])
def test_gate1_rounding_renderings_of_value_number_are_allowed(token, expected):
    assert expected in allowed_number_tokens(PKG)


def test_gate1_accepts_season_and_prior_season():
    assert _nums("Since 2025 and in 2026.").passed


def test_gate1_accepts_a_brief_number_and_digits_inside_brief_prose():
    assert _nums("Webb's 5-game run, 42 games in.", brief=BRIEF).passed  # 5 in headline prose
    assert _nums("Level 8 unrest.", brief=BRIEF).passed  # 8 only in headline_fact prose


def test_gate1_rejects_an_invented_number_in_body_or_headline():
    result = _nums("He scored 41.")
    assert not result.passed and result.gate == "numbers" and "41" in result.offending
    assert not _nums("ok", headline="Webb drops 55").passed


def test_gate1_brief_numbers_are_not_admitted_without_a_brief():
    assert not _nums("Level 8 unrest.").passed


def test_gate1_never_admits_the_tier_character_limits():
    # 350 / 80 are instructions, not facts.
    assert not _nums("A 350 word piece.").passed
    assert not _nums("x", headline="80 things").passed


def test_gate1_blind_spots_spelled_numbers_and_ordinals_pass():
    assert _nums("He scored forty-one, his fifth in a row, a second straight.").passed


# -- gate 2 ----------------------------------------------------------------


def test_gate2_accepts_package_names_and_possessives():
    text = "Marcus Webb's move sends him from Portland Trail Blazers to Boston Celtics."
    assert _nouns(text).passed


def test_gate2_accepts_the_writing_personas_own_name_and_column():
    assert _nouns("Jordan Ellis of The Trade Desk reports a deal.").passed


def test_gate2_accepts_the_cycles_team_vocabulary():
    assert not _nouns("The Denver Nuggets watch.").passed
    assert _nouns("The Denver Nuggets watch.", team_names=["Denver Nuggets"]).passed


def test_gate2_accepts_a_brief_prior_headline_and_other_desk_name():
    text = "As Marcus Webb: hot streak over 5 games showed, and The Long View noted."
    assert not _nouns(text).passed  # persona column of another desk is not in the package alone
    assert _nouns(text, brief=BRIEF).passed
    assert _nouns("Devon Hale is restless.", brief=BRIEF).passed


def test_gate2_prose_field_sequences_are_extracted_defect_a():
    corpus = allowed_proper_noun_corpus(PKG, PERSONA, BRIEF)
    assert "Marcus Webb" in corpus and "The Long View" in corpus


def test_gate2_accepts_closed_league_vocabulary_derived_from_the_phase_enum():
    assert _nouns("Ahead of the Trade Deadline, a deal.").passed
    assert _nouns("Free Agency looms for Marcus Webb.").passed


def test_gate2_rejects_an_invented_two_word_name():
    result = _nouns("Tyrese Holloway was also involved.")
    assert not result.passed and result.gate == "proper_nouns"
    assert result.offending == ("Tyrese Holloway",)


def test_gate2_sentence_initial_common_word_does_not_over_reject():
    assert _nouns("Tonight Marcus Webb debuts.").passed  # "Tonight" sheds; rest is allowed
    assert not _nouns("Tonight Tyrese Holloway debuts.").passed
    # An invented name at a sentence start must NOT shed its first name.
    assert not _nouns("Tyrese Holloway debuts.").passed


def test_gate2_mid_sentence_invented_run_still_rejected_even_with_a_known_first_word():
    assert not _nouns("Portland then Marcus Holloway arrived.").passed


def test_gate2_accepts_attribution_idiom_sources_say():
    assert _nouns("Sources say Portland Trail Blazers view the deal as a win.").passed


def test_gate2_blind_spot_a_single_word_invented_surname_passes():
    assert _nouns("Holloway was also involved.").passed


# -- gate 3 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ['He said "we are ready" after.', "He said “we are ready” after."],
)
def test_gate3_rejects_straight_and_curly_quoted_spans(text):
    result = verify_no_quoted_speech("Headline", text)
    assert not result.passed and result.gate == "quoted_speech"


def test_gate3_rejects_a_quoted_headline_too():
    assert not verify_no_quoted_speech('"Wow" says Webb', "body").passed


def test_gate3_accepts_attribution_idiom_and_apostrophes():
    body = "Sources say Portland view Webb's arrival as key."
    assert verify_no_quoted_speech("Headline", body).passed


# -- composite -------------------------------------------------------------


def test_verify_story_text_first_failure_wins_and_a_clean_story_passes():
    ok = verify_story_text(
        PKG, "Webb heads to Boston Celtics", "Marcus Webb moves in a 3 asset deal.",
        persona=PERSONA, brief=BRIEF,
    )
    assert ok.passed
    bad = verify_story_text(
        PKG, "Trade", 'Marcus Webb said "yes" and scored 99.', persona=PERSONA, brief=BRIEF,
    )
    assert not bad.passed and bad.gate == "numbers_bound"  # BOUND is the default


def test_starters_found_in_the_first_live_rejection_log_shed_but_do_not_open_a_hole():
    """Real rejections of accurate stories: "Only Utah", "It's PRESEASON" (commit 6)."""
    from dba2.media.write import verify

    corpus = frozenset({"Utah Jazz", "PRESEASON"})
    assert verify._sequence_allowed("Only Utah", True, corpus)
    assert verify._sequence_allowed("It's PRESEASON", True, corpus)
    assert not verify._sequence_allowed("Only Tyrese Holloway", True, corpus)


def test_starters_found_in_the_s8_22_rejection_log_shed_but_do_not_open_a_hole():
    """Real rejections of accurate stories: "Whether Los Angeles", "What Phoenix"."""
    from dba2.media.write import verify

    corpus = frozenset({"Los Angeles Lakers", "Phoenix Suns"})
    assert verify._sequence_allowed("Whether Los Angeles", True, corpus)
    assert verify._sequence_allowed("What Phoenix", True, corpus)
    assert not verify._sequence_allowed("Whether Tyrese Holloway", True, corpus)
    assert not verify._sequence_allowed("What Tyrese Holloway", True, corpus)


# -- gate 1b (bound numbers) and gate 4 (claims) -----------------------------
# docs/design/media-v2-step8-gates-v2.md G2-3, G2-5, G2-6, G2-12 item 1. The
# rows are small on purpose: one attribution rule or one typed shape each.

_TEAMS = {
    1: ("Minnesota", "Timberwolves"), 2: ("Los Angeles", "Lakers"), 3: ("San Antonio", "Spurs"),
    4: ("Denver", "Nuggets"), 7: ("Milwaukee", "Bucks"), 8: ("Phoenix", "Suns"),
    9: ("Los Angeles", "Clippers"), 12: ("Dallas", "Mavericks"),
    13: ("Golden State", "Warriors"),
}
ALIASES = {t: TeamAliases(f"{c} {n}", c, n) for t, (c, n) in _TEAMS.items()}
_P = FactSource.PERSISTED


def _fact(key, value, text=None, label="Label"):
    return Fact(key, label, text if text is not None else str(value), value, _P)


def _story(subject, facts, kind):
    return StoryPackage(story_kind=kind, angle=Angle.FULL_PICTURE, subject=subject,
                        facts=(*facts, _fact("season", 2026.0, "2026", "Season")),
                        season=2026, phase=Phase.REGULAR_SEASON, cycle_index=2)


BOUND_BRIEF = LeagueBrief(
    standings=(
        StandingsRow(1, "Minnesota Timberwolves", 25, 12, 1, 6),
        StandingsRow(2, "Los Angeles Lakers", 24, 12, 2, -4),
        StandingsRow(3, "San Antonio Spurs", 24, 12, 3, -1),
        StandingsRow(4, "Denver Nuggets", 24, 13, 4, 2),
        StandingsRow(9, "Los Angeles Clippers", 25, 24, 16, 3),
        StandingsRow(7, "Milwaukee Bucks", 9, 29, 30, -5),
    ),
    other_desks=(
        OtherDeskEntry("player_unrest", "unrest", "Devon Hale", "Devon Hale unrest level 8",
                       SalienceBand.MAIN, BriefLink.NONE),
    ),
    recent_wire=(),
    season_arc=SeasonArc(season=2026, phase="regular_season", cycle_index=2,
                         league_games_played=38),
)
LAKERS = _story(
    Subject(kind=SubjectKind.TEAM, team_id=2, team_name="Los Angeles Lakers"),
    [_fact("wins", 24.0), _fact("losses", 12.0), _fact("standings_position", 2.0),
     _fact("streak_length", -4.0), _fact("standings_position_delta", -3.0)],
    StoryKind.TEAM_PROFILE,
)
SUNS = _story(
    Subject(kind=SubjectKind.TEAM, team_id=8, team_name="Phoenix Suns"),
    [_fact("pace_rank", 7.0), _fact("team_standings_position", 10.0)],
    StoryKind.SCHEME_IDENTITY,
)
DAVIS = _story(
    Subject(kind=SubjectKind.PLAYER, player_id=50, player_name="Anthony Davis", team_id=12,
            team_name="Dallas Mavericks"),
    [_fact("window_points_per36", 20.216), _fact("baseline_points_per36", 29.18)],
    StoryKind.COLD_STRETCH,
)
RIVALRY = _story(
    Subject(kind=SubjectKind.MATCHUP, team_id=9, team_name="Los Angeles Clippers",
            opponent_team_id=8, opponent_team_name="Phoenix Suns"),
    [_fact("team_wins_this_series", 2.0), _fact("opponent_wins_this_series", 1.0),
     _fact("closest_margin", 1.0), _fact("rivalry_score", 6.0),
     _fact("team_named_players", None, "Kawhi Leonard, James Harden")],
    StoryKind.RIVALRY_MATCHUP,
)


def _bound(body, pkg=LAKERS, headline="Headline", derived=True):
    """Gate 1b then gate 4, the first failure wins (as the pass runs them)."""
    for gate in (verify_numbers_bound, verify_claims):
        result = gate(pkg, headline, body, brief=BOUND_BRIEF, team_aliases=ALIASES,
                      derived=derived)
        if not result.passed:
            return result
    return result


@pytest.mark.parametrize(
    "body",
    [
        "The Lakers are 24-12.",  # own clause
        "The Denver Nuggets (24-13) follow.",  # nearest preceding clause
        "The Lakers are 24-12, one game behind Minnesota at 25-12.",  # comparative
        "Minnesota's 25 wins lead, and its 12 losses match the Lakers.",  # pronoun + possessive
        "The Lakers have lost 4 straight. That leaves a 24-12 record.",  # paragraph focus
        "The Lakers have lost 4 straight and moved up 3 spots.",  # negative facts as magnitudes
        "In 2026 the league has played 38 games.",  # LEAGUE numbers anywhere, untyped
        "Los Angeles is 24-12.",  # a shared city resolves to both LA teams
        "The Lakers sit 2nd in the standings.",  # C3: a position
        "Minnesota holds first place.",  # C5
        "Milwaukee is in last place.",  # C5, the largest position shown
        "The Lakers are 1 game behind first place.",  # C5 exemption: not a claim to hold it
        "Devon Hale sits at level 8.",  # a brief entry's own name scope
    ],
)
def test_bound_gate_accepts_true_attribution_shapes(body):
    result = _bound(body)
    assert result.passed, result.offending


def test_bound_gate_attributes_a_headline_to_the_subject():
    assert _bound("Body.", headline="24-12 and sliding").passed
    assert not _bound("Body.", headline="24-13 and sliding").passed


@pytest.mark.parametrize(
    "body,gate,fragment",
    [
        ("The Lakers are 24-13.", "numbers_bound", "bound to Denver Nuggets"),
        ("The Denver Nuggets are 24-12.", "numbers_bound", "record 24-12 -> Denver Nuggets"),
        ("The Lakers are 12-24.", "numbers_bound", "record 12-24"),  # a reversed record
        ("The Los Angeles Lakers are 25-24.", "numbers_bound", "bound to Los Angeles Clippers"),
        ("Minnesota is on a 4-game win streak.", "numbers_bound", "streak"),
        ("The Lakers have won 4 straight.", "claims", "streak direction"),  # C2
        ("The Lakers are up 10 spots.", "numbers_bound", "spots"),  # C4 as a typed token
        ("The Lakers have 3 games left.", "claims", "games left"),  # C1
        ("The Lakers hold first place.", "claims", "first place"),  # C5
        ("The Lakers sit at level 8.", "numbers_bound", "bound to Devon Hale"),
    ],
)
def test_bound_gate_rejects_misattributed_and_false_claims(body, gate, fragment):
    result = _bound(body)
    assert not result.passed and result.gate == gate
    assert any(fragment in line for line in result.offending), result.offending


def test_a_standings_place_must_be_a_position_not_a_rank():
    assert _bound("Phoenix ranks 7th in pace.", pkg=SUNS).passed
    result = _bound("Phoenix sits 7th in the standings.", pkg=SUNS)
    assert not result.passed and result.gate == "claims"  # C3


def test_series_records_bind_from_each_side_and_named_players_are_their_teams_aliases():
    assert _bound("The Clippers hold a 2-1 edge on the Suns.", pkg=RIVALRY).passed
    assert _bound("The Suns trail 1-2.", pkg=RIVALRY).passed
    assert _bound("Leonard's Clippers are 25-24.", pkg=RIVALRY).passed
    assert not _bound("The Clippers hold a 3-1 edge.", pkg=RIVALRY).passed
    assert _bound("The rivalry score stands at 6 and the closest margin is 1.", pkg=RIVALRY).passed


def test_derived_differences_pass_only_with_the_derived_list():
    body = "Davis is at 20.216, down from 29.180, a drop of nearly 9."
    assert _bound(body, pkg=DAVIS).passed
    assert not _bound(body, pkg=DAVIS, derived=False).passed
    assert not _bound("Davis dropped 7 points per 36.", pkg=DAVIS).passed  # nothing else widened


def test_bound_blind_spot_x_and_y_are_both_leaves_x_unchecked():
    """G2-3: the split at " and " gives the record to the Spurs only, so the
    Nuggets' half is unchecked. A hole, never a false reject."""
    assert _bound("The Nuggets and the Spurs are both 24-12.").passed


def test_bound_blind_spot_only_is_not_a_claim_class():
    """G2-6: uniqueness is unverifiable from a capped brief (the Spurs are
    also 24-12), so "only" passes."""
    assert _bound("The Lakers are the only team at 24-12.").passed


def test_bound_blind_spot_a_shared_city_accepts_the_other_teams_record():
    """G2-2: "Los Angeles" alone resolves to both LA teams (any-match)."""
    assert _bound("Los Angeles is 25-24.").passed


def test_number_policy_defaults_to_bound_now_that_the_payload_carries_derived():
    """G2-7/G2-13: BOUND became the default in the commit that put `derived`
    in the payload. POOLED is kept only for the offline evaluation."""
    body = "The Lakers are 24-13."  # Denver's 13: accepted by the pool
    kwargs = dict(persona=PERSONA, brief=BOUND_BRIEF,
                  team_names=[a.full for a in ALIASES.values()], team_aliases=ALIASES)
    default = verify_story_text(LAKERS, "Lakers", body, **kwargs)
    pooled = verify_story_text(LAKERS, "Lakers", body, number_policy=NumberPolicy.POOLED,
                               **kwargs)
    assert not default.passed and default.gate == "numbers_bound"
    assert pooled.passed


# -- shapes from the gates-v2 live run (scratch league 1704) ------------------


def test_games_behind_binds_to_the_leader_too_so_behind_first_place_x_passes():
    """Live run: "7.0 games behind first-place Philadelphia" names only the
    leader in its clause; games behind is a two-entity number (the row team
    and the leader), bound to both like `closest_margin`."""
    # Milwaukee's 16.5 coincides with no number of Minnesota's own.
    assert _bound("Milwaukee is 9-29, 16.5 games behind first-place Minnesota.").passed
    result = _bound("Milwaukee is 9-29, 17.5 games behind first-place Minnesota.")
    assert not result.passed and result.gate == "numbers_bound"


def test_trailing_first_place_is_not_a_claim_to_hold_it():
    """Live run: three true stories said a team "trails first place by N games"."""
    assert _bound("The Lakers trail first place by 0.5 games.").passed
    assert not _bound("The Lakers hold first place.").passed


def test_a_move_in_the_standings_with_no_delta_fact_is_rejected():
    """Live run, story 3591: the pool passed "14" via another team's loss
    count; Minnesota's package has no position delta at all."""
    result = _bound("The run has moved Minnesota up 14 places.")
    assert not result.passed and "bound to nothing" in result.offending[0]


def test_known_false_reject_a_pronoun_after_another_team_resolves_to_that_team():
    """Live run, story 3592 (true text, rejected): after a clause naming the
    leader, "Their" resolves to the leader, not the subject. Recorded as a
    known false reject; making pronouns also reach the subject would be a rule
    widening, which S8-16 item 7 leaves to a decision, not a live-run tweak."""
    body = "The Lakers trail Minnesota. Their win percentage is 0.667."
    assert not _bound(body).passed


_WITH_76ERS = {**ALIASES, 5: TeamAliases("Philadelphia 76ers", "Philadelphia", "76ers")}


def _bound_76ers(body):
    for gate in (verify_numbers_bound, verify_claims):
        result = gate(LAKERS, "Headline", body, brief=BOUND_BRIEF, team_aliases=_WITH_76ERS)
        if not result.passed:
            return result
    return result


@pytest.mark.parametrize(
    "body",
    [
        "The Lakers are 24-12, well behind the Philadelphia 76ers.",
        "The 76ers lead, and the Lakers are 24-12.",
        "The 76ers' run leaves the Lakers 2nd in the standings.",
    ],
)
def test_digits_inside_a_team_name_are_not_numbers(body):
    """Second gates-v2 live pass, story 3858: "76ers" was tokenized as the
    number 76 and rejected as bound to nothing. The digits of a matched name
    are part of the name."""
    result = _bound_76ers(body)
    assert result.passed, result.offending


def test_a_number_beside_a_team_name_is_still_checked():
    result = _bound_76ers("The Philadelphia 76ers scored 76 in the win.")
    assert not result.passed and result.offending == ("76 -> Philadelphia 76ers; bound to nothing",)
    assert not _bound_76ers("The 76ers are 77-5.").passed


# -- C4's direction (the gates-v2 direction fix) -------------------------------
# LAKERS: delta -3, now 2nd, so they ROSE 3 spots, from 5th.


@pytest.mark.parametrize(
    "body",
    [
        "The Lakers moved up 3 spots.",
        "The Lakers climbed 3 places.",
        "The Lakers rose 3 spots, from 5th to 2nd.",  # the derived move phrase, quoted
        "The Lakers sit 2nd after a 3-spot jump.",  # the noun after the count
        "That is a climb of 3 positions for the Lakers.",
        "The Lakers surged 3 places in the standings.",
        "The Lakers' position is a change of 3 spots.",  # no move word: magnitude only
        "The Lakers lost 4 straight but still gained 3 spots.",
    ],
)
def test_a_standings_move_in_the_true_direction_passes(body):
    result = _bound(body)
    assert result.passed, result.offending


@pytest.mark.parametrize(
    "body,word",
    [
        ("The Lakers dropped 3 spots.", "dropped"),  # the old blind-spot row, now caught
        ("The Lakers slid 3 places.", "slid"),
        ("That is a drop of 3 places for the Lakers.", "drop"),
        ("The Lakers sit 2nd after a 3-place fall.", "fall"),
        ("The Lakers are down 3 spots.", "down"),
        ("The Lakers fell 3 positions.", "fell"),
        ("The Lakers lost 3 spots in the standings.", "lost"),
    ],
)
def test_a_standings_move_in_the_wrong_direction_is_a_claim_failure(body, word):
    result = _bound(body)
    assert not result.passed and result.gate == "claims"
    assert result.offending[0].startswith(f"move direction {word!r}"), result.offending


def test_the_move_word_nearest_the_count_decides_its_direction():
    assert not _bound("The Lakers, up to 2nd, went down 3 spots.").passed
    assert _bound("The Lakers had a losing streak yet climbed 3 spots.").passed


WARRIORS_3605 = _story(
    Subject(kind=SubjectKind.TEAM, team_id=13, team_name="Golden State Warriors"),
    [_fact("wins", 27.0), _fact("losses", 34.0), _fact("standings_position", 23.0),
     _fact("streak_length", 2.0), _fact("standings_position_delta", 4.0)],
    StoryKind.TEAM_PROFILE,
)
"""Live story 3605's own facts: +4 means the Warriors FELL from 19th to 23rd."""

TEXT_3605 = (
    "Warriors are 27-34, winners of 2, and up 4 spots to 23rd",
    "The Golden State Warriors are 27-34 and have won 2 straight, which puts them 23rd in the "
    "standings, up 4 places since the last pulse.",
)
"""Verbatim: the live headline and the body's first sentence."""


def test_live_story_3605_up_4_spots_after_a_fall_is_rejected():
    headline, body = TEXT_3605
    assert verify_numbers_bound(WARRIORS_3605, headline, body, team_aliases=ALIASES).passed
    result = verify_claims(WARRIORS_3605, headline, body, team_aliases=ALIASES)
    assert not result.passed
    assert result.offending == (
        "move direction 'up' '4 spots' -> Golden State Warriors; bound to Golden State Warriors",
        "move direction 'up' '4 places' -> Golden State Warriors; bound to Golden State Warriors",
    )


def test_live_story_3605_told_the_right_way_passes():
    body = ("The Golden State Warriors are 27-34 and have won 2 straight. They fell 4 spots, "
            "from 19th to 23rd in the standings.")
    for gate in (verify_numbers_bound, verify_claims):
        result = gate(WARRIORS_3605, "Warriors slide 4 places to 23rd", body,
                      team_aliases=ALIASES)
        assert result.passed, result.offending


def test_bound_blind_spot_a_move_stated_with_no_count_is_not_checked():
    """Only "N spots/places/positions" carries a checked direction: a verb
    with two ordinals and no count ("rose from 19th to 23rd") is not a claim
    class. Asserted so nobody believes it is caught."""
    body = "The Golden State Warriors rose from 19th to 23rd."  # they fell
    for gate in (verify_numbers_bound, verify_claims):
        assert gate(WARRIORS_3605, "H", body, team_aliases=ALIASES).passed
