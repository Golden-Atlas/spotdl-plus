'''
spotify_web.py (providers) - the web player's data, folded into our models.

The pathfinder GraphQL API answers with unions (albumUnion, trackUnion,
artistUnion) whose fields are named for a UI, not a catalogue. The parsers fold
those shapes into the exact same Track/Album/Artist the /v1 provider produces,
and SpotifyWebProvider mirrors that provider's whole surface, so everything
downstream, resolve, expand, the matcher, the store, cannot tell which sign-in
fetched the metadata.

Three things differ from /v1, all of them consequences of talking to a UI's API
instead of a catalogue's, and all of them documented where they bite:

  - No ISRC. The web player never displays one, so its GraphQL never fetches
    one. Every Track keys on sp:<spotify id>, so the same recording on a single
    and on its album no longer collapses. This is the one thing bring-your-own-
    app still buys.
  - No batch reads. There is no "get 50 tracks" call, so tracks()/albums() fan
    out one request each. An album tracklist, though, comes back whole inside
    getAlbum, so the discography path is no worse than /v1 there.
  - No popularity, no genres, no "appears on". The overview query carries none of
    them, so they come back empty rather than faked, and MusicBrainz fills what
    it can downstream.
'''

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from ..core.errors import EntityNotFound
from ..core.models import Album, Artist, ArtistRef, EntityKind, ReleaseType, SecondaryType, Track
from ..net.spotify_pathfinder import PathfinderClient

__all__ = ['SpotifyWebProvider', 'to_album_pf', 'to_artist_pf', 'to_track_pf']


# ----------------------------------------------------------------------------
# shape helpers. Every one is null-safe, because a GraphQL union fills only the
# fields the asking query named and leaves the rest missing, not empty.
# ----------------------------------------------------------------------------

def _id_from_uri(uri: Any, explicit_id: Any = None) -> str | None:
    '''`spotify:track:ID` -> `ID`. Prefers an explicit id field when the union
    carries one (getTrack does, an album tracklist does not).'''
    if isinstance(explicit_id, str) and explicit_id:
        return explicit_id
    if isinstance(uri, str) and ':' in uri:
        return uri.rsplit(':', 1)[-1] or None
    return uri if isinstance(uri, str) and uri else None


def _pf_date(date: Any) -> str | None:
    '''
    `{'isoString': '1997-05-28T00:00:00Z', 'precision': 'DAY'}` -> '1997-05-28',
    truncated to the precision Spotify vouches for so we never claim a day we were
    never told. Matches the partial-ISO shape the /v1 path yields ('1973',
    '1973-03', '1973-03-01').
    '''
    if not isinstance(date, dict):
        return None
    iso = date.get('isoString')
    if isinstance(iso, str) and len(iso) >= 4:
        day = iso[:10]
        precision = str(date.get('precision') or 'DAY').upper()
        if precision == 'YEAR':
            return day[:4]
        if precision == 'MONTH':
            return day[:7]
        return day
    year = date.get('year')
    return str(year) if isinstance(year, int) else None


def _pf_cover(image: Any) -> str | None:
    '''Largest source by area. coverArt and avatarImage share this shape.'''
    if not isinstance(image, dict):
        return None
    sources = image.get('sources') or []
    if not sources:
        return None
    best = max(sources, key=lambda s: (s.get('width') or 0) * (s.get('height') or 0))
    return best.get('url')


def _pf_artist_refs(*containers: Any) -> tuple[ArtistRef, ...]:
    '''
    Flattens one or more `{items: [{profile: {name}, uri}]}` blocks into refs.
    Takes several because getTrack splits credits across firstArtist and
    otherArtists while an album tracklist keeps them in one artists block.
    '''
    refs: list[ArtistRef] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        for item in container.get('items') or []:
            if not isinstance(item, dict):
                continue
            name = (item.get('profile') or {}).get('name') or 'Unknown Artist'
            refs.append(ArtistRef(name=name, spotify_id=_id_from_uri(item.get('uri'), item.get('id'))))
    return tuple(refs)


def _pf_explicit(content_rating: Any) -> bool | None:
    '''`{'label': 'EXPLICIT' | 'NONE' | 'CLEAN'}` -> a bool, or None if unstated.'''
    if not isinstance(content_rating, dict):
        return None
    label = str(content_rating.get('label') or '').upper()
    if label == 'EXPLICIT':
        return True
    if label in ('NONE', 'CLEAN'):
        return False
    return None


def _pf_release_type(type_str: Any) -> ReleaseType:
    '''
    Mirrors the /v1 provider's mapping so both paths guess the same rough type;
    MusicBrainz refines it later. Compilation folds to ALBUM and carries its fact
    as a secondary type, exactly as to_album does.
    '''
    match str(type_str or '').upper():
        case 'ALBUM' | 'COMPILATION':
            return ReleaseType.ALBUM
        case 'SINGLE':
            return ReleaseType.SINGLE
        case 'EP':
            return ReleaseType.EP
        case _:
            return ReleaseType.OTHER


def _group_of_type(type_str: Any) -> str:
    '''
    Maps a release's pathfinder type onto the /v1 album_group vocabulary that
    expand.py filters on. The web discography lists only the artist's own
    releases, so 'appears_on' never comes out of here, EPs ride with singles the
    way Spotify groups them, and everything else is an album.
    '''
    match str(type_str or '').upper():
        case 'SINGLE' | 'EP':
            return 'single'
        case 'COMPILATION':
            return 'compilation'
        case _:
            return 'album'


# ----------------------------------------------------------------------------
# their JSON -> our models
# ----------------------------------------------------------------------------

def to_album_pf(album: dict[str, Any], *, appears_on: bool = False) -> Album:
    '''
    An albumUnion, or the lighter albumOfTrack a getTrack carries. Reads every
    field defensively because the two shapes overlap but are not identical:
    albumOfTrack has no label and no artists block.
    '''
    type_str = album.get('type')
    secondary: set[SecondaryType] = set()
    if str(type_str or '').upper() == 'COMPILATION':
        secondary.add(SecondaryType.COMPILATION)

    copyrights = (album.get('copyright') or {}).get('items') or []
    copyright_text: str | None = None
    if copyrights:
        chosen = next((c for c in copyrights if (c or {}).get('type') == 'C'), copyrights[0])
        copyright_text = (chosen or {}).get('text')

    counted = album.get('tracksV2') or album.get('tracks') or {}
    total = counted.get('totalCount') if isinstance(counted, dict) else None

    return Album(
        title=album.get('name') or 'Unknown Album',
        artists=_pf_artist_refs(album.get('artists')),
        spotify_id=_id_from_uri(album.get('uri'), album.get('id')),
        release_type=_pf_release_type(type_str),
        secondary_types=frozenset(secondary),
        release_date=_pf_date(album.get('date')),
        total_tracks=total,
        label=album.get('label'),
        upc=None,                       # no external_ids on the web path
        copyright=copyright_text,
        cover_url=_pf_cover(album.get('coverArt')),
        is_appears_on=appears_on,
        raw=album,
    )


def to_track_pf(track: dict[str, Any], *, album: Album | None = None) -> Track:
    '''
    A track from an album tracklist, a standalone trackUnion, a search hit, or a
    playlist item, all the same shape but for how they carry artists, the album,
    and (playlists only) a `trackDuration` where the rest say `duration`. `album`,
    when given, wins: the parent we already parsed beats a partial albumOfTrack.
    '''
    album_obj = album
    if album_obj is None and isinstance(track.get('albumOfTrack'), dict):
        album_obj = to_album_pf(track['albumOfTrack'])

    duration = (track.get('duration') or track.get('trackDuration') or {}).get('totalMilliseconds') or 0

    return Track(
        title=track.get('name') or 'Unknown Title',
        artists=_pf_artist_refs(track.get('artists'), track.get('firstArtist'), track.get('otherArtists')),
        album=album_obj,
        isrc=None,                      # the web player never exposes it; identity falls to sp:
        spotify_id=_id_from_uri(track.get('uri'), track.get('id')),
        duration_ms=int(duration),
        track_no=track.get('trackNumber'),
        disc_no=track.get('discNumber'),
        explicit=_pf_explicit(track.get('contentRating')),
        popularity=None,                # playcount is a different scale; don't fake it
        raw=track,
    )


def to_artist_pf(artist: dict[str, Any]) -> Artist:
    '''An artistUnion (overview) or the lighter artist block a search hit carries.'''
    profile = artist.get('profile') or {}
    stats = artist.get('stats') or {}
    avatar = (artist.get('visuals') or {}).get('avatarImage')
    return Artist(
        ref=ArtistRef(
            name=profile.get('name') or 'Unknown Artist',
            spotify_id=_id_from_uri(artist.get('uri'), artist.get('id')),
        ),
        genres=(),                      # the overview query carries none
        popularity=None,
        followers=stats.get('followers'),
        image_url=_pf_cover(avatar),
        raw=artist,
    )


# ----------------------------------------------------------------------------
# the provider
# ----------------------------------------------------------------------------

class SpotifyWebProvider:
    '''
    Read-only, and shaped to stand in for SpotifyProvider anywhere the pipeline
    holds one. Same method names, same return types, same streaming discipline.
    The difference is entirely in the sign-in and the endpoint: every read is a
    persisted query over the web player's API, keyed on the Spotify id because
    there is no ISRC to key on.
    '''

    #: getAlbum and fetchPlaylist page their items; 50 matches the /v1 page size.
    PAGE = 50
    #: A discography rarely runs past this, and the outer list pages if it does.
    DISCOGRAPHY_PAGE = 100

    _SEARCH_BUCKET = {
        EntityKind.ARTIST: 'artists',
        EntityKind.ALBUM: 'albumsV2',
        EntityKind.TRACK: 'tracksV2',
    }

    def __init__(self, pathfinder: PathfinderClient, *, market: str | None = None) -> None:
        self._pf = pathfinder
        #: Accepted for signature parity with SpotifyProvider. The web token is
        #: already region-scoped by the IP it was minted from, so there is no
        #: separate market to pass.
        self._market = market

    # -- single entities -----------------------------------------------------

    def track(self, track_id: str) -> Track:
        data = self._pf.query('getTrack', {'uri': f'spotify:track:{track_id}'})
        return to_track_pf(self._union(data, 'trackUnion', track_id, 'track'))

    def artist(self, artist_id: str) -> Artist:
        data = self._pf.query(
            'queryArtistOverview',
            {'uri': f'spotify:artist:{artist_id}', 'locale': '', 'includePrerelease': True})
        return to_artist_pf(self._union(data, 'artistUnion', artist_id, 'artist'))

    def album(self, album_id: str) -> Album:
        return to_album_pf(self._album_union(album_id, limit=1))

    # -- fan-outs standing in for /v1 batch reads ----------------------------

    def tracks(self, ids: Sequence[str]) -> Iterator[Track]:
        '''
        One getTrack per id, since the web player has no batch read. There is no
        ISRC to gather here the way the /v1 path does; identity is already the
        Spotify id we asked with.
        '''
        for track_id in ids:
            try:
                yield self.track(track_id)
            except EntityNotFound:
                continue

    def albums(self, ids: Sequence[str]) -> Iterator[Album]:
        '''One getAlbum header per id. A dead id is skipped, as /v1 skips a null.'''
        for album_id in ids:
            try:
                yield to_album_pf(self._album_union(album_id, limit=1))
            except EntityNotFound:
                continue

    # -- expansion -----------------------------------------------------------

    def album_track_ids(self, album_id: str) -> Iterator[str]:
        for track in self._album_tracks_raw(album_id):
            track_id = _id_from_uri(track.get('uri'), track.get('id'))
            if track_id:
                yield track_id

    def tracks_of(self, album: Album) -> Iterator[Track]:
        '''
        The tracklist for an album we already hold. getAlbum returns full track
        objects inline, so unlike /v1 there is no re-fetch, one page per 50 tracks
        and no ISRC round-trip.
        '''
        if not album.spotify_id:
            return
        for track in self._album_tracks_raw(album.spotify_id):
            yield to_track_pf(track, album=album)

    def album_tracks(self, album_id: str) -> Iterator[Track]:
        '''Fetch and stream an album's tracks, parsing the header off the first page.'''
        album: Album | None = None
        for union, tracks in self._album_pages(album_id):
            if album is None:
                album = to_album_pf(union)
            for track in tracks:
                yield to_track_pf(track, album=album)

    def artist_album_ids(
        self,
        artist_id: str,
        *,
        groups: Sequence[str] = ('album', 'single', 'compilation', 'appears_on'),
    ) -> Iterator[tuple[str, str]]:
        '''
        Yields (album_id, group) for the artist's own discography, filtered to the
        requested groups. The web path lists only what the artist released, so
        'appears_on' never appears here; a completionist run loses guest features,
        and that is the documented cost of the free path.
        '''
        wanted = set(groups)
        seen: set[str] = set()
        offset = 0
        while True:
            data = self._pf.query(
                'queryArtistDiscographyAll',
                {'uri': f'spotify:artist:{artist_id}', 'offset': offset, 'limit': self.DISCOGRAPHY_PAGE})
            allrel = (((data.get('artistUnion') or {}).get('discography') or {}).get('all') or {})
            items = allrel.get('items') or []
            for item in items:
                for release in ((item or {}).get('releases') or {}).get('items') or []:
                    album_id = _id_from_uri((release or {}).get('uri'), (release or {}).get('id'))
                    if not album_id or album_id in seen:
                        continue
                    group = _group_of_type((release or {}).get('type'))
                    if group in wanted:
                        seen.add(album_id)
                        yield album_id, group
            total = allrel.get('totalCount') or 0
            offset += len(items)
            if not items or offset >= total:
                return

    # -- playlists -----------------------------------------------------------

    def playlist(self, playlist_id: str) -> dict[str, Any]:
        '''
        The header only, shaped like the /v1 dict resolve.py reads: name, owner
        display name, and a track total.
        '''
        data = self._pf.query(
            'fetchPlaylist',
            {'uri': f'spotify:playlist:{playlist_id}', 'offset': 0, 'limit': 1,
             'enableWatchFeedEntrypoint': False})
        pl = data.get('playlistV2')
        if not isinstance(pl, dict) or not (pl.get('name') or pl.get('uri')):
            raise EntityNotFound(
                f'no playlist {playlist_id} on Spotify',
                context={'id': playlist_id, 'kind': 'playlist'})
        owner = (pl.get('ownerV2') or {}).get('data') or {}
        total = (pl.get('content') or {}).get('totalCount')
        return {
            'id': _id_from_uri(pl.get('uri')) or playlist_id,
            'name': pl.get('name') or 'Playlist',
            'description': pl.get('description'),
            'owner': {'display_name': owner.get('name')},
            'tracks': {'total': total},
        }

    def playlist_tracks(self, playlist_id: str) -> Iterator[Track]:
        '''
        Every track on a public playlist, paged. Skips episodes, unavailable
        entries, and local files, none of which carry a Spotify track id.
        '''
        offset = 0
        while True:
            data = self._pf.query(
                'fetchPlaylist',
                {'uri': f'spotify:playlist:{playlist_id}', 'offset': offset, 'limit': self.PAGE,
                 'enableWatchFeedEntrypoint': False})
            content = (data.get('playlistV2') or {}).get('content') or {}
            items = content.get('items') or []
            for item in items:
                wrapper = (item or {}).get('itemV2') or {}
                if wrapper.get('__typename') != 'TrackResponseWrapper':
                    continue
                track = wrapper.get('data')
                if not isinstance(track, dict) or not _id_from_uri(track.get('uri'), track.get('id')):
                    continue
                yield to_track_pf(track)
            total = content.get('totalCount') or 0
            offset += len(items)
            if not items or offset >= total:
                return

    # -- search --------------------------------------------------------------

    def search(self, query: str, kind: EntityKind, *, limit: int = 10) -> list[dict[str, Any]]:
        '''
        Returns hits shaped like the /v1 dicts resolve.py reads (id, name,
        popularity, artists), translated out of the searchV2 bucket for `kind`.
        Popularity is always 0 here, so ranking leans on name similarity, which is
        the documented cost of the free path.
        '''
        capped = max(1, min(limit, 10))     # /v1 search caps at 10 now; match it
        data = self._pf.query('searchDesktop', {
            'searchTerm': query, 'offset': 0, 'limit': capped, 'numberOfTopResults': capped,
            'includeAudiobooks': False, 'includePreReleases': False})
        bucket = (data.get('searchV2') or {}).get(self._SEARCH_BUCKET[kind]) or {}

        out: list[dict[str, Any]] = []
        for item in bucket.get('items') or []:
            node = (item or {}).get('item', item)      # tracks nest under 'item'; albums/artists don't
            node_data = (node or {}).get('data') if isinstance(node, dict) else None
            if isinstance(node_data, dict):
                hit = self._search_dict(kind, node_data)
                if hit['id']:
                    out.append(hit)

        if not out:
            raise EntityNotFound(
                f'nothing on Spotify matches {query!r} as a {kind}',
                context={'query': query, 'kind': str(kind)})
        return out

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _union(data: dict[str, Any], key: str, entity_id: str, kind: str) -> dict[str, Any]:
        union = data.get(key)
        if not isinstance(union, dict) or str(union.get('__typename') or '').lower() in ('notfound', 'genericerror'):
            raise EntityNotFound(
                f'no {kind} {entity_id} on Spotify',
                context={'id': entity_id, 'kind': kind})
        return union

    def _album_union(self, album_id: str, *, limit: int, offset: int = 0) -> dict[str, Any]:
        data = self._pf.query(
            'getAlbum',
            {'locale': '', 'uri': f'spotify:album:{album_id}', 'offset': offset, 'limit': limit})
        union = data.get('albumUnion')
        if not isinstance(union, dict) or not (union.get('uri') or union.get('name')):
            raise EntityNotFound(
                f'no album {album_id} on Spotify',
                context={'id': album_id, 'kind': 'album'})
        return union

    def _album_pages(self, album_id: str) -> Iterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
        '''Yields (albumUnion, tracks-on-this-page) until the tracklist is exhausted.'''
        offset = 0
        while True:
            union = self._album_union(album_id, limit=self.PAGE, offset=offset)
            paged = union.get('tracksV2') or {}
            raw = paged.get('items') or []
            tracks = [t for t in ((it or {}).get('track') for it in raw) if isinstance(t, dict)]
            yield union, tracks
            total = paged.get('totalCount') or 0
            offset += len(raw)
            if not raw or offset >= total:
                return

    def _album_tracks_raw(self, album_id: str) -> Iterator[dict[str, Any]]:
        for _union, tracks in self._album_pages(album_id):
            yield from tracks

    @staticmethod
    def _search_dict(kind: EntityKind, data: dict[str, Any]) -> dict[str, Any]:
        '''One searchV2 hit, reshaped into the /v1 dict resolve.py expects.'''
        if kind is EntityKind.ARTIST:
            return {
                'id': _id_from_uri(data.get('uri'), data.get('id')),
                'name': (data.get('profile') or {}).get('name') or '',
                'popularity': 0,
                'artists': [],
            }
        artists = [
            {'name': (a.get('profile') or {}).get('name') or ''}
            for a in ((data.get('artists') or {}).get('items') or [])
        ]
        return {
            'id': _id_from_uri(data.get('uri'), data.get('id')),
            'name': data.get('name') or '',
            'popularity': 0,
            'artists': artists,
        }
