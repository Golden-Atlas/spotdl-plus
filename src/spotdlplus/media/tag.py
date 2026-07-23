'''
tag.py - metadata embedding

The file has to survive the database. If jobs.db disappeared tomorrow every
placed file would still carry its whole identity: title, artists, album, track
and disc numbers, dates, label, ISRC, UPC, MusicBrainz ids, and art. The
metadata lives in the artifact, not next to it.

The album artist owns the release and decides the folder. The artist list
carries everyone including features, in order.

Each format wants somehting different. Vorbis takes uppercase repeated keys,
ID3 wants frames, MP4 wants atoms. One table handles all of it so no other
file has to know.
'''

from __future__ import annotations

import base64
from pathlib import Path

from ..core.errors import TagWriteFailed
from ..core.models import Track


def is_compilation(track: Track) -> bool:
    '''
    Flags various-artists records so iPods group them as one album instead of
    scattering 20 one-track albums across the device.
    '''
    return (track.album_artist or '').strip().lower() in (
        'various artists', 'various', 'va')


def _vorbis_pairs(track: Track, lyrics_text: str | None = None) -> list[tuple[str, str]]:
    '''The canonical tag set. Every other dialect is a translation of this.'''
    al = track.album
    pairs: list[tuple[str, str]] = [('TITLE', track.title)]
    pairs += [('ARTIST', a.name) for a in track.artists]
    pairs += [('ALBUMARTIST', track.album_artist)]
    if al is not None:
        pairs += [('ALBUM', al.title)]
        if al.release_date:
            pairs += [('DATE', al.release_date)]
        if al.original_date and al.original_date != al.release_date:
            pairs += [('ORIGINALDATE', al.original_date)]
        if al.label:
            pairs += [('LABEL', al.label), ('ORGANIZATION', al.label)]
        if al.copyright:
            pairs += [('COPYRIGHT', al.copyright)]
        if al.upc:
            pairs += [('BARCODE', al.upc)]
        if al.release_group_id:
            pairs += [('MUSICBRAINZ_RELEASEGROUPID', al.release_group_id)]
        if al.total_tracks:
            pairs += [('TRACKTOTAL', str(al.total_tracks))]
        if al.total_discs:
            pairs += [('DISCTOTAL', str(al.total_discs))]
    if track.track_no:
        pairs += [('TRACKNUMBER', str(track.track_no))]
    if track.disc_no:
        pairs += [('DISCNUMBER', str(track.disc_no))]
    if track.isrc:
        pairs += [('ISRC', track.isrc)]
    if track.genres:
        pairs += [('GENRE', g) for g in track.genres[:3]]
    if track.spotify_id:
        pairs += [('SPOTIFY_TRACK_ID', track.spotify_id)]
    if is_compilation(track):
        pairs += [('COMPILATION', '1')]
    if lyrics_text:
        pairs += [('LYRICS', lyrics_text)]
    return pairs


def _flac_picture(cover: bytes) -> 'object':
    from mutagen.flac import Picture
    pic = Picture()
    pic.type = 3                      # front cover
    pic.mime = 'image/jpeg' if cover[:3] == b'\xff\xd8\xff' else 'image/png'
    pic.desc = 'cover'
    pic.data = cover
    return pic


def _tag_vorbis(path: Path, track: Track, cover: bytes | None, kind: str,
                lyrics_text: str | None = None) -> None:
    if kind == 'opus':
        from mutagen.oggopus import OggOpus
        audio = OggOpus(str(path))
    else:
        from mutagen.flac import FLAC
        audio = FLAC(str(path))

    audio.delete()
    grouped: dict[str, list[str]] = {}
    for key, value in _vorbis_pairs(track, lyrics_text):
        grouped.setdefault(key, []).append(value)
    for key, values in grouped.items():
        audio[key] = values

    if cover is not None:
        pic = _flac_picture(cover)
        if kind == 'opus':
            audio['METADATA_BLOCK_PICTURE'] = [
                base64.b64encode(pic.write()).decode('ascii')
            ]
        else:
            audio.clear_pictures()
            audio.add_picture(pic)
    audio.save()


def _tag_id3(path: Path, track: Track, cover: bytes | None,
             lyrics_text: str | None = None) -> None:
    from mutagen.id3 import (
        APIC, ID3, TALB, TCMP, TCON, TCOP, TDOR, TDRC, TIT2, TPE1, TPE2, TPOS,
        TPUB, TRCK, TSRC, TXXX, USLT,
    )
    try:
        tags = ID3(str(path))
        tags.delete()
    except Exception:  # noqa: BLE001  # No existing tag block is fine
        tags = ID3()

    al = track.album
    tags.add(TIT2(encoding=3, text=track.title))
    tags.add(TPE1(encoding=3, text=[a.name for a in track.artists]))
    tags.add(TPE2(encoding=3, text=track.album_artist))
    if al is not None:
        tags.add(TALB(encoding=3, text=al.title))
        if al.release_date:
            tags.add(TDRC(encoding=3, text=al.release_date))
        if al.original_date:
            tags.add(TDOR(encoding=3, text=al.original_date))
        if al.label:
            tags.add(TPUB(encoding=3, text=al.label))
        if al.copyright:
            tags.add(TCOP(encoding=3, text=al.copyright))
        if al.upc:
            tags.add(TXXX(encoding=3, desc='BARCODE', text=al.upc))
    if track.track_no:
        total = f'/{al.total_tracks}' if al and al.total_tracks else ''
        tags.add(TRCK(encoding=3, text=f'{track.track_no}{total}'))
    if track.disc_no:
        dtotal = f'/{al.total_discs}' if al and al.total_discs else ''
        tags.add(TPOS(encoding=3, text=f'{track.disc_no}{dtotal}'))
    if track.isrc:
        tags.add(TSRC(encoding=3, text=track.isrc))
    if track.genres:
        tags.add(TCON(encoding=3, text=list(track.genres[:3])))
    if is_compilation(track):
        tags.add(TCMP(encoding=3, text='1'))
    if lyrics_text:
        # USLT is where players look. LRC timestamps inside it are a widely
        # honored convention (Poweramp, Musicolet, most of teh good ones).
        tags.add(USLT(encoding=3, lang='eng', desc='', text=lyrics_text))
    if cover is not None:
        mime = 'image/jpeg' if cover[:3] == b'\xff\xd8\xff' else 'image/png'
        tags.add(APIC(encoding=3, mime=mime, type=3, desc='cover', data=cover))
    # ID3v2.3, not mutagen's default v2.4. ITunes/iPod have always been twitchy
    # about v2.4 and will silently ignore fields it doesn't like. v2.3 just
    # works.
    tags.save(str(path), v2_version=3)


def _tag_mp4(path: Path, track: Track, cover: bytes | None,
             lyrics_text: str | None = None) -> None:
    from mutagen.mp4 import MP4, MP4Cover
    audio = MP4(str(path))
    audio.delete()
    al = track.album
    audio['\xa9nam'] = [track.title]
    audio['\xa9ART'] = [track.artists_display]
    audio['aART'] = [track.album_artist]
    if al is not None:
        audio['\xa9alb'] = [al.title]
        if al.release_date:
            audio['\xa9day'] = [al.release_date]
        if al.copyright:
            audio['cprt'] = [al.copyright]
        if track.track_no:
            audio['trkn'] = [(track.track_no, al.total_tracks or 0)]
        if track.disc_no:
            audio['disk'] = [(track.disc_no, al.total_discs or 0)]
    if track.genres:
        audio['\xa9gen'] = [track.genres[0]]
    if is_compilation(track):
        audio['cpil'] = True
    if lyrics_text:
        audio['\xa9lyr'] = [lyrics_text]
    if track.isrc:
        audio['----:com.apple.iTunes:ISRC'] = [track.isrc.encode()]
    if cover is not None:
        kind = MP4Cover.FORMAT_JPEG if cover[:3] == b'\xff\xd8\xff' else MP4Cover.FORMAT_PNG
        audio['covr'] = [MP4Cover(cover, imageformat=kind)]
    audio.save()


def write_tags(path: Path, track: Track, *, fmt: str, cover: bytes | None = None,
               lyrics_text: str | None = None) -> None:
    '''Embed everything we know into `path`. The one public verb.'''
    try:
        match fmt:
            case 'opus':
                _tag_vorbis(path, track, cover, 'opus', lyrics_text)
            case 'flac':
                _tag_vorbis(path, track, cover, 'flac', lyrics_text)
            case 'mp3' | 'wav':
                _tag_id3(path, track, cover, lyrics_text)
            case 'm4a':
                _tag_mp4(path, track, cover, lyrics_text)
            case _:
                raise TagWriteFailed(f'no tagger for format {fmt!r}', context={'fmt': fmt})
    except TagWriteFailed:
        raise
    except Exception as exc:  # noqa: BLE001  # Mutagen raises a small zoo
        raise TagWriteFailed(
            f'could not embed tags into {path.name}',
            context={'path': str(path), 'fmt': fmt, 'error': str(exc)},
            cause=exc,
        ) from exc
