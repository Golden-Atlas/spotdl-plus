'''
ytenv.py - the environment yt-dlp needs to work

Every layer here broke on a real machine before it earned its spot.

  force IPv4    The big one. googlevideo resolves to IPv6, and on a network
                whose v6 route blackholes data every download stalls out to a
                read timeout while the API works fine. Metadata fine, media
                dead.
  truststore    Antivirus re-signs all TLS and yt-dlp builds its own SSL
                context, so a process-wide inject is the only way to reach it.
  deno          YouTube ships a JS challenge. Without a runtime the media URLs
                come back throttled or dead.
  PO tokens     Minted locally through deno by the bgutil provider.

`doctor` checks each layer on its own, because when YouTube breaks the first
question is alwasy which layer.
'''

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .http import enable_os_trust_store

#: Where self-provisioned binaries live. Never on the system PATH permanently.
def tools_dir() -> Path:
    if sys.platform == 'win32':
        root = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
        return Path(root) / 'spotdlplus' / 'tools'
    return Path(os.path.expanduser('~/.local/share')) / 'spotdlplus' / 'tools'


def find_deno() -> Path | None:
    '''Bundled-with-the-exe first, then our tools dir, then the machine's PATH.'''
    from ..core.runtime import bundled_tool
    shipped = bundled_tool('deno')
    if shipped is not None:
        return shipped
    ours = tools_dir() / ('deno.exe' if sys.platform == 'win32' else 'deno')
    if ours.is_file():
        return ours
    import shutil
    found = shutil.which('deno')
    return Path(found) if found else None


def bgutil_script() -> Path | None:
    '''The PO-token minting script, at teh plugin's default location.'''
    script = Path.home() / 'bgutil-ytdlp-pot-provider' / 'server' / 'src' / 'generate_once.ts'
    return script if script.is_file() else None


_PREPARED = False


def prepare_environment() -> None:
    '''Process-level setup. Idempotent. Every yt-dlp entry point calls it.'''
    global _PREPARED
    if _PREPARED:
        return
    enable_os_trust_store()
    # deno must trust the antivirus's root too, or token minting dies on TLS
    os.environ.setdefault('DENO_TLS_CA_STORE', 'system,mozilla')
    _PREPARED = True


#: Where to borrow YouTube cookies from, set once at startup (like the noise
#: filter) so ydl_base_opts can read it without threading config through every
#: layer. Cookies clear age-gates and most bot walls, the big-run lifesaver.
_COOKIE_BROWSER: str | None = None
_COOKIE_FILE: str | None = None

#: Download speed cap in bytes/s, set once at startup like the cookie source.
#: None means full speed (the default. Most people want the music NOW).
_RATE_LIMIT_BPS: int | None = None


def set_rate_limit(bps: int | None) -> None:
    '''Cap every download's speed. Call once, before any fetch.'''
    global _RATE_LIMIT_BPS
    _RATE_LIMIT_BPS = bps if bps and bps > 0 else None


def set_cookie_source(*, browser: str | None = None, cookiefile: str | None = None) -> None:
    '''
    Sets where downloads pull YouTube cookies from. Call it once before any fetch.
    `browser` is a name yt-dlp knows and `cookiefile` is a path to a cookies.txt.
    '''
    global _COOKIE_BROWSER, _COOKIE_FILE
    _COOKIE_BROWSER = browser.lower() if browser else None
    _COOKIE_FILE = cookiefile or None


class _SilentLogger:
    '''
    Swallows everything yt-dlp says. Used for the cookie preflight, where we report
    the failure ourselves instead of letting it bleed onto the display.
    '''

    def debug(self, msg: str) -> None: ...
    def info(self, msg: str) -> None: ...
    def warning(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...


def cookie_source_readable() -> tuple[bool, str]:
    '''
    Checks whether the configured cookies can actually be read right now and
    returns (ok, one-line detail). Running it up front means a locked cookie store
    costs one warning and a cookie-free run instead of failing every single track.
    '''
    if not (_COOKIE_BROWSER or _COOKIE_FILE):
        return True, ''
    try:
        from yt_dlp import YoutubeDL
        # A silent logger: yt-dlp prints 'ERROR: Could not copy Chrome cookie
        # database' straight to stderr even under quiet, adn we render our own
        # clean one-line warning from the return value instead.
        opts = {**ydl_base_opts(), 'logger': _SilentLogger()}
        with YoutubeDL(opts) as ydl:
            _ = ydl.cookiejar   # forces the browser/file cookie load. raises if it can't
        return True, ''
    except Exception as exc:  # noqa: BLE001  # Any failure means 'don't rely on them'
        import re
        detail = str(exc).strip().splitlines()
        line = detail[-1] if detail else type(exc).__name__
        # On a real terminal yt-dlp COLORS its 'ERROR:' prefix, and the ANSI
        # escape codes in front of it defeated a plain startswith for two whole
        # releases. Every piped test looked clean while every human saw 'ERROR:
        # ERROR:'. Colors first, prefixes second.
        line = re.sub(r'\x1b\[[0-9;]*m', '', line)
        while line.upper().startswith('ERROR:'):   # yt-dlp likes to double it up
            line = line[len('ERROR:'):].strip()
        return False, line.split('. See ')[0].strip()   # drop the 'See <url>' tail


def is_cookie_extraction_error(text: str) -> bool:
    '''
    Tells whether a yt-dlp error means it couldn't READ the cookies, which usually
    means the browser is open and holding the file. That's different from a
    please-enable-cookies bot wall, which is a block.
    '''
    t = text.lower()
    return 'cookie' in t and any(k in t for k in (
        'could not copy', 'permission denied', 'could not find', 'decrypt',
        'keyring', 'database is locked', 'unable to open', 'extract cookies',
        'no such file', 'is not installed', 'failed to read'))


def ydl_base_opts(*, force_ipv4: bool = True) -> dict[str, Any]:
    '''
    The options every YoutubeDL we build gets. Callers add their own format and
    hooks on top but don't override these without a reason.
    '''
    prepare_environment()
    opts: dict[str, Any] = {
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'noplaylist': True,
        'socket_timeout': 30,
        'retries': 1,                 # the Engine owns real retries
    }
    if force_ipv4:
        # the load-bearing line. see module docstring.
        opts['source_address'] = '0.0.0.0'
    deno = find_deno()
    if deno is not None:
        opts['js_runtimes'] = {'deno': {'path': str(deno)}}
    if _COOKIE_BROWSER:
        # yt-dlp wants a tuple: (browser, profile, keyring, container).
        opts['cookiesfrombrowser'] = (_COOKIE_BROWSER, None, None, None)
    if _COOKIE_FILE:
        opts['cookiefile'] = _COOKIE_FILE
    if _RATE_LIMIT_BPS:
        opts['ratelimit'] = _RATE_LIMIT_BPS
    return opts


#: Subprocess chatter that must never reach a human's display. The PO-token
#: deno script prints warnings and stack traces to stderr, whcih child
#: processes inherit. And one of them once landed mid-animation, verbatim: "Not
#: implemented: HTMLCanvasElement's getContext() method".
_NOISE = None

_FILTER_INSTALLED = False


def install_noise_filter(log_path: Path) -> bool:
    '''
    Reroutes this process's stderr file descriptor through a filter thread. Known
    subprocess noise goes to a log and everything else, our own tracebacks
    included, passes straight through.

    It works at the fd level on purpose. redirect_stderr only rebinds Python's
    sys.stderr and does nothing for child processes, whcih write to fd 2 directly.
    '''
    global _FILTER_INSTALLED, _NOISE
    if _FILTER_INSTALLED:
        return True
    import re
    import threading

    if _NOISE is None:
        # Case-insensitive so 'bgutil' catches 'BgUtil'. The PoTokenProvider
        # 'already registered' assertion is yt-dlp re-loading teh bgutil plugin
        # per YoutubeDL instance. Benign, but it prints a stack trace we don't
        # want a human to read.
        _NOISE = re.compile(
            rb'BotGuard|HTMLCanvasElement|generate_once\.ts|session_manager\.ts'
            rb'|bgutil|Failed while generating POT|at async |at file:///'
            rb'|PoTokenProvider|already registered',
            re.IGNORECASE)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    real_stderr = os.dup(2)
    read_end, write_end = os.pipe()
    os.dup2(write_end, 2)
    os.close(write_end)

    # sys.stderr still points at fd 2, whcih is a pipe now, and flushing a
    # pipe-backed text stream on Windows can throw OSError WinError 1. yt-dlp
    # flushes stderr when it prints warnings, adn that crash escaped a stage as
    # ERR_UNEXPECTED on a real run.
    import sys

    class _FlushTolerant:
        def __init__(self, raw):
            self._raw = raw

        def write(self, s):
            try:
                return self._raw.write(s)
            except OSError:
                return len(s)

        def flush(self):
            try:
                self._raw.flush()
            except OSError:
                pass

        def __getattr__(self, name):
            return getattr(self._raw, name)

    sys.stderr = _FlushTolerant(sys.stderr)

    def pump() -> None:
        log = open(log_path, 'ab')
        with os.fdopen(read_end, 'rb') as src:
            for line in src:
                if _NOISE.search(line):
                    log.write(line)
                    log.flush()
                else:
                    os.write(real_stderr, line)

    threading.Thread(target=pump, daemon=True, name='stderr-noise-filter').start()
    _FILTER_INSTALLED = True
    return True


def probe_environment() -> dict[str, Any]:
    '''What `doctor` reports: each layer, present or not, with the path.'''
    deno = find_deno()
    script = bgutil_script()
    try:
        import yt_dlp
        ytdlp_version = yt_dlp.version.__version__
    except ImportError:
        ytdlp_version = None
    return {
        'yt_dlp': ytdlp_version,
        'deno': str(deno) if deno else None,
        'bgutil_script': str(script) if script else None,
        'force_ipv4': True,
        'trust_store': enable_os_trust_store(),
    }
