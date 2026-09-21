"""Transactional SQLite persistence for temporary shoe-label scan drafts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from services.database import connect, transaction
from services.database_schema import ensure_database_schema
from services.inventory_repository import resolve_database_path, utc_now


SCAN_RECEIVED = "received"
SCAN_VERIFIED = "verified"
SCAN_REVIEW_REQUIRED = "review_required"
SCAN_CONFLICT = "conflict"
SCAN_CONFIRMED = "confirmed"
SCAN_CANCELLED = "cancelled"
SCAN_FAILED = "failed"

CONFIRMABLE_SCAN_STATES = {SCAN_VERIFIED, SCAN_REVIEW_REQUIRED}


@dataclass(frozen=True)
class LabelScan:
    scan_id: str
    discord_message_id: str
    discord_attachment_id: str
    image_sha256: str
    state: str
    barcode_results: list[dict]
    raw_vision: dict | None
    normalized_extraction: dict | None
    market_data: dict | None
    stockx_product_id: str
    stockx_variant_id: str
    validation_status: str
    warnings: list[str]
    error_details: str | None
    supplied_price: float | None
    location: str
    purchase_date: str
    linked_inventory_id: str | None
    created_at: str
    updated_at: str
    confirmed_at: str | None


class LabelScanRepository:
    """Use a short-lived connection and explicit transaction per scan operation."""

    def __init__(self, database_path: str | Path | None = None):
        self.database_path = resolve_database_path(database_path)
        ensure_database_schema(self.database_path)

    def create_or_get(
        self,
        *,
        discord_message_id: str | int,
        discord_attachment_id: str | int,
        image_sha256: str,
        supplied_price: float | None,
        location: str,
        purchase_date: str,
    ) -> tuple[LabelScan, bool]:
        message_id = _required_text(discord_message_id, "Discord message ID")
        attachment_id = _required_text(discord_attachment_id, "Discord attachment ID")
        image_hash = _required_text(image_sha256, "Image SHA-256").lower()
        now = utc_now()

        with transaction(self.database_path) as connection:
            existing = connection.execute(
                """
                SELECT * FROM label_scans
                WHERE discord_message_id = ?
                   OR (discord_message_id = ? AND discord_attachment_id = ?)
                   OR image_sha256 = ?
                LIMIT 1
                """,
                (message_id, message_id, attachment_id, image_hash),
            ).fetchone()
            if existing:
                # Re-sending the same photo with a price updates the open draft.
                if (
                    supplied_price is not None
                    and existing["linked_inventory_id"] is None
                    and existing["state"] not in {SCAN_CANCELLED, SCAN_CONFIRMED}
                ):
                    connection.execute(
                        """
                        UPDATE label_scans
                        SET supplied_price = ?, location = ?, purchase_date = ?, updated_at = ?
                        WHERE scan_id = ?
                        """,
                        (float(supplied_price), location, purchase_date, now, existing["scan_id"]),
                    )
                    existing = _select_scan(connection, existing["scan_id"])
                return _scan_from_row(existing), False

            scan_id = f"SCAN-{uuid4().hex[:16].upper()}"
            connection.execute(
                """
                INSERT INTO label_scans (
                    scan_id, discord_message_id, discord_attachment_id,
                    image_sha256, state, validation_status, supplied_price,
                    location, purchase_date, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id, message_id, attachment_id, image_hash, SCAN_RECEIVED, SCAN_RECEIVED,
                    supplied_price, location, purchase_date, now, now,
                ),
            )
            return _scan_from_row(_select_scan(connection, scan_id)), True

    def get(self, scan_id: str) -> LabelScan | None:
        with connect(self.database_path) as connection:
            row = _select_scan(connection, str(scan_id).strip().upper())
        return _scan_from_row(row) if row else None

    def save_analysis(
        self,
        scan_id: str,
        *,
        state: str,
        barcode_results: list[dict],
        raw_vision: dict | None,
        normalized_extraction: dict | None,
        market_data: dict | None,
        stockx_product_id: str = "",
        stockx_variant_id: str = "",
        validation_status: str,
        warnings: list[str],
        error_details: str | None = None,
    ) -> LabelScan:
        now = utc_now()
        self._update(
            """
            UPDATE label_scans
            SET state = ?, barcode_results_json = ?, raw_vision_json = ?,
                normalized_extraction_json = ?, market_data_json = ?,
                stockx_product_id = ?, stockx_variant_id = ?,
                validation_status = ?, warning_details_json = ?,
                error_details = ?, updated_at = ?
            WHERE scan_id = ?
            """,
            (
                state,
                _json_dump(barcode_results),
                _json_dump(raw_vision),
                _json_dump(normalized_extraction),
                _json_dump(market_data),
                stockx_product_id,
                stockx_variant_id,
                validation_status,
                _json_dump(warnings),
                error_details,
                now,
                scan_id,
            ),
        )
        return self._get_required(scan_id)

    def update_draft_fields(
        self,
        scan_id: str,
        *,
        supplied_price: float | None,
        location: str,
        purchase_date: str,
    ) -> LabelScan:
        self._update(
            """
            UPDATE label_scans
            SET supplied_price = ?, location = ?, purchase_date = ?, updated_at = ?
            WHERE scan_id = ? AND linked_inventory_id IS NULL
            """,
            (supplied_price, location, purchase_date, utc_now(), scan_id),
        )
        return self._get_required(scan_id)

    def mark_confirmed(self, scan_id: str, inventory_id: str) -> LabelScan:
        now = utc_now()
        with transaction(self.database_path) as connection:
            row = _select_scan(connection, scan_id)
            if row is None:
                raise KeyError(f"Label scan was not found: {scan_id}")
            linked = row["linked_inventory_id"]
            if linked and linked != inventory_id:
                raise ValueError(f"Scan {scan_id} is already linked to inventory item {linked}")
            connection.execute(
                """
                UPDATE label_scans
                SET state = ?, validation_status = ?, linked_inventory_id = ?,
                    confirmed_at = COALESCE(confirmed_at, ?), updated_at = ?
                WHERE scan_id = ?
                """,
                (SCAN_CONFIRMED, SCAN_CONFIRMED, inventory_id, now, now, scan_id),
            )
            return _scan_from_row(_select_scan(connection, scan_id))

    def mark_cancelled(self, scan_id: str) -> LabelScan:
        scan = self._get_required(scan_id)
        if scan.linked_inventory_id:
            raise ValueError("A confirmed label scan cannot be cancelled.")
        self._update(
            """
            UPDATE label_scans
            SET state = ?, validation_status = ?, updated_at = ?
            WHERE scan_id = ?
            """,
            (SCAN_CANCELLED, SCAN_CANCELLED, utc_now(), scan_id),
        )
        return self._get_required(scan_id)

    def mark_failed(self, scan_id: str, error: object) -> LabelScan:
        self._update(
            """
            UPDATE label_scans
            SET state = ?, validation_status = ?, error_details = ?, updated_at = ?
            WHERE scan_id = ?
            """,
            (SCAN_FAILED, SCAN_FAILED, str(error)[:4000], utc_now(), scan_id),
        )
        return self._get_required(scan_id)

    def _update(self, statement: str, parameters: tuple) -> None:
        with connect(self.database_path) as connection, connection:
            cursor = connection.execute(statement, parameters)
            if cursor.rowcount != 1:
                raise KeyError(f"Label scan was not found or cannot be changed: {parameters[-1]}")

    def _get_required(self, scan_id: str) -> LabelScan:
        scan = self.get(scan_id)
        if scan is None:
            raise KeyError(f"Label scan was not found: {scan_id}")
        return scan


def _select_scan(connection: sqlite3.Connection, scan_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM label_scans WHERE scan_id = ?", (scan_id,)).fetchone()


def _scan_from_row(row: sqlite3.Row) -> LabelScan:
    values = dict(row)
    return LabelScan(
        scan_id=values["scan_id"],
        discord_message_id=values["discord_message_id"],
        discord_attachment_id=values["discord_attachment_id"],
        image_sha256=values["image_sha256"],
        state=values["state"],
        barcode_results=_json_load(values.get("barcode_results_json"), []),
        raw_vision=_json_load(values.get("raw_vision_json"), None),
        normalized_extraction=_json_load(
            values.get("normalized_extraction_json"), None
        ),
        market_data=_json_load(values.get("market_data_json"), None),
        stockx_product_id=values.get("stockx_product_id") or "",
        stockx_variant_id=values.get("stockx_variant_id") or "",
        validation_status=values.get("validation_status") or values["state"],
        warnings=_json_load(values.get("warning_details_json"), []),
        error_details=values.get("error_details"),
        supplied_price=values.get("supplied_price"),
        location=values["location"],
        purchase_date=values["purchase_date"],
        linked_inventory_id=values.get("linked_inventory_id"),
        created_at=values["created_at"],
        updated_at=values["updated_at"],
        confirmed_at=values.get("confirmed_at"),
    )


def _required_text(value: object, label: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _json_dump(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _json_load(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
