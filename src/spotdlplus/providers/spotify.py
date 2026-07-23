'''
spotify.py - the Spotify catalogue

Two things about this API that every naive client gets wrong, and they shape
every method in here.

Simplified objects aren't tracks. An album's tracklist comes back without
ISRCs, so dedupe would be flying blind. We re-fetch through /v1/tracks, one
extra request per 50, and get a real primary key out of it.

`album_group` is the only place Spotify admits the artist is a guest instead of
the owner, and it doesn't survive the full-album re-fetch, so we carry it
across by hand.
'''

from __future__ import annotations

import base64
import re
from collections.abc import Iterator, Sequence
from dataclasses import replace
from typing import Any

from ..core.errors import BadRequest, CredentialsRejected, EntityNotFound
from ..core.events import EventBus
from ..core.models import Album, Artist, ArtistRef, EntityKind, ReleaseType, Track
from ..net.auth import TokenProvider
from ..net.http import HttpClient

API = 'https://api.spotify.com/v1'
TOKEN_URL = 'https://accounts.spotify.com/api/token'

#: Hard limits imposed by the API. Exceed them and you get a 400, not a truncation.
MAX_TRACK_IDS = 50
MAX_ALBUM_IDS = 20
PAGE = 50

#: `spotify:track:ID`, `https://open.spotify.com/track/ID?si=...`, and the
#: regional variant `open.spotify.com/intl-de/album/ID`.
_URI = re.compile(r'spotify:(track|album|artist|playlist):([A-Za-z0-9]+)')
_URL = re.compile(
    r'open\.spotify\.com/(?:intl-[a-z]{2}/)?(track|album|artist|playlist)/([A-Za-z0-9]+)'
)

_KINDS = {
    'track': EntityKind.TRACK,
    'album': EntityKind.ALBUM,
    'artist': EntityKind.ARTIST,
    'playlist': EntityKind.PLAYLIST,
}


def parse_spotify_ref(text: str) -> tuple[EntityKind, str] | None:
    '''Recognise a Spotify URL or URI. Returns None for a bare search string.'''
    for pattern in (_URI, _URL):
        m = pattern.search(text.strip())
        if m:
            return _KINDS[m.group(1)], m.group(2)
    return None


# ----------------------------------------------------------------------------
# auth
# ----------------------------------------------------------------------------

def spotify_token_provider(
    http: HttpClient,
    client_id: str,
    client_secret: str,
    *,
    bus: EventBus | None = None,
    run_id: str = '',
) -> TokenProvider:
    '''
    Client-credentials flow. There's no user login and no refresh token, so a
    refresh just means minting a new one. That's exactly why single flight matters
    here, since nothing rate-limits you faster than eight workers each minting
    their own.
    '''
    basic = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode('ascii')

    def fetch() -> tuple[str, float]:
        try:
            resp = http.request(
                'POST', TOKEN_URL,
                headers={'Authorization': f'Basic {basic}'},
                data={'grant_type': 'client_credentials'},
            )
        except BadRequest as exc:
            # Spotify answers bad credentials with 400 invalid_client, not 401.
            # Left as BadRequest it would read as "our bug". It is not.
            raise CredentialsRejected(
                'Spotify rejected the client id/secret pair',
                context=exc.context, cause=exc,
            ) from exc

        body = resp.json()
        return body['access_token'], float(body.get('expires_in', 3600))

    return TokenProvider(fetch, name='spotify', bus=bus, run_id=run_id)


# ----------------------------------------------------------------------------
# mapping: their JSON -> our models
# ----------------------------------------------------------------------------

def _artist_ref(d: dict[str, Any]) -> ArtistRef:
    return ArtistRef(name=d.get('name') or 'Unknown Artist', spotify_id=d.get('id'))


def _release_type(album_type: str | None, album_group: str | None = None) -> ReleaseType:
    match (album_type or '').lower():
        case 'album':
            return ReleaseType.ALBUM
        case 'single':
            return ReleaseType.SINGLE
        case 'compilation':
            return ReleaseType.ALBUM   # secondary type carries the 'compilation' fact
        case _:
            return ReleaseType.OTHER


def _cover(images: list[dict[str, Any]] | None) -> str | None:
    '''Largest first. Spotify sorts descending, but do not rely on it.'''
    if not images:
        return None
    best = max(images, key=lambda i: (i.get('width') or 0) * (i.get('height') or 0))
    return best.get('url')


def to_album(d: dict[str, Any], *, appears_on: bool = False) -> Album:
    from ..core.models import SecondaryType

    secondary: set[SecondaryType] = set()
    if (d.get('album_type') or '').lower() == 'compilation':
        secondary.add(SecondaryType.COMPILATION)

    copyrights = d.get('copyrights') or []
    return Album(
        title=d.get('name') or 'Unknown Album',
        artists=tuple(_artist_ref(a) for a in d.get('artists', [])),
        spotify_id=d.get('id'),
        release_type=_release_type(d.get('album_type'), d.get('album_group')),
        secondary_types=frozenset(secondary),
        release_date=d.get('release_date'),
        total_tracks=d.get('total_tracks'),
        label=d.get('label'),
        upc=(d.get('external_ids') or {}).get('upc'),
        copyright=copyrights[0]['text'] if copyrights else None,
        cover_url=_cover(d.get('images')),
        is_appears_on=appears_on or (d.get('album_group') == 'appears_on'),
        raw=d,
    )


def to_track(d: dict[str, Any], *, album: Album | None = None) -> Track:
    album_obj = album
    if album_obj is None and d.get('album'):
        album_obj = to_album(d['album'])

    return Track(
        title=d.get('name') or 'Unknown Title',
        artists=tuple(_artist_ref(a) for a in d.get('artists', [])),
        album=album_obj,
        isrc=(d.get('external_ids') or {}).get('isrc'),
        spotify_id=d.get('id'),
        duration_ms=int(d.get('duration_ms') or 0),
        track_no=d.get('track_number'),
        disc_no=d.get('disc_number'),
        explicit=d.get('explicit'),
        popularity=d.get('popularity'),
        raw=d,
    )


# ----------------------------------------------------------------------------
# the provider
# ----------------------------------------------------------------------------

class SpotifyProvider:
    '''Read-only. Every list method streams. Nothing here accumulates a discography.'''

    def __init__(
        self,
        http: HttpClient,
        auth: TokenProvider,
        *,
        market: str | None = None,
    ) -> None:
        self._http = http
        self._auth = auth
        self._market = market

    # -- plumbing ------------------------------------------------------------

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        if self._market and 'market' not in params:
            params['market'] = self._market
        clean = {k: v for k, v in params.items() if v is not None}
        return self._http.get(f'{API}{path}', params=clean, auth=self._auth).json()

    def _pages(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        '''Follow `next` until it is null. Yields items, never a list of them.'''
        params.setdefault('limit', PAGE)
        page = self._get(path, **params)
        # A playlist page nests under a key. A search result nests deeper.
        while True:
            for item in page.get('items') or []:
                if item is not None:
                    yield item
            nxt = page.get('next')
            if not nxt:
                return
            page = self._http.get(nxt, auth=self._auth).json()

    @staticmethod
    def _chunks(ids: Sequence[str], size: int) -> Iterator[list[str]]:
        for i in range(0, len(ids), size):
            yield list(ids[i:i + size])

    # -- single entities -----------------------------------------------------

    def track(self, track_id: str) -> Track:
        return to_track(self._get(f'/tracks/{track_id}'))

    def artist(self, artist_id: str) -> Artist:
        d = self._get(f'/artists/{artist_id}')
        return Artist(
            ref=ArtistRef(name=d.get('name') or 'Unknown Artist', spotify_id=d.get('id')),
            genres=tuple(d.get('genres') or ()),
            popularity=d.get('popularity'),
            followers=(d.get('followers') or {}).get('total'),
            image_url=_cover(d.get('images')),
            raw=d,
        )

    def album(self, album_id: str) -> Album:
        return to_album(self._get(f'/albums/{album_id}'))

    # -- batched re-fetches: where the ISRCs actually come from ---------------

    def tracks(self, ids: Sequence[str]) -> Iterator[Track]:
        '''
                Full track objects, 50 at a time. This is the only endpoint that returns
                `external_ids.isrc`. The simplified objects in an album tracklist do not.

        '''
        for chunk in self._chunks(ids, MAX_TRACK_IDS):
            body = self._get('/tracks', ids=','.join(chunk))
            for d in body.get('tracks') or []:
                if d:
                    yield to_track(d)

    def albums(self, ids: Sequence[str]) -> Iterator[Album]:
        '''Full album objects, 20 at a time. Carries label, copyright, and UPC.'''
        for chunk in self._chunks(ids, MAX_ALBUM_IDS):
            body = self._get('/albums', ids=','.join(chunk))
            for d in body.get('albums') or []:
                if d:
                    yield to_album(d)

    # -- expansion -----------------------------------------------------------

    def album_track_ids(self, album_id: str) -> Iterator[str]:
        for item in self._pages(f'/albums/{album_id}/tracks'):
            if item.get('id'):
                yield item['id']

    def tracks_of(self, album: Album) -> Iterator[Track]:
        '''
        Gets the tracklist for an album we already have, re-fetched through /v1/tracks
        so every track carries its ISRC. One extra request per 50 buys a stable primary
        key.
        '''
        if not album.spotify_id:
            return
        ids = list(self.album_track_ids(album.spotify_id))
        for track in self.tracks(ids):
            yield replace(track, album=album)

    def album_tracks(self, album_id: str) -> Iterator[Track]:
        yield from self.tracks_of(self.album(album_id))

    def playlist(self, playlist_id: str) -> dict[str, Any]:
        '''Just the header. Name, owner, count. The items are paged separately.'''
        return self._get(f'/playlists/{playlist_id}',
                         fields='id,name,description,owner(display_name),tracks(total)')

    def artist_album_ids(
        self,
        artist_id: str,
        *,
        groups: Sequence[str] = ('album', 'single', 'compilation', 'appears_on'),
    ) -> Iterator[tuple[str, str]]:
        '''
        Yields (album_id, album_group). The group is the only signal that the artist is
        a guest on a record, and it's the difference between a discography and a pile
        of features.
        '''
        seen: set[str] = set()
        for item in self._pages(f'/artists/{artist_id}/albums',
                                include_groups=','.join(groups)):
            aid = item.get('id')
            if aid and aid not in seen:
                seen.add(aid)
                yield aid, item.get('album_group') or 'album'

    def playlist_tracks(self, playlist_id: str) -> Iterator[Track]:
        '''
                Playlist items carry full track objects, ISRC included, no re-fetch needed.
                Skips local files and podcast episodes, which have no meaning here.

        '''
        for item in self._pages(f'/playlists/{playlist_id}/tracks',
                                additional_types='track'):
            t = item.get('track')
            if not t or t.get('is_local') or t.get('type') != 'track':
                continue
            yield to_track(t)

    # -- search --------------------------------------------------------------

    def search(self, query: str, kind: EntityKind, *, limit: int = 10) -> list[dict[str, Any]]:
        body = self._get('/search', q=query, type=str(kind), limit=limit)
        bucket = body.get(f'{kind}s') or {}
        items = [i for i in (bucket.get('items') or []) if i]
        if not items:
            raise EntityNotFound(
                f'nothing on Spotify matches {query!r} as a {kind}',
                context={'query': query, 'kind': str(kind)},
            )
        return items
