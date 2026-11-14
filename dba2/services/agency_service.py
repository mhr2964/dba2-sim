"""Pillar 5's DB-backed caller (Decision P4-13's second of two): builds
`PlayerSituation` from already-loaded league state, and writes trait
generation, satisfaction/morale/trust, and `RequestState` ladder moves
through `motivation_repo`.

docs/design/phase-4-media-and-player-agency.md §11 Stage B steps 6/8/9.

**Write-only for the checkpoint half (Stage B's own scope boundary), now
read from for free agency (Decision P4-16, Stage C step 11).**
`load_motivation_for_league` (below) is the "agency service's own read
model" `rank_offers` reads through -- `gm_service.py`'s `run_phase_hook`
calls it, populates `GmContext.motivation_by_player`, and passes the
result into `run_free_agency`/`rank_offers`. `bootstrap_league_
motivation`/`run_agency_checkpoint` remain unwired into any real
phase-transition hook -- `phase/service.py`'s `advance`/`force_advance`
call nothing in `gm_service.py` today (no commissioner-triggered layer
exists yet for AI GM phases at all, confirmed: `run_phase_hook` is
called only from tests), so wiring the WRITE side into a real hook stays
a genuinely separate, unscoped future step; this stage's own step 11
needs read-back only, which the lightweight loader above supplies
without inventing that hook. This module's write half is built and
tested exactly as `dba2/managers/memory.py::decay_head_to_head_rows`
was: ready for a caller, with no fabricated production call site invented
just to exercise it (that module's own precedent, cited here rather than
repeated: "build the pure decay function so it's ready, but don't invent
a fake production call site").

The evaluator itself (`dba2.agency`) never sees a connection -- this
module's whole job is assembling the `PlayerSituation` inputs and turning
its pure `(axis, delta)` outputs into real `motivation_repo` writes,
inside one `transaction()` per player per checkpoint so a partial write
never leaves a player's satisfaction state and event log disagreeing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dba2.agency.motivation import (
    INITIAL_MORALE,
    INITIAL_REQUEST_STATE,
    INITIAL_SATISFACTION,
    INITIAL_TRUST,
    LADDER_TRANSITION_EVENT_TYPE,
    advance_request_state,
    compute_morale,
    decay_trust,
    dwell_is_open,
    evaluate_satisfaction,
    fold_satisfaction_deltas,
    fold_trust_deltas,
    generate_traits,
    largest_magnitude_axis,
    pressure_from_morale,
    resolve_request_opened_in_season,
    trade_request_pressure,
)
from dba2.agency.situation import PlayerSituation, window_success_score
from dba2.data.db import Executor, transaction
from dba2.data.repositories import award_repo, motivation_repo, player_repo, stats_repo
from dba2.domain.game import GameType
from dba2.domain.motivation import MotivationAxis, PlayerMotivation, PlayerMotivationEvent
from dba2.domain.phase import Phase
from dba2.domain.player import RosterStatus
from dba2.managers.needs import build_need_matrix, tier_for_overall
from dba2.managers.philosophy import PROFILES
from dba2.managers.scouting import player_age
from dba2.managers.snapshot import LeagueSnapshot, RosterPlayer, TeamSnapshot
from dba2.managers.valuation import effective_talent_ovr, market_salary_share

# **Decision P4-16 (Phase 4 Stage C step 11): `GmContext`/`load_gm_inputs`
# are NOT imported at module level here anymore.** `gm_service.py` now
# imports `load_motivation_for_league` (below) from THIS module at its
# own module level (its `run_phase_hook`'s FREE_AGENCY branch needs the
# "agency service's own read model" P4-16 names) -- a real, two-way
# dependency between these two files that a module-level import in both
# directions cannot satisfy (Python raises on the resulting partial-
# module circular import). `GmContext` is only ever used here as a type
# annotation (`from __future__ import annotations` makes every annotation
# a string at runtime, per `dba2/sim/composites.py`'s own identical
# precedent/docstring for this exact situation), so it moves under
# `TYPE_CHECKING`; `load_gm_inputs` is a real runtime call (inside `run_
# agency_checkpoint` below) and moves to a local import at its one call
# site instead, resolved lazily long after both modules' own top-level
# code has finished running.
if TYPE_CHECKING:
    from dba2.services.gm_service import GmContext

# ---------------------------------------------------------------------------
# Decision P4-9's disclosed, not-derived scales for the DB-backed builder --
# same status as `_ROLE_FIT_SCALE`/`SATISFACTION_SCALE` under `dba2/agency/`,
# named here because this module (not the pure package) is where the raw
# DB reads get turned into `PlayerSituation`'s expected units.
# ---------------------------------------------------------------------------

_FULL_MINUTES_ANCHOR = 2000.0
"""Matches `tools/advance_dev_league_season.py::_FULL_MINUTES_ANCHOR` for
the identical quantity -- a reader who has seen one disclosed anchor for
"a significant rotation player's season total" has seen the other."""

_TEAM_ACCOLADE_ANCHOR = 3.0
"""Three career awards reads "a decorated player" for `team_accolades`'s
`[0, 1]` normalization -- disclosed, not derived, same status as
`dba2/agency/situation.py::_TENURE_SEASONS_ANCHOR`."""


def _find_team_and_roster_player(
    snapshot: LeagueSnapshot, player_id: int
) -> tuple[TeamSnapshot, RosterPlayer] | tuple[None, None]:
    for team_snapshot in snapshot.teams:
        for roster_player in team_snapshot.roster:
            if roster_player.id == player_id:
                return team_snapshot, roster_player
    return None, None


async def build_player_situation(
    ex: Executor,
    *,
    league_id: int,
    season: int,
    snapshot: LeagueSnapshot,
    ctx: GmContext,
    player_id: int,
    motivation: PlayerMotivation,
) -> PlayerSituation:
    """Assembles one player's per-checkpoint `PlayerSituation` from an
    already-loaded `snapshot`/`ctx` (Decision P4-9's table) -- both are
    built ONCE per checkpoint by the caller (`run_agency_checkpoint`,
    below) and shared across every player, never rebuilt per player: a
    per-player `load_gm_inputs` call would be a 450-call N+1 on top of
    that function's already-documented 10-round-trip cost. Every field
    this function cannot compute is left `None` (P4-9's "absent is
    neutral, never zero" rule) rather than guessed at."""
    team_snapshot, roster_player = _find_team_and_roster_player(snapshot, player_id)
    if team_snapshot is None or roster_player is None:
        # A free agent or a player who left the league between the
        # snapshot being built and this call -- no team context at all.
        return PlayerSituation()

    team_success = window_success_score(team_snapshot.window)

    matrix = build_need_matrix(team_snapshot, tuning=ctx.tuning)
    tier = tier_for_overall(roster_player.player.overall_cached)
    position = roster_player.player.primary_position
    role_fit = matrix.need[position][tier] - matrix.glut[position][tier]

    season_stats = await stats_repo.get_player_season_stats(
        ex, league_id, player_id, season, GameType.REGULAR
    )
    minutes_share = (
        min(float(season_stats.minutes_played) / _FULL_MINUTES_ANCHOR, 1.5)
        if season_stats is not None
        else None
    )

    # Round-5 critic finding B2, as corrected by round-5's own re-
    # verification (design doc P4-9/P4-63 correction, below): the
    # removed proxy is replaced by `managers/valuation.py::market_
    # salary_share`, NOT a literal call to `market_value_for`.
    # `market_value_for` is real and reachable exactly as P4-63 says, but
    # it returns §4.5's multi-season DISCOUNTED TRADE-EQUITY score (an
    # abstract "value points" scale mixing talent and cap-surplus terms),
    # not a dollar figure comparable to one season's `year.salary` --
    # hand-verified this round: a 70-overall player on a $150M cap reads
    # `market_value_for ~= 186`, and `year.salary / 186` for any real
    # contract saturates `score_money` at `+1.0` for every player, every
    # checkpoint, which is a worse defect than the proxy this replaces.
    # `market_salary_share(effective_ovr, cap_settings) * cap_settings.
    # salary_cap` is the actual real, calibrated, ALREADY-PRODUCTION
    # market-salary curve (§4.4) `market_value_for` itself calls
    # internally to price surplus, and it is the same figure `managers/
    # decisions/signing.py:402/:462` already name `market` for a real
    # signing decision -- dollar-denominated and comparable to `year.
    # salary` by construction.
    pay_vs_market: float | None = None
    if roster_player.contract is not None:
        year = next((y for y in roster_player.contract.years if y.season == season), None)
        if year is not None:
            philosophy = ctx.philosophy_by_team[team_snapshot.id]
            effective_ovr = effective_talent_ovr(roster_player, PROFILES[philosophy])
            market_salary = (
                market_salary_share(float(effective_ovr), team_snapshot.cap_settings)
                * team_snapshot.cap_settings.salary_cap
            )
            if market_salary > 0:
                pay_vs_market = year.salary / market_salary

    # Decision P4-63's per-corpus register: the DB path is not scheduled
    # to gain a tenure mirror this stage (only `HarnessState`'s own
    # tenure counter is, per Stage B step 10) -- `tenure_seasons` stays
    # `None`-neutral here, disclosed rather than approximated from
    # contract-start (which would conflate "re-signed with this team" and
    # "acquired this team").
    tenure_seasons: float | None = None

    market_match: float | None = None
    if motivation.hometown_team_id is not None:
        market_match = 1.0 if motivation.hometown_team_id == team_snapshot.id else 0.0

    # Round-5 critic nit 5: Decision P4-9 specifies "player_awards rows and
    # postseason results FOR THIS TEAM." `list_awards_for_player` has no
    # team filter and `PlayerAward` carries no team_id to filter by even
    # client-side, and postseason results are not read here at all --
    # this is CAREER-wide, not team-scoped, disclosed rather than
    # silently narrower than the decision names. Filtering to tenure with
    # the current team needs the same acquisition-season data `tenure_
    # seasons` above is missing on this path (P4-63); until that lands,
    # narrowing this reading would need to invent a boundary this module
    # cannot verify.
    awards = await award_repo.list_awards_for_player(ex, player_id)
    career_accolades = min(len(awards) / _TEAM_ACCOLADE_ANCHOR, 1.0)

    return PlayerSituation(
        team_success=team_success,
        role_fit=role_fit,
        minutes_share=minutes_share,
        pay_vs_market=pay_vs_market,
        tenure_seasons=tenure_seasons,
        market_match=market_match,
        team_accolades=career_accolades,
    )


# ---------------------------------------------------------------------------
# Decision P4-16 (Phase 4 Stage C step 11) -- the "agency service's own
# read model" free agency (`rank_offers`) reads through, instead of
# importing `motivation_repo` itself. The one, whole-league query this
# needs is cheap by construction (`motivation_repo.list_player_motivation_
# for_league`, one round trip, no N+1) -- a real precedent already exists
# for this exact shape (`_load_free_agency_pool`'s own separate,
# phase-specific loader in `gm_service.py`'s `run_phase_hook`).
# ---------------------------------------------------------------------------


async def load_motivation_for_league(
    ex: Executor, *, league_id: int
) -> dict[int, PlayerMotivation]:
    """Decision P4-16's retargeted P2-21 guarantee made real: this is the
    ONE place outside `agency_service.py` (`gm_service.py`'s `run_phase_
    hook`, FREE_AGENCY branch) that needs motivation data, and it calls
    this function instead of importing `dba2.data.repositories.
    motivation_repo` itself -- exactly one reader of `player_motivation`,
    one place a caching or consistency bug can live. Keyed by `player_id`;
    a player with no row yet (Decision P4-6's bootstrap hasn't reached
    them, e.g. drafted/signed after the last bootstrap pass) is simply
    absent from the returned mapping, never a `KeyError` -- every caller
    downstream already treats absence as `None`-neutral (Decision P4-9),
    the same contract `GmContext.motivation_by_player`'s own docstring
    documents."""
    rows = await motivation_repo.list_player_motivation_for_league(ex, league_id)
    return {row.player_id: row for row in rows}


async def load_ladder_transitions_for_league(
    ex: Executor, *, league_id: int, season: int
) -> list[PlayerMotivationEvent]:
    """Phase 4 Stage D step 17's read-model, extending Decision P4-16's
    "one reader of one table" pattern to media's own event-sourced intake
    (Decision P4-27): `dba2/services/media_service.py` may not import
    `motivation_repo` directly (`test_only_agency_service_imports_
    motivation_repo`), so this is the function it calls instead. One
    league-scoped, season-scoped, event-type-scoped query
    (`motivation_repo.list_motivation_events_for_league`) -- O(events),
    never a per-player N+1 loop and never a full-league motivation scan
    (the same cost argument Decision P4-27 makes for vigilance generally,
    extended to this one read)."""
    return await motivation_repo.list_motivation_events_for_league(
        ex, league_id=league_id, season=season, event_type=LADDER_TRANSITION_EVENT_TYPE
    )


async def load_trade_request_pressure_for_league(
    ex: Executor, *, league_id: int
) -> dict[int, float]:
    """Decision P4-17/P4-51 (Phase 4 Stage C step 12): the sparse
    `GmContext.trade_request_pressure_by_player` read model, the other
    half of `rank_offers`' own "agency service's own read model" pattern.
    One whole-league query (`motivation_repo.list_player_motivation_for_
    league`), then `dba2.agency.motivation.trade_request_pressure` (the
    SAME pure gate/magnitude function the harness mirror below calls)
    filters it down to the sparse "bound requests only" shape Decision
    P4-51 specifies -- every player whose request does not bind is simply
    absent from the returned mapping, never a `0.0` entry."""
    rows = await motivation_repo.list_player_motivation_for_league(ex, league_id)
    result: dict[int, float] = {}
    for row in rows:
        pressure = trade_request_pressure(
            request_state=row.request_state,
            star_leverage=row.star_leverage,
            morale=row.morale,
        )
        if pressure is not None:
            result[row.player_id] = pressure
    return result


# ---------------------------------------------------------------------------
# Decision P4-6/Stage B step 6 -- trait generation, the bootstrap writer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapResult:
    created: int
    skipped_existing: int


async def bootstrap_league_motivation(
    ex: Executor, *, league_id: int, league_rng_seed: int, season: int
) -> BootstrapResult:
    """Decision P4-6's trait generator, called for every player in the
    league who does not already have a `player_motivation` row -- the
    first rows this table has ever held in this project's history
    (verified directly against the live dev DB before this function's
    first real call: `SELECT count(*) FROM player_motivation` reads `0`
    across every league). Idempotent: a player who already has a row is
    skipped, never overwritten (trait regeneration is not this
    function's job -- `update_motivation_traits` exists separately for
    the rare, explicit drift case Decision P4-5 describes)."""
    players = await player_repo.list_players_for_league(ex, league_id)
    created = 0
    skipped = 0
    for player in players:
        existing = await motivation_repo.get_player_motivation(ex, player.id)
        if existing is not None:
            skipped += 1
            continue
        age = float(player_age(player.birth_date, season))
        traits = generate_traits(league_rng_seed, player.id, age=age, overall=player.overall_cached)
        async with transaction(ex) as conn:
            await motivation_repo.create_player_motivation(
                conn,
                player_id=player.id,
                league_id=league_id,
                weight_winning=traits.weight_winning,
                weight_role_usage=traits.weight_role_usage,
                weight_loyalty=traits.weight_loyalty,
                weight_money=traits.weight_money,
                weight_legacy=traits.weight_legacy,
                star_leverage=traits.star_leverage,
                satisfaction_winning=INITIAL_SATISFACTION,
                satisfaction_role_usage=INITIAL_SATISFACTION,
                satisfaction_loyalty=INITIAL_SATISFACTION,
                satisfaction_money=INITIAL_SATISFACTION,
                satisfaction_legacy=INITIAL_SATISFACTION,
                morale=INITIAL_MORALE,
                trust_in_management=INITIAL_TRUST,
                request_state=INITIAL_REQUEST_STATE,
                request_opened_in_season=None,
                last_evaluated_season=season,
                hometown_team_id=traits.hometown_team_id,
                market_preference=traits.market_preference,
            )
        created += 1
    return BootstrapResult(created=created, skipped_existing=skipped)


# ---------------------------------------------------------------------------
# Decision P4-7/P4-10/P4-44/P4-45, P4-14 -- Stage B steps 8/9: the
# satisfaction/morale/trust writer and the RequestState ladder.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckpointResult:
    """`players_skipped` counts the players a resumed checkpoint left alone
    because an earlier, interrupted pass had already evaluated them for this
    `(season, phase)` (phase-5-stage-e-advance.md P5-99.r.2) -- a resumed pass
    reports 250 evaluated and 200 skipped rather than pretending to a full one.
    Defaulted, so an existing construction site needs no edit."""

    players_evaluated: int
    ladder_transitions: int
    players_skipped: int = 0


async def run_agency_checkpoint(
    ex: Executor, *, league_id: int, season: int, phase: Phase
) -> CheckpointResult:
    """One of Decision P4-11's exactly-two-per-season checkpoints (trade
    deadline / offseason -- the caller decides which by the `phase` it
    passes; both call sites are Stage C's job to wire into a real
    phase-transition hook, per this module's own docstring). Builds ONE
    `LeagueSnapshot` via `load_gm_inputs` and re-evaluates every rostered
    player against it -- write-only, per Stage B's scope: nothing here
    changes what `gm_service.py` reads for a single decision this stage.

    **Local import, not module-level** (see this module's own top-of-file
    note): `gm_service.py` imports `load_motivation_for_league` (below)
    from this module at ITS OWN module level, so a module-level import
    the other way would be circular. Resolved lazily here, at the one
    place this module actually calls it, long after both modules have
    finished loading."""
    from dba2.services.gm_service import load_gm_inputs

    snapshot, ctx = await load_gm_inputs(ex, league_id=league_id, season=season)
    players = await player_repo.list_players_for_league(
        ex, league_id, roster_status=RosterStatus.ACTIVE
    )

    players_evaluated = 0
    players_skipped = 0
    ladder_transitions = 0
    for player in players:
        motivation = await motivation_repo.get_player_motivation(ex, player.id)
        if motivation is None:
            # Decision P4-6's bootstrap hasn't reached this player yet
            # (e.g. drafted/signed after the last bootstrap pass) -- a
            # checkpoint is not the trait generator's job; skip rather
            # than silently inventing traits here.
            continue
        if motivation.last_evaluated_season == season and motivation.last_checkpoint_phase == phase:
            # The per-player guard (phase-5-stage-e-advance.md P5-99.r.2): an
            # earlier pass of THIS checkpoint already folded this player, and folding
            # again would re-apply every delta, re-decay trust, and insert a second
            # set of event rows. It cannot be derived from `last_evaluated_season`
            # alone (the bootstrap pre-sets it, and a season has two checkpoints) or
            # from the event log (a quiet player writes no event row yet is still
            # decayed). One comparison, on the row this loop already read.
            players_skipped += 1
            continue

        weights = {
            MotivationAxis.WINNING: motivation.weight_winning,
            MotivationAxis.ROLE_USAGE: motivation.weight_role_usage,
            MotivationAxis.LOYALTY: motivation.weight_loyalty,
            MotivationAxis.MONEY: motivation.weight_money,
            MotivationAxis.LEGACY: motivation.weight_legacy,
        }
        current_satisfactions = {
            MotivationAxis.WINNING: motivation.satisfaction_winning,
            MotivationAxis.ROLE_USAGE: motivation.satisfaction_role_usage,
            MotivationAxis.LOYALTY: motivation.satisfaction_loyalty,
            MotivationAxis.MONEY: motivation.satisfaction_money,
            MotivationAxis.LEGACY: motivation.satisfaction_legacy,
        }

        situation = await build_player_situation(
            ex,
            league_id=league_id,
            season=season,
            snapshot=snapshot,
            ctx=ctx,
            player_id=player.id,
            motivation=motivation,
        )
        requested = evaluate_satisfaction(weights, situation)
        new_satisfactions, written = fold_satisfaction_deltas(current_satisfactions, requested)
        new_morale = compute_morale(weights, new_satisfactions)
        # Round-5 critic finding B4: the write boundary must actually call
        # the saturating fold's clamp, not just `decay_trust` -- `fold_
        # trust_deltas` is otherwise dead code in production (Decision
        # P4-45).
        new_trust, _trust_written = fold_trust_deltas(
            decay_trust(motivation.trust_in_management), ()
        )

        last_transition_season = await motivation_repo.get_last_ladder_transition_season(
            ex, player.id, event_type=LADDER_TRANSITION_EVENT_TYPE
        )
        pressure = pressure_from_morale(new_morale)
        dwell_open = dwell_is_open(
            current_season=season, last_transition_season=last_transition_season
        )
        contract_years_remaining = _years_remaining(snapshot, player.id, season)
        new_request_state, transitioned = advance_request_state(
            motivation.request_state,
            pressure=pressure,
            dwell_open=dwell_open,
            contract_years_remaining=contract_years_remaining,
        )

        # Round-5 critic finding B1: `request_opened_in_season` is
        # resolved by the pure, rung-based rule (not a per-call-site
        # string-literal reimplementation) so a de-escalation can never
        # silently overwrite or strand it -- see `resolve_request_opened_
        # in_season`'s own docstring for the three cases.
        request_opened_in_season = resolve_request_opened_in_season(
            previous_state=motivation.request_state,
            new_state=new_request_state,
            season=season,
            previous_request_opened_in_season=motivation.request_opened_in_season,
        )

        async with transaction(ex) as conn:
            for w in written:
                await motivation_repo.create_motivation_event(
                    conn,
                    league_id=league_id,
                    player_id=player.id,
                    season=season,
                    phase=phase,
                    event_type=w.event_type,
                    axis=w.axis,
                    delta=w.applied,
                    note=w.note,
                )
            if transitioned:
                ladder_transitions += 1
                axis = largest_magnitude_axis(written)
                await motivation_repo.create_motivation_event(
                    conn,
                    league_id=league_id,
                    player_id=player.id,
                    season=season,
                    phase=phase,
                    event_type=LADDER_TRANSITION_EVENT_TYPE,
                    axis=axis,
                    delta=0,
                    note=f"{motivation.request_state.value}->{new_request_state.value}",
                    # docs/design/media-v2-newsroom.md MV-2b: typed companions to
                    # `note`'s own free-text arrow -- media's event-intake layer
                    # explicitly refuses to parse that string (`dba2/media/
                    # events.py`'s own docstring), so these are the real,
                    # structured "state at trade time" reconstruction reads from.
                    # No change to `note`, ladder behavior, thresholds, dwell, or
                    # any tuned constant -- one line added to an insert that
                    # already runs.
                    from_state=motivation.request_state,
                    to_state=new_request_state,
                )
            await motivation_repo.update_motivation_state(
                conn,
                player.id,
                satisfaction_winning=new_satisfactions[MotivationAxis.WINNING],
                satisfaction_role_usage=new_satisfactions[MotivationAxis.ROLE_USAGE],
                satisfaction_loyalty=new_satisfactions[MotivationAxis.LOYALTY],
                satisfaction_money=new_satisfactions[MotivationAxis.MONEY],
                satisfaction_legacy=new_satisfactions[MotivationAxis.LEGACY],
                morale=new_morale,
                trust_in_management=new_trust,
                request_state=new_request_state,
                request_opened_in_season=request_opened_in_season,
                last_evaluated_season=season,
                last_checkpoint_phase=phase,
            )
        players_evaluated += 1

    return CheckpointResult(
        players_evaluated=players_evaluated,
        ladder_transitions=ladder_transitions,
        players_skipped=players_skipped,
    )


def _years_remaining(snapshot: LeagueSnapshot, player_id: int, season: int) -> int | None:
    _team_snapshot, roster_player = _find_team_and_roster_player(snapshot, player_id)
    if roster_player is None or roster_player.contract is None:
        return None
    return roster_player.contract.contract.end_season - season
