'''
factory.py - choosing how to reach Spotify.

Two sign-ins, one interface. The app path (your own key) is unchanged and stays
the way to full ISRC dedupe. The web path signs in the way open.spotify.com
does, needs no account and no Premium, and is what you get when you haven't
handed over a key. Everything downstream holds either one, because
SpotifyWebProvider mirrors SpotifyProvider's surface exactly.

    auto  your key if you set one, otherwise the free web sign-in
    app   force your key (and prompt for one if it's missing)
    anon  force the free web sign-in, ignore any key

The rule lives here, alone, so `_wire` and `doctor` can't drift on which path a
config resolves to. It's a pure function of the config: no network, no I/O.
'''

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.events import EventBus
from ..net.http import HttpClient
from .spotify import SpotifyProvider, spotify_token_provider
from .spotify_web import SpotifyWebProvider

if TYPE_CHECKING:
    from ..core.config import Config

__all__ = ['build_spotify_provider', 'use_web_path']


def use_web_path(cfg: Config) -> bool:
    '''
    Whether this config resolves to the free web sign-in. Only 'anon' does for
    now, so the free path is opt-in. 'auto' and 'app' both use your own key, and
    'auto' still prompts for one on first run exactly as 1.2.0 did. The switch
    that makes 'auto' fall to the free path when you have no key is 1.3.0's, once
    that path is the documented default instead of a thing you turn on.
    '''
    return cfg.spotify_auth == 'anon'


def build_spotify_provider(
    cfg: Config,
    http: HttpClient,
    *,
    bus: EventBus | None = None,
) -> SpotifyProvider | SpotifyWebProvider:
    '''
    Assemble the metadata provider this config asks for. The app branch is the
    exact wiring 1.2 shipped. The web branch mints the anonymous token
    (WebPlayerAuth) and reads over the pathfinder GraphQL API, with the
    persisted-query hashes self-healing off the live bundle when one rots.
    '''
    if use_web_path(cfg):
        # Imported here so the common app path never pays to load the web stack.
        from ..net.spotify_pathfinder import PathfinderClient
        from ..net.spotify_web import WebPlayerAuth
        auth = WebPlayerAuth(autofetch=cfg.spotify_secret_autofetch, bus=bus)
        pathfinder = PathfinderClient(http, auth, refresher=auth.latest_query_hashes)
        return SpotifyWebProvider(pathfinder)

    auth = spotify_token_provider(http, cfg.spotify_client_id, cfg.spotify_client_secret, bus=bus)
    return SpotifyProvider(http, auth)
