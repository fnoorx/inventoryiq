import json
import shutil
import sys
from types import SimpleNamespace

from sqlalchemy import create_engine

from scripts import cloud_database
from scripts.database_transfer import summarize_database
from services.database_schema import ensure_database_schema
from services.inventory_repository import InventoryRepository, InventoryUnitInput


def test_cloud_dry_run_transfer_and_sqlite_recovery(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("CREDENTIALS_BACKEND", "aws")
    source_path = tmp_path / "source.db"
    repository = InventoryRepository(source_path)
    repository.create_unit(InventoryUnitInput(
        location="TEST", purchase_date="2026-09-30", item_type="Footwear",
        style_code="TEST", product_name="Test", size="10", price_paid=10,
    ))
    repository.engine.dispose()
    orders = tmp_path / "orders.json"
    orders.write_text(json.dumps({"processed_order_numbers": ["test-order"]}))
    destination = tmp_path / "destination.db"
    ensure_database_schema(destination)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{destination.as_posix()}")
    monkeypatch.setenv("DEPLOYMENT_BUCKET", "test-bucket")
    monkeypatch.setenv("AWS_REGION", "ca-central-1")
    monkeypatch.setattr(cloud_database, "configure_aws_database", lambda: None)
    recovered = tmp_path / "recovered.db"

    def download(bucket, key, filename):
        assert bucket == "test-bucket"
        shutil.copy2(orders if key.endswith("orders.json") else source_path, filename)

    def upload(filename, bucket, key):
        assert key == "database-backups/test/inventory.db"
        shutil.copy2(filename, recovered)

    monkeypatch.setattr(cloud_database.boto3, "client", lambda *a, **k: SimpleNamespace(
        download_file=download, upload_file=upload,
    ))
    engine = create_engine(f"sqlite:///{destination.as_posix()}")
    try:
        for action, expected_count in (("dry-run", 0), ("transfer", 1), ("export-sqlite", 1)):
            monkeypatch.setattr(sys, "argv", ["cloud_database", action, "--prefix", "test"])
            cloud_database.main()
            with engine.connect() as connection:
                assert summarize_database(connection).inventory_count == expected_count
        restored = create_engine(f"sqlite:///{recovered.as_posix()}")
        try:
            with restored.connect() as connection:
                summary = summarize_database(connection)
                assert summary.inventory_count == 1
                assert summary.processed_order_count == 1
                assert summary.highest_sequence == 1
        finally:
            restored.dispose()
    finally:
        engine.dispose()
