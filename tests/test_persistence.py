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
    assert credit["statement_id"] == body["id"]


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
    assert {r["statement_id"] for r in feb_rows} == {february["id"]}
    assert len(client.get("/transactions").json()) == 6
