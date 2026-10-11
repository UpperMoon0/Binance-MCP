import copy,time
from decimal import Decimal
import pytest
from binance_mcp.client import BinanceClientError
from test_readiness_regressions import persistent_state
from binance_mcp.accounting import apply_audit
from binance_mcp import buyhold
from test_buy_ioc import setup,plan

@pytest.mark.asyncio
async def test_audit_rejects_other_order_trade(tmp_path):
    ledger,ex,svc=setup(tmp_path)
    ex.timeout=True
    await svc.execute(None,'buy','buy_ioc',plan())
    expected=ledger.meta('expectedTotals')
    observed={**expected,'ETH':str(Decimal(expected['ETH'])+Decimal('.4995')),'USDT':str(Decimal(expected['USDT'])-50)}
    trades=copy.deepcopy(ex.fills[1]); trades[0]['orderId']=999
    audit={'id':'wrong-order','evidence':['synthetic history'],'accountDeltas':{'ETH':'.4995','USDT':'-50'},
      'iocRecoveries':[{'intentId':'buy','approveActualFees':True,'observedAt':int(time.time()*1000),'order':copy.deepcopy(ex.orders[1]),'trades':trades,'openOrders':[]}]}
    from binance_mcp.client import BinanceClientError
    with pytest.raises(BinanceClientError): apply_audit(ledger,audit,observed)

@pytest.mark.asyncio
async def test_base_fee_sales_keep_attribution_equal_to_remaining_holds(tmp_path):
    ledger,ex,svc=setup(tmp_path)
    bought=await svc.execute(None,'buy','buy_ioc',plan())
    sale,_=ledger.begin('sell','main',{'operation':'order'},'ETH','SPOT','.4',recovery=True)
    ledger.update('sell','OPEN',result={})
    svc.apply_fill(ledger.get('sell'),{'id':100,'orderId':99,'qty':'.1','quoteQty':'10','price':'100','commission':'.0001','commissionAsset':'ETH','isBuyer':False},bought['result']['preview'])
    svc.apply_fill(ledger.get('sell'),{'id':101,'orderId':99,'qty':'.1','quoteQty':'10','price':'100','commission':'.0001','commissionAsset':'ETH','isBuyer':False},bought['result']['preview'])
    assert buyhold.quantity(ledger,'main','ETH') == ledger.balance('main','ETH')

@pytest.mark.asyncio
async def test_uncertain_earn_owner_audit_preserves_acquired_ownership(tmp_path):
    ledger,ex,svc=setup(tmp_path)
    await svc.execute(None,'buy','buy_ioc',plan())
    ex.earn_timeout=True
    await svc.execute(None,'earn','earn_subscribe',{'asset':'ETH','productId':'ETH001','amount':'.4995'})
    audit={'id':'earn-recovery','evidence':['synthetic exact successful subscription history'],'adjustments':[
       {'strategyId':'main','asset':'ETH','location':'SPOT','category':'transfer','quantityDelta':'-.4995','costDelta':'-50'},
       {'strategyId':'main','asset':'ETH','location':'EARN:ETH001','category':'transfer','quantityDelta':'.4995','costDelta':'50'}],
       'resolveIntents':[{'intentId':'earn','state':'RESOLVED'}]}
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError) as error:
        apply_audit(ledger,audit,ledger.meta('expectedTotals'))
    assert error.value.blocker == 'EARN_AUDIT_RECOVERY_REQUIRED'
    assert persistent_state(ledger) == before
    record = {**ex.records[0], 'time':int(time.time()*1000)}
    audit.pop('adjustments');audit.pop('resolveIntents')
    audit['earnRecoveries']=[{'intentId':'earn','approveAttribution':True,'observedAt':int(time.time()*1000),'record':record}]
    apply_audit(ledger,audit,ledger.meta('expectedTotals'))
    before = persistent_state(ledger)
    apply_audit(ledger,audit,ledger.meta('expectedTotals'))
    assert persistent_state(ledger) == before
    assert ledger.get('earn')['state'] == 'RESOLVED'
    assert ledger.balance('main','ETH') == 0
    assert buyhold.quantity(ledger,'main','ETH','EARN:ETH001') == Decimal('.4995')


def recovery_audit(intent_id,record,**changes):
    return {'id':'recover-'+intent_id,'evidence':['synthetic exact receipt'],
            'earnRecoveries':[{'intentId':intent_id,'approveAttribution':True,
                'observedAt':int(time.time()*1000),'record':{**record,'time':int(time.time()*1000)},**changes}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('orderId',None),('orderId',True),('orderId','not-an-id'),('id',None),('id',True),('id',-1),('id',1.0),('id','x'),('id',2**63)])
async def test_offline_ioc_requires_valid_exchange_identity_fields(tmp_path,field,value):
    ledger,ex,svc=setup(tmp_path);ex.timeout=True
    await svc.execute(None,'buy','buy_ioc',plan())
    expected=ledger.meta('expectedTotals')
    observed={**expected,'ETH':str(Decimal(expected['ETH'])+Decimal('.4995')),'USDT':str(Decimal(expected['USDT'])-50)}
    trades=copy.deepcopy(ex.fills[1]);trades[0][field]=value
    audit={'id':'bad-id','evidence':['fake'],'accountDeltas':{'ETH':'.4995','USDT':'-50'},'iocRecoveries':[{
           'intentId':'buy','approveActualFees':True,'observedAt':int(time.time()*1000),
           'order':ex.orders[1],'trades':trades,'openOrders':[]}]}
    before=persistent_state(ledger)
    with pytest.raises(BinanceClientError):apply_audit(ledger,audit,observed)
    assert persistent_state(ledger)==before


@pytest.mark.asyncio
async def test_duplicate_canonical_trade_ids_are_rejected(tmp_path):
    ledger,ex,svc=setup(tmp_path);ex.timeout=True
    await svc.execute(None,'buy','buy_ioc',plan())
    rows=[{**ex.fills[1][0],'id':value,'qty':'.25','quoteQty':'25','commission':'.00025'} for value in (1,'01')]
    before=persistent_state(ledger)
    with pytest.raises(BinanceClientError):svc.resolve_ioc_evidence(ledger.get('buy'),ex.orders[1],rows,[])
    assert persistent_state(ledger)==before


@pytest.mark.asyncio
@pytest.mark.parametrize('unrelated', ['0','.2'])
@pytest.mark.parametrize('fee_asset',['ETH','USDT'])
async def test_multiple_partial_and_final_sells_preserve_exact_fee_attribution(tmp_path,unrelated,fee_asset):
    ledger,ex,svc=setup(tmp_path)
    bought=await svc.execute(None,'buy','buy_ioc',plan())
    if Decimal(unrelated):ledger.change('main','ETH','SPOT',Decimal(unrelated),Decimal(unrelated)*100)
    params={'symbol':'ETHUSDT','side':'SELL','quantity':'.4','price':'100'}
    preview=await svc.preview(None,'order',params)
    sale,_=ledger.begin('sell','main',{'operation':'order','params':params},'ETH','SPOT','.4',recovery=True,prepared={'preview':preview})
    fee=ledger.db.execute('SELECT * FROM sell_fee_reservations WHERE intent=?',('sell',)).fetchone()
    assert Decimal(fee['quantity'])==Decimal('.0004')
    # Another intent consumes all remaining free units; the sale cannot borrow them.
    other=ledger.available('main','ETH')
    ledger.begin('other','main',{'operation':'oco'},'ETH','SPOT',str(other),recovery=True)
    ledger.update('other','OPEN',result={})
    other_hold=buyhold.reserved(ledger,'other')
    for n in range(4):
        svc.apply_fill(ledger.get('sell'),{'id':100+n,'orderId':99,'qty':'.1','quoteQty':'10','price':'100',
                       'commission':'.0001' if fee_asset=='ETH' else '.01','commissionAsset':fee_asset,'isBuyer':False},preview)
        assert buyhold.quantity(ledger,'main','ETH')<=ledger.balance('main','ETH')
        assert buyhold.reserved(ledger,'other')==other_hold
        assert ledger.available('main','ETH')>=0
    expected=Decimal('.4995')+Decimal(unrelated)-Decimal('.4')-(Decimal('.0004') if fee_asset=='ETH' else 0)
    assert ledger.balance('main','ETH')==expected
    assert buyhold.quantity(ledger,'main','ETH')==expected
    assert ledger.get('sell')['reserved']=='0'
    assert buyhold.reserved(ledger,'sell')==0
    fee=ledger.db.execute('SELECT * FROM sell_fee_reservations WHERE intent=?',('sell',)).fetchone()
    assert Decimal(fee['quantity'])==Decimal(fee['held'])==0


@pytest.mark.asyncio
async def test_legacy_fee_fallback_cannot_borrow_other_reserved_units(tmp_path):
    ledger,ex,svc=setup(tmp_path);bought=await svc.execute(None,'buy','buy_ioc',plan())
    ledger.begin('sell','main',{'operation':'order'},'ETH','SPOT','.4',recovery=True)
    ledger.begin('other','main',{'operation':'oco'},'ETH','SPOT','.0995',recovery=True)
    before=persistent_state(ledger)
    with pytest.raises(BinanceClientError) as error:
        svc.apply_fill(ledger.get('sell'),{'id':100,'orderId':99,'qty':'.1','quoteQty':'10','price':'100',
                       'commission':'.0001','commissionAsset':'ETH','isBuyer':False},bought['result']['preview'])
    assert error.value.blocker=='FEE_RESERVE'
    assert persistent_state(ledger)==before


@pytest.mark.asyncio
async def test_uncertain_earn_redemption_recovery_moves_exact_cost_and_hold(tmp_path):
    from binance_mcp.ledger import Ledger
    ledger,ex,svc=setup(tmp_path)
    await svc.execute(None,'buy','buy_ioc',plan())
    await svc.execute(None,'subscribe','earn_subscribe',{'asset':'ETH','productId':'ETH001','amount':'.4995'})
    original=ex.simple_earn_redeem
    async def timeout(*args):
        await original(*args)
        raise BinanceClientError('accepted but timed out',status=504,outcome_unknown=True)
    ex.simple_earn_redeem=timeout
    result=await svc.execute(None,'redeem','earn_redeem',{'productId':'ETH001','amount':'.2'})
    assert result['state']=='OUTCOME_UNKNOWN'
    audit=recovery_audit('redeem',ex.records[-1])
    apply_audit(ledger,audit,ledger.meta('expectedTotals'))
    reopened=Ledger(str(tmp_path/'ledger.sqlite'))
    assert reopened.balance('main','ETH')==buyhold.quantity(reopened,'main','ETH')==Decimal('.2')
    assert reopened.balance('main','ETH','EARN:ETH001')==buyhold.quantity(reopened,'main','ETH','EARN:ETH001')==Decimal('.2995')
    assert sum(Decimal(r[0]) for r in reopened.db.execute("SELECT cost FROM balances WHERE asset='ETH'"))==50
    assert reopened.get('redeem')['state']=='RESOLVED'
    assert buyhold.reserved(reopened,'redeem')==0
    assert len(ex.earn_writes)==2
    assert reopened.meta('pause')


@pytest.mark.asyncio
@pytest.mark.parametrize('defect',['asset','product','contradictory_product','amount','status','time','id','direction','source','unapproved','stale','mixed','delta'])
async def test_invalid_earn_recovery_rolls_back_every_table(tmp_path,defect):
    ledger,ex,svc=setup(tmp_path);await svc.execute(None,'buy','buy_ioc',plan())
    ex.earn_timeout=True
    await svc.execute(None,'earn','earn_subscribe',{'asset':'ETH','productId':'ETH001','amount':'.4995'})
    audit=recovery_audit('earn',ex.records[0]);rec=audit['earnRecoveries'][0];record=rec['record']
    if defect=='asset':record['asset']='BTC'
    elif defect=='product':record['productId']='ETH002'
    elif defect=='contradictory_product':record['projectId']='ETH002'
    elif defect=='amount':record['amount']='.5'
    elif defect=='status':record['status']='FAILED'
    elif defect=='time':record['time']=1
    elif defect=='id':record['purchaseId']=None
    elif defect=='direction':record['redeemId']=record['purchaseId']
    elif defect=='source':record['sourceAccount']='FUND'
    elif defect=='unapproved':rec['approveAttribution']=False
    elif defect=='stale':rec['observedAt']-=100000
    elif defect=='mixed':audit['adjustments']=[{'asset':'ETH'}]
    elif defect=='delta':audit['accountDeltas']={'ETH':'0'}
    before=persistent_state(ledger)
    with pytest.raises(BinanceClientError):apply_audit(ledger,audit,ledger.meta('expectedTotals'))
    assert persistent_state(ledger)==before


@pytest.mark.asyncio
async def test_earn_receipt_cannot_be_reused_for_another_intent(tmp_path):
    ledger,ex,svc=setup(tmp_path);await svc.execute(None,'buy','buy_ioc',plan())
    await svc.execute(None,'one','earn_subscribe',{'asset':'ETH','productId':'ETH001','amount':'.2'})
    ex.earn_timeout=True
    await svc.execute(None,'two','earn_subscribe',{'asset':'ETH','productId':'ETH001','amount':'.2'})
    audit=recovery_audit('two',ex.records[0])
    before=persistent_state(ledger)
    with pytest.raises(BinanceClientError) as error:apply_audit(ledger,audit,ledger.meta('expectedTotals'))
    assert error.value.blocker=='EARN_HISTORY_IDENTITY'
    assert persistent_state(ledger)==before
    good=recovery_audit('two',ex.records[-1])
    apply_audit(ledger,good,ledger.meta('expectedTotals'))
    assert buyhold.quantity(ledger,'main','ETH','EARN:ETH001')==Decimal('.4')
    assert buyhold.quantity(ledger,'main','ETH')==Decimal('.0995')
