"""draft_picks, drafts, draft_selections, draft_classes

Revision ID: 004
Revises: 003
Create Date: 2026-08-22

phase-0-foundation.md §4.5, §6.3, §7.1.

Decision 21: protection columns (`protection_type`, `protection_through`,
`swap_with_pick_id`) exist now even though conveyance logic is Phase 2/3 —
adding them later would mean a migration plus a backfill of picks already
traded in a live league.

§6.3: `drafts` deliberately has no `status` column. v1 ran a second,
informal state machine there (`pending_lottery` -> `lottery_done` -> ... ->
`complete`) compared as bare string literals with no transition table.
Every one of those values already has a one-to-one counterpart in the
single league phase graph (Phase 0 step 6, not built in this migration
batch), so `drafts` keeps only the pointer fields (`current_round`,
`current_pick_number`) — completion is derived, not stored.

`draft_picks.used_for_player_id` intentionally has no inline FK — see
migration 007, which adds it along with every composite index, to keep this
migration's ownership boundary (draft_repo's own tables) from reaching
directly into player_repo's table at creation time.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE draft_picks (
            id                     SERIAL PRIMARY KEY,
            league_id              INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season                 INT NOT NULL,
            round                  SMALLINT NOT NULL,
            original_team_id       INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            current_team_id        INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            pick_number            SMALLINT,
            used_for_player_id     INT,
            protection_type        TEXT NOT NULL DEFAULT 'none',
            protection_through     SMALLINT,
            swap_with_pick_id      INT REFERENCES draft_picks(id) ON DELETE SET NULL,
            conveyed               BOOLEAN NOT NULL DEFAULT FALSE,
            rolls_over_to_season   INT
        )
    """)

    op.execute("""
        CREATE TABLE drafts (
            id                     SERIAL PRIMARY KEY,
            league_id              INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season                 INT NOT NULL,
            current_round          SMALLINT NOT NULL DEFAULT 1,
            current_pick_number    SMALLINT NOT NULL DEFAULT 1,
            lottery_completed_at   TIMESTAMPTZ,
            total_picks            SMALLINT NOT NULL,
            UNIQUE (league_id, season)
        )
    """)

    # pick_asset_id is RESTRICT, not CASCADE — a selection is the historical record
    # of "team X took player Y with pick N"; the pick-asset row it points at should
    # not be deletable out from under a completed selection.
    op.execute("""
        CREATE TABLE draft_selections (
            id             SERIAL PRIMARY KEY,
            draft_id       INT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
            pick_number    SMALLINT NOT NULL,
            round          SMALLINT NOT NULL,
            team_id        INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            player_id      INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            pick_asset_id  INT NOT NULL REFERENCES draft_picks(id) ON DELETE RESTRICT,
            selected_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (draft_id, pick_number)
        )
    """)

    op.execute("""
        CREATE TABLE draft_classes (
            id             SERIAL PRIMARY KEY,
            league_id      INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            class_year     INT NOT NULL,
            player_id      INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            mock_rank      SMALLINT,
            is_generated   BOOLEAN NOT NULL DEFAULT TRUE,
            source         TEXT
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE draft_classes")
    op.execute("DROP TABLE draft_selections")
    op.execute("DROP TABLE drafts")
    op.execute("DROP TABLE draft_picks")
