"""Field-level helpers shared by CSV adapters.

Institutions disagree about thousands separators, parenthesized negatives and
date layouts, but they agree on one thing: a bad cell has to surface as a
`StatementParseError` naming the row and the column. Leaving that to each
adapter means every new one re-derives the same error handling and one of them
eventually lets a bare `ValueError` escape as a 500.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from decimal import Decimal, InvalidOperation

from statement_normalizer.parsers.exceptions import StatementParseError


def normalize_header(name: str) -> str:
    """`"Completed Date"` -> `"completed_date"`, so lookups survive cosmetic drift."""
    return name.strip().lower().replace(" ", "_")


def to_decimal(value: str, *, institution: str, row: int, column: str) -> Decimal:
    """Parse a money cell. Never float: the value goes straight into NUMERIC(20, 4)."""
    cleaned = value.strip().replace(",", "").replace(" ", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):  # (123.45) means negative
        cleaned = "-" + cleaned[1:-1]
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise StatementParseError(
            institution, f"column {column!r} is not a number: {value!r}", row=row
        ) from exc


def to_date(value: str, *, fmt: str, institution: str, row: int, column: str) -> Date:
    """Parse a date cell. `fmt` may include a time component, which is discarded:
    `Transaction.date` is the posting day, and no downstream filter is finer."""
    raw = value.strip()
    try:
        return datetime.strptime(raw, fmt).date()
    except ValueError as exc:
        raise StatementParseError(
            institution, f"column {column!r} is not a date like {fmt!r}: {raw!r}", row=row
        ) from exc
