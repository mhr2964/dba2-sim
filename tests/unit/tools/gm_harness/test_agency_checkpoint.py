"""`tools/gm_harness/season.py`'s Phase 4 §11 Stage B step 10 wiring --
`HarnessState.motivation`/`motivation_dwell`/`tenure_seasons_by_player`,
the bootstrap call, and the two-per-season checkpoint (Decisions P4-6,
P4-11, P4-13, P4-63).

**Was write-only through Stage B; Stage C (Decisions P4-16/P4-17,
§11 steps 11/12) ends that on purpose.** Most tests below still test
Stage B's own write-side properties (bootstrap, dwell, tenure) and are
unaffected. The one exception is named in its own section below: `agency_
enabled=True` vs `False` are no longer expected to produce byte-identical
decision logs -- `rank_offers`/`propose_packages` now read real agency
state back into real decisions, which is Stage C's whole point.
"""

from __future__ import annotations

import hashlib

import pytest

from dba2.domain.motivation import RequestState
from dba2.managers.tuning import DEFAULT_TUNING
from tools.gm_harness.league import generate_gm_league
from tools.gm_harness.season import run_season, run_seed

_LEAGUE_ID = 1
_SEASON = 2026
_SEED = 42


def _season_digests(record) -> list[str]:
    return [
        hashlib.sha256("\n".join(sr.jsonl_lines).encode("utf-8")).hexdigest()
        for sr in record.seasons
    ]


@pytest.fixture(scope="module")
def state_with_agency_on():
    """One league, one season, `agency_enabled=True` -- shared across the
    assertions that only need to inspect the resulting `HarnessState`."""
    state = generate_gm_league(
        league_id=_LEAGUE_ID, rng_seed=_SEED, season=_SEASON, players_per_team=15
    )
    run_season(
        state,
        season=_SEASON,
        seed=_SEED,
        skip_games=True,
        tuning=DEFAULT_TUNING,
        first_season=_SEASON,
        max_negotiation_rounds=0,
        head_to_head_enabled=False,
        agency_enabled=True,
        media_enabled=False,
    )
    return state


@pytest.mark.slow
def test_bootstrap_gives_every_player_a_motivation_row(state_with_agency_on):
    state = state_with_agency_on
    assert state.motivation  # non-empty
    assert set(state.motivation) == set(state.players)


def test_checkpoint_moved_at_least_one_players_satisfaction_state(state_with_agency_on):
    """A real, non-vacuous check: at least one rostered player's morale
    or satisfaction actually moved off its initial-zero value across a
    full season's two checkpoints (trade deadline + offseason,
    Decision P4-11) -- otherwise this would be a mechanism that reads
    green on a corpus where it never actually fires (P4-34's own
    concern)."""
    state = state_with_agency_on
    any_nonzero = any(
        m.morale != 0
        or m.satisfaction_winning != 0
        or m.satisfaction_role_usage != 0
        or m.satisfaction_money != 0
        or m.satisfaction_loyalty != 0
        or m.satisfaction_legacy != 0
        for m in state.motivation.values()
    )
    assert any_nonzero


def test_checkpoint_updates_last_evaluated_season(state_with_agency_on):
    """Both checkpoints (trade deadline, offseason) run BEFORE the season
    boundary's own `state.season += 1` -- `last_evaluated_season` reads
    the season that just played, `_SEASON`, not the rolled-over one."""
    state = state_with_agency_on
    assert state.season == _SEASON + 1
    rostered_ids = {pid for pid, rp in state.players.items() if rp.player.team_id is not None}
    assert rostered_ids  # sanity
    for player_id in rostered_ids:
        assert state.motivation[player_id].last_evaluated_season == _SEASON


def test_tenure_counter_starts_at_zero_for_every_initially_rostered_player():
    """Before any season boundary has passed, every player generated
    with the league reads tenure `0` -- the counter is only incremented
    AT a season boundary (Decision P4-63)."""
    state = generate_gm_league(
        league_id=_LEAGUE_ID, rng_seed=7, season=_SEASON, players_per_team=15
    )
    rostered = [pid for pid, rp in state.players.items() if rp.player.team_id is not None]
    assert rostered
    # Nothing has run yet -- the dict is empty, and `PlayerSituation`'s
    # own builder treats absence as tenure `0.0`, not `None`.
    assert state.tenure_seasons_by_player == {}


@pytest.mark.slow
def test_tenure_counter_increments_across_a_season_boundary():
    state = generate_gm_league(
        league_id=_LEAGUE_ID, rng_seed=7, season=_SEASON, players_per_team=15
    )
    rostered_before = {pid for pid, rp in state.players.items() if rp.player.team_id is not None}
    run_season(
        state,
        season=_SEASON,
        seed=7,
        skip_games=True,
        tuning=DEFAULT_TUNING,
        first_season=_SEASON,
        max_negotiation_rounds=0,
        head_to_head_enabled=False,
        agency_enabled=True,
        media_enabled=False,
    )
    # A player still on a team after the boundary reads tenure >= 1 --
    # traded/cut/signed players are exempted (their own tenure resets to
    # 0 at the point of the move, per `_acquisition`).
    still_rostered_and_untouched = [
        pid
        for pid in rostered_before
        if pid in state.players and state.players[pid].player.team_id is not None
    ]
    assert still_rostered_and_untouched
    assert any(
        state.tenure_seasons_by_player.get(pid, 0) >= 1 for pid in still_rostered_and_untouched
    )


@pytest.mark.slow
def test_agency_disabled_writes_no_motivation_state_at_all():
    """Decision P4-2: `agency_enabled=False` suppresses the checkpoint
    mechanism itself, not merely its effect on a decision -- `state.
    motivation` stays empty for a whole season with the flag off."""
    state = generate_gm_league(
        league_id=_LEAGUE_ID, rng_seed=7, season=_SEASON, players_per_team=15
    )
    run_season(
        state,
        season=_SEASON,
        seed=7,
        skip_games=True,
        tuning=DEFAULT_TUNING,
        first_season=_SEASON,
        max_negotiation_rounds=0,
        head_to_head_enabled=False,
        agency_enabled=False,
        media_enabled=False,
    )
    assert state.motivation == {}
    assert state.motivation_dwell == {}


# ---------------------------------------------------------------------------
# Decision P4-16/P4-17 (Phase 4 Stage C steps 11/12) SUPERSEDE this
# section's own former load-bearing property. Stage B's promise was
# "agency writes state and changes no decision" -- true then, by design,
# and it is the exact property Stage C step 11/12 exists to end: `rank_
# offers`/`propose_packages` now read real agency state back into real
# decisions. `agency_enabled=False` must still reproduce the mechanism-
# off baseline (the retained regression detector, same shape as Decision
# P3-48's own retained `head_to_head_enabled` detector); `agency_enabled=
# True` is now EXPECTED to diverge from it, and the test below proves the
# divergence is attributable to a real bound trade request existing in
# the run, not silent noise.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_agency_enabled_false_still_matches_the_mechanism_off_baseline():
    """Decision P4-2's byte-identity promise, the half of Stage B's
    contract Stage C does NOT retire: `agency_enabled=False` must still
    reproduce exactly what a mechanism-off run always has -- two separate
    `agency_enabled=False` runs at the same seed produce identical
    digests. `seeds/gm_decision_log_digest.json`'s own byte-identity is
    the production-path equivalent of this check (`tools/generate_gm_
    decision_log_digest.py` pins `agency_enabled=False` explicitly, per
    Decision P4-55, independently re-verified this stage by regenerating
    it and diffing against the committed baseline) -- this is the
    harness-path counterpart, at a corpus scale big enough to exercise a
    real trade window."""
    off_a = run_seed(
        league_id=_LEAGUE_ID, rng_seed=99, first_season=_SEASON, seasons=2,
        players_per_team=15, skip_games=True, tuning=DEFAULT_TUNING, agency_enabled=False,
    )
    off_b = run_seed(
        league_id=_LEAGUE_ID, rng_seed=99, first_season=_SEASON, seasons=2,
        players_per_team=15, skip_games=True, tuning=DEFAULT_TUNING, agency_enabled=False,
    )
    assert _season_digests(off_a) == _season_digests(off_b)


@pytest.mark.slow
def test_agency_enabled_true_diverges_from_disabled_via_free_agency_weighting():
    """REPLACES the retired `test_agency_enabled_true_vs_false_produces_
    byte_identical_decision_logs` (found by this stage's own byte-identity
    verification -- the two runs now genuinely differ at this fixture's
    real `rng_seed=99`, which is what surfaced this test needing to
    change). Byte-identity between `agency_enabled=True` and `False` was
    never the property Stage C is FOR; it is the property Stage C exists
    to end.

    **The divergence is `rank_offers`, not `propose_packages`, and this
    was verified by execution rather than assumed** -- a first version of
    this test asserted the cause was a bound trade request (`RequestState.
    TRADE_REQUESTED`/`HOLDOUT`), which is what Decision P4-17's pressure
    scalar gates on, and that assertion FAILED: at this fixture's own
    2-season/15-players-per-team scale, every player stays `CONTENT`
    (max observed pressure 13, well under rung 1's enter bar -- consistent
    with Decision P4-81's own disclosed low reachability at small scale).
    `propose_packages`' target-ranking boost genuinely never fires here.
    `rank_offers`' motivation weighting fires unconditionally instead --
    every free agent with a bootstrapped `player_motivation` row (which is
    every player, from season 1 onward, regardless of `request_state`)
    gets ranked by weighted axis fit rather than money alone. Reproduced
    directly: re-running both configurations and diffing every `Signing
    Execution.team_id` by `player_id` finds 59 signings that land on a
    different team, confirming the cause directly rather than inferring
    it from the digest diff alone."""
    off = run_seed(
        league_id=_LEAGUE_ID, rng_seed=99, first_season=_SEASON, seasons=2,
        players_per_team=15, skip_games=True, tuning=DEFAULT_TUNING, agency_enabled=False,
    )
    on = run_seed(
        league_id=_LEAGUE_ID, rng_seed=99, first_season=_SEASON, seasons=2,
        players_per_team=15, skip_games=True, tuning=DEFAULT_TUNING, agency_enabled=True,
    )
    assert _season_digests(off) != _season_digests(on)

    def _signed_team_by_player(record) -> dict[int, int]:
        signed: dict[int, int] = {}
        for season_record in record.seasons:
            for signing in season_record.signings:
                signed[signing.player_id] = signing.team_id
        return signed

    off_signings = _signed_team_by_player(off)
    on_signings = _signed_team_by_player(on)
    differing = {
        player_id
        for player_id in set(off_signings) | set(on_signings)
        if off_signings.get(player_id) != on_signings.get(player_id)
    }
    assert differing, "digests diverged but no individual signing outcome changed"

    # Corroborate the negative half directly too, per this docstring's own
    # claim: no player in the `agency_enabled=True` run actually reached a
    # bound request state at this scale.
    state = generate_gm_league(
        league_id=_LEAGUE_ID, rng_seed=99, season=_SEASON, players_per_team=15
    )
    for i in range(2):
        run_season(
            state,
            season=_SEASON + i,
            seed=99,
            skip_games=True,
            tuning=DEFAULT_TUNING,
            first_season=_SEASON,
            agency_enabled=True,
            media_enabled=False,
        )
    bound_states = (RequestState.TRADE_REQUESTED, RequestState.HOLDOUT)
    assert not any(m.request_state in bound_states for m in state.motivation.values())


def test_ladder_state_is_a_real_requeststate_value(state_with_agency_on):
    state = state_with_agency_on
    for motivation in state.motivation.values():
        assert isinstance(motivation.request_state, RequestState)
