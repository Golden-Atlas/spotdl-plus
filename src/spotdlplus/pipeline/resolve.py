'''
resolve.py - turning input into one entity

Takes a Spotify URL, a spotify: URI, a prefixed query liek `artist:Duster`, or
just a bare string. One entity comes out.

The bare string is the interesting part. It gets scored across artists, albums,
and tracks, and when two readings tie we ask instead of guessing. Guessing here
means downloading the wrong discography, which is a lot of bytes to apologize
for.
'''

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from ..core.errors import AmbiguousQuery, EntityNotFound
from ..core.models import EntityKind, normalize_artist, normalize_title
from ..providers.spotify import SpotifyProvider, parse_spotify_ref

#: Popularity is the tiebreak when names are equally good. `Creep` the song is far
#: more popular than any artist named Creep. `Radiohead` the artist far outranks
#: the Talking Heads song of taht name. Weighted so it decides ties, not
#: matches.
POPULARITY_WEIGHT = 0.30

#: Below this, the names simply don't agree and the candidate isn't a reading.
MIN_SIMILARITY = 0.60

#: If the best two readings are closer than this, we refuse to choose for you.
AMBIGUITY_MARGIN = 0.06

#: When the query names an artist ('Creep Radiohead') and a track/album candidate
#: is BY that artist, nudge it up. Enough to beat a more-popular cover by someone
#: else, not enough to override a genuinely better title match on its own.
ARTIST_IN_QUERY_BONUS = 0.25

_PREFIX = re.compile(r'^\s*(artist|album|track|playlist|song)\s*:\s*(.+)$', re.IGNORECASE)
_PREFIX_KINDS = {
    'artist': EntityKind.ARTIST,
    'album': EntityKind.ALBUM,
    'track': EntityKind.TRACK,
    'song': EntityKind.TRACK,
    'playlist': EntityKind.PLAYLIST,
}

#: Searched when a bare string arrives. Playlists are excluded on purpose: a bare
#: word matches thousands of user playlists named after it, adn none of them is
#: what you meant.
_SEARCHABLE = (EntityKind.ARTIST, EntityKind.ALBUM, EntityKind.TRACK)


@dataclass(frozen=True, slots=True)
class Resolution:
    '''Exactly one entity, and how we came to believe in it.'''

    kind: EntityKind
    spotify_id: str
    name: str
    detail: str = ''          # 'Radiohead' for an album, a track count for a playlist
    confidence: float = 1.0   # 1.0 when you handed us a URL and there was nothing to infer
    basis: str = 'url'        # 'url' | 'prefix' | 'scored' | 'picked'

    @property
    def label(self) -> str:
        # Parens, not a dash or a period. 'In Rainbows. Radiohead' reads like a
        # botched sentence, and this string goes straight into the ambiguity
        # error a person has to choose from.
        return f'{self.name} ({self.detail})' if self.detail else self.name


@dataclass(frozen=True, slots=True)
class ResolutionCandidate:
    '''One plausible reading of a bare string, with the arithmetic taht ranked it.'''

    kind: EntityKind
    spotify_id: str
    name: str
    detail: str
    similarity: float
    popularity: int
    score: float

    @property
    def label(self) -> str:
        return f'{self.kind}: {self.name}' + (f' ({self.detail})' if self.detail else '')


#: Given the ranked candidates, return the index of the one you want, or None to
#: give up. The CLI supplies an interactive picker. A script supplies nothing and
#: gets AmbiguousQuery instead of a silent wrong answer.
Picker = Callable[[Sequence[ResolutionCandidate]], int | None]


def _similarity(query: str, name: str, kind: EntityKind) -> float:
    norm = normalize_artist if kind is EntityKind.ARTIST else normalize_title
    return fuzz.ratio(norm(query), norm(name)) / 100.0


def _artists_of(item: dict[str, Any]) -> str:
    return ', '.join(a.get('name', '') for a in item.get('artists') or [])


def _artist_named_in_query(query: str, item: dict[str, Any]) -> bool:
    '''
    Checks whether you wrote this candidate's artist into the query alongside a
    title. Whole-word and space-padded so 'air' inside 'fair' can't score, and it
    needs something left over afterward. Without that a bare artist name matches
    itself and 'Ethel Cain' resolves to a track called Crush.
    '''
    haystack = f' {query.lower()} '
    for artist in item.get('artists') or []:
        needle = (artist.get('name') or '').lower().strip()
        if needle and f' {needle} ' in haystack:
            remainder = haystack.replace(f' {needle} ', ' ', 1).strip()
            if remainder:                     # there's a title, not just the artist
                return True
    return False


def _candidate(query: str, kind: EntityKind, item: dict[str, Any], popularity: int) -> ResolutionCandidate:
    name = item.get('name') or ''
    sim = _similarity(query, name, kind)
    score = sim + POPULARITY_WEIGHT * (popularity / 100.0)

    # Only fires when you actually named the artist, so bare queries behave
    # exactly as before. The real 'Creep' matches 'Creep Radiohead' worse than a
    # cover titled 'Creep (Radiohead cover)' does. Scoring against 'title artist'
    # rescues it and the bonus tips it past the more-popular cover.
    if kind is not EntityKind.ARTIST and _artist_named_in_query(query, item):
        artists = _artists_of(item)
        sim = max(sim, _similarity(query, f'{name} {artists}', kind))
        score = sim + POPULARITY_WEIGHT * (popularity / 100.0) + ARTIST_IN_QUERY_BONUS

    return ResolutionCandidate(
        kind=kind,
        spotify_id=item.get('id') or '',
        name=name,
        detail='' if kind is EntityKind.ARTIST else _artists_of(item),
        similarity=sim,
        popularity=popularity,
        score=score,
    )


def candidates(query: str, sp: SpotifyProvider, *, limit: int = 3) -> list[ResolutionCandidate]:
    '''
    Returns every plausible reading of a bare string, best first. Album results
    come back with no popularity attached so they get one batched re-fetch, and
    without it an album can never win a tie it deserves.
    '''
    found: list[ResolutionCandidate] = []

    for kind in _SEARCHABLE:
        try:
            items = sp.search(query, kind, limit=limit)
        except EntityNotFound:
            continue

        if kind is EntityKind.ALBUM:
            ids = [i['id'] for i in items if i.get('id')]
            popularity = {a.spotify_id: (a.raw.get('popularity') or 0) for a in sp.albums(ids)}
            found += [_candidate(query, kind, i, popularity.get(i.get('id'), 0)) for i in items]
        else:
            found += [_candidate(query, kind, i, i.get('popularity') or 0) for i in items]

    plausible = [c for c in found if c.similarity >= MIN_SIMILARITY]
    plausible.sort(key=lambda c: (-c.score, str(c.kind), c.spotify_id))
    return plausible


def _from_reference(kind: EntityKind, entity_id: str, sp: SpotifyProvider) -> Resolution:
    '''A URL isn't a guess. We fetch the name so we can show you what you asked for.'''
    match kind:
        case EntityKind.ARTIST:
            a = sp.artist(entity_id)
            return Resolution(kind, entity_id, a.name)
        case EntityKind.ALBUM:
            al = sp.album(entity_id)
            detail = al.artists[0].name if al.artists else ''
            return Resolution(kind, entity_id, al.title, detail)
        case EntityKind.TRACK:
            t = sp.track(entity_id)
            return Resolution(kind, entity_id, t.title, t.artists_display)
        case EntityKind.PLAYLIST:
            p = sp.playlist(entity_id)
            total = (p.get('tracks') or {}).get('total')
            owner = (p.get('owner') or {}).get('display_name') or ''
            # Spotify can hand back a playlist with no track total on it. This
            # used to interpolate it anyway, so somebody's first run greeted
            # them with '(None tracks, by ...)'. Say nothing instead.
            bits = [f'{total} tracks'] if total is not None else []
            if owner:
                bits.append(f'by {owner}')
            detail = ', '.join(bits)
            return Resolution(kind, entity_id, p.get('name') or 'Playlist', detail)
        case _:  # pragma: no cover
            raise EntityNotFound(f'cannot resolve a {kind}', context={'id': entity_id})


def resolve(query: str, sp: SpotifyProvider, *, pick: Picker | None = None) -> Resolution:
    '''
    Turns anything into exactly one entity. Raises AmbiguousQuery when a bare
    string has two equally good readings and nothing was passed to pick between
    them, since a script that can't ask should fail loudly instead of choosing for
    you.
    '''
    text = query.strip()
    if not text:
        raise EntityNotFound('nothing to resolve', context={'query': query})

    ref = parse_spotify_ref(text)
    if ref is not None:
        return _from_reference(ref[0], ref[1], sp)

    prefixed = _PREFIX.match(text)
    if prefixed:
        kind = _PREFIX_KINDS[prefixed.group(1).lower()]
        term = prefixed.group(2).strip()
        if kind is EntityKind.PLAYLIST:
            raise EntityNotFound(
                'playlists cannot be searched by name. paste the playlist URL',
                context={'query': term},
            )
        # A prefix pins the kind, not the answer. This path once took teh top
        # scoring hit with no floor and no tie guard, so `song:Sunsetz
        # Cigarettes After Sex` resolved to Oliver Tree.
        ranked = [
            c for c in sorted(
                (_candidate(term, kind, i, i.get('popularity') or 0)
                 for i in sp.search(term, kind)),
                key=lambda c: -c.score,
            )
            if c.similarity >= MIN_SIMILARITY
        ]
        if not ranked:
            raise EntityNotFound(
                f'nothing on Spotify resembles {term!r} as a {kind}. If the '
                f'query mixes title and artist, try quoting just the title.',
                context={'query': term, 'kind': str(kind)},
            )
        return _decide(ranked, text, pick, basis='prefix')

    ranked = candidates(text, sp)
    if not ranked:
        raise EntityNotFound(
            f'nothing on Spotify resembles {text!r}',
            context={'query': text},
        )
    return _decide(ranked, text, pick, basis='scored')


def _decide(
    ranked: list[ResolutionCandidate],
    query: str,
    pick: Picker | None,
    *,
    basis: str,
) -> Resolution:
    '''The one arbiter for "which reading wins", shared by every search path.'''
    top = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    too_close = runner_up is not None and (top.score - runner_up.score) < AMBIGUITY_MARGIN

    if too_close:
        if pick is None:
            raise AmbiguousQuery(
                f'{query!r} could be {top.label} or {runner_up.label}',
                context={
                    'query': query,
                    'candidates': [c.label for c in ranked[:5]],
                    'scores': [round(c.score, 3) for c in ranked[:5]],
                },
            )
        chosen = pick(ranked)
        if chosen is None:
            raise EntityNotFound('nothing chosen', context={'query': query})
        c = ranked[chosen]
        return Resolution(c.kind, c.spotify_id, c.name, c.detail,
                          confidence=c.similarity, basis='picked')

    return Resolution(top.kind, top.spotify_id, top.name, top.detail,
                      confidence=top.similarity, basis=basis)
