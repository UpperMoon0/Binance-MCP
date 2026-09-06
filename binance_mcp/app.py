from contextlib import asynccontextmanager

from fastapi import FastAPI
from .oauth_server import MCPAuth, OAuthManager
from .server import build_mcp_asgi_app, client

mcp_app = build_mcp_asgi_app()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Starlette does not run the lifespan of mounted sub-applications.
    # The MCP SDK initializes its Streamable HTTP task group in the child
    # app lifespan, so explicitly enter it from the parent FastAPI lifespan.
    async with mcp_app.router.lifespan_context(mcp_app):
        yield


app = FastAPI(title="Binance MCP", docs_url=None, redoc_url=None, lifespan=lifespan)
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

app.mount("/mcp", MCPAuth(mcp_app, oauth))
