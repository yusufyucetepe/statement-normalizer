"""The Ziraat Bankası CSV adapter. No database required."""

import re
from datetime import date
from decimal import Decimal

import pytest

from statement_normalizer.models.schemas import Direction
from statement_normalizer.parsers import StatementParseError, ZiraatCsvParser

parser = ZiraatCsvParser()


def test_can_parse_accepts_the_export_and_rejects_the_others(statement_file):
    assert parser.can_parse(statement_file("ziraat_statement.csv")) is True
    assert parser.can_parse(statement_file("revolut_statement.csv")) is False
    assert parser.can_parse(statement_file("wise_statement.csv")) is False
    assert parser.can_parse(statement_file("dummy_bank_statement.csv")) is False
    assert parser.can_parse(statement_file("unknown_institution.csv")) is False


def test_detection_needs_the_bank_and_not_only_the_columns(statement_file):
    """`Tarih`, `Açıklama` and `Bakiye` are what every Turkish bank calls its
    date, description and balance columns. Matching on those alone would file a
    competitor's export under this institution, so the IBAN's bank code — the
    one field in the document that identifies the bank structurally — has to
    agree as well."""
    file = statement_file("ziraat_statement.csv")
    assert parser.can_parse(file) is True

    another_bank = file.text.replace("TR330001000000123456789012", "TR330006200000123456789012")
    assert "Tarih,Fiş No" in another_bank  # the columns are untouched
    assert parser.can_parse(_as_file(another_bank)) is False


def test_the_header_is_found_below_the_account_block(statement_file):
    """The table starts on line 6. Every row is padded to five fields, so the
    account block above it is not distinguishable by width — an adapter that
    assumed line 1 would read the greeting as its column names."""
    file = statement_file("ziraat_statement.csv")
    first_line = file.text.splitlines()[0]

    assert first_line.startswith("Sayın")
    assert first_line.count(",") == 4  # as wide as a transaction row
    assert len(parser.parse(file)) == 7


def test_rows_are_returned_oldest_first_though_the_file_is_newest_first(statement_file):
    """The decision most worth disagreeing with, and the reason it is taken:
    `Bakiye` is a running balance, and a running balance only runs one way."""
    file = statement_file("ziraat_statement.csv")
    transactions = parser.parse(file)

    assert [t.date for t in transactions] == sorted(t.date for t in transactions)
    # The file really is the other way round.
    rows = [line for line in file.text.splitlines() if re.match(r"^\d{2}\.\d{2}\.\d{4},", line)]
    assert rows[0].startswith("29.08.2026")
    assert rows[-1].startswith("07.08.2026")
    assert transactions[0].date == date(2026, 8, 7)


def test_the_running_balance_reconciles_against_the_amount_alone(statement_file):
    """The evidence for synthesizing nothing: if a charge were missing from
    `İşlem Tutarı`, the balances would not chain once put in order."""
    transactions = parser.parse(statement_file("ziraat_statement.csv"))

    for previous, current in zip(transactions, transactions[1:], strict=False):
        assert previous.balance_after + current.signed_amount == current.balance_after


def test_a_charge_is_never_synthesized(statement_file):
    """Wise's rule, not Revolut's. The export emits `KOMİSYON`, `BSMV` and the
    message fee as rows of their own, sharing the transfer's `Fiş No`. Seven
    source rows must produce exactly seven transactions."""
    transactions = parser.parse(statement_file("ziraat_statement.csv"))

    assert len(transactions) == 7
    charges = [t for t in transactions if t.description in {"KOMİSYON", "BSMV"}]
    assert [t.amount for t in charges] == [Decimal("7.62"), Decimal("0.38")]
    assert all(t.direction is Direction.DEBIT for t in charges)


def test_the_fis_no_is_a_group_not_a_row(statement_file):
    """A transfer and the three charges it caused share one id, which is why
    identity keeps the date, amount and currency beside it."""
    transactions = parser.parse(statement_file("ziraat_statement.csv"))
    group = [t for t in transactions if t.external_id == "F10003"]

    assert len(group) == 4
    assert len({t.amount for t in group}) == 4


def test_dates_are_day_first(statement_file):
    """`04.09.2026` is 4 September. Month-first it becomes 9 April — a wrong
    date, not an error."""
    transactions = parser.parse(statement_file("ziraat_overlap.csv"))

    assert transactions[-1].date == date(2026, 9, 4)
    assert transactions[0].date == date(2026, 8, 21)


def test_the_sign_in_the_amount_becomes_the_direction(statement_file):
    transactions = parser.parse(statement_file("ziraat_statement.csv"))
    salary, card = transactions[0], transactions[1]

    assert (salary.direction, salary.amount) == (Direction.CREDIT, Decimal("5000"))
    assert (card.direction, card.amount) == (Direction.DEBIT, Decimal("250.50"))
    assert card.signed_amount == Decimal("-250.50")
    assert all(t.amount >= 0 for t in transactions)


def test_amounts_survive_having_no_fixed_decimal_places(statement_file):
    """This export writes 5,000.00 as `5,000` and 3.20 as `-3.2`. A rule
    expecting two decimals reads a column of money as text."""
    transactions = parser.parse(statement_file("ziraat_statement.csv"))

    assert transactions[0].amount == Decimal("5000")
    assert transactions[-1].amount == Decimal("3.2")
    assert transactions[-1].balance_after == Decimal("3737.93")


def test_the_currency_is_read_once_for_the_document_and_mapped_to_iso(statement_file):
    """There is no currency column: one export is one account. `TL` is the local
    abbreviation and `Transaction.currency` requires ISO 4217, which is `TRY`."""
    transactions = parser.parse(statement_file("ziraat_statement.csv"))

    assert {t.currency for t in transactions} == {"TRY"}


def test_a_statement_that_states_no_currency_raises(statement_file):
    """Defaulting to TRY would mislabel every row of a foreign-currency account
    and raise nothing anywhere."""
    file = statement_file("ziraat_statement.csv")
    without = file.text.replace("ANKARA/KIZILAY ŞUBESİ TL", "ANKARA/KIZILAY ŞUBESİ")

    with pytest.raises(StatementParseError, match="currency"):
        parser.parse(_as_file(without))


def test_the_totals_line_and_the_footer_are_not_transactions(statement_file):
    """Both sit below the header and are padded to full width. Worse, the totals
    line writes `Borç:-2.262,07` in the European convention while every row
    above it writes `-2,262.07`; anything that read it would be out by a
    thousand and raise nothing."""
    file = statement_file("ziraat_statement.csv")
    transactions = parser.parse(file)

    assert "Borç:-2.262,07" in file.text
    assert "www.ziraatbank.com.tr" in file.text
    assert len(transactions) == 7
    assert all(t.description not in {"", "www.ziraatbank.com.tr"} for t in transactions)


def test_account_ref_is_the_iban_not_the_account_number(statement_file):
    """The account number identifies the account only inside the bank."""
    file = statement_file("ziraat_statement.csv")

    assert parser.extract_account_ref(file) == "TR330001000000123456789012"
    assert "123-456789012-5001" in file.text


def test_the_raw_row_is_kept(statement_file):
    transactions = parser.parse(statement_file("ziraat_statement.csv"))

    assert transactions[0].raw_row["fis_no"] == "F10001"
    assert transactions[0].raw_row["bakiye"] == "6,000.00"


def test_a_malformed_amount_names_the_row_and_the_institution(statement_file):
    with pytest.raises(StatementParseError) as excinfo:
        parser.parse(statement_file("ziraat_malformed.csv"))

    message = str(excinfo.value)
    assert "row 7" in message
    assert "ziraat" in message


def _as_file(text: str):
    from statement_normalizer.parsers import StatementFile

    return StatementFile(filename="ziraat_statement.csv", content=text.encode())
