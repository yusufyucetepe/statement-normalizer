# statement-normalizer

A FastAPI service that accepts an uploaded bank or broker statement (CSV or PDF),
detects which institution produced it, parses it into one normalized transaction
schema, validates it, and stores it in Postgres.

**Status: working skeleton.** Upload → detect → parse → validate → store is
wired end to end and covered by tests against a real Postgres. What is missing
is *parsers for real institutions* — the only adapter is `dummy_bank`, which
exists to demonstrate the contract. Adding an institution means adding one file;
see [The adapter pattern](#the-adapter-pattern).

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
| `POST` | `/statements/upload` | Multipart upload. Detects, parses, validates and stores. **201** with the statement summary and a `Location` header; **409** if these exact bytes were uploaded before; **422** if no parser recognizes the file or a claimed file is malformed. |
| `GET`  | `/transactions`      | Filters: `date_from`, `date_to`, `direction` (`credit`/`debit`), `institution`, `statement_id`, `limit`, `offset`. |
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

**4. Registration is explicit.**
Adapters register with `@registry.register` and are imported in
`parsers/__init__.py`. No `pkgutil` package scanning: the live parser set stays
greppable, and duplicate-institution registration fails loudly at import time.
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
│   └── tables.py        SQLAlchemy: statements, transactions
└── parsers/
    ├── base.py          StatementFile, StatementParser (the contract)
    ├── registry.py      ParserRegistry, detect/parse routing
    ├── exceptions.py    NoMatchingParser, AmbiguousParserMatch, StatementParseError
    └── dummy_csv.py     reference adapter
migrations/              Alembic
tests/fixtures/          sample statement exports
```
## Not included, deliberately

No Redis, no Celery, no auth. PDF parsing has no dependency wired up yet —
`StatementFile` detects PDFs by magic bytes so a PDF adapter can be added
without changing the contract.

## Motivation

## Contributing

## Usage

## Quick Start

## Known gaps

- **No real institution adapters yet.** `dummy_bank` is the only one; it exists
  to prove the contract, not to read anyone's statements.
- **No PDF adapter.** `StatementFile` detects PDFs by magic bytes so one can be
  added without touching the contract, but no PDF dependency is wired up.
- **Dedupe is global, not per institution.** The unique index is on
  `content_sha256` alone, so two institutions emitting a byte-identical file
  would collide. Vanishingly unlikely with real exports.
- **Overlapping statement periods double-count.** Dedupe is per file, not per
  transaction, so two statements covering an overlapping range will both store
  their shared rows. Per-transaction identity is the fix when it matters.
- **No pagination metadata.** `/transactions` takes `limit`/`offset` but returns
  a bare array with no total count.

If you'd rather honour your original intent, just delete the ## Known gaps block from the above. Keep ## Not included, deliberately either way — it exists in both sides and must appear exactly once.