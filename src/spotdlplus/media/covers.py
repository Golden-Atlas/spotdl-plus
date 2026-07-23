'''
covers.py - album art

Missing art never fails a track since the audio is the thing we came for. But
every failure gets announced and `audit --fix` backfills it later. Degradation
you can see is a to-do list, degradation you can't see is rot.
'''

from __future__ import annotations

import hashlib
from pathlib import Path

from ..core.errors import SpotdlPlusError
from ..core.events import EventBus, NullBus, Warned
from ..core.models import Album
from ..net.http import HttpClient


def cover_cache_path(album: Album, cache_dir: Path) -> Path:
    key = album.spotify_id or hashlib.sha1((album.cover_url or '').encode()).hexdigest()[:16]
    return cache_dir / 'covers' / f'{key}.img'


def fetch_cover(
    album: Album | None,
    http: HttpClient,
    cache_dir: Path,
    *,
    bus: EventBus | None = None,
    run_id: str = '',
) -> bytes | None:
    '''
    Gets the album's front cover from cache or the CDN, one fetch per album ever.
    Returns None only for a real absence or a failure we announced, never one we
    swallowed.
    '''
    bus = bus or NullBus()
    if album is None or not album.cover_url:
        return None

    cache = cover_cache_path(album, cache_dir)
    if cache.is_file():
        return cache.read_bytes()

    try:
        data = http.get(album.cover_url).content
    except SpotdlPlusError as exc:
        bus.emit(Warned(
            run_id=run_id,
            message=f'cover art for {album.title!r} did not fetch ([{exc.code}]), '
                    f'"spotdlp audit --fix" backfills it later',
            context={'album': album.title, 'error': exc.code, 'url': album.cover_url},
        ))
        return None
    except Exception as exc:  # noqa: BLE001  # Still never fail a track over art
        bus.emit(Warned(
            run_id=run_id,
            message=f'cover art for {album.title!r} did not fetch ({type(exc).__name__})',
            context={'album': album.title, 'url': album.cover_url},
        ))
        return None

    if not data or len(data) < 128:
        bus.emit(Warned(
            run_id=run_id,
            message=f'cover art for {album.title!r} came back empty',
            context={'album': album.title, 'url': album.cover_url},
        ))
        return None

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data


def read_embedded_cover(path: Path) -> bytes | None:
    '''
    Pulls the cover back out of a finished file without touching the network.
    Export needs this because transcoding drops the picture and re-fetching would
    break the offline promise. Anything that goes wrong returns None.
    '''
    suffix = path.suffix.lower().lstrip('.')
    try:
        if suffix == 'opus':
            import base64

            from mutagen.flac import Picture
            from mutagen.oggopus import OggOpus
            block = OggOpus(str(path)).get('metadata_block_picture')
            if block:
                return Picture(base64.b64decode(block[0])).data
        elif suffix == 'flac':
            from mutagen.flac import FLAC
            pics = FLAC(str(path)).pictures
            if pics:
                return pics[0].data
        elif suffix in ('mp3', 'wav'):
            from mutagen.id3 import ID3
            apics = ID3(str(path)).getall('APIC')
            if apics:
                return apics[0].data
        elif suffix in ('m4a', 'mp4', 'alac'):
            from mutagen.mp4 import MP4
            covr = MP4(str(path)).get('covr')
            if covr:
                return bytes(covr[0])
    except Exception:  # noqa: BLE001  # A file with unreadable art still has audio
        return None
    return None


def write_folder_art(album_dir: Path, cover: bytes) -> bool:
    '''
    Drops a cover.jpg next to the tracks. Explorer and a lot of players can't read
    embedded art out of an .opus at all, so the tags are right and invisible. Never
    overwrites one you put there yourself.
    '''
    target = album_dir / 'cover.jpg'
    if target.exists():
        return False
    album_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(cover)
    return True
