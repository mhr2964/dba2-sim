"""games engine/replay columns, game_period_scores, schedule-scan indexes

Revision ID: 008
Revises: 007
Create Date: 2026-08-24

phase-1-simulation-engine.md §7.2. Following Phase 0's migration
convention: zero-padded three-digit revision, linear chain, raw SQL via
`op.execute`, no `IF NOT EXISTS`, explicit `ON DELETE` on every FK to
`leagues`/`teams` (§7.4 #4 asserts this).

`games` gains three columns:
- `engine_version` (Decision P1-22) — which tuning profile produced this
  result, so a stale replay is detectable rather than silently producing
  different basketball.
- `roster_digest` — a hash of each team's *available* roster (not just who
  ends up participating) and their rating rows, computed at
  `build_context` time. Nullable: unsimmed/scheduled games have no digest
  yet.
- `overtime_periods` — 0 for regulation; inferable only by summing minutes,
  which is fragile, hence a stored column.

`game_period_scores` is new: the line score, PK `(game_id, team_id,
period)`, `league_id` carried directly per the blanket "every league-scoped
table carries `league_id`" convention (migration 006's docstring) rather
than only being reachable via `game_id -> games.league_id`.

The four indexes below are the ones §7.2 lists, and are only useful because
they land in the same commit as game_repo.list_games_for_team_season's and
stats_repo.list_player_game_stats_for_player's `league_id`-scoping change
(§8.2) — an index leading on `league_id` is unusable by a query that never
filters on it, which is exactly the bug Amendment 1 corrected in this
table's design.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE games ADD COLUMN engine_version SMALLINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE games ADD COLUMN roster_digest TEXT NULL")
    op.execute("ALTER TABLE games ADD COLUMN overtime_periods SMALLINT NOT NULL DEFAULT 0")

    op.execute("""
        CREATE TABLE game_period_scores (
            game_id     INT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            league_id   INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id     INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            period      SMALLINT NOT NULL,
            points      SMALLINT NOT NULL DEFAULT 0,
            PRIMARY KEY (game_id, team_id, period)
        )
    """)

    op.execute(
        "CREATE INDEX ix_games_league_season_status ON games (league_id, season, status)"
    )
    op.execute(
        "CREATE INDEX ix_games_league_season_home_team "
        "ON games (league_id, season, home_team_id)"
    )
    op.execute(
        "CREATE INDEX ix_games_league_season_away_team "
        "ON games (league_id, season, away_team_id)"
    )
    op.execute(
        "CREATE INDEX ix_player_game_stats_league_player "
        "ON player_game_stats (league_id, player_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_player_game_stats_league_player")
    op.execute("DROP INDEX ix_games_league_season_away_team")
    op.execute("DROP INDEX ix_games_league_season_home_team")
    op.execute("DROP INDEX ix_games_league_season_status")
    op.execute("DROP TABLE game_period_scores")
    op.execute("ALTER TABLE games DROP COLUMN overtime_periods")
    op.execute("ALTER TABLE games DROP COLUMN roster_digest")
    op.execute("ALTER TABLE games DROP COLUMN engine_version")
