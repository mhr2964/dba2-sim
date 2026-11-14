"""`tools/_gm_progression_stub.py`'s own aging-correctness -- determinism,
the three age-band rating branches, retirement, and contract expiry
(Decision P2-23, docs/design/phase-2-manager-minds.md §3, §12 step 10).

The import-quarantine assertion ("nothing under `dba2/` imports this
module") lives in `tests/unit/test_layering.py::
test_nothing_under_dba2_imports_the_gm_progression_stub`, matching that
file's own AST-scanning style and the same "quarantine check vs. the
stub's own correctness are different kinds of test, in different files"
split `tests/unit/test_gm_draft_class_stub.py` already established for
its own sibling stub.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

from dba2.domain.player import RosterStatus
from dba2.managers.snapshot import RosterPlayer, build_roster_player
from dba2.sim.derivation import RATING_FIELD_ORDER
from dba2.sim.ratings import compute_overall
from tests.fixtures.managers import make_contract, make_roster_player
from tools import _gm_progression_stub as stub_module
from tools._gm_progression_stub import (
    RealPlayerContext,
    _synthetic_scalar_delta,
    progress_one_season,
)
from tools._gm_real_trajectory import _REAL_SOURCED_FIELDS

_SEASON = 2026


def _ratings_dict(rp: RosterPlayer) -> dict[str, object]:
    return dataclasses.asdict(rp.ratings)


def _synced(rp: RosterPlayer) -> RosterPlayer:
    """`tests.fixtures.managers.make_roster_player`'s own `overall_cached`
    param is an arbitrary literal, independent of the actual weighted
    mean of its generated ratings -- fine for the `managers/` valuation
    tests that fixture exists for, which never recompute it, but this
    stub *always* recomputes `overall_cached` via `compute_overall`
    (Decision 12: exactly one writer, always in sync with the ratings it
    caches). Left un-synced, every before/after monotonicity assertion
    below would be comparing a recomputed, ratings-derived number against
    an unrelated literal instead of against what a real system's
    `overall_cached` column would actually hold going in. Re-synced once,
    right after construction, so "before" already reflects that."""
    overall = compute_overall(rp.ratings, rp.player.primary_position)
    player = dataclasses.replace(rp.player, overall_cached=overall)
    return build_roster_player(player, rp.ratings, contract=rp.contract)


def _as_draft_eligible(rp: RosterPlayer) -> RosterPlayer:
    """Local mirror of `tests/fixtures/managers.py`'s own private
    `_as_draft_eligible` -- not imported directly since that name is that
    module's own internal helper for its archetype tables, not part of
    the fixtures module's public surface any other test file reads."""
    player = dataclasses.replace(rp.player, roster_status=RosterStatus.DRAFT_ELIGIBLE, team_id=None)
    return build_roster_player(player, rp.ratings, contract=None)


def _as_retired(rp: RosterPlayer) -> RosterPlayer:
    player = dataclasses.replace(
        rp.player, roster_status=RosterStatus.RETIRED, retired_in_season=_SEASON - 1, team_id=None
    )
    return build_roster_player(player, rp.ratings, contract=None)


def _as_real_player(rp: RosterPlayer, external_ref: str) -> RosterPlayer:
    """RPD-6 tuning pass (2026-09-12): marks a fixture as real-seeded
    (`Player.external_ref is not None`) without touching anything else --
    same "rewrite one field via `dataclasses.replace`" shape every other
    `_as_*` helper in this file already uses."""
    player = dataclasses.replace(rp.player, external_ref=external_ref)
    return build_roster_player(player, rp.ratings, contract=rp.contract)


class _ZeroGauss:
    """Stand-in for `rng_for`'s return value -- a real RNG's `.gauss` draw
    would make the real-trajectory branch's exact output non-deterministic
    from the test's point of view; forcing it to 0.0 isolates the clamp/
    multiplier math the tests below actually check. RPD-2's real variance
    behavior itself is out of scope here -- HANDOFF.md already records it
    as verified separately."""

    def gauss(self, mu: float, sigma: float) -> float:
        return 0.0


def _as_status(rp: RosterPlayer, status: RosterStatus) -> RosterPlayer:
    """Rewrites a `make_roster_player`-built `RosterPlayer` to `roster_
    status = status`, keeping `team_id`/`ratings`/`contract` as built --
    `make_roster_player` itself has no `roster_status` override, matching
    `_as_draft_eligible`/`_as_retired` above (B3 item 3: exercising
    `FREE_AGENT`/`TWO_WAY`/`INJURED`, the three `_AGED_STATUSES` members no
    existing fixture ever constructs)."""
    player = dataclasses.replace(rp.player, roster_status=status)
    return build_roster_player(player, rp.ratings, contract=rp.contract)


def test_deterministic_same_seed_and_season():
    rp = make_roster_player(
        1, age=25, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
        contract=make_contract(1, start_season=_SEASON - 1, salaries=(1_000_000, 2_000_000)),
    )
    a = progress_one_season((rp,), season=_SEASON, seed=1)
    b = progress_one_season((rp,), season=_SEASON, seed=1)

    assert dataclasses.asdict(a[0].player) == dataclasses.asdict(b[0].player)
    assert _ratings_dict(a[0]) == _ratings_dict(b[0])
    assert (a[0].contract is None) == (b[0].contract is None)


def test_growth_phase_moves_overall_toward_potential_and_never_decreases():
    rp = _synced(make_roster_player(
        2, age=22, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
        ratings_overrides={"potential": 85, "potential_variance": 80},
    ))
    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    assert result.player.overall_cached >= rp.player.overall_cached
    assert result.player.overall_cached > rp.player.overall_cached


def test_decline_phase_strictly_decreases_overall():
    rp = _synced(make_roster_player(
        3, age=35, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
    ))
    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    assert result.player.overall_cached < rp.player.overall_cached


def test_prime_window_ratings_unchanged_exactly():
    rp = _synced(make_roster_player(
        4, age=29, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
    ))
    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    assert _ratings_dict(result) == _ratings_dict(rp)
    assert result.player.overall_cached == rp.player.overall_cached


def test_draft_eligible_prospect_passes_through_unchanged():
    prospect = _as_draft_eligible(
        make_roster_player(
            5, age=19, season=_SEASON, overall_cached=55, peak_age_start=26, peak_age_end=30
        )
    )
    result = progress_one_season((prospect,), season=_SEASON, seed=1)[0]

    assert result is prospect
    assert result.player.years_pro == prospect.player.years_pro


def test_already_retired_player_passes_through_unchanged():
    retiree = _as_retired(
        make_roster_player(
            6, age=45, season=_SEASON, overall_cached=50, peak_age_start=27, peak_age_end=31
        )
    )
    result = progress_one_season((retiree,), season=_SEASON, seed=1)[0]

    assert result is retiree
    assert result.player.years_pro == retiree.player.years_pro


def test_player_turning_41_retires_and_expires_an_active_contract():
    rp = make_roster_player(
        7, age=41, season=_SEASON, overall_cached=65, peak_age_start=27, peak_age_end=31,
        contract=make_contract(7, start_season=_SEASON, salaries=(1_000_000, 2_000_000, 3_000_000)),
    )
    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    assert result.player.roster_status is RosterStatus.RETIRED
    assert result.player.retired_in_season == _SEASON
    assert result.contract is None
    assert result.player.team_id is None


def test_contract_expiring_before_season_becomes_a_free_agent():
    rp = make_roster_player(
        8, age=28, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
        contract=make_contract(
            8, start_season=_SEASON - 4, salaries=(1_000_000, 2_000_000, 3_000_000)
        ),
    )
    assert rp.contract.contract.end_season < _SEASON  # sanity check on the fixture itself

    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    assert result.contract is None
    assert result.player.roster_status is RosterStatus.FREE_AGENT
    assert result.player.team_id is None


def test_contract_still_running_is_left_untouched():
    rp = make_roster_player(
        9, age=28, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
        contract=make_contract(9, start_season=_SEASON, salaries=(1_000_000, 2_000_000, 3_000_000)),
    )
    assert rp.contract.contract.end_season >= _SEASON  # sanity check on the fixture itself

    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    assert result.contract is not None
    assert result.contract.contract.status is rp.contract.contract.status
    assert result.player.roster_status is rp.player.roster_status
    assert result.player.team_id == rp.player.team_id


_CLAMP_TEST_HIGH_FIELDS: tuple[str, ...] = (
    "speed", "acceleration", "lateral_quickness", "strength", "vertical",
    "stamina", "durability", "handle", "ball_security", "first_step",
)


def test_growth_clamps_at_the_99_ceiling():
    """B2 fix (critic-round): the prior fixture used a literal
    `overall_cached=98`, never synced to the fixture's actual generated
    ratings via `_synced` -- `delta = 0.25 * (potential(99) -
    overall_cached(98)) = 0.25`, which the nudge's integer step floors to
    a zero-field move, so the clamp assertion passed vacuously (critic
    confirmed: removing the ceiling clamp entirely, or raising it to 999,
    both still passed this test unchanged).

    Rebuilt through `_synced()` with ten fields pinned near the ceiling
    (94) and every other public field pinned low (20), so the synced
    `overall_cached` lands low (30, verified by hand) against
    `potential=99` -- `delta = 0.25 * (99 - 30) = 17.25`, big enough that
    all ten high-pinned fields (94 + 17.25 = 111.25 pre-clamp) overflow
    past 99 without the clamp, matching critic's own repro in shape (its
    delta=14.25/10-fields example)."""
    overrides = {
        field_name: 94 if field_name in _CLAMP_TEST_HIGH_FIELDS else 20
        for field_name in RATING_FIELD_ORDER
        if field_name not in ("potential", "potential_variance")
    }
    overrides["potential"] = 99
    overrides["potential_variance"] = 90
    rp = _synced(make_roster_player(
        16, age=20, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
        ratings_overrides=overrides,
    ))
    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    for field_name in _CLAMP_TEST_HIGH_FIELDS:
        assert getattr(result.ratings, field_name) == 99, (
            f"{field_name} should have clamped at the 99 ceiling"
        )
    for field_name in RATING_FIELD_ORDER:
        assert 0 <= getattr(result.ratings, field_name) <= 99


def test_decline_clamps_at_the_0_floor():
    rp = make_roster_player(
        11, age=40, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
        ratings_overrides={"speed": 1},
    )
    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    assert result.ratings.speed == 0
    for field_name in RATING_FIELD_ORDER:
        assert 0 <= getattr(result.ratings, field_name) <= 99


def test_years_pro_increments_only_for_aged_players():
    aged = make_roster_player(
        12, age=25, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
    )
    prospect = _as_draft_eligible(
        make_roster_player(
            13, age=19, season=_SEASON, overall_cached=55, peak_age_start=26, peak_age_end=30
        )
    )
    retiree = _as_retired(
        make_roster_player(
            14, age=45, season=_SEASON, overall_cached=50, peak_age_start=27, peak_age_end=31
        )
    )

    results = progress_one_season((aged, prospect, retiree), season=_SEASON, seed=1)

    assert results[0].player.years_pro == aged.player.years_pro + 1
    assert results[1].player.years_pro == prospect.player.years_pro
    assert results[2].player.years_pro == retiree.player.years_pro


def test_retiring_player_with_no_contract_still_clears_team_id():
    """B1 fix (critic-round, blocking): a retiring player with `contract
    = None` used to keep a dangling `team_id` -- `team_id = None` was
    nested inside `if contract_lapsed:`, which itself requires `contract
    is not None`. `tools/seed_dev_league.py` seeds every player `ACTIVE`
    + `team_id` set + `contract=None` (it deliberately never generates
    contracts), so this was the common case this harness actually
    produces, not a rare edge case. The module's own docstring already
    said retirement clears roster/team state "unconditionally" -- the
    code just didn't match it."""
    rp = make_roster_player(
        15, age=45, season=_SEASON, overall_cached=60, peak_age_start=27, peak_age_end=31,
        contract=None,
    )
    assert rp.player.team_id is not None  # sanity check on the fixture itself
    assert rp.contract is None  # sanity check: the no-contract case this bug hit

    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    assert result.player.team_id is None
    assert result.player.roster_status is RosterStatus.RETIRED
    assert result.player.retired_in_season == _SEASON
    assert result.contract is None


def test_potential_and_potential_variance_are_byte_unchanged_by_a_nudge():
    """B3 item 1: no existing test asserts `potential`/`potential_variance`
    survive a *real* (non-no-op) nudge untouched -- both are the target the
    nudge moves other fields *toward*, not state the nudge itself moves
    (module docstring's "Rating nudge mechanics" section). Uses the growth
    phase (a real, non-zero delta) so this is not the same no-op case
    `test_prime_window_ratings_unchanged_exactly` already covers."""
    rp = _synced(make_roster_player(
        17, age=22, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
        ratings_overrides={"potential": 90, "potential_variance": 80},
    ))
    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    assert result.ratings.potential == rp.ratings.potential
    assert result.ratings.potential_variance == rp.ratings.potential_variance
    # Sanity check the nudge is real, not accidentally a no-op too.
    assert result.ratings.speed != rp.ratings.speed


def test_retiring_player_past_peak_age_end_is_not_nudged():
    """B3 item 2: a player old enough to retire (age > 40) is, by
    construction, also always past `peak_age_end` -- nothing before this
    test constructs that combination and confirms the retirement branch's
    "skips the nudge, not the aging" rule actually wins over the decline
    branch, rather than a retiring player quietly getting declined one
    last time before being marked `RETIRED`."""
    rp = make_roster_player(
        18, age=41, season=_SEASON, overall_cached=65, peak_age_start=27, peak_age_end=31,
    )
    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    assert result.ratings == rp.ratings
    assert result.player.overall_cached == rp.player.overall_cached
    assert result.player.roster_status is RosterStatus.RETIRED


def test_free_agent_two_way_and_injured_all_get_aged():
    """B3 item 3: `_AGED_STATUSES` names `FREE_AGENT`/`TWO_WAY`/`INJURED`
    alongside `ACTIVE`, but no existing test constructs any of the three
    -- a mutation that silently dropped one of them from the aged scope
    (treating it like `DRAFT_ELIGIBLE`, a pass-through) survived the full
    suite. Confirms each of the three is actually aged (years_pro
    increments, ratings move) rather than skipped."""
    for status in (RosterStatus.FREE_AGENT, RosterStatus.TWO_WAY, RosterStatus.INJURED):
        rp = _as_status(
            _synced(make_roster_player(
                19, age=22, season=_SEASON, overall_cached=70,
                peak_age_start=27, peak_age_end=31,
                ratings_overrides={"potential": 90, "potential_variance": 80},
            )),
            status,
        )
        result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

        assert result.player.years_pro == rp.player.years_pro + 1, status
        assert result.ratings.speed != rp.ratings.speed, status
        assert result.player.overall_cached > rp.player.overall_cached, status


def test_prime_window_boundary_ages_are_no_ops():
    """B3 item 4: the prime window is a *closed* interval
    `[peak_age_start, peak_age_end]` (`if age < peak_age_start ... elif
    age > peak_age_end`, both strict), but no existing test uses either
    boundary age exactly -- only ages strictly inside/outside the range.
    Confirms both endpoints are no-ops (delta == 0.0), not growth/decline,
    which a `<=`/`>=` off-by-one at either boundary would silently flip."""
    rp_start = _synced(make_roster_player(
        20, age=27, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
        ratings_overrides={"potential": 99},
    ))
    rp_end = _synced(make_roster_player(
        21, age=31, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
    ))

    result_start = progress_one_season((rp_start,), season=_SEASON, seed=1)[0]
    result_end = progress_one_season((rp_end,), season=_SEASON, seed=1)[0]

    assert result_start.ratings == rp_start.ratings
    assert result_start.player.overall_cached == rp_start.player.overall_cached
    assert result_end.ratings == rp_end.ratings
    assert result_end.player.overall_cached == rp_end.player.overall_cached


def test_growth_floor_clamps_a_negative_raw_delta_to_zero():
    """B3 item 5: `potential`'s own generator has no age term
    (`scouting.py`'s Gap-E docstring, quoted in the module docstring's
    "Growth floored at 0.0" section) -- a prospect's drawn `potential` can
    land below their drawn `overall`, which would otherwise back a
    negative "growth" step for a player still below `peak_age_start`.
    Confirms `max(0.0, ...)` floors that raw negative delta to 0 (no
    movement) rather than letting it go negative inside the growth
    branch. Built through `_synced()` first, then `potential` overridden
    below the synced `overall_cached` (same "sync first, override after"
    order the module docstring's target scenario requires)."""
    synced = _synced(make_roster_player(
        22, age=20, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
    ))
    assert synced.player.overall_cached > 10  # sanity check before the override below
    rp = build_roster_player(
        synced.player,
        dataclasses.replace(synced.ratings, potential=10, potential_variance=10),
        contract=synced.contract,
    )
    assert rp.ratings.potential < rp.player.overall_cached  # the edge case this pins

    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    assert result.ratings == rp.ratings
    assert result.player.overall_cached == rp.player.overall_cached


def test_noop_prime_season_does_not_alter_ratings_or_overall_cached_value():
    """B3 item 6: a genuine no-op season (delta == 0.0, prime years) must
    not recompute the *value* of `overall_cached` or touch `ratings.
    updated_at` -- no existing test pins either explicitly for a
    prime-years player (`test_prime_window_ratings_unchanged_exactly`
    only checks the full ratings dict and `overall_cached`, not this
    field in isolation).

    `player.overall_computed_at`/`player.updated_at` are deliberately
    *not* pinned as unchanged here, despite the fix-round dispatch note
    that named them alongside the two fields above: the module's own
    docstring is explicit that these two *are* meant to bump to `aged_at`
    on every call, no-op or not ("keeps the Player-level
    overall_computed_at timestamp honest about 'this player was assessed
    this season' even when the assessment reconfirms the same number") --
    confirmed by hand against the running code, which bumps both on this
    exact fixture. Asserting they stay unchanged would pin a false
    behavior against the file's own documented, deliberate design;
    flagged for the next critic pass rather than silently resolved either
    way."""
    rp = _synced(make_roster_player(
        23, age=29, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
    ))
    result = progress_one_season((rp,), season=_SEASON, seed=1)[0]

    assert result.player.overall_cached == rp.player.overall_cached
    assert result.ratings.updated_at == rp.ratings.updated_at
    # Documents the actual (intentional) bump behavior -- see docstring above.
    assert result.player.overall_computed_at == datetime(_SEASON, 6, 15, tzinfo=UTC)
    assert result.player.updated_at == datetime(_SEASON, 6, 15, tzinfo=UTC)


def test_real_player_splits_fields_between_trajectory_and_synthetic_scalar(monkeypatch):
    """RPD-6 tuning pass (2026-09-12): closes a real, previously-existing
    gap -- no test in this file (or anywhere: no `test_gm_real_trajectory.
    py` exists) ever exercised `Player.external_ref is not None`'s branch
    at all. Confirms the split this pass introduced: `_REAL_SOURCED_FIELDS`
    move by the (clamped, multiplier-scaled) real-trajectory delta, and the
    remaining nudge-eligible fields move by the exact same synthetic
    scalar a non-real player at this age/potential/overall would get --
    not by the real-trajectory mechanic, which `_gm_real_trajectory.py` no
    longer reports a delta for on those fields at all."""
    monkeypatch.setattr(stub_module, "rng_for", lambda *args, **kwargs: _ZeroGauss())
    real_delta = 5.0  # inside the +/-8.0 clamp, so the clamp is a no-op here
    monkeypatch.setattr(
        stub_module,
        "expected_delta_at_age",
        lambda external_ref, age, **_: dict.fromkeys(_REAL_SOURCED_FIELDS, real_delta),
    )

    rp = _as_real_player(
        _synced(make_roster_player(
            30, age=22, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
            ratings_overrides={"potential": 90, "potential_variance": 80},
        )),
        external_ref="123",
    )
    # fit=success=minutes=1.0 -> combined=1.0 -> multiplier = _REAL_FIT_MULTIPLIER_HI exactly.
    context = RealPlayerContext(fit=1.0, success=1.0, minutes=1.0)

    result = progress_one_season(
        (rp,), season=_SEASON, seed=1, real_player_context={rp.player.id: context}
    )[0]

    real_move = real_delta * stub_module._REAL_FIT_MULTIPLIER_HI
    for field_name in _REAL_SOURCED_FIELDS:
        before = getattr(rp.ratings, field_name)
        expected = round(min(99.0, before + real_move))
        assert getattr(result.ratings, field_name) == expected, field_name

    synthetic_delta = _synthetic_scalar_delta(
        rp.ratings.potential,
        rp.player.overall_cached,
        22,
        rp.ratings.peak_age_start,
        rp.ratings.peak_age_end,
    )
    assert synthetic_delta > 0.0  # sanity: this fixture is in the growth phase
    for field_name in stub_module._SYNTHETIC_FIELDS_FOR_REAL_PLAYERS:
        before = getattr(rp.ratings, field_name)
        expected = round(min(99.0, before + synthetic_delta))
        assert getattr(result.ratings, field_name) == expected, field_name


def test_morale_trim_moves_real_trajectory_deltas_by_the_documented_formula(monkeypatch):
    """Decision P4-50 (Stage C step 13b): the morale trim is a SEPARATE
    multiplicative factor on top of the fit/success/minutes multiplier,
    verified against the documented formula directly (`morale_trim = 1 +
    MORALE_TRIM_SPAN * (2 * morale_signal - 1)`), not just "the number
    moved." A happy player (`morale=1.0`) gets a strictly LARGER real-
    trajectory move than a neutral one (`morale=0.5`), which in turn gets
    a strictly larger move than a miserable one (`morale=0.0`) -- and all
    three are computed against the same fit/success/minutes multiplier
    (`fit=success=minutes=1.0`), isolating the morale term."""
    monkeypatch.setattr(stub_module, "rng_for", lambda *args, **kwargs: _ZeroGauss())
    real_delta = 5.0
    monkeypatch.setattr(
        stub_module,
        "expected_delta_at_age",
        lambda external_ref, age, **_: dict.fromkeys(_REAL_SOURCED_FIELDS, real_delta),
    )
    field_name = next(iter(_REAL_SOURCED_FIELDS))
    rp = _as_real_player(
        _synced(make_roster_player(
            31, age=22, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
            # `field_name` pinned well below the 99 ceiling (real headroom
            # for the largest possible move here, 5.0 * 1.5 * 1.2 = 9.0) --
            # a hand-verified critic-round finding: this test originally
            # let `field_name` take whatever value `make_roster_player`
            # happened to generate at slot 31, which landed close enough
            # to 99 that BOTH the miserable and neutral trims clamped to
            # the identical ceiling value, making the strict-ordering
            # assertion below fail on a clamping collision that had
            # nothing to do with the morale mechanism itself.
            ratings_overrides={"potential": 90, "potential_variance": 80, field_name: 40},
        )),
        external_ref="124",
    )
    base_multiplier = stub_module._REAL_FIT_MULTIPLIER_HI  # fit=success=minutes=1.0
    before = getattr(rp.ratings, field_name)

    def _moved_field(morale: float | None) -> float:
        context = RealPlayerContext(fit=1.0, success=1.0, minutes=1.0, morale=morale)
        result = progress_one_season(
            (rp,), season=_SEASON, seed=1, real_player_context={rp.player.id: context}
        )[0]
        return getattr(result.ratings, field_name) - before

    happy_move = _moved_field(1.0)
    neutral_move = _moved_field(0.5)
    absent_move = _moved_field(None)  # None must read identically to 0.5, per P4-9's convention
    miserable_move = _moved_field(0.0)

    span = stub_module.MORALE_TRIM_SPAN
    assert span > 0.0, "MORALE_TRIM_SPAN must be non-zero for this test to discriminate anything"

    def _expected(trim: float) -> float:
        # Mirrors `_real_trajectory_nudge`'s own arithmetic exactly
        # (`round(before + clamp(delta) * multiplier * trim)`, no
        # variance since `rng_for` is monkeypatched to `_ZeroGauss`) --
        # `round(delta * multiplier)` alone is NOT generally equal to
        # `round(before + delta * multiplier) - before`, since Python's
        # round-half-to-even depends on `before`'s own parity too.
        return round(min(99.0, before + real_delta * base_multiplier * trim)) - before

    assert neutral_move == _expected(1.0)
    assert absent_move == _expected(1.0)
    assert happy_move == _expected(1.0 + span)
    assert miserable_move == _expected(1.0 - span)
    assert miserable_move < neutral_move < happy_move


def test_morale_trim_span_zero_is_a_real_control_run(monkeypatch):
    """Decision P4-50's own targeted integration check, verified directly:
    with `MORALE_TRIM_SPAN` monkeypatched to `0.0` (Decision P4-34's
    zero-mechanism control), rating deltas are IDENTICAL regardless of
    morale -- happy, neutral and miserable all collapse to the same
    fit/success/minutes-only move. This is what makes `MORALE_TRIM_SPAN =
    0.0` a real control run rather than a claim: the mechanism has a
    verified off-switch, not just a documented one."""
    monkeypatch.setattr(stub_module, "rng_for", lambda *args, **kwargs: _ZeroGauss())
    monkeypatch.setattr(stub_module, "MORALE_TRIM_SPAN", 0.0)
    real_delta = 5.0
    monkeypatch.setattr(
        stub_module,
        "expected_delta_at_age",
        lambda external_ref, age, **_: dict.fromkeys(_REAL_SOURCED_FIELDS, real_delta),
    )
    rp = _as_real_player(
        _synced(make_roster_player(
            32, age=22, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
            ratings_overrides={"potential": 90, "potential_variance": 80},
        )),
        external_ref="125",
    )

    def _moved_field(morale: float | None) -> float:
        context = RealPlayerContext(fit=1.0, success=1.0, minutes=1.0, morale=morale)
        result = progress_one_season(
            (rp,), season=_SEASON, seed=1, real_player_context={rp.player.id: context}
        )[0]
        field_name = next(iter(_REAL_SOURCED_FIELDS))
        return getattr(result.ratings, field_name) - getattr(rp.ratings, field_name)

    assert _moved_field(1.0) == _moved_field(0.5) == _moved_field(0.0) == _moved_field(None)


def test_real_player_without_bracketing_trajectory_falls_back_to_full_synthetic(monkeypatch):
    """RPD-3's "not a hard cap": once a real player's trajectory data
    doesn't bracket their current age, every field -- including
    `_REAL_SOURCED_FIELDS` -- falls back to the ordinary synthetic scalar,
    identically to a non-real player built from the same ratings/age/
    potential. Same base `RosterPlayer` used for both sides (only
    `external_ref` differs) so `make_ratings`' per-player-id distinct
    values can't make the comparison look identical by coincidence."""
    monkeypatch.setattr(stub_module, "expected_delta_at_age", lambda external_ref, age, **_: None)

    base_rp = _synced(make_roster_player(
        31, age=22, season=_SEASON, overall_cached=70, peak_age_start=27, peak_age_end=31,
        ratings_overrides={"potential": 90, "potential_variance": 80},
    ))
    real_rp = _as_real_player(base_rp, external_ref="999")

    real_result = progress_one_season((real_rp,), season=_SEASON, seed=1)[0]
    non_real_result = progress_one_season((base_rp,), season=_SEASON, seed=1)[0]

    assert _ratings_dict(real_result) == _ratings_dict(non_real_result)
    assert real_result.player.overall_cached == non_real_result.player.overall_cached
