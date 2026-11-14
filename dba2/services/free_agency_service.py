"""Free-agent offer construction and ranking -- the I/O half of §9.3
(docs/design/phase-2-manager-minds.md §9.3, §12 step 9), delegating every
formula to the pure `managers.decisions.signing` module.

**Scope boundary, stated because it is a real one.** Classifying a team's
`CompetitiveWindow` (`managers.window.classify_window`) needs a full
`LeagueSnapshot` -- the 30-team roster join that is `services/gm_service.
py`'s job (not built this dispatch, out of this dispatch's scope).
`construct_offer_for_team` below therefore takes an already-classified
`window: CompetitiveWindow` and an already-projected `room: int` as
caller-supplied facts, the same "accept already-loaded/-computed data, do
only this module's own I/O" discipline `capsheet_service.py` already uses
for `real_cap_settings_by_season`/`teams`.

**Decision P2-21 -- `rank_offers` never reads `player_motivation`
directly.** The original placeholder (ranked by total guaranteed money,
ties broken by the offering team's window rank, contenders ahead of
rebuilders) is **replaced by Decision P4-16** with a motivation-weighted
version, without this file ever importing `motivation_repo` itself --
motivation data reaches this function as an already-loaded `PlayerMotivation`
parameter (`agency_service.load_motivation_for_league`, "the agency
service's own read model," is the only legal source of it; see
`GmContext.motivation_by_player`'s own docstring). The retargeted P2-21
denylist, `tests/unit/test_layering.py::test_only_agency_service_imports_
motivation_repo`, now checks this repo-wide (only `agency_service.py` may
import `dba2.data.repositories.motivation_repo`) rather than scoping the
check to this one file -- the guarantee this docstring used to name (a
whole-file check on `free_agency_service.py` alone) is a strictly weaker
special case of the guarantee that replaced it.
"""
from __future__ import annotations

from collections.abc import Mapping

from dba2.agency.situation import window_success_score
from dba2.core.errors import NotFoundError
from dba2.data.db import Executor
from dba2.data.repositories import team_repo
from dba2.domain.league import LeagueCapSettings
from dba2.domain.motivation import PlayerMotivation
from dba2.domain.player import Player
from dba2.managers.decisions.signing import SigningOffer, construct_offer
from dba2.managers.explain import DecisionRecord
from dba2.managers.needs import NeedMatrix, tier_for_overall
from dba2.managers.snapshot import RosterPlayer
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow, GmTuning

# Mirrors `window.py::_BAND_ORDER`'s own ascending "win-now-ness" order,
# redeclared here rather than imported -- that name is `window.py`'s own
# underscore-private module constant, and this codebase's own convention
# (`picks.py` declaring its own slot-anchor table rather than importing
# `valuation.py`'s age anchors) is to redeclare rather than reach into
# another module's private data.
_WINDOW_TIE_BREAK_RANK: dict[CompetitiveWindow, int] = {
    CompetitiveWindow.REBUILD: 0,
    CompetitiveWindow.RETOOL: 1,
    CompetitiveWindow.WIN_NOW: 2,
    CompetitiveWindow.CONTEND: 3,
}
assert set(_WINDOW_TIE_BREAK_RANK) == set(CompetitiveWindow), (
    "_WINDOW_TIE_BREAK_RANK must cover exactly the four CompetitiveWindow values"
)


async def construct_offer_for_team(
    ex: Executor,
    *,
    team_id: int,
    roster_player: RosterPlayer,
    matrix: NeedMatrix,
    window: CompetitiveWindow,
    cap_settings: LeagueCapSettings,
    room: int,
    season: int,
    tuning: GmTuning = DEFAULT_TUNING,
) -> tuple[SigningOffer, DecisionRecord]:
    """One team's §9.3 offer for one free agent, plus its Decision P2-18
    `DecisionRecord` (a contract change from the single-`SigningOffer`
    return `managers.decisions.signing.construct_offer` used to have --
    see that module's own docstring). Loads only `team_repo.get_team`
    (for `gm_philosophy`) -- the one read this function needs of its own
    -- and delegates every formula to `construct_offer`. `window`/`cap_
    settings`/`room` are caller-supplied facts (see module docstring's
    scope boundary). Raises `NotFoundError` if `team_id` does not exist."""
    team = await team_repo.get_team(ex, team_id)
    if team is None:
        raise NotFoundError(f"team {team_id} does not exist")
    return construct_offer(
        roster_player,
        matrix,
        team_id=team_id,
        philosophy=team.gm_philosophy,
        window=window,
        cap_settings=cap_settings,
        room=room,
        season=season,
        tuning=tuning,
    )


def _rank_by_money_and_window(offers: tuple[SigningOffer, ...]) -> tuple[SigningOffer, ...]:
    """Decision P2-21's original placeholder, unchanged: sorted by
    `guaranteed_total` descending, ties broken by the offering team's
    window rank (`_WINDOW_TIE_BREAK_RANK`, contenders ahead of
    rebuilders). Kept as its own function (not inlined) because it is
    also `rank_offers`'s own fallback -- Decision P4-16's motivation-
    weighted version behaves exactly like this whenever it has nothing to
    weight by, matching this module's own "`None` means behave exactly as
    before" convention (`GmContext.retirement_age`/`room_for_signings_by_
    team`, `dba2/services/gm_service.py:229-260`)."""
    return tuple(
        sorted(
            offers,
            key=lambda offer: (-offer.guaranteed_total, -_WINDOW_TIE_BREAK_RANK[offer.window]),
        )
    )


def rank_offers(
    offers: tuple[SigningOffer, ...],
    player: Player,
    *,
    motivation: PlayerMotivation | None = None,
    matrix_by_team: Mapping[int, NeedMatrix] | None = None,
) -> tuple[SigningOffer, ...]:
    """Decision P4-16's motivation-weighted replacement for the P2-21
    placeholder. Weights each offer by the free agent's own normalized
    axis weights against the offering team's situation -- **money against
    the offer's own `guaranteed_total`, winning against the offering
    team's window, role/usage against that team's need/glut grid at the
    player's own position and tier, legacy against a hometown match** --
    exactly the four axes P4-16's own design text names, in that order.
    `loyalty` is deliberately never read here: a free agent choosing among
    several NEW suitors has no tenure with any of them yet, the one thing
    that axis measures.

    **`motivation is None` or `matrix_by_team is None`: falls back to
    `_rank_by_money_and_window` (Decision P2-21's original placeholder)
    byte-for-byte.** This is the same "`None` means behave exactly as
    before" convention `GmContext`'s other optional fields already use --
    every call site that predates Phase 4, and every call site that never
    populates `GmContext.motivation_by_player`, is unaffected by this
    replacement.

    **Coefficients are a first-draft shape (P4-9/P4-35: "expected to be
    swept"), not a calibrated formula.** **Only `money` is min-max
    normalized across THIS player's own offer set** (`(offer.guaranteed_
    total - lo) / (hi - lo)`, so the term is currency-scale-invariant
    within this call); `role_fit` is NOT min-max normalized against the
    offer set (critic nit 3 correction -- this docstring previously said
    it was) -- it is a fixed re-centering of `agency_service.py::build_
    player_situation`'s own `role_fit` formula, `(need[pos][tier] -
    glut[pos][tier] + 1.0) / 2.0`, mapping the already-`[-1, 1]`-clamped
    `need - glut` difference to `[0, 1]` independently per offer, with no
    reference to the min/max `role_term` reaches across this player's own
    offer set; `winning` reuses `dba2.agency.situation.window_success_
    score` (the identical `[0, 1]` normalization Pillar 5's own DB/harness
    scorers already use for a `CompetitiveWindow`, so this file gains no
    second copy of that formula); `legacy` is `1.0` on a hometown match,
    else `0.0` -- P4-12 means every real corpus reads `hometown_team_id is
    None` for every player today (a known, disclosed vacuity, P4-80 item
    8), so this term is currently always `0.0` in practice, not a defect
    in this function.
    """
    if motivation is None or matrix_by_team is None:
        return _rank_by_money_and_window(offers)

    weight_total = (
        motivation.weight_winning
        + motivation.weight_role_usage
        + motivation.weight_money
        + motivation.weight_legacy
    )
    if weight_total <= 0:
        # No signal to weight by (all four relevant traits rolled zero,
        # which `generate_traits` should never actually produce, but this
        # function does not assume its own caller's invariants) -- same
        # fallback the `None` branch above uses, never a `ZeroDivisionError`.
        return _rank_by_money_and_window(offers)
    w_winning = motivation.weight_winning / weight_total
    w_role = motivation.weight_role_usage / weight_total
    w_money = motivation.weight_money / weight_total
    w_legacy = motivation.weight_legacy / weight_total

    totals = [offer.guaranteed_total for offer in offers]
    lo, hi = min(totals), max(totals)
    span = hi - lo

    tier = tier_for_overall(player.overall_cached)
    position = player.primary_position

    def _score(offer: SigningOffer) -> float:
        money_term = 0.5 if span <= 0 else (offer.guaranteed_total - lo) / span
        # `SigningOffer.window` is a plain `CompetitiveWindow`, never
        # `None` -- `window_success_score` only returns `None` for a
        # caller's `None` input (an unclassified team, not this case).
        winning_term = window_success_score(offer.window)
        assert winning_term is not None
        matrix = matrix_by_team.get(offer.team_id)
        if matrix is None:
            role_term = 0.5
        else:
            # `agency_service.py::build_player_situation`'s own `role_fit`
            # formula, verbatim -- `NeedMatrix.need`/`.glut` are each
            # clamped to `[0, 1]` by construction (`managers/needs.py`),
            # so this difference is already `[-1, 1]`, re-centered to
            # `[0, 1]` for consistency with the other three terms.
            raw = matrix.need[position][tier] - matrix.glut[position][tier]
            role_term = (raw + 1.0) / 2.0
        legacy_term = (
            1.0
            if motivation.hometown_team_id is not None
            and motivation.hometown_team_id == offer.team_id
            else 0.0
        )
        return (
            w_winning * winning_term
            + w_role * role_term
            + w_money * money_term
            + w_legacy * legacy_term
        )

    # Critic nit 11: `_score` computed once per offer here, not inside the
    # sort key (which would call it O(n log n) times) -- trivial at real
    # offer-set sizes, fixed anyway since it was cheap.
    scored = [(offer, _score(offer)) for offer in offers]
    return tuple(
        offer
        for offer, _score_value in sorted(
            scored,
            key=lambda pair: (
                -pair[1],
                -pair[0].guaranteed_total,
                -_WINDOW_TIE_BREAK_RANK[pair[0].window],
            ),
        )
    )
