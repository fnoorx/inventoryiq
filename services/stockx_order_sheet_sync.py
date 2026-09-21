"""Sync payout-ready StockX orders into the inventory Google Sheet."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from typing import Iterable
from urllib.parse import quote_plus

from services.inventory_repository import InventoryRepository, is_valid_inventory_id
from services.sheets import a1_cell, get_inventory_sheet, inventory_id_column_from_headers, sheet_value
from services.sizes import normalize_size
from services.stockx import BASE_URL, get_json
from utils.performance import record_timing, timed
from utils.text import clean_text, is_blank, to_float

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = ROOT_DIR / "data" / "stockx_order_sync_state.json"
STATE_PATH_ENV_VAR = "STOCKX_SYNC_STATE_PATH"
DEFAULT_PAGE_SIZE = 100
ACTIVE_PAYOUT_READY_STATUSES = ("PAYOUTPENDING", "PAYOUTCOMPLETED")
HISTORICAL_COMPLETED_STATUS = "COMPLETED"
DEFAULT_HISTORY_LOOKBACK_DAYS = 14
SOLD_STATUS = "Sold"


@dataclass
class SheetColumns:
    date_of_sale: int
    status: int
    platform: int
    style_code: int
    size: int
    payout: int
    profit: int
    data_start_row: int = 1


SHEET_COLUMNS = SheetColumns(
    date_of_sale=3,
    status=4,
    platform=5,
    style_code=7,
    size=9,
    payout=13,
    profit=14,
)


@dataclass
class StockxOrderSheetUpdate:
    order_number: str
    row_number: int
    style_code: str
    size: str
    sale_date: str
    payout: float | str
    platform: str
    profit: float | str = ""
    product_name: str = ""
    stockx_url: str = ""
    status: str = SOLD_STATUS
    inventory_id: str = ""


@dataclass
class StockxOrderSyncResult:
    updated: list[StockxOrderSheetUpdate] = field(default_factory=list)
    already_processed: list[str] = field(default_factory=list)
    missing_data: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "updated": [update.__dict__ for update in self.updated],
            "already_processed": self.already_processed,
            "missing_data": self.missing_data,
            "unmatched": self.unmatched,
        }


@timed("stockx_order_sync")
def sync_stockx_payout_ready_orders(
    sheet=None,
    orders: Iterable[dict] | None = None,
    state_path: str | Path | None = None,
    worksheet_name: str | None = None,
    history_lookback_days: int = DEFAULT_HISTORY_LOOKBACK_DAYS,
    inventory_repository: InventoryRepository | None = None,
    database_path: str | Path | None = None,
) -> StockxOrderSyncResult:
    """Match payout-ready StockX orders to unsold sheet rows and update them.

    Orders are matched by product style code and variant size. The StockX order
    number is stored only in a local state file so repeated polling is safe
    without adding another column to the Google Sheet.
    """

    if sheet is None:
        sheet = get_inventory_sheet(worksheet_name) if worksheet_name else get_inventory_sheet()
    if orders is None:
        orders = iter_payout_ready_orders(history_lookback_days=history_lookback_days)

    sheet_read_started = time.perf_counter()
    rows = sheet.get_all_values()
    record_timing("sheets_get_all_values", sheet_read_started, rows=len(rows))
    columns = SHEET_COLUMNS
    inventory_id_column = inventory_id_column_from_headers(rows[0]) if rows else None
    state_path = resolve_state_path(state_path)
    processed_order_numbers = load_processed_order_numbers(state_path)
    result = StockxOrderSyncResult()

    for order in orders:
        payload = order_sheet_payload(order)
        order_number = payload.get("order_number")

        if not order_number:
            result.missing_data.append({"reason": "missing orderNumber", "order": order})
            continue
        missing_fields = [
            field_name
            for field_name in ("style_code", "size", "sale_date", "payout")
            if is_blank(payload.get(field_name))
        ]
        if missing_fields:
            result.missing_data.append(
                {
                    "order_number": order_number,
                    "reason": f"missing {', '.join(missing_fields)}",
                }
            )
            continue

        already_processed = order_number in processed_order_numbers
        if already_processed:
            row_number = find_processed_row_needing_repair(
                rows,
                style_code=payload["style_code"],
                size=payload["size"],
                sale_date=payload["sale_date"],
                platform=payload["platform"],
                columns=columns,
            )
            if row_number is None:
                result.already_processed.append(order_number)
                continue
        else:
            row_number = find_first_unsold_matching_row(
                rows,
                style_code=payload["style_code"],
                size=payload["size"],
                columns=columns,
            )

        if row_number is None:
            result.unmatched.append(
                {
                    "order_number": order_number,
                    "style_code": payload["style_code"],
                    "size": payload["size"],
                }
            )
            continue

        update_sale_fields(
            sheet,
            row_number=row_number,
            columns=columns,
            sale_date=payload["sale_date"],
            payout=payload["payout"],
            platform=payload["platform"],
        )

        rows = set_row_values(
            rows,
            row_number=row_number,
            updates={
                columns.date_of_sale: payload["sale_date"],
                columns.status: SOLD_STATUS,
                columns.payout: payload["payout"],
                columns.platform: payload["platform"],
            },
        )
        profit = read_sheet_value(sheet, rows, row_number, columns.profit)
        inventory_id = (
            clean_text(sheet_value(rows[row_number - 1], inventory_id_column)).upper()
            if inventory_id_column
            else ""
        )

        if is_valid_inventory_id(inventory_id):
            repository = inventory_repository or InventoryRepository(database_path)
            repository.update_status_and_sheet_row(inventory_id, SOLD_STATUS, row_number)

        if not already_processed:
            processed_order_numbers.add(order_number)
            save_processed_order_numbers(processed_order_numbers, state_path)
        result.updated.append(
            StockxOrderSheetUpdate(
                order_number=order_number,
                row_number=row_number,
                style_code=payload["style_code"],
                size=payload["size"],
                sale_date=payload["sale_date"],
                payout=payload["payout"],
                platform=payload["platform"],
                profit=profit,
                product_name=payload["product_name"],
                stockx_url=payload["stockx_url"],
                status=SOLD_STATUS,
                inventory_id=inventory_id,
            )
        )

    return result


def iter_payout_ready_orders(
    history_lookback_days: int = DEFAULT_HISTORY_LOOKBACK_DAYS,
) -> Iterable[dict]:
    """Yield payout-ready orders from active status and recent history.

    StockX does not document a completion-time sort. This bounded reconciliation
    catches orders that pass through PAYOUTPENDING between sync runs while
    avoiding an unbounded historical scan.
    """

    candidates = {}

    for status in ACTIVE_PAYOUT_READY_STATUSES:
        for order in list_active_orders(order_status=status):
            add_order_candidate(candidates, order)

    from_date, to_date = history_lookback_dates(history_lookback_days)
    for order in list_historical_orders(
        order_status=HISTORICAL_COMPLETED_STATUS,
        from_date=from_date,
        to_date=to_date,
    ):
        add_order_candidate(candidates, order)

    for order in candidates.values():
        yield order_with_required_details(order)


def add_order_candidate(candidates: dict[str, dict], order: dict) -> None:
    order_number = clean_text(order.get("orderNumber"))
    if not order_number:
        return

    existing = candidates.get(order_number)
    if existing is None or required_order_field_score(order) > required_order_field_score(existing):
        candidates[order_number] = order


def required_order_field_score(order: dict) -> int:
    product = order.get("product") or {}
    variant = order.get("variant") or {}
    payout = order.get("payout") or {}
    values = (
        order.get("createdAt"),
        product.get("styleId"),
        variant.get("variantValue"),
        payout.get("totalPayout"),
    )
    return sum(not is_blank(value) for value in values)


def order_with_required_details(order: dict) -> dict:
    if not order_needs_detail(order):
        return order

    order_number = clean_text(order.get("orderNumber"))
    if not order_number:
        return order

    return get_order(order_number) or order


def order_needs_detail(order: dict) -> bool:
    product = order.get("product") or {}
    variant = order.get("variant") or {}
    payout = order.get("payout") or {}

    return (
        is_blank(payout.get("totalPayout"))
        or is_blank(product.get("styleId"))
        or is_blank(variant.get("variantValue"))
    )


def list_active_orders(order_status: str, page_size: int = DEFAULT_PAGE_SIZE) -> list[dict]:
    return list_orders("active", {"orderStatus": order_status, "pageSize": page_size})


def list_historical_orders(
    order_status: str = HISTORICAL_COMPLETED_STATUS,
    from_date: str | None = None,
    to_date: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[dict]:
    params = {"orderStatus": order_status, "pageSize": page_size}
    if from_date:
        params["fromDate"] = from_date
    if to_date:
        params["toDate"] = to_date
    return list_orders("history", params)


def list_orders(endpoint: str, params: dict) -> list[dict]:
    """Walk every page of a selling-orders endpoint; a failed page ends the walk."""

    orders = []
    page_number = 1
    while True:
        data = get_json(
            f"{BASE_URL}/selling/orders/{endpoint}",
            f"Error fetching StockX {endpoint} orders",
            params={**params, "pageNumber": page_number},
        )
        if data is None:
            return orders
        orders.extend(data.get("orders", []))
        if not data.get("hasNextPage"):
            return orders
        page_number += 1


def get_order(order_number: str) -> dict | None:
    return get_json(
        f"{BASE_URL}/selling/orders/{order_number}",
        f"Error fetching StockX order {order_number}",
    )


def order_sheet_payload(order: dict) -> dict:
    product = order.get("product") or {}
    variant = order.get("variant") or {}
    payout = order.get("payout") or {}

    return {
        "order_number": clean_text(order.get("orderNumber")),
        "style_code": normalize_order_style_code(product.get("styleId")),
        "size": normalize_size(variant.get("variantValue")),
        "sale_date": format_stockx_sale_date(order.get("createdAt")),
        "payout": format_payout(payout.get("totalPayout")),
        "platform": stockx_platform(order.get("inventoryType"), order.get("orderNumber")),
        "product_name": clean_text(product.get("productName")),
        "stockx_url": stockx_search_url(product.get("styleId")),
    }


def find_first_unsold_matching_row(
    rows: list[list],
    style_code: str,
    size: str,
    columns: SheetColumns,
) -> int | None:
    target_size = normalize_size(size)

    for row_number, row in enumerate(rows, start=1):
        if row_number < columns.data_start_row:
            continue
        if not sheet_style_matches(sheet_value(row, columns.style_code), style_code):
            continue
        if normalize_size(sheet_value(row, columns.size)) != target_size:
            continue
        if clean_text(sheet_value(row, columns.date_of_sale)):
            continue
        return row_number

    return None


def find_processed_row_needing_repair(
    rows: list[list],
    style_code: str,
    size: str,
    sale_date: str,
    platform: str,
    columns: SheetColumns,
) -> int | None:
    """Find an already-synced row whose payout/status/platform cells were left incomplete."""

    target_size = normalize_size(size)
    target_sale_date = normalize_sheet_date(sale_date)

    for row_number, row in enumerate(rows, start=1):
        if row_number < columns.data_start_row:
            continue
        if not sheet_style_matches(sheet_value(row, columns.style_code), style_code):
            continue
        if normalize_size(sheet_value(row, columns.size)) != target_size:
            continue
        if normalize_sheet_date(sheet_value(row, columns.date_of_sale)) != target_sale_date:
            continue

        needs_repair = (
            is_blank(sheet_value(row, columns.payout))
            or clean_text(sheet_value(row, columns.status)).lower() != SOLD_STATUS.lower()
            or clean_text(sheet_value(row, columns.platform)) != platform
        )
        return row_number if needs_repair else None

    return None


def update_sale_fields(
    sheet,
    row_number: int,
    columns: SheetColumns,
    sale_date: str,
    payout: float | str,
    platform: str,
) -> None:
    started_at = time.perf_counter()
    sheet.batch_update(
        [
            {"range": a1_cell(row_number, columns.status), "values": [[SOLD_STATUS]]},
            {"range": a1_cell(row_number, columns.platform), "values": [[platform]]},
            {"range": a1_cell(row_number, columns.payout), "values": [[payout]]},
            {"range": a1_cell(row_number, columns.date_of_sale), "values": [[sale_date]]},
        ]
    )
    record_timing("sheets_update_sale_fields", started_at, row=row_number)


def set_row_values(rows: list[list], row_number: int, updates: dict[int, object]) -> list[list]:
    """Update the working Sheet snapshot in place for subsequent order matches."""

    while len(rows) < row_number:
        rows.append([])

    row = rows[row_number - 1]
    max_column = max(updates)
    while len(row) < max_column:
        row.append("")

    for column_number, value in updates.items():
        row[column_number - 1] = value

    return rows


def read_sheet_value(sheet, rows: list[list], row_number: int, column_number: int) -> str:
    # Profit is a sheet formula, so re-read the cell after writing the payout;
    # the snapshot taken before the write would be stale.
    started_at = time.perf_counter()
    try:
        cell = sheet.cell(row_number, column_number)
        return clean_text(getattr(cell, "value", cell))
    except Exception:
        if row_number > len(rows):
            return ""
        return clean_text(sheet_value(rows[row_number - 1], column_number))
    finally:
        record_timing(
            "sheets_read_formula_cell",
            started_at,
            row=row_number,
            column=column_number,
        )


def stockx_platform(inventory_type: str | None, order_number: str | None = None) -> str:
    normalized_type = clean_text(inventory_type).upper()
    if normalized_type == "FLEX" or clean_text(order_number).upper().startswith("02-"):
        return "StockX Flex"
    if normalized_type == "DIRECT":
        return "StockX Direct"
    return "StockX"


def format_stockx_sale_date(value: str | None) -> str:
    text = clean_text(value)
    if not text:
        return ""

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text

    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.date().isoformat()


def normalize_sheet_date(value) -> str:
    text = clean_text(value)
    for date_format in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            continue
    return text


def history_lookback_dates(days: int) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=max(days, 0))
    return start.isoformat(), today.isoformat()


def format_payout(value):
    number = to_float(value)
    if number is None:
        return clean_text(value)
    return number


def stockx_search_url(style_code: str | None) -> str:
    normalized_style = normalize_order_style_code(style_code)
    if not normalized_style:
        return ""
    return f"https://stockx.com/search?s={quote_plus(normalized_style)}"


def resolve_state_path(state_path: str | Path | None = None) -> Path:
    return Path(state_path or os.getenv(STATE_PATH_ENV_VAR) or DEFAULT_STATE_PATH)


def load_processed_order_numbers(state_path: str | Path = DEFAULT_STATE_PATH) -> set[str]:
    path = Path(state_path)
    if not path.exists():
        return set()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()

    order_numbers = data.get("processed_order_numbers", [])
    return {clean_text(order_number) for order_number in order_numbers if clean_text(order_number)}


def save_processed_order_numbers(
    processed_order_numbers: set[str],
    state_path: str | Path = DEFAULT_STATE_PATH,
) -> None:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "processed_order_numbers": sorted(processed_order_numbers),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def normalize_order_style_code(value) -> str:
    return clean_text(value).upper()


def split_sheet_style_codes(value) -> tuple[str, ...]:
    """Return the individual normalized codes stored in a slash-delimited cell."""

    return tuple(
        normalized
        for part in clean_text(value).split("/")
        if (normalized := normalize_order_style_code(part))
    )


def sheet_style_matches(first, second) -> bool:
    """Match exact individual codes, including either side containing alternatives."""

    first_codes = set(split_sheet_style_codes(first))
    second_codes = set(split_sheet_style_codes(second))
    return bool(first_codes and second_codes and first_codes.intersection(second_codes))
