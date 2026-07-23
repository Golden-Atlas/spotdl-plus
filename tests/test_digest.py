'''
test_digest.py - a run that ends with failures ends with a bucketed to-do list,
not a wall you read one line at a time.
'''

from __future__ import annotations

from typer.testing import CliRunner

from spotdlplus.cli.main import _load, app
from spotdlplus.core.errors import ErrorRecord, Retry
from spotdlplus.core.models import ArtistRef, Track
from spotdlplus.core.store import Store

runner = CliRunner()


def _seed_failures(vault, codes) -> None:
    cfg = _load(vault)
    store = Store(cfg.db_path)
    run = store.create_run('x', 'canonical')
    for i, code in enumerate(codes):
        t = Track(title=f'Song {i}', artists=(ArtistRef(name='A'),),
                  isrc=f'USZZZ00000{i:02d}', duration_ms=1000)
        tid, _ = store.add_track(run, t)
        store.fail(tid, ErrorRecord(code=code, message='boom', retry=Retry.BACKOFF,
                                    remedy='a remedy'))
    store.close()


def test_status_errors_shows_a_grouped_digest(tmp_path):
    vault = tmp_path / 'V'
    _seed_failures(vault, ['FETCH_FAILED', 'FETCH_FAILED', 'MATCH_NONE'])
    result = runner.invoke(app, ['status', '--errors', '-o', str(vault)])
    assert result.exit_code == 0
    assert 'need attention' in result.output
    # bucketed by cause, with the count
    assert 'FETCH_FAILED' in result.output and 'MATCH_NONE' in result.output
    # each bucket names the command that clears it
    assert 'relink --queue' in result.output
    assert 'resume --retry-failed' in result.output


def test_no_failures_no_digest(tmp_path):
    vault = tmp_path / 'Empty'
    _seed_failures(vault, [])
    result = runner.invoke(app, ['status', '--errors', '-o', str(vault)])
    assert result.exit_code == 0
    assert 'need attention' not in result.output
