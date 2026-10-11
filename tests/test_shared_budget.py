from decimal import Decimal
import pytest
from binance_mcp.client import BinanceClientError
from binance_mcp.execution import ExecutionService
from binance_mcp.ledger import Ledger
from test_execution import Exchange, plan, setup


def config(**overrides):
    return {"main": {"mode": "paper", "quote": "USDT", "fundingSource": "shared_account",
                     "budgetCap": "100", "reinvest": False, **overrides}}


def test_shared_budget_persists_spending_and_reservations(tmp_path):
    path = str(tmp_path / "ledger.sqlite")
    first = Ledger(path, config())
    first.change("main", "USDT", "SPOT", Decimal("-20"))
    first.begin("pending", "main", {"operation": "test"}, "USDT", "SPOT", "70")
    second = Ledger(path, config())
    assert second.available("main", "USDT") == Decimal("10")
    assert second.strategy("main")["fundingSource"] == "shared_account"
    with pytest.raises(BinanceClientError, match="insufficient"):
        second.begin("other", "main", {"operation": "test"}, "USDT", "SPOT", "11")
    assert not second.get("other")
    assert second.get("pending")["reserved"] == "70"


@pytest.mark.parametrize("overrides", [
    {"budgetCap": "101"}, {"location": "EARN:USDT001"}, {"reinvest": True},
    {"fundingSource": "allocated", "allocation": "100", "budgetCap": None},
])
def test_existing_shared_budget_cannot_be_reconfigured(tmp_path, overrides):
    path = str(tmp_path / "ledger.sqlite")
    Ledger(path, config())
    with pytest.raises(BinanceClientError):
        Ledger(path, config(**overrides))


@pytest.mark.parametrize("overrides", [
    {"budgetCap": "0"}, {"budgetCap": "-1"}, {"budgetCap": "NaN"},
    {"allocation": "100"}, {"fundingSource": "typo"}, {"reinvest": True},
])
def test_invalid_shared_budget_rejected(overrides):
    with pytest.raises(BinanceClientError):
        Ledger(":memory:", config(**overrides))


def test_shared_budget_needs_no_allocation_or_transfer():
    ledger = Ledger(":memory:", config(location="EARN:USDT001"))
    assert ledger.balance("main", "USDT", "EARN:USDT001") == Decimal("100")
    assert ledger.balance("main", "USDT", "SPOT") == 0
    assert ledger.report("main")["strategy"]["reinvest"] == 0
    assert ledger.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0


def test_existing_allocated_mode_stays_compatible(tmp_path):
    path = str(tmp_path / "ledger.sqlite")
    old = {"main": {"allocation": "100", "mode": "paper"}}
    ledger = Ledger(path, old)
    fingerprint = ledger.meta("allocationPolicy:main")
    ledger.change("main", "USDT", "SPOT", Decimal("-35"))
    assert Ledger(path, old).available("main", "USDT") == Decimal("65")
    assert ledger.meta("allocationPolicy:main") == fingerprint
    with pytest.raises(BinanceClientError):
        Ledger(path, config(reinvest=False))


@pytest.mark.asyncio
async def test_default_budget_preview_and_execution_keep_intent_identity(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.sqlite"), config())
    exchange = Exchange()
    svc = ExecutionService(exchange, ledger, ["BTCUSDT"], default_strategy_id="main")
    await svc.execute(None, "shared-entry", "otoco", plan())
    assert ledger.get("shared-entry")["strategy"] == "main"
    assert ledger.available("main", "USDT") < Decimal("100")
    assert not exchange.writes
    reserved = ledger.get("shared-entry")["reserved"]
    with pytest.raises(BinanceClientError, match="candle observations"):
        await svc.execute("main", "shared-entry", "otoco", plan())
    assert ledger.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1
    assert ledger.get("shared-entry")["reserved"] == reserved


def test_default_budget_never_falls_back_for_explicit_unknown_ids():
    ledger = Ledger(":memory:", config())
    svc = ExecutionService(Exchange(), ledger, ["BTCUSDT"], default_strategy_id="main")
    assert svc.resolve_strategy(None) == "main"
    for wrong in ("", " ", "other"):
        with pytest.raises(BinanceClientError):
            svc.resolve_strategy(wrong)
    unconfigured = ExecutionService(Exchange(), ledger, ["BTCUSDT"])
    with pytest.raises(BinanceClientError) as error:
        unconfigured.resolve_strategy(None)
    assert error.value.blocker == "DEFAULT_BUDGET_UNCONFIGURED"
    with pytest.raises(BinanceClientError):
        ExecutionService(Exchange(), ledger, ["BTCUSDT"], default_strategy_id="missing")


@pytest.mark.asyncio
async def test_omitting_strategy_preserves_live_risk_gate(tmp_path):
    ledger, exchange, fixture = setup(tmp_path, "live")
    svc = ExecutionService(exchange, ledger, ["BTCUSDT"], default_strategy_id="experiment",
                           incident_receiver_required=False)
    ledger.set_meta("riskPolicy:experiment", None)
    with pytest.raises(BinanceClientError) as error:
        await svc.execute(None, "blocked-risk", "otoco", plan())
    assert error.value.blocker == "POLICY_REQUIRED"
    assert not exchange.writes
    assert not ledger.outstanding()


@pytest.mark.asyncio
async def test_mcp_budget_ids_optional_but_intent_ids_required():
    from binance_mcp.server import server
    tools = {t.name: t.input_schema for t in await server.list_tools()}
    names = ["binance_order_request", "binance_simple_earn_subscribe",
             "binance_simple_earn_redeem", "binance_dual_investment_subscribe",
             "binance_dual_investment_from_flexible_earn", "binance_strategy_execution"]
    for name in names:
        schema = tools[name]
        assert "strategyId" not in schema.get("required", [])
        assert "intentId" in schema["required"]
    for name in ("binance_trade_preview", "binance_strategy_status"):
        assert "strategyId" not in tools[name].get("required", [])
