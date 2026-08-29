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

## Milestone 4 — first real institution adapter (done)

Everything above was exercised only by `dummy_bank`, a layout we invented, so it
fit the normalized schema by construction. `revolut` is the first format we do
not control, and it is what turns the earlier design decisions from guesses into
answers.

Decisions: only `COMPLETED` rows become transactions; a `Fee` becomes a second
transaction rather than being dropped or folded into the amount; detection
matches a required *subset* of the header, not equality; `account_ref` falls back
to `Product`, which is well defined only while the service is single-tenant.

- [x] `parsers/csv_fields.py`: `normalize_header` / `to_decimal` / `to_date`
      extracted from `dummy_csv`, so a second adapter cannot re-derive its own
      error handling and let a bare `ValueError` escape as a 500
- [x] `parsers/revolut_csv.py` + registration in `parsers/__init__.py`
- [x] Anonymized fixtures: `revolut_statement.csv` (8 source rows, balances
      reconciling to the penny with fees included), `revolut_overlap.csv`,
      `revolut_malformed.csv`
- [x] 8 parser tests + a registry regression guard on adding a second adapter
- [x] Persistence test proving milestone 3's dedupe on a real format
- [x] README: "What a real format forces", refreshed Known gaps

### Verification performed

- `uv run pytest` with no `TEST_DATABASE_URL` → **35 passed, 11 skipped**
  (was 26/10). Confirms `test_dummy_parser.py` survives the helper extraction.
- `TEST_DATABASE_URL=… uv run pytest` → **46 passed** against real Postgres 16.
- `alembic check` → "No new upgrade operations detected". No migration was
  expected in this milestone; one appearing would have meant the plan was wrong.
- HTTP end to end: upload → `transaction_count` 8 / `new_transaction_count` 8;
  the overlapping re-download → 6 / **2**; unfiltered `/transactions` returns
  **10**, not 14; `?statement_id=` returns 8 and 6; the shared fee row lists both
  statement ids; byte-identical re-upload still 409; `dummy_bank` still routes to
  `dummy_bank` and `/parsers` lists both.
- **Ledger reconciliation:** the stored GBP movements sum to exactly the balance
  delta across the two files (1250.00 → 3123.65). That only holds because fees
  are stored as transactions.
- `ruff check` / `ruff format --check` clean.

## Milestone 5 — PDF adapter (done)

The last format the contract claimed to support without ever proving it.
`StatementFile` detected PDFs by magic bytes from milestone 1, but nothing
parsed one, so the `supported_formats` machinery had never been exercised.

Decisions: `pdfplumber` over a text-only extractor, because word coordinates are
what distinguish a debit column from a credit one; the registry is keyed on
(institution, format), so `dummy_bank` gets one adapter per layout rather than
one class with a branch in it.

- [x] `pdfplumber` dependency; `StatementFile.pdf_words` / `.pdf_text`, cached so
      detection cost does not scale with the parser set, and a `Word` value type
      carrying `x0`/`x1`/`top`
- [x] `ParserRegistry.register` accepts a repeated institution when the adapters'
      formats are disjoint; same institution *and* format still fails at import
- [x] `parsers/dummy_pdf.py`: column geometry read off the statement's own header
      row, money cells matched on content *and* position, wrapped narratives
      folded in by vertical proximity, summary lines and footers dropped, a row
      in two money columns raised rather than guessed
- [x] `tests/fixtures/generate_dummy_bank_pdf.py` — stdlib-only, committed
      alongside the PDF it writes so the fixture is reviewable, and reused by
      tests to synthesize edge-case PDFs
- [x] 9 PDF parser tests, 3 registry tests, 1 cross-format persistence test
- [x] README: "What a PDF forces", revised registration decision, Known gaps

### Verification performed

- `uv run pytest` with no `TEST_DATABASE_URL` → **47 passed, 12 skipped**.
- `TEST_DATABASE_URL=… uv run pytest` → **59 passed** against real Postgres 16.
- `alembic check` → "No new upgrade operations detected". No migration expected;
  this milestone touches no schema.
- Fixture regeneration is deterministic: re-running the generator produces a
  byte-identical PDF.
- **Cross-format dedupe, end to end.** Upload `dummy_bank_statement.csv` (4/4),
  then `dummy_bank_statement.pdf` (7 rows, **5 new**) → 9 transactions stored,
  not 11. The two shared rows each list both statement ids, and `?statement_id=`
  still returns 4 and 7. The same transaction arriving as a CSV cell and as a
  word at a position on a page is stored once.
- `/parsers` lists three adapters, `dummy_bank` twice with disjoint formats.
- Byte-identical PDF re-upload → 409; a PDF that is not a statement → 422.
- `ruff check` / `ruff format --check` clean.

## Next

- [ ] Pagination metadata on `/transactions` (total count alongside the rows).
- [ ] Check the Revolut header against a real export — it is reconstructed from
      the published format, and a mismatch means every upload 422s.
- [ ] A real institution's PDF. `dummy_pdf` is built against a fixture we
      generate, so its layout assumptions (a header row, one line per
      transaction plus wraps) have not met a real statement.
