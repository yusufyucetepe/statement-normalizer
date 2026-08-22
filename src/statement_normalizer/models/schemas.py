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
    statement_id: UUID
    created_at: datetime


class StatementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    source_institution: str
    format: StatementFormat
    content_sha256: str
    transaction_count: int
    uploaded_at: datetime


class UploadResponse(BaseModel):
    """Result of POST /statements/upload."""

    statement: StatementRead | None = None
    detected_institution: str
    transaction_count: int
    stored: bool = Field(description="False while persistence is still a stub.")
    transactions: list[Transaction]
