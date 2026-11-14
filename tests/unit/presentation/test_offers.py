"""`presentation/offers.py` -- the trade-offer review surface's pure builders
(docs/design/phase-5-trade-offer-review-surface.md §4.3, §6.5, §12 step 7).

Two kinds of test, following the rest of this directory: committed goldens
(P5-22 item 1 -- a render change shows up as a diff) and Discord-limit
invariants over adversarial input (§9.2 -- max-length names, more offers than
fit, more assets than the service allows). No DB, no Discord.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from dba2.domain.draft import DraftPick, ProtectionType
from dba2.domain.phase import Phase, TradeStatus
from dba2.domain.player import Player, Position, RosterStatus
from dba2.domain.team import GmPhilosophy, Team, Timeline, TimelineSource
from dba2.domain.trade_offer import TradeOffer
from dba2.presentation.offers import (
    EXPIRY_SUFFIX,
    OfferNames,
    build_counter_pointer_text,
    build_offer_notification_text,
    build_offer_panel,
    build_offers_panel,
    describe_offer_result,
)
from dba2.presentation.panel import (
    MAX_FIELD_NAME_CHARS,
    MAX_FIELD_VALUE_CHARS,
    MAX_FIELDS,
    MAX_TOTAL_CHARS,
    Panel,
)
from tests.unit.presentation._golden_helpers import load_golden, panel_to_dict

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _team(team_id: int, code: str, city: str, name: str) -> Team:
    return Team(
        id=team_id, league_id=1, code=code, city=city, name=name, conference="East",
        division="Atlantic", manager_user_id=None, gm_philosophy=GmPhilosophy.EGALITARIAN,
        timeline=Timeline.RETOOL, home_market_size=3, timeline_source=TimelineSource.AUTO,
    )


def _player(
    player_id: int, first: str, last: str, *, overall: int, position: Position = Position.SF
) -> Player:
    return Player(
        id=player_id, league_id=1, first_name=first, last_name=last, external_ref=None,
        primary_position=position, secondary_position=None, height_in=78, weight_lb=210,
        wingspan_in=80, birth_date=date(1998, 1, 1), draft_year=None, draft_pick_number=None,
        years_pro=5, team_id=None, roster_status=RosterStatus.ACTIVE, overall_cached=overall,
        overall_computed_at=_NOW, retired_in_season=None, created_at=_NOW, updated_at=_NOW,
    )


def _pick(pick_id: int, *, season: int, rnd: int, original_team_id: int) -> DraftPick:
    return DraftPick(
        id=pick_id, league_id=1, season=season, round=rnd, original_team_id=original_team_id,
        current_team_id=original_team_id, pick_number=None, used_for_player_id=None,
        protection_type=ProtectionType.NONE, protection_through=None, swap_with_pick_id=None,
        conveyed=False, rolls_over_to_season=None,
    )


def _offer(offer_id: int, *, from_team_id: int = 1, to_team_id: int = 2, **overrides) -> TradeOffer:
    fields: dict = dict(
        id=offer_id, league_id=1, season=2026, phase=Phase.TRADE_DEADLINE,
        from_team_id=from_team_id, to_team_id=to_team_id, proposed_by=None,
        status=TradeStatus.PROPOSED,
        package={
            "players_from_to": (11, 12), "players_to_from": (21,),
            "picks_from_to": (31,), "picks_to_from": (),
        },
        created_at=_NOW,
    )
    fields.update(overrides)
    return TradeOffer(**fields)


def _names() -> OfferNames:
    teams = {
        1: _team(1, "BOS", "Boston", "Celtics"),
        2: _team(2, "NYK", "New York", "Knicks"),
    }
    players = {
        11: _player(11, "Marcus", "Webb", overall=74, position=Position.SF),
        12: _player(12, "Jalen", "Hart", overall=69, position=Position.PG),
        21: _player(21, "Devin", "Ortiz", overall=81, position=Position.C),
    }
    picks = {31: _pick(31, season=2027, rnd=1, original_team_id=1)}
    return OfferNames(teams=teams, players=players, picks=picks)


# --- goldens ---------------------------------------------------------------


def test_offer_panel_ai_proposed_matches_committed_golden():
    panel = build_offer_panel(_offer(7), _names())
    assert panel_to_dict(panel) == load_golden("presentation_offer.json")


def test_offer_panel_human_counter_matches_committed_golden():
    offer = _offer(9, proposed_by=555, counters_offer_id=7, status=TradeStatus.ACCEPTED)
    assert panel_to_dict(build_offer_panel(offer, _names())) == load_golden(
        "presentation_offer_counter_accepted.json"
    )


def test_offers_inbox_panel_matches_committed_golden():
    received = (_offer(7), _offer(8, proposed_by=555))
    sent = (
        _offer(
            10, from_team_id=2, to_team_id=1, proposed_by=777,
            package={
                "players_from_to": (21,), "players_to_from": (11,),
                "picks_from_to": (), "picks_to_from": (31,),
            },
        ),
    )
    panel = build_offers_panel(team=_names().teams[2], received=received, sent=sent, names=_names())
    assert panel_to_dict(panel) == load_golden("presentation_offers_inbox.json")


def test_empty_inbox_matches_committed_golden():
    panel = build_offers_panel(team=_names().teams[2], received=(), sent=(), names=_names())
    assert panel_to_dict(panel) == load_golden("presentation_offers_empty.json")


# --- content rules ---------------------------------------------------------


def test_offer_panel_puts_each_direction_under_the_team_that_sends_it():
    """`package`'s `*_from_to` keys are what the PROPOSER sends. Swapping the two
    fields would tell a manager they are receiving what they are giving up."""
    panel = build_offer_panel(_offer(7), _names())
    by_name = {f.name: f.value for f in panel.fields}
    assert "Marcus Webb" in by_name["BOS sends"]
    assert "Devin Ortiz" in by_name["NYK sends"]
    assert "Devin Ortiz" not in by_name["BOS sends"]


def test_inbox_phrases_a_received_and_a_sent_offer_from_the_readers_side():
    sent = _offer(10, from_team_id=2, to_team_id=1, proposed_by=777,
                  package={"players_from_to": (21,), "players_to_from": (11,),
                           "picks_from_to": (), "picks_to_from": ()})
    panel = build_offers_panel(
        team=_names().teams[2], received=(_offer(7),), sent=(sent,), names=_names()
    )
    received_field, sent_field = panel.fields
    assert received_field.name == "Received #7 from BOS"
    assert "You get: Marcus Webb" in received_field.value
    assert "You send: Devin Ortiz" in received_field.value
    assert sent_field.name == "Sent #10 to BOS"
    assert "You send: Devin Ortiz" in sent_field.value
    assert "You get: Marcus Webb" in sent_field.value


def test_human_proposer_is_a_mention_and_ai_proposer_is_named():
    human = build_offer_panel(_offer(1, proposed_by=555), _names())
    ai = build_offer_panel(_offer(2, proposed_by=None), _names())
    assert "<@555>" in human.description
    assert "AI GM" in ai.description and "<@" not in ai.description


def test_an_unresolvable_id_renders_as_an_id_never_raises():
    """A deleted player must not make the recovery-path inbox unrenderable."""
    names = OfferNames(teams={}, players={}, picks={})
    panel = build_offer_panel(_offer(7), names)
    text = " ".join(f.value for f in panel.fields) + panel.title
    assert "player #11" in text and "pick #31" in text and "team #1" in text
    inbox = build_offers_panel(
        team=_team(2, "NYK", "New York", "Knicks"), received=(_offer(7),), sent=(), names=names
    )
    assert "player #11" in inbox.fields[0].value


def test_a_pending_offer_says_how_to_answer_and_a_resolved_one_does_not():
    assert "/offer-respond offer_id:7" in build_offer_panel(_offer(7), _names()).footer
    resolved = build_offer_panel(_offer(7, status=TradeStatus.REJECTED), _names())
    assert resolved.footer == ""
    assert "Status: rejected." in resolved.description


def test_the_panel_says_the_offer_expires_when_the_league_advances():
    """§13.2 -- the rule has to be stated on the panel, not only in the design."""
    assert "expires when the league moves to a new phase" in build_offer_panel(
        _offer(7), _names()
    ).description


def test_notification_text_mentions_the_manager_and_names_the_sender():
    text = build_offer_notification_text(_offer(7), manager_user_id=42, names=_names())
    assert text.startswith("<@42>")
    assert "Boston Celtics" in text and "#7" in text
    assert len(text) <= 2000


def test_counter_pointer_names_the_command_and_the_offer_id():
    text = build_counter_pointer_text(31)
    assert "/counter-offer offer_id:31" in text
    assert "team code" not in text.lower()


def test_expiry_suffix_names_the_recovery_command():
    assert "/offers" in EXPIRY_SUFFIX


# --- result messages -------------------------------------------------------


@pytest.mark.parametrize("status", list(TradeStatus))
def test_not_changed_never_reads_as_success(status):
    """`changed=False` means someone else answered first; a message that said
    "accepted" here would tell a manager a trade happened that they did not
    cause."""
    text = describe_offer_result(_offer(7, status=status), changed=False, detail=None,
                                 action="accept")
    assert text == f"Offer #7 was already {status.value} -- nothing was changed."


def test_result_messages_for_each_outcome():
    def msg(status, *, action="accept", detail=None, **kw):
        return describe_offer_result(
            _offer(7, status=status, **kw), changed=True, detail=detail, action=action,
            to_label="NYK",
        )

    assert msg(TradeStatus.ACCEPTED) == "Offer #7 accepted -- the trade is done."
    assert msg(TradeStatus.REJECTED, action="reject") == "Offer #7 rejected."
    ai_no = msg(
        TradeStatus.REJECTED, action="propose", detail="NYK is not interested in that deal."
    )
    assert ai_no == "Offer #7 was rejected: NYK is not interested in that deal."
    assert msg(TradeStatus.VETOED, action="veto") == "Offer #7 vetoed."
    assert msg(TradeStatus.PROPOSED, action="propose") == (
        "Offer #7 sent; it is waiting for NYK's manager."
    )


def test_an_expired_result_carries_the_reason_and_the_illegal_token():
    text = describe_offer_result(
        _offer(7, status=TradeStatus.EXPIRED, illegal_token="illegal:roster_max"),
        changed=True, detail="The league's rules changed under this deal.", action="accept",
    )
    assert "expired -- nothing was traded" in text
    assert "The league's rules changed under this deal." in text
    assert "(illegal:roster_max)" in text


def test_counter_result_distinguishes_the_new_row_from_a_stale_original():
    made = describe_offer_result(
        _offer(12, counters_offer_id=7), changed=True, detail=None, action="counter",
        to_label="BOS",
    )
    assert made == "Countered offer #7 with offer #12; it is waiting for BOS's manager."
    stale = describe_offer_result(
        _offer(7, status=TradeStatus.EXPIRED), changed=True,
        detail="That offer was made during trade_deadline; the league has moved on.",
        action="counter",
    )
    assert stale.startswith("Offer #7 has expired")


# --- Discord limits --------------------------------------------------------


def _assert_within_limits(panel: Panel) -> None:
    assert len(panel.fields) <= MAX_FIELDS
    for f in panel.fields:
        assert 1 <= len(f.name) <= MAX_FIELD_NAME_CHARS
        assert 1 <= len(f.value) <= MAX_FIELD_VALUE_CHARS
    total = (
        len(panel.title) + len(panel.description) + len(panel.footer) + len(panel.author)
        + sum(len(f.name) + len(f.value) for f in panel.fields)
    )
    assert total <= MAX_TOTAL_CHARS


def _max_names(asset_count: int) -> OfferNames:
    teams = {
        1: _team(1, "T" * 40, "C" * 60, "N" * 60),
        2: _team(2, "U" * 40, "D" * 60, "M" * 60),
    }
    players = {
        i: _player(i, "F" * 60, "L" * 60, overall=99) for i in range(1, asset_count + 1)
    }
    picks = {
        1000 + i: _pick(1000 + i, season=2099, rnd=2, original_team_id=1)
        for i in range(asset_count)
    }
    return OfferNames(teams=teams, players=players, picks=picks)


def test_offer_panel_with_max_length_names_and_far_more_assets_than_a_human_can_send():
    count = 40
    package = {
        "players_from_to": tuple(range(1, count + 1)),
        "players_to_from": tuple(range(1, count + 1)),
        "picks_from_to": tuple(1000 + i for i in range(count)),
        "picks_to_from": tuple(1000 + i for i in range(count)),
    }
    offer = _offer(
        123456789, package=package, proposed_by=None, counters_offer_id=123456788,
        status=TradeStatus.EXPIRED,
    )
    panel = build_offer_panel(offer, _max_names(count))
    _assert_within_limits(panel)
    assert "and " in panel.fields[0].value and "more" in panel.fields[0].value


def test_inbox_with_max_length_names_stays_within_limits_and_lists_overflow_ids():
    count = 5
    names = _max_names(count)
    package = {
        "players_from_to": tuple(range(1, count + 1)),
        "players_to_from": tuple(range(1, count + 1)),
        "picks_from_to": tuple(1000 + i for i in range(2)),
        "picks_to_from": tuple(1000 + i for i in range(2)),
    }
    received = tuple(_offer(1000 + i, package=package, proposed_by=999) for i in range(40))
    sent = tuple(
        _offer(2000 + i, from_team_id=2, to_team_id=1, package=package, proposed_by=999)
        for i in range(40)
    )
    panel = build_offers_panel(team=names.teams[2], received=received, sent=sent, names=names)
    _assert_within_limits(panel)
    shown = [f for f in panel.fields if f.name.startswith(("Received", "Sent"))]
    overflow = [f for f in panel.fields if f.name == "Also pending (ids only)"]
    assert len(overflow) == 1
    listed = overflow[0].value
    # Every offer is either a field or an id in the overflow field -- none is
    # silently dropped, because a human needs the id to answer it.
    all_ids = {o.id for o in received + sent}
    shown_ids = {int(f.name.split("#")[1].split()[0]) for f in shown}
    listed_ids = {int(tok.strip("#… ,")) for tok in listed.split(", ") if tok.strip("#… ,")}
    assert shown_ids | listed_ids == all_ids
    assert shown_ids.isdisjoint(listed_ids)


def test_inbox_overflow_ids_are_clipped_to_the_field_limit():
    """More offers than even their bare ids fit in one field: the panel must
    still construct (it raises otherwise), ending the list with an ellipsis."""
    names = OfferNames(teams={1: _team(1, "BOS", "B", "C"), 2: _team(2, "NYK", "N", "K")},
                       players={}, picks={})
    received = tuple(_offer(100_000_000 + i, proposed_by=1) for i in range(400))
    panel = build_offers_panel(team=names.teams[2], received=received, sent=(), names=names)
    _assert_within_limits(panel)
    assert panel.fields[-1].value.endswith("…")


def test_inbox_at_the_field_count_boundary_never_exceeds_25_fields():
    names = _names()
    received = tuple(_offer(i, proposed_by=1) for i in range(1, 60))
    panel = build_offers_panel(team=names.teams[2], received=received, sent=(), names=names)
    _assert_within_limits(panel)
    assert len(panel.fields) <= MAX_FIELDS
