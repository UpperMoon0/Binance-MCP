import hashlib
import hmac
import logging
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


def test_http_transport_info_logging_is_suppressed():
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


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
async def test_flexible_earn_subscription_post_payload():
    seen = {}

    async def handler(request: httpx.Request):
        seen["request"] = request
        return httpx.Response(200, json={"purchaseId": "p1", "success": True})

    client = BinanceClient(
        cfg(api_key="key", api_secret="secret", trading_enabled=True),
        transport=httpx.MockTransport(handler),
    )
    result = await client.simple_earn_subscribe("USD1001", "365", True, "SPOT")
    assert result == {"purchaseId": "p1", "success": True}
    request = seen["request"]
    assert request.method == "POST"
    assert request.url.path == "/sapi/v1/simple-earn/flexible/subscribe"
    query = parse_qs(request.url.query.decode())
    assert query["productId"] == ["USD1001"]
    assert query["amount"] == ["365"]
    assert query["autoSubscribe"] == ["true"]
    assert query["sourceAccount"] == ["SPOT"]


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
    with pytest.raises(BinanceClientError, match="trading is disabled"):
        await client.simple_earn_subscribe("USD1001", "1")

@pytest.mark.asyncio
async def test_connection_reuse_close_and_rate_limit_cooldown():
    calls = []
    async def handler(request):
        calls.append(request)
        if len(calls) == 3:
            return httpx.Response(429, headers={"Retry-After": "120", "X-MBX-USED-WEIGHT-1M": "6000"}, json={"code": -1003})
        return httpx.Response(200, json={"price": "1"})
    client = BinanceClient(cfg(), transport=httpx.MockTransport(handler))
    await client.public_get("spot", "/api/v3/ticker/price")
    session = client._http
    await client.public_get("spot", "/api/v3/ticker/price")
    assert client._http is session
    with pytest.raises(BinanceClientError) as exc:
        await client.public_get("spot", "/api/v3/ticker/price")
    assert exc.value.metadata()["retryAfter"] == 120
    assert exc.value.metadata()["rateLimitHeaders"]["x-mbx-used-weight-1m"] == "6000"
    with pytest.raises(BinanceClientError, match="cooldown"):
        await client.public_get("spot", "/api/v3/ticker/price")
    assert len(calls) == 3
    await client.close()
    assert session.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(504, -1007), (500, -1000), (400, -1006), (409, -2021)])
async def test_structured_unknown_errors_do_not_leak_signed_urls(status, code):
    async def handler(request):
        return httpx.Response(status, json={"code": code, "msg": str(request.url)})
    client = BinanceClient(cfg(api_key="credential-key", api_secret="credential-secret", trading_enabled=True),
                           transport=httpx.MockTransport(handler))
    with pytest.raises(BinanceClientError) as exc:
        await client.order("spot", "/api/v3/order", "create", {"symbol": "BTCUSDT"})
    assert exc.value.outcome_unknown
    assert exc.value.status == status
    assert exc.value.code == code
    assert "signature" not in str(exc.value)
    assert "credential" not in str(exc.value)
    await client.close()


@pytest.mark.asyncio
async def test_server_clock_offset_is_applied_without_retrying_writes(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1700000000.0)
    calls = []
    async def handler(request):
        calls.append(request)
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 1700000002000})
        assert parse_qs(request.url.query.decode())["timestamp"] == ["1700000002000"]
        return httpx.Response(200, json={"ok": True})
    client = BinanceClient(cfg(api_key="k", api_secret="s", trading_enabled=True), transport=httpx.MockTransport(handler))
    assert await client.sync_time() == 2000
    await client.order("spot", "/api/v3/order", "create", {})
    assert len(calls) == 2
    await client.close()
