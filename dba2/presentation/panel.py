"""`Panel`/`PanelField`/`PanelColor` -- the library-agnostic render type
Decision P5-4 names: `presentation/` returns a `Panel`, and `discord.Embed`
exists only inside `dba2/bot/embeds.py` (the one small adapter, per
Decision P5-1/P5-4 and §2.2 Option B).

**The limit numbers below are Discord's real, current, official values,
fetched and read from `docs/developers/docs/resources/message` (the
`Embed Limits` table) this Stage A step -- not the general-knowledge
placeholders §2.2's own caveat flagged as unverified.** Verified
2026-09-17 against `https://discord.com/developers/docs/resources/message`:

    title              256 characters
    description        4096 characters
    fields             up to 25 field objects
    field.name         256 characters
    field.value        1024 characters
    footer.text        2048 characters
    author.name        256 characters
    combined total      6000 characters, summed across title, description,
                         every field.name, every field.value, footer.text
                         and author.name -- **across every embed attached
                         to one message, not just one embed**. `Panel`
                         validates its own total against this ceiling,
                         which is a necessary but not sufficient check
                         when a message carries more than one embed (a
                         `bot/`-side composer that attaches several
                         `Panel`s to one message must sum their totals
                         itself before it does).

A `Panel` that violates any of these limits fails at construction, in a
unit test against a fixture, per Decision P5-4's own reasoning: "a
`Panel` validator is a Stage A deliverable ... so a too-long roster
fails in a unit test rather than at runtime in a production guild."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

# Discord's real, documented embed limits (see module docstring for the
# citation). Named as module-level constants, not inlined, so a future
# reader diffs one place if Discord's own numbers ever change.
MAX_TITLE_CHARS = 256
MAX_DESCRIPTION_CHARS = 4096
MAX_FIELDS = 25
MAX_FIELD_NAME_CHARS = 256
MAX_FIELD_VALUE_CHARS = 1024
MAX_FOOTER_TEXT_CHARS = 2048
MAX_AUTHOR_NAME_CHARS = 256
# "the combined sum of characters in all title, description, field.name,
# field.value, footer.text, and author.name fields across all embeds
# attached to a message must not exceed 6000 characters" -- Discord's own
# wording, verbatim in spirit. `Panel.validate()` checks this against ONE
# panel's own total; see module docstring's note on multi-embed messages.
MAX_TOTAL_CHARS = 6000


class PanelColor(IntEnum):
    """A small, semantic palette rather than a raw RGB int at every call
    site -- `bot/embeds.py` is the only place that ever needs to know
    these are eventually a `discord.Embed`'s `colour` int. Values are
    ordinary 24-bit RGB, chosen for legibility (a `bot/`-side adapter
    passes `.value` straight through), not copied from any Discord
    default -- Discord does not mandate a semantic palette, this
    project's own is namespaced here so `presentation/` never imports
    `discord` to build one."""

    NEUTRAL = 0x5865F2  # Discord's own "blurple" -- the safe default
    POSITIVE = 0x2ECC71
    NEGATIVE = 0xE74C3C
    WARNING = 0xF1C40F
    GOLD = 0xFFD700  # a period's end: distinct from WARNING's amber, which means trouble


@dataclass(frozen=True)
class PanelField:
    """One `discord.Embed` field's worth of content, library-agnostic.
    `inline` mirrors the embed field object's own `inline` flag 1:1 --
    the one piece of Discord's own shape genuinely worth carrying through
    unchanged, because it is a layout decision the panel builder (not the
    adapter) is the right place to make."""

    name: str
    value: str
    inline: bool = False

    def __post_init__(self) -> None:
        if not (1 <= len(self.name) <= MAX_FIELD_NAME_CHARS):
            raise ValueError(
                f"PanelField.name must be 1-{MAX_FIELD_NAME_CHARS} characters, "
                f"got {len(self.name)}"
            )
        if not (1 <= len(self.value) <= MAX_FIELD_VALUE_CHARS):
            raise ValueError(
                f"PanelField.value must be 1-{MAX_FIELD_VALUE_CHARS} characters, "
                f"got {len(self.value)}"
            )


@dataclass(frozen=True)
class Panel:
    """The library-agnostic render type every `presentation/` builder
    returns (Decision P5-4). `bot/embeds.py` is the ONLY module in the
    codebase that ever turns one of these into a `discord.Embed` --
    everything above it (`view_service`, every `presentation/*.py`
    builder) constructs and consumes `Panel`s only.

    Validation happens at construction (`__post_init__`, frozen-dataclass
    safe -- it reads `self`, it never mutates it) rather than as a
    separate opt-in call, so a too-long panel fails the moment it is
    built, in whichever unit test builds it, per Decision P5-4's own
    reasoning."""

    title: str
    description: str = ""
    fields: tuple[PanelField, ...] = field(default_factory=tuple)
    color: PanelColor = PanelColor.NEUTRAL
    footer: str = ""
    author: str = ""

    def __post_init__(self) -> None:
        if not (1 <= len(self.title) <= MAX_TITLE_CHARS):
            raise ValueError(
                f"Panel.title must be 1-{MAX_TITLE_CHARS} characters, got {len(self.title)}"
            )
        if len(self.description) > MAX_DESCRIPTION_CHARS:
            raise ValueError(
                f"Panel.description must be at most {MAX_DESCRIPTION_CHARS} "
                f"characters, got {len(self.description)}"
            )
        if len(self.fields) > MAX_FIELDS:
            raise ValueError(
                f"Panel.fields must have at most {MAX_FIELDS} entries, got {len(self.fields)}"
            )
        if len(self.footer) > MAX_FOOTER_TEXT_CHARS:
            raise ValueError(
                f"Panel.footer must be at most {MAX_FOOTER_TEXT_CHARS} "
                f"characters, got {len(self.footer)}"
            )
        if len(self.author) > MAX_AUTHOR_NAME_CHARS:
            raise ValueError(
                f"Panel.author must be at most {MAX_AUTHOR_NAME_CHARS} "
                f"characters, got {len(self.author)}"
            )
        total = (
            len(self.title)
            + len(self.description)
            + sum(len(f.name) + len(f.value) for f in self.fields)
            + len(self.footer)
            + len(self.author)
        )
        if total > MAX_TOTAL_CHARS:
            raise ValueError(
                f"Panel's own combined character total must be at most "
                f"{MAX_TOTAL_CHARS}, got {total} (Discord's limit is per "
                f"MESSAGE, across every attached embed -- a composer "
                f"combining multiple Panels into one message must also "
                f"sum their totals before sending)"
            )
