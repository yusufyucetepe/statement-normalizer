"""Reading back uploaded statements. Require TEST_DATABASE_URL."""

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
