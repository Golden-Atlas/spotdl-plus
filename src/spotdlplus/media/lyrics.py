'''
lyrics.py - lyrics lookup and embedding

LRCLIB is the source. It's open, needs no key, and serves time-synced LRC when
it has it. The lookup matches exactly on artist, title, album, and duration.
That's strict on purpose, because fuzzy matching gets you the wrong song's
words scrolling over the right song, whcih is worse than having none.

Lyrics never fail a track. If they're missing we say so and move on.
'''

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..core.errors import SpotdlPlusError
from ..core.models import Track
from ..net.http import HttpClient

_API = 'https://lrclib.net/api/get'


@dataclass(frozen=True, slots=True)
class Lyrics:
    '''What came back: synced LRC when they have it, plain text as teh fallback.'''

    synced: str | None = None
    plain: str | None = None

    @property
    def best(self) -> str | None:
        return self.synced or self.plain

    def pick(self, prefer: str) -> str | None:
        '''`prefer` is 'synced' or 'plain'. either way you get SOMETHING if it exists.'''
        if prefer == 'plain':
            return self.plain or self.synced
        return self.synced or self.plain


def fetch_lyrics(track: Track, http: HttpClient) -> Lyrics | None:
    '''
    One exact lookup. Returns None for a clean miss and for any failure, and the
    caller announces both. Neither one ever fails the track.
    '''
    params = {
        'artist_name': track.artist,
        'track_name': track.title,
        'duration': round((track.duration_ms or 0) / 1000),
    }
    if track.album is not None:
        params['album_name'] = track.album.title
    try:
        # a miss is a 404 and the client raises typed errors for those. So the
        # whole not-found/offline/throttled family funnels into one quiet None
        data = http.get(_API, params=params).json()
    except SpotdlPlusError:
        return None
    if not isinstance(data, dict):
        return None
    synced = (data.get('syncedLyrics') or '').strip() or None
    plain = (data.get('plainLyrics') or '').strip() or None
    if synced is None and plain is None:
        return None
    return Lyrics(synced=synced, plain=plain)


def read_embedded_lyrics(path: Path) -> str | None:
    '''
    Pulls lyrics back out of a finished file, the same way read_embedded_cover does
    for art. Export needs it because the re-tag pass rebuilt everything from
    metadata except the lyrics, so device copies were coming out wordless.
    '''
    suffix = path.suffix.lower().lstrip('.')
    try:
        if suffix in ('opus', 'ogg'):
            from mutagen.oggopus import OggOpus
            vals = OggOpus(str(path)).get('LYRICS')
            return vals[0] if vals else None
        if suffix == 'flac':
            from mutagen.flac import FLAC
            vals = FLAC(str(path)).get('LYRICS')
            return vals[0] if vals else None
        if suffix in ('mp3', 'wav'):
            from mutagen.id3 import ID3
            uslt = ID3(str(path)).getall('USLT')
            return uslt[0].text if uslt else None
        if suffix in ('m4a', 'mp4'):
            from mutagen.mp4 import MP4
            vals = MP4(str(path)).get('\xa9lyr')
            return vals[0] if vals else None
    except Exception:  # noqa: BLE001  # Unreadable tags still leave the audio fine
        return None
    return None


def lyrics_cache_path(cache_dir: Path, track_id: str) -> Path:
    '''Where the fetched text waits between tagging and placement.'''
    d = cache_dir / 'lyrics'
    d.mkdir(parents=True, exist_ok=True)
    return d / f'{track_id}.lrc'


def sidecar_path(final: Path) -> Path:
    '''`02 Creep.opus` -> `02 Creep.lrc`, right beside it. Players just find it.'''
    return final.with_suffix('.lrc')
