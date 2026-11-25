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
        self.fee_rate = "0.00101"
        self.discount_enabled = False
        self.bid, self.ask = "0.9979", "0.998"
        self.swap_failure = None
        self.fills = None
        self.fill_status = "EXPIRED"

    def _require_trading(self):
        return None

    async def signed_get(self, _, path, params=None):
        params = params or {}
        if path == "/api/v3/account":
            return {"canTrade": True, "permissions": ["SPOT"], "balances": [{"asset": asset, "free": value, "locked": "0"}
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
        if path == "/api/v3/account/commission":
            result = {key: {"maker": "0", "taker": self.fee_rate if key == "standardCommission" else "0", "buyer": "0", "seller": "0"} for key in ("standardCommission", "taxCommission", "specialCommission")}
            result["discount"] = {"enabledForAccount": self.discount_enabled, "enabledForSymbol": self.discount_enabled}
            return result
        if path == "/api/v3/myTrades":
            if self.fills is not None:
                return self.fills
            return [{"id": 101, "orderId": 19, "isBuyer": False,
                     "qty": "500", "quoteQty": "498.95",
                     "commissionAsset": "USDC", "commission": "0.499"}]
        raise AssertionError(path)

    async def public_get(self, _, path, params=None):
        if path == "/api/v3/exchangeInfo":
            symbol = params["symbol"]
            if symbol not in ("USD1USDC", "USDCUSDT"):
                raise BinanceClientError("invalid symbol", status=400, code=-1121)
            base, quote = ("USD1", "USDC") if symbol == "USD1USDC" else ("USDC", "USDT")
            return {"symbols": [{"symbol": symbol, "baseAsset": base,
                                 "quoteAsset": quote, "status": "TRADING",
                                 "isSpotTradingAllowed": True, "orderTypes": ["LIMIT"],
                                 "allowedSelfTradePreventionModes": ["EXPIRE_TAKER"],
                                 "filters": [
                                     {"filterType": "PRICE_FILTER", "tickSize": "0.0001",
                                      "minPrice": "0.8", "maxPrice": "1.2"},
                                     {"filterType": "LOT_SIZE", "stepSize": "1",
                                      "minQty": "1", "maxQty": "10000000"},
                                     {"filterType": "NOTIONAL", "minNotional": "5"}]}]}
        if path == "/api/v3/ticker/bookTicker":
            return {"bidPrice": self.bid, "askPrice": self.ask}
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
        if self.swap_failure and self.swap_failure.definitive_rejection:
            raise self.swap_failure
        executed = sum((Decimal(t["qty"]) for t in self.fills), Decimal(0)) if self.fills is not None else Decimal("500")
        quote = sum((Decimal(t["quoteQty"]) for t in self.fills), Decimal(0)) if self.fills is not None else Decimal("498.95")
        self.last_order = {"symbol": params["symbol"], "clientOrderId": params["newClientOrderId"],
                           "orderId": 19, "side": params["side"], "type": "LIMIT",
                           "timeInForce": "IOC", "origQty": params["quantity"],
                           "price": params["price"], "status": self.fill_status,
                           "executedQty": str(executed), "cummulativeQuoteQty": str(quote)}
        if self.swap_failure:
            raise self.swap_failure
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
    assert preview["contract"]["applicableAdvertisedApr"] == "0.09099"
    assert preview["contract"]["bonusTierApr"] == "0.07"
    assert preview["contract"]["promotionalTier"] == "0-10000USDC"
    with pytest.raises(BinanceClientError, match="APR"):
        await svc.preview("subscribe", "USDC", "498.451", product_id="USDC001", minimum_apr="0.10")
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


def setup_buy(account, rate="0", fills=None):
    account.free["USDT"] = "913.88"
    account.bid, account.ask = "1.0010", "1.0011"
    account.fee_rate = rate
    account.fills = fills if fills is not None else [
        {"id": 201, "orderId": 19, "isBuyer": True, "qty": "912",
         "quoteQty": "913.0032", "commissionAsset": "USDC", "commission": "0"}
    ]


@pytest.mark.asyncio
async def test_inverse_buy_rounds_down_and_records_exact_source_debit(owner_service):
    svc, account, ledger = owner_service
    setup_buy(account)
    account.fill_status = "FILLED"
    ledger.set_meta("expectedTotals", {"USDT": "1063.88", "USDC": "0"})
    preview = await svc.preview("swap", "USDT", "913.88", target_asset="USDC", minimum_price="0.998")
    contract = preview["contract"]
    assert contract["symbol"] == "USDCUSDT"
    assert contract["side"] == "BUY"
    assert contract["quantity"] == "912"
    assert Decimal(contract["maxSourceDebit"]) <= Decimal("913.88")
    result = await svc.execute("inverse-buy", "swap", "USDT", "913.88", target_asset="USDC",
                               minimum_price="0.998", acknowledge_account_funds=True)
    assert result["state"] == "RESOLVED"
    assert account.orders[0][3]["quantity"] == "912"
    assert account.orders[0][3]["side"] == "BUY"
    assert account.orders[0][3]["selfTradePreventionMode"] == "EXPIRE_TAKER"
    verified = result["response"]["verified"]
    assert Decimal(verified["sourceDebited"]) == Decimal("913.0032")
    assert Decimal(verified["unspentSource"]) == Decimal("0.8768")
    assert verified["netReceived"] == "912"
    assert Decimal(ledger.meta("expectedTotals")["USDT"]) == Decimal("150.8768")
    assert ledger.balance("main", "USDT", "EARN:USDT001") == Decimal("100")
    assert (await svc.reconcile("inverse-buy"))["state"] == "RESOLVED"
    assert len(account.orders) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fee_asset,fee", [("USDT", "0.50055"), ("USDC", "0.5")])
async def test_inverse_buy_fee_reserve_and_partial_fill(owner_service, fee_asset, fee):
    svc, account, ledger = owner_service
    setup_buy(account, "0.001", [{"id": 201, "orderId": 19, "isBuyer": True,
                                    "qty": "500", "quoteQty": "500.55",
                                    "commissionAsset": fee_asset, "commission": fee}])
    ledger.set_meta("expectedTotals", {"USDT": "1063.88", "USDC": "0"})
    result = await svc.execute("inverse-fees", "swap", "USDT", "913.88", target_asset="USDC",
                               minimum_price="0.998", acknowledge_account_funds=True)
    assert result["state"] == "RESOLVED"
    contract = result["preview"]["contract"]
    assert contract["quantity"] == "911"
    assert Decimal(contract["maxSourceDebit"]) <= Decimal("913.88")
    verified = result["response"]["verified"]
    expected_debit = Decimal("500.55") + (Decimal(fee) if fee_asset == "USDT" else 0)
    expected_received = Decimal("500") - (Decimal(fee) if fee_asset == "USDC" else 0)
    assert Decimal(verified["sourceDebited"]) == expected_debit
    assert Decimal(verified["netReceived"]) == expected_received
    assert Decimal(ledger.meta("expectedTotals")["USDT"]) == Decimal("1063.88") - expected_debit
    assert Decimal(ledger.meta("expectedTotals")["USDC"]) == expected_received


@pytest.mark.asyncio
async def test_inverse_buy_zero_fill(owner_service):
    svc, account, _ = owner_service
    setup_buy(account, fills=[])
    account.fill_status = "EXPIRED_IN_MATCH"
    result = await svc.execute("zero-fill", "swap", "USDT", "913.88", target_asset="USDC",
                               minimum_price="0.998", acknowledge_account_funds=True)
    assert result["state"] == "RESOLVED"
    assert result["response"]["verified"]["netReceived"] == "0"
    assert result["response"]["verified"]["unspentSource"] == "913.88"


@pytest.mark.asyncio
async def test_inverse_buy_reject_and_unknown_reconcile(owner_service):
    svc, account, _ = owner_service
    setup_buy(account)
    account.swap_failure = BinanceClientError("rejected", status=400, code=-1013)
    args = dict(target_asset="USDC", minimum_price="0.998", acknowledge_account_funds=True)
    assert (await svc.execute("rejected", "swap", "USDT", "913.88", **args))["state"] == "REJECTED"
    account.swap_failure = BinanceClientError("timeout", outcome_unknown=True)
    assert (await svc.execute("uncertain-buy", "swap", "USDT", "913.88", **args))["state"] == "OUTCOME_UNKNOWN"
    assert (await svc.execute("uncertain-buy", "swap", "USDT", "913.88", **args))["state"] == "OUTCOME_UNKNOWN"
    assert len(account.orders) == 2
    assert (await svc.reconcile("uncertain-buy"))["state"] == "RESOLVED"
    assert len(account.orders) == 2


@pytest.mark.asyncio
async def test_inverse_buy_rechecks_price_and_funds_before_submission(owner_service):
    svc, account, _ = owner_service
    setup_buy(account)
    await svc.preview("swap", "USDT", "913.88", target_asset="USDC", minimum_price="0.998")
    account.bid, account.ask = "1.0030", "1.0031"
    with pytest.raises(BinanceClientError, match="price or spread"):
        await svc.execute("deteriorated", "swap", "USDT", "913.88", target_asset="USDC",
                          minimum_price="0.998", acknowledge_account_funds=True)
    account.free["USDT"] = "900"
    with pytest.raises(BinanceClientError, match="outside strategy allocations"):
        await svc.preview("swap", "USDT", "913.88", target_asset="USDC", minimum_price="0.998")
    assert not account.orders


@pytest.mark.asyncio
async def test_inverse_buy_below_notional(owner_service):
    svc, account, _ = owner_service
    setup_buy(account)
    with pytest.raises(BinanceClientError, match="notional"):
        await svc.preview("swap", "USDT", "4", target_asset="USDC", minimum_price="0.998")


@pytest.mark.asyncio
async def test_direct_pair_transport_failure_does_not_fallback(owner_service):
    svc, account, _ = owner_service
    setup_buy(account)
    async def unavailable(*args, **kwargs):
        raise BinanceClientError("timeout", outcome_unknown=True)
    account.public_get = unavailable
    with pytest.raises(BinanceClientError, match="timeout"):
        await svc.preview("swap", "USDT", "913.88", target_asset="USDC", minimum_price="0.998")
    assert not account.orders


@pytest.mark.asyncio
async def test_zero_commission_discount_does_not_block_inverse_pair(owner_service):
    svc, account, _ = owner_service
    setup_buy(account)
    account.discount_enabled = True
    preview = await svc.preview("swap", "USDT", "913.88", target_asset="USDC", minimum_price="0.998")
    assert preview["contract"]["feeRateBound"] == "0"
    account.fee_rate = "0.001"
    with pytest.raises(BinanceClientError, match="third-asset"):
        await svc.preview("swap", "USDT", "913.88", target_asset="USDC", minimum_price="0.998")


@pytest.mark.asyncio
async def test_inverse_buy_protects_spot_claims_and_reservations():
    ledger = Ledger(":memory:", {
        "main": {"fundingSource": "shared_account", "budgetCap": "100",
                 "quote": "USDT", "mode": "live", "location": "SPOT", "reinvest": False}
    })
    account = FakeAccount()
    setup_buy(account)
    svc = AccountEarnService(account, ledger)
    assert (await svc.preview("swap", "USDT", "813.88", target_asset="USDC",
                              minimum_price="0.998"))["protectedStrategyFunds"] == "100"
    with pytest.raises(BinanceClientError, match="outside strategy allocations"):
        await svc.execute("protected-spot", "swap", "USDT", "813.89", target_asset="USDC",
                          minimum_price="0.998", acknowledge_account_funds=True)
    assert account.orders == []
    assert ledger.balance("main", "USDT", "SPOT") == Decimal("100")
