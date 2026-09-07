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

## `now()` is the transaction timestamp, so a rollback-per-test harness ties every row

A test asserting "most recent first" failed even though the ORDER BY was right.
`uploaded_at` defaults to Postgres' `now()`, which is fixed for the whole
transaction — and the DB fixtures run each test inside one transaction that is
rolled back. Three uploads, one timestamp, order decided by the random-UUID
tiebreaker.

**Why:** the harness that makes DB tests cheap also collapses a distinction the
production code depends on. A timestamp default is not a per-row clock reading,
and any test whose subject is *ordering by time* has to set the times itself
rather than trust the fixture to produce them.

**How to apply:** when a time-ordered assertion fails, check whether the rows
actually have different times before touching the query. And when a test depends
on a value the harness supplies rather than the code under test, set the value
explicitly — the assertion should fail for the reason it names.

## The same column at two institutions can mean opposite things

The Revolut adapter turns its `Fee` column into a second transaction, because
Revolut's `Balance` has already subtracted it. Wise publishes a fee column beside
a signed amount and a running balance too — and doing the same thing there
double-counts every fee, because Wise already emits the charge as its own row (or
folds it into the amount). Same shape, opposite meaning, and the second reading
produces a clean parse with a wrong total.

**Why:** the first adapter for a category quietly becomes the template for the
next one, and its decisions get reused as if they were facts about statements
rather than facts about that institution. Nothing fails loudly when the
assumption travels — that is exactly why it is worth checking.

**How to apply:** when a second institution's export has a column the first one
also had, re-derive what it means from that export's own data instead of copying
the handling. Prefer evidence the file supplies itself: a running balance that
chains against the amounts settles a fee question in one arithmetic check, which
is a reason to want a balance column in a fixture even when nothing reads it.
And when a real export is available, read the *rows*, not just the header — the
header would not have shown either of this milestone's two findings.

## A hash that is already in the database is a schema, not an implementation detail

Changing how `dedupe_key` is built looks like editing one small function. It is
not: every key already stored was built by that function, and altering the
payload by a separator invalidates all of them at once. Nothing raises. The only
symptom is that overlapping uploads quietly start double-counting again.

**Why:** a stored hash has the same contract obligations as a column, but none of
the visible machinery — no migration, no type error, no failing query — so the
usual signals that say "this is a breaking change" are all absent.

**How to apply:** when a new input has to reach a stored fingerprint, add a
*second* payload shape marked distinctly rather than editing the existing one,
and pin the old shape to a literal digest in a test that says what breaking it
costs. Prove the old digest is unchanged against the previous commit rather than
assuming a careful edit was careful enough — `git stash` and compute both.

## Test a diagnostic tool against fixtures whose answers you already know

`inspect_real_file.py` was written to check adapters against real files, and its
first run against `revolut_statement.csv` — a fixture built so the balances
reconcile exactly — reported a preamble that is not there and a balance chain
that breaks. Both were bugs in the tool: a raw comma count mistook a quoted
payee name for a wider row, and the chain check compared a EUR balance against a
GBP one and read the deliberate fee split as money going missing.

**Why:** a tool whose whole purpose is to tell you whether something else is
wrong has no natural error signal of its own. Pointed at an unknown real file it
would have reported both faults as findings about the *bank*, and the fix would
have been applied to the adapter.

**How to apply:** run any new diagnostic against the existing fixtures first and
require the answers to match what they were built to demonstrate. A fixture with
a documented invariant — here, "balances reconcile exactly, fees included" — is
a test for the checker, not only for the parser.

## One real file is worth more than any amount of reasoning about formats

The inspection script was tested against 18 fixtures and five hand-built
adversarial files, and looked finished. The first genuine bank export broke
three of its assumptions in one run: rows padded to a uniform width so preamble
detection had nothing to compare, money written without fixed decimal places
(`450`, `12,345.6`), and a footer of branch addresses sitting inside the table.
It also found a live bug in `to_decimal` that no fixture could have found,
because every fixture was written by someone who already knew the convention.

**Why:** fixtures encode the assumptions of whoever wrote them. Adversarial
fixtures encode the *failures that same person could imagine*. Neither reaches
the things a real institution does for reasons of its own — a padded export is
not a malformed one, and nobody would think to write that fixture.

**How to apply:** treat "verified against fixtures" as untested for anything
whose input comes from outside. Get one real artifact through the code before
believing a format-handling component works, and when it breaks, fix the
component rather than the fixture.

## Two conventions in one document is not a thing you would ever guess

Ziraat's export writes every transaction amount as `-2,262.07` and then writes
the totals line directly beneath them as `Borç:-2.262,07` — the same number, the
other convention, four lines apart in one file. Nothing warns you; both parse.

**Why:** a format description describes the table. The parts of a document that
are not the table — totals, footers, the account block — are written by
different code, sometimes by a different team, and they do not have to agree
with it. The habit of thinking "this file uses convention X" is the bug.

**How to apply:** decide what a data row *is* and take only those, by a positive
test rather than by excluding what you have noticed so far. Here that is a
leading `dd.mm.yyyy`. Everything else in the file is then someone else's problem
by construction, rather than something to be discovered one surprise at a time.

## Redaction has to be checked by something that cannot be reasoned with

Three leaks in one tool. A misidentified header row printed an account holder's
name; a reported `account_ref` field printed an IBAN; and then a real IBAN
reached a public commit as the *example in the docstring of the function that
redacts IBANs*. Each time the reasoning was the same and each time it was wrong:
this part is structure, not content, so it is safe to print.

**Why:** "is this content?" is a judgement, and it was made three times by the
same mind that had just decided the output was safe. A docstring is the purest
case — it does not look like output at all, it looks like documentation, so the
question never even arises.

**How to apply:** put a mechanical check in CI that reads the bytes and has no
opinion about which of them are structural. `scripts/check_no_real_accounts.py`
does this, with fixtures as the allowlist: identifiers are invented in
`tests/fixtures/` and everything else must quote those. Do this the first time
real data comes anywhere near the repository, not after it gets in.

## An ambiguity the data cannot resolve belongs to whoever has the context

`to_decimal` decided, at the bottom of the stack, that commas are thousands
separators. It was never a decision anyone made — it was the shape of the first
format that came along, frozen into a `replace()` and then applied to every
institution that followed. Against a European number it returned an answer a
thousand times too small, and returned it silently.

**Why:** the information needed to resolve it does not exist in the cell. `1.234`
is 1234 in Bonn and 1.234 in Boston; no parser, however careful, can tell from
the string. The knowledge lives two levels up, in the adapter, which is the
thing that knows the institution. A default at the bottom does not eliminate the
ambiguity, it just decides it invisibly and in one direction — and the wrong
direction produces a number rather than an error, which is the worst outcome
available.

**How to apply:** when a low-level helper cannot resolve something from its
inputs, do not pick a sensible-looking default. Make the caller state it, with no
default, so a new caller cannot proceed without deciding. Then make the accepted
form strict enough that a *wrong* declaration also fails: here, requiring
grouping separators to be followed by exactly three digits turns a wrong
`ANGLO`/`EUROPEAN` choice into an error on the first file rather than a thousand
plausible numbers.
