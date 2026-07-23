'''
works.py - telling a reissue from a different song

ISRC identifies a recording and it's the primary key here, but it isn't enough.
Measured against the live API:

    OK Computer                    Airbag               GBAYE9701274   287.9s
    OK Computer OKNOTOK 1997 2017  Airbag - Remastered  GBBKS1700107   283.8s

Same song, different master, different ISRC. Dedupe on ISRC alone and you get
both copies. So tier 2 clusters by title and artist, chains on duration, and
keeps whichever master you asked for.
'''

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .models import MasterPreference, Track, normalize_title

#: Measured twice. OK Computer vs OKNOTOK differ by 4.1s. Los Campesinos!'s
#: 2020 remasters drift 5-8s from thier originals (retrimmed intros), which a
#: 5s tolerance missed. So both editions downloaded. A radio edit differs by
#: tens of seconds, a live take by more. 8 still clears both by a mile.
WORK_DURATION_TOLERANCE_MS = 8_000


def work_key(track: Track) -> str:
    '''
    What two masters of the same song agree on.

    Duration is left out on purpose. It's the guard against merging a live take
    into a studio cut, not part of the identity, and bucketing it puts 287.9s and
    283.8s on opposite sides of a bin boundary.
    '''
    artist = track.artists[0].key if track.artists else ''
    return f'{artist}:{normalize_title(track.title)}'


def same_work(a: Track, b: Track, *, tolerance_ms: int = WORK_DURATION_TOLERANCE_MS) -> bool:
    if work_key(a) != work_key(b):
        return False
    return abs(a.duration_ms - b.duration_ms) <= tolerance_ms


def _release_sort_key(track: Track) -> tuple[str, str]:
    '''
    Sortable release date, oldest first. Partial dates sort right as plain strings
    because ISO-8601 was built that way. No date sorts last instead of winning by
    accident, and identity is the tiebreak so two runs always agree.
    '''
    album = track.album
    date = ''
    if album is not None:
        date = album.original_date or album.release_date or ''
    return (date or '9999', track.identity)


def group_works(
    tracks: Sequence[Track],
    *,
    tolerance_ms: int = WORK_DURATION_TOLERANCE_MS,
) -> list[list[Track]]:
    '''
    Clusters recordings into works. Inside a title and artist key it sorts by
    duration and starts a new cluster when the gap gets too big, so Airbag stays
    with Airbag - Remastered but a 7 minute live version splits off.
    '''
    buckets: dict[str, list[Track]] = {}
    for t in tracks:
        buckets.setdefault(work_key(t), []).append(t)

    clusters: list[list[Track]] = []
    for group in buckets.values():
        group.sort(key=lambda t: (t.duration_ms, t.identity))
        current = [group[0]]
        for track in group[1:]:
            if track.duration_ms - current[-1].duration_ms <= tolerance_ms:
                current.append(track)
            else:
                clusters.append(current)
                current = [track]
        clusters.append(current)
    return clusters


def pick_master(
    cluster: Sequence[Track],
    preference: MasterPreference,
) -> tuple[Track, list[Track]]:
    '''
    Picks the survivor and returns (keeper, dropped). Never called with BOTH, since
    a caller who wants everything shouldn't be collapsing in the first place.
    '''
    if not cluster:
        raise ValueError('cannot pick a master from an empty cluster')
    if preference is MasterPreference.BOTH:
        raise ValueError('MasterPreference.BOTH collapses nothing. do not call pick_master')

    ordered = sorted(cluster, key=_release_sort_key)
    match preference:
        case MasterPreference.ORIGINAL:
            keeper = ordered[0]
        case MasterPreference.REMASTER:
            keeper = ordered[-1]
        case MasterPreference.POPULARITY:
            # Earliest release breaks a popularity tie, so the result is stable.
            keeper = max(ordered, key=lambda t: (t.popularity or -1, _release_sort_key(t)[0] == ''))
        case _:  # pragma: no cover - StrEnum is exhaustive above
            raise ValueError(f'unhandled preference: {preference}')

    return keeper, [t for t in cluster if t is not keeper]


def collapse(
    tracks: Iterable[Track],
    preference: MasterPreference,
    *,
    tolerance_ms: int = WORK_DURATION_TOLERANCE_MS,
) -> tuple[list[Track], list[tuple[Track, Track]]]:
    '''
    Runs the full tier 2 pass over tracks already deduped by ISRC. Returns (kept,
    collapsed) with each drop paired to its keeper, so the event stream can say why
    your remaster went missing.
    '''
    all_tracks = list(tracks)
    if preference is MasterPreference.BOTH:
        return all_tracks, []

    kept: list[Track] = []
    collapsed: list[tuple[Track, Track]] = []
    for cluster in group_works(all_tracks, tolerance_ms=tolerance_ms):
        if len(cluster) == 1:
            kept.append(cluster[0])
            continue
        keeper, dropped = pick_master(cluster, preference)
        kept.append(keeper)
        collapsed.extend((d, keeper) for d in dropped)

    kept.sort(key=lambda t: (t.album.title if t.album else '', t.disc_no or 0, t.track_no or 0))
    return kept, collapsed
