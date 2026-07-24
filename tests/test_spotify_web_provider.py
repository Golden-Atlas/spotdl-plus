'''
test_spotify_web_provider.py - the web provider standing in for the /v1 one.

Held against a fake pathfinder that serves the real captured fixtures, so every
method is proven on Spotify's actual shapes: the single reads, the fan-outs that
replace the batch endpoints, discography filtering, playlist paging, search
translated into the /v1 dicts resolve.py expects, and every not-found. Synthetic
pages cover the tracklists too long to fit one request.
'''
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spotdlplus.core.errors import EntityNotFound
from spotdlplus.core.models import Album, EntityKind
from spotdlplus.providers.spotify_web import SpotifyWebProvider

FIX = Path(__file__).parent / 'fixtures' / 'pathfinder'


def load(name: str) -> dict:
    return json.loads((FIX / f'{name}.json').read_text(encoding='utf-8'))


def data_of(name: str) -> dict:
    return load(name)['data']


class FakePF:
    '''
    Stands in for PathfinderClient. Maps an operation to a data block, or to a
    callable(variables) -> data block when a test needs paging. Records calls so
    the exact query and variables can be asserted.
    '''

    def __init__(self, mapping: dict) -> None:
        self._mapping = mapping
        self.calls: list[tuple[str, dict]] = []

    def query(self, operation: str, variables: dict) -> dict:
        self.calls.append((operation, dict(variables)))
        value = self._mapping[operation]
        return value(variables) if callable(value) else value


def provider(mapping: dict) -> tuple[SpotifyWebProvider, FakePF]:
    fake = FakePF(mapping)
    return SpotifyWebProvider(fake), fake


# ----------------------------------------------------------------------------
# single entities
# ----------------------------------------------------------------------------

def test_track_reads_gettrack_and_asks_by_uri():
    sp, fake = provider({'getTrack': data_of('get_track_airbag')})
    t = sp.track('7c378mlmubSu7NGkLFa4sN')
    assert t.title == 'Airbag'
    assert t.identity == 'sp:7c378mlmubSu7NGkLFa4sN'    # no ISRC on the web path
    assert fake.calls == [('getTrack', {'uri': 'spotify:track:7c378mlmubSu7NGkLFa4sN'})]


def test_artist_reads_the_overview():
    sp, fake = provider({'queryArtistOverview': data_of('artist_overview_radiohead')})
    a = sp.artist('4Z8W4fKeB5YxbusRsdQVPb')
    assert a.name == 'Radiohead'
    assert a.followers == 16656121
    assert a.genres == ()                               # not faked
    assert fake.calls[0][0] == 'queryArtistOverview'


def test_album_reads_a_getalbum_header():
    sp, fake = provider({'getAlbum': data_of('get_album_okcomputer')})
    a = sp.album('6dVIqQ8qmQ5GBnJ9shOYGE')
    assert a.title == 'OK Computer'
    assert a.total_tracks == 12
    assert a.label == 'XL Recordings'
    op, variables = fake.calls[0]
    assert op == 'getAlbum' and variables['limit'] == 1   # header only


# ----------------------------------------------------------------------------
# the fan-outs that replace /v1 batch reads
# ----------------------------------------------------------------------------

def test_tracks_fans_out_and_skips_the_missing():
    def get_track(variables):
        if variables['uri'].endswith('GOOD'):
            return data_of('get_track_airbag')
        return {'trackUnion': None}                       # a dead id
    sp, fake = provider({'getTrack': get_track})
    got = list(sp.tracks(['GOOD', 'DEAD']))
    assert len(got) == 1 and got[0].title == 'Airbag'
    assert len(fake.calls) == 2                           # one request per id


def test_albums_fans_out_one_header_each():
    sp, fake = provider({'getAlbum': data_of('get_album_okcomputer')})
    got = list(sp.albums(['a', 'b', 'c']))
    assert len(got) == 3 and all(al.title == 'OK Computer' for al in got)
    assert len(fake.calls) == 3


# ----------------------------------------------------------------------------
# album expansion: full tracks inline, no re-fetch
# ----------------------------------------------------------------------------

def test_album_tracks_streams_the_whole_tracklist_with_the_header_attached():
    sp, _ = provider({'getAlbum': data_of('get_album_okcomputer')})
    tracks = list(sp.album_tracks('6dVIqQ8qmQ5GBnJ9shOYGE'))
    assert len(tracks) == 12
    assert tracks[0].title == 'Airbag'
    assert all(t.album is not None and t.album.title == 'OK Computer' for t in tracks)
    assert all(t.isrc is None for t in tracks)


def test_tracks_of_attaches_the_album_we_already_hold():
    sp, _ = provider({'getAlbum': data_of('get_album_okcomputer')})
    album = Album(title='Held', spotify_id='6dVIqQ8qmQ5GBnJ9shOYGE')
    tracks = list(sp.tracks_of(album))
    assert len(tracks) == 12
    assert all(t.album is album for t in tracks)         # the passed parent wins


def test_album_track_ids_yields_only_ids():
    sp, _ = provider({'getAlbum': data_of('get_album_okcomputer')})
    ids = list(sp.album_track_ids('6dVIqQ8qmQ5GBnJ9shOYGE'))
    assert len(ids) == 12
    assert ids[0] == '7c378mlmubSu7NGkLFa4sN'
    assert all(isinstance(i, str) for i in ids)


def _synthetic_album(total: int):
    '''A getAlbum responder that pages `total` tracks by (offset, limit).'''
    def responder(variables):
        offset, limit = variables['offset'], variables['limit']
        n = min(limit, max(0, total - offset))
        items = [{'track': {'name': f'T{offset + i}', 'uri': f'spotify:track:id{offset + i}',
                            'duration': {'totalMilliseconds': 1000}, 'trackNumber': offset + i + 1}}
                 for i in range(n)]
        return {'albumUnion': {'uri': 'spotify:album:BIG', 'name': 'Big', 'type': 'ALBUM',
                              'tracksV2': {'totalCount': total, 'items': items}}}
    return responder


def test_a_long_tracklist_pages_until_exhausted():
    sp, fake = provider({'getAlbum': _synthetic_album(120)})
    tracks = list(sp.album_tracks('BIG'))
    assert len(tracks) == 120
    assert [t.title for t in tracks[:2]] == ['T0', 'T1']
    assert tracks[-1].title == 'T119'
    # 120 tracks at 50/page = 3 pages, and it does not fire a wasted empty page
    assert len(fake.calls) == 3


def test_a_tracklist_that_fills_exactly_one_page_does_not_fetch_again():
    sp, fake = provider({'getAlbum': _synthetic_album(50)})
    assert len(list(sp.album_tracks('BIG'))) == 50
    assert len(fake.calls) == 1


# ----------------------------------------------------------------------------
# artist discography: filtered to groups, never 'appears_on'
# ----------------------------------------------------------------------------

def test_artist_album_ids_yields_ids_with_groups_and_no_appears_on():
    sp, _ = provider({'queryArtistDiscographyAll': data_of('artist_discography_radiohead')})
    pairs = list(sp.artist_album_ids('4Z8W4fKeB5YxbusRsdQVPb'))
    assert pairs, 'the discography should not be empty'
    ids = [aid for aid, _ in pairs]
    assert len(ids) == len(set(ids))                     # de-duplicated
    assert all(group in ('album', 'single', 'compilation') for _, group in pairs)
    assert all(group != 'appears_on' for _, group in pairs)   # the web path can't see features


def test_artist_album_ids_honours_the_requested_groups():
    disco = data_of('artist_discography_radiohead')
    sp, _ = provider({'queryArtistDiscographyAll': disco})
    only_albums = list(sp.artist_album_ids('x', groups=('album',)))
    assert only_albums, 'Radiohead has studio albums'
    assert all(group == 'album' for _, group in only_albums)
    # asking for only singles must not smuggle an album through
    sp2, _ = provider({'queryArtistDiscographyAll': disco})
    singles = list(sp2.artist_album_ids('x', groups=('single',)))
    assert all(group == 'single' for _, group in singles)


# ----------------------------------------------------------------------------
# playlists
# ----------------------------------------------------------------------------

def test_playlist_header_matches_the_v1_shape_resolve_reads():
    sp, _ = provider({'fetchPlaylist': data_of('playlist_tophits')})
    head = sp.playlist('37i9dQZF1DXcBWIGoYBM5M')
    assert head['name'].startswith('Today')
    assert (head.get('tracks') or {}).get('total') == 50
    assert (head.get('owner') or {}).get('display_name')  # a name, not None


def test_playlist_tracks_streams_real_tracks_and_skips_non_tracks():
    sp, _ = provider({'fetchPlaylist': data_of('playlist_tophits')})
    tracks = list(sp.playlist_tracks('37i9dQZF1DXcBWIGoYBM5M'))
    assert tracks, 'the playlist has tracks'
    assert all(t.spotify_id for t in tracks)             # every one keyed on sp:
    assert all(t.duration_ms > 0 for t in tracks)        # trackDuration was read


def test_playlist_tracks_skips_episodes_and_local_files():
    sp, _ = provider({'fetchPlaylist': {'playlistV2': {'name': 'Mixed', 'uri': 'spotify:playlist:X',
        'content': {'totalCount': 3, 'items': [
            {'itemV2': {'__typename': 'TrackResponseWrapper',
                        'data': {'name': 'Real', 'uri': 'spotify:track:keep',
                                 'trackDuration': {'totalMilliseconds': 1000}}}},
            {'itemV2': {'__typename': 'EpisodeResponseWrapper',
                        'data': {'name': 'Podcast', 'uri': 'spotify:episode:drop'}}},
            {'itemV2': {'__typename': 'TrackResponseWrapper',
                        'data': {'name': 'Local', 'uri': ''}}},   # local file, no id
        ]}}}})
    tracks = list(sp.playlist_tracks('X'))
    assert [t.title for t in tracks] == ['Real']


# ----------------------------------------------------------------------------
# search: translated into the /v1 dicts resolve.py consumes
# ----------------------------------------------------------------------------

def test_search_tracks_returns_v1_shaped_dicts():
    sp, _ = provider({'searchDesktop': data_of('search_creep')})
    hits = sp.search('Radiohead Creep', EntityKind.TRACK)
    assert hits and hits[0]['name'] == 'Creep'
    first = hits[0]
    assert first['id'] and isinstance(first['id'], str)
    assert first['popularity'] == 0                      # web has none; documented
    assert isinstance(first['artists'], list)
    assert all('name' in a for a in first['artists'])


def test_search_albums_and_artists_use_their_buckets():
    sp, _ = provider({'searchDesktop': data_of('search_creep')})
    albums = sp.search('Radiohead', EntityKind.ALBUM)
    assert albums and all(a['id'] for a in albums)
    artists = sp.search('Radiohead', EntityKind.ARTIST)
    assert artists and all(a['id'] for a in artists)
    assert artists[0]['name']


def test_search_with_no_hits_raises_not_found():
    sp, _ = provider({'searchDesktop': {'searchV2': {'tracksV2': {'items': []}}}})
    with pytest.raises(EntityNotFound):
        sp.search('asdkjfhaslkdjfh', EntityKind.TRACK)


# ----------------------------------------------------------------------------
# not-found is typed, everywhere a union can come back null
# ----------------------------------------------------------------------------

def test_missing_track_is_entity_not_found():
    sp, _ = provider({'getTrack': {'trackUnion': None}})
    with pytest.raises(EntityNotFound):
        sp.track('nope')


def test_missing_album_is_entity_not_found():
    sp, _ = provider({'getAlbum': {'albumUnion': None}})
    with pytest.raises(EntityNotFound):
        sp.album('nope')


def test_missing_artist_is_entity_not_found():
    sp, _ = provider({'queryArtistOverview': {'artistUnion': None}})
    with pytest.raises(EntityNotFound):
        sp.artist('nope')


def test_missing_playlist_is_entity_not_found():
    sp, _ = provider({'fetchPlaylist': {'playlistV2': None}})
    with pytest.raises(EntityNotFound):
        sp.playlist('nope')


# ----------------------------------------------------------------------------
# it really is a drop-in: every method the pipeline calls exists and is callable
# ----------------------------------------------------------------------------

def test_it_covers_the_whole_provider_surface_the_pipeline_uses():
    from spotdlplus.providers.spotify import SpotifyProvider
    used = ('track', 'artist', 'album', 'tracks', 'albums', 'album_track_ids',
            'tracks_of', 'album_tracks', 'playlist', 'playlist_tracks',
            'artist_album_ids', 'search')
    for name in used:
        assert callable(getattr(SpotifyWebProvider, name, None)), f'web provider missing {name}'
        assert callable(getattr(SpotifyProvider, name, None)), f'/v1 provider missing {name}'
