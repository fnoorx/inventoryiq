"""One-time, versioned SQLite schema initialization for bot persistence."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import threading


CURRENT_SCHEMA_VERSION = 1

_schema_lock = threading.RLock()
_initialized_files: dict[Path, tuple[int, int] | None] = {}


def ensure_database_schema(database_path: str | Path) -> None:
    """Create or migrate the schema once per database file, tracked by PRAGMA user_version."""

    path = Path(database_path)
    key = path.resolve()

    with _schema_lock:
        signature = file_identity(path)
        if signature is not None and _initialized_files.get(key) == signature:
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            current_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_version > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {current_version} is newer than "
                    f"supported version {CURRENT_SCHEMA_VERSION}."
                )
            if current_version < 1:
                migrate_to_version_1(connection)
                connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        _initialized_files[key] = file_identity(path)


def migrate_to_version_1(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS inventory_id_sequence (
            sequence_number INTEGER PRIMARY KEY AUTOINCREMENT,
            allocated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS inventory_units (
            inventory_id TEXT PRIMARY KEY,
            location TEXT NOT NULL,
            purchase_date TEXT NOT NULL,
            item_type TEXT NOT NULL,
            style_code TEXT NOT NULL,
            product_name TEXT NOT NULL,
            size TEXT NOT NULL,
            price_paid REAL NOT NULL,
            total_cost REAL NOT NULL,
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
        );

        CREATE INDEX IF NOT EXISTS inventory_units_sheet_sync_status_idx
            ON inventory_units(sheet_sync_status);
        CREATE INDEX IF NOT EXISTS inventory_units_sheet_row_idx
            ON inventory_units(sheet_row);

        CREATE TRIGGER IF NOT EXISTS inventory_units_inventory_id_immutable
        BEFORE UPDATE OF inventory_id ON inventory_units
        WHEN NEW.inventory_id != OLD.inventory_id
        BEGIN
            SELECT RAISE(ABORT, 'inventory_id is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS inventory_sequence_no_delete
        BEFORE DELETE ON inventory_id_sequence
        BEGIN
            SELECT RAISE(ABORT, 'inventory ID allocations cannot be deleted');
        END;

        CREATE TABLE IF NOT EXISTS label_scans (
            scan_id TEXT PRIMARY KEY,
            discord_message_id TEXT NOT NULL UNIQUE,
            discord_attachment_id TEXT NOT NULL,
            image_sha256 TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL,
            barcode_results_json TEXT NOT NULL DEFAULT '[]',
            raw_vision_json TEXT,
            normalized_extraction_json TEXT,
            market_data_json TEXT,
            stockx_product_id TEXT NOT NULL DEFAULT '',
            stockx_variant_id TEXT NOT NULL DEFAULT '',
            validation_status TEXT NOT NULL DEFAULT 'received',
            warning_details_json TEXT NOT NULL DEFAULT '[]',
            error_details TEXT,
            supplied_price REAL,
            location TEXT NOT NULL,
            purchase_date TEXT NOT NULL,
            linked_inventory_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            confirmed_at TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS label_scans_message_attachment_idx
            ON label_scans(discord_message_id, discord_attachment_id);
        CREATE INDEX IF NOT EXISTS label_scans_state_idx
            ON label_scans(state);

        CREATE TRIGGER IF NOT EXISTS label_scans_scan_id_immutable
        BEFORE UPDATE OF scan_id ON label_scans
        WHEN NEW.scan_id != OLD.scan_id
        BEGIN
            SELECT RAISE(ABORT, 'scan_id is immutable');
        END;
        """
    )
    migrate_inventory_cost_columns(connection)
    migrate_label_scan_optional_columns(connection)


def migrate_inventory_cost_columns(connection: sqlite3.Connection) -> None:
    """Upgrade pre-versioned databases that stored a single ambiguous ``cost`` column."""

    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(inventory_units)").fetchall()
    }
    if "cost" in columns and "price_paid" not in columns:
        connection.execute("ALTER TABLE inventory_units RENAME COLUMN cost TO price_paid")
        columns.remove("cost")
        columns.add("price_paid")
    if "total_cost" not in columns:
        connection.execute("ALTER TABLE inventory_units ADD COLUMN total_cost REAL")
    connection.execute(
        """
        UPDATE inventory_units
        SET total_cost = ROUND(price_paid * 1.13, 2)
        WHERE total_cost IS NULL
        """
    )


def migrate_label_scan_optional_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(label_scans)").fetchall()
    }
    for name, declaration in {
        "market_data_json": "TEXT",
        "confirmed_at": "TEXT",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE label_scans ADD COLUMN {name} {declaration}")


def file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino
