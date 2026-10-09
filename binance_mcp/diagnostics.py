"""Safe structured MCP errors and passive readiness; never provisions or reconciles."""
from functools import wraps
from pathlib import Path
import hashlib
import json
import time
import uuid
from mcp.types import CallToolResult, TextContent
from .client import BinanceClientError
from . import incidents, policy


def error_result(exc):
    blocker = exc.blocker or ('RATE_LIMITED' if exc.status in (418, 429) or exc.retry_after is not None else 'PERMISSIONS' if exc.status in (401, 403) else None)
    if blocker is None:
        text = str(exc).lower()
        blocker = next((code for fragment, code in [
            ('strategy must be provisioned', 'STRATEGY_UNPROVISIONED'), ('intent not found', 'INTENT_NOT_FOUND'),
            ('third-asset', 'UNSUPPORTED_FEE_ASSET'), ('permission', 'PERMISSIONS'),
            ('stale', 'OBSERVATION_STALE'), ('incomplete', 'COVERAGE_INCOMPLETE'),
            ('rate limit', 'RATE_LIMITED'), ('cooldown', 'RATE_LIMITED'), ('paused', 'EXECUTION_PAUSED')]
                        if fragment in text), 'EXECUTION_UNCERTAIN' if exc.outcome_unknown else 'VALIDATION_FAILED')
    # Never forward exchange-supplied message text, URLs, balances or headers.
    detail = {'blocker': blocker, 'message': 'Binance capability blocked: ' + blocker,
              'status': exc.status, 'exchangeCode': exc.code, 'retryAfter': exc.retry_after,
              'outcomeUnknown': exc.outcome_unknown, 'automaticWriteRetryAllowed': False,
              'correlationId': uuid.uuid4().hex}
    value = {'ok': False, 'error': detail}
    return CallToolResult(content=[TextContent(type='text', text=json.dumps(value))], structured_content=value, is_error=True)


def rpc_guard(fn):
    @wraps(fn)
    async def guarded(*args, **kwargs):
        try:
            value = await fn(*args, **kwargs)
            if isinstance(value, CallToolResult):
                return value
            # Explicit wire results avoid losing structuredContent under wrappers.
            structured = value if isinstance(value, dict) else {'data': value}
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(value))], structured_content=structured)
        except BinanceClientError as exc:
            return error_result(exc)
        except (ValueError, KeyError, TypeError):
            return error_result(BinanceClientError('invalid request or evidence', blocker='INVALID_EVIDENCE'))
        except Exception:
            return error_result(BinanceClientError('capability unavailable', blocker='CAPABILITY_UNAVAILABLE'))
    import inspect
    signature = inspect.signature(fn, eval_str=True).replace(return_annotation=CallToolResult)
    guarded.__signature__ = signature
    guarded.__annotations__ = {name: p.annotation for name, p in signature.parameters.items()}
    guarded.__annotations__['return'] = CallToolResult
    return guarded


def source_identity():
    source = hashlib.sha256()
    for p in sorted(Path(__file__).parent.glob('*.py')):
        source.update(p.name.encode()); source.update(p.read_bytes())
    return source.hexdigest()


async def readiness(svc, monitor, tools, *, check_exchange=False):
    ledger = svc.ledger
    now = int(time.time() * 1000)
    auth = svc.client.auth_status()
    schemas = [t.model_dump(mode='json', by_alias=True) for t in tools]
    strategies = []
    global_blockers = []
    if not auth.get('account_access_ready'):
        global_blockers.append('ACCOUNT_ACCESS_UNAVAILABLE')
    if not auth.get('trading_enabled'):
        global_blockers.append('TRADING_DISABLED')
    if ledger.meta('pause'):
        global_blockers.append('EXECUTION_PAUSED')
    def fresh(key):
        stamp = ledger.meta(key)
        return bool(stamp and 0 <= now-stamp <= 90000)
    if not fresh('reconciledAt') or not fresh('protectionAt'):
        global_blockers.append('MONITOR_EVIDENCE_STALE')
    if not ledger.meta('streamConnected'):
        global_blockers.append('STREAM_DISCONNECTED')
    if monitor.incident_sink is None:
        global_blockers.append('INCIDENT_CHANNEL_UNCONFIGURED')
    active = incidents.rows(ledger, active=True)
    if active:
        global_blockers.append('OPERATOR_INCIDENT_UNRESOLVED')
    for row in ledger.db.execute('SELECT id,mode FROM strategies'):
        cfg = dict(row)
        blockers = list(global_blockers)
        risk = ledger.meta('riskPolicy:' + row['id'])
        if risk is None:
            blockers.append('POLICY_REQUIRED')
        recovery = ledger.meta('recoveryPolicy:' + row['id'])
        if recovery is None:
            blockers.append('RECOVERY_POLICY_REQUIRED')
        if ledger.meta('riskPause:' + row['id']):
            blockers.append('LOSS_PAUSED')
        if any(i['state'] in ('PREPARED', 'OUTCOME_UNKNOWN') for i in ledger.outstanding() if i['strategy'] == row['id']):
            blockers.append('EXECUTION_UNCERTAIN')
        # Commission/permission checks are symbol-specific, only a preview can
        # functionally verify them immediately before submitting that contract.
        blockers.append('FRESH_TRADE_PREFLIGHT_REQUIRED')
        strategies.append({**cfg, 'riskPolicy': risk, 'recoveryPolicy': recovery,
                           'protectedLive': {'ready': False, 'blockers': blockers},
                           'paper': {'configured': row['mode'] == 'paper', 'functionallyChecked': False}})
    observation = None
    if check_exchange:
        observation = {'portfolio': await svc.snapshots.portfolio()}
        if svc.snapshots.symbols:
            try:
                observation['market'] = await svc.snapshots.market([sorted(svc.snapshots.symbols)[0]])
            except BinanceClientError as exc:
                observation['marketError'] = error_result(exc).structured_content['error']
    from importlib.metadata import version
    import platform
    return {'observedAt': now, 'identity': {'sourceSha256': source_identity(), 'sourceDigestBasis': 'CURRENT_INSTALLED_PACKAGE_FILES',
            'serviceVersion': version('binance-mcp'), 'mcpVersion': version('mcp'), 'pythonVersion': platform.python_version(),
            'schemaSha256': hashlib.sha256(json.dumps(schemas, sort_keys=True).encode()).hexdigest()},
            'capabilities': [t.name for t in tools], 'approvedSymbols': sorted(svc.snapshots.symbols),
            'verification': {'implemented': True, 'runtimePackageFilesIdentified': True, 'loadedCodeAttestation': False,
                             'connectorExposure': 'VERIFY_CLIENT_DISCOVERY', 'financialWriteSmokeTest': False},
            'strategies': strategies, 'provisioned': bool(strategies),
            'monitor': {k: ledger.meta(k) for k in ('heartbeatAt', 'monitorAt', 'reconciledAt', 'protectionAt', 'streamConnected', 'lastUserEventAt')},
            'incidents': active, 'reservations': [{'intentId': i['id'], 'strategyId': i['strategy'], 'state': i['state'], 'asset': i['asset'], 'quantity': i['reserved']} for i in ledger.outstanding()],
            'research': {'functionallyChecked': check_exchange, 'accountScopeComplete': bool(observation and observation['portfolio']['complete']),
                         'observations': observation},
            'paper': {'configured': any(s['mode'] == 'paper' for s in strategies), 'functionallyChecked': False},
            'protectedLive': {'ready': False, 'blockers': global_blockers + ([] if strategies else ['STRATEGY_UNPROVISIONED']) + ['FRESH_TRADE_PREFLIGHT_REQUIRED']}}
