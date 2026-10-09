from decimal import Decimal

import pytest

from binance_mcp.client import BinanceClientError
from binance_mcp.execution import ExecutionService, client_id
from binance_mcp.ledger import Ledger
from test_execution import Exchange, plan


def setup_owners(tmp_path):
    ledger = Ledger(str(tmp_path / 'owners.sqlite'), {
        name: {'allocation': '100', 'mode': 'live'} for name in ('first', 'second')})
    for name in ('first', 'second'):
        ledger.change(name, 'BTC', 'SPOT', Decimal('0.5'))
    exchange = Exchange()
    exchange.account['balances'][1]['free'] = '1'
    return ledger, exchange, ExecutionService(exchange, ledger, ['BTCUSDT'])


def sell_params(operation):
    return plan() if operation == 'oco' else {'symbol': 'BTCUSDT', 'quantity': '0.5', 'price': '100', 'side': 'SELL'}


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['order', 'oco'])
async def test_recovery_sell_requires_aggregate_spot_backing(tmp_path, operation):
    ledger, exchange, svc = setup_owners(tmp_path)
    await svc.check_account()
    exchange.account['balances'][1]['free'] = '0.5'
    original = exchange.signed_get
    async def signed(product, path, params=None):
        if path.endswith('/flexible/position'):
            return {'total': 1, 'rows': [{'asset': 'BTC', 'productId': 'BTC001', 'totalAmount': '0.5'}]}
        return await original(product, path, params)
    exchange.signed_get = signed
    with pytest.raises(BinanceClientError, match='recorded account location'):
        await svc.check_account()
    assert ledger.meta('pause')
    with pytest.raises(BinanceClientError, match='backed'):
        await svc.execute('first', 'recovery', operation, sell_params(operation))
    assert not exchange.writes
    assert ledger.get('recovery') is None
    assert ledger.available('first', 'BTC') == Decimal('0.5')
    assert ledger.available('second', 'BTC') == Decimal('0.5')


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['order', 'oco'])
@pytest.mark.parametrize('backed', [True, False])
async def test_recovery_backing_counts_profit_reserves_but_excludes_other_locations(tmp_path, operation, backed):
    ledger, exchange, svc = setup_owners(tmp_path)
    ledger.change('second', 'BTC', 'PROFIT_RESERVE', Decimal('0.1'))
    ledger.change('second', 'BTC', 'EARN:BTC001', Decimal('10'))
    Ledger(str(tmp_path / 'owners.sqlite'), {'paper': {'allocation': '100', 'mode': 'paper'}}).db.close()
    ledger.change('paper', 'BTC', 'SPOT', Decimal('10'))
    exchange.account['balances'][1].update(free='0.6' if backed else '0.5', locked='0.5')
    ledger.pause('unprotected owned quantity; recovery required')
    if not backed:
        with pytest.raises(BinanceClientError, match='backed'):
            await svc.execute('first', 'repair', operation, sell_params(operation))
        assert not exchange.writes
    else:
        await svc.execute('first', 'repair', operation, sell_params(operation))
        assert len(exchange.writes) == 1
    assert ledger.balance('second', 'BTC') == Decimal('0.5')


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['order', 'oco'])
@pytest.mark.parametrize('bad_balance', [None, 'duplicate', 'invalid', 'missing_locked', 'read_error'])
async def test_recovery_requires_current_valid_balance_evidence(tmp_path, operation, bad_balance):
    ledger, exchange, svc = setup_owners(tmp_path)
    await svc.check_account()  # Previously successful evidence cannot authorize the next sale.
    original = exchange.signed_get
    async def signed(product, path, params=None):
        if path == '/api/v3/account':
            if bad_balance == 'read_error':
                raise BinanceClientError('balance unavailable')
            account = {**exchange.account, 'balances': [dict(b) for b in exchange.account['balances']]}
            if bad_balance is None:
                account['balances'] = None
            elif bad_balance == 'duplicate':
                account['balances'].append(dict(account['balances'][1]))
            elif bad_balance == 'invalid':
                account['balances'][1]['locked'] = 'NaN'
            else:
                del account['balances'][1]['locked']
            return account
        return await original(product, path, params)
    exchange.signed_get = signed
    with pytest.raises(BinanceClientError):
        await svc.execute('first', 'invalid', operation, sell_params(operation))
    assert not exchange.writes
    assert ledger.get('invalid') is None


@pytest.mark.asyncio
async def test_cancellation_still_available_without_backing_or_account_reads(tmp_path):
    ledger, exchange, svc = setup_owners(tmp_path)
    ledger.begin('target', 'first', {'operation': 'order', 'params': sell_params('order')}, 'BTC', 'SPOT', '0.5')
    status = 'NEW'
    async def signed(product, path, params=None):
        if path == '/api/v3/order':
            return {'orderId': 1, 'symbol': 'BTCUSDT', 'clientOrderId': client_id('target'), 'status': status,
                    'side': 'SELL', 'executedQty': '0', 'origQty': '0.5'}
        raise BinanceClientError('account reads unavailable')
    async def order(product, path, action, params):
        nonlocal status
        exchange.writes.append((path, action, params))
        status = 'CANCELED'
        return {'status': status}
    exchange.signed_get, exchange.order = signed, order
    ledger.pause('account location ownership requires reconciliation')
    result = await svc.execute('first', 'cancel', 'cancel', {'targetIntentId': 'target'})
    assert result['state'] == 'RESOLVED'
    assert ledger.get('target')['state'] == 'RESOLVED'
    assert exchange.writes[0][1] == 'cancel'
    assert ledger.available('first', 'BTC') == Decimal('0.5')
