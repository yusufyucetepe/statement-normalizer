# statement-normalizer

A FastAPI service that accepts an uploaded bank or broker statement (CSV or PDF),
detects which institution produced it, parses it into one normalized transaction
schema, validates it, and stores it in Postgres.

**Status: CSV and PDF, two real institutions.** Upload → detect → parse →
validate → store is wired end to end and covered by tests against a real
Postgres. Four adapters are live: `revolut` and `wise` (CSV, against anonymized
copies of their exports) and `dummy_bank` in both CSV and PDF, the reference
implementations that demonstrate the contract. Adding an institution — or a
second format for one — means adding one file; see
[The adapter pattern](#the-adapter-pattern).

---

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) (pip works too).

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
cp .env.example .env
```

### Run the database and migrations

```bash
docker compose up -d db
uv run alembic upgrade head
```

### Run the API

```bash
uv run uvicorn statement_normalizer.main:app --reload
```

Interactive docs at http://localhost:8000/docs.

### Everything in Docker

```bash
docker compose up --build     # api on :8000, postgres on :5432
```

The `api` service waits for Postgres to pass its healthcheck, then runs
`alembic upgrade head` before starting uvicorn.

### Tests and lint

```bash
uv run pytest                 # parser + registry tests; DB tests skip
uv run ruff check .
uv run ruff format --check .
```

Parser and registry tests are pure functions over bytes and need no database.
The persistence tests skip unless `TEST_DATABASE_URL` is set:

```bash
docker compose up -d db
docker compose exec db createdb -U statements statements_test
TEST_DATABASE_URL=postgresql+psycopg://statements:statements@localhost:5432/statements_test \
  uv run pytest
```

Those tests run `alembic upgrade head` against that database, then wrap each
test in a transaction that is rolled back — so they exercise the real
migrations, real JSONB, and real `NUMERIC`, and still leave no residue.

---

## Endpoints

| Method | Path                 | Notes |
|--------|----------------------|-------|
| `POST` | `/statements/upload` | Multipart upload of a CSV or PDF. Detects, parses, validates and stores. **201** with the statement summary and a `Location` header; **409** if these exact bytes were uploaded before; **422** if no parser recognizes the file or a claimed file is malformed. |
| `GET`  | `/transactions`      | Filters: `date_from`, `date_to`, `direction` (`credit`/`debit`), `institution`, `statement_id`, `limit`, `offset`. Returns `{items, total, limit, offset}`. |
| `GET`  | `/statements`        | Uploaded statements, most recent first. Filters: `institution`, `limit`, `offset`. Same envelope. |
| `GET`  | `/statements/{id}`   | One statement. **404** if no statement has that id. |
| `DELETE` | `/statements/{id}` | Removes the statement and the transactions no other statement still holds. **404** if unknown. |
| `GET`  | `/parsers`           | The live adapters, in the order detection considers them. |
| `GET`  | `/health`            | Liveness. |

Try it:

```bash
curl -i -F "file=@tests/fixtures/dummy_bank_statement.csv" \
     http://localhost:8000/statements/upload
# HTTP/1.1 201 Created
# location: /transactions?statement_id=e86a38b2-...

curl "http://localhost:8000/transactions?statement_id=e86a38b2-..."
```

The upload response deliberately does **not** inline the transactions — a
statement can carry thousands of rows. It returns the summary and points at the
paginated `/transactions` endpoint instead.

Both list endpoints answer with an envelope rather than a bare array, because
`limit`/`offset` without a total is a half-answer: a client that receives a full
page cannot tell a last page from a middle one, and has to spend another request
finding out. `total` is the number of rows matching the filters *before*
`limit`/`offset`, so it is what a "showing 20 of 413" line needs.

```json
{ "items": [ ... ], "total": 413, "limit": 100, "offset": 0 }
```

It costs a second `COUNT` query per request. The alternative — `count(*) OVER ()`
carried on each row — is one round trip, but returns nothing at all when the
offset lands past the end, which is precisely the request that needs the total
most.

It carries two counts: `transaction_count` is how many rows were in the file,
and `new_transaction_count` is how many of those were not already stored by an
earlier statement. They differ when statement periods overlap — see below.

### Finding a statement again

`GET /statements` exists because an upload response is otherwise the only place
a statement id ever appears. Without it, a client that dropped the id had one way
back: re-upload the same bytes and read the id off the `409`. It is also the only
way to see `new_transaction_count` after the upload that produced it — the number
that says an overlapping re-download contributed almost nothing.

```bash
curl "http://localhost:8000/statements?institution=revolut"
curl "http://localhost:8000/statements/e86a38b2-..."
```

An unknown id is a `404` here, while `GET /transactions?statement_id=<unknown>`
is an empty page. The difference is deliberate: there, an id that matches nothing
is a filter that excluded everything; here it is a request for a thing that does
not exist.

Ordering is `uploaded_at` descending with `id` as a tiebreaker. The tiebreaker is
not decoration: `uploaded_at` defaults to Postgres' `now()`, which is the
*transaction* timestamp, so any two statements written in the same transaction
share it exactly and would otherwise page unstably.

### Deleting a statement

Deleting is not `DELETE FROM transactions WHERE statement_id = …`, because there
is no such column: overlapping statements share rows, and a shared row belongs to
all of them. Dropping every row the statement contained would silently shorten
the statements that overlap it; dropping none would leak every row it introduced.
The rule that keeps both queries honest is that **a transaction goes when its last
statement does**.

```bash
curl -X DELETE "http://localhost:8000/statements/e86a38b2-..."
# { "statement_id": "e86a38b2-...",
#   "deleted_transaction_count": 2, "retained_transaction_count": 2 }
```

It answers with a body rather than a bare `204` because that split is not
derivable by the caller — how much a delete actually removes depends on what the
*other* statements hold. Deleting January from an overlapping pair removes only
the rows February did not also contain, and February still reports its full four.

The statement row goes first, taking its links with it via `ON DELETE CASCADE`;
the orphan sweep that follows asks which of those transactions no *surviving*
statement references. Because `content_sha256` lives on the deleted row, the same
file can be uploaded again afterwards — a delete is undoable, not a blocklist.

One race is worth naming: an upload linking a transaction that a concurrent
delete is collecting. Postgres blocks the link insert on the delete's row lock,
and if the delete commits first the upload fails its foreign key and rolls back
whole — verified by hand against two connections. The upload's caller sees a
`500` and nothing is stored, which is a poor answer to a request but not a wrong
one: no statement is ever left short a row it should have.

### Re-uploading the same file

Uploads are deduplicated on the SHA-256 of the file contents, which carries a
unique index. A byte-identical re-upload returns `409` with the id of the
statement that already holds it:

```json
{ "detail": { "message": "this file has already been uploaded",
              "statement_id": "e86a38b2-...", "uploaded_at": "..." } }
```

The `SELECT` before the insert is only a fast path. The unique index is the
source of truth: two concurrent identical uploads both pass that check, and the
loser is caught as an `IntegrityError` and converted to the same `409`.

### Overlapping statements

File-level dedupe only catches the same *file* twice. The case that actually
corrupts totals is two different files sharing a period:

> January's export covers Jan 1–31. February's export covers Jan 15–Feb 15.
> The bytes differ, so both are accepted — and the Jan 15–31 transactions would
> be stored twice.

So transactions carry an identity of their own, independent of the file they
arrived in: a SHA-256 over
`institution + account + date + direction + amount + currency + description`,
stored as a unique `dedupe_key` (`models/identity.py`). Uploading an overlapping
statement stores only the rows it introduces:

```jsonc
{ "transaction_count": 40,       // rows in the file
  "new_transaction_count": 12 }  // rows not already stored
```

Three things this has to get right:

- **Genuine duplicates must survive.** Two identical £3.20 coffees on the same
  day are two real transactions. Repeats of the same fingerprint are numbered
  within the statement, so both are stored — and an overlapping statement
  containing the same two coffees numbers them the same way and matches both.
  Collapsing them would be silent data loss, which is worse than the
  double-count being fixed here.
- **A statement still reports its whole file.** Transactions link to statements
  through `statement_transactions`, so a row introduced by January and repeated
  in February belongs to both. `GET /transactions?statement_id=` returns
  everything in that file; unfiltered, each transaction appears exactly once.
  Without the join table one of those two queries would have to be wrong.
  `TransactionRead` therefore exposes **`statement_ids`** (a list), not a single
  `statement_id`.
- **Concurrent uploads must not both insert.** New transactions go in with
  `INSERT … ON CONFLICT (dedupe_key) DO NOTHING` and the ids are resolved by a
  follow-up `SELECT`, so a writer that loses the race links to the winner's row
  instead of duplicating it.

Statements with no `account_ref` get a NULL `dedupe_key` and are never
deduplicated: without an account, two people's identical £4.35 coffee at the
same bank would collapse into one row.

### When the institution publishes its own id

The description is the weakest thing in that fingerprint. It is the part of a row
an institution feels free to reword between exports, and a reworded row
fingerprints differently — so it is stored a second time, and the account
silently doubles. That is the same double-count overlapping statements cause,
arriving by a different route.

Some institutions publish an id for the transaction. When one does, the adapter
passes it as `external_id` and the fingerprint uses **it in place of the
description**:

```
v1 | institution | account | date | direction | amount | currency | description
v2 | institution | account | date | direction | amount | currency | external_id
```

Rewording is then free. On a Wise export whose six descriptions were all
rewritten, the re-upload stores 6 of 6 rows again when keyed on the description
and 0 of 6 when keyed on the id.

Two properties of that layout are load-bearing:

**The id replaces the description, not the rest of the key.** An institution's id
is not necessarily unique per row — Wise gives a transfer and the fee charged for
it the same `TransferWise ID` — so date, amount and currency stay in the payload.
Keying on the id alone would merge the fee into its transfer and lose it.

**The marker is hashed in as the first element**, so the two shapes are different
payloads rather than two spellings of one. A `v1` key and a `v2` key can never
collide, and a description that happens to read like an id cannot fingerprint as
one. `v1` and `v2` are not a version sequence to migrate between: both are live
at once, chosen per transaction.

`v1` is frozen. Every `dedupe_key` in the database was built with it, and
changing that payload by so much as a separator invalidates all of them — with no
error, just totals that quietly stop matching. `test_identity.py` pins it to a
literal digest so that change cannot be made by accident.

---

## The normalized schema

Every parser, whatever the source layout, must return
`list[Transaction]` (`src/statement_normalizer/models/schemas.py`):

| Field | Type | Notes |
|-------|------|-------|
| `date` | `date` | Posting date. |
| `description` | `str` | Whitespace-collapsed narrative. |
| `amount` | `Decimal` | **Always non-negative.** Never a float. |
| `currency` | `str` | ISO 4217 alphabetic, upper-cased on the way in. |
| `direction` | `credit` \| `debit` | Where the sign lives. |
| `balance_after` | `Decimal \| None` | Running balance, when the source provides one. |
| `raw_row` | `dict` | The verbatim source record. |
| `source_institution` | `str` | Which adapter produced this. |

Two choices worth calling out:

- **Sign lives in `direction`, not in `amount`.** Institutions disagree about
  whether a debit is negative, a positive number in a `Debit` column, or
  `(1,234.56)`. Normalizing to *magnitude + direction* means downstream
  aggregation never has to know which convention the source used.
  `Transaction.signed_amount` gives you the signed value when you want it.
- **`raw_row` is always kept.** Parsers are guesses about someone else's export
  format, and they will be wrong. Storing the source record means a fixed parser
  can be re-run over historical data instead of re-requesting statements.

Money is `NUMERIC(20, 4)` in Postgres and `Decimal` in Python, end to end.

---

## The adapter pattern

Each institution's statement layout gets its own **adapter** — a subclass of
`StatementParser` — and a **registry** routes an incoming file to exactly one of
them. The upload endpoint never mentions a bank by name; adding support for a new
institution means adding one file and one decorator, and touching nothing else.

### The interface

```python
class StatementParser(ABC):
    institution: ClassVar[str]  # goes into source_institution
    supported_formats: ClassVar[frozenset[StatementFormat]]
    priority: ClassVar[int] = 100  # higher wins; ties are an error

    @abstractmethod
    def can_parse(self, file: StatementFile) -> bool: ...

    @abstractmethod
    def parse(self, file: StatementFile) -> list[Transaction]: ...
```

### Four design decisions

**1. Detection takes a `StatementFile`, not a file handle.**
Detection asks *every* registered adapter to look at the *same* file. If that
were a stream, the first `can_parse` would read to EOF and every adapter after it
would see an empty buffer — detection results would silently depend on
registration order. `StatementFile` holds the bytes with cached lazy `.text`,
`.head()`, `.format`, and `.sha256`, so `can_parse` is a pure function of an
immutable value: cheap to call N times, and constructible in a test without
FastAPI or an event loop.

**2. `can_parse` is a cheap sniff; `parse` is allowed to be expensive.**
`can_parse` looks at the extension, magic bytes, and the header row. It must not
raise and must not fully parse. The tempting alternative — "call `parse()`, catch
the exception, try the next adapter" — makes a *malformed statement from a known
bank* indistinguishable from an *unknown institution*, and throws away the error
message that would have explained the problem. Splitting the two means a header
match followed by a body failure raises
`StatementParseError("[dummy_bank] at row 3 bad date '03/01/2026'")` instead of
falling through to a stranger's parser.

**3. Ambiguity is an error, not a coin flip.**
`registry.detect()` collects *all* claims, sorts by priority, and returns the
winner. If the top two tie, it raises `AmbiguousParserMatch`. Two adapters
claiming one file at equal priority is a bug in the parser set — resolving it by
import order would attribute real money to the wrong institution with no signal
that anything went wrong. `priority` is the explicit escape hatch when a
specific adapter must beat a generic one (a bank-specific CSV over a generic
OFX-ish CSV), without anyone reordering imports.

**4. Registration is explicit, and keyed on institution *and* format.**
Adapters register with `@registry.register` and are imported in
`parsers/__init__.py`. No `pkgutil` package scanning: the live parser set stays
greppable. An institution may register more than once as long as the adapters
cover disjoint formats — a bank's CSV export and its PDF statement are two
unrelated documents, and one class with a branch in it would mean a file per
*institution* rather than a file per *layout*. Two adapters claiming the same
institution and format is still a bug, and still fails loudly at import time.
Tests build a throwaway `ParserRegistry()` rather than mutating global state.

### Adding a new institution

1. Create `src/statement_normalizer/parsers/<institution>.py`.
2. Subclass `StatementParser`, set `institution`, implement `can_parse` / `parse`.
3. Optionally override `extract_account_ref(file)` if the export names the
   account it covers — it defaults to `None`, so adapters whose format carries
   no account identifier can ignore it.
4. Decorate the class with `@registry.register`.
5. Import it in `parsers/__init__.py`.
6. Drop a real (anonymized) export into `tests/fixtures/` and assert against it.

`dummy_csv.py` is the reference implementation — copy its shape.

### Checking a real export first

Every adapter here was written against a format description rather than a
download from an actual account, which is the largest standing risk in the
project — a header that is wrong by one column name turns every upload into a
422, and a date order that is wrong misdates every row without raising anything.

`scripts/inspect_real_file.py` answers both questions from one real file, without
that file entering the repository:

```bash
uv run python scripts/inspect_real_file.py ~/Downloads/statement.csv
uv run python scripts/inspect_real_file.py ~/Downloads/statement.pdf
```

It reports which adapters claim the file and, for each that does not, the missing
columns in terms of that adapter's own rule; the column names with an inferred
kind; whether the file's own dates prove a day/month order; and for a PDF whether
there is a text layer at all, where the header row sits, and how many amount
columns there are. If an adapter does claim the file it parses it and checks that
the running balances reconcile against the amounts — which tests the signs, the
direction mapping and the fee handling at once.

**The output is redacted by default**: descriptions and cell values are reduced
to a length, so it is safe to paste into an issue. `--show-values` prints them
verbatim. Nothing is written and no database is touched. `--assume revolut` runs
an adapter against a file detection did not claim, which separates "the header
rule is wrong" from "the whole format is wrong".

### What a real format forces: `revolut`

`dummy_bank` is a layout we invented, so it fits the normalized schema by
construction. Revolut's CSV export is the first one that does not, and the three
places it pushes back are the interesting part of the adapter.

**Not every row is a transaction.** The export carries `DECLINED`, `REVERTED`
and `PENDING` rows alongside `COMPLETED` ones. A declined payment moved no
money; a reverted one moved it and moved it back. Storing either invents a
ledger entry that never existed, so only `COMPLETED` rows are parsed. A
`COMPLETED` row with no completion date raises rather than falling back to the
start date — that is the case where guessing would misdate real money.

**A fee is a transaction of its own.** `Fee` is a separate column, and `Balance`
has already subtracted it: a £100 ATM withdrawal with a £2 fee lands on a
balance £102 lower. Dropping the fee stops stored totals reconciling with the
balance; folding it into the amount misstates what the ATM charged. So one
source row becomes two transactions — the withdrawal, and `Fee: Cash at ATM` —
and the first carries no `balance_after`, because the intermediate balance is
not a real position in the ledger. One row expanding into several is something
PDF and broker exports will need too.

**Detection matches a required subset, not the exact header.** `dummy_csv`
demands an exact header tuple, which is right for a format we own and wrong for
one we don't: a column added to someone else's export would turn every upload
into a 422. `RevolutCsvParser` requires its ten columns to be *present* and
tolerates extras. `product` + `started_date` + `completed_date` + `state`
together are distinctive enough that no other institution collides, so detection
stays unambiguous at equal priority.

That rule has one hole, and checking the adapter against Revolut's published
format is what found it. Revolut's **crypto/trading export is this same header
plus four columns**:

```
Type,Product,Started Date,Completed Date,Description,Amount,Currency,
Fiat amount,Fiat amount (inc. fees),Fee,Base currency,State,Balance
```

A subset rule claims it — and its `Amount`, `Currency` and `Balance` describe the
*asset*, not money: `100.0000`, `EOS`. The money is in `Fiat amount`. Parsed as a
fiat statement it stored `100 EOS` as a credit of 100, a fee denominated in `SEK`
as `EOS`, and filed all of it under `revolut|Current` — the same account scope as
the real fiat export. Nothing downstream would have caught it, because `EOS` is
three uppercase letters and passes currency validation.

So `TRADING_COLUMNS` names the two columns (`Fiat amount`, `Base currency`) that
mark that file, and the adapter declines it. A subset rule tolerates a superset
that means *more*; it cannot, on its own, recognize one that means something
*different*. The file now 422s as unrecognized, which is the correct answer until
an adapter exists that models asset quantities.

One consequence worth knowing: because fees expand into extra rows,
`transaction_count` counts transactions produced, not lines in the file.

### What a *second* real format forces: `wise`

The point of a second institution is not another adapter. It is finding out which
of the first adapter's decisions were about statements in general and which were
about Revolut — and the answer turned out to be uncomfortable.

Wise's balance statement looks like Revolut's: a signed `Amount`, a
`Running Balance`, and a fee column beside them. Reading the fee column the way
Revolut's is read **double-counts every fee in the file**, because Wise has
already accounted for it by the time you see the row, in one of two ways:

- on a transfer, as *its own row* — `"Wise Charges for: TRANSFER-9003"`, carrying
  the same id as the transfer it belongs to;
- on a card payment, folded into `Amount` itself.

Both readings are checkable inside the file, and they agree: `Running Balance`
reconciles against `Amount` alone. On the six-row fixture, applying Revolut's
rule lands the closing balance €2.57 below the one Wise printed — a wrong number,
not an error, on a file that parsed cleanly.

So the same column concept means opposite things at two institutions, and neither
adapter can be written from the other. What made the difference decidable was
arithmetic that the export itself supplies — which is a reason to want a running
balance in a fixture even when nothing reads it.

Three smaller decisions:

**Dates are day-first.** `03-04-2026` is 3 April. Read as month-first it silently
becomes 4 March — every row wrong, nothing raised.

**Detection is a subset match, for a sharper reason than Revolut's.** Wise has
shipped 19-, 20- and 23-column versions of this export while keeping the column
*names*, so an exact-header rule would have broken on their release schedule
rather than on anything a user did. The required set is the intersection of the
vintages, anchored on `TransferWise ID` — a name no other institution emits,
which is what keeps a loose rule from colliding.

**`account_ref` is the currency.** There is no account number here, and
`Payee Account Number` is the *counterparty's*: scoping identity by it would file
every transaction under whoever was paid. One Wise file is one currency balance,
so `wise|EUR` is a real account scope — with the same single-tenant caveat as
`revolut|Current`.

### What a PDF forces: `dummy_bank`, again

A statement PDF is a table with no table markup. `dummy_bank` therefore has two
adapters — `dummy_csv.py` and `dummy_pdf.py` — under one institution, which is
what the registration rule above exists for.

**Position is the data.** The amounts in a statement PDF carry no sign; a debit
is a debit because it is printed in the Debit column. So `StatementFile.pdf_words`
exposes each word with its `x0`/`x1`/`top` rather than a flat string, and the
adapter reads the column geometry off the statement's *own header row* instead of
hard-coding x positions. Money cells are then matched on content *and* position:
proximity alone drags the tail of a long narrative into the Debit column, and a
reference number mid-narrative into whatever column it drifts under.

**Most lines are not transactions.** A page carries a masthead, a repeated
column header, `Balance brought forward`, `Closing balance`, and a footer that
sits squarely inside the description column. The rules that sort them out:

| Line has | Verdict |
|----------|---------|
| a parsable date | a transaction |
| no date, but an amount | a summary line — skipped |
| no date, no amount, directly under a transaction | a wrapped narrative — appended |
| anything else | skipped |

"Directly under" is a vertical-gap test, and it is what keeps the page footer
from being glued onto the last transaction above it.

**A row in both money columns is an error, not a guess.** The columns *are* the
sign, so a line read as both a debit and a credit means the geometry was
misread; choosing a side would put real money on the wrong one.

Because identity is content-based, a PDF statement overlapping a CSV export of
the same account deduplicates against it — the same transaction stored once,
whether it arrived as a CSV cell or as a word at a position on a page.

The fixture is generated by `tests/fixtures/generate_dummy_bank_pdf.py`, stdlib
only and committed alongside the PDF it writes, so the fixture is reviewable
rather than an opaque binary. Regenerate it with:

```bash
uv run python tests/fixtures/generate_dummy_bank_pdf.py
```

---

## Layout

```
src/statement_normalizer/
├── main.py              FastAPI app
├── config.py            pydantic-settings
├── db.py                engine + session dependency
├── api/routes/          statements.py, transactions.py
├── models/
│   ├── schemas.py       Pydantic: Transaction, Direction, responses
│   ├── identity.py      transaction fingerprinting (dedupe_key)
│   └── tables.py        SQLAlchemy: statements, transactions, links
└── parsers/
    ├── base.py          StatementFile, StatementParser (the contract)
    ├── registry.py      ParserRegistry, detect/parse routing
    ├── exceptions.py    NoMatchingParser, AmbiguousParserMatch, StatementParseError
    ├── csv_fields.py    shared row iteration and money/date cell parsing, with row-level errors
    ├── revolut_csv.py   Revolut CSV export
    ├── wise_csv.py      Wise balance statement CSV
    ├── dummy_csv.py     reference adapter (CSV)
    └── dummy_pdf.py     reference adapter (PDF), column geometry from word positions
migrations/              Alembic
scripts/                 inspect_real_file.py — report on a real export, redacted
tests/fixtures/          sample statement exports
```

---

## Not included, deliberately

No Redis, no Celery, no auth. PDF text extraction uses `pdfplumber`, chosen over
a lighter text-only extractor because word coordinates are what distinguish a
debit column from a credit one.

## Motivation

## Contributing

## Usage

## Quick Start

## Known gaps

- **Two real institutions.** `revolut` and `wise` are the adapters for formats we
  did not invent; `dummy_bank` remains as the reference implementation.
- **Neither real format has been checked against a download of our own.** Both
  headers, date formats and — for Wise — sample rows with reconciling balances
  are corroborated by several independent third-party importers and by real
  exports committed to public repositories, which is much better than memory and
  is not the same as an export from an actual account.
- **`wise` uses the statement's currency as its account reference.** One Wise
  file is one currency balance, so `wise|EUR` is a real scope — but a user with
  two Wise profiles has two `EUR` balances, and this cannot tell them apart.
  The filename carries a balance id (`statement_12055917_EUR_…`) that would, if
  the upload path ever preserved it.
- **`wise` reads `Total fees` for nothing.** The column is kept in `raw_row` but
  never becomes a transaction, because Wise has already accounted for it. If a
  vintage ever charges a fee it does *not* account for, the money goes missing
  silently — the reconciliation test on the fixture is what would catch it, and
  only for the fixture.
- **Revolut's crypto/trading export is rejected, not parsed.** It is recognized
  well enough to be declined (see above) rather than misread, but a user who
  uploads one gets a generic "no parser recognized this file" rather than an
  explanation. Modelling asset quantities is its own piece of work.
- **`revolut` uses `Product` as its account reference.** The export carries no
  IBAN or account number, so `Current` is the closest thing to an account scope.
  Returning nothing instead would leave `dedupe_key` NULL and let every
  overlapping re-download double-count. `revolut|Current` is well defined only
  because this service is single-tenant by construction; revisit it the day it
  grows users.
- **The PDF adapter is text-layer only.** A scanned or image-only statement
  yields no words and is not recognized; OCR is out of scope.
- **PDF column geometry comes from a header row.** A statement whose table has
  no `Date`/`Description`/`Debit`/`Credit`/`Balance` header — or which splits a
  transaction across a page break — is not handled.
- **Identity still leans on the description wherever there is no id to use.**
  `wise` publishes one; `revolut` and `dummy_bank` do not, so for those a bank
  that reworded its narrative between exports still stores the transaction
  twice. `raw_row` is retained, so historical rows can be re-keyed.
- **Dedupe is global, not per institution.** The unique index on
  `content_sha256` is on the hash alone, so two institutions emitting a
  byte-identical file would collide. Vanishingly unlikely with real exports.
- **Rows stored before migration `0003` have a NULL `dedupe_key`** and never
  match later uploads. Backfilling would have meant reimplementing the
  fingerprint in SQL; re-uploading those statements is the intended fix.
- **Rows stored before migration `0004` are keyed on their description**, and a
  re-upload of the same statement now keys on the id and stores them a second
  time. The id exists only in the source file, so no backfill is possible;
  re-uploading is again the fix, and it is a one-time cost per statement.
- **A delete is not audited and cannot be undone from inside the service.** The
  rows are gone, not tombstoned. Re-uploading the file restores its transactions,
  but the statement gets a new id, and any row it had shared with a statement
  deleted in the meantime comes back with a fresh `created_at`.
- **An upload racing a delete of a row it shares fails with a `500`.** Postgres
  serializes the two and the upload loses its foreign key, rolling back whole —
  safe, but the caller deserves a `409` and a retry rather than a server error.
- **Pagination is offset-based.** Fine at this size, but a deep `offset` makes
  Postgres walk everything it skips, and `total` is a full `COUNT` on every
  request. A keyset cursor over `(date, id)` is the fix when a table gets large
  enough to feel it.
