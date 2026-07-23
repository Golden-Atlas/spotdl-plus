'''
expand.py - turning one entity into a stream of tracks

Everything here is a generator, so an artist with 600 tracks costs the same as
one with 6. Tracks flow out as they're found and land in the store.

The artist path does the real work. Spotify hands back 59 album objects for
Radiohead, mixing in singles and records they only appear on, and it labels the
live album, the compilation, and the remix collection all as `album`. So we
re-fetch each release for its UPC, ask MusicBrainz what it actually is, and let
the profile decide after that. Every rejection gets announced.

One rule beats the rest. An album or playlist you asked for by name is never
filtered.
'''

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import replace

from ..core.errors import ProviderFailed, RateLimited
from ..core.events import EntityDiscovered, EventBus, ReleaseFiltered, Stage, StageFinished, StageStarted, Warned
from ..core.models import (Album, EntityKind, MasterPreference, SecondaryType,
                           SelectionProfile, Track)
from ..providers.musicbrainz import MusicBrainzProvider
from ..providers.spotify import MAX_ALBUM_IDS, SpotifyProvider
from .resolve import Resolution
from .selection import exclusion_reason, needs_enrichment, spotify_album_groups

#: Conservative on purpose: 'live at/in/from <venue>' and 'unplugged' are
#: release-naming conventions, but a bare 'live' is a word bands put in studio
#: album titles all the time. Matching it would walk us right back into teh old
#: mistakes.
_LIVE_HINT = re.compile(r'\b(live (at|in|from) |unplugged)', re.IGNORECASE)


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def _genre_stamper(sp: SpotifyProvider) -> 'callable':
    '''
    Spotify puts genres on artists, not tracks, so each track borrows its primary
    artist's. Fetched once per artist and memoized. If it fails we stamp nothing,
    since genres are decoration.
    '''
    memo: dict[str, tuple[str, ...]] = {}

    def stamp(track: Track) -> Track:
        if track.genres:
            return track
        primary = track.artists[0] if track.artists else None
        aid = primary.spotify_id if primary else None
        if not aid:
            return track
        if aid not in memo:
            try:
                memo[aid] = sp.artist(aid).genres
            except Exception:  # noqa: BLE001  # No genres beats no track
                memo[aid] = ()
        return replace(track, genres=memo[aid]) if memo[aid] else track

    return stamp


def expand(
    resolution: Resolution,
    sp: SpotifyProvider,
    *,
    profile: SelectionProfile,
    mb: MusicBrainzProvider | None = None,
    bus: EventBus,
    run_id: str,
) -> Iterator[Track]:
    '''Stream every track this entity implies, under the given profile.'''
    bus.emit(StageStarted(run_id=run_id, stage=Stage.EXPAND))
    count = 0
    stamp = _genre_stamper(sp)

    match resolution.kind:
        case EntityKind.TRACK:
            yield stamp(sp.track(resolution.spotify_id))
            count = 1

        case EntityKind.ALBUM:
            # Explicitly asked for. The profile doesn't get a vote.
            for track in sp.album_tracks(resolution.spotify_id):
                count += 1
                yield stamp(track)

        case EntityKind.PLAYLIST:
            for track in sp.playlist_tracks(resolution.spotify_id):
                count += 1
                yield stamp(track)

        case EntityKind.ARTIST:
            for track in _expand_artist(resolution, sp, profile=profile, mb=mb,
                                        bus=bus, run_id=run_id):
                count += 1
                yield stamp(track)

    bus.emit(StageFinished(run_id=run_id, stage=Stage.EXPAND, count=count))


def _expand_artist(
    resolution: Resolution,
    sp: SpotifyProvider,
    *,
    profile: SelectionProfile,
    mb: MusicBrainzProvider | None,
    bus: EventBus,
    run_id: str,
) -> Iterator[Track]:
    groups = spotify_album_groups(profile)
    pairs = list(sp.artist_album_ids(resolution.spotify_id, groups=groups))

    bus.emit(EntityDiscovered(
        run_id=run_id, entity_kind=str(EntityKind.ARTIST),
        entity_id=resolution.spotify_id, name=resolution.name, child_count=len(pairs),
    ))

    # `album_group` is the only place Spotify admits the artist is a guest
    # rather thna the owner of a record. It doesn't survive the full-album re-
    # fetch, so it has to be carried across by hand.
    group_of = dict(pairs)
    enrich = mb is not None and needs_enrichment(profile)

    kept: list[Album] = []
    for chunk in _chunks([aid for aid, _ in pairs], MAX_ALBUM_IDS):
        for album in sp.albums(chunk):
            album = replace(album, is_appears_on=group_of.get(album.spotify_id) == 'appears_on')

            if enrich:
                album = _classify(album, mb, bus=bus, run_id=run_id)

            reason = exclusion_reason(album, profile)
            if reason is not None:
                bus.emit(ReleaseFiltered(
                    run_id=run_id, album_id=album.spotify_id or '',
                    title=album.title, reason=reason,
                ))
                continue
            kept.append(album)

    for album in _collapse_release_groups(kept, profile, bus=bus, run_id=run_id):
        bus.emit(EntityDiscovered(
            run_id=run_id, entity_kind=str(EntityKind.ALBUM),
            entity_id=album.spotify_id or '', name=album.title,
            child_count=album.total_tracks or 0,
        ))
        yield from sp.tracks_of(album)


def _collapse_release_groups(
    albums: list[Album],
    profile: SelectionProfile,
    *,
    bus: EventBus,
    run_id: str,
) -> list[Album]:
    '''
    Keeps one release per MusicBrainz release-group.

    I learned this on Los Campesinos!. They rename tracks across remasters, so
    title-keyed dedupe can't see through it and both editions landed whole.

    A release with more tracks than the preferred edition survives anyway, because
    dropping it would lose its exclusive bonus tracks. The track-level pass folds
    the overlap after that.
    '''
    if profile.master_preference is MasterPreference.BOTH:
        return albums

    by_group: dict[str, list[Album]] = {}
    for al in albums:
        by_group.setdefault(al.cluster_key, []).append(al)

    def date_of(al: Album) -> str:
        return al.original_date or al.release_date or '9999'

    result: list[Album] = []
    for group in by_group.values():
        if len(group) == 1:
            result.append(group[0])
            continue
        group.sort(key=date_of)
        preferred = group[0] if profile.master_preference is MasterPreference.ORIGINAL \
            else group[-1]
        for al in group:
            if al is preferred or (al.total_tracks or 0) > (preferred.total_tracks or 0):
                result.append(al)
            else:
                bus.emit(ReleaseFiltered(
                    run_id=run_id, album_id=al.spotify_id or '', title=al.title,
                    reason=f'release_group:superseded by {preferred.title!r}',
                ))
    return result


def _classify(album: Album, mb: MusicBrainzProvider, *, bus: EventBus, run_id: str) -> Album:
    '''
    Asks MusicBrainz what kind of record this is.

    Failing here doesn't fail the run. An unclassified album keeps Spotify's guess,
    which usually means it gets included, and we say so out loud because a live
    album sneaking into a canonical library is worse than a warning nobody reads.

    RateLimited gets caught here for a reason I learned the hard way. Expansion
    runs outside the Engine so nothing reads the retry policy, and an uncaught
    MusicBrainz 429, an error that literally means wait a moment, killed an entire
    artist run.
    '''
    try:
        enriched = mb.enrich_album(album)
    except RateLimited as exc:
        bus.emit(Warned(
            run_id=run_id,
            message=f'musicbrainz throttled us. {album.title!r} keeps Spotify\'s guess, '
                    f're-run later to classify it properly',
            context={'album': album.title, 'error': exc.code,
                     'retry_after': exc.retry_after},
        ))
        return album
    except ProviderFailed as exc:
        bus.emit(Warned(
            run_id=run_id,
            message=f'could not classify {album.title!r}, keeping Spotify\'s guess',
            context={'album': album.title, 'error': exc.code},
        ))
        return album

    if enriched.release_group_id is None:
        # The last resort this project swore off. With 2 differences that make
        # it defensible: 1) it fires ONLY when MusicBrainz is blind, and 2) it
        # announces itself. Without it, 'A Good Night for a Fistfight (Live at
        # Islington Assembly Hall)'. 18 live tracks MusicBrainz had never met .
        # Walked itno a canonical library through the front door.
        hinted = _LIVE_HINT.search(enriched.title)
        if hinted and SecondaryType.LIVE not in enriched.secondary_types:
            bus.emit(Warned(
                run_id=run_id,
                message=f'{album.title!r}: MusicBrainz is blind and the title '
                        f'says {hinted.group(0)!r}. Treating as live '
                        f'(a title guess, flagged as such)',
                context={'album': album.title, 'basis': 'title_fallback'},
            ))
            return replace(enriched,
                           secondary_types=enriched.secondary_types | {SecondaryType.LIVE})
        bus.emit(Warned(
            run_id=run_id,
            message=f'MusicBrainz has no record of {album.title!r}. '
                    f'its type is Spotify\'s guess, not a fact',
            context={'album': album.title, 'spotify_type': str(album.release_type)},
        ))
    return enriched
