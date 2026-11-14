"""`view_service` — the read-only assembly layer between `dba2/
presentation/`'s pure `Panel` builders and the real repository/service
reads that feed them (docs/design/phase-5-discord-presentation.md §11
Stage B step 7, §10 test tier 2: "integration tests against the real
Postgres ... covers 'does the assembled data match what the DB holds,'
never 'does it look right'").

**Decision P5-5/P5-6, this module's own central obligation.** Every
league fact a service already derives is read from that service, never
recomputed here: standings/streak/rank come from `media_service.compute_
current_pulse_snapshot` (never `managers/window.py`, never a `games`
table scan of this module's own); cap numbers come from `cap_service.
get_cap_summary` (never hand-summed from `contracts`); a player's overall
is `Player.overall_cached`, read verbatim. This module's own job is
translation — turning a service's return shape into whatever plain
fields `presentation/`'s pure builders accept — never a second
implementation of a number one of those services already owns.

**Why `PulseSnapshot`/`TeamPulseState` never appear as a parameter or a
type hint anywhere below.** `dba2.media` is restricted to `dba2/services/
media_service.py` alone (Decision P4-31's one-way valve); this module
calls `media_service.compute_current_pulse_snapshot` and reads its return
value with plain attribute access (`.teams`, `.wins`, `.losses`, ...)
without importing the type that names them — the AST layering check
(`tests/unit/test_layering.py`) scans imports, not runtime attribute
access, so this is the one place in the codebase that legitimately
touches a `PulseSnapshot` without being allowed to name it.

`dba2.presentation` is imported HERE and only here among `dba2/services/`
— `test_nothing_but_view_service_imports_presentation`'s own one-way
valve, the identical shape `media_service.py` already has for `dba2.
media`.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from dba2.core.errors import NotFoundError
from dba2.data.db import Executor
from dba2.data.repositories import (
    contract_repo,
    draft_repo,
    game_repo,
    league_repo,
    media_repo,
    player_repo,
    stats_repo,
    team_repo,
    txn_repo,
)
from dba2.domain.contract import Contract
from dba2.domain.draft import AvailableProspect, DraftSelection
from dba2.domain.game import GameType
from dba2.domain.league import PhaseHistory
from dba2.domain.phase import Phase
from dba2.domain.player import Player
from dba2.domain.stats import LeaderMode, LeaderStat, StatLeaderRow
from dba2.domain.team import Team, TeamManagerHistory
from dba2.domain.transaction import LeagueTransaction
from dba2.phase import graph
from dba2.presentation.boxscore import build_boxscore_panel
from dba2.presentation.broadcast import surnames
from dba2.presentation.leaders import LeaderRow, build_leaders_panel
from dba2.presentation.panel import Panel
from dba2.presentation.phase import build_phase_panel
from dba2.presentation.standings import StandingsRow, build_standings_panel
from dba2.presentation.team import build_team_panel
from dba2.presentation.wire import build_wire_panels
from dba2.services import cap_service, media_service
from dba2.sim.events import GameResult

# ---------------------------------------------------------------------------
# /standings
# ---------------------------------------------------------------------------


async def get_standings_panel(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    cycle_index: int,
    group: str | None = None,
) -> Panel:
    """`group` matches either `conference` or `division` — the command
    surface's own `[conference|division]` shape (§7.1).

    **docs/design/standings-v2-conference-split.md SV-3/SV-4.** Rank and
    games-behind are computed PER CONFERENCE, by FILTERING `pulse.py`'s own
    already win-pct-descending/`team_id`-ascending order to one conference
    at a time and enumerating the filtered result 1..N — never a second
    sort, never a new tie-break. `win_pct` is copied verbatim from
    `TeamPulseState.win_pct` (SV-2) — no division anywhere in this module."""
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    teams = await team_repo.list_teams_for_league(ex, league_id)
    pulse = await media_service.compute_current_pulse_snapshot(
        ex, league_id=league_id, season=season, cycle_index=cycle_index
    )
    # `pulse.py`'s own league-wide order, reproduced here by the field it
    # already encodes (`standings_position`) -- not a new sort, the same
    # discipline the old code's own `rows.sort(...)` already applied.
    league_ordered = sorted(
        (team for team in teams if team.id in pulse.teams),
        key=lambda team: pulse.teams[team.id].standings_position,
    )
    rows: list[StandingsRow] = []
    for conference in sorted({team.conference for team in league_ordered}):
        conference_teams = [team for team in league_ordered if team.conference == conference]
        leader = pulse.teams[conference_teams[0].id]
        for rank, team in enumerate(conference_teams, start=1):
            state = pulse.teams[team.id]
            zero_games = state.wins + state.losses == 0
            if rank == 1 or zero_games:
                games_behind = None
            else:
                games_behind = max(
                    0.0, ((leader.wins - state.wins) + (state.losses - leader.losses)) / 2
                )
            rows.append(
                StandingsRow(
                    team_id=team.id,
                    code=team.code,
                    city=team.city,
                    name=team.name,
                    conference=team.conference,
                    division=team.division,
                    wins=state.wins,
                    losses=state.losses,
                    standings_position=state.standings_position,
                    conference_rank=rank,
                    win_pct=state.win_pct,
                    games_behind=games_behind,
                )
            )
    return build_standings_panel(league_name=league.name, season=season, rows=rows, group=group)


# ---------------------------------------------------------------------------
# /team
# ---------------------------------------------------------------------------


async def get_team_panel(
    ex: Executor, *, league_id: int, team_id: int, season: int, cycle_index: int
) -> Panel:
    team = await team_repo.get_team(ex, team_id)
    if team is None or team.league_id != league_id:
        raise NotFoundError(f"No team {team_id} in league {league_id}")
    roster = await player_repo.list_players_for_team(ex, team_id)
    cap_summary = await cap_service.get_cap_summary(
        ex, league_id=league_id, team_id=team_id, season=season
    )
    pulse = await media_service.compute_current_pulse_snapshot(
        ex, league_id=league_id, season=season, cycle_index=cycle_index
    )
    team_pulse = pulse.teams.get(team_id)
    wins = team_pulse.wins if team_pulse is not None else 0
    losses = team_pulse.losses if team_pulse is not None else 0
    streak_length = team_pulse.streak_length if team_pulse is not None else 0
    standings_position = team_pulse.standings_position if team_pulse is not None else 0
    return build_team_panel(
        team=team,
        wins=wins,
        losses=losses,
        streak_length=streak_length,
        standings_position=standings_position,
        roster=roster,
        cap_summary=cap_summary,
    )


# ---------------------------------------------------------------------------
# /box
# ---------------------------------------------------------------------------


async def get_boxscore_panel(ex: Executor, *, game_id: int) -> Panel:
    game = await game_repo.get_game(ex, game_id)
    if game is None:
        raise NotFoundError(f"No game {game_id}")
    home_team = await team_repo.get_team(ex, game.home_team_id)
    away_team = await team_repo.get_team(ex, game.away_team_id)
    if home_team is None or away_team is None:
        raise NotFoundError(f"Game {game_id} references a missing team")

    all_player_stats = await stats_repo.list_player_game_stats_for_game(ex, game_id)
    home_stats = [s for s in all_player_stats if s.team_id == game.home_team_id]
    away_stats = [s for s in all_player_stats if s.team_id == game.away_team_id]
    home_team_stats = await stats_repo.get_team_game_stats(ex, game_id, game.home_team_id)
    away_team_stats = await stats_repo.get_team_game_stats(ex, game_id, game.away_team_id)
    period_scores = await stats_repo.list_game_period_scores(ex, game_id)

    # Two calls cover the common case (every stat line belongs to a
    # player still on the team that played the game); `get_player` is
    # only called per straggler id (a player traded/waived since this
    # game was played) rather than once per box-score row every time —
    # bounded, not the "150-call N+1" shape P5-16 warns `/capsheet`
    # about.
    home_roster = await player_repo.list_players_for_team(ex, game.home_team_id)
    away_roster = await player_repo.list_players_for_team(ex, game.away_team_id)
    players: dict[int, Player] = {p.id: p for p in (*home_roster, *away_roster)}
    needed_ids = {s.player_id for s in all_player_stats}
    for player_id in needed_ids - players.keys():
        player = await player_repo.get_player(ex, player_id)
        if player is not None:
            players[player_id] = player

    return build_boxscore_panel(
        game=game,
        home_team=home_team,
        away_team=away_team,
        home_player_stats=home_stats,
        away_player_stats=away_stats,
        home_team_stats=home_team_stats,
        away_team_stats=away_team_stats,
        period_scores=period_scores,
        players=players,
    )


# ---------------------------------------------------------------------------
# /draft's board
# ---------------------------------------------------------------------------

_DRAFT_BOARD_LIMIT_CEILING = 25
"""Discord's own hard cap on an autocomplete response
(`app_commands.Choice` list length). Clamped HERE as well as in the cog,
for the same reason `_WIRE_LIMIT_CEILING` is: a keyword argument any
future non-Discord caller could pass a bad value to."""


async def get_draft_board(
    ex: Executor, *, league_id: int, season: int, query: str = "", limit: int = 25
) -> tuple[AvailableProspect, ...]:
    """The prospects still available in this season's draft, ranked by mock
    board -- `/draft`'s autocomplete's ONLY database path (Decision P5-98.k).

    The cog reaches the database through this function and nothing else, the
    same one-door shape `/watch` uses (`get_broadcast_inputs`): no SQL and no
    repo call lives in `bot/cogs/draft.py`. It decides nothing -- a person who
    types a raw player id gets the identical service-layer treatment from
    `gm_command_service.draft_pick`, which re-validates the prospect under the
    `drafts` row lock. This read is a convenience, never an authority."""
    limit = max(1, min(limit, _DRAFT_BOARD_LIMIT_CEILING))
    return tuple(
        await draft_repo.list_available_prospects(
            ex, league_id=league_id, season=season, query=query, limit=limit
        )
    )


# ---------------------------------------------------------------------------
# /watch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BroadcastInputData:
    """What a broadcast needs from the database beyond the in-memory
    `GameResult` (design §4.2): both teams (codes and names for the panels) and
    the surname of every player the plays can name. `bot/broadcast.py` pairs
    this with the result as its `BroadcastInputs`; it is a separate type here
    because a service may not import `bot/`."""

    home: Team
    away: Team
    names: dict[int, str]


async def get_broadcast_inputs(
    ex: Executor, *, game_id: int, result: GameResult
) -> BroadcastInputData:
    """Read the rows a `/watch` broadcast renders from, once, before its task
    starts -- so the task itself never queries (design B1).

    `result` is the in-memory `GameResult` the block registry holds; it is a
    parameter because the beat log is never persisted (P5-42) and it says which
    players can appear: everyone with a box line plus every id a beat names
    (`actor_ids`). Those are resolved the way `get_boxscore_panel` does -- the
    two current rosters in two calls, then `get_player` only for a straggler
    traded or waived since the game -- and reduced to surnames by the
    presentation layer's own `surnames`. A player who is gone entirely is left
    out, and the renderer's "Player <id>" fallback covers them.

    Raises `NotFoundError` for a missing game or team, and `ValueError` when
    `result` is not this game's (its team ids disagree with the row): pairing
    one game's beat log with another game's teams would broadcast a game that
    never happened."""
    game = await game_repo.get_game(ex, game_id)
    if game is None:
        raise NotFoundError(f"No game {game_id}")
    if (result.home_team_id, result.away_team_id) != (game.home_team_id, game.away_team_id):
        raise ValueError(
            f"game {game_id}: the in-memory result is for teams "
            f"{result.home_team_id}/{result.away_team_id}, the game row for "
            f"{game.home_team_id}/{game.away_team_id}"
        )
    home = await team_repo.get_team(ex, game.home_team_id)
    away = await team_repo.get_team(ex, game.away_team_id)
    if home is None or away is None:
        raise NotFoundError(f"Game {game_id} references a missing team")

    home_roster = await player_repo.list_players_for_team(ex, game.home_team_id)
    away_roster = await player_repo.list_players_for_team(ex, game.away_team_id)
    players: dict[int, Player] = {p.id: p for p in (*home_roster, *away_roster)}
    needed = {line.player_id for line in result.player_box_lines}
    for record in result.beat_log:
        needed.update(record.actor_ids.values())
    for player_id in sorted(needed - players.keys()):
        player = await player_repo.get_player(ex, player_id)
        if player is not None:
            players[player_id] = player
    return BroadcastInputData(
        home=home,
        away=away,
        names=surnames({pid: p for pid, p in players.items() if pid in needed}),
    )


# ---------------------------------------------------------------------------
# /wire
# ---------------------------------------------------------------------------


# Blocking Issue 3 (Stage C critic round): `limit=0` evaluates
# `stories[-0:]` as `stories[0:]` (the WHOLE list, not zero), and a
# negative `limit` slices from the front instead of the back --
# `/wire n:0` or `/wire n:-3` posted every media story of the season,
# one Discord message each. `bot/cogs/read.py`'s own `app_commands.
# Range[int, 1, 20]` is the primary defense (Discord itself rejects an
# out-of-range value before the command body runs), but this function
# clamps independently -- "regardless of caller" per the critic's own
# wording, since `limit` is a plain keyword argument any future non-
# Discord caller (a `tools/` script) could pass a bad value to.
_WIRE_LIMIT_FLOOR = 1
_WIRE_LIMIT_CEILING = 100


async def _wire_writer_names(ex: Executor, league_id: int) -> dict[int, str]:
    """docs/design/media-v2-newsroom.md MV-7, build step 7: "one extra read
    per call ... built into a `{writer_id: name}` dict -- **not** a per-
    story `get_media_writer` N+1." One batch read regardless of how many
    stories a caller is about to render -- a league carries at most 34
    `media_writers` rows (5 league-wide + 1 per team), so this is cheap
    even for a full-season, hundred-story `/wire` read."""
    writers = await media_repo.list_media_writers_for_league(ex, league_id)
    return {writer.id: writer.name for writer in writers}


async def get_wire_panels(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    cycle_index: int | None = None,
    limit: int | None = None,
    beat: str | None = None,
) -> tuple[Panel, ...]:
    """`cycle_index=None` reads the whole season (§6.3's wire channel
    itself, cycle by cycle, calls this with a real `cycle_index` at the
    end of each media pulse cycle); `limit`, when given, keeps only the
    most recent `n` stories — `list_media_stories_for_league_season`
    already returns oldest-first, so "most recent n" is the tail.
    Clamped to `[_WIRE_LIMIT_FLOOR, _WIRE_LIMIT_CEILING]` regardless of
    what a caller passes (Blocking Issue 3) -- `limit=0` or a negative
    `limit` can no longer produce the whole season's worth of stories.

    **`beat` (build step 7, MV-7/MV-9 -- "`get_wire_panels` gains `beat`/
    `page`") narrows to one persona key** (`media_stories.voice`, e.g.
    `"insider"`, `"hot_take"`) -- `None` (the default) shows every persona,
    unmodified from this function's pre-step-7 behavior, which is why every
    existing caller/test that never passes `beat` keeps working unchanged.
    Filtered in Python, after the existing whole-season read, rather than
    pushed into a new SQL predicate -- this function already materializes
    the whole season before applying `limit`, so a second in-memory filter
    costs nothing this function does not already pay; `/wire`'s own paged
    command uses `get_wire_page` below instead, which filters and pages at
    the SQL layer for the real per-season row counts a live league reaches.

    **Writer names are resolved here now, one batch read** (MV-7's own
    "both wire reads resolve writer names in one batch"), so every panel
    this function returns carries a real byline instead of the column
    alone -- unless the story's own `writer_id` is `None` (MV-8's disclosed
    since-deleted-team race), in which case `build_wire_panel`'s own
    documented "column alone, never the string None" branch renders it."""
    stories = await media_repo.list_media_stories_for_league_season(
        ex, league_id=league_id, season=season, cycle_index=cycle_index
    )
    if beat is not None:
        stories = [story for story in stories if story.voice == beat]
    if limit is not None:
        limit = max(_WIRE_LIMIT_FLOOR, min(limit, _WIRE_LIMIT_CEILING))
        stories = stories[-limit:]
    writer_names = await _wire_writer_names(ex, league_id)
    return build_wire_panels(stories, writer_names)


async def get_unposted_wire_panels(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    cycle_index: int | None = None,
) -> tuple[tuple[int, Panel], ...]:
    """The wire's PUBLICATION read (phase-5-stage-e-advance.md P5-99.s.3): every
    story of the league+season with no `#league-wire` post recorded, as `(story_id,
    panel)` pairs, oldest cycle first. The id rides beside its panel so the poster can
    mark exactly that story once its `channel.send` returns -- sound because
    `build_wire_panels` is one panel per story, in the order given, so pairing by
    position re-derives nothing. `cycle_index` narrows to one cycle; `None` reads the
    season. `get_wire_panels` above stays the `/wire` read: it shows posted stories
    too. Writer names resolved the same one-batch way (MV-7, build step 7)."""
    stories = await media_repo.list_unposted_media_stories(
        ex, league_id=league_id, season=season, cycle_index=cycle_index
    )
    writer_names = await _wire_writer_names(ex, league_id)
    panels = build_wire_panels(stories, writer_names)
    return tuple((story.id, panel) for story, panel in zip(stories, panels, strict=True))


# `/wire`'s pager (MV-7, build step 7) -- one story per page, newest first.
# `get_wire_panels` above stays a whole-season-into-memory read (its own
# pre-step-7 shape, extended, not replaced -- see its own docstring for
# why); this is the real, SQL-paged, beat-filtered counterpart `/wire`'s
# command and its `WireNavButton` click handler actually call, mirroring
# `get_leaders_panel`/`LeadersView`'s own "one bare read, one richer paged
# view, two functions" split rather than overloading one function's return
# shape on whether `page` was given.
_WIRE_PAGE_SIZE = 1
_WIRE_MAX_PAGE = 500
"""`/wire`'s own `WireNavButton` clamp mirrors `leaders_pager.py`'s
`_MAX_PAGE` (LD2-3's "a forged page never reaches an `int8` OFFSET
overflow" reasoning). `WireNavButton`'s own `custom_id` template encodes
`page` in `\\d{1,3}` (a value up to `999` can still match `fullmatch`;
a 4-digit `page` cannot, LD2-3's own bounded-group reasoning) --
`_WIRE_MAX_PAGE` is deliberately BELOW that ceiling (`500`, not `999`),
mirroring `leaders_pager.py`'s own gap between its `_MAX_PAGE=100` and its
identical `\\d{1,3}` bound: a real forged id in `(500, 999]` still matches
the template and reaches this function, so there is a real, testable
"matches but out of range" case, not just an unrepresentable one. `500`
pages at one story each still covers a full season at this corpus's own
measured ~150-450 stories/season (build step 6's As-built note), with
room to spare."""


@dataclass(frozen=True)
class WirePageView:
    """One page of `/wire`'s pager -- `panel` is `None` exactly when this
    beat has no stories at all (never when a stale page number was merely
    too high; `get_wire_page` clamps that case back to page 1 before this
    is ever constructed, the same LD2-4 shape `LeadersView`'s own `page`
    field documents)."""

    panel: Panel | None
    page: int
    has_more: bool

    @property
    def is_empty(self) -> bool:
        return self.panel is None


async def get_wire_page(
    ex: Executor, *, league_id: int, season: int, beat: str | None = None, page: int = 1
) -> WirePageView:
    """`beat` is a persona key (`None` = every persona, unfiltered) -- the
    bot layer's own job is turning a Discord `beat` CHOICE label into this
    parameter's value (`all` -> `None`, `trade-desk` -> `"insider"`, ...,
    per MV-7's own "the beat value maps to a persona key, which is what the
    filter actually queries on" text), not this function's.

    **LD2-4's stale-page clamp, one bounded retry** (the identical shape
    `get_leaders_panel` already uses): if `page > 1` comes back empty (a
    beat switch, or history shrinking under a since-deleted subject's
    cascade, left fewer pages than before), re-run once at page 1 rather
    than rendering an empty panel on a beat that has real stories. The
    `WirePageView.page` returned is the page actually rendered, post-clamp
    -- the caller bakes THAT into the next `custom_id`, never the page it
    originally asked for."""
    rendered_page = max(1, min(page, _WIRE_MAX_PAGE))
    offset = (rendered_page - 1) * _WIRE_PAGE_SIZE
    stories = await media_repo.list_media_stories_page(
        ex, league_id=league_id, season=season, voice=beat,
        offset=offset, limit=_WIRE_PAGE_SIZE + 1,
    )
    if not stories and rendered_page > 1:
        rendered_page = 1
        stories = await media_repo.list_media_stories_page(
            ex, league_id=league_id, season=season, voice=beat,
            offset=0, limit=_WIRE_PAGE_SIZE + 1,
        )
    has_more = len(stories) > _WIRE_PAGE_SIZE
    stories = stories[:_WIRE_PAGE_SIZE]
    if not stories:
        return WirePageView(panel=None, page=rendered_page, has_more=False)
    writer_names = await _wire_writer_names(ex, league_id)
    (panel,) = build_wire_panels(stories, writer_names)
    return WirePageView(panel=panel, page=rendered_page, has_more=has_more)


# ---------------------------------------------------------------------------
# /leaders v2 (docs/design/leaders-v2-per-game-and-paging.md) -- per-game
# default, a live per-team qualification bar, a Total/Per Game toggle, and
# paging, still off `stats_repo.list_live_stat_leaders`'s live aggregate over
# `player_game_stats` (leaders-live.md LD-1, unchanged). `LeaderStat`/
# `LeaderMode`/`StatLeaderRow` are re-exported here only so a Stage C/pager
# caller has one import site for the whole `/leaders` read surface.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeadersView:
    """`get_leaders_panel`'s own return shape (LD2-6) -- a bare `Panel` (or a
    `(Panel, has_more)` tuple, revision 1's rejected shape, LD2-6's B3)
    cannot tell "nothing qualifies" apart from "here is page 1 of one," and
    the cog/click handler need that state to decide which buttons to send,
    never by parsing `panel.description`. Lives here, not `domain/stats.py`,
    because it carries a `Panel` -- a presentation object -- and `domain/`
    must not import `presentation/` (the same one-way-valve rule this
    module's own docstring states for `dba2.media`)."""

    panel: Panel
    page: int  # the page actually rendered, post LD2-4's stale-page clamp
    has_more: bool
    row_count: int  # rows on the rendered page
    max_team_games: int | None  # `LiveLeaderPage.max_team_games`, for `no_games` below

    @property
    def is_empty(self) -> bool:
        return self.row_count == 0

    @property
    def no_games(self) -> bool:
        """LD2-6's case 1 (no final games at all) vs. case 2 (games exist,
        nobody qualifies) -- both have `row_count == 0`; only this property
        tells them apart, and it is trustworthy precisely because LD2-4's
        clamp already ran: after it, zero rows at page 1 means the qualified
        set is genuinely empty, not merely a stale page."""
        return self.max_team_games is None


async def get_stat_leaders(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    game_type: GameType,
    stat: LeaderStat,
    limit: int = 10,
) -> list[StatLeaderRow]:
    """Kept at its EXACT pre-v2 signature and return type (LD2-6, minor 5):
    a four-line adapter over the new repo call -- `mode=TOTAL`,
    `apply_bar=False`, `page=1`, `page_size=limit` -- so its meaning is
    preserved bit for bit (totals, no bar, top-N) and every existing call
    site needs no change."""
    page = await stats_repo.list_live_stat_leaders(
        ex,
        league_id=league_id,
        season=season,
        game_type=game_type,
        stat=stat,
        mode=LeaderMode.TOTAL,
        apply_bar=False,
        page=1,
        page_size=limit,
    )
    return list(page.rows)


_LEADERS_PAGE_SIZE = 10


async def get_leaders_panel(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    game_type: GameType,
    stat: LeaderStat,
    mode: LeaderMode = LeaderMode.PER_GAME,
    page: int = 1,
    page_size: int = _LEADERS_PAGE_SIZE,
) -> LeadersView:
    """Stage C's own half of `/leaders` (`get_stat_leaders` above is Stage
    B's read alone -- see that function's own siting comment), forwarding
    `mode`/`page` to the repo (LD2-1/LD2-3) and returning a `LeadersView`
    instead of a bare `Panel` (LD2-6). `StatLeaderRow` carries no player name
    or team code (`stats_repo`'s own "repository returns measured values"
    split), so this resolves both here, per-row, bounded by `page_size` (the
    repo already trims its own `page_size + 1` probe row before this
    resolves any name, per `LiveLeaderPage.rows`'s own contract) -- the same
    bounded N+1 shape `get_boxscore_panel`'s straggler lookup already uses,
    not `/capsheet`'s 150-call one Decision P5-16 warns about.

    **LD2-4's stale-page clamp, one bounded retry.** If the requested page
    comes back empty and `page > 1` (the qualified list got shorter since
    this page was last rendered), re-run once at page 1: never an empty
    panel on a non-empty qualified list, never a second retry, never an
    error. The `LeadersView.page` returned is the page actually rendered,
    post-clamp -- the cog and the click handler bake THAT into the next
    `custom_id`, never the page the caller originally asked for."""
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    apply_bar = mode is LeaderMode.PER_GAME
    rendered_page = page
    live_page = await stats_repo.list_live_stat_leaders(
        ex,
        league_id=league_id,
        season=season,
        game_type=game_type,
        stat=stat,
        mode=mode,
        apply_bar=apply_bar,
        page=page,
        page_size=page_size,
    )
    if not live_page.rows and page > 1:
        rendered_page = 1
        live_page = await stats_repo.list_live_stat_leaders(
            ex,
            league_id=league_id,
            season=season,
            game_type=game_type,
            stat=stat,
            mode=mode,
            apply_bar=apply_bar,
            page=1,
            page_size=page_size,
        )
    rows: list[LeaderRow] = []
    for stat_row in live_page.rows:
        player = await player_repo.get_player(ex, stat_row.player_id)
        if player is None:
            continue
        team = await team_repo.get_team(ex, player.team_id) if player.team_id is not None else None
        rows.append(
            LeaderRow(
                player_id=stat_row.player_id,
                player_name=f"{player.first_name} {player.last_name}",
                team_code=team.code if team is not None else "FA",
                stat_total=stat_row.stat_total,
                games_played=stat_row.games_played,
                rank=stat_row.rank,
            )
        )
    panel = build_leaders_panel(
        league_name=league.name,
        season=season,
        stat=stat,
        mode=mode,
        rows=rows,
        bar_applied=live_page.bar_applied,
        qualify_fraction=live_page.qualify_fraction,
        min_team_games=live_page.min_team_games,
        max_team_games=live_page.max_team_games,
    )
    return LeadersView(
        panel=panel,
        page=rendered_page,
        has_more=live_page.has_more,
        row_count=len(rows),
        max_team_games=live_page.max_team_games,
    )


# ---------------------------------------------------------------------------
# /phase
# ---------------------------------------------------------------------------


async def get_phase_panel(ex: Executor, *, league_id: int) -> Panel:
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    legal_next = sorted(graph.legal_next_phases(league.current_phase), key=lambda p: p.value)
    return build_phase_panel(
        league_name=league.name,
        season=league.current_season,
        current_phase=league.current_phase,
        legal_next=legal_next,
    )


# ---------------------------------------------------------------------------
# B-12 — the transactions merge (Decision P5-33)
# ---------------------------------------------------------------------------

# `list(Phase)` is §6.2's own declared season-phase order (`dba2/domain/
# phase.py`'s own docstring). This codebase's real `cycle_index` columns
# (`league_transactions.cycle_index`/`trade_offers.cycle_index`) are a
# DEFERRED migration 013 addition — "NOT NULL with no default and no
# writer today" (§32.3's own corrected text) — so `(season, phase-
# ordinal, timestamp)` (§22.5) is built here from the one phase-ordering
# fact that DOES exist unconditionally: a `Phase` member's own position
# in its declared enum order. This is coarser than a real per-cycle
# ordinal (it cannot distinguish the corpus cadence's two `TRADE_
# DEADLINE` visits in one season, §32.3's own disclosed edge case for
# `force_advance`), but it is real, always available, and sufficient for
# what §22.5 actually asks it to do: break a same-second tie across
# sources at a phase boundary, not index into a cadence table.
_PHASE_ORDINAL: dict[Phase, int] = {phase: i for i, phase in enumerate(Phase)}


@dataclass(frozen=True)
class TransactionFeedItem:
    """One line of a `/transactions`-style read — Decision P5-33's "one
    ordered sequence of Panel-ready items," assembled from FIVE already-
    existing sources (`league_transactions`, `phase_history`, `team_
    manager_history`, `draft_selections`, and the contracts whose
    `signed_in_season`/`terminated_in_season` fall in this `season`),
    never a sixth denormalized table — P5-33's own "second-writer failure
    in its most attractive disguise." A plain data record, not a `Panel`
    — Stage B step 6 names four panel builders (`standings.py`/`team.py`/
    `boxscore.py`/`wire.py`); a `/transactions` panel is Stage C's command-
    surface job, built against this shape when that step is reached."""

    kind: str
    season: int
    phase: Phase
    phase_ordinal: int
    occurred_at: datetime
    team_id: int | None
    summary: str


def _phase_at(
    phase_history: Sequence[PhaseHistory], league_start_season: int, ts: datetime
) -> tuple[int, Phase]:
    """The `(season, phase)` active at timestamp `ts`, derived from the
    league's own `phase_history` audit trail — the nearest-preceding
    `occurred_at` row's `(season, to_phase)`, or the league's own
    `start_season_year` at `Phase.SETUP` if `ts` predates every recorded
    transition (e.g. a manager assigned at league creation, before the
    first `/advance`). `phase_history` MUST be sorted ascending by
    `occurred_at` — `league_repo.list_phase_history`'s own `ORDER BY
    occurred_at` guarantees it, and this function does not re-sort.

    This exists because `team_manager_history` carries no `season`/
    `phase` column of its own (migration 001's own shape) and `contracts`
    carries no `phase` column at all (§22.5 calls it "season-grained
    only") — both real gaps between §22.5's literal ordering key and the
    schema it has to run against, disclosed here rather than silently
    worked around with a made-up default."""
    season = league_start_season
    phase = Phase.SETUP
    for row in phase_history:
        if row.occurred_at > ts:
            break
        season = row.season
        phase = row.to_phase
    return season, phase


def _transaction_item(txn: LeagueTransaction) -> TransactionFeedItem:
    # `.value.title()` alone turns a multi-word `txn_type` into "Draft_Pick"
    # / "Timeline_Set" -- the underscore is the storage form, not a word.
    # `.replace("_", " ").title()` is the idiom the cogs already use for enum
    # labels (`cogs/roster.py:98`), and it is applied here rather than
    # matched because `TIMELINE_SET`'s existing "Timeline_Set" was always the
    # same defect, just one nobody had read out loud (Decision P5-98.e).
    summary = f"{txn.txn_type.value.replace('_', ' ').title()} #{txn.id}"
    if txn.note:
        summary += f": {txn.note}"
    return TransactionFeedItem(
        kind=txn.txn_type.value,
        season=txn.season,
        phase=txn.phase,
        phase_ordinal=_PHASE_ORDINAL[txn.phase],
        occurred_at=txn.created_at,
        team_id=None,  # a trade names multiple teams; see league_transaction_assets
        summary=summary,
    )


def _phase_history_item(row: PhaseHistory) -> TransactionFeedItem:
    from_text = row.from_phase.value if row.from_phase is not None else "league start"
    summary = f"Phase advanced: {from_text} → {row.to_phase.value}"
    if row.forced:
        summary += " (forced)"
    return TransactionFeedItem(
        kind="phase_advance",
        season=row.season,
        phase=row.to_phase,
        phase_ordinal=_PHASE_ORDINAL[row.to_phase],
        occurred_at=row.occurred_at,
        team_id=None,
        summary=summary,
    )


def _manager_assigned_item(
    row: TeamManagerHistory, phase: Phase, season: int
) -> TransactionFeedItem:
    summary = (
        f"Manager assigned (user {row.user_id})" if row.user_id is not None
        else "Manager unassigned (returned to AI control)"
    )
    return TransactionFeedItem(
        kind="manager_assigned",
        season=season,
        phase=phase,
        phase_ordinal=_PHASE_ORDINAL[phase],
        occurred_at=row.assigned_at,
        team_id=row.team_id,
        summary=summary,
    )


def _manager_removed_item(
    row: TeamManagerHistory, phase: Phase, season: int
) -> TransactionFeedItem:
    assert row.removed_at is not None
    return TransactionFeedItem(
        kind="manager_removed",
        season=season,
        phase=phase,
        phase_ordinal=_PHASE_ORDINAL[phase],
        occurred_at=row.removed_at,
        team_id=row.team_id,
        summary=f"Manager removed (user {row.user_id})",
    )


def _draft_selection_item(selection: DraftSelection, season: int) -> TransactionFeedItem:
    summary = (
        f"Round {selection.round}, Pick {selection.pick_number}: "
        f"drafted player {selection.player_id}"
    )
    return TransactionFeedItem(
        kind="draft_selection",
        season=season,
        phase=Phase.DRAFT,
        phase_ordinal=_PHASE_ORDINAL[Phase.DRAFT],
        occurred_at=selection.selected_at,
        team_id=selection.team_id,
        summary=summary,
    )


def _contract_signed_item(contract: Contract, phase: Phase, season: int) -> TransactionFeedItem:
    return TransactionFeedItem(
        kind="contract_signed",
        season=season,
        phase=phase,
        phase_ordinal=_PHASE_ORDINAL[phase],
        occurred_at=contract.created_at,
        team_id=contract.team_id,
        summary=f"Contract signed: player {contract.player_id} ({contract.signing_method.value})",
    )


def _contract_terminated_item(
    contract: Contract, phase: Phase, season: int, terminated_at: datetime
) -> TransactionFeedItem:
    """`terminated_at` is a real, caller-supplied termination instant
    (migration 015's `contracts.terminated_at`) -- never `contract.
    created_at`, which is the contract's SIGNING timestamp and produced a
    real, reproduced bug (a contract signed in one season and terminated
    in a later one sorted into the SIGNING season's place in the merge,
    docs/design/phase-5-discord-presentation.md §22.5). The caller
    (`get_league_transactions_feed`) is responsible for never calling
    this with a contract whose `terminated_at IS NULL`."""
    return TransactionFeedItem(
        kind="contract_terminated",
        season=season,
        phase=phase,
        phase_ordinal=_PHASE_ORDINAL[phase],
        occurred_at=terminated_at,
        team_id=contract.team_id,
        summary=f"Contract terminated: player {contract.player_id} ({contract.status.value})",
    )


async def get_league_transactions_feed(
    ex: Executor, *, league_id: int, season: int
) -> tuple[TransactionFeedItem, ...]:
    """B-12 — Decision P5-33's read-time merge, five sources, one
    ordered sequence, scoped to one `(league_id, season)` — matching
    every other Phase 5 read in this module (`/standings`, `/team`,
    `/wire`), never a whole-league-forever scan. Ordering key: `(phase_
    ordinal, occurred_at)` within this `season` (§22.5) — `season` is
    already held constant by the filter, so it is not a third sort key
    here the way §22.5's literal `(season, phase-ordinal, timestamp)`
    triple implies for an unscoped, multi-season read.

    **`contract_terminated` items are skipped, disclosed rather than
    silently dropped, for a contract whose `terminated_at IS NULL`** —
    only possible for a contract terminated before migration 015 added
    that column. There is no real termination instant to sort such a
    row on, and falling back to `created_at` (the SIGNING timestamp) is
    the exact bug migration 015 exists to fix, not an acceptable lesser
    version of it. The contract's termination is still on record in
    `contracts.terminated_in_season`/`status`; it just does not appear
    in this particular read-time feed."""
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    phase_history = await league_repo.list_phase_history(ex, league_id)

    items: list[TransactionFeedItem] = []

    for txn in await txn_repo.list_transactions_for_league(ex, league_id):
        if txn.season == season:
            items.append(_transaction_item(txn))

    for row in phase_history:
        if row.season == season:
            items.append(_phase_history_item(row))

    for row in await team_repo.list_manager_history_for_league(ex, league_id):
        assigned_season, assigned_phase = _phase_at(
            phase_history, league.start_season_year, row.assigned_at
        )
        if assigned_season == season:
            items.append(_manager_assigned_item(row, assigned_phase, assigned_season))
        if row.removed_at is not None:
            removed_season, removed_phase = _phase_at(
                phase_history, league.start_season_year, row.removed_at
            )
            if removed_season == season:
                items.append(_manager_removed_item(row, removed_phase, removed_season))

    draft = await draft_repo.get_draft_for_season(ex, league_id, season)
    if draft is not None:
        for selection in await draft_repo.list_draft_selections_for_draft(ex, draft.id):
            items.append(_draft_selection_item(selection, season))

    for contract in await contract_repo.list_contracts_for_league_season_events(
        ex, league_id, season
    ):
        if contract.signed_in_season == season:
            _, c_phase = _phase_at(phase_history, league.start_season_year, contract.created_at)
            items.append(_contract_signed_item(contract, c_phase, season))
        if contract.terminated_in_season == season:
            # `terminated_at` is NULL only for a contract terminated before
            # migration 015 added the column -- no real termination instant
            # exists for it to sort on, and re-borrowing `created_at` (the
            # SIGNING timestamp) is the exact bug this fix exists to close,
            # not a lesser version of it. Disclosed skip, not a silent one:
            # such a contract's termination is still recorded in
            # `contracts.terminated_in_season`/`status`, just not surfaced
            # in this read-time feed.
            if contract.terminated_at is not None:
                _, c_phase = _phase_at(
                    phase_history, league.start_season_year, contract.terminated_at
                )
                items.append(
                    _contract_terminated_item(contract, c_phase, season, contract.terminated_at)
                )

    items.sort(key=lambda item: (item.phase_ordinal, item.occurred_at))
    return tuple(items)
