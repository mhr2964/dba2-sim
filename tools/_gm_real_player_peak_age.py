"""One-time post-processing pass computing real `peak_age_start`/
`peak_age_end` for every real player in `seeds/real_players/`, from their
own already-backfilled career (RPD-7 item 9,
docs/design/real-player-data-integration.md). No new BDL calls -- this
reads/rewrites only the 30 season files `tools/_gm_real_player_etl.py`
already produced.

**Method, chosen via an inline `>>think` pass with the user over three
options (point estimate, near-peak band, fixed span matching the
synthetic generator's convention) -- near-peak band won:** for each
player, take their career-best `compute_overall()` across every real
season on file, then every season within `_NEAR_PEAK_TOLERANCE` (3.0)
rating points of that best; `peak_age_start`/`peak_age_end` are the
min/max age across that near-peak set. A player with only one real
season on file degenerates to a zero-width window (min == max of a
one-element set) -- not a special case, just what the same formula
already does.

`_age_in_season` reuses `tools/_gm_real_player_etl.py`'s own
`birth_date = date(draft_year - 21, 6, 15)` placeholder convention
(`managers.scouting.player_age` computes `age = season - birth_date.year`,
so `age = season - draft_year + 21`) rather than inventing a second age
anchor -- whatever `birth_date` eventually resolves to for these players,
this stays consistent with it.

**867 of 2858 players (undrafted/no-BDL-draft-data) have `draft_year is
None` in every one of their own season entries** (verified: never
inconsistent within a player when present -- either every entry for a
player has the same real draft_year, or every entry has `None`). For
those, there is no anchor to compute an age from at all. Fallback: treat
the player's own earliest season present in this backfill as if it were
their draft year (i.e. assume age 21 there, same "enters around 21"
assumption the rest of this convention already makes) -- not a new
assumption, just the existing one anchored to the earliest evidence
available instead of a real draft year. This under-estimates true age
for anyone whose real career started before 1996 (the backfill's own
floor), a pre-existing scope limit of the whole backfill, not something
this pass introduces.

**Refuses version-2 seed files (RPD-8).** The RPD-8 builder
(`tools/_gm_real_player_build.py`) now owns `peak_age_start`/`peak_age_end` for every
rebuilt season, computed from version-2 seasons only. This pass reads every file's
ratings as one scale and rewrites all of them, so on a directory holding version-2
seasons it would compute windows across two rating scales and silently overwrite the
builder's; it exits 1 without writing instead. Only a directory of purely legacy seeds
can still be processed here.

Quarantined tool, same convention as `_gm_real_player_etl.py`: lives in
`tools/`, never imported by `dba2/` itself.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from dba2.domain.player import Position
from dba2.sim.ratings import compute_overall
from tools._real_fields import REAL_BUILDER_VERSION

_SEEDS_DIR = Path(__file__).resolve().parent.parent / "seeds" / "real_players"
_DRAFT_AGE_ANCHOR = 21
_NEAR_PEAK_TOLERANCE = 3.0

_OLD_NOTE_CLAUSE = (
    "peak_age_start/peak_age_end and birth_date are placeholders, not "
    "real career data, this pass"
)
_NEW_NOTE_CLAUSE = (
    "birth_date is a placeholder, not real career data; peak_age_start/"
    "peak_age_end are real, computed by tools/_gm_real_player_peak_age.py "
    "(see its own docstring for the near-peak-band method)"
)


def _season_files() -> list[Path]:
    return sorted(_SEEDS_DIR.glob("[12][0-9][0-9][0-9].json"))


def _age_in_season(season: int, draft_year: int) -> int:
    return season - draft_year + _DRAFT_AGE_ANCHOR


def _builder_owned_files(seeds: dict[Path, dict]) -> list[Path]:
    """The season files with at least one RPD-8 version-2 row: their peak ages
    belong to `tools/_gm_real_player_build.py`, not to this pass."""
    return [
        path
        for path, seed in seeds.items()
        if any(
            (player.get("meta") or {}).get("builder_version") == REAL_BUILDER_VERSION
            for player in seed["players"]
        )
    ]


def main() -> int:
    files = _season_files()
    if not files:
        print(f"No season files found under {_SEEDS_DIR}")
        return 1

    seeds = {f: json.loads(f.read_text(encoding="utf-8")) for f in files}

    owned = _builder_owned_files(seeds)
    if owned:
        print(
            f"refusing to rewrite peak ages: {len(owned)} season file(s) "
            f"({owned[0].stem}..{owned[-1].stem}) are ratings version {REAL_BUILDER_VERSION} "
            "and the RPD-8 builder owns their peak_age_start/peak_age_end. Re-run "
            "`python -m tools._gm_real_player_build --all --out-dir seeds/real_players "
            "--allow-seeds-dir` to refresh them.",
            file=sys.stderr,
        )
        return 1

    # external_ref -> [(season, overall, draft_year), ...] across every
    # real season on file, before the age anchor is resolved.
    raw_careers: dict[str, list[tuple[int, int, int | None]]] = {}
    for seed in seeds.values():
        for player in seed["players"]:
            position = Position(player["primary_position"])
            overall = compute_overall(SimpleNamespace(**player["ratings"]), position)
            raw_careers.setdefault(player["external_ref"], []).append(
                (player["season"], overall, player["draft_year"])
            )

    # Undrafted players (draft_year is None for every one of their own
    # entries -- never inconsistent within a player, verified separately)
    # anchor on their own earliest season on file instead (see docstring).
    effective_draft_year: dict[str, int] = {}
    for external_ref, entries in raw_careers.items():
        draft_year = entries[0][2]
        effective_draft_year[external_ref] = (
            draft_year if draft_year is not None else min(season for season, _, _ in entries)
        )

    careers: dict[str, list[tuple[int, int]]] = {
        external_ref: [
            (_age_in_season(season, effective_draft_year[external_ref]), overall)
            for season, overall, _ in entries
        ]
        for external_ref, entries in raw_careers.items()
    }

    peak_windows: dict[str, tuple[int, int]] = {}
    for external_ref, entries in careers.items():
        best_overall = max(overall for _, overall in entries)
        near_peak_ages = [
            age for age, overall in entries if overall >= best_overall - _NEAR_PEAK_TOLERANCE
        ]
        peak_windows[external_ref] = (min(near_peak_ages), max(near_peak_ages))

    for path, seed in seeds.items():
        for player in seed["players"]:
            start, end = peak_windows[player["external_ref"]]
            player["ratings"]["peak_age_start"] = start
            player["ratings"]["peak_age_end"] = end
        if _OLD_NOTE_CLAUSE in seed["_note"]:
            seed["_note"] = seed["_note"].replace(_OLD_NOTE_CLAUSE, _NEW_NOTE_CLAUSE)
        path.write_text(json.dumps(seed, indent=2), encoding="utf-8")

    print(
        f"Computed real peak_age_start/peak_age_end for {len(peak_windows)} "
        f"players across {len(files)} season files."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
