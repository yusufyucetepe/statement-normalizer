class ParserError(Exception):
    """Base class for every failure raised by the parsing layer."""


class NoMatchingParser(ParserError):
    """No registered parser claimed the file."""

    def __init__(self, filename: str, tried: int) -> None:
        super().__init__(f"no parser recognized {filename!r} (tried {tried})")
        self.filename = filename
        self.tried = tried


class AmbiguousParserMatch(ParserError):
    """Two or more parsers claimed the file at the same priority.

    This is a bug in the parser set, not bad input: picking one arbitrarily would
    silently attribute transactions to the wrong institution.
    """

    def __init__(self, filename: str, institutions: list[str]) -> None:
        super().__init__(
            f"{len(institutions)} parsers claim {filename!r} at equal priority: "
            f"{', '.join(institutions)}"
        )
        self.filename = filename
        self.institutions = institutions


class StatementParseError(ParserError):
    """A parser recognized the file but could not read its contents."""

    def __init__(self, institution: str, message: str, *, row: int | None = None) -> None:
        location = f" at row {row}" if row is not None else ""
        super().__init__(f"[{institution}]{location} {message}")
        self.institution = institution
        self.row = row
