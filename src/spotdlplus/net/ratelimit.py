'''
ratelimit.py - shared pacing for every worker

Concurrency isn't a speed knob. Past a low number it's just a way to get your
IP banned, and then the tool feels randomly broken to whoever is using it. The
bucket is shared, so 8 workers get one budget and take turns.

Short waits happen inline. Long ones get raised as RateLimited so the Engine
can defer the track, because a worker sitting out 60 seconds is doing nothing.
'''

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..core.errors import RateLimited

#: The cutoff between waiting a rate-limit out here and handing the track back
#: to be rescheduled
MAX_BLOCK_S = 2.0


class TokenBucket:
    '''
    A normal token bucket. Thread-safe, and fair enough that nobody starves since
    waiters re-check under the lock after every timeout.
    '''

    def __init__(
        self,
        rate_per_s: float,
        burst: int,
        *,
        name: str = '?',
        max_block_s: float = MAX_BLOCK_S,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if rate_per_s <= 0 or burst < 1:
            raise ValueError('rate_per_s must be > 0 and burst >= 1')
        self._rate = float(rate_per_s)
        self._burst = int(burst)
        self._name = name
        self._max_block = float(max_block_s)
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()
        self._cond = threading.Condition(threading.Lock())

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def burst(self) -> int:
        return self._burst

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._last = now

    def acquire(self, n: int = 1) -> None:
        '''
        Spends `n` tokens and waits if it has to, but never for long. Past MAX_BLOCK_S
        it raises RateLimited with the real wait attached and the Engine defers the
        track instead. Nothing sleeps.
        '''
        if n > self._burst:
            raise ValueError(f'cannot take {n} from a bucket of burst {self._burst}')
        with self._cond:
            # The cap is on the WHOLE call, not on one iteration of teh loop. A
            # thread whose tokens keep getting taken by others must not be able
            # to accumulate several sub-cap waits into a long one.
            deadline = self._clock() + self._max_block
            while True:
                self._refill_locked()
                if self._tokens >= n:
                    self._tokens -= n
                    return

                wait = (n - self._tokens) / self._rate
                now = self._clock()
                if now + wait > deadline:
                    raise self._owed(wait)

                # Bounded adn interruptible. Not a sleep: a shutdown can wake
                # us.
                self._cond.wait(timeout=wait)

                if self._clock() <= now:
                    # Time did not move. Either the clock is frozen (a test) or
                    # the platform's monotonic source is coarser than our
                    # waits. Either way, waiting again cannot help. Hand it to
                    # the Engine.
                    raise self._owed(wait)

    def _owed(self, wait: float) -> RateLimited:
        return RateLimited(
            f'{self._name}: {wait:.1f}s of pacing owed, deferring rather than blocking',
            context={'host': self._name, 'owed_s': wait},
            retry_after=wait,
        )

    def peek(self) -> float:
        '''Tokens available right now. For tests and for `doctor`.'''
        with self._cond:
            self._refill_locked()
            return self._tokens


#: Per-host budgets. MusicBrainz's rate limit isn't a suggestion, their terms
#: say one request per second and they will block you for ignoring it. We pace
#: at 0.9 instead of 1.0 because their server throttles under its own load too,
#: and sitting exactly on the line means any jitter puts you over. They 429'd a
#: run that paced at precisely 1/s.
DEFAULT_BUDGETS: dict[str, tuple[float, int]] = {
    'api.spotify.com': (10.0, 10),
    'accounts.spotify.com': (5.0, 5),
    'musicbrainz.org': (0.9, 1),
    'api.deezer.com': (5.0, 5),
    'music.youtube.com': (3.0, 3),
    'www.youtube.com': (3.0, 3),
    # spotify's image CDN. left to the (2,2) fallback, cover fetches got rate-
    # limit-deferred during busy album runs and art was silently skipped. The
    # direct cause of a library with scattered missing covers.
    'i.scdn.co': (10.0, 10),
    # acoustid's published guideline for application traffic
    'api.acoustid.org': (3.0, 3),
    # lrclib is a free community service. being gentle is the whole rent
    'lrclib.net': (4.0, 4),
}

#: Anything we have not thought about gets a conservative budget, not a free pass.
FALLBACK_BUDGET: tuple[float, int] = (2.0, 2)


class HostLimiter:
    '''One bucket per host, created on demand, shared by every worker.'''

    def __init__(
        self,
        budgets: dict[str, tuple[float, int]] | None = None,
        *,
        fallback: tuple[float, int] = FALLBACK_BUDGET,
        max_block_s: float = MAX_BLOCK_S,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._budgets = dict(budgets if budgets is not None else DEFAULT_BUDGETS)
        self._fallback = fallback
        self._max_block = max_block_s
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def bucket(self, host: str) -> TokenBucket:
        with self._lock:
            b = self._buckets.get(host)
            if b is None:
                rate, burst = self._budgets.get(host, self._fallback)
                b = TokenBucket(rate, burst, name=host,
                                max_block_s=self._max_block, clock=self._clock)
                self._buckets[host] = b
            return b

    def acquire(self, host: str, n: int = 1) -> None:
        self.bucket(host).acquire(n)
