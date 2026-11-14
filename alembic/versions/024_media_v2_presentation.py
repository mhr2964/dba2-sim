"""media_v2 step 7 -- the four `media_stories` columns MV-7 always described
as "migration 021"

Revision ID: 024
Revises: 023
Create Date: 2026-09-23

docs/design/media-v2-newsroom.md MV-7 ("Publishing surface"), MV-9's own
`alembic/versions/021_media_v2.py` row, "Build order" step 7.

**Disclosed deviation from MV-7's own text: this is migration `024`, not
`021`.** Migration `021`'s own docstring already disclosed this narrowing
when it shipped (build step 1): it added only the two `player_motivation_
events` columns and the one trade-asset index, explicitly deferring these
four `media_stories` columns to "the detector/writer work in later build
steps (MV-2/MV-6/MV-7, steps 4-7)" -- and steps 022/023 (`auto_pilot_and_
directive_enums`, `player_directive_enums`, a concurrent GM-command-surface
build) landed on this branch in between. Renumbering `021` in place was
never an option once later migrations existed on top of it (the same
already-applied-migration-content-drift argument `023`'s own docstring
makes about `022`); a new migration at the current head is the correct,
only-additive fix.

All four columns are additive and defaulted, so no existing row and no
existing construction site breaks:

- `headline TEXT` -- nullable. A `NULL` headline renders exactly as today,
  via `dba2/presentation/wire.py::_title_for(story.story_kind)` (MV-7).
  Every row `select_and_bind_story` persists from this migration forward
  carries a real one (`TemplateWriter.write().headline`); only pre-
  migration history reads `NULL` here.
- `format_tier TEXT NOT NULL DEFAULT 'blurb'` -- `dba2.domain.media_writer.
  FormatTier`'s value, stored as plain text (the same "closed vocabulary
  enforced in Python, stored as TEXT" convention `media_stories.story_kind`
  and `.voice` already use). Every pre-migration row backfills to
  `'blurb'`, which is what every one of them actually was in effect (MV-4's
  own step-5/6 disclosure: no candidate ever drew a real `ARTICLE` tier
  before this step wired `select_tier` in).
- `fact_package JSONB NOT NULL DEFAULT '{}'` -- `StoryPackage.to_prompt_
  json()` (MV-6's own prompt-contract payload), persisted verbatim so a
  future `AnthropicWriter` (step 8) prompt and a future re-render both read
  the SAME facts the story was actually built from, not a reconstruction.
  Pre-migration rows backfill to `'{}'` -- there is no lossless way to
  recover a package after the fact, and an empty object is honest about
  that rather than fabricating one.
- `writer_backend TEXT NOT NULL DEFAULT 'template'` -- `dba2.domain.
  media_writer.WrittenStory.backend` (`"template"` today; `"anthropic:
  <model>"` once step 8 lands). Every pre-migration row backfills to
  `'template'`, which is what every one of them actually was (the only
  backend that has ever existed).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "024"
down_revision: str | None = "023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE media_stories ADD COLUMN headline TEXT")
    op.execute(
        "ALTER TABLE media_stories ADD COLUMN format_tier TEXT NOT NULL DEFAULT 'blurb'"
    )
    op.execute(
        "ALTER TABLE media_stories ADD COLUMN fact_package JSONB NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE media_stories ADD COLUMN writer_backend TEXT NOT NULL DEFAULT 'template'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE media_stories DROP COLUMN writer_backend")
    op.execute("ALTER TABLE media_stories DROP COLUMN fact_package")
    op.execute("ALTER TABLE media_stories DROP COLUMN format_tier")
    op.execute("ALTER TABLE media_stories DROP COLUMN headline")
