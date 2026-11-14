"""league_channels, team_gm_channels -- the channel registry.

Revision ID: 016
Revises: 015
Create Date: 2026-09-17

docs/design/phase-5-discord-presentation.md §6.1 (Decision P5-12), §6.2
(Decision P5-13/P5-14/P5-41) -- Stage C (§11 step 10, "Migration 012
(P5-12) ... claim the real next revision number when this ships, not
literally 012" per that decision's own round-7 numbering note; `012` was
already claimed by `trade_offers`, `013`/`014`/`015` by later work, so
this is that migration, shipping as `016`).

See `dba2/domain/channel.py` for the scope note: only `wire`/`standings`
of P5-12's originally-proposed four league-wide purposes ship here, and
`league_channels.purpose` is left `TEXT` (not a DB `CHECK`) with the
closed set enforced in Python at `ChannelPurpose`, matching `league_
transactions.txn_type`'s own "open representation, Python-enforced
vocabulary" column shape even though this particular vocabulary is
closed -- so widening it later (the other two purposes) is a Python
change, not a migration, and no CHECK has to be dropped and re-added to
do it.

`team_gm_channels` has no uniqueness constraint enforcing "at most one
active (archived_at IS NULL) row per team" -- see `domain/channel.py`'s
own docstring for why that is enforced in `bot/channels.py`'s reconcile
instead of a DB partial-unique-index, mirroring `team_manager_history`'s
existing `close_manager_assignment` precedent.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "016"
down_revision: str | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE league_channels (
            id            SERIAL PRIMARY KEY,
            league_id     INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            purpose       TEXT NOT NULL,
            channel_id    BIGINT NOT NULL,
            message_id    BIGINT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (league_id, purpose)
        )
    """)

    op.execute("""
        CREATE TABLE team_gm_channels (
            id            SERIAL PRIMARY KEY,
            league_id     INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id       INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            channel_id    BIGINT NOT NULL,
            archived_at   TIMESTAMPTZ,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX ix_team_gm_channels_team_id ON team_gm_channels (team_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_team_gm_channels_team_id")
    op.execute("DROP TABLE team_gm_channels")
    op.execute("DROP TABLE league_channels")
