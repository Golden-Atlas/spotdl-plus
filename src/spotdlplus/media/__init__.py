'''
media. Where bytes become a library.

fetch (yt-dlp, quarantined) -> transcode (ffmpeg, copy-not-reencode when it
can) -> tag (everything we know, embedded) -> place (atomic, sanitized, inside
teh library root or nowhere). One-way street.

May import core and net. Raises typed errors. Never retries, never sleeps.
'''

from .fetch import fetch_audio, fetch_dir, find_fetched
from .place import clean_component, place_atomic, render_path, sanitize_segment, template_fields
from .tag import write_tags
from .transcode import ProbeInfo, probe, transcode, transcode_dir

__all__ = [
    'ProbeInfo', 'clean_component', 'fetch_audio', 'fetch_dir', 'find_fetched',
    'place_atomic', 'probe', 'render_path', 'sanitize_segment', 'template_fields',
    'transcode', 'transcode_dir', 'write_tags',
]
