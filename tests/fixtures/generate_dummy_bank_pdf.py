"""Generate `dummy_bank_statement.pdf`, the fixture the PDF adapter parses.

Committed alongside the PDF it produces, so the fixture is auditable in review
rather than being an opaque binary someone has to take on trust. Stdlib only —
a PDF *writer* would be a dependency the service itself never needs.

    uv run python tests/fixtures/generate_dummy_bank_pdf.py

The layout deliberately reproduces the things that make real statement PDFs hard:
amounts right-aligned into separate Debit and Credit columns (so direction is a
function of x position, not of a minus sign), a description that wraps onto a
second line, summary rows that are not transactions, a page break, and a footer
sitting in the middle of the description column.
"""

from __future__ import annotations

from pathlib import Path

PAGE_WIDTH, PAGE_HEIGHT = 595, 842
FONT_SIZE = 9
LEADING = 14

# Right edge of each right-aligned amount column, and the left edge of the two
# left-aligned ones. The adapter recovers these from the words themselves; they
# are only laid out here.
X_DATE = 50
X_DESCRIPTION = 120
X_DEBIT_RIGHT = 375
X_CREDIT_RIGHT = 450
X_BALANCE_RIGHT = 540

#: Helvetica advance widths (per 1000 units) for the characters amounts use.
_WIDTHS = {**dict.fromkeys("0123456789", 556), ",": 278, ".": 278, "-": 333}


def _width(text: str, size: int = FONT_SIZE) -> float:
    return sum(_WIDTHS.get(char, 556) for char in text) * size / 1000


def _escape(text: str) -> str:
    for char in ("\\", "(", ")"):
        text = text.replace(char, f"\\{char}")
    return text


class Page:
    """Text placed by absolute position, the way a statement generator would."""

    def __init__(self) -> None:
        self.ops: list[str] = []

    def at(self, x: float, top: float, text: str, size: int = FONT_SIZE) -> None:
        """Draw `text` with its left edge at `x`, `top` points from the page top."""
        y = PAGE_HEIGHT - top
        self.ops.append(f"BT /F1 {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({_escape(text)}) Tj ET")

    def right(self, right_edge: float, top: float, text: str) -> None:
        self.at(right_edge - _width(text), top, text)

    def stream(self) -> bytes:
        return "\n".join(self.ops).encode("latin-1")


def _header(page: Page, *, page_number: int, of: int) -> float:
    """Draw the masthead or the continuation header; return the first row's top."""
    if page_number == 1:
        page.at(X_DATE, 60, "DUMMY BANK PLC", size=14)
        page.at(X_DATE, 82, "Statement of Account")
        page.at(X_DATE, 104, "Account Number: GB00DUMY12345678")
        page.at(X_DATE, 118, "Statement Period: 07 Jan 2026 to 31 Jan 2026")
        page.at(X_DATE, 132, "All amounts in GBP")
        top = 170
    else:
        page.at(X_DATE, 60, "DUMMY BANK PLC")
        page.at(X_DATE, 74, "Account Number: GB00DUMY12345678 (continued)")
        top = 110

    page.at(X_DATE, top, "Date")
    page.at(X_DESCRIPTION, top, "Description")
    page.right(X_DEBIT_RIGHT, top, "Debit")
    page.right(X_CREDIT_RIGHT, top, "Credit")
    page.right(X_BALANCE_RIGHT, top, "Balance")

    # A footer that lands inside the description column: the adapter must not
    # mistake it for the continuation of the last transaction above it.
    page.at(270, 800, f"Page {page_number} of {of}")
    return top + LEADING * 2


def _row(page: Page, top: float, row: dict) -> float:
    page.at(X_DATE, top, row["date"])
    page.at(X_DESCRIPTION, top, row["description"])
    if row.get("debit"):
        page.right(X_DEBIT_RIGHT, top, row["debit"])
    if row.get("credit"):
        page.right(X_CREDIT_RIGHT, top, row["credit"])
    page.right(X_BALANCE_RIGHT, top, row["balance"])
    top += LEADING
    if row.get("wrapped"):
        page.at(X_DESCRIPTION, top, row["wrapped"])
        top += LEADING
    return top


PAGE_ONE = [
    {
        "date": "07 Jan 2026",
        "description": "CARD PAYMENT TO UTILITIES CO",
        "debit": "128.90",
        "balance": "3,279.63",
    },
    {
        "date": "09 Jan 2026",
        "description": "REFUND ELECTRONICS LTD",
        "credit": "64.99",
        "balance": "3,344.62",
    },
    {
        "date": "13 Jan 2026",
        "description": "DIRECT DEBIT COUNCIL TAX",
        "wrapped": "MONTHLY INSTALMENT REF 88213",
        "debit": "189.00",
        "balance": "3,155.62",
    },
    {
        "date": "17 Jan 2026",
        "description": "SUPERMARKET GROCERIES",
        "debit": "76.41",
        "balance": "3,079.21",
    },
]

PAGE_TWO = [
    {
        "date": "22 Jan 2026",
        "description": "TRANSFER FROM SAVINGS",
        "credit": "500.00",
        "balance": "3,579.21",
    },
    {
        "date": "28 Jan 2026",
        "description": "GYM MEMBERSHIP",
        "debit": "39.99",
        "balance": "3,539.22",
    },
    {
        "date": "31 Jan 2026",
        "description": "INTEREST PAID",
        "credit": "1.27",
        "balance": "3,540.49",
    },
]


def build_pages() -> list[Page]:
    first = Page()
    top = _header(first, page_number=1, of=2)
    # A summary line: no date, but a balance — so it is neither a transaction
    # nor the continuation of one.
    first.at(X_DESCRIPTION, top, "Balance brought forward")
    first.right(X_BALANCE_RIGHT, top, "3,408.53")
    top += LEADING
    for row in PAGE_ONE:
        top = _row(first, top, row)

    second = Page()
    top = _header(second, page_number=2, of=2)
    second.at(X_DESCRIPTION, top, "Balance brought forward")
    second.right(X_BALANCE_RIGHT, top, "3,079.21")
    top += LEADING
    for row in PAGE_TWO:
        top = _row(second, top, row)
    top += LEADING
    second.at(X_DESCRIPTION, top, "Closing balance")
    second.right(X_BALANCE_RIGHT, top, "3,540.49")

    return [first, second]


def render(pages: list[Page]) -> bytes:
    """Assemble the objects into a PDF with a correct cross-reference table."""
    page_ids = [4 + index * 2 for index in range(len(pages))]

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            f"<< /Type /Pages /Kids [{' '.join(f'{pid} 0 R' for pid in page_ids)}] "
            f"/Count {len(pages)} >>"
        ).encode("latin-1"),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for page, page_id in zip(pages, page_ids, strict=True):
        stream = page.stream()
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>"
        ).encode("latin-1")
        objects[page_id + 1] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode("latin-1") + objects[number] + b"\nendobj\n"

    startxref = len(out)
    count = max(objects) + 1
    out += f"xref\n0 {count}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for number in range(1, count):
        out += f"{offsets[number]:010d} 00000 n \n".encode("latin-1")
    out += f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n".encode(
        "latin-1"
    )
    return bytes(out)


if __name__ == "__main__":
    target = Path(__file__).with_name("dummy_bank_statement.pdf")
    target.write_bytes(render(build_pages()))
    print(f"wrote {target} ({target.stat().st_size} bytes)")
