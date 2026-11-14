"""`TeamSnapshot` / `LeagueSnapshot` / `AssetBundle` -- join-shaped engine
types, not `domain` types (docs/design/phase-2-manager-minds.md §3,
Decision P2-5).

Mirrors `sim/context.py`'s `SimPlayer`/`SimTeam` precedent exactly
(Decision P1-4, cited by name in Decision P2-5): a join over several
`domain` dataclasses plus a small set of construction-time-only caches,
never a new `domain/` dataclass and never a new table -- putting a
join-shaped type in `domain/` would break the one-dataclass-per-table
rule the schema-drift test depends on, the same objection Decision P1-4
already answered for `SimPlayer`.

Decision P2-7's `Decimal` -> `float` boundary is exactly here, in
`build_roster_player`, and nowhere else under `managers/` -- the same
"one conversion boundary, one place, everyone downstream reads the
mirrored float" shape `sim/context.py::build_sim_player` already uses for
`PlayerRatings.confidence`. `domain/ratings.py:135` types `confidence` as
`Decimal`; `float * Decimal` raises `TypeError`, so the cast is named
here rather than left to whoever writes the first line that needs it --
the third time this codebase would otherwise pay for the same lesson
(Phase 0's step-8 `SUM`-returns-`Decimal` finding, Phase 1's P1-5
boundary, now this one).

Zero I/O, per Decision P2-2's purity extension: no `dba2.data` import, no
`datetime.now()`, no bare `random.Random()`, no `open()`, no
`os.environ`. The builder functions below assemble already-fetched
`domain` objects -- the actual database reads are `services/gm_service.py`'s
job (not built this dispatch), exactly as `game_sim_service.build_context`
is the one place `sim/context.py`'s builders get real repository data.
"""
from __future__ import annotations

from dataclasses import dataclass

from dba2.domain.contract import Contract, ContractYear
from dba2.domain.draft import DraftPick
from dba2.domain.league import LeagueCapSettings
from dba2.domain.player import Player
from dba2.domain.ratings import PlayerRatings
from dba2.domain.team import Team
from dba2.managers.tuning import CompetitiveWindow


@dataclass(frozen=True)
class PlayerContract:
    """A player's contract header plus every one of its per-season rows,
    sorted ascending by `season` -- `contract_years.salary`/
    `guaranteed_amount`/`option_type` are per-year facts a single
    `contracts.salary` scalar cannot express (Decision 18, cited by §1
    item 5)."""

    contract: Contract
    years: tuple[ContractYear, ...]


@dataclass(frozen=True)
class RosterPlayer:
    """A join over one rostered (or `draft_eligible`) player's identity,
    ability, and contract -- the unit `managers/valuation.py` (step 3)
    prices. `contract` is `None` for a `draft_eligible` prospect, who by
    definition has no `contracts` row (§9.4).

    Per Decision P2-3, everything downstream of this type reads
    `player.overall_cached` as its talent scalar, never the raw 55
    `ratings` fields directly -- the one stated exception is Decision
    P2-9's `defense_first` group tilt, which is the *only* reason
    `ratings` is carried here at all rather than being dropped after the
    `confidence_f` cast."""

    player: Player
    ratings: PlayerRatings
    contract: PlayerContract | None

    # Decision P2-7's conversion boundary.
    confidence_f: float

    @property
    def id(self) -> int:
        return self.player.id


@dataclass(frozen=True)
class AssetBundle:
    """One side of a trade or a free-agent/draft evaluation -- whatever
    `managers/decisions/` (step 8-9) needs to price a package. Not every
    field is populated by every caller: a trade bundle carries `players`
    and `picks`; a signing offer or a draft pick carries just one asset
    in whichever tuple applies. Empty by default so a caller only fills
    in what it has, mirroring `sim/context.py`'s "every field has a
    legal empty/None state" precedent."""

    players: tuple[RosterPlayer, ...] = ()
    picks: tuple[DraftPick, ...] = ()


@dataclass(frozen=True)
class TeamSnapshot:
    """A join over one team's roster, picks, and cap settings -- Decision
    P2-5's `TeamSnapshot`: "a `Team`, its roster of `Player` +
    `PlayerRatings`, its active `Contract`s and `ContractYear`s, its
    `DraftPick`s, its `LeagueCapSettings`, its computed
    `CompetitiveWindow`."

    `window` is `None` until `managers/window.py` (step 4) classifies it
    -- a `TeamSnapshot` is the *input* a window classification reads as
    well as the type that carries the result, so requiring it non-`None`
    at construction would make classification impossible to express as a
    pure function of a snapshot."""

    team: Team
    roster: tuple[RosterPlayer, ...]
    draft_picks: tuple[DraftPick, ...]
    cap_settings: LeagueCapSettings
    window: CompetitiveWindow | None = None

    @property
    def id(self) -> int:
        return self.team.id

    def player_by_id(self, player_id: int) -> RosterPlayer:
        for p in self.roster:
            if p.id == player_id:
                return p
        raise KeyError(f"team {self.team.id} has no rostered player {player_id}")


@dataclass(frozen=True)
class LeagueSnapshot:
    """The whole league at one `(league_id, season)` instant -- the input
    every §7 window score and every §10 archetype/harness sweep needs a
    30-team view for."""

    league_id: int
    season: int
    teams: tuple[TeamSnapshot, ...]

    def team_by_id(self, team_id: int) -> TeamSnapshot:
        for t in self.teams:
            if t.id == team_id:
                return t
        raise KeyError(f"league {self.league_id} has no team {team_id}")


def build_roster_player(
    player: Player,
    ratings: PlayerRatings,
    *,
    contract: PlayerContract | None = None,
) -> RosterPlayer:
    """The one and only `Decimal` -> `float` conversion boundary for
    `player_ratings.confidence` under `managers/` (Decision P2-7).
    `ratings` must belong to `player` -- this function does not
    cross-check `player_id`/`league_id` consistency across them, the same
    contract `sim/context.py::build_sim_player` states for its own
    inputs; that is the caller's responsibility (`services/gm_service.py`,
    not built this dispatch)."""
    return RosterPlayer(
        player=player,
        ratings=ratings,
        contract=contract,
        confidence_f=float(ratings.confidence),
    )


def build_team_snapshot(
    team: Team,
    roster: tuple[RosterPlayer, ...],
    draft_picks: tuple[DraftPick, ...],
    cap_settings: LeagueCapSettings,
    *,
    window: CompetitiveWindow | None = None,
) -> TeamSnapshot:
    """Thin wrapper for symmetry with `build_roster_player` -- `TeamSnapshot`
    has no `Decimal` fields and no caches of its own, but a dedicated
    constructor keeps the "always build through a function, never the raw
    dataclass" convention uniform across both types, mirroring
    `sim/context.py::build_sim_team`'s identical role."""
    return TeamSnapshot(
        team=team,
        roster=roster,
        draft_picks=draft_picks,
        cap_settings=cap_settings,
        window=window,
    )


def build_league_snapshot(
    league_id: int, season: int, teams: tuple[TeamSnapshot, ...]
) -> LeagueSnapshot:
    """Thin wrapper, same reasoning as `build_team_snapshot`."""
    return LeagueSnapshot(league_id=league_id, season=season, teams=teams)
