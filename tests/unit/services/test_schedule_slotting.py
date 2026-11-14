"""Date-slotting fairness (docs/design/schedule-date-fairness.md, SDF-1/SDF-3/SDF-6).

Pure, no DB -- `build_matchups`, `_flat_instances`, `_assign_home_away` and
`_slot_dates` are all module-level functions over in-memory `Team` rows, so the
real 30-team pipeline can be run and measured here directly instead of through
`generate_regular_season`'s persistence layer.

Teams are built from `seeds/teams.json` (the real conference/division data
`tests/integration/test_schedule_service.py` also exercises through
`league_service.create_league`), not hand-built, so the multiset and the
matchup shape driving `_slot_dates` are the real ones, not a toy case.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from dba2.domain.team import GmPhilosophy, Team, Timeline
from dba2.services import schedule_service

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEAMS_JSON = _REPO_ROOT / "seeds" / "teams.json"
_SEASON = 2026

# SDF-3's acceptance bound. Measured against the real 30-team set below: worst
# spread is 2 (recorded in the assertion), so 3 is not aspirational headroom --
# a future regression to spread 3 would still pass. Do not loosen this if a
# future change pushes the measured spread up; see the design doc's SDF-3.
_MAX_ACCEPTABLE_SPREAD = 3


def _load_teams_meta() -> list[dict]:
    payload = json.loads(_TEAMS_JSON.read_text(encoding="utf-8"))
    return payload["teams"]


def _build_teams(teams_meta: list[dict]) -> list[Team]:
    """Ids assigned 1..30 by position in `teams_meta` -- the caller controls
    ordering (file order, or reversed for the relabel-invariance test)."""
    return [
        Team(
            id=i + 1,
            league_id=1,
            code=meta["code"],
            name=meta["name"],
            city=meta["city"],
            conference=meta["conference"],
            division=meta["division"],
            manager_user_id=None,
            gm_philosophy=GmPhilosophy(meta.get("gm_philosophy", "tendency_respecter")),
            timeline=Timeline(meta.get("timeline", "retool")),
            home_market_size=meta.get("home_market_size", 3),
        )
        for i, meta in enumerate(teams_meta)
    ]


def _run_pipeline(teams: list[Team]) -> list[tuple[date, int, int]]:
    matchups = schedule_service.build_matchups(teams)
    instances = schedule_service._flat_instances(matchups)
    homes_and_aways = schedule_service._assign_home_away(instances)
    dated = schedule_service._slot_dates(homes_and_aways, _SEASON)
    dated.sort(key=lambda row: (row[0], row[1], row[2]))
    return dated


def _max_spread(dated: list[tuple[date, int, int]], team_ids: set[int]) -> int:
    """Max-min games-played spread across all teams, at every distinct date
    (SDF-3), also asserting the two secondary structural invariants a spread
    computation needs to be meaningful: no team twice on a date, and no date
    over the per-day cap."""
    by_date: dict[date, list[tuple[int, int]]] = defaultdict(list)
    for game_date, home, away in dated:
        by_date[game_date].append((home, away))

    played: dict[int, int] = defaultdict(int)
    worst = 0
    for game_date in sorted(by_date):
        games_today = by_date[game_date]
        assert len(games_today) <= schedule_service._MAX_GAMES_PER_DAY, game_date
        teams_today: list[int] = []
        for home, away in games_today:
            teams_today.append(home)
            teams_today.append(away)
        assert len(set(teams_today)) == len(teams_today), (
            f"a team appears twice on {game_date}"
        )
        for home, away in games_today:
            played[home] += 1
            played[away] += 1
        counts = [played.get(t, 0) for t in team_ids]
        worst = max(worst, max(counts) - min(counts))
    return worst


def test_fairness_spread_and_structure_on_the_real_thirty_team_set():
    teams = _build_teams(_load_teams_meta())
    team_ids = {t.id for t in teams}
    dated = _run_pipeline(teams)

    assert len(dated) == 1230
    assert {t for _, home, away in dated for t in (home, away)} <= team_ids

    games_per_team: dict[int, int] = defaultdict(int)
    for _, home, away in dated:
        games_per_team[home] += 1
        games_per_team[away] += 1
    assert games_per_team == {t: 82 for t in team_ids}

    # Measured against the real set (2026-09-22): worst spread is 2. Pinned at
    # <=3 (SDF-3's requirement, not the 2 measured -- 2 leaves no headroom to
    # tune against, 3 is the actual acceptance bound).
    worst = _max_spread(dated, team_ids)
    assert worst == 2, f"measured spread {worst}, expected 2 -- update the comment if this moved"
    assert worst <= _MAX_ACCEPTABLE_SPREAD

    assert dated[0][0] == date(_SEASON, 10, 21)


def test_relabel_invariance_spread_stays_low_under_a_different_id_assignment():
    """The direct regression guard for the original bug: date assignment must
    not be a function of id order. Old code: spread 28 under team-id order
    (measured live, league 543). Reversing which file-order team gets which id
    must not resurrect that -- both orderings must stay <= the same bound."""
    meta = _load_teams_meta()
    reversed_teams = _build_teams(list(reversed(meta)))
    team_ids = {t.id for t in reversed_teams}
    dated = _run_pipeline(reversed_teams)

    assert len(dated) == 1230
    worst = _max_spread(dated, team_ids)
    assert worst <= _MAX_ACCEPTABLE_SPREAD, (
        f"relabeled team order produced spread {worst}, expected <= {_MAX_ACCEPTABLE_SPREAD}"
    )


def test_determinism_two_runs_and_two_team_constructions_agree():
    meta = _load_teams_meta()
    first = _run_pipeline(_build_teams(meta))
    second = _run_pipeline(_build_teams(meta))  # fresh Team objects, same data
    assert first == second

    # Re-running the exact same team list a second time in the same process.
    teams = _build_teams(meta)
    third = _run_pipeline(teams)
    fourth = _run_pipeline(teams)
    assert third == fourth
    assert first == third
