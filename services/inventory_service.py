"""Reusable inventory creation, Sheet synchronization, retry, and backfill flows."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from services.inventory_repository import (
    InventoryRepository,
    InventoryUnit,
    InventoryUnitInput,
    SYNCED,
    is_valid_inventory_id,
)
from services.sheets import (
    append_row,
    ensure_inventory_id_column,
    find_inventory_id_row,
    get_inventory_sheet,
    sheet_value,
    write_inventory_id,
    write_inventory_ids_batch,
)


@dataclass(frozen=True)
class InventoryCreationResult:
    item: InventoryUnit
    created: bool
    sheet_synced: bool
    sheet_error: str | None = None


@dataclass(frozen=True)
class InventoryRetryResult:
    inventory_id: str
    sheet_synced: bool
    already_synced: bool = False
    sheet_row: int | None = None
    error: str | None = None


@dataclass
class InventoryBackfillResult:
    assigned: list[dict] = field(default_factory=list)
    resumed: list[dict] = field(default_factory=list)
    valid_existing: list[dict] = field(default_factory=list)
    imported_existing: list[dict] = field(default_factory=list)
    malformed_ids: list[dict] = field(default_factory=list)
    duplicate_ids: list[dict] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)


BACKFILL_WRITE_BATCH_SIZE = 200


def create_inventory_unit(
    values: InventoryUnitInput,
    discord_message_id: str | int,
    *,
    sheet=None,
    repository: InventoryRepository | None = None,
    database_path: str | Path | None = None,
) -> InventoryCreationResult:
    """Create one physical unit, then mirror it to Sheets with the same ID.

    Callers pass already-resolved product data, so `!add` and photo intake share
    one identity path regardless of how the product was identified.
    """

    repository = repository or InventoryRepository(database_path)
    item, created = repository.create_unit(values, discord_message_id=discord_message_id)

    if item.sheet_sync_status == SYNCED:
        return InventoryCreationResult(item=item, created=created, sheet_synced=True)

    retry_result = sync_inventory_unit_to_sheet(item, sheet=sheet, repository=repository)
    refreshed = repository.get(item.inventory_id) or item
    return InventoryCreationResult(
        item=refreshed,
        created=created,
        sheet_synced=retry_result.sheet_synced,
        sheet_error=retry_result.error,
    )


def sync_inventory_unit_to_sheet(
    item: InventoryUnit,
    *,
    sheet=None,
    repository: InventoryRepository,
) -> InventoryRetryResult:
    """Mirror a unit to the Sheet without ever allocating a second ID.

    A unit that already knows its row only needs its ID cell written; otherwise
    a row is appended. Failures are recorded on the unit for `!inventoryretry`.
    """

    try:
        resolved_sheet = sheet or get_inventory_sheet()
        if item.sheet_row is not None:
            inventory_id_column = ensure_inventory_id_column(resolved_sheet)
            existing_row = find_inventory_id_row(
                resolved_sheet,
                item.inventory_id,
                inventory_id_column,
            )
            sheet_row = existing_row or item.sheet_row
            if existing_row is None:
                write_inventory_id(
                    resolved_sheet,
                    item.sheet_row,
                    inventory_id_column,
                    item.inventory_id,
                )
        else:
            row = append_row(
                item.location,
                item.purchase_date,
                item.item_type,
                item.style_code,
                item.product_name,
                item.size,
                item.price_paid,
                sheet=resolved_sheet,
                inventory_id=item.inventory_id,
            )
            sheet_row = int(row["sheet_row"])
        repository.mark_sheet_synced(item.inventory_id, sheet_row)
        return InventoryRetryResult(
            inventory_id=item.inventory_id,
            sheet_synced=True,
            sheet_row=sheet_row,
        )
    except Exception as exc:
        repository.mark_sheet_sync_failed(item.inventory_id, exc)
        return InventoryRetryResult(
            inventory_id=item.inventory_id,
            sheet_synced=False,
            sheet_row=item.sheet_row,
            error=str(exc),
        )


def retry_inventory_sheet_sync(
    inventory_id: str | None = None,
    *,
    sheet=None,
    repository: InventoryRepository | None = None,
    database_path: str | Path | None = None,
) -> list[InventoryRetryResult]:
    repository = repository or InventoryRepository(database_path)

    if inventory_id:
        item = repository.get(inventory_id)
        if item is None:
            raise ValueError(f"Inventory item `{inventory_id}` was not found.")
        if item.sheet_sync_status == SYNCED:
            return [
                InventoryRetryResult(
                    inventory_id=item.inventory_id,
                    sheet_synced=True,
                    already_synced=True,
                    sheet_row=item.sheet_row,
                )
            ]
        items = [item]
    else:
        items = repository.list_retry_pending()

    if not items:
        return []

    resolved_sheet = sheet or get_inventory_sheet()
    return [
        sync_inventory_unit_to_sheet(item, sheet=resolved_sheet, repository=repository)
        for item in items
    ]


def backfill_inventory_ids(
    *,
    sheet=None,
    repository: InventoryRepository | None = None,
    database_path: str | Path | None = None,
) -> InventoryBackfillResult:
    """Backfill blank Sheet IDs while preserving and auditing existing IDs.

    A unit is committed before its Sheet cell is written. If that write fails,
    its Sheet row remains on the SQLite record with retry status; the next run
    reuses that unit instead of allocating another ID.
    """

    repository = repository or InventoryRepository(database_path)
    resolved_sheet = sheet or get_inventory_sheet()
    inventory_id_column = ensure_inventory_id_column(resolved_sheet)
    rows = resolved_sheet.get_all_values()
    result = InventoryBackfillResult()

    first_rows_by_id: dict[str, int] = {}
    row_classification: dict[int, str] = {}
    for row_number, row in inventory_sheet_data_rows(rows):
        existing_id = sheet_value(row, inventory_id_column).upper()
        if not existing_id:
            row_classification[row_number] = "blank"
        elif not is_valid_inventory_id(existing_id):
            row_classification[row_number] = "malformed"
            result.malformed_ids.append({"row": row_number, "inventory_id": existing_id})
        elif existing_id in first_rows_by_id:
            row_classification[row_number] = "duplicate"
            result.duplicate_ids.append(
                {
                    "row": row_number,
                    "first_row": first_rows_by_id[existing_id],
                    "inventory_id": existing_id,
                }
            )
        else:
            first_rows_by_id[existing_id] = row_number
            row_classification[row_number] = "valid"

    pending_writes: list[dict] = []
    for row_number, row in inventory_sheet_data_rows(rows):
        classification = row_classification[row_number]
        try:
            values = inventory_input_from_sheet_row(row)
            existing_id = sheet_value(row, inventory_id_column).upper()

            if classification == "valid":
                item, imported = repository.import_unit(existing_id, values, row_number)
                if not imported:
                    repository.update_costs_from_sheet(
                        item.inventory_id,
                        values.price_paid,
                        values.total_cost,
                        row_number,
                    )
                    repository.mark_sheet_synced(item.inventory_id, row_number)
                entry = {"row": row_number, "inventory_id": existing_id}
                result.valid_existing.append(entry)
                if imported:
                    result.imported_existing.append(entry)
                continue

            existing_item = repository.get_by_sheet_row(row_number)
            if existing_item:
                item = existing_item
                repository.update_costs_from_sheet(
                    item.inventory_id,
                    values.price_paid,
                    values.total_cost,
                    row_number,
                )
                was_resumed = True
            else:
                item, _ = repository.create_unit(values, sheet_row=row_number)
                was_resumed = False

            if classification == "malformed":
                audit_entry = next(
                    entry for entry in result.malformed_ids if entry["row"] == row_number
                )
                audit_entry["replacement_inventory_id"] = item.inventory_id
            elif classification == "duplicate":
                audit_entry = next(
                    entry for entry in result.duplicate_ids if entry["row"] == row_number
                )
                audit_entry["replacement_inventory_id"] = item.inventory_id
            pending_writes.append(
                {
                    "row": row_number,
                    "item": item,
                    "was_resumed": was_resumed,
                }
            )
        except Exception as exc:
            result.failures.append({"row": row_number, "error": str(exc)})

    for start in range(0, len(pending_writes), BACKFILL_WRITE_BATCH_SIZE):
        batch = pending_writes[start : start + BACKFILL_WRITE_BATCH_SIZE]
        try:
            write_inventory_ids_batch(
                resolved_sheet,
                inventory_id_column,
                [(entry["row"], entry["item"].inventory_id) for entry in batch],
            )
        except Exception as exc:
            for entry in batch:
                item = entry["item"]
                repository.mark_sheet_sync_failed(item.inventory_id, exc)
            result.failures.append(
                {
                    "row": batch[0]["row"],
                    "through_row": batch[-1]["row"],
                    "inventory_id": batch[0]["item"].inventory_id,
                    "error": str(exc),
                }
            )
            break

        for entry in batch:
            item = entry["item"]
            repository.mark_sheet_synced(item.inventory_id, entry["row"])
            result_entry = {"row": entry["row"], "inventory_id": item.inventory_id}
            if entry["was_resumed"]:
                result.resumed.append(result_entry)
            else:
                result.assigned.append(result_entry)

    return result


def inventory_sheet_data_rows(rows: list[list]):
    for row_number, row in enumerate(rows[1:], start=2):
        # Location in column A is the existing layout's row-presence marker.
        if sheet_value(row, 1):
            yield row_number, row


def inventory_input_from_sheet_row(row: list) -> InventoryUnitInput:
    price_paid = parse_sheet_cost(sheet_value(row, 11))
    total_text = sheet_value(row, 12)
    total_cost = (
        parse_sheet_cost(total_text)
        if total_text and not total_text.startswith("=")
        else None
    )
    return InventoryUnitInput(
        location=sheet_value(row, 1),
        purchase_date=sheet_value(row, 2),
        status=sheet_value(row, 4) or "Pending",
        item_type=sheet_value(row, 6),
        style_code=sheet_value(row, 7).upper(),
        product_name=sheet_value(row, 8),
        size=sheet_value(row, 9),
        price_paid=price_paid,
        total_cost=total_cost,
    )


def parse_sheet_cost(value: str) -> float:
    text = str(value).strip().replace("$", "").replace(",", "")
    compact = "".join(text.split())
    if compact in {"", "-", "–", "—"}:
        return 0.0
    return float(compact)


def sheet_item_type(product_type) -> str:
    """Map a StockX product type to the Sheet's item category."""

    normalized = str(product_type or "").strip().lower()
    if normalized == "sneakers":
        return "Footwear"
    if normalized == "streetwear":
        return "Apparel"
    return "Accessory"
