'''
test_expand.py - a discography becomes a stream of tracks, and the junk is named.

The two claims under test:

  1. `canonical` throws out the live album, the compilation, and the appears-on
     records, using MusicBrainz facts rather than title strings.
  2. Nothing is ever accumulated. Expansion is lazy from end to end, so an artist
     with 600 tracks costs the same memory as one with 6.
'''

from __future__ import annotations

from dataclasses import replace

import pytest

from spotdlplus.core.errors import ProviderFailed
from spotdlplus.core.events import (
    EntityDiscovered,
    EventBus,
    ReleaseFiltered,
    StageFinished,
    Warned,
)
from spotdlplus.core.models import (
    CANONICAL,
    COMPLETIONIST,
    STUDIO_ONLY,
    Album,
    ArtistRef,
    EntityKind,
    ReleaseType,
    SecondaryType,
    Track,
)
from spotdlplus.pipeline.expand import expand
from spotdlplus.pipeline.resolve import Resolution
from spotdlplus.pipeline.selection import (
    exclusion_reason,
    needs_enrichment,
    spotify_album_groups,
)

RADIOHEAD = ArtistRef(name='Radiohead', spotify_id='rh')


def album(title, aid, *, rtype=ReleaseType.ALBUM, secondary=(), appears_on=False, tracks=2):
    return Album(title=title, artists=(RADIOHEAD,), spotify_id=aid, release_type=rtype,
                 secondary_types=frozenset(secondary), is_appears_on=appears_on,
                 total_tracks=tracks)


# Radiohead's real discography, in miniature. Every one of these is
# `album_type: album` as far as Spotify is concerned.
OK_COMPUTER = album('OK Computer', 'a1')
IN_RAINBOWS = album('In Rainbows', 'a2')
LIVE_REC = album('Hail to the Thief (Live Recordings)', 'a3', secondary=[SecondaryType.LIVE])
KID_A_MNESIA = album('KID A MNESIA', 'a4', secondary=[SecondaryType.COMPILATION])
CREEP_SINGLE = album('Creep', 'a5', rtype=ReleaseType.SINGLE)
A_GUEST_SPOT = album('Some Compilation', 'a6', appears_on=True)

ALL = [OK_COMPUTER, IN_RAINBOWS, LIVE_REC, KID_A_MNESIA, CREEP_SINGLE, A_GUEST_SPOT]


class FakeSpotify:
    '''Counts its own calls, so laziness is provable rather than asserted.'''

    def __init__(self, albums=ALL):
        self._albums = {a.spotify_id: a for a in albums}
        self.groups_requested: tuple[str, ...] = ()
        self.tracks_fetched_for: list[str] = []

    def artist_album_ids(self, artist_id, *, groups=()):
        self.groups_requested = tuple(groups)
        for a in self._albums.values():
            yield a.spotify_id, ('appears_on' if a.is_appears_on else 'album')

    def albums(self, ids):
        for i in ids:
            # the full re-fetch does not carry album_group. expand must restore it
            yield replace(self._albums[i], is_appears_on=False)

    def tracks_of(self, alb):
        self.tracks_fetched_for.append(alb.spotify_id)
        for n in range(alb.total_tracks or 0):
            yield Track(title=f'{alb.title} #{n}', artists=(RADIOHEAD,), album=alb,
                        isrc=f'ISRC-{alb.spotify_id}-{n}', duration_ms=200_000)

    def album_tracks(self, album_id):
        yield from self.tracks_of(self._albums[album_id])

    def track(self, track_id):
        return Track(title='Creep', artists=(RADIOHEAD,), spotify_id=track_id,
                     duration_ms=238_000, isrc='GBAYE9200001')

    def playlist_tracks(self, playlist_id):
        for n in range(3):
            yield Track(title=f'p{n}', artists=(RADIOHEAD,), isrc=f'P{n}', duration_ms=1000)


class FakeMusicBrainz:
    '''Pass-through: our fixtures already carry the types MusicBrainz would supply.'''

    def __init__(self, *, broken=False, silent=False):
        self.broken = broken
        self.silent = silent
        self.calls = 0

    def enrich_album(self, alb):
        self.calls += 1
        if self.broken:
            raise ProviderFailed('musicbrainz is down', context={'host': 'musicbrainz.org'})
        if self.silent:
            return alb                              # never heard of it
        return replace(alb, release_group_id=f'rg-{alb.spotify_id}')


def run(resolution, sp, *, profile=CANONICAL, mb=None):
    bus = EventBus()
    seen: list = []
    bus.subscribe(seen.append)
    tracks = list(expand(resolution, sp, profile=profile, mb=mb, bus=bus, run_id='r1'))
    return tracks, seen


ARTIST = Resolution(EntityKind.ARTIST, 'rh', 'Radiohead')


# ----------------------------------------------------------------------------
# selection, in isolation
# ----------------------------------------------------------------------------

def test_a_studio_album_survives_canonical():
    assert exclusion_reason(OK_COMPUTER, CANONICAL) is None


@pytest.mark.parametrize('alb, reason', [
    (LIVE_REC, 'secondary:live'),
    (KID_A_MNESIA, 'secondary:compilation'),
    (A_GUEST_SPOT, 'appears_on'),
])
def test_canonical_names_its_reasons(alb, reason):
    assert exclusion_reason(alb, CANONICAL) == reason


def test_completionist_keeps_the_live_album_and_the_guest_spot():
    assert exclusion_reason(LIVE_REC, COMPLETIONIST) is None
    assert exclusion_reason(A_GUEST_SPOT, COMPLETIONIST) is None


def test_studio_only_throws_out_the_single():
    assert exclusion_reason(CREEP_SINGLE, STUDIO_ONLY) == 'type:single'
    assert exclusion_reason(CREEP_SINGLE, CANONICAL) is None


def test_completionist_does_not_pay_for_enrichment_it_will_not_use():
    '''MusicBrainz is one request per second. A minute wasted is a minute wasted.'''
    assert needs_enrichment(CANONICAL)
    assert needs_enrichment(STUDIO_ONLY)
    assert not needs_enrichment(COMPLETIONIST)


def test_we_only_ask_spotify_for_groups_the_profile_could_keep():
    assert spotify_album_groups(CANONICAL) == ('album', 'single')
    assert spotify_album_groups(STUDIO_ONLY) == ('album',)
    assert set(spotify_album_groups(COMPLETIONIST)) == {'album', 'single', 'compilation', 'appears_on'}


# ----------------------------------------------------------------------------
# artist expansion
# ----------------------------------------------------------------------------

def test_canonical_expansion_keeps_the_records_and_drops_the_rest():
    sp, mb = FakeSpotify(), FakeMusicBrainz()
    tracks, events = run(ARTIST, sp, profile=CANONICAL, mb=mb)

    kept = sorted({t.album.title for t in tracks})
    assert kept == ['Creep', 'In Rainbows', 'OK Computer']

    filtered = {(e.title, e.reason) for e in events if isinstance(e, ReleaseFiltered)}
    assert filtered == {
        ('Hail to the Thief (Live Recordings)', 'secondary:live'),
        ('KID A MNESIA', 'secondary:compilation'),
        ('Some Compilation', 'appears_on'),
    }


def test_every_rejection_is_announced_not_swallowed():
    '''"Where did the live album go" deserves an answer.'''
    _, events = run(ARTIST, FakeSpotify(), mb=FakeMusicBrainz())
    assert len([e for e in events if isinstance(e, ReleaseFiltered)]) == 3


def test_appears_on_survives_the_full_album_refetch():
    '''
    `album_group` is the only place Spotify says the artist is a guest, and it is
    absent from the full album object. Losing it means guest spots silently pass
    the filter.
    '''
    sp = FakeSpotify()
    tracks, _ = run(ARTIST, sp, profile=CANONICAL, mb=FakeMusicBrainz())
    assert 'a6' not in {t.album.spotify_id for t in tracks}


def test_completionist_keeps_everything_and_never_calls_musicbrainz():
    sp, mb = FakeSpotify(), FakeMusicBrainz()
    tracks, events = run(ARTIST, sp, profile=COMPLETIONIST, mb=mb)
    assert len({t.album.spotify_id for t in tracks}) == 6
    assert mb.calls == 0, 'enrichment it cannot use is a minute of MusicBrainz wasted'
    assert not [e for e in events if isinstance(e, ReleaseFiltered)]


def test_the_artist_is_announced_with_its_size_before_any_track_arrives():
    _, events = run(ARTIST, FakeSpotify(), mb=FakeMusicBrainz())
    first = next(e for e in events if isinstance(e, EntityDiscovered))
    assert first.entity_kind == 'artist' and first.child_count == 6


def test_expansion_reports_what_it_produced():
    tracks, events = run(ARTIST, FakeSpotify(), mb=FakeMusicBrainz())
    done = next(e for e in events if isinstance(e, StageFinished))
    assert done.count == len(tracks) == 6


# ----------------------------------------------------------------------------
# laziness
# ----------------------------------------------------------------------------

def test_nothing_is_fetched_until_it_is_pulled():
    '''An artist with 600 tracks must cost what an artist with 6 costs.'''
    sp = FakeSpotify()
    stream = expand(ARTIST, sp, profile=CANONICAL, mb=FakeMusicBrainz(),
                    bus=EventBus(), run_id='r1')
    assert sp.tracks_fetched_for == [], 'the generator ran before anyone asked'

    next(stream)
    assert sp.tracks_fetched_for == ['a1'], 'exactly one album was opened'


# ----------------------------------------------------------------------------
# enrichment is best-effort and says so
# ----------------------------------------------------------------------------

def test_a_broken_musicbrainz_warns_and_keeps_going():
    sp = FakeSpotify([OK_COMPUTER])
    tracks, events = run(ARTIST, sp, profile=CANONICAL, mb=FakeMusicBrainz(broken=True))
    assert len(tracks) == 2
    warning = next(e for e in events if isinstance(e, Warned))
    assert 'could not classify' in warning.message


def test_a_throttled_musicbrainz_degrades_enrichment_but_never_kills_the_run():
    '''
    Regression, learned in production: expansion runs OUTSIDE the Engine, so no
    supervisor reads retry policy off errors raised here. A real MusicBrainz 429
    escaped _classify and killed an entire artist run. An error that means
    "wait a moment" became "lose everything". Throttling must degrade
    classification, loudly, and continue.
    '''
    from spotdlplus.core.errors import RateLimited

    class ThrottledMB:
        def enrich_album(self, alb):
            raise RateLimited('musicbrainz.org: throttled',
                              context={'host': 'musicbrainz.org'}, retry_after=30.0)

    # a live record as SPOTIFY sees it: album_type 'album', no secondary types.
    # only enrichment would have caught it, and enrichment is throttled.
    live_but_blind = album('Live Recordings 2003-2009', 'a9')
    sp = FakeSpotify([OK_COMPUTER, live_but_blind])
    tracks, events = run(ARTIST, sp, profile=CANONICAL, mb=ThrottledMB())

    # both albums survive on Spotify's guess. the live one wrongly passes the
    # filter, which is exactly why the warning must be loud
    assert len(tracks) == 4
    warnings = [e for e in events if isinstance(e, Warned)]
    assert len(warnings) == 2
    assert all('throttled' in w.message for w in warnings)
    assert all('re-run later' in w.message for w in warnings)


def test_equal_size_remaster_albums_collapse_at_the_release_group(monkeypatch):
    '''
    The Los Campesinos! regression: the band renames tracks across remasters,
    so title-keyed track dedupe is blind. Romance Is Boring landed twice,
    wholesale. Two releases sharing a MusicBrainz release-group with equal
    track counts are the same record. Canonical keeps the original.
    '''
    from dataclasses import replace as _r

    original = _r(album('Romance Is Boring', 'r1', tracks=15), release_date='2010-01-26')
    remaster = _r(album('Romance Is Boring (Remastered)', 'r2', tracks=15),
                  release_date='2020-02-14')
    original = _r(original, release_group_id='rg-romance')
    remaster = _r(remaster, release_group_id='rg-romance')

    sp = FakeSpotify([original, remaster])
    tracks, events = run(ARTIST, sp, profile=CANONICAL, mb=FakeMusicBrainz(silent=True))

    albums_hit = {t.album.spotify_id for t in tracks}
    assert albums_hit == {'r1'}, 'the original is the only edition expanded'
    dropped = [e for e in events if isinstance(e, ReleaseFiltered)
               and 'release_group:superseded' in e.reason]
    assert len(dropped) == 1 and dropped[0].title == 'Romance Is Boring (Remastered)'


def test_a_deluxe_with_more_tracks_survives_the_release_group_collapse():
    '''OKNOTOK's B-sides live because of this: bigger editions are kept and the
    track-level pass folds only the overlap.'''
    from dataclasses import replace as _r

    original = _r(album('OK Computer', 'o1', tracks=12), release_date='1997-05-28',
                  release_group_id='rg-okc')
    deluxe = _r(album('OK Computer OKNOTOK', 'o2', tracks=23), release_date='2017-06-23',
                release_group_id='rg-okc')

    sp = FakeSpotify([original, deluxe])
    tracks, events = run(ARTIST, sp, profile=CANONICAL, mb=FakeMusicBrainz(silent=True))
    assert {t.album.spotify_id for t in tracks} == {'o1', 'o2'}


def test_completionist_never_collapses_release_groups():
    from dataclasses import replace as _r

    a = _r(album('X', 'x1'), release_group_id='rg-x', release_date='2010')
    b = _r(album('X (Remastered)', 'x2'), release_group_id='rg-x', release_date='2020')
    sp = FakeSpotify([a, b])
    tracks, _ = run(ARTIST, sp, profile=COMPLETIONIST, mb=None)
    assert {t.album.spotify_id for t in tracks} == {'x1', 'x2'}


def test_a_musicbrainz_blind_live_album_is_caught_by_its_title():
    '''
    "A Good Night for a Fistfight (Live at Islington Assembly Hall)", 18 live
    tracks MusicBrainz had never met, once walked into canonical through the
    front door. The title fallback fires ONLY when MB is blind, and announces
    that it is a guess.
    '''
    blind_live = album('A Good Night for a Fistfight (Live at Islington Assembly Hall)', 'L1')
    sp = FakeSpotify([blind_live, OK_COMPUTER])
    tracks, events = run(ARTIST, sp, profile=CANONICAL, mb=FakeMusicBrainz(silent=True))

    assert {t.album.spotify_id for t in tracks} == {'a1'}, 'the live album is out'
    filtered = [e for e in events if isinstance(e, ReleaseFiltered)]
    assert any('secondary:live' in e.reason for e in filtered)
    warned = [e for e in events if isinstance(e, Warned) and 'title guess' in e.message]
    assert len(warned) == 1, 'the guess is announced, not silent'


def test_a_studio_album_with_live_in_its_name_is_not_fooled():
    '''"Live Through This"-style names must not trip the fallback.'''
    tricky = album('The Live Wire Sessions Forever', 'T1')   # no "live at/in/from"
    sp = FakeSpotify([tricky])
    tracks, events = run(ARTIST, sp, profile=CANONICAL, mb=FakeMusicBrainz(silent=True))
    assert {t.album.spotify_id for t in tracks} == {'T1'}


def test_an_album_musicbrainz_never_heard_of_is_flagged_as_a_guess():
    '''A live album slipping silently into a canonical library is the worst outcome.'''
    sp = FakeSpotify([OK_COMPUTER])
    _, events = run(ARTIST, sp, profile=CANONICAL, mb=FakeMusicBrainz(silent=True))
    warning = next(e for e in events if isinstance(e, Warned))
    assert 'no record of' in warning.message
    assert 'not a fact' in warning.message


# ----------------------------------------------------------------------------
# the other three depths
# ----------------------------------------------------------------------------

def test_an_explicitly_requested_album_is_never_filtered():
    '''You asked for the live album by name. A profile is a default, not a veto.'''
    sp = FakeSpotify()
    res = Resolution(EntityKind.ALBUM, 'a3', 'Hail to the Thief (Live Recordings)')
    tracks, events = run(res, sp, profile=CANONICAL, mb=FakeMusicBrainz())
    assert len(tracks) == 2
    assert not [e for e in events if isinstance(e, ReleaseFiltered)]


def test_a_playlist_is_never_filtered_either():
    res = Resolution(EntityKind.PLAYLIST, 'p1', 'late night')
    tracks, _ = run(res, FakeSpotify(), profile=CANONICAL)
    assert len(tracks) == 3


def test_a_single_track_is_a_tree_with_one_leaf():
    res = Resolution(EntityKind.TRACK, 't1', 'Creep')
    tracks, events = run(res, FakeSpotify(), profile=CANONICAL)
    assert len(tracks) == 1 and tracks[0].identity == 'isrc:GBAYE9200001'
    assert next(e for e in events if isinstance(e, StageFinished)).count == 1
