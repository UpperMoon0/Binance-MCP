from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

SCOPES = {"mcp", "offline_access"}
AUTH_METHODS = {"none", "client_secret_basic", "client_secret_post"}
GRANT_TYPES = {"authorization_code", "refresh_token"}
RESPONSE_TYPES = {"code"}
CODE_TTL = 300
MAX_CLIENTS = 256
PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


def _token(n: int = 32) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(n)).rstrip(b"=").decode()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _ct(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _pkce(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def _secure_url(raw: str, *, redirect: bool = False) -> str:
    raw = raw.strip().rstrip("/" if not redirect else "")
    p = urlparse(raw)
    if not p.scheme or not p.netloc or p.username or p.password or p.fragment:
        raise ValueError("URL must be absolute and contain no userinfo or fragment")
    if p.scheme == "https":
        return raw
    host = p.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if p.scheme == "http" and loopback:
        return raw
    raise ValueError("HTTPS is required outside loopback development")


def _scope(raw: str) -> str:
    values = raw.split() or ["mcp"]
    if any(v not in SCOPES for v in values):
        raise ValueError("unsupported scope")
    if "mcp" not in values:
        values.insert(0, "mcp")
    return " ".join(dict.fromkeys(values))


@dataclass(slots=True)
class OAuthConfig:
    issuer: str
    resource: str
    owner_token: str
    state_path: str
    access_ttl: int = 3600
    refresh_ttl: int = 30 * 86400

    @classmethod
    def from_env(cls) -> "OAuthConfig | None":
        owner = os.getenv("MCP_OAUTH_OWNER_TOKEN", "").strip()
        public = os.getenv("MCP_PUBLIC_URL", "").strip()
        if not owner and not public:
            return None
        if not owner or not public:
            raise ValueError("MCP_OAUTH_OWNER_TOKEN and MCP_PUBLIC_URL must be configured together")
        if len(owner) < 32:
            raise ValueError("MCP_OAUTH_OWNER_TOKEN must be at least 32 characters")
        issuer = _secure_url(public)
        return cls(
            issuer=issuer,
            resource=f"{issuer}/mcp/",
            owner_token=owner,
            state_path=os.getenv("MCP_OAUTH_STATE", "/app/data/oauth-state.json"),
            access_ttl=int(os.getenv("MCP_OAUTH_ACCESS_TTL_SECONDS", "3600")),
            refresh_ttl=int(os.getenv("MCP_OAUTH_REFRESH_TTL_SECONDS", str(30 * 86400))),
        )


class OAuthManager:
    def __init__(self, config: OAuthConfig):
        self.config = config
        self.router = APIRouter()
        self._lock = threading.RLock()
        self.clients: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.access: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, dict[str, Any]] = {}
        self._load()
        self._routes()

    @classmethod
    def from_env(cls) -> "OAuthManager | None":
        cfg = OAuthConfig.from_env()
        return cls(cfg) if cfg else None

    def _routes(self) -> None:
        self.router.add_api_route("/.well-known/oauth-protected-resource", self.protected_metadata, methods=["GET"], response_model=None)
        self.router.add_api_route("/.well-known/oauth-protected-resource/mcp", self.protected_metadata, methods=["GET"], response_model=None)
        self.router.add_api_route("/.well-known/oauth-protected-resource/mcp/", self.protected_metadata, methods=["GET"], response_model=None)
        self.router.add_api_route("/.well-known/oauth-authorization-server", self.server_metadata, methods=["GET"], response_model=None)
        self.router.add_api_route("/oauth/register", self.register, methods=["POST"], response_model=None)
        self.router.add_api_route("/oauth/authorize", self.authorize_get, methods=["GET"], response_model=None)
        self.router.add_api_route("/oauth/authorize", self.authorize_post, methods=["POST"], response_model=None)
        self.router.add_api_route("/oauth/token", self.token, methods=["POST"], response_model=None)

    async def protected_metadata(self) -> JSONResponse:
        return JSONResponse(
            {
                "resource": self.config.resource,
                "authorization_servers": [self.config.issuer],
                "bearer_methods_supported": ["header"],
                "scopes_supported": sorted(SCOPES),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def server_metadata(self) -> JSONResponse:
        i = self.config.issuer
        return JSONResponse(
            {
                "issuer": i,
                "authorization_endpoint": f"{i}/oauth/authorize",
                "token_endpoint": f"{i}/oauth/token",
                "registration_endpoint": f"{i}/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": sorted(AUTH_METHODS),
                "scopes_supported": sorted(SCOPES),
                "authorization_response_iss_parameter_supported": True,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def register(self, request: Request) -> JSONResponse:
        raw = await request.body()
        if len(raw) > 65536:
            return self._error(400, "invalid_client_metadata", "registration document too large")
        try:
            payload = json.loads(raw or b"{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return self._error(400, "invalid_client_metadata", "invalid client registration document")
        if not isinstance(payload, dict):
            return self._error(400, "invalid_client_metadata", "invalid client registration document")

        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not 1 <= len(redirect_uris) <= 10:
            return self._error(400, "invalid_redirect_uri", "one to ten redirect_uris are required")
        redirects: list[str] = []
        try:
            for raw_redirect in redirect_uris:
                if not isinstance(raw_redirect, str):
                    raise ValueError("redirect_uri must be a string")
                redirect = _secure_url(raw_redirect, redirect=True)
                if redirect not in redirects:
                    redirects.append(redirect)
        except ValueError as exc:
            return self._error(400, "invalid_redirect_uri", str(exc))

        grant_types = payload.get("grant_types") or []
        response_types = payload.get("response_types") or []
        if (
            not isinstance(grant_types, list)
            or not all(isinstance(value, str) for value in grant_types)
            or not set(grant_types).issubset(GRANT_TYPES)
        ):
            return self._error(400, "invalid_client_metadata", "unsupported grant type")
        if (
            not isinstance(response_types, list)
            or not all(isinstance(value, str) for value in response_types)
            or not set(response_types).issubset(RESPONSE_TYPES)
        ):
            return self._error(400, "invalid_client_metadata", "unsupported response type")
        application_type = str(payload.get("application_type") or "").strip()
        if application_type not in {"", "web", "native"}:
            return self._error(400, "invalid_client_metadata", "unsupported application_type")
        auth_method = str(payload.get("token_endpoint_auth_method") or "none").strip()
        if auth_method not in AUTH_METHODS:
            return self._error(400, "invalid_client_metadata", "unsupported token_endpoint_auth_method")

        client_id = f"binance_{_token(24)}"
        client_secret = _token(32) if auth_method != "none" else ""
        now = int(time.time())
        client = {
            "redirect_uris": redirects,
            "name": str(payload.get("client_name") or "").strip()[:200],
            "application_type": application_type,
            "auth_method": auth_method,
            "secret_hash": _hash(client_secret) if client_secret else "",
            "created": now,
            "last_used": now,
        }
        with self._lock:
            self._cleanup()
            if len(self.clients) >= MAX_CLIENTS:
                return self._error(503, "temporarily_unavailable", "client registration capacity reached")
            self.clients[client_id] = client
            self._save()

        response: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": now,
            "redirect_uris": redirects,
            "token_endpoint_auth_method": auth_method,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": client["name"],
        }
        if client_secret:
            response["client_secret"] = client_secret
            response["client_secret_expires_at"] = 0
        return JSONResponse(response, status_code=201, headers={"Cache-Control": "no-store"})

    def _parse_auth(self, values: dict[str, str]) -> dict[str, str]:
        if values.get("response_type") != "code":
            raise ValueError("response_type must be code")
        client_id = values.get("client_id", "").strip()
        redirect_uri = values.get("redirect_uri", "").strip()
        challenge = values.get("code_challenge", "").strip()
        if not client_id or not redirect_uri or not challenge:
            raise ValueError("client_id, redirect_uri, and code_challenge are required")
        if values.get("code_challenge_method") != "S256" or not PKCE_CHALLENGE_RE.fullmatch(challenge):
            raise ValueError("PKCE S256 is required")
        resource = values.get("resource", "").strip() or self.config.resource
        if resource != self.config.resource:
            raise ValueError("invalid resource")
        scope = _scope(values.get("scope", ""))
        with self._lock:
            client = self.clients.get(client_id)
            if not client or redirect_uri not in client["redirect_uris"]:
                raise ValueError("unknown client or redirect_uri")
            client["last_used"] = int(time.time())
        return {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "challenge": challenge,
            "scope": scope,
            "state": values.get("state", ""),
            "resource": resource,
        }

    async def authorize_get(self, request: Request):
        try:
            values = {k: v for k, v in request.query_params.items()}
            auth = self._parse_auth(values)
        except ValueError as exc:
            return self._error(400, "invalid_request", str(exc))
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v, quote=True)}">'
            for k, v in auth.items()
        )
        action = f"{self.config.issuer}/oauth/authorize"
        body = f"""<!doctype html><html><head><meta charset='utf-8'><title>Authorize Binance MCP</title></head>
<body><main><h1>Authorize Binance MCP</h1><p>Approve this MCP client to access the server. Binance API credentials stay server-side and are never issued to the client.</p>
<form method='post' action='{html.escape(action, quote=True)}'>{hidden}<input type='hidden' name='response_type' value='code'><input type='hidden' name='code_challenge_method' value='S256'><label>Owner approval token <input type='password' name='owner_token' autocomplete='current-password' required></label><button type='submit'>Authorize</button></form></main></body></html>"""
        return HTMLResponse(
            body,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; form-action {action}; frame-ancestors 'none'",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            },
        )

    async def authorize_post(self, request: Request):
        raw = (await request.body()).decode("utf-8")
        values = {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        if not _ct(values.pop("owner_token", ""), self.config.owner_token):
            return self._error(403, "access_denied", "owner approval failed")
        try:
            auth = self._parse_auth(values)
        except ValueError as exc:
            return self._error(400, "invalid_request", str(exc))
        code = _token(32)
        with self._lock:
            self.codes[_hash(code)] = {**auth, "expires": int(time.time()) + CODE_TTL}
            self._save()
        params = {"code": code, "iss": self.config.issuer}
        if auth["state"]:
            params["state"] = auth["state"]
        return RedirectResponse(
            f"{auth['redirect_uri']}?{urlencode(params)}",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    async def token(self, request: Request):
        raw = (await request.body()).decode("utf-8")
        values = {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        client_id, auth_error = self._authenticate_client(request, values)
        if auth_error:
            response = self._error(401, "invalid_client", auth_error)
            response.headers["WWW-Authenticate"] = 'Basic realm="binance-mcp-oauth"'
            return response
        grant = values.get("grant_type")
        if grant == "authorization_code":
            return self._exchange_code(values, client_id)
        if grant == "refresh_token":
            return self._exchange_refresh(values, client_id)
        return self._error(400, "unsupported_grant_type", "unsupported grant_type")

    def _authenticate_client(self, request: Request, values: dict[str, str]) -> tuple[str, str | None]:
        form_id = values.get("client_id", "").strip()
        basic_id = ""
        basic_secret = ""
        has_basic = False
        authorization = request.headers.get("authorization", "")
        if authorization[:6].lower() == "basic ":
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                basic_id, basic_secret = decoded.split(":", 1)
                has_basic = True
            except (ValueError, UnicodeDecodeError, binascii.Error):
                return "", "invalid HTTP Basic credentials"
        client_id = basic_id if has_basic else form_id
        if not client_id:
            return "", "client_id is required"
        with self._lock:
            client = self.clients.get(client_id)
            if not client:
                return "", "unknown client"
            auth_method = client.get("auth_method", "none")
            if auth_method == "none":
                if has_basic or values.get("client_secret", ""):
                    return "", "public client must not send a client secret"
            elif auth_method == "client_secret_basic":
                if not has_basic or not _ct(_hash(basic_secret), client.get("secret_hash", "")):
                    return "", "invalid client credentials"
            elif auth_method == "client_secret_post":
                if has_basic or not _ct(_hash(values.get("client_secret", "")), client.get("secret_hash", "")):
                    return "", "invalid client credentials"
            else:
                return "", "unsupported client authentication method"
            client["last_used"] = int(time.time())
        return client_id, None

    def _exchange_code(self, values: dict[str, str], client_id: str) -> JSONResponse:
        code = values.get("code", "").strip()
        redirect_uri = values.get("redirect_uri", "").strip()
        verifier = values.get("code_verifier", "").strip()
        resource = values.get("resource", "").strip()
        if not code or not redirect_uri or not verifier or not resource:
            return self._error(400, "invalid_request", "code, redirect_uri, code_verifier, and resource are required")
        if not PKCE_VERIFIER_RE.fullmatch(verifier):
            return self._error(400, "invalid_grant", "invalid PKCE code_verifier")
        with self._lock:
            record = self.codes.pop(_hash(code), None)
            if not record or record["expires"] < time.time():
                return self._error(400, "invalid_grant", "authorization code is invalid or expired")
            if client_id != record["client_id"] or redirect_uri != record["redirect_uri"]:
                return self._error(400, "invalid_grant", "client or redirect mismatch")
            if not _ct(_pkce(verifier), record["challenge"]):
                return self._error(400, "invalid_grant", "PKCE verification failed")
            if resource != self.config.resource or resource != record["resource"]:
                return self._error(400, "invalid_target", "resource does not match the authorized MCP server")
            response = self._issue(record["client_id"], record["scope"])
            self._save()
            return JSONResponse(response, headers={"Cache-Control": "no-store"})

    def _exchange_refresh(self, values: dict[str, str], client_id: str) -> JSONResponse:
        presented = values.get("refresh_token", "").strip()
        if not presented:
            return self._error(400, "invalid_request", "refresh_token is required")
        resource = values.get("resource", "").strip()
        if resource and resource != self.config.resource:
            return self._error(400, "invalid_target", "resource does not match this MCP server")
        with self._lock:
            record = self.refresh.pop(_hash(presented), None)
            if not record or record["expires"] < time.time() or client_id != record["client_id"]:
                return self._error(400, "invalid_grant", "refresh token is invalid or expired")
            scope = _scope(values.get("scope", record["scope"]))
            if not set(scope.split()).issubset(set(record["scope"].split())):
                return self._error(400, "invalid_scope", "requested scope exceeds original grant")
            response = self._issue(record["client_id"], scope)
            self._save()
            return JSONResponse(response, headers={"Cache-Control": "no-store"})

    def _issue(self, client_id: str, scope: str) -> dict[str, Any]:
        now = int(time.time())
        access = _token(32)
        refresh = _token(48)
        self.access[_hash(access)] = {"client_id": client_id, "scope": scope, "expires": now + self.config.access_ttl}
        self.refresh[_hash(refresh)] = {"client_id": client_id, "scope": scope, "expires": now + self.config.refresh_ttl}
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.config.access_ttl,
            "scope": scope,
            "refresh_token": refresh,
        }

    def _cleanup(self) -> None:
        now = time.time()
        self.access = {k: v for k, v in self.access.items() if v.get("expires", 0) >= now}
        self.refresh = {k: v for k, v in self.refresh.items() if v.get("expires", 0) >= now}
        self.codes = {k: v for k, v in self.codes.items() if v.get("expires", 0) >= now}

    def valid_access_token(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            record = self.access.get(_hash(token))
            if not record:
                return False
            if record["expires"] < time.time():
                self.access.pop(_hash(token), None)
                self._save()
                return False
            return True

    def unauthorized_response(self) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        metadata = f"{self.config.issuer}/.well-known/oauth-protected-resource/mcp"
        body = b'{"error":"invalid_token","error_description":"a valid OAuth access token is required"}'
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
            (b"www-authenticate", f'Bearer resource_metadata="{metadata}", scope="mcp"'.encode()),
        ]
        return 401, headers, body

    @staticmethod
    def _error(status: int, code: str, description: str) -> JSONResponse:
        return JSONResponse(
            {"error": code, "error_description": description},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    def _load(self) -> None:
        path = Path(self.config.state_path)
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            return
        if raw.get("owner_fingerprint") != _hash(self.config.owner_token):
            return
        self.clients = raw.get("clients", {})
        self.access = raw.get("access", {})
        self.refresh = raw.get("refresh", {})

    def _save(self) -> None:
        path = Path(self.config.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        state = {
            "owner_fingerprint": _hash(self.config.owner_token),
            "clients": self.clients,
            "access": self.access,
            "refresh": self.refresh,
        }
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            path.chmod(0o600)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


class MCPAuth:
    def __init__(self, app: Any, oauth: OAuthManager | None):
        self.app = app
        self.oauth = oauth

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if not self.oauth:
            body = b'{"error":"mcp_auth_not_configured"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        authorization = headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        if self.oauth.valid_access_token(token):
            await self.app(scope, receive, send)
            return
        status, response_headers, body = self.oauth.unauthorized_response()
        await send({"type": "http.response.start", "status": status, "headers": response_headers})
        await send({"type": "http.response.body", "body": body})