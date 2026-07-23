'''
breaker.py - circuit breaker

It counts failures, opens, and cools down. It raises nothing and knows no error
types, because what a trip means depends on the caller. Mechanism here, policy
wherever it gets used.
'''

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = 'closed'        # all good, traffic flows
    OPEN = 'open'            # tripped. Refuse, until the cooldown elapses
    HALF_OPEN = 'half_open'  # cautiously letting one request through to see


class CircuitBreaker:
    '''
    Trips after `threshold` failures inside a rolling `window_s`. Anything older
    than the window is forgotten, since three failures in an hour is noise and
    three in two minutes is a pattern.
    '''

    def __init__(
        self,
        *,
        threshold: int = 3,
        window_s: float = 120.0,
        cooldown_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if threshold < 1:
            raise ValueError('threshold must be >= 1')
        self._threshold = threshold
        self._window = window_s
        self._cooldown = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: list[float] = []
        self._state = BreakerState.CLOSED
        self._opened_at = 0.0

    @property
    def state(self) -> BreakerState:
        with self._lock:
            self._maybe_reopen_locked()
            return self._state

    def _prune_locked(self) -> None:
        cutoff = self._clock() - self._window
        self._failures = [t for t in self._failures if t >= cutoff]

    def _maybe_reopen_locked(self) -> None:
        if self._state is BreakerState.OPEN and self._clock() - self._opened_at >= self._cooldown:
            self._state = BreakerState.HALF_OPEN

    def allow(self) -> bool:
        '''May the caller proceed? Flips OPEN to HALF_OPEN once the cooldown is up.'''
        with self._lock:
            self._maybe_reopen_locked()
            return self._state is not BreakerState.OPEN

    def record_failure(self) -> BreakerState:
        with self._lock:
            now = self._clock()
            if self._state is BreakerState.HALF_OPEN:
                # the probe failed. straight back to open, cooldown restarts.
                self._state = BreakerState.OPEN
                self._opened_at = now
                return self._state
            self._failures.append(now)
            self._prune_locked()
            if len(self._failures) >= self._threshold:
                self._state = BreakerState.OPEN
                self._opened_at = now
            return self._state

    def record_success(self) -> None:
        with self._lock:
            self._failures.clear()
            self._state = BreakerState.CLOSED
