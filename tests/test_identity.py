from datetime import date
from decimal import Decimal

import pytest

from statement_normalizer.models.identity import assign_dedupe_keys, dedupe_key
from statement_normalizer.models.schemas import Direction, Transaction

ACCOUNT = "GB00DUMY12345678"


def make(**overrides) -> Transaction:
    fields = {
        "date": date(2026, 1, 4),
        "description": "COFFEE SHOP #4471",
        "amount": Decimal("4.35"),
        "currency": "GBP",
        "direction": Direction.DEBIT,
        "source_institution": "dummy_bank",
    }
    return Transaction(**{**fields, **overrides})


def key(txn: Transaction, occurrence: int = 0, account_ref: str | None = ACCOUNT) -> str | None:
    return dedupe_key(txn, account_ref=account_ref, occurrence=occurrence)


def test_the_same_transaction_always_produces_the_same_key():
    assert key(make()) == key(make())


@pytest.mark.parametrize(
    "field,value",
    [
        ("date", date(2026, 1, 5)),
        ("description", "COFFEE SHOP #4472"),
        ("amount", Decimal("4.36")),
        ("currency", "EUR"),
        ("direction", Direction.CREDIT),
        ("source_institution", "other_bank"),
    ],
)
def test_every_identifying_field_changes_the_key(field, value):
    assert key(make()) != key(make(**{field: value}))


def test_a_different_account_changes_the_key():
    assert key(make()) != key(make(), account_ref="GB00DUMY99999999")


@pytest.mark.parametrize(
    "field,value",
    [
        ("balance_after", Decimal("3408.53")),
        ("raw_row", {"anything": "at all"}),
    ],
)
def test_non_identifying_fields_do_not_change_the_key(field, value):
    """A running balance differs between statements that start at different
    points in the ledger, and raw_row carries per-export noise."""
    assert key(make()) == key(make(**{field: value}))


def test_decimal_scale_does_not_change_the_key():
    assert key(make(amount=Decimal("10.5"))) == key(make(amount=Decimal("10.50")))


def test_description_case_and_whitespace_do_not_change_the_key():
    assert key(make(description="Coffee   Shop  #4471")) == key(
        make(description="COFFEE SHOP #4471")
    )


def test_no_account_ref_means_no_key():
    """Without an account, two people's identical coffee would collapse into one row."""
    assert key(make(), account_ref=None) is None
    assert key(make(), account_ref="") is None


def test_occurrence_separates_genuinely_repeated_transactions():
    assert key(make(), occurrence=0) != key(make(), occurrence=1)


def test_assign_numbers_repeats_within_one_statement():
    """Two identical coffees on one day are two real transactions, not a duplicate."""
    coffee = make()
    other = make(description="RENT", amount=Decimal("950.00"))
    keys = assign_dedupe_keys([coffee, other, coffee], ACCOUNT)

    assert len(set(keys)) == 3
    assert keys[0] == key(coffee, occurrence=0)
    assert keys[2] == key(coffee, occurrence=1)


def test_assign_matches_the_shared_prefix_of_an_overlapping_statement():
    """A statement holding one extra repeat matches the shared ones and keeps the extra."""
    coffee = make()
    first = assign_dedupe_keys([coffee], ACCOUNT)
    second = assign_dedupe_keys([coffee, coffee], ACCOUNT)

    assert second[0] == first[0]
    assert second[1] not in first


def test_assign_returns_all_none_without_an_account():
    assert assign_dedupe_keys([make(), make()], None) == [None, None]
