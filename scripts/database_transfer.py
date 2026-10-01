import argparse
import json
from pathlib import Path
from dataclasses import asdict, dataclass

from sqlalchemy import Connection, Engine, func, insert, select, inspect
from sqlalchemy.engine import make_url

from services.inventory_repository import is_valid_inventory_id
from services.database import advance_postgresql_sequence, create_engine_for_url
from services.database_tables import (
    inventory_id_sequence,
    inventory_units,
    label_scans,
    processed_stockx_orders,
)

@dataclass(frozen=True)
class DatabaseSummary:
    inventory_count: int
    label_scan_count: int
    processed_order_count: int
    highest_sequence: int

def summarize_database(
    connection: Connection,
    processed_orders: set[str] | None = None,
) -> DatabaseSummary:
    if processed_orders is None:
        processed_orders=read_processed_orders(connection)
    return DatabaseSummary(
        inventory_count=connection.execute(
            select(func.count()).select_from(inventory_units)
        ).scalar_one(),
        label_scan_count=connection.execute(
            select(func.count()).select_from(label_scans)
        ).scalar_one(),
        processed_order_count=len(processed_orders),
        highest_sequence=connection.execute(
            select(func.max(inventory_id_sequence.c.sequence_number))
        ).scalar_one() or 0,
    )

def validate_inventory_ids(connection: Connection) -> list[str]:
    problems = []

    allocated_numbers = set(
        connection.execute(
            select(inventory_id_sequence.c.sequence_number)
        ).scalars()
    )

    inventory_ids = connection.execute(
        select(inventory_units.c.inventory_id)
    ).scalars()

    for inventory_id in inventory_ids:
        if (
            not is_valid_inventory_id(inventory_id)
            or inventory_id != inventory_id.strip().upper()
        ):
            problems.append(f"Invalid inventory ID: {inventory_id!r}")
            continue

        sequence_number = int(inventory_id.removeprefix("INV-"))

        if sequence_number not in allocated_numbers:
            problems.append(
                f"Missing allocation for inventory ID: {inventory_id}"
            )

    return problems

def validate_scan_links(connection: Connection) -> list[str]:
    statement = (
        select(label_scans.c.scan_id, label_scans.c.linked_inventory_id)
        .select_from(
            label_scans.outerjoin(
                inventory_units,
                label_scans.c.linked_inventory_id
                == inventory_units.c.inventory_id,
            )
        )
        .where(
            label_scans.c.linked_inventory_id.is_not(None),
            inventory_units.c.inventory_id.is_(None),
        )
    )

    return [
        f"Scan {scan_id} links to missing inventory item: {inventory_id}"
        for scan_id, inventory_id in connection.execute(statement)
    ]

def require_empty_destination(connection: Connection) -> None:
    tables = (
        inventory_id_sequence,
        inventory_units,
        label_scans,
        processed_stockx_orders,
    )

    for table in tables:
        existing_row = connection.execute(
            select(table).limit(1)
        ).first()

        if existing_row is not None:
            raise ValueError(
                f"Destination must be empty; {table.name} contains data."
            )

def copy_rows(
    source: Connection,
    destination: Connection,
    processed_orders: set[str] | None = None,
) -> None:
    if processed_orders is None:
        processed_orders = read_processed_orders(source)

    for table in (
        inventory_id_sequence,
        inventory_units,
        label_scans,
    ):
        rows = [
            dict(row)
            for row in source.execute(select(table)).mappings()
        ]

        if rows:
            destination.execute(insert(table), rows)

    if processed_orders:
        destination.execute(
            insert(processed_stockx_orders),
            [
                {"order_number": number}
                for number in sorted(processed_orders)
            ],
        )

def verify_copied_records(
    source: Connection,
    destination: Connection,
    processed_orders: set[str],
) -> None:
    for table in (
        inventory_id_sequence,
        inventory_units,
        label_scans,
    ):
        statement = select(table).order_by(*table.primary_key.columns)

        source_rows = [
            dict(row)
            for row in source.execute(statement).mappings()
        ]
        destination_rows = [
            dict(row)
            for row in destination.execute(statement).mappings()
        ]

        if source_rows != destination_rows:
            raise ValueError(f"Copied records differ in {table.name}.")

    if read_processed_orders(destination) != processed_orders:
        raise ValueError("Copied processed-order numbers differ.")

def transfer_database(
    source: Connection,
    destination_engine: Engine,
    *,
    dry_run: bool,
    processed_orders: set[str] | None = None,
) -> DatabaseSummary:
    problems = validate_inventory_ids(source)
    problems.extend(validate_scan_links(source))

    if problems:
        raise ValueError("\n".join(problems))

    if processed_orders is None:
        processed_orders = read_processed_orders(source)

    source_summary = summarize_database(source, processed_orders)

    with destination_engine.begin() as destination:
        require_empty_destination(destination)

        if dry_run:
            return source_summary

        copy_rows(source, destination, processed_orders)

        destination_summary = summarize_database(destination)
        if destination_summary != source_summary:
            raise ValueError("Source and destination summaries do not match.")

        verify_copied_records(
            source,
            destination,
            processed_orders,
        )

        if destination_summary.highest_sequence > 0:
            advance_postgresql_sequence(
                destination,
                destination_summary.highest_sequence,
            )

    return destination_summary

def read_processed_orders(
    source: Connection,
    legacy_json: str | None = None,
) -> set[str]:
    orders = set()
    has_table = inspect(source).has_table("processed_stockx_orders")

    if has_table:
        orders.update(
            source.execute(
                select(processed_stockx_orders.c.order_number)
            ).scalars()
        )
    elif legacy_json is None:
        raise ValueError(
            "Source has no processed-order table. "
            "Provide --legacy-orders-json."
        )

    if legacy_json is not None:
        data = json.loads(Path(legacy_json).read_text(encoding="utf-8"))
        numbers = data.get("processed_order_numbers") if isinstance(data, dict) else None

        if not isinstance(numbers, list) or any(
            not isinstance(number, str) or not number.strip()
            for number in numbers
        ):
            raise ValueError(
                "Legacy JSON must contain a processed_order_numbers "
                "list of non-empty strings."
            )

        orders.update(number.strip() for number in numbers)

    return orders

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and transfer application data."
    )
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--destination-url")
    parser.add_argument("--legacy-orders-json")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    if (args.dry_run or args.confirm) and not args.destination_url:
        parser.error("--dry-run and --confirm require --destination-url.")

    for role, database_url in (
        ("source", args.source_url),
        ("destination", args.destination_url),
    ):
        if database_url is None:
            continue

        url = make_url(database_url)

        if url.get_backend_name() == "sqlite":
            if not url.database or not Path(url.database).is_file():
                parser.error(f"The {role} SQLite file must already exist.")

    source_engine = create_engine_for_url(args.source_url)

    try:
        with source_engine.connect() as source:
            processed_orders = read_processed_orders(
                source, args.legacy_orders_json
            )
            summary = summarize_database(source, processed_orders)
            problems = validate_inventory_ids(source)
            problems.extend(validate_scan_links(source))

            print(json.dumps({
                "summary": asdict(summary),
                "problems": problems,
            }, indent=2))

            if problems:
                raise SystemExit(1)

            if args.destination_url:
                destination_engine = create_engine_for_url(
                    args.destination_url
                )

                try:
                    transfer_database(
                        source,
                        destination_engine,
                        dry_run=not args.confirm,
                        processed_orders=processed_orders,
                    )
                finally:
                    destination_engine.dispose()

                if args.confirm:
                    print("Transfer committed.")
                else:
                    print("Destination is empty. No data was copied.")
    finally:
        source_engine.dispose()

if __name__ == "__main__":
    main()