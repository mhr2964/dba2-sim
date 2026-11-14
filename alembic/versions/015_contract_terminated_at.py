"""contracts.terminated_at -- a real per-event timestamp for termination,
matching `created_at`'s already-real per-event timestamp for signing.

Revision ID: 015
Revises: 014
Create Date: 2026-09-17

**Found by an independent critic re-verification of `ff544f6`, not by
this migration's own author re-deriving it first.** `view_service.
_contract_terminated_item` stamped a `contract_terminated` transactions-
feed item with `contract.created_at` -- the contract's SIGNING timestamp
-- because migration 003 never gave `contracts` a termination timestamp
at all (only `signed_in_season`/`terminated_in_season`, both season-
grained). A contract signed in 2026 and terminated in 2028 was emitted
into the 2028 feed but stamped with 2026's timestamp, and the caller
then derived the item's PHASE from that same wrong timestamp too --
sorting a 2028 event into 2026's place in the merge. This is the same
"add a real column instead of approximating with a wrong value" pattern
this project's own transaction-log work (`0d75982`) already used for a
different gap; borrowing `created_at` a second time was never a live
option, since `created_at` already has a real, different meaning
(signing time) `terminated_at` cannot overload without reproducing the
exact bug it exists to fix.

Nullable, no default, no backfill: **a contract terminated before this
column existed has no real termination instant to record, and this
migration does not invent one.** `terminate_contract` (`contract_repo.
py`) is the one write path that ever sets `status` off `active` (§16 of
that module's own docstring, Phase 0 step-7 critic round B1's guard) and
is updated in the same commit as this migration to stamp `terminated_at
= NOW()` in the same `UPDATE` that stamps `terminated_in_season` --
so every contract terminated FROM THIS COMMIT FORWARD carries a real
value. `view_service.get_league_transactions_feed` skips (rather than
guesses at) a `contract_terminated` item for any contract whose
`terminated_at IS NULL`, disclosed in its own docstring and comment
rather than silently dropped -- the alternative (falling back to
`created_at` again) is the exact bug this migration exists to close, not
a lesser version of it.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE contracts ADD COLUMN terminated_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE contracts DROP COLUMN terminated_at")
