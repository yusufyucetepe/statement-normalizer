from __future__ import annotations

import re
from datetime import date as Date
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class Direction(StrEnum):
    """Which way money moved, relative to the account the statement belongs to."""

    CREDIT = "credit"
    DEBIT = "debit"


class StatementFormat(StrEnum):
    CSV = "csv"
    PDF = "pdf"


class Transaction(BaseModel):
    """The normalized shape every parser must produce, whatever the source layout."""

    model_config = ConfigDict(frozen=True)

    date: Date
    description: str = Field(min_length=1, max_length=1024)
    amount: Decimal = Field(ge=0, description="Always non-negative; sign lives in `direction`.")
    currency: str = Field(description="ISO 4217 alphabetic code.")
    direction: Direction
    balance_after: Decimal | None = None
    raw_row: dict[str, Any] = Field(
        default_factory=dict,
        description="Verbatim source record, kept for audit and re-parsing.",
    )
    source_institution: str = Field(min_length=1, max_length=64)

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip().upper()
            if not _CURRENCY_RE.match(value):
                raise ValueError(f"not an ISO 4217 alphabetic code: {value!r}")
        return value

    @field_validator("description", mode="before")
    @classmethod
    def _collapse_whitespace(cls, value: Any) -> Any:
        if isinstance(value, str):
            return re.sub(r"\s+", " ", value).strip()
        return value

    @property
    def signed_amount(self) -> Decimal:
        """Amount with the direction applied, for arithmetic and storage-side checks."""
        return self.amount if self.direction is Direction.CREDIT else -self.amount


class TransactionRead(Transaction):
    """A stored transaction, as returned by GET /transactions."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    #: Every statement this transaction appeared in. A transaction shared by two
    #: overlapping statements is stored once and lists both.
    statement_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime


class StatementRead(BaseModel):
    """A stored statement. The body of POST /statements/upload.

    The transactions themselves are not inlined: a statement can carry thousands
    of rows, so the upload response points at `GET /transactions?statement_id=`
    via its Location header rather than growing without bound.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    source_institution: str
    format: StatementFormat
    content_sha256: str
    account_ref: str | None = None
    transaction_count: int = Field(description="Rows parsed out of this file.")
    new_transaction_count: int = Field(
        description="Rows not already stored by an earlier statement. Lower than "
        "`transaction_count` when this statement overlaps another.",
    )
    uploaded_at: datetime
