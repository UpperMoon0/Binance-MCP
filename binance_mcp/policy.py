"""Owner-provisioned immutable policy. Research/caller parameters cannot approve it."""
from decimal import Decimal
import time

from .client import BinanceClientError
from .ledger import number

OPERATIONS = {'otoco', 'order', 'oco', 'cancel', 'earn_subscribe', 'earn_redeem', 'dual_subscribe', 'earn_to_dual'}
REQUIRED = {'version', 'approved', 'operations', 'assets', 'maxPositions', 'maxPositionQuote',
            'maxPlannedDownsideQuote', 'lossPauseQuote', 'executionAllowanceBps', 'valuationMaxAgeMs'}


def validate(policy):
    if not isinstance(policy, dict) or set(policy) != REQUIRED or policy['version'] != 1 or policy['approved'] is not True:
        raise BinanceClientError('complete explicitly approved risk policy version 1 required', blocker='POLICY_REQUIRED')
    for key, choices in [('operations', OPERATIONS), ('assets', None)]:
        values = policy[key]
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values) or len(values) != len(set(values)) or (choices is not None and not set(values) <= choices):
            raise BinanceClientError('invalid policy ' + key, blocker='POLICY_INVALID')
    for key in ('maxPositions', 'valuationMaxAgeMs'):
        if type(policy[key]) is not int or policy[key] < 1:
            raise BinanceClientError('invalid policy ' + key, blocker='POLICY_INVALID')
    if policy['valuationMaxAgeMs'] > 90000:
        raise BinanceClientError('valuation maximum age exceeds operational bound', blocker='POLICY_INVALID')
    for key in ('maxPositionQuote', 'maxPlannedDownsideQuote', 'lossPauseQuote'):
        number(policy[key])
    allowance = number(policy['executionAllowanceBps'], zero=True)
    if allowance > 2000:
        raise BinanceClientError('invalid execution allowance', blocker='POLICY_INVALID')
    return policy


def authorize(ledger, strategy, operation, assets):
    policy = ledger.meta('riskPolicy:' + strategy)
    if policy is None:
        if ledger.strategy(strategy)['mode'] == 'live' and operation not in ('cancel', 'oco', 'order'):
            raise BinanceClientError('live execution requires owner-approved risk policy', blocker='POLICY_REQUIRED')
        return None  # owned exits remain available in an unprovisioned upgrade
    validate(policy)
    if operation not in policy['operations'] or not set(assets) <= set(policy['assets']):
        raise BinanceClientError('operation or asset is outside strategy policy', blocker='POLICY_OPERATION')
    if operation not in ('cancel', 'oco', 'order') and ledger.meta('riskPause:' + strategy):
        raise BinanceClientError('strategy loss policy is paused', blocker='LOSS_PAUSED')
    return policy


def entry_check(ledger, strategy, preview, *, persist_loss=False):
    """Call inside the reservation transaction; inputs are freshly fetched observations."""
    policy = authorize(ledger, strategy, preview['operation'], [preview['base'], preview['quote']])
    if not policy or preview['operation'] in ('order', 'oco', 'cancel', 'earn_redeem'):
        return
    risk = preview.get('riskEvidence', {})
    now = int(time.time() * 1000)
    if not risk.get('observedAt') or not 0 <= now - risk['observedAt'] <= policy['valuationMaxAgeMs']:
        raise BinanceClientError('fresh strategy valuation required', blocker='VALUATION_STALE')
    report = ledger.report(strategy, risk.get('prices'))
    if report['missingValuations']:
        raise BinanceClientError('strategy valuation incomplete', blocker='VALUATION_INCOMPLETE')
    realized = Decimal(report['resultsByCategory'].get('realized', '0')) + Decimal(report['resultsByCategory'].get('loss', '0'))
    # Cost bases and realized P&L already include charged commissions. Reserve
    # conservative exit costs on unrealized assets; deposits/rewards are excluded.
    marked = sum((number(b['quantity'], zero=True) * number(risk['prices'][b['asset']])
                  for b in report['balances'] if b['asset'] != report['strategy']['quote'] and number(b['quantity'], zero=True)), Decimal(0))
    fee = number(preview['feeRateBound'], zero=True)
    allowance = number(policy['executionAllowanceBps'], zero=True) / 10000
    pnl = realized + Decimal(report['unrealizedQuote']) - marked * (fee + allowance)
    if -pnl >= number(policy['lossPauseQuote']):
        if persist_loss:
            ledger.set_meta('riskPause:' + strategy, {'code': 'LOSS_PAUSED', 'observedAt': now, 'pnlQuote': str(pnl)})
        raise BinanceClientError('strategy loss threshold reached', blocker='LOSS_PAUSED')
    positions = [i for i in ledger.outstanding() if i['strategy'] == strategy and i['payload'].get('operation') in ('otoco', 'dual_subscribe', 'earn_to_dual')]
    # Filled holdings without their source intent (legacy/cancelled partial fills)
    # occupy slots too; linked siblings never count as independent positions.
    import json
    represented = {}
    source_ids = {i['id'] for i in positions}
    for row in ledger.db.execute("SELECT detail FROM events WHERE strategy=? AND category='fill'", (strategy,)):
        trade = json.loads(row[0])
        if trade.get('intentId') not in source_ids:
            continue
        intent = next(i for i in positions if i['id'] == trade['intentId'])
        base = (intent['result'] or {}).get('preview', {}).get('base')
        if not base:
            continue
        qty = number(trade['qty'])
        fee = number(trade['commission'], zero=True) if trade['commissionAsset'] == base else Decimal(0)
        represented[base] = represented.get(base, Decimal(0)) + (qty - fee if trade['isBuyer'] else -qty-fee)
    holdings = {}
    for b in report['balances']:
        if b['asset'] != report['strategy']['quote']:
            holdings[b['asset']] = holdings.get(b['asset'], Decimal(0)) + number(b['quantity'], zero=True)
    orphaned = set()
    for asset, quantity in holdings.items():
        step = Decimal(0)
        for i in ledger.db.execute('SELECT id FROM intents WHERE strategy=?', (strategy,)):
            saved = (ledger.get(i[0])['result'] or {}).get('preview', {})
            if saved.get('base') == asset:
                lot = next((f for f in saved.get('filters', []) if f['filterType'] == 'LOT_SIZE'), None)
                if lot:
                    step = number(lot['stepSize']) if not step else min(step, number(lot['stepSize']))
        residual = quantity - represented.get(asset, Decimal(0))
        if residual > 0 and (not step or residual >= step):
            orphaned.add(asset)
    locked_di = sum(1 for b in report['balances'] if b['location'].startswith('DI:') and number(b['quantity'], zero=True))
    if preview['operation'] != 'earn_subscribe' and len(positions) + len(orphaned) + locked_di >= policy['maxPositions']:
        raise BinanceClientError('maximum concurrent entry commitments reached', blocker='POSITION_COUNT')
    value = number(preview['reservation'])
    if value > number(policy['maxPositionQuote']):
        raise BinanceClientError('maximum position value exceeded', blocker='POSITION_VALUE')
    if preview['operation'] != 'otoco':
        if preview['operation'] in ('dual_subscribe', 'earn_to_dual') and value > number(policy['maxPlannedDownsideQuote']):
            raise BinanceClientError('DI entire principal exceeds planned downside policy', blocker='PLANNED_DOWNSIDE')
        return
    quantity = number(preview['params']['quantity'])
    price = number(preview['params']['price'])
    stop = number(preview['params']['stopPrice'])
    # Two fee charges + execution allowance; deliberately conservative for base
    # fees and rounding. Stops cannot guarantee this bound in a gap.
    downside = quantity * (price - stop + (price + stop) * fee + price * allowance)
    if downside > number(policy['maxPlannedDownsideQuote']):
        raise BinanceClientError('planned downside exceeds policy', blocker='PLANNED_DOWNSIDE')
