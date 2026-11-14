"""games, player_game_stats, team_game_stats, player_season_stats, injuries

Revision ID: 006
Revises: 005
Create Date: 2026-08-22

phase-0-foundation.md §4.7, §7.1. Schema-only in Phase 0 — Phase 1 needs a
place to write, Phase 0 defines the shapes and stops.

`player_game_stats` / `team_game_stats` carry `league_id` directly, matching
the blanket "every league-scoped table carries `league_id`" convention (§4
conventions preamble) that `player_ratings_history` and `player_season_stats`
already follow. Round 1 applied the opposite tiebreaker here (reachable via
`game_id -> games.league_id`, so denormalizing seemed redundant) while
applying the blanket rule everywhere else — an unintentional contradiction,
not a considered exception, per round-2 critic review. Resolved by applying
the convention consistently rather than carving out a second exception.

Shot-location columns (`fga_rim/fgm_rim`, `fga_mid/fgm_mid`,
`fga_three/fgm_three`) are the *only* field-goal columns — there is no
separate `fgm`/`fga` or `fg3m`/`fg3a`, since a made 3 is definitionally a
`_three` zone shot and a redundant second column claiming the same fact is
exactly the drift Decision 11 warns about for ratings. Totals are summed
across zones at read time.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ZONE_FG_COLUMNS = """
            fga_rim         SMALLINT NOT NULL DEFAULT 0,
            fgm_rim         SMALLINT NOT NULL DEFAULT 0,
            fga_mid         SMALLINT NOT NULL DEFAULT 0,
            fgm_mid         SMALLINT NOT NULL DEFAULT 0,
            fga_three       SMALLINT NOT NULL DEFAULT 0,
            fgm_three       SMALLINT NOT NULL DEFAULT 0
"""


def upgrade() -> None:
    op.execute("""
        CREATE TABLE games (
            id             SERIAL PRIMARY KEY,
            league_id      INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season         INT NOT NULL,
            game_index     INT NOT NULL,
            game_date      DATE NOT NULL,
            home_team_id   INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            away_team_id   INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            home_score     SMALLINT,
            away_score     SMALLINT,
            status         TEXT NOT NULL DEFAULT 'scheduled',
            game_type      TEXT NOT NULL,
            rng_seed       BIGINT NOT NULL,
            simmed_at      TIMESTAMPTZ,
            UNIQUE (league_id, season, game_index),
            CHECK (home_team_id <> away_team_id)
        )
    """)

    op.execute(f"""
        CREATE TABLE player_game_stats (
            game_id           INT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            league_id         INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id         INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            team_id           INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            minutes_played    NUMERIC(4,1) NOT NULL DEFAULT 0,
            points            SMALLINT NOT NULL DEFAULT 0,
{_ZONE_FG_COLUMNS},
            fta               SMALLINT NOT NULL DEFAULT 0,
            ftm               SMALLINT NOT NULL DEFAULT 0,
            oreb              SMALLINT NOT NULL DEFAULT 0,
            dreb              SMALLINT NOT NULL DEFAULT 0,
            ast               SMALLINT NOT NULL DEFAULT 0,
            stl               SMALLINT NOT NULL DEFAULT 0,
            blk               SMALLINT NOT NULL DEFAULT 0,
            tov               SMALLINT NOT NULL DEFAULT 0,
            pf                SMALLINT NOT NULL DEFAULT 0,
            plus_minus        SMALLINT NOT NULL DEFAULT 0,
            PRIMARY KEY (game_id, player_id)
        )
    """)

    op.execute(f"""
        CREATE TABLE team_game_stats (
            game_id        INT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            league_id      INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id        INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            possessions    SMALLINT NOT NULL DEFAULT 0,
            points         SMALLINT NOT NULL DEFAULT 0,
{_ZONE_FG_COLUMNS},
            fta            SMALLINT NOT NULL DEFAULT 0,
            ftm            SMALLINT NOT NULL DEFAULT 0,
            oreb           SMALLINT NOT NULL DEFAULT 0,
            dreb           SMALLINT NOT NULL DEFAULT 0,
            ast            SMALLINT NOT NULL DEFAULT 0,
            stl            SMALLINT NOT NULL DEFAULT 0,
            blk            SMALLINT NOT NULL DEFAULT 0,
            tov            SMALLINT NOT NULL DEFAULT 0,
            pf             SMALLINT NOT NULL DEFAULT 0,
            PRIMARY KEY (game_id, team_id)
        )
    """)

    # Materialized aggregate (§4.7) — rebuilt wholesale by stats_service rather than
    # aggregated on read, because career/leaderboard/award-vote/trade-evaluation
    # queries hit this constantly (Pillar 2 runs them per-decision across a league).
    op.execute(f"""
        CREATE TABLE player_season_stats (
            league_id         INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id         INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            season            INT NOT NULL,
            game_type         TEXT NOT NULL,
            games_played      SMALLINT NOT NULL DEFAULT 0,
            minutes_played    NUMERIC(6,1) NOT NULL DEFAULT 0,
            points            SMALLINT NOT NULL DEFAULT 0,
{_ZONE_FG_COLUMNS},
            fta               SMALLINT NOT NULL DEFAULT 0,
            ftm               SMALLINT NOT NULL DEFAULT 0,
            oreb              SMALLINT NOT NULL DEFAULT 0,
            dreb              SMALLINT NOT NULL DEFAULT 0,
            ast               SMALLINT NOT NULL DEFAULT 0,
            stl               SMALLINT NOT NULL DEFAULT 0,
            blk               SMALLINT NOT NULL DEFAULT 0,
            tov               SMALLINT NOT NULL DEFAULT 0,
            pf                SMALLINT NOT NULL DEFAULT 0,
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (league_id, player_id, season, game_type)
        )
    """)

    op.execute("""
        CREATE TABLE injuries (
            id                  SERIAL PRIMARY KEY,
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id           INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            game_id             INT REFERENCES games(id) ON DELETE SET NULL,
            injury_type         TEXT NOT NULL,
            severity            TEXT NOT NULL,
            games_remaining     SMALLINT,
            occurred_in_season  INT NOT NULL,
            resolved_at         TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX ix_injuries_player_id ON injuries (player_id)")


def downgrade() -> None:
    op.execute("DROP TABLE injuries")
    op.execute("DROP TABLE player_season_stats")
    op.execute("DROP TABLE team_game_stats")
    op.execute("DROP TABLE player_game_stats")
    op.execute("DROP TABLE games")
