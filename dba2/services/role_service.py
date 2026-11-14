"""The auto-GM role-construction pass (`docs/design/auto-gm-role-
construction.md`, RC-22/RC-23 for the pass, RC-28 for `arch_z`, RC-37 for
the ranking rule): the `PRESEASON` work unit that writes real
`player_roles` rows for every team a human GM does not manage, plus the
pure per-roster computation behind it.

**What a row carries, and what it does not.** `touch_share` is the
player's minutes target and nothing else -- `sim.game._minutes_shares`
reads it; usage inside a possession still comes from `overall_cached`
(`sim.beats.setup._role_weight`, RC-20), so this pass decides *who plays*,
never *who shoots*. `role` and `rationale` are display-only (RC-0.7).

**The ranking rule -- RC-37's rotation floor (D15, measured as
`tools/spikes/rc31_archetype_spike.py::_rank_at_cell` with `beta = 0`,
`floor_rank = 8`).** Sort the roster by talent (`tilted_overall`). Talent
ranks 1..`FLOOR_RANK - 1` (1-7) keep their places. Everyone at talent rank
`FLOOR_RANK` (8) or below is reordered into two blocks placed after rank
7: first every *firer* (`arch_z > Z_FLOOR`) in talent order, then every
non-firer in talent order. So a firing talent-rank-8 player keeps rank 8
and a buried firer then lands at 9; with a non-firing talent-rank-8
player the buried firer takes 8 and the old rank-8 player drops to 9 (or
lower, if more than one firer is below him). Ranks 1-7 never change, so a
specialist can never reach the starting five through this pass. Every
firer at any rank carries the `{archetype}_specialist` label (RC-32); only
firers at talent rank 9 or worse move up. No points are added to anyone's score -- the
additive bonus (`BETA`, `Z_CAP`, RC-19.6's `beta * z_cap <= 6.0`) was
removed in RC-37 because on the real population no additive cell cleared
criterion 1 without breaking criterion 3, 4b', 5/7 or the rank-1
tie-break (RC-37.2).

**`arch_z` (RC-28.1).** Per player: position-adjust the 40 pool fields,
z-score each field within the team, take each archetype's field-mean minus
the player's own 40-field mean (`u_A`), divide by `S_A`, and take the max
over the seven archetypes. The argmax names the archetype.

**Pinned constants, and where each came from** (all measured on RC-35.4
D2's production population: every 2009-2025 real franchise-season's top
14 by minutes, 501 rosters / 7,014 players; `docs/ledger/reports/
rc37-rotation-floor.md` sections 1 and 7; `python -m tools.spikes.
rc31_archetype_spike --check` re-derives all of them and exits non-zero
on drift):

- `S_A` is not a table of free numbers. It is RC-36's null scale
  `sqrt((1/m - 1/40)(1 - RHO_BAR))`, computed at import from each
  archetype's field count `m`, so it takes only three values
  (m = 2 / 3 / 4 -> 0.3952 / 0.3184 / 0.2720) and cannot drift from its
  one input. `RHO_BAR = 0.671224` is the measured mean pairwise
  correlation of the pool fields within a roster.
- `Z_FLOOR = 2.9682` is the pooled N1-null `arch_z` p97.5 (five
  per-player shuffles, seeds 4-8, 35,070 draws). p97.5, not D13's p95,
  because criterion 7b (<= 3 specialists on >= 95% of rosters) fails at
  p95 (84.4%); RC-36.9 P6 pre-registered exactly this escalation. It is
  a quantile, not a tuned number.
- `FLOOR_RANK = 8`: a one-slot floor at 9 measured 72-79% on criterion 1
  because a second buried firer takes the slot; 8 gives two slots below
  the seven talent-decided ranks (RC-37.3 D17).

**The margins are thin, and stated rather than hidden** (RC-37.4, RC-37.8
risk 1). Criterion 1 (a D6-fixture rank-11 specialist reaches rank <= 9)
passes 7/7 with a minimum of 90.4% (shot_creator), 0.4pp over the bar.
Criterion 7b passes at 95.6% (479 of 501 rosters; the bar needs 476): it
holds for any `Z_FLOOR` at or above 2.950722 on this population, so the
pin is ~0.0175 above the point where 7b flips; one step down the quantile
ladder (pooled p95) fails it (83.8%) and one step up (p98) fails criterion
1 by one target (shot_creator, 89.8%). A future change moves a bar, not
these constants. Measured at this cell (seeds as of `edf5633`, RC-37
as-built correction 2026-09-24): 8.28% of real players fire (null 2.50%),
0.395 deep-bench lifts per roster, a lift on 35.1% of rosters; the
talent-rank-8 player loses rank 8 on 36.1% of rosters (to rank 10 or below
on 8.2%). `perimeter_defender` fires on only
0.14% of players (`r_A3 = 0.874 < 1`); RC-37.5 leaves it in place (D12,
A3-iv) -- wing stoppers are usually lifted under `rim_protector` instead,
because their steals/blocks feed that archetype.

**Philosophy.** Talent here is `tilted_overall`, which is `overall_cached`
for every philosophy except `defense_first` (a re-weighted overall). The
RC-37 measurements used `compute_overall` -- the identity case, equal to
`overall_cached` for a real player -- so they describe six of the seven
philosophies exactly. `defense_first` changes only the talent order the
floor is applied to; `arch_z`, the firing set and the rule itself are
philosophy-independent. Re-measured with `defense_first`'s tilted overall
as talent on the same population, criterion 1's minimum is 91.6%
(interior_finisher) and the talent-rank-8 player loses rank 8 on 36.1% of
rosters -- no worse than the identity case. The philosophy's `kappa` then
tilts the minutes curve (RC-22.3 step 3)."""
from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from dba2.data.db import Executor, transaction
from dba2.data.repositories import player_repo, ratings_repo, role_repo, team_repo
from dba2.domain.player import Player, Position, RosterStatus
from dba2.domain.ratings import PlayerRatings
from dba2.domain.role import PlayerRole
from dba2.domain.team import GmPhilosophy, Team
from dba2.managers.philosophy import PROFILES
from dba2.managers.valuation import tilted_overall
from dba2.services.gm_service import self_managed_team_ids
from dba2.sim.derivation import POSITION_RATING_OFFSETS
from dba2.sim.game import _minutes_curve_raw
from dba2.sim.ratings import (
    _OVERALL_WEIGHTING_GROUPS,  # noqa: SLF001 -- RC-28.1's own cited pool source
)

# ---------------------------------------------------------------------------
# RC-27.2 -- the seven archetypes, field names re-read at HEAD. Disjoint,
# 23 distinct fields (RC-27's criterion 2).
# ---------------------------------------------------------------------------

ARCHETYPES: Mapping[str, tuple[str, ...]] = {
    "rebounder": ("offensive_rebounding", "defensive_rebounding"),
    "rim_protector": ("rim_protection", "post_defense", "help_iq"),
    "perimeter_defender": ("on_ball_defense", "hands", "foul_discipline", "screen_navigation"),
    "floor_spacer": ("catch_shoot_3", "catch_shoot_mid", "free_throw"),
    "shot_creator": ("handle", "first_step", "off_dribble_3", "off_dribble_mid"),
    "playmaker": ("passing_vision", "passing_accuracy", "pass_under_pressure", "ball_security"),
    "interior_finisher": ("finishing_rim", "contact_finishing", "post_scoring"),
}

# RC-32.1's display label per archetype.
_ARCHETYPE_DISPLAY_LABEL: Mapping[str, str] = {
    "rebounder": "rebounder",
    "rim_protector": "rim protector",
    "perimeter_defender": "perimeter defender",
    "floor_spacer": "floor spacer",
    "shot_creator": "shot creator",
    "playmaker": "playmaker",
    "interior_finisher": "interior finisher",
}

# RC-32.2's "{archetype}_specialist" role-label slug -- an interpretive
# call, disclosed: RC-32.2's own text gives two worked examples
# ("rebounding_specialist", "rim_protection_specialist") and separately
# describes "a closed set of 7 x the 6 rank labels" without fully
# specifying how the two combine. This module drops the rank-tier prefix
# when a player fires (`arch_z > Z_FLOOR`) (matching the two literal examples given)
# rather than concatenating both, since `role` is display-only (RC-0.7)
# and nothing under `sim/` reads it either way.
_ARCHETYPE_SPECIALIST_SLUG: Mapping[str, str] = {
    "rebounder": "rebounding",
    "rim_protector": "rim_protection",
    "perimeter_defender": "perimeter_defense",
    "floor_spacer": "floor_spacing",
    "shot_creator": "shot_creation",
    "playmaker": "playmaking",
    "interior_finisher": "interior_finishing",
}

# RC-5.2 / RC-28.1's 40-field pool: RATING_GROUPS minus PHYSICAL, MENTAL,
# DEVELOPMENT -- the _OVERALL_WEIGHTING_GROUPS grouping (pool membership;
# RC-34.5 bug 1's own resolution: the weighting grouping is correct for
# *pool membership*, only the noise MODEL needed the generation grouping,
# and this module measures real empirical spread rather than a closed-form
# noise prediction, so that distinction does not apply here).
_POOL_GROUPS = tuple(
    g for g in _OVERALL_WEIGHTING_GROUPS if g not in ("PHYSICAL", "MENTAL", "DEVELOPMENT")
)
POOL_FIELDS: tuple[str, ...] = tuple(f for g in _POOL_GROUPS for f in _OVERALL_WEIGHTING_GROUPS[g])
assert len(POOL_FIELDS) == 40, f"RC-5.2/RC-28.1's 40-field pool drifted: got {len(POOL_FIELDS)}"

_STANDARDIZATION_FLOOR = 1e-9

# RC-36.4's null scale at RC-37's measured mean pairwise pool-field
# correlation (production population, `docs/ledger/reports/rc37-rotation-
# floor.md` section 1). The one input `S_A` is derived from.
RHO_BAR = 0.671224


def _null_scale(m: int) -> float:
    """RC-36.4: the SD of `u_A` for an `m`-field archetype when a player's
    40 pool fields are exchangeable with mean pairwise correlation
    `RHO_BAR` -- `sqrt((1/m - 1/40)(1 - RHO_BAR))`."""
    return math.sqrt((1.0 / m - 1.0 / len(POOL_FIELDS)) * (1.0 - RHO_BAR))


S_A: Mapping[str, float] = {name: _null_scale(len(fields)) for name, fields in ARCHETYPES.items()}
assert set(S_A) == set(ARCHETYPES)

# RC-37.4: the pooled five-seed N1-null `arch_z` p97.5 on the production
# population -- a firer is `arch_z > Z_FLOOR` (see the module docstring for
# why p97.5 and how close it sits to criterion 7b's flip).
Z_FLOOR = 2.9682

# RC-37.3 D15/D17: talent ranks 1..FLOOR_RANK-1 are never moved; firers at
# talent rank FLOOR_RANK or below are ordered ahead of non-firers there.
FLOOR_RANK = 8

# RC-22.3 step 3's kappa -> lambda piecewise-linear anchors.
_LAMBDA_ANCHORS: tuple[tuple[float, float], ...] = ((6.0, 1.12), (7.5, 1.00), (10.0, 0.88))

# RC-15's realized quantum, in raw minutes.
_QUANTUM_MINUTES = 1.0

# RC-22.3 step 6's rank-tier labels.
_RANK_LABEL_BOUNDS: tuple[tuple[int, str], ...] = (
    (1, "primary_option"),
    (3, "secondary_option"),
    (5, "starter"),
    (8, "rotation"),
    (11, "bench"),
)
_DEEP_BENCH_LABEL = "deep_bench"


@dataclass(frozen=True)
class RoleAssignment:
    player_id: int
    rank: int
    role_label: str
    minutes_share: Decimal
    rationale: str


def _lambda_for_kappa(kappa: float) -> float:
    (k0, l0), (k1, l1), (k2, l2) = _LAMBDA_ANCHORS
    if kappa <= k0:
        return l0
    if kappa >= k2:
        return l2
    if kappa <= k1:
        t = (kappa - k0) / (k1 - k0)
        return l0 + t * (l1 - l0)
    t = (kappa - k1) / (k2 - k1)
    return l1 + t * (l2 - l1)


def _rank_label(rank: int) -> str:
    for bound, label in _RANK_LABEL_BOUNDS:
        if rank <= bound:
            return label
    return _DEEP_BENCH_LABEL


def _position_adjust(raw: float, position: Position, field: str) -> float:
    return raw - POSITION_RATING_OFFSETS[position][field]


def _team_relative_z(
    rows: Sequence[tuple[int, Position, Mapping[str, float]]],
) -> dict[int, dict[str, float]]:
    """RC-28.1 steps 1-2, over one team's roster. `rows`: (player_id,
    position, {field: raw_value}) for every player being ranked together.
    `strategy_service._STANDARDIZATION_FLOOR` behaviour: a team SD under
    the floor makes every z on that field exactly 0.0."""
    adjusted: dict[int, dict[str, float]] = {
        pid: {f: _position_adjust(raw[f], pos, f) for f in POOL_FIELDS} for pid, pos, raw in rows
    }
    result: dict[int, dict[str, float]] = {pid: {} for pid, _, _ in rows}
    for f in POOL_FIELDS:
        values = [adjusted[pid][f] for pid, _, _ in rows]
        mean = statistics.mean(values)
        sd = statistics.pstdev(values)
        for pid, _, _ in rows:
            result[pid][f] = 0.0 if sd < _STANDARDIZATION_FLOOR else (adjusted[pid][f] - mean) / sd
    return result


def _archetype_composite_excess(y: Mapping[str, float]) -> dict[str, float]:
    """RC-28.1 steps 3-4: own-profile mean, then each archetype's
    composite excess over it."""
    y_bar = statistics.mean(y[f] for f in POOL_FIELDS)
    return {
        name: statistics.mean(y[f] for f in fields) - y_bar for name, fields in ARCHETYPES.items()
    }


def arch_z(u: Mapping[str, float]) -> tuple[float, str]:
    """RC-28.1 steps 5-6: scale-normalize by the pinned `S_A`, then take
    the max over the 7 archetypes. Returns `(arch_z, archetype_key)`, ties
    broken by `ARCHETYPES`' declaration order (Python's `max` keeps the
    first maximal key on a tie when iterating a `dict` in insertion
    order)."""
    scaled = {name: u[name] / S_A[name] for name in ARCHETYPES}
    label = max(scaled, key=lambda k: scaled[k])
    return scaled[label], label


def _rationale(
    *,
    rank: int,
    talent_rank: int,
    n: int,
    tilted_ovr: int,
    archetype_label: str | None,
    philosophy_name: str,
    lam: float,
    minutes: float,
) -> str:
    """RC-22.4 / RC-32.2's rationale template, with RC-37.7 step 1's
    floor clause. Deterministic, no rng, no timestamps, fixed `:.1f`
    formatting throughout, so a diff of two seasons' role tables is
    readable. The specialist clause says what the floor actually did:
    lifted into the rotation (rank <= `FLOOR_RANK`), moved up but still
    outside it (the third or later firer in the reordered block), or not
    moved at all."""
    clause1 = f"Ranked {rank}/{n} on this roster ({tilted_ovr} ovr"
    if archetype_label is not None:
        display = _ARCHETYPE_DISPLAY_LABEL[archetype_label]
        a_display = f"{'an' if display[0] in 'aeiou' else 'a'} {display}"
        if rank < talent_rank and rank <= FLOOR_RANK:
            clause1 += f", lifted to the rotation as {a_display}"
        elif rank < talent_rank:
            clause1 += f", moved up the bench as {a_display}"
        else:
            clause1 += f", {a_display} specialist"
    clause1 += ")"

    clause3 = ""
    if lam != 1.0:
        pct = round(abs(lam - 1.0) * 100)
        direction = "steepened" if lam > 1.0 else "flattened"
        clause3 = f"; {philosophy_name} minutes curve {direction} {pct}%"

    clause4 = f"; target {minutes:.1f} mpg"

    text = clause1 + clause3 + clause4 + "."
    return text[:200]


def rotation_floor_order(talent_order: Sequence[int], firers: frozenset[int]) -> list[int]:
    """RC-37.3 D15, exactly as measured (`rc31_archetype_spike.
    _rank_at_cell`, `beta = 0`, `floor_rank = FLOOR_RANK`): `talent_order`
    is the roster's player ids sorted by talent, best first; the result is
    the final rank order. The first `FLOOR_RANK - 1` ids are returned
    unchanged; every id from talent rank `FLOOR_RANK` down is split into
    firers then non-firers, each block keeping talent order. A firing
    talent-rank-8 player is therefore the first firer and stays at 8."""
    head = list(talent_order[: FLOOR_RANK - 1])
    tail = talent_order[FLOOR_RANK - 1 :]
    firing = [pid for pid in tail if pid in firers]
    return head + firing + [pid for pid in tail if pid not in firers]


def construct_team_roles(
    players: Sequence[Player],
    ratings_by_player: Mapping[int, PlayerRatings],
    *,
    philosophy: GmPhilosophy,
    season: int,  # noqa: ARG001 -- not read by the pure computation; kept for RC-22.3's own signature (a season-keyed row is the caller's concern, not this function's)
) -> tuple[RoleAssignment, ...]:
    """RC-22.3's pure half: zero I/O, a pure function of one roster. Sorts
    `players` by `tilted_overall` (ties by id), applies the rotation floor
    (`rotation_floor_order`, firers being `arch_z > Z_FLOOR`), tilts
    `_MINUTES_CURVE` by the philosophy's `kappa`, and returns one
    `RoleAssignment` per player, in no particular order (callers that need
    rank order should re-derive it from `.rank`, which this function pins
    as the ground truth)."""
    profile = PROFILES[philosophy]
    n = len(players)

    rows: list[tuple[int, Position, Mapping[str, float]]] = []
    for p in players:
        ratings = ratings_by_player[p.id]
        raw = {f: getattr(ratings, f) for f in POOL_FIELDS}
        rows.append((p.id, p.primary_position, raw))
    y_by_player = _team_relative_z(rows)

    talent_by_id: dict[int, int] = {}
    label_by_id: dict[int, str | None] = {}
    for p in players:
        ratings = ratings_by_player[p.id]
        talent_by_id[p.id] = tilted_overall(p.overall_cached, ratings, p.primary_position, profile)
        z, label = arch_z(_archetype_composite_excess(y_by_player[p.id]))
        label_by_id[p.id] = label if z > Z_FLOOR else None

    talent_order = sorted(talent_by_id, key=lambda pid: (-talent_by_id[pid], pid))
    talent_rank = {pid: r for r, pid in enumerate(talent_order, start=1)}
    firers = frozenset(pid for pid, label in label_by_id.items() if label is not None)
    ranked = rotation_floor_order(talent_order, firers)

    lam = _lambda_for_kappa(profile.kappa)
    a_bar = 240.0 / n if n else 0.0
    philosophy_name = philosophy.value.replace("_", " ")

    assignments: list[RoleAssignment] = []
    for rank, player_id in enumerate(ranked, start=1):
        label = label_by_id[player_id]
        a_i = _minutes_curve_raw(rank)
        tilted_minutes = max(a_bar + lam * (a_i - a_bar), _QUANTUM_MINUTES)
        minutes_share = tilted_minutes / 240.0
        rationale = _rationale(
            rank=rank,
            talent_rank=talent_rank[player_id],
            n=n,
            tilted_ovr=talent_by_id[player_id],
            archetype_label=label,
            philosophy_name=philosophy_name,
            lam=lam,
            minutes=tilted_minutes,
        )
        if label is not None:
            role_label = f"{_ARCHETYPE_SPECIALIST_SLUG[label]}_specialist"
        else:
            role_label = _rank_label(rank)
        assignments.append(
            RoleAssignment(
                player_id=player_id,
                rank=rank,
                role_label=role_label,
                minutes_share=Decimal(str(round(minutes_share, 4))),
                rationale=rationale,
            )
        )
    return tuple(assignments)


# ---------------------------------------------------------------------------
# RC-22.1/22.2/22.5 -- the impure half: I/O, the PRESEASON work unit.
# ---------------------------------------------------------------------------


async def construct_ai_roles_for_league(ex: Executor, *, league_id: int, season: int) -> None:
    """`season_service`'s `PRESEASON` entry work, unit 2 (RC-22.1, C11 --
    placed AFTER `_ensure_team_strategies` and BEFORE `_generate_schedule`
    by the caller). Writes real `player_roles` rows for every team NOT in
    `gm_service.self_managed_team_ids` (RC-22.2, C9's shared predicate),
    over that team's `RosterStatus.ACTIVE` roster (RC-22.5, matching
    `game_sim_service.build_context`'s own scope), skipping any row
    already `locked = TRUE` (RC-22.5's first reader of that column) while
    still letting the locked player consume a rank slot."""
    async with transaction(ex) as conn:
        teams = await team_repo.list_teams_for_league(conn, league_id)
        self_managed = self_managed_team_ids(teams)
        for team in teams:
            if team.id in self_managed:
                continue
            await _construct_roles_for_team(conn, team, season=season)


async def _construct_roles_for_team(ex: Executor, team: Team, *, season: int) -> None:
    all_players = await player_repo.list_players_for_team(ex, team.id)
    active_players = [p for p in all_players if p.roster_status is RosterStatus.ACTIVE]
    if not active_players:
        return
    ratings_rows = await ratings_repo.get_for_team(ex, team.id)
    ratings_by_player = {r.player_id: r for r in ratings_rows}
    active_players = [p for p in active_players if p.id in ratings_by_player]
    if not active_players:
        return

    existing_roles = await role_repo.list_player_roles_for_team(ex, team.id, season)
    existing_by_player: dict[int, PlayerRole] = {r.player_id: r for r in existing_roles}

    assignments = construct_team_roles(
        active_players, ratings_by_player, philosophy=team.gm_philosophy, season=season
    )
    for assignment in assignments:
        existing = existing_by_player.get(assignment.player_id)
        if existing is not None and existing.locked:
            continue
        await role_repo.upsert_player_role(
            ex,
            league_id=team.league_id,
            team_id=team.id,
            season=season,
            player_id=assignment.player_id,
            role=assignment.role_label,
            touch_share=assignment.minutes_share,
            rationale=assignment.rationale,
            assigned_by=None,
            locked=False,
        )
