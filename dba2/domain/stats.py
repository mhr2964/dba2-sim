"""player_game_stats, team_game_stats, player_season_stats — `stats_repo`'s
tables (§5: "season/career aggregation").

phase-0-foundation.md §4.7. The three box-score tables share the same
zone-FG-plus-counting-stat column set (`_BoxScoreColumns`, mirroring
migration 006's shared `_ZONE_FG_COLUMNS` SQL fragment); each table's own
dataclass adds only the columns that differ (`minutes_played`/`plus_minus`
for a single game's player line, `possessions` for a team line,
`games_played`/`game_type` for the materialized season aggregate).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from dba2.domain.game import GameType


@dataclass
class _BoxScoreColumns:
    """The zone field-goal split plus standard counting stats. No separate
    total `fgm`/`fga` or `fg3m`/`fg3a` — a made three is definitionally a
    `_three` zone shot; totals are summed across zones at read time (§4.7)."""

    points: int
    fga_rim: int
    fgm_rim: int
    fga_mid: int
    fgm_mid: int
    fga_three: int
    fgm_three: int
    fta: int
    ftm: int
    oreb: int
    dreb: int
    ast: int
    stl: int
    blk: int
    tov: int
    pf: int


@dataclass
class PlayerGameStats(_BoxScoreColumns):
    """PK (game_id, player_id). `league_id` is denormalized here per the
    blanket "every league-scoped table carries `league_id`" convention
    (§4.7 — resolved after round-2 critic review to apply that rule
    consistently rather than carve out an exception for this table)."""

    game_id: int
    league_id: int
    player_id: int
    team_id: int
    minutes_played: Decimal
    plus_minus: int


@dataclass
class TeamGameStats(_BoxScoreColumns):
    """PK (game_id, team_id). `possessions` is the pace-validation input."""

    game_id: int
    league_id: int
    team_id: int
    possessions: int


@dataclass
class PlayerSeasonStats(_BoxScoreColumns):
    """Materialized aggregate, PK (league_id, player_id, season, game_type)
    — rebuilt wholesale by `stats_service`, not aggregated on read, because
    leaderboard/award-vote/trade-evaluation queries hit this constantly
    (§4.7)."""

    league_id: int
    player_id: int
    season: int
    game_type: GameType
    games_played: int
    minutes_played: Decimal
    updated_at: datetime


@dataclass(frozen=True)
class PlayerCareerScoringToDate:
    """One player's regular-season career scoring split at a point in time,
    as of some season `N` — `stats_repo.get_career_scoring_to_date_for_
    league`'s own return shape (Decision P4-121 item 1, phase-4-media-and-
    player-agency.md §39). Three MEASURED fields, not four: `career_points`
    is `career_points_before_season + points_in_season`, computed in the
    pure layer that consumes this (`dba2.media.events.career_scoring_
    milestone_candidates`), never shipped here as a derived quantity a
    caller could disagree with — the same "repository returns measured
    values, pure layer derives" split `_BoxScoreColumns`'s own zone-split
    totals already follow (§4.7).

    `player_id` identifies the subject; `career_points_before_season` is
    the `SUM(points)` for every season strictly before `season` (`0` for a
    player with no prior rows, never `NULL`); `points_in_season`/`games_
    played_in_season` are `season`'s own totals (`0` for a player with no
    row in that season, e.g. injured all year or not yet in the league).
    Frozen: this is a read-only fact from the database, not a value a
    caller mutates in place."""

    player_id: int
    career_points_before_season: int
    points_in_season: int
    games_played_in_season: int


class LeaderMode(StrEnum):
    """docs/design/leaders-v2-per-game-and-paging.md LD2-1/LD2-3: which ORDER BY
    `stats_repo._LEADER_ORDER_EXPRESSIONS` picks, and whether the 70% qualification
    bar applies (`PER_GAME` only, LD2-2) -- the two ways `/leaders` v2 can render the
    same underlying `totals` CTE. A closed set, like `LeaderStat` above: an unknown
    value is a `ValidationError` at the repository boundary, never a caller string."""

    PER_GAME = "pergame"
    TOTAL = "total"


class LeaderStat(StrEnum):
    """Phase 5 Stage B step 8's `/leaders` vocabulary -- docs/design/
    phase-5-discord-presentation.md §7.1's `/leaders <stat>` row ("one new
    repository function"). A closed set, unlike `TransactionType`'s open
    one: each member names a real, already-summed `player_season_stats`
    column (or column pair) `stats_repo.list_player_season_stat_leaders`
    maps to a SQL expression via `stats_repo.LEADER_STAT_COLUMNS` -- never
    a caller-supplied string concatenated into a query (see that repo
    function's own docstring)."""

    POINTS = "points"
    REBOUNDS = "rebounds"
    ASSISTS = "assists"
    STEALS = "steals"
    BLOCKS = "blocks"
    TURNOVERS = "turnovers"


@dataclass(frozen=True)
class StatLeaderRow:
    """One row of a `/leaders` read -- `stats_repo.list_player_season_stat_
    leaders`'s own return shape, the same "repository returns measured
    values" split `PlayerCareerScoringToDate` above documents. `stat_total`
    is the season SUM of whichever `LeaderStat` column(s) the caller asked
    for (`rebounds` sums `oreb + dreb`, matching `_BoxScoreColumns`'s own
    "no separate total" convention -- the split stays in the box score
    table, the combined total is this read's own job); `games_played`
    rides along so a caller can compute a per-game average without a
    second query -- `presentation/`'s own job, per Decision P5-5, never
    this repository's."""

    player_id: int
    stat_total: int
    games_played: int
    rank: int
    """1-based rank within the ORDER BY this row came from -- `ROW_NUMBER()` for
    `list_live_stat_leaders` (docs/design/leaders-v2-per-game-and-paging.md LD2-1),
    or the enumerated position in `list_player_season_stat_leaders`'s own `ORDER BY`
    for the totals-aggregate reader. No default: a defaulted rank is a value nobody
    computed that could still get printed (LD2-6, minor 5)."""


@dataclass(frozen=True)
class LiveLeaderPage:
    """`stats_repo.list_live_stat_leaders`'s own return shape (docs/design/
    leaders-v2-per-game-and-paging.md LD2-1/LD2-6) -- one page of a live
    `/leaders` read, plus the qualification-bar metadata that must survive even a
    zero-row page (LD2-1's `bar b LEFT JOIN page p ON TRUE` shape, B2): a league
    with no final games at all returns `min_team_games`/`max_team_games` both
    `None` and `rows` empty; a league with final games but nobody clearing the bar
    returns real `min_team_games`/`max_team_games` and `rows` still empty. Frozen,
    like `StatLeaderRow` -- a read-only fact from the database."""

    rows: tuple[StatLeaderRow, ...]
    page: int
    has_more: bool
    bar_applied: bool
    qualify_fraction: float
    min_team_games: int | None
    max_team_games: int | None
    teams_counted: int


@dataclass(frozen=True)
class PlayerFormWindow:
    """One player's recent-form read -- `stats_repo.list_recent_form_
    windows`'s own return shape (docs/design/media-v2-newsroom.md MV-2c,
    step 2). Carries BOTH the last-`window_games` sums and the season-to-
    date sums for the same 13 box columns, so the pure detector layer
    (`dba2/media/detect/form.py`, a later build step) can compute
    `baseline = season - window` by subtraction -- exact, since the window
    is a strict subset of the season aggregate, and free: no second query,
    no extra column. `points`/`minutes` are `float` (cast in SQL), **never
    `Decimal`** -- this project's documented asyncpg `Decimal`-vs-`float`
    equality trap (HANDOFF.md's Traps section); every other column here is
    a real, whole-game counting-stat sum and stays `int`.

    `window_games`/`season_games` are the two sample-size denominators
    MV-2c's own eligibility rule (`window_games == 5 AND season_games >=
    15`) reads directly; `team_id` is the player's CURRENT team as of the
    window (a player traded mid-window still aggregates across both team
    ids, MV-2c's own disclosed rule) -- carried so a detector needs no
    second `players`/roster read just to attribute the fact to a team."""

    player_id: int
    team_id: int
    window_games: int
    season_games: int

    window_points: float
    window_minutes: float
    window_fga_rim: int
    window_fga_mid: int
    window_fga_three: int
    window_fgm_three: int
    window_fta: int
    window_ftm: int
    window_ast: int
    window_stl: int
    window_blk: int
    window_dreb: int
    window_tov: int

    season_points: float
    season_minutes: float
    season_fga_rim: int
    season_fga_mid: int
    season_fga_three: int
    season_fgm_three: int
    season_fta: int
    season_ftm: int
    season_ast: int
    season_stl: int
    season_blk: int
    season_dreb: int
    season_tov: int


@dataclass(frozen=True)
class TeamStyleAggregate:
    """One team's measured style read -- `stats_repo.list_team_style_
    aggregates`'s own return shape (docs/design/media-v2-newsroom.md MV-2g,
    step 2). The MEASURED counterpart to `team_strategy`'s DECLARED
    `pace_target`/`three_point_bias` -- a later build step's `SCHEME_
    IDENTITY`/`SCHEME_TENSION` detector compares the two, never this
    dataclass alone. `three_share`/`oreb_rate`/`fta_rate`/`tov_per_100` are
    all real ratios computed in SQL (never `Decimal`, same reasoning as
    `PlayerFormWindow` above); `games` is the sample-size denominator a
    detector needs before trusting any of them."""

    team_id: int
    games: int
    avg_possessions: float
    three_share: float
    oreb_rate: float
    fta_rate: float
    tov_per_100: float


@dataclass
class GamePeriodScore:
    """`game_period_scores` — `stats_repo`'s table (phase-1-simulation-engine.md
    §7.2/§8.1). PK (game_id, team_id, period). Line score is not recoverable
    from a totals-only box score without a full replay — Pillar 6's score
    bug and Pillar 4's "came back from 18 down" storylines both read it.
    `league_id` per the blanket "every league-scoped table carries
    league_id" convention (migration 006's docstring)."""

    game_id: int
    league_id: int
    team_id: int
    period: int
    points: int
