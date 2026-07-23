'''
runtime.py - finding the bundled binaries

From source this does nothing interesting and helpers come off PATH. Frozen,
the installer drops ffmpeg and friends next to the .exe and we find them there
by absolute path, because a stray ffmpeg.exe in a downloads folder isn't ours.
'''

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    '''True when running from a PyInstaller build rather than source.'''
    return bool(getattr(sys, 'frozen', False))


def app_dir() -> Path | None:
    '''
    The folder the shipped .exe lives in, or None when running from source. That
    None keeps dev and tests on the normal PATH behaviour.
    '''
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return None


def _candidates(name: str) -> list[Path]:
    root = app_dir()
    if root is None:
        return []
    exe = f'{name}.exe' if sys.platform == 'win32' else name
    # next to the exe, and in a bin/ subfolder. Either layout the builder
    # picks.
    return [root / exe, root / 'bin' / exe]


def bundled_tool(name: str) -> Path | None:
    '''The path to a helper binary we shipped alongside teh app, or None.'''
    for c in _candidates(name):
        if c.is_file():
            return c
    return None


def resolve_tool(name: str) -> str:
    '''
    Returns the bundled copy of a binary if we shipped one, otherwise the bare name
    so PATH lookup still happens. Either way it goes straight to subprocess.
    '''
    found = bundled_tool(name)
    return str(found) if found else name
