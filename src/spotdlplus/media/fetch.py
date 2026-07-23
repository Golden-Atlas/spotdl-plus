'''
fetch.py - downloading audio into the cache

Grabs the matched candidate's best audio and does nothing else. No
postprocessing, no conversion, no tagging. Every extra job we hand yt-dlp is
another failure mode we don't control.

Files land at cache/fetch/{track_id}.{ext} so later stages find them by id and
no path ever has to go through the database.
'''

from __future__ import annotations

import threading
import time
from pathlib import Path

from ..core.errors import CookiesUnreadable, DownloadFailed, SourceBlocked
from ..core.events import EventBus, Stage, TrackProgress
from ..net.ytenv import is_cookie_extraction_error, ydl_base_opts

#: Progress events at most tihs often per track. A 5MB download fires the hook
#: hundreds of times. A renderer needs perhaps four of those.
_PROGRESS_INTERVAL_S = 1.0

#: One YoutubeDL per worker thread, reused across tracks. yt-dlp caches solved
#: JS challenges and PO tokens on the instance and a cold one spawns deno for
#: every download. Two workers cold-starting deno at once is how six tracks of
#: an album die while seven make it.
_tls = threading.local()


def fetch_dir(cache_dir: Path) -> Path:
    d = cache_dir / 'fetch'
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_fetched(cache_dir: Path, track_id: str) -> Path | None:
    '''The downloaded file for a track, whatever extension it arrived with.'''
    for p in fetch_dir(cache_dir).glob(f'{track_id}.*'):
        if p.suffix != '.part':
            return p
    return None


def _worker_ydl(dest: Path):
    '''
    Builds this thread's YoutubeDL once and keeps it warm. yt-dlp caches solved JS
    challenges and PO tokens on the instance, and a cold one spawns deno for every
    download. Two workers cold-starting deno at the same tiem is how six tracks of
    an album die while seven make it.
    '''
    from yt_dlp import YoutubeDL

    ydl = getattr(_tls, 'ydl', None)
    if ydl is None:
        def hook(d: dict) -> None:
            ctx = getattr(_tls, 'progress_ctx', None)
            if ctx is None or d.get('status') != 'downloading':
                return
            bus, run_id, track_id, last = ctx
            now = time.monotonic()
            if now - last[0] < _PROGRESS_INTERVAL_S:
                return
            last[0] = now
            bus.emit(TrackProgress(
                run_id=run_id, track_id=track_id, stage=Stage.FETCH,
                done_bytes=d.get('downloaded_bytes') or 0,
                total_bytes=d.get('total_bytes') or d.get('total_bytes_estimate'),
            ))

        ydl = YoutubeDL({
            **ydl_base_opts(),        # IPv4, trust store, deno, see net/ytenv.py
            'format': 'bestaudio/best',
            # named by the VIDEO id here. renamed to our track id after landing
            'outtmpl': str(dest / 'dl-%(id)s.%(ext)s'),
            'progress_hooks': [hook],
            'postprocessors': [],     # we do our own ffmpeg, thanks
        })
        _tls.ydl = ydl
    return ydl


def fetch_audio(
    url: str,
    *,
    cache_dir: Path,
    track_id: str,
    bus: EventBus,
    run_id: str,
    timeout_s: float = 300.0,
) -> Path:
    '''
    Downloads the best audio for `url` and returns the cached file.

    It clears out its own stale leftovers first, because letting yt-dlp resume from
    a .part we don't trust is worse than starting over.
    '''
    from yt_dlp.utils import DownloadError

    dest = fetch_dir(cache_dir)
    for stale in dest.glob(f'{track_id}.*'):
        stale.unlink(missing_ok=True)

    try:
        ydl = _worker_ydl(dest)
    except Exception as exc:  # noqa: BLE001  # Constructor failures are fetch failures
        raise DownloadFailed(
            f'could not initialise the downloader ({type(exc).__name__})',
            context={'url': url}, cause=exc,
        ) from exc
    _tls.progress_ctx = (bus, run_id, track_id, [0.0])
    try:
        info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        raise classify_download_error(exc, url) from exc
    except Exception as exc:  # noqa: BLE001  # Deno timeouts et al must stay typed
        raise DownloadFailed(
            f'fetch machinery failed for {url} ({type(exc).__name__})',
            context={'url': url}, cause=exc,
        ) from exc
    finally:
        _tls.progress_ctx = None

    landed = _downloaded_file(info, dest)
    if landed is None or not landed.is_file() or landed.stat().st_size == 0:
        raise DownloadFailed(
            f'yt-dlp reported success but produced no file for {url}',
            context={'url': url},
        )

    final = dest / f'{track_id}{landed.suffix}'
    final.unlink(missing_ok=True)
    landed.rename(final)
    return final


def classify_download_error(exc: Exception, url: str):
    '''
    Turns a raw yt-dlp DownloadError into the right typed error. Both the real
    fetch and doctor's canary use it so they name the same failure the same way.
    '''
    text = str(exc).lower()
    if is_cookie_extraction_error(text):
        return CookiesUnreadable(
            'could not read the browser cookies you configured',
            context={'url': url}, cause=exc,
        )
    if 'sign in' in text or 'bot' in text or '403' in text or 'confirm you' in text:
        return SourceBlocked(
            'youtube is challenging the download. Slow down, or the IP is flagged',
            context={'url': url}, cause=exc,
        )
    return DownloadFailed(f'download failed for {url}', context={'url': url}, cause=exc)


#: The canary. 'Me at the zoo'. The first video ever posted to YouTube, 19s,
#: uploaded by a co-founder. As permanent as anything on the platform gets, so
#: if we can't pull *this*, the problem is us or the network, not a dead link.
_CANARY_URL = 'https://www.youtube.com/watch?v=jNQXAC9IVRw'


def probe_youtube(*, timeout_s: float = 45.0) -> None:
    '''
    Runs one real download through the exact stack a run uses, so IPv4, trust
    store, deno, PO tokens, and whatever cookies are set. It raises the same
    classified error a normal fetch would, which is how doctor tells a walled IP
    apart from bad cookies.
    '''
    import tempfile

    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    with tempfile.TemporaryDirectory(prefix='spdl-probe-') as td:
        opts = {
            **ydl_base_opts(),
            'format': 'bestaudio/best',
            'outtmpl': str(Path(td) / 'canary.%(ext)s'),
            'postprocessors': [],
            'socket_timeout': min(timeout_s, 30.0),
        }
        try:
            with YoutubeDL(opts) as ydl:
                ydl.extract_info(_CANARY_URL, download=True)
        except DownloadError as exc:
            raise classify_download_error(exc, _CANARY_URL) from exc
        except Exception as exc:  # noqa: BLE001  # Keep every failure typed
            raise DownloadFailed(
                f'the network canary failed to run ({type(exc).__name__})',
                context={'url': _CANARY_URL}, cause=exc,
            ) from exc


def _downloaded_file(info: dict, dest: Path) -> Path | None:
    '''Where yt-dlp actually put it. Ask the info dict. Fall back to globbing.'''
    for req in (info or {}).get('requested_downloads') or []:
        fp = req.get('filepath')
        if fp:
            return Path(fp)
    vid = (info or {}).get('id')
    if vid:
        for p in dest.glob(f'dl-{vid}.*'):
            if p.suffix != '.part':
                return p
    return None
