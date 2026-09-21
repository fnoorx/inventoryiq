"""Tests must not contact external accounts or use deployment credentials."""

import os
import socket

import pytest

# Clear inherited deployment settings before application modules are imported.
for name in list(os.environ):
    if name.startswith(("STOCKX_", "DISCORD_", "GOOGLE_CREDS_", "OPENAI_")) or name in {
        "SHEET_ID", "INVENTORY_DATABASE_PATH", "INVENTORY_DEFAULT_LOCATION",
    } or name.endswith("_CHANNEL_ID"):
        os.environ.pop(name, None)
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
for name in ("INVENTORY", "MARKET", "CATALOGUE", "BRAND_CATALOGUE", "LAUNCH", "STOCKX_SYNC"):
    os.environ[f"{name}_CHANNEL_ID"] = "456"  # Synthetic test channel.


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    # Windows asyncio uses a loopback socket pair internally.
    def guard(original):
        def blocked(sock, address):
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
                return original(sock, address)
            raise AssertionError("Tests must mock external network calls")
        return blocked
    monkeypatch.setattr(socket.socket, "connect", guard(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(socket.socket.connect_ex))
