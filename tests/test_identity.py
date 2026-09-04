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
        ("external_id", "TXN-0001"),
    ],
)
def test_every_identifying_field_changes_the_key(field, value):
    assert key(make()) != key(make(**{field: value}))


def test_the_description_fingerprint_is_frozen():
    """Every `dedupe_key` in the database was built by this exact payload.

    Changing it — a separator, a field order, a casing rule — silently
    invalidates all of them, after which overlapping uploads double-count until
    every statement is re-uploaded. That failure is invisible at runtime: no
    error, just totals that quietly stop matching. So the digest is pinned
    literally rather than recomputed, and this test failing means a migration
    is required, not that the constant needs updating.
    """
    assert key(make()) == "6d76b2f1b9574a050a943426d8392a5a9a043e7f03368c33296745146e359c91"


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


class TestExternalId:
    """Identity when the institution publishes its own transaction id."""

    def test_a_reworded_description_no_longer_changes_the_key(self):
        """The point of the whole thing. Without an id, an institution rewording
        its narrative between exports stores the transaction a second time."""
        original = make(external_id="TXN-0001")
        reworded = make(external_id="TXN-0001", description="Coffee Shop, Camden — card 4471")

        assert key(original) == key(reworded)
        # And the same pair without an id is exactly the bug being fixed.
        assert key(make()) != key(make(description="Coffee Shop, Camden — card 4471"))

    def test_the_id_does_not_replace_the_rest_of_the_fingerprint(self):
        """Wise gives a transfer and the fee charged for it the same id. Keying on
        the id alone would merge the fee into the transfer and lose it."""
        transfer = make(external_id="TRANSFER-9003", amount=Decimal("300.00"))
        fee = make(external_id="TRANSFER-9003", amount=Decimal("2.50"))

        assert key(transfer) != key(fee)
        # They are still recognized as repeats of each other's *id*, not merged:
        # two rows, two keys, both stable across exports.
        assert key(transfer) == key(make(external_id="TRANSFER-9003", amount=Decimal("300.00")))

    def test_an_id_and_a_description_can_never_collide(self):
        """The two payload shapes are marked, so a description that happens to
        read like an id cannot fingerprint as one."""
        assert key(make(description="TXN-0001")) != key(make(external_id="TXN-0001"))

    def test_a_blank_id_is_treated_as_no_id_at_all(self):
        """Otherwise every row at an institution with an empty id column would
        share the 'has an identifier' branch with nothing to tell them apart."""
        assert make(external_id="   ").external_id is None
        assert key(make(external_id="")) == key(make())

    def test_an_id_does_not_lift_the_account_requirement(self):
        """Institutions promise ids are unique within an account, not across
        every account they hold."""
        assert key(make(external_id="TXN-0001"), account_ref=None) is None

    def test_repeats_are_still_numbered(self):
        """Two genuinely identical rows sharing an id are still two transactions."""
        row = make(external_id="TXN-0001")
        keys = assign_dedupe_keys([row, row], ACCOUNT)

        assert keys[0] != keys[1]
        assert keys[0] == key(row, occurrence=0)
