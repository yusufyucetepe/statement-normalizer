from __future__ import annotations

import csv
import io
import re
from datetime import date as Date
from itertools import islice
from typing import ClassVar

from statement_normalizer.models.schemas import Direction, StatementFormat, Transaction
from statement_normalizer.parsers.base import StatementFile, StatementParser
from statement_normalizer.parsers.csv_fields import (
    DecimalConvention,
    fold_header,
    table_rows,
    to_date,
    to_decimal,
)
from statement_normalizer.parsers.exceptions import StatementParseError
from statement_normalizer.parsers.registry import registry

#: A Turkish IBAN whose bank code says Ziraat: `TR` + 2 check digits + `00010` +
#: 17 more. Every Turkish IBAN carries the bank in itself, which makes this the
#: one part of the document that cannot drift for cosmetic reasons.
_ZIRAAT_IBAN = re.compile(r"\bTR\d{2}00010\d{17}\b")
#: A transaction row starts with a date in this shape and nothing else does.
_ROW_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
#: How far into the file to look for the header before giving up.
_HEADER_SEARCH_ROWS = 30
#: What the export calls a currency, mapped to what the schema requires. `TL` is
#: the local abbreviation; ISO 4217 for the Turkish lira is `TRY`, and
#: `Transaction.currency` rejects anything that is not three letters.
_CURRENCIES: dict[str, str] = {
    "TL": "TRY",
    "TRY": "TRY",
    "USD": "USD",
    "EUR": "EUR",
    "GBP": "GBP",
    "CHF": "CHF",
}


@registry.register
class ZiraatCsvParser(StatementParser):
    """Ziraat Bankası's `Hesap Hareketleri` CSV export.

    The first adapter written against a file downloaded from a real account
    rather than against a published description of one, and the first whose
    table does not begin at line 1: the export opens with the account holder,
    the statement period, the account number and the IBAN, then a header, then
    the rows, then a totals line and a page of branch boilerplate. Every row is
    padded to the width of the widest, so none of that is distinguishable by
    shape — see `_transactions` for what is.

    Two of its decisions differ from every adapter above, and both are argued
    where they are taken: the rows arrive newest-first (`parse`), and the
    currency is stated once for the whole document rather than per row
    (`_currency`).
    """

    institution: ClassVar[str] = "ziraat"
    supported_formats: ClassVar[frozenset[StatementFormat]] = frozenset({StatementFormat.CSV})
    priority: ClassVar[int] = 100
    decimal_convention: ClassVar[DecimalConvention] = DecimalConvention.ANGLO

    #: Columns this adapter reads, folded to ASCII by `fold_header`. Detection
    #: requires all of them *and* a Ziraat IBAN; see `can_parse`.
    REQUIRED_COLUMNS: ClassVar[frozenset[str]] = frozenset(
        {"tarih", "fis_no", "aciklama", "islem_tutari", "bakiye"}
    )
    DATE_FORMAT: ClassVar[str] = "%d.%m.%Y"

    def can_parse(self, file: StatementFile) -> bool:
        """Require the columns *and* a Ziraat IBAN, which is the specific half.

        The column names are ordinary Turkish banking words — `Tarih`,
        `Açıklama`, `Bakiye` mean date, description and balance at every bank in
        the country — so on their own they would claim a competitor's export and
        file it under this institution. The IBAN's bank code is what makes the
        match specific, and unlike a masthead or a footer URL it cannot be
        dropped from the document without the document ceasing to identify an
        account at all.
        """
        if file.format is not StatementFormat.CSV:
            return False
        if not _ZIRAAT_IBAN.search(file.text):
            return False
        return self._header_index(self._head_rows(file)) is not None

    def parse(self, file: StatementFile) -> list[Transaction]:
        """Parse the table, oldest first.

        **The export is newest-first and this reverses it**, which is the one
        decision here a reader should disagree with if they are going to. Every
        adapter above happens to receive its rows in ledger order and so never
        had to choose; this one does, and normalizing the order is what makes
        `balance_after` mean anything. A running balance is only a running
        balance read forwards — a consumer reconciling one down a descending
        list gets the arithmetic backwards on every pair — and the ordering a
        bank picks for its own screen is a presentation choice, not a fact about
        the account, which makes it exactly the kind of thing to normalize away.

        The cost is that `GET /transactions?statement_id=` no longer shows this
        statement in the order its file lists it, unlike every other institution
        here. Identity is unaffected either way: `dedupe_key` numbers repeated
        transactions within one fingerprint, and rows identical enough to share
        one are identical enough that their order among themselves cannot matter.
        """
        rows = table_rows(file, institution=self.institution)
        header_index = self._header_index(rows)
        if header_index is None:
            raise StatementParseError(
                self.institution, f"no header row with {sorted(self.REQUIRED_COLUMNS)}"
            )
        header = [fold_header(cell) for cell in rows[header_index][1]]
        currency = self._currency(rows[:header_index])

        transactions = [
            self._to_transaction(line, dict(zip(header, cells, strict=False)), currency)
            for line, cells in self._transactions(rows, header_index)
        ]
        transactions.reverse()
        return transactions

    def extract_account_ref(self, file: StatementFile) -> str | None:
        """Use the IBAN, which this export states and which is globally unique.

        The account number beside it — `123-456789012-5001` — identifies the
        account only within the bank, and the branch name beside *that* is not
        an identifier at all. Unlike `revolut|Current` and `wise|EUR`, this one
        stays correct if the service ever grows users.
        """
        match = _ZIRAAT_IBAN.search(file.text)
        return match.group(0) if match else None

    def _head_rows(self, file: StatementFile) -> list[tuple[int, list[str]]]:
        """The first rows only — `can_parse` must stay cheap on a large file."""
        reader = csv.reader(io.StringIO(file.text))
        return [(index, row) for index, row in enumerate(islice(reader, _HEADER_SEARCH_ROWS))]

    def _header_index(self, rows: list[tuple[int, list[str]]]) -> int | None:
        for index, (_, cells) in enumerate(rows):
            if {fold_header(cell) for cell in cells} >= self.REQUIRED_COLUMNS:
                return index
        return None

    def _transactions(
        self, rows: list[tuple[int, list[str]]], header_index: int
    ) -> list[tuple[int, list[str]]]:
        """The rows that are transactions, which is the ones that start with a date.

        Below the header the file also carries a totals line and four lines of
        branch address, trade registry number and a URL — and because every row
        is padded to five fields, none of that is narrower than a transaction.
        A leading `dd.mm.yyyy` is the only thing that separates them.

        Skipping the totals line is not merely tidy. It reads `Borç:-2.262,07`,
        in the *European* convention, while every transaction row in the same
        file writes `-2,262.07`. Anything that read both would have to know that
        one document uses two conventions, and `to_decimal` would silently turn
        that total into -2.26207.
        """
        return [
            (line, cells)
            for line, cells in rows[header_index + 1 :]
            if cells and _ROW_DATE.match(cells[0].strip())
        ]

    def _currency(self, preamble: list[tuple[int, list[str]]]) -> str:
        """Read the currency off the account line above the header.

        There is no currency column: one export is one account, and the account
        line ends with what it is denominated in. Defaulting to `TRY` when that
        is missing would mislabel a foreign-currency account's every row with no
        error anywhere, so a file that does not say raises instead.
        """
        for _, cells in preamble:
            for cell in cells:
                tokens = cell.strip().split()
                if tokens and (code := _CURRENCIES.get(tokens[-1].upper())):
                    return code
        raise StatementParseError(
            self.institution,
            f"statement does not state its currency; expected one of "
            f"{sorted(_CURRENCIES)} at the end of the account line",
        )

    def _to_transaction(self, line: int, row: dict[str, str], currency: str) -> Transaction:
        """Convert one source row into exactly one transaction.

        **Nothing is synthesized**, which is Wise's rule rather than Revolut's
        and for the same reason: this export already emits its charges as rows
        of their own. A transfer is followed by `KOMİSYON`, by `BSMV` — the
        Turkish transaction tax — and by the fee for the SMS announcing it, each
        a row carrying the same `Fiş No` as the transfer that caused it. The
        running balance counts all four, and the evidence is inside the file:
        `Bakiye` reconciles against `İşlem Tutarı` alone, so there is no fee
        anywhere that a row does not already account for.
        """
        missing = sorted(column for column in self.REQUIRED_COLUMNS if row.get(column) is None)
        if missing:
            raise StatementParseError(self.institution, f"missing columns {missing}", row=line)

        posted = self._posted(row["tarih"], line)
        amount = to_decimal(
            row["islem_tutari"],
            convention=self.decimal_convention,
            institution=self.institution,
            row=line,
            column="islem_tutari",
        )
        balance = to_decimal(
            row["bakiye"],
            convention=self.decimal_convention,
            institution=self.institution,
            row=line,
            column="bakiye",
        )
        try:
            return Transaction(
                date=posted,
                description=row["aciklama"],
                amount=abs(amount),
                currency=currency,
                direction=Direction.DEBIT if amount < 0 else Direction.CREDIT,
                balance_after=balance,
                raw_row=dict(row),
                # A transaction *group*, not a row: a transfer and the
                # commission, tax and message fee charged for it share one.
                # Identity keeps the date, amount and currency alongside it for
                # exactly that reason — see `models/identity.py`.
                external_id=row["fis_no"],
                source_institution=self.institution,
            )
        except ValueError as exc:  # pydantic ValidationError subclasses ValueError
            raise StatementParseError(self.institution, str(exc), row=line) from exc

    def _posted(self, value: str, line: int) -> Date:
        """`04.09.2026` is 4 September.

        Dot-separated and day-first, as everything in the country is written.
        Read as month-first it becomes 9 April, which is a wrong date rather
        than an error, on every row that could go either way.
        """
        return to_date(
            value, fmt=self.DATE_FORMAT, institution=self.institution, row=line, column="tarih"
        )
