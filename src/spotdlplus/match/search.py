'''
search.py - the yt-dlp wrapper

yt-dlp is great and it rots fast. It chases YouTube's bot detection week to
week, option names drift, and it throws a zoo of exceptions. So it's walled off
in here. This is the only file that imports it, it turns every failure into one
of our typed errors, and it hands back plain Candidate objects.

Searches are flat. One request gets 8 candidates with duration, channel, and
view count attached, which is enough to score all of them without a round trip
per video.
'''

from __future__ import annotations

from typing import Any

from ..core.errors import (
    CookiesUnreadable,
    DownloadFailed,
    SourceBlocked,
    ToolMissing,
    ToolOutdated,
)
from ..core.models import Candidate
from ..net.ytenv import is_cookie_extraction_error

#: yt-dlp before this is too far behind YouTube's changes to trust. The floor is
#: deliberately loose. `doctor` nudges toward the latest.
MIN_YTDLP = (2024, 1, 1)


def _parse_version(v: str) -> tuple[int, int, int]:
    parts = (v.split('.') + ['0', '0', '0'])[:3]
    try:
        return tuple(int(p) for p in parts)  # type: ignore[return-value]
    except ValueError:
        return (0, 0, 0)


class YtDlpSearcher:
    '''
    Searches YouTube for candidates. Built once and shared across workers so
    yt-dlp's YoutubeDL gets reused instead of rebuilt for every query.
    '''

    def __init__(self, *, results: int = 8, timeout_s: float = 20.0) -> None:
        from ..net.ytenv import ydl_base_opts
        self._results = results
        self._YDL, self._errors = _import_ytdlp()
        self._opts = {
            **ydl_base_opts(),               # IPv4, trust store, deno, see ytenv.py
            'skip_download': True,
            'extract_flat': 'in_playlist',   # metadata only, no per-video fetch
            'default_search': 'ytsearch',
            'socket_timeout': timeout_s,
            'extractor_retries': 1,
        }

    @classmethod
    def probe(cls) -> dict[str, Any]:
        '''
        Checks yt-dlp is installed and new enough. `doctor` calls this before a run so
        a missing or ancient yt-dlp fails in two seconds instead of at track 3.
        '''
        YDL, _ = _import_ytdlp()
        import yt_dlp
        version = yt_dlp.version.__version__
        if _parse_version(version) < MIN_YTDLP:
            raise ToolOutdated(
                f'yt-dlp {version} predates {".".join(map(str, MIN_YTDLP))}',
                context={'tool': 'yt-dlp', 'version': version},
            )
        return {'tool': 'yt-dlp', 'version': version}

    def search(self, query: str) -> list[Candidate]:
        '''
        Runs a flat search and returns candidates ready to score, in whatever order
        YouTube gave them.
        '''
        spec = f'ytsearch{self._results}:{query}'
        try:
            with self._YDL(self._opts) as ydl:
                info = ydl.extract_info(spec, download=False)
        except self._errors as exc:
            # yt-dlp funnels almost everything through DownloadError, and an
            # ExtractorError could be a mere "video unavailable" as easily as a
            # block. Read the text rather than assume.
            raise self._classify(query, exc) from exc

        entries = (info or {}).get('entries') or []
        return [c for c in (_to_candidate(e) for e in entries) if c is not None]

    def _classify(self, query: str, exc: Exception) -> Exception:
        '''
        yt-dlp funnels everything into DownloadError, so we read the text to sort out
        what actually happened.
        '''
        text = str(exc).lower()
        if is_cookie_extraction_error(text):
            return CookiesUnreadable(
                'could not read the browser cookies you configured',
                context={'query': query}, cause=exc,
            )
        if 'sign in' in text or 'bot' in text or 'confirm you' in text or '403' in text:
            return SourceBlocked(
                'youtube wants us to prove we aren\'t a bot. Slow down, or the IP is flagged',
                context={'query': query}, cause=exc,
            )
        return DownloadFailed(f'search failed for {query!r}',
                              context={'query': query}, cause=exc)


def _to_candidate(entry: dict[str, Any]) -> Candidate | None:
    '''Turns one flat search entry into our Candidate and drops anything unplayable.'''
    vid = entry.get('id')
    if not vid:
        return None
    if entry.get('availability') in ('private', 'needs_auth', 'premium_only'):
        return None

    duration = entry.get('duration')
    channel = entry.get('channel') or entry.get('uploader') or ''
    live = entry.get('live_status')

    return Candidate(
        source='youtube',
        source_id=vid,
        url=entry.get('url') or f'https://www.youtube.com/watch?v={vid}',
        title=entry.get('title') or '',
        uploader=channel,
        duration_ms=int(duration * 1000) if duration else 0,
        view_count=entry.get('view_count'),
        is_topic_channel=channel.lower().strip().endswith('- topic'),
        raw={
            'live_status': live,
            'channel_is_verified': entry.get('channel_is_verified'),
            'availability': entry.get('availability'),
        },
    )


def _import_ytdlp() -> tuple[Any, tuple[type[Exception], ...]]:
    '''
    Imports yt-dlp or raises our own ToolMissing. An ImportError escaping as itself
    would dodge the typed-error contract the Engine depends on.
    '''
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError, ExtractorError
    except ImportError as exc:
        raise ToolMissing(
            'yt-dlp isn\'t installed. It\'s the audio-acquisition engine. Nothing '
            'comes down without it. "pip install yt-dlp".',
            context={'tool': 'yt-dlp'}, cause=exc,
        ) from exc
    return YoutubeDL, (DownloadError, ExtractorError)
