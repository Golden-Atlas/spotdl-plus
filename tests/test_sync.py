'''
test_sync.py - the living archive's memory, held to account.

Snapshots remember what a source contained. Sync diffs against them. Prune
deletes what left, hard and loudly, and never a file some other source still
wants. The deletes are real deletes in a tmp_path, because mocking the filesystem
here would mock the exact thing that must be right.
'''

from __future__ import annotations

from pathlib import Path

import pytest

from spotdlplus.core.config import Config
from spotdlplus.core.store import Store

import spotdlplus.cli.main as cli


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 'jobs.db')
    yield s
    s.close()


# ----------------------------------------------------------------------------
# snapshots
# ----------------------------------------------------------------------------

def test_snapshot_round_trip_and_replacement(store):
    store.save_snapshot('playlist:p1', label='road trip', kind='playlist',
                        identities=['isrc:A', 'isrc:B'])
    taken, ids = store.get_snapshot('playlist:p1')
    assert ids == {'isrc:A', 'isrc:B'}

    store.save_snapshot('playlist:p1', label='road trip', kind='playlist',
                        identities=['isrc:B', 'isrc:C'])
    _, ids2 = store.get_snapshot('playlist:p1')
    assert ids2 == {'isrc:B', 'isrc:C'}, 'latest walk wins, whole'


def test_snapshot_missing_source_is_none(store):
    assert store.get_snapshot('playlist:never') is None


def test_identity_in_other_snapshots_guards_cross_source(store):
    store.save_snapshot('playlist:p1', label='x', kind='playlist',
                        identities=['isrc:A', 'isrc:B'])
    store.save_snapshot('album:a1', label='y', kind='album',
                        identities=['isrc:B'])
    # B is wanted by the album. A is only the playlist's
    assert store.identity_in_other_snapshots('isrc:B', except_key='playlist:p1')
    assert not store.identity_in_other_snapshots('isrc:A', except_key='playlist:p1')


# ----------------------------------------------------------------------------
# prune: real files, real deletes, real guards
# ----------------------------------------------------------------------------

def _owned_file(store, tmp_path, identity, rel) -> Path:
    p = tmp_path / 'lib' / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'x' * 64)
    store.remember(identity, str(p), 'opus', 64, 'sha')
    return p


def test_prune_deletes_gone_files_and_folds_empty_dirs(store, tmp_path):
    a = _owned_file(store, tmp_path, 'isrc:A', 'Artist/Album (2020)/01 Gone.opus')
    b = _owned_file(store, tmp_path, 'isrc:B', 'Artist/Other (2021)/01 Stays.opus')
    cfg = Config(output_dir=tmp_path / 'lib')

    deleted, spared = cli._prune_gone(store, cfg, gone={'isrc:A'},
                                      except_key='playlist:p1')

    assert (deleted, spared) == (1, 0)
    assert not a.exists()
    assert not a.parent.exists(), 'the emptied album folder folds up'
    assert b.exists() and store.own('isrc:B') is not None
    assert store.own('isrc:A') is None, 'ownership revoked with the file'
    assert (tmp_path / 'lib').is_dir(), 'the library root itself never goes'


def test_prune_spares_tracks_other_sources_still_want(store, tmp_path):
    a = _owned_file(store, tmp_path, 'isrc:A', 'Artist/Album (2020)/01 Shared.opus')
    store.save_snapshot('album:a1', label='y', kind='album', identities=['isrc:A'])

    deleted, spared = cli._prune_gone(store, cfg := Config(output_dir=tmp_path / 'lib'),
                                      gone={'isrc:A'}, except_key='playlist:p1')

    assert (deleted, spared) == (0, 1)
    assert a.exists() and store.own('isrc:A') is not None, \
        'the album still wants it. the playlist leaving changes nothing'


def test_prune_handles_already_missing_files(store, tmp_path):
    p = _owned_file(store, tmp_path, 'isrc:A', 'Artist/Album (2020)/01 Ghost.opus')
    p.unlink()   # gone from disk already
    deleted, spared = cli._prune_gone(store, Config(output_dir=tmp_path / 'lib'),
                                      gone={'isrc:A'}, except_key='k')
    assert (deleted, spared) == (1, 0)
    assert store.own('isrc:A') is None, 'the stale claim goes even when the file is gone'


# ----------------------------------------------------------------------------
# run_source feeds the snapshot machinery
# ----------------------------------------------------------------------------

def test_run_report_carries_identities_and_saves_a_snapshot(tmp_path):
    from spotdlplus.core.events import NullBus
    from spotdlplus.core.models import Album, ArtistRef, Track
    from spotdlplus.pipeline import run as run_mod

    A = ArtistRef(name='Duster')
    tracks = [Track(title=f'T{i}', artists=(A,),
                    album=Album(title='Stratosphere', artists=(A,)),
                    isrc=f'ISRC{i}', duration_ms=100_000) for i in range(3)]

    class FakeSp:
        def track(self, _id):
            return tracks[0]

    store = Store(tmp_path / 'jobs.db')
    try:
        cfg = Config(output_dir=tmp_path / 'lib', cache_dir=tmp_path / 'cache')

        # monkeypatch-free: call with a gate that declines (no network needed
        # past expansion) and a searcher that's never reached
        report = run_mod.run_source(
            'spotify:track:t1', config=cfg, store=store, bus=NullBus(),
            sp=FakeSp(), mb=None, searcher=None, http=None,
            gate=lambda plan: False,
        )
        assert report.identities == {tracks[0].identity}
        # declined at the gate -> the snapshot must NOT have been saved
        assert store.get_snapshot('track:t1') is None
    finally:
        store.close()
