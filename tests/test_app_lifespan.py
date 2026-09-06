from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from binance_mcp.server import build_mcp_asgi_app


def test_parent_lifespan_starts_mcp_streamable_http_manager():
    mcp_app = build_mcp_asgi_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    parent = FastAPI(lifespan=lifespan)
    parent.mount("/mcp", mcp_app)

    with TestClient(parent, base_url="http://localhost") as client:
        response = client.post(
            "/mcp/",
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "lifespan-test", "version": "1.0"},
                },
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["result"]["serverInfo"]["name"] == "Binance MCP"
