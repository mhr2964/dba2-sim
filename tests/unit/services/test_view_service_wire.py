"""`view_service._transaction_item`'s summary line, over the whole
`TransactionType` vocabulary -- Decision P5-98.e's rendering half.

Pure: `_transaction_item` maps one already-read `LeagueTransaction` to one
`TransactionFeedItem` and touches no database, so the table below is the
complete input space for the part of `/wire` this test owns.

**Parametrized over `TransactionType` itself, not over a hand-listed set**,
because the enum is an open vocabulary by design (`domain/transaction.py`:
a new `txn_type` costs one value and one writer, never a migration). The
day a fourth member lands, this test covers it without being edited -- and
if that member is two words, it fails unless the reader was fixed, which is
exactly the defect `DRAFT_PICK` surfaced ("Draft_Pick #12").
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dba2.domain.phase import Phase
from dba2.domain.transaction import LeagueTransaction, TransactionType
from dba2.services import view_service


def _txn(txn_type: TransactionType, *, note: str | None = None) -> LeagueTransaction:
    return LeagueTransaction(
        id=12,
        league_id=1,
        season=2026,
        phase=Phase.DRAFT,
        txn_type=txn_type,
        actor_user_id=7,
        source_offer_id=None,
        note=note,
        details={},
        created_at=datetime(2026, 6, 25, 18, 0, tzinfo=UTC),
    )


@pytest.mark.parametrize("txn_type", list(TransactionType), ids=lambda t: t.value)
def test_transaction_summary_never_shows_a_storage_underscore(txn_type):
    """The storage form is `draft_pick`; the thing a person reads is "Draft
    Pick". No member of this vocabulary may reach a Discord panel with an
    underscore still in it."""
    item = view_service._transaction_item(_txn(txn_type))
    assert "_" not in item.summary, (
        f"{txn_type.value} renders as {item.summary!r} -- the underscore is the "
        "storage form, not a word"
    )
    assert item.kind == txn_type.value, "the machine-readable kind keeps the raw value"


@pytest.mark.parametrize(
    ("txn_type", "expected"),
    [
        (TransactionType.TRADE, "Trade #12"),
        (TransactionType.TIMELINE_SET, "Timeline Set #12"),
        (TransactionType.DRAFT_PICK, "Draft Pick #12"),
    ],
    ids=["trade", "timeline_set", "draft_pick"],
)
def test_transaction_summary_reads_as_words(txn_type, expected):
    """Hand-written literals, one per member that exists today. The
    `TIMELINE_SET` line is a CHANGED string: it rendered "Timeline_Set"
    before this commit, a pre-existing defect `DRAFT_PICK` happened to
    surface and this fixes rather than matches."""
    assert view_service._transaction_item(_txn(txn_type)).summary == expected


def test_a_note_is_appended_after_the_type_and_id():
    """The note half of the summary, unchanged by the rendering fix -- it is
    asserted here so a future edit to the f-string cannot quietly drop it."""
    item = view_service._transaction_item(_txn(TransactionType.DRAFT_PICK, note="TOR take Smith"))
    assert item.summary == "Draft Pick #12: TOR take Smith"
