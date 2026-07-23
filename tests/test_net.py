'''
test_net.py - the hostile world, simulated.

The centrepiece is `test_eight_workers_produce_exactly_one_refresh`. That single
assertion is the difference between this tool and the one it replaces.
'''

from __future__ import annotations

import threading
import time

import pytest

from spotdlplus.core.errors import AuthRefreshLoop, CredentialsRejected, RateLimited
from spotdlplus.core.events import AuthRefreshed, EventBus
from spotdlplus.net.auth import TokenProvider
from spotdlplus.net.breaker import BreakerState, CircuitBreaker
from spotdlplus.net.ratelimit import MAX_BLOCK_S, HostLimiter, TokenBucket


class Clock:
    '''A clock that only moves when we say so. Time-dependent tests must not flake.'''

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, d: float) -> None:
        self.t += d


# ----------------------------------------------------------------------------
# token bucket
# ----------------------------------------------------------------------------

def test_burst_is_spendable_at_once():
    clk = Clock()
    b = TokenBucket(1.0, 3, clock=clk)
    for _ in range(3):
        b.acquire()
    assert b.peek() == pytest.approx(0.0)


def test_tokens_trickle_back_at_the_stated_rate():
    clk = Clock()
    b = TokenBucket(2.0, 10, clock=clk)
    b.acquire(10)
    clk.advance(3.0)
    assert b.peek() == pytest.approx(6.0)      # 3s * 2/s
    clk.advance(100.0)
    assert b.peek() == pytest.approx(10.0)     # never past burst


def test_an_expensive_wait_is_deferred_not_slept():
    '''
    ARCHITECTURE.md §6a. Ten seconds of debt must not become ten seconds of a
    held thread. It becomes a RateLimited the Engine can defer on.
    '''
    clk = Clock()
    b = TokenBucket(0.1, 1, name='api.spotify.com', clock=clk)
    b.acquire()

    started = time.monotonic()
    with pytest.raises(RateLimited) as exc:
        b.acquire()
    assert time.monotonic() - started < 0.5, 'it blocked instead of deferring'

    assert exc.value.retry_after == pytest.approx(10.0)
    assert exc.value.context['host'] == 'api.spotify.com'
    assert exc.value.retry.value == 'after'


def test_a_cheap_wait_is_paced_inline():
    '''Sub-second pacing stays in the thread. Deferring 20ms would be absurd.'''
    b = TokenBucket(50.0, 1)   # real clock: one token per 20ms
    b.acquire()

    started = time.monotonic()
    b.acquire()
    elapsed = time.monotonic() - started
    assert 0.005 < elapsed < 1.0, f'expected ~20ms of pacing, got {elapsed:.3f}s'


def test_the_blocking_bound_is_the_one_the_contract_names():
    assert MAX_BLOCK_S == 2.0


def test_you_cannot_ask_for_more_than_the_bucket_can_ever_hold():
    with pytest.raises(ValueError):
        TokenBucket(1.0, 2).acquire(3)


def test_a_frozen_clock_defers_instead_of_spinning_forever():
    '''
    Regression. `acquire` used to loop on a condition variable waiting for tokens
    that a non-advancing clock would never mint, and hung the whole suite. If time
    is not moving, waiting cannot help. hand it back to the Engine.
    '''
    clk = Clock()
    b = TokenBucket(100.0, 1, max_block_s=1.0, clock=clk)   # 10ms per token
    b.acquire()

    started = time.monotonic()
    with pytest.raises(RateLimited):
        b.acquire()
    assert time.monotonic() - started < 1.0, 'it spun'


def test_the_cap_bounds_the_whole_call_not_one_iteration():
    '''
    A thread whose tokens keep getting stolen must not accumulate several sub-cap
    waits into one long one. The deadline is set once, on entry.
    '''
    clk = Clock()
    b = TokenBucket(1.0, 1, max_block_s=2.0, clock=clk)
    b.acquire()
    clk.advance(0.5)                    # half a token back: 0.5s still owed
    with pytest.raises(RateLimited):
        b.acquire(1)                    # frozen thereafter -> deferred, not stacked


def test_workers_share_one_budget_per_host():
    lim = HostLimiter(clock=Clock())
    assert lim.bucket('api.spotify.com') is lim.bucket('api.spotify.com')
    assert lim.bucket('api.spotify.com') is not lim.bucket('musicbrainz.org')


def test_musicbrainz_is_paced_under_the_line_their_terms_draw():
    '''
    Their terms say one request per second. We pace at 0.9/s, not 1.0: their
    server also throttles under its own load, and sitting exactly on the line
    means any jitter puts you over. Measured: they 429'd a run pacing at
    precisely 1/s.
    '''
    mb = HostLimiter(clock=Clock()).bucket('musicbrainz.org')
    assert mb.rate < 1.0, 'exactly 1/s is how we got throttled in production'
    assert (mb.rate, mb.burst) == (0.9, 1)


def test_a_second_musicbrainz_call_owes_just_over_a_second():
    clk = Clock()
    lim = HostLimiter(max_block_s=0.0, clock=clk)   # defer everything, block nothing
    lim.acquire('musicbrainz.org')

    with pytest.raises(RateLimited) as exc:
        lim.acquire('musicbrainz.org')
    assert exc.value.retry_after == pytest.approx(1.0 / 0.9)

    clk.advance(1.2)
    lim.acquire('musicbrainz.org')                  # ...and now it is paid


def test_unknown_hosts_get_a_conservative_budget_not_a_free_pass():
    lim = HostLimiter(max_block_s=0.0, clock=Clock())
    b = lim.bucket('some.new.api')
    assert (b.rate, b.burst) == (2.0, 2)
    b.acquire(2)
    with pytest.raises(RateLimited):
        b.acquire(2)


# ----------------------------------------------------------------------------
# circuit breaker
# ----------------------------------------------------------------------------

def test_it_trips_only_on_a_pattern_not_on_noise():
    clk = Clock()
    cb = CircuitBreaker(threshold=3, window_s=120.0, clock=clk)

    for _ in range(2):
        cb.record_failure()
    assert cb.state is BreakerState.CLOSED

    clk.advance(200.0)              # the old failures age out of the window
    cb.record_failure()
    assert cb.state is BreakerState.CLOSED, 'three failures over an hour is not a pattern'

    cb.record_failure()
    cb.record_failure()
    assert cb.state is BreakerState.OPEN, 'three inside the window is'


def test_an_open_breaker_refuses_until_the_cooldown():
    clk = Clock()
    cb = CircuitBreaker(threshold=1, cooldown_s=30.0, clock=clk)
    cb.record_failure()
    assert not cb.allow()

    clk.advance(29.0)
    assert not cb.allow()

    clk.advance(2.0)
    assert cb.allow() and cb.state is BreakerState.HALF_OPEN


def test_a_successful_probe_closes_it_and_a_failed_probe_reopens_it():
    clk = Clock()
    cb = CircuitBreaker(threshold=1, cooldown_s=10.0, clock=clk)
    cb.record_failure()
    clk.advance(11.0)
    assert cb.allow()                       # half open

    cb.record_failure()                     # the probe failed
    assert cb.state is BreakerState.OPEN
    assert not cb.allow(), 'the cooldown restarts. we do not hammer'

    clk.advance(11.0)
    assert cb.allow()
    cb.record_success()
    assert cb.state is BreakerState.CLOSED


# ----------------------------------------------------------------------------
# auth: single flight
# ----------------------------------------------------------------------------

def _counting_fetcher(expires_in: float = 3600.0, delay: float = 0.0):
    calls: list[int] = []
    lock = threading.Lock()

    def fetch():
        with lock:
            calls.append(1)
            n = len(calls)
        if delay:
            time.sleep(delay)
        return (f'token-{n}', expires_in)

    return fetch, calls


def test_a_valid_token_is_handed_out_without_refetching():
    fetch, calls = _counting_fetcher()
    p = TokenProvider(fetch, clock=Clock())
    a, b = p.token(), p.token()
    assert a is b and len(calls) == 1


def test_it_refreshes_early_rather_than_racing_the_expiry():
    clk = Clock()
    fetch, calls = _counting_fetcher(expires_in=100.0)
    p = TokenProvider(fetch, skew_s=60.0, clock=clk)
    p.token()

    clk.advance(39.0)
    p.token()
    assert len(calls) == 1, 'still comfortably valid'

    clk.advance(2.0)          # now 41s in: 59s of life left, inside the 60s skew
    p.token()
    assert len(calls) == 2, 'refresh before the cliff, not at it'


def test_eight_workers_produce_exactly_one_refresh():
    '''
    The bug this whole file exists for.

    Eight threads discover an expired token at the same instant. Without single
    flight you get eight refreshes, seven orphaned tokens, and a 401 storm that
    never ends. With it you get one refresh and eight identical tokens.
    '''
    fetch, calls = _counting_fetcher(delay=0.05)   # hold it open so they all pile up
    p = TokenProvider(fetch)

    got: list = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        got.append(p.token())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1, f'{len(calls)} refreshes, the stampede is back'
    assert len(got) == 8
    assert {t.generation for t in got} == {1}
    assert {t.value for t in got} == {'token-1'}


def test_a_401_on_a_token_someone_already_rotated_does_not_refresh_again():
    '''The straggler. It holds generation 1. The world has moved to generation 2.'''
    fetch, calls = _counting_fetcher()
    p = TokenProvider(fetch, clock=Clock())

    old = p.token()
    p.invalidate()
    new = p.token()
    assert len(calls) == 2 and new.generation == 2

    p.report_unauthorized(old)              # the straggler reports in
    assert p.token() is new, 'it must retry with the current token, not mint a third'
    assert len(calls) == 2


def test_many_401s_on_the_same_generation_still_cause_one_refresh():
    fetch, calls = _counting_fetcher()
    p = TokenProvider(fetch, clock=Clock())
    tok = p.token()

    for _ in range(8):                      # the whole pool reports the same dead token
        p.report_unauthorized(tok)

    p.token()
    assert len(calls) == 2, 'one death, one refresh'


# ----------------------------------------------------------------------------
# auth: the loop detector
# ----------------------------------------------------------------------------

def test_three_stillborn_tokens_stop_the_run_instead_of_spinning():
    '''
    The server issues a token and then refuses it. Refreshing produces another
    one just like it. Three generations of that is not bad luck, it is a fixed
    point, and spotdl will sit in it at full speed until you kill the process.
    '''
    clk = Clock()
    fetch, calls = _counting_fetcher()
    p = TokenProvider(fetch, loop_threshold=3, fresh_grace_s=10.0, clock=clk)

    p.report_unauthorized(p.token())        # generation 1, newborn, rejected
    p.report_unauthorized(p.token())        # generation 2, newborn, rejected

    with pytest.raises(AuthRefreshLoop) as exc:
        p.report_unauthorized(p.token())    # generation 3. enough.

    assert exc.value.retry.value == 'never'
    assert 'clock' in exc.value.remedy, 'the remedy must name the usual culprit'
    assert exc.value.context['stillborn_generations'] == [1, 2, 3]
    assert len(calls) == 3, 'it stopped refreshing, it did not keep going'


def test_once_tripped_it_stays_tripped():
    clk = Clock()
    fetch, _ = _counting_fetcher()
    p = TokenProvider(fetch, loop_threshold=2, clock=clk)

    p.report_unauthorized(p.token())
    with pytest.raises(AuthRefreshLoop):
        p.report_unauthorized(p.token())
    with pytest.raises(AuthRefreshLoop):
        p.token()                           # even asking is refused now


def test_an_expired_token_rejected_is_not_a_loop():
    '''A 401 on an old token is just expiry. Do not cry wolf.'''
    clk = Clock()
    fetch, calls = _counting_fetcher(expires_in=1_000_000)
    p = TokenProvider(fetch, loop_threshold=2, fresh_grace_s=10.0, clock=clk)

    for _ in range(5):
        tok = p.token()
        clk.advance(60.0)                   # this token is old, not newborn
        p.report_unauthorized(tok)

    assert len(calls) == 5                  # five honest refreshes, no trip


def test_success_forgets_the_history():
    '''A loop is consecutive failure. One good request resets the count.'''
    clk = Clock()
    fetch, _ = _counting_fetcher()
    p = TokenProvider(fetch, loop_threshold=2, clock=clk)

    p.report_unauthorized(p.token())        # stillborn generation 1
    tok = p.token()                         # generation 2
    p.report_success(tok)                   # ...and it works
    p.report_unauthorized(tok)              # a later, unrelated rejection

    p.token()                               # must not raise


def test_refreshes_are_announced_on_the_bus():
    bus = EventBus()
    seen: list = []
    bus.subscribe(seen.append)

    fetch, _ = _counting_fetcher(expires_in=3600.0)
    TokenProvider(fetch, bus=bus, run_id='r1', clock=Clock()).token()

    events = [e for e in seen if isinstance(e, AuthRefreshed)]
    assert len(events) == 1
    assert events[0].provider == 'spotify' and events[0].expires_in_s == 3600.0


def test_a_rejected_credential_is_not_retried_into_the_ground():
    def fetch():
        raise CredentialsRejected('spotify said no')

    p = TokenProvider(fetch, clock=Clock())
    with pytest.raises(CredentialsRejected) as exc:
        p.token()
    assert not exc.value.retryable, 'wrong credentials do not become right'
