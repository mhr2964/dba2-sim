"""RMLP DB backstop -- three constraints closing bugs (c)/(d) at the
database, independent of any application-level lock.

Revision ID: 017
Revises: 016
Create Date: 2026-09-18

docs/design/_rmlp_addendum_a.md (amending the inline Roster Mutation Lock
Protocol revision-2 design), §5. **Recommendation, restated:** round 6's
precedent for a single conditional `UPDATE` (`team_repo.
close_manager_assignment`'s `WHERE removed_at IS NULL`) does not transfer
here -- that guard and its write are the SAME statement, atomic under
Postgres' own row lock. Every bug this migration backstops is a
multi-statement SELECT-then-INSERT/UPDATE sequence with no predicate tying
write to read. The decisive asymmetry: a firing constraint costs a
rolled-back transaction and a clean message; an absent one costs a
permanently corrupted `dead_cap_charges`/`contracts` ledger with no cleanup
command and no reset path.

**017a -- one active contract per player.** Backstops round 6's invariant.
Safe against `apply_signing`'s terminate-then-sign sequence because the
index is non-deferrable, checked per statement, and the termination
(RMLP build step 4's expiry guard) now provably precedes the insert.

**017b -- one dead-cap charge per (contract, team, season).** The direct
storage-level kill for bug (c): two concurrent waives writing identical
triples -- the second blocks on the index entry, then fails. `team_id` is
included deliberately (not just `(source_contract_id, season)`): a
plausible future `TRADE_ABSORBED` shape could split one source contract's
dead cap across two teams in the same season, which the tighter key would
block; the looser key still catches every bug shape in this dispatch.

**017c -- a termination cannot predate its own signing.** The constraint
that would have caught bug (d) at the database. `>= signed_in_season`
rather than `>= start_season` -- a signed-but-not-yet-started deal being
voided in the offseason is a legitimate future shape; terminating a
contract before it was signed never is.

**Hard dependency (design's own correction to its "nothing later depends
on an earlier step" claim): 017c requires `gm_service.apply_signing`'s
`end_season < start_season` expiry guard (RMLP build step 4, shipped
before this migration in this same build) to already be landed.** Without
that guard, `apply_signing` could still stamp `terminated_in_season =
start_season - 1` on a LIVE contract whose `signed_in_season` is later,
which `VALIDATE CONSTRAINT` below would then fail on. That guard shipped
in an earlier commit of this same build; if this migration is ever
cherry-picked or replayed against a codebase that doesn't have it, 017c's
`VALIDATE CONSTRAINT` step is the canary that will say so.

**Pre-flight detection, not a blind `CREATE INDEX`/`ADD CONSTRAINT`.**
017a/017b each run a `SELECT ... GROUP BY ... HAVING COUNT(*) > 1` first
and raise with the offending ids if any exist, rather than letting
`CREATE UNIQUE INDEX` fail with an opaque duplicate-key error -- the
round-6/round-7 repro sequences may have left exactly this kind of row in
a dev database. 017c runs `NOT VALID` then `VALIDATE CONSTRAINT` in the
same migration, per the same "fail with a clear, specific message on
existing bad data" principle -- `VALIDATE CONSTRAINT`'s own error names
the violating row.

**017a's own disclosed future collision (§11 item D5-1):** this index
will collide with any future `ContractType.EXTENSION` implementation
(enum member exists, `dba2/domain/contract.py`; no implementation yet) --
the natural shape of an extension (a second `active` `contracts` row for
the same player, covering later seasons) is exactly what this index
forbids. Nothing to solve now; recorded here so whoever builds extensions
finds it in the migration, not in a failing insert.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "017"
down_revision: str | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- 017a: one active contract per player -----------------------------
    op.execute(
        """
        DO $$
        DECLARE
            offending TEXT;
        BEGIN
            SELECT string_agg(player_id::text, ', ') INTO offending
            FROM (
                SELECT player_id FROM contracts
                WHERE status = 'active'
                GROUP BY player_id
                HAVING COUNT(*) > 1
            ) dup;
            IF offending IS NOT NULL THEN
                RAISE EXCEPTION
                    'migration 017a: player_id(s) with more than one active '
                    'contract, must be resolved before this migration can run: %',
                    offending;
            END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_contracts_one_active_per_player "
        "ON contracts (player_id) WHERE status = 'active'"
    )

    # --- 017b: one dead-cap charge per (contract, team, season) -----------
    op.execute(
        """
        DO $$
        DECLARE
            offending TEXT;
        BEGIN
            SELECT string_agg(
                format('(%s, %s, %s)', source_contract_id, team_id, season), ', '
            ) INTO offending
            FROM (
                SELECT source_contract_id, team_id, season FROM dead_cap_charges
                GROUP BY source_contract_id, team_id, season
                HAVING COUNT(*) > 1
            ) dup;
            IF offending IS NOT NULL THEN
                RAISE EXCEPTION
                    'migration 017b: (source_contract_id, team_id, season) '
                    'triple(s) with more than one dead_cap_charges row, must be '
                    'resolved before this migration can run: %',
                    offending;
            END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_dead_cap_one_charge_per_contract_team_season "
        "ON dead_cap_charges (source_contract_id, team_id, season)"
    )

    # --- 017c: a termination cannot predate its own signing ---------------
    op.execute(
        "ALTER TABLE contracts ADD CONSTRAINT ck_contracts_terminated_not_before_signed "
        "CHECK (terminated_in_season IS NULL OR terminated_in_season >= signed_in_season) "
        "NOT VALID"
    )
    op.execute(
        "ALTER TABLE contracts VALIDATE CONSTRAINT ck_contracts_terminated_not_before_signed"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE contracts DROP CONSTRAINT ck_contracts_terminated_not_before_signed")
    op.execute("DROP INDEX ux_dead_cap_one_charge_per_contract_team_season")
    op.execute("DROP INDEX ux_contracts_one_active_per_player")
