"""SQLite connection helpers shared by the repositories."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


@contextmanager
def connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection with row access by column name."""

    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Run a write transaction that takes the lock up front and rolls back on error."""

    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        connection.commit()
