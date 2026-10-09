import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from binance_mcp.client import BinanceClientError
from binance_mcp.execution import ExecutionService
from binance_mcp.investment import InvestmentService
from binance_mcp.ledger import Ledger
from test_execution import setup, plan
from test_investment import FakeClient


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted', [True, False])
async def test_post_redemption_read_rejection_survives_restart(tmp_path, accepted):
    ledger = Ledger(str(tmp_path / 'saga.sqlite'), {'experiment': {'allocation': '1000', 'mode': 'live', 'location': 'EARN:USDT001'}})
    client = FakeClient()
    original = client.signed_get
    fail = True
    async def signed(product, path, params=None):
        if path == '/api/v3/account' and client.redeem_calls:
            if fail:
                raise BinanceClientError('timestamp rejected', status=400, code=-1021)
            return {'balances': [{'asset': 'USDT', 'free': '700'}]}
        if path.endswith('redemptionRecord'):
            return {'total': 1, 'rows': [{'redeemId': 'r1', 'projectId': 'USDT001', 'asset': 'USDT', 'amount': '700', 'status': 'PAID'}]}
        data = await original(product, path, params)
        if path.endswith('/flexible/position'):
            data['rows'][0]['asset'] = 'USDT'
        return data
    client.signed_get = signed
    if not accepted:
        async def rejected(*args):
            raise BinanceClientError('timestamp rejected', status=400, code=-1021)
        client.simple_earn_redeem = rejected
    svc = ExecutionService(client, ledger, ['BTCUSDT'])
    svc.check_account = AsyncMock()
    svc.check_protection = AsyncMock()
    ledger.set_meta('monitorAt', int(time.time() * 1000))
    ledger.set_meta('protectionAt', int(time.time() * 1000))
    from test_execution import setup
    fixture_ledger, _, _ = setup(tmp_path / 'fixture')
    fixture_policy = fixture_ledger.meta('riskPolicy:experiment')
    fixture_policy.update(maxPositionQuote='1000', maxPlannedDownsideQuote='1000')
    ledger.set_meta('riskPolicy:experiment', fixture_policy)
    ledger.set_meta('recoveryPolicy:experiment', fixture_ledger.meta('recoveryPolicy:experiment'))
    svc.incident_sink_configured = True
    fixture_ledger.db.close()
    ledger.set_meta('streamConnected', True)
    p = {'earnProductId': 'USDT001', 'dualProductId': '2650584', 'optionType': 'PUT', 'exercisedCoin': 'BTC', 'investCoin': 'USDT', 'amount': '700', 'preserveEarnAmount': '0'}
    result = await svc.execute('experiment', 'saga', 'earn_to_dual', p)
    if not accepted:
        assert result['state'] == 'REJECTED'
        assert ledger.available('experiment', 'USDT', 'EARN:USDT001') == 1000
        assert ledger.balance('experiment', 'USDT') == 0
        assert not client.subscribe_calls
        return
    assert result['state'] == 'OUTCOME_UNKNOWN'
    assert ledger.available('experiment', 'USDT', 'EARN:USDT001') == 300
    assert result['result']['phase'] == 'earn_redeem_accepted'
    ledger.db.close()
    fail = False
    ledger = Ledger(str(tmp_path / 'saga.sqlite'))
    svc = ExecutionService(client, ledger, ['BTCUSDT'])
    assert (await svc.reconcile('saga'))['state'] == 'RESOLVED'
    assert ledger.balance('experiment', 'USDT', 'EARN:USDT001') == 300
    assert ledger.balance('experiment', 'USDT') == 700
    await svc.reconcile('saga')
    assert len(client.redeem_calls) == 1
    assert not client.subscribe_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['oco', 'otoco'])
async def test_paper_partial_take_cancels_stop_across_restart(tmp_path, operation):
    ledger, exchange, svc = setup(tmp_path)
    if operation == 'oco':
        ledger.change('experiment', 'BTC', 'SPOT', Decimal('0.5'))
    result = await svc.execute('experiment', 'oco', operation, plan())
    start = result['created'] + 1000
    if operation == 'otoco':
        exchange.candles = [[start, '100', '101', '99', '100', '10', start + 59999]]
        with patch('time.time', return_value=(start + 61000) / 1000):
            await svc.reconcile('oco')
        start += 60000
    exchange.candles = [[start, '105', '111', '100', '110', '1', start + 59999]]
    with patch('time.time', return_value=(start + 61000) / 1000):
        result = await svc.reconcile('oco')
    assert Decimal(result['result']['exitFilled']) == Decimal('0.1')
    ledger.db.close()
    ledger = Ledger(str(tmp_path / 'ledger.sqlite'))
    svc = ExecutionService(exchange, ledger, ['BTCUSDT'])
    exchange.candles += [[start + 60000, '90', '94', '85', '90', '10', start + 119999]]
    with patch('time.time', return_value=(start + 121000) / 1000):
        result = await svc.reconcile('oco')
    assert result['state'] == 'OPEN'
    assert result['result']['activeExitLeg'] == 'take'
    assert Decimal(result['result']['exitFilled']) == Decimal('0.1')
    remaining = ledger.balance('experiment', 'BTC')
    assert remaining == (Decimal('0.4') if operation == 'oco' else Decimal('0.3995'))
    exchange.candles += [[start + 120000, '110', '112', '100', '110', '10', start + 179999]]
    with patch('time.time', return_value=(start + 181000) / 1000):
        result = await svc.reconcile('oco')
    assert result['state'] == 'RESOLVED'
    assert not exchange.writes


@pytest.mark.asyncio
@pytest.mark.parametrize('helper', ['snapshot', 'investment'])
@pytest.mark.parametrize('later_total', [101, None, 100])
async def test_advertised_total_cannot_end_on_empty_page(tmp_path, helper, later_total):
    _, client, svc = setup(tmp_path)
    async def signed(product, path, params=None):
        page = params.get('current', params.get('pageIndex'))
        rows = [{'id': str(i)} for i in range(100)] if page == 1 else []
        data = {'rows': rows, 'list': rows}
        if page == 1 or later_total is not None:
            data['total'] = 101 if page == 1 else later_total
        return data
    client.signed_get = signed
    with pytest.raises(BinanceClientError, match='coverage incomplete'):
        if helper == 'snapshot':
            await svc.snapshots.pages('/test')
        else:
            await InvestmentService(client).all_positions()


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_volume', ['1', '0'])
async def test_paper_stop_stays_active_after_trigger(tmp_path, initial_volume):
    ledger, exchange, svc = setup(tmp_path)
    ledger.change('experiment', 'BTC', 'SPOT', Decimal('0.5'))
    result = await svc.execute('experiment', 'stop', 'oco', plan())
    start = result['created'] + 1000
    exchange.candles = [[start, '90', '94', '85', '90', initial_volume, start + 59999]]
    with patch('time.time', return_value=(start + 61000) / 1000):
        result = await svc.reconcile('stop')
    assert result['result']['activeExitLeg'] == 'stop'
    exchange.candles += [[start + 60000, '115', '120', '111', '115', '10', start + 119999]]
    with patch('time.time', return_value=(start + 121000) / 1000):
        result = await svc.reconcile('stop')
    assert result['state'] == 'RESOLVED'
    # Remaining market quantity trades at the subsequent open, not the canceled 110 limit.
    expected = Decimal('100') + (Decimal('9') + Decimal('46') if initial_volume == '1' else Decimal('57.5')) * Decimal('0.999')
    assert ledger.balance('experiment', 'USDT') == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('helper', ['snapshot', 'investment'])
@pytest.mark.parametrize('total', [101, None])
async def test_complete_pagination_is_accepted(tmp_path, helper, total):
    _, client, svc = setup(tmp_path)
    async def signed(product, path, params=None):
        page = params.get('current', params.get('pageIndex'))
        rows = [{'id': str(i)} for i in (range(100) if page == 1 else [100])]
        return {'total': total, 'rows': rows, 'list': rows}
    client.signed_get = signed
    result = await svc.snapshots.pages('/test') if helper == 'snapshot' else await InvestmentService(client).all_positions()
    assert len(result) == 101


@pytest.mark.asyncio
async def test_otoco_entry_candle_activates_stop_without_exit_capacity(tmp_path):
    ledger, exchange, svc = setup(tmp_path)
    result = await svc.execute('experiment', 'entry-stop', 'otoco', plan())
    start = result['created'] + 1000
    exchange.candles = [[start, '100', '101', '94', '100', '5', start + 59999]]
    with patch('time.time', return_value=(start + 61000) / 1000):
        result = await svc.reconcile('entry-stop')
    assert result['result']['paperStatus'] == 'ENTRY_FILLED'
    assert result['result']['activeExitLeg'] == 'stop'
    exchange.candles += [[start + 60000, '115', '120', '111', '115', '10', start + 119999]]
    with patch('time.time', return_value=(start + 121000) / 1000):
        result = await svc.reconcile('entry-stop')
    assert result['state'] == 'RESOLVED'
    assert ledger.balance('experiment', 'USDT') == Decimal('50') + Decimal('0.499') * Decimal('115') * Decimal('0.999')
