import hashlib
import hmac
from urllib.parse import parse_qs

import httpx
import pytest

from binance_mcp.client import BinanceClient, BinanceClientError
from binance_mcp.config import BinanceConfig


def cfg(**overrides):
    values = dict(
        api_key="",
        api_secret="",
        private_key_path="",
        private_key_passphrase="",
        trading_enabled=False,
        timeout_seconds=5.0,
        recv_window_ms=5000,
    )
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
async def test_portfolio_margin_is_bound_to_papi_host():
    async def handler(request: httpx.Request):
        assert request.url.host == "papi.binance.com"
        assert request.url.path == "/papi/v1/account"
        return httpx.Response(200, json={"ok": True})

    client = BinanceClient(cfg(), transport=httpx.MockTransport(handler))
    assert await client.public_get("portfolio_margin", "/papi/v1/account") == {"ok": True}


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
async def test_signed_boolean_serialization_is_lowercase():
    seen = {}

    async def handler(request: httpx.Request):
        seen["request"] = request
        return httpx.Response(200, json={"ok": True})

    client = BinanceClient(cfg(api_key="key", api_secret="secret"), transport=httpx.MockTransport(handler))
    await client.signed_get("spot", "/api/v3/account", {"omitZeroBalances": True, "flag": False})
    query = seen["request"].url.query.decode()
    unsigned = query.rsplit("&signature=", 1)[0]
    assert "omitZeroBalances=true" in unsigned
    assert "flag=false" in unsigned
    assert "True" not in unsigned
    assert "False" not in unsigned


@pytest.mark.asyncio
async def test_flexible_earn_redemption_post_payload():
    seen = {}

    async def handler(request: httpx.Request):
        seen["request"] = request
        return httpx.Response(200, json={"success": True})

    client = BinanceClient(
        cfg(api_key="key", api_secret="secret", trading_enabled=True),
        transport=httpx.MockTransport(handler),
    )
    result = await client.simple_earn_redeem("USDT001", "700", "SPOT")
    assert result == {"success": True}
    request = seen["request"]
    assert request.method == "POST"
    assert request.url.path == "/sapi/v1/simple-earn/flexible/redeem"
    query = parse_qs(request.url.query.decode())
    assert query["productId"] == ["USDT001"]
    assert query["amount"] == ["700"]
    assert query["destAccount"] == ["SPOT"]


@pytest.mark.asyncio
async def test_dual_investment_subscribe_post_payload_defaults_none():
    seen = {}

    async def handler(request: httpx.Request):
        seen["request"] = request
        return httpx.Response(200, json={"purchaseStatus": "PURCHASE_SUCCESS"})

    client = BinanceClient(
        cfg(api_key="key", api_secret="secret", trading_enabled=True),
        transport=httpx.MockTransport(handler),
    )
    await client.dual_investment_subscribe("2650584", 51015871919, "700")
    request = seen["request"]
    assert request.method == "POST"
    assert request.url.path == "/sapi/v1/dci/product/subscribe"
    query = parse_qs(request.url.query.decode())
    assert query["id"] == ["2650584"]
    assert query["orderId"] == ["51015871919"]
    assert query["depositAmount"] == ["700"]
    assert query["autoCompoundPlan"] == ["NONE"]


@pytest.mark.asyncio
async def test_trading_disabled_by_default():
    client = BinanceClient(cfg(api_key="k", api_secret="s", trading_enabled=False))
    with pytest.raises(BinanceClientError, match="trading is disabled"):
        await client.order("spot", "/api/v3/order", "create", {"symbol": "BTCUSDT"})
    with pytest.raises(BinanceClientError, match="trading is disabled"):
        await client.simple_earn_redeem("USDT001", "1")
