"""`dba2/services/media_service.py` -- Stage D step 17's DB-backed caller:
builds the current cycle's team-level pulse snapshot from `games`
(Decision P4-30: standings/streak are computed HERE, `window.py` stays
untouched), reads real ladder-transition events through `dba2/services/
agency_service.py`'s own read-model pattern (Decision P4-16, extended),
and combines both with `dba2/media/pulse.py`'s diff and `dba2/media/
salience.py`'s scoring/top-K.

docs/design/phase-4-media-and-player-agency.md §3 (module layout), §7
(P4-27/P4-28/P4-29/P4-30), §11 Stage D steps 17-19.

**Stage D step 20a additions (Decision P4-103, Part A).**
`run_media_pulse_cycle` now returns `MediaPulseCycleResult` (defined just
above it) instead of a bare `(snapshot, top_candidates)` 2-tuple.
`with_voices_disabled` (below `load_media_library`) is Decision P4-97's
control-arm transformation, built for the same corpus generator.

**docs/design/media-v2-newsroom.md MV-1c.4, "Build order" step 3b: the
scoring step moved.** `run_media_pulse_cycle` no longer scores at all --
`MediaPulseCycleResult.candidates` is the raw, unscored `SalienceCandidate`
tuple, and the two counts that used to live on it (`candidates_scored`,
the number above `salience.SALIENCE_FLOOR`) now live on
`MediaSelectionCycleResult` instead, because scoring (and therefore both
counts) is now `run_media_selection_cycle`'s own act -- see that
dataclass's own docstring for why: two selection calls over the SAME pulse
result, against two different libraries (`tools/generate_media_corpus.
py`'s live/control arms), must score independently, so the counts cannot
live on the one shared, arm-agnostic pulse result.

**Stage D step 19 additions (Decisions P4-32/P4-38/P4-48/P4-66, migration
011), below the step-17/18 code, unchanged except where noted at each call
site below.** Real persistence, wired in for real:

- `run_media_pulse_cycle` now reads its own prior snapshot from `dba2/
  data/repositories/media_repo.py` (`get_prior_pulse_snapshot_teams`) and
  writes the current one back (`upsert_pulse_snapshot_teams`) -- it no
  longer takes `previous_snapshot` as a caller-supplied argument (the
  step-17-era shape, see the withdrawn bullet below). Read-then-diff-then-
  write, once per cycle per team, real DB round trips on both ends.
- `select_and_bind_story` now persists the story it selects/binds
  (`media_repo.create_media_story`) and lazily get-or-creates that story's
  named writer (`get_or_create_media_writer`, below) -- it returns the
  persisted `dba2.domain.media.MediaStory` row (with a real `id`), not a
  bare `select.StorySelection`, because the whole point of this step is
  that the selection now has somewhere real to live. `select.StorySelection`
  and `MediaStory` share the same `voice`/`template_id`/`slot_bindings`
  attribute names by construction, so `render_story` below (which only
  ever reads those three) still accepts either.
- `run_media_selection_cycle` (new) is the single per-cycle loop Decision
  P4-88 requires: it derives the publication plan exactly once (`salience.
  build_publication_plan`, over its own `candidates` parameter -- the
  caller's own `run_media_pulse_cycle` output, MV-1c.4) and `enumerate()`s
  it, so `candidate_sequence` is computed in exactly one place -- see that
  function's own docstring for why a second, independently-computed index
  anywhere else would silently reproduce the P4-88 collision step 18's
  last two fix rounds spent two rounds closing.

**What this module still does NOT do, carried forward from step 17/18 and
narrowed where step 19 changes the boundary:**

- ~~It does not persist a pulse snapshot anywhere.~~ **Superseded above.**
  Decision P4-32's `media_pulse_snapshot` table is real as of this step;
  `run_media_pulse_cycle` both reads and writes it now.
- **It still does not wire a live per-cycle production caller** --
  `tools/gm_harness/season.py`'s own cycle loop is not touched by this
  dispatch. This is a DELIBERATE, disclosed scope choice for step 19, not
  an oversight: the design doc's own §11 Stage D list names step 19 as
  "Migration 011, `media_repo`, persistence" and step 20 (not this one) as
  "Media metric family" -- neither line claims "wire the harness loop,"
  and §11's own five-stage ordering rule ("instrument before building,
  build the risky thing before the pleasant thing, put the regression
  detector in place before the mechanism that could trip it") argues for
  landing persistence correctness, proven by real integration tests
  against a real database, before wiring a live call site that would make
  every future harness run start writing rows. `run_media_selection_cycle`
  below is the real, callable, fully-tested per-cycle entry point a future
  wiring step would call -- this step stops at "correct and callable in
  isolation," which is the literal claim its own name makes.
- It does not wire trade-execution or signing discrete events. See
  `dba2/media/events.py`'s own module docstring for why (the decision-log
  JSONL cannot distinguish a scanned trade from an executed one, and a
  real emission call site inside `run_trade_window`/the free-agency loop
  is cross-cutting work outside this file's own scope).

**Stage D step 18 additions (Decisions P4-21/P4-26/P4-38's selection
mechanism/P4-22/P4-54), below the step-17 code above.** This file gains
the media phrase library's loader (`load_media_library`) and the real
DB-backed name lookup + persona/template/slot orchestration
(`select_and_bind_story`, `render_story`).

**Why the loader lives HERE rather than in a new `dba2/services/media_
phrase_service.py` (mirroring `phrase_service.py`'s own separate-file
shape) -- stated explicitly, per this dispatch's own "your call" framing.**
`tests/unit/test_layering.py::test_nothing_but_media_service_imports_media`
enforces "nothing but `dba2/services/media_service.py` may import `dba2.
media`" as a real, load-bearing valve (Decision P4-31). **Round 12 critic
fix (blocking finding 7): the prior text here claimed a second, disclosed
"`tools/` exception, Decision P4-86" existed and was "irrelevant here."
Decision P4-86 (the `tools/` widening) was WITHDRAWN by Decision P4-87 in
the immediately preceding commit (7a3f475) -- the enforcing test's current
exemption is a single path (`_MEDIA_SERVICE_PATH`), never a `tools/` scan,
and this docstring is corrected to state the valve's real, current shape
rather than a decision that no longer holds.** A
separate loader file would need to import `dba2.media.select` (for
`MediaTemplate`/`MediaPersona`/`SLOT_NAMES`) and would therefore need to be
added as a SECOND named exception to that valve -- a real, disclosed
layering change for no benefit `phrase_service.py`'s own separate-file
shape actually needs (that file is separate from `gm_service.py` because
`gm_service.py` is a large, pre-existing file with its own scope;
`media_service.py` is this phase's own single services-layer file and
already owns every other Stage D step 17 DB-backed concern). Extending the
existing, already-permitted importer is the minimal, valve-respecting
choice; widening the valve for a same-phase, same-file-owner addition
would be exactly the kind of layering change this project's own review
discipline reserves for a real need, and there is none here.

**Why `select_persona`/`select_media_template`/`bind_slots` are called
individually here rather than through one combined pure wrapper.** See
`dba2/media/select.py`'s own module docstring -- Decision P4-54 salts the
template draw with the persona draw's own OUTPUT (`voice`), so the second
RNG stream cannot be built until the first draw's result is known, and
only a `services/` caller may call `rng_for` at all. `select_and_bind_
story` below is the real orchestration sequencing: build `persona_rng`,
draw the persona, build `template_rng` from that persona's key, draw the
template, resolve names via a small additional DB read, bind slots.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import string
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import asyncpg

from dba2.core.config import Config
from dba2.core.rng import rng_for
from dba2.data.db import Executor, transaction
from dba2.data.repositories import (
    draft_repo,
    game_repo,
    head_to_head_repo,
    league_repo,
    media_repo,
    player_repo,
    ratings_repo,
    stats_repo,
    team_repo,
    txn_repo,
)
from dba2.domain.game import GameStatus, GameType
from dba2.domain.media import (
    MediaPulseSnapshot,
    MediaStory,
    MediaWriter,
    Register,
    StoryKind,
    column_for,
)
from dba2.domain.media_bindings import team_aliases_for
from dba2.domain.media_context import LeagueBrief
from dba2.domain.media_facts import (
    FACT_KEYS_BY_KIND,
    REQUIRED_FACT_KEYS_BY_KIND,
    UNIVERSAL_FACT_KEYS,
    Angle,
    Fact,
    FactSource,
    StoryPackage,
    Subject,
    SubjectKind,
    identity_facts,
)
from dba2.domain.media_prompt import (
    build_payload,
    build_persona_card,
    build_retry_note,
    serialize_payload,
)
from dba2.domain.media_writer import FormatTier, PersonaVoice
from dba2.domain.motivation import RequestState
from dba2.domain.phase import Phase
from dba2.domain.player import Position
from dba2.managers.needs import NeedTier, build_need_matrix
from dba2.managers.scouting import player_age
from dba2.managers.snapshot import LeagueSnapshot
from dba2.media import context as media_context
from dba2.media import events, pulse, salience, select
from dba2.media.detect import (
    attribution,
    construction,
    form,
    legacy,
    profile,
    revenge,
    rivalry,
    scheme,
    trades,
)
from dba2.media.detect import motivation as motivation_detect
from dba2.media.frame import StoryCandidate, StoryFrame, derive_frame
from dba2.media.write import verify
from dba2.media.write.budget import apply_headline_style, budget_for, clamp_text, select_tier
from dba2.media.write.template import TemplateWriter
from dba2.services import agency_service, gm_service, media_writer_llm

log = logging.getLogger(__name__)


async def load_gm_inputs_for_cycle(
    ex: Executor, *, league_id: int, season: int, cycle_index: int | None = None
) -> tuple[LeagueSnapshot, gm_service.GmContext] | tuple[None, None]:
    """The one guarded entry point onto `gm_service.load_gm_inputs` both of
    this cycle's hoisted call sites (`season_service._run_media_cycle` and
    `load_team_profile_packages`'s own pre-MV-11 fallback call, below) go
    through -- MV-11 critic-round fix (regression-fix rationale correction).

    **The gap this tolerates is a TEST-FIXTURE shape, not a normal
    production state.** `league_service.create_league` writes a
    `league_cap_settings` row for `current_season` at league creation
    (`league_service.py`, the `create_cap_settings` call), and
    `_require_cap_settings`'s own docstring (`season_service.py`) says the
    same thing -- every real league has one from the moment it exists. The
    only leagues this codebase ever constructs without one are test
    fixtures that call `league_repo.create_league` directly (e.g.
    `tests/integration/_press_fixtures.py::bare_league_at`), bypassing
    `league_service` entirely. A real, production league with teams and no
    cap-settings row for its own current season is a genuine data-integrity
    problem, not an expected pre-trade-deadline gap -- logged at `warning`,
    not `info`, so it does not read as harmless.

    **Narrows `load_gm_inputs`'s `NotFoundError` by checking `league_repo.
    get_cap_settings` directly first**, rather than catching the exception
    and guessing which of its two raise sites fired from the message
    (critic-round nit: the blind `except NotFoundError` this replaced could
    not tell "no cap-settings row" apart from "the league itself does not
    exist," and silently degraded the latter -- a much worse failure -- to
    the same INFO-level "nothing to see here" log). A league that vanishes
    between this check and the `load_gm_inputs` call itself (a real race,
    not the settled case this function exists for) still raises
    `NotFoundError` out of this function uncaught -- that failure belongs
    at the caller's own level, not swallowed here.

    Returns `(None, None)` -- log-and-skip, never a crash -- when cap
    settings are genuinely missing; callers fall back to their pre-MV-11
    shape for that one cycle.

    **Deliberately does NOT guard `load_gm_inputs`'s `ValidationError`**
    (raised when `season != league.current_season`) -- critic-round nit:
    MV-11 hoisted this function's call to run once, unconditionally, near
    the top of every cycle, so a season-mismatch bug is now reachable for
    the four legacy producers too, not just the two new ones. Judged
    acceptable as-is rather than guarded: `season_service._run_media_cycle`
    always calls this with `ctx.season`, which is `_PointerMove.season`,
    itself read from the SAME `league` row `_run_media_cycle` re-fetches
    moments before this call -- and the press's own claim
    (`leagues.advance_claimed_at`) is the mutual-exclusion guarantee that
    row cannot change mid-press. A real mismatch here would mean the
    pointer and the league row disagreed on the current season inside one
    claimed press, which is a season-accounting bug elsewhere, not a state
    this guard should paper over by returning `(None, None)` -- surfacing
    it as a crash is the more honest failure mode for that case."""
    cap_settings = await league_repo.get_cap_settings(ex, league_id, season)
    if cap_settings is None:
        log.warning(
            "media_service: league_id=%s season=%s cycle=%s has no league_cap_settings "
            "row -- every real league gets one at creation (league_service.create_league), "
            "so this is a data-integrity gap, not a normal pre-trade-deadline state, unless "
            "this is a test fixture built via league_repo.create_league directly. Skipping "
            "this cycle's cap-settings-dependent media work rather than crashing it",
            league_id,
            season,
            cycle_index,
        )
        return None, None
    return await gm_service.load_gm_inputs(ex, league_id=league_id, season=season)


async def compute_current_pulse_snapshot(
    ex: Executor, *, league_id: int, season: int, cycle_index: int
) -> pulse.PulseSnapshot:
    """Decision P4-30: standings/streak computed HERE, from `games` --
    never from `window.py`'s `win_pct` (a `team_strength` percentile
    fallback, Phase 7's own scope). One `list_teams_for_league` call plus
    one `list_games_for_league_season` call.

    **Round 10 critic fix (B4): this is NOT the query shape the Stage A
    census measured, and the prior docstring's citation did not transfer.**
    The census's row (a) (`docs/design/phase-4-stage-a-census.md` §3,
    "Standings, all 30 teams, from `games`," mean 4.69ms) is explicitly "one
    aggregate query" returning 30 already-summarized rows. This function
    instead calls `list_games_for_league_season`, which does a full-row
    `SELECT` over every game row for the season (~1,230 rows for a full
    82-game/30-team season), maps each row to a domain `Game` object, and
    aggregates wins/losses/streaks in PYTHON -- a materially different cost
    shape (O(games), not O(teams)) that the aggregate-query citation does
    not describe. **Re-measured fresh this round, against a real, newly
    generated 30-team/420-player dev league with a real, fully-simmed
    1,230-game 2026 season** (`tools.seed_dev_league` + `tools.sim_dev_
    league_season --league-id 120`, deleted after measuring), 5 trials of
    this exact function against the live DB: min 5.51ms, mean 6.82ms, max
    10.85ms. **Still cheap** -- comfortably inside the same "nowhere near an
    expensive full-league scan every tick" territory the census's own
    number was cited for -- but this is this shape's own real, measured
    number, not an inherited one.

    Every league team is included in the result even with zero games
    played this season (Decision P4-24's "a team in game 3" case,
    generalized to "a team in game 0" -- absence in the underlying
    `team_game_results` mapping means the team does not exist YET, not
    that it exists with no games; `dba2/media/pulse.py::build_pulse_
    snapshot`'s own docstring states the same distinction from the pure
    side)."""
    teams = await team_repo.list_teams_for_league(ex, league_id)
    games = await game_repo.list_games_for_league_season(ex, league_id, season)

    team_game_results: dict[int, list[bool]] = {team.id: [] for team in teams}
    # Round 10 critic fix (nit 8): sorted by `(game_index, id)`, matching
    # `game_repo.list_games_for_league_season`'s own canonical ordering
    # (`game_repo.py`'s `ORDER BY game_index, id`) -- the previous
    # `(game_date, id)` key was benign only because `schedule_service`
    # happens to assign `game_index` in date-sorted order today, which is
    # an undisclosed dependency a streak's own sign should not rest on.
    final_games = sorted(
        (g for g in games if g.status is GameStatus.FINAL and g.game_type is GameType.REGULAR),
        key=lambda g: (g.game_index, g.id),
    )
    for game in final_games:
        if game.home_score is None or game.away_score is None:
            # Defensive only -- a real FINAL game always carries both
            # scores (`finalize_game`'s own write contract); skipping
            # rather than crashing keeps one corrupt row from taking down
            # a whole cycle's vigilance pass.
            continue
        home_won = game.home_score > game.away_score
        if game.home_team_id in team_game_results:
            team_game_results[game.home_team_id].append(home_won)
        if game.away_team_id in team_game_results:
            team_game_results[game.away_team_id].append(not home_won)

    return pulse.build_pulse_snapshot(cycle_index=cycle_index, team_game_results=team_game_results)


async def load_player_unrest_candidates(
    ex: Executor, *, league_id: int, season: int
) -> tuple[StoryCandidate, ...]:
    """Decision P4-27's event-sourced intake, the motivation half: real
    `ladder_transition` rows for this season (`agency_service.load_
    ladder_transitions_for_league`, O(events)), each paired with that
    player's CURRENT `request_state`/`morale`
    (`agency_service.load_motivation_for_league`, the same read-model
    `rank_offers` already uses) -- see `dba2/media/events.py::ladder_
    transition_candidate`'s own docstring for why this reads current state
    rather than parsing the event's free-text note. A player who
    transitioned more than once this season (a real, if rare, possibility
    under Decision P4-11's two-checkpoints-a-season cadence) contributes
    exactly one candidate, not one per event -- `state` is read once, not
    replayed."""
    season_events = await agency_service.load_ladder_transitions_for_league(
        ex, league_id=league_id, season=season
    )
    if not season_events:
        return ()

    motivation_by_player = await agency_service.load_motivation_for_league(ex, league_id=league_id)

    candidates: list[StoryCandidate] = []
    seen_players: set[int] = set()
    for event in season_events:
        if event.player_id in seen_players:
            continue
        seen_players.add(event.player_id)

        motivation = motivation_by_player.get(event.player_id)
        if motivation is None:
            # Defensive only -- an event implies a row; absence would mean
            # the player left the league between the event and this read.
            continue

        candidate = events.ladder_transition_candidate(
            player_id=event.player_id,
            request_state=motivation.request_state,
            motivation_axis=event.axis,
            morale=motivation.morale,
        )
        if candidate is not None:
            candidates.append(candidate)
    return tuple(candidates)


async def load_rookie_watch_candidates(
    ex: Executor, *, league_id: int, season: int
) -> tuple[StoryCandidate, ...]:
    """Decision P4-107 item 1 -- a field-for-field copy of `load_player_
    unrest_candidates`'s own shape: real, committed rows in, one candidate
    per real subject out, no live computation. Reads the draft PRIOR to
    `season` (`get_draft_for_season(ex, league_id, season - 1)`) --
    `_run_one_season`'s own cycle cadence (`_CYCLE_PHASES`, `tools/
    generate_media_corpus.py`) places `DRAFT` at the END of its OWN
    season's cycle list, after that season's `REGULAR_SEASON`/`TRADE_
    DEADLINE` cycles, so a rookie picked in season `N`'s own draft has
    played zero games of season `N` by definition (the season is over by
    the time the pick happens) -- last season's draft class is who is
    actually accumulating real per-game numbers THIS season, which is
    what these templates ("per-game numbers are starting to stand out",
    "rookie season is drawing comparisons to seasons past") are about.
    `season - 1 < 1` (the corpus's first season, no prior draft exists)
    reads as `None` from `get_draft_for_season` and returns `()`, the
    identical empty-guard shape `load_player_unrest_candidates` uses for
    "no events yet."

    **The one field this function supplies that the draft selection alone
    cannot: `player_games_played_this_season`, a REAL per-player reading**
    (`stats_repo.get_player_season_stats(ex, league_id, player_id, season,
    GameType.REGULAR).games_played`, `None` if no row exists yet -- a
    materialized aggregate `stats_service.rebuild_player_season_stats`
    writes once a season is complete, never computed live here). See
    `events.draft_selection_candidate`'s own docstring for why this field
    is required rather than defaulted, and why supplying `None` (a real
    "we don't have this yet," never a fabricated pass) is the correct,
    disclosed behavior rather than a gap to paper over -- `derive_frame`'s
    own `_PLAYER_SAMPLE_KINDS` gate (`dba2/media/frame.py`, Decision P4-24,
    unchanged by this step) is what decides sufficiency from here, exactly
    as it already does for every other kind in that group.

    Called once per cycle, same as `load_player_unrest_candidates` --
    every cycle within a season re-reads the identical prior season's
    draft class and re-submits the same candidates, relying on `salience`/
    the repeat penalty to do the same natural de-duplication `player_
    unrest`'s own candidates already rely on, not a new pattern."""
    draft = await draft_repo.get_draft_for_season(ex, league_id, season - 1)
    if draft is None:
        return ()

    selections = await draft_repo.list_draft_selections_for_draft(ex, draft.id)
    if not selections:
        return ()

    candidates: list[StoryCandidate] = []
    for selection in selections:
        stats = await stats_repo.get_player_season_stats(
            ex, league_id, selection.player_id, season, GameType.REGULAR
        )
        candidates.append(
            events.draft_selection_candidate(
                player_id=selection.player_id,
                team_id=selection.team_id,
                player_games_played_this_season=(
                    stats.games_played if stats is not None else None
                ),
            )
        )
    return tuple(candidates)


async def load_milestone_candidates(
    ex: Executor, *, league_id: int, season: int
) -> tuple[StoryCandidate, ...]:
    """Decision P4-121 item 4 -- a field-for-field sibling of `load_rookie_
    watch_candidates`: one bulk read, one pure call, return. `stats_repo.
    get_career_scoring_to_date_for_league(ex, league_id, season=season,
    game_type=GameType.REGULAR)` is the whole DB round trip -- `GameType.
    REGULAR` is fixed here, not a caller parameter, because it is what
    `tools/generate_media_corpus.py::_run_one_season` rebuilds
    (`:1006-1010`) and what a real all-time scoring list means (§39.1
    item 8 -- there is no special-casing for early cycles: a season-`N`
    read honestly returns season `N-1`'s totals before that season's own
    stats are rebuilt, and `career_scoring_milestone_candidates`'s own
    guard/rank rule handles both shapes correctly with no cycle-aware
    branch here).

    Called once per cycle, same as `load_rookie_watch_candidates` -- every
    cycle re-reads the same league-wide career totals and re-derives the
    same board; a season with no movement re-submits the identical empty
    tuple, and a season with movement re-submits the identical set of
    movers every cycle within it, relying on the same natural `salience`/
    repeat-penalty de-duplication every other producer in this module
    already relies on."""
    rows = tuple(
        (
            await stats_repo.get_career_scoring_to_date_for_league(
                ex, league_id, season=season, game_type=GameType.REGULAR
            )
        ).values()
    )
    return events.career_scoring_milestone_candidates(rows)


async def load_team_profile_packages(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    phase: Phase,
    cycle_index: int,
    quota: int,
    league_snapshot: LeagueSnapshot | None = None,
    gm_context: gm_service.GmContext | None = None,
) -> tuple[salience.SalienceCandidate, ...]:
    """docs/design/media-v2-newsroom.md MV-2h.2/MV-2h.3, "Build order" step
    6 -- the one new loader the reserved `TEAM_PROFILE` quota needs: rank
    the eligible pool (every team not yet profiled this season), publish
    the top `quota` that clear `profile.MIN_FACTS_TO_PUBLISH`.

    **A real, disclosed narrowing of MV-2h.4's own four-plus-one sections,
    not the full composition.** This loader supplies section 1 ("where
    they stand" -- the pulse snapshot) and section 2 ("what they're built
    on" -- the need matrix and roster age), plus the trade-activity and
    motivation-drama terms `profile_interest` itself needs. It does NOT
    supply section 3 ("how they play" -- `team_strategy`/`TeamStyleAggregate`),
    section 4's timeline/philosophy/next-game fields, the rivalry preview,
    or section 5 ("last year") -- every one of `team_profile_package`'s
    params for those fields is independently `None`-able (MV-2h.4's own
    "every `None` emits no fact for that key" contract), so omitting them
    is safe and does not violate any invariant; it only means a real
    published profile is thinner than MV-2h.4's full vision until a later
    pass wires the remaining sections in. `rivalry_heat` is therefore
    always `False` in `profile_interest`'s own six-term sum too (an
    honestly-absent signal, not a fabricated one -- this loader does no
    partial, undertested rivalry-scoring integration just to approximate
    one of six terms).

    **The one shared `LeagueSnapshot` per cycle** (MV-10's own "must not
    add a second `load_gm_inputs` build within the same cycle" requirement)
    -- as of MV-11 (`docs/design/media-v2-newsroom.md`, "Build order" step
    7.5a), this function is no longer the sole caller: `_run_media_cycle`
    now loads `(league_snapshot, gm_context)` ONCE per cycle and hands the
    SAME pair to this function AND to `load_event_candidates` (MV-11.2's
    own "hoist the shared load out of `load_team_profile_packages` into
    the cycle" instruction), rather than each loader building its own.
    `league_snapshot`/`gm_context` stay optional keyword parameters
    (`None` by default) so this function's own existing tests -- built
    before MV-11 existed -- keep passing unedited: `None` falls back to
    `load_gm_inputs_for_cycle` (this module, above), the same guarded
    entry point `season_service._run_media_cycle` itself uses.

    **Critic-round fix: this fallback call used to be a bare, unguarded
    `gm_service.load_gm_inputs`.** Reached whenever `eligible_teams` is
    non-empty and no snapshot/context was handed in -- including every
    call from `_run_media_cycle` itself when its own hoisted call already
    caught a missing-cap-settings gap and passed `None`/`None` down, so
    this was a second, unguarded crash point for the exact same gap the
    hoisted call exists to tolerate. Now falls back to `()` (same as the
    empty-eligible-pool case below) when `load_gm_inputs_for_cycle` cannot
    supply a real snapshot/context, rather than propagating the exception.

    Returns `()` immediately when the eligible pool is empty (MV-2h.2's
    own "stop publishing profiles for the rest of that season" rule) --
    the caller (`run_media_selection_cycle`, via `profile_candidates`)
    treats an empty tuple exactly like any other cycle with nothing to
    say: zero profile publications, not an error."""
    profiled_this_season = await media_repo.list_profiled_team_ids(
        ex, league_id=league_id, season=season
    )
    teams = await team_repo.list_teams_for_league(ex, league_id)
    eligible_teams = [t for t in teams if t.id not in profiled_this_season]
    if not eligible_teams:
        return ()

    profiled_last_season = await media_repo.list_profiled_team_ids(
        ex, league_id=league_id, season=season - 1
    )
    current_rows = await media_repo.get_pulse_snapshot_teams(
        ex, league_id=league_id, season=season, cycle_index=cycle_index
    )
    current_snapshot = _rows_to_pulse_snapshot(current_rows, cycle_index=cycle_index)
    prior_rows = await media_repo.get_prior_pulse_snapshot_teams(
        ex, league_id=league_id, season=season, before_cycle_index=cycle_index
    )
    prior_snapshot = (
        None
        if not prior_rows
        else _rows_to_pulse_snapshot(prior_rows, cycle_index=prior_rows[0].cycle_index)
    )

    trades = await txn_repo.list_trades_for_league_season(ex, league_id=league_id, season=season)
    assets_by_transaction = await txn_repo.list_assets_for_transactions(
        ex, [t.id for t in trades]
    )
    trades_in: dict[int, int] = {}
    trades_out: dict[int, int] = {}
    for transaction_assets in assets_by_transaction.values():
        for asset in transaction_assets:
            if asset.to_team_id is not None:
                trades_in[asset.to_team_id] = trades_in.get(asset.to_team_id, 0) + 1
            if asset.from_team_id is not None:
                trades_out[asset.from_team_id] = trades_out.get(asset.from_team_id, 0) + 1

    # Decision P4-16: only `agency_service.py` may import `motivation_repo`
    # directly -- `agency_service.load_motivation_for_league` is the read
    # model every other caller (including this one) goes through instead.
    motivation_by_player = await agency_service.load_motivation_for_league(ex, league_id=league_id)
    players = await player_repo.list_players_for_league(ex, league_id)
    player_team_by_id = {p.id: p.team_id for p in players}
    unrest_by_team: dict[int, int] = {}
    for player_id, motivation in motivation_by_player.items():
        team_id = player_team_by_id.get(player_id)
        if team_id is not None and motivation.request_state is not RequestState.CONTENT:
            unrest_by_team[team_id] = unrest_by_team.get(team_id, 0) + 1

    if league_snapshot is None or gm_context is None:
        league_snapshot, gm_context = await load_gm_inputs_for_cycle(
            ex, league_id=league_id, season=season, cycle_index=cycle_index
        )
        if league_snapshot is None or gm_context is None:
            return ()

    ranking_candidates: list[profile.ProfileCandidate] = []
    for team in eligible_teams:
        team_snapshot = league_snapshot.team_by_id(team.id)
        need_matrix = build_need_matrix(team_snapshot, tuning=gm_context.tuning)
        roster_extremity = max(
            max(need_matrix.need[pos][NeedTier.STARTER], need_matrix.glut[pos][NeedTier.ROTATION])
            for pos in Position
        )
        current_state = current_snapshot.teams.get(team.id)
        prior_state = prior_snapshot.teams.get(team.id) if prior_snapshot is not None else None
        streak_length = current_state.streak_length if current_state is not None else 0
        movement = None
        if current_state is not None and prior_state is not None:
            movement = profile.standings_movement_term(
                position_now=current_state.standings_position,
                position_at_last_pulse=prior_state.standings_position,
                games_played_now=current_state.games_played,
                games_played_at_last_pulse=prior_state.games_played,
            )
        terms = profile.ProfileTerms(
            roster_extremity=roster_extremity,
            streak_length=streak_length,
            standings_movement=movement,
            trades_this_season=trades_in.get(team.id, 0) + trades_out.get(team.id, 0),
            players_not_content=unrest_by_team.get(team.id, 0),
            rivalry_heat=False,
        )
        interest = profile.profile_interest(terms)
        priority = profile.profile_priority(
            interest,
            not_profiled_last_season=team.id not in profiled_last_season,
            human_managed=team.manager_user_id is not None,
        )
        ranking_candidates.append(profile.ProfileCandidate(team_id=team.id, priority=priority))

    ranked_ids = profile.select_teams_to_profile(tuple(ranking_candidates), quota=quota)

    packages: list[StoryPackage] = []
    for team_id in ranked_ids:
        team_snapshot = league_snapshot.team_by_id(team_id)
        need_matrix = build_need_matrix(team_snapshot, tuning=gm_context.tuning)
        best_need_pos = max(Position, key=lambda pos: need_matrix.need[pos][NeedTier.STARTER])
        best_glut_pos = max(Position, key=lambda pos: need_matrix.glut[pos][NeedTier.ROTATION])
        top8 = sorted(team_snapshot.roster, key=lambda rp: rp.player.overall_cached, reverse=True)[
            :8
        ]
        mean_age_top8 = (
            sum(player_age(rp.player.birth_date, season) for rp in top8) / len(top8)
            if top8
            else None
        )
        current_state = current_snapshot.teams.get(team_id)
        prior_state = prior_snapshot.teams.get(team_id) if prior_snapshot is not None else None
        delta = None
        if (
            current_state is not None
            and prior_state is not None
            and current_state.games_played > 0
            and prior_state.games_played > 0
        ):
            delta = current_state.standings_position - prior_state.standings_position

        draft = profile.team_profile_package(
            team_id,
            wins=current_state.wins if current_state is not None else 0,
            losses=current_state.losses if current_state is not None else 0,
            standings_position=(
                current_state.standings_position if current_state is not None else 0
            ),
            streak_length=current_state.streak_length if current_state is not None else 0,
            standings_position_delta=delta,
            mean_age_top8=mean_age_top8,
            biggest_need_position=best_need_pos.value,
            biggest_need_value=need_matrix.need[best_need_pos][NeedTier.STARTER],
            biggest_glut_position=best_glut_pos.value,
            biggest_glut_value=need_matrix.glut[best_glut_pos][NeedTier.ROTATION],
            offensive_scheme=None,
            defensive_scheme=None,
            three_point_bias=None,
            three_share=None,
            pace_target=None,
            avg_possessions=None,
            set_by_a_person=None,
            scheme_tension=None,
            timeline=None,
            gm_philosophy=None,
            trades_in_count=trades_in.get(team_id),
            trades_out_count=trades_out.get(team_id),
            unrest_player_count=unrest_by_team.get(team_id),
            next_game_date=None,
            next_game_is_rivalry=None,
            prior_wins=None,
            prior_losses=None,
            prior_standings_position=None,
            season=season,
            phase=phase,
            cycle_index=cycle_index,
        )
        if draft is None:
            continue
        team_names = {team_id: f"{team_snapshot.team.city} {team_snapshot.team.name}"}
        sealed = seal_package(draft, team_names=team_names, player_names={})
        if sealed is not None:
            packages.append(sealed)

    return tuple(salience.SalienceCandidate(package=package) for package in packages)


def _pair_key(team_id: int, opponent_team_id: int) -> tuple[int, int]:
    """A deterministic, order-independent key for a team pair -- the same
    "lower id first" convention `trades.py::trade_executed_package` already
    uses for its own `team_id`/`opponent_team_id` assignment, applied here
    so a pair is counted/keyed exactly once regardless of which side of a
    `Game`/`LeagueTransaction` row happened to be `home`/`from`."""
    return (
        (team_id, opponent_team_id)
        if team_id < opponent_team_id
        else (opponent_team_id, team_id)
    )


async def load_event_candidates(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    phase: Phase,
    cycle_index: int,
    league_snapshot: LeagueSnapshot,
    gm_context: gm_service.GmContext,
    current_snapshot: pulse.PulseSnapshot,
    team_names: Mapping[int, str],
    player_names: Mapping[int, str],
) -> tuple[salience.SalienceCandidate, ...]:
    """docs/design/media-v2-newsroom.md MV-11 ("Fourteen of twenty-two kinds
    have no producer") / MV-11.2 / "Build order" step 7.5a -- the ONE new
    loader that wires the eight orphaned `dba2/media/detect/` modules
    (`trades`, `form`+`attribution`, `construction`, `rivalry`, `scheme`,
    `revenge`) into the real production media cycle, alongside `legacy`/
    `profile` (already wired) and `motivation` (a pure helper the trade/
    revenge branches below call, never a producer of its own).

    **One composition, sharing the cycle's own `LeagueSnapshot`/`GmContext`**
    (MV-11.2's own "hoist the shared load out of `load_team_profile_
    packages` into the cycle" instruction, and MV-10's "must not add a
    second `load_gm_inputs` build within the same cycle" rule) -- both are
    caller-supplied, never loaded here. Every OTHER read below is a single,
    whole-league, whole-season bulk statement (no per-team, no per-player
    loop): `txn_repo.list_trades_for_league_season` + `list_assets_for_
    transactions`, `stats_repo.list_recent_form_windows`, `team_repo.list_
    team_strategies_for_league_season` (new, MV-11.2's own bulk-read
    requirement), `stats_repo.list_team_style_aggregates`, `head_to_head_
    repo.list_head_to_head_for_league`, `game_repo.list_scheduled_games`,
    `stats_repo.get_player_season_stats_bulk` (critic-round fix, the
    REVENGE_GAME branch's own `games_played` read -- see that loop's own
    comment; this discipline is why a bulk variant exists at all, not a
    per-asset call inside the loop it now precedes),
    `agency_service.load_ladder_transitions_for_league`/`load_motivation_
    for_league` (already read once this cycle by `run_media_pulse_cycle`/
    `load_team_profile_packages`; re-read here rather than threaded through
    a growing parameter list -- a disclosed, small, O(events)/O(players)
    duplication, never a per-team or per-player N+1, the same "duplicate a
    cheap bulk read rather than thread it everywhere" trade this file's own
    `_subject_prominence` docstring already accepts for `overall_cached`).

    **Real, disclosed narrowings kept to a single season, not "this season
    or last" (MV-2f/MV-2i's own text)** -- `RIVALRY_MATCHUP`'s `recent_
    trade` signal and `REVENGE_GAME`'s own trade-recency check both read
    only `season`'s trades/games, never `season - 1`'s. A second full
    season's `games`/`league_transactions` scan for a cross-season check
    was judged disproportionate to what it would buy this build step (the
    kinds still fire; they fire on a narrower trade-recency window than the
    design's own text describes) -- named here rather than silently
    shipping the wider claim. `close_game_count`/season-series are likewise
    `season`-only, not `season` and `season - 1` combined.

    **`RIVALRY_MATCHUP`/`REVENGE_GAME`'s own "shared players"/"named
    individuals with a trade history between the two teams" fact groups are
    NOT built here** -- `rivalry.py`'s own module docstring already
    discloses this gap (a second, per-pair `league_transaction_assets`
    query keyed on both team ids at once); this loader supplies the
    top-2-by-`overall_cached` named-actor half only, exactly as that
    docstring's own "deliberately NOT implemented" section describes.

    Returns every sealed candidate every detector below produced, as one
    flat tuple for the caller (`season_service._run_media_cycle`) to
    concatenate onto `MediaPulseCycleResult.candidates` before scoring --
    this function does not score, exactly like `run_media_pulse_cycle`
    itself (MV-1c.4: scoring is `run_media_selection_cycle`'s own act).

    **Player/ratings maps are built from `player_repo.list_players_for_
    league`/`ratings_repo.get_for_league` (two more whole-league bulk
    reads), NOT from `league_snapshot`'s own rosters.** `TeamSnapshot.
    roster` deliberately excludes `TWO_WAY`/`INJURED`/`FREE_AGENT`/
    `RETIRED`/`DRAFT_ELIGIBLE` players (`managers/snapshot.py`'s own
    documented interpretive call) -- correct for a GM decision, wrong here:
    a player on a 5-game heater who is currently `INJURED` is exactly the
    kind of real story a form detector should still be able to tell, and a
    `TRADE_REQUESTED` player who is `TWO_WAY` is still real market-watch
    material. Using the roster-scoped map instead would silently drop
    those candidates with no signal that anything was skipped."""
    teams = [team_snapshot.team for team_snapshot in league_snapshot.teams]
    team_by_id = {team.id: team for team in teams}
    all_players = await player_repo.list_players_for_league(ex, league_id)
    all_ratings = await ratings_repo.get_for_league(ex, league_id)
    player_ratings_by_id = {r.player_id: r for r in all_ratings}
    player_overall_by_id = {p.id: p.overall_cached for p in all_players}
    player_team_by_id = {p.id: p.team_id for p in all_players if p.team_id is not None}

    motivation_by_player = await agency_service.load_motivation_for_league(ex, league_id=league_id)
    ladder_events = await agency_service.load_ladder_transitions_for_league(
        ex, league_id=league_id, season=season
    )
    ladder_events_by_player: dict[int, list] = {}
    for event in ladder_events:
        ladder_events_by_player.setdefault(event.player_id, []).append(event)

    drafts: list[StoryPackage] = []

    # -- trades.py: TRADE_EXECUTED, TRADE_BROKEN_PROMISE (MV-2a) --------
    season_trades = await txn_repo.list_trades_for_league_season(
        ex, league_id=league_id, season=season
    )
    assets_by_transaction = await txn_repo.list_assets_for_transactions(
        ex, [t.id for t in season_trades]
    )
    for txn in season_trades:
        txn_assets = assets_by_transaction.get(txn.id, [])
        draft = trades.trade_executed_package(
            txn, txn_assets, season=season, phase=phase, cycle_index=cycle_index
        )
        if draft is not None:
            drafts.append(draft)

        for asset in txn_assets:
            if (
                asset.player_id is None
                or asset.from_team_id is None
                or asset.to_team_id is None
            ):
                continue
            player_motivation = motivation_by_player.get(asset.player_id)
            if player_motivation is None:
                continue
            state_at_trade = motivation_detect.request_state_at(
                ladder_events_by_player.get(asset.player_id, ()),
                as_of=txn.created_at,
                current_request_state=player_motivation.request_state,
            )
            promise_draft = trades.trade_broken_promise_package(
                player_id=asset.player_id,
                old_team_id=asset.from_team_id,
                new_team_id=asset.to_team_id,
                state_at_trade=state_at_trade,
                satisfaction_loyalty=player_motivation.satisfaction_loyalty,
                weight_loyalty=player_motivation.weight_loyalty,
                trade_season=txn.season,
                season=season,
                phase=phase,
                cycle_index=cycle_index,
            )
            if promise_draft is not None:
                drafts.append(promise_draft)

    # -- form.py + attribution.py: HOT_STREAK, COLD_STRETCH, ------------
    # -- DEFENSIVE_STANDOUT (MV-2c/2d) -----------------------------------
    windows = await stats_repo.list_recent_form_windows(
        ex, league_id=league_id, season=season, game_type=GameType.REGULAR
    )
    for window in windows:
        if not form.is_eligible(window):
            continue
        ratings = player_ratings_by_id.get(window.player_id)
        if ratings is None:
            continue
        baseline = form.compute_baseline(window)
        form_facts = attribution.explain_form(
            window,
            baseline,
            catch_shoot_3=ratings.catch_shoot_3,
            off_dribble_3=ratings.off_dribble_3,
            finishing_rim=ratings.finishing_rim,
            first_step=ratings.first_step,
            draw_foul=ratings.draw_foul,
            passing_vision=ratings.passing_vision,
        )
        hot_cold_draft = form.hot_or_cold_package(
            window,
            season=season,
            phase=phase,
            cycle_index=cycle_index,
            attribution_facts=form_facts,
        )
        if hot_cold_draft is not None:
            drafts.append(hot_cold_draft)

        defense_facts = attribution.explain_defense(
            window,
            on_ball_defense=ratings.on_ball_defense,
            rim_protection=ratings.rim_protection,
            defensive_rebounding=ratings.defensive_rebounding,
        )
        defense_draft = form.defensive_standout_package(
            window,
            season=season,
            phase=phase,
            cycle_index=cycle_index,
            attribution_facts=defense_facts,
        )
        if defense_draft is not None:
            drafts.append(defense_draft)

    # -- construction.py: ROSTER_CONSTRUCTION (MV-2e) --------------------
    # `team_strategy` is read FIRST so this stays one pass per team, never
    # a "build the need matrix, then re-loop once the strategy read lands"
    # shape -- `need_matrix_by_team` is still built for every team here
    # (not just the ones with a strategy row) because `scheme.py`'s league-
    # wide need scan below needs a need matrix for every team regardless
    # of whether that team also gets a `ROSTER_CONSTRUCTION` package.
    team_strategies = await team_repo.list_team_strategies_for_league_season(
        ex, league_id=league_id, season=season
    )
    strategy_by_team = {s.team_id: s for s in team_strategies}
    need_matrix_by_team: dict[int, object] = {}
    for team_snapshot in league_snapshot.teams:
        need_matrix = build_need_matrix(team_snapshot, tuning=gm_context.tuning)
        need_matrix_by_team[team_snapshot.id] = need_matrix
        if not team_snapshot.roster:
            continue
        strategy = strategy_by_team.get(team_snapshot.id)
        if strategy is None:
            continue
        top8 = sorted(
            team_snapshot.roster, key=lambda rp: rp.player.overall_cached, reverse=True
        )[:8]
        mean_age_top8 = sum(player_age(rp.player.birth_date, season) for rp in top8) / len(top8)
        construction_draft = construction.roster_construction_package(
            team_snapshot.id,
            need=need_matrix.need,
            glut=need_matrix.glut,
            supply=need_matrix.supply,
            starter_tier=NeedTier.STARTER,
            rotation_tier=NeedTier.ROTATION,
            star_tier=NeedTier.STAR,
            mean_age_top8=mean_age_top8,
            timeline=team_snapshot.team.timeline,
            gm_philosophy=team_snapshot.team.gm_philosophy,
            measured_three_point_bias=strategy.three_point_bias,
            season=season,
            phase=phase,
            cycle_index=cycle_index,
        )
        if construction_draft is not None:
            drafts.append(construction_draft)

    # -- scheme.py: SCHEME_IDENTITY, TRADE_MARKET_WATCH (MV-2g) ---------
    style_aggregates = await stats_repo.list_team_style_aggregates(
        ex, league_id=league_id, season=season, game_type=GameType.REGULAR
    )
    style_by_team = {s.team_id: s for s in style_aggregates}
    league_size = len(teams)
    pace_ranked = sorted(
        (s for s in team_strategies), key=lambda s: s.pace_target, reverse=True
    )
    pace_rank_by_team = {s.team_id: i + 1 for i, s in enumerate(pace_ranked)}
    possessions_ranked = sorted(
        (s for s in style_aggregates if s.games > 0),
        key=lambda s: s.avg_possessions,
        reverse=True,
    )
    possessions_rank_by_team = {s.team_id: i + 1 for i, s in enumerate(possessions_ranked)}
    three_share_ranked = sorted(
        (s for s in style_aggregates if s.games > 0), key=lambda s: s.three_share, reverse=True
    )
    three_share_rank_by_team = {s.team_id: i + 1 for i, s in enumerate(three_share_ranked)}

    for team in teams:
        strategy = strategy_by_team.get(team.id)
        style = style_by_team.get(team.id)
        if strategy is None or style is None:
            continue
        pace_rank = pace_rank_by_team.get(team.id)
        possessions_rank = possessions_rank_by_team.get(team.id)
        three_share_rank = three_share_rank_by_team.get(team.id)
        if pace_rank is None or possessions_rank is None or three_share_rank is None:
            continue
        scheme_draft = scheme.scheme_identity_package(
            team.id,
            offensive_scheme=strategy.offensive_scheme,
            defensive_scheme=strategy.defensive_scheme,
            pace_target=strategy.pace_target,
            pace_rank=pace_rank,
            avg_possessions=style.avg_possessions,
            avg_possessions_rank=possessions_rank,
            three_point_bias=strategy.three_point_bias,
            three_share=style.three_share,
            three_share_rank=three_share_rank,
            league_size=league_size,
            set_by_a_person=strategy.set_by is not None,
            games=style.games,
            season=season,
            phase=phase,
            cycle_index=cycle_index,
        )
        if scheme_draft is not None:
            drafts.append(scheme_draft)

    league_biggest_need_by_position: dict[object, float] = {}
    for position in Position:
        league_biggest_need_by_position[position] = max(
            (
                need_matrix_by_team[team_snapshot.id].need[position][NeedTier.STARTER]
                for team_snapshot in league_snapshot.teams
            ),
            default=0.0,
        )
    for player_id, player_motivation in motivation_by_player.items():
        if player_motivation.request_state not in (
            RequestState.TRADE_REQUESTED,
            RequestState.UNSETTLED,
        ):
            continue
        team_id = player_team_by_id.get(player_id)
        if team_id is None or team_id not in need_matrix_by_team:
            continue
        need_matrix = need_matrix_by_team[team_id]
        glut_position = max(Position, key=lambda pos: need_matrix.glut[pos][NeedTier.ROTATION])
        glut_value = need_matrix.glut[glut_position][NeedTier.ROTATION]
        market_watch_draft = scheme.trade_market_watch_package(
            player_id,
            team_id,
            request_state=player_motivation.request_state,
            team_glut_position=glut_position.value,
            team_glut_value=glut_value,
            league_biggest_need_value=league_biggest_need_by_position.get(glut_position, 0.0),
            season=season,
            phase=phase,
            cycle_index=cycle_index,
        )
        if market_watch_draft is not None:
            drafts.append(market_watch_draft)

    # -- Shared scheduling/history read for rivalry.py + revenge.py -----
    scheduled_games = await game_repo.list_scheduled_games(
        ex, league_id, season, GameType.REGULAR
    )
    next_game_by_pair: dict[tuple[int, int], object] = {}
    for game in sorted(scheduled_games, key=lambda g: (g.game_index, g.id)):
        pair = _pair_key(game.home_team_id, game.away_team_id)
        next_game_by_pair.setdefault(pair, game)

    season_games = await game_repo.list_games_for_league_season(ex, league_id, season)
    final_games = [
        g
        for g in season_games
        if g.status is GameStatus.FINAL
        and g.game_type is GameType.REGULAR
        and g.home_score is not None
        and g.away_score is not None
    ]
    final_games_by_pair: dict[tuple[int, int], list] = {}
    for game in final_games:
        final_games_by_pair.setdefault(_pair_key(game.home_team_id, game.away_team_id), []).append(
            game
        )

    head_to_head_rows = await head_to_head_repo.list_head_to_head_for_league(ex, league_id)
    h2h_by_directed_pair = {(row.team_id, row.opponent_id): row for row in head_to_head_rows}

    def _h2h_signal(a: int, b: int) -> bool:
        for team_id, opponent_id in ((a, b), (b, a)):
            row = h2h_by_directed_pair.get((team_id, opponent_id))
            if row is None:
                continue
            if (
                row.rejects_by_opponent >= rivalry.H2H_REJECTS_FLOOR
                or row.far_reject_streak >= rivalry.H2H_FAR_REJECT_STREAK_FLOOR
            ):
                return True
        return False

    def _recent_trade(a: int, b: int) -> bool:
        pair = _pair_key(a, b)
        for txn in season_trades:
            txn_team_ids = {
                asset.from_team_id for asset in assets_by_transaction.get(txn.id, [])
            } | {asset.to_team_id for asset in assets_by_transaction.get(txn.id, [])}
            txn_team_ids.discard(None)
            if len(txn_team_ids) == 2 and _pair_key(*txn_team_ids) == pair:
                return True
        return False

    def _named_players(team_id: int) -> tuple[str, ...]:
        team_snapshot = league_snapshot.team_by_id(team_id)
        top2 = sorted(
            team_snapshot.roster, key=lambda rp: rp.player.overall_cached, reverse=True
        )[:2]
        return tuple(
            player_names.get(rp.player.id, f"{rp.player.first_name} {rp.player.last_name}")
            for rp in top2
        )

    # -- rivalry.py: RIVALRY_MATCHUP (MV-2f) -----------------------------
    for pair, game in next_game_by_pair.items():
        team_id, opponent_team_id = pair
        team = team_by_id.get(team_id)
        opponent = team_by_id.get(opponent_team_id)
        if team is None or opponent is None:
            continue
        pair_final_games = final_games_by_pair.get(pair, ())
        close_game_count = sum(
            1
            for g in pair_final_games
            if abs(g.home_score - g.away_score) <= rivalry.CLOSE_GAME_MARGIN
        )
        wins_team = sum(
            1
            for g in pair_final_games
            if (g.home_team_id == team_id and g.home_score > g.away_score)
            or (g.away_team_id == team_id and g.away_score > g.home_score)
        )
        wins_opponent = len(pair_final_games) - wins_team
        same_division = (
            team.conference == opponent.conference and team.division == opponent.division
        )
        signals = rivalry.RivalrySignals(
            same_division=same_division,
            close_game_count=close_game_count,
            season_series_level=rivalry.season_series_is_level(
                completed=len(pair_final_games), wins_a=wins_team, wins_b=wins_opponent
            ),
            recent_trade=_recent_trade(team_id, opponent_team_id),
            h2h_signal=_h2h_signal(team_id, opponent_team_id),
        )
        closest_margin = (
            min(abs(g.home_score - g.away_score) for g in pair_final_games)
            if pair_final_games
            else 0
        )
        current_team_state = current_snapshot.teams.get(team_id)
        current_opponent_state = current_snapshot.teams.get(opponent_team_id)
        rivalry_draft = rivalry.rivalry_matchup_package(
            team_id,
            opponent_team_id,
            signals,
            has_upcoming_scheduled_game=True,
            team_wins_this_series=wins_team,
            opponent_wins_this_series=wins_opponent,
            closest_margin=closest_margin,
            team_standings_position=(
                current_team_state.standings_position if current_team_state is not None else 0
            ),
            opponent_standings_position=(
                current_opponent_state.standings_position
                if current_opponent_state is not None
                else 0
            ),
            scheduled_game_date=game.game_date.isoformat(),
            season=season,
            phase=phase,
            cycle_index=cycle_index,
            team_named_players=_named_players(team_id),
            opponent_named_players=_named_players(opponent_team_id),
        )
        if rivalry_draft is not None:
            drafts.append(rivalry_draft)

    # -- revenge.py: REVENGE_GAME (MV-2i) --------------------------------
    # Critic-round fix (MV-11 N+1): `get_player_season_stats` used to be
    # called once per traded asset INSIDE this loop -- invisible only
    # because `league_transactions` is empty league-wide right now, a real
    # per-player round trip the moment trades start flowing, exactly the
    # bulk-read discipline this loader's own docstring (above) claims for
    # every other read here. Bulk-fetched once, keyed by player_id, before
    # the loop.
    #
    # `is_first_meeting_since_trade` is MV-15's `revenge.first_meeting_
    # since_trade` helper, fed the pair's `final`-game `simmed_at` stamps --
    # NOT `game_date > txn.created_at.date()` (the ORIGINAL, wrong
    # comparison: `game_date` is the league's simulated calendar,
    # `created_at` is real wall clock, no fixed relationship between them).
    # This loader keeps no derivation logic of its own; see MV-15 before
    # touching this rule.
    revenge_asset_player_ids = {
        asset.player_id
        for txn in season_trades
        for asset in assets_by_transaction.get(txn.id, [])
        if asset.player_id is not None
    }
    revenge_player_season_stats_by_id = await stats_repo.get_player_season_stats_bulk(
        ex, league_id, revenge_asset_player_ids, season, GameType.REGULAR
    )
    for txn in season_trades:
        for asset in assets_by_transaction.get(txn.id, []):
            if (
                asset.player_id is None
                or asset.from_team_id is None
                or asset.to_team_id is None
            ):
                continue
            old_team_id, new_team_id = asset.from_team_id, asset.to_team_id
            # MV-15 item 5: a player traded A->B and then back B->A in the
            # same season would otherwise get one package per asset row, one
            # of which asserts `team_id` for a team he has since left. Skip
            # unless he is still on the team this asset moved him TO.
            if player_team_by_id.get(asset.player_id) != new_team_id:
                continue
            pair = _pair_key(old_team_id, new_team_id)
            upcoming_game = next_game_by_pair.get(pair)
            is_first_meeting_since_trade = revenge.first_meeting_since_trade(
                [g.simmed_at for g in final_games_by_pair.get(pair, ())], txn.created_at
            )
            player_stats = revenge_player_season_stats_by_id.get(asset.player_id)
            games_played = player_stats.games_played if player_stats is not None else 0
            player_motivation = motivation_by_player.get(asset.player_id)
            got_his_wish: bool | None = None
            if player_motivation is not None:
                state_at_trade = motivation_detect.request_state_at(
                    ladder_events_by_player.get(asset.player_id, ()),
                    as_of=txn.created_at,
                    current_request_state=player_motivation.request_state,
                )
                if state_at_trade is RequestState.TRADE_REQUESTED:
                    got_his_wish = True
                elif state_at_trade is RequestState.CONTENT:
                    got_his_wish = False
            current_new_state = current_snapshot.teams.get(new_team_id)
            current_old_state = current_snapshot.teams.get(old_team_id)
            revenge_draft = revenge.revenge_game_package(
                player_id=asset.player_id,
                team_id=new_team_id,
                opponent_team_id=old_team_id,
                trade_season=txn.season,
                trade_asset_count=len(assets_by_transaction.get(txn.id, [])),
                is_first_meeting_since_trade=is_first_meeting_since_trade,
                has_upcoming_scheduled_game=upcoming_game is not None,
                player_games_played_this_season=games_played,
                team_standings_position=(
                    current_new_state.standings_position if current_new_state is not None else 0
                ),
                opponent_standings_position=(
                    current_old_state.standings_position if current_old_state is not None else 0
                ),
                scheduled_game_date=(
                    upcoming_game.game_date.isoformat() if upcoming_game is not None else ""
                ),
                got_his_wish=got_his_wish,
                season=season,
                phase=phase,
                cycle_index=cycle_index,
            )
            if revenge_draft is not None:
                drafts.append(revenge_draft)

    packages: list[StoryPackage] = []
    for draft in drafts:
        sealed = seal_package(draft, team_names=team_names, player_names=player_names)
        if sealed is not None:
            packages.append(sealed)

    return tuple(
        salience.SalienceCandidate(
            package=package,
            subject_prominence=_subject_prominence(package, player_overall_by_id),
        )
        for package in packages
    )


def _pulse_snapshot_to_rows(
    snapshot: pulse.PulseSnapshot, *, league_id: int, season: int
) -> list[MediaPulseSnapshot]:
    """`dba2.media.pulse.PulseSnapshot` (one whole-league object, `dba2/
    media/` pure-layer shape) -> `list[dba2.domain.media.MediaPulseSnapshot]`
    (one row per team, `media_repo`'s own persistence shape). Only this
    file may do this conversion -- `media_repo.py` cannot import `dba2.
    media.pulse` at all (Decision P4-31), and `dba2/media/pulse.py` cannot
    import `dba2.domain.media`'s row dataclass without also importing
    `dba2.data`-adjacent concerns it has no business knowing about -- this
    is the real reason the design doc calls out "the orchestration
    sequencing genuinely has to live in `services/`" for the RNG staging
    (`dba2/media/select.py`'s own module docstring); the identical
    reasoning applies here, one layer down, for the domain/pure boundary."""
    return [
        MediaPulseSnapshot(
            league_id=league_id,
            season=season,
            cycle_index=snapshot.cycle_index,
            team_id=team.team_id,
            wins=team.wins,
            losses=team.losses,
            standings_position=team.standings_position,
            streak_length=team.streak_length,
        )
        for team in snapshot.teams.values()
    ]


def _rows_to_pulse_snapshot(
    rows: list[MediaPulseSnapshot], *, cycle_index: int
) -> pulse.PulseSnapshot:
    """The inverse of `_pulse_snapshot_to_rows` -- `[]` (no persisted prior
    cycle) maps to an EMPTY `PulseSnapshot`, never `None` directly at this
    boundary; `run_media_pulse_cycle` below is the one place that turns
    "no rows" into the real `previous is None` `dba2/media/pulse.py::diff_
    pulse_snapshots` short-circuits on (Decision P4-28), so this helper
    stays a pure, total row->object mapping with no caller-facing
    None-ness decision of its own."""
    return pulse.PulseSnapshot(
        cycle_index=cycle_index,
        teams={
            row.team_id: pulse.TeamPulseState(
                team_id=row.team_id,
                wins=row.wins,
                losses=row.losses,
                standings_position=row.standings_position,
                streak_length=row.streak_length,
                # `MediaPulseSnapshot` (the persisted row) has no `win_pct` column --
                # this diff-only reconstruction (`run_media_pulse_cycle`'s own
                # "previous" snapshot) never reads `.win_pct`, so this is the same
                # `wins/(wins+losses)` convention `pulse.py::_team_win_pct_from_games`
                # uses, recomputed here rather than persisted, not a new formula.
                win_pct=row.wins / (row.wins + row.losses) if (row.wins + row.losses) else 0.0,
            )
            for row in rows
        },
    )


def seal_package(
    draft: StoryPackage, *, team_names: Mapping[int, str], player_names: Mapping[int, str]
) -> StoryPackage | None:
    """**The only constructor of a sealed `StoryPackage`** (docs/design/
    media-v2-newsroom.md MV-1c.1). Takes an UNSEALED draft (`dba2/media/
    detect/legacy.py::adapt_frame`'s own return shape -- `Subject`'s names
    `None`, no identity/universal facts) and:

    1. Resolves `Subject`'s name fields from the two bulk row maps the
       cycle's own loaders already hold (`run_media_pulse_cycle` builds
       both ONCE per cycle, from `team_repo.list_teams_for_league`/
       `player_repo.list_players_for_league`, rather than one lookup per
       candidate).
    2. Calls the pure `dba2.domain.media_facts.identity_facts(subject,
       story_kind)` to derive the name facts FROM the resolved `Subject` --
       never the reverse, so `Subject.player_name` and the `player_name`
       fact are always the same string (MV-1c.1's "nothing to keep in
       sync"). `story_kind` picks only the `opponent_team_name` fact's
       label (Nit 10, critic round on commits `b288b8e`..`99db9f4`) -- it
       does not gate which facts are emitted.
    3. Prepends those identity facts, plus `season`/`phase` (`UNIVERSAL_
       FACT_KEYS`), to the draft's own facts.
    4. Returns `None` if any key in `REQUIRED_FACT_KEYS_BY_KIND[kind]` is
       still absent -- the same "never scored, never persisted" short-
       circuit shape `legacy.py::adapt_frame`'s own `insufficient_data`
       check already uses, one step later.

    `season`/`phase` are rendered as `Fact`s with `FactSource.PULSE_
    SNAPSHOT` -- disclosed, not a perfect fit: neither value is read FROM
    a pulse-snapshot row, but every cycle's own `(season, phase, cycle_
    index)` context is exactly what that table's own primary key carries,
    and none of `FactSource`'s other members is a closer match for "cycle
    metadata" than the one table whose PK literally is that metadata."""
    subject = draft.subject
    resolved_subject = Subject(
        kind=subject.kind,
        player_id=subject.player_id,
        player_name=(
            player_names.get(subject.player_id) if subject.player_id is not None else None
        ),
        team_id=subject.team_id,
        team_name=team_names.get(subject.team_id) if subject.team_id is not None else None,
        opponent_team_id=subject.opponent_team_id,
        opponent_team_name=(
            team_names.get(subject.opponent_team_id)
            if subject.opponent_team_id is not None
            else None
        ),
    )
    season_fact = Fact(
        key="season",
        label="Season",
        value_text=str(draft.season),
        value_number=float(draft.season),
        source=FactSource.PULSE_SNAPSHOT,
    )
    phase_fact = Fact(
        key="phase",
        label="Phase",
        value_text=draft.phase.value,
        value_number=None,
        source=FactSource.PULSE_SNAPSHOT,
    )
    sealed_facts = (
        *identity_facts(resolved_subject, draft.story_kind), season_fact, phase_fact, *draft.facts
    )
    sealed = dataclasses.replace(draft, subject=resolved_subject, facts=sealed_facts)

    required = REQUIRED_FACT_KEYS_BY_KIND.get(sealed.story_kind, frozenset())
    present = {fact.key for fact in sealed.facts}
    if not required <= present:
        return None
    return sealed


@dataclasses.dataclass(frozen=True)
class MediaPulseCycleResult:
    """Decision P4-103, Part A -- the disclosed instrumentation seam
    `run_media_pulse_cycle` returns instead of a bare 2-tuple.

    **docs/design/media-v2-newsroom.md MV-1c.4, "Build order" step 3b: this
    function no longer scores.** `top_candidates`/`candidates_scored`/
    `candidates_above_floor` are GONE from this result -- they were
    arm-dependent quantities living on the wrong side of the pulse/
    selection boundary (two selection calls over the SAME pulse result,
    against two different libraries, must not silently share one scoring
    pass). `candidates` is the raw, UNSCORED `SalienceCandidate` tuple
    every sealed package this cycle produced; `run_media_selection_cycle`
    scores it (`salience.score_candidates`), and `candidates_scored`/
    `candidates_above_floor` now live on ITS OWN result
    (`MediaSelectionCycleResult`), one per arm.

    **Named fields, not a bare tuple, a mutable out-parameter, or an
    observer callback** -- see Decision P4-103's own "three shapes
    rejected" section: a tuple fails with a bare arity error at a future
    field, an out-parameter can be silently absent (this project's
    standing "absent is neutral, never a fabricated zero" rule, Decision
    P4-28/P4-34), and a callback's firing order relative to the snapshot
    write is not obvious from a caller's own code.

    **The one invariant this seam still holds** (Decision P4-103's own
    "measurement never influences selection," restated for the split):
    the sequence of values handed to `media_repo.upsert_pulse_snapshot_
    teams` is byte-identical before and after this seam existed, and
    `candidates` is exactly the sequence `salience.score_candidates` would
    have been called with pre-split -- this function computes it and stops,
    it never scores it itself."""

    snapshot: pulse.PulseSnapshot
    candidates: tuple[salience.SalienceCandidate, ...]
    team_names: Mapping[int, str] = dataclasses.field(default_factory=dict)
    """docs/design/media-v2-newsroom.md MV-12.1 -- the exact `{team_id:
    "City Name"}` map this function already builds, ONE BULK READ, for
    `seal_package`'s own per-candidate name resolution a few lines below
    (`team_repo.list_teams_for_league`, unconditional, every cycle). Added
    to this result (not a second read) so `LeagueBrief`'s §1 standings
    section -- which needs every LEAGUE team's name, not just the handful
    a candidate's own `Subject` already carries -- has a real name map
    available on every cycle, including a REGULAR_SEASON cycle before the
    trade deadline where `season_service._run_media_cycle`'s own
    `league_snapshot`/`gm_context` pair is `None` (MV-11's disclosed
    `NotFoundError` fallback) and would otherwise leave the brief with no
    name source at all. Defaulted to `{}` so a hand-built test fixture
    predating this field keeps constructing `MediaPulseCycleResult`
    unchanged."""


_PROMINENCE_OVERALL_FLOOR = 60.0
_PROMINENCE_OVERALL_SPAN = 35.0
"""MV-5's own real prominence anchor, verbatim: `(overall_cached - 60) /
35`, clamped `[0, 1]` -- a 60-overall player reads as `0.0` (no
prominence boost), a 95-overall (or better) player clamps to `1.0`."""


def _subject_prominence(
    package: StoryPackage, player_overall_by_id: Mapping[int, int]
) -> float | None:
    """MV-5, "Build order" step 6: `SalienceCandidate.subject_prominence`'s
    first real signal (it has been `None`-neutral forever before this).
    Wired for every PLAYER-subject candidate using the ALREADY-loaded
    `overall_cached` bulk map (`run_media_pulse_cycle`'s own name-resolution
    load, reused rather than a second read) -- **a disclosed narrowing of
    MV-5's own text**, which also names `motivation.star_leverage / 100` as
    an alternative anchor specifically for a motivation-scoped subject
    (`PLAYER_UNREST`/`HOLDOUT_WATCH`). Loading `player_motivation` a second
    time just to differentiate two kinds from every other player-subject
    kind was judged disproportionate to what it would buy -- `overall_
    cached` is a real, meaningful prominence signal for those two kinds
    too (a star's holdout is more newsworthy than a bench player's, the
    identical reasoning MV-5 gives for the general case). `None` (neutral,
    unchanged) for a TEAM-subject candidate -- MV-5's own text: "no wiring
    exists for one" -- and for a player id this cycle's bulk load does not
    carry (defensive; should not happen for a real candidate)."""
    player_id = package.subject.player_id
    if player_id is None:
        return None
    overall = player_overall_by_id.get(player_id)
    if overall is None:
        return None
    return max(0.0, min((overall - _PROMINENCE_OVERALL_FLOOR) / _PROMINENCE_OVERALL_SPAN, 1.0))


async def run_media_pulse_cycle(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    cycle_index: int,
) -> MediaPulseCycleResult:
    """One cycle's full vigilance pass (Decision P4-27): reads its own real
    prior snapshot from `media_repo` (Decision P4-28's "absent is neutral,
    never zero" -- no persisted rows reads as `previous=None`, never a
    fabricated zero baseline), builds the current pulse snapshot, diffs the
    two, combines the result with the real motivation event intake,
    (Stage D step 21, Decision P4-107 item 1) the real prior-season draft
    class's own rookie-watch intake, and (Stage D step 23, Decision
    P4-121 item 4) the real career-scoring-leaderboard milestone intake,
    derives a `StoryFrame` per candidate
    (`dba2/media/frame.py`, step 16), scores every SUFFICIENT frame
    (Decision P4-29), writes the current snapshot back to `media_repo` for
    next cycle's own read, and returns the top-K above the floor.

    **Stage D step 19 change: no longer takes `previous_snapshot` as a
    caller-supplied argument.** Step 17's version left "read/persist the
    prior snapshot" as the CALLER's job because no persistence layer
    existed yet (this function's own docstring said so explicitly). Now
    that `media_repo.get_prior_pulse_snapshot_teams`/`upsert_pulse_
    snapshot_teams` are real, this function does its own read-then-diff-
    then-write, once per cycle per team, exactly as this step's own charge
    requires -- a caller no longer needs to thread a snapshot from one call
    to the next by hand, and (Decision P4-28's own "a missed cycle widens
    the window, never corrupts it") the repo read is against the most
    recent PERSISTED prior cycle, not a literal `cycle_index - 1`, so a
    caller that skips a cycle still gets a correct, if coarser, diff rather
    than a silently-wrong one.

    Returns a `MediaPulseCycleResult` (Decision P4-103, Part A -- see that
    dataclass's own docstring for why a bare tuple stopped being enough):
    `snapshot` is `current_snapshot`, already durably persisted by the
    time this function returns (a caller that wants it for display/logging
    no longer has anywhere else it needs to write it); `candidates` is
    every sealed, UNSCORED `SalienceCandidate` this cycle's real producers
    combined to build. `subject_prominence`/`cycles_since_last_seen` are
    left at their neutral/max-novelty defaults for every candidate this
    function builds -- no production caller wires a real talent/history
    signal into either yet (that needs the persona/library layer's own
    subject-tracking, still unbuilt); `cycles_since_last_seen`'s real value
    is filled in downstream, per arm, by `run_media_selection_cycle`'s own
    `subject_last_seen` parameter (MV-1c.4), never here.

    **docs/design/media-v2-newsroom.md MV-1c.4, "Build order" step 3b: this
    function no longer scores.** Step 3a already sealed every candidate
    into a `StoryPackage` before this step existed; step 3b removes the
    `salience.score_candidates`/`salience.select_top_k` calls that used to
    follow -- scoring is now `run_media_selection_cycle`'s own first act,
    because it is the arm-dependent side of the boundary (two selection
    calls over this SAME pulse result, against two different libraries,
    must each score independently; this function cannot know in advance
    how many arms will consume its output, so it cannot score on their
    behalf). This function's own SIGNATURE stays exactly `(ex, league_id,
    season, cycle_index)` -- unchanged by this step, matching MV-1c.4's own
    rule that this function gains neither `phase` nor the library (`phase`
    is still read once, below, only because sealing a package needs SOME
    real `Phase` value).

    **docs/design/media-v2-newsroom.md, "Build order" step 3a: every
    candidate is now sealed into a `StoryPackage` before scoring, not
    scored as a bare `StoryFrame`.** `StoryFrame`/`derive_frame` are
    unchanged (`dba2/media/frame.py`, MV-8: "nothing closed and tuned gets
    discarded") and every real producer above is called exactly as before
    -- only the packaging step between "a frame exists" and "score it" is
    new: `dba2/media/detect/legacy.py::adapt_frame` turns each SUFFICIENT
    frame into an unsealed package (dropping an insufficient one, the
    relocated short-circuit), and `seal_package` above resolves its
    `Subject`'s names and completes it. **This function's own SIGNATURE is
    unchanged** (`ex, league_id, season, cycle_index` -- no `phase`
    parameter, matching MV-1c.4's own rule that this function gains
    neither `phase` nor the library); `phase` is instead read once, from
    the league's own current row (`league_repo.get_league`), because
    sealing a package needs SOME real `Phase` value and this function
    always runs as part of that phase's own closing work
    (`season_service._run_media_cycle`'s calling convention) -- one extra,
    disclosed read, not a new caller-facing parameter."""
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise ValueError(f"league {league_id} does not exist")
    phase = league.current_phase

    prior_rows = await media_repo.get_prior_pulse_snapshot_teams(
        ex, league_id=league_id, season=season, before_cycle_index=cycle_index
    )
    previous_snapshot = (
        None
        if not prior_rows
        else _rows_to_pulse_snapshot(prior_rows, cycle_index=prior_rows[0].cycle_index)
    )

    current_snapshot = await compute_current_pulse_snapshot(
        ex, league_id=league_id, season=season, cycle_index=cycle_index
    )
    continuous_candidates = pulse.diff_pulse_snapshots(previous_snapshot, current_snapshot)
    motivation_candidates = await load_player_unrest_candidates(
        ex, league_id=league_id, season=season
    )
    rookie_watch_candidates = await load_rookie_watch_candidates(
        ex, league_id=league_id, season=season
    )
    milestone_candidates = await load_milestone_candidates(
        ex, league_id=league_id, season=season
    )

    frames: tuple[StoryFrame, ...] = tuple(
        derive_frame(candidate)
        for candidate in (
            *continuous_candidates,
            *motivation_candidates,
            *rookie_watch_candidates,
            *milestone_candidates,
        )
    )

    # Bulk name maps, built ONCE per cycle -- `seal_package` reads both for
    # every candidate rather than one `team_repo.get_team`/`player_repo.
    # get_player` round trip per candidate (the pre-retype shape, which
    # only ever resolved names for the top-K SELECTED stories). This is a
    # real, disclosed cost-shape change (MV-1c.1: names are resolved once,
    # before the package is sealed, so every SUFFICIENT candidate now gets
    # a name lookup, not just the ones that go on to be published) --
    # bulk-loaded rather than per-candidate specifically to keep that cost
    # at two queries a cycle, never O(candidates).
    team_rows = await team_repo.list_teams_for_league(ex, league_id)
    team_names = {team.id: f"{team.city} {team.name}" for team in team_rows}
    player_rows = await player_repo.list_players_for_league(ex, league_id)
    player_names = {player.id: f"{player.first_name} {player.last_name}" for player in player_rows}
    player_overall_by_id = {player.id: player.overall_cached for player in player_rows}

    packages: list[StoryPackage] = []
    for frame in frames:
        draft = legacy.adapt_frame(frame, season=season, phase=phase, cycle_index=cycle_index)
        if draft is None:
            continue
        sealed = seal_package(draft, team_names=team_names, player_names=player_names)
        if sealed is not None:
            packages.append(sealed)

    candidates = tuple(
        salience.SalienceCandidate(
            package=package,
            subject_prominence=_subject_prominence(package, player_overall_by_id),
        )
        for package in packages
    )

    await media_repo.upsert_pulse_snapshot_teams(
        ex, _pulse_snapshot_to_rows(current_snapshot, league_id=league_id, season=season)
    )
    return MediaPulseCycleResult(
        snapshot=current_snapshot,
        candidates=candidates,
        team_names=team_names,
    )


# ---------------------------------------------------------------------------
# Stage D step 18 -- the media phrase library loader (Decisions P4-21/P4-25/
# P4-26/P4-53), and real slot-binding resolution (P4-22/P4-38/P4-54). See
# this file's own module docstring for why both live here rather than in a
# new, separately-importing file.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MEDIA_PHRASE_LIBRARY_PATH = _REPO_ROOT / "seeds" / "media_phrases.json"
"""Mirrors `dba2/services/phrase_service.py::DEFAULT_PHRASE_LIBRARY_PATH`'s
own `seeds/` convention exactly (Decision P3-17 item 3, extended to media
by Decision P4-21) -- a SEPARATE file (`seeds/media_phrases.json`), never
`seeds/gm_phrases.json` itself: Decision P4-21/P4-33 both forbid moving a
`GM_TUNING_VERSION`-gated file to help a new feature, and the two libraries
are independently tunable by design, not sharing a knob."""

_VALID_OPS = frozenset({"lt", "lte", "gte", "gt", "eq", "in"})
"""The identical closed six-operator set `dba2/services/phrase_service.py::
_VALID_OPS` declares -- `engine.py::_trigger_holds`'s own operator table is
frame-agnostic and this is a structural fact about the ENGINE, not a
per-library tunable Decision P4-21's "not shared constants" rule is about
-- so an independent declaration here (rather than an import) matches
`phrase_service.py`'s own precedent of not sharing this particular set
with `engine.py`'s own function-local copy either."""

_VALID_STORY_KINDS = frozenset(kind.value for kind in StoryKind)

_PACKAGE_PSEUDO_FEATURES = frozenset(
    {"package_story_kind", "package_angle", "package_fact_count"}
)
"""The trigger view's own namespaced pseudo-features (`dba2/media/
select.py::_PackageTriggerView`, MV-1c.1 rule 4) -- legal trigger targets
for any template regardless of its declared `kinds`, since every package
of every kind carries all three. Declared independently here rather than
imported from `select.py` (that module keeps its own copy private, `_`-
prefixed) -- this is the identical "two closed constants, same value,
declared where each is used" precedent `_VALID_OPS`'s own docstring
already states for `engine.py`'s operator table."""


def _template_fact_vocabulary(template: select.MediaTemplate) -> frozenset[str]:
    """**MV-1b(d)/MV-1c.1: `_VALID_FEATURES` stops being `dataclasses.
    fields(StoryFrame)` and becomes a check per template against its own
    declared kinds.** A wildcard-`kinds` template (`template.kinds == ()`)
    may use only `UNIVERSAL_FACT_KEYS`; an explicit-`kinds` template may
    use `UNIVERSAL_FACT_KEYS` plus the UNION of every one of its declared
    kinds' `FACT_KEYS_BY_KIND` entry (Decision P3-18's own vocabulary rule,
    now per-kind rather than one flat list). Every one of `_validate_media_
    trigger_vocabulary`, `_validate_media_slot_vocabulary` and `_validate_
    media_slot_placeholder_coverage` reads this SAME expression -- MV-1b(d)
    is explicit that all three must, or a per-check drift risk reopens the
    exact class of bug Decision P4-103 Part A's extraction already closed
    once for `above_floor`."""
    if not template.kinds:
        return frozenset(UNIVERSAL_FACT_KEYS)
    vocabulary: set[str] = set(UNIVERSAL_FACT_KEYS)
    for kind_value in template.kinds:
        # An unknown kind value is `_validate_media_kind_vocabulary`'s own
        # failure to report -- this function is called after that check
        # would already have raised for a real `load_media_library` run,
        # but stays defensive (skips rather than raising a second error
        # shape) for a caller that invokes it directly, e.g. a test.
        kind = StoryKind(kind_value) if kind_value in _VALID_STORY_KINDS else None
        if kind is not None:
            vocabulary |= set(FACT_KEYS_BY_KIND.get(kind, {}))
    return frozenset(vocabulary)


_VALID_PERSONAS = frozenset({"beat_writer", "hot_take", "analyst", "insider", "historian"})
"""Decision P4-53's exactly-five closed persona set, verbatim -- the same
"listed, not counted, so it cannot drift" convention `StoryKind`/
`SituationToken` already use, applied to a set this loader must validate
the library's own `personas` array against rather than merely trust."""


@dataclasses.dataclass(frozen=True)
class LoadedMediaLibrary:
    """The validated, in-memory media library -- `templates`/`personas`
    plus the two knobs Decision P4-21 puts in the file rather than in code
    (`repeat_penalty`, `select_media_template`'s own required parameter;
    `persona_exclusivity_floor`, this loader's own analogue of Decision
    P3-31's `exclusive_mass_share`), both independent values from `seeds/
    gm_phrases.json`'s own knobs of the same shape. `fallback_by_kind`
    (MV-1c.2) is the one no-trigger, wildcard-voice template per `StoryKind`
    `_validate_media_fallback_coverage` already proves exists and is
    unique -- `select_media_template` resolves it directly by lookup when
    the real eligible-and-triggered survivor set is empty, rather than
    trusting the fallback to still be in that set after every filter."""

    templates: select.MediaTemplateLibrary
    personas: tuple[select.MediaPersona, ...]
    repeat_penalty: float
    persona_exclusivity_floor: float
    fallback_by_kind: Mapping[StoryKind, select.MediaTemplate]
    persona_metadata: Mapping[str, PersonaMetadata] = dataclasses.field(default_factory=dict)
    """MV-3/MV-3h/MV-6's own per-persona voice fields, keyed by persona
    `key` -- defaulted to `{}` so every hand-built `LoadedMediaLibrary` in
    the existing test suite (built before this field existed) keeps
    constructing without edit; the real, shipped library always populates
    all five keys (`_validate_media_persona_metadata`)."""
    phase_cadence: Mapping[str, Mapping[str, PhaseCadence]] = dataclasses.field(
        default_factory=dict
    )
    """MV-3's own cadence table -- `{persona_key: {phase_value: PhaseCadence}}`,
    defaulted to `{}` for the identical backward-compatibility reason as
    `persona_metadata` above. Consuming it (a per-candidate `persona_
    multipliers` composition, and the `max_per_cycle` cap) is `media_
    service` composition -- a later build step's own wiring, per MV-3h.1."""
    team_profile_quota: int = 3
    """MV-2h.3's own flat reserved quota, `3` per cycle across all nine
    cycle-bearing phases -- defaulted to the real, disclosed value (never
    `0`) so a hand-built library that omits it still reads MV-2h.3's own
    number rather than a silent zero that would look like "profiles
    disabled" instead of "field omitted.\""""


def _fallback_templates_by_kind(
    templates: select.MediaTemplateLibrary,
) -> dict[StoryKind, select.MediaTemplate]:
    """One no-trigger, wildcard-voice template per `StoryKind` -- the same
    predicate `_validate_media_fallback_coverage` below already checks for
    "exactly one," factored out so the loader and the validator share one
    definition of "is this template a fallback for this kind" rather than
    two independently-written copies (`_selectable_weight_for_persona`'s
    own docstring already names this drift class as demonstrated, not
    hypothetical, in this file)."""
    result: dict[StoryKind, select.MediaTemplate] = {}
    for kind in StoryKind:
        for template in templates:
            if not template.triggers and not template.voices and (
                not template.kinds or kind.value in template.kinds
            ):
                result[kind] = template
                break
    return result


def _parse_media_trigger(row: dict) -> select.MediaTrigger:
    return select.MediaTrigger(feature=row["feature"], op=row["op"], value=row["value"])


def _parse_media_template(row: dict) -> select.MediaTemplate:
    return select.MediaTemplate(
        id=row["id"],
        kinds=tuple(row.get("kinds", ())),
        voices=tuple(row.get("voices", ())),
        triggers=tuple(_parse_media_trigger(t) for t in row.get("triggers", ())),
        weight=row["weight"],
        text=row["text"],
        slots=tuple(row.get("slots", ())),
        angles=tuple(row.get("angles", ())),
        tiers=tuple(row.get("tiers", ())),
    )


def _parse_media_persona(row: dict) -> select.MediaPersona:
    return select.MediaPersona(
        key=row["key"], story_kind_weights=dict(row.get("story_kind_weights", {}))
    )


@dataclasses.dataclass(frozen=True)
class PersonaMetadata:
    """The STATIC, library-declared half of one persona's voice
    (docs/design/media-v2-newsroom.md MV-3/MV-3h/MV-6) -- `column`,
    `display_name`, `beat_description`, `register`, `voice_rules`,
    `fences`, one row per persona in `seeds/media_phrases.json`. Kept
    separate from `select.MediaPersona` (which carries only `key`/`story_
    kind_weights`, the SELECTION-time shape `select_persona` reads)
    because these fields serve a different reader -- a writer backend's
    voice, never the weighted-choice draw -- the same "one dataclass per
    concern" split `StoryFrame`/`StoryPackage` already established one
    layer over.

    **`display_name` is the ONE field this dataclass leaves optional, and
    it is optional for a real reason, not an oversight.** MV-3 gives
    exactly two personas (`hot_take`, `beat_writer`) a fixed, library-
    declared display name ("Duke Sabatino", "Jordan Ellis"); the other
    three draw a real, `_generate_writer_name`-generated name per league
    and have no FIXED one to declare. `resolve_persona_voice` below is
    where a caller turns `None` into the real generated name for those
    three -- this dataclass itself never fabricates one."""

    column: str
    display_name: str | None
    beat_description: str
    register: Register
    voice_rules: tuple[str, ...]
    fences: tuple[str, ...]


def _parse_persona_metadata(row: dict) -> PersonaMetadata:
    return PersonaMetadata(
        column=row["column"],
        display_name=row.get("display_name"),
        beat_description=row["beat_description"],
        register=Register(row["register"]),
        voice_rules=tuple(row["voice_rules"]),
        fences=tuple(row["fences"]),
    )


def resolve_persona_voice(
    metadata: PersonaMetadata, key: str, *, generated_name: str
) -> PersonaVoice:
    """The one function that turns a `PersonaMetadata` (library-static)
    into a real `PersonaVoice` (MV-6's own writer-facing shape) -- `
    display_name` is the library's own declared value when it has one
    (`hot_take`, `beat_writer`), or `generated_name` (the caller's own
    already-resolved `MediaWriter.name`, from `get_or_create_media_writer`)
    for the three personas with no fixed one. Pure: this function never
    resolves a name itself, only chooses between two the caller already
    has -- the identical "mirror, never resolve" discipline `dba2.domain.
    media_facts.identity_facts` already follows for a package's own
    subject names."""
    return PersonaVoice(
        key=key,
        display_name=metadata.display_name if metadata.display_name is not None else generated_name,
        column=metadata.column,
        beat_description=metadata.beat_description,
        register=metadata.register,
        voice_rules=metadata.voice_rules,
        fences=metadata.fences,
    )


def _validate_media_persona_set(personas: tuple[select.MediaPersona, ...]) -> None:
    """Decision P4-53 -- the persona set is exactly the five closed keys,
    no more, no fewer, no duplicates. A library that ships a sixth persona
    (or misspells one of the five) fails here, at load time, rather than
    silently under- or over-covering `_validate_media_every_kind_has_a_
    persona`'s own coverage check below."""
    keys = [persona.key for persona in personas]
    if len(keys) != len(set(keys)):
        raise ValueError("media_phrases.json: duplicate persona key in 'personas'")
    if set(keys) != _VALID_PERSONAS:
        raise ValueError(
            f"media_phrases.json: 'personas' must name exactly {sorted(_VALID_PERSONAS)} "
            f"(Decision P4-53) -- got {sorted(keys)}"
        )


def _validate_media_persona_kind_vocabulary(personas: tuple[select.MediaPersona, ...]) -> None:
    """Every key a persona's `story_kind_weights` names must be a real
    `StoryKind` value -- the persona-side analogue of `_validate_media_
    kind_vocabulary` below."""
    for persona in personas:
        for kind in persona.story_kind_weights:
            if kind not in _VALID_STORY_KINDS:
                raise ValueError(
                    f"media_phrases.json: persona {persona.key!r} names unknown story kind "
                    f"{kind!r} (valid: {sorted(_VALID_STORY_KINDS)})"
                )


_REQUIRED_PERSONA_METADATA_STRING_KEYS = ("column", "beat_description")


def _validate_media_persona_metadata(payload_personas: list[dict]) -> None:
    """docs/design/media-v2-newsroom.md MV-3/MV-3h/MV-6's own per-persona
    voice fields -- validated against the RAW JSON rows (before `Register(
    ...)`'s own enum construction would raise a less-actionable error), the
    same "validate before parse, for a friendlier authoring error" shape
    `_validate_media_repeat_penalty`/`_validate_media_exclusivity_floor_
    value` already use for the file's other two top-level knobs.
    `display_name` is the one field deliberately NOT required here -- see
    `PersonaMetadata`'s own docstring for why exactly two personas (`hot_
    take`, `beat_writer`) declare one and the other three do not."""
    valid_registers = {member.value for member in Register}
    for row in payload_personas:
        key = row.get("key", "<missing key>")
        for field in _REQUIRED_PERSONA_METADATA_STRING_KEYS:
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(
                    f"media_phrases.json: persona {key!r} must declare a non-empty string "
                    f"{field!r}"
                )
        register = row.get("register")
        if register not in valid_registers:
            raise ValueError(
                f"media_phrases.json: persona {key!r} has unknown 'register' {register!r} "
                f"(valid: {sorted(valid_registers)})"
            )
        voice_rules = row.get("voice_rules")
        if not isinstance(voice_rules, list) or not voice_rules or not all(
            isinstance(rule, str) and rule.strip() for rule in voice_rules
        ):
            raise ValueError(
                f"media_phrases.json: persona {key!r} must declare a non-empty list of "
                "non-empty string 'voice_rules'"
            )
        fences = row.get("fences")
        if not isinstance(fences, list) or not all(
            isinstance(fence, str) and fence.strip() for fence in fences
        ):
            raise ValueError(
                f"media_phrases.json: persona {key!r}'s 'fences' must be a list of "
                "non-empty strings (an empty list is allowed -- not every persona needs one)"
            )
        display_name = row.get("display_name")
        if display_name is not None and (
            not isinstance(display_name, str) or not display_name.strip()
        ):
            raise ValueError(
                f"media_phrases.json: persona {key!r}'s 'display_name' must be a non-empty "
                "string when declared, or omitted entirely"
            )


def _validate_media_every_kind_has_a_persona(personas: tuple[select.MediaPersona, ...]) -> None:
    """This loader's own added invariant (disclosed, not required in so
    many words by any single decision): every `StoryKind` must have at
    least one persona with a POSITIVE preference weight, or `dba2/media/
    select.py::select_persona` can never draw a writer for it at all --
    the zero-eligible-personas case that function's own docstring says
    should be load-time-unreachable. Checked here so a library that leaves
    a kind uncovered fails at load time, not the first time vigilance ever
    produces a real candidate of that kind."""
    covered = {
        kind
        for persona in personas
        for kind, weight in persona.story_kind_weights.items()
        if weight > 0.0
    }
    missing = _VALID_STORY_KINDS - covered
    if missing:
        raise ValueError(
            "media_phrases.json: every StoryKind must have at least one persona with a "
            f"positive preference weight -- missing: {sorted(missing)}"
        )


_CYCLE_BEARING_PHASE_VALUES: frozenset[str] = frozenset(
    {
        Phase.PRESEASON.value, Phase.REGULAR_SEASON.value, Phase.TRADE_DEADLINE.value,
        Phase.POST_DEADLINE.value, Phase.DRAFT_LOTTERY.value, Phase.DRAFT.value,
        Phase.RESIGN_WINDOW.value, Phase.FREE_AGENCY.value, Phase.WAIVERS.value,
    }
)
"""MV-3's own cadence table names exactly these nine phases -- duplicated
here BY VALUE from `dba2.services.season_service.CYCLE_BEARING_PHASES`,
never by import: `season_service` imports THIS module
(`run_media_pulse_cycle`/`run_media_selection_cycle`), so the reverse
import would be circular. `tests/unit/services/test_media_service.py`
pins the two constants equal, the same "duplicated, but pinned, when a
real import would cycle" shape `tools/generate_media_corpus.py`'s own
`_CYCLE_PHASES` module constant already uses for an unrelated pair."""


@dataclasses.dataclass(frozen=True)
class PhaseCadence:
    """One `(persona, phase)` cell of MV-3's cadence table -- `weight_
    multiplier` scales `story_kind_weights[kind]` inside the persona draw;
    `max_per_cycle` is the hard cap `run_media_selection_cycle` enforces
    (both consumed by a later build step's own wiring, MV-3h.1; this build
    step only loads and validates the table)."""

    weight_multiplier: float
    max_per_cycle: int


def _validate_media_phase_cadence(payload: dict) -> None:
    """MV-3's own cadence table, validated against the raw JSON before
    `_parse_phase_cadence` trusts its shape -- every one of the five
    personas must name exactly the nine cycle-bearing phases, and every
    cell must be a real `[weight, max_per_cycle]` pair with a positive
    weight and a positive integer cap."""
    cadence = payload.get("phase_cadence")
    if not isinstance(cadence, dict) or set(cadence) != _VALID_PERSONAS:
        got = sorted(cadence) if isinstance(cadence, dict) else cadence
        raise ValueError(
            "media_phrases.json: 'phase_cadence' must be an object naming exactly "
            f"{sorted(_VALID_PERSONAS)} -- got {got!r}"
        )
    for persona_key, per_phase in cadence.items():
        if not isinstance(per_phase, dict) or set(per_phase) != _CYCLE_BEARING_PHASE_VALUES:
            raise ValueError(
                f"media_phrases.json: 'phase_cadence[{persona_key!r}]' must name exactly the "
                f"nine cycle-bearing phases {sorted(_CYCLE_BEARING_PHASE_VALUES)} -- got "
                f"{sorted(per_phase) if isinstance(per_phase, dict) else per_phase!r}"
            )
        for phase_value, cell in per_phase.items():
            if not isinstance(cell, list) or len(cell) != 2:
                raise ValueError(
                    f"media_phrases.json: 'phase_cadence[{persona_key!r}][{phase_value!r}]' "
                    f"must be a [weight, max_per_cycle] pair, got {cell!r}"
                )
            weight, cap = cell
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
                raise ValueError(
                    f"media_phrases.json: 'phase_cadence[{persona_key!r}][{phase_value!r}]' "
                    f"weight must be a positive number, got {weight!r}"
                )
            if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
                raise ValueError(
                    f"media_phrases.json: 'phase_cadence[{persona_key!r}][{phase_value!r}]' "
                    f"max_per_cycle must be a positive integer, got {cap!r}"
                )


def _parse_phase_cadence(payload: dict) -> dict[str, dict[str, PhaseCadence]]:
    return {
        persona_key: {
            phase_value: PhaseCadence(weight_multiplier=float(cell[0]), max_per_cycle=int(cell[1]))
            for phase_value, cell in per_phase.items()
        }
        for persona_key, per_phase in payload["phase_cadence"].items()
    }


def _validate_media_team_profile_quota(payload: dict) -> int:
    """MV-2h.3's own flat reserved quota -- one top-level integer, not a
    per-phase table (MV-2h.3's own text: "one block," and MV-3's cadence
    table shows the identical `3` in every one of its nine phase columns,
    confirming there is nothing per-phase to declare)."""
    quota = payload.get("team_profile_quota")
    if isinstance(quota, bool) or not isinstance(quota, int) or quota <= 0:
        raise ValueError(
            f"media_phrases.json: 'team_profile_quota' must be a positive integer, got {quota!r}"
        )
    return quota


def _validate_media_kind_vocabulary(templates: select.MediaTemplateLibrary) -> None:
    for template in templates:
        for kind in template.kinds:
            if kind not in _VALID_STORY_KINDS:
                raise ValueError(
                    f"media_phrases.json: template {template.id!r} names unknown story kind "
                    f"{kind!r} (valid: {sorted(_VALID_STORY_KINDS)})"
                )


def _validate_media_voice_vocabulary(
    templates: select.MediaTemplateLibrary, personas: tuple[select.MediaPersona, ...]
) -> None:
    valid_keys = {persona.key for persona in personas}
    for template in templates:
        for voice in template.voices:
            if voice not in valid_keys:
                raise ValueError(
                    f"media_phrases.json: template {template.id!r} names unknown persona "
                    f"{voice!r} (valid: {sorted(valid_keys)})"
                )


def _validate_media_trigger_vocabulary(templates: select.MediaTemplateLibrary) -> None:
    """Decision P3-18's vocabulary check, extended to media -- a
    `MediaTrigger` naming an unknown `feature`/`op` is a load-time error.
    **MV-1b(d)/MV-1c.1: checked per template against `_template_fact_
    vocabulary` (that template's own declared kinds) plus the trigger
    view's own namespaced pseudo-features**, not one flat `StoryFrame`
    field list -- this is the check whose failure IS build step 3a's
    acceptance test: the two shipped triggers on `motivation_magnitude`/
    `request_state` (`seeds/media_phrases.json:132,222`) must resolve
    against `FACT_KEYS_BY_KIND[PLAYER_UNREST/HOLDOUT_WATCH]`, or the
    unmodified shipped library fails to load."""
    for template in templates:
        valid_features = _template_fact_vocabulary(template) | _PACKAGE_PSEUDO_FEATURES
        for trigger in template.triggers:
            if trigger.feature not in valid_features:
                raise ValueError(
                    f"media_phrases.json: template {template.id!r} triggers on unknown "
                    f"feature {trigger.feature!r} -- not a declared fact key for its own "
                    f"kind(s) (valid: {sorted(valid_features)})"
                )
            if trigger.op not in _VALID_OPS:
                raise ValueError(
                    f"media_phrases.json: template {template.id!r} uses unknown operator "
                    f"{trigger.op!r} -- must be one of {sorted(_VALID_OPS)}"
                )


def _validate_media_fallback_coverage(templates: select.MediaTemplateLibrary) -> None:
    """Decision P4-25 -- exactly one no-trigger, wildcard-voice (`voices ==
    ()`) fallback template per `StoryKind`, mirroring `dba2/services/
    phrase_service.py::_validate_fallback_coverage`'s own shape with
    `voices` standing in for `philosophies`/`windows` and `kinds` standing
    in for `situations` -- a fallback's `kinds` may be wildcard (matches
    every kind) or explicitly contain the kind, the same either-shape
    `_validate_fallback_coverage` already accepts.

    **MV-1c.2, mechanism one: a fallback is additionally dimension-blind
    and REQUIRED-only, checked here at load time.** For every template
    this function identifies as a fallback: `angles == ()` and `tiers ==
    ()` (wildcard on both of MV-1b(c)'s new dimensions -- a fallback that
    declared either would be ineligible for some of its own kind's
    packages while still counting as the kind's mandatory fallback,
    MV-1c.2's own `IndexError` reproduction), and every declared slot is in
    `REQUIRED_FACT_KEYS_BY_KIND[k]` for every kind `k` this fallback covers
    (an explicit-`kinds` fallback: that one kind; a wildcard-`kinds`
    fallback: the intersection across every kind, `UNIVERSAL_FACT_KEYS` in
    practice -- no shipped template is wildcard-`kinds`, MV-1c.2's own
    verified note). This supersedes revision 4's withdrawn "only
    `UNIVERSAL_FACT_KEYS` slots" rule (MV-1b(e)), which failed against six
    of the eleven shipped fallbacks (MV-1c.1's own verified count)."""
    missing: list[str] = []
    duplicated: list[str] = []
    dimensioned: list[str] = []
    bad_slots: list[str] = []
    for kind in StoryKind:
        fallbacks = [
            t
            for t in templates
            if not t.triggers and not t.voices and (not t.kinds or kind.value in t.kinds)
        ]
        if len(fallbacks) == 0:
            missing.append(kind.value)
        elif len(fallbacks) > 1:
            duplicated.append(f"{kind.value} ({sorted(t.id for t in fallbacks)})")
        for fallback in fallbacks:
            if fallback.angles or fallback.tiers:
                dimensioned.append(fallback.id)
            covered_kinds = (
                tuple(StoryKind(k) for k in fallback.kinds) if fallback.kinds else tuple(StoryKind)
            )
            required_intersection = (
                set.intersection(
                    *(set(REQUIRED_FACT_KEYS_BY_KIND.get(k, frozenset())) for k in covered_kinds)
                )
                if covered_kinds
                else set()
            )
            if not set(fallback.slots) <= required_intersection:
                bad_slots.append(fallback.id)
    if missing or duplicated:
        parts = []
        if missing:
            parts.append(f"missing a fallback for: {missing}")
        if duplicated:
            parts.append(f"more than one fallback for: {duplicated}")
        raise ValueError(
            "media_phrases.json: Decision P4-25 requires exactly one no-trigger, "
            "wildcard-voice fallback template per story kind -- " + "; ".join(parts)
        )
    if dimensioned:
        raise ValueError(
            "media_phrases.json: a fallback template must be dimension-blind (MV-1c.2) -- "
            f"declares a non-wildcard 'angles' or 'tiers': {sorted(set(dimensioned))}"
        )
    if bad_slots:
        raise ValueError(
            "media_phrases.json: a fallback template's slots must all be in "
            "REQUIRED_FACT_KEYS_BY_KIND for every kind it covers (MV-1c.2) -- "
            f"violated by: {sorted(set(bad_slots))}"
        )


def _validate_media_slot_vocabulary(templates: select.MediaTemplateLibrary) -> None:
    """Every declared slot name must be a real fact key this template's own
    kind(s) can carry -- **MV-1b(d)/MV-1c.1: `select.SLOT_NAMES` (the old
    nine-name closed set) is gone with the resolvers it named; the check is
    now `_template_fact_vocabulary(template)`, the identical expression
    `_validate_media_trigger_vocabulary` reads**, so a template's `slots`
    and `triggers` are validated against exactly one closed vocabulary,
    never two that could silently drift apart. An unknown slot would
    otherwise only surface as `bind_slots`'s own defensive
    `AssertionError`, at selection time, far from the authoring mistake
    that caused it. (Pseudo-features are deliberately EXCLUDED here, unlike
    the trigger check -- `package_story_kind`/`package_angle`/`package_
    fact_count` are not real `Fact`s, so `bind_slots`' `package.text(slot)`
    read can never resolve one; nothing shipped or specified declares one
    as a slot.)"""
    for template in templates:
        valid_slots = _template_fact_vocabulary(template)
        for slot in template.slots:
            if slot not in valid_slots:
                raise ValueError(
                    f"media_phrases.json: template {template.id!r} declares unknown slot "
                    f"{slot!r} (valid: {sorted(valid_slots)})"
                )


def _iter_format_fields(text: str) -> Iterable[tuple[str, bool]]:
    """Yields `(field_name, is_positional)` for every replacement field
    `str.format` would actually resolve in `text` -- **Round 13 critic fix
    (nit 4), extending `string.Formatter().parse`'s single-level scan to
    match `.format()`'s own two blind spots the shipped version had:**

    1. A NESTED replacement field inside a format spec (`"{streak_length:
       {team_name}}"`) is returned by `Formatter().parse` as a raw
       substring of `format_spec` (`"{team_name}"`), never parsed itself --
       invisible to a single top-level scan, so `team_name` was never
       checked against `slots` even though `.format()` resolves it for
       real at render time. Fixed by recursing into any `format_spec` that
       itself contains `{`.
    2. An auto-numbered (`"{}"`, empty field name) or explicit positional
       (`"{0}"`) field is UNSUPPORTED here regardless of what `slots`
       declares -- `render_story` always calls `template.text.format(**
       slot_bindings)`, no positional arguments, so both shapes raise
       `IndexError` at render time no matter how `slots` is authored.
       `is_positional` flags both so the caller can reject them outright
       rather than silently checking them against `slots` (an empty field
       name is falsy and would otherwise vanish from a `if field_name`
       filter, the shipped version's actual gap)."""
    for _, field_name, format_spec, _ in string.Formatter().parse(text):
        if field_name is not None:
            head = field_name.split(".")[0].split("[")[0]
            yield field_name, (head == "" or head.isdigit())
        if format_spec and "{" in format_spec:
            yield from _iter_format_fields(format_spec)


def _validate_media_slot_placeholder_coverage(templates: select.MediaTemplateLibrary) -> None:
    """**Round 12 critic fix (blocking finding 4).** `_validate_media_slot_
    vocabulary` above only checks that every DECLARED slot name is
    recognized -- it never checks the reverse: that every `{placeholder}`
    actually appearing in `template.text` is covered by that template's own
    declared `slots`. `render_story` below does `template.text.format(**
    slot_bindings)`, so a placeholder present in `text` but missing from
    `slots` (a typo in either direction) raises `KeyError` at selection
    time, far from the authoring mistake that caused it -- reproduced by
    mutating one shipped template's `text` and confirming the `KeyError`.
    Uses `_iter_format_fields` above (built on `string.Formatter().parse`,
    stdlib, exactly what `str.format` itself uses to find field names)
    rather than a hand-rolled regex, so this check parses `text` the
    identical way `.format()` will. Matches Decision P3-18's own standing
    rule, extended to media (already cited by `_validate_media_trigger_
    vocabulary` above): "a typo that silently disables a template is
    exactly the defect class this project has paid for repeatedly."

    **Round 13 critic fix (nit 4).** The shipped version parsed only
    `string.Formatter().parse(template.text)`'s own top-level fields, and
    filtered on bare `if field_name` truthiness -- two gaps `_iter_format_
    fields` above closes, both reproduced and confirmed load-clean before
    this fix: an auto-numbered field (`"...{}'s locker room."`, `slots:
    []`) has `field_name == ""`, which `if field_name` treats as absent
    (falsy) rather than as a real, unsupported field, so it loaded clean
    and raised `IndexError` at `render_story`'s `.format()` call; a nested
    field inside a format spec (`"{streak_length:{team_name}}"`) is never
    visible to a single-level `.parse()` call at all, so a missing
    `team_name` slot loaded clean and raised `KeyError` at render time."""
    for template in templates:
        positional: set[str] = set()
        named: set[str] = set()
        for field_name, is_positional in _iter_format_fields(template.text):
            (positional if is_positional else named).add(field_name)
        if positional:
            raise ValueError(
                f"media_phrases.json: template {template.id!r} uses an auto-numbered or "
                f"positional placeholder ({sorted(positional)!r}) in its 'text' -- "
                "render_story's .format(**slot_bindings) call passes no positional "
                "arguments, so this raises IndexError at selection time regardless of "
                "'slots'"
            )
        missing = named - set(template.slots)
        if missing:
            raise ValueError(
                f"media_phrases.json: template {template.id!r} uses placeholder(s) "
                f"{sorted(missing)} in its 'text' but does not declare them in 'slots' "
                f"(declared: {sorted(template.slots)}) -- render_story's .format() call "
                "would raise KeyError at selection time"
            )


def _validate_media_positive_weights(templates: select.MediaTemplateLibrary) -> None:
    """Mirrors `phrase_service.py::_validate_positive_weights` exactly --
    every template's `weight` must be a positive (`> 0`), real number, or a
    zero/negative weight only surfaces as a confusing `rng.choices` failure
    at selection time."""
    for template in templates:
        if isinstance(template.weight, bool) or not isinstance(template.weight, (int, float)):
            raise ValueError(
                f"media_phrases.json: template {template.id!r} has a non-numeric weight "
                f"{template.weight!r}"
            )
        if template.weight <= 0:
            raise ValueError(
                f"media_phrases.json: template {template.id!r} has a non-positive weight "
                f"{template.weight!r} -- every template's weight must be > 0"
            )


def _validate_media_repeat_penalty(payload: dict, path: Path) -> float:
    """Mirrors `phrase_service.py::_validate_repeat_penalty` exactly,
    against media's own top-level key -- see that function's own docstring
    for the full reasoning (the upper bound is exclusive because `1.0`
    disables the penalty entirely, and this is media's OWN, independent
    value, never `gm_phrases.json`'s)."""
    if "repeat_penalty" not in payload:
        raise ValueError(f"{path}: missing required top-level key 'repeat_penalty'")
    repeat_penalty = payload["repeat_penalty"]
    if isinstance(repeat_penalty, bool) or not isinstance(repeat_penalty, (int, float)):
        raise ValueError(f"{path}: 'repeat_penalty' must be a number, got {repeat_penalty!r}")
    if not (0.0 <= repeat_penalty < 1.0):
        raise ValueError(
            f"{path}: 'repeat_penalty' ({repeat_penalty}) must be in [0.0, 1.0) -- a value "
            ">= 1.0 turns repeated template use into a bonus instead of a penalty, and a "
            "negative value makes the effective weight oscillate in sign"
        )
    return float(repeat_penalty)


def _validate_media_exclusivity_floor_value(payload: dict, path: Path) -> float:
    """Decision P4-21's second top-level knob -- unlike `phrase_service.py`
    's `exclusive_mass_share` (Decision P3-31 fixes an explicit `>= 0.25`
    authoring floor on the DECLARED number itself), no decision fixes an
    absolute minimum for media's own floor, so this validates only that a
    real number in `[0.0, 1.0]` is declared -- the REAL per-persona ratios
    are checked against it by `_validate_media_persona_exclusivity` below,
    which is where an actual authoring failure would surface."""
    if "persona_exclusivity_floor" not in payload:
        raise ValueError(f"{path}: missing required top-level key 'persona_exclusivity_floor'")
    floor = payload["persona_exclusivity_floor"]
    if isinstance(floor, bool) or not isinstance(floor, (int, float)):
        raise ValueError(f"{path}: 'persona_exclusivity_floor' must be a number, got {floor!r}")
    if not (0.0 <= floor <= 1.0):
        raise ValueError(
            f"{path}: 'persona_exclusivity_floor' ({floor}) must be in [0.0, 1.0]"
        )
    return float(floor)


def _selectable_weight_for_persona(
    templates: select.MediaTemplateLibrary,
    *,
    persona_key: str,
    relevant_kinds: set[str],
    exclusive_only: bool = False,
) -> float:
    """The real selectable weight for one persona over one kind-set --
    `select.py::_template_matches_story`'s own wildcard-or-membership
    semantics on the kind dimension, applied here rather than a cheaper
    intersection-only stand-in for it (Round 12 critic fix, blocking
    findings 2-3 -- see `_validate_media_persona_exclusivity`'s own
    docstring for the two under-counts this replaces). A template counts
    toward `persona_key`'s selectable weight iff a kind it could be drawn
    for (`t.kinds == ()` or `set(t.kinds) & relevant_kinds`) holds, AND
    (depending on `exclusive_only`) either:

    - `exclusive_only=False` (the denominator, every template `persona_key`
      could ever be handed): a voice it could be drawn under (`t.voices ==
      ()` or `persona_key in t.voices`) -- `select.py`'s own membership
      test, minus the frame-specific single-kind check (a validator
      reasons over a kind SET, a real selection over one frame's single
      kind).
    - `exclusive_only=True` (the numerator, this persona's OWN exclusive
      templates): `t.voices == (persona_key,)` exactly -- no other persona
      can ever be handed this template.

    **Round 13 critic fix (the one blocking bug round 12's own fix
    introduced).** `_validate_media_persona_exclusivity` used to compute
    `exclusive_weight` with a hand-rolled `t.voices == (persona.key,)` sum
    that applied NO kind filter at all, while the denominator (this
    function) applies one -- reopening the exact false-pass class this
    validator exists to catch. A template naming `voices == (persona.key,)`
    but `kinds` entirely outside that persona's own `relevant_kinds` is
    dead (unselectable by anyone -- `select_persona` never draws that
    persona for a kind it has no positive weight for) yet was still counted
    toward the numerator while excluded from the (correctly kind-filtered)
    denominator, letting one dead off-kind exclusive template inflate the
    ratio arbitrarily. Both sums now go through this ONE function with the
    same kind-filter expression, so they cannot drift apart again --
    `exclusive_only` switches only the voice test, never the kind test."""
    def _voice_matches(t: select.MediaTemplate) -> bool:
        if exclusive_only:
            return t.voices == (persona_key,)
        return not t.voices or persona_key in t.voices

    return sum(
        t.weight
        for t in templates
        if _voice_matches(t) and (not t.kinds or set(t.kinds) & relevant_kinds)
    )


def _validate_media_persona_exclusivity(
    templates: select.MediaTemplateLibrary,
    personas: tuple[select.MediaPersona, ...],
    floor: float,
) -> None:
    """This loader's own analogue of `phrase_service.py::_validate_
    exclusive_mass_share_by_philosophy` (Decision P3-31/§9.1's real
    per-philosophy rule) -- **deliberately scoped differently, stated
    explicitly rather than silently copied.** `phrase_service.py` computes
    its ratio GLOBALLY, over every template regardless of `situations`,
    because negotiation's own selection is single-stage (every philosophy
    can in principle encounter every situation/window). Media's selection
    is TWO-stage (Decision P4-26: persona drawn first, template drawn
    second) and a persona is only ever asked to select among templates for
    a kind it already has a positive preference for -- weighing a persona's
    exclusivity against fallback templates for kinds it can NEVER be drawn
    for would inflate every persona's ratio with a denominator it never
    actually competes in. So this function restricts BOTH sums to the
    persona's own `relevant_kinds` (`story_kind_weights` at a positive
    weight) before computing the ratio:

        exclusive_weight(P) = sum(t.weight for t in templates
                                   where t.voices == (P.key,) exactly
                                   and (t.kinds == () (wildcard) or
                                   set(t.kinds) & relevant_kinds))
        selectable_weight(P) = sum(t.weight for t in templates
                                    where (t.voices == () (wildcard)
                                    or P.key in t.voices)             (membership,
                                    matching `select.py::_template_matches_story`'s
                                    own voice semantics exactly -- a multi-voice
                                    template such as `voices: ["beat_writer",
                                    "insider"]` IS selectable by beat_writer even
                                    though it is not beat_writer-EXCLUSIVE)
                                    and (t.kinds == () (wildcard, selectable
                                    for every kind, matching `_validate_media_
                                    fallback_coverage`'s own wildcard-kinds
                                    acceptance and `select.py`'s own kind
                                    semantics) or set(t.kinds) & relevant_kinds))
        ratio(P) = exclusive_weight(P) / selectable_weight(P)

    Both sums share the identical kind-filter clause (`t.kinds == () or
    set(t.kinds) & relevant_kinds`) -- see the Round 13 note below for why
    that sharing is load-bearing, not cosmetic.

    Raises if `ratio(P) < floor` for any persona, or if `selectable_
    weight(P) == 0` (a persona with no selectable templates at all cannot
    clear any positive floor, and dividing by zero would otherwise mask
    that as a pass) -- the identical two failure shapes `_validate_
    exclusive_mass_share_by_philosophy` already guards.

    **Round 12 critic fix (blocking findings 2-3).** The denominator
    previously computed `exclusive_weight + sum(t.weight for t in templates
    if not t.voices and set(t.kinds) & relevant_kinds)` -- two separate
    under-counts, both silently excluding real selectable weight and
    therefore inflating `ratio` past what the library actually earns:

    1. **Tuple-equality on the exclusive side double-served as an
       incorrect stand-in for the selectable-side membership test**,
       exactly the defect `phrase_service.py:282-290`'s own docstring
       documents as already caught and fixed there for
       `_validate_exclusive_mass_share_by_philosophy` -- a template with
       `voices: ["beat_writer", "insider"]` is selectable by BOTH personas
       (`select.py::_template_matches_story`'s own membership check) but
       was counted in neither persona's old denominator. Verified: 20
       templates with all-five-persona voices and `weight=50.0` appended to
       the real shipped library load clean, while true selectable-share per
       persona is ~0.006-0.012 against the declared `0.5` floor.
    2. **`set(t.kinds) & relevant_kinds` alone is empty for a wildcard-kinds
       (`t.kinds == ()`) template**, excluding it from the denominator even
       though `_validate_media_fallback_coverage` explicitly accepts a
       wildcard-kinds fallback and `select.py` makes such a template
       selectable for every kind by every persona. Verified: a library
       whose only fallback is wildcard-kinds passes this floor at ratio
       `1.0` while real selectable-share is the same ~0.006-0.012.

    Both are fixed together (`_selectable_weight_for_persona` below) so the
    floor can once again catch an under-authored library -- the exact
    M26/M27-class failure this check exists for.

    **Round 13 critic fix (the one blocking bug round 12's own fix
    introduced).** After round 12, `exclusive_weight` was still computed by
    a hand-rolled `sum(t.weight for t in templates if t.voices ==
    (persona.key,))` -- no kind filter at all -- while `selectable_weight`
    (the denominator) had just gained one via `_selectable_weight_for_
    persona`. A template with `voices == (persona.key,)` but `kinds`
    entirely outside `relevant_kinds` is DEAD (`select_persona` never draws
    this persona for a kind it has no positive weight for, so the template
    can never be reached) yet still inflated the numerator while the
    now-correctly-filtered denominator excluded it -- reopening the exact
    "floor cannot catch an under-authored library" failure this validator
    exists to close. Verified (critic's repro): the shipped library plus
    one dead off-kind exclusive template (`{"kinds": ["award_race"],
    "voices": ["beat_writer"], "weight": 1000}`, `beat_writer` having no
    positive weight for `award_race`) loaded clean at ratio 56.222; the
    same 20-all-five-voice-weight-50 under-authored mutation round 12's own
    tests exercise (true selectable share 0.006-0.012 against a 0.5 floor)
    loaded clean again (ratio 9.8-9.9) once one dead off-kind exclusive
    template per persona was added. Fixed by routing BOTH sums through
    `_selectable_weight_for_persona`'s `exclusive_only` parameter -- the
    numerator and denominator now share one kind-filter expression in one
    function body, so they cannot silently diverge again the way a second,
    hand-rolled copy of the same filter already once let them."""
    failing: list[str] = []
    for persona in personas:
        relevant_kinds = {
            kind for kind, weight in persona.story_kind_weights.items() if weight > 0.0
        }
        exclusive_weight = _selectable_weight_for_persona(
            templates, persona_key=persona.key, relevant_kinds=relevant_kinds, exclusive_only=True
        )
        selectable_weight = _selectable_weight_for_persona(
            templates, persona_key=persona.key, relevant_kinds=relevant_kinds
        )
        if selectable_weight <= 0:
            failing.append(f"{persona.key} (no selectable templates)")
            continue
        ratio = exclusive_weight / selectable_weight
        if ratio < floor:
            failing.append(f"{persona.key} ({ratio:.3f})")
    if failing:
        raise ValueError(
            "media_phrases.json: persona-exclusivity floor requires each persona draw at "
            f"least {floor} of its selectable weight (both sums restricted to templates "
            "selectable for at least one of its own relevant kinds) from templates only it "
            "can select -- failing: " + ", ".join(sorted(failing))
        )


def load_media_library(path: Path = DEFAULT_MEDIA_PHRASE_LIBRARY_PATH) -> LoadedMediaLibrary:
    """Reads, parses, and validates `seeds/media_phrases.json`, raising
    `ValueError` on any violation -- Decision P3-18's own standing rule
    ("a typo that silently disables a template is exactly the defect class
    this project has paid for repeatedly"), extended to media.

    **Which of `phrase_service.py::load_library`'s eight checks this loader
    keeps, drops, or replaces, stated explicitly (this dispatch's own
    instruction).**

    1. Duplicate template ids -- KEPT (inline, below).
    2. Trigger vocabulary -- KEPT, against `StoryFrame`'s own fields
       (`_validate_media_trigger_vocabulary`).
    3. Fallback coverage -- KEPT, against `StoryKind` instead of
       `SituationToken`, `voices` instead of `philosophies`/`windows`
       (`_validate_media_fallback_coverage`).
    4. `_validate_open_never_triggers_on_stance` -- DROPPED. This check
       exists because `NegotiationFrame.stance` on an `open` frame carries
       information the proposer does not have yet at open time (see that
       function's own docstring). `StoryFrame` has no `stance`-shaped field
       and no `open`-shaped situation at all; there is no analogous trap
       here to guard against.
    5. `_validate_exclusive_mass_share` (the declared-number floor) --
       KEPT IN SPIRIT, replaced by `_validate_media_exclusivity_floor_value`
       -- same shape (required key, numeric, range-checked), but with no
       fixed `>= 0.25` minimum on the declared number (no decision sets one
       for media; see that function's own docstring).
    6. `_validate_exclusive_mass_share_by_philosophy` (the REAL
       per-philosophy check) -- KEPT, replaced by `_validate_media_
       persona_exclusivity` -- same two-failure-shape check, deliberately
       RE-SCOPED to each persona's own relevant kinds rather than computed
       globally (see that function's own docstring for why).
    7. Positive weights -- KEPT (`_validate_media_positive_weights`).
    8. Repeat penalty range -- KEPT (`_validate_media_repeat_penalty`).

    Two checks this loader ADDS that `phrase_service.py` has no
    analogue of, because media's vocabulary has structure negotiation's
    does not: persona-set closure (`_validate_media_persona_set`, Decision
    P4-53) and story-kind persona coverage (`_validate_media_every_kind_
    has_a_persona`, this loader's own added invariant -- see its own
    docstring). Plus vocabulary checks for the two dimensions this
    library's templates key on that `phrase_service.py` has no equivalent
    of at all: `kinds` (`_validate_media_kind_vocabulary`) and `voices`
    (`_validate_media_voice_vocabulary`), and the slot vocabulary check
    (`_validate_media_slot_vocabulary`) `phrase_service.py` never needed
    because Phase 3's `slot_bindings` was always `{}` (Decision P4-22's own
    "Phase 3 explicitly deferred this" framing).

    **Round 12 critic fix (blocking finding 4) adds a ninth check with no
    `phrase_service.py` analogue either**: `_validate_media_slot_
    placeholder_coverage` -- the REVERSE direction of check
    `_validate_media_slot_vocabulary` above, catching a `text` placeholder
    with no matching declared `slots` entry (that function's own docstring
    has the full defect shape).

    **docs/design/media-v2-newsroom.md, "Build order" step 5 adds three
    more, none with a `phrase_service.py` analogue**: `_validate_media_
    persona_metadata` (MV-3/MV-3h/MV-6's own per-persona voice fields --
    `column`/`beat_description`/`register`/`voice_rules`/`fences`, plus the
    optional `display_name` exactly two personas declare), `_validate_
    media_phase_cadence` (MV-3's own nine-phase cadence table), and
    `_validate_media_team_profile_quota` (MV-2h.3's flat reserved quota).
    All three are genuinely new library content this build step adds --
    negotiation's own `gm_phrases.json` has no cadence table, no persona
    voice, and no reserved-quota concept to mirror."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    personas = tuple(_parse_media_persona(row) for row in payload["personas"])
    templates = tuple(_parse_media_template(row) for row in payload["templates"])

    ids = [t.id for t in templates]
    if len(ids) != len(set(ids)):
        raise ValueError("media_phrases.json has a duplicate template id")

    _validate_media_persona_set(personas)
    _validate_media_persona_kind_vocabulary(personas)
    _validate_media_every_kind_has_a_persona(personas)
    _validate_media_kind_vocabulary(templates)
    _validate_media_voice_vocabulary(templates, personas)
    _validate_media_trigger_vocabulary(templates)
    _validate_media_fallback_coverage(templates)
    _validate_media_slot_vocabulary(templates)
    _validate_media_slot_placeholder_coverage(templates)
    _validate_media_positive_weights(templates)
    repeat_penalty = _validate_media_repeat_penalty(payload, path)
    persona_exclusivity_floor = _validate_media_exclusivity_floor_value(payload, path)
    _validate_media_persona_exclusivity(templates, personas, persona_exclusivity_floor)
    _validate_media_persona_metadata(payload["personas"])
    persona_metadata = {row["key"]: _parse_persona_metadata(row) for row in payload["personas"]}
    _validate_media_phase_cadence(payload)
    phase_cadence = _parse_phase_cadence(payload)
    team_profile_quota = _validate_media_team_profile_quota(payload)

    return LoadedMediaLibrary(
        templates=templates,
        personas=personas,
        repeat_penalty=repeat_penalty,
        persona_exclusivity_floor=persona_exclusivity_floor,
        fallback_by_kind=_fallback_templates_by_kind(templates),
        persona_metadata=persona_metadata,
        phase_cadence=phase_cadence,
        team_profile_quota=team_profile_quota,
    )


def with_voices_disabled(library: LoadedMediaLibrary) -> LoadedMediaLibrary:
    """Decision P4-97's control transformation, specified in §30.6 and
    built here for the first time (Decision P4-95's 20a is its first real
    caller): forces every template's own `voices` tuple to `()` (wildcard),
    exactly as if the `voices` dimension did not exist in the library at
    all -- every persona becomes able to draw every template, on identical
    footing. Modeled line for line on `tools/validate_gm.py::with_
    philosophies_disabled` (§30.6: "`with_philosophies_disabled` is the
    model to copy line for line"), with `voices` standing in for
    `philosophies`.

    **One real hazard, named because it is the whole point of this
    function and the reason it is copied rather than reinvented:** it must
    touch `voices` and NOTHING else -- not `kinds`, not `triggers`, not
    `weight` -- or the paired difference `tv_live - tv_control` stops
    isolating the allow-list channel alone (§30.6). `dataclasses.replace`
    on exactly one field, mirroring the original's own single-field
    replace, is what keeps that true by construction rather than by
    discipline.

    A pure, MEASUREMENT-ONLY transformation -- never reachable from any
    production caller (nothing under `dba2/` calls it; `tools/generate_
    media_corpus.py` is its only caller). Built in-memory from an ALREADY-
    LOADED, already-validated real library, never routed back through
    `load_media_library`'s own file-loading path -- re-validating it there
    would fail `_validate_media_persona_exclusivity`'s own floor by
    construction (every exclusive template just became a wildcard), which
    is the whole point of this transformation, not a defect in it (the
    identical reasoning `with_philosophies_disabled`'s own docstring
    states for `exclusive_mass_share`).

    **`fallback_by_kind` is carried over by identity, never re-derived from
    the transformed library.** An earlier version of this function called
    `_fallback_templates_by_kind(new_templates)` here, on the theory that
    every fallback already has `voices == ()` (Decision P4-25's own
    requirement) so re-deriving would just find the same templates again.
    That reasoning misses the actual hazard: `_fallback_templates_by_kind`
    picks the FIRST no-trigger, wildcard-voice template it finds per kind,
    and stripping every OTHER template's `voices` too makes every no-
    trigger dedicated template look like a wildcard-voice candidate for
    that same first-match-wins scan. For six of eleven kinds (`award_race`,
    `holdout_watch`, `milestone`, `rookie_watch`, `signing_reaction`,
    `trade_fallout`) that re-derivation silently resolved a template OTHER
    than the kind's real `fallback_<kind>` -- and re-running
    `_validate_media_fallback_coverage` against the transformed library
    actually raises `ValueError` ("more than one fallback for: ..."),
    proving the transformed library violates the exactly-one-fallback-per-
    kind invariant the real library satisfies. Confirmed harmless in every
    corpus run to date only because voices are wildcarded everywhere in
    this arm, so the real fallback is always still in the eligible-and-
    triggered survivor set and the wrong map entry is never actually
    consulted -- but a future dedicated no-trigger template with a non-
    REQUIRED slot could get silently installed as a kind's fallback here,
    reopening the exact `bind_slots` `AssertionError` MV-1c.2's fallback-
    lookup mechanism exists to prevent. The fix: keep the REAL, pre-
    transformation `fallback_by_kind` mapping (kind -> which template ID is
    that kind's fallback) and only re-point each entry at that SAME
    template's already-transformed object in `new_templates` -- required
    because `select_media_template`'s lookup is by dict value, not by
    re-deriving from `templates`, so a stale (pre-transformation) object
    would still render correctly but would defeat the whole point of one
    map being derived from the other."""
    new_templates = tuple(
        dataclasses.replace(template, voices=()) for template in library.templates
    )
    new_templates_by_id = {template.id: template for template in new_templates}
    return dataclasses.replace(
        library,
        templates=new_templates,
        fallback_by_kind={
            kind: new_templates_by_id[fallback.id]
            for kind, fallback in library.fallback_by_kind.items()
        },
    )


# ---------------------------------------------------------------------------
# Stage D step 18 -- real DB-backed selection: persona draw, template draw,
# real slot-binding resolution (Decisions P4-22/P4-26/P4-38/P4-54).
# ---------------------------------------------------------------------------


def _subject_identity(subject: Subject) -> tuple[str, int]:
    """Decision P4-54's `(subject_kind, subject_id)` salt components --
    **retyped from `(frame)` to `(subject)`** (MV-8, revision 5: "a caller
    that moves, not a caller that disappears"). **Player preferred over
    team when a subject carries both** (only `signing_reaction`'s ungated
    shape ever does) -- unchanged rule, unchanged tuple, for `PLAYER`/
    `TEAM` subjects: `("player", id)`/`("team", id)`, byte-identical to
    what this function returned for the equivalent `StoryFrame` (MV-8's own
    "for PLAYER and TEAM packages the returned tuple is byte-identical to
    today's" claim, which is what keeps this build step's RNG streams
    unchanged for every current `StoryKind`). **Gains a `MATCHUP` branch**
    returning `("matchup", team_id)` -- a distinct `subject_kind` string, so
    a future `MATCHUP` package can never collide on this salt with a `TEAM`
    package for the same team; unreachable in this build step (no current
    kind produces a `MATCHUP` subject), included now because `Subject.kind`
    already carries the member and a future detector should not need this
    function edited again to use it. Raises `AssertionError` if a subject
    carries NEITHER id -- `seal_package`'s own `REQUIRED_FACT_KEYS_BY_KIND`
    check should make a sealed package with neither unreachable."""
    if subject.kind is SubjectKind.MATCHUP:
        if subject.team_id is None:
            raise AssertionError(
                "a MATCHUP subject with no team_id reached selection -- seal_package's own "
                "REQUIRED-key check should make this unreachable"
            )
        return "matchup", subject.team_id
    if subject.player_id is not None:
        return "player", subject.player_id
    if subject.team_id is not None:
        return "team", subject.team_id
    raise AssertionError(
        f"Subject (kind={subject.kind.value}) has neither player_id nor team_id -- "
        "seal_package's own REQUIRED-key check should make this unreachable for a "
        "package that reached selection"
    )


def _cycles_since_last_seen(
    subject_last_seen: Mapping[tuple[str, int], int], *, subject: Subject, cycle_index: int
) -> int | None:
    """docs/design/media-v2-newsroom.md MV-1c.4/MV-10 -- `run_media_
    selection_cycle`'s own `subject_last_seen` parameter turned into a real
    per-candidate value, reusing `_subject_identity`'s own `(subject_kind,
    subject_id)` key (the same tuple the RNG salt already keys on, so no
    third key shape is invented for the identical subject concept). `None`
    (the subject has never been seen, by this caller's own map) preserves
    `salience._novelty`'s own existing "absent is fully novel" reading --
    never a fabricated `0`. A caller-supplied `last_seen` cycle AFTER this
    one (a malformed map) still returns a real, non-negative int rather
    than a negative distance a caller never intended `_novelty` to read."""
    last_seen = subject_last_seen.get(_subject_identity(subject))
    if last_seen is None:
        return None
    return max(cycle_index - last_seen, 0)


async def load_subject_last_seen(
    ex: Executor, *, league_id: int, season: int
) -> dict[tuple[str, int], int]:
    """MV-10's "computed from `media_stories` by subject" -- the one real
    production caller of `media_repo.get_subject_last_seen_cycles`, doing
    the `(subject_kind, subject_id)`-collapsing this repo function cannot
    do itself (Decision P4-31's layering direction). Mirrors `_subject_
    identity`'s own "player preferred over team" rule: a row naming both
    (never produced by a real `Subject`, since `identity_facts`' own
    construction only ever sets one of the two non-opponent ids, but
    handled the same way regardless) collapses to `("player", id)`; a row
    naming neither (a `MATCHUP`-subject story, or a since-purged subject,
    `ON DELETE SET NULL`) contributes nothing -- an untrackable subject is
    not a `0`-cycle-old one, it is simply absent from the map, which
    `_cycles_since_last_seen` already reads as "fully novel." A subject
    whose rows collapse to the SAME key from more than one raw `(player_id,
    team_id)` pair (a traded player's own `team_id` changing across the
    season) takes the MAX of every pair's own `last_cycle` -- the true most
    recent cycle that subject was written about, under whichever key its
    identity collapses to, not merely one pair's own reading of it."""
    rows = await media_repo.get_subject_last_seen_cycles(ex, league_id=league_id, season=season)
    result: dict[tuple[str, int], int] = {}
    for player_id, team_id, last_cycle in rows:
        if player_id is not None:
            key = ("player", player_id)
        elif team_id is not None:
            key = ("team", team_id)
        else:
            continue
        result[key] = max(result.get(key, last_cycle), last_cycle)
    return result


async def select_and_bind_story(
    ex: Executor,
    *,
    league_id: int,
    league_rng_seed: int,
    season: int,
    phase: Phase,
    cycle_index: int,
    scored: salience.ScoredCandidate,
    library: LoadedMediaLibrary,
    use_counts: dict[str, dict[str, int]],
    candidate_sequence: int,
    eligible_personas: frozenset[str] | None = None,
    persona_multipliers: Mapping[str, float] | None = None,
    brief_inputs: media_context.CycleBriefInputs | None = None,
) -> MediaStory:
    """One scored candidate's full Stage D steps 18-19 pipeline: draw a
    persona, draw a template, resolve real slot values, lazily get-or-
    create that story's named writer, and PERSIST the result -- the first
    real production caller of this function (Stage D step 19). `use_counts`
    is caller-supplied here, the identical "managers/ stays stateless, the
    caller owns history" shape `select_phrase`'s own `use_counts` parameter
    already has.

    **Build step 7: `use_counts` is now `dict`, not `Mapping`, and IS
    mutated in place.** Steps 18-19 through step 6 read it (`.get(persona.
    key, {})`) without ever writing through it -- the caller (`run_media_
    selection_cycle`) did its own separate `running_use_counts.setdefault
    (...)`/increment after this function returned. Now that this function
    constructs a real `dba2.media.write.template.TemplateWriter` and hands
    it `use_counts` BY REFERENCE (never a copy), that writer's own `write()`
    advances the SAME mapping the caller holds -- the identical value, one
    mechanism, not two. A caller that still expects to do its own increment
    after this call returns would double-count; `run_media_selection_
    cycle`'s own post-call increment was removed for exactly this reason.

    **Round-15-critic fix (the one remaining Round-14 blocking finding):
    `use_counts` is now `{voice: {template_id: prior use count}}`, scoped
    per `(voice, cycle)`, not a single flat `{template_id: count}` pool
    shared by every persona in the cycle.** This is Decision P4-29's own
    already-specified scope ("First draft: template `use_counts` scoped
    per `(voice, cycle)`", design doc §7) -- round 14 shipped the flat
    shape instead and its own docstring/design-doc text (§28) falsely
    claimed the flat shape was what P4-29 already required; it was not.
    The real, measured consequence of the flat shape: two personas
    competing for the same small template pool in one cycle (most kinds
    have exactly one voice-wildcard fallback template alongside each
    persona's own dedicated one) could suppress each other's repeat
    penalty across a voice boundary the design doc's own `(voice, cycle)`
    wording exists specifically to prevent -- e.g. two `analyst`-voiced
    picks of a shared fallback template silently starving a later
    `historian`-voiced candidate's own draw of that same fallback, purely
    because they happen to share a template id, never because the
    `historian` voice itself repeated anything. `voice` here is `persona.
    key`, the same persona `select_persona` already drew for this
    candidate a few lines below -- the only voice a template draw is ever
    scored against (Decision P4-54's own `..., voice)` salt component
    already keys the template RNG stream the identical way).

    **Stage D step 19 change: this function now returns the persisted
    `dba2.domain.media.MediaStory` row, not a bare `select.StorySelection`,
    and takes a new required `league_id`.** Both changes exist for the
    same reason: a selection with nowhere to go was step 18's own disclosed
    scope; this step's whole job is giving it somewhere real to go
    (`media_repo.create_media_story`), and persisting a story needs to know
    which league it belongs to. `MediaStory` carries the identical `voice`/
    `template_id`/`slot_bindings` attribute names `select.StorySelection`
    had, so `render_story` below (which only ever reads those three
    attributes) still accepts a `MediaStory` unchanged -- existing callers
    that pass this function's return value straight into `render_story`
    keep working with no change to that call.

    **`candidate_sequence` -- Round 12 critic fix (blocking finding 1),
    now formally Decision P4-88 (AMENDS P4-54).** `dba2/media/events.py::
    trade_execution_candidate` is per `(team, trade)`, so a team executing
    two trades in the same trade window yields two `TRADE_REACTION`
    candidates sharing an IDENTICAL `(frame.kind, subject_kind,
    subject_id)` -- and since `StoryFrame` carries only trade-asset
    COUNTS, never the underlying player/pick ids (Decision P4-24's already-
    closed field list), the two candidates' frames can be byte-identical
    too. Before this fix, both salts below ended at that same tuple with
    no further discriminant and collided: verified, both candidates drew
    byte-identical `voice` AND `template_id` on every seed -- exactly the
    failure class P4-23/P4-54 already name from Phase 3's own seven-tuple
    self-collision ("a media cycle selects many stories in one tick").
    `candidate_sequence` is this candidate's zero-based position within the
    SAME stably-ordered sequence of candidates its caller derived for this
    cycle (e.g. the tuple `salience.select_top_k` returns, whose own
    ORIGINAL-INPUT-ORDER tie-break is already a real, reproducible property
    of that list -- see that function's own docstring) -- a property of the
    candidate LIST itself, never of the order this function happens to be
    awaited in (a caller that processes the list out of order, or
    concurrently, must still pass each candidate's own fixed position, not
    a call counter). Required, not defaulted, so a caller cannot silently
    reintroduce the collision by omitting it.

    **The two RNG streams, both on Decision P4-23's `"media"` salt, per
    Decision P4-54's own two-draw-kinds rule -- extended here with a THIRD
    salt label, Decision P4-88.** P4-54 names exactly the template-selection
    salt (`"story"`, ending in the already-drawn `voice`) and the writer-
    NAME salt (`"writer_name"`, step 19's concern). It does not separately
    name the PERSONA draw's own salt, because `voice` -- the very value
    that salt would need to exclude -- does not exist yet at that point;
    extending the `"story"` label to a shorter, `voice`-less tuple would
    leave one label covering two different arities of draw, which is the
    exact kind of ambiguity Decision P4-54's own closing "general rule" (a
    draw's salt must be exactly the tuple that identifies THAT draw) argues
    against. A dedicated `"persona"` label removes that ambiguity outright,
    and Decision P4-88 records it (this dispatch's prior draft called it
    "this dispatch's own judgment call" -- P4-54's own general rule says
    this would "come up a third time" and requires a numbered decision, not
    a docstring aside):

    - **Persona draw:** `rng_for(seed, "media", "persona", season, phase,
      cycle_index, story_kind, subject_kind, subject_id, candidate_
      sequence)` -- P4-23's own collision fix (subject + kind, so two
      stories drawn in the same cycle never share a stream) plus P4-88's
      own `candidate_sequence` (so two same-subject-and-kind candidates in
      one cycle never share a stream either), minus `voice` (unknown at
      this point).
    - **Template draw:** `rng_for(seed, "media", "story", season, phase,
      cycle_index, story_kind, subject_kind, subject_id, candidate_
      sequence, voice)` -- Decision P4-54's own tuple, extended by the same
      `candidate_sequence` component for the identical reason.

    **docs/design/media-v2-newsroom.md, "Build order" step 3a: names are
    already resolved, at SEAL time, before this function ever runs.**
    `StoryPackage.subject` already carries `team_name`/`player_name`
    (`seal_package`, called from `run_media_pulse_cycle` before scoring) --
    this function no longer does its own `team_repo.get_team`/`player_repo.
    get_player` lookup FOR NAMES. It still does the identical two lookups
    for a SECOND, separate reason that does not move: confirming the
    subject ROW still exists RIGHT NOW, immediately before an `INSERT` that
    would otherwise raise a real `ForeignKeyViolationError` against an id
    that existed at seal time but has since been deleted (MV-8, revision 5:
    "callers that move, not callers that disappear"). **One disclosed
    behavior change falls out of the names moving earlier, not out of this
    function's own logic**: a team deleted BETWEEN sealing and persistence
    now renders its REAL name (sealed while the row still existed) with a
    `NULL subject_team_id` and a `NULL writer_id`, where the pre-retype
    code rendered the placeholder `"the team"` in that same race -- the
    guarantee (never raise on a since-deleted subject) is unchanged; its
    exact rendered text is not.

    **`eligible_personas`/`persona_multipliers` (MV-3h, "Build order" step
    6) -- both pass straight through to `select.select_persona`, unchanged
    by this function.** The CALLER (`run_media_selection_cycle`) is the one
    that knows which of the three publication-plan groups this candidate
    came from (main/band/profile) and therefore which restriction, if any,
    applies, and it is the one that enforces MV-1c.3's own cap-never-waived
    rule BEFORE ever calling this function -- by the time this function
    runs, `eligible_personas` (when given) is already the real, cap-
    reduced eligible set for this exact draw, so `select.select_persona`'s
    own empty-set `AssertionError` should be unreachable here. Both default
    to `None` (today's pre-cadence behavior) so every existing caller/test
    keeps working unchanged.

    **`brief_inputs` (docs/design/media-v2-newsroom.md MV-12, "Build
    order" step 7.5b) -- the cycle-scoped `CycleBriefInputs` `run_media_
    selection_cycle` built once, or `None`.** This function is where the
    per-story `LeagueBrief` is actually assembled (`dba2.media.context.
    build_league_brief`, called AFTER the persona draw below, since MV-13's
    own "on beat" ranking needs the drawn persona's `story_kind_weights`)
    and where it is handed to the writer (`WriterBackend.write`'s own
    `brief` keyword, MV-12.3) and persisted (`media_stories.league_brief`,
    migration `025`). `None` propagates to `brief=None` at the write call
    and `league_brief={}` at persistence -- both defaults, so a caller that
    never passes `brief_inputs` sees no behavior change at all."""
    package = scored.candidate.package
    subject_kind, subject_id = _subject_identity(package.subject)

    persona_rng = rng_for(
        league_rng_seed, "media", "persona", season, phase, cycle_index,
        package.story_kind, subject_kind, subject_id, candidate_sequence,
    )
    persona = select.select_persona(
        package.story_kind, library.personas, rng=persona_rng,
        persona_multipliers=persona_multipliers, eligible_personas=eligible_personas,
    )
    display_name = None
    metadata = library.persona_metadata.get(persona.key)
    if metadata is not None:
        display_name = metadata.display_name

    template_rng = rng_for(
        league_rng_seed, "media", "story", season, phase, cycle_index,
        package.story_kind, subject_kind, subject_id, candidate_sequence, persona.key,
    )
    # MV-4's own deterministic tier rule (build step 7 -- steps 5/6 drew
    # every candidate at a hardcoded `FormatTier.BLURB`, disclosed as a
    # stand-in in both of those steps' own As-built notes). `None` is only
    # possible for a `TEAM_PROFILE` package below `TEAM_PROFILE_MIN_FACTS`
    # (MV-4's "discarded, not degraded to BLURB" rule) -- `load_team_
    # profile_packages`'s own `profile.MIN_FACTS_TO_PUBLISH` filter already
    # keeps such a package from ever reaching this function, so this is a
    # defensive, not a documented, production path (mirroring `render_
    # story`'s own `KeyError` docstring one function below).
    tier = select_tier(scored.score, len(package.facts), story_kind=package.story_kind)
    if tier is None:
        raise ValueError(
            f"select_and_bind_story: a {package.story_kind.value!r} TEAM_PROFILE package "
            "reached selection below MV-4's own publish floor -- load_team_profile_"
            "packages should have discarded it before scoring"
        )

    # **Persist only ids CONFIRMED to exist at this exact moment, never a
    # bare `package.subject.team_id`/`player_id` passed straight through.**
    # `media_stories.subject_team_id`/`subject_player_id` and `media_
    # writers.team_id` are all real foreign keys -- migration 011's `ON
    # DELETE SET NULL` on the story's two subject columns is an ON-DELETE
    # action (it nulls out an EXISTING row's reference once its subject is
    # later removed); it has no bearing on a brand-new INSERT that names an
    # id already gone by the time this function runs (the adversarial race
    # `tests/integration/test_media_service.py::test_select_and_bind_story_
    # degrades_gracefully_when_a_team_was_deleted` exercises directly) --
    # Postgres has no "insert with SET NULL" behavior, only "insert with
    # SET NULL ON A LATER DELETE," so a stale id there is a real, immediate
    # `ForeignKeyViolationError`, not a degraded story.
    team = None
    if package.subject.team_id is not None:
        team = await team_repo.get_team(ex, package.subject.team_id)
    subject_team_id = team.id if team is not None else None

    player = None
    if package.subject.player_id is not None:
        player = await player_repo.get_player(ex, package.subject.player_id)
    subject_player_id = player.id if player is not None else None

    # Decision P4-38: `beat_writer` is the one team-scoped archetype (also
    # the ONLY archetype migration 011's own `CHECK` constraint requires a
    # `team_id` for) -- every kind the shipped library gives `beat_writer` a
    # positive `story_kind_weights` entry for is itself team-scoped
    # (`signing_reaction`/`standings_surge`/`standings_collapse`/`streak`/
    # `trade_fallout`/`trade_reaction`, verified against `seeds/media_
    # phrases.json` directly, not assumed), so `package.subject.team_id` is
    # present, and ordinarily still a real row, whenever `select_persona`
    # actually draws `beat_writer` for a real package. **When the team has
    # since been deleted (`team is None` above), no valid team-scoped writer
    # can be resolved at all** -- rather than raising (the CHECK constraint
    # would reject a `team_id=NULL` beat_writer row outright) or fabricating
    # a dangling reference (the same FK problem `subject_team_id` above just
    # avoided), this story is persisted UNATTRIBUTED (`writer_id=None`) --
    # `media_stories.writer_id`'s own nullability already anticipates
    # exactly this case ("a story bylined by a since-deleted writer row is
    # still league history").
    writer_id: int | None = None
    writer: MediaWriter | None = None
    if persona.key != "beat_writer":
        writer = await get_or_create_media_writer(
            ex, league_rng_seed=league_rng_seed, league_id=league_id, team_id=None,
            persona=persona.key, display_name=display_name,
        )
        writer_id = writer.id
    elif team is not None:
        writer = await get_or_create_media_writer(
            ex, league_rng_seed=league_rng_seed, league_id=league_id, team_id=team.id,
            persona=persona.key, display_name=display_name,
        )
        writer_id = writer.id

    # **Build step 7: the actual write, through the `WriterBackend` seam
    # (MV-6), replacing the old direct `select.select_media_template`/
    # `bind_slots`/`render_story` inline call this function used through
    # step 6.** `metadata` is real for every persona once a library has
    # passed `_validate_media_persona_metadata` at load (every real caller
    # here uses `load_media_library()`) -- a `None` metadata is a hand-built
    # test fixture predating this wiring, which this function has no honest
    # voice to render with, so it fails loudly rather than fabricating one.
    if metadata is None:
        raise ValueError(
            f"select_and_bind_story: no persona metadata for {persona.key!r} -- "
            "library.persona_metadata must carry every persona once a real write "
            "is attempted (MV-3/MV-6)"
        )
    # The one persona-voice field `resolve_persona_voice` cannot resolve
    # itself (MV-3's own "generated once, stable for the league's life"
    # name): `writer.name` when a writer was actually created, or `persona.
    # key` when it was not (the one case that can leave `writer` `None` --
    # `beat_writer` with a since-deleted team, directly above). Provably
    # never read in that case: `beat_writer`'s own library metadata always
    # declares a fixed `display_name` ("Jordan Ellis"), and `resolve_
    # persona_voice` only ever falls back to `generated_name` when the
    # metadata's `display_name` is `None` -- true for the other three
    # personas, never for `beat_writer`.
    generated_name = writer.name if writer is not None else persona.key
    persona_voice = resolve_persona_voice(metadata, persona.key, generated_name=generated_name)

    # `use_counts` is the SAME mutable mapping the caller (`run_media_
    # selection_cycle`'s own `running_use_counts`) keeps across every
    # candidate in this cycle -- `TemplateWriter` mutates it in place inside
    # `write()` (its own docstring), so this candidate's draw is visible to
    # the next one without a second, parallel counting mechanism. A fresh
    # `TemplateWriter` per call is deliberate and cheap (it is a thin
    # wrapper holding only `library` and a reference to `use_counts`, never
    # its own copy) -- what makes the repeat-penalty scope real is the
    # SHARED mapping, not object identity of the writer.
    # docs/design/media-v2-newsroom.md MV-12/MV-13, "Build order" step
    # 7.5b: the one per-story `LeagueBrief` assembly call, AFTER the
    # persona draw above (MV-13 point 2 needs the drawn persona's own
    # `story_kind_weights` to rank "on beat" entries ahead of off-beat
    # ones within a link class) and BEFORE the write below (MV-12.3's
    # seam: an additive, defaulted `brief` keyword on `WriterBackend.
    # write`). `None` when `brief_inputs` is `None` (every existing
    # caller/test, and any cycle `run_media_selection_cycle` had no real
    # `current_snapshot`/`team_names` for) -- `write()` then receives
    # `brief=None`, byte-identical to pre-7.5b behavior.
    brief = None
    if brief_inputs is not None:
        brief = media_context.build_league_brief(
            brief_inputs, package=package, persona_story_kind_weights=persona.story_kind_weights,
        )

    writer_backend = TemplateWriter(library, use_counts=use_counts)
    written = await writer_backend.write(
        package, persona=persona_voice, tier=tier, rng=template_rng, brief=brief,
    )
    assert writer_backend.last_template_id is not None, (
        "TemplateWriter.write() must always record the template it drew"
    )

    return await media_repo.create_media_story(
        ex,
        league_id=league_id,
        season=season,
        phase=phase,
        cycle_index=cycle_index,
        story_kind=package.story_kind,
        voice=persona.key,
        template_id=writer_backend.last_template_id,
        slot_bindings=writer_backend.last_bindings,
        rendered_text=written.body,
        salience=scored.score,
        subject_player_id=subject_player_id,
        subject_team_id=subject_team_id,
        writer_id=writer_id,
        headline=written.headline,
        format_tier=tier.value,
        fact_package=package.to_prompt_json(),
        writer_backend=written.backend,
        league_brief=brief.to_prompt_json() if brief is not None else None,
    )


def render_story(selection: select.StorySelection, library: LoadedMediaLibrary) -> str:
    """Decision P4-22's interpolation step, mirroring `phrase_service.py::
    render`'s own shape exactly (`template.text.format(**slot_bindings)`)
    -- pure string formatting, no DB read, since `selection.slot_bindings`
    already carries fully-resolved strings by the time this is called.
    Raises `KeyError` if `selection.template_id` does not name a template
    in `library` -- not a documented production path, since every real
    `StorySelection` originates from `select_and_bind_story` choosing a
    template out of this same library.

    **Round 12 critic fix (nit 2): the first character of the rendered
    result is upper-cased.** Several shipped templates open with
    `{team_name}` (`fallback_standings_surge`: `"{team_name} are trending
    up..."`), so a lowercase-first-letter render would fragment the
    sentence -- the identical `team_name`/`player_name` slots also appear
    MID-sentence in other shipped templates (`insider_trade_reaction_
    picks`: `"Sources say {team_name} view..."`), where a forced capital on
    the SLOT VALUE would be the real error. Capitalizing only the first
    character of the fully-rendered sentence, here, fixes the sentence-
    initial case unconditionally and cannot affect a mid-sentence
    occurrence, because it touches exactly one position in the final
    string regardless of which slot (or literal template text) happens to
    occupy it. **This build step's own retype removes the historical
    reason this fix mattered most** (`select.py`'s old degrade-to-
    placeholder resolvers, `"the team"`/`"the player"`, deleted along with
    `_SLOT_RESOLVERS` -- MV-1b(c): an absent name now makes a template
    INELIGIBLE rather than rendering a placeholder), but the fix itself
    stays: a real, sealed name can still be lowercase-first (nothing
    capitalizes a city/team/player name for this purpose), so the same
    sentence-initial fragment risk exists for real values too."""
    by_id = {t.id: t for t in library.templates}
    template = by_id[selection.template_id]
    rendered = template.text.format(**selection.slot_bindings)
    return rendered[:1].upper() + rendered[1:]


# ---------------------------------------------------------------------------
# Stage D step 19 -- named-writer identity (Decision P4-38, migration 011's
# `media_writers` table), and the real per-cycle selection+persistence loop
# (Decision P4-88's `candidate_sequence` contract).
# ---------------------------------------------------------------------------

_WRITER_FIRST_NAMES = (
    "Alex", "Jordan", "Casey", "Morgan", "Taylor", "Riley", "Sam", "Jamie",
    "Drew", "Quinn", "Avery", "Reese", "Harper", "Cameron", "Blake", "Skyler",
    "Rowan", "Elliot", "Marlowe", "Finley",
)
_WRITER_LAST_NAMES = (
    "Bishop", "Cole", "Delgado", "Ellison", "Foster", "Grant", "Hayes",
    "Ibarra", "Keating", "Lindqvist", "Marsh", "Nakamura", "Okafor", "Pryce",
    "Quintero", "Rourke", "Stavros", "Tanaka", "Vance", "Whitfield",
)
"""Disclosed, not derived -- the same status as `dba2/media/frame.py::_MIN_
TEAM_GAMES_FOR_SAMPLE`/`salience.py`'s scoring weights: a small, closed pool
authored for this step, not sourced from `tools/_gm_draft_class_stub.py`
(its own placeholder shape is literally `"Prospect" "001"`, not a real name
pool -- reusing it would mean persisting placeholder-looking bylines) or
from `tools/seed_dev_league.py`'s own `_FIRST_NAMES`/`_LAST_NAMES` (a real
pool, but it lives under `tools/`, and `dba2/services/` importing `tools/`
would invert this project's own layering direction -- `tools/` is the one
package allowed to import `dba2.services`, never the reverse, per `tests/
unit/test_layering.py`'s "only bot and tools may import services from
outside" rule). Decision P4-38's own text anticipates exactly this choice:
"reuse ... if a sibling already has one; a small new generator otherwise."
20x20 = 400 combinations per league -- comfortably above the five-archetype
ceiling any one league ever draws from (one `beat_writer` per team, plus
one of each of the other four archetypes, league-wide), so a same-league
name collision across two different writers is possible but cosmetic, not
a correctness concern (`media_writers`' own uniqueness is on `(league_id,
team_id, persona)`, never on `name`)."""


def _generate_writer_name(
    league_rng_seed: int, *, league_id: int, team_id: int | None, persona: str
) -> str:
    """Decision P4-54's own writer-name salt, verbatim: `rng_for(seed,
    "media", "writer_name", league_id, persona, team_id_or_0)` -- **no
    season, no phase, no cycle_index, and no `candidate_sequence`**,
    deliberately: Decision P4-38 requires this name to be "generated once,
    stable for the league's life," and any time-varying (or per-candidate)
    salt component would make a regenerated name differ from the one
    already persisted, turning a display-continuity feature into a silent
    inconsistency the moment two different cycles' calls computed two
    different names for what `get_or_create_media_writer` below treats as
    the SAME writer row. `team_id if team_id is not None else 0` -- P4-54's
    own `team_id_or_0` component: `None` and `0` stringify differently, and
    a salt tuple built from an `Optional` would otherwise be a salt tuple
    with two spellings for the same league-wide case."""
    rng = rng_for(
        league_rng_seed, "media", "writer_name", league_id, persona,
        team_id if team_id is not None else 0,
    )
    return f"{rng.choice(_WRITER_FIRST_NAMES)} {rng.choice(_WRITER_LAST_NAMES)}"


async def mark_wire_story_posted(ex: Executor, story_id: int) -> bool:
    """The one service-level writer the bot calls to record that a story's
    `#league-wire` message went out (phase-5-stage-e-advance.md P5-99.s.1,
    P5-99.m.1): the mark lives on the STORY (`media_stories.posted_at`), never on the
    `leagues` row, so the cog needs no `leagues` write and the press needs no callback
    into Discord. Fenced -- `True` only for the call that moved the column; a repeat
    (a startup sweep racing a press) is `False` and keeps the first timestamp.
    `dba2.bot` reaches it and never the repository, because `data/repositories/` is a
    layer the bot does not import (the layering rule
    `test_nothing_under_dba2_except_bot_imports_services` pins the other direction)."""
    return await media_repo.mark_media_story_posted(ex, story_id)


async def get_or_create_media_writer(
    ex: Executor,
    *,
    league_rng_seed: int,
    league_id: int,
    team_id: int | None,
    persona: str,
    display_name: str | None = None,
) -> MediaWriter:
    """Lazy get-or-create for one `(league_id, team_id, persona)` writer
    identity (Decision P4-38) -- the read is a plain `media_repo.get_media_
    writer` lookup; a miss generates a name (`_generate_writer_name` above)
    and inserts. **The race window between the read and the insert is real
    and is handled, not assumed away**: two concurrent selections for the
    same persona/team in the same league (plausible -- nothing in this
    codebase serializes two candidates' own `select_and_bind_story` calls
    against each other) could both read a miss and both attempt to insert,
    and migration 011's own partial unique indexes (`ux_media_writers_team`/
    `ux_media_writers_league`) are exactly what turns the LOSING insert into
    a real `asyncpg.UniqueViolationError` rather than a silent duplicate
    row -- caught here, and resolved by re-reading the row the WINNING
    insert just created, matching `dba2/data/repositories/media_repo.py::
    create_media_writer`'s own documented "let the DB be the source of
    truth for the conflict, don't paper over it with `ON CONFLICT DO
    NOTHING`" choice one layer up.

    **Round-14-critic fix (blocking finding 2): the insert attempt runs
    inside its own `dba2/data/db.py::transaction(ex)` scope, not bare on
    `ex` directly.** The bug this closes: when `ex` is a `Connection`
    already inside an open transaction (the multi-repo-in-one-transaction
    case `db.py`'s own module docstring exists for -- e.g. a trade's writes
    across four repos), a losing racer's `UniqueViolationError` aborts the
    ENTIRE surrounding transaction, not just this insert -- Postgres puts
    an aborted transaction into a state where every subsequent command
    (including the compensating re-read below) raises `asyncpg.
    InFailedSQLTransactionError` instead of running, which made the
    "caught here, resolved by re-reading" claim above true only for a bare
    `Pool` executor (each call there gets its own fresh connection/
    transaction, so one racer's abort cannot touch another's) and silently
    false for a `Connection` executor -- no test exercised that path.
    `transaction(ex)` already implements exactly the fix this needs and
    already ships in this codebase (`db.py`'s own docstring, `dba2/services/
    gm_service.py` and nine other services already call it this same way):
    given a `Connection`, it opens a SAVEPOINT via asyncpg's own nested-
    transaction support (`ex.transaction()` called on a connection already
    in a transaction transparently issues `SAVEPOINT`/`RELEASE SAVEPOINT`/
    `ROLLBACK TO SAVEPOINT` instead of a second top-level `BEGIN`/`COMMIT`)
    -- so a `UniqueViolationError` raised inside this block only unwinds
    back to the savepoint, leaving the OUTER transaction (and every write
    a caller made in it before this call) intact and still usable for the
    re-read that follows. Given a `Pool`, `transaction(ex)` opens a real
    connection+transaction exactly as `media_repo.create_media_writer(ex,
    ...)` already did implicitly for a single statement -- no behavior
    change for the already-covered, already-passing `Pool` call sites.
    `tests/integration/test_media_service.py::test_get_or_create_media_
    writer_race_survives_shared_transaction` forces this exact race with a
    `Connection` executor inside a real `transaction(pool)` block and
    confirms the function now returns the winner instead of raising.

    **`display_name` (docs/design/media-v2-newsroom.md MV-3b.2) -- the
    library's own persona-declared byline (`hot_take`'s "Duke Sabatino",
    `beat_writer`'s "Jordan Ellis"), `None` for the three personas that
    have none.** Three branches, per MV-3b.2's own text: a miss inserts
    `display_name or _generate_writer_name(...)` (a declared name wins,
    falling back to the generated one only when the library names none);
    a hit whose `display_name is None` or whose stored `name` already
    equals it returns the row untouched (every call after the first, and
    the whole path for the three personas with no `display_name`); a hit
    whose stored `name` differs calls the bulk, persona-scoped rename
    (`media_repo.update_media_writer_names_for_persona`) for a TEAM-scoped
    persona (`team_id is not None`) -- renaming every one of that
    persona's rows in the league in one statement, not just this team's,
    which is what lets `beat_writer`'s 30 rows reconcile as soon as the
    FIRST one is next written about rather than one team at a time over
    most of a season (MV-3b.2's own revision-5 correction) -- or the
    single-row-by-primary-key form (`update_media_writer_name`) for a
    LEAGUE-wide persona, then re-reads the row so the caller always gets
    the writer's current, real name back. Idempotent either way (the bulk
    form's own `name <> $3` predicate, the single-row form only ever
    called from a branch that already confirmed the names differ) -- it
    fires at most once per league per persona, ever."""
    existing = await media_repo.get_media_writer(
        ex, league_id=league_id, team_id=team_id, persona=persona
    )
    if existing is not None:
        if display_name is None or existing.name == display_name:
            return existing
        if team_id is not None:
            await media_repo.update_media_writer_names_for_persona(
                ex, league_id=league_id, persona=persona, name=display_name
            )
            renamed = await media_repo.get_media_writer(
                ex, league_id=league_id, team_id=team_id, persona=persona
            )
            assert renamed is not None, (
                "update_media_writer_names_for_persona just renamed this exact row -- a "
                "re-read of the same (league_id, team_id, persona) triple must find it"
            )
            return renamed
        return await media_repo.update_media_writer_name(ex, existing.id, display_name)

    name = display_name or _generate_writer_name(
        league_rng_seed, league_id=league_id, team_id=team_id, persona=persona
    )
    try:
        async with transaction(ex) as conn:
            return await media_repo.create_media_writer(
                conn, league_id=league_id, team_id=team_id, persona=persona, name=name
            )
    except asyncpg.UniqueViolationError:
        winner = await media_repo.get_media_writer(
            ex, league_id=league_id, team_id=team_id, persona=persona
        )
        assert winner is not None, (
            "create_media_writer raised UniqueViolationError but a re-read of the same "
            "(league_id, team_id, persona) triple found nothing -- the conflicting row "
            "must exist for the violation to have been raised at all"
        )
        return winner


def _hot_take_kinds(personas: tuple[select.MediaPersona, ...]) -> frozenset[StoryKind]:
    """MV-3h.0's declared beat, read from the library rather than hard-coded
    -- `salience.build_publication_plan`'s own `hot_take_kinds` parameter is
    data, not a library object (that module may not import this one), so
    this is the one place that derives it. Every `StoryKind` string
    `hot_take`'s own `story_kind_weights` names at a positive weight,
    parsed back into real `StoryKind` members (a library that validated
    successfully guarantees every key is a real member, per `_validate_
    media_kind_vocabulary`, so this raises nothing for a real library)."""
    hot_take = next((p for p in personas if p.key == "hot_take"), None)
    if hot_take is None:
        return frozenset()
    return frozenset(
        StoryKind(kind_value)
        for kind_value, weight in hot_take.story_kind_weights.items()
        if weight > 0.0
    )


def _cadence_for(
    library: LoadedMediaLibrary, persona_key: str, phase: Phase
) -> PhaseCadence | None:
    """The one lookup both `_has_cap_headroom`/`_persona_multipliers_for`
    share -- `None` for a persona/phase this library's `phase_cadence`
    names nothing for (every hand-built test library that predates step 6,
    and the three-persona case where `phase_cadence` itself is `{}`),
    which both callers read as "uncapped"/"multiplier 1.0" respectively, so
    every pre-cadence caller keeps its old, unrestricted behavior."""
    return library.phase_cadence.get(persona_key, {}).get(phase.value)


def _has_cap_headroom(
    library: LoadedMediaLibrary, persona_key: str, phase: Phase, draws_so_far: Mapping[str, int]
) -> bool:
    """MV-3h's own `max_per_cycle`, checked against this cycle's real,
    advancing draw count for `persona_key` -- `True` (uncapped) when this
    library names no cadence cell for this exact `(persona, phase)` pair."""
    cadence = _cadence_for(library, persona_key, phase)
    if cadence is None:
        return True
    return draws_so_far.get(persona_key, 0) < cadence.max_per_cycle


def _persona_multipliers_for(
    library: LoadedMediaLibrary, *, phase: Phase, angle: Angle
) -> dict[str, float]:
    """MV-3h's own composed multiplier, per candidate: `phase_cadence[p]
    [phase].weight x (1.6 if p == "hot_take" and angle in DRAMA_ANGLES else
    1.0)`. Composed here (not in `select.py`, which stays pure and knows
    neither the phase nor the library) and handed to `select.select_
    persona`'s own `persona_multipliers` parameter unchanged."""
    return {
        persona.key: (
            (_cadence_for(library, persona.key, phase) or PhaseCadence(1.0, 0)).weight_multiplier
            * (1.6 if persona.key == "hot_take" and angle in select.DRAMA_ANGLES else 1.0)
        )
        for persona in library.personas
    }


def _eligible_personas_under_cap(
    library: LoadedMediaLibrary,
    story_kind: StoryKind,
    *,
    phase: Phase,
    group_restriction: frozenset[str] | None,
    draws_so_far: Mapping[str, int],
) -> frozenset[str]:
    """MV-1c.3's own "eligible-persona set after every in-force cap is
    applied" -- the same positive-weight-for-kind filter `select.
    select_persona` applies internally, ANDed with `group_restriction`
    (the publication-plan group's own admission rule: `None` for main,
    `{"hot_take"}` for band) and with `_has_cap_headroom`. Computed here,
    BEFORE `select_persona` is ever called, so a candidate whose reduced
    set is empty can be skipped without reaching that function's own
    empty-set `AssertionError` at all (MV-1c.3: "`select_persona` is not
    called at all for that candidate")."""
    return frozenset(
        persona.key
        for persona in library.personas
        if persona.story_kind_weights.get(story_kind.value, 0.0) > 0.0
        and (group_restriction is None or persona.key in group_restriction)
        and _has_cap_headroom(library, persona.key, phase, draws_so_far)
    )


@dataclasses.dataclass(frozen=True)
class Publication:
    """docs/design/media-v2-newsroom.md MV-1c.5 -- one entry in
    `run_media_selection_cycle`'s own single `enumerate()`, carrying its
    fixed `candidate_sequence`, the `ScoredCandidate` it was drawn from, and
    the real persisted `MediaStory` it produced. Replaces the corpus tool's
    old `zip(stories, candidates, strict=True)` reconstruction (MV-1c.5):
    once a caller has `tuple[Publication, ...]`, the pairing is CARRIED,
    never re-derived, so a plan whose `.stories` differs in length from its
    input candidates (a real band/profile group, or a real capped-out skip
    -- both real as of "Build order" step 6) still tells a caller exactly
    which candidate produced which story, with no zip and no length
    assumption."""

    candidate_sequence: int
    scored: salience.ScoredCandidate
    story: MediaStory


@dataclasses.dataclass(frozen=True)
class MediaSelectionCycleResult:
    """docs/design/media-v2-newsroom.md MV-1c.5 -- what `run_media_
    selection_cycle` returns instead of a bare `tuple[MediaStory, ...]`,
    now that it scores (MV-1c.4 moved `salience.score_candidates` here from
    `run_media_pulse_cycle`, so `candidates_scored`/`candidates_above_
    floor` -- Decision P4-103, Part A's own two counts -- live HERE now,
    one set per arm, not on the shared pulse result).

    **`capped_out`/`band_published` are real as of "Build order" step 6**
    (MV-1c.3's cap-never-waived rule, MV-3h's band): `capped_out` counts
    every main/band candidate whose reduced eligible-persona set was empty
    after `_eligible_personas_under_cap`'s own cap check (never a profile
    -- MV-3h's "two independent numbers" rule exempts the reserved quota
    from `beat_writer`'s own event cap entirely); `band_published` counts
    every band-group candidate that DID produce a story. **`fallback_
    forced` is still always `0`, a real, disclosed narrowing carried
    forward** -- `select_media_template`'s own MV-1c.2 lookup-fallback
    branch is a defensive net for a library bug that would violate its own
    load-time guarantee, and this function has no way to observe from a
    template draw's return value alone whether that branch fired (it
    returns a `MediaTemplate` either way) -- exposing that signal is real,
    additional plumbing through `select_media_template`/`select_and_bind_
    story`, not built this step, and disclosed rather than guessed at.
    `profiles_published` is a genuine, scored pass-through (MV-3's
    "profiles bypass the floor and K" rule), real since step 3b."""

    publications: tuple[Publication, ...]
    stories: tuple[MediaStory, ...]
    candidates_scored: int
    candidates_above_floor: int
    plan_size: int
    capped_out: int
    fallback_forced: int
    band_published: int
    profiles_published: int


def _persona_column_for(library: LoadedMediaLibrary, voice: str) -> str:
    """MV-12.1 §3's "persona column" field, for one `media_stories.voice`
    value -- `library.persona_metadata[voice].column` when the library
    still carries that persona (the real case for every row written under
    the library that wrote it), falling back to `dba2.domain.media.column_
    for` (its own "any key this dict does not carry" fallback) for a
    history row whose persona has since been removed from `seeds/media_
    phrases.json` -- never a `KeyError` for a brief section that is
    supposed to degrade gracefully, never never a fabricated column name."""
    metadata = library.persona_metadata.get(voice)
    if metadata is not None:
        return metadata.column
    return column_for(voice)


def _cycle_brief_inputs(
    *,
    season: int,
    phase: Phase,
    cycle_index: int,
    library: LoadedMediaLibrary,
    plan: salience.PublicationPlan,
    current_snapshot: pulse.PulseSnapshot | None,
    team_names: Mapping[int, str] | None,
    recent_stories: Sequence[MediaStory] | None,
    season_arc_snapshot_rows: Sequence[MediaPulseSnapshot] | None,
) -> media_context.CycleBriefInputs | None:
    """docs/design/media-v2-newsroom.md MV-12.1: the ONE per-cycle
    assembly of every already-loaded ingredient `dba2.media.context.
    build_league_brief` needs -- built once, here, never per candidate.

    `None` (no brief built this cycle at all) when the caller has no real
    `current_snapshot`/`team_names` to build §1 from -- the two ingredients
    every real caller (`season_service._run_media_cycle`) always has
    (`run_media_pulse_cycle` runs unconditionally, every cycle) and every
    OTHER existing caller/test (predating step 7.5b) passes neither,
    keeping this function, `select_and_bind_story`, and `run_media_
    selection_cycle` itself fully additive: no brief is built and `write()`
    receives `brief=None`, byte-identical to pre-7.5b behavior.
    `recent_stories`/`season_arc_snapshot_rows` are independently optional
    -- "absent is neutral," MV-12.1's own rule: a league's first 1-3
    cycles, or its first season, still gets §1/§2 populated with §3/§4/§5
    empty, never blocking brief construction entirely."""
    if current_snapshot is None or team_names is None:
        return None
    other_desks_pool: tuple[tuple[salience.ScoredCandidate, media_context.SalienceBand], ...] = (
        tuple((scored, media_context.SalienceBand.MAIN) for scored in plan.main)
        + tuple((scored, media_context.SalienceBand.BAND) for scored in plan.band)
        + tuple((scored, media_context.SalienceBand.PROFILE) for scored in plan.profile)
    )
    recent_wire_pool = tuple(
        (story, _persona_column_for(library, story.voice)) for story in (recent_stories or ())
    )
    season_arc_top, season_arc_bottom = media_context.build_season_arc_rows(
        season_arc_snapshot_rows or (), team_names
    )
    return media_context.CycleBriefInputs(
        season=season,
        phase=phase.value,
        cycle_index=cycle_index,
        league_games_played=media_context.league_games_played(current_snapshot),
        current_snapshot=current_snapshot,
        team_names=team_names,
        standings_base=media_context.build_standings_base(current_snapshot, team_names),
        other_desks_pool=other_desks_pool,
        recent_wire_pool=recent_wire_pool,
        season_arc_top=season_arc_top,
        season_arc_bottom=season_arc_bottom,
    )


async def run_media_selection_cycle(
    ex: Executor,
    *,
    league_id: int,
    league_rng_seed: int,
    season: int,
    phase: Phase,
    cycle_index: int,
    candidates: tuple[salience.SalienceCandidate, ...],
    library: LoadedMediaLibrary,
    use_counts: Mapping[str, Mapping[str, int]],
    profile_candidates: tuple[salience.SalienceCandidate, ...] = (),
    subject_last_seen: Mapping[tuple[str, int], int] | None = None,
    current_snapshot: pulse.PulseSnapshot | None = None,
    team_names: Mapping[int, str] | None = None,
    recent_stories: Sequence[MediaStory] | None = None,
    season_arc_snapshot_rows: Sequence[MediaPulseSnapshot] | None = None,
) -> MediaSelectionCycleResult:
    """The single per-cycle loop Decision P4-88 requires: derives
    `candidate_sequence` in exactly ONE place (this function's own
    `enumerate()` over `salience.build_publication_plan`'s own concatenated
    `.candidates`), so no second, independently-computed index anywhere
    else can silently reproduce the RNG-salt collision step 18's last two
    fix rounds spent two full rounds closing.

    **docs/design/media-v2-newsroom.md MV-1c.4, "Build order" step 3b: this
    function now scores.** `candidates` is `run_media_pulse_cycle`'s own
    raw, UNSCORED `SalienceCandidate` tuple -- this function is the
    arm-dependent side of the pulse/selection boundary (MV-1c.4's own
    reasoning: two selection calls over the SAME pulse result, against two
    different libraries, must score independently), so it calls `salience.
    score_candidates` itself, then `salience.build_publication_plan`, then
    `enumerate()`s the plan's own `.candidates` -- never a second,
    independently-derived top-K. `select_top_k`'s own documented tie-break
    (stable Python `sorted` over input order) is what makes "the plan's own
    `enumerate()` position" a real, reproducible property in the first
    place -- see that function's own docstring.

    `subject_last_seen`, when given, replaces each candidate's `cycles_
    since_last_seen` with a real value derived from it (keyed by `_subject_
    identity`, cycle distance from the caller-supplied last-seen cycle) --
    the mechanism MV-10 names, arm-scoped by construction because it is a
    per-call parameter, not a shared read. `None` leaves every candidate at
    its existing neutral/max-novelty default. **Not every real caller
    passes `None` as of a later fix round**: `season_service._run_media_
    cycle` passes a real `load_subject_last_seen` read, and `tools/
    generate_media_corpus.py` passes a real, single in-memory map it
    maintains itself, advanced from its LIVE arm's own publications only
    and handed UNCHANGED to both arms' calls for a cycle (never a shared DB
    read, since both of its arms write to the same `media_stories` rows
    with nothing distinguishing them -- MV-5 revision 5, corrected
    2026-09-23 after a fix round shipped two independently-tracked per-arm
    maps, which broke Decision P4-97 ground (ii) by making the two arms'
    scoring inputs diverge on more than `with_voices_disabled`) -- `None`
    is only ever real for a caller with no history to derive one from yet
    (a from-scratch test fixture, mainly).

    `profile_candidates` is scored and appended to the plan unconditionally
    (MV-3's "profiles bypass the floor and K" rule). **Not every real
    caller passes `()` as of a later fix round**: `season_service._run_
    media_cycle` passes a real `load_team_profile_packages` load, and
    `tools/generate_media_corpus.py` composes ONE real load per cycle and
    passes the SAME tuple to BOTH arms (corrected 2026-09-23; MV-5 revision
    4's own concern was never calling the *loader* twice in one cycle --
    which would double-consume the eligible-team pool `media_stories`'
    no-repeat rule reads from `media_stories` -- not that the two arms must
    publish different profile content). The control arm therefore now
    "publishes" the same `TEAM_PROFILE` stories as live in its own
    accounting too, which is correct: the control arm is a single-channel
    (`with_voices_disabled`) measurement device, not a self-consistent
    alternate league, and it already shares the RNG seed and the
    underlying league/pulse result with the live arm for the same reason.

    **`current_snapshot`/`team_names`/`recent_stories`/`season_arc_
    snapshot_rows` (docs/design/media-v2-newsroom.md MV-12, "Build order"
    step 7.5b) -- all four additive and defaulted, mirroring `subject_
    last_seen`'s own shape exactly.** `None`/`None` for the first two skips
    `LeagueBrief` construction ENTIRELY for this cycle (`_cycle_brief_
    inputs` returns `None`), so every existing caller/test keeps its
    pre-7.5b behavior byte-for-byte. A real caller (`season_service._run_
    media_cycle`) passes `run_media_pulse_cycle`'s own `snapshot`/`team_
    names` (both real every cycle, unconditionally -- unlike `league_
    snapshot`/`gm_context`, which MV-11's own disclosed `NotFoundError`
    fallback can leave `None` pre-trade-deadline), plus a fresh `media_
    repo.list_media_stories_for_cycle_range` read (the last 3 cycles) and
    `media_repo.get_final_pulse_snapshot_for_season(season - 1)` -- MV-
    12.1's own "total new I/O: 2 reads per cycle" (both happen once, here,
    never per candidate below).

    Sequential, not concurrent (`for` + `await`, not `asyncio.gather`) --
    deliberate: `get_or_create_media_writer`'s own race-handling above
    tolerates two DIFFERENT candidates' concurrent writer creation attempts
    landing on the SAME persona, but concurrent execution within a single
    cycle's own candidate list buys nothing here (each candidate's
    persistence is already a handful of small, cheap round trips, not a
    long-running operation) and would only add the exact race surface this
    function's own sequential shape avoids needing to reason about at all.

    Returns a `MediaSelectionCycleResult` (MV-1c.5) whose `.stories` is one
    persisted `MediaStory` per plan entry, in plan order -- **not wired
    into any harness or per-season cycle loop yet** (disclosed scope
    choice, see this file's own module docstring): this is the real,
    callable, fully-tested per-cycle entry point step 19 was scoped to
    build; wiring a live call site into `tools/gm_harness/season.py`'s own
    cycle loop is left for whichever future step actually needs media
    stories to appear during a real per-season run.

    **Round-14-critic fix (blocking finding 1): `use_counts` is now
    advanced across the candidates in THIS cycle, not passed through
    unchanged.** Before this fix every candidate in the `for` loop below
    saw the identical, caller-supplied `use_counts` mapping -- the
    parameter's own `Mapping[str, int]` (read-only) type meant nothing in
    this function even could have mutated it in place, so `select_media_
    template`'s repeat penalty (`seeds/media_phrases.json`'s
    `repeat_penalty = 0.3`) was a structural no-op for every
    candidate after the first: two same-shape candidates competing for the
    same small template pool in one cycle could draw the SAME template
    twice, because the second draw never saw the first draw's own pick.

    **Round-15-critic fix (the one remaining Round-14 blocking finding):
    the advancing counts are now scoped per `(voice, cycle)`, `{voice:
    {template_id: count}}`, not one flat `{template_id: count}` pool
    shared by every persona drawn in the cycle.** Round 14 shipped the flat
    shape and its own docstring/design-doc text (§28) claimed that was what
    Decision P4-29 already required -- false: P4-29's own text specifies
    the scope as per `(voice, cycle)` (design doc §7), and a critic
    reproduction confirmed the flat shape is a real, measurable bug, not a
    theoretical one -- replaying `select_and_bind_story`'s real RNG salts
    for six same-shape candidates across different personas, the flat and
    per-`(voice, cycle)` scopes produced different template draws on 7 of
    12 tested seeds, because two personas sharing one kind's single
    voice-wildcard fallback template can suppress each other's repeat
    penalty across a voice boundary that was never supposed to be shared.
    `running_use_counts` is therefore `dict[str, dict[str, int]]`, one
    level deeper than round 14's `dict[str, int]`, keyed first by `story.
    voice` (`MediaStory`'s own column, Decision P4-38) -- **genuinely
    mirroring** `dba2/services/gm_service.py::_select_negotiation_phrase`'s
    own already-shipped shape (`~:3066-3071`) now, not merely claiming to:
    that function's own `phrase_use_counts.setdefault(speaker_id, {})`
    (outer, per-speaker partition) plus `use_counts[template_id] = use_
    counts.get(template_id, 0) + 1` (inner, post-draw increment) is exactly
    the two-line shape the loop below uses, `setdefault` for the outer
    per-voice partition and `.get(..., 0) + 1` for the inner per-template
    increment -- round 14's own docstring claimed this mirroring while the
    code only did the inner half; the outer half is what this round adds.
    Seeded from the caller's own `use_counts` via a one-level-deep copy
    (never mutates the caller's own outer OR inner mappings -- callers may
    reuse the same `use_counts` across seasons' worth of cycles, so both
    levels are copied rather than adopted by reference), then advanced
    after every `select_and_bind_story` call and before the next one runs
    -- so candidate N+1 in this cycle sees every prior candidate's real
    draw for the SAME voice, and never a different voice's draw, and the
    repeat penalty has a real, per-voice-scoped, advancing count to apply
    against."""
    scoring_inputs = candidates
    if subject_last_seen is not None:
        scoring_inputs = tuple(
            dataclasses.replace(
                candidate,
                cycles_since_last_seen=_cycles_since_last_seen(
                    subject_last_seen, subject=candidate.package.subject, cycle_index=cycle_index
                ),
            )
            for candidate in candidates
        )
    scored = salience.score_candidates(scoring_inputs)
    hot_take_kinds = _hot_take_kinds(library.personas)
    plan = salience.build_publication_plan(
        scored, phase=phase, hot_take_kinds=hot_take_kinds, profile_candidates=profile_candidates
    )
    plan_candidates = plan.candidates
    main_count = len(plan.main)
    band_count = len(plan.band)
    # docs/design/media-v2-newsroom.md MV-12.1, "Build order" step 7.5b:
    # every already-loaded ingredient a `LeagueBrief` needs, cached ONCE
    # here (not per candidate) -- `None` when this caller has no real
    # `current_snapshot`/`team_names` to build one from (every existing
    # test/call site that predates step 7.5b), which is what keeps this
    # whole feature additive: `brief_inputs=None` below produces
    # `brief=None` at every `write()` call, byte-identical to pre-7.5b
    # behavior.
    brief_inputs = _cycle_brief_inputs(
        season=season, phase=phase, cycle_index=cycle_index, library=library, plan=plan,
        current_snapshot=current_snapshot, team_names=team_names,
        recent_stories=recent_stories, season_arc_snapshot_rows=season_arc_snapshot_rows,
    )

    publications: list[Publication] = []
    running_use_counts: dict[str, dict[str, int]] = {
        voice: dict(counts) for voice, counts in use_counts.items()
    }
    # MV-1c.3's own per-cycle draw count, advanced for every REAL draw
    # (never for a skipped/capped-out candidate) -- checked against MV-3h's
    # `max_per_cycle` before each of the MAIN/BAND groups' own draws.
    # Deliberately NOT advanced by a PROFILE-group draw and NOT checked at
    # all for one: MV-3h's own "two independent numbers, two mechanisms,
    # one persona" rule means `beat_writer`'s cadence cell governs only its
    # EVENT-driven (main-pool) draw -- the `TEAM_PROFILE` quota is a
    # separate, flat reservation (already bounded to `team_profile_quota`
    # by `load_team_profile_packages`' own caller) that must not compete
    # with, or count against, that same cap.
    draws_so_far: dict[str, int] = {}
    capped_out = 0
    band_published = 0
    for candidate_sequence, plan_entry in enumerate(plan_candidates):
        package = plan_entry.candidate.package
        if candidate_sequence < main_count:
            group_restriction: frozenset[str] | None = None
            is_band = False
            is_profile = False
        elif candidate_sequence < main_count + band_count:
            group_restriction = frozenset({"hot_take"})
            is_band = True
            is_profile = False
        else:
            # No `group_restriction` here -- the profile branch below never
            # calls `_eligible_personas_under_cap` (profiles bypass the cap
            # entirely, MV-3h's "two independent numbers" rule) and hardcodes
            # `eligible_personas` directly instead, so a `group_restriction`
            # assignment in this branch would never be read. Was dead code
            # before this fix.
            is_band = False
            is_profile = True

        if is_profile:
            # No cap check at all (see the module-level comment above) --
            # `beat_writer` always has a positive `team_profile` weight
            # (`_validate_media_every_kind_has_a_persona`), so this is
            # never empty for a real library.
            eligible_personas = frozenset({"beat_writer"})
        else:
            eligible_personas = _eligible_personas_under_cap(
                library, package.story_kind, phase=phase,
                group_restriction=group_restriction, draws_so_far=draws_so_far,
            )
            if not eligible_personas:
                capped_out += 1
                continue

        persona_multipliers = _persona_multipliers_for(library, phase=phase, angle=package.angle)
        story = await select_and_bind_story(
            ex,
            league_id=league_id,
            league_rng_seed=league_rng_seed,
            season=season,
            phase=phase,
            cycle_index=cycle_index,
            scored=plan_entry,
            library=library,
            use_counts=running_use_counts,
            candidate_sequence=candidate_sequence,
            eligible_personas=eligible_personas,
            persona_multipliers=persona_multipliers,
            brief_inputs=brief_inputs,
        )
        if not is_profile:
            draws_so_far[story.voice] = draws_so_far.get(story.voice, 0) + 1
        if is_band:
            band_published += 1
        # **Build step 7: no manual `running_use_counts` increment here
        # anymore.** `select_and_bind_story` now hands `running_use_counts`
        # to a real `TemplateWriter` by reference, and that writer's own
        # `write()` advances it (`self._use_counts.setdefault(...)` plus the
        # per-template increment) before this line would ever run -- a
        # second increment here would double-count every draw. The
        # per-`(voice, cycle)` SCOPE this replaces is unchanged (see this
        # function's own extended docstring above); only which line does
        # the advancing moved, from this loop into the writer it now calls.
        publications.append(
            Publication(candidate_sequence=candidate_sequence, scored=plan_entry, story=story)
        )

    return MediaSelectionCycleResult(
        publications=tuple(publications),
        stories=tuple(p.story for p in publications),
        candidates_scored=len(scored),
        candidates_above_floor=len(salience.above_floor(scored)),
        plan_size=len(plan_candidates),
        capped_out=capped_out,
        fallback_forced=0,
        band_published=band_published,
        profiles_published=len(plan.profile),
    )


# ---------------------------------------------------------------------------
# The AI rewrite pass (docs/design/media-v2-step8-anthropic-writer.md S8-1,
# S8-9 - S8-13). Post-commit: every story is already a correct, publishable
# template story before the first model call, so a gate failure is simply
# "do nothing" and the fallback path is today's behavior exactly.
# ---------------------------------------------------------------------------

_BREAKER_CONSECUTIVE_FAILURES = 3
_BREAKER_CONSECUTIVE_CLI_ERRORS = 2
"""S8-9, as amended by the gate retry (gates-v2 G2-18): abort the rest of a
pass after 3 consecutive STORY failures, or 2 consecutive `cli_error`s (an
exhausted usage window or bad auth is made worse by retrying). A story
failure is a story still rejected after all its attempts, or one that hit a
writer failure (which is never retried). A rejection that a retry then fixes
is not a failure and does not count. Twelve stories x a 90 s timeout would
otherwise be eighteen minutes of a commissioner staring at a silent press."""

_RETRY_WITH_FEEDBACK = True
"""Whether a retry tells the model which gate rejected its draft and on what
(`media_prompt.build_retry_note`), or just asks again. Measured live, both
arms on the same 5 rejected stories (G2-18): feedback landed on the first
retry 4/5 against plain's 3/5 (6 retry calls against 7), both 5/5 by the
third call, and neither accepted a wrong-entity or wrong-direction text. A
small sample, so a constant, not a config field: it is a measured property
of the prompt, not a commissioner choice."""


@dataclasses.dataclass(frozen=True)
class AttemptRecord:
    """One CLI call for one story. `result` is `accepted`, the name of the gate
    that rejected the text (`numbers_bound`, `claims`, ...), or the writer's
    failure category (`timeout`, `cli_error`, ...). `offending` is the gate's
    lines on a rejection, never the body."""

    result: str
    offending: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class StoryAttempts:
    """Per-story telemetry for the pass log: every call the story took and how
    it ended (`accepted`, `gate_failed`, or a writer failure category)."""

    story_id: int
    voice: str
    attempts: tuple[AttemptRecord, ...]
    outcome: str

    @property
    def calls(self) -> int:
        return len(self.attempts)


@dataclasses.dataclass(frozen=True)
class RewriteSummary:
    """What one pass did. `attempted` counts STORIES given at least one call
    and `calls` the CLI calls spent on them, retries included; `accepted` the
    stories updated. `failures` is per story, by how it ended (`gate_failed`
    for a story still rejected after its last attempt, else the writer
    category); `gate_rejections` counts every rejected CALL by gate. `retried`
    is the stories that took more than one call and `accepted_on_retry` those
    of them that were updated. `skipped` are stories eligible but not
    attempted (over the per-cycle cap, after the breaker aborted, or with the
    call budget spent). `disabled_reason` is set only when the pass could not
    start at all (flag off, or no `claude` CLI). `stories` is the per-story
    log, in story id order."""

    attempted: int = 0
    accepted: int = 0
    failures: Mapping[str, int] = dataclasses.field(default_factory=dict)
    skipped: int = 0
    aborted: bool = False
    disabled_reason: str | None = None
    updated_story_ids: tuple[int, ...] = ()
    total_cost_usd: float = 0.0
    wall_clock_s: float = 0.0
    mean_input_tokens: float | None = None
    mean_output_tokens: float | None = None
    calls: int = 0
    call_budget: int = 0
    budget_exhausted: bool = False
    retried: int = 0
    accepted_on_retry: int = 0
    gate_rejections: Mapping[str, int] = dataclasses.field(default_factory=dict)
    stories: tuple[StoryAttempts, ...] = ()


@dataclasses.dataclass(frozen=True)
class _Accepted:
    story_id: int
    headline: str
    body: str
    backend: str


def _prepare_story_for_ai(
    story: MediaStory, library: LoadedMediaLibrary, writer_names: Mapping[int, str]
) -> tuple[StoryPackage, FormatTier, LeagueBrief | None, PersonaVoice] | None:
    """Everything the AI writer needs, rebuilt from one persisted row, or
    `None` (logged) when the row cannot be used. An unreadable BRIEF is not
    fatal: the story runs brief-less rather than re-deriving a stale brief
    (S8-14a -- a re-derived brief would disagree with the one the story was
    selected against, which is worse than none)."""
    metadata = library.persona_metadata.get(story.voice)
    if metadata is None:
        log.warning("media AI: no persona metadata for %r (story %s)", story.voice, story.id)
        return None
    try:
        package = StoryPackage.from_prompt_json(story.fact_package)
        tier = FormatTier(story.format_tier)
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("media AI: story %s has an unreadable fact_package (%s)", story.id, exc)
        return None
    brief: LeagueBrief | None = None
    if story.league_brief:
        try:
            brief = LeagueBrief.from_prompt_json(story.league_brief)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("media AI: story %s brief unreadable (%s); brief-less", story.id, exc)
    persona = resolve_persona_voice(
        metadata, story.voice, generated_name=writer_names.get(story.writer_id, story.voice)
    )
    return package, tier, brief, persona


@dataclasses.dataclass(frozen=True)
class AiStoryPreview:
    """`preview_story_for_ai`'s result (the human-verification tool's view of
    one story): the exact system prompt and stdin payload, and -- only when a
    live call was requested -- the writer outcome, the gate result and the
    text that WOULD be stored. Nothing here is ever written."""

    system_prompt: str
    payload: str
    outcome: media_writer_llm.WriterOutcome | None = None
    gate: verify.GateResult | None = None
    would_be_headline: str | None = None
    would_be_body: str | None = None


async def preview_story_for_ai(
    ex: Executor,
    *,
    story_id: int,
    library: LoadedMediaLibrary,
    config: Config,
    live: bool,
    runner: media_writer_llm.Runner | None = None,
) -> AiStoryPreview:
    """`tools/newsroom_ai.py`'s one entry point (S8-15 item 7). `live=False`
    renders the prompt and exits: no auth, no call, no cost -- the way a
    prompt change is reviewed. `live=True` makes exactly one call and gates
    it, still writing nothing. Raises `LookupError` for an unknown or
    unusable story (a tool, not the press, so a loud failure is right)."""
    story = await media_repo.get_media_story(ex, story_id)
    if story is None:
        raise LookupError(f"media story {story_id} not found")
    writers = await media_repo.list_media_writers_for_league(ex, story.league_id)
    prepared = _prepare_story_for_ai(story, library, {w.id: w.name for w in writers})
    if prepared is None:
        raise LookupError(f"media story {story_id} cannot be rebuilt for the AI writer (logged)")
    package, tier, brief, persona = prepared
    budget = budget_for(tier, package.story_kind)
    system_prompt = build_persona_card(
        persona, tier, title_max=budget.title_max, description_max=budget.description_max,
        has_brief=brief is not None,
    )
    payload = serialize_payload(
        build_payload(
            package, brief, tier, title_max=budget.title_max,
            description_max=budget.description_max,
        )
    )
    if not live:
        return AiStoryPreview(system_prompt=system_prompt, payload=payload)

    teams = await team_repo.list_teams_for_league(ex, story.league_id)
    vocabulary = tuple(
        {t.conference for t in teams if t.conference}
        | {f"{t.city} {t.name}" for t in teams}
        | {t.name for t in teams}
    )
    with media_writer_llm.AnthropicWriter(
        model=config.media_ai_model,
        effort=config.media_ai_effort,
        timeout_s=config.media_ai_timeout_s,
        limits_for=lambda t, p: (budget.title_max, budget.description_max),
        cli_path=config.media_ai_cli_path,
        runner=runner,
    ) as writer:
        outcome = await writer.attempt(package, persona=persona, tier=tier, brief=brief)
    if outcome.written is None:
        return AiStoryPreview(system_prompt=system_prompt, payload=payload, outcome=outcome)
    headline = clamp_text(outcome.written.headline, budget.title_max, field_name="headline")
    body = clamp_text(outcome.written.body, budget.description_max, field_name="body")
    gate = verify.verify_story_text(
        package, headline, body, persona=persona, brief=brief, team_names=vocabulary,
        team_aliases=team_aliases_for(teams),
    )
    return AiStoryPreview(
        system_prompt=system_prompt, payload=payload, outcome=outcome, gate=gate,
        would_be_headline=apply_headline_style(headline, persona.register), would_be_body=body,
    )


async def rewrite_cycle_stories_with_ai(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    cycle_index: int,
    library: LoadedMediaLibrary,
    config: Config,
    runner: media_writer_llm.Runner | None = None,
) -> RewriteSummary:
    """Rewrite one cycle's unposted template stories with the AI writer, then
    return a summary. **Never raises for a writer failure** (the caller runs
    this inside `run_after_commit` beside the wire post, S8-9), and with
    `config.media_ai_enabled` False it returns immediately having read and
    written nothing.

    Selection (S8-3): unposted stories still `writer_backend == 'template'`
    (so a re-run never re-rewrites), optionally narrowed to
    `media_ai_personas`, highest `salience` first, at most
    `media_ai_max_stories_per_cycle` (a budget control on HOW MANY, not a
    rule about whose stories are worth writing). Calls run under an
    `asyncio.Semaphore(media_ai_concurrency)` (S8-12); results are applied
    in ascending story id regardless of completion order. The model's text
    is clamped to the tier budget, then gated (S8-10) as the stored text,
    and only a passing story is `UPDATE`d -- three columns, via
    `media_repo.update_media_story_text`.

    **The gate retry (gates-v2 G2-18).** A story whose text the gates reject
    is called again, up to `media_ai_max_attempts` calls in all, and every
    retry's text goes through every gate again. A writer failure (timeout,
    `cli_error`, a bad envelope ...) is never retried here: the writer's own
    single spawn retry is the only one. No call is made once the pass has
    spent `media_ai_call_budget` calls, so a bad run cannot burn unbounded
    subscription usage. The breaker counts stories, not rejections."""
    if not config.media_ai_enabled:
        return RewriteSummary(disabled_reason="disabled")

    stories = await media_repo.list_media_stories_for_league_season(
        ex, league_id=league_id, season=season, cycle_index=cycle_index
    )
    persona_filter = config.media_ai_persona_keys
    eligible = [
        s
        for s in stories
        if s.posted_at is None
        and s.writer_backend == "template"
        and s.fact_package
        and (not persona_filter or s.voice in persona_filter)
    ]
    eligible.sort(key=lambda s: (-s.salience, s.id))
    chosen = eligible[: config.media_ai_max_stories_per_cycle]
    skipped = len(eligible) - len(chosen)
    if not chosen:
        return RewriteSummary(skipped=skipped)

    if runner is None and media_writer_llm.resolve_cli_path(config.media_ai_cli_path) is None:
        log.warning("media AI pass disabled: claude CLI not found (set MEDIA_AI_CLI_PATH)")
        return RewriteSummary(disabled_reason="claude CLI not found", skipped=len(eligible))

    teams = await team_repo.list_teams_for_league(ex, league_id)
    vocabulary = tuple(
        {t.conference for t in teams if t.conference}
        | {f"{t.city} {t.name}" for t in teams}
        | {t.name for t in teams}
    )
    team_aliases = team_aliases_for(teams)
    writers = await media_repo.list_media_writers_for_league(ex, league_id)
    writer_names = {w.id: w.name for w in writers}

    def _limits(tier: FormatTier, package: StoryPackage) -> tuple[int, int]:
        budget = budget_for(tier, package.story_kind)
        return budget.title_max, budget.description_max

    max_attempts = config.media_ai_max_attempts
    call_budget = config.media_ai_call_budget
    failures: dict[str, int] = {}
    gate_rejections: dict[str, int] = {}
    accepted: list[_Accepted] = []
    story_log: list[StoryAttempts] = []
    telemetry: list[media_writer_llm.Telemetry] = []
    state = {
        "attempted": 0, "calls": 0, "consecutive": 0, "consecutive_cli": 0,
        "aborted": False, "budget_exhausted": False, "unstarted": len(chosen),
    }
    started = time.monotonic()

    def _record_failure(category: str) -> None:
        failures[category] = failures.get(category, 0) + 1
        state["consecutive"] += 1
        state["consecutive_cli"] = (
            state["consecutive_cli"] + 1 if category == media_writer_llm.CATEGORY_CLI_ERROR else 0
        )
        tripped = (
            state["consecutive"] >= _BREAKER_CONSECUTIVE_FAILURES
            or state["consecutive_cli"] >= _BREAKER_CONSECUTIVE_CLI_ERRORS
        )
        if tripped and not state["aborted"]:
            state["aborted"] = True
            log.warning(
                "media AI pass aborting after %d consecutive story failures (last: %s); "
                "remaining stories keep their template text",
                state["consecutive"], category,
            )

    def _may_call(first: bool) -> bool:
        """The call-budget guard, checked before EVERY call. A retry may not
        spend a call that a story still waiting for its FIRST call would need,
        so retries never cost another story its turn. The check and the
        increment are one synchronous step, so two concurrent stories can
        never both spend the last call."""
        reserved = 0 if first else state["unstarted"]
        if state["calls"] + reserved >= call_budget:
            if not state["budget_exhausted"]:
                state["budget_exhausted"] = True
                log.warning(
                    "media AI pass reached its call budget (%d of %d calls spent, %d held for "
                    "stories not yet started); stories past it keep their template text",
                    state["calls"], call_budget, reserved,
                )
            return False
        state["calls"] += 1
        return True

    with media_writer_llm.AnthropicWriter(
        model=config.media_ai_model,
        effort=config.media_ai_effort,
        timeout_s=config.media_ai_timeout_s,
        limits_for=_limits,
        cli_path=config.media_ai_cli_path,
        runner=runner,
    ) as writer:
        semaphore = asyncio.Semaphore(config.media_ai_concurrency)

        async def _one(story: MediaStory) -> None:
            async with semaphore:
                if state["aborted"]:
                    return
                state["unstarted"] -= 1
                prepared = _prepare_story_for_ai(story, library, writer_names)
                if prepared is None:
                    return
                package, tier, brief, persona = prepared
                budget = budget_for(tier, package.story_kind)
                records: list[AttemptRecord] = []
                retry_note: str | None = None
                ended = media_writer_llm.CATEGORY_GATE_FAILED
                for attempt_no in range(1, max_attempts + 1):
                    if attempt_no > 1 and state["aborted"]:
                        break
                    if not _may_call(first=attempt_no == 1):
                        break
                    if attempt_no == 1:
                        state["attempted"] += 1
                    outcome = await writer.attempt(
                        package, persona=persona, tier=tier, brief=brief, retry_note=retry_note
                    )
                    if outcome.telemetry is not None:
                        telemetry.append(outcome.telemetry)
                    if outcome.written is None:
                        ended = outcome.category or "unknown"
                        records.append(AttemptRecord(ended))
                        log.warning(
                            "media AI: story %s (%s) failed: %s %s",
                            story.id, story.voice, ended, outcome.detail,
                        )
                        break
                    headline = clamp_text(
                        outcome.written.headline, budget.title_max, field_name="headline"
                    )
                    body = clamp_text(
                        outcome.written.body, budget.description_max, field_name="body"
                    )
                    gate = verify.verify_story_text(
                        package, headline, body, persona=persona, brief=brief,
                        team_names=vocabulary, team_aliases=team_aliases,
                    )
                    if gate.passed:
                        ended = "accepted"
                        records.append(AttemptRecord(ended))
                        accepted.append(
                            _Accepted(
                                story_id=story.id,
                                headline=apply_headline_style(headline, persona.register),
                                body=body,
                                backend=outcome.written.backend,
                            )
                        )
                        break
                    gate_name = gate.gate or "unknown"
                    records.append(AttemptRecord(gate_name, gate.offending))
                    gate_rejections[gate_name] = gate_rejections.get(gate_name, 0) + 1
                    log.warning(
                        "media AI: story %s (%s) attempt %d/%d failed gate %s on %s",
                        story.id, story.voice, attempt_no, max_attempts, gate_name,
                        list(gate.offending),
                    )
                    log.debug("media AI: rejected story %s body: %r", story.id, body)
                    retry_note = (
                        build_retry_note(gate_name, gate.offending)
                        if _RETRY_WITH_FEEDBACK
                        else None
                    )
                if not records:
                    return  # the call budget was spent before this story's first call
                story_log.append(StoryAttempts(story.id, story.voice, tuple(records), ended))
                log.info(
                    "media AI: story %s (%s) %s after %d call(s); per call: %s",
                    story.id, story.voice, ended, len(records), [r.result for r in records],
                )
                if ended == "accepted":
                    state["consecutive"] = 0
                    state["consecutive_cli"] = 0
                else:
                    _record_failure(ended)

        await asyncio.gather(*(_one(s) for s in chosen))

    updated: list[int] = []
    for item in sorted(accepted, key=lambda a: a.story_id):
        try:
            await media_repo.update_media_story_text(
                ex, item.story_id, headline=item.headline, rendered_text=item.body,
                writer_backend=item.backend,
            )
        except LookupError:
            log.warning("media AI: story %s was posted or deleted mid-pass; left as-is",
                        item.story_id)
            continue
        updated.append(item.story_id)

    story_log.sort(key=lambda s: s.story_id)
    retried = [s for s in story_log if s.calls > 1]
    inputs = [t.input_tokens for t in telemetry if t.input_tokens is not None]
    outputs = [t.output_tokens for t in telemetry if t.output_tokens is not None]
    summary = RewriteSummary(
        attempted=state["attempted"],
        accepted=len(updated),
        failures=dict(sorted(failures.items())),
        skipped=skipped + (len(chosen) - state["attempted"]),
        aborted=state["aborted"],
        updated_story_ids=tuple(updated),
        total_cost_usd=sum(t.total_cost_usd or 0.0 for t in telemetry),
        wall_clock_s=time.monotonic() - started,
        mean_input_tokens=sum(inputs) / len(inputs) if inputs else None,
        mean_output_tokens=sum(outputs) / len(outputs) if outputs else None,
        calls=state["calls"],
        call_budget=call_budget,
        budget_exhausted=state["budget_exhausted"],
        retried=len(retried),
        accepted_on_retry=sum(1 for s in retried if s.story_id in updated),
        gate_rejections=dict(sorted(gate_rejections.items())),
        stories=tuple(story_log),
    )
    log.info(
        "media AI pass: attempted=%d accepted=%d (on retry %d) retried=%d calls=%d/%d "
        "failures=%s gate_rejections=%s skipped=%d aborted=%s budget_exhausted=%s "
        "est_cost_usd=%.4f wall=%.1fs mean_input_tokens=%s",
        summary.attempted, summary.accepted, summary.accepted_on_retry, summary.retried,
        summary.calls, summary.call_budget, dict(summary.failures),
        dict(summary.gate_rejections), summary.skipped, summary.aborted,
        summary.budget_exhausted, summary.total_cost_usd, summary.wall_clock_s,
        summary.mean_input_tokens,
    )
    return summary
