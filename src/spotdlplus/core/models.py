'''
models.py - entity types and the state machine

An input resolves to an entity, entities expand into children, and the children
end up as Tracks. A single track download is the same shape with one leaf.

Durations are always integer milliseconds and sizes are always integer bytes.
Use floats and you end up with a 3.0000000004 minute track adn a dedupe pass
that can't tell two identical recordings apart.
'''

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar


class EntityKind(StrEnum):
    ARTIST = 'artist'
    ALBUM = 'album'
    PLAYLIST = 'playlist'
    TRACK = 'track'


class ReleaseType(StrEnum):
    '''
    What kind of record this is, straight from MusicBrainz instead of guessed off
    the title.
    '''

    ALBUM = 'album'
    SINGLE = 'single'
    EP = 'ep'
    BROADCAST = 'broadcast'
    OTHER = 'other'


class SecondaryType(StrEnum):
    '''Orthogonal flavors. A record can carry several at once.'''

    COMPILATION = 'compilation'
    LIVE = 'live'
    REMIX = 'remix'
    SOUNDTRACK = 'soundtrack'
    DJ_MIX = 'dj-mix'
    MIXTAPE = 'mixtape'
    DEMO = 'demo'
    SPOKENWORD = 'spokenword'
    INTERVIEW = 'interview'
    AUDIOBOOK = 'audiobook'
    KARAOKE = 'karaoke'


class MasterPreference(StrEnum):
    '''
    Which master survives when two recordings turn out to be the same work. It
    lives here instead of works.py because SelectionProfile needs it and works.py
    needs SelectionProfile.
    '''

    ORIGINAL = 'original'      # earliest release. The artifact as it shipped.
    REMASTER = 'remaster'      # latest release. Usually what streaming serves.
    POPULARITY = 'popularity'  # whichever the crowd plays more. Drifts over time.
    BOTH = 'both'              # collapse nothing. Honest, and larger.


class TrackState(StrEnum):
    '''
    Every state a track moves through. The store remembers where each one stopped
    so a dead run can pick back up instead of starting over.
    '''

    DISCOVERED = 'discovered'    # metadata exists, nothing else does
    ENRICHED = 'enriched'        # isrc + musicbrainz ids attached
    MATCHED = 'matched'          # an audio source was chosen, and we know why
    FETCHED = 'fetched'          # raw audio in the cache, unverified
    TRANSCODED = 'transcoded'    # target format in the cache
    TAGGED = 'tagged'            # metadata + art embedded
    PLACED = 'placed'            # atomically moved into the library
    DONE = 'done'                # verified. The only state that means "have it"
    SKIPPED = 'skipped'          # already had it, or you pruned it
    FAILED = 'failed'            # carries a code and a remedy


#: Legal forward transitions. Anything else is a bug and the store will say so.
TRANSITIONS: dict[TrackState, frozenset[TrackState]] = {
    TrackState.DISCOVERED: frozenset({TrackState.ENRICHED, TrackState.SKIPPED, TrackState.FAILED}),
    TrackState.ENRICHED: frozenset({TrackState.MATCHED, TrackState.SKIPPED, TrackState.FAILED}),
    TrackState.MATCHED: frozenset({TrackState.FETCHED, TrackState.SKIPPED, TrackState.FAILED}),
    TrackState.FETCHED: frozenset({TrackState.TRANSCODED, TrackState.FAILED}),
    TrackState.TRANSCODED: frozenset({TrackState.TAGGED, TrackState.FAILED}),
    TrackState.TAGGED: frozenset({TrackState.PLACED, TrackState.FAILED}),
    TrackState.PLACED: frozenset({TrackState.DONE, TrackState.FAILED}),
    TrackState.DONE: frozenset(),
    TrackState.SKIPPED: frozenset({TrackState.DISCOVERED}),   # un-prune and retry
    TrackState.FAILED: frozenset({TrackState.DISCOVERED, TrackState.ENRICHED,
                                  TrackState.MATCHED, TrackState.SKIPPED}),
}

#: States from whcih a resume should pick up work.
RESUMABLE: frozenset[TrackState] = frozenset({
    TrackState.DISCOVERED, TrackState.ENRICHED, TrackState.MATCHED,
    TrackState.FETCHED, TrackState.TRANSCODED, TrackState.TAGGED,
    TrackState.PLACED, TrackState.FAILED,
})


# ----------------------------------------------------------------------------
# normalization. The quiet workhorse of dedupe
# ----------------------------------------------------------------------------

_PAREN_NOISE = re.compile(
    r'\s*[\(\[]\s*(remaster(ed)?|deluxe|expanded|bonus|anniversary|edition|'
    r'mono|stereo|explicit|clean|radio edit|album version|single version|'
    r'\d{4}\s*remaster(ed)?|remaster(ed)?\s*\d{4})\b[^)\]]*[\)\]]',
    re.IGNORECASE,
)
_FEAT = re.compile(r'\s*[\(\[]?\s*(feat\.?|ft\.?|featuring)\s+[^)\]]*[\)\]]?', re.IGNORECASE)
_PUNCT = re.compile(r'[^\w\s]')
_SPACE = re.compile(r'\s+')


def normalize_title(title: str, *, strip_feat: bool = True) -> str:
    '''
    Folds a title down to what two releases would agree on. A 2011 Remaster and a
    - Remastered have to land on the same string or the deluxe edition ships you a
    duplicate.

    Does NOT strip live, remix, or acoustic. Those are a different recording and
    collapsing them loses data.
    '''
    s = unicodedata.normalize('NFKD', title)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = _PAREN_NOISE.sub(' ', s)
    if strip_feat:
        s = _FEAT.sub(' ', s)
    s = re.sub(r'\s*-\s*(remaster(ed)?|\d{4}\s*remaster(ed)?|mono|stereo)\b.*$', '', s, flags=re.I)
    s = _PUNCT.sub(' ', s)
    s = _SPACE.sub(' ', s)
    return s.strip().casefold()


def normalize_artist(name: str) -> str:
    '''Same idea, lighter touch. "The Beatles" and "Beatles" stay distinct.'''
    s = unicodedata.normalize('NFKD', name)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT.sub(' ', s)
    return _SPACE.sub(' ', s).strip().casefold()


# ----------------------------------------------------------------------------
# entities
# ----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ArtistRef:
    '''A pointer to an artist, as seen from a track or album credit.'''

    name: str
    spotify_id: str | None = None
    mb_artist_id: str | None = None

    @property
    def key(self) -> str:
        return self.mb_artist_id or self.spotify_id or normalize_artist(self.name)


@dataclass(frozen=True, slots=True)
class Artist:
    '''An artist and everything we could learn about them.'''

    ref: ArtistRef
    genres: tuple[str, ...] = ()
    popularity: int | None = None
    followers: int | None = None
    image_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def name(self) -> str:
        return self.ref.name


@dataclass(frozen=True, slots=True)
class Album:
    '''
    One release. The deluxe, the remaster, and the Japan press all share a
    release_group_id, and we collapse them down to one.
    '''

    title: str
    artists: tuple[ArtistRef, ...] = ()
    spotify_id: str | None = None
    mb_release_id: str | None = None
    release_group_id: str | None = None
    release_type: ReleaseType = ReleaseType.ALBUM
    secondary_types: frozenset[SecondaryType] = frozenset()
    release_date: str | None = None          # ISO, possibly partial: '1973', '1973-03'
    original_date: str | None = None         # the release-group's first date
    total_tracks: int | None = None
    total_discs: int | None = None
    label: str | None = None
    upc: str | None = None
    copyright: str | None = None
    cover_url: str | None = None
    is_appears_on: bool = False              # artist is a guest, not the owner
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def year(self) -> int | None:
        src = self.original_date or self.release_date
        if src and len(src) >= 4 and src[:4].isdigit():
            return int(src[:4])
        return None

    @property
    def cluster_key(self) -> str:
        '''What identifies "the same record" across editions.'''
        if self.release_group_id:
            return f'rg:{self.release_group_id}'
        artist = self.artists[0].key if self.artists else ''
        return f'ta:{artist}:{normalize_title(self.title)}'


@dataclass(frozen=True, slots=True)
class Track:
    '''
    A recording with every field we could get. spotdl didn't fetch most of this, so
    there is room for all of it and the enrich stage fills the gaps.
    '''

    title: str
    artists: tuple[ArtistRef, ...]
    album: Album | None = None

    # identity. In descending order of how much we trust it
    isrc: str | None = None
    mb_recording_id: str | None = None
    spotify_id: str | None = None

    duration_ms: int = 0
    track_no: int | None = None
    disc_no: int | None = None
    explicit: bool | None = None
    popularity: int | None = None
    genres: tuple[str, ...] = ()

    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def album_artist(self) -> str:
        if self.album and self.album.artists:
            return self.album.artists[0].name
        return self.artists[0].name if self.artists else 'Unknown Artist'

    @property
    def artist(self) -> str:
        return self.artists[0].name if self.artists else 'Unknown Artist'

    @property
    def artists_display(self) -> str:
        return ', '.join(a.name for a in self.artists) or 'Unknown Artist'

    @property
    def identity(self) -> str:
        '''
        The primary key. ISRC when we have it, since that's the recording's real name
        and it stays the same across every release it shows up on.
        '''
        if self.isrc:
            return f'isrc:{self.isrc.upper()}'
        if self.mb_recording_id:
            return f'mbid:{self.mb_recording_id}'
        if self.spotify_id:
            return f'sp:{self.spotify_id}'
        return f'fuzzy:{self.fuzzy_key}'

    @property
    def fuzzy_key(self) -> str:
        '''
        Last resort when there's no identifier at all. Duration gets bucketed to 2s so
        two rips of one recording match but a genuinely different cut doesn't.
        '''
        artist = self.artists[0].key if self.artists else ''
        bucket = self.duration_ms // 2000
        return f'{artist}:{normalize_title(self.title)}:{bucket}'


# ----------------------------------------------------------------------------
# matching
# ----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Candidate:
    '''One possible audio source for a Track, befoer we have judged it.'''

    source: str                    # 'ytmusic' | 'youtube' | ...
    source_id: str
    url: str
    title: str
    uploader: str = ''
    duration_ms: int = 0
    size_bytes: int | None = None  # this is what makes the size preview real
    is_topic_channel: bool = False # youtube auto-generated "Art Track", gold
    view_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True, slots=True)
class Fingerprint:
    '''
    A file's acoustic fingerprint and the duration it covers. It lives in core
    because media makes them and providers reads them, and imports only flow down.
    '''

    fingerprint: str
    duration_s: int


@dataclass(frozen=True, slots=True)
class MatchResult:
    '''
    Why we picked what we picked, including the ones we didn't. Once the scoreboard
    is on disk a bad match is something you can read instead of re-run.
    '''

    chosen: Candidate | None
    score: float
    basis: str                                    # 'isrc_exact' | 'scored'
    breakdown: dict[str, float] = field(default_factory=dict)
    #: (candidate, why it lost, its real score). The score used to be dropped
    #: here and every loser read back as 0.00 in the relink detail, a small
    #: lie.
    rejected: tuple[tuple[Candidate, str, float], ...] = ()
    runner_up_score: float | None = None

    #: Below this, we skip the track rather thna guess. A missing song is a
    #: nuisance. A wrong song that looks right is a corrupted library.
    CONFIDENCE_FLOOR: ClassVar[float] = 0.72

    @property
    def confident(self) -> bool:
        if self.chosen is None:
            return False
        return self.basis == 'isrc_exact' or self.score >= self.CONFIDENCE_FLOOR


# ----------------------------------------------------------------------------
# selection
# ----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SelectionProfile:
    '''
    What give-me-this-artist actually means. A discography isn't every audio file
    with the artist's name on it.
    '''

    name: str
    include_types: frozenset[ReleaseType]
    exclude_secondary: frozenset[SecondaryType]
    include_appears_on: bool = False
    dedupe_by_isrc: bool = True
    #: Tier 2. `BOTH` keeps every master. Anything else collapses reissues.
    master_preference: MasterPreference = MasterPreference.ORIGINAL


CANONICAL = SelectionProfile(
    name='canonical',
    include_types=frozenset({ReleaseType.ALBUM, ReleaseType.EP, ReleaseType.SINGLE}),
    exclude_secondary=frozenset({
        SecondaryType.COMPILATION, SecondaryType.LIVE, SecondaryType.DJ_MIX,
        SecondaryType.KARAOKE, SecondaryType.INTERVIEW, SecondaryType.AUDIOBOOK,
        SecondaryType.SPOKENWORD, SecondaryType.DEMO,
    }),
    include_appears_on=False,
    master_preference=MasterPreference.ORIGINAL,
)

COMPLETIONIST = SelectionProfile(
    name='completionist',
    include_types=frozenset(ReleaseType),
    exclude_secondary=frozenset({SecondaryType.KARAOKE}),
    include_appears_on=True,
    master_preference=MasterPreference.BOTH,
)

STUDIO_ONLY = SelectionProfile(
    name='studio',
    include_types=frozenset({ReleaseType.ALBUM}),
    exclude_secondary=frozenset(SecondaryType) - {SecondaryType.SOUNDTRACK},
    include_appears_on=False,
    master_preference=MasterPreference.ORIGINAL,
)

PROFILES: dict[str, SelectionProfile] = {
    p.name: p for p in (CANONICAL, COMPLETIONIST, STUDIO_ONLY)
}
