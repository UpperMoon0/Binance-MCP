import asyncio
import copy
from binance_mcp.client import BinanceClientError
from test_readiness_regressions import persistent_state
import time
from decimal import Decimal
import pytest
from test_buy_ioc import setup, plan
from binance_mcp.execution import ExecutionService
from binance_mcp import buyhold
from binance_mcp.ledger import Ledger

@pytest.mark.asyncio
async def test_mixed_protected_and_held(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    bought = await svc.execute(None, 'buy', 'buy_ioc', plan())
    ledger.change('main','ETH','SPOT',Decimal('1'),Decimal('100'))
    ledger.begin('protected','main',{'operation':'oco'},'ETH','SPOT','1',recovery=True)
    ledger.update('protected','OPEN',result={'preview':bought['result']['preview'], 'protected':True,
                  'protectionUncertain':False,'verifiedAt':int(time.time()*1000)})
    # 1 ETH has fresh protected ownership and the other .4995 is a verified hold.
    await svc.check_protection()

@pytest.mark.asyncio
async def test_two_services_cannot_over_subscribe_acquired_subset(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    await svc.execute(None, 'buy', 'buy_ioc', plan())
    # Unrelated owned dust must not become acquired-asset Earn principal.
    ledger.change('main','ETH','SPOT',Decimal('.0005'),Decimal('.05'))
    connection = Ledger(str(tmp_path/'ledger.sqlite'))
    second = ExecutionService(ex,connection,['ETHUSDT'],incident_receiver_required=False,default_strategy_id='main')
    ready, resume = asyncio.Event(), asyncio.Event()
    original = second.investment_preflight
    async def delayed(*args):
        result = await original(*args)
        ready.set()
        await resume.wait()
        return result
    second.investment_preflight = delayed
    pending = asyncio.create_task(second.execute(None,'earn-two','earn_subscribe',{'asset':'ETH','productId':'ETH001','amount':'.1998'}))
    await ready.wait()
    one = await svc.execute(None,'earn-one','earn_subscribe',{'asset':'ETH','productId':'ETH001','amount':'.3'})
    assert one['state'] == 'RESOLVED',one
    resume.set()
    with pytest.raises(BinanceClientError) as error:
        await pending
    assert error.value.blocker == 'ACQUIRED_ASSET_OWNERSHIP'
    assert len(ex.earn_writes) == 1
    assert ledger.balance('main', 'ETH') == Decimal('.2')
    assert buyhold.quantity(ledger, 'main', 'ETH') == Decimal('.1995')

@pytest.mark.asyncio
async def test_owner_recovery_preserves_spent_allowance(tmp_path):
    from binance_mcp.accounting import apply_audit
    ledger, ex, svc = setup(tmp_path)
    # Accepted buy with a fee above the preflight bound requires owner accounting.
    original = ex.order
    async def changed_fee(*args):
        response = await original(*args)
        ex.fills[1][0]['commission'] = '.0006'
        ex.balance_change('ETH',-Decimal('.0001'))
        return response
    ex.order = changed_fee
    result = await svc.execute(None,'buy','buy_ioc',plan())
    assert result['state'] == 'OUTCOME_UNKNOWN'
    expected = ledger.meta('expectedTotals')
    observed = {**expected,'ETH':str(Decimal(expected['ETH'])+Decimal('.4994')),'USDT':str(Decimal(expected['USDT'])-50)}
    audit = {'id':'fix-fee','evidence':['synthetic exact terminal order and commission'],
       'accountDeltas':{'ETH':'.4994','USDT':'-50'},
       'adjustments':[{'strategyId':'main','asset':'USDT','location':'SPOT','category':'settlement','quantityDelta':'-50'},
                      {'strategyId':'main','asset':'ETH','location':'SPOT','category':'settlement','quantityDelta':'.4994','costDelta':'50'}],
       'resolveIntents':[{'intentId':'buy','state':'RESOLVED'}]}
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError) as error:
        apply_audit(ledger,audit,observed)
    assert error.value.blocker == 'IOC_AUDIT_RECOVERY_REQUIRED'
    assert persistent_state(ledger) == before
    audit.pop('adjustments')
    audit.pop('resolveIntents')
    audit['iocRecoveries'] = [{'intentId':'buy','approveActualFees':True,'observedAt':int(time.time()*1000),
                             'order':copy.deepcopy(ex.orders[1]),'trades':copy.deepcopy(ex.fills[1]),'openOrders':[]}]
    apply_audit(ledger,audit,observed)
    before = persistent_state(ledger)
    apply_audit(ledger,audit,observed)
    assert persistent_state(ledger) == before
    assert ledger.get('buy')['state'] == 'RESOLVED'
    assert ledger.balance('main','ETH') == buyhold.quantity(ledger,'main','ETH') == Decimal('.4994')
    assert ledger.balance('main','USDT') == 50
    reopened = Ledger(str(tmp_path/'ledger.sqlite'))
    assert Decimal(buyhold.budget(reopened,'main')['remainingQuote']) == Decimal('9.94')
    assert Decimal(buyhold.budget(ledger,'main')['spentQuoteIncludingFeeValue']) == Decimal('50.06')

@pytest.mark.asyncio
async def test_ioc_cannot_inherit_destructive_stp(tmp_path):
    ledger,ex,svc = setup(tmp_path)
    ex.info.update(defaultSelfTradePreventionMode='EXPIRE_MAKER',allowedSelfTradePreventionModes=['EXPIRE_MAKER','EXPIRE_TAKER'])
    original=ex.signed_get
    async def with_maker(product,path,params=None):
        if path == '/api/v3/openOrders':
            return [{'symbol':'ETHUSDT','side':'SELL','type':'LIMIT','price':'100','origQty':'1','executedQty':'0','clientOrderId':'legacy-sell','orderId':99}]
        return await original(product,path,params)
    ex.signed_get=with_maker
    preview=await svc.preview(None,'buy_ioc',plan())
    _,params=svc.order_params('buy',preview)
    assert params.get('selfTradePreventionMode') == 'EXPIRE_TAKER'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['EXPIRE_MAKER','EXPIRE_BOTH','DECREMENT','NONE'])
async def test_stp_execution_preserves_resting_owned_sell(tmp_path, mode):
    ledger, ex, svc = setup(tmp_path)
    ex.info.update(defaultSelfTradePreventionMode=mode,allowedSelfTradePreventionModes=[mode,'EXPIRE_TAKER'])
    maker = {'symbol':'ETHUSDT','side':'SELL','type':'LIMIT','status':'NEW','price':'100','origQty':'1',
             'executedQty':'0','clientOrderId':'legacy-sell','orderId':99}
    ledger.change('main','ETH','SPOT',Decimal('1'),Decimal('100'))
    ledger.begin('legacy','main',{'operation':'legacy_order','params':{'symbol':'ETHUSDT'}},'ETH','SPOT','1',recovery=True)
    ledger.update('legacy','OPEN',result={'legacyClientOrderId':'legacy-sell'})
    original_read, original_write = ex.signed_get, ex.order
    async def read(product,path,params=None):
        if path == '/api/v3/openOrders': return [maker]
        return await original_read(product,path,params)
    ex.signed_get = read
    # Isolate the exchange-side STP behavior from unrelated legacy protection handling.
    async def no_op(): return None
    svc.reconcile_all = svc.check_protection = no_op
    async def write(product,path,action,params):
        stp = params.get('selfTradePreventionMode',ex.info['defaultSelfTradePreventionMode'])
        if stp in ('EXPIRE_MAKER','EXPIRE_BOTH'): maker['status'] = 'EXPIRED_IN_MATCH'
        elif stp == 'DECREMENT': maker['origQty'] = '.5'
        elif stp == 'NONE': maker['executedQty'] = '.5'
        if stp == 'EXPIRE_TAKER': ex.executed,ex.status = '0','EXPIRED_IN_MATCH'
        return await original_write(product,path,action,params)
    ex.order = write
    before = copy.deepcopy(maker)
    result = await svc.execute(None,'stp-buy','buy_ioc',plan())
    assert result['state'] == 'RESOLVED',result
    assert result['result']['terminalStatus'] == 'EXPIRED_IN_MATCH'
    assert maker == before
    assert ledger.get('legacy')['reserved'] == '1'
    assert len(ex.writes) == 1 and ex.writes[0][2]['selfTradePreventionMode'] == 'EXPIRE_TAKER'


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed', [None,[],['NONE'],['EXPIRE_MAKER','EXPIRE_BOTH','DECREMENT']])
async def test_missing_maker_preserving_stp_fails_before_write(tmp_path,allowed):
    ledger,ex,svc = setup(tmp_path)
    ex.info['allowedSelfTradePreventionModes'] = allowed
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError) as error:
        await svc.preview(None,'buy_ioc',plan())
    assert error.value.blocker == 'STP_UNSUPPORTED'
    assert persistent_state(ledger) == before and not ex.writes


@pytest.mark.asyncio
async def test_reconciled_stp_must_match_persisted_safe_mode(tmp_path):
    ledger,ex,svc = setup(tmp_path)
    original = ex.order
    async def wrong(*args):
        result = await original(*args)
        result['selfTradePreventionMode'] = 'EXPIRE_MAKER'
        return result
    ex.order = wrong
    result = await svc.execute(None,'buy','buy_ioc',plan())
    assert result['state'] == 'OUTCOME_UNKNOWN'
    assert Decimal(result['reserved']) == Decimal('50.05')
    assert ledger.balance('main','ETH') == 0


@pytest.mark.asyncio
async def test_genuinely_overlapping_exit_never_masks_unprotected_inventory(tmp_path):
    ledger,ex,svc = setup(tmp_path)
    bought = await svc.execute(None,'buy','buy_ioc',plan())
    ledger.begin('hold-exit','main',{'operation':'oco'},'ETH','SPOT','.4',recovery=True)
    assert buyhold.reserved(ledger,'hold-exit') == Decimal('.4')
    ledger.update('hold-exit','OPEN',result={'preview':bought['result']['preview'],'protected':True,
                   'protectionUncertain':False,'verifiedAt':int(time.time()*1000)})
    await svc.check_protection()
    ledger.change('main','ETH','SPOT',Decimal('.1'),Decimal('10'))
    with pytest.raises(BinanceClientError,match='unprotected'):
        await svc.check_protection()


@pytest.mark.asyncio
async def test_disjoint_exit_partial_sale_preserves_hold_and_updates_overlap(tmp_path):
    ledger,ex,svc = setup(tmp_path)
    bought = await svc.execute(None,'buy','buy_ioc',plan())
    ledger.change('main','ETH','SPOT',Decimal('.2'),Decimal('20'))
    sale,_ = ledger.begin('mixed-exit','main',{'operation':'oco'},'ETH','SPOT','.5',recovery=True)
    assert buyhold.reserved(ledger,'mixed-exit') == Decimal('.3')
    preview = bought['result']['preview']
    svc.apply_fill(sale,{'id':100,'orderId':99,'qty':'.1','quoteQty':'10','price':'100',
        'commission':'0','commissionAsset':'USDT','isBuyer':False},preview)
    assert buyhold.quantity(ledger,'main','ETH') == Decimal('.4995')
    assert buyhold.reserved(ledger,'mixed-exit') == Decimal('.3')
    svc.apply_fill(ledger.get('mixed-exit'),{'id':101,'orderId':99,'qty':'.2','quoteQty':'20','price':'100',
        'commission':'0','commissionAsset':'USDT','isBuyer':False},preview)
    assert buyhold.quantity(ledger,'main','ETH') == Decimal('.3995')
    assert buyhold.reserved(ledger,'mixed-exit') == Decimal('.2')
    assert ledger.balance('main','ETH') == Decimal('.3995')


@pytest.mark.asyncio
async def test_acquired_transfer_cannot_use_exit_reserved_hold(tmp_path):
    ledger,ex,svc = setup(tmp_path)
    await svc.execute(None,'buy','buy_ioc',plan())
    ledger.begin('exit','main',{'operation':'oco'},'ETH','SPOT','.4',recovery=True)
    ledger.update('exit','OPEN',result={})
    assert buyhold.available(ledger,'main','ETH') == Decimal('.0995')
    with pytest.raises(BinanceClientError):
        await svc.investment_preflight('main','earn_subscribe',{'asset':'ETH','productId':'ETH001','amount':'.1'})
    assert not ex.earn_writes


@pytest.mark.asyncio
async def test_exact_transfer_reconciliation_cannot_silently_use_unrelated_units(tmp_path):
    ledger,ex,svc = setup(tmp_path)
    await svc.execute(None,'buy','buy_ioc',plan())
    original = ex.simple_earn_subscribe
    async def corrupt(*args):
        result = await original(*args)
        buyhold.change(ledger,'main','ETH','SPOT',Decimal('-.1'))
        return result
    ex.simple_earn_subscribe = corrupt
    result = await svc.execute(None,'earn','earn_subscribe',{'asset':'ETH','productId':'ETH001','amount':'.4995'})
    assert result['state'] == 'OUTCOME_UNKNOWN'
    assert ledger.balance('main','ETH') == Decimal('.4995')
    assert ledger.balance('main','ETH','EARN:ETH001') == 0
    assert not list(ledger.db.execute("SELECT * FROM events WHERE id='move:earn'"))


@pytest.mark.asyncio
@pytest.mark.parametrize('defect',['stale','wrong_id','wrong_side','live_remainder','missing_fills','wrong_totals','no_approval','mixed_adjustments','duplicate','changed_trade_price'])
async def test_bad_ioc_audit_rolls_back_every_table(tmp_path,defect):
    from binance_mcp.accounting import apply_audit
    ledger,ex,svc = setup(tmp_path)
    ex.timeout = True
    await svc.execute(None,'buy','buy_ioc',plan())
    expected = ledger.meta('expectedTotals')
    observed = {**expected,'ETH':str(Decimal(expected['ETH'])+Decimal('.4995')),'USDT':str(Decimal(expected['USDT'])-50)}
    evidence={'intentId':'buy','approveActualFees':True,'observedAt':int(time.time()*1000),
              'order':copy.deepcopy(ex.orders[1]),'trades':copy.deepcopy(ex.fills[1]),'openOrders':[]}
    audit={'id':'recovery','evidence':['fake-verified-history'],'accountDeltas':{'ETH':'.4995','USDT':'-50'},'iocRecoveries':[evidence]}
    if defect == 'stale': evidence['observedAt'] -= 100000
    elif defect == 'wrong_id': evidence['order']['clientOrderId'] = 'foreign'
    elif defect == 'wrong_side': evidence['trades'][0]['isBuyer'] = False
    elif defect == 'live_remainder': evidence['openOrders'] = [evidence['order']]
    elif defect == 'missing_fills': evidence['trades'] = []
    elif defect == 'wrong_totals':
        audit['accountDeltas']['ETH'] = '.6'
        observed['ETH'] = str(Decimal(expected['ETH'])+Decimal('.6'))
    elif defect == 'no_approval': evidence['approveActualFees'] = False
    elif defect == 'mixed_adjustments': audit['adjustments']=[{'strategyId':'main','asset':'ETH','quantityDelta':'.4995'}]
    elif defect == 'duplicate': audit['iocRecoveries'].append(copy.deepcopy(evidence))
    elif defect == 'changed_trade_price': evidence['trades'][0]['price'] = '101'
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError): apply_audit(ledger,audit,observed)
    assert persistent_state(ledger) == before
    assert Decimal(buyhold.budget(ledger,'main')['reservedQuote']) == Decimal('50.05')


@pytest.mark.asyncio
async def test_pending_ioc_cannot_be_indirectly_booked_through_generic_adjustment(tmp_path):
    from binance_mcp.accounting import apply_audit
    ledger,ex,svc = setup(tmp_path)
    ex.timeout = True
    await svc.execute(None,'buy','buy_ioc',plan())
    observed = ledger.meta('expectedTotals')
    audit={'id':'unsafe','evidence':['fake'],'adjustments':[{'strategyId':'main','asset':'ETH','location':'SPOT',
           'category':'settlement','quantityDelta':'.4995','costDelta':'50'}]}
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError) as error: apply_audit(ledger,audit,observed)
    assert error.value.blocker == 'IOC_AUDIT_RECOVERY_REQUIRED'
    assert persistent_state(ledger) == before


@pytest.mark.asyncio
async def test_restart_preserves_exact_earn_and_exit_subset_reservations(tmp_path):
    ledger,ex,svc = setup(tmp_path)
    await svc.execute(None,'buy','buy_ioc',plan())
    preview,asset,location,amount=await svc.investment_preflight('main','earn_subscribe',{'asset':'ETH','productId':'ETH001','amount':'.2'})
    ledger.begin('earn','main',{'operation':'earn_subscribe','params':{'asset':'ETH','productId':'ETH001','amount':'.2'}},asset,location,amount,prepared={'preview':preview})
    reopened=Ledger(str(tmp_path/'ledger.sqlite'))
    assert buyhold.reserved(reopened,'earn') == Decimal('.2')
    assert buyhold.available(reopened,'main','ETH') == Decimal('.2995')
    ledger.update('earn','RESOLVED',result={})
    ledger.begin('exit','main',{'operation':'oco'},'ETH','SPOT','.3',recovery=True)
    assert buyhold.reserved(reopened,'exit') == Decimal('.3')
