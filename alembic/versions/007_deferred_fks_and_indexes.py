"""cross-table FKs deferred by ordering, plus all composite indexes

Revision ID: 007
Revises: 006
Create Date: 2026-08-22

phase-0-foundation.md §7.1.

Every index in this migration is 2+ columns and tied to a named query
pattern from §4/§5 (not speculative):
- players: the three composite indexes §4.3.1 names explicitly (roster by
  team, roster by status, leaderboard sort).
- contracts: (league_id, team_id, status) — the "active contracts for a
  team" half of the cap-summary formula in §4.4, and `contract_repo`
  ("all cap-sheet queries live here", §5).
- dead_cap_charges: (league_id, team_id, season) — the other half of that
  same cap-summary formula, verbatim from §4.4's definition.
- draft_picks: (league_id, current_team_id) — "a team's current draft
  assets," the query a trade evaluator runs (Decision 21).
- games: (league_id, season, game_date) — `game_repo`'s "schedule
  queries" (§5).

`draft_picks.used_for_player_id -> players(id)` is added here rather than
inline in migration 004 to keep that migration's table-creation statements
from reaching across the draft_repo/player_repo boundary at creation time —
see the note in 004's docstring.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE draft_picks
            ADD CONSTRAINT fk_draft_picks_used_for_player
            FOREIGN KEY (used_for_player_id) REFERENCES players(id) ON DELETE SET NULL
    """)

    op.execute("CREATE INDEX ix_players_league_team ON players (league_id, team_id)")
    op.execute(
        "CREATE INDEX ix_players_league_roster_status ON players (league_id, roster_status)"
    )
    op.execute(
        "CREATE INDEX ix_players_league_overall ON players (league_id, overall_cached DESC)"
    )

    op.execute(
        "CREATE INDEX ix_contracts_league_team_status ON contracts (league_id, team_id, status)"
    )

    op.execute(
        "CREATE INDEX ix_dead_cap_charges_league_team_season "
        "ON dead_cap_charges (league_id, team_id, season)"
    )

    op.execute(
        "CREATE INDEX ix_draft_picks_league_current_team "
        "ON draft_picks (league_id, current_team_id)"
    )

    op.execute(
        "CREATE INDEX ix_games_league_season_date ON games (league_id, season, game_date)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_games_league_season_date")
    op.execute("DROP INDEX ix_draft_picks_league_current_team")
    op.execute("DROP INDEX ix_dead_cap_charges_league_team_season")
    op.execute("DROP INDEX ix_contracts_league_team_status")
    op.execute("DROP INDEX ix_players_league_overall")
    op.execute("DROP INDEX ix_players_league_roster_status")
    op.execute("DROP INDEX ix_players_league_team")
    op.execute("ALTER TABLE draft_picks DROP CONSTRAINT fk_draft_picks_used_for_player")
