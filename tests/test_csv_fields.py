"""`to_decimal` and the decimal convention it refuses to guess. No database."""

from decimal import Decimal

import pytest

from statement_normalizer.parsers import DecimalConvention, StatementParseError
from statement_normalizer.parsers.csv_fields import to_decimal

ANGLO = DecimalConvention.ANGLO
EUROPEAN = DecimalConvention.EUROPEAN


def parse(value: str, convention: DecimalConvention) -> Decimal:
    return to_decimal(value, convention=convention, institution="test_bank", row=7, column="amount")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1,234.56", "1234.56"),
        ("5,000", "5000"),  # no decimal places at all: Ziraat writes money this way
        ("-3.2", "-3.2"),  # and one decimal place, on the very next row
        ("1.2345", "1.2345"),  # four places, which NUMERIC(20, 4) still holds exactly
        ("(123.45)", "-123.45"),
        ("1,234,567.89", "1234567.89"),
        (" 42 ", "42"),
    ],
)
def test_anglo_reads_the_conventions_the_shipped_adapters_use(value, expected):
    assert parse(value, ANGLO) == Decimal(expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.204,55", "1204.55"),
        ("-42,90", "-42.90"),
        ("5.000,00", "5000"),
        ("-2.262,07", "-2262.07"),  # verbatim from the Ziraat totals line
        ("1.234.567,89", "1234567.89"),
        ("1 234,56", "1234.56"),  # grouped with a non-breaking space
    ],
)
def test_european_reads_the_mirror_image(value, expected):
    assert parse(value, EUROPEAN) == Decimal(expected)


@pytest.mark.parametrize("value", ["1.204,55", "-42,90"])
def test_a_european_number_declared_anglo_raises_instead_of_answering(value):
    """The bug this whole change exists for. These two used to return 1.20455
    and -4290: a wrong number, out by a factor of a thousand, on a file that
    parsed cleanly and stored without complaint."""
    with pytest.raises(StatementParseError) as excinfo:
        parse(value, ANGLO)

    message = str(excinfo.value)
    assert "row 7" in message
    assert "amount" in message
    assert "test_bank" in message


def test_an_anglo_number_declared_european_raises_too():
    """`2,262.07` read as European puts the decimal separator before a grouping
    separator, which no number does in either convention."""
    with pytest.raises(StatementParseError):
        parse("2,262.07", EUROPEAN)


@pytest.mark.parametrize("convention", [ANGLO, EUROPEAN])
@pytest.mark.parametrize("value", ["not-a-number", "", "   ", "-", "1,23,456", "12.34.56", "TR33"])
def test_neither_convention_accepts_a_group_of_the_wrong_length(value, convention):
    """A separator that is not followed by exactly three digits is the signal
    that the cell is not the kind of number it was declared to be."""
    with pytest.raises(StatementParseError):
        parse(value, convention)


def test_the_convention_has_to_be_passed():
    """No default, because a default is a guess, and a guess here does not fail
    loudly — it returns a plausible number that is wrong."""
    with pytest.raises(TypeError):
        to_decimal("1,234.56", institution="test_bank", row=7, column="amount")
