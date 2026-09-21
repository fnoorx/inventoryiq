import json
from types import SimpleNamespace

import pytest

from services.inventory_repository import InventoryRepository, InventoryUnitInput
from services.stockx_order_sheet_sync import (
    ACTIVE_PAYOUT_READY_STATUSES,
    HISTORICAL_COMPLETED_STATUS,
    SOLD_STATUS,
    SHEET_COLUMNS,
    SheetColumns,
    find_first_unsold_matching_row,
    find_processed_row_needing_repair,
    history_lookback_dates,
    iter_payout_ready_orders,
    load_processed_order_numbers,
    normalize_sheet_date,
    normalize_size,
    order_needs_detail,
    order_sheet_payload,
    save_processed_order_numbers,
    set_row_values,
    split_sheet_style_codes,
    stockx_search_url,
    stockx_platform,
    sheet_style_matches,
    sync_stockx_payout_ready_orders,
)


class FakeSheet:
    def __init__(self, rows):
        self.rows = [list(row) for row in rows]
        self.updates = []
        self.batch_updates = []

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def update(self, values, range_name):
        self.updates.append((range_name, values))
        self._apply_cell_update(range_name, values[0][0])

    def batch_update(self, updates):
        self.batch_updates.append(updates)
        for update in updates:
            self._apply_cell_update(update["range"], update["values"][0][0])

    def _apply_cell_update(self, range_name, value):
        row_number, column_number = parse_a1_cell(range_name)
        while len(self.rows) < row_number:
            self.rows.append([])
        row = self.rows[row_number - 1]
        while len(row) < column_number:
            row.append("")
        row[column_number - 1] = value

    def cell(self, row_number, column_number):
        if row_number > len(self.rows):
            return SimpleNamespace(value="")
        row = self.rows[row_number - 1]
        if column_number > len(row):
            return SimpleNamespace(value="")
        return SimpleNamespace(value=row[column_number - 1])


def parse_a1_cell(value):
    letters = "".join(character for character in value if character.isalpha())
    digits = "".join(character for character in value if character.isdigit())
    column = 0
    for character in letters:
        column = column * 26 + (ord(character.upper()) - 64)
    return int(digits), column


def test_sheet_columns_match_inventory_layout():
    assert SHEET_COLUMNS == SheetColumns(
        date_of_sale=3,
        status=4,
        platform=5,
        style_code=7,
        size=9,
        payout=13,
        profit=14,
        data_start_row=1,
    )


def test_find_first_unsold_matching_row_matches_style_and_size_then_skips_sold_rows():
    rows = [
        ["Location", "DOP", "Date of Sale", "Status", "Platform", "Type", "Style Code", "Name", "Size", "Payout"],
        ["A", "2026-01-01", "2026-02-01", "", "", "Footwear", "QX1001-200", "Shoe", "10", "80"],
        ["A", "2026-01-02", "", "", "", "Footwear", "QX1001-200", "Shoe", "10", ""],
        ["A", "2026-01-03", "", "", "", "Footwear", "QX1001-200", "Shoe", "11", ""],
    ]
    columns = SHEET_COLUMNS

    row_number = find_first_unsold_matching_row(rows, "qx1001-200", "M 10", columns)

    assert row_number == 3


def test_style_code_matching_checks_each_slash_delimited_code():
    combined = " QX1004-400 / QX1003-400 "

    assert split_sheet_style_codes(combined) == ("QX1004-400", "QX1003-400")
    assert sheet_style_matches(combined, "qx1004-400")
    assert sheet_style_matches(combined, "QX1003-400")
    assert not sheet_style_matches(combined, "QX1004-01")
    assert not sheet_style_matches(combined, "QX1005-400")


def test_set_row_values_updates_working_snapshot_without_copying_all_rows():
    rows = [["header"], ["old"]]

    updated = set_row_values(rows, row_number=2, updates={1: "new", 3: "value"})

    assert updated is rows
    assert rows == [["header"], ["new", "", "value"]]


def test_unsold_row_matches_either_style_code_in_sheet_cell():
    rows = [
        ["Location", "DOP", "Date of Sale", "Status", "Platform", "Type", "Style Code", "Name", "Size", "Payout"],
        ["A", "2026-01-01", "", "", "", "Footwear", "QX1004-400/QX1003-400", "Shoe", "10", ""],
    ]

    assert find_first_unsold_matching_row(rows, "QX1004-400", "10", SHEET_COLUMNS) == 2
    assert find_first_unsold_matching_row(rows, "QX1003-400", "10", SHEET_COLUMNS) == 2


def test_processed_row_repair_matches_either_style_code_in_sheet_cell():
    rows = [
        ["Location", "DOP", "Date of Sale", "Status", "Platform", "Type", "Style Code", "Name", "Size", "Payout"],
        ["A", "2026-01-01", "2026-07-01", "Sold", "StockX", "Footwear", "QX1004-400/QX1003-400", "Shoe", "10", ""],
    ]

    assert (
        find_processed_row_needing_repair(
            rows,
            style_code="QX1003-400",
            size="10",
            sale_date="2026-07-01",
            platform="StockX",
            columns=SHEET_COLUMNS,
        )
        == 2
    )


def test_order_sheet_payload_extracts_fields_without_requiring_written_order_number():
    order = {
        "orderNumber": "02-DEMO000001",
        "createdAt": "2026-07-01T13:51:47.000Z",
        "inventoryType": "FLEX",
        "product": {"styleId": " qx1001-200 ", "productName": "Aether Trail Test"},
        "variant": {"variantValue": "M 10.0"},
        "payout": {"totalPayout": "76.81"},
    }

    payload = order_sheet_payload(order)

    assert payload == {
        "order_number": "02-DEMO000001",
        "style_code": "QX1001-200",
        "size": "10",
        "sale_date": "2026-07-01",
        "payout": 76.81,
        "platform": "StockX Flex",
        "product_name": "Aether Trail Test",
        "stockx_url": "https://stockx.com/search?s=QX1001-200",
    }


def test_stockx_platform_maps_standard_and_flex():
    assert stockx_platform("STANDARD", "123-456") == "StockX"
    assert stockx_platform("FLEX", "02-DEMO000001") == "StockX Flex"
    assert stockx_platform(None, "02-DEMO000001") == "StockX Flex"


def test_normalize_size_handles_common_stockx_size_formats():
    assert normalize_size("M 10.0") == "10"
    assert normalize_size("US 10.5") == "10.5"
    assert normalize_size("W 7.0") == "7W"
    assert normalize_size("7W") == "7W"


def test_normalize_sheet_date_handles_sheet_and_stockx_formats():
    assert normalize_sheet_date("2026-07-01") == "2026-07-01"
    assert normalize_sheet_date("07/01/2026") == "2026-07-01"


def test_stockx_search_url_uses_style_code():
    assert stockx_search_url(" qx1001-200 ") == "https://stockx.com/search?s=QX1001-200"
    assert stockx_search_url("") == ""


def test_order_needs_detail_only_when_required_list_fields_are_missing():
    complete_order = {
        "product": {"styleId": "QX1001-200"},
        "variant": {"variantValue": "10"},
        "payout": {"totalPayout": "76.81"},
    }

    assert order_needs_detail(complete_order) is False
    assert order_needs_detail({**complete_order, "payout": None}) is True
    assert order_needs_detail({**complete_order, "variant": {}}) is True


def test_iter_payout_ready_orders_reconciles_active_and_recent_history(monkeypatch):
    active_calls = []
    historical_calls = []
    detail_calls = []

    complete_pending = {
        "orderNumber": "pending-1",
        "product": {"styleId": "QX1001-200"},
        "variant": {"variantValue": "10"},
        "payout": {"totalPayout": "76.81"},
    }
    completed_needing_detail = {"orderNumber": "completed-1"}
    active_needing_detail = {"orderNumber": "active-detail-1"}
    active_detail = {
        "orderNumber": "active-detail-1",
        "createdAt": "2026-07-09T13:51:47.000Z",
        "product": {"styleId": "QX2006-100"},
        "variant": {"variantValue": "8"},
        "payout": {"totalPayout": "88"},
    }
    duplicate_history = {
        "orderNumber": "completed-1",
        "createdAt": "2026-07-08T13:51:47.000Z",
        "product": {"styleId": "QX1009-800"},
        "variant": {"variantValue": "9.5"},
        "payout": {"totalPayout": "102.25"},
    }
    new_history = {
        "orderNumber": "history-1",
        "product": {"styleId": "QX2007-612"},
        "variant": {"variantValue": "11"},
        "payout": {"totalPayout": "140"},
    }

    def fake_list_active_orders(order_status, page_size=100):
        active_calls.append((order_status, page_size))
        if order_status == "PAYOUTPENDING":
            return [complete_pending]
        if order_status == "PAYOUTCOMPLETED":
            return [completed_needing_detail, active_needing_detail]
        return []

    def fake_list_historical_orders(order_status, from_date, to_date, page_size=100):
        historical_calls.append((order_status, from_date, to_date, page_size))
        return [duplicate_history, new_history]

    def fake_get_order(order_number):
        detail_calls.append(order_number)
        if order_number == "active-detail-1":
            return active_detail
        return None

    monkeypatch.setattr(
        "services.stockx_order_sheet_sync.list_active_orders",
        fake_list_active_orders,
    )
    monkeypatch.setattr(
        "services.stockx_order_sheet_sync.list_historical_orders",
        fake_list_historical_orders,
    )
    monkeypatch.setattr("services.stockx_order_sheet_sync.get_order", fake_get_order)
    monkeypatch.setattr(
        "services.stockx_order_sheet_sync.history_lookback_dates",
        lambda days: ("2026-06-25", "2026-07-09"),
    )

    orders = list(iter_payout_ready_orders(history_lookback_days=14))

    assert [call[0] for call in active_calls] == list(ACTIVE_PAYOUT_READY_STATUSES)
    assert historical_calls == [(HISTORICAL_COMPLETED_STATUS, "2026-06-25", "2026-07-09", 100)]
    assert detail_calls == ["active-detail-1"]
    assert [order["orderNumber"] for order in orders] == [
        "pending-1",
        "completed-1",
        "active-detail-1",
        "history-1",
    ]


def test_history_lookback_dates_returns_bounded_window():
    from_date, to_date = history_lookback_dates(14)

    assert len(from_date) == 10
    assert len(to_date) == 10
    assert from_date <= to_date


def test_sync_updates_matching_row_and_persists_processed_order_state(tmp_path):
    rows = [
        ["Location", "DOP", "Date of Sale", "Status", "Platform", "Type", "Style Code", "Name", "Size", "Original Price", "Price Paid", "Total", "Payout", "Profit"],
        ["A", "2026-01-01", "", "", "", "Footwear", "QX1001-200", "Shoe", "10", "120", "55", "", "", "21.81"],
    ]
    sheet = FakeSheet(rows)
    state_path = tmp_path / "stockx_state.json"
    orders = [
        {
            "orderNumber": "100000001-100000002",
            "createdAt": "2026-07-01T13:51:47.000Z",
            "inventoryType": "STANDARD",
            "product": {"styleId": "QX1001-200", "productName": "Aether Trail Test"},
            "variant": {"variantValue": "10"},
            "payout": {"totalPayout": "76.81"},
        }
    ]

    result = sync_stockx_payout_ready_orders(
        sheet=sheet,
        orders=orders,
        state_path=state_path,
    )

    assert len(result.updated) == 1
    assert result.updated[0].row_number == 2
    assert result.updated[0].status == SOLD_STATUS
    assert result.updated[0].profit == "21.81"
    assert result.updated[0].product_name == "Aether Trail Test"
    assert result.updated[0].stockx_url == "https://stockx.com/search?s=QX1001-200"
    assert sheet.batch_updates == [
        [
            {"range": "D2", "values": [["Sold"]]},
            {"range": "E2", "values": [["StockX"]]},
            {"range": "M2", "values": [[76.81]]},
            {"range": "C2", "values": [["2026-07-01"]]},
        ]
    ]
    assert load_processed_order_numbers(state_path) == {"100000001-100000002"}

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["processed_order_numbers"] == ["100000001-100000002"]


def test_sync_includes_inventory_id_and_updates_sqlite_status(tmp_path):
    rows = [
        ["Location", "DOP", "Date of Sale", "Status", "Platform", "Type", "Style Code", "Name", "Size", "Original Price", "Price Paid", "Total", "Payout", "Profit", "Inventory ID"],
        ["A", "2026-01-01", "", "Pending", "", "Footwear", "QX1001-200", "Shoe", "10", "120", "55", "", "", "21.81", "INV-000001"],
    ]
    sheet = FakeSheet(rows)
    repository = InventoryRepository(tmp_path / "inventory.db")
    repository.import_unit(
        "INV-000001",
        InventoryUnitInput(
            location="A",
            purchase_date="2026-01-01",
            item_type="Footwear",
            style_code="QX1001-200",
            product_name="Shoe",
            size="10",
            cost=55,
        ),
        sheet_row=2,
    )

    result = sync_stockx_payout_ready_orders(
        sheet=sheet,
        orders=[
            {
                "orderNumber": "with-inventory-id",
                "createdAt": "2026-07-01T13:51:47.000Z",
                "inventoryType": "STANDARD",
                "product": {"styleId": "QX1001-200", "productName": "Aether Trail Test"},
                "variant": {"variantValue": "10"},
                "payout": {"totalPayout": "76.81"},
            }
        ],
        state_path=tmp_path / "stockx_state.json",
        inventory_repository=repository,
    )

    assert result.updated[0].inventory_id == "INV-000001"
    assert repository.get("INV-000001").status == SOLD_STATUS


def test_sync_persists_each_successful_order_before_later_failure(tmp_path):
    class FailingSecondBatchSheet(FakeSheet):
        def batch_update(self, updates):
            if self.batch_updates:
                raise RuntimeError("sheet update failed")
            super().batch_update(updates)

    rows = [
        ["Location", "DOP", "Date of Sale", "Status", "Platform", "Type", "Style Code", "Name", "Size", "Original Price", "Price Paid", "Total", "Payout", "Profit"],
        ["A", "2026-01-01", "", "", "", "Footwear", "QX1001-200", "Shoe", "10", "120", "55", "", "", "21.81"],
        ["A", "2026-01-02", "", "", "", "Footwear", "QX1009-800", "Shoe", "9.5", "150", "55", "", "", "47.25"],
    ]
    sheet = FailingSecondBatchSheet(rows)
    state_path = tmp_path / "stockx_state.json"
    orders = [
        {
            "orderNumber": "first-order",
            "createdAt": "2026-07-01T13:51:47.000Z",
            "inventoryType": "STANDARD",
            "product": {"styleId": "QX1001-200", "productName": "Aether Trail Test"},
            "variant": {"variantValue": "10"},
            "payout": {"totalPayout": "76.81"},
        },
        {
            "orderNumber": "second-order",
            "createdAt": "2026-07-01T13:51:47.000Z",
            "inventoryType": "STANDARD",
            "product": {"styleId": "QX1009-800", "productName": "Aether Trail Test 2"},
            "variant": {"variantValue": "9.5"},
            "payout": {"totalPayout": "102.25"},
        },
    ]

    with pytest.raises(RuntimeError):
        sync_stockx_payout_ready_orders(
            sheet=sheet,
            orders=orders,
            state_path=state_path,
        )

    assert load_processed_order_numbers(state_path) == {"first-order"}


def test_sync_skips_previously_processed_order(tmp_path):
    rows = [
        ["Location", "DOP", "Date of Sale", "Status", "Platform", "Type", "Style Code", "Name", "Size", "Payout"],
        ["A", "2026-01-01", "", "", "", "Footwear", "QX1001-200", "Shoe", "10", ""],
    ]
    sheet = FakeSheet(rows)
    state_path = tmp_path / "stockx_state.json"
    save_processed_order_numbers({"100000001-100000002"}, state_path)

    result = sync_stockx_payout_ready_orders(
        sheet=sheet,
        orders=[
            {
                "orderNumber": "100000001-100000002",
                "createdAt": "2026-07-01T13:51:47.000Z",
                "inventoryType": "STANDARD",
                "product": {"styleId": "QX1001-200"},
                "variant": {"variantValue": "10"},
                "payout": {"totalPayout": "76.81"},
            }
        ],
        state_path=state_path,
    )

    assert result.updated == []
    assert result.already_processed == ["100000001-100000002"]
    assert sheet.updates == []


def test_sync_repairs_blank_payout_for_previously_processed_order(tmp_path):
    rows = [
        ["Location", "DOP", "Date of Sale", "Status", "Platform", "Type", "Style Code", "Name", "Size", "Original Price", "Price Paid", "Total", "Payout", "Profit"],
        ["A", "2026-01-01", "07/01/2026", "Sold", "StockX", "Footwear", "QX1001-200", "Shoe", "10", "76.81", "55", "", "", "21.81"],
    ]
    sheet = FakeSheet(rows)
    state_path = tmp_path / "stockx_state.json"
    save_processed_order_numbers({"100000001-100000002"}, state_path)

    result = sync_stockx_payout_ready_orders(
        sheet=sheet,
        orders=[
            {
                "orderNumber": "100000001-100000002",
                "createdAt": "2026-07-01T13:51:47.000Z",
                "inventoryType": "STANDARD",
                "product": {"styleId": "QX1001-200", "productName": "Aether Trail Test"},
                "variant": {"variantValue": "10"},
                "payout": {"totalPayout": "76.81"},
            }
        ],
        state_path=state_path,
    )

    assert len(result.updated) == 1
    assert result.already_processed == []
    assert sheet.rows[1][12] == 76.81
    assert sheet.rows[1][9] == "76.81"
    assert sheet.batch_updates[0][2] == {"range": "M2", "values": [[76.81]]}
