import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from binance_mcp.oauth import OAuthConfig
from binance_mcp.oauth_server import OAuthManager


ISSUER = "https://binance.example.test"
RESOURCE = f"{ISSUER}/mcp/"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
OWNER_TOKEN = "owner-approval-token-0123456789abcdef"
VERIFIER = "c" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


def make_client(tmp_path: Path) -> TestClient:
    manager = OAuthManager(OAuthConfig(ISSUER, RESOURCE, OWNER_TOKEN, str(tmp_path / "oauth.json")))
    app = FastAPI()
    app.include_router(manager.router)
    return TestClient(app)


def test_chatgpt_authorize_form_allows_callback_origin_and_redirects(tmp_path: Path):
    client = make_client(tmp_path)
    registration = client.post(
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
    assert registration.status_code == 201, registration.text
    client_id = registration.json()["client_id"]

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "scope": "mcp",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
        "state": "oauth_s_example",
        "ui_locales": "en-US",
    }
    page = client.get("/oauth/authorize", params=params)
    assert page.status_code == 200, page.text
    assert "form-action 'self' https://chatgpt.com" in page.headers["content-security-policy"]
    assert f'name="redirect_uri" value="{REDIRECT}"' in page.text

    params.pop("ui_locales")
    approved = client.post(
        "/oauth/authorize",
        data={**params, "owner_token": OWNER_TOKEN},
        follow_redirects=False,
    )
    assert approved.status_code == 303, approved.text

    callback = urlparse(approved.headers["location"])
    assert callback.scheme == "https"
    assert callback.netloc == "chatgpt.com"
    assert callback.path == "/connector_platform_oauth_redirect"
    query = parse_qs(callback.query)
    assert query["code"][0]
    assert query["state"] == ["oauth_s_example"]
    assert query["iss"] == [ISSUER]
