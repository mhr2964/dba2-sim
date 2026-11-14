"""`/standings [conference|division]`'s panel builder (docs/design/phase-5-
discord-presentation.md §11 Stage B step 6, §7.1's `/standings` row;
docs/design/standings-v2-conference-split.md SV-1 through SV-5).

**Decision P5-6: standings for display come from `media_service.compute_
current_pulse_snapshot`, never recomputed here.** This module cannot even
import that function's own return type (`dba2.media.pulse.PulseSnapshot`/
`TeamPulseState`) — Decision P4-31's one-way valve restricts `dba2.media`
to `dba2/services/media_service.py` alone, and Decision P5-3 restricts
`presentation/` to `dba2.domain`/`dba2.core`/`dba2.sim.events`.
`StandingsRow` below is this module's own plain-data input shape;
`view_service.get_standings_panel` is the one place that reads a real
`PulseSnapshot` and copies its `TeamPulseState` fields into `StandingsRow`s
one-for-one, with no arithmetic of its own — Decision P5-5 is satisfied by
construction (nothing here computes a win, a loss, a rank, or a
percentage) rather than by promise.

**standings-v2 SV-5: no Strk column.** Fitting Pct and GB into the
1024-char `PanelField.value` budget left no room for it; a team's streak
is still visible on `/team`'s own panel."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from dba2.presentation.panel import Panel, PanelColor, PanelField

# Keeps a 30-row-per-conference table inside `PanelField.value`'s 1024-char
# ceiling regardless of how long a real `teams.city`/`teams.name` TEXT value
# gets (§9.2's adversarial "60-character city" case) — truncated with an
# ellipsis, never silently dropped from the row. SV-5's own "Field-width
# bound, checked, not assumed" measured the real 30-team seed's longest
# name at 22 characters, comfortably under this — 24 did not need to shrink.
_MAX_NAME_CHARS = 24

# SV-2: fits `1.000` (5 chars); SV-3: fits `-`, `0.0`, up to `12.5` (5 chars).
_PCT_WIDTH = 5
_GB_WIDTH = 5


@dataclass(frozen=True)
class StandingsRow:
    """One team's already-computed standings line. Every field is copied
    verbatim from `Team` (`code`/`city`/`name`/`conference`/`division`) or
    from a real `TeamPulseState` (`wins`/`losses`/`standings_position`/
    `win_pct`) or computed by `view_service` as a filter+enumerate over
    already-known counts (`conference_rank`, `games_behind`) — this module
    performs no arithmetic on any of them, only formatting and filtering."""

    team_id: int
    code: str
    city: str
    name: str
    conference: str
    division: str
    wins: int
    losses: int
    standings_position: int
    conference_rank: int
    win_pct: float
    games_behind: float | None


def _display_name(row: StandingsRow) -> str:
    full = f"{row.city} {row.name}"
    if len(full) <= _MAX_NAME_CHARS:
        return full
    return full[: _MAX_NAME_CHARS - 1] + "…"


def _pct_text(win_pct: float, games_played: int) -> str:
    """SV-2, verbatim: a team with zero games played renders `-.--` (never
    `.000`, which misleadingly reads as "winless"); `1.000` renders in full
    (5 characters, the undefeated case); every other percentage strips its
    leading zero (`.750`), matching every real broadcast."""
    if games_played == 0:
        return "-.--"
    if win_pct >= 1.0:
        return "1.000"
    return f"{win_pct:.3f}"[1:]


def _gb_text(games_behind: float | None) -> str:
    """SV-3: `None` (the conference leader, or a team with zero games
    played) renders `-`; every other team renders its already-floored,
    one-decimal games-behind number."""
    if games_behind is None:
        return "-"
    return f"{games_behind:.1f}"


def _table_lines(rows: Sequence[StandingsRow]) -> list[str]:
    lines = [f"{'Pos':<3}  {'Pct':<{_PCT_WIDTH}}  {'GB':<{_GB_WIDTH}}  W -  L   Team"]
    for row in rows:
        games_played = row.wins + row.losses
        lines.append(
            f"{row.conference_rank:>3}  {_pct_text(row.win_pct, games_played):>{_PCT_WIDTH}}  "
            f"{_gb_text(row.games_behind):>{_GB_WIDTH}}  "
            f"{row.wins:>2} - {row.losses:<2}  {_display_name(row)}"
        )
    return lines


def _table_field(name: str, rows: Sequence[StandingsRow]) -> PanelField:
    value = "```\n" + "\n".join(_table_lines(rows)) + "\n```"
    return PanelField(name=name, value=value, inline=True)


def build_standings_panel(
    *,
    league_name: str,
    season: int,
    rows: Sequence[StandingsRow],
    group: str | None = None,
) -> Panel:
    """`group`, when given, matches EITHER `conference` or `division` on
    each row — the slash command's own `[conference|division]` shape does
    not distinguish the two at the argument level — and FILTERS the
    already-ranked `rows`; it never re-sorts or re-ranks them. `view_
    service` supplies `rows` already ordered, per conference, by
    `conference_rank` ascending (SV-4's own filter+enumerate, never a
    second sort) — this module has no ranking logic to diverge from the
    service's own.

    **standings-v2 SV-4/Layout: `group=None` renders TWO side-by-side
    conference tables, not one league-wide list.** Each of `rows`' own
    distinct `conference` values (in the order they first appear — `view_
    service` already assembles `rows` one conference block at a time, in a
    stable order) becomes its own `PanelField`, named after the conference,
    holding that conference's own already-ranked table. `group` given
    (a conference or a division name) narrows to ONE table instead — a
    division's own rows still carry their CONFERENCE's rank/GB (SV-4),
    unrenumbered. `description` is always left empty; all content lives in
    `fields` (SV-4's own Layout section).

    Raises `ValueError` if `group` is given and matches no row (a
    commissioner typo, e.g. `/standings Atlantik`) — the same "fail loudly
    in a unit test, not silently in a production guild" discipline
    `Panel.__post_init__` itself uses. `group=None` with zero `rows` (an
    empty league) renders one empty, header-only table rather than raising
    or sending a field-less panel."""
    selected = [r for r in rows if group is None or r.conference == group or r.division == group]
    if group is not None and not selected:
        raise ValueError(f"No teams match group={group!r}")

    title = f"{league_name} Standings — Season {season}"
    if group is not None:
        title += f" ({group})"

    if group is not None:
        fields: tuple[PanelField, ...] = (_table_field(group, selected),)
    else:
        conferences = list(dict.fromkeys(r.conference for r in selected))
        if not conferences:
            fields = (_table_field(league_name, ()),)
        else:
            fields = tuple(
                _table_field(conference, [r for r in selected if r.conference == conference])
                for conference in conferences
            )

    return Panel(title=title, description="", fields=fields, color=PanelColor.NEUTRAL)
