"""CLI: create a dev league (30 real teams via `league_service.create_league`)
and a calibrated synthetic roster per team, using `sim.derivation`'s
latent-talent generator (docs/design/phase-1-step-7-calibration.md §11.4,
Decision S7-14) -- the manual smoke-test path §8 step 8 names explicitly,
and per §9's risk table: "Ratings ship empty and stay unvalidated through
all of Phase 0 ... Mitigate with `tools/seed_dev_league.py` generating
plausible random ratings so nothing downstream is blocked on real data."
It also becomes Phase 1's harness input (§8's outline).

Usage (run with `-m` from the repo root, NOT as a bare script path --
`tools/` has no `__init__.py` and isn't declared in pyproject's package
list, so `python tools/seed_dev_league.py` puts only `tools/` itself on
`sys.path` and `import dba2` fails; `python -m` puts the current working
directory on `sys.path` instead, which is what makes `dba2` importable
here without a `pip install -e .` step):

    python -m tools.seed_dev_league --name "Dev League" \
        --emit-targets seeds/synthetic_calibration_targets.json

**Reproducibility (§11.4, corrected again by critic round 17's B17-2): a
fixed `--rng-seed` reproduces the same league, including tendencies and
`team_strategy` -- not just `overall_cached`.** The generator no longer
uses a bare `random.Random` seeded once per script run -- every team draws
its own stream via `core.rng.rng_for(rng_seed, "synthetic", "team",
team_index)` (team offset, §5.2's position assignment), every player draws
its own via `rng_for(rng_seed, "synthetic", "player", team_index,
slot_index)` (physicals, tier talent, the 55 ratings), and tendencies draw
from `rng_for(rng_seed, "tendencies", team_index, slot_index)` -- salted by
this tool's own stable per-slot identity, the same one every other stream
here already uses, **not** by the database-assigned `player.id` a prior
version of this tool salted with. That salt was the actual defect: a fresh
run's serial-id insert is never the same id twice even at a fixed
`--rng-seed`, so tendencies (and every `team_strategy` numeric that reads
one) silently differed between two same-seed runs while `overall_cached`
did not -- measured at 419/420 tendency rows and 30/30 `team_strategy` rows
differing before this fix; see
`tests/integration/test_seed_dev_league.py::
test_seed_dev_league_same_seed_reproduces_identical_tendencies_and_team_strategy`
for the regression guard. §6.6's league-level distribution is an asserted
property this tool's own `--emit-targets` output records provenance for,
and a re-run at the same seed reproducing it is the direct check §12 step
7 names ("re-running the tool at the same seed reproduces identical
`overall_cached` values for every player" -- now also true of every other
generated field).

Scope: real bio (name/birth date, still hand-rolled per §5.4 -- "Names and
birth dates stay in tools/") plus a calibrated `player_ratings` +
`player_tendencies` row per player via `sim.derivation`'s pure generator
and `ratings_service`'s write wrappers, then `strategy_service.
ensure_defaults_for_season` for every team's `team_strategy` row.
Deliberately does NOT generate `player_roles` or contracts -- neither is
named in §9's risk-table mitigation, and adding them here would be scope
creep past "unblock manual smoke-testing."

**Decision P4-67 (docs/design/phase-4-media-and-player-agency.md §11
Stage C step 13a) -- this docstring's own prior claim about `player_
motivation` is now stale, and stale on purpose.** This function DOES now
generate `player_motivation`: one `agency_service.bootstrap_league_
motivation` call at the end of `seed_dev_league`, for every player this
league seeds. Without it, `tools/advance_dev_league_season.py`'s morale
trim (Decision P4-50) has no motivation data to read on this path at
all -- P4-67's own finding, "the relocated instrument has no route to its
own input." `pay_vs_market`/`tenure_seasons` stay `None`-neutral here
regardless (Decision P4-63: this path genuinely has no contracts), which
is correct and must not be "fixed" by inventing one.

**RPD-4 hydration (docs/design/real-player-data-integration.md), additive,
opt-in via `--real-players-season`/`--real-players-per-team`:** with both
flags set, the first `real_players_per_team` slots of every team are real
players sampled (without replacement, across the whole league) from
`seeds/real_players/<season>.json` instead of `sim.derivation`'s synthetic
pipeline -- `source=RatingSource.REAL_STAT_DERIVED`, real `external_ref`,
real 57-column ratings block (including the real `peak_age_start`/
`peak_age_end` `tools/_gm_real_player_peak_age.py` already computed), real
`confidence`/`derived_from_season` carried through unchanged from the seed
row. Remaining slots stay fully synthetic, unaffected -- each slot's own
RNG stream is keyed by its own `(team_index, slot_index)`, never chained
sequentially, so hydrating slot 0..k-1 does not perturb what slot k..
players_per_team-1 would have drawn. Default (`real_players_per_team=0`)
reproduces today's fully-synthetic behavior exactly; real players are also
excluded from `--emit-targets`' calibration-target stats, which calibrate
the synthetic generator specifically. **Every row carries its real team**
(`team.abbreviation`; an earlier version of this note wrongly said no such
field exists). By default hydration still deals real players onto teams at
random; `--real-team-rosters` places each on the franchise he actually
played for. `wingspan_in` has no BDL
source at any tier (confirmed in the ETL's own docstring): a version-2 row
stores the builder's per-player estimate (RPC-10), and only a legacy row is
synthesized here from the real `height_in` via the same height-relative offset
`sim.derivation.synthetic_physicals` already uses for synthetic players,
not left as a fabricated placeholder without disclosure. `draft_pick_
number` stays `None` for every real player -- BDL exposes `draft_year`
but never a real pick number, at any tier.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from dataclasses import fields as dc_fields
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from random import Random

from dba2.core.config import get_config
from dba2.core.rng import rng_for
from dba2.data.db import Executor, close_pool, get_pool, transaction
from dba2.data.repositories import player_repo, ratings_repo, team_repo
from dba2.domain.player import Position, RosterStatus
from dba2.domain.ratings import RatingSource
from dba2.domain.team import Team
from dba2.services import agency_service, league_service, ratings_service, strategy_service
from dba2.sim import derivation as d
from tools._real_fields import require_builder_version

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REAL_PLAYERS_DIR = _REPO_ROOT / "seeds" / "real_players"
_REAL_PLAYER_DRAFT_AGE_ANCHOR = 21
# sim.derivation.synthetic_physicals's own wingspan-relative-to-height
# convention, reused verbatim since real players carry no BDL wingspan.
_WINGSPAN_OFFSET_MEAN = 3.5
_WINGSPAN_OFFSET_SD = 1.5

_FIRST_NAMES = (
    "James", "Michael", "Chris", "Anthony", "Kevin", "Jordan", "Marcus",
    "Devin", "Trey", "Malik", "Isaiah", "Xavier", "Jalen", "Cole", "Elijah",
    "Andre", "Damian", "Terrence", "Josh", "Reggie", "Tyler", "Cameron",
    "Bryce", "Darius", "Miles",
)
_LAST_NAMES = (
    "Johnson", "Williams", "Brown", "Davis", "Miller", "Wilson", "Moore",
    "Taylor", "Anderson", "Thomas", "Jackson", "White", "Harris", "Martin",
    "Thompson", "Robinson", "Clark", "Lewis", "Walker", "Young", "Scott",
    "Green", "Baker", "Nelson", "Carter",
)

_DEFAULT_PLAYERS_PER_TEAM = 14


@dataclass(frozen=True)
class _GeneratedRecord:
    """One generated player's data, kept in memory just long enough to
    build `--emit-targets`' §7 artifact -- not persisted anywhere itself."""

    position: Position
    ratings: dict[str, int]
    overall_cached: int
    tendencies: dict[str, int]


def _slot_position(positions: tuple[Position, ...], slot_index: int) -> Position:
    """§11.4: "slots beyond 14 take BENCH; fewer than 14 truncates from the
    end" covers tier; position for a slot past `synthetic_positions`'
    fixed 14-length tuple cycles rather than erroring, so
    `--players-per-team` values other than 14 still produce a legal
    roster (degraded, not crashing) -- the default and every documented
    use of this tool stays at 14."""
    return positions[slot_index % len(positions)]


def _slot_tier(slot_index: int) -> d.RosterTier:
    if slot_index < len(d.TIER_BY_SLOT):
        return d.TIER_BY_SLOT[slot_index]
    return d.RosterTier.BENCH


def _load_real_player_pool(season: int, *, allow_legacy_ratings: bool = False) -> list[dict]:
    """The season's real-player rows. Refuses a season whose rows are not all
    RPD-8 builder version 2 (`require_builder_version`): legacy ratings compress
    stars toward 70. `allow_legacy_ratings` is the explicit opt-out."""
    path = _REAL_PLAYERS_DIR / f"{season}.json"
    if not path.exists():
        raise ValueError(
            f"no real-player seed file for season {season} ({path}) -- "
            "backfilled seasons are 1996-2025, see "
            "docs/design/real-player-data-integration.md"
        )
    rows = json.loads(path.read_text(encoding="utf-8"))["players"]
    require_builder_version(rows, season, allow_legacy_ratings)
    return rows


def _sample_real_players(pool: list[dict], *, rng_seed: int, count: int) -> list[dict]:
    """`count` distinct players, sampled without replacement across the
    whole league in one draw (never per-team) so no player can be assigned
    to two teams in the same run."""
    if count > len(pool):
        raise ValueError(
            f"requested {count} real players but the season's pool only has "
            f"{len(pool)} -- lower --real-players-per-team or pick a season "
            "with a deeper pool"
        )
    sample_rng = rng_for(rng_seed, "real_players", "sample")
    return sample_rng.sample(pool, count)


# seeds/teams.json says BRK for Brooklyn; the BDL-derived player files say BKN.
_REAL_DATA_TEAM_CODE = {"BRK": "BKN"}


def _real_players_on_their_real_teams(
    pool: list[dict], *, team_codes: list[str], per_team: int
) -> dict[int, list[dict]]:
    """Each team's `per_team` real players by minutes played that season, keyed by
    team index. Each row's own `team.abbreviation` is the real affiliation, so
    this places a real player on the franchise he actually played for. A team
    with fewer than `per_team` real rows is a hard error, not a silent
    shortfall filled with someone else's players."""
    by_code: dict[str, list[dict]] = {}
    for row in pool:
        by_code.setdefault(row["team"]["abbreviation"], []).append(row)
    placed: dict[int, list[dict]] = {}
    for team_index, code in enumerate(team_codes):
        rows = sorted(
            by_code.get(_REAL_DATA_TEAM_CODE.get(code, code), []),
            key=lambda r: r["meta"].get("minutes_total", 0),
            reverse=True,
        )
        if len(rows) < per_team:
            raise ValueError(
                f"team {code} has only {len(rows)} real players in this season's pool, "
                f"fewer than --real-players-per-team {per_team}"
            )
        placed[team_index] = rows[:per_team]
    return placed


def _real_player_effective_draft_year(real_player: dict, *, season: int) -> int:
    """Same fallback `tools/_gm_real_player_peak_age.py` uses for the ~30%
    of players with no real BDL draft year (undrafted/international): treat
    the season being hydrated as if it were their draft year (age 21 there)
    rather than leaving no age anchor at all. Here there is only ever one
    season in hand (not a whole career), so "earliest season on file" and
    "this season" are the same thing."""
    draft_year = real_player["draft_year"]
    return draft_year if draft_year is not None else season


def _real_player_birth_date(real_player: dict, *, season: int) -> date:
    """`tools/_gm_real_player_etl.py`'s own placeholder convention (no BDL
    source for birth_date at any tier) -- kept consistent rather than
    inventing a second age anchor for the same players."""
    effective_draft_year = _real_player_effective_draft_year(real_player, season=season)
    return date(effective_draft_year - _REAL_PLAYER_DRAFT_AGE_ANCHOR, 6, 15)


def _real_player_years_pro(real_player: dict, *, season: int) -> int:
    return max(0, season - _real_player_effective_draft_year(real_player, season=season))


def real_player_wingspan_in(
    real_player: dict, *, rng_seed: int, team_index: int, slot_index: int
) -> int:
    """A hydrated real player's wingspan. BDL has none: a version-2 seed row
    stores the builder's per-player estimate (RPC-10,
    docs/design/real-player-rating-calibration.md), so the same player is equally
    long in every league and on every slot. A row without one (the 1996-2008
    legacy seasons) is drawn from the real `height_in` with `synthetic_physicals`'
    height-relative convention, keyed by the league seed and roster slot. Shared
    with `tools/validate_sim.py`'s real-player mode so both hydrate the same
    player identically."""
    if real_player.get("wingspan_in") is not None:
        return int(real_player["wingspan_in"])
    height_in = real_player["height_in"]
    wingspan_rng = rng_for(rng_seed, "real_players", "wingspan", team_index, slot_index)
    wingspan_offset = wingspan_rng.gauss(_WINGSPAN_OFFSET_MEAN, _WINGSPAN_OFFSET_SD)
    return int(max(height_in - 1, min(height_in + 9, round(height_in + wingspan_offset))))


async def _create_real_player(
    pool,
    *,
    league_id: int,
    team_id: int,
    team_index: int,
    slot_index: int,
    real_player: dict,
    current_season: int,
    rng_seed: int,
) -> int:
    """One real player, hydrated from a `seeds/real_players/<season>.json`
    row instead of `sim.derivation` -- see module docstring's RPD-4
    section. Mirrors `_create_synthetic_player`'s own transaction shape
    (create -> upsert ratings -> recompute_overall -> derive tendencies)
    so a real player is otherwise indistinguishable downstream from a
    synthetic one; not appended to `records`, since calibration targets
    (`_build_calibration_targets`) measure the synthetic generator's own
    distribution, not real players."""
    position = Position(real_player["primary_position"])
    height_in = real_player["height_in"]
    wingspan_in = real_player_wingspan_in(
        real_player, rng_seed=rng_seed, team_index=team_index, slot_index=slot_index
    )

    async with transaction(pool) as conn:
        player = await player_repo.create_player(
            conn,
            league_id=league_id,
            first_name=real_player["first_name"],
            last_name=real_player["last_name"],
            external_ref=real_player["external_ref"],
            primary_position=position,
            height_in=height_in,
            weight_lb=real_player["weight_lb"],
            wingspan_in=wingspan_in,
            birth_date=_real_player_birth_date(real_player, season=current_season),
            draft_year=real_player["draft_year"],
            years_pro=_real_player_years_pro(real_player, season=current_season),
            team_id=team_id,
            roster_status=RosterStatus.ACTIVE,
            overall_cached=1,
            overall_computed_at=datetime.now(UTC),
        )

        await ratings_repo.upsert_player_ratings(
            conn,
            player_id=player.id,
            league_id=league_id,
            source=RatingSource.REAL_STAT_DERIVED,
            ratings=real_player["ratings"],
            derived_from_season=real_player["derived_from_season"],
            confidence=Decimal(str(real_player["confidence"])),
        )

        await ratings_service.recompute_overall(conn, player.id)

        tendencies_rng = rng_for(rng_seed, "tendencies", team_index, slot_index)
        await ratings_service.derive_tendencies_for_player(conn, player.id, rng=tendencies_rng)

    return player.id


async def _create_synthetic_player(
    pool,
    *,
    league_id: int,
    team_id: int,
    league_seed: int,
    team_index: int,
    slot_index: int,
    position: Position,
    tier: d.RosterTier,
    team_offset: float,
    bio_rng: Random,
    current_season: int,
    records: list[_GeneratedRecord],
) -> int:
    """One player, generated + persisted per §11.4's per-slot pipeline:
    physicals -> talent -> ratings -> `create_player` -> `upsert_player_
    ratings` -> `recompute_overall` -> `ratings_service.
    derive_tendencies_for_player` (which itself calls `derive_tendencies`
    then `upsert_player_tendencies`), all inside one `transaction(pool)`
    block (unchanged from this tool's pre-existing B3 atomicity fix: a
    mid-loop failure must roll back the whole player, not leave one stuck
    at the `overall_cached=1` placeholder)."""
    player_rng = rng_for(league_seed, "synthetic", "player", team_index, slot_index)
    physicals = d.synthetic_physicals(position, player_rng)
    talent = d.tier_talent(tier, team_offset, player_rng)
    ratings = d.synthetic_ratings(talent, position, player_rng)

    first_name = bio_rng.choice(_FIRST_NAMES)
    last_name = bio_rng.choice(_LAST_NAMES)
    birth_year = bio_rng.randint(current_season - 30, current_season - 19)
    birth_date = date(birth_year, bio_rng.randint(1, 12), bio_rng.randint(1, 28))
    years_pro = max(0, current_season - birth_year - 19)
    peak_start = bio_rng.randint(24, 28)
    peak_end = peak_start + bio_rng.randint(2, 6)

    async with transaction(pool) as conn:
        # `overall_cached` is NOT NULL, but a player must exist before
        # `player_ratings.player_id` (its FK) can be inserted -- `1` here
        # is a throwaway placeholder, overwritten below by
        # `ratings_service.recompute_overall`, Decision 12's one
        # legitimate writer, before this transaction commits.
        player = await player_repo.create_player(
            conn,
            league_id=league_id,
            first_name=first_name,
            last_name=last_name,
            primary_position=position,
            height_in=physicals.height_in,
            weight_lb=physicals.weight_lb,
            wingspan_in=physicals.wingspan_in,
            birth_date=birth_date,
            years_pro=years_pro,
            team_id=team_id,
            roster_status=RosterStatus.ACTIVE,
            overall_cached=1,
            overall_computed_at=datetime.now(UTC),
        )

        # §5.4: `synthetic_ratings` returns exactly the 55 rating names;
        # the two age columns are merged in here, not inside `sim/`.
        full_ratings = {**ratings, "peak_age_start": peak_start, "peak_age_end": peak_end}
        await ratings_repo.upsert_player_ratings(
            conn,
            player_id=player.id,
            league_id=league_id,
            source=RatingSource.GENERATED,
            ratings=full_ratings,
        )

        overall_cached = await ratings_service.recompute_overall(conn, player.id)

        # Salted by (team_index, slot_index) -- the same stable in-run
        # identity `_create_synthetic_player`'s other streams already use
        # (see the `player_rng` line above) -- rather than by `player.id`.
        # A DB-assigned serial id is not stable across two separate runs of
        # this tool at the same `--rng-seed` (a fresh insert gets a fresh
        # id every time), which used to make every tendencies draw -- and
        # therefore every `team_strategy` numeric that reads one -- differ
        # run to run despite the fixed seed (critic round 17, B17-2).
        # Routed through `ratings_service.derive_tendencies_for_player`
        # rather than calling `ratings_repo.upsert_player_tendencies`
        # directly, so this stays the "one pure function, one writer" path
        # `ratings_service`'s module docstring documents (critic round 17,
        # B17-3) -- `derive_tendencies_for_league`'s bulk pairing isn't
        # used here because it salts by `player.id` itself (§11.3, a
        # different, DB-scoped bulk-admin path this per-slot pipeline
        # doesn't need) and this tool already has a per-player transaction
        # loop to route `_for_player` through.
        tendencies_rng = rng_for(league_seed, "tendencies", team_index, slot_index)
        tendencies_row = await ratings_service.derive_tendencies_for_player(
            conn, player.id, rng=tendencies_rng
        )
        tendencies = {
            f.name: getattr(tendencies_row, f.name)
            for f in dc_fields(tendencies_row)
            if f.name not in {"player_id", "league_id", "updated_at"}
        }

    records.append(
        _GeneratedRecord(
            position=position, ratings=ratings, overall_cached=overall_cached, tendencies=tendencies
        )
    )
    return player.id


async def _create_synthetic_team(
    pool,
    *,
    league_id: int,
    team: Team,
    team_index: int,
    league_seed: int,
    players_per_team: int,
    current_season: int,
    bio_rng: Random,
    records: list[_GeneratedRecord],
    real_players: list[dict],
) -> None:
    """§5.1/§5.2: one team-offset draw and one `synthetic_positions` draw
    per team, both from the team-scoped stream; then §11.4's per-slot
    pipeline for each of `players_per_team` slots.

    `real_players` (RPD-4 hydration, empty by default) takes the first
    `len(real_players)` slots verbatim; each remaining slot still draws
    from its own `(team_index, slot_index)`-keyed stream exactly as if no
    hydration were happening, so enabling hydration never changes what a
    non-hydrated slot would have generated."""
    team_rng = rng_for(league_seed, "synthetic", "team", team_index)
    team_offset = team_rng.gauss(0.0, 2.5)
    positions = d.synthetic_positions(team_rng)

    for slot_index in range(players_per_team):
        if slot_index < len(real_players):
            await _create_real_player(
                pool,
                league_id=league_id,
                team_id=team.id,
                team_index=team_index,
                slot_index=slot_index,
                real_player=real_players[slot_index],
                current_season=current_season,
                rng_seed=league_seed,
            )
            continue
        await _create_synthetic_player(
            pool,
            league_id=league_id,
            team_id=team.id,
            league_seed=league_seed,
            team_index=team_index,
            slot_index=slot_index,
            position=_slot_position(positions, slot_index),
            tier=_slot_tier(slot_index),
            team_offset=team_offset,
            bio_rng=bio_rng,
            current_season=current_season,
            records=records,
        )


def _percentile(sorted_values: list[int], p: float) -> float:
    idx = int(round(p * (len(sorted_values) - 1)))
    return sorted_values[idx]


def _group_mean(record: _GeneratedRecord, group: str) -> float:
    fields = d.RATING_GROUPS[group]
    return sum(record.ratings[f] for f in fields) / len(fields)


def _build_calibration_targets(
    records: list[_GeneratedRecord],
    *,
    league_seed: int,
    teams: int,
    players_per_team: int,
) -> dict[str, object]:
    """§7's `seeds/synthetic_calibration_targets.json` shape. `groups[*].sd`
    is §6.6's own named statistic -- the SD, across players, of each
    player's own mean over that group's fields -- not the SD over every
    individual field value; `by_position` records group means only, per
    §7's own schema note."""
    overalls = sorted(r.overall_cached for r in records)
    overall_block = {
        "mean": statistics.mean(overalls),
        "sd": statistics.pstdev(overalls),
        "min": overalls[0],
        "max": overalls[-1],
        "p10": _percentile(overalls, 0.10),
        "p50": _percentile(overalls, 0.50),
        "p90": _percentile(overalls, 0.90),
    }

    groups_block = {
        group: {
            "mean": statistics.mean(_group_mean(r, group) for r in records),
            "sd": statistics.pstdev(_group_mean(r, group) for r in records),
        }
        for group in d.RATING_GROUPS
    }

    by_position_block: dict[str, dict[str, dict[str, float]]] = {}
    for position in Position:
        position_records = [r for r in records if r.position is position]
        if not position_records:
            by_position_block[position.value] = {}
            continue
        by_position_block[position.value] = {
            group: {"mean": statistics.mean(_group_mean(r, group) for r in position_records)}
            for group in d.RATING_GROUPS
        }

    tendency_field_names = records[0].tendencies.keys() if records else []
    tendencies_block = {
        field_name: {
            "mean": statistics.mean(r.tendencies[field_name] for r in records),
            "sd": statistics.pstdev(r.tendencies[field_name] for r in records),
        }
        for field_name in tendency_field_names
    }

    return {
        "source": "synthetic-v1 (docs/design/phase-1-step-7-calibration.md)",
        "generated_at": datetime.now(UTC).isoformat(),
        "league_seed": league_seed,
        "teams": teams,
        "players_per_team": players_per_team,
        "overall": overall_block,
        "groups": groups_block,
        "by_position": by_position_block,
        "tendencies": tendencies_block,
    }


# --replace-league -----------------------------------------------------------
#
# Reseeding a Discord guild used to be a manual procedure: kill the bot, delete
# the league's channels through Discord's HTTP API, `DELETE FROM leagues` (the
# registry tables cascade), reseed. `leagues.discord_guild_id` is UNIQUE, so a
# reseed against a guild that already has a league cannot happen without it,
# and deleting only the rows would orphan the channels in Discord. The bot MUST
# be stopped first: a running bot's startup reconcile would recreate channels
# for the league mid-replacement.
#
# Ordering is the safety property: every Discord delete happens BEFORE any
# database delete, and a Discord failure (anything but "already gone") aborts
# with the rows still in place. A rerun is therefore always safe -- channels
# already deleted answer 404 and count as gone.

# Discord API v10, `DELETE /channels/{channel.id}` (needs MANAGE_CHANNELS).
_DISCORD_API_BASE = "https://discord.com/api/v10"
# Discord blocks clients without a `DiscordBot ($url, $versionNumber)` user agent.
_DISCORD_USER_AGENT = "DiscordBot (https://github.com/dba2/dba2, 1.0) seed_dev_league"
_DISCORD_MAX_ATTEMPTS = 3
_DISCORD_MAX_RETRY_AFTER_S = 30.0

DeleteChannel = Callable[[int], Awaitable[bool]]
"""Deletes one Discord channel. Returns True if it deleted the channel, False if
Discord says it is already gone or unreachable (404/403); raises
`ReplaceLeagueError` for anything else. Injectable so tests never touch the
network or the token."""


class ReplaceLeagueError(RuntimeError):
    """`--replace-league` refused or failed. Messages carry ids and HTTP status
    codes only -- never the token."""


@dataclass(frozen=True)
class ReplacementPlan:
    guild_id: int
    league_ids: tuple[int, ...]
    channel_ids: tuple[int, ...]


def build_replacement_plan(
    guild_id: int,
    league_rows: Sequence[tuple[int, int]],
    channel_rows: Sequence[tuple[int, int]],
) -> ReplacementPlan:
    """The pure half of the replacement: `league_rows` are `(league_id,
    discord_guild_id)`, `channel_rows` are `(league_id, channel_id)` in the order
    they should be deleted. Refuses a league whose guild id is not `guild_id`, and
    a channel row that belongs to none of the leagues (either would delete
    something the caller did not name). Duplicate channel ids collapse."""
    if guild_id <= 0:
        raise ReplaceLeagueError(f"refusing to replace: guild id {guild_id} is not a valid id")
    league_ids = tuple(league_id for league_id, _ in league_rows)
    for league_id, league_guild_id in league_rows:
        if league_guild_id != guild_id:
            raise ReplaceLeagueError(
                f"refusing to replace league {league_id}: its guild id "
                f"{league_guild_id} is not the requested guild id {guild_id}"
            )
    channel_ids: list[int] = []
    for league_id, channel_id in channel_rows:
        if league_id not in league_ids:
            raise ReplaceLeagueError(
                f"refusing to replace: channel {channel_id} belongs to league {league_id}, "
                f"which is not one of guild {guild_id}'s leagues {list(league_ids)}"
            )
        if channel_id not in channel_ids:
            channel_ids.append(channel_id)
    return ReplacementPlan(guild_id, league_ids, tuple(channel_ids))


async def load_replacement_plan(ex: Executor, guild_id: int) -> ReplacementPlan:
    """Reads `guild_id`'s leagues and every Discord channel the registry holds for
    them: `league_channels` (#games, #standings, #wire, ...) then the per-team
    `team_gm_channels` (archived ones included -- archiving only renames them)."""
    league_records = await ex.fetch(
        "SELECT id, discord_guild_id FROM leagues WHERE discord_guild_id = $1 ORDER BY id",
        guild_id,
    )
    league_rows = [(r["id"], r["discord_guild_id"]) for r in league_records]
    league_ids = [league_id for league_id, _ in league_rows]
    channel_rows: list[tuple[int, int]] = []
    for table in ("league_channels", "team_gm_channels"):
        records = await ex.fetch(
            f"SELECT league_id, channel_id FROM {table} "  # fixed table names, no user input
            "WHERE league_id = ANY($1::bigint[]) ORDER BY league_id, id",
            league_ids,
        )
        channel_rows.extend((r["league_id"], r["channel_id"]) for r in records)
    return build_replacement_plan(guild_id, league_rows, channel_rows)


def _delete_channel_blocking(token: str, channel_id: int) -> bool:
    for attempt in range(1, _DISCORD_MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            f"{_DISCORD_API_BASE}/channels/{channel_id}",
            method="DELETE",
            headers={"Authorization": f"Bot {token}", "User-Agent": _DISCORD_USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=30):
                return True
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return False
            if exc.code == 429 and attempt < _DISCORD_MAX_ATTEMPTS:
                try:
                    wait = float(exc.headers.get("Retry-After", "1"))
                except ValueError:
                    wait = 1.0
                if wait <= _DISCORD_MAX_RETRY_AFTER_S:
                    time.sleep(wait)
                    continue
            raise ReplaceLeagueError(
                f"Discord refused to delete channel {channel_id}: HTTP {exc.code}"
            ) from None
        except urllib.error.URLError as exc:
            raise ReplaceLeagueError(
                f"could not reach Discord to delete channel {channel_id}: "
                f"{type(exc.reason).__name__}"
            ) from None
    raise ReplaceLeagueError(f"Discord kept rate-limiting the delete of channel {channel_id}")


async def delete_discord_channel(channel_id: int) -> bool:
    """The real `DeleteChannel`: `DELETE /channels/{id}` with the configured bot
    token, read from `get_config()` at call time and never printed or logged."""
    token = get_config().discord_token
    if not token:
        raise ReplaceLeagueError("DISCORD_TOKEN is not configured; cannot delete channels")
    return await asyncio.to_thread(_delete_channel_blocking, token, channel_id)


async def replace_guild_leagues(
    pool: Executor,
    guild_id: int,
    *,
    delete_channel: DeleteChannel,
    out: Callable[[str], None] = print,
) -> ReplacementPlan:
    """Removes every league of `guild_id` and its Discord channels, printing only
    ids. Discord first, database second (see the section comment): a
    `delete_channel` error propagates before any row is touched. The delete is one
    transaction that also re-checks the guild id and the row count, so a league
    changing under us rolls the whole thing back."""
    plan = await load_replacement_plan(pool, guild_id)
    if not plan.league_ids:
        out(f"No existing league for guild {guild_id}; nothing to replace.")
        return plan

    for channel_id in plan.channel_ids:
        deleted = await delete_channel(channel_id)
        out(f"channel {channel_id}: {'deleted' if deleted else 'already gone'}")

    async with transaction(pool) as conn:
        status = await conn.execute(
            "DELETE FROM leagues WHERE id = ANY($1::bigint[]) AND discord_guild_id = $2",
            list(plan.league_ids),
            guild_id,
        )
        removed = int(status.rsplit(" ", 1)[-1])
        if removed != len(plan.league_ids):
            raise ReplaceLeagueError(
                f"expected to delete leagues {list(plan.league_ids)} but deleted {removed}; "
                "rolled back"
            )
    for league_id in plan.league_ids:
        out(f"league {league_id}: removed")
    return plan


async def seed_dev_league(
    *,
    discord_guild_id: int,
    name: str,
    start_season_year: int,
    current_season: int,
    commissioner_user_id: int,
    rng_seed: int,
    players_per_team: int = _DEFAULT_PLAYERS_PER_TEAM,
    emit_targets_path: str | None = None,
    real_players_season: int | None = None,
    real_players_per_team: int = 0,
    real_team_rosters: bool = False,
    replace_league: bool = False,
    delete_channel: DeleteChannel | None = None,
    allow_legacy_ratings: bool = False,
) -> int:
    """Creates the league (`league_service.create_league` -- 30 teams + cap
    settings, one atomic unit), then generates `players_per_team`
    calibrated synthetic players per seeded team via `sim.derivation`
    (Decision S7-14), and finally `strategy_service.
    ensure_defaults_for_season` for every team's `team_strategy` row.
    Returns the new league's id.

    `rng_seed` is used directly as the salt root for every generation
    stream below (rather than re-reading `leagues.rng_seed` back out) --
    it is the same value `league_service.create_league` just persisted
    into that column, so re-reading it would be a redundant round trip.

    `real_players_season`/`real_players_per_team` (RPD-4, both default
    off) hydrate that many of each team's slots from real players instead
    of the synthetic pipeline -- see module docstring's RPD-4 section.

    `replace_league` (default off, behaviour unchanged): first remove any
    league already registered for `discord_guild_id` and its Discord
    channels (`replace_guild_leagues`); `delete_channel` overrides the real
    Discord HTTP call (tests pass a fake).

    `allow_legacy_ratings` (default off): a `real_players_season` whose seed rows
    are not RPD-8 builder version 2 is refused (`LegacyRatingsError`); this opts
    back into the legacy ratings.
    """
    if real_players_per_team > 0 and real_players_season is None:
        raise ValueError("real_players_per_team > 0 requires real_players_season")
    if real_players_per_team > players_per_team:
        raise ValueError(
            f"real_players_per_team ({real_players_per_team}) exceeds "
            f"players_per_team ({players_per_team})"
        )

    # Loaded (and version-checked) BEFORE anything touches the database: a
    # refused season must not first delete the guild's existing league via
    # `replace_league`.
    pool_players: list[dict] = []
    if real_players_per_team > 0:
        pool_players = _load_real_player_pool(
            real_players_season, allow_legacy_ratings=allow_legacy_ratings
        )

    pool = await get_pool()

    if replace_league:
        await replace_guild_leagues(
            pool,
            discord_guild_id,
            delete_channel=delete_channel or delete_discord_channel,
        )

    league = await league_service.create_league(
        pool,
        discord_guild_id=discord_guild_id,
        name=name,
        start_season_year=start_season_year,
        current_season=current_season,
        commissioner_user_id=commissioner_user_id,
        rng_seed=rng_seed,
    )

    bio_rng = rng_for(rng_seed, "synthetic", "bio")
    teams = await team_repo.list_teams_for_league(pool, league.id)

    real_players_by_team: dict[int, list[dict]] = {}
    if real_players_per_team > 0:
        if real_team_rosters:
            real_players_by_team = _real_players_on_their_real_teams(
                pool_players,
                team_codes=[team.code for team in teams],
                per_team=real_players_per_team,
            )
        else:
            sampled = _sample_real_players(
                pool_players, rng_seed=rng_seed, count=real_players_per_team * len(teams)
            )
            for team_index in range(len(teams)):
                start = team_index * real_players_per_team
                real_players_by_team[team_index] = sampled[start : start + real_players_per_team]

    records: list[_GeneratedRecord] = []
    for team_index, team in enumerate(teams):
        await _create_synthetic_team(
            pool,
            league_id=league.id,
            team=team,
            team_index=team_index,
            league_seed=rng_seed,
            players_per_team=players_per_team,
            current_season=current_season,
            bio_rng=bio_rng,
            records=records,
            real_players=real_players_by_team.get(team_index, []),
        )

    await strategy_service.ensure_defaults_for_season(pool, league.id, current_season)

    # Decision P4-67 (Phase 4 Stage C step 13a): wire agency into the
    # dev-league path FIRST -- P4-50's morale-trim verification was
    # relocated onto this exact path, and until this call exists there is
    # no `player_motivation` row anywhere in a dev league for that trim
    # to read (13b's check would be vacuously green: no motivation means
    # every trim reads the neutral default, indistinguishable from a
    # correctly-suppressed control). One call, after every player this
    # league will ever have at seed time already exists -- `bootstrap_
    # league_motivation` is idempotent per player, so a later re-seed
    # pass (none exists today) would simply skip everyone already here.
    await agency_service.bootstrap_league_motivation(
        pool, league_id=league.id, league_rng_seed=rng_seed, season=current_season
    )

    if emit_targets_path is not None:
        targets = _build_calibration_targets(
            records,
            league_seed=rng_seed,
            teams=len(teams),
            players_per_team=players_per_team,
        )
        Path(emit_targets_path).write_text(
            json.dumps(targets, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    return league.id


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a dev league with 30 real NBA teams and a calibrated synthetic "
            "roster (sim.derivation, §12 item 7) -- the manual smoke-test / Phase 1 "
            "harness path (§8 step 8)."
        )
    )
    parser.add_argument(
        "--guild-id",
        type=int,
        default=None,
        help=(
            "Discord guild id (random if omitted -- must be unique per league; "
            "required with --replace-league)."
        ),
    )
    parser.add_argument("--name", default="Dev League")
    parser.add_argument("--start-season-year", type=int, default=2026)
    parser.add_argument("--current-season", type=int, default=2026)
    parser.add_argument("--commissioner-user-id", type=int, default=1)
    parser.add_argument("--rng-seed", type=int, default=1)
    parser.add_argument(
        "--players-per-team",
        type=int,
        default=_DEFAULT_PLAYERS_PER_TEAM,
        help=(
            "Decision S7-1a: the tier template (§5.1) assumes 14. Other values "
            "degrade gracefully -- slots beyond 14 take BENCH, fewer than 14 "
            "truncates from the end."
        ),
    )
    parser.add_argument(
        "--emit-targets",
        default=None,
        metavar="PATH",
        help="Write §7's calibration-target JSON (e.g. seeds/synthetic_calibration_targets.json).",
    )
    parser.add_argument(
        "--real-players-season",
        type=int,
        default=None,
        metavar="YEAR",
        help=(
            "RPD-4: hydrate --real-players-per-team slots per team from "
            "seeds/real_players/<YEAR>.json instead of synthetic generation "
            "(rebuilt ratings v2: 2009-2025; 1996-2008 are legacy and need "
            "--allow-legacy-ratings)."
        ),
    )
    parser.add_argument(
        "--real-players-per-team",
        type=int,
        default=0,
        help="RPD-4: how many of each team's slots are real players (default 0, off).",
    )
    parser.add_argument(
        "--real-team-rosters",
        action="store_true",
        help=(
            "With --real-players-per-team: put each real player on the team he played "
            "for that season (top players by minutes) instead of dealing them at random."
        ),
    )
    parser.add_argument(
        "--replace-league",
        action="store_true",
        help=(
            "Before seeding, delete every existing league for --guild-id together with "
            "its Discord channels (the channel deletes use the configured bot token; "
            "a channel Discord reports gone counts as done). STOP THE BOT FIRST."
        ),
    )
    parser.add_argument(
        "--allow-legacy-ratings",
        action="store_true",
        help=(
            "With --real-players-season: accept a season whose seed rows are not "
            "RPD-8 builder version 2 (1996-2008 are legacy) instead of refusing. "
            "Legacy ratings compress stars toward 70."
        ),
    )
    args = parser.parse_args()
    if args.replace_league and args.guild_id is None:
        parser.error("--replace-league requires an explicit --guild-id")
    if args.guild_id is None:
        args.guild_id = random.randint(10_000_000, 2_000_000_000)
    return args


async def _main() -> None:
    args = _parse_args()
    try:
        league_id = await seed_dev_league(
            discord_guild_id=args.guild_id,
            name=args.name,
            start_season_year=args.start_season_year,
            current_season=args.current_season,
            commissioner_user_id=args.commissioner_user_id,
            rng_seed=args.rng_seed,
            players_per_team=args.players_per_team,
            emit_targets_path=args.emit_targets,
            real_players_season=args.real_players_season,
            real_players_per_team=args.real_players_per_team,
            real_team_rosters=args.real_team_rosters,
            replace_league=args.replace_league,
            allow_legacy_ratings=args.allow_legacy_ratings,
        )
        print(
            f"Created league id={league_id} ('{args.name}') with 30 teams and "
            f"{args.players_per_team} players/team."
        )
        if args.real_players_per_team > 0:
            print(
                f"Hydrated {args.real_players_per_team} real players/team from "
                f"season {args.real_players_season}."
            )
        if args.emit_targets is not None:
            print(f"Wrote calibration targets to {args.emit_targets}")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
