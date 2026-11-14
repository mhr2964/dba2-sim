"""`StoryPackage` -- the typed, verified fact hand-off between the scanner
and the writer (docs/design/media-v2-newsroom.md MV-1, MV-1b, MV-1c).

**Why this lives under `domain/`, mirroring `dba2/domain/media.py`'s own
`StoryKind` docstring (Decision P4-49) exactly.** `media_repo.py` has to
persist a package's contents (`media_stories.fact_package`, a later build
step) and `presentation/wire.py` has to render one; neither may import
`dba2.media` (Decision P4-31's one-way valve, `tests/unit/test_layering.py`),
so the shape that both sides need has to live somewhere neither of those
rules forbids. `domain/` is that place, exactly as it already is for
`StoryKind` itself.

**`StoryPackage` retypes the selection spine (MV-1b) — `StoryFrame` (`dba2/
media/frame.py`) survives only inside `dba2/media/detect/legacy.py`, the
thin adapter that turns an existing, already-tuned `StoryFrame` into one of
these.** Every eleven of `StoryKind`'s CURRENT members flow through that
adapter unchanged in this build step (docs/design/media-v2-newsroom.md
"Build order" step 3a — a pure refactor, zero new kinds); the eleven new
kinds MV-2 adds are a later build step's job and are not represented in the
registries below yet.

**MV-1c's shape, restated as code rather than prose.** Revision 4 tried to
close "what may a template author declare" with a declaration CONSTRAINT
(a fallback may only use `UNIVERSAL_FACT_KEYS`); revision 5 replaced that
with a typed, closed KEY SPACE plus one sealing function, because a
constraint on declarations has to be re-proved against every new filter
dimension and a handled empty set does not. `FactType` is the piece that
makes the trigger view's "which value does a key resolve to" rule a
constant lookup instead of a per-instance branch (MV-1c.1, rule 1); the two
registries below (`FACT_KEYS_BY_KIND`, `REQUIRED_FACT_KEYS_BY_KIND`) are
what let a fallback's slots be checked against a real, per-kind
"unconditionally present" set instead of a single universal set that fails
against six of the eleven shipped fallbacks (MV-1c.1's own verified count).

Zero I/O, the same purity rules every other `domain/` module carries
(`tests/unit/test_domain_layering.py`) -- this file imports only
`dba2.domain.phase` and stdlib.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from dba2.domain.media import StoryKind
from dba2.domain.phase import Phase

# ---------------------------------------------------------------------------
# FactSource -- the real table, or pure-layer product, every Fact traces
# back to. A fact with no source cannot be constructed (MV-1's own table).
# ---------------------------------------------------------------------------


class FactSource(StrEnum):
    """Closed and listed, mirroring `StoryKind`'s own "enumerate every
    member by eye" convention. `TEAMS` (revision 2) and `PLAYERS` (this
    build step's own addition -- see below) are the two identity-lookup
    sources; every other member is a real, already-verified table or
    pure-layer product named in MV-1/MV-2's own per-fact-family tables."""

    LEAGUE_TRANSACTIONS = "league_transactions"
    MOTIVATION_EVENTS = "motivation_events"
    PLAYER_GAME_STATS = "player_game_stats"
    PLAYER_RATINGS = "player_ratings"
    TEAM_STRATEGY = "team_strategy"
    TEAM_GAME_STATS = "team_game_stats"
    GAMES = "games"
    NEED_MATRIX = "need_matrix"
    GM_HEAD_TO_HEAD = "gm_head_to_head"
    PULSE_SNAPSHOT = "pulse_snapshot"
    DRAFT_SELECTIONS = "draft_selections"
    PLAYER_SEASON_STATS = "player_season_stats"
    TEAMS = "teams"
    PLAYERS = "players"
    PERSISTED = "persisted"
    """Added for `StoryPackage.from_prompt_json`
    (docs/design/media-v2-step8-anthropic-writer.md, commit 4): `to_prompt_json`
    deliberately drops `Fact.source` (an LLM has no use for it), so a package
    rehydrated from `media_stories.fact_package` cannot know it. Rather than
    fabricate one of the real sources, a rehydrated fact says honestly that
    it came back from storage. Nothing that reads a rehydrated package (the
    prompt, the gates) looks at `source`."""
    """**Added in this build step, not named by any doc revision.** MV-1's
    own table adds `TEAMS` in revision 2 for exactly this reason ("none of
    which any existing `FactSource` member covers") but never adds a
    matching member for a NAME resolved off the `players` table --
    `identity_facts` below needs one for `player_name` for the identical
    reason `TEAMS` exists for `team_name`. A narrow, additive, same-shape
    fix to a gap the doc did not anticipate, not a design decision: it adds
    one closed-enum member, changes no existing one, and every fallback/
    trigger vocabulary check below is unaffected by its presence."""


class FactType(StrEnum):
    """The declared type of a fact KEY (not of one `Fact` instance) --
    MV-1c.1: tells the trigger view which of `Fact.value_number`/
    `value_text` a key resolves to, and tells `tools/generate_media_
    corpus.py::_FLOAT_FEATURES` whether a feature's interval may be integer-
    widened. A key has exactly one declared type across every kind that
    declares it (asserted by a pure unit test over the constants below)."""

    INT = "int"
    FLOAT = "float"
    TEXT = "text"


@dataclass(frozen=True)
class Fact:
    """One verified fact -- MV-1's own table. `key` is a machine name,
    closed per kind (`FACT_KEYS_BY_KIND` below); `label` is a human-
    readable name ("Points per 36, last 5"); `value_text` is render-ready
    (a slot's own binding, MV-1b(c)); `value_number` is the numeric reading
    a trigger/salience term reads, or `None` for a `TEXT`-only key. A fact's
    declared `FactType` constrains it (MV-1's own rule, held by a
    parametrized test over every detector in a later build step, not by a
    runtime check here -- matching this codebase's existing convention of
    validating a closed vocabulary at LOAD time, `MediaTrigger`/
    `MediaTemplate`'s own shape): an `INT` key's `value_number` is
    integer-valued (`float(int(x)) == x`), a `FLOAT` key's is non-`None`, a
    `TEXT` key's `value_number` is `None`."""

    key: str
    label: str
    value_text: str
    value_number: float | None
    source: FactSource


class SubjectKind(StrEnum):
    PLAYER = "player"
    TEAM = "team"
    MATCHUP = "matchup"


@dataclass(frozen=True)
class Subject:
    """The package's subject -- names resolved ONCE, at seal time
    (`dba2.services.media_service.seal_package`), never by a detector
    (MV-1's own table; MV-1c.1). Every field is present regardless of
    `kind`: a `signing_reaction` package is the one shape that carries both
    a player and a team id/name (MV-1c.1's own verified example), so a
    fixed `kind` marker (used for RNG-salt/attribution purposes only, see
    `dba2.services.media_service._subject_identity`) does not mean the
    OTHER identity's fields are absent."""

    kind: SubjectKind
    player_id: int | None = None
    player_name: str | None = None
    team_id: int | None = None
    team_name: str | None = None
    opponent_team_id: int | None = None
    opponent_team_name: str | None = None


class Angle(StrEnum):
    """The editorial hook -- closed and listed (MV-1's own table). Not
    every member has a producer in this build step: only `SURGE`/
    `COLLAPSE`/`STREAK`/`CAREER_CLIMB`/`ROOKIE_ARRIVAL`/`UNREST`/`HOLDOUT`
    are ever assigned by `dba2/media/detect/legacy.py` (the eleven CURRENT
    `StoryKind` members' own adapter, step 3a); the rest belong to the
    eleven new kinds a later build step adds. The old library's own
    templates declare no `angles` at all (an empty tuple, meaning
    wildcard -- MV-1b(c)), so which angle an old-kind package carries has
    zero effect on selection in this build step; it exists only so
    `StoryPackage.angle` is never left unpopulated."""

    SURGE = "surge"
    COLLAPSE = "collapse"
    STREAK = "streak"
    CAREER_CLIMB = "career_climb"
    ROOKIE_ARRIVAL = "rookie_arrival"
    UNREST = "unrest"
    HOLDOUT = "holdout"
    TRADE_MADE = "trade_made"
    PROMISE_BROKEN = "promise_broken"
    REVENGE = "revenge"
    HEATING_UP = "heating_up"
    GOING_COLD = "going_cold"
    LOCKDOWN = "lockdown"
    ROSTER_IMBALANCE = "roster_imbalance"
    SCHEME_IDENTITY = "scheme_identity"
    SCHEME_TENSION = "scheme_tension"
    RIVALRY = "rivalry"
    MARKET_WATCH = "market_watch"
    FULL_PICTURE = "full_picture"
    """`TEAM_PROFILE`'s own angle (docs/design/media-v2-newsroom.md MV-2h,
    "Build order" step 4) -- a real, necessary addition no doc revision's
    own table names. None of the other seventeen members fits a composed
    "everything true about this team right now" piece; `FULL_PICTURE` is
    the one MV-2h.4's own four-section framing describes."""


@dataclass(frozen=True)
class StoryPackage:
    """The scanner's whole output for one candidate story -- MV-1's own
    table. JSON-serializable by construction (every leaf is `str`/`float`/
    `int`/`None`), which is what lets it be persisted verbatim
    (`media_stories.fact_package`, a later build step) and handed to an LLM
    prompt with no second serializer to keep in sync. `package_version`
    defaults to `1` -- every construction site in this build step passes
    the same value, so a later prompt-contract change (MV-6) is the one
    place that bumps it, not a scatter of call-site edits."""

    story_kind: StoryKind
    angle: Angle
    subject: Subject
    facts: tuple[Fact, ...]
    season: int
    phase: Phase
    cycle_index: int
    package_version: int = 1

    def number(self, key: str) -> float | None:
        """`Fact.value_number` for `key`, or `None` if absent -- "absent is
        neutral, never a fabricated zero," the same rule everything else in
        this design follows. A linear scan over `facts` (MV-1b(a)'s own
        reasoning: a package carries at most ~12 facts, so an index would
        cost more than it saves)."""
        for fact in self.facts:
            if fact.key == key:
                return fact.value_number
        return None

    def text(self, key: str) -> str | None:
        """`Fact.value_text` for `key`, or `None` if absent -- see
        `number`'s own docstring; the two accessors share the identical
        scan-and-return-`None` shape."""
        for fact in self.facts:
            if fact.key == key:
                return fact.value_text
        return None

    @classmethod
    def from_prompt_json(cls, data: Mapping[str, object]) -> StoryPackage:
        """Inverse of `to_prompt_json()`, for the post-commit AI rewrite pass,
        which only has the persisted `media_stories.fact_package`. Every fact
        comes back with `FactSource.PERSISTED` (`source` is not serialized).
        Raises `KeyError`/`ValueError`/`TypeError` on a malformed or empty
        mapping (a pre-migration-024 row's `{}` default) rather than
        guessing; `build_payload(from_prompt_json(x).to_prompt_json())`
        reproduces `x` exactly, which a test asserts."""
        subject = data["subject"]
        return cls(
            story_kind=StoryKind(data["story_kind"]),
            angle=Angle(data["angle"]),
            subject=Subject(
                kind=SubjectKind(subject["kind"]),
                player_id=subject["player_id"],
                player_name=subject["player_name"],
                team_id=subject["team_id"],
                team_name=subject["team_name"],
                opponent_team_id=subject["opponent_team_id"],
                opponent_team_name=subject["opponent_team_name"],
            ),
            facts=tuple(
                Fact(
                    key=f["key"], label=f["label"], value_text=f["value_text"],
                    value_number=f["value_number"], source=FactSource.PERSISTED,
                )
                for f in data["facts"]
            ),
            season=data["season"],
            phase=Phase(data["phase"]),
            cycle_index=data["cycle_index"],
            package_version=data["package_version"],
        )

    def to_prompt_json(self) -> dict:
        """The exact object an LLM backend's user message serializes
        (MV-6: `json.dumps(package.to_prompt_json(), sort_keys=True)`) --
        not built or exercised by any caller in this build step (the writer
        seam is a later step), included now because it is a one-line,
        zero-risk consequence of `StoryPackage` already being flat/
        JSON-leaf-only by construction, and because it is listed as part of
        this module's own step-1 scope (docs/design/media-v2-newsroom.md
        MV-9's `dba2/domain/media_facts.py` row)."""
        return {
            "story_kind": self.story_kind.value,
            "angle": self.angle.value,
            "subject": {
                "kind": self.subject.kind.value,
                "player_id": self.subject.player_id,
                "player_name": self.subject.player_name,
                "team_id": self.subject.team_id,
                "team_name": self.subject.team_name,
                "opponent_team_id": self.subject.opponent_team_id,
                "opponent_team_name": self.subject.opponent_team_name,
            },
            "facts": [
                {
                    "key": fact.key,
                    "label": fact.label,
                    "value_text": fact.value_text,
                    "value_number": fact.value_number,
                }
                for fact in self.facts
            ],
            "season": self.season,
            "phase": self.phase.value,
            "cycle_index": self.cycle_index,
            "package_version": self.package_version,
        }


# ---------------------------------------------------------------------------
# The three registries -- MV-1c.1's "three registries, one typed key space."
# Declared here, beside `StoryPackage` itself, because `dba2/services/
# media_service.py`'s load-time validators, `dba2/media/select.py`'s
# eligibility predicate and trigger view, and every detector's own emitted-
# key test all read the SAME constants -- one closed vocabulary, not a
# scatter of per-module copies.
#
# **Scoped to the CURRENT eleven `StoryKind` members only, in this build
# step.** MV-8's own "full shipped vocabulary" table (swept against
# `seeds/media_phrases.json` directly, not sampled) is the source for every
# entry below -- it is what step 3a's acceptance test ("the existing
# library still loads") actually exercises. The richer fact families MV-8's
# OTHER table describes (e.g. `standings_position_at_last_pulse`, `wins`/
# `losses`, `from_state`/`to_state`/`axis`/`morale`) need either a threaded
# extra parameter `dba2/media/detect/legacy.py`'s thin StoryFrame adapter
# does not have, or a new detector read -- both later build steps' work,
# not this one's. Adding them now, before a real producer exists to fill
# them, would be exactly the "layer of abstraction for a hypothetical
# future need" this dispatch's own hard limits forbid.
# ---------------------------------------------------------------------------

UNIVERSAL_FACT_KEYS: Mapping[str, FactType] = MappingProxyType(
    {"season": FactType.INT, "phase": FactType.TEXT}
)
"""`{season, phase}` and nothing else (MV-1c.1, revision 5 -- revision 4's
"the subject's names, season, phase" does not exist: a `TEAM`-subject
package has no `player_name` and a `PLAYER`-subject package need not carry
a `team_name`; the names live in `REQUIRED_FACT_KEYS_BY_KIND`, per kind)."""

FACT_KEYS_BY_KIND: Mapping[StoryKind, Mapping[str, FactType]] = MappingProxyType(
    {
        StoryKind.STANDINGS_SURGE: MappingProxyType(
            {
                "standings_position_delta": FactType.INT,
                "standings_position": FactType.INT,
                "team_name": FactType.TEXT,
            }
        ),
        StoryKind.STANDINGS_COLLAPSE: MappingProxyType(
            {
                "standings_position_delta": FactType.INT,
                "standings_position": FactType.INT,
                "team_name": FactType.TEXT,
            }
        ),
        StoryKind.STREAK: MappingProxyType(
            {"streak_length": FactType.INT, "team_name": FactType.TEXT}
        ),
        StoryKind.TRADE_REACTION: MappingProxyType(
            {
                "trade_in_players": FactType.INT,
                "trade_out_players": FactType.INT,
                "trade_in_picks": FactType.INT,
                "team_name": FactType.TEXT,
            }
        ),
        StoryKind.TRADE_FALLOUT: MappingProxyType({"team_name": FactType.TEXT}),
        StoryKind.SIGNING_REACTION: MappingProxyType(
            {"team_name": FactType.TEXT, "player_name": FactType.TEXT}
        ),
        StoryKind.MILESTONE: MappingProxyType({"player_name": FactType.TEXT}),
        StoryKind.AWARD_RACE: MappingProxyType({"player_name": FactType.TEXT}),
        StoryKind.ROOKIE_WATCH: MappingProxyType({"player_name": FactType.TEXT}),
        StoryKind.PLAYER_UNREST: MappingProxyType(
            {
                "motivation_magnitude": FactType.INT,
                "request_state": FactType.TEXT,
                "player_name": FactType.TEXT,
            }
        ),
        StoryKind.HOLDOUT_WATCH: MappingProxyType(
            {
                "motivation_magnitude": FactType.INT,
                "request_state": FactType.TEXT,
                "player_name": FactType.TEXT,
            }
        ),
        # -------------------------------------------------------------
        # MV-2's eleven new kinds (docs/design/media-v2-newsroom.md,
        # "Build order" step 4) -- added incrementally, one detector
        # module at a time, so each kind's fact vocabulary is decided
        # against its own detector's real logic, not guessed in bulk.
        # -------------------------------------------------------------
        StoryKind.TRADE_EXECUTED: MappingProxyType(
            {
                "team_name": FactType.TEXT,
                "opponent_team_name": FactType.TEXT,
                "players_acquired_count": FactType.INT,
                "players_sent_count": FactType.INT,
                "picks_acquired_count": FactType.INT,
                "picks_sent_count": FactType.INT,
                "asset_count_edge": FactType.INT,
                "human_negotiated": FactType.INT,
                "ai_executed": FactType.INT,
            }
        ),
        StoryKind.TRADE_BROKEN_PROMISE: MappingProxyType(
            {
                "player_name": FactType.TEXT,
                "team_name": FactType.TEXT,
                "opponent_team_name": FactType.TEXT,
                "got_his_wish": FactType.INT,
                "satisfaction_loyalty": FactType.INT,
                "weight_loyalty": FactType.INT,
                "trade_season": FactType.INT,
            }
        ),
        StoryKind.HOT_STREAK: MappingProxyType(
            {
                "player_name": FactType.TEXT,
                "window_points_per36": FactType.FLOAT,
                "baseline_points_per36": FactType.FLOAT,
                "window_true_shooting": FactType.FLOAT,
                "baseline_true_shooting": FactType.FLOAT,
                "attribution_three_point_volume": FactType.FLOAT,
                "attribution_rim_pressure": FactType.FLOAT,
                "attribution_free_throws": FactType.FLOAT,
                "attribution_playmaking": FactType.FLOAT,
            }
        ),
        StoryKind.COLD_STRETCH: MappingProxyType(
            {
                "player_name": FactType.TEXT,
                "window_points_per36": FactType.FLOAT,
                "baseline_points_per36": FactType.FLOAT,
                "window_true_shooting": FactType.FLOAT,
                "baseline_true_shooting": FactType.FLOAT,
            }
        ),
        StoryKind.DEFENSIVE_STANDOUT: MappingProxyType(
            {
                "player_name": FactType.TEXT,
                "window_stocks_per36": FactType.FLOAT,
                "baseline_stocks_per36": FactType.FLOAT,
                "attribution_on_ball_defense": FactType.FLOAT,
                "attribution_rim_protection": FactType.FLOAT,
                "attribution_defensive_rebounding": FactType.FLOAT,
            }
        ),
        StoryKind.ROSTER_CONSTRUCTION: MappingProxyType(
            {
                "team_name": FactType.TEXT,
                "biggest_need_position": FactType.TEXT,
                "biggest_need_value": FactType.FLOAT,
                "biggest_glut_position": FactType.TEXT,
                "biggest_glut_value": FactType.FLOAT,
                "stark_need_position": FactType.TEXT,
                "stark_need_value": FactType.FLOAT,
                "stark_need_supply": FactType.FLOAT,
                "stark_glut_position": FactType.TEXT,
                "stark_glut_value": FactType.FLOAT,
                "aging_core_mean_age": FactType.FLOAT,
                "starless_win_now_count": FactType.INT,
                "philosophy_mismatch": FactType.TEXT,
            }
        ),
        StoryKind.RIVALRY_MATCHUP: MappingProxyType(
            {
                "team_name": FactType.TEXT,
                "opponent_team_name": FactType.TEXT,
                "rivalry_score": FactType.INT,
                "team_wins_this_series": FactType.INT,
                "opponent_wins_this_series": FactType.INT,
                "closest_margin": FactType.INT,
                "team_standings_position": FactType.INT,
                "opponent_standings_position": FactType.INT,
                "scheduled_game_date": FactType.TEXT,
                "team_named_players": FactType.TEXT,
                "opponent_named_players": FactType.TEXT,
            }
        ),
        StoryKind.SCHEME_IDENTITY: MappingProxyType(
            {
                "team_name": FactType.TEXT,
                "offensive_scheme": FactType.TEXT,
                "defensive_scheme": FactType.TEXT,
                "pace_target": FactType.INT,
                "pace_rank": FactType.INT,
                "avg_possessions": FactType.FLOAT,
                "avg_possessions_rank": FactType.INT,
                "three_point_bias": FactType.INT,
                "three_share": FactType.FLOAT,
                "three_share_rank": FactType.INT,
                "set_by_a_person": FactType.INT,
                "tension": FactType.TEXT,
                "league_size": FactType.INT,
            }
        ),
        StoryKind.TRADE_MARKET_WATCH: MappingProxyType(
            {
                "player_name": FactType.TEXT,
                "team_name": FactType.TEXT,
                "request_state": FactType.TEXT,
                "team_glut_position": FactType.TEXT,
                "team_glut_value": FactType.FLOAT,
                "league_biggest_need_value": FactType.FLOAT,
            }
        ),
        StoryKind.REVENGE_GAME: MappingProxyType(
            {
                "player_name": FactType.TEXT,
                "team_name": FactType.TEXT,
                "opponent_team_name": FactType.TEXT,
                "trade_season": FactType.INT,
                "trade_asset_count": FactType.INT,
                "player_games_played_this_season": FactType.INT,
                "team_standings_position": FactType.INT,
                "opponent_standings_position": FactType.INT,
                "scheduled_game_date": FactType.TEXT,
                "got_his_wish": FactType.INT,
            }
        ),
        StoryKind.TEAM_PROFILE: MappingProxyType(
            {
                "team_name": FactType.TEXT,
                # 1. Where they stand.
                "wins": FactType.INT,
                "losses": FactType.INT,
                "standings_position": FactType.INT,
                "streak_length": FactType.INT,
                "standings_position_delta": FactType.INT,
                # 2. What they're built on.
                "mean_age_top8": FactType.FLOAT,
                "biggest_need_position": FactType.TEXT,
                "biggest_need_value": FactType.FLOAT,
                "biggest_glut_position": FactType.TEXT,
                "biggest_glut_value": FactType.FLOAT,
                # 3. How they play.
                "offensive_scheme": FactType.TEXT,
                "defensive_scheme": FactType.TEXT,
                "three_point_bias": FactType.INT,
                "three_share": FactType.FLOAT,
                "pace_target": FactType.INT,
                "avg_possessions": FactType.FLOAT,
                "set_by_a_person": FactType.INT,
                "scheme_tension": FactType.TEXT,
                # 4. Where they're going.
                "timeline": FactType.TEXT,
                "gm_philosophy": FactType.TEXT,
                "trades_in_count": FactType.INT,
                "trades_out_count": FactType.INT,
                "unrest_player_count": FactType.INT,
                "next_game_date": FactType.TEXT,
                "next_game_is_rivalry": FactType.INT,
                # 5. Last year (conditional).
                "prior_wins": FactType.INT,
                "prior_losses": FactType.INT,
                "prior_standings_position": FactType.INT,
            }
        ),
    }
)
"""Every key a package of that kind MAY carry, with its declared type --
the trigger vocabulary, the slot vocabulary and the honesty allowlist all
read this (MV-1b(a)/MV-1c.1). Swept from MV-8's own "full shipped
vocabulary" table, which lists every `feature`/`slot` `seeds/
media_phrases.json` actually declares, per kind."""

REQUIRED_FACT_KEYS_BY_KIND: Mapping[StoryKind, frozenset[str]] = MappingProxyType(
    {
        StoryKind.STANDINGS_SURGE: frozenset({"team_name"}),
        StoryKind.STANDINGS_COLLAPSE: frozenset({"team_name"}),
        StoryKind.STREAK: frozenset({"team_name"}),
        StoryKind.TRADE_REACTION: frozenset({"team_name"}),
        StoryKind.TRADE_FALLOUT: frozenset({"team_name"}),
        StoryKind.SIGNING_REACTION: frozenset({"team_name", "player_name"}),
        StoryKind.MILESTONE: frozenset({"player_name"}),
        StoryKind.AWARD_RACE: frozenset({"player_name"}),
        StoryKind.ROOKIE_WATCH: frozenset({"player_name"}),
        StoryKind.PLAYER_UNREST: frozenset({"player_name"}),
        StoryKind.HOLDOUT_WATCH: frozenset({"player_name"}),
        # MV-2's eleven new kinds, added incrementally (see FACT_KEYS_BY_KIND's
        # own matching comment above).
        StoryKind.TRADE_EXECUTED: frozenset({"team_name", "opponent_team_name"}),
        StoryKind.TRADE_BROKEN_PROMISE: frozenset({"player_name", "team_name"}),
        StoryKind.HOT_STREAK: frozenset({"player_name"}),
        StoryKind.COLD_STRETCH: frozenset({"player_name"}),
        StoryKind.DEFENSIVE_STANDOUT: frozenset({"player_name"}),
        StoryKind.ROSTER_CONSTRUCTION: frozenset({"team_name"}),
        StoryKind.RIVALRY_MATCHUP: frozenset({"team_name", "opponent_team_name"}),
        StoryKind.SCHEME_IDENTITY: frozenset({"team_name"}),
        StoryKind.TRADE_MARKET_WATCH: frozenset({"player_name"}),
        StoryKind.REVENGE_GAME: frozenset({"player_name", "team_name", "opponent_team_name"}),
        StoryKind.TEAM_PROFILE: frozenset({"team_name"}),
    }
)
"""A subset of `FACT_KEYS_BY_KIND`: the keys every package of that kind
carries UNCONDITIONALLY (MV-1c.1). This is the constant a fallback's
`slots` are checked against at load time, and the constant `seal_package`
checks before sealing -- a package missing one of its kind's `REQUIRED`
keys is never sealed (returns `None`, MV-1c.1)."""


_PACKAGE_PREFIX = "package_"
"""No real fact key may begin with this -- MV-1c.1 rule 4: the trigger
view's own package-level pseudo-features (`package_story_kind`,
`package_angle`, `package_fact_count`) are namespaced under this prefix
specifically so a real fact key can never collide with one. Enforced by a
pure unit test over the two registries above, not at construction time
(matching every other closed-vocabulary check in this codebase, which
lives at load/test time, not in a dataclass `__post_init__`)."""


_OPPONENT_TEAM_NAME_LABEL_BY_KIND: Mapping[StoryKind, str] = MappingProxyType(
    {StoryKind.TRADE_BROKEN_PROMISE: "Former team"}
)
"""Nit 10 (critic round on commits `b288b8e`..`99db9f4`): `identity_facts`
used to hardcode `label="Opponent"` for every `opponent_team_name` fact,
which is simply wrong for `TRADE_BROKEN_PROMISE` -- `trades.py::trade_
broken_promise_package` sets `opponent_team_id=old_team_id`, the player's
FORMER team, not a game opponent (`trades.py`'s own `Subject(...,
team_id=new_team_id, opponent_team_id=old_team_id)` construction). Every
OTHER kind that carries this key (`TRADE_EXECUTED`, `RIVALRY_MATCHUP`,
`REVENGE_GAME`) really is a game/trade opponent, so `"Opponent"` stays the
default; this maps the one kind that needs a different word to it, rather
than adding a per-kind branch inline at the one call site."""


def opponent_team_name_is_game_opponent(story_kind: StoryKind) -> bool:
    """Whether `opponent_team_name`, on a package of this kind, names a
    genuine matchup/game opponent -- the signal `dba2/media/write/
    template.py::_generate_headline` needs to gate its own "X vs. Y"
    framing (critic round on commits `ea84c5f`..`665526d`, bug B4.1):
    before this fix that function used "`opponent_team_name` is present"
    alone as its trigger, which fired for `TRADE_BROKEN_PROMISE` too and
    produced a real, wrong render (`"Miami Heat vs. Chicago Bulls: trade
    broken promise"` for a trade story with no game in it at all --
    `opponent_team_name` there names a FORMER team, never an opponent in
    any game or trade sense).

    Reuses `_OPPONENT_TEAM_NAME_LABEL_BY_KIND` as the one, single source of
    truth for this question rather than a second, separately-maintained
    list of "kinds where opponent_team_name is a real opponent" that could
    silently drift out of sync with that table's own overrides: a kind
    gets an override in that table specifically because its `opponent_
    team_name` is NOT a game opponent, so "does this kind's label read as
    the default `'Opponent'`" and "is this kind's opponent a game
    opponent" are the identical question, asked two different ways."""
    return _OPPONENT_TEAM_NAME_LABEL_BY_KIND.get(story_kind, "Opponent") == "Opponent"


def identity_facts(subject: Subject, story_kind: StoryKind) -> tuple[Fact, ...]:
    """Derives the `team_name`/`player_name`/`opponent_team_name` `Fact`s
    from an ALREADY-RESOLVED `Subject` -- MV-1c.1: "the identity facts that
    mirror [`Subject`'s name fields] are built by the same one function that
    seals the package, never by a detector." Pure: this function never
    resolves a name itself, only mirrors one already on `subject`.
    `Subject.player_name` and the returned `player_name` fact are therefore
    always the same string, because one function (this one, called from
    `dba2.services.media_service.seal_package`) produced both from one
    source -- "there is nothing to keep in sync" (MV-1c.1's own closing
    line). Absent is neutral: a `None` name emits no fact for that key,
    never a fabricated placeholder string.

    `story_kind` is used ONLY to pick the `opponent_team_name` fact's
    human-readable `label` (`_OPPONENT_TEAM_NAME_LABEL_BY_KIND`, Nit 10) --
    it does not gate which facts are emitted; that is still governed
    entirely by which of `subject`'s name fields are non-`None`.

    **`opponent_team_name` (docs/design/media-v2-newsroom.md MV-2, "Build
    order" step 4) is a real, necessary extension, not part of any doc
    revision's own text.** A `MATCHUP` subject (`TRADE_EXECUTED`,
    `RIVALRY_MATCHUP`, `REVENGE_GAME`) needs BOTH sides named in a template,
    and `Subject` already carries `opponent_team_id`/`opponent_team_name`
    (resolved by `seal_package` exactly like the primary team) -- but this
    function, before this change, only ever mirrored two of `Subject`'s four
    name fields, so the second team's name had nowhere to reach a
    template's `slots` at all. Same shape, same rule: absent is neutral."""
    facts: list[Fact] = []
    if subject.team_name is not None:
        facts.append(
            Fact(
                key="team_name",
                label="Team",
                value_text=subject.team_name,
                value_number=None,
                source=FactSource.TEAMS,
            )
        )
    if subject.player_name is not None:
        facts.append(
            Fact(
                key="player_name",
                label="Player",
                value_text=subject.player_name,
                value_number=None,
                source=FactSource.PLAYERS,
            )
        )
    if subject.opponent_team_name is not None:
        facts.append(
            Fact(
                key="opponent_team_name",
                label=_OPPONENT_TEAM_NAME_LABEL_BY_KIND.get(story_kind, "Opponent"),
                value_text=subject.opponent_team_name,
                value_number=None,
                source=FactSource.TEAMS,
            )
        )
    return tuple(facts)
