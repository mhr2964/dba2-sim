"""league setup — domains, leagues, teams, strategy, phase history

Revision ID: 001
Revises:
Create Date: 2026-08-22

phase-0-foundation.md §4.1-4.2, §7.1.

`rating` (Decision 6) and `tendency` are Postgres domains rather than ~60
individual CHECK constraints on the tables that use them — one declaration,
uniformly enforced, and the scale is documented in the schema itself.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE DOMAIN rating AS SMALLINT CHECK (VALUE BETWEEN 0 AND 99)")
    op.execute("CREATE DOMAIN tendency AS SMALLINT CHECK (VALUE BETWEEN 0 AND 100)")

    op.execute("""
        CREATE TABLE leagues (
            id                     SERIAL PRIMARY KEY,
            discord_guild_id       BIGINT UNIQUE NOT NULL,
            name                   TEXT NOT NULL,
            start_season_year      INT NOT NULL,
            current_season         INT NOT NULL,
            current_phase          TEXT NOT NULL DEFAULT 'SETUP',
            phase_entered_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            phase_data             JSONB NOT NULL DEFAULT '{}',
            commissioner_user_id   BIGINT NOT NULL,
            rng_seed               BIGINT NOT NULL,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            archived_at            TIMESTAMPTZ
        )
    """)

    op.execute("""
        CREATE TABLE league_cap_settings (
            league_id              INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season                 INT NOT NULL,
            salary_cap             BIGINT NOT NULL,
            luxury_tax             BIGINT NOT NULL,
            apron_1                BIGINT NOT NULL,
            apron_2                BIGINT NOT NULL,
            max_pct_0_6            SMALLINT NOT NULL,
            max_pct_7_9            SMALLINT NOT NULL,
            max_pct_10_plus        SMALLINT NOT NULL,
            supermax_pct           SMALLINT NOT NULL,
            min_salary             BIGINT NOT NULL,
            mle_amount             BIGINT NOT NULL,
            bi_annual_amount       BIGINT NOT NULL,
            raise_pct_bird         SMALLINT NOT NULL,
            raise_pct_non_bird     SMALLINT NOT NULL,
            roster_min              SMALLINT NOT NULL,
            roster_max               SMALLINT NOT NULL,
            PRIMARY KEY (league_id, season)
        )
    """)

    # team.code, gm_philosophy, timeline, home_market_size per §4.2. team_offense_rating,
    # team_defense_rating, and pace are deliberately absent (Decision 9) — cached
    # aggregates nobody uniquely owns go stale silently; strength is computed from the
    # roster on read and pace moves to team_strategy as a strategy choice, not a rating.
    op.execute("""
        CREATE TABLE teams (
            id                  SERIAL PRIMARY KEY,
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            code                TEXT NOT NULL,
            name                TEXT NOT NULL,
            city                TEXT NOT NULL,
            conference          TEXT NOT NULL,
            division            TEXT NOT NULL,
            manager_user_id     BIGINT,
            gm_philosophy       TEXT NOT NULL DEFAULT 'tendency_respecter',
            timeline            TEXT NOT NULL DEFAULT 'retool',
            home_market_size    SMALLINT NOT NULL,
            UNIQUE (league_id, code)
        )
    """)
    op.execute("CREATE INDEX ix_teams_league_id ON teams (league_id)")

    # pace_target / help_aggression / three_point_bias / crash_oreb_bias are all
    # documented as 0-100 in §4.2 — the same scale player_tendencies uses the
    # `tendency` domain for. They must use the same domain rather than bare
    # SMALLINT, or Phase 1's possession-engine setup-beat inputs (§4.2's own
    # framing) can silently receive an out-of-range value with no CHECK to
    # catch it.
    op.execute("""
        CREATE TABLE team_strategy (
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id             INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            season              INT NOT NULL,
            pace_target         tendency NOT NULL,
            offensive_scheme    TEXT NOT NULL,
            defensive_scheme    TEXT NOT NULL,
            help_aggression     tendency NOT NULL,
            three_point_bias    tendency NOT NULL,
            crash_oreb_bias     tendency NOT NULL,
            set_by              BIGINT,
            PRIMARY KEY (league_id, team_id, season)
        )
    """)

    # Ported from v1 unchanged (§4.2) — no league_id; reachable via team_id -> teams.league_id.
    op.execute("""
        CREATE TABLE team_manager_history (
            id            SERIAL PRIMARY KEY,
            team_id       INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            user_id       BIGINT,
            assigned_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            removed_at    TIMESTAMPTZ,
            assigned_by   BIGINT NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_team_manager_history_team_id ON team_manager_history (team_id)")

    # actor_user_id is NOT NULL: §6.3 — every transition is triggered by a commissioner
    # action (or league creation itself), so every phase_history row ties to an actor.
    op.execute("""
        CREATE TABLE phase_history (
            id              SERIAL PRIMARY KEY,
            league_id       INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season          INT NOT NULL,
            from_phase      TEXT,
            to_phase        TEXT NOT NULL,
            occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            actor_user_id   BIGINT NOT NULL,
            forced          BOOLEAN NOT NULL DEFAULT FALSE,
            reason          TEXT
        )
    """)
    op.execute("CREATE INDEX ix_phase_history_league_id ON phase_history (league_id)")


def downgrade() -> None:
    op.execute("DROP TABLE phase_history")
    op.execute("DROP TABLE team_manager_history")
    op.execute("DROP TABLE team_strategy")
    op.execute("DROP TABLE teams")
    op.execute("DROP TABLE league_cap_settings")
    op.execute("DROP TABLE leagues")
    op.execute("DROP DOMAIN tendency")
    op.execute("DROP DOMAIN rating")
