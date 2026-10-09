"""No production credentials/store or network in the test process."""
import os
import tempfile
import socket
import ipaddress
import pytest

_store = tempfile.TemporaryDirectory(prefix='binance-mcp-tests-')
os.environ['BINANCE_LEDGER_PATH'] = _store.name + '/server.sqlite'
os.environ['BINANCE_STRATEGIES'] = '{}'
for name in ('BINANCE_API_KEY', 'BINANCE_API_SECRET', 'BINANCE_PRIVATE_KEY_PATH', 'BINANCE_PRIVATE_KEY_PASSPHRASE'):
    os.environ.pop(name, None)
os.environ['BINANCE_TRADING_ENABLED'] = 'false'

def guarded_connect(original):
    def connect(sock, address):
        # asyncio's Windows socketpair connects to a numeric loopback address.
        # Never resolve hostnames here: outbound hosts remain denied.
        local = sock.family == getattr(socket, 'AF_UNIX', object())
        if sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple):
            try:
                local = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                local = False
        if not local:
            raise AssertionError('network disabled in isolated regression harness')
        return original(sock, address)
    return connect


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    for name in ('connect', 'connect_ex'):
        monkeypatch.setattr(socket.socket, name, guarded_connect(getattr(socket.socket, name)))
