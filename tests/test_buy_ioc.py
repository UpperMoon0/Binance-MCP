"""Synthetic writes only; the global harness blocks external sockets and credentials."""
import asyncio
import copy
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from binance_mcp import buyhold
from binance_mcp.client import BinanceClientError
from binance_mcp.execution import ExecutionService, client_id
from binance_mcp.ledger import Ledger
from test_execution import Exchange
from test_readiness_regressions import persistent_state


def config(mode='live', cap='60'):
    return {'main': {'mode': mode, 'quote': 'USDT', 'fundingSource': 'shared_account', 'budgetCap': '100', 'reinvest': False,
        'riskPolicy': {'version': 1, 'approved': True, 'operations': ['buy_ioc', 'otoco', 'oco', 'order', 'earn_subscribe', 'earn_redeem'],
            'assets': ['ETH', 'USDT'], 'maxPositions': 10, 'maxPositionQuote': '100', 'maxPlannedDownsideQuote': '100',
            'lossPauseQuote': '100', 'executionAllowanceBps': '10', 'valuationMaxAgeMs': 90000},
        'recoveryPolicy': {'version': 1, 'approved': True, 'action': 'operator_only', 'maxDelayMs': 30000},
        'buyHoldPolicy': {'version': 1, 'approved': True, 'mode': 'buy_and_hold', 'symbols': ['ETHUSDT'], 'maxCumulativeQuote': cap}}}


def plan(**changes):
    return {'symbol': 'ETHUSDT', 'quantity': '.5', 'price': '100', 'maxQuoteSpend': '50.05', **changes}


class IocExchange(Exchange):
    def __init__(self, ledger):
        super().__init__()
        self.ledger = ledger
        self.info.update(symbol='ETHUSDT', baseAsset='ETH')
        self.account['balances'][1]['asset'] = 'ETH'
        self.executed = '.5'
        self.status = 'FILLED'
        self.fee_asset = 'ETH'
        self.timeout = False
        self.hidden = False
        self.hide_trades = False
        self.live_remainder = False
        self.products = [{'asset': 'ETH', 'productId': 'ETH001', 'canPurchase': True, 'isSoldOut': False,
                          'status': 'PURCHASING', 'minPurchaseAmount': '.001', 'subscriptionStartTime': 0}]
        self.positions = []
        self.records = []
        self.earn_writes = []
        self.earn_timeout = False

    def balance_change(self, asset, delta):
        row = next(b for b in self.account['balances'] if b['asset'] == asset)
        row['free'] = str(Decimal(row['free']) + delta)

    async def order(self, product, path, action, params):
        assert path == '/api/v3/order' and action == 'create'
        assert params['side'] == 'BUY' and params['timeInForce'] == 'IOC'
        saved = next(i for i in self.ledger.outstanding() if client_id(i['id']) == params['newClientOrderId'])
        assert saved['state'] == 'PREPARED' and Decimal(saved['reserved']) > 0
        # Verify the journal is committed and visible from a second connection before sending.
        filename = self.ledger.db.execute('PRAGMA database_list').fetchone()['file']
        other = Ledger(filename)
        assert other.get(saved['id'])['state'] == 'PREPARED'
        other.db.close()
        self.writes.append((path, action, params))
        if self.failure:
            raise self.failure
        oid = len(self.orders) + 1
        qty = Decimal(self.executed)
        price = Decimal(params['price'])
        fee = qty * Decimal('.001') if self.fee_asset == 'ETH' else qty * price * Decimal('.001')
        order = {'symbol': params['symbol'], 'orderId': oid, 'clientOrderId': params['newClientOrderId'],
                 'status': self.status, 'origQty': params['quantity'], 'executedQty': str(qty), 'side': 'BUY',
                 'type': 'LIMIT', 'price': params['price'], 'timeInForce': 'IOC', 'cummulativeQuoteQty': str(qty*price)}
        self.orders[oid] = order
        self.fills[oid] = [] if not qty else [{'id': oid, 'orderId': oid, 'qty': str(qty), 'quoteQty': str(qty*price),
            'price': str(price), 'commission': str(fee), 'commissionAsset': self.fee_asset, 'isBuyer': True}]
        self.balance_change('USDT', -qty*price - (fee if self.fee_asset == 'USDT' else 0))
        self.balance_change('ETH', qty - (fee if self.fee_asset == 'ETH' else 0))
        if self.timeout:
            raise BinanceClientError('accepted but timed out', status=504, outcome_unknown=True)
        return order

    async def signed_get(self, product, path, params=None):
        if path == '/api/v3/order':
            if self.hidden:
                raise BinanceClientError('not yet visible', status=400, code=-2013)
            return next(o for o in self.orders.values() if o['clientOrderId'] == params['origClientOrderId'])
        if path == '/api/v3/myTrades' and self.hide_trades:
            return []
        if path == '/api/v3/openOrders' and self.live_remainder:
            return list(self.orders.values())
        if path == '/sapi/v1/simple-earn/flexible/list':
            return {'total': len(self.products), 'rows': self.products}
        if path == '/sapi/v1/simple-earn/flexible/position':
            return {'total': len(self.positions), 'rows': self.positions}
        if '/simple-earn/flexible/history/' in path:
            rows = [r for r in self.records if ('purchaseId' in r) == ('subscriptionRecord' in path)]
            return {'total': len(rows), 'rows': rows}
        return await super().signed_get(product, path, params)

    async def simple_earn_subscribe(self, product, amount, auto, source):
        assert auto is False and source == 'SPOT'
        self.earn_writes.append(('subscribe', product, amount))
        self.balance_change('ETH', -Decimal(amount))
        if not self.positions:
            self.positions = [{'productId': product, 'asset': 'ETH', 'totalAmount': '0', 'canRedeem': True, 'autoSubscribe': False}]
        self.positions[0]['totalAmount'] = str(Decimal(self.positions[0]['totalAmount']) + Decimal(amount))
        rid = len(self.records) + 1
        self.records.append({'purchaseId': rid, 'productId': product, 'asset': 'ETH', 'amount': amount, 'status': 'SUCCESS'})
        if self.earn_timeout:
            raise BinanceClientError('accepted but timed out', status=504, outcome_unknown=True)
        return {'purchaseId': rid, 'success': True}

    async def simple_earn_redeem(self, product, amount, dest):
        self.earn_writes.append(('redeem', product, amount))
        self.balance_change('ETH', Decimal(amount))
        self.positions[0]['totalAmount'] = str(Decimal(self.positions[0]['totalAmount']) - Decimal(amount))
        rid = len(self.records) + 1
        self.records.append({'redeemId': rid, 'productId': product, 'asset': 'ETH', 'amount': amount, 'status': 'PAID'})
        return {'redeemId': rid, 'success': True}


def setup(tmp_path, mode='live', cap='60'):
    ledger = Ledger(str(tmp_path/'ledger.sqlite'), config(mode, cap))
    ex = IocExchange(ledger)
    svc = ExecutionService(ex, ledger, ['ETHUSDT'], incident_receiver_required=False, default_strategy_id='main')
    ledger.set_meta('protectionAt', int(time.time()*1000))
    ledger.set_meta('streamConnected', True)
    return ledger, ex, svc


@pytest.mark.asyncio
@pytest.mark.parametrize('quantity,status', [('0', 'EXPIRED'), ('.2', 'EXPIRED'), ('.5', 'FILLED'), ('0', 'REJECTED')])
@pytest.mark.parametrize('fee_asset', ['ETH', 'USDT'])
async def test_ioc_terminal_accounting(tmp_path, quantity, status, fee_asset):
    ledger, ex, svc = setup(tmp_path)
    ex.executed, ex.status, ex.fee_asset = quantity, status, fee_asset
    before = persistent_state(ledger)
    preview = await svc.preview(None, 'buy_ioc', plan())
    assert preview['protection'] == 'UNPROTECTED_BUY_AND_HOLD'
    assert persistent_state(ledger) == before and not ex.writes
    result = await svc.execute(None, 'buy', 'buy_ioc', plan())
    assert result['state'] == 'RESOLVED', result
    assert result['reserved'] == '0'
    data = result['result']
    gross = Decimal(quantity)
    net = gross * Decimal('.999') if fee_asset == 'ETH' else gross
    debit = gross*100 * (Decimal('1.001') if fee_asset == 'USDT' else 1)
    assert Decimal(data['actualQuoteSpent']) == gross*100
    assert Decimal(data['quoteDebit']) == debit
    assert Decimal(data['netAcquiredQuantity']) == net
    assert Decimal(data['feeValueQuote']) == gross*Decimal('.1')
    assert ledger.balance('main', 'ETH') == net
    assert buyhold.quantity(ledger, 'main', 'ETH') == net
    assert ledger.balance('main', 'USDT') == 100-debit
    assert not data['protected'] and not data['liveBuyRemainder']
    assert Decimal(data['buybackBudget']['remainingQuote']) == 60-gross*Decimal('100.1')
    assert len(ex.writes) == 1
    await svc.check_protection()  # intentional hold does not invent a stop-loss
    assert await svc.execute(None, 'buy', 'buy_ioc', plan()) == result
    assert len(ex.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['timeout', 'hidden', 'trades', 'open'])
async def test_restart_and_delayed_evidence_never_rebuy(tmp_path, defect):
    ledger, ex, svc = setup(tmp_path)
    ex.timeout = defect == 'timeout'
    ex.hidden = defect == 'hidden'
    ex.hide_trades = defect == 'trades'
    ex.live_remainder = defect == 'open'
    result = await svc.execute(None, 'buy', 'buy_ioc', plan())
    assert result['state'] == 'OUTCOME_UNKNOWN'
    assert Decimal(result['reserved']) == Decimal('50.05')
    assert ledger.balance('main', 'USDT') == 100
    with pytest.raises(BinanceClientError):
        await svc.execute(None, 'buy', 'buy_ioc', plan(price='99.99'))
    reopened = Ledger(str(tmp_path/'ledger.sqlite'), config())
    fresh = ExecutionService(ex, reopened, ['ETHUSDT'], default_strategy_id='main', incident_receiver_required=False)
    ex.timeout = ex.hidden = ex.hide_trades = ex.live_remainder = False
    result = await fresh.execute(None, 'buy', 'buy_ioc', plan())
    assert result['state'] == 'RESOLVED' and len(ex.writes) == 1
    assert reopened.meta('pause')  # implementation never clears operational blockers
    assert reopened.balance('main', 'USDT') == 50


@pytest.mark.asyncio
async def test_definitive_rejection_releases_only_that_intent(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    ex.failure = BinanceClientError('bad order', status=400, code=-1013)
    result = await svc.execute(None, 'rejected', 'buy_ioc', plan())
    assert result['state'] == 'REJECTED'
    assert buyhold.budget(ledger, 'main')['remainingQuote'] == '60'
    await svc.execute(None, 'rejected', 'buy_ioc', plan())
    assert len(ex.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'maxQuoteSpend': '50'}, {'quantity': '.5001'}, {'price': '100.001'}, {'side': 'SELL'},
                                    {'timeInForce': 'GTC'}, {'type': 'MARKET'}, {'stopPrice': '95'}, {'takeProfit': '110'}])
async def test_contract_and_rounding_fail_passively(tmp_path, change):
    ledger, ex, svc = setup(tmp_path)
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError):
        await svc.preview(None, 'buy_ioc', plan(**change))
    assert persistent_state(ledger) == before and not ex.writes


@pytest.mark.asyncio
async def test_allowance_does_not_replenish_with_sales_or_deposits(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    await svc.execute(None, 'buy', 'buy_ioc', plan())
    # Synthetic owner funding does not reset the distinct cumulative allowance.
    ledger.change('main', 'USDT', 'SPOT', Decimal(100))
    with pytest.raises(BinanceClientError) as error:
        await svc.preview(None, 'buy_ioc', plan())
    assert error.value.blocker == 'BUYBACK_ALLOWANCE'
    assert Decimal(buyhold.budget(ledger, 'main')['spentQuoteIncludingFeeValue']) == Decimal('50.05')


@pytest.mark.asyncio
async def test_two_connections_atomic_cumulative_cap(tmp_path):
    ledger, ex, svc = setup(tmp_path, 'paper', cap='60')
    prepared = {'preview': await svc.preview(None, 'buy_ioc', plan())}
    def reserve(key):
        connection = Ledger(str(tmp_path/'ledger.sqlite'))
        try:
            connection.begin(key, 'main', {'operation': 'buy_ioc', 'params': plan()}, 'USDT', 'SPOT', '50.05', prepared=prepared)
            return True
        except BinanceClientError:
            return False
        finally:
            connection.db.close()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve, ['one', 'two']))
    assert sorted(results) == [False, True]
    assert Decimal(buyhold.budget(ledger, 'main')['remainingQuote']) == Decimal('9.95')


@pytest.mark.asyncio
async def test_policy_default_unknown_budget_and_other_earmarks(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    for unknown in ('missing', ''):
        with pytest.raises(BinanceClientError):
            await svc.execute(unknown, 'bad', 'buy_ioc', plan())
    hold = ledger.meta('buyHoldPolicy:main')
    ledger.set_meta('buyHoldPolicy:main', None)
    with pytest.raises(BinanceClientError) as error:
        await svc.preview(None, 'buy_ioc', plan())
    assert error.value.blocker == 'BUY_HOLD_POLICY_REQUIRED'
    ledger.set_meta('buyHoldPolicy:main', hold)
    with pytest.raises(KeyError):
        await svc.preview(None, 'otoco', {'symbol':'ETHUSDT','quantity':'.5','price':'100'})
    ledger.begin('earmark', 'main', {'operation': 'earmark'}, 'USDT', 'SPOT', '60', recovery=True)
    with pytest.raises(BinanceClientError):
        await svc.preview(None, 'buy_ioc', plan())
    assert not ex.writes


@pytest.mark.asyncio
async def test_buy_policy_is_immutable(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    with pytest.raises(BinanceClientError, match='immutable'):
        Ledger(str(tmp_path/'ledger.sqlite'), config(cap='100'))


@pytest.mark.asyncio
async def test_owned_eth_earn_and_redemption_preserve_cost_and_unrelated_assets(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    await svc.execute(None, 'buy', 'buy_ioc', plan())
    p = {'productId': 'ETH001', 'asset': 'ETH', 'amount': '.4995'}
    before = persistent_state(ledger)
    preview, asset, location, amount = await svc.investment_preflight('main', 'earn_subscribe', p)
    assert asset == 'ETH' and preview['acquiredAsset']
    assert persistent_state(ledger) == before
    result = await svc.execute(None, 'earn', 'earn_subscribe', p)
    assert result['state'] == 'RESOLVED', result
    assert result['result']['verifiedDestination'] == 'EARN:ETH001'
    assert ledger.balance('main', 'ETH') == 0
    assert ledger.balance('main', 'ETH', 'EARN:ETH001') == Decimal('.4995')
    assert buyhold.quantity(ledger, 'main', 'ETH', 'EARN:ETH001') == Decimal('.4995')
    assert ledger.db.execute("SELECT cost FROM balances WHERE asset='ETH' AND location='EARN:ETH001'").fetchone()[0] == '50.0'
    assert next(b['free'] for b in ex.account['balances'] if b['asset'] == 'ETH') == '10.0000'
    assert len(ex.earn_writes) == 1
    assert (await svc.execute(None, 'earn', 'earn_subscribe', p))['state'] == 'RESOLVED'
    result = await svc.execute(None, 'redeem', 'earn_redeem', {'productId':'ETH001', 'amount':'.2'})
    assert result['state'] == 'RESOLVED', result
    assert ledger.balance('main', 'ETH') == Decimal('.2')
    assert buyhold.quantity(ledger, 'main', 'ETH') == Decimal('.2')
    assert sum(Decimal(r[0]) for r in ledger.db.execute("SELECT cost FROM balances WHERE asset='ETH'")) == 50
    assert Decimal(buyhold.budget(ledger, 'main')['spentQuoteIncludingFeeValue']) == Decimal('50.05')


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['asset', 'product', 'sold_out', 'minimum', 'auto', 'unknown_auto', 'amount', 'gross', 'unowned'])
async def test_acquired_earn_failures_leave_funds_in_spot(tmp_path, defect):
    ledger, ex, svc = setup(tmp_path)
    await svc.execute(None, 'buy', 'buy_ioc', plan())
    p = {'productId':'ETH001', 'asset':'ETH', 'amount':'.4995'}
    if defect == 'asset': ex.products[0]['asset'] = 'BTC'
    elif defect == 'product': ex.products = []
    elif defect == 'sold_out': ex.products[0]['isSoldOut'] = True
    elif defect == 'minimum': ex.products[0]['minPurchaseAmount'] = '1'
    elif defect in ('auto', 'unknown_auto'):
        ex.positions = [{'productId':'ETH001','asset':'ETH','totalAmount':'0','autoSubscribe':True if defect == 'auto' else None}]
    elif defect == 'amount': p['amount'] = '10'
    elif defect == 'gross': p['amount'] = '.5'
    else: buyhold.change(ledger, 'main', 'ETH', 'SPOT', -Decimal('.4995'))
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError):
        await svc.investment_preflight('main', 'earn_subscribe', p)
    assert persistent_state(ledger) == before
    assert not ex.earn_writes and ledger.balance('main', 'ETH') == Decimal('.4995')


@pytest.mark.asyncio
async def test_earn_uncertain_response_never_duplicates_subscription(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    await svc.execute(None, 'buy', 'buy_ioc', plan())
    ex.earn_timeout = True
    p = {'productId':'ETH001','asset':'ETH','amount':'.4995'}
    result = await svc.execute(None, 'earn', 'earn_subscribe', p)
    assert result['state'] == 'OUTCOME_UNKNOWN'
    assert Decimal(result['reserved']) == Decimal('.4995')
    reopened = Ledger(str(tmp_path/'ledger.sqlite'))
    svc = ExecutionService(ex, reopened, ['ETHUSDT'], default_strategy_id='main')
    with pytest.raises(BinanceClientError, match='identifier unavailable'):
        await svc.execute(None, 'earn', 'earn_subscribe', p)
    assert len(ex.earn_writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['price', 'quantity', 'side', 'tif', 'client', 'status', 'quote_total', 'trade_side', 'fee_asset', 'fee_bound', 'duplicate', 'trade_price'])
async def test_contradictory_exchange_evidence_keeps_full_reservation(tmp_path, defect):
    ledger, ex, svc = setup(tmp_path)
    original = ex.order
    async def corrupt(*args):
        response = await original(*args)
        order = ex.orders[1]
        trade = ex.fills[1][0]
        if defect == 'price': order['price'] = '101'
        elif defect == 'quantity': order['origQty'] = '.6'
        elif defect == 'side': order['side'] = 'SELL'
        elif defect == 'tif': order['timeInForce'] = 'GTC'
        elif defect == 'client': order['clientOrderId'] = 'foreign'
        elif defect == 'status': order['status'] = 'NEW'
        elif defect == 'quote_total': order['cummulativeQuoteQty'] = '49'
        elif defect == 'trade_side': trade['isBuyer'] = False
        elif defect == 'fee_asset': trade['commissionAsset'] = 'BNB'
        elif defect == 'fee_bound': trade['commission'] = '.001'
        elif defect == 'duplicate': ex.fills[1].append(copy.deepcopy(trade))
        elif defect == 'trade_price': trade['price'] = '101'
        return response
    ex.order = corrupt
    result = await svc.execute(None, 'bad-evidence', 'buy_ioc', plan())
    assert result['state'] == 'OUTCOME_UNKNOWN'
    assert Decimal(result['reserved']) == Decimal('50.05')
    assert ledger.balance('main', 'ETH') == 0 and ledger.balance('main', 'USDT') == 100
    assert not list(ledger.db.execute("SELECT * FROM events WHERE category='buyback_spend'"))
    assert len(ex.writes) == 1


@pytest.mark.asyncio
async def test_multiple_fills_are_atomic_and_commissions_are_not_double_debited(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    original = ex.order
    async def split(*args):
        response = await original(*args)
        ex.fills[1] = [
            {'id': 10, 'orderId': 1, 'qty': '.2', 'quoteQty': '20', 'price': '100', 'commission': '.0002', 'commissionAsset': 'ETH', 'isBuyer': True},
            {'id': 11, 'orderId': 1, 'qty': '.3', 'quoteQty': '30', 'price': '100', 'commission': '.03', 'commissionAsset': 'USDT', 'isBuyer': True}]
        return response
    ex.order = split
    result = await svc.execute(None, 'mixed', 'buy_ioc', plan())
    assert result['state'] == 'RESOLVED'
    assert Decimal(result['result']['quoteDebit']) == Decimal('50.03')
    assert Decimal(result['result']['feeValueQuote']) == Decimal('.05')
    assert ledger.balance('main', 'ETH') == Decimal('.4998')
    assert ledger.balance('main', 'USDT') == Decimal('49.97')
    assert Decimal(buyhold.budget(ledger, 'main')['spentQuoteIncludingFeeValue']) == Decimal('50.05')


@pytest.mark.asyncio
async def test_new_buy_requires_fresh_permission_and_backing_revalidation(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    await svc.preview(None, 'buy_ioc', plan())
    ex.account['canTrade'] = False
    with pytest.raises(BinanceClientError):
        await svc.execute(None, 'new', 'buy_ioc', plan())
    assert not ex.writes and ledger.get('new') is None
    ex.account['canTrade'] = True
    ex.account['balances'][0]['free'] = '90'  # insufficient backing for the full earmarked budget
    with pytest.raises(BinanceClientError):
        await svc.execute(None, 'new', 'buy_ioc', plan())
    assert not ex.writes


@pytest.mark.asyncio
async def test_unrelated_unprotected_assets_are_not_exempted(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    await svc.execute(None, 'buy', 'buy_ioc', plan())
    ledger.change('main', 'ETH', 'SPOT', Decimal('.01'), Decimal('1'))
    with pytest.raises(BinanceClientError, match='unprotected'):
        await svc.check_protection()
    assert ledger.meta('pause')


@pytest.mark.asyncio
async def test_sale_consumes_hold_attribution_without_restoring_allowance(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    result = await svc.execute(None, 'buy', 'buy_ioc', plan())
    sale, _ = ledger.begin('sell', 'main', {'operation': 'order'}, 'ETH', 'SPOT', '.1', recovery=True)
    trade = {'id': 100, 'orderId': 5, 'qty': '.1', 'quoteQty': '10', 'price': '100',
             'commission': '.01', 'commissionAsset': 'USDT', 'isBuyer': False}
    svc.apply_fill(sale, trade, result['result']['preview'])
    assert buyhold.quantity(ledger, 'main', 'ETH') == Decimal('.3995')
    assert Decimal(buyhold.budget(ledger, 'main')['spentQuoteIncludingFeeValue']) == Decimal('50.05')


@pytest.mark.asyncio
async def test_external_auto_subscription_movement_blocks_new_earn(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    await svc.execute(None, 'buy', 'buy_ioc', plan())
    ex.balance_change('ETH', -Decimal('10.4995'))
    ex.positions = [{'productId':'ETH001', 'asset':'ETH', 'totalAmount':'10.4995', 'autoSubscribe':True}]
    with pytest.raises(BinanceClientError):
        await svc.execute(None, 'earn', 'earn_subscribe', {'asset':'ETH','productId':'ETH001','amount':'.4995'})
    assert not ex.earn_writes
    assert ledger.balance('main', 'ETH') == Decimal('.4995')
    assert ledger.meta('pause')


@pytest.mark.asyncio
async def test_owner_attributed_rewards_remain_separate_from_funding(tmp_path):
    from binance_mcp.accounting import apply_audit
    ledger, ex, svc = setup(tmp_path)
    await svc.execute(None, 'buy', 'buy_ioc', plan())
    await svc.execute(None, 'earn', 'earn_subscribe', {'asset':'ETH','productId':'ETH001','amount':'.4995'})
    expected = ledger.meta('expectedTotals')
    observed = {**expected, 'ETH':str(Decimal(expected['ETH']) + Decimal('.001'))}
    audit = {'id':'reward', 'evidence':['synthetic-reward-history'], 'accountDeltas':{'ETH':'.001'},
             'adjustments':[{'strategyId':'main','asset':'ETH','location':'EARN:ETH001','category':'earn_reward',
                             'quantityDelta':'.001','costDelta':'.1','quoteValue':'.1','buyHoldReward':True}]}
    apply_audit(ledger, audit, observed)
    apply_audit(ledger, audit, observed)
    assert ledger.balance('main','ETH','EARN:ETH001') == Decimal('.5005')
    assert buyhold.quantity(ledger,'main','ETH','EARN:ETH001') == Decimal('.5005')
    assert Decimal(buyhold.budget(ledger,'main')['spentQuoteIncludingFeeValue']) == Decimal('50.05')
    assert ledger.balance('main','USDT') == 50


@pytest.mark.asyncio
async def test_paper_ioc_is_terminal_conservative_zero_fill(tmp_path):
    ledger, ex, svc = setup(tmp_path, 'paper')
    result = await svc.execute(None, 'paper-ioc', 'buy_ioc', plan())
    assert result['state'] == 'RESOLVED' and result['reserved'] == '0'
    assert result['result']['simulation'] == 'CONSERVATIVE_ZERO_FILL'
    assert result['result']['buybackBudget']['remainingQuote'] == '60'
    assert not ex.writes and ledger.balance('main', 'USDT') == 100


@pytest.mark.asyncio
@pytest.mark.parametrize('policy_key,value,blocker', [('maxPlannedDownsideQuote','10','PLANNED_DOWNSIDE'),
                                                     ('maxPositionQuote','10','POSITION_VALUE')])
async def test_ioc_keeps_risk_constraints(tmp_path, policy_key, value, blocker):
    ledger, ex, svc = setup(tmp_path)
    risk = ledger.meta('riskPolicy:main')
    risk[policy_key] = value
    ledger.set_meta('riskPolicy:main', risk)
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError) as error:
        await svc.preview(None, 'buy_ioc', plan())
    assert error.value.blocker == blocker
    assert persistent_state(ledger) == before and not ex.writes


@pytest.mark.asyncio
async def test_owned_earn_preserves_existing_unrelated_product_position(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    ex.positions = [{'productId':'ETH001', 'asset':'ETH', 'totalAmount':'5', 'autoSubscribe':False, 'canRedeem':True}]
    await svc.execute(None, 'buy', 'buy_ioc', plan())
    result = await svc.execute(None, 'earn', 'earn_subscribe', {'asset':'ETH','productId':'ETH001','amount':'.4995'})
    assert result['state'] == 'RESOLVED'
    assert ex.positions[0]['totalAmount'] == '5.4995' and ex.positions[0]['autoSubscribe'] is False
    assert ledger.balance('main','ETH','EARN:ETH001') == Decimal('.4995')
    assert ledger.balance('main','USDT') == 50


@pytest.mark.asyncio
async def test_restart_after_prepared_never_sends_unobserved_original(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    preview = await svc.preview(None, 'buy_ioc', plan())
    path, order = svc.order_params('crash', preview)
    ledger.begin('crash','main',{'operation':'buy_ioc','params':plan()},'USDT','SPOT',preview['reservation'],
                 prepared={'preview':preview,'path':path,'order':order})
    ex.hidden = True
    reopened = Ledger(str(tmp_path/'ledger.sqlite'))
    svc = ExecutionService(ex,reopened,['ETHUSDT'],default_strategy_id='main')
    with pytest.raises(BinanceClientError):
        await svc.execute(None,'crash','buy_ioc',plan())
    assert not ex.writes and Decimal(reopened.get('crash')['reserved']) == Decimal('50.05')


@pytest.mark.asyncio
async def test_passive_earn_loss_preview_does_not_persist_loss_pause(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    await svc.execute(None,'buy','buy_ioc',plan())
    risk = ledger.meta('riskPolicy:main')
    risk['lossPauseQuote'] = '.001'
    ledger.set_meta('riskPolicy:main',risk)
    before = persistent_state(ledger)
    with pytest.raises(BinanceClientError) as error:
        await svc.investment_preflight('main','earn_subscribe',{'asset':'ETH','productId':'ETH001','amount':'.4995'})
    assert error.value.blocker == 'LOSS_PAUSED'
    assert persistent_state(ledger) == before


@pytest.mark.asyncio
async def test_migration_revision_includes_hold_attribution(tmp_path):
    from binance_mcp.migration import revision
    ledger, ex, svc = setup(tmp_path)
    before = revision(ledger)
    buyhold.change(ledger,'main','ETH','SPOT',Decimal('.1'))
    assert revision(ledger) != before


@pytest.mark.asyncio
async def test_ioc_does_not_require_linked_order_support(tmp_path):
    ledger, ex, svc = setup(tmp_path)
    ex.info.update(ocoAllowed=False,otoAllowed=False,orderTypes=['LIMIT'])
    assert (await svc.execute(None,'buy','buy_ioc',plan()))['state'] == 'RESOLVED'
    assert all(path == '/api/v3/order' and action == 'create' for path,action,p in ex.writes)
