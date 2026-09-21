"""Run the portfolio walkthrough without network access or credentials."""

from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory


def offline_only(*args, **kwargs):
    raise RuntimeError("Network access is disabled in the offline demo")


def main():
    # Alert text contains non-ASCII characters; legacy Windows consoles default to cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    socket.socket.connect = offline_only
    socket.socket.connect_ex = offline_only
    from cogs.catalogue_alerts import build_message
    from cogs.inventory import parse_add_arguments
    from services.catalogue_profitability import CatalogueProduct, evaluate_catalogue_product
    from services.inventory_repository import InventoryRepository, InventoryUnitInput
    from services.scrape_catalogue import build_snapshot, demo_catalogue, find_new_products, find_price_changes

    products = demo_catalogue()
    previous = build_snapshot([{**products[0], "price": "$100.00"}])
    changes = find_new_products(previous, products) + find_price_changes(previous, products)
    print("SYNTHETIC CATALOGUE CHANGES")
    for message in build_message(changes):
        print(message)

    print("SYNTHETIC MARKET ESTIMATE (not live StockX data)")
    candidate = evaluate_catalogue_product(
        CatalogueProduct("Demo Running Shoe", products[0]["link"], "DEMO-001", 100),
        {"10": {"avg_sales": 180, "highest_bid": 150}},
        discount_percent=30,
    )
    print(f"Cost: ${candidate.discounted_price:.2f}; estimated profit: ${candidate.estimated_profit:.2f}")

    print("LOCAL INVENTORY + DISCOUNT + IDEMPOTENCY")
    location, date, style, size, price = parse_add_arguments(
        ("DEMO", "today", "DEMO-001", "10", "100", "30%")
    )
    with TemporaryDirectory(prefix="portfolio-inventory-") as directory:
        repository = InventoryRepository(Path(directory) / "demo.db")
        values = InventoryUnitInput(location, date, "Footwear", style, "Demo Running Shoe", size, price_paid=price)
        first, created = repository.create_unit(values, discord_message_id="demo-message")
        repeated, created_again = repository.create_unit(values, discord_message_id="demo-message")
        assert created and not created_again and first.inventory_id == repeated.inventory_id
        print(f"{first.inventory_id}: Price Paid ${first.price_paid:.2f}; Total ${first.total_cost:.2f}")
        print("Replaying the same message reuses its inventory ID.")
    print("Finished. Temporary demo database removed; no external accounts contacted.")


if __name__ == "__main__":
    main()
