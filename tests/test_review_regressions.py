import time
from decimal import Decimal

import pytest

from binance_mcp.client import BinanceClientError
from binance_mcp.monitor import Monitor
from test_execution import setup, plan, orders_for, fill


@pytest.mark.asyncio
async def test_paid_redemption_resolves_once_and_releases_commitment(tmp_path):
    ledger, exchange, svc = setup(tmp_path, "live")
    ledger.change("experiment", "USDT", "SPOT", Decimal(-50))
    ledger.change("experiment", "USDT", "EARN:USDT001", Decimal(50))
    payload = {"operation": "earn_redeem", "params": {"productId": "USDT001", "amount": "50"}}
    ledger.begin("redeem", "experiment", payload, "USDT", "EARN:USDT001", "50",
                 prepared={"preview": {"historyStart": 1}, "response": {"response": {"redeemId": "r1"}}})
    original = exchange.signed_get
    async def signed(product, path, params=None):
        if path.endswith("redemptionRecord"):
            return {"total": 1, "rows": [{"redeemId": "r1", "projectId": "USDT001", "asset": "USDT", "amount": "50", "status": "PAID"}]}
        return await original(product, path, params)
    exchange.signed_get = signed
    result = await svc.reconcile("redeem")
    assert result["state"] == "RESOLVED"
    assert ledger.available("experiment", "USDT") == 100
    assert ledger.balance("experiment", "USDT", "EARN:USDT001") == 0
    await svc.reconcile("redeem")
    assert ledger.available("experiment", "USDT") == 100
    assert not exchange.writes


@pytest.mark.asyncio
async def test_first_subscription_uses_product_catalog_not_account_position(tmp_path):
    ledger, exchange, svc = setup(tmp_path, "live")
    requests = []
    original = exchange.signed_get
    async def signed(product, path, params=None):
        requests.append(path)
        if path.endswith("/flexible/list"):
            return {"total": 1, "rows": [{"productId": "USDTNEW", "asset": "USDT", "canPurchase": True,
                                          "isSoldOut": False, "minPurchaseAmount": "10", "status": "PURCHASING",
                                          "subscriptionStartTime": 1}]}
        if path.endswith("subscriptionRecord"):
            return {"total": 1, "rows": [{"purchaseId": "p1", "productId": "USDTNEW", "asset": "USDT", "amount": "25", "status": "SUCCESS"}]}
        return await original(product, path, params)
    async def subscribe(product_id, amount, auto_subscribe, source_account):
        exchange.writes.append((product_id, amount, auto_subscribe, source_account))
        return {"purchaseId": "p1", "success": True}
    exchange.signed_get = signed
    exchange.simple_earn_subscribe = subscribe
    result = await svc.execute("experiment", "first-sub", "earn_subscribe", {"productId": "USDTNEW", "amount": "25"})
    assert result["state"] == "RESOLVED"
    assert "/sapi/v1/simple-earn/flexible/list" in requests
    assert not any(path.endswith("/flexible/position") for path in requests[requests.index("/sapi/v1/simple-earn/flexible/list"):])
    assert ledger.balance("experiment", "USDT") == 75
    assert ledger.balance("experiment", "USDT", "EARN:USDTNEW") == 25
    assert exchange.writes == [("USDTNEW", "25", False, "SPOT")]
    await svc.execute("experiment", "first-sub", "earn_subscribe", {"productId": "USDTNEW", "amount": "25"})
    assert len(exchange.writes) == 1


@pytest.mark.asyncio
async def test_failed_tick_keeps_pause_and_last_success_despite_stored_protection(tmp_path):
    ledger, exchange, svc = setup(tmp_path, "live")
    exchange.orders = orders_for("protected", "FILLED", "0.5", "NEW")
    exchange.fills = {1: [fill()]}
    result = await svc.execute("experiment", "protected", "otoco", plan())
    assert result["result"]["protected"]
    # Reflect accounted fills in exchange totals so unrelated account drift cannot hide the bug.
    exchange.account["balances"][0]["free"] = "950"
    exchange.account["balances"][1]["free"] = "10.4995"
    monitor = Monitor(svc)
    await monitor.tick()
    success_at = ledger.meta("monitorAt")
    original = exchange.signed_get
    async def failed(product, path, params=None):
        if path == "/api/v3/orderList":
            raise BinanceClientError("temporary list query failure")
        return await original(product, path, params)
    exchange.signed_get = failed
    from unittest.mock import patch
    with patch("time.time", return_value=time.time()+10):
        with pytest.raises(BinanceClientError, match="reconciliation incomplete"):
            await monitor.tick()
    assert ledger.meta("pause") == "reconciliation incomplete for protected"
    assert ledger.meta("monitorAt") == success_at
    assert ledger.get("protected")["result"]["protected"]  # old evidence still stored, not accepted as fresh
    exchange.signed_get = original
    with patch("time.time", return_value=time.time()+20):
        await monitor.tick()
    assert not ledger.meta("pause")
    assert ledger.meta("monitorAt") > success_at

@pytest.mark.asyncio
@pytest.mark.parametrize("op,status,accepted", [
    ("earn_subscribe", "SUCCESS", True), ("earn_subscribe", "PAID", False),
    ("earn_subscribe", "PENDING", False), ("earn_redeem", "PAID", True),
    ("earn_redeem", "SUCCESS", False), ("earn_redeem", "WAITING", False),
])
async def test_earn_terminal_status_is_operation_specific(tmp_path, op, status, accepted):
    ledger, exchange, svc = setup(tmp_path, "live")
    location = "SPOT" if op == "earn_subscribe" else "EARN:USDT001"
    destination = "EARN:USDT001" if op == "earn_subscribe" else "SPOT"
    if op == "earn_redeem":
        ledger.change("experiment", "USDT", "SPOT", Decimal(-50))
        ledger.change("experiment", "USDT", location, Decimal(50))
    field = "purchaseId" if op == "earn_subscribe" else "redeemId"
    product_field = "productId" if op == "earn_subscribe" else "projectId"
    ledger.begin("terminal", "experiment", {"operation": op, "params": {"productId": "USDT001", "amount": "50"}},
                 "USDT", location, "50", prepared={"preview": {"historyStart": 1}, "response": {"response": {field: "r1"}}})
    original = exchange.signed_get
    async def signed(product, path, params=None):
        if path.endswith("Record"):
            return {"total": 1, "rows": [{field: "r1", product_field: "USDT001", "asset": "USDT", "amount": "50", "status": status}]}
        return await original(product, path, params)
    exchange.signed_get = signed
    before = ledger.balance("experiment", "USDT", destination)
    if accepted:
        assert (await svc.reconcile("terminal"))["state"] == "RESOLVED"
        assert ledger.balance("experiment", "USDT", destination) == before + 50
    else:
        with pytest.raises(BinanceClientError, match="exact transfer"):
            await svc.reconcile("terminal")
        assert ledger.get("terminal")["state"] == "PREPARED"
        assert ledger.balance("experiment", "USDT", destination) == before
        assert ledger.get("terminal")["reserved"] == "50"
    assert not exchange.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("canPurchase", False), ("isSoldOut", True), ("status", "END"),
                                        ("minPurchaseAmount", "30"), ("subscriptionStartTime", 9999999999999), ("asset", "ETH")])
async def test_subscription_catalog_guards_reject_before_write(tmp_path, field, value):
    ledger, exchange, svc = setup(tmp_path, "live")
    metadata = {"productId": "USDTNEW", "asset": "USDT", "canPurchase": True, "isSoldOut": False,
                "minPurchaseAmount": "10", "status": "PURCHASING", "subscriptionStartTime": 1, field: value}
    original = exchange.signed_get
    async def signed(product, path, params=None):
        if path.endswith("/flexible/list"):
            return {"total": 1, "rows": [metadata]}
        return await original(product, path, params)
    exchange.signed_get = signed
    with pytest.raises(BinanceClientError):
        await svc.execute("experiment", "invalid", "earn_subscribe", {"productId": "USDTNEW", "amount": "25"})
    assert not exchange.writes
    assert not ledger.outstanding()


@pytest.mark.asyncio
async def test_redemption_still_requires_an_existing_owned_position(tmp_path):
    _, exchange, svc = setup(tmp_path, "live")
    with pytest.raises(BinanceClientError, match="account positions"):
        await svc.investment_preflight("experiment", "earn_redeem", {"productId": "USDTNEW", "amount": "25"})
    assert not exchange.writes
