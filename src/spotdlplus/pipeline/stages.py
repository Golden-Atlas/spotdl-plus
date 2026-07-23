'''
stages.py - the seven pipeline stages

Each class is boring on purpose. It says what it consumes, what it produces,
and has a run(). No retries, no sleeping, no state machine calls, no printing.
The tests enforce that.

    DISCOVERED -> ENRICHED    estimate bytes
    ENRICHED   -> MATCHED     find the source, save the scoreboard
    MATCHED    -> FETCHED     download best audio into the cache
    FETCHED    -> TRANSCODED  remux or re-encode
    TRANSCODED -> TAGGED      embed everything, plus art and lyrics
    TAGGED     -> PLACED      atomic move into the library
    PLACED     -> DONE        hash it, probe it, record it as owned

Verify is its own stage on purpose. 'The file is in the library' and 'the file
is correct' are two different claims and only the second one earns a library
row.
'''

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, ClassVar

from ..core.engine import Context, SkipTrack
from ..core.errors import DownloadFailed, SpotdlPlusError, VerifyFailed, WouldOverwrite
from ..core.events import MatchDecided, Stage
from ..core.models import Track, TrackState
from ..core.store import TrackRow
from ..match.matcher import Searcher, match_track
from ..media.covers import cover_cache_path, fetch_cover, write_folder_art
from ..media.fetch import fetch_audio, find_fetched
from ..media.place import place_atomic, render_path
from ..media.tag import write_tags
from ..media.transcode import ext_for, probe, transcode, transcode_dir
from ..net.http import HttpClient
from ..net.ratelimit import TokenBucket

#: Container overhead on top of duration x bitrate. Measured, not guessed:
#: ogg framing plus embedded cover art lands within a few percent of this.
_SIZE_FUDGE = 1.04

#: The placed file's probed duration may differ from Spotify's number by the
#: same margin the matcher tolerated, plus stream trim. Beyond this, the file
#: is not the song we think it is.
_VERIFY_DURATION_TOLERANCE_MS = 20_000


def _metadata(ctx: Context, row: TrackRow) -> Track:
    track = ctx.store.get_track_metadata(row.id)
    if track is None:
        raise VerifyFailed(f'row {row.id} has no metadata blob', context={'row': row.id})
    return track


def _bitrate_bps(bitrate: str) -> int:
    digits = ''.join(c for c in bitrate if c.isdigit())
    return int(digits or 192) * 1000


class EnrichStage:
    '''Cheap bookkeeping before the network gets involved.'''

    name: ClassVar[Stage] = Stage.ENRICH
    consumes: ClassVar[TrackState] = TrackState.DISCOVERED
    produces: ClassVar[TrackState] = TrackState.ENRICHED

    def run(self, ctx: Context, row: TrackRow) -> dict[str, Any]:
        est = int(row.duration_ms / 1000 * _bitrate_bps(ctx.config.bitrate) / 8 * _SIZE_FUDGE)
        return {'est_bytes': est}


class MatchStage:
    '''The scoreboard, persisted win or lose.'''

    name: ClassVar[Stage] = Stage.MATCH
    consumes: ClassVar[TrackState] = TrackState.ENRICHED
    produces: ClassVar[TrackState] = TrackState.MATCHED

    def __init__(self, searcher: Searcher, *, bucket: TokenBucket | None = None) -> None:
        self._searcher = searcher
        #: Shared across workers: yt-dlp doesn't route through HttpClient, so
        #: the polite pacing has to live here or nowhere.
        self._bucket = bucket or TokenBucket(2.0, 2, name='youtube-search')

    def run(self, ctx: Context, row: TrackRow) -> dict[str, Any]:
        track = _metadata(ctx, row)
        self._bucket.acquire()   # may raise RateLimited -> Engine defers, no thread held

        try:
            result = match_track(track, self._searcher)   # raises typed refusals
        except SpotdlPlusError as exc:
            # A refusal keeps its receipts: teh scoreboard rides on the error,
            # gets persisted here, and `relink` shows a human everything we
            # saw.
            board = getattr(exc, 'scoreboard', None)
            if board:
                ctx.store.record_scoreboard(row.id, board)
            raise
        ctx.store.record_match(row.id, result)
        chosen = result.chosen
        ctx.bus.emit(MatchDecided(
            run_id=ctx.run_id, track_id=row.id, chosen_url=chosen.url,
            score=result.score, basis=result.basis, breakdown=dict(result.breakdown),
            runner_up_score=result.runner_up_score,
            considered=1 + len(result.rejected),
        ))
        return {
            'chosen_url': chosen.url,
            'match_score': result.score,
            'match_basis': result.basis,
        }


class FetchStage:
    name: ClassVar[Stage] = Stage.FETCH
    consumes: ClassVar[TrackState] = TrackState.MATCHED
    produces: ClassVar[TrackState] = TrackState.FETCHED

    def run(self, ctx: Context, row: TrackRow) -> dict[str, Any]:
        # Rechecked here, not just at ingest: a stale queue matched before the
        # library knew those tracks once re-downloaded ~230 of them. A relinked
        # tarck still re-fetches, because force_relink revokes ownership first.
        owned = ctx.store.own(row.identity)
        if owned is not None:
            if Path(owned['final_path']).is_file():
                raise SkipTrack('owned')
            ctx.store.revoke_ownership(row.identity)   # heal: the file is gone

        if not row.chosen_url:
            raise DownloadFailed(f'{row.id} reached FETCH with no chosen url',
                                 context={'row': row.id})
        got = fetch_audio(
            row.chosen_url,
            cache_dir=ctx.config.cache_dir, track_id=row.id,
            bus=ctx.bus, run_id=ctx.run_id,
        )

        # Checks that a download didn't get cut short with a false success
        # flag. If we wait to do it later we're more likely to get false
        # positive results.
        try:
            info = probe(got)
        except SpotdlPlusError as exc:
            got.unlink(missing_ok=True)
            raise DownloadFailed(
                f'{row.id}: downloaded stream is corrupt ({exc.code}), refetching',
                context={'row': row.id, 'url': row.chosen_url}, cause=exc,
            ) from exc
        if row.duration_ms and abs(info.duration_ms - row.duration_ms) > _VERIFY_DURATION_TOLERANCE_MS:
            got.unlink(missing_ok=True)
            raise DownloadFailed(
                f'{row.id}: stream runs {info.duration_ms // 1000}s, expected '
                f'~{row.duration_ms // 1000}s, truncated, refetching',
                context={'row': row.id, 'url': row.chosen_url,
                         'probed_ms': info.duration_ms},
            )
        return {}


class TranscodeStage:
    name: ClassVar[Stage] = Stage.TRANSCODE
    consumes: ClassVar[TrackState] = TrackState.FETCHED
    produces: ClassVar[TrackState] = TrackState.TRANSCODED

    def run(self, ctx: Context, row: TrackRow) -> dict[str, Any]:
        src = find_fetched(ctx.config.cache_dir, row.id)
        if src is None:
            # cache evaporated between runs. Rewind is the honest answer
            raise DownloadFailed(f'fetched file for {row.id} is gone from the cache',
                                 context={'row': row.id})
        fmt = ctx.config.audio_format
        # `fmt` drives the codec (may be 'alac'). The file on disk takes the
        # container's extension ('m4a' for alac), so they can differ.
        dst = transcode_dir(ctx.config.cache_dir) / f'{row.id}.{ext_for(fmt)}'
        transcode(src, dst, fmt=fmt, bitrate=ctx.config.bitrate)
        return {}


class TagStage:
    '''Cover art is fetched once per album and cached. 13 tracks, one download.'''

    name: ClassVar[Stage] = Stage.TAG
    consumes: ClassVar[TrackState] = TrackState.TRANSCODED
    produces: ClassVar[TrackState] = TrackState.TAGGED

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def run(self, ctx: Context, row: TrackRow) -> dict[str, Any]:
        from ..media.lyrics import fetch_lyrics, lyrics_cache_path

        track = _metadata(ctx, row)
        ext = ext_for(ctx.config.audio_format)
        path = transcode_dir(ctx.config.cache_dir) / f'{row.id}.{ext}'
        cover = fetch_cover(track.album, self._http, ctx.config.cache_dir,
                            bus=ctx.bus, run_id=ctx.run_id)

        # Lyrics ride along like cover art: fetched free, embedded, and never
        # allowed to fail the track. 'off' skips the lookup entirely.
        lyric_text: str | None = None
        if ctx.config.lyrics != 'off':
            got = fetch_lyrics(track, self._http)
            if got is not None:
                lyric_text = got.pick(ctx.config.lyrics)
            if got is not None and got.synced:
                # park the synced copy for PlaceStage's sidecar, wanted or not
                # yet known. A cached 2KB text file is cheaper than a re-fetch
                lyrics_cache_path(ctx.config.cache_dir, row.id).write_text(
                    got.synced, encoding='utf-8')

        # tags dispatch on the CONTAINER (alac shares m4a's MP4 atoms), so pass
        # the extension, not the codec name.
        write_tags(path, track, fmt=ext, cover=cover, lyrics_text=lyric_text)
        return {}


class PlaceStage:
    name: ClassVar[Stage] = Stage.PLACE
    consumes: ClassVar[TrackState] = TrackState.TAGGED
    produces: ClassVar[TrackState] = TrackState.PLACED

    def run(self, ctx: Context, row: TrackRow) -> dict[str, Any]:
        track = _metadata(ctx, row)
        ext = ext_for(ctx.config.audio_format)
        src = transcode_dir(ctx.config.cache_dir) / f'{row.id}.{ext}'
        final = render_path(track, template=ctx.config.template,
                            output_dir=ctx.config.output_dir, ext=ext)

        # Fresh work always wins over OUR OWN file. An existing file is only
        # trusted when we have nothing to replace it with (crash recovery).
        # Skipping placement because the destination exists once wedged 18
        # corrupt files. Every retry fetched a good copy hten declined to use
        # it.
        #
        # But "ours" is the load-bearing word. Point output_dir at a folder
        # that already has music in it and a rendered path can land exactly on
        # a stranger's file, and place_atomic would replace it without a word.
        # We own it only if a library row says so. Anything else stops.
        if src.is_file():
            if final.exists() and ctx.store.owner_of_path(final) is None:
                raise WouldOverwrite(
                    f'{row.id}: {final.name} already exists and is not ours',
                    context={'row': row.id, 'path': str(final)},
                )
            place_atomic(src, final)
        elif not final.exists():
            raise DownloadFailed(
                f'{row.id}: nothing to place. Cache artifact and destination both missing',
                context={'row': row.id, 'expected_src': str(src)},
            )

        # Folder art rides along with the first track of each album. Embedded
        # art is correct and, in .opus, invisible to Windows Explorer and many
        # players. Cover.jpg is teh fallback everything understands.
        al = track.album
        if al is not None:
            cached = cover_cache_path(al, ctx.config.cache_dir)
            if cached.is_file():
                write_folder_art(final.parent, cached.read_bytes())

        # The .lrc sidecar, when asked for: the synced text TagStage cached
        # lands beside the placed file, named to match, so players just find it.
        if ctx.config.lyrics_sidecar:
            from ..media.lyrics import lyrics_cache_path, sidecar_path
            cached_lrc = lyrics_cache_path(ctx.config.cache_dir, row.id)
            if cached_lrc.is_file():
                sidecar_path(final).write_text(
                    cached_lrc.read_text(encoding='utf-8'), encoding='utf-8')

        return {'final_path': str(final)}


class VerifyStage:
    '''The only stage allowed to declare a track DONE, and it earns it.'''

    name: ClassVar[Stage] = Stage.PLACE   # same phase family for event purposes
    consumes: ClassVar[TrackState] = TrackState.PLACED
    produces: ClassVar[TrackState] = TrackState.DONE

    def run(self, ctx: Context, row: TrackRow) -> dict[str, Any]:
        track = _metadata(ctx, row)
        if not row.final_path:
            raise VerifyFailed(f'{row.id} reached VERIFY without a final path',
                               context={'row': row.id})
        final = Path(row.final_path)
        if not final.is_file() or final.stat().st_size == 0:
            raise VerifyFailed(f'{final.name} is missing or empty',
                               context={'path': str(final)})

        info = probe(final)
        delta = abs(info.duration_ms - track.duration_ms)
        if track.duration_ms and delta > _VERIFY_DURATION_TOLERANCE_MS:
            raise VerifyFailed(
                f'{final.name} runs {info.duration_ms // 1000}s but the source '
                f'metadata says {track.duration_ms // 1000}s. Wrong or truncated file',
                context={'path': str(final), 'probed_ms': info.duration_ms,
                         'expected_ms': track.duration_ms},
            )

        sha = hashlib.sha256()
        with open(final, 'rb') as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b''):
                sha.update(block)

        # Store the CONTAINER (m4a), not the codec (alac). Audit reads this to
        # pick a tag dialect, and ALAC files are MP4 liek any other .m4a.
        ext = ext_for(ctx.config.audio_format)
        ctx.store.remember(row.identity, str(final), ext,
                           final.stat().st_size, sha.hexdigest())

        # the cache served its purpose. a verified track owes it nothing
        fetched = find_fetched(ctx.config.cache_dir, row.id)
        if fetched is not None:
            fetched.unlink(missing_ok=True)
        (transcode_dir(ctx.config.cache_dir) / f'{row.id}.{ext}').unlink(
            missing_ok=True)
        (ctx.config.cache_dir / 'lyrics' / f'{row.id}.lrc').unlink(missing_ok=True)
        return {}


def download_stages(searcher: Searcher, http: HttpClient) -> list:
    '''The standard walk, in order. `run.py` drives these after the plan.'''
    return [
        FetchStage(),
        TranscodeStage(),
        TagStage(http),
        PlaceStage(),
        VerifyStage(),
    ]


def resolve_stages(searcher: Searcher) -> list:
    '''The pre-plan half: cheap bookkeeping, then matching.'''
    return [EnrichStage(), MatchStage(searcher)]
