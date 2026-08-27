"""Per-transaction identity: dedupe across overlapping statements.

Before this migration a transaction belonged to exactly one statement, so two
statements covering an overlapping period stored the shared rows twice. This
moves the statement link into a join table and gives transactions a synthesized
identity (`dedupe_key`), so the shared rows are stored once and still reported
by both statements.

Two things worth knowing:

* `dedupe_key` is left NULL for rows that already exist. The fingerprint lives
  in `models/identity.py`; reimplementing it in SQL to backfill would mean two
  implementations of one hash drifting apart. NULL already means "do not
  deduplicate", so pre-existing rows simply never match later uploads.
* **The downgrade is lossy.** A transaction that appeared in two statements
  cannot fit back into a single `statement_id` column: downgrade keeps the
  earliest statement and drops the other links.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "statements",
        sa.Column("new_transaction_count", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "statement_transactions",
        sa.Column("statement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["statement_id"], ["statements.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("statement_id", "transaction_id"),
        sa.UniqueConstraint(
            "statement_id", "row_index", name="uq_statement_transactions_statement_row"
        ),
    )
    op.create_index(
        "ix_statement_transactions_transaction_id",
        "statement_transactions",
        ["transaction_id"],
    )

    op.add_column("transactions", sa.Column("dedupe_key", sa.String(length=64), nullable=True))
    op.add_column("transactions", sa.Column("account_ref", sa.String(length=64), nullable=True))
    op.create_index("ix_transactions_dedupe_key", "transactions", ["dedupe_key"], unique=True)

    # Carry the existing one-to-many across before dropping it.
    op.execute(
        """
        INSERT INTO statement_transactions (statement_id, transaction_id, row_index)
        SELECT statement_id, id, row_index FROM transactions
        """
    )
    op.execute(
        """
        UPDATE transactions t
           SET account_ref = s.account_ref
          FROM statements s
         WHERE s.id = t.statement_id
        """
    )

    op.drop_constraint("uq_transactions_statement_row", "transactions", type_="unique")
    op.drop_index("ix_transactions_statement_id", table_name="transactions")
    op.drop_column("transactions", "row_index")
    op.drop_column("transactions", "statement_id")

    op.alter_column("statements", "new_transaction_count", server_default=None)


def downgrade() -> None:
    op.add_column(
        "transactions", sa.Column("statement_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("transactions", sa.Column("row_index", sa.Integer(), nullable=True))

    # Lossy on purpose: a shared transaction keeps only its earliest statement.
    op.execute(
        """
        UPDATE transactions t
           SET statement_id = pick.statement_id,
               row_index    = pick.row_index
          FROM (
                SELECT DISTINCT ON (st.transaction_id)
                       st.transaction_id, st.statement_id, st.row_index
                  FROM statement_transactions st
                  JOIN statements s ON s.id = st.statement_id
                 ORDER BY st.transaction_id, s.uploaded_at, s.id
               ) AS pick
         WHERE pick.transaction_id = t.id
        """
    )
    op.execute("DELETE FROM transactions WHERE statement_id IS NULL")

    op.alter_column("transactions", "statement_id", nullable=False)
    op.alter_column("transactions", "row_index", nullable=False)
    op.create_foreign_key(
        "transactions_statement_id_fkey",
        "transactions",
        "statements",
        ["statement_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_transactions_statement_id", "transactions", ["statement_id"])
    op.create_unique_constraint(
        "uq_transactions_statement_row", "transactions", ["statement_id", "row_index"]
    )

    op.drop_index("ix_transactions_dedupe_key", table_name="transactions")
    op.drop_column("transactions", "account_ref")
    op.drop_column("transactions", "dedupe_key")

    op.drop_index("ix_statement_transactions_transaction_id", table_name="statement_transactions")
    op.drop_table("statement_transactions")
    op.drop_column("statements", "new_transaction_count")
