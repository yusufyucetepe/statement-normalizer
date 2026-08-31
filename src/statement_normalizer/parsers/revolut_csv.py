from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from decimal import Decimal
from typing import ClassVar

from statement_normalizer.models.schemas import Direction, StatementFormat, Transaction
from statement_normalizer.parsers.base import StatementFile, StatementParser
from statement_normalizer.parsers.csv_fields import normalize_header, to_date, to_decimal
from statement_normalizer.parsers.exceptions import StatementParseError
from statement_normalizer.parsers.registry import registry

_ZERO = Decimal("0")


@registry.register
class RevolutCsvParser(StatementParser):
    """Revolut's personal-account CSV export.

    The first adapter for an institution whose format we do not control. Three
    things about it do not map onto the normalized schema without a decision
    being taken; each is argued where it is taken, in `can_parse`,
    `_to_transactions` and `extract_account_ref`.
    """

    institution: ClassVar[str] = "revolut"
    supported_formats: ClassVar[frozenset[StatementFormat]] = frozenset({StatementFormat.CSV})
    priority: ClassVar[int] = 100

    #: Columns this adapter reads, normalized. Detection requires all of them.
    REQUIRED_COLUMNS: ClassVar[frozenset[str]] = frozenset(
        {
            "type",
            "product",
            "started_date",
            "completed_date",
            "description",
            "amount",
            "fee",
            "currency",
            "state",
            "balance",
        }
    )
    #: Columns that appear only in Revolut's crypto/trading export. That file
    #: carries every column this adapter requires, so a subset match claims it —
    #: but its `Amount`, `Currency` and `Balance` are denominated in the asset
    #: (`100.0000`, `EOS`), and the money value lives in `Fiat amount`. Parsing
    #: it here would file crypto quantities as fiat, and nothing downstream would
    #: catch it: `EOS` is three uppercase letters and passes currency validation.
    TRADING_COLUMNS: ClassVar[frozenset[str]] = frozenset({"fiat_amount", "base_currency"})
    DATETIME_FORMAT: ClassVar[str] = "%Y-%m-%d %H:%M:%S"
    #: The one state in which money actually moved. See `_to_transactions`.
    SETTLED: ClassVar[str] = "COMPLETED"

    def can_parse(self, file: StatementFile) -> bool:
        """Match on a required *subset* of the header, not on equality.

        `dummy_csv` can demand an exact header tuple because we own that format.
        This one belongs to someone else, and a column appended to their export
        must not turn every upload into a 422. The combination of `product`,
        `started_date`, `completed_date` and `state` is distinctive enough that
        no other institution collides, so detection stays unambiguous.

        The exception is a superset that means something *different* rather than
        more: Revolut's crypto/trading export is this layout plus four columns,
        and claiming it would store asset quantities as money. A subset rule
        cannot tell that apart on its own, so it is named.
        """
        if file.format is not StatementFormat.CSV:
            return False
        head = file.head(1)
        if not head:
            return False
        header = {normalize_header(column) for column in next(csv.reader(head))}
        if header & self.TRADING_COLUMNS:
            return False
        return header >= self.REQUIRED_COLUMNS

    def parse(self, file: StatementFile) -> list[Transaction]:
        transactions: list[Transaction] = []
        for row_number, row in self._rows(file):
            transactions.extend(self._to_transactions(row, row_number))
        return transactions

    def extract_account_ref(self, file: StatementFile) -> str | None:
        """Use `Product` — this export carries no IBAN or account number.

        Returning None would leave `dedupe_key` NULL and let every overlapping
        re-download double-count, which is the bug per-transaction identity
        exists to prevent. `revolut|Current` is a well-defined account scope only
        because this service is single-tenant by construction; revisit this the
        day it grows users.
        """
        for _, row in self._rows(file):
            product = (row.get("product") or "").strip()
            if product:
                return product
        return None

    def _rows(self, file: StatementFile) -> Iterator[tuple[int, dict[str, str]]]:
        """Non-empty data rows, with their 1-based line number for error messages."""
        reader = csv.DictReader(io.StringIO(file.text))
        if reader.fieldnames is None:
            raise StatementParseError(self.institution, "file is empty")
        reader.fieldnames = [normalize_header(name) for name in reader.fieldnames]
        for row_number, row in enumerate(reader, start=2):  # row 1 is the header
            if any((value or "").strip() for value in row.values()):
                yield row_number, row

    def _to_transactions(self, row: dict[str, str], row_number: int) -> list[Transaction]:
        """Convert one source row into zero, one, or two normalized transactions."""
        missing = sorted(column for column in self.REQUIRED_COLUMNS if row.get(column) is None)
        if missing:
            raise StatementParseError(
                self.institution, f"missing columns {missing}", row=row_number
            )

        # DECLINED moved no money at all; REVERTED moved it and moved it back.
        # Storing either invents a ledger entry that never existed, and storing
        # only the outbound leg of a reversal is worse than storing neither.
        if (row["state"] or "").strip().upper() != self.SETTLED:
            return []

        completed = (row["completed_date"] or "").strip()
        if not completed:
            # Falling back to `started_date` here would misdate real money.
            raise StatementParseError(
                self.institution, f"{self.SETTLED} row has no completion date", row=row_number
            )
        posted = to_date(
            completed,
            fmt=self.DATETIME_FORMAT,
            institution=self.institution,
            row=row_number,
            column="completed_date",
        )

        amount = self._money(row, "amount", row_number, default=None)
        fee = self._money(row, "fee", row_number, default=_ZERO)
        balance = self._money(row, "balance", row_number, default=None)

        transactions = [
            self._build(
                row,
                row_number,
                date=posted,
                description=row["description"],
                amount=amount,
                # `Balance` has already absorbed the fee, so when there is one it
                # is not this transaction's balance — and the intermediate point
                # it would need is not a real position in the ledger.
                balance_after=None if fee else balance,
            )
        ]
        if fee:
            # A fee is a movement of its own: the running balance counts it, so
            # dropping it would stop stored totals reconciling, and folding it
            # into the amount would misstate what the merchant or ATM charged.
            transactions.append(
                self._build(
                    row,
                    row_number,
                    date=posted,
                    description=f"Fee: {row['description']}",
                    # Negated because Revolut reports a fee as a positive charge,
                    # and `_build` reads the sign to decide the direction.
                    amount=-fee,
                    balance_after=balance,
                )
            )
        return transactions

    def _money(
        self, row: dict[str, str], column: str, row_number: int, *, default: Decimal | None
    ) -> Decimal | None:
        raw = (row[column] or "").strip()
        if not raw:
            return default
        return to_decimal(raw, institution=self.institution, row=row_number, column=column)

    def _build(
        self,
        row: dict[str, str],
        row_number: int,
        *,
        date,
        description: str,
        amount: Decimal | None,
        balance_after: Decimal | None,
    ) -> Transaction:
        if amount is None:
            raise StatementParseError(self.institution, "row has no amount", row=row_number)
        try:
            return Transaction(
                date=date,
                description=description,
                amount=abs(amount),
                currency=row["currency"],
                direction=Direction.DEBIT if amount < 0 else Direction.CREDIT,
                balance_after=balance_after,
                raw_row=dict(row),
                source_institution=self.institution,
            )
        except ValueError as exc:  # pydantic ValidationError subclasses ValueError
            raise StatementParseError(self.institution, str(exc), row=row_number) from exc
