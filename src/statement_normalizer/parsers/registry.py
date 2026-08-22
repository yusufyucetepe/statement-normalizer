from __future__ import annotations

from dataclasses import dataclass

from statement_normalizer.models.schemas import Transaction
from statement_normalizer.parsers.base import StatementFile, StatementParser
from statement_normalizer.parsers.exceptions import AmbiguousParserMatch, NoMatchingParser


@dataclass(frozen=True)
class ParseResult:
    institution: str
    transactions: list[Transaction]


class ParserRegistry:
    """Holds the available adapters and routes a file to exactly one of them.

    Registration is explicit (`@registry.register`) rather than package scanning,
    so the set of live parsers is greppable and a test can build its own empty
    registry instead of mutating a global.
    """

    def __init__(self) -> None:
        self._parsers: list[StatementParser] = []

    def register(self, parser_cls: type[StatementParser]) -> type[StatementParser]:
        """Class decorator. Instantiates the adapter once; adapters are stateless."""
        if not issubclass(parser_cls, StatementParser):
            raise TypeError(f"{parser_cls.__name__} does not implement StatementParser")
        institution = getattr(parser_cls, "institution", None)
        if not institution:
            raise TypeError(f"{parser_cls.__name__} must define a non-empty `institution`")
        if any(p.institution == institution for p in self._parsers):
            raise ValueError(f"a parser for {institution!r} is already registered")
        self._parsers.append(parser_cls())
        return parser_cls

    @property
    def parsers(self) -> list[StatementParser]:
        """Registered adapters, highest priority first, then by institution for
        a stable order that does not depend on import sequence."""
        return sorted(self._parsers, key=lambda p: (-p.priority, p.institution))

    def candidates(self, file: StatementFile) -> list[StatementParser]:
        """Every adapter that claims the file, best first."""
        return [
            parser
            for parser in self.parsers
            if (file.format is None or file.format in parser.supported_formats)
            and parser.can_parse(file)
        ]

    def detect(self, file: StatementFile) -> StatementParser:
        """Pick the single adapter responsible for `file`.

        Raises `NoMatchingParser` if none claim it, and `AmbiguousParserMatch` if
        the two best claims tie on priority — an arbitrary pick there would
        mislabel real money.
        """
        matches = self.candidates(file)
        if not matches:
            raise NoMatchingParser(file.filename, tried=len(self._parsers))
        if len(matches) > 1 and matches[0].priority == matches[1].priority:
            tied = [p.institution for p in matches if p.priority == matches[0].priority]
            raise AmbiguousParserMatch(file.filename, tied)
        return matches[0]

    def parse(self, file: StatementFile) -> ParseResult:
        parser = self.detect(file)
        return ParseResult(institution=parser.institution, transactions=parser.parse(file))


#: Process-wide registry. Concrete parsers register onto it at import time;
#: see `statement_normalizer.parsers.__init__`.
registry = ParserRegistry()
