"""Test isolation: forbid outbound network access while allowing loopback and local IPC.

Why not "patch socket.connect to always fail"? On Windows, `socket.socketpair()` (used by every asyncio event loop at start-up for its
self-pipe) is emulated with a TCP listener on 127.0.0.1 plus a connect to it. A blanket ban therefore fails inside `asyncio.run()`
before any application code runs - a false positive. This guard instead decides per destination:

  allowed   loopback addresses (127.0.0.0/8, ::1, "localhost") and AF_UNIX paths (local IPC, not network)
  rejected  everything else: any other IP literal, any hostname lookup (DNS), UDP sendto to a non-loopback address

The check runs BEFORE the real call, so a rejected attempt never sends a packet. Every rejected attempt is also recorded in
`guard.attempts`; `assert_clean()` (called at fixture teardown) fails the test even if the code under test swallowed the exception.
"""
from __future__ import annotations

import ipaddress
import socket
import sys
from typing import Optional

import pytest


class ProhibitedNetworkAccess(AssertionError):
    pass


def is_loopback_host(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host is None:
        return True                                  # getaddrinfo(None, port): wildcard/loopback lookup, no DNS
    host = str(host).split("%", 1)[0].strip("[]").lower()
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False                                 # a real hostname: resolving it would need DNS
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or bool(mapped and mapped.is_loopback)


def emulated_windows_socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
    """CPython's pure-Python Windows fallback for socket.socketpair(): loopback TCP listener + connect. Installed on every platform
    so Linux/macOS runs exercise the same event-loop start-up path Windows does."""
    lsock = socket.socket(family, type, proto)
    try:
        lsock.bind(("127.0.0.1", 0))
        lsock.listen()
        addr, port = lsock.getsockname()[:2]
        csock = socket.socket(family, type, proto)
        try:
            csock.setblocking(False)
            try:
                csock.connect((addr, port))
            except (BlockingIOError, InterruptedError):
                pass
            csock.setblocking(True)
            ssock, _ = lsock.accept()
        except Exception:
            csock.close()
            raise
    finally:
        lsock.close()
    return ssock, csock


class NetworkGuard:
    def __init__(self) -> None:
        self.attempts: list[str] = []

    # -- decision
    def _check(self, what: str, target) -> None:
        host = target[0] if isinstance(target, tuple) and target else target
        if is_loopback_host(host):
            return
        msg = f"prohibited network access: {what} -> {target!r}"
        self.attempts.append(msg)
        raise ProhibitedNetworkAccess(msg)

    def assert_clean(self) -> None:
        if self.attempts:
            raise AssertionError("prohibited network access was attempted (it may have been swallowed by the code under test):\n  "
                                 + "\n  ".join(self.attempts))

    # -- installation
    def install(self, monkeypatch: pytest.MonkeyPatch, *, emulate_windows_socketpair: bool = True) -> "NetworkGuard":
        guard = self
        orig_connect, orig_connect_ex, orig_sendto = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto
        orig_getaddrinfo = socket.getaddrinfo

        def is_unix(sock) -> bool:
            return getattr(socket, "AF_UNIX", None) is not None and sock.family == socket.AF_UNIX

        def connect(self, address):
            if not is_unix(self):
                guard._check("socket.connect", address)
            return orig_connect(self, address)

        def connect_ex(self, address):
            if not is_unix(self):
                guard._check("socket.connect_ex", address)
            return orig_connect_ex(self, address)

        def sendto(self, data, *args):
            if not is_unix(self):
                guard._check("socket.sendto", args[-1])
            return orig_sendto(self, data, *args)

        def getaddrinfo(host, *args, **kwargs):
            guard._check("socket.getaddrinfo", host)
            return orig_getaddrinfo(host, *args, **kwargs)

        monkeypatch.setattr(socket.socket, "connect", connect)
        monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
        monkeypatch.setattr(socket.socket, "sendto", sendto)
        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        for name in ("gethostbyname", "gethostbyname_ex"):
            real = getattr(socket, name)
            monkeypatch.setattr(socket, name, (lambda r, n: (lambda host, *a, **k: (guard._check(f"socket.{n}", host), r(host, *a, **k))[1]))(real, name))
        if emulate_windows_socketpair and sys.platform != "win32":
            monkeypatch.setattr(socket, "socketpair", emulated_windows_socketpair)
        return self


@pytest.fixture
def netguard(monkeypatch):
    """Forbid outbound network; allow loopback + local IPC; fail the test at teardown if anything prohibited was even attempted."""
    g = NetworkGuard().install(monkeypatch)
    yield g
    g.assert_clean()


@pytest.fixture
def no_network(netguard):
    """Back-compat name used by the discovery dry-run tests."""
    return netguard
