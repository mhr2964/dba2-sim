"""Hand-built `LeagueCapSettings`/`PlayerContract`/`RosterPlayer` fixtures
for `managers/` Tier 1 unit tests (§12 step 3 onward), following `tests/
fixtures/sim.py`'s own precedent of reusing one shared fixtures module
rather than each test file growing its own ad hoc builders.

`make_player`/`make_ratings` (this project's existing pairwise-distinct-
value fixtures) are reused directly from `tests/fixtures/sim.py` -- this
file adds only what step 3 needs on top: cap settings, contracts, and the
`RosterPlayer` join.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, date, datetime

from dba2.domain.contract import (
    Contract,
    ContractStatus,
    ContractType,
    ContractYear,
    OptionDecision,
    OptionType,
    RookieScale,
    SigningMethod,
)
from dba2.domain.league import LeagueCapSettings
from dba2.domain.player import Position, RosterStatus
from dba2.domain.team import GmPhilosophy
from dba2.managers.decisions.draft import prospect_value_for
from dba2.managers.picks import RookieScaleTable, pick_value, protected_pick_value
from dba2.managers.snapshot import PlayerContract, RosterPlayer, build_roster_player
from dba2.managers.tuning import CompetitiveWindow
from dba2.managers.valuation import market_value_for
from tests.fixtures.sim import make_player, make_ratings

# seeds/cap_settings.json's real 2025-26 values (Decision 8), constructed
# here as a real `LeagueCapSettings` row instead of loose module constants
# (the same figures `tools/_verify_gm_design_numbers.py` used to hardcode,
# before that design-time-only script was deleted at step 5 per its own
# stated deletion rule -- critic round 10, B4).
CAP_SETTINGS: LeagueCapSettings = LeagueCapSettings(
    league_id=1,
    season=2026,
    salary_cap=154_647_000,
    luxury_tax=187_895_000,
    apron_1=195_945_000,
    apron_2=207_824_000,
    max_pct_0_6=25,
    max_pct_7_9=30,
    max_pct_10_plus=35,
    supermax_pct=35,
    min_salary=1_272_870,
    mle_amount=14_104_000,
    bi_annual_amount=5_130_000,
    raise_pct_bird=8,
    raise_pct_non_bird=5,
    roster_min=14,
    roster_max=15,
)

# migration 003's real 30 `rookie_scale` rows, verbatim (pick_number ->
# RookieScale) -- the same source `tools/_verify_gm_design_numbers.py`
# used to hardcode before it was deleted at step 5 (critic round 10, B4).
# **Row 30 here is un-repaired** (Decision P2-33's repair is
# migration 009's job, step 7's -- not built as of this fixture): **all
# four year-columns are corrupt, not year_4 alone** -- y1/y2/y3 are each
# ~-33.99% below row 29's own figure and y4 is ~-37.56% below row 29's,
# against columns that otherwise decline 2.4-2.7%/slot (y1-y3) or
# 4.3-4.6%/slot (y4) from row 25 through row 29 (critic round 10, B1).
# `test_picks.py`'s monotonicity test carves out the 29->30 boundary
# explicitly and cites this fixture's own docstring rather than silently
# repairing the data here, which is out of `picks.py`'s (step 5's) scope.
ROOKIE_SCALE: RookieScaleTable = {
    row.pick_number: row
    for row in (
        RookieScale(1, 10306508, 10821833, 11337157, 14858652),
        RookieScale(2, 9700911, 10185957, 10671002, 13925000),
        RookieScale(3, 9145524, 9602800, 10060076, 13105500),
        RookieScale(4, 8633147, 9064804, 9496462, 12330000),
        RookieScale(5, 8160117, 8568123, 8976128, 11600000),
        RookieScale(6, 7722917, 8109063, 8495209, 10910000),
        RookieScale(7, 7319127, 7685083, 8051039, 10265000),
        RookieScale(8, 6945399, 7292669, 7639939, 9662000),
        RookieScale(9, 6599445, 6929417, 7259389, 9097000),
        RookieScale(10, 6279079, 6593033, 6906986, 8571000),
        RookieScale(11, 5982158, 6281266, 6580374, 8080000),
        RookieScale(12, 5706614, 5991945, 6277275, 7622000),
        RookieScale(13, 5450437, 5722959, 5995481, 7195000),
        RookieScale(14, 5212638, 5473270, 5733902, 6796000),
        RookieScale(15, 4992269, 5241882, 5491496, 6425000),
        RookieScale(16, 4787395, 5026765, 5266134, 6078000),
        RookieScale(17, 4597110, 4826966, 5056821, 5756000),
        RookieScale(18, 4420537, 4641564, 4862590, 5455000),
        RookieScale(19, 4256826, 4469667, 4682508, 5174000),
        RookieScale(20, 4105149, 4310406, 4515664, 4912000),
        RookieScale(21, 3964702, 4162937, 4361172, 4668000),
        RookieScale(22, 3834700, 4026435, 4218171, 4441000),
        RookieScale(23, 3714376, 3900095, 4085814, 4228000),
        RookieScale(24, 3602982, 3782131, 3961281, 4029000),
        RookieScale(25, 3499789, 3672778, 3845768, 3842000),
        RookieScale(26, 3404091, 3571296, 3738500, 3667000),
        RookieScale(27, 3315200, 3476960, 3638720, 3503000),
        RookieScale(28, 3232448, 3394070, 3555693, 3348000),
        RookieScale(29, 3155188, 3312947, 3470707, 3203000),
        RookieScale(30, 2082678, 2186812, 2290946, 2000000),  # corrupt row (Decision P2-33)
    )
}
assert len(ROOKIE_SCALE) == 30, "migration 003 seeds exactly 30 rookie_scale rows"


def birth_date_for_age(age: int, season: int) -> date:
    """The inverse of `managers.scouting.player_age` (`season -
    birth_date.year`) -- picks a birth year that makes `player_age(...,
    season) == age` exactly, so archetype fixtures can be built "a 25-year-
    old in season 2026" rather than hand-computing a birth year."""
    return date(season - age, 6, 15)


def make_contract(
    player_id: int,
    *,
    league_id: int = 1,
    team_id: int = 1,
    start_season: int,
    salaries: tuple[int, ...],
    option_types: tuple[OptionType, ...] | None = None,
    contract_type: ContractType = ContractType.STANDARD,
    signing_method: SigningMethod = SigningMethod.CAP_SPACE,
) -> PlayerContract:
    """A `PlayerContract` -- one `Contract` header plus one `ContractYear`
    per entry in `salaries`, seasons `start_season .. start_season +
    len(salaries) - 1`. `option_types` defaults to `OptionType.NONE`
    (fully guaranteed) for every year; a caller names only the years that
    differ."""
    n = len(salaries)
    options = option_types if option_types is not None else tuple(OptionType.NONE for _ in range(n))
    assert len(options) == n, "option_types must be the same length as salaries"

    contract = Contract(
        id=player_id,
        league_id=league_id,
        player_id=player_id,
        team_id=team_id,
        signed_in_season=start_season,
        start_season=start_season,
        end_season=start_season + n - 1,
        total_years=n,
        contract_type=contract_type,
        signing_method=signing_method,
        no_trade_clause=False,
        trade_kicker_pct=0,
        status=ContractStatus.ACTIVE,
        terminated_in_season=None,
        created_at=datetime(start_season - 1, 7, 1, tzinfo=UTC),
        terminated_at=None,
    )
    years = tuple(
        ContractYear(
            contract_id=contract.id,
            season=start_season + i,
            salary=salaries[i],
            guaranteed_amount=salaries[i] if options[i] is OptionType.NONE else 0,
            option_type=options[i],
            option_decision=(
                OptionDecision.N_A if options[i] is OptionType.NONE else OptionDecision.PENDING
            ),
        )
        for i in range(n)
    )
    return PlayerContract(contract=contract, years=years)


def make_roster_player(
    player_id: int,
    *,
    age: int,
    season: int,
    overall_cached: int,
    league_id: int = 1,
    team_id: int | None = 1,
    primary_position: Position = Position.SG,
    secondary_position: Position | None = None,
    contract: PlayerContract | None = None,
    peak_age_start: int | None = None,
    peak_age_end: int | None = None,
    ratings_overrides: dict[str, int] | None = None,
) -> RosterPlayer:
    """A full `RosterPlayer` (step 1's join type) at a stated age/overall
    in a stated season -- the unit `managers.valuation.market_value`
    prices. Delegates rating-field distinctness to `tests.fixtures.sim.
    make_ratings` (this project's established "55 pairwise-distinct
    values" discipline) rather than a flat/uniform fixture. `peak_age_
    start`/`peak_age_end` are applied via `dataclasses.replace` after
    construction (matching `test_scouting.py`'s own precedent) since `make_
    ratings` already binds both as explicit keyword arguments and cannot
    also accept them through `**ratings_overrides`. `primary_position`/
    `secondary_position` default to `make_player`'s own defaults (SG,
    `None`) -- added for `managers.needs`' (step 6) position-coverage
    fixtures, which are the first callers under this project that need to
    vary either."""
    player = make_player(
        player_id,
        league_id=league_id,
        team_id=team_id,
        primary_position=primary_position,
        overall_cached=overall_cached,
    )
    player.secondary_position = secondary_position
    player.birth_date = birth_date_for_age(age, season)
    ratings = make_ratings(player_id, league_id=league_id, **(ratings_overrides or {}))
    replacements: dict[str, int] = {}
    if peak_age_start is not None:
        replacements["peak_age_start"] = peak_age_start
    if peak_age_end is not None:
        replacements["peak_age_end"] = peak_age_end
    if replacements:
        ratings = dataclasses.replace(ratings, **replacements)
    return build_roster_player(player, ratings, contract=contract)


@dataclass(frozen=True)
class Archetype:
    """One row of `seeds/gm_value_archetypes.json` -- a label plus enough
    to build the `RosterPlayer` and reproduce a scouted view of it
    deterministically. `team_id`/`league_rng_seed` are fixed per archetype
    (not swept) since the golden table itself has no team axis (§10: "7 x
    4 = 28 columns," a `(philosophy, window)` grid only) -- the *disagreement*
    verify sweeps `team_id` separately, over its own synthetic league, not
    through this table."""

    label: str
    roster_player: RosterPlayer
    season: int
    team_id: int
    league_rng_seed: int


_ARCHETYPE_SEASON = 2026
_ARCHETYPE_TEAM_ID = 1
_ARCHETYPE_RNG_SEED = 2026_03


def _archetypes() -> tuple[Archetype, ...]:
    season = _ARCHETYPE_SEASON

    rookie_90_25 = make_roster_player(
        101, age=25, season=season, overall_cached=90,
        contract=make_contract(
            101, start_season=season,
            salaries=(9_500_000, 10_200_000, 10_900_000, 11_600_000),
        ),
        peak_age_start=28, peak_age_end=32,
        ratings_overrides={"potential": 95, "potential_variance": 88},
    )
    max_85_33 = make_roster_player(
        102, age=33, season=season, overall_cached=85,
        contract=make_contract(
            102, start_season=season,
            salaries=(46_394_100, 48_713_805, 51_033_510, 53_353_215),
        ),
        peak_age_start=26, peak_age_end=31,
        ratings_overrides={"potential": 83, "potential_variance": 70},
    )
    min_76_27 = make_roster_player(
        103, age=27, season=season, overall_cached=76,
        contract=make_contract(103, start_season=season, salaries=(2_087_519,)),
        peak_age_start=27, peak_age_end=31,
        ratings_overrides={"potential": 78, "potential_variance": 72},
    )
    overpaid_72_30 = make_roster_player(
        104, age=30, season=season, overall_cached=72,
        contract=make_contract(
            104, start_season=season,
            salaries=(30_929_400, 32_475_870, 34_022_340, 35_568_810),
        ),
        peak_age_start=27, peak_age_end=31,
        ratings_overrides={"potential": 70, "potential_variance": 65},
    )
    team_option_78_29 = make_roster_player(
        105, age=29, season=season, overall_cached=78,
        contract=make_contract(
            105, start_season=season,
            salaries=(21_000_000, 22_000_000, 18_000_000),
            option_types=(OptionType.NONE, OptionType.NONE, OptionType.TEAM),
        ),
        peak_age_start=29, peak_age_end=33,
        ratings_overrides={"potential": 77, "potential_variance": 74},
    )
    player_option_78_29 = make_roster_player(
        106, age=29, season=season, overall_cached=78,
        contract=make_contract(
            106, start_season=season,
            salaries=(21_000_000, 22_000_000, 18_000_000),
            option_types=(OptionType.NONE, OptionType.NONE, OptionType.PLAYER),
        ),
        peak_age_start=29, peak_age_end=33,
        ratings_overrides={"potential": 77, "potential_variance": 74},
    )
    team_option_84_above_market = make_roster_player(
        107, age=29, season=season, overall_cached=84,
        contract=make_contract(
            107, start_season=season, salaries=(63_405_270,),
            option_types=(OptionType.TEAM,),
        ),
        peak_age_start=29, peak_age_end=33,
        ratings_overrides={"potential": 84, "potential_variance": 80},
    )
    player_option_84_above_market = make_roster_player(
        108, age=29, season=season, overall_cached=84,
        contract=make_contract(
            108, start_season=season, salaries=(63_405_270,),
            option_types=(OptionType.PLAYER,),
        ),
        peak_age_start=29, peak_age_end=33,
        ratings_overrides={"potential": 84, "potential_variance": 80},
    )
    positive_residual_21 = make_roster_player(
        109, age=21, season=season, overall_cached=65,
        contract=make_contract(
            109, start_season=season,
            salaries=(4_105_149, 4_310_406, 4_515_664, 4_912_000),
        ),
        peak_age_start=27, peak_age_end=31,
        ratings_overrides={"potential": 76, "potential_variance": 82},
    )
    negative_residual_21 = make_roster_player(
        110, age=21, season=season, overall_cached=65,
        contract=make_contract(
            110, start_season=season,
            salaries=(4_105_149, 4_310_406, 4_515_664, 4_912_000),
        ),
        peak_age_start=27, peak_age_end=31,
        ratings_overrides={"potential": 54, "potential_variance": 48},
    )
    defense_first_wing = make_roster_player(
        111, age=26, season=season, overall_cached=79,
        contract=make_contract(
            111, start_season=season,
            salaries=(19_000_000, 20_000_000, 21_000_000),
        ),
        peak_age_start=28, peak_age_end=32,
        ratings_overrides={
            "catch_shoot_3": 92, "off_dribble_3": 88, "handle": 90, "first_step": 91,
            "on_ball_defense": 40, "help_iq": 38, "rim_protection": 30, "post_defense": 35,
            "potential": 80, "potential_variance": 75,
        },
    )
    defense_first_big = make_roster_player(
        112, age=28, season=season, overall_cached=74,
        contract=make_contract(112, start_season=season, salaries=(9_000_000, 9_500_000)),
        peak_age_start=27, peak_age_end=31,
        ratings_overrides={
            "catch_shoot_3": 20, "off_dribble_3": 15, "handle": 25, "first_step": 22,
            "on_ball_defense": 90, "help_iq": 93, "rim_protection": 95, "post_defense": 91,
            "potential": 75, "potential_variance": 70,
        },
    )

    labeled = (
        ("rookie_90_25", rookie_90_25),
        ("max_85_33", max_85_33),
        ("min_76_27", min_76_27),
        ("overpaid_72_30", overpaid_72_30),
        ("team_option_78_29", team_option_78_29),
        ("player_option_78_29", player_option_78_29),
        ("team_option_84_above_market", team_option_84_above_market),
        ("player_option_84_above_market", player_option_84_above_market),
        ("positive_residual_21", positive_residual_21),
        ("negative_residual_21", negative_residual_21),
        ("defense_first_wing", defense_first_wing),
        ("defense_first_big", defense_first_big),
    )
    return tuple(
        Archetype(
            label=label,
            roster_player=roster_player,
            season=season,
            team_id=_ARCHETYPE_TEAM_ID,
            league_rng_seed=_ARCHETYPE_RNG_SEED,
        )
        for label, roster_player in labeled
    )


ARCHETYPES: tuple[Archetype, ...] = _archetypes()
assert len(ARCHETYPES) == 12, "§10's player archetype table is 12 rows, exactly"
assert len({a.label for a in ARCHETYPES}) == 12, "archetype labels must be unique"


def generate_archetype_table() -> dict[str, dict[str, float]]:
    """`seeds/gm_value_archetypes.json`'s payload, computed fresh from
    `ARCHETYPES` and `managers.valuation.market_value_for` -- 12 rows x
    7 philosophies x 4 windows = 336 entries (§10). Called both by the
    one-off generation step and by `test_valuation.py`'s own golden-file
    test, so the two can never drift apart by construction (the same
    function produces the checked-in file and the value the test compares
    it against)."""
    table: dict[str, dict[str, float]] = {}
    for archetype in ARCHETYPES:
        row: dict[str, float] = {}
        for philosophy in GmPhilosophy:
            for window in CompetitiveWindow:
                value = market_value_for(
                    archetype.roster_player,
                    league_rng_seed=archetype.league_rng_seed,
                    team_id=archetype.team_id,
                    season=archetype.season,
                    philosophy=philosophy,
                    window=window,
                    cap_settings=CAP_SETTINGS,
                )
                row[f"{philosophy.value}:{window.value}"] = round(value, 4)
        table[archetype.label] = row
    return table


# ---------------------------------------------------------------------------
# §10's pick archetype table (step 5, `picks.py`) -- eight rows, per §10's
# own itemized list and its "eight, counted rather than gestured at" text.
# **§12 step 5's own summary paragraph says "6 rows x 28 pairs," which is
# stale** -- round 3's caption/prose mismatch that §10 later corrected to
# eight and never backported into step 5's one-line summary (see this
# dispatch's report). §10's itemized list and its `| 8 picks | 224 |`
# table row are the more specific, later-revised text and are followed
# here.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PickArchetype:
    """One row of `seeds/gm_pick_archetypes.json`. `protection_through`
    is `None` for a plain, unprotected `pick_value(slot, years_out, ...)`
    row; when set, `slot` doubles as the recipe's `expected_slot` and the
    row is priced through `protected_pick_value` instead. `expected_slot`
    is always a literal parameter here, never a lookup into a seeded
    league's own `window_score` ranking -- "a golden table that depends on
    which league was seeded is not golden" (§10)."""

    label: str
    slot: int
    years_out: int = 0
    protection_through: int | None = None


PICK_ARCHETYPES: tuple[PickArchetype, ...] = (
    PickArchetype("top3_pick_2y_out", slot=2, years_out=2),
    PickArchetype("protected_top5_expected_slot_2", slot=2, protection_through=5),
    PickArchetype("protected_top5_expected_slot_15", slot=15, protection_through=5),
    PickArchetype("pick_29", slot=29),
    PickArchetype("pick_30", slot=30),
    PickArchetype("pick_31", slot=31),
    PickArchetype("pick_45", slot=45),
    PickArchetype("pick_60", slot=60),
)
assert len(PICK_ARCHETYPES) == 8, "§10's pick archetype table is 8 rows, exactly"
assert len({a.label for a in PICK_ARCHETYPES}) == 8, "pick archetype labels must be unique"


def generate_pick_archetype_table() -> dict[str, dict[str, float]]:
    """`seeds/gm_pick_archetypes.json`'s payload -- 8 rows x 7 philosophies
    x 4 windows = 224 entries (§10), computed fresh from `PICK_ARCHETYPES`
    against `ROOKIE_SCALE`/`CAP_SETTINGS`. Priced against the real,
    currently-seeded (un-repaired) `rookie_scale` table -- see `ROOKIE_
    SCALE`'s own docstring for why that is this step's correct, documented
    scope rather than an oversight."""
    table: dict[str, dict[str, float]] = {}
    for archetype in PICK_ARCHETYPES:
        row: dict[str, float] = {}
        for philosophy in GmPhilosophy:
            for window in CompetitiveWindow:
                if archetype.protection_through is not None:
                    value = protected_pick_value(
                        float(archetype.slot),
                        archetype.protection_through,
                        philosophy,
                        window,
                        rookie_scale=ROOKIE_SCALE,
                        cap_settings=CAP_SETTINGS,
                        years_out=archetype.years_out,
                    )
                else:
                    value = pick_value(
                        archetype.slot,
                        archetype.years_out,
                        philosophy,
                        window,
                        rookie_scale=ROOKIE_SCALE,
                        cap_settings=CAP_SETTINGS,
                    )
                row[f"{philosophy.value}:{window.value}"] = round(value, 4)
        table[archetype.label] = row
    return table


# ---------------------------------------------------------------------------
# §10's prospect archetype table (step 9, `draft.py`) -- 2 rows, per §10's
# own itemized list ("2 prospects | 56" -- 2 rows x 7 philosophies x 4
# windows). A draft-eligible prospect at implied slot 1 and one at implied
# slot 40 -- the pair that pins Decision P2-27's implied-contract rule on
# both sides of the 30/31 horizon split.
# ---------------------------------------------------------------------------


def _as_draft_eligible(roster_player: RosterPlayer) -> RosterPlayer:
    """Rewrites a `make_roster_player`-built `RosterPlayer` to `status =
    DRAFT_ELIGIBLE`, `team_id = None` -- `make_roster_player` itself has
    no `roster_status` override (every other Phase 2 fixture is a
    rostered player), so this is a small, local post-processing step
    rather than growing that function's own parameter surface for a
    single caller."""
    player = dataclasses.replace(
        roster_player.player, roster_status=RosterStatus.DRAFT_ELIGIBLE, team_id=None
    )
    return build_roster_player(player, roster_player.ratings, contract=None)


@dataclass(frozen=True)
class ProspectArchetype:
    """One row of `seeds/gm_prospect_archetypes.json`. `implied_slot` is a
    literal parameter here, never a lookup into a seeded league's own
    board -- "a golden table that depends on which league was seeded is
    not golden" (§10, the same reasoning `PickArchetype`'s own docstring
    states for `expected_slot`)."""

    label: str
    roster_player: RosterPlayer
    implied_slot: int
    season: int
    team_id: int
    league_rng_seed: int


def _prospect_archetypes() -> tuple[ProspectArchetype, ...]:
    season = _ARCHETYPE_SEASON

    top_prospect = _as_draft_eligible(
        make_roster_player(
            201, age=19, season=season, overall_cached=68,
            peak_age_start=26, peak_age_end=30,
            ratings_overrides={"potential": 88, "potential_variance": 82},
        )
    )
    fringe_prospect = _as_draft_eligible(
        make_roster_player(
            202, age=20, season=season, overall_cached=57,
            peak_age_start=25, peak_age_end=29,
            ratings_overrides={"potential": 64, "potential_variance": 60},
        )
    )

    return (
        ProspectArchetype(
            label="top_prospect_implied_slot_1", roster_player=top_prospect, implied_slot=1,
            season=season, team_id=_ARCHETYPE_TEAM_ID, league_rng_seed=_ARCHETYPE_RNG_SEED,
        ),
        ProspectArchetype(
            label="fringe_prospect_implied_slot_40", roster_player=fringe_prospect,
            implied_slot=40, season=season, team_id=_ARCHETYPE_TEAM_ID,
            league_rng_seed=_ARCHETYPE_RNG_SEED,
        ),
    )


PROSPECT_ARCHETYPES: tuple[ProspectArchetype, ...] = _prospect_archetypes()
assert len(PROSPECT_ARCHETYPES) == 2, "§10's prospect archetype table is 2 rows, exactly"
assert len({a.label for a in PROSPECT_ARCHETYPES}) == 2, "prospect archetype labels must be unique"


def generate_prospect_archetype_table() -> dict[str, dict[str, float]]:
    """`seeds/gm_prospect_archetypes.json`'s payload -- 2 rows x 7
    philosophies x 4 windows = 56 entries (§10), computed fresh from
    `PROSPECT_ARCHETYPES` against `ROOKIE_SCALE`/`CAP_SETTINGS` via
    `managers.decisions.draft.prospect_value_for`."""
    table: dict[str, dict[str, float]] = {}
    for archetype in PROSPECT_ARCHETYPES:
        row: dict[str, float] = {}
        for philosophy in GmPhilosophy:
            for window in CompetitiveWindow:
                value = prospect_value_for(
                    archetype.roster_player,
                    league_rng_seed=archetype.league_rng_seed,
                    team_id=archetype.team_id,
                    season=archetype.season,
                    philosophy=philosophy,
                    window=window,
                    cap_settings=CAP_SETTINGS,
                    rookie_scale=ROOKIE_SCALE,
                    implied_slot=archetype.implied_slot,
                )
                row[f"{philosophy.value}:{window.value}"] = round(value, 4)
        table[archetype.label] = row
    return table
