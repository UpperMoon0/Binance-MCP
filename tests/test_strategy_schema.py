from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from binance_mcp.server import build_mcp_asgi_app


def test_rpc_schema_requires_intents_on_every_financial_tool():
    mcp_app = build_mcp_asgi_app()
    @asynccontextmanager
    async def lifespan(_):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield
    parent = FastAPI(lifespan=lifespan)
    parent.mount("/mcp", mcp_app)
    headers = {"accept": "application/json, text/event-stream"}
    with TestClient(parent, base_url="http://localhost") as client:
        response = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert response.status_code == 200
        tools = {t["name"]: t for t in response.json()["result"]["tools"]}
        for name in ("binance_order_request", "binance_simple_earn_subscribe", "binance_simple_earn_redeem",
                     "binance_dual_investment_subscribe", "binance_dual_investment_from_flexible_earn", "binance_strategy_execution"):
            assert {"strategyId", "intentId"} <= set(tools[name]["inputSchema"]["required"])
        assert {"binance_trade_preview", "binance_portfolio_snapshot", "binance_market_scan",
                "binance_strategy_status", "binance_execution_status"} <= tools.keys()
        response = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "binance_order_request", "arguments": {"product": "spot", "action": "create", "params": {"symbol": "BTCUSDT"}}}})
        assert response.json()["result"]["isError"]
