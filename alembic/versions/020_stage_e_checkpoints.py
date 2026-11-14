"""stage_e_checkpoints -- the durable resumption points Stage E's `/advance` press
needs so a crash mid-press converges on the next press instead of stranding or
double-applying work.

Revision ID: 020
Revises: 019
Create Date: 2026-09-20

docs/design/phase-5-stage-e-advance.md P5-99.r.1, P5-99.r.2, P5-99.s.1, build-plan
commit 9 (one migration, three tables).

- `leagues.entry_work_completed_index` / `leagues.closing_work_completed_index`
  (`INT NOT NULL DEFAULT -1`, the shape of migration 013's
  `hook_completed_cycle_index`) -- the two completion checkpoints of a phase entry's
  work lists. **Keyed by `entry_index`, not `cycle_ordinal`**: phase work exists at
  `PROGRESSION` and `REGULAR_SEASON_COMPLETE`, which are not cycle-bearing and have
  no ordinal. They advance only when their phase has work of that kind (three and
  ten times a season), monotone by predicate at the writer. `-1` means "no entry's
  work of this kind has completed yet".
  **Season scoping is Phase 7's:** both restart at 0 every season while the writer's
  `column < $2` is monotone forever, so the season-rollover writer must reset them
  (and `hook_completed_cycle_index`) to `-1` in the transaction that increments
  `leagues.current_season`. Phase 5 never rolls over, so nothing is broken today.
- `player_motivation.last_checkpoint_phase` (`TEXT NULL`, no `CHECK` -- the closed
  set is `domain.phase.Phase`, enforced in Python like `player_motivation_events.
  phase`) -- the phase of the last checkpoint that evaluated this player. It is the
  per-player guard that makes a resumed `run_agency_checkpoint` skip the players it
  already folded rather than fold them twice. `NULL` for a player no checkpoint has
  ever reached.
- `media_stories.posted_at` (`TIMESTAMPTZ NULL`) -- when this story was posted to
  `#league-wire`; publication state is per story, not per cycle (P5-99.s.1). Its
  read path ships with the column (`test_schema_drift`), its writer in the
  media-cycle commit, so for now it is a fully-plumbed, always-`NULL` column.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "020"
down_revision: str | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE leagues ADD COLUMN entry_work_completed_index INT NOT NULL DEFAULT -1")
    op.execute(
        "ALTER TABLE leagues ADD COLUMN closing_work_completed_index INT NOT NULL DEFAULT -1"
    )
    op.execute("ALTER TABLE player_motivation ADD COLUMN last_checkpoint_phase TEXT")
    op.execute("ALTER TABLE media_stories ADD COLUMN posted_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE media_stories DROP COLUMN posted_at")
    op.execute("ALTER TABLE player_motivation DROP COLUMN last_checkpoint_phase")
    op.execute("ALTER TABLE leagues DROP COLUMN closing_work_completed_index")
    op.execute("ALTER TABLE leagues DROP COLUMN entry_work_completed_index")
