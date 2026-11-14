"""`SimPlayer` / `SimTeam` / `GameContext` -- join-shaped engine types, not
`domain` types (phase-1-simulation-engine.md §3, Decision P1-4/P1-5).

Decision P1-4: `SimPlayer` is a `Player` plus its `PlayerRatings` plus its
`PlayerTendencies` plus its optional `PlayerRole`/`PlayerDirective`, plus a
small set of precomputed scalars that are constant for the whole game (no
dependency on lineup, action type, role assignment, or runtime state).
Frozen, constructed once per game, never mutated -- all mutation lives in
`state.py` (not built this dispatch).

Decision P1-5: the `Decimal` -> `float` conversion boundary is exactly
here, `build_sim_player`/`build_sim_team`, and nowhere else under `sim/`.
`PlayerRole.touch_share` and `PlayerRatings.confidence` are `Decimal`
fields on the real domain dataclasses (domain/role.py, domain/ratings.py);
this module converts each to `float` once and mirrors it as its own
`_f`-suffixed attribute (`touch_share_f`, `confidence_f`). Every other
engine module reads only the mirrored `float` attribute, never the raw
`Decimal` domain field. This module -- like every module under `sim/` --
never imports `decimal` or names `Decimal`; `float()` is a builtin and
works on a `Decimal` operand without either.

Zero I/O, per Decision P1-1: `domain` and `core` only, no `dba2.data`.
"""
from __future__ import annotations

from dataclasses import dataclass

from dba2.domain.player import Player
from dba2.domain.ratings import PlayerRatings, PlayerTendencies
from dba2.domain.role import PlayerDirective, PlayerRole
from dba2.domain.team import Team, TeamStrategy
from dba2.sim.composites import (
    loose_ball_contribution as _loose_ball_contribution,
)
from dba2.sim.composites import (
    normalize_physical,
)
from dba2.sim.composites import (
    rim_protection_contribution as _rim_protection_contribution,
)
from dba2.sim.composites import (
    scheme_contribution as _scheme_contribution,
)
from dba2.sim.composites import (
    spacing_contribution as _spacing_contribution,
)
from dba2.sim.tuning import TuningProfile


@dataclass(frozen=True)
class SimPlayer:
    """A join over one player's identity/ability/propensity/role rows plus
    the construction-time-only caches Decision P1-4 allows (see this
    module's docstring). Constructed exclusively via `build_sim_player`
    below -- never call the dataclass constructor directly outside this
    module, since the `_f`-suffixed and cached fields must stay derived
    from the same `ratings`/`role`/`tuning` the rest of the instance holds.
    """

    player: Player
    ratings: PlayerRatings
    tendencies: PlayerTendencies
    role: PlayerRole | None
    directive: PlayerDirective | None

    # Decision P1-5's conversion boundary.
    touch_share_f: float | None
    confidence_f: float

    # Decision P1-4/P1-6/P1-13's legal construction-time caches.
    wingspan_normalized: float
    height_normalized: float
    spacing_contribution: float
    scheme_contribution: float
    # BDA-4.1 (docs/design/block-defender-attribution.md), revised by
    # block-attribution-v2.md BA-2.4: each helper's weak-side rim-help
    # scalar, read for both his share and his contest -- same construction-
    # time-cache legality as the two above.
    rim_protection_contribution: float
    # docs/design/turnover-recovery.md TR-4.4/TR-4.7: the loose-ball
    # contest's per-player scalar (both lineups' means feed the recovery
    # probability; it weights the offense's recoverer draw) -- same
    # construction-time-cache legality as the three above.
    loose_ball_contribution: float

    @property
    def id(self) -> int:
        return self.player.id


@dataclass(frozen=True)
class SimTeam:
    """A team plus its live strategy row plus its full *available* roster
    (every player eligible to enter the rotation, per Decision P1-22's
    `roster_digest` scope -- not just whoever the rotation model actually
    plays)."""

    team: Team
    strategy: TeamStrategy
    players: tuple[SimPlayer, ...]

    @property
    def id(self) -> int:
        return self.team.id

    def player_by_id(self, player_id: int) -> SimPlayer:
        for p in self.players:
            if p.id == player_id:
                return p
        raise KeyError(f"team {self.team.id} has no available player {player_id}")


@dataclass(frozen=True)
class GameContext:
    """The engine's whole input surface for one game. Built once by
    `game_sim_service.build_context` (step 8, not this dispatch) and then
    handed to the pure engine with no further database access -- the
    entire reason Option B (§2.1) is faster than a query-per-possession
    architecture."""

    game_id: int
    season: int
    home: SimTeam
    away: SimTeam
    tuning: TuningProfile


def build_sim_player(
    player: Player,
    ratings: PlayerRatings,
    tendencies: PlayerTendencies,
    tuning: TuningProfile,
    *,
    role: PlayerRole | None = None,
    directive: PlayerDirective | None = None,
) -> SimPlayer:
    """The one and only `Decimal` -> `float` conversion boundary (Decision
    P1-5) and the one place Decision P1-4's construction-time caches get
    computed. `ratings`/`tendencies`/`role`/`directive` must all belong to
    `player` -- this function does not cross-check `player_id`/`league_id`
    consistency across them; that is `game_sim_service.build_context`'s
    responsibility as the caller assembling these from repository reads."""
    touch_share_f = float(role.touch_share) if role is not None else None
    confidence_f = float(ratings.confidence)
    wingspan_normalized = normalize_physical(
        player.wingspan_in, tuning.physical_anchors.wingspan
    )
    height_normalized = normalize_physical(player.height_in, tuning.physical_anchors.height)
    return SimPlayer(
        player=player,
        ratings=ratings,
        tendencies=tendencies,
        role=role,
        directive=directive,
        touch_share_f=touch_share_f,
        confidence_f=confidence_f,
        wingspan_normalized=wingspan_normalized,
        height_normalized=height_normalized,
        spacing_contribution=_spacing_contribution(ratings, tuning),
        scheme_contribution=_scheme_contribution(ratings, tuning),
        rim_protection_contribution=_rim_protection_contribution(
            ratings, wingspan_normalized, tuning
        ),
        loose_ball_contribution=_loose_ball_contribution(ratings, tuning),
    )


def build_sim_team(team: Team, strategy: TeamStrategy, players: tuple[SimPlayer, ...]) -> SimTeam:
    """Thin wrapper for symmetry with `build_sim_player` -- `SimTeam` has
    no `Decimal` fields and no caches of its own, but a dedicated
    constructor keeps the "always build through a function, never the raw
    dataclass" convention uniform across both types."""
    return SimTeam(team=team, strategy=strategy, players=players)
