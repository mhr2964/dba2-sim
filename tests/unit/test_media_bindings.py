"""`dba2/domain/media_bindings.py` -- whose number is it
(docs/design/media-v2-step8-gates-v2.md G2-1, G2-2, G2-4, G2-7, G2-12 item 1).

The scope map is asserted TOTAL over the closed key space: a new fact key
with no scope would bind to nothing and false-reject every story of its
kind, so it must fail here instead (the `_PHASE_PLAIN` precedent)."""
from __future__ import annotations

import pytest

from dba2.domain.media import StoryKind
from dba2.domain.media_bindings import (
    LEAGUE,
    ScopeRule,
    TeamAliases,
    build_bindings,
    derived_facts,
    entity_aliases,
    fact_kind,
    fact_scopes,
    rendered_forms,
    scope_rule,
    standings_move,
    standings_move_phrase,
    team_aliases_for,
)
from dba2.domain.media_context import LeagueBrief, SeasonArc, StandingsRow
from dba2.domain.media_facts import (
    FACT_KEYS_BY_KIND,
    Angle,
    Fact,
    FactSource,
    StoryPackage,
    Subject,
    SubjectKind,
)
from dba2.domain.phase import Phase
from dba2.domain.team import GmPhilosophy, Team, Timeline

S = FactSource.PERSISTED


def _f(key, value, text=None, label="Label"):
    return Fact(key, label, text if text is not None else str(value), value, S)


def _pkg(subject, facts, kind=StoryKind.TEAM_PROFILE):
    return StoryPackage(story_kind=kind, angle=Angle.FULL_PICTURE, subject=subject,
                        facts=tuple(facts), season=2026, phase=Phase.REGULAR_SEASON,
                        cycle_index=2)


TEAM_SUBJ = Subject(kind=SubjectKind.TEAM, team_id=2, team_name="Los Angeles Lakers")
MATCHUP = Subject(kind=SubjectKind.MATCHUP, team_id=9, team_name="Los Angeles Clippers",
                  opponent_team_id=8, opponent_team_name="Phoenix Suns")
PLAYER = Subject(kind=SubjectKind.PLAYER, player_id=50, player_name="Anthony Davis",
                 team_id=12, team_name="Dallas Mavericks")

ALL_KEYS = sorted({(kind, key) for kind, keys in FACT_KEYS_BY_KIND.items() for key in keys})


def _brief(rows):
    return LeagueBrief(standings=tuple(rows), other_desks=(), recent_wire=(),
                       season_arc=SeasonArc(season=2026, phase="regular_season",
                                            cycle_index=2, league_games_played=38))


# -- scope/kind map --------------------------------------------------------


@pytest.mark.parametrize("kind,key", ALL_KEYS, ids=str)
def test_every_fact_key_of_every_kind_binds_to_at_least_one_scope(kind, key):
    for subject in (TEAM_SUBJ, MATCHUP, PLAYER):
        scopes = fact_scopes(_pkg(subject, [], kind), key)
        assert scopes and all(scopes)
    assert isinstance(scope_rule(key), ScopeRule)
    assert fact_kind(key) in {"position", "rank", "streak", "delta", "wins", "losses",
                              "series_w", "series_l", "value"}


@pytest.mark.parametrize(
    "key,subject,expected",
    [
        ("season", TEAM_SUBJ, (LEAGUE,)),
        ("league_size", TEAM_SUBJ, (LEAGUE,)),
        ("trade_season", PLAYER, (LEAGUE,)),
        ("closest_margin", MATCHUP, (("team", 9), ("team", 8))),
        ("rivalry_score", MATCHUP, (("team", 9), ("team", 8))),
        ("opponent_standings_position", MATCHUP, (("team", 8),)),
        ("opponent_wins_this_series", MATCHUP, (("team", 8),)),
        ("team_wins_this_series", MATCHUP, (("team", 9),)),
        ("wins", TEAM_SUBJ, (("team", 2),)),
        ("window_points_per36", PLAYER, (("player", 50),)),
        ("team_standings_position", PLAYER, (("player", 50), ("team", 12))),
    ],
)
def test_scope_table_rows(key, subject, expected):
    assert fact_scopes(_pkg(subject, []), key) == expected


@pytest.mark.parametrize(
    "key,kind",
    [("standings_position", "position"), ("team_standings_position", "position"),
     ("prior_standings_position", "position"), ("pace_rank", "rank"),
     ("streak_length", "streak"), ("standings_position_delta", "delta"),
     ("wins", "wins"), ("prior_wins", "wins"), ("losses", "losses"),
     ("team_wins_this_series", "series_w"), ("opponent_wins_this_series", "series_l"),
     ("biggest_need_position", "value"), ("mean_age_top8", "value")],
)
def test_kind_is_derived_from_the_key(key, kind):
    assert fact_kind(key) == kind


def test_no_fact_key_states_games_remaining():
    """Claim class C1 rejects "N games left" because no key of this class
    exists. If one is ever added, C1 must bind to it instead."""
    for keys in FACT_KEYS_BY_KIND.values():
        for key in keys:
            assert "remaining" not in key and "games_left" not in key


def test_negative_numbers_also_render_as_their_magnitude():
    assert {"-4", "4", "4.0", "4.00", "4.000"} <= rendered_forms(-4.0)
    assert "9" in rendered_forms(-8.964) and "8.964" in rendered_forms(-8.964)
    assert "3" not in rendered_forms(4.0)


# -- derived facts ---------------------------------------------------------


def test_derived_scoring_and_efficiency_changes_are_signed_and_rendered():
    pkg = _pkg(PLAYER, [_f("window_points_per36", 20.216), _f("baseline_points_per36", 29.180),
                        _f("window_true_shooting", 0.517), _f("baseline_true_shooting", 0.644)],
               StoryKind.COLD_STRETCH)
    by_key = {d.key: d for d in derived_facts(pkg, None)}
    assert by_key["points_per36_delta"].value_text == "-8.964"
    assert by_key["true_shooting_delta"].value_text == "-0.127"
    assert by_key["points_per36_delta"].about == ("player", 50)
    assert by_key["points_per36_delta"].to_prompt_json() == {
        "key": "points_per36_delta", "label": "Points per 36, last 5 minus rest of season",
        "value_text": "-8.964", "about": {"player_id": 50},
    }


def test_derived_series_games_played_is_about_both_teams():
    pkg = _pkg(MATCHUP, [_f("team_wins_this_series", 2.0), _f("opponent_wins_this_series", 1.0)],
               StoryKind.RIVALRY_MATCHUP)
    series = [d for d in derived_facts(pkg, None) if d.key == "series_games_played"]
    assert {d.about for d in series} == {("team", 9), ("team", 8)}
    assert {d.value_text for d in series} == {"3"}


def test_derived_standings_values_and_the_zero_games_omission():
    brief = _brief([StandingsRow(1, "Minnesota Timberwolves", 25, 12, 1, 6),
                    StandingsRow(4, "Denver Nuggets", 24, 13, 4, 2),
                    StandingsRow(10, "Indiana Pacers", 0, 0, 9, 0)])
    derived = derived_facts(_pkg(TEAM_SUBJ, []), brief)
    got = {(d.key, d.about): d.value_text for d in derived}
    assert got[("games_played", ("team", 1))] == "37"
    assert got[("win_pct", ("team", 4))] == "0.649"
    assert got[("games_behind", ("team", 4))] == "1.0"
    assert ("games_behind", ("team", 1)) not in got  # the leader is behind nobody
    assert ("win_pct", ("team", 10)) not in got  # never a fabricated 0.000


def test_games_behind_is_bound_to_the_leader_as_well_as_the_row_team():
    brief = _brief([StandingsRow(1, "Minnesota Timberwolves", 25, 12, 1, 6),
                    StandingsRow(4, "Denver Nuggets", 20, 17, 4, 2)])
    b = build_bindings(_pkg(TEAM_SUBJ, []), brief)
    assert _scopes(b, "5.0") == {("team", 4), ("team", 1)}  # (13 - 3) / 2
    behind = next(d for d in derived_facts(_pkg(TEAM_SUBJ, []), brief) if d.key == "games_behind")
    assert behind.counterpart == ("team", 1)
    assert "counterpart" not in behind.to_prompt_json()  # the payload is unchanged


@pytest.mark.parametrize(
    "position,delta,phrase",
    [
        (23.0, 4.0, "fell 4 spots, from 19th to 23rd"),  # live story 3605: +4 is a FALL
        (13.0, -9.0, "rose 9 spots, from 22nd to 13th"),  # live story 3597
        (30.0, 1.0, "fell 1 spot, from 29th to 30th"),  # singular
        (11.0, -1.0, "rose 1 spot, from 12th to 11th"),  # 11th/12th/13th take "th"
        (1.0, -20.0, "rose 20 spots, from 21st to 1st"),
        (23.0, 0.0, "held at 23rd"),
    ],
)
def test_the_standings_move_states_its_direction_in_words(position, delta, phrase):
    pkg = _pkg(TEAM_SUBJ, [_f("standings_position", position),
                           _f("standings_position_delta", delta)])
    move = next(d for d in derived_facts(pkg, None) if d.key == "standings_move")
    assert move.value_text == phrase
    assert move.value_number == abs(delta)
    assert move.about == ("team", 2)
    assert move.to_prompt_json()["label"] == "Standings move since last pulse"


def test_no_standings_move_without_both_facts():
    assert standings_move(_pkg(TEAM_SUBJ, [_f("standings_position", 3.0)])) is None
    assert standings_move(_pkg(TEAM_SUBJ, [_f("standings_position_delta", 3.0)])) is None
    assert standings_move_phrase(5, 5) == "held at 5th"


def test_the_prior_pulse_position_binds_as_a_position_but_not_a_current_one():
    """The move phrase's "from 19th" must pass the ordinal check (a position
    kind), but the team does not hold 19th now (C5 reads current positions)."""
    pkg = _pkg(TEAM_SUBJ, [_f("standings_position", 23.0), _f("standings_position_delta", 4.0)])
    b = build_bindings(pkg, None)
    assert (("team", 2), "position") in b.scopes_for("19")
    assert b.positions[("team", 2)] == {23}
    without = build_bindings(pkg, None, derived=False)
    assert (("team", 2), "position") not in without.scopes_for("19")


def test_derived_facts_are_empty_when_nothing_derives():
    assert derived_facts(_pkg(TEAM_SUBJ, [_f("standings_position", 3.0)]), None) == ()


# -- bindings --------------------------------------------------------------


def _scopes(b, token):
    return {scope for scope, _ in b.scopes_for(token)}


def test_a_derived_number_binds_only_to_its_entity_and_only_with_derived_on():
    pkg = _pkg(PLAYER, [_f("window_points_per36", 20.216), _f("baseline_points_per36", 29.180)],
               StoryKind.COLD_STRETCH)
    assert _scopes(build_bindings(pkg, None), "9") == {("player", 50)}
    assert _scopes(build_bindings(pkg, None, derived=False), "9") == set()
    assert _scopes(build_bindings(pkg, None), "7") == set()  # nothing else is widened


def test_brief_rows_bind_their_numbers_and_record_pair_to_their_team():
    brief = _brief([StandingsRow(4, "Denver Nuggets", 24, 13, 4, -2)])
    b = build_bindings(_pkg(TEAM_SUBJ, []), brief)
    assert ("team", 4) in _scopes(b, "13")
    assert b.pairs[("team", 4)] == {(24, 13)}
    assert b.streaks[("team", 4)] == {-2}
    assert b.positions[("team", 4)] == {4}
    assert LEAGUE in _scopes(b, "38")


def test_series_pairs_bind_from_each_side():
    pkg = _pkg(MATCHUP, [_f("team_wins_this_series", 2.0), _f("opponent_wins_this_series", 1.0)],
               StoryKind.RIVALRY_MATCHUP)
    b = build_bindings(pkg, None)
    assert b.pairs[("team", 9)] == {(2, 1)} and b.pairs[("team", 8)] == {(1, 2)}


def test_last_seasons_position_is_typed_but_not_current():
    pkg = _pkg(TEAM_SUBJ, [_f("prior_standings_position", 3.0), _f("standings_position", 10.0)])
    b = build_bindings(pkg, None)
    assert (("team", 2), "position") in b.scopes_for("3")
    assert b.positions[("team", 2)] == {10}


# -- aliases ---------------------------------------------------------------


def _team(tid, city, name):
    return Team(id=tid, league_id=1, code=name[:3].upper(), name=name, city=city,
                conference="West", division="Pacific", manager_user_id=None,
                gm_philosophy=list(GmPhilosophy)[0], timeline=list(Timeline)[0],
                home_market_size=1)


def test_team_aliases_come_from_team_rows():
    aliases = team_aliases_for([_team(2, "Los Angeles", "Lakers")])
    assert aliases == {2: TeamAliases("Los Angeles Lakers", "Los Angeles", "Lakers")}


def test_entity_aliases_cover_teams_shared_cities_players_and_named_players():
    aliases = team_aliases_for([_team(2, "Los Angeles", "Lakers"),
                                _team(9, "Los Angeles", "Clippers"), _team(8, "Phoenix", "Suns")])
    pkg = _pkg(MATCHUP, [_f("team_named_players", None, "Kawhi Leonard, James Harden")],
               StoryKind.RIVALRY_MATCHUP)
    table = entity_aliases(pkg, None, aliases)
    assert ("Los Angeles", ("team", 2)) in table and ("Los Angeles", ("team", 9)) in table
    assert ("Suns", ("team", 8)) in table and ("Phoenix Suns", ("team", 8)) in table
    assert ("Leonard", ("team", 9)) in table and ("Kawhi Leonard", ("team", 9)) in table
    assert all(alias != "Kawhi" for alias, _ in table)  # first names are not aliases
    player_table = entity_aliases(_pkg(PLAYER, []), None, aliases)
    assert ("Davis", ("player", 50)) in player_table
    assert ("Anthony", ("player", 50)) not in player_table
