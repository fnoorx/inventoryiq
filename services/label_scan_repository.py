"""Transactional persistence for temporary shoe-label scan drafts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import or_, select, update

from services.database import create_repository_engine, insert_ignoring_conflicts
from services.database_tables import label_scans
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
    """Run each scan operation in its own explicit transaction."""

    def __init__(self, database_path: str | Path | None = None):
        self.database_path = resolve_database_path(database_path)
        # Only an explicit path overrides DATABASE_URL; related repositories reuse it.
        self.explicit_database_path = database_path
        self.engine = create_repository_engine(database_path)

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

        with self.engine.begin() as connection:
            existing = _find_duplicate(connection, message_id, image_hash)
            if existing is None:
                scan_id = f"SCAN-{uuid4().hex[:16].upper()}"
                inserted = connection.execute(
                    insert_ignoring_conflicts(connection, label_scans).values(
                        scan_id=scan_id,
                        discord_message_id=message_id,
                        discord_attachment_id=attachment_id,
                        image_sha256=image_hash,
                        state=SCAN_RECEIVED,
                        validation_status=SCAN_RECEIVED,
                        supplied_price=supplied_price,
                        location=location,
                        purchase_date=purchase_date,
                        created_at=now,
                        updated_at=now,
                    )
                ).first()
                if inserted is not None:
                    return _scan_from_row(_select_scan(connection, scan_id)), True

                # A concurrent upload stored the same message or image first.
                existing = _find_duplicate(connection, message_id, image_hash)
                if existing is None:
                    raise RuntimeError("Label scan insert conflicted without a duplicate.")

            # Re-sending the same photo with a price updates the open draft.
            if (
                supplied_price is not None
                and existing["linked_inventory_id"] is None
                and existing["state"] not in {SCAN_CANCELLED, SCAN_CONFIRMED}
            ):
                connection.execute(
                    update(label_scans)
                    .where(label_scans.c.scan_id == existing["scan_id"])
                    .values(
                        supplied_price=float(supplied_price),
                        location=location,
                        purchase_date=purchase_date,
                        updated_at=now,
                    )
                )
                existing = _select_scan(connection, existing["scan_id"])
            return _scan_from_row(existing), False

    def get(self, scan_id: str) -> LabelScan | None:
        with self.engine.connect() as connection:
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
        self._update(
            scan_id,
            state=state,
            barcode_results_json=_json_dump(barcode_results),
            raw_vision_json=_json_dump(raw_vision),
            normalized_extraction_json=_json_dump(normalized_extraction),
            market_data_json=_json_dump(market_data),
            stockx_product_id=stockx_product_id,
            stockx_variant_id=stockx_variant_id,
            validation_status=validation_status,
            warning_details_json=_json_dump(warnings),
            error_details=error_details,
            updated_at=utc_now(),
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
            scan_id,
            require_unlinked=True,
            supplied_price=supplied_price,
            location=location,
            purchase_date=purchase_date,
            updated_at=utc_now(),
        )
        return self._get_required(scan_id)

    def mark_confirmed(self, scan_id: str, inventory_id: str) -> LabelScan:
        normalized_id = str(scan_id).strip().upper()
        now = utc_now()
        with self.engine.begin() as connection:
            row = _select_scan(connection, normalized_id)
            if row is None:
                raise KeyError(f"Label scan was not found: {scan_id}")
            linked = row["linked_inventory_id"]
            if linked and linked != inventory_id:
                raise ValueError(f"Scan {scan_id} is already linked to inventory item {linked}")
            connection.execute(
                update(label_scans)
                .where(label_scans.c.scan_id == normalized_id)
                .values(
                    state=SCAN_CONFIRMED,
                    validation_status=SCAN_CONFIRMED,
                    linked_inventory_id=inventory_id,
                    confirmed_at=row["confirmed_at"] or now,
                    updated_at=now,
                )
            )
            return _scan_from_row(_select_scan(connection, normalized_id))

    def mark_cancelled(self, scan_id: str) -> LabelScan:
        scan = self._get_required(scan_id)
        if scan.linked_inventory_id:
            raise ValueError("A confirmed label scan cannot be cancelled.")
        self._update(
            scan_id,
            state=SCAN_CANCELLED,
            validation_status=SCAN_CANCELLED,
            updated_at=utc_now(),
        )
        return self._get_required(scan_id)

    def mark_failed(self, scan_id: str, error: object) -> LabelScan:
        self._update(
            scan_id,
            state=SCAN_FAILED,
            validation_status=SCAN_FAILED,
            error_details=str(error)[:4000],
            updated_at=utc_now(),
        )
        return self._get_required(scan_id)

    def _update(self, scan_id: str, *, require_unlinked: bool = False, **values: object) -> None:
        normalized_id = str(scan_id).strip().upper()
        statement = update(label_scans).where(label_scans.c.scan_id == normalized_id)
        if require_unlinked:
            statement = statement.where(label_scans.c.linked_inventory_id.is_(None))
        with self.engine.begin() as connection:
            result = connection.execute(statement.values(**values))
            if result.rowcount != 1:
                raise KeyError(f"Label scan was not found or cannot be changed: {normalized_id}")

    def _get_required(self, scan_id: str) -> LabelScan:
        scan = self.get(scan_id)
        if scan is None:
            raise KeyError(f"Label scan was not found: {scan_id}")
        return scan


def _find_duplicate(connection, message_id: str, image_hash: str):
    return connection.execute(
        select(label_scans)
        .where(
            or_(
                label_scans.c.discord_message_id == message_id,
                label_scans.c.image_sha256 == image_hash,
            )
        )
        .limit(1)
    ).mappings().first()


def _select_scan(connection, scan_id: str):
    return connection.execute(
        select(label_scans).where(label_scans.c.scan_id == scan_id)
    ).mappings().first()


def _scan_from_row(row) -> LabelScan:
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
