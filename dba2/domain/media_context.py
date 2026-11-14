"""`LeagueBrief` -- the bounded league-state payload a writer backend may
read alongside its own `StoryPackage` (docs/design/media-v2-newsroom.md
MV-12, MV-13).

**Why this lives under `domain/`, mirroring `dba2/domain/media_facts.py`'s
own docstring exactly.** `media_repo.py` has to persist one verbatim
(`media_stories.league_brief`, migration `025`, MV-12.3 Option A) and a
future `AnthropicWriter` (step 8) has to serialize one into a prompt;
neither may import `dba2.media` (Decision P4-31's one-way valve), so the
shape both sides need lives here, exactly where `StoryPackage` already
does for the identical reason.

**Five sections, each hard-capped, JSON-leaf-only by construction** (MV-
12.1): `standings`, `other_desks`, `recent_wire`, `season_arc`,
`subject_history`. The caps below are the exact numbers MV-12.1's own
table names -- a construction-time truncation the assembly layer
(`dba2/media/context.py`) enforces by only ever handing this module
already-ranked, already-truncated tuples, never a runtime clamp inside
`to_prompt_json` itself (MV-12.1: "a brief that exceeds its declared
serialized ceiling is a TEST FAILURE, not a silent truncation").

Zero I/O, the same purity rules every other `domain/` module carries --
this file imports only `dba2.domain.phase` (well, not even that: every
field here is a plain scalar) and stdlib.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_NUMBER_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")
"""The identical digit-token regex MV-6's own gate 1 tokenizes a model's
output body with (`docs/design/media-v2-newsroom.md`, "Gate 1 --
numbers") -- shared here so `LeagueBrief.allowed_numbers()`'s own
prose-field extraction (`headline`/`headline_fact`/`persona_column`) uses
the SAME pattern gate 1 will tokenize against, not an independently
re-typed copy that could silently drift from it."""

# ---------------------------------------------------------------------------
# BriefLink / SalienceBand -- MV-13's own closed vocabulary.
# ---------------------------------------------------------------------------


class BriefLink(StrEnum):
    """MV-13's closed `link` vocabulary, computed by the assembly layer
    (`dba2/media/context.py`) by intersecting an entry's own subject ids
    against the WRITING story's own subject ids -- a dict lookup over at
    most twenty entries, never an inference (MV-13 point 1). `SAME_PLAYER`
    and `SAME_TEAM` are checked in that order (a matchup subject can
    satisfy both; the more specific reading wins), `OPPONENT` covers the
    writing subject's own `opponent_team_id`, and `NONE` is everything
    else -- never omitted, so "no link" is a real, present value, not an
    absent field a prompt template could read as `None`/missing."""

    SAME_PLAYER = "same_player"
    SAME_TEAM = "same_team"
    OPPONENT = "opponent"
    NONE = "none"


class SalienceBand(StrEnum):
    """Which of `salience.PublicationPlan`'s three groups a §2 candidate
    came from -- read directly off the plan a cycle already built (MV-
    12.1's own "already in memory" claim for §2), never a second,
    independently-invented score-bucketing scheme. `MAIN` cleared the
    phase's own `TOP_K_BY_PHASE` floor-and-K; `BAND` is the `hot_take`
    sub-floor pool (MV-3h.0); `PROFILE` is the reserved `TEAM_PROFILE`
    quota, which bypasses the floor entirely."""

    MAIN = "main"
    BAND = "band"
    PROFILE = "profile"


# ---------------------------------------------------------------------------
# Section 1 -- standings
# ---------------------------------------------------------------------------

STANDINGS_TOP_CAP = 5
STANDINGS_BOTTOM_CAP = 3
STANDINGS_CAP = 9
"""MV-12.1 §1: top 5, bottom 3, plus the writing story's own subject team
if not already present among those 8 -- 9 rows at most."""


@dataclass(frozen=True)
class StandingsRow:
    team_id: int
    team_name: str
    wins: int
    losses: int
    standings_position: int
    streak_length: int

    def to_prompt_json(self) -> dict:
        return {
            "team_id": self.team_id,
            "team_name": self.team_name,
            "wins": self.wins,
            "losses": self.losses,
            "standings_position": self.standings_position,
            "streak_length": self.streak_length,
        }


# ---------------------------------------------------------------------------
# Section 2 -- other_desks
# ---------------------------------------------------------------------------

OTHER_DESKS_CAP = 8


@dataclass(frozen=True)
class OtherDeskEntry:
    """One OTHER sealed candidate this cycle produced -- MV-12.1 §2.
    `headline_fact` is `None` (never a fabricated placeholder) when a
    package carries no fact outside `UNIVERSAL_FACT_KEYS`/the identity
    keys -- "absent is neutral," the same rule every other section here
    follows."""

    story_kind: str
    angle: str
    subject_name: str
    headline_fact: str | None
    salience_band: SalienceBand
    link: BriefLink

    def to_prompt_json(self) -> dict:
        return {
            "story_kind": self.story_kind,
            "angle": self.angle,
            "subject_name": self.subject_name,
            "headline_fact": self.headline_fact,
            "salience_band": self.salience_band.value,
            "link": self.link.value,
        }


# ---------------------------------------------------------------------------
# Section 3 -- recent_wire
# ---------------------------------------------------------------------------

RECENT_WIRE_CAP = 12


@dataclass(frozen=True)
class WireEntry:
    """One story published in the last 3 cycles -- MV-12.1 §3. `link` is
    computed the same way an `OtherDeskEntry`'s is (MV-13 point 1: "on
    every §2 and §3 entry")."""

    cycle_index: int
    story_kind: str
    subject_name: str
    headline: str
    persona_column: str
    link: BriefLink

    def to_prompt_json(self) -> dict:
        return {
            "cycle_index": self.cycle_index,
            "story_kind": self.story_kind,
            "subject_name": self.subject_name,
            "headline": self.headline,
            "persona_column": self.persona_column,
            "link": self.link.value,
        }


# ---------------------------------------------------------------------------
# Section 4 -- season_arc
# ---------------------------------------------------------------------------

SEASON_ARC_TOP_CAP = 3
SEASON_ARC_BOTTOM_CAP = 1


@dataclass(frozen=True)
class SeasonArcRow:
    team_id: int
    team_name: str
    wins: int
    losses: int
    standings_position: int

    def to_prompt_json(self) -> dict:
        return {
            "team_id": self.team_id,
            "team_name": self.team_name,
            "wins": self.wins,
            "losses": self.losses,
            "standings_position": self.standings_position,
        }


@dataclass(frozen=True)
class SeasonArc:
    """MV-12.1 §4: this cycle's own coordinates plus last SEASON's final
    top-3/bottom-1 -- `last_season_top`/`last_season_bottom` are both `()`
    (never a fabricated row) when `get_final_pulse_snapshot_for_season`
    found nothing for `season - 1` (the league's first season, or one
    older than the media cycle itself, MV-12.1's own "absent is neutral"
    rule, carried over from `dba2.media.pulse`'s identical convention)."""

    season: int
    phase: str
    cycle_index: int
    league_games_played: int
    last_season_top: tuple[SeasonArcRow, ...] = ()
    last_season_bottom: tuple[SeasonArcRow, ...] = ()

    def to_prompt_json(self) -> dict:
        return {
            "season": self.season,
            "phase": self.phase,
            "cycle_index": self.cycle_index,
            "league_games_played": self.league_games_played,
            "last_season_top": [r.to_prompt_json() for r in self.last_season_top],
            "last_season_bottom": [r.to_prompt_json() for r in self.last_season_bottom],
        }


# ---------------------------------------------------------------------------
# Section 5 -- subject_history
# ---------------------------------------------------------------------------

SUBJECT_HISTORY_CAP = 5


@dataclass(frozen=True)
class SubjectHistoryEntry:
    cycle_index: int
    story_kind: str
    headline: str

    def to_prompt_json(self) -> dict:
        return {
            "cycle_index": self.cycle_index,
            "story_kind": self.story_kind,
            "headline": self.headline,
        }


# ---------------------------------------------------------------------------
# LeagueBrief
# ---------------------------------------------------------------------------

MAX_SERIALIZED_CHARS = 6000
"""MV-14.4 item 3: `len(json.dumps(brief.to_prompt_json(), sort_keys=True))`
must never exceed this for a real corpus cycle -- asserted by a FAILING
TEST (`tests/unit/domain/test_media_context.py`), never truncated here at
runtime. ~1500 tokens, MV-14.5's own token-budget citation."""


@dataclass(frozen=True)
class LeagueBrief:
    """MV-12.1's whole payload -- one instance per (cycle, subject) pair in
    this build step (see `dba2/media/context.py::build_league_brief`'s own
    docstring for why the "one brief per cycle, shared by every story"
    framing and the doc's own subject-relative section text -- "the
    subject's own team," "every OTHER sealed candidate" -- are reconciled:
    every section's READS are cycle-scoped and loaded exactly once; only
    the cheap, zero-I/O `link`/self-exclusion/own-team decoration that
    MV-13 and MV-12.1's own table both require varies per subject).

    `to_prompt_json()` is the exact object MV-6's future prompt contract
    serializes (`json.dumps(brief.to_prompt_json(), sort_keys=True)`,
    MV-14.4 item 3) -- not exercised by any real prompt caller in this
    build step (the writer seam step 8 builds is the eventual reader),
    included now for the identical reason `StoryPackage.to_prompt_json()`
    was: a zero-risk, zero-cost consequence of every field already being a
    flat JSON leaf."""

    standings: tuple[StandingsRow, ...]
    other_desks: tuple[OtherDeskEntry, ...]
    recent_wire: tuple[WireEntry, ...]
    season_arc: SeasonArc
    subject_history: tuple[SubjectHistoryEntry, ...] = ()

    def to_prompt_json(self) -> dict:
        return {
            "standings": [r.to_prompt_json() for r in self.standings],
            "other_desks": [r.to_prompt_json() for r in self.other_desks],
            "recent_wire": [r.to_prompt_json() for r in self.recent_wire],
            "season_arc": self.season_arc.to_prompt_json(),
            "subject_history": [r.to_prompt_json() for r in self.subject_history],
        }

    @classmethod
    def from_prompt_json(cls, data: Mapping[str, Any]) -> LeagueBrief:
        """Inverse of `to_prompt_json()` -- rehydrates a brief persisted on
        `media_stories.league_brief` (migration 025) so the post-commit AI
        rewrite pass can call `allowed_numbers()` / `allowed_proper_nouns()`
        on it (docs/design/media-v2-step8-anthropic-writer.md S8-14a: those
        are methods, so raw persisted JSON is not enough for the gates).
        Mirrors `MediaCorpusArm.from_json`'s pattern. Raises `KeyError` /
        `ValueError` on a malformed mapping rather than guessing -- the
        caller (the rewrite pass) treats that as "no usable brief" and
        must not fabricate one. An empty mapping (the column's `'{}'`
        default, meaning "no brief was persisted") is the caller's
        None-case and is rejected here with `ValueError`."""
        if not data:
            raise ValueError("cannot rehydrate a LeagueBrief from an empty mapping")
        arc = data["season_arc"]

        def _arc_rows(rows: list[Mapping[str, Any]]) -> tuple[SeasonArcRow, ...]:
            return tuple(
                SeasonArcRow(
                    team_id=r["team_id"], team_name=r["team_name"], wins=r["wins"],
                    losses=r["losses"], standings_position=r["standings_position"],
                )
                for r in rows
            )

        return cls(
            standings=tuple(
                StandingsRow(
                    team_id=r["team_id"], team_name=r["team_name"], wins=r["wins"],
                    losses=r["losses"], standings_position=r["standings_position"],
                    streak_length=r["streak_length"],
                )
                for r in data["standings"]
            ),
            other_desks=tuple(
                OtherDeskEntry(
                    story_kind=r["story_kind"], angle=r["angle"],
                    subject_name=r["subject_name"], headline_fact=r["headline_fact"],
                    salience_band=SalienceBand(r["salience_band"]), link=BriefLink(r["link"]),
                )
                for r in data["other_desks"]
            ),
            recent_wire=tuple(
                WireEntry(
                    cycle_index=r["cycle_index"], story_kind=r["story_kind"],
                    subject_name=r["subject_name"], headline=r["headline"],
                    persona_column=r["persona_column"], link=BriefLink(r["link"]),
                )
                for r in data["recent_wire"]
            ),
            season_arc=SeasonArc(
                season=arc["season"], phase=arc["phase"], cycle_index=arc["cycle_index"],
                league_games_played=arc["league_games_played"],
                last_season_top=_arc_rows(arc["last_season_top"]),
                last_season_bottom=_arc_rows(arc["last_season_bottom"]),
            ),
            subject_history=tuple(
                SubjectHistoryEntry(
                    cycle_index=r["cycle_index"], story_kind=r["story_kind"],
                    headline=r["headline"],
                )
                for r in data["subject_history"]
            ),
        )

    def allowed_numbers(self) -> frozenset[str]:
        """MV-12.4's own trap, made concrete -- and, per a step-8 design
        dispatch's own direct read of this method, CORRECTED to the real
        shape gate 1 needs. MV-6's own gate 1 text (`docs/design/media-v2-
        newsroom.md`, "Gate 1 -- numbers") tokenizes the model's OUTPUT body
        with `\\d+(?:\\.\\d+)?` and checks each token for STRING EQUALITY
        against an allowed set built from, per `Fact`: the numeric tokens of
        its OWN `value_text` (the identical regex) **plus** `value_number`
        rendered at 0/1/2/3 decimal places **plus** its integer part. A
        `frozenset[float]` (this method's first shape) can never satisfy a
        string-equality check against a regex-tokenized body -- `"5"` never
        equals `5.0`. This method now returns the exact rendered-STRING
        forms gate 1 compares against, so the widening is a plain union:
        `gate1_allowed_tokens |= brief.allowed_numbers()`. Covers both
        halves of the package-side rule: every structured numeric leaf
        (win/loss counts, standings positions, streak lengths, cycle
        indices, season, league games played), rendered at every precision
        gate 1 itself renders at, AND every digit token embedded in a PROSE
        field this brief exposes (`headline_fact`, `recent_wire`/`subject_
        history` headlines, `persona_column`) via the identical `\\d+
        (?:\\.\\d+)?` regex `Fact.value_text` is scanned with -- a digit
        inside a quoted prior headline is exactly as real as one inside a
        `Fact.value_text`, and gate 1 must not reject it for living one
        section over."""
        tokens: set[str] = set()

        def _add_number(value: float) -> None:
            tokens.add(str(int(value)))
            tokens.add(f"{value:.0f}")
            tokens.add(f"{value:.1f}")
            tokens.add(f"{value:.2f}")
            tokens.add(f"{value:.3f}")

        def _add_text_tokens(text: str | None) -> None:
            if text:
                tokens.update(_NUMBER_TOKEN_RE.findall(text))

        for row in self.standings:
            for value in (row.wins, row.losses, row.standings_position, row.streak_length):
                _add_number(float(value))
        for row in self.other_desks:
            _add_text_tokens(row.headline_fact)
        for row in self.recent_wire:
            _add_number(float(row.cycle_index))
            _add_text_tokens(row.headline)
            _add_text_tokens(row.persona_column)
        arc = self.season_arc
        for value in (arc.season, arc.cycle_index, arc.league_games_played):
            _add_number(float(value))
        for row in (*arc.last_season_top, *arc.last_season_bottom):
            for value in (row.wins, row.losses, row.standings_position):
                _add_number(float(value))
        for row in self.subject_history:
            _add_number(float(row.cycle_index))
            _add_text_tokens(row.headline)
        return frozenset(tokens)

    def allowed_proper_nouns(self) -> frozenset[str]:
        """MV-12.4's own trap, made concrete -- and, per a step-8 design
        dispatch's own direct read of this method, CORRECTED. MV-6's own
        gate 2 text builds its allowed set from `package.subject`'s names
        **∪ every `Fact.value_text`** ∪ the cycle's team-name vocabulary,
        then requires every multi-word capitalized sequence in the model's
        OUTPUT body to "appear in" that union -- `Fact.value_text` is
        admitted WHOLESALE, as a string to search WITHIN (a captured
        "Marcus Webb" can be a substring of a longer `value_text` like
        "Marcus Webb (24) leads the league"), never pre-tokenized for
        capitalized words on the package side. This method's first shape
        excluded `headline`/`headline_fact`/`persona_column` on the theory
        that gate 2 already tokenizes prose separately -- **it does not**;
        it admits `Fact.value_text` verbatim, and a brief-sourced prose
        field is exactly the same kind of value and must be admitted the
        same way, or MV-14.4 item 6's own worked example (a persona
        referencing another desk's prior headline about the same player)
        fails gate 2 outright, silently, for every story, forever -- the
        precise failure mode MV-12.4 already warns about for a missed name.
        Returned as one search corpus (short names AND long prose strings
        together): `gate2_search_corpus |= brief.allowed_proper_nouns()`,
        then gate 2's own "appears in" check (substring, not set-equality)
        runs against the widened corpus unchanged."""
        corpus: set[str] = set()
        for row in self.standings:
            corpus.add(row.team_name)
        for row in self.other_desks:
            corpus.add(row.subject_name)
            if row.headline_fact:
                corpus.add(row.headline_fact)
        for row in self.recent_wire:
            corpus.add(row.subject_name)
            corpus.add(row.headline)
            corpus.add(row.persona_column)
        for row in (*self.season_arc.last_season_top, *self.season_arc.last_season_bottom):
            corpus.add(row.team_name)
        for row in self.subject_history:
            corpus.add(row.headline)
        return frozenset(corpus)
