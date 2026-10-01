import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

import bot as bot_module
from utils import stockx_api


def fake_discord_bot(monkeypatch):
    fake_bot = SimpleNamespace(
        event=lambda handler: handler,
        load_extension=AsyncMock(),
        start=AsyncMock(),
    )
    monkeypatch.setattr(bot_module.commands, "Bot", Mock(return_value=fake_bot))
    return fake_bot


@pytest.fixture
def live_startup(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_INTEGRATIONS", "true")
    monkeypatch.setenv("DISCORD_TOKEN", "fake-test-token")
    monkeypatch.delenv("DISCORD_ONLY", raising=False)
    monkeypatch.setattr(bot_module, "load_credentials", Mock())


def test_live_integrations_stay_opt_in(monkeypatch):
    monkeypatch.delenv("ENABLE_LIVE_INTEGRATIONS", raising=False)
    monkeypatch.setattr(bot_module, "load_credentials", Mock())
    create_bot = Mock()
    monkeypatch.setattr(bot_module.commands, "Bot", create_bot)

    with pytest.raises(RuntimeError, match="Live integrations are disabled"):
        asyncio.run(bot_module.main())

    create_bot.assert_not_called()


def test_discord_only_skips_business_integrations(monkeypatch, live_startup):
    monkeypatch.setenv("DISCORD_ONLY", "1")
    refresh = Mock()
    monkeypatch.setattr(stockx_api, "ensure_valid_token", refresh)
    fake_bot = fake_discord_bot(monkeypatch)

    asyncio.run(bot_module.main())

    refresh.assert_not_called()
    fake_bot.load_extension.assert_not_called()
    fake_bot.start.assert_awaited_once_with("fake-test-token")


def test_default_startup_loads_business_integrations(monkeypatch, live_startup):
    refresh = Mock()
    monkeypatch.setattr(stockx_api, "ensure_valid_token", refresh)
    fake_bot = fake_discord_bot(monkeypatch)

    asyncio.run(bot_module.main())

    refresh.assert_called_once_with()
    assert fake_bot.load_extension.await_args_list == [call(cog) for cog in bot_module.COGS]
    fake_bot.start.assert_awaited_once_with("fake-test-token")


def test_credential_failure_prevents_bot_startup(monkeypatch):
    monkeypatch.setattr(bot_module, "load_credentials", Mock(side_effect=RuntimeError("Secret denied")))
    create_bot = Mock()
    monkeypatch.setattr(bot_module.commands, "Bot", create_bot)

    with pytest.raises(RuntimeError, match="Secret denied"):
        asyncio.run(bot_module.main())

    create_bot.assert_not_called()


def test_credentials_load_before_stockx_import_and_extensions(monkeypatch):
    import builtins

    events = []
    monkeypatch.delenv("DISCORD_ONLY", raising=False)

    def load_credentials(_path):
        events.append("credentials")
        monkeypatch.setenv("ENABLE_LIVE_INTEGRATIONS", "true")
        monkeypatch.setenv("DISCORD_TOKEN", "loaded-test-token")

    real_import = builtins.__import__

    def track_import(name, *args, **kwargs):
        if name == "utils.stockx_api":
            assert events == ["credentials"]
            events.append("stockx-import")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(bot_module, "load_credentials", load_credentials)
    monkeypatch.setattr(builtins, "__import__", track_import)
    monkeypatch.setattr(stockx_api, "ensure_valid_token", lambda: events.append("refresh"))

    async def load_extension(_name):
        assert events == ["credentials", "stockx-import", "refresh"]

    fake_bot = SimpleNamespace(event=lambda handler: handler, load_extension=load_extension, start=AsyncMock())
    monkeypatch.setattr(bot_module.commands, "Bot", Mock(return_value=fake_bot))

    asyncio.run(bot_module.main())

    fake_bot.start.assert_awaited_once_with("loaded-test-token")
