"""SQLite persistence for permanent physical inventory identities."""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3

from services.database import connect, transaction
from services.database_schema import ensure_database_schema
from services.pricing import calculate_total_cost


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = ROOT_DIR / "data" / "inventoryiq.db"
DATABASE_PATH_ENV_VAR = "INVENTORY_DATABASE_PATH"
INVENTORY_ID_PATTERN = re.compile(r"^INV-(\d{6}|[1-9]\d{6,})$")

SYNC_PENDING = "pending"
SYNCED = "synced"
SYNC_FAILED = "retry_pending"

INSERT_UNIT_SQL = """
    INSERT INTO inventory_units (
        inventory_id, location, purchase_date, item_type, style_code,
        product_name, size, price_paid, total_cost, status, source_identifier,
        stockx_product_id, stockx_variant_id, sheet_row,
        sheet_sync_status, discord_message_id, created_at, updated_at, sheet_synced_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True)
class InventoryUnitInput:
    location: str
    purchase_date: str
    item_type: str
    style_code: str
    product_name: str
    size: str
    price_paid: float | None = None
    total_cost: float | None = None
    status: str = "Pending"
    source_identifier: str = ""
    stockx_product_id: str = ""
    stockx_variant_id: str = ""
    cost: InitVar[float | None] = None

    def __post_init__(self, cost: float | None) -> None:
        subtotal = self.price_paid if self.price_paid is not None else cost
        if subtotal is None:
            raise ValueError("Price paid subtotal is required")
        subtotal = float(subtotal)
        total = self.total_cost
        if total is None:
            total = calculate_total_cost(subtotal)
        object.__setattr__(self, "price_paid", subtotal)
        object.__setattr__(self, "total_cost", float(total))


@dataclass(frozen=True)
class InventoryUnit:
    inventory_id: str
    location: str
    purchase_date: str
    item_type: str
    style_code: str
    product_name: str
    size: str
    price_paid: float
    total_cost: float
    status: str
    source_identifier: str
    stockx_product_id: str
    stockx_variant_id: str
    sheet_row: int | None
    sheet_sync_status: str
    sheet_sync_error: str | None
    discord_message_id: str | None
    created_at: str
    updated_at: str
    sheet_synced_at: str | None


def resolve_database_path(database_path: str | Path | None = None) -> Path:
    return Path(database_path or os.getenv(DATABASE_PATH_ENV_VAR) or DEFAULT_DATABASE_PATH)


def is_valid_inventory_id(value: object) -> bool:
    match = INVENTORY_ID_PATTERN.fullmatch(str(value or "").strip().upper())
    return bool(match and int(match.group(1)) > 0)


class InventoryRepository:
    """Permanent inventory identities backed by SQLite.

    IDs come from an append-only sequence table, so a number is never reused
    even if a unit is later removed. Creation is keyed by the Discord message
    that requested it: replaying the same message returns the existing unit
    instead of allocating another ID. Each operation opens its own short-lived
    connection; writes run inside explicit transactions.
    """

    def __init__(self, database_path: str | Path | None = None):
        self.database_path = resolve_database_path(database_path)
        ensure_database_schema(self.database_path)

    def create_unit(
        self,
        values: InventoryUnitInput,
        discord_message_id: str | int | None = None,
        sheet_row: int | None = None,
    ) -> tuple[InventoryUnit, bool]:
        """Allocate the next permanent ID, or return the unit already created for this message."""

        message_id = _optional_text(discord_message_id)
        now = utc_now()

        with transaction(self.database_path) as connection:
            if message_id:
                existing = _select_unit(connection, "discord_message_id", message_id)
                if existing:
                    return existing, False

            cursor = connection.execute(
                "INSERT INTO inventory_id_sequence(allocated_at) VALUES (?)", (now,)
            )
            inventory_id = format_inventory_id(cursor.lastrowid)
            connection.execute(
                INSERT_UNIT_SQL,
                _unit_parameters(inventory_id, values, sheet_row, SYNC_PENDING, message_id, now, None),
            )
            return _select_unit(connection, "inventory_id", inventory_id), True

    def import_unit(
        self,
        inventory_id: str,
        values: InventoryUnitInput,
        sheet_row: int,
    ) -> tuple[InventoryUnit, bool]:
        """Adopt an ID that already exists in the Sheet, reserving its sequence number."""

        normalized_id = str(inventory_id).strip().upper()
        match = INVENTORY_ID_PATTERN.fullmatch(normalized_id)
        if not match or int(match.group(1)) < 1:
            raise ValueError(f"Malformed inventory ID: {inventory_id}")

        sequence_number = int(match.group(1))
        now = utc_now()
        with transaction(self.database_path) as connection:
            existing = _select_unit(connection, "inventory_id", normalized_id)
            if existing:
                return existing, False

            already_allocated = connection.execute(
                "SELECT 1 FROM inventory_id_sequence WHERE sequence_number = ?",
                (sequence_number,),
            ).fetchone()
            if already_allocated:
                raise ValueError(f"Inventory ID was already allocated: {normalized_id}")

            connection.execute(
                "INSERT INTO inventory_id_sequence(sequence_number, allocated_at) VALUES (?, ?)",
                (sequence_number, now),
            )
            connection.execute(
                INSERT_UNIT_SQL,
                _unit_parameters(normalized_id, values, sheet_row, SYNCED, None, now, now),
            )
            return _select_unit(connection, "inventory_id", normalized_id), True

    def get(self, inventory_id: str) -> InventoryUnit | None:
        with connect(self.database_path) as connection:
            return _select_unit(connection, "inventory_id", str(inventory_id).strip().upper())

    def get_by_sheet_row(self, sheet_row: int) -> InventoryUnit | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM inventory_units
                WHERE sheet_row = ?
                ORDER BY created_at DESC, inventory_id DESC
                LIMIT 1
                """,
                (sheet_row,),
            ).fetchone()
        return _unit_from_row(row) if row else None

    def list_retry_pending(self) -> list[InventoryUnit]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM inventory_units
                WHERE sheet_sync_status != ?
                ORDER BY inventory_id
                """,
                (SYNCED,),
            ).fetchall()
        return [_unit_from_row(row) for row in rows]

    def mark_sheet_synced(self, inventory_id: str, sheet_row: int) -> InventoryUnit:
        now = utc_now()
        self._update(
            """
            UPDATE inventory_units
            SET sheet_row = ?, sheet_sync_status = ?, sheet_sync_error = NULL,
                sheet_synced_at = ?, updated_at = ?
            WHERE inventory_id = ?
            """,
            (sheet_row, SYNCED, now, now, inventory_id),
        )
        return self._get_required(inventory_id)

    def update_costs_from_sheet(
        self,
        inventory_id: str,
        price_paid: float,
        total_cost: float,
        sheet_row: int,
    ) -> InventoryUnit:
        now = utc_now()
        self._update(
            """
            UPDATE inventory_units
            SET price_paid = ?, total_cost = ?, sheet_row = ?, updated_at = ?
            WHERE inventory_id = ?
            """,
            (float(price_paid), float(total_cost), sheet_row, now, inventory_id),
        )
        return self._get_required(inventory_id)

    def mark_sheet_sync_failed(self, inventory_id: str, error: object) -> InventoryUnit:
        now = utc_now()
        self._update(
            """
            UPDATE inventory_units
            SET sheet_sync_status = ?, sheet_sync_error = ?, updated_at = ?
            WHERE inventory_id = ?
            """,
            (SYNC_FAILED, str(error)[:2000], now, inventory_id),
        )
        return self._get_required(inventory_id)

    def update_status_and_sheet_row(
        self,
        inventory_id: str,
        status: str,
        sheet_row: int,
    ) -> InventoryUnit | None:
        now = utc_now()
        with connect(self.database_path) as connection, connection:
            connection.execute(
                """
                UPDATE inventory_units
                SET status = ?, sheet_row = ?, updated_at = ?
                WHERE inventory_id = ?
                """,
                (status, sheet_row, now, str(inventory_id).strip().upper()),
            )
        return self.get(inventory_id)

    def _update(self, statement: str, parameters: tuple) -> None:
        with connect(self.database_path) as connection, connection:
            cursor = connection.execute(statement, parameters)
            if cursor.rowcount != 1:
                raise KeyError(f"Inventory item was not found: {parameters[-1]}")

    def _get_required(self, inventory_id: str) -> InventoryUnit:
        item = self.get(inventory_id)
        if item is None:
            raise KeyError(f"Inventory item was not found: {inventory_id}")
        return item


def format_inventory_id(sequence_number: int) -> str:
    return f"INV-{sequence_number:06d}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _select_unit(connection: sqlite3.Connection, column: str, value) -> InventoryUnit | None:
    row = connection.execute(
        f"SELECT * FROM inventory_units WHERE {column} = ?", (value,)
    ).fetchone()
    return _unit_from_row(row) if row else None


def _unit_parameters(inventory_id, values, sheet_row, sync_status, message_id, now, synced_at):
    return (
        inventory_id,
        values.location,
        values.purchase_date,
        values.item_type,
        values.style_code,
        values.product_name,
        values.size,
        float(values.price_paid),
        float(values.total_cost),
        values.status,
        values.source_identifier,
        values.stockx_product_id,
        values.stockx_variant_id,
        sheet_row,
        sync_status,
        message_id,
        now,
        now,
        synced_at,
    )


def _unit_from_row(row: sqlite3.Row) -> InventoryUnit:
    return InventoryUnit(**dict(row))
