"""Command -> eligible-phases gate (v1's `transitions.py::ALLOWED`, renamed
per §6.1 — "transitions" was itself a confusing name for a command-
eligibility table, distinct from `graph.py`'s phase-to-phase adjacency;
v1's own module docstring had to open by disclaiming its name).

dba2 is a from-scratch project; Phase 5 builds its bot commands in
stages, and `COMMAND_GATES` grows with each one. **Stage C (docs/design/
phase-5-discord-presentation.md §11 step 9, Decision P5-17) adds the
read-only command surface's real gate entries below** --
`standings`/`team`/`box`/`wire`/`leaders`/`phase` are legal in every
phase (there is no phase where reading league state is illegitimate, the
same reasoning `advance`'s own entry already uses), and
`setup`/`assign_manager`/`remove_manager` (commissioner utilities,
Decision P5-41) are legal in every phase for the identical reason
`advance` is: a commissioner escape hatch that is only sometimes
available is not one. **Stage D' (§21 JC-4, §27, Decision P5-40) adds
`waive`/`release`/`sign_free_agent`/`set_strategy`/`set_role`/`set_
directive`/`timeline`'s real gate entries** -- see `ROSTER_TRANSACTION_
PHASES`'s own comment, above, for the split and its reasoning.
**The trade-offer review surface's five entries (`propose_trade`,
`offers`, `offer_respond`, `counter_offer`, `veto_offer`) are registered
here** (docs/design/phase-5-trade-offer-review-surface.md §8) by the commit
that builds their service layer, ahead of the Discord commands that will
call them -- `is_command_legal` raises `KeyError` on an unregistered command
by design, and `offer_service` reads these sets for its own `require_phase`.
`propose_trade`'s entry stops being the "representative rule" stub it was:
it becomes load-bearing the moment a human can call it (Decision P5-40),
which is now.
"""
from __future__ import annotations

from dba2.domain.phase import Phase

ALL_PHASES: frozenset[Phase] = frozenset(Phase)

# Trades (docs/design/phase-5-trade-offer-review-surface.md §8): closed from
# the deadline through the postseason, open again in the offseason.
# `POST_DEADLINE`, `REGULAR_SEASON_COMPLETE`, `PLAYIN` and the four playoff
# rounds are the post-deadline stretch. `DRAFT` stays closed because
# `apply_draft` is that phase's own exclusive roster-changing mechanism (see
# `ROSTER_TRANSACTION_PHASES`' reasoning below); `POST_DRAFT_TRADES` exists
# precisely to hold draft-adjacent deals.
#
# **A subtraction from `ALL_PHASES`, so every phase not named here is open --
# including ones with no NBA-calendar analogue (`SETUP`, `PROGRESSION`,
# `OPTION_DECISIONS`, `RESIGN_WINDOW`, `FREE_AGENCY`, `WAIVERS`, `AWARDS`,
# `DRAFT_LOTTERY`, `POST_DRAFT_TRADES`).** The openness of those is inherited
# from `ROSTER_TRANSACTION_PHASES`' convention (closing a phase for a reason
# nobody stated "would be inventing a restriction JC-4 never asked for"), NOT
# derived from the real-NBA calendar, and the set is "modelled on" the NBA
# rule rather than "the" NBA rule. It is a league rule awaiting the user's
# ratification (JC-5, still open); ship as designed.
#
# It agrees with the AI: `run_phase_hook`'s trade windows fire only at
# `REGULAR_SEASON` and `TRADE_DEADLINE`, both open here, so the human and AI
# paths agree about when a trade is legal.
_TRADE_CLOSED_PHASES: frozenset[Phase] = frozenset(
    {
        Phase.POST_DEADLINE,
        Phase.REGULAR_SEASON_COMPLETE,
        Phase.PLAYIN,
        Phase.PLAYOFFS_R1,
        Phase.PLAYOFFS_R2,
        Phase.CONFERENCE_FINALS,
        Phase.FINALS,
        Phase.DRAFT,
    }
)
TRADE_OPEN_PHASES: frozenset[Phase] = ALL_PHASES - _TRADE_CLOSED_PHASES

# Stage D' roster-decision commands (docs/design/phase-5-discord-
# presentation.md §21 JC-4, §27's "all six, not a subset" confirmation,
# Decision P5-40). Unlike `propose_trade`'s phase set, this one is a
# builder gap-fill, not a doc ruling -- JC-4's own text names WHICH six
# commands are in scope but never rules on WHEN each is legal, the way
# JC-5 explicitly rules on `propose_trade`'s. Disclosed rather than
# silently invented: split into two groups by the same reasoning `propose_
# trade`'s own gate already uses --
#
# `ROSTER_TRANSACTION_PHASES` (waive/release/sign): real asset moves, the
# same class of action `propose_trade` gates, closed ONLY during `DRAFT`
# -- `apply_draft` is that phase's own exclusive roster-changing
# mechanism, and an arbitrary human waive/sign mid-draft could shrink the
# free-agent/draft-eligible pool out from under an in-progress pick
# sequence. Left open during every playoff round (unlike trades), because
# no stated mechanism conflict exists there and closing it would be
# inventing a restriction JC-4 never asked for.
#
# `ALL_PHASES` (gm_strategy/gm_role/gm_directive/gm_timeline, moved under
# the `/gm` group by GM-6, docs/design/gm-command-surface.md): standing
# team settings, not transactions -- the same "a lever, not an asset
# move" reasoning Decision P5-68 gives for `/timeline`'s own identical
# gate, extended here to the other three settings-shaped commands for
# consistency (a `/gm strategy` legal in every phase except one arbitrary
# exception would need its own stated reason, and none exists). `/gm
# auto` and `/gm status` join them on the same reasoning once GM-8/GM-9
# land: delegating your own decisions is a lever, not an asset move, and
# reading your own settings is never illegitimate.
_ROSTER_TRANSACTION_CLOSED_PHASES: frozenset[Phase] = frozenset({Phase.DRAFT})
ROSTER_TRANSACTION_PHASES: frozenset[Phase] = ALL_PHASES - _ROSTER_TRANSACTION_CLOSED_PHASES

COMMAND_GATES: dict[str, frozenset[Phase]] = {
    # A commissioner escape hatch is legal in every phase by design. Keyed by the
    # command's own name (`/advance`), which is the key `test_command_gates_
    # completeness` derives from every registered command -- the stub's original
    # `advance_phase` had no command behind it and could never have matched. The exit
    # gate, not this table, refuses a press (phase-5-stage-e-advance.md P5-99.a).
    "advance": ALL_PHASES,
    # Stage E's five commissioner companions to `/advance` (P5-99.a, P5-17): ALL_PHASES for
    # every one, for `advance`'s own reason -- an escape hatch that is only sometimes
    # available is not one. What refuses a `/advance-to` or `/advance-force` is the service
    # (the exit gate, the work prefix, the `PRESEASON` and missing-schedule refusals), not
    # this table.
    "advance_to": ALL_PHASES,
    "advance_force": ALL_PHASES,
    "advance_unlock": ALL_PHASES,
    "advance_postpone_game": ALL_PHASES,
    "advance_restore_game": ALL_PHASES,
    # `/advance-run` and its stop (docs/design/advance-run.md AR-10, P5-17): ALL_PHASES for
    # `advance`'s own reason. What refuses a run is the route check and its stop conditions, and
    # the stop must be reachable in whatever phase the run has got to.
    "advance_run": ALL_PHASES,
    "advance_run_stop": ALL_PHASES,
    # The trade-offer review surface (§8). An accept executes a trade, so
    # `offer_respond` and `counter_offer` gate identically to `propose_trade`.
    # `offers` is a read (never illegitimate). `veto_offer` is ALL_PHASES: a
    # commissioner escape hatch that is only sometimes available is not one,
    # and killing a bad offer must not be blocked by the phase that made it bad.
    "propose_trade": TRADE_OPEN_PHASES,
    "offers": ALL_PHASES,
    "offer_respond": TRADE_OPEN_PHASES,
    "counter_offer": TRADE_OPEN_PHASES,
    "veto_offer": ALL_PHASES,
    # Stage C's read-only command surface (§11 step 9) -- reading league
    # state is never illegitimate, in any phase.
    "standings": ALL_PHASES,
    "team": ALL_PHASES,
    # `box` is legal in every phase; what it may SHOW is a separate, per-game gate on reveal
    # (`bot/cogs/read.py`, `game_feed.box_score_is_public`, P5-99.n) that this table cannot express.
    "box": ALL_PHASES,
    "wire": ALL_PHASES,
    "leaders": ALL_PHASES,
    "phase": ALL_PHASES,
    # Stage C's commissioner utilities (Decision P5-41, P5-13).
    "setup": ALL_PHASES,
    "assign_manager": ALL_PHASES,
    "remove_manager": ALL_PHASES,
    # Stage D' roster-decision commands (JC-4's six, P5-40) plus /timeline
    # (Decision P5-68, "the seventh ROSTER command") -- see this module's
    # own comment above `ROSTER_TRANSACTION_PHASES` for the split's
    # reasoning.
    "waive": ROSTER_TRANSACTION_PHASES,
    "release": ROSTER_TRANSACTION_PHASES,
    "sign_free_agent": ROSTER_TRANSACTION_PHASES,
    # GM-6/GM-7 (docs/design/gm-command-surface.md): moved under the `/gm`
    # group, keyed by qualified name (`"gm strategy"` -> `gm_strategy`),
    # not the old top-level `set_strategy`/`set_role`/`set_directive`/
    # `timeline` names -- an alias table was rejected by name (GM-7(b)) as
    # a second place to forget a command.
    "gm_strategy": ALL_PHASES,
    "gm_role": ALL_PHASES,
    "gm_directive": ALL_PHASES,
    "gm_timeline": ALL_PHASES,
    # GM-8/GM-9/GM-10 (docs/design/gm-command-surface.md): delegating your
    # own decisions is a lever, not an asset move (the same reasoning
    # above); reading your own (or a player's) settings is never
    # illegitimate.
    "gm_auto": ALL_PHASES,
    "gm_status": ALL_PHASES,
    "gm_player": ALL_PHASES,
    # Stage D'-25's `/draft` (docs/design/phase-5-stage-d-prime-draft.md,
    # Decision P5-98.j) -- the table's only single-phase entry, and its
    # reason is already written next to its complement above: `_ROSTER_
    # TRANSACTION_CLOSED_PHASES = {DRAFT}` closes waive/release/sign during
    # the draft because "`apply_draft` is that phase's own exclusive
    # roster-changing mechanism". `/draft` IS that mechanism; outside DRAFT
    # it has no pick to apply, and a gate that let it run would produce
    # `require_phase`'s error one layer deeper anyway.
    "draft": frozenset({Phase.DRAFT}),
    # Stage D's broadcast (docs/design/phase-5-stage-d-broadcast.md P5-97.r).
    # Watching a replay of a final game is never illegitimate; a phase with
    # nothing to watch gets P5-97.p's honest "not yet played" reply, not a
    # phase error.
    "watch": ALL_PHASES,
}


def is_command_legal(command: str, current: Phase) -> bool:
    """True if `command` is gated open in `current`.

    Raises `KeyError` for a command that isn't registered in
    `COMMAND_GATES` — an unregistered command should fail loudly, not
    silently be treated as legal everywhere or nowhere.
    """
    return current in COMMAND_GATES[command]
