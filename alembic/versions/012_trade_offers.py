"""trade_offers -- a minimal slice of docs/design/phase-5-discord-
presentation.md's own migration-013 shape (Decision P5-35), scoped down
to exactly what A-23's `run_phase_hook` skip (Decisions P5-44 through
P5-91) needs to satisfy its own proof obligation: a real, persisted row
for an AI-initiated offer to a human-managed team, never an auto-resolved
trade.

Revision ID: 012
Revises: 011
Create Date: 2026-09-17

See `dba2/domain/trade_offer.py` for the full reasoning on what is
deliberately absent here (`counters_offer_id`, `channel_id`/`message_id`,
`illegal_token`, `resolved_at`, a separate `trade_offer_assets` table) and
why -- every omission is a real, additive migration for whichever future
commit builds that column's own writer, not a foreclosed shape.

**Why 012, not 013.** The design doc's own text reserves "migration 013"
for a much larger bundle (P5-31's transaction log, P5-66's `timeline_
source` override, P5-78/P5-90's `advance_claimed_at`/`advance_claimed_by`
claim columns, P5-71's `cycle_index` columns) that has not shipped --
migration 012 (the channel registry, P5-12) has not shipped either, so
`012` is this repository's real next revision number, not the design
doc's own reserved label. Renumbering later migrations if `012` is ever
built for the channel registry instead is a problem for whichever commit
adds it, same as every other migration file in this project.

`package` is `JSONB`, no custom type codec registered on the pool
(`dba2/data/db.py` never calls `conn.set_type_codec` for json/jsonb) --
marshalled to/from `dict` at the repo boundary via `json.dumps`/
`json.loads`, the same disclosed convention `media_repo.py`'s `slot_
bindings` and `league_repo.py`'s `phase_data` columns already use.
`status` is `TEXT`, matching this schema's blanket "status columns store
a StrEnum value as TEXT, no DB CHECK" convention (`league_repo.py`'s own
docstring for `current_phase`, `media_repo.py`'s for `story_kind`/
`phase`) -- a corrupt stored value raises a bare `ValueError` from
`TradeStatus(...)` construction, uncaught, matching every other
repository module in this package.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE trade_offers (
            id                SERIAL PRIMARY KEY,
            league_id         INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season            INT NOT NULL,
            phase             TEXT NOT NULL,
            from_team_id      INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            to_team_id        INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            proposed_by       BIGINT,
            status            TEXT NOT NULL,
            package           JSONB NOT NULL,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX ix_trade_offers_league_status ON trade_offers (league_id, status)"
    )
    op.execute(
        "CREATE INDEX ix_trade_offers_to_team_status ON trade_offers (to_team_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE trade_offers")
