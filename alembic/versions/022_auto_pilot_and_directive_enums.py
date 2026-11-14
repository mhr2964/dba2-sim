"""teams.auto_pilot

Revision ID: 022
Revises: 021
Create Date: 2026-09-22

docs/design/gm-command-surface.md (GM-1). Built across three separate
commits per that doc's own "Build order" section, each amending this same
not-yet-shipped migration rather than adding a new one, since nothing in
this design has run against a live database yet:

- **Build step 2 (this state) adds `teams.auto_pilot` alone.**
- Build step 8 will add CHECK constraints on `player_directives.shot_diet`/
  `usage_mode`/`defense_mode`/`clutch_mode` against their closed enum value
  sets (PD-1/PD-3/PD-4/PD-5).
- Build step 9 will drop `player_directives.role_mode` (PD-6, confirmed by
  the user 2026-09-22 -- the one irreversible step in this design).

`teams.auto_pilot BOOLEAN NOT NULL DEFAULT FALSE`, fully independent of
`manager_user_id` -- no composite meaning, no CHECK coupling the two
columns together (GM-1: a row may legitimately hold `manager_user_id IS
NULL AND auto_pilot = TRUE` for the instant between a `/remove-manager`
and its own clear, and the derived predicate answers identically in both
states, so a constraint would buy nothing and could fail a legitimate
write ordering).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "022"
down_revision: str | None = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE teams ADD COLUMN auto_pilot BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE teams DROP COLUMN auto_pilot")
