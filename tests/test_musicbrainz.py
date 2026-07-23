'''
test_musicbrainz.py - the fixtures are real responses, captured 2026-07-10.

The heart of it: Spotify calls `Hail to the Thief (Live Recordings 2003-2009)` an
`album_type: album`. MusicBrainz calls it `primary-type: Album,
secondary-types: ["Live"]`. That difference is the only reason the `canonical`
profile can honestly promise you no live records.
'''

from __future__ import annotations

import httpx
import pytest

from spotdlplus.core.errors import ProviderFailed
from spotdlplus.core.models import Album, ArtistRef, ReleaseType, SecondaryType
from spotdlplus.net.http import HttpClient
from spotdlplus.net.ratelimit import HostLimiter
from spotdlplus.providers.musicbrainz import MusicBrainzProvider

RADIOHEAD = ArtistRef(name='Radiohead')

# Exactly what the API returned for barcode 191404156353.
LIVE_RG = {
    'id': '44419ec0-d44a-4ec5-875c-d08f5c7ec13c',
    'title': 'Hail to the Thief: Live Recordings 2003–2009',   # colon, en dash
    'primary-type': 'Album',
    'secondary-types': ['Live'],
}

# The plain studio album carries no `secondary-types` key at all.
STUDIO_RG = {'id': 'rg-studio', 'title': 'Hail to the Thief', 'primary-type': 'Album', 'score': 94}


def mb(handler) -> MusicBrainzProvider:
    '''MusicBrainz at one request per second would make this file take a minute.'''
    http = HttpClient(
        transport=httpx.MockTransport(handler),
        limiter=HostLimiter({}, fallback=(1000.0, 1000)),
    )
    return MusicBrainzProvider(http)


def json_ok(payload) -> httpx.Response:
    return httpx.Response(200, json=payload)


# ----------------------------------------------------------------------------
# barcode: the exact path
# ----------------------------------------------------------------------------

def test_a_barcode_resolves_to_a_release_group_in_one_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return json_ok({'releases': [{'id': 'rel-1', 'release-group': LIVE_RG}]})

    rg = mb(handler).release_group_by_barcode('191404156353')

    assert len(calls) == 1
    assert 'barcode' in calls[0] and '191404156353' in calls[0]
    assert rg.mbid == '44419ec0-d44a-4ec5-875c-d08f5c7ec13c'
    assert rg.primary is ReleaseType.ALBUM
    assert rg.secondary == {SecondaryType.LIVE}
    assert rg.is_live and not rg.is_compilation


def test_an_unknown_barcode_is_none_not_an_explosion():
    assert mb(lambda r: json_ok({'releases': []})).release_group_by_barcode('000') is None
    assert mb(lambda r: httpx.Response(404)).release_group_by_barcode('000') is None


def test_a_release_group_with_no_secondary_types_is_simply_a_record():
    handler = lambda r: json_ok({'releases': [{'release-group': STUDIO_RG}]})
    rg = mb(handler).release_group_by_barcode('634904078164')
    assert rg.secondary == frozenset()
    assert not rg.is_live


# ----------------------------------------------------------------------------
# search: the path that punctuation breaks
# ----------------------------------------------------------------------------

def test_the_search_query_is_unquoted_and_normalized():
    '''
    Regression. A quoted phrase query for Spotify's title returns ZERO hits,
    because MusicBrainz spells the same record with a colon and an en dash.
    Measured against the live API.
    '''
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params['query'])
        return json_ok({'release-groups': []})

    mb(handler).release_group_by_search('Radiohead', 'Hail to the Thief (Live Recordings 2003-2009)')

    q = seen[0]
    assert 'releasegroup:(hail to the thief live recordings 2003 2009)' in q
    assert 'releasegroup:"' not in q, 'a quoted phrase can never match across punctuation'
    assert 'artist:"Radiohead"' in q


def test_a_loose_search_is_verified_strictly():
    '''
    The live query returns 105 hits. The right one is rank 1 at score 100. Rank 2
    is a different record at score 94. Only an exact normalized title is believed.
    '''
    payload = {'release-groups': [
        dict(LIVE_RG, score=100),
        STUDIO_RG,                                # score 94, wrong record
    ]}
    rg = mb(lambda r: json_ok(payload)).release_group_by_search(
        'Radiohead', 'Hail to the Thief (Live Recordings 2003-2009)')
    assert rg.mbid == LIVE_RG['id']
    assert rg.secondary == {SecondaryType.LIVE}


def test_a_high_score_on_the_wrong_record_is_still_the_wrong_record():
    payload = {'release-groups': [dict(STUDIO_RG, score=100)]}
    rg = mb(lambda r: json_ok(payload)).release_group_by_search(
        'Radiohead', 'Hail to the Thief (Live Recordings 2003-2009)')
    assert rg is None, 'the titles do not match, whatever their scorer thinks'


def test_a_confident_scorer_below_the_floor_is_ignored():
    payload = {'release-groups': [dict(LIVE_RG, score=40)]}
    rg = mb(lambda r: json_ok(payload)).release_group_by_search(
        'Radiohead', 'Hail to the Thief (Live Recordings 2003-2009)')
    assert rg is None


def test_lucene_operators_in_an_artist_name_are_escaped():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params['query'])
        return json_ok({'release-groups': []})

    mb(handler).release_group_by_search('AC/DC', 'Back in Black')
    assert r'AC\/DC' in seen[0]


# ----------------------------------------------------------------------------
# type vocabulary
# ----------------------------------------------------------------------------

@pytest.mark.parametrize('mb_type, ours', [
    ('Live', SecondaryType.LIVE),
    ('Compilation', SecondaryType.COMPILATION),
    ('Soundtrack', SecondaryType.SOUNDTRACK),
    ('DJ-mix', SecondaryType.DJ_MIX),
    ('Mixtape/Street', SecondaryType.MIXTAPE),
    ('Demo', SecondaryType.DEMO),
])
def test_their_vocabulary_maps_onto_ours(mb_type, ours):
    payload = {'releases': [{'release-group': dict(LIVE_RG, **{'secondary-types': [mb_type]})}]}
    rg = mb(lambda r: json_ok(payload)).release_group_by_barcode('x')
    assert rg.secondary == {ours}


def test_a_secondary_type_we_do_not_know_is_dropped_not_guessed():
    '''An unknown type must never silently become a known one and change filtering.'''
    payload = {'releases': [{'release-group':
                             dict(LIVE_RG, **{'secondary-types': ['Live', 'Field recording']})}]}
    rg = mb(lambda r: json_ok(payload)).release_group_by_barcode('x')
    assert rg.secondary == {SecondaryType.LIVE}


@pytest.mark.parametrize('mb_primary, ours', [
    ('Album', ReleaseType.ALBUM),
    ('Single', ReleaseType.SINGLE),
    ('EP', ReleaseType.EP),
    ('Broadcast', ReleaseType.BROADCAST),
    ('Nonsense', ReleaseType.OTHER),
])
def test_primary_types_map_and_unknowns_become_other(mb_primary, ours):
    payload = {'releases': [{'release-group': dict(LIVE_RG, **{'primary-type': mb_primary})}]}
    assert mb(lambda r: json_ok(payload)).release_group_by_barcode('x').primary is ours


# ----------------------------------------------------------------------------
# enrichment is best-effort, and must never make things worse
# ----------------------------------------------------------------------------

def _spotify_album(**kw) -> Album:
    base = dict(title='Hail to the Thief (Live Recordings 2003-2009)',
                artists=(RADIOHEAD,), upc='191404156353',
                release_type=ReleaseType.ALBUM, release_date='2025-08-13')
    return Album(**{**base, **kw})


def test_enrichment_tells_us_a_live_album_is_live():
    '''The whole point. Spotify said `album`. It is a live record.'''
    handler = lambda r: json_ok({'releases': [{'release-group': LIVE_RG}]})
    before = _spotify_album()
    assert before.secondary_types == frozenset()

    after = mb(handler).enrich_album(before)
    assert SecondaryType.LIVE in after.secondary_types
    assert after.release_group_id == LIVE_RG['id']


def test_enrichment_merges_rather_than_replaces_what_spotify_knew():
    '''Spotify already knew this was a compilation. MusicBrainz not repeating it
    does not make it stop being one.'''
    handler = lambda r: json_ok({'releases': [{'release-group': LIVE_RG}]})
    before = _spotify_album(secondary_types=frozenset({SecondaryType.COMPILATION}))
    after = mb(handler).enrich_album(before)
    assert after.secondary_types == {SecondaryType.COMPILATION, SecondaryType.LIVE}


def test_an_album_musicbrainz_never_heard_of_survives_unchanged():
    handler = lambda r: json_ok({'releases': [], 'release-groups': []})
    before = _spotify_album()
    assert mb(handler).enrich_album(before) == before


def test_a_broken_musicbrainz_never_fails_a_track():
    '''Enrichment that fails must leave you no worse off than enrichment you skipped.'''
    def handler(r):
        return httpx.Response(500)

    before = _spotify_album()
    after = mb(handler).enrich_album(before)
    assert after == before

    # ...but the underlying error is still typed, for anyone who does want it.
    with pytest.raises(ProviderFailed):
        mb(handler).release_group_by_barcode('191404156353')


def test_barcode_is_preferred_over_search_because_it_cannot_be_wrong():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return json_ok({'releases': [{'release-group': LIVE_RG}]})

    mb(handler).release_group_for_album(_spotify_album())
    assert paths == ['/ws/2/release'], 'no search request was ever made'


def test_search_is_the_fallback_when_there_is_no_barcode():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return json_ok({'release-groups': [dict(LIVE_RG, score=100)]})

    rg = mb(handler).release_group_for_album(_spotify_album(upc=None))
    assert paths == ['/ws/2/release-group']
    assert rg.is_live


# ----------------------------------------------------------------------------
# recordings
# ----------------------------------------------------------------------------

def test_an_isrc_resolves_to_a_recording():
    payload = {'recordings': [{'id': 'rec-1', 'title': 'Airbag', 'length': 287900}]}
    rec = mb(lambda r: json_ok(payload)).recording_by_isrc('gbaye9701274')
    assert rec.mbid == 'rec-1' and rec.length_ms == 287900


def test_an_unknown_isrc_is_none():
    assert mb(lambda r: httpx.Response(404)).recording_by_isrc('XX000000000') is None
