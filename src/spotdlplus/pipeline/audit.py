'''
audit.py - checking and repairing the library

Since the file is supposed to survive the database, this checks the files
themselves. Present, sound, fully tagged, has art, accounted for, and with
--identity, acoustically confirmed.

Repair rewrites tags and art from the metadata blob we stored at discovery.
Audio bytes never get edited. A file that fails on identity moves whole into
quarantine instead of getting deleted, because it's evidence.
'''

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..core.errors import SpotdlPlusError
from ..core.events import EventBus, NullBus
from ..core.store import Store
from ..media.covers import fetch_cover, write_folder_art
from ..media.tag import write_tags
from ..media.transcode import probe
from ..net.http import HttpClient

#: Tags every placed file must carry. DATE and ISRC are included on purpose:
#: an archive whose files can't state when they were released or which
#: recording they are has lost the plot.
REQUIRED_TAGS = ('title', 'artist', 'album', 'albumartist', 'tracknumber', 'date', 'isrc')

#: Same tolerance the verify stage uses. An audit must not be stricter than
#: the gate that admitted the file.
DURATION_TOLERANCE_MS = 20_000

#: Vorbis (opus/flac) key names are our canonical ones. Other formats map here.
_ID3_MAP = {
    'title': 'TIT2', 'artist': 'TPE1', 'album': 'TALB', 'albumartist': 'TPE2',
    'tracknumber': 'TRCK', 'date': 'TDRC', 'isrc': 'TSRC',
}
_MP4_MAP = {
    'title': '\xa9nam', 'artist': '\xa9ART', 'album': '\xa9alb', 'albumartist': 'aART',
    'tracknumber': 'trkn', 'date': '\xa9day', 'isrc': '----:com.apple.iTunes:ISRC',
}


@dataclass(frozen=True, slots=True)
class Issue:
    '''One thing wrong with one file. `kind` is stable. Renderers key off it.'''

    kind: str          # missing_file | unreadable | duration_mismatch |
                       # missing_tags | no_art | no_metadata | orphan
    path: str
    identity: str = ''
    detail: str = ''
    fixable: bool = False


@dataclass(slots=True)
class AuditReport:
    checked: int = 0
    healthy: int = 0
    issues: list[Issue] = field(default_factory=list)
    fixed: int = 0
    fix_failed: int = 0
    folder_art_written: int = 0
    #: teh sixth claim, three-valued: acoustically confirmed / unknown to
    #: AcoustID (common for bedroom releases, so it flags and never fails) / positive
    #: mismatch (quarantine-grade)
    identity_confirmed: int = 0
    identity_unknown: int = 0
    identity_mismatch: int = 0
    quarantined: int = 0

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    @property
    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for i in self.issues:
            out[i.kind] = out.get(i.kind, 0) + 1
        return out


def try_read_tag_state(path: Path, fmt: str) -> tuple[set[str], bool] | None:
    '''
    Same as read_tag_state, except a file too broken to parse returns None instead
    of killing the audit. Learned that live. A file can pass ffprobe fine and still
    have tag structure mangled past reading.
    '''
    try:
        return read_tag_state(path, fmt)
    except Exception:  # noqa: BLE001  # Mutagen raises a zoo. all of it means 'unreadable'
        return None


def read_tag_state(path: Path, fmt: str) -> tuple[set[str], bool]:
    '''
    Returns (missing required tags, has cover art) for one file. It's dialect
    specific for the same reason writing is, since a vorbis-only check would call
    every mp3 in the library broken.
    '''
    missing: set[str] = set()
    has_art = False

    if fmt in ('opus', 'flac'):
        if fmt == 'opus':
            from mutagen.oggopus import OggOpus
            tags = OggOpus(str(path))
            keys = {k.lower() for k in tags.keys()}
            has_art = 'metadata_block_picture' in keys
        else:
            from mutagen.flac import FLAC
            tags = FLAC(str(path))
            keys = {k.lower() for k in tags.keys()}
            has_art = bool(tags.pictures)
        missing = {t for t in REQUIRED_TAGS if t not in keys}

    elif fmt in ('mp3', 'wav'):
        from mutagen.id3 import ID3
        try:
            tags = ID3(str(path))
        except Exception:  # noqa: BLE001  # No ID3 block at all
            return set(REQUIRED_TAGS), False
        keys = set(tags.keys())
        missing = {name for name, frame in _ID3_MAP.items()
                   if not any(k.startswith(frame) for k in keys)}
        has_art = any(k.startswith('APIC') for k in keys)

    elif fmt == 'm4a':
        from mutagen.mp4 import MP4
        tags = MP4(str(path))
        missing = {name for name, atom in _MP4_MAP.items() if atom not in tags}
        has_art = 'covr' in tags

    else:
        missing = set(REQUIRED_TAGS)

    return missing, has_art


def _verify_identity(fp, track, acoustid) -> tuple[str, str]:
    '''
    Runs the sixth claim and returns (verdict, detail). Wrapped so a flaky AcoustID
    moment drops one file to UNKNOWN instead of taking down the whole audit.
    '''
    from ..core.errors import SpotdlPlusError as _E
    try:
        m = acoustid.verify(fp, expected_recording_id=track.mb_recording_id)
        return str(m.verdict), m.detail
    except _E as exc:
        return 'unknown', f'lookup failed ({exc.code})'


def _check_one(row, store: Store) -> Iterator[Issue]:
    path = Path(row['final_path'])
    fmt = row['format'] or path.suffix.lstrip('.')
    identity = row['identity']

    if not path.is_file() or path.stat().st_size == 0:
        yield Issue('missing_file', str(path), identity,
                    'owned but absent from disk. "spotdlp relink" refetches it')
        return

    try:
        info = probe(path)
    except SpotdlPlusError as exc:
        yield Issue('unreadable', str(path), identity,
                    f'ffprobe rejects it ({exc.code}). "spotdlp relink" refetches it')
        return


    track = store.metadata_for_identity(identity)
    if track is None:
        yield Issue('no_metadata', str(path), identity,
                    'no metadata blob anywhere in the store. can\'t verify or re-tag')
        return

    if track.duration_ms and abs(info.duration_ms - track.duration_ms) > DURATION_TOLERANCE_MS:
        yield Issue('duration_mismatch', str(path), identity,
                    f'runs {info.duration_ms // 1000}s, metadata says '
                    f'{track.duration_ms // 1000}s. "spotdlp relink" to refetch')

    state = try_read_tag_state(path, fmt)
    if state is None:
        yield Issue('unreadable', str(path), identity,
                    'audio probes fine but the tag structure is mangled, '
                    '"spotdlp relink" refetches it cleanly')
        return
    missing, has_art = state
    # a tarck can only be expected to carry what we actually know about it
    expectable = set(REQUIRED_TAGS)
    if not track.isrc:
        expectable.discard('isrc')
    if not (track.album and track.album.release_date):
        expectable.discard('date')
    if not track.track_no:
        expectable.discard('tracknumber')
    if track.album is None:
        expectable.discard('album')
    truly_missing = missing & expectable
    if truly_missing:
        yield Issue('missing_tags', str(path), identity,
                    ', '.join(sorted(truly_missing)), fixable=True)

    if not has_art and track.album is not None and track.album.cover_url:
        yield Issue('no_art', str(path), identity,
                    f'album {track.album.title!r} has a cover we never embedded',
                    fixable=True)


def _quarantine(store: Store, row, path: Path, output_dir: Path,
                report: AuditReport) -> None:
    '''
    Handles a hard mismatch. The file moves whole into <library>/.quarantine/, its
    library row gets revoked, and the track requeues for a fresh match so `resume`
    can fetch a replacement.
    '''
    import shutil

    target_dir = output_dir / '.quarantine'
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    n = 1
    while target.exists():
        target = target_dir / f'{path.stem}.{n}{path.suffix}'
        n += 1
    shutil.move(str(path), str(target))

    tid = store.newest_track_id(row['identity'])
    if tid is not None:
        store.requeue_for_rematch(tid)
    report.quarantined += 1


def _find_orphans(store: Store, output_dir: Path, formats: tuple[str, ...]) -> Iterator[Issue]:
    '''Files in teh library folder that no library row vouches for.'''
    owned = {Path(r['final_path']).resolve() for r in store.library_rows()}
    for fmt in formats:
        for p in output_dir.rglob(f'*.{fmt}'):
            if p.resolve() not in owned:
                yield Issue('orphan', str(p), '',
                            'on disk but not owned. Likely a corrupt-era leftover. '
                            'safe to delete once inspected')


def audit_library(
    store: Store,
    *,
    output_dir: Path,
    cache_dir: Path,
    http: HttpClient | None = None,
    fix: bool = False,
    deep: bool = False,
    acoustid=None,
    bus: EventBus | None = None,
    progress=None,
) -> AuditReport:
    '''
    Checks every owned file and optionally repairs tags and art in place.

    `deep` adds a full decode per file at about half a second each, which catches
    corruption a header probe misses. Passing an `acoustid` client adds the
    identity check, where a real mismatch quarantines under `fix` but UNKNOWN only
    flags, since bedroom releases honestly aren't in the database and that isn't
    the file's fault.
    '''
    from ..media.fingerprint import fingerprint_file

    bus = bus or NullBus()
    report = AuditReport()
    deep = deep or acoustid is not None

    all_rows = store.library_rows()
    for row in all_rows:
        report.checked += 1
        if progress is not None:
            # a deep+identity pass over a real library runs about 20 minutes,
            # and 20 silent minutes reads as a hang. Say something.
            progress(report.checked, len(all_rows), row['final_path'])
        found = list(_check_one(row, store))

        path = Path(row['final_path'])
        if not found and deep and path.is_file():
            # Fingerprinting decodes every sample. It catches mid-stream
            # corruption (CRC-mismatched pages) that header probes pass.
            # Measured: the one file a human flagged was the one that failed
            # here and nowhere else.
            track_meta = store.metadata_for_identity(row['identity'])
            too_short = (track_meta is not None
                         and 0 < track_meta.duration_ms < 5_000)
            try:
                fp = fingerprint_file(path)
            except SpotdlPlusError as exc:
                if too_short:
                    # chromaprint has a floor around a few seconds. a skit
                    # track it can't fingerprint isn't a corrupt file
                    if acoustid is not None:
                        report.identity_unknown += 1
                    report.healthy += 1
                    continue
                found.append(Issue(
                    'unreadable', str(path), row['identity'],
                    f'passes a header probe but won\'t fully decode '
                    f'({exc.code}). "spotdlp relink" refetches it'))
            else:
                if acoustid is not None:
                    track = store.metadata_for_identity(row['identity'])
                    verdict, detail = _verify_identity(fp, track, acoustid)
                    if verdict == 'confirmed':
                        report.identity_confirmed += 1
                    elif verdict == 'unknown':
                        report.identity_unknown += 1
                    else:
                        report.identity_mismatch += 1
                        found.append(Issue(
                            'identity_mismatch', str(path), row['identity'],
                            detail))
                        if fix:
                            _quarantine(store, row, path, output_dir, report)
        if not found:
            report.healthy += 1
            continue

        for issue in found:
            if fix and issue.kind == 'missing_file':
                # A human deleted it outside the tool. Under --fix the answer
                # is not a report but a heal: stop vouching for the ghost adn
                # requeue the track. `spotdlp resume` fetches it back.
                store.revoke_ownership(issue.identity)
                tid = store.newest_track_id(issue.identity)
                if tid is not None:
                    row_t = store.get_track(tid)
                    if row_t is not None and row_t.chosen_url:
                        store.force_relink(tid, row_t.chosen_url)
                    else:
                        store.requeue_for_rematch(tid)
                report.fixed += 1
                report.add(Issue('missing_file', issue.path, issue.identity,
                                 'ownership revoked and track requeued, '
                                 '"spotdlp resume" fetches it back'))
                continue
            if not (fix and issue.fixable):
                report.add(issue)
                continue

            track = store.metadata_for_identity(issue.identity)
            path = Path(issue.path)
            fmt = row['format'] or path.suffix.lstrip('.')
            cover = None
            if http is not None:
                cover = fetch_cover(track.album, http, cache_dir, bus=bus)
            try:
                write_tags(path, track, fmt=fmt, cover=cover)
            except SpotdlPlusError as exc:
                report.fix_failed += 1
                report.add(Issue(issue.kind, issue.path, issue.identity,
                                 f'fix failed: [{exc.code}] {exc.message}'))
                continue

            state_after = try_read_tag_state(path, fmt)
            if state_after is None:
                report.fix_failed += 1
                report.add(Issue('unreadable', issue.path, issue.identity,
                                 'file unreadable after tag rewrite'))
                continue
            still_missing, has_art_now = state_after
            ok = (issue.kind == 'missing_tags' and not still_missing) or \
                 (issue.kind == 'no_art' and has_art_now)
            if ok:
                report.fixed += 1
            else:
                report.fix_failed += 1
                report.add(Issue(issue.kind, issue.path, issue.identity,
                                 'rewrite completed but the deficiency persists'))

    for orphan in _find_orphans(store, output_dir,
                                ('opus', 'mp3', 'flac', 'm4a', 'wav')):
        report.add(orphan)

    if fix and http is not None:
        report.folder_art_written = _backfill_folder_art(store, http, cache_dir, bus)

    return report


def _backfill_folder_art(store: Store, http: HttpClient, cache_dir: Path,
                         bus: EventBus) -> int:
    '''
    Puts a cover.jpg in every album folder that's missing one. Embedded opus art is
    invisible to Explorer and a lot of players, so this is the fallback everything
    understands.
    '''
    written = 0
    seen_dirs: set[Path] = set()
    for row in store.library_rows():
        path = Path(row['final_path'])
        album_dir = path.parent
        if album_dir in seen_dirs or not path.is_file():
            continue
        seen_dirs.add(album_dir)
        if (album_dir / 'cover.jpg').exists():
            continue
        track = store.metadata_for_identity(row['identity'])
        if track is None or track.album is None:
            continue
        cover = fetch_cover(track.album, http, cache_dir, bus=bus)
        if cover and write_folder_art(album_dir, cover):
            written += 1
    return written
