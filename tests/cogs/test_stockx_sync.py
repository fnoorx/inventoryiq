import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs import stockx_sync
from services.stockx_order_sheet_sync import StockxOrderSheetUpdate, StockxOrderSyncResult


def make_ctx(channel_id=stockx_sync.CHANNEL_ID):
    return SimpleNamespace(
        channel=SimpleNamespace(id=channel_id),
        send=AsyncMock(),
    )


def make_message(content="sync", channel_id=stockx_sync.CHANNEL_ID, author_bot=False):
    return SimpleNamespace(
        author=SimpleNamespace(bot=author_bot),
        channel=SimpleNamespace(id=channel_id, send=AsyncMock()),
        content=content,
    )


def run_sync_command(ctx):
    cog = stockx_sync.StockxSync(bot=object())
    command_callback = stockx_sync.StockxSync.sync.callback
    asyncio.run(command_callback(cog, ctx))


def run_sync_listener(message):
    cog = stockx_sync.StockxSync(bot=object())
    asyncio.run(cog.on_message(message))


def run_auto_sync(cog, channel, announce_start=False, notify_no_changes=False):
    asyncio.run(
        cog._run_and_send_sync(
            channel,
            announce_start=announce_start,
            notify_no_changes=notify_no_changes,
        )
    )


def sample_result():
    return StockxOrderSyncResult(
        updated=[
            StockxOrderSheetUpdate(
                order_number="100000001-100000002",
                row_number=12,
                style_code="QX1001-200",
                size="10",
                sale_date="2026-07-01",
                payout=76.81,
                platform="StockX",
                profit=21.81,
                product_name="Aether Trail Test",
                stockx_url="https://stockx.com/search?s=QX1001-200",
                inventory_id="INV-000001",
            )
        ],
        unmatched=[
            {
                "order_number": "02-DEMO000001",
                "style_code": "QX1009-800",
                "size": "9.5",
            }
        ],
        missing_data=[
            {
                "order_number": "111-222",
                "reason": "missing payout",
            }
        ],
        already_processed=["999-888"],
    )


def empty_result():
    return StockxOrderSyncResult()


def test_build_sync_embeds_summarizes_result():
    embeds = stockx_sync.build_sync_embeds(sample_result())

    assert [embed.title for embed in embeds] == [
        "StockX Order Sync",
        "Updated Rows",
        "Unmatched Orders",
        "Missing Data",
    ]

    summary_fields = {field.name: field.value for field in embeds[0].fields}
    assert summary_fields == {
        "Updated": "1",
        "Unmatched": "1",
        "Missing Data": "1",
        "Already Processed": "1",
    }

    assert embeds[1].fields[0].name == "INV-000001 | Row 12 | QX1001-200 size 10"
    assert embeds[1].fields[0].value == (
        "[Aether Trail Test](https://stockx.com/search?s=QX1001-200)\n"
        "Payout: $76.81 | Profit: $21.81\n"
        "Platform: StockX | Status: Sold\n"
        "Sale date: 2026-07-01"
    )
    assert embeds[2].fields[0].value == "[QX1009-800](https://stockx.com/search?s=QX1009-800) size 9.5 | order 02-DEMO000001"
    assert embeds[3].fields[0].value == "- 111-222: missing payout"


def test_should_notify_only_for_actionable_results():
    assert stockx_sync.should_notify(sample_result()) is True

    result = empty_result()
    result.already_processed.append("999-888")
    assert stockx_sync.should_notify(result) is False


def test_parse_poll_interval_minutes_falls_back_on_bad_values():
    assert stockx_sync.parse_poll_interval_minutes("15") == 15
    assert stockx_sync.parse_poll_interval_minutes("") == stockx_sync.DEFAULT_POLL_INTERVAL_MINUTES
    assert stockx_sync.parse_poll_interval_minutes("bad") == stockx_sync.DEFAULT_POLL_INTERVAL_MINUTES
    assert stockx_sync.parse_poll_interval_minutes("-1") == stockx_sync.DEFAULT_POLL_INTERVAL_MINUTES


def test_default_background_sync_interval_is_three_hours():
    assert stockx_sync.DEFAULT_POLL_INTERVAL_MINUTES == 180
    assert stockx_sync.parse_poll_interval_minutes(None) == 180


def test_sync_command_rejects_wrong_channel(monkeypatch):
    to_thread = AsyncMock(return_value=sample_result())
    monkeypatch.setattr(stockx_sync.asyncio, "to_thread", to_thread)
    ctx = make_ctx(channel_id=123)

    run_sync_command(ctx)

    ctx.send.assert_awaited_once_with(
        f"This command can only be used in the <#{stockx_sync.CHANNEL_ID}> channel."
    )
    to_thread.assert_not_awaited()


def test_sync_command_runs_and_sends_summary(monkeypatch):
    to_thread = AsyncMock(return_value=sample_result())
    monkeypatch.setattr(stockx_sync.asyncio, "to_thread", to_thread)
    ctx = make_ctx()

    run_sync_command(ctx)

    assert ctx.send.await_args_list[0].args[0] == "Running StockX order sync..."
    sent_embeds = [call.kwargs["embed"] for call in ctx.send.await_args_list[1:]]
    assert [embed.title for embed in sent_embeds] == [
        "StockX Order Sync",
        "Updated Rows",
        "Unmatched Orders",
        "Missing Data",
    ]
    to_thread.assert_awaited_once()


def test_auto_sync_sends_embeds_without_running_message(monkeypatch):
    to_thread = AsyncMock(return_value=sample_result())
    monkeypatch.setattr(stockx_sync.asyncio, "to_thread", to_thread)
    channel = SimpleNamespace(send=AsyncMock())
    cog = stockx_sync.StockxSync(bot=object())

    run_auto_sync(cog, channel)

    sent_embeds = [call.kwargs["embed"] for call in channel.send.await_args_list]
    assert [embed.title for embed in sent_embeds] == [
        "StockX Order Sync",
        "Updated Rows",
        "Unmatched Orders",
        "Missing Data",
    ]
    to_thread.assert_awaited_once()


def test_auto_sync_stays_silent_when_nothing_changed(monkeypatch):
    to_thread = AsyncMock(return_value=empty_result())
    monkeypatch.setattr(stockx_sync.asyncio, "to_thread", to_thread)
    channel = SimpleNamespace(send=AsyncMock())
    cog = stockx_sync.StockxSync(bot=object())

    run_auto_sync(cog, channel)

    channel.send.assert_not_awaited()
    to_thread.assert_awaited_once()


def test_plain_sync_message_runs_in_sync_channel(monkeypatch):
    to_thread = AsyncMock(return_value=sample_result())
    monkeypatch.setattr(stockx_sync.asyncio, "to_thread", to_thread)
    message = make_message(content=" sync ")

    run_sync_listener(message)

    assert message.channel.send.await_args_list[0].args[0] == "Running StockX order sync..."
    sent_embeds = [call.kwargs["embed"] for call in message.channel.send.await_args_list[1:]]
    assert [embed.title for embed in sent_embeds] == [
        "StockX Order Sync",
        "Updated Rows",
        "Unmatched Orders",
        "Missing Data",
    ]
    to_thread.assert_awaited_once()


def test_plain_sync_message_ignores_other_content(monkeypatch):
    to_thread = AsyncMock(return_value=sample_result())
    monkeypatch.setattr(stockx_sync.asyncio, "to_thread", to_thread)
    message = make_message(content="hello")

    run_sync_listener(message)

    message.channel.send.assert_not_awaited()
    to_thread.assert_not_awaited()
