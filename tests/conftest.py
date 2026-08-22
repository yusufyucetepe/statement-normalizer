from pathlib import Path

import pytest

from statement_normalizer.parsers import StatementFile

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def statement_file():
    """Load a fixture file into the value object parsers actually receive."""

    def _load(name: str) -> StatementFile:
        return StatementFile.from_path(FIXTURES / name, content_type="text/csv")

    return _load
