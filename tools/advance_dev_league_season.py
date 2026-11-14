"""CLI: advance one dev league (`tools/seed_dev_league.py`'s own output)
one season, using `tools/_gm_progression_stub.py`'s crude-on-purpose aging
pass -- the missing piece RPD-6
(docs/design/real-player-data-integration.md) needs before it can ever run
against a real player. `progress_one_season` had exactly one real caller
in this codebase before this tool, `tools/gm_harness/season.py`'s frozen
exit-gate harness, which never contains real players by construction
(RPD-4's own scoping decision, `docs/design/real-player-data-integration.md`
item 10). Without this CLI, a real-player-seeded dev league had no
season-advance mechanism reaching it at all -- RPD-6, however built, would
still have nowhere to actually run.

Usage (run with `-m` from the repo root -- same reason
`seed_dev_league.py`'s own docstring gives):

    python -m tools.advance_dev_league_season --league-id 89 --rng-seed 1

**Scope: DB-backed players/ratings/overall_cached only, no contracts.**
`seed_dev_league.py`'s own docstring is explicit that it "does NOT
generate ... contracts" -- every dev-league player's `contract` is
already `None`, so `progress_one_season`'s contract-lapse branch can
never fire for a player this tool ever touches, and this tool does not
read or write `contracts` at all. If a future caller ever seeds contracts
into a dev league, this tool's `contract=None` assumption would need
revisiting -- not silently wrong today, but worth flagging here rather
than only in a caller's head.

**What gets persisted per aged player** (one DB transaction for the
whole league -- either the season fully advances or nothing does):
1. A `PlayerRatingsHistory` snapshot of the player's PRE-progression
   ratings, tagged `RatingHistoryReason.PROGRESSION`, at the season being
   left -- so a player's sim-side history starts accumulating from the
   first time they're ever advanced, not before. (Real players' own
   *real* career history stays in `seeds/real_players/`, untouched by
   this -- this is the sim-side history the domain schema already has a
   dedicated append-only table for, `player_ratings_history`.)
2. The nudged ratings, re-tagged `RatingSource.PROGRESSION` regardless of
   what they were before (`GENERATED` or `REAL_STAT_DERIVED`) -- once
   progression has touched a player's numbers, they're no longer "as
   generated" or "as the real 2022 season," they're progressed. `derived_
   from_season`/`confidence` are carried through unchanged from the prior
   row -- those describe where the player's *starting point* came from,
   which progression doesn't change.
3. `overall_cached`, recomputed for real from the newly-persisted ratings
   via `ratings_service.recompute_overall` -- **not** trusted from the
   stub's own in-memory `new_overall`, so there is exactly one writer of
   this cache column league-wide, matching Decision 12's existing
   single-writer contract.
4. `years_pro`/`retired_in_season` (new `player_repo.set_years_pro_and_
   retirement`) and, only if either changed, `team_id`/`roster_status`
   (`player_repo.set_team_and_status`).
5. `leagues.current_season`, bumped by exactly one, read under
   `league_repo.get_phase_state_for_update`'s existing row lock so two
   concurrent advances on the same league can't race.

**RPD-6's fit/success/minutes signals, built here (not inside the pure
progression stub) from data only a DB-backed caller has**, per real
player, using this transaction's own already-simmed season:
- *Fit*: `managers/needs.py::build_need_matrix`'s `need[pos][tier] -
  glut[pos][tier]` at the player's own position/tier -- the same need/
  glut grid Phase 2's trade valuation reads, reused directly rather than
  through `need_bonus`/`fit_value` (those fold in GM-philosophy
  sensitivity for trade *pricing*, a different concern from a player's
  own growth).
- *Success*: `managers/window.py::classify_window`'s `CompetitiveWindow`
  band index, normalized -- currently backed by that function's own
  `team_strength` percentile fallback (real win/loss wiring is a
  documented, pre-existing Phase 7 gap, not something this tool fixes).
- *Minutes*: `player_season_stats.minutes_played` for the season just
  simmed (`tools/sim_dev_league_season.py`), normalized against
  `_FULL_MINUTES_ANCHOR` -- `0.0` (not skipped) for a player with no
  season-stats row yet, e.g. a league whose season was advanced without
  ever being simmed.

All three default to a neutral `0.5` if this whole enrichment step can't
run for some reason (see `_build_real_player_context`'s own docstring) --
`RealPlayerContext`'s absence is always a safe, graceful degradation
inside `_gm_progression_stub.py`, never a crash.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict as dc_asdict
from dataclasses import fields as dc_fields

from dba2.data.db import close_pool, get_pool, transaction
from dba2.data.repositories import league_repo, player_repo, ratings_repo, stats_repo, team_repo
from dba2.domain.game import GameType
from dba2.domain.motivation import PlayerMotivation
from dba2.domain.phase import Phase
from dba2.domain.player import Player, RosterStatus
from dba2.domain.ratings import PlayerRatings, RatingHistoryReason, RatingSource
from dba2.managers import capsheet
from dba2.managers.needs import build_need_matrix, tier_for_overall
from dba2.managers.snapshot import (
    RosterPlayer,
    build_league_snapshot,
    build_roster_player,
    build_team_snapshot,
)
from dba2.managers.tuning import DEFAULT_TUNING
from dba2.managers.window import CompetitiveWindow, classify_window
from dba2.services import agency_service, ratings_service
from tools._gm_progression_stub import (
    _AGED_STATUSES,
    RealPlayerContext,
    progress_one_season,
)

_NON_RATING_FIELDS = frozenset(
    {"player_id", "league_id", "source", "derived_from_season", "confidence", "updated_at"}
)

# window.py's own _BAND_ORDER, restated here rather than imported (private
# name) -- worst-to-best, used only to normalize a CompetitiveWindow into
# a 0..1 success score.
_WINDOW_ORDER = (
    CompetitiveWindow.REBUILD,
    CompetitiveWindow.RETOOL,
    CompetitiveWindow.WIN_NOW,
    CompetitiveWindow.CONTEND,
)

# Judgment calls, disclosed rather than tuned: `need`/`glut` are each
# O(1)-scaled body-count targets (needs.py's own target table tops out at
# 2.6), so a raw need-minus-glut difference rarely exceeds a couple of
# units -- _FIT_SCALE=2.0 keeps a typical difference inside [0, 1] without
# claiming any real calibration. _FULL_MINUTES_ANCHOR=2000.0 is a rough
# "significant rotation player's season total" (roughly 24 min/game across
# 82 games), not a real minutes-distribution percentile.
_FIT_SCALE = 2.0
_FULL_MINUTES_ANCHOR = 2000.0
_NEUTRAL_SCORE = 0.5


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _morale_signal(motivation: PlayerMotivation | None) -> float | None:
    """Decision P4-50's stated normalization: `player_motivation.morale`
    (`[-100, 100]`) to `RealPlayerContext.morale`'s own `[0, 1]` scale --
    `(morale + 100) / 200`, so a neutral `morale == 0` reads `0.5`. `None`
    (never `0.0`) when `motivation` itself is `None` -- a real player
    whose trait bootstrap hasn't reached them yet -- so `RealPlayerContext.
    morale`'s own `None`-is-neutral handling applies, rather than this
    function silently asserting a false neutral reading of its own."""
    if motivation is None:
        return None
    return _clamp01((motivation.morale + 100.0) / 200.0)


def _ratings_dict(ratings) -> dict[str, int]:
    """`PlayerRatings` -> the plain 57-column dict `ratings_repo`'s write
    functions expect -- the same `dataclasses.fields` pattern `tools/
    seed_dev_league.py::_create_synthetic_player` already uses for its own
    tendencies dict, reused here rather than importing `ratings_repo`'s
    private `_RATING_COLUMN_NAMES`."""
    return {
        f.name: getattr(ratings, f.name)
        for f in dc_fields(ratings)
        if f.name not in _NON_RATING_FIELDS
    }


async def _cap_settings_for_season(conn, league_id: int, season: int):
    """Real row if one exists for `season`; otherwise the league's own
    creation-season row, projected forward -- `seed_dev_league.py` only
    ever writes a cap-settings row for its own creation season
    (`league_service.create_league`), so any season advanced past that
    has none of its own yet. Same projection function `tools/gm_harness/
    league.py` already uses for future seasons, applied to a real anchor
    rather than re-deriving a second growth formula."""
    settings = await league_repo.get_cap_settings(conn, league_id, season)
    if settings is not None:
        return settings
    league = await league_repo.get_league(conn, league_id)
    anchor = await league_repo.get_cap_settings(conn, league_id, league.start_season_year)
    if anchor is None:
        raise ValueError(
            f"league {league_id} has no cap_settings row for its own creation "
            f"season {league.start_season_year} -- nothing to project forward from"
        )
    return capsheet.project_cap_settings(anchor, season, tuning=DEFAULT_TUNING)


async def _build_real_player_context(
    conn,
    league_id: int,
    season: int,
    players: list[Player],
    ratings_by_player: dict[int, PlayerRatings],
) -> dict[int, RealPlayerContext]:
    """One `RealPlayerContext` per real player (`external_ref is not
    None`) in `players` -- see module docstring's "RPD-6's fit/success/
    minutes signals" section for what each score means and where it comes
    from. Synthetic players are never given an entry (the progression
    stub doesn't consult this mapping for them at all)."""
    real_players = [p for p in players if p.external_ref is not None]
    if not real_players:
        return {}

    # Decision P4-50 (Stage C step 13b): `player_motivation.morale`,
    # read through `agency_service`'s own read model (the retargeted
    # P2-21/Decision P4-16 discipline extended here too -- one reader of
    # `motivation_repo`, even though this file is under `tools/` and the
    # mechanical layering test only scans `dba2/`) rather than importing
    # `motivation_repo` directly. A player absent from this mapping (no
    # bootstrap has reached them yet) gets `morale=None` below, which
    # `RealPlayerContext.morale`'s own docstring already treats as
    # neutral -- no special-casing needed at this call site.
    motivation_by_player = await agency_service.load_motivation_for_league(
        conn, league_id=league_id
    )

    teams = await team_repo.list_teams_for_league(conn, league_id)
    cap_settings = await _cap_settings_for_season(conn, league_id, season)

    roster_players = tuple(
        build_roster_player(p, ratings_by_player[p.id], contract=None) for p in players
    )
    by_team: dict[int, list[RosterPlayer]] = {}
    for rp in roster_players:
        if rp.player.team_id is not None:
            by_team.setdefault(rp.player.team_id, []).append(rp)

    team_snapshots = {
        team.id: build_team_snapshot(
            team, tuple(by_team.get(team.id, ())), (), cap_settings, window=None
        )
        for team in teams
    }
    league_snapshot = build_league_snapshot(league_id, season, tuple(team_snapshots.values()))

    windows = {
        team_id: classify_window(snapshot, league_snapshot, tuning=DEFAULT_TUNING)
        for team_id, snapshot in team_snapshots.items()
    }
    need_matrices = {
        team_id: build_need_matrix(snapshot, tuning=DEFAULT_TUNING)
        for team_id, snapshot in team_snapshots.items()
    }

    context_by_player: dict[int, RealPlayerContext] = {}
    for player in real_players:
        morale = _morale_signal(motivation_by_player.get(player.id))

        if player.team_id is None or player.team_id not in need_matrices:
            context_by_player[player.id] = RealPlayerContext(
                fit=_NEUTRAL_SCORE, success=_NEUTRAL_SCORE, minutes=_NEUTRAL_SCORE, morale=morale
            )
            continue

        tier = tier_for_overall(player.overall_cached)
        matrix = need_matrices[player.team_id]
        position = player.primary_position
        fit_raw = matrix.need[position][tier] - matrix.glut[position][tier]
        fit = _clamp01(0.5 + fit_raw / (2 * _FIT_SCALE))

        window = windows[player.team_id]
        success = _WINDOW_ORDER.index(window) / (len(_WINDOW_ORDER) - 1)

        season_stats = await stats_repo.get_player_season_stats(
            conn, league_id, player.id, season, GameType.REGULAR
        )
        minutes = (
            _clamp01(float(season_stats.minutes_played) / _FULL_MINUTES_ANCHOR)
            if season_stats is not None
            else 0.0
        )

        context_by_player[player.id] = RealPlayerContext(
            fit=fit, success=success, minutes=minutes, morale=morale
        )

    return context_by_player


async def advance_dev_league_season(*, league_id: int, rng_seed: int) -> tuple[int, int, int]:
    """Advances `league_id` one season. Returns `(new_season, aged_count,
    retired_count)`."""
    pool = await get_pool()

    async with transaction(pool) as conn:
        phase_state = await league_repo.get_phase_state_for_update(conn, league_id)
        if phase_state is None:
            raise ValueError(f"no league with id {league_id}")
        _current_phase, current_season = phase_state
        next_season = current_season + 1

        players = await player_repo.list_players_for_league(conn, league_id)
        all_ratings = await ratings_repo.get_for_league(conn, league_id)
        ratings_by_player = {r.player_id: r for r in all_ratings}
        missing = [p.id for p in players if p.id not in ratings_by_player]
        if missing:
            raise ValueError(f"players missing a player_ratings row: {missing}")

        roster_players: tuple[RosterPlayer, ...] = tuple(
            build_roster_player(p, ratings_by_player[p.id], contract=None) for p in players
        )

        # `run_agency_checkpoint`'s own `load_gm_inputs` call requires a
        # REAL `league_cap_settings` row for `season` ITSELF (unlike
        # `season + 1`, which it projects when missing) -- `seed_dev_
        # league.py` only ever writes one, for the league's creation
        # season, so any advance past that has none yet. **Caught by
        # actually running this tool twice in a row against a real dev
        # league before landing** (this stage's own build report): the
        # second advance raised `NotFoundError` without this fix, the
        # first never exercised it (creation season already had a real
        # row). Persisted here, not merely computed in memory (`_cap_
        # settings_for_season`'s own shape, reused for the projection) --
        # the season being LEFT should read as a real, historical fact
        # from here on, not a moving projection every future call
        # recomputes.
        if await league_repo.get_cap_settings(conn, league_id, current_season) is None:
            projected = await _cap_settings_for_season(conn, league_id, current_season)
            projected_kwargs = {
                k: v for k, v in dc_asdict(projected).items() if k not in ("league_id", "season")
            }
            await league_repo.create_cap_settings(
                conn, league_id=league_id, season=current_season, **projected_kwargs
            )

        # Decision P4-67 (Phase 4 Stage C step 13a): the agency checkpoint
        # runs BEFORE `_build_real_player_context`, in this exact order --
        # 13b's morale trim reads `player_motivation.morale` through that
        # context, and P4-67's own finding is that reading it before this
        # checkpoint has ever run reads a green-because-empty control, not
        # a real one. `Phase.PROGRESSION` is this tool's own phase (P4-11's
        # "offseason" checkpoint, concretely) -- `run_agency_checkpoint`
        # re-evaluates every ACTIVE player against a fresh `LeagueSnapshot`
        # it builds itself (`load_gm_inputs`), independent of the
        # `roster_players`/`RosterPlayer` tuple this function already
        # built above.
        await agency_service.run_agency_checkpoint(
            conn, league_id=league_id, season=current_season, phase=Phase.PROGRESSION
        )

        real_player_context = await _build_real_player_context(
            conn, league_id, current_season, players, ratings_by_player
        )
        progressed = progress_one_season(
            roster_players,
            season=next_season,
            seed=rng_seed,
            real_player_context=real_player_context,
        )

        aged = 0
        retired = 0
        for before, after in zip(roster_players, progressed, strict=True):
            if before.player.roster_status not in _AGED_STATUSES:
                continue
            aged += 1
            if after.player.roster_status is RosterStatus.RETIRED:
                retired += 1

            prior_ratings = ratings_by_player[before.player.id]
            await ratings_repo.create_ratings_history_snapshot(
                conn,
                player_id=before.player.id,
                season=current_season,
                league_id=league_id,
                reason=RatingHistoryReason.PROGRESSION,
                ratings=_ratings_dict(before.ratings),
            )
            await ratings_repo.upsert_player_ratings(
                conn,
                player_id=after.player.id,
                league_id=league_id,
                source=RatingSource.PROGRESSION,
                ratings=_ratings_dict(after.ratings),
                derived_from_season=prior_ratings.derived_from_season,
                confidence=prior_ratings.confidence,
            )
            await ratings_service.recompute_overall(conn, after.player.id)
            await player_repo.set_years_pro_and_retirement(
                conn,
                after.player.id,
                years_pro=after.player.years_pro,
                retired_in_season=after.player.retired_in_season,
            )
            if (
                after.player.team_id != before.player.team_id
                or after.player.roster_status != before.player.roster_status
            ):
                await player_repo.set_team_and_status(
                    conn, after.player.id, after.player.team_id, after.player.roster_status
                )

        await league_repo.set_current_season(conn, league_id, next_season)

    return next_season, aged, retired


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Advance one tools/seed_dev_league.py-created dev league one season, using "
            "tools/_gm_progression_stub.py's crude-on-purpose aging pass."
        )
    )
    parser.add_argument("--league-id", type=int, required=True)
    parser.add_argument(
        "--rng-seed",
        type=int,
        default=1,
        help="Drives RPD-2's variance draw for real-seeded players (a fresh, "
        "progression-scoped RNG stream -- unrelated to the league's own rng_seed).",
    )
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    try:
        next_season, aged, retired = await advance_dev_league_season(
            league_id=args.league_id, rng_seed=args.rng_seed
        )
        print(
            f"League {args.league_id} advanced to season {next_season}: "
            f"{aged} players aged, {retired} retired."
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
