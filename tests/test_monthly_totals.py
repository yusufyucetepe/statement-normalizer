"""`GET /transactions/monthly-totals`. Require TEST_DATABASE_URL."""

from decimal import Decimal


def totals(client, **params) -> list[dict]:
    response = client.get("/transactions/monthly-totals", params=params)
    assert response.status_code == 200
    return response.json()["items"]


def group(items: list[dict], month: str, currency: str, direction: str) -> dict:
    return next(
        row
        for row in items
        if (row["month"], row["currency"], row["direction"]) == (month, currency, direction)
    )


def test_the_institution_filter_does_not_double_count_an_overlap(upload, client, count_rows):
    """The reason this endpoint is worth building rather than assumed correct.

    The two Revolut downloads share four transactions, so the link table holds 14
    rows for 10 transactions. Filtering by institution through a join over
    `statement_transactions` would match those four twice and inflate every sum
    by exactly the overlap — milestone 3's double-count, returning in an endpoint
    whose entire output is numbers, where a wrong total looks exactly like a
    right one. Filtering on the column denormalized onto `transactions` cannot
    multiply a row, and this is what says so.
    """
    upload("revolut_statement.csv")
    upload("revolut_overlap.csv")
    assert (count_rows("transactions"), count_rows("statement_transactions")) == (10, 14)

    filtered = totals(client, institution="revolut")
    assert filtered == totals(client)

    # Hand-totalled from the two fixtures, counting each shared row once.
    assert [
        (row["month"], row["currency"], row["direction"], row["total"], row["transaction_count"])
        for row in filtered
    ] == [
        ("2026-02-01", "EUR", "debit", "8.4000", 1),
        ("2026-02-01", "GBP", "credit", "2053.2000", 3),
        ("2026-02-01", "GBP", "debit", "179.5500", 6),
    ]

    # What the join would have produced, spelled out: the four shared rows are
    # 50.00 + 0.35 GBP debit, 3.20 GBP credit and 8.40 EUR debit.
    assert Decimal(group(filtered, "2026-02-01", "GBP", "debit")["total"]) != Decimal("229.90")
    assert Decimal(group(filtered, "2026-02-01", "EUR", "debit")["total"]) != Decimal("16.80")


def test_currencies_are_not_summed_and_directions_are_not_netted(upload, client):
    """Both would produce a number with no meaning: `amount` is a magnitude and
    the sign lives in `direction`, so a net would silently subtract 8.40 EUR from
    a pile of pounds."""
    upload("revolut_statement.csv")
    items = totals(client)

    assert {(row["currency"], row["direction"]) for row in items} == {
        ("GBP", "debit"),
        ("GBP", "credit"),
        ("EUR", "debit"),
    }
    assert all(Decimal(row["total"]) >= 0 for row in items)


def test_each_month_is_its_own_group(upload, client):
    upload("dummy_bank_statement.csv")
    upload("dummy_bank_february.csv")
    items = totals(client, institution="dummy_bank")

    assert [(row["month"], row["direction"], row["total"]) for row in items] == [
        ("2026-01-01", "credit", "2564.9900"),
        ("2026-01-01", "debit", "133.2500"),
        ("2026-02-01", "credit", "2500.0000"),
        ("2026-02-01", "debit", "31.2000"),
    ]


def test_an_institution_filter_excludes_the_other_institution(upload, client):
    upload("dummy_bank_february.csv")
    upload("revolut_statement.csv")

    assert {row["currency"] for row in totals(client, institution="dummy_bank")} == {"GBP"}
    assert [row["transaction_count"] for row in totals(client, institution="dummy_bank")] == [1, 1]
    assert totals(client, institution="nobody") == []


def test_date_bounds_narrow_the_groups(upload, client):
    upload("dummy_bank_statement.csv")
    upload("dummy_bank_february.csv")

    assert {row["month"] for row in totals(client, date_from="2026-02-01")} == {"2026-02-01"}
    assert {row["month"] for row in totals(client, date_to="2026-01-31")} == {"2026-01-01"}


def test_an_inverted_date_range_is_rejected(client):
    response = client.get(
        "/transactions/monthly-totals",
        params={"date_from": "2026-03-01", "date_to": "2026-01-01"},
    )
    assert response.status_code == 422


def test_the_response_is_an_envelope_without_pagination(upload, client):
    """Keyed `items` like the two list endpoints, so all three read the same way —
    but no `limit`/`offset`, because an aggregate is not a page."""
    upload("dummy_bank_statement.csv")
    body = client.get("/transactions/monthly-totals").json()

    assert set(body) == {"items"}
    assert set(body["items"][0]) == {"month", "currency", "direction", "total", "transaction_count"}


def test_no_statements_is_an_empty_result_not_an_error(client):
    assert totals(client) == []
