"""media_stories, media_pulse_snapshot, media_writers

Revision ID: 011
Revises: 010
Create Date: 2026-09-13

phase-4-media-and-player-agency.md §8 (Decision P4-32, as amended by
P4-38/P4-48/P4-66), §9.5 (P4-39), §11 Stage D step 19. Pure persistence --
this step wires `media_service.py::select_and_bind_story`/`run_media_pulse_
cycle` as real callers in the same commit, but nothing outside this
migration and `dba2/data/repositories/media_repo.py` touches these tables.

**§8's own schema block is pseudocode, not DDL (Decision P4-66) -- this
migration is the real translation, not a copy.** Two shapes in the design
doc's sketch are not valid inside a `CREATE TABLE` body at all: `INDEX
(league_id, season, cycle_index)` and `UNIQUE INDEX ... WHERE ...` are both
`CREATE INDEX` statements, issued separately below, matching every other
migration in this project's own `op.execute("CREATE INDEX ...")` convention
(migrations 001/002/003/005/006/007/008).

**`media_writers` is created FIRST, ahead of `media_stories`, even though
§8's own prose lists `media_stories` first** -- `media_stories.writer_id`
is a real `REFERENCES media_writers(id)` foreign key, so the referenced
table must exist first. `downgrade()` below drops in the reverse order for
the identical reason (`media_stories` must be dropped before `media_writers`,
since it is the one holding the live FK).

**media_writers.** One row per persistent, named persona instance (Decision
P4-38) -- `(league_id, team_id, persona)` for `beat_writer` (team-scoped:
one named beat writer per team), `(league_id, NULL, persona)` for the other
four archetypes (league-wide: one syndicated byline per league).
`CHECK ((persona = 'beat_writer') = (team_id IS NOT NULL))` (Decision
P4-66) enforces that persona/scope correspondence at the database, not by
caller discipline -- `beat_writer` is the exactly-one team-scoped archetype
in the closed five-key persona set (`seeds/media_phrases.json`'s own
`personas` array: `beat_writer`, `hot_take`, `analyst`, `insider`,
`historian` -- verified against the shipped library, not assumed from the
design doc's prose, per Decision P4-53).

**Two partial unique indexes, not a table-body `UNIQUE` constraint**
(Decision P4-48, corrected further by P4-66 into real `CREATE UNIQUE INDEX
... WHERE ...` statements): a plain `UNIQUE (league_id, team_id, persona)`
does not hold on `postgres:16` for the league-wide rows, because SQL NULL
is never equal to another NULL, so two `(league_id, NULL, 'hot_take')` rows
for the same league would both satisfy a naive constraint. The two partial
indexes below close that gap directly: `ux_media_writers_team` covers every
`team_id IS NOT NULL` row (one `beat_writer` per team), `ux_media_writers_
league` covers every `team_id IS NULL` row (one of each of the other four
archetypes per league).

**media_stories.** `subject_player_id`/`subject_team_id`/`writer_id` are
all `ON DELETE SET NULL` -- a story about a retired-and-purged player, or
bylined by a since-deleted writer row, is still real league history and
should not vanish with its subject; only `league_id -> leagues ON DELETE
CASCADE` removes the row itself, matching this schema's blanket
league-scoped-table convention (migration 006's docstring, reaffirmed by
008/010). `salience` is `DOUBLE PRECISION`, matching migration 010's own
choice for `gm_head_to_head`'s counters and this project's documented
`Decimal`-vs-`int`/`float` equality trap (HANDOFF.md's Traps section) --
`NUMERIC` would map to Python `Decimal` via asyncpg with no codec
registered on the pool (`dba2/data/db.py`), `DOUBLE PRECISION` maps to
`float`. `slot_bindings` is `JSONB`, marshalled to/from `dict` at the repo
boundary via `json.dumps`/`json.loads` (no custom type codec is registered
on the pool -- the same disclosed convention `dba2/data/repositories/
league_repo.py`'s `phase_data` column already uses). Both `slot_bindings`
and `rendered_text` are stored -- deliberate redundancy (Decision P4-32):
the structured form keeps a story queryable and re-renderable after a
template-library edit, the rendered snapshot means a template deleted in a
later library edit does not silently erase last season's news.
**Deliberately NO uniqueness constraint on `(league_id, season,
cycle_index, template_id)`** -- two different subjects can legitimately
draw the same template in the same cycle (e.g. two different teams both
drawing the league's one `fallback_streak` template in one cycle), and a
uniqueness constraint here would convert a normal, expected outcome into a
crash. A real `CREATE INDEX` on `(league_id, season, cycle_index)` supports
the per-cycle read pattern (`media_repo.list_media_stories_for_league_
season`) without needing that uniqueness.

**media_pulse_snapshot.** One row per team per cycle -- composite primary
key `(league_id, season, cycle_index, team_id)`, the persisted mirror of
`dba2.media.pulse.TeamPulseState`, one row per team rather than one JSON
blob per whole-league snapshot (mirrors `gm_head_to_head`'s own
directed-row-per-pair shape, migration 010, rather than a single blob per
row). The PK's own leading columns (`league_id, season, cycle_index`)
already cover both real read patterns this table needs -- "every team's row
for exactly this cycle" and, via a `MAX(cycle_index) WHERE cycle_index <
$3` subquery restricted to the same three leading columns, "every team's
row for the most recent PRIOR cycle" -- so no additional index is needed,
the identical "one `WHERE league_id = $1`-shaped loader, no N+1" reasoning
migration 010's own docstring states for `gm_head_to_head`. `wins`/
`losses`/`standings_position`/`streak_length` are plain `INT`, not
`DOUBLE PRECISION` -- unlike `gm_head_to_head`'s counters, these never
decay or accumulate fractionally; they are re-derived from `games` fresh
every cycle (Decision P4-30) and always integers. `ON DELETE CASCADE` on
both `league_id` and `team_id` -- this table is operational, rewritable
per-cycle state, not persisted history the way `media_stories` is (the
same status `gm_head_to_head`'s own rows have, and the reason that table
also has no `created_at`); a pulse reading for a deleted team or league has
no meaning to keep. No `created_at` column, for the identical reason.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE media_writers (
            id            SERIAL PRIMARY KEY,
            league_id     INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id       INT REFERENCES teams(id) ON DELETE CASCADE,
            persona       TEXT NOT NULL,
            name          TEXT NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK ((persona = 'beat_writer') = (team_id IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_media_writers_team ON media_writers (league_id, team_id, persona)
            WHERE team_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_media_writers_league ON media_writers (league_id, persona)
            WHERE team_id IS NULL
    """)

    op.execute("""
        CREATE TABLE media_stories (
            id                 SERIAL PRIMARY KEY,
            league_id          INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season             INT NOT NULL,
            phase              TEXT NOT NULL,
            cycle_index        INT NOT NULL,
            story_kind         TEXT NOT NULL,
            voice              TEXT NOT NULL,
            template_id        TEXT NOT NULL,
            slot_bindings      JSONB NOT NULL,
            rendered_text      TEXT NOT NULL,
            salience           DOUBLE PRECISION NOT NULL,
            subject_player_id  INT REFERENCES players(id) ON DELETE SET NULL,
            subject_team_id    INT REFERENCES teams(id) ON DELETE SET NULL,
            writer_id          INT REFERENCES media_writers(id) ON DELETE SET NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX ix_media_stories_league_season_cycle "
        "ON media_stories (league_id, season, cycle_index)"
    )

    op.execute("""
        CREATE TABLE media_pulse_snapshot (
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season              INT NOT NULL,
            cycle_index         INT NOT NULL,
            team_id             INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            wins                INT NOT NULL,
            losses              INT NOT NULL,
            standings_position  INT NOT NULL,
            streak_length       INT NOT NULL,
            PRIMARY KEY (league_id, season, cycle_index, team_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE media_pulse_snapshot")
    op.execute("DROP TABLE media_stories")
    op.execute("DROP TABLE media_writers")
