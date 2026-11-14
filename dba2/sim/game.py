"""Game loop: periods, clock, dead-ball substitution, fatigue/foul
feedback, overtime -> `GameResult` (phase-1-simulation-engine.md §5, §8.1,
Decision P1-17/P1-18/P1-19, §12 step 5).

Zero I/O, per Decision P1-1. `simulate_game`'s `rng_seed` must be the
persisted `games.rng_seed` (Decision P1-21) -- this module derives every
possession's `random.Random` via `core.rng.rng_for(rng_seed,
possession_index)` and never constructs one itself.

**Interpretive calls made in this dispatch, flagged in the build report:**

1. **Minutes targets** (Decision P1-19 item 1, retuned per
   `docs/design/auto-gm-role-construction.md` RC-4/RC-16): a player with a
   `player_roles` row uses `touch_share_f` directly as their minutes
   share; a player without one reads a raw minutes value off
   `_MINUTES_CURVE`, a 15-entry table indexed by **absolute** rank
   (1-indexed) within their own team's *available* roster -- ranked by
   `(-overall_cached, id)` over every player on the team, role-carrying
   players included, so a role-carrying player still consumes a rank slot
   even though his own share comes from `touch_share_f` rather than the
   curve (RC-16: this is what makes a roleless player's rank reflect his
   real position on a mixed roster instead of his position among other
   roleless players alone). Both groups' raw shares are normalized
   together to sum to 1.0 per team. Every `SimTeam.players` entry is
   treated as available for the game (no active-roster trim to 12-15) --
   that selection is a `game_sim_service`/roster-service concern this
   dispatch doesn't build (step 8).
2. **Substitution granularity**: every possession boundary is treated as
   a dead-ball substitution opportunity (Decision P1-19 item 1: "dead
   balls"), rather than only the real subset of terminal kinds that are
   actually dead balls in NBA rules (a make is *not* a dead ball if the
   defense doesn't call timeout, for instance). Each period, the 5
   eligible (non-disqualified) players furthest behind their own
   period-scaled minutes target are selected for the next possession --
   a greedy balancer that guarantees the Tier-2 identity "team minutes
   sum to `240 + 25*overtime_periods`" by construction (exactly 5 players
   per team are on court every elapsed second), without needing to model
   real dead-ball timing precisely.
3. **`PERIOD_EXPIRED` (Decision P1-14's 8th closed-set member) is never
   produced by this dispatch's `game.py`.** A possession in progress when
   the period clock reaches 0 is allowed to finish naturally (its own
   terminal kind stands), and the *next* possession waits for the next
   period; interrupting a possession mid-chain at the exact buzzer would
   require plumbing a second termination trigger through every beat call
   in `possession.py` for a rare, mostly cosmetic case. Flagged as a
   known gap rather than silently working around Decision P1-14's own
   closed set.

**B4 fix (critic-proven, this fix round): fatigue, foul-trouble-aware
rotation, and pace are wired end to end.** `_update_fatigue` (below)
depletes/recovers `state.PlayerGameState.fatigue` once per possession, for
every player on both rosters -- on-court players deplete (by seconds
played plus a `BeatRecord.actor_ids`-derived beat-participation count, the
documented proxy for "intensity" described in `tuning.FatigueTuning`'s
docstring), bench players recover -- and `possession.py` reads the live
pool back via `game_state.player(id).fatigue` when it calls
`resolve_on_ball_beat`. `_select_lineup`'s ratio-based balancer now
inflates a foul-trouble player's played/target ratio (see
`tuning.PossessionTuning.foul_trouble_bench_multiplier`), making them less
likely to be one of the 5 players selected for the next possession.
`offense_team.strategy.pace_target` is passed into `simulate_possession`
as `offense_pace_target`, which scales per-beat seconds costs (Decision
P1-17) -- this also gives `GameState.lineup` (`LineupState`, previously
constructed every possession and never read by anything, per critic's own
finding) a real reader: `_update_fatigue` reads it to know who's on court
vs. on the bench this possession. Heat/streak (Decision P1-20's other
named piece) is explicitly deferred, not built this round -- flagged here
rather than silently dropped a second time.

`offense_team.strategy.three_point_bias` is passed into `simulate_
possession` the same way (docs/design/gm-command-surface.md PD-2, build
step 7) -- the first commit that makes this column load-bearing for every
team in every league, not just a team with a player directive set (2.4:
previously a shipped, validated, persisted, re-derived, rendered knob no
engine file read at all).

`offense_team.strategy.offensive_scheme` is passed into `simulate_
possession` the same way (PD-10, build step 10) -- the last dead
`team_strategy` field (2.4) closes here. Unlike `three_point_bias`'s
default-50-is-neutral shape, `offensive_scheme` is never optional on a
real `TeamStrategy` row, so this changes sim output at a fixed seed and
fixed roster for every team in every league, not only ones with a
non-default value (PD-10.7's `TUNING_VERSION` bump note).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from dba2.core.rng import rng_for
from dba2.sim.boxscore import PossessionRecord, accumulate_game
from dba2.sim.context import GameContext, SimPlayer, SimTeam
from dba2.sim.events import BeatRecord, GameResult, TerminalKind, TerminalOutcome, Zone
from dba2.sim.possession import simulate_possession
from dba2.sim.probability import clamp
from dba2.sim.state import GameState, LineupState
from dba2.sim.tuning import TuningProfile

# Decision P1-19 item 1's anchor minutes (out of a 240 team-minute
# regulation game) for the no-role-row rank curve.
#
# Replaced per docs/design/auto-gm-role-construction.md RC-4 (revision 2,
# 2026-09-23; two critic rounds, the second an independent 1,230-game-season
# instrumentation run confirming the numbers below): the flat 5/3/rest tier
# split (31.0/22.0/3.5, §10.1/Decision S7-1b) gave every player within a
# tier *identical* minutes -- ranks 9-14 were all indistinguishable at 3.97
# realized mpg, and the tier model's one uncorrectable defect (5 players/
# team over 30 mpg vs. a real ~2.5) is structural: a flat-within-tier model
# always gives all 5 starters the same number. `_MINUTES_CURVE` is a
# 15-entry table of raw minutes read directly by **absolute** rank
# (1-indexed), not interpolated and not normalized-rank (RC-4.1: the three
# harness bands -- `starter_mpg`/`rotation_bench_mpg`/`deep_bench_mpg` --
# are themselves defined on fixed absolute rank positions,
# `tools/validate_sim.py:559-561`'s `team_mpg[:5]`/`[5:8]`/`[8:]`, so keying
# the curve on normalized rank re-maps every player's position when the
# roster size changes and converts a small drift into a band breach --
# proven live in RC-4.1's revision-1 N-sweep). Values are "minutes at
# N=14" and sum to exactly 240 over the first 14 entries; ranks 1-5 descend
# in steps of 1.2, then 1.1, 1.1, 1.1 (the `share_players_over_30mpg`
# deliverable), ranks 6-8 in steeper steps of 1.8 each (the
# `p10_ppg_rotation` composition lever,
# RC-4.4 -- steepening this segment is the fastest way to breach that
# band), and rank 9 is a deliberate cliff (19.2 -> 9.5) that keeps the
# rotation pool at exactly 8 players/team across every legal roster size
# (RC-4.3, proven N=10-15). Ranks beyond the table (illegal today --
# `seeds/cap_settings.json` caps `roster_max` at 15 -- but `_minutes_shares`
# must stay a total function) decay geometrically at `_MINUTES_CURVE_TAIL_
# DECAY` from the last entry, per RC-4.2. This change bumps `TUNING_VERSION`
# (§10.3 of phase-1-step-7-calibration.md, Decision S7-1c): it changes sim
# output at a fixed seed and fixed roster, which is what `engine_version`
# exists to flag, even though this table lives here rather than in
# `tuning.py`.
#
# Ranks 1-5 re-anchored a second time, per docs/design/per-36-spread-
# compression.md SC-7 Commit 4 (M1, 2026-09-23). SC-2.4's original 36.0
# anchor assumed (a) a real mean-of-team-leaders of 35.5 (working estimate,
# never sourced) and (b) a realization delta of -0.45 mpg at rank 1. Both
# assumptions were superseded by real measurements before this commit:
# the real 2025 mean of all 30 teams' own per-team minutes leaders is
# **34.70** (`seeds/nba_reference_distributions.json`'s `minutes_leader_mpg`
# entry, sourced from all 30 teams' real per-player stats -- not 35.5), and
# SC-8.1 measurement 3 (this doc's own Commits 1-2 As-built) measured the
# rank-1 realization delta at the *shipped* 33.2 anchor as **-0.70**, not
# -0.45 -- outside the -0.30..-0.60 precondition window, which per that
# measurement's own decision rule means "the anchor must be re-solved," not
# assumed. Re-solved empirically per SC-8.3's ladder, not by formula: 36.0
# was tried first (SC-2.4's own top rung) and measured `top_scorer_ppg`
# 39.27 on a 1-season screening run -- over the design's own <=37.5
# ladder-trigger and over the [25.6, 38.4] band ceiling outright, so per
# the ladder's own rule ("never widen the band") the anchor stepped down,
# not the band. **35.5 (the ladder's second rung) is what shipped**,
# confirmed on a 3-season/3,690-game run: realized rank-1
# (`minutes_leader_mpg`) **34.5955** against the real **34.70** target (a
# -0.10 delta, well inside the [32.62, 36.78] band), and `top_scorer_ppg`
# **36.04** against the [25.6, 38.4] band -- 6.1% headroom, *more* than
# SC-2.4's original 4.7% estimate for the 36.0 anchor it never got to ship,
# because the measured realization delta at 35.5 (-0.90 mpg) is smaller
# than the measured delta at 36.0 (-1.08 mpg, 1-season screening figure) --
# the delta is not anchor-invariant, which is exactly why this was measured
# per-candidate rather than solved by the original formula alone. Ranks 2-5
# were rescaled from SC-2.4's own 36.0-anchor shape
# (`36.0/32.6/30.4/28.6/27.0`) by a single linear scale factor on each
# rank's deviation from the 30.92 mean (`k = (35.5-30.92)/(36.0-30.92) =
# 0.9016`), which preserves the original curve's descending-step shape and
# its sum-to-154.6/mean-30.92 identity by construction, rather than
# re-deriving a new shape from scratch. 35.0 (the ladder's third rung) was
# screened (1-season) but not needed -- 35.5 already clears the ladder's
# own stopping condition (`top_scorer_ppg <= 37.5`) with room. Ranks 6-15
# unchanged. Full real numbers (before/after every gated band, golden-seed
# churn, the collateral OT-seed and rare-shape fixture re-anchors): this
# doc's own As-built note.
#
# Re-verified against the same doc's Commit 3 (the assist branch tilt,
# `849a4e7`), which landed concurrently with this build and was not yet
# committed when the 35.5 anchor above was chosen. Commit 3's own As-built
# note reported a hand-spliced approximation of this composition regressing
# `minutes_leader_mpg` to 32.51 (a PASS -> FAIL flip) -- since resolved by
# an independent critic as a contaminated A/B in that note's own hand
# splice (its "after" run never actually carried this anchor's still-
# uncommitted edit), not a real Commit-3 interaction: Commit 3 alone moves
# this metric by +0.02-0.03%, noise-scale. A direct, isolated-worktree
# measurement of the real, both-committed code confirms 34.5801, still
# PASS, matching the pre-Commit-3 34.5955 to within noise. See the As-built
# note for the full account; no anchor change was needed.
#
# Re-measured a third time at `cbc10e7`, after Commit 3's forward-fix
# (`3a8f703`) re-centered its tilt: `minutes_leader_mpg` 34.5914,
# `top_scorer_ppg` 35.1382 -- no anchor change needed.
_MINUTES_CURVE: tuple[float, ...] = (
    35.5,  # rank 1 -- SC-7 Commit 4 (M1), re-anchored from 33.2 -> 36.0 -> 35.5
    32.4,  # rank 2
    30.5,  # rank 3
    28.8,  # rank 4
    27.4,  # rank 5 -- ranks 1-5 sum 154.6, mean 30.92 ("starter_mpg"), unchanged
    22.8,  # rank 6
    21.0,  # rank 7
    19.2,  # rank 8 -- ranks 6-8 sum 63.0, mean 21.00 ("rotation_bench_mpg")
    9.5,  # rank 9 -- the rotation-pool cliff (RC-4.3)
    5.4,  # rank 10
    3.3,  # rank 11
    1.8,  # rank 12
    1.4,  # rank 13
    1.0,  # rank 14
    0.8,  # rank 15 -- ranks 9-15 (14 players' worth, 9-14) mean 3.733 ("deep_bench_mpg")
)
_MINUTES_CURVE_TAIL_DECAY = 0.8


def _minutes_curve_raw(rank: int) -> float:
    """`rank` is 1-indexed absolute rank. RC-4.2: ranks beyond the table's
    15 entries (not a legal roster size today, but `_minutes_shares` must
    stay a total function over any `len(team.players)`) decay geometrically
    from the last entry at `_MINUTES_CURVE_TAIL_DECAY`, which keeps strict
    monotonicity without introducing meaningful mass."""
    if rank <= len(_MINUTES_CURVE):
        return _MINUTES_CURVE[rank - 1]
    overflow = rank - len(_MINUTES_CURVE)
    return _MINUTES_CURVE[-1] * (_MINUTES_CURVE_TAIL_DECAY**overflow)


def _minutes_shares(team: SimTeam) -> dict[int, float]:
    """Decision P1-19 item 1. Returns each player's share of the team's
    total minutes (sums to 1.0 across `team.players`).

    RC-16 fix (`docs/design/auto-gm-role-construction.md`): the whole
    roster is ranked once, by `(-overall_cached, id)`, role-carrying
    players included. A role-carrying player keeps his own written
    `touch_share_f` but still consumes a rank slot in that ordering, so a
    roleless player's curve index reflects his position on the *actual*
    roster, not his position among other roleless players alone -- a team
    with 13 role rows and one roleless player used to hand that player
    `without_role[0]`, the rank-1 (starter) anchor, regardless of his real
    quality. On an all-roleless team (every team in production today,
    since nothing yet writes `player_roles` rows) this ordering is
    bit-identical to ranking the roleless subset alone, so the fix is a
    no-op there and only changes behavior on a mixed roster."""
    if len(team.players) < 5:
        raise ValueError(f"team {team.id} has fewer than 5 available players")
    ranked = sorted(team.players, key=lambda p: (-p.player.overall_cached, p.id))
    shares: dict[int, float] = {}
    for i, p in enumerate(ranked):
        if p.touch_share_f is not None:
            # RC-21 (docs/design/auto-gm-role-construction.md): this used to
            # be `max(p.touch_share_f, 0.01)`. The 0.01 floor (2.4 mpg) was
            # never load-bearing at its one real consumer, `_select_lineup.
            # _ratio` below -- a zero (or negative) share produces a
            # `target <= 0`, which that function already short-circuits to
            # `float("inf")` before dividing, and a zero-total team is
            # already handled below. The floor's only real effect was
            # flattening `_MINUTES_CURVE`'s own ranks 12-14 (0.0075/0.0058/
            # 0.0042, all under 0.01) into one identical value whenever a
            # written role happened to land there, and making a literal 0.0
            # -- the only way `/gm role`'s `minutes=0.0` ("does not play")
            # can be expressed -- unreachable. `share if share > 0.0 else
            # 0.0` is deliberately a comparison, not `max`, for a fourth
            # reason beyond restoring that semantic: `max(nan, 0.0)` returns
            # `nan` (Python's `max` returns its first argument when the
            # comparison is False), which would poison this team's
            # normalizer below and then every `_ratio` sort key in the
            # game; `nan > 0.0` is `False`, so the comparison form maps a
            # `NUMERIC` `'NaN'` (reachable at the column, even though no
            # writer in this codebase produces one) to a DNP instead.
            share = p.touch_share_f
            shares[p.id] = share if share > 0.0 else 0.0
        else:
            shares[p.id] = _minutes_curve_raw(i + 1) / 240.0
    total = sum(shares.values())
    if total <= 0:  # pragma: no cover -- unreachable, every share is positive by construction
        even = 1.0 / len(team.players)
        return {p.id: even for p in team.players}
    return {pid: v / total for pid, v in shares.items()}


def _select_lineup(
    team: SimTeam,
    *,
    target_period_seconds: Mapping[int, float],
    period_seconds_played: Mapping[int, float],
    game_state: GameState,
    tuning: TuningProfile,
) -> tuple[SimPlayer, ...]:
    """The 5 eligible players furthest behind their own period-scaled
    minutes target (see module docstring, interpretive call 2).

    B4 fix (critic-proven): a player in foul trouble (§4.6: "feeds the
    rotation model," `state.PlayerGameState.is_in_foul_trouble`) has their
    ratio inflated by `tuning.possession.foul_trouble_bench_multiplier`
    before the sort below -- making them look further ahead of their
    minutes target than they actually are, so they are less likely to be
    among the 5 players furthest behind it. This is "more likely to sit,"
    not "cannot play": a team with 4 non-foul-trouble players at a
    position still plays the 5th regardless of this nudge, since `_ratio`
    only reorders `eligible`, it never removes anyone from it."""
    eligible = [p for p in team.players if not game_state.player(p.id).is_disqualified(tuning)]
    if len(eligible) < 5:
        eligible = list(team.players)  # documented gap: see module docstring

    def _ratio(p: SimPlayer) -> tuple[float, int]:
        target = target_period_seconds.get(p.id, 0.0)
        played = period_seconds_played.get(p.id, 0.0)
        if target <= 0:
            return (float("inf"), p.id)
        ratio = played / target
        if game_state.player(p.id).is_in_foul_trouble(period=game_state.period):
            ratio *= tuning.possession.foul_trouble_bench_multiplier
        return (ratio, p.id)

    chosen = sorted(eligible, key=_ratio)[:5]
    return tuple(sorted(chosen, key=lambda p: p.id))


def _beat_participation_counts(beat_log: Sequence[BeatRecord]) -> dict[int, int]:
    """How many times each player is named as a beat actor across one
    possession's beat log -- the documented proxy for "beat participation
    intensity" `tuning.FatigueTuning`'s docstring describes (a ball-handler
    in a `pnr_ball` is an actor in setup, screen, on-ball, help, rotation,
    and resolution; a weak-side shooter who never touches the ball is
    named far less often).

    Critic-proven fix (block-defender-attribution follow-up, this dispatch):
    `789df46` widened `RoleSlot.RIM_PROTECTOR` (`playbook.py`) onto every
    action type's defense list, sourced via `composites.best_rim_protector`
    rather than the matchup-derived 1:1 mapping every other defensive role
    slot uses -- so it frequently names a player who is ALREADY named under
    a different role-slot key in that same `setup` beat's `actor_ids`
    (measured live on a real game, seed 777: 174/295 setup beats, 59%,
    carried a duplicate id across keys). `record.actor_ids` is keyed by
    role slot (`"defense:screen_def"`, `"defense:rim_protector"`, ...)
    specifically so other consumers (`presentation/broadcast.py`,
    `services/view_service.py`) can address a specific role's player even
    when two roles happen to name the same person -- so the fix belongs
    here, at the one consumer that wants "was this player involved in this
    beat at all" (a per-beat, per-player question), not at the dict's
    write site in `beats/setup.py`, which would collapse role-slot keys
    other consumers still need distinct. Deduplicating by player id WITHIN
    each beat (not globally) means a player who legitimately acts in
    multiple different beats of the same possession (e.g. a ball-handler in
    both `setup` and `on_ball`) still accrues multiple counts -- only a
    same-beat, same-player double-count from colliding role-slot keys is
    prevented. This dedup is unconditional on which role slots collide, so
    it also protects against any *future* role-slot change reintroducing
    the same defect class, not just this specific `RIM_PROTECTOR` case."""
    counts: dict[int, int] = {}
    for record in beat_log:
        for player_id in set(record.actor_ids.values()):
            counts[player_id] = counts.get(player_id, 0) + 1
    return counts


def _update_fatigue(
    game_state: GameState,
    *,
    all_players: Mapping[int, SimPlayer],
    on_court_ids: frozenset[int],
    beat_participation: Mapping[int, int],
    credited_seconds: float,
    tuning: TuningProfile,
) -> None:
    """B4 fix (critic-proven): Decision P1-20's fatigue pool -- "depleted
    by seconds played and by beat participation intensity ... recovering
    on the bench, with `stamina` setting both the pool size and the
    recovery rate" -- was declared in `state.py` but nothing ever wrote to
    it. Called once per possession by `simulate_game`'s own loop, for
    every player on both rosters (not just this possession's 10 on-court
    players), so bench recovery actually accrues across the whole game."""
    cfg = tuning.fatigue
    for player_id, player in all_players.items():
        state = game_state.player(player_id)
        stamina = clamp(player.ratings.stamina / 99.0, 0.0, 1.0)
        if player_id in on_court_ids:
            depletion_scale = cfg.stamina_depletion_hi - stamina * (
                cfg.stamina_depletion_hi - cfg.stamina_depletion_lo
            )
            depletion = credited_seconds * cfg.seconds_depletion_per_second * depletion_scale
            depletion += (
                beat_participation.get(player_id, 0)
                * cfg.beat_participation_depletion
                * depletion_scale
            )
            state.fatigue = clamp(state.fatigue + depletion, 0.0, 1.0)
            state.seconds_played += credited_seconds
        else:
            recovery_scale = cfg.stamina_recovery_lo + stamina * (
                cfg.stamina_recovery_hi - cfg.stamina_recovery_lo
            )
            recovery = credited_seconds * cfg.recovery_per_second * recovery_scale
            state.fatigue = clamp(state.fatigue - recovery, 0.0, 1.0)


def _points_for(outcome: TerminalOutcome) -> int:
    points = outcome.ftm
    if outcome.kind is TerminalKind.MADE_FG:
        points += 3 if outcome.zone is Zone.THREE else 2
    return points


def _possession_points(events: list[TerminalOutcome]) -> int:
    return sum(_points_for(e) for e in events)


def simulate_game(context: GameContext, *, rng_seed: int) -> GameResult:
    """§5's game loop: 4x12-minute periods + 5-minute overtime repeated
    until the tie breaks (Decision P1-18), calling `possession.py` once
    per possession and accumulating the result via `boxscore.py`."""
    tuning = context.tuning
    home_shares = _minutes_shares(context.home)
    away_shares = _minutes_shares(context.away)
    # B4 fix: every player on both rosters, keyed by id, so `_update_fatigue`
    # can deplete on-court players and recover bench players every
    # possession without rebuilding this map each time.
    all_players: dict[int, SimPlayer] = {
        p.id: p for p in (*context.home.players, *context.away.players)
    }

    game_state = GameState()
    possession_log: list[PossessionRecord] = []
    beat_log: list[BeatRecord] = []
    period_scores: list[tuple[int, int, int]] = []
    possession_index = 0
    period = 1

    while True:
        game_state.period = period
        period_length = (
            tuning.possession.period_seconds if period <= 4 else tuning.possession.overtime_seconds
        )
        game_state.clock_seconds = period_length
        game_state.reset_team_fouls_for_new_period()
        home_period_seconds: dict[int, float] = {}
        away_period_seconds: dict[int, float] = {}
        home_target = {pid: share * period_length for pid, share in home_shares.items()}
        away_target = {pid: share * period_length for pid, share in away_shares.items()}
        period_start_home, period_start_away = game_state.home_score, game_state.away_score

        while game_state.clock_seconds > 0:
            offense_is_home = possession_index % 2 == 0
            home_lineup = _select_lineup(
                context.home,
                target_period_seconds=home_target,
                period_seconds_played=home_period_seconds,
                game_state=game_state,
                tuning=tuning,
            )
            away_lineup = _select_lineup(
                context.away,
                target_period_seconds=away_target,
                period_seconds_played=away_period_seconds,
                game_state=game_state,
                tuning=tuning,
            )
            game_state.lineup = LineupState(
                home_on_court=frozenset(p.id for p in home_lineup),
                away_on_court=frozenset(p.id for p in away_lineup),
            )
            offense_lineup, defense_lineup = (
                (home_lineup, away_lineup) if offense_is_home else (away_lineup, home_lineup)
            )
            offense_team = context.home if offense_is_home else context.away
            defense_team = context.away if offense_is_home else context.home

            rng = rng_for(rng_seed, possession_index)
            sim_result = simulate_possession(
                offense_lineup,
                defense_lineup,
                tuning=tuning,
                rng=rng,
                game_state=game_state,
                home_score=game_state.home_score,
                away_score=game_state.away_score,
                offense_is_home=offense_is_home,
                defense_help_aggression=defense_team.strategy.help_aggression,
                defense_defensive_scheme=defense_team.strategy.defensive_scheme,
                crash_oreb_bias=offense_team.strategy.crash_oreb_bias,
                offense_pace_target=offense_team.strategy.pace_target,
                three_point_bias=offense_team.strategy.three_point_bias,
                offensive_scheme=offense_team.strategy.offensive_scheme,
            )
            possession_index += 1
            beat_log.extend(sim_result.beat_log)

            # Credited (box-score / period-clock) time is clamped to
            # whatever remained on the period clock, even though the
            # possession itself (already fully resolved above) may have
            # taken slightly longer -- this is what makes "team minutes
            # sum to exactly 240 + 25*overtime_periods" (§9's Tier-2
            # identity) true by construction rather than an
            # approximation that drifts by a few seconds per period.
            remaining_before = game_state.clock_seconds
            credited = min(sim_result.elapsed_seconds, remaining_before)
            game_state.clock_seconds = max(0.0, remaining_before - sim_result.elapsed_seconds)
            for p in home_lineup:
                home_period_seconds[p.id] = home_period_seconds.get(p.id, 0.0) + credited
            for p in away_lineup:
                away_period_seconds[p.id] = away_period_seconds.get(p.id, 0.0) + credited

            # B4 fix: depletes the 10 on-court players' fatigue pools and
            # recovers everyone else's, once per possession -- see
            # `_update_fatigue`'s own docstring and module docstring's B4
            # paragraph. `game_state.lineup` was set above and is read
            # here for the first time in this module.
            _update_fatigue(
                game_state,
                all_players=all_players,
                on_court_ids=game_state.lineup.home_on_court | game_state.lineup.away_on_court,
                beat_participation=_beat_participation_counts(sim_result.beat_log),
                credited_seconds=credited,
                tuning=tuning,
            )

            events = [sim_result.result.terminal, *sim_result.result.credit_events]
            pts = _possession_points(events)
            if offense_is_home:
                game_state.home_score += pts
            else:
                game_state.away_score += pts

            possession_log.append(
                PossessionRecord(
                    offense_team_id=offense_team.id,
                    defense_team_id=defense_team.id,
                    offense_lineup=tuple(p.id for p in offense_lineup),
                    defense_lineup=tuple(p.id for p in defense_lineup),
                    elapsed_seconds=credited,
                    result=sim_result.result,
                )
            )

        period_scores.append(
            (
                period,
                game_state.home_score - period_start_home,
                game_state.away_score - period_start_away,
            )
        )

        if period >= 4 and game_state.home_score != game_state.away_score:
            break
        period += 1

    overtime_periods = max(0, period - 4)

    player_team: dict[int, int] = {p.id: context.home.id for p in context.home.players}
    player_team.update({p.id: context.away.id for p in context.away.players})
    player_lines, team_lines = accumulate_game(
        possession_log,
        player_team=player_team,
        home_team_id=context.home.id,
        away_team_id=context.away.id,
    )

    return GameResult(
        home_team_id=context.home.id,
        away_team_id=context.away.id,
        home_score=game_state.home_score,
        away_score=game_state.away_score,
        period_scores=tuple(period_scores),
        overtime_periods=overtime_periods,
        player_box_lines=player_lines,
        team_box_lines=team_lines,
        beat_log=tuple(beat_log),
    )
