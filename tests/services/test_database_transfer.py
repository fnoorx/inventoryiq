import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, insert, select, update

from scripts import database_transfer as transfer
from scripts.database_transfer import (
    DatabaseSummary,
    main,
    read_processed_orders,
    require_empty_destination,
    summarize_database,
    transfer_database,
    validate_inventory_ids,
    validate_scan_links,
)
from services.database_tables import (
    inventory_id_sequence, inventory_units, label_scans, metadata,
    processed_stockx_orders,
)


def test_processed_orders_merge_database_and_json(engine, tmp_path):
    path = tmp_path / "orders.json"
    path.write_text(json.dumps({"processed_order_numbers": [" existing ", "legacy", "legacy"]}))
    with engine.begin() as connection:
        connection.execute(insert(processed_stockx_orders).values(order_number="existing"))
        assert read_processed_orders(connection, str(path)) == {"existing", "legacy"}


@pytest.mark.parametrize("content", [
    "not json", "[]", "{}", '{"processed_order_numbers": "order"}',
    '{"processed_order_numbers": [null]}', '{"processed_order_numbers": [" "]}',
])
def test_invalid_legacy_json_is_rejected(engine, tmp_path, content):
    path = tmp_path / "orders.json"
    path.write_text(content)
    with engine.connect() as connection:
        with pytest.raises(ValueError):
            read_processed_orders(connection, str(path))


def test_legacy_source_requires_json_and_missing_file_is_rejected(engine, tmp_path):
    processed_stockx_orders.drop(engine)
    with engine.connect() as connection:
        with pytest.raises(ValueError, match="Provide --legacy-orders-json"):
            read_processed_orders(connection)
        with pytest.raises(FileNotFoundError):
            read_processed_orders(connection, str(tmp_path / "missing.json"))


@pytest.mark.parametrize("mode", ["--dry-run", "--confirm"])
def test_legacy_command_preserves_source_and_imports_json(tmp_path, mode):
    source_path, destination_path = tmp_path / "old.db", tmp_path / "new.db"
    urls = [f"sqlite:///{path.as_posix()}" for path in (source_path, destination_path)]
    for index, url in enumerate(urls):
        db_engine = create_engine(url)
        try:
            metadata.create_all(db_engine)
            if index == 0:
                processed_stockx_orders.drop(db_engine)
        finally:
            db_engine.dispose()
    json_path = tmp_path / "orders.json"
    json_path.write_text(json.dumps({"processed_order_numbers": ["legacy-1", "legacy-2"]}))
    before_source, before_json = source_path.read_bytes(), json_path.read_bytes()
    result = subprocess.run([
        sys.executable, "-m", "scripts.database_transfer",
        "--source-url", urls[0], "--destination-url", urls[1],
        "--legacy-orders-json", str(json_path), mode,
    ], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '"processed_order_count": 2' in result.stdout
    assert source_path.read_bytes() == before_source
    assert json_path.read_bytes() == before_json
    db_engine = create_engine(urls[1])
    try:
        with db_engine.connect() as connection:
            assert read_processed_orders(connection) == (
                {"legacy-1", "legacy-2"} if mode == "--confirm" else set()
            )
    finally:
        db_engine.dispose()


@pytest.mark.parametrize("populated_destination", [False, True])
@pytest.mark.parametrize("mode", [[], ["--dry-run"], ["--confirm"]])
def test_command_checks_separate_databases_without_changing_them(
    tmp_path, populated_destination, mode
):
    paths = [tmp_path / "source.db", tmp_path / "destination.db"]
    urls = [f"sqlite:///{path.as_posix()}" for path in paths]
    for index, url in enumerate(urls):
        database_engine = create_engine(url)
        try:
            metadata.create_all(database_engine)
            if index == 0 or populated_destination:
                with database_engine.begin() as connection:
                    connection.execute(
                        insert(processed_stockx_orders).values(order_number="test-order")
                    )
        finally:
            database_engine.dispose()

    before = [path.read_bytes() for path in paths]
    result = subprocess.run(
        [
            sys.executable, "-m", "scripts.database_transfer",
            "--source-url", urls[0], "--destination-url", urls[1],
            *mode,
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
    )

    assert paths[0].read_bytes() == before[0]
    if mode != ["--confirm"] or populated_destination:
        assert paths[1].read_bytes() == before[1]
    assert '"processed_order_count": 1' in result.stdout
    if populated_destination:
        assert result.returncode != 0
        assert "processed_stockx_orders contains data" in result.stderr
        assert "Destination is empty" not in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        if mode == ["--confirm"]:
            assert "Transfer committed." in result.stdout
            destination_engine = create_engine(urls[1])
            try:
                with destination_engine.connect() as connection:
                    assert connection.execute(
                        select(processed_stockx_orders.c.order_number)
                    ).scalars().all() == ["test-order"]
            finally:
                destination_engine.dispose()
        else:
            assert "Destination is empty. No data was copied." in result.stdout


@pytest.mark.parametrize("fail_midway", [False, True])
def test_transfer_preserves_all_rows_and_rolls_back_on_failure(engine, fail_midway):
    tables = (inventory_id_sequence, inventory_units, label_scans, processed_stockx_orders)
    with engine.begin() as source:
        source.execute(insert(inventory_id_sequence), [
            {"sequence_number": number, "allocated_at": "2026-09-26"}
            for number in (1, 42)
        ])
        insert_inventory(source, "INV-000001")
        source.execute(insert(label_scans).values(
            scan_id="SCAN-TEST", discord_message_id="message-test",
            discord_attachment_id="attachment-test", image_sha256="a" * 64,
            state="confirmed", location="Test", purchase_date="2026-09-26",
            linked_inventory_id="INV-000001",
            created_at="2026-09-26T00:00:00+00:00",
            updated_at="2026-09-26T00:00:00+00:00",
            confirmed_at="2026-09-26T00:00:00+00:00",
        ))
        source.execute(insert(processed_stockx_orders).values(order_number="test-order"))

    destination_engine = create_engine("sqlite:///:memory:")
    metadata.create_all(destination_engine)

    def fail_last_table(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO processed_stockx_orders"):
            raise RuntimeError("Controlled copy failure")

    try:
        with engine.connect() as source:
            if fail_midway:
                event.listen(destination_engine, "before_cursor_execute", fail_last_table)
                with pytest.raises(RuntimeError, match="Controlled copy failure"):
                    transfer_database(source, destination_engine, dry_run=False)
                event.remove(destination_engine, "before_cursor_execute", fail_last_table)
                with destination_engine.connect() as destination:
                    require_empty_destination(destination)

            assert transfer_database(source, destination_engine, dry_run=False) == (
                DatabaseSummary(1, 1, 1, 42)
            )
            with destination_engine.connect() as destination:
                for table in tables:
                    assert destination.execute(select(table)).mappings().all() == (
                        source.execute(select(table)).mappings().all()
                    )

            with pytest.raises(ValueError, match="Destination must be empty"):
                transfer_database(source, destination_engine, dry_run=False)
    finally:
        destination_engine.dispose()


def test_transfer_rejects_changed_records_and_rolls_back(engine, monkeypatch):
    with engine.begin() as source:
        source.execute(
            insert(inventory_id_sequence).values(
                sequence_number=42,
                allocated_at="2026-09-26T00:00:00+00:00",
            )
        )

    destination_engine = create_engine("sqlite:///:memory:")

    try:
        metadata.create_all(destination_engine)
        original_copy = transfer.copy_rows

        def copy_with_changed_timestamp(source, destination, processed_orders):
            original_copy(source, destination, processed_orders)
            destination.execute(
                update(inventory_id_sequence).values(
                    allocated_at="2000-01-01T00:00:00+00:00"
                )
            )

        monkeypatch.setattr(transfer, "copy_rows", copy_with_changed_timestamp)

        with engine.connect() as source:
            with pytest.raises(ValueError, match="Copied records differ"):
                transfer.transfer_database(source, destination_engine, dry_run=False)

        with destination_engine.connect() as destination:
            require_empty_destination(destination)
    finally:
        destination_engine.dispose()


def test_empty_destination_is_accepted(engine):
    with engine.connect() as connection:
        require_empty_destination(connection)


def test_invalid_source_stops_before_opening_destination(engine, tmp_path, monkeypatch, capsys):
    paths = [tmp_path / "source.db", tmp_path / "destination.db"]
    for path in paths:
        path.touch()
    urls = [f"sqlite:///{path.as_posix()}" for path in paths]
    opened_urls = []

    def tracked_engine(url):
        opened_urls.append(url)
        return engine

    with engine.begin() as connection:
        insert_inventory(connection, "INV-000042")
    monkeypatch.setattr("scripts.database_transfer.create_engine_for_url", tracked_engine)
    monkeypatch.setattr(
        "sys.argv",
        ["database_transfer", "--source-url", urls[0], "--destination-url", urls[1]],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert opened_urls == [urls[0]]
    assert "Destination is empty" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "table",
    [inventory_id_sequence, inventory_units, label_scans, processed_stockx_orders],
)
def test_destination_with_data_is_rejected_without_removing_it(engine, table):
    with engine.begin() as connection:
        if table is inventory_units:
            insert_inventory(connection, "INV-000001")
        elif table is inventory_id_sequence:
            connection.execute(insert(table).values(sequence_number=1, allocated_at="2026-09-26"))
        elif table is processed_stockx_orders:
            connection.execute(insert(table).values(order_number="test-order"))
        else:
            connection.execute(insert(table).values(
                scan_id="SCAN-TEST",
                discord_message_id="message-test",
                discord_attachment_id="attachment-test",
                image_sha256="a" * 64,
                state="received",
                location="Test",
                purchase_date="2026-09-26",
                created_at="2026-09-26T00:00:00+00:00",
                updated_at="2026-09-26T00:00:00+00:00",
            ))
        with pytest.raises(ValueError, match=table.name):
            require_empty_destination(connection)
        assert connection.execute(select(table)).first() is not None


@pytest.mark.parametrize(
    "linked_id, expected",
    [
        (None, []),
        ("INV-000001", []),
        ("INV-000099", ["Scan SCAN-TEST links to missing inventory item: INV-000099"]),
    ],
)
def test_scan_link_validation(engine, linked_id, expected):
    with engine.begin() as connection:
        insert_inventory(connection, "INV-000001")
        connection.execute(
            insert(label_scans).values(
                scan_id="SCAN-TEST",
                discord_message_id="message-test",
                discord_attachment_id="attachment-test",
                image_sha256="a" * 64,
                state="received" if linked_id is None else "confirmed",
                location="Test",
                purchase_date="2026-09-26",
                linked_inventory_id=linked_id,
                created_at="2026-09-26T00:00:00+00:00",
                updated_at="2026-09-26T00:00:00+00:00",
            )
        )
        assert validate_scan_links(connection) == expected
        assert connection.execute(
            select(label_scans.c.linked_inventory_id)
        ).scalar_one() == linked_id


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def insert_inventory(connection, inventory_id):
    connection.execute(
        insert(inventory_units).values(
            inventory_id=inventory_id,
            location="Test",
            purchase_date="2026-09-26",
            item_type="Footwear",
            style_code="TEST-001",
            product_name="Test shoe",
            size="10",
            price_paid=100,
            total_cost=113,
            status="Pending",
            sheet_sync_status="pending",
            created_at="2026-09-26T00:00:00+00:00",
            updated_at="2026-09-26T00:00:00+00:00",
        )
    )


def test_validation_accepts_empty_database_and_unused_allocations(engine):
    with engine.begin() as connection:
        assert validate_inventory_ids(connection) == []
        connection.execute(
            insert(inventory_id_sequence),
            [
                {"sequence_number": number, "allocated_at": "2026-09-26"}
                for number in (1, 42)
            ],
        )
        insert_inventory(connection, "INV-000001")
        assert validate_inventory_ids(connection) == []


@pytest.mark.parametrize(
    "inventory_id",
    ["bad-id", "INV-000000", "INV-1", "inv-000001", " INV-000001", "INV-000001 "],
)
def test_validation_reports_invalid_ids_without_changing_them(engine, inventory_id):
    with engine.begin() as connection:
        insert_inventory(connection, inventory_id)
        assert validate_inventory_ids(connection) == [
            f"Invalid inventory ID: {inventory_id!r}"
        ]
        assert connection.execute(
            select(inventory_units.c.inventory_id)
        ).scalar_one() == inventory_id


def test_validation_reports_missing_allocation(engine):
    with engine.begin() as connection:
        insert_inventory(connection, "INV-000042")
        assert validate_inventory_ids(connection) == [
            "Missing allocation for inventory ID: INV-000042"
        ]


@pytest.mark.parametrize("invalid_source", [False, True])
def test_command_reports_validation_and_exit_status(
    engine, tmp_path, monkeypatch, capsys, invalid_source
):
    # The CLI checks the path; queries use the isolated in-memory engine.
    source_path = tmp_path / "source.db"
    source_path.touch()
    monkeypatch.setattr(
        "scripts.database_transfer.create_engine_for_url", lambda url: engine
    )
    monkeypatch.setattr(
        "sys.argv",
        ["database_transfer", "--source-url", f"sqlite:///{source_path.as_posix()}"],
    )
    if invalid_source:
        with engine.begin() as connection:
            insert_inventory(connection, "INV-000042")
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 1
    else:
        main()

    report = json.loads(capsys.readouterr().out)
    assert report["summary"] == {
        "inventory_count": int(invalid_source),
        "label_scan_count": 0,
        "processed_order_count": 0,
        "highest_sequence": 0,
    }
    assert report["problems"] == (
        ["Missing allocation for inventory ID: INV-000042"] if invalid_source else []
    )


def test_summary_reports_empty_database_and_highest_allocation():
    engine = create_engine("sqlite:///:memory:")

    try:
        metadata.create_all(engine)

        with engine.begin() as connection:
            assert summarize_database(connection) == DatabaseSummary(0, 0, 0, 0)

            connection.execute(
                insert(inventory_id_sequence),
                [
                    {"sequence_number": 42, "allocated_at": "2026-09-26T00:00:00+00:00"},
                    {"sequence_number": 7, "allocated_at": "2026-09-26T00:00:00+00:00"},
                ],
            )

            assert summarize_database(connection) == DatabaseSummary(0, 0, 0, 42)
    finally:
        engine.dispose()
