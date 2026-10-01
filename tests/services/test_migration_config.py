from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from services.database_tables import metadata
from services.inventory_repository import InventoryRepository, InventoryUnitInput


ROOT_DIR = Path(__file__).resolve().parents[2]


def migration_config():
    config = Config(output_buffer=StringIO())
    config.set_main_option("script_location", str(ROOT_DIR / "migrations"))
    return config


@pytest.mark.parametrize("database_url", [None, "  "])
def test_alembic_requires_explicit_url(monkeypatch, tmp_path, database_url):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    if database_url is not None:
        monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("INVENTORY_DATABASE_PATH", str(tmp_path / "legacy.db"))
    (tmp_path / ".env").write_text("DATABASE_URL=sqlite:///implicit.db\n")

    with pytest.raises(RuntimeError, match="Set DATABASE_URL explicitly"):
        command.upgrade(migration_config(), "head", sql=True)

    assert not list(tmp_path.glob("*.db"))


def test_alembic_accepts_percent_encoded_url_without_connecting(monkeypatch):
    url = "postgresql+psycopg://test:pass%40word@127.0.0.1/test"
    monkeypatch.setenv("DATABASE_URL", url)
    config = migration_config()

    command.upgrade(config, "head", sql=True)

    assert config.get_main_option("sqlalchemy.url") == url


def test_verified_existing_sqlite_schema_can_be_stamped(monkeypatch, tmp_path):
    database_path = tmp_path / "existing.db"
    repository = InventoryRepository(database_path)
    item, _ = repository.create_unit(
        InventoryUnitInput(
            location="DEMO",
            purchase_date="2026-09-07",
            item_type="Footwear",
            style_code="TEST-001",
            product_name="Test Shoe",
            size="10",
            price_paid=100,
        ),
        discord_message_id="adoption-check",
    )

    inspector = inspect(repository.engine)
    for table_name, table in metadata.tables.items():
        actual_columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        assert actual_columns == set(table.c.keys())

    with repository.engine.connect() as connection:
        triggers = set(
            connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            ).scalars()
        )

    assert {
        "inventory_units_inventory_id_immutable",
        "inventory_sequence_no_delete",
        "label_scans_scan_id_immutable",
    }.issubset(triggers)

    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite:///{database_path.resolve().as_posix()}",
    )
    command.stamp(migration_config(), "head")

    with repository.engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    assert revision == "d760594bf6a4"
    assert repository.get(item.inventory_id) == item
