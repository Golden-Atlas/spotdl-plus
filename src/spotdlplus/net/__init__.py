'''
net, the hostile-world layer.

Everything here exists because the services we depend on are not obliged to be
kind: they expire tokens, they throttle, they lie about being up, and they go
away entirely. This layer absorbs that so the layers above can be written as if
the network were a function call.

No HTTP status code, httpx exception, or `Retry-After` header ever crosses this
boundary. Only typed errors that already know whether they are worth retrying.

Imports core. Imported by everything above. Never imports upward.
'''

from .auth import Token, TokenFetcher, TokenProvider
from .breaker import BreakerState, CircuitBreaker
from .http import DEFAULT_TIMEOUT, HttpClient, Response, default_user_agent, parse_retry_after
from .probe import OnlineProbe
from .ratelimit import DEFAULT_BUDGETS, MAX_BLOCK_S, HostLimiter, TokenBucket

__all__ = [
    'DEFAULT_BUDGETS', 'DEFAULT_TIMEOUT', 'MAX_BLOCK_S', 'BreakerState',
    'CircuitBreaker', 'HostLimiter', 'HttpClient', 'OnlineProbe', 'Response',
    'Token', 'TokenBucket', 'TokenFetcher', 'TokenProvider',
    'default_user_agent', 'parse_retry_after',
]
