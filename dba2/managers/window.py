"""`CompetitiveWindow` classification -- the score, the rank bands, the two
absolute guards, Decision P2-14's hysteresis cap, and the map onto the
persisted `Timeline` enum (docs/design/phase-2-manager-minds.md §7, §12
step 4). The enum and its parameter table already shipped at step 1
(`managers/tuning.py`'s `CompetitiveWindow`/`WINDOW_PARAMS`) -- this module
is only the classifier.

**The one thing this module must never do, stated up front because it took
three design rounds to actually close (§7):** `future_strength` -- and
therefore `window_score` -- must never consult a philosophy constant, a
`window_upside` window parameter, `confidence` damping, a `risk`
adjustment, or a scouted (`ScoutedPeak`) value. Round 2 tried to default
only the *philosophy* constant and left `window_upside` circular (window
needs a value it is itself computing); the fix that actually works is
`projected_ovr_2` (below), which reads only player facts (age,
`peak_age_start`, `overall_cached`, `potential`) and one fixed scalar,
`WINDOW_INPUT_UPSIDE`. That is what makes "classifying the same roster
from any of the 30 teams' snapshots, under all seven philosophies, returns
an identical window" true -- and it is a real, checkable property, not an
aspiration: see `tests/unit/managers/test_window.py`'s
`test_classification_is_identical_across_all_30_teams_and_7_philosophies`.

Classification is a pure function of a `LeagueSnapshot` and touches no
cap-sheet quantity (`TeamSnapshot.cap_settings` is never read here) --
`cap_flex` was removed from §7 in round 2 specifically to avoid importing
§8's projected room before `capsheet.py` (step 7) exists (§7: "a window is
a statement about a roster, not about a balance sheet").

Zero I/O, per Decision P2-2's purity extension: no `dba2.data`, no
`datetime.now()`, no bare `random.Random()`, no `open()`, no
`os.environ`. Imports `domain`, `managers.scouting` (for `age_ramp`,
`player_age`, `projected_peak` -- the *modelled* peak, never
`managers.scouting.scout_player`'s scouted one), and `managers.snapshot`/
`managers.tuning` only. Deliberately does **not** import
`managers.valuation` -- `market_value`'s `room_t` shape is close to
`projected_ovr_2`'s but pulling in `valuation.py` would also pull in its
`LeagueCapSettings`-shaped surface, which is exactly what this module's
"touches no cap-sheet quantity" property exists to keep out.
"""
from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from dataclasses import dataclass

from dba2.domain.team import GmPhilosophy, Timeline
from dba2.managers.explain import ReasonTerm
from dba2.managers.scouting import age_ramp, player_age, projected_peak
from dba2.managers.snapshot import LeagueSnapshot, RosterPlayer, TeamSnapshot
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow, GmTuning

# ---------------------------------------------------------------------------
# §7's constants, verbatim
# ---------------------------------------------------------------------------

# The ten-slot rotation-taper weights `team_strength`/`mean_age`/
# `future_strength` all share (§7) -- the same shape `sim/game.py`'s own
# rotation tiers taper by.
_ROTATION_WEIGHTS: tuple[float, ...] = (
    0.16, 0.15, 0.13, 0.12, 0.11, 0.09, 0.08, 0.06, 0.05, 0.05,
)
assert abs(sum(_ROTATION_WEIGHTS) - 1.0) < 1e-9, "the ten rotation weights must sum to 1"

# §7's score weights, verbatim sign and magnitude -- a mapping, not five
# positional args, so "all five declared inputs appear in the score and no
# undeclared input does" is a real, iterable, checkable property (see the
# module-level assert below and `test_window.py`'s own test of it) rather
# than something a reader has to eyeball against the formula.
SCORE_WEIGHTS: dict[str, float] = {
    "team_strength": 0.38,
    "win_pct": 0.22,
    "top2_ovr": 0.13,
    "future_strength": 0.17,
    "mean_age": -0.10,
}

_ABS_GUARD_SCORE_FLOOR = -0.90
_ABS_GUARD_STAR_OVERALL = 78

# §7's rank bands, stated for a 30-team league (CONTEND 1-6, WIN_NOW 7-14,
# RETOOL 15-22, REBUILD 23-30) -- `_rank_band` scales these proportionally
# for any other league size.
_CONTEND_FRACTION = 6 / 30
_WIN_NOW_FRACTION = 14 / 30
_RETOOL_FRACTION = 22 / 30

# Ascending "win-now-ness" -- Decision P2-14's one-band cap moves an index
# in this tuple by at most one per evaluation.
_BAND_ORDER: tuple[CompetitiveWindow, ...] = (
    CompetitiveWindow.REBUILD,
    CompetitiveWindow.RETOOL,
    CompetitiveWindow.WIN_NOW,
    CompetitiveWindow.CONTEND,
)

# Decision P2-13's many-to-one map onto the persisted `Timeline` column.
_TIMELINE_MAP: dict[CompetitiveWindow, Timeline] = {
    CompetitiveWindow.REBUILD: Timeline.REBUILD,
    CompetitiveWindow.RETOOL: Timeline.RETOOL,
    CompetitiveWindow.WIN_NOW: Timeline.WIN_NOW,
    CompetitiveWindow.CONTEND: Timeline.WIN_NOW,
}
assert set(_TIMELINE_MAP) == set(CompetitiveWindow), (
    "_TIMELINE_MAP must cover exactly the four CompetitiveWindow values"
)


def _weighted_sum(values_desc: Sequence[float]) -> float:
    """`Sigma w_i * v_i` over `_ROTATION_WEIGHTS`, `values_desc` already
    sorted descending by whatever key the caller ranked on. `zip` stops at
    the shorter sequence, so a roster with fewer than 10 bodies is simply
    summed over as many weights as it has slots for -- callers that must
    not shrink for a short roster (`future_strength`) pad to exactly 10
    themselves before calling this."""
    return sum(w * v for w, v in zip(_ROTATION_WEIGHTS, values_desc, strict=False))


# ---------------------------------------------------------------------------
# §7's five raw inputs
# ---------------------------------------------------------------------------


def team_strength(team_snapshot: TeamSnapshot) -> float:
    """`Sigma w_i * ovr_i`, roster sorted desc on `overall_cached` (§7).
    Reads `overall_cached` directly (Decision P2-3) -- never a
    philosophy-tilted overall, which is part of what makes this input
    identical for all seven philosophies classifying the same roster."""
    ovrs = sorted((rp.player.overall_cached for rp in team_snapshot.roster), reverse=True)
    return _weighted_sum(ovrs)


def top2_ovr(team_snapshot: TeamSnapshot) -> float:
    """Mean of the two highest `overall_cached` (§7) -- "star presence is
    not the same fact as depth." `0.0` for a roster of fewer than two
    players (a hand-built degenerate-league guard case, not a real
    roster)."""
    ovrs = sorted((rp.player.overall_cached for rp in team_snapshot.roster), reverse=True)
    top2 = ovrs[:2]
    return sum(top2) / len(top2) if top2 else 0.0


def mean_age(team_snapshot: TeamSnapshot, season: int) -> float:
    """The same `w_i`-weighted mean as `team_strength`, over age instead
    of `overall_cached`, roster sorted desc on `overall_cached` (§7) --
    the weighting is a rotation-minutes proxy keyed to talent rank, not
    to age rank, so the sort key stays `overall_cached` here too."""
    ranked = sorted(team_snapshot.roster, key=lambda rp: rp.player.overall_cached, reverse=True)
    ages = [float(player_age(rp.player.birth_date, season)) for rp in ranked]
    return _weighted_sum(ages)


def projected_ovr_2(
    roster_player: RosterPlayer, season: int, *, tuning: GmTuning = DEFAULT_TUNING
) -> float:
    """§7's `projected_ovr_2(p)`:

    `ovr + WINDOW_INPUT_UPSIDE * max(0, projected_peak(p) - ovr)
         * age_ramp(age + 2, peak_age_start) / max(age_ramp(age, peak_age_start), EPS)`

    This is the function the round-2/3/4/5 critic rounds spent three
    passes on, and it is the one this module must reproduce exactly, not
    approximately: `ovr` is `player.overall_cached` (never a
    philosophy-tilted value -- `effective_talent_ovr` is not imported
    here on purpose); `projected_peak` is `managers.scouting.
    projected_peak`, §4.3.1's *modelled* peak, not §4.7's *scouted* one
    -- no `ScoutedPeak`, no `rng_for`, no `team_id` anywhere in this
    function's call graph; `WINDOW_INPUT_UPSIDE` (`tuning.
    window_input_upside`, `= 0.50`) stands in for a philosophy's own
    `upside_weight` with no `window_upside` multiplier, no `confidence`
    damping, and no `risk` adjustment -- all four omissions are
    deliberate (§7: "no `window_upside` factor ... no `confidence`
    damping, no `risk` adjustment, and no scouting draw at all")."""
    player = roster_player.player
    ratings = roster_player.ratings
    ovr = float(player.overall_cached)
    age = float(player_age(player.birth_date, season))
    peak_age_start = float(ratings.peak_age_start)

    peak = projected_peak(
        player.overall_cached, age, peak_age_start, ratings.potential, tuning=tuning
    )
    ramp_2 = age_ramp(age + 2.0, peak_age_start, tuning=tuning)
    ramp_0 = age_ramp(age, peak_age_start, tuning=tuning)
    room = max(0.0, peak - ovr) * ramp_2 / max(ramp_0, tuning.eps)
    return ovr + tuning.window_input_upside * room


def future_strength(
    team_snapshot: TeamSnapshot, season: int, *, tuning: GmTuning = DEFAULT_TUNING
) -> float:
    """`team_strength` recomputed on `projected_ovr_2`, over players under
    contract in `season + 2`, padded to 10 bodies at `REPLACEMENT_OVR`
    (§7). The pad is a floor, not a reward (§7: "an unfilled roster slot
    in season t+2 will be filled, at worst by a minimum player") -- a
    team with fewer than 10 players under contract two seasons out reads
    as a team of replacements, never as a team of nothing."""
    target_season = season + 2
    locked_in = [
        rp
        for rp in team_snapshot.roster
        if rp.contract is not None and any(y.season == target_season for y in rp.contract.years)
    ]
    projected = sorted(
        (projected_ovr_2(rp, season, tuning=tuning) for rp in locked_in), reverse=True
    )
    pad_count = max(0, 10 - len(projected))
    padded = projected[:10] + [float(tuning.replacement_ovr)] * pad_count
    return _weighted_sum(padded)


def _percentile_ranks(values: Sequence[float]) -> list[float]:
    """Fractional rank in `[0, 1]`, tie-averaged (lowest value -> `0.0`,
    highest -> `1.0` for `n > 1`; every value -> `0.5` for `n <= 1`)."""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return [r / (n - 1) for r in ranks]


@dataclass(frozen=True)
class RawInputs:
    """§7's five raw (pre-z-score) inputs for one team -- field names
    match `SCORE_WEIGHTS`' keys exactly (asserted at module load)."""

    team_strength: float
    win_pct: float
    top2_ovr: float
    future_strength: float
    mean_age: float


_RAW_INPUT_FIELDS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(RawInputs))
assert set(SCORE_WEIGHTS) == _RAW_INPUT_FIELDS, (
    "SCORE_WEIGHTS and RawInputs must declare exactly the same five fields -- "
    "§7's 'every declared input is now consumed and every consumed input is declared'"
)


def raw_inputs(
    league_snapshot: LeagueSnapshot, *, tuning: GmTuning = DEFAULT_TUNING
) -> dict[int, RawInputs]:
    """Every §7 raw input, for every team in `league_snapshot`, computed
    once. `win_pct`'s two real-evidence tiers (§7: "from `games` this
    season if >= 20 played, else last season") both need a game log that
    `TeamSnapshot` does not carry -- Decision P2-5's join lists no
    `games` table, and this module is required to be a pure function of
    a `LeagueSnapshot` alone (§12 step 4). So `win_pct` here always
    resolves to §7's own third, explicitly stated tier: "`team_strength`
    percentile." A stated interpretation, not a silent narrowing --
    matching this codebase's own convention for flagging one
    (`valuation.py`'s module docstring, item 1)."""
    strengths = [team_strength(t) for t in league_snapshot.teams]
    win_pcts = _percentile_ranks(strengths)
    result: dict[int, RawInputs] = {}
    for team_snapshot, strength, wp in zip(
        league_snapshot.teams, strengths, win_pcts, strict=True
    ):
        result[team_snapshot.id] = RawInputs(
            team_strength=strength,
            win_pct=wp,
            top2_ovr=top2_ovr(team_snapshot),
            future_strength=future_strength(team_snapshot, league_snapshot.season, tuning=tuning),
            mean_age=mean_age(team_snapshot, league_snapshot.season),
        )
    return result


def _z_scores(values: Sequence[float]) -> list[float]:
    """Population z-score across the whole `values` sequence (§7:
    "z-scored across the league's 30 teams, so the score is always
    relative to the league that exists"). Guarded at zero variance (every
    team identical on this input) so a hand-built degenerate league can't
    divide by zero -- every team reads `0.0` on that input instead."""
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    sd = math.sqrt(variance)
    if sd < 1e-9:
        return [0.0] * n
    return [(v - mean) / sd for v in values]


# ---------------------------------------------------------------------------
# §7 -- the score
# ---------------------------------------------------------------------------


def window_score(
    team_snapshot: TeamSnapshot,
    league_snapshot: LeagueSnapshot,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> float:
    """§7's score, verbatim:

    `0.38*z(team_strength) + 0.22*z(win_pct) + 0.13*z(top2_ovr)
     + 0.17*z(future_strength) - 0.10*z(mean_age)`

    z-scored across every team in `league_snapshot` -- a pure function of
    `league_snapshot` alone. `team_snapshot` must be one of `league_
    snapshot.teams` (matched by `id`), since a z-score needs the whole
    league's distribution to be defined against; raises `KeyError`
    otherwise, the same contract `LeagueSnapshot.team_by_id` states."""
    raw_by_team = raw_inputs(league_snapshot, tuning=tuning)
    if team_snapshot.id not in raw_by_team:
        raise KeyError(f"team {team_snapshot.id} is not in this league_snapshot")

    team_ids = [t.id for t in league_snapshot.teams]
    score = 0.0
    for field_name, weight in SCORE_WEIGHTS.items():
        values = [getattr(raw_by_team[tid], field_name) for tid in team_ids]
        zs = dict(zip(team_ids, _z_scores(values), strict=True))
        score += weight * zs[team_snapshot.id]
    return score


def score_terms(
    team_snapshot: TeamSnapshot,
    league_snapshot: LeagueSnapshot,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> tuple[ReasonTerm, ...]:
    """`window_score`'s five terms, itemized as `ReasonTerm`s -- §9.1's
    "no black box" (Decision P2-18) applied to a window classification,
    which `classify_window` alone cannot satisfy since it returns a bare
    `CompetitiveWindow` with no accounting behind it.

    One `ReasonTerm` per `SCORE_WEIGHTS` key, in that dict's iteration
    order: `label` is the raw-input field name (`"team_strength"`,
    `"win_pct"`, `"top2_ovr"`, `"future_strength"`, `"mean_age"`) --
    a stable machine identifier, never a sentence, per `ReasonTerm.
    label`'s own contract. `input_value` is that field's *raw* (pre-
    z-score) value from `raw_inputs`; `weight` is `SCORE_WEIGHTS[label]`
    verbatim; `contribution` is `weight * z_score`, computed by the exact
    same `_z_scores` call `window_score` itself runs, so `sum(t.
    contribution for t in terms) == window_score(...)` to floating-point
    precision, not merely within some tolerance -- the reconstruction
    identity §9.1 requires, verified for every team in a synthetic
    30-team league by `test_window.py`.

    Purely additive: does not change `window_score`'s or `classify_
    window`'s own return value, and is itself zero-I/O like every other
    function in this module."""
    raw_by_team = raw_inputs(league_snapshot, tuning=tuning)
    if team_snapshot.id not in raw_by_team:
        raise KeyError(f"team {team_snapshot.id} is not in this league_snapshot")

    team_ids = [t.id for t in league_snapshot.teams]
    terms: list[ReasonTerm] = []
    for field_name, weight in SCORE_WEIGHTS.items():
        values = [getattr(raw_by_team[tid], field_name) for tid in team_ids]
        zs = dict(zip(team_ids, _z_scores(values), strict=True))
        input_value = getattr(raw_by_team[team_snapshot.id], field_name)
        contribution = weight * zs[team_snapshot.id]
        terms.append(
            ReasonTerm(
                label=field_name,
                input_value=input_value,
                weight=weight,
                contribution=contribution,
            )
        )
    return tuple(terms)


# ---------------------------------------------------------------------------
# §7 -- rank bands and the two absolute guards
# ---------------------------------------------------------------------------


def _rank_band(rank: int, n_teams: int) -> CompetitiveWindow:
    """Rank-based band assignment, not absolute z-score cutoffs (§7:
    "absolute cutoffs on a z-score in a compressed league produce
    'everyone is retooling', which is a market with no counterparties").
    `rank` is 1-indexed, `1` = highest `window_score`. §7's own cutoffs
    are stated for a 30-team league; scaled proportionally by `round()`
    for any other size so a hand-built degenerate-league guard test does
    not have to build exactly 30 teams."""
    contend_cutoff = round(_CONTEND_FRACTION * n_teams)
    win_now_cutoff = round(_WIN_NOW_FRACTION * n_teams)
    retool_cutoff = round(_RETOOL_FRACTION * n_teams)
    if rank <= contend_cutoff:
        return CompetitiveWindow.CONTEND
    if rank <= win_now_cutoff:
        return CompetitiveWindow.WIN_NOW
    if rank <= retool_cutoff:
        return CompetitiveWindow.RETOOL
    return CompetitiveWindow.REBUILD


def _has_rostered_star(team_snapshot: TeamSnapshot) -> bool:
    return any(
        rp.player.overall_cached >= _ABS_GUARD_STAR_OVERALL for rp in team_snapshot.roster
    )


def _apply_absolute_guards(
    band: CompetitiveWindow, score: float, team_snapshot: TeamSnapshot
) -> CompetitiveWindow:
    """§7's two absolute guards, applied after the rank band so the
    ranking cannot produce a fiction:
    - `window_score < -0.90` -> at most RETOOL, regardless of rank.
    - no rostered player at `overall >= 78` -> cannot be CONTEND,
      regardless of rank."""
    if score < _ABS_GUARD_SCORE_FLOOR and band in (
        CompetitiveWindow.CONTEND,
        CompetitiveWindow.WIN_NOW,
    ):
        band = CompetitiveWindow.RETOOL
    if band is CompetitiveWindow.CONTEND and not _has_rostered_star(team_snapshot):
        band = CompetitiveWindow.WIN_NOW
    return band


def classify_window(
    team_snapshot: TeamSnapshot,
    league_snapshot: LeagueSnapshot,
    *,
    tuning: GmTuning = DEFAULT_TUNING,
) -> CompetitiveWindow:
    """§7's full classification for one team: `window_score`, the
    rank-based band, then the two absolute guards -- a pure function of
    `league_snapshot` alone (`team_snapshot` must be a member of it).

    **No hysteresis here.** Decision P2-14's one-band-per-evaluation cap
    is a separate concern layered on top by `apply_hysteresis`, since it
    needs a *previous* window as an extra input a pure `LeagueSnapshot`
    classification does not have."""
    scores = {t.id: window_score(t, league_snapshot, tuning=tuning) for t in league_snapshot.teams}
    if team_snapshot.id not in scores:
        raise KeyError(f"team {team_snapshot.id} is not in this league_snapshot")

    ranked_ids = sorted(scores, key=lambda tid: scores[tid], reverse=True)
    rank = ranked_ids.index(team_snapshot.id) + 1
    band = _rank_band(rank, len(league_snapshot.teams))
    return _apply_absolute_guards(band, scores[team_snapshot.id], team_snapshot)


# ---------------------------------------------------------------------------
# Decision P2-14 -- the hysteresis cap
# ---------------------------------------------------------------------------


def apply_hysteresis(
    previous: CompetitiveWindow,
    proposed: CompetitiveWindow,
    gm_philosophy: GmPhilosophy,
) -> CompetitiveWindow:
    """Decision P2-14: window is re-evaluated only at `PRESEASON`,
    `TRADE_DEADLINE`, and `DRAFT_LOTTERY`, and moves at most one band per
    evaluation -- "without it a two-game losing streak flips a contender
    to a rebuild and the AI tears down a good team" (§7). `chaos` is
    exempt from the cap (§6, "which is the point of `chaos`"): a `chaos`
    GM's own window can jump straight to `proposed` regardless of
    distance. `gm_philosophy` and `previous` are both inputs `classify_
    window` deliberately does not take -- neither belongs in a pure
    `LeagueSnapshot` classification, both belong here."""
    if gm_philosophy is GmPhilosophy.CHAOS:
        return proposed
    prev_idx = _BAND_ORDER.index(previous)
    proposed_idx = _BAND_ORDER.index(proposed)
    delta = max(-1, min(1, proposed_idx - prev_idx))
    return _BAND_ORDER[prev_idx + delta]


# ---------------------------------------------------------------------------
# Decision P2-13 -- the map onto the persisted `Timeline` column
# ---------------------------------------------------------------------------


def to_timeline(window: CompetitiveWindow) -> Timeline:
    """Decision P2-13's many-to-one map (§7): `REBUILD -> rebuild`,
    `RETOOL -> retool`, `WIN_NOW -> win_now`, `CONTEND -> win_now`. No
    migration, no schema change -- `teams.timeline` keeps meaning exactly
    what Phase 0 said it means."""
    return _TIMELINE_MAP[window]
