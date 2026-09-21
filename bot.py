"""Discord bot entry point. Live startup is opt-in; see README for the offline demo."""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env before importing modules that read settings at import time.
load_dotenv(Path(__file__).with_name(".env"))

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

from utils.stockx_api import ensure_valid_token  # noqa: E402

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
    if os.getenv("ENABLE_LIVE_INTEGRATIONS", "false").lower() != "true":
        raise RuntimeError("Live integrations are disabled. Run `python demo.py` for the offline demo.")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN not found in environment variables")

    await asyncio.to_thread(ensure_valid_token)

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    for cog in COGS:
        await bot.load_extension(cog)
    await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
