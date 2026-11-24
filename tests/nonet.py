"""The tests run without the network, wherever they run: any connection or name lookup that is not
this machine fails at once, as it would in a sandbox. A test that talks to YouTube by mistake fails."""
import socket

_connect, _connect_ex, _getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo


def _local(host) -> bool:
    return isinstance(host, str) and (host.startswith('127.') or host in ('::1', 'localhost', ''))


def _guarded(real):
    def connect(self, address):
        if self.family == getattr(socket, 'AF_UNIX', None) or _local(address[0] if isinstance(address, tuple) else address):
            return real(self, address)
        raise OSError(101, 'the tests run without the network')
    return connect


def _getaddrinfo_local(host, *args, **kwargs):
    if host is None or _local(host if isinstance(host, str) else ''):
        return _getaddrinfo(host, *args, **kwargs)
    raise socket.gaierror(-2, 'the tests run without the network')


socket.socket.connect = _guarded(_connect)
socket.socket.connect_ex = _guarded(_connect_ex)
socket.getaddrinfo = _getaddrinfo_local
