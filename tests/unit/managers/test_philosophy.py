"""Tier 1: `PhilosophyProfile`/`PROFILES` -- §12 step 1's own verify list
(docs/design/phase-2-manager-minds.md): every profile field inside its
declared bound, every `loyalty` >= 1.00, and the seven enum values exactly
cover `PROFILES`'s keys."""
from __future__ import annotations

from dba2.domain.team import GmPhilosophy
from dba2.managers.philosophy import PROFILES, PhilosophyProfile

_POSITIVE_SCALARS = (
    "kappa", "upside_weight", "surplus_weight", "fit_sens", "pick_pref",
    "vet_bias", "risk", "loyalty",
)


def test_profiles_covers_exactly_the_seven_gm_philosophy_values():
    assert set(PROFILES) == set(GmPhilosophy)
    assert len(PROFILES) == 7


def test_every_loyalty_is_at_least_1_00():
    """Decision P2-11's endowment floor: round 1 left three philosophies
    at exactly 1.00 and two below it, which inverted the mechanism for
    those two (`loyalty < 1` makes a GM *more* willing to churn). Every
    philosophy must sit at or above the floor, `chaos` exactly at it."""
    for philosophy, profile in PROFILES.items():
        assert profile.loyalty >= 1.00, f"{philosophy}: loyalty {profile.loyalty} < 1.00"


def test_every_profile_scalar_is_strictly_positive():
    """No formula in §4/§5/§6/§7 is defined for a zero or negative
    `kappa`/`upside_weight`/`surplus_weight`/`fit_sens`/`pick_pref`/
    `vet_bias`/`risk`/`loyalty` -- `kappa` alone sits in `base_value`'s
    denominator, so a non-positive value is not a tuning choice, it's a
    ZeroDivisionError or a sign-flipped talent curve waiting to happen."""
    for philosophy, profile in PROFILES.items():
        for field_name in _POSITIVE_SCALARS:
            value = getattr(profile, field_name)
            assert value > 0, f"{philosophy}.{field_name} = {value}, must be > 0"


def test_defense_first_is_the_only_profile_with_a_group_tilt():
    for philosophy, profile in PROFILES.items():
        if philosophy is GmPhilosophy.DEFENSE_FIRST:
            assert dict(profile.group_tilt) == {
                "ON_BALL_DEF": 1.45, "TEAM_DEF": 1.45, "SHOOTING": 0.85, "CREATION": 0.85,
            }
        else:
            assert dict(profile.group_tilt) == {}, (
                f"{philosophy} has a non-empty group_tilt: {dict(profile.group_tilt)}"
            )


def test_kappa_ordering_matches_star_maxer_most_star_loving():
    """`kappa` -- lower = more star-loving (§6). `star_maxer` must be the
    lowest and `egalitarian` the highest of the seven."""
    kappas = {philosophy: profile.kappa for philosophy, profile in PROFILES.items()}
    assert kappas[GmPhilosophy.STAR_MAXER] == min(kappas.values())
    assert kappas[GmPhilosophy.EGALITARIAN] == max(kappas.values())


def test_philosophy_profile_is_frozen():
    profile = PROFILES[GmPhilosophy.CHAOS]
    assert isinstance(profile, PhilosophyProfile)
    try:
        profile.kappa = 1.0  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("PhilosophyProfile must be frozen")
