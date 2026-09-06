from types import SimpleNamespace

import pytest

from binance_mcp.client import BinanceClientError
from binance_mcp.investment import InvestmentService


def product(apr="0.2008", *, can_purchase=True, strike="77500"):
    return {
        "id": "2650584",
        "investCoin": "USDT",
        "exercisedCoin": "BTC",
        "strikePrice": strike,
        "duration": 3,
        "settleDate": 9999999999999,
        "purchaseEndTime": 9999999999999,
        "canPurchase": can_purchase,
        "apr": apr,
        "orderId": 51015871919,
        "minAmount": "10",
        "maxAmount": "3433948",
        "optionType": "PUT",
        "isAutoCompoundEnable": False,
        "autoCompoundPlanList": [],
    }


class FakeClient:
    def __init__(self, *, product_sequence=None, positions=None, trading_enabled=True, spot_prices=None):
        self.config = SimpleNamespace(trading_enabled=trading_enabled)
        self.product_sequence = list(product_sequence or [product()])
        self.product_calls = 0
        self.positions_data = positions if positions is not None else []
        self.spot_prices = list(spot_prices or ["79756"])
        self.spot_price_calls = 0
        self.spot_balances = ["0", "700"]
        self.spot_balance_calls = 0
        self.subscribe_calls = []
        self.redeem_calls = []

    def _require_trading(self):
        if not self.config.trading_enabled:
            raise BinanceClientError("trading is disabled by deployment policy")

    async def signed_get(self, product_name, path, params=None):
        if path == "/sapi/v1/simple-earn/flexible/position":
            return {
                "total": 1,
                "rows": [{
                    "productId": "USDT001",
                    "totalAmount": "1306.61",
                    "canRedeem": True,
                }],
            }
        if path == "/sapi/v1/dci/product/list":
            index = min(self.product_calls, len(self.product_sequence) - 1)
            current = self.product_sequence[index]
            self.product_calls += 1
            if current is None:
                return {"total": 0, "list": []}
            return {"total": 1, "list": [current]}
        if path == "/api/v3/account":
            index = min(self.spot_balance_calls, len(self.spot_balances) - 1)
            free = self.spot_balances[index]
            self.spot_balance_calls += 1
            return {"balances": [{"asset": "USDT", "free": free, "locked": "0"}]}
        if path == "/sapi/v1/dci/product/positions":
            return {"total": len(self.positions_data), "list": self.positions_data}
        raise AssertionError(f"unexpected signed GET {path}")

    async def public_get(self, product_name, path, params=None):
        assert path == "/api/v3/ticker/price"
        index = min(self.spot_price_calls, len(self.spot_prices) - 1)
        value = self.spot_prices[index]
        self.spot_price_calls += 1
        return {"symbol": params["symbol"], "price": value}

    async def simple_earn_redeem(self, product_id, amount, dest_account="SPOT"):
        self._require_trading()
        self.redeem_calls.append((product_id, amount, dest_account))
        return {"success": True, "redeemId": "r1"}

    async def dual_investment_subscribe(self, product_id, order_id, deposit_amount, auto_compound_plan="NONE"):
        self._require_trading()
        self.subscribe_calls.append((product_id, order_id, deposit_amount, auto_compound_plan))
        return {
            "id": "position-1",
            "orderId": order_id,
            "subscriptionAmount": deposit_amount,
            "purchaseStatus": "PURCHASE_SUCCESS",
        }


def matching_position(apr="0.2008"):
    return {
        "id": "position-1",
        "investCoin": "USDT",
        "exercisedCoin": "BTC",
        "subscriptionAmount": "700",
        "strikePrice": "77500",
        "duration": 3,
        "settleDate": 9999999999999,
        "purchaseStatus": "PURCHASE_SUCCESS",
        "apr": apr,
        "orderId": 51015871919,
        "purchaseEndTime": 9999999999999,
        "optionType": "PUT",
        "autoCompoundPlan": "NONE",
        "subscriptionTime": 1,
    }


@pytest.mark.asyncio
async def test_minimum_apr_guard():
    service = InvestmentService(FakeClient())
    with pytest.raises(BinanceClientError, match="APR fell below minimumApr"):
        await service.validate_dual_product(product(apr="0.17"), "700", minimum_apr="0.18")


@pytest.mark.asyncio
async def test_minimum_strike_distance_guard():
    service = InvestmentService(FakeClient(spot_prices=["79000"]))
    with pytest.raises(BinanceClientError, match="strike distance fell below"):
        await service.validate_dual_product(
            product(strike="77500"),
            "700",
            minimum_strike_distance_percent="2.0",
        )


@pytest.mark.asyncio
async def test_preserve_earn_balance_guard():
    service = InvestmentService(FakeClient(positions=[matching_position()]))
    with pytest.raises(BinanceClientError, match="preserveEarnAmount guard failed"):
        await service.from_flexible_earn(
            earn_product_id="USDT001",
            dual_product_id="2650584",
            option_type="PUT",
            exercised_coin="BTC",
            invest_coin="USDT",
            amount="900",
            preserve_earn_amount="500",
        )


@pytest.mark.asyncio
async def test_mock_flow_1306_61_preserve_500_redeem_700_subscribes_and_verifies():
    client = FakeClient(
        product_sequence=[product(apr="0.2008"), product(apr="0.2008")],
        positions=[matching_position(apr="0.2008")],
        spot_prices=["79756", "79756"],
    )
    service = InvestmentService(client)
    result = await service.from_flexible_earn(
        earn_product_id="USDT001",
        dual_product_id="2650584",
        option_type="PUT",
        exercised_coin="BTC",
        invest_coin="USDT",
        amount="700",
        preserve_earn_amount="500",
        minimum_apr="0.18",
        minimum_strike_distance_percent="2.0",
        auto_compound_plan="NONE",
    )
    assert result["redeemed"] is True
    assert result["subscribed"] is True
    assert result["verified"] is True
    assert result["earnRemaining"] == "606.61"
    assert client.redeem_calls == [("USDT001", "700", "SPOT")]
    assert client.subscribe_calls == [("2650584", 51015871919, "700", "NONE")]


@pytest.mark.asyncio
async def test_apr_drop_after_redemption_leaves_funds_in_spot_and_does_not_subscribe():
    client = FakeClient(
        product_sequence=[product(apr="0.2008"), product(apr="0.15")],
        positions=[],
        spot_prices=["79756", "79756"],
    )
    service = InvestmentService(client)
    result = await service.from_flexible_earn(
        earn_product_id="USDT001",
        dual_product_id="2650584",
        option_type="PUT",
        exercised_coin="BTC",
        invest_coin="USDT",
        amount="700",
        preserve_earn_amount="500",
        minimum_apr="0.18",
        minimum_strike_distance_percent="2.0",
    )
    assert result["redeemed"] is True
    assert result["subscribed"] is False
    assert result["fundsLocation"] == "SPOT"
    assert "APR fell below minimumApr" in result["reason"]
    assert client.subscribe_calls == []


@pytest.mark.asyncio
async def test_product_unavailable_after_redemption_does_not_substitute():
    client = FakeClient(
        product_sequence=[product(apr="0.2008"), None],
        positions=[],
        spot_prices=["79756"],
    )
    service = InvestmentService(client)
    result = await service.from_flexible_earn(
        earn_product_id="USDT001",
        dual_product_id="2650584",
        option_type="PUT",
        exercised_coin="BTC",
        invest_coin="USDT",
        amount="700",
        preserve_earn_amount="500",
        minimum_apr="0.18",
    )
    assert result["redeemed"] is True
    assert result["subscribed"] is False
    assert result["fundsLocation"] == "SPOT"
    assert "no longer available" in result["reason"]
    assert client.subscribe_calls == []


@pytest.mark.asyncio
async def test_verification_failure_after_subscription_never_retries_write():
    client = FakeClient(product_sequence=[product()], positions=[])
    service = InvestmentService(client)
    result = await service.subscribe_dual(
        product_id="2650584",
        option_type="PUT",
        exercised_coin="BTC",
        invest_coin="USDT",
        deposit_amount="700",
        auto_compound_plan="NONE",
        minimum_apr="0.18",
    )
    assert result["subscribed"] is True
    assert result["verified"] is False
    assert len(client.subscribe_calls) == 1
    assert "not verified" in result["reason"]


@pytest.mark.asyncio
async def test_trading_disabled_blocks_financial_workflow_before_reads():
    service = InvestmentService(FakeClient(trading_enabled=False))
    with pytest.raises(BinanceClientError, match="trading is disabled"):
        await service.from_flexible_earn(
            earn_product_id="USDT001",
            dual_product_id="2650584",
            option_type="PUT",
            exercised_coin="BTC",
            invest_coin="USDT",
            amount="700",
            preserve_earn_amount="500",
        )
