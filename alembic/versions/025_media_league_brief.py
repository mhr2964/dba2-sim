"""media_v2 step 7.5b -- `media_stories.league_brief`

Revision ID: 025
Revises: 024
Create Date: 2026-09-23

docs/design/media-v2-newsroom.md MV-12.3 ("Where the brief enters the
writer, and the persistence fork"), MV-14.3, "Build order" step 7.5b.

**Option A, chosen over re-deriving at rewrite time (Option B) or a
normalized `media_cycle_briefs` table (Option C) -- MV-12.3's own
reasoning.** MV-6's own "the LLM rewrites `headline`/`rendered_text` only,
POST-COMMIT" design means a future `AnthropicWriter` (step 8) rewrite call
runs after this row is already durably persisted with a template body, by
which point league state has moved on -- §2 (this cycle's other
candidates) is not reconstructible at all once the cycle is over, and §1/
§3 would read a league that has since advanced (Option B, rejected).
Mirroring `fact_package`'s own precedent exactly (persist the prompt
payload verbatim so a later prompt and a later re-render both read what
the story was actually built from) costs one defaulted column and no join;
a normalized table (Option C) would be correct too, but the duplication
Option A accepts is measured and trivial (MV-12.3: "roughly 2-4 KB per row,
~50 KB per cycle, under half a megabyte per season").

Additive and defaulted, so no existing row and no existing construction
site breaks:

- `league_brief JSONB NOT NULL DEFAULT '{}'` -- `LeagueBrief.to_prompt_
  json()` (`dba2.domain.media_context`), persisted verbatim. Every row
  persisted before this migration, and every row `select_and_bind_story`
  writes without a real `CycleBriefInputs` available (a pre-trade-deadline
  cycle -- MV-11's own disclosed `NotFoundError` fallback leaves
  `league_snapshot`/`gm_context` `None` for that one cycle -- or any caller/
  test that predates step 7.5b), backfills to `'{}'`: there is no lossless
  way to recover a brief after the fact, and an empty object is honest
  about that rather than fabricating one.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "025"
down_revision: str | None = "024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE media_stories ADD COLUMN league_brief JSONB NOT NULL DEFAULT '{}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE media_stories DROP COLUMN league_brief")
