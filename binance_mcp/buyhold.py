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


def move(ledger, strategy, asset, source, destination, amount, *, intent_id=None, exact=False):
    if exact:
        if intent_id is None or reserved(ledger, intent_id) != amount or quantity(ledger, strategy, asset, source) < amount:
            raise BinanceClientError('exact acquired transfer attribution missing', blocker='ACQUIRED_ASSET_OWNERSHIP')
        attributed = amount
        ledger.db.execute('UPDATE hold_reservations SET quantity=? WHERE intent=?', ('0', intent_id))
    else:
        attributed = min(quantity(ledger, strategy, asset, source), amount)
    change(ledger, strategy, asset, source, -attributed)
    change(ledger, strategy, asset, destination, attributed)


def reserved(ledger, intent_id):
    row = ledger.db.execute('SELECT quantity FROM hold_reservations WHERE intent=?', (intent_id,)).fetchone()
    return Decimal(row[0]) if row else Decimal(0)


def available(ledger, strategy, asset, location='SPOT'):
    committed = sum((reserved(ledger, i['id']) for i in ledger.outstanding()
                     if i['strategy'] == strategy and i['asset'] == asset and i['location'] == location), Decimal(0))
    return quantity(ledger, strategy, asset, location) - committed


def reserve(ledger, intent_id, strategy, asset, location, amount, *, acquired=False):
    """Inside Ledger.begin: acquired transfers are exact; exits use non-hold units first."""
    held_available = available(ledger, strategy, asset, location)
    if acquired:
        if held_available < amount:
            raise BinanceClientError('acquired subset is already committed or unavailable', blocker='ACQUIRED_ASSET_OWNERSHIP')
        attributed = amount
    else:
        nonhold_available = ledger.available(strategy, asset, location) - held_available
        attributed = max(Decimal(0), amount - max(Decimal(0), nonhold_available))
        if attributed > held_available:
            raise BinanceClientError('exit exceeds attributed ownership', blocker='OWNERSHIP_DEFICIT')
    ledger.db.execute('INSERT INTO hold_reservations VALUES(?,?)', (intent_id, str(attributed)))


def consume(ledger, intent, amount):
    """Decrease the exact hold portion of a sale; other committed holds stay untouched."""
    attribution = reserved(ledger, intent['id'])
    if not attribution and not quantity(ledger, intent['strategy'], intent['asset'], intent['location']):
        return
    nonhold = max(Decimal(0), number(intent['reserved'], zero=True) - attribution)
    used = max(Decimal(0), amount - nonhold)
    if used > attribution:
        # Do not consume another intent's hold to pay an unexpected sell-side charge.
        raise BinanceClientError('sale exceeds reserved hold attribution', blocker='OWNERSHIP_DEFICIT')
    change(ledger, intent['strategy'], intent['asset'], intent['location'], -used)
    ledger.db.execute('UPDATE hold_reservations SET quantity=? WHERE intent=?', (str(attribution-used), intent['id']))
