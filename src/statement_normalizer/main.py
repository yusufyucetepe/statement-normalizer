from fastapi import FastAPI

from statement_normalizer import __version__
from statement_normalizer.api.routes import statements, transactions
from statement_normalizer.parsers import registry

app = FastAPI(
    title="statement-normalizer",
    version=__version__,
    summary="Normalize bank and broker statements into one transaction schema.",
)

app.include_router(statements.router)
app.include_router(transactions.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/parsers", tags=["meta"])
def list_parsers() -> list[dict]:
    """Which adapters are live, in the order detection considers them."""
    return [
        {
            "institution": parser.institution,
            "priority": parser.priority,
            "formats": sorted(f.value for f in parser.supported_formats),
        }
        for parser in registry.parsers
    ]
