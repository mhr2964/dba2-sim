"""`dba2/media/detect/trades.py` -- docs/design/media-v2-newsroom.md MV-2a:
`TRADE_EXECUTED` (a real, wired trade-completion story, unlike the
unreachable `TRADE_REACTION`/`TRADE_FALLOUT` of the original eleven kinds)
and `TRADE_BROKEN_PROMISE` (the irony case, backed by `dba2/media/detect/
motivation.py`'s own ladder-state-at-a-past-instant reconstruction).

Pure: fed by already-loaded data. `trade_executed_package` takes one
`LeagueTransaction` header plus its own `LeagueTransactionAsset` rows
(`txn_repo.list_trades_for_league_season`/`list_assets_for_transactions`,
already built in an earlier build step) -- a future loader in
`dba2/services/media_service.py` is what actually calls those repo
functions and hands this module their return values; this module never
reads a database. `trade_broken_promise_package` takes the ALREADY-
reconstructed `state_at_trade` (`motivation.request_state_at`'s own return
value, computed by that future loader from `agency_service.load_ladder_
transitions_for_league`'s output, which every media cycle already loads)
rather than reconstructing it itself, so this module owns exactly the
firing rule and the package shape, never the reconstruction.

**Both functions build UNSEALED `StoryPackage` drafts** -- `Subject`'s name
fields are always `None` here (MV-1c.1: "a detector never emits an
identity fact"); `dba2.services.media_service.seal_package` resolves them
and prepends `season`/`phase`/the identity facts.

**Real, disclosed simplification against MV-2a's own "players in each
direction (names)" text: this module counts assets, it does not name
them.** MV-1c.1's own "a detector never emits an identity fact" rule
already forces every *subject*'s name to be resolved once, at seal time,
from a bulk id->name map the cycle's own loaders hold -- naming every
individual traded PLAYER (not just the subject) the same way would need a
second, per-asset bulk name resolution this build step's loader does not
build, and MV-9's own per-detector test list for this kind never actually
asks for a named asset list (its four cases -- players both ways, picks
only, one-sided, a purged player row -- are all about counting assets
correctly, never about rendering a name). Counting also makes the
documented post-purge state (`LeagueTransactionAsset.player_id IS NULL`)
trivially safe: `asset_kind` alone is enough to count a movement, so a
purged id changes nothing about how many assets moved.

Zero I/O, the same purity rules every other module under `dba2/media/`
carries: no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.media`, `dba2.domain.
media_facts`, `dba2.domain.motivation`, `dba2.domain.phase`, `dba2.domain.
transaction` and stdlib.
"""

from __future__ import annotations

from collections.abc import Sequence

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import Angle, Fact, FactSource, StoryPackage, Subject, SubjectKind
from dba2.domain.motivation import RequestState
from dba2.domain.phase import Phase
from dba2.domain.transaction import LeagueTransaction, LeagueTransactionAsset, TransactionAssetKind


def _int_fact(key: str, label: str, value: int, source: FactSource) -> Fact:
    return Fact(
        key=key, label=label, value_text=str(value), value_number=float(value), source=source
    )


def _bool_fact(key: str, label: str, value: bool, source: FactSource) -> Fact:
    """A boolean-shaped fact, `FactType.INT` (MV-1c.1 rule 1: every key has
    exactly one declared type, and an equality trigger on `1`/`0` is the
    same shape every other `INT` key already uses) -- `value_number` stays
    `0.0`/`1.0`, matching `_int_fact`'s own numeric rendering exactly, so
    the trigger/salience view (which reads `value_number` for an `INT`
    key, MV-1c.1 rule 1) sees no difference at all.

    **`value_text` is `"Yes"`/`"No"`, not `"1"`/`"0"` (critic round on
    commits `ea84c5f`..`665526d`, bug B2).** `_build_fact_box` (`dba2/
    media/write/template.py`) publishes a fact box straight from `Fact.
    value_text` with no type awareness of its own -- a literal `"0"`/`"1"`
    reached real rendered output (`('He asked to be traded and it
    happened', '0')`) with no human-readable meaning at all. Fixing the
    rendering HERE, at the one place that already knows a key is boolean-
    shaped, is deliberately narrower than teaching the write layer a
    second, key-name-keyed "which INT keys are secretly booleans" list --
    that knowledge already lives here and nowhere else needs to duplicate
    it. Every one of this fact's own `label`s already reads as a plain
    positive assertion ("Came out of a real negotiation", "He asked to be
    traded and it happened"), so a `"Yes"`/`"No"` swap for `value_text`
    reads correctly without also inverting `label` per value."""
    return Fact(
        key=key, label=label, value_text=("Yes" if value else "No"),
        value_number=float(int(value)), source=source
    )


def _classify_trade_assets(
    assets: Sequence[LeagueTransactionAsset], *, team_id: int
) -> tuple[int, int, int, int]:
    """`(players_acquired, players_sent, picks_acquired, picks_sent)` for
    `team_id`, counted directly off `asset_kind`/`from_team_id`/
    `to_team_id` -- never off `player_id`/`draft_pick_id`, which is exactly
    what makes this safe against the documented post-purge state
    (`player_id IS NULL` on a real, historical `PLAYER` asset row still
    counts as one player movement; `asset_kind` is fixed at insert and
    never changes, MV-2a's own cited guarantee)."""
    players_acquired = players_sent = picks_acquired = picks_sent = 0
    for asset in assets:
        is_player = asset.asset_kind is TransactionAssetKind.PLAYER
        if asset.to_team_id == team_id:
            if is_player:
                players_acquired += 1
            else:
                picks_acquired += 1
        if asset.from_team_id == team_id:
            if is_player:
                players_sent += 1
            else:
                picks_sent += 1
    return players_acquired, players_sent, picks_acquired, picks_sent


def trade_executed_package(
    transaction: LeagueTransaction,
    assets: Sequence[LeagueTransactionAsset],
    *,
    season: int,
    phase: Phase,
    cycle_index: int,
) -> StoryPackage | None:
    """One package per trade (MV-2a), subject a `MATCHUP` of the two teams
    -- derived from `assets` itself (the two distinct team ids appearing as
    `from_team_id`/`to_team_id`), never a caller-supplied pair, so this
    function cannot silently mis-pair a transaction against the wrong
    teams. Returns `None` for a transaction whose assets do not name
    exactly two distinct teams (a malformed or non-trade row reaching this
    function by caller error) -- a real, disclosed guard, not a documented
    production path (every real `txn_type='trade'` row moves assets between
    exactly two teams, `gm_service.apply_trade`'s own contract).

    `team_id`/`opponent_team_id` are assigned by the LOWER team id first
    (deterministic, no RNG, no reliance on `assets`' own row order) --
    `team_id` is therefore not "the team that benefited" or "the team a
    human GM controls," just a stable, reproducible choice of which side is
    primary; both sides' own counts are still real and correctly
    attributed via `_classify_trade_assets`.

    **`asset_count_edge` (critic round on commits `ea84c5f`..`665526d`,
    bug B3) is the one real, per-instance directional signal this
    detector can honestly emit** -- `(players_acquired + picks_acquired) -
    (players_sent + picks_sent)`, positive when `team_id`'s side counted
    more assets in than out, negative the other way, zero for a
    numerically even swap. This is NOT "who won the trade" (this
    function's own docstring above is unchanged: asset value, not asset
    count, is what a real trade winner needs, and this module has no
    value data at all) -- it is the narrower, honest claim `seeds/media_
    phrases.json`'s `hot_take_trade_executed_fleeced*` templates actually
    need: a real, reproducible count comparison to gate which team's name
    a "fleeced" hot take names as the beneficiary, rather than asserting a
    direction this package cannot support (the exact bug the critic
    found)."""
    team_ids = sorted(
        {asset.from_team_id for asset in assets if asset.from_team_id is not None}
        | {asset.to_team_id for asset in assets if asset.to_team_id is not None}
    )
    if len(team_ids) != 2:
        return None
    team_id, opponent_team_id = team_ids

    players_acquired, players_sent, picks_acquired, picks_sent = _classify_trade_assets(
        assets, team_id=team_id
    )
    asset_count_edge = (players_acquired + picks_acquired) - (players_sent + picks_sent)

    facts = (
        _int_fact("players_acquired_count", "Players acquired", players_acquired,
                   FactSource.LEAGUE_TRANSACTIONS),
        _int_fact("players_sent_count", "Players sent", players_sent,
                   FactSource.LEAGUE_TRANSACTIONS),
        _int_fact("picks_acquired_count", "Picks acquired", picks_acquired,
                   FactSource.LEAGUE_TRANSACTIONS),
        _int_fact("picks_sent_count", "Picks sent", picks_sent, FactSource.LEAGUE_TRANSACTIONS),
        _int_fact("asset_count_edge", "Net assets gained by count (team vs. opponent)",
                   asset_count_edge, FactSource.LEAGUE_TRANSACTIONS),
        _bool_fact("human_negotiated", "Came out of a real negotiation",
                    transaction.source_offer_id is not None, FactSource.LEAGUE_TRANSACTIONS),
        _bool_fact("ai_executed", "Executed by the AI, unattended",
                    transaction.actor_user_id is None, FactSource.LEAGUE_TRANSACTIONS),
    )
    return StoryPackage(
        story_kind=StoryKind.TRADE_EXECUTED,
        angle=Angle.TRADE_MADE,
        subject=Subject(
            kind=SubjectKind.MATCHUP, team_id=team_id, opponent_team_id=opponent_team_id
        ),
        facts=facts,
        season=season,
        phase=phase,
        cycle_index=cycle_index,
    )


_LOYALTY_SATISFACTION_FLOOR = 40
_LOYALTY_WEIGHT_FLOOR = 25
"""MV-2a's own two thresholds, verbatim -- disclosed, not calibrated (same
status as every other threshold in this design): `state_at_trade is
CONTENT` **and** `satisfaction_loyalty >= 40` **and** `weight_loyalty >=
25` is the "he cared about loyalty and was satisfied on it" reading."""


def trade_broken_promise_package(
    *,
    player_id: int,
    old_team_id: int,
    new_team_id: int,
    state_at_trade: RequestState | None,
    satisfaction_loyalty: int,
    weight_loyalty: int,
    trade_season: int,
    season: int,
    phase: Phase,
    cycle_index: int,
) -> StoryPackage | None:
    """MV-2a's irony case. Fires only when `state_at_trade` (the caller's
    own `motivation.request_state_at` reconstruction) is a REAL reading --
    `None` (no reading: a pre-migration row, or the reconstruction landed
    on one) never fires, and specifically never reads as `CONTENT`, the
    same "absent is neutral" rule `motivation.request_state_at`'s own
    docstring states.

    Two firing shapes, mutually exclusive by `state_at_trade`'s own value:

    - `state_at_trade is CONTENT` and `satisfaction_loyalty >=
      _LOYALTY_SATISFACTION_FLOOR` and `weight_loyalty >=
      _LOYALTY_WEIGHT_FLOOR` -- angle `PROMISE_BROKEN`, `got_his_wish`
      false: a player who cared about loyalty and was satisfied on it,
      traded anyway.
    - `state_at_trade is TRADE_REQUESTED` -- angle `TRADE_MADE`,
      `got_his_wish` true: the opposite irony, he asked and got it. No
      loyalty gate here -- MV-2a's own text draws no threshold for this
      branch, only the state itself.

    Every other `state_at_trade` reading (`CONTENT` below the loyalty
    bars, `UNSETTLED`, `EXTENSION_DEMANDED`, `HOLDOUT`) is not one of
    MV-2a's two named ironies and returns `None` -- a real trade with
    nothing ironic about it is not a story this detector tells."""
    if state_at_trade is RequestState.CONTENT:
        below_satisfaction = satisfaction_loyalty < _LOYALTY_SATISFACTION_FLOOR
        below_weight = weight_loyalty < _LOYALTY_WEIGHT_FLOOR
        if below_satisfaction or below_weight:
            return None
        angle = Angle.PROMISE_BROKEN
        got_his_wish = False
    elif state_at_trade is RequestState.TRADE_REQUESTED:
        angle = Angle.TRADE_MADE
        got_his_wish = True
    else:
        return None

    facts = (
        _int_fact("satisfaction_loyalty", "Loyalty satisfaction", satisfaction_loyalty,
                   FactSource.MOTIVATION_EVENTS),
        _int_fact("weight_loyalty", "How much loyalty matters to him", weight_loyalty,
                   FactSource.MOTIVATION_EVENTS),
        _bool_fact("got_his_wish", "He asked to be traded and it happened", got_his_wish,
                    FactSource.MOTIVATION_EVENTS),
        _int_fact("trade_season", "Season the trade happened", trade_season,
                   FactSource.LEAGUE_TRANSACTIONS),
    )
    return StoryPackage(
        story_kind=StoryKind.TRADE_BROKEN_PROMISE,
        angle=angle,
        subject=Subject(kind=SubjectKind.PLAYER, player_id=player_id, team_id=new_team_id,
                          opponent_team_id=old_team_id),
        facts=facts,
        season=season,
        phase=phase,
        cycle_index=cycle_index,
    )
