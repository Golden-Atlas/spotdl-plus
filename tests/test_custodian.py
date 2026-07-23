'''
test_custodian.py - the careful commands, proven on real files.

cleanup's quick pass, move-library's re-anchoring, and undo's take-backs all
manipulate the actual filesystem, so these tests do too, in tmp_path, where
a bug costs nothing. The library root involved is always a throwaway.
'''

from __future__ import annotations

from pathlib import Path

import pytest

import spotdlplus.cli.main as cli
from spotdlplus.core.models import Album, ArtistRef, Track, TrackState
from spotdlplus.core.store import Store

A = ArtistRef(name='Radiohead')


def _track(title='Creep', isrc='GBAYE9200001'):
    return Track(title=title, artists=(A,),
                 album=Album(title='Pablo Honey', artists=(A,)),
                 isrc=isrc, duration_ms=238_000)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 'jobs.db')
    yield s
    s.close()


def _place(store, tmp_path, run, track, rel):
    '''Walk a track to DONE with a real file at library/<rel>.'''
    p = tmp_path / 'lib' / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'x' * 64)
    tid, _ = store.add_track(run, track)
    for st in (TrackState.ENRICHED, TrackState.MATCHED, TrackState.FETCHED,
               TrackState.TRANSCODED, TrackState.TAGGED, TrackState.PLACED,
               TrackState.DONE):
        kw = {'chosen_url': 'yt/x'} if st is TrackState.MATCHED else {}
        kw = {'final_path': str(p)} if st is TrackState.PLACED else kw
        store.advance(tid, st, **kw)
    store.remember(track.identity, str(p), 'opus', 64, 'sha')
    return p


# ----------------------------------------------------------------------------
# fold_empty_dirs: the shared janitor
# ----------------------------------------------------------------------------

def test_fold_empty_dirs_removes_nests_but_never_the_root(tmp_path):
    (tmp_path / 'a' / 'b' / 'c').mkdir(parents=True)
    (tmp_path / 'keep').mkdir()
    (tmp_path / 'keep' / 'song.opus').write_bytes(b'x')
    folded = cli._fold_empty_dirs(tmp_path)
    assert folded == 3
    assert tmp_path.is_dir() and (tmp_path / 'keep' / 'song.opus').is_file()
    assert not (tmp_path / 'a').exists()


# ----------------------------------------------------------------------------
# active_track_ids: what cleanup must not touch
# ----------------------------------------------------------------------------

def test_active_ids_keep_in_flight_and_failed_tracks(store):
    run = store.create_run('x', 'canonical')
    inflight, _ = store.add_track(run, _track('A', 'ISRC0000001'))
    store.advance(inflight, TrackState.ENRICHED)

    from spotdlplus.core import errors as E
    failed, _ = store.add_track(run, _track('B', 'ISRC0000002'))
    store.advance(failed, TrackState.ENRICHED)
    store.fail(failed, E.DownloadFailed('x').record())

    done, _ = store.add_track(run, _track('C', 'ISRC0000003'))
    store.advance(done, TrackState.SKIPPED)

    ids = store.active_track_ids()
    assert inflight in ids and failed in ids
    assert done not in ids, 'a settled track owes the cache nothing'


# ----------------------------------------------------------------------------
# move-library: the bookkeeping half
# ----------------------------------------------------------------------------

def test_rewrite_library_root_reanchors_both_tables(store, tmp_path):
    run = store.create_run('x', 'canonical')
    old = _place(store, tmp_path, run, _track(), 'Radiohead/Pablo Honey (1993)/02 Creep.opus')

    old_root = str(tmp_path / 'lib')
    new_root = str(tmp_path / 'newlib')
    n = store.rewrite_library_root(old_root, new_root)

    assert n == 1
    row = store.own('isrc:GBAYE9200001')
    assert row['final_path'].startswith(new_root)
    assert row['final_path'].endswith('02 Creep.opus')
    assert store.get_track(store.newest_track_id('isrc:GBAYE9200001')) \
        .final_path.startswith(new_root), 'tracks moved with the library'
    assert old.exists(), 'rewrite is bookkeeping only. the CLI moves the bytes'


# ----------------------------------------------------------------------------
# undo: the last run unwinds whole. earlier work survives
# ----------------------------------------------------------------------------

def test_undo_scope_is_only_what_the_run_placed(store, tmp_path):
    old_run = store.create_run('old', 'canonical')
    kept = _place(store, tmp_path, old_run, _track('Old Song', 'ISRC0000001'),
                  'Radiohead/Old (1990)/01 Old Song.opus')
    store.set_run_status(old_run, 'finished')

    new_run = store.create_run('new', 'canonical')
    gone = _place(store, tmp_path, new_run, _track('New Song', 'ISRC0000002'),
                  'Radiohead/New (2024)/01 New Song.opus')

    placed = store.run_placements(new_run)
    assert [r['identity'] for r in placed] == ['isrc:ISRC0000002'], \
        'the old run\'s file is not on the chopping block'

    # the CLI's actual unwind, minus the prompt
    for r in placed:
        Path(r['final_path']).unlink()
        store.revoke_ownership(r['identity'])
    store.delete_run(new_run)

    assert kept.exists() and store.own('isrc:ISRC0000001') is not None
    assert not gone.exists() and store.own('isrc:ISRC0000002') is None
    assert store.latest_run_info()['id'] == old_run, 'the run itself is erased'


def test_undo_ignores_identities_owned_before_the_run(store, tmp_path):
    run1 = store.create_run('first', 'canonical')
    _place(store, tmp_path, run1, _track(), 'R/A/01.opus')
    store.set_run_status(run1, 'finished')

    # run2 sees the same identity but places nothing new (owned-skip)
    run2 = store.create_run('second', 'canonical')
    tid, _ = store.add_track(run2, _track())
    store.mark_skipped(tid, 'owned')

    assert store.run_placements(run2) == [], \
        'a re-get that skipped everything must have nothing to undo'
