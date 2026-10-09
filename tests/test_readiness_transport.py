"""Exercise real schemas/tools/call with an isolated fake exchange and store."""
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from binance_mcp import server as rpc
from binance_mcp.monitor import Monitor
from binance_mcp.client import BinanceClientError
from test_execution import setup, plan
from test_readiness_regressions import persistent_state


@pytest.fixture
def transport(tmp_path, monkeypatch):
    # TestClient serves ASGI on its own thread; serialized test calls share the
    # fixture connection. Production runs its ledger on the service event loop.
    import sqlite3
    original_connect = sqlite3.connect
    def test_connect(*a, **kw):
        kw['check_same_thread'] = False
        return original_connect(*a, **kw)
    monkeypatch.setattr(sqlite3, 'connect', test_connect)
    ledger, exchange, svc = setup(tmp_path)
    exchange.auth_status = lambda: {'account_access_ready': False, 'trading_enabled': False}
    monkeypatch.setattr(rpc, 'ledger', ledger)
    monkeypatch.setattr(rpc, 'execution', svc)
    monkeypatch.setattr(rpc, 'client', exchange)
    monkeypatch.setattr(rpc, 'monitor', Monitor(svc))
    app = rpc.build_mcp_asgi_app()
    @asynccontextmanager
    async def lifespan(_):
        async with app.router.lifespan_context(app): yield
    parent = FastAPI(lifespan=lifespan); parent.mount('/mcp',app)
    with TestClient(parent,base_url='http://localhost') as http:
        def call(name, args):
            response = http.post('/mcp/',headers={'accept':'application/json, text/event-stream'},json={
                'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}})
            assert response.status_code == 200
            return response.json()['result']
        yield ledger,exchange,svc,http,call


def test_schema_passive_reads_and_explicit_reconcile(transport):
    ledger,ex,svc,http,call=transport
    response=http.post('/mcp/',headers={'accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
    tools={t['name']:t for t in response.json()['result']['tools']}
    for name in ('binance_execution_status','binance_readiness','binance_fee_compatibility','binance_trade_preview'):
        assert tools[name]['annotations']['readOnlyHint'] is True
    assert tools['binance_execution_reconcile']['annotations']['readOnlyHint'] is False
    assert len(tools)==18
    before=persistent_state(ledger)
    result=call('binance_execution_status',{'intentId':'missing'})
    assert result['isError']
    assert result['structuredContent']['error']['blocker']=='INTENT_NOT_FOUND'
    assert persistent_state(ledger)==before
    result=call('binance_execution_reconcile',{'intentId':'missing'})
    assert result['isError']
    assert persistent_state(ledger)==before
    assert not ex.writes


def test_ready_empty_installation_and_paper_are_passive(transport):
    ledger,ex,svc,http,call=transport
    before=persistent_state(ledger)
    result=call('binance_readiness',{})
    assert not result.get('isError',False)
    value=result['structuredContent']
    assert value['paper']['configured']
    assert not value['paper']['functionallyChecked']
    assert not value['protectedLive']['ready']
    assert value['identity']['sourceSha256'] and value['identity']['schemaSha256']
    assert persistent_state(ledger)==before
    ledger.db.execute('DELETE FROM balances');ledger.db.execute('DELETE FROM strategies')
    before=persistent_state(ledger)
    result=call('binance_readiness',{})['structuredContent']
    assert not result['provisioned']
    assert 'STRATEGY_UNPROVISIONED' in result['protectedLive']['blockers']
    assert persistent_state(ledger)==before


def test_preview_success_failure_and_unprovisioned_are_passive(transport):
    ledger,ex,svc,http,call=transport
    before=persistent_state(ledger)
    result=call('binance_trade_preview',{'strategyId':'experiment','operation':'otoco','params':plan()})
    assert not result.get('isError',False)
    assert persistent_state(ledger)==before
    for args,code in [({'strategyId':'missing','operation':'otoco','params':plan()},'STRATEGY_UNPROVISIONED'),
                      ({'strategyId':'experiment','operation':'otoco','params':plan(quantity='NaN')},'VALIDATION_FAILED')]:
        result=call('binance_trade_preview',args)
        assert result['isError']
        assert result['structuredContent']['error']['blocker']==code
        assert persistent_state(ledger)==before


def test_status_does_not_reconcile_existing_paper_intent(transport):
    ledger,ex,svc,http,call=transport
    result=call('binance_strategy_execution',{'strategyId':'experiment','intentId':'paper','operation':'otoco','params':plan()})
    assert result['structuredContent']['state']=='OPEN'
    def forbidden(*a,**k): raise AssertionError('status must not touch exchange')
    ex.public_get=forbidden;ex.signed_get=forbidden
    before=persistent_state(ledger)
    result=call('binance_execution_status',{'intentId':'paper'})
    assert result['structuredContent']['state']=='OPEN'
    assert persistent_state(ledger)==before


def test_safe_rate_limit_and_uncertain_error_metadata(transport):
    ledger,ex,svc,http,call=transport
    async def limited(*a,**k):
        raise BinanceClientError('private account detail and signed URL',status=429,code=-1003,retry_after=10,outcome_unknown=True,headers={'Authorization':'secret'})
    ex.public_get=limited
    before=persistent_state(ledger)
    result=call('binance_public_request',{'product':'spot','path':'/api/v3/time'})
    assert result['isError']
    error=result['structuredContent']['error']
    assert error['retryAfter']==10 and error['outcomeUnknown']
    assert not error['automaticWriteRetryAllowed']
    assert 'private account' not in json.dumps(result) and 'secret' not in json.dumps(result)
    assert persistent_state(ledger)==before


def test_fee_diagnostic_and_stale_readiness_do_not_mutate(transport):
    ledger,ex,svc,http,call=transport
    result=call('binance_fee_compatibility',{'symbol':'BTCUSDT'})
    assert result['structuredContent']['compatible']
    before=persistent_state(ledger)
    result=call('binance_readiness',{'checkExchange':True})['structuredContent']
    assert result['research']['observations']['marketError']['blocker']=='VALIDATION_FAILED'
    assert result['research']['observations']['portfolio']['accountWideComplete'] is False
    assert persistent_state(ledger)==before
    assert not ex.writes


def test_live_blocked_without_provisioned_operational_channel(transport):
    ledger,ex,svc,http,call=transport
    ledger.db.execute("UPDATE strategies SET mode='live'")
    before=persistent_state(ledger)
    result=call('binance_strategy_execution',{'strategyId':'experiment','intentId':'live','operation':'otoco','params':plan()})
    assert result['isError']
    assert result['structuredContent']['error']['blocker']=='INCIDENT_CHANNEL_UNCONFIGURED'
    assert persistent_state(ledger)==before
    assert not ex.writes
