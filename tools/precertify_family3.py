"""CLI: (re-)precertify Family 3's `gated_value_precertified` citations
(Decision P3-47, `docs/design/phase-3-inter-manager-society.md` Sec.9/
Sec.11 step 9, `docs/ledger/phase-3.md` rounds 36-38).

Promotes `artifacts/scratch_measure_m14_se_v3.py` (throwaway, gitignored)
to a committed, resumable tool -- same checkpointed, atomic-per-seed-write
pattern (this environment has killed long background runs mid-flight
before; a kill after seed 40/45 must resume at 40, not restart at 0).

**What this measures.** M11-M13/M15/M16's own `params.precision.sd_across_
seeds` citations already measure the STANDARD ERROR of each metric at 45
seeds -- this tool measures the pooled 45-seed VALUE those SEs are
centered on, which none of the six metrics had recorded as its own
citation before Decision P3-47. M14 gets one too (Sec.11 step 9's own
table): its existing `params.se_precertified.se_measured` citation is an
SE-only citation, and `gated_value_precertified` is the first time M14's
own 45-seed VALUE (not just its SE) is recorded.

**Never reimplements the metric math.** Every value below comes from
calling the real `compute_m11`/`compute_m12`/`compute_m13`/`compute_m14`/
`compute_m15`/`compute_m16` entry points the gate itself calls, then
reading `detail["live_reading"]["value"]` -- the freshly-computed reading
each one always reports regardless of whatever `gated_value_precertified`
citation the `bands` object handed in already carries (so re-running this
tool against a bands file that already has a citation still measures a
fresh number, not an echo of the old one -- the whole point of committing
this rather than leaving it as one-shot scratch work).

**Reads the existing `m14_seeds_v3` pickles by default, does not
regenerate them.** `artifacts/validation_gm/m14_seeds_v3/`'s 45 pickles
already exist on disk (Sec.11 step 8's own re-precertification), and
Decision P3-47 step 1's `_m16_core` season-index pin is a verified no-op
at this corpus's `seasons=5` -- so this round does not need a fresh
45-seed run (~60 minutes saved). `--regenerate` (or a corpus dir that does
not yet exist) drives a fresh run under the parameters below, for whatever
future re-precertification needs a genuinely new sample (a `GM_TUNING_
VERSION` bump, a mechanism change, or the still-open step-18 question
this round explicitly does not resolve -- see this round's own handoff).

```
python -m tools.precertify_family3
python -m tools.precertify_family3 --corpus-dir artifacts/validation_gm/some_new_dir --regenerate
```
"""
from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from dba2.core.rng import rng_for  # noqa: E402
from dba2.managers.tuning import DEFAULT_TUNING  # noqa: E402
from dba2.services.gm_service import GM_BEHAVIOR_SCOPE_VERSION  # noqa: E402
from tools.gm_harness.bands import DEFAULT_BANDS_PATH, load_bands  # noqa: E402
from tools.gm_harness.metrics import (  # noqa: E402
    compute_m11,
    compute_m12,
    compute_m13,
    compute_m14,
    compute_m15,
    compute_m16,
)
from tools.gm_harness.season import run_seed  # noqa: E402

DEFAULT_CORPUS_DIR = _REPO_ROOT / "artifacts" / "validation_gm" / "m14_seeds_v3"
RNG_SEED = 1  # validate_gm.py's own --rng-seed default
SEASON_YEAR = 2026  # validate_gm.py's own --season-year default
SEASONS = 5
PLAYERS_PER_TEAM = 15  # validate_gm.py's own --players-per-team default
N_SEEDS = 45

_METRIC_FNS = {
    "M11": compute_m11,
    "M12": compute_m12,
    "M13": compute_m13,
    "M14": compute_m14,
    "M15": compute_m15,
    "M16": compute_m16,
}


def _generate_or_load_corpus(
    corpus_dir: Path, *, n_seeds: int, rng_seed: int, season_year: int, seasons: int,
    players_per_team: int, skip_games: bool, regenerate: bool,
) -> tuple:
    """Checkpointed, atomic-per-seed-write, resumable -- `artifacts/
    scratch_measure_m14_se_v3.py`'s own pattern, generalized to any corpus
    dir/seed count/season config rather than the one hardcoded shape.

    When `regenerate` is `False` and every seed's pickle already exists
    (the `m14_seeds_v3` case), this never calls `run_seed` at all -- it
    only reads what is already on disk. Otherwise it fills in whatever
    seeds are missing (a fresh corpus dir, or a partial one left behind by
    a prior kill) and leaves already-checkpointed seeds untouched."""
    corpus_dir.mkdir(parents=True, exist_ok=True)

    for i in range(n_seeds):
        out_path = corpus_dir / f"seed_{i:03d}.pkl"
        if out_path.exists() and not regenerate:
            continue
        seed = rng_for(rng_seed, "validate_gm", "seed", i).getrandbits(63)
        # `agency_enabled`/`media_enabled` intentionally left at `run_seed`'s
        # own defaults (`True`) here -- explicit call, not a silent omission.
        # This corpus is the LIVE/main corpus these M-metrics measure, not a
        # frozen control baseline diffed against a committed artifact (that's
        # `tools.validate_gm`'s M29 control-run site and `tools.generate_gm_
        # decision_log_digest`'s generator, both of which Decision P4-55 pins
        # `False`). As of Stage B (§11 step 10), `agency_enabled` is a real
        # mechanism switch -- this corpus reflects the live satisfaction/
        # morale/trust/ladder checkpoint, which is the correct reading for
        # a live/main corpus. `media_enabled` is still a no-op (Stage D not
        # built); pinning either `False` here would be the wrong call, not
        # just an unmade one.
        record = run_seed(
            league_id=i + 1,
            rng_seed=seed,
            first_season=season_year,
            seasons=seasons,
            players_per_team=players_per_team,
            skip_games=skip_games,
            tuning=DEFAULT_TUNING,
        )
        tmp_path = out_path.with_suffix(".pkl.tmp")
        with open(tmp_path, "wb") as f:
            pickle.dump(record, f)
        tmp_path.replace(out_path)  # atomic -- a kill mid-write can't leave a corrupt checkpoint
        print(f"seed {i + 1}/{n_seeds} (rng_seed={seed}) done and checkpointed")

    runs = []
    for i in range(n_seeds):
        p = corpus_dir / f"seed_{i:03d}.pkl"
        if not p.exists():
            print(f"MISSING seed {i} -- not all seeds complete, rerun this tool to continue")
            sys.exit(1)
        with open(p, "rb") as f:
            runs.append(pickle.load(f))
    return tuple(runs)


def _live_value(metric_id: str, runs: tuple, bands) -> float | None:
    """Reads `detail["live_reading"]["value"]` (the freshly-computed
    reading `_attach_precision`/`compute_m14` always populate, independent
    of whatever `gated_value_precertified` citation `bands` already
    carries) rather than `.value` itself, which is the GATED value once a
    valid citation is present -- reading `.value` here would make this
    tool echo back its own prior citation on a re-run instead of measuring
    a fresh one."""
    mv = _METRIC_FNS[metric_id](runs, bands)
    live_reading = mv.detail.get("live_reading")
    if live_reading is not None:
        return live_reading.get("value")
    return mv.value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--n-seeds", type=int, default=N_SEEDS)
    parser.add_argument("--rng-seed", type=int, default=RNG_SEED)
    parser.add_argument("--season-year", type=int, default=SEASON_YEAR)
    parser.add_argument("--seasons", type=int, default=SEASONS)
    parser.add_argument("--players-per-team", type=int, default=PLAYERS_PER_TEAM)
    parser.add_argument(
        "--no-skip-games", dest="skip_games", action="store_false",
        help="Run real games rather than skip-games (much slower; the existing m14_seeds_v3 "
             "corpus and every Family-3 citation to date is skip-games=True, confirmed "
             "skip-games-invariant by construction -- see this round's own preflight check).",
    )
    parser.set_defaults(skip_games=True)
    parser.add_argument(
        "--regenerate", action="store_true",
        help="Force a fresh run for every seed even if a checkpoint already exists on disk.",
    )
    parser.add_argument("--bands", type=Path, default=DEFAULT_BANDS_PATH)
    args = parser.parse_args()

    runs = _generate_or_load_corpus(
        args.corpus_dir, n_seeds=args.n_seeds, rng_seed=args.rng_seed,
        season_year=args.season_year, seasons=args.seasons,
        players_per_team=args.players_per_team, skip_games=args.skip_games,
        regenerate=args.regenerate,
    )
    bands = load_bands(args.bands)

    print(f"ALL DONE -- {len(runs)} seeds loaded from {args.corpus_dir}")
    # Decision P4-68 (as re-scoped by P4-73 item 2 -- SEVEN blocks need a
    # `behavior_scope` key transcribed, not six: the six value citations
    # below PLUS M14's own `se_precertified` block): print the scope
    # value alongside every measured value, read from the same constant
    # the predicate itself reads (`dba2.services.gm_service.GM_BEHAVIOR_
    # SCOPE_VERSION`), so the number a human copies into `seeds/gm_
    # validation_bands.json` and the scope it is valid at come from one
    # line of one run's output -- the cheapest guard against the one
    # failure mode the detector itself cannot see (copying the live
    # constant into a citation without having re-run the corpus).
    for metric_id in ("M11", "M12", "M13", "M14", "M15", "M16"):
        value = _live_value(metric_id, runs, bands)
        print(f"  {metric_id}: value={value!r}  behavior_scope={GM_BEHAVIOR_SCOPE_VERSION!r}")
    print(f"  M14 se_precertified: behavior_scope={GM_BEHAVIOR_SCOPE_VERSION!r}")


if __name__ == "__main__":
    main()
