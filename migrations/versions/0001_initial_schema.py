"""initial schema: statements and transactions

Revision ID: 0001
Revises:
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The enum types are created explicitly in upgrade(), so the column definitions
# must NOT re-emit CREATE TYPE (`create_type=False`) or the migration fails with
# "type already exists".
statement_format = postgresql.ENUM("csv", "pdf", name="statement_format", create_type=False)
transaction_direction = postgresql.ENUM(
    "credit", "debit", name="transaction_direction", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    statement_format.create(bind, checkfirst=True)
    transaction_direction.create(bind, checkfirst=True)

    op.create_table(
        "statements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("source_institution", sa.String(length=64), nullable=False),
        sa.Column("format", statement_format, nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("transaction_count", sa.Integer(), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_statements"),
    )
    op.create_index("ix_statements_source_institution", "statements", ["source_institution"])
    op.create_index("ix_statements_content_sha256", "statements", ["content_sha256"])

    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("statement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("direction", transaction_direction, nullable=False),
        sa.Column("balance_after", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("raw_row", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_institution", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_transactions"),
        sa.ForeignKeyConstraint(
            ["statement_id"],
            ["statements.id"],
            name="fk_transactions_statement_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("statement_id", "row_index", name="uq_transactions_statement_row"),
        sa.CheckConstraint("amount >= 0", name="ck_transactions_amount_non_negative"),
    )
    op.create_index("ix_transactions_statement_id", "transactions", ["statement_id"])
    op.create_index("ix_transactions_source_institution", "transactions", ["source_institution"])
    op.create_index("ix_transactions_date_direction", "transactions", ["date", "direction"])


def downgrade() -> None:
    op.drop_index("ix_transactions_date_direction", table_name="transactions")
    op.drop_index("ix_transactions_source_institution", table_name="transactions")
    op.drop_index("ix_transactions_statement_id", table_name="transactions")
    op.drop_table("transactions")

    op.drop_index("ix_statements_content_sha256", table_name="statements")
    op.drop_index("ix_statements_source_institution", table_name="statements")
    op.drop_table("statements")

    bind = op.get_bind()
    transaction_direction.drop(bind, checkfirst=True)
    statement_format.drop(bind, checkfirst=True)
