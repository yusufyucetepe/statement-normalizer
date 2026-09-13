# statement-normalizer

Upload a bank statement — CSV or PDF, from any of several institutions — and
get back one normalized, validated, deduplicated transaction schema in
Postgres.

```
POST /statements/upload  →  detect institution  →  parse  →  validate  →  store
```

A FastAPI service built around one idea: **every institution's export is a
different shape, and exactly one place in the code is allowed to know that.**
That place is an adapter. Everything downstream — validation, identity, storage,
the API — sees a single `Transaction` type and never mentions a bank by name.

**Status:** working end to end, 166 tests against a real Postgres. Five adapters
across three real institutions (`ziraat`, `revolut`, `wise`) plus `dummy_bank` in
both CSV and PDF as the reference implementation. `ziraat` was written from a
statement downloaded off a live account rather than from a published description
of one, and it found more problems than the other four combined.

## Run it

```bash
docker compose up --build          # api on :8000, postgres on :5432
```

The `api` service waits for Postgres, runs `alembic upgrade head`, then starts
uvicorn. Interactive docs — the only interface this project has — at
**http://localhost:8000/docs**.

The same account, exported twice — once as a CSV, once as a PDF statement:

```bash
U=http://localhost:8000
curl -sF "file=@tests/fixtures/dummy_bank_statement.csv" $U/statements/upload
# {"format": "csv", "transaction_count": 4, "new_transaction_count": 4, ...}
curl -sF "file=@tests/fixtures/dummy_bank_statement.pdf" $U/statements/upload
# {"format": "pdf", "transaction_count": 7, "new_transaction_count": 5, ...}

curl -s "$U/transactions" | jq .total
# 9
```

Nine, not eleven. Two of those transactions arrived in both files — one as a cell
in a CSV, one as a word at a position on a page — and they are stored once.

Without Docker, with Python 3.12+ and [uv](https://docs.astral.sh/uv/):
`uv pip install -e ".[dev]"`, `cp .env.example .env`, `uv run alembic upgrade
head`, then `uv run uvicorn statement_normalizer.main:app --reload`.

## Endpoints

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/statements/upload` | Multipart CSV or PDF. **201** with a summary and a `Location`; **409** if these exact bytes were uploaded before; **422** if nothing recognizes the file, or a claimed file is malformed. |
| `GET` | `/transactions` | Filters: `date_from`, `date_to`, `direction`, `institution`, `statement_id`, `limit`, `offset`. Returns `{items, total, limit, offset}`. |
| `GET` | `/transactions/monthly-totals` | Money per month by currency and direction. Filters: `date_from`, `date_to`, `institution`. |
| `GET` | `/statements` | Uploaded statements, newest first. Same envelope. |
| `GET` | `/statements/{id}` | One statement. **404** if unknown. |
| `DELETE` | `/statements/{id}` | Removes it, plus the transactions no other statement still holds. |
| `GET` | `/parsers` | The live adapters, in the order detection considers them. |
| `GET` | `/health` | Liveness. |

## The normalized schema

What every adapter must return, whatever the source looked like:

| Field | Type | Notes |
|-------|------|-------|
| `date` | `date` | Posting date. |
| `description` | `str` | Whitespace-collapsed narrative. |
| `amount` | `Decimal` | **Always non-negative.** Never a float. |
| `currency` | `str` | ISO 4217, upper-cased on the way in. |
| `direction` | `credit` \| `debit` | Where the sign lives. |
| `balance_after` | `Decimal \| None` | Running balance, when the source has one. |
| `raw_row` | `dict` | The verbatim source record, always kept. |
| `source_institution` | `str` | Which adapter produced this. |

Money is `Decimal` in Python and `NUMERIC(20, 4)` in Postgres, end to end.

## The four big decisions

- **The sign lives in `direction`, not in `amount`.** Institutions disagree about
  whether a debit is negative, a positive number in a `Debit` column, or
  `(1,234.56)`. Magnitude + direction means nothing downstream has to care.
- **Each adapter *declares* its decimal convention.** `1.234` is 1234 in Bonn and
  1.234 in Boston, and no amount of staring at the cell will say which — so
  `to_decimal` refuses to parse without being told. The alternative isn't an
  error, it's a plausible number wrong by a factor of a thousand.
- **Identity is per transaction, not per file.** January's export and February's
  overlap; the bytes differ, so both upload. Each row is fingerprinted as
  `sha256(marker | institution | account | date | direction | amount | currency |
  identity | occurrence)`, so the overlap stores once, and an upload reports
  `transaction_count` and `new_transaction_count` separately. `identity` is the
  institution's own transaction id where it publishes one and the description
  where it does not, with `marker` recording which — so **a bank that rewords its
  narrative between exports no longer double-counts**, and the two shapes can
  never be compared as equal. `occurrence` numbers genuine repeats, because two
  identical £3.20 coffees on one day are two real transactions.
- **Ambiguous detection is an error, not a coin flip.** Two adapters claiming one
  file at equal priority raises. Resolving that by import order would attribute
  real money to the wrong institution with nothing to show for it.

Each of these is argued at length, with the real export that forced it, in
**[DESIGN.md](DESIGN.md)**.

## Layout

```
src/statement_normalizer/
├── main.py              FastAPI app
├── api/routes/          statements.py, transactions.py
├── models/
│   ├── schemas.py       Pydantic: Transaction, Direction, responses
│   ├── identity.py      transaction fingerprinting (dedupe_key)
│   └── tables.py        SQLAlchemy: statements, transactions, links
└── parsers/
    ├── base.py          StatementFile, StatementParser (the contract)
    ├── registry.py      ParserRegistry, detect/parse routing
    ├── csv_fields.py    shared row iteration, DecimalConvention, money/date cells
    ├── ziraat_csv.py    Ziraat Bankası CSV, table not at line 1
    ├── revolut_csv.py   Revolut CSV
    ├── wise_csv.py      Wise balance statement CSV
    ├── dummy_csv.py     reference adapter (CSV)
    └── dummy_pdf.py     reference adapter (PDF), column geometry from word positions
migrations/              Alembic
scripts/                 inspect_real_file.py    report on a real export, redacted
                         check_no_real_accounts.py    CI guard against leaked identifiers
tests/fixtures/          sample statement exports
```

Adding an institution means adding one file in `parsers/` and one decorator.
Nothing else changes — see [DESIGN.md](DESIGN.md#adding-a-new-institution).

## Tests

```bash
uv run pytest                                      # DB tests skip without a database
uv run ruff check . && uv run ruff format --check .
uv run python scripts/check_no_real_accounts.py    # runs first in CI
```

Parser and registry tests are pure functions over bytes. The persistence tests
need `TEST_DATABASE_URL`, run the real migrations against it, and wrap each test
in a transaction that is rolled back:

```bash
docker compose exec db createdb -U statements statements_test
TEST_DATABASE_URL=postgresql+psycopg://statements:statements@localhost:5432/statements_test \
  uv run pytest
```

That last check exists because a live IBAN once reached a public commit — inside
the docstring of the function written to redact IBANs. A docstring does not look
like output, which is exactly the problem. Invented identifiers live in
`tests/fixtures/`; anything IBAN- or card-shaped anywhere else fails CI.

## What this deliberately does not do

No auth, no Redis, no Celery, no frontend, no categorization or budgeting. It
normalizes statements and answers questions about them. The full list of known
gaps — including the ones that would matter first, like the Revolut fee split
never having met a real fee — is the last section of
**[DESIGN.md](DESIGN.md#known-gaps)**.