'''
place.py - path building and atomic placement

Two jobs here, both about not corrupting the library.

Titles show up with every character Windows forbids in them, plus reserved
device names, so a song called CON would break a naive path join. Each segment
gets sanitized on its own and the template's slashes are the only directory
separators that survive.

The cache is on C: and the library is on D:, and a cross-volume rename quietly
turns into copy-then-delete with a window where a torn file looks finished. So
we copy to a .part on the destination volume, fsync it, then os.replace, whcih
is atomic inside one volume. A crash leaves the old file or the new one and
never 60% of a song that a library scanner would happily index.
'''

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from ..core.errors import FsDriveLost, NoSpace, PathUnwritable
from ..core.models import Track

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACES = re.compile(r'\s+')
_EMPTY_PARENS = re.compile(r'\s*\(\s*\)')
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL',
             *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}

#: Per-segment cap. Windows caps full paths at 260 by default. A 120-char
#: segment under a short library root leaves comfortable headroom.
_MAX_SEGMENT = 120

#: Budget for the whole rendered path. Two long segments can each pass the per-
#: segment cap and still cross MAX_PATH together. I hit this on a Dreamville
#: posse cut whose 14 artist feature list made a 260 char path that ffprobe
#: tolerated and fpcalc couldn't even open.
_MAX_TOTAL_PATH = 240

#: A truncation that ends mid-parenthesis reads as damage. Trim the fragment.
_DANGLING_PAREN = re.compile(r'\s*\([^)]*$')


def clean_component(text: str) -> str:
    '''
    Cleans a metadata value on its way into the template. Illegal characters go,
    spaces collapse, and an empty result stays empty so a missing year renders as
    nothing and the empty-parens cleanup can take out its wrapper.
    '''
    s = _ILLEGAL.sub('', text)
    return _SPACES.sub(' ', s).strip()


def sanitize_segment(text: str, *, max_len: int = _MAX_SEGMENT) -> str:
    '''
    Makes one path component safe for NTFS without losing what it says. The length
    cap eats into the stem and never the extension, since chopping '.opus' off
    ships a file nothing recognizes. It did that once.
    '''
    s = _ILLEGAL.sub('', text)
    s = _EMPTY_PARENS.sub('', s)
    s = _SPACES.sub(' ', s).strip(' .')
    if not s:
        s = '_'
    if s.upper().split('.')[0] in _RESERVED:
        s = '_' + s
    if len(s) <= max_len:
        return s.rstrip(' .')

    stem, dot, ext = s.rpartition('.')
    # a real extension is short. a 'suffix' longer than 8 chars is just a dot
    # inside a title ("S.O.S. extended edition") and gets no protection
    if dot and stem and len(ext) <= 8:
        keep = max_len - len(ext) - 1
        stem = _DANGLING_PAREN.sub('', stem[:keep]).rstrip(' .')
        return f'{stem or "_"}.{ext}'
    s = _DANGLING_PAREN.sub('', s[:max_len]).rstrip(' .')
    return s or '_'


def template_fields(track: Track, *, ext: str) -> dict[str, object]:
    '''Everything a template may reference, with honest defaults for the holes.'''
    al = track.album
    year = al.year if al else None
    multi_disc = bool(al and al.total_discs and al.total_discs > 1)
    track_no = track.track_no or 0
    track_tag = f'{track.disc_no or 1}-{track_no:02d}' if multi_disc else f'{track_no:02d}'
    return {
        'artist': track.artist,
        'artists': track.artists_display,
        'album_artist': track.album_artist,
        'album': al.title if al else 'Singles',
        'title': track.title,
        'year': year if year is not None else '',
        'track_no': track_no,
        'disc_no': track.disc_no or 1,
        'track_tag': track_tag,
        'isrc': track.isrc or '',
        'ext': ext,
    }


def render_path(track: Track, *, template: str, output_dir: Path, ext: str) -> Path:
    '''
    Determines where the track goes with integrated sanitization.

    Tbh I fucked up my first iteration of this because I forgot song titles have
    slashes in them sometimes. Now the metadata gets scrubbed before it goes into
    the template, so the only slashes left when we split are the template's own.
    '''
    fields = {
        k: (clean_component(v) if isinstance(v, str) and k != 'ext' else v)
        for k, v in template_fields(track, ext=ext).items()
    }
    rendered = template.format(**fields)
    segments = [sanitize_segment(seg) for seg in rendered.split('/') if seg.strip()]
    if not segments:
        segments = [sanitize_segment(f'{track.artist} - {track.title}.{ext}')]

    final = output_dir.joinpath(*segments)

    # The whole path has a budget, not just the parts. The filename gets
    # squeezed first, since folder names are shared by every track on the album
    # adn shortening them per-track would scatter one record across
    # directories. If the folders alone blow the budget then the squeeze moves
    # up instead of shipping a path Windows can't open.
    def _total() -> int:
        return len(str(output_dir.joinpath(*segments)))

    floors = [24] * (len(segments) - 1) + [len(ext) + 12]   # legible minimums
    for idx in range(len(segments) - 1, -1, -1):
        overshoot = _total() - _MAX_TOTAL_PATH
        if overshoot <= 0:
            break
        room = max(len(segments[idx]) - overshoot, floors[idx])
        segments[idx] = sanitize_segment(segments[idx], max_len=room)
    final = output_dir.joinpath(*segments)

    # Belt and braces: sanitized segments cannot contain '..', but a guarantee
    # this cheap is worth stating as code. Nothing escapes the library root.
    if not final.resolve().is_relative_to(output_dir.resolve()):
        raise PathUnwritable(
            f'rendered path escapes the library root: {final}',
            context={'template': template, 'rendered': rendered},
        )
    return final


def place_atomic(src: Path, final: Path) -> int:
    '''
    Moves `src` to `final` with no half-written state anyone can see, and returns
    the bytes placed. ENOSPC turns into NoSpace, which parks the run, since a full
    disk isn't one track's problem.
    '''
    part = final.parent / f'.{final.name}.spotdlplus.part'

    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        with open(src, 'rb') as fsrc, open(part, 'wb') as fdst:
            shutil.copyfileobj(fsrc, fdst, length=1024 * 1024)
            fdst.flush()
            os.fsync(fdst.fileno())
        os.replace(part, final)
    except OSError as exc:
        # The whole destination drive vanished (unplugged USB / external /
        # iPod)? Check FIRST. You can't even clean up a .part on a drive that's
        # gone, and this PARKS the run instead of failing the track.
        anchor = final.anchor
        if anchor and not os.path.exists(anchor):
            raise FsDriveLost(
                f'the drive holding {final} is no longer attached',
                context={'path': str(final)}, cause=exc,
            ) from exc
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        if exc.errno == 28:   # ENOSPC
            raise NoSpace(
                f'the volume holding {final.parent} is full',
                context={'path': str(final)}, cause=exc,
            ) from exc
        raise PathUnwritable(
            f'could not write {final.name} into the library',
            context={'path': str(final), 'errno': exc.errno}, cause=exc,
        ) from exc

    return final.stat().st_size
