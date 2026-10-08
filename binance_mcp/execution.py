"""Guarded execution coordinator. Exchange writes never use automatic retries."""
from __future__ import annotations

import asyncio
import hashlib
import time
from decimal import Decimal, ROUND_DOWN

from .client import BinanceClientError
from .investment import InvestmentService
from .ledger import Ledger, number
from .snapshots import Snapshots

TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}


def client_id(intent: str, suffix: str = "") -> str:
    return "mcp_" + hashlib.sha256((intent + suffix).encode()).hexdigest()[:28]


class ExecutionService:
    def __init__(self, client, ledger: Ledger, symbols: list[str]):
        self.client, self.ledger = client, ledger
        self.snapshots = Snapshots(client, ledger, symbols)
        self.investment = InvestmentService(client)
        self.lock = asyncio.Lock()

    async def symbol(self, symbol: str) -> dict:
        if symbol not in self.snapshots.symbols:
            raise BinanceClientError("symbol is outside approved universe")
        self.client._cache.clear()
        data = await self.client.public_get("spot", "/api/v3/exchangeInfo", {"symbol": symbol})
        matches = [s for s in data.get("symbols", []) if s["symbol"] == symbol]
        if len(matches) != 1 or matches[0]["status"] != "TRADING" or not matches[0].get("isSpotTradingAllowed", False):
            raise BinanceClientError("symbol is not available for Spot trading")
        return matches[0]

    @staticmethod
    def validate_leg(info: dict, quantity: Decimal, price: Decimal, side: str, average: Decimal, *, market: bool = False) -> None:
        for f in info["filters"]:
            kind = f["filterType"]
            if kind == "LOT_SIZE" or (kind == "MARKET_LOT_SIZE" and market):
                minimum, maximum, step = (number(f[k], zero=True) for k in ("minQty", "maxQty", "stepSize"))
                if quantity < minimum or (maximum and quantity > maximum) or (step and quantity % step):
                    raise BinanceClientError("quantity violates LOT_SIZE")
            elif kind == "PRICE_FILTER":
                minimum, maximum, tick = (number(f[k], zero=True) for k in ("minPrice", "maxPrice", "tickSize"))
                if (minimum and price < minimum) or (maximum and price > maximum) or (tick and price % tick):
                    raise BinanceClientError("price violates PRICE_FILTER")
            elif kind in ("MIN_NOTIONAL", "NOTIONAL"):
                minimum = number(f["minNotional"], zero=True)
                maximum = number(f.get("maxNotional", "0"), zero=True)
                if quantity * price < minimum or (maximum and quantity * price > maximum):
                    raise BinanceClientError("entry or exit violates notional filter")
            elif kind in ("PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"):
                prefix = "bid" if side == "BUY" else "ask"
                up = f.get(prefix + "MultiplierUp", f.get("multiplierUp"))
                down = f.get(prefix + "MultiplierDown", f.get("multiplierDown"))
                if (up and price > average * number(up)) or (down and price < average * number(down)):
                    raise BinanceClientError("price violates percent price filter")

    async def preview(self, strategy: str, operation: str, params: dict) -> dict:
        cfg = self.ledger.strategy(strategy)
        if operation not in ("order", "oco", "otoco"):
            raise BinanceClientError("preview supports order, oco or otoco")
        allowed = {"symbol", "quantity", "price", "stopPrice", "takeProfit", "maxSpreadBps", "maxSlippageBps",
                   "minStopDistanceBps", "maxStopDistanceBps", "side", "type", "timeInForce"}
        if set(params) - allowed:
            raise BinanceClientError("unsupported order parameter; narrow workflow requires explicit limit prices")
        info = await self.symbol(params["symbol"])
        if info["quoteAsset"] != cfg["quote"]:
            raise BinanceClientError("symbol quote does not match allocation currency")
        if operation == "oco" and any(i["strategy"] == strategy and i["payload"]["operation"] == "otoco"
                                      and i["payload"]["params"].get("symbol") == info["symbol"]
                                      for i in self.ledger.outstanding()):
            raise BinanceClientError("cancel and reconcile previous OTOCO entry before replacement OCO")
        qty, price = number(params["quantity"]), number(params["price"])
        if operation == "order" and (params.get("side") != "SELL" or params.get("type", "LIMIT") != "LIMIT"):
            raise BinanceClientError("entries require protected otoco; ordinary orders support LIMIT SELL only")
        if params.get("timeInForce", "GTC") != "GTC":
            raise BinanceClientError("only GTC is supported")
        side = "BUY" if operation == "otoco" else "SELL"
        if "side" in params and params["side"] != side:
            raise BinanceClientError("side disagrees with workflow")
        book, avg = await asyncio.gather(
            self.client.public_get("spot", "/api/v3/ticker/bookTicker", {"symbol": info["symbol"]}),
            self.client.public_get("spot", "/api/v3/avgPrice", {"symbol": info["symbol"]}),
        )
        if cfg["mode"] == "paper":
            account = {"canTrade": True, "permissions": ["SPOT"], "balances": [
                {"asset": r["asset"], "free": r["quantity"], "locked": "0"}
                for r in self.ledger.db.execute("SELECT asset,quantity FROM balances WHERE strategy=? AND location='SPOT'", (strategy,))]}
            commission = {k: {"maker": rate, "taker": rate, "buyer": "0", "seller": "0"}
                          for k, rate in (("standardCommission", "0.001"), ("taxCommission", "0"), ("specialCommission", "0"))}
        else:
            account, commission = await asyncio.gather(
                self.client.signed_get("spot", "/api/v3/account"),
                self.client.signed_get("spot", "/api/v3/account/commission", {"symbol": info["symbol"]}),
            )
        discount = commission.get("discount", {})
        if cfg["mode"] == "live" and discount.get("enabledForAccount") and discount.get("enabledForSymbol"):
            raise BinanceClientError("third-asset commission payment unsupported; disable Binance fee discount before managed trading")
        if not account.get("canTrade") or "SPOT" not in account.get("permissions", []):
            raise BinanceClientError("account lacks Spot trading permission")
        fee_rate = Decimal(0)
        for key in ("standardCommission", "taxCommission", "specialCommission"):
            rates = commission.get(key)
            if rates is None:
                raise BinanceClientError("commission coverage incomplete")
            fee_rate += max(number(rates["maker"], zero=True), number(rates["taker"], zero=True))
            fee_rate += max(number(rates["buyer"], zero=True), number(rates["seller"], zero=True))
        if fee_rate >= 1:
            raise BinanceClientError("invalid commission rate")
        bid, ask = number(book["bidPrice"]), number(book["askPrice"])
        if bid > ask:
            raise BinanceClientError("invalid order book")
        spread = (ask - bid) / ((ask + bid)/2) * 10000
        max_spread = number(params.get("maxSpreadBps", "20"), zero=True)
        if max_spread > 100 or spread > max_spread:
            raise BinanceClientError("spread guard failed")
        reference = ask if side == "BUY" else bid
        slippage = abs(price-reference) / reference * 10000
        slippage_limit = number(params.get("maxSlippageBps", "100"), zero=True)
        if slippage_limit > 500 or slippage > slippage_limit:
            raise BinanceClientError("limit price slippage guard failed")
        avg_price = number(avg["price"])
        for f in info["filters"]:
            if f["filterType"] in ("PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE") and int(f.get("avgPriceMins", 0)) != int(avg.get("mins", 0)):
                raise BinanceClientError("percent-price average window unavailable")
        self.validate_leg(info, qty, price, side, avg_price)
        lot = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
        step = number(lot["stepSize"])
        sellable = (qty * (1-fee_rate) / step).to_integral_value(rounding=ROUND_DOWN) * step if side == "BUY" else qty
        if operation in ("oco", "otoco"):
            stop, take = number(params["stopPrice"]), number(params["takeProfit"])
            if not stop < min(price, bid) or not max(price, ask) < take:
                raise BinanceClientError("exit prices must bracket entry and current market")
            distance = (price-stop)/price * 10000
            min_distance = number(params.get("minStopDistanceBps", "10"), zero=True)
            max_distance = number(params.get("maxStopDistanceBps", "2000"))
            if min_distance < 10 or max_distance > 2000 or not min_distance <= distance <= max_distance:
                raise BinanceClientError("stop distance guard failed")
            self.validate_leg(info, sellable, stop, "SELL", avg_price, market=True)
            self.validate_leg(info, sellable, take, "SELL", avg_price)
            if not info.get("ocoAllowed", False) or (operation == "otoco" and not info.get("otoAllowed", False)):
                raise BinanceClientError("symbol does not permit requested linked orders")
        asset = info["quoteAsset"] if side == "BUY" else info["baseAsset"]
        reservation = qty * price * (1 + fee_rate) if side == "BUY" else qty
        if self.ledger.available(strategy, asset) < reservation:
            raise BinanceClientError("insufficient available strategy capital")
        balances = {b["asset"]: b for b in account["balances"]}
        if cfg["mode"] == "live" and number(balances.get(asset, {}).get("free", "0"), zero=True) < reservation:
            raise BinanceClientError("insufficient exchange free balance")
        for f in info["filters"]:
            if f["filterType"] == "MAX_POSITION" and side == "BUY":
                balance = balances.get(info["baseAsset"], {})
                held = number(balance.get("free", "0"), zero=True) + number(balance.get("locked", "0"), zero=True)
                if held + qty > number(f["maxPosition"]):
                    raise BinanceClientError("MAX_POSITION guard failed")
        if cfg["mode"] == "paper":
            all_orders = []
            for intent in self.ledger.outstanding():
                if self.ledger.strategy(intent["strategy"])["mode"] != "paper":
                    continue
                op = intent["payload"]["operation"]
                for kind in (["LIMIT", "LIMIT_MAKER", "STOP_LOSS"] if op == "otoco" else ["LIMIT_MAKER", "STOP_LOSS"] if op == "oco" else ["LIMIT"] if op == "order" else []):
                    all_orders.append({"symbol": intent["payload"]["params"]["symbol"], "type": kind})
            orders = [o for o in all_orders if o["symbol"] == info["symbol"]]
        else:
            orders = await self.client.signed_get("spot", "/api/v3/openOrders", {"symbol": info["symbol"]})
            all_orders = await self.client.signed_get("spot", "/api/v3/openOrders")
        for f in info["filters"]:
            if f["filterType"] == "MAX_POSITION" and side == "BUY":
                balance = balances.get(info["baseAsset"], {})
                held = number(balance.get("free", "0"), zero=True) + number(balance.get("locked", "0"), zero=True)
                pending = sum((number(o["origQty"]) - number(o["executedQty"], zero=True)
                               for o in orders if o.get("side") == "BUY"), Decimal(0))
                if held + pending + qty > number(f["maxPosition"]):
                    raise BinanceClientError("MAX_POSITION pending-order guard failed")
        lists = await self.client.signed_get("spot", "/api/v3/openOrderList") if cfg["mode"] == "live" else [
            {"symbol": i["payload"]["params"]["symbol"]} for i in self.ledger.outstanding()
            if self.ledger.strategy(i["strategy"])["mode"] == "paper" and i["payload"]["operation"] in ("oco", "otoco")]
        exchange = await self.client.public_get("spot", "/api/v3/exchangeInfo")
        count = 3 if operation == "otoco" else 2 if operation == "oco" else 1
        for f in info["filters"] + exchange.get("exchangeFilters", []):
            kind = f["filterType"]
            if kind in ("MAX_NUM_ORDERS", "EXCHANGE_MAX_NUM_ORDERS"):
                current = len(all_orders) if kind.startswith("EXCHANGE") else len(orders)
                limit = int(f.get("maxNumOrders", 0))
                if limit and current + count > limit:
                    raise BinanceClientError("open order count guard failed")
            elif kind in ("MAX_NUM_ORDER_LISTS", "EXCHANGE_MAX_NUM_ORDER_LISTS"):
                current = len(lists) if kind.startswith("EXCHANGE") else sum(o["symbol"] == info["symbol"] for o in lists)
                if current + int(operation in ("oco", "otoco")) > int(f["maxNumOrderLists"]):
                    raise BinanceClientError("open order-list count guard failed")
            elif kind in ("MAX_NUM_ALGO_ORDERS", "EXCHANGE_MAX_NUM_ALGO_ORDERS"):
                current = sum(o["type"] in ("STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT")
                              for o in (all_orders if kind.startswith("EXCHANGE") else orders))
                if current + (1 if operation in ("oco", "otoco") else 0) > int(f["maxNumAlgoOrders"]):
                    raise BinanceClientError("algorithmic order count guard failed")
        return {"observedAt": int(time.time() * 1000), "strategyId": strategy, "operation": operation,
                "params": params, "base": info["baseAsset"], "quote": info["quoteAsset"],
                "asset": asset, "reservation": str(reservation), "sellableQuantity": str(sellable),
                "feeRateBound": str(fee_rate), "spreadBps": str(spread), "slippageBps": str(slippage),
                "filters": info["filters"], "bookTicker": book,
                "protection": "PENDING_FULL_FILL" if operation == "otoco" else "REQUIRES_RECONCILIATION"}

    def order_params(self, intent_id: str, preview: dict) -> tuple[str, dict]:
        p, op = preview["params"], preview["operation"]
        if op == "order":
            return "/api/v3/order", {"symbol": p["symbol"], "side": "SELL", "type": "LIMIT", "timeInForce": "GTC",
                "quantity": p["quantity"], "price": p["price"], "newClientOrderId": client_id(intent_id)}
        params = {"symbol": p["symbol"], "listClientOrderId": client_id(intent_id), "newOrderRespType": "FULL"}
        if op == "otoco":
            params.update(workingType="LIMIT", workingSide="BUY", workingTimeInForce="GTC",
                          workingQuantity=p["quantity"], workingPrice=p["price"],
                          workingClientOrderId=client_id(intent_id, "entry"), pendingSide="SELL",
                          pendingQuantity=preview["sellableQuantity"], pendingAboveType="LIMIT_MAKER",
                          pendingAbovePrice=p["takeProfit"], pendingAboveClientOrderId=client_id(intent_id, "take"),
                          pendingBelowType="STOP_LOSS", pendingBelowStopPrice=p["stopPrice"],
                          pendingBelowClientOrderId=client_id(intent_id, "stop"))
        else:
            params.update(side="SELL", quantity=p["quantity"], aboveType="LIMIT_MAKER", abovePrice=p["takeProfit"],
                          aboveClientOrderId=client_id(intent_id, "take"), belowType="STOP_LOSS",
                          belowStopPrice=p["stopPrice"], belowClientOrderId=client_id(intent_id, "stop"))
        return "/api/v3/orderList/" + op, params

    async def execute(self, strategy: str, intent_id: str, operation: str, params: dict) -> dict:
        payload = {"operation": operation, "params": params}
        async with self.lock:
            old = self.ledger.get(intent_id)
            if old:
                if old["strategy"] != strategy or old["fingerprint"] != self.ledger.fingerprint(payload):
                    raise BinanceClientError("intentId reused with a different contract")
                return await self.reconcile(intent_id)
            cfg = self.ledger.strategy(strategy)
            if cfg["mode"] == "paper" and operation not in ("cancel", "oco", "order") and self.ledger.meta("paperPause:" + strategy):
                raise BinanceClientError("paper strategy paused: " + self.ledger.meta("paperPause:" + strategy))
            if cfg["mode"] == "live":
                self.client._require_trading()
                await self.reconcile_all()
                if operation not in ("cancel", "oco", "order"):
                    last = self.ledger.meta("monitorAt")
                    if not last or int(time.time() * 1000) - last > 90000 or not self.ledger.meta("streamConnected"):
                        self.ledger.pause("user stream or operational account state stale")
                        raise BinanceClientError("operational monitor is stale or disconnected")
                    await self.check_account()
                    await self.check_protection()
            if operation in ("order", "oco", "otoco"):
                preview = await self.preview(strategy, operation, params)
                asset, location, reserve = preview["asset"], "SPOT", preview["reservation"]
                path, order = self.order_params(intent_id, preview)
            elif operation in ("earn_subscribe", "earn_redeem", "dual_subscribe", "earn_to_dual"):
                preview, asset, location, reserve = await self.investment_preflight(strategy, operation, params)
                path, order = "", {}
            elif operation == "cancel":
                target = self.ledger.get(params["targetIntentId"])
                if target is None or target["strategy"] != strategy or target["payload"]["operation"] not in ("order", "oco", "otoco"):
                    raise BinanceClientError("cancel requires a strategy-owned order intent")
                preview, asset, location, reserve = {}, target["asset"], "SPOT", "0"
                linked = target["payload"]["operation"] != "order"
                path = "/api/v3/orderList" if linked else "/api/v3/order"
                order = {"symbol": target["payload"]["params"]["symbol"],
                         "listClientOrderId" if linked else "origClientOrderId": client_id(target["id"])}
            else:
                raise BinanceClientError("unsupported execution operation")
            intent, fresh = self.ledger.begin(intent_id, strategy, payload, asset, location, reserve,
                                               recovery=operation in ("cancel", "oco", "order"),
                                               prepared={"preview": preview, "path": path, "order": order})
            if not fresh:
                return await self.reconcile(intent_id)
            self.ledger.update(intent_id, "PREPARED", result={"preview": preview, "path": path, "order": order})
            try:
                if cfg["mode"] == "paper":
                    return self.paper_execute(intent_id)
                if operation == "cancel":
                    response = await self.client.order("spot", path, "cancel", order)
                elif operation in ("order", "oco", "otoco"):
                    response = await self.client.order("spot", path, "create", order)
                else:
                    def checkpoint(phase, data):
                        current = self.ledger.get(intent_id)
                        self.ledger.update(intent_id, "PREPARED", result={**(current["result"] or {}), "phase": phase, "checkpoint": data})
                    self.investment.checkpoint = checkpoint
                    try:
                        response = await self.investment_write(operation, params)
                    finally:
                        self.investment.checkpoint = lambda phase, data: None
                checkpoint_data = (self.ledger.get(intent_id)["result"] or {}).get("checkpoint", {})
                state = "OPEN" if operation in ("order", "oco", "otoco", "cancel") else "OUTCOME_UNKNOWN"
                if operation == "dual_subscribe" and response.get("outcome") == "REJECTED":
                    state = "REJECTED"
                self.ledger.update(intent_id, state, result={"preview": preview, "response": response,
                                                            "path": path, "order": order, "checkpoint": checkpoint_data})
                if operation == "cancel":
                    target = await self.reconcile(params["targetIntentId"])
                    if target["state"] != "RESOLVED":
                        raise BinanceClientError("cancellation accepted; terminal target not yet verified")
                    self.ledger.update(intent_id, "RESOLVED", result={"response": response})
                else:
                    await self.reconcile(intent_id)
            except Exception as exc:
                rejected = isinstance(exc, BinanceClientError) and exc.definitive_rejection
                current = self.ledger.get(intent_id)
                if current["state"] != "PREPARED":
                    rejected = False
                self.ledger.update(intent_id, "REJECTED" if rejected else "OUTCOME_UNKNOWN", result=current["result"],
                                   error=exc.metadata() if isinstance(exc, BinanceClientError) else {"message": "execution interrupted"})
                if not rejected:
                    self.ledger.pause("uncertain execution " + intent_id)
            return self.ledger.get(intent_id)

    async def investment_preflight(self, strategy: str, op: str, p: dict) -> tuple[dict, str, str, str]:
        cfg = self.ledger.strategy(strategy)
        amount = str(number(p["amount"]))
        if op.startswith("earn_"):
            position = await self.investment.flexible_position(p["productId"] if op != "earn_to_dual" else p["earnProductId"])
            asset = position.get("asset")
            if not asset:
                raise BinanceClientError("Earn position asset unavailable")
            if op in ("earn_redeem", "earn_to_dual"):
                if not position.get("canRedeem") or number(position["totalAmount"], zero=True) - number(amount) < number(p.get("preserveEarnAmount", "0"), zero=True):
                    raise BinanceClientError("Earn redemption preservation guard failed")
        else:
            asset = p["investCoin"]
        if asset != cfg["quote"]:
            raise BinanceClientError("investment asset must match strategy quote")
        location = "EARN:" + p.get("productId", p.get("earnProductId", "")) if op in ("earn_redeem", "earn_to_dual") else "SPOT"
        if self.ledger.available(strategy, asset, location) < number(amount):
            raise BinanceClientError("investment exceeds strategy-owned funds")
        contract = {}
        if op in ("dual_subscribe", "earn_to_dual"):
            product = await self.investment.dual_product(p["dualProductId"], p["optionType"], p["exercisedCoin"], p["investCoin"])
            contract = await self.investment.validate_dual_product(product, amount, p.get("minimumApr"),
                        p.get("minimumStrikeDistancePercent"), p.get("autoCompoundPlan", "NONE"))
            contract["previousPositionIds"] = [str(pos["positionId"]) for pos in await self.investment.all_positions()]
        if op == "earn_subscribe" and p.get("autoSubscribe", False):
            raise BinanceClientError("automatic Earn subscriptions bypass allocation tracking; disabled")
        if p.get("autoCompoundPlan", "NONE") != "NONE":
            raise BinanceClientError("automatic DI compounding bypasses allocation tracking; disabled")
        if p.get("sourceAccount", "SPOT") != "SPOT" or p.get("destAccount", "SPOT") != "SPOT":
            raise BinanceClientError("managed investments use Spot funds only")
        if cfg["mode"] == "live" and location == "SPOT" and await self.investment._spot_free(asset) < number(amount):
            raise BinanceClientError("insufficient exchange free investment balance")
        return {"amount": amount, "asset": asset, "location": location, "contract": contract,
                "historyStart": int(time.time() * 1000)}, asset, location, amount

    async def investment_write(self, op: str, p: dict) -> dict:
        if op == "earn_subscribe":
            return await self.investment.subscribe_flexible(p["productId"], p["amount"], False, "SPOT")
        if op == "earn_redeem":
            return await self.investment.redeem_flexible(p["productId"], p["amount"], "SPOT")
        if op == "dual_subscribe":
            return await self.investment.subscribe_dual(product_id=p["dualProductId"], option_type=p["optionType"],
                exercised_coin=p["exercisedCoin"], invest_coin=p["investCoin"], deposit_amount=p["amount"],
                minimum_apr=p.get("minimumApr"), minimum_strike_distance_percent=p.get("minimumStrikeDistancePercent"))
        return await self.investment.from_flexible_earn(earn_product_id=p["earnProductId"], dual_product_id=p["dualProductId"],
            option_type=p["optionType"], exercised_coin=p["exercisedCoin"], invest_coin=p["investCoin"], amount=p["amount"],
            preserve_earn_amount=p["preserveEarnAmount"], minimum_apr=p.get("minimumApr"),
            minimum_strike_distance_percent=p.get("minimumStrikeDistancePercent"))

    def paper_execute(self, key: str) -> dict:
        intent = self.ledger.get(key)
        op = intent["payload"]["operation"]
        result = intent["result"]
        if op == "cancel":
            target = self.ledger.get(intent["payload"]["params"]["targetIntentId"])
            self.ledger.update(target["id"], "RESOLVED", result={**target["result"], "paperStatus": "CANCELED"})
            self.ledger.update(key, "RESOLVED", result={"paper": True})
        elif op in ("order", "oco", "otoco"):
            self.ledger.update(key, "OPEN", result={**result, "paper": True, "paperStatus": "NEW", "protected": op == "oco"})
            if op == "oco":
                self.clear_paper_pause_if_protected(intent["strategy"])
        else:
            self.ledger.update(key, "REJECTED", error={"message": "paper investments require settlement evidence; use Spot paper lifecycle"})
        return self.ledger.get(key)

    async def reconcile(self, key: str) -> dict:
        intent = self.ledger.get(key)
        if intent is None:
            raise BinanceClientError("intent not found")
        if intent["state"] in ("RESOLVED", "REJECTED"):
            return intent
        if self.ledger.strategy(intent["strategy"])["mode"] == "paper":
            return await self.paper_reconcile(intent)
        op, p = intent["payload"]["operation"], intent["payload"]["params"]
        result = intent["result"] or {}
        if op in ("order", "oco", "otoco"):
            linked = op != "order"
            response = await self.client.signed_get("spot", "/api/v3/orderList" if linked else "/api/v3/order",
                        {"origClientOrderId": client_id(key), **({} if linked else {"symbol": p["symbol"]})})
            orders = [response] if not linked else [await self.client.signed_get("spot", "/api/v3/order",
                      {"symbol": o["symbol"], "orderId": o["orderId"]}) for o in response["orders"]]
            expected_ids = {client_id(key, s) for s in ("entry", "take", "stop")} if op == "otoco" else {client_id(key, s) for s in ("take", "stop")} if op == "oco" else {client_id(key)}
            if {o.get("clientOrderId") for o in orders} != expected_ids or any(o.get("symbol") != p["symbol"] for o in orders):
                raise BinanceClientError("exchange order contract identifiers do not match intent")
            if any(o.get("side") != ("BUY" if op == "otoco" and o["clientOrderId"] == client_id(key, "entry") else "SELL") for o in orders):
                raise BinanceClientError("exchange order side does not match intent")
            for order in orders:
                if number(order.get("executedQty", "0"), zero=True):
                    await self.apply_order_fills(intent, order)
            protected = False
            hazard = False
            if op == "otoco":
                entry = next((o for o in orders if o["clientOrderId"] == client_id(key, "entry")), None)
                exits = [o for o in orders if o["clientOrderId"] in (client_id(key, "take"), client_id(key, "stop"))]
                if entry is None or len(exits) != 2:
                    hazard = True
                else:
                    executed = number(entry["executedQty"], zero=True)
                    protected = entry["status"] == "FILLED" and all(o["status"] == "NEW" for o in exits)
                    if entry["status"] == "FILLED":
                        preview = result["preview"]
                        sellable = self.ledger.balance(intent["strategy"], preview["base"])
                        remaining = max((number(o["origQty"]) - number(o.get("executedQty", "0"), zero=True) for o in exits), default=Decimal(0))
                        if protected and remaining > sellable:
                            protected = False
                        self.ledger.db.execute("UPDATE intents SET asset=?,reserved=? WHERE id=?",
                                               (preview["base"], str(remaining), key))
                    hazard = executed > 0 and not protected and not any(o["status"] == "FILLED" for o in exits)
                    if entry["status"] == "PARTIALLY_FILLED":
                        hazard = True
            elif op == "oco":
                protected = len(orders) == 2 and all(o["status"] == "NEW" for o in orders)
                hazard = not protected and not any(o["status"] == "FILLED" for o in orders)
            if len(orders) != (3 if op == "otoco" else 2 if op == "oco" else 1):
                raise BinanceClientError("order list child coverage incomplete")
            done = all(o["status"] in TERMINAL for o in orders)
            if hazard:
                self.ledger.pause("missing or inactive protection for " + key + "; cancel entry, reconcile fills, then attach owned OCO")
            self.ledger.update(key, "RESOLVED" if done else "OPEN",
                               result={**result, "exchange": response, "orders": orders,
                                       "protected": protected, "protectionUncertain": hazard})
        elif op == "cancel":
            target = await self.reconcile(p["targetIntentId"])
            if target["state"] == "RESOLVED":
                self.ledger.update(key, "RESOLVED", result={"target": target["id"]})
        else:
            await self.reconcile_investment(intent)
        return self.ledger.get(key)

    async def apply_order_fills(self, intent: dict, order: dict) -> None:
        rows = []
        cursor = 0
        for _ in range(100):
            batch = await self.client.signed_get("spot", "/api/v3/myTrades",
                       {"symbol": order["symbol"], "orderId": order["orderId"], "limit": 1000, "fromId": cursor})
            rows.extend(t for t in batch if t["orderId"] == order["orderId"])
            if len(batch) < 1000:
                break
            cursor = int(batch[-1]["id"]) + 1
        else:
            raise BinanceClientError("fill pagination incomplete")
        if sum((number(t["qty"]) for t in rows), Decimal(0)) != number(order["executedQty"], zero=True):
            raise BinanceClientError("fill history not yet complete")
        preview = intent["result"]["preview"]
        for trade in rows:
            self.apply_fill(intent, trade, preview)

    def apply_fill(self, intent: dict, trade: dict, preview: dict, *, paper: bool = False) -> None:
        strategy, base, quote = intent["strategy"], preview["base"], preview["quote"]
        qty, proceeds, commission = number(trade["qty"]), number(trade["quoteQty"]), number(trade["commission"], zero=True)
        fee_asset = trade["commissionAsset"]
        key = ("paper:" if paper else "trade:") + preview["params"]["symbol"] + ":" + str(trade["id"])
        with self.ledger.transaction():
            if not self.ledger.event(key, strategy, "fill", base, qty, detail=trade):
                return
            buy = trade["isBuyer"]
            fee_quote = commission if fee_asset == quote else commission * number(trade["price"]) if fee_asset == base else Decimal(0)
            if commission and fee_asset not in (base, quote):
                raise BinanceClientError("third-asset commission requires owned fee reserve and valuation; paused")
            if buy:
                net = qty - (commission if fee_asset == base else 0)
                cost = proceeds + (commission if fee_asset == quote else 0)
                self.ledger.change(strategy, quote, "SPOT", -cost)
                self.ledger.change(strategy, base, "SPOT", net, cost)
            else:
                sold = qty + (commission if fee_asset == base else 0)
                balance = self.ledger.balance(strategy, base)
                row = self.ledger.db.execute("SELECT cost FROM balances WHERE strategy=? AND asset=? AND location='SPOT'", (strategy, base)).fetchone()
                cost = Decimal(row[0]) * sold / balance
                net = proceeds - (commission if fee_asset == quote else 0)
                pnl = net - cost
                self.ledger.change(strategy, base, "SPOT", -sold, -cost)
                self.ledger.change(strategy, quote, "SPOT", net)
                self.ledger.event(key + ":pnl", strategy, "realized", quote, net, pnl)
                if pnl > 0 and not self.ledger.strategy(strategy)["reinvest"]:
                    self.ledger.change(strategy, quote, "SPOT", -pnl)
                    self.ledger.change(strategy, quote, "PROFIT_RESERVE", pnl)
            self.ledger.event(key + ":fee", strategy, "commission", fee_asset, commission, fee_quote)
            current = self.ledger.get(intent["id"])
            released = qty * number(preview["params"]["price"]) * (1 + number(preview["feeRateBound"], zero=True)) if buy else qty
            if current["asset"] == (quote if buy else base):
                remaining = max(Decimal(0), Decimal(current["reserved"]) - released)
                self.ledger.db.execute("UPDATE intents SET reserved=? WHERE id=?", (str(remaining), intent["id"]))
            if not paper:
                expected = self.ledger.meta("expectedTotals")
                if expected is not None:
                    expected[base] = str(Decimal(expected.get(base, "0")) + (qty if buy else -qty) - (commission if fee_asset == base else 0))
                    expected[quote] = str(Decimal(expected.get(quote, "0")) + (-proceeds if buy else proceeds) - (commission if fee_asset == quote else 0))
                    self.ledger.set_meta("expectedTotals", expected)

    async def reconcile_investment(self, intent: dict) -> None:
        p, op, result = intent["payload"]["params"], intent["payload"]["operation"], intent["result"] or {}
        preview = result.get("preview", {})
        response = result.get("response", {})
        checkpoint = result.get("checkpoint", {})
        partial_redemption = op == "earn_to_dual" and (
            (response.get("subscribed") is False and response.get("fundsLocation") == "SPOT")
            or response.get("outcome") == "REJECTED"
            or result.get("phase") in ("earn_redeem_sending", "earn_redeem_accepted"))
        if partial_redemption:
            raw = response.get("redeemResponse", checkpoint.get("redeemResponse", {}))
            record_id = raw.get("redeemId")
            if record_id is None:
                raise BinanceClientError("redemption outcome unknown; unique exchange history identifier required")
            await self.verify_earn_history(intent, "earn_redeem", p["earnProductId"], record_id)
            if await self.investment._spot_free(intent["asset"]) < number(p["amount"]):
                raise BinanceClientError("redeemed Spot receipt unavailable")
            destination = "SPOT"
        elif op in ("dual_subscribe", "earn_to_dual"):
            checkpoint = result.get("checkpoint", {})
            product = response.get("product") or response.get("postRedemptionGuards", {}).get("product") or checkpoint.get("product") or preview["contract"]["product"]
            position = await self.investment._verify_subscription(product=product, deposit_amount=p["amount"], auto_compound_plan="NONE",
                response=response.get("response", response.get("subscribeResponse", {})),
                previous_ids=set(response.get("previousPositionIds", checkpoint.get("previousPositionIds", preview["contract"]["previousPositionIds"]))))
            destination = "DI:" + str(position["positionId"])
        else:
            raw = response.get("response", {})
            record_id = raw.get("purchaseId" if op == "earn_subscribe" else "redeemId")
            if record_id is None:
                raise BinanceClientError("Earn write identifier unavailable; operator reconciliation required")
            await self.verify_earn_history(intent, op, p["productId"], record_id)
            destination = "EARN:" + p["productId"] if op == "earn_subscribe" else "SPOT"
        with self.ledger.transaction():
            if self.ledger.event("move:" + intent["id"], intent["strategy"], "transfer", intent["asset"], number(p["amount"]), detail={"destination": destination}):
                self.ledger.change(intent["strategy"], intent["asset"], intent["location"], -number(p["amount"]))
                self.ledger.change(intent["strategy"], intent["asset"], destination, number(p["amount"]))
            self.ledger.update(intent["id"], "RESOLVED", result={**result, "verifiedDestination": destination})

    async def verify_earn_history(self, intent: dict, op: str, product_id: str, record_id) -> None:
        preview, p = intent["result"]["preview"], intent["payload"]["params"]
        records = await self.snapshots.pages("/sapi/v1/simple-earn/flexible/history/" + ("subscriptionRecord" if op == "earn_subscribe" else "redemptionRecord"),
                     extra={"startTime": preview["historyStart"], "endTime": int(time.time() * 1000)})
        id_field = "purchaseId" if op == "earn_subscribe" else "redeemId"
        matches = [r for r in records if str(r.get(id_field)) == str(record_id)
                   and r.get("productId", r.get("projectId")) == product_id and r.get("asset") == intent["asset"]
                   and number(r["amount"]) == number(p["amount"]) and r.get("status") == "SUCCESS"]
        if len(matches) != 1:
            raise BinanceClientError("Earn history has not verified exact transfer")

    async def reconcile_all(self) -> None:
        for intent in self.ledger.outstanding(live_only=True):
            try:
                await self.reconcile(intent["id"])
            except Exception:
                self.ledger.pause("reconciliation incomplete for " + intent["id"])

    async def check_account(self) -> dict:
        snapshot = await self.snapshots.portfolio()
        if not snapshot["complete"]:
            self.ledger.pause("account coverage incomplete")
            raise BinanceClientError("account coverage incomplete")
        totals = snapshot["underlyingTotals"]
        locations = {}
        for balance in snapshot["coverage"]["spot"]["data"]["balances"]:
            if not balance["asset"].startswith("LD"):
                locations[(balance["asset"], "SPOT")] = number(balance["free"], zero=True) + number(balance["locked"], zero=True)
        for balance in snapshot["coverage"]["earn"]["data"]:
            key = (balance["asset"], "EARN:" + balance["productId"])
            locations[key] = locations.get(key, Decimal(0)) + number(balance["totalAmount"], zero=True)
        for balance in snapshot["coverage"]["dual"]["data"]:
            if balance["status"] == "PURCHASE_SUCCESS":
                locations[(balance["investCoin"], "DI:" + str(balance["positionId"]))] = number(balance["depositAmount"])
        required_locations = {}
        for row in self.ledger.db.execute("SELECT b.asset,b.location,b.quantity FROM balances b JOIN strategies s ON b.strategy=s.id WHERE s.mode='live'"):
            key = (row["asset"], "SPOT" if row["location"] == "PROFIT_RESERVE" else row["location"])
            required_locations[key] = required_locations.get(key, Decimal(0)) + Decimal(row["quantity"])
        if any(v > locations.get(k, Decimal(0)) for k, v in required_locations.items()):
            self.ledger.pause("account location ownership requires reconciliation")
            raise BinanceClientError("strategy capital is not backed at its recorded account location")

        expected = self.ledger.meta("expectedTotals")
        if expected is None:
            required: dict[str, Decimal] = {}
            for row in self.ledger.db.execute("SELECT b.asset,b.quantity FROM balances b JOIN strategies s ON b.strategy=s.id WHERE s.mode='live'"):
                required[row["asset"]] = required.get(row["asset"], Decimal(0)) + Decimal(row["quantity"])
            if any(Decimal(totals.get(a, "0")) < v for a, v in required.items()):
                self.ledger.pause("deployment allocations exceed account capital")
                raise BinanceClientError("deployment allocations exceed account capital")
            self.ledger.set_meta("expectedTotals", totals)
        elif any(Decimal(totals.get(a, "0")) != Decimal(expected.get(a, "0")) for a in set(totals) | set(expected)):
            self.ledger.pause("outside activity, reward or settlement requires audited reconciliation")
            raise BinanceClientError("account differs from ledger expected totals")
        ids = set()
        for row in self.ledger.db.execute("SELECT id FROM intents"):
            ids.update(client_id(row[0], suffix) for suffix in ("", "entry", "take", "stop"))
        if any(o.get("clientOrderId") not in ids for o in snapshot["coverage"]["orders"]["data"]):
            self.ledger.pause("unmanaged open orders require reconciliation")
            raise BinanceClientError("unmanaged open orders")
        self.ledger.set_meta("reconciledAt", int(time.time() * 1000))
        return snapshot

    async def paper_reconcile(self, intent: dict) -> dict:
        result = intent["result"] or {}
        op = intent["payload"]["operation"]
        if op not in ("order", "oco", "otoco") or not result.get("paper"):
            return intent
        p = intent["payload"]["params"]
        candles = await self.client.public_get("spot", "/api/v3/klines", {"symbol": p["symbol"], "interval": "1m", "limit": 1000})
        last = result.get("lastCandle", intent["created"])
        closed = [c for c in candles if int(c[6]) < int(time.time() * 1000)]
        if not closed or int(closed[-1][6]) < last:
            raise BinanceClientError("paper candle observations stale or missing")
        if int(closed[0][0]) > last + 60000:
            raise BinanceClientError("paper candle coverage gap; outcome remains unresolved")
        for c in closed:
            if int(c[0]) <= last:
                continue
            if int(c[0]) > last + 60000:
                raise BinanceClientError("paper candle coverage gap; outcome remains unresolved")
            high, low = number(c[2]), number(c[3])
            preview = result["preview"]
            step = number(next(f for f in preview["filters"] if f["filterType"] == "LOT_SIZE")["stepSize"])
            # A conservative participation cap avoids fictitious fills in zero/thin-volume candles.
            capacity = (number(c[5], zero=True) * Decimal("0.1") / step).to_integral_value(rounding=ROUND_DOWN) * step
            buy = op == "otoco" and result["paperStatus"] in ("NEW", "PARTIALLY_FILLED")
            fill_price = None
            if buy and low <= number(p["price"]):
                fill_price = number(p["price"])
            elif op == "order" and high >= number(p["price"]):
                fill_price = number(p["price"])
            elif not buy and op in ("oco", "otoco"):
                if low <= number(p["stopPrice"]):
                    fill_price = min(number(p["stopPrice"]), number(c[1]))
                elif high >= number(p["takeProfit"]):
                    fill_price = number(p["takeProfit"])
            with self.ledger.transaction():
                if fill_price is not None and capacity:
                    field = "entryFilled" if buy else "exitFilled"
                    filled = number(result.get(field, "0"), zero=True)
                    target = number(p["quantity"] if buy or op != "otoco" else preview["sellableQuantity"])
                    qty = min(target - filled, capacity)
                    fee = qty * number(preview["feeRateBound"], zero=True) if buy else qty * fill_price * number(preview["feeRateBound"], zero=True)
                    trade = {"id": intent["id"] + (":entry:" if buy else ":exit:") + str(c[0]),
                             "qty": str(qty), "quoteQty": str(qty*fill_price), "price": str(fill_price),
                             "commission": str(fee), "commissionAsset": preview["base"] if buy else preview["quote"], "isBuyer": buy}
                    self.apply_fill(intent, trade, preview, paper=True)
                    result[field] = str(filled + qty)
                    complete = filled + qty == target
                    result["paperStatus"] = ("ENTRY_FILLED" if complete else "PARTIALLY_FILLED") if buy else ("FILLED" if complete else "EXIT_PARTIALLY_FILLED")
                    result["protected"] = buy and complete
                    if buy:
                        if complete:
                            self.ledger.db.execute("UPDATE intents SET asset=?,reserved=? WHERE id=?", (preview["base"], preview["sellableQuantity"], intent["id"]))
                            self.clear_paper_pause_if_protected(intent["strategy"])
                            # Entry/stop ordering is unknowable within a candle; model adverse stop
                            # on the newly activated full entry if that candle crosses the stop.
                            if low <= number(p["stopPrice"]) and capacity - qty >= number(preview["sellableQuantity"]):
                                exit_qty = number(preview["sellableQuantity"])
                                stop = min(number(p["stopPrice"]), number(c[1]))
                                exit_trade = {"id": intent["id"] + ":same-candle-stop:" + str(c[0]), "qty": str(exit_qty),
                                              "quoteQty": str(exit_qty * stop), "price": str(stop),
                                              "commission": str(exit_qty * stop * number(preview["feeRateBound"], zero=True)),
                                              "commissionAsset": preview["quote"], "isBuyer": False}
                                self.apply_fill(intent, exit_trade, preview, paper=True)
                                result["paperStatus"], result["protected"] = "FILLED", False
                        else:
                            self.ledger.set_meta("paperPause:" + intent["strategy"], "partial entry has inactive exits; cancel and protect owned quantity")
                result["lastCandle"] = int(c[0])
                self.ledger.update(intent["id"], "RESOLVED" if result["paperStatus"] == "FILLED" else "OPEN", result=result)
            last = int(c[0])
            self.clear_paper_pause_if_protected(intent["strategy"])
            if result["paperStatus"] == "FILLED":
                break
        return self.ledger.get(intent["id"])

    def clear_paper_pause_if_protected(self, strategy: str) -> None:
        cfg = self.ledger.strategy(strategy)
        intents = [i for i in self.ledger.outstanding() if i["strategy"] == strategy]
        for row in self.ledger.db.execute("SELECT asset,quantity FROM balances WHERE strategy=? AND location='SPOT' AND asset!=?", (strategy, cfg["quote"])):
            covered = Decimal(0)
            step = None
            for i in intents:
                result = i["result"] or {}
                preview = result.get("preview", {})
                if preview.get("base") == row["asset"]:
                    step = number(next(f for f in preview["filters"] if f["filterType"] == "LOT_SIZE")["stepSize"])
                    if result.get("protected") and i["asset"] == row["asset"]:
                        covered += number(i["reserved"], zero=True)
            if step is None or Decimal(row["quantity"]) - covered >= step:
                return
        self.ledger.set_meta("paperPause:" + strategy, None)

    async def check_protection(self) -> None:
        for strategy in self.ledger.db.execute("SELECT id,quote FROM strategies WHERE mode='live'").fetchall():
            balances = self.ledger.db.execute("SELECT asset,quantity FROM balances WHERE strategy=? AND location='SPOT' AND asset!=?",
                                              (strategy["id"], strategy["quote"])).fetchall()
            for row in balances:
                if not Decimal(row["quantity"]):
                    continue
                covered = Decimal(0)
                step = None
                for intent in self.ledger.outstanding(live_only=True):
                    result = intent["result"] or {}
                    preview = result.get("preview", {})
                    if intent["strategy"] != strategy["id"] or preview.get("base") != row["asset"]:
                        continue
                    lot = next((f for f in preview.get("filters", []) if f["filterType"] == "LOT_SIZE"), None)
                    if lot:
                        step = number(lot["stepSize"])
                    if result.get("protected") and not result.get("protectionUncertain"):
                        covered += number(intent["reserved"], zero=True)
                if step is None:
                    info = await self.symbol(row["asset"] + strategy["quote"])
                    step = number(next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")["stepSize"])
                if Decimal(row["quantity"]) - covered >= step:
                    self.ledger.pause("unprotected owned quantity for " + strategy["id"] + ":" + row["asset"])
                    raise BinanceClientError("unprotected owned quantity")
