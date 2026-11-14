"""CLI: generates/refreshes `seeds/gm_decision_log_digest.json` --
Decision P3-29(b)'s committed Phase-2 control digest (Phase 3's design doc,
`docs/design/phase-3-inter-manager-society.md` Sec.11 Stage A step 2).

A per-season SHA-256 over `SeasonRecord.jsonl_lines` at **1 seed x 2
seasons x `--skip-games`** (~34s, from the design doc's own Sec.13.3-cited
85.0s-per-seed-at-5-seasons baseline -- 2 seasons exercises the season
boundary Phase 3's later decay applier uses). Generated and committed
*before any Phase 3 mechanism exists in the codebase*, which is what makes
it a baseline for a later step (M29, `control_equivalence`) to diff
against rather than a snapshot of an already-different state.

Mirrors `tools/validate_gm.py`'s own shape: `argparse`, `generate_digest
(...)` as the importable entry point, `main()`, `if __name__ ==
"__main__": main()`, invoked `python -m tools.generate_gm_decision_log_
digest` from the repo root.

**Seed derivation matches `validate_gm.py`'s own seed-0 formula exactly**
(`rng_for(rng_seed, "validate_gm", "seed", 0).getrandbits(63)`) so this
digest's own seed is the same one a `python -m tools.validate_gm --seeds 1
--seasons 2 --skip-games` run at this file's default `--rng-seed 1` would
use for its only seed -- one derivation scheme project-wide, not two.

The digest computation itself (`season_decision_log_digest`) lives in
`tools/gm_harness/metrics.py`, per Decision P3-29(b) -- this module is
only the CLI shell and the `generate_digest` orchestration `run_seed`
needs, matching `validate_gm.py`'s own metrics/CLI split.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dba2.core.rng import rng_for
from dba2.managers.tuning import DEFAULT_TUNING, GM_TUNING_VERSION
from tools.gm_harness.metrics import season_decision_log_digest
from tools.gm_harness.season import RunRecord, SeasonRecord, run_seed

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUT_PATH = _REPO_ROOT / "seeds" / "gm_decision_log_digest.json"


def _run_configured_seed(
    *, rng_seed: int, league_id: int, season_year: int, seasons: int, players_per_team: int,
) -> tuple[int, RunRecord]:
    """Derives the seed the same way `validate_gm.py`'s own per-seed loop
    derives seed 0, then runs it. Returns `(derived_seed, record)` so a
    caller (the report payload builder, or a rerun-comparison test) has
    both without re-deriving.

    **`max_negotiation_rounds=0`, explicit, since Phase 3 §11 step 7 wired
    the negotiation loop into `run_trade_window` and made it `run_seed`'s
    own DEFAULT behavior.** This generator's entire job is reproducing
    Decision P3-29(b)'s committed control baseline -- captured "before any
    Phase 3 mechanism exists in the codebase" -- byte-for-byte, forever.
    Without this, `generate_digest`/this module's own "reproduces on
    rerun" and "compares clean today" tests would silently start
    reproducing a DIFFERENT, negotiation-enabled run instead of the frozen
    baseline, which is exactly the failure this control artifact exists to
    catch. `max_negotiation_rounds=0` reduces the negotiation loop to
    exactly Phase 2's own round-0-only accept/reject read (Decision P3-8),
    which is what makes this still the right baseline rather than a stale
    one.

    **`head_to_head_enabled=False`, also explicit, since Phase 3 §11 step
    17 wired channel 1 (Decision P3-48).** Channel 1's per-partner target
    count is decided inside `propose_packages`, upstream of any
    negotiation turn -- `max_negotiation_rounds=0` alone never touches it,
    because it nullifies channels 2/3 by making the negotiation PROTOCOL
    unreachable, not by suppressing the memory those channels and channel
    1 alike read. This generator's whole job is reproducing a pre-Phase-3
    baseline forever, and `max_negotiation_rounds=0` alone stopped being
    sufficient for that the moment a supply-side channel (channel 1)
    existed to read `state.head_to_head` before round 0 is ever reached.

    **`agency_enabled=False`, `media_enabled=False`, also explicit, since
    Phase 4 §11 Stage A step 1 built both flags (Decision P4-2).** Neither
    flag does anything today -- no Phase 4 mechanism exists yet, so
    `run_seed` at its own `True` defaults would reproduce byte-for-byte the
    same run these two explicit `False`s do. **Decision P4-55 requires
    pinning them here anyway, now, while the pin is free**, because
    `head_to_head_enabled` above is the proof of what happens when a flag
    is left on this call site's implicit default instead: Phase 3 built
    that flag in its own Stage A and did not add it here, and by the time
    Stage C's channel 1 actually needed suppressing, this generator's
    default-`True` call would have silently started reproducing a
    DIFFERENT run than the one already committed to `seeds/gm_decision_
    log_digest.json` -- caught only because Decision P3-48 happened to
    notice mid-build. Pinning `agency_enabled=False`/`media_enabled=False`
    here, in Stage A, before either flag's mechanism exists, means the day
    a Phase 4 mechanism starts reading one of them is the day this
    generator either keeps reproducing the frozen baseline (because it
    explicitly asked for the mechanism off) or a reviewer sees the pin
    removed in the same diff that added the mechanism -- never a silent
    drift discovered after the fact."""
    derived_seed = rng_for(rng_seed, "validate_gm", "seed", 0).getrandbits(63)
    record = run_seed(
        league_id=league_id, rng_seed=derived_seed, first_season=season_year, seasons=seasons,
        players_per_team=players_per_team, skip_games=True, tuning=DEFAULT_TUNING,
        max_negotiation_rounds=0, head_to_head_enabled=False,
        agency_enabled=False, media_enabled=False,
    )
    return derived_seed, record


def generate_digest(
    *,
    rng_seed: int = 1,
    league_id: int = 1,
    season_year: int = 2026,
    seasons: int = 2,
    players_per_team: int = 15,
) -> dict:
    """Runs one seed at `seasons` seasons under `--skip-games` and returns
    the JSON-serializable payload committed at `seeds/gm_decision_log_
    digest.json`. Importable so both this step's "reproduces on rerun"
    verify test and the digest comparator's own tests can call it
    in-process (twice, for the rerun check) without shelling out."""
    derived_seed, record = _run_configured_seed(
        rng_seed=rng_seed, league_id=league_id, season_year=season_year, seasons=seasons,
        players_per_team=players_per_team,
    )
    return {
        "_description": (
            "Decision P3-29(b)'s committed control digest -- a per-season "
            "SHA-256 over SeasonRecord.jsonl_lines at 1 seed x 2 seasons x "
            "--skip-games, generated before any Phase 3 mechanism exists in "
            "the codebase. Regenerate with `python -m tools."
            "generate_gm_decision_log_digest`; see that module's own "
            "docstring for the seed-derivation formula. No dependency on "
            "any other Phase 3 step or switch -- this is a baseline, not a "
            "gate."
        ),
        "rng_seed": rng_seed,
        "derived_seed": derived_seed,
        "league_id": league_id,
        "season_year": season_year,
        "seasons": seasons,
        "players_per_team": players_per_team,
        "skip_games": True,
        "gm_tuning_version": GM_TUNING_VERSION,
        "season_digests": [
            {
                "season": season_record.season,
                "digest": season_decision_log_digest(season_record.jsonl_lines),
                "line_count": len(season_record.jsonl_lines),
            }
            for season_record in record.seasons
        ],
    }


def _seasons_for_rerun_comparison(
    *,
    rng_seed: int = 1,
    league_id: int = 1,
    season_year: int = 2026,
    seasons: int = 2,
    players_per_team: int = 15,
) -> tuple[SeasonRecord, ...]:
    """Returns the raw `SeasonRecord`s (not the JSON payload) for a fresh
    run at the same configuration -- used by the digest comparator's own
    line-level localization, which needs live `jsonl_lines` to diff
    against rather than the committed file's hashes alone."""
    _, record = _run_configured_seed(
        rng_seed=rng_seed, league_id=league_id, season_year=season_year, seasons=seasons,
        players_per_team=players_per_team,
    )
    return record.seasons


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate/refresh seeds/gm_decision_log_digest.json, Decision "
            "P3-29(b)'s committed Phase-2 control digest."
        )
    )
    parser.add_argument("--rng-seed", type=int, default=1)
    parser.add_argument("--league-id", type=int, default=1)
    parser.add_argument("--season-year", type=int, default=2026)
    parser.add_argument("--seasons", type=int, default=2)
    parser.add_argument("--players-per-team", type=int, default=15)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT_PATH)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = generate_digest(
        rng_seed=args.rng_seed, league_id=args.league_id, season_year=args.season_year,
        seasons=args.seasons, players_per_team=args.players_per_team,
    )
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.out} ({len(payload['season_digests'])} season(s))")


if __name__ == "__main__":
    main()
