from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from statement_normalizer.api.deps import SessionDep
from statement_normalizer.models.schemas import Direction, TransactionRead
from statement_normalizer.models.tables import Transaction as TransactionRow

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("", response_model=list[TransactionRead])
def list_transactions(
    session: SessionDep,
    date_from: Annotated[date | None, Query(description="Inclusive lower bound on date.")] = None,
    date_to: Annotated[date | None, Query(description="Inclusive upper bound on date.")] = None,
    direction: Annotated[Direction | None, Query(description="Credit or debit.")] = None,
    institution: Annotated[str | None, Query(description="Source institution.")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TransactionRow]:
    """List stored transactions, newest first, with date and type filters.

    STUB: the query is real, but nothing writes to these tables yet (see
    POST /statements/upload), so this returns an empty list until persistence
    lands.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "date_from must not be after date_to"
        )

    stmt = select(TransactionRow)
    if date_from:
        stmt = stmt.where(TransactionRow.date >= date_from)
    if date_to:
        stmt = stmt.where(TransactionRow.date <= date_to)
    if direction:
        stmt = stmt.where(TransactionRow.direction == direction)
    if institution:
        stmt = stmt.where(TransactionRow.source_institution == institution)

    stmt = stmt.order_by(TransactionRow.date.desc(), TransactionRow.row_index)
    stmt = stmt.limit(limit).offset(offset)
    return list(session.scalars(stmt))
