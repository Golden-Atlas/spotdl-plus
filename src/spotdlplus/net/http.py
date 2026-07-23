'''
http.py - HTTP client and failure classification

Every request goes through 4 gates in order. The circuit breaker checks if the
host is worth talking to, the token bucket checks if we're allowed to yet, the
token provider hands over a credential, and then the request runs. Anything
that goes wrong becomes a typed error befoer it leaves this file.

No httpx types cross the boundary. Everything above us gets our Response and
our errors, so swapping the client out stays an implementation detail.
'''

from __future__ import annotations

import json as jsonlib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..core.errors import (
    BadRequest,
    CredentialsRejected,
    EntityNotFound,
    MetadataForbidden,
    NetCaptive,
    NetClockSkew,
    NetDns,
    NetTimeout,
    Offline,
    ProviderFailed,
    RateLimited,
    SourceBlocked,
    TlsUntrusted,
    TokenExpired,
)
from .auth import TokenProvider
from .breaker import CircuitBreaker
from .probe import OnlineProbe
from .ratelimit import HostLimiter

__all__ = [
    'DEFAULT_TIMEOUT', 'HttpClient', 'Response', 'default_ssl_context',
    'default_user_agent', 'is_tls_trust_failure', 'parse_retry_after',
]

#: Generous on read, impatient on connect. A server that will not answer the
#: handshake in ten seconds is not going to serve us an album.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

#: Hosts that answer questions about music rather than serve the audio. A 403
#: from one of these means a permission problem with the thing we asked for,
#: not a bot wall, so it gets its own error and its own advice.
_METADATA_HOSTS = ('spotify.com', 'musicbrainz.org', 'acoustid.org', 'lrclib.net')


def default_ssl_context() -> ssl.SSLContext | bool:
    '''
    Verifies against the OS trust store when it can.

    Bundled CA lists only trust public authorities, which is correct and also
    useless on the very common machine where antivirus or a corporate proxy
    re-signs HTTPS with its own root. That root only exists in the OS store.
    '''
    try:
        import truststore
    except ImportError:
        return True
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


_TRUST_STORE_INJECTED = False


def enable_os_trust_store() -> bool:
    '''
    Makes the whole process verify TLS against the OS trust store, once.

    Our HttpClient already does this directly. yt-dlp builds its own context deep
    in its stack where we can't reach it, and on a box with antivirus intercepting
    HTTPS that context rejects everything, so a process-wide inject is the only
    way in.
    '''
    global _TRUST_STORE_INJECTED
    if _TRUST_STORE_INJECTED:
        return True
    try:
        import truststore
    except ImportError:
        return False
    truststore.inject_into_ssl()
    _TRUST_STORE_INJECTED = True
    return True


def is_tls_trust_failure(exc: BaseException) -> bool:
    '''
    Walks the cause chain looking for a certificate that wouldn't verify. httpx
    buries it inside a ConnectError where it looks exactly like the host being
    down, and those two want opposite responses.
    '''
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        if 'CERTIFICATE_VERIFY_FAILED' in str(cur).upper():
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def is_clock_skew(exc: BaseException) -> bool:
    '''
    Catches a TLS failure that's really the system clock, where a cert reads
    expired only because the machine has the wrong day. Has to run before the
    generic trust check, which it also looks like.
    '''
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        msg = str(cur).lower()
        if ('certificate has expired' in msg or 'certificate is not yet valid' in msg
                or 'cert_has_expired' in msg or 'cert_not_yet_valid' in msg):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def is_dns_failure(exc: BaseException) -> bool:
    '''Couldn't resolve the hostname at all. DNS is down or the wifi's a lie.'''
    import socket
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, socket.gaierror):
            return True
        msg = str(cur).lower()
        if ('getaddrinfo failed' in msg or 'name or service not known' in msg
                or 'temporary failure in name resolution' in msg
                or 'nodename nor servname' in msg or 'name resolution' in msg
                or '11001' in msg):     # WSAHOST_NOT_FOUND on Windows
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def default_user_agent(version: str) -> str:
    '''
    MusicBrainz wants a User-Agent that names the app and gives them a way to
    contact whoever is hammering them, and they enforce it. An anonymous one gets
    you a 403 that looks liek a bug.
    '''
    return f'spotdlplus/{version} ( https://github.com/Golden-Atlas/spotdl-plus )'


@dataclass(frozen=True, slots=True)
class Response:
    '''What the layers above receive. Notably: no httpx types cross this line.'''

    status: int
    url: str
    headers: dict[str, str]
    content: bytes

    def json(self) -> Any:
        try:
            return jsonlib.loads(self.content or b'null')
        except ValueError as exc:
            raise ProviderFailed(
                f'{self.url}: response was not JSON',
                context={'url': self.url, 'body': self.content[:400].decode('utf-8', 'replace')},
                cause=exc,
            ) from exc


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    '''
    Handles both forms RFC 9110 allows, seconds or an HTTP-date, since servers use
    both. Never returns a negative wait for a date already in the past.
    '''
    if not value:
        return None
    v = value.strip()
    if v.isdigit():
        return float(v)
    try:
        when = parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (when - reference).total_seconds())


class HttpClient:
    '''
    Shared by every provider so there's one limiter, one breaker per host, and one
    probe. Eight workers hitting Spotify share a single budget instead of eight.
    '''

    def __init__(
        self,
        *,
        version: str = '0.0.0',
        user_agent: str | None = None,
        limiter: HostLimiter | None = None,
        probe: OnlineProbe | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        verify: ssl.SSLContext | bool | None = None,
        breaker_threshold: int = 5,
        breaker_cooldown_s: float = 60.0,
    ) -> None:
        self._limiter = limiter or HostLimiter()
        self._probe = probe or OnlineProbe()
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown_s
        self._breakers: dict[str, CircuitBreaker] = {}
        kwargs: dict[str, Any] = {}
        if transport is None:
            # httpx forbids passing both. a mock transport does no TLS anyway.
            kwargs['verify'] = default_ssl_context() if verify is None else verify
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            headers={'User-Agent': user_agent or default_user_agent(version)},
            **kwargs,
        )

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> 'HttpClient':
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def breaker(self, host: str) -> CircuitBreaker:
        b = self._breakers.get(host)
        if b is None:
            b = CircuitBreaker(threshold=self._breaker_threshold,
                               cooldown_s=self._breaker_cooldown)
            self._breakers[host] = b
        return b

    # -- the one public verb -------------------------------------------------

    def get(self, url: str, **kw: Any) -> Response:
        return self.request('GET', url, **kw)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        auth: TokenProvider | None = None,
        cost: int = 1,
    ) -> Response:
        host = httpx.URL(url).host
        breaker = self.breaker(host)

        if not breaker.allow():
            raise ProviderFailed(
                f'{host}: circuit open after repeated failures, not hitting it again yet',
                context={'host': host, 'breaker': str(breaker.state)},
            )

        self._limiter.acquire(host, cost)          # may raise RateLimited

        token = auth.token() if auth is not None else None
        resp = self._send(method, url, params, headers, json, data, token, host, breaker)

        if resp.status == 401 and auth is not None and token is not None:
            # The credential is stale. Present a fresh one once, immediately.
            # `report_unauthorized` is what collapses eight simultaneous 401s
            # into one refresh, adn what trips AUTH_REFRESH_LOOP if this is a
            # fixed point.
            auth.report_unauthorized(token)
            fresh = auth.token()

            self._limiter.acquire(host, cost)
            resp = self._send(method, url, params, headers, json, data, fresh, host, breaker)

            if resp.status == 401:
                auth.report_unauthorized(fresh)
                raise TokenExpired(
                    f'{host}: rejected a freshly minted token',
                    context={'host': host, 'url': url},
                )
            token = fresh

        self._classify(resp, host, breaker)        # raises on anything non-2xx

        # A 2xx that's HTML where a JSON API should be is a captive portal. The
        # wifi's "click to join" page intercepting the request. Our providers
        # olny ever return JSON (or an image) on success, so this is
        # unambiguous.
        if 'text/html' in resp.headers.get('content-type', '').lower():
            raise NetCaptive(
                f'{host}: got a web page where the API should be',
                context={'host': host, 'content_type': resp.headers.get('content-type', '')},
            )

        breaker.record_success()
        if auth is not None and token is not None:
            auth.report_success(token)
        return resp

    # -- internals -----------------------------------------------------------

    def _send(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        json: Any,
        data: dict[str, Any] | None,
        token: Any,
        host: str,
        breaker: CircuitBreaker,
    ) -> Response:
        hdrs = dict(headers or {})
        if token is not None:
            hdrs['Authorization'] = f'Bearer {token.value}'

        try:
            r = self._client.request(method, url, params=params, headers=hdrs,
                                     json=json, data=data)

        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # A wrong system clock makes a valid cert read as expired. A TLS
            # failure that's really a clock problem. Check it BEFORE trust,
            # whcih it otherwise looks identical to.
            if is_clock_skew(exc):
                raise NetClockSkew(
                    f'{host}: TLS failed on a certificate date',
                    context={'host': host}, cause=exc,
                ) from exc

            # A certificate we cannot verify arrives dressed as a ConnectError,
            # indistinguishable from "the host is down" unless you look. Check this
            # FIRST: the server is innocent, and telling the user otherwise sends
            # them hunting for an outage that does not exist.
            if is_tls_trust_failure(exc):
                raise TlsUntrusted(
                    f'{host}: could not verify the TLS certificate',
                    context={'host': host}, cause=exc,
                ) from exc

            # Couldn't even resolve the name. DNS is down, or the wifi hasn't
            # actually let us onto the internet yet. The probe won't catch tihs
            # (it dials IP literals), so it has to be named here.
            if is_dns_failure(exc):
                breaker.record_failure()
                raise NetDns(
                    f'{host}: could not resolve the hostname',
                    context={'host': host}, cause=exc,
                ) from exc

            # We could not reach the host. The only question that matters now is
            # whether *anything* is reachable, because the answers diverge.
            breaker.record_failure()
            if not self._probe.online():
                raise Offline(
                    f'no route to the network (while reaching {host})',
                    context={'host': host}, cause=exc,
                ) from exc
            raise ProviderFailed(
                f'{host} is unreachable, but the network is up -- their problem, not yours',
                context={'host': host}, cause=exc,
            ) from exc

        except httpx.TimeoutException as exc:
            breaker.record_failure()
            raise NetTimeout(f'{host}: timed out', context={'host': host}, cause=exc) from exc

        except httpx.HTTPError as exc:
            breaker.record_failure()
            raise ProviderFailed(
                f'{host}: transport failure, {type(exc).__name__}',
                context={'host': host}, cause=exc,
            ) from exc

        return Response(
            status=r.status_code,
            url=str(r.url),
            headers={k.lower(): v for k, v in r.headers.items()},
            content=r.content,
        )

    def _classify(self, resp: Response, host: str, breaker: CircuitBreaker) -> None:
        s = resp.status
        if 200 <= s < 300:
            return

        retry_after = parse_retry_after(resp.headers.get('retry-after'))
        ctx = {'host': host, 'url': resp.url, 'status': s}

        # Throttling is not the server failing. Do not hold it against the host.
        if s == 429 or (s == 503 and retry_after is not None):
            raise RateLimited(f'{host}: throttled', context=ctx, retry_after=retry_after)

        if s == 401:
            # Only reachable with no TokenProvider attached: we sent no credential,
            # or a static one the server refuses.
            raise CredentialsRejected(f'{host}: unauthorized', context=ctx)

        if s == 403:
            # A 403 from a metadata provider is not the same animal as a 403
            # from YouTube. SourceBlocked's remedy is all about bot walls and
            # browser cookies, and handing that to somebody whose playlist was
            # simply private sends them off configuring cookies they already
            # had. It also isn't the host failing, so it must not trip the
            # breaker and take the rest of the run down with it.
            if any(h in host for h in _METADATA_HOSTS):
                raise MetadataForbidden(f'{host}: forbidden', context=ctx)
            breaker.record_failure()
            raise SourceBlocked(f'{host}: forbidden', context=ctx)

        if s == 404:
            raise EntityNotFound(f'{host}: {resp.url} does not exist', context=ctx)

        if 500 <= s < 600:
            breaker.record_failure()
            raise ProviderFailed(f'{host}: server error {s}', context=ctx)

        body = resp.content[:400].decode('utf-8', 'replace')
        raise BadRequest(f'{host}: rejected our request with {s}', context={**ctx, 'body': body})
