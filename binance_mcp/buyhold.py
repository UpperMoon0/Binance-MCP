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
    fee_held = sum((Decimal(r[0]) for r in ledger.db.execute(
        "SELECT f.held FROM sell_fee_reservations f JOIN intents i ON f.intent=i.id "
        "WHERE f.strategy=? AND f.asset=? AND f.location=? AND i.state NOT IN ('RESOLVED','REJECTED')",
        (strategy, asset, location))), Decimal(0))
    return quantity(ledger, strategy, asset, location) - committed - fee_held


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


def reserve_sell_fee(ledger, intent, rate):
    amount = number(intent['reserved'], zero=True) * rate
    strategy, asset, location = intent['strategy'], intent['asset'], intent['location']
    free = ledger.available(strategy, asset, location)
    if free < amount:
        raise BinanceClientError('insufficient separate sell commission reserve', blocker='FEE_RESERVE')
    held = max(Decimal(0), amount - max(Decimal(0), free - available(ledger, strategy, asset, location)))
    ledger.db.execute('INSERT INTO sell_fee_reservations VALUES(?,?,?,?,?,?,?)',
                     (intent['id'], strategy, asset, location, str(amount), str(held), str(rate)))


def consume(ledger, intent, principal, base_fee=Decimal(0)):
    """Principal and commission use separate units; never borrow another reservation."""
    strategy, asset, location = intent['strategy'], intent['asset'], intent['location']
    attribution = reserved(ledger, intent['id'])
    if not attribution and not quantity(ledger, strategy, asset, location) and not ledger.db.execute(
            'SELECT 1 FROM sell_fee_reservations WHERE intent=?', (intent['id'],)).fetchone():
        return
    nonhold = max(Decimal(0), number(intent['reserved'], zero=True) - attribution)
    used = max(Decimal(0), principal - nonhold)
    if used > attribution:
        raise BinanceClientError('sale exceeds reserved hold attribution', blocker='OWNERSHIP_DEFICIT')
    fee = ledger.db.execute('SELECT * FROM sell_fee_reservations WHERE intent=?', (intent['id'],)).fetchone()
    if fee:
        fee_quantity, held_fee = Decimal(fee['quantity']), Decimal(fee['held'])
        released = min(fee_quantity, principal * Decimal(fee['rate']))
        if base_fee > released:
            raise BinanceClientError('base commission exceeds reserved fee bound', blocker='FEE_RESERVE')
        nonhold_fee = fee_quantity-held_fee
        used_held_fee = max(Decimal(0), base_fee-nonhold_fee)
        unused = released-base_fee
        remaining_nonhold = nonhold_fee-min(base_fee,nonhold_fee)
        released_held_fee = max(Decimal(0), unused-remaining_nonhold)
        ledger.db.execute('UPDATE sell_fee_reservations SET quantity=?,held=? WHERE intent=?',
                          (str(fee_quantity-released),str(held_fee-used_held_fee-released_held_fee),intent['id']))
    else:
        # Older persisted exits may have no separate fee bucket. Exact observed fees
        # can consume only currently uncommitted units, never another intent's backing.
        free = ledger.available(strategy, asset, location)
        if base_fee > free:
            raise BinanceClientError('unreserved base commission would consume another commitment', blocker='FEE_RESERVE')
        used_held_fee = max(Decimal(0), base_fee-max(Decimal(0),free-available(ledger,strategy,asset,location)))
    change(ledger, strategy, asset, location, -used-used_held_fee)
    ledger.db.execute('UPDATE hold_reservations SET quantity=? WHERE intent=?', (str(attribution-used),intent['id']))
