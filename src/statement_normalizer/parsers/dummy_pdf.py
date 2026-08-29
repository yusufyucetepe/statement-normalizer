from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar

from statement_normalizer.models.schemas import Direction, StatementFormat, Transaction
from statement_normalizer.parsers.base import StatementFile, StatementParser, Word
from statement_normalizer.parsers.csv_fields import to_date, to_decimal
from statement_normalizer.parsers.exceptions import StatementParseError
from statement_normalizer.parsers.registry import registry

_ACCOUNT_RE = re.compile(r"Account Number:\s*([A-Z0-9]+)")
_CURRENCY_RE = re.compile(r"All amounts in ([A-Z]{3})")
#: What counts as a money cell. Deliberately strict about the two decimal
#: places, so a reference number inside a narrative is never mistaken for one.
_AMOUNT_RE = re.compile(r"^-?[\d,]+\.\d{2}$")

#: Words within this many points of each other vertically are one line.
_LINE_TOLERANCE = 3.0
#: How far a word's centre may sit from a column's centre and still belong to
#: it. Amounts are right-aligned under a roughly centred header, so the two
#: never coincide exactly.
_COLUMN_TOLERANCE = 25.0
#: A wrapped narrative continues on the very next line. Anything further down
#: the page is a footer or a summary, not part of the transaction above it.
_CONTINUATION_GAP = 20.0


@dataclass(frozen=True)
class _Columns:
    """Where the money columns sit, measured from the statement's own header row."""

    description_left: float
    debit: float
    credit: float
    balance: float

    def amount_column(self, word: Word) -> str | None:
        """Which money column `word` falls in, if any."""
        for name, centre in (
            ("debit", self.debit),
            ("credit", self.credit),
            ("balance", self.balance),
        ):
            if abs(word.center - centre) <= _COLUMN_TOLERANCE:
                return name
        return None


@dataclass
class _Cells:
    """One visual line, sorted into the columns the header defined."""

    top: float
    date: str = ""
    description: list[str] = field(default_factory=list)
    debit: str | None = None
    credit: str | None = None
    balance: str | None = None

    @property
    def has_amount(self) -> bool:
        return any((self.debit, self.credit, self.balance))

    @property
    def narrative(self) -> str:
        return " ".join(self.description)


@registry.register
class DummyBankPdfParser(StatementParser):
    """The `dummy_bank` PDF statement — the same institution, a different layout.

    Registered alongside `DummyBankCsvParser` under one institution because the
    two formats are genuinely unrelated documents; see `ParserRegistry.register`.
    Transactions parsed here carry the same `source_institution` and account, so
    a PDF statement overlapping a CSV one deduplicates against it: identity is a
    property of the transaction, not of the file it arrived in.

    A statement PDF is a table with no table markup. Everything below recovers
    the structure a CSV would have given for free — which is why `pdf_words`
    exposes positions rather than text.
    """

    institution: ClassVar[str] = "dummy_bank"
    supported_formats: ClassVar[frozenset[StatementFormat]] = frozenset({StatementFormat.PDF})
    priority: ClassVar[int] = 100

    MASTHEAD: ClassVar[str] = "DUMMY BANK PLC"
    DATE_FORMAT: ClassVar[str] = "%d %b %Y"
    #: Header words that anchor each column. `Date` and `Description` are the
    #: left-aligned pair; the rest sit over right-aligned amounts.
    HEADERS: ClassVar[tuple[str, ...]] = ("Date", "Description", "Debit", "Credit", "Balance")

    def can_parse(self, file: StatementFile) -> bool:
        if not file.is_pdf:
            return False
        try:
            text = file.pdf_text
        except Exception:
            # Contract: `can_parse` must not raise. An unreadable PDF becomes a
            # detection miss and therefore a 422, which loses some of the
            # message quality a claimed-then-failed parse would have given.
            return False
        return self.MASTHEAD in text and "Statement of Account" in text

    def parse(self, file: StatementFile) -> list[Transaction]:
        currency = self._document_field(file, _CURRENCY_RE, "currency ('All amounts in XXX')")
        rows: list[_Cells] = []
        for page in file.pdf_words:
            rows.extend(self._page_rows(page))
        return [
            self._to_transaction(row, currency, index) for index, row in enumerate(rows, start=1)
        ]

    def extract_account_ref(self, file: StatementFile) -> str | None:
        match = _ACCOUNT_RE.search(file.pdf_text)
        return match.group(1) if match else None

    def _document_field(self, file: StatementFile, pattern: re.Pattern, what: str) -> str:
        match = pattern.search(file.pdf_text)
        if not match:
            # Defaulting here would attach invented facts to real money.
            raise StatementParseError(self.institution, f"statement does not state its {what}")
        return match.group(1)

    def _page_rows(self, words: list[Word]) -> list[_Cells]:
        """Transaction rows on one page, with wrapped narratives folded in."""
        lines = self._lines(words)
        columns, start = self._locate_columns(lines)
        if columns is None:
            return []  # a page with no table on it, e.g. terms and conditions

        rows: list[_Cells] = []
        previous_top: float | None = None
        for line in lines[start:]:
            cells = self._sort_into_columns(line, columns)

            if cells.date:
                rows.append(cells)
                previous_top = cells.top
                continue

            # No date. Either the continuation of the narrative above, or a line
            # that is not a transaction at all.
            is_continuation = (
                rows
                and not cells.has_amount
                and cells.description
                and previous_top is not None
                and cells.top - previous_top <= _CONTINUATION_GAP
            )
            if is_continuation:
                rows[-1].description.extend(cells.description)
                previous_top = cells.top
            # Everything else is dropped: the repeated column header, the
            # "Balance brought forward" and "Closing balance" summaries (no date
            # but an amount), and the page footer (too far below the last row).
        return rows

    def _lines(self, words: list[Word]) -> list[list[Word]]:
        """Group words into visual lines, left to right."""
        lines: list[list[Word]] = []
        for word in sorted(words, key=lambda w: (w.top, w.x0)):
            if lines and abs(word.top - lines[-1][0].top) <= _LINE_TOLERANCE:
                lines[-1].append(word)
            else:
                lines.append([word])
        return [sorted(line, key=lambda w: w.x0) for line in lines]

    def _locate_columns(self, lines: list[list[Word]]) -> tuple[_Columns | None, int]:
        """Read the column geometry off the statement's own header row.

        Hard-coding x positions would make the adapter a hostage to the exact
        page template; the header is the statement telling us where its columns
        are, and it is repeated on every page that carries the table.
        """
        for index, line in enumerate(lines):
            by_text = {word.text: word for word in line}
            if not set(self.HEADERS) <= by_text.keys():
                continue
            return (
                _Columns(
                    description_left=by_text["Description"].x0,
                    debit=by_text["Debit"].center,
                    credit=by_text["Credit"].center,
                    balance=by_text["Balance"].center,
                ),
                index + 1,
            )
        return None, 0

    def _sort_into_columns(self, line: list[Word], columns: _Columns) -> _Cells:
        """Assign each word on a line to a column.

        Money columns are matched on *content and* position: a bare proximity
        test would pull the tail of a long narrative into the Debit column, and
        a reference number in the middle of one into whichever column it drifted
        under.
        """
        cells = _Cells(top=line[0].top)
        for word in line:
            column = columns.amount_column(word) if _AMOUNT_RE.match(word.text) else None
            if column:
                setattr(cells, column, word.text)
            elif word.center < columns.description_left:
                cells.date = f"{cells.date} {word.text}".strip()
            else:
                cells.description.append(word.text)
        return cells

    def _to_transaction(self, row: _Cells, currency: str, index: int) -> Transaction:
        posted = to_date(
            row.date,
            fmt=self.DATE_FORMAT,
            institution=self.institution,
            row=index,
            column="date",
        )
        if bool(row.debit) == bool(row.credit):
            # The columns *are* the sign. Both or neither means the line was
            # misread, and inferring a direction from a misread row would put
            # real money on the wrong side of the ledger.
            side = "both a debit and a credit" if row.debit else "no amount"
            raise StatementParseError(
                self.institution, f"{row.date} {row.narrative!r} has {side}", row=index
            )

        raw = row.debit or row.credit
        amount = to_decimal(raw, institution=self.institution, row=index, column="amount")
        balance = (
            to_decimal(row.balance, institution=self.institution, row=index, column="balance")
            if row.balance
            else None
        )

        try:
            return Transaction(
                date=posted,
                description=row.narrative,
                amount=abs(amount),
                currency=currency,
                direction=Direction.DEBIT if row.debit else Direction.CREDIT,
                balance_after=balance,
                # No verbatim source record exists for a PDF row, so record the
                # cells as read. This is what a re-parse would be checked against.
                raw_row={
                    "date": row.date,
                    "description": row.narrative,
                    "debit": row.debit,
                    "credit": row.credit,
                    "balance": row.balance,
                },
                source_institution=self.institution,
            )
        except ValueError as exc:  # pydantic ValidationError subclasses ValueError
            raise StatementParseError(self.institution, str(exc), row=index) from exc
