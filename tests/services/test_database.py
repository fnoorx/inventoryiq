import os

import pytest
from sqlalchemy import text

from services.database import create_database_engine, create_engine_for_url


def test_sqlite_engine_executes_query():
    engine = create_engine_for_url("sqlite:///:memory:")

    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()

@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_DATABASE_URL"),
    reason="TEST_POSTGRES_DATABASE_URL is not configured",
)
def test_postgresql_engine_executes_query():
    engine = create_engine_for_url(os.environ["TEST_POSTGRES_DATABASE_URL"])

    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()

def test_default_engine_uses_sqlite_when_database_url_is_missing(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("INVENTORY_DATABASE_PATH", raising=False)

    engine = create_database_engine(tmp_path / "inventoryiq.db")

    try:
        assert engine.url.get_backend_name() == "sqlite"
        assert engine.url.database == (tmp_path / "inventoryiq.db").as_posix()
    finally:
        engine.dispose()


def test_engine_explicit_path_overrides_database_url(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused/test")
    path = tmp_path / "isolated.db"
    engine = create_database_engine(database_path=path)
    try:
        assert engine.url.database == path.as_posix()
        assert not path.exists()
    finally:
        engine.dispose()


@pytest.mark.parametrize("backend", ["aws", "AWS", " aws "])
def test_aws_backend_variants_supply_rds_credentials(monkeypatch, backend):
    monkeypatch.setenv("CREDENTIALS_BACKEND", backend)
    engine = create_engine_for_url("postgresql+psycopg://rds.example/inventory")

    try:
        assert len(engine.dialect.dispatch.do_connect) == 1
    finally:
        engine.dispose()


def test_local_backend_leaves_postgresql_credentials_alone(monkeypatch):
    monkeypatch.setenv("CREDENTIALS_BACKEND", "local")
    engine = create_engine_for_url("postgresql+psycopg://localhost/inventory")

    try:
        assert len(engine.dialect.dispatch.do_connect) == 0
    finally:
        engine.dispose()
