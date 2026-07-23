'''
transcode.py - ffmpeg integration and format verification

If the source codec is the archive target, we stream-copy instead of
re-encoding. YouTube's best quality is normally opus type, and if opus is the
target for the download then the common path is a container remux with no
generational loss.
'''

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import ToolHung, ToolMissing, ToolQuarantined, TranscodeFailed
from ..core.runtime import resolve_tool

#: Our target formats, the codec each one carries, and the encoder that makes
#: it. 'alac' is Apple Lossless. It rides in an .m4a container (see _EXT),
#: which is what an iPod plays, so teh codec name and the extension
#: deliberately differ.
_FORMAT_CODEC: dict[str, str | None] = {
    'opus': 'opus', 'mp3': 'mp3', 'flac': 'flac', 'm4a': 'aac', 'wav': None,
    'alac': 'alac',
}
_ENCODER: dict[str, list[str]] = {
    'opus': ['-c:a', 'libopus'],
    'mp3': ['-c:a', 'libmp3lame'],
    'flac': ['-c:a', 'flac'],
    'm4a': ['-c:a', 'aac'],
    'wav': ['-c:a', 'pcm_s16le'],
    'alac': ['-c:a', 'alac'],
}
#: Lossless targets ignore the bitrate knob.
_LOSSLESS = {'flac', 'wav', 'alac'}

#: A format's on-disk extension. Only ALAC differs from its own name: it lives
#: in an .m4a container (same as AAC), becuase that's the file an iPod reads.
_EXT: dict[str, str] = {'alac': 'm4a'}


def ext_for(fmt: str) -> str:
    '''The file extension `fmt` lands on disk with. Usually the format's own name.'''
    return _EXT.get(fmt, fmt)


@dataclass(frozen=True, slots=True)
class ProbeInfo:
    codec: str
    duration_ms: int
    bitrate: int | None


def _run(argv: list[str], *, timeout_s: float, what: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except FileNotFoundError as exc:
        tool = Path(argv[0])
        if tool.is_absolute():
            # We resolved this to a specific bundled binary next to the exe,
            # and now it's not there. That's almost always antivirus
            # quarantining it mid-run, not a PATH problem.
            raise ToolQuarantined(
                f'{tool.name} was here at startup and vanished mid-run',
                context={'tool': str(tool)}, cause=exc,
            ) from exc
        raise ToolMissing(
            f'{argv[0]} is not on PATH. It is required for {what}. '
            'Install ffmpeg (which ships ffprobe) and re-run "spotdlp doctor".',
            context={'tool': argv[0]}, cause=exc,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        # A hang is TRANSIENT. The machine was busy, not the file broken. This
        # was Retry.NEVER once, and a single slow moment during a big run
        # permanently failed a perfectly good track.
        raise ToolHung(
            f'{Path(argv[0]).name} hung for {timeout_s:.0f}s and was killed',
            context={'cmd': argv}, cause=exc,
        ) from exc


def probe(path: Path, *, timeout_s: float = 30.0) -> ProbeInfo:
    '''What is actually inside this file. Trust the container, verify the stream.'''
    argv = [
        resolve_tool('ffprobe'), '-v', 'error',
        '-select_streams', 'a:0',
        '-show_entries', 'stream=codec_name,bit_rate:format=duration,bit_rate',
        '-of', 'json', str(path),
    ]
    proc = _run(argv, timeout_s=timeout_s, what='probing audio files')
    if proc.returncode != 0:
        raise TranscodeFailed(
            f'ffprobe rejected {path.name}',
            context={'cmd': argv, 'stderr': proc.stderr[-500:]},
        )

    data = json.loads(proc.stdout or '{}')
    streams = data.get('streams') or [{}]
    fmt = data.get('format') or {}
    duration_s = float(fmt.get('duration') or 0.0)
    bitrate = streams[0].get('bit_rate') or fmt.get('bit_rate')
    return ProbeInfo(
        codec=streams[0].get('codec_name') or 'unknown',
        duration_ms=int(duration_s * 1000),
        bitrate=int(bitrate) if bitrate else None,
    )


def transcode(
    src: Path,
    dst: Path,
    *,
    fmt: str,
    bitrate: str,
    src_codec: str | None = None,
    timeout_s: float = 300.0,
) -> bool:
    '''
        Produce `dst` in `fmt` from `src`. Returns True if it was a lossless
        stream-copy, False if a re-encode happened. The tag stage records which.

    '''
    if fmt not in _FORMAT_CODEC:
        raise TranscodeFailed(f'unknown target format {fmt!r}', context={'fmt': fmt})

    codec = src_codec or probe(src).codec
    copy = _FORMAT_CODEC[fmt] == codec

    argv = [resolve_tool('ffmpeg'), '-y', '-hide_banner', '-loglevel', 'error', '-i', str(src), '-vn']
    if copy:
        argv += ['-c:a', 'copy']
    else:
        argv += _ENCODER[fmt]
        if fmt not in _LOSSLESS:
            argv += ['-b:a', bitrate]
    argv += [str(dst)]

    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(argv, timeout_s=timeout_s, what='transcoding audio')
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise TranscodeFailed(
            f'ffmpeg failed converting {src.name} to {fmt}',
            context={'cmd': argv, 'stderr': proc.stderr[-500:]},
        )
    return copy


def transcode_dir(cache_dir: Path) -> Path:
    d = cache_dir / 'tx'
    d.mkdir(parents=True, exist_ok=True)
    return d
