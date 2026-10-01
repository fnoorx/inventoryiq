import pytest

from services.database_config import resolve_database_url


@pytest.fixture(autouse=True)
def clear_database_environment(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("INVENTORY_DATABASE_PATH", raising=False)

# No DATABASE_URL -> SQLite
def test_missing_database_url_uses_sqlite_default(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    result = resolve_database_url(
        default_sqlite_path=tmp_path / "inventoryiq.db"
    )

    assert result == f"sqlite:///{(tmp_path / 'inventoryiq.db').as_posix()}"

# Explicit SQLite URL -> SQLite
def test_explicit_sqlite_url_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///custom/test.db")

    assert resolve_database_url(
        default_sqlite_path=tmp_path / "inventoryiq.db"
    ) == "sqlite:///custom/test.db"

# Explicit PostgreSQL -> PostgreSQL
def test_postgresql_url_is_preserved(monkeypatch, tmp_path):
    database_url = (
        "postgresql+psycopg://inventoryiq:password@localhost:5432/inventoryiq"
    )
    monkeypatch.setenv("DATABASE_URL", database_url)

    assert resolve_database_url(
        default_sqlite_path=tmp_path / "inventoryiq.db"
    ) == database_url


def test_legacy_path_is_used_when_database_url_is_blank(monkeypatch, tmp_path):
    legacy_path = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_URL", "  ")
    monkeypatch.setenv("INVENTORY_DATABASE_PATH", str(legacy_path))

    assert resolve_database_url(tmp_path / "default.db") == (
        f"sqlite:///{legacy_path.as_posix()}"
    )


def test_database_url_wins_over_legacy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("INVENTORY_DATABASE_PATH", str(tmp_path / "legacy.db"))

    assert resolve_database_url() == "sqlite:///:memory:"


def test_explicit_path_wins_over_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused/test")
    monkeypatch.setenv("INVENTORY_DATABASE_PATH", str(tmp_path / "legacy.db"))
    explicit_path = tmp_path / "explicit.db"

    assert resolve_database_url(database_path=explicit_path) == (
        f"sqlite:///{explicit_path.as_posix()}"
    )
