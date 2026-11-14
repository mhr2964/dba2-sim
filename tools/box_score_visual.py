"""Standalone dev tool: sim one game entirely in memory and render its
GameResult as a static HTML box score. Not a formal deliverable -- exists
so headless engine runs have something visible to glance at, per the
user's "rendered HTML box score" request (see HANDOFF.md's Recent
context). Takes no dependency on Postgres; uses tests/fixtures/sim.py's
builders exactly like the unit tests do.

Usage: python tools/box_score_visual.py [--seed N] [--out PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dba2.sim.events import GameResult, PlayerBoxLine, TeamBoxLine  # noqa: E402
from dba2.sim.game import simulate_game  # noqa: E402
from dba2.sim.tuning import DEFAULT_TUNING  # noqa: E402
from tests.fixtures.sim import make_game_context  # noqa: E402

_PLAYER_COLUMNS = (
    ("MIN", lambda p: f"{p.minutes_played:.1f}"),
    ("PTS", lambda p: str(p.points)),
    ("FG", lambda p: f"{p.fgm_rim + p.fgm_mid}-{p.fga_rim + p.fga_mid}"),
    ("3P", lambda p: f"{p.fgm_three}-{p.fga_three}"),
    ("FT", lambda p: f"{p.ftm}-{p.fta}"),
    ("OREB", lambda p: str(p.oreb)),
    ("DREB", lambda p: str(p.dreb)),
    ("REB", lambda p: str(p.oreb + p.dreb)),
    ("AST", lambda p: str(p.ast)),
    ("STL", lambda p: str(p.stl)),
    ("BLK", lambda p: str(p.blk)),
    ("TOV", lambda p: str(p.tov)),
    ("PF", lambda p: str(p.pf)),
    ("+/-", lambda p: f"{p.plus_minus:+d}"),
)


def _player_name_map(context) -> dict[int, tuple[str, str]]:
    names: dict[int, tuple[str, str]] = {}
    for team in (context.home, context.away):
        for sim_player in team.players:
            p = sim_player.player
            names[p.id] = (f"{p.first_name} {p.last_name}", p.primary_position.value)
    return names


def _player_rows(lines: tuple[PlayerBoxLine, ...], names: dict[int, tuple[str, str]]) -> str:
    rows = []
    for line in sorted(lines, key=lambda p: p.minutes_played, reverse=True):
        name, pos = names.get(line.player_id, (f"Player {line.player_id}", "?"))
        cells = "".join(f"<td>{fn(line)}</td>" for _, fn in _PLAYER_COLUMNS)
        rows.append(f"<tr><td class='name'>{name}</td><td>{pos}</td>{cells}</tr>")
    return "\n".join(rows)


def _team_totals_row(line: TeamBoxLine) -> str:
    fg_m, fg_a = line.fgm_rim + line.fgm_mid, line.fga_rim + line.fga_mid
    return (
        "<tr class='totals'><td class='name'>Team</td><td></td>"
        f"<td>-</td><td>{line.points}</td>"
        f"<td>{fg_m}-{fg_a}</td><td>{line.fgm_three}-{line.fga_three}</td>"
        f"<td>{line.ftm}-{line.fta}</td><td>{line.oreb}</td><td>{line.dreb}</td>"
        f"<td>{line.oreb + line.dreb}</td><td>{line.ast}</td><td>{line.stl}</td>"
        f"<td>{line.blk}</td><td>{line.tov}</td><td>{line.pf}</td><td>-</td></tr>"
    )


def _team_table(team_label: str, team_id: int, result: GameResult, names: dict[int, tuple[str, str]]) -> str:
    player_lines = tuple(p for p in result.player_box_lines if p.team_id == team_id)
    team_line = next(t for t in result.team_box_lines if t.team_id == team_id)
    header = "".join(f"<th>{label}</th>" for label, _ in _PLAYER_COLUMNS)
    return f"""
    <h2>{team_label}</h2>
    <table>
      <thead><tr><th class='name'>Player</th><th>Pos</th>{header}</tr></thead>
      <tbody>
        {_player_rows(player_lines, names)}
        {_team_totals_row(team_line)}
      </tbody>
    </table>
    """


def render_html(context, result: GameResult, seed: int) -> str:
    names = _player_name_map(context)
    home_label = f"{context.home.team.city} {context.home.team.name}"
    away_label = f"{context.away.team.city} {context.away.team.name}"
    period_rows = "\n".join(
        f"<tr><td>{period}</td><td>{home_pts}</td><td>{away_pts}</td></tr>"
        for period, home_pts, away_pts in result.period_scores
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Box Score - seed {seed}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; background: #fff; }}
  h1 {{ margin-bottom: 0.25rem; }}
  .final {{ color: #555; margin-top: 0; }}
  table {{ border-collapse: collapse; margin-bottom: 2rem; font-size: 0.85rem; }}
  th, td {{ border: 1px solid #ddd; padding: 0.3rem 0.5rem; text-align: right; }}
  th {{ background: #f2f2f2; }}
  td.name, th.name {{ text-align: left; min-width: 10rem; }}
  tr.totals {{ font-weight: bold; background: #fafafa; }}
  .period-table {{ font-size: 0.85rem; }}
</style></head>
<body>
  <h1>{home_label} {result.home_score} - {result.away_score} {away_label}</h1>
  <p class="final">seed {seed} | {result.overtime_periods} OT period(s) | {len(result.beat_log)} beats logged</p>
  <table class="period-table">
    <thead><tr><th>Period</th><th>{home_label}</th><th>{away_label}</th></tr></thead>
    <tbody>{period_rows}</tbody>
  </table>
  {_team_table(home_label, context.home.id, result, names)}
  {_team_table(away_label, context.away.id, result, names)}
</body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=777)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    out_path = args.out or (_REPO_ROOT / "tools" / "output" / f"box_score_seed{args.seed}.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    context = make_game_context(1, tuning=DEFAULT_TUNING)
    result = simulate_game(context, rng_seed=args.seed)
    out_path.write_text(render_html(context, result, args.seed), encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
