'''
test_healing.py - a manually deleted file must never haunt the library.

The scenario, verbatim from the user: "if I delete a file manually outside of
spotdlp, and I redownload that related song at some point in the workflow, it
is able to recognize that it doesn't exist and will add it without skipping."

Ownership is a promise about a file. When the file is gone, the promise dies,
by every path: `get` heals at ingest, `audit --fix` heals in bulk.
'''

from __future__ import annotations

from pathlib import Path

import pytest

from spotdlplus.core.events import EventBus, Warned
from spotdlplus.core.models import Album, ArtistRef, Track, TrackState
from spotdlplus.core.store import Store
from spotdlplus.pipeline.ingest import SKIP_OWNED, ingest

ARTIST = ArtistRef(name='Pretty Sick')


def track(title='home2hide', isrc='GBK3W2503575'):
    return Track(title=title, artists=(ARTIST,),
                 album=Album(title='home2hide', artists=(ARTIST,)),
                 isrc=isrc, duration_ms=240_000)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 'jobs.db')
    yield s
    s.close()


def _bus():
    b = EventBus()
    b.seen = []                  # type: ignore[attr-defined]
    b.subscribe(b.seen.append)   # type: ignore[attr-defined]
    return b


def _own_with_file(store, tmp_path, t: Track) -> Path:
    f = tmp_path / 'lib' / 'Pretty Sick' / '01 home2hide.opus'
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b'audio' * 100)
    store.remember(t.identity, str(f), 'opus', f.stat().st_size, 'sha')
    return f


# ----------------------------------------------------------------------------
# the get path: ingest verifies the file, not just the row
# ----------------------------------------------------------------------------

def test_an_intact_owned_file_still_skips(store, tmp_path):
    t = track()
    _own_with_file(store, tmp_path, t)
    run = store.create_run('x', 'canonical')

    stats = ingest([t], store, run, bus=_bus())
    assert stats.already_owned == 1 and stats.healed == 0
    assert store.own(t.identity) is not None


def test_a_deleted_file_heals_instead_of_haunting(store, tmp_path):
    '''The user's exact scenario, end to end at the ingest layer.'''
    t = track()
    f = _own_with_file(store, tmp_path, t)
    f.unlink()   # the human curates outside the tool

    run = store.create_run('x', 'canonical')
    bus = _bus()
    stats = ingest([t], store, run, bus=bus)

    assert stats.healed == 1
    assert stats.already_owned == 0, 'a ghost must not count as owned'
    assert store.own(t.identity) is None, 'the stale promise is revoked'

    # the track sits in the queue as fresh work, not skipped
    counts = store.counts(run)
    assert counts == {'discovered': 1}
    assert store.skipped(run, SKIP_OWNED) == []

    warning = next(e for e in bus.seen if isinstance(e, Warned))
    assert 'file is gone' in warning.message


def test_healing_is_per_recording_not_per_run(store, tmp_path):
    '''One deleted file heals. Its intact neighbors still skip.'''
    kept, gone = track('a', 'ISRC-A'), track('b', 'ISRC-B')
    _own_with_file(store, tmp_path, kept)
    f = tmp_path / 'lib' / 'b.opus'
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b'x')
    store.remember(gone.identity, str(f), 'opus', 1, 'sha')
    f.unlink()

    run = store.create_run('x', 'canonical')
    stats = ingest([kept, gone], store, run, bus=_bus())
    assert stats.already_owned == 1 and stats.healed == 1


# ----------------------------------------------------------------------------
# the audit path: --fix heals in bulk
# ----------------------------------------------------------------------------

def test_audit_fix_revokes_and_requeues_a_missing_file(store, tmp_path):
    from spotdlplus.pipeline.audit import audit_library

    t = track()
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, t)
    # walk it to done with a chosen source, then delete the file
    store.advance(tid, TrackState.ENRICHED)
    store.advance(tid, TrackState.MATCHED, chosen_url='yt/good')
    for st in (TrackState.FETCHED, TrackState.TRANSCODED, TrackState.TAGGED):
        store.advance(tid, st)
    f = _own_with_file(store, tmp_path, t)
    store.advance(tid, TrackState.PLACED, final_path=str(f))
    store.advance(tid, TrackState.DONE)
    f.unlink()

    report = audit_library(store, output_dir=tmp_path / 'lib',
                           cache_dir=tmp_path / 'cache', fix=True)

    assert report.fixed == 1
    assert store.own(t.identity) is None
    row = store.get_track(tid)
    assert row.state is TrackState.MATCHED, 'requeued with its known-good source'
    assert row.chosen_url == 'yt/good'


def test_audit_without_fix_only_reports_the_missing_file(store, tmp_path):
    from spotdlplus.pipeline.audit import audit_library

    t = track()
    f = _own_with_file(store, tmp_path, t)
    run = store.create_run('x', 'canonical')
    store.add_track(run, t)
    f.unlink()

    report = audit_library(store, output_dir=tmp_path / 'lib',
                           cache_dir=tmp_path / 'cache')
    assert report.by_kind == {'missing_file': 1}
    assert store.own(t.identity) is not None, 'read-only audit revokes nothing'
