"""Which entity every allowed number is about
(docs/design/media-v2-step8-gates-v2.md G2-1, G2-2, G2-4, G2-7).

**Why this exists.** The AI writer's number gate used to accept a token if
it equalled ANY number in the story's package or league brief: a record
quoted next to "Lakers" passed when it was Denver's. The fix is to know,
for every allowed number, whose number it is (its *scope*) and what it is
(its *kind*), so the gate can ask whether the sentence the number sits in
is about that entity. This module builds that table; `dba2/media/write/
verify.py` uses it. It also computes the closed list of derived numbers
(`derived_facts`) that the prompt payload shows the model and the gate
accepts, from ONE function, so the model is shown exactly the numbers the
gate will accept and nothing else is widened.

**Every binding source is a verbatim DB value or a derivation of two of
them** (MV-12.4): nothing false enters the table. A missing source is a
false reject, never a false accept, which is the direction every choice
here is resolved in.

Pure: stdlib + `dba2.domain` only. Nothing here reads a database; team
aliases are built by the caller (`media_service`) from the team rows it
already loads for gate 2.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from dba2.domain.media_context import LeagueBrief
from dba2.domain.media_facts import StoryPackage
from dba2.domain.team import Team

Scope = tuple[object, ...]
"""`("team", team_id)`, `("player", player_id)`, `("name", subject_name)`
for a brief entry that carries no id, or `LEAGUE`."""

LEAGUE: Scope = ("league",)
"""Numbers about nobody (season, cycle, league games played, league size):
accepted in any clause, untyped (G2-3 rule 5)."""

_NUMBER_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")
"""The same token regex gate 1 tokenizes a body with."""


# ---------------------------------------------------------------------------
# Kinds and scopes of package facts (G2-4) -- a total function over the
# closed key space, asserted total by a test over `FACT_KEYS_BY_KIND`.
# ---------------------------------------------------------------------------


class ScopeRule(StrEnum):
    LEAGUE = "league"
    TWO_SIDED = "two_sided"
    OPPONENT = "opponent"
    PRIMARY = "primary"


LEAGUE_KEYS = frozenset({"season", "phase", "trade_season", "league_size", "cycle_index"})
TWO_SIDED_KEYS = frozenset(
    {"closest_margin", "rivalry_score", "asset_count_edge", "scheduled_game_date"}
)


def scope_rule(key: str) -> ScopeRule:
    """First match wins, G2-4's table. Every key has a rule (the default is
    PRIMARY), so a new key can never bind to nothing."""
    if key in LEAGUE_KEYS:
        return ScopeRule.LEAGUE
    if key in TWO_SIDED_KEYS:
        return ScopeRule.TWO_SIDED
    if key.startswith("opponent_"):
        return ScopeRule.OPPONENT
    return ScopeRule.PRIMARY


def fact_kind(key: str) -> str:
    """What the number IS; used only by typed tokens and claim classes (an
    untyped body token matches any kind)."""
    if key.endswith("standings_position"):
        return "position"
    if key.endswith("_rank"):
        return "rank"
    if key == "streak_length":
        return "streak"
    if key == "standings_position_delta":
        return "delta"
    if key in ("wins", "prior_wins"):
        return "wins"
    if key in ("losses", "prior_losses"):
        return "losses"
    if key == "team_wins_this_series":
        return "series_w"
    if key == "opponent_wins_this_series":
        return "series_l"
    return "value"


def primary_scope(package: StoryPackage) -> Scope:
    subject = package.subject
    if subject.player_id is not None:
        return ("player", subject.player_id)
    if subject.team_id is not None:
        return ("team", subject.team_id)
    return LEAGUE


def fact_scopes(package: StoryPackage, key: str) -> tuple[Scope, ...]:
    subject = package.subject
    primary = primary_scope(package)
    team = ("team", subject.team_id) if subject.team_id is not None else None
    opponent = ("team", subject.opponent_team_id) if subject.opponent_team_id is not None else None
    rule = scope_rule(key)
    if rule is ScopeRule.LEAGUE:
        return (LEAGUE,)
    if rule is ScopeRule.TWO_SIDED:
        return tuple(s for s in (team or primary, opponent) if s is not None)
    if rule is ScopeRule.OPPONENT and opponent is not None:
        return (opponent,)
    if subject.player_id is not None and key.startswith("team_") and team is not None:
        return (primary, team)
    return (primary,)


def rendered_forms(value: float) -> set[str]:
    """Gate 1's renderings (0/1/2/3 decimals plus the integer part, MV-6)
    PLUS the magnitude of a negative number: "lost 4 straight" cites
    `streak_length = -4` and "up 3 spots" cites a delta of -3 (G2-1)."""
    forms = {str(int(value)), f"{value:.0f}", f"{value:.1f}", f"{value:.2f}", f"{value:.3f}"}
    if value < 0:
        forms |= rendered_forms(-value)
    return forms


# ---------------------------------------------------------------------------
# Derived facts (G2-7) -- the closed list, computed at prompt time.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedFact:
    """Fact-shaped, plus `about`. `value_text` is the rendered string the
    payload carries (never a float repr, so the payload bytes are
    platform-stable); `value_number` is what the gate renders.
    `counterpart` is the other side of a two-entity derivation (the leader a
    team is games behind): the gate binds the number to it too, the same
    two-sided rule as `closest_margin`, because copy states it in a clause
    that names the leader ("7.0 games behind first-place Philadelphia", the
    live run's most common false reject). Not serialized."""

    key: str
    label: str
    value_text: str
    value_number: float
    about: Scope
    counterpart: Scope | None = None

    def to_prompt_json(self) -> dict:
        kind, ident = self.about
        return {
            "key": self.key,
            "label": self.label,
            "value_text": self.value_text,
            "about": {f"{kind}_id": ident},
        }


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def standings_move(package: StoryPackage) -> tuple[int, int] | None:
    """`(position at the last pulse, position now)`, from the package's own
    two facts: `standings_position_delta` is `current - prior`, so the prior
    position is `current - delta`. `None` when either is absent."""
    current = package.number("standings_position")
    delta = package.number("standings_position_delta")
    if current is None or delta is None:
        return None
    return int(current - delta), int(current)


def standings_move_phrase(prior: int, current: int) -> str:
    """The move in plain words. A smaller position is higher in the table,
    so a positive delta is a FALL; the gates-v2 live run showed the model
    reading the signed delta the other way in 6 of 8 stories, so the payload
    states the direction and hides the sign (`media_prompt.build_payload`)."""
    if prior == current:
        return f"held at {ordinal(current)}"
    n = abs(current - prior)
    verb = "rose" if current < prior else "fell"
    return f"{verb} {n} spot{'s' if n != 1 else ''}, from {ordinal(prior)} to {ordinal(current)}"


def derived_facts(package: StoryPackage, brief: LeagueBrief | None) -> tuple[DerivedFact, ...]:
    """The numbers a reader wants and the fence forbids the model to compute:
    games played, win percentage and games behind for each brief standings
    row; the window-minus-baseline scoring and efficiency changes of a hot or
    cold streak; season-series games played; the subject's standings move
    since the last pulse, direction in words. Nothing else, until a live run
    shows a specific missing one (G2-7)."""
    out: list[DerivedFact] = []
    subject = package.subject
    primary = primary_scope(package)
    move = standings_move(package)
    if move is not None:
        prior, current = move
        out.append(DerivedFact("standings_move", "Standings move since last pulse",
                               standings_move_phrase(prior, current),
                               float(abs(current - prior)), primary))
    for window, baseline, key, label in (
        ("window_points_per36", "baseline_points_per36", "points_per36_delta",
         "Points per 36, last 5 minus rest of season"),
        ("window_true_shooting", "baseline_true_shooting", "true_shooting_delta",
         "True shooting, last 5 minus rest of season"),
    ):
        w, b = package.number(window), package.number(baseline)
        if w is not None and b is not None:
            delta = round(w - b, 3)
            out.append(DerivedFact(key, label, f"{delta:.3f}", delta, primary))
    team_series = package.number("team_wins_this_series")
    opp_series = package.number("opponent_wins_this_series")
    if team_series is not None and opp_series is not None:
        played = int(team_series) + int(opp_series)
        for team_id in (subject.team_id, subject.opponent_team_id):
            if team_id is not None:
                out.append(DerivedFact("series_games_played", "Season series games played",
                                       str(played), float(played), ("team", team_id)))
    if brief is not None:
        leader = next((r for r in brief.standings if r.standings_position == 1), None)
        for row in brief.standings:
            about: Scope = ("team", row.team_id)
            played = row.wins + row.losses
            out.append(DerivedFact("games_played", "Games played", str(played),
                                   float(played), about))
            if played:
                pct = round(row.wins / played, 3)
                out.append(DerivedFact("win_pct", "Win percentage", f"{pct:.3f}", pct, about))
            if leader is not None and row.team_id != leader.team_id:
                behind = ((leader.wins - leader.losses) - (row.wins - row.losses)) / 2
                out.append(DerivedFact("games_behind", "Games behind first place",
                                       f"{behind:.1f}", behind, about,
                                       counterpart=("team", leader.team_id)))
    return tuple(out)


# ---------------------------------------------------------------------------
# The binding table (G2-1)
# ---------------------------------------------------------------------------


@dataclass
class Bindings:
    """`tokens`: body token -> {(scope, kind)}. `pairs`: record-shaped pairs
    per scope (a record, a season series from each side). `streaks` and
    `positions`: signed streaks and current standings positions per scope,
    for the claim classes. `deltas`: signed standings position deltas
    (`current - prior`, so positive is a fall) per scope, for C4's direction.
    `max_position`: the largest current position the story was shown (for
    "last place")."""

    tokens: dict[str, set[tuple[Scope, str]]] = field(default_factory=lambda: defaultdict(set))
    pairs: dict[Scope, set[tuple[int, int]]] = field(default_factory=lambda: defaultdict(set))
    streaks: dict[Scope, set[int]] = field(default_factory=lambda: defaultdict(set))
    positions: dict[Scope, set[int]] = field(default_factory=lambda: defaultdict(set))
    deltas: dict[Scope, set[int]] = field(default_factory=lambda: defaultdict(set))
    max_position: int | None = None

    def add_text(self, scope: Scope, kind: str, text: str | None) -> None:
        if text:
            for token in _NUMBER_TOKEN_RE.findall(text):
                self.tokens[token].add((scope, kind))

    def add_number(
        self, scope: Scope, kind: str, value: float | None, *, current: bool = True
    ) -> None:
        """`current=False` for last season's numbers: typed tokens still see
        them, the claim classes (which are about now) do not."""
        if value is None:
            return
        for token in rendered_forms(float(value)):
            self.tokens[token].add((scope, kind))
        if kind == "streak" and current:
            self.streaks[scope].add(int(value))
        elif kind == "position" and current:
            self.positions[scope].add(int(value))
        elif kind == "delta" and current:
            self.deltas[scope].add(int(value))

    def scopes_for(self, token: str) -> set[tuple[Scope, str]]:
        return self.tokens.get(token, set())


def build_bindings(
    package: StoryPackage, brief: LeagueBrief | None, *, derived: bool = True
) -> Bindings:
    """Every number the story was shown, bound to the entity it is about.
    `derived=False` exists only for the offline evaluation's before/after
    column (`tools/media_gate_eval.py --no-derived`)."""
    b = Bindings()
    subject = package.subject
    primary = primary_scope(package)
    team = ("team", subject.team_id) if subject.team_id is not None else primary
    opponent = ("team", subject.opponent_team_id) if subject.opponent_team_id is not None else None
    for fact in package.facts:
        kind = fact_kind(fact.key)
        # Last season's position is a position, but not a CURRENT one (C5).
        current = fact.key != "prior_standings_position"
        for scope in fact_scopes(package, fact.key):
            b.add_text(scope, kind, fact.value_text)
            b.add_number(scope, kind, fact.value_number, current=current)
            b.add_text(scope, "label", fact.label)
    b.add_number(LEAGUE, "value", float(package.season))
    b.add_number(LEAGUE, "value", float(package.season - 1))

    def _pair(scope: Scope, first: float | None, second: float | None) -> None:
        if first is not None and second is not None:
            b.pairs[scope].add((int(first), int(second)))

    _pair(primary, package.number("wins"), package.number("losses"))
    _pair(primary, package.number("prior_wins"), package.number("prior_losses"))
    series_team = package.number("team_wins_this_series")
    series_opp = package.number("opponent_wins_this_series")
    _pair(team, series_team, series_opp)
    if opponent is not None:
        _pair(opponent, series_opp, series_team)

    if brief is not None:
        for row in brief.standings:
            scope: Scope = ("team", row.team_id)
            b.add_number(scope, "wins", row.wins)
            b.add_number(scope, "losses", row.losses)
            b.add_number(scope, "position", row.standings_position)
            b.add_number(scope, "streak", row.streak_length)
            b.pairs[scope].add((row.wins, row.losses))
        arc = brief.season_arc
        for value in (arc.season, arc.cycle_index, arc.league_games_played):
            b.add_number(LEAGUE, "value", value)
        for row in (*arc.last_season_top, *arc.last_season_bottom):
            scope = ("team", row.team_id)
            b.add_number(scope, "prior_wins", row.wins)
            b.add_number(scope, "prior_losses", row.losses)
            b.add_number(scope, "position", row.standings_position, current=False)
            b.pairs[scope].add((row.wins, row.losses))
        for desk in brief.other_desks:
            b.add_text(("name", desk.subject_name), "value", desk.headline_fact)
        for wire in brief.recent_wire:
            b.add_text(("name", wire.subject_name), "value", wire.headline)
            b.add_text(("name", wire.subject_name), "value", wire.persona_column)
            b.add_number(LEAGUE, "value", wire.cycle_index)
        for entry in brief.subject_history:
            b.add_text(primary, "value", entry.headline)
            b.add_number(LEAGUE, "value", entry.cycle_index)
        positions = [r.standings_position for r in brief.standings]
        b.max_position = max(positions) if positions else None

    if derived:
        for fact in derived_facts(package, brief):
            b.add_number(fact.about, "value", fact.value_number)
            if fact.counterpart is not None:
                b.add_number(fact.counterpart, "value", fact.value_number)
        move = standings_move(package)
        if move is not None:
            # The move phrase's "from 19th": a position, but not a current one (C5).
            b.add_number(primary, "position", move[0], current=False)
    return b


# ---------------------------------------------------------------------------
# Entity aliases (G2-2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TeamAliases:
    """The three ways copy names a team. `city` may be shared by two teams
    ("Los Angeles"); a shared city resolves to every team that has it."""

    full: str
    city: str
    nickname: str


def team_aliases_for(teams: Iterable[Team]) -> dict[int, TeamAliases]:
    """From the rows `team_repo.list_teams_for_league` already returns; the
    full name is `f"{city} {name}"`, exactly as `seal_package` builds it."""
    return {t.id: TeamAliases(full=f"{t.city} {t.name}", city=t.city, nickname=t.name)
            for t in teams}


def entity_aliases(
    package: StoryPackage,
    brief: LeagueBrief | None,
    team_aliases: Mapping[int, TeamAliases],
) -> tuple[tuple[str, Scope], ...]:
    """Closed and derived, never hand-written: every league team's full name,
    nickname and city; the subject player's full and last name; each named
    player of a matchup package (full and last name) as an alias OF HIS TEAM;
    a brief entry's `subject_name` verbatim for its `("name", ...)` scope.
    First names alone are not aliases (a disclosed blind spot)."""
    out: list[tuple[str, Scope]] = []
    for team_id, names in team_aliases.items():
        for alias in (names.full, names.nickname, names.city):
            if alias:
                out.append((alias, ("team", team_id)))
    subject = package.subject
    if subject.player_name and subject.player_id is not None:
        out.append((subject.player_name, ("player", subject.player_id)))
        last = subject.player_name.split()[-1]
        if last != subject.player_name:
            out.append((last, ("player", subject.player_id)))
    for key, team_id in (
        ("team_named_players", subject.team_id),
        ("opponent_named_players", subject.opponent_team_id),
    ):
        text = package.text(key)
        if text and team_id is not None:
            for name in (n.strip() for n in text.split(",")):
                if name:
                    out.append((name, ("team", team_id)))
                    out.append((name.split()[-1], ("team", team_id)))
    if brief is not None:
        for entry in (*brief.other_desks, *brief.recent_wire):
            out.append((entry.subject_name, ("name", entry.subject_name)))
    return tuple(dict.fromkeys(out))


def scope_name(
    scope: Scope, package: StoryPackage, team_aliases: Mapping[int, TeamAliases]
) -> str | None:
    """The `subject_name` a brief entry would carry for this entity, so a
    mention of "Lakers" also reaches a `("name", "Los Angeles Lakers")` entry."""
    if scope[0] == "team":
        names = team_aliases.get(scope[1])  # type: ignore[call-overload]
        return names.full if names is not None else None
    if scope[0] == "player" and scope[1] == package.subject.player_id:
        return package.subject.player_name
    return None
