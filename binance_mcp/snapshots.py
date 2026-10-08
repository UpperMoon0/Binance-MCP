"""Bounded semantic account and market reads; missing coverage is explicit."""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

from .client import BinanceClientError
from .investment import InvestmentService
from .ledger import number


class Snapshots:
    def __init__(self, client, ledger, symbols: list[str]):
        self.client, self.ledger, self.symbols = client, ledger, set(symbols)

    async def pages(self, path: str, *, rows: str = "rows", page_key: str = "current",
                    size_key: str = "size", extra: dict | None = None) -> list[dict]:
        collected = []
        seen = set()
        for page in range(1, 101):
            data = await self.client.signed_get("spot", path, {**(extra or {}), page_key: page, size_key: 100})
            if not isinstance(data, dict) or rows not in data:
                raise BinanceClientError("unexpected paginated response; coverage incomplete")
            batch = data[rows]
            signature = repr(batch)
            if batch and signature in seen:
                raise BinanceClientError("pagination repeated; coverage incomplete")
            seen.add(signature)
            collected.extend(batch)
            if not batch or ("total" in data and len(collected) >= int(data["total"])) or (
                "total" not in data and len(batch) < 100
            ):
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
        async def observe(call):
            try:
                data = await call
                return {"observedAt": int(time.time() * 1000), "data": data}
            except Exception as exc:
                return {"observedAt": int(time.time() * 1000), "error": str(exc)}
        values = await asyncio.gather(*(observe(c) for c in calls.values()))
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
            closes = [number(c[4]) for c in closed]
            returns = [closes[i] / closes[i-1] - 1 for i in range(1, len(closes))]
            mean = sum(returns) / len(returns)
            volatility = (sum((r-mean)**2 for r in returns) / len(returns)).sqrt()
            bid, ask = number(book["bidPrice"]), number(book["askPrice"])
            row: dict[str, Any] = {"symbol": symbol, "observedAt": now,
                "changePercent": str((closes[-1] / closes[0] - 1) * 100),
                "volatilityPercent": str(volatility * 100),
                "quoteVolume": str(sum(number(c[7], zero=True) for c in closed)),
                "spreadBps": str((ask-bid) / ((ask+bid)/2) * 10000),
                "observations": {"closedCandles": closed, "bookTicker": book}}
            if symbol in shortlist:
                row["observations"]["depth"] = await self.client.public_get("spot", "/api/v3/depth", {"symbol": symbol, "limit": 20})
            rows.append(row)
        return {"observedAt": int(time.time() * 1000), "symbols": rows}
