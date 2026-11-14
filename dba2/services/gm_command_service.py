"""Stage D''s human roster-decision commands (docs/design/phase-5-discord-
presentation.md §21 JC-4, §23 D'-17, §27's "all six of JC-4's roster
decisions, not a subset" confirmation) plus `/timeline` (§29, Decision
P5-66/P5-67/P5-68, "the seventh ROSTER command").

**This module is the service-layer half of Decision P5-38's dual
enforcement** -- every function here resolves the league and the team,
calls `permissions.require_can_act_for_team` (or, for the six roster
commands, that same predicate; `/timeline` is team-scoped identically to
the other six, not commissioner-only) BEFORE doing any write, and calls
`phase.service.require_phase` against the SAME phase set `command_gates.
COMMAND_GATES` registers for that command -- one dict, read by both the
bot layer's `ensure_command_legal` and this layer's `require_phase` call,
so the two checks can never drift apart (the exact single-sourcing
Decision P5-38's own text requires: "there is no second implementation
of who owns this team," extended here to phase legality too).

**Decision P5-39 -- no human code path that moves a player, a contract or
a pick.** `waive`/`release` call `roster_service.waive`/`release`
directly (Phase 0's own appliers); `sign_free_agent` calls `gm_service.
apply_signing` (the exact function `run_phase_hook`'s free-agency branch
calls for an AI signing) with a caller-built `SigningExecution`. `set_
strategy`/`set_role`/`set_directive`/`set_timeline` call the existing
Phase 0/2/5 repo upserts directly (`team_repo.upsert_team_strategy`,
`role_repo.upsert_player_role`/`upsert_player_directive`, `team_repo.
set_team_timeline`) -- none of those six have an AI-side applier to
share (no AI code path ever upserts a role/directive/strategy row on a
human's behalf), so there is nothing to reuse and nothing to duplicate.

**`apply_signing` needs a `SigningExecution.record: DecisionRecord`
field it never actually reads (verified by reading its own body --
Decision P2-18's `DecisionRecord` is written to the JSONL decision log
only by `run_phase_hook`'s OWN caller, never by `apply_signing` itself).
`sign_free_agent` builds a minimal, inert placeholder record** (`terms=
()`, `score=0.0`, so `record.reconstructs()` trivially holds) purely to
satisfy the dataclass's required field -- it is never persisted, never
logged, and does not claim a human's signing was an AI decision.

**Stage D'-25 adds `/draft`'s own service half** (`draft_pick`, docs/
design/phase-5-stage-d-prime-draft.md, Decisions P5-98.g/h) -- the first
function here with TWO transactions, and the first that calls a service
applier after its own write has already committed. It obeys the same dual
enforcement as the seven above and adds the league-claim check P5-90 rule
2 requires of every human-initiated write.

**Of the roster commands, only `/timeline` writes a `league_transactions`
row** (`/draft` writes one too, but from `gm_service.apply_human_draft_
pick`, not from here -- Decision P5-98.e). Decision P5-32
is a real test, run rather than waved at: a fact only belongs in the log
if no other table already carries its history. Waive/release/sign already
have one (`contracts`/`contract_years`/`dead_cap_charges`); strategy/
role/directive rows ARE their own current-state history (no time-series
table exists for any of the three, but nothing in §18.1 names them as
needing one either -- unlike a team's timeline, which P5-68's own text
proves has no history table anywhere). `/timeline`'s log write is P5-68's
own explicit second member of the vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from dba2.core.errors import ConcurrencyError, NotFoundError, ValidationError
from dba2.core.logging import get_logger
from dba2.data.db import Executor, transaction
from dba2.data.repositories import (
    contract_repo,
    draft_repo,
    league_repo,
    offer_repo,
    player_repo,
    role_repo,
    team_repo,
    txn_repo,
)
from dba2.domain.contract import Contract, ContractStatus, DeadCapCharge, OptionType, SigningMethod
from dba2.domain.draft import DraftPick
from dba2.domain.league import League
from dba2.domain.phase import Phase, TradeStatus
from dba2.domain.player import Player, RosterStatus
from dba2.domain.role import DefenseMode, PlayerDirective, PlayerRole, ShotDiet, UsageMode
from dba2.domain.team import (
    DefensiveScheme,
    GmPhilosophy,
    OffensiveScheme,
    Team,
    TeamStrategy,
    Timeline,
    TimelineSource,
)
from dba2.domain.transaction import TransactionType
from dba2.managers import capsheet
from dba2.managers.decisions import signing as signing_decisions
from dba2.managers.explain import DecisionRecord
from dba2.managers.snapshot import PlayerContract
from dba2.phase import command_gates, permissions
from dba2.phase.service import require_phase
from dba2.services import cap_service, league_claim, roster_lock, roster_service
from dba2.services.gm_service import (
    _TIMELINE_TO_WINDOW,
    DRAFT_CONTENTION_MESSAGE,
    DraftCycleOutcome,
    DraftCycleReason,
    DraftCycleState,
    SigningExecution,
    _write_decision_records,
    apply_human_draft_pick,
    apply_signing,
    run_draft_cycle,
)

log = get_logger(__name__)


@dataclass(frozen=True)
class TeamActionContext:
    """What every function below resolves before it can even check a
    permission -- `require_can_act_for_team` needs `team.manager_user_id`,
    which means the team must already be fetched (unlike `assign_manager`'s
    commissioner-only check, which needs no team at all and therefore
    checks permission first). Returned to callers that need the resolved
    `league`/`team` afterward (most do, to build a response panel)."""

    league: League
    team: Team

    @property
    def season(self) -> int:
        return self.league.current_season


async def _resolve_and_authorize(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    actor_user_id: int,
    command: str,
    refuse_under_auto_pilot: bool,
) -> TeamActionContext:
    """GM-3b.2(a) (docs/design/gm-command-surface.md): `refuse_under_auto_
    pilot` is a REQUIRED keyword-only parameter, not a command-keyed
    frozenset -- chosen over a lookup table precisely because a table is a
    second PLACE to forget a command (GM-7(b)'s rejected alias-map shape),
    while a required parameter is not a place at all: omitting it is a
    `TypeError` at the call site, not a silent default. `"draft"` passes
    `False` here NOT because it is allowed under auto-pilot -- it is
    refused, one layer deeper, by GM-3's own branch inside `gm_service.
    apply_human_draft_pick` -- see that call site's own inline comment.

    **(b) The predicate is `team.manager_user_id is not None and team.
    auto_pilot`, not bare `auto_pilot`.** This is what keeps JC-1's
    commissioner override (`permissions.py`) intact: JC-1's case is an
    ABSENT manager, and an auto-piloted team always has one (`/gm auto`
    requires `require_team_manager`, which no manager-less row can
    satisfy), so the two states are disjoint by construction except for
    GM-5's own disclosed stranded-row residue (`manager_user_id IS NULL
    AND auto_pilot = TRUE`) -- spelling the predicate this way makes a
    blanket refusal on that residue impossible for free.

    **(d) One pre-transaction check, no re-check under the gate.** The
    only writer that can ever set `auto_pilot` TRUE is the team's own
    manager (`/gm auto`, `require_team_manager`-gated); the only other
    writer (`remove_manager`) only ever clears it. So the one possible
    third-party interleaving -- a commissioner revoking the team out from
    under an auto-piloted manager mid-command -- can only ever produce a
    SPURIOUS refusal, never a missed one (the freshly-fetched `team`
    row's own `require_can_act_for_team` re-check, made below, refuses
    that actor anyway on a different ground). A human write racing an AI
    decision is separately impossible regardless (EXCLUSIVE vs SHARED on
    the league gate, GM-3b's own "what is NOT at stake" analysis)."""
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    team = await team_repo.get_team(ex, team_id)
    if team is None or team.league_id != league_id:
        raise NotFoundError(f"No team {team_id} in league {league_id}")
    permissions.require_can_act_for_team(
        actor_user_id,
        commissioner_user_id=league.commissioner_user_id,
        team_manager_user_id=team.manager_user_id,
        team_id=team_id,
    )
    if refuse_under_auto_pilot and team.manager_user_id is not None and team.auto_pilot:
        _raise_auto_pilot_refusal(actor_user_id, team, command=command)
    require_phase(league.current_phase, *command_gates.COMMAND_GATES[command])
    return TeamActionContext(league=league, team=team)


_AUTO_PILOT_REFUSAL_VERBS = {
    "waive": "waive",
    "release": "release",
    "sign_free_agent": "sign a free agent",
}


def _raise_auto_pilot_refusal(actor_user_id: int, team: Team, *, command: str) -> None:
    """GM-3b.2(c): the message is conditional on WHO is being refused,
    exactly like GM-3's own draft-branch message -- `/gm auto` is manager-
    only (GM-8), so telling a non-owning commissioner to run it is advice
    they will be refused for taking."""
    verb = _AUTO_PILOT_REFUSAL_VERBS[command]
    if permissions.is_team_manager(actor_user_id, team_manager_user_id=team.manager_user_id):
        raise permissions.PermissionDenied(
            f"{team.code} is on auto-pilot -- the AI is handling its roster moves. "
            f"Run `/gm auto enabled:off` to {verb} yourself."
        )
    raise permissions.PermissionDenied(
        f"{team.code} is on auto-pilot -- the AI is handling its roster moves. "
        f"Only {team.code}'s own GM can turn that off (`/gm auto`), or use "
        "`/remove-manager` to hand the team to the AI outright."
    )


async def _refuse_while_the_advance_is_claimed(conn, *, league_id: int) -> None:
    """The claim check for the four settings writes, which open no gate (P5-99.t).

    Claim-only, the shape `offer_service.veto` uses: a settings upsert mutates
    no RMLP table, so these do not open the league gate. Each setter opens its
    own transaction with `set_lock_timeout` as its FIRST statement and calls
    this second -- `check_claim` takes `leagues ... FOR SHARE`, and behind a
    press's uncommitted `UPDATE leagues` that wait is otherwise unbounded (the
    server default), pinning a pool connection and a person's deferred reply.
    The timeout is spelled out in each setter rather than folded in here
    because discipline check 10 reads each function's `transaction()` block for
    it syntactically."""
    await league_claim.check_claim(
        conn, gate=None, league_id=league_id, require_phase_agreement=False
    )


@dataclass(frozen=True)
class SigningLegalityBasis:
    """The two computed facts `_check_free_agent_signing_legal` gates on --
    split out as its own return value (round-3 critic recommendation)
    rather than folded into a function that only ever raises, so a test
    can inspect these exact numbers directly instead of only observing a
    pass/fail on a bespoke fixture.

    **Round-5 correction of this docstring's own earlier claim.** This
    docstring used to say a parity test should assert BOTH fields "agree
    with the AI's own basis" -- true for `apron2_headroom` (below), but
    exactly the claim that made rounds 2, 3, and 4 each ship a real
    roster-overfill regression in `physical_roster_count`'s computation:
    each round borrowed the AI's own next-season-oriented `_next_season_
    roster_count`/`_occupies_next_season` and each round picked a
    different, wrong `season`/`season + 1` argument for it, because the
    underlying question ("does this team have room for one more body
    RIGHT NOW") was never actually a next-season question -- `sign_free_
    agent` writes `start_season=ctx.season`, not `ctx.season + 1`. Round
    4's fix (see `_compute_signing_legality_basis`'s own docstring)
    replaced the borrowed AI formula with a direct `RosterStatus.ACTIVE`
    count, and no test should ever again assert `physical_roster_count`
    equals a next-season AI projection -- see `tests/integration/
    test_gm_command_service_legality_matches_ai_basis.py::test_physical_
    roster_count_matches_real_active_count_independent_of_either_formula`
    for the correct, basis-independent replacement (ground-truth Postgres
    `COUNT(*)`, no formula on either side).

    `apron2_headroom` is the one field where "agrees with the AI's own
    basis" remains true and worth testing: both the human and the AI ask
    "what is this team's real cap room, via `capsheet.compute_team_
    season_cap_sheet`'s real `SeasonCapProjection`, at the season the
    money actually lands" -- for a human signing that is `season` (now);
    for an AI signing it is `season + 1` (`SigningExecution.start_season=
    next_season`). Same primitive, different (correctly different)
    season argument -- see `test_gm_command_service_legality_matches_ai_
    basis.py`'s remaining cap-headroom-parity test.

    **Guidance for whatever the offer-review surface (`offers.py`, still
    unbuilt) copies from this module (round-5 critic finding B2):** an
    immediate-effect legality check -- and a trade's accept-time
    re-validation genuinely is one, exactly like a human signing -- must
    use a DIRECT current-state count for roster occupancy (real rows,
    real `roster_status`, read right now), never `_next_season_roster_
    count`/`_occupies_next_season` or any other AI next-season-projection
    function. For cap legality, DO copy `capsheet.compute_team_season_
    cap_sheet` (fed the trade's own real effective season), not a hand-
    rolled re-derivation of `apron1_room` (round 2's own blocking issue)."""

    physical_roster_count: int
    apron2_headroom: int
    roster_max: int


async def _compute_signing_legality_basis(
    ex: Executor, *, league_id: int, team: Team, season: int
) -> SigningLegalityBasis:
    """The pure-computation half of `_check_free_agent_signing_legal` --
    everything up to but not including the raise. Enforces the same TWO
    kinds of rule `run_free_agency`'s own AI bidder loop applies before it
    will even construct an offer for a team (`gm_service.py` around
    :5287-5301 -- `roster_count[team_id] >= roster_max`, `_can_sign_at_
    all`'s apron_2-headroom test): the AI's decision layer never lets a
    signing reach `apply_signing` without both holding. The human path had
    no equivalent gate before round 1's fix (critic's blocking issue 4).

    **Round-5 correction:** this docstring used to say this function
    "mirrors" the AI's own two gates, meaning it should compute the SAME
    numbers the AI's own formulas would on an identical fixture. That is
    still true for the cap-legality half (`apron2_headroom`, built from
    the same `capsheet.compute_team_season_cap_sheet` primitive the AI's
    own basis uses). It is no longer true, and was never actually correct,
    for the roster-max half: a human's `/sign-free-agent` signing is an
    immediate-effect, CURRENT-season write (`start_season=ctx.season`),
    while the AI's own roster-count gate is a NEXT-season predicate built
    for an AI-accepted offer that starts at `ctx.next_season` -- two
    genuinely different questions that happen to share a name. Rounds 2,
    3, and 4 each tried to make `physical_roster_count` "mirror" the AI's
    formula anyway and each shipped a real roster-overfill regression
    doing it (see the roster-max gate's own section comment below for the
    full history). Round 4 replaced that borrowed formula with a direct
    current-state count that asks no season-basis question at all; this
    docstring is corrected to stop describing that half as a mirror of
    the AI's own next-season formula, matching `SigningLegalityBasis`'s
    own corrected docstring above.

    **Round 2 blocking-issue fix.** Round 1's gate reused `signing_
    decisions.apron2_headroom` (correct) but fed it `cap_settings.apron_1
    - cap_summary.used` -- a hand-rolled re-derivation, NOT the real
    `SeasonCapProjection.apron1_room` `apron2_headroom`'s own docstring
    names as its REQUIRED input. The two disagree by exactly `minimum_
    filler = max(0, roster_min - roster_count) * min_salary` (`capsheet.
    compute_team_season_cap_sheet`, item 7) -- `cap_summary.used` never
    includes it, and `minimum_filler` is always `>= 0`, so the old gate
    was always looser than or equal to the AI's real gate, worst on a thin
    roster (a human GM's most likely situation). Reproduced live (critic,
    round 2) and again in `tests/integration/test_gm_command_service.py::
    test_sign_free_agent_uses_the_same_apron1_room_basis_the_ai_path_does`:
    a 1-player, contract-less roster computed `headroom=$200,000,000`
    under the old formula vs `$183,200,000` under this fix (the
    `minimum_filler` gap: `$16,800,000` = `(roster_min=14) * (min_salary
    =$1.2M)`), and a `$184,200,000` signing was WRONGLY accepted by the
    old gate.

    **The fix**: build a real `SeasonCapProjection` for `(team.id, season)`
    via `capsheet.compute_team_season_cap_sheet` -- the same pure, single-
    season primitive `capsheet_service.project_league_cap_sheets` (the AI
    path's own basis, `gm_service.py::load_gm_inputs`'s `apron1_room_by_
    team`) calls once per `(team, season)` -- instead of re-deriving
    `apron1_room` from `cap_summary.used` by hand. Not routed through
    `project_league_cap_sheets` itself: that function is a whole-30-team,
    5-season-horizon load (its own docstring: "a single-team entry point
    would still need every one of the bulk-loader reads... and would
    silently re-run them once per team if a caller looped this module 30
    times") -- wasteful for a single human signing check. `committed_
    salary`/`dead_cap`/`cap_holds` come straight off `cap_service.
    get_cap_summary` -- `CapSheetTeamSeasonInputs.committed_salary`'s own
    docstring states this is "identical to `contract_repo.get_committed_
    salary`'s own sum" by construction (Decision P2-17's load-bearing
    cross-check), so this is not a second formula for those three terms,
    only for `roster_count`/`team_option_salary`/`player_option_salary`,
    derived below from the SAME active-contract-years data the roster-
    count gate also needs (one fetch, two uses).

    Raises `ValidationError` naming which rule failed, BEFORE `sign_free_
    agent`'s caller does any write -- an illegal signing never reaches
    `apply_signing`, so there is no partial state to roll back."""
    cap_settings = await league_repo.get_cap_settings(ex, league_id, season)
    if cap_settings is None:
        raise NotFoundError(f"No league_cap_settings row for league {league_id}, season {season}")

    # Active contracts + their per-season years -- the cap-legality gate's
    # `roster_count`/`team_option_salary`/`player_option_salary`
    # derivation below needs these; the roster-max gate (just below) does
    # NOT, by design (see its own comment) -- it counts physical
    # `RosterStatus.ACTIVE` occupancy directly, no contract data at all.
    active_contracts = await contract_repo.list_contracts_for_team(
        ex, team.id, status=ContractStatus.ACTIVE
    )
    contract_by_player: dict[int, PlayerContract] = {}
    for c in active_contracts:
        years = tuple(await contract_repo.list_contract_years_for_contract(ex, c.id))
        contract_by_player[c.player_id] = PlayerContract(contract=c, years=years)

    # --- Roster-max gate -----------------------------------------------
    # **Round-4 fix (this round).** Rounds 2 and 3 each tried to answer
    # this gate's question by borrowing `gm_service._next_season_roster_
    # count`/`_occupies_next_season` -- a NEXT-season predicate the AI's
    # `run_free_agency` loop uses because an AI-accepted offer takes effect
    # at `ctx.next_season`. A human's `/sign-free-agent` signing does not:
    # `sign_free_agent` below writes `start_season=ctx.season` (the SAME
    # current-season basis the cap-legality gate right after this one
    # already uses, correctly, since round 2). Passing `season` as the
    # next-season predicate's argument (round 2) excluded anyone whose
    # contract had already lapsed before `season` -- unreachable in
    # practice, since nothing outside `tools/` ever advances `current_
    # season` without rolling such players over first, but wrong in
    # principle. Passing `season + 1` (round 3, to "match the AI path
    # exactly") was worse and WAS reachable in practice: `_occupies_next_
    # season` excludes anyone whose contract's last covered season equals
    # `next_season - 1`, which became `season` itself -- every player in
    # the final year of their contract, a common, live, in-season state --
    # so those players vanished from the gate's count entirely. Reproduced
    # live (round-4 critic): 15 ACTIVE players (5 in their contract's
    # final year, 10 signed further out) on a `roster_max=15` team let 30
    # consecutive signings all succeed, ending at 45 ACTIVE bodies.
    #
    # The fix does not pick a THIRD season basis -- it sidesteps the
    # season-basis question entirely. "Will this team have more than
    # `roster_max` bodies on it starting right now" is a CURRENT-state
    # occupancy question, not a projection over contract years at all: the
    # answer is just how many players are physically `RosterStatus.ACTIVE`
    # on this team at this instant, direct from `players`, the same first-
    # level filter `load_gm_inputs` itself documents as "a roster is every
    # player with roster_status == ACTIVE on this team" and the same
    # `RosterStatus.ACTIVE` this module already reads at contract-signing
    # time elsewhere (`roster_service.sign`). No contract-year lookup, no
    # retirement-age cutoff, no `next_season`/`season + 1` arithmetic --
    # those are exactly the ingredients the last two rounds each got wrong
    # in a different way. This also fixes round 2's narrower (unreachable
    # in production, but still real) lapsed-contract gap for free, since a
    # direct status count never asks a contract-year question at all.
    players = await player_repo.list_players_for_team(ex, team.id)
    physical_roster_count = sum(1 for p in players if p.roster_status is RosterStatus.ACTIVE)

    # --- Cap-legality gate ----------------------------------------------
    # `capsheet`'s own `roster_count`/`team_option_salary`/`player_option_
    # salary` basis: distinct players with an ACTIVE contract-year that
    # actually COVERS `season` (a financial fact, deliberately different
    # from the physical-occupancy basis just above -- a contract-less
    # `ACTIVE` player counts toward `physical_roster_count` but not toward
    # this `roster_count`, and the two must never be merged into one
    # number).
    roster_count = 0
    team_option_salary = 0
    player_option_salary = 0
    for c in active_contracts:
        year = next(
            (y for y in contract_by_player[c.player_id].years if y.season == season), None
        )
        if year is None:
            continue
        roster_count += 1
        if year.option_type is OptionType.TEAM:
            team_option_salary += year.salary
        elif year.option_type is OptionType.PLAYER:
            player_option_salary += year.salary

    cap_summary = await cap_service.get_cap_summary(
        ex, league_id=league_id, team_id=team.id, season=season
    )
    projection_inputs = capsheet.CapSheetTeamSeasonInputs(
        team_id=team.id,
        season=season,
        committed_salary=cap_summary.committed_salary,
        dead_cap=cap_summary.dead_cap,
        cap_holds=cap_summary.cap_holds,
        team_option_salary=team_option_salary,
        player_option_salary=player_option_salary,
        roster_count=roster_count,
    )
    # `projected=False` -- `cap_settings` above is a real `league_cap_
    # settings` row for this exact season (`league_repo.get_cap_settings`
    # raised `NotFoundError` above otherwise), never a `project_cap_
    # settings`-grown one.
    projection = capsheet.compute_team_season_cap_sheet(
        projection_inputs, cap_settings, projected=False, include_holds=False
    )
    headroom = signing_decisions.apron2_headroom(cap_settings, projection.apron1_room)

    return SigningLegalityBasis(
        physical_roster_count=physical_roster_count,
        apron2_headroom=headroom,
        roster_max=cap_settings.roster_max,
    )


async def _check_free_agent_signing_legal(
    ex: Executor, *, league_id: int, team: Team, season: int, annual_salary: int
) -> None:
    """Thin raising wrapper around `_compute_signing_legality_basis` --
    both gates it can fail on are named in `SigningLegalityBasis`, so no
    second `cap_settings` fetch is needed here. Raises `ValidationError`
    naming which rule failed, BEFORE `sign_free_agent`'s caller does any
    write -- an illegal signing never reaches `apply_signing`, so there is
    no partial state to roll back."""
    basis = await _compute_signing_legality_basis(
        ex, league_id=league_id, team=team, season=season
    )
    if basis.physical_roster_count >= basis.roster_max:
        raise ValidationError(
            f"Team {team.id} already has {basis.physical_roster_count} players occupying a "
            f"roster slot (roster_max is {basis.roster_max}) -- waive or release "
            "someone before signing another free agent."
        )
    if annual_salary > basis.apron2_headroom:
        raise ValidationError(
            f"That signing (${annual_salary:,}/yr) would exceed team {team.id}'s apron_2 "
            f"headroom (${basis.apron2_headroom:,}) for season {season} -- illegal under the "
            "league's cap rules."
        )


async def _active_contract_for_team(
    ex: Executor, *, player_id: int, team_id: int
) -> Contract:
    contracts = await contract_repo.list_contracts_for_player(ex, player_id)
    active = next((c for c in contracts if c.status is ContractStatus.ACTIVE), None)
    if active is None:
        raise NotFoundError(f"Player {player_id} has no active contract.")
    if active.team_id != team_id:
        raise ValidationError(
            f"Player {player_id}'s active contract belongs to team {active.team_id}, "
            f"not team {team_id}."
        )
    return active


# ---------------------------------------------------------------------------
# Waive / release
# ---------------------------------------------------------------------------


async def waive_player(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    player_id: int,
    actor_user_id: int,
    stretch: bool = False,
) -> list[DeadCapCharge]:
    """RMLP §6.6 (build step 7), one of the three human-command entry
    points (`tests/unit/test_roster_mutation_lock_discipline.py`'s
    `RMLP_ENTRY_POINTS`). `_resolve_and_authorize`'s own `require_phase`
    call (a POOL read, before any transaction) stays as the fast, honest
    early rejection; `open_league_gate` is the first statement of this
    function's OWN transaction, and `require_phase` is re-checked against
    `gate.phase` (read under the gate) -- closing the disclosed league
    TOCTOU for this command. `_active_contract_for_team` moves INSIDE the
    transaction as the discovery read. `waived_in_season` is
    `gate.season`, not `ctx.season` -- the gate's season is the only one
    read under the lock."""
    await _resolve_and_authorize(
        ex,
        league_id=league_id,
        team_id=team_id,
        actor_user_id=actor_user_id,
        command="waive",
        refuse_under_auto_pilot=True,
    )
    async with transaction(ex) as conn:
        gate = await roster_lock.open_league_gate(
            conn, league_id=league_id, mode=roster_lock.LockMode.SHARED
        )
        require_phase(gate.phase, *command_gates.COMMAND_GATES["waive"])
        await league_claim.check_claim(conn, gate=gate, league_id=league_id)
        contract = await _active_contract_for_team(conn, player_id=player_id, team_id=team_id)
        return await roster_service.waive(
            conn,
            league_id=league_id,
            contract_id=contract.id,
            waived_in_season=gate.season,
            stretch=stretch,
        )


async def release_player(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    player_id: int,
    actor_user_id: int,
) -> Contract:
    """RMLP §6.6 (build step 7) -- `waive_player`'s own twin, identical
    treatment (see that function's docstring)."""
    await _resolve_and_authorize(
        ex,
        league_id=league_id,
        team_id=team_id,
        actor_user_id=actor_user_id,
        command="release",
        refuse_under_auto_pilot=True,
    )
    async with transaction(ex) as conn:
        gate = await roster_lock.open_league_gate(
            conn, league_id=league_id, mode=roster_lock.LockMode.SHARED
        )
        require_phase(gate.phase, *command_gates.COMMAND_GATES["release"])
        await league_claim.check_claim(conn, gate=gate, league_id=league_id)
        contract = await _active_contract_for_team(conn, player_id=player_id, team_id=team_id)
        return await roster_service.release(
            conn, league_id=league_id, contract_id=contract.id, released_in_season=gate.season
        )


# ---------------------------------------------------------------------------
# Sign a free agent
# ---------------------------------------------------------------------------


async def sign_free_agent(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    player_id: int,
    actor_user_id: int,
    years: int,
    annual_salary: int,
    signing_method: SigningMethod = SigningMethod.CAP_SPACE,
) -> None:
    """Calls `gm_service.apply_signing` directly -- Decision P5-39's own
    "a human's signing calls `gm_service.apply_signing`" text, verbatim.
    Restricted to `RosterStatus.FREE_AGENT` (JC-4's own wording: "sign A
    FREE AGENT," not any player) -- a `DRAFT_ELIGIBLE` prospect is `apply_
    draft`'s concern, not this command's, and a player still `ACTIVE` on
    another team already fails `roster_service.sign`'s own "one active
    contract" guard with a less specific error; this check gives a human
    the real reason first.

    **Round-5 critic's blocking issue B1 -- the legality check and the
    write were not atomic.** `roster.py` passes the POOL (not a single
    connection) all the way down this call chain, and before this fix the
    roster-max/cap-legality read (`_check_free_agent_signing_legal`) and
    `apply_signing`'s own write ran as two separate, unwrapped statements
    against that pool -- two (or more) concurrent `/sign-free-agent` calls
    on the SAME team could each read "room available" before either one's
    write landed, and both writes would succeed. Reproduced live by the
    round-5 critic: 14 ACTIVE players on a `roster_max=15` team, 5
    concurrent `sign_free_agent` calls fired via `asyncio.gather` -- all 5
    accepted, final roster count 19. **Fixed** by wrapping the legality
    check AND `apply_signing`'s write in one `transaction(ex)` block, with
    `team_repo.lock_team_row` (renamed from `get_team_for_update`, RMLP
    Decision 1 -- now `FOR NO KEY UPDATE`, see that function's docstring)
    taking a row lock on the team FIRST, before either gate reads anything -- a second
    concurrent call on the same team blocks on that lock acquisition until
    the first call's transaction commits (or rolls back), and only then
    re-reads roster/cap state that reflects the first call's write (Postgres'
    default READ COMMITTED isolation re-reads committed state per
    statement, so the second call's post-lock reads are never stale).
    Mirrors `phase/service.py::advance`'s identical "lock the row this
    check-then-write critical section depends on, inside the same
    transaction, before reading it" pattern (`league_repo.get_phase_state_
    for_update`). `apply_signing` already opens its own `transaction(ex)`
    internally -- passing the already-open `Connection` down (not the
    pool) makes that a savepoint nest inside this function's own
    transaction, not a second independent one, per `transaction()`'s own
    documented Connection-in-Connection behavior. New regression test:
    `tests/integration/test_gm_command_service.py::
    test_concurrent_sign_free_agent_calls_do_not_overfill_the_roster` --
    the critic's exact repro (14/15, 5 concurrent `asyncio.gather` calls),
    asserting exactly 1 succeeds and the final roster never exceeds
    `roster_max`.

    **Round-6 critic's blocking issue B1 -- the team-row lock above does
    nothing for two DIFFERENT teams racing to sign the SAME free agent.**
    Each call locks its OWN team's row, so two calls for two different
    `team_id`s never contend on the SAME lock -- the free-agent status
    check below (`player.roster_status is not RosterStatus.FREE_AGENT`)
    is a POOL read, outside any transaction, purely a fast, honest early
    rejection for the common case; it is not, and was never meant to be,
    what makes this safe under concurrency. The real fix lives one call
    deeper: `roster_service.sign` now takes its own `SELECT ... FOR
    UPDATE` lock on the PLAYER row and re-checks "no existing active
    contract" AFTER acquiring it, inside the same transaction this
    function opens below (`player_repo.lock_player_row`, renamed from
    `get_player_for_update`,
    docs/design/phase-5-discord-presentation.md §43.6's B1 fix) -- so a
    second concurrent signing of this SAME player, from a different team,
    blocks on THAT lock and correctly rejects itself once the first
    commits, even though the two calls never contend for the same team
    lock. See `roster_service.sign`'s own docstring for the mechanism.

    **RMLP §6.6 (build step 7).** The bare `team_repo.lock_team_row` call
    above is replaced by one `roster_lock.acquire` call carrying BOTH
    `{team_id}` and `{player_id}` (`sign()`'s own `acquire` re-acquisition,
    reached via `apply_signing`, is then a no-op -- idempotent nesting).
    `_check_free_agent_signing_legal` is fed the helper's LOCKED team row.
    `require_phase` is re-checked against `gate.phase`; the free-agent
    status check below (kept as the fast, honest early pool read for the
    common case) is ALSO re-checked against the helper's LOCKED player row
    -- today that early check is a pool read outside any transaction
    (§43.6 itself calls it "a fast, honest early rejection"), and the
    locked re-check is what makes it authoritative rather than merely
    advisory. `gate.season` is the signing's season basis, not `ctx.
    season`."""
    if years < 1:
        raise ValidationError("years must be at least 1")
    if annual_salary < 0:
        raise ValidationError("annual_salary must be non-negative")
    ctx = await _resolve_and_authorize(
        ex,
        league_id=league_id,
        team_id=team_id,
        actor_user_id=actor_user_id,
        command="sign_free_agent",
        refuse_under_auto_pilot=True,
    )
    player = await player_repo.get_player(ex, player_id)
    if player is None or player.league_id != league_id:
        raise NotFoundError(f"No player {player_id} in league {league_id}")
    if player.roster_status is not RosterStatus.FREE_AGENT:
        raise ValidationError(
            f"Player {player_id} is not a free agent (status={player.roster_status.value})."
        )
    # Inert placeholder -- see module docstring. `apply_signing` never
    # reads `.record`; this only satisfies `SigningExecution`'s required
    # field with a value that is honest about being nobody's decision
    # (`terms=()` means `reconstructs()` holds trivially, not because a
    # real AI evaluation produced it). Built before the locked section
    # below -- it only reads `ctx.team`'s already-resolved philosophy/
    # timeline, neither of which the concurrency race below can affect.
    placeholder_record = DecisionRecord(
        team_id=team_id,
        philosophy=ctx.team.gm_philosophy,
        window=_TIMELINE_TO_WINDOW[ctx.team.timeline],
        subject=f"player:{player_id}",
        verdict="human_sign",
        score=0.0,
        threshold=0.0,
        terms=(),
    )
    # Blocking issue B1 (critic, round 5): lock the team row FIRST, inside
    # one transaction that also carries the write, so a second concurrent
    # `/sign-free-agent` on the same team can't read "room available"
    # before this call's write commits. Blocking issue 4 (critic, round 1)
    # is the legality check itself -- the AI's own decision layer already
    # applies an equivalent gate upstream of `apply_signing`; the human
    # path had none before that round. Both now run, and can reject,
    # before any write happens, inside the SAME locked transaction.
    async with transaction(ex) as conn:
        gate = await roster_lock.open_league_gate(
            conn, league_id=league_id, mode=roster_lock.LockMode.SHARED
        )
        require_phase(gate.phase, *command_gates.COMMAND_GATES["sign_free_agent"])
        await league_claim.check_claim(conn, gate=gate, league_id=league_id)
        lock_set = await roster_lock.acquire(
            conn, league_id=league_id, teams=[team_id], players=[player_id]
        )
        locked_team = lock_set.teams_by_id[team_id]
        locked_player = lock_set.players_by_id[player_id]
        if locked_player.roster_status is not RosterStatus.FREE_AGENT:
            raise ValidationError(
                f"Player {player_id} is not a free agent "
                f"(status={locked_player.roster_status.value})."
            )
        await _check_free_agent_signing_legal(
            conn,
            league_id=league_id,
            team=locked_team,
            season=gate.season,
            annual_salary=annual_salary,
        )
        execution = SigningExecution(
            team_id=team_id,
            player_id=player_id,
            years=years,
            annual_salary=annual_salary,
            start_season=gate.season,
            signing_method=signing_method,
            record=placeholder_record,
        )
        await apply_signing(conn, execution, league_id=league_id)


# ---------------------------------------------------------------------------
# The draft pick (docs/design/phase-5-stage-d-prime-draft.md, P5-98.g/h)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftPickOutcome:
    """Everything `/draft` has to answer with, from BOTH of `draft_pick`'s
    transactions: the pick the person actually made (committed in the
    first), and what the AI resume did or did not do afterwards (the
    second). Two facts rather than one because the resume can legitimately
    not run at all -- a `SKIPPED` cycle sits beside a real, committed
    `pick`, and a reply that read only the cycle would tell that person
    nothing happened."""

    pick: DraftPick
    """The `draft_picks` row `apply_human_draft_pick` spent -- its
    `round`/`pick_number`/`current_team_id` are what the confirmation and
    the announcement name. Returned by the applier rather than re-read,
    because after the write it is filtered out of the eligible list."""

    prospect_id: int
    cycle: DraftCycleOutcome


async def _resume_the_ai_draft(
    ex: Executor, *, league_id: int, gate: roster_lock.LeagueGate
) -> DraftCycleOutcome:
    """`draft_pick`'s SECOND transaction, and the whole of it -- extracted
    because every exit it has is a returned `DraftCycleOutcome` and nothing
    it can do may reach `draft_pick`'s caller as an exception (round-1
    critic's blocking issues B1 and B2).

    **The human's pick has already committed when this is entered.** That
    single fact settles both of the rules below.

    **B1 -- it sets its own `lock_timeout`, as its first statement.** This
    transaction opens no league gate (a draft pick mutates no RMLP table,
    and the gate may only be opened as the first statement of an OUTERMOST
    transaction, which this is not) -- so nothing sets `lock_timeout` for it
    unless it does, and the server default is wait-forever. `check_claim`'s
    `leagues ... FOR SHARE` on the very next line is a real blocking lock: a
    press's uncommitted `UPDATE leagues` held that row for 8 seconds in the
    critic's repro, and this transaction waited all 8 -- pinning a pool
    connection and a person's deferred `/draft` reply to somebody else's
    write, unbounded. `run_draft_cycle` bounded its own `drafts` wait the
    same way for the same reason (Decision P5-98.g, `roster_lock.set_lock_
    timeout`'s own docstring); this is the sibling statement that was
    missing. `tests/unit/test_roster_mutation_lock_discipline.py`'s check 10
    pins it statically, with a mutation twin.

    **B2 -- every failure here is a SKIPPED resume, never a raise.** Before
    this fix any tx2 failure escaped `draft_pick` after tx1 had committed:
    `cogs/draft.py`'s `except` then answered "no change was made" (false --
    the pick stands), and, because that `except` returns before
    `draft_feed.notify_on_the_clock`, the pick was never announced in
    `#games` and the next person was never pinged. So: contention (the
    `leagues` row above, the `drafts` row inside the cycle, or a claim held
    by a press) is `SKIPPED(RESUME_CONTENDED)`, and anything else at all is
    logged with its traceback and returned as `SKIPPED(RESUME_FAILED)`.
    `BaseException` -- a cancellation, a `KeyboardInterrupt` -- is
    deliberately NOT caught: a cancelled command has no reply to protect.

    **M-A (round-2 critic) -- a failure AFTER the resume committed is not a
    skipped resume.** The `try` above wraps the `async with` block, so it
    also catches whatever the block's own exit raises: the COMMIT, and then
    `data/db.py::transaction`'s `pool.release(conn)`. An `InterfaceError`
    from that release (a connection that died under it, reproduced by the
    critic) used to be reported as `SKIPPED(RESUME_FAILED)` while the AI's
    picks were already durable -- so those picks were never announced, no
    `DecisionRecord` was written for them (`draft_pick` writes them from
    `cycle.records`, which was empty), the next person was never pinged, and
    `#games` was told the draft was paused when it was not. The outcome is
    therefore captured into a local the moment it exists, INSIDE the block,
    and either handler that finds it set logs the post-commit failure and
    answers with the real outcome.

    **Disclosed residue:** a failure of the COMMIT itself lands in the same
    handler as a failure of the release after it, and the two are not
    distinguishable from the exception. This function answers with the
    captured outcome either way, so a commit that rolled back would be
    announced as picks that did not happen. Nothing durable is wrong in that
    case -- the board is re-derived from the database by the next `/draft`
    or `/advance`, and no record is written for a pick the database does not
    have -- but the one announcement would be. The alternative (reporting a
    skip) is wrong in the far likelier direction, on picks that really are
    committed.

    The resume is best-effort by design, not by accident: the eligible list
    is re-derived from the database on every call, so the next `/draft` (or
    Stage E's `/advance`) resumes the draft from wherever this left it."""
    # Assigned the moment an outcome exists, INSIDE the block -- see the
    # "M-A" paragraph above. A handler that finds this set is looking at a
    # failure of the transaction's own exit, not of the resume.
    outcome: DraftCycleOutcome | None = None
    try:
        async with transaction(ex) as conn:
            await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
            try:
                state = await league_claim.check_claim(
                    conn, gate=gate, league_id=league_id, require_phase_agreement=False
                )
            except ConcurrencyError:
                outcome = DraftCycleOutcome(
                    state=DraftCycleState.SKIPPED, reason=DraftCycleReason.CLAIM_HELD
                )
            else:
                # The phase is asserted against the gate's own `(phase,
                # season)` rather than re-derived: tx1 already required DRAFT
                # under the gate, so "still what tx1 decided against" is the
                # whole question, and a season that moved is the same
                # league-advanced fact wearing a different column.
                if (state.current_phase, state.current_season) != (gate.phase, gate.season):
                    outcome = DraftCycleOutcome(
                        state=DraftCycleState.SKIPPED, reason=DraftCycleReason.PHASE_MOVED
                    )
                else:
                    outcome = await run_draft_cycle(conn, league_id=league_id, season=gate.season)
        return outcome
    except (ConcurrencyError, *roster_lock.LOCK_CONTENTION_EXCEPTIONS) as exc:
        if outcome is not None:
            log.exception(
                "draft_pick: league %s's AI resume finished and then its transaction's "
                "own exit failed -- answering with the outcome it produced",
                league_id,
            )
            return outcome
        # `ConcurrencyError` from `run_draft_cycle`'s own `drafts`-row
        # mapping; a raw 55P03/40P01 from the `leagues` lock above, which
        # this transaction maps itself because `maps_lock_contention` (on
        # `draft_pick`) maps at the whole-call level -- too late to be a
        # skip rather than a raise.
        log.warning(
            "draft_pick: the AI resume for league %s lost a lock and was skipped: %s",
            league_id,
            exc,
        )
        return DraftCycleOutcome(
            state=DraftCycleState.SKIPPED, reason=DraftCycleReason.RESUME_CONTENDED
        )
    except Exception:
        if outcome is not None:
            log.exception(
                "draft_pick: league %s's AI resume finished and then its transaction's "
                "own exit failed -- answering with the outcome it produced",
                league_id,
            )
            return outcome
        log.exception(
            "draft_pick: the AI resume for league %s failed after the human's pick "
            "had already committed -- the pick stands and the draft has stopped",
            league_id,
        )
        return DraftCycleOutcome(
            state=DraftCycleState.SKIPPED, reason=DraftCycleReason.RESUME_FAILED
        )


@roster_lock.maps_lock_contention
async def draft_pick(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    prospect_id: int,
    actor_user_id: int,
) -> DraftPickOutcome:
    """`/draft`'s service half: one human pick, then the AI draft run
    forward to the next person's turn (Decisions P5-98.g and P5-98.h).

    **Two transactions, and the split is forced, not stylistic.**
    `open_league_gate` may be opened once per transaction and must be the
    first statement of the outermost one (`roster_lock.py:203-224`), and
    the human's pick has to be durable before the AI reads the board it
    decides against. If the process dies between them the pick stands and
    the next `/draft` (or Stage E's `/advance`) resumes -- the eligible
    list is re-derived from the database every call and no truncation
    parameter is threaded anywhere, which is what makes the sequence
    idempotent under a crash.

    **tx1, in the canonical lock order -- gate -> `leagues FOR SHARE` ->
    `drafts FOR UPDATE`, the `drafts` row always last (P5-98.g).** The gate
    is taken for its `SET LOCAL lock_timeout` and its post-gate
    `(phase, season)` read, NOT because a draft pick mutates an RMLP table
    (it mutates none) -- `offer_service.veto`'s precedent verbatim, which is
    why this function joins `RMLP_ENTRY_POINTS` in the same commit or
    discipline check 6 fails. `require_phase` is re-checked against
    `gate.phase`, the same double-check `waive_player` makes, and
    `league_claim.check_claim` refuses while Stage E's advance claim is held
    (P5-90 rule 2). `gate.season` is the only season anything below reads.

    **tx2 is best-effort and cannot fail this command** -- see `_resume_the_
    ai_draft`, which is the whole of it. It re-validates the claim and the
    phase before it locks (a press can claim the league in the gap between
    the two transactions -- P5-90 rule 2's race, arriving through a door
    P5-90 did not cover), bounds its own waits, and turns every failure it
    can meet into a `SKIPPED` reason rather than an exception, because the
    human's pick is already committed by the time it runs. **Those checks
    live there and not inside `run_draft_cycle`**: a Stage E press
    legitimately holds the claim while running the DRAFT hook, so a claim
    check inside the cycle would make the press refuse its own work.

    **A `DraftPickOutcome` is therefore returned for every path that gets
    past tx1's commit** (round-1 critic's blocking issue B2), which is what
    lets `cogs/draft.py` treat everything after that commit as a follow-up:
    the announcement in `#games` and the ping for the next person run on the
    `SKIPPED` paths too, and the reply tells the person which one they got.

    **The decision log is written after tx2 commits**, by this function and
    not by the cycle (P5-98.h): `_write_decision_records` does a blocking
    `open()`/`write`, which must not happen inside a transaction holding the
    `drafts` row. `run_phase_hook` writes its own the same way, with its own
    `seq` -- `seq` is unique within one call's writes, never across the
    artifact file, so starting at 0 here is the same contract the hook has.

    **No `DecisionRecord` is written for the human's own pick** (P5-98.e):
    `apply_human_draft_pick` writes a `league_transactions` row instead, and
    the records below are the AI's, from the resume."""
    await _resolve_and_authorize(
        ex,
        league_id=league_id,
        team_id=team_id,
        actor_user_id=actor_user_id,
        command="draft",
        # False: NOT because /draft is allowed under auto-pilot -- it is
        # refused, one layer deeper and with its own two messages, by
        # GM-3's branch inside `gm_service.apply_human_draft_pick`, which
        # runs on `on_the_clock` inside the caller's own gated transaction
        # (a pool-read resolve here can't see who is actually on the
        # clock). A bare `True` here would double-refuse with the wrong
        # message; a bare omission would be a `TypeError`.
        refuse_under_auto_pilot=False,
    )
    async with transaction(ex) as conn:  # tx1 -- the human's pick
        gate = await roster_lock.open_league_gate(
            conn, league_id=league_id, mode=roster_lock.LockMode.SHARED
        )
        require_phase(gate.phase, *command_gates.COMMAND_GATES["draft"])
        await league_claim.check_claim(conn, gate=gate, league_id=league_id)
        try:
            draft = await draft_repo.get_draft_for_season_for_update(conn, league_id, gate.season)
        except roster_lock.LOCK_CONTENTION_EXCEPTIONS as exc:
            # Mapped AT the statement, like `run_draft_cycle`'s own copy:
            # `maps_lock_contention` (still applied above, as the backstop
            # for any other statement) would give this `_ENTITY_CONTENTION_
            # MESSAGE`, which names a team or a player -- the wrong noun for
            # a `drafts` row, OS-13's disclosed residue.
            raise ConcurrencyError(DRAFT_CONTENTION_MESSAGE) from exc
        if draft is None:
            raise NotFoundError(f"No draft for league {league_id}, season {gate.season}")
        pick = await apply_human_draft_pick(
            conn,
            league_id=league_id,
            season=gate.season,
            draft_id=draft.id,
            team_id=team_id,
            prospect_id=prospect_id,
            actor_user_id=actor_user_id,
        )

    cycle = await _resume_the_ai_draft(ex, league_id=league_id, gate=gate)  # tx2

    _write_decision_records(
        cycle.records,
        league_id=league_id,
        season=gate.season,
        phase=Phase.DRAFT,
        seq_start=0,
    )
    return DraftPickOutcome(pick=pick, prospect_id=prospect_id, cycle=cycle)


# ---------------------------------------------------------------------------
# Team strategy
# ---------------------------------------------------------------------------


async def set_strategy(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    actor_user_id: int,
    pace_target: int,
    offensive_scheme: OffensiveScheme,
    defensive_scheme: DefensiveScheme,
    help_aggression: int,
    three_point_bias: int,
    crash_oreb_bias: int,
) -> TeamStrategy:
    ctx = await _resolve_and_authorize(
        ex,
        league_id=league_id,
        team_id=team_id,
        actor_user_id=actor_user_id,
        command="gm_strategy",
        # A standing team setting, not a transaction (GM-7): delegating
        # your own decisions must not lock you out of configuring the
        # delegate.
        refuse_under_auto_pilot=False,
    )
    for name, value in (
        ("pace_target", pace_target),
        ("help_aggression", help_aggression),
        ("three_point_bias", three_point_bias),
        ("crash_oreb_bias", crash_oreb_bias),
    ):
        if not (0 <= value <= 100):
            raise ValidationError(f"{name} must be between 0 and 100, got {value}")
    async with transaction(ex) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        await _refuse_while_the_advance_is_claimed(conn, league_id=league_id)
        return await team_repo.upsert_team_strategy(
            conn,
            league_id=league_id,
            team_id=team_id,
            season=ctx.season,
            pace_target=pace_target,
            offensive_scheme=offensive_scheme,
            defensive_scheme=defensive_scheme,
            help_aggression=help_aggression,
            three_point_bias=three_point_bias,
            crash_oreb_bias=crash_oreb_bias,
            set_by=actor_user_id,
        )


# ---------------------------------------------------------------------------
# Player roles
# ---------------------------------------------------------------------------


async def set_role(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    actor_user_id: int,
    player_id: int,
    role: str,
    minutes: float,
    locked: bool = False,
    rationale: str | None = None,
) -> PlayerRole:
    """RC-20.4 (`docs/design/auto-gm-role-construction.md`): `minutes`
    replaces the old `touch_share: Range[0.0, 1.0]` parameter -- a Discord
    user typing a sensible-looking `0.30` for `touch_share` was really
    asking for 72 of a team's 240 minutes (RC-2.3), and after RC-20
    deleted `touch_share`'s two non-`player_roles` readers, that is
    *exactly and only* what this value means, so the parameter is
    expressed in the unit a GM actually reasons in. The column, the domain
    field and `role_repo.upsert_player_role`'s own `touch_share=` signature
    are unchanged -- `NUMERIC(5,4)` holds `48/240 = 0.2000` and
    `1/240 = 0.0042` exactly, so no migration is needed."""
    ctx = await _resolve_and_authorize(
        ex,
        league_id=league_id,
        team_id=team_id,
        actor_user_id=actor_user_id,
        command="gm_role",
        refuse_under_auto_pilot=False,
    )
    if not (0.0 <= minutes <= 48.0):
        raise ValidationError(f"minutes must be between 0.0 and 48.0, got {minutes}")
    touch_share = minutes / 240.0
    async with transaction(ex) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        await _refuse_while_the_advance_is_claimed(conn, league_id=league_id)
        # Read AFTER the claim check, on the same connection: with the `leagues`
        # row held `FOR SHARE` no press can claim until this commits, so the
        # roster this reads is the roster the write lands against.
        player = await player_repo.get_player(conn, player_id)
        if player is None or player.league_id != league_id:
            raise NotFoundError(f"No player {player_id} in league {league_id}")
        if player.team_id != team_id:
            raise ValidationError(f"Player {player_id} is not on team {team_id}'s roster.")
        return await role_repo.upsert_player_role(
            conn,
            league_id=league_id,
            team_id=team_id,
            season=ctx.season,
            player_id=player_id,
            role=role,
            # `player_roles.touch_share` is `NUMERIC(5,4)` (migration 005) --
            # asyncpg's numeric codec wants a `Decimal`, not a bare `float`;
            # `str(touch_share)` avoids float's own binary-representation
            # noise (e.g. `0.1 -> Decimal('0.1000000000000000055511151231...')`)
            # leaking into a column Discord's own float option already
            # rounds to a handful of decimal digits.
            touch_share=Decimal(str(touch_share)),
            rationale=rationale,
            assigned_by=actor_user_id,
            locked=locked,
        )


# ---------------------------------------------------------------------------
# Player directives
# ---------------------------------------------------------------------------


async def set_directive(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    actor_user_id: int,
    player_id: int,
    shot_diet: ShotDiet | None = None,
    usage_mode: UsageMode | None = None,
    defense_mode: DefenseMode | None = None,
) -> PlayerDirective:
    """A genuine PARTIAL update (critic's blocking issue 3, round 1).

    Before that fix, all five parameters defaulted to `None` and were
    forwarded verbatim into `role_repo.upsert_player_directive`'s `ON
    CONFLICT ... DO UPDATE SET <all 5> = EXCLUDED.<all 5>` -- so a `/set-
    directive` call naming only ONE field silently NULLed the other four
    previously-set fields, and a call naming ZERO fields (which Discord's
    own optional-parameter slash commands allow through unchanged) was
    accepted and wiped all five. The fix: reject a call where every field
    is unspecified up front (there is nothing to update, and letting
    Discord accept that as a "success" is misleading); otherwise fetch the
    existing row and merge -- a `None` argument means "the caller didn't
    touch this field," so it takes the EXISTING value forward, not a NULL
    overwrite.

    **PD-1 retype + PD-6/PD-7 field drop (docs/design/gm-command-surface.md,
    2026-09-23), superseding the two paragraphs this docstring used to
    carry about `PlayerDirective` never being read under `dba2/sim/` and
    about having no way to explicitly clear a field.** Both are now false:
    `shot_diet`/`usage_mode`/`defense_mode` are live sim inputs (PD-2/PD-3,
    PD-4, PD-5), and each is now a closed enum whose own neutral member
    (`BALANCED`) **is** the missing "clear" -- a `str | None` free-text
    parameter could never distinguish "didn't pass it" from "passed an
    explicit clear," but a required `Choice` parameter with `balanced` as
    one of its values can, and does. `role_mode` is gone entirely (PD-6,
    the column dropped in the same migration); `clutch_mode` is gone from
    THIS command's surface only (PD-7 -- the column and its enum stay,
    with no engine hook yet, so nothing here can honestly offer a choice
    that changes nothing)."""
    if all(v is None for v in (shot_diet, usage_mode, defense_mode)):
        raise ValidationError(
            "set_directive requires at least one of shot_diet/usage_mode/defense_mode -- "
            "all three were unspecified, so there is nothing to update."
        )
    await _resolve_and_authorize(
        ex,
        league_id=league_id,
        team_id=team_id,
        actor_user_id=actor_user_id,
        command="gm_directive",
        refuse_under_auto_pilot=False,
    )
    async with transaction(ex) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        await _refuse_while_the_advance_is_claimed(conn, league_id=league_id)
        player = await player_repo.get_player(conn, player_id)
        if player is None or player.league_id != league_id:
            raise NotFoundError(f"No player {player_id} in league {league_id}")
        if player.team_id != team_id:
            raise ValidationError(f"Player {player_id} is not on team {team_id}'s roster.")
        existing = await role_repo.get_player_directive(conn, league_id, player_id)
        return await role_repo.upsert_player_directive(
            conn,
            league_id=league_id,
            player_id=player_id,
            shot_diet=(
                shot_diet if shot_diet is not None else (existing.shot_diet if existing else None)
            ),
            usage_mode=(
                usage_mode
                if usage_mode is not None
                else (existing.usage_mode if existing else None)
            ),
            defense_mode=(
                defense_mode
                if defense_mode is not None
                else (existing.defense_mode if existing else None)
            ),
            clutch_mode=existing.clutch_mode if existing else None,
            set_by=actor_user_id,
        )


# ---------------------------------------------------------------------------
# Timeline override
# ---------------------------------------------------------------------------


async def set_timeline(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    actor_user_id: int,
    timeline: Timeline | None,
) -> Team:
    """`timeline=None` is `/timeline auto`'s own shape (Decision P5-67's
    "returning to auto is a plain write of `timeline_source = 'auto'`" --
    `teams.timeline` itself is left untouched, so the next classification
    pass reads it as `previous_by_team` and hysteresis damps the
    transition exactly as it damps any other). Logs a `league_transactions`
    row either way (Decision P5-68) -- `details` carries `from`/`to`/
    `source` so the transactions feed shows what actually changed, even
    when `to` (for `auto`) is "whatever the classifier decides next," not
    a value this command itself picks.

    **Both writes run inside one `transaction(ex)` block (critic's
    blocking issue 2, round 1).** Before this fix, `team_repo.set_team_
    timeline` and `txn_repo.create_league_transaction` were two separate,
    unwrapped statements against `ex` -- a failure in the second left the
    team's `timeline`/`timeline_source` committed with zero `league_
    transactions` rows, exactly the "a trade with no record" failure mode
    `apply_trade`'s own docstring says must never happen (`gm_service.py`,
    Decision P5-34/P5-74: "the trade's own `league_transactions` row is
    written last, inside this SAME transaction, so a trade and its record
    commit or roll back together"). Decision P5-68 makes `timeline_set`
    the log's second legitimate member; it gets that identical guarantee
    now, not a weaker one."""
    ctx = await _resolve_and_authorize(
        ex,
        league_id=league_id,
        team_id=team_id,
        actor_user_id=actor_user_id,
        command="gm_timeline",
        refuse_under_auto_pilot=False,
    )
    source = TimelineSource.AUTO if timeline is None else TimelineSource.MANUAL
    before = ctx.team.timeline
    async with transaction(ex) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        await _refuse_while_the_advance_is_claimed(conn, league_id=league_id)
        updated = await team_repo.set_team_timeline(
            conn, team_id, timeline=timeline, source=source
        )
        await txn_repo.create_league_transaction(
            conn,
            league_id=league_id,
            season=ctx.season,
            phase=ctx.league.current_phase,
            txn_type=TransactionType.TIMELINE_SET,
            actor_user_id=actor_user_id,
            details={
                "team_id": team_id,
                "from": before.value,
                "to": updated.timeline.value,
                "source": source.value,
            },
        )
    return updated


# ---------------------------------------------------------------------------
# Auto-pilot (GM-8/GM-9, docs/design/gm-command-surface.md)
# ---------------------------------------------------------------------------


async def set_auto_pilot(
    ex: Executor,
    *,
    league_id: int,
    team_id: int,
    actor_user_id: int,
    enabled: bool,
    philosophy: GmPhilosophy | None,
) -> Team:
    """`/gm auto`'s own writer (GM-8).

    **Authorization is the team's own CURRENT manager -- not the
    commissioner-or-manager union every other `/gm` setter gets through
    `_resolve_and_authorize`.** This is deliberately the one `/gm`
    subcommand that does not route through that function: GM-8's own
    decision is that going on auto-pilot is the team's OWN GM choosing to
    delegate their decisions, not a lever a commissioner should be able
    to pull for them. A commissioner who wants an AI team already has
    `/remove-manager`, which is what it is for. `permissions.require_
    team_manager` -- the same predicate the trade-offer surface's four
    binding actions use -- is asked directly, per Decision P5-38's
    two-halves discipline (the bot layer's own `ensure_team_manager`
    checks first; this is the authoritative re-check).

    **`enabled=True` with `philosophy=None` is rejected before any
    write.** Discord cannot express "required only if another option has
    a particular value", so the UI layer is not asked to. The rejected
    alternative -- defaulting silently to the column's existing value --
    would let a GM enable a delegate whose disposition they never saw.

    **`enabled=False` ignores `philosophy` entirely and keeps the team's
    LAST value.** Turning auto off does not reset `gm_philosophy`: the
    column keeps whatever it last held, which is also the philosophy the
    AI uses if the team is ever left unmanaged, and `/gm status` (GM-9)
    shows it only while auto-pilot is on, so a stale value never reads as
    an active setting when it is not one.

    **Flag, philosophy and a `league_transactions` row commit in ONE
    `transaction(ex)` block -- not optional.** §43.1 blocking issue 2 was
    exactly this bug on `/timeline` (a team write that committed with no
    audit row), fixed there by wrapping both in one transaction; a new
    write command that repeated it would be a known regression."""
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    team = await team_repo.get_team(ex, team_id)
    if team is None or team.league_id != league_id:
        raise NotFoundError(f"No team {team_id} in league {league_id}")
    permissions.require_team_manager(
        actor_user_id,
        commissioner_user_id=league.commissioner_user_id,
        team_manager_user_id=team.manager_user_id,
        team_id=team_id,
        action="gm_auto",
    )
    require_phase(league.current_phase, *command_gates.COMMAND_GATES["gm_auto"])
    if enabled and philosophy is None:
        raise ValidationError("Choose a philosophy to enable auto GM.")
    effective_philosophy = philosophy if enabled else team.gm_philosophy
    async with transaction(ex) as conn:
        await roster_lock.set_lock_timeout(conn, roster_lock.SHARED_LOCK_TIMEOUT)
        await _refuse_while_the_advance_is_claimed(conn, league_id=league_id)
        updated = await team_repo.set_auto_pilot(
            conn, team_id, auto_pilot=enabled, gm_philosophy=effective_philosophy
        )
        await txn_repo.create_league_transaction(
            conn,
            league_id=league_id,
            season=league.current_season,
            phase=league.current_phase,
            txn_type=TransactionType.AUTO_PILOT_SET,
            actor_user_id=actor_user_id,
            details={
                "team_id": team_id,
                "enabled": enabled,
                "gm_philosophy": effective_philosophy.value,
            },
        )
    return updated


@dataclass(frozen=True)
class GmStatus:
    """`/gm status`'s read-only bundle (GM-9) -- team-wide settings only,
    plus the one cross-cutting fact GM-4's rule makes worth surfacing
    here: how many trade offers are parked in this team's inbox right
    now. `parked_offers_addressed_to_team` is computed HERE, in the
    service, never in the panel builder -- `presentation/roster_
    commands.py`'s own "each a thin, direct field mapping... computing
    nothing" rule (Decision P5-5) survives because `build_gm_status_
    panel` receives a plain `int`."""

    team: Team
    strategy: TeamStrategy | None
    parked_offers_addressed_to_team: int


async def get_gm_status(ex: Executor, *, league_id: int, team_id: int) -> GmStatus:
    """The service half of `/gm status` (GM-9). A pure read: no
    authorization beyond the bot layer's own `ensure_command_legal`/
    `ensure_can_act_for_team` (reading your own settings is never
    illegitimate -- `gm_status`'s gate is `ALL_PHASES` for that reason),
    so this function takes no `actor_user_id` at all, matching `/offers`'
    own read-only shape.

    **`offer_repo` had no caller anywhere in this module before this
    function** (`:66-74`'s import tuple had no offers import) -- the
    parked-offer count GM-9 wants had no fetch site anywhere until now.
    Counts rows whose `to_team_id` is THIS team: an offer addressed to
    the GM is one GM-4's rule leaves them to answer; one they sent is not
    parked in their own inbox."""
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    team = await team_repo.get_team(ex, team_id)
    if team is None or team.league_id != league_id:
        raise NotFoundError(f"No team {team_id} in league {league_id}")
    strategy = await team_repo.get_team_strategy(ex, team_id, league.current_season)
    parked = await offer_repo.list_offers_for_team_both_sides(
        ex, league_id=league_id, team_id=team_id, status=TradeStatus.PROPOSED
    )
    parked_addressed_to_team = sum(1 for o in parked if o.to_team_id == team_id)
    return GmStatus(
        team=team, strategy=strategy, parked_offers_addressed_to_team=parked_addressed_to_team
    )


# ---------------------------------------------------------------------------
# /gm player (GM-10, docs/design/gm-command-surface.md)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GmPlayerView:
    """`/gm player`'s read-only bundle -- per-player detail `/gm status`
    deliberately excludes (thirty players will not fit a `PanelField`'s
    1024-character budget). `role`/`directive` are `None` when nobody has
    ever set one -- a real, distinct answer from an empty string, not an
    error."""

    player: Player
    role: PlayerRole | None
    directive: PlayerDirective | None


async def get_gm_player_view(
    ex: Executor, *, league_id: int, team_id: int, player_id: int
) -> GmPlayerView:
    """A pure read, same authorization shape as `get_gm_status` -- no
    `actor_user_id`, reading a player's own settings is never
    illegitimate for the requesting team's own roster. **Verifies
    `player.team_id == team_id`** (the same roster-membership check
    `set_directive` already has, `:1215-1216`) -- league membership alone
    is not enough, or any team code paired with any player id in the
    league would return that player's private directive verbatim (B2)."""
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    team = await team_repo.get_team(ex, team_id)
    if team is None or team.league_id != league_id:
        raise NotFoundError(f"No team {team_id} in league {league_id}")
    player = await player_repo.get_player(ex, player_id)
    if player is None or player.league_id != league_id:
        raise NotFoundError(f"No player {player_id} in league {league_id}")
    if player.team_id != team_id:
        raise ValidationError(f"Player {player_id} is not on team {team_id}'s roster.")
    role = await role_repo.get_player_role(ex, league_id, team_id, league.current_season, player_id)
    directive = await role_repo.get_player_directive(ex, league_id, player_id)
    return GmPlayerView(player=player, role=role, directive=directive)
