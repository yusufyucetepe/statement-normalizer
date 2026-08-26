"""statements.account_ref, and a unique content hash for upload dedupe

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("statements", sa.Column("account_ref", sa.String(length=64), nullable=True))

    # The unique index is what makes upload dedupe correct: a concurrent pair of
    # identical uploads both pass a SELECT pre-check, and only this constraint
    # stops the second insert.
    op.drop_index("ix_statements_content_sha256", table_name="statements")
    op.create_index("ix_statements_content_sha256", "statements", ["content_sha256"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_statements_content_sha256", table_name="statements")
    op.create_index("ix_statements_content_sha256", "statements", ["content_sha256"])
    op.drop_column("statements", "account_ref")
