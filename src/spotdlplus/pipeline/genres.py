'''
genres.py - the genre map in the library root

Tags already carry each file's genres but tags are per-file and invisible in
bulk, so this is the aggregate view across songs, albums, and artists. It gets
rebuilt whole after every run, because a patcher that misses one edge case lies
forever and a rebuild can't.
'''

from __future__ import annotations

import json
from pathlib import Path

from ..core.store import Store


def write_genres_json(store: Store, output_dir: Path) -> Path | None:
    '''
    Rebuilds genres.json from stored metadata and returns the path, or None when
    nothing is owned yet since an empty map helps nobody.
    '''
    rows = store.library_rows()
    if not rows:
        return None

    songs: dict[str, list[str]] = {}
    albums: dict[str, set[str]] = {}
    artists: dict[str, set[str]] = {}
    for row in rows:
        track = store.metadata_for_identity(row['identity'])
        if track is None or not track.genres:
            continue
        genres = list(track.genres)
        songs[f'{track.title} — {track.artist}'] = genres
        if track.album is not None:
            albums.setdefault(f'{track.album_artist} — {track.album.title}',
                              set()).update(genres)
        artists.setdefault(track.artist, set()).update(genres)

    if not songs:
        return None   # nothing genre-tagged yet. an empty map helps nobody

    out = {
        'songs': dict(sorted(songs.items())),
        'albums': {k: sorted(v) for k, v in sorted(albums.items())},
        'artists': {k: sorted(v) for k, v in sorted(artists.items())},
    }
    path = output_dir / 'genres.json'
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + '\n',
                    encoding='utf-8')
    return path
