"""Bounded semantic account and market reads; missing coverage is explicit."""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

from .client import BinanceClientError
from .investment import InvestmentService
from .ledger import number
from .pagination import Coverage


class Snapshots:
    def __init__(self, client, ledger, symbols: list[str]):
        self.client, self.ledger, self.symbols = client, ledger, set(symbols)

    async def pages(self, path: str, *, rows: str = "rows", page_key: str = "current",
                    size_key: str = "size", extra: dict | None = None) -> list[dict]:
        collected = []
        seen = set()
        coverage = Coverage()
        for page in range(1, 101):
            data = await self.client.signed_get("spot", path, {**(extra or {}), page_key: page, size_key: 100})
            if not isinstance(data, dict) or rows not in data:
                raise BinanceClientError("unexpected paginated response; coverage incomplete")
            batch = data[rows]
            if not isinstance(batch, list):
                raise BinanceClientError("unexpected page rows; coverage incomplete")
            signature = repr(batch)
            if batch and signature in seen:
                raise BinanceClientError("pagination repeated; coverage incomplete")
            seen.add(signature)
            collected.extend(batch)
            if coverage.complete(data.get("total"), len(collected), len(batch)):
                return collected
        raise BinanceClientError("pagination limit exceeded; coverage incomplete")

    async def portfolio(self) -> dict:
        started = int(time.time() * 1000)
        calls = {
            "spot": self.client.signed_get("spot", "/api/v3/account", {"omitZeroBalances": True}),
            "earn": self.pages("/sapi/v1/simple-earn/flexible/position"),
            "dual": InvestmentService(self.client).all_positions(),
            "orders": self.client.signed_get("spot", "/api/v3/openOrders"),
            "lists": self.client.signed_get("spot", "/api/v3/openOrderList"),
        }
        async def observe(name, call):
            try:
                data = await call
                if name == 'spot':
                    rows = data.get('balances') if isinstance(data, dict) else None
                    if not isinstance(rows, list) or len({b['asset'] for b in rows}) != len(rows):
                        raise BinanceClientError('Spot coverage incomplete')
                    for b in rows:
                        number(b['free'], zero=True); number(b['locked'], zero=True)
                elif name in ('orders', 'lists'):
                    if not isinstance(data, list):
                        raise BinanceClientError('order coverage incomplete')
                    if name == 'orders' and (any(not {'symbol', 'orderId', 'clientOrderId'} <= b.keys() for b in data) or len({(b['symbol'], str(b['orderId'])) for b in data}) != len(data)):
                        raise BinanceClientError('order identity coverage incomplete')
                elif name == 'earn':
                    if len({(b['asset'], b['productId']) for b in data}) != len(data):
                        raise BinanceClientError('Earn coverage contradictory')
                    for b in data:
                        number(b['totalAmount'], zero=True)
                elif name == 'dual':
                    for b in data:
                        if not b.get('positionId') or not b.get('investCoin') or b.get('status') not in ('PURCHASE_SUCCESS', 'PENDING', 'PURCHASE_FAIL', 'SETTLED'):
                            raise BinanceClientError('DI coverage incomplete')
                        number(b['depositAmount'], zero=True)
                return {"observedAt": int(time.time() * 1000), "data": data}
            except Exception as exc:
                return {"observedAt": int(time.time() * 1000), "error": "COVERAGE_UNAVAILABLE"}
        values = await asyncio.gather(*(observe(k, c) for k,c in calls.items()))
        coverage = dict(zip(calls, values))
        missing = [k for k, v in coverage.items() if "error" in v]
        totals: dict[str, Decimal] = {}
        receipts = []
        if "spot" not in missing:
            for b in coverage["spot"]["data"].get("balances", []):
                asset = b["asset"]
                # LD* are Simple Earn receipt representations, never extra underlying capital.
                if asset.startswith("LD"):
                    receipts.append(b)
                    continue
                totals[asset] = totals.get(asset, Decimal(0)) + number(b["free"], zero=True) + number(b["locked"], zero=True)
        if "earn" not in missing:
            for b in coverage["earn"]["data"]:
                asset = b["asset"]
                totals[asset] = totals.get(asset, Decimal(0)) + number(b["totalAmount"], zero=True)
        if "dual" not in missing:
            for b in coverage["dual"]["data"]:
                if b["status"] in ("PURCHASE_SUCCESS", "PENDING"):
                    asset = b["investCoin"]
                    totals[asset] = totals.get(asset, Decimal(0)) + number(b["depositAmount"], zero=True)
        reservations = [{"intentId": i["id"], "strategyId": i["strategy"], "asset": i["asset"],
                         "location": i["location"], "quantity": i["reserved"], "state": i["state"]}
                        for i in self.ledger.outstanding()]
        return {"startedAt": started, "observedAt": int(time.time() * 1000), "coverage": coverage,
                "missingData": missing, "complete": not missing,
                "completeScope": ['spot', 'flexibleEarn', 'dualInvestment', 'spotOpenOrders', 'spotOrderLists'],
                "accountWideComplete": False,
                "excludedCoverage": ['margin', 'futures', 'options', 'portfolioMargin', 'fundingWallet', 'lockedEarn', 'staking', 'externalWallets'],
                "pagination": {"pageLimit": 100, "pageSize": 100, "bounded": True},
                "underlyingTotals": {a: str(v) for a, v in totals.items()}, "receiptRepresentations": receipts,
                "strategyReservations": reservations,
                "reconciliation": {"pauseReason": self.ledger.meta("pause"),
                                   "lastSuccessfulAt": self.ledger.meta("reconciledAt")}}

    async def market(self, symbols: list[str], shortlist: list[str] | None = None) -> dict:
        if not symbols or len(symbols) > 20 or len(set(symbols)) != len(symbols) or not set(symbols) <= self.symbols:
            raise BinanceClientError("scan requires 1..20 unique symbols from deployment-approved universe")
        shortlist = shortlist or []
        if len(shortlist) > 5 or not set(shortlist) <= set(symbols):
            raise BinanceClientError("detailed shortlist must be a subset of at most five scan symbols")
        rows = []
        for symbol in symbols:
            candles, book = await asyncio.gather(
                self.client.public_get("spot", "/api/v3/klines", {"symbol": symbol, "interval": "1h", "limit": 25}),
                self.client.public_get("spot", "/api/v3/ticker/bookTicker", {"symbol": symbol}),
            )
            now = int(time.time() * 1000)
            closed = [c for c in candles if int(c[6]) < now]
            if len(closed) < 2:
                raise BinanceClientError("insufficient closed candles")
            if not 0 <= now-int(closed[-1][6]) <= 3600000 + 90000:
                raise BinanceClientError('market candles stale', blocker='OBSERVATION_STALE')
            if any(int(closed[i][0]) - int(closed[i-1][0]) != 3600000 for i in range(1, len(closed))):
                raise BinanceClientError('market candle coverage incomplete', blocker='COVERAGE_INCOMPLETE')
            closes = [number(c[4]) for c in closed]
            returns = [closes[i] / closes[i-1] - 1 for i in range(1, len(closes))]
            mean = sum(returns) / len(returns)
            volatility = (sum((r-mean)**2 for r in returns) / len(returns)).sqrt()
            bid, ask = number(book["bidPrice"]), number(book["askPrice"])
            if bid > ask:
                raise BinanceClientError('market book contradictory', blocker='INVALID_EVIDENCE')
            row: dict[str, Any] = {"symbol": symbol, "observedAt": now,
                "changePercent": str((closes[-1] / closes[0] - 1) * 100),
                "volatilityPercent": str(volatility * 100),
                "quoteVolume": str(sum(number(c[7], zero=True) for c in closed)),
                "spreadBps": str((ask-bid) / ((ask+bid)/2) * 10000),
                "window": {"interval": "1h", "firstOpenAt": int(closed[0][0]), "lastClosedAt": int(closed[-1][6]),
                           "bookTimestampSource": "LOCAL_RECEIPT", "bookReceivedAt": now},
                "observations": {"closedCandles": closed, "bookTicker": book}}
            if symbol in shortlist:
                row["observations"]["depth"] = await self.client.public_get("spot", "/api/v3/depth", {"symbol": symbol, "limit": 20})
            rows.append(row)
        return {"observedAt": int(time.time() * 1000), "symbols": rows}
