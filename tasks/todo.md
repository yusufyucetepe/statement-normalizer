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

## Milestone 6 — pagination metadata (done)

Decision: an envelope (`{items, total, limit, offset}`) rather than an
`X-Total-Count` header. The header keeps the body a bare array and so is not a
breaking change, but it is invisible in OpenAPI and in every generated client,
and `total` is not metadata about the transport — it is part of the answer.
The endpoint has no external consumers yet, so this is the cheapest moment it
will ever be to change the shape.

`total` comes from a second `COUNT` query rather than `count(*) OVER ()` beside
the rows: the window function returns the total *on each row*, so an offset past
the end returns no rows and therefore no total — the one request that most needs
one.

- [x] `TransactionPage` schema; `GET /transactions` returns it
- [x] Count query built from the same filtered select, with ordering stripped
- [x] `tests/test_transactions_api.py` — 6 tests, including the offset-past-the-end
      case and a full walk proving paging visits every row exactly once
- [x] Existing `/transactions` assertions moved to `["items"]`
- [x] README: the envelope and what it costs; Known gaps swaps "no pagination
      metadata" for offset-vs-keyset

### Verification performed

- `uv run pytest` with no `TEST_DATABASE_URL` → **47 passed, 18 skipped**.
- `TEST_DATABASE_URL=… uv run pytest` → **65 passed** against real Postgres 16
  (59 before, plus the 6 new).
- `alembic check` → "No new upgrade operations detected". No schema change.
- HTTP end to end: two statements (4 + 2 rows), then `?limit=2` →
  `total 6, limit 2, offset 0` with 2 items; `?offset=100` → `total 6` with 0
  items; `?direction=debit` → `total 3`. `/openapi.json` resolves the 200 to
  `#/components/schemas/TransactionPage`.
- `ruff check` / `ruff format --check` clean.

## Milestone 7 — verify the Revolut format against the published spec (done)

The header was reconstructed from memory and flagged as the top risk in the
project: a mismatch means every Revolut upload 422s. Checked it against
Revolut's published format and three independent third-party importers.

**The header is correct.** All ten column names match exactly, in order, and the
`%Y-%m-%d %H:%M:%S` timestamp format is confirmed by real sample data. Nothing to
change there.

**The check found a different bug, and a worse one.** Revolut's crypto/trading
export uses the same header *plus* four columns (`Fiat amount`, `Fiat amount
(inc. fees)`, `Base currency`, and an extra ordering). Our detection is a
required-subset match, so it claimed that file — and its `Amount`, `Currency` and
`Balance` are the *asset* (`100.0000`, `EOS`), with the money in `Fiat amount`.
Demonstrated against the pre-fix code: `100 EOS` stored as a credit of 100, a fee
denominated in SEK stored as EOS, all under `revolut|Current` — the same account
scope as the real fiat export. `EOS` is three uppercase letters, so currency
validation passed and nothing downstream would ever have flagged it.

- [x] Header, column order and datetime format verified against published format
      + `tariochbctools`, `ofxstatement-revolut`, `revolutax`
- [x] `TRADING_COLUMNS` names the columns that mark the crypto export; detection
      declines a header containing them
- [x] `tests/fixtures/revolut_crypto.csv` — the real trading header, synthetic rows
- [x] 2 tests: the adapter declines it *and* the file genuinely contains every
      required column (so the rejection is from what it adds, not what it lacks);
      the registry claims it for nobody
- [x] README: the hole in subset detection, and what it stored before the fix

### Verification performed

- `uv run pytest` with no `TEST_DATABASE_URL` → **49 passed, 18 skipped**.
- `TEST_DATABASE_URL=… uv run pytest` → **67 passed** against real Postgres 16.
- Pre-fix behaviour reproduced directly by clearing `TRADING_COLUMNS`: detection
  returns True and four bogus transactions come out. With the fix, False.
- `ruff check` / `ruff format --check` clean.

## Milestone 8 — reading statements back (done)

Uploaded statements were write-only. `POST /statements/upload` returns an id and
nothing in the API could retrieve it afterwards: a client that dropped the id had
exactly one way back, re-uploading the same bytes to read it off the 409. The
milestone-3 headline number, `new_transaction_count`, was visible for one
response and then unreachable.

Decisions: a generic `Page[ItemT]` subclassed per endpoint, so the two list
endpoints cannot drift into two different envelopes while OpenAPI keeps calling
them `TransactionPage` and `StatementPage`; the `count(*)`-vs-window reasoning
moved into one `count_matching` helper rather than being copied; `404` for an
unknown statement id even though `GET /transactions?statement_id=` returns an
empty page for the same id (a filter matching nothing is not a missing thing);
`institution` is the only filter — `format` is in the response and is not a real
access path.

- [x] `models/schemas.py`: generic `Page[ItemT]`, `TransactionPage`, `StatementPage`
- [x] `api/paging.py`: `count_matching`, used by both list endpoints
- [x] `GET /statements` — newest first, `institution` / `limit` / `offset`
- [x] `GET /statements/{statement_id}` — 404 when unknown
- [x] 9 tests in `tests/test_statements_api.py`
- [x] README: both endpoints, why 404 vs empty page, why the `id` tiebreaker;
      Known gaps gains "a statement cannot be deleted"
- [x] No migration — nothing about the schema changed, and `alembic check` agrees

### Verification performed

- `uv run pytest` with no `TEST_DATABASE_URL` → 49 passed, 27 skipped.
- `TEST_DATABASE_URL=... uv run pytest` → **76 passed** against real Postgres 16.
- `alembic check` → "No new upgrade operations detected".
- HTTP end to end: three uploads listed newest first with distinct
  `uploaded_at`; `?institution=` → totals 2 and 1; `?offset=100` → `[]` with
  `total: 3`; detail by id → 200 and `new_transaction_count: 2` on the overlap
  statement, matching its upload response; unknown id → 404; non-UUID → 422.
- OpenAPI: schema names are `StatementPage` / `TransactionPage` — the generic did
  not leak `Page_TransactionRead_`, so milestone 6's schema name is unchanged.
- `ruff check` / `ruff format --check` clean.

### The test that failed first

`test_statements_are_listed_most_recent_first` failed against real Postgres, and
the cause was not the ordering. `uploaded_at` defaults to `now()`, which is the
*transaction* timestamp, and the test harness runs every upload inside one
rolled-back transaction — so all three rows shared a timestamp and fell through
to the `id` tiebreaker. Production never produces that tie (one upload is one
transaction, confirmed by the distinct times over HTTP above), so the test now
stamps distinct times rather than trusting the fixture, and the tie the harness
does produce is what `test_paging_visits_every_statement_exactly_once` exercises.

## Milestone 9 — deleting a statement (done)

Closes the gap milestone 8 documented: uploading the wrong file was permanent
short of opening a psql shell. The lifecycle now closes — upload, list, read,
delete.

The semantics were the work, not the SQL. There is no `statement_id` column on
`transactions` to delete by: overlapping statements share rows, so dropping
everything the statement contained would silently shorten its neighbours, and
dropping nothing would leak every row it introduced. The rule that keeps both
`/transactions` and `?statement_id=` honest is that a transaction goes when its
*last* statement does.

Decisions: a `200` with `{deleted, retained}` counts rather than a bare `204`,
because the split depends on what the other statements hold and the caller
cannot derive it; the statement row is deleted first so its links cascade away
and the orphan sweep asks about the statements that *remain*; a second delete of
the same id is `404`, not an idempotent `204`, since the id no longer names
anything and pretending otherwise hides a confused client.

- [x] `StatementDeleted` schema
- [x] `DELETE /statements/{statement_id}`, orphan sweep via a correlated
      `NOT EXISTS` over the surviving links
- [x] 6 tests, including the shared-row case and delete-then-re-upload
- [x] README: "Deleting a statement", the named race, refreshed Known gaps
- [x] No migration — `ON DELETE CASCADE` was already in `0003`

### Verification performed

- `uv run pytest` with no `TEST_DATABASE_URL` → 49 passed, 33 skipped.
- `TEST_DATABASE_URL=... uv run pytest` → **82 passed** against real Postgres 16.
- `alembic check` → "No new upgrade operations detected".
- HTTP end to end: two overlapping uploads → 6 transactions, 4 apiece. Deleting
  January returned `deleted 2 / retained 2`, left 4 transactions, and the
  overlap statement still reported all 4 of its own rows. `GET` and a second
  `DELETE` on the dead id → 404; re-uploading the same file → 201.
- OpenAPI: `delete` on `/statements/{statement_id}` with a `StatementDeleted` ref.
- `ruff check` / `ruff format --check` clean.

### The race, measured rather than assumed

An upload linking a transaction that a concurrent delete is collecting was the
one interleaving worth checking, so it was run by hand over two connections:
Postgres blocks the link insert on the delete's row lock, and when the delete
commits the upload fails its foreign key and rolls back whole. Loud, not silent
— no statement is left short a row. The upload's caller gets a `500`, which is
recorded in Known gaps as deserving a `409` instead.

## Next

- [ ] Check the Revolut adapter against a real export — the header is confirmed
      against the published format and third-party importers, but no download
      from an actual account has been through it.
- [ ] A real institution's PDF. `dummy_pdf` is built against a fixture we
      generate, so its layout assumptions (a header row, one line per
      transaction plus wraps) have not met a real statement.
