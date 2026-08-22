"""Parser adapters.

Concrete parsers are imported here so that importing this package is what
populates the registry. Discovery is deliberately explicit — no package
scanning — so the live parser set stays greppable.
"""

from statement_normalizer.parsers.base import StatementFile, StatementParser
from statement_normalizer.parsers.dummy_csv import DummyBankCsvParser
from statement_normalizer.parsers.exceptions import (
    AmbiguousParserMatch,
    NoMatchingParser,
    ParserError,
    StatementParseError,
)
from statement_normalizer.parsers.registry import ParseResult, ParserRegistry, registry

__all__ = [
    "AmbiguousParserMatch",
    "DummyBankCsvParser",
    "NoMatchingParser",
    "ParseResult",
    "ParserError",
    "ParserRegistry",
    "StatementFile",
    "StatementParseError",
    "StatementParser",
    "registry",
]
