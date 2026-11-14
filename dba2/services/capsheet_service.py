"""`project_league_cap_sheets` -- the I/O half of §8's cap-sheet projection
(docs/design/phase-2-manager-minds.md §8, §12 step 7): loads every input
`managers.capsheet`'s pure functions need for a whole league's 5-season
projection (Decision P2-15's horizon) via the three `contract_repo` bulk
loaders plus one `get_cap_settings` call per season, then hands the
assembled, already-loaded data to `managers.capsheet.project_team_cap_sheet`
per team -- the same "one bulk load up front, reused across every team"
shape `game_sim_service.simulate_scheduled_games`'s snapshot load already
established for Phase 1.

Returns all 30 teams at once (`dict[team_id, tuple[SeasonCapProjection,
...]]`), not one team at a time, on purpose: a single-team entry point
would still need every one of `league_id`'s bulk-loader reads (each is a
`GROUP BY team_id, season` over the whole league, not filterable to one
team without a second, narrower query) and would silently re-run them once
per team if a caller looped this module 30 times -- exactly the N+1 shape
§8's own text says these loaders exist to avoid.

**Known gap, flagged rather than silently worked around:** §8's own
"repository additions" paragraph lists exactly three `contract_repo` bulk
loaders and does not include one for `cap_holds` totals, even though
`cap_holds` is one of the nine per-season quantities (item 3). This module
therefore falls back to the existing single-`(team, season)` `contract_repo.
get_cap_hold_total`, looped over every team x every horizon season below --
a real N+1 (30 x 5 = 150 calls) for that one quantity only. Left as the
existing function rather than inventing an unrequested fourth bulk loader
outside this step's stated scope; cheap in practice today since `cap_holds`
ships schema-only through Phase 2 step 7 (Decision 19) and is not yet
populated at volume by anything in this codebase. Worth a bulk loader of
its own the day something actually writes rows into that table at scale.
"""
from __future__ import annotations

from collections import defaultdict

from dba2.core.errors import NotFoundError
from dba2.data.db import Executor
from dba2.data.repositories import contract_repo, league_repo, team_repo
from dba2.domain.contract import OptionType
from dba2.domain.league import LeagueCapSettings
from dba2.managers import capsheet
from dba2.managers.tuning import DEFAULT_TUNING, GmTuning


async def project_league_cap_sheets(
    ex: Executor,
    *,
    league_id: int,
    tuning: GmTuning = DEFAULT_TUNING,
    include_holds: bool = False,
) -> dict[int, tuple[capsheet.SeasonCapProjection, ...]]:
    """Every team's `tuning.horizon`-season (Decision P2-15: 5) cap-sheet
    projection, starting at the league's `current_season`. Raises
    `NotFoundError` if the league does not exist or if `current_season`
    itself (the projection's season-0 row) has no real `league_cap_settings`
    row -- there is no meaningful season-0 figure to build the rest of the
    horizon from without it, the same "raise rather than fabricate" call
    `cap_service.get_cap_summary` makes for the single-season case, and the
    one Decision P2-17's cross-check depends on being possible at all."""
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")

    start_season = league.current_season
    seasons = tuple(start_season + i for i in range(tuning.horizon))

    real_cap_settings: dict[int, LeagueCapSettings] = {}
    for season in seasons:
        settings = await league_repo.get_cap_settings(ex, league_id, season)
        if settings is not None:
            real_cap_settings[season] = settings
    if start_season not in real_cap_settings:
        raise NotFoundError(
            f"No league_cap_settings row for league {league_id}, season {start_season}"
        )

    teams = await team_repo.list_teams_for_league(ex, league_id)

    committed = await contract_repo.get_committed_salary_for_league_seasons(ex, league_id, seasons)
    dead_cap = await contract_repo.get_dead_cap_totals_for_league_seasons(ex, league_id, seasons)
    active_years = await contract_repo.list_active_contract_years_for_league_range(
        ex, league_id, seasons
    )

    roster_players: dict[tuple[int, int], set[int]] = defaultdict(set)
    team_option: dict[tuple[int, int], int] = defaultdict(int)
    player_option: dict[tuple[int, int], int] = defaultdict(int)
    for row in active_years:
        key = (row.team_id, row.season)
        roster_players[key].add(row.player_id)
        if row.option_type is OptionType.TEAM:
            team_option[key] += row.salary
        elif row.option_type is OptionType.PLAYER:
            player_option[key] += row.salary

    # No bulk cap_holds loader -- see module docstring's "known gap."
    cap_holds: dict[tuple[int, int], int] = {}
    for team in teams:
        for season in seasons:
            cap_holds[(team.id, season)] = await contract_repo.get_cap_hold_total(
                ex, team.id, season
            )

    projections: dict[int, tuple[capsheet.SeasonCapProjection, ...]] = {}
    for team in teams:
        team_inputs = tuple(
            capsheet.CapSheetTeamSeasonInputs(
                team_id=team.id,
                season=season,
                committed_salary=committed.get((team.id, season), 0),
                dead_cap=dead_cap.get((team.id, season), 0),
                cap_holds=cap_holds[(team.id, season)],
                team_option_salary=team_option.get((team.id, season), 0),
                player_option_salary=player_option.get((team.id, season), 0),
                roster_count=len(roster_players.get((team.id, season), ())),
            )
            for season in seasons
        )
        projections[team.id] = capsheet.project_team_cap_sheet(
            team_inputs, real_cap_settings, tuning=tuning, include_holds=include_holds
        )
    return projections
