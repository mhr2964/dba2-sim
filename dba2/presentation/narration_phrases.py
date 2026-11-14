"""The phrase library /watch's full narration picks its pre-lines from
(docs/design/watch-narration-v2.md WN-2.2, WN-3, WN-7). Data only: no
rendering, no selection -- `presentation/broadcast.py` owns both.

**Every phrase says only what a beat recorded (WN-3).** Each variant carries
its own tag string, in the design's vocabulary:

* `[F]` fact -- a player, role or outcome the beat records;
* `[M]` fact by the engine's model -- the action's own definition (a spot-up IS
  a pass to the shooter, a `dho` IS a handoff, a kick-out IS a pass to the
  vacated shooter);
* `[V]` flavor -- connective texture. The `flavor` tuple lists every `[V]`
  string of the variant, verbatim, so a test can hold them to the rule: a
  flavor word is admissible only if it is true under every outcome it can be
  printed with, and the flavor words of one outcome never appear under another
  outcome of the same beat (`OUTCOME_GROUPS`).

Names reach a template only through its placeholders, and every variant of a
key uses exactly `PLACEHOLDERS[key]`, so no variant can silently drop the
defender. No template names a dribble move, a court location other than the
role-name locations of WN-3.3, or a speed or effort adjective.

**Setup lines start with the player who holds the ball** (the ball handler, or
the passer of a pass-fed action), so a restart whose ball changed hands can
print "{X} now has it, <the rest>" by dropping that leading placeholder
(WN-4.3). A test pins the shape.

Placeholders: `BH` ball handler, `OBD` his on-ball defender, `SCR` screener,
`HB` handoff big, `FB` first man back, `EP` entry passer, `PS` post scorer,
`POSTD` post defender, `CUT` cutter, `CD` his (or the closing) defender,
`PASSER`, `SH` shooter, `CL` closer, `SD` screen defender, `HELPER`, `VS`
vacated shooter, `TAG` tagger, `PD` the popper's defender, `D` the contesting
defender, `X` whoever now has the ball.
"""

from __future__ import annotations

import zlib
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Phrase:
    """One variant: a `str.format` template, its `[F]`/`[M]` tags as a note for
    the reader, and the exact `[V]` flavor strings it contains."""

    template: str
    tags: str
    flavor: tuple[str, ...] = ()


def _p(template: str, tags: str, *flavor: str) -> Phrase:
    return Phrase(template, tags, flavor)


# Slots (WN-2.2): S setup, C screen coverage (folded into the S line), R restart
# prefix, O on-ball read, O+ its suffixes, H the help clause, P the pass, T the
# terminal line's defender clause (WN-5).
LIBRARY: Mapping[tuple[str, str], tuple[Phrase, ...]] = {
    # --- S: the first decision, the ball holder and his man (WN-4.1) ----------------
    ("S", "pnr_ball"): (
        _p(
            "{BH} comes off {SCR}'s screen, {OBD} on the ball",
            "[F] handler, screener, on-ball def",
            "comes off",
        ),
        _p(
            "{BH} uses {SCR}'s screen, {OBD} guarding", "[F] handler, screener, on-ball def", "uses"
        ),
        _p(
            "{BH} comes off a {SCR} screen, {OBD} on him",
            "[F] handler, screener, on-ball def",
            "comes off a",
        ),
        _p(
            "{BH} works off {SCR}'s screen, {OBD} on the ball",
            "[F] handler, screener, on-ball def",
            "works off",
        ),
        _p(
            "{BH} goes off {SCR}'s screen, {OBD} guarding",
            "[F] handler, screener, on-ball def",
            "goes off",
        ),
    ),
    ("S", "dho"): (
        _p(
            "{BH} takes the handoff from {HB}, {OBD} on the ball",
            "[M] handoff [F] handler, big, on-ball def",
        ),
        _p(
            "{BH} gets it from {HB} on the handoff, {OBD} guarding",
            "[M] handoff [F] handler, big, on-ball def",
        ),
        _p("{BH} takes it from {HB}, {OBD} on him", "[M] handoff [F] handler, big, on-ball def"),
        _p(
            "{BH} comes off {HB}'s handoff, {OBD} on the ball",
            "[M] handoff [F] handler, big, on-ball def",
            "comes off",
        ),
        _p(
            "{BH} gets the handoff from {HB}, {OBD} guarding",
            "[M] handoff [F] handler, big, on-ball def",
        ),
    ),
    ("S", "iso"): (
        # "sizes up" asserts the matchup an iso is by definition (WN-3.2).
        _p("{BH} sizes up {OBD}", "[F] matchup", "sizes up"),
        _p("{BH} isolates against {OBD}", "[M] iso [F] matchup"),
        _p("{BH} takes {OBD} one-on-one", "[M] iso [F] matchup"),
        _p("{BH} faces up {OBD}", "[F] matchup", "faces up"),
        _p("{BH} gets {OBD} one-on-one", "[M] iso [F] matchup"),
    ),
    ("S", "transition"): (
        _p(
            "{BH} pushes it, {FB} the first man back",
            "[M] transition [F] handler, first back",
            "pushes it",
        ),
        _p(
            "{BH} pushes the pace, {FB} back first",
            "[M] transition [F] handler, first back",
            "pushes the pace",
        ),
        _p(
            "{BH} leads the break, {FB} the first man back",
            "[M] transition [F] handler, first back",
            "leads the break",
        ),
        _p(
            "{BH} runs in transition, {FB} getting back",
            "[M] transition [F] handler, first back",
            "runs",
        ),
        _p(
            "{BH} brings it up the floor, {FB} back to meet him",
            "[M] transition [F] handler, first back",
            "brings it up the floor",
        ),
    ),
    # A continuation that reselects `transition` mid-possession (WN-1.2's engine quirk,
    # WN-15 R6) is narrated neutrally, never "pushes in transition".
    ("S", "attack"): (
        _p("{BH} attacks, {FB} on him", "[F] handler, his defender", "attacks"),
        _p("{BH} attacks {FB}", "[F] handler, his defender", "attacks"),
        _p("{BH} goes at {FB}", "[F] handler, his defender", "goes at"),
    ),
    # A screen action whose screen beat was never recorded (the setup tick ran the
    # shot clock out): the screen did not happen, so it is not narrated.
    ("S", "has_it"): (
        _p("{BH} has it, {OBD} on him", "[F] handler, on-ball def"),
        _p("{BH} has the ball, {OBD} guarding", "[F] handler, on-ball def"),
    ),
    ("S", "post_up"): (
        _p(
            "{EP} enters it to {PS} in the post, {POSTD} guarding",
            "[M] entry pass [F] roles, location=role",
        ),
        _p(
            "{EP} feeds {PS} in the post, {POSTD} on him",
            "[M] entry pass [F] roles, location=role",
            "feeds",
        ),
        _p(
            "{EP} finds {PS} in the post, {POSTD} guarding",
            "[M] entry pass [F] roles, location=role",
            "finds",
        ),
        _p(
            "{EP} dumps it in to {PS} in the post, {POSTD} on him",
            "[M] entry pass [F] roles, location=role",
            "dumps it in",
        ),
        _p(
            "{EP} gets it in to {PS} in the post, {POSTD} guarding",
            "[M] entry pass [F] roles, location=role",
        ),
    ),
    ("S", "off_ball_screen"): (
        _p(
            "{EP} hits {CUT} coming off {SCR}'s screen, {CD} chasing",
            "[M] cut + pass [F] roles",
            "chasing",
        ),
        _p(
            "{EP} finds {CUT} off {SCR}'s screen, {CD} chasing",
            "[M] cut + pass [F] roles",
            "finds",
            "chasing",
        ),
        _p(
            "{EP} feeds {CUT} off a {SCR} screen, {CD} trailing",
            "[M] cut + pass [F] roles",
            "feeds",
            "trailing",
        ),
        _p(
            "{EP} gets it to {CUT} coming off {SCR}'s screen, {CD} trailing",
            "[M] cut + pass [F] roles",
            "trailing",
        ),
        _p(
            "{EP} finds {CUT} running off {SCR}'s screen, {CD} chasing him",
            "[M] cut + pass [F] roles",
            "running off",
            "chasing him",
        ),
    ),
    ("S", "spot_up"): (
        _p(
            "{PASSER} swings it to {SH}, {CL} closing out",
            "[M] pass [F] shooter, closer",
            "swings it",
        ),
        _p("{PASSER} finds {SH}, {CL} closing out", "[M] pass [F] shooter, closer", "finds"),
        _p(
            "{PASSER} moves it to {SH}, {CL} running at him",
            "[M] pass [F] shooter, closer",
            "moves it",
            "running at him",
        ),
        _p(
            "{PASSER} hits {SH}, {CL} coming at him",
            "[M] pass [F] shooter, closer",
            "hits",
            "coming at him",
        ),
        _p("{PASSER} gets it to {SH}, {CL} closing", "[M] pass [F] shooter, closer"),
    ),
    # --- C: the screen's coverage, at team level except where the engine names who ----
    ("C", "under"): (
        _p("the defense goes under", "[F] coverage=under"),
        _p("they go under it", "[F] coverage=under"),
        _p("the defense ducks under", "[F] coverage=under", "ducks"),
    ),
    ("C", "over"): (
        _p("the defense fights over", "[F] coverage=over", "fights"),
        _p("they fight over the top", "[F] coverage=over", "fight", "the top"),
        _p("the defense goes over the top", "[F] coverage=over", "the top"),
    ),
    ("C", "hedge_recover"): (
        _p("the defense hedges and recovers", "[F] coverage=hedge_recover"),
        _p("they show and recover", "[F] coverage=hedge_recover", "show"),
        _p("a hedge, then they recover", "[F] coverage=hedge_recover"),
    ),
    # `switch`: `apply_switch` is the engine's own statement that SD now guards BH.
    ("C", "switch"): (
        _p("they switch, {SD} picks up {BH}", "[F] switch: screen def now on the ball"),
        _p("{SD} switches onto {BH}", "[F] switch: screen def now on the ball"),
        _p("a switch, {SD} now on {BH}", "[F] switch: screen def now on the ball"),
    ),
    # `blown`: "the screen defender loses the matchup outright" (screen.py's docstring).
    ("C", "blown"): (
        _p("{SCR} catches {SD} on it", "[F] blown: screener beat screen def"),
        _p("{SCR} catches {SD} on the screen", "[F] blown: screener beat screen def"),
        _p("{SCR} catches {SD} cleanly", "[F] blown: screener beat screen def", "cleanly"),
    ),
    # --- R: why a continuation restarted (WN-4.3) ------------------------------------
    ("R", "reset"): (
        _p("Reset — ", "[F] previous read contained"),
        _p("They reset — ", "[F] previous read contained"),
    ),
    ("R", "offensive_board"): (
        _p("Off the offensive board — ", "[F] offensive rebound"),
        _p("Second chance — ", "[F] offensive rebound", "Second chance"),
    ),
    ("R", "foul"): (
        _p("After the foul — ", "[F] non-bonus foul"),
        _p("Off the foul — ", "[F] non-bonus foul"),
    ),
    # TR-4.6: the offense won its own loose ball. "Scramble" is the design's word for
    # it (WN-4.3) and claims nothing the recovery clause does not: no turnover, no steal.
    ("R", "scramble"): (
        _p("After the scramble — ", "[F] loose ball recovered by the offense"),
        _p("Out of the scramble — ", "[F] loose ball recovered by the offense"),
    ),
    ("R", "now_has_it"): (
        _p("{X} now has it, ", "[F] the ball changed hands; no pass is claimed"),
        _p("{X} has it now, ", "[F] the ball changed hands; no pass is claimed"),
        _p("{X} has the ball now, ", "[F] the ball changed hands; no pass is claimed"),
        _p("{X}, with the ball now, ", "[F] the ball changed hands; no pass is claimed"),
    ),
    # --- O: the dribble read, the one place a dribble exists (WN-3.2) -----------------
    ("O", "advantage"): (
        _p("{BH} gets a step on {OBD}", "[F] advantage", "gets a step on"),
        _p("{BH} turns the corner on {OBD}", "[F] advantage", "turns the corner on"),
        _p("{BH} gets by {OBD}", "[F] advantage", "gets by"),
        _p("{BH} gets past {OBD}", "[F] advantage", "gets past"),
        _p("{BH} gets around {OBD}", "[F] advantage", "gets around"),
        _p("{BH} slips by {OBD}", "[F] advantage", "slips by"),
        _p("{BH} gets the edge on {OBD}", "[F] advantage", "gets the edge on"),
        _p("{BH} gets a half-step on {OBD}", "[F] advantage", "gets a half-step on"),
    ),
    ("O", "contained"): (
        _p("{OBD} stays in front of {BH}", "[F] contained", "stays in front of"),
        _p("{BH} can't shake {OBD}", "[F] contained", "can't shake"),
        _p("{BH} gets walled off by {OBD}", "[F] contained", "gets walled off by"),
        _p("{BH} can't get past {OBD}", "[F] contained", "can't get past"),
        _p("{OBD} keeps {BH} in front", "[F] contained", "keeps", "in front"),
        _p("{OBD} holds his ground against {BH}", "[F] contained", "holds his ground against"),
        _p("{BH} is held in check by {OBD}", "[F] contained", "is held in check by"),
        _p("{BH} probes, {OBD} right with him", "[F] contained", "probes", "right with him"),
    ),
    ("O", "defender_wins"): (
        _p("{OBD} cuts {BH} off", "[F] defender_wins", "cuts", "off"),
        _p("{BH} is cut off by {OBD}", "[F] defender_wins", "is cut off by"),
        _p("{BH} gets bottled up by {OBD}", "[F] defender_wins", "gets bottled up by"),
        _p("{BH} is turned back by {OBD}", "[F] defender_wins", "is turned back by"),
        _p("{OBD} beats {BH} to the spot", "[F] defender_wins", "beats", "to the spot"),
        _p("{OBD} stonewalls {BH}", "[F] defender_wins", "stonewalls"),
        _p("{OBD} shuts {BH} down", "[F] defender_wins", "shuts", "down"),
        _p("{BH} is headed off by {OBD}", "[F] defender_wins", "is headed off by"),
    ),
    ("O+", "help_stayed"): (
        _p("; {HELPER} stays home", "[F] help_stayed", "stays home"),
        _p("; {HELPER} stays with his man", "[F] help_stayed", "stays with his man"),
        _p("; {HELPER} stays put", "[F] help_stayed", "stays put"),
    ),
    ("O+", "keeps_it"): (
        _p("; {BH} keeps it", "[F] no_pass"),
        _p("; {BH} keeps it himself", "[F] no_pass"),
        _p("; {BH} holds on to it", "[F] no_pass", "holds on to it"),
    ),
    # --- H: the help clause, the head of a P line or an O suffix ------------------------
    ("H", "help_committed"): (
        _p("{HELPER} comes over to help", "[F] help_committed"),
        _p("{HELPER} helps off his man", "[F] help_committed"),
        _p("{HELPER} slides over to help", "[F] help_committed", "slides"),
    ),
    # `help_late` is the engine's own word for a helper out of position when he arrives.
    ("H", "help_late"): (
        _p("{HELPER} is late coming over", "[F] help_late"),
        _p("{HELPER} comes over late", "[F] help_late"),
        _p("{HELPER} is late to help", "[F] help_late"),
    ),
    # --- P: every pass the engine models (WN-4.2) ----------------------------------------
    ("P", "kickout_open"): (
        _p("{PASSER} kicks to {VS} — {CD} can't get there", "[M] kick-out [F] closeout failed"),
        _p("{PASSER} kicks it out to {VS}, {CD} too late", "[M] kick-out [F] closeout failed"),
        _p(
            "{PASSER} kicks it to {VS} — {CD} can't recover",
            "[M] kick-out [F] closeout failed",
        ),
    ),
    ("P", "kickout_contested"): (
        _p("{PASSER} kicks to {VS}, {CD} closes out", "[M] kick-out [F] closeout arrived"),
        _p(
            "{PASSER} kicks it out to {VS}, {CD} there on the closeout",
            "[M] kick-out [F] closeout arrived",
        ),
        _p(
            "{PASSER} kicks it to {VS}, {CD} gets out to him",
            "[M] kick-out [F] closeout arrived",
        ),
    ),
    ("P", "roll_live"): (
        _p(
            "{PASSER} hits {SCR} on the roll — {TAG} can't tag him",
            "[M] roll pass [F] tag failed",
        ),
        _p(
            "{PASSER} finds {SCR} rolling, no tag from {TAG}",
            "[M] roll pass [F] tag failed",
        ),
        _p(
            "{PASSER} dumps it to {SCR} on the roll, {TAG} late with the tag",
            "[M] roll pass [F] tag failed",
        ),
    ),
    ("P", "roll_tagged"): (
        _p("{PASSER} hits {SCR} rolling, {TAG} tags him", "[M] roll pass [F] tagged"),
        _p(
            "{PASSER} finds {SCR} on the roll, {TAG} there to tag",
            "[M] roll pass [F] tagged",
        ),
        _p("{PASSER} gets it to {SCR} rolling, {TAG} makes the tag", "[M] roll pass [F] tagged"),
    ),
    ("P", "pop_open"): (
        _p(
            "{PASSER} finds {SCR} popping out — {PD} is late to him",
            "[M] pop pass [F] late closeout",
        ),
        _p("{PASSER} hits {SCR} on the pop — {PD} late", "[M] pop pass [F] late closeout"),
        _p(
            "{PASSER} gets it to {SCR} on the pop — {PD} can't get out to him",
            "[M] pop pass [F] late closeout",
        ),
    ),
    ("P", "pop_contested"): (
        _p(
            "{PASSER} finds {SCR} popping, {PD} closes on him",
            "[M] pop pass [F] contested",
        ),
        _p(
            "{PASSER} hits {SCR} on the pop, {PD} right there",
            "[M] pop pass [F] contested",
            "right there",
        ),
        _p(
            "{PASSER} gets it to {SCR} on the pop, {PD} gets out to him",
            "[M] pop pass [F] contested",
        ),
    ),
    # --- T: who he scored on, and who stopped him (WN-5) ------------------------------------
    # Rule A make: he beat the defender who had won the read.
    ("T", "over"): (
        _p(" over {D}", "[F] contesting defender who won a read"),
        _p(" right over {D}", "[F] contesting defender who won a read", "right"),
        _p(" with {D} contesting", "[F] contesting defender who won a read"),
    ),
    # Two-beat make at p_make < 0.40 over a core-five defender: "tough" is the engine's
    # own number; the core-five gate chooses which true sentence to print.
    ("T", "tough_over"): (
        _p(", a tough one over {D}", "[F] p_make < .40, contesting defender"),
        _p(", a tough make over {D}", "[F] p_make < .40, contesting defender"),
        _p(", a tough shot over {D}", "[F] p_make < .40, contesting defender"),
    ),
    ("T", "stopped"): (
        _p(" — {D} stopped him", "[F] defender_wins, then the miss"),
        _p(" — {D} forced it", "[F] defender_wins, then the miss", "forced it"),
        _p(" — good defense by {D}", "[F] defender_wins, then the miss", "good defense"),
    ),
    ("T", "closed_out"): (
        _p(" — {D} closed it out", "[F] contested closeout, then the miss"),
        _p(" — {D} got out to contest it", "[F] contested closeout, then the miss", "got out"),
        _p(" — {D} closed out on him", "[F] contested closeout, then the miss"),
    ),
    ("T", "tagged"): (
        _p(" — {D} tagged it", "[F] roll_tagged, then the miss"),
        _p(" — {D} got there on the tag", "[F] roll_tagged, then the miss", "got there"),
        _p(" — {D} tagged the roll", "[F] roll_tagged, then the miss"),
    ),
}

PLACEHOLDERS: Mapping[tuple[str, str], frozenset[str]] = {
    ("S", "pnr_ball"): frozenset({"BH", "SCR", "OBD"}),
    ("S", "dho"): frozenset({"BH", "HB", "OBD"}),
    ("S", "iso"): frozenset({"BH", "OBD"}),
    ("S", "transition"): frozenset({"BH", "FB"}),
    ("S", "attack"): frozenset({"BH", "FB"}),
    ("S", "has_it"): frozenset({"BH", "OBD"}),
    ("S", "post_up"): frozenset({"EP", "PS", "POSTD"}),
    ("S", "off_ball_screen"): frozenset({"EP", "CUT", "SCR", "CD"}),
    ("S", "spot_up"): frozenset({"PASSER", "SH", "CL"}),
    ("C", "under"): frozenset(),
    ("C", "over"): frozenset(),
    ("C", "hedge_recover"): frozenset(),
    ("C", "switch"): frozenset({"SD", "BH"}),
    ("C", "blown"): frozenset({"SCR", "SD"}),
    ("R", "reset"): frozenset(),
    ("R", "offensive_board"): frozenset(),
    ("R", "foul"): frozenset(),
    ("R", "scramble"): frozenset(),
    ("R", "now_has_it"): frozenset({"X"}),
    ("O", "advantage"): frozenset({"BH", "OBD"}),
    ("O", "contained"): frozenset({"BH", "OBD"}),
    ("O", "defender_wins"): frozenset({"BH", "OBD"}),
    ("O+", "help_stayed"): frozenset({"HELPER"}),
    ("O+", "keeps_it"): frozenset({"BH"}),
    ("H", "help_committed"): frozenset({"HELPER"}),
    ("H", "help_late"): frozenset({"HELPER"}),
    ("P", "kickout_open"): frozenset({"PASSER", "VS", "CD"}),
    ("P", "kickout_contested"): frozenset({"PASSER", "VS", "CD"}),
    ("P", "roll_live"): frozenset({"PASSER", "SCR", "TAG"}),
    ("P", "roll_tagged"): frozenset({"PASSER", "SCR", "TAG"}),
    ("P", "pop_open"): frozenset({"PASSER", "SCR", "PD"}),
    ("P", "pop_contested"): frozenset({"PASSER", "SCR", "PD"}),
    ("T", "over"): frozenset({"D"}),
    ("T", "tough_over"): frozenset({"D"}),
    ("T", "stopped"): frozenset({"D"}),
    ("T", "closed_out"): frozenset({"D"}),
    ("T", "tagged"): frozenset({"D"}),
}

# The keys that are ALTERNATIVE outcomes of one beat: a variant of one may never
# read like a variant of another, and their `[V]` sets are disjoint (WN-3.1).
OUTCOME_GROUPS: tuple[tuple[tuple[str, str], ...], ...] = (
    (("C", "under"), ("C", "over"), ("C", "hedge_recover"), ("C", "switch"), ("C", "blown")),
    (("O", "advantage"), ("O", "contained"), ("O", "defender_wins")),
    (("H", "help_committed"), ("H", "help_late")),
    (
        ("P", "kickout_open"),
        ("P", "kickout_contested"),
        ("P", "roll_live"),
        ("P", "roll_tagged"),
        ("P", "pop_open"),
        ("P", "pop_contested"),
    ),
    (("T", "stopped"), ("T", "closed_out"), ("T", "tagged")),
)

# WN-7.2: a per-key integer is one of the selector's inputs, so two keys that fire in
# the same slot of the same possession do not pick in lockstep. The design used the
# key's POSITION in the library; that rewords every later key's lines (in every replay)
# whenever a key is added -- which the turnover-recovery hand-over's `("R",
# "scramble")` would have done. A CRC-32 of the key's own name is just as fixed across
# processes (unlike `hash()`) and moves nothing when the library grows.
KEY_ORDINALS: Mapping[tuple[str, str], int] = {
    key: zlib.crc32(f"{key[0]}:{key[1]}".encode()) for key in LIBRARY
}

# The leading placeholder of every S variant: the player holding the ball when the
# action starts (WN-4.3's "now has it" drops it).
SETUP_HOLDER: Mapping[str, str] = {
    "pnr_ball": "BH",
    "dho": "BH",
    "iso": "BH",
    "transition": "BH",
    "attack": "BH",
    "has_it": "BH",
    "post_up": "EP",
    "off_ball_screen": "EP",
    "spot_up": "PASSER",
}
