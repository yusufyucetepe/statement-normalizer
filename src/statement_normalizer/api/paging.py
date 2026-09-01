from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select


def count_matching(session: Session, stmt: Select) -> int:
    """How many rows `stmt` matches, before `limit`/`offset` are applied.

    A second query rather than a `count(*) OVER ()` alongside the rows: a window
    function returns the total on each row, so an offset past the end returns no
    rows and therefore no total — exactly the request that most needs one.

    It counts over the caller's own select rather than rebuilding the filters,
    which is what stops the two queries from drifting when a filter is added to
    only one of them.
    """
    return session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
