"""player_roles, player_directives, player_motivation, player_motivation_events, player_awards

Revision ID: 005
Revises: 004
Create Date: 2026-08-22

phase-0-foundation.md §4.4 (player_awards), §4.6, §7.1.

`player_awards` is placed in this migration per §7.1's explicit migration
split, even though §4.4/Decision 20 discuss it alongside contracts (it's the
supermax-eligibility input) — the migration boundary follows the repo-module
boundary (`award_repo`), not the section it's discussed in.

`player_motivation` satisfaction_* columns get an explicit -100..100 CHECK
because §4.6 states that range for those four axes specifically; `morale`
and `trust_in_management` are left unconstrained since the doc doesn't give
them a documented range and this migration would rather leave a gap open
than guess wrong on a constraint that's expensive to loosen later.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Ported from v1's migration 040 unchanged (§4.6).
    op.execute("""
        CREATE TABLE player_roles (
            id            SERIAL PRIMARY KEY,
            league_id     INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id       INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            season        INT NOT NULL,
            player_id     INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            role          TEXT NOT NULL,
            touch_share   NUMERIC(5,4) NOT NULL,
            rationale     TEXT,
            assigned_by   BIGINT,
            locked        BOOLEAN NOT NULL DEFAULT FALSE,
            assigned_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (league_id, team_id, season, player_id)
        )
    """)

    # Ported from v1's migration 018 unchanged (§4.6).
    op.execute("""
        CREATE TABLE player_directives (
            id             SERIAL PRIMARY KEY,
            league_id      INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id      INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            shot_diet      TEXT,
            usage_mode     TEXT,
            defense_mode   TEXT,
            role_mode      TEXT,
            clutch_mode    TEXT,
            set_by         BIGINT,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (league_id, player_id)
        )
    """)

    op.execute("""
        CREATE TABLE player_motivation (
            player_id                   INT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
            league_id                   INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,

            weight_winning              tendency NOT NULL,
            weight_role_usage           tendency NOT NULL,
            weight_loyalty              tendency NOT NULL,
            weight_money                tendency NOT NULL,
            weight_legacy               tendency NOT NULL,
            hometown_team_id            INT REFERENCES teams(id) ON DELETE SET NULL,
            market_preference           TEXT NOT NULL DEFAULT 'neutral',
            star_leverage               tendency NOT NULL,

            satisfaction_winning        SMALLINT NOT NULL,
            satisfaction_role_usage     SMALLINT NOT NULL,
            satisfaction_loyalty        SMALLINT NOT NULL,
            satisfaction_money          SMALLINT NOT NULL,
            satisfaction_legacy         SMALLINT NOT NULL,
            morale                      SMALLINT NOT NULL,
            trust_in_management         SMALLINT NOT NULL,
            request_state               TEXT NOT NULL DEFAULT 'content',
            request_opened_in_season    INT,
            last_evaluated_season       INT NOT NULL,

            CHECK (satisfaction_winning BETWEEN -100 AND 100),
            CHECK (satisfaction_role_usage BETWEEN -100 AND 100),
            CHECK (satisfaction_loyalty BETWEEN -100 AND 100),
            CHECK (satisfaction_money BETWEEN -100 AND 100),
            CHECK (satisfaction_legacy BETWEEN -100 AND 100)
        )
    """)
    op.execute("CREATE INDEX ix_player_motivation_league_id ON player_motivation (league_id)")

    op.execute("""
        CREATE TABLE player_motivation_events (
            id           SERIAL PRIMARY KEY,
            league_id    INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id    INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            season       INT NOT NULL,
            phase        TEXT NOT NULL,
            event_type   TEXT NOT NULL,
            axis         TEXT NOT NULL,
            delta        SMALLINT NOT NULL,
            note         TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX ix_player_motivation_events_player_id "
        "ON player_motivation_events (player_id)"
    )

    # UNIQUE (league_id, player_id, season, award_type) prevents a duplicate award
    # row for the same honor in the same season — a real integrity concern once
    # award voting writes here, not a speculative addition.
    op.execute("""
        CREATE TABLE player_awards (
            id            SERIAL PRIMARY KEY,
            league_id     INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id     INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            season        INT NOT NULL,
            award_type    TEXT NOT NULL,
            place         SMALLINT,
            vote_share    NUMERIC(5,4),
            UNIQUE (league_id, player_id, season, award_type)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE player_awards")
    op.execute("DROP TABLE player_motivation_events")
    op.execute("DROP TABLE player_motivation")
    op.execute("DROP TABLE player_directives")
    op.execute("DROP TABLE player_roles")
