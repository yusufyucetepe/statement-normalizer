# statement-normalizer

## Milestone 1 — skeleton + parsing contract (done)

- [x] src/ layout, `pyproject.toml`, uv lockfile (`uv.lock`)
- [x] FastAPI app: `POST /statements/upload`, `GET /transactions`, `/health`, `/parsers`
- [x] Pydantic `Transaction`: date, description, amount (Decimal), currency,
      direction, balance_after, raw_row, source_institution
- [x] SQLAlchemy `statements` + `transactions` tables
- [x] Alembic configured; hand-written initial migration `0001`
- [x] `StatementParser` ABC (`can_parse` / `parse`) + `ParserRegistry`
- [x] `DummyBankCsvParser` against a fake CSV fixture
- [x] pytest over the registry and the dummy parser
- [x] Dockerfile + docker-compose with Postgres
- [x] GitHub Actions: ruff check, ruff format --check, pytest
- [x] README with setup and an "adapter pattern" section

## Milestone 2 — persistence (done)

Decisions: reject duplicate uploads with 409 on `content_sha256`; return a
summary + `Location` header rather than the full transaction list; sync `def`
routes over async SQLAlchemy; nullable `account_ref` column rather than an
`accounts` table; Postgres service container in CI.

- [x] Migration `0002`: nullable `statements.account_ref`, unique index on
      `content_sha256`
- [x] Optional `StatementParser.extract_account_ref` hook (defaults to `None`,
      so `parse(file) -> list[Transaction]` stays the required interface);
      `ParseResult` carries `format` + `account_ref`
- [x] Dummy fixture gained an `Account Number` column so the hook is proven
      end to end
- [x] `POST /statements/upload` persists atomically, returns 201 +
      `Location`, 409 on duplicate content
- [x] `UploadResponse` / `stored` deleted — dead once persistence is real
- [x] `statement_id` filter on `GET /transactions`
- [x] DB test fixtures that skip without `TEST_DATABASE_URL`; 6 persistence tests
- [x] CI runs a `postgres:16-alpine` service
- [x] README: 409 semantics, `TEST_DATABASE_URL`, refreshed "Known gaps"

### Verification performed

- `uv run pytest` with no `TEST_DATABASE_URL` → 9 passed, 6 skipped.
- `TEST_DATABASE_URL=... uv run pytest` → **15 passed** against real Postgres.
- `alembic check` → "No new upgrade operations detected" — `0002` matches the ORM.
- `alembic downgrade base && alembic upgrade head` → clean round trip.
- HTTP against the compose stack: 201 + `Location`; re-upload → 409 with the
  same `Location`; `?statement_id=` → the 4 rows; unknown and malformed → 422
  with zero rows written; `account_ref` persisted as `GB00DUMY12345678`.
- **Race test:** 8 concurrent identical uploads → exactly one 201 and seven
  409s, one statement row, two transaction rows. This is the `IntegrityError`
  branch, not the `SELECT` fast path.
- `ruff check` / `ruff format --check` clean.

## Milestone 3 — per-transaction identity (done)

Fixes the correctness bug behind "overlapping statement periods double-count":
file-level dedupe only catches the same *file* twice, so two exports sharing a
period stored the shared rows twice and every total computed off `transactions`
was silently wrong.

Decisions: many-to-many, so `?statement_id=` reports a statement's whole file
while the unfiltered list counts each transaction once; a wholly-duplicate
upload is 201 with `new_transaction_count: 0`, not 409.

- [x] `models/identity.py`: SHA-256 fingerprint + occurrence numbering, so
      genuinely repeated transactions survive while overlaps merge
- [x] Migration `0003`: `statement_transactions` join table, `transactions`
      gains `dedupe_key` (unique) and `account_ref`, `statements` gains
      `new_transaction_count`
- [x] Upload inserts with `ON CONFLICT (dedupe_key) DO NOTHING` over
      pre-generated ids, so a lost race links to the winner's row
- [x] `/transactions` joins through the link table; `TransactionRead` exposes
      `statement_ids` (list) instead of `statement_id`
- [x] Fixtures: `dummy_bank_overlap.csv`, `dummy_bank_repeats.csv`,
      `dummy_bank_restated.csv`
- [x] 17 identity tests (no DB) + 4 new persistence tests
- [x] README: "Overlapping statements", refreshed Known gaps

### Verification performed

- `uv run pytest` with no `TEST_DATABASE_URL` → identity/parser/registry pass,
  DB tests skip.
- `TEST_DATABASE_URL=… uv run pytest` → **36 passed** against real Postgres 16.
- `alembic check` → "No new upgrade operations detected". Caught a real drift
  first time: the ORM declared a unique *constraint* while `0003` created a
  unique *index*.
- `alembic downgrade -1 && alembic upgrade head` on data with a real overlap:
  8 links → 6 after downgrade, demonstrating the documented lossiness.
- **Race test:** 8 concurrent byte-distinct uploads sharing two transactions →
  8×201, 10 transactions (not 24), 24 links, each shared row stored once and
  linked to all 8 statements, `new_transaction_count` summing to exactly 10.
  This is the `ON CONFLICT` path, not the sha256 fast path.
- `ruff check` / `ruff format --check` clean.

## Next

- [ ] First real institution adapter, with an anonymized fixture. This is the
      point of the project and everything above now exists to support it.
- [ ] PDF adapter (`StatementFile.is_pdf` already detects by magic bytes; no
      PDF dependency is wired up yet).
- [ ] Pagination metadata on `/transactions` (total count alongside the rows).
