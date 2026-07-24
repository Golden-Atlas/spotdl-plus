'''
spotify_pathfinder.py - the web player's own GraphQL API.

The /v1 catalogue endpoints Spotify used to serve to any client are gone as of
early 2026. The batch album/track/artist reads were removed outright, and what
survives walls an anonymous token behind a multi-hour retry-after. The web
player never used /v1 for this; it talks to api-partner.spotify.com over a
persisted-query GraphQL API, and that one Spotify has to keep alive because
open.spotify.com runs on it. So we talk to it too, with the same paired
signature WebPlayerAuth mints.

Two things rotate and both self-heal. The TOTP secret lives in WebPlayerAuth.
The persisted-query hashes live here: a hash names a query Spotify has
registered server-side, and it re-issues them whenever the web player ships a
build. We bundle the current set so a fresh install works on its first run, and
when a bundled hash stops being recognised we re-read the live set from the web
player's own script and try once more. The bundle is the floor, not the ceiling.

This is transport only. It hands back the `data` block and lets the provider
above decide what a null union means, because that is an entity question, not a
wire question. The one wire concern it owns is the hash: a rejected hash is not
a bad request, it is a stale id, and it gets its own typed error that points at
bring-your-own-app.
'''

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..core.errors import ProviderFailed, SpotifyWebQueryStale
from .auth import TokenProvider
from .http import HttpClient

__all__ = ['BUNDLED_HASHES', 'PF_URL', 'PathfinderClient']

PF_URL = 'https://api-partner.spotify.com/pathfinder/v1/query'

#: The persisted-query hashes the current web player uses, captured 2026-07.
#: Each names a query Spotify registered server-side. These are the floor: when
#: one stops being recognised the client re-reads the live set (see the
#: refresher) and only falls back to a typed error if that also comes up empty.
#: Refresh this snapshot on a release so a brand-new install starts current.
BUNDLED_HASHES: dict[str, str] = {
    'getAlbum': 'b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10',
    'getTrack': '1a2f0cce77c90a4a5b1730beecc4da7e34290d684324c16663bf09a268ebce48',
    'queryArtistOverview': 'ae0e2958a4ab645b35ca19ac04d0495ae12d9c5d7b7286217674801a9aab281a',
    'queryArtistDiscographyAll': '5e07d323febb57b4a56a42abbf781490e58764aa45feb6e3dc0591564fc56599',
    'searchDesktop': 'db61238974d27839a136c9dc02bfdbe3fab7635f21cf85976ebff9a1ee281345',
    'fetchPlaylist': 'e4b2953f160e58e38ac025d79b5a9b3aceee5c4c716598e9830bfceb69faff5f',
}

#: Reads the current hashes off the live web player and returns them (or None if
#: it couldn't). Injected so the wire logic is testable without a network, and so
#: the fragile JS-parsing only ever runs as a fallback, never on the hot path.
HashRefresher = Callable[[], dict[str, str] | None]


class PathfinderClient:
    '''
    One GraphQL verb over the shared HttpClient, so the pathfinder host gets the
    same breaker, the same limiter, and the same single-flight 401 retry as
    every other request. The paired signature (bearer + client-token) comes from
    the WebPlayerAuth passed as `auth`, exactly the way an app token would.
    '''

    def __init__(
        self,
        http: HttpClient,
        auth: TokenProvider,
        *,
        hashes: dict[str, str] | None = None,
        refresher: HashRefresher | None = None,
    ) -> None:
        self._http = http
        self._auth = auth
        self._hashes = dict(hashes if hashes is not None else BUNDLED_HASHES)
        self._refresher = refresher
        self._refreshed = False

    def query(self, operation: str, variables: dict[str, Any]) -> dict[str, Any]:
        '''
        Run a persisted query and return its `data` block. Retries exactly once,
        and only after re-reading the live hashes, if Spotify says it doesn't
        recognise the one we sent. Any other GraphQL error is the provider's to
        interpret except an unrecognised-query error that survives the refresh,
        which is a stale build and says so.
        '''
        body = self._post(operation, variables)
        if self._is_query_miss(body) and self._refresh_hashes():
            body = self._post(operation, variables)

        if self._is_query_miss(body):
            raise SpotifyWebQueryStale(
                f'Spotify no longer recognises our "{operation}" query id',
                context={'operation': operation, 'refreshed': self._refreshed})

        errors = body.get('errors')
        if errors:
            raise ProviderFailed(
                f'pathfinder returned an error on "{operation}"',
                context={'operation': operation, 'errors': errors[:3]})

        data = body.get('data')
        if not isinstance(data, dict):
            raise ProviderFailed(
                f'pathfinder returned no data block on "{operation}"',
                context={'operation': operation, 'body_keys': sorted(body)[:8]})
        return data

    # -- internals -----------------------------------------------------------

    def _post(self, operation: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            sha256 = self._hashes[operation]
        except KeyError as exc:
            raise ProviderFailed(
                f'no persisted-query hash bundled for "{operation}"',
                context={'operation': operation, 'known': sorted(self._hashes)}) from exc

        params = {
            'operationName': operation,
            'variables': json.dumps(variables, separators=(',', ':')),
            'extensions': json.dumps(
                {'persistedQuery': {'version': 1, 'sha256Hash': sha256}},
                separators=(',', ':')),
        }
        resp = self._http.request('POST', PF_URL, params=params, auth=self._auth)
        body = resp.json()
        if not isinstance(body, dict):
            raise ProviderFailed(
                f'pathfinder answered "{operation}" with a non-object body',
                context={'operation': operation, 'type': type(body).__name__})
        return body

    @staticmethod
    def _is_query_miss(body: dict[str, Any]) -> bool:
        '''
        Apollo's persisted-query protocol answers an unknown hash with a 200 and
        a PersistedQueryNotFound error, not a 4xx, so it never reaches the http
        classifier. This is the one GraphQL error the transport itself acts on.
        '''
        for err in (body.get('errors') or []):
            message = str((err or {}).get('message', '')).lower().replace(' ', '')
            if 'persistedquerynotfound' in message or 'persistedquerynotsupported' in message:
                return True
        return False

    def _refresh_hashes(self) -> bool:
        '''Re-read the live hashes once. Merges anything new over the bundle.'''
        if self._refreshed or self._refresher is None:
            return False
        self._refreshed = True
        fresh = self._refresher()
        if not fresh:
            return False
        self._hashes.update(fresh)
        return True
