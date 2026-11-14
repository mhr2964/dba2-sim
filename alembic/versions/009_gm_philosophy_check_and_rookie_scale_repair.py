"""teams.gm_philosophy CHECK constraint, rookie_scale row-30 data repair

Revision ID: 009
Revises: 008
Create Date: 2026-08-28

phase-2-manager-minds.md §3 (Decision P2-1), §4.6 (Decision P2-33), §8
("Migration 009's exact scope"). Decision P2-5 is explicit that this
migration carries exactly these two things and nothing else.

Part A -- Decision P2-1: `teams.gm_philosophy` has been an unconstrained
`TEXT NOT NULL DEFAULT 'tendency_respecter'` since migration 001, with no
CHECK (Gap B). `domain/team.py::GmPhilosophy` already enumerates the seven
Pillar-2 keys this constraint closes the gap against; the enum's own
docstring names this exact migration as the fix, in advance, at the point
the enum was introduced (Phase 2 step 1).

Part B -- Decision P2-33: migration 003 seeded `rookie_scale` row 30 with a
figure corrupted in all four year-columns (identified critic round 10, B1
-- y1/y2/y3 each ~-34% below row 29's own figure and y4 ~-38% below, against
a table that otherwise declines only 2.4-2.7%/slot for y1-y3 and 4.3-4.6%/
slot for y4 across rows 25-29). A targeted `UPDATE ... WHERE pick_number =
30`, not a table re-seed -- rows 1-29 must stay byte-identical to migration
003's own INSERT, which `test_migration_009_rookie_scale_repair` in
tests/integration/test_migrations_roundtrip.py asserts directly (the check
that stops a data repair from quietly widening into a re-seed).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decision P2-1: the seven GmPhilosophy keys, verbatim (domain/team.py).
_GM_PHILOSOPHY_VALUES = (
    "star_maxer",
    "egalitarian",
    "defense_first",
    "tendency_respecter",
    "vet_overrater",
    "youth_developer",
    "chaos",
)

# Decision P2-33: corrupt (migration 003) vs. repaired row-30 figures.
_ROW_30_CORRUPT = (2_082_678, 2_186_812, 2_290_946, 2_000_000)
_ROW_30_REPAIRED = (3_079_775, 3_233_763, 3_387_752, 3_064_280)


def upgrade() -> None:
    values_list = ", ".join(f"'{v}'" for v in _GM_PHILOSOPHY_VALUES)
    op.execute(f"""
        ALTER TABLE teams
            ADD CONSTRAINT ck_teams_gm_philosophy
            CHECK (gm_philosophy IN ({values_list}))
    """)

    op.execute(f"""
        UPDATE rookie_scale
        SET year_1_salary = {_ROW_30_REPAIRED[0]},
            year_2_salary = {_ROW_30_REPAIRED[1]},
            year_3_salary = {_ROW_30_REPAIRED[2]},
            year_4_salary = {_ROW_30_REPAIRED[3]}
        WHERE pick_number = 30
    """)


def downgrade() -> None:
    op.execute(f"""
        UPDATE rookie_scale
        SET year_1_salary = {_ROW_30_CORRUPT[0]},
            year_2_salary = {_ROW_30_CORRUPT[1]},
            year_3_salary = {_ROW_30_CORRUPT[2]},
            year_4_salary = {_ROW_30_CORRUPT[3]}
        WHERE pick_number = 30
    """)

    op.execute("ALTER TABLE teams DROP CONSTRAINT ck_teams_gm_philosophy")
