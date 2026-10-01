"""Choose the database: a local SQLite file by default, or DATABASE_URL when set."""

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_PATH = ROOT_DIR / "data" / "inventoryiq.db"
DATABASE_PATH_ENV_VAR = "INVENTORY_DATABASE_PATH"


def resolve_database_path(database_path: str | Path | None = None) -> Path:
    return Path(database_path or os.getenv(DATABASE_PATH_ENV_VAR) or DEFAULT_SQLITE_PATH)


def resolve_database_url(
    default_sqlite_path: str | Path = DEFAULT_SQLITE_PATH,
    *,
    database_path: str | Path | None = None,
) -> str:
    """An explicit path always means SQLite; otherwise DATABASE_URL wins over the default file."""

    if database_path is None:
        configured_url = os.getenv("DATABASE_URL", "").strip()
        if configured_url:
            return configured_url
        database_path = os.getenv(DATABASE_PATH_ENV_VAR) or default_sqlite_path

    sqlite_path = Path(database_path).resolve()
    return f"sqlite:///{sqlite_path.as_posix()}"
