'''
setup.py - the first run wizard

Everyone needs their own free Spotify credentials and there's no way around
that, so the job here is making it painless. Open the dashboard, walk through
the three clicks, take what they paste, write it to config.

It fires on its own the first time a command needs the network and finds no
credentials, but only on a real terminal. Scripts get the typed error instead
of a prompt that would hang forever.
'''

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from ..core.config import SUPPORTED_BROWSERS, _app_dir

_DASHBOARD = 'https://developer.spotify.com/dashboard'

#: Only these keys are ever written by the wizard. Everything else already in the
#: file is read back and preserved untouched.
_MANAGED = ('spotify_client_id', 'spotify_client_secret', 'output_dir',
            'acoustid_api_key', 'delivery', 'youtube_cookies_from_browser')


def config_path() -> Path:
    '''The one config file the wizard reads and writes.'''
    return _app_dir('config') / 'config.toml'


def _read_existing(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    import tomllib
    try:
        with path.open('rb') as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        # A broken file shouldn't block setup. We'll just rewrite it clean.
        return {}


def clean_input(s: str) -> str:
    '''
    Strips a paste down to printable characters and nothing else.

    A hidden-input paste once smuggled a run of \x7f bytes into a client secret.
    .strip() only trims the ends so they rode straight into config.toml and made it
    unparseable, and then every command died on load. Worst case now is a wrong but
    valid value, which is a clean auth error instead of a brick.
    '''
    return ''.join(ch for ch in s if ch.isprintable()).strip()


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    # Belt to clean_input's suspenders: even if a control char reaches
    # here from somewhere else, it never lands in the file.
    text = ''.join(ch for ch in str(value) if ch.isprintable())
    escaped = text.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _dump_toml(data: dict[str, Any]) -> str:
    '''
    A tiny TOML emitter: flat keys first, then one [table.sub] section per
    nested dict (export profiles need those). Still not worth a dependency.
    '''
    lines, tables = [], []
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, dict):
            for sub, body in value.items():
                if not isinstance(body, dict):
                    continue
                tables.append(f'\n[{key}.{sub}]')
                tables += [f'{k} = {_toml_value(v)}' for k, v in body.items()
                           if v is not None]
            continue
        lines.append(f'{key} = {_toml_value(value)}')
    return '\n'.join(lines + tables) + '\n'


def write_config(**updates: Any) -> Path:
    '''
    Merge `updates` into the existing config file and write it back. Returns the
    path written. Only non-None updates land. Existing keys survive.
    '''
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_existing(path)
    for key, value in updates.items():
        if value is not None:
            data[key] = value
    path.write_text(_dump_toml(data), encoding='utf-8')
    return path


def run_setup_wizard(console: Console | None = None, *, reason: str | None = None) -> bool:
    '''
    Walks through creating a Spotify app and pasting the credentials in. Returns
    True if they got saved and False if they bailed. Interactive only, so the
    caller checks for a terminal first.
    '''
    console = console or Console(highlight=False)

    console.print('\n[bold cyan]spotdl+ setup[/]')
    if reason:
        console.print(f'[grey58]{reason}[/]')
    console.print(
        'To download, spotdl+ needs a free Spotify app of your own, it takes '
        'about a minute. Opening the Spotify dashboard in your browser now.\n')

    try:
        webbrowser.open(_DASHBOARD)
    except Exception:  # noqa: BLE001  # A headless box just gets the link below
        pass

    console.print(f'[grey58]If it did not open, go to:[/] {_DASHBOARD}\n')
    console.print(' 1. Log in (or make a free Spotify account) and click '
                  '[bold]Create app[/].')
    console.print(' 2. Name it anything, "my music" is fine.')
    console.print(' 3. Where it asks which APIs you plan to use, tick '
                  '[bold]Web API[/]. Do not skip this one. An app without it '
                  'still gives you keys, and then every playlist you ask for '
                  'comes back refused.')
    console.print(' 4. For the redirect URI, put '
                  '[bold]http://localhost:8080/callback[/]. We never send you '
                  'there, but the app wants one before it behaves.')
    console.print(' 5. Tick the agreement and [bold]Save[/], then open the app, '
                  'click [bold]Settings[/], and copy the two values below.\n')

    client_id = clean_input(typer.prompt('Paste your Client ID'))
    client_secret = clean_input(typer.prompt('Paste your Client Secret', hide_input=True))

    if not client_id or not client_secret:
        console.print('[yellow]No credentials entered, setup canceled.[/]')
        return False

    # An output folder too, while we're here. Enter accepts the sensible default.
    default_out = str(Path.home() / 'Music' / 'spotdl+')
    out = clean_input(typer.prompt('Where should your music be saved?', default=default_out))

    # And the format. Most people want the small, high-quality default. IPod /
    # Apple Music users need a format those actually play.
    console.print('\n[grey58]Do you use an iPod or the Apple Music app? If so, '
                  'we\'ll save in the format they like (AAC .m4a).[/]')
    ipod = typer.confirm('Save in iPod / Apple Music format?', default=False)
    delivery = 'ipod' if ipod else 'archive'

    # Browser cookies. The big reliability lever. Optional. Enter skips it.
    console.print('\n[grey58]Which web browser do you normally use? Lending its '
                  'YouTube cookies makes downloads far more reliable on strict '
                  'networks. Press Enter to skip.[/]')
    browser = clean_input(typer.prompt('Browser (chrome / firefox / edge / brave, or blank)',
                                       default='', show_default=False)).lower()
    if browser and browser not in SUPPORTED_BROWSERS:
        console.print(f'[yellow]  {browser!r} isn\'t one we know, skipping. '
                      f'(known: {", ".join(SUPPORTED_BROWSERS)})[/]')
        browser = ''

    # AcoustID: optional, free, and worth a sentence. It powers teh deep audit.
    # Acoustically confirming each file IS the recording it claims to be.
    console.print('\n[grey58]One optional extra: an AcoustID key lets "spotdlp audit"'
                  ' verify tracks by their actual sound, not just their tags. '
                  'It\'s free and takes a minute.[/]')
    acoustid_key = ''
    if typer.confirm('Set up acoustic verification?', default=False):
        console.print('[grey58]Opening acoustid.org. Register the application, '
                      'then paste the API key it gives you.[/]')
        try:
            webbrowser.open('https://acoustid.org/new-application')
        except Exception:  # noqa: BLE001  # Headless boxes just type the URL
            pass
        acoustid_key = clean_input(typer.prompt('AcoustID API key (blank to skip)',
                                                default='', show_default=False))

    path = write_config(
        spotify_client_id=client_id,
        spotify_client_secret=client_secret,
        output_dir=out or default_out,
        delivery=delivery,
        youtube_cookies_from_browser=browser or None,
        acoustid_api_key=acoustid_key or None,
    )
    console.print(f'\n[green]Saved.[/] [grey58]({path})[/]')

    # Everything below came out of watching somebody use this for the first
    # time. All of it was a real dead end for them, none of it was guessable.
    console.print('\n[bold]Three things that trip people up:[/]')
    console.print('  [bold]Put quotes around links.[/] A Spotify share link has '
                  'an "&" in it, and without quotes your terminal cuts the '
                  'command in half and you get a confusing error about nothing.')
    console.print('     [grey58]spotdlp get "https://open.spotify.com/playlist/'
                  '..."[/]')
    console.print('  [bold]Playlists have to be public.[/] We sign in as an app '
                  'and never as you, so a private playlist is invisible even '
                  'when it is your own. Open the playlist menu and hit "Make '
                  'public", grab it, set it back after if you want.')
    console.print('  [bold]Close your browser fully before a big run.[/] We '
                  'borrow its YouTube cookies, and Windows will not hand them '
                  'over while it is open. Downloads still work without them, '
                  'just with less cover against bot walls.')

    console.print('\nYou\'re ready. Try:  [bold]spotdlp get "artist:Duster"[/]')
    console.print('[grey58]If something goes wrong later, run "spotdlp doctor '
                  '--network". It checks every layer and names the one that '
                  'broke, which beats guessing.[/]\n')
    return True
