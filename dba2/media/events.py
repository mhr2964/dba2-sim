"""`dba2/media/events.py` -- Decision P4-27's event-sourced intake half:
turning an event a writer ALREADY wrote into a `StoryCandidate`. Pure:
given already-typed inputs, no I/O of any kind. docs/design/phase-4-
media-and-player-agency.md §7 (P4-27), §11 Stage D step 17.

**What is real and what is built-ready-but-unwired, stated rather than
left for a reader to discover -- Round 10 critic fix (B3): NEITHER
function below has a real production caller that invokes it once per
cycle today. The prior draft of this docstring claimed `ladder_transition_
candidate` was "wired to a real production caller," which was false and
contradicted `dba2/services/media_service.py`'s own docstring in the same
commit ("It does not wire ANY production caller... to invoke it once per
cycle"). The real, accurate distinction between the two is not "wired" vs
"not wired" -- it is WHY each one is unwired, and the two reasons are
different in kind:**

- `ladder_transition_candidate` has a real, DB-backed CALL PATH that
  already works end to end -- `dba2/services/media_service.py::load_
  player_unrest_candidates` calls it correctly, reading `player_motivation_
  events` through `dba2/services/agency_service.py`'s own read-model
  pattern (Decision P4-16's "one reader of one table" rule --
  `media_service.py` may not import `motivation_repo` directly;
  `tests/unit/test_layering.py::test_only_agency_service_imports_
  motivation_repo` enforces it as a whole-file check) -- but nothing in
  production actually CALLS `load_player_unrest_candidates` (or `run_
  media_pulse_cycle`, which calls it) once per cycle yet: only
  `tests/integration/test_media_service.py` invokes it today. The gap here
  is "the pipe is built and correct, nothing turns the tap" -- wiring a
  real per-cycle caller needs the persona/selection engine (step 18) to
  have somewhere real to send the output, per `media_service.py`'s own
  module docstring.
- `trade_execution_candidate` is built and unit-tested against hand-built
  inputs, the same way `tests/unit/managers/voice/test_frame.py` hand-
  builds `DecisionRecord`s to drive `NegotiationFrame` without a real
  trade engine in the loop -- but it has no real CALL PATH at all, because
  there is nothing correct to call it FROM yet. Decision P4-27's own text
  recommends "Phase 4 adds an emission" at the trade-execution site itself
  (`run_trade_window`) rather than polling the decision-log JSONL after
  the fact -- and the Stage A census independently found the JSONL CANNOT
  distinguish a scanned trade candidate from an executed one (docs/design/
  phase-4-stage-a-census.md step 4: 68,364 trade-shaped JSONL records
  scanned vs. 242 real executed trades over the same 5-season corpus, a
  282.5:1 scan-to-execution ratio -- the one real, reproducible
  measurement this document has for the gap, cited here rather than
  restated with a second, differently-defined ratio; a round 10 draft of
  this docstring added an unprovenanced "7,205 vs. 40" re-measurement with
  no named corpus, seed, or command, and used a different denominator
  concept (accept-verdicts, not all scanned records) -- withdrawn round
  11 as unreproducible rather than re-derived), so polling it here would
  be not merely against P4-27's own stated preference but actively wrong.
  Wiring a real emission call site inside `run_trade_window` (or the
  free-agency signing loop, for `signing_reaction`) is real, cross-cutting
  work outside `dba2/media/`/`dba2/services/media_service.py` -- it
  touches the trade/free-agency pipelines themselves -- and is left
  disclosed rather than built here, per this dispatch's own "resist scope
  creep into steps 18-20" instruction.

**Stage D step 21 addition (Decision P4-107, item 1): `draft_selection_
candidate` joins `ladder_transition_candidate` in the "real, DB-backed
CALL PATH that already works end to end" category, not the `trade_
execution_candidate` one.** `dba2/services/media_service.py::load_rookie_
watch_candidates` calls it correctly, reading `drafts`/`draft_selections`
through `dba2.data.repositories.draft_repo` (already a direct importer
elsewhere, e.g. `gm_service.py` -- no one-reader-of-one-table valve like
`motivation_repo`'s applies here) plus a real per-player games-played
reading through `stats_repo.get_player_season_stats`. See that
constructor's own docstring for why the games-played field cannot be
filled from the draft selection alone and is therefore a required,
caller-supplied parameter rather than a default.

Zero I/O, the same purity rules `dba2/media/frame.py`/`dba2/media/pulse.py`
already carry: no `dba2.data`, no `datetime.now()`, no bare
`random.Random(`, no `open(`, no `os.environ`. Imports only
`dba2.domain.motivation`, `dba2.domain.stats`, `dba2.media.frame` and
stdlib.

**Stage D step 23 addition (Decision P4-121 item 3): `career_scoring_
milestone_candidates` joins `draft_selection_candidate` in the "real,
DB-backed CALL PATH" category.** `dba2/services/media_service.py::load_
milestone_candidates` calls it correctly, reading `player_season_stats`
through the first bulk `player_season_stats`-for-a-league reader this
codebase has ever had (`stats_repo.get_career_scoring_to_date_for_
league`, §39.1 items 5-6). It is the one function in this module that
takes a whole-league ROW SET rather than one subject's own fields --
every other constructor here turns one already-identified event into one
candidate; this one derives WHICH players are subjects at all from a
league-wide ranking, so it cannot share `draft_selection_candidate`'s
per-subject shape and lives here as a second, adjacent pattern instead
of a forced fit into the first.
"""

from __future__ import annotations

from dba2.domain.motivation import MotivationAxis, RequestState
from dba2.domain.stats import PlayerCareerScoringToDate
from dba2.media.frame import StoryCandidate, StoryKind


def story_kind_for_request_state(state: RequestState) -> StoryKind | None:
    """`None` for `CONTENT` -- a player calming back down is not one of
    this phase's eleven story kinds (there is no "player_calm" kind, and
    manufacturing one for a non-event would invert Decision P4-25's own
    "listed rather than counted" discipline). `UNSETTLED`/`TRADE_
    REQUESTED`/`EXTENSION_DEMANDED` all read as `PLAYER_UNREST` -- the
    flavor distinction between the two rung-2 states is carried on the
    candidate's own `request_state` field (`derive_frame` passes it
    through unchanged), not on the story KIND itself; only `HOLDOUT` gets
    its own kind, matching `StoryKind`'s real eleven-member enum (there is
    no separate `trade_requested`/`extension_demanded` kind to map onto)."""
    if state is RequestState.HOLDOUT:
        return StoryKind.HOLDOUT_WATCH
    if state in (
        RequestState.UNSETTLED,
        RequestState.TRADE_REQUESTED,
        RequestState.EXTENSION_DEMANDED,
    ):
        return StoryKind.PLAYER_UNREST
    return None


def ladder_transition_candidate(
    *,
    player_id: int,
    request_state: RequestState,
    motivation_axis: MotivationAxis,
    morale: int,
) -> StoryCandidate | None:
    """One player's real ladder-transition event, turned into a candidate.
    `None` when the player's CURRENT state is `CONTENT` (see `story_kind_
    for_request_state`).

    **Reads the player's CURRENT `request_state`, not the transition
    event's own free-text `note`.** The `ladder_transition` event row
    Stage B/C's checkpoint writes (`LADDER_TRANSITION_EVENT_TYPE`,
    `dba2/agency/motivation.py`) carries a typed `axis` column but only a
    human-readable `note` (`f"{old.value}->{new.value}"`) for the actual
    state transition -- parsing that string here would be fragile string-
    matching against an internal debug field with no contract behind its
    format. Reading the player's CURRENT `request_state` instead (via
    `agency_service.load_motivation_for_league`, the caller's job) is both
    more robust and exactly correct for "the transition that just
    happened": Decision P4-11 fixes exactly two checkpoints a season, and
    nothing else writes `request_state` between one checkpoint and the
    next, so "an event exists" and "current state" agree by construction
    for any event from THIS season's most recent checkpoint. A player who
    (somehow) shows a `ladder_transition` event yet reads back `CONTENT`
    cannot happen under that constraint and is handled the same as any
    other content reading -- `None`, not a raised error.

    `motivation_magnitude` is `round(-morale)` -- `pressure_from_morale`'s
    own reading (`dba2/agency/motivation.py`), the SAME pressure value the
    ladder itself escalated or released on. This is the player's CURRENT
    morale, not a per-event morale snapshot (`player_motivation_events`
    stores none) -- a disclosed approximation, exact between checkpoints
    since nothing else moves morale in between (Decision P4-7)."""
    kind = story_kind_for_request_state(request_state)
    if kind is None:
        return None
    return StoryCandidate(
        kind=kind,
        player_id=player_id,
        motivation_axis=motivation_axis,
        motivation_magnitude=int(round(-morale)),
        request_state=request_state,
    )


def draft_selection_candidate(
    *,
    player_id: int,
    team_id: int | None,
    player_games_played_this_season: int | None,
) -> StoryCandidate:
    """One real draft selection, turned into a `StoryKind.ROOKIE_WATCH`
    candidate -- Decision P4-107 item 1, a field-for-field copy of
    `ladder_transition_candidate`'s own shape (real, committed data in;
    a candidate out; no `None`-return branch here, unlike that function,
    because every real draft selection is a real pick with a real subject
    -- there is no `RequestState.CONTENT`-shaped "nothing happened" case
    for a completed selection to collapse to).

    **`player_games_played_this_season` is the one field this constructor
    cannot fill from the draft selection alone, and it is REQUIRED (no
    default) so a caller cannot forget it exists.** `StoryKind.ROOKIE_
    WATCH` is one of `dba2/media/frame.py`'s `_PLAYER_SAMPLE_KINDS` --
    `derive_frame` marks a candidate `NO_GAMES_PLAYED`/`EARLY_SEASON_
    SAMPLE`-insufficient (dropped before scoring) unless this field reads
    a real, non-`None` value `>= _MIN_PLAYER_GAMES_FOR_SAMPLE`. The draft
    selection itself (`draft_repo.DraftSelection`) carries no games-played
    field -- `load_rookie_watch_candidates` (`dba2/services/media_
    service.py`) supplies a REAL per-player reading, `stats_repo.get_
    player_season_stats(...).games_played`, for the player's CURRENT
    season, not a fabricated stand-in; `None` here (a since-graduated
    rookie who has not yet played, or a season with no rebuilt stats row
    yet) correctly reads as genuinely insufficient rather than silently
    passing.

    `team_id` is the drafting team at PICK time (`DraftSelection.team_id`)
    -- not re-resolved against the player's current roster, since no
    rookie_watch template reads `team_name` (only `player_name`; verified
    against `seeds/media_phrases.json`'s three `rookie_watch` templates)
    and a stale draft-team id costs nothing downstream that a fresh lookup
    would avoid."""
    return StoryCandidate(
        kind=StoryKind.ROOKIE_WATCH,
        player_id=player_id,
        team_id=team_id,
        player_games_played_this_season=player_games_played_this_season,
    )


_MILESTONE_LEADERBOARD_SIZE = 10


def _career_points(row: PlayerCareerScoringToDate) -> int:
    return row.career_points_before_season + row.points_in_season


def career_scoring_milestone_candidates(
    rows: tuple[PlayerCareerScoringToDate, ...],
    *,
    leaderboard_size: int = _MILESTONE_LEADERBOARD_SIZE,
) -> tuple[StoryCandidate, ...]:
    """One candidate per player whose position on this league's all-time,
    in-league, regular-season career-scoring top `leaderboard_size` moved
    UP between the end of the prior season and `rows`'s own season
    (Decision P4-121 item 3, phase-4-media-and-player-agency.md §39.2
    Option C -- a RELATIVE achievement, never an absolute point total).
    Pure: `rows` is `stats_repo.get_career_scoring_to_date_for_league`'s
    own return values (the caller's job to fetch), one row per player who
    has ANY regular-season row at or before `rows`'s own season.

    **Two boards, built from the same rows.** The PRIOR board ranks every
    player with `career_points_before_season > 0` (his career total as it
    stood at the end of the prior season) descending, `player_id` ascending
    as the deterministic tiebreak for equal totals -- rank `1..M`. A player
    with `career_points_before_season == 0` (a rookie with no prior season,
    or anyone who scored nothing before this season) has NO prior rank --
    genuinely absent from the board, never a rank manufactured from a
    zero-zero tie, which is what keeps a league's first season of record
    from emitting ten stories ordered by nothing but serial id (§39.1
    item 9's own closing worry). The CURRENT board ranks every player with
    `career_points_before_season + points_in_season > 0`, same sort, rank
    `1..K`. A player who only has rows in seasons after `rows`'s own season
    never reaches this function at all -- `stats_repo`'s own `season <=
    $season` scope excludes him before this function ever sees a row.

    **Emit for player `p` iff `current_rank(p) <= leaderboard_size` AND
    (`p` has no prior rank OR `prior_rank(p) > current_rank(p)`)** -- a new
    entrant to the top ten, or someone already on it who passed another
    player (including a retired player whose own total is frozen and who
    is not, himself, a subject -- he has no games this season and so
    cannot appear as a mover, only be passed). A player who holds the exact
    same rank both times (a static board) is NOT emitted -- re-announcing
    an unchanged list is not a story.

    **Guard: an empty prior board returns `()`** -- the same empty-guard
    shape `load_player_unrest_candidates`/`load_rookie_watch_candidates`
    already use for "no prior state exists," and the honest reading for a
    league whose first season of record has just been recorded (there is
    no "up" to measure yet).

    **Emission order is part of the contract, not an implementation
    detail** (§39.1 item 9's tie-break consequence, once this producer
    exists to have one): candidates are emitted in CURRENT-board rank
    order, best career total first, so that a `salience.select_top_k` cut
    keeps the most significant movements rather than an arbitrary row
    order. The volume this can ever emit is bounded above by `leaderboard_
    size` BY CONSTRUCTION (`current_rank(p) <= leaderboard_size` is a
    precondition of emission at all) -- the one property §39.2's Option C
    was chosen for over an absolute-threshold rule, and it is proven by a
    dedicated volume-bound unit test, never merely assumed.

    **`player_games_played_this_season`/`season_points_per_game`.** The
    former is `games_played_in_season`, passed straight through (the same
    "real, caller-supplied, `None` is a genuine absence" contract `draft_
    selection_candidate` already documents -- here it is always a real
    `int`, since a row only exists for a player `stats_repo` actually
    found, never fabricated). `season_points_per_game` is `points_in_
    season / games_played_in_season` when `games_played_in_season > 0`,
    and left `None`, with NO division attempted, when it is `0` --
    `derive_frame`'s own `NO_GAMES_PLAYED` sufficiency gate (`frame.py`,
    unchanged by this step) drops such a candidate downstream regardless,
    but this function does not divide by zero to get there. `team_id` is
    not supplied -- no `milestone` template reads `{team_name}` (§39.1
    item 1), the same reasoning `draft_selection_candidate`'s own
    docstring already records for its own field choices, reached here for
    the same field."""
    prior_by_player = {r.player_id: r for r in rows if r.career_points_before_season > 0}
    if not prior_by_player:
        return ()

    prior_order = sorted(
        prior_by_player.values(),
        key=lambda r: (-r.career_points_before_season, r.player_id),
    )
    prior_rank = {r.player_id: i + 1 for i, r in enumerate(prior_order)}

    current_by_player = {r.player_id: r for r in rows if _career_points(r) > 0}
    current_order = sorted(
        current_by_player.values(),
        key=lambda r: (-_career_points(r), r.player_id),
    )

    candidates: list[StoryCandidate] = []
    for current_position, row in enumerate(current_order, start=1):
        if current_position > leaderboard_size:
            break
        prior_position = prior_rank.get(row.player_id)
        if prior_position is not None and prior_position <= current_position:
            continue

        games_played = row.games_played_in_season
        season_points_per_game = (
            row.points_in_season / games_played if games_played > 0 else None
        )
        candidates.append(
            StoryCandidate(
                kind=StoryKind.MILESTONE,
                player_id=row.player_id,
                player_games_played_this_season=games_played,
                season_points_per_game=season_points_per_game,
            )
        )
    return tuple(candidates)


def trade_execution_candidate(
    *,
    team_id: int,
    trade_in_player_ids: tuple[int, ...] = (),
    trade_out_player_ids: tuple[int, ...] = (),
    trade_in_pick_ids: tuple[int, ...] = (),
    trade_out_pick_ids: tuple[int, ...] = (),
) -> StoryCandidate:
    """One team's own side of one executed trade -- always `StoryKind.
    TRADE_REACTION`. **Built ready, no real production caller this step**
    (see module docstring). `TRADE_FALLOUT`'s own distinguishing trigger is
    left unbuilt here too: neither the design doc's §6.2 prose nor `dba2/
    media/frame.py`'s own docstrings (step 16) define what separates a
    "reaction" from a "fallout" beyond the two enum names -- inventing that
    distinction is a template-authoring judgment call for whoever builds
    the library (step 18), not a vigilance-layer one."""
    return StoryCandidate(
        kind=StoryKind.TRADE_REACTION,
        team_id=team_id,
        trade_in_player_ids=trade_in_player_ids,
        trade_out_player_ids=trade_out_player_ids,
        trade_in_pick_ids=trade_in_pick_ids,
        trade_out_pick_ids=trade_out_pick_ids,
    )
