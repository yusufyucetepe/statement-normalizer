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

## Detection must not consume the file
Passing a file handle to a chain of `can_parse` calls means the first reader
drains it and every later parser sees an empty stream — detection silently
depends on registration order. Read once into a value object
(`StatementFile`) and hand every adapter the same immutable bytes.
