"""Round-trip integration test; use only a disposable local PostgreSQL database.

Set TEST_TRANSFER_POSTGRES_DATABASE_URL to an Alembic-migrated, empty database
whose name starts with inventoryiq_transfer_test. Test rows are removed afterward.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError

from scripts.database_transfer import (
    DatabaseSummary,
    require_empty_destination,
    transfer_database,
)
from services.database_tables import (
    inventory_id_sequence,
    inventory_units,
    label_scans,
    metadata,
    processed_stockx_orders,
)
from services.inventory_repository import InventoryRepository, InventoryUnitInput


POSTGRES_URL = os.getenv("TEST_TRANSFER_POSTGRES_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="TEST_TRANSFER_POSTGRES_DATABASE_URL is not configured",
)
TABLES = (inventory_id_sequence, inventory_units, label_scans, processed_stockx_orders)


@pytest.fixture
def postgres_destination():
    url = make_url(POSTGRES_URL)
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"127.0.0.1", "localhost", "::1"}
        or not (url.database or "").startswith("inventoryiq_transfer_test")
    ):
        pytest.fail("Use a disposable local inventoryiq_transfer_test database")

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            require_empty_destination(connection)
        try:
            yield engine
        finally:
            with engine.begin() as connection:
                connection.execute(text(
                    "TRUNCATE label_scans, inventory_units, inventory_id_sequence, "
                    "processed_stockx_orders RESTART IDENTITY"
                ))
    finally:
        engine.dispose()


def snapshot(engine):
    with engine.connect() as connection:
        return {
            table.name: [dict(row) for row in connection.execute(
                select(table).order_by(*table.primary_key.columns)
            ).mappings()]
            for table in TABLES
        }


def migrate_sqlite(url, monkeypatch):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[2] / "migrations")
    )
    with monkeypatch.context() as environment:
        environment.setenv("DATABASE_URL", url)
        command.upgrade(config, "head")


def seed_transfer_source(source_engine):
    timestamp = "2026-09-28T12:34:56.123456+00:00"
    with source_engine.begin() as connection:
        connection.execute(insert(inventory_id_sequence), [
            {"sequence_number": number, "allocated_at": timestamp}
            for number in (1, 42, 100)
        ])
        connection.execute(insert(inventory_units).values(
            inventory_id="INV-000042", location="Montréal test",
            purchase_date="2026-09-28", item_type="Footwear",
            style_code="TEST-042", product_name="Synthetic shoe", size="10.5",
            price_paid=100.25, total_cost=113.28, status="Pending",
            source_identifier="synthetic-source", stockx_product_id="test-product",
            stockx_variant_id="test-variant", sheet_row=7,
            sheet_sync_status="retry_pending", sheet_sync_error="Synthetic timeout",
            discord_message_id="test-message", created_at=timestamp,
            updated_at=timestamp, sheet_synced_at=None,
        ))
        connection.execute(insert(label_scans).values(
            scan_id="SCAN-ROUNDTRIP", discord_message_id="test-scan-message",
            discord_attachment_id="test-attachment", image_sha256="a" * 64,
            state="confirmed", barcode_results_json='["synthetic-barcode"]',
            raw_vision_json='{"test": true}',
            normalized_extraction_json='{"style_code": "TEST-042"}',
            market_data_json=None, stockx_product_id="test-product",
            stockx_variant_id="test-variant", validation_status="verified",
            warning_details_json='["synthetic warning"]', error_details=None,
            supplied_price=100.25, location="Montréal test",
            purchase_date="2026-09-28", linked_inventory_id="INV-000042",
            created_at=timestamp, updated_at=timestamp, confirmed_at=timestamp,
        ))
        connection.execute(insert(processed_stockx_orders), [
            {"order_number": number} for number in ("test-order-1", "test-order-2")
        ])


def test_sqlite_postgres_sqlite_preserves_records_and_next_ids(
    postgres_destination, tmp_path, monkeypatch
):
    source_path = tmp_path / "source.db"
    restored_path = tmp_path / "restored.db"
    source_url = f"sqlite:///{source_path.as_posix()}"
    restored_url = f"sqlite:///{restored_path.as_posix()}"
    for url in (source_url, restored_url):
        migrate_sqlite(url, monkeypatch)

    source_engine = create_engine(source_url)
    restored_engine = create_engine(restored_url)
    try:
        seed_transfer_source(source_engine)

        expected_records = snapshot(source_engine)
        source_bytes = source_path.read_bytes()
        expected_summary = DatabaseSummary(1, 1, 2, 100)

        with source_engine.connect() as source:
            assert transfer_database(source, postgres_destination, dry_run=True) == expected_summary
            with postgres_destination.connect() as destination:
                require_empty_destination(destination)
            assert transfer_database(source, postgres_destination, dry_run=False) == expected_summary
            with pytest.raises(ValueError, match="Destination must be empty"):
                transfer_database(source, postgres_destination, dry_run=False)

        assert snapshot(postgres_destination) == expected_records
        with postgres_destination.connect() as source:
            assert transfer_database(source, restored_engine, dry_run=False) == expected_summary

        assert snapshot(restored_engine) == expected_records
        assert snapshot(postgres_destination) == expected_records
        assert source_path.read_bytes() == source_bytes

        # Check both generators only after checking exact round-trip equality.
        for url in (POSTGRES_URL, restored_url):
            monkeypatch.setenv("DATABASE_URL", url)
            repository = InventoryRepository()
            try:
                following, created = repository.create_unit(InventoryUnitInput(
                    location="Test", purchase_date="2026-09-28", item_type="Footwear",
                    style_code="TEST-NEXT", product_name="Next synthetic shoe",
                    size="10", price_paid=10,
                ))
                assert created is True
                assert following.inventory_id == "INV-000101"
            finally:
                repository.engine.dispose()
    finally:
        source_engine.dispose()
        restored_engine.dispose()


def test_postgres_copy_failure_rolls_back_and_retry_succeeds(postgres_destination):
    source_engine = create_engine("sqlite:///:memory:")

    try:
        metadata.create_all(source_engine)
        with source_engine.begin() as source:
            source.execute(
                insert(inventory_id_sequence).values(
                    sequence_number=42,
                    allocated_at="2026-09-28T12:00:00+00:00",
                )
            )
            source.execute(
                insert(processed_stockx_orders).values(order_number="test-order")
            )

        expected_records = snapshot(source_engine)

        def fail_after_allocations(
            connection, cursor, statement, parameters, context, executemany
        ):
            if statement.startswith("INSERT INTO inventory_id_sequence"):
                connection.execute(text("SELECT 1 / 0"))

        event.listen(
            postgres_destination, "after_cursor_execute", fail_after_allocations
        )
        try:
            with source_engine.connect() as source:
                with pytest.raises(DataError, match="division by zero"):
                    transfer_database(source, postgres_destination, dry_run=False)
        finally:
            event.remove(
                postgres_destination, "after_cursor_execute", fail_after_allocations
            )

        with postgres_destination.connect() as destination:
            require_empty_destination(destination)
        assert snapshot(source_engine) == expected_records

        with source_engine.connect() as source:
            result = transfer_database(source, postgres_destination, dry_run=False)

        assert result == DatabaseSummary(0, 0, 1, 42)
        assert snapshot(postgres_destination) == expected_records
        assert snapshot(source_engine) == expected_records
    finally:
        source_engine.dispose()


def run_transfer_cli(*arguments):
    environment = os.environ.copy()
    for name in ("DATABASE_URL", "INVENTORY_DATABASE_PATH"):
        environment.pop(name, None)
    environment["PYTHON_DOTENV_DISABLED"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "scripts.database_transfer", *arguments],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("legacy_source", [False, True])
def test_cli_roundtrip_with_legacy_json_and_confirmation(
    postgres_destination, tmp_path, monkeypatch, legacy_source
):
    source_path = tmp_path / "cli-source.db"
    restored_path = tmp_path / "cli-restored.db"
    source_url = f"sqlite:///{source_path.as_posix()}"
    restored_url = f"sqlite:///{restored_path.as_posix()}"
    for url in (source_url, restored_url):
        migrate_sqlite(url, monkeypatch)

    source_engine = create_engine(source_url)
    restored_engine = create_engine(restored_url)
    try:
        seed_transfer_source(source_engine)
        expected_records = snapshot(source_engine)
        if legacy_source:
            processed_stockx_orders.drop(source_engine)

        legacy_path = tmp_path / "legacy-orders.json"
        legacy_path.write_text(json.dumps({
            "processed_order_numbers": [" test-order-1 ", "legacy-order", "legacy-order"]
        }), encoding="utf-8")
        expected_orders = {"test-order-1", "legacy-order"}
        if not legacy_source:
            expected_orders.add("test-order-2")
        expected_records["processed_stockx_orders"] = [
            {"order_number": number} for number in sorted(expected_orders)
        ]
        expected_report = {
            "summary": {
                "inventory_count": 1,
                "label_scan_count": 1,
                "processed_order_count": len(expected_orders),
                "highest_sequence": 100,
            },
            "problems": [],
        }
        source_before = source_path.read_bytes()
        legacy_before = legacy_path.read_bytes()
        source_arguments = (
            "--source-url", source_url, "--legacy-orders-json", str(legacy_path)
        )
        import_arguments = (*source_arguments, "--destination-url", POSTGRES_URL)

        preflight = run_transfer_cli(*source_arguments)
        assert preflight.returncode == 0, preflight.stderr
        assert json.loads(preflight.stdout) == expected_report

        # Both an omitted mode and explicit --dry-run must leave PostgreSQL empty.
        for mode in ((), ("--dry-run",)):
            dry_run = run_transfer_cli(*import_arguments, *mode)
            assert dry_run.returncode == 0, dry_run.stderr
            assert json.JSONDecoder().raw_decode(dry_run.stdout)[0] == expected_report
            assert "Destination is empty. No data was copied." in dry_run.stdout
            with postgres_destination.connect() as destination:
                require_empty_destination(destination)
            assert source_path.read_bytes() == source_before
            assert legacy_path.read_bytes() == legacy_before

        confirmed = run_transfer_cli(*import_arguments, "--confirm")
        assert confirmed.returncode == 0, confirmed.stderr
        assert "Transfer committed." in confirmed.stdout
        assert snapshot(postgres_destination) == expected_records

        repeated = run_transfer_cli(*import_arguments, "--confirm")
        assert repeated.returncode != 0
        assert "Destination must be empty" in repeated.stderr
        assert "Transfer committed." not in repeated.stdout
        assert snapshot(postgres_destination) == expected_records

        export_arguments = (
            "--source-url", POSTGRES_URL, "--destination-url", restored_url
        )
        export_preflight = run_transfer_cli("--source-url", POSTGRES_URL)
        assert export_preflight.returncode == 0, export_preflight.stderr
        assert json.loads(export_preflight.stdout) == expected_report

        restored_before = restored_path.read_bytes()
        export_dry_run = run_transfer_cli(*export_arguments, "--dry-run")
        assert export_dry_run.returncode == 0, export_dry_run.stderr
        assert "Destination is empty. No data was copied." in export_dry_run.stdout
        assert restored_path.read_bytes() == restored_before

        exported = run_transfer_cli(*export_arguments, "--confirm")
        assert exported.returncode == 0, exported.stderr
        assert "Transfer committed." in exported.stdout
        assert snapshot(restored_engine) == expected_records

        restored_after = restored_path.read_bytes()
        repeated_export = run_transfer_cli(*export_arguments, "--confirm")
        assert repeated_export.returncode != 0
        assert "Destination must be empty" in repeated_export.stderr
        assert "Transfer committed." not in repeated_export.stdout
        assert restored_path.read_bytes() == restored_after
        assert snapshot(postgres_destination) == expected_records
        assert source_path.read_bytes() == source_before
        assert legacy_path.read_bytes() == legacy_before
    finally:
        source_engine.dispose()
        restored_engine.dispose()
