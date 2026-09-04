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
    #: Rows parsed out of this file.
    transaction_count: Mapped[int] = mapped_column(default=0)
    #: How many of those were not already stored by an earlier statement. Lower
    #: than `transaction_count` when this statement's period overlaps another's.
    new_transaction_count: Mapped[int] = mapped_column(default=0)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    transactions: Mapped[list[Transaction]] = relationship(
        secondary="statement_transactions",
        viewonly=True,
        order_by="Transaction.date",
    )


class Transaction(Base):
    """A single normalized transaction line."""

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_transactions_amount_non_negative"),
        Index("ix_transactions_date_direction", "date", "direction"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Synthesized identity, so the same transaction arriving in two overlapping
    # statements is stored once. NULL means "never deduplicate this row" — see
    # `models/identity.py`. NULLs do not collide in a unique index, which is what
    # makes that case need no special handling here.
    dedupe_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, unique=True
    )
    # Denormalized from the owning statement: a unique index cannot span a join,
    # and `dedupe_key` is only meaningful when scoped to an account.
    account_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The institution's own id, when it publishes one. Not unique and not
    # indexed: it feeds the fingerprint rather than being looked up, and it is
    # not unique per row anyway — Wise reuses one across a transfer and its fee.
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    date: Mapped[Date]
    description: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    direction: Mapped[Direction] = mapped_column(_direction_enum)
    balance_after: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    raw_row: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_institution: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Every statement this transaction appeared in. Read-only: links are written
    # directly by the upload path, and `ON DELETE CASCADE` removes them.
    statements: Mapped[list[Statement]] = relationship(
        secondary="statement_transactions", viewonly=True
    )

    @property
    def statement_ids(self) -> list[uuid.UUID]:
        """What `TransactionRead` serializes. Eager-load `statements` to avoid N+1."""
        return [statement.id for statement in self.statements]


class StatementTransaction(Base):
    """Which statements a transaction appeared in, and where in each file.

    A join table rather than a foreign key on `transactions` because overlapping
    statements genuinely share rows. Without it, one of two queries has to be
    wrong: either the unfiltered list double-counts the overlap, or a statement
    fails to report rows that are really in its file.
    """

    __tablename__ = "statement_transactions"
    __table_args__ = (
        UniqueConstraint(
            "statement_id", "row_index", name="uq_statement_transactions_statement_row"
        ),
    )

    statement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("statements.id", ondelete="CASCADE"), primary_key=True
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    # Position in this statement's file; preserves in-file order for equal dates.
    row_index: Mapped[int]
