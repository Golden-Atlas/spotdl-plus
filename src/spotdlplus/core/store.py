'''
store.py - job storage and queue management

Every tarck's state lives in SQLite instead of a list in memory. If a run dies
at track 812 of 4,000 we keep the 811 and start back up at 813.

Workers claim leased batches so we never load the whole thing at once. State
transitions get checked so a track can't skip steps. Identity is the ISRC, so
duplicates collide in the database itself instead of in a cleanup pass.
'''

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ErrorRecord, SpotdlPlusError, StoreBusy, StoreCorrupt
from .models import (
    RESUMABLE,
    TRANSITIONS,
    Album,
    ArtistRef,
    MatchResult,
    SecondaryType,
    Track,
    TrackState,
)
from .works import work_key


def _artist_from_path(path: str | None) -> str:
    '''
    Last-resort artist for an owned file whose track row got tidied away. The
    default template is <artist>/<album>/<file>, so the artist folder is two up.
    Falls back to the parent, then to a visible placeholder rather than a blank
    row nobody can interpret.
    '''
    if not path:
        return '(unknown)'
    parts = Path(path).parts
    if len(parts) >= 3:
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return '(unknown)'


def _sqlite_error(exc: sqlite3.Error) -> SpotdlPlusError | None:
    '''
    The two sqlite failures a normal person hits: 'locked' is a second copy of the
    app or antivirus holding the file, 'malformed' is a damaged database. Anything
    else returns None and stays raw.
    '''
    msg = str(exc).lower()
    if 'database is locked' in msg or 'database is busy' in msg:
        return StoreBusy('the run database is locked', cause=exc)
    if ('malformed' in msg or 'not a database' in msg
            or 'file is not a database' in msg or 'disk image is malformed' in msg):
        return StoreCorrupt('the run database is damaged', cause=exc)
    return None


SCHEMA_VERSION = 2

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    profile     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',   -- active|parked|finished
    started_at  REAL NOT NULL,
    finished_at REAL,
    note        TEXT
);

-- one row per distinct recording per run. the UNIQUE is the dedupe.
CREATE TABLE IF NOT EXISTS tracks (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    identity      TEXT NOT NULL,
    work_key      TEXT NOT NULL DEFAULT '',   -- tier 2: the song, across masters
    state         TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    leased_until  REAL NOT NULL DEFAULT 0,

    title         TEXT NOT NULL,
    artist        TEXT NOT NULL,
    album_title   TEXT,
    album_artist  TEXT,
    isrc          TEXT,
    spotify_id    TEXT,
    mb_recording_id TEXT,
    release_group_id TEXT,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    track_no      INTEGER,
    disc_no       INTEGER,
    year          INTEGER,
    release_date  TEXT,                       -- ISO, possibly partial. orders masters.

    metadata_json TEXT NOT NULL,          -- the full blob. nothing is thrown away.

    chosen_url    TEXT,
    match_score   REAL,
    match_basis   TEXT,
    est_bytes     INTEGER,

    final_path    TEXT,
    error_code    TEXT,
    error_json    TEXT,
    skip_reason   TEXT,                       -- 'owned' | 'duplicate:work' | 'pruned'

    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    UNIQUE (run_id, identity)
);

CREATE INDEX IF NOT EXISTS ix_tracks_run_state ON tracks(run_id, state);
CREATE INDEX IF NOT EXISTS ix_tracks_identity  ON tracks(identity);
CREATE INDEX IF NOT EXISTS ix_tracks_work      ON tracks(run_id, work_key);

-- every candidate we considered, winners and losers, with the scoreboard.
-- this is what makes a bad match explainable instead of mysterious.
CREATE TABLE IF NOT EXISTS candidates (
    id           TEXT PRIMARY KEY,
    track_id     TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    rank         INTEGER NOT NULL,
    chosen       INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    uploader     TEXT,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    size_bytes   INTEGER,
    score        REAL NOT NULL DEFAULT 0,
    breakdown_json TEXT,
    rejected_why TEXT
);

CREATE INDEX IF NOT EXISTS ix_candidates_track ON candidates(track_id);

-- what we already own, across all runs. the answer to "stop re-downloading
-- the same song every time the playlist changes by one entry".
CREATE TABLE IF NOT EXISTS library (
    identity   TEXT PRIMARY KEY,
    final_path TEXT NOT NULL,
    format     TEXT,
    size_bytes INTEGER,
    sha256     TEXT,
    added_at   REAL NOT NULL
);

-- what each source CONTAINED last time we walked it. One row per source,
-- latest wins. `sync` reads the previous one to know what left a playlist.
CREATE TABLE IF NOT EXISTS snapshots (
    source_key      TEXT PRIMARY KEY,   -- 'playlist:37i9dQ...'. Kind + spotify id
    label           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    taken_at        REAL NOT NULL,
    identities_json TEXT NOT NULL
);
'''


def _now() -> float:
    return time.time()


def _release_date(track: Track) -> str | None:
    '''The date that orders two masters of the same work. Oldest wins by default.'''
    if track.album is None:
        return None
    return track.album.original_date or track.album.release_date


def _uid() -> str:
    return uuid.uuid4().hex[:16]


class StateTransitionError(RuntimeError):
    '''An illegal move. Loud on purpose. This is always a bug, never input.'''


@dataclass(frozen=True, slots=True)
class TrackRow:
    '''A track as the store sees it. Enough to work on, not the whole blob.'''

    id: str
    run_id: str
    identity: str
    state: TrackState
    attempts: int
    title: str
    artist: str
    album_title: str | None
    isrc: str | None
    duration_ms: int
    chosen_url: str | None
    est_bytes: int | None
    final_path: str | None

    @classmethod
    def from_sql(cls, r: sqlite3.Row) -> 'TrackRow':
        return cls(
            id=r['id'], run_id=r['run_id'], identity=r['identity'],
            state=TrackState(r['state']), attempts=r['attempts'],
            title=r['title'], artist=r['artist'], album_title=r['album_title'],
            isrc=r['isrc'], duration_ms=r['duration_ms'],
            chosen_url=r['chosen_url'], est_bytes=r['est_bytes'],
            final_path=r['final_path'],
        )


@dataclass(frozen=True, slots=True)
class PlanSummary:
    '''The size preview, computed from the store rather than guessed.'''

    total: int
    matched: int
    unmatched: int
    already_have: int      # skip_reason='owned'
    collapsed: int         # skip_reason='duplicate:work'. A better master won
    est_bytes: int
    est_bytes_known: int   # tracks with a real byte count, not an extrapolation

    @property
    def est_bytes_total(self) -> int:
        '''Extrapolate the unknown tracks from the mean of the known ones.'''
        if self.est_bytes_known == 0 or self.matched == 0:
            return self.est_bytes
        mean = self.est_bytes / self.est_bytes_known
        return int(self.est_bytes + mean * (self.matched - self.est_bytes_known))


# ----------------------------------------------------------------------------
# metadata (de)serialization. Explicit, becuase frozensets aren't JSON
# ----------------------------------------------------------------------------

def _artist_ref_to_dict(a: ArtistRef) -> dict[str, Any]:
    return {'name': a.name, 'spotify_id': a.spotify_id, 'mb_artist_id': a.mb_artist_id}


def _album_to_dict(al: Album) -> dict[str, Any]:
    return {
        'title': al.title,
        'artists': [_artist_ref_to_dict(a) for a in al.artists],
        'spotify_id': al.spotify_id,
        'mb_release_id': al.mb_release_id,
        'release_group_id': al.release_group_id,
        'release_type': str(al.release_type),
        'secondary_types': sorted(str(s) for s in al.secondary_types),
        'release_date': al.release_date,
        'original_date': al.original_date,
        'total_tracks': al.total_tracks,
        'total_discs': al.total_discs,
        'label': al.label,
        'upc': al.upc,
        'copyright': al.copyright,
        'cover_url': al.cover_url,
        'is_appears_on': al.is_appears_on,
        'raw': al.raw,
    }


def track_to_json(t: Track) -> str:
    '''Everything we know, kept verbatim. Storage is cheap. Refetching isn't.'''
    return json.dumps({
        'title': t.title,
        'artists': [_artist_ref_to_dict(a) for a in t.artists],
        'album': _album_to_dict(t.album) if t.album else None,
        'isrc': t.isrc,
        'mb_recording_id': t.mb_recording_id,
        'spotify_id': t.spotify_id,
        'duration_ms': t.duration_ms,
        'track_no': t.track_no,
        'disc_no': t.disc_no,
        'explicit': t.explicit,
        'popularity': t.popularity,
        'genres': list(t.genres),
        'raw': t.raw,
    }, ensure_ascii=False, separators=(',', ':'))


def track_from_json(blob: str) -> Track:
    '''Round-trip. Used by retag and relink, whcih must not refetch metadata.'''
    d = json.loads(blob)
    album = None
    if d.get('album'):
        a = d['album']
        from .models import ReleaseType  # local: avoid a cycle at import time
        album = Album(
            title=a['title'],
            artists=tuple(ArtistRef(**x) for x in a.get('artists', [])),
            spotify_id=a.get('spotify_id'),
            mb_release_id=a.get('mb_release_id'),
            release_group_id=a.get('release_group_id'),
            release_type=ReleaseType(a.get('release_type', 'album')),
            secondary_types=frozenset(SecondaryType(s) for s in a.get('secondary_types', [])),
            release_date=a.get('release_date'),
            original_date=a.get('original_date'),
            total_tracks=a.get('total_tracks'),
            total_discs=a.get('total_discs'),
            label=a.get('label'),
            upc=a.get('upc'),
            copyright=a.get('copyright'),
            cover_url=a.get('cover_url'),
            is_appears_on=a.get('is_appears_on', False),
            raw=a.get('raw', {}),
        )
    return Track(
        title=d['title'],
        artists=tuple(ArtistRef(**x) for x in d.get('artists', [])),
        album=album,
        isrc=d.get('isrc'),
        mb_recording_id=d.get('mb_recording_id'),
        spotify_id=d.get('spotify_id'),
        duration_ms=d.get('duration_ms', 0),
        track_no=d.get('track_no'),
        disc_no=d.get('disc_no'),
        explicit=d.get('explicit'),
        popularity=d.get('popularity'),
        genres=tuple(d.get('genres', [])),
        raw=d.get('raw', {}),
    )


# ----------------------------------------------------------------------------
# the store
# ----------------------------------------------------------------------------

class Store:
    '''
    Thread-safe SQLite. WAL so a reader (a GUI polling status) never blocks the
    writer (the pipeline). One lock, held briefly, around every mutation.
    '''

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA synchronous=NORMAL')
        self._db.execute('PRAGMA foreign_keys=ON')
        self._db.execute('PRAGMA busy_timeout=30000')
        self._migrate()

    def _migrate(self) -> None:
        '''
        Upgrade first, then create.

        Order is load-bearing. `_SCHEMA` indexes tracks(work_key). Running it against a
        v1 database raises inside the constructor and the tool is dead on startup.
        '''
        with self._lock:
            db = self._db
            db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

            has_tracks = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'"
            ).fetchone() is not None

            if has_tracks:
                row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                current = int(row['value']) if row else 1
                if current < SCHEMA_VERSION:
                    self._upgrade_from(current)

            db.executescript(_SCHEMA)
            db.execute(
                '''INSERT INTO meta(key, value) VALUES ('schema_version', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
                (str(SCHEMA_VERSION),),
            )
            db.commit()

    def _upgrade_from(self, version: int) -> None:
        '''
                Bolt on columns an old database lacks and backfill them from the metadata blob,
                which was kept for exactly this. Columns only. `_SCHEMA` handles indexes next.

        '''
        db = self._db
        if version < 2:
            existing = {r['name'] for r in db.execute('PRAGMA table_info(tracks)')}
            for name in ('work_key', 'release_date', 'skip_reason'):
                if name not in existing:
                    db.execute(f'ALTER TABLE tracks ADD COLUMN {name} TEXT')

            for r in db.execute('SELECT id, metadata_json FROM tracks').fetchall():
                track = track_from_json(r['metadata_json'])
                db.execute(
                    'UPDATE tracks SET work_key=?, release_date=? WHERE id=?',
                    (work_key(track), _release_date(track), r['id']),
                )

    def close(self) -> None:
        with self._lock:
            self._db.execute('PRAGMA optimize')
            self._db.close()

    def library_stats(self, *, details: bool = False) -> dict[str, Any]:
        '''
        The library in numbers. Read-only, one pass plus per-identity lookups. A few
        thousand rows is nothing, so no caching.
        '''
        with self._lock:
            db = self._db
            totals = db.execute(
                'SELECT COUNT(*) n, COALESCE(SUM(size_bytes),0) size FROM library'
            ).fetchone()
            formats = db.execute(
                '''SELECT format, COUNT(*) n, COALESCE(SUM(size_bytes),0) size
                   FROM library GROUP BY format ORDER BY n DESC'''
            ).fetchall()
            # One track row per identity, since any run's copy carries the
            # metadata. Grouped by album artist because that's the record's
            # owner and it matches the folder layout. Grouping by the full
            # feature list once split one library into 543 artists, one per
            # collab combination.
            #
            # LEFT JOIN, not JOIN. `tidy` prunes track rows for finished runs
            # but ownership outlives them on purpose, so an inner join silently
            # dropped every tidied file out of the artist list AND out of the
            # hours. My own library read 3841 tracks but only 135 artists and
            # 232.1 hours, because 212 owned files had no track row left. Count
            # and size were right the whole time, which is what made it look
            # fine. Orphans fall back to their folder, same as `library` does.
            rows = db.execute(
                '''SELECT l.size_bytes size, l.final_path path,
                          COALESCE(t.album_artist, t.artist) artist,
                          t.duration_ms ms
                   FROM library l
                   LEFT JOIN tracks t ON t.id = (SELECT id FROM tracks
                                                 WHERE identity = l.identity LIMIT 1)'''
            ).fetchall()
            agg: dict[str, list[int]] = {}
            for r in rows:
                artist = r['artist'] or _artist_from_path(r['path'])
                slot = agg.setdefault(artist, [0, 0, 0])
                slot[0] += 1
                slot[1] += r['size'] or 0
                slot[2] += r['ms'] or 0
            per_artist = sorted(agg.items(), key=lambda kv: kv[1][0], reverse=True)
            out: dict[str, Any] = {
                'tracks': totals['n'],
                'bytes': totals['size'],
                'duration_ms': sum(v[2] for _, v in per_artist),
                'artists': len(per_artist),
                'formats': [(r['format'], r['n'], r['size']) for r in formats],
                'per_artist': [(a, v[0], v[1], v[2]) for a, v in per_artist],
            }
            if details:
                out['albums'] = db.execute(
                    '''SELECT t.album_artist || ' - ' || t.album_title name,
                              COUNT(*) n, COALESCE(SUM(l.size_bytes),0) size
                       FROM library l
                       JOIN tracks t ON t.id = (SELECT id FROM tracks
                                                WHERE identity = l.identity LIMIT 1)
                       WHERE t.album_title IS NOT NULL
                       GROUP BY t.album_artist, t.album_title
                       ORDER BY size DESC'''
                ).fetchall()
                out['recent'] = db.execute(
                    '''SELECT final_path, size_bytes, added_at FROM library
                       ORDER BY added_at DESC LIMIT 10'''
                ).fetchall()
                week_ago = _now() - 7 * 86400
                r = db.execute(
                    'SELECT COUNT(*) n, COALESCE(SUM(size_bytes),0) size '
                    'FROM library WHERE added_at > ?', (week_ago,)
                ).fetchone()
                out['last_7_days'] = (r['n'], r['size'])
        return out

    # -- the custodian's queries ---------------------------------------------

    def active_track_ids(self) -> set[str]:
        '''
        Tracks still in flight, whose cache artifacts are LIVE. done/skipped owe the
        cache nothing. failed keeps its artifacts because retry-failed rewinds into them.
        '''
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM tracks WHERE state NOT IN ('done', 'skipped')"
            ).fetchall()
        return {r['id'] for r in rows}

    def latest_run_info(self) -> sqlite3.Row | None:
        '''The newest run, whole row. Undo reads this to know what to unwind.'''
        with self._lock:
            return self._db.execute(
                'SELECT * FROM runs ORDER BY started_at DESC LIMIT 1'
            ).fetchone()

    def run_placements(self, run_id: str) -> list[sqlite3.Row]:
        '''
        What this run actually put in the library: its identities, joined to rows added
        at or after it started. Anything owned before that is someone else's work.
        '''
        with self._lock:
            run = self._db.execute('SELECT started_at FROM runs WHERE id=?',
                                   (run_id,)).fetchone()
            if run is None:
                return []
            return self._db.execute(
                '''SELECT l.* FROM library l
                   WHERE l.added_at >= ?
                     AND l.identity IN (SELECT identity FROM tracks WHERE run_id=?)''',
                (run['started_at'], run_id),
            ).fetchall()

    def delete_run(self, run_id: str) -> int:
        '''Erase a run's bookkeeping whole. Tracks and candidates cascade.'''
        with self._tx() as db:
            db.execute('DELETE FROM candidates WHERE track_id IN '
                       '(SELECT id FROM tracks WHERE run_id=?)', (run_id,))
            db.execute('DELETE FROM tracks WHERE run_id=?', (run_id,))
            return db.execute('DELETE FROM runs WHERE id=?', (run_id,)).rowcount

    def ghost_ownership(self) -> list[sqlite3.Row]:
        '''Library rows whose file is gone from disk. (The disk check is the
        caller's. This just hands over every row to check.)'''
        return self.library_rows()

    def rewrite_library_root(self, old_root: str, new_root: str) -> int:
        '''
                Re-anchor every stored path to a new library root. The bookkeeping half of
                move-library. `library` and `tracks.final_path` move together or resume looks
                in two places. Returns rows touched.

        '''
        with self._tx() as db:
            n = db.execute(
                '''UPDATE library SET final_path =
                   ? || SUBSTR(final_path, LENGTH(?) + 1)
                   WHERE final_path LIKE ? || '%' ''',
                (new_root, old_root, old_root),
            ).rowcount
            db.execute(
                '''UPDATE tracks SET final_path =
                   ? || SUBSTR(final_path, LENGTH(?) + 1)
                   WHERE final_path LIKE ? || '%' ''',
                (new_root, old_root, old_root),
            )
            return n

    # -- snapshots: what a source contained, last we looked ------------------

    def save_snapshot(self, source_key: str, *, label: str, kind: str,
                      identities: Iterable[str]) -> None:
        '''Replace the source's snapshot with what it contains right now.'''
        with self._tx() as db:
            db.execute(
                '''INSERT INTO snapshots (source_key, label, kind, taken_at, identities_json)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(source_key) DO UPDATE SET
                     label=excluded.label, kind=excluded.kind,
                     taken_at=excluded.taken_at, identities_json=excluded.identities_json''',
                (source_key, label, kind, _now(), json.dumps(sorted(set(identities)))),
            )

    def get_snapshot(self, source_key: str) -> tuple[float, frozenset[str]] | None:
        '''(taken_at, identities) for a source, or None the first time around.'''
        with self._lock:
            r = self._db.execute(
                'SELECT taken_at, identities_json FROM snapshots WHERE source_key=?',
                (source_key,),
            ).fetchone()
        if r is None:
            return None
        return r['taken_at'], frozenset(json.loads(r['identities_json']))

    def identity_in_other_snapshots(self, identity: str, *, except_key: str) -> bool:
        '''
        Is this recording still wanted by another synced source? The guard that stops
        `sync --prune` on a playlist from deleting a song an album also claims.
        '''
        with self._lock:
            rows = self._db.execute(
                'SELECT identities_json FROM snapshots WHERE source_key != ?',
                (except_key,),
            ).fetchall()
        return any(identity in json.loads(r['identities_json']) for r in rows)

    def search_owned(self, query: str) -> list[sqlite3.Row]:
        '''Owned files whose title/artist/album matches. SQL only. Stays fast.'''
        like = f'%{query}%'
        with self._lock:
            return self._db.execute(
                '''SELECT l.final_path, l.format, l.size_bytes, l.added_at,
                          t.title, t.artist, t.album_title, t.year, t.duration_ms
                   FROM library l
                   JOIN tracks t ON t.id = (SELECT id FROM tracks
                                            WHERE identity = l.identity LIMIT 1)
                   WHERE t.title LIKE ? OR t.artist LIKE ? OR t.album_title LIKE ?
                   ORDER BY t.artist, t.album_title, t.track_no''',
                (like, like, like),
            ).fetchall()

    def album_completeness(self, query: str) -> list[sqlite3.Row]:
        '''
                Per album: how many tracks we know of vs own. 'Know of' means every recording
                any run ever saw there. Offline, because `search` has to stay instant.

        '''
        like = f'%{query}%'
        with self._lock:
            return self._db.execute(
                '''SELECT t.artist, t.album_title,
                          COUNT(DISTINCT t.identity) known,
                          COUNT(DISTINCT CASE WHEN l.identity IS NOT NULL
                                              THEN t.identity END) owned
                   FROM tracks t
                   LEFT JOIN library l ON l.identity = t.identity
                   WHERE t.album_title IS NOT NULL
                     AND (t.artist LIKE ? OR t.album_title LIKE ?
                          -- a TITLE hit resolves to its whole album: asking
                          -- 'do I have creep' really asks about Pablo Honey
                          OR (t.artist, t.album_title) IN (
                              SELECT artist, album_title FROM tracks
                              WHERE title LIKE ? AND album_title IS NOT NULL))
                   GROUP BY t.artist, t.album_title
                   ORDER BY t.artist, t.album_title''',
                (like, like, like),
            ).fetchall()

    def tidy(self, *, keep_recent: int = 5) -> dict[str, int]:
        '''
        Prune bookkeeping nobody will ask for again, then compact.

        Prunable = finished AND holding no failed tracks. Failed tracks are the relink
        queue and eating pending verdicts would be silent data loss. The newest
        `keep_recent` runs survive regardless. The library table is never touched.
        '''
        with self._tx() as db:
            prunable = [r['id'] for r in db.execute(
                '''SELECT id FROM runs
                   WHERE status = 'finished'
                     AND NOT EXISTS (SELECT 1 FROM tracks t
                                     WHERE t.run_id = runs.id AND t.state = 'failed')
                     AND id NOT IN (SELECT id FROM runs
                                    ORDER BY started_at DESC LIMIT ?)''',
                (keep_recent,),
            ).fetchall()]
            if not prunable:
                return {'runs': 0, 'tracks': 0, 'candidates': 0, 'reclaimed_bytes': 0}
            marks = ','.join('?' * len(prunable))
            cands = db.execute(
                f'''DELETE FROM candidates WHERE track_id IN
                    (SELECT id FROM tracks WHERE run_id IN ({marks}))''',
                prunable,
            ).rowcount
            tracks = db.execute(
                f'DELETE FROM tracks WHERE run_id IN ({marks})', prunable).rowcount
            runs = db.execute(
                f'DELETE FROM runs WHERE id IN ({marks})', prunable).rowcount

        # VACUUM rebuilds the file and hands the space back to the OS. It must
        # run OUTSIDE any transaction, hence after the _tx block.
        before = self.path.stat().st_size if self.path.is_file() else 0
        with self._lock:
            self._db.execute('VACUUM')
        after = self.path.stat().st_size if self.path.is_file() else 0
        return {'runs': runs, 'tracks': tracks, 'candidates': cands,
                'reclaimed_bytes': max(0, before - after)}

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        '''One writer at a time, committed or rolled back. No leaked handles.'''
        with self._lock:
            try:
                yield self._db
                self._db.commit()
            except sqlite3.Error as exc:
                try:
                    self._db.rollback()
                except sqlite3.Error:
                    pass                          # a corrupt db can't even roll back
                mapped = _sqlite_error(exc)
                if mapped is not None:
                    raise mapped from exc         # STORE_BUSY / STORE_CORRUPT
                raise
            except Exception:
                self._db.rollback()
                raise

    # -- runs ---------------------------------------------------------------

    def create_run(self, source: str, profile: str) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self._tx() as db:
            db.execute(
                'INSERT INTO runs(id, source, profile, status, started_at) VALUES (?,?,?,?,?)',
                (run_id, source, profile, 'active', _now()),
            )
        return run_id

    def set_run_status(self, run_id: str, status: str, note: str | None = None) -> None:
        fin = _now() if status == 'finished' else None
        with self._tx() as db:
            db.execute(
                'UPDATE runs SET status=?, finished_at=COALESCE(?, finished_at), note=? WHERE id=?',
                (status, fin, note, run_id),
            )

    def latest_run(self, statuses: Sequence[str] = ('active', 'parked')) -> str | None:
        '''What `spotdlp resume` reaches for when you don't name a run.'''
        q = ','.join('?' * len(statuses))
        with self._lock:
            r = self._db.execute(
                f'SELECT id FROM runs WHERE status IN ({q}) ORDER BY started_at DESC LIMIT 1',
                tuple(statuses),
            ).fetchone()
        return r['id'] if r else None

    def latest_run_with_failures(self) -> str | None:
        '''
        The newest run of any status still holding failed tracks. A run that finished
        with failures is `finished`, so without this `resume --retry-failed` answered
        "nothing to resume" in precisely its advertised scenario.
        '''
        with self._lock:
            r = self._db.execute(
                '''SELECT r.id FROM runs r
                   WHERE EXISTS (SELECT 1 FROM tracks t
                                 WHERE t.run_id = r.id AND t.state = 'failed')
                   ORDER BY r.started_at DESC LIMIT 1''',
            ).fetchone()
        return r['id'] if r else None

    # -- tracks -------------------------------------------------------------

    def add_track(self, run_id: str, track: Track) -> tuple[str, bool]:
        '''
        Insert a track. Returns (track_id, was_new).

        UNIQUE(run_id, identity) does the deduping: the same recording from the album,
        the reissue, and a compilation lands once, because identity is the ISRC.
        '''
        tid = _uid()
        album = track.album
        with self._tx() as db:
            cur = db.execute(
                '''INSERT OR IGNORE INTO tracks (
                       id, run_id, identity, work_key, state, attempts,
                       title, artist, album_title, album_artist,
                       isrc, spotify_id, mb_recording_id, release_group_id,
                       duration_ms, track_no, disc_no, year, release_date,
                       metadata_json, created_at, updated_at
                   ) VALUES (?,?,?,?,?,0, ?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?,?)''',
                (
                    tid, run_id, track.identity, work_key(track), str(TrackState.DISCOVERED),
                    track.title, track.artists_display,
                    album.title if album else None, track.album_artist,
                    track.isrc, track.spotify_id, track.mb_recording_id,
                    album.release_group_id if album else None,
                    track.duration_ms, track.track_no, track.disc_no,
                    album.year if album else None, _release_date(track),
                    track_to_json(track), _now(), _now(),
                ),
            )
            if cur.rowcount == 0:
                existing = db.execute(
                    'SELECT id FROM tracks WHERE run_id=? AND identity=?',
                    (run_id, track.identity),
                ).fetchone()
                return existing['id'], False
        return tid, True

    def get_track(self, track_id: str) -> TrackRow | None:
        with self._lock:
            r = self._db.execute('SELECT * FROM tracks WHERE id=?', (track_id,)).fetchone()
        return TrackRow.from_sql(r) if r else None

    def get_track_metadata(self, track_id: str) -> Track | None:
        '''The full blob back as a Track. Nothing is refetched to do this.'''
        with self._lock:
            r = self._db.execute(
                'SELECT metadata_json FROM tracks WHERE id=?', (track_id,)
            ).fetchone()
        return track_from_json(r['metadata_json']) if r else None

    def advance(self, track_id: str, to: TrackState, **fields: Any) -> None:
        '''
        Move a track forward. Refuses illegal transitions rather than letting a
        half-fetched file get tagged and shipped into your library.
        '''
        allowed_cols = {
            'chosen_url', 'match_score', 'match_basis', 'est_bytes', 'final_path',
            'isrc', 'mb_recording_id', 'release_group_id', 'metadata_json',
            'skip_reason',
        }
        bad = set(fields) - allowed_cols
        if bad:
            raise ValueError(f'advance() cannot set columns: {sorted(bad)}')

        with self._tx() as db:
            row = db.execute('SELECT state FROM tracks WHERE id=?', (track_id,)).fetchone()
            if row is None:
                raise KeyError(f'no such track: {track_id}')
            cur = TrackState(row['state'])
            if to not in TRANSITIONS[cur]:
                raise StateTransitionError(f'{track_id}: {cur} -> {to} is not a legal move')

            sets = ['state=?', 'updated_at=?', 'leased_until=0']
            args: list[Any] = [str(to), _now()]
            if to is not TrackState.FAILED:
                sets += ['error_code=NULL', 'error_json=NULL']
            for k, v in fields.items():
                sets.append(f'{k}=?')
                args.append(v)
            args.append(track_id)
            db.execute(f'UPDATE tracks SET {", ".join(sets)} WHERE id=?', args)

    def fail(
        self,
        track_id: str,
        err: ErrorRecord,
        *,
        retryable_to: TrackState | None = None,
        defer_s: float = 0.0,
    ) -> None:
        '''
                Record a failure. Retryable errors rewind to the stage they can restart from;
                everything else rests in FAILED with its remedy attached.

                `defer_s` is backoff without sleeping. The track keeps its lease into the
                future, so it's invisible to claim() until the penalty elapses. A thread
                waiting out 60s is a thread doing nothing.

        '''
        with self._tx() as db:
            row = db.execute('SELECT state, attempts FROM tracks WHERE id=?', (track_id,)).fetchone()
            if row is None:
                raise KeyError(f'no such track: {track_id}')
            attempts = row['attempts'] + 1
            target = retryable_to or TrackState.FAILED
            until = _now() + defer_s if defer_s > 0 else 0.0
            db.execute(
                '''UPDATE tracks SET state=?, attempts=?, error_code=?, error_json=?,
                   leased_until=?, updated_at=? WHERE id=?''',
                (str(target), attempts, err.code, json.dumps(err.to_dict()), until, _now(), track_id),
            )

    def claim(
        self,
        run_id: str,
        states: Sequence[TrackState],
        *,
        limit: int = 8,
        lease_s: float = 300.0,
    ) -> list[TrackRow]:
        '''
                Lease a batch of work. A worker that dies loses its lease after `lease_s` and
                the tracks return to the pool, no manual unsticking.

        '''
        now = _now()
        q = ','.join('?' * len(states))
        with self._tx() as db:
            rows = db.execute(
                f'''SELECT * FROM tracks
                    WHERE run_id=? AND state IN ({q}) AND leased_until < ?
                    ORDER BY disc_no NULLS LAST, track_no NULLS LAST, created_at
                    LIMIT ?''',
                (run_id, *[str(s) for s in states], now, limit),
            ).fetchall()
            if rows:
                ids = [r['id'] for r in rows]
                db.execute(
                    f'UPDATE tracks SET leased_until=? WHERE id IN ({",".join("?" * len(ids))})',
                    (now + lease_s, *ids),
                )
        return [TrackRow.from_sql(r) for r in rows]

    def pending(self, run_id: str, states: Sequence[TrackState]) -> tuple[int, float]:
        '''
        Work in these states, claimable or not, plus how long until the soonest lease
        frees. The Engine needs both: an empty claim() can mean "every track is serving
        a penalty", and exiting there would drop them.
        '''
        q = ','.join('?' * len(states))
        args = [run_id, *[str(s) for s in states]]
        with self._lock:
            r = self._db.execute(
                f'''SELECT COUNT(*) c, MIN(leased_until) soonest
                    FROM tracks WHERE run_id=? AND state IN ({q})''',
                args,
            ).fetchone()
        count = r['c'] or 0
        wait = max(0.0, (r['soonest'] or 0.0) - _now()) if count else 0.0
        return count, wait

    def mark_skipped(self, track_id: str, reason: str) -> None:
        '''
                Take a track out of the run on purpose, with the reason on the record. `owned`,
                `duplicate:work`, `pruned`. None are failures, all are questions you'll ask.

        '''
        self.advance(track_id, TrackState.SKIPPED, skip_reason=reason)

    def iter_work_groups(
        self,
        run_id: str,
        *,
        states: Sequence[TrackState] = (TrackState.DISCOVERED, TrackState.SKIPPED),
    ) -> Iterator[tuple[str, list[tuple[str, Track, TrackState]]]]:
        '''
                Stream tracks grouped by work, one group at a time.

                SKIPPED rows ride along by default, and that default cost a real bug: reissue
                tracks carry fresh ISRCs, so with the originals owned-and-skipped the reissues
                had no rivals and re-downloaded an album the archive held, eleven duplicates
                on a real disk. The collapse pass has to see what the archive already decided.

                Ordered by work_key, so memory costs the largest group, not the discography.

        '''
        q = ','.join('?' * len(states))
        with self._lock:
            rows = self._db.execute(
                f'''SELECT id, work_key, state, metadata_json FROM tracks
                    WHERE run_id=? AND state IN ({q}) AND work_key != ''
                    ORDER BY work_key, duration_ms, id''',
                (run_id, *[str(s) for s in states]),
            ).fetchall()

        current_key: str | None = None
        group: list[tuple[str, Track, TrackState]] = []
        for r in rows:
            if r['work_key'] != current_key:
                if group:
                    yield current_key, group   # type: ignore[misc]
                current_key, group = r['work_key'], []
            group.append((r['id'], track_from_json(r['metadata_json']),
                          TrackState(r['state'])))
        if group:
            yield current_key, group   # type: ignore[misc]

    def find_tracks(self, query: str, *, limit: int = 10) -> list[sqlite3.Row]:
        '''Human search over everything we have ever queued. Powers `relink`.'''
        like = f'%{query}%'
        with self._lock:
            return self._db.execute(
                '''SELECT id, run_id, title, artist, album_title, state, chosen_url,
                          match_score, final_path
                   FROM tracks
                   WHERE title LIKE ? OR artist LIKE ? OR id = ?
                   ORDER BY updated_at DESC LIMIT ?''',
                (like, like, query, limit),
            ).fetchall()

    def force_relink(self, track_id: str, url: str) -> None:
        '''
                A human said the match is wrong, and that outranks the state machine.

                The one sanctioned TRANSITIONS bypass: straight to MATCHED with the new source,
                attempts reset, library row revoked. A file we no longer trust must never
                justify another skip.

        '''
        with self._tx() as db:
            row = db.execute('SELECT identity FROM tracks WHERE id=?', (track_id,)).fetchone()
            if row is None:
                raise KeyError(f'no such track: {track_id}')
            db.execute(
                '''UPDATE tracks SET state=?, chosen_url=?, match_basis='relink',
                       match_score=NULL, attempts=0, leased_until=0,
                       error_code=NULL, error_json=NULL, skip_reason=NULL,
                       final_path=NULL, updated_at=?
                   WHERE id=?''',
                (str(TrackState.MATCHED), url, _now(), track_id),
            )
            db.execute('DELETE FROM library WHERE identity=?', (row['identity'],))
            db.execute(
                "UPDATE runs SET status='active' WHERE id="
                '(SELECT run_id FROM tracks WHERE id=?)',
                (track_id,),
            )

    def newest_track_id(self, identity: str) -> str | None:
        '''The most recent track row for a recording, across all runs.'''
        with self._lock:
            r = self._db.execute(
                'SELECT id FROM tracks WHERE identity=? ORDER BY updated_at DESC LIMIT 1',
                (identity,),
            ).fetchone()
        return r['id'] if r else None

    def requeue_for_rematch(self, track_id: str) -> None:
        '''
        Identity mismatch: the audio isn't this recording, so the source itself is
        condemned. Unlike force_relink it clears the choice and rewinds to ENRICHED.
        Ownership is revoked. The scoreboard survives so `relink` keeps its history.
        '''
        with self._tx() as db:
            row = db.execute('SELECT identity FROM tracks WHERE id=?', (track_id,)).fetchone()
            if row is None:
                raise KeyError(f'no such track: {track_id}')
            db.execute(
                '''UPDATE tracks SET state=?, chosen_url=NULL, match_score=NULL,
                       match_basis=NULL, attempts=0, leased_until=0,
                       error_code=NULL, error_json=NULL, skip_reason=NULL,
                       final_path=NULL, updated_at=?
                   WHERE id=?''',
                (str(TrackState.ENRICHED), _now(), track_id),
            )
            db.execute('DELETE FROM library WHERE identity=?', (row['identity'],))
            db.execute(
                "UPDATE runs SET status='active' WHERE id="
                '(SELECT run_id FROM tracks WHERE id=?)',
                (track_id,),
            )

    def rewind_failed(self, run_id: str, *, codes: Sequence[str] | None = None) -> int:
        '''
                Give FAILED tracks another life. MATCHED if they have a source, else ENRICHED.
                Attempts reset, because someone re-running believes the world changed.

                `codes` narrows it: retrying FETCH_BLOCKED shouldn't re-run 41 searches that
                will refuse again for the same honest reasons. Returns how many came back.

        '''
        code_filter = ''
        args: list[Any] = [str(TrackState.MATCHED), str(TrackState.ENRICHED), _now(),
                           run_id, str(TrackState.FAILED)]
        if codes:
            code_filter = f' AND error_code IN ({",".join("?" * len(codes))})'
            args += list(codes)
        with self._tx() as db:
            cur = db.execute(
                f'''UPDATE tracks SET
                       state = CASE WHEN chosen_url IS NOT NULL THEN ? ELSE ? END,
                       attempts = 0, leased_until = 0,
                       error_code = NULL, error_json = NULL, updated_at = ?
                   WHERE run_id = ? AND state = ?{code_filter}''',
                args,
            )
            return cur.rowcount

    def match_queue(self, query: str | None = None) -> list[sqlite3.Row]:
        '''
                Every track waiting on a human's matching verdict. Refusals and ties,
                across all runs, deduped by identity. Feeds `relink --queue`. `query` narrows it, because
                72 prompts is a chore when 68 of them weren't today's errand.

        '''
        filter_sql = ''
        args: list[str] = []
        if query:
            like = f'%{query}%'
            filter_sql = (' AND (t.artist LIKE ? OR t.title LIKE ? '
                          'OR t.album_title LIKE ?)')
            args = [like, like, like]
        with self._lock:
            return self._db.execute(
                f'''SELECT t.id, t.run_id, t.title, t.artist, t.album_title,
                          t.duration_ms, t.error_code, t.identity
                   FROM tracks t
                   JOIN (SELECT identity, MAX(updated_at) AS latest
                         FROM tracks WHERE state='failed'
                           AND error_code IN ('MATCH_NONE', 'MATCH_AMBIGUOUS')
                         GROUP BY identity) newest
                     ON t.identity = newest.identity AND t.updated_at = newest.latest
                   WHERE t.state = 'failed'{filter_sql}
                   ORDER BY t.artist, t.album_title, t.title''',
                args,
            ).fetchall()

    def release(self, track_id: str) -> None:
        '''
        Hand a claimed track back untouched: no state change, no attempt burned. Being
        offline isn't the track's fault and shouldn't cost it a life.
        '''
        with self._tx() as db:
            db.execute('UPDATE tracks SET leased_until=0 WHERE id=?', (track_id,))

    def resumable(self, run_id: str) -> int:
        q = ','.join('?' * len(RESUMABLE))
        with self._lock:
            r = self._db.execute(
                f'SELECT COUNT(*) c FROM tracks WHERE run_id=? AND state IN ({q})',
                (run_id, *[str(s) for s in RESUMABLE]),
            ).fetchone()
        return r['c']

    # -- candidates: the scoreboard ------------------------------------------

    def record_match(self, track_id: str, result: MatchResult) -> None:
        '''
                Persist the whole decision. The pick, its breakdown, every loser and why.
                `relink` reads it back, so correcting a match never costs a re-search.

        '''
        with self._tx() as db:
            db.execute('DELETE FROM candidates WHERE track_id=?', (track_id,))
            rank = 0
            if result.chosen:
                db.execute(
                    '''INSERT INTO candidates
                       (id, track_id, rank, chosen, source, url, title, uploader,
                        duration_ms, size_bytes, score, breakdown_json, rejected_why)
                       VALUES (?,?,?,1,?,?,?,?,?,?,?,?,NULL)''',
                    (_uid(), track_id, rank, result.chosen.source, result.chosen.url,
                     result.chosen.title, result.chosen.uploader, result.chosen.duration_ms,
                     result.chosen.size_bytes, result.score, json.dumps(result.breakdown)),
                )
            for cand, why, cscore in result.rejected:
                rank += 1
                db.execute(
                    '''INSERT INTO candidates
                       (id, track_id, rank, chosen, source, url, title, uploader,
                        duration_ms, size_bytes, score, breakdown_json, rejected_why)
                       VALUES (?,?,?,0,?,?,?,?,?,?,?,NULL,?)''',
                    (_uid(), track_id, rank, cand.source, cand.url, cand.title,
                     cand.uploader, cand.duration_ms, cand.size_bytes, cscore, why),
                )

    def record_scoreboard(self, track_id: str, scoreboard) -> None:
        '''
        Persist the scoreboard for a track we REFUSED. Duck-typed over the match
        layer's Scored objects because core imports nothing from up there. Without it
        `relink` shows an empty table and `--auto` has nothing to read.
        '''
        with self._tx() as db:
            db.execute('DELETE FROM candidates WHERE track_id=?', (track_id,))
            for rank, s in enumerate(scoreboard):
                c = s.candidate
                db.execute(
                    '''INSERT INTO candidates
                       (id, track_id, rank, chosen, source, url, title, uploader,
                        duration_ms, size_bytes, score, breakdown_json, rejected_why)
                       VALUES (?,?,?,0,?,?,?,?,?,?,?,?,?)''',
                    (_uid(), track_id, rank, c.source, c.url, c.title, c.uploader,
                     c.duration_ms, c.size_bytes, s.score,
                     json.dumps(dict(s.breakdown)), s.rejected_reason),
                )

    def candidates(self, track_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                'SELECT * FROM candidates WHERE track_id=? ORDER BY chosen DESC, rank',
                (track_id,),
            ).fetchall()

    # -- library: what we already own ----------------------------------------

    def skipped(self, run_id: str, reason: str) -> list[sqlite3.Row]:
        '''Everything taken out of the run for one reason. Feeds `explain`.'''
        with self._lock:
            return self._db.execute(
                '''SELECT id, title, artist, album_title, isrc, skip_reason
                   FROM tracks WHERE run_id=? AND skip_reason=? ORDER BY artist, title''',
                (run_id, reason),
            ).fetchall()

    def metadata_for_identity(self, identity: str) -> Track | None:
        '''
        The richest metadata we ever stored for a recording, from any run. This is what
        lets `audit --fix` re-tag without touching a provider.
        '''
        with self._lock:
            r = self._db.execute(
                '''SELECT metadata_json FROM tracks
                   WHERE identity=? AND metadata_json IS NOT NULL
                   ORDER BY updated_at DESC LIMIT 1''',
                (identity,),
            ).fetchone()
        return track_from_json(r['metadata_json']) if r else None

    def library_rows(self) -> list[sqlite3.Row]:
        '''Everything verified and owned, across all runs. Powers `library`.'''
        with self._lock:
            return self._db.execute(
                'SELECT identity, final_path, format, size_bytes, added_at '
                'FROM library ORDER BY final_path',
            ).fetchall()

    def own(self, identity: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                'SELECT * FROM library WHERE identity=?', (identity,)
            ).fetchone()

    def owner_of_path(self, path: Path | str) -> sqlite3.Row | None:
        '''
        The library row that claims this exact file, or None when nothing does.
        Compared resolved, because the stored path and a freshly rendered one
        can differ by a drive-letter case or a separator and still be the same
        file. None here means the file on disk is somebody else's.
        '''
        want = Path(path).resolve()
        with self._lock:
            rows = self._db.execute('SELECT * FROM library').fetchall()
        for r in rows:
            if r['final_path'] and Path(r['final_path']).resolve() == want:
                return r
        return None

    def revoke_ownership(self, identity: str) -> bool:
        '''
        Stop vouching for a recording. When the file is gone or distrusted the promise
        has to die with it, or every future download skips against a ghost.
        '''
        with self._tx() as db:
            cur = db.execute('DELETE FROM library WHERE identity=?', (identity,))
            return cur.rowcount > 0

    def remember(self, identity: str, path: str, fmt: str, size: int, sha256: str) -> None:
        with self._tx() as db:
            db.execute(
                '''INSERT INTO library(identity, final_path, format, size_bytes, sha256, added_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(identity) DO UPDATE SET
                       final_path=excluded.final_path, format=excluded.format,
                       size_bytes=excluded.size_bytes, sha256=excluded.sha256''',
                (identity, path, fmt, size, sha256, _now()),
            )

    # -- reporting -----------------------------------------------------------

    def counts(self, run_id: str) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                'SELECT state, COUNT(*) c FROM tracks WHERE run_id=? GROUP BY state', (run_id,)
            ).fetchall()
        return {r['state']: r['c'] for r in rows}

    def plan_summary(self, run_id: str) -> PlanSummary:
        '''What the size preview reads. No downloads have happened yet.'''
        with self._lock:
            r = self._db.execute(
                '''SELECT
                     COUNT(*)                                        AS total,
                     SUM(chosen_url IS NOT NULL)                     AS matched,
                     SUM(chosen_url IS NULL AND state != 'skipped')  AS unmatched,
                     SUM(skip_reason = 'owned')                      AS already_have,
                     SUM(skip_reason = 'duplicate:work')             AS collapsed,
                     COALESCE(SUM(est_bytes), 0)                     AS est_bytes,
                     SUM(est_bytes IS NOT NULL)                      AS known
                   FROM tracks WHERE run_id=?''',
                (run_id,),
            ).fetchone()
        return PlanSummary(
            total=r['total'] or 0,
            matched=r['matched'] or 0,
            unmatched=r['unmatched'] or 0,
            already_have=r['already_have'] or 0,
            collapsed=r['collapsed'] or 0,
            est_bytes=r['est_bytes'] or 0,
            est_bytes_known=r['known'] or 0,
        )

    def failures(self, run_id: str) -> list[dict[str, Any]]:
        '''Every failure with its remedy, ready to render. Transparency, on tap.'''
        with self._lock:
            rows = self._db.execute(
                '''SELECT id, title, artist, error_code, error_json, attempts
                   FROM tracks WHERE run_id=? AND state='failed' ORDER BY updated_at''',
                (run_id,),
            ).fetchall()
        out = []
        for r in rows:
            rec = json.loads(r['error_json']) if r['error_json'] else {}
            out.append({
                'track_id': r['id'], 'title': r['title'], 'artist': r['artist'],
                'attempts': r['attempts'], **rec,
            })
        return out
