"""The Wise CSV adapter. No database required."""

from datetime import date
from decimal import Decimal

import pytest

from statement_normalizer.models.schemas import Direction
from statement_normalizer.parsers import StatementParseError, WiseCsvParser

parser = WiseCsvParser()


def test_can_parse_accepts_the_export_and_rejects_the_others(statement_file):
    assert parser.can_parse(statement_file("wise_statement.csv")) is True
    assert parser.can_parse(statement_file("revolut_statement.csv")) is False
    assert parser.can_parse(statement_file("dummy_bank_statement.csv")) is False
    assert parser.can_parse(statement_file("unknown_institution.csv")) is False


def test_can_parse_accepts_every_vintage_of_the_export(statement_file):
    """Wise has shipped 19-, 20- and 23-column versions of this file. An exact
    header rule would have broken on their release schedule, not on user input."""
    old = statement_file("wise_statement.csv")
    new = statement_file("wise_new_format.csv")

    assert parser.can_parse(old) is True
    assert parser.can_parse(new) is True
    # Genuinely different files, not two spellings of one.
    assert len(old.text.splitlines()[0].split(",")) != len(new.text.splitlines()[0].split(","))


def test_a_fee_never_becomes_a_transaction_of_its_own(statement_file):
    """The decision most able to corrupt totals here, and the one that differs
    from Revolut: Wise has already accounted for the fee by the time the row is
    read — as its own row on a transfer, or folded into `Amount` on a card. The
    file carries `Total fees` of 0.07 and 2.50; synthesizing either would count
    that money twice. Six source rows must produce exactly six transactions."""
    transactions = parser.parse(statement_file("wise_statement.csv"))

    assert len(transactions) == 6
    assert [t.description for t in transactions].count("Fee: Sent money to Jane Doe") == 0
    # The charge Wise itself emitted survives; it is a real row, not a synthetic one.
    charge = transactions[3]
    assert charge.description == "Wise Charges for: TRANSFER-9003 (Sending money)"
    assert charge.amount == Decimal("2.50")
    assert charge.direction is Direction.DEBIT


def test_the_running_balance_reconciles_against_amount_alone(statement_file):
    """The evidence for the decision above, checkable inside the file: if a fee
    were missing from `Amount`, the balances would not chain."""
    transactions = parser.parse(statement_file("wise_statement.csv"))

    for previous, current in zip(transactions, transactions[1:], strict=False):
        assert previous.balance_after + current.signed_amount == current.balance_after


def test_dates_are_day_first(statement_file):
    """`03-04-2026` is 3 April. Read as month-first it silently becomes 4 March —
    a wrong date rather than an error, on every row in the file."""
    transactions = parser.parse(statement_file("wise_new_format.csv"))

    assert transactions[0].date == date(2026, 4, 3)
    assert transactions[1].date == date(2026, 4, 4)


def test_the_sign_in_amount_becomes_the_direction(statement_file):
    transactions = parser.parse(statement_file("wise_statement.csv"))
    topup, card = transactions[0], transactions[1]

    assert (topup.direction, topup.amount) == (Direction.CREDIT, Decimal("500.00"))
    assert (card.direction, card.amount) == (Direction.DEBIT, Decimal("19.72"))
    assert card.signed_amount == Decimal("-19.72")
    assert all(t.amount >= 0 for t in transactions)


def test_the_payment_reference_joins_the_description(statement_file):
    """Wise puts what the sender typed in its own column. Two payments to the
    same payee on the same day are otherwise identical, and identity would
    collapse them into one stored row."""
    transactions = parser.parse(statement_file("wise_statement.csv"))

    assert transactions[2].description == "Sent money to Jane Doe (Rent February)"
    assert transactions[0].description == "Topped up balance"  # no reference, no parentheses


def test_the_transaction_id_is_carried_through(statement_file):
    """Wise is the reason `external_id` exists: it publishes an id, so identity
    can stop leaning on the description. The id is a transaction *group* though —
    the transfer and the fee charged for it share one."""
    transactions = parser.parse(statement_file("wise_statement.csv"))

    assert transactions[0].external_id == "TRANSFER-9001"
    assert transactions[2].external_id == transactions[3].external_id == "TRANSFER-9003"
    assert transactions[2].amount != transactions[3].amount


def test_the_balance_is_carried_and_the_raw_row_kept(statement_file):
    transactions = parser.parse(statement_file("wise_statement.csv"))

    assert transactions[0].balance_after == Decimal("1500.00")
    assert transactions[0].raw_row["transferwise_id"] == "TRANSFER-9001"
    assert transactions[0].raw_row["total_fees"] == "0.00"


def test_account_ref_is_the_currency_not_the_payee_account(statement_file):
    """`Payee Account Number` looks like an account number and belongs to the
    counterparty. Scoping identity by it would file every transaction under
    whoever was paid."""
    file = statement_file("wise_statement.csv")

    assert parser.extract_account_ref(file) == "EUR"
    assert "PL12345678901234567890" in file.text


def test_a_malformed_amount_names_the_row_and_the_institution(statement_file):
    with pytest.raises(StatementParseError) as excinfo:
        parser.parse(statement_file("wise_malformed.csv"))

    message = str(excinfo.value)
    assert "row 3" in message
    assert "wise" in message
