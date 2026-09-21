"""Discord channel configuration: IDs come from the environment, never source."""

import os


def channel_id(name: str) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else 0


async def in_channel(ctx, channel_id: int) -> bool:
    """Reject a command sent outside its configured channel; 0 means unrestricted."""

    if not channel_id or ctx.channel.id == channel_id:
        return True
    await ctx.send(f"This command can only be used in the <#{channel_id}> channel.")
    return False
