from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from statement_normalizer.api.deps import RegistryDep, SessionDep
from statement_normalizer.config import get_settings
from statement_normalizer.models.schemas import StatementRead
from statement_normalizer.models.tables import Statement as StatementRow
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


def _persist(session, statement_file: StatementFile, result: ParseResult) -> StatementRow:
    """Insert the statement and its transactions atomically.

    Parsing has already succeeded by this point, so a 4xx can never leave a
    half-written statement behind.
    """
    statement = StatementRow(
        filename=statement_file.filename,
        source_institution=result.institution,
        format=result.format,
        content_sha256=statement_file.sha256,
        account_ref=result.account_ref,
        transaction_count=len(result.transactions),
    )
    session.add(statement)
    session.add_all(
        TransactionRow(
            statement=statement,
            row_index=row_index,
            date=txn.date,
            description=txn.description,
            amount=txn.amount,
            currency=txn.currency,
            direction=txn.direction,
            balance_after=txn.balance_after,
            raw_row=txn.raw_row,
            source_institution=txn.source_institution,
        )
        for row_index, txn in enumerate(result.transactions)
    )

    try:
        session.commit()
    except IntegrityError as exc:
        # The pre-check above loses the race between two concurrent identical
        # uploads: both see nothing and both insert. The unique index on
        # content_sha256 is the actual source of truth, so the loser lands here.
        session.rollback()
        winner = session.scalars(
            select(StatementRow).where(StatementRow.content_sha256 == statement_file.sha256)
        ).first()
        if winner is None:
            raise
        raise _duplicate(winner) from exc

    session.refresh(statement)
    return statement


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
