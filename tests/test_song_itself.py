'''
test_song_itself.py - the 1.1.4 metadata layer, proven on real files.

Lyrics parsing and preference, the compilation flag in all three tag dialects,
the genre stamper's memoization, and the genres.json rebuild. Tag tests run
against a real ffmpeg tone because mocking mutagen proves nothing about mutagen.
'''

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from spotdlplus.core.models import Album, ArtistRef, Track
from spotdlplus.core.store import Store
from spotdlplus.media.lyrics import Lyrics, sidecar_path
from spotdlplus.media.tag import is_compilation, write_tags

A = ArtistRef(name='Radiohead', spotify_id='art1')
VA = ArtistRef(name='Various Artists')

FFMPEG = shutil.which('ffmpeg') is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason='ffmpeg not on PATH')

_LRC = '[00:01.00] first line\n[00:05.00] second line'


def _track(artists=(A,), genres=()):
    album = Album(title='Pablo Honey', artists=(artists[0],),
                  release_date='1993-02-22', total_tracks=12)
    return Track(title='Creep', artists=tuple(artists), album=album,
                 isrc='GBAYE9200001', duration_ms=238_000, track_no=2,
                 genres=tuple(genres))


@pytest.fixture
def tone(tmp_path):
    out = tmp_path / 'tone.opus'
    subprocess.run(
        ['ffmpeg', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
         '-c:a', 'libopus', '-b:a', '96k', str(out)],
        check=True, capture_output=True,
    )
    return out


# ----------------------------------------------------------------------------
# lyrics: the little value object
# ----------------------------------------------------------------------------

def test_lyrics_pick_prefers_what_you_asked_for_but_never_returns_nothing():
    both = Lyrics(synced=_LRC, plain='first line\nsecond line')
    assert both.pick('synced') == _LRC
    assert both.pick('plain') == 'first line\nsecond line'
    only_synced = Lyrics(synced=_LRC, plain=None)
    assert only_synced.pick('plain') == _LRC, 'plain preference still takes synced over nothing'
    only_plain = Lyrics(synced=None, plain='words')
    assert only_plain.pick('synced') == 'words'


def test_sidecar_path_matches_the_track_name(tmp_path):
    final = tmp_path / 'Radiohead' / '02 Creep.opus'
    assert sidecar_path(final).name == '02 Creep.lrc'


# ----------------------------------------------------------------------------
# compilation detection
# ----------------------------------------------------------------------------

def test_various_artists_is_a_compilation_and_a_band_is_not():
    va_album = Album(title='Now 47', artists=(VA,))
    va = Track(title='Hit', artists=(A,), album=va_album, isrc='X1',
               duration_ms=1000)
    assert is_compilation(va)
    assert not is_compilation(_track())


# ----------------------------------------------------------------------------
# tags on real files: lyrics + compilation land and read back
# ----------------------------------------------------------------------------

@needs_ffmpeg
def test_opus_carries_lyrics_and_genre(tone, tmp_path):
    from mutagen.oggopus import OggOpus
    f = tmp_path / 'x.opus'
    shutil.copy(tone, f)
    write_tags(f, _track(genres=('art rock', 'alternative')),
               fmt='opus', lyrics_text=_LRC)
    tags = OggOpus(str(f))
    assert tags['LYRICS'] == [_LRC]
    assert tags['GENRE'] == ['art rock', 'alternative']


@needs_ffmpeg
def test_lyrics_survive_the_round_trip_out_of_a_file(tone, tmp_path):
    # the export path: read the words back out of the archive copy, so the
    # device copy is never wordless (which it was, for exactly one release)
    from spotdlplus.media.lyrics import read_embedded_lyrics
    f = tmp_path / 'x.opus'
    shutil.copy(tone, f)
    write_tags(f, _track(), fmt='opus', lyrics_text=_LRC)
    assert read_embedded_lyrics(f) == _LRC
    bare = tmp_path / 'bare.opus'
    shutil.copy(tone, bare)
    write_tags(bare, _track(), fmt='opus')
    assert read_embedded_lyrics(bare) is None


@needs_ffmpeg
def test_m4a_carries_lyrics_and_compilation(tone, tmp_path):
    from mutagen.mp4 import MP4
    from spotdlplus.media.transcode import transcode
    f = tmp_path / 'x.m4a'
    transcode(tone, f, fmt='m4a', bitrate='128k')
    va_album = Album(title='Now 47', artists=(VA,))
    va = Track(title='Hit', artists=(A,), album=va_album, isrc='X1',
               duration_ms=2000)
    write_tags(f, va, fmt='m4a', lyrics_text=_LRC)
    tags = MP4(str(f))
    assert tags['\xa9lyr'] == [_LRC]
    assert bool(tags['cpil'])


@needs_ffmpeg
def test_mp3_carries_uslt_and_tcmp(tone, tmp_path):
    from mutagen.id3 import ID3
    from spotdlplus.media.transcode import transcode
    f = tmp_path / 'x.mp3'
    transcode(tone, f, fmt='mp3', bitrate='128k')
    va_album = Album(title='Now 47', artists=(VA,))
    va = Track(title='Hit', artists=(A,), album=va_album, isrc='X1',
               duration_ms=2000)
    write_tags(f, va, fmt='mp3', lyrics_text=_LRC)
    tags = ID3(str(f))
    assert tags.getall('USLT')[0].text == _LRC
    assert tags.getall('TCMP')[0].text == ['1']


# ----------------------------------------------------------------------------
# the genre stamper: one artist fetch, memoized, failure-silent
# ----------------------------------------------------------------------------

def test_genre_stamper_fetches_each_artist_once():
    from spotdlplus.pipeline.expand import _genre_stamper

    calls = []

    class FakeSp:
        def artist(self, aid):
            calls.append(aid)
            from spotdlplus.core.models import Artist
            return Artist(ref=A, genres=('art rock',))

    stamp = _genre_stamper(FakeSp())
    t1, t2 = stamp(_track()), stamp(_track())
    assert t1.genres == ('art rock',) and t2.genres == ('art rock',)
    assert calls == ['art1'], 'two tracks, one fetch. the memo is the point'


def test_genre_stamper_swallows_provider_failures():
    from spotdlplus.pipeline.expand import _genre_stamper

    class AngrySp:
        def artist(self, aid):
            raise RuntimeError('spotify is having a day')

    t = _genre_stamper(AngrySp())(_track())
    assert t.genres == (), 'no genres beats no track'


# ----------------------------------------------------------------------------
# genres.json: rebuilt whole, correct sections
# ----------------------------------------------------------------------------

def test_genres_json_has_the_three_sections(tmp_path):
    from spotdlplus.pipeline.genres import write_genres_json

    store = Store(tmp_path / 'jobs.db')
    try:
        run = store.create_run('x', 'canonical')
        t = _track(genres=('art rock', 'alternative'))
        store.add_track(run, t)
        store.remember(t.identity, str(tmp_path / 'c.opus'), 'opus', 64, 'sha')

        path = write_genres_json(store, tmp_path)
        data = json.loads(path.read_text(encoding='utf-8'))
        assert data['songs']['Creep — Radiohead'] == ['art rock', 'alternative']
        assert data['albums']['Radiohead — Pablo Honey'] == ['alternative', 'art rock']
        assert data['artists']['Radiohead'] == ['alternative', 'art rock']
    finally:
        store.close()


def test_genres_json_declines_to_write_an_empty_map(tmp_path):
    from spotdlplus.pipeline.genres import write_genres_json

    store = Store(tmp_path / 'jobs.db')
    try:
        run = store.create_run('x', 'canonical')
        t = _track()   # no genres
        store.add_track(run, t)
        store.remember(t.identity, str(tmp_path / 'c.opus'), 'opus', 64, 'sha')
        assert write_genres_json(store, tmp_path) is None
        assert not (tmp_path / 'genres.json').exists()
    finally:
        store.close()
