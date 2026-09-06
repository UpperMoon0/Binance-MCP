from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse

from .oauth import MCPAuth, OAuthConfig, OAuthManager as _BaseOAuthManager


class OAuthManager(_BaseOAuthManager):
    """Browser-compatible OAuth manager for hosted MCP clients such as ChatGPT."""

    async def authorize_get(self, request: Request):
        response = await super().authorize_get(request)
        if not isinstance(response, HTMLResponse) or response.status_code >= 400:
            return response

        # The authorize form posts back to this server and then returns a 303 to
        # the already-validated registered redirect_uri. CSP form-action applies
        # to that form navigation, including redirects, so the callback origin
        # must be allowed as well as self.
        redirect_uri = request.query_params.get("redirect_uri", "")
        parsed = urlparse(redirect_uri)
        if parsed.scheme and parsed.netloc:
            callback_origin = f"{parsed.scheme}://{parsed.netloc}"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; "
                f"form-action 'self' {callback_origin}; frame-ancestors 'none'"
            )
        return response


__all__ = ["MCPAuth", "OAuthConfig", "OAuthManager"]
