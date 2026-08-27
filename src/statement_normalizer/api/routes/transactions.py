from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from statement_normalizer.api.deps import SessionDep
from statement_normalizer.models.schemas import Direction, TransactionRead
from statement_normalizer.models.tables import StatementTransaction as LinkRow
from statement_normalizer.models.tables import Transaction as TransactionRow

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("", response_model=list[TransactionRead])
def list_transactions(
    session: SessionDep,
    date_from: Annotated[date | None, Query(description="Inclusive lower bound on date.")] = None,
    date_to: Annotated[date | None, Query(description="Inclusive upper bound on date.")] = None,
    direction: Annotated[Direction | None, Query(description="Credit or debit.")] = None,
    institution: Annotated[str | None, Query(description="Source institution.")] = None,
    statement_id: Annotated[UUID | None, Query(description="One uploaded statement.")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TransactionRow]:
    """List stored transactions, newest first, with date and type filters.

    Each transaction appears once however many statements contained it, so
    overlapping statement periods do not double-count. Filtering by
    `statement_id` returns everything in that file, including rows a previous
    statement introduced.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "date_from must not be after date_to"
        )

    stmt = select(TransactionRow).options(selectinload(TransactionRow.statements))
    if date_from:
        stmt = stmt.where(TransactionRow.date >= date_from)
    if date_to:
        stmt = stmt.where(TransactionRow.date <= date_to)
    if direction:
        stmt = stmt.where(TransactionRow.direction == direction)
    if institution:
        stmt = stmt.where(TransactionRow.source_institution == institution)
    if statement_id:
        # Join rather than a column filter: a transaction can belong to several
        # statements, and each statement stores its own position for it.
        stmt = stmt.join(LinkRow, LinkRow.transaction_id == TransactionRow.id).where(
            LinkRow.statement_id == statement_id
        )
        stmt = stmt.order_by(LinkRow.row_index)
    else:
        # No statement in view means no in-file position to order by; `id` is the
        # tiebreaker that keeps pagination stable across requests.
        stmt = stmt.order_by(
            TransactionRow.date.desc(), TransactionRow.created_at, TransactionRow.id
        )

    stmt = stmt.limit(limit).offset(offset)
    return list(session.scalars(stmt))
