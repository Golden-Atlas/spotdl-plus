'''
spotify_web.py - signing in the way the web player does, no app and no Premium.

Spotify put API app creation behind Premium in early 2026, which would have
locked most people out. This signs in the way open.spotify.com does instead:
bootstrap a session, mint an anonymous access token from a server-side TOTP,
fetch a client-token, and send BOTH on every request.

The signature is the whole game. A bare bearer gets a retry-after of 74240
seconds, which is a 20-hour wall, because it looks exactly like a scraper. The
bearer paired with the client-token and a real session gets a retry-after of 15
seconds, a normal rate limit you just pace around. So this is not optional
decoration, it is the difference between working and not.

Two things rot over time and I plan for both. Spotify rotates the TOTP secret,
so a fresh one gets fetched when the bundled one stops working (see the config
switch and the notice it emits). And the token endpoint can move, which is why
every failure here is typed and points at bring-your-own-app as the escape.

This is the one place the tool reaches a non-Spotify host (for the secret), and
only when it has to. Everything downstream stays identical, because it is still
Spotify's own data on the same endpoints.
'''

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from ..core.errors import (
    SpotdlPlusError,
    SpotifyWebClientTokenFailed,
    SpotifyWebSecretStale,
    SpotifyWebSessionFailed,
    SpotifyWebTokenFailed,
)
from ..core.events import EventBus, NullBus, Warned
from .auth import TokenProvider
from .http import default_ssl_context

_SESSION_URL = 'https://open.spotify.com'
_TOKEN_URL = 'https://open.spotify.com/api/token'
_CLIENTTOKEN_URL = 'https://clienttoken.spotify.com/v1/clienttoken'
#: Community-maintained mirror of Spotify's current TOTP secret. Only hit when
#: the bundled one below stops working, and only if autofetch is on.
_SECRET_URL = ('https://code.thetadev.de/ThetaDev/spotify-secrets/'
               'raw/branch/main/secrets/secretDict.json')

#: A real browser UA, because the whole point is not looking like a script.
_BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

#: Baked-in fallback secret, version 61. Used first, every run. When Spotify
#: rotates past it the fetch above picks up the current one. Refresh this on a
#: release so the offline path stays fresh too.
_FALLBACK_SECRET: tuple[int, bytes] = (
    61,
    bytes([44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120, 97, 75, 76,
           94, 102, 43, 69, 49, 120, 118, 80, 64, 78]),
)


# ---------------------------------------------------------------------------
# the TOTP, kept as pure functions so the whole thing is testable without a
# network. Given a secret and a fixed clock the output is exact.
# ---------------------------------------------------------------------------

def _hotp(key: bytes, counter: int, digits: int = 6) -> str:
    '''RFC 4226. The dynamic-truncation dance every authenticator app does.'''
    mac = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0f
    code = struct.unpack('>I', mac[offset:offset + 4])[0] & 0x7fffffff
    return str(code % (10 ** digits)).zfill(digits)


def _totp(secret_b32: str, now: float, period: int = 30, digits: int = 6) -> str:
    '''RFC 6238. Standard 30-second window, matches what pyotp would hand back.'''
    pad = '=' * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32 + pad)
    return _hotp(key, int(now // period), digits)


def _secret_to_b32(secret_bytes: bytes) -> str:
    '''
    Spotify's own obfuscation of the seed, replicated. XOR each byte against a
    position-dependent value, join the results as a decimal string, hex it, and
    base32 that. Weird, but it is exactly what the web player does, so it is
    what the server checks against.
    '''
    transformed = [e ^ ((t % 33) + 9) for t, e in enumerate(secret_bytes)]
    joined = ''.join(str(n) for n in transformed)
    return base64.b32encode(bytes.fromhex(joined.encode().hex())).decode().rstrip('=')


def generate_totp(secret_bytes: bytes, *, now: float) -> str:
    '''The one-time code for this instant, from a raw secret. Pure and pinnable.'''
    return _totp(_secret_to_b32(secret_bytes), now)


# ---------------------------------------------------------------------------
# the persisted-query hashes, re-read off the live bundle. The web player
# registers each query as `"name","query","<64 hex>"`, a stable, self-describing
# shape, so pulling the current set is a plain scan of one bundle with no
# reliance on webpack's chunk layout -- which is the part that actually rots.
# These are pure so the scan is pinned without a network; the fetch that feeds
# them lives on WebPlayerAuth.
# ---------------------------------------------------------------------------

_BUNDLE_RE = re.compile(r'https://[\w.-]+/cdn/build/web-player/web-player\.[0-9a-f]+\.js')
_QUERY_HASH_RE = re.compile(r'"(\w+)","(?:query|mutation)","([0-9a-f]{64})"')


def find_web_player_bundle(html: str) -> str | None:
    '''The main web-player JS bundle URL in the session page, or None if it moved.'''
    match = _BUNDLE_RE.search(html)
    return match.group(0) if match else None


def extract_query_hashes(js_text: str) -> dict[str, str]:
    '''
    Every `{operationName: sha256}` a bundle registers. First occurrence wins, so
    a duplicated registration can't shadow the one the player actually uses.
    '''
    found: dict[str, str] = {}
    for name, sha in _QUERY_HASH_RE.findall(js_text):
        found.setdefault(name, sha)
    return found


@dataclass(frozen=True, slots=True)
class LegResult:
    '''One step of the sign-in and whether it held. What doctor renders.'''

    name: str
    ok: bool
    detail: str


# ---------------------------------------------------------------------------
# the auth object
# ---------------------------------------------------------------------------

class WebPlayerAuth(TokenProvider):
    '''
    Mints and refreshes the anonymous access token AND the client-token, and
    hands both to the http client. The bearer rides the TokenProvider machinery
    it inherits, single-flight and loop-detected the same as an app token. The
    client-token is its own small refresh next to it, because it expires on its
    own schedule and Spotify signals its death differently.

    Its own httpx client on purpose, separate from the API HttpClient, because
    the sign-in is stateful (cookies carry the session) and the API client is
    not. Forces IPv4 and verifies through the OS trust store, the same two fixes
    the rest of net/ needs, because a bundled CA list dies behind antivirus that
    re-signs TLS and a v6 route that blackholes.
    '''

    def __init__(
        self,
        *,
        autofetch: bool = True,
        bus: EventBus | None = None,
        transport: httpx.BaseTransport | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._autofetch = autofetch
        self._emit = (bus or NullBus()).emit
        self._wall = wall_clock

        if transport is None:
            transport = httpx.HTTPTransport(
                local_address='0.0.0.0', verify=default_ssl_context(), retries=1)
        self._web = httpx.Client(
            transport=transport, follow_redirects=True,
            timeout=httpx.Timeout(20.0), headers={'User-Agent': _BROWSER_UA})

        # session state, filled by _bootstrap_session
        self._client_version: str | None = None
        self._client_id: str | None = None
        self._device_id: str = ''
        # the secret currently in play, bundled until a fetch replaces it
        self._secret: tuple[int, bytes] = _FALLBACK_SECRET
        self._tried_fetch = False
        # client-token state, guarded on its own lock
        self._ct_lock = threading.RLock()
        self._ct_value: str | None = None
        self._ct_expires_at: float = 0.0

        super().__init__(fetch=self._mint_bearer, name='spotify-web', bus=bus)

    # -- the request seam ----------------------------------------------------

    def extra_headers(self) -> dict[str, str]:
        '''
        Everything the pathfinder host wants alongside the bearer, merged onto
        each request by the http client. Two are load-bearing. The client-token
        keeps us off the 20-hour wall. And a browser User-Agent is not optional:
        api-partner.spotify.com answers a non-browser UA with a flat 403 no matter
        how good the token is, and a browser one with a 200 -- the UA is the whole
        gate, no TLS impersonation needed. This overrides the honest spotdlplus UA
        the shared client sends, but only on the web path, so MusicBrainz still
        gets the descriptive UA it requires. App-version and language mirror what
        the real web player sends.
        '''
        headers = {
            'client-token': self._client_token(),
            'User-Agent': _BROWSER_UA,
            'Accept-Language': 'en',
        }
        if self._client_version:
            headers['Spotify-App-Version'] = self._client_version
        return headers

    def close(self) -> None:
        self._web.close()

    # -- the reactive heal for the OTHER rotating thing ----------------------

    def latest_query_hashes(self) -> dict[str, str] | None:
        '''
        Re-read the live persisted-query hashes off the web player's own bundle.
        This is the reactive heal behind PathfinderClient: it runs only when a
        bundled hash gets rejected, so it is best-effort by design. Any failure
        returns None and the caller keeps the bundled set (and, if that is also
        stale, raises a typed SpotifyWebQueryStale that points at bring-your-own-
        app). One fetch of the main bundle, which carries the download-path
        queries; the search query lives in a lazily-loaded chunk this does not
        chase, so search leans on its bundled hash and a release refresh.

        No transparency notice here, unlike the secret heal: this only ever
        reaches Spotify's own CDN, the same infrastructure the rest of the
        sign-in already talks to, not a third party.
        '''
        try:
            page = self._web.get(_SESSION_URL, headers={'Accept': 'text/html'})
            if page.status_code != 200:
                return None
            bundle = find_web_player_bundle(page.text)
            if not bundle:
                return None
            js = self._web.get(bundle)
            if js.status_code != 200:
                return None
        except httpx.HTTPError:
            return None
        return extract_query_hashes(js.text) or None

    # -- the diagnostic ------------------------------------------------------

    def diagnose(self) -> list[LegResult]:
        '''
        Run each leg of the sign-in on its own and report which held and which
        broke, without raising. This is what doctor renders, and running it on a
        clean network is how the free path proves itself. Stops at the first
        broken leg, because the ones after it depend on it.
        '''
        out: list[LegResult] = []
        try:
            self._bootstrap_session()
            out.append(LegResult('session', True,
                                 f'open.spotify.com, clientVersion {self._client_version}'))
        except SpotdlPlusError as exc:
            out.append(LegResult('session', False, f'[{exc.code}] {exc.message}'))
            return out
        try:
            self.token()
            out.append(LegResult('access token', True,
                                 f'minted, login secret v{self._secret[0]}'))
        except SpotdlPlusError as exc:
            out.append(LegResult('access token', False, f'[{exc.code}] {exc.message}'))
            return out
        try:
            self._client_token()
            out.append(LegResult('client-token', True, 'granted'))
        except SpotdlPlusError as exc:
            out.append(LegResult('client-token', False, f'[{exc.code}] {exc.message}'))
        return out

    # -- session -------------------------------------------------------------

    def _bootstrap_session(self) -> None:
        '''
        Load open.spotify.com once for the cookies and the client version. The
        sp_t cookie becomes the device id the client-token wants, and the
        version is baked into the page's appServerConfig blob.
        '''
        try:
            r = self._web.get(_SESSION_URL, headers={'Accept': 'text/html'})
        except httpx.HTTPError as exc:
            raise SpotifyWebSessionFailed(
                'could not reach open.spotify.com', cause=exc,
                context={'url': _SESSION_URL}) from exc
        if r.status_code != 200:
            raise SpotifyWebSessionFailed(
                f'open.spotify.com answered {r.status_code}', context={'status': r.status_code})
        try:
            raw = r.text.split('<script id="appServerConfig" type="text/plain">')[1].split('</script>')[0]
            cfg = json.loads(base64.b64decode(raw))
            self._client_version = cfg['clientVersion']
        except Exception as exc:  # noqa: BLE001  # any parse miss means the page shape moved
            raise SpotifyWebSessionFailed(
                'open.spotify.com loaded but its config block moved', cause=exc,
                context={'url': _SESSION_URL}) from exc
        self._device_id = self._web.cookies.get('sp_t') or ''

    # -- the bearer, the TokenProvider fetch ---------------------------------

    def _mint_bearer(self) -> tuple[str, float]:
        '''
        The anonymous access token. Bootstraps a session if we don't have one,
        signs a TOTP with the current secret, and asks for a token. If the
        secret is stale and autofetch is on, grabs the current one and tries
        once more before giving up, so a rotation self-heals instead of failing
        a run. Returns (token, seconds-until-expiry) the way TokenProvider wants.
        '''
        if self._client_version is None:
            self._bootstrap_session()

        token = self._request_token()
        if token is None and self._autofetch and not self._tried_fetch:
            self._tried_fetch = True
            if self._fetch_latest_secret():
                token = self._request_token()
        if token is None:
            raise SpotifyWebSecretStale(
                'Spotify rejected the login secret and no fresh one worked',
                context={'secret_version': self._secret[0], 'autofetch': self._autofetch})

        value, exp_ms, self._client_id = token
        expires_in = max(0.0, exp_ms / 1000.0 - self._wall())
        return value, expires_in

    def _request_token(self) -> tuple[str, float, str] | None:
        '''
        One shot at /api/token. Returns (access_token, expiry_ms, client_id), or
        None when the failure looks like a stale secret so the caller can refetch
        and retry. Any other failure is a hard, typed error.
        '''
        version, secret = self._secret
        code = generate_totp(secret, now=self._wall())
        try:
            r = self._web.get(_TOKEN_URL, params={
                'reason': 'init', 'productType': 'web-player',
                'totp': code, 'totpVer': version, 'totpServer': code})
        except httpx.HTTPError as exc:
            raise SpotifyWebTokenFailed(
                'the token endpoint did not answer', cause=exc, context={'url': _TOKEN_URL}) from exc
        if r.status_code != 200:
            body = r.text[:200]
            if 'totp' in body.lower() or r.status_code in (400, 401):
                return None   # smells like a stale secret, let the caller refetch
            raise SpotifyWebTokenFailed(
                f'the token endpoint answered {r.status_code}',
                context={'status': r.status_code, 'body': body})
        try:
            j = r.json()
            return j['accessToken'], float(j.get('accessTokenExpirationTimestampMs') or 0), j['clientId']
        except Exception:  # noqa: BLE001  # missing accessToken == the shape changed
            return None

    def _fetch_latest_secret(self) -> bool:
        '''
        Pull the current secret from the community mirror. The one outbound call
        that isn't Spotify or music, so it announces itself. Returns whether it
        found a newer secret than the one already in play.
        '''
        try:
            r = self._web.get(_SECRET_URL, timeout=8)
            if r.status_code != 200:
                return False
            data = r.json()
            version = max(data, key=int)
            secret = bytes(data[version])
        except Exception:  # noqa: BLE001  # a missing secret just leaves the bundled one
            return False
        if int(version) == self._secret[0]:
            return False
        self._secret = (int(version), secret)
        self._emit(Warned(
            run_id=self._run_id,
            message=('Spotify rotated its login secret, so I fetched the current '
                     f'one (v{version}) from the community mirror. Turn '
                     '"spotify_secret_autofetch" off in config if you would rather '
                     'this never reach out.'),
            context={'from': _SECRET_URL, 'version': int(version)}))
        return True

    # -- the client-token ----------------------------------------------------

    def _client_token(self) -> str:
        '''
        The current client-token, refreshed on time under its own lock. Needs a
        session and a client id, so it leans on the bearer having been minted at
        least once (token() fills client_id) and bootstraps a session if not.
        '''
        now = self._wall()
        if self._ct_value is not None and now < self._ct_expires_at:
            return self._ct_value
        with self._ct_lock:
            if self._ct_value is not None and now < self._ct_expires_at:
                return self._ct_value
            if self._client_id is None:
                self.token()   # mints the bearer, which fills client_id + session
            return self._mint_client_token()

    def _mint_client_token(self) -> str:
        payload = {'client_data': {
            'client_version': self._client_version, 'client_id': self._client_id,
            'js_sdk_data': {'device_brand': 'unknown', 'device_model': 'unknown',
                            'os': 'windows', 'os_version': 'NT 10.0',
                            'device_id': self._device_id, 'device_type': 'computer'}}}
        try:
            r = self._web.post(_CLIENTTOKEN_URL, json=payload,
                               headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
        except httpx.HTTPError as exc:
            raise SpotifyWebClientTokenFailed(
                'clienttoken endpoint did not answer', cause=exc,
                context={'url': _CLIENTTOKEN_URL}) from exc
        if r.status_code != 200:
            raise SpotifyWebClientTokenFailed(
                f'clienttoken answered {r.status_code}', context={'status': r.status_code})
        try:
            j = r.json()
            if j.get('response_type') != 'RESPONSE_GRANTED_TOKEN_RESPONSE':
                raise SpotifyWebClientTokenFailed(
                    'clienttoken refused to grant', context={'response_type': j.get('response_type')})
            granted = j['granted_token']
            self._ct_value = granted['token']
            # refresh_after_seconds if they give it, else a conservative half hour
            self._ct_expires_at = self._wall() + float(granted.get('refresh_after_seconds') or 1800)
            return self._ct_value
        except SpotifyWebClientTokenFailed:
            raise
        except Exception as exc:  # noqa: BLE001  # a shape we didn't expect
            raise SpotifyWebClientTokenFailed(
                'clienttoken sent a body we could not read', cause=exc) from exc
