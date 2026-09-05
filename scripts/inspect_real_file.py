#!/usr/bin/env python
"""Report what the adapters make of a real statement, without storing it anywhere.

Two items in `tasks/todo.md` need a file from an actual account: the Revolut
adapter's header is reconstructed from published documentation, and `dummy_pdf`'s
layout assumptions have never met a bank's own PDF. Both are answerable by
running one real file through detection and parsing — the file itself does not
need to enter the repository, and should not.

So this prints *structure*, not content: column names, row counts, geometry,
whether balances chain. Descriptions, payees and account numbers are redacted to
a length unless `--show-values` is passed, which makes the output safe to paste
into a bug report or a chat. Nothing is written to disk and no database is
touched.

    uv run python scripts/inspect_real_file.py ~/Downloads/statement.csv
    uv run python scripts/inspect_real_file.py ~/Downloads/statement.pdf
    uv run python scripts/inspect_real_file.py FILE --assume revolut
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from itertools import islice
from pathlib import Path

# Run from a checkout without installing the package first.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from statement_normalizer.models.schemas import StatementFormat, Transaction  # noqa: E402
from statement_normalizer.parsers import (  # noqa: E402
    StatementFile,
    StatementParseError,
    StatementParser,
    registry,
)
from statement_normalizer.parsers.csv_fields import normalize_header  # noqa: E402

#: A cell that looks like money, allowing for thousands separators, a trailing
#: minus, parenthesized negatives and a leading currency symbol.
_MONEY = re.compile(r"^[^\d\-(]{0,3}[-(]?\d[\d,. ]*[.,]\d{2}\)?-?$")
#: A cell that looks like a date with separators, whatever the field order.
_DATE = re.compile(r"^\s*(\d{1,4})[-/.](\d{1,2})[-/.](\d{1,4})")
#: PDF words within this many points of each other vertically are one line.
_LINE_TOLERANCE = 3.0
#: Column headers worth reporting the position of, lowercased.
_TABLE_WORDS = frozenset(
    {
        "date",
        "description",
        "details",
        "debit",
        "credit",
        "balance",
        "amount",
        "paid",
        "in",
        "out",
        "reference",
        "type",
        "money",
    }
)
#: How many data rows to sample when inferring a column's kind.
_SAMPLE = 200


def main() -> int:
    args = _parse_args()
    path = Path(args.path).expanduser()
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 2

    file = StatementFile.from_path(path)
    _file_section(file, path)
    claimed = _detection_section(file)

    if file.format is StatementFormat.PDF:
        _pdf_section(file, show=args.show_values)
    else:
        _csv_section(file, show=args.show_values)

    parser = _parser_to_run(claimed, args.assume)
    if parser is not None:
        _parse_section(file, parser, show=args.show_values)
    _next_steps(file, claimed, args.assume)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("path", help="the statement to inspect; it is only read")
    parser.add_argument(
        "--show-values",
        action="store_true",
        help="print descriptions and cell values verbatim (they are redacted by default)",
    )
    parser.add_argument(
        "--assume",
        metavar="INSTITUTION",
        help="parse with this adapter even if detection did not claim the file, "
        "to see whether parsing would have worked had detection matched",
    )
    return parser.parse_args()


# --------------------------------------------------------------------------- file


def _file_section(file: StatementFile, path: Path) -> None:
    _heading("FILE")
    _field("name", path.name)
    _field("size", f"{len(file.content):,} bytes")
    _field("sha256", file.sha256)
    _field("extension", file.extension or "(none)")
    _field("format", file.format.value if file.format else "(unrecognized — upload would 422)")
    if not file.is_pdf:
        _field("encoding", _encoding(file.content))


def _encoding(content: bytes) -> str:
    """Which decoding `StatementFile.text` will land on.

    The latin-1 fallback never raises, so a mis-encoded export parses fine and
    quietly mangles every accented merchant name rather than failing.
    """
    if content.startswith(b"\xef\xbb\xbf"):
        return "utf-8 with BOM"
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        return f"NOT utf-8 (byte {exc.start}) — decoded as latin-1, accents may be wrong"
    return "utf-8"


# ---------------------------------------------------------------------- detection


def _detection_section(file: StatementFile) -> list[StatementParser]:
    _heading("DETECTION")
    header = _normalized_header(file) if file.format is not StatementFormat.PDF else set()
    claimed: list[StatementParser] = []

    for parser in registry.parsers:
        name = f"{parser.institution}/{_formats(parser)}"
        if file.format is not None and file.format not in parser.supported_formats:
            _field(name, "skipped — different format")
            continue
        try:
            verdict = parser.can_parse(file)
        except Exception as exc:
            # The contract says can_parse must not raise. If one does, detection
            # is broken for every file, not just this one.
            _field(name, f"!! RAISED {type(exc).__name__}: {exc} — this is a bug")
            continue
        if verdict:
            claimed.append(parser)
            _field(name, "CLAIMS THIS FILE")
        else:
            _field(name, f"no — {_explain_miss(parser, file, header)}")

    print()
    if not claimed:
        _note("No adapter claims this file: uploading it would return 422.")
        _note("The column list below is what a new or corrected adapter needs.")
    elif len(claimed) > 1 and claimed[0].priority == claimed[1].priority:
        _note("Two adapters tie: upload would fail with AmbiguousParserMatch.")
    return claimed


def _explain_miss(parser: StatementParser, file: StatementFile, header: set[str]) -> str:
    """Why an adapter passed on the file, in terms of its own detection rule.

    Reads the public class attributes each adapter documents as its rule
    (`REQUIRED_COLUMNS`, `HEADER`, `MASTHEAD`); an adapter that publishes none
    just gets an honest shrug.
    """
    excluded = header & getattr(parser, "TRADING_COLUMNS", frozenset())
    if excluded:
        return f"deliberately excluded by columns {sorted(excluded)}"

    required = getattr(parser, "REQUIRED_COLUMNS", None)
    if required is not None:
        missing = sorted(required - header)
        return f"missing required columns {missing}" if missing else "header matched but still no"

    exact = getattr(parser, "HEADER", None)
    if exact is not None:
        if set(exact) == header:
            return "same columns, different order (this adapter needs an exact header)"
        return f"header is not exactly {list(exact)}"

    masthead = getattr(parser, "MASTHEAD", None)
    if masthead is not None:
        if not file.is_pdf:
            return "not a PDF"
        return f"{masthead!r} does not appear in the text"
    return "can_parse returned False"


# ---------------------------------------------------------------------------- csv


def _csv_section(file: StatementFile, *, show: bool) -> None:
    _heading("CSV LAYOUT")
    lines = file.text.splitlines()
    if not lines:
        _note("file is empty")
        return

    delimiter = _delimiter(file.text)
    if delimiter != ",":
        _field("delimiter", f"{delimiter!r} — every adapter here assumes ','")

    rows = [
        row
        for row in csv.reader(io.StringIO(file.text), delimiter=delimiter)
        if any(cell.strip() for cell in row)
    ]
    if not rows:
        _note("no rows")
        return

    preamble = _preamble_rows(rows)
    if preamble:
        _field(
            "header row",
            f"row {preamble + 1}, not row 1 — {preamble} preamble row(s) above it. "
            "can_parse only reads the first line, so detection cannot see this header",
        )
    raw_header, data = rows[preamble], rows[preamble + 1 :]

    _field("columns", str(len(raw_header)))
    _field("data rows", str(len(data)))
    ragged = {len(row) for row in data} - {len(raw_header)}
    if ragged:
        _field("ragged rows", f"rows with {sorted(ragged)} cells instead of {len(raw_header)}")

    print()
    print("  column                          normalized                      kind      filled")
    print("  " + "-" * 84)
    for index, name in enumerate(raw_header):
        values = [row[index].strip() for row in data[:_SAMPLE] if index < len(row)]
        filled = sum(1 for value in values if value)
        kind = _column_kind(values)
        print(
            f"  {_clip(name, 30):<30}  {_clip(normalize_header(name), 30):<30}  "
            f"{kind:<8}  {filled}/{len(values)}"
        )

    _date_evidence(raw_header, data)
    if show:
        print()
        _field("first data row", str(data[0]) if data else "(none)")


def _delimiter(text: str) -> str:
    """The separator that best explains the file, not the one csv.Sniffer guesses.

    Every candidate is tried through `csv.reader` rather than counted raw, so a
    comma inside a quoted payee name cannot vote. The winner is the one whose
    *typical* row has the most fields: the mode rather than the minimum, so a
    preamble or one ragged row does not decide it.
    """
    best, best_score = ",", 1
    for candidate in (",", ";", "\t", "|"):
        rows = csv.reader(io.StringIO(text), delimiter=candidate)
        counts = [len(row) for row in islice(rows, 20) if any(cell.strip() for cell in row)]
        if not counts:
            continue
        typical = Counter(counts).most_common(1)[0][0]
        if typical > best_score:
            best, best_score = candidate, typical
    return best


def _preamble_rows(rows: list[list[str]]) -> int:
    """How many rows sit above the real header.

    Plenty of banks open an export with the account holder, the period and a
    blank line. Every adapter here sniffs line 1, so this is the difference
    between "we need a new adapter" and "we need to skip two lines".
    """
    widths = [len(row) for row in rows[:15]]
    widest = max(widths)
    if widest < 2:
        return 0
    first_full = widths.index(widest)
    # Only call it preamble if every row above is genuinely narrower.
    return first_full if all(width < widest for width in widths[:first_full]) else 0


def _column_kind(values: list[str]) -> str:
    present = [value for value in values if value]
    if not present:
        return "empty"
    if all(_MONEY.match(value) for value in present):
        return "money"
    if all(_DATE.match(value) for value in present):
        return "date"
    if all(_is_number(value) for value in present):
        return "number"
    return "text"


def _is_number(value: str) -> bool:
    try:
        Decimal(value.replace(",", "").replace(" ", ""))
    except InvalidOperation:
        return False
    return True


def _date_evidence(raw_header: list[str], rows: list[list[str]]) -> None:
    """Say whether the file proves its own day/month order.

    A day-first date read as month-first does not raise — `03-04-2026` silently
    becomes 4 March. The only in-file proof is a component above 12.
    """
    for index, name in enumerate(raw_header):
        firsts, seconds = set(), set()
        for row in rows[:_SAMPLE]:
            if index >= len(row):
                continue
            match = _DATE.match(row[index].strip())
            if match and len(match.group(1)) <= 2:
                firsts.add(int(match.group(1)))
                seconds.add(int(match.group(2)))
        if not firsts:
            continue
        if max(firsts) > 12:
            verdict = "day-first (a first component > 12 proves it)"
        elif max(seconds, default=0) > 12:
            verdict = "month-first (a second component > 12 proves it)"
        else:
            verdict = "AMBIGUOUS — no component above 12; a wrong guess misdates every row"
        _field(f"date order in {name!r}", verdict)


# ---------------------------------------------------------------------------- pdf


def _pdf_section(file: StatementFile, *, show: bool) -> None:
    _heading("PDF LAYOUT")
    if not file.is_pdf:
        # `pdf_words` returns [] rather than raising for a non-PDF, which would
        # otherwise be reported below as a PDF with no text layer.
        _field("readable", "NO — named .pdf but does not begin with %PDF-")
        _note("This is not a PDF. Check the download, or that it is not an HTML")
        _note("error page or a zip that was saved with the wrong extension.")
        return
    try:
        pages = file.pdf_words
    except Exception as exc:
        _field("readable", f"NO — {type(exc).__name__}: {exc}")
        _note("If this says the file is encrypted, the statement is password protected")
        _note("(often a date of birth). pdfplumber needs the password; no adapter takes one.")
        return

    total = sum(len(page) for page in pages)
    _field("pages", str(len(pages)))
    if not pages:
        _note("A PDF header but no pages: the file is truncated or corrupt.")
        return

    _field("words", f"{total:,}  ({', '.join(str(len(page)) for page in pages[:10])} per page)")
    if total == 0:
        _note("No text layer: this is a scanned image. pdfplumber cannot read it and")
        _note("no adapter can parse it — it needs OCR, which this project does not do.")
        _note("That is a real finding: ask the bank for a digital statement instead.")
        return

    for number, words in enumerate(pages[:2], start=1):
        lines = _pdf_lines(words)
        print()
        _field(f"page {number}", f"{len(lines)} lines")
        headers = [line for line in lines if _looks_like_table_header(line)]
        if not headers:
            _note("no line looks like a column header — dummy_pdf reads column")
            _note("positions off the header row, so it has nothing to anchor to here")
        for line in headers[:2]:
            cells = "  ".join(f"{w.text}@{w.center:.0f}" for w in line if len(w.text) > 1)
            _field("  header row", _clip(cells, 88))
        _money_columns(lines)
        if show:
            for line in lines[:12]:
                print("    | " + " ".join(w.text for w in line))


def _pdf_lines(words: list) -> list[list]:
    lines: list[list] = []
    for word in sorted(words, key=lambda w: (w.top, w.x0)):
        if lines and abs(word.top - lines[-1][0].top) <= _LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def _looks_like_table_header(line: list) -> bool:
    words = {word.text.lower().strip(":") for word in line}
    return len(words & _TABLE_WORDS) >= 2


def _money_columns(lines: list[list]) -> None:
    """Cluster the money-shaped words by x position.

    This is the layout question that decides whether an adapter is even
    possible: two clusters means separate debit and credit columns, which is
    what `dummy_pdf` assumes; one means a single signed column, and the sign
    then has to come from the text rather than the geometry.
    """
    clusters: dict[int, int] = defaultdict(int)
    for line in lines:
        for word in line:
            if _MONEY.match(word.text):
                clusters[round(word.center / 10) * 10] += 1
    if not clusters:
        _note("  no money-shaped words found — amounts may use a separator we did not expect")
        return
    ordered = sorted(clusters.items())
    _field(
        "  amount columns",
        "  ".join(f"x≈{x} ({n} value{'s' if n > 1 else ''})" for x, n in ordered),
    )
    if len(ordered) == 1:
        _note("  one column: the sign lives in the text, not the geometry —")
        _note("  dummy_pdf's debit/credit column split does not apply here")


# -------------------------------------------------------------------------- parse


def _parser_to_run(claimed: list[StatementParser], assume: str | None) -> StatementParser | None:
    if assume:
        for parser in registry.parsers:
            if parser.institution == assume:
                return parser
        known = sorted({p.institution for p in registry.parsers})
        print(f"\nno adapter called {assume!r}; known: {known}", file=sys.stderr)
        return None
    return claimed[0] if claimed else None


def _parse_section(file: StatementFile, parser: StatementParser, *, show: bool) -> None:
    _heading(f"PARSE with {parser.institution}")
    try:
        transactions = parser.parse(file)
    except StatementParseError as exc:
        _field("result", f"FAILED — {exc}")
        _note("This is the loud failure: an upload would return 422 naming that row.")
        return
    except Exception as exc:
        _field("result", f"CRASHED — {type(exc).__name__}: {exc}")
        _note("A non-StatementParseError escaping parse() would be a 500. That is a bug.")
        return

    _field("transactions", str(len(transactions)))
    if not transactions:
        _note("Parsed clean but produced nothing — check whether every row was skipped.")
        return

    try:
        account = parser.extract_account_ref(file)
    except Exception as exc:
        account = f"(raised {type(exc).__name__})"
    _field("account_ref", str(account) if account else "None — dedupe_key will be NULL")
    if not account:
        _note("Without an account ref every re-download double-counts.")

    dates = sorted(txn.date for txn in transactions)
    _field("date range", f"{dates[0]} .. {dates[-1]}")
    _field("currencies", ", ".join(sorted({txn.currency for txn in transactions})))
    directions = Counter(txn.direction.value for txn in transactions)
    _field("directions", ", ".join(f"{n} {d}" for d, n in sorted(directions.items())))
    with_id = sum(1 for txn in transactions if txn.external_id)
    _field("external_id", f"{with_id}/{len(transactions)} rows carry one")
    _balance_chain(transactions)

    print()
    _field("first row", _describe(transactions[0], show=show))
    _field("last row", _describe(transactions[-1], show=show))


def _balance_chain(transactions: list[Transaction]) -> None:
    """The strongest correctness check a single file can give.

    If every running balance equals the last known one plus everything that
    moved since, then the signs, the direction mapping and the fee handling are
    all right at once. A break is where money was invented or lost.
    """
    checked, breaks = _chain_breaks(transactions)
    if not checked:
        _field("balance chain", "not checkable — too few rows carry a balance")
        return
    if not breaks:
        _field("balance chain", f"reconciles at all {checked} checkable points")
        return

    if not _chain_breaks(list(reversed(transactions)))[1]:
        _field("balance chain", f"{len(breaks)}/{checked} break — but REVERSED it is clean")
        _note("The export is newest-first. Rows are stored in file order, so this")
        _note("is a real finding for the adapter, not a parsing error.")
        return

    _field("balance chain", f"BREAKS at {len(breaks)}/{checked} points")
    for index, delta in breaks[:5]:
        _note(f"  row {index}: balance is {delta} away from what the amounts say")
    _note("A delta equal to a fee means the fee is being counted twice, or not at all.")


def _chain_breaks(transactions: list[Transaction]) -> tuple[int, list[tuple[int, Decimal]]]:
    """Walk the rows accumulating movements, and check each balance we meet.

    Two things stop this being a comparison of adjacent rows. A row may carry no
    balance at all — the Revolut adapter deliberately leaves it off the main row
    of a fee-split pair, because the intermediate point is not a real position in
    the ledger — so movements accumulate until the next row that does carry one.
    And a multi-currency export interleaves independent balances, so each
    currency is chained separately; comparing a EUR balance against a GBP one
    would report a break on every file Revolut emits.
    """
    anchor: dict[str, Decimal] = {}
    moved: dict[str, Decimal] = defaultdict(Decimal)
    checked = 0
    breaks: list[tuple[int, Decimal]] = []

    for index, txn in enumerate(transactions, start=1):
        currency = txn.currency
        moved[currency] += txn.signed_amount
        if txn.balance_after is None:
            continue
        if currency in anchor:
            checked += 1
            expected = anchor[currency] + moved[currency]
            if expected != txn.balance_after:
                breaks.append((index, txn.balance_after - expected))
        anchor[currency] = txn.balance_after
        moved[currency] = Decimal(0)
    return checked, breaks


def _describe(txn: Transaction, *, show: bool) -> str:
    description = repr(txn.description) if show else f"<{len(txn.description)} chars>"
    balance = txn.balance_after if txn.balance_after is not None else "-"
    return (
        f"{txn.date}  {txn.direction.value:<6}  {txn.amount} {txn.currency}  "
        f"balance {balance}  {description}"
    )


# --------------------------------------------------------------------------- exit


def _next_steps(file: StatementFile, claimed: list[StatementParser], assume: str | None) -> None:
    _heading("WHAT TO REPORT")
    if claimed:
        print("  The adapter claimed the file. Worth reporting: the transaction count")
        print("  against what you can count in the statement yourself, and the balance")
        print("  chain line above.")
    elif file.format is StatementFormat.PDF:
        print("  No PDF adapter matched, which is expected — only dummy_bank has one.")
        print("  The useful output is the PDF LAYOUT section: text layer, header row,")
        print("  and how many amount columns there are.")
    else:
        print("  No adapter matched. Paste the column table above; that is what an")
        print("  adapter needs, and it contains no transaction data.")
        if not assume:
            print("  Re-run with --assume revolut (or wise) to see whether parsing would")
            print("  have worked if only detection had matched.")
    print()
    print("  Output is redacted; --show-values prints descriptions and cells verbatim.")
    print("  Nothing was written and no statement content is in this output by default.")


def _heading(title: str) -> None:
    print(f"\n{title}\n{'=' * len(title)}")


def _field(label: str, value: str) -> None:
    print(f"  {label:<16} {value}")


def _note(text: str) -> None:
    print(f"  ~ {text}")


def _clip(text: str, width: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _formats(parser: StatementParser) -> str:
    return "+".join(sorted(fmt.value for fmt in parser.supported_formats))


def _normalized_header(file: StatementFile) -> set[str]:
    head = file.head(1)
    if not head:
        return set()
    return {normalize_header(column) for column in next(csv.reader(head))}


if __name__ == "__main__":
    raise SystemExit(main())
