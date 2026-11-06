import base64
import hashlib
import json
import re
from contextlib import asynccontextmanager

import httpx
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from binance_mcp.oauth import MCPAuth, OAuthConfig, _hash, _pkce
from binance_mcp.oauth_server import OAuthManager
from binance_mcp.sentinel_auth import SentinelAuthError, SentinelIdentity

ISSUER = "https://minecraft-mcp.example.test"
RESOURCE = f"{ISSUER}/mcp/"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
OWNER_TOKEN = "owner-approval-token-0123456789abcdef"
VERIFIER = "a" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture(autouse=True)
def sentinel(monkeypatch):
    monkeypatch.setenv("MCP_SENTINEL_URL", "http://sentinel:8081")
    monkeypatch.setenv("MCP_SENTINEL_ALLOWED_ACCOUNT_IDS", "1")

    async def authenticate(email, password):
        if email != "owner@example.com" or password != "test-password":
            raise SentinelAuthError(401, "invalid email or password")
        return SentinelIdentity(1, email)

    monkeypatch.setattr("binance_mcp.oauth.authenticate_from_env", authenticate)


def make_client(tmp_path: Path) -> tuple[OAuthManager, TestClient]:
    manager = OAuthManager(OAuthConfig(ISSUER, RESOURCE, OWNER_TOKEN, str(tmp_path / "oauth.json")))
    app = FastAPI()
    app.include_router(manager.router)
    return manager, TestClient(app, base_url=ISSUER)


def start(client, registration, scope="mcp offline_access", **overrides):
    params = dict(response_type="code", client_id=registration["client_id"], redirect_uri=REDIRECT,
                  scope=scope, code_challenge=CHALLENGE, code_challenge_method="S256",
                  resource=RESOURCE, state="oauth_s_example")
    page = client.get("/oauth/authorize", params={**params, **overrides})
    assert page.status_code == 200, page.text
    assert "Owner approval token" not in page.text
    assert "Sign in with Sentinel" in page.text
    return re.search(r"name='authorization_id' value='([^']+)'", page.text)[1]


def approve(client, authorization_id, **overrides):
    return client.post("/oauth/authorize", data={"authorization_id": authorization_id,
                       "email": "owner@example.com", "password": "test-password", **overrides},
                       follow_redirects=False)


def exchange(client, registration, code, **overrides):
    return client.post("/oauth/token", data={"grant_type": "authorization_code",
                       "client_id": registration["client_id"], "code": code,
                       "redirect_uri": REDIRECT, "code_verifier": VERIFIER, **overrides})


def connect(client, registration, scope="mcp offline_access"):
    approved = approve(client, start(client, registration, scope))
    assert approved.status_code == 303
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
    response = exchange(client, registration, code)
    assert response.status_code == 200, response.text
    return response.json()


def refresh(client, registration, token, **overrides):
    return client.post("/oauth/token", data={"grant_type": "refresh_token",
                       "client_id": registration["client_id"], "refresh_token": token, **overrides})


def register(client: TestClient) -> dict:
    response = client.post(
        "/oauth/register",
        json={
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "ChatGPT",
            "application_type": "web",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_requires_https_outside_loopback(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_OWNER_TOKEN", "x" * 32)
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://example.com")
    with pytest.raises(ValueError):
        OAuthConfig.from_env()


def test_pkce_s256_shape():
    challenge = _pkce("A" * 43)
    assert len(challenge) == 43
    assert "=" not in challenge


def test_discovery_and_unauthorized_challenge(tmp_path: Path):
    manager, client = make_client(tmp_path)
    metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"] == RESOURCE
    assert metadata["authorization_servers"] == [ISSUER]
    auth_metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert auth_metadata["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
    assert auth_metadata["token_endpoint"] == f"{ISSUER}/oauth/token"
    assert auth_metadata["registration_endpoint"] == f"{ISSUER}/oauth/register"

    async def protected_app(scope, receive, send):
        raise AssertionError("unauthorized request must not reach MCP")

    root = FastAPI()
    root.mount("/mcp", MCPAuth(protected_app, manager))
    denied = TestClient(root).get("/mcp/")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp", scope="mcp"'
    )


def test_chatgpt_redirect_and_pkce_token_flow(tmp_path: Path):
    manager, client = make_client(tmp_path)
    registration = register(client)
    assert registration["client_id"].startswith("binance_")

    params = {
        "response_type": "code",
        "client_id": registration["client_id"],
        "redirect_uri": REDIRECT,
        "scope": "mcp offline_access",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
        "state": "oauth_s_example",
        "ui_locales": "en-US",
    }
    page = client.get("/oauth/authorize", params=params)
    assert page.status_code == 200, page.text
    assert "Authorize Binance MCP" in page.text
    assert "form-action 'self' https://chatgpt.com" in page.headers["content-security-policy"]

    authorization_id = re.search(r"name='authorization_id' value='([^']+)'", page.text)[1]
    approved = approve(client, authorization_id)
    assert approved.status_code == 303, approved.text
    callback = urlparse(approved.headers["location"])
    assert callback.scheme == "https"
    assert callback.netloc == "chatgpt.com"
    assert callback.path == "/connector_platform_oauth_redirect"
    query = parse_qs(callback.query)
    assert query["state"] == ["oauth_s_example"]
    assert query["iss"] == [ISSUER]

    tokens = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registration["client_id"],
            "code": query["code"][0],
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "resource": RESOURCE,
        },
    )
    assert tokens.status_code == 200, tokens.text
    payload = tokens.json()
    assert payload["token_type"] == "Bearer"
    assert manager.valid_access_token(payload["access_token"])

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registration["client_id"],
            "refresh_token": payload["refresh_token"],
            "resource": RESOURCE,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["refresh_token"] != payload["refresh_token"]


@pytest.mark.parametrize("bad", [dict(password="wrong"), dict(email="denied@example.com")])
def test_failed_login_can_retry_without_leaking_credentials(tmp_path, bad):
    manager, client = make_client(tmp_path)
    registration = register(client)
    authorization_id = start(client, registration)
    denied = approve(client, authorization_id, **bad)
    assert denied.status_code == 401
    assert "wrong" not in denied.text and "denied@example.com" not in denied.text
    assert not manager.codes and not manager.sessions
    assert approve(client, authorization_id).status_code == 303
    assert approve(client, authorization_id).status_code == 400


def test_browser_binding_origin_expiry_and_parameter_tampering(tmp_path):
    manager, client = make_client(tmp_path)
    registration = register(client)
    authorization_id = start(client, registration)
    _, stranger = make_client(tmp_path / "stranger")
    assert approve(stranger, authorization_id).status_code == 400
    denied = client.post("/oauth/authorize", data={"authorization_id": authorization_id},
                         headers={"Origin": "https://evil.test"})
    assert denied.status_code == 403
    assert approve(client, "invented").status_code == 400
    approved = approve(client, authorization_id, redirect_uri="https://evil.test", scope="admin")
    assert approved.status_code == 303
    assert approved.headers["location"].startswith(REDIRECT)
    authorization_id = start(client, registration)
    manager.pending[_hash(authorization_id)]["expires"] = 0
    assert approve(client, authorization_id).status_code == 400
    # The previous owner-key form is no longer a supported sign-in path.
    assert client.post("/oauth/authorize", data={"owner_token": OWNER_TOKEN}).status_code == 400


def test_browser_form_referrer_policy_preserves_origin_on_post(tmp_path):
    _, client = make_client(tmp_path)
    registration = register(client)
    authorization_id = start(client, registration)
    # A real browser navigation form POST sends Origin: null under no-referrer.
    # The form must retain its same-origin Origin, without leaking the OAuth
    # query string to the cross-origin callback. Real Edge reproduction also
    # verifies this browser behavior; TestClient does not implement it.
    page = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": registration["client_id"], "redirect_uri": REDIRECT,
        "scope": "mcp", "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
    assert page.headers["referrer-policy"] == "same-origin"
    authorization_id = re.search(r"name='authorization_id' value='([^']+)'", page.text)[1]
    fields = {"authorization_id": authorization_id, "email": "owner@example.com", "password": "test-password"}
    for headers in ({"Origin": "null", "Sec-Fetch-Site": "same-origin"},
                    {"Origin": "https://evil.test", "Sec-Fetch-Site": "same-origin"},
                    {"Origin": ISSUER, "Sec-Fetch-Site": "cross-site"},
                    {"Origin": ISSUER, "Sec-Fetch-Site": "same-site"}):
        assert client.post("/oauth/authorize", data=fields, headers=headers).status_code == 403
    denied = client.post("/oauth/authorize", data={**fields, "password": "wrong"},
                         headers={"Origin": ISSUER, "Sec-Fetch-Site": "same-origin"})
    assert denied.status_code == 401
    assert denied.headers["referrer-policy"] == "same-origin"
    approved = client.post("/oauth/authorize", data=fields,
                           headers={"Origin": ISSUER, "Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert approved.status_code == 303
    assert approved.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("overrides,error", [
    ({"scope": "unknown"}, "invalid_scope"),
    ({"scope": "mcp offline_access extra"}, "invalid_scope"),
    ({"resource": "https://evil.test/mcp/"}, "invalid_target"),
])
def test_bad_refresh_does_not_consume_valid_token(tmp_path, overrides, error):
    manager, client = make_client(tmp_path)
    registration = register(client)
    tokens = connect(client, registration)
    denied = refresh(client, registration, tokens["refresh_token"], **overrides)
    assert denied.status_code == 400 and denied.json()["error"] == error
    assert refresh(client, registration, tokens["refresh_token"]).status_code == 200


def test_wrong_client_cannot_consume_code_refresh_or_revoke(tmp_path):
    manager, client = make_client(tmp_path)
    registration, other = register(client), register(client)
    approval = approve(client, start(client, registration))
    code = parse_qs(urlparse(approval.headers["location"]).query)["code"][0]
    assert exchange(client, other, code).status_code == 400
    assert exchange(client, registration, code, resource="https://wrong.test").status_code == 400
    assert exchange(client, registration, code, code_verifier="b" * 64).status_code == 400
    tokens = exchange(client, registration, code).json()
    assert exchange(client, registration, code).status_code == 400
    assert refresh(client, other, tokens["refresh_token"]).status_code == 400
    assert client.post("/oauth/revoke", data={"client_id": other["client_id"],
                       "token": tokens["refresh_token"]}).status_code == 200
    assert manager.valid_access_token(tokens["access_token"])
    assert refresh(client, registration, tokens["refresh_token"]).status_code == 200


def test_access_expiry_renews_and_refresh_expiry_requires_reconnect(tmp_path):
    manager, client = make_client(tmp_path)
    registration = register(client)
    tokens = connect(client, registration)
    manager.access[_hash(tokens["access_token"])]["expires"] = 0
    assert not manager.valid_access_token(tokens["access_token"])
    renewed = refresh(client, registration, tokens["refresh_token"])
    assert renewed.status_code == 200
    assert refresh(client, registration, tokens["refresh_token"]).status_code == 400
    manager.refresh[_hash(renewed.json()["refresh_token"])]["expires"] = 0
    assert refresh(client, registration, renewed.json()["refresh_token"]).status_code == 400
    assert manager.valid_access_token(connect(client, registration)["access_token"])


@pytest.mark.parametrize("kind", ["access_token", "refresh_token", "rotated_refresh"])
def test_revocation_invalidates_entire_session_and_survives_restart(tmp_path, kind):
    manager, client = make_client(tmp_path)
    registration = register(client)
    tokens = connect(client, registration)
    other_session = connect(client, registration)
    renewed = refresh(client, registration, tokens["refresh_token"]).json()
    token = tokens["refresh_token"] if kind == "rotated_refresh" else renewed[kind]
    revoked = client.post("/oauth/revoke", data={"client_id": registration["client_id"], "token": token})
    assert revoked.status_code == 200
    assert not manager.valid_access_token(tokens["access_token"])
    assert not manager.valid_access_token(renewed["access_token"])
    assert refresh(client, registration, renewed["refresh_token"]).status_code == 400
    manager, client = make_client(tmp_path)
    assert not manager.valid_access_token(renewed["access_token"])
    assert manager.valid_access_token(other_session["access_token"])
    assert manager.valid_access_token(connect(client, registration)["access_token"])


def test_restart_preserves_client_pending_code_and_rotating_tokens(tmp_path):
    manager, client = make_client(tmp_path)
    registration = register(client)
    approval = approve(client, start(client, registration))
    code = parse_qs(urlparse(approval.headers["location"]).query)["code"][0]
    manager, client = make_client(tmp_path)
    tokens = exchange(client, registration, code).json()
    manager, client = make_client(tmp_path)
    assert manager.valid_access_token(tokens["access_token"])
    renewed = refresh(client, registration, tokens["refresh_token"])
    assert renewed.status_code == 200
    state = Path(manager.config.state_path).read_text()
    for secret in ("test-password", "owner@example.com", code, tokens["access_token"],
                   tokens["refresh_token"], renewed.json()["refresh_token"]):
        assert secret not in state


def test_allowlist_removal_and_legacy_grants_require_signin(tmp_path, monkeypatch):
    manager, client = make_client(tmp_path)
    registration = register(client)
    tokens = connect(client, registration)
    monkeypatch.setenv("MCP_SENTINEL_ALLOWED_ACCOUNT_IDS", "2")
    assert not manager.valid_access_token(tokens["access_token"])
    assert refresh(client, registration, tokens["refresh_token"]).status_code == 400
    monkeypatch.setenv("MCP_SENTINEL_ALLOWED_ACCOUNT_IDS", "1")
    manager.access[_hash("legacy")] = {"client_id": registration["client_id"], "scope": "mcp", "expires": 9999999999}
    manager._save()
    manager, client = make_client(tmp_path)
    assert not manager.valid_access_token("legacy")
    assert manager.valid_access_token(connect(client, registration)["access_token"])


def test_existing_mcp_only_clients_renew_without_expanding_scopes(tmp_path):
    manager, client = make_client(tmp_path)
    registration = register(client)
    mcp_tokens = connect(client, registration, "mcp")
    assert mcp_tokens["scope"] == "mcp" and mcp_tokens["refresh_token"]
    renewed = refresh(client, registration, mcp_tokens["refresh_token"])
    assert renewed.status_code == 200 and renewed.json()["scope"] == "mcp"
    assert refresh(client, registration, renewed.json()["refresh_token"], scope="mcp offline_access").json()["error"] == "invalid_scope"
    tokens = connect(client, registration)
    narrowed = refresh(client, registration, tokens["refresh_token"], scope="mcp")
    assert narrowed.status_code == 200
    assert narrowed.json()["scope"] == "mcp"
    assert narrowed.json()["refresh_token"]


def test_expired_authorization_code_and_unavailable_signin(tmp_path, monkeypatch):
    manager, client = make_client(tmp_path)
    registration = register(client)
    approval = approve(client, start(client, registration))
    code = parse_qs(urlparse(approval.headers["location"]).query)["code"][0]
    manager.codes[_hash(code)]["expires"] = 0
    assert exchange(client, registration, code).status_code == 400
    assert not manager.sessions

    async def unavailable(email, password):
        raise SentinelAuthError(502, "Sentinel authentication is unavailable")

    monkeypatch.setattr("binance_mcp.oauth.authenticate_from_env", unavailable)
    denied = approve(client, start(client, registration))
    assert denied.status_code == 502
    assert "test-password" not in denied.text
    assert "Sign in with Sentinel" in denied.text


def test_malformed_and_duplicate_forms_are_rejected(tmp_path):
    _, client = make_client(tmp_path)
    for endpoint in ("/oauth/authorize", "/oauth/token", "/oauth/revoke"):
        for body in (b"\xff", b"x=" + b"a" * 17000, b"client_id=first&client_id=second"):
            assert client.post(endpoint, content=body).status_code == 400


def test_concurrent_refresh_only_issues_one_replacement(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    manager, client = make_client(tmp_path)
    registration = register(client)
    tokens = connect(client, registration)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: refresh(client, registration, tokens["refresh_token"]), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 400]
    replacement = next(r.json() for r in responses if r.status_code == 200)
    assert refresh(client, registration, replacement["refresh_token"]).status_code == 200


@pytest.mark.parametrize("field,value", [("scope", "admin"), ("resource", "https://evil.test"),
                                        ("redirect_uri", "https://evil.test"), ("code_challenge_method", "plain")])
def test_invalid_authorization_never_shows_signin(tmp_path, field, value):
    manager, client = make_client(tmp_path)
    registration = register(client)
    params = dict(response_type="code", client_id=registration["client_id"], redirect_uri=REDIRECT,
                  scope="mcp offline_access", code_challenge=CHALLENGE, code_challenge_method="S256", resource=RESOURCE)
    denied = client.get("/oauth/authorize", params={**params, field: value})
    assert denied.status_code == 400
    assert not manager.pending


@pytest.mark.parametrize("auth_method", ["client_secret_basic", "client_secret_post"])
def test_confidential_clients_refresh_and_revoke(tmp_path, auth_method):
    manager, client = make_client(tmp_path)
    registration = client.post("/oauth/register", json={"redirect_uris": [REDIRECT],
                               "token_endpoint_auth_method": auth_method}).json()
    approval = approve(client, start(client, registration))
    code = parse_qs(urlparse(approval.headers["location"]).query)["code"][0]
    data = {"client_id": registration["client_id"]}
    kwargs = {}
    if auth_method == "client_secret_basic":
        kwargs["auth"] = (registration["client_id"], registration["client_secret"])
    else:
        data["client_secret"] = registration["client_secret"]
    response = client.post("/oauth/token", data={**data, "grant_type": "authorization_code", "code": code,
                           "redirect_uri": REDIRECT, "code_verifier": VERIFIER}, **kwargs)
    assert response.status_code == 200
    renewed = client.post("/oauth/token", data={**data, "grant_type": "refresh_token",
                          "refresh_token": response.json()["refresh_token"]}, **kwargs)
    assert renewed.status_code == 200
    assert client.post("/oauth/revoke", data={**data, "token": renewed.json()["refresh_token"]}, **kwargs).status_code == 200
    assert not manager.valid_access_token(renewed.json()["access_token"])




def test_sentinel_configuration_requires_no_owner_secret(monkeypatch):
    monkeypatch.delenv("MCP_OAUTH_OWNER_TOKEN", raising=False)
    monkeypatch.setenv("MCP_PUBLIC_URL", ISSUER)
    monkeypatch.setenv("MCP_SENTINEL_URL", "https://sentinel.example.test")
    monkeypatch.setenv("MCP_SENTINEL_ALLOWED_ACCOUNT_IDS", "1")
    cfg = OAuthConfig.from_env()
    assert cfg.issuer == ISSUER
    binding = cfg.state_binding
    monkeypatch.setenv("MCP_OAUTH_OWNER_TOKEN", "obsolete-secret-must-be-ignored")
    assert OAuthConfig.from_env().state_binding == binding
