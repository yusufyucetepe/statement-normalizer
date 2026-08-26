import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from statement_normalizer.parsers import StatementFile

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).parent.parent

#: DB-backed tests run only when this is set, so the parser and registry tests
#: still work on a machine with no Postgres.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def statement_file():
    """Load a fixture file into the value object parsers actually receive."""

    def _load(name: str) -> StatementFile:
        return StatementFile.from_path(FIXTURES / name, content_type="text/csv")

    return _load


@pytest.fixture(scope="session")
def db_engine():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")

    from alembic import command
    from alembic.config import Config

    engine = create_engine(TEST_DATABASE_URL, future=True)

    # Migrate rather than metadata.create_all: this is what proves the migrations
    # themselves work, which is how the enum bug in 0001 got caught.
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")

    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Iterator[Session]:
    """A session whose writes are rolled back when the test ends.

    The outer transaction is never committed, so `session.commit()` inside the
    code under test lands on a savepoint instead. Each test therefore sees a
    clean database without the cost of re-running migrations.
    """
    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db_session):
    """TestClient wired to the rolled-back test session."""
    from fastapi.testclient import TestClient

    from statement_normalizer.db import get_session
    from statement_normalizer.main import app

    app.dependency_overrides[get_session] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def upload(client):
    """POST a fixture file to /statements/upload."""

    def _upload(name: str):
        with open(FIXTURES / name, "rb") as handle:
            return client.post("/statements/upload", files={"file": (name, handle, "text/csv")})

    return _upload


@pytest.fixture
def count_rows(db_session):
    def _count(table: str) -> int:
        return db_session.scalar(text(f"SELECT count(*) FROM {table}"))

    return _count
