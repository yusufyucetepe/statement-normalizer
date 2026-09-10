from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Date as SqlDate
from sqlalchemy import cast, func, select
from sqlalchemy.orm import selectinload

from statement_normalizer.api.deps import SessionDep
from statement_normalizer.api.paging import count_matching
from statement_normalizer.models.schemas import (
    Direction,
    MonthlyTotal,
    MonthlyTotals,
    TransactionPage,
)
from statement_normalizer.models.tables import StatementTransaction as LinkRow
from statement_normalizer.models.tables import Transaction as TransactionRow

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("", response_model=TransactionPage)
def list_transactions(
    session: SessionDep,
    date_from: Annotated[date | None, Query(description="Inclusive lower bound on date.")] = None,
    date_to: Annotated[date | None, Query(description="Inclusive upper bound on date.")] = None,
    direction: Annotated[Direction | None, Query(description="Credit or debit.")] = None,
    institution: Annotated[str | None, Query(description="Source institution.")] = None,
    statement_id: Annotated[UUID | None, Query(description="One uploaded statement.")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TransactionPage:
    """List stored transactions, newest first, with date and type filters.

    Returns one page of rows plus `total`, the number matching the filters
    before `limit`/`offset` are applied.

    Each transaction appears once however many statements contained it, so
    overlapping statement periods do not double-count. Filtering by
    `statement_id` returns everything in that file, including rows a previous
    statement introduced.
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

    total = count_matching(session, stmt)
    rows = session.scalars(
        stmt.options(selectinload(TransactionRow.statements)).limit(limit).offset(offset)
    )
    return TransactionPage(items=list(rows), total=total, limit=limit, offset=offset)


@router.get("/monthly-totals", response_model=MonthlyTotals)
def monthly_totals(
    session: SessionDep,
    date_from: Annotated[date | None, Query(description="Inclusive lower bound on date.")] = None,
    date_to: Annotated[date | None, Query(description="Inclusive upper bound on date.")] = None,
    institution: Annotated[str | None, Query(description="Source institution.")] = None,
) -> MonthlyTotals:
    """Total money per month, split by currency and by direction.

    Currencies are never summed together and debits are never netted against
    credits: both would produce a number with no meaning, since `amount` is a
    magnitude whose sign lives in `direction`.

    Each transaction is counted once however many statements contain it. That is
    the whole risk in this endpoint — `institution` is filtered on the column
    denormalized onto `transactions`, never by joining `statement_transactions`,
    because a transaction shared by two statements of the same institution would
    match the join twice and inflate every sum by exactly the overlap. A wrong
    total looks exactly like a right one.

    Aggregation runs in Postgres rather than over a full table read in Python.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "date_from must not be after date_to"
        )

    month = cast(func.date_trunc("month", TransactionRow.date), SqlDate).label("month")
    stmt = select(
        month,
        TransactionRow.currency,
        TransactionRow.direction,
        func.sum(TransactionRow.amount).label("total"),
        func.count().label("transaction_count"),
    )
    if date_from:
        stmt = stmt.where(TransactionRow.date >= date_from)
    if date_to:
        stmt = stmt.where(TransactionRow.date <= date_to)
    if institution:
        stmt = stmt.where(TransactionRow.source_institution == institution)

    group = (month, TransactionRow.currency, TransactionRow.direction)
    rows = session.execute(stmt.group_by(*group).order_by(*group)).all()
    return MonthlyTotals(
        items=[MonthlyTotal.model_validate(row, from_attributes=True) for row in rows]
    )
