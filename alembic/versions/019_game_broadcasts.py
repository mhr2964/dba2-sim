"""game_broadcasts -- the persisted half of Stage D's `/watch` broadcast: which
Discord thread and message a game's replay lives in, how far the broadcast got,
and whether the game's result has been published to `#games`.

Revision ID: 019
Revises: 018
Create Date: 2026-09-19

docs/design/phase-5-stage-d-broadcast.md §8 (P5-97), build-plan commit 5.

Why a table at all (design §8, "Does this contradict P5-42?"): P5-42 forbids
persisting the beat log and nothing here does -- this stores Discord message
ids, the category P5-12/P5-37 already put in Postgres so the bot can edit a
message again after a restart. One small table serves three requirements the
process cannot: the cross-restart thread link (`/watch` is keyed on this row,
never on memory, so a restart cannot produce a second thread for one game),
the reveal compare-and-swap (P5-97.m), and the startup sweep's queue
(P5-97.q item 6).

- `UNIQUE (game_id)` -- one broadcast row per game (P5-18: one thread per
  game). It is also what makes `create_pending_broadcasts`'s
  `ON CONFLICT (game_id) DO NOTHING` and the single-row reveal CAS possible.
- `league_id`, `game_id` are `ON DELETE CASCADE`: a broadcast row is pure
  bookkeeping about a game and has no meaning once its league or game is gone.
  The ids are `BIGINT` per the design although `leagues.id`/`games.id` are
  `INT` (`SERIAL`); a `BIGINT` foreign key onto an `INT` primary key is legal
  and every value that can exist in the referenced column fits.
- `status` is `TEXT` with NO `CHECK` -- migration 016's own choice for
  `league_channels.purpose`, for the same reason: the closed set
  (`pending | queued | live | finished | interrupted`) is enforced in Python
  at `domain/broadcast.BroadcastStatus`, so widening it is a Python change
  and not a drop-and-re-add of a constraint.
- `reveal_claimed_at` / `reveal_posted_at` -- the two phases of the reveal
  (P5-97.m, M4). A row with claimed set and posted NULL is a crashed reveal.
- `root_message_id`, `thread_id`, `message_id` are nullable with no backfill:
  a row is born `pending` before any Discord object exists.

`ix_game_broadcasts_sweep` (M10) -- the startup sweep asks, per league, for
every row whose result has not yet been published (`reveal_posted_at IS
NULL`): the unfinished ones (`live`/`queued`), the crashed reveals, and the
orphaned `pending` rows. `status` follows `league_id` so the planner can also
narrow to one status, and the partial predicate keeps the index down to the
handful of rows still owing a reveal instead of every game ever broadcast.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "019"
down_revision: str | None = "018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE game_broadcasts (
            id                BIGSERIAL PRIMARY KEY,
            league_id         BIGINT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            game_id           BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            block_id          TEXT   NOT NULL,
            root_message_id   BIGINT,
            thread_id         BIGINT,
            message_id        BIGINT,
            status            TEXT   NOT NULL,
            reveal_claimed_at TIMESTAMPTZ,
            reveal_posted_at  TIMESTAMPTZ,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (game_id)
        )
    """)
    op.execute("""
        CREATE INDEX ix_game_broadcasts_sweep
            ON game_broadcasts (league_id, status)
            WHERE reveal_posted_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX ix_game_broadcasts_sweep")
    op.execute("DROP TABLE game_broadcasts")
