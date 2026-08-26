from __future__ import annotations

import uuid
from datetime import date as Date
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from statement_normalizer.models.schemas import Direction, StatementFormat

# Money is stored as NUMERIC, never float. 4 decimal places covers FX-converted
# amounts and per-unit broker prices, not just 2-decimal fiat.
Money = Numeric(20, 4)


def _enum_values(enum_cls) -> list[str]:
    """Store enum *values* ("credit"), not member names ("CREDIT"), in Postgres."""
    return [member.value for member in enum_cls]


_direction_enum = Enum(Direction, name="transaction_direction", values_callable=_enum_values)
_format_enum = Enum(StatementFormat, name="statement_format", values_callable=_enum_values)


class Base(DeclarativeBase):
    pass


class Statement(Base):
    """One uploaded file, and the parser that claimed it."""

    __tablename__ = "statements"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(512))
    source_institution: Mapped[str] = mapped_column(String(64), index=True)
    format: Mapped[StatementFormat] = mapped_column(_format_enum)
    # Unique: re-uploading the same bytes is rejected rather than double-counted.
    content_sha256: Mapped[str] = mapped_column(String(64), index=True, unique=True)
    # The account/IBAN/broker id, when the export exposes one. Deliberately a
    # nullable column rather than an Account entity: we model that once a real
    # export shows us its shape.
    account_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transaction_count: Mapped[int] = mapped_column(default=0)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="statement",
        cascade="all, delete-orphan",
        order_by="Transaction.row_index",
    )


class Transaction(Base):
    """A single normalized transaction line."""

    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("statement_id", "row_index", name="uq_transactions_statement_row"),
        CheckConstraint("amount >= 0", name="ck_transactions_amount_non_negative"),
        Index("ix_transactions_date_direction", "date", "direction"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    statement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("statements.id", ondelete="CASCADE"), index=True
    )
    # Position in the source file; preserves statement order for equal dates.
    row_index: Mapped[int]

    date: Mapped[Date]
    description: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    direction: Mapped[Direction] = mapped_column(_direction_enum)
    balance_after: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    raw_row: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_institution: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    statement: Mapped[Statement] = relationship(back_populates="transactions")
