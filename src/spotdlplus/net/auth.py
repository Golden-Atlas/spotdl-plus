'''
auth.py - token refresh and the 401 loop fix

The bug: 8 workers share a token, it expires, all 8 get a 401, all 8 try to
refresh. Seven of them mint tokens that the eighth immediately orphans, so they
retry with dead tokens, get another 401, and refresh again. It never stops.
This is what killed old spotdl.

Two things fix it. Single flight means a 401 only marks the token stale if you
were holding the current one, so only one refresh actually happens. Loop
detection means if three brand new tokens get rejected in a row we stop and
tell you to check your system clock, becuase that is usually what it is.
'''

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..core.errors import AuthRefreshLoop
from ..core.events import AuthRefreshed, EventBus, NullBus

#: Returns (token_value, expires_in_seconds). Raises a typed error on refusal.
TokenFetcher = Callable[[], tuple[str, float]]


@dataclass(frozen=True, slots=True)
class Token:
    '''
    An access token plus the two facts that make it shareable across threads.
    `generation` turns a thundering herd into one refresh: a 401 report names which
    token was rejected, so 'this is dead' differs from 'you hold an old one'.
    '''

    value: str
    expires_at: float   # on the injected clock, not wall time
    minted_at: float
    generation: int

    def fresh(self, now: float, grace_s: float) -> bool:
        '''Young enough that a rejection means something is genuinely wrong.'''
        return (now - self.minted_at) <= grace_s


class TokenProvider:
    '''
    Hands out a valid token, refreshes at most once no matter how many threads ask,
    and refuses to spin when refreshing plainly isn't working. Every wait is on a
    lock, never a clock.
    '''

    def __init__(
        self,
        fetch: TokenFetcher,
        *,
        name: str = 'spotify',
        bus: EventBus | None = None,
        run_id: str = '',
        skew_s: float = 60.0,
        fresh_grace_s: float = 10.0,
        loop_threshold: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._name = name
        self._bus = bus or NullBus()
        self._run_id = run_id
        self._skew = skew_s              # refresh early. never race the expiry
        self._grace = fresh_grace_s
        self._loop_threshold = loop_threshold
        self._clock = clock

        self._lock = threading.RLock()
        self._token: Token | None = None
        self._stale = False
        self._generation = 0
        self._refreshes = 0
        #: Generations that were rejected while still newborn. Three distinct
        #: ones means three fresh tokens in a row were refused: taht is the
        #: loop.
        self._stillborn: set[int] = set()
        #: Once true, always true. A latch, not a derived value.
        #: `report_success` clears the stillborn set, and a tripped provider
        #: must not un-trip because some unrelated request happened to work.
        self._looping = False

    # -- introspection, for tests and `doctor` -------------------------------

    @property
    def refreshes(self) -> int:
        return self._refreshes

    @property
    def generation(self) -> int:
        return self._generation

    # -- the request seam ----------------------------------------------------

    def extra_headers(self) -> dict[str, str]:
        '''
        Headers a request carries beyond the bearer. Empty for a normal app
        token, because that path only ever needed Authorization. The web-player
        auth overrides this to add its client-token, because Spotify clamps a
        bare bearer into a 20-hour wall and only lets the paired signature
        through at a normal rate. The http client merges whatever this returns.
        '''
        return {}

    # -- the hot path --------------------------------------------------------

    def token(self) -> Token:
        '''
        A usable token, lock-free when one exists. The double check under the lock is
        the single-flight guarantee: seven threads find the eighth already refreshed.
        '''
        if self._looping:
            raise self._loop_error()

        tok = self._token
        if self._usable(tok):
            return tok   # type: ignore[return-value]

        with self._lock:
            tok = self._token
            if self._usable(tok):
                return tok   # type: ignore[return-value]
            return self._refresh_locked()

    def _usable(self, tok: Token | None) -> bool:
        if tok is None or self._stale:
            return False
        return tok.expires_at - self._skew > self._clock()

    def _refresh_locked(self) -> Token:
        self._trip_if_looping()

        value, expires_in = self._fetch()   # may raise CredentialsRejected, NetTimeout, ...
        now = self._clock()
        self._generation += 1
        self._refreshes += 1
        self._stale = False
        self._token = Token(
            value=value,
            expires_at=now + float(expires_in),
            minted_at=now,
            generation=self._generation,
        )
        self._bus.emit(AuthRefreshed(
            run_id=self._run_id, provider=self._name, expires_in_s=float(expires_in),
        ))
        return self._token

    def _trip_if_looping(self) -> None:
        if self._looping or len(self._stillborn) >= self._loop_threshold:
            self._looping = True
            raise self._loop_error()

    def _loop_error(self) -> AuthRefreshLoop:
        return AuthRefreshLoop(
            f'{self._name}: {len(self._stillborn)} freshly minted tokens were rejected '
            f'within {self._grace:.0f}s of being issued. Refreshing is not the problem',
            context={
                'provider': self._name,
                'refreshes': self._refreshes,
                'stillborn_generations': sorted(self._stillborn),
            },
        )

    # -- what callers report back --------------------------------------------

    def report_unauthorized(self, tok: Token) -> None:
        '''
                A request using `tok` came back 401. Does nothing if it isn't the current
                generation. Someone already rotated for you, so retry rather than refresh.
                That one check is the difference between one refresh and eight.

        '''
        with self._lock:
            cur = self._token
            if cur is None or tok.generation != cur.generation:
                return

            # Mark it dead FIRST. `_trip_if_looping` raises, and a provider that
            # trips while still believing its token is good will happily hand
            # taht dead token to the next caller from the fast path.
            self._stale = True

            if cur.fresh(self._clock(), self._grace):
                # A token the server issued seconds ago, and it will not honour it.
                # Refreshing produces another one just like it.
                self._stillborn.add(cur.generation)
                self._trip_if_looping()

    def report_success(self, tok: Token) -> None:
        '''
        Auth is working. Forget the history. A loop is about *consecutive*
        failure.
        '''
        with self._lock:
            if self._token is not None and tok.generation == self._token.generation:
                self._stillborn.clear()

    def invalidate(self) -> None:
        '''Force the next `token()` to refresh. Used by `doctor` and by tests.'''
        with self._lock:
            self._stale = True
