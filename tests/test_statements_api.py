"""Reading back and deleting uploaded statements. Require TEST_DATABASE_URL."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from statement_normalizer.models.tables import Statement as StatementRow


@pytest.fixture
def three_statements(upload):
    """Two dummy_bank statements and one revolut, uploaded in that order."""
    return [
        upload("dummy_bank_statement.csv").json(),
        upload("dummy_bank_february.csv").json(),
        upload("revolut_statement.csv").json(),
    ]


def test_an_uploaded_statement_can_be_found_again(client, upload):
    """The gap this closes: before, an id the client dropped was unrecoverable
    without re-uploading the bytes and reading it off the 409."""
    uploaded = upload("dummy_bank_statement.csv").json()

    page = client.get("/statements").json()

    assert page["total"] == 1
    assert page["items"][0] == uploaded


def test_a_statement_is_readable_by_id(client, upload):
    uploaded = upload("dummy_bank_statement.csv").json()

    response = client.get(f"/statements/{uploaded['id']}")

    assert response.status_code == 200
    assert response.json() == uploaded


def test_an_unknown_id_is_not_found_rather_than_empty(client):
    response = client.get(f"/statements/{uuid.uuid4()}")

    assert response.status_code == 404


def test_statements_are_listed_most_recent_first(client, db_session, three_statements):
    """Stamped rather than trusting the fixture: `uploaded_at` defaults to
    Postgres' `now()`, which is the *transaction* timestamp, and this harness
    runs every upload inside one transaction — so all three rows share a time and
    the order would be the `id` tiebreaker's, not time's. Real uploads are one
    transaction each; distinct times are what make this assert the ORDER BY.
    """
    for minute, statement in enumerate(three_statements):
        db_session.execute(
            update(StatementRow)
            .where(StatementRow.id == uuid.UUID(statement["id"]))
            .values(uploaded_at=datetime(2026, 3, 1, 12, minute, tzinfo=UTC))
        )

    page = client.get("/statements").json()

    assert page["total"] == 3
    assert [item["id"] for item in page["items"]] == [s["id"] for s in reversed(three_statements)]


def test_the_institution_filter_narrows_the_total_too(client, three_statements):
    page = client.get("/statements", params={"institution": "dummy_bank"}).json()

    assert page["total"] == 2
    assert {item["source_institution"] for item in page["items"]} == {"dummy_bank"}


def test_paging_visits_every_statement_exactly_once(client, three_statements):
    """These three share an `uploaded_at` (see above), so this is the tie the
    `id` tiebreaker exists for: without it, paging could repeat or skip a row."""
    seen: list[str] = []
    offset = 0
    while True:
        page = client.get("/statements", params={"limit": 2, "offset": offset}).json()
        seen.extend(item["id"] for item in page["items"])
        offset += page["limit"]
        if offset >= page["total"]:
            break

    assert len(set(seen)) == 3


def test_an_offset_past_the_end_still_reports_the_total(client, three_statements):
    page = client.get("/statements", params={"offset": 100}).json()

    assert page["items"] == []
    assert page["total"] == 3


def test_an_overlapping_upload_reports_its_new_row_count_after_the_fact(client, upload):
    """What the list is for beyond id recovery: `new_transaction_count` is the
    number that says an overlapping export contributed almost nothing, and until
    now it was visible only in the response that created it."""
    upload("dummy_bank_statement.csv")
    overlap = upload("dummy_bank_overlap.csv").json()

    stored = client.get(f"/statements/{overlap['id']}").json()

    assert stored["transaction_count"] > stored["new_transaction_count"]
    assert stored == overlap


def test_a_rejected_upload_leaves_nothing_to_list(client, upload):
    assert upload("unknown_institution.csv").status_code == 422
    assert upload("dummy_bank_malformed.csv").status_code == 422

    assert client.get("/statements").json()["total"] == 0


@pytest.fixture
def overlapping_pair(upload):
    """Two statements sharing two transactions: 4 + 4 rows, 6 stored."""
    return upload("dummy_bank_statement.csv").json(), upload("dummy_bank_overlap.csv").json()


def test_deleting_a_lone_statement_takes_its_transactions_with_it(client, upload, count_rows):
    statement = upload("dummy_bank_statement.csv").json()

    body = client.delete(f"/statements/{statement['id']}").json()

    assert body["deleted_transaction_count"] == 4
    assert body["retained_transaction_count"] == 0
    assert client.get(f"/statements/{statement['id']}").status_code == 404
    assert count_rows("transactions") == 0
    assert count_rows("statement_transactions") == 0


def test_a_shared_transaction_outlives_the_statement_it_arrived_in(client, overlapping_pair):
    """The whole reason this endpoint is not `DELETE FROM transactions`: the two
    rows January shares with the overlap belong to both. Removing them would
    silently shorten a statement nobody asked to change."""
    january, overlap = overlapping_pair

    body = client.delete(f"/statements/{january['id']}").json()

    assert body["deleted_transaction_count"] == 2  # the two only January held
    assert body["retained_transaction_count"] == 2  # the two the overlap also holds
    page = client.get("/transactions", params={"statement_id": overlap["id"]}).json()
    assert page["total"] == 4  # the surviving statement is still whole


def test_deleting_both_statements_leaves_nothing_behind(client, overlapping_pair, count_rows):
    """The shared rows are retained by the first delete and collected by the
    second: a row goes when its last statement does, not before and not never."""
    january, overlap = overlapping_pair

    client.delete(f"/statements/{january['id']}")
    body = client.delete(f"/statements/{overlap['id']}").json()

    assert body["deleted_transaction_count"] == 4
    assert body["retained_transaction_count"] == 0
    assert count_rows("transactions") == 0


def test_a_deleted_statement_can_be_uploaded_again(client, upload):
    """`content_sha256` is what rejects a re-upload, and it went with the row —
    so a delete is genuinely undoable rather than blocklisting the file."""
    statement = upload("dummy_bank_statement.csv").json()
    client.delete(f"/statements/{statement['id']}")

    again = upload("dummy_bank_statement.csv")

    assert again.status_code == 201
    assert again.json()["new_transaction_count"] == 4


def test_deleting_an_unknown_statement_is_not_found(client):
    assert client.delete(f"/statements/{uuid.uuid4()}").status_code == 404


def test_a_delete_is_not_idempotent_in_the_second_call(client, upload):
    """404 rather than 204 on the second call: the id no longer names anything,
    and pretending otherwise hides a client that lost track of what it deleted."""
    statement = upload("dummy_bank_statement.csv").json()

    assert client.delete(f"/statements/{statement['id']}").status_code == 200
    assert client.delete(f"/statements/{statement['id']}").status_code == 404
