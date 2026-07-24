'''
test_spotify_pathfinder.py - the web player GraphQL transport, proven offline.

Every wire concern the PathfinderClient owns is pinned here against a mock
Spotify: the exact persisted-query request it sends, the paired signature it
carries, the one-shot self-heal when a hash is rejected, and each typed failure.
Success bodies are the real captured fixtures, so the shapes are Spotify's, not
mine. The live reads are doctor's job, not the suite's.
'''
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from spotdlplus.core.errors import ProviderFailed, RateLimited, SpotifyWebQueryStale
from spotdlplus.net.auth import TokenProvider
from spotdlplus.net.http import HttpClient
from spotdlplus.net.spotify_pathfinder import BUNDLED_HASHES, PF_URL, PathfinderClient

FIX = Path(__file__).parent / 'fixtures' / 'pathfinder'


def _fixture(name: str) -> dict:
    return json.loads((FIX / f'{name}.json').read_text(encoding='utf-8'))


class _WebAuth(TokenProvider):
    '''A stand-in for WebPlayerAuth: a real bearer plus the client-token header.'''

    def __init__(self) -> None:
        super().__init__(fetch=lambda: ('BEARER', 3600.0), name='spotify-web')

    def extra_headers(self) -> dict[str, str]:
        return {'client-token': 'CTOKEN'}


def make(responder, **pf_kwargs):
    '''A PathfinderClient over a mock transport that defers to `responder`.'''
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return responder(req, seen)

    http = HttpClient(transport=httpx.MockTransport(handler))
    return PathfinderClient(http, _WebAuth(), **pf_kwargs), http, seen


def _sent_hash(req: httpx.Request) -> str:
    return json.loads(req.url.params['extensions'])['persistedQuery']['sha256Hash']


# ----------------------------------------------------------------------------
# the happy path: the exact request, and the signature that keeps us off the wall
# ----------------------------------------------------------------------------

def test_query_sends_the_persisted_query_and_carries_the_paired_signature():
    pf, http, seen = make(lambda req, seen: httpx.Response(200, json=_fixture('get_album_okcomputer')))
    try:
        data = pf.query('getAlbum',
                        {'uri': 'spotify:album:6dVIqQ8qmQ5GBnJ9shOYGE', 'offset': 0, 'limit': 50})
        assert data['albumUnion']['name'] == 'OK Computer'

        req = seen[0]
        assert req.method == 'POST'
        assert str(req.url).startswith(PF_URL)
        assert req.url.params['operationName'] == 'getAlbum'
        assert _sent_hash(req) == BUNDLED_HASHES['getAlbum']
        assert json.loads(req.url.params['variables'])['uri'] == 'spotify:album:6dVIqQ8qmQ5GBnJ9shOYGE'
        # the whole reason the free path works: bearer AND client-token, together
        assert req.headers['authorization'] == 'Bearer BEARER'
        assert req.headers['client-token'] == 'CTOKEN'
    finally:
        http.close()


# ----------------------------------------------------------------------------
# self-heal: a rotated hash re-reads the live set and retries, exactly once
# ----------------------------------------------------------------------------

def test_a_rejected_hash_reheals_from_the_refresher_and_retries_once():
    fresh = {'getAlbum': 'f' * 64}

    def responder(req, seen):
        if _sent_hash(req) == BUNDLED_HASHES['getAlbum']:
            return httpx.Response(200, json={'errors': [{'message': 'PersistedQueryNotFound'}], 'data': None})
        assert _sent_hash(req) == fresh['getAlbum']      # the retry uses the healed id
        return httpx.Response(200, json=_fixture('get_album_okcomputer'))

    refreshed = {'n': 0}

    def refresher():
        refreshed['n'] += 1
        return fresh

    pf, http, seen = make(responder, refresher=refresher)
    try:
        data = pf.query('getAlbum', {'uri': 'x'})
        assert data['albumUnion']['name'] == 'OK Computer'
        assert refreshed['n'] == 1        # re-read the live set exactly once
        assert len(seen) == 2             # first miss, then the healed retry
    finally:
        http.close()


def test_a_rejected_hash_with_no_refresher_is_a_typed_stale_error():
    pf, http, seen = make(
        lambda req, seen: httpx.Response(200, json={'errors': [{'message': 'PersistedQueryNotFound'}]}))
    try:
        with pytest.raises(SpotifyWebQueryStale):
            pf.query('getAlbum', {'uri': 'x'})
        assert len(seen) == 1             # no refresher, so no retry
    finally:
        http.close()


def test_a_hash_that_stays_rejected_after_refresh_gives_up_without_looping():
    refreshed = {'n': 0}

    def refresher():
        refreshed['n'] += 1
        return {'getAlbum': 'a' * 64}

    pf, http, seen = make(
        lambda req, seen: httpx.Response(200, json={'errors': [{'message': 'PersistedQueryNotFound'}]}),
        refresher=refresher)
    try:
        with pytest.raises(SpotifyWebQueryStale):
            pf.query('getAlbum', {'uri': 'x'})
        assert refreshed['n'] == 1        # refreshed once, never in a loop
        assert len(seen) == 2             # original + one retry, then it stops
    finally:
        http.close()


# ----------------------------------------------------------------------------
# every other failure is typed, and a 429 stays the pace-able kind
# ----------------------------------------------------------------------------

def test_a_non_persisted_query_error_surfaces_as_provider_failed():
    pf, http, seen = make(
        lambda req, seen: httpx.Response(200, json={'errors': [{'message': 'internal server error'}], 'data': None}))
    try:
        with pytest.raises(ProviderFailed):
            pf.query('getAlbum', {'uri': 'x'})
    finally:
        http.close()


def test_a_body_with_no_data_block_is_provider_failed():
    pf, http, seen = make(lambda req, seen: httpx.Response(200, json={'extensions': {}}))
    try:
        with pytest.raises(ProviderFailed):
            pf.query('getAlbum', {'uri': 'x'})
    finally:
        http.close()


def test_an_operation_with_no_bundled_hash_never_hits_the_wire():
    pf, http, seen = make(lambda req, seen: httpx.Response(200, json={'data': {}}))
    try:
        with pytest.raises(ProviderFailed):
            pf.query('noSuchOperation', {})
        assert seen == []                 # refused before sending anything
    finally:
        http.close()


def test_a_429_from_pathfinder_is_the_normal_pace_able_rate_limit():
    pf, http, seen = make(
        lambda req, seen: httpx.Response(429, headers={'retry-after': '15'}, json={}))
    try:
        with pytest.raises(RateLimited):
            pf.query('getAlbum', {'uri': 'x'})
    finally:
        http.close()
