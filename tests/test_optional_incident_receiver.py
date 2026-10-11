import time
import pytest
from binance_mcp import incidents
from binance_mcp.client import BinanceClientError
from binance_mcp.diagnostics import readiness
from binance_mcp.execution import ExecutionService
from binance_mcp.monitor import Monitor
from test_execution import setup, plan
from test_readiness_regressions import persistent_state


@pytest.mark.parametrize("value, expected", [(None, True), ("true", True), ("false", False), (" FALSE ", False)])
def test_receiver_requirement_is_explicit(monkeypatch, value, expected):
    monkeypatch.delenv("BINANCE_INCIDENT_RECEIVER_REQUIRED", raising=False)
    if value is not None:
        monkeypatch.setenv("BINANCE_INCIDENT_RECEIVER_REQUIRED", value)
    assert incidents.receiver_required() is expected


@pytest.mark.parametrize("value", ["", "0", "no", "disabled", "typo"])
def test_invalid_receiver_requirement_fails_startup(monkeypatch, value):
    monkeypatch.setenv("BINANCE_INCIDENT_RECEIVER_REQUIRED", value)
    with pytest.raises(ValueError):
        incidents.receiver_required()


@pytest.mark.asyncio
async def test_required_receiver_blocks_before_any_write(tmp_path):
    ledger, exchange, svc = setup(tmp_path, "live")
    svc.incident_sink_configured = False
    with pytest.raises(BinanceClientError) as error:
        await svc.execute("experiment", "missing-receiver", "otoco", plan())
    assert error.value.blocker == "INCIDENT_CHANNEL_UNCONFIGURED"
    assert not exchange.writes
    assert not ledger.outstanding()


@pytest.mark.asyncio
async def test_optional_receiver_allows_otherwise_valid_mock_execution(tmp_path):
    ledger, exchange, fixture = setup(tmp_path, "live")
    svc = ExecutionService(exchange, ledger, ["BTCUSDT", "ETHUSDT"], incident_receiver_required=False)
    await svc.execute("experiment", "optional-receiver", "otoco", plan())
    assert len(exchange.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["stream", "stale", "risk", "recovery", "budget", "permission"])
async def test_optional_receiver_preserves_other_execution_guards(tmp_path, blocker):
    ledger, exchange, fixture = setup(tmp_path, "live")
    svc = ExecutionService(exchange, ledger, ["BTCUSDT", "ETHUSDT"], incident_receiver_required=False)
    params = plan()
    if blocker == "stream":
        ledger.set_meta("streamConnected", False)
    elif blocker == "stale":
        ledger.set_meta("protectionAt", 1)
    elif blocker == "risk":
        ledger.set_meta("riskPolicy:experiment", None)
    elif blocker == "recovery":
        ledger.set_meta("recoveryPolicy:experiment", None)
    elif blocker == "budget":
        params = plan(quantity="2")
    elif blocker == "permission":
        exchange.account["canTrade"] = False
    with pytest.raises(BinanceClientError):
        await svc.execute("experiment", "blocked-" + blocker, "otoco", params)
    assert not exchange.writes


@pytest.mark.asyncio
async def test_local_incidents_remain_persisted_and_readiness_is_passive(tmp_path):
    ledger, exchange, fixture = setup(tmp_path, "live")
    svc = ExecutionService(exchange, ledger, ["BTCUSDT"], incident_receiver_required=False)
    monitor = Monitor(svc)
    ledger.set_meta("reconciledAt", int(time.time() * 1000))
    exchange.auth_status = lambda: {"account_access_ready": True, "trading_enabled": True}
    key = incidents.record(ledger, {"id": "local-hazard", "strategy": "experiment"}, "PROTECTION_LOST", "0.1", {})
    await incidents.deliver(ledger, None)
    before = persistent_state(ledger)
    result = await readiness(svc, monitor, [])
    assert result["incidentNotifications"] == {"required": False, "configured": False, "deliveryMode": "LOCAL_ONLY"}
    assert "INCIDENT_CHANNEL_UNCONFIGURED" not in result["protectedLive"]["blockers"]
    assert "OPERATOR_INCIDENT_UNRESOLVED" in result["protectedLive"]["blockers"]
    assert result["incidents"][0]["id"] == key
    assert result["incidents"][0]["detail"]["delivery"] == "PENDING"
    assert ledger.meta("pause")
    assert persistent_state(ledger) == before
    assert not exchange.writes
