"""Durable decimal ledger. All reservations and state transitions use SQLite transactions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .client import BinanceClientError


def number(value: Any, *, zero: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BinanceClientError("invalid decimal") from exc
    if not result.is_finite() or result < 0 or (not zero and result == 0):
        raise BinanceClientError("amount must be finite and positive")
    return result


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


class Ledger:
    def __init__(self, path: str, allocations: dict[str, Any] | None = None):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS strategies(id TEXT PRIMARY KEY, mode TEXT NOT NULL,
                quote TEXT NOT NULL, initial TEXT NOT NULL, reinvest INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS balances(strategy TEXT, asset TEXT, location TEXT,
                quantity TEXT NOT NULL, cost TEXT NOT NULL DEFAULT '0',
                PRIMARY KEY(strategy,asset,location));
            CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, strategy TEXT NOT NULL,
                fingerprint TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
                asset TEXT NOT NULL, location TEXT NOT NULL, reserved TEXT NOT NULL,
                created INTEGER NOT NULL, result TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, strategy TEXT NOT NULL,
                category TEXT NOT NULL, asset TEXT NOT NULL, amount TEXT NOT NULL,
                quote_value TEXT NOT NULL, created INTEGER NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        with self.transaction():
            for key, cfg in (allocations or {}).items():
                amount = number(cfg["allocation"])
                mode = cfg.get("mode", "paper")
                quote = cfg.get("quote", "USDT")
                if mode not in ("paper", "live"):
                    raise BinanceClientError("strategy mode must be paper or live")
                location = cfg.get("location", "SPOT")
                if location != "SPOT" and not (location.startswith("EARN:") and len(location) > 5):
                    raise BinanceClientError("initial allocation location must be SPOT or EARN:productId")
                policy_digest = self.fingerprint({"mode": mode, "quote": quote, "allocation": str(amount), "reinvest": cfg.get("reinvest", True), "location": location})
                saved_policy = self.meta("allocationPolicy:" + key)
                if saved_policy is not None and saved_policy != policy_digest:
                    raise BinanceClientError("existing allocation policy is immutable")
                old = self.db.execute("SELECT * FROM strategies WHERE id=?", (key,)).fetchone()
                if old:
                    if (old["mode"], old["quote"], Decimal(old["initial"]), bool(old["reinvest"])) != (
                        mode, quote, amount, cfg.get("reinvest", True)
                    ):
                        raise BinanceClientError("existing allocation is immutable; use an audited migration")
                    continue
                self.db.execute("INSERT INTO strategies VALUES(?,?,?,?,?)",
                                (key, mode, quote, str(amount), int(cfg.get("reinvest", True))))
                self.db.execute("INSERT INTO balances VALUES(?,?,?,?,?)", (key, quote, location, str(amount), "0"))
                self.set_meta("allocationPolicy:" + key, policy_digest)

    @contextmanager
    def transaction(self):
        nested = self.db.in_transaction
        if nested:
            self.db.execute("SAVEPOINT ledger_nested")
        else:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK TO ledger_nested" if nested else "ROLLBACK")
            if nested:
                self.db.execute("RELEASE ledger_nested")
            raise
        else:
            self.db.execute("RELEASE ledger_nested" if nested else "COMMIT")

    def strategy(self, key: str) -> dict:
        row = self.db.execute("SELECT * FROM strategies WHERE id=?", (key,)).fetchone()
        if row is None:
            raise BinanceClientError("strategy must be provisioned by deployment allocation policy")
        return dict(row)

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM intents WHERE id=?", (key,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        for field in ("payload", "result", "error"):
            value[field] = json.loads(value[field]) if value[field] else None
        return value

    @staticmethod
    def fingerprint(payload: dict) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def balance(self, strategy: str, asset: str, location: str = "SPOT") -> Decimal:
        row = self.db.execute("SELECT quantity FROM balances WHERE strategy=? AND asset=? AND location=?",
                              (strategy, asset, location)).fetchone()
        return Decimal(row[0]) if row else Decimal(0)

    def available(self, strategy: str, asset: str, location: str = "SPOT") -> Decimal:
        held = sum((Decimal(r[0]) for r in self.db.execute(
            "SELECT reserved FROM intents WHERE strategy=? AND asset=? AND location=? "
            "AND state NOT IN ('RESOLVED','REJECTED')", (strategy, asset, location))), Decimal(0))
        return self.balance(strategy, asset, location) - held

    def begin(self, key: str, strategy: str, payload: dict, asset: str, location: str, amount: str,
              *, recovery: bool = False, prepared: dict | None = None) -> tuple[dict, bool]:
        if not key or len(key) > 128:
            raise BinanceClientError("a stable intentId of 1..128 characters is mandatory")
        amount_d = number(amount, zero=True)
        fingerprint = self.fingerprint(payload)
        with self.transaction():
            old = self.get(key)
            if old:
                if old["strategy"] != strategy or old["fingerprint"] != fingerprint:
                    raise BinanceClientError("intentId reused with a different contract")
                return old, False
            cfg = self.strategy(strategy)
            if cfg["mode"] == "paper" and not recovery and self.meta("paperPause:" + strategy):
                raise BinanceClientError("paper strategy paused: " + self.meta("paperPause:" + strategy))
            if cfg["mode"] == "live" and not recovery:
                pause = self.meta("pause")
                if pause:
                    raise BinanceClientError("new execution paused: " + pause)
                if self.db.execute("SELECT 1 FROM intents i JOIN strategies s ON i.strategy=s.id "
                                   "WHERE s.mode='live' AND i.state IN ('PREPARED','OUTCOME_UNKNOWN')").fetchone():
                    raise BinanceClientError("unresolved execution blocks further expenditure")
            if self.available(strategy, asset, location) < amount_d:
                raise BinanceClientError("insufficient available strategy capital")
            self.db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (key, strategy, fingerprint, json.dumps(payload, sort_keys=True), "PREPARED",
                             asset, location, str(amount_d), int(time.time() * 1000), json.dumps(prepared) if prepared is not None else None, None))
            return self.get(key), True

    def update(self, key: str, state: str, *, result: Any = None, error: Any = None) -> None:
        self.db.execute("UPDATE intents SET state=?,result=?,error=? WHERE id=?",
                        (state, json.dumps(result) if result is not None else None,
                         json.dumps(error) if error is not None else None, key))

    def meta(self, key: str) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)))

    def pause(self, reason: str) -> None:
        self.set_meta("pause", reason)

    def change(self, strategy: str, asset: str, location: str, delta: Decimal, cost_delta: Decimal = Decimal(0)) -> None:
        row = self.db.execute("SELECT quantity,cost FROM balances WHERE strategy=? AND asset=? AND location=?",
                              (strategy, asset, location)).fetchone()
        qty = (Decimal(row[0]) if row else Decimal(0)) + delta
        cost = (Decimal(row[1]) if row else Decimal(0)) + cost_delta
        if qty < 0 or cost < 0:
            raise BinanceClientError("ledger ownership deficit; reconciliation required")
        self.db.execute("INSERT INTO balances VALUES(?,?,?,?,?) ON CONFLICT(strategy,asset,location) "
                        "DO UPDATE SET quantity=excluded.quantity,cost=excluded.cost",
                        (strategy, asset, location, str(qty), str(cost)))

    def event(self, key: str, strategy: str, category: str, asset: str, amount: Decimal,
              quote_value: Decimal = Decimal(0), detail: dict | None = None) -> bool:
        if self.db.execute("SELECT 1 FROM events WHERE id=?", (key,)).fetchone():
            return False
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                        (key, strategy, category, asset, str(amount), str(quote_value),
                         int(time.time() * 1000), json.dumps(detail or {})))
        return True

    def outstanding(self, *, live_only: bool = False) -> list[dict]:
        query = "SELECT i.id FROM intents i JOIN strategies s ON i.strategy=s.id WHERE i.state NOT IN ('RESOLVED','REJECTED')"
        if live_only:
            query += " AND s.mode='live'"
        return [self.get(r[0]) for r in self.db.execute(query)]

    def report(self, strategy: str, prices: dict[str, str] | None = None) -> dict:
        cfg = self.strategy(strategy)
        balances = [dict(r) for r in self.db.execute("SELECT * FROM balances WHERE strategy=?", (strategy,))]
        events = [dict(r) for r in self.db.execute("SELECT * FROM events WHERE strategy=? ORDER BY created", (strategy,))]
        totals: dict[str, Decimal] = {}
        for event in events:
            category = event["category"]
            totals[category] = totals.get(category, Decimal(0)) + Decimal(event["quote_value"])
        unrealized = Decimal(0)
        missing = []
        for row in balances:
            row["available"] = str(self.available(strategy, row["asset"], row["location"]))
            if row["asset"] != cfg["quote"] and Decimal(row["quantity"]):
                if row["asset"] not in (prices or {}):
                    missing.append(row["asset"])
                else:
                    unrealized += Decimal(row["quantity"]) * number(prices[row["asset"]]) - Decimal(row["cost"])
        return {"strategy": cfg, "balances": balances, "events": events[-200:], "eventCount": len(events),
                "paperPauseReason": self.meta("paperPause:" + strategy),
                "resultsByCategory": {k: str(v) for k, v in totals.items()},
                "unrealizedQuote": str(unrealized) if not missing else None,
                "missingValuations": missing, "pauseReason": self.meta("pause"),
                "intents": [i for i in self.outstanding() if i["strategy"] == strategy]}

