"""`role_service`'s pure half (`docs/design/auto-gm-role-construction.md`
RC-22.3, RC-24's own test list): `construct_team_roles` and `arch_z`. No
I/O -- the impure PRESEASON pass (`construct_ai_roles_for_league`) is
integration-tested in `tests/integration/test_role_service.py`.
"""
from __future__ import annotations

import dataclasses

import pytest

from dba2.domain.player import Position
from dba2.domain.team import GmPhilosophy
from dba2.managers.philosophy import PROFILES
from dba2.services.role_service import (
    ARCHETYPES,
    FLOOR_RANK,
    RHO_BAR,
    S_A,
    Z_FLOOR,
    _minutes_curve_raw,
    construct_team_roles,
    rotation_floor_order,
)
from tests.fixtures.sim import make_player, make_ratings

# `test_touch_share_is_read_only_as_minutes_by_the_sim` scans `dba2/sim/` with
# `rglob`, so it must not run beside a test that plants scratch modules there.
pytestmark = pytest.mark.xdist_group("source-tree")

_POSITIONS = (Position.PG, Position.SG, Position.SF, Position.PF, Position.C)


def _make_team_of(n: int, *, overall_start: int = 90, overall_step: int = -3):
    """`n` players, strictly descending `overall_cached`, each with the
    fixture file's own pairwise-distinct ratings (seeded by player_id) so
    no two players' 40-pool-field profiles collide."""
    players = []
    ratings_by_player = {}
    for i in range(n):
        pid = 1000 + i
        overall = overall_start + overall_step * i
        players.append(
            make_player(
                pid,
                primary_position=_POSITIONS[i % len(_POSITIONS)],
                overall_cached=overall,
            )
        )
        ratings_by_player[pid] = make_ratings(pid)
    return players, ratings_by_player


def test_the_pass_is_the_identity_at_the_default_philosophy():
    """The most important test in the file (RC-24's own words): on a
    `tendency_respecter` team (kappa=7.5, the identity anchor -- RC-22.5
    property 1), every written `minutes_share` equals
    `_MINUTES_CURVE[rank]/240` exactly. This is the property RC-23.4's
    control run depends on."""
    n = 14
    players, ratings_by_player = _make_team_of(n)
    assignments = construct_team_roles(
        players, ratings_by_player, philosophy=GmPhilosophy.TENDENCY_RESPECTER, season=2026
    )
    by_rank = {a.rank: a for a in assignments}
    assert set(by_rank) == set(range(1, n + 1))
    for rank in range(1, n + 1):
        expected = _minutes_curve_raw(rank) / 240.0
        assert float(by_rank[rank].minutes_share) == pytest.approx(expected, abs=5e-5)


def test_written_shares_are_strictly_decreasing_in_rank_through_rank_11():
    """RC-22.3 step 4's own disclosed cost of steepening (RC-25 risk 6):
    the `a_min` quantum clamp is the only place ties can appear, and at
    `star_maxer`'s lambda=1.12 it legitimately reaches ranks 12-14 (the
    property-4 table's own worked example) -- so the invariant this test
    checks is "no ties through rank 11," not "no ties anywhere." A clamp
    reaching rank 11 or above would be the real regression this test
    exists to catch."""
    n = 14
    players, ratings_by_player = _make_team_of(n)
    for philosophy in GmPhilosophy:
        assignments = construct_team_roles(
            players, ratings_by_player, philosophy=philosophy, season=2026
        )
        by_rank = sorted(assignments, key=lambda a: a.rank)
        shares = [float(a.minutes_share) for a in by_rank]
        assert shares == sorted(shares, reverse=True), (
            f"{philosophy}: shares are not strictly decreasing by rank: {shares}"
        )
        through_11 = shares[:11]
        assert len(set(through_11)) == len(through_11), (
            f"{philosophy}: tied shares reach rank <= 11: {shares}"
        )


def test_no_written_share_is_below_the_quantum():
    """RC-21.3's last row: the pass never writes a target the engine
    cannot realize -- every value >= 1.0/240."""
    n = 14
    players, ratings_by_player = _make_team_of(n)
    for philosophy in GmPhilosophy:
        assignments = construct_team_roles(
            players, ratings_by_player, philosophy=philosophy, season=2026
        )
        for a in assignments:
            assert float(a.minutes_share) >= (1.0 / 240.0) - 1e-9, (philosophy, a)


def test_the_rotation_pool_stays_at_eight_across_the_whole_kappa_range():
    """RC-22.3 step 3 property 4: rank 8 stays well inside the 8-man
    rotation and rank 9 well outside it, across every real philosophy's
    kappa -- checked against `validate_sim.py`'s own 15.0-mpg rotation
    cutoff."""
    for n in (12, 13, 14, 15):
        players, ratings_by_player = _make_team_of(n)
        for philosophy in GmPhilosophy:
            assignments = construct_team_roles(
                players, ratings_by_player, philosophy=philosophy, season=2026
            )
            by_rank = {a.rank: a for a in assignments}
            rank8_mpg = float(by_rank[8].minutes_share) * 240.0
            rank9_mpg = float(by_rank[9].minutes_share) * 240.0
            assert rank8_mpg >= 15.0, (n, philosophy, rank8_mpg)
            assert rank9_mpg < 15.0, (n, philosophy, rank9_mpg)


def test_rationale_is_deterministic_across_two_runs():
    """Byte-equal strings, no timestamps, no rng -- the rows are upserted
    every preseason, and a rationale that varies run-to-run makes a diff
    of two seasons' role tables unreadable."""
    n = 14
    players, ratings_by_player = _make_team_of(n)
    first = construct_team_roles(
        players, ratings_by_player, philosophy=GmPhilosophy.EGALITARIAN, season=2026
    )
    second = construct_team_roles(
        players, ratings_by_player, philosophy=GmPhilosophy.EGALITARIAN, season=2026
    )
    first_by_id = {a.player_id: a.rationale for a in first}
    second_by_id = {a.player_id: a.rationale for a in second}
    assert first_by_id == second_by_id


# ---------------------------------------------------------------------------
# RC-37's rotation floor. The pure ordering rule first, on synthetic id
# lists (every case D15 names, including the one that separates the
# measured rule from both of revision 7's misreadings), then the whole pass
# on real production rosters -- the population every RC-37 number was
# measured on, loaded through the spike's own loader so the two cannot
# disagree about what a "production roster" is.
# ---------------------------------------------------------------------------

_TALENT_ORDER = tuple(range(1, 15))  # id == talent rank, 14-man roster


def _final_ranks(firers: set[int]) -> dict[int, int]:
    order = rotation_floor_order(_TALENT_ORDER, frozenset(firers))
    assert sorted(order) == list(_TALENT_ORDER), "the floor must be a permutation"
    return {pid: rank for rank, pid in enumerate(order, start=1)}


def test_a_firing_talent_rank_8_player_keeps_8_and_a_buried_firer_lands_at_9():
    """The case the critic found the design text did not pin down (RC-37
    revision 7.1). Under the measured rule the talent-rank-8 firer is the
    first firer in the reordered block, so he keeps 8 and the buried firer
    (talent rank 12) takes 9, pushing talent ranks 9-11 down one each.
    Revision 7's D15 read literally would put the buried firer at 8 and the
    rank-8 firer at 9, which this test rejects. (Its RC-37.7 step 1 read
    literally -- a floor at 9 -- happens to agree on this roster; the next
    test is the one that rejects it.)"""
    ranks = _final_ranks({8, 12})
    assert ranks[8] == 8
    assert ranks[12] == 9
    assert [ranks[pid] for pid in (9, 10, 11)] == [10, 11, 12]
    assert [ranks[pid] for pid in (13, 14)] == [13, 14]


def test_a_buried_firer_takes_rank_8_from_a_non_firing_talent_rank_8_player():
    """The floor is at 8, not 9: with no firer at talent rank 8 the best
    buried firer takes rank 8 itself. A floor at 9 (revision 7's RC-37.7
    step 1, literally) would have left him at 9 -- 72-79% on criterion 1
    instead of 90.7%."""
    ranks = _final_ranks({11})
    assert ranks[11] == 8
    assert ranks[8] == 9


def test_two_buried_firers_stack_at_8_and_9_in_talent_order():
    """D18: several lifted firers keep talent order among themselves (not
    arch_z order), and the non-firing talent-rank-8 player drops one slot per
    firer below him -- here to 10."""
    ranks = _final_ranks({13, 10})
    assert ranks[10] == 8
    assert ranks[13] == 9
    assert ranks[8] == 10
    assert ranks[9] == 11


def test_a_firing_talent_rank_3_player_does_not_move():
    ranks = _final_ranks({3})
    assert ranks == {pid: pid for pid in _TALENT_ORDER}


def test_ranks_1_to_7_equal_the_talent_order_for_every_possible_firing_set():
    """Exhaustive over all 2^14 firing subsets of a 14-man roster: the first
    seven slots never change, the result is always a permutation, and both
    the firers and the non-firers keep talent order among themselves."""
    for mask in range(1 << len(_TALENT_ORDER)):
        firers = {pid for pid in _TALENT_ORDER if mask & (1 << (pid - 1))}
        order = rotation_floor_order(_TALENT_ORDER, frozenset(firers))
        assert order[:7] == list(_TALENT_ORDER[:7])
        tail = order[7:]
        firing = [pid for pid in tail if pid in firers]
        assert tail == firing + [pid for pid in tail if pid not in firers]
        assert firing == sorted(firing)
        assert tail[len(firing) :] == sorted(tail[len(firing) :])


def _production_team(roster):
    """One spike `TeamRoster` as `construct_team_roles` input. Player ids are
    assigned in sorted `external_ref` order so that `construct_team_roles`'s
    `(-talent, id)` tie-break orders talent ties exactly as the spike's
    `(-talent, external_ref)` does -- without that, the two disagree only on
    tied talent, which is a test artefact, not a mechanism difference.
    `overall_cached` is `compute_overall` of the real ratings, which is what
    `ratings_service.recompute_overall` writes for a seeded real player."""
    from dba2.sim.ratings import compute_overall
    from tools.spikes.rc31_archetype_spike import _ratings_from_raw

    id_by_ref = {ref: i for i, ref in enumerate(sorted(row[0] for row in roster), start=1)}
    players = []
    ratings_by_player = {}
    for ref, position, raw in roster:
        pid = id_by_ref[ref]
        ratings = dataclasses.replace(_ratings_from_raw(raw), player_id=pid)
        ratings_by_player[pid] = ratings
        players.append(
            make_player(
                pid, primary_position=position, overall_cached=compute_overall(ratings, position)
            )
        )
    return players, ratings_by_player, id_by_ref


@pytest.fixture(scope="module")
def production_population():
    """RC-35.4 D2's population, via the spike's own loader: every 2009-2025
    real franchise-season's top 14 by minutes, on its real franchise (501
    rosters)."""
    from tools.spikes.rc31_archetype_spike import _PRODUCTION_SEASONS, production_rosters

    rosters, _shortfalls = production_rosters(_PRODUCTION_SEASONS)
    return rosters


def _talent_ranks(players):
    ordered = sorted(players, key=lambda p: (-p.overall_cached, p.id))
    return {p.id: rank for rank, p in enumerate(ordered, start=1)}


@pytest.mark.slow
def test_the_pass_reproduces_the_measured_mechanism_on_every_production_roster(
    production_population,
):
    """`construct_team_roles` against the spike's `_rank_at_cell(beta=0,
    floor_rank=8)` -- the function every RC-37 number was scored with -- on
    all 501 production rosters: identical rank for every player, and the
    recommended cell's published operational figures (RC-37.4; rc37 report
    section 7) come back to the printed digit. This is what makes the
    shipped constants and the shipped rule the measured ones, rather than
    the design's prose about them."""
    from tools.spikes.rc31_archetype_spike import (
        _rank_at_cell,
        _state_for,
        _talent,
        null_s_a,
    )

    s_a = null_s_a(RHO_BAR)
    n_players = n_fired = rosters_le3 = lifts = rosters_with_lift = 0
    for roster in production_population:
        players, ratings_by_player, id_by_ref = _production_team(roster)
        by_id = {
            a.player_id: a
            for a in construct_team_roles(
                players,
                ratings_by_player,
                philosophy=GmPhilosophy.TENDENCY_RESPECTER,
                season=2026,
            )
        }
        talents = {ref: _talent(raw, pos) for ref, pos, raw in roster}
        spike = _rank_at_cell(
            _state_for(roster, s_a, talents),
            beta=0.0,
            z_floor=Z_FLOOR,
            z_cap=1.5,
            floor_rank=FLOOR_RANK,
        )
        for ref, pid in id_by_ref.items():
            assert by_id[pid].rank == spike[ref][0], (ref, by_id[pid].rank, spike[ref][0])
            assert by_id[pid].role_label.endswith("_specialist") == spike[ref][2], ref

        talent_rank = _talent_ranks(players)
        fired = [pid for pid, a in by_id.items() if a.role_label.endswith("_specialist")]
        lifted = sum(1 for pid in by_id if talent_rank[pid] >= 10 and by_id[pid].rank <= 9)
        n_players += len(players)
        n_fired += len(fired)
        rosters_le3 += len(fired) <= 3
        lifts += lifted
        rosters_with_lift += lifted > 0

    n = len(production_population)
    assert n == 501
    assert round(n_fired / n_players * 100, 2) == 8.28
    assert round(rosters_le3 / n * 100, 1) == 95.6
    assert round(lifts / n, 3) == 0.395
    assert round(rosters_with_lift / n * 100, 1) == 35.1


def test_unmodified_real_rank_11_players_are_rarely_lifted(production_population):
    """Negative control, as a rate on the production population (RC-37
    revision 7.1) rather than one roster: with no rating edit at all, the
    talent-rank-11 player is lifted to rank <= 9 on 6.4% of the 501 real
    rosters -- under criterion 5's 10% bar for null firing -- and the
    league-wide specialist rate sits between the N1 null's 2.50% and
    criterion 7a's 20% cap. A single roster could pass or fail on one
    player; this cannot quietly drift."""
    rank11_lifted = n_players = n_fired = 0
    for roster in production_population:
        players, ratings_by_player, _ids = _production_team(roster)
        assignments = construct_team_roles(
            players, ratings_by_player, philosophy=GmPhilosophy.TENDENCY_RESPECTER, season=2026
        )
        talent_rank = _talent_ranks(players)
        rank11 = next(pid for pid, r in talent_rank.items() if r == 11)
        rank11_lifted += next(a.rank for a in assignments if a.player_id == rank11) <= 9
        n_players += len(assignments)
        n_fired += sum(1 for a in assignments if a.role_label.endswith("_specialist"))

    rate = rank11_lifted / len(production_population)
    assert rate <= 0.10, rate
    assert round(rate * 100, 1) == 6.4
    assert 0.025 < n_fired / n_players <= 0.20


def _boosted_rebounder(players, ratings_by_player, target_id, delta=25):
    """RC-31's +25 raw edit on both rebounder fields (clamped at 99), with
    `overall_cached` recomputed from the edited ratings (RC-34.1)."""
    from dba2.sim.ratings import compute_overall

    ratings = ratings_by_player[target_id]
    boosted = dataclasses.replace(
        ratings,
        offensive_rebounding=min(99, ratings.offensive_rebounding + delta),
        defensive_rebounding=min(99, ratings.defensive_rebounding + delta),
    )
    target = next(p for p in players if p.id == target_id)
    new_overall = compute_overall(boosted, target.primary_position)
    players = [
        dataclasses.replace(p, overall_cached=new_overall) if p.id == target_id else p
        for p in players
    ]
    return players, {**ratings_by_player, target_id: boosted}


def test_a_real_specialist_gets_promoted_past_the_deep_bench_cliff():
    """The specialist-promotion demo, end to end on all 30 real 2025 rosters:
    each team's talent-rank-11 player gets RC-31's +25 on both rebounding
    fields. 29 of the 30 fire as a rebounder and reach the rotation edge;
    the 30th (external_ref 56677846, a 2025 SG) stays a bench player at his
    talent rank 11 because +25 alone does not clear `Z_FLOOR` on that
    roster's own field spread -- criterion 1 is a >= 90% rate, never 100%,
    and on the pre-edf5633 seeds all 30 happened to fire. Where no other
    firer sits between talent rank 8 and him, he lands at exactly rank 8 --
    19.2 raw minutes instead of 3.3 -- and the rationale says he was lifted;
    where a firer is already there (a firing talent-rank-8 player, or another
    buried firer ahead of him in talent order), he lands one slot lower per
    such firer, exactly as D15 orders."""
    from tools.spikes.rc31_archetype_spike import production_rosters

    rosters, _shortfalls = production_rosters((2025,))
    assert len(rosters) == 30
    at_8 = 0
    not_fired = []
    for roster in rosters:
        players, ratings_by_player, _ids = _production_team(roster)
        target_id = next(pid for pid, r in _talent_ranks(players).items() if r == 11)
        players, ratings_by_player = _boosted_rebounder(players, ratings_by_player, target_id)
        by_id = {
            a.player_id: a
            for a in construct_team_roles(
                players,
                ratings_by_player,
                philosophy=GmPhilosophy.TENDENCY_RESPECTER,
                season=2026,
            )
        }
        talent_rank = _talent_ranks(players)
        promoted = by_id[target_id]
        if promoted.role_label != "rebounding_specialist":
            assert promoted.role_label == "bench"
            assert promoted.rank == talent_rank[target_id] == 11
            not_fired.append(sorted(row[0] for row in roster)[target_id - 1])
            continue
        firers_ahead = sum(
            1
            for pid, a in by_id.items()
            if pid != target_id
            and a.role_label.endswith("_specialist")
            and FLOOR_RANK <= talent_rank[pid] < talent_rank[target_id]
        )
        assert promoted.rank == FLOOR_RANK + firers_ahead
        assert promoted.rank <= 9
        if promoted.rank == FLOOR_RANK:
            at_8 += 1
            assert "lifted to the rotation as a rebounder" in promoted.rationale
            assert float(promoted.minutes_share) * 240.0 == pytest.approx(19.2, abs=0.05)
    assert not_fired == ["56677846"]
    assert at_8 == 25


def test_a_firing_talent_rank_8_player_keeps_8_on_a_real_roster():
    """The firing-rank-8 case on real data (2025 DET and SAC under the demo's
    +25 edit): the talent-rank-8 player fires on his own, keeps rank 8, and
    the lifted specialist lands at 9 behind him."""
    from tools.spikes.rc31_archetype_spike import _production_roster_index, production_rosters

    rosters, _shortfalls = production_rosters((2025,))
    keys = _production_roster_index((2025,))
    seen = set()
    for (_season, code), roster in zip(keys, rosters, strict=True):
        if code not in ("DET", "SAC"):
            continue
        players, ratings_by_player, _ids = _production_team(roster)
        talent_rank = _talent_ranks(players)
        target_id = next(pid for pid, r in talent_rank.items() if r == 11)
        rank8_id = next(pid for pid, r in talent_rank.items() if r == 8)
        players, ratings_by_player = _boosted_rebounder(players, ratings_by_player, target_id)
        by_id = {
            a.player_id: a
            for a in construct_team_roles(
                players,
                ratings_by_player,
                philosophy=GmPhilosophy.TENDENCY_RESPECTER,
                season=2026,
            )
        }
        assert by_id[rank8_id].role_label.endswith("_specialist"), code
        assert by_id[rank8_id].rank == 8, code
        assert "specialist" in by_id[rank8_id].rationale, code
        assert by_id[target_id].rank == 9, code
        seen.add(code)
    assert seen == {"DET", "SAC"}


def test_ranks_1_to_7_follow_each_philosophys_own_talent_order(production_population):
    """Production ranks by `tilted_overall`, not `compute_overall`: for
    `defense_first` that is a re-weighted overall. The floor must leave the
    first seven slots in *that* order for every philosophy -- the spike
    measured the identity case, and the rule is philosophy-independent
    only if this holds."""
    from dba2.managers.valuation import tilted_overall

    for roster in production_population[:60]:
        players, ratings_by_player, _ids = _production_team(roster)
        for philosophy in GmPhilosophy:
            profile = PROFILES[philosophy]
            talent = {
                p.id: tilted_overall(
                    p.overall_cached, ratings_by_player[p.id], p.primary_position, profile
                )
                for p in players
            }
            expected_top7 = sorted(talent, key=lambda pid: (-talent[pid], pid))[:7]
            assignments = construct_team_roles(
                players, ratings_by_player, philosophy=philosophy, season=2026
            )
            top7 = [a.player_id for a in sorted(assignments, key=lambda a: a.rank)[:7]]
            assert top7 == expected_top7, philosophy


def test_arch_z_pinned_s_a_table_is_the_null_table_at_the_pinned_rho_bar():
    """RC-37.7 step 3: `S_A` is RC-36's closed form at `RHO_BAR`, so it has
    exactly three values (by field count) and must equal both the spike's
    own `null_s_a(RHO_BAR)` -- the function the design's numbers were
    measured with -- and the committed three-value table (0.3952 / 0.3184 /
    0.2720 for m = 2 / 3 / 4, rc37 report section 1). `--check` re-derives
    `RHO_BAR` itself from the production population."""
    from tools.spikes.rc31_archetype_spike import null_s_a

    assert set(S_A) == set(ARCHETYPES)
    assert RHO_BAR == 0.671224
    assert null_s_a(RHO_BAR) == pytest.approx(dict(S_A), abs=1e-15)
    committed_by_m = {2: 0.3952, 3: 0.3184, 4: 0.2720}
    for name, fields in ARCHETYPES.items():
        assert round(S_A[name], 4) == committed_by_m[len(fields)], name


def test_the_rationale_says_what_the_floor_did_with_the_right_article():
    """Three specialist clauses (RC-37.7 step 1): lifted into the rotation,
    moved up but still outside it, or not moved. `interior finisher` is the
    one display label that takes "an" -- the live 2025 run wrote "a
    interior finisher" before this was fixed."""
    from dba2.services.role_service import _rationale

    def clause(rank, talent_rank, label):
        return _rationale(
            rank=rank,
            talent_rank=talent_rank,
            n=14,
            tilted_ovr=60,
            archetype_label=label,
            philosophy_name="tendency respecter",
            lam=1.0,
            minutes=19.2,
        )

    assert "lifted to the rotation as a rebounder" in clause(8, 11, "rebounder")
    assert "moved up the bench as an interior finisher" in clause(10, 13, "interior_finisher")
    assert "(60 ovr, an interior finisher specialist)" in clause(6, 6, "interior_finisher")
    assert "(60 ovr, a floor spacer specialist)" in clause(8, 8, "floor_spacer")
    assert "(60 ovr)" in clause(9, 9, None)


def test_the_pins_are_the_rc37_recommended_cell():
    assert Z_FLOOR == 2.9682
    assert FLOOR_RANK == 8


def test_touch_share_is_read_only_as_minutes_by_the_sim():
    """What the floor changes is `touch_share` (minutes) and the display-only
    `role` label. Usage must keep coming from `overall_cached`
    (`beats/setup.py::_role_weight`, RC-20), so a lifted specialist plays
    more without being handed more shots. Structural guard: inside
    `dba2/sim/`, only `game.py` reads `touch_share_f` (as the minutes share)
    and only `context.py` builds it; nothing reads the `role` label."""
    import re
    from pathlib import Path

    sim_dir = Path(__file__).resolve().parents[3] / "dba2" / "sim"
    readers = set()
    label_readers = set()
    for path in sim_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        code = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
        if re.search(r"\.touch_share_f\b", code):
            readers.add(path.relative_to(sim_dir).as_posix())
        if re.search(r"\brole\.role\b|\.role_label\b", code):
            label_readers.add(path.relative_to(sim_dir).as_posix())
    assert readers == {"game.py"}, readers
    assert label_readers == set(), label_readers


def test_tilted_overall_matches_effective_talent_ovr():
    """RC-22.3 step 1's C10 refactor: `tilted_overall` and `effective_
    talent_ovr` must agree, since the latter is now a one-line wrapper
    over the former."""
    from dba2.managers.valuation import effective_talent_ovr, tilted_overall
    from tests.fixtures.managers import make_roster_player

    for philosophy in GmPhilosophy:
        profile = PROFILES[philosophy]
        roster_player = make_roster_player(1, age=27, season=2026, overall_cached=72)
        direct = tilted_overall(
            roster_player.player.overall_cached,
            roster_player.ratings,
            roster_player.player.primary_position,
            profile,
        )
        wrapped = effective_talent_ovr(roster_player, profile)
        assert direct == wrapped, philosophy
