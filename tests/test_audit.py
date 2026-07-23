'''
test_audit.py - the auditor audited.

Real opus files, real tags, real deficiencies induced on purpose. The claims:
a healthy file passes all five checks. Every induced deficiency is found with
the right kind. --fix repairs tags and art in place without touching audio
bytes. And the auditor never demands a tag we have no metadata for.
'''

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from spotdlplus.core.models import Album, ArtistRef, Track
from spotdlplus.core.store import Store
from spotdlplus.media.tag import write_tags
from spotdlplus.pipeline.audit import audit_library, read_tag_state

needs_ffmpeg = pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='no ffmpeg')

ARTIST = ArtistRef(name='Devon Hendryx')
COVER = b'\xff\xd8\xff' + b'\x00' * 200   # jpeg magic + padding


def make_track(title='Sakura', isrc='USAAA1300001', cover_url='https://i.scdn.co/x'):
    album = Album(title='The Ghost~Pop Tape', artists=(ARTIST,),
                  release_date='2013-07-01', total_tracks=38,
                  cover_url=cover_url, spotify_id='al-ghostpop')
    return Track(title=title, artists=(ARTIST,), album=album,
                 isrc=isrc, duration_ms=4_000, track_no=17)


@pytest.fixture
def tone_opus(tmp_path) -> Path:
    out = tmp_path / 'src.opus'
    subprocess.run(
        ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4',
         '-c:a', 'libopus', '-b:a', '96k', str(out)],
        check=True, capture_output=True,
    )
    return out


@pytest.fixture
def world(tmp_path, tone_opus):
    '''A tiny real library: store, output dir, and one placed+owned file.'''
    lib = tmp_path / 'Music'
    store = Store(tmp_path / 'jobs.db')

    def place(name: str, track: Track, *, cover, tag=True) -> Path:
        final = lib / 'Devon Hendryx' / 'The Ghost~Pop Tape (2013)' / name
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(tone_opus, final)
        if tag:
            write_tags(final, track, fmt='opus', cover=cover)
        run = store.create_run('x', 'canonical')
        tid, _ = store.add_track(run, track)
        store.remember(track.identity, str(final), 'opus',
                       final.stat().st_size, 'sha')
        return final

    yield store, lib, place
    store.close()


class NoHttp:
    '''Cover fetch that always "succeeds" from a canned byte string.'''

    class _R:
        content = COVER

    def get(self, url):
        return self._R()


# ----------------------------------------------------------------------------
# reading
# ----------------------------------------------------------------------------

@needs_ffmpeg
def test_a_fully_tagged_file_reads_as_complete(tone_opus, tmp_path):
    f = tmp_path / 'ok.opus'
    shutil.copy(tone_opus, f)
    write_tags(f, make_track(), fmt='opus', cover=COVER)
    missing, has_art = read_tag_state(f, 'opus')
    assert missing == set() and has_art


@needs_ffmpeg
def test_an_untagged_file_reads_as_missing_everything(tone_opus):
    missing, has_art = read_tag_state(tone_opus, 'opus')
    assert 'title' in missing and 'isrc' in missing
    assert not has_art


# ----------------------------------------------------------------------------
# the five claims
# ----------------------------------------------------------------------------

@needs_ffmpeg
def test_a_healthy_library_is_healthy(world):
    store, lib, place = world
    place('17 Sakura.opus', make_track(), cover=COVER)
    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache')
    assert report.checked == 1 and report.healthy == 1
    assert report.issues == []


@needs_ffmpeg
def test_missing_art_is_found_and_named(world):
    '''The Sakura case: tagged fine, art silently skipped at tag time.'''
    store, lib, place = world
    place('38 Sakura.opus', make_track(), cover=None)   # <- the defect
    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache')
    kinds = report.by_kind
    assert kinds == {'no_art': 1}
    assert report.issues[0].fixable


@needs_ffmpeg
def test_missing_tags_are_found_by_name(world):
    store, lib, place = world
    place('01 Bare.opus', make_track(title='Bare'), cover=COVER, tag=False)
    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache')
    tag_issues = [i for i in report.issues if i.kind == 'missing_tags']
    assert len(tag_issues) == 1
    assert 'title' in tag_issues[0].detail and 'isrc' in tag_issues[0].detail


@needs_ffmpeg
def test_a_vanished_file_is_reported_not_crashed(world):
    store, lib, place = world
    f = place('17 Sakura.opus', make_track(), cover=COVER)
    f.unlink()
    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache')
    assert report.by_kind == {'missing_file': 1}
    assert 'relink' in report.issues[0].detail


@needs_ffmpeg
def test_an_orphan_file_is_noticed(world):
    store, lib, place = world
    place('17 Sakura.opus', make_track(), cover=COVER)
    stray = lib / 'Devon Hendryx' / 'stray.opus'
    stray.write_bytes(b'not really audio but definitely present')
    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache')
    assert report.by_kind == {'orphan': 1}
    assert 'stray.opus' in report.issues[0].path


@needs_ffmpeg
def test_the_auditor_never_demands_what_we_never_knew(world):
    '''A track with no ISRC and no release date must not be flagged for lacking them.'''
    store, lib, place = world
    bare_album = Album(title='Unknown Sessions', artists=(ARTIST,), cover_url=None)
    t = Track(title='Mystery', artists=(ARTIST,), album=bare_album, duration_ms=4_000)
    place('01 Mystery.opus', t, cover=None)
    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache')
    assert report.by_kind.get('missing_tags') is None, 'isrc/date were unknowable'
    assert report.by_kind.get('no_art') is None, 'no cover_url means nothing to embed'


# ----------------------------------------------------------------------------
# fixing
# ----------------------------------------------------------------------------

@needs_ffmpeg
def test_fix_backfills_art_without_touching_audio(world):
    store, lib, place = world
    f = place('38 Sakura.opus', make_track(), cover=None)
    from spotdlplus.media.transcode import probe
    duration_before = probe(f).duration_ms

    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache',
                           http=NoHttp(), fix=True)

    assert report.fixed == 1 and report.fix_failed == 0
    missing, has_art = read_tag_state(f, 'opus')
    assert has_art, 'the cover is embedded now'
    assert missing == set(), 'the full tag set was rewritten alongside it'
    assert abs(probe(f).duration_ms - duration_before) < 50, 'audio untouched'


@needs_ffmpeg
def test_fix_rewrites_missing_tags(world):
    store, lib, place = world
    f = place('01 Bare.opus', make_track(title='Bare', isrc='USAAA1300002'),
              cover=COVER, tag=False)
    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache',
                           http=NoHttp(), fix=True)
    assert report.fixed >= 1
    missing, _ = read_tag_state(f, 'opus')
    assert missing == set()


# ----------------------------------------------------------------------------
# the sixth claim: identity
# ----------------------------------------------------------------------------

class FakeAcoustId:
    '''Scripted verdicts keyed by nothing, so every file gets the same answer.'''

    def __init__(self, verdict: str, detail: str = ''):
        from spotdlplus.providers.acoustid import AcoustMatch, Verdict
        self._m = AcoustMatch(Verdict(verdict), 0.95, ('mb-rec-x',), detail)
        self.calls = 0

    available = True

    def verify(self, fp, *, expected_recording_id):
        self.calls += 1
        return self._m


needs_fpcalc = pytest.mark.skipif(
    __import__('spotdlplus.media.fingerprint', fromlist=['find_fpcalc']).find_fpcalc() is None,
    reason='fpcalc not provisioned')


@needs_ffmpeg
@needs_fpcalc
def test_confirmed_identity_counts_and_stays_healthy(world):
    store, lib, place = world
    place('17 Sakura.opus', make_track(), cover=COVER)
    ac = FakeAcoustId('confirmed')
    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache', acoustid=ac)
    assert ac.calls == 1
    assert report.identity_confirmed == 1 and report.healthy == 1
    assert report.by_kind == {}


@needs_ffmpeg
@needs_fpcalc
def test_unknown_identity_flags_but_never_fails(world):
    '''Bedroom releases are honestly absent from AcoustID. Policy: flag only.'''
    store, lib, place = world
    place('17 Sakura.opus', make_track(), cover=COVER)
    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache',
                           acoustid=FakeAcoustId('unknown'))
    assert report.identity_unknown == 1
    assert report.healthy == 1, 'absence of evidence is not evidence of a wrong song'
    assert 'identity_mismatch' not in report.by_kind


@needs_ffmpeg
@needs_fpcalc
def test_a_mismatch_reports_without_fix_and_quarantines_with_it(world):
    store, lib, place = world
    f = place('17 Sakura.opus', make_track(), cover=COVER)

    # report-only first
    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache',
                           acoustid=FakeAcoustId('mismatch', 'audio matches mb-other'))
    assert report.identity_mismatch == 1
    assert report.by_kind.get('identity_mismatch') == 1
    assert f.is_file(), 'without --fix nothing moves'

    # and the policy response under fix
    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache',
                           http=NoHttp(), fix=True,
                           acoustid=FakeAcoustId('mismatch', 'audio matches mb-other'))
    assert report.quarantined == 1
    assert not f.is_file(), 'the condemned file left the library'
    q = list((lib / '.quarantine').glob('*.opus'))
    assert len(q) == 1, 'moved whole into quarantine, because it is evidence'
    assert store.own(make_track().identity) is None, 'the library no longer vouches'


@needs_ffmpeg
@needs_fpcalc
def test_a_quarantined_track_requeues_for_a_fresh_match(world):
    from spotdlplus.core.models import TrackState

    store, lib, place = world
    place('17 Sakura.opus', make_track(), cover=COVER)
    audit_library(store, output_dir=lib, cache_dir=lib / '.cache',
                  http=NoHttp(), fix=True, acoustid=FakeAcoustId('mismatch'))

    tid = store.newest_track_id(make_track().identity)
    row = store.get_track(tid)
    assert row.state is TrackState.ENRICHED, 'ready for a fresh match'
    assert row.chosen_url is None, 'the condemned source is cleared'


@needs_ffmpeg
def test_a_missing_file_is_healed_under_fix_not_merely_reported(world):
    '''Once "unfixable". Now --fix revokes the ghost ownership and requeues.'''
    store, lib, place = world
    f = place('17 Sakura.opus', make_track(), cover=COVER)
    f.unlink()
    report = audit_library(store, output_dir=lib, cache_dir=lib / '.cache',
                           http=NoHttp(), fix=True)
    assert report.fixed == 1
    assert report.by_kind == {'missing_file': 1}   # reported AND healed
    assert 'requeued' in report.issues[0].detail
    assert store.own(make_track().identity) is None, 'the ghost is revoked'
