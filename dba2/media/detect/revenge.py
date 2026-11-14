"""`dba2/media/detect/revenge.py` -- docs/design/media-v2-newsroom.md MV-2i
(revision 4): `REVENGE_GAME`, the one kind no revision defined until this
one -- the read it needs (`txn_repo.list_trades_involving_player`) was
already specified in MV-2a as "the revenge-game and broken-promise spine";
this module was the only missing piece.

Pure: fed by already-loaded data (a future `dba2/services/media_service.py`
loader resolves the trade/schedule facts this function's four conditions
read). This module owns only the firing rule and the package shape.

Zero I/O, the same purity rules every other module under `dba2/media/`
carries: no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. Imports only `dba2.domain.media`, `dba2.domain.
media_facts`, `dba2.domain.phase` and stdlib.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import Angle, Fact, FactSource, StoryPackage, Subject, SubjectKind
from dba2.domain.phase import Phase

TRADE_RECENCY_SEASONS = 1
MIN_GAMES_PLAYED_FOR_A = 5
"""MV-2i's own two numeric thresholds, verbatim: the trade's `season` is
`current_season` or `current_season - TRADE_RECENCY_SEASONS` (this season
or the one before -- the same two-season window MV-2f's own trade signal
uses, "one anchor for 'recent trade,' not two"); the player has played
`>= 5` games for his new team this season."""


def _int_fact(key: str, label: str, value: int, source: FactSource) -> Fact:
    return Fact(
        key=key, label=label, value_text=str(value), value_number=float(value), source=source
    )


def _text_fact(key: str, label: str, value: str, source: FactSource) -> Fact:
    return Fact(key=key, label=label, value_text=value, value_number=None, source=source)


def _bool_fact(key: str, label: str, value: bool, source: FactSource) -> Fact:
    """`FactType.INT` (matching `_int_fact`'s own `value_number` rendering
    exactly, MV-1c.1 rule 1), but `value_text` renders as `"Yes"`/`"No"`,
    not `_int_fact`'s literal `"1"`/`"0"` -- critic round on commits
    `ea84c5f`..`665526d`, bug B2: this module used to build `got_his_wish`
    with a bare `_int_fact(..., int(got_his_wish), ...)` call, which
    published a literal `"0"`/`"1"` with no human-readable meaning at all.
    See `dba2/media/detect/trades.py`'s own identically-fixed `_bool_fact`
    for the full reasoning."""
    return Fact(
        key=key, label=label, value_text=("Yes" if value else "No"),
        value_number=float(int(value)), source=source
    )


def first_meeting_since_trade(
    pair_final_game_simmed_ats: Sequence[datetime | None], trade_created_at: datetime
) -> bool:
    """MV-2i condition 3's real derivation -- `docs/design/media-v2-newsroom.
    md` MV-15 (Option C), the fix for a design bug, not a coding bug: the
    ORIGINAL condition-3 text compared `games.game_date` (the league's
    SIMULATED calendar) against `league_transactions.created_at` (real wall
    clock) -- two clocks with no fixed relationship, so the comparison could
    never be true in the intended case. **The correct ordering is `games.
    simmed_at` vs `created_at`, both real wall clock** (`finalize_game`
    writes `simmed_at` via `COALESCE($7, NOW())` with no production caller
    ever passing `$7`), so they are the same clock as `created_at`'s own
    `DEFAULT NOW()`.

    A sibling of `motivation.py::request_state_at` in shape and purpose:
    pure, primitives-only (`Sequence[datetime | None]`, not `Game` objects --
    this module's own import pledge is `media`/`media_facts`/`phase`/stdlib,
    narrower than `dba2.domain`), fed by the caller's already-loaded `games`
    scan.

    `pair_final_game_simmed_ats` is the pair's `final`-game `simmed_at`
    stamps (any order). Returns `True` (a genuine first meeting since the
    trade) only if every one of them is BEFORE the trade. Two tie/NULL
    rules, both resolved toward silence rather than a false claim (this
    project's standing "absent is neutral, never fabricated" rule):

    - **Strict `>`.** A game whose `simmed_at` equals `trade_created_at`
      counts as *before* the trade -- Postgres `NOW()` is transaction-start
      time, so a game finalized in the same transaction as the trade ties;
      a tie does not block the claim (matches `request_state_at`'s own
      strict `>` for "after").
    - **`simmed_at is None` on a `final` game counts as a meeting since the
      trade** (blocks the claim). Unreachable through `finalize_game`, but
      `set_status` writes whatever status its caller passes and takes no
      stamp, so this function must be total over `None` -- an unorderable
      meeting cannot support "this is the first one."

    **Standing invariant this function's caller must never violate:**
    `league_transactions.created_at` and `games.simmed_at` are one shared
    ordering clock -- anything that writes league history out of causal
    order (e.g. a synthetic trade inserted after a season is already fully
    simmed) breaks it and can fabricate or suppress this claim."""
    for simmed_at in pair_final_game_simmed_ats:
        if simmed_at is None:
            return False
        if simmed_at > trade_created_at:
            return False
    return True


def revenge_game_package(
    *,
    player_id: int,
    team_id: int,
    opponent_team_id: int,
    trade_season: int,
    trade_asset_count: int,
    is_first_meeting_since_trade: bool,
    has_upcoming_scheduled_game: bool,
    player_games_played_this_season: int,
    team_standings_position: int,
    opponent_standings_position: int,
    scheduled_game_date: str,
    got_his_wish: bool | None,
    season: int,
    phase: Phase,
    cycle_index: int,
) -> StoryPackage | None:
    """`team_id` is the player's CURRENT team (A, where he plays now);
    `opponent_team_id` is the team that traded him away (B). All four of
    MV-2i's conditions must hold, checked in the order stated there:

    1. `season - trade_season <= TRADE_RECENCY_SEASONS` -- a trade three
       seasons back does not fire.
    2. `has_upcoming_scheduled_game` -- no upcoming game means no fire, EVEN
       at maximum `trade_asset_count` (this is a preview, not trivia, the
       identical gate `RIVALRY_MATCHUP` uses).
    3. `is_first_meeting_since_trade` -- MV-15's `first_meeting_since_trade`
       helper (this module), fed the pair's `final`-game `simmed_at` stamps:
       no `final` game between A and B was SIMMED after the trade's own
       `created_at` (both real wall clock -- NOT `games.game_date`, the
       league's simulated calendar, which is what MV-2i's original text
       wrongly prescribed); the SECOND meeting after a trade does not fire,
       only the first.
    4. `player_games_played_this_season >= 5` -- below that, there is no
       real line to put next to his name yet.

    `got_his_wish` is `None` when MV-2b's own reconstruction has no reading
    (a pre-migration row, or landed on one) -- carried as a fact ONLY when
    it is a real `bool`, never fabricated as `False` for "no reading.\""""
    if season - trade_season > TRADE_RECENCY_SEASONS:
        return None
    if not has_upcoming_scheduled_game:
        return None
    if not is_first_meeting_since_trade:
        return None
    if player_games_played_this_season < MIN_GAMES_PLAYED_FOR_A:
        return None

    facts: list[Fact] = [
        _int_fact("trade_season", "Season of the trade", trade_season,
                   FactSource.LEAGUE_TRANSACTIONS),
        _int_fact("trade_asset_count", "Total assets in that trade", trade_asset_count,
                   FactSource.LEAGUE_TRANSACTIONS),
        _int_fact("player_games_played_this_season", "Games played for his new team",
                   player_games_played_this_season, FactSource.PLAYER_SEASON_STATS),
        _int_fact("team_standings_position", "His team's standings position",
                   team_standings_position, FactSource.PULSE_SNAPSHOT),
        _int_fact("opponent_standings_position", "His old team's standings position",
                   opponent_standings_position, FactSource.PULSE_SNAPSHOT),
        _text_fact("scheduled_game_date", "The rematch", scheduled_game_date, FactSource.GAMES),
    ]
    if got_his_wish is not None:
        facts.append(
            _bool_fact("got_his_wish", "He asked to be traded and it happened",
                        got_his_wish, FactSource.MOTIVATION_EVENTS)
        )

    return StoryPackage(
        story_kind=StoryKind.REVENGE_GAME,
        angle=Angle.REVENGE,
        subject=Subject(kind=SubjectKind.PLAYER, player_id=player_id, team_id=team_id,
                          opponent_team_id=opponent_team_id),
        facts=tuple(facts),
        season=season,
        phase=phase,
        cycle_index=cycle_index,
    )
