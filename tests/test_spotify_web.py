'''
test_spotify_web.py - the free Spotify sign-in, held to account with no network.

Two things get pinned hard. The TOTP, because it has to match byte for byte
what Spotify checks or every token gets refused, so it is asserted against exact
values that I cross-checked against pyotp. And the whole session-token-clienttoken
dance, run against a mock transport that plays Spotify's part, so the happy path
and every typed failure are proven offline. The live network legs are doctor's
job and the dev/ spikes, not the suite.
'''
from __future__ import annotations

import base64
import json

import httpx
import pytest

from spotdlplus.core.errors import (
    SpotifyWebClientTokenFailed,
    SpotifyWebSecretStale,
    SpotifyWebSessionFailed,
)
from spotdlplus.net import spotify_web as W
from spotdlplus.net.auth import TokenProvider

# a fixed instant so the TOTP and the token expiry are deterministic
FIXED = 1700000000.0


# ----------------------------------------------------------------------------
# the TOTP, pinned to values cross-checked against pyotp
# ----------------------------------------------------------------------------

def test_totp_is_byte_identical_to_the_reference():
    s = W._FALLBACK_SECRET[1]
    assert W.generate_totp(s, now=1700000000.0) == '371599'
    assert W.generate_totp(s, now=1700000030.0) == '947302'
    assert W.generate_totp(s, now=1721000029.0) == '669094'


def test_totp_holds_across_a_window_and_flips_at_the_edge():
    s = W._FALLBACK_SECRET[1]
    # 1721000010 starts a 30-second window (it divides evenly by 30). Anything
    # inside it is the same code, the tick past it is a new one.
    assert W.generate_totp(s, now=1721000010.0) == W.generate_totp(s, now=1721000039.0)
    assert W.generate_totp(s, now=1721000010.0) != W.generate_totp(s, now=1721000040.0)


def test_secret_transform_is_stable():
    assert W._secret_to_b32(W._FALLBACK_SECRET[1]) == (
        'GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUG'
        'Q4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY')


# ----------------------------------------------------------------------------
# a mock Spotify, so the whole flow runs offline
# ----------------------------------------------------------------------------

def _app_config(version='1.2.95.test') -> str:
    blob = base64.b64encode(json.dumps({'clientVersion': version}).encode()).decode()
    return f'<html><head><script id="appServerConfig" type="text/plain">{blob}</script></head></html>'


def spotify(*, session_ok=True, token_ok=True, ct_ok=True, needs_secret=None):
    '''
    Plays Spotify's three endpoints. `needs_secret`, if set, makes /api/token
    only succeed once the totpVer reaches that version, so the autofetch heal
    can be exercised.
    '''
    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if 'open.spotify.com/api/token' in u:
            if needs_secret is not None:
                ver = int(req.url.params.get('totpVer', '0'))
                if ver < needs_secret:
                    return httpx.Response(400, text='ERROR: Invalid TOTP')
            elif not token_ok:
                return httpx.Response(400, text='ERROR: Invalid TOTP')
            return httpx.Response(200, json={
                'accessToken': 'AT', 'accessTokenExpirationTimestampMs': 9_999_999_999_000,
                'clientId': 'CID', 'isAnonymous': True})
        if 'clienttoken.spotify.com' in u:
            if not ct_ok:
                return httpx.Response(500, text='nope')
            return httpx.Response(200, json={
                'response_type': 'RESPONSE_GRANTED_TOKEN_RESPONSE',
                'granted_token': {'token': 'CT', 'refresh_after_seconds': 1800}})
        if 'code.thetadev.de' in u:
            return httpx.Response(200, json={'99': list(W._FALLBACK_SECRET[1])})
        if 'open.spotify.com' in u:   # the session page
            if not session_ok:
                return httpx.Response(503, text='down')
            return httpx.Response(200, text=_app_config(),
                                  headers={'set-cookie': 'sp_t=DEVICE; Path=/'})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _auth(**kw):
    kw.setdefault('wall_clock', lambda: FIXED)
    return W.WebPlayerAuth(**kw)


# ----------------------------------------------------------------------------
# the flow
# ----------------------------------------------------------------------------

def test_happy_path_mints_both_tokens():
    auth = _auth(transport=spotify())
    try:
        assert auth.token().value == 'AT'
        headers = auth.extra_headers()
        assert headers['client-token'] == 'CT'
        assert headers['Spotify-App-Version'] == '1.2.95.test'
    finally:
        auth.close()


def test_extra_headers_carry_a_browser_user_agent():
    # Not cosmetic: api-partner.spotify.com answers a non-browser User-Agent with
    # a flat 403 no matter the token. This override is the whole gate.
    auth = _auth(transport=spotify())
    try:
        auth.token()
        assert auth.extra_headers()['User-Agent'].startswith('Mozilla/')
    finally:
        auth.close()


def test_session_grabs_the_device_cookie():
    auth = _auth(transport=spotify())
    try:
        auth.token()
        assert auth._device_id == 'DEVICE'   # noqa: SLF001
    finally:
        auth.close()


def test_session_failure_is_typed():
    auth = _auth(transport=spotify(session_ok=False))
    try:
        with pytest.raises(SpotifyWebSessionFailed):
            auth.token()
    finally:
        auth.close()


def test_stale_secret_without_autofetch_fails_clean():
    auth = _auth(transport=spotify(token_ok=False), autofetch=False)
    try:
        with pytest.raises(SpotifyWebSecretStale):
            auth.token()
    finally:
        auth.close()


def test_stale_secret_heals_itself_when_autofetch_is_on():
    # /api/token refuses until the secret reaches v99, which the fetch supplies
    auth = _auth(transport=spotify(needs_secret=99), autofetch=True)
    try:
        assert auth.token().value == 'AT'
        assert auth._secret[0] == 99   # noqa: SLF001  # it fetched and swapped in
    finally:
        auth.close()


def test_client_token_failure_is_typed():
    auth = _auth(transport=spotify(ct_ok=False))
    try:
        auth.token()   # the bearer is fine
        with pytest.raises(SpotifyWebClientTokenFailed):
            auth.extra_headers()
    finally:
        auth.close()


def test_an_app_token_carries_no_extra_headers():
    # the base provider, the path that shipped in 1.2.0, must stay a no-op
    tp = TokenProvider(fetch=lambda: ('t', 3600.0))
    assert tp.extra_headers() == {}


# ----------------------------------------------------------------------------
# diagnose: what doctor renders
# ----------------------------------------------------------------------------

def test_diagnose_reports_every_leg_when_all_hold():
    auth = _auth(transport=spotify())
    try:
        legs = auth.diagnose()
        assert [leg.name for leg in legs] == ['session', 'access token', 'client-token']
        assert all(leg.ok for leg in legs)
    finally:
        auth.close()


def test_diagnose_stops_at_the_first_broken_leg():
    auth = _auth(transport=spotify(session_ok=False))
    try:
        legs = auth.diagnose()
        assert len(legs) == 1
        assert legs[0].name == 'session' and not legs[0].ok
        assert 'SPOTIFY_WEB_SESSION_FAILED' in legs[0].detail
    finally:
        auth.close()


def test_diagnose_names_the_stale_secret_on_the_token_leg():
    auth = _auth(transport=spotify(token_ok=False), autofetch=False)
    try:
        legs = auth.diagnose()
        assert [leg.name for leg in legs] == ['session', 'access token']
        assert legs[0].ok and not legs[1].ok
        assert 'SPOTIFY_WEB_SECRET_STALE' in legs[1].detail
    finally:
        auth.close()


# ----------------------------------------------------------------------------
# the query-hash heal: re-reading the live persisted-query ids off the bundle
# ----------------------------------------------------------------------------

BUNDLE_URL = 'https://open.spotifycdn.com/cdn/build/web-player/web-player.deadbeef.js'


def _bundle_js(pairs) -> str:
    '''Fakes a web-player bundle carrying `"op","query","<64 hex>"` registrations.'''
    return 'var noise=1;' + ''.join(f'r.exports=["{op}","query","{h}"];' for op, h in pairs)


def _refresh_transport(*, session_ok=True, html=None, bundle_ok=True, js=None):
    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if 'web-player.' in u and u.endswith('.js'):
            if not bundle_ok:
                return httpx.Response(404)
            return httpx.Response(200, text=js if js is not None else _bundle_js([('getAlbum', 'a' * 64)]))
        if 'open.spotify.com' in u:
            if not session_ok:
                return httpx.Response(503, text='down')
            return httpx.Response(200, text=html if html is not None else f'<script src="{BUNDLE_URL}"></script>')
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_extract_query_hashes_is_a_plain_scan():
    js = _bundle_js([('getAlbum', 'a' * 64), ('doThing', 'b' * 64)])
    out = W.extract_query_hashes(js)
    assert out == {'getAlbum': 'a' * 64, 'doThing': 'b' * 64}


def test_extract_query_hashes_ignores_junk_and_keeps_the_first():
    js = (f'"getAlbum","query","{"a" * 64}"'
          f'"getAlbum","query","{"c" * 64}"'      # a later dupe must not shadow the first
          '"tooShort","query","abcdef"'           # not 64 hex
          f'"notAQuery","subscription","{"d" * 64}"')   # only query/mutation count
    out = W.extract_query_hashes(js)
    assert out['getAlbum'] == 'a' * 64
    assert 'tooShort' not in out
    assert 'notAQuery' not in out


def test_find_web_player_bundle_pulls_the_url_or_none():
    assert W.find_web_player_bundle(f'<script src="{BUNDLE_URL}"></script>') == BUNDLE_URL
    assert W.find_web_player_bundle('<html>no bundle here</html>') is None


def test_latest_query_hashes_reads_the_live_bundle():
    auth = _auth(transport=_refresh_transport(
        js=_bundle_js([('getAlbum', 'a' * 64), ('searchDesktop', 'b' * 64)])))
    try:
        assert auth.latest_query_hashes() == {'getAlbum': 'a' * 64, 'searchDesktop': 'b' * 64}
    finally:
        auth.close()


def test_latest_query_hashes_is_none_when_the_session_fails():
    auth = _auth(transport=_refresh_transport(session_ok=False))
    try:
        assert auth.latest_query_hashes() is None
    finally:
        auth.close()


def test_latest_query_hashes_is_none_when_the_bundle_url_moved():
    auth = _auth(transport=_refresh_transport(html='<html>no bundle here</html>'))
    try:
        assert auth.latest_query_hashes() is None
    finally:
        auth.close()


def test_latest_query_hashes_is_none_when_the_bundle_fetch_fails():
    auth = _auth(transport=_refresh_transport(bundle_ok=False))
    try:
        assert auth.latest_query_hashes() is None
    finally:
        auth.close()


def test_latest_query_hashes_is_none_when_the_bundle_has_no_hashes():
    auth = _auth(transport=_refresh_transport(js='var x = 1; // nothing to see'))
    try:
        assert auth.latest_query_hashes() is None
    finally:
        auth.close()
