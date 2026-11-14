"""Pure prompt assembly for the AI writer
(docs/design/media-v2-step8-anthropic-writer.md S8-2 / S8-4 / S8-5).

**Why this lives under `domain/`.** `AnthropicWriter` lives in `services/`
and must call this; `dba2.media` may only be imported by `media_service.py`
(P4-31's one-way valve), and `domain/` is importable from everywhere.
`media_facts.py::to_prompt_json` is the precedent -- prompt-payload
serialization already lives here.

**Why the split between a persona card and a payload.** Prompt caching keys
on the request prefix, so everything stable across a cycle's calls (the
persona card, the fences, the output contract) is one byte-identical string
and everything per-story is a separate JSON object. Any change to the card's
byte layout or the payload's key order silently destroys cache hits, which
is why `tests/unit/test_media_prompt.py` asserts both against golden files.

Zero I/O, no randomness. Imports `dba2.domain` and stdlib only.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from dba2.domain.media_bindings import derived_facts
from dba2.domain.media_context import LeagueBrief
from dba2.domain.media_facts import StoryPackage
from dba2.domain.media_writer import FormatTier, PersonaVoice
from dba2.domain.phase import Phase

WRITE_INSTRUCTION = "Write today's item from the JSON on standard input."
"""The constant `-p` argument (S8-4 part 3) -- constant across every call so
it joins the cached prefix."""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["headline", "body"],
    "additionalProperties": False,
}
"""The two-field output contract handed to `--json-schema` (S8-7)."""

FACT_FENCE = (
    "You will receive a JSON object of verified facts about one event in a "
    "simulated basketball league. Every factual claim in your output must be "
    "traceable to a value in that object. Do not introduce statistics, player "
    "names, team names, dates, quotes, injuries, or events that do not appear "
    "in it. Do not speculate about the future beyond what the facts state. If "
    "the facts do not support a full article, write a shorter piece."
)
"""MV-6's verbatim fact fence, unchanged."""

BRIEF_FENCE = (
    "The `league_brief` object is background. You may reference an entry in "
    "it only when that entry's `link` field is not `none`; an unlinked entry "
    "may inform your framing but must not be stated as a claim about your "
    "subject."
)
"""MV-12.4's brief clause, verbatim."""

NO_QUOTE_FENCE = (
    "This league records no spoken words. Never write a quotation, an "
    "attributed statement, or a paraphrase of anything anyone said."
)
NO_SPELLED_NUMBER_FENCE = (
    "Never write a spelled-out number, ordinal or count that is not in the "
    "facts -- prefer the digits the facts give you."
)
"""The two fences S8-4 adds. The first is enforced by gate 3; the second is
a disclosed blind spot the fence merely discourages."""

HOME_TURF_FENCE = (
    "Your beat is your home turf, and most of what you write is about it. Write "
    "like someone who has watched these games all season and has a voice of "
    "their own. You have no history with this league beyond the facts and the "
    "brief: never invent lore, a backstory, a rivalry, a past season, a "
    "habit of a player or a team, or anything you \"remember\"."
)
"""Persona-with-a-life (S8-22): a niche and a viewpoint, with the no-invention
line stated as plainly as the permission so the two cannot be read apart."""

NUMBER_FENCE = (
    "Use a number only exactly as it is written in the facts or the brief, as "
    "digits (15, never fifteen). Never compute one: no differences, sums, "
    "ratios, averages, percentages or unit conversions, and never round, "
    "shorten, approximate or reformat a value (0.477 stays 0.477, never 47.7 "
    "or 48; 27.625 is never \"late 20s\"). State every comparison in words -- "
    "\"well ahead\", \"trails\", \"fewer wins\" -- never as a figure you "
    "worked out. The `derived` list holds the differences and rates already "
    "worked out for you; quote those instead of computing your own. Describe "
    "a move in the standings only in the direction the `derived` standings "
    "move states (rose, fell or held)."
)
"""S8-22 (1): 8 of the first live run's 10 gate rejections were the model
deriving or reformatting a number. The gate stays exactly as strict; this is
the instruction that keeps the model from tripping it. The last sentence
(gates v2, G2-7) points at the payload's `derived` list, the closed set of
derivations the bound gate accepts. The direction sentence came from the
gates-v2 live run: 6 of 8 team profiles stated their move backwards."""

PROMPT_HIDDEN_FACT_KEYS = frozenset({"standings_position_delta"})
"""Package facts the payload does not show. The signed delta is `current -
prior`, so +4 is a FALL of four places; the live run's model read it as a
climb in 6 of 8 stories. The `derived` standings move carries the same
number with its direction in words, so the sign can only mislead. The
persisted package keeps the fact, and the gate still binds it."""

NO_META_FENCE = (
    "Never write about what the facts lack or do not say: no \"the data does "
    "not say\", \"the facts on file do not include\", \"it is unknown "
    "whether\". Absence is not content. When there is little to say, write "
    "less. The character limit is a ceiling, never a target: length follows the "
    "facts, roughly one sentence per fact, so a story with a few facts is a "
    "short paragraph."
)
"""S8-22 (2): the first run's analyst and beat pieces were padded with
commentary on their own missing data."""

VARIETY_FENCE = (
    "Vary how you open and close. Do not begin with a stock phrase or a "
    "formula you would use on any story (for example \"This is a disaster\"), "
    "do not reuse a sentence shape from one paragraph to the next, and do not "
    "end on a command aimed at a player or team. Start from the most "
    "specific thing in these facts."
)
"""S8-22 (4): every hot_take story of the first run opened "This is a
disaster." with the same cadence."""

_PHASE_PLAIN: dict[Phase, str] = {
    Phase.SETUP: "the league setup",
    Phase.PRESEASON: "the preseason",
    Phase.REGULAR_SEASON: "the regular season",
    Phase.TRADE_DEADLINE: "the trade deadline",
    Phase.POST_DEADLINE: "the stretch after the trade deadline",
    Phase.REGULAR_SEASON_COMPLETE: "the end of the regular season",
    Phase.PLAYIN: "the play-in",
    Phase.PLAYOFFS_R1: "the first round of the playoffs",
    Phase.PLAYOFFS_R2: "the second round of the playoffs",
    Phase.CONFERENCE_FINALS: "the conference finals",
    Phase.FINALS: "the finals",
    Phase.AWARDS: "awards time",
    Phase.DRAFT_LOTTERY: "the draft lottery",
    Phase.DRAFT: "the draft",
    Phase.POST_DRAFT_TRADES: "the trade window after the draft",
    Phase.PROGRESSION: "the offseason progression period",
    Phase.OPTION_DECISIONS: "option decision time",
    Phase.RESIGN_WINDOW: "the re-signing window",
    Phase.FREE_AGENCY: "free agency",
    Phase.WAIVERS: "the waiver period",
}
"""Every `Phase` has a plain phrase (a test asserts the coverage, so a new
phase cannot silently leak its raw token)."""

PLAIN_LANGUAGE_FENCE = (
    "The facts contain internal tokens written in capitals with underscores. "
    "Never print one. Say it in plain words: "
    + "; ".join(f"{p.value} is {words}" for p, words in _PHASE_PLAIN.items())
    + ". Do the same for any other token written with underscores, such as "
    "iso_star."
)
"""S8-22 (3): the first run printed TRADE_DEADLINE, POST_DEADLINE and
DRAFT_LOTTERY. The gate allows them (they are in the facts), so only the
prompt can stop it."""

_TANGENT_TEMPLATE = (
    "`league_brief` is what else is going on in the league right now. You may "
    "connect your subject to at most {max_tangents} of its entries, and only "
    "when the connection is real and a reader would care. If nothing in it "
    "connects, write about your subject alone -- that is the normal case and "
    "a focused piece is better than a forced link. Never list the brief; a "
    "connection you use should read as one clause or one sentence inside your "
    "own story."
)
"""S8-5's conditional-permission-plus-numeric-cap clause. No imperative to
find a connection, no prohibition on leaving the subject."""


def max_tangents(tier: FormatTier) -> int:
    """S8-5: 1 for `BLURB`, 2 for `ARTICLE` -- derived from the budget, never
    configured (a 350-character blurb with two tangents has no room left for
    its own subject)."""
    return 1 if tier is FormatTier.BLURB else 2


def build_persona_card(
    persona: PersonaVoice,
    tier: FormatTier,
    *,
    title_max: int,
    description_max: int,
    has_brief: bool,
) -> str:
    """The system prompt (S8-4 part 1). `title_max`/`description_max` are the
    tier's real `budget_for` limits, passed as plain ints so this module
    never imports `dba2.media` (which `domain/` may not). `has_brief=False`
    omits the tangent clause (S8-14): inviting a connection to material that
    was not supplied is the definition of a forced tangent."""
    parts: list[str] = [
        f"You are {persona.display_name}, who writes {persona.column} for a "
        "basketball league's wire service.",
        persona.beat_description,
        f"Your register is {persona.register.value}.",
    ]
    parts.extend(persona.voice_rules)
    parts.append(HOME_TURF_FENCE)
    parts.append(FACT_FENCE)
    parts.append(BRIEF_FENCE)
    if has_brief:
        parts.append(_TANGENT_TEMPLATE.format(max_tangents=max_tangents(tier)))
    parts.append(NO_QUOTE_FENCE)
    parts.append(NO_SPELLED_NUMBER_FENCE)
    parts.append(NUMBER_FENCE)
    parts.append(NO_META_FENCE)
    parts.append(PLAIN_LANGUAGE_FENCE)
    parts.append(VARIETY_FENCE)
    parts.extend(persona.fences)
    parts.append(
        "Return an object with exactly two string fields, `headline` and "
        f"`body`. The headline is at most {title_max} characters, the body at "
        f"most {description_max}. Write the headline in sentence case. No "
        "markdown, no lists, no headings, no emoji."
    )
    return "\n\n".join(parts) + "\n"


def build_payload(
    package: StoryPackage,
    brief: LeagueBrief | None,
    tier: FormatTier,
    *,
    title_max: int,
    description_max: int,
) -> dict[str, Any]:
    """The per-story stdin object (S8-4 part 2). `league_brief` is an
    explicit `None` when there is no brief -- an explicit null tells the
    model the section exists and is empty, where a missing key invites
    invention about why. `derived` is always present, `[]` when nothing
    derives, for the same reason (G2-7, G2-10): it is computed from the
    package and brief by the same function the bound gate binds, so the
    model is shown exactly the derived numbers the gate accepts. The facts
    in `PROMPT_HIDDEN_FACT_KEYS` are left out of `story.facts`."""
    story = package.to_prompt_json()
    story["facts"] = [f for f in story["facts"] if f["key"] not in PROMPT_HIDDEN_FACT_KEYS]
    return {
        "story": story,
        "league_brief": brief.to_prompt_json() if brief is not None else None,
        "derived": [fact.to_prompt_json() for fact in derived_facts(package, brief)],
        "tier": tier.value,
        "limits": {"headline_chars": title_max, "body_chars": description_max},
    }


def serialize_payload(payload: dict[str, Any]) -> str:
    """`sort_keys=True` so the byte layout is stable run to run."""
    return json.dumps(payload, sort_keys=True)


def response_schema_json() -> str:
    """Compact, key-order-stable JSON for the `--json-schema` argv slot."""
    return json.dumps(RESPONSE_SCHEMA, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# The retry note (docs/design/media-v2-step8-gates-v2.md G2-18): what a
# retried call is told about the draft the gates rejected.
# ---------------------------------------------------------------------------

RETRY_NOTE_MAX_ITEMS = 5
RETRY_NOTE_ITEM_CHARS = 160
"""Bounds on what a rejection can add to the next call: the gates' own
`offending` lines, at most five, each at most 160 characters. A rejection is
never allowed to grow the prompt without limit."""

RETRY_NOTE_HEADER = (
    "Your previous draft of this item was rejected by an automatic fact check "
    "and will not be published. Write the whole item again from the same JSON "
    "above, following every rule. The check rejected:"
)

_RETRY_GATE_FOOTER: dict[str, str] = {
    "numbers_bound": (
        "Use a number only where the facts, the brief or the `derived` list give it "
        "for the team or player that sentence is about, or leave it out."
    ),
    "claims": (
        "State a streak, a standings place, a standings move or games left only as "
        "the facts and the `derived` list give them for that team, or leave it out."
    ),
    "numbers": "Use only numbers written in the facts, the brief or the `derived` list.",
    "proper_nouns": "Use only names that appear in the JSON.",
    "quoted_speech": "Write no quotation of anyone.",
}

_BOUND_LINE_RE = re.compile(r"^(?P<what>.*) -> (?P<about>.*); bound to (?P<owners>.*)$")
_WHITESPACE_RE = re.compile(r"\s+")


def _retry_item(gate: str, raw: str) -> str:
    """One offending line in words. The bound gates' lines read `what ->
    about; bound to owners` (verify.py); anything else is shown as-is."""
    text = _WHITESPACE_RE.sub(" ", raw).strip()
    match = _BOUND_LINE_RE.match(text)
    if gate == "numbers_bound" and match:
        owners = match["owners"]
        whose = (
            "no fact you were given holds it" if owners == "nothing"
            else f"the facts give it to {owners}"
        )
        text = f"{match['what']}: not a number the facts give for {match['about']}; {whose}"
    elif gate == "claims" and match:
        text = f"{match['what']}: not what the facts say about {match['about']}"
    elif gate == "numbers":
        text = f"{text}: not a number in the facts"
    elif gate == "proper_nouns":
        text = f"{text}: a name that is not in the facts"
    elif gate == "quoted_speech":
        text = f"{text}: a quotation (this league records no spoken words)"
    if len(text) > RETRY_NOTE_ITEM_CHARS:
        text = text[: RETRY_NOTE_ITEM_CHARS - 3].rstrip() + "..."
    return text


def build_retry_note(gate: str, offending: Sequence[str]) -> str:
    """The text appended after the payload on a retried call. Pure and
    deterministic: the same rejection always gives the same bytes (golden
    tested). It carries ONLY the gate's name and its `offending` lines -- the
    tokens the gate rejected and the entities it attributed them to, all of
    which are already in the payload or the model's own draft -- never the
    rejected draft, the brief, or anything persisted beyond that. It goes on
    stdin, never argv: the lines hold model-written text, and argv reaches
    `claude.cmd` through cmd.exe."""
    items: list[str] = []
    for raw in offending:
        item = _retry_item(gate, raw)
        if item and item not in items:
            items.append(item)
    shown = items[:RETRY_NOTE_MAX_ITEMS]
    lines = [RETRY_NOTE_HEADER, *(f"- {item}" for item in shown)]
    if len(items) > len(shown):
        lines.append(f"- and {len(items) - len(shown)} more of the same kind")
    footer = _RETRY_GATE_FOOTER.get(gate)
    if footer:
        lines.append(footer)
    return "\n".join(lines) + "\n"


def with_retry_note(payload_text: str, note: str | None) -> str:
    """The stdin of a call: the serialized payload, then (on a retry with
    feedback) one blank line and the note. The payload bytes are unchanged,
    so a retry's JSON is byte-identical to the first call's."""
    return payload_text if not note else f"{payload_text}\n\n{note}"
