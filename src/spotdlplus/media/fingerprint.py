'''
fingerprint.py - acoustic fingerprinting

fpcalc decodes every sample of a file to build its fingerprint, which makes
this two checks in one. The decode catches mid-stream corruption that a header
probe walks right past, and the fingerprint answers whether this is even the
right song.
'''

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ..core.errors import ToolMissing, VerifyFailed
from ..core.models import Fingerprint
from ..net.ytenv import tools_dir

__all__ = ['Fingerprint', 'find_fpcalc', 'fingerprint_file']


def find_fpcalc() -> Path | None:
    '''Bundled-with-the-exe first, then our tools dir, then the machine's PATH.'''
    from ..core.runtime import bundled_tool
    shipped = bundled_tool('fpcalc')
    if shipped is not None:
        return shipped
    ours = tools_dir() / ('fpcalc.exe' if sys.platform == 'win32' else 'fpcalc')
    if ours.is_file():
        return ours
    import shutil
    found = shutil.which('fpcalc')
    return Path(found) if found else None


def fingerprint_file(path: Path, *, timeout_s: float = 60.0) -> Fingerprint:
    '''
    Fingerprints one file. Raises VerifyFailed when the audio won't decode, which
    is a finding rather than an error. It means the file is damaged deeper than a
    header probe can see.
    '''
    fpcalc = find_fpcalc()
    if fpcalc is None:
        raise ToolMissing(
            'fpcalc (chromaprint) is not available. It powers acoustic '
            'verification. "spotdlp doctor" explains how it is provisioned.',
            context={'tool': 'fpcalc'},
        )

    try:
        proc = subprocess.run(
            [str(fpcalc), '-json', str(path)],
            capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerifyFailed(
            f'fpcalc hung on {path.name} for {timeout_s:.0f}s',
            context={'path': str(path)}, cause=exc,
        ) from exc

    if proc.returncode != 0:
        raise VerifyFailed(
            f'{path.name} would not fully decode. Damaged more deeply than a '
            f'header probe can see',
            context={'path': str(path), 'stderr': proc.stderr[-300:]},
        )

    data = json.loads(proc.stdout or '{}')
    fp = data.get('fingerprint')
    if not fp:
        raise VerifyFailed(
            f'fpcalc produced no fingerprint for {path.name}',
            context={'path': str(path)},
        )
    return Fingerprint(fingerprint=fp, duration_s=int(data.get('duration') or 0))
