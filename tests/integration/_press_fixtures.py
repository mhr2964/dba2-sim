"""League builders shared by the season-service block-press, postpone/restore and
hook-retry integration cases (build-plan commit 10).

Two kinds of league, for two costs. `sim_league_at` seeds a REAL league (30
teams, calibrated players, strategies, cap settings: ~6 s) whose games actually
sim, for the cases whose subject is what a block press does to real rows.
`bare_league_at` is a league with a few hand-made teams and games that cannot
sim (no strategy rows) -- cheap, and exactly what a deterministically failing
game, a postponement or a restore needs.

**Both walk REAL `phase_history`** from `SETUP` with direct
`phase_service.advance` calls (no press, no hook, no work). A league created at
a non-`SETUP` phase has zero history rows, and a Block-path count of `count - 1`
then raises: the fixtures must be built the way a real season builds them.
"""

from __future__ import annotations

import dataclasses
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime

from dba2.data.repositories import (
    draft_repo,
    game_repo,
    league_repo,
    player_repo,
    ratings_repo,
    team_repo,
)
from dba2.domain.draft import DraftPick
from dba2.domain.game import Game, GameStatus, GameType
from dba2.domain.phase import Phase
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import RatingSource
from dba2.domain.team import GmPhilosophy, Timeline
from dba2.phase import service as phase_service
from dba2.services import gm_service, media_service, schedule_service, season_service
from tests.fixtures.managers import birth_date_for_age
from tests.fixtures.sim import make_ratings
from tools import seed_dev_league

SEASON = 2026
COMMISSIONER = 1

# The live media library, parsed and validated ONCE per test process -- the cog's own
# rule (P5-99.s.5): `press` takes it as a required argument and never loads it itself.
LIBRARY = media_service.load_media_library()


async def press(pool, *, league_id: int, actor_user_id: int = COMMISSIONER, **kwargs):
    """`season_service.press` with the process's media library supplied, for the cases
    whose subject is not the library. `**kwargs` passes `agency_enabled` through."""
    return await season_service.press(
        pool, league_id=league_id, actor_user_id=actor_user_id, library=LIBRARY, **kwargs
    )


# The whole in-season walk, in graph order. Every phase from `PRESEASON` to
# `WAIVERS` is entered exactly once, so a league walked to phase P has real
# history for every phase before it.
WALK = [
    Phase.PRESEASON,
    Phase.REGULAR_SEASON,
    Phase.TRADE_DEADLINE,
    Phase.POST_DEADLINE,
    Phase.REGULAR_SEASON_COMPLETE,
    Phase.PLAYIN,
    Phase.PLAYOFFS_R1,
    Phase.PLAYOFFS_R2,
    Phase.CONFERENCE_FINALS,
    Phase.FINALS,
    Phase.AWARDS,
    Phase.DRAFT_LOTTERY,
    Phase.DRAFT,
    Phase.POST_DRAFT_TRADES,
    Phase.PROGRESSION,
    Phase.OPTION_DECISIONS,
    Phase.RESIGN_WINDOW,
    Phase.FREE_AGENCY,
    Phase.WAIVERS,
]


async def walk_to(pool, league_id: int, phase: Phase) -> None:
    """Direct advances from `SETUP` up to and including `phase`."""
    if phase is Phase.SETUP:
        return
    for target in WALK:
        await phase_service.advance(pool, league_id, target, COMMISSIONER)
        if target is phase:
            return
    raise AssertionError(f"{phase} is not in the walk")


async def mark_current_entry_work_done(pool, league_id: int) -> None:
    """Sets the completion checkpoints a press would have written for the league's
    CURRENT entry, so a case about something else (the sim, a postponement) is not
    also a case about phase work or the hook. The entry work of `TRADE_DEADLINE`
    and `PROGRESSION` is a real agency checkpoint that needs a bootstrapped
    league; the hook is stubbed or unwanted."""
    league = await league_repo.get_league(pool, league_id)
    entries = await league_repo.count_phase_entries(pool, league_id, league.current_season)
    cycle_entries = await league_repo.count_phase_entries(
        pool, league_id, league.current_season, season_service.CYCLE_BEARING_PHASES
    )
    entry_index = entries - 1
    if season_service.phase_has_work(season_service.WorkKind.ENTRY, league.current_phase):
        await league_repo.set_entry_work_completed_index(pool, league_id, entry_index)
    if league.current_phase in season_service.CYCLE_BEARING_PHASES:
        await league_repo.set_hook_completed_cycle_index(pool, league_id, cycle_entries - 1)


async def sim_league_at(pool, phase: Phase, *, keep_games: int, rng_seed: int = 3) -> int:
    """A real, sim-able league walked to `phase`, whose regular-season schedule is
    trimmed to its first `keep_games` games (game indexes `0..keep_games-1`) so
    every games target is reachable in a handful of presses. The generated
    schedule's first dates carry 1, 1, 2, 2, 3, 3, 4, 4 games."""
    league_id = await seed_dev_league.seed_dev_league(
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Block Press Test League",
        start_season_year=SEASON,
        current_season=SEASON,
        commissioner_user_id=COMMISSIONER,
        rng_seed=rng_seed,
    )
    await schedule_service.generate_regular_season(pool, league_id, SEASON)
    await pool.execute(
        "DELETE FROM games WHERE league_id = $1 AND season = $2 AND game_index >= $3",
        league_id,
        SEASON,
        keep_games,
    )
    await walk_to(pool, league_id, phase)
    await mark_current_entry_work_done(pool, league_id)
    return league_id


async def bare_league_at(pool, phase: Phase) -> int:
    """A league with no teams and no games, walked to `phase`."""
    league = await league_repo.create_league(
        pool,
        discord_guild_id=random.randint(10_000_000, 2_000_000_000),
        name="Bare Press Test League",
        start_season_year=SEASON,
        current_season=SEASON,
        commissioner_user_id=COMMISSIONER,
        rng_seed=1,
    )
    await walk_to(pool, league.id, phase)
    await mark_current_entry_work_done(pool, league.id)
    return league.id


async def regular_games_by_date(pool, league_id: int) -> list[tuple[date, list[Game]]]:
    """The league's regular-season games grouped by `game_date`, oldest date first."""
    games = await game_repo.list_games_for_league_season(pool, league_id, SEASON)
    by_date: dict[date, list[Game]] = defaultdict(list)
    for g in games:
        if g.game_type is GameType.REGULAR:
            by_date[g.game_date].append(g)
    return sorted(by_date.items())


async def add_two_teams(pool, league_id: int) -> tuple[int, int]:
    teams = []
    for code in ("PRH", "PRV"):
        team = await team_repo.create_team(
            pool,
            league_id=league_id,
            code=code,
            name=f"{code} Team",
            city=f"{code} City",
            conference="West",
            division="Pacific",
            home_market_size=2,
        )
        teams.append(team.id)
    return teams[0], teams[1]


async def add_game(
    pool, league_id: int, teams: tuple[int, int], *, game_index: int, game_date: date
) -> Game:
    """One `scheduled` regular-season game between the two teams. With no
    strategy rows behind them the game fails deterministically in
    `sim_scheduled_games` ("missing team/strategy snapshot data")."""
    return await game_repo.create_game(
        pool,
        league_id=league_id,
        season=SEASON,
        game_index=game_index,
        game_date=game_date,
        home_team_id=teams[0],
        away_team_id=teams[1],
        game_type=GameType.REGULAR,
        rng_seed=game_index + 1,
    )


async def add_played_game(
    pool, league_id: int, teams: tuple[int, int] | None = None, *, game_index: int = 0
) -> Game:
    """One `final` regular-season game: a schedule EXISTS and nothing in it is left to
    play. A schedule phase with no games at all is a `SCHEDULE_MISSING` blocker (the gate
    will not wave a season with nothing to play through, and no closing work runs over zero
    games), so a case whose subject is something else but that presses through one of the
    four schedule-consuming phases gives its league this. Creates its own two teams
    (`PLA`, `PLB`) unless `teams` names existing ones."""
    if teams is None:
        made = []
        for code in ("PLA", "PLB"):
            team = await team_repo.create_team(
                pool,
                league_id=league_id,
                code=code,
                name=f"{code} Team",
                city=f"{code} City",
                conference="West",
                division="Pacific",
                home_market_size=2,
            )
            made.append(team.id)
        teams = (made[0], made[1])
    return await game_repo.create_game(
        pool,
        league_id=league_id,
        season=SEASON,
        game_index=game_index,
        game_date=date(2026, 10, 20),
        home_team_id=teams[0],
        away_team_id=teams[1],
        game_type=GameType.REGULAR,
        rng_seed=game_index + 1,
        status=GameStatus.FINAL,
        home_score=100,
        away_score=90,
    )


async def drop(pool, league_id: int) -> None:
    # A real DRAFT hook appends its decision records to a per-league JSONL artifact.
    gm_service.artifact_path(league_id, SEASON).unlink(missing_ok=True)
    await pool.execute("DELETE FROM leagues WHERE id = $1", league_id)


_RATINGS_NON_COLUMN_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
)


@dataclass(frozen=True)
class DraftBoard:
    """What `add_draft_board` built: the teams by code, the picks in creation order
    (`picks[i]` is the `i`th tuple passed), the prospects and the `drafts` row's id
    (`None` when the board was built without one)."""

    teams: dict[str, int]
    picks: tuple[DraftPick, ...]
    prospects: tuple[int, ...]
    draft_id: int | None


async def add_draft_board(
    pool,
    league_id: int,
    *,
    picks: tuple[tuple[int, int | None, str], ...] = ((1, 1, "TOR"),),
    managers: dict[str, int] | None = None,
    auto_pilot: frozenset[str] = frozenset(),
    prospects: int = 2,
    draft_row: bool = True,
    used: tuple[int, ...] = (),
    teams: dict[str, int] | None = None,
) -> DraftBoard:
    """A draft board for an EXISTING league (which the caller walked through real
    history): teams, cap settings, the `drafts` row, `picks` as `(round, pick_number,
    team_code)` in creation order (`pick_number=None` is a slot the lottery has not
    resolved), `prospects` draft-eligible players with ratings, and `used` naming the
    indexes of picks already spent (each on the prospect of the same index -- which
    leaves that prospect still "available": availability is read from
    `draft_selections`, which this helper does not write).

    `teams` maps a code to a team that already exists; every other code in `picks` gets
    a new AI-managed team unless `managers` names a person for it. `auto_pilot` names
    which of `managers`' codes are ALSO on auto-pilot (GM-1, docs/design/gm-command-
    surface.md) -- a code in `auto_pilot` with no entry in `managers` is a builder
    error the assertion below catches rather than silently drafting for the AI on a
    manager-less row. Real enough for the real DRAFT hook to run over (cap settings,
    philosophy, timeline, ratings)."""
    managers = managers or {}
    assert auto_pilot <= managers.keys(), "a code cannot be on auto-pilot with no manager"
    teams = dict(teams or {})
    if await league_repo.get_cap_settings(pool, league_id, SEASON) is None:
        await league_repo.create_cap_settings(
            pool,
            league_id=league_id,
            season=SEASON,
            salary_cap=150_000_000,
            luxury_tax=180_000_000,
            apron_1=190_000_000,
            apron_2=200_000_000,
            max_pct_0_6=25,
            max_pct_7_9=30,
            max_pct_10_plus=35,
            supermax_pct=35,
            min_salary=1_200_000,
            mle_amount=13_000_000,
            bi_annual_amount=5_000_000,
            raise_pct_bird=8,
            raise_pct_non_bird=5,
            roster_min=14,
            roster_max=15,
        )
    for code in dict.fromkeys(code for _, _, code in picks):
        if code in teams:
            continue
        team = await team_repo.create_team(
            pool,
            league_id=league_id,
            code=code,
            name=f"{code} Team",
            city=f"{code} City",
            conference="East",
            division="Atlantic",
            home_market_size=3,
            manager_user_id=managers.get(code),
            gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER,
            timeline=Timeline.RETOOL,
        )
        teams[code] = team.id
        if code in auto_pilot:
            await team_repo.set_auto_pilot(
                pool, team.id, auto_pilot=True, gm_philosophy=GmPhilosophy.YOUTH_DEVELOPER
            )
    draft = (
        await draft_repo.create_draft(
            pool, league_id=league_id, season=SEASON, total_picks=len(picks)
        )
        if draft_row
        else None
    )
    pick_rows = []
    for round_, pick_number, code in picks:
        pick_rows.append(
            await draft_repo.create_draft_pick(
                pool,
                league_id=league_id,
                season=SEASON,
                round=round_,
                original_team_id=teams[code],
                current_team_id=teams[code],
                pick_number=pick_number,
            )
        )
    prospect_ids = []
    for i in range(prospects):
        player = await player_repo.create_player(
            pool,
            league_id=league_id,
            first_name="Prospect",
            last_name=f"Number{i}",
            primary_position=Position.SF,
            height_in=79,
            weight_lb=215,
            wingspan_in=82,
            birth_date=birth_date_for_age(19, SEASON),
            roster_status=RosterStatus.DRAFT_ELIGIBLE,
            overall_cached=70 - i,
            overall_computed_at=datetime.now(UTC),
            team_id=None,
        )
        base = make_ratings(player.id)
        await ratings_repo.upsert_player_ratings(
            pool,
            player_id=player.id,
            league_id=league_id,
            source=RatingSource.GENERATED,
            ratings={
                f.name: getattr(base, f.name)
                for f in dataclasses.fields(base)
                if f.name not in _RATINGS_NON_COLUMN_FIELDS
            },
        )
        prospect_ids.append(player.id)
    for index in used:
        await draft_repo.set_used_for_player(pool, pick_rows[index].id, prospect_ids[index])
    return DraftBoard(
        teams=teams,
        picks=tuple(pick_rows),
        prospects=tuple(prospect_ids),
        draft_id=None if draft is None else draft.id,
    )
