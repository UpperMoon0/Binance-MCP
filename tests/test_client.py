import hashlib
import hmac
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from binance_mcp.client import BinanceClient, BinanceClientError
from binance_mcp.config import BinanceConfig


def cfg(**overrides):
    values = dict(api_key="", api_secret="", private_key_path="", private_key_passphrase="", trading_enabled=False, timeout_seconds=5.0, recv_window_ms=5000)
    values.update(overrides)
    return BinanceConfig(**values)


def test_rejects_full_external_url():
    client = BinanceClient(cfg())
    with pytest.raises(BinanceClientError):
        client._validate_path("https://evil.example/api/v3/time")


def test_auth_status_is_redacted():
    client = BinanceClient(cfg(api_key="secret-key", api_secret="secret-secret"))
    status = client.auth_status()
    assert status["account_access_ready"] is True
    assert "secret-key" not in repr(status)
    assert "secret-secret" not in repr(status)
    assert status["withdrawals_supported"] is False


@pytest.mark.asyncio
async def test_public_get_is_bound_to_binance_host():
    async def handler(request: httpx.Request):
        assert request.url.host == "api.binance.com"
        assert request.url.path == "/api/v3/time"
        return httpx.Response(200, json={"serverTime": 123})

    client = BinanceClient(cfg(), transport=httpx.MockTransport(handler))
    assert await client.public_get("spot", "/api/v3/time") == {"serverTime": 123}


@pytest.mark.asyncio
async def test_hmac_signature_covers_percent_encoded_payload(monkeypatch):
    seen = {}
    async def handler(request: httpx.Request):
        seen["request"] = request
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("time.time", lambda: 1700000000.0)
    client = BinanceClient(cfg(api_key="key", api_secret="secret"), transport=httpx.MockTransport(handler))
    result = await client.signed_get("spot", "/api/v3/account", {"note": "１２３"})
    assert result == {"ok": True}
    request = seen["request"]
    assert request.headers["X-MBX-APIKEY"] == "key"
    query = request.url.query.decode()
    unsigned, signature = query.rsplit("&signature=", 1)
    expected = hmac.new(b"secret", unsigned.encode("ascii"), hashlib.sha256).hexdigest()
    assert signature == expected
    assert "%EF%BC%91" in unsigned


@pytest.mark.asyncio
async def test_trading_disabled_by_default():
    client = BinanceClient(cfg(api_key="k", api_secret="s", trading_enabled=False))
    with pytest.raises(BinanceClientError, match="trading is disabled"):
        await client.order("spot", "/api/v3/order", "create", {"symbol": "BTCUSDT"})
