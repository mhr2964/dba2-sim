"""`generate_regular_season` -- the 1,230-game regular-season schedule
(phase-1-simulation-engine.md §8.3, Decision P1-27).

**Opponent multiset (the real NBA formula, per Decision P1-27):** 4 games
against each of 4 division rivals (16), 4 games against 6 conference
non-division opponents and 3 against the other 4 (36), 2 games against each
of the 15 inter-conference opponents (30) -- 82 games/team, 1,230 games/
league (30 * 82 / 2).

**Which 6-of-10 conference non-division opponents get 4 games, stated
explicitly because the real NBA's answer (a schedule-makers' rotation with
no closed formula) isn't reproducible here and doesn't need to be --
Decision P1-27 only requires the *opponent-count multiset* to be correct,
not historical fidelity.** Divisions within a conference are indexed 0-2
(sorted by division name) and teams within a division are indexed 0-4
(sorted by team code). For two divisions A (lower index) and B (higher
index), a team at index `i` in A and a team at index `j` in B are "4-game"
partners iff `(j - i) % 5 in {0, 1, 2}` -- a circulant bipartite relation
that gives every team in A exactly 3 four-game partners in B *and* every
team in B exactly 3 four-game partners in A (proof: fixing `i`, 3 of 5 `j`
values satisfy the condition; fixing `j`, the condition rearranges to
`(i - j) % 5 in {0, 4, 3}`, also 3 of 5 `i` values). Each team touches two
other divisions, so 3 + 3 = 6 four-game partners and 2 + 2 = 4 three-game
partners, exactly Decision P1-27's split, and the relation is symmetric by
construction so it never needs a separate "does B agree with A" check.

**Home/away balance:** games are generated in round-robin-interleaved order
(round `r` includes every matchup that still owes a game at count `r`) and
home is assigned greedily to whichever of the two teams has the fewer home
games scheduled so far, tie-broken by total-games-scheduled-so-far parity.
This is the standard "assign to the underdog" balancing rule and keeps
every team within a few games of an even 41/41 split without needing an
exact per-matchup 2-2 split (which the odd-count division/conference
matchups can't have anyway).

**Date slotting (SDF-1 option C, docs/design/schedule-date-fairness.md):**
one calendar day is filled at a time rather than one game at a time. Every
day, the still-unplaced `(home, away)` pairs are ordered by `(min(games
played so far by home, games played so far by away), games played so far
by home PLUS games played so far by away, original pool position)` -- the
team that is behind plays first in line; among pairs tied on that, the
pair that is collectively furthest behind plays first; only then does
`_assign_home_away`'s own output order break a remaining tie -- and taken
off that ordering in turn, skipping any pair that would repeat a team
already used on that day, until the day holds `_MAX_GAMES_PER_DAY` games
or no eligible pair is left. Games-played counts are frozen for the whole
day: every pair that day is scored against the counts as they stood at
the day's start, not re-scored after each placement within the same day.
There is no "next free day" pointer -- "not twice in a day" is enforced
entirely by that day's own used-team set. This replaced a single forward
pass keyed only by team id order, which let the first-id team play ~29
straight days from opening night while the last-id teams waited (measured
live, league 543: a ~28-game division-by-division staircase). **The
middle (sum) term is load-bearing, not a tie-break nicety**: dropping it
(so ties fall straight to the id-correlated pool position) measured a
worst spread of 14 across 124 dates (2 short); with it restored, the
worst max-min games-played spread across all 30 teams, at any date, is 2
across 123 full dates (requirement <= 3; see the design doc's SDF-3 for
the acceptance test and SDF-1 for why the sum term is what actually
closes the gap the two-part key leaves open). The resulting per-game day
indices are then sorted to produce `game_index` in chronological order,
which is what Decision P1-21's `rng_for(league.rng_seed, season,
game_index)` derivation depends on.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta

from dba2.core.errors import NotFoundError, ValidationError
from dba2.core.rng import rng_for
from dba2.data.db import Executor, transaction
from dba2.data.repositories import game_repo, league_repo, team_repo
from dba2.domain.game import GameType
from dba2.domain.team import Team

_TEAMS_PER_LEAGUE = 30
_DIVISION_GAMES = 4
_CONFERENCE_FOUR_GAME_COUNT = 4
_CONFERENCE_THREE_GAME_COUNT = 3
_INTER_CONFERENCE_GAMES = 2
_EXPECTED_GAMES_PER_TEAM = 82

# Games/day cap for the date-slotting pass. 30 teams means at most 15 games
# could ever share a day without double-booking a team; deliberately set
# well below that so the 1,230-game season spreads across a realistic
# season-length number of calendar days rather than a dense sub-100-day
# block -- a cosmetic choice, not a modeled rest/travel rule (Decision
# P1-27 explicitly excludes those).
_MAX_GAMES_PER_DAY = 10

# Placeholder season-opening date (mid-to-late October, matching a real NBA
# opening night) -- Decision P1-27 has no stated calendar requirement beyond
# "slot dates," so this is a fixed, documented anchor rather than a
# configurable input nothing downstream reads yet.
_SEASON_START_MONTH_DAY = (10, 21)


def _division_key(team: Team) -> tuple[str, str]:
    return (team.conference, team.division)


def _build_indices(
    teams: Sequence[Team],
) -> tuple[dict[tuple[str, str], int], dict[int, int]]:
    """Returns (division_index_within_conference, team_index_within_division).

    Division index is 0-2, assigned by sorting division names within each
    conference alphabetically. Team index is 0-4, assigned by sorting team
    codes within each division alphabetically. Both are arbitrary but
    deterministic -- the circulant rule above only needs *a* stable
    ordering, not a particular one.
    """
    by_conference: dict[str, set[str]] = defaultdict(set)
    for t in teams:
        by_conference[t.conference].add(t.division)

    division_index: dict[tuple[str, str], int] = {}
    for conference, divisions in by_conference.items():
        for idx, division in enumerate(sorted(divisions)):
            division_index[(conference, division)] = idx

    by_division: dict[tuple[str, str], list[Team]] = defaultdict(list)
    for t in teams:
        by_division[_division_key(t)].append(t)

    team_index: dict[int, int] = {}
    for roster in by_division.values():
        for idx, t in enumerate(sorted(roster, key=lambda x: x.code)):
            team_index[t.id] = idx

    return division_index, team_index


def _four_game_conference_pair(
    team_a: Team,
    team_b: Team,
    division_index: dict[tuple[str, str], int],
    team_index: dict[int, int],
) -> bool:
    """The circulant rule from the module docstring, applied with a fixed
    "lower division index plays the role of A" orientation so the result
    doesn't depend on which of the two teams is passed first."""
    div_a = division_index[_division_key(team_a)]
    div_b = division_index[_division_key(team_b)]
    i_a = team_index[team_a.id]
    i_b = team_index[team_b.id]
    if div_a < div_b:
        return (i_b - i_a) % 5 in (0, 1, 2)
    return (i_a - i_b) % 5 in (0, 1, 2)


def _games_between(
    team_a: Team,
    team_b: Team,
    division_index: dict[tuple[str, str], int],
    team_index: dict[int, int],
) -> int:
    if team_a.conference != team_b.conference:
        return _INTER_CONFERENCE_GAMES
    if team_a.division == team_b.division:
        return _DIVISION_GAMES
    if _four_game_conference_pair(team_a, team_b, division_index, team_index):
        return _CONFERENCE_FOUR_GAME_COUNT
    return _CONFERENCE_THREE_GAME_COUNT


def build_matchups(teams: Sequence[Team]) -> list[tuple[int, int, int]]:
    """Every unordered team pair with its game count, per Decision P1-27's
    formula. Public so a unit/integration test can assert the multiset
    directly without regenerating a full schedule. Sorted by
    `(team_a_id, team_b_id)` for determinism."""
    if len(teams) != _TEAMS_PER_LEAGUE:
        raise ValidationError(
            f"generate_regular_season expects exactly {_TEAMS_PER_LEAGUE} teams, "
            f"got {len(teams)}"
        )
    division_index, team_index = _build_indices(teams)
    sorted_teams = sorted(teams, key=lambda t: t.id)
    matchups: list[tuple[int, int, int]] = []
    for pos, team_a in enumerate(sorted_teams):
        for team_b in sorted_teams[pos + 1 :]:
            n = _games_between(team_a, team_b, division_index, team_index)
            matchups.append((team_a.id, team_b.id, n))
    return matchups


def _flat_instances(matchups: Sequence[tuple[int, int, int]]) -> list[tuple[int, int]]:
    """Round-robin interleave: round `r` contributes one (a, b) instance for
    every matchup that still owes a game at count `r`, so repeat matchups
    are spread across the whole ordering rather than clustered together."""
    if not matchups:
        return []
    max_n = max(n for _, _, n in matchups)
    instances: list[tuple[int, int]] = []
    for round_idx in range(max_n):
        for team_a, team_b, n in matchups:
            if n > round_idx:
                instances.append((team_a, team_b))
    return instances


def _assign_home_away(instances: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Greedy "assign home to the team with fewer home games so far" over
    the round-robin-ordered instance list (module docstring's "home/away
    balance" section). Returns a list of (home_team_id, away_team_id)."""
    home_count: dict[int, int] = defaultdict(int)
    total_count: dict[int, int] = defaultdict(int)
    result: list[tuple[int, int]] = []
    for team_a, team_b in instances:
        if home_count[team_a] < home_count[team_b]:
            home, away = team_a, team_b
        elif home_count[team_b] < home_count[team_a]:
            home, away = team_b, team_a
        else:
            home, away = (team_a, team_b) if total_count[team_a] % 2 == 0 else (team_b, team_a)
        home_count[home] += 1
        total_count[team_a] += 1
        total_count[team_b] += 1
        result.append((home, away))
    return result


def _slot_dates(games: Sequence[tuple[int, int]], season: int) -> list[tuple[date, int, int]]:
    """Day-at-a-time fill (SDF-1 option C): every calendar day, order the
    still-unplaced pool by `(min(played[home], played[away]), played[home] +
    played[away], original pool position)` -- the team that is behind plays
    first; among pairs tied on that, the pair that is collectively furthest
    behind plays first; only then does pool order break a remaining tie --
    and take candidates off that ordering, skipping any that would repeat a
    team already used today, until the day holds `_MAX_GAMES_PER_DAY` games
    or no eligible candidate is left. `played` counts are frozen for the
    whole day (scored once at the day's start, not re-scored after each
    placement). A day always starts empty, so the first candidate in that
    day's ordering always fits -- at least one game is placed per day, so
    this terminates in at most `len(games)` days. Returns (game_date, home,
    away), NOT yet sorted into final chronological order -- the caller
    re-sorts by date.

    **The middle (sum) term is required, not a tie-break nicety** (design
    doc SDF-1's revision-2 correction): dropping it and breaking ties
    straight from `min` to pool position measured a worst spread of 14
    across 124 dates (2 short) on the real 30-team set -- worse than this
    fix exists to prevent, because on an early-season near-universal tie the
    fallback to pool position is exactly `_flat_instances`' round-robin
    order, itself correlated with team id. With the sum term restored the
    measured worst spread is 2 across 123 full dates."""
    season_start = date(season, *_SEASON_START_MONTH_DAY)
    played: dict[int, int] = defaultdict(int)
    pool: list[tuple[int, tuple[int, int]]] = list(enumerate(games))

    dated: list[tuple[date, int, int]] = []
    day = 0
    while pool:
        played_at_day_start = dict(played)
        pool.sort(
            key=lambda item: (
                min(played_at_day_start.get(item[1][0], 0), played_at_day_start.get(item[1][1], 0)),
                played_at_day_start.get(item[1][0], 0) + played_at_day_start.get(item[1][1], 0),
                item[0],
            )
        )
        used_today: set[int] = set()
        game_date = season_start + timedelta(days=day)
        remaining: list[tuple[int, tuple[int, int]]] = []
        placed = 0
        for pos, (home, away) in pool:
            if placed < _MAX_GAMES_PER_DAY and home not in used_today and away not in used_today:
                used_today.add(home)
                used_today.add(away)
                played[home] += 1
                played[away] += 1
                dated.append((game_date, home, away))
                placed += 1
            else:
                remaining.append((pos, (home, away)))
        pool = remaining
        day += 1
    return dated


async def generate_regular_season(ex: Executor, league_id: int, season: int) -> int:
    """Builds and persists the 1,230-game regular-season schedule for
    `league_id`/`season` and returns the count. Raises `ValidationError` if
    a schedule already exists for this league/season (regenerating on top
    of an existing one would either violate the `(league_id, season,
    game_index)` UNIQUE constraint or silently double the season -- this
    function doesn't own a re-generation workflow, matching Decision
    P1-27's scope)."""
    async with transaction(ex) as conn:
        league = await league_repo.get_league(conn, league_id)
        if league is None:
            raise NotFoundError(f"league {league_id} does not exist")

        existing = await game_repo.list_games_for_league_season(conn, league_id, season)
        if existing:
            raise ValidationError(
                f"league {league_id} season {season} already has {len(existing)} games "
                "scheduled -- generate_regular_season does not regenerate an existing schedule"
            )

        teams = await team_repo.list_teams_for_league(conn, league_id)
        matchups = build_matchups(teams)

        instances = _flat_instances(matchups)
        homes_and_aways = _assign_home_away(instances)
        dated = _slot_dates(homes_and_aways, season)
        dated.sort(key=lambda row: (row[0], row[1], row[2]))

        rows = []
        for game_index, (game_date, home_id, away_id) in enumerate(dated):
            rng_seed = rng_for(league.rng_seed, season, game_index).getrandbits(63)
            rows.append(
                {
                    "league_id": league_id,
                    "season": season,
                    "game_index": game_index,
                    "game_date": game_date,
                    "home_team_id": home_id,
                    "away_team_id": away_id,
                    "game_type": GameType.REGULAR,
                    "rng_seed": rng_seed,
                }
            )

        created = await game_repo.create_games_many(conn, rows)
        return len(created)
