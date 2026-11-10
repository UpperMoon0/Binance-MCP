"""Explicit owner-account Earn moves and stablecoin conversions.

This journal is separate from the trading-strategy ledger. Account holdings are
not silently attributed to a strategy; every movement subtracts live strategy
claims at the source location and is recorded before the exchange request.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from decimal import Decimal
from typing import Any

from .client import BinanceClientError
from .investment import InvestmentService
from .ledger import Ledger, decimal_text, number
from .snapshots import Snapshots


def _now() -> int:
    return int(time.time() * 1000)


def _client_id(intent_id: str) -> str:
    return "owner-" + hashlib.sha256(intent_id.encode()).hexdigest()[:28]


def _asset_set() -> set[str]:
    return {part.strip().upper() for part in
            os.getenv("BINANCE_ACCOUNT_EARN_ASSETS", "USD1,USDC,USDT,U").split(",") if part.strip()}


class AccountEarnService:
    """Manual, scoped account operations; never draws from a strategy allocation."""

    def __init__(self, client: Any, ledger: Ledger):
        self.client, self.ledger = client, ledger
        self.investment = InvestmentService(client)
        self.snapshots = Snapshots(client, ledger, [])
        self.lock = asyncio.Lock()
        self.ledger.db.execute("""
            CREATE TABLE IF NOT EXISTS account_earn_intents(
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request TEXT NOT NULL,
                state TEXT NOT NULL, preview TEXT NOT NULL, response TEXT,
                created INTEGER NOT NULL, error TEXT)
        """)

    def _row(self, intent_id: str) -> dict | None:
        row = self.ledger.db.execute("SELECT * FROM account_earn_intents WHERE id=?", (intent_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        for field in ("request", "preview", "response", "error"):
            value[field] = json.loads(value[field]) if value[field] else None
        return value

    def status(self, intent_id: str) -> dict:
        row = self._row(intent_id)
        if row is None:
            raise BinanceClientError("account Earn intent not found", blocker="INTENT_NOT_FOUND")
        return row

    def _update(self, intent_id: str, state: str, *, response: Any = None, error: Any = None) -> None:
        self.ledger.db.execute(
            "UPDATE account_earn_intents SET state=?, response=?, error=? WHERE id=?",
            (state, json.dumps(response) if response is not None else None,
             json.dumps(error) if error is not None else None, intent_id),
        )

    def _strategy_claim(self, asset: str, location: str) -> Decimal:
        locations = ("SPOT", "PROFIT_RESERVE") if location == "SPOT" else (location,)
        placeholders = ",".join("?" for _ in locations)
        rows = self.ledger.db.execute(
            f"SELECT b.quantity FROM balances b JOIN strategies s ON b.strategy=s.id "
            f"WHERE s.mode='live' AND b.asset=? AND b.location IN ({placeholders})",
            (asset, *locations),
        )
        return sum((number(r["quantity"], zero=True) for r in rows), Decimal(0))

    async def _spot_free(self, asset: str) -> Decimal:
        account = await self.client.signed_get("spot", "/api/v3/account")
        balances = account.get("balances")
        if not isinstance(balances, list):
            raise BinanceClientError("Spot account coverage incomplete", blocker="ACCOUNT_COVERAGE")
        matches = [b for b in balances if isinstance(b, dict) and b.get("asset") == asset]
        if len(matches) > 1:
            raise BinanceClientError("duplicate Spot asset", blocker="ACCOUNT_COVERAGE")
        return number(matches[0]["free"], zero=True) if matches else Decimal(0)

    @staticmethod
    def _aprs(product: dict, amount: Decimal, existing: Decimal) -> dict:
        base = number(product["latestAnnualPercentageRate"], zero=True)
        bonus = Decimal(0)
        selected = None
        for tier, apr in (product.get("tierAnnualPercentageRate") or {}).items():
            m = re.fullmatch(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)([A-Z0-9]+)", tier)
            if not m or m.group(3) != product["asset"]:
                continue
            lower, upper = Decimal(m.group(1)), Decimal(m.group(2))
            if lower <= existing and existing + amount <= upper:
                candidate = number(apr, zero=True)
                if candidate > bonus:
                    bonus, selected = candidate, tier
        return {"baseApr": decimal_text(base), "bonusTierApr": decimal_text(bonus),
                "applicableAdvertisedApr": decimal_text(base + bonus),
                "promotionalTier": selected, "aprVariable": True}

    async def preview(self, operation: str, asset: str, amount: str, *, product_id: str | None = None,
                      target_asset: str | None = None, minimum_price: str | None = None,
                      minimum_apr: str | None = None) -> dict:
        if operation not in ("redeem", "subscribe", "swap"):
            raise BinanceClientError("unsupported account Earn operation")
        if asset not in _asset_set() or (target_asset is not None and target_asset not in _asset_set()):
            raise BinanceClientError("asset not in owner-account allowlist", blocker="ACCOUNT_ASSET_POLICY")
        quantity = number(amount)
        cap = number(os.getenv("BINANCE_ACCOUNT_EARN_MAX_AMOUNT", "1000"))
        if quantity > cap:
            raise BinanceClientError("owner-account operation exceeds configured per-intent cap", blocker="ACCOUNT_AMOUNT_CAP")
        if operation in ("redeem", "subscribe") and (not product_id or target_asset or minimum_price):
            raise BinanceClientError("Earn move requires only exact productId and asset")
        if operation == "swap" and (product_id or not target_asset or target_asset == asset or minimum_price is None or minimum_apr):
            raise BinanceClientError("swap requires targetAsset and minimumPrice, not productId")
        started = _now()
        contract: dict[str, Any] = {}
        location = "SPOT"
        if operation == "redeem":
            positions = await self.snapshots.pages(
                "/sapi/v1/simple-earn/flexible/position", extra={"asset": asset})
            matches = [r for r in positions if r.get("productId") == product_id]
            if len(matches) != 1 or matches[0].get("asset") != asset:
                raise BinanceClientError("exact Earn position not available", blocker="PRODUCT_ASSET_MISMATCH")
            position = matches[0]
            if position.get("canRedeem") is not True:
                raise BinanceClientError("Earn position is not redeemable")
            available = number(position["totalAmount"], zero=True)
            location = "EARN:" + product_id
        else:
            available = await self._spot_free(asset)
        protected = self._strategy_claim(asset, location)
        if available - protected < quantity:
            raise BinanceClientError("amount exceeds account-owned funds outside strategy allocations",
                                     blocker="ACCOUNT_UNALLOCATED_FUNDS")
        if operation == "subscribe":
            products = await self.snapshots.pages(
                "/sapi/v1/simple-earn/flexible/list", extra={"asset": asset})
            matches = [r for r in products if r.get("productId") == product_id]
            if len(matches) != 1 or matches[0].get("asset") != asset:
                raise BinanceClientError("exact Earn product unavailable", blocker="PRODUCT_ASSET_MISMATCH")
            product = matches[0]
            if (product.get("canPurchase") is not True or product.get("isSoldOut") is not False
                    or product.get("status") != "PURCHASING" or
                    int(product["subscriptionStartTime"]) > started or
                    quantity < number(product["minPurchaseAmount"], zero=True)):
                raise BinanceClientError("Earn product is not currently purchasable")
            positions = await self.snapshots.pages(
                "/sapi/v1/simple-earn/flexible/position", extra={"asset": asset})
            existing = sum((number(r["totalAmount"], zero=True) for r in positions
                            if r.get("productId") == product_id), Decimal(0))
            contract = self._aprs(product, quantity, existing)
            if minimum_apr is not None and number(contract["applicableAdvertisedApr"], zero=True) < number(minimum_apr, zero=True):
                raise BinanceClientError("Earn advertised APR below requested minimum", blocker="APR_FLOOR")
        elif operation == "swap":
            price_floor = number(minimum_price)
            symbol = asset + target_asset
            info = await self.client.public_get("spot", "/api/v3/exchangeInfo", {"symbol": symbol})
            symbols = info.get("symbols") if isinstance(info, dict) else None
            if not isinstance(symbols, list) or len(symbols) != 1:
                raise BinanceClientError("exact Spot pair unavailable")
            pair = symbols[0]
            if (pair.get("symbol") != symbol or pair.get("status") != "TRADING" or
                    pair.get("baseAsset") != asset or pair.get("quoteAsset") != target_asset or
                    pair.get("isSpotTradingAllowed") is not True or
                    "LIMIT" not in pair.get("orderTypes", [])):
                raise BinanceClientError("pair is not enabled for Spot trading")
            filters = {f["filterType"]: f for f in pair["filters"]}
            tick = number(filters["PRICE_FILTER"]["tickSize"])
            step = number(filters["LOT_SIZE"]["stepSize"])
            if (quantity % step or price_floor % tick or
                    quantity < number(filters["LOT_SIZE"]["minQty"]) or
                    quantity > number(filters["LOT_SIZE"]["maxQty"]) or
                    price_floor < number(filters["PRICE_FILTER"]["minPrice"]) or
                    price_floor > number(filters["PRICE_FILTER"]["maxPrice"])):
                raise BinanceClientError("swap price/quantity violates exchange filters", blocker="SPOT_FILTER")
            notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
            if notional and quantity * price_floor < number(notional["minNotional"]):
                raise BinanceClientError("swap notional below minimum", blocker="SPOT_FILTER")
            book = await self.client.public_get("spot", "/api/v3/ticker/bookTicker", {"symbol": symbol})
            bid, ask = number(book["bidPrice"]), number(book["askPrice"])
            if bid < price_floor or (ask - bid) / bid > Decimal("0.005"):
                raise BinanceClientError("minimum price or spread guard failed", blocker="SPOT_PRICE_GUARD")
            contract = {"symbol": symbol, "side": "SELL", "type": "LIMIT", "timeInForce": "IOC",
                        "price": decimal_text(price_floor), "bookBid": decimal_text(bid),
                        "bookAsk": decimal_text(ask), "estimatedMinProceeds": decimal_text(quantity * price_floor)}
        return {"observedAt": started, "operation": operation, "asset": asset,
                "targetAsset": target_asset, "productId": product_id, "amount": decimal_text(quantity),
                "location": location, "accountAvailable": decimal_text(available),
                "protectedStrategyFunds": decimal_text(protected),
                "unallocatedAvailable": decimal_text(available - protected),
                "contract": contract}

    async def execute(self, intent_id: str, operation: str, asset: str, amount: str, *,
                      product_id: str | None = None, target_asset: str | None = None,
                      minimum_price: str | None = None, minimum_apr: str | None = None,
                      acknowledge_account_funds: bool = False) -> dict:
        if acknowledge_account_funds is not True:
            raise BinanceClientError("explicit account-funds acknowledgement required", blocker="ACCOUNT_CONFIRMATION")
        if not intent_id or len(intent_id) > 128:
            raise BinanceClientError("stable intentId of at most 128 characters required")
        self.client._require_trading()
        request = {"operation": operation, "asset": asset, "amount": decimal_text(number(amount)),
                   "productId": product_id, "targetAsset": target_asset,
                   "minimumPrice": minimum_price, "minimumApr": minimum_apr}
        fingerprint = self.ledger.fingerprint(request)
        async with self.lock:
            prior = self._row(intent_id)
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise BinanceClientError("account intentId reused with different contract")
                return prior  # never resend an uncertain mutation
            if self.ledger.get(intent_id):
                raise BinanceClientError("intentId already belongs to a strategy")
            with self.ledger.transaction():
                if self.ledger.db.execute(
                        "SELECT 1 FROM account_earn_intents WHERE state NOT IN ('RESOLVED','REJECTED')").fetchone():
                    raise BinanceClientError("unresolved owner-account transfer requires reconciliation",
                                             blocker="ACCOUNT_INTENT_PENDING")
            preflight = await self.preview(operation, asset, amount, product_id=product_id,
                                           target_asset=target_asset, minimum_price=minimum_price,
                                           minimum_apr=minimum_apr)
            if _now() - preflight["observedAt"] > 90000:
                raise BinanceClientError("account operation preflight stale")
            with self.ledger.transaction():
                if self.ledger.db.execute(
                        "SELECT 1 FROM account_earn_intents WHERE state NOT IN ('RESOLVED','REJECTED')").fetchone():
                    raise BinanceClientError("owner account operation raced with another request")
                self.ledger.db.execute(
                    "INSERT INTO account_earn_intents VALUES(?,?,?,?,?,?,?,?)",
                    (intent_id, fingerprint, json.dumps(request, sort_keys=True), "PREPARED",
                     json.dumps(preflight), None, _now(), None))
                self._update(intent_id, "SENDING")  # crash/timeout: never auto-retry
            try:
                if operation == "redeem":
                    response = await self.client.simple_earn_redeem(product_id, request["amount"], "SPOT")
                elif operation == "subscribe":
                    response = await self.client.simple_earn_subscribe(product_id, request["amount"], False, "SPOT")
                else:
                    contract = preflight["contract"]
                    response = await self.client.order("spot", "/api/v3/order", "create", {
                        "symbol": contract["symbol"], "side": "SELL", "type": "LIMIT",
                        "timeInForce": "IOC", "quantity": request["amount"],
                        "price": contract["price"], "newClientOrderId": _client_id(intent_id)})
                self._update(intent_id, "OUTCOME_UNKNOWN", response=response)
            except BinanceClientError as exc:
                self._update(intent_id, "REJECTED" if exc.definitive_rejection else "OUTCOME_UNKNOWN",
                             error=exc.metadata())
                return self.status(intent_id)
            except Exception:
                self._update(intent_id, "OUTCOME_UNKNOWN", error={"message": "exchange outcome uncertain"})
                return self.status(intent_id)
            try:
                return await self._reconcile_locked(intent_id)
            except Exception:
                return self.status(intent_id)

    async def reconcile(self, intent_id: str) -> dict:
        async with self.lock:
            return await self._reconcile_locked(intent_id)

    async def _reconcile_locked(self, intent_id: str) -> dict:
        row = self.status(intent_id)
        if row["state"] in ("RESOLVED", "REJECTED"):
            return row
        request = row["request"]
        op, asset, amount = request["operation"], request["asset"], number(request["amount"])
        response = row["response"] or {}
        if op in ("redeem", "subscribe"):
            field = "redeemId" if op == "redeem" else "purchaseId"
            identity = response.get(field)
            if identity is None:
                return row  # no identifier: requires manual audit, not a second request
            history_type = "redemptionRecord" if op == "redeem" else "subscriptionRecord"
            history = await self.snapshots.pages(
                "/sapi/v1/simple-earn/flexible/history/" + history_type,
                extra={"startTime": row["created"], "endTime": _now()})
            matches = [r for r in history if str(r.get(field)) == str(identity) and
                       r.get("productId", r.get("projectId")) == request["productId"] and
                       r.get("asset") == asset and
                       number(r.get("amount", "0"), zero=True) == amount and
                       r.get("status") == ("PAID" if op == "redeem" else "SUCCESS")]
            if len(matches) != 1:
                return row
            verified = {"recordId": str(identity), "record": matches[0],
                        "destination": "SPOT" if op == "redeem" else "EARN:" + request["productId"]}
        else:
            symbol = row["preview"]["contract"]["symbol"]
            order = await self.client.signed_get("spot", "/api/v3/order",
                                                 {"symbol": symbol, "origClientOrderId": _client_id(intent_id)})
            if (order.get("symbol") != symbol or order.get("clientOrderId") != _client_id(intent_id) or
                    order.get("side") != "SELL" or order.get("type") != "LIMIT" or
                    order.get("timeInForce") != "IOC" or
                    number(order.get("origQty", "0")) != amount or
                    number(order.get("price", "0")) != number(row["preview"]["contract"]["price"])):
                raise BinanceClientError("Spot order contradicts owner intent", blocker="ORDER_IDENTITY")
            if order.get("status") not in ("FILLED", "EXPIRED", "CANCELED", "REJECTED"):
                return row
            qty = number(order.get("executedQty", "0"), zero=True)
            quote = number(order.get("cummulativeQuoteQty", "0"), zero=True)
            trades = await self.client.signed_get("spot", "/api/v3/myTrades",
                                                  {"symbol": symbol, "orderId": order["orderId"], "limit": 1000})
            if not isinstance(trades, list) or len(trades) >= 1000 or any(
                    t.get("orderId") != order["orderId"] or t.get("isBuyer") is not False for t in trades):
                raise BinanceClientError("owner swap fill evidence incomplete", blocker="FILL_COVERAGE")
            if (sum((number(t["qty"]) for t in trades), Decimal(0)) != qty or
                    sum((number(t["quoteQty"]) for t in trades), Decimal(0)) != quote or
                    len({str(t["id"]) for t in trades}) != len(trades)):
                raise BinanceClientError("Spot fills differ from order", blocker="FILL_COVERAGE")
            fees: dict[str, Decimal] = {}
            for trade in trades:
                fee_asset = trade["commissionAsset"]
                fees[fee_asset] = fees.get(fee_asset, Decimal(0)) + number(trade["commission"], zero=True)
            verified = {"orderId": order["orderId"], "clientOrderId": _client_id(intent_id),
                        "status": order["status"], "sold": decimal_text(qty),
                        "grossReceived": decimal_text(quote),
                        "commissions": {k: decimal_text(v) for k, v in fees.items()},
                        "netReceived": decimal_text(quote - fees.get(request["targetAsset"], Decimal(0))),
                        "tradeIds": [t["id"] for t in trades]}
        with self.ledger.transaction():
            latest = self.status(intent_id)
            if latest["state"] == "RESOLVED":
                return latest
            if op == "swap":
                expected = self.ledger.meta("expectedTotals")
                if expected is not None:
                    deltas = {asset: -number(verified["sold"], zero=True),
                              request["targetAsset"]: number(verified["grossReceived"], zero=True)}
                    for fee_asset, commission in verified["commissions"].items():
                        deltas[fee_asset] = deltas.get(fee_asset, Decimal(0)) - number(commission, zero=True)
                    for coin, delta in deltas.items():
                        expected[coin] = decimal_text(number(expected.get(coin, "0"), zero=True) + delta)
                    self.ledger.set_meta("expectedTotals", expected)
            self._update(intent_id, "RESOLVED", response={"exchangeResponse": response, "verified": verified})
        return self.status(intent_id)
