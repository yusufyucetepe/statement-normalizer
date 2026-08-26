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
