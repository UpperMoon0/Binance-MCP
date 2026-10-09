"""Offline owner migration: passive plans, exact revisions, backed-up atomic apply."""
import json
import time
from decimal import Decimal
from .accounting import apply_audit
from .client import BinanceClientError
from .ledger import Ledger, number

COMPATIBILITY = {'legacy_limit_sell_gtc': 'IMPORTABLE_WITH_COST_BASIS', 'legacy_ioc_buyback': 'UNSUPPORTED',
                 'non_quote_earn': 'UNSUPPORTED', 'conversion': 'UNSUPPORTED', 'auto_subscription': 'OWNER_AUDIT_REQUIRED'}


def revision(ledger):
    data = {table: [tuple(r) for r in ledger.db.execute('SELECT * FROM ' + table + ' ORDER BY 1')]
            for table in ('strategies', 'balances', 'intents', 'events')}
    data['policies'] = [tuple(r) for r in ledger.db.execute("SELECT * FROM meta WHERE key LIKE '%Policy:%' OR key='expectedTotals' OR key LIKE 'audit:%' ORDER BY key")]
    return Ledger.fingerprint(data)


def locations(snapshot):
    result = {}
    for b in snapshot['coverage']['spot']['data']['balances']:
        if not b['asset'].startswith('LD'):
            key = (b['asset'], 'SPOT')
            if key in result:
                raise BinanceClientError('duplicate Spot evidence')
            result[key] = number(b['free'], zero=True) + number(b['locked'], zero=True)
    for b in snapshot['coverage']['earn']['data']:
        key = (b['asset'], 'EARN:' + b['productId'])
        result[key] = result.get(key, Decimal(0)) + number(b['totalAmount'], zero=True)
    for b in snapshot['coverage']['dual']['data']:
        if b['status'] == 'PURCHASE_SUCCESS':
            key = (b['investCoin'], 'DI:' + str(b['positionId']))
            if key in result:
                raise BinanceClientError('duplicate DI evidence')
            result[key] = number(b['depositAmount'])
    return result


def check_snapshot(snapshot):
    now = int(time.time()*1000)
    if snapshot.get('complete') is not True or not 0 <= now - snapshot.get('startedAt', 0) <= 90000:
        raise BinanceClientError('complete fresh scoped snapshot required for migration')


def _apply(ledger, snapshot, audit):
    """All functions called here operate on the clone for planning."""
    key = 'migration:' + audit['id']
    digest = Ledger.fingerprint(audit)
    old = ledger.meta(key)
    if old:
        if old != digest:
            raise BinanceClientError('migration id reused with different evidence')
        return {'alreadyApplied': True}
    if audit.get('ledgerRevision') != revision(ledger):
        raise BinanceClientError('migration ledger revision changed')
    from .diagnostics import source_identity
    if not audit.get('evidence') or audit.get('sourceRevision') != source_identity():
        raise BinanceClientError('owner evidence and source revision required')
    if ledger.meta('expectedTotals') is None:
        if audit.get('accountDeltas'):
            raise BinanceClientError('initial migration cannot invent historical account deltas')
        ledger.set_meta('expectedTotals', snapshot['underlyingTotals'])
    apply_audit(ledger, audit, snapshot['underlyingTotals'])
    observed_orders = {(o['symbol'], str(o['orderId'])): o for o in snapshot['coverage']['orders']['data']}
    for item in audit.get('legacyOrders', []):
        identity = (item['symbol'], str(item['orderId']))
        order = observed_orders.get(identity)
        if not order or order.get('clientOrderId') != item.get('clientOrderId'):
            raise BinanceClientError('legacy identity not established by fresh exchange evidence')
        if any(order.get(k) != item.get(k) for k in ('side', 'type', 'timeInForce', 'price', 'origQty', 'executedQty')):
            raise BinanceClientError('legacy contract or fill evidence contradicts import')
        if order.get('orderListId', -1) != -1 or order.get('status') not in ('NEW', 'PARTIALLY_FILLED') or order['side'] != 'SELL' or order['type'] != 'LIMIT' or order['timeInForce'] != 'GTC':
            raise BinanceClientError('unsupported legacy order; owner compatibility decision required')
        historical = snapshot.get('legacyEvidence', {}).get(item['symbol'] + ':' + str(item['orderId']))
        if historical is None or historical['symbol']['baseAsset'] != item['baseAsset'] or historical['symbol']['quoteAsset'] != item['quoteAsset']:
            raise BinanceClientError('fresh legacy symbol and trade evidence required')
        trades = historical['trades']
        if len({str(t['id']) for t in trades}) != len(trades) or any(t.get('isBuyer') is not False or str(t.get('orderId')) != str(item['orderId']) for t in trades) or sum((number(t['qty']) for t in trades), Decimal(0)) != number(order['executedQty'], zero=True):
            raise BinanceClientError('legacy historical fills incomplete or contradictory')
        strategy = item['strategyId']
        cfg = ledger.strategy(strategy)
        if cfg['mode'] != 'live' or item['quoteAsset'] != cfg['quote'] or not item.get('costBasisEvidence') or not item.get('historicalFillEvidence'):
            raise BinanceClientError('legacy strategy/cost basis/history attribution required')
        cost_row = ledger.db.execute("SELECT cost FROM balances WHERE strategy=? AND asset=? AND location='SPOT'", (strategy, item['baseAsset'])).fetchone()
        if cost_row is None or Decimal(cost_row[0]) <= 0:
            raise BinanceClientError('remaining legacy cost basis missing')
        remaining = number(order['origQty']) - number(order['executedQty'], zero=True)
        if remaining <= 0:
            raise BinanceClientError('invalid legacy remaining commitment')
        intent_id = 'legacy:' + item['symbol'] + ':' + str(item['orderId'])
        payload = {'operation': 'legacy_order', 'params': {'symbol': item['symbol'], 'quantity': item['origQty'], 'price': item['price'], 'side': 'SELL'},
                   'legacyEvidence': item}
        preview = {'base': item['baseAsset'], 'quote': item['quoteAsset'], 'params': payload['params'], 'feeRateBound': historical['feeCompatibility']['feeRateBound']}
        imported, fresh = ledger.begin(intent_id, strategy, payload, item['baseAsset'], 'SPOT', str(remaining), recovery=True,
            prepared={'preview': preview, 'legacyBaselineExecuted': item['executedQty'], 'legacyBaselineTrades': {str(t['id']): t for t in trades}, 'legacyClientOrderId': item['clientOrderId'], 'legacyOrderId': item['orderId'], 'importEvidence': audit['evidence']})
        if not fresh:
            raise BinanceClientError('legacy identity already imported; duplicate ownership refused')
        ledger.update(intent_id, 'OPEN', result=imported['result'])
    owned_locations = {}
    for row in ledger.db.execute("SELECT b.asset,b.location,b.quantity FROM balances b JOIN strategies s ON b.strategy=s.id WHERE s.mode='live'"):
        key_location = (row['asset'], 'SPOT' if row['location'] == 'PROFIT_RESERVE' else row['location'])
        owned_locations[key_location] = owned_locations.get(key_location, Decimal(0)) + Decimal(row['quantity'])
    actual = locations(snapshot)
    if any(q > actual.get(k, Decimal(0)) for k,q in owned_locations.items()):
        raise BinanceClientError('migration ownership exceeds exact exchange location')
    # No external/unknown order is silently ignored or granted experiment authority.
    known = {r['result'].get('legacyClientOrderId') for r in ledger.outstanding() if r['payload'].get('operation') == 'legacy_order'}
    from .execution import client_id
    for row in ledger.db.execute('SELECT id FROM intents'):
        known.update(client_id(row[0], suffix) for suffix in ('', 'entry', 'take', 'stop'))
    unmanaged = [o for o in observed_orders.values() if o['clientOrderId'] not in known]
    if unmanaged:
        raise BinanceClientError('unclassified legacy orders remain; migration fails closed')
    ledger.set_meta(key, digest)
    return {'alreadyApplied': False, 'importedOrders': len(audit.get('legacyOrders', []))}


def plan(ledger, snapshot, audit=None):
    check_snapshot(snapshot)
    result = {'ledgerRevision': revision(ledger), 'snapshotObservedAt': snapshot['observedAt'],
              'holdings': snapshot['coverage'], 'receiptRepresentations': snapshot['receiptRepresentations'],
              'earmarks': [dict(r) for r in ledger.db.execute('SELECT * FROM balances')],
              'outstanding': ledger.outstanding(), 'compatibility': COMPATIBILITY,
              'excludedCoverage': snapshot.get('excludedCoverage', []), 'canApply': False}
    if audit is not None:
        clone = Ledger(':memory:')
        try:
            ledger.db.backup(clone.db)
            with clone.transaction():
                result['candidate'] = _apply(clone, snapshot, audit)
            result['canApply'] = True
        except BinanceClientError as exc:
            result['blocker'] = str(exc)
        finally:
            clone.db.close()
    return result


def apply(ledger, snapshot, audit, backup_path):
    check_snapshot(snapshot)
    import sqlite3
    from pathlib import Path
    if not backup_path or Path(backup_path).exists():
        raise BinanceClientError('new durable owner backup path required')
    import os
    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    backup = sqlite3.connect(backup_path)
    try:
        ledger.db.backup(backup)
    finally:
        backup.close()
    with ledger.transaction():
        return _apply(ledger, snapshot, audit)


async def observe(svc, audit):
    """Bounded signed reads, no ledger writes; collect independent import evidence."""
    snapshot = await svc.snapshots.portfolio()
    evidence = {}
    for item in audit.get('legacyOrders', []):
        info = await svc.symbol(item['symbol'])
        from .fees import inspect
        commission = await svc.client.signed_get('spot', '/api/v3/account/commission', {'symbol': item['symbol']})
        fee_compatibility = inspect(commission)
        if not fee_compatibility['compatible']:
            raise BinanceClientError('legacy fee mode unsupported', blocker='UNSUPPORTED_FEE_ASSET')
        trades = []
        cursor = 0
        for _ in range(100):
            batch = await svc.client.signed_get('spot', '/api/v3/myTrades', {'symbol': item['symbol'], 'orderId': item['orderId'], 'fromId': cursor, 'limit': 1000})
            trades.extend(t for t in batch if str(t['orderId']) == str(item['orderId']))
            if len(batch) < 1000:
                break
            cursor = int(batch[-1]['id']) + 1
        else:
            raise BinanceClientError('legacy fill history incomplete')
        evidence[item['symbol'] + ':' + str(item['orderId'])] = {'symbol': info, 'trades': trades, 'feeCompatibility': fee_compatibility}
    snapshot['legacyEvidence'] = evidence
    return snapshot


async def run(path, *, apply_changes=False, backup_path=None):
    import os
    from pathlib import Path
    from .client import BinanceClient
    from .execution import ExecutionService
    audit = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    path_ledger = os.getenv('BINANCE_LEDGER_PATH', 'data/strategy.sqlite')
    ledger = Ledger(path_ledger) if apply_changes else Ledger.open_readonly(path_ledger)
    client = BinanceClient()
    try:
        svc = ExecutionService(client, ledger, os.getenv('BINANCE_APPROVED_SYMBOLS', 'BTCUSDT,ETHUSDT').split(','))
        snapshot = await observe(svc, audit)
        if apply_changes:
            value = apply(ledger, snapshot, audit, backup_path)
        else:
            value = plan(ledger, snapshot, audit)
        print(json.dumps(value))
    finally:
        await client.close()
        ledger.db.close()


if __name__ == '__main__':
    import argparse
    import asyncio
    parser = argparse.ArgumentParser(description='Owner migration. Default: passive plan. Stop service before --apply.')
    parser.add_argument('evidence_json')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup')
    args = parser.parse_args()
    asyncio.run(run(args.evidence_json, apply_changes=args.apply, backup_path=args.backup))
