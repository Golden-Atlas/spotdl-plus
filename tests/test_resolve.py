'''
test_resolve.py - one door in, and it refuses to guess.

The failure this file prevents: you type `Creep`, and the tool quietly resolves it
to the artist named Creep and downloads their entire discography.
'''

from __future__ import annotations

import pytest

from spotdlplus.core.errors import AmbiguousQuery, EntityNotFound
from spotdlplus.core.models import Album, ArtistRef, Artist, EntityKind, Track
from spotdlplus.pipeline.resolve import (
    AMBIGUITY_MARGIN,
    Resolution,
    candidates,
    resolve,
)
from spotdlplus.providers.spotify import parse_spotify_ref


# ----------------------------------------------------------------------------
# a stub catalogue. only what the resolver reaches for.
# ----------------------------------------------------------------------------

class FakeSpotify:
    def __init__(self, catalogue: dict[EntityKind, list[dict]] | None = None) -> None:
        self.catalogue = catalogue or {}
        self.searches: list[tuple[str, EntityKind]] = []

    def search(self, query, kind, *, limit=10):
        self.searches.append((query, kind))
        items = self.catalogue.get(kind) or []
        if not items:
            raise EntityNotFound(f'no {kind}', context={'query': query})
        return items[:limit]

    def albums(self, ids):
        for i in ids:
            item = next(a for a in self.catalogue[EntityKind.ALBUM] if a['id'] == i)
            yield Album(title=item['name'], spotify_id=i, raw=item)

    def artist(self, artist_id):
        return Artist(ref=ArtistRef(name='Radiohead', spotify_id=artist_id))

    def album(self, album_id):
        return Album(title='OK Computer', artists=(ArtistRef(name='Radiohead'),),
                     spotify_id=album_id)

    def track(self, track_id):
        return Track(title='Creep', artists=(ArtistRef(name='Radiohead'),),
                     spotify_id=track_id, duration_ms=238_000)

    def playlist(self, playlist_id):
        return {'id': playlist_id, 'name': 'late night',
                'owner': {'display_name': 'kai'}, 'tracks': {'total': 42}}


def artist_hit(name, pop):
    return {'id': f'ar-{name}', 'name': name, 'popularity': pop}


def track_hit(name, pop, by='Someone'):
    return {'id': f'tr-{name}', 'name': name, 'popularity': pop,
            'artists': [{'name': by}]}


def album_hit(name, pop, by='Someone'):
    return {'id': f'al-{name}', 'name': name, 'popularity': pop,
            'artists': [{'name': by}]}


# ----------------------------------------------------------------------------
# references are not guesses
# ----------------------------------------------------------------------------

@pytest.mark.parametrize('text, kind, ident', [
    ('spotify:track:6b2oQwSGFkzsMtQruIWm2p', EntityKind.TRACK, '6b2oQwSGFkzsMtQruIWm2p'),
    ('https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE', EntityKind.ALBUM, '6dVIqQ8qmQ5GBnJ9shOYGE'),
    ('https://open.spotify.com/artist/4Z8W4fKeB5YxbusRsdQVPb?si=abc', EntityKind.ARTIST, '4Z8W4fKeB5YxbusRsdQVPb'),
    ('open.spotify.com/intl-de/playlist/37i9dQZF1DX', EntityKind.PLAYLIST, '37i9dQZF1DX'),
])
def test_urls_and_uris_are_recognised(text, kind, ident):
    assert parse_spotify_ref(text) == (kind, ident)


def test_a_bare_string_is_not_a_reference():
    assert parse_spotify_ref('Radiohead') is None


def test_a_url_resolves_without_searching_anything():
    sp = FakeSpotify()
    r = resolve('https://open.spotify.com/artist/4Z8W4fKeB5YxbusRsdQVPb', sp)
    assert r == Resolution(EntityKind.ARTIST, '4Z8W4fKeB5YxbusRsdQVPb', 'Radiohead')
    assert r.basis == 'url' and r.confidence == 1.0
    assert sp.searches == [], 'a URL is not a question'


def test_a_playlist_url_carries_its_size_for_the_preview():
    r = resolve('spotify:playlist:37i9dQZF1DX', FakeSpotify())
    assert r.name == 'late night'
    assert '42 tracks' in r.detail and 'kai' in r.detail


# ----------------------------------------------------------------------------
# prefixes: you said what you meant
# ----------------------------------------------------------------------------

def test_a_prefix_pins_the_type_even_when_popularity_disagrees():
    sp = FakeSpotify({
        EntityKind.ARTIST: [artist_hit('Creep', 12)],
        EntityKind.TRACK: [track_hit('Creep', 95, by='Radiohead')],
    })
    r = resolve('artist:Creep', sp)
    assert r.kind is EntityKind.ARTIST and r.spotify_id == 'ar-Creep'
    assert r.basis == 'prefix'
    assert sp.searches == [('Creep', EntityKind.ARTIST)], 'nothing else was searched'


def test_song_is_an_alias_for_track():
    sp = FakeSpotify({EntityKind.TRACK: [track_hit('Creep', 95)]})
    assert resolve('song: Creep', sp).kind is EntityKind.TRACK


def test_playlists_cannot_be_searched_by_name():
    with pytest.raises(EntityNotFound, match='paste the playlist URL'):
        resolve('playlist:late night', FakeSpotify())


def test_a_prefix_still_refuses_a_garbage_match():
    '''
    Regression, found live: `song:Sunsetz Cigarettes After Sex` resolved to
    Oliver Tree's "Cigarettes" because the prefix path took the top hit with no
    similarity floor. A prefix pins the KIND, not the answer.
    '''
    sp = FakeSpotify({
        EntityKind.TRACK: [track_hit('Cigarettes', 80, by='Oliver Tree')],
    })
    with pytest.raises(EntityNotFound, match='resembles'):
        resolve('song:Sunsetz Cigarettes After Sex', sp)


def test_a_prefix_tie_asks_instead_of_guessing():
    sp = FakeSpotify({
        EntityKind.TRACK: [track_hit('Duster', 60, by='A'),
                           track_hit('Duster', 60, by='B')],
    })
    with pytest.raises(AmbiguousQuery):
        resolve('song:Duster', sp)


# ----------------------------------------------------------------------------
# bare strings: the part that must not guess wrong
# ----------------------------------------------------------------------------

def test_a_famous_song_beats_an_obscure_artist_of_the_same_name():
    '''`Creep` must not download the discography of a band called Creep.'''
    sp = FakeSpotify({
        EntityKind.ARTIST: [artist_hit('Creep', 14)],
        EntityKind.TRACK: [track_hit('Creep', 92, by='Radiohead')],
        EntityKind.ALBUM: [],
    })
    r = resolve('Creep', sp)
    assert r.kind is EntityKind.TRACK
    assert r.basis == 'scored'


def test_a_famous_artist_beats_an_obscure_song_of_the_same_name():
    sp = FakeSpotify({
        EntityKind.ARTIST: [artist_hit('Radiohead', 82)],
        EntityKind.TRACK: [track_hit('Radiohead', 41, by='Talking Heads')],
        EntityKind.ALBUM: [],
    })
    assert resolve('Radiohead', sp).kind is EntityKind.ARTIST


def test_an_album_can_win_because_we_fetch_its_popularity():
    '''
    Album search results carry no popularity. Without the batched re-fetch an
    album can never win a tie, and `OK Computer` resolves to a cover version.
    '''
    sp = FakeSpotify({
        EntityKind.ARTIST: [],
        EntityKind.TRACK: [track_hit('OK Computer', 20, by='A Cover Band')],
        EntityKind.ALBUM: [album_hit('OK Computer', 78, by='Radiohead')],
    })
    r = resolve('OK Computer', sp)
    assert r.kind is EntityKind.ALBUM and r.spotify_id == 'al-OK Computer'


def test_a_reissue_does_not_hijack_the_original():
    '''
    `fuzz.WRatio` scores `OK Computer` against `OK Computer OKNOTOK 1997 2017` at
    90 through partial matching. `fuzz.ratio` does not, which is why we use it.
    '''
    sp = FakeSpotify({
        EntityKind.ARTIST: [],
        EntityKind.TRACK: [],
        EntityKind.ALBUM: [album_hit('OK Computer', 70),
                           album_hit('OK Computer OKNOTOK 1997 2017', 99)],
    })
    r = resolve('OK Computer', sp)
    assert r.name == 'OK Computer', 'popularity must not overcome a worse name match'


def test_two_equally_good_readings_stop_rather_than_flip_a_coin():
    sp = FakeSpotify({
        EntityKind.ARTIST: [artist_hit('Duster', 60)],
        EntityKind.TRACK: [track_hit('Duster', 60, by='Someone Else')],
        EntityKind.ALBUM: [],
    })
    with pytest.raises(AmbiguousQuery) as exc:
        resolve('Duster', sp)

    assert 'could be' in str(exc.value)
    assert len(exc.value.context['candidates']) == 2
    assert 'pick' in exc.value.remedy


def test_a_picker_resolves_the_ambiguity_instead_of_raising():
    sp = FakeSpotify({
        EntityKind.ARTIST: [artist_hit('Duster', 60)],
        EntityKind.TRACK: [track_hit('Duster', 60)],
        EntityKind.ALBUM: [],
    })
    r = resolve('Duster', sp, pick=lambda cands: next(
        i for i, c in enumerate(cands) if c.kind is EntityKind.ARTIST))
    assert r.kind is EntityKind.ARTIST and r.basis == 'picked'


def test_a_picker_that_declines_does_not_silently_pick_anyway():
    sp = FakeSpotify({
        EntityKind.ARTIST: [artist_hit('Duster', 60)],
        EntityKind.TRACK: [track_hit('Duster', 60)],
        EntityKind.ALBUM: [],
    })
    with pytest.raises(EntityNotFound, match='nothing chosen'):
        resolve('Duster', sp, pick=lambda _: None)


def test_a_clear_winner_is_never_put_to_a_vote():
    picked = []
    sp = FakeSpotify({
        EntityKind.ARTIST: [artist_hit('Radiohead', 82)],
        EntityKind.TRACK: [track_hit('Radiohead', 10)],
        EntityKind.ALBUM: [],
    })
    resolve('Radiohead', sp, pick=lambda c: picked.append(c) or 0)
    assert picked == [], 'the picker must only be consulted when it is close'


def test_names_that_do_not_resemble_the_query_are_not_readings():
    sp = FakeSpotify({
        EntityKind.ARTIST: [artist_hit('Something Else Entirely', 99)],
        EntityKind.TRACK: [], EntityKind.ALBUM: [],
    })
    assert candidates('Radiohead', sp) == []
    with pytest.raises(EntityNotFound, match='resembles'):
        resolve('Radiohead', sp)


def test_nothing_at_all_is_not_a_query():
    with pytest.raises(EntityNotFound):
        resolve('   ', FakeSpotify())


def test_the_margin_is_the_one_the_module_names():
    assert AMBIGUITY_MARGIN == 0.06


# ----------------------------------------------------------------------------
# 'Creep Radiohead' is Radiohead, not the more-popular Glee cover
# ----------------------------------------------------------------------------

def test_naming_the_artist_beats_a_more_popular_cover():
    sp = FakeSpotify({EntityKind.TRACK: [
        # the cover puts the artist's name in its TITLE and is more popular
        track_hit('Creep - Cover of Radiohead', 78, by='Glee Cast'),
        # the real thing has a plain title and is a bit less popular here
        track_hit('Creep', 62, by='Radiohead'),
    ]})
    ranked = candidates('Creep Radiohead', sp)
    assert ranked, 'the real track must clear the similarity floor'
    assert ranked[0].detail == 'Radiohead', f'picked {ranked[0].detail!r}'


def test_a_bare_title_is_unaffected_by_the_artist_rule():
    '''No artist named -> nothing changes. The popular reading still wins.'''
    sp = FakeSpotify({EntityKind.TRACK: [
        track_hit('Creep', 80, by='Radiohead'),
        track_hit('Creep', 40, by='Nobody'),
    ]})
    ranked = candidates('Creep', sp)
    assert ranked[0].popularity == 80


def test_a_bare_artist_name_resolves_to_the_artist_not_their_track():
    '''
    Regression: the artist-in-query rule must NOT fire when the query IS just the
    artist name. 'Ethel Cain' is an artist search, and a track by her (even a popular
    one) must not outrank the artist herself.
    '''
    sp = FakeSpotify({
        EntityKind.ARTIST: [artist_hit('Ethel Cain', 74)],
        EntityKind.TRACK: [track_hit('Crush', 88, by='Ethel Cain')],  # popular track by her
    })
    ranked = candidates('Ethel Cain', sp)
    assert ranked[0].kind is EntityKind.ARTIST, f'picked {ranked[0].kind} {ranked[0].name!r}'


def test_title_plus_artist_still_prefers_that_artists_track():
    '''The intended behavior still holds when a title IS present.'''
    sp = FakeSpotify({EntityKind.TRACK: [
        track_hit('Crush - Cover of Ethel Cain', 90, by='Some Cover Act'),
        track_hit('Crush', 70, by='Ethel Cain'),
    ]})
    ranked = candidates('Crush Ethel Cain', sp)
    assert ranked[0].detail == 'Ethel Cain'
