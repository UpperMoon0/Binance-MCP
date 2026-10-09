import copy
import json
import time
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

import pytest
from binance_mcp.client import BinanceClientError
from binance_mcp.ledger import Ledger
from binance_mcp import fees, policy, incidents
from binance_mcp.execution import ExecutionService, spot_permissions
from binance_mcp.monitor import Monitor
from test_execution import setup, plan, orders_for, fill, Exchange
from test_investment import FakeClient, product, matching_position
from binance_mcp.investment import InvestmentService


def persistent_state(ledger):
    return {r[0]: [tuple(v) for v in ledger.db.execute('SELECT * FROM ' + r[0] + ' ORDER BY 1')]
            for r in ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['oco', 'otoco'])
@pytest.mark.parametrize('defect', ['undersized', 'oversized', 'type', 'price', 'list', 'symbol', 'missing', 'rejected'])
async def test_each_exit_contract_fails_closed(tmp_path, operation, defect):
    ledger, exchange, svc = setup(tmp_path, 'live')
    key = 'coverage'
    exchange.orders = orders_for(key, 'FILLED', '0.5', 'NEW')
    exchange.fills = {1: [fill()]}
    params = plan()
    if operation == 'oco':
        del exchange.orders[1]
        exchange.fills = {}
        ledger.change('experiment', 'BTC', 'SPOT', Decimal('0.499'), Decimal('50'))
        params = plan(quantity='0.499')
    leg = exchange.orders[3]
    if defect == 'undersized': leg['origQty'] = '0.001'
    elif defect == 'oversized': leg['origQty'] = '0.500'
    elif defect == 'type': leg['type'] = 'LIMIT'
    elif defect == 'price': leg['stopPrice'] = '90'
    elif defect == 'list': leg['orderListId'] = 2
    elif defect == 'symbol': leg['symbol'] = 'ETHUSDT'
    elif defect == 'missing': del exchange.orders[3]
    else: leg['status'] = 'REJECTED'
    result = await svc.execute('experiment', key, operation, params)
    assert not (result['result'] or {}).get('protected')
    assert ledger.meta('pause')
    assert Decimal(result['reserved']) > 0
    if operation == 'otoco':
        with pytest.raises(BinanceClientError):
            await svc.check_protection()
    assert len(exchange.writes) == 1


@pytest.mark.asyncio
async def test_leg_validation_restart_and_valid_oco(tmp_path):
    ledger, exchange, svc = setup(tmp_path, 'live')
    ledger.change('experiment', 'BTC', 'SPOT', Decimal('.499'), Decimal('50'))
    exchange.orders = orders_for('oco', 'FILLED', '.5', 'NEW')
    del exchange.orders[1]
    result = await svc.execute('experiment', 'oco', 'oco', plan(quantity='.499'))
    assert result['result']['protected']
    reopened = Ledger(str(tmp_path/'ledger.sqlite'))
    result = await ExecutionService(exchange, reopened, ['BTCUSDT']).reconcile('oco')
    assert result['result']['protected']
    exchange.orders[2]['price'] = '111'
    result = await svc.reconcile('oco')
    assert result['result']['protectionUncertain']
    assert Decimal(result['reserved']) == Decimal('.499')  # exclusive siblings reserved once


@pytest.mark.parametrize('permissions,sets,expected', [
    (['SPOT'], [['SPOT']], True), (['TRD_GRP_071'], [['SPOT','TRD_GRP_071']], True),
    (['SPOT'], [['SPOT','MARGIN'],['TRD_GRP_004','TRD_GRP_005']], False),
    (['SPOT','TRD_GRP_005'], [['SPOT','MARGIN'],['TRD_GRP_004','TRD_GRP_005']], True),
    ([], [['SPOT']], False), (['SPOT'], [], False), (['SPOT'], ['SPOT'], False),
    (['SPOT'], [[]], False), (['SPOT'], [['SPOT',None]], False)])
def test_permission_boolean_semantics(permissions, sets, expected):
    assert spot_permissions({'canTrade': True, 'permissions': permissions}, {'permissionSets': sets}) is expected


@pytest.mark.asyncio
async def test_trading_group_preview_and_execution_revalidation(tmp_path):
    ledger, exchange, svc = setup(tmp_path, 'live')
    exchange.account['permissions'] = ['TRD_GRP_071']
    exchange.info['permissionSets'] = [['SPOT', 'TRD_GRP_071']]
    before = persistent_state(ledger)
    assert (await svc.preview('experiment', 'otoco', plan()))['operation'] == 'otoco'
    assert persistent_state(ledger) == before
    exchange.account['permissions'] = ['TRD_GRP_999']
    with pytest.raises(BinanceClientError, match='permission'):
        await svc.execute('experiment', 'permission', 'otoco', plan())
    assert not exchange.writes
    assert not ledger.outstanding()


@pytest.mark.asyncio
async def test_failed_preview_is_passive_on_all_tables(tmp_path):
    ledger, exchange, svc = setup(tmp_path, 'live')
    ledger.change('experiment', 'BTC', 'SPOT', Decimal('.5'), Decimal('50'))
    exchange.account['balances'][1]['free'] = '.1'
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError, match='backed'):
        await svc.preview('experiment', 'order', {'symbol':'BTCUSDT','quantity':'.1','price':'100','side':'SELL'})
    assert persistent_state(ledger) == before
    assert not exchange.writes


@pytest.mark.asyncio
@pytest.mark.parametrize('identity,accepted', [('2650584', True),(2650584, True),('wrong',False),(None,True)])
async def test_di_exposed_product_identity(identity, accepted):
    row = matching_position()
    row['productId'] = identity
    client = FakeClient(positions=[row])
    client.subscribe_calls.append((1,))
    service = InvestmentService(client)
    if accepted:
        assert (await service._verify_subscription(product=product(),deposit_amount='700',auto_compound_plan='NONE'))['positionId'] == 'position-1'
    else:
        with pytest.raises(BinanceClientError, match='not verified'):
            await service._verify_subscription(product=product(),deposit_amount='700',auto_compound_plan='NONE')
    assert len(client.subscribe_calls) == 1


@pytest.mark.asyncio
async def test_atomic_position_count_across_connections(tmp_path):
    ledger, _, svc = setup(tmp_path)
    cfg = ledger.meta('riskPolicy:experiment'); cfg['maxPositions'] = 1
    ledger.set_meta('riskPolicy:experiment', cfg)
    preview = await svc.preview('experiment', 'otoco', plan(quantity='.3'))
    payload = {'operation':'otoco','params':plan(quantity='.3')}
    def reserve(key):
        db = Ledger(str(tmp_path/'ledger.sqlite'))
        try:
            return db.begin(key,'experiment',payload,'USDT','SPOT',preview['reservation'],prepared={'preview':preview})
        except BinanceClientError as exc:
            return exc.blocker
        finally:
            db.db.close()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve, ['first','second']))
    assert results.count('POSITION_COUNT') == 1
    assert len(ledger.outstanding()) == 1
    assert ledger.available('experiment','USDT') > 60


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value,code', [('maxPositionQuote','49','POSITION_VALUE'), ('maxPlannedDownsideQuote','1','PLANNED_DOWNSIDE')])
async def test_policy_caps_before_reservation(tmp_path,field,value,code):
    ledger, exchange, svc = setup(tmp_path)
    cfg = ledger.meta('riskPolicy:experiment'); cfg[field] = value
    ledger.set_meta('riskPolicy:experiment',cfg)
    with pytest.raises(BinanceClientError) as e:
        await svc.execute('experiment','risk','otoco',plan())
    assert e.value.blocker == code
    assert not ledger.outstanding() and not exchange.writes


@pytest.mark.asyncio
async def test_loss_pause_persists_but_preview_is_passive(tmp_path):
    ledger, exchange, svc = setup(tmp_path)
    cfg = ledger.meta('riskPolicy:experiment'); cfg['lossPauseQuote'] = '5'
    ledger.set_meta('riskPolicy:experiment',cfg)
    ledger.change('experiment','BTC','SPOT',Decimal('.5'),Decimal('60'))
    ledger.event('reward','experiment','earn_reward','USDT',Decimal(100),Decimal(100))
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError) as e:
        await svc.preview('experiment','otoco',plan())
    assert e.value.blocker == 'LOSS_PAUSED'
    assert persistent_state(ledger) == before
    with pytest.raises(BinanceClientError):
        await svc.execute('experiment','loss','otoco',plan())
    assert ledger.meta('riskPause:experiment')
    assert Ledger(str(tmp_path/'ledger.sqlite')).meta('riskPause:experiment')
    assert not exchange.writes


@pytest.mark.asyncio
async def test_stale_and_missing_risk_valuation_block(tmp_path):
    ledger,_,svc=setup(tmp_path)
    preview=await svc.preview('experiment','otoco',plan())
    preview['riskEvidence']['observedAt']=1
    with pytest.raises(BinanceClientError) as e: policy.entry_check(ledger,'experiment',preview)
    assert e.value.blocker=='VALUATION_STALE'
    ledger.change('experiment','BTC','SPOT',Decimal('.5'),Decimal('50'))
    preview['riskEvidence']['observedAt']=int(time.time()*1000)
    with pytest.raises(BinanceClientError) as e: policy.entry_check(ledger,'experiment',preview)
    assert e.value.blocker=='VALUATION_INCOMPLETE'


@pytest.mark.asyncio
async def test_unapproved_live_policy_blocks_and_recovery_stays_available(tmp_path):
    ledger = Ledger(str(tmp_path/'unapproved'), {'unapproved':{'mode':'live','allocation':'100'}})
    ex=Exchange();svc=ExecutionService(ex,ledger,['BTCUSDT'])
    with pytest.raises(BinanceClientError) as e: await svc.execute('unapproved','entry','otoco',plan())
    assert e.value.blocker=='POLICY_REQUIRED'
    ledger.change('unapproved','BTC','SPOT',Decimal('.5'),Decimal('50'))
    await svc.execute('unapproved','exit','order',{'symbol':'BTCUSDT','quantity':'.5','price':'100','side':'SELL'})
    assert len(ex.writes)==1


@pytest.mark.parametrize('account,symbol,compatible', [(True,True,False),(True,False,True),(False,True,True),(False,False,True)])
def test_fee_compatibility_discount_combinations(account,symbol,compatible):
    rates={k:{'maker':'0.001','taker':'0.002','buyer':'0','seller':'0'} for k in ('standardCommission','taxCommission','specialCommission')}
    rates['discount']={'enabledForAccount':account,'enabledForSymbol':symbol,'discountAsset':'BNB','discount':'.25'}
    result=fees.inspect(rates)
    assert result['compatible'] is compatible
    assert Decimal(result['feeRateBound'])==Decimal('.006')


@pytest.mark.asyncio
async def test_unknown_fee_asset_fill_atomic_rollback(tmp_path):
    ledger,_,svc=setup(tmp_path)
    preview=await svc.preview('experiment','otoco',plan())
    intent,_=ledger.begin('fee','experiment',{},'USDT','SPOT','50',prepared={'preview':preview})
    before=persistent_state(ledger)
    with pytest.raises(BinanceClientError,match='third-asset'): svc.apply_fill(intent,fill(fee_asset='BNB'),preview)
    assert persistent_state(ledger)==before


@pytest.mark.asyncio
async def test_durable_incident_dedup_delivery_failure_and_restart(tmp_path):
    ledger,exchange,svc=setup(tmp_path,'live')
    exchange.orders=orders_for('partial','PARTIALLY_FILLED','.2')
    exchange.fills={1:[fill(qty='.2',commission='.0002')]}
    await svc.execute('experiment','partial','otoco',plan())
    await svc.reconcile('partial')
    assert len(incidents.rows(ledger))==1
    first=incidents.rows(ledger)[0]
    failed=[]
    async def sink(event): failed.append(event); return False
    await incidents.deliver(ledger,sink)
    assert incidents.rows(ledger)[0]['detail']['delivery']=='PENDING'
    reopened=Ledger(str(tmp_path/'ledger.sqlite'))
    async def healthy(event): return True
    await incidents.deliver(reopened,healthy)
    await incidents.deliver(reopened,healthy)
    current=incidents.rows(reopened)[0]
    assert current['detail']['deliveryAttempts']==2
    assert current['detail']['firstDetectedAt']==first['detail']['firstDetectedAt']
    incidents.acknowledge(reopened,current['id'],['synthetic operator receipt'])
    assert incidents.rows(reopened)[0]['detail']['acknowledgedAt']
    assert ledger.meta('pause')


@pytest.mark.asyncio
async def test_failure_heartbeat_does_not_refresh_success_or_clear_owner_pause(tmp_path):
    ledger,exchange,svc=setup(tmp_path,'live')
    ledger.pause('owner audit required')
    ledger.set_meta('protectionAt',1);ledger.set_meta('reconciledAt',1)
    async def bad(*args,**kwargs): raise BinanceClientError('unavailable')
    exchange.signed_get=bad
    monitor=Monitor(svc)
    with pytest.raises(BinanceClientError): await monitor.tick()
    assert ledger.meta('heartbeatAt')>1
    assert ledger.meta('protectionAt')==1
    assert ledger.meta('reconciledAt')==1
    assert ledger.meta('pause')=='owner audit required'


@pytest.mark.asyncio
async def test_same_asset_orphan_partial_position_is_not_hidden_by_new_entry(tmp_path):
    ledger,_,svc=setup(tmp_path)
    cfg=ledger.meta('riskPolicy:experiment');cfg['maxPositions']=2
    ledger.set_meta('riskPolicy:experiment',cfg)
    # A cancelled partially filled entry still owns inventory. Another working
    # entry on the same asset must not hide that independent exposure bucket.
    ledger.change('experiment','BTC','SPOT',Decimal('.1'),Decimal('10'))
    await svc.execute('experiment','one','otoco',plan(quantity='.3'))
    with pytest.raises(BinanceClientError) as e:
        await svc.execute('experiment','two','otoco',plan(quantity='.3'))
    assert e.value.blocker=='POSITION_COUNT'
    assert len(ledger.outstanding())==1


def test_null_permission_sets_and_wrong_account_type_fail_closed():
    assert not spot_permissions({'canTrade':True,'permissions':['SPOT']},{'permissionSets':None})
    assert not spot_permissions({'canTrade':True,'permissions':['SPOT'],'accountType':'MARGIN'},{'permissionSets':[['SPOT']]})
    assert spot_permissions({'canTrade':True,'permissions':['SPOT']},{})


@pytest.mark.asyncio
@pytest.mark.parametrize('fee_mode', ['high', 'missing', 'discount', 'wrong_symbol'])
async def test_mixed_symbol_holdings_use_fresh_exit_fees(tmp_path, fee_mode):
    ledger, ex, svc = setup(tmp_path, 'live')
    cfg = ledger.meta('riskPolicy:experiment')
    cfg['lossPauseQuote'] = '.5'
    ledger.set_meta('riskPolicy:experiment', cfg)
    ledger.change('experiment', 'ETH', 'SPOT', Decimal('1'), Decimal('99.99'))
    original = ex.signed_get
    queried = []
    async def signed(product, path, params=None):
        result = await original(product, path, params)
        if path == '/api/v3/account/commission':
            queried.append(params['symbol'])
            result['symbol'] = params['symbol']
            if params['symbol'] == 'ETHUSDT':
                if fee_mode == 'high':
                    result['standardCommission'].update(maker='.01', taker='.01')
                elif fee_mode == 'missing':
                    del result['taxCommission']
                elif fee_mode == 'discount':
                    result['discount'].update(enabledForAccount=True, enabledForSymbol=True)
                else:
                    result['symbol'] = 'BTCUSDT'
        return result
    ex.signed_get = signed
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError) as exc:
        await svc.preview('experiment', 'otoco', plan())
    assert exc.value.blocker == {'high': 'LOSS_PAUSED', 'missing': 'FEE_EVIDENCE', 'discount': 'UNSUPPORTED_FEE_ASSET', 'wrong_symbol': 'FEE_EVIDENCE'}[fee_mode]
    assert queried == ['BTCUSDT', 'ETHUSDT']
    assert persistent_state(ledger) == before and not ex.writes
    if fee_mode == 'high':
        with pytest.raises(BinanceClientError):
            await svc.execute('experiment', 'mixed-loss', 'otoco', plan())
        assert not ex.writes


@pytest.mark.asyncio
async def test_missing_exit_fee_evidence_fails_closed_in_atomic_policy_check(tmp_path):
    ledger, _, svc = setup(tmp_path)
    ledger.change('experiment', 'BTC', 'SPOT', Decimal('.1'), Decimal('10'))
    preview = await svc.preview('experiment', 'otoco', plan())
    del preview['riskEvidence']['exitFeeRates']['BTC']
    with pytest.raises(BinanceClientError) as exc:
        policy.entry_check(ledger, 'experiment', preview)
    assert exc.value.blocker == 'FEE_EVIDENCE'


@pytest.mark.asyncio
async def test_mixed_symbol_fee_evidence_remains_distinct_and_is_used_atomically(tmp_path):
    ledger, ex, svc = setup(tmp_path, 'live')
    ledger.change('experiment', 'ETH', 'SPOT', Decimal('1'), Decimal('99.99'))
    original = ex.signed_get
    async def signed(product, path, params=None):
        result = await original(product, path, params)
        if path == '/api/v3/account/commission' and params['symbol'] == 'ETHUSDT':
            result['standardCommission'].update(maker='.002', taker='.002')
        return result
    ex.signed_get = signed
    preview = await svc.preview('experiment', 'otoco', plan())
    assert preview['feeRateBound'] == '0.001'
    assert preview['riskEvidence']['exitFeeRates'] == {'ETH': '0.002'}
    cfg = ledger.meta('riskPolicy:experiment')
    cfg['lossPauseQuote'] = '.25'  # ETH exit fees + allowance ~.30, BTC rate would give ~.20
    ledger.set_meta('riskPolicy:experiment', cfg)
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError) as exc:
        ledger.begin('fee-race', 'experiment', {'operation': 'otoco'}, 'USDT', 'SPOT', preview['reservation'], prepared={'preview': preview})
    assert exc.value.blocker == 'LOSS_PAUSED'
    assert persistent_state(ledger) == before
