from __future__ import annotations

import hashlib
import io
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import cached_property
from typing import ClassVar

from statement_normalizer.models.schemas import StatementFormat, Transaction

_PDF_MAGIC = b"%PDF-"


@dataclass(frozen=True)
class Word:
    """One word on a PDF page, with the geometry that gives it meaning.

    `top` is measured from the top of the page, so lines sort naturally; `x0`
    and `x1` are what let an adapter say which column a number belongs to.
    """

    text: str
    x0: float
    x1: float
    top: float

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class StatementFile:
    """An uploaded statement, fully read into memory.

    Detection asks every registered parser to look at the same file, so the file
    cannot be a stream: the first reader would consume it and every parser after
    it would see an empty buffer. Holding the bytes makes `can_parse` a pure
    function of an immutable value, cheap to call N times and trivial to
    construct in tests without FastAPI or an event loop.
    """

    filename: str
    content: bytes
    content_type: str | None = None

    @classmethod
    def from_path(cls, path, content_type: str | None = None) -> StatementFile:
        from pathlib import Path

        path = Path(path)
        return cls(filename=path.name, content=path.read_bytes(), content_type=content_type)

    @cached_property
    def extension(self) -> str:
        """Lowercased extension without the dot, e.g. ``csv``. Empty if there is none."""
        _, _, ext = self.filename.rpartition(".")
        return ext.lower() if "." in self.filename else ""

    @cached_property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @cached_property
    def is_pdf(self) -> bool:
        return self.content.startswith(_PDF_MAGIC)

    @cached_property
    def format(self) -> StatementFormat | None:
        if self.is_pdf or self.extension == "pdf":
            return StatementFormat.PDF
        if self.extension in {"csv", "txt"}:
            return StatementFormat.CSV
        return None

    @cached_property
    def text(self) -> str:
        """Decoded contents. Falls back to latin-1, which never raises, so that a
        mis-encoded file surfaces as a detection miss rather than a 500."""
        for encoding in ("utf-8-sig", "utf-8"):
            try:
                return self.content.decode(encoding)
            except UnicodeDecodeError:
                continue
        return self.content.decode("latin-1")

    def head(self, lines: int = 5) -> list[str]:
        """First `lines` non-empty lines — enough to sniff a header row."""
        out: list[str] = []
        for line in self.text.splitlines():
            if line.strip():
                out.append(line)
            if len(out) >= lines:
                break
        return out

    @cached_property
    def pdf_words(self) -> list[list[Word]]:
        """Words per page, positioned — `[]` when this is not a PDF.

        Cached for the same reason `text` is: detection hands every adapter the
        same file, and extracting a PDF once per adapter would make detection
        cost scale with the size of the parser set.

        Positions are the point of it. A statement PDF is a table, and which
        column a number sits in is the only thing distinguishing a debit from a
        credit — text alone throws that away.
        """
        if not self.is_pdf:
            return []
        import pdfplumber  # imported lazily: CSV uploads should not pay for it

        with pdfplumber.open(io.BytesIO(self.content)) as pdf:
            return [
                [
                    Word(text=w["text"], x0=w["x0"], x1=w["x1"], top=w["top"])
                    for w in page.extract_words()
                ]
                for page in pdf.pages
            ]

    @cached_property
    def pdf_text(self) -> str:
        """The PDF's words as plain text, one line per page. For cheap sniffing."""
        return "\n".join(" ".join(word.text for word in page) for page in self.pdf_words)


class StatementParser(ABC):
    """The adapter interface every institution-specific parser implements.

    Contract:
      * `can_parse` is a cheap sniff (extension, magic bytes, header row). It must
        not raise and must not depend on parser call order.
      * `parse` may be expensive and *should* raise `StatementParseError` when a
        file it claimed turns out to be malformed — falling through to another
        parser would hide the real problem.
    """

    #: Stable identifier written to `Transaction.source_institution`.
    institution: ClassVar[str]
    #: Formats this adapter handles; used by the registry to skip obvious misses.
    supported_formats: ClassVar[frozenset[StatementFormat]] = frozenset({StatementFormat.CSV})
    #: Higher wins when several parsers claim the same file. Ties are an error.
    priority: ClassVar[int] = 100

    @abstractmethod
    def can_parse(self, file: StatementFile) -> bool:
        """Return True if this adapter recognizes the file's layout."""

    @abstractmethod
    def parse(self, file: StatementFile) -> list[Transaction]:
        """Convert the file into normalized transactions, in statement order."""

    def extract_account_ref(self, file: StatementFile) -> str | None:
        """The account/IBAN/broker id this statement covers, if the export says.

        Optional: adapters whose format carries no account identifier inherit
        this and do nothing. Kept off `parse` so the required interface stays
        `can_parse` / `parse`.
        """
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} institution={self.institution!r} priority={self.priority}>"
