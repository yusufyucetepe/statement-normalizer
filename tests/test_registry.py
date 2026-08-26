import pytest

from statement_normalizer.models.schemas import StatementFormat, Transaction
from statement_normalizer.parsers import (
    AmbiguousParserMatch,
    DummyBankCsvParser,
    NoMatchingParser,
    ParserRegistry,
    StatementFile,
    StatementParser,
    registry,
)


class _AlwaysMatches(StatementParser):
    institution = "always_a"
    supported_formats = frozenset({StatementFormat.CSV})
    priority = 100

    def can_parse(self, file: StatementFile) -> bool:
        return True

    def parse(self, file: StatementFile) -> list[Transaction]:
        return []


def _clone(name: str, priority: int) -> type[StatementParser]:
    return type(name, (_AlwaysMatches,), {"institution": name, "priority": priority})


def test_detect_routes_a_known_layout_to_its_parser(statement_file):
    parser = registry.detect(statement_file("dummy_bank_statement.csv"))

    assert isinstance(parser, DummyBankCsvParser)
    assert parser.institution == "dummy_bank"


def test_parse_result_carries_the_statement_metadata(statement_file):
    result = registry.parse(statement_file("dummy_bank_statement.csv"))

    assert result.institution == "dummy_bank"
    assert result.format is StatementFormat.CSV
    assert result.account_ref == "GB00DUMY12345678"
    assert len(result.transactions) == 4


def test_unrecognized_layout_raises_no_matching_parser(statement_file):
    with pytest.raises(NoMatchingParser) as exc:
        registry.detect(statement_file("unknown_institution.csv"))

    assert "unknown_institution.csv" in str(exc.value)


def test_highest_priority_parser_wins_over_a_generic_one():
    local = ParserRegistry()
    local.register(_clone("generic_csv", priority=10))
    local.register(_clone("specific_bank", priority=200))

    winner = local.detect(StatementFile(filename="x.csv", content=b"anything"))

    assert winner.institution == "specific_bank"


def test_equal_priority_matches_are_an_error_not_a_coin_flip():
    local = ParserRegistry()
    local.register(_clone("bank_one", priority=100))
    local.register(_clone("bank_two", priority=100))

    with pytest.raises(AmbiguousParserMatch) as exc:
        local.detect(StatementFile(filename="x.csv", content=b"anything"))

    assert sorted(exc.value.institutions) == ["bank_one", "bank_two"]
