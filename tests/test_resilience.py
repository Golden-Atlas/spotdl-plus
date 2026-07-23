'''
test_resilience.py - the 1.1.1 defenses against a friend's messy machine.

Every case here is a real thing that broke on someone else's computer: a paste
that smuggled control characters into the config, a locked cookie store, a raw
yt-dlp error that needed translating. The point is that none of them should ever
again dump a traceback or flood the terminal.
'''

from __future__ import annotations

import tomllib

from spotdlplus.core import errors as E


# ----------------------------------------------------------------------------
# setup: a paste can never corrupt the config file
# ----------------------------------------------------------------------------

def test_clean_input_strips_control_chars_but_keeps_spaces():
    from spotdlplus.cli.setup import clean_input
    # the exact shape that bricked a config: secret + a run of DEL + secret
    dirty = '3e8584d4' + '\x7f' * 20 + '3e8584d4'
    assert clean_input(dirty) == '3e8584d43e8584d4'
    assert clean_input('  spaced value  ') == 'spaced value'   # inner space kept
    assert clean_input('plain') == 'plain'


def test_setup_wizard_writes_a_valid_config_from_a_dirty_paste(tmp_path, monkeypatch):
    # The whole first-time story: a person pastes a secret that smuggled control
    # characters, and the wizard still writes a clean, parseable config.
    import spotdlplus.cli.setup as setup
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))

    # Fake credentials, deliberately: a real key in a test file is a real key
    # on the internet the day the repo goes public. Shape is all that matters.
    answers = iter([
        '0' * 32,                                 # Client ID
        'sekret1234' + '\x7f' * 8,                # Client Secret + paste garbage
        str(tmp_path / 'lib'),                    # output dir
    ])
    monkeypatch.setattr(setup.typer, 'prompt',
                        lambda *a, **k: 'chrome' if (a and 'Browser' in a[0]) else next(answers))
    monkeypatch.setattr(setup.typer, 'confirm', lambda *a, **k: False)   # iPod? no
    monkeypatch.setattr(setup.webbrowser, 'open', lambda *a, **k: True)

    assert setup.run_setup_wizard() is True

    written = tmp_path / 'spotdlplus' / 'config' / 'config.toml'
    data = tomllib.loads(written.read_text(encoding='utf-8'))   # raises if corrupt
    assert data['spotify_client_secret'] == 'sekret1234'        # DEL bytes gone
    assert data['youtube_cookies_from_browser'] == 'chrome'
    assert data['delivery'] == 'archive'


def test_dump_toml_always_emits_parseable_toml_even_from_dirty_values():
    from spotdlplus.cli.setup import _dump_toml
    data = {
        'spotify_client_secret': 'abc\x7f\x00def',   # control chars
        'output_dir': r'C:\Users\kai\Music',          # backslashes
        'note': 'he said "hi"',                        # quotes
    }
    text = _dump_toml(data)
    parsed = tomllib.loads(text)   # would raise if we emitted junk
    assert parsed['spotify_client_secret'] == 'abcdef'
    assert parsed['output_dir'] == r'C:\Users\kai\Music'
    assert parsed['note'] == 'he said "hi"'


def test_dump_toml_writes_nested_profile_tables():
    from spotdlplus.cli.setup import _dump_toml
    text = _dump_toml({
        'output_dir': 'D:/Music',
        'export_profiles': {
            'car-usb': {'to': 'universal', 'dest': r'E:\music'},
            'ipod': {'to': 'ipod'},
        },
    })
    parsed = tomllib.loads(text)
    assert parsed['export_profiles']['car-usb'] == {'to': 'universal',
                                                    'dest': r'E:\music'}
    assert parsed['export_profiles']['ipod'] == {'to': 'ipod'}
    assert parsed['output_dir'] == 'D:/Music', 'flat keys still come out flat'


def test_config_round_trips_export_profiles(tmp_path, monkeypatch):
    from spotdlplus.cli.setup import write_config
    from spotdlplus.core.config import load_config
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    write_config(spotify_client_id='x', spotify_client_secret='y',
                 export_profiles={'car-usb': {'to': 'universal', 'dest': 'E:/m'}})
    cfg = load_config(project_dir=tmp_path)   # no stray project toml
    assert cfg.export_profiles['car-usb']['dest'] == 'E:/m'


# ----------------------------------------------------------------------------
# the shared download-error classifier (fetch + doctor speak the same language)
# ----------------------------------------------------------------------------

def test_classify_download_error_maps_the_real_cases():
    from spotdlplus.media.fetch import classify_download_error
    blocked = classify_download_error(Exception('Sign in to confirm you are not a bot'), 'u')
    assert isinstance(blocked, E.SourceBlocked)
    cookies = classify_download_error(Exception('could not copy chrome cookie database'), 'u')
    assert isinstance(cookies, E.CookiesUnreadable)
    other = classify_download_error(Exception('HTTP Error 500'), 'u')
    assert isinstance(other, E.DownloadFailed)


# ----------------------------------------------------------------------------
# cookie preflight: no configured source is trivially fine
# ----------------------------------------------------------------------------

def test_cookie_detail_survives_ansi_colored_error_prefixes(monkeypatch):
    # On a real terminal yt-dlp COLORS its 'ERROR:' prefix. The escape codes
    # defeated a plain startswith for two releases while every piped test
    # looked clean. Never again.
    from spotdlplus.net import ytenv

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            raise RuntimeError(
                '\x1b[0;31mERROR:\x1b[0m ERROR: Could not copy Chrome cookie '
                'database. See https://github.com/yt-dlp/yt-dlp/issues/7271 '
                'for more info')

        def __exit__(self, *a):
            return False

    import yt_dlp
    monkeypatch.setattr(yt_dlp, 'YoutubeDL', FakeYDL)
    ytenv.set_cookie_source(browser='chrome')
    try:
        ok, detail = ytenv.cookie_source_readable()
    finally:
        ytenv.set_cookie_source(browser=None, cookiefile=None)
    assert not ok
    assert detail == 'Could not copy Chrome cookie database', detail


def test_cookie_preflight_is_ok_when_nothing_is_configured():
    from spotdlplus.net import ytenv
    ytenv.set_cookie_source(browser=None, cookiefile=None)
    ok, detail = ytenv.cookie_source_readable()
    assert ok and detail == ''
