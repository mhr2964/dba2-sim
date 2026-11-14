"""CLI: sim N full headless seasons and check the results against
`seeds/nba_reference_distributions.json` (docs/design/phase-1-simulation-
engine.md §10 -- "the phase's exit criterion... a deliverable, not a script
someone writes at the end").

Same shape as the league-distribution harnesses already built for steps 7/9
(`tests/unit/sim/_derivation_harness.py`, `tests/unit/sim/
test_derivation_league_distribution.py`): generate a league, run it, and
aggregate the output -- just aggregating game-level output (`sim.game.
simulate_game` results) instead of roster-generation output. Entirely
in-memory, no Postgres: reuses `sim.derivation`'s synthetic roster
generator plus `strategy_service`'s and `schedule_service`'s *pure* helper
functions (the parts of those modules that take plain data and do no I/O),
never their `async def f(ex, ...)` entry points -- `ex`/`transaction` are
never touched by this module, so it needs no live database (§12 step 10's
own verify bullet: "this needs no Postgres, per the parent design's §12
item 7 verify step").

**Runtime, stated explicitly because §10 asks for it:** a full 1,230-game
season, in memory, takes roughly 90-120s on ordinary dev hardware (measured:
~0.076s/game x 1,230 = ~94s for the possession engine alone, plus league
generation/aggregation overhead) -- well over §11's <45s *target*, but that
target is the possession engine's own performance envelope, not this tool's
scope to hit; the gap is a step-11-or-later engine-perf question, not a
harness defect. `--seasons` defaults to 1 for that reason (a single
replicate keeps a manual CLI run to roughly two minutes); pass a higher
value for the "reproduced across at least three different league seeds"
robustness check step 11 calls for, at roughly `N * 100s`.

**A "season" here means one full independent league replicate** (a fresh
30-team, `--players-per-team`-man-per-team synthetic roster plus its own
1,230-game schedule), not 82 games replayed under a different tuning
profile -- so player/team identities do not carry across replicates, and
per-player/per-team shape metrics (top-scorer PPG, win-total spread, minutes
tiers) are computed once per replicate and then averaged across replicates,
never pooled (pooling would silently treat two different synthetic
leagues' players as comparable individuals, which they are not).

**Real-player mode** (`--real-players SEASON [--real-team-rosters]`,
docs/design/real-player-rating-calibration.md RPC-13), additive and off by
default: the first `--real-players-per-team` slots of every roster (default:
all of them) are hydrated from `seeds/real_players/<SEASON>.json` exactly as
`tools/seed_dev_league.py`'s `--real-players-season` path hydrates them, so a
real-seeded league can be measured without a database. A synthetic league has
no ground truth against which "the best centre leads blocks" is wrong; this
mode adds a `real_fidelity` group that does: Spearman of each simulated per-36
rate against the player's real one from `seeds/real_players_raw/<SEASON>.json`,
the named-player block/assist anchors, and a top-10 legibility table (a real
leader's simulated rank). Real players are the same people in every replicate
(with `--real-team-rosters`), so fidelity is computed once on their totals
pooled across replicates (keyed by `external_ref`); the league-aggregate
metrics are still averaged per replicate and still compared to the synthetic
reference bands, which is where `blk_per36_max` on a real-seeded league is
read. The default (synthetic) invocation is unchanged.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from dba2.core.rng import rng_for
from dba2.domain.player import Player, Position, RosterStatus
from dba2.domain.ratings import PlayerRatings, PlayerTendencies, RatingSource
from dba2.domain.role import PlayerRole
from dba2.domain.team import (
    DefensiveScheme,
    GmPhilosophy,
    OffensiveScheme,
    Team,
    TeamStrategy,
    Timeline,
)
from dba2.services import role_service, schedule_service, strategy_service
from dba2.sim import derivation as d
from dba2.sim import ratings as sim_ratings
from dba2.sim.context import GameContext, SimPlayer, build_sim_player, build_sim_team
from dba2.sim.events import GameResult
from dba2.sim.game import simulate_game
from dba2.sim.tuning import TuningProfile
from tools import seed_dev_league
from tools._gm_real_player_report import RAW_DIR as _REAL_RAW_DIR
from tools._gm_real_player_report import load_raw_truth, spearman

# RC-23.3 (docs/design/auto-gm-role-construction.md): `--philosophy-spread`'s
# four settings, each isolating one channel `seeds/teams.json`'s uniform
# `tendency_respecter` otherwise can never exercise.
_PHILOSOPHY_SPREADS: tuple[str, ...] = ("none", "cycle", "kappa-only", "tilt-only")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEAMS_JSON = _REPO_ROOT / "seeds" / "teams.json"
_REFERENCE_JSON = _REPO_ROOT / "seeds" / "nba_reference_distributions.json"

_DEFAULT_PLAYERS_PER_TEAM = 14
_GAME_SECONDS_REGULATION = 48 * 60
_OVERTIME_SECONDS = 5 * 60

# §12 step 10's own interpretive call, named here rather than silently
# assumed: a "rotation player" (for the p90/p50/p10 PPG shape bands) is
# anyone averaging at least this many minutes/game across the season -- a
# common real-world cutoff, not a value derived from anywhere else in this
# codebase.
_ROTATION_MPG_THRESHOLD = 15.0
# block-attribution-v2.md BA-4.1's guard control pools these two positions.
_GUARD_POSITIONS = frozenset({"PG", "SG"})

# This harness's own fixed rank-based tiers (§12 step 10's "starter/
# rotation-bench/deep-bench mpg" reference metrics). `sim.game` no longer
# has a 5/3/rest split of its own -- RC-4 replaced that flat tier model
# with a 15-entry per-absolute-rank minutes curve, and the dependency runs
# the other way now: the curve's own absolute rank bands are keyed to
# these fixed harness tier sizes (`docs/design/auto-gm-role-construction.md`
# RC-4.1), not the reverse, so a mismatch here is still a real
# minutes-model finding, not a definitional artifact.
_STARTER_TIER_SIZE = 5
_ROTATION_TIER_SIZE = 3


# ---------------------------------------------------------------------------
# League generation (in-memory `Team`/`Player`/`PlayerRatings`/
# `PlayerTendencies`/`TeamStrategy` -- the same shape `tools/
# seed_dev_league.py` persists, minus every repository call).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeagueData:
    league_id: int
    season: int
    teams: tuple[Team, ...]
    players_by_team: Mapping[int, tuple[Player, ...]]
    ratings_by_player: Mapping[int, PlayerRatings]
    tendencies_by_player: Mapping[int, PlayerTendencies]
    strategy_by_team: Mapping[int, TeamStrategy]
    roles_by_player: Mapping[int, PlayerRole] = field(default_factory=dict)
    """RC-23.1 (docs/design/auto-gm-role-construction.md): empty unless
    `--construct-roles` populates it (`simulate_season`'s own docstring).
    Threaded into `build_context` exactly like every other per-player
    mapping here."""


def _load_teams_meta() -> list[dict]:
    payload = json.loads(_TEAMS_JSON.read_text(encoding="utf-8"))
    return payload["teams"]


# RC-23.3's `cycle`/`kappa-only`/`tilt-only` settings, each a function of a
# team's 0-indexed position in `seeds/teams.json`.
_CYCLE_PHILOSOPHIES: tuple[GmPhilosophy, ...] = tuple(GmPhilosophy)


def _philosophy_for_team(
    team_index: int, *, philosophy_spread: str, default: GmPhilosophy
) -> GmPhilosophy:
    if philosophy_spread == "none":
        return default
    if philosophy_spread == "cycle":
        return _CYCLE_PHILOSOPHIES[team_index % len(_CYCLE_PHILOSOPHIES)]
    if philosophy_spread == "kappa-only":
        return GmPhilosophy.STAR_MAXER if team_index % 2 == 0 else GmPhilosophy.EGALITARIAN
    if philosophy_spread == "tilt-only":
        return (
            GmPhilosophy.TENDENCY_RESPECTER if team_index % 2 == 0 else GmPhilosophy.DEFENSE_FIRST
        )
    raise ValueError(
        f"unknown --philosophy-spread {philosophy_spread!r}, expected one of {_PHILOSOPHY_SPREADS}"
    )


def _build_teams(
    teams_meta: Sequence[dict], *, league_id: int, philosophy_spread: str = "none"
) -> tuple[Team, ...]:
    """`philosophy_spread` (RC-23.3): overrides `seeds/teams.json`'s own
    uniform `gm_philosophy` for the harness only -- editing the seed file
    itself was rejected (RC-23.3's own text: it is read by production
    league creation and `seed_dev_league.py`, not just this harness, and
    varying it there would destroy the `none` control case)."""
    return tuple(
        Team(
            id=i + 1,
            league_id=league_id,
            code=meta["code"],
            name=meta["name"],
            city=meta["city"],
            conference=meta["conference"],
            division=meta["division"],
            manager_user_id=None,
            gm_philosophy=_philosophy_for_team(
                i,
                philosophy_spread=philosophy_spread,
                default=GmPhilosophy(meta.get("gm_philosophy", "tendency_respecter")),
            ),
            timeline=Timeline(meta.get("timeline", "retool")),
            home_market_size=meta.get("home_market_size", 3),
        )
        for i, meta in enumerate(teams_meta)
    )


def _slot_tier(slot_index: int) -> d.RosterTier:
    if slot_index < len(d.TIER_BY_SLOT):
        return d.TIER_BY_SLOT[slot_index]
    return d.TIER_BY_SLOT[-1]


def _generate_team_roster(
    team: Team,
    *,
    team_index: int,
    league_seed: int,
    players_per_team: int,
    current_season: int,
    real_rows: Sequence[Mapping] = (),
) -> tuple[list[Player], dict[int, PlayerRatings], dict[int, PlayerTendencies]]:
    """One team's synthetic roster, per the same per-slot pipeline `tools/
    seed_dev_league.py::_create_synthetic_player` uses (physicals -> talent
    -> ratings -> `compute_overall` -> `derive_tendencies`), minus every
    `player_repo`/`ratings_repo` write -- `Player.id` is assigned locally
    (`team_index * 1000 + slot_index`, globally unique across a 30-team
    league) rather than by a database sequence.

    `real_rows` (RPC-13, empty by default) take the first `len(real_rows)`
    slots, hydrated as `seed_dev_league._create_real_player` does; every other
    slot draws from its own `(team_index, slot_index)` stream exactly as if
    none were real, the same property `seed_dev_league` documents."""
    team_rng = rng_for(league_seed, "synthetic", "team", team_index)
    team_offset = team_rng.gauss(0.0, 2.5)
    positions = d.synthetic_positions(team_rng)

    players: list[Player] = []
    ratings_by_player: dict[int, PlayerRatings] = {}
    tendencies_by_player: dict[int, PlayerTendencies] = {}

    for slot_index in range(players_per_team):
        player_id = team_index * 1000 + slot_index
        if slot_index < len(real_rows):
            player, ratings, tendencies = _real_player_slot(
                team,
                player_id=player_id,
                team_index=team_index,
                slot_index=slot_index,
                row=real_rows[slot_index],
                league_seed=league_seed,
                current_season=current_season,
            )
            players.append(player)
            ratings_by_player[player_id] = ratings
            tendencies_by_player[player_id] = tendencies
            continue
        position = positions[slot_index % len(positions)]
        tier = _slot_tier(slot_index)

        player_rng = rng_for(league_seed, "synthetic", "player", team_index, slot_index)
        physicals = d.synthetic_physicals(position, player_rng)
        talent = d.tier_talent(tier, team_offset, player_rng)
        ratings_dict = d.synthetic_ratings(talent, position, player_rng)
        full_ratings = {**ratings_dict, "peak_age_start": 26, "peak_age_end": 30}

        ratings = PlayerRatings(
            player_id=player_id,
            league_id=team.league_id,
            source=RatingSource.GENERATED,
            derived_from_season=None,
            confidence=Decimal("0.85"),
            updated_at=datetime(current_season, 1, 1, tzinfo=UTC),
            **full_ratings,
        )
        overall_cached = sim_ratings.compute_overall(ratings, position)

        player = Player(
            id=player_id,
            league_id=team.league_id,
            first_name=f"Player{player_id}",
            last_name=f"Team{team.id}",
            external_ref=None,
            primary_position=position,
            secondary_position=None,
            height_in=physicals.height_in,
            weight_lb=physicals.weight_lb,
            wingspan_in=physicals.wingspan_in,
            birth_date=date(current_season - 25, 1, 1),
            draft_year=None,
            draft_pick_number=None,
            years_pro=5,
            team_id=team.id,
            roster_status=RosterStatus.ACTIVE,
            overall_cached=overall_cached,
            overall_computed_at=datetime(current_season, 1, 1, tzinfo=UTC),
            retired_in_season=None,
            created_at=datetime(current_season, 1, 1, tzinfo=UTC),
            updated_at=datetime(current_season, 1, 1, tzinfo=UTC),
        )

        tendencies_rng = rng_for(league_seed, "tendencies", team_index, slot_index)
        tendencies_values = d.derive_tendencies(ratings, position, rng=tendencies_rng)
        tendencies = PlayerTendencies(
            player_id=player_id,
            league_id=team.league_id,
            updated_at=datetime(current_season, 1, 1, tzinfo=UTC),
            **tendencies_values,
        )

        players.append(player)
        ratings_by_player[player_id] = ratings
        tendencies_by_player[player_id] = tendencies

    return players, ratings_by_player, tendencies_by_player


def _real_player_slot(
    team: Team,
    *,
    player_id: int,
    team_index: int,
    slot_index: int,
    row: Mapping,
    league_seed: int,
    current_season: int,
) -> tuple[Player, PlayerRatings, PlayerTendencies]:
    """One real player from a `seeds/real_players/<season>.json` row, in memory:
    `seed_dev_league._create_real_player` minus every repository call (same
    wingspan, birth date, years pro, ratings block and tendencies stream)."""
    position = Position(row["primary_position"])
    stamp = datetime(current_season, 1, 1, tzinfo=UTC)
    ratings = PlayerRatings(
        player_id=player_id,
        league_id=team.league_id,
        source=RatingSource.REAL_STAT_DERIVED,
        derived_from_season=row["derived_from_season"],
        confidence=Decimal(str(row["confidence"])),
        updated_at=stamp,
        **row["ratings"],
    )
    player = Player(
        id=player_id,
        league_id=team.league_id,
        first_name=row["first_name"],
        last_name=row["last_name"],
        external_ref=str(row["external_ref"]),
        primary_position=position,
        secondary_position=None,
        height_in=row["height_in"],
        weight_lb=row["weight_lb"],
        wingspan_in=seed_dev_league.real_player_wingspan_in(
            dict(row), rng_seed=league_seed, team_index=team_index, slot_index=slot_index
        ),
        birth_date=seed_dev_league._real_player_birth_date(dict(row), season=current_season),
        draft_year=row["draft_year"],
        draft_pick_number=None,
        years_pro=seed_dev_league._real_player_years_pro(dict(row), season=current_season),
        team_id=team.id,
        roster_status=RosterStatus.ACTIVE,
        overall_cached=sim_ratings.compute_overall(ratings, position),
        overall_computed_at=stamp,
        retired_in_season=None,
        created_at=stamp,
        updated_at=stamp,
    )
    tendencies_rng = rng_for(league_seed, "tendencies", team_index, slot_index)
    tendencies = PlayerTendencies(
        player_id=player_id,
        league_id=team.league_id,
        updated_at=stamp,
        **d.derive_tendencies(ratings, position, rng=tendencies_rng),
    )
    return player, ratings, tendencies


def select_real_rosters(
    season: int,
    teams: Sequence[Team],
    *,
    league_seed: int,
    per_team: int,
    real_team_rosters: bool,
) -> dict[int, list[dict]]:
    """Team index -> that team's real seed rows, chosen exactly as
    `seed_dev_league.seed_dev_league` chooses them: each franchise's own top
    `per_team` by minutes with `real_team_rosters`, else one league-wide sample
    without replacement keyed by the league seed."""
    pool = seed_dev_league._load_real_player_pool(season)
    if real_team_rosters:
        return seed_dev_league._real_players_on_their_real_teams(
            pool, team_codes=[t.code for t in teams], per_team=per_team
        )
    sampled = seed_dev_league._sample_real_players(
        pool, rng_seed=league_seed, count=per_team * len(teams)
    )
    return {i: sampled[i * per_team : (i + 1) * per_team] for i in range(len(teams))}


def _build_team_strategies(
    teams: Sequence[Team],
    players: Sequence[Player],
    ratings_by_player: Mapping[int, PlayerRatings],
    tendencies_by_player: Mapping[int, PlayerTendencies],
    *,
    league_id: int,
    season: int,
) -> dict[int, TeamStrategy]:
    """The in-memory equivalent of `strategy_service.
    ensure_defaults_for_season`'s body -- reuses that module's pure scoring
    functions (`_build_squads`/`_standardized_features`/`_scheme_scores`/
    `_argmax_scheme`/`_numeric_values`, none of which take an `Executor` or
    do I/O) rather than re-deriving §9's scheme-assignment math a second
    time in this tool, which would risk the two drifting apart."""
    team_ids = [t.id for t in teams]
    # Reaches into `strategy_service`'s private (but pure, I/O-free) scoring
    # helpers rather than re-deriving §9's scheme-assignment math a second
    # time in this tool -- see this function's own docstring.
    squads = strategy_service._build_squads(
        team_ids, players, ratings_by_player, tendencies_by_player
    )
    standardized = strategy_service._standardized_features(squads, team_ids)

    offense_scores = strategy_service._scheme_scores(
        strategy_service._OFFENSE_SCHEMES, standardized, team_ids
    )
    defense_scores = strategy_service._scheme_scores(
        strategy_service._DEFENSE_SCHEMES, standardized, team_ids
    )
    offense_choice = strategy_service._argmax_scheme(offense_scores, OffensiveScheme, team_ids)
    defense_choice = strategy_service._argmax_scheme(defense_scores, DefensiveScheme, team_ids)
    numeric = strategy_service._numeric_values(standardized, team_ids)

    return {
        tid: TeamStrategy(
            league_id=league_id,
            team_id=tid,
            season=season,
            pace_target=numeric["pace_target"][tid],
            offensive_scheme=offense_choice[tid],
            defensive_scheme=defense_choice[tid],
            help_aggression=numeric["help_aggression"][tid],
            three_point_bias=numeric["three_point_bias"][tid],
            crash_oreb_bias=numeric["crash_oreb_bias"][tid],
            set_by=None,
        )
        for tid in team_ids
    }


def generate_league(
    *,
    league_id: int,
    season: int,
    rng_seed: int,
    players_per_team: int,
    philosophy_spread: str = "none",
    real_rows_by_team: Mapping[int, Sequence[Mapping]] | None = None,
) -> LeagueData:
    teams_meta = _load_teams_meta()
    teams = _build_teams(teams_meta, league_id=league_id, philosophy_spread=philosophy_spread)

    players_by_team: dict[int, tuple[Player, ...]] = {}
    ratings_by_player: dict[int, PlayerRatings] = {}
    tendencies_by_player: dict[int, PlayerTendencies] = {}
    all_players: list[Player] = []

    for team_index, team in enumerate(teams):
        players, ratings, tendencies = _generate_team_roster(
            team,
            team_index=team_index,
            league_seed=rng_seed,
            players_per_team=players_per_team,
            current_season=season,
            real_rows=(real_rows_by_team or {}).get(team_index, ()),
        )
        players_by_team[team.id] = tuple(players)
        ratings_by_player.update(ratings)
        tendencies_by_player.update(tendencies)
        all_players.extend(players)

    strategy_by_team = _build_team_strategies(
        teams,
        all_players,
        ratings_by_player,
        tendencies_by_player,
        league_id=league_id,
        season=season,
    )

    return LeagueData(
        league_id=league_id,
        season=season,
        teams=teams,
        players_by_team=players_by_team,
        ratings_by_player=ratings_by_player,
        tendencies_by_player=tendencies_by_player,
        strategy_by_team=strategy_by_team,
    )


def _construct_roles_in_memory(league: LeagueData) -> dict[int, PlayerRole]:
    """RC-23.2: calls `role_service`'s **pure** `construct_team_roles` over
    each generated roster, in exactly the pure-function-reuse shape
    `_build_team_strategies` above already uses for `strategy_service` --
    this is why RC-22.3's pure/impure split in `role_service` is mandatory,
    not stylistic. No database: builds in-memory `PlayerRole` rows the
    same way this module already builds `Player`/`PlayerRatings` (`id=0`,
    a fixed `assigned_at`)."""
    roles_by_player: dict[int, PlayerRole] = {}
    for team in league.teams:
        players = league.players_by_team[team.id]
        assignments = role_service.construct_team_roles(
            players, league.ratings_by_player, philosophy=team.gm_philosophy, season=league.season
        )
        for a in assignments:
            roles_by_player[a.player_id] = PlayerRole(
                id=0,
                league_id=team.league_id,
                team_id=team.id,
                season=league.season,
                player_id=a.player_id,
                role=a.role_label,
                touch_share=a.minutes_share,
                rationale=a.rationale,
                assigned_by=None,
                locked=False,
                assigned_at=datetime(league.season, 1, 1, tzinfo=UTC),
            )
    return roles_by_player


# ---------------------------------------------------------------------------
# Schedule generation -- reuses `schedule_service`'s pure matchup/home-away
# helpers, skipping only the calendar date-slotting pass (irrelevant to a
# headless aggregate run; `game_index`, which is what Decision P1-21's RNG
# derivation actually needs, comes directly from the balanced round-robin
# order those helpers already produce).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameSpec:
    game_index: int
    home_team_id: int
    away_team_id: int
    rng_seed: int


def build_schedule(teams: Sequence[Team], *, league_seed: int, season: int) -> list[GameSpec]:
    matchups = schedule_service.build_matchups(teams)
    instances = schedule_service._flat_instances(matchups)
    homes_and_aways = schedule_service._assign_home_away(instances)
    return [
        GameSpec(
            game_index=i,
            home_team_id=home_id,
            away_team_id=away_id,
            rng_seed=rng_for(league_seed, season, i).getrandbits(63),
        )
        for i, (home_id, away_id) in enumerate(homes_and_aways)
    ]


# ---------------------------------------------------------------------------
# `GameContext` assembly -- the in-memory equivalent of `game_sim_service.
# build_context`, minus every repository read.
# ---------------------------------------------------------------------------


def _assemble_sim_players(
    players: Sequence[Player],
    ratings_by_player: Mapping[int, PlayerRatings],
    tendencies_by_player: Mapping[int, PlayerTendencies],
    tuning: TuningProfile,
    roles_by_player: Mapping[int, PlayerRole] | None = None,
) -> tuple[SimPlayer, ...]:
    """RC-23.1: `roles_by_player` defaults to `None` (every caller's
    behaviour before RC-23 -- `role=None` for every player, unconditionally)
    so `--construct-roles` is the only thing that can make this read a
    real row."""
    return tuple(
        build_sim_player(
            p,
            ratings_by_player[p.id],
            tendencies_by_player[p.id],
            tuning,
            role=roles_by_player.get(p.id) if roles_by_player else None,
            directive=None,
        )
        for p in players
    )


def build_context(
    league: LeagueData, game: GameSpec, tuning: TuningProfile
) -> GameContext:
    teams_by_id = {t.id: t for t in league.teams}
    home_team = teams_by_id[game.home_team_id]
    away_team = teams_by_id[game.away_team_id]
    home_players = _assemble_sim_players(
        league.players_by_team[home_team.id],
        league.ratings_by_player,
        league.tendencies_by_player,
        tuning,
        league.roles_by_player,
    )
    away_players = _assemble_sim_players(
        league.players_by_team[away_team.id],
        league.ratings_by_player,
        league.tendencies_by_player,
        tuning,
        league.roles_by_player,
    )
    return GameContext(
        game_id=game.game_index,
        season=league.season,
        home=build_sim_team(home_team, league.strategy_by_team[home_team.id], home_players),
        away=build_sim_team(away_team, league.strategy_by_team[away_team.id], away_players),
        tuning=tuning,
    )


# ---------------------------------------------------------------------------
# Aggregation -- one season's worth of `GameResult`s into the §10 metric
# table.
# ---------------------------------------------------------------------------


@dataclass
class SeasonAccumulator:
    games: int = 0
    total_seconds: float = 0.0
    team_games: int = 0
    team_possessions_raw: int = 0
    team_possessions_normalized_to_48: float = 0.0
    points: int = 0
    fga_rim: int = 0
    fgm_rim: int = 0
    fga_mid: int = 0
    fgm_mid: int = 0
    fga_three: int = 0
    fgm_three: int = 0
    fta: int = 0
    ftm: int = 0
    ast: int = 0
    tov: int = 0
    oreb: int = 0
    dreb: int = 0
    pf: int = 0
    # BDA-6.3 (docs/design/block-defender-attribution.md): `blk` already
    # exists on both box-line dataclasses (`events.py:243`/`:217`) but was
    # never read here -- `blk_per_game`/`top_shot_blocker_bpg` are this
    # doc's own named additions to the harness.
    blk: int = 0
    win_counts: dict[int, int] = field(default_factory=dict)
    games_played: dict[int, int] = field(default_factory=dict)
    player_points: dict[int, int] = field(default_factory=dict)
    player_minutes: dict[int, float] = field(default_factory=dict)
    player_blocks: dict[int, int] = field(default_factory=dict)
    # docs/design/per-36-spread-compression.md SC-4/SC-7 Commit 1: the
    # per-player assist/rebound accumulation `SeasonAccumulator` never had
    # -- same shape as `player_points`/`player_blocks` above, populated in
    # the same `add_game` loop below. `player_rebounds` is `oreb + dreb`
    # (the box line has no combined `reb` field); assists and blocks are
    # each a single field already on `PlayerBoxLine`.
    player_assists: dict[int, int] = field(default_factory=dict)
    player_rebounds: dict[int, int] = field(default_factory=dict)
    # RPC-13-2 (docs/design/real-player-rating-calibration.md): the two
    # remaining `PlayerBoxLine` counting stats, same shape, for the
    # real-player mode's fidelity rows.
    player_steals: dict[int, int] = field(default_factory=dict)
    player_turnovers: dict[int, int] = field(default_factory=dict)
    player_team: dict[int, int] = field(default_factory=dict)

    def add_game(self, game: GameSpec, result: GameResult) -> None:
        game_seconds = _GAME_SECONDS_REGULATION + result.overtime_periods * _OVERTIME_SECONDS
        self.games += 1
        self.total_seconds += game_seconds

        for team_line in result.team_box_lines:
            self.team_games += 1
            self.team_possessions_raw += team_line.possessions
            self.team_possessions_normalized_to_48 += (
                team_line.possessions * _GAME_SECONDS_REGULATION / game_seconds
            )
            self.points += team_line.points
            self.fga_rim += team_line.fga_rim
            self.fgm_rim += team_line.fgm_rim
            self.fga_mid += team_line.fga_mid
            self.fgm_mid += team_line.fgm_mid
            self.fga_three += team_line.fga_three
            self.fgm_three += team_line.fgm_three
            self.fta += team_line.fta
            self.ftm += team_line.ftm
            self.ast += team_line.ast
            self.tov += team_line.tov
            self.oreb += team_line.oreb
            self.dreb += team_line.dreb
            self.pf += team_line.pf
            self.blk += team_line.blk

        for player_line in result.player_box_lines:
            pid = player_line.player_id
            self.player_points[pid] = self.player_points.get(pid, 0) + player_line.points
            self.player_minutes[pid] = (
                self.player_minutes.get(pid, 0.0) + player_line.minutes_played
            )
            self.player_blocks[pid] = self.player_blocks.get(pid, 0) + player_line.blk
            self.player_assists[pid] = self.player_assists.get(pid, 0) + player_line.ast
            self.player_rebounds[pid] = (
                self.player_rebounds.get(pid, 0) + player_line.oreb + player_line.dreb
            )
            self.player_steals[pid] = self.player_steals.get(pid, 0) + player_line.stl
            self.player_turnovers[pid] = self.player_turnovers.get(pid, 0) + player_line.tov
            self.player_team[pid] = player_line.team_id

        self.games_played[game.home_team_id] = self.games_played.get(game.home_team_id, 0) + 1
        self.games_played[game.away_team_id] = self.games_played.get(game.away_team_id, 0) + 1
        winner = game.home_team_id if result.home_score > result.away_score else game.away_team_id
        self.win_counts[winner] = self.win_counts.get(winner, 0) + 1


def _percentile(sorted_values: Sequence[float], p: float) -> float:
    idx = round(p * (len(sorted_values) - 1))
    return sorted_values[idx]


def compute_metrics(
    acc: SeasonAccumulator,
    teams: Sequence[Team],
    *,
    positions: Mapping[int, str] | None = None,
) -> dict[str, float]:
    """`positions` (player id -> primary position value, e.g. `"PG"`) is only
    needed for `guard_blk_per36` (block-attribution-v2.md BA-4.1's guard
    control); without it that one metric is not computed and its band row
    reports SKIP rather than a made-up value."""
    total_fga = acc.fga_rim + acc.fga_mid + acc.fga_three
    total_fgm = acc.fgm_rim + acc.fgm_mid + acc.fgm_three
    total_reb = acc.oreb + acc.dreb

    metrics: dict[str, float] = {
        "possessions_per_48": acc.team_possessions_normalized_to_48 / acc.team_games,
        "seconds_per_possession": acc.total_seconds / acc.team_possessions_raw,
        "points_per_game": acc.points / acc.team_games,
        "efg_pct": (total_fgm + 0.5 * acc.fgm_three) / total_fga,
        "fg_pct_rim": acc.fgm_rim / acc.fga_rim,
        "fg_pct_mid": acc.fgm_mid / acc.fga_mid,
        "fg_pct_three": acc.fgm_three / acc.fga_three,
        "ft_pct": acc.ftm / acc.fta,
        "share_fga_rim": acc.fga_rim / total_fga,
        "share_fga_mid": acc.fga_mid / total_fga,
        "share_fga_three": acc.fga_three / total_fga,
        "fta_per_fga": acc.fta / total_fga,
        "ast_per_made_fg": acc.ast / total_fgm,
        "tov_per_100_poss": acc.tov / acc.team_possessions_raw * 100.0,
        "oreb_pct": acc.oreb / total_reb,
        "dreb_pct": acc.dreb / total_reb,
        "pf_per_game": acc.pf / acc.team_games,
        "fta_per_game": acc.fta / acc.team_games,
        "blk_per_game": acc.blk / acc.team_games,
    }

    # Shape metrics: per-player season PPG/MPG, using each player's own
    # team's season game count as the denominator (a player who DNP'd some
    # games still divides by the team's full schedule count, matching
    # standard basketball-reference-style season PPG/MPG).
    ppg: dict[int, float] = {}
    mpg: dict[int, float] = {}
    bpg: dict[int, float] = {}
    for pid, team_id in acc.player_team.items():
        games = acc.games_played.get(team_id, 0)
        if games == 0:
            continue  # pragma: no cover -- unreachable, every scheduled team plays >=1 game
        ppg[pid] = acc.player_points.get(pid, 0) / games
        mpg[pid] = acc.player_minutes.get(pid, 0.0) / games
        bpg[pid] = acc.player_blocks.get(pid, 0) / games

    rotation_ppg = sorted(
        v for pid, v in ppg.items() if mpg.get(pid, 0.0) >= _ROTATION_MPG_THRESHOLD
    )

    # docs/design/per-36-spread-compression.md SC-4/SC-7 Commit 1: per-36
    # rate + spread statistics over the rotation pool (the same >=
    # _ROTATION_MPG_THRESHOLD cutoff every other rotation-pool metric in
    # this module uses -- SC-8.5 step 1's "fix a pool definition once and
    # apply it identically" methodology). Per-36 is `stat * 36 /
    # total_minutes` over the full season (SC-7 step 2's own formula), a
    # ratio of two season totals -- not derived from the per-game ppg/mpg
    # averages above, which would not equal that ratio in general since
    # different players play different game counts.
    def _per36(totals: Mapping[int, int]) -> dict[int, float]:
        return {
            pid: totals.get(pid, 0) * 36.0 / minutes
            for pid, minutes in acc.player_minutes.items()
            if minutes > 0
        }

    rotation_pids = {pid for pid, v in mpg.items() if v >= _ROTATION_MPG_THRESHOLD}
    per36_by_stat = {
        "ast_per36": _per36(acc.player_assists),
        "reb_per36": _per36(acc.player_rebounds),
        "blk_per36": _per36(acc.player_blocks),
        "pts_per36": _per36(acc.player_points),
    }
    for name, per36 in per36_by_stat.items():
        rotation_values = [v for pid, v in per36.items() if pid in rotation_pids]
        if not rotation_values:
            continue  # pragma: no cover -- unreachable at N=14, every team has a rotation
        metrics[f"{name}_max"] = max(rotation_values)
        metrics[f"{name}_sd_rotation"] = (
            statistics.pstdev(rotation_values) if len(rotation_values) > 1 else 0.0
        )

    # docs/design/block-attribution-v2.md BA-4.1 / Commit 0: the block
    # distribution's *shape*, which `blk_per36_max` and `_sd_rotation` above
    # cannot see on their own -- a league of thirty equal "team-best" bigs
    # passes both while having no tail at all (BA-0.2: top-10 #1/#10 = 1.16-1.23
    # against a real 1.60). `_top10_ratio` is #1 over #10 in the rotation
    # pool; `_p95_rotation` is the body's upper edge (real 1.90 while the max
    # is 3.78 -- a thin body with a long tail). `players_over_2_bpg` is
    # reported, never gated: real 2025 had 1, a normal season 3-5, and one
    # outlier season is not a band. `guard_blk_per36` (PG + SG rotation mean)
    # is a floor-only alarm for "did the concentration zero out the guards",
    # not a target (real 0.44).
    rotation_blk36 = sorted(
        (v for pid, v in per36_by_stat["blk_per36"].items() if pid in rotation_pids),
        reverse=True,
    )
    if len(rotation_blk36) >= 10 and rotation_blk36[9] > 0:
        metrics["blk_per36_top10_ratio"] = rotation_blk36[0] / rotation_blk36[9]
    if rotation_blk36:
        metrics["blk_per36_p95_rotation"] = _percentile(sorted(rotation_blk36), 0.95)
    metrics["players_over_2_bpg"] = float(sum(1 for v in bpg.values() if v >= 2.0))
    if positions is not None:
        guard_blk36 = [
            v
            for pid, v in per36_by_stat["blk_per36"].items()
            if pid in rotation_pids and positions.get(pid) in _GUARD_POSITIONS
        ]
        if guard_blk36:
            metrics["guard_blk_per36"] = statistics.mean(guard_blk36)

    metrics["top_scorer_ppg"] = max(ppg.values()) if ppg else 0.0
    # BDA-6.3: same shape as `top_scorer_ppg` above -- the exposure defect
    # this doc exists to fix is a *distribution-shape* problem ("no
    # individual is an elite shot-blocker"), so the league's own best shot
    # blocker's season BPG is the shape metric that actually detects it;
    # `blk_per_game` alone (a league-wide mean) cannot, by construction.
    metrics["top_shot_blocker_bpg"] = max(bpg.values()) if bpg else 0.0
    if rotation_ppg:
        metrics["p90_ppg_rotation"] = _percentile(rotation_ppg, 0.90)
        metrics["p50_ppg_rotation"] = _percentile(rotation_ppg, 0.50)
        metrics["p10_ppg_rotation"] = _percentile(rotation_ppg, 0.10)
    metrics["share_players_over_30mpg"] = (
        sum(1 for v in mpg.values() if v >= 30.0) / len(mpg) if mpg else 0.0
    )

    starter_means: list[float] = []
    rotation_means: list[float] = []
    bench_means: list[float] = []
    # docs/design/per-36-spread-compression.md SC-2.4/SC-7 Commit 1:
    # `minutes_leader_mpg`(`_sd`) -- the mean and cross-team SD of each
    # team's own rank-1-by-mpg player, built from the same per-team
    # `team_mpg` sort as `starter_mpg` below (SC-2.3's cross-team-variance
    # metric; SC-2.4's anchor derivation is conditional on the mean).
    # `rank{N}_realized_mpg` (N=1-5) is SC-2.2/SC-8.1 measurement 3's own
    # per-rank realized value, reported (not gated) -- the starter-tier
    # mean the existing `starter_mpg` band uses cannot tell rank 1 apart
    # from rank 5, and Commit 4's anchor re-derivation needs rank 1 alone.
    leader_mpg: list[float] = []
    rank_mpg: dict[int, list[float]] = {rank: [] for rank in range(1, _STARTER_TIER_SIZE + 1)}
    for team in teams:
        team_mpg = sorted(
            (v for pid, v in mpg.items() if acc.player_team.get(pid) == team.id), reverse=True
        )
        if not team_mpg:
            continue  # pragma: no cover -- unreachable, every team has players
        leader_mpg.append(team_mpg[0])
        for rank in range(1, _STARTER_TIER_SIZE + 1):
            if len(team_mpg) >= rank:
                rank_mpg[rank].append(team_mpg[rank - 1])
        starters = team_mpg[:_STARTER_TIER_SIZE]
        rotation = team_mpg[_STARTER_TIER_SIZE : _STARTER_TIER_SIZE + _ROTATION_TIER_SIZE]
        bench = team_mpg[_STARTER_TIER_SIZE + _ROTATION_TIER_SIZE :]
        if starters:
            starter_means.append(statistics.mean(starters))
        if rotation:
            rotation_means.append(statistics.mean(rotation))
        if bench:
            bench_means.append(statistics.mean(bench))
    if leader_mpg:
        metrics["minutes_leader_mpg"] = statistics.mean(leader_mpg)
        metrics["minutes_leader_mpg_sd"] = (
            statistics.pstdev(leader_mpg) if len(leader_mpg) > 1 else 0.0
        )
    for rank, values in rank_mpg.items():
        if values:
            metrics[f"rank{rank}_realized_mpg"] = statistics.mean(values)
    if starter_means:
        metrics["starter_mpg"] = statistics.mean(starter_means)
    if rotation_means:
        metrics["rotation_bench_mpg"] = statistics.mean(rotation_means)
    if bench_means:
        metrics["deep_bench_mpg"] = statistics.mean(bench_means)

    wins = [acc.win_counts.get(t.id, 0) for t in teams]
    metrics["team_win_total_sd"] = statistics.pstdev(wins) if len(wins) > 1 else 0.0

    return metrics


def simulate_season(
    *,
    league_id: int,
    season: int,
    rng_seed: int,
    players_per_team: int,
    tuning: TuningProfile,
    construct_roles: bool = False,
    philosophy_spread: str = "none",
    real_players_season: int | None = None,
    real_team_rosters: bool = False,
    real_players_per_team: int | None = None,
) -> tuple[dict[str, float], LeagueData, int]:
    """Generates one full league + schedule and sims every game, entirely
    in memory. Returns (metrics, league, games_simmed); see
    `simulate_season_detailed` for the per-player accumulator as well.

    `construct_roles` (RC-23.2, `docs/design/auto-gm-role-construction.md`):
    when set, calls `role_service.construct_team_roles` over every
    generated roster right after generation and before the game loop, so
    every game in this season reads real `player_roles` rows instead of
    `role=None`. `philosophy_spread` (RC-23.3) is threaded to `generate_
    league` regardless of `construct_roles` -- the "roles off" row of
    RC-23.4's 2x4 grid needs the flag wired to *nothing else* to be a
    meaningful control.

    `real_players_season` (RPC-13) hydrates the first `real_players_per_team`
    slots of every roster (default: all `players_per_team`) from that season's
    real-player seed file, on each franchise's real roster with
    `real_team_rosters`; `None` (the default) is the synthetic league."""
    metrics, league, games, _acc = simulate_season_detailed(
        league_id=league_id,
        season=season,
        rng_seed=rng_seed,
        players_per_team=players_per_team,
        tuning=tuning,
        construct_roles=construct_roles,
        philosophy_spread=philosophy_spread,
        real_players_season=real_players_season,
        real_team_rosters=real_team_rosters,
        real_players_per_team=real_players_per_team,
    )
    return metrics, league, games


def simulate_season_detailed(
    *,
    league_id: int,
    season: int,
    rng_seed: int,
    players_per_team: int,
    tuning: TuningProfile,
    construct_roles: bool = False,
    philosophy_spread: str = "none",
    real_players_season: int | None = None,
    real_team_rosters: bool = False,
    real_players_per_team: int | None = None,
) -> tuple[dict[str, float], LeagueData, int, SeasonAccumulator]:
    """`simulate_season`, also returning the season's `SeasonAccumulator` (the
    per-player totals the real-player fidelity rows are computed from)."""
    real_rows_by_team = None
    if real_players_season is not None:
        per_team = players_per_team if real_players_per_team is None else real_players_per_team
        real_rows_by_team = select_real_rosters(
            real_players_season,
            _build_teams(_load_teams_meta(), league_id=league_id),
            league_seed=rng_seed,
            per_team=per_team,
            real_team_rosters=real_team_rosters,
        )
    league = generate_league(
        league_id=league_id,
        season=season,
        rng_seed=rng_seed,
        players_per_team=players_per_team,
        philosophy_spread=philosophy_spread,
        real_rows_by_team=real_rows_by_team,
    )
    if construct_roles:
        roles_by_player = _construct_roles_in_memory(league)
        league = dataclasses.replace(league, roles_by_player=roles_by_player)
    schedule = build_schedule(league.teams, league_seed=rng_seed, season=season)

    acc = SeasonAccumulator()
    for game in schedule:
        context = build_context(league, game, tuning)
        result = simulate_game(context, rng_seed=game.rng_seed)
        acc.add_game(game, result)

    positions = {
        player.id: player.primary_position.value
        for players in league.players_by_team.values()
        for player in players
    }
    metrics = compute_metrics(acc, league.teams, positions=positions)
    return metrics, league, len(schedule), acc


# ---------------------------------------------------------------------------
# RPC-13 (docs/design/real-player-rating-calibration.md): real-player
# fidelity. Floors are this tool's own constants, never entries in
# `seeds/nba_reference_distributions.json` (RPC-13-4): those bands calibrate
# the synthetic generator's league aggregates.
# ---------------------------------------------------------------------------

# name -> (sim per-36 counter, real per-36 truth key, floor): RPC-12 S1/S2.
_FIDELITY_RATES: tuple[tuple[str, str, str, float], ...] = (
    ("blk", "player_blocks", "blk36", 0.70),
    ("reb", "player_rebounds", "reb36", 0.75),
    ("ast", "player_assists", "ast36", 0.80),
    ("stl", "player_steals", "stl36", 0.0),
    ("tov", "player_turnovers", "tov36", 0.0),
    ("pts", "player_points", "pts36", 0.0),
)
# S3: the engine must still render the rating it is given.
_FIDELITY_RENDER_FLOOR = 0.70
# S4: (label, counter, name, inside?, limit)
_FIDELITY_ANCHORS: tuple[tuple[str, str, str, bool, int], ...] = (
    ("bpg", "player_blocks", "Victor Wembanyama", True, 5),
    ("bpg", "player_blocks", "Nikola Jokic", False, 40),
    ("apg", "player_assists", "Nikola Jokic", True, 5),
)
_LEGIBILITY_TOP = 10


@dataclass
class RealPlayerTotals:
    """One real player's simulated season totals, pooled across replicates."""

    name: str
    position: str
    rim_protection: int
    minutes: float = 0.0
    games: int = 0  # his team's scheduled games, the ppg/mpg denominator used above
    counts: dict[str, int] = field(default_factory=dict)


def pool_real_totals(
    pooled: dict[str, RealPlayerTotals], league: LeagueData, acc: SeasonAccumulator
) -> None:
    """Adds one replicate's real players to `pooled`, keyed by `external_ref`."""
    for team in league.teams:
        for player in league.players_by_team[team.id]:
            if player.external_ref is None:
                continue
            entry = pooled.setdefault(
                player.external_ref,
                RealPlayerTotals(
                    name=f"{player.first_name} {player.last_name}",
                    position=player.primary_position.value,
                    rim_protection=league.ratings_by_player[player.id].rim_protection,
                ),
            )
            entry.minutes += acc.player_minutes.get(player.id, 0.0)
            entry.games += acc.games_played.get(team.id, 0)
            for _label, counter, _key, _floor in _FIDELITY_RATES:
                value = getattr(acc, counter).get(player.id, 0)
                entry.counts[counter] = entry.counts.get(counter, 0) + value


def _fold_name(text: str) -> str:
    import unicodedata

    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c)).lower().strip()


def compute_real_fidelity(
    pooled: Mapping[str, RealPlayerTotals], truth: Mapping[str, dict]
) -> dict:
    """RPC-12 S1-S4 plus the legibility table, over the real players the sim
    played at least `_ROTATION_MPG_THRESHOLD` mpg (this module's one rotation
    pool definition) who have a raw row. S4's ranks are over every real player
    by per-game rate, ties broken by `external_ref`."""
    rotation = {
        ref: t
        for ref, t in pooled.items()
        if ref in truth and t.games and t.minutes / t.games >= _ROTATION_MPG_THRESHOLD
    }

    def sim36(t: RealPlayerTotals, counter: str) -> float:
        return t.counts.get(counter, 0) * 36.0 / t.minutes if t.minutes > 0 else 0.0

    rows: list[dict] = []
    legibility: dict[str, list[str]] = {}
    for label, counter, key, floor in _FIDELITY_RATES:
        refs = sorted(rotation)
        rho = spearman([sim36(rotation[r], counter) for r in refs], [truth[r][key] for r in refs])
        rows.append(
            {
                "name": f"{label}_per36_spearman_vs_real",
                "value": rho,
                "floor": floor or None,
                "passed": (rho >= floor) if floor else None,
            }
        )
        sim_order = sorted(refs, key=lambda r: (-sim36(rotation[r], counter), r))
        sim_rank = {r: i for i, r in enumerate(sim_order, start=1)}
        leaders = sorted(refs, key=lambda r: (-truth[r][key], r))[:_LEGIBILITY_TOP]
        legibility[label] = [
            f"{rotation[r].name} ({rotation[r].position}) real {truth[r][key]:.2f} -> sim "
            f"{sim36(rotation[r], counter):.2f}, #{sim_rank[r]} of {len(refs)}"
            for r in leaders
        ]
    refs = sorted(rotation)
    render = spearman(
        [sim36(rotation[r], "player_blocks") for r in refs],
        [rotation[r].rim_protection for r in refs],
    )
    rows.append(
        {
            "name": "blk_per36_spearman_vs_rim_protection",
            "value": render,
            "floor": _FIDELITY_RENDER_FLOOR,
            "passed": render >= _FIDELITY_RENDER_FLOOR,
        }
    )
    for label, counter, name, inside, limit in _FIDELITY_ANCHORS:
        per_game = {
            ref: t.counts.get(counter, 0) / t.games for ref, t in pooled.items() if t.games
        }
        order = sorted(per_game, key=lambda r: (-per_game[r], r))
        hits = [r for r in order if _fold_name(pooled[r].name) == _fold_name(name)]
        title = f"{name} {'inside' if inside else 'outside'} sim top {limit} by {label}"
        if not hits:
            rows.append({"name": title, "value": None, "floor": None, "passed": None})
            continue
        rank = order.index(hits[0]) + 1
        rows.append(
            {
                "name": title,
                "value": rank,
                "detail": f"{per_game[hits[0]]:.2f} {label}",
                "floor": None,
                "passed": rank <= limit if inside else rank > limit,
            }
        )
    top_blocker = max(
        rotation, key=lambda r: (sim36(rotation[r], "player_blocks"), r), default=None
    )
    return {
        "rotation_players": len(rotation),
        "real_players": len(pooled),
        "rows": rows,
        "legibility": legibility,
        "rotation_blk_per36_max_holder": (
            f"{rotation[top_blocker].name} {sim36(rotation[top_blocker], 'player_blocks'):.3f}"
            if top_blocker
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Reference comparison + report writing.
# ---------------------------------------------------------------------------


# Computed by `compute_metrics` and printed in the report, deliberately with no
# band in `nba_reference_distributions.json` (block-attribution-v2.md BA-4.1).
_REPORTED_ONLY_METRICS: tuple[str, ...] = ("players_over_2_bpg",)


def load_reference(path: Path = _REFERENCE_JSON) -> dict[str, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in payload.items() if isinstance(v, dict) and "band_low" in v}


@dataclass(frozen=True)
class MetricResult:
    name: str
    group: str
    value: float | None
    band_low: float
    band_high: float
    reference_value: float
    passed: bool | None  # None if the metric wasn't computed (e.g. no rotation players)


def evaluate(
    metrics: Mapping[str, float], reference: Mapping[str, dict]
) -> list[MetricResult]:
    results = []
    for name, ref in reference.items():
        value = metrics.get(name)
        passed = None if value is None else ref["band_low"] <= value <= ref["band_high"]
        results.append(
            MetricResult(
                name=name,
                group=ref.get("group", "unknown"),
                value=value,
                band_low=ref["band_low"],
                band_high=ref["band_high"],
                reference_value=ref["reference_value"],
                passed=passed,
            )
        )
    return results


def _average_metrics(per_season: Sequence[dict[str, float]]) -> dict[str, float]:
    keys = {k for m in per_season for k in m}
    return {
        k: statistics.mean(m[k] for m in per_season if k in m)
        for k in keys
        if any(k in m for m in per_season)
    }


def write_report(
    out_dir: Path,
    *,
    per_season_metrics: Sequence[dict[str, float]],
    averaged_results: Sequence[MetricResult],
    seasons: int,
    players_per_team: int,
    rng_seed: int,
    elapsed_seconds: float,
    real_fidelity: Mapping | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    passed = sum(1 for r in averaged_results if r.passed)
    failed = sum(1 for r in averaged_results if r.passed is False)
    skipped = sum(1 for r in averaged_results if r.passed is None)

    report_json = {
        "generated_at": datetime.now(UTC).isoformat(),
        "seasons": seasons,
        "players_per_team": players_per_team,
        "rng_seed": rng_seed,
        "elapsed_seconds": elapsed_seconds,
        "summary": {
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "total": len(averaged_results),
        },
        "per_season_metrics": per_season_metrics,
        "metrics": [
            {
                "name": r.name,
                "group": r.group,
                "value": r.value,
                "band_low": r.band_low,
                "band_high": r.band_high,
                "reference_value": r.reference_value,
                "passed": r.passed,
            }
            for r in averaged_results
        ],
    }
    if real_fidelity is not None:
        report_json["real_fidelity"] = real_fidelity
    (out_dir / "report.json").write_text(json.dumps(report_json, indent=2) + "\n", encoding="utf-8")

    lines = [
        f"# Sim validation report -- {seasons} season(s), {players_per_team} players/team, "
        f"rng_seed={rng_seed}",
        "",
        f"Generated: {report_json['generated_at']}  |  Elapsed: {elapsed_seconds:.1f}s",
        "",
        f"**{passed}/{len(averaged_results)} metrics in band** "
        f"({failed} failed, {skipped} skipped -- no data, e.g. no rotation players)",
        "",
        "| Group | Metric | Value | Band | Reference | Result |",
        "|---|---|---|---|---|---|",
    ]
    for r in sorted(averaged_results, key=lambda x: (x.group, x.name)):
        value_str = f"{r.value:.4f}" if r.value is not None else "n/a"
        status = "PASS" if r.passed else ("SKIP" if r.passed is None else "FAIL")
        lines.append(
            f"| {r.group} | {r.name} | {value_str} | [{r.band_low:.4f}, {r.band_high:.4f}] | "
            f"{r.reference_value:.4f} | {status} |"
        )
    averaged_reported = _average_metrics(per_season_metrics)
    lines += ["", "Reported, not gated:"]
    for name in _REPORTED_ONLY_METRICS:
        value = averaged_reported.get(name)
        lines.append(f"- {name}: {value:.4f}" if value is not None else f"- {name}: n/a")
    if real_fidelity is not None:
        lines += _real_fidelity_lines(real_fidelity)
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _real_fidelity_lines(fidelity: Mapping) -> list[str]:
    lines = [
        "",
        f"## Real-player fidelity -- season {fidelity['season']}, "
        f"{fidelity['rotation_players']} of {fidelity['real_players']} real players at "
        f">= {_ROTATION_MPG_THRESHOLD:g} sim mpg (pooled over replicates)",
        "",
        "| Row | Value | Floor | Result |",
        "|---|---|---|---|",
    ]
    for row in fidelity["rows"]:
        value = row["value"]
        shown = "n/a" if value is None else (f"{value:.4f}" if isinstance(value, float) else value)
        if row.get("detail"):
            shown = f"{shown} ({row['detail']})"
        status = "INFO" if row["passed"] is None else ("PASS" if row["passed"] else "FAIL")
        floor = "" if row["floor"] is None else f"{row['floor']:.2f}"
        lines.append(f"| {row['name']} | {shown} | {floor} | {status} |")
    lines.append("")
    lines.append(f"Rotation blk/36 max holder: {fidelity['rotation_blk_per36_max_holder']}")
    for label, leaders in fidelity["legibility"].items():
        lines += ["", f"Top {len(leaders)} real {label}/36 -> simulated rank:", ""]
        lines += [f"- {entry}" for entry in leaders]
    return lines


def run_validation(
    *,
    seasons: int,
    players_per_team: int,
    rng_seed: int,
    season_year: int,
    out_dir: Path,
    construct_roles: bool = False,
    philosophy_spread: str = "none",
    real_players_season: int | None = None,
    real_team_rosters: bool = False,
    real_players_per_team: int | None = None,
) -> list[MetricResult]:
    tuning = TuningProfile()
    reference = load_reference()

    t0 = time.perf_counter()
    per_season_metrics: list[dict[str, float]] = []
    pooled: dict[str, RealPlayerTotals] = {}
    for i in range(seasons):
        season_seed = rng_for(rng_seed, "validate_sim", "season", i).getrandbits(63)
        metrics, league, games_simmed, acc = simulate_season_detailed(
            league_id=i + 1,
            season=season_year,
            rng_seed=season_seed,
            players_per_team=players_per_team,
            tuning=tuning,
            construct_roles=construct_roles,
            philosophy_spread=philosophy_spread,
            real_players_season=real_players_season,
            real_team_rosters=real_team_rosters,
            real_players_per_team=real_players_per_team,
        )
        if real_players_season is not None:
            pool_real_totals(pooled, league, acc)
        per_season_metrics.append(metrics)
        print(f"season {i + 1}/{seasons}: {games_simmed} games simmed")
    elapsed = time.perf_counter() - t0

    averaged = _average_metrics(per_season_metrics)
    results = evaluate(averaged, reference)

    fidelity = None
    if real_players_season is not None:
        truth = load_raw_truth(_REAL_RAW_DIR, [real_players_season]).get(real_players_season, {})
        fidelity = {"season": real_players_season, **compute_real_fidelity(pooled, truth)}
        for line in _real_fidelity_lines(fidelity):
            print(line)
    write_report(
        out_dir,
        per_season_metrics=per_season_metrics,
        averaged_results=results,
        seasons=seasons,
        players_per_team=players_per_team,
        rng_seed=rng_seed,
        elapsed_seconds=elapsed,
        real_fidelity=fidelity,
    )
    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sim N full headless NBA seasons (in-memory, no Postgres) and check the "
            "results against seeds/nba_reference_distributions.json (§10's validation "
            "harness). Default 1 season keeps a manual run to roughly 1-2 minutes -- "
            "see this module's own docstring for the runtime tradeoff."
        )
    )
    parser.add_argument("--seasons", type=int, default=1)
    parser.add_argument("--players-per-team", type=int, default=_DEFAULT_PLAYERS_PER_TEAM)
    parser.add_argument("--rng-seed", type=int, default=1)
    parser.add_argument("--season-year", type=int, default=2026)
    parser.add_argument(
        "--construct-roles",
        action="store_true",
        help=(
            "RC-23.2 (docs/design/auto-gm-role-construction.md): run the auto-GM "
            "role-construction pass (arch_z) over every generated roster before "
            "simming, instead of every player reading role=None."
        ),
    )
    parser.add_argument(
        "--philosophy-spread",
        choices=_PHILOSOPHY_SPREADS,
        default="none",
        help=(
            "RC-23.3: override seeds/teams.json's uniform tendency_respecter "
            "gm_philosophy for this run only. 'none' (default) is the control."
        ),
    )
    parser.add_argument(
        "--real-players",
        type=int,
        default=None,
        metavar="SEASON",
        help=(
            "RPC-13 (docs/design/real-player-rating-calibration.md): hydrate rosters "
            "from seeds/real_players/SEASON.json and add the real_fidelity rows "
            "(simulated vs real per-36, named anchors) to the report."
        ),
    )
    parser.add_argument(
        "--real-team-rosters",
        action="store_true",
        help="With --real-players: each franchise's own real players, as the live league.",
    )
    parser.add_argument(
        "--real-players-per-team",
        type=int,
        default=None,
        help="With --real-players: real slots per roster (default: --players-per-team).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory (default: artifacts/validation/<UTC timestamp>).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    out_dir = (
        Path(args.out)
        if args.out is not None
        else _REPO_ROOT / "artifacts" / "validation" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    results = run_validation(
        seasons=args.seasons,
        players_per_team=args.players_per_team,
        rng_seed=args.rng_seed,
        season_year=args.season_year,
        out_dir=out_dir,
        construct_roles=args.construct_roles,
        philosophy_spread=args.philosophy_spread,
        real_players_season=args.real_players,
        real_team_rosters=args.real_team_rosters,
        real_players_per_team=args.real_players_per_team,
    )
    passed = sum(1 for r in results if r.passed)
    failed = [r for r in results if r.passed is False]
    print(f"\n{passed}/{len(results)} metrics in band. Report: {out_dir / 'report.md'}")
    if failed:
        print("FAILED metrics:")
        for r in failed:
            print(f"  {r.name}: {r.value:.4f} not in [{r.band_low:.4f}, {r.band_high:.4f}]")


if __name__ == "__main__":
    main()
