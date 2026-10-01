"""SQLAlchemy engines shared by the repositories, for SQLite or PostgreSQL."""

import os
from pathlib import Path

from sqlalchemy import Engine, Table, create_engine, event, make_url, text
from sqlalchemy.dialects import postgresql, sqlite

from services.database_config import (
    DEFAULT_SQLITE_PATH,
    resolve_database_path,
    resolve_database_url,
)
from services.database_schema import ensure_database_schema


SQLITE_LOCK_TIMEOUT_SECONDS = 30


def create_engine_for_url(database_url: str) -> Engine:
    is_sqlite = make_url(database_url).get_backend_name() == "sqlite"
    connect_args = {"timeout": SQLITE_LOCK_TIMEOUT_SECONDS} if is_sqlite else {}
    engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)

    if is_sqlite:
        _serialize_sqlite_transactions(engine)
    elif engine.url.get_backend_name() == "postgresql":
        _supply_aws_credentials_when_configured(engine)

    return engine


def create_database_engine(
    default_sqlite_path: str | Path = DEFAULT_SQLITE_PATH,
    *,
    database_path: str | Path | None = None,
) -> Engine:
    database_url = resolve_database_url(default_sqlite_path, database_path=database_path)
    return create_engine_for_url(database_url)


def create_repository_engine(database_path: str | Path | None = None) -> Engine:
    """Engine for a repository; a local SQLite file is created and migrated on first use.

    A PostgreSQL schema is managed by Alembic migrations instead.
    """

    if database_path is not None or not os.getenv("DATABASE_URL", "").strip():
        ensure_database_schema(resolve_database_path(database_path))
    return create_database_engine(database_path=database_path)


def _supply_aws_credentials_when_configured(engine: Engine) -> None:
    from services.credentials import credentials_backend, read_database_credentials

    if credentials_backend() != "aws":
        return

    # Read on every new connection, so a rotated RDS password is picked up.
    @event.listens_for(engine, "do_connect")
    def supply_current_credentials(dialect, connection_record, args, parameters):
        credentials = read_database_credentials()
        parameters.update(user=credentials["username"], password=credentials["password"])


def _serialize_sqlite_transactions(engine: Engine) -> None:
    """Take SQLite's write lock at BEGIN so check-then-insert blocks run one at a time."""

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None
        dbapi_connection.execute("PRAGMA foreign_keys = ON")
        dbapi_connection.execute(f"PRAGMA busy_timeout = {SQLITE_LOCK_TIMEOUT_SECONDS * 1000}")

    @event.listens_for(engine, "begin")
    def begin_immediate(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def insert_ignoring_conflicts(connection, table: Table):
    """Build an INSERT that skips rows violating a unique constraint.

    The statement returns the primary key of an inserted row and no row when
    skipped; ``rowcount`` is not reliable for this across drivers.
    """

    dialect = postgresql if connection.dialect.name == "postgresql" else sqlite
    return dialect.insert(table).on_conflict_do_nothing().returning(*table.primary_key.columns)


def advance_postgresql_sequence(connection, sequence_number: int) -> None:
    """Keep PostgreSQL's ID sequence ahead of an explicitly imported inventory ID."""

    if connection.dialect.name != "postgresql":
        return

    current_value = connection.execute(
        text(
            "SELECT pg_sequence_last_value("
            "pg_get_serial_sequence('inventory_id_sequence', 'sequence_number')::regclass)"
        )
    ).scalar_one()

    if current_value is None or sequence_number > current_value:
        connection.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('inventory_id_sequence', 'sequence_number'), "
                ":sequence_number, true)"
            ),
            {"sequence_number": sequence_number},
        )
