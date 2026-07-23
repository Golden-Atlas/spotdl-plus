'''
test_http.py - every way a server can disappoint us, and what we do about it.

The one to read first is `test_a_dead_host_is_not_a_dead_network`. Confusing those
two is why tools park a 4,000-track run because one API sneezed, and why they
grind for two hours against a network that is not there.
'''

from __future__ import annotations

import ssl
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from spotdlplus.core.errors import (
    AuthRefreshLoop,
    BadRequest,
    CredentialsRejected,
    EntityNotFound,
    MetadataForbidden,
    NetTimeout,
    Offline,
    ProviderFailed,
    RateLimited,
    Retry,
    SourceBlocked,
    TlsUntrusted,
    TokenExpired,
)
from spotdlplus.net.auth import TokenProvider
from spotdlplus.net.http import (
    HttpClient,
    Response,
    default_ssl_context,
    is_tls_trust_failure,
    parse_retry_after,
)
from spotdlplus.net.probe import OnlineProbe
from spotdlplus.net.ratelimit import HostLimiter

URL = 'https://api.test/v1/thing'


def _online(_addr, _timeout) -> None:
    return None


def _offline(_addr, _timeout) -> None:
    raise OSError('no route to host')


def client(handler, *, online: bool = True, **kw) -> HttpClient:
    '''A client whose network, clock, and budget are entirely ours.'''
    return HttpClient(
        transport=httpx.MockTransport(handler),
        probe=OnlineProbe(connect=_online if online else _offline, ttl_s=0.0),
        limiter=HostLimiter({}, fallback=(1000.0, 1000)),   # pacing is tested elsewhere
        **kw,
    )


def ok(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


# ----------------------------------------------------------------------------
# the happy path
# ----------------------------------------------------------------------------

def test_a_good_response_comes_back_as_data_not_as_httpx():
    with client(lambda r: ok({'name': 'a record'})) as c:
        resp = c.get(URL)
    assert isinstance(resp, Response)
    assert resp.status == 200
    assert resp.json() == {'name': 'a record'}


def test_we_identify_ourselves_because_musicbrainz_requires_it():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers['user-agent'])
        return ok({})

    with client(handler, version='1.2.3') as c:
        c.get(URL)
    assert seen[0].startswith('spotdlplus/1.2.3')
    assert 'http' in seen[0], 'their terms want a contact URL, not just a name'


def test_a_body_that_is_not_json_is_a_typed_failure_not_a_traceback():
    with client(lambda r: httpx.Response(200, content=b'<html>oops</html>')) as c:
        resp = c.get(URL)
        with pytest.raises(ProviderFailed) as exc:
            resp.json()
    assert 'oops' in exc.value.context['body']


# ----------------------------------------------------------------------------
# status codes become policy
# ----------------------------------------------------------------------------

def test_429_is_throttling_and_carries_the_servers_own_deadline():
    handler = lambda r: httpx.Response(429, headers={'Retry-After': '7'})
    with client(handler) as c, pytest.raises(RateLimited) as exc:
        c.get(URL)
    assert exc.value.retry is Retry.AFTER
    assert exc.value.retry_after == 7.0


def test_a_503_that_names_a_deadline_is_treated_as_throttling():
    handler = lambda r: httpx.Response(503, headers={'Retry-After': '30'})
    with client(handler) as c, pytest.raises(RateLimited) as exc:
        c.get(URL)
    assert exc.value.retry_after == 30.0


def test_a_503_with_no_deadline_is_just_a_broken_server():
    with client(lambda r: httpx.Response(503)) as c, pytest.raises(ProviderFailed) as exc:
        c.get(URL)
    assert exc.value.retry is Retry.BACKOFF


def test_404_is_final_because_the_thing_is_simply_not_there():
    with client(lambda r: httpx.Response(404)) as c, pytest.raises(EntityNotFound) as exc:
        c.get(URL)
    assert exc.value.retry is Retry.NEVER


def test_403_is_a_block_and_backs_off():
    with client(lambda r: httpx.Response(403)) as c, pytest.raises(SourceBlocked) as exc:
        c.get(URL)
    assert exc.value.retry is Retry.BACKOFF


def test_500_backs_off():
    with client(lambda r: httpx.Response(500)) as c, pytest.raises(ProviderFailed) as exc:
        c.get(URL)
    assert exc.value.retry is Retry.BACKOFF


def test_a_400_is_our_bug_and_retrying_sends_the_same_bytes():
    handler = lambda r: httpx.Response(400, content=b'{"error":"bad market code"}')
    with client(handler) as c, pytest.raises(BadRequest) as exc:
        c.get(URL)
    assert exc.value.retry is Retry.NEVER
    assert 'bad market code' in exc.value.context['body']


def test_401_without_a_token_provider_means_the_credentials_are_wrong():
    with client(lambda r: httpx.Response(401)) as c, pytest.raises(CredentialsRejected) as exc:
        c.get(URL)
    assert exc.value.retry is Retry.NEVER


# ----------------------------------------------------------------------------
# the distinction that earns this module its keep
# ----------------------------------------------------------------------------

def test_a_dead_network_parks_the_run():
    def handler(r):
        raise httpx.ConnectError('unreachable')

    with client(handler, online=False) as c, pytest.raises(Offline) as exc:
        c.get(URL)
    assert exc.value.retry is Retry.PARK
    assert 'resume' in exc.value.remedy


def test_a_dead_host_is_not_a_dead_network():
    '''
    The same ConnectError. Completely different response. One host being down must
    never park a run. we have 3,999 other tracks and most of them do not need it.
    '''
    def handler(r):
        raise httpx.ConnectError('unreachable')

    with client(handler, online=True) as c, pytest.raises(ProviderFailed) as exc:
        c.get(URL)
    assert exc.value.retry is Retry.BACKOFF
    assert 'their problem' in str(exc.value)


def test_an_unverifiable_certificate_blames_the_machine_not_the_server():
    '''
    Found the hard way: AVG Antivirus was terminating HTTPS on the dev box and
    re-signing it with its own root. The old code reported "their problem, not
    yours" about a server that was entirely healthy.
    '''
    def handler(r):
        raise httpx.ConnectError(
            '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: '
            'unable to get local issuer certificate'
        )

    with client(handler, online=True) as c, pytest.raises(TlsUntrusted) as exc:
        c.get(URL)

    assert exc.value.retry is Retry.NEVER, 'retrying will not install a root CA'
    assert 'antivirus' in exc.value.remedy.lower()
    assert 'not skip verification' in exc.value.remedy


def test_a_bad_certificate_is_not_reported_as_being_offline():
    '''Even with no network, an untrusted cert is the more specific truth.'''
    def handler(r):
        raise httpx.ConnectError('[SSL: CERTIFICATE_VERIFY_FAILED] nope')

    with client(handler, online=False) as c, pytest.raises(TlsUntrusted):
        c.get(URL)


def test_a_bad_certificate_is_not_held_against_the_host():
    def handler(r):
        raise httpx.ConnectError('[SSL: CERTIFICATE_VERIFY_FAILED] nope')

    with client(handler, breaker_threshold=1) as c:
        with pytest.raises(TlsUntrusted):
            c.get(URL)
        assert c.breaker('api.test').allow(), 'our trust store is not their fault'


def test_tls_failures_are_found_however_deeply_they_are_buried():
    inner = ssl.SSLCertVerificationError('certificate verify failed')
    middle = OSError('connect failed')
    middle.__cause__ = inner
    outer = httpx.ConnectError('unreachable')
    outer.__cause__ = middle

    assert is_tls_trust_failure(outer)
    assert not is_tls_trust_failure(httpx.ConnectError('just plain down'))


def test_we_verify_against_the_os_trust_store_when_we_can():
    '''The fallback is httpx's default. It is never `verify=False`.'''
    ctx = default_ssl_context()
    assert ctx is not False
    assert isinstance(ctx, (ssl.SSLContext, bool))


def test_a_timeout_is_never_mistaken_for_being_offline():
    def handler(r):
        raise httpx.ReadTimeout('slow')

    with client(handler, online=False) as c, pytest.raises(NetTimeout) as exc:
        c.get(URL)
    assert exc.value.retry is Retry.BACKOFF, 'a slow server is not a missing network'


# ----------------------------------------------------------------------------
# auth, end to end
# ----------------------------------------------------------------------------

def _token_provider(**kw) -> TokenProvider:
    counter = {'n': 0}

    def fetch():
        counter['n'] += 1
        return (f'token-{counter["n"]}', 3600.0)

    p = TokenProvider(fetch, **kw)
    p.fetches = counter   # type: ignore[attr-defined]
    return p


def test_a_stale_credential_is_refreshed_once_and_the_request_resent():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get('authorization', '')
        seen.append(auth)
        return ok({'ok': True}) if auth == 'Bearer token-2' else httpx.Response(401)

    auth = _token_provider()
    with client(handler) as c:
        resp = c.request('GET', URL, auth=auth)

    assert resp.json() == {'ok': True}
    assert seen == ['Bearer token-1', 'Bearer token-2']
    assert auth.refreshes == 2, 'one initial mint, one refresh, not a storm'


def test_a_freshly_minted_token_that_is_also_refused_stops_trying():
    with client(lambda r: httpx.Response(401)) as c, pytest.raises(TokenExpired) as exc:
        c.request('GET', URL, auth=_token_provider(loop_threshold=99))
    assert exc.value.retry is Retry.NOW, 'the Engine decides whether to try again'


def test_a_persistent_401_trips_the_loop_detector_from_inside_a_request():
    '''The 401 storm, caught at the layer where it actually happens.'''
    auth = _token_provider(loop_threshold=2)
    with client(lambda r: httpx.Response(401)) as c, pytest.raises(AuthRefreshLoop):
        c.request('GET', URL, auth=auth)
    assert auth.refreshes == 2, 'it stopped. It did not keep minting'


def test_a_successful_request_reports_the_credential_as_healthy():
    auth = _token_provider(loop_threshold=2)
    with client(lambda r: ok({})) as c:
        c.request('GET', URL, auth=auth)
        c.request('GET', URL, auth=auth)
    assert auth.refreshes == 1, 'a valid token is reused, not re-minted'


# ----------------------------------------------------------------------------
# the breaker, in situ
# ----------------------------------------------------------------------------

def test_a_failing_host_stops_being_asked():
    calls = {'n': 0}

    def handler(r):
        calls['n'] += 1
        return httpx.Response(500)

    with client(handler, breaker_threshold=2) as c:
        for _ in range(2):
            with pytest.raises(ProviderFailed):
                c.get(URL)

        with pytest.raises(ProviderFailed) as exc:
            c.get(URL)

    assert calls['n'] == 2, 'the third request never left the building'
    assert 'circuit open' in str(exc.value)


def test_throttling_is_not_held_against_the_host():
    '''A 429 means we were rude, not that they are broken. Do not trip on it.'''
    handler = lambda r: httpx.Response(429, headers={'Retry-After': '1'})
    with client(handler, breaker_threshold=2) as c:
        for _ in range(5):
            with pytest.raises(RateLimited):
                c.get(URL)
        assert c.breaker('api.test').allow(), 'the breaker should still be closed'


# ----------------------------------------------------------------------------
# retry-after parsing
# ----------------------------------------------------------------------------

def test_retry_after_reads_seconds():
    assert parse_retry_after('120') == 120.0


def test_retry_after_reads_an_http_date():
    when = datetime.now(timezone.utc) + timedelta(seconds=60)
    stamp = when.strftime('%a, %d %b %Y %H:%M:%S GMT')
    got = parse_retry_after(stamp, now=datetime.now(timezone.utc))
    assert 55.0 <= got <= 61.0


def test_a_retry_after_in_the_past_is_not_a_negative_wait():
    stamp = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime('%a, %d %b %Y %H:%M:%S GMT')
    assert parse_retry_after(stamp) == 0.0


def test_garbage_retry_after_is_ignored_rather_than_fatal():
    assert parse_retry_after('soon-ish') is None
    assert parse_retry_after(None) is None
    assert parse_retry_after('') is None


def test_html_where_json_should_be_is_a_captive_portal():
    from spotdlplus.core.errors import NetCaptive
    page = httpx.Response(200, headers={'content-type': 'text/html'},
                          content=b'<html><body>click to join the wifi</body></html>')
    with client(lambda r: page) as c, pytest.raises(NetCaptive):
        c.get(URL)


# ----------------------------------------------------------------------------
# a 403 is two different animals depending on who said it
# ----------------------------------------------------------------------------

def test_a_metadata_403_does_not_give_youtube_advice():
    '''
    A private playlist used to raise SourceBlocked, whose remedy is entirely
    about bot walls and browser cookies. Somebody's first run got told to
    configure cookies they had already configured, for a problem that was only
    a visibility setting.
    '''
    def handler(_r):
        return httpx.Response(403)

    with client(handler) as c, pytest.raises(MetadataForbidden) as exc:
        c.get('https://api.spotify.com/v1/playlists/abc')

    err = exc.value
    assert err.code == 'META_FORBIDDEN'
    assert 'public' in err.remedy.lower(), 'it names the actual fix'
    assert 'youtube_cookies_from_browser' not in err.remedy
    assert 'private' in err.remedy.lower()


def test_a_youtube_403_still_gets_the_bot_wall_advice():
    def handler(_r):
        return httpx.Response(403)

    with client(handler) as c, pytest.raises(SourceBlocked) as exc:
        c.get('https://www.youtube.com/watch')
    assert 'youtube_cookies_from_browser' in exc.value.remedy


def test_a_metadata_403_does_not_trip_the_breaker():
    '''
    One playlist we may not read is not the host being down. Tripping the
    breaker would take the rest of the run with it.
    '''
    def handler(_r):
        return httpx.Response(403)

    with client(handler) as c:
        for _ in range(8):
            with pytest.raises(MetadataForbidden):
                c.get('https://api.spotify.com/v1/playlists/abc')
        with pytest.raises(MetadataForbidden):
            c.get('https://api.spotify.com/v1/playlists/def')
