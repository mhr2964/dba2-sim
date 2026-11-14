"""trade_offers -- the six columns the trade-offer review surface writes, plus
the index `/offers` and `expire_offers_for_team` read through.

Revision ID: 018
Revises: 017
Create Date: 2026-09-19

docs/design/phase-5-trade-offer-review-surface.md §2. Migration 012 shipped
`trade_offers` with ten columns and named five more it deliberately left out
"for whichever future commit builds that column's own writer"
(`012_trade_offers.py`'s docstring). This is that commit: every column below
has a real writer in the same slice (`services/offer_service.py`,
`data/repositories/offer_repo.py`), which is the only reason it is allowed to
exist -- a `NOT NULL`/no-writer column is the shape this project has now
declined three times.

- `counters_offer_id` -- P5-36's "a counter is a new row, never a mutated
  one". `ON DELETE SET NULL` (not CASCADE): deleting the original of a chain
  must not silently delete its counter.
- `resolved_at`, `resolved_by` -- every status CAS, plus the two bulk writers
  (`expire_offers_for_team`, `expire_stale_offers_for_league`) which resolve
  rows without a CAS. `resolved_by IS NULL` means "resolved by the AI or by a
  rule (expiry)"; nobody clicked. `resolved_by` is the one column not in
  P5-35's original schema: a rejected offer's actor is recorded nowhere else
  (an accepted one is, in `league_transactions.actor_user_id`).
- `illegal_token` -- the `"illegal:<rule>"` string an accept-time legality
  re-check failed on.
- `channel_id`, `message_id` -- the delivery drain's write-back;
  `status = 'proposed' AND message_id IS NULL` *is* the undelivered queue.

Deliberately still absent: `cycle_index` (no correct source exists yet,
013's own docstring), `trade_offer_assets`, and any TTL column (expiry is
keyed on the row's own `(season, phase)`).

All columns nullable, no backfill: rows written before this migration have no
resolution instant, actor or message to record, and inventing one would be
the "approximate with a wrong value" shape migration 015 exists to avoid.

`ix_trade_offers_from_team_status` -- `/offers` shows sent offers too, and
`expire_offers_for_team` already filters on `from_team_id` with no index
behind it.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "018"
down_revision: str | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE trade_offers
            ADD COLUMN counters_offer_id INT REFERENCES trade_offers(id) ON DELETE SET NULL,
            ADD COLUMN resolved_at       TIMESTAMPTZ,
            ADD COLUMN resolved_by       BIGINT,
            ADD COLUMN illegal_token     TEXT,
            ADD COLUMN channel_id        BIGINT,
            ADD COLUMN message_id        BIGINT
    """)
    op.execute(
        "CREATE INDEX ix_trade_offers_from_team_status ON trade_offers (from_team_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_trade_offers_from_team_status")
    op.execute("""
        ALTER TABLE trade_offers
            DROP COLUMN message_id,
            DROP COLUMN channel_id,
            DROP COLUMN illegal_token,
            DROP COLUMN resolved_by,
            DROP COLUMN resolved_at,
            DROP COLUMN counters_offer_id
    """)
