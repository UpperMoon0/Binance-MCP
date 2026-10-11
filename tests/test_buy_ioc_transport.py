"""Actual ASGI MCP discovery and tools/call against credential-free fake exchange."""
from binance_mcp import server as rpc
from binance_mcp.monitor import Monitor
from test_readiness_transport import transport
from test_readiness_regressions import persistent_state
from test_buy_ioc import setup, plan


def connect(transport, tmp_path, monkeypatch):
    _, _, _, http, call = transport
    ledger, ex, svc = setup(tmp_path/'ioc')
    ex.auth_status = lambda: {'account_access_ready': True, 'trading_enabled': True}
    monkeypatch.setattr(rpc, 'ledger', ledger)
    monkeypatch.setattr(rpc, 'execution', svc)
    monkeypatch.setattr(rpc, 'client', ex)
    monkeypatch.setattr(rpc, 'monitor', Monitor(svc))
    return ledger, ex, svc, http, call


def test_discovery_previews_execution_and_earn(transport, tmp_path, monkeypatch):
    ledger, ex, svc, http, call = connect(transport, tmp_path, monkeypatch)
    tools = http.post('/mcp/', headers={'accept':'application/json, text/event-stream'}, json={
        'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}).json()['result']['tools']
    tools = {t['name']: t for t in tools}
    for name in ['binance_trade_preview', 'binance_strategy_execution']:
        assert 'buy_ioc' in tools[name]['inputSchema']['properties']['operation']['enum']
    assert 'intentId' in tools['binance_strategy_execution']['inputSchema']['required']
    assert tools['binance_earn_preview']['annotations']['readOnlyHint']
    before = persistent_state(ledger)
    preview = call('binance_trade_preview', {'operation':'buy_ioc', 'params':plan()})
    assert not preview.get('isError') and preview['structuredContent']['protection'] == 'UNPROTECTED_BUY_AND_HOLD'
    assert persistent_state(ledger) == before and not ex.writes
    blocked = call('binance_trade_preview', {'operation':'buy_ioc', 'params':plan(maxQuoteSpend='1')})
    assert blocked['isError'] and blocked['structuredContent']['error']['blocker'] == 'QUOTE_SPEND_CAP'
    assert persistent_state(ledger) == before
    result = call('binance_strategy_execution', {'intentId':'wire-buy','operation':'buy_ioc','params':plan()})
    assert not result.get('isError') and result['structuredContent']['state'] == 'RESOLVED'
    before = persistent_state(ledger)
    preview = call('binance_earn_preview', {'operation':'earn_subscribe','params':{'asset':'ETH','productId':'ETH001','amount':'.4995'}})
    assert not preview.get('isError') and preview['structuredContent']['acquiredAsset']
    assert persistent_state(ledger) == before and not ex.earn_writes
    result = call('binance_simple_earn_subscribe', {'intentId':'wire-earn','asset':'ETH','productId':'ETH001','amount':'.4995'})
    assert not result.get('isError') and result['structuredContent']['state'] == 'RESOLVED'
    assert len(ex.writes) == len(ex.earn_writes) == 1


def test_readiness_truthful_scoped_blockers_are_passive(transport, tmp_path, monkeypatch):
    ledger, ex, svc, http, call = connect(transport, tmp_path, monkeypatch)
    ledger.set_meta('pause', 'owner halt remains')
    before = persistent_state(ledger)
    result = call('binance_readiness', {})
    data = result['structuredContent']['strategies'][0]
    assert data['buyOnlyIoc']['implemented'] and not data['buyOnlyIoc']['ready']
    assert not data['buyOnlyIoc']['protected']
    assert 'EXECUTION_PAUSED' in data['buyOnlyIoc']['blockers']
    assert data['acquiredAssetEarn']['orchestration'] is False
    assert 'EXACT_OWNED_ASSET_PRODUCT_PREFLIGHT_REQUIRED' in data['acquiredAssetEarn']['blockers']
    assert persistent_state(ledger) == before and not ex.writes
    ledger.set_meta('buyHoldPolicy:main', None)
    before = persistent_state(ledger)
    result = call('binance_trade_preview', {'operation':'buy_ioc','params':plan()})
    assert result['isError'] and result['structuredContent']['error']['blocker'] == 'BUY_HOLD_POLICY_REQUIRED'
    assert persistent_state(ledger) == before


def test_readiness_distinguishes_allowed_buy_from_forbidden_earn(transport, tmp_path, monkeypatch):
    ledger, ex, svc, http, call = connect(transport, tmp_path, monkeypatch)
    risk = ledger.meta('riskPolicy:main')
    risk['operations'] = ['buy_ioc']
    ledger.set_meta('riskPolicy:main', risk)
    data = call('binance_readiness', {})['structuredContent']['strategies'][0]
    assert 'POLICY_OPERATION' not in data['buyOnlyIoc']['blockers']
    assert 'POLICY_OPERATION' in data['acquiredAssetEarn']['operations']['earn_subscribe']['blockers']
    assert 'POLICY_OPERATION' in data['acquiredAssetEarn']['operations']['earn_redeem']['blockers']
