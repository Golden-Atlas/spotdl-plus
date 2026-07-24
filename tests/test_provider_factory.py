'''
test_provider_factory.py - which sign-in a config resolves to.

The rule that matters: a key you set is always honoured, and the default first
run needs none. Pinned as a pure function of the config, plus a check that each
branch actually builds the right provider type. No network: construction is
lazy, so this stays hermetic over a mock transport.
'''
from __future__ import annotations

import httpx
import pytest

from spotdlplus.core.config import Config
from spotdlplus.core.errors import ConfigInvalid
from spotdlplus.net.http import HttpClient
from spotdlplus.providers.factory import build_spotify_provider, use_web_path
from spotdlplus.providers.spotify import SpotifyProvider
from spotdlplus.providers.spotify_web import SpotifyWebProvider

KEYED = {'spotify_client_id': 'id', 'spotify_client_secret': 'secret'}


def _http() -> HttpClient:
    return HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))


# ----------------------------------------------------------------------------
# use_web_path: the whole decision, as a pure function
# ----------------------------------------------------------------------------

def test_auto_uses_your_key_when_you_set_one():
    assert use_web_path(Config(**KEYED)) is False


def test_auto_stays_on_the_app_path_for_now():
    # The free path is opt-in in 1.2.x. 'auto' with no key still prompts for one;
    # the fall-to-web switch is 1.3.0's.
    assert use_web_path(Config()) is False


def test_anon_forces_web_even_with_a_key():
    assert use_web_path(Config(spotify_auth='anon', **KEYED)) is True


def test_app_forces_your_key_even_with_none_set():
    assert use_web_path(Config(spotify_auth='app')) is False


# ----------------------------------------------------------------------------
# build_spotify_provider: the right object comes out of each branch
# ----------------------------------------------------------------------------

def test_auto_with_a_key_builds_the_app_provider():
    http = _http()
    try:
        assert isinstance(build_spotify_provider(Config(**KEYED), http), SpotifyProvider)
    finally:
        http.close()


def test_auto_without_a_key_still_builds_the_app_provider():
    # Opt-in only: 'auto' never silently switches to the web provider in 1.2.x.
    http = _http()
    try:
        assert isinstance(build_spotify_provider(Config(), http), SpotifyProvider)
    finally:
        http.close()


def test_anon_builds_the_web_provider_even_with_a_key():
    http = _http()
    try:
        provider = build_spotify_provider(Config(spotify_auth='anon', **KEYED), http)
        assert isinstance(provider, SpotifyWebProvider)
    finally:
        http.close()


def test_app_builds_the_app_provider():
    http = _http()
    try:
        assert isinstance(build_spotify_provider(Config(spotify_auth='app', **KEYED), http), SpotifyProvider)
    finally:
        http.close()


# ----------------------------------------------------------------------------
# a typo fails loud, before it can silently route the wrong way
# ----------------------------------------------------------------------------

def test_an_unknown_spotify_auth_is_rejected():
    # validate() is what load_config runs; a typo in config.toml fails there.
    with pytest.raises(ConfigInvalid):
        Config(spotify_auth='freemium').validate()
