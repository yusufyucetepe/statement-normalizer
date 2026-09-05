#!/usr/bin/env python
"""Fail if an account identifier that is not a fixture's has entered the repo.

Written after one did. A real IBAN reached a public commit inside the docstring
of the function whose whole job is redacting IBANs — the example value was
copied from a live account, and a docstring does not look like output, so it
survived review by looking like documentation.

That is the shape of the problem: every leak so far has been in something
classified as structural rather than as content — a header row, a reported
field, a docstring. This does not care how anything was classified. It reads the
bytes.

The rule is that **`tests/fixtures/` defines the sanctioned values**. Identifiers
that appear in a fixture are synthetic by policy and may be quoted anywhere;
anything else that is shaped like an account identifier is a finding. Adding a
new example therefore means adding it to a fixture first, which is the habit
worth enforcing.

    uv run python scripts/check_no_real_accounts.py

Exits non-zero on a finding, listing file, line and what matched.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

#: An IBAN: two letters of country, two check digits, then up to 30 alphanumeric
#: characters. Deliberately anchored on word boundaries — a longer run of
#: base64 or hex that happens to open this way is not an account number.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
#: A card number: 13-19 digits, optionally split by spaces or hyphens. Validated
#: with Luhn before being reported, because one digit run in ten passes by
#: chance and a checksum is what separates a PAN from a timestamp.
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

#: Where synthetic identifiers are allowed to be invented.
_FIXTURES = Path("tests/fixtures")
#: Files whose bytes are not text worth scanning.
_SKIP_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".lock"})


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    tracked = _tracked_files(root)
    sanctioned = _sanctioned(root, tracked)

    findings: list[str] = []
    for path in tracked:
        if _FIXTURES in path.parents or path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        findings.extend(_scan(root / path, path, sanctioned))

    if not findings:
        print(f"no unsanctioned account identifiers in {len(tracked)} tracked files")
        return 0

    print("Unsanctioned account identifiers found:\n", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    print(
        "\nIf this is a real account, remove it and rewrite the commit — it does not\n"
        "belong in the repository at all. If it is a made-up example, put it in a\n"
        f"fixture under {_FIXTURES}/ and quote that value instead.",
        file=sys.stderr,
    )
    return 1


def _tracked_files(root: Path) -> list[Path]:
    """Only what git actually stores — never `.venv` or build output."""
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(name) for name in listing.stdout.split("\0") if name]


def _sanctioned(root: Path, tracked: list[Path]) -> set[str]:
    """Every identifier appearing in a fixture, which is where they are invented."""
    allowed: set[str] = set()
    for path in tracked:
        if _FIXTURES not in path.parents or path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        text = _read(root / path)
        allowed.update(_IBAN.findall(text))
        allowed.update(_normalize(match) for match in _CARD.findall(text))
    return allowed


def _scan(full: Path, shown: Path, sanctioned: set[str]) -> list[str]:
    findings = []
    for number, line in enumerate(_read(full).splitlines(), start=1):
        for value in _IBAN.findall(line):
            if value not in sanctioned:
                findings.append(f"{shown}:{number}: IBAN-shaped {_mask(value)}")
        for raw in _CARD.findall(line):
            value = _normalize(raw)
            if value not in sanctioned and _luhn(value):
                findings.append(f"{shown}:{number}: card-shaped {_mask(value)}")
    return findings


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _normalize(value: str) -> str:
    return value.replace(" ", "").replace("-", "")


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _mask(value: str) -> str:
    """Report enough to find it, never the whole thing — this output is public too."""
    return f"{value[:4]}…{value[-2:]} ({len(value)} chars)"


if __name__ == "__main__":
    raise SystemExit(main())
