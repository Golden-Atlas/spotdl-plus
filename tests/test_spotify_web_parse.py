'''
test_spotify_web_parse.py - pathfinder JSON -> our models, pinned to real shapes.

The parsers are pure, so they get held against the captured fixtures (Spotify's
own shapes) for the real values, and against hand-built inputs for the edges the
fixtures don't happen to cover: partial dates, content ratings, compilation
folding, and the ISRC-shaped hole that forces sp: identity.
'''
from __future__ import annotations

import json
from pathlib import Path

from spotdlplus.core.models import ReleaseType, SecondaryType
from spotdlplus.providers.spotify_web import (
    _id_from_uri,
    _pf_cover,
    _pf_date,
    _pf_explicit,
    _pf_release_type,
    to_album_pf,
    to_artist_pf,
    to_track_pf,
)

FIX = Path(__file__).parent / 'fixtures' / 'pathfinder'


def load(name: str) -> dict:
    return json.loads((FIX / f'{name}.json').read_text(encoding='utf-8'))


# ----------------------------------------------------------------------------
# real fixtures: the values Spotify actually returns
# ----------------------------------------------------------------------------

def test_album_from_real_getalbum():
    a = to_album_pf(load('get_album_okcomputer')['data']['albumUnion'])
    assert a.title == 'OK Computer'
    assert a.spotify_id == '6dVIqQ8qmQ5GBnJ9shOYGE'
    assert a.release_type is ReleaseType.ALBUM
    assert a.release_date == '1997-05-28'
    assert a.year == 1997
    assert a.total_tracks == 12
    assert a.label == 'XL Recordings'
    assert a.copyright == '1997 XL Recordings Ltd'
    assert a.cover_url and a.cover_url.startswith('https://i.scdn.co/')
    assert a.artists[0].name == 'Radiohead'
    assert a.artists[0].spotify_id == '4Z8W4fKeB5YxbusRsdQVPb'
    assert a.upc is None                        # no external_ids on the web path


def test_track_from_real_album_tracklist_keys_on_spotify_id():
    au = load('get_album_okcomputer')['data']['albumUnion']
    album = to_album_pf(au)
    first = au['tracksV2']['items'][0]['track']
    t = to_track_pf(first, album=album)
    assert t.title == 'Airbag'
    assert t.spotify_id == '7c378mlmubSu7NGkLFa4sN'
    assert t.track_no == 1
    assert t.disc_no == 1
    assert t.duration_ms == 287880
    assert t.explicit is False
    assert t.isrc is None
    # the whole consequence of the free path in one assertion: no ISRC, sp: key
    assert t.identity == 'sp:7c378mlmubSu7NGkLFa4sN'
    assert t.artists[0].name == 'Radiohead'
    assert t.album is album                     # the passed parent wins


def test_track_from_real_gettrack_builds_album_from_albumoftrack():
    tu = load('get_track_airbag')['data']['trackUnion']
    t = to_track_pf(tu)
    assert t.title == 'Airbag'
    assert t.spotify_id == '7c378mlmubSu7NGkLFa4sN'
    assert t.duration_ms == 287880
    assert t.album is not None
    assert t.album.title == 'OK Computer'
    assert t.album.spotify_id == '6dVIqQ8qmQ5GBnJ9shOYGE'
    assert any(a.name == 'Radiohead' for a in t.artists)


def test_artist_from_real_overview():
    au = load('artist_overview_radiohead')['data']['artistUnion']
    ar = to_artist_pf(au)
    assert ar.name == 'Radiohead'
    assert ar.ref.spotify_id == '4Z8W4fKeB5YxbusRsdQVPb'
    assert ar.followers == 16656121
    assert ar.image_url and ar.image_url.startswith('https://i.scdn.co/')
    assert ar.genres == ()                      # overview carries none
    assert ar.popularity is None


def test_search_hits_reuse_the_same_three_parsers():
    s = load('search_creep')['data']['searchV2']
    t = to_track_pf(s['tracksV2']['items'][0]['item']['data'])
    assert t.title == 'Creep' and t.spotify_id
    al = to_album_pf(s['albumsV2']['items'][0]['data'])
    assert al.title and al.spotify_id
    ar = to_artist_pf(s['artists']['items'][0]['data'])
    assert ar.name and ar.ref.spotify_id


# ----------------------------------------------------------------------------
# the edges the fixtures don't happen to hit
# ----------------------------------------------------------------------------

def test_id_from_uri_prefers_explicit_and_handles_junk():
    assert _id_from_uri('spotify:track:ABC') == 'ABC'
    assert _id_from_uri('spotify:track:ABC', 'XYZ') == 'XYZ'   # explicit id wins
    assert _id_from_uri(None, 'XYZ') == 'XYZ'
    assert _id_from_uri(None) is None
    assert _id_from_uri('') is None
    assert _id_from_uri('bareid') == 'bareid'                  # no colon, taken as-is


def test_pf_date_truncates_to_the_precision_spotify_vouches_for():
    assert _pf_date({'isoString': '1997-05-28T00:00:00Z', 'precision': 'DAY'}) == '1997-05-28'
    assert _pf_date({'isoString': '1997-05-28T00:00:00Z', 'precision': 'MONTH'}) == '1997-05'
    assert _pf_date({'isoString': '1997-05-28T00:00:00Z', 'precision': 'YEAR'}) == '1997'
    assert _pf_date({'year': 1997}) == '1997'
    assert _pf_date(None) is None
    assert _pf_date({}) is None


def test_pf_cover_picks_the_largest_source_and_survives_nulls():
    img = {'sources': [
        {'url': 'small', 'width': 64, 'height': 64},
        {'url': 'big', 'width': 640, 'height': 640},
        {'url': 'mid', 'width': 300, 'height': 300},
    ]}
    assert _pf_cover(img) == 'big'
    assert _pf_cover({'sources': []}) is None
    assert _pf_cover(None) is None
    assert _pf_cover({'sources': [{'url': 'only', 'width': None, 'height': None}]}) == 'only'


def test_pf_explicit_reads_the_content_rating_label():
    assert _pf_explicit({'label': 'EXPLICIT'}) is True
    assert _pf_explicit({'label': 'NONE'}) is False
    assert _pf_explicit({'label': 'CLEAN'}) is False
    assert _pf_explicit({'label': 'WHATEVER'}) is None
    assert _pf_explicit(None) is None


def test_pf_release_type_mirrors_the_v1_mapping():
    assert _pf_release_type('ALBUM') is ReleaseType.ALBUM
    assert _pf_release_type('SINGLE') is ReleaseType.SINGLE
    assert _pf_release_type('EP') is ReleaseType.EP
    assert _pf_release_type('COMPILATION') is ReleaseType.ALBUM
    assert _pf_release_type('anything else') is ReleaseType.OTHER


def test_compilation_folds_to_album_and_carries_the_secondary_fact():
    a = to_album_pf({'name': 'Hits', 'type': 'COMPILATION', 'uri': 'spotify:album:X'})
    assert a.release_type is ReleaseType.ALBUM
    assert SecondaryType.COMPILATION in a.secondary_types


def test_the_sparsest_inputs_still_produce_valid_models():
    t = to_track_pf({})
    assert t.title == 'Unknown Title'
    assert t.duration_ms == 0
    assert t.artists == ()
    assert t.identity.startswith('fuzzy:')      # no ids at all, last resort
    a = to_album_pf({})
    assert a.title == 'Unknown Album'
    assert a.spotify_id is None
    ar = to_artist_pf({})
    assert ar.name == 'Unknown Artist'
    assert ar.followers is None
