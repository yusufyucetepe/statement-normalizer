"""Carry the institution's own transaction id, and let identity use it.

Transaction identity was built from the description, which is the part of a row
an institution feels free to reword between exports. A reworded row fingerprints
differently and is stored a second time — the double-counting `0003` exists to
prevent, reintroduced by a changed narrative rather than an overlapping period.

Adapters whose export publishes an id now pass it through, and the fingerprint
uses it in place of the description for those rows.

Two things worth knowing:

* **No backfill, and none is possible.** The id is only in the source file. Rows
  stored before this migration have a NULL `external_id` and keep their existing
  description-based `dedupe_key`, which stays valid: the fingerprint marker is
  hashed in, so `v1` and `v2` keys coexist without colliding. A row stored the
  old way and re-uploaded the new way *will* be stored twice — re-uploading a
  statement is what re-keys it, exactly as for `0003`.
* The column is deliberately not unique and not indexed. An institution's id is
  not unique per row: Wise gives a transfer and the fee charged for it the same
  `TransferWise ID`.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("external_id", sa.String(length=128), nullable=True))


def downgrade() -> None:
    # Lossy in the same way `0003` is: the ids came from files, so dropping the
    # column throws away the only copy. Keys already built from an id are not
    # rebuilt, and stay in the database as v2 hashes of a value no longer stored.
    op.drop_column("transactions", "external_id")
