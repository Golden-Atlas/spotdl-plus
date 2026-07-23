'''
test_cookies.py - borrowing browser cookies is opt-in, validated, and actually
reaches the yt-dlp options.

Cookies clear age-gates and most bot walls, so this is the biggest reliability
lever for a big run, but it stays off unless asked, and a nonsense browser
name must fail loudly at config time, not deep in a download.
'''

from __future__ import annotations

import pytest

from spotdlplus.core.config import SUPPORTED_BROWSERS, load_config
from spotdlplus.core.errors import ConfigInvalid
from spotdlplus.net import ytenv


def cfg(tmp_path, **overrides):
    return load_config(project_dir=tmp_path,
                       user_config=tmp_path / 'none.toml',
                       env={}, overrides=overrides or None)


def test_default_is_no_cookies(tmp_path):
    c = cfg(tmp_path)
    assert c.youtube_cookies_from_browser is None
    assert c.youtube_cookiefile is None


def test_a_known_browser_is_accepted(tmp_path):
    c = cfg(tmp_path, youtube_cookies_from_browser='chrome')
    assert c.youtube_cookies_from_browser == 'chrome'


def test_case_insensitive(tmp_path):
    c = cfg(tmp_path, youtube_cookies_from_browser='Firefox')
    assert c.youtube_cookies_from_browser == 'Firefox'   # stored as-given
    c.validate()                                         # ...but validates fine


def test_a_nonsense_browser_is_rejected(tmp_path):
    with pytest.raises(ConfigInvalid, match='youtube_cookies_from_browser'):
        cfg(tmp_path, youtube_cookies_from_browser='netscape')


def test_from_env(tmp_path):
    c = load_config(project_dir=tmp_path, user_config=tmp_path / 'none.toml',
                    env={'SPOTDLPLUS_COOKIES_FROM_BROWSER': 'edge'})
    assert c.youtube_cookies_from_browser == 'edge'


# ----------------------------------------------------------------------------
# the option actually reaches yt-dlp
# ----------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_cookie_source():
    '''Never let one test's cookie source leak into the next (module state).'''
    ytenv.set_cookie_source(browser=None, cookiefile=None)
    yield
    ytenv.set_cookie_source(browser=None, cookiefile=None)


def test_no_cookie_source_means_no_cookie_opts():
    opts = ytenv.ydl_base_opts()
    assert 'cookiesfrombrowser' not in opts and 'cookiefile' not in opts


def test_browser_reaches_ydl_opts_as_a_tuple():
    ytenv.set_cookie_source(browser='chrome')
    opts = ytenv.ydl_base_opts()
    assert opts['cookiesfrombrowser'] == ('chrome', None, None, None)


def test_cookiefile_reaches_ydl_opts():
    ytenv.set_cookie_source(cookiefile='C:/cookies.txt')
    assert ytenv.ydl_base_opts()['cookiefile'] == 'C:/cookies.txt'


def test_browser_is_lowercased_at_the_source():
    ytenv.set_cookie_source(browser='EDGE')
    assert ytenv.ydl_base_opts()['cookiesfrombrowser'][0] == 'edge'


def test_the_supported_list_is_what_the_wizard_offers():
    # a light guard that the two stay in the same universe
    assert 'chrome' in SUPPORTED_BROWSERS and 'firefox' in SUPPORTED_BROWSERS


# ----------------------------------------------------------------------------
# the wizard writes it
# ----------------------------------------------------------------------------

def test_wizard_write_config_persists_the_browser(tmp_path, monkeypatch):
    from spotdlplus.cli import setup as setup_mod
    monkeypatch.setattr(setup_mod, 'config_path', lambda: tmp_path / 'config.toml')
    setup_mod.write_config(spotify_client_id='a', spotify_client_secret='b',
                           youtube_cookies_from_browser='firefox')
    text = (tmp_path / 'config.toml').read_text()
    assert 'youtube_cookies_from_browser = "firefox"' in text
