"""Owner-only, offline-policy accounting import. Never exposed as an MCP tool."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

from .client import BinanceClient, BinanceClientError
from .execution import ExecutionService
from .ledger import Ledger, number

CATEGORIES = {"deposit", "withdrawal", "earn_reward", "settlement", "commission", "loss", "transfer"}


def signed_decimal(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise BinanceClientError("audit decimal must be finite")
    return result


def apply_audit(ledger: Ledger, audit: dict, observed_totals: dict[str, str]) -> dict:
    """Apply owner-attributed movements only when complete observed account totals prove the proposed baseline.

    Attribution and evidence are supplied by the deployment owner. The MCP caller
    cannot change allocations or clear uncertain intents through this function.
    """
    if not audit.get("id") or not audit.get("evidence"):
        raise BinanceClientError("audit id and owner evidence references are mandatory")
    digest = Ledger.fingerprint(audit)
    key = "audit:" + audit["id"]
    with ledger.transaction():
        previous = ledger.meta(key)
        if previous:
            if previous != digest:
                raise BinanceClientError("audit id reused with different evidence")
            return {"id": audit["id"], "alreadyApplied": True}
        expected = ledger.meta("expectedTotals")
        if expected is None:
            raise BinanceClientError("account baseline must be established before importing movements")
        candidate = {a: Decimal(v) for a, v in expected.items()}
        for asset, delta in audit.get("accountDeltas", {}).items():
            candidate[asset] = candidate.get(asset, Decimal(0)) + signed_decimal(delta)
            if candidate[asset] < 0:
                raise BinanceClientError("audit creates negative account capital")
        if any(candidate.get(a, Decimal(0)) != number(observed_totals.get(a, "0"), zero=True)
               for a in set(candidate) | set(observed_totals)):
            raise BinanceClientError("audit does not explain complete observed account totals")
        for index, adjustment in enumerate(audit.get("adjustments", [])):
            strategy = adjustment["strategyId"]
            ledger.strategy(strategy)
            category = adjustment["category"]
            if category not in CATEGORIES:
                raise BinanceClientError("unsupported accounting category")
            quantity = signed_decimal(adjustment["quantityDelta"])
            cost = signed_decimal(adjustment.get("costDelta", "0"))
            quote_value = signed_decimal(adjustment.get("quoteValue", "0"))
            ledger.change(strategy, adjustment["asset"], adjustment["location"], quantity, cost)
            ledger.event(key + ":" + str(index), strategy, category, adjustment["asset"], quantity, quote_value,
                         detail={"evidence": audit["evidence"], "adjustment": adjustment})
        for resolution in audit.get("resolveIntents", []):
            intent = ledger.get(resolution["intentId"])
            if intent is None or intent["state"] not in ("PREPARED", "OUTCOME_UNKNOWN", "OPEN"):
                raise BinanceClientError("audit resolution must reference an outstanding intent")
            if resolution["state"] not in ("RESOLVED", "REJECTED"):
                raise BinanceClientError("audit terminal state must be RESOLVED or REJECTED")
            ledger.update(intent["id"], resolution["state"], result={**(intent["result"] or {}),
                          "ownerAuditId": audit["id"], "evidence": audit["evidence"]})
        # Global ownership cannot exceed actual capital after attributing movements.
        owned = {}
        for row in ledger.db.execute("SELECT b.asset,b.quantity FROM balances b JOIN strategies s ON b.strategy=s.id WHERE s.mode='live'"):
            owned[row["asset"]] = owned.get(row["asset"], Decimal(0)) + Decimal(row["quantity"])
        if any(v > candidate.get(a, Decimal(0)) for a, v in owned.items()):
            raise BinanceClientError("attributed strategy ownership exceeds exchange capital")
        ledger.set_meta("expectedTotals", {a: str(v) for a, v in candidate.items()})
        ledger.set_meta(key, digest)
        ledger.pause("monitor starting; owner audit imported, protection and account checks required")
    return {"id": audit["id"], "alreadyApplied": False}


async def run(path: str):
    audit = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    ledger = Ledger(os.getenv("BINANCE_LEDGER_PATH", "data/strategy.sqlite"))
    client = BinanceClient()
    svc = ExecutionService(client, ledger, os.getenv("BINANCE_APPROVED_SYMBOLS", "BTCUSDT,ETHUSDT").split(","))
    try:
        snapshot = await svc.snapshots.portfolio()
        if not snapshot["complete"]:
            raise BinanceClientError("audit requires complete live portfolio coverage")
        print(json.dumps(apply_audit(ledger, audit, snapshot["underlyingTotals"])))
    finally:
        await client.close()
        ledger.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import owner-reviewed accounting evidence; stop the service first.")
    parser.add_argument("evidence_json")
    asyncio.run(run(parser.parse_args().evidence_json))
