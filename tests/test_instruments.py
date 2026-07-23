'''
test_instruments.py - the 1.1.3 read-only surfaces, proven against a real store.

stats, search, completeness, and the rate parser. None of these touch the
network, so all of them are provable with a seeded store and nothing else.
'''

from __future__ import annotations

import pytest

from spotdlplus.core.config import parse_rate
from spotdlplus.core.models import Album, ArtistRef, Track
from spotdlplus.core.store import Store

A = ArtistRef(name='Radiohead')
B = ArtistRef(name='Duster')


def _track(title, isrc, artist=A, album='Pablo Honey', dur=200_000):
    return Track(title=title, artists=(artist,),
                 album=Album(title=album, artists=(artist,)),
                 isrc=isrc, duration_ms=dur)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 'jobs.db')
    yield s
    s.close()


def _own(store, run, track, path='/m/x.opus', size=4_000_000):
    tid, _ = store.add_track(run, track)
    store.remember(track.identity, path, 'opus', size, 'sha')
    return tid


# ----------------------------------------------------------------------------
# parse_rate: the limit_rate knob's little parser
# ----------------------------------------------------------------------------

@pytest.mark.parametrize('text,expect', [
    ('2M', 2 * 1024 ** 2),
    ('500K', 500 * 1024),
    ('500k', 500 * 1024),
    ('30000', 30000),
    ('1.5M', int(1.5 * 1024 ** 2)),
    ('2M/s', 2 * 1024 ** 2),
])
def test_parse_rate_accepts_the_sane_forms(text, expect):
    assert parse_rate(text) == expect


@pytest.mark.parametrize('text', ['fast', '', '-2M', '0', 'M'])
def test_parse_rate_rejects_noise(text):
    assert parse_rate(text) is None


# ----------------------------------------------------------------------------
# stats
# ----------------------------------------------------------------------------

def test_stats_counts_the_library_not_the_runs(store):
    run = store.create_run('x', 'canonical')
    _own(store, run, _track('Creep', 'ISRC0000001'), '/m/creep.opus')
    _own(store, run, _track('Inside Out', 'ISRC0000002', B, 'Stratosphere'),
         '/m/inside.opus', size=6_000_000)
    # a track that was seen but never downloaded must not count
    store.add_track(run, _track('No Surprises', 'ISRC0000003'))

    s = store.library_stats()
    assert s['tracks'] == 2
    assert s['bytes'] == 10_000_000
    assert s['artists'] == 2
    assert s['duration_ms'] == 400_000
    assert s['formats'] == [('opus', 2, 10_000_000)]


def test_stats_groups_collabs_under_the_album_owner(store):
    # 'JPEGMAFIA, Danny Brown' is not a third artist, it's a JPEGMAFIA record
    # with a guest. Grouping by feature-list once split one library into 543
    # 'artists', which read as absurd because it was.
    run = store.create_run('x', 'canonical')
    solo = Track(title='1539 N. Calvert', artists=(ArtistRef(name='JPEGMAFIA'),),
                 album=Album(title='Veteran', artists=(ArtistRef(name='JPEGMAFIA'),)),
                 isrc='ISRC0000010', duration_ms=200_000)
    collab = Track(title='Burfict!',
                   artists=(ArtistRef(name='JPEGMAFIA'), ArtistRef(name='Danny Brown')),
                   album=Album(title='Scaring the Hoes',
                               artists=(ArtistRef(name='JPEGMAFIA'),)),
                   isrc='ISRC0000011', duration_ms=200_000)
    _own(store, run, solo, '/m/a.opus')
    _own(store, run, collab, '/m/b.opus')

    s = store.library_stats()
    assert s['artists'] == 1, 'one owner, two records, zero phantom artists'
    assert s['per_artist'][0][0] == 'JPEGMAFIA'
    assert s['per_artist'][0][1] == 2


def test_stats_still_sees_a_file_whose_run_was_tidied_away(store):
    '''
    Ownership outlives the run that created it, on purpose, so `tidy` leaves
    library rows with no track row behind them. stats used to inner-join the
    two and silently drop every one of those, which on my real archive meant
    3841 tracks but only 135 artists and 232.1 hours, with 212 owned files
    missing. Count and size read fine the whole time, which is what hid it.
    '''
    run = store.create_run('x', 'canonical')
    _own(store, run, _track('Creep', 'ISRC0000001'),
         '/m/Radiohead/Pablo Honey (1993)/01 Creep.opus')
    _own(store, run, _track('Inside Out', 'ISRC0000002', B, 'Stratosphere'),
         '/m/Duster/Stratosphere (1998)/01 Inside Out.opus', size=6_000_000)

    # what tidy does: the run's bookkeeping goes, the ownership stays
    with store._lock:                                    # noqa: SLF001
        store._db.execute('DELETE FROM tracks')          # noqa: SLF001
        store._db.commit()                               # noqa: SLF001

    s = store.library_stats()
    assert s['tracks'] == 2, 'ownership is untouched'
    assert s['artists'] == 2, 'both files still have an owner, from the folder'
    names = {a for a, *_ in s['per_artist']}
    assert names == {'Radiohead', 'Duster'}
    assert s['bytes'] == 10_000_000


def test_stats_details_adds_albums_and_recency(store):
    run = store.create_run('x', 'canonical')
    _own(store, run, _track('Creep', 'ISRC0000001'))
    s = store.library_stats(details=True)
    assert len(s['albums']) == 1 and s['albums'][0]['n'] == 1
    assert s['last_7_days'][0] == 1
    assert len(s['recent']) == 1


# ----------------------------------------------------------------------------
# search + completeness
# ----------------------------------------------------------------------------

def test_search_owned_matches_title_artist_and_album(store):
    run = store.create_run('x', 'canonical')
    _own(store, run, _track('Creep', 'ISRC0000001'))
    assert len(store.search_owned('creep')) == 1
    assert len(store.search_owned('radiohead')) == 1
    assert len(store.search_owned('pablo')) == 1
    assert store.search_owned('zzz') == []


def test_completeness_tells_full_from_partial(store):
    run = store.create_run('x', 'canonical')
    # album with 3 known tracks, 2 owned -> partial
    _own(store, run, _track('Creep', 'ISRC0000001'))
    _own(store, run, _track('Anyone', 'ISRC0000002'))
    store.add_track(run, _track('Lurgee', 'ISRC0000003'))
    # album fully owned -> complete
    _own(store, run, _track('Inside Out', 'ISRC0000010', B, 'Stratosphere'))

    rows = {r['album_title']: (r['owned'], r['known'])
            for r in store.album_completeness('a')}   # 'a' matches both artists
    assert rows['Pablo Honey'] == (2, 3)
    assert rows['Stratosphere'] == (1, 1)


def test_completeness_resolves_a_title_to_its_album(store):
    run = store.create_run('x', 'canonical')
    _own(store, run, _track('Creep', 'ISRC0000001'))
    store.add_track(run, _track('Lurgee', 'ISRC0000002'))
    rows = store.album_completeness('creep')   # a TITLE, not an album
    assert len(rows) == 1
    assert rows[0]['album_title'] == 'Pablo Honey'
    assert (rows[0]['owned'], rows[0]['known']) == (1, 2)


def test_owned_skip_state_does_not_break_search(store):
    # a skipped-as-owned row must not duplicate results (join is per-identity)
    run1 = store.create_run('x', 'canonical')
    _own(store, run1, _track('Creep', 'ISRC0000001'))
    run2 = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run2, _track('Creep', 'ISRC0000001'))
    store.mark_skipped(tid, 'owned')
    assert len(store.search_owned('creep')) == 1
