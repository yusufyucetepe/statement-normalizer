# statement-normalizer — commit 1: skeleton + parsing contract

## Scope
Structure and the adapter contract only. No parsers for real institutions.

## Done
- [x] src/ layout, `pyproject.toml`, uv lockfile (`uv.lock`)
- [x] FastAPI app: `POST /statements/upload`, `GET /transactions` (date + type
      filters), plus `/health` and `/parsers`
- [x] Pydantic `Transaction`: date, description, amount (Decimal), currency,
      direction, balance_after, raw_row, source_institution
- [x] SQLAlchemy `statements` + `transactions` tables
- [x] Alembic configured; hand-written initial migration `0001`
- [x] `StatementParser` ABC (`can_parse` / `parse`) + `ParserRegistry`
- [x] `DummyBankCsvParser` against `tests/fixtures/dummy_bank_statement.csv`
- [x] pytest: 7 tests over the registry and the dummy parser
- [x] Dockerfile + docker-compose with Postgres
- [x] GitHub Actions: ruff check, ruff format --check, pytest
- [x] README with setup and an "adapter pattern" section

## Verification performed
- `pytest` — 7 passed.
- `ruff check .` / `ruff format --check .` — clean.
- `docker compose up --build` — Postgres healthy, `alembic upgrade head` ran,
  uvicorn served; `POST /statements/upload` returned 4 normalized transactions
  over HTTP; unknown layout returned 422.
- `alembic check` — "No new upgrade operations detected", i.e. migration `0001`
  matches the ORM metadata exactly.
- `alembic downgrade base && alembic upgrade head` — round trip clean.
- ORM round trip against real Postgres: insert -> `list_transactions` filters
  (direction / date_from / date_to / institution) -> `TransactionRead`
  serialization -> FK cascade delete.

## Next
- [ ] Persist on upload: insert `Statement` + `Transaction` rows in one
      transaction; set `stored=true`; return `StatementRead`.
- [ ] Dedupe re-uploads on `Statement.content_sha256` (column + index exist).
- [ ] First real institution adapter, with an anonymized fixture.
- [ ] PDF adapter (`StatementFile.is_pdf` already detects by magic bytes; no
      PDF dependency is wired up yet).
