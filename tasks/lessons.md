# Lessons

## Alembic: explicit `sa.Enum(...).create()` collides with `create_table`
Creating a Postgres enum type explicitly in `upgrade()` **and** referencing the
same `sa.Enum` object in `op.create_table` makes SQLAlchemy emit `CREATE TYPE`
twice, and the migration dies with `DuplicateObject: type "x" already exists`.
Caught here by actually running `alembic upgrade head` against a real Postgres
rather than assuming the migration was fine.

Fix: build the column type with `postgresql.ENUM(..., create_type=False)` and
create/drop the type explicitly. See `migrations/versions/0001_initial_schema.py`.

Rule: never call a hand-written migration done without applying it to a real
database, and run `alembic check` to prove it matches the ORM metadata.

## SELECT-then-INSERT is not deduplication
Checking "does this hash already exist?" before inserting loses the race: two
concurrent identical uploads both find nothing and both insert. The unique index
is the only actual source of truth. Keep the pre-check as a fast path, but catch
`IntegrityError`, roll back, re-query, and return the same 409.

Verified by firing 8 concurrent uploads of one file: exactly one 201, seven 409s,
one row. Worth actually racing this kind of code rather than reasoning about it.

## Don't let `env.py` hard-override the Alembic URL
`migrations/env.py` set `sqlalchemy.url` from app settings unconditionally, so a
caller that had already set a URL (the test suite pointing at the test database)
was silently ignored — the tests connected to the wrong host and failed on
authentication. Default to the app DSN only when nothing else set one:

```python
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
```

Rule: "config comes from one place" should still let an explicit caller win.

## Detection must not consume the file
Passing a file handle to a chain of `can_parse` calls means the first reader
drains it and every later parser sees an empty stream — detection silently
depends on registration order. Read once into a value object
(`StatementFile`) and hand every adapter the same immutable bytes.

## One hash, one implementation

Migration `0003` needed a `dedupe_key` backfill, and the obvious move was to
recompute the fingerprint in SQL with `sha256()` and a window function for the
occurrence counter. That would have been a second implementation of a hash whose
Python version lives in `models/identity.py` — and the two only have to disagree
about one detail (Decimal scale, case folding, separator) for the backfilled
rows to never match anything the app writes afterwards, silently.

**Why:** a hash used as an identity has no tolerance for drift, and drift
between two implementations is invisible until the data is already wrong.

**How to apply:** never reimplement a fingerprint in a second language to
backfill. Either run the real implementation over the rows (a data-migration
script that imports the app code), or leave the column NULL and make NULL mean
something safe. `0003` left it NULL, because NULL already meant "do not
deduplicate".

## `alembic check` catches constraint-vs-index drift

`mapped_column(..., unique=True)` declares a unique **constraint**;
`mapped_column(..., index=True, unique=True)` declares a unique **index**. They
behave identically for `ON CONFLICT`, so nothing failed at runtime — `alembic
check` was the only thing that noticed `0003` and the ORM disagreed.

**Why:** functional equivalence hides schema drift, and drift compounds across
migrations until an autogenerate produces nonsense.

**How to apply:** run `alembic check` after every hand-written migration, not
just after autogenerate. Treat a diff as a real finding even when the two forms
are functionally the same.

## Exact-header detection is right for a format you own, wrong for one you don't

`dummy_csv.can_parse` compares the header to an exact tuple. That is correct for
a fixture we invented: any drift is our own bug and should fail loudly. Copying
it into `revolut_csv` would have meant a single column added to someone else's
export turning every upload into a 422 — a format we do not control, failing
closed on a change we have no say in.

**Why:** the strictness that makes a self-owned format safe makes a third-party
one brittle, and the failure lands on the user, not on us.

**How to apply:** for external formats, require the columns you actually read to
be *present* and tolerate extras. Pick a required set distinctive enough that
detection stays unambiguous — for `revolut`, `product` + `started_date` +
`completed_date` + `state`. Keep exact matching for formats you define.

## A uniqueness rule should name what actually has to be unique

`ParserRegistry.register` rejected a second adapter for an institution. That
looked like "one adapter per institution" but the real invariant is narrower:
two adapters must never claim the *same file*. Since `candidates()` already
filters on format, two adapters for one institution covering disjoint formats
can never both claim anything — the rule was rejecting a case it had no reason
to. It only surfaced when `dummy_bank` needed a PDF adapter, and the workarounds
on offer were both bad: invent a fake institution, or branch on format inside
one class and give up a file per layout.

**Why:** a uniqueness rule that is broader than its invariant does not fail
loudly — it quietly pushes the next feature into a worse shape.

**How to apply:** when a registration or constraint rejects something, check
whether the rejected case can actually cause the harm the rule exists to
prevent. Key the rule on the full identity that matters — here (institution,
format) — rather than on the convenient prefix of it.

## A subset match cannot tell "more" from "different"

`RevolutCsvParser` detects on a required *subset* of the header, so an added
column does not break the upload. That reasoning is still right, but it has a
blind spot I did not see when I wrote it: a file can contain every column I
require and still be a different document. Revolut's crypto export is the fiat
header plus four columns, and it was claimed and silently misparsed — asset
quantities stored as money, under the same account scope as the real statement.

**Why:** "tolerate additions" and "recognize this document" are two different
questions. A subset rule answers the first and is silently assumed to answer the
second.

**How to apply:** when detection is a subset match, ask what *else* is a superset
of it — especially other exports from the same institution. If one exists, name
the columns that distinguish it and decline explicitly. And when a check confirms
the thing I was worried about, keep looking: the risk I wrote down was the header
being wrong, and the real bug was next to it.
