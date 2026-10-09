from decimal import Decimal

import pytest

from binance_mcp.accounting import apply_audit
from binance_mcp.client import BinanceClientError
from binance_mcp.ledger import Ledger
from test_execution import setup, plan


@pytest.mark.asyncio
async def test_snapshot_receipts_are_not_double_counted_and_failures_are_explicit(tmp_path):
    ledger, exchange, svc = setup(tmp_path)
    original = exchange.signed_get
    async def signed(product, path, params=None):
        if path.endswith("/account"):
            return {"balances": [{"asset": "USDT", "free": "10", "locked": "2"},
                                  {"asset": "LDUSDT", "free": "100", "locked": "0"}]}
        if path.endswith("/flexible/position"):
            return {"total": 1, "rows": [{"asset": "USDT", "productId": "USDT001", "totalAmount": "100"}]}
        return await original(product, path, params)
    exchange.signed_get = signed
    ledger.begin("reserve", "experiment", {}, "USDT", "SPOT", "25")
    result = await svc.snapshots.portfolio()
    assert result["complete"]
    assert Decimal(result["underlyingTotals"]["USDT"]) == 112
    assert "LDUSDT" not in result["underlyingTotals"]
    assert result["receiptRepresentations"]
    assert result["strategyReservations"][0]["quantity"] == "25"
    assert all(v["observedAt"] >= result["startedAt"] for v in result["coverage"].values())
    async def failed(product, path, params=None):
        if path.endswith("/flexible/position"):
            raise BinanceClientError("unavailable")
        return await signed(product, path, params)
    exchange.signed_get = failed
    result = await svc.snapshots.portfolio()
    assert not result["complete"]
    assert result["missingData"] == ["earn"]


@pytest.mark.asyncio
async def test_market_scan_closed_observations_approved_universe_and_shortlist(tmp_path):
    _, exchange, svc = setup(tmp_path)
    import time
    t = int(time.time()*1000) - 7200000
    exchange.candles = [[t, "100", "110", "90", "100", "2", t+3599999, "200"],
                        [t+3600000, "100", "110", "90", "110", "3", t+7199999, "330"],
                        [9999999999999, "100", "110", "90", "999", "1", 99999999999999, "1"]]
    result = await svc.snapshots.market(["BTCUSDT"], ["BTCUSDT"])
    row = result["symbols"][0]
    assert Decimal(row["changePercent"]) == 10
    assert Decimal(row["quoteVolume"]) == 530
    assert len(row["observations"]["closedCandles"]) == 2
    assert row["observations"]["depth"]
    with pytest.raises(BinanceClientError, match="universe"):
        await svc.snapshots.market(["BADUSDT"])
    with pytest.raises(BinanceClientError, match="shortlist"):
        await svc.snapshots.market(["BTCUSDT"], ["ETHUSDT"])


@pytest.mark.asyncio
async def test_snapshot_repeated_pagination_never_claims_complete_coverage(tmp_path):
    _, exchange, svc = setup(tmp_path)
    async def repeated(*args, **kwargs):
        return {"total": 200, "rows": [{"asset": "USDT", "totalAmount": "1"}] * 100}
    exchange.signed_get = repeated
    with pytest.raises(BinanceClientError, match="repeated"):
        await svc.snapshots.pages("/sapi/v1/simple-earn/flexible/position")


def audit(delta="1"):
    return {"id": "reward", "evidence": ["owner-reviewed Binance history record"], "accountDeltas": {"USDT": delta},
            "adjustments": [{"strategyId": "experiment", "category": "earn_reward", "asset": "USDT",
                             "location": "SPOT", "quantityDelta": delta, "quoteValue": delta}]}


def test_owner_audit_is_evidence_backed_idempotent_and_categorized(tmp_path):
    ledger, _, _ = setup(tmp_path, "live")
    ledger.set_meta("expectedTotals", {"USDT": "100"})
    assert not apply_audit(ledger, audit(), {"USDT": "101"})["alreadyApplied"]
    assert apply_audit(ledger, audit(), {"USDT": "101"})["alreadyApplied"]
    assert ledger.balance("experiment", "USDT") == 101
    assert ledger.report("experiment")["resultsByCategory"]["earn_reward"] == "1"
    assert ledger.meta("pause").startswith("monitor starting")
    with pytest.raises(BinanceClientError, match="different evidence"):
        apply_audit(ledger, audit("2"), {"USDT": "102"})


def test_owner_audit_rolls_back_unexplained_or_overallocated_capital(tmp_path):
    ledger, _, _ = setup(tmp_path, "live")
    ledger.set_meta("expectedTotals", {"USDT": "100"})
    with pytest.raises(BinanceClientError, match="explain"):
        apply_audit(ledger, audit(), {"USDT": "102"})
    assert ledger.balance("experiment", "USDT") == 100
    oversized = audit()
    oversized["adjustments"][0]["quantityDelta"] = "100"
    with pytest.raises(BinanceClientError, match="ownership"):
        apply_audit(ledger, oversized, {"USDT": "101"})
    assert ledger.balance("experiment", "USDT") == 100
    assert ledger.meta("expectedTotals") == {"USDT": "100"}
    assert not ledger.report("experiment")["events"]


@pytest.mark.asyncio
async def test_initial_allocation_must_be_backed_at_exact_location(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.sqlite"), {"experiment": {"allocation": "100", "mode": "live", "location": "EARN:USDT001"}})
    from binance_mcp.execution import ExecutionService
    from test_execution import Exchange
    svc = ExecutionService(Exchange(), ledger, ["BTCUSDT"])
    with pytest.raises(BinanceClientError, match="location"):
        await svc.check_account()
    assert ledger.meta("pause")


@pytest.mark.asyncio
async def test_paper_execution_needs_no_signed_reads_or_writes(tmp_path):
    _, exchange, svc = setup(tmp_path)
    async def forbidden(*args, **kwargs):
        raise AssertionError("paper must not access signed account endpoints")
    exchange.signed_get = forbidden
    result = await svc.execute("experiment", "paper-only", "otoco", plan())
    assert result["state"] == "OPEN"
    assert not exchange.writes


@pytest.mark.asyncio
async def test_missing_trade_history_does_not_release_funds_or_claim_protection(tmp_path):
    ledger, exchange, svc = setup(tmp_path, "live")
    from test_execution import orders_for
    exchange.orders = orders_for("gap", "FILLED", "0.5", "NEW")
    result = await svc.execute("experiment", "gap", "otoco", plan())
    assert result["state"] == "OUTCOME_UNKNOWN"
    assert ledger.available("experiment", "USDT") == Decimal("49.95")
    assert ledger.balance("experiment", "BTC") == 0
    assert ledger.meta("pause")


@pytest.mark.asyncio
async def test_thin_volume_paper_entry_stays_partial_and_pauses_entries(tmp_path):
    ledger, exchange, svc = setup(tmp_path)
    result = await svc.execute("experiment", "partial-paper", "otoco", plan())
    created = result["created"]
    t = created + 60000
    exchange.candles = [[t, "100", "101", "99", "100", "1", t+1]]
    from unittest.mock import patch
    with patch("time.time", return_value=(t+60000)/1000):
        result = await svc.reconcile("partial-paper")
    assert result["result"]["paperStatus"] == "PARTIALLY_FILLED"
    assert not result["result"]["protected"]
    assert ledger.balance("experiment", "BTC") == Decimal("0.0999")
    assert ledger.meta("paperPause:experiment")
    with pytest.raises(BinanceClientError, match="paper strategy paused"):
        await svc.execute("experiment", "blocked-paper", "otoco", plan())
