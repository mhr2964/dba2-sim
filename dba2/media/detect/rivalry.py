"""`dba2/media/detect/rivalry.py` -- docs/design/media-v2-newsroom.md MV-2f:
`RIVALRY_MATCHUP`, a `RivalryScore` computed fresh each cycle from data that
already exists (a loader's own `games`/`teams`/`league_transactions`/
`gm_head_to_head` reads) -- zero new tables, zero new columns
(MV-2f's own "fully emergent, recomputed per cycle" decision).

Pure: every signal below is a plain value the caller (a future `dba2/
services/media_service.py` loader) has already computed from real rows;
this module owns only the scoring arithmetic and the firing/package rules.

Zero I/O, the same purity rules every other module under `dba2/media/`
carries: no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.media`, `dba2.domain.
media_facts`, `dba2.domain.phase` and stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import Angle, Fact, FactSource, StoryPackage, Subject, SubjectKind
from dba2.domain.phase import Phase

RIVALRY_THRESHOLD = 4
CLOSE_GAME_MARGIN = 5
CLOSE_GAME_POINTS_CAP = 3
SAME_DIVISION_POINTS = 2
SEASON_SERIES_LEVEL_MAX_GAP = 1
SEASON_SERIES_LEVEL_MIN_COMPLETED = 2
H2H_REJECTS_FLOOR = 3
H2H_FAR_REJECT_STREAK_FLOOR = 2
"""MV-2f's own scoring table, verbatim -- disclosed, not calibrated, same
status as every other threshold in this design."""


@dataclass(frozen=True)
class RivalrySignals:
    """Every signal MV-2f's scoring table names, already computed by the
    caller from real rows -- this dataclass carries no logic of its own,
    only the inputs `score` reads. `close_game_count` is NOT pre-capped
    (`score` applies MV-2f's own `+1 each, capped +3` rule); a caller
    should pass the real count of games decided by `<= 5` points this
    season or last, not an already-clamped one."""

    same_division: bool
    close_game_count: int
    season_series_level: bool
    recent_trade: bool
    h2h_signal: bool


def score(signals: RivalrySignals) -> int:
    """MV-2f's scoring table -- `+2` same division, `+1` per close game
    (capped `+3`), `+1` a level-or-within-one-game season series, `+1` a
    recent trade, `+1` an h2h reject signal. Integer arithmetic throughout
    (every term is a fixed integer weight), so the result is exact, never a
    rounding artifact."""
    total = 0
    if signals.same_division:
        total += SAME_DIVISION_POINTS
    total += min(signals.close_game_count, CLOSE_GAME_POINTS_CAP)
    if signals.season_series_level:
        total += 1
    if signals.recent_trade:
        total += 1
    if signals.h2h_signal:
        total += 1
    return total


def season_series_is_level(*, completed: int, wins_a: int, wins_b: int) -> bool:
    """MV-2f's revision-4 fix: well-defined for a 2-, 3- or 4-game series
    (the league's real four series lengths, `schedule_service.py`) --
    `completed >= 2` AND `abs(wins_a - wins_b) <= 1`, never the undefined
    "2-2 or tighter" revision 1 wrote."""
    return completed >= SEASON_SERIES_LEVEL_MIN_COMPLETED and abs(wins_a - wins_b) <= (
        SEASON_SERIES_LEVEL_MAX_GAP
    )


def _int_fact(key: str, label: str, value: int, source: FactSource) -> Fact:
    return Fact(
        key=key, label=label, value_text=str(value), value_number=float(value), source=source
    )


def _text_fact(key: str, label: str, value: str, source: FactSource) -> Fact:
    return Fact(key=key, label=label, value_text=value, value_number=None, source=source)


def rivalry_matchup_package(
    team_id: int,
    opponent_team_id: int,
    signals: RivalrySignals,
    *,
    has_upcoming_scheduled_game: bool,
    team_wins_this_series: int,
    opponent_wins_this_series: int,
    closest_margin: int,
    team_standings_position: int,
    opponent_standings_position: int,
    scheduled_game_date: str,
    season: int,
    phase: Phase,
    cycle_index: int,
    team_named_players: tuple[str, ...] = (),
    opponent_named_players: tuple[str, ...] = (),
) -> StoryPackage | None:
    """`RIVALRY_MATCHUP` fires only when `score(signals) >= RIVALRY_
    THRESHOLD` AND `has_upcoming_scheduled_game` -- MV-2f's own two-part
    gate: a real rivalry score is not enough by itself, because without a
    scheduled game this is trivia, not a preview. Neither condition alone
    fires: a score of 8 with no scheduled game returns `None`, a scheduled
    game between two teams that scored 3 also returns `None`.

    **`team_named_players`/`opponent_named_players` (docs/design/
    media-v2-newsroom.md MV-2f, `:1159-1161`) -- the named-actor fact
    group's TOP-2-BY-`overall_cached` half, real and wired.** Each is
    already ranked and named by the caller (a future `dba2/services/
    media_service.py` loader's own whole-roster `overall_cached` scan --
    this module stays pure, fed by loaders, like every other detector
    here); this function's only job is turning an already-ranked name
    tuple into a fact. Absent is neutral: an empty tuple emits no fact for
    that key, never a placeholder name.

    **Disclosed, deliberately NOT implemented in this build step: the other
    half of MV-2f's same named-actor fact group** -- "the shared players
    (anyone on team A today who `league_transaction_assets` says came from
    team B)" and "... or any player with a trade history between the two
    teams." Both need a real `league_transaction_assets` query keyed on
    BOTH team ids at once, which is a loader's job this build step does not
    build (the same "detectors, not loaders" scope every other MV-2
    detector in this step states at its own module's top). Naming this
    gap here rather than silently shipping two-thirds of one fact group as
    if it were the whole thing -- the same shape MV-2a's `trades.py` and
    MV-2g's `scheme.py` already disclose for their own THIRD-PARTY-naming
    simplifications."""
    if not has_upcoming_scheduled_game or score(signals) < RIVALRY_THRESHOLD:
        return None

    facts = [
        _int_fact("rivalry_score", "Rivalry score", score(signals), FactSource.GAMES),
        _int_fact("team_wins_this_series", "Season series wins", team_wins_this_series,
                   FactSource.GAMES),
        _int_fact("opponent_wins_this_series", "Season series losses", opponent_wins_this_series,
                   FactSource.GAMES),
        _int_fact("closest_margin", "Closest margin so far", closest_margin, FactSource.GAMES),
        _int_fact("team_standings_position", "Standings position", team_standings_position,
                   FactSource.PULSE_SNAPSHOT),
        _int_fact("opponent_standings_position", "Opponent standings position",
                   opponent_standings_position, FactSource.PULSE_SNAPSHOT),
        _text_fact("scheduled_game_date", "Next meeting", scheduled_game_date, FactSource.GAMES),
    ]
    if team_named_players:
        facts.append(
            _text_fact("team_named_players", "Team's headline names",
                       ", ".join(team_named_players), FactSource.PLAYERS)
        )
    if opponent_named_players:
        facts.append(
            _text_fact("opponent_named_players", "Opponent's headline names",
                       ", ".join(opponent_named_players), FactSource.PLAYERS)
        )
    return StoryPackage(
        story_kind=StoryKind.RIVALRY_MATCHUP,
        angle=Angle.RIVALRY,
        subject=Subject(
            kind=SubjectKind.MATCHUP, team_id=team_id, opponent_team_id=opponent_team_id
        ),
        facts=tuple(facts),
        season=season,
        phase=phase,
        cycle_index=cycle_index,
    )
