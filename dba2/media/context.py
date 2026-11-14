"""`dba2/media/context.py` -- docs/design/media-v2-newsroom.md MV-12/MV-13,
the pure assembly, ranking and linkage logic that turns a cycle's own
already-loaded reads into a per-story `LeagueBrief` (`dba2.domain.media_
context`). Under `dba2/media/` (not `dba2/services/`) so `test_layering.
py`'s automatic no-I/O sweep covers it, matching every other pure
detector/scoring module in this package.

**One `CycleBriefInputs`, built once per cycle by `dba2/services/media_
service.py::run_media_selection_cycle`; one `LeagueBrief` per (cycle,
subject) pair, built by `build_league_brief` below.** MV-12.1's own text
("one `LeagueBrief`, built once per cycle, ... shared by every story")
and its own per-section table both talk about a subject-relative row
("plus the subject's own team if not already present," "every OTHER
sealed candidate," MV-13's per-entry `link` field) -- both are true at
once because every EXPENSIVE thing (the reads: standings, this cycle's
scored candidate pool, the last-3-cycles wire read, last season's final
snapshot) is computed exactly ONCE and cached on `CycleBriefInputs`; only
the free, zero-I/O per-story decoration (self-exclusion, the subject's own
standings row, and every `link`/on-beat classification MV-13 requires)
varies per call to `build_league_brief`. **Total new I/O: 2 reads per
cycle, 0 per story** (MV-12.1/MV-14.4 item 4) -- this module does none of
either; both reads happen once, upstream, in `media_service.py`.

Zero I/O, the same purity rules every other module under `dba2/media/`
carries: no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.media`, `dba2.domain.
media_context`, `dba2.domain.media_facts`, `dba2.media.pulse`, `dba2.media.
salience` and stdlib.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from dba2.domain.media import MediaPulseSnapshot, MediaStory
from dba2.domain.media_context import (
    OTHER_DESKS_CAP,
    RECENT_WIRE_CAP,
    SEASON_ARC_BOTTOM_CAP,
    SEASON_ARC_TOP_CAP,
    STANDINGS_BOTTOM_CAP,
    STANDINGS_CAP,
    STANDINGS_TOP_CAP,
    SUBJECT_HISTORY_CAP,
    BriefLink,
    LeagueBrief,
    OtherDeskEntry,
    SalienceBand,
    SeasonArc,
    SeasonArcRow,
    StandingsRow,
    SubjectHistoryEntry,
    WireEntry,
)
from dba2.domain.media_facts import StoryPackage, Subject, SubjectKind
from dba2.media import pulse, salience

_IDENTITY_FACT_KEYS = frozenset(
    {"season", "phase", "team_name", "player_name", "opponent_team_name"}
)
"""Mirrors `dba2/media/write/budget.py::FACT_BOX_EXCLUDED_KEYS`
exactly, deliberately NOT imported from it: that name is private to the
writer layer, and this module (MV-12's pure assembly layer) has no
dependency on `dba2.media.write` at all. Two independently-declared,
identical closed sets -- the accepted cost of the layering, matching
`tests/unit/test_media_context_domain.py`'s own parametrized "the two sets
stay equal" check."""

_MAX_PROSE_FIELD_CHARS = 80
"""**A real, disclosed field-length clamp MV-12.1's own row-count caps do
not cover on their own.** MV-14.4 item 3's 6000-char ceiling is a real
`assert`, not a clamp -- but every ROW-COUNT cap in this module (§2's 8,
§3's 12, §5's 5) only bounds how many entries survive; nothing bounded how
LONG any one entry's own prose field could be, and two of them are
genuinely unbounded upstream: `MediaStory.headline` falls back to
`.rendered_text` for a pre-migration-024 row (bounded only by `budget.py`'s
own `description_max`, up to 1400 chars for a real `ARTICLE`), and
`_headline_fact` concatenates a raw `Fact.label`/`.value_text` pair with no
write-layer clamp at all (`budget.py`'s `field_name_max`/`field_value_max`
apply only inside `TemplateWriter._build_fact_box`, never to a bare
`Fact`). A single such row could burn a third of the whole char budget on
its own. Clamped here, at assembly time, matching `budget.py`'s own
`clamp_text` shape (truncate, never raise) -- 80 chars mirrors `_BLURB_
BUDGET.title_max`, a real, already-used "one readable line" bound in this
same codebase, not an invented number."""


def _clamp_prose(text: str, *, max_chars: int = _MAX_PROSE_FIELD_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _headline_fact(package: StoryPackage) -> str | None:
    """The first non-identity, non-universal fact in package order --
    MV-12.1 §2's "one headline fact." `None` (never fabricated) if a
    package carries nothing but identity/universal keys."""
    for fact in package.facts:
        if fact.key not in _IDENTITY_FACT_KEYS:
            return _clamp_prose(f"{fact.label}: {fact.value_text}")
    return None


def _subject_display_name(subject: Subject) -> str:
    """Mirrors `dba2/media/write/template.py::_generate_headline`'s own
    "which name is primary" rule: a `PLAYER`-subject package prefers
    `player_name`, everything else prefers `team_name` -- the identical
    fallback order, so a brief entry never disagrees with that same
    package's own rendered headline about which name is the subject's."""
    if subject.kind is SubjectKind.PLAYER:
        return subject.player_name or subject.team_name or "unknown"
    return subject.team_name or subject.player_name or "unknown"


def _story_subject_name(story: MediaStory) -> str:
    """`media_stories.fact_package` already carries `subject.player_name`/
    `.team_name` verbatim (`StoryPackage.to_prompt_json()`, persisted at
    write time) -- reading it here is the one new read's own payoff: no
    second name-resolution join against `player_repo`/`team_repo` for a
    story that may be about a since-deleted subject anyway."""
    subject = story.fact_package.get("subject") if isinstance(story.fact_package, dict) else None
    if isinstance(subject, dict):
        name = subject.get("player_name") or subject.get("team_name")
        if name:
            return name
    return "unknown"


def _link_for(
    subject: Subject, *, other_player_id: int | None, other_team_id: int | None
) -> BriefLink:
    """MV-13 point 1: a dict-lookup-shaped intersection, never an
    inference. Checked in order -- `SAME_PLAYER`, then `SAME_TEAM`, then
    `OPPONENT` -- so a `MATCHUP`-subject entry that could satisfy more than
    one reads as the most specific real match."""
    if subject.player_id is not None and other_player_id == subject.player_id:
        return BriefLink.SAME_PLAYER
    if subject.team_id is not None and other_team_id == subject.team_id:
        return BriefLink.SAME_TEAM
    if subject.opponent_team_id is not None and other_team_id == subject.opponent_team_id:
        return BriefLink.OPPONENT
    return BriefLink.NONE


def league_games_played(current_snapshot: pulse.PulseSnapshot) -> int:
    """MV-12.1 §4's "league games played" scalar -- the furthest-along
    team's own `games_played` this cycle. `0` for a snapshot with no teams
    (defensive; never true for a real cycle -- `build_pulse_snapshot`'s own
    "a team in game 0" rule keeps every real team present from cycle 0)."""
    if not current_snapshot.teams:
        return 0
    return max(t.games_played for t in current_snapshot.teams.values())


def build_standings_base(
    current_snapshot: pulse.PulseSnapshot, team_names: Mapping[int, str]
) -> tuple[StandingsRow, ...]:
    """MV-12.1 §1's cycle-scoped 8 rows -- top 5, bottom 3, by standings
    position, before any per-story "subject's own team" injection
    (`_standings_with_subject` below does that, per story, for free). A
    league with 8 or fewer teams never double-counts: `bottom` is sliced
    from whatever `top` did not already take."""
    teams = sorted(current_snapshot.teams.values(), key=lambda t: t.standings_position)
    top = teams[:STANDINGS_TOP_CAP]
    remaining = teams[STANDINGS_TOP_CAP:]
    bottom = remaining[-STANDINGS_BOTTOM_CAP:] if remaining else []
    return tuple(
        StandingsRow(
            team_id=t.team_id,
            team_name=team_names.get(t.team_id, f"Team {t.team_id}"),
            wins=t.wins,
            losses=t.losses,
            standings_position=t.standings_position,
            streak_length=t.streak_length,
        )
        for t in (*top, *bottom)
    )


def _standings_with_subject(
    base: tuple[StandingsRow, ...],
    *,
    current_snapshot: pulse.PulseSnapshot,
    team_names: Mapping[int, str],
    subject_team_id: int | None,
) -> tuple[StandingsRow, ...]:
    if subject_team_id is None or any(row.team_id == subject_team_id for row in base):
        return base
    state = current_snapshot.teams.get(subject_team_id)
    if state is None:
        return base
    extra = StandingsRow(
        team_id=state.team_id,
        team_name=team_names.get(state.team_id, f"Team {state.team_id}"),
        wins=state.wins,
        losses=state.losses,
        standings_position=state.standings_position,
        streak_length=state.streak_length,
    )
    return (*base, extra)[:STANDINGS_CAP]


def build_season_arc_rows(
    rows: Sequence[MediaPulseSnapshot], team_names: Mapping[int, str]
) -> tuple[tuple[SeasonArcRow, ...], tuple[SeasonArcRow, ...]]:
    """Last season's final top-3/bottom-1 -- MV-12.1 §4. `rows` is
    `media_repo.get_final_pulse_snapshot_for_season`'s own return for
    `season - 1`; `()` (both halves empty, never fabricated) when that
    season has no snapshot at all."""
    ordered = sorted(rows, key=lambda r: r.standings_position)
    top = ordered[:SEASON_ARC_TOP_CAP]
    remaining = ordered[SEASON_ARC_TOP_CAP:]
    bottom = remaining[-SEASON_ARC_BOTTOM_CAP:] if remaining else []

    def _row(r: MediaPulseSnapshot) -> SeasonArcRow:
        return SeasonArcRow(
            team_id=r.team_id,
            team_name=team_names.get(r.team_id, f"Team {r.team_id}"),
            wins=r.wins,
            losses=r.losses,
            standings_position=r.standings_position,
        )

    return tuple(_row(r) for r in top), tuple(_row(r) for r in bottom)


@dataclass(frozen=True)
class CycleBriefInputs:
    """Every already-loaded ingredient `build_league_brief` needs, cached
    ONCE per cycle by `run_media_selection_cycle` -- MV-12.1's "total new
    I/O: 2 reads per cycle, 0 per story" claim holds because nothing below
    is re-read per story; `build_league_brief` only ever slices, filters
    and sorts these same tuples/mappings.

    `other_desks_pool` is `PublicationPlan.main/.band/.profile`, each
    candidate tagged with the `SalienceBand` it came from -- "already in
    memory," MV-12.1's own citation for §2's source, never a second scoring
    pass. `recent_wire_pool` is every story `media_repo`'s new last-3-
    cycles read returned, each paired with its own resolved persona
    column -- shared, unfiltered, by BOTH §3 (ranked and capped) and §5
    (filtered to one subject), per MV-12.1's own "§5: sliced from §3's
    wider read, no second query.\""""

    season: int
    phase: str
    cycle_index: int
    league_games_played: int
    current_snapshot: pulse.PulseSnapshot
    team_names: Mapping[int, str]
    standings_base: tuple[StandingsRow, ...]
    other_desks_pool: tuple[tuple[salience.ScoredCandidate, SalienceBand], ...]
    recent_wire_pool: tuple[tuple[MediaStory, str], ...]
    season_arc_top: tuple[SeasonArcRow, ...] = ()
    season_arc_bottom: tuple[SeasonArcRow, ...] = ()


def build_league_brief(
    inputs: CycleBriefInputs,
    *,
    package: StoryPackage,
    persona_story_kind_weights: Mapping[str, float],
) -> LeagueBrief:
    """The one per-story assembly call -- `select_and_bind_story` calls
    this exactly once per candidate, after the persona draw (MV-13 point 2
    needs the drawn persona's own `story_kind_weights` to rank "on beat"),
    before `writer_backend.write(...)`.

    **MV-13's ranking rule, applied identically to §2 and §3** (point 1:
    "on every §2 and §3 entry"): linked entries sort ahead of unlinked
    ones; within a link class, an entry whose `story_kind` falls inside the
    WRITING persona's own declared beat (`persona_story_kind_weights[kind]
    > 0`) sorts ahead of one that does not; ties keep `other_desks_pool`'s/
    `recent_wire_pool`'s own arrival order (`sort` is stable), which is
    itself already highest-score-first for §2's `main`/`band` groups and
    newest-cycle-first for §3 (`run_media_selection_cycle`'s own read
    order -- see that function's docstring)."""
    subject = package.subject

    standings = _standings_with_subject(
        inputs.standings_base,
        current_snapshot=inputs.current_snapshot,
        team_names=inputs.team_names,
        subject_team_id=subject.team_id,
    )

    other_desks_ranked: list[tuple[bool, bool, OtherDeskEntry]] = []
    for scored, band in inputs.other_desks_pool:
        other_package = scored.candidate.package
        if other_package is package:
            continue
        link = _link_for(
            subject,
            other_player_id=other_package.subject.player_id,
            other_team_id=other_package.subject.team_id,
        )
        on_beat = persona_story_kind_weights.get(other_package.story_kind.value, 0.0) > 0.0
        entry = OtherDeskEntry(
            story_kind=other_package.story_kind.value,
            angle=other_package.angle.value,
            subject_name=_subject_display_name(other_package.subject),
            headline_fact=_headline_fact(other_package),
            salience_band=band,
            link=link,
        )
        other_desks_ranked.append((link is BriefLink.NONE, not on_beat, entry))
    other_desks_ranked.sort(key=lambda t: (t[0], t[1]))
    other_desks = tuple(entry for _, _, entry in other_desks_ranked[:OTHER_DESKS_CAP])

    recent_wire_ranked: list[tuple[bool, bool, WireEntry]] = []
    for story, persona_column in inputs.recent_wire_pool:
        link = _link_for(
            subject,
            other_player_id=story.subject_player_id,
            other_team_id=story.subject_team_id,
        )
        on_beat = persona_story_kind_weights.get(story.story_kind.value, 0.0) > 0.0
        entry = WireEntry(
            cycle_index=story.cycle_index,
            story_kind=story.story_kind.value,
            subject_name=_story_subject_name(story),
            headline=_clamp_prose(story.headline or story.rendered_text),
            persona_column=persona_column,
            link=link,
        )
        recent_wire_ranked.append((link is BriefLink.NONE, not on_beat, entry))
    recent_wire_ranked.sort(key=lambda t: (t[0], t[1]))
    recent_wire = tuple(entry for _, _, entry in recent_wire_ranked[:RECENT_WIRE_CAP])

    subject_stories = [
        story
        for story, _ in inputs.recent_wire_pool
        if (subject.player_id is not None and story.subject_player_id == subject.player_id)
        or (subject.team_id is not None and story.subject_team_id == subject.team_id)
    ]
    subject_stories.sort(key=lambda s: s.cycle_index, reverse=True)
    subject_history = tuple(
        SubjectHistoryEntry(
            cycle_index=s.cycle_index,
            story_kind=s.story_kind.value,
            headline=_clamp_prose(s.headline or s.rendered_text),
        )
        for s in subject_stories[:SUBJECT_HISTORY_CAP]
    )

    season_arc = SeasonArc(
        season=inputs.season,
        phase=inputs.phase,
        cycle_index=inputs.cycle_index,
        league_games_played=inputs.league_games_played,
        last_season_top=inputs.season_arc_top,
        last_season_bottom=inputs.season_arc_bottom,
    )

    return LeagueBrief(
        standings=standings,
        other_desks=other_desks,
        recent_wire=recent_wire,
        season_arc=season_arc,
        subject_history=subject_history,
    )
