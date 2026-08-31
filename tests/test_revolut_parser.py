from datetime import date
from decimal import Decimal

import pytest

from statement_normalizer.models.schemas import Direction
from statement_normalizer.parsers import RevolutCsvParser, StatementFile, StatementParseError

parser = RevolutCsvParser()

FIXTURE = "revolut_statement.csv"


def test_can_parse_accepts_its_own_layout_and_rejects_others(statement_file):
    assert parser.can_parse(statement_file(FIXTURE)) is True
    assert parser.can_parse(statement_file("dummy_bank_statement.csv")) is False
    assert parser.can_parse(statement_file("unknown_institution.csv")) is False


def test_can_parse_tolerates_a_column_being_added(statement_file):
    """Detection is a required subset, not equality: this export is not ours to
    freeze, and a new column must not turn every upload into a 422."""
    lines = [line for line in statement_file(FIXTURE).text.splitlines() if line.strip()]
    widened = "\n".join(
        f"{line},{'Balance Reference' if index == 0 else 'REF-000'}"
        for index, line in enumerate(lines)
    )

    file = StatementFile(filename="revolut.csv", content=widened.encode("utf-8"))

    assert parser.can_parse(file) is True
    assert len(parser.parse(file)) == 8


def test_can_parse_rejects_the_crypto_export_that_contains_the_same_columns(statement_file):
    """Revolut's crypto/trading export is this header plus four columns, so a
    plain subset match claims it — and its `Amount` and `Balance` are asset
    quantities, its `Currency` a ticker. `EOS` passes currency validation, so
    parsing it would file 100 EOS as 100 units of money and nothing would flag
    it. A 422 is the right answer; a wrong number is not."""
    file = statement_file("revolut_crypto.csv")

    assert parser.can_parse(file) is False
    # The columns it shares are genuinely all of ours: the rejection has to come
    # from what the file adds, not from something it is missing.
    header = {c.strip().lower().replace(" ", "_") for c in file.text.splitlines()[0].split(",")}
    assert header >= parser.REQUIRED_COLUMNS


def test_only_completed_rows_become_transactions(statement_file):
    descriptions = [t.description for t in parser.parse(statement_file(FIXTURE))]

    # A DECLINED payment moved no money; a REVERTED one moved it and moved it back.
    assert "Declined merchant" not in descriptions
    assert "Reverted charge" not in descriptions


def test_parse_normalizes_sign_date_and_currency(statement_file):
    transactions = parser.parse(statement_file(FIXTURE))

    assert len(transactions) == 8  # 6 settled rows, two of which also carry a fee

    debit = transactions[0]
    assert debit.date == date(2026, 2, 1)  # the completion date, not the start
    assert debit.description == "Pret A Manger"
    assert debit.amount == Decimal("3.20")  # sign lives in `direction`
    assert debit.direction is Direction.DEBIT
    assert debit.signed_amount == Decimal("-3.20")
    assert debit.balance_after == Decimal("1246.80")
    assert debit.source_institution == "revolut"
    assert debit.raw_row["type"] == "CARD_PAYMENT"

    credit = transactions[1]
    assert credit.direction is Direction.CREDIT
    assert credit.description == "Payment from Sender, A."  # quoted comma survives

    foreign = transactions[6]
    assert foreign.currency == "EUR"  # currency is per row, not per statement
    assert foreign.description == "Café Möller"


def test_a_fee_becomes_its_own_transaction(statement_file):
    """The Balance column has already subtracted the fee, so the fee has to be
    stored as a movement of its own for totals to reconcile."""
    withdrawal, fee = parser.parse(statement_file(FIXTURE))[2:4]

    assert withdrawal.description == "Cash at ATM"
    assert withdrawal.amount == Decimal("100.00")
    assert withdrawal.balance_after is None  # the intermediate balance is not real

    assert fee.description == "Fee: Cash at ATM"
    assert fee.amount == Decimal("2.00")
    assert fee.direction is Direction.DEBIT
    assert fee.date == withdrawal.date
    assert fee.currency == withdrawal.currency
    assert fee.balance_after == Decimal("1394.80")


def test_extract_account_ref_falls_back_to_the_product(statement_file):
    assert parser.extract_account_ref(statement_file(FIXTURE)) == "Current"


def test_a_claimed_but_malformed_file_reports_the_row(statement_file):
    with pytest.raises(StatementParseError) as exc:
        parser.parse(statement_file("revolut_malformed.csv"))

    assert "row 3" in str(exc.value)
    assert "revolut" in str(exc.value)


def test_a_settled_row_without_a_completion_date_is_an_error():
    header = (
        "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance"
    )
    row = "CARD_PAYMENT,Current,2026-02-01 08:14:21,,No date,-3.20,0.00,GBP,COMPLETED,1.00"
    file = StatementFile(filename="revolut.csv", content=f"{header}\n{row}\n".encode())

    with pytest.raises(StatementParseError) as exc:
        parser.parse(file)

    # Falling back to the start date here would misdate real money.
    assert "completion date" in str(exc.value)
