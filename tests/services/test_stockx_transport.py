from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import requests

from services import stockx
from utils import stockx_api


def response(status_code, retry_after=None):
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return SimpleNamespace(status_code=status_code, headers=headers)


def test_rate_limited_retry_reenters_global_request_gate(monkeypatch):
    responses = iter([response(429, "2"), response(200)])
    gate_calls = []
    sleeps = []
    request_calls = []
    monkeypatch.setattr(stockx, "wait_for_stockx_slot", lambda: gate_calls.append(True))
    monkeypatch.setattr(stockx.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        stockx.requests,
        "get",
        lambda url, **kwargs: request_calls.append((url, kwargs)) or next(responses),
    )

    result = stockx.stockx_get("https://example.test", headers={"Authorization": "Bearer token"})

    assert result.status_code == 200
    assert len(request_calls) == 2
    assert len(gate_calls) == 2
    assert sleeps == [2.0]


def test_unauthorized_request_refreshes_header_and_reenters_gate(monkeypatch):
    responses = iter([response(401), response(200)])
    seen_headers = []
    gate_calls = []
    rejected_tokens = []
    monkeypatch.setattr(stockx, "wait_for_stockx_slot", lambda: gate_calls.append(True))
    monkeypatch.setattr(
        stockx,
        "get_valid_access_token",
        lambda rejected_token=None: rejected_tokens.append(rejected_token) or "new-token",
    )

    def fake_get(_url, **kwargs):
        seen_headers.append(dict(kwargs["headers"]))
        return next(responses)

    monkeypatch.setattr(stockx.requests, "get", fake_get)

    result = stockx.stockx_get(
        "https://example.test",
        headers={"Authorization": "Bearer old-token", "x-api-key": "key"},
    )

    assert result.status_code == 200
    assert len(gate_calls) == 2
    assert rejected_tokens == ["old-token"]
    assert seen_headers == [
        {"Authorization": "Bearer old-token", "x-api-key": "key"},
        {"Authorization": "Bearer new-token", "x-api-key": "key"},
    ]


def test_network_timeout_retries_with_backoff(monkeypatch):
    responses = iter([requests.ReadTimeout("slow"), requests.ReadTimeout("slow"), response(200)])
    gate_calls = []
    sleeps = []
    monkeypatch.setattr(stockx, "wait_for_stockx_slot", lambda: gate_calls.append(True))
    monkeypatch.setattr(stockx.time, "sleep", lambda seconds: sleeps.append(seconds))

    def fake_get(*_args, **_kwargs):
        item = next(responses)
        if isinstance(item, requests.RequestException):
            raise item
        return item

    monkeypatch.setattr(stockx.requests, "get", fake_get)

    result = stockx.stockx_get("https://example.test")

    assert result.status_code == 200
    assert len(gate_calls) == 3
    assert sleeps == [3.0, 8.0]


def test_expired_token_is_refreshed_on_demand(monkeypatch):
    monkeypatch.setitem(stockx_api.token_store, "access_token", "expired-token")
    monkeypatch.setitem(
        stockx_api.token_store,
        "expires_at",
        datetime.now(UTC) - timedelta(seconds=1),
    )
    refresh_calls = []

    def refresh():
        refresh_calls.append(True)
        stockx_api.token_store["access_token"] = "fresh-token"
        stockx_api.token_store["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
        return True

    monkeypatch.setattr(stockx_api, "refresh_access_token", refresh)

    assert stockx_api.get_valid_access_token() == "fresh-token"
    assert refresh_calls == [True]


def test_rejected_token_reuses_refresh_completed_by_another_request(monkeypatch):
    monkeypatch.setitem(stockx_api.token_store, "access_token", "already-refreshed")
    monkeypatch.setitem(
        stockx_api.token_store,
        "expires_at",
        datetime.now(UTC) + timedelta(hours=1),
    )
    refresh_calls = []
    monkeypatch.setattr(
        stockx_api,
        "refresh_access_token",
        lambda: refresh_calls.append(True) or True,
    )

    assert (
        stockx_api.get_valid_access_token(rejected_token="old-rejected-token")
        == "already-refreshed"
    )
    assert refresh_calls == []


def test_stockx_request_spacing_matches_documented_limit():
    assert stockx.STOCKX_MIN_REQUEST_INTERVAL_SECONDS >= 1.0
