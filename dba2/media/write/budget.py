"""`dba2/media/write/budget.py` -- docs/design/media-v2-newsroom.md MV-4:
tier selection (deterministic) and the per-tier rendering budgets that
apply a second time, at construction, on top of `presentation/panel.py`'s
own fail-fast `Panel.__post_init__` validation (Decision P5-4) -- so an
over-budget body is rejected here, before a `Panel` is ever built, not
merely caught there. Also MV-3h's own "`register` ... picks a headline-
style bank and its punctuation/casing rules in `budget.py`" -- the one
place both writer backends (MV-6) will eventually read that rule from.

**These are THIS DESIGN's own tighter editorial budgets, not
`panel.py`'s real ceilings** (MV-4's own revision-4 correction: the real
constants are `MAX_TITLE_CHARS = 256`, `MAX_DESCRIPTION_CHARS = 4096`,
`MAX_FIELDS = 25`, `MAX_FIELD_NAME_CHARS = 256`, `MAX_FIELD_VALUE_CHARS =
1024`, `MAX_FOOTER_TEXT_CHARS = 2048`, `MAX_AUTHOR_NAME_CHARS = 256`,
`MAX_TOTAL_CHARS = 6000`; every budget below sits well inside all of
them). `tests/unit/test_media_write_budget.py::test_every_budget_constant_
sits_inside_panels_own_real_limits` checks that relationship as a real,
asserted fact, not merely a comment that could silently go stale if either
file's numbers ever move.

Zero I/O, the same purity rules every other module under `dba2/media/`
carries: no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.media`, `dba2.domain.
media_writer` and stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass

from dba2.domain.media import Register, StoryKind
from dba2.domain.media_facts import StoryPackage
from dba2.domain.media_writer import FormatTier

ARTICLE_SALIENCE_THRESHOLD = 0.62
"""MV-4's own deterministic tier rule, verbatim: `ARTICLE` iff `scored.score
>= ARTICLE_SALIENCE_THRESHOLD` AND `len(package.facts) >= ARTICLE_MIN_
FACTS`; otherwise `BLURB`. Disclosed, not calibrated -- same status as
`salience.SALIENCE_FLOOR`."""

ARTICLE_MIN_FACTS = 4
TEAM_PROFILE_MIN_FACTS = 6
"""MV-4's own two amendments for `TEAM_PROFILE`: it is always `ARTICLE` or
not published at all (never a `BLURB`), and the rule is the fact count
alone (`>= 6`) -- its score was never asked to clear a floor (MV-3h.1: a
profile bypasses `above_floor`/`select_top_k` entirely), so thresholding it
at `ARTICLE_SALIENCE_THRESHOLD` would apply a bar it was deliberately
exempted from."""


def select_tier(
    score: float, fact_count: int, *, story_kind: StoryKind
) -> FormatTier | None:
    """MV-4's tier rule. Returns `None` for `story_kind is TEAM_PROFILE`
    below `TEAM_PROFILE_MIN_FACTS` -- the caller's own signal that the
    package must not be published at all (MV-2h.2's own "a team that
    cannot clear six facts is skipped and its quota slot goes unused,"
    MV-4's own "there is nothing worth publishing in the degraded case").
    Every other kind always returns a real tier -- there is no "do not
    publish" case for an event-driven story once it has cleared salience's
    own floor, only a choice of how much room it gets."""
    if story_kind is StoryKind.TEAM_PROFILE:
        return FormatTier.ARTICLE if fact_count >= TEAM_PROFILE_MIN_FACTS else None
    if score >= ARTICLE_SALIENCE_THRESHOLD and fact_count >= ARTICLE_MIN_FACTS:
        return FormatTier.ARTICLE
    return FormatTier.BLURB


_DEFAULT_FACT_BOX_FIELDS = 3
_FACT_BOX_FIELDS_BY_KIND: dict[StoryKind, int] = {StoryKind.TEAM_PROFILE: 5}
"""MV-4's own per-kind fact-box allowance -- a budget parameter, not an
identity (MV-4's own "a tier is a rendering budget, a column is an
identity" distinction, restated for this one exception): `TEAM_PROFILE`
genuinely has more to show (record, core, scheme, trades, next game) than
any other `ARTICLE`, so it gets 5 `PanelField`s instead of the default 3.
Every other kind reads the default -- there is no editorial reason (yet)
to vary it per kind, so none is invented."""


def fact_box_field_count(story_kind: StoryKind) -> int:
    return _FACT_BOX_FIELDS_BY_KIND.get(story_kind, _DEFAULT_FACT_BOX_FIELDS)


@dataclass(frozen=True)
class TierBudget:
    """One tier's own rendering budget -- MV-4's table, as data. `field_
    count` is resolved per-kind by `budget_for` below (`fact_box_field_
    count`), never a fixed constant on this dataclass, since it is the one
    field MV-4 says varies by kind rather than by tier."""

    title_max: int
    description_max: int
    author_max: int
    footer_max: int
    field_count: int
    field_name_max: int
    field_value_max: int


_BLURB_BUDGET = TierBudget(
    title_max=80, description_max=350, author_max=60, footer_max=60,
    field_count=0, field_name_max=0, field_value_max=0,
)
_ARTICLE_BUDGET = TierBudget(
    title_max=120, description_max=1400, author_max=60, footer_max=60,
    field_count=_DEFAULT_FACT_BOX_FIELDS, field_name_max=40, field_value_max=200,
)


def budget_for(tier: FormatTier, story_kind: StoryKind) -> TierBudget:
    """MV-4's own table, resolved for one `(tier, story_kind)` pair. Only
    `ARTICLE`'s `field_count` ever varies by kind (`BLURB` always carries
    zero fields, MV-4's own "0 fields" rule, no exception for any kind)."""
    if tier is FormatTier.BLURB:
        return _BLURB_BUDGET
    return TierBudget(
        title_max=_ARTICLE_BUDGET.title_max,
        description_max=_ARTICLE_BUDGET.description_max,
        author_max=_ARTICLE_BUDGET.author_max,
        footer_max=_ARTICLE_BUDGET.footer_max,
        field_count=fact_box_field_count(story_kind),
        field_name_max=_ARTICLE_BUDGET.field_name_max,
        field_value_max=_ARTICLE_BUDGET.field_value_max,
    )


def _truncate(text: str, max_chars: int) -> str:
    """A clean, word-boundary truncation with a trailing ellipsis when
    `text` overflows `max_chars` -- an editorial choice (a hard mid-word
    cut reads as broken, MV-4's own "over-budget article fails ... never
    in a production guild" concern extended to what a CLAMPED string looks
    like, not just whether one exists). Never returns a string longer than
    `max_chars`."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    ellipsis = "…"
    if max_chars <= len(ellipsis):
        return ellipsis[:max_chars]
    budget = max_chars - len(ellipsis)
    truncated = text[:budget]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip() + ellipsis


def clamp_text(text: str, max_chars: int, *, field_name: str) -> str:
    """The one clamp every field below shares -- raises `ValueError` for an
    unclampable (empty-after-strip) value (MV-4/MV-6's own "a pure clamp
    and a `ValueError` for an unclampable headline," generalized to every
    field this module budgets: an empty author or footer is exactly as
    wrong as an empty headline), truncates otherwise. Never raises for a
    non-empty value, however long -- truncation always succeeds once the
    empty case is ruled out."""
    stripped = text.strip()
    if not stripped:
        raise ValueError(f"cannot clamp an empty {field_name}")
    return _truncate(stripped, max_chars)


@dataclass(frozen=True)
class HeadlineStyle:
    """MV-3h's own "picks a headline-style bank and its punctuation/casing
    rules" -- the punctuation/casing half, made real. `allow_exclamation`
    is the one rule this design actually needs today: `hot_take`'s
    `INFLAMMATORY` register is the one voice permitted to end a headline on
    an exclamation point; every other register is declarative."""

    allow_exclamation: bool
    title_case: bool


HEADLINE_STYLE_BY_REGISTER: dict[Register, HeadlineStyle] = {
    Register.REPORTORIAL: HeadlineStyle(allow_exclamation=False, title_case=False),
    Register.MEASURED: HeadlineStyle(allow_exclamation=False, title_case=False),
    Register.NARRATIVE: HeadlineStyle(allow_exclamation=False, title_case=True),
    Register.INFLAMMATORY: HeadlineStyle(allow_exclamation=True, title_case=False),
}
"""Every `Register` member has an entry -- checked by a pure unit test over
the enum itself, the same exhaustiveness shape `salience.KIND_WEIGHTS`
already uses, for the identical reason: a future `Register` member added
with no matching style must fail loudly, never silently render with a
missing style. `NARRATIVE` (`historian`, "The Long View") is the one
register that title-cases its headline -- a narrative piece reads like a
feature byline, not a wire blurb."""


def apply_headline_style(text: str, register: Register) -> str:
    """Applies `HEADLINE_STYLE_BY_REGISTER[register]` to an already-rendered
    headline string -- pure text transformation, no re-rendering. A
    register that does not allow exclamation points has every `!` replaced
    with `.` (never simply stripped, which would leave a run-on sentence);
    `title_case` applies `str.title()` unconditionally when set."""
    style = HEADLINE_STYLE_BY_REGISTER[register]
    result = text
    if not style.allow_exclamation:
        result = result.replace("!", ".")
    if style.title_case:
        result = result.title()
    return result


STORY_KIND_HEADLINE_LABEL: dict[StoryKind, str] = {
    StoryKind.STANDINGS_SURGE: "surge",
    StoryKind.STANDINGS_COLLAPSE: "collapse",
    StoryKind.STREAK: "streak",
    StoryKind.TRADE_REACTION: "trade reaction",
    StoryKind.TRADE_FALLOUT: "trade fallout",
    StoryKind.SIGNING_REACTION: "signing",
    StoryKind.MILESTONE: "milestone",
    StoryKind.AWARD_RACE: "award race",
    StoryKind.PLAYER_UNREST: "unrest",
    StoryKind.HOLDOUT_WATCH: "holdout watch",
    StoryKind.ROOKIE_WATCH: "rookie watch",
    StoryKind.TRADE_EXECUTED: "trade",
    StoryKind.TRADE_BROKEN_PROMISE: "broken promise",
    StoryKind.REVENGE_GAME: "revenge game",
    StoryKind.HOT_STREAK: "hot streak",
    StoryKind.COLD_STRETCH: "cold stretch",
    StoryKind.DEFENSIVE_STANDOUT: "defensive standout",
    StoryKind.ROSTER_CONSTRUCTION: "roster construction",
    StoryKind.SCHEME_IDENTITY: "scheme",
    StoryKind.RIVALRY_MATCHUP: "rivalry",
    StoryKind.TRADE_MARKET_WATCH: "trade market",
    StoryKind.TEAM_PROFILE: "team profile",
}
"""`dba2/media/write/template.py::_generate_headline`'s own headline-label
bank (critic round on commits `ea84c5f`..`665526d`, bug B4.2) -- every
`StoryKind` member has a real, hand-written entry here, checked by a pure
exhaustiveness unit test over the enum itself (the same shape `HEADLINE_
STYLE_BY_REGISTER` above already uses), precisely so a future `StoryKind`
addition with no matching label fails loudly at test time rather than
silently falling through to `story_kind.value.replace("_", " ")`'s own
raw-enum-leak -- exactly the bug this design's own As-built note already
disclosed refusing to ship for `offensive_scheme` (a `TEXT` fact value,
not a headline) but shipped anyway for the headline itself, unnoticed,
until this critic round. Declared here (not in `dba2/domain/media.py`
alongside `StoryKind`) because it is this write layer's own editorial
phrasing choice, the identical reasoning `HEADLINE_STYLE_BY_REGISTER`
already follows for `Register`."""


def headline_label_for(story_kind: StoryKind) -> str:
    return STORY_KIND_HEADLINE_LABEL[story_kind]


FACT_BOX_EXCLUDED_KEYS = frozenset(
    {"season", "phase", "team_name", "player_name", "opponent_team_name"}
)
"""Keys a fact box never repeats -- universal facts (season/phase) carry no
editorial content of their own, and the identity facts already appear in
the headline/author line, so repeating them as a `PanelField` would waste
the tier's own small field budget on information the reader already has."""


def build_fact_box(package: StoryPackage, field_count: int) -> tuple[tuple[str, str], ...]:
    """Promoted out of `template.py` (docs/design/media-v2-step8-anthropic-writer.md
    S8-6) so `AnthropicWriter` can build the identical fact box without
    importing a private name across modules. The first `field_count`
    non-identity, non-universal facts, in the package's own `facts` order (a
    detector's own emission order, itself deterministic) -- never re-sorted,
    so two runs over the same package produce the same fact box. Each
    `(label, value_text)` pair is clamped
    to `budget.py`'s own field-name/field-value limits."""
    if field_count <= 0:
        return ()
    budget = budget_for(FormatTier.ARTICLE, package.story_kind)
    chosen = [f for f in package.facts if f.key not in FACT_BOX_EXCLUDED_KEYS][:field_count]
    return tuple(
        (
            clamp_text(fact.label, budget.field_name_max, field_name="fact box label"),
            clamp_text(fact.value_text, budget.field_value_max, field_name="fact box value"),
        )
        for fact in chosen
    )
