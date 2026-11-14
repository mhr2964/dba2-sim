"""The three post-generation gates for AI-written stories
(docs/design/media-v2-step8-anthropic-writer.md S8-10, MV-6).

**Why this exists at all.** Template output is correct by construction; a
model's text is not, so it is checked against the facts the model was
given. **The gates are not a fact-checker** -- they catch invented digits,
invented multi-word proper nouns and fabricated quotations, and nothing
else. Spelled numerals/ordinals, a single-word invented surname and
non-numeric non-nominal fabrication (a rumor, an injury) are disclosed blind
spots, asserted as passing in `tests/unit/test_media_write_verify.py` so
nobody mistakes this module for more than it is.

**Gate policy is the caller's** (`media_service`'s rewrite pass); this
module only returns a `GateResult`. Pure: stdlib + `dba2.domain`.

**The widening rule (MV-12.4).** An allowlist narrower than the prompt's own
payload rejects correct prose forever and looks exactly like model
non-compliance. Every member added below is a verbatim DB or library value;
nothing false enters a set. Beyond S8-10's list this also admits the numeric
tokens / text of `Fact.label`, because labels are in the payload too
("Points per 36, last 5" is something a model may echo).
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from dba2.domain.media_bindings import (
    LEAGUE,
    Bindings,
    Scope,
    TeamAliases,
    build_bindings,
    entity_aliases,
    scope_name,
)
from dba2.domain.media_context import LeagueBrief
from dba2.domain.media_facts import StoryPackage
from dba2.domain.media_writer import PersonaVoice
from dba2.domain.phase import Phase

_NUMBER_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")
"""Identical to `media_context._NUMBER_TOKEN_RE` and MV-6's gate 1."""

_CAP_WORD = "[A-Z][A-Za-z]*(?:[-'’][A-Za-z]+)*"
_CAP_SEQUENCE_RE = re.compile(_CAP_WORD + r"(?:\s+" + _CAP_WORD + ")+")
"""A run of two or more capitalized words. Single capitalized words are a
disclosed blind spot (they are also every sentence's first word)."""

_POSSESSIVE_RE = re.compile("(?:['’]s|['’])$")
_SENTENCE_END_RE = re.compile(r"(?:^|[.!?:]\s+|\n)$")
_STRAIGHT_QUOTED_RE = re.compile('"[^"]*"')
_CURLY_QUOTED_RE = re.compile("“[^”]*”")


@dataclass(frozen=True)
class GateResult:
    """`gate` names the failing gate (`numbers` / `numbers_bound` / `claims`
    / `proper_nouns` / `quoted_speech`) and `offending` the failing tokens
    ONLY -- never the body (S8-11: a rejected body at WARNING level is log
    spam). A bound-gate token carries its attribution ("record 24-13 -> Los
    Angeles Lakers; bound to Denver Nuggets") so the WARNING line says what
    was misattributed, not just which digit."""

    passed: bool
    gate: str | None = None
    offending: tuple[str, ...] = ()


_PASS = GateResult(passed=True)


# ---------------------------------------------------------------------------
# Gate 1 -- numbers
# ---------------------------------------------------------------------------


def _render_number(value: float) -> set[str]:
    """MV-6: the value rendered at 0/1/2/3 decimals plus its integer part,
    so honest rounding passes. Same forms as `LeagueBrief.allowed_numbers`
    (Defect B: strings, not floats, because gate 1 is string equality)."""
    return {
        str(int(value)),
        f"{value:.0f}",
        f"{value:.1f}",
        f"{value:.2f}",
        f"{value:.3f}",
    }


def allowed_number_tokens(
    package: StoryPackage, brief: LeagueBrief | None = None
) -> frozenset[str]:
    """Package side (MV-6) UNION the brief's own set. Deliberately NOT the
    tier's character limits: those are instructions, not facts."""
    tokens: set[str] = set()
    for fact in package.facts:
        tokens.update(_NUMBER_TOKEN_RE.findall(fact.value_text))
        tokens.update(_NUMBER_TOKEN_RE.findall(fact.label))
        if fact.value_number is not None:
            tokens |= _render_number(fact.value_number)
    tokens |= _render_number(float(package.season))
    tokens |= _render_number(float(package.season - 1))
    if brief is not None:
        tokens |= brief.allowed_numbers()
    return frozenset(tokens)


def verify_no_invented_numbers(
    package: StoryPackage,
    headline: str,
    body: str,
    *,
    brief: LeagueBrief | None = None,
) -> GateResult:
    allowed = allowed_number_tokens(package, brief)
    bad: list[str] = []
    for token in _NUMBER_TOKEN_RE.findall(f"{headline}\n{body}"):
        if token not in allowed and token not in bad:
            bad.append(token)
    return GateResult(False, "numbers", tuple(bad)) if bad else _PASS


# ---------------------------------------------------------------------------
# Gate 1b -- numbers bound to the entity they describe, and gate 4 -- claim
# classes (docs/design/media-v2-step8-gates-v2.md G2-1..G2-6)
# ---------------------------------------------------------------------------


class NumberPolicy(StrEnum):
    """`POOLED` is gate 1 above: a token passes if it equals any number in
    the package or brief. `BOUND` is gate 1b plus gate 4: a token passes only
    if it is a number about an entity its clause is about, typed tokens
    (records, ordinals, spots, streaks) must match the right KIND, and five
    closed claim shapes are checked. `BOUND` is the default, flipped in the
    same commit that put the `derived` list in the payload (G2-7: before
    that, it false-rejects true derivations). Not a config field (G2-8): a
    commissioner has no reason to choose a weaker gate. `POOLED` exists so
    `tools/media_gate_eval.py` can print the before column."""

    POOLED = "pooled"
    BOUND = "bound"


_PLAYER_PRONOUN_RE = re.compile(r"\b(?:he|his|him)\b", re.IGNORECASE)
_TEAM_PRONOUN_RE = re.compile(
    r"\b(?:they|their|them|its|the team|the roster|the club)\b", re.IGNORECASE
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_SPLIT_RE = re.compile(
    r"[,;:()]|\s--\s|\s?—\s?|\s+(?:and|but|while|whereas|as|so)\s+"
)
"""G2-3: clauses split at punctuation, dashes and coordinators, so "X is
24-12, one game behind Y at 25-12" gives each record its own team. "X and Y
are both 24-12" splits too and leaves X's half unchecked: a hole, never a
false reject (asserted in the tests)."""

_RECORD_RE = re.compile(r"(?<![\d-])(\d+)-(\d+)(?![\d-])")
_ORDINAL_RE = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b")
_NO_N_RE = re.compile(r"\bNo\.\s*(\d+)\b|\bposition (\d+)\b")
_SPOTS_RE = re.compile(r"\b(\d+)[- ](?:spots?|places?|positions?)\b", re.IGNORECASE)
_MOVE_UP = (
    r"up|rose|risen|rises?|rising|climb(?:s|ed|ing)?|jump(?:s|ed|ing)?|surg(?:e|es|ed|ing)"
    r"|leap(?:s|t|ed|ing)?|vault(?:s|ed|ing)?|soar(?:s|ed|ing)?|gain(?:s|ed|ing)?"
    r"|improv(?:e|es|ed|ing)"
)
_MOVE_DOWN = (
    r"down|fell|fall(?:s|en|ing)?|drop(?:s|ped|ping)?|slid(?:e|es|ing)?|slip(?:s|ped|ping)?"
    r"|tumbl(?:e|es|ed|ing)|plung(?:e|es|ed|ing)|sank|sunk|sink(?:s|ing)?"
    r"|crater(?:s|ed|ing)?|lost|los(?:e|es|ing)|dip(?:s|ped|ping)?|skid(?:s|ded|ding)?"
    r"|declin(?:e|es|ed|ing)"
)
_MOVE_WORD_RE = re.compile(rf"\b(?:(?P<up>{_MOVE_UP})|(?P<down>{_MOVE_DOWN}))\b", re.IGNORECASE)
_MOVE_NOUN_AFTER_RE = re.compile(
    r"\s+(?:(?P<up>climb|jump|rise|surge|leap|gain|improvement)"
    r"|(?P<down>drop|fall|slide|slip|tumble|plunge|dip|skid|decline))\b",
    re.IGNORECASE,
)
_LAST_WORDS_RE = re.compile(r"(?:\S+\s+){0,4}$")
"""C4's direction (the gates-v2 direction fix): the move word is the noun
right after the count ("a 24-place climb"), else the nearest move word in
the four words before it ("a drop of 9 places", "moved up 1 spot"). A count
with no move word ("a change of 6 places") claims no direction and is
checked for magnitude only. Comparisons that are not moves ("4 places
behind", "higher", "lower") are deliberately not move words."""
_STREAK_GAME_RE = re.compile(
    r"\b(\d+)[- ]game (win|winning|losing|loss)(?:ing)? streak", re.IGNORECASE
)
_STREAK_VERB_RE = re.compile(
    r"\b(won|lost|dropped|winners of|losers of) (\d+) (?:straight|in a row)", re.IGNORECASE
)
_STREAK_NOUN_RE = re.compile(r"\b(\d+) straight (wins|losses)\b", re.IGNORECASE)
_GAMES_LEFT_RE = re.compile(
    r"\b(\d+) (?:more )?(?:games?|meetings?|matchups?) (?:left|remaining|to go|to play)\b",
    re.IGNORECASE,
)
_IN_STANDINGS_RE = re.compile(
    r"\b(\d+)(?:st|nd|rd|th)\b[^.,;]{0,25}\b(?:in the standings|place)\b", re.IGNORECASE
)
_FIRST_PLACE_RE = re.compile(
    r"\bfirst place\b|\batop the (?:league|standings)\b", re.IGNORECASE
)
_LAST_PLACE_RE = re.compile(
    r"\blast place\b|\bbottom of the (?:league|standings)\b", re.IGNORECASE
)
_PLACE_NOT_CLAIMED_RE = re.compile(
    r"\b(?:behind|back of|out of|from|for|chasing|toward|towards|than|of|to"
    r"|trail|trails|trailed|trailing)\s+(?:the\s+)?$",
    re.IGNORECASE,
)
"""C5 exemption: "3 games behind first place", "a race for first place" and
"trails first place by 13.5 games" name the place without claiming the
clause's team holds it. `trail*` was added from the live run (three true
stories rejected on it)."""

_POSITION_KINDS = frozenset({"position", "rank"})
_PLACE_KINDS = frozenset({"position"})
_STREAK_KINDS = frozenset({"streak"})
_DELTA_KINDS = frozenset({"delta"})


def _mention_spans(
    text: str, aliases: tuple[tuple[str, Scope], ...]
) -> list[tuple[int, int, Scope]]:
    """`(start, end, scope)` per mention. Longest alias first; a shorter alias
    inside an already-matched span is not a second mention ("Los Angeles
    Lakers" is not also "Los Angeles", the Clippers' city). The same alias
    string for two teams (a shared city) yields both, the permissive
    any-match of G2-2."""
    by_alias: dict[str, list[Scope]] = {}
    for alias, scope in aliases:
        by_alias.setdefault(alias, []).append(scope)
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, int, Scope]] = []
    for alias in sorted(by_alias, key=len, reverse=True):
        pattern = r"(?<![A-Za-z])" + re.escape(alias) + r"(?:['’]s?)?(?![A-Za-z])"
        for match in re.finditer(pattern, text):
            start, end = match.span()
            if any(start < t_end and t_start < end for t_start, t_end in taken):
                continue
            taken.append((start, end))
            found.extend((start, end, scope) for scope in by_alias[alias])
    return sorted(found, key=lambda item: item[0])


def _mentions(text: str, aliases: tuple[tuple[str, Scope], ...]) -> list[tuple[int, Scope]]:
    return [(start, scope) for start, _, scope in _mention_spans(text, aliases)]


@dataclass(frozen=True)
class _Context:
    package: StoryPackage
    bindings: Bindings
    aliases: tuple[tuple[str, Scope], ...]
    team_aliases: Mapping[int, TeamAliases]

    def label(self, scope: Scope) -> str:
        if scope == LEAGUE:
            return "league"
        if scope[0] == "name":
            return str(scope[1])
        return scope_name(scope, self.package, self.team_aliases) or f"{scope[0]} {scope[1]}"

    def labels(self, scopes: Iterable[Scope]) -> str:
        names = sorted({self.label(s) for s in scopes})
        return ", ".join(names) if names else "nothing"

    def token_ok(self, token: str, scopes: set[Scope], kinds: frozenset[str] | None) -> bool:
        for scope, kind in self.bindings.scopes_for(token):
            if scope == LEAGUE and kinds is None:
                return True
            if scope in scopes and (kinds is None or kind in kinds):
                return True
        return False

    def owners(self, token: str, kinds: frozenset[str] | None = None) -> set[Scope]:
        return {s for s, k in self.bindings.scopes_for(token) if kinds is None or k in kinds}


def _attributed_clauses(
    ctx: _Context, headline: str, body: str
) -> Iterable[tuple[str, set[Scope]]]:
    """`(clause, scopes)` for every clause of the headline and body, `scopes`
    being the entities the clause's numbers are attributed to (G2-3): the
    clause's own mentions, else the nearest preceding clause of the sentence
    that has one; pronouns add the paragraph's last player/team on top; with
    still nothing, the paragraph's last player and team, else the story's own
    subject. The headline is its own paragraph."""
    subject = ctx.package.subject
    subject_player: Scope | None = (
        ("player", subject.player_id) if subject.player_id is not None else None
    )
    subject_team: Scope | None = ("team", subject.team_id) if subject.team_id is not None else None
    subject_scopes = {s for s in (subject_player, subject_team) if s is not None}
    if subject.opponent_team_id is not None:
        subject_scopes.add(("team", subject.opponent_team_id))
    for paragraph in [headline, *body.split("\n")]:
        last_player: Scope | None = None
        last_team: Scope | None = None
        for sentence in _SENTENCE_SPLIT_RE.split(paragraph.strip()):
            if not sentence:
                continue
            clauses = [c for c in _CLAUSE_SPLIT_RE.split(sentence) if c and c.strip()]
            clause_entities = [{s for _, s in _mentions(c, ctx.aliases)} for c in clauses]
            for i, clause in enumerate(clauses):
                scopes: set[Scope] = set(clause_entities[i])
                if not scopes:
                    scopes = next((set(e) for e in reversed(clause_entities[:i]) if e), set())
                pronoun_scopes = []
                if _PLAYER_PRONOUN_RE.search(clause):
                    pronoun_scopes.append(last_player or subject_player)
                if _TEAM_PRONOUN_RE.search(clause):
                    pronoun_scopes.append(last_team or subject_team)
                scopes.update(s for s in pronoun_scopes if s is not None)
                if not scopes:
                    scopes = {s for s in (last_player, last_team) if s is not None}
                    scopes = scopes or set(subject_scopes)
                for scope in list(scopes):
                    name = scope_name(scope, ctx.package, ctx.team_aliases)
                    if name:
                        scopes.add(("name", name))
                yield clause, scopes
                for _, scope in _mentions(clause, ctx.aliases):
                    if scope[0] == "player":
                        last_player = scope
                    elif scope[0] == "team":
                        last_team = scope


def _move_word(clause: str, match: re.Match[str]) -> tuple[str, bool] | None:
    """`(word, up)` for a spots count, or `None` when it states no direction."""
    after = _MOVE_NOUN_AFTER_RE.match(clause, match.end())
    if after is not None:
        return after.group(0).strip(), after.group("up") is not None
    window = _LAST_WORDS_RE.search(clause[: match.start()])
    words = list(_MOVE_WORD_RE.finditer(window.group(0))) if window else []
    if not words:
        return None
    return words[-1].group(0), words[-1].group("up") is not None


def _move_direction_ok(ctx: _Context, scopes: set[Scope], n: int, up: bool) -> bool:
    """A smaller position is higher in the table, so a climb is a NEGATIVE
    delta (`current - prior`). A zero delta has no direction to contradict."""
    return any(
        abs(value) == n and (value == 0 or (value < 0) == up)
        for scope in scopes
        for value in ctx.bindings.deltas.get(scope, ())
    )


def _streak_direction_ok(ctx: _Context, scopes: set[Scope], n: int, winning: bool) -> bool:
    return any(
        abs(value) == n and (value > 0) == winning
        for scope in scopes
        for value in ctx.bindings.streaks.get(scope, ())
    )


def _check_bound(
    package: StoryPackage,
    headline: str,
    body: str,
    brief: LeagueBrief | None,
    team_aliases: Mapping[int, TeamAliases],
    derived: bool,
) -> tuple[list[str], list[str]]:
    """`(gate 1b failures, gate 4 failures)`, each labelled with the entities
    the token was attributed to and the entities it is actually bound to."""
    ctx = _Context(
        package=package,
        bindings=build_bindings(package, brief, derived=derived),
        aliases=entity_aliases(package, brief, team_aliases),
        team_aliases=team_aliases,
    )
    numbers: list[str] = []
    claims: list[str] = []

    def _fail(into: list[str], what: str, scopes: set[Scope], owners: set[Scope]) -> None:
        about = ctx.labels(s for s in scopes if s[0] != "name")
        line = f"{what} -> {about}; bound to {ctx.labels(owners)}"
        if line not in into:
            into.append(line)

    for clause, scopes in _attributed_clauses(ctx, headline, body):
        # Digits inside a matched entity NAME ("Philadelphia 76ers") are part
        # of the name, not a number the clause states (gates-v2 second live
        # pass, story 3858). Only digits inside a mention's own span.
        consumed: set[int] = {
            m.start()
            for start, end, _ in _mention_spans(clause, ctx.aliases)
            for m in _NUMBER_TOKEN_RE.finditer(clause, start, end)
        }
        for m in _RECORD_RE.finditer(clause):
            pair = (int(m.group(1)), int(m.group(2)))
            if not any(pair in ctx.bindings.pairs.get(s, ()) for s in scopes):
                owners = {s for s, pairs in ctx.bindings.pairs.items() if pair in pairs}
                _fail(numbers, f"record {m.group(0)}", scopes, owners)
            consumed.update({m.start(1), m.start(2)})
        for m in _GAMES_LEFT_RE.finditer(clause):
            # C1: rejected whatever the number, because no fact of this class exists.
            _fail(claims, f"games left {m.group(0)!r}", scopes, set())
            consumed.add(m.start(1))
        streak_shapes = [
            (m, m.start(1), m.group(1), m.group(2).lower().startswith("win"))
            for m in _STREAK_GAME_RE.finditer(clause)
        ]
        streak_shapes += [
            (m, m.start(2), m.group(2), m.group(1).lower().startswith(("won", "winners")))
            for m in _STREAK_VERB_RE.finditer(clause)
        ]
        streak_shapes += [
            (m, m.start(1), m.group(1), m.group(2).lower() == "wins")
            for m in _STREAK_NOUN_RE.finditer(clause)
        ]
        for m, start, token, winning in streak_shapes:
            if not ctx.token_ok(token, scopes, _STREAK_KINDS):
                _fail(numbers, f"streak {m.group(0)!r}", scopes,
                      ctx.owners(token, _STREAK_KINDS))
            elif not _streak_direction_ok(ctx, scopes, int(token), winning):
                _fail(claims, f"streak direction {m.group(0)!r}", scopes,
                      ctx.owners(token, _STREAK_KINDS))  # C2
            consumed.add(start)
        for m in _SPOTS_RE.finditer(clause):  # C4: magnitude a typed token, direction a claim
            if not ctx.token_ok(m.group(1), scopes, _DELTA_KINDS):
                _fail(numbers, f"spots {m.group(0)!r}", scopes,
                      ctx.owners(m.group(1), _DELTA_KINDS))
            elif (move := _move_word(clause, m)) is not None and not _move_direction_ok(
                ctx, scopes, int(m.group(1)), move[1]
            ):
                _fail(claims, f"move direction {move[0]!r} {m.group(0)!r}", scopes,
                      ctx.owners(m.group(1), _DELTA_KINDS))
            consumed.add(m.start(1))
        for m in _IN_STANDINGS_RE.finditer(clause):  # C3: a place is a position, not a rank
            if not ctx.token_ok(m.group(1), scopes, _PLACE_KINDS):
                _fail(claims, f"standings place {m.group(0)!r}", scopes,
                      ctx.owners(m.group(1), _PLACE_KINDS))
                consumed.add(m.start(1))
        for m in _ORDINAL_RE.finditer(clause):
            if m.start(1) in consumed:
                continue
            if not ctx.token_ok(m.group(1), scopes, _POSITION_KINDS):
                _fail(numbers, f"ordinal {m.group(0)!r}", scopes,
                      ctx.owners(m.group(1), _POSITION_KINDS))
            consumed.add(m.start(1))
        for m in _NO_N_RE.finditer(clause):
            token = m.group(1) or m.group(2)
            start = m.start(1) if m.group(1) else m.start(2)
            if start in consumed:
                continue
            if not ctx.token_ok(token, scopes, _POSITION_KINDS):
                _fail(numbers, f"position {m.group(0)!r}", scopes,
                      ctx.owners(token, _POSITION_KINDS))
            consumed.add(start)
        for m in _NUMBER_TOKEN_RE.finditer(clause):
            if m.start() in consumed:
                continue
            if not ctx.token_ok(m.group(0), scopes, None):
                _fail(numbers, m.group(0), scopes, ctx.owners(m.group(0)))
        for regex, first in ((_FIRST_PLACE_RE, True), (_LAST_PLACE_RE, False)):  # C5
            for m in regex.finditer(clause):
                if _PLACE_NOT_CLAIMED_RE.search(clause[: m.start()]):
                    continue
                target = 1 if first else ctx.bindings.max_position
                held = {p for s in scopes for p in ctx.bindings.positions.get(s, ())}
                if target is None or target not in held:
                    owners = {s for s, ps in ctx.bindings.positions.items() if target in ps}
                    _fail(claims, repr(m.group(0)), scopes, owners)
    return numbers, claims


def verify_numbers_bound(
    package: StoryPackage,
    headline: str,
    body: str,
    *,
    brief: LeagueBrief | None = None,
    team_aliases: Mapping[int, TeamAliases] | None = None,
    derived: bool = True,
) -> GateResult:
    """Gate 1b. `derived=False` exists only for the offline evaluation's
    before/after column; the rewrite pass always binds the derived list the
    payload carries."""
    numbers, _ = _check_bound(package, headline, body, brief, team_aliases or {}, derived)
    return GateResult(False, "numbers_bound", tuple(numbers)) if numbers else _PASS


def verify_claims(
    package: StoryPackage,
    headline: str,
    body: str,
    *,
    brief: LeagueBrief | None = None,
    team_aliases: Mapping[int, TeamAliases] | None = None,
    derived: bool = True,
) -> GateResult:
    """Gate 4: C1 games left (always rejected: no fact of that class
    exists), C2 streak direction, C3 an ordinal "in the standings / place"
    must be a position, C4's direction (a climb of N spots is a negative
    delta of the attributed team, a fall a positive one), C5 first/last
    place. C4's magnitude is gate 1b's typed token. "only" and superlatives
    are NOT a class: uniqueness is unverifiable from a capped brief (G2-6),
    a disclosed blind spot."""
    _, claims = _check_bound(package, headline, body, brief, team_aliases or {}, derived)
    return GateResult(False, "claims", tuple(claims)) if claims else _PASS


# ---------------------------------------------------------------------------
# Gate 2 -- proper nouns
# ---------------------------------------------------------------------------


def _strip_possessive(word: str) -> str:
    return _POSSESSIVE_RE.sub("", word)


def _capitalized_sequences(text: str) -> list[tuple[str, bool]]:
    """`(sequence, sentence_initial)` for every multi-word capitalized run,
    possessive suffix stripped from the last word."""
    found: list[tuple[str, bool]] = []
    for match in _CAP_SEQUENCE_RE.finditer(text):
        words = match.group(0).split()
        words[-1] = _strip_possessive(words[-1])
        initial = bool(_SENTENCE_END_RE.search(text[: match.start()]))
        found.append((" ".join(words), initial))
    return found


def _closed_league_vocabulary() -> set[str]:
    """S8-10 item 4: multi-word league terms the domain enums already
    carry -- derived from `Phase`, never a hand-maintained parallel list."""
    return {p.value.replace("_", " ").title() for p in Phase if "_" in p.value}


def allowed_proper_noun_corpus(
    package: StoryPackage,
    persona: PersonaVoice,
    brief: LeagueBrief | None = None,
    team_names: Iterable[str] = (),
) -> frozenset[str]:
    """A SEARCH corpus: gate 2 checks a captured sequence is a substring of
    some member (MV-6 admits `Fact.value_text` wholesale, as a string to
    search WITHIN). Sources: package subject names, every fact's text and
    label, the cycle's team-name vocabulary, the writing persona's own
    `display_name`/`column`, `brief.allowed_proper_nouns()`, the capitalized
    sequences of the brief's four prose fields (Defect A) and the closed
    league vocabulary. `team_names` is the caller's, since the vocabulary
    is a database read this pure module cannot do."""
    corpus: set[str] = set()
    subject = package.subject
    for name in (subject.player_name, subject.team_name, subject.opponent_team_name):
        if name:
            corpus.add(name)
    for fact in package.facts:
        corpus.add(fact.value_text)
        corpus.add(fact.label)
    corpus.update(n for n in team_names if n)
    corpus.add(persona.display_name)
    corpus.add(persona.column)
    corpus |= _closed_league_vocabulary()
    if brief is not None:
        corpus |= brief.allowed_proper_nouns()
        prose_fields: list[str | None] = []
        prose_fields.extend(r.headline_fact for r in brief.other_desks)
        prose_fields.extend(r.headline for r in brief.recent_wire)
        prose_fields.extend(r.persona_column for r in brief.recent_wire)
        prose_fields.extend(r.headline for r in brief.subject_history)
        for text in prose_fields:
            if text:
                corpus.update(seq for seq, _ in _capitalized_sequences(text))
    return frozenset(corpus)


_SENTENCE_STARTERS = frozenset(
    {
        "The", "A", "An", "This", "That", "These", "Those", "Now", "Then", "Today", "Tonight",
        "Meanwhile", "After", "Before", "With", "Without", "Still", "Even", "Also", "Yet",
        "But", "And", "Amid", "Behind", "Ahead", "Following", "Despite", "Since", "Once", "As",
        "When", "While", "If", "In", "On", "At", "For", "From", "By", "So", "Both", "Another",
        "Every", "Each", "Two", "Three", "Four", "Five", "It's", "Only", "Whether", "What",
    }
)
"""Closed set of ordinary words that start sentences. A closed list, not "any
first word", on purpose: shedding an arbitrary first word would let an
invented name at the start of a sentence ("Tyrese Holloway debuts") shed its
first name and slip through as a single word. A starter this list misses
costs an over-reject (a story keeps its template), never a hole."""


def _sequence_allowed(seq: str, initial: bool, corpus: frozenset[str]) -> bool:
    if any(seq in entry for entry in corpus):
        return True
    if initial:
        first, rest = seq.split(" ", 1)
        if first in _SENTENCE_STARTERS:
            return " " not in rest or any(rest in entry for entry in corpus)
    return False


def verify_no_invented_proper_nouns(
    package: StoryPackage,
    headline: str,
    body: str,
    *,
    persona: PersonaVoice,
    brief: LeagueBrief | None = None,
    team_names: Iterable[str] = (),
) -> GateResult:
    corpus = allowed_proper_noun_corpus(package, persona, brief, team_names)
    bad: list[str] = []
    for text in (headline, body):
        for seq, initial in _capitalized_sequences(text):
            if not _sequence_allowed(seq, initial, corpus) and seq not in bad:
                bad.append(seq)
    return GateResult(False, "proper_nouns", tuple(bad)) if bad else _PASS


# ---------------------------------------------------------------------------
# Gate 3 -- quoted speech
# ---------------------------------------------------------------------------


def verify_no_quoted_speech(headline: str, body: str) -> GateResult:
    """The simulation records no spoken words, so any quotation is fabricated
    by construction. Forbids a straight or curly double-quoted SPAN, not
    attribution idiom ("Sources say ..." has no quoted span and passes).
    Also applied to the headline -- stricter than S8-10's "body" and just as
    structurally justified."""
    for text in (headline, body):
        spans = _STRAIGHT_QUOTED_RE.findall(text) + _CURLY_QUOTED_RE.findall(text)
        if spans:
            return GateResult(False, "quoted_speech", tuple(s[:40] for s in spans))
    return _PASS


def verify_story_text(
    package: StoryPackage,
    headline: str,
    body: str,
    *,
    persona: PersonaVoice,
    brief: LeagueBrief | None = None,
    team_names: Iterable[str] = (),
    team_aliases: Mapping[int, TeamAliases] | None = None,
    number_policy: NumberPolicy = NumberPolicy.BOUND,
) -> GateResult:
    """Every gate in order; the first failure wins (one failure is enough to
    leave the template row alone). `team_names` is gate 2's vocabulary and
    `team_aliases` the bound gate's (G2-2), both from the caller's one
    `list_teams_for_league` read. Under `BOUND` the number gates are 1b then
    4 (claims); under `POOLED`, gate 1."""
    if number_policy is NumberPolicy.BOUND:
        numbers, claims = _check_bound(
            package, headline, body, brief, team_aliases or {}, derived=True
        )
        number_results: tuple[GateResult, ...] = (
            GateResult(False, "numbers_bound", tuple(numbers)) if numbers else _PASS,
            GateResult(False, "claims", tuple(claims)) if claims else _PASS,
        )
    else:
        number_results = (verify_no_invented_numbers(package, headline, body, brief=brief),)
    for result in (
        *number_results,
        verify_no_invented_proper_nouns(
            package, headline, body, persona=persona, brief=brief, team_names=team_names
        ),
        verify_no_quoted_speech(headline, body),
    ):
        if not result.passed:
            return result
    return _PASS
