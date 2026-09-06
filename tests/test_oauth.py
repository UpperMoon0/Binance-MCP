import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from binance_mcp.oauth import MCPAuth, OAuthConfig, OAuthManager, _pkce


ISSUER = "https://binance.example.test"
RESOURCE = f"{ISSUER}/mcp/"
REDIRECT = "https://chatgpt.com/connector/oauth/binance-test"
OWNER_TOKEN = "owner-approval-token-0123456789abcdef"
VERIFIER = "a" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


def make_client(tmp_path: Path) -> tuple[OAuthManager, TestClient]:
    manager = OAuthManager(OAuthConfig(ISSUER, RESOURCE, OWNER_TOKEN, str(tmp_path / "oauth.json")))
    app = FastAPI()
    app.include_router(manager.router)
    return manager, TestClient(app)


def register(client: TestClient, auth_method: str = "none") -> dict:
    response = client.post(
        "/oauth/register",
        json={
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": auth_method,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "ChatGPT",
            "application_type": "web",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def authorize(client: TestClient, client_id: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "resource": RESOURCE,
        "state": "state-123",
        "scope": "mcp offline_access",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
    }
    page = client.get("/oauth/authorize", params=params)
    assert page.status_code == 200, page.text
    assert "Authorize Binance MCP" in page.text
    assert 'name="code_challenge"' in page.text
    assert f'value="{CHALLENGE}"' in page.text
    assert 'name="code_challenge_method" value="S256"' in page.text or "name='code_challenge_method' value='S256'" in page.text

    response = client.post(
        "/oauth/authorize",
        data={**params, "owner_token": OWNER_TOKEN},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    callback = urlparse(response.headers["location"])
    query = parse_qs(callback.query)
    assert query["state"] == ["state-123"]
    assert query["iss"] == [ISSUER]
    return query["code"][0]


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
    manager, metadata_client = make_client(tmp_path)

    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource/mcp/",
    ):
        response = metadata_client.get(path, follow_redirects=False)
        assert response.status_code == 200
        assert response.json()["resource"] == RESOURCE
        assert response.json()["authorization_servers"] == [ISSUER]
        assert response.json()["bearer_methods_supported"] == ["header"]

    authorization_metadata = metadata_client.get("/.well-known/oauth-authorization-server").json()
    assert authorization_metadata["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
    assert authorization_metadata["token_endpoint"] == f"{ISSUER}/oauth/token"
    assert authorization_metadata["registration_endpoint"] == f"{ISSUER}/oauth/register"
    assert authorization_metadata["authorization_response_iss_parameter_supported"] is True
    assert "offline_access" in authorization_metadata["scopes_supported"]
    assert set(authorization_metadata["token_endpoint_auth_methods_supported"]) == {
        "none",
        "client_secret_basic",
        "client_secret_post",
    }

    async def protected_app(scope, receive, send):
        raise AssertionError("unauthorized request must not reach MCP")

    protected_root = FastAPI()
    protected_root.mount("/mcp", MCPAuth(protected_app, manager))
    denied = TestClient(protected_root).get("/mcp/")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp", scope="mcp"'
    )
    assert denied.json()["error"] == "invalid_token"


def test_chatgpt_dynamic_registration_honors_client_auth_methods(tmp_path: Path):
    _manager, client = make_client(tmp_path)

    public = register(client, "none")
    assert public["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in public

    for method in ("client_secret_basic", "client_secret_post"):
        confidential = register(client, method)
        assert confidential["token_endpoint_auth_method"] == method
        assert confidential["client_name"] == "ChatGPT"
        assert confidential["client_secret"]
        assert confidential["client_secret_expires_at"] == 0


def test_chatgpt_confidential_client_authorization_code_and_refresh_flow(tmp_path: Path):
    manager, client = make_client(tmp_path)
    registration = register(client, "client_secret_basic")
    code = authorize(client, registration["client_id"])

    token_response = client.post(
        "/oauth/token",
        auth=(registration["client_id"], registration["client_secret"]),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "resource": RESOURCE,
        },
    )
    assert token_response.status_code == 200, token_response.text
    tokens = token_response.json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["scope"] == "mcp offline_access"
    assert manager.valid_access_token(tokens["access_token"])

    refreshed = client.post(
        "/oauth/token",
        auth=(registration["client_id"], registration["client_secret"]),
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "resource": RESOURCE,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    refreshed_tokens = refreshed.json()
    assert refreshed_tokens["refresh_token"] != tokens["refresh_token"]
    assert manager.valid_access_token(refreshed_tokens["access_token"])

    reused = client.post(
        "/oauth/token",
        auth=(registration["client_id"], registration["client_secret"]),
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "resource": RESOURCE,
        },
    )
    assert reused.status_code == 400
    assert reused.json()["error"] == "invalid_grant"


def test_chatgpt_client_secret_post_and_resource_binding(tmp_path: Path):
    _manager, client = make_client(tmp_path)
    registration = register(client, "client_secret_post")
    code = authorize(client, registration["client_id"])

    wrong_resource = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "resource": "https://other.example.test/mcp/",
        },
    )
    assert wrong_resource.status_code == 400
    assert wrong_resource.json()["error"] == "invalid_target"
