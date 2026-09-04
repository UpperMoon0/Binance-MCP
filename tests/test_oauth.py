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
