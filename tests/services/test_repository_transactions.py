from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from services.database_tables import inventory_id_sequence, inventory_units
from services.inventory_repository import InventoryRepository, InventoryUnitInput
from services import label_scan_repository
from services.label_scan_repository import LabelScanRepository
from services.processed_order_repository import ProcessedOrderRepository


def test_sqlite_failed_inventory_insert_rolls_back_related_rows(tmp_path):
    repository = InventoryRepository(tmp_path / "inventory.db")
    invalid_values = InventoryUnitInput(
        location=None,
        purchase_date="2026-09-06",
        item_type="Footwear",
        style_code="TEST-001",
        product_name="Test Shoe",
        size="10",
        price_paid=100,
    )

    with pytest.raises(IntegrityError):
        repository.create_unit(invalid_values, discord_message_id="failure")

    with repository.engine.connect() as connection:
        allocation_count = connection.execute(
            select(func.count()).select_from(inventory_id_sequence)
        ).scalar_one()
        inventory_count = connection.execute(
            select(func.count()).select_from(inventory_units)
        ).scalar_one()

    following, _ = repository.create_unit(
        InventoryUnitInput(
            location="DEMO",
            purchase_date="2026-09-06",
            item_type="Footwear",
            style_code="TEST-001",
            product_name="Test Shoe",
            size="10",
            price_paid=100,
        ),
        discord_message_id="success",
    )

    assert allocation_count == 0
    assert inventory_count == 0
    assert following.inventory_id == "INV-000001"


def _run_concurrently(count, action):
    barrier = threading.Barrier(count)

    def run(index):
        barrier.wait()
        return action(index)

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(run, range(count)))


def _unit_values():
    return InventoryUnitInput(
        location="DEMO",
        purchase_date="2026-09-30",
        item_type="Footwear",
        style_code="TEST-001",
        product_name="Test Shoe",
        size="10",
        price_paid=100,
    )


def test_concurrent_create_unit_returns_existing_unit(tmp_path):
    database = tmp_path / "inventory.db"
    repositories = [InventoryRepository(database) for _ in range(4)]

    results = _run_concurrently(
        4, lambda i: repositories[i].create_unit(_unit_values(), "same-message")
    )

    assert sorted(created for _, created in results) == [False, False, False, True]
    assert {unit.inventory_id for unit, _ in results} == {"INV-000001"}


def test_concurrent_import_of_same_id_returns_existing_unit(tmp_path):
    database = tmp_path / "inventory.db"
    repositories = [InventoryRepository(database) for _ in range(4)]

    results = _run_concurrently(
        4, lambda i: repositories[i].import_unit("INV-000007", _unit_values(), 2)
    )

    assert sorted(created for _, created in results) == [False, False, False, True]


def test_concurrent_processed_order_add_reports_one_insert(tmp_path):
    database = tmp_path / "inventory.db"
    repositories = [ProcessedOrderRepository(database) for _ in range(4)]

    results = _run_concurrently(4, lambda i: repositories[i].add("order-1"))

    assert sorted(results) == [False, False, False, True]


def test_concurrent_label_scans_return_one_draft(tmp_path):
    database = tmp_path / "inventory.db"
    repositories = [LabelScanRepository(database) for _ in range(4)]

    results = _run_concurrently(
        4,
        lambda i: repositories[i].create_or_get(
            discord_message_id="message-1",
            discord_attachment_id=f"attachment-{i}",
            image_sha256="a" * 64,
            supplied_price=None,
            location="DEMO",
            purchase_date="2026-09-30",
        ),
    )

    assert sorted(created for _, created in results) == [False, False, False, True]
    assert len({scan.scan_id for scan, _ in results}) == 1


def test_label_scan_insert_conflict_returns_existing_draft(tmp_path, monkeypatch):
    repository = LabelScanRepository(tmp_path / "inventory.db")
    arguments = dict(
        discord_message_id="message-1",
        discord_attachment_id="attachment-1",
        image_sha256="b" * 64,
        supplied_price=None,
        location="DEMO",
        purchase_date="2026-09-30",
    )
    original, _ = repository.create_or_get(**arguments)
    find_duplicate = label_scan_repository._find_duplicate
    lookups = iter([None])

    # Simulate another writer inserting between the duplicate check and insert.
    monkeypatch.setattr(
        label_scan_repository,
        "_find_duplicate",
        lambda *args: next(lookups, None) or find_duplicate(*args),
    )

    scan, created = repository.create_or_get(**arguments)

    assert created is False
    assert scan.scan_id == original.scan_id
