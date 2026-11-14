"""The DB-free half of `services/offer_service.py` (docs/design/phase-5-trade-
offer-review-surface.md §7.4 tests 9 and 10's pure parts, §4.4's consent
record). Everything here runs with no Postgres: `_validate_package_ids` is
pure by design (it must be, to run before any lock), the consent record and
stance mapping are pure functions of their arguments.
"""
from __future__ import annotations

import logging

import pytest

from dba2.core.errors import ValidationError
from dba2.domain.phase import TradeStatus
from dba2.domain.team import GmPhilosophy, Team, Timeline
from dba2.managers.explain import DecisionRecord
from dba2.managers.negotiation import NegotiationStance
from dba2.managers.tuning import CompetitiveWindow
from dba2.services import offer_service
from dba2.services.offer_service import (
    MAX_PICKS_PER_SIDE,
    MAX_PLAYERS_PER_SIDE,
    _consent_record,
    _lock_offer_for_answer,
    _resolution_for_ai_answer,
    _resolve_locked,
    _stance_for,
    _validate_package_ids,
)


def _ok(**overrides):
    args = dict(
        from_team_id=1, to_team_id=2, players_from_to=(10,), players_to_from=(20,),
        picks_from_to=(), picks_to_from=(),
    )
    args.update(overrides)
    return args


# ---------------------------------------------------------------------------
# _validate_package_ids (§4.2 step b, B9; §7.4 test 9)
# ---------------------------------------------------------------------------


def test_a_well_formed_package_passes():
    _validate_package_ids(**_ok())
    _validate_package_ids(
        **_ok(players_from_to=(10, 11), picks_from_to=(1,), players_to_from=(), picks_to_from=(2,))
    )


def test_9a_the_same_player_twice_on_one_side_is_refused_naming_the_id():
    with pytest.raises(ValidationError, match=r"Id 10 is listed twice under players you send"):
        _validate_package_ids(**_ok(players_from_to=(10, 10)))


def test_9a_the_same_pick_twice_on_one_side_is_refused_naming_the_id():
    with pytest.raises(ValidationError, match=r"Id 5 is listed twice under picks you get"):
        _validate_package_ids(**_ok(picks_to_from=(5, 5)))


def test_9b_the_same_player_on_both_sides_is_refused_naming_the_id():
    with pytest.raises(ValidationError, match=r"Player id 10 is on both sides"):
        _validate_package_ids(**_ok(players_from_to=(10,), players_to_from=(10,)))


def test_9b_the_same_pick_on_both_sides_is_refused_naming_the_id():
    with pytest.raises(ValidationError, match=r"Pick id 3 is on both sides"):
        _validate_package_ids(**_ok(picks_from_to=(3,), picks_to_from=(3,)))


def test_9c_a_team_trading_with_itself_is_refused():
    with pytest.raises(ValidationError, match=r"cannot trade with itself"):
        _validate_package_ids(**_ok(from_team_id=4, to_team_id=4))


def test_each_side_must_name_at_least_one_asset():
    with pytest.raises(ValidationError, match=r"sending"):
        _validate_package_ids(**_ok(players_from_to=(), picks_from_to=()))
    with pytest.raises(ValidationError, match=r"getting"):
        _validate_package_ids(**_ok(players_to_from=(), picks_to_from=()))


def test_a_pick_alone_is_a_valid_side():
    _validate_package_ids(**_ok(players_from_to=(), picks_from_to=(7,)))


def test_the_per_side_caps_are_enforced_and_the_boundary_passes():
    _validate_package_ids(
        **_ok(
            players_from_to=tuple(range(100, 100 + MAX_PLAYERS_PER_SIDE)),
            picks_to_from=tuple(range(200, 200 + MAX_PICKS_PER_SIDE)),
        )
    )
    with pytest.raises(ValidationError, match=r"At most 3 players you send"):
        _validate_package_ids(**_ok(players_from_to=(100, 101, 102, 103)))
    with pytest.raises(ValidationError, match=r"At most 2 picks you send"):
        _validate_package_ids(**_ok(picks_from_to=(1, 2, 3)))


def test_a_player_id_typed_into_a_pick_slot_is_deliberately_not_caught_here():
    """Both are bare ints; nothing about the id distinguishes them. That is a
    fact about the database and fails at the bundle build, where it can be
    told apart -- this check must not pretend to know."""
    _validate_package_ids(**_ok(players_from_to=(10,), picks_to_from=(10,)))


# ---------------------------------------------------------------------------
# The consent record (§4.4)
# ---------------------------------------------------------------------------


def _team(*, philosophy=GmPhilosophy.STAR_MAXER, timeline=Timeline.WIN_NOW) -> Team:
    return Team(
        id=7, league_id=1, code="AAA", name="A", city="A", conference="East",
        division="Atlantic", manager_user_id=101, gm_philosophy=philosophy,
        timeline=timeline, home_market_size=3,
    )


def test_the_consent_record_carries_every_field_from_its_documented_source():
    record = _consent_record(_team(), offer_id=42)
    assert record.team_id == 7
    assert record.philosophy is GmPhilosophy.STAR_MAXER  # team.gm_philosophy, not inferred
    assert record.window is CompetitiveWindow.WIN_NOW  # _TIMELINE_TO_WINDOW, not a classifier
    assert record.subject == "offer:42"
    assert record.score == 0.0 and record.threshold == 0.0 and record.terms == ()
    assert record.reconstructs()


def test_the_consent_verdict_is_the_literal_accept():
    """`negotiation.respond` tests `record.verdict == "accept"` -- exact string
    equality. Any other token (a `"human_consent"` parallel to
    `sign_free_agent`'s `"human_sign"`) reads to it as a REFUSAL, and every
    human accept would resolve REJECTED."""
    assert _consent_record(_team(), offer_id=1).verdict == "accept"


def test_a_human_consent_token_other_than_accept_would_read_as_a_refusal():
    refused = DecisionRecord(
        team_id=1, philosophy=GmPhilosophy.EGALITARIAN, window=CompetitiveWindow.RETOOL,
        subject="offer:1", verdict="human_consent", score=0.0, threshold=0.0, terms=(),
    )
    stance, _token = _stance_for(refused, _consent_record(_team(), offer_id=1))
    assert stance is not NegotiationStance.ACCEPT


@pytest.mark.parametrize("timeline", list(Timeline))
def test_the_consent_window_covers_every_persisted_timeline(timeline):
    assert isinstance(_consent_record(_team(timeline=timeline), offer_id=1).window,
                      CompetitiveWindow)


# ---------------------------------------------------------------------------
# _stance_for and the stance -> status map (§4.4; §7.4 test 10's pure half)
# ---------------------------------------------------------------------------


def _ai_record(*, verdict: str, score: float = 0.0, threshold: float = 1.0) -> DecisionRecord:
    return DecisionRecord(
        team_id=9, philosophy=GmPhilosophy.EGALITARIAN, window=CompetitiveWindow.RETOOL,
        subject="ai", verdict=verdict, score=score, threshold=threshold, terms=(),
    )


def test_two_consent_records_are_an_accept():
    a, b = _consent_record(_team(), offer_id=1), _consent_record(_team(), offer_id=1)
    assert _stance_for(a, b) == (NegotiationStance.ACCEPT, None)


def test_a_consent_record_against_an_accepting_ai_is_an_accept():
    stance, token = _stance_for(
        _consent_record(_team(), offer_id=1), _ai_record(verdict="accept", score=5.0)
    )
    assert (stance, token) == (NegotiationStance.ACCEPT, None)


def test_a_refusing_ai_never_yields_counter_at_zero_rounds():
    """`rounds_remaining=0` makes `COUNTER` unreachable: a near miss is
    `EXPIRE`, a far one `REJECT_TOO_FAR` -- both refusals."""
    consent = _consent_record(_team(), offer_id=1)
    near, _ = _stance_for(consent, _ai_record(verdict="reject", score=0.9, threshold=1.0))
    far, _ = _stance_for(consent, _ai_record(verdict="reject", score=-500.0, threshold=1.0))
    assert near is NegotiationStance.EXPIRE
    assert far is NegotiationStance.REJECT_TOO_FAR


def test_an_illegal_record_yields_reject_illegal_with_the_failing_verdict_as_the_token():
    consent = _consent_record(_team(), offer_id=1)
    stance, token = _stance_for(consent, _ai_record(verdict="illegal:roster_max"))
    assert (stance, token) == (NegotiationStance.REJECT_ILLEGAL, "illegal:roster_max")


# The AI-as-answering-partner map, written out by hand: every `NegotiationStance`
# but ACCEPT (the caller's) and the status it must resolve to. Deliberately NOT
# derived from the function under test.
_PARTNER_STATUS = {
    NegotiationStance.REJECT_TOO_FAR: TradeStatus.REJECTED,
    # The mapping most likely to be got wrong: two unrelated words.
    NegotiationStance.EXPIRE: TradeStatus.REJECTED,
    NegotiationStance.REJECT_DEADLOCK: TradeStatus.REJECTED,
    NegotiationStance.REJECT_ILLEGAL: TradeStatus.EXPIRED,
    NegotiationStance.COUNTER: TradeStatus.REJECTED,
}


@pytest.mark.parametrize(("stance", "status"), list(_PARTNER_STATUS.items()))
def test_every_refusing_stance_maps_to_a_terminal_status(stance, status):
    resolution = _resolution_for_ai_answer(stance, "illegal:x", ai_code="AAA", ai_is_proposer=False)
    assert resolution.status is status
    assert resolution.status is not TradeStatus.PROPOSED
    assert resolution.detail


@pytest.mark.parametrize("ai_is_proposer", [False, True])
def test_the_stance_map_refuses_to_be_asked_about_accept(ai_is_proposer):
    """`ACCEPT` used to fall through to a plain refusal, so a caller that
    dropped its `is not ACCEPT` guard would silently reject a deal the AI
    accepted. It is an invariant violation now."""
    with pytest.raises(AssertionError, match="ACCEPT"):
        _resolution_for_ai_answer(
            NegotiationStance.ACCEPT, None, ai_code="AAA", ai_is_proposer=ai_is_proposer
        )


def test_the_stance_map_covers_every_stance_but_accept():
    """`ACCEPT` is the caller's; every other member must have an EXPLICIT
    expected status in the table above, so a new `NegotiationStance` member
    fails here (its entry is missing) rather than defaulting silently in
    production."""
    assert set(_PARTNER_STATUS) == set(NegotiationStance) - {NegotiationStance.ACCEPT}, (
        "NegotiationStance gained or lost a member: decide its TradeStatus in "
        "_resolution_for_ai_answer AND in _PARTNER_STATUS / _PROPOSER_STATUS"
    )
    assert set(_PROPOSER_STATUS) == set(_PARTNER_STATUS)
    for stance in NegotiationStance:
        if stance is NegotiationStance.ACCEPT:
            continue
        got = _resolution_for_ai_answer(
            stance, "illegal:x", ai_code="AAA", ai_is_proposer=False
        ).status
        assert got is _PARTNER_STATUS[stance], stance


@pytest.mark.parametrize(
    "stance", [NegotiationStance.REJECT_ILLEGAL, NegotiationStance.COUNTER]
)
def test_the_two_unreachable_stances_are_logged_as_errors(stance, caplog):
    with caplog.at_level(logging.ERROR, logger=offer_service.__name__):
        _resolution_for_ai_answer(stance, "illegal:x", ai_code="AAA", ai_is_proposer=False)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_a_plain_refusal_is_not_logged_as_an_error(caplog):
    with caplog.at_level(logging.ERROR, logger=offer_service.__name__):
        _resolution_for_ai_answer(
            NegotiationStance.REJECT_TOO_FAR, None, ai_code="AAA", ai_is_proposer=False
        )
    assert not caplog.records


def test_only_the_illegal_stance_carries_the_token():
    assert _resolution_for_ai_answer(
        NegotiationStance.REJECT_ILLEGAL, "illegal:apron_1", ai_code="A", ai_is_proposer=False
    ).illegal_token == "illegal:apron_1"
    assert _resolution_for_ai_answer(
        NegotiationStance.EXPIRE, "illegal:apron_1", ai_code="A", ai_is_proposer=False
    ).illegal_token is None


# The AI-proposer path (§5.3 step 10) shares the map, the alarms and the
# messages; the ONE difference is that a plain refusal is EXPIRED, not REJECTED.
_PROPOSER_STATUS = {
    NegotiationStance.REJECT_TOO_FAR: TradeStatus.EXPIRED,
    NegotiationStance.EXPIRE: TradeStatus.EXPIRED,
    NegotiationStance.REJECT_DEADLOCK: TradeStatus.EXPIRED,
    NegotiationStance.REJECT_ILLEGAL: TradeStatus.EXPIRED,
    NegotiationStance.COUNTER: TradeStatus.EXPIRED,
}


@pytest.mark.parametrize(("stance", "status"), list(_PROPOSER_STATUS.items()))
def test_an_ai_proposer_that_withdraws_always_resolves_expired(stance, status):
    resolution = _resolution_for_ai_answer(
        stance, "illegal:x", ai_code="AAA", ai_is_proposer=True
    )
    assert resolution.status is status


@pytest.mark.parametrize(
    "stance",
    [
        NegotiationStance.REJECT_TOO_FAR,
        NegotiationStance.EXPIRE,
        NegotiationStance.REJECT_DEADLOCK,
        NegotiationStance.COUNTER,
    ],
)
def test_an_ai_proposers_plain_withdrawal_says_no_longer_wants_and_carries_no_token(stance):
    resolution = _resolution_for_ai_answer(
        stance, "illegal:x", ai_code="AAA", ai_is_proposer=True
    )
    assert resolution.detail == "AAA no longer wants this deal."
    assert resolution.illegal_token is None


def test_an_ai_proposer_reporting_illegal_names_the_rules_not_a_change_of_heart():
    resolution = _resolution_for_ai_answer(
        NegotiationStance.REJECT_ILLEGAL, "illegal:roster_max", ai_code="AAA",
        ai_is_proposer=True,
    )
    assert resolution.illegal_token == "illegal:roster_max"
    assert resolution.detail == "The league's rules changed under this deal."


@pytest.mark.parametrize(
    "stance", [NegotiationStance.REJECT_ILLEGAL, NegotiationStance.COUNTER]
)
def test_the_unreachable_stances_alarm_on_the_ai_proposer_path_too(stance, caplog):
    with caplog.at_level(logging.ERROR, logger=offer_service.__name__):
        _resolution_for_ai_answer(stance, "illegal:x", ai_code="AAA", ai_is_proposer=True)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# The shared core's own discipline marker (§3, OS-11)
# ---------------------------------------------------------------------------


def test_the_resolution_core_carries_its_never_opens_a_gate_marker():
    """Nothing in the discipline suite polices `_resolve_locked` (it contains
    no gate call, is in no RMLP table), so its rule is carried by
    `acquire`'s runtime assertion plus this required docstring marker."""
    assert (
        "RMLP: receives an open gated transaction from its caller; never opens a gate"
        in (_resolve_locked.__doc__ or "")
    )


def test_the_answer_lock_helper_carries_its_never_opens_a_gate_marker():
    """`_lock_offer_for_answer` takes tier-1/2 locks but is called by `respond`
    and `counter`, which each open their own gate first. Like the core it is
    in no RMLP table, so the same required marker carries its rule."""
    assert (
        "RMLP: receives an open gated transaction from its caller; never opens a gate"
        in (_lock_offer_for_answer.__doc__ or "")
    )
