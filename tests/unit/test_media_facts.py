"""`dba2/domain/media_facts.py` -- the registry tests MV-9 names twice
(`:2622-2624`, `:2662-2668`) as "folded into the one parametrized registry
test" but that, until this fix, did not exist anywhere (Nit 6, critic round
on commits `b288b8e`..`99db9f4`). This is exactly the class of bug BLOCKING
4 (an undeclared `team_name` fact on `TRADE_MARKET_WATCH`) slipped through
for: every OTHER guard in this codebase checks a template's DECLARED slots
against the registry, never a detector's ACTUALLY EMITTED facts against it.

Reuses `tests/unit/test_media_salience.py`'s own real-package fixtures
(`_sufficient_package` for the original eleven kinds via the real
`derive_frame` -> `legacy.adapt_frame` -> `seal_package` path, and
`_sufficient_packages_for_the_new_eleven_kinds` for MV-2's eleven, each
built through its own detector's real function) rather than a second,
independently hand-built package per kind that could drift from the first."""

from __future__ import annotations

from dba2.domain.media import StoryKind
from dba2.domain.media_facts import (
    _PACKAGE_PREFIX,
    FACT_KEYS_BY_KIND,
    REQUIRED_FACT_KEYS_BY_KIND,
    UNIVERSAL_FACT_KEYS,
    Subject,
    SubjectKind,
    identity_facts,
    opponent_team_name_is_game_opponent,
)
from tests.unit.test_media_salience import (
    _ORIGINAL_ELEVEN_STORY_KINDS,
    _sufficient_package,
    _sufficient_packages_for_the_new_eleven_kinds,
)


def _real_package_per_kind() -> dict[StoryKind, object]:
    packages = [_sufficient_package(kind) for kind in _ORIGINAL_ELEVEN_STORY_KINDS]
    packages += list(_sufficient_packages_for_the_new_eleven_kinds())
    by_kind = {p.story_kind: p for p in packages}
    assert set(by_kind) == set(StoryKind), (
        "every StoryKind member must have a real, firing package fixture here, or this "
        "test cannot make its own claim about that kind at all"
    )
    return by_kind


_PACKAGES_BY_KIND = _real_package_per_kind()


def test_every_story_kind_has_a_real_package_fixture():
    """The fixture-completeness precondition every test below depends on,
    asserted on its own so a future `StoryKind` addition with no fixture
    fails here first, with a clear message, rather than inside a
    parametrized test's own less obvious `KeyError`."""
    assert set(_PACKAGES_BY_KIND) == set(StoryKind)


def test_every_detectors_emitted_facts_are_a_subset_of_its_registry_entry():
    """The parametrized registry test MV-9 names twice and no prior build
    step actually wrote (Nit 6). Reproduction: before BLOCKING 4's fix
    (`team_name` added to `FACT_KEYS_BY_KIND[TRADE_MARKET_WATCH]`), this
    failed for `TRADE_MARKET_WATCH` specifically, because `trade_market_
    watch_package`'s `Subject(kind=PLAYER, player_id=..., team_id=...)`
    causes `identity_facts` to emit a `team_name` fact no registry entry
    declared."""
    for kind, package in _PACKAGES_BY_KIND.items():
        # `season`/`phase` are added by `seal_package` (via `UNIVERSAL_FACT_KEYS`),
        # never by a detector -- present here only for the original eleven kinds'
        # fixtures, which are built through the real sealing path; the eleven new
        # kinds' fixtures call each detector's own function directly, unsealed.
        allowed = set(FACT_KEYS_BY_KIND[kind]) | set(UNIVERSAL_FACT_KEYS)
        emitted = {fact.key for fact in package.facts}
        undeclared = emitted - allowed
        assert not undeclared, (
            f"{kind.name} emits fact key(s) {undeclared} not declared in "
            f"FACT_KEYS_BY_KIND[{kind.name}]"
        )


def test_every_story_kind_has_a_fact_keys_registry_entry():
    assert set(FACT_KEYS_BY_KIND) == set(StoryKind)


def test_every_story_kind_has_a_required_fact_keys_registry_entry():
    assert set(REQUIRED_FACT_KEYS_BY_KIND) == set(StoryKind)


def test_required_fact_keys_are_always_a_subset_of_fact_keys():
    for kind in StoryKind:
        assert REQUIRED_FACT_KEYS_BY_KIND[kind] <= set(FACT_KEYS_BY_KIND[kind])


def test_no_registry_key_begins_with_the_package_prefix():
    """MV-1c.1 rule 4: no real fact key may collide with the trigger view's
    own `package_*` pseudo-features."""
    for kind, keys in FACT_KEYS_BY_KIND.items():
        for key in keys:
            assert not key.startswith(_PACKAGE_PREFIX), (
                f"{kind.name}'s registry entry declares {key!r}, which collides with the "
                "trigger view's own package_ prefix"
            )


# ---------------------------------------------------------------------------
# Nit 10 (critic round on commits b288b8e..99db9f4): `identity_facts`
# hardcoded label="Opponent" for every opponent_team_name fact, which is
# wrong for TRADE_BROKEN_PROMISE (that id is the player's FORMER team).
# ---------------------------------------------------------------------------


def test_opponent_team_name_label_is_former_team_for_trade_broken_promise():
    subject = Subject(
        kind=SubjectKind.PLAYER, player_id=1, player_name="A Player", team_id=2,
        team_name="New Team", opponent_team_id=3, opponent_team_name="Old Team",
    )
    facts = identity_facts(subject, StoryKind.TRADE_BROKEN_PROMISE)
    opponent_fact = next(f for f in facts if f.key == "opponent_team_name")
    assert opponent_fact.label == "Former team"


def test_opponent_team_name_label_stays_opponent_for_a_real_game_opponent_kind():
    """Reproduction: before this fix, every kind (including
    `TRADE_BROKEN_PROMISE`) got the same hardcoded `"Opponent"` label --
    this pins that the fix did not flip the default for kinds where
    "Opponent" is actually correct (`TRADE_EXECUTED`/`RIVALRY_MATCHUP`/
    `REVENGE_GAME`'s real game/trade opponents)."""
    subject = Subject(
        kind=SubjectKind.MATCHUP, team_id=1, team_name="Home Team",
        opponent_team_id=2, opponent_team_name="Away Team",
    )
    facts = identity_facts(subject, StoryKind.TRADE_EXECUTED)
    opponent_fact = next(f for f in facts if f.key == "opponent_team_name")
    assert opponent_fact.label == "Opponent"


# ---------------------------------------------------------------------------
# Bug B4.1 (critic round on commits ea84c5f..665526d): `_generate_headline`
# used to use "opponent_team_name is present" alone as its "vs." trigger,
# which fired for TRADE_BROKEN_PROMISE too (a FORMER team, not a game
# opponent). `opponent_team_name_is_game_opponent` is the fix's own signal.
# ---------------------------------------------------------------------------


def test_opponent_team_name_is_not_a_game_opponent_for_trade_broken_promise():
    assert opponent_team_name_is_game_opponent(StoryKind.TRADE_BROKEN_PROMISE) is False


def test_opponent_team_name_is_a_game_opponent_for_real_matchup_kinds():
    for kind in (StoryKind.TRADE_EXECUTED, StoryKind.RIVALRY_MATCHUP, StoryKind.REVENGE_GAME):
        assert opponent_team_name_is_game_opponent(kind) is True


def test_a_fact_key_has_exactly_one_declared_type_across_every_kind_that_declares_it():
    """A key declared `INT` under one kind and `FLOAT`/`TEXT` under another
    would make the trigger view's "which value does this key resolve to"
    lookup ambiguous (MV-1c.1's own closed-type-space rule)."""
    seen: dict[str, object] = {}
    for keys in FACT_KEYS_BY_KIND.values():
        for key, declared_type in keys.items():
            if key in seen:
                assert seen[key] == declared_type, (
                    f"{key!r} is declared {seen[key]} by one kind and {declared_type} by another"
                )
            else:
                seen[key] = declared_type
