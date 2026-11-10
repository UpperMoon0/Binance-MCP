"""Owner-account Earn operations must not bypass or mutate strategy ownership."""
import time
from decimal import Decimal

import pytest

from binance_mcp.account_earn import AccountEarnService
from binance_mcp.client import BinanceClientError
from binance_mcp.ledger import Ledger


class FakeAccount:
    def __init__(self):
        self.free = {"USD1": "0", "USDC": "0", "USDT": "0"}
        self.earn = {"USD1": Decimal("796.57856741"), "USDT": Decimal("150")}
        self.earn_writes = []
        self.orders = []
        self.last_order = None
        self.unknown = False
        self.history_enabled = True

    def _require_trading(self):
        return None

    async def signed_get(self, _, path, params=None):
        params = params or {}
        if path == "/api/v3/account":
            return {"balances": [{"asset": asset, "free": value, "locked": "0"}
                                 for asset, value in self.free.items()]}
        if path == "/sapi/v1/simple-earn/flexible/position":
            rows = []
            for asset, balance in self.earn.items():
                rows.append({"asset": asset, "productId": "USD1001" if asset == "USD1" else "USDT001",
                             "totalAmount": str(balance), "canRedeem": True})
            return {"total": len(rows), "rows": rows}
        if path == "/sapi/v1/simple-earn/flexible/list":
            rows = [{"asset": "USDC", "productId": "USDC001", "status": "PURCHASING",
                     "canPurchase": True, "isSoldOut": False, "minPurchaseAmount": "0.01",
                     "subscriptionStartTime": 1, "latestAnnualPercentageRate": "0.02099",
                     "tierAnnualPercentageRate": {"0-10000USDC": "0.07"}}]
            return {"total": 1, "rows": rows}
        if path.endswith("/redemptionRecord") or path.endswith("/subscriptionRecord"):
            if not self.history_enabled:
                return {"total": 0, "rows": []}
            is_redeem = "redemptionRecord" in path
            return {"total": 1, "rows": [{
                "productId": "USD1001" if is_redeem else "USDC001",
                "asset": "USD1" if is_redeem else "USDC",
                "amount": "796.57856741" if is_redeem else "498.451",
                "status": "PAID" if is_redeem else "SUCCESS",
                "redeemId" if is_redeem else "purchaseId": "R1" if is_redeem else "S1",
            }]}
        if path == "/api/v3/order":
            assert params["origClientOrderId"] == self.last_order["clientOrderId"]
            return self.last_order
        if path == "/api/v3/myTrades":
            return [{"id": 101, "orderId": 19, "isBuyer": False,
                     "qty": "500", "quoteQty": "498.95",
                     "commissionAsset": "USDC", "commission": "0.499"}]
        raise AssertionError(path)

    async def public_get(self, _, path, params=None):
        if path == "/api/v3/exchangeInfo":
            return {"symbols": [{"symbol": "USD1USDC", "baseAsset": "USD1",
                                 "quoteAsset": "USDC", "status": "TRADING",
                                 "isSpotTradingAllowed": True, "orderTypes": ["LIMIT"],
                                 "filters": [
                                     {"filterType": "PRICE_FILTER", "tickSize": "0.0001",
                                      "minPrice": "0.8", "maxPrice": "1.2"},
                                     {"filterType": "LOT_SIZE", "stepSize": "1",
                                      "minQty": "1", "maxQty": "10000000"},
                                     {"filterType": "NOTIONAL", "minNotional": "5"}]}]}
        if path == "/api/v3/ticker/bookTicker":
            return {"bidPrice": "0.9979", "askPrice": "0.998"}
        raise AssertionError(path)

    async def simple_earn_redeem(self, product_id, amount, dest_account):
        self.earn_writes.append(("redeem", product_id, amount, dest_account))
        if self.unknown:
            raise BinanceClientError("uncertain", outcome_unknown=True)
        self.earn["USD1"] -= Decimal(amount)
        self.free["USD1"] = str(Decimal(self.free["USD1"]) + Decimal(amount))
        return {"redeemId": "R1", "success": True}

    async def simple_earn_subscribe(self, product_id, amount, auto_subscribe, source_account):
        self.earn_writes.append(("subscribe", product_id, amount, auto_subscribe, source_account))
        return {"purchaseId": "S1", "success": True}

    async def order(self, product, path, action, params):
        self.orders.append((product, path, action, params))
        self.last_order = {"symbol": params["symbol"], "clientOrderId": params["newClientOrderId"],
                           "orderId": 19, "side": "SELL", "type": "LIMIT",
                           "timeInForce": "IOC", "origQty": params["quantity"],
                           "price": params["price"], "status": "EXPIRED",
                           "executedQty": "500", "cummulativeQuoteQty": "498.95"}
        return {"orderId": 19, "clientOrderId": params["newClientOrderId"]}


@pytest.fixture
def owner_service():
    ledger = Ledger(":memory:", {
        "main": {"fundingSource": "shared_account", "budgetCap": "100",
                 "quote": "USDT", "mode": "live", "location": "EARN:USDT001", "reinvest": False}
    })
    account = FakeAccount()
    return AccountEarnService(account, ledger), account, ledger


@pytest.mark.asyncio
async def test_owner_redeems_existing_earn_without_strategy_attribution(owner_service):
    svc, account, ledger = owner_service
    result = await svc.execute("account-usd1-redeem", "redeem", "USD1", "796.57856741",
                               product_id="USD1001", acknowledge_account_funds=True)
    assert result["state"] == "RESOLVED"
    assert result["response"]["verified"]["destination"] == "SPOT"
    assert ledger.balance("main", "USDT", "EARN:USDT001") == Decimal(100)
    identical = await svc.execute("account-usd1-redeem", "redeem", "USD1", "796.57856741",
                                  product_id="USD1001", acknowledge_account_funds=True)
    assert identical["state"] == "RESOLVED"
    assert len(account.earn_writes) == 1
    with pytest.raises(BinanceClientError, match="different contract"):
        await svc.execute("account-usd1-redeem", "redeem", "USD1", "1",
                          product_id="USD1001", acknowledge_account_funds=True)


@pytest.mark.asyncio
async def test_strategy_earn_capital_cannot_be_used_as_owner_funds(owner_service):
    svc, account, ledger = owner_service
    with pytest.raises(BinanceClientError, match="outside strategy allocations"):
        await svc.execute("account-protected", "redeem", "USDT", "100",
                          product_id="USDT001", acknowledge_account_funds=True)
    assert account.earn_writes == []


@pytest.mark.asyncio
async def test_uncertain_redemption_never_retries_and_blocks_another(owner_service):
    svc, account, _ = owner_service
    account.unknown = True
    first = await svc.execute("account-unknown", "redeem", "USD1", "100",
                              product_id="USD1001", acknowledge_account_funds=True)
    assert first["state"] == "OUTCOME_UNKNOWN"
    again = await svc.execute("account-unknown", "redeem", "USD1", "100",
                              product_id="USD1001", acknowledge_account_funds=True)
    assert again["state"] == "OUTCOME_UNKNOWN"
    assert len(account.earn_writes) == 1
    with pytest.raises(BinanceClientError, match="requires reconciliation"):
        await svc.execute("another", "redeem", "USD1", "100",
                          product_id="USD1001", acknowledge_account_funds=True)


@pytest.mark.asyncio
async def test_explicit_account_acknowledgement_and_apr_floor(owner_service):
    svc, account, _ = owner_service
    account.free["USDC"] = "600"
    preview = await svc.preview("subscribe", "USDC", "498.451", product_id="USDC001", minimum_apr="0.07")
    assert preview["contract"]["applicableAdvertisedApr"] == "0.07"
    assert preview["contract"]["promotionalTier"] == "0-10000USDC"
    with pytest.raises(BinanceClientError, match="APR"):
        await svc.preview("subscribe", "USDC", "498.451", product_id="USDC001", minimum_apr="0.08")
    with pytest.raises(BinanceClientError, match="acknowledgement"):
        await svc.execute("no-confirm", "subscribe", "USDC", "498.451",
                          product_id="USDC001")
    assert account.earn_writes == []


@pytest.mark.asyncio
async def test_price_floored_ioc_records_actual_partial_fill_and_reconciles_once(owner_service):
    svc, account, ledger = owner_service
    account.free["USD1"] = "796.57856741"
    ledger.set_meta("expectedTotals", {"USD1": "796.57856741", "USDC": "0", "USDT": "150"})
    with pytest.raises(BinanceClientError, match="price or spread"):
        await svc.preview("swap", "USD1", "796", target_asset="USDC", minimum_price="0.999")
    result = await svc.execute("account-usd1-to-usdc", "swap", "USD1", "796",
                               target_asset="USDC", minimum_price="0.997",
                               acknowledge_account_funds=True)
    assert result["state"] == "RESOLVED"
    assert result["response"]["verified"]["sold"] == "500"
    assert result["response"]["verified"]["netReceived"] == "498.451"
    assert len(account.orders) == 1
    same = await svc.reconcile("account-usd1-to-usdc")
    assert same["state"] == "RESOLVED"
    assert len(account.orders) == 1
    expected = ledger.meta("expectedTotals")
    assert Decimal(expected["USD1"]) == Decimal("296.57856741")
    assert Decimal(expected["USDC"]) == Decimal("498.451")


@pytest.mark.asyncio
async def test_account_owned_subscription_confirmed_by_history(owner_service):
    svc, account, _ = owner_service
    account.free["USDC"] = "600"
    result = await svc.execute("account-usdc-earn", "subscribe", "USDC", "498.451",
                               product_id="USDC001", minimum_apr="0.07",
                               acknowledge_account_funds=True)
    assert result["state"] == "RESOLVED"
    assert account.earn_writes == [("subscribe", "USDC001", "498.451", False, "SPOT")]


@pytest.mark.asyncio
async def test_missing_receipt_is_not_assumed_success(owner_service):
    svc, account, _ = owner_service
    account.history_enabled = False
    result = await svc.execute("account-no-history", "redeem", "USD1", "796.57856741",
                               product_id="USD1001", acknowledge_account_funds=True)
    assert result["state"] == "OUTCOME_UNKNOWN"
    assert len(account.earn_writes) == 1
    result = await svc.reconcile("account-no-history")
    assert result["state"] == "OUTCOME_UNKNOWN"
    assert len(account.earn_writes) == 1
