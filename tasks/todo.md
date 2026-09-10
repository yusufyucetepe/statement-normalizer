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

## Milestone 10 — the second real institution (Wise CSV)

The point of a second institution is not another adapter. It is finding out which
of the first adapter's decisions were about statements in general and which were
about Revolut. The answer was uncomfortable, and it is the whole milestone.

**The finding.** Wise's balance statement looks like Revolut's — signed `Amount`,
`Running Balance`, a fee column beside them. Reading the fee column the way
Revolut's is read double-counts every fee in the file: Wise has already accounted
for it, either as its own row (`"Wise Charges for: TRANSFER-…"`, sharing the
transfer's id) or folded into `Amount` on a card payment. On the six-row fixture,
Revolut's rule lands the closing balance €2.57 below the one Wise printed. A
wrong number on a file that parses cleanly — the failure this project exists to
prevent, and it came from reusing a decision rather than re-taking it.

**The second finding, which killed the original plan.** This milestone was going
to key `dedupe_key` on Wise's `TransferWise ID`, attacking the top-flagged risk
in Known gaps (identity leaning on the description). Reading real exports showed
the id is **not row-unique**: a transfer and its charge row carry the same one.
Keying on it would have collapsed the fee into its parent — data loss, silently.
The id is a transaction *group*, so that work is now its own item below with the
constraint written down.

Decisions: no synthetic fee rows, argued in `_to_transaction` and provable from
the file's own arithmetic; `REQUIRED_COLUMNS` is the *intersection* of the three
shipped vintages (19/20/23 columns), anchored on `transferwise_id`; dates are
`%d-%m-%Y` day-first, not `Date Time` (absent from the old vintage);
`account_ref` is the statement currency, not `Payee Account Number` — which is
the counterparty's and would file every transaction under whoever was paid;
`Payment Reference` is appended to the description, because two payments to the
same payee on the same day are otherwise identical and identity would merge them.

- [x] `parsers/wise_csv.py`, registered and re-exported
- [x] `csv_fields.dict_rows` — the row iterator was about to be copied a second
      time, which is what that module exists to prevent; `revolut_csv` now uses it
- [x] Fixtures: `wise_statement.csv` (old vintage, balances reconcile through a
      real charge row), `wise_new_format.csv` (23-column vintage),
      `wise_overlap.csv`, `wise_malformed.csv`
- [x] 10 parser tests, 1 registry test (two subset-matching adapters must still
      route unambiguously), 1 persistence test (overlap dedupe on Wise + the two
      institutions staying out of each other's totals)
- [x] README: "What a *second* real format forces", refreshed Known gaps
- [x] No migration — nothing about the schema changed

### Verification performed

- `uv run pytest` with no `TEST_DATABASE_URL` → 60 passed, 34 skipped.
- `TEST_DATABASE_URL=... uv run pytest` → **94 passed** against real Postgres 16.
- `alembic check` → "No new upgrade operations detected".
- The fee decision, checked against the file's own arithmetic: opening + the sum
  of parsed amounts = 1257.78 = the closing `Running Balance`. Synthesizing fee
  rows lands it at 1255.21.
- HTTP end to end: `/parsers` lists four adapters; `wise_statement.csv` → 6 new,
  `wise_overlap.csv` → 6 rows / 2 new, `wise_new_format.csv` → 2 new, all
  `account_ref: EUR`; `?institution=wise` → 10 and `?institution=revolut` → 8,
  so the two institutions do not merge.
- `ruff check` / `ruff format --check` clean.

### Format sources

Header, vintages and day-first dates corroborated across `ofxstatement-
transferwise`, `jlabath/wiseconvert` (`%d-%m-%Y`), `BananaAccounting/Universal`,
`OSadovy/uabean` and `metabrainz.org`. The fee behaviour was read off real
committed exports (`kedder/ofxstatement-transferwise` sample, `uabean` fixtures)
where `787.62 - 11.35 = 776.27` settles it.

## Milestone 11 — identity on the institution's own id (done)

Closes the risk the README had called "the part most likely to need revisiting":
the fingerprint's weakest element was the description, which is the part of a row
an institution feels free to reword between exports. A reworded row fingerprints
differently and is stored again — the same double-count `0003` exists to prevent,
arriving by a different route.

Adapters whose export publishes an id now pass `external_id`, and the fingerprint
uses it *in place of the description* for those rows.

Decisions: the id replaces the description but **not** the rest of the payload,
because an institution's id is not row-unique (milestone 10's finding: Wise gives
a transfer and its fee the same one, and keying on the id alone would merge them);
the shape marker (`v1`/`v2`) is hashed in as the first element, so the two are
different payloads rather than two spellings of one and can never collide; `v1`
is frozen and pinned to a literal digest in the tests; a blank id column is
normalized to `None` rather than becoming an id that is the empty string; an
external id does **not** lift the `account_ref` requirement, since institutions
promise ids are unique within an account, not across every account they hold.

- [x] `Transaction.external_id`, with a validator making a blank cell no id
- [x] `identity._fingerprint` branches on it; `v1` payloads byte-identical
- [x] Migration `0004`: nullable `transactions.external_id`, no backfill possible
- [x] `wise` passes `transferwise_id`; the upload path persists the column
- [x] `wise_redescribed.csv` — the same six transactions, every description
      reworded — plus 6 identity tests, 1 parser test, 1 persistence test
- [x] README: "When the institution publishes its own id", refreshed Known gaps

### Verification performed

- `uv run pytest` with no `TEST_DATABASE_URL` → 69 passed, 35 skipped.
- `TEST_DATABASE_URL=... uv run pytest` → **104 passed** against real Postgres 16.
- `alembic check` → "No new upgrade operations detected"; `downgrade base` then
  `upgrade head` round trips clean.
- **`v1` keys proven unchanged**, three ways: the digest from the committed
  pre-change code, the digest from the new code, and a sha256 of the payload
  written out by hand all equal
  `6d76b2f1…e359c91`. That constant is now pinned by `test_identity.py`.
- The payoff, measured: re-uploading the reworded export stores **6 of 6** rows
  again when keyed on the description, **0 of 6** when keyed on the id.
- HTTP end to end: `wise_redescribed.csv` → `transaction_count 6`,
  `new_transaction_count 0`, all six rows linked to both statements; Revolut
  unaffected, `external_id: null`, still 8 rows.
- `ruff check` / `ruff format --check` clean.

## Milestone 12 — a way to check a real file (done)

Both remaining items need a statement from an actual account, and the reason
neither had moved is that the obvious way to do them is wrong: dropping a real
export into `tests/fixtures/` publishes an account history, and scrubbing one so
the balances still chain is fiddly enough to be its own task. Verifying an
adapter and committing a fixture are separate jobs, and only the second needs
the file to be in the repo at all.

`scripts/inspect_real_file.py` does the first: point it at a file anywhere on
disk and it reports detection, layout and — where an adapter claims the file —
a parse with a balance reconciliation. Descriptions and cell values are redacted
to a length by default, so the output can be pasted into an issue.

- [x] Detection: per adapter, claimed or the missing columns in terms of that
      adapter's own published rule (`REQUIRED_COLUMNS` / `HEADER` / `MASTHEAD`);
      a `can_parse` that raises is reported as the contract violation it is
- [x] CSV: quote-aware delimiter sniff, preamble rows above the header, ragged
      rows, per-column inferred kind and fill rate, latin-1 fallback warning
- [x] CSV: whether the file's own dates *prove* a day/month order, or whether
      no component exceeds 12 and the order is unprovable from the file
- [x] PDF: text layer or scanned image, encrypted, truncated, not-a-PDF; the
      header row with each word's x centre; money-word clusters, which is what
      says whether the layout has debit/credit columns or one signed column
- [x] Parse: counts, account_ref, currencies, date range, direction split,
      external_id coverage, and a per-currency balance chain that accumulates
      across rows carrying no balance (the fee-split case)
- [x] `--assume <institution>` to separate "the header rule is wrong" from
      "the whole format is wrong"; `--show-values` to opt out of redaction
- [x] Containers identified by magic bytes rather than read as text: `.xls`
      (OLE2), `.xlsx`/`.ods` (zip of XML, told apart by what is inside), a
      plain zip, and an HTML table served under an `.xls` name. Each says what
      to do instead, with the warning that a spreadsheet round trip rewrites
      dates into the machine's locale — the failure this project cares about
      most, since it is wrong rows rather than an error
- [x] README: "Checking a real export first" under the adapter pattern

Verification: run against all 18 fixtures plus hand-built adversarial files —
a latin-1 semicolon export with three preamble rows and a quoted comma, an
empty file, a scanned image PDF, a truncated PDF, and a non-PDF named `.pdf`.
Two bugs surfaced by running it against fixtures whose answers were already
known: a raw comma count called a quoted payee name a preamble row, and the
balance check compared EUR balances against GBP ones and treated the Revolut
fee split as a break. Both fixed; the Revolut and Wise fixtures now reconcile
at every checkable point. 69 passed / 35 skipped, ruff clean.

## Findings from the first real export (2026-09-05)

A Turkish bank's `Hesap Hareketleri` CSV — the first file from an actual account
to go through anything here. It was not inspected for its own sake: it broke
three of the script's assumptions and found one live bug in the project.

**`to_decimal` silently mis-parses European numbers.** It strips commas and
keeps dots, so `1.204,55` becomes `1.20455` and `-42,90` becomes `-4290` — a
wrong number, out by a factor of a thousand, on a file that parses cleanly and
stores without complaint. No institution shipped here uses that convention, so
nothing is currently broken; the next European adapter written without noticing
would be. **Unfixed** — the fix is a decision, because the convention cannot be
inferred per cell (`1.234` is either) and so has to be declared by the adapter.
This file itself uses `1,234.56` and is unaffected. **Fixed in milestone 14.**

**Every row is padded to the widest one**, so the header sits under 11 rows of
greeting and account metadata that are all still 5-field rows. Preamble
detection compared field counts and found nothing; it now compares *non-empty*
counts and picks the candidate that reads like column titles.

**A misidentified header row printed the account holder's name** into output
whose whole purpose is to be safe to paste. Column names print verbatim because
they are format rather than content — but only once the row is established as
column names, so an unrecognized header is now redacted like any other cell.

**Money has no fixed decimal places**: `450` for 450.00, `12,345.6` for
12,345.60. A rule demanding two decimals read three columns of money as text.
Column kinds are now a majority vote, which also surfaces the four footer rows
sitting inside the date column as `odd` rather than hiding the column's type.

Columns, for whenever an adapter is written: `Tarih` (dd.mm.yyyy, day-first
proven), `Fiş No` (a per-row id — an `external_id` candidate), `Açıklama`,
`İşlem Tutarı` (signed, sign carries direction), `Bakiye` (running balance).
45 transactions, 5 preamble rows, 4 footer rows, UTF-8.

## Milestone 13 — the first adapter from a real account (`ziraat`, done)

Ziraat Bankası's `Hesap Hareketleri` CSV. Not chosen for its own sake: it was
the one real download available, and the point was to find out how much a
published format description leaves out. The answer is most of what mattered.

Decisions, each argued where it is taken:
- [x] `table_rows` in `csv_fields`: hand an adapter the rows before a header is
      assumed. The table starts on line 6 under a block of account metadata, and
      every row is padded to the widest, so `DictReader` reads the greeting as
      the header and nothing about the shape says otherwise
- [x] A row is a transaction iff it starts with `dd.mm.yyyy`. Below the table sit
      a totals line and four lines of boilerplate, all padded to full width — and
      the totals line writes `Borç:-2.262,07` in the European convention while
      every transaction row writes `-2,262.07`, so reading it would be out by a
      factor of a thousand with no error
- [x] Reverse the file: the export is newest-first, and `balance_after` is only
      a running balance read forwards. The decision most worth disagreeing with,
      and the cost — statement order no longer matches file order — is stated
- [x] Detection is the columns *and* an IBAN with bank code `00010`. `Tarih`,
      `Açıklama` and `Bakiye` are what every Turkish bank calls those columns;
      matching on them alone would file a competitor's export under this name
- [x] Synthesize nothing: `KOMİSYON`, `BSMV` and the message fee are already
      rows sharing the transfer's `Fiş No`. Wise's rule, not Revolut's, and the
      balance reconciliation inside the file is the evidence
- [x] `account_ref` is the IBAN — the first account scope here that survives the
      service growing users
- [x] Currency is read once for the document and mapped `TL` -> `TRY`; a
      statement that does not say raises rather than defaulting
- [x] `external_id` is `Fiş No`, a transaction *group* id like Wise's
- [x] `fold_header` in `csv_fields`: Turkish `İ` lowercases to `i` plus a
      combining dot above, so `"i̇şlem_tutarı"` in source is one invisible
      codepoint from the real key. Folding to ASCII makes the constants
      greppable and therefore reviewable

Verification: 87 passed / 38 skipped with no DB; **125 passed** against real
Postgres 16; `alembic check` clean and no migration needed. Against the actual
download: detection claims it and nothing else does; 45 transactions parsed,
all 45 carrying an id (24 distinct — a group id, as designed); the balance chain
reconciles at all 44 points; upload stores 45 and the identical bytes return
409. An earlier download of the same account, built by dropping the 10 newest
rows, reports **0 new** and leaves the total at 45 — overlap dedupe proven on a
real account rather than a fixture.

Also fixed: the inspection script printed `account_ref` verbatim, which for this
institution is an IBAN. It is redacted to a shape now, like every other value.

## Milestone 14 — the decimal convention becomes a declaration (done)

The bug recorded above, taken seriously. `to_decimal` stripped commas and kept
dots, so `1.204,55` returned `1.20455` and `-42,90` returned `-4290`: not an
error, a plausible number a thousand times off, on a file that parsed cleanly
and stored without complaint. Nothing shipped was affected — all five adapters
write Anglo — which is exactly what made it worth fixing before the adapter that
would have been.

- [x] `DecimalConvention` (`ANGLO` / `EUROPEAN`) in `csv_fields`, beside the
      grammar that interprets it. The convention cannot be inferred from a cell
      — `1.234` is 1234 one way and 1.234 the other — so the adapter declares
      it, because the adapter is the only thing that knows the institution
- [x] `to_decimal` takes `convention` keyword-only **with no default**. A default
      would be a guess wearing a reassuring name, and the failure mode of a guess
      here is a wrong number rather than an exception
- [x] A total grammar per convention rather than a `replace()`: a grouping
      separator is followed by exactly three digits, and the decimal separator
      appears once and after all of them. This is what makes a *wrong*
      declaration loud — `1,20455` under `ANGLO` and `2,262.07` under `EUROPEAN`
      both raise, naming the row and the column like every other cell error here
- [x] `decimal_convention: ClassVar[DecimalConvention]` on `StatementParser`,
      annotated without a value like `institution`. No `__init_subclass__`
      enforcement: forgetting is already loud (a required keyword), and the real
      hazard is declaring *wrong*, which only the grammar and a test can catch
- [x] All five adapters declare `ANGLO`; nine call sites thread it through. No
      behaviour changes — every money cell in all 18 fixtures already satisfies
      the strict grammar, checked before the first line was written
- [x] `tests/test_csv_fields.py`, new: `to_decimal` had no direct test at all,
      only coverage through adapters
- [x] The Ziraat totals line is no longer merely skipped. A test parses it as
      `EUROPEAN` and reconciles it against the same statement's rows parsed as
      `ANGLO` — the only real bank string the European branch has, and it sits
      in the same file as its opposite

Verification: 120 passed / 38 skipped with no DB — including all four existing
adapter modules untouched, which is the regression that matters, since the
fixtures are the evidence that the strict grammar accepts every real Anglo value
already shipped. **158 passed** against real Postgres 16; `alembic check` clean
and no migration (this changes parsing, not storage). Against the actual Ziraat
download: still 45 transactions, balance chain still reconciles at all 44 points,
upload 201 then 409, total 45 — every number unmoved. And the check the fixture
could only imitate: the bank's own totals line, in the European convention,
reconciles to the kuruş against 45 rows read in the Anglo one.

## Findings from the first real PDFs (2026-09-09)

Two statements downloaded from actual accounts, run through
`scripts/inspect_real_file.py`. Neither produces an adapter, and both are worth
recording, because a negative answer about a format is still an answer about
the format.

**Ziraat's PDF export has no text layer.** Two pages and zero extracted words,
against `/Font 0` and five embedded images. The bank renders the statement to an
image and wraps it in a PDF. `pdfplumber` returns pages with nothing on them,
so no adapter can ever read it and no amount of layout work would change that —
it needs OCR, which this project does not do. This closes the question for
Ziraat specifically: the CSV/XLSX route that `ziraat_csv` already parses is not
one option among several, it is the only machine-readable export the bank
offers. The script's scanned-PDF branch, written blind in milestone 12, printed
exactly the right thing on the first real file to hit it.

**Wise's PDF is a `Statement of fees`, not a statement of transactions.** It has
a healthy text layer — 4 pages, 691 words — but the table is service names
against usage counts (funding fee, receiving fee, card payment fee), with no
dates and no transactions. It is the annual fee disclosure, a different document
that happens to live under the same download menu. A transaction statement has
to be exported per balance rather than per account, which is why the CSV was
hard to find.

**So `dummy_pdf`'s layout assumptions are still untested.** Neither file is a
transaction PDF, so the open question — whether reading debit and credit off
column geometry survives contact with a real bank — stands unanswered. What the
two files did establish is the *failure* path: both are correctly claimed by
nobody and would return 422 rather than a partial parse or a 500, and the
text-layer-less one does not raise anywhere in detection.

**A small corroboration.** The Wise document writes money with a dot decimal
separator on a Turkish-resident account, where the local convention is a comma.
`wise_csv`'s `decimal_convention = ANGLO` was declared in milestone 14 from
published documentation; this is the first evidence for it from a document Wise
actually produced.

## Milestone 15 — one analytics endpoint (`monthly-totals`, done)

`GET /transactions/monthly-totals`: money per month, split by currency and by
direction, with the row count per group. The whole of the analytics work, per the
freeze below.

- [x] **The institution filter never touches `statement_transactions`.** This is
      the entire risk in the endpoint. Filtering the aggregate by joining the link
      table would match a shared transaction once per statement holding it and
      inflate every sum by exactly the overlap — milestone 3's double-count,
      returning where the output is nothing but numbers and a wrong total looks
      exactly like a right one. `transactions.source_institution` is denormalized
      onto the row (`tables.py:111`, indexed) and `GET /transactions` already
      filters on it the same way, so the aggregate cannot multiply a row.
- [x] **The test that is the reason to build this rather than assume it.**
      `test_monthly_totals.py` uploads `revolut_statement.csv` and
      `revolut_overlap.csv` — 10 transactions across 14 links — and asserts the
      institution-filtered totals equal the unfiltered ones. Verified by
      sabotage: swapping the predicate for the join turns 8.40 EUR into 16.80 and
      179.55 GBP into 229.90, and the test fails on the first assertion.
- [x] Currencies are never summed together and debits are never netted against
      credits. `amount` is a magnitude with the sign in `direction`, so either
      would be a number with no meaning.
- [x] Aggregation in Postgres (`date_trunc`, `GROUP BY`), not a full table read
      summed in Python.
- [x] Filters are `date_from`, `date_to` and `institution` — the sibling
      endpoint's vocabulary, minus `statement_id` (which would have to join) and
      with nothing new invented. An earlier draft of this spec said `account_ref`;
      that would have been new vocabulary, since `GET /transactions` does not
      take it.
- [x] **Response envelope decided rather than defaulted.** Keyed `items` like the
      two list endpoints, so all three read the same way — but not `Page`, and
      not a bare array. No `limit`/`offset`: an aggregate is not a page, and for a
      real page those fields answer "is there more", which here would only ever
      be `len(items)`.
- [x] No migration: this reads what is already stored. `alembic check` agrees.

### Verification performed

`166 passed` against Postgres 16 (was 158; the 8 new tests), `120 passed, 46
skipped` with no database, ruff clean, `alembic check` clean. The double-count
guard was checked by deliberately breaking it, not only by watching it pass.

### A claim that was wrong, recorded so it is not repeated

While specifying this I said `ix_transactions_date_direction` covers two of the
three group-by columns. It does not. A btree on `(date, direction)` serves a
range filter on `date`, but Postgres will not use it to satisfy
`GROUP BY date_trunc('month', date)` — it has no way to know the function
preserves order, so it scans and hash-aggregates regardless. At these row counts
that costs nothing and nothing was changed, but the claim itself is false.

## Next

- [ ] Check the Revolut adapter against a real export — the header is confirmed
      against the published format and third-party importers, but no download
      from an actual account has been through it. **Unblocked by
      `scripts/inspect_real_file.py`**: Revolut app → the account → Statement →
      the Excel/CSV option → run the script on it. A range with an ATM
      withdrawal or an exchange is worth more than a long one, since the fee
      split is the decision most able to be wrong. Verification of an adapter
      that already ships, so it survives the freeze — but it needs a download.

## Scope freeze (2026-09-10)

Everything in this section is a **closed question**, not a backlog. The cost that
matters is no longer in the code, it is in the length of the open list and in how
much someone has to read before they understand what this is. Each item below is
recorded with its reason so it does not get reopened by whoever reads the repo
next, including me.

**The registry is frozen at five adapters.** "Which institution next" is closed.
Three real institutions already found the things only real files find — rows
padded to a uniform width, a table starting on line 6, money without fixed
decimal places, two decimal conventions inside one document. A fourth would
mostly re-find them, and each one adds a fixture, a test module and a published
format to keep true.

**`scripts/inspect_real_file.py` is frozen.** No new container types, no new
heuristics. It has already paid for itself twice over — two bug fixes and the
entire `ziraat` adapter came out of it — and it is now the size where every
branch added is a second product growing inside `scripts/`.

**Multi-tenancy is not being built.** It is the one real architectural gap and it
stays a documented gap. The reason is that it is not a feature, it is four
coupled changes: auth, a user model, a migration, and a rescoped `dedupe_key`.
The answer, if it is asked:

> The service is single-tenant by construction. Transaction identity is
> `sha256(marker|institution|account_ref|date|direction|amount|currency|identity|occurrence)`
> under a unique index on that hash alone, so it has an account dimension but no
> owner dimension — two people uploading the same export would collide as
> duplicates of each other rather than as two people's money. Making it
> multi-tenant means a `user_id` on statements and transactions, that column
> folded into both the fingerprint and the unique indexes, and auth to populate
> it — which is why it is written down as the known gap rather than half-built.

**Analytics stops at the one endpoint above.** No categories, no tagging, no
budgets, no rules engine. That is the line where this stops being a statement
normalizer with a defensible boundary and becomes a personal finance app, and a
half-built personal finance app is worth less than a finished normalizer.

**No frontend.** `/docs` is the interface. FastAPI generates it from the same
schemas the endpoints validate against, so it cannot drift from the API the way
a hand-written client would.

**No Redis, no Celery, no auth.** Standing constraint since milestone 1, restated
here because this is now the list people will read.

**A real PDF with a text layer and transactions in it.** Held open since
milestone 5 to test whether `dummy_pdf`'s column geometry survives a real bank.
Two candidates on 2026-09-09 answered neither way — Ziraat renders its export to
an image, Wise's is a fee disclosure — so it is not blocked on effort but on
finding an institution that happens to publish one, which is not something we can
go and do. It stays in the README's known gaps, where it belongs: a limit of the
adapter, not a task.
