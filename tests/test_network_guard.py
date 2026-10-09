import socket
import pytest
from conftest import guarded_connect


@pytest.mark.parametrize('method', ['connect', 'connect_ex'])
def test_local_socketpair_connection_allowed(method):
    # Exercise the TCP loopback connection used by Windows asyncio socketpair.
    with socket.socket() as listener, socket.socket() as client:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        result = getattr(client, method)(listener.getsockname())
        if method == 'connect_ex':
            assert result == 0
        with listener.accept()[0] as peer:
            client.sendall(b'ipc')
            assert peer.recv(3) == b'ipc'


@pytest.mark.parametrize('address', [('8.8.8.8', 443), ('api.binance.com', 443), ('localhost', 443), ('::ffff:8.8.8.8', 443)])
def test_guard_denies_non_loopback_without_connecting(address):
    with socket.socket() as client:
        for method in ('connect', 'connect_ex'):
            with pytest.raises(AssertionError, match='network disabled'):
                getattr(client, method)(address)


@pytest.mark.parametrize('host', ['127.0.0.1', '::1'])
def test_guard_allows_numeric_ipv4_and_ipv6_loopback(host):
    calls = []
    with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET) as client:
        guarded_connect(lambda sock, address: calls.append(address))(client, (host, 1))
    assert calls == [(host, 1)]
