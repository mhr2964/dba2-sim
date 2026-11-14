"""player_ratings, player_tendencies, player_ratings_history —
`ratings_repo`'s tables (§5).

phase-0-foundation.md §4.3.2-4.3.4. `player_ratings` and
`player_ratings_history` share the exact same 57-column rating block (the
history table is an append-only snapshot of the live one, §4.3.4) — that
block is factored into `_RatingColumns`, a base dataclass neither table's
repo mapper constructs directly, so the 57 fields are declared exactly
once instead of twice-and-hoping-they-stay-in-sync (the same reasoning
migration 002 gives for factoring `_RATING_COLUMNS` into one SQL fragment
shared by both `CREATE TABLE` statements). Each table still gets its own
concrete dataclass (`PlayerRatings`, `PlayerRatingsHistory`), satisfying
"one dataclass per table" — they just don't redeclare the shared columns.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class RatingSource(StrEnum):
    REAL_STAT_DERIVED = "real_stat_derived"
    GENERATED = "generated"
    MANUAL = "manual"
    PROGRESSION = "progression"


class RatingHistoryReason(StrEnum):
    PROGRESSION = "progression"
    INJURY_ADJUSTMENT = "injury_adjustment"
    MANUAL = "manual"


@dataclass
class _RatingColumns:
    """The 57 columns from §4.3.2's beat-grouped table: 55 on the `rating`
    Postgres domain (0-99) + 2 plain-SMALLINT peak-age exceptions
    (`peak_age_start`/`peak_age_end` — an age, not a 0-99 rating). Field
    order matches migration 002's `_RATING_COLUMNS` SQL fragment exactly,
    for grep-ability against the schema.
    """

    # physical / athletic
    speed: int
    acceleration: int
    lateral_quickness: int
    strength: int
    vertical: int
    stamina: int
    durability: int

    # on-ball creation (beat 2b, offense)
    handle: int
    ball_security: int
    first_step: int
    change_of_direction: int
    screen_use: int
    draw_foul: int

    # finishing (beats 3 and 5, offense)
    finishing_rim: int
    contact_finishing: int
    floater: int
    post_scoring: int

    # shooting, split by catch vs. off-dribble (beat 5)
    catch_shoot_3: int
    off_dribble_3: int
    catch_shoot_mid: int
    off_dribble_mid: int
    free_throw: int

    # playmaking (beats 3, 4)
    passing_vision: int
    passing_accuracy: int
    pass_under_pressure: int
    short_roll_passing: int

    # screening (beat 2a, offense)
    screen_setting: int
    screen_timing: int
    roll_ability: int
    catch_hands: int

    # off-ball offense (beat 4; spacing aggregate)
    off_ball_movement: int
    relocation: int
    cutting: int
    offensive_rebounding: int

    # on-ball defense (beat 2b, defense)
    on_ball_defense: int
    hands: int
    post_defense: int
    foul_discipline: int

    # team / help defense (beats 2a, 3, 4, 5)
    screen_navigation: int
    help_iq: int
    rotation_speed: int
    rim_protection: int
    closeout_speed: int
    closeout_control: int
    scheme_discipline: int
    switchability: int
    defensive_rebounding: int

    # mental
    basketball_iq: int
    composure: int
    consistency: int
    motor: int
    coachability: int

    # development (Pillar 7 / progression)
    potential: int
    potential_variance: int
    peak_age_start: int
    peak_age_end: int
    work_ethic: int


@dataclass
class PlayerRatings(_RatingColumns):
    """Decision 17: `confidence` carries sample-size weakness through the
    schema so the trade evaluator and Phase 1's validation harness don't
    each have to guess it from the generator."""

    player_id: int
    league_id: int
    source: RatingSource
    derived_from_season: int | None
    confidence: Decimal
    updated_at: datetime


@dataclass
class PlayerRatingsHistory(_RatingColumns):
    """Append-only snapshot, PK (player_id, season) — Decision 16."""

    player_id: int
    season: int
    league_id: int
    reason: RatingHistoryReason
    recorded_at: datetime


@dataclass
class PlayerTendencies:
    """One row per player — propensity, kept apart from ability
    (Decision 11). `pullup_vs_catch` decides which shooting rating the
    resolution beat reads (0 = pure catch-and-shoot, 100 = pure
    off-dribble)."""

    player_id: int
    league_id: int
    updated_at: datetime

    shot_freq_rim: int
    shot_freq_mid: int
    shot_freq_three: int
    shot_freq_post: int

    drive_freq: int
    pullup_vs_catch: int
    iso_freq: int
    pnr_ball_freq: int
    pnr_screen_freq: int
    transition_freq: int
    pass_freq: int

    spot_up_freq: int
    cut_freq: int
    crash_oreb_freq: int

    help_gamble_freq: int
    steal_gamble_freq: int
    foul_freq: int
