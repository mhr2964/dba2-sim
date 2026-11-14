"""Hand-built `SimPlayer`/`SimTeam` fixtures for Tier 1 unit tests
(phase-1-simulation-engine.md §9: "every beat resolver ... against
hand-built SimPlayer fixtures in tests/fixtures/sim.py").

Every one of `PlayerRatings`' 55 rating fields gets a distinct value per
player by construction (`_distinct_ratings` below) -- this project's
established fixture discipline (`EFFORT_LEDGER.md` has hit "same-value
fixture hides a mapper/logic bug" four separate times in Phase 0's critic
rounds) applies just as much to the sim layer's weighted-mean/ability-
vector code as it did to the repository mappers that pattern was found in:
a field-swap bug in an ability-vector weight dict would be just as
invisible against a flat/uniform fixture as a mapper field swap was.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal

from dba2.domain.player import Player, Position, RosterStatus
from dba2.domain.ratings import PlayerRatings, PlayerTendencies, RatingSource
from dba2.domain.role import DefenseMode, PlayerDirective, PlayerRole, ShotDiet, UsageMode
from dba2.domain.team import (
    DefensiveScheme,
    GmPhilosophy,
    OffensiveScheme,
    Team,
    TeamStrategy,
    Timeline,
)
from dba2.sim.context import GameContext, SimPlayer, SimTeam, build_sim_player, build_sim_team
from dba2.sim.tuning import TuningProfile

# The 8 `PlayerRatings` fields that are not one of the 55 averaged rating
# columns (mirrors `dba2.sim.ratings._NON_RATING_FIELDS` -- duplicated
# here, not imported, since that name is that module's own private
# implementation detail and this is a test-fixture concern, not a sim/
# concern).
_NON_RATING_FIELDS = frozenset(
    {
        "player_id",
        "league_id",
        "source",
        "derived_from_season",
        "confidence",
        "updated_at",
        "peak_age_start",
        "peak_age_end",
    }
)
_RATING_FIELD_ORDER: tuple[str, ...] = tuple(
    f.name for f in dataclasses.fields(PlayerRatings) if f.name not in _NON_RATING_FIELDS
)
assert len(_RATING_FIELD_ORDER) == 55


def _distinct_ratings(seed: int) -> dict[str, int]:
    """55 pairwise-distinct values in [15, 94] (well inside the `rating`
    domain's [0, 99] range). `i` ranges over 55 *consecutive* integers
    (step 1) before the modulus is applied, which is what guarantees
    distinctness regardless of `seed` -- shifting a run of 55 consecutive
    integers by a constant offset modulo 80 can never introduce a
    collision among them, since 55 < 80."""
    return {
        name: 15 + ((i + seed * 23) % 80) for i, name in enumerate(_RATING_FIELD_ORDER)
    }


def make_ratings(
    player_id: int, *, league_id: int = 1, seed: int | None = None, **overrides: int
) -> PlayerRatings:
    values = _distinct_ratings(seed if seed is not None else player_id)
    values.update(overrides)
    return PlayerRatings(
        player_id=player_id,
        league_id=league_id,
        source=RatingSource.GENERATED,
        derived_from_season=None,
        confidence=Decimal("0.85"),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        peak_age_start=24 + (player_id % 6),
        peak_age_end=30 + (player_id % 6),
        **values,
    )


def make_player(
    player_id: int,
    *,
    league_id: int = 1,
    primary_position: Position = Position.SG,
    height_in: int = 78,
    weight_lb: int = 210,
    wingspan_in: int = 80,
    team_id: int | None = 1,
    overall_cached: int = 60,
) -> Player:
    return Player(
        id=player_id,
        league_id=league_id,
        first_name=f"First{player_id}",
        last_name=f"Last{player_id}",
        external_ref=None,
        primary_position=primary_position,
        secondary_position=None,
        height_in=height_in,
        weight_lb=weight_lb,
        wingspan_in=wingspan_in,
        birth_date=date(1998, 1, 1),
        draft_year=2020,
        draft_pick_number=15,
        years_pro=5,
        team_id=team_id,
        roster_status=RosterStatus.ACTIVE,
        overall_cached=overall_cached,
        overall_computed_at=datetime(2026, 1, 1, tzinfo=UTC),
        retired_in_season=None,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# B2 fix (critic-proven): `make_tendencies` used to hand every fixture
# player a byte-identical constant table -- `PlayerRatings` was the only
# genuinely per-player-distinct fixture, so every tendency-driven
# selection path (setup beat's action-type/role weighting, Decision P1-8)
# was completely unexercised by field-swap mutations. These 13 fields have
# no cross-field constraint (each is its own independent `tendency` domain
# value, 0-100) and get the same construction `_distinct_ratings` above
# uses: `n` consecutive integers offset by `seed`, taken mod a modulus
# coprime with the stride so neither an in-player collision (13 < 47) nor
# a same-field cross-player collision (47 is prime, so it's coprime with
# every stride) is possible for any two distinct seeds within one period.
#
# B2-2 fix (critic-proven): pairwise-distinct-within-each-group was not
# enough -- critic proved 3 field-swap mutations that swap a field from
# *this* 13-field group with a field from the 4-field `_SHOT_FREQ_*` group
# below survived the full suite, because nothing stopped the two groups'
# formulas from landing on the same integer for a given player (a single-
# value tie) or the same lineup mean (a 5-player-average tie). Rather than
# chase down individual coincidences, the two groups' value sets are now
# made *disjoint* by parity: every field in this group is forced ODD
# (`1 + 2 * ...` below); every field in `_SHOT_FREQ_*` is forced EVEN
# (`_SHOT_FREQ_BASE` and both nudges are even). An odd integer can never
# equal an even one, and a sum of 5 odd values is always odd while a sum
# of 5 even values is always even (the lineup size every field-swap test
# in `test_setup.py` actually uses), so neither collision shape is
# representable anymore, for any seed -- not just the ones critic's
# mutation harness happened to try.
_GENERAL_TENDENCY_FIELD_ORDER: tuple[str, ...] = (
    "drive_freq",
    "pullup_vs_catch",
    "iso_freq",
    "pnr_ball_freq",
    "pnr_screen_freq",
    "transition_freq",
    "pass_freq",
    "spot_up_freq",
    "cut_freq",
    "crash_oreb_freq",
    "help_gamble_freq",
    "steal_gamble_freq",
    "foul_freq",
)

_SHOT_FREQ_FIELD_ORDER: tuple[str, ...] = (
    "shot_freq_rim",
    "shot_freq_mid",
    "shot_freq_three",
    "shot_freq_post",
)
# A fixed distinct-4 partition of 100, nudged and then rotated per-seed
# below -- can't reuse the general construction verbatim since these four
# must sum to exactly 100 (DB CHECK), not just be independently distinct.
# Sorted descending, evenly spaced 14 apart (46-32=32-18=18-4=14) so the
# nudge below (up to +/-6) has plenty of headroom before crossing into a
# neighboring value's territory -- an arbitrary-but-generous choice, not a
# claim that 14 is the widest gap 4 positive/even/sum-100 terms could have.
_SHOT_FREQ_BASE: tuple[int, ...] = (46, 32, 18, 4)


def _distinct_general_tendencies(seed: int) -> dict[str, int]:
    """13 pairwise-distinct ODD values in [1, 93]. `i` ranges over 13
    *consecutive* residues mod 47 (prime) before the `1 + 2 * ...` step --
    same in-player/cross-player distinctness argument as `_distinct_ratings`
    above, just with a smaller (odd, prime) modulus so the produced range
    stays disjoint from `_distinct_shot_freq_split`'s even values (see the
    B2-2 comment above)."""
    return {
        name: 1 + 2 * ((i + seed * 7) % 47)
        for i, name in enumerate(_GENERAL_TENDENCY_FIELD_ORDER)
    }


def _distinct_shot_freq_split(seed: int) -> dict[str, int]:
    """4 pairwise-distinct positive EVEN values summing to exactly 100.

    Nudges `_SHOT_FREQ_BASE` (sorted descending) *before* rotating it into
    the 4 field slots, pairing by rank rather than by array position: the
    top 2 values each lose a delta, the bottom 2 each gain one, with each
    pair's deltas transferred from its partner (`base[0] -= delta_x`,
    `base[3] += delta_x`; `base[1] -= delta_y`, `base[2] += delta_y`) so
    the sum-to-100 invariant holds by construction. Pairing by rank (not
    by post-rotation array position) is what keeps the smallest base value
    (4) safely away from zero regardless of which field it rotates into --
    it only ever *gains* a delta, never loses one. `delta_x`/`delta_y` are
    independently drawn from {2, 4, 6}, and every nudged value's possible
    range ([40,44], [26,30], [20,24], [6,10]) is separated from its
    neighbors' by a margin of at least 10, comfortably clear of the 14-wide
    gap between adjacent un-nudged base values -- so no combination of
    rotation/delta_x/delta_y can ever make two of a player's 4 shot-freq
    values collide, or push one non-positive.

    Only *after* nudging is the result rotated through the 4 field slots by
    `seed % 4` (so which field gets which nudged value differs by seed).
    B2-2 fix (critic-proven): the previous version rotated first and only
    nudged the first 2 rotated slots, leaving `shot_freq_three`/
    `shot_freq_post` (rotated slots 2/3) a pure `seed % 4` rotation with
    period-4 repetition -- two players 16 apart got byte-identical splits.

    `delta_x`/`delta_y` both key off `k = seed // 4` (the "which step
    within this rotation class" counter) rather than off `seed` directly,
    with `delta_y`'s cycle offset by 1 step from `delta_x`'s (`(k + 1) % 3`
    vs `k % 3`) so *both* deltas change on every single step within a
    rotation class -- not just one of them. That's the fix for a subtler
    version of the same B2-2 bug this rewrite first tried and
    critic-equivalent self-review caught: keying `delta_y` off a
    slower-changing digit of `seed` (e.g. `seed // 12`, which only changes
    every 3 steps) leaves it constant across several consecutive
    same-rotation players, and since a given field depends on *only one*
    of `delta_x`/`delta_y` for any fixed rotation (never both -- see the
    nudge-index table above), whichever field happens to depend on the
    constant one collides across those players anyway, just on a
    different pair of fields than the original bug.

    This bounds the guaranteed-unique window to 12 seeds (`rotation`'s
    period 4 times `delta_x`/`delta_y`'s shared period 3) rather than a
    larger number -- deliberately: {2, 4, 6} is already the full safe
    range (`delta_x`/`delta_y` any larger risks crossing into a
    neighboring base value's territory or going non-positive, both
    verified by brute force against every `(delta_x, delta_y)` combination
    in that range), and every player this project's tests ever compares
    for exact field-value equality comes from one `_lineup(...)` call --
    at most 4 apart, well inside a 12-seed window."""
    k = seed // 4
    delta_x = 2 * (1 + (k % 3))
    delta_y = 2 * (1 + ((k + 1) % 3))
    nudged = [
        _SHOT_FREQ_BASE[0] - delta_x,
        _SHOT_FREQ_BASE[1] - delta_y,
        _SHOT_FREQ_BASE[2] + delta_y,
        _SHOT_FREQ_BASE[3] + delta_x,
    ]
    rotation = seed % 4
    rotated = nudged[rotation:] + nudged[:rotation]
    return dict(zip(_SHOT_FREQ_FIELD_ORDER, rotated, strict=True))


def make_tendencies(
    player_id: int, *, league_id: int = 1, seed: int | None = None, **overrides: int
) -> PlayerTendencies:
    s = seed if seed is not None else player_id
    values: dict[str, int] = {
        **_distinct_shot_freq_split(s),
        **_distinct_general_tendencies(s),
    }
    values.update(overrides)
    zone_sum = (
        values["shot_freq_rim"]
        + values["shot_freq_mid"]
        + values["shot_freq_three"]
        + values["shot_freq_post"]
    )
    assert zone_sum == 100, f"shot_freq_* must sum to 100 (DB CHECK), got {zone_sum}"
    return PlayerTendencies(
        player_id=player_id,
        league_id=league_id,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        **values,
    )


def make_role(
    player_id: int,
    *,
    league_id: int = 1,
    team_id: int = 1,
    season: int = 2026,
    role: str = "starter",
    touch_share: str = "0.22",
) -> PlayerRole:
    return PlayerRole(
        id=player_id,
        league_id=league_id,
        team_id=team_id,
        season=season,
        player_id=player_id,
        role=role,
        touch_share=Decimal(touch_share),
        rationale=None,
        assigned_by=None,
        locked=False,
        assigned_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def make_directive(
    player_id: int,
    *,
    league_id: int = 1,
    shot_diet: ShotDiet | None = None,
    usage_mode: UsageMode | None = None,
    defense_mode: DefenseMode | None = None,
) -> PlayerDirective:
    """PD-1 retype (2026-09-23): `role_mode` is gone (PD-6); `clutch_mode`
    has no command surface (PD-7) and no caller of this fixture has ever
    needed a non-`None` value for it, so it stays a fixed `None` here
    rather than growing a fourth parameter nothing exercises."""
    return PlayerDirective(
        id=player_id,
        league_id=league_id,
        player_id=player_id,
        shot_diet=shot_diet,
        usage_mode=usage_mode,
        defense_mode=defense_mode,
        clutch_mode=None,
        set_by=None,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def make_team(
    team_id: int,
    *,
    league_id: int = 1,
    conference: str = "East",
    division: str = "Atlantic",
) -> Team:
    return Team(
        id=team_id,
        league_id=league_id,
        code=f"T{team_id:02d}",
        name=f"Team {team_id}",
        city=f"City {team_id}",
        conference=conference,
        division=division,
        manager_user_id=None,
        gm_philosophy=GmPhilosophy.TENDENCY_RESPECTER,
        timeline=Timeline.RETOOL,
        home_market_size=3,
    )


def make_team_strategy(
    team_id: int,
    *,
    league_id: int = 1,
    season: int = 2026,
    pace_target: int = 50,
    offensive_scheme: OffensiveScheme = OffensiveScheme.MOTION,
    defensive_scheme: DefensiveScheme = DefensiveScheme.DROP,
    help_aggression: int = 50,
    three_point_bias: int = 50,
    crash_oreb_bias: int = 50,
) -> TeamStrategy:
    return TeamStrategy(
        league_id=league_id,
        team_id=team_id,
        season=season,
        pace_target=pace_target,
        offensive_scheme=offensive_scheme,
        defensive_scheme=defensive_scheme,
        help_aggression=help_aggression,
        three_point_bias=three_point_bias,
        crash_oreb_bias=crash_oreb_bias,
        set_by=None,
    )


def make_sim_player(
    player_id: int,
    *,
    tuning: TuningProfile,
    league_id: int = 1,
    primary_position: Position = Position.SG,
    height_in: int = 78,
    wingspan_in: int = 80,
    team_id: int | None = 1,
    overall_cached: int = 60,
    with_role: bool = True,
    touch_share: str = "0.22",
    ratings_overrides: dict[str, int] | None = None,
    tendencies_overrides: dict[str, int] | None = None,
    directive: PlayerDirective | None = None,
) -> SimPlayer:
    """The full construction path a real `build_context` would use, given
    hand-built domain rows instead of repository reads -- exercises the
    same `Decimal` -> `float` boundary and construction-time caches every
    beat's tests rely on. `directive` defaults to `None` (no row), the
    same "never set" state every real player starts in -- pass a
    `make_directive(...)` fixture to exercise PD-3/PD-4/PD-5's sim hooks."""
    player = make_player(
        player_id,
        league_id=league_id,
        primary_position=primary_position,
        height_in=height_in,
        wingspan_in=wingspan_in,
        team_id=team_id,
        overall_cached=overall_cached,
    )
    ratings = make_ratings(player_id, league_id=league_id, **(ratings_overrides or {}))
    tendencies = make_tendencies(player_id, league_id=league_id, **(tendencies_overrides or {}))
    role = (
        make_role(player_id, league_id=league_id, team_id=team_id or 1, touch_share=touch_share)
        if with_role
        else None
    )
    return build_sim_player(player, ratings, tendencies, tuning, role=role, directive=directive)


# B6 fix (critic-proven): `make_sim_team`'s players used to all share the
# same `touch_share`/`overall_cached`/`primary_position`/`height_in`
# (SG, 78in, 60 overall, "0.22" touch_share, every player) -- this
# project's recurring "fixture reuses the same value, hides a real
# branch" failure class (see this file's own module docstring, and
# `EFFORT_LEDGER.md`'s round-6 critic entry) at the roster-knob level
# rather than the rating-field level. Two real branches were completely
# unexercised across the whole Tier-2 suite as a result:
# `game._minutes_shares`' `touch_share_f is not None` vs. no-role
# `overall_cached`-rank-tier split, and `playbook.assign_matchups`'
# height/id-sorted tiebreak (which had no position or height variance to
# actually sort on).
#
# B6 fix, round 2 (critic-round-7-proven): the 50/50 split above fixed the
# *touch_share-vs-no-role* branch but not the rank-tier branch *inside*
# the no-role half. A 10-player roster split 5/5 puts exactly 5 no-role
# players on the roster -- every one of them lands in the same rank tier by
# construction, so the flat model's rotation/bench-minutes constants were
# never read by this fixture at all. Critic proved this live: zeroing both
# to -999.0 left the full 133/133 suite, golden file included, green. Fixed
# by shrinking the role-carrying group to the minimum that still exercises
# the `touch_share_f` branch (1 player) and growing the no-role group to 9
# -- enough to span every rank tier the old flat 5/3/rest model had on a
# standard 10-player roster. The single role-carrying player also gets a
# distinct `overall_cached` (previously every role-carrying player shared
# the `make_sim_player` default of 60, unnoticed because that value was
# never read by anything with only one such player left, but a second use
# of this knob list -- or a future roster-size change -- could resurrect
# the same collision this project has hit four separate times per
# `EFFORT_LEDGER.md`).
#
# **RC-4/RC-16 update** (`docs/design/auto-gm-role-construction.md`,
# 2026-09-23): `game.py`'s flat tiers are gone, replaced by a 15-entry
# per-absolute-rank curve, so "spans all three rank tiers" no longer
# describes anything real -- every one of the 9 no-role players now reads a
# *distinct* curve anchor by construction, which is a strictly stronger
# coverage property than the old 5/3/1 split gave. What this knob list still
# has to guarantee, and does: the role-carrying player has the roster's
# highest `overall_cached` (90), so he is the full roster's absolute rank 1
# and the 9 no-role players are ranked 2-10 -- the exact shape
# `test_minutes_shares_ranks_full_roster_including_role_carrying_player`
# (`tests/unit/sim/test_game.py`) needs to prove RC-16's fix (rank within
# the *full* roster, not the no-role subset alone) is live.
_ROSTER_KNOB_POSITIONS: tuple[Position, ...] = (
    Position.PG,
    Position.SG,
    Position.SF,
    Position.PF,
    Position.C,
)

# 1 role-carrying player + the rest no-role, sized so a 10-player roster's
# no-role group (9) reads 9 distinct anchors off `game.py`'s per-rank
# curve. Every current caller uses the default 10-player roster
# (`make_game_context`'s `home_player_ids`/`away_player_ids` defaults) --
# grepped before assuming so, not eyeballed.
_ROLE_KNOB_COUNT = 1


def _distinct_roster_knobs(count: int) -> list[dict[str, object]]:
    """Splits `count` players into two disjoint groups: `_ROLE_KNOB_COUNT`
    get a real `PlayerRole` (`with_role=True`) with a distinct
    `touch_share` (and a distinct `overall_cached`, see the B6-round-2
    comment above) per player, exercising `_minutes_shares`'
    `touch_share_f` branch; the rest get none (`with_role=False`) with a
    distinct `overall_cached` per player, spanning distinct absolute ranks
    on `game.py`'s per-rank minutes curve on a standard 10-player roster,
    exercising its no-role fallback across a real range of ranks rather
    than one degenerate rank. Every player additionally gets a distinct
    `height_in` (74,
    76, 78, ..., 2 inches apart, never colliding for any `count` this
    project's fixtures use) and a `primary_position` that repeats every 5
    players -- 2 players per position for a 10-player roster -- so
    `assign_matchups` always falls through to its height/id tiebreak for
    every matchup slot (never the "exactly one player at this position"
    shortcut) and that tiebreak has real height variance to sort on
    instead of the previous all-`Position.SG`/all-78in degenerate case."""
    role_count = min(_ROLE_KNOB_COUNT, count)
    knobs: list[dict[str, object]] = []
    for i in range(count):
        knob: dict[str, object] = {
            "primary_position": _ROSTER_KNOB_POSITIONS[i % len(_ROSTER_KNOB_POSITIONS)],
            "height_in": 74 + i * 2,
        }
        if i < role_count:
            knob["with_role"] = True
            knob["touch_share"] = f"{0.10 + 0.04 * i:.2f}"
            knob["overall_cached"] = 90 - 2 * i
        else:
            knob["with_role"] = False
            # B9 fix (critic-round-8-proven): the prior `40 + 8 * (i -
            # role_count)` step produced 104 for the 9th no-role player on
            # a 10-player roster -- outside the `rating` domain's `CHECK
            # (VALUE BETWEEN 0 AND 99)` (`alembic/versions/001_league_
            # setup.py`, consumed by `002_players.py`'s `overall_cached
            # rating NOT NULL` column). A step of 6 off a floor of 40 caps
            # at 40 + 6*8 = 88 for the widest roster this fixture builds
            # (9 no-role players, `role_count` 1 on a 10-player roster),
            # staying well inside [0, 99] with room to spare, and 88 never
            # collides with the role-carrying player's own 90 (`role_count`
            # is always 1 today, so that's the only other value in play).
            # Still pairwise-distinct across every no-role player and still
            # spans a wide enough range for `_minutes_shares`' rank-tier
            # sort (B6) to see real separation between tiers.
            knob["overall_cached"] = 40 + 6 * (i - role_count)
        knobs.append(knob)
    return knobs


def make_sim_team(
    team_id: int,
    player_ids: tuple[int, ...],
    *,
    tuning: TuningProfile,
    league_id: int = 1,
    **strategy_overrides: object,
) -> SimTeam:
    team = make_team(team_id, league_id=league_id)
    strategy = make_team_strategy(team_id, league_id=league_id, **strategy_overrides)
    knobs = _distinct_roster_knobs(len(player_ids))
    players = tuple(
        make_sim_player(pid, tuning=tuning, league_id=league_id, team_id=team_id, **knobs[i])
        for i, pid in enumerate(player_ids)
    )
    return build_sim_team(team, strategy, players)


def make_game_context(
    game_id: int,
    *,
    tuning: TuningProfile,
    league_id: int = 1,
    season: int = 2026,
    home_team_id: int = 1,
    away_team_id: int = 2,
    home_player_ids: tuple[int, ...] = tuple(range(1, 11)),
    away_player_ids: tuple[int, ...] = tuple(range(101, 111)),
    home_strategy_overrides: dict[str, object] | None = None,
    away_strategy_overrides: dict[str, object] | None = None,
) -> GameContext:
    """A full `GameContext` for `game.py`/`possession.py` Tier-2 tests --
    step 4-5's own addition to this fixture file (§9: "extend
    tests/fixtures/sim.py following its existing distinct-value
    discipline" for anything the existing builders don't cover yet, which
    a whole-game context is). Reuses `make_sim_team` for both rosters, so
    every player still gets the file's pairwise-distinct rating/tendency
    treatment -- no separate, looser-valued fixture is introduced."""
    home = make_sim_team(
        home_team_id,
        home_player_ids,
        tuning=tuning,
        league_id=league_id,
        **(home_strategy_overrides or {}),
    )
    away = make_sim_team(
        away_team_id,
        away_player_ids,
        tuning=tuning,
        league_id=league_id,
        **(away_strategy_overrides or {}),
    )
    return GameContext(game_id=game_id, season=season, home=home, away=away, tuning=tuning)
