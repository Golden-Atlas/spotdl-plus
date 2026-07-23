'''
test_audit_fixes.py - the 1.1.2 audit findings, pinned so they stay fixed.

Three real defects from a full functional audit: a transient tool hang was
terminal, a resumed stale queue re-downloaded the whole library, and the run
database grew without bound. Each gets the regression test it should have had.
'''

from __future__ import annotations

from dataclasses import replace

import pytest

from spotdlplus.core import errors as E
from spotdlplus.core.config import Config
from spotdlplus.core.engine import Context, Engine
from spotdlplus.core.events import EventBus
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


# ----------------------------------------------------------------------------
# a hung tool is transient, not terminal
# ----------------------------------------------------------------------------

def test_a_tool_timeout_is_retryable_not_terminal(monkeypatch):
    import importlib
    import subprocess

    # `from spotdlplus.media import transcode` returns the FUNCTION the package
    # re-exports, not the module. importlib sidesteps the shadowing.
    transcode_mod = importlib.import_module('spotdlplus.media.transcode')

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd='ffprobe', timeout=30)

    monkeypatch.setattr(transcode_mod.subprocess, 'run', hang)
    with pytest.raises(E.ToolHung) as err:
        transcode_mod._run(['ffprobe', '-version'], timeout_s=30, what='probing')
    assert err.value.retry is E.Retry.BACKOFF, \
        'a busy machine is a moment and not a verdict, so the Engine retries'


# ----------------------------------------------------------------------------
# fetch rechecks ownership: a stale queue cannot re-download the library
# ----------------------------------------------------------------------------

def _ctx(store, tmp_path, run_id):
    cfg = replace(Config(), output_dir=tmp_path / 'lib', cache_dir=tmp_path / 'cache',
                  backoff_base_s=0.001, backoff_cap_s=0.01)
    return Context(store=store, bus=EventBus(), config=cfg, run_id=run_id)


def _matched_row(store, run_id, track):
    tid, _ = store.add_track(run_id, track)
    store.advance(tid, TrackState.ENRICHED)
    store.advance(tid, TrackState.MATCHED, chosen_url='yt/x', match_score=0.9)
    return store.get_track(tid)


def test_fetch_skips_a_track_the_library_already_owns(store, tmp_path):
    from spotdlplus.pipeline.stages import FetchStage
    from spotdlplus.core.engine import SkipTrack

    run = store.create_run('x', 'canonical')
    row = _matched_row(store, run, _track())

    owned_file = tmp_path / 'lib' / 'creep.opus'
    owned_file.parent.mkdir(parents=True)
    owned_file.write_bytes(b'x' * 64)
    store.remember(row.identity, str(owned_file), 'opus', 64, 'sha')

    ctx = _ctx(store, tmp_path, run)
    with pytest.raises(SkipTrack) as skip:
        FetchStage().run(ctx, row)
    assert skip.value.reason == 'owned', 'no download for a song already on disk'


def test_fetch_heals_ownership_whose_file_is_gone(store, tmp_path):
    # owned on paper, gone on disk -> revoke and proceed to a real download
    # (the fake url then fails inside yt-dlp, which is fine: the point is that
    # it TRIED instead of skipping on a lie)
    from spotdlplus.pipeline.stages import FetchStage

    run = store.create_run('x', 'canonical')
    row = _matched_row(store, run, _track())
    store.remember(row.identity, str(tmp_path / 'lib' / 'gone.opus'), 'opus', 64, 'sha')

    ctx = _ctx(store, tmp_path, run)
    with pytest.raises(E.SpotdlPlusError):
        FetchStage().run(ctx, row)                    # fake url -> download error
    assert store.own(row.identity) is None, 'the stale claim is revoked'


def test_engine_records_the_skip_reason(store, tmp_path):
    # the ownership skip must land as skip_reason='owned', the same currency
    # plan summaries already count as already_have
    from spotdlplus.core.engine import SkipTrack

    class OwnedSkip:
        name = __import__('spotdlplus.core.events', fromlist=['Stage']).Stage.FETCH
        consumes = TrackState.MATCHED
        produces = TrackState.FETCHED

        def run(self, ctx, row):
            raise SkipTrack('owned')

    run = store.create_run('x', 'canonical')
    _matched_row(store, run, _track())
    ctx = _ctx(store, tmp_path, run)
    Engine(ctx).drive(OwnedSkip())
    assert store.plan_summary(run).already_have == 1


# ----------------------------------------------------------------------------
# tidy: bounded bookkeeping, protected relink queue
# ----------------------------------------------------------------------------

def _finished_run(store, *, failed: bool, isrc: str) -> str:
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, _track(isrc=isrc))
    if failed:
        store.advance(tid, TrackState.ENRICHED)
        rec = E.NoAcceptableMatch('nothing cleared the floor').record()
        store.fail(tid, rec)
    store.set_run_status(run, 'finished')
    return run


def test_tidy_prunes_clean_runs_but_never_the_relink_queue(store):
    clean = [_finished_run(store, failed=False, isrc=f'CLEAN{i:07d}') for i in range(4)]
    with_queue = _finished_run(store, failed=True, isrc='QUEUED000001')

    result = store.tidy(keep_recent=0)

    assert result['runs'] == len(clean)
    assert store.counts(with_queue).get('failed') == 1, 'pending verdicts survive'
    for run in clean:
        assert store.counts(run) == {}, 'pruned runs leave no track rows'


def test_tidy_keeps_the_newest_runs_regardless(store):
    for i in range(6):
        _finished_run(store, failed=False, isrc=f'KEEP{i:08d}')
    result = store.tidy(keep_recent=5)
    assert result['runs'] == 1, 'only the one past the keep window goes'


def test_tidy_never_touches_the_library(store):
    _finished_run(store, failed=False, isrc='OWNED0000001')
    store.remember('isrc:OWNED0000001', '/music/x.opus', 'opus', 64, 'sha')
    store.tidy(keep_recent=0)
    assert store.own('isrc:OWNED0000001') is not None, 'what you own is sacred'
