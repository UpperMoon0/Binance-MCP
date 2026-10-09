from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import pytest

from binance_mcp.client import BinanceClientError
from binance_mcp.execution import ExecutionService, client_id
from binance_mcp.ledger import Ledger
from binance_mcp.monitor import Monitor


class Exchange:
    def __init__(self):
        self._cache = {}
        self.writes = []
        self.failure = None
        self.candles = []
        self.orders = {}
        self.fills = {}
        self.account = {"canTrade": True, "permissions": ["SPOT"], "balances": [
            {"asset": "USDT", "free": "1000", "locked": "0"}, {"asset": "BTC", "free": "10", "locked": "0"}]}
        self.info = {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
            "permissionSets": [["SPOT"]], "isSpotTradingAllowed": True, "ocoAllowed": True, "otoAllowed": True,
            "filters": [{"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
                        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "100000", "tickSize": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "5"}]}
        self.book = {"bidPrice": "99.99", "askPrice": "100.01", "bidQty": "10", "askQty": "10"}

    def _require_trading(self):
        pass

    async def sync_time(self):
        return 0

    async def public_get(self, product, path, params=None):
        if path == "/api/v3/exchangeInfo":
            return {"symbols": [self.info], "exchangeFilters": []}
        if path == "/api/v3/ticker/bookTicker":
            return self.book
        if path == "/api/v3/avgPrice":
            return {"price": "100", "mins": 5}
        if path == "/api/v3/klines":
            return self.candles
        if path == "/api/v3/depth":
            return {"bids": [["99.99", "10"]], "asks": [["100.01", "10"]]}
        raise AssertionError(path)

    async def signed_get(self, product, path, params=None):
        if path == "/api/v3/account":
            return self.account
        if path == "/api/v3/account/commission":
            return {"discount": {"enabledForAccount": False, "enabledForSymbol": False}, **{k: {"maker": rate, "taker": rate, "buyer": "0", "seller": "0"}
                    for k, rate in (("standardCommission", "0.001"), ("taxCommission", "0"), ("specialCommission", "0"))}}
        if path in ("/api/v3/openOrders", "/api/v3/openOrderList"):
            return []
        if path == "/sapi/v1/simple-earn/flexible/position":
            return {"total": 0, "rows": []}
        if path == "/sapi/v1/dci/product/positions":
            return {"total": 0, "list": []}
        if path == "/api/v3/orderList":
            if not self.orders:
                raise BinanceClientError("order not visible", status=400, code=-2013)
            return {"orderListId": 1, "symbol": "BTCUSDT", "listClientOrderId": params["origClientOrderId"], "orders": [{"symbol": "BTCUSDT", "orderId": key} for key in self.orders]}
        if path == "/api/v3/order":
            return self.orders[params["orderId"]]
        if path == "/api/v3/myTrades":
            return self.fills.get(params["orderId"], [])
        raise AssertionError(path)

    async def order(self, product, path, action, params):
        self.writes.append((path, action, params))
        if self.failure:
            raise self.failure
        return {"orderListId": 1}


def setup(tmp_path, mode="paper", reinvest=True):
    ledger = Ledger(str(tmp_path / "ledger.sqlite"), {"experiment": {"allocation": "100", "mode": mode, "reinvest": reinvest,
        "recoveryPolicy": {"version": 1, "approved": True, "action": "operator_only", "maxDelayMs": 30000},
        "riskPolicy": {"version": 1, "approved": True, "operations": ['otoco', 'oco', 'order', 'cancel', 'earn_subscribe', 'earn_redeem', 'dual_subscribe', 'earn_to_dual'],
                       "assets": ['BTC', 'ETH', 'USDT'], "maxPositions": 10, "maxPositionQuote": "100",
                       "maxPlannedDownsideQuote": "100", "lossPauseQuote": "100",
                       "executionAllowanceBps": "10", "valuationMaxAgeMs": 90000}}})
    exchange = Exchange()
    execution = ExecutionService(exchange, ledger, ["BTCUSDT", "ETHUSDT"])
    execution.incident_sink_configured = True
    ledger.set_meta("monitorAt", int(time.time()*1000))
    ledger.set_meta("protectionAt", int(time.time()*1000))
    ledger.set_meta("streamConnected", True)
    return ledger, exchange, execution


def plan(**changes):
    return {"symbol": "BTCUSDT", "quantity": "0.5", "price": "100", "stopPrice": "95", "takeProfit": "110", **changes}


def orders_for(key, status="NEW", qty="0", exits="PENDING_NEW"):
    return {
        1: {"symbol": "BTCUSDT", "orderId": 1, "clientOrderId": client_id(key, "entry"), "status": status,
            "origQty": "0.5", "executedQty": qty, "side": "BUY", "type": "LIMIT", "price": "100", "timeInForce": "GTC", "orderListId": 1},
        2: {"symbol": "BTCUSDT", "orderId": 2, "clientOrderId": client_id(key, "take"), "status": exits,
            "origQty": "0.499", "executedQty": "0", "side": "SELL", "type": "LIMIT_MAKER", "price": "110", "orderListId": 1},
        3: {"symbol": "BTCUSDT", "orderId": 3, "clientOrderId": client_id(key, "stop"), "status": exits,
            "origQty": "0.499", "executedQty": "0", "side": "SELL", "type": "STOP_LOSS", "stopPrice": "95", "orderListId": 1},
    }


def fill(key=1, qty="0.5", side=True, price="100", commission="0.0005", fee_asset="BTC"):
    return {"id": key, "orderId": 1, "qty": qty, "quoteQty": str(Decimal(qty)*Decimal(price)),
            "price": price, "commission": commission, "commissionAsset": fee_asset, "isBuyer": side}


def test_allocations_are_once_only_and_immutable(tmp_path):
    ledger, _, _ = setup(tmp_path)
    ledger.change("experiment", "USDT", "SPOT", Decimal(-20))
    ledger.db.close()
    reopened = Ledger(str(tmp_path / "ledger.sqlite"), {"experiment": {"allocation": "100", "mode": "paper"}})
    assert reopened.balance("experiment", "USDT") == 80
    with pytest.raises(BinanceClientError, match="immutable"):
        Ledger(str(tmp_path / "ledger.sqlite"), {"experiment": {"allocation": "200", "mode": "paper"}})


def test_atomic_budget_and_intent_conflict_across_connections(tmp_path):
    ledger, _, _ = setup(tmp_path)
    second = Ledger(str(tmp_path / "ledger.sqlite"))
    ledger.begin("a", "experiment", {"amount": "70"}, "USDT", "SPOT", "70")
    with pytest.raises(BinanceClientError, match="capital"):
        second.begin("b", "experiment", {"amount": "70"}, "USDT", "SPOT", "70")
    assert second.begin("a", "experiment", {"amount": "70"}, "USDT", "SPOT", "70")[1] is False
    with pytest.raises(BinanceClientError, match="different contract"):
        second.begin("a", "experiment", {"amount": "71"}, "USDT", "SPOT", "71")


@pytest.mark.asyncio
async def test_timeout_survives_restart_and_never_resubmits(tmp_path):
    ledger, exchange, svc = setup(tmp_path, "live")
    exchange.failure = BinanceClientError("timeout", outcome_unknown=True)
    result = await svc.execute("experiment", "timeout", "otoco", plan())
    assert result["state"] == "OUTCOME_UNKNOWN"
    assert ledger.available("experiment", "USDT") == Decimal("49.95")
    ledger.db.close()
    reopened = Ledger(str(tmp_path / "ledger.sqlite"))
    svc = ExecutionService(exchange, reopened, ["BTCUSDT"])
    with pytest.raises(BinanceClientError, match="visible"):
        await svc.execute("experiment", "timeout", "otoco", plan())
    assert len(exchange.writes) == 1
    with pytest.raises(BinanceClientError):
        await svc.execute("experiment", "second", "otoco", plan())
    assert len(exchange.writes) == 1


@pytest.mark.asyncio
async def test_known_rejection_releases_reservation(tmp_path):
    ledger, exchange, svc = setup(tmp_path, "live")
    exchange.failure = BinanceClientError("rejected", status=400, code=-2010)
    result = await svc.execute("experiment", "reject", "otoco", plan())
    assert result["state"] == "REJECTED"
    assert ledger.available("experiment", "USDT") == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"quantity": "0.0009"}, {"price": "100.001"}, {"quantity": "0.05"},
                                     {"stopPrice": "99.99"}, {"price": "110"}, {"quantity": "2"}])
async def test_preflight_rejects_bad_entry_or_exit(tmp_path, changes):
    _, exchange, svc = setup(tmp_path)
    with pytest.raises(BinanceClientError):
        await svc.execute("experiment", "invalid", "otoco", plan(**changes))
    assert not exchange.writes


@pytest.mark.asyncio
async def test_unprotected_buy_and_other_strategy_owned_sells_blocked(tmp_path):
    _, exchange, svc = setup(tmp_path)
    with pytest.raises(BinanceClientError, match="protected"):
        await svc.preview("experiment", "order", {"symbol": "BTCUSDT", "quantity": "0.5", "price": "100", "side": "BUY"})
    with pytest.raises(BinanceClientError, match="capital"):
        await svc.preview("experiment", "order", {"symbol": "BTCUSDT", "quantity": "0.5", "price": "100", "side": "SELL"})
    assert not exchange.writes


@pytest.mark.asyncio
async def test_partial_fill_is_unprotected_and_fills_are_idempotent(tmp_path):
    ledger, exchange, svc = setup(tmp_path, "live")
    exchange.orders = orders_for("partial", "PARTIALLY_FILLED", "0.2")
    exchange.fills = {1: [fill(qty="0.2", commission="0.0002")]}
    result = await svc.execute("experiment", "partial", "otoco", plan())
    assert result["state"] == "OPEN"
    assert not result["result"]["protected"]
    assert result["result"]["protectionUncertain"]
    assert ledger.balance("experiment", "BTC") == Decimal("0.1998")
    assert ledger.balance("experiment", "USDT") == 80
    await svc.reconcile("partial")
    assert ledger.balance("experiment", "USDT") == 80
    assert len(exchange.writes) == 1
    assert ledger.meta("pause")


@pytest.mark.asyncio
async def test_full_entry_requires_active_fee_adjusted_exits(tmp_path):
    ledger, exchange, svc = setup(tmp_path, "live")
    exchange.orders = orders_for("full", "FILLED", "0.5", "NEW")
    exchange.fills = {1: [fill()]}
    result = await svc.execute("experiment", "full", "otoco", plan())
    assert result["result"]["protected"]
    assert result["asset"] == "BTC"
    assert ledger.available("experiment", "USDT") == 50
    assert ledger.available("experiment", "BTC") == Decimal("0.0005")
    exchange.orders[3]["status"] = "REJECTED"
    result = await svc.reconcile("full")
    assert result["result"]["protectionUncertain"]
    assert ledger.meta("pause")


@pytest.mark.asyncio
async def test_stale_monitor_and_manual_activity_pause_entries(tmp_path):
    ledger, exchange, svc = setup(tmp_path, "live")
    ledger.set_meta("protectionAt", 1)
    with pytest.raises(BinanceClientError, match="stale"):
        await svc.execute("experiment", "x", "otoco", plan())
    assert not exchange.writes
    ledger.set_meta("pause", None)
    ledger.set_meta("monitorAt", int(time.time()*1000))
    ledger.set_meta("protectionAt", int(time.time()*1000))
    await svc.check_account()
    exchange.account["balances"][0]["free"] = "999"
    with pytest.raises(BinanceClientError, match="differs"):
        await svc.execute("experiment", "y", "otoco", plan())
    assert not exchange.writes


@pytest.mark.asyncio
async def test_paper_lifecycle_fees_proceeds_reused_without_live_writes(tmp_path):
    ledger, exchange, svc = setup(tmp_path)
    result = await svc.execute("experiment", "paper", "otoco", plan())
    created = result["created"]
    entry_time = created + 60000
    exit_time = created + 120000
    exchange.candles = [[entry_time, "100", "101", "99", "100", "10", entry_time+1],
                        [exit_time, "100", "111", "98", "110", "10", exit_time+1]]
    # Make these closed observations relative to mock clock.
    from unittest.mock import patch
    with patch("time.time", return_value=(exit_time+60000)/1000):
        result = await svc.reconcile("paper")
    assert result["state"] == "RESOLVED"
    assert not exchange.writes
    assert ledger.balance("experiment", "USDT") == Decimal("104.835110")
    assert ledger.report("experiment", {"BTC": "110"})["resultsByCategory"]["realized"]
    assert ledger.available("experiment", "USDT") > 100


@pytest.mark.asyncio
async def test_monitor_receives_events_and_paper_does_not_open_stream(tmp_path):
    ledger, _, svc = setup(tmp_path)
    monitor = Monitor(svc)
    await monitor.start()
    assert len(monitor.tasks) == 1
    await monitor.handle_event({"event": {"e": "executionReport"}})
    assert monitor.wake.is_set()
    assert ledger.meta("lastUserEventAt")
    await monitor.stop()
    assert not ledger.meta("streamConnected")


def test_nonfinite_negative_and_zero_amounts_never_reserved(tmp_path):
    ledger, _, _ = setup(tmp_path)
    for bad in ("NaN", "Infinity", "-1"):
        with pytest.raises(BinanceClientError):
            ledger.begin(bad, "experiment", {}, "USDT", "SPOT", bad)


@pytest.mark.asyncio
async def test_parallel_paper_requests_share_budget(tmp_path):
    ledger, _, svc = setup(tmp_path)
    results = await asyncio.gather(svc.execute("experiment", "a", "otoco", plan(quantity="0.7")),
                                   svc.execute("experiment", "b", "otoco", plan(quantity="0.7")), return_exceptions=True)
    assert sum(isinstance(r, BinanceClientError) for r in results) == 1
    assert len(ledger.outstanding()) == 1

@pytest.mark.asyncio
async def test_replacement_oco_cannot_race_active_partial_entry(tmp_path):
    ledger, exchange, svc = setup(tmp_path, "live")
    exchange.orders = orders_for("partial", "PARTIALLY_FILLED", "0.2")
    exchange.fills = {1: [fill(qty="0.2", commission="0.0002")]}
    await svc.execute("experiment", "partial", "otoco", plan())
    with pytest.raises(BinanceClientError, match="cancel and reconcile"):
        await svc.execute("experiment", "replacement", "oco", plan(quantity="0.199"))
    assert len(exchange.writes) == 1


@pytest.mark.asyncio
async def test_unknown_di_reconciliation_from_prewrite_checkpoint_moves_owned_funds(tmp_path):
    from test_investment import FakeClient, product, matching_position
    ledger = Ledger(str(tmp_path / "di.sqlite"), {"experiment": {"allocation": "1000", "mode": "live"}})
    p = {"amount": "700", "dualProductId": "2650584", "optionType": "PUT", "investCoin": "USDT", "exercisedCoin": "BTC"}
    payload = {"operation": "dual_subscribe", "params": p}
    preview = {"contract": {"product": product(), "previousPositionIds": []}, "historyStart": 1}
    ledger.begin("di-unknown", "experiment", payload, "USDT", "SPOT", "700",
                 prepared={"preview": preview, "phase": "dual_sending", "checkpoint": {"product": product(), "previousPositionIds": []}})
    ledger.db.close()
    reopened = Ledger(str(tmp_path / "di.sqlite"))
    client = FakeClient(positions=[matching_position()])
    client.subscribe_calls.append((1,))
    svc = ExecutionService(client, reopened, ["BTCUSDT"])
    result = await svc.reconcile("di-unknown")
    assert result["state"] == "RESOLVED"
    assert reopened.balance("experiment", "USDT") == 300
    assert reopened.balance("experiment", "USDT", "DI:position-1") == 700
    await svc.reconcile("di-unknown")
    assert reopened.balance("experiment", "USDT") == 300
    assert len(client.subscribe_calls) == 1


@pytest.mark.asyncio
async def test_guard_failed_after_redemption_recovers_spot_from_unique_history(tmp_path):
    from test_investment import FakeClient, product
    ledger = Ledger(str(tmp_path / "earn.sqlite"), {"experiment": {"allocation": "1000", "mode": "live", "location": "EARN:USDT001"}})
    p = {"amount": "700", "earnProductId": "USDT001", "dualProductId": "2650584", "optionType": "PUT", "investCoin": "USDT", "exercisedCoin": "BTC"}
    payload = {"operation": "earn_to_dual", "params": p}
    preview = {"contract": {"product": product(), "previousPositionIds": []}, "historyStart": 1}
    ledger.begin("redeemed", "experiment", payload, "USDT", "EARN:USDT001", "700",
                 prepared={"preview": preview, "phase": "earn_redeem_accepted", "checkpoint": {"redeemResponse": {"redeemId": "r1"}}})
    client = FakeClient()
    client.spot_balances = ["700"]
    original = client.signed_get
    async def signed(product_name, path, params=None):
        if path.endswith("redemptionRecord"):
            return {"total": 1, "rows": [{"redeemId": "r1", "projectId": "USDT001", "asset": "USDT", "amount": "700", "status": "PAID"}]}
        return await original(product_name, path, params)
    client.signed_get = signed
    result = await ExecutionService(client, ledger, ["BTCUSDT"]).reconcile("redeemed")
    assert result["state"] == "RESOLVED"
    assert ledger.balance("experiment", "USDT") == 700
    assert ledger.balance("experiment", "USDT", "EARN:USDT001") == 300
    assert not client.subscribe_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_price", ["95", "110"])
async def test_exit_sale_books_gains_losses_and_reinvest_policy(tmp_path, exit_price):
    ledger, _, svc = setup(tmp_path, reinvest=False)
    preview = await svc.preview("experiment", "otoco", plan())
    intent, _ = ledger.begin("pnl", "experiment", {}, "USDT", "SPOT", preview["reservation"], prepared={"preview": preview})
    svc.apply_fill(intent, fill(), preview, paper=True)
    svc.apply_fill(intent, fill(key=2, qty="0.499", side=False, price=exit_price, commission=str(Decimal("0.499") * Decimal(exit_price) * Decimal("0.001")), fee_asset="USDT"), preview, paper=True)
    assert (ledger.balance("experiment", "USDT", "PROFIT_RESERVE") > 0) == (exit_price == "110")
    assert ledger.balance("experiment", "USDT") < 100
    report = ledger.report("experiment", {"BTC": exit_price})
    assert (Decimal(report["resultsByCategory"]["realized"]) > 0) == (exit_price == "110")
    assert Decimal(report["resultsByCategory"]["commission"]) > 0

@pytest.mark.asyncio
async def test_partial_paper_cancel_then_owned_oco_repair_clears_entry_pause(tmp_path):
    ledger, exchange, svc = setup(tmp_path)
    result = await svc.execute("experiment", "partial-paper", "otoco", plan())
    t = result["created"] + 60000
    exchange.candles = [[t, "100", "101", "99", "100", "1", t+1]]
    from unittest.mock import patch
    with patch("time.time", return_value=(t+60000)/1000):
        await svc.reconcile("partial-paper")
    assert ledger.meta("paperPause:experiment")
    await svc.execute("experiment", "cancel-partial", "cancel", {"targetIntentId": "partial-paper"})
    result = await svc.execute("experiment", "repair", "oco", plan(quantity="0.099"))
    assert result["result"]["protected"]
    assert not ledger.meta("paperPause:experiment")
    assert not exchange.writes


@pytest.mark.asyncio
async def test_monitor_complete_reconciliation_clears_only_operational_pause(tmp_path):
    ledger, _, svc = setup(tmp_path, "live")
    ledger.pause("monitor starting; account reconciliation required")
    monitor = Monitor(svc)
    await monitor.tick()
    assert not ledger.meta("pause")
    assert ledger.meta("monitorAt")
    ledger.pause("owner audit required")
    await monitor.tick()
    assert ledger.meta("pause") == "owner audit required"


@pytest.mark.asyncio
async def test_third_asset_fee_discount_fails_preflight_before_submission(tmp_path):
    _, exchange, svc = setup(tmp_path, "live")
    original = exchange.signed_get
    async def signed(product, path, params=None):
        result = await original(product, path, params)
        if path.endswith("/commission"):
            result["discount"] = {"enabledForAccount": True, "enabledForSymbol": True, "discountAsset": "BNB"}
        return result
    exchange.signed_get = signed
    with pytest.raises(BinanceClientError, match="third-asset"):
        await svc.execute("experiment", "bnb", "otoco", plan())
    assert not exchange.writes
