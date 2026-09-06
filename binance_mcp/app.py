from fastapi import FastAPI
from .oauth_server import MCPAuth, OAuthManager
from .server import build_mcp_asgi_app, client

app = FastAPI(title="Binance MCP", docs_url=None, redoc_url=None)
oauth = OAuthManager.from_env()
if oauth:
    app.include_router(oauth.router)

@app.get("/health")
async def health() -> dict:
    status = client.auth_status()
    return {
        "ok": True,
        "mcp_auth_mode": "oauth" if oauth else "unconfigured",
        "binance_account_access_ready": status["account_access_ready"],
        "binance_trading_enabled": status["trading_enabled"],
    }

app.mount("/mcp", MCPAuth(build_mcp_asgi_app(), oauth))
