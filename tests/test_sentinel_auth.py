import json

import httpx
import pytest

from binance_mcp.sentinel_auth import SentinelAuthError, SentinelClient, SentinelConfig


def test_config_requires_explicit_url_and_allowlist(monkeypatch):
    monkeypatch.delenv("MCP_SENTINEL_URL", raising=False)
    monkeypatch.delenv("MCP_SENTINEL_ALLOWED_ACCOUNT_IDS", raising=False)
    with pytest.raises(SentinelAuthError) as exc:
        SentinelConfig.from_env()
    assert exc.value.status_code == 503

    monkeypatch.setenv("MCP_SENTINEL_URL", "http://sentinel:8081/")
    monkeypatch.setenv("MCP_SENTINEL_ALLOWED_ACCOUNT_IDS", "1, 7")
    config = SentinelConfig.from_env()
    assert config.base_url == "http://sentinel:8081"
    assert config.allowed_account_ids == frozenset({1, 7})


@pytest.mark.asyncio
async def test_authenticate_validates_identity_and_revokes_refresh_token():
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/auth/login":
            assert json.loads(request.content) == {"email": "owner@example.com", "password": "test-password"}
            return httpx.Response(200, json={
                "id": 1,
                "email": "owner@example.com",
                "token": "access-token",
                "refreshToken": "refresh-token",
            })
        if request.url.path == "/auth/validate":
            assert request.headers["Authorization"] == "Bearer access-token"
            return httpx.Response(200, json={"valid": True, "accountId": 1, "email": "owner@example.com"})
        if request.url.path == "/auth/logout":
            assert request.headers["Authorization"] == "Bearer access-token"
            assert json.loads(request.content) == {"accountId": 1}
            return httpx.Response(200, json={"message": "ok"})
        raise AssertionError(request.url.path)

    client = SentinelClient(
        SentinelConfig("http://sentinel:8081", frozenset({1})),
        transport=httpx.MockTransport(handler),
    )
    identity = await client.authenticate(" OWNER@example.com ", "test-password")
    assert identity.account_id == 1
    assert identity.email == "owner@example.com"
    assert calls == [
        ("POST", "/auth/login"),
        ("GET", "/auth/validate"),
        ("POST", "/auth/logout"),
    ]


@pytest.mark.asyncio
async def test_authenticate_rejects_non_allowlisted_account_and_still_logs_out():
    logged_out = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal logged_out
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"id": 2, "email": "other@example.com", "token": "access-token"})
        if request.url.path == "/auth/validate":
            return httpx.Response(200, json={"valid": True, "accountId": 2, "email": "other@example.com"})
        if request.url.path == "/auth/logout":
            logged_out = True
            return httpx.Response(200, json={"message": "ok"})
        raise AssertionError(request.url.path)

    client = SentinelClient(
        SentinelConfig("http://sentinel:8081", frozenset({1})),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SentinelAuthError) as exc:
        await client.authenticate("other@example.com", "test-password")
    assert exc.value.status_code == 403
    assert logged_out is True


@pytest.mark.asyncio
async def test_authenticate_maps_bad_credentials_without_exposing_sentinel_body():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "upstream detail that should not leak"})

    client = SentinelClient(
        SentinelConfig("http://sentinel:8081", frozenset({1})),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SentinelAuthError) as exc:
        await client.authenticate("owner@example.com", "wrong")
    assert exc.value.status_code == 401
    assert exc.value.detail == "invalid email or password"


@pytest.mark.parametrize("payload", [{"valid": True}, {"valid": True, "accountId": "secret-invalid-id"},
                                    {"valid": False}, ["secret-invalid-payload"]])
async def test_invalid_validation_is_sanitized_and_logs_out(payload, caplog):
    logged_out = False

    async def handler(request):
        nonlocal logged_out
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"id": 1, "email": "owner@example.com", "token": "private-token"})
        if request.url.path == "/auth/validate":
            return httpx.Response(200, json=payload)
        logged_out = True
        return httpx.Response(200, json={})

    client = SentinelClient(SentinelConfig("http://sentinel:8081", frozenset({1})),
                            transport=httpx.MockTransport(handler))
    with pytest.raises(SentinelAuthError) as exc:
        await client.authenticate("owner@example.com", "private-password")
    assert exc.value.status_code == 502
    assert logged_out
    assert "secret-invalid" not in str(exc.value) + caplog.text
    assert "private-token" not in str(exc.value) + caplog.text
    assert "private-password" not in str(exc.value) + caplog.text


async def test_unavailable_sentinel_does_not_expose_transport_details():
    async def handler(request):
        raise httpx.ConnectError("private-upstream-detail", request=request)

    client = SentinelClient(SentinelConfig("http://sentinel:8081", frozenset({1})),
                            transport=httpx.MockTransport(handler))
    with pytest.raises(SentinelAuthError) as exc:
        await client.authenticate("owner@example.com", "private-password")
    assert exc.value.status_code == 502
    assert str(exc.value) == "Sentinel authentication is unavailable"
