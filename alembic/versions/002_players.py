"""players, player_ratings, player_tendencies, player_ratings_history

Revision ID: 002
Revises: 001
Create Date: 2026-08-22

phase-0-foundation.md §4.3, §7.1.

Decision 10 splits identity (players) from ability (player_ratings) from
propensity (player_tendencies) — different write cadences (identity is
near-immutable; ratings/tendencies are rewritten wholesale at progression
time) and a bulk ratings load with no bio text in the payload (Phase 1's
sim batch). Decision 11 keeps ability and propensity in separate tables so
"how well he shoots threes" and "how often he shoots threes" can never be
conflated into one column set the way v1's `players` table was.

`player_ratings` and `player_tendencies` are one row per player (PK
`player_id`) rather than `id SERIAL` — a player has exactly one current
rating/tendency set, and the FK-as-PK makes that a schema fact instead of a
convention a caller has to remember to enforce with a UNIQUE constraint.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The rating-domain columns are identical across player_ratings and
# player_ratings_history (the history table is an append-only snapshot of
# player_ratings). Defined once so the two CREATE TABLE statements can't
# drift from each other by a typo.
_RATING_COLUMNS = """
    -- physical / athletic
    speed                 rating NOT NULL,
    acceleration          rating NOT NULL,
    lateral_quickness     rating NOT NULL,
    strength              rating NOT NULL,
    vertical              rating NOT NULL,
    stamina               rating NOT NULL,
    durability            rating NOT NULL,

    -- on-ball creation (beat 2b, offense)
    handle                rating NOT NULL,
    ball_security         rating NOT NULL,
    first_step            rating NOT NULL,
    change_of_direction   rating NOT NULL,
    screen_use            rating NOT NULL,
    draw_foul             rating NOT NULL,

    -- finishing (beats 3 and 5, offense)
    finishing_rim         rating NOT NULL,
    contact_finishing     rating NOT NULL,
    floater               rating NOT NULL,
    post_scoring          rating NOT NULL,

    -- shooting, split by catch vs. off-dribble (beat 5)
    catch_shoot_3         rating NOT NULL,
    off_dribble_3         rating NOT NULL,
    catch_shoot_mid       rating NOT NULL,
    off_dribble_mid       rating NOT NULL,
    free_throw            rating NOT NULL,

    -- playmaking (beats 3, 4)
    passing_vision        rating NOT NULL,
    passing_accuracy      rating NOT NULL,
    pass_under_pressure   rating NOT NULL,
    short_roll_passing    rating NOT NULL,

    -- screening (beat 2a, offense)
    screen_setting        rating NOT NULL,
    screen_timing         rating NOT NULL,
    roll_ability          rating NOT NULL,
    catch_hands           rating NOT NULL,

    -- off-ball offense (beat 4; spacing aggregate)
    off_ball_movement     rating NOT NULL,
    relocation            rating NOT NULL,
    cutting               rating NOT NULL,
    offensive_rebounding  rating NOT NULL,

    -- on-ball defense (beat 2b, defense)
    on_ball_defense       rating NOT NULL,
    hands                 rating NOT NULL,
    post_defense          rating NOT NULL,
    foul_discipline       rating NOT NULL,

    -- team / help defense (beats 2a, 3, 4, 5)
    screen_navigation     rating NOT NULL,
    help_iq               rating NOT NULL,
    rotation_speed        rating NOT NULL,
    rim_protection        rating NOT NULL,
    closeout_speed        rating NOT NULL,
    closeout_control      rating NOT NULL,
    scheme_discipline     rating NOT NULL,
    switchability         rating NOT NULL,
    defensive_rebounding  rating NOT NULL,

    -- mental
    basketball_iq         rating NOT NULL,
    composure             rating NOT NULL,
    consistency           rating NOT NULL,
    motor                 rating NOT NULL,
    coachability           rating NOT NULL,

    -- development (Pillar 7 / progression)
    potential              rating NOT NULL,
    potential_variance     rating NOT NULL,
    peak_age_start          SMALLINT NOT NULL,
    peak_age_end            SMALLINT NOT NULL,
    work_ethic              rating NOT NULL
"""


def upgrade() -> None:
    op.execute("""
        CREATE TABLE players (
            id                      SERIAL PRIMARY KEY,
            league_id               INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            first_name              TEXT NOT NULL,
            last_name               TEXT NOT NULL,
            external_ref            TEXT,
            primary_position        TEXT NOT NULL,
            secondary_position      TEXT,
            height_in               SMALLINT NOT NULL,
            weight_lb               SMALLINT NOT NULL,
            wingspan_in             SMALLINT NOT NULL,
            birth_date              DATE NOT NULL,
            draft_year              INT,
            draft_pick_number       INT,
            years_pro               SMALLINT NOT NULL DEFAULT 0,
            team_id                 INT REFERENCES teams(id) ON DELETE SET NULL,
            roster_status           TEXT NOT NULL,
            overall_cached          rating NOT NULL,
            overall_computed_at     TIMESTAMPTZ NOT NULL,
            retired_in_season       INT,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    # Single-column FK index — "get this league's roster" is the single most
    # common player query and is obviously justified without waiting on a
    # measured pattern. The three composite indexes named in §4.3.1
    # (league_id+team_id, league_id+roster_status, league_id+overall_cached)
    # are added in migration 007 alongside every other composite index.
    op.execute("CREATE INDEX ix_players_league_id ON players (league_id)")

    op.execute(f"""
        CREATE TABLE player_ratings (
            player_id               INT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
            league_id                INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            source                    TEXT NOT NULL,
            derived_from_season       INT,
            confidence                 NUMERIC(3,2) NOT NULL DEFAULT 1.00,
            updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            {_RATING_COLUMNS}
        )
    """)
    op.execute("CREATE INDEX ix_player_ratings_league_id ON player_ratings (league_id)")

    # shot_freq_* four-way CHECK is the direct schema encoding of "these four sum to
    # 100" from §4.3.3 — location-mix tendencies can't silently drift past 100%.
    op.execute("""
        CREATE TABLE player_tendencies (
            player_id           INT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
            league_id            INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            shot_freq_rim        tendency NOT NULL,
            shot_freq_mid        tendency NOT NULL,
            shot_freq_three      tendency NOT NULL,
            shot_freq_post       tendency NOT NULL,
            CHECK (shot_freq_rim + shot_freq_mid + shot_freq_three + shot_freq_post = 100),

            drive_freq           tendency NOT NULL,
            pullup_vs_catch      tendency NOT NULL,
            iso_freq             tendency NOT NULL,
            pnr_ball_freq        tendency NOT NULL,
            pnr_screen_freq      tendency NOT NULL,
            transition_freq      tendency NOT NULL,
            pass_freq            tendency NOT NULL,

            spot_up_freq         tendency NOT NULL,
            cut_freq             tendency NOT NULL,
            crash_oreb_freq      tendency NOT NULL,

            help_gamble_freq     tendency NOT NULL,
            steal_gamble_freq    tendency NOT NULL,
            foul_freq            tendency NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_player_tendencies_league_id ON player_tendencies (league_id)")

    # Append-only snapshot (§4.3.4, Decision 16) — the schema ships now even though
    # nothing writes to it until the progression system exists (Phase 2+), because
    # retrofitting history after a season has been played means that season's
    # befores are already gone. league_id is not explicitly listed in §4.3.4's column
    # description but is added here for consistency with every other per-player table
    # and with the general "every league-scoped table carries league_id" convention.
    op.execute(f"""
        CREATE TABLE player_ratings_history (
            player_id     INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            season        INT NOT NULL,
            league_id     INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            reason        TEXT NOT NULL,
            recorded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            {_RATING_COLUMNS},
            PRIMARY KEY (player_id, season)
        )
    """)
    op.execute(
        "CREATE INDEX ix_player_ratings_history_league_id "
        "ON player_ratings_history (league_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE player_ratings_history")
    op.execute("DROP TABLE player_tendencies")
    op.execute("DROP TABLE player_ratings")
    op.execute("DROP TABLE players")
