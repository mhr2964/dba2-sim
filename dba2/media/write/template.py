"""`dba2/media/write/template.py` -- docs/design/media-v2-newsroom.md MV-6,
Implementation A: `TemplateWriter`, the `WriterBackend` (`dba2.domain.
media_writer`) built from the existing template-selection machinery
(`dba2.media.select`), generalized to the package's own `Fact.key` set.

**One real, disclosed simplification against MV-6's own "a headline
template bank ... selected through the existing engine.select_template
core" text.** This build step generates the headline PROGRAMMATICALLY
(`_generate_headline` below), not from a second, JSON-authored template
bank selected through `engine.select_template`. A real second bank needs
its own `LoadedMediaLibrary` field, its own fallback-coverage validation
per `StoryKind`, and a ripple into every function that already constructs
or transforms a `LoadedMediaLibrary` (`with_voices_disabled`, every
hand-built test library) -- real, novel authoring and validation work
disproportionate to what a headline (one short, derived sentence) needs
compared to a body paragraph (the actual variety this design cares about).
The BODY half of this module is fully faithful to MV-6's own text: it
reuses `select.select_media_template`/`select.bind_slots` unchanged,
against the real, already-validated `templates` bank, with the real
`(voice, cycle)` repeat-penalty scope. Recorded here, not silently
substituted, so a later build step can replace `_generate_headline` with a
real bank without this module's own public shape changing at all
(`write()`'s signature is fixed by `WriterBackend`, so nothing outside
this file needs to change either way).

**`TemplateWriter` is stateful across a cycle, by construction, because
the repeat-penalty scope it reuses IS stateful** (MV-6's own text: "the
existing ... repeat-penalty scope", which `dba2/services/media_service.py::
run_media_selection_cycle`'s own `running_use_counts` dict already proves
needs to advance across every candidate in one cycle). A caller constructs
one `TemplateWriter` per cycle, seeded from that cycle's own starting
`use_counts`, and calls `write()` once per candidate in sequence -- the
identical "one mutable structure per cycle, advanced after every draw"
shape `run_media_selection_cycle` already uses for its own inline
selection, now behind the `WriterBackend` protocol instead of it.

Zero I/O, the same purity rules every other module under `dba2/media/`
carries: no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. `random.Random` is only ever RECEIVED (via
`write()`'s own `rng` parameter), never constructed here. Imports only
`dba2.domain.media_facts`, `dba2.domain.media_writer`, `dba2.media.select`,
`dba2.media.write.budget` and stdlib.
"""

from __future__ import annotations

import random

from dba2.domain.media_context import LeagueBrief
from dba2.domain.media_facts import StoryPackage, SubjectKind, opponent_team_name_is_game_opponent
from dba2.domain.media_writer import FormatTier, PersonaVoice, WrittenStory
from dba2.media import select
from dba2.media.write.budget import (
    apply_headline_style,
    budget_for,
    build_fact_box,
    clamp_text,
    headline_label_for,
)


def _generate_headline(package: StoryPackage, persona: PersonaVoice) -> str:
    """Pure, deterministic, no RNG -- a headline derived entirely from the
    package's own `story_kind` and subject name(s), styled by `persona.
    register` (`budget.apply_headline_style`) at the call site, not here
    (this function returns the plain sentence; the caller applies style and
    the tier's character budget, the same separation `budget.py` already
    keeps between "what the text says" and "how long it may be").

    **Two real content bugs fixed here (critic round on commits
    `ea84c5f`..`665526d`, bug B4).** (1) `label` used to be the raw `Story
    Kind.value.replace("_", " ")` -- every headline read `trade broken
    promise`, `standings collapse`, etc., the exact snake-case-enum-leak
    this design's own As-built note already disclosed refusing to ship for
    `offensive_scheme`; `budget.headline_label_for` (a real, hand-written,
    exhaustively-tested label bank) replaces it. (2) `subject` used to
    prefer `team_name` over `player_name` unconditionally, so a `PLAYER`-
    subject story (e.g. `TRADE_BROKEN_PROMISE`) headlined with a team
    instead of the actual subject; `package.subject.kind` now picks
    whichever field the package's own `Subject` actually names as primary.

    **The "X vs. Y" framing is now gated by `opponent_team_name_is_game_
    opponent`, not merely by `opponent_team_name` being present.** Before
    this fix, `TRADE_BROKEN_PROMISE` (whose `opponent_team_name` is a
    player's FORMER team, never a game opponent) rendered a real, wrong
    "X vs. Y" headline; only a kind whose `opponent_team_name` fact really
    means a matchup opponent (`TRADE_EXECUTED`, `RIVALRY_MATCHUP`,
    `REVENGE_GAME`) gets that framing now."""
    label = headline_label_for(package.story_kind)
    if package.subject.kind is SubjectKind.PLAYER:
        subject = package.text("player_name") or package.text("team_name")
    else:
        subject = package.text("team_name") or package.text("player_name")
    opponent = package.text("opponent_team_name")
    if (
        subject is not None
        and opponent is not None
        and opponent_team_name_is_game_opponent(package.story_kind)
    ):
        return f"{subject} vs. {opponent}: {label}"
    if subject is not None:
        return f"{subject}: {label}"
    return label.capitalize()


class TemplateWriter:
    """MV-6's `WriterBackend` structural implementation -- `name = "template"`
    is read by `dba2.domain.media_writer.WriterBackend`'s own protocol and
    is recorded on `media_stories.writer_backend` (build step 7, MV-9's
    `writer_backend` column)."""

    name = "template"

    def __init__(
        self,
        library,
        *,
        use_counts: dict[str, dict[str, int]] | None = None,
    ) -> None:
        """`library` is a `dba2.services.media_service.LoadedMediaLibrary`
        (not type-annotated here to avoid this module importing `dba2.
        services` at all, which the layering valve forbids regardless of
        annotation-only usage -- `from __future__ import annotations` alone
        does not exempt a real top-level `import`). `use_counts` is the
        caller's own running `{voice: {template_id: count}}` structure
        (`run_media_selection_cycle`'s own shape) -- `None` starts a fresh
        one; a caller resuming mid-cycle passes its own dict and this
        writer mutates it in place, exactly as `run_media_selection_cycle`
        already does for its own inline selection. Passing the SAME dict a
        caller keeps across candidates (rather than a copy) is what makes
        that in-place mutation visible outside this class -- build step 7's
        `select_and_bind_story` relies on this to keep advancing the exact
        `running_use_counts` mapping `run_media_selection_cycle` already
        owns, with no second, parallel counting mechanism."""
        self._library = library
        self._use_counts: dict[str, dict[str, int]] = use_counts if use_counts is not None else {}
        self.last_template_id: str | None = None
        self.last_bindings: dict[str, str] = {}
        """The most recent `write()` call's own template draw and resolved
        slot values (build step 7) -- `WrittenStory` itself deliberately
        carries neither (MV-6's own protocol is backend-agnostic; an LLM
        backend has no `template_id`), but `media_stories.template_id`/
        `.slot_bindings` are real, `NOT NULL` columns (migration 011) that
        predate this writer seam and still need a value for every row this
        backend produces. Exposed here, on the CONCRETE class, not the
        `WriterBackend` `Protocol` -- a caller that only knows the protocol
        (a future `AnthropicWriter` included) has no reason to read these;
        `select_and_bind_story` reads them only because it already knows,
        by construction, which concrete backend it is holding this build
        step (there is only one). Overwritten, never accumulated -- a
        caller must read them immediately after `await write(...)` returns,
        before the next call; safe because `run_media_selection_cycle`'s own
        docstring already establishes its candidate loop is sequential, not
        concurrent, for an unrelated reason (no two candidates' writer
        creation may interleave)."""

    async def write(
        self,
        package: StoryPackage,
        *,
        persona: PersonaVoice,
        tier: FormatTier,
        rng: random.Random,
        brief: LeagueBrief | None = None,
    ) -> WrittenStory:
        """**`brief` is accepted and ignored, on purpose** (docs/design/
        media-v2-newsroom.md MV-12.3/MV-14.2) -- this is the "true swap-in
        test" step 8 needs: a `TemplateWriter` handed a real `LeagueBrief`
        renders byte-identically to one handed `None`, because a slot-
        filling writer has no binding site for free-form context
        (`bind_slots` resolves `template.slots` against `package.facts`'
        own `Fact.key` set only). Making that usable would mean promoting
        brief content into named `FACT_KEYS_BY_KIND` entries and authoring
        matching templates per kind per persona -- a real library-authoring
        project, deferred (MV-14.2), not a defect here."""
        voice_use_counts = self._use_counts.setdefault(persona.key, {})
        template = select.select_media_template(
            package, self._library.templates, voice=persona.key, tier=tier,
            fallback_by_kind=self._library.fallback_by_kind, use_counts=voice_use_counts,
            repeat_penalty=self._library.repeat_penalty, rng=rng,
        )
        bindings = select.bind_slots(template, package)
        rendered = template.text.format(**bindings)
        rendered = rendered[:1].upper() + rendered[1:]
        voice_use_counts[template.id] = voice_use_counts.get(template.id, 0) + 1
        self.last_template_id = template.id
        self.last_bindings = dict(bindings)

        budget = budget_for(tier, package.story_kind)
        body = clamp_text(rendered, budget.description_max, field_name="body")

        headline_text = _generate_headline(package, persona)
        headline_text = apply_headline_style(headline_text, persona.register)
        headline = clamp_text(headline_text, budget.title_max, field_name="headline")

        fact_box = (
            build_fact_box(package, budget.field_count) if tier is FormatTier.ARTICLE else ()
        )

        return WrittenStory(headline=headline, body=body, fact_box=fact_box, backend=self.name)
