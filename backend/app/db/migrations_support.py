"""PostgreSQL extension requirements shared by migrations, readiness checks, and tests."""

from sqlalchemy import Connection, text

REQUIRED_EXTENSIONS = ("vector",)
OPTIONAL_EXTENSIONS = ("pg_trgm",)


def installed_extensions(connection: Connection) -> dict[str, str]:
    """Return {name: version} for the required and optional extensions that are installed."""
    rows = connection.execute(
        text("SELECT extname, extversion FROM pg_extension WHERE extname = ANY(:names)"),
        {"names": [*REQUIRED_EXTENSIONS, *OPTIONAL_EXTENSIONS]},
    )
    return {name: version for name, version in rows}
