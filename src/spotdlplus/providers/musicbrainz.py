'''
musicbrainz.py - release classification

Spotify has one field for this, `album_type`, and it reports live albums,
compilations, and remix collections all as `album`. MusicBrainz release-groups
carry primary and secondary types, and that's the difference between a
discography and a pile.

There are two lookup paths, exact by UPC barcode and a search fallback.
Everything is paced under one request per second because their terms say one
adn they mean it.
'''

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..core.errors import EntityNotFound, ProviderFailed
from ..core.models import Album, ReleaseType, SecondaryType, normalize_title
from ..net.http import HttpClient

WS = 'https://musicbrainz.org/ws/2'

_PRIMARY: dict[str, ReleaseType] = {
    'album': ReleaseType.ALBUM,
    'single': ReleaseType.SINGLE,
    'ep': ReleaseType.EP,
    'broadcast': ReleaseType.BROADCAST,
    'other': ReleaseType.OTHER,
}

#: MusicBrainz's vocabulary, mapped onto ours. Anything unrecognised is dropped
#: rather than guessed at. An unknown secondary type must never silently become
#: a known one and change what gets excluded.
_SECONDARY: dict[str, SecondaryType] = {
    'compilation': SecondaryType.COMPILATION,
    'live': SecondaryType.LIVE,
    'remix': SecondaryType.REMIX,
    'soundtrack': SecondaryType.SOUNDTRACK,
    'dj-mix': SecondaryType.DJ_MIX,
    'mixtape/street': SecondaryType.MIXTAPE,
    'demo': SecondaryType.DEMO,
    'spokenword': SecondaryType.SPOKENWORD,
    'interview': SecondaryType.INTERVIEW,
    'audiobook': SecondaryType.AUDIOBOOK,
}

#: The secondary types MusicBrainz can actually tell us about. There's no
#: KARAOKE in their vocabulary, so no album will ever carry it and a profile
#: excluding it is asking for a fact nobody can supply.
DERIVABLE_SECONDARY_TYPES: frozenset[SecondaryType] = frozenset(_SECONDARY.values())

#: Lucene reserves these. An unescaped `:` in an album title turns a search into a
#: syntax error, and a `!` turns it into a different query than you meant.
_LUCENE = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')

#: Below this, MusicBrainz's own relevance score is telling us it guessed.
MIN_SEARCH_SCORE = 80


def _escape(text: str) -> str:
    return _LUCENE.sub(r'\\\1', text)


@dataclass(frozen=True, slots=True)
class ReleaseGroupInfo:
    '''The abstract record, of which your deluxe edition is merely one printing.'''

    mbid: str
    title: str
    primary: ReleaseType
    secondary: frozenset[SecondaryType]
    first_release_date: str | None
    score: int | None = None

    @property
    def is_live(self) -> bool:
        return SecondaryType.LIVE in self.secondary

    @property
    def is_compilation(self) -> bool:
        return SecondaryType.COMPILATION in self.secondary


@dataclass(frozen=True, slots=True)
class RecordingInfo:
    mbid: str
    title: str
    length_ms: int | None


def _to_release_group(d: dict[str, Any]) -> ReleaseGroupInfo:
    primary = _PRIMARY.get((d.get('primary-type') or '').lower(), ReleaseType.OTHER)
    secondary = frozenset(
        _SECONDARY[s.lower()]
        for s in (d.get('secondary-types') or [])
        if s.lower() in _SECONDARY
    )
    return ReleaseGroupInfo(
        mbid=d['id'],
        title=d.get('title') or '',
        primary=primary,
        secondary=secondary,
        first_release_date=d.get('first-release-date') or None,
        score=d.get('score'),
    )


class MusicBrainzProvider:
    '''
    Enrichment, so best-effort by design. A missing release-group returns None and
    never raises. Not knowing what kind of record something is should never fail a
    track, it just means the profile decides on Spotify's word.
    '''

    def __init__(self, http: HttpClient, *, min_score: int = MIN_SEARCH_SCORE) -> None:
        self._http = http
        self._min_score = min_score

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        params['fmt'] = 'json'
        clean = {k: v for k, v in params.items() if v is not None}
        return self._http.get(f'{WS}{path}', params=clean).json()

    # -- release groups ------------------------------------------------------

    def release_group_by_barcode(self, upc: str) -> ReleaseGroupInfo | None:
        '''
        The exact path. A UPC identifies one printing and that printing belongs to one
        release-group, so there's nothing to guess at. Spotify hands us the UPC on
        every full album object, which is why we re-fetch them.
        '''
        try:
            body = self._get('/release', query=f'barcode:{_escape(upc)}', limit=1)
        except EntityNotFound:
            return None

        for release in body.get('releases') or []:
            rg = release.get('release-group')
            if rg and rg.get('id'):
                return _to_release_group(rg)
        return None

    def release_group_by_search(self, artist: str, title: str) -> ReleaseGroupInfo | None:
        '''
        The inexact path, and the one that taught me something.

        A quoted phrase query can't work here. Spotify writes 'Hail to the Thief (Live
        Recordings 2003-2009)' and MusicBrainz writes 'Hail to the Thief: Live
        Recordings...'. Punctuation drifts between catalogues, so we search unquoted
        and normalized and let the scoring sort it out.
        '''
        want = normalize_title(title)
        if not want or not artist:
            return None

        # `want` has already had punctuation stripped, so it carries no Lucene
        # operators. The artist name has not.
        query = f'artist:"{_escape(artist)}" AND releasegroup:({want})'
        try:
            body = self._get('/release-group', query=query, limit=10)
        except EntityNotFound:
            return None

        for d in body.get('release-groups') or []:
            if (d.get('score') or 0) < self._min_score:
                continue
            if normalize_title(d.get('title') or '') == want:
                return _to_release_group(d)
        return None

    def release_group_for_album(self, album: Album) -> ReleaseGroupInfo | None:
        '''UPC first, because it cannot be wrong. Search only as a fallback.'''
        if album.upc:
            found = self.release_group_by_barcode(album.upc)
            if found is not None:
                return found
        artist = album.artists[0].name if album.artists else ''
        if not artist or not album.title:
            return None
        return self.release_group_by_search(artist, album.title)

    # -- the useful verb -----------------------------------------------------

    def enrich_album(self, album: Album) -> Album:
        '''
        Returns the album with its real type attached, or unchanged when MusicBrainz
        has never heard of it. Enrichment that fails should leave you no worse off than
        never trying.
        '''
        try:
            rg = self.release_group_for_album(album)
        except ProviderFailed:
            return album
        if rg is None:
            return album

        from dataclasses import replace
        return replace(
            album,
            release_group_id=rg.mbid,
            release_type=rg.primary,
            secondary_types=album.secondary_types | rg.secondary,
            original_date=rg.first_release_date or album.original_date,
        )

    # -- recordings ----------------------------------------------------------

    def recording_by_isrc(self, isrc: str) -> RecordingInfo | None:
        '''
        Exact, and deliberately not in the default pipeline. At one request per second
        a 500 track discography costs eight minutes for a field nothing reads yet. It's
        here for `relink` and for later.
        '''
        try:
            body = self._get(f'/isrc/{isrc.upper()}')
        except EntityNotFound:
            return None

        for rec in body.get('recordings') or []:
            if rec.get('id'):
                length = rec.get('length')
                return RecordingInfo(
                    mbid=rec['id'],
                    title=rec.get('title') or '',
                    length_ms=int(length) if length else None,
                )
        return None
