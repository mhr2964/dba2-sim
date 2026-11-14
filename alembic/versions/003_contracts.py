"""contracts, contract_years, dead_cap_charges, cap_holds, rookie_scale

Revision ID: 003
Revises: 002
Create Date: 2026-08-22

phase-0-foundation.md §4.4, §7.1, §7.2. This is the section with no v1
ancestor (§1 finding 1) — dead cap and max contracts don't exist there.

Decision 18: per-season `contract_years` rows instead of v1's single
`contracts.salary` + `years_remaining` — raises, options, and partial
guarantees are all per-year facts, and dead cap is computed from *remaining
guaranteed years*, which a single scalar cannot express.

ON DELETE choices worth calling out (not defaulting to CASCADE without
thinking, per this agent's own quality gate):
- `contracts.team_id` is RESTRICT, not SET NULL (round-2 fix — round 1 had
  this as SET NULL, which reproduces a named v1 bug: deleting a team left an
  `active` contract with `team_id = NULL` and its `contract_years` intact,
  so the money silently vanished from every cap sheet with no trace). §4.4
  says team_id is "nullable only for voided rows" — nothing enforced that
  under SET NULL, since a delete happily nulled an active row's team_id too.
  RESTRICT makes "a team with active contracts can't be deleted" a schema
  fact instead of a hoped-for caller discipline; a season-long league has no
  legitimate reason to delete a team that still owes money to players, and
  the correct sequence (waive/trade every contract first, generating the
  usual dead-cap/trade bookkeeping) already goes through `contract_repo`
  before a team row would ever be removed.
- `dead_cap_charges.player_id` / `.source_contract_id` are RESTRICT — the
  charge is a financial-history ledger row that must not silently
  disappear if a player or contract row is ever deleted; `dead_cap_charges`
  itself only leaves via its own `league_id` cascade (a whole-league
  teardown), never via losing its player/contract pointer.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE contracts (
            id                     SERIAL PRIMARY KEY,
            league_id              INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id              INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            team_id                INT REFERENCES teams(id) ON DELETE RESTRICT,
            signed_in_season       INT NOT NULL,
            start_season           INT NOT NULL,
            end_season             INT NOT NULL,
            total_years            SMALLINT NOT NULL,
            contract_type          TEXT NOT NULL,
            signing_method         TEXT NOT NULL,
            no_trade_clause        BOOLEAN NOT NULL DEFAULT FALSE,
            trade_kicker_pct       SMALLINT NOT NULL DEFAULT 0,
            status                 TEXT NOT NULL,
            terminated_in_season   INT,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_contracts_player_id ON contracts (player_id)")

    op.execute("""
        CREATE TABLE contract_years (
            contract_id         INT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
            season               INT NOT NULL,
            salary               BIGINT NOT NULL,
            guaranteed_amount    BIGINT NOT NULL,
            option_type          TEXT NOT NULL DEFAULT 'none',
            option_decision      TEXT NOT NULL DEFAULT 'pending',
            PRIMARY KEY (contract_id, season),
            CHECK (guaranteed_amount <= salary)
        )
    """)

    op.execute("""
        CREATE TABLE dead_cap_charges (
            id                    SERIAL PRIMARY KEY,
            league_id             INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id               INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            player_id             INT NOT NULL REFERENCES players(id) ON DELETE RESTRICT,
            source_contract_id    INT NOT NULL REFERENCES contracts(id) ON DELETE RESTRICT,
            season                INT NOT NULL,
            amount                BIGINT NOT NULL,
            reason                TEXT NOT NULL,
            created_in_season     INT NOT NULL,
            stretch_years         SMALLINT
        )
    """)

    # Decision 19: cap_holds ships schema-only in Phase 0 so cap_service's
    # get_cap_summary(..., include_holds: bool) signature doesn't need a
    # rewrite when Phase 2 free agency starts populating it.
    op.execute("""
        CREATE TABLE cap_holds (
            id           SERIAL PRIMARY KEY,
            league_id    INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id      INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            player_id    INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            season       INT NOT NULL,
            amount       BIGINT NOT NULL,
            hold_type    TEXT NOT NULL
        )
    """)

    # Ported verbatim from v1 (pick_number PK, year_1..4_salary), including its
    # seeded 30-row 2024-25 approximation (§7.2 — league-independent lookup data
    # ships inside a migration, not seeds/).
    op.execute("""
        CREATE TABLE rookie_scale (
            pick_number      SMALLINT PRIMARY KEY,
            year_1_salary    BIGINT NOT NULL,
            year_2_salary    BIGINT NOT NULL,
            year_3_salary    BIGINT NOT NULL,
            year_4_salary    BIGINT NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO rookie_scale
            (pick_number, year_1_salary, year_2_salary, year_3_salary, year_4_salary)
        VALUES
            (1,  10306508, 10821833, 11337157, 14858652),
            (2,   9700911,  10185957, 10671002, 13925000),
            (3,   9145524,  9602800, 10060076, 13105500),
            (4,   8633147,  9064804,  9496462, 12330000),
            (5,   8160117,  8568123,  8976128, 11600000),
            (6,   7722917,  8109063,  8495209, 10910000),
            (7,   7319127,  7685083,  8051039, 10265000),
            (8,   6945399,  7292669,  7639939,  9662000),
            (9,   6599445,  6929417,  7259389,  9097000),
            (10,  6279079,  6593033,  6906986,  8571000),
            (11,  5982158,  6281266,  6580374,  8080000),
            (12,  5706614,  5991945,  6277275,  7622000),
            (13,  5450437,  5722959,  5995481,  7195000),
            (14,  5212638,  5473270,  5733902,  6796000),
            (15,  4992269,  5241882,  5491496,  6425000),
            (16,  4787395,  5026765,  5266134,  6078000),
            (17,  4597110,  4826966,  5056821,  5756000),
            (18,  4420537,  4641564,  4862590,  5455000),
            (19,  4256826,  4469667,  4682508,  5174000),
            (20,  4105149,  4310406,  4515664,  4912000),
            (21,  3964702,  4162937,  4361172,  4668000),
            (22,  3834700,  4026435,  4218171,  4441000),
            (23,  3714376,  3900095,  4085814,  4228000),
            (24,  3602982,  3782131,  3961281,  4029000),
            (25,  3499789,  3672778,  3845768,  3842000),
            (26,  3404091,  3571296,  3738500,  3667000),
            (27,  3315200,  3476960,  3638720,  3503000),
            (28,  3232448,  3394070,  3555693,  3348000),
            (29,  3155188,  3312947,  3470707,  3203000),
            (30,  2082678,  2186812,  2290946,  2000000)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE rookie_scale")
    op.execute("DROP TABLE cap_holds")
    op.execute("DROP TABLE dead_cap_charges")
    op.execute("DROP TABLE contract_years")
    op.execute("DROP TABLE contracts")
