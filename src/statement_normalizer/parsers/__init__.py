"""Parser adapters.

Concrete parsers are imported here so that importing this package is what
populates the registry. Discovery is deliberately explicit — no package
scanning — so the live parser set stays greppable.
"""

from statement_normalizer.parsers.base import StatementFile, StatementParser, Word
from statement_normalizer.parsers.dummy_csv import DummyBankCsvParser
from statement_normalizer.parsers.dummy_pdf import DummyBankPdfParser
from statement_normalizer.parsers.exceptions import (
    AmbiguousParserMatch,
    NoMatchingParser,
    ParserError,
    StatementParseError,
)
from statement_normalizer.parsers.registry import ParseResult, ParserRegistry, registry
from statement_normalizer.parsers.revolut_csv import RevolutCsvParser
from statement_normalizer.parsers.wise_csv import WiseCsvParser

__all__ = [
    "AmbiguousParserMatch",
    "DummyBankCsvParser",
    "DummyBankPdfParser",
    "NoMatchingParser",
    "ParseResult",
    "ParserError",
    "ParserRegistry",
    "RevolutCsvParser",
    "StatementFile",
    "StatementParseError",
    "StatementParser",
    "WiseCsvParser",
    "Word",
    "registry",
]
