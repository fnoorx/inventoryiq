import sqlite3

import pytest

from services import database_schema
from services.inventory_repository import (
    InventoryRepository,
    InventoryUnitInput,
    SYNCED,
    SYNC_FAILED,
    calculate_total_cost,
    resolve_database_path,
)
from services.inventory_service import (
    backfill_inventory_ids,
    create_inventory_unit,
    inventory_input_from_sheet_row,
    parse_sheet_cost,
    retry_inventory_sheet_sync,
)
from services.sheets import ensure_inventory_id_column


class FakeSpreadsheet:
    def __init__(self, sheet_id=123, tables=None):
        self.sheet_id = sheet_id
        self.tables = tables or []

    def fetch_sheet_metadata(self, params=None):
        assert params == {"includeGridData": "false"}
        return {
            "sheets": [
                {
                    "properties": {"sheetId": self.sheet_id},
                    "tables": self.tables,
                }
            ]
        }


class FakeInventorySheet:
    def __init__(self, rows, tables=None):
        self.id = 123
        self.rows = [list(row) for row in rows]
        self.row_count = max(len(self.rows), 20)
        self.col_count = max((len(row) for row in self.rows), default=1)
        self.spreadsheet = FakeSpreadsheet(self.id, tables=tables)
        self.updates = []
        self.batch_updates = []
        self.fail_batch_before_apply = False
        self.fail_batch_after_apply = False
        self.fail_next_data_cell_update = False

    def row_values(self, row_number):
        return list(self.rows[row_number - 1]) if row_number <= len(self.rows) else []

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def col_values(self, column_number):
        values = [
            row[column_number - 1] if column_number <= len(row) else ""
            for row in self.rows
        ]
        while values and values[-1] == "":
            values.pop()
        return values

    def update(self, values, range_name):
        row_number, _ = parse_a1_cell(range_name)
        if row_number > 1 and self.fail_next_data_cell_update:
            self.fail_next_data_cell_update = False
            raise RuntimeError("simulated Sheet cell failure")
        self.updates.append((range_name, values))
        apply_range(self.rows, range_name, values)

    def batch_update(self, updates):
        if self.fail_batch_before_apply:
            raise RuntimeError("simulated Sheet batch failure")
        copied_rows = [list(row) for row in self.rows]
        for update in updates:
            apply_range(copied_rows, update["range"], update["values"])
        self.rows = copied_rows
        self.batch_updates.append(updates)
        if self.fail_batch_after_apply:
            self.fail_batch_after_apply = False
            raise RuntimeError("simulated ambiguous Sheet response")

    def add_rows(self, count):
        self.row_count += count

    def add_cols(self, count):
        self.col_count += count


def parse_a1_cell(value):
    start = value.split(":", 1)[0]
    letters = "".join(character for character in start if character.isalpha())
    digits = "".join(character for character in start if character.isdigit())
    column = 0
    for character in letters:
        column = column * 26 + ord(character.upper()) - 64
    return int(digits), column


def apply_range(rows, range_name, values):
    start_row, start_column = parse_a1_cell(range_name)
    for row_offset, values_row in enumerate(values):
        row_number = start_row + row_offset
        while len(rows) < row_number:
            rows.append([])
        row = rows[row_number - 1]
        required_columns = start_column - 1 + len(values_row)
        while len(row) < required_columns:
            row.append("")
        for column_offset, value in enumerate(values_row):
            row[start_column - 1 + column_offset] = value


def sample_input(name="AETHER TEST SHOE"):
    return InventoryUnitInput(
        location="DEMO",
        purchase_date="07/21/2026",
        item_type="Footwear",
        style_code="QX1006-500",
        product_name=name,
        size="10",
        cost=180.0,
        source_identifier="194817794556",
        stockx_product_id="product-1",
        stockx_variant_id="variant-1",
    )


@pytest.mark.parametrize("value", ["", " -", "$ -   ", "–", "—"])
def test_sheet_accounting_dashes_are_zero_cost(value):
    assert parse_sheet_cost(value) == 0.0


def test_sheet_row_keeps_price_paid_subtotal_separate_from_total():
    row = inventory_row()
    row[10] = "100.00"
    row[11] = "119.75"

    values = inventory_input_from_sheet_row(row)

    assert values.price_paid == 100.0
    assert values.total_cost == 119.75


def inventory_header(with_id=False):
    header = [
        "Location",
        "DOP",
        "Date of Sale",
        "Status",
        "Platform",
        "Type",
        "Style Code",
        "Name",
        "Size",
        "Original Price",
        "Price Paid",
        "Total",
        "Payout",
        "Profit",
        "Margin",
        "Notes",
    ]
    if with_id:
        header.append("Inventory ID")
    return header


def inventory_row(inventory_id=""):
    return [
        "DEMO",
        "07/21/2026",
        "",
        "Pending",
        "",
        "Footwear",
        "QX1006-500",
        "AETHER TEST SHOE",
        "10",
        "",
        "180",
        '=IF(K2="","",K2*1.13)',
        "",
        '=IF(M2="","",M2-L2)',
        '=IF(M2="","",N2/M2)',
        "",
        inventory_id,
    ]


def test_repository_allocates_separate_non_reused_ids_and_is_idempotent(tmp_path):
    database_path = tmp_path / "inventory.db"
    repository = InventoryRepository(database_path)

    first, first_created = repository.create_unit(sample_input(), discord_message_id="100")
    duplicate_message, duplicate_created = repository.create_unit(
        sample_input(), discord_message_id="100"
    )
    identical_unit, identical_created = repository.create_unit(
        sample_input(), discord_message_id="101"
    )

    assert (first.inventory_id, first_created) == ("INV-000001", True)
    assert (duplicate_message.inventory_id, duplicate_created) == ("INV-000001", False)
    assert (identical_unit.inventory_id, identical_created) == ("INV-000002", True)

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="inventory_id is immutable"):
            connection.execute(
                "UPDATE inventory_units SET inventory_id = ? WHERE inventory_id = ?",
                ("INV-999999", "INV-000002"),
            )
        connection.rollback()
        connection.execute("DELETE FROM inventory_units WHERE inventory_id = ?", ("INV-000002",))
        connection.commit()

    next_unit, _ = repository.create_unit(sample_input(), discord_message_id="102")
    assert next_unit.inventory_id == "INV-000003"


def test_repository_uses_environment_path_and_parameterized_values(tmp_path, monkeypatch):
    database_path = tmp_path / "override" / "resell.db"
    monkeypatch.setenv("INVENTORY_DATABASE_PATH", str(database_path))

    repository = InventoryRepository()
    item, _ = repository.create_unit(
        sample_input(name="Robert'); DROP TABLE inventory_units;--"),
        discord_message_id="quoted-message",
    )

    assert resolve_database_path() == database_path
    assert repository.get(item.inventory_id).product_name == "Robert'); DROP TABLE inventory_units;--"


def test_repository_migrates_legacy_cost_to_subtotal_and_after_tax_total(tmp_path):
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE inventory_units (
                inventory_id TEXT PRIMARY KEY,
                location TEXT NOT NULL,
                purchase_date TEXT NOT NULL,
                item_type TEXT NOT NULL,
                style_code TEXT NOT NULL,
                product_name TEXT NOT NULL,
                size TEXT NOT NULL,
                cost REAL NOT NULL,
                status TEXT NOT NULL,
                source_identifier TEXT NOT NULL DEFAULT '',
                stockx_product_id TEXT NOT NULL DEFAULT '',
                stockx_variant_id TEXT NOT NULL DEFAULT '',
                sheet_row INTEGER,
                sheet_sync_status TEXT NOT NULL,
                sheet_sync_error TEXT,
                discord_message_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sheet_synced_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO inventory_units VALUES (
                'INV-000001', 'DEMO', '07/21/2026', 'Footwear', 'QX1006-500',
                'AETHER TEST SHOE', '10', 100, 'Pending', '', '', '', 2,
                'synced', NULL, 'message-1', 'created', 'updated', 'synced-at'
            )
            """
        )
        connection.commit()

    repository = InventoryRepository(database_path)
    item = repository.get("INV-000001")

    assert item.price_paid == 100.0
    assert item.total_cost == 113.0
    with sqlite3.connect(database_path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(inventory_units)")]
    assert "price_paid" in columns
    assert "total_cost" in columns
    assert "cost" not in columns


def test_database_schema_is_versioned_and_initialized_once_per_file(tmp_path, monkeypatch):
    database_path = tmp_path / "versioned.db"
    InventoryRepository(database_path)
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == database_schema.CURRENT_SCHEMA_VERSION

    migrate = pytest.fail
    monkeypatch.setattr(database_schema, "migrate_to_version_1", migrate)
    InventoryRepository(database_path)


def test_total_cost_uses_currency_rounding():
    assert calculate_total_cost(69.98) == 79.08


def test_creation_batches_operational_cells_and_id_without_overwriting_formulas(tmp_path):
    sheet = FakeInventorySheet([inventory_header(), inventory_row()])
    repository = InventoryRepository(tmp_path / "inventory.db")
    original_formula = sheet.rows[1][11]

    result = create_inventory_unit(
        sample_input(),
        "discord-1",
        sheet=sheet,
        repository=repository,
    )

    assert result.item.inventory_id == "INV-000001"
    assert result.sheet_synced is True
    assert result.item.sheet_row == 3
    assert sheet.rows[0][16] == "Inventory ID"
    assert sheet.rows[2][16] == "INV-000001"
    assert sheet.rows[1][11] == original_formula
    assert [update["range"] for update in sheet.batch_updates[0]] == ["A3:K3", "Q3"]

    repeated = create_inventory_unit(
        sample_input(),
        "discord-1",
        sheet=sheet,
        repository=repository,
    )
    assert repeated.created is False
    assert repeated.item.inventory_id == "INV-000001"
    assert len(sheet.batch_updates) == 1


def test_sheet_failure_preserves_unit_and_retry_never_allocates_another_id(tmp_path):
    sheet = FakeInventorySheet([inventory_header()])
    sheet.fail_batch_before_apply = True
    repository = InventoryRepository(tmp_path / "inventory.db")

    created = create_inventory_unit(
        sample_input(),
        "discord-failure",
        sheet=sheet,
        repository=repository,
    )

    assert created.item.inventory_id == "INV-000001"
    assert created.sheet_synced is False
    assert created.item.sheet_sync_status == SYNC_FAILED

    sheet.fail_batch_before_apply = False
    retried = retry_inventory_sheet_sync(
        "INV-000001", sheet=sheet, repository=repository
    )
    assert retried[0].sheet_synced is True
    assert repository.get("INV-000001").sheet_sync_status == SYNCED
    assert sheet.rows[1][16] == "INV-000001"

    no_op = retry_inventory_sheet_sync("INV-000001", sheet=sheet, repository=repository)
    assert no_op[0].already_synced is True
    assert len(sheet.batch_updates) == 1


def test_retry_recovers_ambiguous_sheet_success_by_finding_existing_id(tmp_path):
    sheet = FakeInventorySheet([inventory_header()])
    sheet.fail_batch_after_apply = True
    repository = InventoryRepository(tmp_path / "inventory.db")

    created = create_inventory_unit(
        sample_input(),
        "discord-ambiguous",
        sheet=sheet,
        repository=repository,
    )
    assert created.sheet_synced is False
    assert sheet.rows[1][16] == "INV-000001"

    retried = retry_inventory_sheet_sync(
        "INV-000001", sheet=sheet, repository=repository
    )
    assert retried[0].sheet_row == 2
    assert len(sheet.rows) == 2
    assert len(sheet.batch_updates) == 1


def test_id_column_is_added_after_existing_table_without_shifting_it():
    tables = [
        {
            "tableId": "table-1",
            "range": {
                "startRowIndex": 0,
                "endRowIndex": 3,
                "startColumnIndex": 0,
                "endColumnIndex": 16,
            },
        }
    ]
    sheet = FakeInventorySheet([inventory_header()], tables=tables)

    column = ensure_inventory_id_column(sheet)

    assert column == 17
    assert sheet.updates == [("Q1", [["Inventory ID"]])]


def test_backfill_assigns_identical_rows_separately_and_reports_bad_existing_ids(tmp_path):
    rows = [
        inventory_header(with_id=True),
        inventory_row(),
        inventory_row(),
        inventory_row("INV-000050"),
        inventory_row("INV-000050"),
        inventory_row("legacy-7"),
    ]
    sheet = FakeInventorySheet(rows)
    repository = InventoryRepository(tmp_path / "inventory.db")

    result = backfill_inventory_ids(sheet=sheet, repository=repository)

    assert [entry["inventory_id"] for entry in result.assigned] == [
        "INV-000001",
        "INV-000002",
        "INV-000051",
        "INV-000052",
    ]
    assert sheet.rows[1][16] != sheet.rows[2][16]
    assert result.imported_existing == [{"row": 4, "inventory_id": "INV-000050"}]
    assert result.duplicate_ids == [
        {
            "row": 5,
            "first_row": 4,
            "inventory_id": "INV-000050",
            "replacement_inventory_id": "INV-000051",
        }
    ]
    assert result.malformed_ids == [
        {
            "row": 6,
            "inventory_id": "LEGACY-7",
            "replacement_inventory_id": "INV-000052",
        }
    ]
    assert sheet.rows[4][16] == "INV-000051"
    assert sheet.rows[5][16] == "INV-000052"
    assert len(sheet.batch_updates) == 1
    assert len(sheet.batch_updates[0]) == 4

    next_unit, _ = repository.create_unit(sample_input(), discord_message_id="after-backfill")
    assert next_unit.inventory_id == "INV-000053"


def test_backfill_resumes_failed_sheet_write_with_same_id(tmp_path):
    sheet = FakeInventorySheet([inventory_header(with_id=True), inventory_row()])
    sheet.fail_batch_before_apply = True
    repository = InventoryRepository(tmp_path / "inventory.db")

    failed = backfill_inventory_ids(sheet=sheet, repository=repository)
    assert failed.failures[0]["inventory_id"] == "INV-000001"
    assert repository.get("INV-000001").sheet_sync_status == SYNC_FAILED

    sheet.fail_batch_before_apply = False
    resumed = backfill_inventory_ids(sheet=sheet, repository=repository)
    assert resumed.resumed == [{"row": 2, "inventory_id": "INV-000001"}]
    assert sheet.rows[1][16] == "INV-000001"


def test_general_retry_repairs_a_failed_backfill_row_in_place(tmp_path):
    sheet = FakeInventorySheet([inventory_header(with_id=True), inventory_row()])
    sheet.fail_batch_before_apply = True
    repository = InventoryRepository(tmp_path / "inventory.db")
    backfill_inventory_ids(sheet=sheet, repository=repository)

    sheet.fail_batch_before_apply = False
    retried = retry_inventory_sheet_sync(sheet=sheet, repository=repository)

    assert retried[0].inventory_id == "INV-000001"
    assert retried[0].sheet_row == 2
    assert sheet.rows[1][16] == "INV-000001"
    assert len(sheet.rows) == 2
    assert sheet.batch_updates == []
