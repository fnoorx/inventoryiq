"""StockX order numbers already applied to the Sheet, so each is processed once."""

from pathlib import Path

from sqlalchemy import select

from services.database import create_repository_engine, insert_ignoring_conflicts
from services.database_tables import processed_stockx_orders


class ProcessedOrderRepository:
    def __init__(self, database_path: str | Path | None = None):
        self.engine = create_repository_engine(database_path)

    def list_order_numbers(self) -> set[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(processed_stockx_orders.c.order_number)).scalars()
            return set(rows)

    def add(self, order_number: str) -> bool:
        """Record an order; False when it was already recorded, including by a concurrent sync."""

        normalized_number = str(order_number).strip()
        if not normalized_number:
            raise ValueError("Order number is required")

        with self.engine.begin() as connection:
            inserted = connection.execute(
                insert_ignoring_conflicts(connection, processed_stockx_orders).values(
                    order_number=normalized_number
                )
            ).first()
        return inserted is not None
