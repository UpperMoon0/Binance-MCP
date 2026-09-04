from __future__ import annotations

import os
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import BinanceClient, Scalar
from .config import ORDER_PATHS, Product

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)

client = BinanceClient()
server = MCPServer(
    "Binance MCP",
    instructions=(
        "Use binance_public_request for public Binance REST data. Use binance_account_request for read-only signed account endpoints. "
        "Trading is separately deployment-gated. Never request or reveal Binance secrets in chat. Withdrawals are intentionally unsupported."
    ),
)

def _csv(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]

def _transport_security() -> TransportSecuritySettings:
    hosts = _csv("MCP_ALLOWED_HOSTS")
    if not hosts:
        hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"]
        domain = os.getenv("DOMAIN_NAME", "").strip().strip(".")
        if domain:
            host = f"binance-mcp.{domain}"
            hosts.extend([host, f"{host}:*"])
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=_csv("MCP_ALLOWED_ORIGINS"))

@server.tool(title="Binance public API request", description="GET any public Binance REST endpoint across Spot, USDⓈ-M Futures, COIN-M Futures, or Options. Full external URLs are rejected.", annotations=READ_ONLY)
async def binance_public_request(
    product: Annotated[Product, Field(description="spot, usds_futures, coin_futures, or options")],
    path: Annotated[str, Field(description="API path such as /api/v3/ticker/price")],
    params: Annotated[dict[str, Scalar] | None, Field(description="Query parameters")] = None,
) -> object:
    return await client.public_get(product, path, params)

@server.tool(title="Binance account API request", description="GET a signed account endpoint using server-side Binance credentials. Read-only transport: no orders, transfers, or withdrawals.", annotations=READ_ONLY)
async def binance_account_request(
    product: Annotated[Product, Field(description="Binance product API host")],
    path: Annotated[str, Field(description="Signed GET endpoint path such as /api/v3/account")],
    params: Annotated[dict[str, Scalar] | None, Field(description="Query parameters; timestamp/signature are added server-side")] = None,
) -> object:
    return await client.signed_get(product, path, params)

@server.tool(title="Binance order mutation", description="Create or cancel a standard Spot/Futures/Options order. Requires BINANCE_TRADING_ENABLED=true and a trade-enabled API key. Withdrawals are not supported.", annotations=WRITE)
async def binance_order_request(
    product: Annotated[Product, Field(description="Binance product")],
    action: Annotated[Literal["create", "cancel"], Field(description="Order action")],
    params: Annotated[dict[str, Scalar], Field(description="Order parameters required by Binance")],
) -> object:
    return await client.order(product, ORDER_PATHS[product], action, params)

@server.tool(title="Binance authentication status", description="Show redacted server-side Binance account signing/trading capability. Never returns credentials.", annotations=READ_ONLY)
async def binance_auth_status() -> dict:
    return client.auth_status()

def build_mcp_asgi_app():
    return server.streamable_http_app(json_response=True, stateless_http=True, streamable_http_path="/", transport_security=_transport_security())
