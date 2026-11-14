"""league_transaction_assets -- restore the half of P5-31's original
cross-column CHECK that migration 013 removed unnecessarily.

Revision ID: 014
Revises: 013
Create Date: 2026-09-17

**Found by an independent critic re-verification of `1abe7b0`/`0d75982`,
not by this migration's own author re-deriving it first.** Migration
013's docstring treats P5-31's two-column CHECK correspondence
(`CHECK ((asset_kind = 'player') = (player_id IS NOT NULL))` and the pick
equivalent) as one indivisible thing and removes both halves because one
half conflicts with `ON DELETE SET NULL`. **It is not one thing.** The
CHECK is really two logically separate guarantees glued into one
expression by the `=` operator:

1. **Presence-correspondence** -- `asset_kind = 'player' IMPLIES
   player_id IS NOT NULL` (and the pick equivalent). This is the half
   that genuinely conflicts with `ON DELETE SET NULL`: a purged player's
   own cascade nulls `player_id` while `asset_kind` stays `'player'`,
   which this direction of the CHECK then rejects on the FK's own
   UPDATE. Migration 013's removal of this half was correct and stays
   removed here -- it is enforced in Python instead, at
   `txn_repo.add_transaction_asset`'s own `ValueError` guard, unchanged.

2. **Wrong-column exclusion** -- `asset_kind = 'player' IMPLIES
   draft_pick_id IS NULL` (and the pick equivalent: `asset_kind = 'pick'
   IMPLIES player_id IS NULL`). `ON DELETE SET NULL` can only ever turn a
   non-NULL id into NULL; it can never populate the OTHER id column, so
   this half is never touched by any cascade a purge can trigger and
   never enters the conflict migration 013's docstring describes.
   Migration 013 removed this half anyway, along with the first, without
   separately checking whether it needed to -- surrendering a real
   DB-level guarantee P5-31's own design text (`docs/design/phase-5-
   discord-presentation.md:675`) specifically defends ("a `'player'` row
   with a `draft_pick_id` and no `player_id` is rejected by the database
   rather than by caller discipline"), for a conflict this half was never
   part of.

This migration adds back exactly the wrong-column-exclusion half, as one
combined `CHECK`, and changes nothing else. `asset_kind`'s existing plain
`CHECK (asset_kind IN ('player', 'pick'))` (migration 013) is untouched.
The presence-correspondence half stays Python-enforced, for the same
reason migration 013 gave -- no attempt is made to weaken this
migration's constraint to also cover it, since doing that would
reintroduce the exact conflict migration 013 already reproduced and
this migration exists to NOT repeat.

**Verified directly against Postgres, not assumed correct from the
argument above**, mirroring migration 013's own "found by this
migration's own integration test, not assumed correct from the design
doc's prose" discipline: a real player and a real draft pick were each
created, attached to a `league_transaction_assets` row, and deleted.
Both cascades complete with the referencing column nulled and the
restored CHECK never fires -- see `tests/integration/test_txn_repo.py::
test_asset_row_survives_its_players_own_purge_with_player_id_nulled` and
its new pick-side twin,
`test_asset_row_survives_its_picks_own_purge_with_draft_pick_id_nulled`.
A third new test,
`test_wrong_column_check_rejects_a_player_row_carrying_a_draft_pick_id`,
inserts directly against the table (bypassing `txn_repo`'s own Python
guard on purpose) and confirms the restored CHECK is what rejects it --
proving this migration's constraint is load-bearing and not merely
redundant with the repo-layer guard.

docs/design/phase-5-discord-presentation.md's own record of this
deviation is at Decision P5-93 (round 8), which AMENDS P5-31 and P5-35
-- P5-35's `trade_offer_assets` spec (not yet built) is corrected in the
same round so its own builder does not walk into the conflict migration
013 already found and this migration only half-reverses.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "ck_league_transaction_assets_kind_excludes_wrong_column"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE league_transaction_assets
            ADD CONSTRAINT {_CONSTRAINT_NAME}
            CHECK (
                NOT (asset_kind = 'player' AND draft_pick_id IS NOT NULL)
                AND NOT (asset_kind = 'pick' AND player_id IS NOT NULL)
            )
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE league_transaction_assets DROP CONSTRAINT {_CONSTRAINT_NAME}")
