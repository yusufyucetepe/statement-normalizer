from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from statement_normalizer.api.deps import RegistryDep, SessionDep
from statement_normalizer.config import get_settings
from statement_normalizer.models.schemas import UploadResponse
from statement_normalizer.parsers import (
    AmbiguousParserMatch,
    NoMatchingParser,
    StatementFile,
    StatementParseError,
)

router = APIRouter(prefix="/statements", tags=["statements"])


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_200_OK)
async def upload_statement(
    registry: RegistryDep,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="A CSV or PDF statement export.")],
) -> UploadResponse:
    """Detect the institution, parse the file, and return normalized transactions.

    STUB: detection and parsing are real; persistence is not wired yet, so the
    response always reports `stored=false` and `statement=null`.
    """
    settings = get_settings()
    content = await file.read()
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

    try:
        result = registry.parse(statement_file)
    except NoMatchingParser as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except AmbiguousParserMatch as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except StatementParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    # TODO(persistence): insert a Statement row plus its Transactions inside one
    # transaction, then set stored=True and return the saved StatementRead.
    return UploadResponse(
        statement=None,
        detected_institution=result.institution,
        transaction_count=len(result.transactions),
        stored=False,
        transactions=result.transactions,
    )
