"""Single source of truth for which rating fields are REAL (derived from stats) in a
version-2 real-player seed, and which evidence feeds each one (RPD-8-5, RPD-8-7,
RPD-8-8 in docs/design/real-player-ratings-v2.md).

**Why one module.** Three tools need this list: the offline builder
(`_gm_real_player_build.py`), the real-trajectory code (`_gm_real_trajectory.py`,
which used to keep its own 15-name copy "to avoid cross-quarantine-tool coupling")
and the loaders that refuse legacy seeds. With 30 real fields instead of 15 a copy
per tool would drift; the trajectory's own comment is superseded by this module.

**The evidence-to-field contract.** `METRIC_FEEDS` says which evidence metric feeds
which field; `FIELD_METRIC_WEIGHTS` is the builder's exact weighting. They are checked
against each other at import in both directions, so a weight cannot quietly feed a field
the table does not list and the table cannot list a feed nothing uses. A field's
evidence z is `sum(weight * z_eff[metric])` (weights sum to 1); the builder then
residualises and standardises it. RPD-8-5's table fed several fields from ONE metric
(four from 2P%, two from usage, ...), which made nine of the fifteen new fields another
field plus a constant (critic B3); every new field now has its own mix, which the table
above widened accordingly (a documented deviation from RPD-8-5, see the As-built note).
`tests/unit/tools/test_gm_real_player_build.py` requires the offset-removed difference
of every pair of real fields to have SD >= 3.0 rating points on a real season.

**Why each new field's mix** (the box score carries no tracking data, so each mix is
the best available proxy; FT% and 3P% are the standard shooting-touch proxies):

- `catch_shoot_3` = 3P% .35 + 3PA/36 .5 + FT% .15: spot-up shooters take many threes,
  make them, and show touch at the line; volume leads so a player who never shoots one
  cannot read as a shooter (see `THREE_PT_PRIOR_DEFICIT` in the builder).
- `off_dribble_3` = usage .45 + 3P% .2 + FT% .2 + AST/36 .15: a self-created three needs
  a creator's load and passing feel, then the same touch signal.
- `finishing_rim` = 2P% .55 + OREB/36 .2 + FTA/36 .25: efficiency inside, putbacks and
  dunks, and drawing contact at the rim.
- `contact_finishing` = FTA/36 .3 + FT% .3 + usage .2 + 2P% .2: scoring through contact
  is drawing fouls and converting them, at a real workload.
- `post_scoring` = usage .35 + height .3 + 2P% .2 + OREB/36 .15: a post game is a big
  who is fed the ball; height (a physical, unshrunk, see `PHYSICAL_METRICS`) tilts it to
  bigs even after the position residual.
- `catch_shoot_mid` = FT% .4 + 2P% .4 + 3P% .2: mid-range touch is best read from the
  free-throw line and shooting efficiency inside the arc.
- `off_dribble_mid` = usage .45 + 2P% .3 + 3P% .15 + FT% .1: pull-ups come off a
  creator's shot volume.
- `floater` = FT% .35 + 2P% .25 + AST/36 .25 + usage .15: soft touch of a guard who
  attacks and passes.
- `handle` = usage .5 + FTA/36 .2 + (1 - TOV rate) .15 + AST/36 .15: dribble volume,
  getting to the line off the bounce, keeping the ball, creating.
- `first_step` = usage .4 + steals .2 + 2P% .2 + FTA/36 .2: an explosive first step
  attacks (usage, foul-drawing) and shows in steals.
- `change_of_direction` = AST/36 .25 + (1 - TOV rate) .25 + steals .25 + FTA/36 .25:
  changing pace shows as playmaking with security, active hands and contact drawn (a
  different blend from `first_step`, which is volume-led).
- `pass_under_pressure` = (1 - TOV rate) .4 + AST/36 .3 + usage .2 + AST:TOV .1: passing
  while turnover-free at a high load.
- `short_roll_passing` = AST/36 .4 + height .3 + OREB/36 .3: a big who both passes and
  works the offensive glass.
- `help_iq` = steals+blocks/36 .35 + DREB/36 .3 + fouls avoided .25 + steals .1:
  reading the floor, finishing possessions, staying on the floor.
- `closeout_speed` = fouls avoided .35 + steals .3 + 3PA/36 .2 + steals+blocks/36 .15:
  discipline and active hands, with three-point volume as the perimeter-player proxy.

The 15 existing fields keep their single ETL metric so a 1500+ minute player's evidence
z equals what the ETL used.

**Real field count.** RPD-8-5 says "about 25 of 55"; its own table names 15 new
targets on top of the 15 existing ones, i.e. 30. The table is followed, not the
approximate count (see the As-built note in the design doc).

Every metric is oriented so that HIGHER IS BETTER (turnover rate, personal fouls,
game-score variance and matchup FG% allowed are negated by the builder), the same
convention `_gm_real_player_etl.py` uses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from dba2.sim.derivation import RATING_FIELD_ORDER

REAL_BUILDER_VERSION = 2

# Fields the ETL already derived from box-score totals (RPD-7), 12 of them.
BOX_SCORE_REAL_FIELDS: tuple[str, ...] = (
    "durability",
    "ball_security",
    "draw_foul",
    "free_throw",
    "passing_vision",
    "passing_accuracy",
    "offensive_rebounding",
    "defensive_rebounding",
    "foul_discipline",
    "hands",
    "rim_protection",
    "consistency",
)

# Real only where the advanced band exists (2012-13+). With the advanced endpoint
# unavailable the builder recovers their z from the legacy seeds.
ADVANCED_REAL_FIELDS: tuple[str, ...] = ("on_ball_defense", "screen_setting", "motor")

# RPD-8-5's new real fields: scoring, shooting, finishing, creation.
SCORING_REAL_FIELDS: tuple[str, ...] = (
    "catch_shoot_3",
    "off_dribble_3",
    "finishing_rim",
    "contact_finishing",
    "post_scoring",
    "catch_shoot_mid",
    "off_dribble_mid",
    "floater",
    "handle",
    "first_step",
    "change_of_direction",
    "pass_under_pressure",
    "short_roll_passing",
    "help_iq",
    "closeout_speed",
)

REAL_FIELDS: tuple[str, ...] = (
    *BOX_SCORE_REAL_FIELDS,
    *ADVANCED_REAL_FIELDS,
    *SCORING_REAL_FIELDS,
)
REAL_FIELD_SET: frozenset[str] = frozenset(REAL_FIELDS)

assert len(REAL_FIELDS) == len(REAL_FIELD_SET), "a field is listed twice"
assert REAL_FIELD_SET <= set(RATING_FIELD_ORDER)
# potential/potential_variance are hidden from `compute_overall`; a real rating on
# them would be invisible to the fit and meaningless as a stat.
assert not REAL_FIELD_SET & {"potential", "potential_variance", "work_ethic"}

# Evidence metrics that always exist for a player-season (box-score totals).
BOX_METRICS: tuple[str, ...] = (
    # the 12 existing, named after the field they were born feeding
    *BOX_SCORE_REAL_FIELDS,
    # RPD-8-5's new box-score metrics
    "three_pct",
    "three_rate",
    "two_pct",
    "usage36",
    "stocks36",
)
# Metrics that exist only with the advanced band (raw sums or recovered z).
ADVANCED_METRICS: tuple[str, ...] = ADVANCED_REAL_FIELDS
# The star signal's own performance terms (RPD-8-2); blended like every metric.
IMPACT_METRICS: tuple[str, ...] = ("gmsc36", "ts_scoring")
BLENDED_METRICS: tuple[str, ...] = (*BOX_METRICS, *ADVANCED_METRICS, *IMPACT_METRICS)
# A physical, not a performance: it does not thin out with a short sample, so it is
# read from the current season's own percentile and never blended or shrunk.
PHYSICAL_METRICS: tuple[str, ...] = ("height",)
EVIDENCE_METRICS: tuple[str, ...] = (*BLENDED_METRICS, *PHYSICAL_METRICS)

# metric -> the fields it feeds. Existing metrics feed the field they are named after;
# the rest follow the per-field reasoning in the module docstring. Checked against
# `FIELD_METRIC_WEIGHTS` in BOTH directions at import.
METRIC_FEEDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "durability": ("durability",),
        "ball_security": ("ball_security", "handle", "change_of_direction", "pass_under_pressure"),
        "draw_foul": (
            "draw_foul",
            "finishing_rim",
            "contact_finishing",
            "handle",
            "first_step",
            "change_of_direction",
        ),
        "free_throw": (
            "free_throw",
            "catch_shoot_3",
            "off_dribble_3",
            "contact_finishing",
            "catch_shoot_mid",
            "off_dribble_mid",
            "floater",
        ),
        "passing_vision": (
            "passing_vision",
            "off_dribble_3",
            "floater",
            "handle",
            "change_of_direction",
            "pass_under_pressure",
            "short_roll_passing",
        ),
        "passing_accuracy": ("passing_accuracy", "pass_under_pressure"),
        "offensive_rebounding": (
            "offensive_rebounding",
            "finishing_rim",
            "post_scoring",
            "short_roll_passing",
        ),
        "defensive_rebounding": ("defensive_rebounding", "help_iq"),
        "foul_discipline": ("foul_discipline", "help_iq", "closeout_speed"),
        "hands": ("hands", "first_step", "change_of_direction", "help_iq", "closeout_speed"),
        "rim_protection": ("rim_protection",),
        "consistency": ("consistency",),
        "on_ball_defense": ("on_ball_defense",),
        "screen_setting": ("screen_setting",),
        "motor": ("motor",),
        "three_pct": ("catch_shoot_3", "off_dribble_3", "catch_shoot_mid", "off_dribble_mid"),
        "three_rate": ("catch_shoot_3", "closeout_speed"),
        "two_pct": (
            "finishing_rim",
            "contact_finishing",
            "post_scoring",
            "catch_shoot_mid",
            "off_dribble_mid",
            "floater",
            "first_step",
        ),
        "usage36": (
            "off_dribble_3",
            "contact_finishing",
            "post_scoring",
            "off_dribble_mid",
            "floater",
            "handle",
            "first_step",
            "pass_under_pressure",
        ),
        "stocks36": ("help_iq", "closeout_speed"),
        "height": ("post_scoring", "short_roll_passing"),
        "gmsc36": (),
        "ts_scoring": (),
    }
)

# field -> {metric: weight}. The 15 existing fields stay single-metric so a 1500+ minute
# player's evidence z equals what the ETL used; each of the 15 new fields has ITS OWN
# mix (reasoning in the module docstring). Values were tuned only on structural
# properties (pairwise distinctness, drag), never on named players.
FIELD_METRIC_WEIGHTS: Mapping[str, Mapping[str, float]] = MappingProxyType(
    {
        **{
            name: MappingProxyType({name: 1.0})
            for name in (*BOX_SCORE_REAL_FIELDS, *ADVANCED_REAL_FIELDS)
        },
        "catch_shoot_3": MappingProxyType(
            {"three_pct": 0.35, "three_rate": 0.5, "free_throw": 0.15}
        ),
        "off_dribble_3": MappingProxyType(
            {"usage36": 0.45, "three_pct": 0.2, "free_throw": 0.2, "passing_vision": 0.15}
        ),
        "finishing_rim": MappingProxyType(
            {"two_pct": 0.55, "offensive_rebounding": 0.2, "draw_foul": 0.25}
        ),
        "contact_finishing": MappingProxyType(
            {"draw_foul": 0.3, "free_throw": 0.3, "usage36": 0.2, "two_pct": 0.2}
        ),
        "post_scoring": MappingProxyType(
            {"usage36": 0.35, "height": 0.3, "two_pct": 0.2, "offensive_rebounding": 0.15}
        ),
        "catch_shoot_mid": MappingProxyType(
            {"free_throw": 0.4, "two_pct": 0.4, "three_pct": 0.2}
        ),
        "off_dribble_mid": MappingProxyType(
            {"usage36": 0.45, "two_pct": 0.3, "three_pct": 0.15, "free_throw": 0.1}
        ),
        "floater": MappingProxyType(
            {"free_throw": 0.35, "two_pct": 0.25, "passing_vision": 0.25, "usage36": 0.15}
        ),
        "handle": MappingProxyType(
            {"usage36": 0.5, "draw_foul": 0.2, "ball_security": 0.15, "passing_vision": 0.15}
        ),
        "first_step": MappingProxyType(
            {"usage36": 0.4, "hands": 0.2, "two_pct": 0.2, "draw_foul": 0.2}
        ),
        "change_of_direction": MappingProxyType(
            {"passing_vision": 0.25, "ball_security": 0.25, "hands": 0.25, "draw_foul": 0.25}
        ),
        "pass_under_pressure": MappingProxyType(
            {"ball_security": 0.4, "passing_vision": 0.3, "usage36": 0.2, "passing_accuracy": 0.1}
        ),
        "short_roll_passing": MappingProxyType(
            {"passing_vision": 0.4, "height": 0.3, "offensive_rebounding": 0.3}
        ),
        "help_iq": MappingProxyType(
            {
                "stocks36": 0.35,
                "defensive_rebounding": 0.3,
                "foul_discipline": 0.25,
                "hands": 0.1,
            }
        ),
        "closeout_speed": MappingProxyType(
            {"foul_discipline": 0.35, "hands": 0.3, "three_rate": 0.2, "stocks36": 0.15}
        ),
    }
)


def _check_contract() -> None:
    assert set(FIELD_METRIC_WEIGHTS) == REAL_FIELD_SET, "every real field needs weights"
    for field_name, weights in FIELD_METRIC_WEIGHTS.items():
        assert abs(sum(weights.values()) - 1.0) < 1e-9, f"{field_name}: weights must sum to 1"
        for metric in weights:
            assert metric in EVIDENCE_METRICS, f"{field_name}: unknown metric {metric}"
            assert field_name in METRIC_FEEDS[metric], (
                f"{field_name} is fed by {metric}, which METRIC_FEEDS does not list"
            )
    assert set(METRIC_FEEDS) == set(EVIDENCE_METRICS), "every metric needs a feeds row"
    for metric, fed in METRIC_FEEDS.items():
        for field_name in fed:
            assert metric in FIELD_METRIC_WEIGHTS[field_name], (
                f"METRIC_FEEDS lists {metric} -> {field_name} but the weights do not use it"
            )


_check_contract()


class LegacyRatingsError(ValueError):
    """A seed season is not builder version 2 and the caller did not opt into the
    old behaviour."""


def _row_version(row: Mapping) -> int | None:
    meta = row.get("meta") or {}
    version = meta.get("builder_version")
    return version if isinstance(version, int) and not isinstance(version, bool) else None


def require_builder_version(rows: Sequence[Mapping], season: int, allow_legacy: bool) -> None:
    """Refuse a season whose rows are not all `REAL_BUILDER_VERSION` (RPD-8-7).

    Version 1 fields are `70 + 12*z*w` and version 2 fields are `T + offset + 8u`;
    mixing them silently corrupts anything that compares seasons (the real
    trajectory) and shows stars at legacy scale next to rebuilt ones. Refuses with an
    error naming the season and the commands that fix it, unless `allow_legacy`.
    A season with no rows has nothing to refuse."""
    if allow_legacy:
        return
    stale = [row for row in rows if _row_version(row) != REAL_BUILDER_VERSION]
    if stale:
        raise LegacyRatingsError(
            f"season {season}: {len(stale)} of {len(rows)} real-player rows are not "
            f"builder version {REAL_BUILDER_VERSION} (legacy ratings compress stars "
            "toward 70 and cannot be mixed with rebuilt seasons). Rebuild it with "
            f"`python -m tools._gm_real_player_fetch_raw {season} {season}` then "
            "`python -m tools._gm_real_player_build --all --out-dir seeds/real_players "
            "--allow-seeds-dir` (the builder only writes seeds/real_players for a full "
            "--all rebuild), or pass --allow-legacy-ratings to keep the old behaviour."
        )
