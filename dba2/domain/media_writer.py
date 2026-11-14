"""The AI-writer seam's protocol types (docs/design/media-v2-newsroom.md
MV-6) -- `FormatTier`, `WrittenStory`, `PersonaVoice`, and the
`WriterBackend` `Protocol` every backend (`TemplateWriter` today, a future
`AnthropicWriter`) implements identically.

**Why `FormatTier` lands in this build step (1) even though MV-4/MV-6's own
tier-selection logic and the writer backends themselves are later build
steps.** `dba2/media/select.py`'s retyped `select_media_template` gains a
`tier: FormatTier` keyword in THIS build step (revision 5, MV-1c.2 --
revision 4 added the `tiers` template dimension with no parameter path for
it), so the type has to exist before that signature can be written. The
rest of this module (`WrittenStory`/`PersonaVoice`/`WriterBackend`) has no
reader yet in this build step; declaring the full module now rather than
splitting `FormatTier` out into its own file keeps the file's own eventual
shape (MV-9's `dba2/domain/media_writer.py` row) as one piece rather than
two, at zero risk -- every one of these is a closed dataclass/`Protocol`
with no I/O and no dependency on anything the detector/writer layers have
not built yet.

Zero I/O, the same purity rules every other `domain/` module carries.
Imports only `dba2.domain.media` (`Register`), `dba2.domain.media_facts`
(`StoryPackage`) and stdlib.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from dba2.domain.media import Register
from dba2.domain.media_context import LeagueBrief
from dba2.domain.media_facts import StoryPackage


class FormatTier(StrEnum):
    """Two tiers, plus a byline -- not three tiers (MV-4: "a tier is a
    rendering budget; a column is an identity"). `BLURB` is the default
    shape for a thin week; `ARTICLE` is reserved for a package that clears
    both the salience threshold and a minimum fact count (MV-4's own
    deterministic rule, a later build step)."""

    BLURB = "blurb"
    ARTICLE = "article"


@dataclass(frozen=True)
class WrittenStory:
    """One writer backend's output for one package (MV-6) -- `fact_box` is
    `((label, value_text), ...)`, populated only for `ARTICLE` (MV-4's
    per-kind fact-box allowance); `backend` is `"template"` or
    `"anthropic:<model>"`, recorded on the persisted story
    (`media_stories.writer_backend`, a later build step) so a future reader
    can tell which rows an LLM wrote."""

    headline: str
    body: str
    fact_box: tuple[tuple[str, str], ...]
    backend: str


@dataclass(frozen=True)
class PersonaVoice:
    """One persona's voice, read by BOTH writer backends (MV-6, fields
    spelled out in MV-3h) -- `register` is what lets `TemplateWriter` pick
    a headline-style bank and a future `AnthropicWriter` render the
    identical voice into a persona card, without either backend knowing
    about the other's own rendering mechanism."""

    key: str
    display_name: str
    column: str
    beat_description: str
    register: Register
    voice_rules: tuple[str, ...]
    fences: tuple[str, ...]


class WriterBackend(Protocol):
    """MV-6's protocol -- `async` for both implementations (the template
    one awaits nothing) so there is ONE protocol, not two. `dba2/media/
    write/template.py::TemplateWriter` and the future `dba2/services/
    media_writer_llm.py::AnthropicWriter` both satisfy this structurally;
    neither needs to import the other or this module's own `Protocol` to
    do so (structural typing), matching `dba2/managers/voice/engine.py`'s
    own "a caller passing an object that merely has the right attributes is
    satisfied structurally" convention.

    **`brief` (docs/design/media-v2-newsroom.md MV-12.3, build step 7.5b)
    -- additive, defaulted, per-call.** Considered and rejected: a
    constructor argument on the concrete backend. §5 of a `LeagueBrief` is
    genuinely per-story (MV-12.1), which a per-cycle-constructed backend
    instance cannot represent without becoming per-story itself; a
    defaulted `write()` keyword keeps every existing caller and both
    implementations' pre-MV-12 signatures working unchanged.
    `TemplateWriter` accepts and ignores it (MV-14.2: a slot-filling writer
    has no binding site for free-form context); a future `AnthropicWriter`
    (step 8) is this parameter's one real consumer."""

    name: str

    async def write(
        self,
        package: StoryPackage,
        *,
        persona: PersonaVoice,
        tier: FormatTier,
        rng: random.Random,
        brief: LeagueBrief | None = None,
    ) -> WrittenStory: ...
