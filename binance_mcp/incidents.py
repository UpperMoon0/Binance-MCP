"""Durable operational incidents and narrowly approved cancel/attach recovery."""
import hashlib
import json
import time
from decimal import Decimal, ROUND_DOWN
from .client import BinanceClientError
from .ledger import number


def validate_recovery(value):
    required = {'version', 'approved', 'action', 'maxDelayMs'}
    if not isinstance(value, dict) or set(value) != required or value['version'] != 1 or value['approved'] is not True or value['action'] not in ('operator_only', 'cancel_attach') or type(value['maxDelayMs']) is not int or not 1000 <= value['maxDelayMs'] <= 90000:
        raise BinanceClientError('explicit bounded owner recovery policy required', blocker='RECOVERY_POLICY_REQUIRED')


def record(ledger, intent, code, exposure, evidence):
    now = int(time.time() * 1000)
    with ledger.transaction():
        old = ledger.db.execute("SELECT id,detail FROM incidents WHERE source=? AND state!='RESOLVED' ORDER BY created DESC LIMIT 1", (intent['id'],)).fetchone()
        policy = ledger.meta('recoveryPolicy:' + intent['strategy'])
        if old:
            key, detail = old['id'], json.loads(old['detail'])
        else:
            serial = ledger.db.execute('SELECT COUNT(*) FROM incidents WHERE source=?', (intent['id'],)).fetchone()[0]
            key = hashlib.sha256((intent['id'] + ':' + str(serial)).encode()).hexdigest()[:24]
            detail = {'firstDetectedAt': now, 'deadline': now + (policy['maxDelayMs'] if policy else 1000),
                      'cancelIntentId': 'recovery-cancel-' + key, 'exitIntentId': 'recovery-exit-' + key,
                      'delivery': 'PENDING', 'acknowledgedAt': None, 'deliveryAttempts': 0}
            ledger.db.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?)', (key, intent['id'], intent['strategy'], 'DETECTED', now, '{}'))
        detail.update(code=code, exposureQuantity=str(exposure), evidence=evidence, lastDetectedAt=now)
        ledger.db.execute('UPDATE incidents SET detail=? WHERE id=?', (json.dumps(detail), key))
        ledger.pause(ledger.meta('pause') or ('missing or inactive protection; incident ' + key))
        return key


def rows(ledger, *, active=False):
    query = 'SELECT * FROM incidents' + (" WHERE state!='RESOLVED'" if active else '')
    return [{**dict(r), 'detail': json.loads(r['detail'])} for r in ledger.db.execute(query)]


def update(ledger, incident, state, **changes):
    # Merge current delivery state: independent delivery/recovery cannot overwrite it.
    detail = json.loads(ledger.db.execute('SELECT detail FROM incidents WHERE id=?', (incident['id'],)).fetchone()[0])
    detail.update(changes)
    ledger.db.execute('UPDATE incidents SET state=?,detail=? WHERE id=?', (state, json.dumps(detail), incident['id']))


async def deliver(ledger, sink):
    for incident in rows(ledger):
        d = incident['detail']
        if d['delivery'] == 'DELIVERED' or sink is None:
            continue
        try:
            # Sink must deduplicate by incident id across an interrupted acknowledgment.
            acknowledged = await sink({'incidentId': incident['id'], 'code': d['code'],
                                       'strategyId': incident['strategy'], 'deadline': d['deadline'],
                                       'state': incident['state'], 'sourceIntentId': incident['source'],
                                       'exposureQuantity': d['exposureQuantity'],
                                       'recoveryIntentIds': {'cancel': d['cancelIntentId'], 'exit': d['exitIntentId']}})
        except Exception:
            acknowledged = False
        update(ledger, incident, incident['state'], delivery='DELIVERED' if acknowledged is True else 'PENDING',
               deliveryAttempts=d['deliveryAttempts'] + 1, deliveredAt=int(time.time()*1000) if acknowledged is True else None)


async def replacement_preflight(svc, source):
    """Prove current replacement feasibility without releasing real protection.

    Release only this intent's local reservation in a disposable ledger. Exchange
    free capital and order limits receive no speculative cancellation credit;
    uncertain/locked capital therefore requires operator intervention.
    """
    from .ledger import Ledger
    from .execution import ExecutionService
    _, _, quantity = svc.protection_evidence(source, source['result']['exchange'], source['result']['orders'])
    lot = number(next(f for f in source['result']['preview']['filters'] if f['filterType'] == 'LOT_SIZE')['stepSize'])
    quantity = (quantity / lot).to_integral_value(rounding=ROUND_DOWN) * lot
    if not quantity:
        raise BinanceClientError('no replaceable exposure', blocker='RESIDUAL_DUST_OPERATOR_REQUIRED')
    clone = Ledger(':memory:')
    try:
        svc.ledger.db.backup(clone.db)
        clone.update(source['id'], 'RESOLVED', result=source['result'])
        candidate = ExecutionService(svc.client, clone, svc.snapshots.symbols)
        await candidate.preview(source['strategy'], 'oco', {**source['payload']['params'], 'quantity': str(quantity)})
    finally:
        clone.db.close()


async def recover(svc):
    ledger = svc.ledger
    for incident in rows(ledger, active=True):
        source = ledger.get(incident['source'])
        repaired = ledger.get(incident['detail']['exitIntentId'])
        if repaired and (repaired['result'] or {}).get('protected') and not (repaired['result'] or {}).get('protectionUncertain'):
            update(ledger, incident, 'RESOLVED', resolution='OWNED_OCO_VERIFIED')
            continue
        if source and source['state'] not in ('RESOLVED', 'REJECTED'):
            try:
                async with svc.lock:
                    source = await svc.reconcile(source['id'])
            except Exception:
                update(ledger, incident, 'WAITING_EVIDENCE', blocker='RECOVERY_EVIDENCE_REQUIRED')
                if int(time.time()*1000) >= incident['detail']['deadline']:
                    update(ledger, incident, 'ESCALATED', deadlineExceeded=True)
                continue
        if source and (source['result'] or {}).get('protected') and not (source['result'] or {}).get('protectionUncertain'):
            update(ledger, incident, 'RESOLVED', resolution='ORIGINAL_PROTECTION_VERIFIED')
            continue
        if int(time.time()*1000) >= incident['detail']['deadline']:
            update(ledger, incident, 'ESCALATED', deadlineExceeded=True)
            continue
        policy = ledger.meta('recoveryPolicy:' + incident['strategy'])
        if policy is None or policy['action'] == 'operator_only':
            if int(time.time()*1000) >= incident['detail']['deadline']:
                update(ledger, incident, 'ESCALATED', blocker='OPERATOR_RESPONSE_REQUIRED')
            continue
        # Never turn an ambiguous cancel/attach into a new intent on restart.
        d = incident['detail']
        source = ledger.get(incident['source'])
        if source is None or source['payload']['operation'] not in ('oco', 'otoco'):
            update(ledger, incident, 'BLOCKED', blocker='RECOVERY_CONTRACT_UNSUPPORTED')
            continue
        try:
            if (source['result'] or {}).get('protected') and not (source['result'] or {}).get('protectionUncertain'):
                update(ledger, incident, 'RESOLVED', resolution='ORIGINAL_PROTECTION_VERIFIED')
                continue
            # A failed feasibility check must leave the original list untouched.
            # Repeat execution preflight after cancellation for fills/market races.
            if ledger.get(d['cancelIntentId']) is None:
                async with svc.lock:
                    await replacement_preflight(svc, source)
                if int(time.time()*1000) >= d['deadline']:
                    update(ledger, incident, 'ESCALATED', deadlineExceeded=True)
                    continue
            update(ledger, incident, 'CANCELING')
            cancel = await svc.execute(incident['strategy'], d['cancelIntentId'], 'cancel', {'targetIntentId': source['id']})
            if cancel['state'] != 'RESOLVED':
                update(ledger, incident, 'WAITING_CANCEL', blocker='OUTCOME_UNKNOWN')
                continue
            source = ledger.get(source['id'])
            preview = source['result']['preview']
            _, _, quantity = svc.protection_evidence(source, source['result']['exchange'], source['result']['orders'])
            lot = number(next(f for f in preview['filters'] if f['filterType'] == 'LOT_SIZE')['stepSize'])
            quantity = (quantity / lot).to_integral_value(rounding=ROUND_DOWN) * lot
            if not quantity:
                update(ledger, incident, 'BLOCKED', blocker='RESIDUAL_DUST_OPERATOR_REQUIRED')
                continue
            p = {**source['payload']['params'], 'quantity': str(quantity)}
            # Original stop/take remain the approved protection contract. A crossed
            # trigger or below-minimum residual is refused by the fresh preview.
            update(ledger, incident, 'ATTACHING', exposureQuantity=str(quantity))
            result = await svc.execute(incident['strategy'], d['exitIntentId'], 'oco', p)
            if (result['result'] or {}).get('protected') and not (result['result'] or {}).get('protectionUncertain'):
                update(ledger, incident, 'RESOLVED', resolution='OWNED_OCO_VERIFIED')
            else:
                update(ledger, incident, 'WAITING_EXIT', blocker='PROTECTION_UNVERIFIED')
        except BinanceClientError as exc:
            update(ledger, incident, 'BLOCKED', blocker=exc.blocker or 'RECOVERY_EVIDENCE_REQUIRED')
        if int(time.time()*1000) >= d['deadline']:
            latest = next(i for i in rows(ledger) if i['id'] == incident['id'])
            if latest['state'] != 'RESOLVED':
                update(ledger, latest, 'ESCALATED', deadlineExceeded=True)


def acknowledge(ledger, incident_id, evidence):
    """Owner/offline acknowledgment only. Does not clear pauses or resolve exposure."""
    if not evidence:
        raise BinanceClientError('operator acknowledgment evidence required')
    incident = next((i for i in rows(ledger) if i['id'] == incident_id), None)
    if incident is None:
        raise BinanceClientError('incident not found')
    update(ledger, incident, incident['state'], acknowledgedAt=int(time.time()*1000), acknowledgmentEvidence=evidence)


def configured_sink():
    """Optional owner-configured idempotent HTTPS incident receiver; never tested live."""
    import os
    from urllib.parse import urlsplit
    import httpx
    url = os.getenv('BINANCE_INCIDENT_WEBHOOK_URL', '')
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('incident receiver requires an owner-configured HTTPS URL')
    token = os.getenv('BINANCE_INCIDENT_WEBHOOK_TOKEN', '')
    async def sink(incident):
        headers = {'Idempotency-Key': incident['incidentId']}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.post(url, headers=headers, json=incident)
        return response.status_code == 200 and response.json().get('acknowledged') == incident['incidentId']
    return sink
