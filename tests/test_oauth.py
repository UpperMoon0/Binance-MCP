import os
from pathlib import Path

import pytest

from binance_mcp.oauth import OAuthConfig, OAuthManager, _pkce


def test_requires_https_outside_loopback(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_OWNER_TOKEN", "x" * 32)
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://example.com")
    with pytest.raises(ValueError):
        OAuthConfig.from_env()


def test_owner_rotation_invalidates_persisted_grants(tmp_path: Path):
    path = tmp_path / "oauth.json"
    cfg1 = OAuthConfig("https://example.test", "https://example.test/mcp/", "a" * 32, str(path))
    manager1 = OAuthManager(cfg1)
    token = manager1._issue("client", "mcp")["access_token"]
    manager1._save()
    assert manager1.valid_access_token(token)

    cfg2 = OAuthConfig("https://example.test", "https://example.test/mcp/", "b" * 32, str(path))
    manager2 = OAuthManager(cfg2)
    assert not manager2.valid_access_token(token)


def test_pkce_s256_shape():
    verifier = "A" * 43
    challenge = _pkce(verifier)
    assert len(challenge) == 43
    assert "=" not in challenge


def test_chatgpt_oauth_discovery_metadata_and_challenge(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from binance_mcp.oauth import MCPAuth

    issuer = "https://binance.example.test"
    manager = OAuthManager(OAuthConfig(issuer, f"{issuer}/mcp/", "x" * 32, str(tmp_path / "oauth.json")))

    metadata_app = FastAPI()
    metadata_app.include_router(manager.router)
    metadata_client = TestClient(metadata_app)

    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource/mcp/",
    ):
        response = metadata_client.get(path, follow_redirects=False)
        assert response.status_code == 200
        assert response.json()["resource"] == f"{issuer}/mcp/"
        assert response.json()["authorization_servers"] == [issuer]
        assert response.json()["bearer_methods_supported"] == ["header"]

    authorization_metadata = metadata_client.get("/.well-known/oauth-authorization-server").json()
    assert authorization_metadata["authorization_endpoint"] == f"{issuer}/oauth/authorize"
    assert authorization_metadata["token_endpoint"] == f"{issuer}/oauth/token"
    assert authorization_metadata["registration_endpoint"] == f"{issuer}/oauth/register"
    assert authorization_metadata["authorization_response_iss_parameter_supported"] is True
    assert "offline_access" in authorization_metadata["scopes_supported"]

    async def protected_app(scope, receive, send):
        raise AssertionError("unauthorized request must not reach MCP")

    protected_root = FastAPI()
    protected_root.mount("/mcp", MCPAuth(protected_app, manager))
    denied = TestClient(protected_root).get("/mcp/")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{issuer}/.well-known/oauth-protected-resource/mcp", scope="mcp"'
    )
    assert denied.json()["error"] == "invalid_token"
