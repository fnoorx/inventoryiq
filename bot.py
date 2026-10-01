"""Discord bot entry point. Live startup is opt-in; see README for the offline demo.

Credentials come from ``.env`` locally, or from AWS Secrets Manager when
``CREDENTIALS_BACKEND=aws`` (see ``infra/aws/production/SECRETS.md``).
"""

import asyncio
import os
from pathlib import Path

import discord
from discord.ext import commands

from services.credentials import load_credentials

COGS = (
    "cogs.inventory",
    "cogs.photo_intake",
    "cogs.market_data",
    "cogs.catalogue_alerts",
    "cogs.brand_catalogue",
    "cogs.launch",
    "cogs.stockx_sync",
)


async def main():
    # Load settings before importing modules that read them at import time.
    await asyncio.to_thread(load_credentials, Path(__file__).with_name(".env"))
    if os.getenv("ENABLE_LIVE_INTEGRATIONS", "false").lower() != "true":
        raise RuntimeError("Live integrations are disabled. Run `python demo.py` for the offline demo.")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN not found in environment variables")

    # DISCORD_ONLY=1 connects without StockX or the cogs, to check a container starts.
    discord_only = os.getenv("DISCORD_ONLY", "0") == "1"
    if not discord_only:
        from utils.stockx_api import ensure_valid_token

        await asyncio.to_thread(ensure_valid_token)

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        print("InventoryIQ connected to Discord.", flush=True)

    if not discord_only:
        for cog in COGS:
            await bot.load_extension(cog)
    await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
