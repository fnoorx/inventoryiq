from concurrent.futures import ThreadPoolExecutor
import os
import threading

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from services.database import create_engine_for_url
from services.database_tables import inventory_id_sequence, inventory_units
from services.inventory_repository import InventoryRepository, InventoryUnitInput
from services.label_scan_repository import (
    LabelScanRepository,
    SCAN_CONFIRMED,
    SCAN_VERIFIED,
)
from services.processed_order_repository import ProcessedOrderRepository


POSTGRES_URL = os.getenv("TEST_POSTGRES_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="TEST_POSTGRES_DATABASE_URL is not configured",
)


@pytest.fixture(autouse=True)
def empty_postgres(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    engine = create_engine_for_url(POSTGRES_URL)

    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE label_scans, inventory_units, "
                "inventory_id_sequence, processed_stockx_orders RESTART IDENTITY"
            )
        )

    yield

    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE label_scans, inventory_units, "
                "inventory_id_sequence, processed_stockx_orders RESTART IDENTITY"
            )
        )

    engine.dispose()


def inventory_values(location: str | None = "DEMO") -> InventoryUnitInput:
    return InventoryUnitInput(
        location=location,
        purchase_date="2026-09-06",
        item_type="Footwear",
        style_code="TEST-001",
        product_name="Test Shoe",
        size="10",
        price_paid=100,
    )


def test_inventory_contract_and_imported_sequence():
    repository = InventoryRepository()

    first, created = repository.create_unit(
        inventory_values(),
        discord_message_id="message-1",
    )
    repeated, repeated_created = repository.create_unit(
        inventory_values(),
        discord_message_id="message-1",
    )
    imported, imported_created = repository.import_unit(
        "INV-000100",
        inventory_values(),
        sheet_row=20,
    )
    following, _ = repository.create_unit(
        inventory_values(),
        discord_message_id="message-2",
    )

    assert created is True
    assert first.inventory_id == "INV-000001"
    assert repeated_created is False
    assert repeated.inventory_id == first.inventory_id
    assert imported_created is True
    assert imported.inventory_id == "INV-000100"
    assert following.inventory_id == "INV-000101"


def test_label_scan_contract():
    repository = LabelScanRepository()

    scan, created = repository.create_or_get(
        discord_message_id="message-1",
        discord_attachment_id="attachment-1",
        image_sha256="a" * 64,
        supplied_price=100,
        location="DEMO",
        purchase_date="2026-09-06",
    )
    repeated, repeated_created = repository.create_or_get(
        discord_message_id="message-2",
        discord_attachment_id="attachment-2",
        image_sha256="a" * 64,
        supplied_price=None,
        location="DEMO",
        purchase_date="2026-09-06",
    )
    analyzed = repository.save_analysis(
        scan.scan_id,
        state=SCAN_VERIFIED,
        barcode_results=[],
        raw_vision=None,
        normalized_extraction={"style_code": "TEST-001"},
        market_data=None,
        validation_status=SCAN_VERIFIED,
        warnings=[],
    )
    confirmed = repository.mark_confirmed(scan.scan_id, "INV-000001")

    assert created is True
    assert repeated_created is False
    assert repeated.scan_id == scan.scan_id
    assert analyzed.state == SCAN_VERIFIED
    assert confirmed.state == SCAN_CONFIRMED
    assert confirmed.linked_inventory_id == "INV-000001"


def test_processed_order_contract():
    repository = ProcessedOrderRepository()

    assert repository.add("order-1") is True
    assert repository.add("order-1") is False
    assert repository.list_order_numbers() == {"order-1"}


def test_failed_inventory_insert_rolls_back_related_rows():
    repository = InventoryRepository()

    with pytest.raises(IntegrityError):
        repository.create_unit(
            inventory_values(location=None),
            discord_message_id="failure",
        )

    with repository.engine.connect() as connection:
        allocation_count = connection.execute(
            select(func.count()).select_from(inventory_id_sequence)
        ).scalar_one()
        inventory_count = connection.execute(
            select(func.count()).select_from(inventory_units)
        ).scalar_one()

    following, _ = repository.create_unit(
        inventory_values(),
        discord_message_id="success",
    )

    assert allocation_count == 0
    assert inventory_count == 0
    assert following.inventory_id == "INV-000002"


def _run_concurrently(count, action):
    barrier = threading.Barrier(count)

    def run(index):
        barrier.wait()
        return action(index)

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(run, range(count)))


def test_concurrent_create_unit_returns_existing_unit():
    repositories = [InventoryRepository() for _ in range(4)]

    results = _run_concurrently(
        4,
        lambda i: repositories[i].create_unit(
            inventory_values(), discord_message_id="same-message"
        ),
    )

    assert sorted(created for _, created in results) == [False, False, False, True]
    assert len({unit.inventory_id for unit, _ in results}) == 1


def test_concurrent_import_of_same_id_returns_existing_unit():
    repositories = [InventoryRepository() for _ in range(4)]

    results = _run_concurrently(
        4,
        lambda i: repositories[i].import_unit(
            "INV-000007", inventory_values(), sheet_row=2
        ),
    )

    assert sorted(created for _, created in results) == [False, False, False, True]


def test_concurrent_processed_order_add_reports_one_insert():
    repositories = [ProcessedOrderRepository() for _ in range(4)]

    results = _run_concurrently(4, lambda i: repositories[i].add("order-1"))

    assert sorted(results) == [False, False, False, True]


def test_concurrent_label_scans_return_one_draft():
    repositories = [LabelScanRepository() for _ in range(4)]

    results = _run_concurrently(
        4,
        lambda i: repositories[i].create_or_get(
            discord_message_id="message-1",
            discord_attachment_id=f"attachment-{i}",
            image_sha256="c" * 64,
            supplied_price=None,
            location="DEMO",
            purchase_date="2026-09-30",
        ),
    )

    assert sorted(created for _, created in results) == [False, False, False, True]
    assert len({scan.scan_id for scan, _ in results}) == 1
