"""CLI: the offline evaluation of the AI writer's number gate
(docs/design/media-v2-step8-gates-v2.md G2-9).

**Why this exists.** The number gate's false-accept rate cannot be read off
live runs: every story a live run shows us already passed the gate, so the
misattributed-but-true numbers it lets through are invisible there. This
tool runs the gate over a committed, labelled fixture
(`tests/fixtures/media_gates/cases.json`) and prints how many cases of each
label each policy accepts, so a gate change has a before/after number that
does not depend on a model call. It NEVER makes a model call.

    python -m tools.media_gate_eval --cases tests/fixtures/media_gates/cases.json --policy pooled
    python -m tools.media_gate_eval --cases tests/fixtures/media_gates/cases.json --policy bound
    python -m tools.media_gate_eval --cases ... --policy bound --no-derived

The table always shows all three columns (pooled = gate 1; bound without the
derived list; bound = gate 1b + gate 4 as the rewrite pass runs them). The
per-case lines are for the selected policy: every case whose outcome
disagrees with its label, with the attributed offending tokens. Labels:
`clean` and `derived_true` are true texts (should be accepted);
`rhetorical`, `false_claim` and `negative` should be rejected.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dba2.domain.media_bindings import TeamAliases
from dba2.domain.media_context import LeagueBrief
from dba2.domain.media_facts import StoryPackage
from dba2.media.write import verify

DEFAULT_CASES_PATH = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "media_gates" / "cases.json"
)

TRUE_LABELS = frozenset({"clean", "derived_true"})
"""Labels whose text is true and should be accepted; every other label
should be rejected."""

LABEL_ORDER = ("clean", "derived_true", "rhetorical", "false_claim", "negative")

COLUMNS = (
    ("pooled", verify.NumberPolicy.POOLED, True),
    ("bound-no-derived", verify.NumberPolicy.BOUND, False),
    ("bound", verify.NumberPolicy.BOUND, True),
)


@dataclass(frozen=True)
class EvalCase:
    id: str
    source: str
    label: str
    note: str
    headline: str
    body: str
    package: StoryPackage
    brief: LeagueBrief | None
    team_aliases: Mapping[int, TeamAliases]


def load_cases(path: Path = DEFAULT_CASES_PATH) -> list[EvalCase]:
    """Rehydrates every case. A synthetic case names its recorded `base` and
    reuses that story's package and brief (it is a one-clause mutation of it).
    A recorded case with a `league` uses that league's own alias table: a
    live league reuses the hand-built cases' team names under other ids, and
    one shared table would make every name two teams."""
    data: Mapping[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    def _aliases(table: Mapping[str, Any]) -> dict[int, TeamAliases]:
        return {
            int(tid): TeamAliases(full=row["full"], city=row["city"], nickname=row["nickname"])
            for tid, row in table.items()
        }

    default_aliases = _aliases(data["team_aliases"])
    league_aliases = {
        league: _aliases(table) for league, table in data.get("league_team_aliases", {}).items()
    }
    recorded: dict[str, Mapping[str, Any]] = {}
    cases: list[EvalCase] = []
    for row in data["cases"]:
        source_row = recorded[row["base"]] if "base" in row else row
        if "base" not in row:
            recorded[row["id"]] = row
        brief_json = source_row["brief"]
        cases.append(
            EvalCase(
                id=row["id"], source=row["source"], label=row["label"], note=row["note"],
                headline=row["headline"], body=row["body"],
                package=StoryPackage.from_prompt_json(source_row["package"]),
                brief=LeagueBrief.from_prompt_json(brief_json) if brief_json else None,
                team_aliases=(league_aliases[source_row["league"]] if "league" in source_row
                              else default_aliases),
            )
        )
    return cases


def evaluate(
    case: EvalCase,
    policy: verify.NumberPolicy = verify.NumberPolicy.POOLED,
    *,
    derived: bool = True,
) -> verify.GateResult:
    """The number gates exactly as the rewrite pass runs them: gate 1 under
    `POOLED`; gate 1b then gate 4 under `BOUND`. Gates 2 and 3 are not part
    of this evaluation."""
    if policy is verify.NumberPolicy.POOLED:
        return verify.verify_no_invented_numbers(
            case.package, case.headline, case.body, brief=case.brief
        )
    for gate in (verify.verify_numbers_bound, verify.verify_claims):
        result = gate(
            case.package, case.headline, case.body, brief=case.brief,
            team_aliases=case.team_aliases, derived=derived,
        )
        if not result.passed:
            return result
    return result


def expected_accept(case: EvalCase) -> bool:
    return case.label in TRUE_LABELS


def summarize(cases: Sequence[EvalCase]) -> list[str]:
    header = f"{'label':<13}{'n':>3} |" + "".join(f" {name:>17}" for name, _, _ in COLUMNS)
    lines = [header]
    for label in LABEL_ORDER:
        rows = [c for c in cases if c.label == label]
        if not rows:
            continue
        counts = [
            sum(evaluate(c, policy, derived=derived).passed for c in rows)
            for _, policy, derived in COLUMNS
        ]
        lines.append(f"{label:<13}{len(rows):>3} |" + "".join(f" {n:>17}" for n in counts))
    return lines


def disagreements(
    cases: Sequence[EvalCase], policy: verify.NumberPolicy, *, derived: bool
) -> list[str]:
    lines = []
    for case in cases:
        result = evaluate(case, policy, derived=derived)
        if result.passed != expected_accept(case):
            verdict = (
                "ACCEPTED" if result.passed
                else f"REJECTED {result.gate} {list(result.offending)}"
            )
            lines.append(f"{case.id:>5} {case.label:<12} {verdict}  -- {case.note}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--policy", choices=[p.value for p in verify.NumberPolicy],
                        default=verify.NumberPolicy.BOUND.value)
    parser.add_argument("--no-derived", action="store_true",
                        help="bound policy without the derived list (the ordering check)")
    args = parser.parse_args(argv)
    cases = load_cases(args.cases)
    policy = verify.NumberPolicy(args.policy)
    print("\n".join(summarize(cases)))
    print()
    suffix = " --no-derived" if args.no_derived and policy is verify.NumberPolicy.BOUND else ""
    print(f"disagreements with the label under --policy {policy.value}{suffix}:")
    lines = disagreements(cases, policy, derived=not args.no_derived)
    print("\n".join(lines) or "  none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
