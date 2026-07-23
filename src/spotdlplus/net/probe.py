'''
probe.py - telling a dead network from a dead host

These two failures show up looking identical and want opposite responses. If
your wifi is down we park the run and nothing is anyone's fault. If one host is
down we fail that host and carry on. Guess wrong either way and you either burn
attempts against a dead network or park the whole run over one bad server.
'''

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Sequence

#: Anycast resolvers, reachable from essentially anywhere with a route out. Two
#: ports, because some networks block 443 adn some block 53.
DEFAULT_TARGETS: tuple[tuple[str, int], ...] = (
    ('1.1.1.1', 443),
    ('8.8.8.8', 53),
    ('9.9.9.9', 443),
)

#: Opens a TCP connection or raises OSError. Injected so tests never touch a NIC.
Connector = Callable[[tuple[str, int], float], None]


def _tcp_connect(address: tuple[str, int], timeout_s: float) -> None:
    with socket.create_connection(address, timeout=timeout_s):
        pass


class OnlineProbe:
    '''
    Answers whether we have a network, cached for a few seconds. A run that just
    lost its connection asks once per in-flight track, and hammering three
    resolvers forty times to answer the same question is its own kind of rude.
    '''

    def __init__(
        self,
        *,
        targets: Sequence[tuple[str, int]] = DEFAULT_TARGETS,
        timeout_s: float = 1.5,
        ttl_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        connect: Connector = _tcp_connect,
    ) -> None:
        self._targets = tuple(targets)
        self._timeout = timeout_s
        self._ttl = ttl_s
        self._clock = clock
        self._connect = connect
        self._lock = threading.Lock()
        self._answer: bool | None = None
        self._checked_at = 0.0

    def online(self) -> bool:
        '''True if any target answers. Cached for `ttl_s`.'''
        with self._lock:
            now = self._clock()
            if self._answer is not None and (now - self._checked_at) < self._ttl:
                return self._answer

            result = False
            for target in self._targets:
                try:
                    self._connect(target, self._timeout)
                except OSError:
                    continue
                result = True
                break

            self._answer = result
            self._checked_at = now
            return result

    def invalidate(self) -> None:
        '''Drop the cache. `doctor` wants a fresh answer, not a five-second-old one.'''
        with self._lock:
            self._answer = None
