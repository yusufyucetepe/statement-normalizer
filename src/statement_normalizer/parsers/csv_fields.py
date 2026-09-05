"""Field-level helpers shared by CSV adapters.

Institutions disagree about thousands separators, parenthesized negatives and
date layouts, but they agree on one thing: a bad cell has to surface as a
`StatementParseError` naming the row and the column. Leaving that to each
adapter means every new one re-derives the same error handling and one of them
eventually lets a bare `ValueError` escape as a 500.
"""

from __future__ import annotations

import csv
import io
import unicodedata
from collections.abc import Iterator
from datetime import date as Date
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from statement_normalizer.parsers.exceptions import StatementParseError

if TYPE_CHECKING:
    from statement_normalizer.parsers.base import StatementFile


def dict_rows(file: StatementFile, *, institution: str) -> Iterator[tuple[int, dict[str, str]]]:
    """Non-empty data rows keyed by normalized header, with 1-based line numbers.

    The line number is why this returns tuples rather than plain dicts: every
    error an adapter raises has to name the row a human can go and look at, and
    `enumerate` starts at 2 because row 1 is the header.

    Blank lines are skipped rather than parsed. Institutions pad exports with
    them, and a row of empty strings would otherwise fail as a missing amount.
    """
    reader = csv.DictReader(io.StringIO(file.text))
    if reader.fieldnames is None:
        raise StatementParseError(institution, "file is empty")
    reader.fieldnames = [normalize_header(name) for name in reader.fieldnames]
    for row_number, row in enumerate(reader, start=2):
        if any((value or "").strip() for value in row.values()):
            yield row_number, row


def table_rows(file: StatementFile, *, institution: str) -> list[tuple[int, list[str]]]:
    """Non-empty rows as plain cell lists, with the line each ended on.

    The sibling of `dict_rows`, for exports whose table does not start at line 1:
    a bank that opens with the account holder, the account number and the
    statement period has no header for `csv.DictReader` to find, and an adapter
    has to locate it before rows mean anything. Returning a list rather than an
    iterator is what lets an adapter look at the block above its header, which is
    where such a file puts the account and the currency.

    `dict_rows` is left to its own reader rather than being rebuilt on this one:
    it numbers rows as `DictReader` sees them, which skips blank lines, and the
    error messages of two shipped adapters are pinned to that numbering.
    """
    reader = csv.reader(io.StringIO(file.text))
    rows = [(reader.line_num, row) for row in reader if any((cell or "").strip() for cell in row)]
    if not rows:
        raise StatementParseError(institution, "file is empty")
    return rows


def normalize_header(name: str) -> str:
    """`"Completed Date"` -> `"completed_date"`, so lookups survive cosmetic drift."""
    return name.strip().lower().replace(" ", "_")


def fold_header(name: str) -> str:
    """`"İşlem Tutarı"` -> `"islem_tutari"`: `normalize_header` plus its accents.

    Lowercasing alone is not enough once a header leaves ASCII, and the way it
    fails is invisible. Turkish `İ` lowercases to `i` followed by a *combining
    dot above*, so the obvious `"i̇şlem_tutarı"` written in a source file is one
    codepoint away from the key the file actually produces, and the two look
    identical in every editor. Folding to ASCII makes the column constants
    greppable and typeable, which is the only way they can be reviewed.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    # `ı` (dotless i) has no decomposition to strip, so it is mapped by hand.
    return normalize_header(stripped.replace("ı", "i").replace("I", "i"))


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
