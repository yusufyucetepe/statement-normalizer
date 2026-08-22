from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from statement_normalizer.db import get_session
from statement_normalizer.parsers import ParserRegistry, registry


def get_registry() -> ParserRegistry:
    """Overridable in tests via `app.dependency_overrides`."""
    return registry


SessionDep = Annotated[Session, Depends(get_session)]
RegistryDep = Annotated[ParserRegistry, Depends(get_registry)]
