"""The trade-offer review surface's service layer -- propose, answer, counter,
veto and list human-facing trade offers (docs/design/phase-5-trade-offer-
review-surface.md, design rounds 1-4; this module is §12 step 4).

**Constraint that shapes everything below: an offer's terminal status is the
LAST write of its transaction (§5.3).** `TRADE_TRANSITIONS[ACCEPTED]` is
`frozenset()` -- `ACCEPTED` has no outgoing edge -- so a status written
before `apply_trade` runs can never be corrected to `EXPIRED` when
`apply_trade` then fails, and the row would commit `ACCEPTED` with no trade
behind it. Every check and `apply_trade` itself therefore run first; every
failure path writes its terminal status from `PROPOSED`, the only state with
outgoing edges.

**Who opens the RMLP gate, and why it is never the shared core.**
`propose`, `respond`, `counter` and `veto` each open their OWN transaction and
call `roster_lock.open_league_gate` as its first statement, in their OWN
body -- `tests/unit/test_roster_mutation_lock_discipline.py`'s check 6
resolves the enclosing function of every gate call statically, and
`roster_lock`'s F9 rule forbids a gate opened by a function that may be called
with an already-open transaction. `_resolve_locked` receives the open
`Connection` and the `LeagueGate` and never opens a gate itself. All four
carry `@roster_lock.maps_lock_contention` because the statements this module
adds (`leagues ... FOR SHARE`, `get_trade_offer_for_update`, the terminal CAS)
sit outside `roster_lock`'s own two mapping sites and would otherwise surface
a raw 55P03 after `lock_timeout`. `veto` opens the gate even though it never
takes a team lock: `open_league_gate` issues `SET LOCAL lock_timeout` for it,
and a gate-less veto would block forever behind a slow accept. (`open_league_
gate` is no longer the codebase's only issuer -- P5-98.g lifted the statement
into `roster_lock.set_lock_timeout` for the draft's gate-less `drafts` lock.
A gate-less veto would still be wrong: it needs the gate itself, for the AI
pass, not only for the timeout.)

**Lock order (§6.3): gate -> `leagues` row -> tier-1 teams -> tier-2 players ->
`trade_offers` (tier 3).** No transaction here touches a `trade_offers` row
before it has taken the team/player locks it will ever take -- `veto` takes
none, which is the only reason it may lock the offer row directly. The
league-wide expiry sweep (`offer_repo.expire_stale_offers_for_league`) is
called only from `list_offers`, in a transaction of its own.

**A human's side of a negotiation is a consent record, never a valuation
(§4.4).** `negotiation.respond` returns `ACCEPT` only when both records say
`"accept"`; feeding it an AI model of the human's own team would let a model
of a person veto that person's decision. The human side is therefore
`_consent_record` -- an inert `DecisionRecord` with `verdict="accept"`
(exact string equality is what `respond` tests), `score=0.0`, `terms=()`.
`evaluate_trade` is called only for an AI-managed team.

**Permissions are narrower than roster commands (§9).** A trade binds a
second team's assets, so propose/accept/reject/counter require the team's own
manager (`permissions.require_team_manager`); the commissioner's only lever
is `veto`.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from dba2.core.errors import ConcurrencyError, NotFoundError, ValidationError
from dba2.core.logging import get_logger
from dba2.data.db import Executor, transaction
from dba2.data.repositories import league_repo, offer_repo, team_repo
from dba2.domain.league import LeagueClaimState
from dba2.domain.phase import TradeStatus
from dba2.domain.player import Player, RosterStatus
from dba2.domain.team import Team
from dba2.domain.trade_offer import TradeOffer
from dba2.managers import negotiation
from dba2.managers.decisions import trade as trade_decisions
from dba2.managers.explain import DecisionRecord
from dba2.managers.negotiation import NegotiationStance
from dba2.managers.picks import rank_teams_by_projected_slot
from dba2.managers.snapshot import AssetBundle, LeagueSnapshot
from dba2.phase import command_gates, permissions
from dba2.phase.entity_states import TRADE_TRANSITIONS, is_legal_transition
from dba2.phase.service import require_phase
from dba2.services import gm_service, league_claim, roster_lock
from dba2.services.gm_service import _TIMELINE_TO_WINDOW, MAX_NEGOTIATION_ROUNDS, GmContext

log = get_logger(__name__)

MAX_PLAYERS_PER_SIDE = 3
"""Per direction. `/propose-trade`'s fixed slots (§4.1) and `max_outgoing_
players`'s default in `negotiation.respond` -- a real rule, not a UI
artifact: it bounds the tier-2 lock set a human's command can take."""
MAX_PICKS_PER_SIDE = 2

_NO_SUCH_OFFER = "No such offer."
"""ONE message for a missing offer and for an offer in another league --
a wording that distinguished them would let an unauthorized caller probe
which ids exist (§5.3 step 3)."""
# The claim's two messages moved to `league_claim` with the check that
# raises them (Decision P5-98.g step 2). No alias is kept here: nothing in
# this module or its tests read them by name, and a re-export nobody reads
# is just a second place for the wording to drift from.
_OFFER_CHANGED_MESSAGE = (
    "That offer changed while it was being answered -- nothing was applied, try again."
)


class OfferAction(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"


@dataclass(frozen=True)
class OfferResult:
    """What every write entry point returns. `offer` is the row as it stands
    when the call ends. **For `counter` it is the NEW row only when the
    counter was actually made** (`changed=True`; the countered original is
    then `offer.counters_offer_id`). When `counter` short-circuits in
    `_lock_offer_for_answer` it is the ORIGINAL row instead: already answered
    (`changed=False`, nothing written), or stale (this call wrote its
    `EXPIRED`, `changed=True`) -- so `changed=True` alone does not tell a
    caller which row it holds; check `offer.counters_offer_id`/`offer.status`.
    `changed` is `False` only when nothing was written because someone else
    had already answered the offer. `detail` is the human-readable reason a
    row went `EXPIRED`/`REJECTED` without a human saying so -- the status
    alone cannot carry "Marcus Webb is now listed INJURED"."""

    offer: TradeOffer
    changed: bool
    detail: str | None = None


@dataclass(frozen=True)
class TeamOffers:
    """`list_offers`' result: every `PROPOSED` offer `team_id` received and
    every one it sent, oldest first."""

    team_id: int
    received: tuple[TradeOffer, ...]
    sent: tuple[TradeOffer, ...]


@dataclass(frozen=True)
class _LockedWorld:
    """Everything read UNDER the gate, the `leagues` share lock and the
    tier-1/tier-2 locks -- the only snapshot anything below may believe."""

    snapshot: LeagueSnapshot
    ctx: GmContext
    lock_set: roster_lock.LockSet


@dataclass(frozen=True)
class _LockedOffer:
    offer: TradeOffer
    lock_set: roster_lock.LockSet


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _validate_package_ids(
    *,
    from_team_id: int,
    to_team_id: int,
    players_from_to: Sequence[int],
    players_to_from: Sequence[int],
    picks_from_to: Sequence[int],
    picks_to_from: Sequence[int],
) -> None:
    """Pure, no I/O, runs before any lock. Each rejection prevents a concrete
    corruption (§4.2 step b, B9):

    - **The same id twice in one list.** `_check_legality` computes roster
      counts and salaries off list LENGTHS (`trade.py:508-514`), so a
      duplicate double-counts and can pass `roster_max` illegitimately;
      `apply_trade`'s move list has no de-dup, and `create_trade_transaction`
      would write two asset rows for one player.
    - **The same id on both sides.** Nets to a no-op in legality, then
      `apply_trade` builds two contradictory moves and raises -- but only
      after taking every lock and running the whole evaluation.
    - **A team trading with itself.** Collapses to one team at the lock
      step and would write a transaction whose `from_team_id == to_team_id`.
    - **An empty side, or more than `MAX_PLAYERS_PER_SIDE` players /
      `MAX_PICKS_PER_SIDE` picks in one direction.**

    Deliberately NOT caught here: a player id typed into a pick slot or vice
    versa. Both are bare ints; that is a fact about the database and fails at
    the bundle build, which is where it can be told apart."""
    if from_team_id == to_team_id:
        raise ValidationError("A team cannot trade with itself.")

    lists = {
        "players you send": players_from_to,
        "players you get": players_to_from,
        "picks you send": picks_from_to,
        "picks you get": picks_to_from,
    }
    for label, ids in lists.items():
        seen: set[int] = set()
        for asset_id in ids:
            if asset_id in seen:
                raise ValidationError(f"Id {asset_id} is listed twice under {label}.")
            seen.add(asset_id)

    for label, sent, received in (
        ("player", players_from_to, players_to_from),
        ("pick", picks_from_to, picks_to_from),
    ):
        both = set(sent) & set(received)
        if both:
            raise ValidationError(
                f"{label.capitalize()} id {min(both)} is on both sides of the trade."
            )

    if not (players_from_to or picks_from_to):
        raise ValidationError("Name at least one player or pick you are sending.")
    if not (players_to_from or picks_to_from):
        raise ValidationError("Name at least one player or pick you are getting.")

    for label, ids, cap in (
        ("players you send", players_from_to, MAX_PLAYERS_PER_SIDE),
        ("players you get", players_to_from, MAX_PLAYERS_PER_SIDE),
        ("picks you send", picks_from_to, MAX_PICKS_PER_SIDE),
        ("picks you get", picks_to_from, MAX_PICKS_PER_SIDE),
    ):
        if len(ids) > cap:
            raise ValidationError(f"At most {cap} {label} per trade (you named {len(ids)}).")


def _package_of(
    players_from_to: Sequence[int],
    players_to_from: Sequence[int],
    picks_from_to: Sequence[int],
    picks_to_from: Sequence[int],
) -> dict[str, tuple[int, ...]]:
    """The four id tuples in `TradeOffer.package`'s own key shape."""
    return {
        "players_from_to": tuple(players_from_to),
        "players_to_from": tuple(players_to_from),
        "picks_from_to": tuple(picks_from_to),
        "picks_to_from": tuple(picks_to_from),
    }


def _all_player_ids(package: Mapping[str, Sequence[int]]) -> tuple[int, ...]:
    return tuple(package["players_from_to"]) + tuple(package["players_to_from"])


def _consent_record(team: Team, *, offer_id: int) -> DecisionRecord:
    """ENCODES A HUMAN'S CONSENT; MODELS NOTHING. A human's side of a
    `negotiation.respond` call is a fact ("this person clicked Accept / made
    this proposal"), not a prediction, so it is an inert record -- the same
    "placeholder `DecisionRecord` built purely to satisfy the dataclass"
    precedent `gm_command_service.sign_free_agent` sets.

    `verdict` MUST be the literal `"accept"`: `negotiation.respond` tests
    exact string equality (`negotiation.py:417-418`), and any other token
    reads to it as a refusal. `score=0.0` + `terms=()` is what carries "no
    opinion" (and makes `reconstructs()` trivially true). `window` is
    `_TIMELINE_TO_WINDOW[team.timeline]` -- a column rename over the
    persisted `teams.timeline`, NOT `managers/window.py`'s classifier, which
    would be an AI model of a human's team."""
    return DecisionRecord(
        team_id=team.id,
        philosophy=team.gm_philosophy,
        window=_TIMELINE_TO_WINDOW[team.timeline],
        subject=f"offer:{offer_id}",
        verdict="accept",
        score=0.0,
        threshold=0.0,
        terms=(),
    )


def _stance_for(
    initiator_record: DecisionRecord, responder_record: DecisionRecord
) -> tuple[NegotiationStance, str | None]:
    """This surface's ONE call into the stance function. `rounds_remaining`
    is `0` in v1: the AI cannot generate a counter (OS-4), so there is no
    round to spend, and at `0` a would-be `COUNTER` becomes `EXPIRE`
    (`negotiation.py:436-437`) -- `COUNTER` is unreachable. When OS-4 lifts
    that, the value becomes `MAX_NEGOTIATION_ROUNDS - chain_depth` and the
    consent record's `terms=()` needs re-deriving against the gap test at
    `negotiation.py:434`.

    Returns the stance plus, for `REJECT_ILLEGAL`, the failing record's
    verdict token (`"illegal:<rule>"`), which is what `illegal_token` stores."""
    stance = negotiation.respond(initiator_record, responder_record, rounds_remaining=0)
    token: str | None = None
    if stance is NegotiationStance.REJECT_ILLEGAL:
        token = next(
            r.verdict
            for r in (initiator_record, responder_record)
            if r.verdict.startswith("illegal:")
        )
    return stance, token


def _describe_missing_player(player: Player | None, *, player_id: int, owner_code: str) -> str:
    if player is None:
        return f"Player {player_id} was not found."
    name = f"{player.first_name} {player.last_name}"
    if player.roster_status in (RosterStatus.INJURED, RosterStatus.TWO_WAY):
        # F-D: `TeamSnapshot.roster` is ACTIVE-only and `evaluate_trade`
        # raises a bare ValueError on an outgoing player absent from it, so
        # such a player cannot be priced -- narrower than `apply_trade`
        # permits, in the safe direction.
        return (
            f"{name} is now listed {player.roster_status.value.upper()}; "
            "this deal can no longer be priced."
        )
    return f"{name} (id {player_id}) is not on {owner_code}'s active roster."


def _build_bundles(
    world: _LockedWorld,
    *,
    from_team_id: int,
    to_team_id: int,
    package: Mapping[str, Sequence[int]],
) -> tuple[AssetBundle, AssetBundle]:
    """`(from_team's outgoing bundle, to_team's outgoing bundle)`, resolved
    against the snapshot read under the locks. Raises `ValidationError`
    naming the first asset that is not where the package says it is -- the
    check that catches "you tried to trade their player as if it were
    yours", and the one that implements the INJURED/TWO_WAY restriction."""
    from_snapshot = world.snapshot.team_by_id(from_team_id)
    to_snapshot = world.snapshot.team_by_id(to_team_id)

    def players_of(team_snapshot, ids: Sequence[int]):
        roster = {rp.id: rp for rp in team_snapshot.roster}
        found = []
        for player_id in ids:
            rp = roster.get(player_id)
            if rp is None:
                raise ValidationError(
                    _describe_missing_player(
                        world.lock_set.players_by_id.get(player_id),
                        player_id=player_id,
                        owner_code=team_snapshot.team.code,
                    )
                )
            found.append(rp)
        return tuple(found)

    def picks_of(team_snapshot, ids: Sequence[int]):
        owned = {pick.id: pick for pick in team_snapshot.draft_picks}
        found = []
        for pick_id in ids:
            pick = owned.get(pick_id)
            if pick is None:
                raise ValidationError(
                    f"Pick {pick_id} is not currently owned by {team_snapshot.team.code}."
                )
            found.append(pick)
        return tuple(found)

    from_out = AssetBundle(
        players=players_of(from_snapshot, package["players_from_to"]),
        picks=picks_of(from_snapshot, package["picks_from_to"]),
    )
    to_out = AssetBundle(
        players=players_of(to_snapshot, package["players_to_from"]),
        picks=picks_of(to_snapshot, package["picks_to_from"]),
    )
    return from_out, to_out


def _first_illegal_side(
    world: _LockedWorld,
    *,
    from_team_id: int,
    to_team_id: int,
    from_out: AssetBundle,
    to_out: AssetBundle,
) -> tuple[int, str] | None:
    """`(team_id, "illegal:<rule>")` for the first side whose acceptance
    would be illegal, else `None`. Calls the AI's own `_check_legality` with
    the AI's own argument shape (`run_trade_window`, `gm_service.py:4154-
    4169`) -- pre-trade roster off the snapshot, `ctx.next_season`, its cap
    settings, that team's `room_by_team` -- because a human-specific rule
    would be either stricter (making AI offers unacceptable by construction)
    or looser (the D4-1 hole `apply_trade` deliberately leaves open) than
    the AI's (§5.2). Checked proposer side first, then the receiving side."""
    ctx = world.ctx
    for team_id, incoming, outgoing in (
        (from_team_id, to_out, from_out),
        (to_team_id, from_out, to_out),
    ):
        token = trade_decisions._check_legality(
            world.snapshot.team_by_id(team_id),
            incoming,
            outgoing,
            ctx.next_season,
            ctx.next_season_cap_settings,
            ctx.room_by_team[team_id],
        )
        if token is not None:
            return team_id, token
    return None


def _evaluate_ai_side(
    world: _LockedWorld, team_id: int, *, incoming: AssetBundle, outgoing: AssetBundle
) -> DecisionRecord:
    """A REAL `evaluate_trade` for an AI-managed team -- never called for a
    human's team (§4.4). Argument set is `gm_service.py:2239-2256`'s
    template, every value off the same `load_gm_inputs` result the AI path
    uses; `league_snapshot` must be passed (its default `None` changes pick
    pricing) so the basis is the same, not merely similar."""
    ctx = world.ctx
    return trade_decisions.evaluate_trade(
        world.snapshot.team_by_id(team_id),
        incoming,
        outgoing,
        ctx.philosophy_by_team[team_id],
        ctx.window_by_team[team_id],
        league_rng_seed=ctx.league_rng_seed,
        season=ctx.season,
        phase=ctx.phase,
        cap_settings=ctx.cap_settings,
        next_season=ctx.next_season,
        next_season_cap_settings=ctx.next_season_cap_settings,
        next_season_room_dollars=ctx.room_by_team[team_id],
        rookie_scale=ctx.rookie_scale,
        league_snapshot=world.snapshot,
        tuning=ctx.tuning,
        pick_slot_ranks=rank_teams_by_projected_slot(world.snapshot, tuning=ctx.tuning),
    )


@dataclass(frozen=True)
class _Resolution:
    status: TradeStatus
    illegal_token: str | None
    detail: str


def _resolution_for_ai_answer(
    stance: NegotiationStance, token: str | None, *, ai_code: str, ai_is_proposer: bool
) -> _Resolution:
    """The ONE stance -> status map, for both places an AI answers (§4.4). All
    six `NegotiationStance` members, none implicit. `ai_is_proposer` selects
    the single deliberate difference between the two call sites -- the
    terminal status of a plain refusal -- and nothing else:

    - AI is the ANSWERING PARTNER (`propose`/`counter` to an AI team): a plain
      refusal is `REJECTED`. Somebody refused; `EXPIRED` would be a lie.
    - AI is the PROPOSER (a human accepts an AI-authored offer, §5.3 step 10):
      the offer went stale and the proposer withdrew, so a plain refusal is
      `EXPIRED`, and the message says "no longer wants", not "not interested".

    Stances:
    - `REJECT_TOO_FAR` / `EXPIRE` / `REJECT_DEADLOCK` -> the plain-refusal
      status above. **`EXPIRE` is the one to get right**:
      `NegotiationStance.EXPIRE` means "out of negotiating rounds" (at
      `rounds_remaining=0`, "no, but not wildly off"), and is unrelated to
      `TradeStatus.EXPIRED`. `REJECT_DEADLOCK` is unreachable (one side is
      always a consent record) but mapped so the function is total.
    - `REJECT_ILLEGAL` -> `EXPIRED` + token + `log.error`, on BOTH paths.
      Unreachable: the legality gate ran on both sides immediately upstream
      from the same snapshot. If it fires, this service's gate and
      `evaluate_trade`'s disagree -- the divergence class §5.2 exists to
      prevent. The message names the rules, never "no longer wants".
    - `COUNTER` -> the plain-refusal status + `log.error`, on BOTH paths.
      Unreachable at `rounds_remaining=0`; mapped rather than raised because
      an exception inside a human's interaction is worse than a conservative
      resolution.
    - `ACCEPT` is the caller's, not this map's: passing it is a programming
      error and raises `AssertionError` (an explicit `raise`, so `python -O`
      cannot elide it) instead of answering an accepting AI with a refusal."""
    if stance is NegotiationStance.ACCEPT:
        raise AssertionError(
            "_resolution_for_ai_answer was called with NegotiationStance.ACCEPT; "
            "the caller resolves an accept, this map only produces refusals"
        )
    if stance is NegotiationStance.REJECT_ILLEGAL:
        log.error(
            "offer_service: evaluate_trade and the service's own legality gate "
            "disagree (token=%s) -- invariant violation", token,
        )
        return _Resolution(
            TradeStatus.EXPIRED, token, "The league's rules changed under this deal."
        )
    if stance is NegotiationStance.COUNTER:
        log.error(
            "offer_service: negotiation.respond returned COUNTER at rounds_remaining=0 "
            "-- OS-4 landed without revisiting the stance map"
        )
    if ai_is_proposer:
        return _Resolution(TradeStatus.EXPIRED, None, f"{ai_code} no longer wants this deal.")
    return _Resolution(
        TradeStatus.REJECTED, None, f"{ai_code} is not interested in that deal."
    )


# ---------------------------------------------------------------------------
# Locked-transaction building blocks. None of these opens a gate.
# ---------------------------------------------------------------------------


async def _check_claim(
    conn,
    *,
    gate: roster_lock.LeagueGate,
    league_id: int,
    require_phase_agreement: bool = True,
) -> LeagueClaimState:
    """Delegates to `league_claim.check_claim`, which is this function's own
    body moved out unchanged (Decision P5-98.g step 2) now that `/draft` is
    its second caller.

    **The wrapper is not ceremony; it is a name a committed test binds to.**
    `tests/integration/test_offer_service.py:1127-1145` monkeypatches
    `offer_service._check_claim` by name to open its race window, and that
    test must not be edited to accommodate a move -- a refactor that forces
    its own regression test to be rewritten has stopped being a refactor.
    Keeping the symbol also keeps every call site inside this module reading
    as this module's own step, which is what the discipline test's
    `_reachable_targets` traversal (same-module helpers only) walks."""
    return await league_claim.check_claim(
        conn,
        gate=gate,
        league_id=league_id,
        require_phase_agreement=require_phase_agreement,
    )


async def _load_world(
    conn, *, gate: roster_lock.LeagueGate, league_id: int, lock_set: roster_lock.LockSet
) -> _LockedWorld:
    """`load_gm_inputs` read UNDER the gate, the `leagues` share lock and
    every named team/player row lock. `gate.season` equals the league's
    `current_season` here (`_check_claim` asserted it), so the season
    mismatch `load_gm_inputs` raises on cannot fire."""
    snapshot, ctx = await gm_service.load_gm_inputs(conn, league_id=league_id, season=gate.season)
    return _LockedWorld(snapshot=snapshot, ctx=ctx, lock_set=lock_set)


async def _write_terminal(
    conn,
    offer: TradeOffer,
    new_status: TradeStatus,
    *,
    resolved_by: int | None,
    illegal_token: str | None = None,
    detail: str | None = None,
) -> OfferResult:
    """The terminal status write: `is_legal_transition` from `PROPOSED`, then
    the CAS. **Zero rows raises `ConcurrencyError`, which rolls back the
    whole transaction -- including a preceding `apply_trade`.** The caller
    holds the offer row's `FOR UPDATE` lock from §5.3 step 6, so zero rows
    means that lock did not hold for the whole span; this is the assertion,
    not an independent defence."""
    if not is_legal_transition(TRADE_TRANSITIONS, TradeStatus.PROPOSED, new_status):
        raise ValidationError(f"An offer cannot move from proposed to {new_status.value}.")
    affected = await offer_repo.resolve_offer_cas(
        conn,
        offer.id,
        expected_status=TradeStatus.PROPOSED,
        new_status=new_status,
        resolved_by=resolved_by,
        illegal_token=illegal_token,
    )
    if affected != 1:
        raise ConcurrencyError(_OFFER_CHANGED_MESSAGE)
    resolved = await offer_repo.get_trade_offer(conn, offer.id)
    assert resolved is not None  # we just updated it
    return OfferResult(offer=resolved, changed=True, detail=detail)


async def _lock_offer_for_answer(
    conn,
    *,
    gate: roster_lock.LeagueGate,
    league_id: int,
    offer_id: int,
    actor_user_id: int,
    action: str,
    extra_player_ids: Sequence[int] = (),
    pre_lock_check: Callable[[TradeOffer], None] | None = None,
) -> _LockedOffer | OfferResult:
    """§5.3 steps 2-7, shared by `respond` and `counter` (which each open
    their own gate first, then call this). Returns a `_LockedOffer` when the
    offer is `PROPOSED`, current, and this actor may answer it -- or an
    `OfferResult` when the call is already over (someone answered it first;
    or it was stale and this call wrote `EXPIRED`).

    RMLP: receives an open gated transaction from its caller; never opens a gate.
    It takes the tier-1/tier-2 locks itself (step 5) -- deliberately not left
    to `apply_trade`'s own acquire inside its savepoint (§5.4) -- and the
    discipline suite pins that.

    Ordering is load-bearing:
    - step 3 (unlocked discovery) only learns which rows to lock, and
      reports NOTHING about status: an unauthorized caller must not learn
      whether an offer exists or what state it is in. One `NotFoundError`
      message covers missing and wrong-league.
    - step 4 (permission) precedes every lock and every write (NIT 3).
      `pre_lock_check` runs right after it, for `counter`'s package
      validation: it needs the offer's team ids, and validating before the
      permission check would leak the offer's existence.
    - step 6 re-reads under `FOR UPDATE`; this is the authoritative status."""
    state = await _check_claim(conn, gate=gate, league_id=league_id)  # step 2

    discovered = await offer_repo.get_trade_offer(conn, offer_id)  # step 3
    if discovered is None or discovered.league_id != league_id:
        raise NotFoundError(_NO_SUCH_OFFER)
    to_team = await team_repo.get_team(conn, discovered.to_team_id)  # step 3b
    if to_team is None or to_team.league_id != league_id:
        raise NotFoundError(_NO_SUCH_OFFER)

    # COUPLING: `to_team.manager_user_id` is read UNLOCKED (no row lock, READ
    # COMMITTED), so it can be stale by the time step 6 locks the offer. There
    # is deliberately NO (f2)-style re-check of the manager after step 5, and
    # none is needed, because `league_service.assign_manager`/`remove_manager`
    # call `offer_repo.expire_offers_for_team` (both sides, PROPOSED only) in
    # the SAME transaction as `team_repo.set_manager`. A manager change that
    # commits after this read has therefore already expired the offer, and
    # step 6's `FOR UPDATE` re-read observes `EXPIRED` and returns "already
    # expired" without writing. Break that same-transaction expiry and this
    # check becomes a stale authority check: a removed manager could answer.
    # Pinned by `tests/integration/test_offer_service.py::
    # test_an_answer_racing_a_manager_removal_finds_the_offer_expired`.
    permissions.require_team_manager(  # step 4
        actor_user_id,
        commissioner_user_id=state.commissioner_user_id,
        team_manager_user_id=to_team.manager_user_id,
        team_id=to_team.id,
        action=action,
    )
    if pre_lock_check is not None:
        pre_lock_check(discovered)

    lock_set = await roster_lock.acquire(  # step 5
        conn,
        league_id=league_id,
        teams=[discovered.from_team_id, discovered.to_team_id],
        players=_all_player_ids(discovered.package) + tuple(extra_player_ids),
    )

    offer = await offer_repo.get_trade_offer_for_update(conn, offer_id)  # step 6
    if offer is None:
        raise NotFoundError(_NO_SUCH_OFFER)
    # An ASSERTION, not a live guard: no code path updates these columns after
    # insert (an offer's package is immutable; `counter` inserts a NEW row), so
    # this can only fire if a future writer starts mutating them -- in which
    # case it is the tripwire, not a race this service expects to win.
    if (offer.from_team_id, offer.to_team_id, offer.package) != (
        discovered.from_team_id,
        discovered.to_team_id,
        discovered.package,
    ):
        raise ConcurrencyError(_OFFER_CHANGED_MESSAGE)
    if offer.status is not TradeStatus.PROPOSED:
        return OfferResult(
            offer=offer, changed=False, detail=f"That offer is already {offer.status.value}."
        )

    if offer.season != gate.season or offer.phase != gate.phase:  # step 7
        return await _write_terminal(
            conn,
            offer,
            TradeStatus.EXPIRED,
            resolved_by=None,
            detail=(
                f"That offer was made during {offer.phase.value}; the league has moved on "
                f"to {gate.phase.value}, so it has expired."
            ),
        )
    return _LockedOffer(offer=offer, lock_set=lock_set)


async def _resolve_locked(
    conn,
    *,
    gate: roster_lock.LeagueGate,
    league_id: int,
    offer: TradeOffer,
    world: _LockedWorld,
    actor_user_id: int,
    ai_partner_answers: bool,
) -> OfferResult:
    """RMLP: receives an open gated transaction from its caller; never opens a gate.

    The accept core, §5.3 steps 8-13, and the ONLY caller of
    `gm_service.apply_trade` in this module -- exactly three callers reach it:
    `respond` (a human clicks Accept), `propose`'s AI-partner path and
    `counter`'s AI-partner path (`ai_partner_answers=True`: the receiving AI
    team answers synchronously inside the proposing transaction, so there is
    no observable state in which an offer row exists but its AI answer never
    ran, §7.3). `veto` never comes here.

    Precondition, established by the caller under the locks: `offer` is
    `PROPOSED` (this transaction holds its row lock, or just inserted it),
    current for `gate`'s `(season, phase)`, and `world` was loaded after
    `roster_lock.acquire`. Nothing here opens a gate; `roster_lock.acquire`'s
    runtime "gate held" assertion is the only mechanical enforcement of that,
    plus this docstring's first line, which a unit test pins. It is deliberately
    invisible to the discipline test (§7.5).

    Steps, in order; every failure path before 12 writes its terminal status
    from `PROPOSED` and returns (nobody refused, the world moved -> `EXPIRED`):
    8 every named asset is where the offer says it is; 9 `_check_legality` on
    both sides; 10 the AI side, if any, answers via `_stance_for` (skipped
    structurally when both sides are human -- two consent records need no
    stance function); 12 `apply_trade`; 13 the terminal CAS, LAST."""
    from_id, to_id = offer.from_team_id, offer.to_team_id
    from_team = world.snapshot.team_by_id(from_id).team
    to_team = world.snapshot.team_by_id(to_id).team

    try:  # step 8
        from_out, to_out = _build_bundles(
            world, from_team_id=from_id, to_team_id=to_id, package=offer.package
        )
    except ValidationError as exc:
        return await _write_terminal(
            conn, offer, TradeStatus.EXPIRED, resolved_by=None, detail=exc.message
        )

    failure = _first_illegal_side(  # step 9
        world, from_team_id=from_id, to_team_id=to_id, from_out=from_out, to_out=to_out
    )
    if failure is not None:
        team_id, token = failure
        return await _write_terminal(
            conn,
            offer,
            TradeStatus.EXPIRED,
            resolved_by=None,
            illegal_token=token,
            detail=(
                f"This deal is no longer legal for {world.snapshot.team_by_id(team_id).team.code} "
                f"({token.removeprefix('illegal:')})."
            ),
        )

    # Step 10. Exactly one side can be an AI valuation; a human's side is
    # always a consent record.
    if ai_partner_answers:
        from_record = _consent_record(from_team, offer_id=offer.id)
        to_record = _evaluate_ai_side(world, to_id, incoming=from_out, outgoing=to_out)
        stance, token = _stance_for(from_record, to_record)
        if stance is not NegotiationStance.ACCEPT:
            resolution = _resolution_for_ai_answer(
                stance, token, ai_code=to_team.code, ai_is_proposer=False
            )
            return await _write_terminal(
                conn,
                offer,
                resolution.status,
                resolved_by=None,
                illegal_token=resolution.illegal_token,
                detail=resolution.detail,
            )
    elif offer.proposed_by is None:
        # AI proposer: it must still want the deal at accept time, or the
        # exploit is "sit on an offer until the AI would no longer make it".
        from_record = _evaluate_ai_side(world, from_id, incoming=to_out, outgoing=from_out)
        to_record = _consent_record(to_team, offer_id=offer.id)
        stance, token = _stance_for(from_record, to_record)
        if stance is not NegotiationStance.ACCEPT:
            # Same map and alarms as the partner branch above; the ONE
            # deliberate difference is status: a plain refusal here is
            # `EXPIRED`, not `REJECTED` -- nobody refused a human's offer, the
            # AI-authored offer went stale and its author withdrew (§5.3
            # step 10; §4.4's resolution).
            resolution = _resolution_for_ai_answer(
                stance, token, ai_code=from_team.code, ai_is_proposer=True
            )
            return await _write_terminal(
                conn,
                offer,
                resolution.status,
                resolved_by=None,
                illegal_token=resolution.illegal_token,
                detail=resolution.detail,
            )
    else:
        from_record = _consent_record(from_team, offer_id=offer.id)
        to_record = _consent_record(to_team, offer_id=offer.id)

    execution = gm_service.TradeExecution(  # step 12
        team_a_id=from_id,
        team_b_id=to_id,
        players_a_to_b=tuple(offer.package["players_from_to"]),
        players_b_to_a=tuple(offer.package["players_to_from"]),
        picks_a_to_b=tuple(offer.package["picks_from_to"]),
        picks_b_to_a=tuple(offer.package["picks_to_from"]),
        net_a=from_record.score,
        net_b=to_record.score,
        record_a=from_record,
        record_b=to_record,
    )
    # `apply_trade` opens its own `transaction(conn)`, i.e. a SAVEPOINT: a
    # `ValidationError` from its revalidation rolls that back cleanly while
    # this outer transaction survives to write the EXPIRED row that records
    # why. `ConcurrencyError` (it is `@maps_lock_contention`) is NOT caught
    # here on purpose: nothing is wrong with the deal, the offer must stay
    # PROPOSED and the human retries. Do not widen this to `except Exception`
    # -- that would turn a transient lock timeout into a permanent EXPIRED.
    try:
        await gm_service.apply_trade(
            conn,
            execution,
            gate.season,
            gate.phase,
            league_id=league_id,
            actor_user_id=actor_user_id,
            source_offer_id=offer.id,
        )
    except ValidationError as exc:
        return await _write_terminal(
            conn, offer, TradeStatus.EXPIRED, resolved_by=None, detail=exc.message
        )

    return await _write_terminal(  # step 13 -- the LAST write
        conn,
        offer,
        TradeStatus.ACCEPTED,
        resolved_by=None if ai_partner_answers else actor_user_id,
    )


async def _team_in_league(ex: Executor, league_id: int, team_id: int) -> Team:
    team = await team_repo.get_team(ex, team_id)
    if team is None or team.league_id != league_id:
        raise NotFoundError(f"No team {team_id} in league {league_id}")
    return team


async def _counter_chain_depth(conn, offer: TradeOffer) -> int:
    """How many `counters_offer_id` links lie above `offer` (a root is `0`).
    Walks upward, capped at `MAX_NEGOTIATION_ROUNDS + 1` hops so a corrupt
    cycle cannot spin."""
    depth = 0
    current = offer
    while current.counters_offer_id is not None and depth <= MAX_NEGOTIATION_ROUNDS:
        depth += 1
        parent = await offer_repo.get_trade_offer(conn, current.counters_offer_id)
        if parent is None:  # ON DELETE SET NULL makes this unreachable
            break
        current = parent
    return depth


def _assert_new_package_legal(
    world: _LockedWorld,
    *,
    from_team_id: int,
    to_team_id: int,
    package: Mapping[str, tuple[int, ...]],
    noun: str,
) -> None:
    """§4.2 steps (h)-(i)'s pre-insert half, shared by `propose` and
    `counter`: the bundle build and legality on both sides. Both RAISE
    (`ValidationError`) -- unlike an accept-time failure there is no row yet
    whose reason needs recording, and the caller's transaction rolls back
    untouched."""
    from_out, to_out = _build_bundles(
        world, from_team_id=from_team_id, to_team_id=to_team_id, package=package
    )
    failure = _first_illegal_side(
        world, from_team_id=from_team_id, to_team_id=to_team_id, from_out=from_out, to_out=to_out
    )
    if failure is not None:
        team_id, token = failure
        raise ValidationError(
            f"That {noun} is not legal for {world.snapshot.team_by_id(team_id).team.code} "
            f"({token.removeprefix('illegal:')})."
        )


async def _insert_offer(
    conn,
    *,
    gate: roster_lock.LeagueGate,
    league_id: int,
    from_team_id: int,
    to_team_id: int,
    package: Mapping[str, tuple[int, ...]],
    actor_user_id: int,
    counters_offer_id: int | None,
) -> TradeOffer:
    return await offer_repo.create_trade_offer(
        conn,
        league_id=league_id,
        season=gate.season,
        phase=gate.phase,
        from_team_id=from_team_id,
        to_team_id=to_team_id,
        proposed_by=actor_user_id,
        status=TradeStatus.PROPOSED,
        players_from_to=package["players_from_to"],
        players_to_from=package["players_to_from"],
        picks_from_to=package["picks_from_to"],
        picks_to_from=package["picks_to_from"],
        counters_offer_id=counters_offer_id,
    )


# ---------------------------------------------------------------------------
# Entry points. Each opens its OWN transaction and its OWN gate.
# ---------------------------------------------------------------------------


@roster_lock.maps_lock_contention
async def propose(
    ex: Executor,
    *,
    league_id: int,
    from_team_id: int,
    to_team_id: int,
    players_from_to: tuple[int, ...],
    players_to_from: tuple[int, ...],
    picks_from_to: tuple[int, ...],
    picks_to_from: tuple[int, ...],
    actor_user_id: int,
) -> OfferResult:
    """`/propose-trade`, §4.2. Creates ROOT offers only -- there is exactly
    one way to counter an offer, `counter`. If the receiving team is
    AI-managed it answers synchronously, in this same transaction; otherwise
    the row is left `PROPOSED` for its human manager.

    (a) resolve and authorize before anything is read for content, locked or
    written -- checking permission after resolving ids would let a
    non-manager enumerate which ids exist; (b) pure package validation before
    any lock; (c) one transaction whose first statement is the gate; (d) the
    phase re-checked against the gate's; then the locked steps."""
    league = await league_repo.get_league(ex, league_id)  # (a)
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    from_team = await _team_in_league(ex, league_id, from_team_id)
    await _team_in_league(ex, league_id, to_team_id)
    permissions.require_team_manager(
        actor_user_id,
        commissioner_user_id=league.commissioner_user_id,
        team_manager_user_id=from_team.manager_user_id,
        team_id=from_team_id,
        action="propose_trade",
    )
    require_phase(league.current_phase, *command_gates.COMMAND_GATES["propose_trade"])
    # GM-3b/GM-3b.1 (docs/design/gm-command-surface.md, item 8(a)): the
    # pre-transaction prelude, the "fast, honest early rejection" -- after
    # `require_team_manager` (so it cannot leak whether an id exists) and
    # after `require_phase`, and BEFORE `_validate_package_ids` so a
    # deliberately illegal package from an auto-piloted GM still produces
    # the auto-pilot message, not the package-validation one. Only the
    # team's own manager can ever reach this branch (`require_team_
    # manager` already refused everyone else), so the message is single
    # and unconditional -- unlike `_resolve_and_authorize`'s union-gated
    # branch.
    if from_team.manager_user_id is not None and from_team.auto_pilot:
        raise permissions.PermissionDenied(
            f"{from_team.code} is on auto-pilot -- the AI is handling its trades. "
            "Run `/gm auto enabled:off` to propose a trade yourself."
        )

    _validate_package_ids(  # (b)
        from_team_id=from_team_id,
        to_team_id=to_team_id,
        players_from_to=players_from_to,
        players_to_from=players_to_from,
        picks_from_to=picks_from_to,
        picks_to_from=picks_to_from,
    )
    package = _package_of(players_from_to, players_to_from, picks_from_to, picks_to_from)

    async with transaction(ex) as conn:  # (c)
        gate = await roster_lock.open_league_gate(
            conn, league_id=league_id, mode=roster_lock.LockMode.SHARED
        )
        require_phase(gate.phase, *command_gates.COMMAND_GATES["propose_trade"])  # (d)
        await _check_claim(conn, gate=gate, league_id=league_id)  # (e)
        lock_set = await roster_lock.acquire(  # (f)
            conn,
            league_id=league_id,
            teams=[from_team_id, to_team_id],
            players=_all_player_ids(package),
        )
        # (f2) The authorize-before-lock check above read `manager_user_id`
        # unlocked; a `remove_manager`/`assign_manager` that committed between
        # that read and this lock would otherwise let a just-removed manager
        # bind both teams' assets. Re-ask the SAME predicate of the row the
        # tier-1 lock just read. Raising rolls the transaction back: nothing
        # was written before this point.
        permissions.require_team_manager(
            actor_user_id,
            commissioner_user_id=league.commissioner_user_id,
            team_manager_user_id=lock_set.teams_by_id[from_team_id].manager_user_id,
            team_id=from_team_id,
            action="propose_trade",
        )
        world = await _load_world(conn, gate=gate, league_id=league_id, lock_set=lock_set)  # (g)
        _assert_new_package_legal(  # (h), (i)
            world,
            from_team_id=from_team_id,
            to_team_id=to_team_id,
            package=package,
            noun="trade",
        )
        offer = await _insert_offer(
            conn,
            gate=gate,
            league_id=league_id,
            from_team_id=from_team_id,
            to_team_id=to_team_id,
            package=package,
            actor_user_id=actor_user_id,
            counters_offer_id=None,
        )
        if to_team_id in world.ctx.self_managed_team_ids:
            return OfferResult(offer=offer, changed=True)
        return await _resolve_locked(
            conn,
            gate=gate,
            league_id=league_id,
            offer=offer,
            world=world,
            actor_user_id=actor_user_id,
            ai_partner_answers=True,
        )


@roster_lock.maps_lock_contention
async def respond(
    ex: Executor,
    *,
    league_id: int,
    offer_id: int,
    action: OfferAction,
    actor_user_id: int,
) -> OfferResult:
    """Accept or reject an offer addressed to the actor's team (§5.3).

    A reject takes the same path as an accept as far as step 7 -- including
    the gate and the tier-1/tier-2 locks -- and then writes `REJECTED`. It
    would need neither, but branching on the action to skip them would create
    the one code path where a later edit could reach a roster write without a
    gate; the price is that a reject issued during the league's AI pass is
    refused with "try again" instead of succeeding."""
    async with transaction(ex) as conn:
        gate = await roster_lock.open_league_gate(
            conn, league_id=league_id, mode=roster_lock.LockMode.SHARED
        )
        require_phase(gate.phase, *command_gates.COMMAND_GATES["offer_respond"])  # step 1
        locked = await _lock_offer_for_answer(
            conn,
            gate=gate,
            league_id=league_id,
            offer_id=offer_id,
            actor_user_id=actor_user_id,
            action="offer_respond",
        )
        if isinstance(locked, OfferResult):
            return locked
        if action is OfferAction.REJECT:
            return await _write_terminal(
                conn, locked.offer, TradeStatus.REJECTED, resolved_by=actor_user_id
            )
        world = await _load_world(
            conn, gate=gate, league_id=league_id, lock_set=locked.lock_set
        )
        return await _resolve_locked(
            conn,
            gate=gate,
            league_id=league_id,
            offer=locked.offer,
            world=world,
            actor_user_id=actor_user_id,
            ai_partner_answers=False,
        )


@roster_lock.maps_lock_contention
async def counter(
    ex: Executor,
    *,
    league_id: int,
    offer_id: int,
    actor_user_id: int,
    players_from_to: tuple[int, ...],
    players_to_from: tuple[int, ...],
    picks_from_to: tuple[int, ...],
    picks_to_from: tuple[int, ...],
) -> OfferResult:
    """The ONLY function that writes a `counters_offer_id` (§4.5). The
    package is expressed from the COUNTERING team's side: `from` is the
    original's `to_team_id` (the only team the actor may counter for -- so
    "counter on someone else's behalf" is not a shape that exists), `to` its
    `from_team_id`. In one transaction: CAS the original `PROPOSED ->
    COUNTERED`, insert the new row pointing back at it, and -- if the other
    side is AI-managed -- let it answer. A counter never loses the original
    or orphans the new row.

    One `acquire` for the union of both packages' players, upfront: the
    original's because that row is being resolved, the new one's because it
    may auto-accept. Chain depth is checked BEFORE the CAS so a chain at the
    bound resolves nothing."""
    new_player_ids = tuple(players_from_to) + tuple(players_to_from)

    def validate_new_package(original: TradeOffer) -> None:
        _validate_package_ids(
            from_team_id=original.to_team_id,
            to_team_id=original.from_team_id,
            players_from_to=players_from_to,
            players_to_from=players_to_from,
            picks_from_to=picks_from_to,
            picks_to_from=picks_to_from,
        )

    async with transaction(ex) as conn:
        gate = await roster_lock.open_league_gate(
            conn, league_id=league_id, mode=roster_lock.LockMode.SHARED
        )
        require_phase(gate.phase, *command_gates.COMMAND_GATES["counter_offer"])
        locked = await _lock_offer_for_answer(
            conn,
            gate=gate,
            league_id=league_id,
            offer_id=offer_id,
            actor_user_id=actor_user_id,
            action="counter_offer",
            extra_player_ids=new_player_ids,
            pre_lock_check=validate_new_package,
        )
        if isinstance(locked, OfferResult):
            return locked
        original = locked.offer

        # GM-3b.1 (docs/design/gm-command-surface.md): a counter is
        # materially a NEW proposal, not an answer -- it destroys the
        # original (`_write_terminal(..., COUNTERED)` below) and
        # originates a brand-new package with the auto-piloted team as
        # the proposer, unconstrained to resemble the original. Sited
        # here, immediately after `locked` resolves and before every
        # write, NOT in the shared `_lock_offer_for_answer` (which
        # `respond` also calls and must not inherit this refusal --
        # accepting/rejecting stays open under auto-pilot, GM-4's rule).
        # Reads `locked.lock_set.teams_by_id[original.to_team_id]` -- a
        # TIER-1-LOCKED row, the one refusal site in this design that
        # reads `auto_pilot` under a lock, because that locked row is
        # simply what is in hand at the placement the information-leak
        # rule (step 4 of `_lock_offer_for_answer`) already forced. Only
        # the countering team's own manager can ever reach this branch
        # (`_lock_offer_for_answer`'s own step 4 already refused everyone
        # else), so the message is single and unconditional. **Ordering
        # caveat (round 3 critic, MINOR-9):** this sits after lock
        # acquisition and package validation, unlike `propose`'s early
        # rejection, so a malformed package from an auto-piloted GM
        # surfaces the package-validation error instead of this message
        # -- a forced, accepted consequence of the information-leak rule,
        # not an oversight.
        countering_team = locked.lock_set.teams_by_id[original.to_team_id]
        if countering_team.manager_user_id is not None and countering_team.auto_pilot:
            raise permissions.PermissionDenied(
                f"{countering_team.code} is on auto-pilot -- the AI is handling its trades. "
                "Run `/gm auto enabled:off` to counter yourself -- the original offer is "
                "still there to accept or reject in the meantime."
            )

        depth = await _counter_chain_depth(conn, original)
        if depth >= MAX_NEGOTIATION_ROUNDS:
            raise ValidationError(
                "This negotiation has already been countered as many times as it can be."
            )

        world = await _load_world(
            conn, gate=gate, league_id=league_id, lock_set=locked.lock_set
        )
        package = _package_of(players_from_to, players_to_from, picks_from_to, picks_to_from)
        # Build/legality first (they raise); only then write anything.
        _assert_new_package_legal(
            world,
            from_team_id=original.to_team_id,
            to_team_id=original.from_team_id,
            package=package,
            noun="counter",
        )
        await _write_terminal(conn, original, TradeStatus.COUNTERED, resolved_by=actor_user_id)
        new_offer = await _insert_offer(
            conn,
            gate=gate,
            league_id=league_id,
            from_team_id=original.to_team_id,
            to_team_id=original.from_team_id,
            package=package,
            actor_user_id=actor_user_id,
            counters_offer_id=original.id,
        )
        if original.from_team_id in world.ctx.self_managed_team_ids:
            return OfferResult(offer=new_offer, changed=True)
        return await _resolve_locked(
            conn,
            gate=gate,
            league_id=league_id,
            offer=new_offer,
            world=world,
            actor_user_id=actor_user_id,
            ai_partner_answers=True,
        )


@roster_lock.maps_lock_contention
async def veto(
    ex: Executor, *, league_id: int, offer_id: int, actor_user_id: int
) -> OfferResult:
    """The commissioner's one lever over trades (§9): kill a PENDING offer.
    Not a caller of `_resolve_locked` and never calls `acquire` -- a veto can
    never reach `apply_trade`, which is what makes it safe to lock the offer
    row without holding any tier-1/tier-2 lock (§6.3).

    V1 resolve the league and require the commissioner BEFORE any read of the
    offer, so an unauthorized caller learns nothing about offers; V2 own
    transaction + gate first (for `lock_timeout` -- see the module docstring);
    NO `require_phase`: `veto_offer` is `ALL_PHASES`, the phase escape hatch;
    V3 `leagues` row `FOR SHARE` + claim check (a veto mid-press is still a
    human write against league state); V4 unlocked read, league match, one
    `NotFoundError` message; V5 `FOR UPDATE` re-read, `PROPOSED` else report
    the current state and write nothing; V6 CAS with `resolved_by`."""
    league = await league_repo.get_league(ex, league_id)  # V1
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    permissions.require_commissioner(
        actor_user_id, commissioner_user_id=league.commissioner_user_id, action="veto_offer"
    )
    async with transaction(ex) as conn:  # V2
        gate = await roster_lock.open_league_gate(
            conn, league_id=league_id, mode=roster_lock.LockMode.SHARED
        )
        await _check_claim(  # V3
            conn, gate=gate, league_id=league_id, require_phase_agreement=False
        )
        discovered = await offer_repo.get_trade_offer(conn, offer_id)  # V4
        if discovered is None or discovered.league_id != league_id:
            raise NotFoundError(_NO_SUCH_OFFER)
        offer = await offer_repo.get_trade_offer_for_update(conn, offer_id)  # V5
        if offer is None:
            raise NotFoundError(_NO_SUCH_OFFER)
        if offer.status is not TradeStatus.PROPOSED:
            return OfferResult(
                offer=offer, changed=False, detail=f"That offer is already {offer.status.value}."
            )
        return await _write_terminal(  # V6
            conn, offer, TradeStatus.VETOED, resolved_by=actor_user_id
        )


async def list_offers(
    ex: Executor, *, league_id: int, team_id: int, actor_user_id: int
) -> TeamOffers:
    """`/offers`: every `PROPOSED` offer `team_id` received and sent. Read-only
    except the lazy expiry sweep, which runs FIRST and in a transaction of its
    own that takes no team or player lock (§6.4) -- never in the same
    transaction as the reads, and never inside a team-locking one. Opens no
    gate: it writes no roster state, and its one write (expiry) skips rows
    another transaction is resolving rather than waiting on them.

    Wider than the accept path on purpose (§9): the team's manager OR the
    commissioner may read; a read is not the binding act."""
    league = await league_repo.get_league(ex, league_id)
    if league is None:
        raise NotFoundError(f"No league {league_id}")
    team = await _team_in_league(ex, league_id, team_id)
    permissions.require_can_act_for_team(
        actor_user_id,
        commissioner_user_id=league.commissioner_user_id,
        team_manager_user_id=team.manager_user_id,
        team_id=team_id,
    )
    require_phase(league.current_phase, *command_gates.COMMAND_GATES["offers"])

    await offer_repo.expire_stale_offers_for_league(ex, league_id)
    offers = await offer_repo.list_offers_for_team_both_sides(
        ex, league_id=league_id, team_id=team_id, status=TradeStatus.PROPOSED
    )
    return TeamOffers(
        team_id=team_id,
        received=tuple(o for o in offers if o.to_team_id == team_id),
        sent=tuple(o for o in offers if o.from_team_id == team_id),
    )
