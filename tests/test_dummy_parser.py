from datetime import date
from decimal import Decimal

import pytest

from statement_normalizer.models.schemas import Direction
from statement_normalizer.parsers import DummyBankCsvParser, StatementParseError

parser = DummyBankCsvParser()


def test_can_parse_accepts_its_own_header_and_rejects_others(statement_file):
    assert parser.can_parse(statement_file("dummy_bank_statement.csv")) is True
    assert parser.can_parse(statement_file("unknown_institution.csv")) is False


def test_parse_normalizes_every_row(statement_file):
    transactions = parser.parse(statement_file("dummy_bank_statement.csv"))

    assert len(transactions) == 4  # the blank line is skipped

    credit = transactions[0]
    assert credit.date == date(2026, 1, 3)
    assert credit.description == "ACME PAYROLL JAN"  # whitespace collapsed
    assert credit.amount == Decimal("2500.00")  # thousands separator stripped
    assert credit.direction is Direction.CREDIT
    assert credit.signed_amount == Decimal("2500.00")
    assert credit.balance_after == Decimal("3412.88")
    assert credit.source_institution == "dummy_bank"
    assert credit.raw_row["narrative"] == "ACME PAYROLL   JAN"  # source kept verbatim

    debit = transactions[1]
    assert debit.direction is Direction.DEBIT
    assert debit.amount == Decimal("4.35")  # sign lives in `direction`, not `amount`
    assert debit.signed_amount == Decimal("-4.35")

    assert transactions[2].currency == "GBP"  # lowercase input normalized


def test_a_claimed_but_malformed_file_reports_the_row(statement_file):
    with pytest.raises(StatementParseError) as exc:
        parser.parse(statement_file("dummy_bank_malformed.csv"))

    assert "row 3" in str(exc.value)
    assert "dummy_bank" in str(exc.value)
