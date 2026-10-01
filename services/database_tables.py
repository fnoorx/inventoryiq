"""Table definitions shared by the repositories and Alembic migrations.

They mirror the SQLite schema in ``database_schema.py``.
"""

from sqlalchemy import Column, Float, Index, Integer, MetaData, Table, Text

metadata = MetaData()

inventory_id_sequence = Table(
    "inventory_id_sequence",
    metadata,
    Column("sequence_number", Integer, primary_key=True, autoincrement=True),
    Column("allocated_at", Text, nullable=False),
    sqlite_autoincrement=True,
)

inventory_units = Table(
    "inventory_units",
    metadata,
    Column("inventory_id", Text, primary_key=True),
    Column("location", Text, nullable=False),
    Column("purchase_date", Text, nullable=False),
    Column("item_type", Text, nullable=False),
    Column("style_code", Text, nullable=False),
    Column("product_name", Text, nullable=False),
    Column("size", Text, nullable=False),
    Column("price_paid", Float, nullable=False),
    Column("total_cost", Float, nullable=False),
    Column("status", Text, nullable=False),
    Column("source_identifier", Text, nullable=False, server_default=""),
    Column("stockx_product_id", Text, nullable=False, server_default=""),
    Column("stockx_variant_id", Text, nullable=False, server_default=""),
    Column("sheet_row", Integer),
    Column("sheet_sync_status", Text, nullable=False),
    Column("sheet_sync_error", Text),
    Column("discord_message_id", Text, unique=True),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("sheet_synced_at", Text),
)

Index(
    "inventory_units_sheet_sync_status_idx",
    inventory_units.c.sheet_sync_status,
)
Index(
    "inventory_units_sheet_row_idx",
    inventory_units.c.sheet_row,
)

label_scans = Table(
    "label_scans",
    metadata,
    Column("scan_id", Text, primary_key=True),
    Column("discord_message_id", Text, nullable=False, unique=True),
    Column("discord_attachment_id", Text, nullable=False),
    Column("image_sha256", Text, nullable=False, unique=True),
    Column("state", Text, nullable=False),
    Column("barcode_results_json", Text, nullable=False, server_default="[]"),
    Column("raw_vision_json", Text),
    Column("normalized_extraction_json", Text),
    Column("market_data_json", Text),
    Column("stockx_product_id", Text, nullable=False, server_default=""),
    Column("stockx_variant_id", Text, nullable=False, server_default=""),
    Column("validation_status", Text, nullable=False, server_default="received"),
    Column("warning_details_json", Text, nullable=False, server_default="[]"),
    Column("error_details", Text),
    Column("supplied_price", Float),
    Column("location", Text, nullable=False),
    Column("purchase_date", Text, nullable=False),
    Column("linked_inventory_id", Text),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("confirmed_at", Text),
)

Index(
    "label_scans_message_attachment_idx",
    label_scans.c.discord_message_id,
    label_scans.c.discord_attachment_id,
    unique=True,
)
Index("label_scans_state_idx", label_scans.c.state)

processed_stockx_orders = Table(
    "processed_stockx_orders",
    metadata,
    Column("order_number", Text, primary_key=True),
)
