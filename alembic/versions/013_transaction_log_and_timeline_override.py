"""league_transactions, league_transaction_assets, teams.timeline_source,
leagues' advance-claim/hook-checkpoint columns, phase_history's missing
composite index.

Revision ID: 013
Revises: 012
Create Date: 2026-09-17

docs/design/phase-5-discord-presentation.md §18 (Decision P5-31, the
general transaction/audit log), §29 (Decision P5-66, the `/timeline`
override), §32.3 (the A-26/A-28 findings), §32.4/§40 (Decisions P5-72,
P5-78, P5-90, the orchestrator's claim/checkpoint columns) -- Stage B
(B-9, B-13, B-15, B-17), continuing past what migration 012's own
disclosed minimal slice shipped.

**Scope narrowed from the design doc's own "migration 013" bundle in one
place, disclosed here rather than silently dropped, matching migration
012's own precedent for the identical reason (no real writer yet).**
`trade_offers.cycle_index` and `league_transactions.cycle_index` (both
specified `NOT NULL`, no default -- P5-71/P5-74) are DEFERRED to
whichever commit builds Stage E's orchestrator. P5-71 is explicit that
the ordinal is derived by ONE site -- "the count of that season's prior
cycle-bearing phase entries in `phase_history`, read inside the same
transaction as the advance that creates the new one" -- and that a
second derivation of it anywhere else is the exact "second source of
truth" shape Decision P5-5 forbids. The orchestrator does not exist in
this codebase yet (Stage E, unbuilt); the one place a `cycle_index`
value would be needed today, `apply_trade`'s new log write (this
migration's own `league_transactions` table, wired in the commit
immediately following this one), has no correct source for it without
either reimplementing the orchestrator's own not-yet-built cadence logic
in miniature (the real risk: a standalone re-derivation drifting from
whatever Stage E's real cycle-bearing-phase set turns out to be) or
adding a `NOT NULL` column with no writer able to satisfy it, which is
the identical "table/column with no real writer" shape this project has
already declined twice (P5-9's beat-log table, `trade_offer_assets`
here in migration 012). **Every other column P5-71's own text describes
as safely inert at its default (`leagues.hook_completed_cycle_index`,
`leagues.advance_claimed_at`/`advance_claimed_by`) ships in this
migration as designed** -- those are genuinely inert (a default, or
nullable with no non-NULL value ever required), unlike the two
`cycle_index` columns, which are `NOT NULL` with no default and would
therefore need a real value supplied by SOMEONE on every future insert
starting the moment this migration lands.

**league_transactions / league_transaction_assets (Decision P5-31).**
Two tables, not one with a JSON asset array -- "who did team 7 acquire
in season 3" is a query, not a blob scan (the same `media_pulse_
snapshot`/`gm_head_to_head` row-per-subject argument migration 011/010
already made).

**Real deviation from P5-31's own literal schema, found by this
migration's own integration test, not assumed correct from the design
doc's prose.** P5-31's text specifies the two-column CHECK correspondence
`CHECK ((asset_kind = 'player') = (player_id IS NOT NULL))` verbatim
(mirroring `media_writers`' own persona/scope CHECK, migration 011) --
**but that constraint is incompatible with the SAME decision's own `ON
DELETE SET NULL` rule on every asset FK, one paragraph later in this
same migration's docstring.** Deleting a purged player CASCADEs from
`leagues`/`players` and triggers `league_transaction_assets.player_id`'s
own `ON DELETE SET NULL` -- which then violates the two-column CHECK,
because `asset_kind` stays `'player'` while `player_id` becomes `NULL`
in the same UPDATE the FK trigger issues. Postgres CHECK constraints
have no "insert-only" mode; they re-validate on every row state,
including one an FK's own `ON DELETE SET NULL` produces. Reproduced
directly: `tests/integration/test_gm_service.py`'s own trade-log
assertions pass, and the test's `finally`-block league cleanup (which
CASCADE-deletes the league's players) then fails with
`CheckViolationError` on this exact constraint. **The correspondence is
therefore enforced in Python at the repo boundary
(`txn_repo.add_transaction_asset`'s own `ValueError` guard) instead of
by a database CHECK** -- the same "enforced in Python, not by a DB
CHECK" shape this migration already uses for `txn_type`'s open
vocabulary, extended here for a reason specific to this table rather
than by default. `asset_kind` keeps a plain `CHECK (asset_kind IN
('player', 'pick'))` (a closed two-value set with no FK involved, so
this one constraint has no SET-NULL conflict) -- only the
CROSS-COLUMN correspondence is removed. `txn_type` is `TEXT` with an open
vocabulary enforced in Python (matching `player_motivation_events.
event_type`, `media_stories.story_kind`) -- a `CHECK` on the value set
would make adding a transaction type a migration, defeating the
extensibility the user's own ruling asked for (P5-26 on Q6b: "anything
needed should be added, there are probably other things that will need
to be tracked"). `details JSONB NOT NULL DEFAULT '{}'` matches `leagues.
phase_data`'s own convention (migration 001) -- marshalled to/from
`dict` at the repo boundary via `json.dumps`/`json.loads`, no custom
type codec registered on the pool (`dba2/data/db.py`); its own scope is
fenced in the design doc's own words and restated here: nothing this
column holds is ever filtered on in a query. Every asset FK is `ON
DELETE SET NULL` (a trade involving a since-purged player is still real
league history, `media_stories`' own documented rule); the header's own
`league_id` is `CASCADE` (only deleting the league deletes the record).
`source_offer_id` (nullable, `ON DELETE SET NULL`) is the join to
`trade_offers` -- written only on acceptance, once that path exists;
this migration adds the column now so the FK relationship is real from
the start, matching P5-31's own "created first for FK ordering"
discipline in spirit even though `trade_offers` itself shipped a
migration number earlier than this document's original plan (migration
012's own disclosed numbering note).

**teams.timeline_source (Decision P5-66).** `TEXT NOT NULL DEFAULT
'auto'` -- inert at its default, no writer yet (`/timeline`, Stage
D'-23, unbuilt). `teams.timeline` remains the ONE column holding the
value; this is a SOURCE tag, not a second value, matching P5-66's own
"a second column holding 'the manual value' would be a second
definition of a team's timeline" rule.

**phase_history composite index (A-26 finding).** Migration 001's own
`ix_phase_history_league_id` is single-column (`league_id` alone); the
cycle-ordinal count P5-71 will eventually run filters on both `league_
id` AND `season`. Postgres can still use the single-column index and
filter `season` afterward (not a correctness bug), but A-26's own
obligation is answered here rather than left for Stage E to discover:
add the composite index now, since the table already exists and gains
nothing by waiting.

**leagues.advance_claimed_at / advance_claimed_by (Decision P5-78,
amended by P5-90) and leagues.hook_completed_cycle_index (Decision
P5-72).** Both nullable/defaulted, no writer yet -- the orchestrator's
own claim-before-every-press mechanism and post-hook checkpoint (Stage
E, unbuilt). Per P5-90's own corrected semantics (round 6, carried
into B-17): these are the league's standing WORK CLAIM, read by every
human-initiated write path once built (not merely a guard against two
concurrent `/advance` presses), and the release is a fenced conditional
`UPDATE`, not a bare `SET NULL` -- documented here at the schema so a
future writer does not have to rediscover P5-90's own reasoning from
the design doc alone. `hook_completed_cycle_index INT NOT NULL DEFAULT
-1` is safe to add now precisely because it HAS a default and is
genuinely never compared against anything until the orchestrator
exists to write it -- unlike the two `cycle_index` columns this
migration declines to add, its own default value is never a lie about
"which cycle" (it means "no cycle's hook has completed yet," which is
true of every league before Stage E ships regardless of how many
`cycle_index` values a hypothetical column would have held).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "013"
down_revision: str | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE league_transactions (
            id                SERIAL PRIMARY KEY,
            league_id         INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season            INT NOT NULL,
            phase             TEXT NOT NULL,
            txn_type          TEXT NOT NULL,
            actor_user_id     BIGINT,
            source_offer_id   INT REFERENCES trade_offers(id) ON DELETE SET NULL,
            note              TEXT,
            details           JSONB NOT NULL DEFAULT '{}',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX ix_league_transactions_league_season_created "
        "ON league_transactions (league_id, season, created_at)"
    )

    op.execute("""
        CREATE TABLE league_transaction_assets (
            id                SERIAL PRIMARY KEY,
            transaction_id    INT NOT NULL REFERENCES league_transactions(id) ON DELETE CASCADE,
            asset_kind        TEXT NOT NULL,
            player_id         INT REFERENCES players(id) ON DELETE SET NULL,
            draft_pick_id     INT REFERENCES draft_picks(id) ON DELETE SET NULL,
            from_team_id      INT REFERENCES teams(id) ON DELETE SET NULL,
            to_team_id        INT REFERENCES teams(id) ON DELETE SET NULL,
            CHECK (asset_kind IN ('player', 'pick'))
        )
    """)
    op.execute(
        "CREATE INDEX ix_league_transaction_assets_transaction_id "
        "ON league_transaction_assets (transaction_id)"
    )

    op.execute(
        "ALTER TABLE teams ADD COLUMN timeline_source TEXT NOT NULL DEFAULT 'auto'"
    )

    op.execute(
        "CREATE INDEX ix_phase_history_league_season ON phase_history (league_id, season)"
    )

    op.execute("ALTER TABLE leagues ADD COLUMN advance_claimed_at TIMESTAMPTZ")
    op.execute("ALTER TABLE leagues ADD COLUMN advance_claimed_by BIGINT")
    op.execute(
        "ALTER TABLE leagues ADD COLUMN hook_completed_cycle_index INT NOT NULL DEFAULT -1"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE leagues DROP COLUMN hook_completed_cycle_index")
    op.execute("ALTER TABLE leagues DROP COLUMN advance_claimed_by")
    op.execute("ALTER TABLE leagues DROP COLUMN advance_claimed_at")
    op.execute("DROP INDEX ix_phase_history_league_season")
    op.execute("ALTER TABLE teams DROP COLUMN timeline_source")
    op.execute("DROP TABLE league_transaction_assets")
    op.execute("DROP TABLE league_transactions")
