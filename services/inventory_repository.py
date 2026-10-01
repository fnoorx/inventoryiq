"""Database persistence for permanent physical inventory identities."""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from pathlib import Path
import re

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from services.database import (
    advance_postgresql_sequence,
    create_repository_engine,
    insert_ignoring_conflicts,
)
from services.database_config import resolve_database_path
from services.database_tables import inventory_id_sequence, inventory_units
from services.pricing import calculate_total_cost


INVENTORY_ID_PATTERN = re.compile(r"^INV-(\d{6}|[1-9]\d{6,})$")

SYNC_PENDING = "pending"
SYNCED = "synced"
SYNC_FAILED = "retry_pending"


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


def is_valid_inventory_id(value: object) -> bool:
    match = INVENTORY_ID_PATTERN.fullmatch(str(value or "").strip().upper())
    return bool(match and int(match.group(1)) > 0)


class InventoryRepository:
    """Permanent inventory identities in SQLite or PostgreSQL.

    IDs come from an append-only sequence table, so a number is never reused
    even if a unit is later removed. Creation is keyed by the Discord message
    that requested it: replaying the same message returns the existing unit
    instead of allocating another ID, even when two requests race.
    """

    def __init__(self, database_path: str | Path | None = None):
        self.database_path = resolve_database_path(database_path)
        self.engine = create_repository_engine(database_path)

    def create_unit(
        self,
        values: InventoryUnitInput,
        discord_message_id: str | int | None = None,
        sheet_row: int | None = None,
    ) -> tuple[InventoryUnit, bool]:
        """Allocate the next permanent ID, or return the unit already created for this message."""

        message_id = _optional_text(discord_message_id)
        now = utc_now()

        with self.engine.begin() as connection:
            if message_id:
                existing = _select_unit(connection, inventory_units.c.discord_message_id, message_id)
                if existing:
                    return existing, False

            try:
                # A savepoint lets a concurrent duplicate undo its ID allocation.
                with connection.begin_nested():
                    inventory_id = format_inventory_id(
                        connection.execute(
                            insert(inventory_id_sequence)
                            .values(allocated_at=now)
                            .returning(inventory_id_sequence.c.sequence_number)
                        ).scalar_one()
                    )
                    connection.execute(
                        insert(inventory_units).values(
                            **_unit_values(values, sheet_row, now),
                            inventory_id=inventory_id,
                            sheet_sync_status=SYNC_PENDING,
                            discord_message_id=message_id,
                        )
                    )
            except IntegrityError:
                existing = message_id and _select_unit(
                    connection, inventory_units.c.discord_message_id, message_id
                )
                if not existing:
                    raise
                return existing, False

            return _select_unit(connection, inventory_units.c.inventory_id, inventory_id), True

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
        with self.engine.begin() as connection:
            existing = _select_unit(connection, inventory_units.c.inventory_id, normalized_id)
            if existing:
                return existing, False

            allocation = connection.execute(
                insert_ignoring_conflicts(connection, inventory_id_sequence).values(
                    sequence_number=sequence_number, allocated_at=now
                )
            ).first()
            if allocation is None:
                # A concurrent import may have just claimed this ID for the same unit.
                existing = _select_unit(connection, inventory_units.c.inventory_id, normalized_id)
                if existing:
                    return existing, False
                raise ValueError(f"Inventory ID was already allocated: {normalized_id}")

            connection.execute(
                insert(inventory_units).values(
                    **_unit_values(values, sheet_row, now),
                    inventory_id=normalized_id,
                    sheet_sync_status=SYNCED,
                    sheet_synced_at=now,
                )
            )
            advance_postgresql_sequence(connection, sequence_number)
            return _select_unit(connection, inventory_units.c.inventory_id, normalized_id), True

    def get(self, inventory_id: str) -> InventoryUnit | None:
        with self.engine.connect() as connection:
            return _select_unit(
                connection, inventory_units.c.inventory_id, str(inventory_id).strip().upper()
            )

    def get_by_sheet_row(self, sheet_row: int) -> InventoryUnit | None:
        statement = (
            select(inventory_units)
            .where(inventory_units.c.sheet_row == sheet_row)
            .order_by(inventory_units.c.created_at.desc(), inventory_units.c.inventory_id.desc())
            .limit(1)
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().first()
        return _unit_from_row(row) if row else None

    def list_retry_pending(self) -> list[InventoryUnit]:
        statement = (
            select(inventory_units)
            .where(inventory_units.c.sheet_sync_status != SYNCED)
            .order_by(inventory_units.c.inventory_id)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_unit_from_row(row) for row in rows]

    def mark_sheet_synced(self, inventory_id: str, sheet_row: int) -> InventoryUnit:
        now = utc_now()
        self._update(
            inventory_id,
            sheet_row=sheet_row,
            sheet_sync_status=SYNCED,
            sheet_sync_error=None,
            sheet_synced_at=now,
            updated_at=now,
        )
        return self._get_required(inventory_id)

    def update_costs_from_sheet(
        self,
        inventory_id: str,
        price_paid: float,
        total_cost: float,
        sheet_row: int,
    ) -> InventoryUnit:
        self._update(
            inventory_id,
            price_paid=float(price_paid),
            total_cost=float(total_cost),
            sheet_row=sheet_row,
            updated_at=utc_now(),
        )
        return self._get_required(inventory_id)

    def mark_sheet_sync_failed(self, inventory_id: str, error: object) -> InventoryUnit:
        self._update(
            inventory_id,
            sheet_sync_status=SYNC_FAILED,
            sheet_sync_error=str(error)[:2000],
            updated_at=utc_now(),
        )
        return self._get_required(inventory_id)

    def update_status_and_sheet_row(
        self,
        inventory_id: str,
        status: str,
        sheet_row: int,
    ) -> InventoryUnit | None:
        normalized_id = str(inventory_id).strip().upper()
        with self.engine.begin() as connection:
            connection.execute(
                update(inventory_units)
                .where(inventory_units.c.inventory_id == normalized_id)
                .values(status=status, sheet_row=sheet_row, updated_at=utc_now())
            )
        return self.get(normalized_id)

    def _update(self, inventory_id: str, **values: object) -> None:
        normalized_id = str(inventory_id).strip().upper()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(inventory_units)
                .where(inventory_units.c.inventory_id == normalized_id)
                .values(**values)
            )
            if result.rowcount != 1:
                raise KeyError(f"Inventory item was not found: {normalized_id}")

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


def _select_unit(connection, column, value) -> InventoryUnit | None:
    row = connection.execute(select(inventory_units).where(column == value)).mappings().first()
    return _unit_from_row(row) if row else None


def _unit_values(values: InventoryUnitInput, sheet_row: int | None, now: str) -> dict:
    return {
        "location": values.location,
        "purchase_date": values.purchase_date,
        "item_type": values.item_type,
        "style_code": values.style_code,
        "product_name": values.product_name,
        "size": values.size,
        "price_paid": float(values.price_paid),
        "total_cost": float(values.total_cost),
        "status": values.status,
        "source_identifier": values.source_identifier,
        "stockx_product_id": values.stockx_product_id,
        "stockx_variant_id": values.stockx_variant_id,
        "sheet_row": sheet_row,
        "created_at": now,
        "updated_at": now,
    }


def _unit_from_row(row) -> InventoryUnit:
    return InventoryUnit(**dict(row))
