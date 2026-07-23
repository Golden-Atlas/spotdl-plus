'''
providers. Where metadata comes from.

Spotify resolves what you asked for. MusicBrainz says what kind of record it is
adn clusters its editions. Deezer fills in ISRCs that Spotify withheld.

May import core and net. Never blocks, never retries, never sleeps. A provider
just raises a typed error and the Engine decides what that means.
'''

from .musicbrainz import MusicBrainzProvider, RecordingInfo, ReleaseGroupInfo
from .spotify import SpotifyProvider, parse_spotify_ref, spotify_token_provider

__all__ = [
    'MusicBrainzProvider', 'RecordingInfo', 'ReleaseGroupInfo',
    'SpotifyProvider', 'parse_spotify_ref', 'spotify_token_provider',
]
