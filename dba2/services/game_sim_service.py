"""`build_context` / `sim_game` / `sim_scheduled_games` -- the phase's
public API (phase-1-simulation-engine.md §8.3).

**`build_context(ex, game, *, tuning=None) -> GameContext`** loads one
game's home/away rosters + ratings + tendencies + roles + strategy via the
bulk loaders and assembles a `GameContext`. Separated from `sim_game`
deliberately so a future tuning harness can build once and run the pure
engine thousands of times with no DB.

**Interpretive call, flagged here rather than silently done:** §8.3's own
text lists this function's signature as `build_context(ex, game)`, with no
`tuning` parameter -- `sim_game`'s `tuning` argument would then have to be
applied to the context *after* `build_context` returns. That doesn't work:
`sim.context.build_sim_player` bakes `tuning`-dependent values
(`wingspan_normalized`, `height_normalized`, `spacing_contribution`,
`scheme_contribution`) into each `SimPlayer` at construction time (Decision
P1-4/P1-6), so swapping `context.tuning` after the fact via
`dataclasses.replace` would leave every player's cached fields computed
under the *wrong* profile while `context.tuning` claimed a different one --
a silent mismatch a tuning sweep would never detect from the outside. This
module instead threads `tuning` into `build_context` itself (defaulting to
`TuningProfile()` when omitted, matching `TuningProfile`'s own "a complete,
usable profile with no arguments" contract) so construction-time caches and
`context.tuning` are always built from the same profile.

**`sim_game(ex, game_id, *, tuning=None, resim=False, write_beat_log=False)
-> GameResult`** builds the context, runs `sim.game.simulate_game`,
persists box stats + period scores + finalization, all inside one
`transaction(ex)` (Decision P1-25/P1-26). `resim=True` is required to
re-sim an already-`final` game (deletes and re-inserts its stat rows inside
the same transaction, per Decision P1-25) and is rejected on anything else.

**`sim_scheduled_games(ex, league_id, season, *, through_game_index=None,
game_type=GameType.REGULAR, tuning=None, write_beat_log=False) ->
BatchResult`** loads ONE league snapshot up front via the bulk loaders and
reuses it across every game in the batch -- the N+1 risk §11 names -- with
one `transaction(ex)` PER GAME (never per batch, so one bad row can't roll
back the whole season and a season-long transaction never holds a
connection open for minutes).

Zero I/O lives in `sim/` (Decision P1-1); every database touch in this
module goes through a repository function, never raw SQL.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from dba2.core.errors import NotFoundError, ValidationError
from dba2.core.logging import get_logger
from dba2.data.db import Executor, transaction
from dba2.data.repositories import (
    game_repo,
    league_repo,
    player_repo,
    ratings_repo,
    role_repo,
    stats_repo,
    team_repo,
)
from dba2.domain.game import Game, GameStatus, GameType
from dba2.domain.player import Player, RosterStatus
from dba2.domain.ratings import PlayerRatings, PlayerTendencies
from dba2.domain.role import PlayerDirective, PlayerRole
from dba2.domain.team import Team, TeamStrategy
from dba2.phase.entity_states import GAME_TRANSITIONS, is_legal_transition
from dba2.sim.context import GameContext, SimPlayer, build_sim_player, build_sim_team
from dba2.sim.derivation import RATING_FIELD_ORDER
from dba2.sim.events import GameResult, PlayerBoxLine, TeamBoxLine
from dba2.sim.game import simulate_game
from dba2.sim.tuning import TUNING_VERSION, TuningProfile

log = get_logger(__name__)

# `PlayerBoxLine`/`TeamBoxLine`'s non-box-score identity fields -- everything
# else on each dataclass is exactly the 16-column box-score block
# `stats_repo._validate_box_score_dict` checks against. Deriving the box
# dict from `dataclasses.fields` (minus this set) rather than hardlisting
# the 16 names a second time means a drift between `sim/events.py` and
# `stats_repo`'s column set fails loudly (via that validation) instead of
# silently dropping a column.
_PLAYER_LINE_IDENTITY_FIELDS = frozenset({"player_id", "team_id", "minutes_played", "plus_minus"})
_TEAM_LINE_IDENTITY_FIELDS = frozenset({"team_id", "possessions"})


@dataclasses.dataclass(frozen=True)
class SimmedGame:
    """One committed game of a batch, carrying its own identity.

    `GameResult` has no game id (it is the engine's output, and the engine
    knows a seed, not a row), so a caller that wants to say WHICH game produced
    which score cannot recover that from the result. It also cannot recover it
    by position: a failed game is skipped, so the k-th result is not the k-th
    scheduled game once anything before it has failed (P5-99.i, F6). The ids
    are therefore copied from the `games` row at the moment the game commits."""

    game_id: int
    game_date: date
    home_team_id: int
    away_team_id: int
    result: GameResult


@dataclasses.dataclass(frozen=True)
class GameFailure:
    """Why one game of a batch did not sim: the discriminating cause
    `sim_scheduled_games` logs, carried in memory beside the id so the Stage E
    press can name it in the reply of the press that just tried (P5-99.e.1,
    P5-99.u item 3). Not persisted -- a data failure is deterministic and a
    transient one succeeds, so pressing again re-derives it."""

    game_id: int
    cause: str


@dataclasses.dataclass(frozen=True)
class BatchResult:
    """`sim_scheduled_games`'s return shape -- not a domain/table type (no
    `§8.3` table backs a batch run), so it lives here rather than in
    `domain/`, matching `sim/events.py`'s own precedent of service/engine
    return shapes living beside the code that produces them.

    `results` is empty unless the caller asked for it (`return_results=True`):
    each `GameResult` carries ~969 `BeatRecord`s, so keeping every one costs
    ~360 MB for a 1,230-game season-long call against ~5 MB for a game-day
    block. (That figure predates watch-v2 E-1, whose stamp also retains the
    `TerminalOutcome` payload on each possession's last record: about 228
    more small frozen objects per game, ~193 terminals + ~35 credits, as
    measured -- not re-priced into the ~360 MB.) When asked, it holds one
    `SimmedGame` per game in `games_simmed`, in the order they were simmed,
    and none for `failed_game_ids`.

    `failures` is always filled, one `GameFailure` per id in `failed_game_ids`
    (same order): the id says WHICH game failed, the cause says why -- a team with
    no strategy row, an incomplete roster snapshot, or a per-game transaction that
    raised."""

    games_simmed: int
    failed_game_ids: tuple[int, ...]
    results: tuple[SimmedGame, ...] = ()
    failures: tuple[GameFailure, ...] = ()


def _box_dict(line: PlayerBoxLine | TeamBoxLine, identity_fields: frozenset[str]) -> dict[str, int]:
    return {
        f.name: getattr(line, f.name)
        for f in dataclasses.fields(line)
        if f.name not in identity_fields
    }


def _quantize_minutes(value: float) -> Decimal:
    """Decision P1-5: the engine produces minutes as `float`; this module
    (never `sim/`) quantizes to the column scale
    (`NUMERIC(4,1)`/`NUMERIC(6,1)`) and constructs the `Decimal` on write.
    `Decimal(str(value))` avoids seeding the quantize step with a binary
    float's imprecise repr (e.g. `Decimal(28.1)` vs `Decimal("28.1")`)."""
    return Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _assemble_sim_players(
    players: Sequence[Player],
    ratings_by_player: Mapping[int, PlayerRatings],
    tendencies_by_player: Mapping[int, PlayerTendencies],
    roles_by_player: Mapping[int, PlayerRole],
    directives_by_player: Mapping[int, PlayerDirective],
    tuning: TuningProfile,
) -> tuple[SimPlayer, ...]:
    sim_players = []
    for p in players:
        ratings = ratings_by_player.get(p.id)
        tendencies = tendencies_by_player.get(p.id)
        if ratings is None or tendencies is None:
            raise NotFoundError(
                f"player {p.id} (team {p.team_id}) is missing a player_ratings or "
                "player_tendencies row -- ratings_service.derive_tendencies_for_league "
                "(and a ratings source) must run before this roster can be simmed"
            )
        sim_players.append(
            build_sim_player(
                p,
                ratings,
                tendencies,
                tuning,
                role=roles_by_player.get(p.id),
                directive=directives_by_player.get(p.id),
            )
        )
    return tuple(sim_players)


def _roster_digest(context: GameContext) -> str:
    """Decision P1-22: a hash over each team's *available* roster (every
    player `build_context`/`sim_scheduled_games` put on the `SimTeam`, not
    just whoever the rotation model ends up playing) and their rating rows.
    Computed from the already-built `GameContext` -- both teams' `SimPlayer`
    objects already hold the `PlayerRatings` this needs, so no second query
    is issued. Player order is sorted by id so the digest doesn't depend on
    the order a bulk-loader query happened to return rows in."""
    parts: list[str] = []
    for label, team in (("home", context.home), ("away", context.away)):
        for p in sorted(team.players, key=lambda sp: sp.id):
            values = ",".join(str(getattr(p.ratings, f)) for f in RATING_FIELD_ORDER)
            parts.append(f"{label}:{p.id}:{values}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


async def build_context(
    ex: Executor, game: Game, *, tuning: TuningProfile | None = None
) -> GameContext:
    """Loads `game`'s home/away rosters + ratings + tendencies + roles +
    directives + strategy and assembles a `GameContext`. Zero I/O happens
    inside `sim/` (Decision P1-1) -- every read here goes through a
    repository bulk loader (§8.2): `ratings_repo.get_for_team` (team-scoped,
    already exists), `ratings_repo.get_tendencies_for_league` /
    `role_repo.list_player_directives_for_league` (league-scoped -- no
    team-scoped tendencies loader was added by §8.2, so a single-game
    context build reads the league's tendencies/directives rows once and
    filters locally rather than adding a new repo function beyond §8.2's
    stated list), and `role_repo.list_player_roles_for_team` (team-scoped,
    already exists).

    Raises `NotFoundError` if either team, either team's `team_strategy`
    row for `game.season`, or any active player's ratings/tendencies row is
    missing -- `strategy_service.ensure_defaults_for_season` and a ratings
    source (synthetic generation or a real derivation) must both have run
    for this league/season first.

    "Available roster" (Decision P1-22's `roster_digest` scope) is read as
    every player currently on the team with `roster_status == ACTIVE` --
    an interpretive call, flagged here: the design doc names "every player
    eligible to enter the rotation" without enumerating which
    `RosterStatus` values qualify. `TWO_WAY`/`INJURED`/`FREE_AGENT`/
    `RETIRED`/`DRAFT_ELIGIBLE` are all excluded on the reasoning that none
    of them are "on this team's playable roster right now" in Phase 1's
    scope (no in-game injury model per this doc's non-goals, and two-way/
    G-League eligibility rules are unmodeled).
    """
    tuning = tuning if tuning is not None else TuningProfile()

    home_team = await team_repo.get_team(ex, game.home_team_id)
    away_team = await team_repo.get_team(ex, game.away_team_id)
    if home_team is None or away_team is None:
        raise NotFoundError(f"game {game.id}: home or away team not found")

    home_strategy = await team_repo.get_team_strategy(ex, game.home_team_id, game.season)
    away_strategy = await team_repo.get_team_strategy(ex, game.away_team_id, game.season)
    if home_strategy is None or away_strategy is None:
        raise NotFoundError(
            f"game {game.id}: team_strategy missing for season {game.season} -- run "
            "strategy_service.ensure_defaults_for_season first"
        )

    home_players = [
        p
        for p in await player_repo.list_players_for_team(ex, game.home_team_id)
        if p.roster_status == RosterStatus.ACTIVE
    ]
    away_players = [
        p
        for p in await player_repo.list_players_for_team(ex, game.away_team_id)
        if p.roster_status == RosterStatus.ACTIVE
    ]

    home_ratings = {r.player_id: r for r in await ratings_repo.get_for_team(ex, game.home_team_id)}
    away_ratings = {r.player_id: r for r in await ratings_repo.get_for_team(ex, game.away_team_id)}

    tendencies_by_player = {
        t.player_id: t for t in await ratings_repo.get_tendencies_for_league(ex, game.league_id)
    }
    directives_by_player = {
        d.player_id: d
        for d in await role_repo.list_player_directives_for_league(ex, game.league_id)
    }
    home_roles = {
        r.player_id: r
        for r in await role_repo.list_player_roles_for_team(ex, game.home_team_id, game.season)
    }
    away_roles = {
        r.player_id: r
        for r in await role_repo.list_player_roles_for_team(ex, game.away_team_id, game.season)
    }

    home_sim_players = _assemble_sim_players(
        home_players, home_ratings, tendencies_by_player, home_roles, directives_by_player, tuning
    )
    away_sim_players = _assemble_sim_players(
        away_players, away_ratings, tendencies_by_player, away_roles, directives_by_player, tuning
    )

    return GameContext(
        game_id=game.id,
        season=game.season,
        home=build_sim_team(home_team, home_strategy, home_sim_players),
        away=build_sim_team(away_team, away_strategy, away_sim_players),
        tuning=tuning,
    )


def _write_beat_log(game: Game, result: GameResult) -> None:
    """Decision P1-23/§7.1: structured play-by-play, gitignored, written
    only when the caller asks. One JSON object per line, from
    `dataclasses.asdict(record)`. A `BeatRecord` is NOT flat: on the last
    record of each possession (watch-v2 E-1) `terminal` and each entry of
    `possession_credits` are nested `TerminalOutcome` dataclasses, which
    `asdict` turns into JSON objects, and their `kind`/`zone` fields are
    `StrEnum`s, which `json.dumps` writes as their plain string values -- so
    no custom encoder is needed. Every other record has `terminal: null` and
    `possession_credits: []`."""
    path = (
        Path("artifacts")
        / "beatlogs"
        / str(game.league_id)
        / str(game.season)
        / f"{game.id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in result.beat_log:
            f.write(json.dumps(dataclasses.asdict(record)) + "\n")


async def _persist_game_result(
    conn, *, game: Game, result: GameResult, roster_digest: str, engine_version: int
) -> None:
    player_rows = [
        {
            "game_id": game.id,
            "league_id": game.league_id,
            "player_id": line.player_id,
            "team_id": line.team_id,
            "minutes_played": _quantize_minutes(line.minutes_played),
            "plus_minus": line.plus_minus,
            "box": _box_dict(line, _PLAYER_LINE_IDENTITY_FIELDS),
        }
        for line in result.player_box_lines
    ]
    await stats_repo.create_player_game_stats_many(conn, player_rows)

    team_rows = [
        {
            "game_id": game.id,
            "league_id": game.league_id,
            "team_id": line.team_id,
            "possessions": line.possessions,
            "box": _box_dict(line, _TEAM_LINE_IDENTITY_FIELDS),
        }
        for line in result.team_box_lines
    ]
    await stats_repo.create_team_game_stats_many(conn, team_rows)

    period_rows = []
    for period, home_pts, away_pts in result.period_scores:
        period_rows.append(
            {
                "game_id": game.id,
                "league_id": game.league_id,
                "team_id": game.home_team_id,
                "period": period,
                "points": home_pts,
            }
        )
        period_rows.append(
            {
                "game_id": game.id,
                "league_id": game.league_id,
                "team_id": game.away_team_id,
                "period": period,
                "points": away_pts,
            }
        )
    await stats_repo.upsert_game_period_scores(conn, period_rows)

    await game_repo.finalize_game(
        conn,
        game.id,
        home_score=result.home_score,
        away_score=result.away_score,
        overtime_periods=result.overtime_periods,
        engine_version=engine_version,
        roster_digest=roster_digest,
    )


async def _run_and_persist(
    conn,
    game: Game,
    context: GameContext,
    *,
    roster_digest: str,
    resim: bool,
    write_beat_log: bool,
) -> GameResult:
    """Shared "flip status, [delete-then-]persist" body for both `sim_game`
    and `sim_scheduled_games` -- the caller owns opening the one
    `transaction(ex)` this runs inside (Decision P1-25/P1-26: one
    transaction per game, and re-sim's `FINAL -> IN_PROGRESS` transition
    plus its delete-then-reinsert both live in that same transaction)."""
    if resim:
        log.info(
            "resim: game %s (league %s, season %s) final -> in_progress for re-sim",
            game.id,
            game.league_id,
            game.season,
        )
        await game_repo.set_status(
            conn, game.id, from_status=GameStatus.FINAL, to_status=GameStatus.IN_PROGRESS
        )
        await stats_repo.delete_stats_for_game(conn, game.id)
    else:
        await game_repo.set_status(
            conn, game.id, from_status=GameStatus.SCHEDULED, to_status=GameStatus.IN_PROGRESS
        )

    result = simulate_game(context, rng_seed=game.rng_seed)
    await _persist_game_result(
        conn, game=game, result=result, roster_digest=roster_digest, engine_version=TUNING_VERSION
    )
    if write_beat_log:
        _write_beat_log(game, result)
    return result


async def sim_game(
    ex: Executor,
    game_id: int,
    *,
    tuning: TuningProfile | None = None,
    resim: bool = False,
    write_beat_log: bool = False,
) -> GameResult:
    """Loads the context, runs the engine, persists box + period scores +
    finalization, all inside one `transaction(ex)` (§8.3). `resim=True` is
    required to re-sim an already-`final` game (Decision P1-25) and is
    rejected against any status other than `final`; without `resim=True`,
    only a `scheduled` game may be simmed.

    The legality of both the `final -> in_progress` (resim) and
    `scheduled -> in_progress` (fresh sim) edges is checked against
    `entity_states.GAME_TRANSITIONS` via `is_legal_transition` -- critic
    round 19 B19-2: this used to be a hardcoded reimplementation of the
    same rule with zero production reader of the table anywhere in the
    codebase (`entity_states.py`'s own docstring names this function as the
    intended gate owner). Behaviorally identical to the table today; the
    fix is that a future edit to `GAME_TRANSITIONS` now actually changes
    what this function accepts, instead of silently drifting from it."""
    async with transaction(ex) as conn:
        game = await game_repo.get_game(conn, game_id)
        if game is None:
            raise NotFoundError(f"game {game_id} does not exist")

        if resim:
            if game.status != GameStatus.FINAL or not is_legal_transition(
                GAME_TRANSITIONS, GameStatus.FINAL, GameStatus.IN_PROGRESS
            ):
                raise ValidationError(
                    f"resim=True requires game {game_id} to be final; it is "
                    f"{game.status.value}"
                )
        elif game.status != GameStatus.SCHEDULED or not is_legal_transition(
            GAME_TRANSITIONS, GameStatus.SCHEDULED, GameStatus.IN_PROGRESS
        ):
            raise ValidationError(
                f"game {game_id} has status {game.status.value}, not scheduled -- pass "
                "resim=True to re-sim an already-final game (Decision P1-25)"
            )

        context = await build_context(conn, game, tuning=tuning)
        roster_digest = _roster_digest(context)
        return await _run_and_persist(
            conn,
            game,
            context,
            roster_digest=roster_digest,
            resim=resim,
            write_beat_log=write_beat_log,
        )


async def sim_scheduled_games(
    ex: Executor,
    league_id: int,
    season: int,
    *,
    game_type: GameType = GameType.REGULAR,
    through_game_index: int | None = None,
    tuning: TuningProfile | None = None,
    write_beat_log: bool = False,
    return_results: bool = False,
) -> BatchResult:
    """Loads ONE league snapshot up front via the bulk loaders and reuses it
    across every game in the batch (§11's N+1 risk), then opens one
    `transaction(ex)` PER GAME (never per batch -- a 1,230-game transaction
    would hold a connection for minutes and roll back the whole season on
    one bad row). A game whose roster snapshot is incomplete, or whose sim
    transaction raises, is recorded in `BatchResult.failed_game_ids` and the
    batch continues rather than aborting.

    `game_type` defaults to `REGULAR` -- not named in §8.3's own signature
    list, added here since `game_repo.list_scheduled_games` requires one and
    Phase 1 has no non-regular schedule generator yet (§14.4 defers
    preseason/play-in/playoffs).

    `return_results=True` fills `BatchResult.results` with one `SimmedGame` per
    committed game (see `BatchResult` for why it is opt-in). Between games the
    loop yields to the event loop with `await asyncio.sleep(0)` -- outside any
    transaction, never between the engine call and its persist -- so a block of
    ten games does not hold the loop for its whole run (P5-99.j). The yield
    changes no result: each game's seed is its own `game_index`, not a shared
    stream."""
    tuning = tuning if tuning is not None else TuningProfile()

    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"league {league_id} does not exist")

    games = await game_repo.list_scheduled_games(ex, league_id, season, game_type)
    if through_game_index is not None:
        games = [g for g in games if g.game_index <= through_game_index]
    if not games:
        return BatchResult(games_simmed=0, failed_game_ids=())

    teams_by_id: dict[int, Team] = {
        t.id: t for t in await team_repo.list_teams_for_league(ex, league_id)
    }
    strategy_by_team: dict[int, TeamStrategy] = {}
    for team_id in teams_by_id:
        strategy = await team_repo.get_team_strategy(ex, team_id, season)
        if strategy is not None:
            strategy_by_team[team_id] = strategy

    players = await player_repo.list_players_for_league(
        ex, league_id, roster_status=RosterStatus.ACTIVE
    )
    players_by_team: dict[int, list[Player]] = defaultdict(list)
    for p in players:
        if p.team_id is not None:
            players_by_team[p.team_id].append(p)

    ratings_by_player = {r.player_id: r for r in await ratings_repo.get_for_league(ex, league_id)}
    tendencies_by_player = {
        t.player_id: t for t in await ratings_repo.get_tendencies_for_league(ex, league_id)
    }
    directives_by_player = {
        d.player_id: d for d in await role_repo.list_player_directives_for_league(ex, league_id)
    }
    roles_by_team: dict[int, dict[int, PlayerRole]] = {
        team_id: {
            r.player_id: r
            for r in await role_repo.list_player_roles_for_team(ex, team_id, season)
        }
        for team_id in teams_by_id
    }

    simmed = 0
    failed: list[int] = []
    failures: list[GameFailure] = []
    results: list[SimmedGame] = []
    for position, game in enumerate(games):
        if position:
            await asyncio.sleep(0)
        home_team = teams_by_id.get(game.home_team_id)
        away_team = teams_by_id.get(game.away_team_id)
        home_strategy = strategy_by_team.get(game.home_team_id)
        away_strategy = strategy_by_team.get(game.away_team_id)
        if home_team is None or away_team is None or home_strategy is None or away_strategy is None:
            log.error(
                "sim_scheduled_games: game %s missing team/strategy snapshot data -- skipping",
                game.id,
            )
            failed.append(game.id)
            failures.append(
                GameFailure(game.id, "missing team or strategy snapshot data (no strategy row?)")
            )
            continue

        try:
            home_sim_players = _assemble_sim_players(
                players_by_team.get(game.home_team_id, []),
                ratings_by_player,
                tendencies_by_player,
                roles_by_team.get(game.home_team_id, {}),
                directives_by_player,
                tuning,
            )
            away_sim_players = _assemble_sim_players(
                players_by_team.get(game.away_team_id, []),
                ratings_by_player,
                tendencies_by_player,
                roles_by_team.get(game.away_team_id, {}),
                directives_by_player,
                tuning,
            )
        except NotFoundError:
            log.exception("sim_scheduled_games: game %s roster snapshot incomplete", game.id)
            failed.append(game.id)
            failures.append(GameFailure(game.id, "roster snapshot incomplete"))
            continue

        context = GameContext(
            game_id=game.id,
            season=game.season,
            home=build_sim_team(home_team, home_strategy, home_sim_players),
            away=build_sim_team(away_team, away_strategy, away_sim_players),
            tuning=tuning,
        )
        roster_digest = _roster_digest(context)

        try:
            async with transaction(ex) as conn:
                result = await _run_and_persist(
                    conn,
                    game,
                    context,
                    roster_digest=roster_digest,
                    resim=False,
                    write_beat_log=write_beat_log,
                )
            simmed += 1
            if return_results:
                results.append(
                    SimmedGame(
                        game_id=game.id,
                        game_date=game.game_date,
                        home_team_id=game.home_team_id,
                        away_team_id=game.away_team_id,
                        result=result,
                    )
                )
        except Exception as exc:
            log.exception("sim_scheduled_games: game %s failed and was rolled back", game.id)
            failed.append(game.id)
            failures.append(
                GameFailure(game.id, f"simulation failed and was rolled back: {_cause_text(exc)}")
            )

    return BatchResult(
        games_simmed=simmed,
        failed_game_ids=tuple(failed),
        results=tuple(results),
        failures=tuple(failures),
    )


_CAUSE_TEXT_LIMIT = 200


def _cause_text(exc: Exception) -> str:
    """`ExceptionType: message`, bounded -- a cause goes into a Discord reply."""
    text = f"{type(exc).__name__}: {exc}"
    if len(text) > _CAUSE_TEXT_LIMIT:
        text = text[: _CAUSE_TEXT_LIMIT - 3] + "..."
    return text
