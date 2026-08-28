from __future__ import annotations

import csv
import io
from typing import ClassVar

from statement_normalizer.models.schemas import Direction, StatementFormat, Transaction
from statement_normalizer.parsers.base import StatementFile, StatementParser
from statement_normalizer.parsers.csv_fields import normalize_header, to_date, to_decimal
from statement_normalizer.parsers.exceptions import StatementParseError
from statement_normalizer.parsers.registry import registry


@registry.register
class DummyBankCsvParser(StatementParser):
    """Reference adapter against the fake `dummy_bank` CSV export.

    It exists to exercise the contract end to end — detection, parsing,
    validation — without committing to any real institution's layout yet. Real
    adapters should look like this one.
    """

    institution: ClassVar[str] = "dummy_bank"
    supported_formats: ClassVar[frozenset[StatementFormat]] = frozenset({StatementFormat.CSV})
    priority: ClassVar[int] = 100

    #: The exact header this export emits; the fingerprint used for detection.
    HEADER: ClassVar[tuple[str, ...]] = (
        "account_number",
        "posting_date",
        "narrative",
        "amount",
        "currency",
        "running_balance",
    )
    DATE_FORMAT: ClassVar[str] = "%Y-%m-%d"

    def can_parse(self, file: StatementFile) -> bool:
        if file.format is not StatementFormat.CSV:
            return False
        head = file.head(1)
        if not head:
            return False
        header = tuple(normalize_header(c) for c in next(csv.reader(head)))
        return header == self.HEADER

    def parse(self, file: StatementFile) -> list[Transaction]:
        reader = csv.DictReader(io.StringIO(file.text))
        if reader.fieldnames is None:
            raise StatementParseError(self.institution, "file is empty")
        reader.fieldnames = [normalize_header(name) for name in reader.fieldnames]

        transactions: list[Transaction] = []
        for row_number, row in enumerate(reader, start=2):  # row 1 is the header
            if not any((value or "").strip() for value in row.values()):
                continue
            transactions.append(self._to_transaction(row, row_number))
        return transactions

    def extract_account_ref(self, file: StatementFile) -> str | None:
        """This export repeats the account number on every row; take the first."""
        reader = csv.DictReader(io.StringIO(file.text))
        if reader.fieldnames is None:
            return None
        reader.fieldnames = [normalize_header(name) for name in reader.fieldnames]
        for row in reader:
            account_ref = (row.get("account_number") or "").strip()
            if account_ref:
                return account_ref
        return None

    def _to_transaction(self, row: dict[str, str], row_number: int) -> Transaction:
        missing = [column for column in self.HEADER if row.get(column) is None]
        if missing:
            raise StatementParseError(
                self.institution, f"missing columns {missing}", row=row_number
            )

        posted = to_date(
            row["posting_date"],
            fmt=self.DATE_FORMAT,
            institution=self.institution,
            row=row_number,
            column="posting_date",
        )

        amount = to_decimal(
            row["amount"], institution=self.institution, row=row_number, column="amount"
        )
        balance_raw = (row["running_balance"] or "").strip()
        balance = (
            to_decimal(
                balance_raw,
                institution=self.institution,
                row=row_number,
                column="running_balance",
            )
            if balance_raw
            else None
        )

        try:
            return Transaction(
                date=posted,
                description=row["narrative"],
                amount=abs(amount),
                currency=row["currency"],
                direction=Direction.DEBIT if amount < 0 else Direction.CREDIT,
                balance_after=balance,
                raw_row=dict(row),
                source_institution=self.institution,
            )
        except ValueError as exc:  # pydantic ValidationError subclasses ValueError
            raise StatementParseError(self.institution, str(exc), row=row_number) from exc
