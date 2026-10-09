"""No production credentials/store or network in the test process."""
import os
import tempfile
import socket
import pytest

_store = tempfile.TemporaryDirectory(prefix='binance-mcp-tests-')
os.environ['BINANCE_LEDGER_PATH'] = _store.name + '/server.sqlite'
os.environ['BINANCE_STRATEGIES'] = '{}'
for name in ('BINANCE_API_KEY', 'BINANCE_API_SECRET', 'BINANCE_PRIVATE_KEY_PATH', 'BINANCE_PRIVATE_KEY_PASSPHRASE'):
    os.environ.pop(name, None)
os.environ['BINANCE_TRADING_ENABLED'] = 'false'

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('network disabled in isolated regression harness')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)
