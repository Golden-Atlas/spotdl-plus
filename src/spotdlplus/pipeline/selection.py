'''
selection.py - what counts as part of a discography

A discography isn't every audio file with the artist's name on it. It's
records, plus forty things that aren't records: live albums, compilations,
karaoke, DJ mixes, and every feature where they were a guest.

Spotify can't tell you which is which, it calls them all `album`. MusicBrainz
release-groups can, so every exclusion is made on a fact adn every one is
announced.
'''

from __future__ import annotations

from ..core.models import Album, ReleaseType, SecondaryType, SelectionProfile
from ..providers.musicbrainz import DERIVABLE_SECONDARY_TYPES


def exclusion_reason(album: Album, profile: SelectionProfile) -> str | None:
    '''
    Says why this release doesn't belong, or None if it does. The string rides
    along on a ReleaseFiltered event and ends up in front of you, because 'where
    did the live album go' deserves an answer.
    '''
    if album.is_appears_on and not profile.include_appears_on:
        return 'appears_on'

    if album.release_type not in profile.include_types:
        return f'type:{album.release_type}'

    unwanted = album.secondary_types & profile.exclude_secondary
    if unwanted:
        return 'secondary:' + ','.join(sorted(str(s) for s in unwanted))

    return None


def needs_enrichment(profile: SelectionProfile) -> bool:
    '''
    Checks whether this profile's answer actually depends on MusicBrainz. They
    allow one request per second, so enriching a 60 release discography costs a
    minute, and that minute is wasted if the profile would keep everything anyway.
    '''
    if profile.exclude_secondary & DERIVABLE_SECONDARY_TYPES:
        return True
    return profile.include_types != frozenset(ReleaseType)


def spotify_album_groups(profile: SelectionProfile) -> tuple[str, ...]:
    '''
    Picks which of Spotify's include_groups are worth asking for. Spotify has no
    idea what an EP is, they show up as album or single and only MusicBrainz knows
    which, so wanting EPs means fetching both.
    '''
    groups: list[str] = []
    wants_album_shaped = bool(profile.include_types & {ReleaseType.ALBUM, ReleaseType.EP})
    wants_single_shaped = bool(profile.include_types & {ReleaseType.SINGLE, ReleaseType.EP})

    if wants_album_shaped:
        groups.append('album')
    if wants_single_shaped:
        groups.append('single')
    if SecondaryType.COMPILATION not in profile.exclude_secondary:
        groups.append('compilation')
    if profile.include_appears_on:
        groups.append('appears_on')

    return tuple(groups) or ('album',)
