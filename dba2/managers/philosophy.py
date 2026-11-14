"""The seven `PhilosophyProfile` records plus the `defense_first` group-tilt
vector (docs/design/phase-2-manager-minds.md §6, §12 step 1).

Decision P2-11: "a philosophy is exactly one frozen `PhilosophyProfile` of
eight scalars plus one optional group-tilt vector, and nothing else. There
is no per-philosophy code path." `chaos` is a multiplier (its `risk` is
the highest in the table, plus the seeded jitter §6 describes -- applied
by `managers/decisions/trade.py`, not here); `defense_first` is a tilt
vector, not a second value model.

Zero I/O, per Decision P2-2's purity extension: no `dba2.data`, no
`datetime.now()`, no bare `random.Random()`, no `open()`, no
`os.environ`. Imports `domain` (for `GmPhilosophy`) only.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from dba2.domain.team import GmPhilosophy

# `defense_first`'s tilt is applied over `sim/ratings.py`'s
# `_OVERALL_WEIGHTING_GROUPS` (Decision P2-9's own careful distinction:
# NOT `sim/derivation.py::RATING_GROUPS` -- the two agree everywhere
# except `post_defense`, which `_OVERALL_WEIGHTING_GROUPS` moves to
# TEAM_DEF). The four group names below are members of both tables, so
# the tilt is unambiguous regardless of which one a reader checks it
# against.
_DEFENSE_FIRST_TILT: Mapping[str, float] = MappingProxyType(
    {"ON_BALL_DEF": 1.45, "TEAM_DEF": 1.45, "SHOOTING": 0.85, "CREATION": 0.85}
)

# The no-op tilt every other philosophy uses -- an empty mapping, read by
# the (not-yet-built) tilted `compute_overall` entry point as "every
# group's multiplier defaults to 1.0," per §6's "`group_tilt` is a
# `Mapping[str, float]` defaulting to all-1.0."
_NO_TILT: Mapping[str, float] = MappingProxyType({})


@dataclass(frozen=True)
class PhilosophyProfile:
    """§6's eight-scalar shape, verbatim. Every GM in the league is
    exactly one of these -- no subclassing, no per-philosophy branch
    anywhere downstream."""

    kappa: float
    """Star-curve convexity; lower = more star-loving (§4.2, §6)."""
    upside_weight: float
    """How much unrealized potential counts (§4.3.1, §6)."""
    surplus_weight: float
    """How much contract efficiency counts (§4.4, §6)."""
    fit_sens: float
    """How much positional need moves value (§5, §6)."""
    pick_pref: float
    """Multiplier on every asset priced off an implied rookie contract --
    an unsigned pick or a `draft_eligible` prospect (Decision P2-34,
    §4.6, §6)."""
    vet_bias: float
    """Flattens (>1) or steepens (<1) the age curve above 29 only (§6)."""
    risk: float
    """Appetite for the upside tail, applied through the scouted peak
    (§4.7, §6)."""
    loyalty: float
    """Endowment premium on players already on the roster -- floored at
    1.00 league-wide (Decision P2-11); every value below is >= 1.00."""
    group_tilt: Mapping[str, float] = field(default_factory=lambda: _NO_TILT)
    """Per-group `_OVERALL_WEIGHTING_GROUPS` multiplier, defaulting to
    all-1.0 (empty mapping = no tilt). Only `defense_first` sets this."""


PROFILES: Mapping[GmPhilosophy, PhilosophyProfile] = MappingProxyType(
    {
        GmPhilosophy.STAR_MAXER: PhilosophyProfile(
            kappa=6.0, upside_weight=0.55, surplus_weight=0.50, fit_sens=0.60,
            pick_pref=0.80, vet_bias=1.00, risk=1.20, loyalty=1.02,
        ),
        GmPhilosophy.EGALITARIAN: PhilosophyProfile(
            kappa=10.0, upside_weight=0.50, surplus_weight=1.20, fit_sens=1.30,
            pick_pref=1.10, vet_bias=1.00, risk=0.85, loyalty=1.10,
        ),
        GmPhilosophy.DEFENSE_FIRST: PhilosophyProfile(
            kappa=7.5, upside_weight=0.50, surplus_weight=1.00, fit_sens=1.15,
            pick_pref=1.00, vet_bias=1.00, risk=0.95, loyalty=1.06,
            group_tilt=_DEFENSE_FIRST_TILT,
        ),
        GmPhilosophy.TENDENCY_RESPECTER: PhilosophyProfile(
            kappa=7.5, upside_weight=0.50, surplus_weight=1.00, fit_sens=1.00,
            pick_pref=1.00, vet_bias=1.00, risk=1.00, loyalty=1.06,
        ),
        GmPhilosophy.VET_OVERRATER: PhilosophyProfile(
            kappa=7.0, upside_weight=0.15, surplus_weight=0.70, fit_sens=0.90,
            pick_pref=0.75, vet_bias=1.35, risk=0.90, loyalty=1.15,
        ),
        GmPhilosophy.YOUTH_DEVELOPER: PhilosophyProfile(
            kappa=8.5, upside_weight=0.85, surplus_weight=1.10, fit_sens=0.95,
            pick_pref=1.25, vet_bias=0.70, risk=1.15, loyalty=1.05,
        ),
        GmPhilosophy.CHAOS: PhilosophyProfile(
            kappa=7.5, upside_weight=0.50, surplus_weight=1.00, fit_sens=1.00,
            pick_pref=1.00, vet_bias=1.00, risk=1.60, loyalty=1.00,
        ),
    }
)
assert set(PROFILES) == set(GmPhilosophy), (
    "PROFILES must cover exactly the seven GmPhilosophy values"
)
assert all(profile.loyalty >= 1.00 for profile in PROFILES.values()), (
    "Decision P2-11's endowment floor: every philosophy's loyalty must be >= 1.00"
)
