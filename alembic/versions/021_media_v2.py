"""media_v2 step 1 -- the two additive columns and one index the media-v2
newsroom retype needs before any detector or spine code can be written.

Revision ID: 021
Revises: 020
Create Date: 2026-09-22

docs/design/media-v2-newsroom.md MV-2b, MV-2a, "Build order" step 1.

**Scope, deliberately narrow.** The design doc's own MV-9 table also lists
four new `media_stories` columns (`headline`, `format_tier`, `fact_package`,
`writer_backend`) under "migration 021" -- those back the detector/writer
work in later build steps (MV-2/MV-6/MV-7, steps 4-7) and are NOT added
here. This migration carries only what steps 1-3a of the build order need:

- `player_motivation_events.from_state` / `.to_state` (`TEXT NULL`, no
  default, no `CHECK` -- matching `event_type`'s own open-vocabulary-in-
  Python convention). MV-2b: the state-transition ladder move is today only
  recorded as a free-text `note = f"{old.value}->{new.value}"`
  (`agency_service.py`), which the media event-intake layer's own docstring
  already refuses to parse ("parsing that string here would be fragile
  string-matching against an internal debug field with no contract behind
  its format"). Two typed, nullable columns let a future reader recover
  the transition without parsing debug text. Rows written before this
  migration read `NULL`/`NULL` on both columns -- MV-2b's own rule: that
  reads as "no reading," never as a fabricated `CONTENT`.
- `CREATE INDEX ix_league_transaction_assets_player` -- MV-2a: the trade
  scan's own `txn_repo.list_trades_involving_player` (the revenge-game and
  broken-promise spine, step 2) joins `league_transaction_assets` on
  `player_id`; that column carries only the table's default
  `transaction_id` index today, so a player lookup is a sequential scan.
  Partial (`WHERE player_id IS NOT NULL`) because a `PICK` asset row's
  `player_id` is always `NULL` (migration 013/014's own kind-exclusion
  CHECK) and indexing that half would cost writes for no read this design
  ever issues.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "021"
down_revision: str | None = "020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE player_motivation_events ADD COLUMN from_state TEXT")
    op.execute("ALTER TABLE player_motivation_events ADD COLUMN to_state TEXT")
    op.execute(
        "CREATE INDEX ix_league_transaction_assets_player "
        "ON league_transaction_assets (player_id) WHERE player_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_league_transaction_assets_player")
    op.execute("ALTER TABLE player_motivation_events DROP COLUMN to_state")
    op.execute("ALTER TABLE player_motivation_events DROP COLUMN from_state")
