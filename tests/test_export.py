'''
test_export.py - the archive becomes an iPod copy, offline and safe to re-run.

The real chain runs against ffmpeg: synthesize opus 'archive' files, register them
as owned, and export them to AAC .m4a, because mocking ffmpeg would only test my
assumptions about ffmpeg, not ffmpeg.
'''

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spotdlplus.cli.main import app
from spotdlplus.core.config import DEFAULT_TEMPLATE
from spotdlplus.core.models import Album, ArtistRef, Track
from spotdlplus.core.store import Store
from spotdlplus.media.tag import write_tags
from spotdlplus.media.transcode import probe, transcode
from spotdlplus.pipeline.export import export_library

FFMPEG = shutil.which('ffmpeg') is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason='ffmpeg not on PATH')

ARTIST = ArtistRef(name='Artist')


def _track(title: str, isrc: str) -> Track:
    album = Album(title='Album', artists=(ARTIST,), release_date='2020', total_tracks=2)
    return Track(title=title, artists=(ARTIST,), album=album, isrc=isrc,
                 duration_ms=1000, track_no=1)


@pytest.fixture(scope='module')
def tone(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp('tone') / 'src.webm'
    subprocess.run(
        ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
         '-c:a', 'libopus', '-b:a', '96k', str(out)],
        check=True, capture_output=True,
    )
    return out


def _seed(store: Store, lib: Path, tone: Path, titles) -> None:
    '''Put N tagged .opus 'archive' files on disk and mark them owned.'''
    lib.mkdir(parents=True, exist_ok=True)
    run = store.create_run('x', 'canonical')
    for i, (title, isrc) in enumerate(titles):
        t = _track(title, isrc)
        store.add_track(run, t)
        opus = lib / f'{title}.opus'
        transcode(tone, opus, fmt='opus', bitrate='192k')
        write_tags(opus, t, fmt='opus', cover=None)
        store.remember(t.identity, str(opus), 'opus', opus.stat().st_size, f'sha{i}')


TITLES = [('One', 'US0000000001'), ('Two', 'US0000000002')]


@needs_ffmpeg
def test_export_writes_aac_m4a_copies(tmp_path, tone):
    lib, dest = tmp_path / 'lib', tmp_path / 'ipod'
    store = Store(tmp_path / 'jobs.db')
    _seed(store, lib, tone, TITLES)
    try:
        rep = export_library(store, dest=dest, target_format='m4a',
                             bitrate='256k', template=DEFAULT_TEMPLATE)
    finally:
        store.close()

    assert rep.exported == 2 and rep.failed == 0 and rep.missing_source == 0
    m4as = sorted(dest.rglob('*.m4a'))
    assert len(m4as) == 2
    assert probe(m4as[0]).codec == 'aac'


@needs_ffmpeg
def test_re_export_skips_what_is_already_there(tmp_path, tone):
    lib, dest = tmp_path / 'lib', tmp_path / 'ipod'
    store = Store(tmp_path / 'jobs.db')
    _seed(store, lib, tone, TITLES)
    try:
        export_library(store, dest=dest, target_format='m4a', bitrate='256k',
                       template=DEFAULT_TEMPLATE)
        again = export_library(store, dest=dest, target_format='m4a', bitrate='256k',
                               template=DEFAULT_TEMPLATE)
    finally:
        store.close()
    assert again.exported == 0 and again.skipped == 2


@needs_ffmpeg
def test_deleting_one_copy_re_exports_exactly_that_one(tmp_path, tone):
    lib, dest = tmp_path / 'lib', tmp_path / 'ipod'
    store = Store(tmp_path / 'jobs.db')
    _seed(store, lib, tone, TITLES)
    try:
        export_library(store, dest=dest, target_format='m4a', bitrate='256k',
                       template=DEFAULT_TEMPLATE)
        sorted(dest.rglob('*.m4a'))[0].unlink()
        rep = export_library(store, dest=dest, target_format='m4a', bitrate='256k',
                             template=DEFAULT_TEMPLATE)
    finally:
        store.close()
    assert rep.exported == 1 and rep.skipped == 1


@needs_ffmpeg
def test_export_never_touches_the_archive(tmp_path, tone):
    lib, dest = tmp_path / 'lib', tmp_path / 'ipod'
    store = Store(tmp_path / 'jobs.db')
    _seed(store, lib, tone, TITLES)
    before = {p: p.read_bytes() for p in lib.glob('*.opus')}
    try:
        export_library(store, dest=dest, target_format='m4a', bitrate='256k',
                       template=DEFAULT_TEMPLATE)
    finally:
        store.close()
    after = {p: p.read_bytes() for p in lib.glob('*.opus')}
    assert before == after, 'the archive must be read-only during export'


@needs_ffmpeg
def test_missing_source_is_reported_not_fatal(tmp_path, tone):
    lib, dest = tmp_path / 'lib', tmp_path / 'ipod'
    store = Store(tmp_path / 'jobs.db')
    _seed(store, lib, tone, TITLES)
    (lib / 'One.opus').unlink()   # a human deleted an archived file
    try:
        rep = export_library(store, dest=dest, target_format='m4a', bitrate='256k',
                             template=DEFAULT_TEMPLATE)
    finally:
        store.close()
    assert rep.missing_source == 1 and rep.exported == 1


@needs_ffmpeg
def test_query_narrows_the_export(tmp_path, tone):
    lib, dest = tmp_path / 'lib', tmp_path / 'ipod'
    store = Store(tmp_path / 'jobs.db')
    _seed(store, lib, tone, TITLES)
    try:
        rep = export_library(store, dest=dest, target_format='m4a', bitrate='256k',
                             template=DEFAULT_TEMPLATE, query='One')
    finally:
        store.close()
    assert rep.total == 1 and rep.exported == 1


# ----------------------------------------------------------------------------
# the CLI surface (no ffmpeg needed)
# ----------------------------------------------------------------------------

runner = CliRunner()


def test_export_help_advertises_dest_and_to(command_options):
    opts = command_options('export')
    assert '--dest' in opts and '--to' in opts


def test_export_rejects_archive_as_a_target(tmp_path):
    result = runner.invoke(app, ['export', '--dest', str(tmp_path / 'd'),
                                 '--to', 'archive', '-o', str(tmp_path / 'v')])
    assert result.exit_code == 1
    assert 'device format' in result.output


def test_export_empty_library_says_so(tmp_path):
    result = runner.invoke(app, ['export', '--dest', str(tmp_path / 'd'),
                                 '-o', str(tmp_path / 'emptyvault')])
    assert result.exit_code == 0
    assert 'empty' in result.output
