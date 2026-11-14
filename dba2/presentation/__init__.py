"""`dba2/presentation/` -- Pillar 8/6's pure render package (Phase 5 Stage
A step 4).

docs/design/phase-5-discord-presentation.md §3/§9, Decisions P5-3/P5-4.

Zero I/O, the same denylist `dba2/managers/`/`dba2/agency/`/`dba2/media/`
already carry (`tests/unit/test_layering.py`): no `dba2.data`, no
`datetime.now()`, no bare `random.Random(`, no `open(`, no `os.environ`.
May import `dba2.domain`, `dba2.core` and `dba2.sim`'s pure event types
(`BeatRecord`/`GameResult`/`PlayerBoxLine`) -- never `dba2.data`.

**`discord` may be imported nowhere under `dba2/` except `dba2/bot/`**
(Decision P5-1) -- `presentation/` returns the library-agnostic `Panel`
(this package's `panel.py`); the one small adapter that turns a `Panel`
into a `discord.Embed` is `dba2/bot/embeds.py`, and it is the only file
in the whole codebase allowed to know what a `discord.Embed` is.

`presentation/` never derives a league fact a service already derives
(Decision P5-5) -- standings come from `media_service.
compute_current_pulse_snapshot`, never recomputed from `games` in a
panel builder; cap numbers come from `cap_service.get_cap_summary`,
never re-added; a player's overall comes from `ratings_service`, never
recomputed from ratings. A rendering layer is the single most tempting
place in this codebase to make that mistake ("it's just for display").

Real panel-builder content (`standings.py`, `team.py`, `boxscore.py`,
`wire.py`, `broadcast.py`) lands at Stage B/D; this package's first
module, `panel.py`, ships alone at Stage A step 4 per the design doc's
own outline -- ground truth and scaffolding, nothing user-visible yet.
"""
from __future__ import annotations
