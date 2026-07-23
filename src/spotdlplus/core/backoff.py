'''
backoff.py - retry timing math

No clock and no sleeping in here. Retry timing is either exactly right or
quietly catastrophic, and you can't test quietly catastrophic if the function
sleeps.
'''

from __future__ import annotations

import random


def backoff_delay(
    attempt: int,
    *,
    base_s: float = 1.0,
    cap_s: float = 60.0,
    jitter: bool = True,
    rng: random.Random | None = None,
) -> float:
    '''
    How long to wait before retry number `attempt`, counting from 1. Full jitter,
    so it picks anywhere between 0 and the exponential ceiling and a pool of
    workers spreads out instead of all hitting the far end together.
    '''
    if attempt < 1:
        raise ValueError(f'attempt must be >= 1, got {attempt}')
    ceiling = min(cap_s, base_s * (2 ** (attempt - 1)))
    if not jitter:
        return ceiling
    r = rng or random
    return r.uniform(0.0, ceiling)


def retry_after_delay(retry_after: float | None, *, fallback_attempt: int, cap_s: float = 300.0) -> float:
    '''
    The server told us when to come back. Obey it, to the second, up to a cap
    that stops a hostile or broken header from parking us for a day.
    '''
    if retry_after is None:
        return backoff_delay(fallback_attempt, cap_s=cap_s)
    return max(0.0, min(float(retry_after), cap_s))
