"""Explicit buy-and-hold authority and non-replenishing buyback accounting.

Hold quantities are a subset of balances, never additional assets or funding.
"""
from decimal import Decimal
from .client import BinanceClientError
from .ledger import number


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'approved', 'mode', 'symbols', 'maxCumulativeQuote'}
            or value['version'] != 1 or value['approved'] is not True or value['mode'] != 'buy_and_hold'
            or not isinstance(value['symbols'], list) or not value['symbols']
            or any(not isinstance(s, str) or not s for s in value['symbols'])
            or len(set(value['symbols'])) != len(value['symbols'])):
        raise BinanceClientError('explicit buy-and-hold policy required', blocker='BUY_HOLD_POLICY_REQUIRED')
    number(value['maxCumulativeQuote'])
    return value


def authorize(ledger, strategy, symbol):
    value = validate(ledger.meta('buyHoldPolicy:' + strategy))
    if symbol not in value['symbols']:
        raise BinanceClientError('symbol outside buy-and-hold policy', blocker='POLICY_OPERATION')
    return value


def budget(ledger, strategy):
    value = validate(ledger.meta('buyHoldPolicy:' + strategy))
    spent = sum((Decimal(r[0]) for r in ledger.db.execute(
        "SELECT quote_value FROM events WHERE strategy=? AND category='buyback_spend'", (strategy,))), Decimal(0))
    reserved = sum((number(i['reserved'], zero=True) for i in ledger.outstanding()
                    if i['strategy'] == strategy and i['payload'].get('operation') == 'buy_ioc'), Decimal(0))
    cap = number(value['maxCumulativeQuote'])
    return {'capQuote': str(cap), 'spentQuoteIncludingFeeValue': str(spent),
            'reservedQuote': str(reserved), 'remainingQuote': str(max(Decimal(0), cap-spent-reserved))}


def check(ledger, strategy, preview):
    authorize(ledger, strategy, preview['params']['symbol'])
    if number(preview['reservation']) > number(budget(ledger, strategy)['remainingQuote'], zero=True):
        raise BinanceClientError('cumulative buyback allowance exhausted', blocker='BUYBACK_ALLOWANCE')


def quantity(ledger, strategy, asset, location='SPOT'):
    row = ledger.db.execute('SELECT quantity FROM buy_hold WHERE strategy=? AND asset=? AND location=?',
                            (strategy, asset, location)).fetchone()
    return Decimal(row[0]) if row else Decimal(0)


def change(ledger, strategy, asset, location, delta):
    qty = quantity(ledger, strategy, asset, location) + delta
    if qty < 0:
        raise BinanceClientError('buy-and-hold attribution deficit', blocker='OWNERSHIP_DEFICIT')
    ledger.db.execute('INSERT INTO buy_hold VALUES(?,?,?,?) ON CONFLICT(strategy,asset,location) DO UPDATE SET quantity=excluded.quantity',
                      (strategy, asset, location, str(qty)))


def move(ledger, strategy, asset, source, destination, amount):
    attributed = min(quantity(ledger, strategy, asset, source), amount)
    change(ledger, strategy, asset, source, -attributed)
    change(ledger, strategy, asset, destination, attributed)
