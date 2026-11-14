"""player_directives enum CHECKs, role_mode drop

Revision ID: 023
Revises: 022
Create Date: 2026-09-23

docs/design/gm-command-surface.md (PD-1/PD-3/PD-4/PD-5/PD-6), build steps
8-9. **Disclosed deviation from the design doc's own migration plan**:
migration `022`'s own docstring said build steps 8/9 would AMEND that same
migration file (it was "not yet shipped" when that docstring was written).
That is no longer true -- `alembic current` against the live dev database
already reports `022 (head)` (GM-1 through GM-10 are built, and the
current league at guild 1503802254346551318 has already played a full
season on top of it). Editing an already-applied migration's `upgrade()`
in place would silently diverge the migration FILE from what already ran
against every existing database -- alembic tracks a database's position by
revision id, not by content hash, so a stale-content mismatch would not
even fail loudly. A new migration is the standard-practice fix and the one
taken here; this docstring is what a future reader should trust over
`022`'s own now-stale build-step notes.

CHECK constraints on `player_directives.shot_diet`/`usage_mode`/
`defense_mode`/`clutch_mode` against their closed `StrEnum` value sets
(`dba2/domain/role.py`'s `ShotDiet`/`UsageMode`/`DefenseMode`/`ClutchMode`),
matching migration `009`'s `ck_teams_gm_philosophy` precedent exactly.
`role_mode` dropped (PD-6, confirmed by the user 2026-09-22 -- the one
irreversible step in this design). `player_directives` has zero rows in
the live dev database at the time of this migration (verified directly),
so neither change needs a data-migration step.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "023"
down_revision: str | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# dba2/domain/role.py -- verbatim.
_SHOT_DIET_VALUES = ("balanced", "more_threes", "attack_the_rim", "fewer_midrange")
_USAGE_MODE_VALUES = ("balanced", "initiate", "off_ball", "screen_and_roll", "post_up")
_DEFENSE_MODE_VALUES = ("balanced", "stay_home", "help_early")
_CLUTCH_MODE_VALUES = ("balanced",)


def upgrade() -> None:
    shot_diet_list = ", ".join(f"'{v}'" for v in _SHOT_DIET_VALUES)
    usage_mode_list = ", ".join(f"'{v}'" for v in _USAGE_MODE_VALUES)
    defense_mode_list = ", ".join(f"'{v}'" for v in _DEFENSE_MODE_VALUES)
    clutch_mode_list = ", ".join(f"'{v}'" for v in _CLUTCH_MODE_VALUES)

    op.execute(f"""
        ALTER TABLE player_directives
            ADD CONSTRAINT ck_player_directives_shot_diet
            CHECK (shot_diet IN ({shot_diet_list}))
    """)
    op.execute(f"""
        ALTER TABLE player_directives
            ADD CONSTRAINT ck_player_directives_usage_mode
            CHECK (usage_mode IN ({usage_mode_list}))
    """)
    op.execute(f"""
        ALTER TABLE player_directives
            ADD CONSTRAINT ck_player_directives_defense_mode
            CHECK (defense_mode IN ({defense_mode_list}))
    """)
    op.execute(f"""
        ALTER TABLE player_directives
            ADD CONSTRAINT ck_player_directives_clutch_mode
            CHECK (clutch_mode IN ({clutch_mode_list}))
    """)
    op.execute("ALTER TABLE player_directives DROP COLUMN role_mode")


def downgrade() -> None:
    op.execute("ALTER TABLE player_directives ADD COLUMN role_mode TEXT")
    op.execute(
        "ALTER TABLE player_directives DROP CONSTRAINT ck_player_directives_clutch_mode"
    )
    op.execute(
        "ALTER TABLE player_directives DROP CONSTRAINT ck_player_directives_defense_mode"
    )
    op.execute(
        "ALTER TABLE player_directives DROP CONSTRAINT ck_player_directives_usage_mode"
    )
    op.execute(
        "ALTER TABLE player_directives DROP CONSTRAINT ck_player_directives_shot_diet"
    )
