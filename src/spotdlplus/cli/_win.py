'''
_win.py - making Windows consoles behave

The live display uses check marks, arrows, and musical notes. Default cmd.exe
still runs cp1252 and the first one of those kills the run with a
UnicodeEncodeError. For a tool aimed at people who just double-clicked an
installer that's a showstopper, so we switch the console to UTF-8 and let the
streams replace anything they can't print.
'''

from __future__ import annotations

import sys


def _isatty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001  # A captured/dummy stream just means 'no'
        return False


def harden_console() -> None:
    if sys.platform != 'win32':
        return
    # Never touch the test runner's streams. That's the harness's to own.
    if 'pytest' in sys.modules:
        return
    # Only when we're actually the shipped exe or sitting on a real console.
    if not (getattr(sys, 'frozen', False) or _isatty(sys.stdout)):
        return

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
        kernel32.SetConsoleOutputCP(65001)          # UTF-8 out
        kernel32.SetConsoleCP(65001)                # UTF-8 in
    except Exception:  # noqa: BLE001  # No console handle? nothing to set.
        pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001  # A non-reconfigurable stream is fine as-is
            pass
