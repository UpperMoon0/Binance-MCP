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
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .sentinel_auth import SentinelAuthError, SentinelConfig, authenticate_from_env

SCOPES = {"mcp", "offline_access"}
AUTH_METHODS = {"none", "client_secret_basic", "client_secret_post"}
GRANT_TYPES = {"authorization_code", "refresh_token"}
RESPONSE_TYPES = {"code"}
CODE_TTL = 300
SIGNIN_TTL = 600
SIGNIN_COOKIE = "binance_mcp_authorize"
LOGIN_CSS = Path(__file__).with_name("sentinel-login.css").read_text(encoding="utf-8")
LOGIN_CSS_ETAG = '"' + hashlib.sha256(LOGIN_CSS.encode()).hexdigest() + '"'
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
    state_binding: str
    state_path: str
    access_ttl: int = 3600
    refresh_ttl: int = 30 * 86400

    def __post_init__(self) -> None:
        if self.access_ttl <= 0 or self.refresh_ttl < self.access_ttl:
            raise ValueError("OAuth lifetimes require 0 < access TTL <= refresh TTL")

    @classmethod
    def from_env(cls) -> "OAuthConfig | None":
        public = os.getenv("MCP_PUBLIC_URL", "").strip()
        if not public:
            return None
        issuer = _secure_url(public)
        sentinel = SentinelConfig.from_env()
        binding = "sentinel-v1:" + issuer + ":" + ",".join(map(str, sorted(sentinel.allowed_account_ids)))
        return cls(
            issuer=issuer,
            resource=f"{issuer}/mcp/",
            state_binding=binding,
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
        self.sessions: dict[str, dict[str, Any]] = {}
        self.spent_refresh: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self._load()
        self._routes()

    @classmethod
    def from_env(cls) -> "OAuthManager | None":
        cfg = OAuthConfig.from_env()
        return cls(cfg) if cfg else None

    def _routes(self) -> None:
        self.router.add_api_route("/oauth/login.css", self.login_styles, methods=["GET"], response_model=None)
        self.router.add_api_route("/.well-known/oauth-protected-resource", self.protected_metadata, methods=["GET"], response_model=None)
        self.router.add_api_route("/.well-known/oauth-protected-resource/mcp", self.protected_metadata, methods=["GET"], response_model=None)
        self.router.add_api_route("/.well-known/oauth-protected-resource/mcp/", self.protected_metadata, methods=["GET"], response_model=None)
        self.router.add_api_route("/.well-known/oauth-authorization-server", self.server_metadata, methods=["GET"], response_model=None)
        self.router.add_api_route("/oauth/register", self.register, methods=["POST"], response_model=None)
        self.router.add_api_route("/oauth/authorize", self.authorize_get, methods=["GET"], response_model=None)
        self.router.add_api_route("/oauth/authorize", self.authorize_post, methods=["POST"], response_model=None)
        self.router.add_api_route("/oauth/token", self.token, methods=["POST"], response_model=None)
        self.router.add_api_route("/oauth/revoke", self.revoke, methods=["POST"], response_model=None)

    async def login_styles(self, request: Request) -> Response:
        headers = {"Cache-Control": "public, no-cache", "ETag": LOGIN_CSS_ETAG,
                   "X-Content-Type-Options": "nosniff"}
        if request.headers.get("if-none-match") == LOGIN_CSS_ETAG:
            return Response(status_code=304, headers=headers)
        return Response(LOGIN_CSS, media_type="text/css", headers=headers)

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
                "revocation_endpoint": f"{i}/oauth/revoke",
                "revocation_endpoint_auth_methods_supported": sorted(AUTH_METHODS),
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
        except (TypeError, ValueError):
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
        code_challenge = values.get("code_challenge", "").strip()
        if not client_id or not redirect_uri or not code_challenge:
            raise ValueError("client_id, redirect_uri, and code_challenge are required")
        if values.get("code_challenge_method") != "S256" or not PKCE_CHALLENGE_RE.fullmatch(code_challenge):
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
            "code_challenge": code_challenge,
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
        binding = request.cookies.get(SIGNIN_COOKIE) or _token()
        authorization_id = _token()
        with self._lock:
            self._cleanup()
            if len(self.pending) >= MAX_CLIENTS:
                return self._error(503, "temporarily_unavailable", "sign-in capacity reached")
            self.pending[_hash(authorization_id)] = {
                **auth, "binding": _hash(binding), "expires": int(time.time()) + SIGNIN_TTL,
            }
        return self._signin_response(auth, authorization_id, binding)

    def _signin_response(self, auth, authorization_id, binding, *, error="", status=200):
        with self._lock:
            client_name = self.clients[auth["client_id"]]["name"] or "MCP client"
        action = f"{self.config.issuer}/oauth/authorize"
        callback = urlparse(auth["redirect_uri"])
        callback_origin = f"{callback.scheme}://{callback.netloc}"
        error_html = f"<div class='error-box' role='alert'>{html.escape(error)}</div>" if error else ""
        renewal = (f"{self.config.refresh_ttl // 86400} days" if self.config.refresh_ttl % 86400 == 0
                   else f"{self.config.refresh_ttl} seconds")
        body = f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><meta name='theme-color' content='#090c12'>
<title>Authorize Binance MCP</title><link rel='stylesheet' href='/oauth/login.css'></head>
<body class='sentinel-auth-page'><main class='login-shell'>
<form class='login-card' method='post' action='{html.escape(action, quote=True)}' aria-labelledby='login-title'>
<div class='login-icon' aria-hidden='true'>🛡️</div><h1 id='login-title'>Connect Binance MCP</h1>
<p>Sign in with your Sentinel account to connect <strong>{html.escape(client_name)}</strong>.</p>
<section class='login-access' aria-label='Requested access'><h2>Service access</h2>
<p>Access to this service through its configured tool permissions.</p>
<p>This connection can renew its access for {renewal} after each renewal. You can revoke it at any time.</p></section>
<input type='hidden' name='authorization_id' value='{authorization_id}'>
<label class='login-field'><span>Email</span><input autofocus type='email' name='email' autocomplete='username' placeholder='you@example.com' maxlength='320' required></label>
<label class='login-field'><span>Password</span><input type='password' name='password' autocomplete='current-password' placeholder='Sentinel password' maxlength='4096' required></label>
<button class='primary wide' type='submit'>Sign in with Sentinel</button>{error_html}
<p class='login-privacy'>Your password is verified securely and is never stored by Binance MCP.</p>
</form></main></body></html>"""
        response = HTMLResponse(
            body, status_code=status,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": f"default-src 'none'; style-src 'self'; form-action 'self' {callback_origin}; frame-ancestors 'none'",
                "X-Frame-Options": "DENY",
                # no-referrer makes browser form POSTs send Origin: null,
                # which correctly fails the strict origin check below.
                # Keep the origin for our form action; disclose no referrer
                # on cross-origin navigation to the connector callback.
                "Referrer-Policy": "same-origin",
            },
        )

        response.set_cookie(SIGNIN_COOKIE, binding, max_age=SIGNIN_TTL, httponly=True,
                            secure=self.config.issuer.startswith("https://"), samesite="lax",
                            path="/oauth/authorize")
        return response

    async def authorize_post(self, request: Request):
        origin = request.headers.get("origin")
        parsed = urlparse(self.config.issuer)
        if ((origin and origin != f"{parsed.scheme}://{parsed.netloc}")
                or request.headers.get("sec-fetch-site", "same-origin") not in {"same-origin", "none"}):
            return self._error(403, "access_denied", "cross-origin sign-in rejected")
        values = await self._form(request)
        if values is None:
            return self._error(400, "invalid_request", "invalid form")
        key = _hash(values.get("authorization_id", ""))
        binding = _hash(request.cookies.get(SIGNIN_COOKIE, ""))
        with self._lock:
            auth = self.pending.get(key)
            if not auth or auth["expires"] <= time.time() or not _ct(binding, auth["binding"]):
                return self._error(400, "invalid_request", "sign-in expired; restart connector authorization")
        try:
            identity = await authenticate_from_env(values.get("email", ""), values.get("password", ""))
        except SentinelAuthError as exc:
            return self._signin_response(auth, values["authorization_id"], request.cookies[SIGNIN_COOKIE],
                                         error=exc.detail, status=exc.status_code)
        code = _token(32)
        with self._lock:
            if self.pending.pop(key, None) is not auth or auth["expires"] <= time.time():
                return self._error(400, "invalid_request", "sign-in expired; restart connector authorization")
            self.codes[_hash(code)] = {
                **{k: v for k, v in auth.items() if k not in {"binding", "expires"}},
                "account_id": identity.account_id, "expires": int(time.time()) + CODE_TTL,
            }
            self._save()
        callback = urlparse(auth["redirect_uri"])
        query = parse_qs(callback.query, keep_blank_values=True)
        query["code"] = [code]
        query["iss"] = [self.config.issuer]
        if auth["state"]:
            query["state"] = [auth["state"]]
        flattened = [(key, value) for key, values_ in query.items() for value in values_]
        redirect_url = urlunparse(callback._replace(query=urlencode(flattened)))
        return RedirectResponse(redirect_url, status_code=303,
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def token(self, request: Request):
        values = await self._form(request)
        if values is None:
            return self._error(400, "invalid_request", "invalid form")
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

    @staticmethod
    async def _form(request: Request) -> dict[str, str] | None:
        raw = await request.body()
        if len(raw) > 16384:
            return None
        try:
            values = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=32)
        except (ValueError, UnicodeDecodeError):
            return None
        if any(len(v) != 1 for v in values.values()):
            return None
        return {k: v[0] for k, v in values.items()}

    async def revoke(self, request: Request):
        values = await self._form(request)
        if values is None:
            return self._error(400, "invalid_request", "invalid form")
        client_id, error = self._authenticate_client(request, values)
        if error:
            return self._error(401, "invalid_client", error)
        if not values.get("token"):
            return self._error(400, "invalid_request", "token is required")
        key = _hash(values["token"])
        with self._lock:
            record = self.refresh.get(key) or self.access.get(key) or self.spent_refresh.get(key)
            if record and record["client_id"] == client_id:
                self._revoke_session(record.get("session_id"))
                self._save()
        return JSONResponse({}, headers={"Cache-Control": "no-store"})

    def _revoke_session(self, session_id: str | None) -> None:
        if session_id:
            self.sessions.pop(session_id, None)
            for records in (self.access, self.refresh, self.spent_refresh):
                for key in [k for k, v in records.items() if v.get("session_id") == session_id]:
                    del records[key]

    @staticmethod
    def _account_allowed(account_id: int | None) -> bool:
        try:
            return account_id in SentinelConfig.from_env().allowed_account_ids
        except SentinelAuthError:
            return False

    def _session_active(self, record: dict[str, Any]) -> bool:
        session = self.sessions.get(record.get("session_id", ""))
        return bool(session and session["expires"] > time.time()
                    and session["client_id"] == record["client_id"]
                    and self._account_allowed(session["account_id"]))

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
        resource = values.get("resource", "").strip() or self.config.resource
        if not code or not redirect_uri or not verifier:
            return self._error(400, "invalid_request", "code, redirect_uri, and code_verifier are required")
        if not PKCE_VERIFIER_RE.fullmatch(verifier):
            return self._error(400, "invalid_grant", "invalid PKCE code_verifier")
        with self._lock:
            record = self.codes.get(_hash(code))
            if not record or record["expires"] <= time.time():
                return self._error(400, "invalid_grant", "authorization code is invalid or expired")
            if client_id != record["client_id"] or redirect_uri != record["redirect_uri"]:
                return self._error(400, "invalid_grant", "client or redirect mismatch")
            if not _ct(_pkce(verifier), record["code_challenge"]):
                return self._error(400, "invalid_grant", "PKCE verification failed")
            if resource != self.config.resource or resource != record["resource"]:
                return self._error(400, "invalid_target", "resource does not match the authorized MCP server")
            if not self._account_allowed(record.get("account_id")):
                return self._error(400, "invalid_grant", "authorization is no longer allowed; sign in again")
            del self.codes[_hash(code)]
            session_id = _token()
            self.sessions[session_id] = {
                "client_id": client_id, "account_id": record["account_id"],
                "expires": int(time.time()) + self.config.refresh_ttl,
            }
            response = self._issue(record["client_id"], record["scope"], session_id)
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
            record = self.refresh.get(_hash(presented))
            if (not record or record["expires"] <= time.time() or client_id != record["client_id"]
                    or not self._session_active(record)):
                return self._error(400, "invalid_grant", "refresh token is invalid or expired")
            try:
                scope = _scope(values.get("scope", record["scope"]))
            except ValueError:
                return self._error(400, "invalid_scope", "unsupported scope")
            if not set(scope.split()).issubset(set(record["scope"].split())):
                return self._error(400, "invalid_scope", "requested scope exceeds original grant")
            # Validate every parameter before consuming a rotating refresh token.
            del self.refresh[_hash(presented)]
            self.spent_refresh[_hash(presented)] = record
            session_id = record["session_id"]
            self.sessions[session_id]["expires"] = int(time.time()) + self.config.refresh_ttl
            response = self._issue(record["client_id"], scope, session_id)
            self._save()
            return JSONResponse(response, headers={"Cache-Control": "no-store"})

    def _issue(self, client_id: str, scope: str, session_id: str) -> dict[str, Any]:
        now = int(time.time())
        access = _token(32)
        self._cleanup()
        self.access[_hash(access)] = {"client_id": client_id, "scope": scope, "expires": now + self.config.access_ttl,
                                     "session_id": session_id}
        response = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.config.access_ttl,
            "scope": scope,
        }
        # OAuth refresh does not require the OIDC offline_access scope. Existing
        # ChatGPT registrations request mcp alone; retain their renewable access
        # without adding scopes that the client did not request or the user approve.
        refresh = _token(48)
        self.refresh[_hash(refresh)] = {"client_id": client_id, "scope": scope, "expires": now + self.config.refresh_ttl,
                                       "session_id": session_id}
        response["refresh_token"] = refresh
        return response

    def _cleanup(self) -> None:
        now = time.time()
        for records in (self.access, self.refresh, self.codes, self.pending, self.sessions, self.spent_refresh):
            for key in [k for k, v in records.items() if v.get("expires", 0) <= now]:
                del records[key]

    def valid_access_token(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            record = self.access.get(_hash(token))
            if not record:
                return False
            if record["expires"] <= time.time() or not self._session_active(record) or "mcp" not in record["scope"].split():
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
        self.clients = raw.get("clients", {})
        if raw.get("auth_binding") != _hash(self.config.state_binding):
            return
        self.access = raw.get("access", {})
        self.refresh = raw.get("refresh", {})
        self.codes = raw.get("codes", {})
        self.sessions = raw.get("sessions", {})
        self.spent_refresh = raw.get("spent_refresh", {})
        self._cleanup()

    def _save(self) -> None:
        path = Path(self.config.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        state = {
            "version": 3,
            "auth_binding": _hash(self.config.state_binding),
            "clients": self.clients,
            "access": self.access,
            "refresh": self.refresh,
            "codes": self.codes,
            "sessions": self.sessions,
            "spent_refresh": self.spent_refresh,
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
