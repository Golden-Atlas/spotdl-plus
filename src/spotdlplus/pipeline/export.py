'''
export.py - making a device copy of the library

This is what makes iPod support additive instead of a fork. Your library stays
opus and verified, and export writes a second copy in whatever the device
wants.

Three promises. It never touches the network, since it transcodes the archived
master and re-embeds its own art. It never writes anywhere except dest. And
it's safe to re-run, because anything already there gets left alone, so a
killed export picks back up and adding one song copies one song.
'''

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..core.errors import SpotdlPlusError
from ..core.store import Store
from ..media.covers import read_embedded_cover, write_folder_art
from ..media.place import render_path
from ..media.tag import write_tags
from ..media.transcode import ext_for, transcode

#: progress(done, total, path). The CLI draws a bar off this. None runs silent.
Progress = Callable[[int, int, str], None]


@dataclass(slots=True)
class ExportReport:
    '''What the export turned into. Every number is a question someone asks.'''

    total: int = 0
    exported: int = 0        # freshly written this run
    skipped: int = 0         # already at the destination
    missing_source: int = 0  # owned, but the archived file is gone from disk
    failed: int = 0          # transcode/tag blew up
    #: (source path, reason) for everything that didn't make it, a to-do list.
    problems: list[tuple[str, str]] = field(default_factory=list)


def export_library(
    store: Store,
    *,
    dest: Path,
    target_format: str,
    bitrate: str,
    template: str,
    query: str | None = None,
    force: bool = False,
    progress: Progress | None = None,
) -> ExportReport:
    '''
    Copies every owned file into `dest` in `target_format`. `query` narrows it to
    paths containing that string, and `force` rewrites copies that are already
    there.
    '''
    rows = store.library_rows()
    if query:
        needle = query.lower()
        rows = [r for r in rows if needle in (r['final_path'] or '').lower()]

    report = ExportReport(total=len(rows))
    ext = ext_for(target_format)

    for i, row in enumerate(rows, 1):
        if progress is not None:
            progress(i, len(rows), row['final_path'])

        src = Path(row['final_path'])
        if not src.is_file():
            report.missing_source += 1
            report.problems.append((str(src), 'owned, but the file is gone from disk'))
            continue

        track = store.metadata_for_identity(row['identity'])
        if track is None:
            report.failed += 1
            report.problems.append((str(src), 'no stored metadata to tag with'))
            continue

        final = render_path(track, template=template, output_dir=dest, ext=ext)
        if not force and final.is_file() and final.stat().st_size > 0:
            report.skipped += 1
            continue

        # Write to a sibling temp first and swap it in, so the destination only
        # ever holds a complete file and the resume check can trust it. The
        # real extension stays last (`...part.m4a`) so ffmpeg can still tell
        # what container it is. A plain `.part` leaves it guessing and it
        # fails.
        tmp = final.with_name(f'{final.stem}.part{final.suffix}')
        try:
            from ..media.lyrics import read_embedded_lyrics
            cover = read_embedded_cover(src)
            words = read_embedded_lyrics(src)   # the archive already knows them
            final.parent.mkdir(parents=True, exist_ok=True)
            transcode(src, tmp, fmt=target_format, bitrate=bitrate)
            write_tags(tmp, track, fmt=ext, cover=cover, lyrics_text=words)
            os.replace(tmp, final)                       # atomic within the folder
            if cover is not None:
                write_folder_art(final.parent, cover)
            report.exported += 1
        except SpotdlPlusError as exc:
            tmp.unlink(missing_ok=True)
            report.failed += 1
            report.problems.append((str(src), f'[{exc.code}] {exc.message}'))
        except Exception as exc:  # noqa: BLE001  # One bad file must not end the export
            tmp.unlink(missing_ok=True)
            report.failed += 1
            report.problems.append((str(src), f'{type(exc).__name__}: {exc}'))

    return report
