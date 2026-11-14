"""media_stories, media_pulse_snapshot, media_writers -- `media_repo`'s
three tables (migration 011, Decision P4-32 as amended by P4-38/P4-48/
P4-66).

docs/design/phase-4-media-and-player-agency.md §8, §9.5, §11 Stage D step
19.

**Why single-row inserts for `media_stories`, not a bulk `UNNEST` writer
like `head_to_head_repo.upsert_head_to_head_for_league`.** `head_to_head_
repo`'s bulk shape exists because one write touches many of a league's 870
rows at once (a trade window's counter increments, a season-boundary decay
pass). A media cycle's real write volume is the opposite shape: `dba2.
media.salience.TOP_K_PER_CYCLE` caps a cycle at 12 selected stories, and
persisting each one already requires its own per-candidate work upstream of
this module (a persona/template RNG draw, a `team_repo.get_team`/`player_
repo.get_player` name lookup, a `get_or_create_media_writer` lookup-or-
insert) -- `dba2/services/media_service.py::select_and_bind_story` already
does one DB round trip's worth of *other* work per candidate before it ever
reaches this module, so batching only the final `INSERT` would not remove
the dominant cost and would need every candidate's full pipeline to
complete before any of them could be persisted. A plain per-candidate
`create_media_story` call, issued from inside that same per-candidate loop,
is the real shape this step's own caller needs.

**Why `media_pulse_snapshot` DOES get a bulk upsert.** Unlike stories, one
cycle's pulse write is genuinely "many rows, one shape, no other per-row
work" -- every league team gets exactly one row (`dba2/media/pulse.py::
build_pulse_snapshot` already builds one `TeamPulseState` per team,
unconditionally), so `upsert_pulse_snapshot_teams` mirrors `head_to_head_
repo`'s own `UNNEST` + `ON CONFLICT DO UPDATE` shape for the identical
reason: one round trip for however many team rows a cycle produces, not one
query per team.

`slot_bindings` is `JSONB` with no custom type codec registered on the pool
(`dba2/data/db.py` never calls `conn.set_type_codec` for json/jsonb), so
this module marshals it to/from `dict` at the repo boundary via `json.
dumps`/`json.loads` -- the identical, already-proven convention `dba2/data/
repositories/league_repo.py`'s `phase_data` column uses.

`story_kind`/`phase` are `TEXT`-backed enum columns with no DB `CHECK`
constraining them (this schema's existing convention, per `league_repo.py`'s
own docstring for `current_phase`): a corrupt stored value raises a bare
`ValueError` from `StoryKind(...)`/`Phase(...)` construction, let through
uncaught rather than wrapped, matching every other repository module in
this package.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

import asyncpg

from dba2.data.db import Executor
from dba2.domain.media import MediaPulseSnapshot, MediaStory, MediaWriter, StoryKind
from dba2.domain.phase import Phase

# ---------------------------------------------------------------------------
# media_stories
# ---------------------------------------------------------------------------

_MEDIA_STORY_COLUMNS = """
    id, league_id, season, phase, cycle_index, story_kind, voice, template_id,
    slot_bindings, rendered_text, salience, subject_player_id, subject_team_id,
    writer_id, created_at, posted_at, headline, format_tier, fact_package,
    writer_backend, league_brief
"""


def _map_media_story(r: asyncpg.Record) -> MediaStory:
    return MediaStory(
        id=r["id"],
        league_id=r["league_id"],
        season=r["season"],
        phase=Phase(r["phase"]),
        cycle_index=r["cycle_index"],
        story_kind=StoryKind(r["story_kind"]),
        voice=r["voice"],
        template_id=r["template_id"],
        slot_bindings=json.loads(r["slot_bindings"]),
        rendered_text=r["rendered_text"],
        salience=r["salience"],
        subject_player_id=r["subject_player_id"],
        subject_team_id=r["subject_team_id"],
        writer_id=r["writer_id"],
        created_at=r["created_at"],
        posted_at=r["posted_at"],
        headline=r["headline"],
        format_tier=r["format_tier"],
        fact_package=json.loads(r["fact_package"]),
        writer_backend=r["writer_backend"],
        league_brief=json.loads(r["league_brief"]),
    )


async def create_media_story(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    phase: Phase,
    cycle_index: int,
    story_kind: StoryKind,
    voice: str,
    template_id: str,
    slot_bindings: Mapping[str, str],
    rendered_text: str,
    salience: float,
    subject_player_id: int | None,
    subject_team_id: int | None,
    writer_id: int | None,
    headline: str | None = None,
    format_tier: str = "blurb",
    fact_package: Mapping[str, object] | None = None,
    writer_backend: str = "template",
    league_brief: Mapping[str, object] | None = None,
) -> MediaStory:
    """One story, one insert -- see module docstring for why this is
    single-row rather than bulk. No `ON CONFLICT` clause: migration 011
    deliberately carries no uniqueness constraint on `(league_id, season,
    cycle_index, template_id)` (two different subjects can legitimately
    draw the same template in one cycle), so a second insert naming the
    same template in the same cycle is a normal outcome, not a caller bug
    to absorb.

    **`headline`/`format_tier`/`fact_package`/`writer_backend` (migration
    024, build step 7) are all defaulted** so every pre-step-7 caller (the
    integration tests that predate the writer-backend wiring) keeps working
    unchanged, persisting the same `'blurb'`/`'{}'`/`'template'` values the
    column defaults themselves backfill onto history. `fact_package`
    defaults to `{}` here (not `None`) at the query boundary -- `None` would
    write a SQL `NULL` into a `NOT NULL JSONB` column and raise.

    **`league_brief` (migration 025, MV-12.3 Option A, build step 7.5b) is
    defaulted the identical way, for the identical reason** -- `{}` for
    every caller that has no real `LeagueBrief` to persist yet (every
    pre-step-7.5b caller, and `select_and_bind_story`'s own pre-trade-
    deadline fallback, MV-11's disclosed gap)."""
    row = await ex.fetchrow(
        f"""
        INSERT INTO media_stories
            (league_id, season, phase, cycle_index, story_kind, voice, template_id,
             slot_bindings, rendered_text, salience, subject_player_id,
             subject_team_id, writer_id, headline, format_tier, fact_package,
             writer_backend, league_brief)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
        RETURNING {_MEDIA_STORY_COLUMNS}
        """,
        league_id,
        season,
        phase.value,
        cycle_index,
        story_kind.value,
        voice,
        template_id,
        json.dumps(dict(slot_bindings)),
        rendered_text,
        salience,
        subject_player_id,
        subject_team_id,
        writer_id,
        headline,
        format_tier,
        json.dumps(dict(fact_package) if fact_package is not None else {}),
        writer_backend,
        json.dumps(dict(league_brief) if league_brief is not None else {}),
    )
    return _map_media_story(row)


async def get_media_story(ex: Executor, story_id: int) -> MediaStory | None:
    row = await ex.fetchrow(
        f"SELECT {_MEDIA_STORY_COLUMNS} FROM media_stories WHERE id = $1", story_id
    )
    return _map_media_story(row) if row is not None else None


async def list_media_stories_for_league_season(
    ex: Executor, *, league_id: int, season: int, cycle_index: int | None = None
) -> list[MediaStory]:
    """Every story for one league+season, optionally narrowed to one
    `cycle_index` -- the two real read shapes this dispatch's own scope
    names. `cycle_index=None` reads the whole season, ordered oldest-cycle-
    first then insertion order within a cycle (`id` is monotonic per
    `SERIAL`, so this is also real selection order within a cycle, matching
    `candidate_sequence`'s own zero-based position -- not asserted or relied
    on by anything here, just a disclosed, free-to-observe property)."""
    if cycle_index is None:
        rows = await ex.fetch(
            f"""
            SELECT {_MEDIA_STORY_COLUMNS} FROM media_stories
            WHERE league_id = $1 AND season = $2
            ORDER BY cycle_index, id
            """,
            league_id,
            season,
        )
    else:
        rows = await ex.fetch(
            f"""
            SELECT {_MEDIA_STORY_COLUMNS} FROM media_stories
            WHERE league_id = $1 AND season = $2 AND cycle_index = $3
            ORDER BY id
            """,
            league_id,
            season,
            cycle_index,
        )
    return [_map_media_story(r) for r in rows]


async def list_media_stories_for_cycle_range(
    ex: Executor, *, league_id: int, season: int, start_cycle_index: int, end_cycle_index: int
) -> list[MediaStory]:
    """MV-12.1 §3's own read (`recent_wire`, "published stories from the
    last 3 cycles") -- `cycle_index BETWEEN start AND end`, inclusive,
    newest-cycle-first (`ORDER BY cycle_index DESC, id DESC`, matching
    `list_media_stories_page`'s own recency convention, since a brief's own
    §3/§5 ranking cares about recency, not insertion order within a
    cycle). One bounded read: a real cycle's own `TOP_K_BY_PHASE` caps a
    3-cycle window to well under 40 rows even at the richest phase
    (`FREE_AGENCY`'s `12`), so the caller (`dba2/services/media_service.py::
    run_media_selection_cycle`) fetches the whole window ONCE per cycle and
    ranks/truncates it in Python (`dba2.media.context.build_league_brief`),
    never a second, per-story query. A negative `start_cycle_index` (a
    league's first 1-3 cycles) simply matches nothing below `0` -- no
    special-casing needed, since `cycle_index` is never negative for a real
    row."""
    rows = await ex.fetch(
        f"""
        SELECT {_MEDIA_STORY_COLUMNS} FROM media_stories
        WHERE league_id = $1 AND season = $2
              AND cycle_index BETWEEN $3 AND $4
        ORDER BY cycle_index DESC, id DESC
        """,
        league_id,
        season,
        start_cycle_index,
        end_cycle_index,
    )
    return [_map_media_story(r) for r in rows]


async def list_media_stories_page(
    ex: Executor, *, league_id: int, season: int, voice: str | None, offset: int, limit: int
) -> list[MediaStory]:
    """`/wire`'s pager read (docs/design/media-v2-newsroom.md MV-7, build
    step 7) -- **newest-cycle-first** (`ORDER BY cycle_index DESC, id
    DESC`), the reverse of `list_media_stories_for_league_season`'s oldest-
    first order: the pager's page 1 is the most recent story, matching
    `/wire`'s own pre-pager convention (its first reply was always
    `panels[-1]`, the newest). `voice`, when given, narrows to one persona
    key -- the SQL half of `beat` (`None` shows every persona, unfiltered).
    `offset`/`limit` are the caller's own page math (`view_service.get_
    wire_page`'s `page_size + 1` probe, LD2-4's own "one extra row tells you
    has_more" shape) -- this function does no clamping of its own, matching
    every other paged read in this module."""
    if voice is None:
        rows = await ex.fetch(
            f"""
            SELECT {_MEDIA_STORY_COLUMNS} FROM media_stories
            WHERE league_id = $1 AND season = $2
            ORDER BY cycle_index DESC, id DESC
            OFFSET $3 LIMIT $4
            """,
            league_id,
            season,
            offset,
            limit,
        )
    else:
        rows = await ex.fetch(
            f"""
            SELECT {_MEDIA_STORY_COLUMNS} FROM media_stories
            WHERE league_id = $1 AND season = $2 AND voice = $3
            ORDER BY cycle_index DESC, id DESC
            OFFSET $4 LIMIT $5
            """,
            league_id,
            season,
            voice,
            offset,
            limit,
        )
    return [_map_media_story(r) for r in rows]


async def update_media_story_text(
    ex: Executor,
    story_id: int,
    *,
    headline: str,
    rendered_text: str,
    writer_backend: str,
) -> MediaStory:
    """The AI rewrite pass's one write path (docs/design/media-v2-step8-
    anthropic-writer.md S8-13). Touches EXACTLY three columns -- `template_id`,
    `slot_bindings`, `fact_package`, `salience`, `format_tier`, `voice`, both
    subject ids and `league_brief` are never written here, which is what makes
    the determinism test expressible as "every column but three is
    byte-identical". Also guarded on `posted_at IS NULL`: a story already on
    the wire is never rewritten under its readers. Raises `LookupError` when
    no row matched (unknown id, or already posted) so the caller cannot
    mistake a no-op for a success."""
    row = await ex.fetchrow(
        f"""
        UPDATE media_stories
           SET headline = $2, rendered_text = $3, writer_backend = $4
         WHERE id = $1 AND posted_at IS NULL
        RETURNING {_MEDIA_STORY_COLUMNS}
        """,
        story_id,
        headline,
        rendered_text,
        writer_backend,
    )
    if row is None:
        raise LookupError(f"media story {story_id} not found or already posted")
    return _map_media_story(row)


async def get_subject_last_seen_cycles(
    ex: Executor, *, league_id: int, season: int
) -> list[tuple[int | None, int | None, int]]:
    """MV-10's own real novelty signal, the read half: every distinct
    `(subject_player_id, subject_team_id)` pair this league-season's own
    `media_stories` carries, with the highest `cycle_index` either was
    published at. Returns raw `(player_id, team_id, last_cycle)` rows,
    never a `(subject_kind, subject_id)`-keyed mapping -- this repo module
    may not import `dba2.services.media_service` (Decision P4-31's own
    layering direction), so `_subject_identity`'s "player preferred over
    team" collapsing rule is the CALLER's job, one layer up. A row with
    both columns `NULL` (a `MATCHUP`-subject story, or a subject whose row
    was later purged by `ON DELETE SET NULL`) is still returned -- the
    caller decides whether an untrackable subject contributes anything,
    not this query."""
    rows = await ex.fetch(
        """
        SELECT subject_player_id, subject_team_id, MAX(cycle_index) AS last_cycle
        FROM media_stories
        WHERE league_id = $1 AND season = $2
        GROUP BY subject_player_id, subject_team_id
        """,
        league_id,
        season,
    )
    return [(r["subject_player_id"], r["subject_team_id"], r["last_cycle"]) for r in rows]


async def list_unposted_media_stories(
    ex: Executor, *, league_id: int, season: int, cycle_index: int | None = None
) -> list[MediaStory]:
    """The stories for one league+season that no `#league-wire` post has been
    recorded for (`posted_at IS NULL`), oldest cycle first then insertion order --
    the wire's PUBLICATION read (phase-5-stage-e-advance.md P5-99.s.3), as
    `list_media_stories_for_league_season` is its whole-season read. `cycle_index`
    narrows to one cycle. History cannot re-post from this read because history is
    marked."""
    if cycle_index is None:
        rows = await ex.fetch(
            f"""
            SELECT {_MEDIA_STORY_COLUMNS} FROM media_stories
            WHERE league_id = $1 AND season = $2 AND posted_at IS NULL
            ORDER BY cycle_index, id
            """,
            league_id,
            season,
        )
    else:
        rows = await ex.fetch(
            f"""
            SELECT {_MEDIA_STORY_COLUMNS} FROM media_stories
            WHERE league_id = $1 AND season = $2 AND cycle_index = $3
                  AND posted_at IS NULL
            ORDER BY id
            """,
            league_id,
            season,
            cycle_index,
        )
    return [_map_media_story(r) for r in rows]


async def mark_media_story_posted(ex: Executor, story_id: int) -> bool:
    """Records that this story's `#league-wire` message went out, and reports whether
    it moved the column. **Fenced by `posted_at IS NULL`**: the first mark wins and
    keeps its timestamp; a second (a startup sweep racing a press) is a zero-row
    no-op reporting `False`. One story per call by design -- marking a whole cycle
    would lose the remainder of a publication that was interrupted after story k
    (P5-99.s.1)."""
    row = await ex.fetchrow(
        "UPDATE media_stories SET posted_at = NOW() WHERE id = $1 AND posted_at IS NULL "
        "RETURNING id",
        story_id,
    )
    return row is not None


# ---------------------------------------------------------------------------
# media_pulse_snapshot
# ---------------------------------------------------------------------------

_PULSE_COLUMNS = (
    "league_id, season, cycle_index, team_id, wins, losses, standings_position, streak_length"
)


def _map_pulse(r: asyncpg.Record) -> MediaPulseSnapshot:
    return MediaPulseSnapshot(
        league_id=r["league_id"],
        season=r["season"],
        cycle_index=r["cycle_index"],
        team_id=r["team_id"],
        wins=r["wins"],
        losses=r["losses"],
        standings_position=r["standings_position"],
        streak_length=r["streak_length"],
    )


async def get_pulse_snapshot_teams(
    ex: Executor, *, league_id: int, season: int, cycle_index: int
) -> list[MediaPulseSnapshot]:
    """Every team's row for exactly one cycle -- one leading-column scan of
    the primary key, no separate index needed (see migration's own
    docstring)."""
    rows = await ex.fetch(
        f"""
        SELECT {_PULSE_COLUMNS} FROM media_pulse_snapshot
        WHERE league_id = $1 AND season = $2 AND cycle_index = $3
        """,
        league_id,
        season,
        cycle_index,
    )
    return [_map_pulse(r) for r in rows]


async def get_prior_pulse_snapshot_teams(
    ex: Executor, *, league_id: int, season: int, before_cycle_index: int
) -> list[MediaPulseSnapshot]:
    """Every team's row for the most recent PERSISTED cycle strictly before
    `before_cycle_index` -- `[]` if none exists yet (the league's first
    pulse cycle ever, Decision P4-28's own "absent is neutral, never zero"
    case). **Deliberately not `cycle_index - 1`** -- Decision P4-28's "a
    missed cycle widens the window, never corrupts it": if cycle N-1 was
    never persisted (a skipped cycle, or this is the caller's first call
    after step 19 lands mid-season), reading the MOST RECENT real prior
    cycle instead of a literal `N-1` is what gives `dba2/media/pulse.py::
    diff_pulse_snapshots` the wider-but-correct diff that property
    describes, rather than silently reading a real gap as "no snapshot at
    all" the moment a caller assumes contiguous cycle numbering. One round
    trip: the `MAX(cycle_index)` subquery is restricted to the same three
    leading PK columns as the outer query, so this is still a single
    index-scan-shaped read, not a second query the caller has to issue."""
    rows = await ex.fetch(
        f"""
        SELECT {_PULSE_COLUMNS} FROM media_pulse_snapshot
        WHERE league_id = $1 AND season = $2 AND cycle_index = (
            SELECT MAX(cycle_index) FROM media_pulse_snapshot
            WHERE league_id = $1 AND season = $2 AND cycle_index < $3
        )
        """,
        league_id,
        season,
        before_cycle_index,
    )
    return [_map_pulse(r) for r in rows]


async def upsert_pulse_snapshot_teams(
    ex: Executor, rows: list[MediaPulseSnapshot]
) -> list[MediaPulseSnapshot]:
    """Bulk upsert via `UNNEST` + `ON CONFLICT DO UPDATE`, mirroring
    `head_to_head_repo.upsert_head_to_head_for_league` exactly -- one round
    trip for however many team rows one cycle's snapshot has (up to a
    league's full team count), not one query per team. See module
    docstring for why this table gets a bulk writer and `media_stories`
    does not."""
    if not rows:
        return []
    result = await ex.fetch(
        f"""
        INSERT INTO media_pulse_snapshot ({_PULSE_COLUMNS})
        SELECT * FROM UNNEST(
            $1::int[], $2::int[], $3::int[], $4::int[],
            $5::int[], $6::int[], $7::int[], $8::int[]
        )
        ON CONFLICT (league_id, season, cycle_index, team_id) DO UPDATE SET
            wins = EXCLUDED.wins,
            losses = EXCLUDED.losses,
            standings_position = EXCLUDED.standings_position,
            streak_length = EXCLUDED.streak_length
        RETURNING {_PULSE_COLUMNS}
        """,
        [r.league_id for r in rows],
        [r.season for r in rows],
        [r.cycle_index for r in rows],
        [r.team_id for r in rows],
        [r.wins for r in rows],
        [r.losses for r in rows],
        [r.standings_position for r in rows],
        [r.streak_length for r in rows],
    )
    return [_map_pulse(r) for r in result]


# ---------------------------------------------------------------------------
# media_writers
# ---------------------------------------------------------------------------

_MEDIA_WRITER_COLUMNS = "id, league_id, team_id, persona, name, created_at"


def _map_media_writer(r: asyncpg.Record) -> MediaWriter:
    return MediaWriter(
        id=r["id"],
        league_id=r["league_id"],
        team_id=r["team_id"],
        persona=r["persona"],
        name=r["name"],
        created_at=r["created_at"],
    )


async def get_media_writer(
    ex: Executor, *, league_id: int, team_id: int | None, persona: str
) -> MediaWriter | None:
    """`team_id=None` reads the league-wide row (`... WHERE team_id IS
    NULL ...`) -- a plain `team_id = $2` predicate would never match a NULL
    column regardless of what `$2` is bound to, so the two cases need two
    real query shapes, not one parameterized on a value that could itself
    be `NULL`. This is the read half of the get-or-create pair `dba2/
    services/media_service.py::get_or_create_media_writer` orchestrates --
    RNG-driven name generation is a services-layer concern (it needs `dba2.
    core.rng.rng_for`), so it is not built here."""
    if team_id is None:
        row = await ex.fetchrow(
            f"""
            SELECT {_MEDIA_WRITER_COLUMNS} FROM media_writers
            WHERE league_id = $1 AND team_id IS NULL AND persona = $2
            """,
            league_id,
            persona,
        )
    else:
        row = await ex.fetchrow(
            f"""
            SELECT {_MEDIA_WRITER_COLUMNS} FROM media_writers
            WHERE league_id = $1 AND team_id = $2 AND persona = $3
            """,
            league_id,
            team_id,
            persona,
        )
    return _map_media_writer(row) if row is not None else None


async def create_media_writer(
    ex: Executor, *, league_id: int, team_id: int | None, persona: str, name: str
) -> MediaWriter:
    """The create half of the get-or-create pair -- a plain `INSERT`, no
    `ON CONFLICT`. A caller racing another writer for the same `(league_id,
    team_id, persona)` triple hits migration 011's own partial unique index
    (`ux_media_writers_team`/`ux_media_writers_league`) and gets a real
    `asyncpg.UniqueViolationError`; `dba2/services/media_service.py::get_
    or_create_media_writer` is the layer that catches it and re-reads,
    matching this schema's existing convention of letting a real constraint
    violation raise here rather than silently absorbing it with `ON
    CONFLICT DO NOTHING` (`award_repo.create_award`'s own documented
    choice, for the identical reason: a caller bug should raise, not be
    quietly reshaped)."""
    row = await ex.fetchrow(
        f"""
        INSERT INTO media_writers (league_id, team_id, persona, name)
        VALUES ($1, $2, $3, $4)
        RETURNING {_MEDIA_WRITER_COLUMNS}
        """,
        league_id,
        team_id,
        persona,
        name,
    )
    return _map_media_writer(row)


async def list_media_writers_for_league(ex: Executor, league_id: int) -> list[MediaWriter]:
    rows = await ex.fetch(
        f"SELECT {_MEDIA_WRITER_COLUMNS} FROM media_writers WHERE league_id = $1",
        league_id,
    )
    return [_map_media_writer(r) for r in rows]


async def update_media_writer_name(ex: Executor, writer_id: int, name: str) -> MediaWriter:
    """The single-statement writer-by-primary-key half of docs/design/
    media-v2-newsroom.md MV-3b.2: `get_or_create_media_writer`'s third
    branch (a hit whose stored `name` differs from the library's own
    `display_name`) calls this rather than issuing a second `INSERT` --
    `media_stories.writer_id` is a foreign key to THIS ROW, not to the
    name, so every already-published story's byline updates with it. No
    `WHERE name <> ...` guard here (unlike the bulk, persona-scoped form
    MV-3b.2 also specifies, not built in this step) -- this form is always
    called from a branch that already confirmed the names differ."""
    row = await ex.fetchrow(
        f"""
        UPDATE media_writers SET name = $2 WHERE id = $1
        RETURNING {_MEDIA_WRITER_COLUMNS}
        """,
        writer_id,
        name,
    )
    return _map_media_writer(row)


async def update_media_writer_names_for_persona(
    ex: Executor, *, league_id: int, persona: str, name: str
) -> int:
    """docs/design/media-v2-newsroom.md MV-3b.2 (revision 5) -- the bulk,
    persona-scoped form `get_or_create_media_writer`'s third branch calls
    for a TEAM-scoped persona (`beat_writer`): one `UPDATE ... WHERE
    league_id = $1 AND persona = $2 AND name <> $3`, renaming every one of
    that persona's rows in one statement rather than reconciling them one
    team at a time as each is next written about (which MV-3b.2's own
    revision-5 text measures at "most of a season, not one cycle" for 30
    rows). The `name <> $3` predicate makes a repeat call with the same
    `name` a real zero-row update -- idempotent, safe to call on every
    reconciling draw, not just the first. Returns the number of rows
    actually renamed (`0` on a no-op call), for a caller that wants to
    observe whether anything happened; no caller reads it today."""
    tag = await ex.execute(
        "UPDATE media_writers SET name = $3 WHERE league_id = $1 AND persona = $2 AND name <> $3",
        league_id,
        persona,
        name,
    )
    # `dba2/data/repositories/broadcast_repo.py::_affected`'s own already-
    # shipped "parse asyncpg's own command tag" shape -- e.g. "UPDATE 3".
    return int(tag.rsplit(" ", 1)[-1])


# ---------------------------------------------------------------------------
# docs/design/media-v2-newsroom.md MV-2h -- the team-profile composer's own
# two reads. Neither adds a column or a table: both derive their answer
# from rows `media_stories`/`media_pulse_snapshot` already carry.
# ---------------------------------------------------------------------------


_TEAM_PROFILE_STORY_KIND = "team_profile"
"""The literal `story_kind` value `TEAM_PROFILE` will be once `StoryKind`
gains it (docs/design/media-v2-newsroom.md MV-2h, a later build step --
steps 1-3a add no new `StoryKind` member, per the "zero new kinds" pure-
refactor scope this build is held to). `story_kind` is a `TEXT` column
with no DB `CHECK` (this schema's existing convention, `media_repo.py`'s
own module docstring), so querying against the string a future enum member
will hold is safe today and becomes `StoryKind.TEAM_PROFILE.value` as a
one-line swap the moment that member exists -- named as a module constant
here specifically so that future edit has exactly one line to change."""


async def list_profiled_team_ids(ex: Executor, *, league_id: int, season: int) -> set[int]:
    """MV-2h.1: a team already profiled this season IS a `TEAM_PROFILE`
    row naming it as subject -- derived from `media_stories` rather than a
    new table, so it cannot drift from what is actually on the wire (`the
    wire IS the record`, MV-2h.1's own framing). Uses `ix_media_stories_
    league_season_cycle`'s two leading columns (`league_id`, `season`); no
    new index (MV-2h.1's own cost note: a `story_kind` index would cost
    writes to save microseconds on a hundred-row-per-season filter).
    Returns the empty set today, on every real league -- no detector emits
    a `team_profile` package until a later build step, and this function
    has nothing else to read; it is built now because the repo-read layer
    (step 2) is scoped ahead of the detector layer (step 4) by design
    (docs/design/media-v2-newsroom.md "Build order")."""
    rows = await ex.fetch(
        """
        SELECT DISTINCT subject_team_id FROM media_stories
        WHERE league_id = $1 AND season = $2 AND story_kind = $3
              AND subject_team_id IS NOT NULL
        """,
        league_id,
        season,
        _TEAM_PROFILE_STORY_KIND,
    )
    return {r["subject_team_id"] for r in rows}


async def get_final_pulse_snapshot_for_season(
    ex: Executor, *, league_id: int, season: int
) -> list[MediaPulseSnapshot]:
    """MV-2h.4: the highest-`cycle_index` row of a PRIOR season, one team
    per row -- the "last year" profile facts read this. Same `MAX(cycle_
    index)` subquery shape `get_prior_pulse_snapshot_teams` above already
    uses, without that function's `< $3` bound (there is no "before" cycle
    to exclude; this wants the season's own last one). Returns `[]` when
    the season has no snapshot at all (a league older than the media
    cycle, or one that has not reached that season yet) -- the composer
    then emits no "last year" facts rather than a fabricated `0-0`, the
    same "absent is neutral" rule `get_prior_pulse_snapshot_teams` already
    follows."""
    rows = await ex.fetch(
        f"""
        SELECT {_PULSE_COLUMNS} FROM media_pulse_snapshot
        WHERE league_id = $1 AND season = $2 AND cycle_index = (
            SELECT MAX(cycle_index) FROM media_pulse_snapshot
            WHERE league_id = $1 AND season = $2
        )
        """,
        league_id,
        season,
    )
    return [_map_pulse(r) for r in rows]
