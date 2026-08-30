"""Paging and filtering on GET /transactions. Require TEST_DATABASE_URL."""

import pytest


@pytest.fixture
def six_transactions(upload):
    """Six stored rows across two statements: three credits, three debits."""
    return [upload("dummy_bank_statement.csv").json(), upload("dummy_bank_february.csv").json()]


def test_a_page_reports_the_size_of_the_whole_result_set(client, six_transactions):
    page = client.get("/transactions", params={"limit": 2}).json()

    assert len(page["items"]) == 2
    assert page["total"] == 6  # not 2: the point of the envelope
    assert (page["limit"], page["offset"]) == (2, 0)


def test_total_counts_the_filtered_set_not_the_table(client, six_transactions):
    page = client.get("/transactions", params={"direction": "debit"}).json()

    assert page["total"] == 3
    assert len(page["items"]) == 3
    assert all(row["direction"] == "debit" for row in page["items"])


def test_an_offset_past_the_end_still_reports_the_total(client, six_transactions):
    """The case a `count(*) OVER ()` alongside the rows would get wrong: no rows
    come back, so there is nothing to carry the count."""
    page = client.get("/transactions", params={"offset": 100}).json()

    assert page["items"] == []
    assert page["total"] == 6


def test_paging_visits_every_row_exactly_once(client, six_transactions):
    seen: list[str] = []
    offset = 0
    while True:
        page = client.get("/transactions", params={"limit": 2, "offset": offset}).json()
        seen.extend(row["id"] for row in page["items"])
        offset += page["limit"]
        if offset >= page["total"]:
            break

    assert len(seen) == 6
    assert len(set(seen)) == 6  # stable ordering: no row seen twice, none skipped


def test_total_respects_the_statement_filter(client, six_transactions):
    january, february = six_transactions

    assert client.get("/transactions", params={"statement_id": january["id"]}).json()["total"] == 4
    assert client.get("/transactions", params={"statement_id": february["id"]}).json()["total"] == 2


def test_an_impossible_date_range_is_rejected_rather_than_returned_empty(client):
    response = client.get(
        "/transactions", params={"date_from": "2026-02-01", "date_to": "2026-01-01"}
    )

    assert response.status_code == 422
