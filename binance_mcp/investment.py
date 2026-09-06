from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from .client import BinanceClient, BinanceClientError

OptionType = Literal["PUT", "CALL"]
AutoCompoundPlan = Literal["NONE", "STANDARD", "ADVANCED"]
DestinationAccount = Literal["SPOT", "FUND"]

DUAL_PRODUCT_LIST_PATH = "/sapi/v1/dci/product/list"
DUAL_POSITIONS_PATH = "/sapi/v1/dci/product/positions"
FLEXIBLE_POSITION_PATH = "/sapi/v1/simple-earn/flexible/position"


def _decimal(name: str, value: str | int | float | Decimal, *, allow_zero: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BinanceClientError(f"{name} must be a valid decimal") from exc
    if not parsed.is_finite():
        raise BinanceClientError(f"{name} must be finite")
    if parsed < 0 or (parsed == 0 and not allow_zero):
        operator = ">= 0" if allow_zero else "> 0"
        raise BinanceClientError(f"{name} must be {operator}")
    return parsed


def _string_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class InvestmentService:
    def __init__(self, client: BinanceClient):
        self.client = client

    def _require_trading(self) -> None:
        self.client._require_trading()

    async def flexible_position(self, product_id: str) -> dict[str, Any]:
        response = await self.client.signed_get(
            "spot",
            FLEXIBLE_POSITION_PATH,
            {"productId": product_id, "size": 100},
        )
        rows = response.get("rows", []) if isinstance(response, dict) else []
        for row in rows:
            if str(row.get("productId", "")) == product_id:
                return row
        raise BinanceClientError(f"Flexible Earn product {product_id} was not found in account positions")

    async def redeem_flexible(
        self,
        product_id: str,
        amount: str,
        dest_account: DestinationAccount = "SPOT",
    ) -> dict[str, Any]:
        self._require_trading()
        parsed_amount = _decimal("amount", amount)
        if dest_account not in ("SPOT", "FUND"):
            raise BinanceClientError("destAccount must be SPOT or FUND")
        response = await self.client.simple_earn_redeem(product_id, _string_decimal(parsed_amount), dest_account)
        return {
            "productId": product_id,
            "amount": _string_decimal(parsed_amount),
            "destAccount": dest_account,
            "response": response,
        }

    async def _dual_product_page(
        self,
        option_type: OptionType,
        exercised_coin: str,
        invest_coin: str,
        page_index: int,
    ) -> dict[str, Any]:
        response = await self.client.signed_get(
            "spot",
            DUAL_PRODUCT_LIST_PATH,
            {
                "optionType": option_type,
                "exercisedCoin": exercised_coin,
                "investCoin": invest_coin,
                "pageSize": 100,
                "pageIndex": page_index,
            },
        )
        if not isinstance(response, dict):
            raise BinanceClientError("unexpected Dual Investment product-list response")
        return response

    async def dual_product(
        self,
        product_id: str,
        option_type: OptionType,
        exercised_coin: str,
        invest_coin: str,
    ) -> dict[str, Any]:
        for page_index in range(1, 21):
            response = await self._dual_product_page(option_type, exercised_coin, invest_coin, page_index)
            products = response.get("list", [])
            for product in products:
                if str(product.get("id", "")) == product_id:
                    return product
            total = int(response.get("total", 0) or 0)
            if not products or page_index * 100 >= total:
                break
        raise BinanceClientError(f"Dual Investment product {product_id} is no longer available")

    @staticmethod
    def _spot_symbol(product: dict[str, Any]) -> str:
        option_type = str(product.get("optionType", "")).upper()
        invest_coin = str(product.get("investCoin", "")).upper()
        exercised_coin = str(product.get("exercisedCoin", "")).upper()
        if option_type == "PUT":
            return f"{exercised_coin}{invest_coin}"
        if option_type == "CALL":
            return f"{invest_coin}{exercised_coin}"
        raise BinanceClientError("Dual Investment product has an unsupported optionType")

    async def _spot_price(self, product: dict[str, Any]) -> Decimal:
        symbol = self._spot_symbol(product)
        response = await self.client.public_get("spot", "/api/v3/ticker/price", {"symbol": symbol})
        if not isinstance(response, dict) or "price" not in response:
            raise BinanceClientError(f"could not read live spot price for {symbol}")
        return _decimal("spot price", response["price"])

    async def validate_dual_product(
        self,
        product: dict[str, Any],
        deposit_amount: str,
        minimum_apr: str | None = None,
        minimum_strike_distance_percent: str | None = None,
        auto_compound_plan: AutoCompoundPlan = "NONE",
    ) -> dict[str, Any]:
        amount = _decimal("depositAmount", deposit_amount)
        if not bool(product.get("canPurchase")):
            raise BinanceClientError("Dual Investment product is not currently purchasable")

        min_amount = _decimal("product minAmount", product.get("minAmount", "0"), allow_zero=True)
        max_amount = _decimal("product maxAmount", product.get("maxAmount", "0"), allow_zero=True)
        if amount < min_amount or (max_amount > 0 and amount > max_amount):
            raise BinanceClientError(
                f"depositAmount {_string_decimal(amount)} is outside product range "
                f"{_string_decimal(min_amount)}..{_string_decimal(max_amount)}"
            )

        purchase_end_time = int(product.get("purchaseEndTime", 0) or 0)
        if purchase_end_time <= int(time.time() * 1000):
            raise BinanceClientError("Dual Investment product purchase window has ended")

        apr = _decimal("product APR", product.get("apr", "0"), allow_zero=True)
        if minimum_apr is not None:
            minimum = _decimal("minimumApr", minimum_apr, allow_zero=True)
            if apr < minimum:
                raise BinanceClientError(
                    f"APR fell below minimumApr: current={_string_decimal(apr)}, minimum={_string_decimal(minimum)}"
                )

        if auto_compound_plan not in ("NONE", "STANDARD", "ADVANCED"):
            raise BinanceClientError("autoCompoundPlan must be NONE, STANDARD, or ADVANCED")
        if auto_compound_plan != "NONE":
            available = {str(value).upper() for value in product.get("autoCompoundPlanList", [])}
            if auto_compound_plan not in available:
                raise BinanceClientError(f"autoCompoundPlan {auto_compound_plan} is not available for this product")

        spot_price: Decimal | None = None
        strike_distance: Decimal | None = None
        if minimum_strike_distance_percent is not None:
            minimum_distance = _decimal(
                "minimumStrikeDistancePercent",
                minimum_strike_distance_percent,
                allow_zero=True,
            )
            spot_price = await self._spot_price(product)
            strike = _decimal("strikePrice", product.get("strikePrice", "0"))
            option_type = str(product.get("optionType", "")).upper()
            if option_type == "PUT":
                strike_distance = (spot_price - strike) / spot_price * Decimal("100")
            elif option_type == "CALL":
                strike_distance = (strike - spot_price) / spot_price * Decimal("100")
            else:
                raise BinanceClientError("Dual Investment product has an unsupported optionType")
            if strike_distance < minimum_distance:
                raise BinanceClientError(
                    "strike distance fell below minimumStrikeDistancePercent: "
                    f"current={_string_decimal(strike_distance)}, minimum={_string_decimal(minimum_distance)}"
                )

        return {
            "product": product,
            "spotPrice": _string_decimal(spot_price) if spot_price is not None else None,
            "strikeDistancePercent": _string_decimal(strike_distance) if strike_distance is not None else None,
        }

    async def positions(
        self,
        status: str | None = None,
        page_size: int = 100,
        page_index: int = 1,
    ) -> dict[str, Any]:
        if page_size < 1 or page_size > 100:
            raise BinanceClientError("pageSize must be between 1 and 100")
        if page_index < 1:
            raise BinanceClientError("pageIndex must be >= 1")
        params: dict[str, str | int] = {"pageSize": page_size, "pageIndex": page_index}
        if status:
            params["status"] = status
        response = await self.client.signed_get("spot", DUAL_POSITIONS_PATH, params)
        if not isinstance(response, dict):
            raise BinanceClientError("unexpected Dual Investment positions response")
        normalized = [self._normalize_position(item) for item in response.get("list", [])]
        return {"total": response.get("total", len(normalized)), "positions": normalized}

    @staticmethod
    def _normalize_position(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "positionId": item.get("id"),
            "orderId": item.get("orderId"),
            "productId": item.get("productId"),
            "investCoin": item.get("investCoin"),
            "exercisedCoin": item.get("exercisedCoin"),
            "depositAmount": item.get("subscriptionAmount"),
            "strikePrice": item.get("strikePrice"),
            "apr": item.get("apr"),
            "settlementDate": item.get("settleDate"),
            "status": item.get("purchaseStatus"),
            "optionType": item.get("optionType"),
            "autoCompoundPlan": item.get("autoCompoundPlan"),
            "subscriptionTime": item.get("subscriptionTime"),
        }

    async def _verify_subscription(
        self,
        *,
        product: dict[str, Any],
        deposit_amount: str,
        auto_compound_plan: AutoCompoundPlan,
    ) -> dict[str, Any]:
        target_order_id = product.get("orderId")
        target_amount = _decimal("depositAmount", deposit_amount)
        response = await self.positions(page_size=100, page_index=1)
        for position in response["positions"]:
            if position.get("orderId") != target_order_id:
                continue
            if position.get("depositAmount") is None:
                continue
            if _decimal("position depositAmount", position["depositAmount"]) != target_amount:
                continue
            if str(position.get("strikePrice")) != str(product.get("strikePrice")):
                continue
            plan = position.get("autoCompoundPlan")
            if plan is not None and str(plan).upper() != auto_compound_plan:
                continue
            return position
        raise BinanceClientError("subscription POST succeeded but matching Dual Investment position was not verified")

    async def subscribe_dual(
        self,
        *,
        product_id: str,
        option_type: OptionType,
        exercised_coin: str,
        invest_coin: str,
        deposit_amount: str,
        auto_compound_plan: AutoCompoundPlan = "NONE",
        minimum_apr: str | None = None,
        minimum_strike_distance_percent: str | None = None,
    ) -> dict[str, Any]:
        self._require_trading()
        amount = _string_decimal(_decimal("depositAmount", deposit_amount))
        product = await self.dual_product(product_id, option_type, exercised_coin, invest_coin)
        guard = await self.validate_dual_product(
            product,
            amount,
            minimum_apr=minimum_apr,
            minimum_strike_distance_percent=minimum_strike_distance_percent,
            auto_compound_plan=auto_compound_plan,
        )
        order_id = product.get("orderId")
        if order_id is None:
            raise BinanceClientError("Dual Investment product is missing orderId")
        response = await self.client.dual_investment_subscribe(
            product_id,
            int(order_id),
            amount,
            auto_compound_plan,
        )
        try:
            position = await self._verify_subscription(
                product=product,
                deposit_amount=amount,
                auto_compound_plan=auto_compound_plan,
            )
        except BinanceClientError as exc:
            return {
                "subscribed": True,
                "verified": False,
                "reason": str(exc),
                "product": product,
                "guards": guard,
                "response": response,
            }
        return {
            "subscribed": True,
            "verified": True,
            "product": product,
            "guards": guard,
            "position": position,
            "response": response,
        }

    async def _spot_free(self, asset: str) -> Decimal:
        account = await self.client.signed_get("spot", "/api/v3/account", {"omitZeroBalances": True})
        balances = account.get("balances", []) if isinstance(account, dict) else []
        for balance in balances:
            if str(balance.get("asset", "")).upper() == asset.upper():
                return _decimal("Spot free balance", balance.get("free", "0"), allow_zero=True)
        return Decimal("0")

    async def from_flexible_earn(
        self,
        *,
        earn_product_id: str,
        dual_product_id: str,
        option_type: OptionType,
        exercised_coin: str,
        invest_coin: str,
        amount: str,
        preserve_earn_amount: str,
        minimum_apr: str | None = None,
        minimum_strike_distance_percent: str | None = None,
        auto_compound_plan: AutoCompoundPlan = "NONE",
    ) -> dict[str, Any]:
        self._require_trading()
        redeem_amount = _decimal("amount", amount)
        preserve = _decimal("preserveEarnAmount", preserve_earn_amount, allow_zero=True)

        earn = await self.flexible_position(earn_product_id)
        if not bool(earn.get("canRedeem")):
            raise BinanceClientError("Flexible Earn position is not currently redeemable")
        earn_total = _decimal("Flexible Earn totalAmount", earn.get("totalAmount", "0"), allow_zero=True)
        if redeem_amount > earn_total:
            raise BinanceClientError("requested redemption exceeds Flexible Earn balance")
        remaining = earn_total - redeem_amount
        if remaining < preserve:
            raise BinanceClientError(
                "preserveEarnAmount guard failed: "
                f"remaining={_string_decimal(remaining)}, preserve={_string_decimal(preserve)}"
            )

        product = await self.dual_product(dual_product_id, option_type, exercised_coin, invest_coin)
        preflight = await self.validate_dual_product(
            product,
            _string_decimal(redeem_amount),
            minimum_apr=minimum_apr,
            minimum_strike_distance_percent=minimum_strike_distance_percent,
            auto_compound_plan=auto_compound_plan,
        )

        spot_before = await self._spot_free(invest_coin)
        redeem_response = await self.client.simple_earn_redeem(
            earn_product_id,
            _string_decimal(redeem_amount),
            "SPOT",
        )
        spot_after = await self._spot_free(invest_coin)
        expected_after = spot_before + redeem_amount
        if spot_after < expected_after:
            return {
                "redeemed": True,
                "subscribed": False,
                "verified": False,
                "reason": "redemption was accepted but Spot receipt could not be verified",
                "fundsLocation": "UNKNOWN",
                "earnRemaining": _string_decimal(remaining),
                "spotBefore": _string_decimal(spot_before),
                "spotAfter": _string_decimal(spot_after),
                "redeemResponse": redeem_response,
                "preflight": preflight,
            }

        try:
            refreshed = await self.dual_product(dual_product_id, option_type, exercised_coin, invest_coin)
            refreshed_guard = await self.validate_dual_product(
                refreshed,
                _string_decimal(redeem_amount),
                minimum_apr=minimum_apr,
                minimum_strike_distance_percent=minimum_strike_distance_percent,
                auto_compound_plan=auto_compound_plan,
            )
        except BinanceClientError as exc:
            return {
                "redeemed": True,
                "subscribed": False,
                "verified": True,
                "reason": str(exc),
                "fundsLocation": "SPOT",
                "earnRemaining": _string_decimal(remaining),
                "spotBalance": _string_decimal(spot_after),
                "redeemResponse": redeem_response,
                "preflight": preflight,
            }

        order_id = refreshed.get("orderId")
        if order_id is None:
            return {
                "redeemed": True,
                "subscribed": False,
                "verified": True,
                "reason": "refreshed Dual Investment product is missing orderId",
                "fundsLocation": "SPOT",
                "earnRemaining": _string_decimal(remaining),
                "spotBalance": _string_decimal(spot_after),
                "redeemResponse": redeem_response,
                "preflight": preflight,
            }

        try:
            subscribe_response = await self.client.dual_investment_subscribe(
                dual_product_id,
                int(order_id),
                _string_decimal(redeem_amount),
                auto_compound_plan,
            )
        except BinanceClientError as exc:
            return {
                "redeemed": True,
                "subscribed": False,
                "verified": True,
                "reason": f"subscription failed after redemption: {exc}",
                "fundsLocation": "SPOT",
                "earnRemaining": _string_decimal(remaining),
                "spotBalance": _string_decimal(spot_after),
                "redeemResponse": redeem_response,
                "preflight": preflight,
                "postRedemptionGuards": refreshed_guard,
            }

        try:
            position = await self._verify_subscription(
                product=refreshed,
                deposit_amount=_string_decimal(redeem_amount),
                auto_compound_plan=auto_compound_plan,
            )
        except BinanceClientError as exc:
            return {
                "redeemed": True,
                "subscribed": True,
                "verified": False,
                "reason": str(exc),
                "fundsLocation": "DUAL_INVESTMENT_OR_PENDING",
                "earnRemaining": _string_decimal(remaining),
                "redeemResponse": redeem_response,
                "subscribeResponse": subscribe_response,
                "preflight": preflight,
                "postRedemptionGuards": refreshed_guard,
            }

        return {
            "redeemed": True,
            "subscribed": True,
            "verified": True,
            "fundsLocation": "DUAL_INVESTMENT",
            "earnRemaining": _string_decimal(remaining),
            "redeemResponse": redeem_response,
            "subscribeResponse": subscribe_response,
            "preflight": preflight,
            "postRedemptionGuards": refreshed_guard,
            "position": position,
        }
