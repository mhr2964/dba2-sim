"""`dba2/media/detect/profile.py` -- docs/design/media-v2-newsroom.md MV-2h:
`TEAM_PROFILE`, the one composer among MV-2's eleven new kinds (built LAST
in this build step -- it composes the other detectors'/loaders' own
outputs, so it cannot exist before they do). Not a detector in the "did
something happen?" sense; it answers "pick a team and tell me everything
true about it right now," per the commissioner's own brief.

Pure: every term below is a plain value a future `dba2/services/
media_service.py` loader has already computed from data every other part
of this design already loads (`needs.build_need_matrix`, `media_pulse_
snapshot`, `league_transactions`, `player_motivation`, `dba2/media/detect/
rivalry.py::score`) -- this module adds no new read of its own beyond what
MV-2h's own text names (`list_profiled_team_ids`, `get_final_pulse_
snapshot_for_season`, both already built in an earlier build step).

Zero I/O, the same purity rules every other module under `dba2/media/`
carries: no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.media`, `dba2.domain.
media_facts`, `dba2.domain.phase` and stdlib.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import Angle, Fact, FactSource, StoryPackage, Subject, SubjectKind
from dba2.domain.phase import Phase

MIN_FACTS_TO_PUBLISH = 6
"""MV-2h.2's own minimum bar: a team that cannot clear six facts is
skipped and its quota slot goes unused -- never backfilled with a thinner
piece (MV-4's amendment)."""

_ROSTER_EXTREMITY_WEIGHT = 0.20
_STREAK_WEIGHT = 0.20
_STANDINGS_MOVEMENT_WEIGHT = 0.15
_TRADE_ACTIVITY_WEIGHT = 0.15
_MOTIVATION_DRAMA_WEIGHT = 0.15
_RIVALRY_HEAT_WEIGHT = 0.15
"""MV-2h.2's own six weights, verbatim -- sum to exactly `1.00` so
`profile_interest` is `[0, 1]` by construction (asserted by a pure unit
test over the constants themselves, the same exhaustiveness shape this
design uses everywhere else a weight vector must sum to `1.0`)."""

_STREAK_ANCHOR = 8.0
_STANDINGS_MOVEMENT_ANCHOR = 15.0
_TRADE_ACTIVITY_ANCHOR = 3.0
_MOTIVATION_DRAMA_ANCHOR = 3.0

CARRY_OVER_MULTIPLIER = 1.5
HUMAN_MANAGED_MULTIPLIER = 1.25
"""MV-2h.2's own two multipliers, verbatim -- applied to `profile_interest`
to produce `profile_priority`, which ranks the eligible pool and is used
for nothing else (it is not `[0, 1]`, and never reaches `salience`)."""


@dataclass(frozen=True)
class ProfileTerms:
    """The six raw inputs `profile_interest` reads -- already computed by
    the caller from data every other part of this design already loads
    (MV-2h.2's own "Source" column). `standings_movement` is `None` unless
    BOTH the current and prior pulse have `games_played > 0` (MV-2h.2's own
    revision-4 gate); `None` reads as `0.0`, never a fabricated movement."""

    roster_extremity: float
    streak_length: int
    standings_movement: float | None
    trades_this_season: int
    players_not_content: int
    rivalry_heat: bool


def profile_interest(terms: ProfileTerms) -> float:
    """MV-2h.2's six-term weighted sum, `[0, 1]` by construction. This is
    the ONLY value `_magnitude`'s `TEAM_PROFILE` branch may read (MV-2h.2's
    own "the value `_magnitude` reads, and the only thing it is used
    for") -- `profile_priority` below is a DIFFERENT value, for a different
    purpose, and must never be substituted here."""
    streak_term = min(abs(terms.streak_length) / _STREAK_ANCHOR, 1.0)
    movement_term = (
        0.0
        if terms.standings_movement is None
        else min(terms.standings_movement, 1.0)
    )
    trade_term = min(terms.trades_this_season / _TRADE_ACTIVITY_ANCHOR, 1.0)
    drama_term = min(terms.players_not_content / _MOTIVATION_DRAMA_ANCHOR, 1.0)
    rivalry_term = 1.0 if terms.rivalry_heat else 0.0
    roster_term = max(0.0, min(terms.roster_extremity, 1.0))

    total = (
        _ROSTER_EXTREMITY_WEIGHT * roster_term
        + _STREAK_WEIGHT * streak_term
        + _STANDINGS_MOVEMENT_WEIGHT * movement_term
        + _TRADE_ACTIVITY_WEIGHT * trade_term
        + _MOTIVATION_DRAMA_WEIGHT * drama_term
        + _RIVALRY_HEAT_WEIGHT * rivalry_term
    )
    return max(0.0, min(total, 1.0))


def standings_movement_term(
    *, position_now: int, position_at_last_pulse: int, games_played_now: int,
    games_played_at_last_pulse: int,
) -> float | None:
    """MV-2h.2's revision-4 movement term: `min(abs(position_now -
    position_at_last_pulse) / 15, 1.0)`, and `None` unless BOTH endpoints
    have `games_played > 0` -- never measured from a zero-game `PRESEASON`
    pulse, which `pulse.py`'s own documented tiebreak artifact would make a
    fabricated "climbed N spots" claim (MV-2h.2's own full reasoning)."""
    if games_played_now <= 0 or games_played_at_last_pulse <= 0:
        return None
    return min(abs(position_now - position_at_last_pulse) / _STANDINGS_MOVEMENT_ANCHOR, 1.0)


def profile_priority(
    interest: float, *, not_profiled_last_season: bool, human_managed: bool
) -> float:
    """`profile_interest x carry_over x human_managed`, range `[0, 1.875]`
    -- ranks the eligible pool and is used for NOTHING else (MV-2h.2's own
    "these three statements cannot all be true" correction: this value is
    NOT `[0, 1]` and must never be handed to `salience`)."""
    priority = interest
    if not_profiled_last_season:
        priority *= CARRY_OVER_MULTIPLIER
    if human_managed:
        priority *= HUMAN_MANAGED_MULTIPLIER
    return priority


@dataclass(frozen=True)
class ProfileCandidate:
    team_id: int
    priority: float


def select_teams_to_profile(
    candidates: Sequence[ProfileCandidate], *, quota: int
) -> tuple[int, ...]:
    """MV-2h.2's own eligible-pool ranking: highest `profile_priority`
    first, `team_id` ASCENDING as the deterministic tiebreak (never an RNG
    draw -- "same league, same cycle, same inputs -> same output").
    `candidates` is the caller's own ALREADY-FILTERED eligible pool (every
    team NOT in `list_profiled_team_ids` for this season); this function
    does no filtering of its own. Returns at most `quota` team ids -- fewer
    when `candidates` itself has fewer than `quota` entries (MV-2h.2: "no
    repeat used to fill the third slot"), and `()` when `candidates` is
    empty (the pool-empty branch, MV-2h.2's own "stop publishing profiles
    for the rest of that season")."""
    ranked = sorted(candidates, key=lambda c: (-c.priority, c.team_id))
    return tuple(c.team_id for c in ranked[:quota])


def _text_fact(key: str, label: str, value: str, source: FactSource) -> Fact:
    return Fact(key=key, label=label, value_text=value, value_number=None, source=source)


def _int_fact(key: str, label: str, value: int, source: FactSource) -> Fact:
    return Fact(
        key=key, label=label, value_text=str(value), value_number=float(value), source=source
    )


def _float_fact(key: str, label: str, value: float, source: FactSource) -> Fact:
    return Fact(key=key, label=label, value_text=f"{value:.3f}", value_number=value, source=source)


def _bool_fact(key: str, label: str, value: bool, source: FactSource) -> Fact:
    """`FactType.INT` (matching `_int_fact`'s own `value_number` rendering
    exactly, MV-1c.1 rule 1), but `value_text` renders as `"Yes"`/`"No"`,
    not `_int_fact`'s literal `"1"`/`"0"` -- critic round on commits
    `ea84c5f`..`665526d`, bug B2: `_build_fact_box` (`dba2/media/write/
    template.py`) publishes `value_text` verbatim with no type awareness,
    so a real `set_by_a_person`/`next_game_is_rivalry` fact used to reach
    published output as a bare digit. See `dba2/media/detect/trades.py`'s
    own identically-fixed `_bool_fact` for the full reasoning; this module
    duplicates the helper (this codebase's own established per-detector-
    module convention for `_int_fact`/`_text_fact`/`_float_fact` already),
    not the fix's reasoning."""
    return Fact(
        key=key, label=label, value_text=("Yes" if value else "No"),
        value_number=float(int(value)), source=source
    )


def team_profile_package(  # noqa: PLR0913 -- a composer over MV-2h.4's own four-plus-one sections
    team_id: int,
    *,
    wins: int,
    losses: int,
    standings_position: int,
    streak_length: int,
    standings_position_delta: int | None,
    mean_age_top8: float | None,
    biggest_need_position: str | None,
    biggest_need_value: float | None,
    biggest_glut_position: str | None,
    biggest_glut_value: float | None,
    offensive_scheme: str | None,
    defensive_scheme: str | None,
    three_point_bias: int | None,
    three_share: float | None,
    pace_target: int | None,
    avg_possessions: float | None,
    set_by_a_person: bool | None,
    scheme_tension: str | None,
    timeline: str | None,
    gm_philosophy: str | None,
    trades_in_count: int | None,
    trades_out_count: int | None,
    unrest_player_count: int | None,
    next_game_date: str | None,
    next_game_is_rivalry: bool | None,
    prior_wins: int | None,
    prior_losses: int | None,
    prior_standings_position: int | None,
    season: int,
    phase: Phase,
    cycle_index: int,
) -> StoryPackage | None:
    """MV-2h.4's four named sections, plus one conditional fifth. Every
    parameter is `None`-able and every `None` emits no fact for that key
    (absent is neutral, never a fabricated zero) -- "section 5" (last
    year) is entirely conditional on the caller passing real prior-season
    values; a team with none simply contributes no fact from that group,
    same as any other absent reading. Returns `None` if fewer than
    `MIN_FACTS_TO_PUBLISH` facts survive (MV-2h.2's own minimum bar) --
    `wins`/`losses`/`standings_position`/`streak_length` are the only
    unconditionally-real facts (a real pulse snapshot always has them), so
    a team needs at least two more real readings from anywhere else in the
    package to clear the bar."""
    facts: list[Fact] = [
        _int_fact("wins", "Wins", wins, FactSource.PULSE_SNAPSHOT),
        _int_fact("losses", "Losses", losses, FactSource.PULSE_SNAPSHOT),
        _int_fact("standings_position", "Standings position", standings_position,
                   FactSource.PULSE_SNAPSHOT),
        _int_fact("streak_length", "Streak", streak_length, FactSource.PULSE_SNAPSHOT),
    ]
    if standings_position_delta is not None:
        facts.append(
            _int_fact("standings_position_delta", "Change since last pulse",
                       standings_position_delta, FactSource.PULSE_SNAPSHOT)
        )
    if mean_age_top8 is not None:
        facts.append(
            _float_fact("mean_age_top8", "Mean age, top 8", mean_age_top8,
                         FactSource.PLAYER_RATINGS)
        )
    if biggest_need_position is not None and biggest_need_value is not None:
        facts.append(
            _text_fact("biggest_need_position", "Biggest need", biggest_need_position,
                        FactSource.NEED_MATRIX)
        )
        facts.append(
            _float_fact("biggest_need_value", "Need value", biggest_need_value,
                         FactSource.NEED_MATRIX)
        )
    if biggest_glut_position is not None and biggest_glut_value is not None:
        facts.append(
            _text_fact("biggest_glut_position", "Biggest glut", biggest_glut_position,
                        FactSource.NEED_MATRIX)
        )
        facts.append(
            _float_fact("biggest_glut_value", "Glut value", biggest_glut_value,
                         FactSource.NEED_MATRIX)
        )
    if offensive_scheme is not None:
        facts.append(
            _text_fact("offensive_scheme", "Offensive scheme", offensive_scheme,
                        FactSource.TEAM_STRATEGY)
        )
    if defensive_scheme is not None:
        facts.append(
            _text_fact("defensive_scheme", "Defensive scheme", defensive_scheme,
                        FactSource.TEAM_STRATEGY)
        )
    if three_point_bias is not None:
        facts.append(
            _int_fact("three_point_bias", "Declared three-point bias", three_point_bias,
                       FactSource.TEAM_STRATEGY)
        )
    if three_share is not None:
        facts.append(
            _float_fact("three_share", "Measured three-point rate", three_share,
                         FactSource.TEAM_GAME_STATS)
        )
    if pace_target is not None:
        facts.append(
            _int_fact("pace_target", "Declared pace target", pace_target,
                       FactSource.TEAM_STRATEGY)
        )
    if avg_possessions is not None:
        facts.append(
            _float_fact("avg_possessions", "Measured possessions/game", avg_possessions,
                         FactSource.TEAM_GAME_STATS)
        )
    if set_by_a_person is not None:
        facts.append(
            _bool_fact("set_by_a_person", "A person chose this scheme", set_by_a_person,
                        FactSource.TEAM_STRATEGY)
        )
    if scheme_tension is not None:
        facts.append(
            _text_fact("scheme_tension", "Declared-vs-measured tension", scheme_tension,
                        FactSource.TEAM_STRATEGY)
        )
    if timeline is not None:
        facts.append(_text_fact("timeline", "Competitive timeline", timeline, FactSource.TEAMS))
    if gm_philosophy is not None:
        facts.append(_text_fact("gm_philosophy", "GM philosophy", gm_philosophy, FactSource.TEAMS))
    if trades_in_count is not None:
        facts.append(
            _int_fact("trades_in_count", "Trades made this season", trades_in_count,
                       FactSource.LEAGUE_TRANSACTIONS)
        )
    if trades_out_count is not None:
        facts.append(
            _int_fact("trades_out_count", "Assets sent out this season", trades_out_count,
                       FactSource.LEAGUE_TRANSACTIONS)
        )
    if unrest_player_count is not None:
        facts.append(
            _int_fact("unrest_player_count", "Players not settled", unrest_player_count,
                       FactSource.MOTIVATION_EVENTS)
        )
    if next_game_date is not None:
        facts.append(
            _text_fact("next_game_date", "Next scheduled game", next_game_date, FactSource.GAMES)
        )
    if next_game_is_rivalry is not None:
        facts.append(
            _bool_fact("next_game_is_rivalry", "That game is a real rivalry", next_game_is_rivalry,
                        FactSource.GAMES)
        )
    has_prior_season = (
        prior_wins is not None
        and prior_losses is not None
        and prior_standings_position is not None
    )
    if has_prior_season:
        facts.append(
            _int_fact("prior_wins", "Wins, last season", prior_wins, FactSource.PULSE_SNAPSHOT)
        )
        facts.append(
            _int_fact(
                "prior_losses", "Losses, last season", prior_losses, FactSource.PULSE_SNAPSHOT
            )
        )
        facts.append(
            _int_fact("prior_standings_position", "Standings position, last season",
                       prior_standings_position, FactSource.PULSE_SNAPSHOT)
        )

    non_identity_facts = [f for f in facts if f.key not in ("team_name",)]
    if len(non_identity_facts) < MIN_FACTS_TO_PUBLISH:
        return None

    return StoryPackage(
        story_kind=StoryKind.TEAM_PROFILE,
        angle=Angle.FULL_PICTURE,
        subject=Subject(kind=SubjectKind.TEAM, team_id=team_id),
        facts=tuple(facts),
        season=season,
        phase=phase,
        cycle_index=cycle_index,
    )
