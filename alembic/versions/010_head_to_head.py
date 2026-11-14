"""gm_head_to_head

Revision ID: 010
Revises: 009
Create Date: 2026-09-09

phase-3-inter-manager-society.md §7 (Decision P3-22, P3-35, P3-42). Pure
persistence -- §11 step 10 wires no caller; nothing reads or writes this
table yet.

One row per directed `(league_id, team_id, opponent_id)` pair -- `team_id`'s
running memory of negotiations with `opponent_id`. 30 x 29 = 870 rows per
league (`team_id != opponent_id` is an application invariant, not a CHECK
constraint here -- no other table in this schema enforces a same-table
self-reference exclusion at the DB level, and the loader/upsert are the
only writers this step introduces). `league_id` is carried directly per the
blanket "every league-scoped table carries `league_id`" convention
(migration 006's docstring, reaffirmed by migration 008's), not only
reachable transitively via `team_id -> teams.league_id`.

Every counter is `DOUBLE PRECISION`, not `INT` or `NUMERIC` -- `numeric`
maps to Python `Decimal` via asyncpg, which is the trap (HANDOFF's
recurring bug class); `double precision` maps to `float`. The counters are
float rather than int for a second, independent reason stated in §7: they
decay at the season boundary (`H2H_DECAY`, Decision P3-25), so a decayed
count is not an integer.

Counters default to 0 (matching this schema's existing convention for
per-row counters, e.g. migration 006's box-score columns) -- a row
representing a pair that has never interacted starts at all-zero. This
migration does not seed any rows; a later step is responsible for creating
the 870-row block per league.

`last_gap_by_opponent` and `last_season_interacted` are nullable with no
default: 0.0 is a real, distinguishable gap value, and season 0 is not a
real season, so "no event yet" is represented as `NULL`, not a sentinel
zero. `updated_season` is `NOT NULL` with no default -- every write states
which season it reflects.

Composite PK `(league_id, team_id, opponent_id)` -- the loader's single
`WHERE league_id = $1` query is a leading-column scan of this same index,
so no additional index is needed (§7: "one `SELECT ... WHERE league_id =
$1` is the whole loader, no N+1 concern").

`ON DELETE CASCADE` on all three FKs (`league_id -> leagues`, `team_id ->
teams`, `opponent_id -> teams`): a head-to-head row is meaningless once
either team or the league is gone, matching this schema's existing
convention for every other league/team-scoped table (e.g. migration 008's
`game_period_scores`).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE gm_head_to_head (
            league_id               INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id                 INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            opponent_id             INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            opened                  DOUBLE PRECISION NOT NULL DEFAULT 0,
            received                DOUBLE PRECISION NOT NULL DEFAULT 0,
            accepts_by_opponent     DOUBLE PRECISION NOT NULL DEFAULT 0,
            rejects_by_opponent     DOUBLE PRECISION NOT NULL DEFAULT 0,
            accepts_by_self         DOUBLE PRECISION NOT NULL DEFAULT 0,
            rejects_by_self         DOUBLE PRECISION NOT NULL DEFAULT 0,
            counters_given          DOUBLE PRECISION NOT NULL DEFAULT 0,
            counters_received       DOUBLE PRECISION NOT NULL DEFAULT 0,
            executions              DOUBLE PRECISION NOT NULL DEFAULT 0,
            refused_illegal         DOUBLE PRECISION NOT NULL DEFAULT 0,
            far_reject_streak       DOUBLE PRECISION NOT NULL DEFAULT 0,
            gap_by_opponent_sum     DOUBLE PRECISION NOT NULL DEFAULT 0,
            last_gap_by_opponent    DOUBLE PRECISION NULL,
            last_season_interacted  INT NULL,
            updated_season          INT NOT NULL,
            PRIMARY KEY (league_id, team_id, opponent_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE gm_head_to_head")
