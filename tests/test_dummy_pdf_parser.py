from datetime import date
from decimal import Decimal

import pytest

from statement_normalizer.models.schemas import Direction
from statement_normalizer.parsers import DummyBankPdfParser, StatementFile, StatementParseError

parser = DummyBankPdfParser()

FIXTURE = "dummy_bank_statement.pdf"


def test_can_parse_accepts_the_pdf_and_rejects_csvs(statement_file):
    assert parser.can_parse(statement_file(FIXTURE)) is True
    assert parser.can_parse(statement_file("dummy_bank_statement.csv")) is False
    assert parser.can_parse(statement_file("revolut_statement.csv")) is False


def test_can_parse_rejects_a_file_that_is_not_a_pdf_without_raising():
    assert parser.can_parse(StatementFile(filename="x.pdf", content=b"not a pdf at all")) is False


def test_direction_comes_from_the_column_not_from_a_sign(statement_file):
    """The whole reason this adapter needs word positions: the amounts carry no
    sign, so which column a number sits in is the only thing that says which way
    the money went."""
    transactions = parser.parse(statement_file(FIXTURE))

    debit = transactions[0]
    assert debit.raw_row["debit"] == "128.90"  # no minus anywhere in the source
    assert debit.raw_row["credit"] is None
    assert debit.direction is Direction.DEBIT
    assert debit.signed_amount == Decimal("-128.90")

    credit = transactions[1]
    assert credit.raw_row["credit"] == "64.99"
    assert credit.direction is Direction.CREDIT


def test_parse_reads_every_transaction_across_both_pages(statement_file):
    transactions = parser.parse(statement_file(FIXTURE))

    assert len(transactions) == 7
    assert [t.date for t in transactions] == [
        date(2026, 1, 7),
        date(2026, 1, 9),
        date(2026, 1, 13),
        date(2026, 1, 17),
        date(2026, 1, 22),  # page 2 starts here
        date(2026, 1, 28),
        date(2026, 1, 31),
    ]

    first = transactions[0]
    assert first.description == "CARD PAYMENT TO UTILITIES CO"
    assert first.amount == Decimal("128.90")
    assert first.balance_after == Decimal("3279.63")
    assert first.currency == "GBP"  # from "All amounts in GBP" in the masthead
    assert first.source_institution == "dummy_bank"


def test_a_wrapped_narrative_is_joined_onto_its_transaction(statement_file):
    wrapped = parser.parse(statement_file(FIXTURE))[2]

    assert wrapped.description == "DIRECT DEBIT COUNCIL TAX MONTHLY INSTALMENT REF 88213"
    assert wrapped.amount == Decimal("189.00")
    # The reference number sits in the description column and looks numeric; it
    # must not have been read as an amount.
    assert wrapped.raw_row["debit"] == "189.00"


def test_summary_and_footer_lines_are_not_transactions(statement_file):
    transactions = parser.parse(statement_file(FIXTURE))
    descriptions = [t.description for t in transactions]

    # "Balance brought forward" and "Closing balance" have a balance but no date.
    assert not any("brought forward" in d for d in descriptions)
    assert not any("Closing balance" in d for d in descriptions)
    # The page footer sits inside the description column, far below the last row
    # on the page — the vertical gap is what stops it being read as a wrap.
    assert transactions[3].description == "SUPERMARKET GROCERIES"
    assert not any("Page" in d for d in descriptions)


def test_extract_account_ref_reads_the_masthead(statement_file):
    assert parser.extract_account_ref(statement_file(FIXTURE)) == "GB00DUMY12345678"


def test_a_statement_that_does_not_state_its_currency_is_an_error(pdf_generator):
    pages = pdf_generator.build_pages()
    pages[0].ops = [op for op in pages[0].ops if "All amounts in" not in op]
    file = StatementFile(filename="no_currency.pdf", content=pdf_generator.render(pages))

    with pytest.raises(StatementParseError) as exc:
        parser.parse(file)

    # Defaulting to GBP would attach an invented fact to real money.
    assert "currency" in str(exc.value)


def test_a_row_in_two_money_columns_is_an_error(pdf_generator):
    """A line read as both a debit and a credit means the geometry was misread.
    Picking a side would put real money on the wrong one."""
    pages = pdf_generator.build_pages()
    first_row_top = 212  # the 07 Jan line; see `_header` and `build_pages`
    pages[0].right(pdf_generator.X_CREDIT_RIGHT, first_row_top, "5.00")
    file = StatementFile(filename="two_columns.pdf", content=pdf_generator.render(pages))

    with pytest.raises(StatementParseError) as exc:
        parser.parse(file)

    assert "both a debit and a credit" in str(exc.value)
    assert "row 1" in str(exc.value)
