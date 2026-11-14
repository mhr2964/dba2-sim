"""`dba2/media/detect/scheme.py` -- docs/design/media-v2-newsroom.md MV-2g:
`SCHEME_IDENTITY` (the team's declared scheme pair plus its measured
numbers, league-ranked; `SCHEME_TENSION` is an angle on the SAME kind, not
a second kind) and `TRADE_MARKET_WATCH` (players whose `request_state` is
`TRADE_REQUESTED`/`UNSETTLED` right now, paired with their team's biggest
glut and the league's biggest matching need).

Pure: fed by already-loaded data -- `team_strategy` (declared), `stats_
repo.list_team_style_aggregates`'s own `TeamStyleAggregate` (measured), and
whatever league-rank a future `dba2/services/media_service.py` loader
computes from a whole-league scan of either. This module never computes a
rank itself (ranking the whole league is the loader's own O(teams) scan,
not a per-team pure function's job).

Zero I/O, the same purity rules every other module under `dba2/media/`
carries: no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.media`, `dba2.domain.
media_facts`, `dba2.domain.motivation`, `dba2.domain.phase`, `dba2.domain.
team` and stdlib.
"""

from __future__ import annotations

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import Angle, Fact, FactSource, StoryPackage, Subject, SubjectKind
from dba2.domain.motivation import RequestState
from dba2.domain.phase import Phase
from dba2.domain.team import DefensiveScheme, OffensiveScheme

THREE_POINT_BIAS_TENSION_FLOOR = 70
PACE_TENSION_FLOOR = 65
BOTTOM_THIRD_DIVISOR = 3
"""MV-2g's own `SCHEME_TENSION` condition, verbatim: declared
`three_point_bias >= 70` but measured `three_share` in the bottom third of
the league, OR declared `pace_target >= 65` but measured `avg_possessions`
in the bottom third. "Bottom third" means `rank > 2 * league_size /
BOTTOM_THIRD_DIVISOR` (rank `1` is best, matching this project's other
league-rank conventions)."""


def _is_bottom_third(rank: int, league_size: int) -> bool:
    return rank > (2 * league_size) / BOTTOM_THIRD_DIVISOR


def _int_fact(key: str, label: str, value: int, source: FactSource) -> Fact:
    return Fact(
        key=key, label=label, value_text=str(value), value_number=float(value), source=source
    )


def _float_fact(key: str, label: str, value: float, source: FactSource) -> Fact:
    return Fact(key=key, label=label, value_text=f"{value:.3f}", value_number=value, source=source)


def _text_fact(key: str, label: str, value: str, source: FactSource) -> Fact:
    return Fact(key=key, label=label, value_text=value, value_number=None, source=source)


def _bool_fact(key: str, label: str, value: bool, source: FactSource) -> Fact:
    """`FactType.INT` (matching `_int_fact`'s own `value_number` rendering
    exactly, MV-1c.1 rule 1), but `value_text` renders as `"Yes"`/`"No"`,
    not `_int_fact`'s literal `"1"`/`"0"` -- critic round on commits
    `ea84c5f`..`665526d`, bug B2: this module used to build `set_by_a_
    person` with a bare `_int_fact(..., int(set_by_a_person), ...)` call,
    which published a literal `"0"`/`"1"` with no human-readable meaning
    at all. See `dba2/media/detect/trades.py`'s own identically-fixed
    `_bool_fact` for the full reasoning."""
    return Fact(
        key=key, label=label, value_text=("Yes" if value else "No"),
        value_number=float(int(value)), source=source
    )


def scheme_identity_package(
    team_id: int,
    *,
    offensive_scheme: OffensiveScheme,
    defensive_scheme: DefensiveScheme,
    pace_target: int,
    pace_rank: int,
    avg_possessions: float,
    avg_possessions_rank: int,
    three_point_bias: int,
    three_share: float,
    three_share_rank: int,
    league_size: int,
    set_by_a_person: bool,
    games: int,
    season: int,
    phase: Phase,
    cycle_index: int,
) -> StoryPackage | None:
    """Produces a package for every team that has played at least one game
    this season (MV-2g's own text names no firing threshold for the plain
    `SCHEME_IDENTITY` angle -- salience scoring, the floor and top-K are
    what keep an unremarkable team's scheme from crowding the wire, exactly
    as they do for every other kind; this function's only job beyond the
    gate below is to decide WHICH angle a real package carries).
    `angle` is `SCHEME_TENSION` when the declared-vs-measured disagreement
    holds, else `SCHEME_IDENTITY`.

    **`games` is the absent-measurement gate BLOCKING finding 1 (critic
    round on commits `b288b8e`..`99db9f4`) requires.** Every `*_rank`/
    `three_share`/`avg_possessions` parameter above is a MEASURED value --
    a whole-league scan over `team_game_stats` (MV-2g's own
    `stats_repo.list_team_style_aggregates`) -- and a team with zero rows
    there (PRESEASON, before its first game) has no real measurement to
    report, only whatever a rank-over-zero-rows scan happens to produce.
    `games` is that same aggregate's own `games` column (`TeamStyleAggregate.
    games`, already read by every real caller), so this needs no new read --
    only a check the caller was never asked to make before this fix. `games
    <= 0` returns `None` rather than emitting a measured-looking fact built
    on no real measurement, matching every other detector's own "absent is
    neutral, never a fabricated zero" convention in this codebase (`dba2/
    media/detect/form.py`'s own zero-denominator guard is the closest
    sibling)."""
    if games <= 0:
        return None

    three_tension = (
        three_point_bias >= THREE_POINT_BIAS_TENSION_FLOOR
        and _is_bottom_third(three_share_rank, league_size)
    )
    pace_tension = pace_target >= PACE_TENSION_FLOOR and _is_bottom_third(
        avg_possessions_rank, league_size
    )
    if three_tension:
        angle = Angle.SCHEME_TENSION
        tension = "three_point_bias"
    elif pace_tension:
        angle = Angle.SCHEME_TENSION
        tension = "pace"
    else:
        angle = Angle.SCHEME_IDENTITY
        tension = "none"

    facts = (
        _text_fact("offensive_scheme", "Offensive scheme", offensive_scheme.value,
                    FactSource.TEAM_STRATEGY),
        _text_fact("defensive_scheme", "Defensive scheme", defensive_scheme.value,
                    FactSource.TEAM_STRATEGY),
        _int_fact("pace_target", "Declared pace target", pace_target, FactSource.TEAM_STRATEGY),
        _int_fact("pace_rank", "League pace rank", pace_rank, FactSource.TEAM_GAME_STATS),
        _float_fact("avg_possessions", "Measured possessions/game", avg_possessions,
                     FactSource.TEAM_GAME_STATS),
        _int_fact("avg_possessions_rank", "League possessions rank", avg_possessions_rank,
                   FactSource.TEAM_GAME_STATS),
        _int_fact("three_point_bias", "Declared three-point bias", three_point_bias,
                   FactSource.TEAM_STRATEGY),
        _float_fact("three_share", "Measured three-point rate", three_share,
                     FactSource.TEAM_GAME_STATS),
        _int_fact("three_share_rank", "League three-point rate rank", three_share_rank,
                   FactSource.TEAM_GAME_STATS),
        _bool_fact("set_by_a_person", "A person chose this scheme", set_by_a_person,
                    FactSource.TEAM_STRATEGY),
        _text_fact("tension", "Declared-vs-measured tension", tension, FactSource.TEAM_STRATEGY),
        _int_fact("league_size", "Teams in the league", league_size, FactSource.TEAM_GAME_STATS),
    )
    return StoryPackage(
        story_kind=StoryKind.SCHEME_IDENTITY,
        angle=angle,
        subject=Subject(kind=SubjectKind.TEAM, team_id=team_id),
        facts=facts,
        season=season,
        phase=phase,
        cycle_index=cycle_index,
    )


_WATCHED_REQUEST_STATES = frozenset({RequestState.TRADE_REQUESTED, RequestState.UNSETTLED})


def trade_market_watch_package(
    player_id: int,
    team_id: int,
    *,
    request_state: RequestState,
    team_glut_position: str,
    team_glut_value: float,
    league_biggest_need_value: float,
    season: int,
    phase: Phase,
    cycle_index: int,
) -> StoryPackage | None:
    """MV-2g: fires only for a player whose CURRENT `request_state` is
    `TRADE_REQUESTED` or `UNSETTLED` -- every element is a stored fact, "no
    sources say." `league_biggest_need_value` is the VALUE alone, not the
    specific team that holds it (a disclosed simplification: naming that
    third team would need a second, per-candidate bulk name resolution this
    build step's loader does not build -- the same reasoning `dba2/media/
    detect/trades.py`'s own module docstring already states for a
    different kind)."""
    if request_state not in _WATCHED_REQUEST_STATES:
        return None

    facts = (
        _text_fact("request_state", "Current request state", request_state.value,
                    FactSource.MOTIVATION_EVENTS),
        _text_fact("team_glut_position", "Team's biggest glut", team_glut_position,
                    FactSource.NEED_MATRIX),
        _float_fact("team_glut_value", "Glut value", team_glut_value, FactSource.NEED_MATRIX),
        _float_fact("league_biggest_need_value", "League's biggest need at that position",
                     league_biggest_need_value, FactSource.NEED_MATRIX),
    )
    return StoryPackage(
        story_kind=StoryKind.TRADE_MARKET_WATCH,
        angle=Angle.MARKET_WATCH,
        subject=Subject(kind=SubjectKind.PLAYER, player_id=player_id, team_id=team_id),
        facts=facts,
        season=season,
        phase=phase,
        cycle_index=cycle_index,
    )
