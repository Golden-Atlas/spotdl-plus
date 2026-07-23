'''
test_media.py - the last ten feet, held to account.

Path rendering is pure and gets exhaustive treatment: every character Windows
forbids, reserved device names, features filing under the album owner. The
transcode/tag chain runs against a real ffmpeg-synthesized tone, two seconds of
sine wave, because mocking ffmpeg tests my assumptions about ffmpeg, and my
assumptions are the thing under test.
'''

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from spotdlplus.core.models import Album, ArtistRef, Track
from spotdlplus.media.place import (
    place_atomic,
    render_path,
    sanitize_segment,
    template_fields,
)
from spotdlplus.media.tag import write_tags
from spotdlplus.media.transcode import probe, transcode

TEMPLATE = '{album_artist}/{album} ({year})/{track_tag} {title}.{ext}'

BABEHEAVEN = ArtistRef(name='Babeheaven')
NAVY_BLUE = ArtistRef(name='Navy Blue')

FFMPEG = shutil.which('ffmpeg') is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason='ffmpeg not on PATH')


def track(title='Song', artists=(BABEHEAVEN,), album_title='Home For Now',
          year='2020-11-06', track_no=3, total_discs=1, **kw):
    album = Album(title=album_title, artists=(artists[0],), release_date=year,
                  total_tracks=10, total_discs=total_discs, label='AWAL',
                  upc='5056167129999')
    return Track(title=title, artists=tuple(artists), album=album,
                 isrc='GB45A2102651', duration_ms=202_000, track_no=track_no, **kw)


# ----------------------------------------------------------------------------
# sanitization: every hostile title we could think of
# ----------------------------------------------------------------------------

@pytest.mark.parametrize('nasty, safe', [
    ('AM/PM', 'AMPM'),
    ('What Is Love?', 'What Is Love'),
    ('Colon: The Song', 'Colon The Song'),
    ('"Quoted"', 'Quoted'),
    ('Wait|For|It', 'WaitForIt'),
    ('Ends with dot.', 'Ends with dot'),
    ('Ends with space ', 'Ends with space'),
    ('   ', '_'),
    ('***', '_'),
    ('a  b   c', 'a b c'),
])
def test_illegal_characters_are_removed_not_fatal(nasty, safe):
    assert sanitize_segment(nasty) == safe


def test_reserved_device_names_cannot_brick_the_path():
    '''A song literally titled CON would otherwise create an unopenable file.'''
    assert sanitize_segment('CON') == '_CON'
    assert sanitize_segment('con.opus') == '_con.opus'
    assert sanitize_segment('Consequences') == 'Consequences'


def test_unicode_survives():
    '''Devon Hendryx has an album titled ❤︎. It must remain ❤︎.'''
    assert sanitize_segment('❤︎') == '❤︎'
    assert sanitize_segment('Café Racer') == 'Café Racer'


def test_very_long_titles_are_capped_without_trailing_garbage():
    s = sanitize_segment('x' * 300 + ' .')
    assert len(s) <= 120 and not s.endswith((' ', '.'))


def test_the_cap_never_amputates_the_extension():
    '''
    The Costa Rica lesson: a 14-artist feature list pushed the filename past
    the segment cap and the old code chopped '.opus' off the end, shipping a
    file no player or tool would open by name.
    '''
    long_name = '01 Costa Rica (with ' + ', '.join(f'Artist {i}' for i in range(14)) + ').opus'
    s = sanitize_segment(long_name)
    assert s.endswith('.opus')
    assert len(s) <= 120
    assert not s.rstrip('.opus').endswith('(')


def test_a_dot_inside_a_title_is_not_an_extension():
    s = sanitize_segment(('S.O.S. ' + 'x' * 200))
    assert len(s) <= 120   # no 8+ char 'extension' gets protected


def test_the_whole_path_stays_under_the_windows_budget(tmp_path):
    '''Two individually-legal segments must not jointly cross MAX_PATH.'''
    posse = 'Costa Rica (with ' + ', '.join(f'Feature Artist Number {i}' for i in range(10)) + ')'
    t = Track(title=posse, artists=(BABEHEAVEN,),
              album=Album(title=posse, artists=(BABEHEAVEN,), release_date='2019-07-05',
                          total_tracks=10),
              duration_ms=218_000, track_no=1)
    final = render_path(t, template=TEMPLATE, output_dir=tmp_path, ext='opus')
    assert len(str(final)) <= 240
    assert final.suffix == '.opus', 'the extension survives any squeeze'
    # the album folder may shed its unclosed feature-list parenthetical
    # entirely ('Costa Rica (with ...' -> 'Costa Rica'), which is the good
    # outcome. what it must never be is empty or mid-paren garbage
    assert len(final.parent.name) >= 8
    assert not final.parent.name.endswith('(')


# ----------------------------------------------------------------------------
# the feature rule: files under the owner, credits everyone
# ----------------------------------------------------------------------------

def test_a_feature_files_under_the_album_owner(tmp_path):
    t = track(title='Make Me Wanna', artists=(BABEHEAVEN, NAVY_BLUE))
    final = render_path(t, template=TEMPLATE, output_dir=tmp_path, ext='opus')

    assert final.parts[-3] == 'Babeheaven', 'the owner gets the folder'
    assert 'Navy Blue' not in str(final), 'the feature is credit, not geography'

    fields = template_fields(t, ext='opus')
    assert fields['artists'] == 'Babeheaven, Navy Blue', 'but the credit survives'


def test_single_disc_albums_are_not_littered_with_disc_numbers(tmp_path):
    final = render_path(track(track_no=3), template=TEMPLATE, output_dir=tmp_path, ext='opus')
    assert final.name == '03 Make Me Wanna.opus' or final.name.startswith('03 ')


def test_multi_disc_albums_do_carry_the_disc(tmp_path):
    t = track(title='Pyramid Song', total_discs=2, disc_no=2, track_no=1)
    final = render_path(t, template=TEMPLATE, output_dir=tmp_path, ext='opus')
    assert final.name.startswith('2-01 ')


def test_a_missing_year_does_not_leave_empty_parens(tmp_path):
    t = Track(title='Mystery', artists=(BABEHEAVEN,),
              album=Album(title='Undated', artists=(BABEHEAVEN,)), duration_ms=1000)
    final = render_path(t, template=TEMPLATE, output_dir=tmp_path, ext='opus')
    assert '()' not in str(final)
    assert final.parts[-2] == 'Undated'


def test_a_track_with_no_album_files_under_singles(tmp_path):
    t = Track(title='Loose One', artists=(BABEHEAVEN,), duration_ms=1000)
    final = render_path(t, template=TEMPLATE, output_dir=tmp_path, ext='opus')
    assert 'Singles' in final.parts


def test_nothing_escapes_the_library_root(tmp_path):
    '''
    Traversal is neutralized at the sanitizer: '..' strips to nothing and
    becomes '_', so a hostile template lands harmlessly INSIDE the root. The
    resolve-guard in render_path is a second, deeper line that should be
    unreachable, so this test pins the first line.
    '''
    t = track()
    final = render_path(t, template='../../{title}.{ext}', output_dir=tmp_path, ext='opus')
    assert final.resolve().is_relative_to(tmp_path.resolve())
    assert '..' not in final.parts


def test_a_title_that_is_itself_a_path_cannot_navigate(tmp_path):
    t = Track(title='D:/Windows/System32/evil', artists=(BABEHEAVEN,),
              album=Album(title='X', artists=(BABEHEAVEN,)), duration_ms=1000)
    final = render_path(t, template=TEMPLATE, output_dir=tmp_path, ext='opus')
    assert final.resolve().is_relative_to(tmp_path.resolve())
    assert 'System32' not in final.parts, 'slashes in metadata are not separators'


# ----------------------------------------------------------------------------
# atomic placement
# ----------------------------------------------------------------------------

def test_placement_is_whole_or_absent(tmp_path):
    src = tmp_path / 'src.bin'
    src.write_bytes(b'x' * 100_000)
    final = tmp_path / 'lib' / 'Artist' / 'Album' / '01 Song.opus'

    n = place_atomic(src, final)
    assert n == 100_000
    assert final.read_bytes() == b'x' * 100_000
    assert not list(final.parent.glob('.*part')), 'no droppings left behind'


# ----------------------------------------------------------------------------
# the real chain: synthesize -> transcode -> tag -> read back
# ----------------------------------------------------------------------------

@pytest.fixture(scope='module')
def tone(tmp_path_factory) -> Path:
    '''Two seconds of A440 as webm/opus, the same shape yt-dlp delivers.'''
    out = tmp_path_factory.mktemp('tone') / 'src.webm'
    subprocess.run(
        ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
         '-c:a', 'libopus', '-b:a', '96k', str(out)],
        check=True, capture_output=True,
    )
    return out


@needs_ffmpeg
def test_same_codec_is_stream_copied_not_reencoded(tone, tmp_path):
    dst = tmp_path / 'out.opus'
    was_copy = transcode(tone, dst, fmt='opus', bitrate='192k')
    assert was_copy, 'opus -> opus must not pay a second lossy generation'
    info = probe(dst)
    assert info.codec == 'opus'
    assert abs(info.duration_ms - 2000) < 200


@needs_ffmpeg
def test_a_different_codec_is_reencoded(tone, tmp_path):
    dst = tmp_path / 'out.mp3'
    was_copy = transcode(tone, dst, fmt='mp3', bitrate='192k')
    assert not was_copy
    assert probe(dst).codec == 'mp3'


@needs_ffmpeg
def test_alac_encodes_apple_lossless_into_an_m4a(tone, tmp_path):
    '''iPod lossless: the codec is alac, the container is m4a.'''
    from spotdlplus.media.transcode import ext_for
    assert ext_for('alac') == 'm4a'
    dst = tmp_path / f'out.{ext_for("alac")}'
    was_copy = transcode(tone, dst, fmt='alac', bitrate='256k')
    assert not was_copy, 'opus source is not alac, so it re-encodes'
    assert probe(dst).codec == 'alac'
    assert abs(probe(dst).duration_ms - 2000) < 200


@needs_ffmpeg
def test_alac_files_tag_through_the_m4a_dialect(tone, tmp_path):
    '''An ALAC .m4a is MP4 like any other, so the m4a tagger has to handle it.'''
    from mutagen.mp4 import MP4

    dst = tmp_path / 'lossless.m4a'
    transcode(tone, dst, fmt='alac', bitrate='256k')
    t = track(title='Make Me Wanna', artists=(BABEHEAVEN, NAVY_BLUE))
    write_tags(dst, t, fmt='m4a', cover=None)   # stages pass the container, not 'alac'

    tags = MP4(str(dst))
    assert tags['\xa9nam'][0] == 'Make Me Wanna'
    assert probe(dst).codec == 'alac', 'tagging must not disturb the audio codec'


@needs_ffmpeg
def test_mp3_is_tagged_as_id3v2_3_for_itunes(tone, tmp_path):
    from mutagen.id3 import ID3
    dst = tmp_path / 'out.mp3'
    transcode(tone, dst, fmt='mp3', bitrate='192k')
    write_tags(dst, track(disc_no=1), fmt='mp3', cover=None)
    assert ID3(str(dst)).version[:2] == (2, 3), 'iTunes wants v2.3, not v2.4'


@needs_ffmpeg
def test_mp3_carries_track_and_disc_totals(tone, tmp_path):
    from mutagen.id3 import ID3
    dst = tmp_path / 'out.mp3'
    transcode(tone, dst, fmt='mp3', bitrate='192k')
    write_tags(dst, track(track_no=3, disc_no=1), fmt='mp3', cover=None)
    tags = ID3(str(dst))
    assert str(tags['TRCK']) == '3/10'
    assert str(tags['TPOS']) == '1/1'


@needs_ffmpeg
def test_tags_round_trip_through_an_opus_file(tone, tmp_path):
    '''Write everything, read it back cold. The file must survive the database.'''
    from mutagen.oggopus import OggOpus

    dst = tmp_path / 'tagged.opus'
    transcode(tone, dst, fmt='opus', bitrate='192k')

    t = track(title='Make Me Wanna', artists=(BABEHEAVEN, NAVY_BLUE))
    fake_cover = b'\xff\xd8\xff' + b'\x00' * 64   # jpeg magic + padding
    write_tags(dst, t, fmt='opus', cover=fake_cover)

    back = OggOpus(str(dst))
    assert back['TITLE'] == ['Make Me Wanna']
    assert back['ARTIST'] == ['Babeheaven', 'Navy Blue'], 'both credited, in order'
    assert back['ALBUMARTIST'] == ['Babeheaven'], 'the owner owns it'
    assert back['ISRC'] == ['GB45A2102651']
    assert back['LABEL'] == ['AWAL']
    assert back['BARCODE'] == ['5056167129999']
    assert back['TRACKNUMBER'] == ['3']
    assert 'METADATA_BLOCK_PICTURE' in back, 'cover art embedded'


@needs_ffmpeg
def test_tags_round_trip_through_mp3(tone, tmp_path):
    from mutagen.id3 import ID3

    dst = tmp_path / 'tagged.mp3'
    transcode(tone, dst, fmt='mp3', bitrate='128k')
    write_tags(dst, track(), fmt='mp3', cover=None)

    back = ID3(str(dst))
    assert str(back['TIT2']) == 'Make Me Wanna' or str(back['TIT2']) == 'Song'
    assert str(back['TPE2']) == 'Babeheaven'
    assert str(back['TSRC']) == 'GB45A2102651'
