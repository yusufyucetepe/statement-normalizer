from __future__ import annotations

import csv
from collections.abc import Iterator
from decimal import Decimal
from typing import ClassVar

from statement_normalizer.models.schemas import Direction, StatementFormat, Transaction
from statement_normalizer.parsers.base import StatementFile, StatementParser
from statement_normalizer.parsers.csv_fields import dict_rows, normalize_header, to_date, to_decimal
from statement_normalizer.parsers.exceptions import StatementParseError
from statement_normalizer.parsers.registry import registry


@registry.register
class WiseCsvParser(StatementParser):
    """Wise's per-currency balance statement CSV.

    The second real institution, and it is here mostly because it disagrees with
    the first. Revolut and Wise both publish a fee column beside a signed amount
    and a running balance; reading Wise's the way Revolut's is read would
    double-count every fee in the file. What the schema had to absorb was not a
    new shape but the same shape meaning something else — see `_to_transaction`.
    """

    institution: ClassVar[str] = "wise"
    supported_formats: ClassVar[frozenset[StatementFormat]] = frozenset({StatementFormat.CSV})
    priority: ClassVar[int] = 100

    #: Columns this adapter reads, normalized. Deliberately the *intersection* of
    #: the export's vintages rather than any one of them: Wise has shipped 19-,
    #: 20- and 23-column versions of this file, and these six appear in all of
    #: them. `transferwise_id` alone is distinctive enough to make detection
    #: unambiguous, so the set buys specificity without buying brittleness.
    REQUIRED_COLUMNS: ClassVar[frozenset[str]] = frozenset(
        {
            "transferwise_id",
            "date",
            "amount",
            "currency",
            "description",
            "running_balance",
        }
    )
    #: Day-first with hyphens. Not `Date Time`, which is absent from the older
    #: vintage — and not `%m-%d-%Y`: `03-04-2026` in this file is 3 April.
    DATE_FORMAT: ClassVar[str] = "%d-%m-%Y"

    def can_parse(self, file: StatementFile) -> bool:
        """Match on a required subset, as for Revolut, and for a sharper reason.

        Wise has changed this export's column count twice while keeping the
        names, so an exact-header rule would have broken on their release
        schedule rather than on anything a user did. `transferwise_id` is not a
        column name anyone else emits, which is what keeps the loose rule from
        colliding with another institution.
        """
        if file.format is not StatementFormat.CSV:
            return False
        head = file.head(1)
        if not head:
            return False
        header = {normalize_header(column) for column in next(csv.reader(head))}
        return header >= self.REQUIRED_COLUMNS

    def parse(self, file: StatementFile) -> list[Transaction]:
        return [self._to_transaction(row, row_number) for row_number, row in self._rows(file)]

    def extract_account_ref(self, file: StatementFile) -> str | None:
        """Use the statement's currency: a Wise balance *is* a currency.

        There is no account number in this export. `Payee Account Number` looks
        like one and is the counterparty's — scoping identity by it would file
        every transaction under whoever was paid, so two payments to different
        people could never be recognized as the same account's.

        One file is one currency balance, so `wise|EUR` is a real account scope.
        Like `revolut|Current` it is well defined only while this service is
        single-tenant; both assumptions expire the day it grows users.
        """
        for _, row in self._rows(file):
            currency = (row.get("currency") or "").strip().upper()
            if currency:
                return currency
        return None

    def _rows(self, file: StatementFile) -> Iterator[tuple[int, dict[str, str]]]:
        return dict_rows(file, institution=self.institution)

    def _to_transaction(self, row: dict[str, str], row_number: int) -> Transaction:
        """Convert one source row into exactly one transaction.

        **`Total fees` is never turned into a transaction of its own**, which is
        the opposite of what the Revolut adapter does with its `Fee` column, and
        the single decision in this file most able to corrupt totals silently.
        Wise has already accounted for the fee by the time it reaches this row,
        in one of two ways:

        - on a transfer, as its own row (`"Wise Charges for: TRANSFER-…"`),
          carrying the same id as the transfer it belongs to;
        - on a card payment, folded into `Amount` itself.

        Either way `Amount` is the exact movement the balance recorded, and both
        readings are checkable in the same file: `Running Balance` reconciles
        against `Amount` alone. Synthesizing a fee row would count the money
        twice under both.
        """
        missing = sorted(column for column in self.REQUIRED_COLUMNS if row.get(column) is None)
        if missing:
            raise StatementParseError(
                self.institution, f"missing columns {missing}", row=row_number
            )

        posted = to_date(
            row["date"],
            fmt=self.DATE_FORMAT,
            institution=self.institution,
            row=row_number,
            column="date",
        )
        amount = self._money(row, "amount", row_number)
        if amount is None:
            raise StatementParseError(self.institution, "row has no amount", row=row_number)

        description = (row["description"] or "").strip()
        reference = (row.get("payment_reference") or "").strip()
        if reference:
            # Wise puts what the sender typed in a column of its own. Two
            # payments to the same payee on the same day are otherwise identical
            # rows, and identity would collapse them into one.
            description = f"{description} ({reference})"

        try:
            return Transaction(
                date=posted,
                description=description,
                amount=abs(amount),
                currency=row["currency"],
                direction=Direction.DEBIT if amount < 0 else Direction.CREDIT,
                balance_after=self._money(row, "running_balance", row_number),
                raw_row=dict(row),
                # Not unique per row: a transfer and the fee charged for it carry
                # the same id. Identity keeps date, amount and currency alongside
                # it for exactly that reason — see `models/identity.py`.
                external_id=row["transferwise_id"],
                source_institution=self.institution,
            )
        except ValueError as exc:  # pydantic ValidationError subclasses ValueError
            raise StatementParseError(self.institution, str(exc), row=row_number) from exc

    def _money(self, row: dict[str, str], column: str, row_number: int) -> Decimal | None:
        raw = (row.get(column) or "").strip()
        if not raw:
            return None
        return to_decimal(raw, institution=self.institution, row=row_number, column=column)
