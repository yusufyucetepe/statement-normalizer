import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from statement_normalizer.api.deps import RegistryDep, SessionDep
from statement_normalizer.api.paging import count_matching
from statement_normalizer.config import get_settings
from statement_normalizer.models.identity import assign_dedupe_keys
from statement_normalizer.models.schemas import StatementPage, StatementRead
from statement_normalizer.models.tables import Statement as StatementRow
from statement_normalizer.models.tables import StatementTransaction as LinkRow
from statement_normalizer.models.tables import Transaction as TransactionRow
from statement_normalizer.parsers import (
    AmbiguousParserMatch,
    NoMatchingParser,
    ParseResult,
    StatementFile,
    StatementParseError,
)

router = APIRouter(prefix="/statements", tags=["statements"])


def _location(statement_id) -> str:
    return f"/transactions?statement_id={statement_id}"


# Sync `def`, not `async def`: FastAPI runs sync handlers in a threadpool, so the
# blocking SQLAlchemy calls below cannot stall the event loop.
@router.post(
    "/upload",
    response_model=StatementRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "This exact file has already been uploaded."}},
)
def upload_statement(
    registry: RegistryDep,
    session: SessionDep,
    response: Response,
    file: Annotated[UploadFile, File(description="A CSV or PDF statement export.")],
) -> StatementRow:
    """Detect the institution, parse the file, and store its transactions.

    Returns 201 with the statement summary and a Location header pointing at
    this statement's transactions. Re-uploading a byte-identical file returns
    409 with the statement that already holds it.
    """
    settings = get_settings()
    content = file.file.read()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "uploaded file is empty")
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"file exceeds {settings.max_upload_bytes} bytes",
        )

    statement_file = StatementFile(
        filename=file.filename or "upload",
        content=content,
        content_type=file.content_type,
    )

    # Fast path: skip the parse entirely if we already hold these bytes.
    existing = session.scalars(
        select(StatementRow).where(StatementRow.content_sha256 == statement_file.sha256)
    ).first()
    if existing is not None:
        raise _duplicate(existing)

    try:
        result = registry.parse(statement_file)
    except NoMatchingParser as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except AmbiguousParserMatch as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except StatementParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    statement = _persist(session, statement_file, result)
    response.headers["Location"] = _location(statement.id)
    return statement


@router.get("", response_model=StatementPage)
def list_statements(
    session: SessionDep,
    institution: Annotated[str | None, Query(description="Source institution.")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StatementPage:
    """List uploaded statements, most recently uploaded first.

    The upload response is otherwise the only place a statement id ever appears,
    so this is how a client recovers one it did not keep — and the only way to
    read `new_transaction_count` after the upload that produced it.
    """
    stmt = select(StatementRow)
    if institution:
        stmt = stmt.where(StatementRow.source_institution == institution)
    # `id` breaks ties: `uploaded_at` is a clock reading, and two uploads landing
    # in the same tick would otherwise page unstably.
    stmt = stmt.order_by(StatementRow.uploaded_at.desc(), StatementRow.id)

    total = count_matching(session, stmt)
    rows = session.scalars(stmt.limit(limit).offset(offset))
    return StatementPage(items=list(rows), total=total, limit=limit, offset=offset)


@router.get(
    "/{statement_id}",
    response_model=StatementRead,
    responses={404: {"description": "No statement with this id."}},
)
def get_statement(session: SessionDep, statement_id: uuid.UUID) -> StatementRow:
    """One uploaded statement by id, or 404.

    404 rather than the empty page `GET /transactions?statement_id=` returns for
    an unknown id: there, an id that matches nothing is a filter that excluded
    everything; here it is a request for a thing that does not exist.
    """
    statement = session.get(StatementRow, statement_id)
    if statement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no statement with id {statement_id}")
    return statement


def _persist(session, statement_file: StatementFile, result: ParseResult) -> StatementRow:
    """Store the statement, the transactions it introduces, and the links between.

    Transactions are deduplicated on `dedupe_key`, so a statement whose period
    overlaps an earlier one contributes only its new rows — but it still links to
    every row in its own file, so `?statement_id=` reports the whole statement.

    Parsing has already succeeded by this point, so a 4xx can never leave a
    half-written statement behind.
    """
    keys = assign_dedupe_keys(result.transactions, result.account_ref)
    # Generate the ids up front. `ON CONFLICT DO NOTHING` does not report which
    # rows it skipped, so knowing our own ids is what lets us tell "we inserted
    # this" from "someone else already had it" with a single follow-up SELECT.
    ids = [uuid.uuid4() for _ in result.transactions]

    statement = StatementRow(
        filename=statement_file.filename,
        source_institution=result.institution,
        format=result.format,
        content_sha256=statement_file.sha256,
        account_ref=result.account_ref,
        transaction_count=len(result.transactions),
    )
    session.add(statement)

    try:
        session.flush()
        resolved = _insert_transactions(session, result, keys, ids)
        if resolved:
            session.execute(
                insert(LinkRow),
                [
                    {
                        "statement_id": statement.id,
                        "transaction_id": transaction_id,
                        "row_index": row_index,
                    }
                    for row_index, transaction_id in enumerate(resolved)
                ],
            )
        statement.new_transaction_count = sum(
            1 for ours, actual in zip(ids, resolved, strict=True) if ours == actual
        )
        session.commit()
    except IntegrityError as exc:
        # The pre-check in the route loses the race between two concurrent
        # identical uploads: both see nothing and both insert. The unique index
        # on content_sha256 is the actual source of truth, so the loser lands
        # here. (Transaction-level races do not reach this branch — they are
        # absorbed by ON CONFLICT DO NOTHING below.)
        session.rollback()
        winner = session.scalars(
            select(StatementRow).where(StatementRow.content_sha256 == statement_file.sha256)
        ).first()
        if winner is None:
            raise
        raise _duplicate(winner) from exc

    session.refresh(statement)
    return statement


def _insert_transactions(
    session, result: ParseResult, keys: list[str | None], ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    """Insert the transactions this statement introduces; return one id per parsed row.

    Rows already stored under the same `dedupe_key` resolve to the existing id
    rather than a new one. `ON CONFLICT DO NOTHING` rather than
    SELECT-then-INSERT because a concurrent upload of an overlapping statement
    would otherwise have both writers insert the same transaction; the follow-up
    SELECT then resolves every key to whichever row actually won.

    A NULL `dedupe_key` never conflicts, so statements with no account reference
    always insert fresh rows — see `models/identity.py` for why.
    """
    if not result.transactions:
        return []

    session.execute(
        pg_insert(TransactionRow)
        .values(
            [
                {
                    "id": transaction_id,
                    "dedupe_key": key,
                    "account_ref": result.account_ref,
                    "date": txn.date,
                    "description": txn.description,
                    "amount": txn.amount,
                    "currency": txn.currency,
                    "direction": txn.direction,
                    "balance_after": txn.balance_after,
                    "raw_row": txn.raw_row,
                    "source_institution": txn.source_institution,
                }
                for transaction_id, key, txn in zip(ids, keys, result.transactions, strict=True)
            ]
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )

    known = [key for key in keys if key is not None]
    winners: dict[str, uuid.UUID] = {}
    if known:
        winners = dict(
            session.execute(
                select(TransactionRow.dedupe_key, TransactionRow.id).where(
                    TransactionRow.dedupe_key.in_(known)
                )
            ).all()
        )
    return [
        winners.get(key, transaction_id) if key is not None else transaction_id
        for key, transaction_id in zip(keys, ids, strict=True)
    ]


def _duplicate(existing: StatementRow) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "message": "this file has already been uploaded",
            "statement_id": str(existing.id),
            "uploaded_at": existing.uploaded_at.isoformat(),
        },
        headers={"Location": _location(existing.id)},
    )
