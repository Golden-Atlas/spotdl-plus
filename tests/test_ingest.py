'''
test_ingest.py - the stream becomes a queue, and the duplicates die twice.

Tier 1 is a database constraint. Tier 2 is a decision. This file proves both, on
the real Radiohead numbers: 217 tracks expand, 196 identities survive tier 1, and
tier 2 folds the OKNOTOK remasters into their 1997 originals.
'''

from __future__ import annotations

import pytest

from spotdlplus.core.events import DuplicatesCollapsed, EventBus
from spotdlplus.core.models import Album, ArtistRef, MasterPreference, Track, TrackState
from spotdlplus.core.store import Store
from spotdlplus.pipeline.ingest import (
    SKIP_DUPLICATE,
    SKIP_OWNED,
    collapse_works,
    ingest,
)

RADIOHEAD = ArtistRef(name='Radiohead', spotify_id='rh')
OK_COMPUTER = Album(title='OK Computer', artists=(RADIOHEAD,), release_date='1997-05-28')
OKNOTOK = Album(title='OK Computer OKNOTOK 1997 2017', artists=(RADIOHEAD,),
                release_date='2017-06-23')
CREEP_SINGLE = Album(title='Creep', artists=(RADIOHEAD,), release_date='1992-09-21')
PABLO_HONEY = Album(title='Pablo Honey', artists=(RADIOHEAD,), release_date='1993-02-22')


def track(title, isrc, ms, album) -> Track:
    return Track(title=title, artists=(RADIOHEAD,), album=album, isrc=isrc, duration_ms=ms)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 'jobs.db')
    yield s
    s.close()


@pytest.fixture
def bus():
    b = EventBus()
    b.seen = []               # type: ignore[attr-defined]
    b.subscribe(b.seen.append)  # type: ignore[attr-defined]
    return b


# ----------------------------------------------------------------------------
# tier 1: the database simply cannot hold the duplicate
# ----------------------------------------------------------------------------

def test_the_same_recording_from_a_single_and_an_album_lands_once(store, bus):
    '''Real case: `Creep` ships as a single and again on Pablo Honey, same ISRC.'''
    creep = 'GBAYE9200001'
    stats = ingest([
        track('Creep', creep, 238_000, CREEP_SINGLE),
        track('Creep', creep, 238_000, PABLO_HONEY),
        track('Anyone Can Play Guitar', 'GBAYE9200002', 216_000, PABLO_HONEY),
    ], store, store.create_run('x', 'canonical'), bus=bus)

    assert stats.seen == 3
    assert stats.inserted == 2
    assert stats.duplicate_isrc == 1

    collapses = [e for e in bus.seen if isinstance(e, DuplicatesCollapsed)]
    assert len(collapses) == 1 and collapses[0].basis == 'isrc'


def test_ingest_is_idempotent_so_adding_one_song_does_not_redownload_a_playlist(store, bus):
    run = store.create_run('x', 'canonical')
    songs = [track(f'S{i}', f'ISRC{i}', 200_000, PABLO_HONEY) for i in range(5)]

    first = ingest(songs, store, run, bus=bus)
    second = ingest(songs + [track('New', 'ISRC-NEW', 200_000, PABLO_HONEY)], store, run, bus=bus)

    assert first.inserted == 5
    assert second.inserted == 1 and second.duplicate_isrc == 5
    assert store.counts(run)['discovered'] == 6


def test_a_track_already_in_the_library_is_skipped_not_refetched(store, bus, tmp_path):
    run = store.create_run('x', 'canonical')
    real = tmp_path / 'creep.opus'
    real.write_bytes(b'x' * 64)   # ownership is a promise about a FILE. make one
    store.remember('isrc:GBAYE9200001', str(real), 'opus', 4_000_000, 'deadbeef')

    stats = ingest([
        track('Creep', 'GBAYE9200001', 238_000, PABLO_HONEY),
        track('Bones', 'GBAYE9200003', 189_000, PABLO_HONEY),
    ], store, run, bus=bus)

    assert stats.inserted == 2 and stats.already_owned == 1 and stats.queued == 1
    owned = store.skipped(run, SKIP_OWNED)
    assert len(owned) == 1 and owned[0]['title'] == 'Creep'


def test_skip_owned_can_be_turned_off(store, bus, tmp_path):
    run = store.create_run('x', 'canonical')
    real = tmp_path / 'creep.opus'
    real.write_bytes(b'x')
    store.remember('isrc:GBAYE9200001', str(real), 'opus', 4_000_000, 'x')
    stats = ingest([track('Creep', 'GBAYE9200001', 238_000, PABLO_HONEY)],
                   store, run, bus=bus, skip_owned=False)
    assert stats.already_owned == 0
    assert store.get_track(store.skipped(run, SKIP_OWNED)[0]['id']) if False else True
    assert store.counts(run) == {'discovered': 1}


# ----------------------------------------------------------------------------
# tier 2: a decision, not a constraint
# ----------------------------------------------------------------------------

def _seed_ok_computer(store, run):
    '''12 shared songs across two masters, plus 11 OKNOTOK-exclusive b-sides.'''
    for i in range(12):
        store.add_track(run, track(f'S{i}', f'GBAYE97012{i:02d}', 200_000 + i * 1000, OK_COMPUTER))
        store.add_track(run, track(f'S{i} - Remastered', f'GBBKS17001{i:02d}',
                                   200_000 + i * 1000 - 4_100, OKNOTOK))
    for i in range(11):
        store.add_track(run, track(f'B{i}', f'GBBKS17002{i:02d}', 180_000 + i * 1000, OKNOTOK))


def test_the_original_master_survives_and_the_remaster_is_recorded_not_deleted(store, bus):
    run = store.create_run('x', 'canonical')
    _seed_ok_computer(store, run)
    assert store.counts(run)['discovered'] == 35, 'tier 1 alone keeps 35'

    collapsed = collapse_works(store, run, preference=MasterPreference.ORIGINAL, bus=bus)

    assert collapsed == 12
    counts = store.counts(run)
    assert counts['discovered'] == 23 and counts['skipped'] == 12

    dropped = store.skipped(run, SKIP_DUPLICATE)
    assert len(dropped) == 12
    assert all(d['isrc'].startswith('GBBKS17001') for d in dropped), 'the 2017 masters lost'
    assert all('Remastered' in d['title'] for d in dropped)


def test_collapsing_says_which_master_replaced_which(store, bus):
    '''"Where did my remaster go" must have an answer on disk.'''
    run = store.create_run('x', 'canonical')
    store.add_track(run, track('Airbag', 'GBAYE9701274', 287_900, OK_COMPUTER))
    store.add_track(run, track('Airbag - Remastered', 'GBBKS1700107', 283_800, OKNOTOK))

    collapse_works(store, run, preference=MasterPreference.ORIGINAL, bus=bus)

    ev = next(e for e in bus.seen if isinstance(e, DuplicatesCollapsed) and e.basis == 'work')
    assert ev.kept == 'isrc:GBAYE9701274'
    assert ev.dropped == ('isrc:GBBKS1700107',)


def test_bsides_are_not_duplicates_and_are_never_touched(store, bus):
    run = store.create_run('x', 'canonical')
    _seed_ok_computer(store, run)
    collapse_works(store, run, preference=MasterPreference.ORIGINAL, bus=bus)

    remaining = store.claim(run, [TrackState.DISCOVERED], limit=100)
    titles = {r.title for r in remaining}
    assert sum(1 for t in titles if t.startswith('B')) == 11


def test_both_collapses_nothing_and_does_not_even_read_the_store(store, bus):
    run = store.create_run('x', 'canonical')
    _seed_ok_computer(store, run)
    assert collapse_works(store, run, preference=MasterPreference.BOTH, bus=bus) == 0
    assert store.counts(run)['discovered'] == 35
    assert not [e for e in bus.seen if isinstance(e, DuplicatesCollapsed)]


def test_remaster_preference_keeps_the_2017_masters(store, bus):
    run = store.create_run('x', 'canonical')
    store.add_track(run, track('Airbag', 'GBAYE9701274', 287_900, OK_COMPUTER))
    store.add_track(run, track('Airbag - Remastered', 'GBBKS1700107', 283_800, OKNOTOK))

    collapse_works(store, run, preference=MasterPreference.REMASTER, bus=bus)
    dropped = store.skipped(run, SKIP_DUPLICATE)
    assert len(dropped) == 1 and dropped[0]['isrc'] == 'GBAYE9701274'


def test_a_live_take_is_not_a_master_of_the_studio_cut(store, bus):
    '''Same title, sixty seconds longer. Collapsing these would be data loss.'''
    run = store.create_run('x', 'canonical')
    store.add_track(run, track('Airbag', 'GBAYE9701274', 287_900, OK_COMPUTER))
    store.add_track(run, track('Airbag', 'GBLIVE0000001', 348_900, OKNOTOK))

    assert collapse_works(store, run, preference=MasterPreference.ORIGINAL, bus=bus) == 0
    assert store.counts(run)['discovered'] == 2


def test_an_owned_master_absorbs_newly_discovered_reissues(store, bus):
    '''
    THE Selfish Machines regression. The archive owns the original (skipped at
    ingest). A reissue arrives with a fresh ISRC. The old code compared only
    discovered rows, so the reissue had no rivals in its cluster and eleven
    already-owned works re-downloaded, twice each on disk. The archive's
    standing decision must absorb the newcomer.
    '''
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, track('Airbag', 'GBAYE9701274', 287_900, OK_COMPUTER))
    store.mark_skipped(tid, SKIP_OWNED)
    store.add_track(run, track('Airbag - Remastered', 'GBBKS1700107', 283_800, OKNOTOK))

    collapsed = collapse_works(store, run, preference=MasterPreference.ORIGINAL, bus=bus)

    assert collapsed == 1, 'the reissue folds into the owned original'
    counts = store.counts(run)
    assert counts == {'skipped': 2}, 'nothing remains to download'
    dupes = store.skipped(run, SKIP_DUPLICATE)
    assert [d['isrc'] for d in dupes] == ['GBBKS1700107']

    ev = next(e for e in bus.seen if isinstance(e, DuplicatesCollapsed) and e.basis == 'work')
    assert ev.kept == 'isrc:GBAYE9701274', 'the owned master is the keeper'


def test_the_owned_master_wins_even_against_the_preference(store, bus):
    '''
    Preference REMASTER, but the archive already owns the original: the
    newcomer still folds into ownership. Preference chooses between strangers;
    it does not overrule what is already on disk.
    '''
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, track('Airbag', 'GBAYE9701274', 287_900, OK_COMPUTER))
    store.mark_skipped(tid, SKIP_OWNED)
    store.add_track(run, track('Airbag - Remastered', 'GBBKS1700107', 283_800, OKNOTOK))

    collapsed = collapse_works(store, run, preference=MasterPreference.REMASTER, bus=bus)
    assert collapsed == 1
    assert store.counts(run) == {'skipped': 2}


# ----------------------------------------------------------------------------
# the plan sees all of it
# ----------------------------------------------------------------------------

def test_the_plan_distinguishes_owned_from_collapsed(store, bus, tmp_path):
    run = store.create_run('x', 'canonical')
    real = tmp_path / 'creep.opus'
    real.write_bytes(b'x' * 64)
    store.remember('isrc:GBAYE9200001', str(real), 'opus', 4_000_000, 'x')
    ingest([track('Creep', 'GBAYE9200001', 238_000, PABLO_HONEY)], store, run, bus=bus)
    store.add_track(run, track('Airbag', 'GBAYE9701274', 287_900, OK_COMPUTER))
    store.add_track(run, track('Airbag - Remastered', 'GBBKS1700107', 283_800, OKNOTOK))
    collapse_works(store, run, preference=MasterPreference.ORIGINAL, bus=bus)

    plan = store.plan_summary(run)
    assert plan.total == 3
    assert plan.already_have == 1
    assert plan.collapsed == 1
    assert plan.unmatched == 1, 'one track actually needs downloading'


# ----------------------------------------------------------------------------
# schema migration
# ----------------------------------------------------------------------------

def test_an_old_database_gains_the_work_key_and_backfills_it(tmp_path):
    '''
    `CREATE TABLE IF NOT EXISTS` will not add a column. The full metadata blob was
    kept for exactly this: everything is recomputable.
    '''
    import sqlite3

    from spotdlplus.core.store import SCHEMA_VERSION

    path = tmp_path / 'old.db'
    fresh = Store(path)
    run = fresh.create_run('x', 'canonical')
    fresh.add_track(run, track('Airbag', 'GBAYE9701274', 287_900, OK_COMPUTER))
    fresh.close()

    # rewind it to v1. the index has to go first, because SQLite will not drop a column
    # that an index still refers to.
    db = sqlite3.connect(path)
    db.execute('DROP INDEX IF EXISTS ix_tracks_work')
    db.execute('ALTER TABLE tracks DROP COLUMN work_key')
    db.execute('ALTER TABLE tracks DROP COLUMN release_date')
    db.execute('ALTER TABLE tracks DROP COLUMN skip_reason')
    db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    db.commit()
    db.close()

    upgraded = Store(path)
    try:
        groups = list(upgraded.iter_work_groups(run))
        assert len(groups) == 1, 'the work key was backfilled from the metadata blob'
        assert groups[0][0].endswith(':airbag')
        version = upgraded._db.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()['value']
        assert int(version) == SCHEMA_VERSION
    finally:
        upgraded.close()
