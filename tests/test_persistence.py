"""End-to-end tests for upload persistence. Require TEST_DATABASE_URL."""

from decimal import Decimal


def test_upload_stores_the_statement_and_its_transactions(upload, client, count_rows):
    response = upload("dummy_bank_statement.csv")

    assert response.status_code == 201
    body = response.json()
    assert body["source_institution"] == "dummy_bank"
    assert body["transaction_count"] == 4
    assert body["format"] == "csv"
    assert body["account_ref"] == "GB00DUMY12345678"
    assert response.headers["Location"] == f"/transactions?statement_id={body['id']}"

    assert count_rows("statements") == 1
    assert count_rows("transactions") == 4

    rows = client.get(response.headers["Location"]).json()
    assert len(rows) == 4
    credit = next(r for r in rows if r["description"] == "ACME PAYROLL JAN")
    assert Decimal(credit["amount"]) == Decimal("2500.00")
    assert credit["direction"] == "credit"
    assert credit["raw_row"]["narrative"] == "ACME PAYROLL   JAN"
    assert credit["statement_ids"] == [body["id"]]


def test_reuploading_the_same_bytes_is_rejected(upload, count_rows):
    first = upload("dummy_bank_statement.csv")
    assert first.status_code == 201

    second = upload("dummy_bank_statement.csv")

    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["statement_id"] == first.json()["id"]
    assert second.headers["Location"] == first.headers["Location"]

    # The duplicate wrote nothing.
    assert count_rows("statements") == 1
    assert count_rows("transactions") == 4


def test_a_different_file_from_the_same_institution_is_accepted(upload, count_rows):
    assert upload("dummy_bank_statement.csv").status_code == 201
    assert upload("dummy_bank_february.csv").status_code == 201

    # Dedupe is per file content, not per institution.
    assert count_rows("statements") == 2
    assert count_rows("transactions") == 6


def test_a_malformed_file_leaves_nothing_behind(upload, count_rows):
    response = upload("dummy_bank_malformed.csv")

    assert response.status_code == 422
    assert "row 3" in response.json()["detail"]
    assert count_rows("statements") == 0
    assert count_rows("transactions") == 0


def test_an_unknown_layout_leaves_nothing_behind(upload, count_rows):
    response = upload("unknown_institution.csv")

    assert response.status_code == 422
    assert count_rows("statements") == 0


def test_statement_id_filter_scopes_results_to_one_statement(upload, client):
    january = upload("dummy_bank_statement.csv").json()
    february = upload("dummy_bank_february.csv").json()

    jan_rows = client.get("/transactions", params={"statement_id": january["id"]}).json()
    feb_rows = client.get("/transactions", params={"statement_id": february["id"]}).json()

    assert len(jan_rows) == 4
    assert len(feb_rows) == 2
    assert all(r["statement_ids"] == [february["id"]] for r in feb_rows)
    assert len(client.get("/transactions").json()) == 6


def test_an_overlapping_statement_stores_the_union_not_the_sum(upload, client, count_rows):
    """The bug this milestone exists to fix: two exports sharing a period.

    dummy_bank_overlap.csv repeats the 01-07 and 01-09 rows of the January
    statement and adds two of its own.
    """
    january = upload("dummy_bank_statement.csv").json()
    overlap = upload("dummy_bank_overlap.csv")

    assert overlap.status_code == 201
    body = overlap.json()
    assert body["transaction_count"] == 4  # rows in the file
    assert body["new_transaction_count"] == 2  # rows not already stored

    assert count_rows("statements") == 2
    assert count_rows("transactions") == 6  # union of 4 and 4, not 8
    assert count_rows("statement_transactions") == 8  # every row of both files

    # Each statement still reports its own file in full.
    assert len(client.get("/transactions", params={"statement_id": january["id"]}).json()) == 4
    assert len(client.get("/transactions", params={"statement_id": body["id"]}).json()) == 4

    # And the unfiltered list counts each transaction exactly once.
    rows = client.get("/transactions").json()
    assert len(rows) == 6
    shared = next(r for r in rows if r["description"] == "REFUND ELECTRONICS LTD")
    assert sorted(shared["statement_ids"]) == sorted([january["id"], body["id"]])


def test_a_wholly_duplicate_statement_is_accepted_and_adds_nothing(upload, count_rows):
    """Same content, different bytes: the file is recorded, the transactions are not.

    dummy_bank_restated.csv is the January statement with the blank line removed
    and the currency column upper-cased — neither of which changes a transaction.
    """
    upload("dummy_bank_statement.csv")
    response = upload("dummy_bank_restated.csv")

    assert response.status_code == 201
    body = response.json()
    assert body["transaction_count"] == 4
    assert body["new_transaction_count"] == 0

    assert count_rows("statements") == 2
    assert count_rows("transactions") == 4
    assert count_rows("statement_transactions") == 8


def test_repeated_transactions_in_one_statement_are_all_stored(upload, client, count_rows):
    """Three identical coffees are three real transactions, not one."""
    response = upload("dummy_bank_repeats.csv")

    assert response.status_code == 201
    assert response.json()["new_transaction_count"] == 3
    assert count_rows("transactions") == 3

    rows = client.get("/transactions").json()
    same_day = [r for r in rows if r["date"] == "2026-03-02"]
    assert len(same_day) == 2
    assert all(Decimal(r["amount"]) == Decimal("3.20") for r in same_day)


def test_statement_ordering_follows_the_file_not_the_ledger(upload, client):
    """Within a statement, rows come back in file order."""
    overlap = upload("dummy_bank_overlap.csv").json()

    rows = client.get("/transactions", params={"statement_id": overlap["id"]}).json()
    assert [r["date"] for r in rows] == ["2026-01-07", "2026-01-09", "2026-01-14", "2026-01-20"]


def test_a_real_adapter_deduplicates_overlapping_downloads(upload, client, count_rows):
    """Per-transaction identity, proven on a real institution's export.

    The two Revolut downloads share three source rows — one of which carries a
    fee and so contributes two transactions — so the overlap is four.
    """
    first = upload("revolut_statement.csv").json()
    second = upload("revolut_overlap.csv").json()

    assert first["source_institution"] == "revolut"
    assert first["account_ref"] == "Current"
    assert (first["transaction_count"], first["new_transaction_count"]) == (8, 8)
    assert (second["transaction_count"], second["new_transaction_count"]) == (6, 2)

    assert count_rows("statements") == 2
    assert count_rows("transactions") == 10  # the union, not 8 + 6
    assert count_rows("statement_transactions") == 14

    # Each download still reports its own whole file.
    assert len(client.get(f"/transactions?statement_id={first['id']}").json()) == 8
    assert len(client.get(f"/transactions?statement_id={second['id']}").json()) == 6

    shared = next(
        row
        for row in client.get("/transactions?limit=1000").json()
        if row["description"] == "Fee: Exchanged to EUR"
    )
    assert sorted(shared["statement_ids"]) == sorted([first["id"], second["id"]])
