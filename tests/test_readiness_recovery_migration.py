import copy
import time
from decimal import Decimal
import pytest
from binance_mcp import incidents, migration
from binance_mcp.client import BinanceClientError
from binance_mcp.ledger import Ledger
from binance_mcp.execution import ExecutionService, client_id
from binance_mcp.diagnostics import source_identity
from test_execution import setup, plan, orders_for, fill
from test_readiness_regressions import persistent_state


async def exposed(tmp_path):
    ledger,ex,svc=setup(tmp_path,'live')
    ledger.set_meta('recoveryPolicy:experiment',{'version':1,'approved':True,'action':'cancel_attach','maxDelayMs':30000})
    ex.orders=orders_for('partial','PARTIALLY_FILLED','.2')
    ex.fills={1:[fill(qty='.2',commission='.0002')]}
    await svc.execute('experiment','partial','otoco',plan())
    return ledger,ex,svc


def install_recovery_exchange(ex, *, ambiguous=False, fill_race=False):
    async def order(product,path,action,params):
        ex.writes.append((path,action,params))
        if action=='cancel':
            if ambiguous: raise BinanceClientError('ambiguous cancel',outcome_unknown=True)
            if fill_race:
                ex.orders[1]['executedQty']='.3'
                ex.fills[1].append(fill(key=2,qty='.1',commission='.0001'))
            for o in ex.orders.values(): o['status']='CANCELED'
        else:
            key=params['listClientOrderId']
            ex.orders={2:{'symbol':'BTCUSDT','orderId':2,'clientOrderId':params['aboveClientOrderId'],'orderListId':1,
                          'side':'SELL','type':'LIMIT_MAKER','status':'NEW','price':params['abovePrice'],
                          'origQty':params['quantity'],'executedQty':'0'},
                       3:{'symbol':'BTCUSDT','orderId':3,'clientOrderId':params['belowClientOrderId'],'orderListId':1,
                          'side':'SELL','type':'STOP_LOSS','status':'NEW','stopPrice':params['belowStopPrice'],
                          'origQty':params['quantity'],'executedQty':'0'}}
        return {'orderListId':1}
    ex.order=order


@pytest.mark.asyncio
@pytest.mark.parametrize('fill_race',[False,True])
async def test_approved_recovery_cancel_reconcile_attach_exact_owned_quantity(tmp_path,fill_race):
    ledger,ex,svc=await exposed(tmp_path)
    install_recovery_exchange(ex,fill_race=fill_race)
    await incidents.recover(svc)
    incident=incidents.rows(ledger)[0]
    assert incident['state']=='RESOLVED'
    quantity=ex.writes[-1][2]['quantity']
    assert Decimal(quantity)==Decimal('.299' if fill_race else '.199')
    assert len(ex.writes)==3
    await incidents.recover(svc)
    assert len(ex.writes)==3
    assert ledger.get(incident['detail']['exitIntentId'])['result']['protected']


@pytest.mark.asyncio
async def test_ambiguous_cancel_restart_never_resubmits_or_attaches(tmp_path):
    ledger,ex,svc=await exposed(tmp_path)
    install_recovery_exchange(ex,ambiguous=True)
    await incidents.recover(svc)
    assert len(ex.writes)==2
    assert incidents.rows(ledger)[0]['state']=='WAITING_CANCEL'
    reopened=Ledger(str(tmp_path/'ledger.sqlite'))
    svc=ExecutionService(ex,reopened,['BTCUSDT'])
    await incidents.recover(svc)
    assert len(ex.writes)==2
    assert not any(i['payload']['operation']=='oco' for i in ledger.outstanding())
    assert reopened.meta('pause')


@pytest.mark.asyncio
async def test_crossed_stop_recovery_escalates_without_liquidation(tmp_path):
    ledger,ex,svc=await exposed(tmp_path)
    install_recovery_exchange(ex)
    ex.book.update(bidPrice='90',askPrice='90.01')
    await incidents.recover(svc)
    assert len(ex.writes)==1  # failed preflight preserves the original list
    assert incidents.rows(ledger)[0]['state']=='BLOCKED'
    assert ledger.balance('experiment','BTC')==Decimal('.1998')


async def legacy_fixture(tmp_path):
    ledger,ex,svc=setup(tmp_path,'live')
    order={'symbol':'BTCUSDT','orderId':17,'clientOrderId':'historic-client-id','orderListId':-1,
           'side':'SELL','type':'LIMIT','timeInForce':'GTC','price':'110','origQty':'.5','executedQty':'.2','status':'PARTIALLY_FILLED'}
    trade={**fill(key=11,qty='.2',side=False,price='110',commission='.022',fee_asset='USDT'),'orderId':17}
    ex.fills={17:[trade]}
    ex.orders={17:order}
    original=ex.signed_get
    async def signed(product,path,params=None):
        if path=='/api/v3/openOrders': return [order]
        if path=='/api/v3/order': return ex.orders[17]
        return await original(product,path,params)
    ex.signed_get=signed
    item={**order,'strategyId':'experiment','baseAsset':'BTC','quoteAsset':'USDT',
          'costBasisEvidence':['synthetic prior acquisition'],'historicalFillEvidence':['synthetic fill history']}
    audit={'id':'legacy-import','evidence':['synthetic owner-reviewed history'],'sourceRevision':source_identity(),
           'ledgerRevision':migration.revision(ledger),'accountDeltas':{},
           'adjustments':[{'strategyId':'experiment','category':'transfer','asset':'BTC','location':'SPOT','quantityDelta':'.3','costDelta':'30'}],
           'legacyOrders':[item]}
    snapshot=await migration.observe(svc,audit)
    return ledger,ex,svc,audit,snapshot


@pytest.mark.asyncio
async def test_migration_plan_passive_apply_backed_up_idempotent_follow_fills(tmp_path):
    ledger,ex,svc,audit,snapshot=await legacy_fixture(tmp_path)
    before=persistent_state(ledger)
    result=migration.plan(ledger,snapshot,audit)
    assert result['canApply'],result
    assert persistent_state(ledger)==before
    backup=tmp_path/'backup.sqlite'
    assert not migration.apply(ledger,snapshot,audit,str(backup))['alreadyApplied']
    assert backup.exists()
    assert migration.apply(ledger,snapshot,audit,str(tmp_path/'backup2.sqlite'))['alreadyApplied']
    assert ledger.balance('experiment','BTC')==Decimal('.3')
    assert ledger.available('experiment','BTC')==0
    intent=ledger.get('legacy:BTCUSDT:17')
    assert intent['result']['legacyClientOrderId']=='historic-client-id'
    ledger.db.close()
    reopened=Ledger(str(tmp_path/'ledger.sqlite'))
    svc=ExecutionService(ex,reopened,['BTCUSDT'])
    # Baseline partial fill is already accounted by owner evidence. Only new fills
    # change ownership/P&L, using the historical client ID after restart.
    ex.orders[17]['executedQty']='.3'
    ex.fills[17].append({**fill(key=12,qty='.1',side=False,price='110',commission='.011',fee_asset='USDT'),'orderId':17})
    await svc.reconcile('legacy:BTCUSDT:17')
    assert reopened.balance('experiment','BTC')==Decimal('.2')
    assert reopened.balance('experiment','USDT')==Decimal('110.989')
    await svc.reconcile('legacy:BTCUSDT:17')
    assert reopened.balance('experiment','BTC')==Decimal('.2')
    assert not ex.writes


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['revision','location','identity','history','unknown','cost'])
async def test_migration_contradictions_roll_back(tmp_path,failure):
    ledger,ex,svc,audit,snapshot=await legacy_fixture(tmp_path)
    if failure=='revision': audit['ledgerRevision']='wrong'
    elif failure=='location': audit['adjustments'][0]['location']='EARN:BTC001'
    elif failure=='identity': audit['legacyOrders'][0]['clientOrderId']='fabricated'
    elif failure=='history': snapshot['legacyEvidence']['BTCUSDT:17']['trades']=[]
    elif failure=='unknown': snapshot['coverage']['orders']['data'].append({**snapshot['coverage']['orders']['data'][0],'orderId':18,'clientOrderId':'unknown'})
    else: audit['adjustments'][0]['costDelta']='0'
    before=persistent_state(ledger)
    assert not migration.plan(ledger,snapshot,audit)['canApply']
    assert persistent_state(ledger)==before
    with pytest.raises(BinanceClientError): migration.apply(ledger,snapshot,audit,str(tmp_path/'backup.sqlite'))
    assert persistent_state(ledger)==before


@pytest.mark.asyncio
async def test_quote_fee_entry_recovery_covers_all_actual_received_units(tmp_path):
    ledger,ex,svc=setup(tmp_path,'live')
    ledger.set_meta('recoveryPolicy:experiment',{'version':1,'approved':True,'action':'cancel_attach','maxDelayMs':30000})
    ex.orders=orders_for('partial','FILLED','.5','NEW')
    ex.fills={1:[fill(commission='.05',fee_asset='USDT')]}
    result=await svc.execute('experiment','partial','otoco',plan())
    assert not result['result']['protected']
    assert Decimal(result['reserved'])==Decimal('.5')
    install_recovery_exchange(ex)
    await incidents.recover(svc)
    assert Decimal(ex.writes[-1][2]['quantity'])==Decimal('.5')
    assert incidents.rows(ledger)[0]['state']=='RESOLVED'


@pytest.mark.asyncio
async def test_expired_recovery_starts_no_new_exchange_mutation(tmp_path):
    ledger,ex,svc=await exposed(tmp_path)
    install_recovery_exchange(ex)
    incident=incidents.rows(ledger)[0]
    incidents.update(ledger,incident,'DETECTED',deadline=1)
    await incidents.recover(svc)
    assert len(ex.writes)==1
    assert incidents.rows(ledger)[0]['state']=='ESCALATED'


@pytest.mark.asyncio
async def test_missing_recovery_read_evidence_never_cancels(tmp_path):
    ledger,ex,svc=await exposed(tmp_path)
    install_recovery_exchange(ex)
    async def unavailable(*a,**k): raise BinanceClientError('REST failure')
    ex.signed_get=unavailable
    await incidents.recover(svc)
    assert len(ex.writes)==1
    assert incidents.rows(ledger)[0]['state']=='WAITING_EVIDENCE'


@pytest.mark.asyncio
async def test_readonly_migration_connection_has_zero_database_writes(tmp_path):
    ledger,ex,svc,audit,snapshot=await legacy_fixture(tmp_path)
    readonly=Ledger.open_readonly(str(tmp_path/'ledger.sqlite'))
    before=persistent_state(ledger)
    assert migration.plan(readonly,snapshot,audit)['canApply']
    assert readonly.db.total_changes==0
    assert persistent_state(ledger)==before
    with pytest.raises(Exception): readonly.pause('forbidden')
    readonly.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['crossed', 'notional', 'permissions', 'fees', 'free', 'orders'])
async def test_failed_replacement_preflight_preserves_working_stop(tmp_path, failure):
    ledger, ex, svc = setup(tmp_path, 'live')
    ledger.set_meta('recoveryPolicy:experiment', {'version': 1, 'approved': True, 'action': 'cancel_attach', 'maxDelayMs': 30000})
    ex.orders = orders_for('damaged', 'FILLED', '.5', 'NEW')
    ex.orders[2]['price'] = '111'  # invalid take; stop is still valid and active
    ex.fills = {1: [fill()]}
    await svc.execute('experiment', 'damaged', 'otoco', plan())
    install_recovery_exchange(ex)
    if failure == 'crossed':
        ex.book.update(bidPrice='90', askPrice='90.01')
    elif failure == 'notional':
        ex.info['filters'][2]['minNotional'] = '50'
    elif failure == 'permissions':
        ex.account['canTrade'] = False
    elif failure == 'free':
        ex.account['balances'][1].update(free='0', locked='10')
    elif failure == 'orders':
        ex.info['filters'].append({'filterType': 'MAX_NUM_ORDERS', 'maxNumOrders': 1})
    else:
        original = ex.signed_get
        async def signed(product, path, params=None):
            result = await original(product, path, params)
            if path == '/api/v3/account/commission':
                result['discount'].update(enabledForAccount=True, enabledForSymbol=True)
            return result
        ex.signed_get = signed
    before = copy.deepcopy(ex.orders[3])
    await incidents.recover(svc)
    assert len(ex.writes) == 1
    assert ex.orders[3] == before and ex.orders[3]['status'] == 'NEW'
    assert incidents.rows(ledger)[0]['state'] == 'BLOCKED'
    assert ledger.balance('experiment', 'BTC') == Decimal('.4995')
    assert ledger.get('damaged')['state'] == 'OPEN'
    assert ledger.get(incidents.rows(ledger)[0]['detail']['cancelIntentId']) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('control', ['pause', 'riskPause:experiment', 'streamConnected', 'incident'])
async def test_migration_rejects_safety_change_since_plan(tmp_path, control):
    ledger, ex, svc, audit, snapshot = await legacy_fixture(tmp_path)
    assert migration.plan(ledger, snapshot, audit)['canApply']
    previous = migration.revision(ledger)
    if control == 'incident':
        ledger.db.execute("INSERT INTO incidents VALUES('new','source','experiment','DETECTED',1,'{}')")
    else:
        ledger.set_meta(control, 'OWNER_EMERGENCY_HALT')
    assert migration.revision(ledger) != previous
    before = persistent_state(ledger)
    assert not migration.plan(ledger, snapshot, audit)['canApply']
    with pytest.raises(BinanceClientError, match='revision changed'):
        migration.apply(ledger, snapshot, audit, str(tmp_path / 'stale-backup.sqlite'))
    assert persistent_state(ledger) == before


@pytest.mark.asyncio
async def test_fresh_migration_audit_preserves_owner_emergency_halt(tmp_path):
    ledger, ex, svc, audit, snapshot = await legacy_fixture(tmp_path)
    ledger.pause('OWNER_EMERGENCY_HALT')
    audit['ledgerRevision'] = migration.revision(ledger)
    assert migration.plan(ledger, snapshot, audit)['canApply']
    migration.apply(ledger, snapshot, audit, str(tmp_path / 'halt-backup.sqlite'))
    assert ledger.meta('pause') == 'OWNER_EMERGENCY_HALT'


def test_direct_accounting_audit_preserves_owner_pause(tmp_path):
    from binance_mcp.accounting import apply_audit
    ledger, _, _ = setup(tmp_path, 'live')
    ledger.set_meta('expectedTotals', {'USDT': '100'})
    ledger.pause('OWNER_EMERGENCY_HALT')
    apply_audit(ledger, {'id': 'halt-audit', 'evidence': ['owner']}, {'USDT': '100'})
    assert ledger.meta('pause') == 'OWNER_EMERGENCY_HALT'
