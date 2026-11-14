"""Standalone dev tool: sim one game entirely in memory between two
hand-picked real-player rosters pulled from `seeds/real_players/<season>.json`,
and render the result as a static HTML box score -- a real-data sibling of
`box_score_visual.py` (which uses fully synthetic fixture players). Exists
to let the user eyeball engine behavior against real rosters/real ratings
without needing a Postgres-backed dev league.

`seeds/real_players/<season>.json` carries no team/roster info (see
`tools/seed_dev_league.py`'s own module docstring) -- so which real players
go on which side is hand-picked below, not derived from the data. Uses the
same pure functions the DB-backed hydration path (`tools/seed_dev_league.py
::_create_real_player`) calls -- `sim.ratings.compute_overall` and
`sim.derivation.derive_tendencies` -- just skipping the database round-trip
entirely, since nothing here needs to persist.

No `PlayerRole` is assigned to anyone (no real per-player touch-share data
exists), so `game._minutes_shares` falls through to its per-absolute-rank
minutes curve (RC-4, `docs/design/auto-gm-role-construction.md`) for every
player on both rosters, ranked by `overall_cached` -- stars naturally get
more minutes because their real ratings compute a high overall and land
them near the top of that rank curve, not because of any hand-tuned role
assignment.

Usage: python tools/box_score_real_matchup.py [--season 2016] [--seed N] [--out PATH]
       [--allow-legacy-ratings]  (seasons 1996-2008 are legacy ratings and refused otherwise)
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from random import Random

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import json  # noqa: E402

from dba2.domain.player import Player, Position, RosterStatus  # noqa: E402
from dba2.domain.ratings import PlayerRatings, PlayerTendencies, RatingSource  # noqa: E402
from dba2.domain.team import (  # noqa: E402
    DefensiveScheme,
    GmPhilosophy,
    OffensiveScheme,
    Team,
    TeamStrategy,
    Timeline,
)
from dba2.sim.context import GameContext, build_sim_player, build_sim_team  # noqa: E402
from dba2.sim.derivation import derive_tendencies  # noqa: E402
from dba2.sim.game import simulate_game  # noqa: E402
from dba2.sim.ratings import compute_overall  # noqa: E402
from dba2.sim.tuning import DEFAULT_TUNING  # noqa: E402
from tools._real_fields import require_builder_version  # noqa: E402
from tools.box_score_visual import render_html  # noqa: E402

_REAL_PLAYERS_DIR = _REPO_ROOT / "seeds" / "real_players"

# 2016-17 champion Warriors (Durant's first season) vs. that year's Finals
# opponent Cavaliers, both real 10-man rotations, by (first, last) exactly
# as they appear in seeds/real_players/2016.json.
_WARRIORS = [
    ("Stephen", "Curry"),
    ("Klay", "Thompson"),
    ("Kevin", "Durant"),
    ("Draymond", "Green"),
    ("Andre", "Iguodala"),
    ("Zaza", "Pachulia"),
    ("Shaun", "Livingston"),
    ("David", "West"),
    ("JaVale", "McGee"),
    ("Patrick", "McCaw"),
]
_CAVALIERS = [
    ("LeBron", "James"),
    ("Kyrie", "Irving"),
    ("Kevin", "Love"),
    ("JR", "Smith"),
    ("Tristan", "Thompson"),
    ("Channing", "Frye"),
    ("Iman", "Shumpert"),
    ("Kyle", "Korver"),
    ("Deron", "Williams"),
    ("Richard", "Jefferson"),
]

_DRAFT_AGE_ANCHOR = 21  # matches tools/seed_dev_league.py's own convention


@dataclass(frozen=True)
class RealTeamSpec:
    team_id: int
    code: str
    name: str
    city: str
    conference: str
    division: str
    roster: list[tuple[str, str]]


_HOME = RealTeamSpec(1, "GSW", "Warriors", "Golden State", "West", "Pacific", _WARRIORS)
_AWAY = RealTeamSpec(2, "CLE", "Cavaliers", "Cleveland", "East", "Central", _CAVALIERS)


def _load_real_player_pool(season: int, *, allow_legacy_ratings: bool = False) -> list[dict]:
    """The season's real-player rows; refuses one that is not RPD-8 builder
    version 2 unless `allow_legacy_ratings` (legacy ratings compress stars)."""
    path = _REAL_PLAYERS_DIR / f"{season}.json"
    if not path.exists():
        raise ValueError(f"no real-player seed file for season {season} ({path})")
    rows = json.loads(path.read_text(encoding="utf-8"))["players"]
    require_builder_version(rows, season, allow_legacy_ratings)
    return rows


def _find(pool: list[dict], first: str, last: str) -> dict:
    for p in pool:
        if p["first_name"] == first and p["last_name"] == last:
            return p
    raise ValueError(f"{first} {last} not found in this season's real-player pool")


def _effective_draft_year(real_player: dict, *, season: int) -> int:
    draft_year = real_player["draft_year"]
    return draft_year if draft_year is not None else season


def _birth_date(real_player: dict, *, season: int) -> date:
    effective_draft_year = _effective_draft_year(real_player, season=season)
    return date(effective_draft_year - _DRAFT_AGE_ANCHOR, 6, 15)


def _build_sim_team(spec: RealTeamSpec, pool: list[dict], *, season: int, rng_seed: int):
    team = Team(
        id=spec.team_id,
        league_id=1,
        code=spec.code,
        name=spec.name,
        city=spec.city,
        conference=spec.conference,
        division=spec.division,
        manager_user_id=None,
        gm_philosophy=GmPhilosophy.TENDENCY_RESPECTER,
        timeline=Timeline.WIN_NOW,
        home_market_size=5,
    )
    strategy = TeamStrategy(
        league_id=1,
        team_id=spec.team_id,
        season=season,
        pace_target=50,
        offensive_scheme=OffensiveScheme.MOTION,
        defensive_scheme=DefensiveScheme.DROP,
        help_aggression=50,
        three_point_bias=50,
        crash_oreb_bias=50,
        set_by=None,
    )

    sim_players = []
    for slot_index, (first, last) in enumerate(spec.roster):
        real_player = _find(pool, first, last)
        player_id = spec.team_id * 1000 + slot_index
        position = Position(real_player["primary_position"])

        ratings = PlayerRatings(
            player_id=player_id,
            league_id=1,
            source=RatingSource.REAL_STAT_DERIVED,
            derived_from_season=real_player["derived_from_season"],
            confidence=Decimal(str(real_player["confidence"])),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            **real_player["ratings"],
        )
        overall = compute_overall(ratings, position)

        player = Player(
            id=player_id,
            league_id=1,
            first_name=real_player["first_name"],
            last_name=real_player["last_name"],
            external_ref=real_player["external_ref"],
            primary_position=position,
            secondary_position=None,
            height_in=real_player["height_in"],
            weight_lb=real_player["weight_lb"],
            # RPC-10: the row's own estimate when it stores one (version-2 seeds)
            wingspan_in=real_player.get("wingspan_in", real_player["height_in"] + 2),
            birth_date=_birth_date(real_player, season=season),
            draft_year=real_player["draft_year"],
            draft_pick_number=None,
            years_pro=max(0, season - _effective_draft_year(real_player, season=season)),
            team_id=spec.team_id,
            roster_status=RosterStatus.ACTIVE,
            overall_cached=overall,
            overall_computed_at=datetime(2026, 1, 1, tzinfo=UTC),
            retired_in_season=None,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

        tendencies_rng = Random(rng_seed + player_id)
        tendencies_values = derive_tendencies(ratings, position, rng=tendencies_rng)
        tendencies = PlayerTendencies(
            player_id=player_id,
            league_id=1,
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            **tendencies_values,
        )

        sim_players.append(
            build_sim_player(player, ratings, tendencies, DEFAULT_TUNING, role=None, directive=None)
        )

    return build_sim_team(team, strategy, tuple(sim_players))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=2016)
    parser.add_argument("--seed", type=int, default=777)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--allow-legacy-ratings",
        action="store_true",
        help=(
            "Accept a season whose seed rows are not RPD-8 builder version 2 "
            "(1996-2008) instead of refusing; legacy ratings compress stars toward 70."
        ),
    )
    args = parser.parse_args()

    out_path = args.out or (
        _REPO_ROOT / "tools" / "output" / f"real_matchup_{args.season}_seed{args.seed}.html"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pool = _load_real_player_pool(args.season, allow_legacy_ratings=args.allow_legacy_ratings)
    home = _build_sim_team(_HOME, pool, season=args.season, rng_seed=args.seed)
    away = _build_sim_team(_AWAY, pool, season=args.season, rng_seed=args.seed)
    context = GameContext(
        game_id=1, season=args.season, home=home, away=away, tuning=DEFAULT_TUNING
    )

    result = simulate_game(context, rng_seed=args.seed)
    out_path.write_text(render_html(context, result, args.seed), encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
