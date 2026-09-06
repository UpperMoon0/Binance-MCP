from __future__ import annotations

import os
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import BinanceClient, Scalar
from .config import ORDER_PATHS, Product, TradingProduct
from .investment import AutoCompoundPlan, DestinationAccount, InvestmentService, OptionType

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)

client = BinanceClient()
investment = InvestmentService(client)
server = MCPServer(
    "Binance MCP",
    instructions=(
        "Use binance_public_request for public Binance REST data and binance_account_request for read-only signed account endpoints. "
        "Use the named Simple Earn and Dual Investment tools for those financial writes; do not request arbitrary signed POSTs. "
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


@server.tool(
    title="Binance public API request",
    description="GET any public Binance REST endpoint across Spot, USDⓈ-M Futures, COIN-M Futures, Options, or Portfolio Margin. Full external URLs are rejected.",
    annotations=READ_ONLY,
)
async def binance_public_request(
    product: Annotated[Product, Field(description="spot, usds_futures, coin_futures, options, or portfolio_margin")],
    path: Annotated[str, Field(description="API path such as /api/v3/ticker/price")],
    params: Annotated[dict[str, Scalar] | None, Field(description="Query parameters")] = None,
) -> object:
    return await client.public_get(product, path, params)


@server.tool(
    title="Binance account API request",
    description="GET a signed account endpoint using server-side Binance credentials. Read-only transport: no orders, transfers, or withdrawals.",
    annotations=READ_ONLY,
)
async def binance_account_request(
    product: Annotated[Product, Field(description="Binance product API host, including portfolio_margin for papi.binance.com")],
    path: Annotated[str, Field(description="Signed GET endpoint path such as /api/v3/account or /papi/v1/account")],
    params: Annotated[dict[str, Scalar] | None, Field(description="Query parameters; timestamp/signature are added server-side")] = None,
) -> object:
    return await client.signed_get(product, path, params)


@server.tool(
    title="Binance order mutation",
    description="Create or cancel a standard Spot/Futures/Options order. Requires BINANCE_TRADING_ENABLED=true and a trade-enabled API key. Portfolio Margin uses multiple distinct order families and is intentionally not routed through this generic order tool. Withdrawals are not supported.",
    annotations=WRITE,
)
async def binance_order_request(
    product: Annotated[TradingProduct, Field(description="spot, usds_futures, coin_futures, or options")],
    action: Annotated[Literal["create", "cancel"], Field(description="Order action")],
    params: Annotated[dict[str, Scalar], Field(description="Order parameters required by Binance")],
) -> object:
    return await client.order(product, ORDER_PATHS[product], action, params)


@server.tool(
    title="Redeem Simple Earn Flexible",
    description="Redeem a specific Simple Earn Flexible amount to Spot or Funding. Financial write action; requires BINANCE_TRADING_ENABLED=true. Credentials and signatures stay server-side.",
    annotations=WRITE,
)
async def binance_simple_earn_redeem(
    productId: Annotated[str, Field(description="Simple Earn Flexible product id, for example USDT001")],
    amount: Annotated[str, Field(description="Positive decimal amount to redeem")],
    destAccount: Annotated[DestinationAccount, Field(description="Destination account. Defaults to SPOT.")] = "SPOT",
) -> object:
    return await investment.redeem_flexible(productId, amount, destAccount)


@server.tool(
    title="Subscribe Dual Investment",
    description="Subscribe Spot funds into one exact Dual Investment product after re-fetching it and applying optional APR and strike-distance guards. Financial write action; requires BINANCE_TRADING_ENABLED=true. autoCompoundPlan defaults to NONE.",
    annotations=WRITE,
)
async def binance_dual_investment_subscribe(
    productId: Annotated[str, Field(description="Exact Dual Investment product id from the live product list")],
    investCoin: Annotated[str, Field(description="Asset deposited into the product, e.g. USDT for Buy Low BTC")],
    exercisedCoin: Annotated[str, Field(description="Target exercised asset, e.g. BTC for Buy Low BTC")],
    optionType: Annotated[OptionType, Field(description="PUT for Buy Low or CALL for Sell High")],
    depositAmount: Annotated[str, Field(description="Positive decimal subscription amount")],
    autoCompoundPlan: Annotated[AutoCompoundPlan, Field(description="NONE, STANDARD, or ADVANCED. Defaults to NONE.")] = "NONE",
    minimumApr: Annotated[str | None, Field(description="Optional decimal APR floor, e.g. 0.18 for 18%")] = None,
    minimumStrikeDistancePercent: Annotated[
        str | None,
        Field(description="Optional minimum live strike distance percentage, e.g. 2.0"),
    ] = None,
) -> object:
    return await investment.subscribe_dual(
        product_id=productId,
        option_type=optionType,
        exercised_coin=exercisedCoin,
        invest_coin=investCoin,
        deposit_amount=depositAmount,
        auto_compound_plan=autoCompoundPlan,
        minimum_apr=minimumApr,
        minimum_strike_distance_percent=minimumStrikeDistancePercent,
    )


@server.tool(
    title="Read Dual Investment positions",
    description="Read and normalize current Dual Investment positions. Signed read-only endpoint.",
    annotations=READ_ONLY,
)
async def binance_dual_investment_positions(
    status: Annotated[
        str | None,
        Field(description="Optional Binance purchase status such as PENDING, PURCHASE_SUCCESS, SETTLED, or PURCHASE_FAIL"),
    ] = None,
    pageSize: Annotated[int, Field(ge=1, le=100, description="Number of positions per page")] = 100,
    pageIndex: Annotated[int, Field(ge=1, description="1-based page index")] = 1,
) -> object:
    return await investment.positions(status=status, page_size=pageSize, page_index=pageIndex)


@server.tool(
    title="Move Flexible Earn funds into Dual Investment",
    description="Safely orchestrate Flexible Earn redemption to Spot and subscription into one exact Dual Investment product while preserving a configured Earn balance. Re-checks the product after redemption and leaves funds in Spot rather than substituting another product if guards fail. Financial write action; requires BINANCE_TRADING_ENABLED=true.",
    annotations=WRITE,
)
async def binance_dual_investment_from_flexible_earn(
    earnProductId: Annotated[str, Field(description="Simple Earn Flexible product id, e.g. USDT001")],
    dualProductId: Annotated[str, Field(description="Exact Dual Investment product id")],
    investCoin: Annotated[str, Field(description="Asset being redeemed and invested, e.g. USDT")],
    exercisedCoin: Annotated[str, Field(description="Target exercised asset, e.g. BTC")],
    optionType: Annotated[OptionType, Field(description="PUT for Buy Low or CALL for Sell High")],
    amount: Annotated[str, Field(description="Positive decimal amount to redeem and invest")],
    preserveEarnAmount: Annotated[str, Field(description="Minimum Flexible Earn balance that must remain after redemption")],
    minimumApr: Annotated[str | None, Field(description="Optional decimal APR floor, e.g. 0.18 for 18%")] = None,
    minimumStrikeDistancePercent: Annotated[
        str | None,
        Field(description="Optional minimum live strike distance percentage"),
    ] = None,
    autoCompoundPlan: Annotated[AutoCompoundPlan, Field(description="Defaults to NONE; never silently enables compounding")] = "NONE",
) -> object:
    return await investment.from_flexible_earn(
        earn_product_id=earnProductId,
        dual_product_id=dualProductId,
        option_type=optionType,
        exercised_coin=exercisedCoin,
        invest_coin=investCoin,
        amount=amount,
        preserve_earn_amount=preserveEarnAmount,
        minimum_apr=minimumApr,
        minimum_strike_distance_percent=minimumStrikeDistancePercent,
        auto_compound_plan=autoCompoundPlan,
    )


@server.tool(
    title="Binance authentication status",
    description="Show redacted server-side Binance account signing/trading capability. Never returns credentials.",
    annotations=READ_ONLY,
)
async def binance_auth_status() -> dict:
    return client.auth_status()


def build_mcp_asgi_app():
    return server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        streamable_http_path="/",
        transport_security=_transport_security(),
    )
